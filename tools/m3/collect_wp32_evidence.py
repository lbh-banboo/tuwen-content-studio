#!/usr/bin/env python
"""WP32 证据采集脚本（扩产 / 批产）。

产出的文件（落在 `evidence/m3/`）：

| 文件 | 内容 |
|---|---|
| `wp32-batches.log` | [M] 迁移 head 真 CHECK + 迁移文件机械判据（不 import 取值域 / ADR-13 grep / downgrade 范围）；[S] 3 表逐列 / 类型 / 可空 / 默认、20 条 CHECK 逐名对账、取值域集合、FK、唯一约束、索引、6 个触发器与 3 个自有函数、`jobs.batch_id` 外键（已 VALIDATE）与部分索引、ADR-13 库内定义；[P] 行为探针（独立事务 + rollback）：数量一致性 / 成员归属 / run 归属 / 不可变 / cancel 双向等值 / 金额币种同有同无 / 唯一约束 / ADR-13 无上界；[D] 一次性临时库真跑 downgrade 再 upgrade；[X] 跨包共存；[R] 路由对账（6 path / 7 operation）；[G] 进度折算纯函数；[V] 反向验证 9 条（错误实现 / 错误数据下判据必须报红）；[Z] 探针零残留 |

## 本包的结构性特点

- `batch_items` / `batch_item_runs` 不可变（复用跨包共用的 `rednote_reject_mutation`）；
  `batches` 可变，但只有命令位（`state` / `cancel_*` / `lock_version`）。
- 4 个一致性触发器全是 `CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED`。
- ADR-13：`batches.quantity` 的 CHECK 只能是 `quantity > 0`，上限（10/30/50）属于业务配置，
  不得落成 DDL 常量；本脚本同时查迁移源码与库内定义，并真插 51 / 1000 验证库不拦。
- `jobs.batch_id` 的外键用「先 NOT VALID 再 VALIDATE」建；判据看 `convalidated = true`。

## 目标库与迁移

默认库 `rednote_verify_wp32`（需先 `createdb` + `CREATE EXTENSION vector` +
`alembic upgrade head`），可用环境变量 `REDNOTE_WP32_EVIDENCE_DATABASE_URL` 覆盖。
迁移 head 判据是**真 CHECK**（不早于 `b2f32c0de002`），库里没有 `alembic_version` 表即 FAIL，不 SKIP。

⚠️ **本脚本不建目标库里的任何表**。结构不全时报 FAIL 并提示先跑迁移 ——
自己建表再核对自己建的表是**循环判据**（恒绿）。
唯一的建库动作是 [D] 节的一次性临时库（`<目标库>_tmp_<pid>`），用完 DROP，
用来真跑 downgrade / 再 upgrade；它与目标库互不影响。

## 探针自身踩过的坑

1. **★ 期望条数必须实测，不能凭印象**（与 WP24~WP31 同型教训）：
   `complete = len(checks) >= EXPECTED_CHECKS` 在期望值偏低时恒真，
   本脚本另加 `exact = len(checks) == EXPECTED_CHECKS` 并入 verdict。
2. **一致性触发器是延迟的**：INSERT 那一刻不检查，必须 `SET CONSTRAINTS ... IMMEDIATE`
   才能看到它拒绝，否则“坏值写进去了”会被误读成“防线失效”。
3. **不能写 `SET CONSTRAINTS ALL IMMEDIATE`**：探针要造一条合法的 `production_baselines`
   （`batches.baseline_id` 的 FK 目标），而它自己挂着一个延迟的“同事务批准”触发器
   （`rednote_check_baseline_approval`），`ALL` 会把它也拉进来，于是所有探针都被
   基线的触发器误杀（报的是“基线没有批准”，与本包无关）。因此只点名本包 4 个触发器。
4. **点名 IMMEDIATE 后，之后每条 INSERT 都会立刻触发检查**：必须先把批次、成员、
   job、run 全部写完，**最后**才 `SET CONSTRAINTS`，否则“先建批次后建成员”会被自己误杀。
5. **「别的批次」必须是真实存在的批次**：run 归属探针里 `jobs.batch_id` 指向另一个批次，
   若随机 uuid 则撞的是 `fk_jobs_batch_id_batches`（23503）而不是本包的触发器（23514），
   判据会因为拒绝了错误的原因而假绿。所以第二个批次是真插的（含自己的成员）。
6. **CHECK 类探针要核对 `diag.constraint_name`**：只比 sqlstate=23514 区分不出
   “被我要测的那条拒绝”与“被别的 CHECK 顺手拒绝”。
7. **不可变表探针绝不能 commit**：一旦提交就永远删不掉。每组探针独立连接 +
   无条件 `rollback()`；单条探针再包一层 savepoint。
8. **反向验证改的是库对象（事务内 DDL，PG 支持事务性 DDL），不是代码树**：
   做完 `rollback()`，库回到原样；紧随其后的 [Z] 与再次的结构判据证明确实回到原样。
9. **内联脚本只允许单行注释且不能出现三引号**（ISS-45），由
   `check_evidence_scripts.py` 机械核对。
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

#: 期望条数（与内联脚本里的 EXPECTED_CHECKS 保持同值，**实测回填**）。
EXPECTED_CHECKS: int = 150

DEFAULT_DB = "postgresql+psycopg://rednote@localhost:5432/rednote_verify_wp32"
DB_ENV = "REDNOTE_WP32_EVIDENCE_DATABASE_URL"

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

INSPECT_SCRIPT = r"""
# 内联脚本：只允许单行注释，且不能出现三引号（三引号会提前终止外层包裹，见 ISS-45）。

import ast
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from rednote.modules.batches import ddl as batches_ddl
from rednote.modules.batches import models as batches_models
from rednote.modules.batches import progress as batches_progress
from rednote.modules.batches import types as batches_types

EXPECTED_CHECKS = 150

DB = os.environ.get(
    "REDNOTE_WP32_EVIDENCE_DATABASE_URL",
    "postgresql+psycopg://rednote@localhost:5432/rednote_verify_wp32",
)
engine = create_engine(DB)

WP32_MIGRATION_REVISION = "b2f32c0de002"
WP32_DOWN_REVISION = "a1f31c0de001"

OWN_TABLES = batches_models.OWN_TABLES
IMMUTABLE_SQLSTATE = "23001"
CONSISTENCY_SQLSTATE = "23514"
UNIQUE_SQLSTATE = "23505"
MAX_IDENTIFIER_BYTES = 63

PROBE_ISSUER = "wp32-probe"
PROBE_PROJECT_NAME = "WP32 探针项目"
PROBE_PROJECT_PREFIX = "WP32PROBE"

# 以下全是写死的化石字面量（刻意不从应用代码取），用来对账迁移与库。
EXPECTED_COLUMNS = {
    "batches": (
        ("id", "uuid", False, "gen_random_uuid()"),
        ("project_id", "uuid", False, None),
        ("batch_kind", "character varying(8)", False, None),
        ("seq_no", "integer", False, None),
        ("quantity", "integer", False, None),
        ("state", "character varying(20)", False, "'SUBMITTED'::character varying"),
        ("plan_version_id", "uuid", False, None),
        ("baseline_id", "uuid", False, None),
        ("allocation", "jsonb", False, None),
        ("limit_applied", "integer", False, None),
        ("limit_source", "character varying(16)", False, None),
        ("estimated_amount", "numeric(20,8)", True, None),
        ("estimated_currency", "character varying(3)", True, None),
        ("estimate_refs", "jsonb", False, "'[]'::jsonb"),
        ("idempotency_key", "character varying(200)", False, None),
        ("request_hash", "character varying(64)", False, None),
        ("cancel_requested_at", "timestamp with time zone", True, None),
        ("cancel_requested_by", "uuid", True, None),
        ("created_by", "uuid", True, None),
        ("created_at", "timestamp with time zone", False, "now()"),
        ("updated_at", "timestamp with time zone", False, "now()"),
        ("lock_version", "bigint", False, "1"),
    ),
    "batch_items": (
        ("id", "uuid", False, "gen_random_uuid()"),
        ("batch_id", "uuid", False, None),
        ("ordinal", "integer", False, None),
        ("content_item_id", "uuid", False, None),
        ("scene_key", "character varying(100)", True, None),
        ("form_key", "character varying(100)", True, None),
        ("created_at", "timestamp with time zone", False, "now()"),
    ),
    "batch_item_runs": (
        ("id", "uuid", False, "gen_random_uuid()"),
        ("batch_item_id", "uuid", False, None),
        ("round_no", "integer", False, None),
        ("job_id", "uuid", False, None),
        ("created_at", "timestamp with time zone", False, "now()"),
    ),
}

EXPECTED_CHECK_NAMES = {
    "batches": {
        "ck_batches_cancel_pair_consistent", "ck_batches_batch_kind_valid",
        "ck_batches_allocation_is_object", "ck_batches_estimate_refs_is_array",
        "ck_batches_limit_source_valid", "ck_batches_state_valid",
        "ck_batches_estimate_pair_consistent", "ck_batches_cancel_actor_requires_time",
        "ck_batches_idempotency_key_not_blank", "ck_batches_request_hash_not_blank",
        "ck_batches_estimated_amount_non_negative", "ck_batches_limit_applied_positive",
        "ck_batches_lock_version_positive", "ck_batches_quantity_positive",
        "ck_batches_seq_no_positive",
    },
    "batch_items": {
        "ck_batch_items_assignment_paired", "ck_batch_items_form_key_not_blank",
        "ck_batch_items_ordinal_positive", "ck_batch_items_scene_key_not_blank",
    },
    "batch_item_runs": {"ck_batch_item_runs_round_no_positive"},
}

EXPECTED_DOMAINS = (
    ("batches", "ck_batches_batch_kind_valid", {"BULK", "SCALE", "TRIAL"}),
    ("batches", "ck_batches_state_valid", {"SUBMITTED", "CANCEL_REQUESTED"}),
    ("batches", "ck_batches_limit_source_valid", {"GLOBAL", "INHERITED", "MANUAL"}),
)

EXPECTED_FKS = (
    ("batches", "fk_batches_baseline_id_production_baselines", "production_baselines"),
    ("batches", "fk_batches_cancel_requested_by_users", "users"),
    ("batches", "fk_batches_created_by_users", "users"),
    ("batches", "fk_batches_plan_version_id_version_nodes", "version_nodes"),
    ("batches", "fk_batches_project_id_projects", "projects"),
    ("batch_items", "fk_batch_items_batch_id_batches", "batches"),
    ("batch_items", "fk_batch_items_content_item_id_content_items", "content_items"),
    ("batch_item_runs", "fk_batch_item_runs_batch_item_id_batch_items", "batch_items"),
    ("batch_item_runs", "fk_batch_item_runs_job_id_jobs", "jobs"),
)

EXPECTED_UNIQUES = (
    ("batches", "uq_batches_project_kind_seq", ["project_id", "batch_kind", "seq_no"]),
    ("batches", "uq_batches_project_idempotency_key", ["project_id", "idempotency_key"]),
    ("batch_items", "uq_batch_items_batch_ordinal", ["batch_id", "ordinal"]),
    ("batch_items", "uq_batch_items_content_item_id", ["content_item_id"]),
    ("batch_item_runs", "uq_batch_item_runs_item_round", ["batch_item_id", "round_no"]),
    ("batch_item_runs", "uq_batch_item_runs_job_id", ["job_id"]),
)

EXPECTED_INDEXES = (
    ("batches", "ix_batches_baseline_id", ["baseline_id"]),
    ("batches", "ix_batches_plan_version_id", ["plan_version_id"]),
    ("batches", "ix_batches_project_created", ["project_id", "created_at"]),
)

EXPECTED_TRIGGERS = (
    ("trg_batch_items_immutable", "batch_items", "rednote_reject_mutation", False),
    ("trg_batch_item_runs_immutable", "batch_item_runs", "rednote_reject_mutation", False),
    ("trg_batches_item_count_consistency", "batches", "rednote_check_batch_item_count", True),
    ("trg_batch_items_count_consistency", "batch_items", "rednote_check_batch_item_count", True),
    ("trg_batch_items_ownership_consistency", "batch_items", "rednote_check_batch_item_ownership", True),
    ("trg_batch_item_runs_ownership_consistency", "batch_item_runs", "rednote_check_batch_item_run_ownership", True),
)
OWN_FUNCTIONS = (
    "rednote_check_batch_item_count",
    "rednote_check_batch_item_ownership",
    "rednote_check_batch_item_run_ownership",
)
DEFERRED_TRIGGER_NAMES = [t[0] for t in EXPECTED_TRIGGERS if t[3]]

EXPECTED_ROUTES = {
    ("/api/v1/projects/{projectId}/batch-previews", "post",
     "post_api_v1_projects_projectId_batch-previews"),
    ("/api/v1/projects/{projectId}/batches", "post",
     "post_api_v1_projects_projectId_batches"),
    ("/api/v1/projects/{projectId}/batches", "get",
     "get_api_v1_projects_projectId_batches"),
    ("/api/v1/batches/{batchId}", "get", "get_api_v1_batches_batchId"),
    ("/api/v1/batches/{batchId}/items", "get", "get_api_v1_batches_batchId_items"),
    ("/api/v1/batches/{batchId}/cancellations", "post",
     "post_api_v1_batches_batchId_cancellations"),
    ("/api/v1/batches/{batchId}/retries", "post",
     "post_api_v1_batches_batchId_retries"),
}

REPO = Path.cwd().resolve()
while REPO != REPO.parent and not (REPO / "migrations" / "alembic.ini").exists():
    REPO = REPO.parent
MIGRATION_FILE = REPO / "migrations" / "versions" / "20261021_0251_b2f32c0de002_wp32_batches.py"


def emit(ok, label, detail):
    print(f"CHECK {'OK  ' if ok else 'FAIL'} {label}: {detail}")


def sqlstate(exc):
    return getattr(exc.orig, "sqlstate", None)


def revision_chain():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(REPO / "migrations" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    return [
        r.revision
        for r in ScriptDirectory.from_config(cfg).walk_revisions(base="base", head="heads")
    ]


def revision_is_at_least(current, introduced):
    # walk_revisions 从 head 往 base 走：下标越小越新。沿真实链序比较，不用等号。
    chain = revision_chain()
    if current not in chain or introduced not in chain:
        return False
    return chain.index(current) <= chain.index(introduced)


def db_names(conn, table, contype):
    return {
        row[0]
        for row in conn.execute(
            text(
                "SELECT conname FROM pg_constraint"
                " WHERE contype = :t AND conrelid = CAST(:table AS regclass)"
            ),
            {"t": contype, "table": table},
        )
    }


def model_names(table, kind):
    return {
        c.name
        for c in batches_models.Base.metadata.tables[table].constraints
        if c.__class__.__name__ == kind
    }


def constraint_def(conn, name):
    return conn.execute(
        text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
        {"n": name},
    ).scalar_one_or_none()


# --- 探针夹具 -----------------------------------------------------------------
def mk_world(conn):
    w = {"n": 0}
    uid = uuid.uuid4()
    brand = uuid.uuid4()
    prod = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, display_name, role, status)"
            " VALUES (:u, :iss, :sub, 'WP32 探针', 'ADMIN', 'ACTIVE')"
        ),
        {"u": str(uid), "iss": PROBE_ISSUER, "sub": f"probe-{uid}"},
    )
    conn.execute(
        text("INSERT INTO brands (id, name, status) VALUES (:b, :n, 'ACTIVE')"),
        {"b": str(brand), "n": f"WP32 品牌 {str(brand)[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO products (id, brand_id, name, model_code, status)"
            " VALUES (:p, :b, :n, :c, 'ACTIVE')"
        ),
        {"p": str(prod), "b": str(brand), "n": f"WP32 产品 {str(prod)[:8]}",
         "c": f"WP32-{str(prod)[:8]}"},
    )
    projects = []
    for _ in range(2):
        proj = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO projects (id, project_code, name, brand_id, product_id,"
                " owner_id, target_delivery_count, due_date)"
                " VALUES (:p, :c, :n, :b, :pr, :o, 1, (now() + interval '30 days')::date)"
            ),
            {"p": str(proj), "c": f"{PROBE_PROJECT_PREFIX}{str(proj)[:8]}",
             "n": PROBE_PROJECT_NAME, "b": str(brand), "pr": str(prod), "o": str(uid)},
        )
        projects.append(str(proj))
    w["user"] = str(uid)
    w["project"], w["other_project"] = projects
    nodes = {}
    for kind in ("PLAN", "BASELINE"):
        node = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
                " payload_hash, schema_version)"
                " VALUES (:n, :k, :e, 1, :h, 1)"
            ),
            {"n": str(node), "k": kind, "e": w["project"], "h": f"h-{str(node)[:12]}"},
        )
        nodes[kind] = str(node)
    conn.execute(
        text(
            "INSERT INTO production_baselines (id, project_id, ruleset_version,"
            " snapshot, change_reason, created_by)"
            " VALUES (:i, :p, 'wp32-probe', cast('{}' as jsonb), 'wp32 探针', :u)"
        ),
        {"i": nodes["BASELINE"], "p": w["project"], "u": w["user"]},
    )
    w["plan"], w["baseline"] = nodes["PLAN"], nodes["BASELINE"]
    w["contents"] = [mk_content(conn, w["project"]) for _ in range(3)]
    w["other_content"] = mk_content(conn, w["other_project"])
    return w


def mk_content(conn, project):
    cid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO content_items (id, project_id, state, current_version_no,"
            " lock_version) VALUES (:c, :p, 'DRAFT', 0, 0)"
        ),
        {"c": str(cid), "p": project},
    )
    return str(cid)


def mk_batch(conn, w, quantity, **over):
    w["n"] += 1
    row = {
        "id": str(uuid.uuid4()),
        "project_id": w["project"],
        "batch_kind": "BULK",
        "seq_no": w["n"],
        "quantity": quantity,
        "plan_version_id": w["plan"],
        "baseline_id": w["baseline"],
        "limit_applied": 30,
        "limit_source": "GLOBAL",
        "idempotency_key": f"k-{uuid.uuid4()}",
        "request_hash": "h",
        "created_by": w["user"],
    }
    row.update(over)
    cols = list(row)
    values = ", ".join(f":{c}" for c in cols)
    conn.execute(
        text(
            f"INSERT INTO batches ({', '.join(cols)}, allocation) "
            f"VALUES ({values}, cast('{{}}' as jsonb))"
        ),
        row,
    )
    return row["id"]


def mk_item(conn, batch_id, content_id, ordinal=1):
    item = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO batch_items (id, batch_id, ordinal, content_item_id)"
            " VALUES (:i, :b, :o, :c)"
        ),
        {"i": item, "b": batch_id, "o": ordinal, "c": content_id},
    )
    return item


def mk_job(conn, w, batch_id, content_id):
    job = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO jobs (id, project_id, batch_id, content_id, kind, queue,"
            " state, idempotency_scope, idempotency_key, input_hash)"
            " VALUES (:i, :p, :b, :c, 'wp32.probe', 'default', 'QUEUED',"
            " 'wp32-probe', :k, 'h')"
        ),
        {"i": job, "p": w["project"], "b": batch_id, "c": content_id,
         "k": f"jk-{uuid.uuid4()}"},
    )
    return job


def mk_run(conn, item_id, job_id, round_no=1):
    run = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO batch_item_runs (id, batch_item_id, round_no, job_id)"
            " VALUES (:i, :it, :r, :j)"
        ),
        {"i": run, "it": item_id, "r": round_no, "j": job_id},
    )
    return run


def imm(conn):
    # 只点名本包 4 个延迟触发器；ALL 会把基线的批准触发器也拉进来（见 docstring 踩坑 3）。
    # 只点名库里当前存在的（反向验证摘掉触发器后，点名不存在的会报 42704 而不是我们要测的拒绝）。
    live = [
        r[0]
        for r in conn.execute(
            text("SELECT tgname FROM pg_trigger WHERE tgname = ANY(:n) AND NOT tgisinternal"),
            {"n": DEFERRED_TRIGGER_NAMES},
        )
    ]
    if live:
        conn.execute(text("SET CONSTRAINTS " + ", ".join(live) + " IMMEDIATE"))


def full_chain(conn, w, job_batch="same", job_content="same"):
    b = mk_batch(conn, w, 1)
    item = mk_item(conn, b, w["contents"][0])
    if job_batch == "same":
        jb = b
    elif job_batch == "other":
        b2 = mk_batch(conn, w, 1)
        mk_item(conn, b2, w["contents"][1])
        jb = b2
    else:
        jb = None
    jc = w["contents"][0] if job_content == "same" else w["contents"][2]
    job = mk_job(conn, w, jb, jc)
    run = mk_run(conn, item, job)
    return b, item, job, run


# --- 探针构造器：每个返回 None，抛 DBAPIError 即“被拒” ---------------------------
def b_count_low(conn):
    w = mk_world(conn)
    b = mk_batch(conn, w, 2)
    mk_item(conn, b, w["contents"][0])
    imm(conn)


def b_count_high(conn):
    w = mk_world(conn)
    b = mk_batch(conn, w, 1)
    mk_item(conn, b, w["contents"][0], 1)
    mk_item(conn, b, w["contents"][1], 2)
    imm(conn)


def b_count_ok(conn):
    w = mk_world(conn)
    b = mk_batch(conn, w, 2)
    mk_item(conn, b, w["contents"][0], 1)
    mk_item(conn, b, w["contents"][1], 2)
    imm(conn)


def b_item_foreign(conn):
    w = mk_world(conn)
    b = mk_batch(conn, w, 1)
    mk_item(conn, b, w["other_content"])
    imm(conn)


def b_run_ok(conn):
    w = mk_world(conn)
    full_chain(conn, w)
    imm(conn)


def b_run_job_batch_null(conn):
    w = mk_world(conn)
    full_chain(conn, w, job_batch="null")
    imm(conn)


def b_run_job_batch_other(conn):
    w = mk_world(conn)
    full_chain(conn, w, job_batch="other")
    imm(conn)


def b_run_job_content_mismatch(conn):
    w = mk_world(conn)
    full_chain(conn, w, job_content="other")
    imm(conn)


def mutate_after_chain(sql_text):
    def build(conn):
        w = mk_world(conn)
        b, item, job, run = full_chain(conn, w)
        imm(conn)
        conn.execute(text(sql_text), {"b": b, "i": item, "r": run})
    return build


def single_batch(quantity, **over):
    def build(conn):
        w = mk_world(conn)
        kw = dict(over)
        if kw.pop("with_actor", False):
            kw["cancel_requested_by"] = w["user"]
        mk_batch(conn, w, quantity, **kw)
    return build


def b_idem_dup(conn):
    w = mk_world(conn)
    mk_batch(conn, w, 1, idempotency_key="same-key")
    mk_batch(conn, w, 1, idempotency_key="same-key")


def b_idem_other_project(conn):
    w = mk_world(conn)
    mk_batch(conn, w, 1, idempotency_key="same-key")
    mk_batch(conn, w, 1, idempotency_key="same-key", project_id=w["other_project"])


def b_seq_dup(conn):
    w = mk_world(conn)
    mk_batch(conn, w, 1, seq_no=1)
    mk_batch(conn, w, 1, seq_no=1)


def b_content_twice(conn):
    w = mk_world(conn)
    b1 = mk_batch(conn, w, 1)
    mk_item(conn, b1, w["contents"][0])
    b2 = mk_batch(conn, w, 1)
    mk_item(conn, b2, w["contents"][0])


def b_ordinal_dup(conn):
    w = mk_world(conn)
    b1 = mk_batch(conn, w, 2)
    mk_item(conn, b1, w["contents"][0], 1)
    mk_item(conn, b1, w["contents"][1], 1)


PAST = "now()"
# (key, label, build, 期望 sqlstate（None=必须放行）, 期望约束名（None=不核）)
PROBES = {
    "count_low": ("[P] 批次 quantity=2 而成员 1 条被拒（延迟触发器）", b_count_low, CONSISTENCY_SQLSTATE, "msg:必须一致"),
    "count_high": ("[P] 批次 quantity=1 而成员 2 条被拒", b_count_high, CONSISTENCY_SQLSTATE, "msg:必须一致"),
    "count_ok": ("[P] 批次 quantity=2 成员 2 条放行（阴性对照）", b_count_ok, None, None),
    "item_foreign": ("[P] 成员内容属于别的项目被拒（归属一致）", b_item_foreign, CONSISTENCY_SQLSTATE, "msg:批次只能含本项目"),
    "run_ok": ("[P] run 的 job.batch_id / content_id 都一致放行（阴性对照）", b_run_ok, None, None),
    "run_null": ("[P] run 的 job.batch_id 为空被拒", b_run_job_batch_null, CONSISTENCY_SQLSTATE, "msg:batch_id=(空)"),
    "run_other": ("[P] run 的 job.batch_id 指向另一个真实批次被拒", b_run_job_batch_other, CONSISTENCY_SQLSTATE, "msg:而成员属于批次"),
    "run_content": ("[P] run 的 job.content_id 与成员不符被拒", b_run_job_content_mismatch, CONSISTENCY_SQLSTATE, "msg:而成员内容是"),
    "imm_item_upd": ("[P] batch_items 拒绝 UPDATE", mutate_after_chain("UPDATE batch_items SET ordinal = 9 WHERE id = :i"), IMMUTABLE_SQLSTATE, None),
    "imm_item_del": ("[P] batch_items 拒绝 DELETE", mutate_after_chain("DELETE FROM batch_items WHERE id = :i"), IMMUTABLE_SQLSTATE, None),
    "imm_run_upd": ("[P] batch_item_runs 拒绝 UPDATE", mutate_after_chain("UPDATE batch_item_runs SET round_no = 9 WHERE id = :r"), IMMUTABLE_SQLSTATE, None),
    "imm_run_del": ("[P] batch_item_runs 拒绝 DELETE", mutate_after_chain("DELETE FROM batch_item_runs WHERE id = :r"), IMMUTABLE_SQLSTATE, None),
    "batch_mutable": ("[P] batches 本身可变（命令位 UPDATE 放行；阴性对照）", mutate_after_chain("UPDATE batches SET lock_version = lock_version + 1 WHERE id = :b"), None, None),
    "cancel_no_time": ("[P] state=CANCEL_REQUESTED 却无 cancel_requested_at 被拒", single_batch(1, state="CANCEL_REQUESTED"), CONSISTENCY_SQLSTATE, "ck_batches_cancel_pair_consistent"),
    "cancel_time_only": ("[P] state=SUBMITTED 却有 cancel_requested_at 被拒", single_batch(1, cancel_requested_at="2026-10-21T00:00:00+00:00"), CONSISTENCY_SQLSTATE, "ck_batches_cancel_pair_consistent"),
    "cancel_actor": ("[P] 有 cancel_requested_by 却无时间被拒", single_batch(1, with_actor=True), CONSISTENCY_SQLSTATE, "ck_batches_cancel_actor_requires_time"),
    "cancel_ok": ("[P] CANCEL_REQUESTED + 时间 + 操作人放行（阴性对照）", single_batch(1, state="CANCEL_REQUESTED", cancel_requested_at="2026-10-21T00:00:00+00:00", with_actor=True), None, None),
    "est_amount_only": ("[P] 有金额无币种被拒", single_batch(1, estimated_amount=1.5), CONSISTENCY_SQLSTATE, "ck_batches_estimate_pair_consistent"),
    "est_currency_only": ("[P] 有币种无金额被拒", single_batch(1, estimated_currency="CNY"), CONSISTENCY_SQLSTATE, "ck_batches_estimate_pair_consistent"),
    "est_both": ("[P] 金额 + 币种同有放行（阴性对照）", single_batch(1, estimated_amount=1.5, estimated_currency="CNY"), None, None),
    "est_negative": ("[P] 负金额被拒", single_batch(1, estimated_amount=-1, estimated_currency="CNY"), CONSISTENCY_SQLSTATE, "ck_batches_estimated_amount_non_negative"),
    "qty_zero": ("[P] quantity=0 被拒", single_batch(0), CONSISTENCY_SQLSTATE, "ck_batches_quantity_positive"),
    "qty_neg": ("[P] quantity=-1 被拒", single_batch(-1), CONSISTENCY_SQLSTATE, "ck_batches_quantity_positive"),
    "qty_51": ("[P] ADR-13：quantity=51 库不拦（上限归业务配置）", single_batch(51), None, None),
    "qty_1000": ("[P] ADR-13：quantity=1000 库不拦", single_batch(1000), None, None),
    "idem_dup": ("[P] 同项目同 idempotency_key 被拒", b_idem_dup, UNIQUE_SQLSTATE, "uq_batches_project_idempotency_key"),
    "idem_other": ("[P] 不同项目同 idempotency_key 放行（阴性对照）", b_idem_other_project, None, None),
    "seq_dup": ("[P] 同项目同档位同 seq_no 被拒", b_seq_dup, UNIQUE_SQLSTATE, "uq_batches_project_kind_seq"),
    "content_twice": ("[P] 同一内容进两个批次被拒", b_content_twice, UNIQUE_SQLSTATE, "uq_batch_items_content_item_id"),
    "ordinal_dup": ("[P] 同批次内 ordinal 重复被拒", b_ordinal_dup, UNIQUE_SQLSTATE, "uq_batch_items_batch_ordinal"),
}


def attempt(conn, build):
    # 在 savepoint 里跑一次，无论成败都回滚 savepoint；返回 None=放行，否则 (sqlstate, 约束名, 消息)。
    sp = conn.begin_nested()
    try:
        build(conn)
        result = None
    except DBAPIError as exc:
        diag = getattr(exc.orig, "diag", None)
        result = (sqlstate(exc), getattr(diag, "constraint_name", None), str(exc.orig))
    sp.rollback()
    return result


def run_probe(conn, key):
    label, build, want_state, want_constraint = PROBES[key]
    got = attempt(conn, build)
    if want_state is None:
        return got is None, f"实际={'放行' if got is None else got}"
    ok = got is not None and got[0] == want_state
    if got is None:
        return False, f"实际=放行（期望被拒 {want_state}）"
    if ok and want_constraint is not None:
        if want_constraint.startswith("msg:"):
            ok = want_constraint[4:] in got[2]
        else:
            ok = got[1] == want_constraint
    return ok, f"实际={got[:2] + (got[2].splitlines()[0][:70],)} 期望=({want_state}, {want_constraint})"


# --- 判据（正向与反向共用同一份实现）------------------------------------------
def j_adr13_db(conn):
    defs = [
        row[0]
        for row in conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE contype = 'c' AND conrelid = CAST('batches' AS regclass)"
                " AND pg_get_constraintdef(oid) ILIKE '%quantity%'"
            )
        )
    ]
    ok = defs == ["CHECK ((quantity > 0))"]
    bad = [d for d in defs if re.search(r"quantity\s*(<=|<|>=|=|BETWEEN|IN)\s*\(?\s*\d+", d)]
    return ok and not bad, f"含 quantity 的 CHECK={defs}"


def j_check_names(conn):
    parts = []
    ok = True
    for table, expected in EXPECTED_CHECK_NAMES.items():
        actual = db_names(conn, table, "c")
        ok = ok and actual == expected
        parts.append(f"{table}: 差集{sorted(actual ^ expected)}")
    return ok, "; ".join(parts)


def j_jobs_fk(conn):
    row = conn.execute(
        text(
            "SELECT convalidated, confdeltype, confrelid::regclass::text"
            " FROM pg_constraint WHERE conname = 'fk_jobs_batch_id_batches'"
            " AND conrelid = CAST('jobs' AS regclass)"
        )
    ).first()
    if row is None:
        return False, "fk_jobs_batch_id_batches 不存在"
    return (bool(row[0]) and row[1] == "r" and row[2] == "batches"), (
        f"convalidated={row[0]} confdeltype={row[1]} 指向={row[2]}"
    )


def j_jobs_ix(conn):
    row = conn.execute(
        text(
            "SELECT pg_get_indexdef(i.indexrelid), i.indisunique, i.indisvalid"
            " FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
            " WHERE c.relname = 'ix_jobs_batch_id'"
        )
    ).first()
    if row is None:
        return False, "ix_jobs_batch_id 不存在"
    ok = "WHERE (batch_id IS NOT NULL)" in row[0] and not row[1] and row[2]
    return ok, f"{row[0]}"


def j_triggers(conn):
    rows = conn.execute(
        text(
            "SELECT t.tgname, c.relname, p.proname, t.tgdeferrable, t.tginitdeferred"
            " FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
            " JOIN pg_proc p ON p.oid = t.tgfoid"
            " WHERE c.relname = ANY(:names) AND NOT t.tgisinternal"
        ),
        {"names": list(OWN_TABLES)},
    ).all()
    actual = {r[0]: r for r in rows}
    problems = []
    if set(actual) != {t[0] for t in EXPECTED_TRIGGERS}:
        problems.append(f"名集合差{sorted(set(actual) ^ {t[0] for t in EXPECTED_TRIGGERS})}")
    for name, table, func, deferred in EXPECTED_TRIGGERS:
        r = actual.get(name)
        if r is None:
            continue
        if (r[1], r[2], bool(r[3]), bool(r[4])) != (table, func, deferred, deferred):
            problems.append(f"{name} 实际{tuple(r)[1:]}")
    return not problems, f"{problems or '6 个全部一致'}"


def j_migration_imports(source):
    # 迁移只允许 import rednote.modules.batches.ddl（触发器 DDL 同源）；不得 import 取值域。
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("rednote"):
            names = [a.name for a in node.names]
            found.append(f"{node.module}:{','.join(names)}")
        elif isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if a.name.startswith("rednote"))
    bad = [f for f in found if f != "rednote.modules.batches:ddl"]
    return not bad, f"rednote 导入={found} 越界={bad}"


def j_one_state_rollup(fn):
    # 8 个派生值各一例 + UNKNOWN 不折成 0 / FAILED 的几个例子。返回 (ok, detail)。
    cases = (
        ("QUEUED", "SUBMITTED", ["QUEUED", "QUEUED"]),
        ("RUNNING", "SUBMITTED", ["QUEUED", "RUNNING"]),
        ("RUNNING", "SUBMITTED", ["SUCCEEDED", "WAITING_RECONCILIATION"]),
        ("RUNNING", "SUBMITTED", ["FAILED", "WAITING_PROVIDER"]),
        ("CANCELLING", "CANCEL_REQUESTED", ["RUNNING", "SUCCEEDED"]),
        ("CANCELLING", "CANCEL_REQUESTED", ["CANCELLED", "WAITING_RECONCILIATION"]),
        ("CANCELLED", "CANCEL_REQUESTED", ["CANCELLED", "SUCCEEDED"]),
        ("SUCCEEDED", "SUBMITTED", ["SUCCEEDED", "SUCCEEDED"]),
        ("PARTIAL", "SUBMITTED", ["SUCCEEDED", "FAILED"]),
        ("PARTIAL", "SUBMITTED", ["PARTIAL", "FAILED"]),
        ("FAILED", "SUBMITTED", ["FAILED", "CANCELLED"]),
        ("UNKNOWN", "SUBMITTED", []),
        ("UNKNOWN", "SUBMITTED", [None, "SUCCEEDED"]),
        ("UNKNOWN", "SUBMITTED", ["NOT_A_JOB_STATE"]),
        ("UNKNOWN", "CANCEL_REQUESTED", [None]),
    )
    bad = []
    seen = set()
    for want, batch_state, states in cases:
        got = fn(batch_state, states).state
        seen.add(got)
        if got != want:
            bad.append((batch_state, states, got, want))
    recon = fn("SUBMITTED", ["SUCCEEDED", "WAITING_RECONCILIATION"])
    if recon.needs_reconciliation != 1:
        bad.append(("needs_reconciliation", recon.needs_reconciliation))
    return not bad, f"覆盖派生值 {len(seen)} 个={sorted(seen)} 偏差={bad}"


class BadProgress:
    # 错误实现：缺失 / 空集折成 FAILED，WAITING_RECONCILIATION 当终态。
    def __init__(self, state, total, counts):
        self.state = state
        self.counts = counts
        self.needs_reconciliation = 0


def bad_rollup(batch_state, states):
    terminal = {"SUCCEEDED", "FAILED", "CANCELLED", "PARTIAL", "WAITING_RECONCILIATION"}
    vals = [s for s in states if s is not None]
    if not vals:
        return BadProgress("FAILED", len(states), {})
    if all(s in terminal for s in vals):
        return BadProgress("SUCCEEDED" if all(s == "SUCCEEDED" for s in vals) else "FAILED",
                           len(states), {})
    return BadProgress("RUNNING", len(states), {})


def reverse(label, judge):
    # 反向验证：事务内制造坏状态，同一判据必须报红；无条件 rollback。
    with engine.connect() as conn:
        try:
            mutate = judge["mutate"]
            mutate(conn)
            ok, detail = judge["judge"](conn)
        finally:
            conn.rollback()
    emit(not ok, label, f"坏状态下判据报红={not ok}（{detail}）")


def tmp_db_url():
    url = make_url(DB)
    return url.set(database=f"{url.database}_tmp_{os.getpid()}")


def alembic(url, *args):
    env = os.environ.copy()
    env["REDNOTE_DATABASE_URL"] = url.render_as_string(hide_password=False)
    done = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "migrations/alembic.ini", *args],
        cwd=str(REPO), env=env, capture_output=True, text=True, check=False,
    )
    return done.returncode, (done.stdout + done.stderr)[-300:]


# --- M：迁移 -----------------------------------------------------------------
with engine.connect() as conn:
    has_alembic_table = conn.execute(
        text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
    ).scalar_one()
    current = None
    if has_alembic_table:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        current = row[0] if row else None
    emit(has_alembic_table and revision_is_at_least(current, WP32_MIGRATION_REVISION),
         "[M] 迁移 head 不早于 WP32 引入点（真 CHECK，不 SKIP）",
         f"current={current} 引入点={WP32_MIGRATION_REVISION}"
         f"{'' if has_alembic_table else '；目标库无 alembic_version，先 alembic upgrade head'}")

source = MIGRATION_FILE.read_text(encoding="utf-8")
ok, detail = j_migration_imports(source)
emit(ok, "[M] 迁移不从应用代码 import 取值域（只允许 batches.ddl）", detail)
emit("'BULK', 'SCALE', 'TRIAL'" in source and "'CANCEL_REQUESTED', 'SUBMITTED'" in source
     and "'GLOBAL', 'INHERITED', 'MANUAL'" in source,
     "[M] 迁移里取值域是化石字面量", "三组 IN 列表字面量都在迁移源码里")
emit(f"down_revision: str | Sequence[str] | None = '{WP32_DOWN_REVISION}'" in source,
     "[M] down_revision 指向 WP31 迁移", WP32_DOWN_REVISION)
q_hits = re.findall(r"quantity\s*(?:<=|<|>=|>|=|!=|BETWEEN|IN)\s*\(?\s*(?:\d+|'\d+')", source)
emit(q_hits and set(q_hits) == {"quantity > 0"},
     "[M] ADR-13：迁移里 quantity 比较只有 quantity > 0", f"命中 {q_hits}")
emit(not re.search(r"quantity\s*(?:<=|<|>=|=|BETWEEN|IN)\s*\(?\s*(?:10|30|50)\b", source),
     "[M] ADR-13：迁移不出现 10/30/50 作为 quantity 比较右值", "无命中")
dg = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == "downgrade"][0]
# ast.unparse 丢掉注释与文档字符串，只剩真正执行的代码。
dg_code = "\n".join(ast.unparse(n) for n in dg.body[1:])
exec_lines = [l for l in dg_code.splitlines() if "DROP FUNCTION" in l or "function_names" in l]
emit("consistency_function_names()" in dg_code and "rednote_reject_mutation" not in dg_code
     and "op.execute(_drop_function)" not in dg_code,
     "[M] downgrade 只删 consistency_function_names()，执行代码里不点名 rednote_reject_mutation",
     f"相关行 {exec_lines}")

# --- S：结构 -----------------------------------------------------------------
with engine.connect() as conn:
    n = conn.execute(
        text(
            "SELECT count(*) FROM information_schema.tables"
            " WHERE table_schema='public' AND table_name = ANY(:names)"
        ),
        {"names": list(OWN_TABLES)},
    ).scalar_one()
    emit(n == 3, "[S] 本包 3 张表存在", f"{n}/3（缺则先跑 alembic upgrade head；本脚本不建表）")

    for table, specs in EXPECTED_COLUMNS.items():
        rows = conn.execute(
            text(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull,"
                " pg_get_expr(d.adbin, d.adrelid)"
                " FROM pg_attribute a LEFT JOIN pg_attrdef d"
                " ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
                " WHERE a.attrelid = CAST(:t AS regclass) AND a.attnum > 0"
                " AND NOT a.attisdropped"
            ),
            {"t": table},
        ).all()
        actual = {r[0]: (r[1], bool(r[2]), r[3]) for r in rows}
        model_cols = {c.name for c in batches_models.Base.metadata.tables[table].columns}
        emit(set(actual) == model_cols == {s[0] for s in specs} and len(actual) == len(specs),
             f"[S] {table} 列名集合与模型 / 草案一致",
             f"库 {len(actual)} 列 / 期望 {len(specs)} 列 差集 {sorted(set(actual) ^ {s[0] for s in specs})}")
        for name, typ, nullable, default in specs:
            got = actual.get(name)
            emit(got == (typ, nullable, default),
                 f"[S] {table}.{name}",
                 f"实际 {got} 期望 {(typ, nullable, default)}")

    for table in OWN_TABLES:
        expected = EXPECTED_CHECK_NAMES[table]
        actual = db_names(conn, table, "c")
        model = model_names(table, "CheckConstraint")
        emit(actual == expected == model,
             f"[S] {table} CHECK 名集合：库 = 草案 = 模型（{len(expected)} 条）",
             f"库 {len(actual)} / 草案 {len(expected)} / 模型 {len(model)} 差集 {sorted(actual ^ expected)}")
    total = sum(len(db_names(conn, t, "c")) for t in OWN_TABLES)
    emit(total == 20, "[S] 3 表 CHECK 合计 20 条（15 + 4 + 1）", f"{total} 条")

    for table, cname, values in EXPECTED_DOMAINS:
        d = constraint_def(conn, cname) or ""
        got = set(re.findall(r"'([A-Z_]+)'", d))
        emit(got == values, f"[S] {cname} 取值域集合（与应用常量同值）",
             f"库 {sorted(got)} 期望 {sorted(values)}")
    app_domains = (batches_types.BATCH_KINDS, batches_types.BATCH_STATES, batches_types.LIMIT_SOURCES)
    emit([set(x) for x in app_domains] == [d[2] for d in EXPECTED_DOMAINS],
         "[S] 应用层取值域常量与迁移化石字面量一致", "三组一致")
    ok, detail = j_adr13_db(conn)
    emit(ok, "[S] ADR-13：库内 quantity 的 CHECK 只有 quantity > 0（无 10/30/50 上界）", detail)

    for table, name, ref in EXPECTED_FKS:
        row = conn.execute(
            text(
                "SELECT confrelid::regclass::text, confdeltype, convalidated"
                " FROM pg_constraint WHERE conname = :n AND contype = 'f'"
                " AND conrelid = CAST(:t AS regclass)"
            ),
            {"n": name, "t": table},
        ).first()
        emit(row is not None and row[0] == ref and row[1] == "r" and row[2],
             f"[S] FK {name}", f"实际 {tuple(row) if row else None} 期望 ({ref}, r, True)")
    for table in OWN_TABLES:
        expected = {n for t, n, _ in EXPECTED_FKS if t == table}
        actual = db_names(conn, table, "f")
        extra = {n for n in actual if n not in expected}
        emit(not extra and model_names(table, "ForeignKeyConstraint") == actual,
             f"[S] {table} FK 全集与模型一致（无多余）", f"库 {len(actual)} 多余 {sorted(extra)}")

    for table, name, cols in EXPECTED_UNIQUES:
        row = conn.execute(
            text(
                "SELECT array_agg(a.attname ORDER BY k.ord)"
                " FROM pg_constraint c"
                " CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)"
                " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum"
                " WHERE c.conname = :n AND c.contype = 'u'"
                " AND c.conrelid = CAST(:t AS regclass)"
            ),
            {"n": name, "t": table},
        ).scalar_one_or_none()
        emit(row == cols, f"[S] 唯一约束 {name}", f"实际 {row} 期望 {cols}")
    for table in OWN_TABLES:
        expected = {n for t, n, _ in EXPECTED_UNIQUES if t == table}
        emit(db_names(conn, table, "u") == expected,
             f"[S] {table} 唯一约束全集一致（无多余）", f"库 {sorted(db_names(conn, table, 'u'))}")

    for table, name, cols in EXPECTED_INDEXES:
        row = conn.execute(
            text(
                "SELECT array_agg(a.attname ORDER BY k.ord)"
                " FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
                " CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)"
                " JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum"
                " WHERE c.relname = :n"
            ),
            {"n": name},
        ).scalar_one_or_none()
        emit(row == cols, f"[S] 索引 {name}", f"实际 {row} 期望 {cols}")
    model_ix = set()
    for table in OWN_TABLES:
        for index in batches_models.Base.metadata.tables[table].indexes:
            model_ix.add(index.name)
    emit(model_ix == {n for _, n, _ in EXPECTED_INDEXES}, "[S] 模型声明的索引集合 = 草案 3 条",
         f"模型 {sorted(model_ix)}")

    ok, detail = j_triggers(conn)
    emit(ok, "[S] 6 个触发器（2 不可变 BEFORE + 4 延迟一致性）名 / 表 / 函数 / 延迟属性", detail)
    emit(set(batches_ddl.trigger_names()) == {t[0] for t in EXPECTED_TRIGGERS},
         "[S] ddl.trigger_names() 与化石清单一致", f"{sorted(batches_ddl.trigger_names())}")
    for fn in OWN_FUNCTIONS:
        n = conn.execute(text("SELECT count(*) FROM pg_proc WHERE proname = :f"), {"f": fn}).scalar_one()
        emit(n == 1, f"[S] 自有函数存在 {fn}", f"{n} 个")
    emit(set(batches_ddl.consistency_function_names()) == set(OWN_FUNCTIONS)
         and "rednote_reject_mutation" not in batches_ddl.consistency_function_names(),
         "[S] consistency_function_names() 恰为 3 个自有函数，不含 rednote_reject_mutation",
         f"{batches_ddl.consistency_function_names()}")

    ok, detail = j_jobs_fk(conn)
    emit(ok, "[S] jobs.batch_id 外键存在且 convalidated=true（ON DELETE RESTRICT）", detail)
    ok, detail = j_jobs_ix(conn)
    emit(ok, "[S] ix_jobs_batch_id 为非唯一部分索引（WHERE batch_id IS NOT NULL）", detail)

    too_long = [
        row[0]
        for row in conn.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE length(conname) > :m"
                " AND conrelid = ANY(SELECT oid FROM pg_class WHERE relname = ANY(:names))"
            ),
            {"m": MAX_IDENTIFIER_BYTES, "names": list(OWN_TABLES)},
        )
    ]
    emit(not too_long, "[S] 全部约束名在 63 字节内（不会被静默截断）", f"超限 {too_long}")

# --- P：行为探针（每组独立连接，无条件 rollback）-----------------------------------
with engine.connect() as conn:
    try:
        for key in PROBES:
            ok, detail = run_probe(conn, key)
            emit(ok, PROBES[key][0], detail)
    finally:
        conn.rollback()

# --- X：跨包共存 --------------------------------------------------------------
with engine.connect() as conn:
    n = conn.execute(text("SELECT count(*) FROM pg_proc WHERE proname='rednote_reject_mutation'")).scalar_one()
    emit(n == 1, "[X] 跨包共用不可变函数 rednote_reject_mutation() 仍在", f"{n} 个")

# --- R：路由 ------------------------------------------------------------------
from rednote.main import create_app

schema = create_app().openapi()
actual_routes = set()
for path, item in schema["paths"].items():
    if "/batches" in path or "/batch-previews" in path:
        for method, op in item.items():
            actual_routes.add((path, method, op.get("operationId")))
emit(actual_routes == EXPECTED_ROUTES,
     "[R] 本包 7 个 operation 的 path / method / operationId 逐条对账",
     f"多 {sorted(actual_routes - EXPECTED_ROUTES)} 少 {sorted(EXPECTED_ROUTES - actual_routes)}")
emit(len({r[0] for r in actual_routes}) == 6 and len(actual_routes) == 7
     and len({r[2] for r in actual_routes}) == 7,
     "[R] 6 个 path / 7 个 operation / operationId 互不重复",
     f"path {len({r[0] for r in actual_routes})} op {len(actual_routes)}")

# --- G：进度折算纯函数 ----------------------------------------------------------
ok, detail = j_one_state_rollup(batches_progress.rollup)
emit(ok, "[G] rollup 8 个派生值各一例 + WAITING_RECONCILIATION 非终态 + 单列", detail)
emit(set(batches_types.PROGRESS_STATES) == {
        "QUEUED", "RUNNING", "CANCELLING", "SUCCEEDED", "PARTIAL", "FAILED",
        "CANCELLED", "UNKNOWN"},
     "[G] PROGRESS_STATES 恰为 8 值", f"{sorted(batches_types.PROGRESS_STATES)}")
unk = batches_progress.rollup("SUBMITTED", [None, "FAILED"])
emit(unk.state == "UNKNOWN" and unk.missing == 1 and unk.state not in ("FAILED", "SUCCEEDED"),
     "[G] 缺失 run 的成员 ⇒ UNKNOWN（不折成 0 / FAILED），missing 单列", f"state={unk.state} missing={unk.missing}")
emp = batches_progress.rollup("SUBMITTED", [])
emit(emp.state == "UNKNOWN" and emp.total == 0, "[G] 空批次 ⇒ UNKNOWN（不折成 SUCCEEDED）", f"state={emp.state}")
emit(len(batches_progress.rollup("SUBMITTED", ["QUEUED"]).counts) == 9,
     "[G] counts 恒 9 个键（确为 0 才是 0）", "9 键")

# --- D：一次性临时库真跑 downgrade / 再 upgrade ---------------------------------
tmp_url = tmp_db_url()
admin = create_engine(make_url(DB).set(database="postgres"), isolation_level="AUTOCOMMIT")
tmp_name = tmp_url.database
try:
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{tmp_name}"'))
    tmp_engine = create_engine(tmp_url)
    with tmp_engine.connect() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        c.commit()
    rc, tail = alembic(tmp_url, "upgrade", "head")
    emit(rc == 0, "[D] 临时库 alembic upgrade head 成功", f"rc={rc}")

    def snapshot():
        with tmp_engine.connect() as c:
            funcs = {r[0] for r in c.execute(text(
                "SELECT proname FROM pg_proc WHERE proname = ANY(:n)"),
                {"n": list(OWN_FUNCTIONS) + ["rednote_reject_mutation"]})}
            tables = {r[0] for r in c.execute(text(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema='public' AND table_name = ANY(:n)"), {"n": list(OWN_TABLES)})}
            col = c.execute(text(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_name='jobs' AND column_name='batch_id'")).scalar_one()
            fk = c.execute(text(
                "SELECT count(*) FROM pg_constraint WHERE conname='fk_jobs_batch_id_batches'")).scalar_one()
            ix = c.execute(text("SELECT count(*) FROM pg_class WHERE relname='ix_jobs_batch_id'")).scalar_one()
            return funcs, tables, col, fk, ix

    funcs, tables, col, fk, ix = snapshot()
    emit(tables == set(OWN_TABLES) and fk == 1 and ix == 1 and funcs == set(OWN_FUNCTIONS) | {"rednote_reject_mutation"},
         "[D] upgrade 后 3 表 / 3 自有函数 / 共用函数 / jobs 外键与索引齐全", f"{sorted(funcs)} {sorted(tables)} fk={fk} ix={ix}")
    rc, tail = alembic(tmp_url, "downgrade", WP32_DOWN_REVISION)
    emit(rc == 0, "[D] downgrade 到 WP31 引入点成功", f"rc={rc} {tail[-120:] if rc else ''}")
    funcs, tables, col, fk, ix = snapshot()
    emit(not (funcs & set(OWN_FUNCTIONS)) and not tables and fk == 0 and ix == 0,
         "[D] downgrade 后 3 表 / 3 自有函数 / jobs 外键与索引都已摘除",
         f"函数 {sorted(funcs)} 表 {sorted(tables)} fk={fk} ix={ix}")
    emit("rednote_reject_mutation" in funcs, "[D] downgrade 后 rednote_reject_mutation() 仍在（不删跨包共用函数）",
         f"funcs={sorted(funcs)}")
    emit(col == 1, "[D] downgrade 后 jobs.batch_id 列保留（只摘外键与索引）", f"列数 {col}")
    rc, tail = alembic(tmp_url, "upgrade", "head")
    funcs, tables, col, fk, ix = snapshot()
    emit(rc == 0 and tables == set(OWN_TABLES) and fk == 1 and ix == 1,
         "[D] 再次 upgrade head 成功（downgrade 后可重新升级）", f"rc={rc} 表 {sorted(tables)} fk={fk} ix={ix}")
    tmp_engine.dispose()
finally:
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{tmp_name}" WITH (FORCE)'))
        gone = c.execute(text("SELECT count(*) FROM pg_database WHERE datname = :n"), {"n": tmp_name}).scalar_one()
    emit(gone == 0, "[D] 临时库已 DROP", f"残留 {gone}")
    admin.dispose()


# --- V：反向验证（事务内制造坏状态，判据必须报红；全部 rollback）--------------------
def m_widen_quantity(conn):
    conn.execute(text("ALTER TABLE batches DROP CONSTRAINT ck_batches_quantity_positive"))
    conn.execute(text("ALTER TABLE batches ADD CONSTRAINT ck_batches_quantity_positive CHECK (quantity > 0 AND quantity <= 50)"))


def m_drop_run_trigger(conn):
    conn.execute(text("DROP TRIGGER trg_batch_item_runs_ownership_consistency ON batch_item_runs"))


def m_fk_not_valid(conn):
    conn.execute(text("ALTER TABLE jobs DROP CONSTRAINT fk_jobs_batch_id_batches"))
    conn.execute(text(
        "ALTER TABLE jobs ADD CONSTRAINT fk_jobs_batch_id_batches FOREIGN KEY (batch_id)"
        " REFERENCES batches (id) ON DELETE RESTRICT NOT VALID"))


def m_drop_cancel_check(conn):
    conn.execute(text("ALTER TABLE batches DROP CONSTRAINT ck_batches_cancel_pair_consistent"))


def m_drop_immutable(conn):
    conn.execute(text("DROP TRIGGER trg_batch_items_immutable ON batch_items"))


def m_noop_count_function(conn):
    conn.execute(text(
        "CREATE OR REPLACE FUNCTION rednote_check_batch_item_count() RETURNS trigger AS $f$"
        " BEGIN RETURN NULL; END; $f$ LANGUAGE plpgsql"))


def m_full_index(conn):
    conn.execute(text("DROP INDEX ix_jobs_batch_id"))
    conn.execute(text("CREATE INDEX ix_jobs_batch_id ON jobs (batch_id)"))


reverse("[V1] 把 quantity 的 CHECK 改成 <= 50 ⇒ ADR-13 判据报红",
        {"mutate": m_widen_quantity, "judge": j_adr13_db})
reverse("[V2] 摘掉 run 归属触发器 ⇒ “job.batch_id 为空被拒”探针报红",
        {"mutate": m_drop_run_trigger, "judge": lambda c: run_probe(c, "run_null")})
reverse("[V3] jobs 外键改成 NOT VALID ⇒ convalidated 判据报红",
        {"mutate": m_fk_not_valid, "judge": j_jobs_fk})
reverse("[V4] 摘掉 cancel 双向等值 CHECK ⇒ CHECK 名集合判据报红",
        {"mutate": m_drop_cancel_check, "judge": j_check_names})
reverse("[V5] 摘掉 cancel 双向等值 CHECK ⇒ cancel 探针报红",
        {"mutate": m_drop_cancel_check, "judge": lambda c: run_probe(c, "cancel_no_time")})
reverse("[V6] 摘掉 batch_items 不可变触发器 ⇒ “拒绝 UPDATE”探针报红",
        {"mutate": m_drop_immutable, "judge": lambda c: run_probe(c, "imm_item_upd")})
reverse("[V7] 把计数函数换成空操作 ⇒ “quantity 与成员数不一致被拒”探针报红",
        {"mutate": m_noop_count_function, "judge": lambda c: run_probe(c, "count_low")})
reverse("[V8] 把部分索引换成全量索引 ⇒ ix_jobs_batch_id 部分索引判据报红",
        {"mutate": m_full_index, "judge": j_jobs_ix})
ok, detail = j_one_state_rollup(bad_rollup)
emit(not ok, "[V9] 把空集 / 缺失折成 FAILED、WAITING_RECONCILIATION 当终态的错误 rollup ⇒ 折算判据报红",
     f"坏实现下判据报红={not ok}（{detail[:160]}）")
bad_src = source + "\nfrom rednote.modules.batches.types import BATCH_KINDS\n"
ok, detail = j_migration_imports(bad_src)
emit(not ok, "[V10] 迁移源码混入 import batches.types ⇒ “不 import 取值域”判据报红",
     f"坏源码下判据报红={not ok}（{detail}）")
bad_src = source.replace("quantity > 0", "quantity <= 50")
hits = re.findall(r"quantity\s*(?:<=|<|>=|>|=|!=|BETWEEN|IN)\s*\(?\s*(?:\d+|'\d+')", bad_src)
emit(set(hits) != {"quantity > 0"}, "[V11] 迁移源码把 quantity > 0 改成 <= 50 ⇒ ADR-13 源码判据报红",
     f"坏源码命中 {hits}")

# --- Z：探针零残留 --------------------------------------------------------------
with engine.connect() as conn:
    n = conn.execute(
        text(
            "SELECT count(*) FROM batches b JOIN projects p ON p.id = b.project_id"
            " WHERE p.name = :n"
        ),
        {"n": PROBE_PROJECT_NAME},
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 batches（探针项目）", f"{n} 行")
    n = conn.execute(
        text(
            "SELECT count(*) FROM batch_items i JOIN batches b ON b.id = i.batch_id"
            " JOIN projects p ON p.id = b.project_id WHERE p.name = :n"
        ),
        {"n": PROBE_PROJECT_NAME},
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 batch_items（探针项目）", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM jobs WHERE idempotency_scope = 'wp32-probe'")
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 jobs（探针）", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM users WHERE issuer = :iss"), {"iss": PROBE_ISSUER}
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 users（探针）", f"{n} 行")
    ok, _ = j_adr13_db(conn)
    emit(ok and j_jobs_fk(conn)[0] and j_jobs_ix(conn)[0] and j_check_names(conn)[0]
         and j_triggers(conn)[0],
         "[Z] 反向验证后目标库结构原样（ADR-13 / jobs 外键 / 部分索引 / CHECK / 触发器）", "全部复原")

print(f"RESULT_EMITTED (期望 {EXPECTED_CHECKS} 条)")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def collect_wp32() -> tuple[Path, bool]:
    env = os.environ.copy()
    env.update(
        {
            "NO_PROXY": "*",
            "no_proxy": "*",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "PYTHONPATH": str(BACKEND_DIR / "src"),
            DB_ENV: os.environ.get(DB_ENV, DEFAULT_DB),
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
        "# WP32 扩产 / 批产：3 表逐列 / 20 条 CHECK / FK / 唯一约束 / 索引 /"
        " 6 个触发器 / jobs.batch_id 外键与部分索引 / ADR-13 / 行为探针 /"
        " 临时库 downgrade 往返 / 路由对账 / 进度折算 / 反向验证 + 探针零残留",
        f"# 生成时间: {_now()}",
        f"# 目标库: {env[DB_ENV]}",
        "# 命令: cd services/backend && python -c '<pg_catalog + 试写 + 试插 + OpenAPI + 临时库 alembic>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp32-batches.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    complete = len(checks) >= EXPECTED_CHECKS
    exact = len(checks) == EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete and exact)


def main() -> int:
    path, ok = collect_wp32()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
