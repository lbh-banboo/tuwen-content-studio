#!/usr/bin/env python
"""WP25 证据采集脚本。

产出的文件（落在 `evidence/m3/`）：

| 文件 | 内容 |
|---|---|
| `wp25-pricing-budget-ledger.log` | 5 张表结构 / 约束 / 2 个不可变触发器 / 1 条部分唯一索引，+ **库内活性验证 + 纯函数判据** |

## 与前几个包的**结构性异同**

- 与 WP24 相同：`ddl.consistency_items()` 返回 **空列表**（有意形状），本包**没有**
  "注册表行与领域表行必须配对"这一类跨表一致性不变量。因此验证重量同样压在
  **部分唯一索引的双向活性** 与 **不可变表的"该拦的拦 / 该过的过"对照** 上。
- 与 WP24 不同（**本包独有**）：
  1. **两条部分唯一索引 → 只有 1 条**（`uq_usage_ledger_idempotency`），
     但多了**一条带 `NULLS NOT DISTINCT` 的普通唯一索引**
     （`uq_pricing_versions_provider_model_currency_from`）。后者的反向验证
     与部分索引**不同形**：部分索引要证明"`WHERE` 让 NULL 行不被约束"，
     `NULLS NOT DISTINCT` 要证明"NULL 被当作相等值而**照样**被约束"——
     两个方向的断言**恰好相反**，只测一边会漏掉整类失效（下方 I2 / I5）。
  2. **两张不可变表**（`pricing_versions` / `usage_ledger`），因此"该拦的拦"
     从 2 条变 4 条（`UPDATE` / `DELETE` × 2 表）—— 触发器里 `UPDATE` 与
     `DELETE` 是两个分支，**必须分别证明**。
  3. **`within_limit` 是恒真式**（`committed + reserved <= limit`，只在
     "绕过条件 UPDATE 直接写"时才响）—— 见判据纪律第 2 条，本脚本对它的
     探针形态写在 D 节注释里。

## 覆盖范围由**磁盘**决定，不由手写名单决定（判据纪律第 14 条）

本包 CHECK 29 条 / FK 12 条 / UQ 1 条**全部**由两侧机械枚举后**逐条对账**：

- 期望侧：`billing_models.Base.metadata.tables[t]` 的 `sa.CheckConstraint` /
  `ForeignKeyConstraint` / `UniqueConstraint` 集合（来自 `models.py`）；
- 实际侧：`pg_constraint` 里该表的同名集合。

因此"少了一条约束"或"多了一条约束"都会在**那一张表**上报红，并打印差集。
这比写死一个总数（`== 29`）强：总数相等但成员不同（换了一条）时，
总数判据是**恒绿**的（判据纪律第 4 条：两条判据必须独立）。

## 探针数据的处置

每条验证跑在**独立连接**里，最后**无条件 `rollback()`** → **零残留**。
理由：`pricing_versions` / `usage_ledger` 是**不可变表**（触发器拦 `DELETE`），
一旦 commit 就删不掉（WP18 的 ISS-63、WP24 的坑 4 已实测）。

**本脚本用在哪个库**：默认 `rednote_verify_wp25`（收口时新建的干净库，
迁移从 bootstrap 一路跑到 head 建出全部结构）。用它的理由与 WP24 完全一致：
分支测试库里的表是**测试夹具 `create_all` 建的**，不是迁移建的 ——
在那种库上取证据，等于在验证夹具而不是验证迁移。
（实测：本机的 `rednote_wp25` 里 `projects` 有 39738 行测试残留，
不适合作为"迁移建出的干净库"证据来源。）

## 跨包回归项（本包最容易误伤别人的地方）

- 共用的不可变触发器函数 `rednote_reject_mutation()` 必须仍在
  （本包 `downgrade` **刻意不删它** —— 所有不可变表共用）；
- WP18 / WP20 / WP22 / WP23 / WP24 的一致性函数必须存活（下界判据）；
- 本包**不拥有** `providers` / `model_profiles` / `projects` / `users` 等表，
  只作为 FK 目标引用它们 —— 探针用的是**独立事务 + rollback**，不污染它们。

## 条数 = 89（分节实测）

| 节 | 内容 | 条数 |
|---|---|---|
| S1~S4 | 迁移 head / 5 表存在 / 库总表数下界 / 5 表列数 | 4 |
| S5~S7 | 逐表 CHECK(5) + FK(5) + 唯一约束(1) 名单对账 | 11 |
| S8~S9 | CHECK 总数 / FK 总数 | 2 |
| S10~S19 | 触发器集合/函数、共用函数、无一致性不变量(有意形状)、索引规格漂移、部分索引规格数、普通索引规格数、部分索引不在 pg_constraint、部分索引 WHERE、`NULLS NOT DISTINCT` | 10 |
| S20 | 跨包 `rednote_check_*` 存活（4 个包分列） | 4 |
| I1~I7 | 两条唯一索引的**双向**活性（7 条） | 7 |
| T | 4 条"该拦的拦" + 3 条"该过的过" | 7 |
| D1~D22 | 形状约束（含 6 条"合法值必须通过"的对照） | 22 |
| P1~P9 | 纯函数层（含 1 条反向） | 10 |
| K1~K3 | 契约 | 3 |
| Z | 零残留 | 9 |
| **合计** | | **89** |

## 探针自身踩过的坑（写在这里，避免重复踩）

本节是"证据脚本自己出错"的记录 —— 与 WP18 的 ISS-63、WP23 的四处坑、
WP24 的七处坑同源（探针出错会**伪装成实现有缺陷**，比不写探针更危险）。

1. **★ 期望条数必须实测，不能凭印象**（WP24 坑 2 的**同型复发**）：
   本脚本初稿把 `EXPECTED_CHECKS` 写成 **77**，实跑输出 **89**。
   危害不是"数字不好看"：`complete = len(checks) >= EXPECTED_CHECKS`
   在 77 时**恒真**（89 永远 >= 77），于是"静默少跑 12 条"**永远不会报红** ——
   这正是 ISS-43 要防的事，而写低期望值把这道防线自己拆了。
2. **`providers.network_mode` 取值域是 `LOCAL` / `EXTERNAL`，不是 `DIRECT`**
   （初稿按印象写 `DIRECT`，`ck_providers_network_mode_valid` 当场报红）。
   凡是**别的包拥有**的表，其取值域必须**当场查库**而不是照抄别包的探针。
3. **`usage_ledger` 的 `USAGE` 分录必须带 `quantity`**（`usage_requires_quantity`）：
   初稿用 `USAGE` + 无 quantity 去测幂等键重复，结果**报红的原因错了**
   （是缺 quantity，不是撞幂等键）—— 与 WP24 坑 5（撞了迁移的真值行）
   **同型**：**"被拒"的结论正确不代表"被拒的原因"正确**。
4. **一条语句报错会把整个事务打成 aborted**（后续语句返回 `25P02`）。
   因此"被拦"与"对照通过"这两类探针**必须各自独立事务与独立连接**。
5. **不可变表探针绝不能 commit**：`pricing_versions` / `usage_ledger` 一旦
   提交就**永远删不掉**（触发器拦 `DELETE`）。因此每条探针独立连接 +
   无条件 `rollback()` ⇒ 零残留（Z 节 9 条判据专门核对这一点）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "services" / "backend"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"

# 期望条数（与内联脚本里的 EXPECTED_CHECKS 保持同值）。
# 两处都要：内联脚本用它写进 RESULT 行，外层用它判定成功与否（ISS-43）。
EXPECTED_CHECKS: int = 89

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

INSPECT_SCRIPT = r"""
# 内联脚本：只允许单行注释，且不能出现三引号（三引号会提前终止外层包裹，见 ISS-45）。

import json
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from rednote.modules.billing import ddl as billing_ddl
from rednote.modules.billing import models as billing_models

# 期望产生的核对条数（ISS-43：崩溃伪装成"全过"的防线）。
EXPECTED_CHECKS = 89

DB = "postgresql+psycopg://rednote@localhost:5432/rednote_verify_wp25"
engine = create_engine(DB)

CONTRACT_PATH = Path(
    "/Users/<user>/内容运营AI Native/rednote-production/contracts/openapi.json"
)

# 本包引入的迁移版本。判据用"不早于"而不是"恰好等于"（ISS-44：
# 硬编码"恰好停在某个版本"会让每个新工作包把历史证据集体打红）。
REVISION_INTRODUCED = "daea2b27212c"

# 本包 5 张表的期望列数（按 information_schema **实测**填写，不凭印象）。
TABLE_COLUMNS = (
    ("pricing_versions", 10),
    ("cost_estimates", 15),
    ("project_budgets", 8),
    ("budget_reservations", 12),
    ("usage_ledger", 13),
)

# 本包 1 条部分唯一索引（名字 + 必须出现在 indexdef 里的 WHERE 片段）。
# 它**不在 `pg_constraint` 里** —— 只查约束表完全看不到它（WP22 已登记此坑）。
PARTIAL_INDEX = "uq_usage_ledger_idempotency"

# 本包 1 条带 NULLS NOT DISTINCT 的唯一约束索引（与部分索引**方向相反**）。
NULLS_NOT_DISTINCT_INDEX = "uq_pricing_versions_provider_model_currency_from"

# 契约里本包的 10 个路径 / 14 个操作。
OWN_PATHS = (
    "/api/v1/billing/reconciliations",
    "/api/v1/budget-reservations/{reservationId}/releases",
    "/api/v1/budget-reservations/{reservationId}/settlements",
    "/api/v1/cost-estimates/{estimateId}",
    "/api/v1/pricing-versions",
    "/api/v1/projects/{projectId}/budget",
    "/api/v1/projects/{projectId}/budget-reservations",
    "/api/v1/projects/{projectId}/cost-estimates",
    "/api/v1/projects/{projectId}/ledger-entries",
    "/api/v1/projects/{projectId}/ledger-summary",
)

# 探针用的标记：全部探针数据的名字都带它，便于零残留核对。
PROBE_ISSUER = "wp25-probe"

# 不可变违反的 sqlstate（触发器 ERRCODE）。
IMMUTABLE_VIOLATION_SQLSTATE = "23001"


def _revision_is_at_least(current, introduced):
    # 当前 head 是否**不早于** introduced —— 沿真实迁移链判断，不是猜。
    #
    # **为什么不能写 `== EXPECTED_REVISION`**（ISS-44）：那会让每个新工作包
    # 把前面所有包的历史证据集体打红，因为校验的其实是"迁移停在某个特定
    # 版本"，而真正该校验的是"**该包引入的表/约束还在**"（由本脚本后面的
    # 核对项保证）。
    #
    # **为什么也不能用 `len(current) >= len(introduced)`**（ISS-115）：
    # 本项目全部 revision id 都是 12 位十六进制，于是"长度不短于"在
    # `current != introduced` 时**恒为 True** —— 那是把等式判据换成了
    # **恒绿判据**（判据纪律第 7/9 条）。
    #
    # 正确做法：拿到**真实迁移链**，按位置比较。`walk_revisions(base=..., head=...)`
    # 的返回值是 head→base 降序，因此 index 小 = 新。
    # 该调用会 import 每个迁移模块，故内联段必须在 PYTHONPATH=<repo>/src 下运行。
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo = Path.cwd().resolve()
    while repo != repo.parent and not (repo / "migrations" / "alembic.ini").exists():
        repo = repo.parent
    cfg = Config(str(repo / "migrations" / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo / "migrations"))
    chain = [
        r.revision
        for r in ScriptDirectory.from_config(cfg).walk_revisions(
            base="base", head="heads"
        )
    ]
    # 认不出来的 revision（拼错/已删除）一律判否 —— fail-closed。
    if current not in chain or introduced not in chain:
        return False
    return chain.index(current) <= chain.index(introduced)


def emit(ok, label, detail):
    print(f"CHECK {'OK  ' if ok else 'FAIL'} {label}: {detail}")


def sqlstate(exc):
    return getattr(exc.orig, "sqlstate", None)


def constraint_name(exc):
    return getattr(getattr(exc.orig, "diag", None), "constraint_name", None)


def _expect_check(label, conn, action, sqlstate_expected="23514", constraint=None):
    # 公共外壳：执行 action，断言被 CHECK 拦下且约束名对得上。
    # **不吞异常**：action 成功也要报 FAIL（"见谁都拒"的反面同样是缺陷）。
    try:
        action()
        emit(False, label, "竟然写进去了 —— 约束未生效")
    except DBAPIError as exc:
        name = constraint_name(exc)
        ok = sqlstate(exc) == sqlstate_expected and (
            constraint is None or name == constraint
        )
        emit(ok, label, f"sqlstate={sqlstate(exc)} constraint={name}")


def _expect_ok(label, conn, action, detail=""):
    # 反向外壳：action **必须**成功。用于"该过的过"的对照。
    # 一条语句报错会把事务打成 aborted（后续返回 25P02），因此每条探针
    # 自带独立连接与事务（WP24 的坑 3）。
    try:
        action()
        emit(True, label, detail or "写入成功（符合预期）")
    except DBAPIError as exc:
        emit(False, label, f"被拒了 —— 约束锁过头：sqlstate={sqlstate(exc)}"
                           f" constraint={constraint_name(exc)}")


def _seed_tx(conn):
    # 造最小可用的 user / brand / product / project / provider / model_profile。
    # 非空集合全部按 information_schema 实测（本项目已多次踩中列名/非空）。
    #   · providers      : network_mode 取值域是 LOCAL/EXTERNAL（不是 DIRECT！）
    #   · model_profiles : 非空 = provider_id/model_name/capabilities
    #   · projects       : project_code 唯一；owner_id/target_delivery_count/due_date 非空
    uid = uuid.uuid4()
    brand = uuid.uuid4()
    prod = uuid.uuid4()
    proj = uuid.uuid4()
    prov = uuid.uuid4()
    mp = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, display_name, role, status)"
            " VALUES (:u, :iss, :sub, 'WP25 探针', 'ADMIN', 'ACTIVE')"
        ),
        {"u": str(uid), "iss": PROBE_ISSUER, "sub": f"probe-{uid}"},
    )
    conn.execute(
        text("INSERT INTO brands (id, name, status) VALUES (:b, :n, 'ACTIVE')"),
        {"b": str(brand), "n": f"WP25 品牌 {str(brand)[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO products (id, brand_id, name, model_code, status)"
            " VALUES (:p, :b, :n, :c, 'ACTIVE')"
        ),
        {
            "p": str(prod), "b": str(brand),
            "n": f"WP25 产品 {str(prod)[:8]}", "c": f"WP25-{str(prod)[:8]}",
        },
    )
    conn.execute(
        text(
            "INSERT INTO projects (id, project_code, name, brand_id, product_id,"
            " owner_id, target_delivery_count, due_date)"
            " VALUES (:p, :c, :n, :b, :pr, :o, 10, now() + interval '30 days')"
        ),
        {
            "p": str(proj), "c": f"WP25-{str(proj)[:8]}",
            "n": f"WP25 探针项目 {str(proj)[:8]}",
            "b": str(brand), "pr": str(prod), "o": str(uid),
        },
    )
    conn.execute(
        text(
            "INSERT INTO providers (id, name, network_mode, endpoint)"
            " VALUES (:v, :n, 'EXTERNAL', 'https://wp25.test')"
        ),
        {"v": str(prov), "n": f"WP25 供应商 {str(prov)[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO model_profiles (id, provider_id, model_name, capabilities)"
            " VALUES (:m, :v, :n, '[]'::jsonb)"
        ),
        {"m": str(mp), "v": str(prov), "n": f"wp25-probe-{str(mp)[:8]}"},
    )
    return {
        "user": str(uid), "brand": str(brand), "product": str(prod),
        "project": str(proj), "provider": str(prov), "model": str(mp),
    }


def _pricing_tx(conn, ids, *, currency="CNY", label="探针价", model=None, at=None):
    # 插一条 pricing_versions。`at` 为 None 时用 now()；否则用给定时间戳
    # （用于"同一 effective_from"的重复插入探针）。
    conn.execute(
        text(
            "INSERT INTO pricing_versions (id, provider_id, model_profile_id, state,"
            " currency, label, unit_prices, effective_from)"
            " VALUES (gen_random_uuid(), :v, :m, 'DRAFT', :cur, :lab,"
            " '{\"CALL\": \"1.00000000\"}'::jsonb, coalesce(:at, now()))"
        ),
        {
            "v": ids["provider"], "m": model,
            "cur": currency, "lab": label, "at": at,
        },
    )


def _ledger_tx(conn, ids, *, kind="USAGE", amount="1.0", quantity="1", unit="CALL",
               reverses=None, idem=None, description=None):
    # 插一条 usage_ledger。
    #   · USAGE 必须带 quantity（usage_requires_quantity）
    #   · quantity 与 unit 必须同时给或同时不给（quantity_unit_paired）
    #   · REVERSAL 必须带 reverses 且金额为负
    conn.execute(
        text(
            "INSERT INTO usage_ledger (id, project_id, entry_kind, currency, amount,"
            " quantity, unit, reverses_ledger_id, idempotency_key, description,"
            " occurred_at)"
            " VALUES (gen_random_uuid(), :p, :k, 'CNY', :amt, :q, :u, :rev, :idem,"
            " :descr, now())"
        ),
        {
            "p": ids["project"], "k": kind, "amt": amount,
            "q": quantity, "u": unit,
            "rev": reverses, "idem": idem, "descr": description,
        },
    )


# ===========================================================================
# S 节 —— 迁移与结构（期望侧来自 models 元数据，实际侧来自 pg_constraint）
# ===========================================================================

with engine.connect() as conn:
    head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    emit(
        _revision_is_at_least(head, REVISION_INTRODUCED),
        "[S1] 迁移 head 不早于本包引入的版本",
        f"head={head} introduced={REVISION_INTRODUCED}",
    )

    present = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema='public'"
            )
        ).fetchall()
    }
    own = tuple(billing_ddl.OWN_TABLES)
    emit(
        set(own) <= present,
        "[S2] 本包 5 张表全部存在",
        f"缺失={sorted(set(own) - present) or '无'}",
    )

    total_tables = len(present)
    # 下界而不是等号：后续包（乃至 M4）会继续加表（判据纪律第 1 条）。
    emit(total_tables >= 57, "[S3] 库总表数下界", f"{total_tables} (下界 57)")

    cols = {
        r[0]: r[1]
        for r in conn.execute(
            text(
                "SELECT table_name, count(*) FROM information_schema.columns"
                " WHERE table_schema='public' GROUP BY table_name"
            )
        ).fetchall()
    }
    drift_cols = [
        f"{t}:{cols.get(t)}!={n}" for t, n in TABLE_COLUMNS if cols.get(t) != n
    ]
    emit(
        not drift_cols,
        "[S4] 5 张表列数全部符合实测值",
        " ".join(f"{t}={cols.get(t)}" for t, n in TABLE_COLUMNS)
        + (f" 漂移={drift_cols}" if drift_cols else ""),
    )

    def _db_names(table, contype):
        return sorted(
            r[0]
            for r in conn.execute(
                text(
                    "SELECT conname FROM pg_constraint"
                    " WHERE conrelid = cast(:t as regclass) AND contype = :c"
                ),
                {"t": table, "c": contype},
            ).fetchall()
        )

    def _model_names(table, kind):
        t = billing_models.Base.metadata.tables[table]
        if kind == "c":
            return sorted(
                c.name for c in t.constraints if isinstance(c, sa.CheckConstraint)
            )
        if kind == "f":
            return sorted(
                c.name
                for c in t.constraints
                if isinstance(c, sa.ForeignKeyConstraint)
            )
        return sorted(
            c.name for c in t.constraints if isinstance(c, sa.UniqueConstraint)
        )

    # CHECK / FK 的**成员逐条对账**（不是只比总数 —— 换一条也能让总数相等）。
    for table in own:
        want, got = _model_names(table, "c"), _db_names(table, "c")
        emit(
            want == got,
            f"[S5] {table} CHECK 名单与 models 逐条一致",
            f"n={len(got)} 缺失={sorted(set(want) - set(got)) or '无'}"
            f" 多余={sorted(set(got) - set(want)) or '无'}",
        )
    for table in own:
        want, got = _model_names(table, "f"), _db_names(table, "f")
        emit(
            want == got,
            f"[S6] {table} FK 名单与 models 逐条一致",
            f"n={len(got)} 缺失={sorted(set(want) - set(got)) or '无'}"
            f" 多余={sorted(set(got) - set(want)) or '无'}",
        )
    for table in own:
        want, got = _model_names(table, "u"), _db_names(table, "u")
        if not want and not got:
            continue
        emit(
            want == got,
            f"[S7] {table} 唯一约束名单与 models 逐条一致",
            f"n={len(got)} want={want} got={got}",
        )

    # 汇总（对上一步的成员对账起"总量兜底"作用；两者独立）。
    tot_chk = sum(len(_db_names(t, "c")) for t in own)
    tot_fk = sum(len(_db_names(t, "f")) for t in own)
    emit(tot_chk == 29, "[S8] 本包 CHECK 总数（实测 29）", tot_chk)
    emit(tot_fk == 12, "[S9] 本包 FK 总数（实测 12）", tot_fk)

    # 触发器集合必须与 ddl 声明一致（不数总个数 —— 别的包也可能挂触发器）。
    declared = set(billing_ddl.trigger_names())
    actual_trg = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid"
                " WHERE NOT t.tgisinternal AND c.relname = ANY(:ts)"
            ),
            {"ts": list(own)},
        ).fetchall()
    }
    emit(
        declared == actual_trg,
        "[S10] 本包触发器集合与 ddl.trigger_names() 一致",
        f"声明={sorted(declared)} 实际={sorted(actual_trg)}",
    )

    # 触发器函数必须**是**那个共用函数（挂错了函数 = 保护语义不对）。
    fns = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT DISTINCT p.proname FROM pg_trigger t"
                " JOIN pg_class c ON c.oid=t.tgrelid"
                " JOIN pg_proc p ON p.oid=t.tgfoid"
                " WHERE NOT t.tgisinternal AND c.relname = ANY(:ts)"
            ),
            {"ts": list(own)},
        ).fetchall()
    }
    emit(
        fns == set(billing_ddl.immutable_function_names()),
        "[S11] 触发器指向共用不可变函数（非自造函数）",
        f"实际={sorted(fns)}",
    )

    # 跨包：共用不可变函数必须仍在（本包 downgrade 刻意不删它）。
    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname = 'rednote_reject_mutation'")
    ).scalar_one()
    emit(n == 1, "[S12] 共用函数 rednote_reject_mutation 仍在", n)

    # ★ 本包**没有**跨表一致性不变量 —— `consistency_items()` 返回空列表，
    # 且那是有意的显式形状。这条断言把"有意为空"与"忘了写"区分开：
    # 若将来有人补上，两条会同时变红，逼他同时更新 ddl 的 docstring 与本脚本。
    emit(
        billing_ddl.consistency_items() == []
        and billing_ddl.consistency_trigger_names() == (),
        "[S13] 本包无跨表一致性不变量（有意形状，非漏写）",
        f"items={billing_ddl.consistency_items()}"
        f" triggers={billing_ddl.consistency_trigger_names()}",
    )

    # ★ ddl.py 的规格与 models.py 的实现逐字段对账（三处副本的唯一机械判据）。
    # 只比对名字不够：名字相同、WHERE 不同是最会漂移的形态。
    drift = billing_ddl.index_spec_drift()
    emit(
        not drift,
        "[S14] 索引规格与 models 实现逐字段一致（键列含顺序 + WHERE）",
        drift or "无漂移",
    )
    emit(
        len(billing_ddl.partial_index_specs()) == 1,
        "[S15] 部分唯一索引规格数 == 1",
        len(billing_ddl.partial_index_specs()),
    )
    emit(
        len(billing_ddl.model_index_specs()) == 10,
        "[S16] 普通索引规格数 == 10",
        len(billing_ddl.model_index_specs()),
    )

    # 部分索引**不应**出现在 pg_constraint（若出现说明写成了普通约束，
    # 那样 `idempotency_key IS NULL` 的行会互相打架 —— 人工分录直接失效）。
    n = conn.execute(
        text("SELECT count(*) FROM pg_constraint WHERE conname = :n"),
        {"n": PARTIAL_INDEX},
    ).scalar_one()
    emit(n == 0, "[S17] 部分唯一索引不在 pg_constraint 里（部分索引固有性质）", n)

    row = conn.execute(
        text(
            "SELECT indexdef FROM pg_indexes"
            " WHERE schemaname='public' AND indexname = :n"
        ),
        {"n": PARTIAL_INDEX},
    ).scalar_one_or_none()
    if row is None:
        emit(False, "[S18] 部分唯一索引存在且含正确的 WHERE", "未找到")
    else:
        # 判据不是"名字在不在"，而是"WHERE 在不在、且指向正确的列"——
        # 名字相同、WHERE 不同（或没有 WHERE）是最可能的漂移形态。
        ok = (
            "UNIQUE INDEX" in row
            and " WHERE " in row
            and "idempotency_key IS NOT NULL" in row
        )
        emit(ok, "[S18] 部分唯一索引存在且含正确的 WHERE", row)

    row = conn.execute(
        text(
            "SELECT indexdef FROM pg_indexes"
            " WHERE schemaname='public' AND indexname = :n"
        ),
        {"n": NULLS_NOT_DISTINCT_INDEX},
    ).scalar_one_or_none()
    emit(
        row is not None and "NULLS NOT DISTINCT" in row,
        "[S19] pricing 唯一索引带 NULLS NOT DISTINCT（NULL 参与唯一性）",
        row or "未找到",
    )

    # 跨包：WP18/20/22/23/24 的一致性函数必须存活，用下界（判据纪律第 1 条）。
    # 另按包分列计数，这样"某个包整批消失"依然能被看见（合计数会被别包掩盖）。
    for _pkg, _lo in (
        ("brief", 4), ("knowledge", 4), ("baseline", 6), ("prompt", 4),
    ):
        n = conn.execute(
            text(
                "SELECT count(*) FROM pg_proc"
                " WHERE proname LIKE 'rednote_check_' || :p || '%'"
            ),
            {"p": _pkg},
        ).scalar_one()
        emit(
            n >= _lo,
            f"[S20] 跨包一致性函数 rednote_check_{_pkg}_* 存活",
            f"{n} (下界 {_lo})",
        )

# ===========================================================================
# I 节 —— 两条唯一索引的**双向**活性（本包核心防线）
# ===========================================================================
#
# ⚠️ 这两条索引的失效方向**恰好相反**，务必分别证明：
#   · 部分索引：WHERE 写错 ⇒ NULL 行**被多余地约束** ⇒ 人工分录第二条被拒；
#   · NULLS NOT DISTINCT：丢掉该子句 ⇒ NULL 行**不再互相冲突** ⇒
#     同一个"无型号"计价版本可以重复发布，而账面看不出异常。
# 只测"重复被拒"（正向）会同时漏掉这两类。

# I1 正向：同四元组重复 → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _pricing_tx(conn, ids, model=ids["model"], at="2026-01-01T00:00:00+08:00")
        _expect_check(
            "[I1] pricing 同四元组重复被拒",
            conn,
            lambda: _pricing_tx(
                conn, ids, model=ids["model"], at="2026-01-01T00:00:00+08:00"
            ),
            sqlstate_expected="23505",
            constraint=NULLS_NOT_DISTINCT_INDEX,
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[I1] pricing 同四元组重复被拒",
         f"探针异常 {type(exc).__name__}: {exc}")

# I2 对照：同一 provider/model/currency、**不同** effective_from → 都能插
# （证明唯一键含 effective_from，而不是"一个型号只能有一个版本"）
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _pricing_tx(conn, ids, model=ids["model"], at="2026-01-01T00:00:00+08:00")
        _expect_ok(
            "[I2] 不同 effective_from 可共存（键含 effective_from）",
            conn,
            lambda: _pricing_tx(
                conn, ids, model=ids["model"], at="2026-02-01T00:00:00+08:00"
            ),
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[I2] 不同 effective_from 可共存", f"探针异常 {type(exc).__name__}: {exc}")

# I3 对照：同一 provider/model/effective_from、**不同** currency → 都能插
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _pricing_tx(
            conn, ids, model=ids["model"], currency="CNY",
            at="2026-01-01T00:00:00+08:00",
        )
        _expect_ok(
            "[I3] 不同 currency 可共存（键含 currency）",
            conn,
            lambda: _pricing_tx(
                conn, ids, model=ids["model"], currency="USD",
                at="2026-01-01T00:00:00+08:00",
            ),
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[I3] 不同 currency 可共存", f"探针异常 {type(exc).__name__}: {exc}")

# I4 ★ NULLS NOT DISTINCT 的反向（证明它确实把 NULL 当相等值）：
#     两条 model_profile_id 均为 NULL、effective_from 相同的行 → **必须被拒**。
#     若这条通过，说明索引退化成普通 UNIQUE（PG 默认 NULL != NULL），
#     于是同一个"无型号"计价版本可以重复发布 —— 而每一行本身都合法，
#     账面完全看不出来。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _pricing_tx(conn, ids, model=None, at="2026-01-01T00:00:00+08:00")
        _expect_check(
            "[I4] 两个 NULL model_profile_id 同 effective_from 被拒"
            "（证明 NULLS NOT DISTINCT 生效）",
            conn,
            lambda: _pricing_tx(
                conn, ids, model=None, at="2026-01-01T00:00:00+08:00"
            ),
            sqlstate_expected="23505",
            constraint=NULLS_NOT_DISTINCT_INDEX,
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[I4] NULLS NOT DISTINCT 生效（NULL 参与唯一性）",
         f"探针异常 {type(exc).__name__}: {exc}")

# I5 ★ 部分索引的反向（证明 WHERE 确实把它收窄了）：
#     三行 idempotency_key 均为 NULL → **都必须能插**。
#     若被拒，说明 WHERE 丢了（退化成"每行都必须唯一"），而人工分录
#     （ADJUSTMENT，没有天然幂等键，只能留 NULL）会**第二条就失败** ——
#     账本唯一的纠错手段当场失效。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        for i in range(3):
            _ledger_tx(
                conn, ids, kind="ADJUSTMENT", amount="1.0",
                quantity=None, unit=None, description=f"人工调整 {i}",
            )
        emit(True, "[I5] 三行 NULL 幂等键都能插（证明 WHERE 收窄生效）",
             "3 行写入成功")
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[I5] 三行 NULL 幂等键都能插", f"探针异常 {type(exc).__name__}: {exc}")

# I6 正向：重复的非空幂等键 → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _ledger_tx(conn, ids, idem="wp25-probe-key-1")
        _expect_check(
            "[I6] 重复非空幂等键被拒",
            conn,
            lambda: _ledger_tx(conn, ids, idem="wp25-probe-key-1"),
            sqlstate_expected="23505",
            constraint=PARTIAL_INDEX,
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[I6] 重复非空幂等键被拒",
         f"探针异常 {type(exc).__name__}: {exc}")

# I7 对照：三个**不同**的非空幂等键 → 都能插（防"见谁都拒"）
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        for k in ("wp25-probe-a", "wp25-probe-b", "wp25-probe-c"):
            _ledger_tx(conn, ids, idem=k)
        emit(True, "[I7] 三个不同幂等键都能插（对照，防见谁都拒）", "3 行写入成功")
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[I7] 三个不同幂等键都能插", f"探针异常 {type(exc).__name__}: {exc}")

# ===========================================================================
# T 节 —— 不可变保护：4 条"该拦的拦" + 3 条"该过的过"
# ===========================================================================
# UPDATE 与 DELETE 是触发器里的两个分支，**必须分别证明**
# （只测 UPDATE 会漏掉"DELETE 分支写成了 INSERT"这种形态）。

for _tbl in billing_ddl.OWN_TABLES:
    if _tbl not in billing_models.IMMUTABLE_TABLES:
        continue
    for _op in ("UPDATE", "DELETE"):
        try:
            with engine.connect() as conn:
                trans = conn.begin()
                ids = _seed_tx(conn)
                if _tbl == "pricing_versions":
                    _pricing_tx(conn, ids, model=ids["model"])
                    sql = (
                        "UPDATE pricing_versions SET label = '改了'"
                        " WHERE provider_id = :k"
                        if _op == "UPDATE"
                        else "DELETE FROM pricing_versions WHERE provider_id = :k"
                    )
                    prm = {"k": ids["provider"]}
                else:
                    _ledger_tx(conn, ids)
                    sql = (
                        "UPDATE usage_ledger SET amount = 2.0 WHERE project_id = :k"
                        if _op == "UPDATE"
                        else "DELETE FROM usage_ledger WHERE project_id = :k"
                    )
                    prm = {"k": ids["project"]}
                _expect_check(
                    f"[T] 不可变表 {_tbl} 的 {_op} 被拦",
                    conn,
                    lambda: conn.execute(text(sql), prm),
                    sqlstate_expected=IMMUTABLE_VIOLATION_SQLSTATE,
                )
                trans.rollback()
        except Exception as exc:  # noqa: BLE001
            emit(False, f"[T] 不可变表 {_tbl} 的 {_op} 被拦",
                 f"探针异常 {type(exc).__name__}: {exc}")

# 对照：三张**可变**表必须能 UPDATE 与 DELETE。
# 若这三条被拦，说明有人把不可变触发器挂错表了 ——
# 而"两张表都锁上"的实现也能让上面 4 条全绿。
for _tbl in ("cost_estimates", "project_budgets", "budget_reservations"):
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            ids = _seed_tx(conn)
            if _tbl == "cost_estimates":
                conn.execute(
                    text(
                        "INSERT INTO cost_estimates (id, project_id, batch_kind,"
                        " quantity, currency, estimated_amount)"
                        " VALUES (gen_random_uuid(), :p, 'SINGLE_IMAGE', 1, 'CNY', 1.0)"
                    ),
                    {"p": ids["project"]},
                )
                conn.execute(
                    text("UPDATE cost_estimates SET quantity = 2 WHERE project_id = :k"),
                    {"k": ids["project"]},
                )
                conn.execute(
                    text("DELETE FROM cost_estimates WHERE project_id = :k"),
                    {"k": ids["project"]},
                )
            elif _tbl == "project_budgets":
                conn.execute(
                    text(
                        "INSERT INTO project_budgets (project_id, currency, limit_amount)"
                        " VALUES (:p, 'CNY', 100.0)"
                    ),
                    {"p": ids["project"]},
                )
                conn.execute(
                    text(
                        "UPDATE project_budgets SET limit_amount = 200.0"
                        " WHERE project_id = :k"
                    ),
                    {"k": ids["project"]},
                )
                conn.execute(
                    text("DELETE FROM project_budgets WHERE project_id = :k"),
                    {"k": ids["project"]},
                )
            else:
                conn.execute(
                    text(
                        "INSERT INTO budget_reservations (id, project_id, state,"
                        " currency, amount)"
                        " VALUES (gen_random_uuid(), :p, 'HELD', 'CNY', 10.0)"
                    ),
                    {"p": ids["project"]},
                )
                conn.execute(
                    text(
                        "UPDATE budget_reservations SET state = 'RELEASED'"
                        " WHERE project_id = :k"
                    ),
                    {"k": ids["project"]},
                )
                conn.execute(
                    text("DELETE FROM budget_reservations WHERE project_id = :k"),
                    {"k": ids["project"]},
                )
            emit(True, f"[T] 对照：可变表 {_tbl} 的 UPDATE/DELETE 能通过",
                 "写入成功（符合预期）")
            trans.rollback()
    except Exception as exc:  # noqa: BLE001
        emit(False, f"[T] 对照：可变表 {_tbl} 的 UPDATE/DELETE 能通过",
             f"探针异常 {type(exc).__name__}: {exc}")

# ===========================================================================
# D 节 —— 形状约束（每条配"合法值必须通过"的对照，防"见谁都拒"）
# ===========================================================================

# D1 账本符号一致性：BILL 负金额 → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _expect_check(
            "[D1] BILL 负金额被拒（amount_sign_matches_kind）",
            conn,
            lambda: _ledger_tx(conn, ids, kind="BILL", amount="-1.0"),
            constraint="ck_usage_ledger_amount_sign_matches_kind",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D1] BILL 负金额被拒", f"探针异常 {type(exc).__name__}: {exc}")

# D2 同条另一分支：REVERSAL 正金额 → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _ledger_tx(conn, ids, kind="USAGE", idem="wp25-probe-rev-target")
        target = conn.execute(
            text(
                "SELECT id FROM usage_ledger WHERE idempotency_key ="
                " 'wp25-probe-rev-target'"
            )
        ).scalar_one()
        _expect_check(
            "[D2] REVERSAL 正金额被拒（同条 CHECK 的另一分支）",
            conn,
            lambda: _ledger_tx(
                conn, ids, kind="REVERSAL", amount="1.0",
                quantity=None, unit=None, reverses=str(target),
            ),
            constraint="ck_usage_ledger_amount_sign_matches_kind",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D2] REVERSAL 正金额被拒", f"探针异常 {type(exc).__name__}: {exc}")

# D3 对照：REVERSAL 负金额且指向真实分录 → 能通过
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _ledger_tx(conn, ids, kind="USAGE", idem="wp25-probe-rev-src")
        target = conn.execute(
            text(
                "SELECT id FROM usage_ledger WHERE idempotency_key ="
                " 'wp25-probe-rev-src'"
            )
        ).scalar_one()
        _expect_ok(
            "[D3] 对照：合法冲正分录能写入",
            conn,
            lambda: _ledger_tx(
                conn, ids, kind="REVERSAL", amount="-1.0",
                quantity=None, unit=None, reverses=str(target),
            ),
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D3] 对照：合法冲正分录能写入",
         f"探针异常 {type(exc).__name__}: {exc}")

# D4 USAGE 必须带 quantity
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _expect_check(
            "[D4] USAGE 无 quantity 被拒（usage_requires_quantity）",
            conn,
            lambda: _ledger_tx(conn, ids, kind="USAGE", quantity=None, unit=None),
            constraint="ck_usage_ledger_usage_requires_quantity",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D4] USAGE 无 quantity 被拒", f"探针异常 {type(exc).__name__}: {exc}")

# D5 quantity 与 unit 必须配对（只给一个）→ 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _expect_check(
            "[D5] quantity 与 unit 只给一个被拒（quantity_unit_paired）",
            conn,
            lambda: _ledger_tx(conn, ids, kind="ADJUSTMENT", quantity="1", unit=None),
            constraint="ck_usage_ledger_quantity_unit_paired",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D5] quantity 与 unit 只给一个被拒",
         f"探针异常 {type(exc).__name__}: {exc}")

# D6 非法 unit → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _expect_check(
            "[D6] 非法 unit 被拒（unit_valid）",
            conn,
            lambda: _ledger_tx(conn, ids, kind="USAGE", unit="PIXEL"),
            constraint="ck_usage_ledger_unit_valid",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D6] 非法 unit 被拒", f"探针异常 {type(exc).__name__}: {exc}")

# D7 非 REVERSAL 却带 reverses_ledger_id → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _ledger_tx(conn, ids, kind="USAGE", idem="wp25-probe-target-2")
        target = conn.execute(
            text(
                "SELECT id FROM usage_ledger WHERE idempotency_key ="
                " 'wp25-probe-target-2'"
            )
        ).scalar_one()
        _expect_check(
            "[D7] 非 REVERSAL 带 reverses 被拒（reversal_target_matches_kind 另一分支）",
            conn,
            lambda: _ledger_tx(conn, ids, kind="BILL", reverses=str(target)),
            constraint="ck_usage_ledger_reversal_target_matches_kind",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D7] 非 REVERSAL 带 reverses 被拒",
         f"探针异常 {type(exc).__name__}: {exc}")

# D8 冲正自指（reverses 指向自己）→ 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        rid = uuid.uuid4()
        _expect_check(
            "[D8] 冲正自指被拒（reversal_not_self）",
            conn,
            lambda: conn.execute(
                text(
                    "INSERT INTO usage_ledger (id, project_id, entry_kind, currency,"
                    " amount, reverses_ledger_id, occurred_at)"
                    " VALUES (:r, :p, 'REVERSAL', 'CNY', -1.0, :r, now())"
                ),
                {"r": str(rid), "p": ids["project"]},
            ),
            constraint="ck_usage_ledger_reversal_not_self",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D8] 冲正自指被拒", f"探针异常 {type(exc).__name__}: {exc}")

# D9 非法币种 → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _expect_check(
            "[D9] 非法币种被拒（currency_valid）",
            conn,
            lambda: _pricing_tx(conn, ids, model=ids["model"], currency="EUR"),
            constraint="ck_pricing_versions_currency_valid",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D9] 非法币种被拒", f"探针异常 {type(exc).__name__}: {exc}")

# D10 label 空串 → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _expect_check(
            "[D10] label 空串被拒（label_not_blank）",
            conn,
            lambda: _pricing_tx(conn, ids, model=ids["model"], label=""),
            constraint="ck_pricing_versions_label_not_blank",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D10] label 空串被拒", f"探针异常 {type(exc).__name__}: {exc}")

# D11 unit_prices 是数组 → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _expect_check(
            "[D11] unit_prices 非对象被拒（unit_prices_is_object）",
            conn,
            lambda: conn.execute(
                text(
                    "INSERT INTO pricing_versions (id, provider_id, state, currency,"
                    " label, unit_prices, effective_from)"
                    " VALUES (gen_random_uuid(), :v, 'DRAFT', 'CNY', 'A',"
                    " '[1,2]'::jsonb, now())"
                ),
                {"v": ids["provider"]},
            ),
            constraint="ck_pricing_versions_unit_prices_is_object",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D11] unit_prices 非对象被拒", f"探针异常 {type(exc).__name__}: {exc}")

# D12 cost_estimates.lines 非数组 → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _expect_check(
            "[D12] lines 非数组被拒（lines_is_array）",
            conn,
            lambda: conn.execute(
                text(
                    "INSERT INTO cost_estimates (id, project_id, batch_kind, quantity,"
                    " currency, estimated_amount, lines)"
                    " VALUES (gen_random_uuid(), :p, 'SINGLE_IMAGE', 1, 'CNY', 1.0,"
                    " '{}'::jsonb)"
                ),
                {"p": ids["project"]},
            ),
            constraint="ck_cost_estimates_lines_is_array",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D12] lines 非数组被拒", f"探针异常 {type(exc).__name__}: {exc}")

# D13 cost_estimates.quantity = 0 → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _expect_check(
            "[D13] quantity=0 被拒（quantity_positive）",
            conn,
            lambda: conn.execute(
                text(
                    "INSERT INTO cost_estimates (id, project_id, batch_kind, quantity,"
                    " currency, estimated_amount)"
                    " VALUES (gen_random_uuid(), :p, 'SINGLE_IMAGE', 0, 'CNY', 1.0)"
                ),
                {"p": ids["project"]},
            ),
            constraint="ck_cost_estimates_quantity_positive",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D13] quantity=0 被拒", f"探针异常 {type(exc).__name__}: {exc}")

# D14 SETTLED 缺 actual/variance → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _expect_check(
            "[D14] cost_estimates SETTLED 缺 actual 被拒（settled_requires_actual）",
            conn,
            lambda: conn.execute(
                text(
                    "INSERT INTO cost_estimates (id, project_id, batch_kind, quantity,"
                    " currency, state, estimated_amount)"
                    " VALUES (gen_random_uuid(), :p, 'SINGLE_IMAGE', 1, 'CNY',"
                    " 'SETTLED', 1.0)"
                ),
                {"p": ids["project"]},
            ),
            constraint="ck_cost_estimates_settled_requires_actual",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D14] SETTLED 缺 actual 被拒",
         f"探针异常 {type(exc).__name__}: {exc}")

# D15 对照：SETTLED 带 actual + variance → 能通过
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _expect_ok(
            "[D15] 对照：SETTLED 带 actual+variance 能写入",
            conn,
            lambda: conn.execute(
                text(
                    "INSERT INTO cost_estimates (id, project_id, batch_kind, quantity,"
                    " currency, state, estimated_amount, actual_amount, variance_amount)"
                    " VALUES (gen_random_uuid(), :p, 'SINGLE_IMAGE', 1, 'CNY',"
                    " 'SETTLED', 1.0, 1.2, 0.2)"
                ),
                {"p": ids["project"]},
            ),
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D15] 对照：SETTLED 带 actual+variance 能写入",
         f"探针异常 {type(exc).__name__}: {exc}")

# D16 ★ within_limit：本包唯一的**跨列算术**约束。
#     判据纪律第 2 条：它是恒真式的孪生 —— 正常路径由条件 UPDATE 保证，
#     只有"绕过条件 UPDATE 直接写"才响。因此探针**必须**用直接 INSERT/UPDATE
#     造出越界行，否则这条判据永远绿（而那是"没测"而不是"通过"）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        conn.execute(
            text(
                "INSERT INTO project_budgets (project_id, currency, limit_amount,"
                " committed_amount, reserved_amount)"
                " VALUES (:p, 'CNY', 100.0, 60.0, 30.0)"
            ),
            {"p": ids["project"]},
        )
        _expect_check(
            "[D16] committed+reserved > limit 被拒（within_limit）",
            conn,
            lambda: conn.execute(
                text(
                    "UPDATE project_budgets SET reserved_amount = 50.0"
                    " WHERE project_id = :p"
                ),
                {"p": ids["project"]},
            ),
            constraint="ck_project_budgets_within_limit",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D16] committed+reserved > limit 被拒",
         f"探针异常 {type(exc).__name__}: {exc}")

# D17 对照：正好等于 limit → 能通过（边界而不是"小于"）
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _expect_ok(
            "[D17] 对照：committed+reserved == limit 能写入（边界含等号）",
            conn,
            lambda: conn.execute(
                text(
                    "INSERT INTO project_budgets (project_id, currency, limit_amount,"
                    " committed_amount, reserved_amount)"
                    " VALUES (:p, 'CNY', 100.0, 60.0, 40.0)"
                ),
                {"p": ids["project"]},
            ),
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D17] 对照：== limit 能写入", f"探针异常 {type(exc).__name__}: {exc}")

# D18 预算上限为负 → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _expect_check(
            "[D18] limit_amount 负数被拒（limit_non_negative）",
            conn,
            lambda: conn.execute(
                text(
                    "INSERT INTO project_budgets (project_id, currency, limit_amount)"
                    " VALUES (:p, 'CNY', -1.0)"
                ),
                {"p": ids["project"]},
            ),
            constraint="ck_project_budgets_limit_non_negative",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D18] limit_amount 负数被拒", f"探针异常 {type(exc).__name__}: {exc}")

# D19 budget_reservations SETTLED 缺 settled_amount → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _expect_check(
            "[D19] 预占 SETTLED 缺 settled_amount 被拒",
            conn,
            lambda: conn.execute(
                text(
                    "INSERT INTO budget_reservations (id, project_id, state, currency,"
                    " amount)"
                    " VALUES (gen_random_uuid(), :p, 'SETTLED', 'CNY', 10.0)"
                ),
                {"p": ids["project"]},
            ),
            constraint="ck_budget_reservations_settled_amount_matches_state",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D19] 预占 SETTLED 缺 settled_amount 被拒",
         f"探针异常 {type(exc).__name__}: {exc}")

# D20 同条另一分支：非 SETTLED 却带 settled_amount → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _expect_check(
            "[D20] 预占非 SETTLED 带 settled_amount 被拒（同条另一分支）",
            conn,
            lambda: conn.execute(
                text(
                    "INSERT INTO budget_reservations (id, project_id, state, currency,"
                    " amount, settled_amount)"
                    " VALUES (gen_random_uuid(), :p, 'HELD', 'CNY', 10.0, 10.0)"
                ),
                {"p": ids["project"]},
            ),
            constraint="ck_budget_reservations_settled_amount_matches_state",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D20] 预占非 SETTLED 带 settled_amount 被拒",
         f"探针异常 {type(exc).__name__}: {exc}")

# D21 预占金额必须为正
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _expect_check(
            "[D21] 预占金额为 0 被拒（amount_positive）",
            conn,
            lambda: conn.execute(
                text(
                    "INSERT INTO budget_reservations (id, project_id, state, currency,"
                    " amount)"
                    " VALUES (gen_random_uuid(), :p, 'HELD', 'CNY', 0)"
                ),
                {"p": ids["project"]},
            ),
            constraint="ck_budget_reservations_amount_positive",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D21] 预占金额为 0 被拒", f"探针异常 {type(exc).__name__}: {exc}")

# D22 对照：一整套**合法**数据能写完（整体对照 —— 前 21 条全是"断言被拒"，
# 若约束写得过严，只有这一条会红）
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed_tx(conn)
        _pricing_tx(conn, ids, model=ids["model"])
        conn.execute(
            text(
                "INSERT INTO cost_estimates (id, project_id, batch_kind, quantity,"
                " currency, estimated_amount)"
                " VALUES (gen_random_uuid(), :p, 'BATCH_IMAGES', 8, 'CNY', 12.5)"
            ),
            {"p": ids["project"]},
        )
        conn.execute(
            text(
                "INSERT INTO project_budgets (project_id, currency, limit_amount)"
                " VALUES (:p, 'CNY', 1000.0)"
            ),
            {"p": ids["project"]},
        )
        conn.execute(
            text(
                "INSERT INTO budget_reservations (id, project_id, state, currency,"
                " amount)"
                " VALUES (gen_random_uuid(), :p, 'HELD', 'CNY', 12.5)"
            ),
            {"p": ids["project"]},
        )
        _ledger_tx(conn, ids, kind="USAGE", amount="12.5", quantity="8", unit="IMAGE",
                   idem="wp25-probe-happy")
        emit(True, "[D22] 对照：一整套合法数据能写入（整体对照）", "5 表各 1 行成功")
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D22] 对照：一整套合法数据能写入",
         f"探针异常 {type(exc).__name__}: {exc}")

# ===========================================================================
# P 节 —— 纯函数层（落不到数据库层的那几件事）
# ===========================================================================
# ⚠️ 纯函数探针必须对着**真实 API** 写，不能凭印象（WP24 的坑 7：四处记错
# 名字，症状是**一条 ImportError 打包了全部判据**）。
# 因此 except 一律把异常类型与消息原样写进 RESULT 行。

try:
    import uuid as _uuid

    from rednote.modules.billing import money as _money
    from rednote.modules.billing import pricing as _pricing
    from rednote.modules.billing import types as _types

    _ALL3 = tuple(
        [
            _pricing.PricingLine(unit="TOKEN", unit_price="1.0"),
            _pricing.PricingLine(unit="IMAGE", unit_price="2.0"),
            _pricing.PricingLine(unit="SECOND", unit_price="3.0"),
        ]
    )

    # P1 缺必需单位 → 必须报错（不完整的计价版本发布出去会静默少算）
    _missing = tuple(
        [
            _pricing.PricingLine(unit="TOKEN", unit_price="1.0"),
            _pricing.PricingLine(unit="IMAGE", unit_price="2.0"),
        ]
    )
    _raised = None
    try:
        _pricing.validate_pricing_lines(_missing)
    except _pricing.PricingError as exc:
        _raised = str(exc)[:60]
    emit(
        _raised is not None,
        "[P1] validate_pricing_lines 缺必需单位时报错",
        f"PricingError={_raised}",
    )
    # P1 反向（★ 证明上一条能报红）：补齐三个单位必须**不报错**。
    # 不做这一步的话，"见谁都拒"的实现也能让上一条通过。
    _ok_map = _pricing.validate_pricing_lines(_ALL3)
    emit(
        isinstance(_ok_map, dict) and set(_ok_map) == {"TOKEN", "IMAGE", "SECOND"},
        "[P1-] 反向：三单位齐全时返回价格映射（证明 P1 能区分两种实现）",
        f"keys={sorted(_ok_map) if isinstance(_ok_map, dict) else _ok_map}",
    )

    # P2 指纹必须与键序无关（否则同一份计价会算出两个指纹）
    _fp1 = _pricing.pricing_version_fingerprint(
        version_id=_uuid.UUID(int=1), currency="CNY", prices=_ok_map
    )
    _fp2 = _pricing.pricing_version_fingerprint(
        version_id=_uuid.UUID(int=1), currency="CNY",
        prices=dict(reversed(list(_ok_map.items()))),
    )
    emit(_fp1 == _fp2, "[P2] 指纹与价格键序无关",
         f"{_fp1[:24]}... == {_fp2[:24]}...")

    # P3 指纹必须对币种敏感（否则换币种会被当成同一版本复用）
    _fp3 = _pricing.pricing_version_fingerprint(
        version_id=_uuid.UUID(int=1), currency="USD", prices=_ok_map
    )
    emit(_fp1 != _fp3, "[P3] 指纹对币种敏感（换币种不复用）", f"{_fp3[:24]}...")

    # P4 金额严格口径：超出 8 位小数必须**报错**而不是静默截断
    # （静默截断会让"应收"与"实收"之间出现无法追溯的差额）
    _prec = None
    try:
        _money.parse_wire_strict("1.234567891")
    except _money.MoneyPrecisionError as exc:
        _prec = type(exc).__name__
    emit(_prec is not None, "[P4] 金额超 8 位小数时报错（不静默截断）", _prec)

    # P5 存储口径：quantize 截断到 8 位（与 P4 是**两个不同口径**，都要有）
    _q = str(_money.quantize("1.234567891"))
    emit(_q == "1.23456789", "[P5] quantize 截断到 8 位小数", _q)

    # P6 金额合成：estimate_for_metrics 逐行金额之和 == total_of（两处独立算法对账）
    _lines = _pricing.estimate_for_metrics(
        prices=_ok_map,
        metrics={"input_tokens": "10", "image_count": "2", "seconds": "5"},
    )
    _total = _pricing.total_of(_lines)
    _manual = sum(x.amount for x in _lines)
    emit(
        _total == _manual == _money.as_amount("29"),
        "[P6] 逐行金额之和 == total_of == 手算 29（10*1+2*2+5*3）",
        f"total={_total} manual={_manual}",
    )

    # P7 混币种计价必须报错（否则一个版本里两种币的单价被直接相加）
    _mixed = None
    try:
        _pricing.assert_single_currency(["CNY", "USD"])
    except _pricing.PricingError:
        _mixed = True
    emit(_mixed is True, "[P7] 混币种计价版本被拒", f"raised={_mixed}")

    # P8 对照：单币种（含重复）必须通过
    emit(
        _pricing.assert_single_currency(["CNY", "CNY"]) == "CNY",
        "[P8] 对照：单币种（含重复）通过并返回该币种",
        _pricing.assert_single_currency(["CNY", "CNY"]),
    )

    # P9 不可变表集合必须与 ddl 的触发器名一一对应（改一处忘另一处会静默）
    emit(
        tuple(billing_models.IMMUTABLE_TABLES)
        == ("pricing_versions", "usage_ledger")
        and len(billing_ddl.immutable_trigger_names()) == 2,
        "[P9] IMMUTABLE_TABLES 与 immutable_trigger_names 数量一致",
        f"tables={billing_models.IMMUTABLE_TABLES}"
        f" triggers={billing_ddl.immutable_trigger_names()}",
    )
except Exception as exc:  # noqa: BLE001
    emit(False, "[P] 纯函数层（计价校验 / 指纹 / 金额口径）",
         f"探针异常 {type(exc).__name__}: {exc}")

# ===========================================================================
# K 节 —— 契约
# ===========================================================================
try:
    _spec = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    _paths = _spec["paths"]
    _bill = {
        p: v
        for p, v in _paths.items()
        if p in OWN_PATHS
    }
    _missing_paths = sorted(set(OWN_PATHS) - set(_paths))
    emit(
        not _missing_paths,
        "[K1] 契约含本包全部 10 个路径",
        f"缺失={_missing_paths or '无'}",
    )
    _ops = 0
    for _p, _item in _bill.items():
        _ops += sum(
            1 for _m in _item if _m.lower() in ("get", "post", "put", "patch", "delete")
        )
    emit(_ops == 14, "[K2] 本包契约操作数 == 14", f"{_ops}（10 paths）")
    _all_ops = sum(
        1
        for _p, _item in _paths.items()
        for _m in _item
        if _m.lower() in ("get", "post", "put", "patch", "delete")
    )
    _schemas = len(_spec.get("components", {}).get("schemas", {}))
    # 下界而不是等号（判据纪律第 1 条）：后续包会继续加路径。
    emit(
        len(_paths) >= 65 and _all_ops >= 82,
        "[K3] 全库契约规模下界",
        f"paths={len(_paths)}(>=65) ops={_all_ops}(>=82) schemas={_schemas}",
    )
except Exception as exc:  # noqa: BLE001
    emit(False, "[K] 契约核对", f"探针异常 {type(exc).__name__}: {exc}")

# ===========================================================================
# Z 节 —— 零残留（不可变表一旦 commit 就删不掉，故必须核对）
# ===========================================================================
with engine.connect() as conn:
    for _t in billing_ddl.OWN_TABLES:
        n = conn.execute(text(f"SELECT count(*) FROM {_t}")).scalar_one()
        emit(n == 0, f"[Z] 零残留 {_t}", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM users WHERE issuer = :i"), {"i": PROBE_ISSUER}
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 探针用户", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM projects WHERE name LIKE 'WP25 探针项目%'")
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 探针项目", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM providers WHERE name LIKE 'WP25 供应商%'")
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 探针供应商", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM brands WHERE name LIKE 'WP25 品牌%'")
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 探针品牌", f"{n} 行")

print(f"RESULT_EMITTED (期望 {EXPECTED_CHECKS} 条)")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def collect_wp25() -> tuple[Path, bool]:
    env = os.environ.copy()
    env.update(
        {
            "NO_PROXY": "*",
            "no_proxy": "*",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "PYTHONPATH": str(BACKEND_DIR / "src"),
        }
    )
    completed = subprocess.run(
        [PYTHON, "-c", INSPECT_SCRIPT],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    checks = [
        line for line in output.splitlines() if line.startswith(("CHECK", "SKIP"))
    ]
    failures = [line for line in checks if line.startswith("CHECK FAIL")]

    lines = [
        "# WP25 供应商计价 / 预算并发预占 / 费用账本：5 张表 / 29 CHECK / 12 FK"
        " / 2 不可变触发器 / 1 部分唯一索引 + 1 NULLS NOT DISTINCT 唯一索引"
        " / 库内活性验证 + 纯函数判据 / 契约",
        f"# 生成时间: {_now()}",
        "# 目标库: rednote_verify_wp25（收口时由迁移从 bootstrap 建出，非测试夹具建表）",
        "# 命令: cd services/backend && python -c '<information_schema + 试写 + 试插 + 纯函数>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp25-pricing-budget-ledger.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # **条数不足也算失败**（ISS-43：崩溃伪装成"全过"）。
    complete = len(checks) >= EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete)


def main() -> int:
    path, ok = collect_wp25()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())