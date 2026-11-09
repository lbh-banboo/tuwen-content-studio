#!/usr/bin/env python
"""WP20 证据采集脚本。

产出的文件（落在 `evidence/m1/`）：

| 文件 | 内容 |
|---|---|
| `wp20-knowledge.log` | 5 张表结构 / 索引 / 约束 / 6 个触发器 / 迁移版本，+ **八处活性验证** |

**八处活性验证**（结构核对只能证明约束**存在**，不能证明它**生效**）：

1. **OP-88 正向判据生效**：注册一个 `entity_type='PLAN'` 的版本节点，
   再插一行 `knowledge_versions` 指向它 → 断言 INSERT 阶段**不报错**、
   强制校验时被延迟触发器拦下（SQLSTATE `23514`）。这是
   「`entity_type`/`entity_id` 与子表一致」的数据库级证据 ——
   该约束正是 WP11 的欠账、由 WP18 与 WP20 共同闭合。
2. **OP-88 反向判据生效**：`knowledge_assets` 行存在 + `entity_type='KNOWLEDGE'`
   的版本节点 + **不插** `knowledge_versions` 行 → 断言被
   `trg_version_nodes_knowledge_paired` 拦下（`23514`）。
   没有这一条，OP-88 只闭合了一半（"子表指向错节点"能拦，"节点没有子表"漏网）。
   **注意反例的准确形状**：该触发器刻意收窄为
   "`entity_id` 是一个真实存在的逻辑知识时才强制"，否则会把别的包
   尚未建 asset 的合法中间态判成"孤立节点"（WP18 已实测过同型问题）。
   因此"孤立节点"必须理解为"**有** knowledge_assets 行、**无** knowledge_versions 行"。
3. **合法配对必须能通过**：`knowledge_assets` + `KNOWLEDGE` 节点
   （`entity_id = asset_id`）+ `knowledge_versions` → 断言通过强制校验。
   **这一条是前两条的对照组**：一个永远 `RAISE` 的触发器也能让
   "正向拦、反向拦"两条全绿，没有对照组就无法排除"判据把一切都拒绝"。
4. **发布事件的版本归属受管**：试插一行 `knowledge_publications`，
   声明 `asset_id = A` 但 `version_id` 属于 `asset B` → 断言被
   `trg_knowledge_publications_asset_scope` 拦下（`23514`）。
   这条防的是"投影会算出别人的版本是本知识的当前版本"。
5. **生效版本归属受管**：把 `asset. active_version_id` 改成一个
   **不属于本知识**的版本 → 断言被 `trg_knowledge_assets_active_scope` 拦下（`23514`）。
6. **不可变表真的不可篡改**：对真实 `knowledge_versions` 行与
   `knowledge_publications` 行试写 `UPDATE` / `DELETE`，断言被
   `rednote_reject_mutation()` 拦下（`23001`）。
   **同时**断言 `knowledge_chunks`（**刻意不是不可变表**）的 UPDATE **能通过**
   —— 否则一个"对所有表都加锁"的实现也能让前两条全绿，
   而它会挡住 WP21 的"换模型重切块"。
7. **`FACT` 不许 GLOBAL 范围**（本包最实质的一条 CHECK）：
   试插 `category='FACT', scope='GLOBAL'` → 断言被
   `ck_knowledge_assets_fact_requires_concrete_scope` 拦下（`23514`）；
   **对照组**：`category='STYLE', scope='GLOBAL'` 必须能通过。
   一个全局事实在下游会被不加区分地写进任何产品的文案里 —— 那是不可撤回的错误。
8. **候选"不直接生效"受管**：
   · `state='PENDING'` 且 `confirmed_by` 非空 → 被拦；
   · `state='REJECTED'` 且无 `decision_reason` → 被拦；
   · `proposed_payload = '{}'` → 被拦；
   · **对照组**：`state='PENDING'` + `confirmed_by=NULL` + 非空 payload → 通过。

**跨包回归项**（本包最容易误伤别人的地方）：

- 共用的不可变触发器函数 `rednote_reject_mutation()` 必须仍在
  （本包 `downgrade` 刻意不删它 —— 所有不可变表共用）；
- WP15 / WP16 / WP17 / WP18 的不可变触发器必须仍存在（本包**新增 2 个**，
  总数从 8 变 10）；
- `knowledge_publications` 的 `sequence` 唯一约束必须存在
  （它是本包**唯一的真并发保护** —— 不是 ETag）；
- `ix_knowledge_assets_fact_lookup` 的 `WHERE` 必须还在
  （部分索引 WHERE 静默退化是本项目踩过的坑）；
- `knowledge_assets` / `knowledge_versions` 的两条指向 `version_nodes`
  的外键必须是 `DEFERRABLE INITIALLY DEFERRED`。

**探针数据的处置**：每条验证跑在**独立连接**里，用
`SET CONSTRAINTS ALL IMMEDIATE` 强制触发延迟判据拿到结论，
最后**无条件 rollback** → **零残留**。理由：`knowledge_versions` /
`knowledge_publications` 都不可变（触发器拦 DELETE），一旦提交就删不掉，
且它们用外键钉住 `knowledge_assets`、`version_nodes`、`users`（外键 RESTRICT）
—— 提交后连"清理可变部分"都做不到（WP18 的 ISS-63 已实测）。

**为什么用 `SET CONSTRAINTS ALL IMMEDIATE` 而不是 `trans.commit()`**：
本机实测 `commit()` 之后再 `rollback()` 只能回滚"空事务"，数据已落库；
而本包的探针造的都是不可变表的行 → 会污染证据库。强制校验把
"提交时才跑"的延迟触发器提前到当前语句执行，事务仍未提交，
因此 rollback 之后一行不留。

**本脚本用在哪个库**：默认 `rednote_verify_wp20`（**收口时新建的干净库**，
迁移从 bootstrap 一路跑到 head 建出全部结构）。用它的理由：
分支测试库 `rednote_test_wp20` 里的表是**测试夹具 create_all 建的**，
不是迁移建的 —— 在那种库上取证据，等于在验证夹具而不是验证迁移。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "services" / "backend"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m1"

# 期望条数（与内联脚本里的 EXPECTED_CHECKS 保持同值）。
# 两处都要：内联脚本用它写进 RESULT 行，外层用它判定成功与否（ISS-43）。
EXPECTED_CHECKS: int = 54

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

INSPECT_SCRIPT = r"""
# 内联脚本：只允许单行注释，且不能出现三引号（三引号会提前终止外层包裹，见 ISS-45）。

import json
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

# 期望产生的核对条数（ISS-43：崩溃伪装成"全过"的防线）。
EXPECTED_CHECKS = 54

DB = "postgresql+psycopg://rednote@localhost:5432/rednote_verify_wp20"
engine = create_engine(DB)

CONTRACT_PATH = Path(
    "/Users/<user>/内容运营AI Native/rednote-production/contracts/openapi.json"
)

# 该包**引入**的迁移版本；判据是"不早于"而不是"恰好等于"（ISS-44）。
REVISION_INTRODUCED = "ebc05dfca16b"

TABLE_COLUMNS = [
    ("knowledge_assets", 12),
    ("knowledge_versions", 11),
    ("knowledge_publications", 8),
    ("knowledge_chunks", 9),
    ("knowledge_proposals", 13),
]
SMOKE_TABLES = ("smoke_meta", "wp05_vec_probe")

# 本包应建的 6 个触发器。
KNOWLEDGE_TRIGGERS = (
    "trg_knowledge_versions_immutable",
    "trg_knowledge_publications_immutable",
    "trg_knowledge_versions_node_consistency",
    "trg_version_nodes_knowledge_paired",
    "trg_knowledge_publications_asset_scope",
    "trg_knowledge_assets_active_scope",
)

# 不可变保护那 2 个（**不含 knowledge_chunks** —— 它刻意不是不可变表）。
KNOWLEDGE_IMMUTABLE_TRIGGERS = (
    "trg_knowledge_versions_immutable",
    "trg_knowledge_publications_immutable",
)

# 本包新增的一致性函数（downgrade 时按"是不是我建的"删这 4 个）。
KNOWLEDGE_FUNCTIONS = (
    "rednote_check_knowledge_version_consistency",
    "rednote_check_knowledge_node_paired",
    "rednote_check_knowledge_publication_scope",
    "rednote_check_knowledge_active_scope",
)

# 部分索引：WHERE 静默退化是本项目踩过的坑，必须逐条核对（WP12）。
PARTIAL_INDEXES = [
    (
        "ix_knowledge_assets_fact_lookup",
        "knowledge_assets",
        "active_version_id IS NOT NULL AND archived_at IS NULL",
    ),
]

# 必须 DEFERRABLE INITIALLY DEFERRED 的两条外键（指向共用表 version_nodes）。
DEFERRED_FKS = (
    "fk_knowledge_assets_active_version_id_version_nodes",
    "fk_knowledge_versions_id_version_nodes",
)

# 跨包不可变触发器总数：WP15/WP16/WP17/WP18 原有 8 个 + 本包 2 个 = 10。
EXPECTED_IMMUTABLE_TOTAL = 10

# 本包 5 张表（零残留判据用）。
OWN_TABLES = (
    "knowledge_assets",
    "knowledge_versions",
    "knowledge_publications",
    "knowledge_chunks",
    "knowledge_proposals",
)


def _revision_is_at_least(current, introduced):
    # 当前 head 是否**不早于** introduced —— 沿真实迁移链判断，不是猜。
    #
    # **为什么不能写 `== EXPECTED_REVISION`**（ISS-44）：那会让每个新工作包
    # 把前面所有包的历史证据集体打红，因为校验的其实是"迁移停在某个特定
    # 版本"，而真正该校验的是"**该包引入的表/约束还在**"（由本脚本后面的
    # 核对项保证）。
    #
    # **为什么也不能用 `len(current) >= len(introduced)`**（ISS-115，2026-09-27
    # 收口轮实测）：本项目全部 16 个 revision id **都是 12 位十六进制**，
    # 于是"长度不短于"在 `current != introduced` 时**恒为 True** ——
    # 那是把等式判据换成了**恒绿判据**，比原缺陷更隐蔽：它永不失败，
    # 因此也永不报警（判据纪律第 7/9 条：判据必须能区分两种实现）。
    # 本项目**存量 12 个脚本全是这个写法**，全部靠它"通过"。
    #
    # 正确做法：拿到**真实迁移链**，按位置比较。`walk_revisions(base=..., head=...)`
    # 的返回值是 head→base 降序，因此 index 小 = 新。
    # ⚠️ 该调用会 import 每个迁移模块（`from rednote.modules...`），
    # 故内联段必须在 `PYTHONPATH=<repo>/services/backend/src` 下运行。
    from pathlib import Path

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


# ---------------------------------------------------------------------------
# 探针的公共前置：造一个项目 + 一个用户（可变表，可正常清理，但仍然整体 rollback）
# ---------------------------------------------------------------------------

def _seed_tx(conn):
    # 造最小可用的 project / user，返回 (project_id, user_id, brand_id, product_id)。
    # 用固定前缀标记探针数据，便于"与基线差分"式判零残留。
    #
    # ⚠️ 列名与非空集合全部按 `information_schema` 实测，**不要凭印象写**
    # （WP18 一次踩中 5 处，本包第一次跑踩了 4 处列名、第二次踩了 5 处非空）：
    #   · users    : 没有 email，是 (issuer, subject) 唯一 —— OIDC 风格
    #   · projects : 没有 code/created_by；**brand_id / product_id /
    #                target_delivery_count / due_date 都是 NOT NULL**，
    #                且 phase 默认 'PREPARATION'（不是 'INTAKE'）
    #   · brands   : 没有 code
    #   · products : 没有 code，是 model_code
    pid = uuid.uuid4()
    uid = uuid.uuid4()
    brand = uuid.uuid4()
    prod = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, display_name, role, status)"
            " VALUES (:u, :iss, :sub, :n, 'ADMIN', 'ACTIVE')"
        ),
        {
            "u": str(uid),
            "iss": "wp20-probe",
            "sub": f"probe-{uid}",
            "n": "WP20 探针",
        },
    )
    conn.execute(
        text("INSERT INTO brands (id, name, status) VALUES (:b, :n, 'ACTIVE')"),
        {"b": str(brand), "n": f"WP20 探针品牌 {str(brand)[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO products (id, brand_id, name, model_code, status)"
            " VALUES (:p, :b, :n, :c, 'ACTIVE')"
        ),
        {
            "p": str(prod),
            "b": str(brand),
            "n": f"WP20 探针产品 {str(prod)[:8]}",
            "c": f"WP20-{str(prod)[:8]}",
        },
    )
    conn.execute(
        text(
            "INSERT INTO projects"
            " (id, project_code, name, brand_id, product_id, owner_id, phase,"
            "  target_delivery_count, due_date, timezone)"
            " VALUES (:p, :c, :n, :b, :pr, :u, 'PREPARATION', 1,"
            "         (now() + interval '30 days')::date, 'Asia/Shanghai')"
        ),
        {
            "p": str(pid),
            "c": f"WP20PROBE{str(pid)[:8]}",
            "n": "WP20 探针项目",
            "b": str(brand),
            "pr": str(prod),
            "u": str(uid),
        },
    )
    return pid, uid, brand, prod


def _asset_tx(conn, pid, uid, *, category="STYLE", scope="GLOBAL"):
    aid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO knowledge_assets"
            " (id, category, title, scope, created_by)"
            " VALUES (:a, :c, :t, :s, :u)"
        ),
        {"a": str(aid), "c": category, "t": "WP20 探针知识", "s": scope, "u": str(uid)},
    )
    return aid


def _node_tx(conn, node_id, entity_type, entity_id, pid, uid, hash_char):
    conn.execute(
        text(
            "INSERT INTO version_nodes"
            " (id, entity_type, entity_id, revision_no, project_id,"
            "  payload_hash, schema_version, created_by)"
            " VALUES (:i, :et, :eid, 2, :p, :h, 1, :u)"
        ),
        {
            "i": str(node_id),
            "et": entity_type,
            "eid": str(entity_id),
            "p": str(pid),
            "h": hash_char * 64,
            "u": str(uid),
        },
    )


def _version_tx(conn, version_id, asset_id, uid, *, body="WP20 探针正文",
                payload='{"x": 1}'):
    # 注意 payload 字面量里**不能有冒号**（JSONB 里的 :1 会被 text() 当绑定参数，
    # 铁律第 4 条）。这里用 {"x": 1} 也不行 —— '1' 前面是冒号。
    # 因此统一走绑定参数 + cast。
    conn.execute(
        text(
            "INSERT INTO knowledge_versions"
            " (id, asset_id, body, structured_payload, change_reason, created_by)"
            " VALUES (:i, :a, :b, cast(:pl as jsonb), :cr, :u)"
        ),
        {
            "i": str(version_id),
            "a": str(asset_id),
            "b": body,
            "pl": payload,
            "cr": "探针变更原因",
            "u": str(uid),
        },
    )


# ---------------------------------------------------------------------------
# 结构核对
# ---------------------------------------------------------------------------
with engine.connect() as conn:
    for table, expected in TABLE_COLUMNS:
        actual = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_schema='public' AND table_name=:t"
            ),
            {"t": table},
        ).scalar_one()
        emit(actual == expected, f"{table} 列数", f"{actual} (期望 {expected})")

    version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    emit(
        _revision_is_at_least(version, REVISION_INTRODUCED),
        "alembic_version 不早于本包引入的版本",
        f"{version} (本包引入 {REVISION_INTRODUCED})",
    )

    for name in KNOWLEDGE_TRIGGERS:
        n = conn.execute(
            text("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname = :n"),
            {"n": name},
        ).scalar_one()
        emit(n == 1, f"触发器存在 {name}", n)

    # 零调用符号反查（阶段 7 的"声明了但一行都没实现"）：本包 ddl.py 暴露的
    # 4 个一致性 builder 必须都真的被 all_trigger_ddl 用上。
    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'rednote_check_knowledge%'")
    ).scalar_one()
    emit(n == len(KNOWLEDGE_FUNCTIONS), "一致性函数已建", f"{n} (期望 {len(KNOWLEDGE_FUNCTIONS)})")

    # CHECK 总数（本包 30 条）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conrelid::regclass::text LIKE 'knowledge_%' AND contype='c'"
        )
    ).scalar_one()
    emit(n == 30, "CHECK 约束总数", f"{n} (期望 30)")

    # UNIQUE 总数（2 条：asset+sequence、version+chunk_no）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conrelid::regclass::text LIKE 'knowledge_%' AND contype='u'"
        )
    ).scalar_one()
    emit(n == 2, "UNIQUE 约束总数", f"{n} (期望 2)")

    # FK 总数（15 条）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conrelid::regclass::text LIKE 'knowledge_%' AND contype='f'"
        )
    ).scalar_one()
    emit(n == 15, "FK 约束总数", f"{n} (期望 15)")

    # 部分索引的 WHERE 必须还在。
    # ⚠️ 判据不能用"整串相等" —— PG 会重写表达式（加括号、改引号），
    # 因此只断言"存在 WHERE" + 关键列名都在 WHERE 里出现。
    for name, table, expected_where in PARTIAL_INDEXES:
        row = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
            {"n": name},
        ).scalar_one_or_none()
        key_parts = [p.strip() for p in expected_where.split(" AND ")]
        ok = (
            row is not None
            and " WHERE " in row
            and all(p.replace(" ", "") in row.replace(" ", "") for p in key_parts)
        )
        emit(ok, f"部分索引 WHERE 未退化 {name}", (row or "缺失")[:160])

    # 两条延迟外键。
    for name in DEFERRED_FKS:
        deferred = conn.execute(
            text(
                "SELECT condeferrable AND condeferred FROM pg_constraint WHERE conname = :n"
            ),
            {"n": name},
        ).scalar_one_or_none()
        emit(deferred is True, f"外键是 DEFERRABLE INITIALLY DEFERRED {name}", deferred)

    # 跨包：共用不可变函数必须仍在（本包 downgrade 刻意不删它）。
    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname = 'rednote_reject_mutation'")
    ).scalar_one()
    emit(n == 1, "共用函数 rednote_reject_mutation 仍在", n)

    # 跨包：不可变触发器总数（WP15~WP18 的 8 个 + 本包 2 个 = 10）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_trigger"
            " WHERE NOT tgisinternal AND tgname LIKE 'trg_%_immutable'"
        )
    ).scalar_one()
    emit(n == EXPECTED_IMMUTABLE_TOTAL, "不可变触发器总数", f"{n} (期望 {EXPECTED_IMMUTABLE_TOTAL})")

    # knowledge_chunks **不该**有不可变触发器（它是可重建的物化结果）。
    n = conn.execute(
        text("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname = :n"),
        {"n": "trg_knowledge_chunks_immutable"},
    ).scalar_one()
    emit(n == 0, "knowledge_chunks 刻意没有不可变触发器", n)

    # 并发保护的真正落点：asset+sequence 唯一约束必须存在。
    n = conn.execute(
        text("SELECT count(*) FROM pg_constraint WHERE conname = 'uq_publications_asset_sequence'")
    ).scalar_one()
    emit(n == 1, "并发保护 uq_publications_asset_sequence 存在", n)

    # G2 三件事：判据不能是"M0 冒烟表还在库里" —— 它只在默认库 postgres 上，
    # 而本脚本跑在收口新建的干净库上（迁移不建它们，所以这里本来就没有）。
    # **G2 真正要求的是「迁移文件里没有删它们的语句」**，因此改查迁移源码。
    migration = Path(
        "/Users/<user>/内容运营AI Native/rednote-production/migrations/versions"
        "/20260923_1411_ebc05dfca16b_wp20_knowledge_crud_and_versions.py"
    )
    src = migration.read_text(encoding="utf-8")
    for t in SMOKE_TABLES:
        # 只看可执行语句，不看注释（注释里**应该**保留那 4 行的原文作为说明）。
        lines = [
            ln.strip()
            for ln in src.splitlines()
            if ln.strip().startswith("op.drop") and t in ln
        ]
        emit(
            not lines,
            f"G2：迁移未删除 M0 冒烟表 {t}",
            "无可执行的 op.drop 语句" if not lines else f"发现 {lines}",
        )
    # G2 第三件：postgresql 方言必须已导入（本包用了 JSONB + 两种 ARRAY）。
    emit(
        "from sqlalchemy.dialects import postgresql" in src,
        "G2：迁移已补 postgresql 方言导入",
        "已导入",
    )

    # 契约增量：本包的 9 个知识操作必须在已导出契约里。
    #
    # ⚠️ 判据范围：数的是**本包自己的 9 个操作**，不是"全契约里 /knowledge* 的操作总数"。
    # 后者会在别的包合法地往同一路径段加端点时变红 —— 而那不是"本包多了或少了一个"。
    # **实测教训**：WP23 加了 `POST /knowledge/proposals`（闭合 OP-150 的候选产生方，
    # 与 WP20 的 `GET /knowledge/proposals` 同路径不同方法）之后，
    # 按全契约计数的版本数到 10 而红 —— 症状是"WP20 的证据挂了"，
    # 真相是"WP23 交付了一个端点"。本项目同一根因已第八次现身
    # （ISS-44/64/71/83，本条是它的证据脚本侧）。
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    #: 本包的 9 个操作**从本包 router 现场取**（不手抄 —— 手抄一份必然与 router 漂移，
    #: 而漂移方向是"证据核对的东西与真实路由不是同一个"，本项目 ISS-45 同族教训）。
    from rednote.modules.knowledge.api import router as knowledge_router

    own_ops = {
        ("/api/v1" + route.path, method.lower())
        for route in knowledge_router.routes
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }
    emit(
        len(own_ops) == 9,
        "本包 router 自己声明 9 个操作",
        len(own_ops),
    )
    present = {
        (p, m)
        for p, item in contract["paths"].items()
        for m in item
        if m in ("get", "post", "put", "patch", "delete")
    }
    missing = sorted(own_ops - present)
    emit(
        not missing,
        "契约含本包全部 9 个 knowledge 操作（缺 " + str(len(missing)) + " 个）",
        "全部在" if not missing else missing,
    )
    #: 本包的 7 条路径（`POST /knowledge/proposals` 由 WP23 提供，路径集合本身不受影响）
    own_paths = {p for p, _ in own_ops} | {"/api/v1/knowledge/proposals"}
    missing_paths = sorted(own_paths - set(contract["paths"]))
    emit(
        not missing_paths,
        "契约含本包的 7 条 knowledge 路径",
        len(own_paths) if not missing_paths else missing_paths,
    )
    emit(
        "/api/v1/knowledge/searches" in contract["paths"],
        "searches 端点已由 WP21 实现并进入契约（协议 §9 第 8 行）",
        "在契约里" if "/api/v1/knowledge/searches" in contract["paths"] else "缺少",
    )


# ---------------------------------------------------------------------------
# 活性验证 1~3：OP-88 正向 / 反向 / 对照组
# ---------------------------------------------------------------------------

def _probe_op88_forward():
    # 正向：节点 entity_type 不是 KNOWLEDGE → 延迟触发器必须拦下。
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, uid, _brand, _prod = _seed_tx(conn)
            aid = _asset_tx(conn, pid, uid)
            vid = uuid.uuid4()
            _node_tx(conn, vid, "PLAN", aid, pid, uid, "a")
            _version_tx(conn, vid, aid, uid)
            # INSERT 阶段不该报错（延迟语义）。
            emit(True, "OP-88 正向：INSERT 阶段不报错（延迟语义）", "未抛异常")
            blocked = False
            detail = ""
            try:
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            except DBAPIError as exc:
                blocked = True
                detail = f"sqlstate={sqlstate(exc)} constraint={constraint_name(exc)}"
            emit(blocked and "23514" in detail, "OP-88 正向：强制校验时被拦下", detail or "未拦下")
        except DBAPIError as exc:
            emit(False, "OP-88 正向：探针自身出错", f"sqlstate={sqlstate(exc)}")
        finally:
            trans.rollback()


def _probe_op88_reverse():
    # 反向：asset 存在 + KNOWLEDGE 节点 + 无 knowledge_versions 行 → 必须拦下。
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, uid, _brand, _prod = _seed_tx(conn)
            aid = _asset_tx(conn, pid, uid)
            vid = uuid.uuid4()
            _node_tx(conn, vid, "KNOWLEDGE", aid, pid, uid, "b")
            # 刻意不插 knowledge_versions 行。
            blocked = False
            detail = ""
            try:
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            except DBAPIError as exc:
                blocked = True
                detail = f"sqlstate={sqlstate(exc)} constraint={constraint_name(exc)}"
            emit(blocked and "23514" in detail, "OP-88 反向：孤立 KNOWLEDGE 节点被拦下", detail or "未拦下")
        except DBAPIError as exc:
            emit(False, "OP-88 反向：探针自身出错", f"sqlstate={sqlstate(exc)}")
        finally:
            trans.rollback()

    # 对照组的第二半：entity_id 不对应真实 asset 时必须**放行**（刻意收窄）。
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, uid, _brand, _prod = _seed_tx(conn)
            vid = uuid.uuid4()
            ghost = uuid.uuid4()
            _node_tx(conn, vid, "KNOWLEDGE", ghost, pid, uid, "c")
            ok = True
            detail = "放行"
            try:
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            except DBAPIError as exc:
                ok = False
                detail = f"sqlstate={sqlstate(exc)} constraint={constraint_name(exc)}"
            emit(ok, "OP-88 反向收窄：entity_id 无对应 asset 时放行（别的包的中间态）", detail)
        except DBAPIError as exc:
            emit(False, "OP-88 反向收窄：探针自身出错", f"sqlstate={sqlstate(exc)}")
        finally:
            trans.rollback()


def _probe_happy_path():
    # 对照组：合法配对（asset + KNOWLEDGE 节点 + version）必须全部通过。
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, uid, _brand, _prod = _seed_tx(conn)
            aid = _asset_tx(conn, pid, uid)
            vid = uuid.uuid4()
            _node_tx(conn, vid, "KNOWLEDGE", aid, pid, uid, "d")
            _version_tx(conn, vid, aid, uid)
            ok = True
            detail = "通过"
            try:
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            except DBAPIError as exc:
                ok = False
                detail = f"sqlstate={sqlstate(exc)} constraint={constraint_name(exc)}"
            emit(ok, "对照组：合法配对通过强制校验（排除「判据拒绝一切」）", detail)

            # 再加一层：发布事件（合法）也必须通过。
            pub_ok = True
            pub_detail = "通过"
            try:
                conn.execute(
                    text(
                        "INSERT INTO knowledge_publications"
                        " (asset_id, version_id, action, sequence, actor_id)"
                        " VALUES (:a, :v, 'ACTIVATE', 1, :u)"
                    ),
                    {"a": str(aid), "v": str(vid), "u": str(uid)},
                )
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            except DBAPIError as exc:
                pub_ok = False
                pub_detail = f"sqlstate={sqlstate(exc)} constraint={constraint_name(exc)}"
            emit(pub_ok, "对照组：合法的 ACTIVATE 发布事件通过", pub_detail)
        except DBAPIError as exc:
            emit(False, "对照组：探针自身出错", f"sqlstate={sqlstate(exc)}")
        finally:
            trans.rollback()


# ---------------------------------------------------------------------------
# 活性验证 4：发布事件引用的版本必须属于该 asset
# ---------------------------------------------------------------------------

def _probe_publication_scope():
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, uid, _brand, _prod = _seed_tx(conn)
            aid_a = _asset_tx(conn, pid, uid)
            aid_b = _asset_tx(conn, pid, uid)
            vid_a = uuid.uuid4()
            _node_tx(conn, vid_a, "KNOWLEDGE", aid_a, pid, uid, "e")
            _version_tx(conn, vid_a, aid_a, uid)
            # 声明 asset_a，但 version 属于 asset_b —— 归属张冠李戴。
            conn.execute(
                text(
                    "INSERT INTO knowledge_publications"
                    " (asset_id, version_id, action, sequence, actor_id)"
                    " VALUES (:a, :v, 'ACTIVATE', 1, :u)"
                ),
                {"a": str(aid_b), "v": str(vid_a), "u": str(uid)},
            )
            blocked = False
            detail = ""
            try:
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            except DBAPIError as exc:
                blocked = True
                detail = f"sqlstate={sqlstate(exc)} constraint={constraint_name(exc)}"
            emit(
                blocked and "23514" in detail,
                "发布事件版本归属：跨 asset 引用被拦下",
                detail or "未拦下",
            )
        except DBAPIError as exc:
            emit(False, "发布事件归属：探针自身出错", f"sqlstate={sqlstate(exc)}")
        finally:
            trans.rollback()


# ---------------------------------------------------------------------------
# 活性验证 5：生效版本必须属于本知识
# ---------------------------------------------------------------------------

def _probe_active_scope():
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, uid, _brand, _prod = _seed_tx(conn)
            aid_a = _asset_tx(conn, pid, uid)
            aid_b = _asset_tx(conn, pid, uid)
            vid_b = uuid.uuid4()
            _node_tx(conn, vid_b, "KNOWLEDGE", aid_b, pid, uid, "f")
            _version_tx(conn, vid_b, aid_b, uid)
            # 把 asset_a 的生效版本指向 asset_b 的版本。
            conn.execute(
                text("UPDATE knowledge_assets SET active_version_id = :v WHERE id = :a"),
                {"v": str(vid_b), "a": str(aid_a)},
            )
            blocked = False
            detail = ""
            try:
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            except DBAPIError as exc:
                blocked = True
                detail = f"sqlstate={sqlstate(exc)} constraint={constraint_name(exc)}"
            emit(
                blocked and "23514" in detail,
                "生效版本归属：指向别人的版本被拦下",
                detail or "未拦下",
            )
        except DBAPIError as exc:
            emit(False, "生效版本归属：探针自身出错", f"sqlstate={sqlstate(exc)}")
        finally:
            trans.rollback()


# ---------------------------------------------------------------------------
# 活性验证 6：不可变 + chunks 刻意可变
# ---------------------------------------------------------------------------

def _probe_immutable_and_chunks_mutable():
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, uid, _brand, _prod = _seed_tx(conn)
            aid = _asset_tx(conn, pid, uid)
            vid = uuid.uuid4()
            _node_tx(conn, vid, "KNOWLEDGE", aid, pid, uid, "g")
            _version_tx(conn, vid, aid, uid)

            # 6a：UPDATE knowledge_versions 必须被拦（23001）。
            b, detail = _blocked(
                conn,
                "UPDATE knowledge_versions SET body = '篡改' WHERE id = :i",
                {"i": str(vid)},
            )
            emit(b and "23001" in detail, "不可变：knowledge_versions UPDATE 被拦下", detail)

            # 6b：DELETE knowledge_versions 必须被拦。
            b, detail = _blocked(
                conn, "DELETE FROM knowledge_versions WHERE id = :i", {"i": str(vid)}
            )
            emit(b and "23001" in detail, "不可变：knowledge_versions DELETE 被拦下", detail)

            # 6c：插一条 publication 后试 UPDATE，必须被拦。
            conn.execute(
                text(
                    "INSERT INTO knowledge_publications"
                    " (asset_id, version_id, action, sequence, actor_id)"
                    " VALUES (:a, :v, 'ACTIVATE', 1, :u)"
                ),
                {"a": str(aid), "v": str(vid), "u": str(uid)},
            )
            pub_id = conn.execute(
                text(
                    "SELECT id FROM knowledge_publications WHERE asset_id = :a AND sequence = 1"
                ),
                {"a": str(aid)},
            ).scalar_one()
            b, detail = _blocked(
                conn,
                "UPDATE knowledge_publications SET reason = '篡改' WHERE id = :i",
                {"i": str(pub_id)},
            )
            emit(
                b and "23001" in detail,
                "不可变：knowledge_publications UPDATE 被拦下",
                detail,
            )

            # 6d **对照组**：knowledge_chunks 的 UPDATE 必须**能通过**
            # （它是可重建的物化结果，WP21 要靠这个实现"换模型重切块"）。
            conn.execute(
                text(
                    "INSERT INTO knowledge_chunks"
                    " (knowledge_version_id, chunk_no, text_content, text_hash,"
                    "  token_count, locator, keywords)"
                    " VALUES (:v, 1, '探针块', :h, 10, cast(:lo as jsonb), cast(:kw as text[]))"
                ),
                {
                    "v": str(vid),
                    "h": "0" * 64,
                    "lo": '{"page": 1}',
                    "kw": [],
                },
            )
            ok = True
            detail = "通过（符合预期：它是可重算的物化结果）"
            try:
                conn.execute(
                    text("UPDATE knowledge_chunks SET token_count = 11 WHERE knowledge_version_id = :v"),
                    {"v": str(vid)},
                )
            except DBAPIError as exc:
                ok = False
                detail = f"sqlstate={sqlstate(exc)}（不该被拦！会挡住 WP21 的重切块）"
            emit(ok, "对照组：knowledge_chunks UPDATE 可改（刻意非不可变）", detail)
        except DBAPIError as exc:
            emit(False, "不可变性：探针自身出错", f"sqlstate={sqlstate(exc)}")
        finally:
            trans.rollback()


# ---------------------------------------------------------------------------
# 活性验证 7：FACT 不许 GLOBAL（含对照组）
# ---------------------------------------------------------------------------

def _probe_fact_scope():
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, uid, _brand, _prod = _seed_tx(conn)
            b, detail = _blocked(
                conn,
                "INSERT INTO knowledge_assets (category, title, scope, created_by)"
                " VALUES ('FACT', '探针事实', 'GLOBAL', :u)",
                {"u": str(uid)},
            )
            emit(
                b and "23514" in detail,
                "FACT 不许 GLOBAL：被 ck_knowledge_assets_fact_requires_concrete_scope 拦下",
                detail,
            )

            # 对照组：STYLE + GLOBAL 必须能通过。
            ok = True
            detail = "通过"
            try:
                _asset_tx(conn, pid, uid, category="STYLE", scope="GLOBAL")
            except DBAPIError as exc:
                ok = False
                detail = f"sqlstate={sqlstate(exc)} constraint={constraint_name(exc)}"
            emit(ok, "对照组：STYLE + GLOBAL 通过（该类别允许全局）", detail)

            # 第二组对照：FACT + PRODUCT 必须能通过（给了具体范围就行）。
            # 直接复用 _seed_tx 已造的 brand/product，不再自建。
            ok = True
            detail = "通过"
            try:
                conn.execute(
                    text(
                        "INSERT INTO knowledge_assets"
                        " (id, category, title, scope, product_id, created_by)"
                        " VALUES (:a, 'FACT', '探针事实', 'PRODUCT', :p, :u)"
                    ),
                    {"a": str(uuid.uuid4()), "p": str(_prod), "u": str(uid)},
                )
            except DBAPIError as exc:
                ok = False
                detail = f"sqlstate={sqlstate(exc)} constraint={constraint_name(exc)}"
            emit(ok, "对照组：FACT + PRODUCT 通过（范围明确即可）", detail)
        except DBAPIError as exc:
            emit(False, "FACT 范围：探针自身出错", f"sqlstate={sqlstate(exc)}")
        finally:
            trans.rollback()


# ---------------------------------------------------------------------------
# 活性验证 8：候选"不直接生效"（PENDING ⇔ 无确认人 + 驳回必须说理由）
# ---------------------------------------------------------------------------

def _probe_proposal_rules():
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, uid, _brand, _prod = _seed_tx(conn)

            # 8a：PENDING 且 confirmed_by 非空 → 拦。
            b, detail = _blocked(
                conn,
                "INSERT INTO knowledge_proposals"
                " (proposed_payload, scope, state, confirmed_by)"
                " VALUES (cast(:pl as jsonb), 'GLOBAL', 'PENDING', :u)",
                {"pl": '{"probe": true}', "u": str(uid)},
            )
            emit(b and "23514" in detail, "候选：PENDING 不得有确认人", detail)

            # 8b：REJECTED 且无 decision_reason → 拦。
            b, detail = _blocked(
                conn,
                "INSERT INTO knowledge_proposals"
                " (proposed_payload, scope, state, confirmed_by)"
                " VALUES (cast(:pl as jsonb), 'GLOBAL', 'REJECTED', :u)",
                {"pl": '{"probe": true}', "u": str(uid)},
            )
            emit(b and "23514" in detail, "候选：驳回必须带理由", detail)

            # 8c：空 payload → 拦（"不直接生效"的结构性落点）。
            b, detail = _blocked(
                conn,
                "INSERT INTO knowledge_proposals"
                " (proposed_payload, scope, state)"
                " VALUES (cast(:pl as jsonb), 'GLOBAL', 'PENDING')",
                {"pl": "{}"},
            )
            emit(b and "23514" in detail, "候选：空 payload 被拦下", detail)

            # 8d 对照组：合法 PENDING（无确认人 + 非空 payload）必须通过。
            ok = True
            detail = "通过"
            try:
                conn.execute(
                    text(
                        "INSERT INTO knowledge_proposals"
                        " (proposed_payload, scope, state)"
                        " VALUES (cast(:pl as jsonb), 'GLOBAL', 'PENDING')"
                    ),
                    {"pl": '{"category": "FACT"}'},
                )
            except DBAPIError as exc:
                ok = False
                detail = f"sqlstate={sqlstate(exc)} constraint={constraint_name(exc)}"
            emit(ok, "对照组：合法的 PENDING 候选通过", detail)
        except DBAPIError as exc:
            emit(False, "候选规则：探针自身出错", f"sqlstate={sqlstate(exc)}")
        finally:
            trans.rollback()


# ---------------------------------------------------------------------------
# 零残留核对（与"运行前基线"差分，不是"全表 0 行"）
# ---------------------------------------------------------------------------

def _probe_no_residue(baseline):
    with engine.connect() as conn:
        for table in OWN_TABLES:
            now = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            emit(
                now <= baseline[table],
                f"零残留 {table}",
                f"现在 {now} / 运行前基线 {baseline[table]}",
            )


def _baseline_counts():
    with engine.connect() as conn:
        return {
            t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
            for t in OWN_TABLES
        }


def _blocked(conn, sql, params=None):
    # 在 SAVEPOINT 内执行一条**预期被拦**的语句，返回 (是否被拦, detail)。
    #
    # **必须用 SAVEPOINT**：PostgreSQL 在任一语句出错后会 abort **整个事务**，
    # 后续语句一律报 `25P02 in_failed_sql_transaction` —— 于是同一探针里的
    # 第 2 条判据根本测不到（本脚本第一次跑就踩到：8 条判据报 25P02）。
    # 用 SAVEPOINT 把失败圈在局部，主事务保持可用。
    #
    # 判据仍要求"真的被拦"，因此未拦下时 SAVEPOINT rollback + 返回 False。
    sp = conn.begin_nested()
    try:
        conn.execute(text(sql), params or {})
    except DBAPIError as exc:
        sp.rollback()
        return True, f"sqlstate={sqlstate(exc)} constraint={constraint_name(exc)}"
    sp.rollback()
    return False, "未拦下"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
baseline = _baseline_counts()
_probe_op88_forward()
_probe_op88_reverse()
_probe_happy_path()
_probe_publication_scope()
_probe_active_scope()
_probe_immutable_and_chunks_mutable()
_probe_fact_scope()
_probe_proposal_rules()
_probe_no_residue(baseline)
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def collect_wp20() -> tuple[Path, bool]:
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
    checks = [line for line in output.splitlines() if line.startswith(("CHECK", "SKIP"))]
    failures = [line for line in checks if line.startswith("CHECK FAIL")]

    lines = [
        "# WP20 知识 CRUD 与版本：5 张表 / 30 CHECK / 6 触发器 / 八处活性验证 / 契约",
        f"# 生成时间: {_now()}",
        f"# 目标库: rednote_verify_wp20（收口时由迁移从 bootstrap 建出，非测试夹具建表）",
        "# 命令: cd services/backend && python -c '<information_schema + 延迟约束试写 + 试插>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp20-knowledge.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # **条数不足也算失败**（ISS-43）。
    complete = len(checks) >= EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete)


def main() -> int:
    path, ok = collect_wp20()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())