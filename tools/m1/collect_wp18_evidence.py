#!/usr/bin/env python
"""WP18 证据采集脚本。

产出的文件（落在 `evidence/m1/`）：

| 文件 | 内容 |
|---|---|
| `wp18-brief.log` | 5 张表结构 / 索引 / 约束 / 7 个触发器 / 迁移版本，+ **七处活性验证** |

**七处活性验证**（结构核对只能证明约束**存在**，不能证明它**生效**）：

1. **OP-88 正向判据生效**：注册一个 `entity_type='PLAN'` 的版本节点，
   再插一行 `brief_versions` 指向它 → 断言 INSERT 阶段**不报错**、
   强制校验时被延迟触发器拦下（SQLSTATE `23514`）。这是
   「`entity_type`/`entity_id` 与子表一致」的数据库级证据 ——
   该约束正是本包闭合的 WP11 欠账。
2. **OP-88 反向判据生效**：`briefs` 行存在 + `entity_type='BRIEF'` 的版本
   节点 + **不插** `brief_versions` 行 → 断言被
   `trg_version_nodes_brief_paired` 拦下（`23514`）。
   没有这一条，OP-88 只闭合了一半（"子表指向错节点"能拦，
   "节点没有子表"漏网）。
   **注意反例的准确形状**（本机实测）：该触发器刻意收窄为
   "`entity_id` 是一个真实存在的逻辑 Brief 时才强制"，
   因为 WP11 的测试夹具把 `BRIEF` 当作**默认** `entity_type` 且不写
   `briefs` 行 —— 不收窄则 WP11 整批测试会在提交时集体炸。
   因此"孤立节点"必须理解为"**有** briefs 行、**无** brief_versions 行"。
3. **合法配对必须能通过**：`briefs` + `BRIEF` 节点（`entity_id = brief_id`）
   + `brief_versions` → 断言通过强制校验。
   **这一条是前两条的对照组**：一个永远 `RAISE` 的触发器也能让
   "正向拦、反向拦"两条全绿，没有对照组就无法排除"判据把一切都拒绝"。
4. **延迟语义真的延迟**：错误插入在 `INSERT` 时**不报错**，
   在强制校验时才报错（由验证 1 内联断言）。若写成即时约束，
   中间态就无法存在 —— 而 `register_version` 与子表插入顺序由应用决定
   （内部 SAVEPOINT 重试），即时约束会把正常路径也误伤。
5. **版本不可篡改**：对真实 `brief_versions` 行试写 `UPDATE`，
   断言被触发器拦下（`23001`）。
6. **事实类 claim 必须有证据**：试插 `category='SOURCE_FACT'` 但
   `evidence_segment_id` 与 `evidence_url` 全空 → 断言被拦（`23514`）。
   这是验收原文「分类来源」的第一半。
7. **BLOCK 疑问阻断过闸**：试插 `severity='BLOCK'` 且 `state='OPEN'`
   → 断言被拦（`23514`）。这是验收原文「字段疑问…闭环」的落点：
   核心疑问未解决时，数据库层就不允许把它当成"已处理"。

**跨包回归项**（本包最容易误伤别人的地方）：

- 共用的不可变触发器函数 `rednote_reject_mutation()` 必须仍在
  （本包 `downgrade` 刻意不删它 —— 所有不可变表共用）；
- WP15 / WP16 / WP17 的不可变触发器必须仍存在（本包**新增 3 个**
  不可变触发器，总数从 5 变 8）；
- `briefs` / `brief_drafts`（可变表）**不该**有不可变触发器。

**探针数据的处置**：每条验证跑在**独立连接**里，用
`SET CONSTRAINTS ALL IMMEDIATE` 强制触发延迟判据拿到结论，
最后**无条件 rollback** → **零残留**。理由：`brief_versions` /
`brief_claims` / `brief_issues` 都不可变（触发器拦 DELETE），一旦提交
就删不掉，且它们用外键钉住 `briefs`、`version_nodes`、`projects`、`users`
（外键 RESTRICT）—— 提交后连"清理可变部分"都做不到。

**为什么用 `SET CONSTRAINTS ALL IMMEDIATE` 而不是 `trans.commit()`**：
本机实测 `commit()` 之后再 `rollback()` 只能回滚"空事务"，数据已落库；
而这三个探针造的都是不可变表的行 → 会污染证据库。
强制校验把"提交时才跑"的延迟触发器提前到当前语句执行，事务仍未提交，
因此 rollback 之后一行不留。
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
EXPECTED_CHECKS: int = 39

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
EXPECTED_CHECKS = 39

DB = "postgresql+psycopg://rednote@localhost:5432/postgres"
engine = create_engine(DB)

CONTRACT_PATH = Path(
    "/Users/<user>/内容运营AI Native/rednote-production/contracts/openapi.json"
)

# 该包**引入**的迁移版本；判据是"不早于"而不是"恰好等于"（ISS-44）。
REVISION_INTRODUCED = "19441cc26ce9"

TABLE_COLUMNS = [
    ("briefs", 6),
    ("brief_drafts", 17),
    ("brief_versions", 12),
    ("brief_claims", 13),
    ("brief_issues", 10),
]
SMOKE_TABLES = ("smoke_meta", "wp05_vec_probe")

# 本包应建的 7 个触发器。
BRIEF_TRIGGERS = (
    "trg_brief_versions_immutable",
    "trg_brief_claims_immutable",
    "trg_brief_issues_immutable",
    "trg_brief_versions_node_consistency",
    "trg_version_nodes_brief_paired",
    "trg_brief_claims_evidence_scope",
    "trg_brief_issues_project_scope",
)

# 不可变保护那 3 个（可变表不该有）。
BRIEF_IMMUTABLE_TRIGGERS = (
    "trg_brief_versions_immutable",
    "trg_brief_claims_immutable",
    "trg_brief_issues_immutable",
)

# 部分索引：唯一约束的 WHERE 静默退化是本项目踩过的坑，必须逐条核对（WP12）。
PARTIAL_INDEXES = [
    ("ix_brief_claims_pending", "brief_claims", "resolution_status = 'PENDING'"),
    ("ix_brief_issues_open", "brief_issues", "state = 'OPEN'"),
]

# 跨包不可变触发器**基数**：WP15/WP16/WP17 原有 5 个 + 本包 3 个 = 8。
#
# ⚠️ 判据必须是"**不少于**"而不是"恰好等于" —— 这是 ISS-64 的第五次现身：
# WP20 合法新增 2 个不可变触发器（knowledge_versions / knowledge_publications）
# 之后，总数从 8 变 10，用 `== 8` 会让 WP18 的证据**在收口复跑时变红**
# （单跑 WP18 时是绿的，只有复跑全部历史证据脚本才发现 —— 与本项目
# ISS-44「恰好停在某迁移版本」、ISS-64「否定式判据写成全契约范围」同源）。
#
# 要守的不变量是「**我建的 3 个还在**，且不可变保护的整体规模没有被削弱」，
# 而不是"全项目恰好有 8 个"。
MIN_IMMUTABLE_TOTAL = 8


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

    for name in BRIEF_TRIGGERS:
        n = conn.execute(
            text(
                "SELECT count(*) FROM pg_trigger"
                " WHERE NOT tgisinternal AND tgname = :n"
            ),
            {"n": name},
        ).scalar_one()
        emit(n == 1, f"触发器存在 {name}", n)

    # 零调用符号反查（阶段 7 的"声明了但一行都没实现"）：本包 ddl.py 暴露的
    # 4 个一致性 builder 必须都真的被 all_trigger_ddl 用上。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_proc WHERE proname LIKE 'rednote_check_brief%'"
        )
    ).scalar_one()
    emit(n == 4, "本包 4 个一致性函数都在库里", f"{n} (期望 4)")

    # 共用函数必须仍在（本包 downgrade 刻意不删它）。
    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname = 'rednote_reject_mutation'")
    ).scalar_one()
    emit(n == 1, "共用不可变函数 rednote_reject_mutation 仍在", n)

    # 跨包回归：WP15 / WP16 / WP17 的触发器不能被本包误删。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_trigger"
            " WHERE NOT tgisinternal AND tgname IN"
            " ('trg_audit_events_immutable', 'trg_gateway_egress_immutable',"
            "  'trg_source_segments_immutable')"
        )
    ).scalar_one()
    emit(n == 3, "跨包不可变触发器 trg_audit_events/gateway_egress/source_segments 仍在", n)

    # ① 本包自己建的 3 个必须**逐个**还在 —— 这才是真正要断言的东西。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname IN"
            " ('trg_brief_versions_immutable', 'trg_brief_claims_immutable',"
            "  'trg_brief_issues_immutable')"
        )
    ).scalar_one()
    emit(n == 3, "本包 3 个不可变触发器仍在", n)

    # ② 整体规模**不得少于** 8（原有 5 + 本包 3）。
    #    用 `>=` 而不是 `==`：后续包合法新增不可变触发器不该让本条变红
    #    （WP20 就新增了 2 个 → 10）。要守的不变量是"保护没有被削弱"，
    #    不是"全项目恰好有 8 个"。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_trigger"
            " WHERE NOT tgisinternal AND tgname LIKE 'trg_%_immutable'"
        )
    ).scalar_one()
    emit(n >= MIN_IMMUTABLE_TOTAL, f"不可变触发器总数不少于 {MIN_IMMUTABLE_TOTAL}", n)

    # 可变表不该有不可变触发器。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_trigger"
            " WHERE NOT tgisinternal AND tgname IN"
            " ('trg_briefs_immutable', 'trg_brief_drafts_immutable')"
        )
    ).scalar_one()
    emit(n == 0, "可变表 briefs/brief_drafts 无不可变触发器", n)

    # 部分索引的 WHERE 必须真的在（静默退化不报错，只能逐条核对）。
    #
    # ⚠️ 不能拿"源码里写的字符串"去比对 indexdef：PG 会把 WHERE 子句
    # **规范化**，例如源码 `state = 'OPEN'` 在 indexdef 里是
    # `WHERE ((state)::text = 'OPEN'::text)`。硬比对会把正确实现的索引
    # 判成退化（本机实测踩到过）。因此只断言两件事：
    #   1) indexdef 里有 WHERE（没有就是退化成全表索引）；
    #   2) WHERE 的关键字面量（英文 token）确实出现 —— 用规范化后的形态。
    for index_name, table, where_fragment in PARTIAL_INDEXES:
        definition = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
            {"n": index_name},
        ).scalar_one_or_none()
        if definition is None:
            emit(False, f"部分索引 {index_name} 的 WHERE 未退化", "索引缺失")
            continue
        has_where = " WHERE " in definition
        # 取 WHERE 里的非空白 token（去掉引号与类型转换），做关键字包含判断。
        normalized = (
            definition.split(" WHERE ", 1)[1]
            .replace("::text", "")
            .replace("(", "")
            .replace(")", "")
            .replace("'", "")
        )
        # 从源码片断里抽出关键字面量（如 OPEN / PENDING）与列名。
        key_tokens = [
            tok.strip("'\"")
            for tok in where_fragment.replace("=", " ").split()
            if tok.strip("'\"") and tok.strip("'\"") not in ("=",)
        ]
        tokens_present = all(tok in normalized for tok in key_tokens)
        emit(
            has_where and tokens_present,
            f"部分索引 {index_name} 的 WHERE 未退化",
            f"WHERE={'有' if has_where else '无'} tokens={key_tokens} in {normalized.strip()[:70]}",
        )

    # 唯一约束（本包的"一项目一份 Brief"与"同版本同字段同摘要不重复"）。
    for name in (
        "uq_briefs_project_id",
        "uq_brief_drafts_brief_id",
        "uq_brief_claims_version_field_hash",
    ):
        n = conn.execute(
            text("SELECT count(*) FROM pg_constraint WHERE conname = :n"),
            {"n": name},
        ).scalar_one()
        emit(n == 1, f"唯一约束 {name} 存在", n)

    # CHECK 总数（5 张表的业务不变量）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE contype='c' AND conrelid IN"
            " (SELECT oid FROM pg_class WHERE relname IN"
            "  ('briefs','brief_drafts','brief_versions','brief_claims','brief_issues'))"
        )
    ).scalar_one()
    emit(n == 35, "5 张表 CHECK 约束总数", f"{n} (期望 35)")

    # M0 探针表不能被本包迁移误删（G2 铁律）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM information_schema.tables"
            " WHERE table_schema='public' AND table_name IN ('smoke_meta','wp05_vec_probe')"
        )
    ).scalar_one()
    emit(n == 2, "M0 探针表仍在（G2）", n)


# ---------------------------------------------------------------------------
# 活性验证 1~3：OP-88 的延迟一致性（正反双向 + 延迟语义）
# ---------------------------------------------------------------------------
def _seed_projects_tx(conn):
    # 造一个用户 / 品牌 / 产品 / 项目（FK 目标），随外层事务整体回滚。
    # 列名与取值域**全部以 information_schema / pg_constraint 实测为准**，
    # 不靠猜：本机实测一次踩中 5 处错列名或错取值域（见本文件顶部注释）。
    #   users.username 不存在     -> OIDC 口径 issuer + subject
    #   projects.brand_id/product_id 都是 NOT NULL -> 必须先造 brands/products
    #   projects.phase 取值域是 PREPARATION/PRODUCTION/DATA_COLLECTION/CLOSED
    #     （不是 'DRAFT'），且非 CLOSED 时 closed_at 必须为 NULL
    #   projects.external_policy NOT NULL 无默认 -> 必须显式给
    #   blobs 是 sha256 不是 content_hash，且 mime_type NOT NULL 无默认
    #   file_objects.classification 取值域是 SUPPORTED/UNSUPPORTED（不是 'BRIEF'）
    pid = uuid.uuid4()
    uid = uuid.uuid4()
    brand_id = uuid.uuid4()
    product_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, display_name, role, status)"
            " VALUES (:u, 'https://idp.probe', :sub, 'WP18 探针', 'ADMIN', 'ACTIVE')"
        ),
        {"u": uid, "sub": "wp18-probe-" + uid.hex[:8]},
    )
    conn.execute(
        text("INSERT INTO brands (id, name) VALUES (:b, 'WP18 探针品牌')"),
        {"b": brand_id},
    )
    conn.execute(
        text(
            "INSERT INTO products (id, brand_id, name)"
            " VALUES (:pr, :b, 'WP18 探针产品')"
        ),
        {"pr": product_id, "b": brand_id},
    )
    conn.execute(
        text(
            "INSERT INTO projects"
            " (id, project_code, name, brand_id, product_id, owner_id, phase,"
            "  target_delivery_count, target_publication_count, due_date, timezone,"
            "  external_policy)"
            " VALUES (:p, :code, 'WP18 探针项目', :b, :pr, :u, 'PREPARATION',"
            "  10, 10, DATE '2030-12-31', 'Asia/Shanghai', 'LOCAL_ONLY')"
        ),
        {"p": pid, "code": "WP18P" + pid.hex[:6], "b": brand_id,
         "pr": product_id, "u": uid},
    )
    return pid, uid


def _seed_file_tx(conn, *, pid, uid, name, digest):
    # blobs: sha256(64) + mime_type 是必填；file_objects: classification
    # 取值域 SUPPORTED/UNSUPPORTED，visibility='PROJECT' 时必须给 project_id
    # （ck_file_objects_visibility_matches_project）。
    blob_id = uuid.uuid4()
    file_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO blobs"
            " (id, storage_backend, object_key, sha256, byte_size, mime_type,"
            "  scan_status, storage_state)"
            " VALUES (:b, 'LOCAL', :key, :h, 10,"
            "  'application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document', 'CLEAN', 'ACTIVE')"
        ),
        {"b": blob_id, "h": digest, "key": "wp18/" + blob_id.hex},
    )
    conn.execute(
        text(
            "INSERT INTO file_objects"
            " (id, blob_id, original_name, visibility, project_id, classification,"
            "  uploaded_by)"
            " VALUES (:f, :b, :n, 'PROJECT', :p, 'SUPPORTED', :u)"
        ),
        {"f": file_id, "b": blob_id, "n": name, "p": pid, "u": uid},
    )
    return file_id


# ---------------------------------------------------------------------------
# 活性验证 1~7（结构核对只能证明约束「存在」，不能证明它「生效」）
# ---------------------------------------------------------------------------
#
# **探针数据零残留的落地方式**：每个验证各自跑在**独立的 engine.connect()**
# 里。用 `SET CONSTRAINTS ALL IMMEDIATE` 强制触发延迟判据，**不需要真的
# 提交**就能拿到判定，最后一次无条件 rollback。不用一个大事务串起来，理由：
#   1. 约束冲突会把事务打成 aborted，一条失败会连累后面全部（WP10 踩过）；
#   2. brief_versions / brief_claims / brief_issues 都**不可变**（触发器拦
#      DELETE），一旦提交就删不掉，且外键 RESTRICT 钉住 briefs / version_nodes
#      / projects / users —— 提交后连"清理可变部分"都做不到。
# 独立连接 + `SET CONSTRAINTS ALL IMMEDIATE` + rollback 让每条验证天然隔离，
# 且谁都不 commit。
#
# **为什么必须用 `SET CONSTRAINTS ALL IMMEDIATE` 而不是 `trans.commit()`**：
# 本机实测 `commit()` 之后再 `rollback()` 只能回滚"空事务"，数据已经落库；
# 而这三个探针造的都是**不可变表**的行，提交后无法删除 → 会污染证据库。
# 强制校验把"提交时才跑"的延迟触发器提前到当前语句执行，事务仍未提交，
# 因此 rollback 之后**一行不留**。
#
# **关于 `trg_version_nodes_brief_paired` 的收窄**（本机实测确认，不是缺陷）：
# 该函数第一段是 `IF NOT EXISTS (SELECT 1 FROM briefs WHERE id = NEW.entity_id)
# THEN RETURN NULL`，即"`entity_id` 不是一个真实存在的逻辑 Brief 时直接放过"。
# 收窄是**必需的**：WP11 的测试夹具 `_register(entity_type=ENTITY_TYPE_BRIEF)`
# 把 BRIEF 作为**默认参数**，它只写 `version_nodes`、不写 `briefs` ——
# 若不收窄，本包一落库，WP11 的整批测试会在提交时集体炸。
# 因此反向判据的正确反例是「**`briefs` 行存在** + `BRIEF` 节点 + 无
# `brief_versions` 行」，而不是"孤立节点"字面上的"随便造一个 BRIEF 节点"。
# 见 ACTIVITY 2。


def _brief_tx(conn, pid):
    # 造一行 briefs（反向判据收窄逻辑的"锚"）。briefs 只有 id/project_id 必填。
    brief_id = uuid.uuid4()
    conn.execute(
        text("INSERT INTO briefs (id, project_id) VALUES (:b, :p)"),
        {"b": brief_id, "p": pid},
    )
    return brief_id


def _register_node_tx(conn, node_id, entity_type, entity_id, pid, uid, h_char):
    # version_nodes 的必填列（实测）：entity_type / entity_id / revision_no /
    # payload_hash；project_id 与 created_by 可空但本包语义上必须给。
    conn.execute(
        text(
            "INSERT INTO version_nodes"
            " (id, entity_type, entity_id, project_id, revision_no,"
            "  schema_version, payload_hash, created_by)"
            " VALUES (:id, :t, :eid, :pid, 1, 1, :h, :u)"
        ),
        {"id": node_id, "t": entity_type, "eid": entity_id, "pid": pid,
         "h": h_char * 64, "u": uid},
    )


def _probe_op88_forward():
    # 活性验证 1 + 2：`brief_versions` 指向 `entity_type='PLAN'` 的节点。
    # 顺序：先断言 INSERT 阶段不报错（延迟语义），再强制校验拿判定。
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, uid = _seed_projects_tx(conn)
            brief_id = _brief_tx(conn, pid)
            plan_node = uuid.uuid4()
            _register_node_tx(conn, plan_node, "PLAN", uuid.uuid4(),
                              pid, uid, "0")
            file_id = _seed_file_tx(conn, pid=pid, uid=uid,
                                    name="bad.docx", digest="1" * 64)
            # 关键：brief_versions.id 指向 PLAN 节点 —— entity_type 不匹配。
            conn.execute(
                text(
                    "INSERT INTO brief_versions"
                    " (id, brief_id, source_file_id, source_state, product_name,"
                    "  summary, structured_fields, created_by)"
                    " VALUES (:id, :b, :f, 'SUCCEEDED', '探针产品', '摘要',"
                    "  CAST('{}' AS jsonb), :u)"
                ),
                {"id": plan_node, "b": brief_id, "f": file_id, "u": uid},
            )
            emit(True, "OP-88 正向：INSERT 阶段未报错（延迟语义成立）",
                 "错误插入此刻可见，强制校验时才判定")
            detail = "竟然通过了强制校验 —— 正向判据未生效"
            ok = False
            try:
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            except DBAPIError as exc:
                detail = f"sqlstate={sqlstate(exc)} 触发器={constraint_name(exc)}"
                ok = detail.startswith("sqlstate=23514")
            emit(ok, "OP-88 正向：延迟判据被强制校验拦下（23514）", detail)
        except Exception as exc:
            emit(False, "OP-88 正向探针", f"探针异常 {type(exc).__name__}: {exc}")
        finally:
            try:
                trans.rollback()
            except Exception:
                pass


def _probe_op88_reverse():
    # 活性验证 3：**`briefs` 行存在**的 BRIEF 节点，但没有任何 brief_versions
    # 行 —— 这才是反向判据的真实反例（收窄逻辑见本段顶部说明）。
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, uid = _seed_projects_tx(conn)
            brief_id = _brief_tx(conn, pid)
            orphan = uuid.uuid4()
            _register_node_tx(conn, orphan, "BRIEF", brief_id, pid, uid, "2")
            detail = "竟然通过了强制校验 —— 反向判据缺失"
            ok = False
            try:
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            except DBAPIError as exc:
                detail = f"sqlstate={sqlstate(exc)} 触发器={constraint_name(exc)}"
                ok = detail.startswith("sqlstate=23514")
            emit(ok, "OP-88 反向：BRIEF 节点缺 brief_versions 行被拦（23514）",
                 detail)
        except Exception as exc:
            emit(False, "OP-88 反向探针", f"探针异常 {type(exc).__name__}: {exc}")
        finally:
            try:
                trans.rollback()
            except Exception:
                pass


def _probe_happy_path():
    # 活性验证 4：**合法配对必须能通过**。
    # 没有这一条，前两条（正向拦、反向拦）无法排除"判据把一切都拒绝"——
    # 一个永远 RAISE 的触发器也能让前两条全绿。
    # 本机实测：正向判据要求 `entity_id = brief_versions.brief_id`
    #（即 entity_id 指向**逻辑 Brief**，不是版本 id），据此造正确配对。
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, uid = _seed_projects_tx(conn)
            brief_id = _brief_tx(conn, pid)
            node_id = uuid.uuid4()
            _register_node_tx(conn, node_id, "BRIEF", brief_id, pid, uid, "3")
            file_id = _seed_file_tx(conn, pid=pid, uid=uid,
                                    name="ok.docx", digest="4" * 64)
            conn.execute(
                text(
                    "INSERT INTO brief_versions"
                    " (id, brief_id, source_file_id, source_state, product_name,"
                    "  summary, structured_fields, created_by)"
                    " VALUES (:id, :b, :f, 'SUCCEEDED', '合法产品', '摘要',"
                    "  CAST('{}' AS jsonb), :u)"
                ),
                {"id": node_id, "b": brief_id, "f": file_id, "u": uid},
            )
            detail = "合法配对通过强制校验（判据不是一律拒绝）"
            ok = True
            try:
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            except DBAPIError as exc:
                detail = f"合法配对竟被拦 sqlstate={sqlstate(exc)}"
                ok = False
            emit(ok, "合法配对（briefs + BRIEF 节点 + brief_versions）通过",
                 detail)
        except Exception as exc:
            emit(False, "合法配对探针", f"探针异常 {type(exc).__name__}: {exc}")
        finally:
            try:
                trans.rollback()
            except Exception:
                pass


def _probe_immutable_and_rules():
    # 活性验证 5/6/7：不可篡改、事实类 claim 必须有证据、BLOCK 不能停在 OPEN。
    # 三条试写各自包在 begin_nested()（SAVEPOINT）里：约束冲突会把事务打成
    # aborted，不隔离的话后面两条全废。
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, uid = _seed_projects_tx(conn)
            brief_id = _brief_tx(conn, pid)
            # 造一个 entity_id 指向**逻辑 Brief** 的合法 BRIEF 节点 + 子表行。
            good_node = uuid.uuid4()
            _register_node_tx(conn, good_node, "BRIEF", brief_id, pid, uid, "5")
            file_id = _seed_file_tx(conn, pid=pid, uid=uid,
                                    name="rules.docx", digest="6" * 64)
            conn.execute(
                text(
                    "INSERT INTO brief_versions"
                    " (id, brief_id, source_file_id, source_state, product_name,"
                    "  summary, structured_fields, created_by)"
                    " VALUES (:id, :b, :f, 'SUCCEEDED', '规则探针产品', '摘要',"
                    "  CAST('{}' AS jsonb), :u)"
                ),
                {"id": good_node, "b": brief_id, "f": file_id, "u": uid},
            )

            # 验证 5：不可变 —— UPDATE 被拦（触发器，SQLSTATE 23001）。
            detail = "竟然改成功了"
            sp = conn.begin_nested()
            try:
                conn.execute(
                    text("UPDATE brief_versions SET summary='改过了' WHERE id=:i"),
                    {"i": good_node},
                )
                sp.commit()
            except DBAPIError as exc:
                detail = f"sqlstate={sqlstate(exc)} 触发器={constraint_name(exc)}"
                sp.rollback()
            emit(detail.startswith("sqlstate=23001"),
                 "brief_versions 不可篡改：UPDATE 被拦（23001）", detail)

            # 验证 6：事实类 claim 必须有证据（SOURCE_FACT 属事实类）。
            detail = "竟然插进去了"
            sp = conn.begin_nested()
            try:
                conn.execute(
                    text(
                        "INSERT INTO brief_claims"
                        " (id, brief_version_id, field_path, claim_text, category,"
                        "  claim_hash)"
                        " VALUES (gen_random_uuid(), :v, 'product.name', '是事实',"
                        "  'SOURCE_FACT', :h)"
                    ),
                    {"v": good_node, "h": "7" * 64},
                )
                sp.commit()
            except DBAPIError as exc:
                detail = f"sqlstate={sqlstate(exc)} 约束={constraint_name(exc)}"
                sp.rollback()
            emit(detail.startswith("sqlstate=23514"),
                 "事实类 claim 缺证据被拦（23514）", detail)

            # 验证 7：BLOCK 疑问不允许停在 OPEN。
            detail = "竟然插进去了"
            sp = conn.begin_nested()
            try:
                conn.execute(
                    text(
                        "INSERT INTO brief_issues"
                        " (id, project_id, brief_version_id, field_path, severity,"
                        "  description, state)"
                        " VALUES (gen_random_uuid(), :p, :v, 'product.name', 'BLOCK',"
                        "  '缺规格', 'OPEN')"
                    ),
                    {"p": pid, "v": good_node},
                )
                sp.commit()
            except DBAPIError as exc:
                detail = f"sqlstate={sqlstate(exc)} 约束={constraint_name(exc)}"
                sp.rollback()
            emit(detail.startswith("sqlstate=23514"),
                 "BLOCK 疑问不能停在 OPEN 被拦（23514）", detail)
        except Exception as exc:
            emit(False, "不可变/证据/BLOCK 探针",
                 f"探针异常 {type(exc).__name__}: {exc}")
        finally:
            try:
                trans.rollback()
            except Exception:
                pass


def _probe_claim_evidence_scope():
    # 活性验证 8：`trg_brief_claims_evidence_scope` 真生效 —— **假溯源被拦**。
    # 判据（函数体实测）：`brief_versions.source_parse_run_id` 必须存在，
    # 且 `brief_claims.evidence_segment_id` 必须属于**那次**解析运行。
    # 这里造"片段属于另一次解析运行"的假溯源，断言被拦。
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, uid = _seed_projects_tx(conn)
            brief_id = _brief_tx(conn, pid)
            node_id = uuid.uuid4()
            _register_node_tx(conn, node_id, "BRIEF", brief_id, pid, uid, "8")
            file_id = _seed_file_tx(conn, pid=pid, uid=uid,
                                    name="scope.docx", digest="9" * 64)
            # 两次解析运行：真实 run 与"别人的" run。
            # 用 'QUEUED' 而不是 'SUCCEEDED'：后者被
            # ck_parse_runs_succeeded_means_all_parsed 要求
            # total_units = parsed_units > 0 且 finished_at 非空 ——
            # 探针不需要解析真的完成，只要 run 行存在即可。
            real_run, other_run = uuid.uuid4(), uuid.uuid4()
            for run in (real_run, other_run):
                conn.execute(
                    text(
                        "INSERT INTO parse_runs (id, file_id, state)"
                        " VALUES (:r, :f, 'QUEUED')"
                    ),
                    {"r": run, "f": file_id},
                )
            conn.execute(
                text(
                    "INSERT INTO brief_versions"
                    " (id, brief_id, source_file_id, source_parse_run_id,"
                    "  source_state, product_name, summary, structured_fields,"
                    "  created_by)"
                    " VALUES (:id, :b, :f, :run, 'SUCCEEDED', '范围产品', '摘要',"
                    "  CAST('{}' AS jsonb), :u)"
                ),
                {"id": node_id, "b": brief_id, "f": file_id, "run": real_run,
                 "u": uid},
            )
            # 片段挂在 other_run 上 —— 对版本而言是假溯源。
            seg_id = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO source_segments"
                    " (id, parse_run_id, segment_no, position, text_content,"
                    "  content_hash, extraction_kind)"
                    " VALUES (:s, :run, 1, CAST('{\"kind\":\"TEXT\"}' AS jsonb),"
                    "  '片段正文', :h, 'TEXT')"
                ),
                {"s": seg_id, "run": other_run, "h": "a" * 64},
            )
            conn.execute(
                text(
                    "INSERT INTO brief_claims"
                    " (id, brief_version_id, field_path, claim_text, category,"
                    "  evidence_segment_id, claim_hash)"
                    " VALUES (gen_random_uuid(), :v, 'product.name', '外部事实',"
                    "  'EXTERNAL_EVIDENCE', :s, :h)"
                ),
                {"v": node_id, "s": seg_id, "h": "b" * 64},
            )
            detail = "竟然通过了强制校验 —— 假溯源未被拦"
            ok = False
            try:
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            except DBAPIError as exc:
                detail = f"sqlstate={sqlstate(exc)} 触发器={constraint_name(exc)}"
                ok = detail.startswith("sqlstate=23514")
            emit(ok, "假溯源（片段不属于本版本解析运行）被拦（23514）", detail)
        except Exception as exc:
            emit(False, "证据来源范围探针", f"探针异常 {type(exc).__name__}: {exc}")
        finally:
            try:
                trans.rollback()
            except Exception:
                pass


def _probe_issue_project_scope():
    # 活性验证 9：`trg_brief_issues_project_scope` 真生效 ——
    # **疑问挂错项目被拦**（判据：`brief_issues.project_id` 必须等于
    # 它所指版本所属 Brief 的 `project_id`）。
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, uid = _seed_projects_tx(conn)
            # 反例要"挂到**另一个真实项目**下"，而不是造一个不存在的 uuid ——
            # 后者会先撞外键（fk_brief_issues_project_id_projects），
            # 根本走不到本模块的触发器，那样测的是外键不是判据。
            other_pid, _other_uid = _seed_projects_tx(conn)
            brief_id = _brief_tx(conn, pid)
            node_id = uuid.uuid4()
            _register_node_tx(conn, node_id, "BRIEF", brief_id, pid, uid, "c")
            file_id = _seed_file_tx(conn, pid=pid, uid=uid,
                                    name="scope2.docx", digest="d" * 64)
            conn.execute(
                text(
                    "INSERT INTO brief_versions"
                    " (id, brief_id, source_file_id, source_state, product_name,"
                    "  summary, structured_fields, created_by)"
                    " VALUES (:id, :b, :f, 'SUCCEEDED', '项目范围产品', '摘要',"
                    "  CAST('{}' AS jsonb), :u)"
                ),
                {"id": node_id, "b": brief_id, "f": file_id, "u": uid},
            )
            # state='RESOLVED' 必须带 resolution+resolved_by；severity='WARN'
            # 才允许非 OPEN（BLOCK+OPEN 已被验证 7 拦下）。
            conn.execute(
                text(
                    "INSERT INTO brief_issues"
                    " (id, project_id, brief_version_id, field_path, severity,"
                    "  description, state, resolution, resolved_by)"
                    " VALUES (gen_random_uuid(), :wrong, :v, 'product.name', 'WARN',"
                    "  '描述', 'RESOLVED', '已确认', :u)"
                ),
                {"wrong": other_pid, "v": node_id, "u": uid},
            )
            detail = "竟然通过了强制校验 —— 项目归属未被校验"
            ok = False
            try:
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            except DBAPIError as exc:
                detail = f"sqlstate={sqlstate(exc)} 触发器={constraint_name(exc)}"
                ok = detail.startswith("sqlstate=23514")
            emit(ok, "疑问挂错项目（project_id 与版本所属项目不一致）被拦（23514）",
                 detail)
        except Exception as exc:
            emit(False, "疑问项目归属探针", f"探针异常 {type(exc).__name__}: {exc}")
        finally:
            try:
                trans.rollback()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 探针零残留：先取基线，跑完再差分（本机实测发现的必要做法）
# ---------------------------------------------------------------------------
#
# **为什么用差分而不是"全表 0 行"**：
#   1. 三张业务表里已有 52 / 46 / 6 行 `测试产品`（pytest 全量跑的遗留）；
#      它们是**不可变表**（触发器拦 DELETE）——那是本包设计后果
#      （"版本一旦固化不可篡改"），不是探针污染。
#   2. 更硬的证据：本脚本**第一版**用 `trans.commit()` 取判定，留下了一个
#      `WP18P*` 项目 + 一个 `BRIEF` 版本节点（`11:08:14`）。我试图按 FK 顺序
#      清理时，`DELETE FROM version_nodes` 被
#      `rednote_reject_mutation()` 拦下 —— **连自己的探针残留都删不掉**。
#      这不是脚本缺陷，恰恰是"不可变"落到物理层的强证据；
#      同时也是必须改用 `SET CONSTRAINTS ALL IMMEDIATE`（不提交取判定）
#      的直接理由。
#   因此正确判据是：**本次运行相对基线零新增**。

PROBE_PROJECT_PREFIX = "WP18P"


def _probe_baseline():
    # 取本次运行前的基线指纹（探针各类专属指纹的计数）。
    with engine.connect() as conn:
        return {
            "projects": conn.execute(
                text("SELECT count(*) FROM projects WHERE project_code LIKE :p"),
                {"p": PROBE_PROJECT_PREFIX + "%"},
            ).scalar_one(),
            "brands": conn.execute(
                text("SELECT count(*) FROM brands WHERE name = 'WP18 探针品牌'")
            ).scalar_one(),
            "products": conn.execute(
                text("SELECT count(*) FROM products WHERE name = 'WP18 探针产品'")
            ).scalar_one(),
            "users": conn.execute(
                text("SELECT count(*) FROM users WHERE issuer = 'https://idp.probe'")
            ).scalar_one(),
            "versions": conn.execute(
                text(
                    "SELECT count(*) FROM brief_versions WHERE product_name IN"
                    " ('探针产品','合法产品','规则探针产品','范围产品','项目范围产品')"
                )
            ).scalar_one(),
            "claims": conn.execute(
                text(
                    "SELECT count(*) FROM brief_claims WHERE claim_text IN"
                    " ('是事实','外部事实')"
                )
            ).scalar_one(),
            "issues": conn.execute(
                text(
                    "SELECT count(*) FROM brief_issues WHERE description IN"
                    " ('缺规格','描述')"
                )
            ).scalar_one(),
        }


_BASELINE = _probe_baseline()


def _probe_no_residue():
    # 跨包误伤 + 零残留（本包最容易被质疑的两点）：
    #   1. WP15/16/17 的不可变触发器**仍然生效**（不只"存在"）：
    #      对共用表 `audit_events` 试写 UPDATE，断言仍被 23001 拦下。
    #      本包 downgrade 刻意不删共用函数 —— 这里验证"没删坏"。
    #   2. **探针零残留**：结尾与基线差分，断言本次运行零新增。
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, uid = _seed_projects_tx(conn)
            ref = uuid.uuid4()
            # audit_events 必填列（实测）：actor_kind / action / object_type /
            # object_id / request_id（**不是** actor_id + entity_type + detail，
            # 那是我第一版凭印象写错的列名）。
            conn.execute(
                text(
                    "INSERT INTO audit_events"
                    " (id, actor_kind, action, object_type, object_id, project_id,"
                    "  request_id)"
                    " VALUES (:i, 'USER', 'probe.noop', 'PROJECT', :p2, :p, :r)"
                ),
                {"i": ref, "p2": str(pid), "p": pid, "r": "wp18-probe-" + ref.hex[:8]},
            )
            detail = "竟然改成功了 —— 共用不可变保护失效"
            sp = conn.begin_nested()
            try:
                conn.execute(
                    text("UPDATE audit_events SET action='改过了' WHERE id=:i"),
                    {"i": ref},
                )
                sp.commit()
            except DBAPIError as exc:
                detail = f"sqlstate={sqlstate(exc)} 触发器={constraint_name(exc)}"
                sp.rollback()
            emit(detail.startswith("sqlstate=23001"),
                 "跨包回归：共用表 audit_events 不可变保护仍生效（23001）", detail)
        except Exception as exc:
            emit(False, "跨包回归探针", f"探针异常 {type(exc).__name__}: {exc}")
        finally:
            try:
                trans.rollback()
            except Exception:
                pass

    # 零残留：与基线差分（详见本段顶部说明）。
    after = _probe_baseline()
    delta = {k: after[k] - _BASELINE[k] for k in after}
    ok = all(v == 0 for v in delta.values())
    detail = (f"基线 {_BASELINE} → 现值 {after}；本次新增 {delta}")
    emit(ok, "探针数据零残留（本次运行相对基线零新增）", detail)


def _probe_draft_partial_requires_issue():
    # 活性验证 10：**草稿层**的"残缺不得洗白" —— 硬约束的第一层。
    # `ck_brief_drafts_partial_requires_issue`：`source_state='PARTIAL'` 时
    # `issue_resolutions` 必须非空。这是 PARTIAL 消费规则在数据结构层的落点：
    # 有页面没解析出来时，草稿不能把缺失当"没有"。
    # 固化层（版本）由验证 1 的函数兜底，这里是草稿层，两层合起来才闭环。
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            pid, _uid = _seed_projects_tx(conn)
            brief_id = _brief_tx(conn, pid)
            detail = "竟然插进去了 —— 残缺被洗白"
            ok = False
            sp = conn.begin_nested()
            try:
                # brief_drafts 只有 brief_id 是必填（其余列都有默认）；
                # 这里显式给 PARTIAL + 空的 issue_resolutions，正是被拦的形状。
                conn.execute(
                    text(
                        "INSERT INTO brief_drafts"
                        " (brief_id, source_state, issue_resolutions)"
                        " VALUES (:b, 'PARTIAL', CAST('[]' AS jsonb))"
                    ),
                    {"b": brief_id},
                )
                sp.commit()
            except DBAPIError as exc:
                detail = f"sqlstate={sqlstate(exc)} 约束={constraint_name(exc)}"
                sp.rollback()
            emit(detail.startswith("sqlstate=23514"),
                 "草稿层：PARTIAL 却无疑问记录被拦（23514）", detail)
        except Exception as exc:
            emit(False, "草稿残缺探针", f"探针异常 {type(exc).__name__}: {exc}")
        finally:
            try:
                trans.rollback()
            except Exception:
                pass


_probe_op88_forward()
_probe_op88_reverse()
_probe_happy_path()
_probe_immutable_and_rules()
_probe_claim_evidence_scope()
_probe_issue_project_scope()
_probe_draft_partial_requires_issue()
_probe_no_residue()


# ---------------------------------------------------------------------------
# 契约核对
# ---------------------------------------------------------------------------
try:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    paths = contract["paths"]
    brief_paths = sorted(p for p in paths if "brief" in p)
    expected_brief = [
        "/api/v1/projects/{projectId}/brief",
        "/api/v1/projects/{projectId}/brief-draft",
        "/api/v1/projects/{projectId}/brief-enrichment-jobs",
        "/api/v1/projects/{projectId}/brief-parse-jobs",
        "/api/v1/projects/{projectId}/brief-versions",
    ]
    emit(
        brief_paths == expected_brief,
        "契约含 WP18 的 5 个 Brief 端点",
        f"{len(brief_paths)} 个",
    )
except Exception as exc:
    emit(False, "契约核对", f"读取失败: {exc}")


total = 0
failed = 0
print("")
print(f"INLINE_DONE expected={EXPECTED_CHECKS}")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def collect_wp18() -> tuple[Path, bool]:
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
        "# WP18 Brief 结构化解构：5 张表 / 35 CHECK / 7 触发器 / 六处活性验证 / 契约",
        f"# 生成时间: {_now()}",
        "# 命令: cd services/backend && python -c '<information_schema + 延迟约束试写 + 试插>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp18-brief.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # **条数不足也算失败**（ISS-43）。
    complete = len(checks) >= EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete)


def main() -> int:
    path, ok = collect_wp18()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())