#!/usr/bin/env python
"""测试残留判据：**跑完测试后，库里不许多出持久对象**（ISS-147）。

## 为什么需要这个（§4.2-B 的机制化）

`docs/研发进度复盘_2026-09-29.md` §4.2-B 现场发现：默认测试库累积到
**74,145 行** `projects`（跨 9 天，仍在增长），全量 pytest 因此从秒级
退化到 **13~20 分钟**（WP24 在干净库上 9s，差约 130 倍）。
§4.2-B 的建议原文：

> 需清库，并增加「测试残留」判据（`check_test_db_isolation` 管不到这个）

**清库做完了**（2026-09-29，`postgres` 库 665,951 行 → 0）。本脚本补的是
后半句 —— 「残留判据」。

## 已有判据管不到这件事

| 判据 | 管什么 | 为什么管不到残留 |
|---|---|---|
| `check_test_db_isolation.py` | DSN **是否可被环境变量覆盖** | 那是"可诊断性"，与"跑完留不留垃圾"是**两个独立事实**（ISS-108 vs 本项） |
| `drop_own_tables`（conftest） | 夹具**建表前**清理**自己**的表 | 它管不到：① 运行期动态建的**索引**（不属任何包的 `create_all`）② 测试直接 `CREATE` 的探针表 |
| 各包证据脚本 | 单包行为 | 无跨包、跨运行的"库整体有没有变脏"视角 |

## 判据设计：**前后差分**，不是"查名单"

```
1. 对目标库做**结构快照**（表 / 索引 / 视图 / 序列 / 触发器 / 约束）
2. 跑一遍 pytest
3. 再做一次快照，**相减**
4. 差集非空 → 报红，逐条列出"谁在什么时候多建了什么"
```

为什么用差分而不是"找名字像测试垃圾的表"（那条路**必然**变成人写名单，
铁律 14）：差分**不需要知道垃圾长什么样** —— 它只要求"跑测试前后结构不变"。
一个从不被清理的新索引和一个从不被清理的新表，在差分里是**同一种东西**，
不需要为每种形态预先想一个名字。

⚠️ 反过来，"名字名单"式的判据（`like '%probe%'`）对本仓**实测无效**：
真实残留是 `ix_chunk_embeddings_hnsw_<profile_uuid>`（8 个），
名字里**没有任何**测试特征词 —— 它看起来就像个正常业务索引。

## 已实测到的事实（本条判据的活靶子）

2026-09-29 现场：`rednote_test` 里有 **8 个** `hnsw` 索引，
名字形如 `ix_chunk_embeddings_hnsw_d5498743_611a_4b7d_93f8_2cf16db882b3`。
来源：`test_knowledge_wp21.py` 的
`test_partial_hnsw_index_built_on_expression` / `test_query_plan_uses_the_profile_partial_index`
用 `emb_ddl.hnsw_index_ddl()` 建索引后**从不删**
（`embedding_ddl.hnsw_index_drop_ddl` 是零调用点）。
每跑一次 WP21 测试，残留就多几个 —— **正是 §4.2-B 那种"跨天累积"**。

## 为什么必须真跑一遍 pytest（不能只查现状）

"查现状"与"差分"的区别：现状里可能有**迁移该有的**对象（36 个触发器、
52 张表都是合法的），靠现状无法区分"合法"与"残留"。只有**跑一次测试**、
看**它**多建了什么，才是"残留"的定义。因此本脚本**会执行 pytest**。

跑法：
    python tools/quality/check_test_residue.py
    # 默认对 `rednote_test` 跑 `-m "not slow"`；可用环境变量调
    REDNOTE_TEST_RESIDUE_PYTEST_ARGS='-m smoke' python tools/quality/check_test_residue.py
退出码 0 = 跑测试前后结构一致（无残留）；1 = 有残留（逐条列出）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "services" / "backend"

#: 目标库 DSN —— **本脚本只读结构 + 跑测试**，不改任何业务数据。
#: 默认口径与本仓 `conftest.py` 一致（库名不是 `postgres`，ISS-145）。
DEFAULT_TEST_DSN = "postgresql+psycopg://rednote@localhost:5432/rednote_test"

#: 默认 pytest 参数。用 `-m "not slow"` 而不是全量：slow 集要造大量数据，
#: 而"有没有残留"与数据量无关 —— 残留是**结构**层面的。
#: ⚠️ `-m` 的表达式**必须是一个 argv 元素**（`"-m", "not slow"`）。
#: 直接用 `str.split()` 会把它拆成 3 个 → pytest 拿 `slow` 当路径 →
#: "no tests ran"（**退出码 4**）—— 于是"没跑"伪装成"跑过且无残留"（ISS-143）。
DEFAULT_PYTEST_ARGS: tuple[str, ...] = ("-p", "no:cacheprovider", "-m", "not slow")


#: 快照要看的对象类别（每类一个独立 SQL）。**逐类列理由**：
#:   · table    —— 残留最重的一种（§4.2-B 的 74,145 行就是它）；
#:   · index    —— 本仓实测残留的主力（8 个 HNSW）；
#:   · view/matview —— 测试可能建视图做投影验证；
#:   · sequence —— serial/identity 的副产物；
#:   · trigger  —— 迁移建的触发器是合法的，但**重复建**会暴露残留；
#:   · constraint —— 部分唯一索引之类的旁路。
#:   · 唯一约束/主键归 constraint（`pg_constraint`）。
SNAPSHOT_QUERIES: dict[str, str] = {
    "table": (
        "select c.relname from pg_class c"
        " join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname = 'public' and c.relkind = 'r'"
    ),
    "index": (
        "select c.relname || '.' || i.relname from pg_index x"
        " join pg_class i on i.oid = x.indexrelid"
        " join pg_class c on c.oid = x.indrelid"
        " join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname = 'public'"
    ),
    "view": (
        "select c.relname from pg_class c"
        " join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname = 'public' and c.relkind in ('v', 'm')"
    ),
    "sequence": (
        "select c.relname from pg_class c"
        " join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname = 'public' and c.relkind = 'S'"
    ),
    "trigger": (
        "select c.relname || '.' || t.tgname from pg_trigger t"
        " join pg_class c on c.oid = t.tgrelid"
        " join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname = 'public' and not t.tgisinternal"
    ),
    "constraint": (
        "select c.relname || '.' || con.conname from pg_constraint con"
        " join pg_class c on c.oid = con.conrelid"
        " join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname = 'public'"
    ),
}


def _test_dsn() -> str:
    return os.environ.get("REDNOTE_TEST_DATABASE_URL", DEFAULT_TEST_DSN)


def _psycopg_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


def _snapshot() -> dict[str, set[str]]:
    """读目标库的结构快照。**纯只读**（只有 select）。"""
    import psycopg

    with psycopg.connect(_psycopg_dsn(_test_dsn())) as conn:
        snap: dict[str, set[str]] = {}
        for kind, sql in SNAPSHOT_QUERIES.items():
            snap[kind] = {row[0] for row in conn.execute(sql).fetchall()}
    return snap


def _diff(before: dict[str, set[str]], after: dict[str, set[str]]) -> dict[str, set[str]]:
    """跑测试**之后多出来**的对象。少了的对象不算残留（那是清理，属好事）。"""
    return {
        kind: (after[kind] - before[kind])
        for kind in SNAPSHOT_QUERIES
        if after[kind] - before[kind]
    }


def _parse_pytest_args(raw: str) -> list[str]:
    """把环境变量里的参数串按 **shell 语义**解析（尊重引号）。

    ⚠️ 不要用 `str.split()`：`-m "not slow"` 会被拆成 `['-m', '"not', 'slow"']`，
    pytest 拿 `slow` 当路径 → "no tests ran" → 退出码 4。
    实测踩过一次（见 `DEFAULT_PYTEST_ARGS` 注释）。
    """
    import shlex

    return shlex.split(raw)


def _parse_pytest_counts(tail: str) -> tuple[int, int]:
    """从 pytest 输出尾部解析 `(通过数, 收集数)`。

    只用它做"**测试真的跑了**"的前置判断（ISS-143：没跑与跑失败同形）。
    返回 `(0, 0)` 表示解析不出 —— 由调用方决定这算不算失败。
    """
    import re

    passed = 0
    m = re.search(r"(\d+)\s+passed", tail)
    if m:
        passed = int(m.group(1))
    total = passed
    m2 = re.search(r"(\d+)\s+(?:failed|error)", tail)
    if m2:
        total += int(m2.group(1))
    return passed, total


def _classify(names: set[str], kind: str) -> str:
    """给残留加一句"它大概是哪来的" —— 让人能顺着去修，而不是只看一堆名字。

    这里**允许**做名字启发式：它只影响**提示文案**，不影响判定。
    判定完全由差分决定（差分不看名字）—— 所以启发式错了也不会漏判/误判。
    """
    if kind == "index":
        if any("_hnsw_" in n.lower() or n.lower().endswith("hnsw") for n in names):
            return (
                "  可能来源：`test_knowledge_wp21.py` 用 "
                "`embedding_ddl.hnsw_index_ddl()` 建了部分 HNSW 索引后未删"
                "（`hnsw_index_drop_ddl` 零调用点）"
            )
    if kind == "table" and any("probe" in n.lower() or "smoke" in n.lower() for n in names):
        return "  可能来源：M0 探针表（`wp05_vec_probe` / `smoke_meta`）—— 若无清理属预期内，请登记豁免理由"
    return ""


def main() -> int:
    dsn = _test_dsn()
    raw_args = os.environ.get("REDNOTE_TEST_RESIDUE_PYTEST_ARGS", "")
    pytest_args = _parse_pytest_args(raw_args) if raw_args else list(DEFAULT_PYTEST_ARGS)

    print("测试残留判据（ISS-147）：跑一遍测试，库里**不许多出结构对象**")
    print(f"  目标库：{dsn}")
    print(f"  pytest：{' '.join(pytest_args)}")
    print()

    try:
        before = _snapshot()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 连不上目标库（{type(exc).__name__}: {exc}）")
        print("  本条判据需要一个**真库**才能定义'残留'—— 没有真库就没有可对账的对象。")
        return 1

    print(f"  [1/3] 跑测试**前**快照：{_summarize(before)}")

    env = {**os.environ, "NO_PROXY": "*", "PYTHONPATH": "src"}
    env.setdefault("REDNOTE_TEST_DATABASE_URL", dsn)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *pytest_args],
        capture_output=True,
        text=True,
        cwd=str(BACKEND),
        env=env,
    )
    pytest_tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-8:])
    print(f"  [2/3] pytest 退出码={proc.returncode}")
    for line in pytest_tail.splitlines():
        print(f"        {line}")

    # ---- 前置：测试必须**真的跑过**（ISS-143：没跑与跑失败同形）----
    # 退出码 4 = "no tests ran"（用法错/参数错）。它与"跑过且无残留"在
    # **结构差分上看不出区别** —— 两者都是"没多出东西"。因此这里必须显式拦住。
    passed, collected = _parse_pytest_counts(pytest_tail)
    if collected == 0:
        print()
        print("[FAIL] **测试一个都没跑**（退出码 "
              f"{proc.returncode}）—— 差分无意义，不能当成'无残留'。")
        print("  最常见原因：`REDNOTE_TEST_RESIDUE_PYTEST_ARGS` 里 `-m` 的表达式被拆开，")
        print("  pytest 拿 `slow` 当路径（正确写法：-m 'not slow'，本脚本已按 shell 语义解析）。")
        return 1
    if proc.returncode != 0:
        # 测试本身红了 → 差分"跑在错的底座上"，结论不可信。
        print()
        print(f"[FAIL] pytest 自身未通过（退出码 {proc.returncode}，"
              f"passed={passed}）—— 测试红了时差分不可信，先修测试。")
        return 1

    try:
        after = _snapshot()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 跑完测试后连不上库（{type(exc).__name__}: {exc}）")
        return 1

    residue = _diff(before, after)
    print(f"  [3/3] 跑测试**后**快照：{_summarize(after)}")
    print()

    if not residue:
        print("[PASS] 跑测试前后库结构一致 —— 无残留。")
        return 0

    total = sum(len(v) for v in residue.values())
    print(f"[FAIL] 跑测试留下了 {total} 个持久对象：\n")
    for kind in SNAPSHOT_QUERIES:
        names = residue.get(kind)
        if not names:
            continue
        print(f"  [{kind}] 新增 {len(names)} 条：")
        for n in sorted(names):
            print(f"    - {n}")
        hint = _classify(names, kind)
        if hint:
            print(hint)
        print()

    print(
        "含义：测试**建了东西但没清理**。本仓实测过它的代价 ——\n"
        "默认测试库累积 74,145 行、全量 pytest 从秒级退化到 13~20 分钟（§4.2-B）。\n"
        "结构残留（本判据）比数据残留更阴：`drop_own_tables` 清的是**自己建的表**，\n"
        "而运行期动态建的索引不属任何包的 `create_all`，没人管 → 跨天累积。"
    )
    return 1


def _summarize(snap: dict[str, set[str]]) -> str:
    return " / ".join(f"{k}={len(v)}" for k, v in snap.items())


if __name__ == "__main__":
    sys.exit(main())
