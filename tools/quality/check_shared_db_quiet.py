#!/usr/bin/env python
"""共享库静止前提（铁律第 17 条 / ISS-160 / ISS-163）。

## 为什么需要这个

`MEMORY.md` 的第 17 条纪律原文：

> **凡要真跑 pytest / 真库的判据，收口期必须先在私有库上跑**（库名由包号派生，
> 别用共享 `rednote_test`）。「并行会话各自隔离库」此前只写在文档里 ⇒
> 收口期**两次**踩到双向污染。

WP-DEBT-02 收口期的实测代价（ISS-160）：

| 观测 | 值 |
|---|---|
| 本包那一轮的 `backend-pytest.log` | `1 failed, 1146 passed, 639 errors` / EXIT=1 |
| 错误形态 | `QueryCanceled` / `statement timeout` 共 **1917** 次 |
| 639 errors 的分布 | 集中在 4 个文件（`DROP TABLE … CASCADE` 等锁超时） |
| 真正的原因 | 两个会话在 `rednote_test` 上**并发**跑 pytest |

而判断"有没有并行 pytest 在跑"**不能看 `ps`**（本机沙箱会拦 `ps`），
要看 **`pg_stat_activity`**：

```sql
select datname, state, left(query, 60)
from pg_stat_activity
where datname is not null and pid <> pg_backend_pid();
```

**这条纪律此前纯靠自觉** —— 所以它两次失效而不自知（= ISS-95：
「声称自己有防线」比「没有防线」更危险）。

## 判据 E 的做法

```python
assert_quiet(dsn, scope="...")   # 有他人连接即抛 SharedDatabaseBusy（拒跑）
```

四条子判据（缺一不可）：

| # | 判据 | 含义 |
|---|---|---|
| E1 | **能读出目标库名** | 防「连错库 / 连不上」被当成「没人用」（ISS-143 同族：把"没跑"当成"通过"） |
| E2 | **其他连接数 == 0** | 主判据。不为 0 即拒跑，并打印占用者 |
| E3 | **排除自身** | `pid <> pg_backend_pid()`；且用**独立连接**查（不复用被测连接） |
| E4 | **查询失败即拒跑**（fail-closed） | 权限 / 网络问题不能变成「放行」 |

### 为什么 E1 是必要的（它是本判据最易漏的一条）

"其他连接数 == 0" 这个结果有**两种互不相同的来源**，而它们在输出上同形：

  · 真没人用（可以跑）；
  · 我连的不是目标库（DSN 写错 / 环境变量没生效）—— 那个库当然没人用。

若不额外断言「我确实读到了目标库名」，第二种会被当成第一种。
因此 `QuietReport.database` 必须等于目标库名，否则拒跑。

### CI 上的适用性

CI runner **独占** PG 实例（`services: postgres`），"有没有别人在用"没有区分度。
因此本判据在 CI 上应**响亮 SKIP 并写明理由**（`--ci` 参数），
而不是静默通过 —— 「判据在此环境不适用」与「判据不存在」是两件事（ISS-118 同纪律）。

跑法：

    python tools/quality/check_shared_db_quiet.py            # 查默认测试库
    python tools/quality/check_shared_db_quiet.py --ci       # CI：显式 SKIP
    python tools/quality/check_shared_db_quiet.py --dsn <dsn>

作为库使用（推荐 —— 这是第 17 条真正生效的方式）：

    from check_shared_db_quiet import assert_quiet
    assert_quiet(dsn, scope="收口第 11 步")
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

#: 环境变量名（DSN 来源）。**必须流向可覆盖的环境变量**，
#: 否则会被 `tools/quality/check_test_db_isolation.py` 判为硬编码 DSN（ISS-108）。
DB_URL_ENV = "REDNOTE_DATABASE_URL"
TEST_DB_URL_ENV = "REDNOTE_TEST_DATABASE_URL"

#: 默认 DSN 常量（可被上面的环境变量整体覆盖）。口径与本仓 `conftest.py` 一致：
#: 指向**专用测试库**，不是 `postgres` 系统库（WP-DEBT-02 判据 A 守的就是这件事）。
DEFAULT_DATABASE_URL = "postgresql://rednote@localhost:5432/rednote_test"

#: 查询：同库、且不是我自己进程的连接。
_QUERY = """
select
    datname,
    pid,
    coalesce(application_name, '-') as app,
    coalesce(state, '-') as state,
    left(coalesce(query, ''), 60) as q
from pg_stat_activity
where datname is not null
  and pid <> pg_backend_pid()
  and datname = current_database()
order by pid
"""


class SharedDatabaseBusy(RuntimeError):
    """目标库上有他人连接 —— 按第 17 条纪律**拒跑**。"""


class SharedDatabaseUnverifiable(RuntimeError):
    """无法确认目标库是否空闲（连不上 / 读不到库名）—— **fail-closed 拒跑**。"""


@dataclass
class Occupant:
    """一个占用者（他人连接的摘要）。"""

    pid: int
    application_name: str
    state: str
    query: str

    def describe(self) -> str:
        return (
            f"pid={self.pid} app={self.application_name} "
            f"state={self.state} query={self.query!r}"
        )


@dataclass
class QuietReport:
    """静止检查的结果。"""

    database: str
    occupants: list[Occupant] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        return not self.occupants


def _resolve_dsn(dsn: str | None) -> str:
    """解析目标 DSN：显式参数 > `REDNOTE_DATABASE_URL` > `REDNOTE_TEST_DATABASE_URL` > 默认常量。"""
    if dsn:
        return dsn
    return (
        os.environ.get(DB_URL_ENV)
        or os.environ.get(TEST_DB_URL_ENV)
        or DEFAULT_DATABASE_URL
    )


def _dsn_source(explicit: str | None) -> str:
    """回报 DSN 的**来源**（不是 DSN 本身 —— 避免把连接串写进日志）。

    这条信息是 E1 的伴生证据：当输出说「目标库 = rednote_test」时，
    读者必须能判断这个库名是**哪儿来的**（显式参数 / 环境变量 / 默认常量）。

    ⚠️ **2026-10-01 三遍法自纠**：本函数是**后补的**。此前 `main()` 里
    直接 `print(f"  目标 DSN 来源：{_dsn_source(explicit)}")` 调用了一个
    **尚未定义**的函数 ⇒ `NameError` ⇒ 脚本在任何分支下都跑不起来。
    被发现的原因是三遍法第一遍"结构"扫到了未定义引用 ——
    **判据自己的可运行性也得有人判**（ISS-95 的轻量版）。
    """
    if explicit:
        return "--dsn（显式指定）"
    if os.environ.get(DB_URL_ENV):
        return f"环境变量 {DB_URL_ENV}"
    if os.environ.get(TEST_DB_URL_ENV):
        return f"环境变量 {TEST_DB_URL_ENV}"
    return "默认常量 DEFAULT_DATABASE_URL（环境变量未设置）"


def _libpq_dsn(dsn: str) -> str:
    """SQLAlchemy 方言串 → libpq 串（`postgresql+psycopg://` → `postgresql://`）。

    本脚本直接用 `psycopg.connect`，而本仓其它地方用的是 SQLAlchemy 方言串
    （`conftest.py` 的 `postgresql+psycopg://…`），不归一化会抛
    `ProgrammingError: missing "=" after …`（实测）。

    ⚠️ **2026-10-01 三遍法自纠**：在一次"清理死代码"编辑里，
    本函数的 docstring **与函数体**被一并删掉，只剩 `def` 行 ⇒ `SyntaxError`。
    教训：**删注释**这个动作本身也要过一遍语法检查 ——
    `python -m py_compile` 是这条的判据，改完必跑。
    """
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


def probe_quiet(dsn: str | None = None) -> QuietReport:
    """连一次目标库，查同库的他人连接。

    **用独立连接**（E3）：不与任何被测 DSN 复用，避免"用自己的连接数自己"。
    连接用后即关 —— 本函数**不留连接**（否则它自己就成了下一个占用者）。
    """
    import psycopg

    resolved = _resolve_dsn(dsn)
    raw = _libpq_dsn(resolved)
    try:
        with psycopg.connect(raw, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("select current_database()")
                row = cur.fetchone()
                database = str(row[0]) if row else ""
                # ---- E1：必须能读出库名
                if not database:
                    raise SharedDatabaseUnverifiable(
                        "读不到目标库名 —— 无法确认这不是「连错库」"
                    )
                cur.execute(_QUERY)
                occupants = [
                    Occupant(pid=int(r[1]), application_name=str(r[2]),
                             state=str(r[3]), query=str(r[4]))
                    for r in cur.fetchall()
                ]
    except (SharedDatabaseUnverifiable, SharedDatabaseBusy):
        raise
    except Exception as exc:  # ---- E4：fail-closed
        raise SharedDatabaseUnverifiable(
            f"无法连接/查询目标库（{type(exc).__name__}: {exc}）—— 按第 17 条拒跑"
        ) from exc
    return QuietReport(database=database, occupants=occupants)


def assert_quiet(dsn: str | None = None, *, scope: str = "(未命名)") -> QuietReport:
    """第 17 条的**可复用入口**：目标库不静止则拒跑。

    在跑任何真库判据（pytest / 证据采集 / 迁移）**之前**调用它。
    """
    try:
        report = probe_quiet(dsn)
    except (SharedDatabaseBusy, SharedDatabaseUnverifiable) as exc:
        raise SharedDatabaseBusy(f"[{scope}] {exc}") from exc
    if not report.quiet:
        detail = "\n".join(f"    · {o.describe()}" for o in report.occupants)
        raise SharedDatabaseBusy(
            f"[{scope}] 目标库 `{report.database}` 上有 {len(report.occupants)} "
            f"个他人连接 —— 按第 17 条纪律拒跑（并发会双向污染，ISS-160）：\n{detail}\n"
            f"  处置：换私有库（REDNOTE_TEST_DATABASE_URL=…_wpdebtNN），"
            f"或等并行会话结束、PG 回到静止窗后再跑。"
        )
    return report


def main() -> int:
    argv = sys.argv[1:]
    explicit: str | None = None
    if "--dsn" in argv:
        idx = argv.index("--dsn")
        if idx + 1 < len(argv):
            explicit = argv[idx + 1]

    if "--ci" in argv:
        # CI runner 独占 PG 实例 ⇒ 本判据在 CI 上**没有区分度**，必须响亮 SKIP。
        print("共享库静止前提（铁律 17 / ISS-163）")
        print("  [SKIP] 本判据在 CI 上不适用：runner 以 services: postgres 独占库，")
        print("         「有没有别人在用」无区分度 ⇒ 显式跳过，而不是静默通过。")
        print("         本判据的实际载体是收口期的本机步骤（见 tools/m1/rerun_all_evidence.py）。")
        return 0

    print("共享库静止前提（铁律 17 / ISS-163）")
    print(f"  目标 DSN 来源：{_dsn_source(explicit)}")
    try:
        report = probe_quiet(explicit)
    except SharedDatabaseUnverifiable as exc:
        print(f"  [FAIL] {exc}")
        return 1

    print(f"  目标库：{report.database}")
    print(f"  他人连接数：{len(report.occupants)}")
    for occupant in report.occupants:
        print(f"    · {occupant.describe()}")

    if not report.quiet:
        print()
        print(f"[FAIL] 目标库 `{report.database}` 上有 {len(report.occupants)} 个他人连接。")
        print("       按第 17 条纪律**拒跑** —— 并发跑真库判据会双向污染（ISS-160）。")
        print("       处置：改用私有库，或等 PG 回到静止窗。")
        return 1

    print()
    print(f"[PASS] `{report.database}` 静止（无他人连接），可以开始真库判据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
