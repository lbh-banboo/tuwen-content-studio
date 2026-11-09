#!/usr/bin/env python
"""WP34 证据采集脚本（回退影响图 / 导出失效）。

产出的文件（落在 `evidence/m3/`）：

| 文件 | 内容 |
|---|---|
| `wp34-export-validity.log` | [M] 迁移 head 真 CHECK + 迁移文件机械判据（只 import `versions.impact.ddl` / 取值域是化石字面量 / downgrade 不删共用函数）；[S] `export_validity_events` 逐列 / 类型 / 可空 / 默认、13 个约束名逐名对账模型（7 CHECK + 4 FK + PK + UQ）、取值域集合、FK（含指向 `export_items.id` 的点名与 ON DELETE RESTRICT）、唯一约束、索引、2 个触发器与 1 个自有函数；[P] 行为探针（独立事务 + rollback）：不可变 UPDATE / DELETE 被拒（23001）、seq 连续与 VALID / INVALIDATED 交替（23514，按消息区分）、观测指纹 / 版本自洽、各 CHECK / FK / UQ 阴性探针；[T] ★ T-EXP-01 证据版：向事件表 INSERT 前后，被引用 `export_items` 行的 xmin / ctid / 整行 md5 不变，且直接 UPDATE / DELETE `export_items` 仍被 23001 拒绝；[X] 跨包共存；[D] 一次性临时库 upgrade -> downgrade -> upgrade 往返；[R] 路由对账（5 个 operation）；[G] 遍历纯函数（分层 BFS / 环只标记不抛错 / 深度与节点上限的确定前缀 / E3 链回溯）；[C] 静态源码守卫；[V] 反向验证（错误实现 / 错误数据下判据必须报红）；[Z] 探针零残留 |

## 本包的结构性特点

- 只有一张表 `export_validity_events`：不可变、追加写；`export_items` 零改动，
  事件表只有一条指向它的出向 FK。
- 当前态 = 同一 `export_item_id` 下最大 `seq` 的 `transition`（无事件 = VALID）。
  `seq` 由 AFTER INSERT 即时触发器钉成「从 1 连续、INVALIDATED / RESTORED 严格交替」。
- 取值域（transition / reason / scope_kind）在迁移里是化石字面量，迁移不 import types / models。
- downgrade 只删本包自己的 1 个函数，不删跨包共用的 `rednote_reject_mutation`。

## 目标库与迁移

默认库 `rednote_verify_wp34`（需先 `createdb` + `CREATE EXTENSION vector` +
`alembic upgrade head`），可用环境变量 `REDNOTE_WP34_EVIDENCE_DATABASE_URL` 覆盖。
迁移 head 判据是**真 CHECK**（不早于 `c3f34c0de003`），库里没有 `alembic_version` 表即 FAIL，不 SKIP。

注意：**本脚本不建目标库里的任何表**。结构不全时报 FAIL 并提示先跑迁移 ——
自己建表再核对自己建的表是循环判据（恒绿）。
唯一的建库动作是 [D] 节的一次性临时库（`<目标库>_tmp_<pid>`），用完 DROP。

## 探针自身踩过的坑

1. **期望条数必须实测，不能凭印象**（与 WP24~WP32 同型教训）：
   `complete = len(checks) >= EXPECTED_CHECKS` 在期望值偏低时恒真，
   本脚本另加 `exact = len(checks) == EXPECTED_CHECKS` 并入 verdict。
2. **同一事务内 xmin 分辨不出自己做的 UPDATE**：探针必须在回滚事务里造 `export_items`
   行（不可变表不能提交），于是行的 xmin 就是本事务 id；同事务内再 UPDATE，新行版本的 xmin
   仍是本事务 id。所以 T-EXP-01 的快照同时取 xmin、ctid（UPDATE 必产生新 ctid）、整行 md5，
   并用反向验证 V8（摘掉触发器后 UPDATE）证明这个快照确实能分辨出行被改写。
3. **CHECK 类探针要核对 `diag.constraint_name`**：只比 sqlstate=23514 区分不出
   「被我要测的那条拒绝」与「被别的 CHECK 顺手拒绝」；触发器类 23514 没有约束名，按消息区分。
4. **不可变表探针绝不能 commit**：每组探针独立连接 + 无条件 `rollback()`；单条探针再包一层 savepoint。
5. **反向验证改的是库对象（事务内 DDL），不是代码树**：做完 `rollback()`，库回到原样；
   紧随其后的 [Z] 与再次的结构判据证明确实回到原样。静态守卫与迁移源码判据的反向验证
   用内存里被污染的源码文本，不写盘。
6. **内联脚本只允许单行注释且不能出现三引号**（ISS-45），由
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
EXPECTED_CHECKS: int = 134

DEFAULT_DB = "postgresql+psycopg://rednote@localhost:5432/rednote_verify_wp34"
DB_ENV = "REDNOTE_WP34_EVIDENCE_DATABASE_URL"

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

from rednote.modules.versions import impact as impact_pkg
from rednote.modules.versions.impact import ddl as impact_ddl
from rednote.modules.versions.impact import models as impact_models
from rednote.modules.versions.impact import traversal as impact_traversal
from rednote.modules.versions.impact import types as impact_types

EXPECTED_CHECKS = 134

DB = os.environ.get(
    "REDNOTE_WP34_EVIDENCE_DATABASE_URL",
    "postgresql+psycopg://rednote@localhost:5432/rednote_verify_wp34",
)
engine = create_engine(DB)

WP34_MIGRATION_REVISION = "c3f34c0de003"
WP34_DOWN_REVISION = "b2f32c0de002"

TABLE = "export_validity_events"
IMMUTABLE_SQLSTATE = "23001"
CONSISTENCY_SQLSTATE = "23514"
UNIQUE_SQLSTATE = "23505"
FK_SQLSTATE = "23503"
MAX_IDENTIFIER_BYTES = 63

PROBE_ISSUER = "wp34-probe"
PROBE_PROJECT_NAME = "WP34 探针项目"
PROBE_PROJECT_PREFIX = "WP34PROBE"
PROBE_BLOB_PREFIX = "wp34-probe/"
PROBE_HASH_PREFIX = "wp34-probe-"

HA = "a" * 64
HB = "b" * 64
HC = "c" * 64

# 以下全是写死的化石字面量（刻意不从应用代码取），用来对账迁移与库。
EXPECTED_COLUMNS = (
    ("id", "uuid", False, "gen_random_uuid()"),
    ("export_item_id", "uuid", False, None),
    ("seq", "integer", False, None),
    ("transition", "character varying(12)", False, None),
    ("reason", "character varying(24)", True, None),
    ("observed_version_id", "uuid", False, None),
    ("observed_eligibility_hash", "character varying(64)", False, None),
    ("scope_kind", "character varying(16)", False, None),
    ("source_version_id", "uuid", True, None),
    ("created_by", "uuid", False, None),
    ("created_at", "timestamp with time zone", False, "now()"),
)

EXPECTED_CHECK_NAMES = {
    "ck_export_validity_events_seq_positive",
    "ck_export_validity_events_transition_valid",
    "ck_export_validity_events_reason_valid",
    "ck_export_validity_events_reason_paired",
    "ck_export_validity_events_scope_kind_valid",
    "ck_export_validity_events_scope_source_paired",
    "ck_export_validity_events_observed_hash_shape",
}
EXPECTED_ALL_NAMES = EXPECTED_CHECK_NAMES | {
    "pk_export_validity_events",
    "uq_export_validity_events_export_item_id_seq",
    "fk_export_validity_events_created_by_users",
    "fk_export_validity_events_export_item_id_export_items",
    "fk_export_validity_events_observed_version_id_version_nodes",
    "fk_export_validity_events_source_version_id_version_nodes",
}

EXPECTED_DOMAINS = (
    ("ck_export_validity_events_transition_valid", {"INVALIDATED", "RESTORED"}),
    ("ck_export_validity_events_reason_valid", {"ASSET_CHANGED", "VERSION_SUPERSEDED"}),
    ("ck_export_validity_events_scope_kind_valid", {"CONTENTS", "FROM_VERSION"}),
)

# (约束名, 本表列, 被引用表, 被引用列)
EXPECTED_FKS = (
    ("fk_export_validity_events_created_by_users", ["created_by"], "users", ["id"]),
    ("fk_export_validity_events_export_item_id_export_items", ["export_item_id"],
     "export_items", ["id"]),
    ("fk_export_validity_events_observed_version_id_version_nodes",
     ["observed_version_id"], "version_nodes", ["id"]),
    ("fk_export_validity_events_source_version_id_version_nodes",
     ["source_version_id"], "version_nodes", ["id"]),
)

EXPECTED_UNIQUES = (
    ("uq_export_validity_events_export_item_id_seq", ["export_item_id", "seq"]),
)
EXPECTED_INDEX_NAMES = {
    "pk_export_validity_events",
    "uq_export_validity_events_export_item_id_seq",
}

# tgtype 位：ROW=1 BEFORE=2 INSERT=4 DELETE=8 UPDATE=16
EXPECTED_TRIGGERS = (
    ("trg_export_validity_events_immutable", "rednote_reject_mutation", 1 | 2 | 8 | 16),
    ("trg_export_validity_events_consistency", "rednote_check_export_validity_event", 1 | 4),
)
OWN_FUNCTIONS = ("rednote_check_export_validity_event",)

EXPECTED_ROUTES = {
    ("/api/v1/projects/{projectId}/rollback-impacts", "get",
     "get_api_v1_projects_projectId_rollback-impacts"),
    ("/api/v1/projects/{projectId}/versions/{versionId}/impact", "get",
     "get_api_v1_projects_projectId_versions_versionId_impact"),
    ("/api/v1/projects/{projectId}/exports/{exportGroupId}/validity", "get",
     "get_api_v1_projects_projectId_exports_exportGroupId_validity"),
    ("/api/v1/projects/{projectId}/export-validity-sweeps", "post",
     "post_api_v1_projects_projectId_export-validity-sweeps"),
    ("/api/v1/projects/{projectId}/export-validity-events", "get",
     "get_api_v1_projects_projectId_export-validity-events"),
}

IMPACT_FILES = {
    "__init__.py", "api.py", "currentness_bridge.py", "ddl.py", "dependencies.py",
    "export_bridge.py", "export_items_bridge.py", "models.py", "repository.py",
    "schemas.py", "service.py", "traversal.py", "types.py",
}

REPO = Path.cwd().resolve()
while REPO != REPO.parent and not (REPO / "migrations" / "alembic.ini").exists():
    REPO = REPO.parent
MIGRATION_FILE = REPO / "migrations" / "versions" / "20261023_0318_c3f34c0de003_wp34_export_validity.py"
IMPACT_DIR = Path(impact_pkg.__file__).resolve().parent


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


def constraint_def(conn, name):
    return conn.execute(
        text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
        {"n": name},
    ).scalar_one_or_none()


# --- 探针夹具 -----------------------------------------------------------------
def mk_world(conn):
    w = {}
    uid = uuid.uuid4()
    brand = uuid.uuid4()
    prod = uuid.uuid4()
    proj = uuid.uuid4()
    content = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, display_name, role, status)"
            " VALUES (:u, :iss, :sub, 'WP34 探针', 'ADMIN', 'ACTIVE')"
        ),
        {"u": str(uid), "iss": PROBE_ISSUER, "sub": f"probe-{uid}"},
    )
    conn.execute(
        text("INSERT INTO brands (id, name, status) VALUES (:b, :n, 'ACTIVE')"),
        {"b": str(brand), "n": f"WP34 品牌 {str(brand)[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO products (id, brand_id, name, model_code, status)"
            " VALUES (:p, :b, :n, :c, 'ACTIVE')"
        ),
        {"p": str(prod), "b": str(brand), "n": f"WP34 产品 {str(prod)[:8]}",
         "c": f"WP34-{str(prod)[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO projects (id, project_code, name, brand_id, product_id,"
            " owner_id, target_delivery_count, due_date)"
            " VALUES (:p, :c, :n, :b, :pr, :o, 1, (now() + interval '30 days')::date)"
        ),
        {"p": str(proj), "c": f"{PROBE_PROJECT_PREFIX}{str(proj)[:8]}",
         "n": PROBE_PROJECT_NAME, "b": str(brand), "pr": str(prod), "o": str(uid)},
    )
    conn.execute(
        text(
            "INSERT INTO content_items (id, project_id, state, current_version_no,"
            " lock_version) VALUES (:c, :p, 'DRAFT', 0, 0)"
        ),
        {"c": str(content), "p": str(proj)},
    )
    w["user"], w["project"], w["content"] = str(uid), str(proj), str(content)
    for no, key in ((1, "v1"), (2, "v2")):
        node = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO version_nodes (id, entity_type, entity_id, project_id,"
                " revision_no, payload_hash, schema_version)"
                " VALUES (:n, 'CONTENT', :e, :p, :r, :h, 1)"
            ),
            {"n": str(node), "e": w["content"], "p": w["project"], "r": no,
             "h": f"{PROBE_HASH_PREFIX}{str(node)[:12]}"},
        )
        w[key] = str(node)
    blob = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO blobs (id, storage_backend, object_key, sha256, byte_size, mime_type)"
            " VALUES (:i, 'LOCAL', :k, :h, 1, 'application/zip')"
        ),
        {"i": str(blob), "k": f"{PROBE_BLOB_PREFIX}{blob}", "h": "0" * 64},
    )
    fobj = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO file_objects (id, blob_id, original_name, visibility, project_id,"
            " classification, uploaded_by)"
            " VALUES (:i, :b, 'wp34-probe.zip', 'PROJECT', :p, 'SUPPORTED', :u)"
        ),
        {"i": str(fobj), "b": str(blob), "p": w["project"], "u": w["user"]},
    )
    w["file"] = str(fobj)
    w["item"] = mk_export_item(conn, w)
    w["item2"] = mk_export_item(conn, w)
    return w


def mk_export_item(conn, w):
    item = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO export_items (id, project_id, export_group_id, content_item_id,"
            " version_node_id, eligibility_hash, export_kind, export_format, item_count,"
            " artifact_file_id, zip_sha256, zip_byte_size, request_hash, created_by)"
            " VALUES (:i, :p, :g, :c, :v, :h, 'SINGLE', 'STATIC', 1, :f, :z, 1, :z, :u)"
        ),
        {"i": item, "p": w["project"], "g": str(uuid.uuid4()), "c": w["content"],
         "v": w["v1"], "h": HA, "f": w["file"], "z": "1" * 64, "u": w["user"]},
    )
    return item


def ev(conn, w, seq, transition, **over):
    # 默认造一条合法事件：INVALIDATED 观测到 v2 / HB，RESTORED 回到冻结值 v1 / HA。
    inv = transition == "INVALIDATED"
    row = {
        "id": str(uuid.uuid4()),
        "export_item_id": w["item"],
        "seq": seq,
        "transition": transition,
        "reason": "VERSION_SUPERSEDED" if inv else None,
        "observed_version_id": w["v2"] if inv else w["v1"],
        "observed_eligibility_hash": HB if inv else HA,
        "scope_kind": "CONTENTS",
        "source_version_id": None,
        "created_by": w["user"],
    }
    row.update(over)
    cols = list(row)
    values = ", ".join(f":{c}" for c in cols)
    conn.execute(
        text(f"INSERT INTO {TABLE} ({', '.join(cols)}) VALUES ({values})"), row
    )
    return row["id"]


def snap(conn, item):
    row = conn.execute(
        text("SELECT xmin::text, md5(t::text), ctid::text FROM export_items t WHERE id = :i"),
        {"i": item},
    ).first()
    return tuple(row) if row else None


def seq_of(*steps):
    def build(conn):
        w = mk_world(conn)
        for seq, transition, over in steps:
            ev(conn, w, seq, transition, **over)
    return build


def build_with(fn):
    def build(conn):
        w = mk_world(conn)
        fn(conn, w)
    return build


def mutate_event(sql_text):
    def build(conn):
        w = mk_world(conn)
        ev(conn, w, 1, "INVALIDATED")
        conn.execute(text(sql_text), {"i": w["item"]})
    return build


def mutate_export_item(sql_text):
    def build(conn):
        w = mk_world(conn)
        conn.execute(text(sql_text), {"i": w["item"]})
    return build


def rnd():
    return str(uuid.uuid4())


# (label, build, 期望 sqlstate（None=必须放行）, 期望约束名或 msg:消息片段（None=不核）)
PROBES = {
    "ok_alternate": ("[P] INVALIDATED(1) -> RESTORED(2) -> INVALIDATED(3) 合法交替放行（阴性对照）",
        build_with(lambda c, w: (ev(c, w, 1, "INVALIDATED"), ev(c, w, 2, "RESTORED"),
                                 ev(c, w, 3, "INVALIDATED", reason="ASSET_CHANGED",
                                    observed_version_id=w["v1"], observed_eligibility_hash=HC))),
        None, None),
    "ok_asset": ("[P] ASSET_CHANGED：版本仍是冻结版、指纹变了，放行（阴性对照）",
        build_with(lambda c, w: ev(c, w, 1, "INVALIDATED", reason="ASSET_CHANGED",
                                   observed_version_id=w["v1"])), None, None),
    "ok_from_version": ("[P] FROM_VERSION + source_version_id 放行（阴性对照）",
        build_with(lambda c, w: ev(c, w, 1, "INVALIDATED", scope_kind="FROM_VERSION",
                                   source_version_id=w["v2"])), None, None),
    "ok_two_items": ("[P] 不同导出行各自从 seq=1 起，互不干扰（阴性对照）",
        build_with(lambda c, w: (ev(c, w, 1, "INVALIDATED"),
                                 ev(c, w, 1, "INVALIDATED", export_item_id=w["item2"]))),
        None, None),
    "first_seq2": ("[P] 首条事件 seq=2 被拒（触发器）",
        seq_of((2, "INVALIDATED", {})), CONSISTENCY_SQLSTATE, "msg:首条事件必须是 seq=1"),
    "first_restored": ("[P] 首条事件是 RESTORED 被拒（无失效不能恢复）",
        seq_of((1, "RESTORED", {})), CONSISTENCY_SQLSTATE, "msg:首条事件必须是 seq=1"),
    "seq_gap": ("[P] seq 跳号（1 -> 3）被拒",
        seq_of((1, "INVALIDATED", {}), (3, "RESTORED", {})),
        CONSISTENCY_SQLSTATE, "msg:不连续或未交替"),
    "same_transition": ("[P] 连续两条 INVALIDATED 被拒（未交替）",
        seq_of((1, "INVALIDATED", {}), (2, "INVALIDATED", {})),
        CONSISTENCY_SQLSTATE, "msg:不连续或未交替"),
    "restored_twice": ("[P] 连续两条 RESTORED 被拒（未交替）",
        seq_of((1, "INVALIDATED", {}), (2, "RESTORED", {}), (3, "RESTORED", {})),
        CONSISTENCY_SQLSTATE, "msg:不连续或未交替"),
    "inv_same_hash": ("[P] INVALIDATED 的观测指纹等于冻结指纹被拒",
        seq_of((1, "INVALIDATED", {"observed_eligibility_hash": HA})),
        CONSISTENCY_SQLSTATE, "msg:观测指纹与冻结指纹相同"),
    "vs_same_version": ("[P] VERSION_SUPERSEDED 但观测版本仍是冻结版本被拒",
        build_with(lambda c, w: ev(c, w, 1, "INVALIDATED", observed_version_id=w["v1"])),
        CONSISTENCY_SQLSTATE, "msg:观测版本仍等于冻结版本"),
    "ac_other_version": ("[P] ASSET_CHANGED 但观测版本已不是冻结版本被拒",
        seq_of((1, "INVALIDATED", {"reason": "ASSET_CHANGED"})),
        CONSISTENCY_SQLSTATE, "msg:观测版本已不是冻结版本"),
    "res_bad_hash": ("[P] RESTORED 的观测指纹没回到冻结值被拒",
        seq_of((1, "INVALIDATED", {}), (2, "RESTORED", {"observed_eligibility_hash": HB})),
        CONSISTENCY_SQLSTATE, "msg:未回到冻结值"),
    "res_bad_version": ("[P] RESTORED 的观测版本没回到冻结版本被拒",
        build_with(lambda c, w: (ev(c, w, 1, "INVALIDATED"),
                                 ev(c, w, 2, "RESTORED", observed_version_id=w["v2"]))),
        CONSISTENCY_SQLSTATE, "msg:未回到冻结值"),
    "seq_zero": ("[P] seq=0 被拒", seq_of((0, "INVALIDATED", {})),
        CONSISTENCY_SQLSTATE, "ck_export_validity_events_seq_positive"),
    "transition_bad": ("[P] transition 取值域外被拒", seq_of((1, "PAUSED", {})),
        CONSISTENCY_SQLSTATE, "ck_export_validity_events_transition_valid"),
    "reason_bad": ("[P] reason 取值域外被拒", seq_of((1, "INVALIDATED", {"reason": "BOGUS"})),
        CONSISTENCY_SQLSTATE, "ck_export_validity_events_reason_valid"),
    "inv_no_reason": ("[P] INVALIDATED 无 reason 被拒（reason_paired 双向等价）",
        seq_of((1, "INVALIDATED", {"reason": None})),
        CONSISTENCY_SQLSTATE, "ck_export_validity_events_reason_paired"),
    "res_with_reason": ("[P] RESTORED 带 reason 被拒（reason_paired 双向等价）",
        seq_of((1, "RESTORED", {"reason": "VERSION_SUPERSEDED"})),
        CONSISTENCY_SQLSTATE, "ck_export_validity_events_reason_paired"),
    "scope_bad": ("[P] scope_kind 取值域外被拒", seq_of((1, "INVALIDATED", {"scope_kind": "ALL"})),
        CONSISTENCY_SQLSTATE, "ck_export_validity_events_scope_kind_valid"),
    "from_no_source": ("[P] FROM_VERSION 无 source_version_id 被拒",
        seq_of((1, "INVALIDATED", {"scope_kind": "FROM_VERSION"})),
        CONSISTENCY_SQLSTATE, "ck_export_validity_events_scope_source_paired"),
    "contents_with_source": ("[P] CONTENTS 带 source_version_id 被拒",
        build_with(lambda c, w: ev(c, w, 1, "INVALIDATED", source_version_id=w["v2"])),
        CONSISTENCY_SQLSTATE, "ck_export_validity_events_scope_source_paired"),
    "hash_short": ("[P] 观测指纹长度不是 64 被拒",
        seq_of((1, "INVALIDATED", {"observed_eligibility_hash": "abc"})),
        CONSISTENCY_SQLSTATE, "ck_export_validity_events_observed_hash_shape"),
    "hash_upper": ("[P] 观测指纹含大写被拒",
        seq_of((1, "INVALIDATED", {"observed_eligibility_hash": "A" * 64})),
        CONSISTENCY_SQLSTATE, "ck_export_validity_events_observed_hash_shape"),
    "uq_dup": ("[P] 同一导出行同 seq 重复被拒（并发追加的唯一防线）",
        seq_of((1, "INVALIDATED", {}), (1, "INVALIDATED", {})),
        UNIQUE_SQLSTATE, "uq_export_validity_events_export_item_id_seq"),
    "fk_item": ("[P] export_item_id 指向不存在的导出行被拒",
        build_with(lambda c, w: ev(c, w, 1, "INVALIDATED", export_item_id=rnd())),
        FK_SQLSTATE, "fk_export_validity_events_export_item_id_export_items"),
    "fk_observed": ("[P] observed_version_id 指向不存在的版本被拒",
        seq_of((1, "INVALIDATED", {"observed_version_id": rnd()})),
        FK_SQLSTATE, "fk_export_validity_events_observed_version_id_version_nodes"),
    "fk_source": ("[P] source_version_id 指向不存在的版本被拒",
        seq_of((1, "INVALIDATED", {"scope_kind": "FROM_VERSION", "source_version_id": rnd()})),
        FK_SQLSTATE, "fk_export_validity_events_source_version_id_version_nodes"),
    "fk_user": ("[P] created_by 指向不存在的用户被拒",
        seq_of((1, "INVALIDATED", {"created_by": rnd()})),
        FK_SQLSTATE, "fk_export_validity_events_created_by_users"),
    "imm_upd": ("[P] 事件表拒绝 UPDATE（23001）",
        mutate_event("UPDATE export_validity_events SET seq = seq WHERE export_item_id = :i"),
        IMMUTABLE_SQLSTATE, None),
    "imm_del": ("[P] 事件表拒绝 DELETE（23001）",
        mutate_event("DELETE FROM export_validity_events WHERE export_item_id = :i"),
        IMMUTABLE_SQLSTATE, None),
    "item_upd": ("[P] export_items 仍拒绝 UPDATE（本包零改动）",
        mutate_export_item("UPDATE export_items SET zip_byte_size = 2 WHERE id = :i"),
        IMMUTABLE_SQLSTATE, None),
    "item_del": ("[P] export_items 仍拒绝 DELETE（本包零改动）",
        mutate_export_item("DELETE FROM export_items WHERE id = :i"),
        IMMUTABLE_SQLSTATE, None),
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
    if got is None:
        return False, f"实际=放行（期望被拒 {want_state}）"
    ok = got[0] == want_state
    if ok and want_constraint is not None:
        if want_constraint.startswith("msg:"):
            ok = want_constraint[4:] in got[2]
        else:
            ok = got[1] == want_constraint
    return ok, f"实际={got[:2] + (got[2].splitlines()[0][:70],)} 期望=({want_state}, {want_constraint})"


# --- T-EXP-01 证据版（正向与反向共用同一份实现）---------------------------------------
def texp01(conn, tamper=None):
    # 在回滚事务里造 export_items 行，追加 3 条事件，前后比对被引用行的 xmin / ctid / md5。
    sp = conn.begin_nested()
    try:
        w = mk_world(conn)
        s0 = snap(conn, w["item"])
        ev(conn, w, 1, "INVALIDATED")
        if tamper is not None:
            tamper(conn, w)
        s1 = snap(conn, w["item"])
        ev(conn, w, 2, "RESTORED")
        s2 = snap(conn, w["item"])
        ev(conn, w, 3, "INVALIDATED", reason="ASSET_CHANGED", observed_version_id=w["v1"],
           observed_eligibility_hash=HC)
        s3 = snap(conn, w["item"])
        events = conn.execute(
            text("SELECT count(*) FROM export_validity_events WHERE export_item_id = :i"),
            {"i": w["item"]},
        ).scalar_one()
        upd = attempt(conn, lambda c: c.execute(
            text("UPDATE export_items SET zip_byte_size = zip_byte_size WHERE id = :i"),
            {"i": w["item"]}))
        dele = attempt(conn, lambda c: c.execute(
            text("DELETE FROM export_items WHERE id = :i"), {"i": w["item"]}))
        s4 = snap(conn, w["item"])
        stable = s0 == s1 == s2 == s3 == s4 and s0 is not None
        rejected = upd is not None and upd[0] == IMMUTABLE_SQLSTATE and (
            dele is not None and dele[0] == IMMUTABLE_SQLSTATE)
        detail = (f"events={events} xmin={s0[0] if s0 else None}->{s3[0] if s3 else None}"
                  f" ctid={s0[2] if s0 else None}->{s3[2] if s3 else None}"
                  f" md5_equal={bool(s0 and s3 and s0[1] == s3[1])}"
                  f" 快照全程相等={stable} 直接UPDATE被拒={upd[0] if upd else '放行'}"
                  f" 直接DELETE被拒={dele[0] if dele else '放行'}")
        return events == 3 and stable and rejected, detail
    finally:
        sp.rollback()


def tamper_update(conn, w):
    conn.execute(text("DROP TRIGGER trg_export_items_immutable ON export_items"))
    conn.execute(text("UPDATE export_items SET zip_byte_size = 99 WHERE id = :i"), {"i": w["item"]})


# --- 判据（正向与反向共用同一份实现）------------------------------------------
def j_check_names(conn):
    actual = db_names(conn, TABLE, "c")
    return actual == EXPECTED_CHECK_NAMES, f"差集{sorted(actual ^ EXPECTED_CHECK_NAMES)}"


def j_all_names(conn):
    with_pk = db_names(conn, TABLE, "c") | db_names(conn, TABLE, "f") | db_names(conn, TABLE, "u")
    with_pk |= db_names(conn, TABLE, "p")
    return with_pk == EXPECTED_ALL_NAMES, f"差集{sorted(with_pk ^ EXPECTED_ALL_NAMES)}"


def j_domains(conn):
    bad = []
    for name, values in EXPECTED_DOMAINS:
        got = set(re.findall(r"'([A-Z_]+)'", constraint_def(conn, name) or ""))
        if got != values:
            bad.append((name, sorted(got)))
    return not bad, f"偏差{bad}"


def j_fks(conn):
    problems = []
    for name, cols, ref, refcols in EXPECTED_FKS:
        row = conn.execute(
            text(
                "SELECT confrelid::regclass::text, confdeltype, convalidated,"
                " (SELECT array_agg(a.attname ORDER BY k.ord) FROM unnest(c.conkey)"
                "   WITH ORDINALITY AS k(attnum, ord) JOIN pg_attribute a"
                "   ON a.attrelid = c.conrelid AND a.attnum = k.attnum),"
                " (SELECT array_agg(a.attname ORDER BY k.ord) FROM unnest(c.confkey)"
                "   WITH ORDINALITY AS k(attnum, ord) JOIN pg_attribute a"
                "   ON a.attrelid = c.confrelid AND a.attnum = k.attnum)"
                " FROM pg_constraint c WHERE c.conname = :n AND c.contype = 'f'"
                " AND c.conrelid = CAST(:t AS regclass)"
            ),
            {"n": name, "t": TABLE},
        ).first()
        want = (ref, "r", True, cols, refcols)
        if row is None or tuple(row) != want:
            problems.append((name, tuple(row) if row else None))
    extra = db_names(conn, TABLE, "f") - {f[0] for f in EXPECTED_FKS}
    if extra:
        problems.append(("多余FK", sorted(extra)))
    return not problems, f"4 条 FK 全部 (被引用表, RESTRICT, 已校验, 列) 一致" if not problems else f"偏差{problems}"


def j_uniques(conn):
    got = {}
    for row in conn.execute(
        text(
            "SELECT c.conname, array_agg(a.attname ORDER BY k.ord)"
            " FROM pg_constraint c"
            " CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)"
            " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum"
            " WHERE c.contype = 'u' AND c.conrelid = CAST(:t AS regclass) GROUP BY c.conname"
        ),
        {"t": TABLE},
    ):
        got[row[0]] = list(row[1])
    return got == dict(EXPECTED_UNIQUES), f"实际{got}"


def j_indexes(conn):
    got = {r[0] for r in conn.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename = :t"),
        {"t": TABLE})}
    return got == EXPECTED_INDEX_NAMES, f"实际{sorted(got)}"


def j_triggers(conn):
    rows = conn.execute(
        text(
            "SELECT t.tgname, p.proname, t.tgtype::int, t.tgdeferrable, t.tgenabled"
            " FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid"
            " WHERE t.tgrelid = CAST(:t AS regclass) AND NOT t.tgisinternal"
        ),
        {"t": TABLE},
    ).all()
    actual = {r[0]: r for r in rows}
    problems = []
    if set(actual) != {t[0] for t in EXPECTED_TRIGGERS}:
        problems.append(f"名集合差{sorted(set(actual) ^ {t[0] for t in EXPECTED_TRIGGERS})}")
    for name, func, tgtype in EXPECTED_TRIGGERS:
        r = actual.get(name)
        if r is not None and (r[1], r[2], bool(r[3]), r[4]) != (func, tgtype, False, "O"):
            problems.append(f"{name} 实际{tuple(r)[1:]}")
    return not problems, f"{problems or '2 个全部一致（函数 / tgtype / 非延迟 / 启用）'}"


def j_migration_imports(source):
    # 迁移只允许 import rednote.modules.versions.impact.ddl；不得 import types / models。
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("rednote"):
            found.append(f"{node.module}:{','.join(a.name for a in node.names)}")
        elif isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if a.name.startswith("rednote"))
    bad = [f for f in found if f != "rednote.modules.versions.impact:ddl"]
    return bool(found) and not bad, f"rednote 导入={sorted(set(found))} 越界={bad}"


def downgrade_code(source):
    dg = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef)
          and n.name == "downgrade"][0]
    # ast.unparse 丢掉注释与文档字符串，只剩真正执行的代码。
    return "\n".join(ast.unparse(n) for n in dg.body[1:])


def j_downgrade(source):
    code = downgrade_code(source)
    ok = ("OWN_FUNCTIONS" in code and "rednote_reject_mutation" not in code
          and "op.execute(_drop_function)" not in code
          and "op.drop_table('export_validity_events')" in code)
    return ok, f"执行代码行数={len(code.splitlines())} 含 OWN_FUNCTIONS={'OWN_FUNCTIONS' in code}"


def imports_of(source):
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            out.append(node.module or "")
        elif isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
    return out


def j_no_order_by_version_no(sources):
    hits = [n for n, s in sources.items()
            if re.search(r"ORDER\s+BY\s+(?:\w+\.)?version_no\b", s, re.IGNORECASE)]
    return not hits, f"命中文件{hits}"


def j_no_hashlib(sources):
    hits = [n for n, s in sources.items()
            if any(m == "hashlib" or m.startswith("hashlib.") for m in imports_of(s))]
    return not hits, f"命中文件{hits}"


def j_no_media_renditions(sources):
    hits = [n for n, s in sources.items() if "media_renditions" in s]
    return not hits, f"命中文件{hits}"


def j_exports_import_only_bridge(sources):
    hits = sorted(n for n, s in sources.items()
                  if any(m.startswith("rednote.modules.exports") for m in imports_of(s)))
    return hits == ["export_bridge.py"], f"import exports 的文件={hits}"


def j_pure_modules(sources):
    bad = []
    for n in ("types.py", "traversal.py"):
        for m in imports_of(sources[n]):
            if m.startswith("rednote.modules.") and m != "rednote.modules.versions.impact.types":
                bad.append((n, m))
            if m.startswith("sqlalchemy"):
                bad.append((n, m))
    return not bad, f"越界导入{bad}"


def read_sources():
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(IMPACT_DIR.rglob("*.py"))}


# --- 遍历纯函数夹具 -----------------------------------------------------------------
def U(n):
    return uuid.UUID(int=n)


def mk_fetch(edge_list, reverse=False, calls=None):
    def fetch(ids):
        if calls is not None:
            calls.append(list(ids))
        out = [e for e in edge_list if e.dependency in set(ids)]
        return list(reversed(out)) if reverse else out
    return fetch


E = impact_types.Edge


def chain_edges(n, start=1):
    # start <- start+1 <- ... ：parent=k+1 依赖 dependency=k
    return [E(U(start + i + 1), U(start + i), "P") for i in range(n)]


def g_chain(fn):
    r = fn(mk_fetch(chain_edges(3)), U(1))
    got = [(n.version_id, n.depth) for n in r.nodes]
    ok = got == [(U(2), 1), (U(3), 2), (U(4), 3)] and r.complete and r.truncation is None \
        and r.anomalies == () and r.max_depth_reached == 3 and r.round_trips == 4
    return ok, f"nodes={[(int(a), b) for a, b in got]} round_trips={r.round_trips} complete={r.complete}"


def g_diamond(fn):
    edges = [E(U(2), U(1), "A"), E(U(3), U(1), "A"), E(U(4), U(2), "A"), E(U(4), U(3), "A")]
    r = fn(mk_fetch(edges), U(1))
    got = [(int(n.version_id), n.depth) for n in r.nodes]
    ok = got == [(2, 1), (3, 1), (4, 2)] and r.anomalies == () and r.cycle_node_count == 0 \
        and r.round_trips == 3 and r.complete
    return ok, f"nodes={got} anomalies={r.anomalies} cycle_nodes={r.cycle_node_count}"


def g_cycle(fn):
    edges = [E(U(2), U(1), "A"), E(U(3), U(2), "A"), E(U(2), U(3), "A")]
    r = fn(mk_fetch(edges), U(1))
    ok = r.anomalies == ("DEPENDENCY_CYCLE",) and r.cycle_node_count == 2 and r.complete \
        and [int(n.version_id) for n in r.nodes] == [2, 3]
    return ok, f"anomalies={r.anomalies} cycle_nodes={r.cycle_node_count} complete={r.complete}"


def g_cycle_through_start(fn):
    edges = [E(U(2), U(1), "A"), E(U(1), U(2), "A")]
    r = fn(mk_fetch(edges), U(1))
    ok = r.anomalies == ("DEPENDENCY_CYCLE",) and r.complete \
        and [int(n.version_id) for n in r.nodes] == [2]
    return ok, f"anomalies={r.anomalies} nodes={[int(n.version_id) for n in r.nodes]}"


def g_cycle_innocent(fn):
    # 1 <- 2 <- 3 <-> 4：环外的 1 / 2 不算环上节点。
    edges = [E(U(2), U(1), "A"), E(U(3), U(2), "A"), E(U(4), U(3), "A"), E(U(3), U(4), "A")]
    r = fn(mk_fetch(edges), U(1))
    ok = r.anomalies == ("DEPENDENCY_CYCLE",) and r.cycle_node_count == 2
    return ok, f"cycle_nodes={r.cycle_node_count}（期望只算 3 / 4 两个）"


def g_depth_limit(fn):
    r = fn(mk_fetch(chain_edges(6)), U(1), max_depth=3)
    exact = fn(mk_fetch(chain_edges(6)), U(1), max_depth=6)
    ok = [int(n.version_id) for n in r.nodes] == [2, 3, 4] and not r.complete \
        and r.truncation == "DEPTH_LIMIT" and r.max_depth_reached == 3 \
        and exact.complete and exact.truncation is None and len(exact.nodes) == 6
    return ok, (f"超限: nodes={[int(n.version_id) for n in r.nodes]} complete={r.complete}"
                f" truncation={r.truncation}；恰好走完: complete={exact.complete}")


def g_node_limit(fn):
    star = [E(U(n), U(1), "A") for n in range(2, 12)]
    r = fn(mk_fetch(star), U(1), max_nodes=4)
    rr = fn(mk_fetch(star, reverse=True), U(1), max_nodes=4)
    exact = fn(mk_fetch(star), U(1), max_nodes=10)
    ok = [int(n.version_id) for n in r.nodes] == [2, 3, 4, 5] and not r.complete \
        and r.truncation == "NODE_LIMIT" and r == rr \
        and exact.complete and len(exact.nodes) == 10
    return ok, (f"超限前缀={[int(n.version_id) for n in r.nodes]} truncation={r.truncation}"
                f" 边序反转后结果相同={r == rr}；恰好 10 个: complete={exact.complete}")


def g_purposes(fn):
    edges = [E(U(2), U(1), "ZED"), E(U(2), U(1), "ALPHA"), E(U(2), U(1), "ALPHA"),
             E(U(3), U(2), "ONLY")]
    r = fn(mk_fetch(edges), U(1))
    by = {int(n.version_id): n.purposes for n in r.nodes}
    ok = by == {2: ("ALPHA", "ZED"), 3: ("ONLY",)}
    return ok, f"purposes={by}（purpose 不参与过滤，排序去重透出）"


def g_defaults(fn):
    ok12 = fn(mk_fetch(chain_edges(12)), U(1))
    ok13 = fn(mk_fetch(chain_edges(13)), U(1))
    ok = impact_types.MAX_TRAVERSAL_DEPTH == 12 and impact_types.MAX_TRAVERSAL_NODES == 2000 \
        and ok12.complete and len(ok12.nodes) == 12 \
        and not ok13.complete and ok13.truncation == "DEPTH_LIMIT" and len(ok13.nodes) == 12
    return ok, (f"默认 12 / 2000；链 12: complete={ok12.complete}；"
                f"链 13: complete={ok13.complete} truncation={ok13.truncation}")


def g_batching(fn):
    calls = []
    edges = [E(U(n), U(1), "A") for n in range(2, 12)] + [E(U(100), U(2), "A")]
    r = fn(mk_fetch(edges, calls=calls), U(1))
    ok = r.round_trips == len(calls) == 3 and [len(c) for c in calls] == [1, 10, 1]
    return ok, f"round_trips={r.round_trips} 每层 frontier 大小={[len(c) for c in calls]}（按层批量取，不逐点取）"


def g_e3_chain(fn):
    v1, v2, v3, prompt, baseline = U(11), U(12), U(13), U(1), U(2)
    edges = [E(v1, prompt, "GENERATION_PROMPT"), E(v1, baseline, "GENERATION_BASELINE"),
             E(v2, v1, "CONTENT_REVISION"), E(v3, v2, "CONTENT_REVISION")]
    r = fn(mk_fetch(edges), baseline)
    p = fn(mk_fetch(edges), prompt)
    got = [(int(n.version_id), n.depth, n.purposes) for n in r.nodes]
    want = [(11, 1, ("GENERATION_BASELINE",)), (12, 2, ("CONTENT_REVISION",)),
            (13, 3, ("CONTENT_REVISION",))]
    ok = got == want and [int(n.version_id) for n in p.nodes] == [11, 12, 13] and r.complete
    return ok, f"从 BASELINE: {got}；从 PROMPT: {[int(n.version_id) for n in p.nodes]}"


GRAPH_CASES = (
    ("[G] 链式分层 BFS：深度 1 / 2 / 3、按层 4 次取数、完整", g_chain),
    ("[G] 菱形汇聚：节点只出现一次、深度取最短、无环误报", g_diamond),
    ("[G] 环（2 <-> 3）：只标 DEPENDENCY_CYCLE，不抛错，complete=true", g_cycle),
    ("[G] 经过起点的环：只标 DEPENDENCY_CYCLE，不抛错", g_cycle_through_start),
    ("[G] 环下游 / 上游的无辜节点不计入环上节点数", g_cycle_innocent),
    ("[G] 深度上限：超限 complete=false + DEPTH_LIMIT + 确定前缀；恰好走完不误报", g_depth_limit),
    ("[G] 节点上限：超限返回按 id 排序的确定前缀（与边序无关）；恰好上限不误报", g_node_limit),
    ("[G] purpose 不过滤、排序去重原样透出，起点不在结果里", g_purposes),
    ("[G] 默认上限 12 / 2000 生效（链 12 完整，链 13 被深度截断）", g_defaults),
    ("[G] 每层批量取数（frontier 一次一批，round_trips = 层数 + 1）", g_batching),
    ("[G] E3 链：从 BASELINE / PROMPT 沿 CONTENT_REVISION 回溯到最新版本", g_e3_chain),
)


def run_graph(fn):
    results = []
    for label, case in GRAPH_CASES:
        try:
            ok, detail = case(fn)
        except Exception as exc:
            ok, detail = False, f"抛出 {type(exc).__name__}: {exc}"
        results.append((label, ok, detail))
    return results


def bad_traverse_raises_on_cycle(fetch, start, **kw):
    # 错误实现：发现环就抛错。
    r = impact_traversal.traverse_dependents(fetch, start, **kw)
    if r.anomalies:
        raise RuntimeError("检测到环，抛错")
    return r


def bad_traverse_no_limits(fetch, start, **kw):
    # 错误实现：忽略深度 / 节点上限。
    return impact_traversal.traverse_dependents(
        fetch, start, max_depth=10 ** 6, max_nodes=10 ** 6)


def reverse(label, judge):
    # 反向验证：事务内制造坏状态，同一判据必须报红；无条件 rollback。
    with engine.connect() as conn:
        try:
            judge["mutate"](conn)
            ok, detail = judge["judge"](conn)
        finally:
            conn.rollback()
    emit(not ok, label, f"坏状态下判据报红={not ok}（{detail[:200]}）")


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
    emit(has_alembic_table and revision_is_at_least(current, WP34_MIGRATION_REVISION),
         "[M] 迁移 head 不早于 WP34 引入点（真 CHECK，不 SKIP）",
         f"current={current} 引入点={WP34_MIGRATION_REVISION}"
         f"{'' if has_alembic_table else '；目标库无 alembic_version，先 alembic upgrade head'}")

source = MIGRATION_FILE.read_text(encoding="utf-8")
ok, detail = j_migration_imports(source)
emit(ok, "[M] 迁移只 import versions.impact.ddl，不 import types / models", detail)
emit("'INVALIDATED', 'RESTORED'" in source and "'ASSET_CHANGED', 'VERSION_SUPERSEDED'" in source
     and "'CONTENTS', 'FROM_VERSION'" in source,
     "[M] 迁移里取值域是化石字面量", "三组 IN 列表字面量都在迁移源码里")
emit(f"down_revision: str | Sequence[str] | None = '{WP34_DOWN_REVISION}'" in source
     and f"revision: str = '{WP34_MIGRATION_REVISION}'" in source,
     "[M] revision / down_revision 指向 WP32 迁移", f"{WP34_MIGRATION_REVISION} <- {WP34_DOWN_REVISION}")
ok, detail = j_downgrade(source)
emit(ok, "[M] downgrade 只删 OWN_FUNCTIONS，执行代码里不点名 rednote_reject_mutation", detail)
emit(tuple(impact_ddl.OWN_FUNCTIONS) == OWN_FUNCTIONS
     and "rednote_reject_mutation" not in impact_ddl.OWN_FUNCTIONS,
     "[M] ddl.OWN_FUNCTIONS 恰为本包 1 个函数，不含 rednote_reject_mutation",
     f"{impact_ddl.OWN_FUNCTIONS}")

# --- S：结构 -----------------------------------------------------------------
model_table = impact_models.Base.metadata.tables[TABLE]
with engine.connect() as conn:
    n = conn.execute(
        text(
            "SELECT count(*) FROM information_schema.tables"
            " WHERE table_schema='public' AND table_name = :n"
        ),
        {"n": TABLE},
    ).scalar_one()
    emit(n == 1, "[S] 本包 1 张表存在", f"{n}/1（缺则先跑 alembic upgrade head；本脚本不建表）")
    emit(set(impact_models.OWN_TABLES) == {TABLE} == set(impact_models.IMMUTABLE_TABLES),
         "[S] 模型声明的自有表 / 不可变表恰为 export_validity_events",
         f"{impact_models.OWN_TABLES} {impact_models.IMMUTABLE_TABLES}")

    rows = conn.execute(
        text(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull,"
            " pg_get_expr(d.adbin, d.adrelid)"
            " FROM pg_attribute a LEFT JOIN pg_attrdef d"
            " ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
            " WHERE a.attrelid = CAST(:t AS regclass) AND a.attnum > 0"
            " AND NOT a.attisdropped"
        ),
        {"t": TABLE},
    ).all()
    actual = {r[0]: (r[1], bool(r[2]), r[3]) for r in rows}
    model_cols = {c.name for c in model_table.columns}
    spec_names = {s[0] for s in EXPECTED_COLUMNS}
    emit(set(actual) == model_cols == spec_names and len(actual) == len(EXPECTED_COLUMNS),
         "[S] export_validity_events 列名集合与模型 / 草案一致",
         f"库 {len(actual)} 列 / 期望 {len(EXPECTED_COLUMNS)} 列 差集 {sorted(set(actual) ^ spec_names)}")
    for name, typ, nullable, default in EXPECTED_COLUMNS:
        got = actual.get(name)
        emit(got == (typ, nullable, default), f"[S] {TABLE}.{name}",
             f"实际 {got} 期望 {(typ, nullable, default)}")

    model_all = {c.name for c in model_table.constraints}
    ok, detail = j_check_names(conn)
    emit(ok and db_names(conn, TABLE, "c") == {c.name for c in model_table.constraints
         if c.__class__.__name__ == "CheckConstraint"},
         "[S] CHECK 名集合：库 = 草案 = 模型（7 条）", detail)
    ok, detail = j_all_names(conn)
    emit(ok and model_all == EXPECTED_ALL_NAMES and len(EXPECTED_ALL_NAMES) == 13,
         "[S] 全部 13 个约束名（7 CHECK + 4 FK + PK + UQ）库 = 草案 = 模型", detail)
    emit(db_names(conn, TABLE, "p") == {"pk_export_validity_events"},
         "[S] 主键 pk_export_validity_events", f"{sorted(db_names(conn, TABLE, 'p'))}")
    seq_def = constraint_def(conn, "ck_export_validity_events_seq_positive")
    hash_def = constraint_def(conn, "ck_export_validity_events_observed_hash_shape") or ""
    paired_def = constraint_def(conn, "ck_export_validity_events_reason_paired") or ""
    scope_def = constraint_def(conn, "ck_export_validity_events_scope_source_paired") or ""
    emit(seq_def == "CHECK ((seq >= 1))", "[S] seq_positive 定义", f"{seq_def}")
    emit("= 64" in hash_def and "^[0-9a-f]+$" in hash_def, "[S] observed_hash_shape 定义（64 位小写十六进制）", f"{hash_def}")
    emit("'INVALIDATED'" in paired_def and "reason IS NOT NULL" in paired_def,
         "[S] reason_paired 定义（INVALIDATED 与 reason 非空双向等价）", f"{paired_def}")
    emit("'FROM_VERSION'" in scope_def and "source_version_id IS NOT NULL" in scope_def,
         "[S] scope_source_paired 定义（FROM_VERSION 与 source_version_id 非空双向等价）", f"{scope_def}")
    ok, detail = j_domains(conn)
    emit(ok, "[S] transition / reason / scope_kind 取值域集合（与化石字面量同值）", detail)
    emit([set(impact_types.TRANSITIONS), set(impact_types.EVENT_REASONS),
          set(impact_types.SCOPE_KINDS)] == [d[1] for d in EXPECTED_DOMAINS],
         "[S] 应用层取值域常量与迁移化石字面量一致", "三组一致")

    ok, detail = j_fks(conn)
    emit(ok, "[S] 4 条 FK 逐条点名（含 export_item_id -> export_items.id、ON DELETE RESTRICT、已校验）", detail)
    row = conn.execute(
        text(
            "SELECT confrelid::regclass::text, confdeltype FROM pg_constraint"
            " WHERE conname = 'fk_export_validity_events_export_item_id_export_items'"
        )
    ).first()
    emit(row is not None and tuple(row) == ("export_items", "r"),
         "[S] 指向 export_items 的 FK 名与 ON DELETE RESTRICT（事件引用的导出行不可级联删除）",
         f"{tuple(row) if row else None}")
    inbound = [r[0] for r in conn.execute(
        text("SELECT conname FROM pg_constraint WHERE contype = 'f'"
             " AND confrelid = CAST('export_items' AS regclass) AND conrelid = CAST(:t AS regclass)"),
        {"t": TABLE})]
    emit(len(inbound) == 1, "[S] 事件表对 export_items 只有 1 条出向 FK（export_items 零改动）", f"{inbound}")
    model_fks = {c.name for c in model_table.constraints
                 if c.__class__.__name__ == "ForeignKeyConstraint"}
    emit(model_fks == db_names(conn, TABLE, "f"), "[S] FK 全集与模型一致（无多余）", f"{sorted(model_fks)}")
    ok, detail = j_uniques(conn)
    emit(ok, "[S] 唯一约束 UQ(export_item_id, seq) 且无多余", detail)
    ok, detail = j_indexes(conn)
    emit(ok, "[S] 索引全集 = 主键 + UQ 索引（不另建降序索引）", detail)
    emit(not model_table.indexes, "[S] 模型未声明额外索引", f"{sorted(i.name for i in model_table.indexes)}")

    ok, detail = j_triggers(conn)
    emit(ok, "[S] 2 个触发器（不可变 BEFORE UPDATE OR DELETE + 追加一致性 AFTER INSERT）名 / 函数 / tgtype / 非延迟", detail)
    emit(set(impact_ddl.trigger_names()) == {t[0] for t in EXPECTED_TRIGGERS},
         "[S] ddl.trigger_names() 与化石清单一致", f"{sorted(impact_ddl.trigger_names())}")
    for fn in OWN_FUNCTIONS:
        frow = conn.execute(
            text(
                "SELECT count(*), min(l.lanname), min(format_type(p.prorettype, NULL)),"
                " bool_and(position('首条事件必须是' IN p.prosrc) > 0)"
                " FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang WHERE p.proname = :f"
            ),
            {"f": fn},
        ).first()
        emit(tuple(frow) == (1, "plpgsql", "trigger", True), f"[S] 自有函数存在且是 plpgsql 触发器函数 {fn}",
             f"{tuple(frow)}")
    too_long = [
        row[0]
        for row in conn.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE length(conname) > :m"
                " AND conrelid = CAST(:t AS regclass)"
            ),
            {"m": MAX_IDENTIFIER_BYTES, "t": TABLE},
        )
    ]
    emit(not too_long, "[S] 全部约束名在 63 字节内（不会被静默截断）", f"超限 {too_long}")

# --- P：行为探针（独立连接，无条件 rollback）-----------------------------------------
with engine.connect() as conn:
    try:
        for key in PROBES:
            ok, detail = run_probe(conn, key)
            emit(ok, PROBES[key][0], detail)
    finally:
        conn.rollback()

# --- T：T-EXP-01 证据版 -------------------------------------------------------------
with engine.connect() as conn:
    try:
        ok, detail = texp01(conn)
        emit(ok, "[T] ★ T-EXP-01：INSERT 3 条事件前后，被引用 export_items 行 xmin / ctid / 整行 md5 全程不变，"
             "直接 UPDATE / DELETE 仍被 23001 拒绝", detail)
        trg = conn.execute(
            text(
                "SELECT count(*) FROM pg_trigger WHERE tgrelid = CAST('export_items' AS regclass)"
                " AND tgname = 'trg_export_items_immutable' AND (tgtype & 2) > 0 AND (tgtype & 16) > 0"
                " AND NOT tgisinternal"
            )
        ).scalar_one()
        emit(trg == 1, "[T] T-EXP-01 前提：export_items 上存在 BEFORE UPDATE 的不可变触发器", f"{trg} 个")
    finally:
        conn.rollback()

# --- X：跨包共存 --------------------------------------------------------------
with engine.connect() as conn:
    n = conn.execute(text("SELECT count(*) FROM pg_proc WHERE proname='rednote_reject_mutation'")).scalar_one()
    emit(n == 1, "[X] 跨包共用不可变函数 rednote_reject_mutation() 仍在", f"{n} 个")
    used = conn.execute(
        text(
            "SELECT count(*) FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid"
            " WHERE p.proname = 'rednote_reject_mutation' AND NOT t.tgisinternal"
        )
    ).scalar_one()
    emit(used >= 2, "[X] 共用函数被多张不可变表引用（含本包事件表与 export_items）", f"{used} 个触发器")

# --- R：路由 ------------------------------------------------------------------
from rednote.main import create_app

schema = create_app().openapi()
actual_routes = set()
tags_ok = True
for path, item in schema["paths"].items():
    for method, op in item.items():
        if op.get("tags") == ["versions-impact"]:
            actual_routes.add((path, method, op.get("operationId")))
emit(actual_routes == EXPECTED_ROUTES,
     "[R] 本包 5 个 operation 的 path / method / operationId 逐条点名对账",
     f"多 {sorted(actual_routes - EXPECTED_ROUTES)} 少 {sorted(EXPECTED_ROUTES - actual_routes)}")
emit(len({r[0] for r in actual_routes}) == 5 and len(actual_routes) == 5
     and len({r[2] for r in actual_routes}) == 5,
     "[R] 5 个 path / 5 个 operation / operationId 互不重复",
     f"path {len({r[0] for r in actual_routes})} op {len(actual_routes)}")
emit(sum(1 for r in actual_routes if r[1] == "post") == 1,
     "[R] 只有 1 个写 operation（sweeps），其余 4 个只读", f"POST {sum(1 for r in actual_routes if r[1] == 'post')}")

# --- G：遍历纯函数（不连库）---------------------------------------------------------
for label, ok, detail in run_graph(impact_traversal.traverse_dependents):
    emit(ok, label, detail)
ok = impact_traversal.find_cycle_nodes([E(U(2), U(1), "A"), E(U(3), U(1), "A"), E(U(4), U(2), "A"),
                                        E(U(4), U(3), "A")], {U(1), U(2), U(3), U(4)}) == 0
emit(ok, "[G] find_cycle_nodes：菱形 0 个环上节点", "0")

# --- C：静态源码守卫 ------------------------------------------------------------
sources = read_sources()
emit(IMPACT_FILES <= set(sources), "[C] impact 包文件齐全（以磁盘为准的 13 个必有文件）",
     f"磁盘 {sorted(sources)} 缺 {sorted(IMPACT_FILES - set(sources))}")
ok, detail = j_no_order_by_version_no(sources)
emit(ok, "[C] impact 内不得出现 ORDER BY version_no（最大 seq 才是当前态，设计稿 §4.2）", detail)
ok, detail = j_no_hashlib(sources)
emit(ok, "[C] impact 内不得 import hashlib（指纹只经 export_bridge 取，不自算，设计稿 §5.4）", detail)
ok, detail = j_no_media_renditions(sources)
emit(ok, "[C] impact 内不得查 media_renditions（设计稿 §4.2 / §5.4）", detail)
ok, detail = j_exports_import_only_bridge(sources)
emit(ok, "[C] impact 内仅 export_bridge.py import rednote.modules.exports", detail)
ok, detail = j_pure_modules(sources)
emit(ok, "[C] types.py / traversal.py 不 import 其它 rednote.modules.* 与 sqlalchemy", detail)

# --- D：一次性临时库真跑 upgrade / downgrade / 再 upgrade --------------------------------
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
    rc, tail = alembic(tmp_url, "upgrade", WP34_MIGRATION_REVISION)
    emit(rc == 0, "[D] 临时库 alembic upgrade 到 c3f34c0de003 成功", f"rc={rc} {tail[-120:] if rc else ''}")

    def snapshot():
        with tmp_engine.connect() as c:
            funcs = {r[0] for r in c.execute(text(
                "SELECT proname FROM pg_proc WHERE proname = ANY(:n)"),
                {"n": list(OWN_FUNCTIONS) + ["rednote_reject_mutation"]})}
            tables = {r[0] for r in c.execute(text(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema='public' AND table_name = ANY(:n)"),
                {"n": [TABLE, "export_items"]})}
            trigs = {r[0] for r in c.execute(text(
                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname = ANY(:n)"),
                {"n": [t[0] for t in EXPECTED_TRIGGERS] + ["trg_export_items_immutable"]})}
            version = c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            return funcs, tables, trigs, version

    own_trigs = {t[0] for t in EXPECTED_TRIGGERS}
    funcs, tables, trigs, version = snapshot()
    emit(tables == {TABLE, "export_items"} and trigs == own_trigs | {"trg_export_items_immutable"}
         and funcs == set(OWN_FUNCTIONS) | {"rednote_reject_mutation"} and version == WP34_MIGRATION_REVISION,
         "[D] upgrade 后事件表 / 2 触发器 / 自有函数 / 共用函数齐全，version=c3f34c0de003",
         f"{sorted(funcs)} {sorted(tables)} {sorted(trigs)} {version}")
    rc, tail = alembic(tmp_url, "downgrade", WP34_DOWN_REVISION)
    emit(rc == 0, "[D] downgrade 到 WP32 引入点成功", f"rc={rc} {tail[-120:] if rc else ''}")
    funcs, tables, trigs, version = snapshot()
    emit(not (funcs & set(OWN_FUNCTIONS)) and TABLE not in tables and not (trigs & own_trigs)
         and version == WP34_DOWN_REVISION,
         "[D] downgrade 后事件表 / 2 触发器 / 自有函数都已摘除，version 回到 b2f32c0de002",
         f"函数 {sorted(funcs)} 表 {sorted(tables)} 触发器 {sorted(trigs)} {version}")
    emit("rednote_reject_mutation" in funcs, "[D] downgrade 后 rednote_reject_mutation() 仍在（不删跨包共用函数）",
         f"funcs={sorted(funcs)}")
    emit("export_items" in tables and "trg_export_items_immutable" in trigs,
         "[D] downgrade 后 export_items 及其不可变触发器原样保留（本包零改动）",
         f"{sorted(tables)} {sorted(trigs)}")
    rc, tail = alembic(tmp_url, "upgrade", WP34_MIGRATION_REVISION)
    funcs, tables, trigs, version = snapshot()
    emit(rc == 0 and TABLE in tables and own_trigs <= trigs and funcs >= set(OWN_FUNCTIONS)
         and version == WP34_MIGRATION_REVISION,
         "[D] 再次 upgrade 成功（downgrade 后可重新升级）", f"rc={rc} 表 {sorted(tables)} 触发器 {sorted(trigs)}")
    tmp_engine.dispose()
finally:
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{tmp_name}" WITH (FORCE)'))
        gone = c.execute(text("SELECT count(*) FROM pg_database WHERE datname = :n"), {"n": tmp_name}).scalar_one()
    emit(gone == 0, "[D] 临时库已 DROP", f"残留 {gone}")
    admin.dispose()


# --- V：反向验证（事务内制造坏状态，判据必须报红；全部 rollback，不改代码树）--------------
def m_drop_consistency_trigger(conn):
    conn.execute(text("DROP TRIGGER trg_export_validity_events_consistency ON export_validity_events"))


def m_noop_function(conn):
    conn.execute(text(
        "CREATE OR REPLACE FUNCTION rednote_check_export_validity_event() RETURNS trigger AS $f$"
        " BEGIN RETURN NULL; END; $f$ LANGUAGE plpgsql"))


def m_drop_immutable(conn):
    conn.execute(text("DROP TRIGGER trg_export_validity_events_immutable ON export_validity_events"))


def m_drop_reason_paired(conn):
    conn.execute(text("ALTER TABLE export_validity_events DROP CONSTRAINT ck_export_validity_events_reason_paired"))


def m_fk_cascade(conn):
    conn.execute(text("ALTER TABLE export_validity_events DROP CONSTRAINT"
                      " fk_export_validity_events_export_item_id_export_items"))
    conn.execute(text(
        "ALTER TABLE export_validity_events ADD CONSTRAINT"
        " fk_export_validity_events_export_item_id_export_items FOREIGN KEY (export_item_id)"
        " REFERENCES export_items (id) ON DELETE CASCADE"))


def m_drop_uq(conn):
    conn.execute(text("ALTER TABLE export_validity_events DROP CONSTRAINT"
                      " uq_export_validity_events_export_item_id_seq"))


def m_drop_item_immutable(conn):
    conn.execute(text("DROP TRIGGER trg_export_items_immutable ON export_items"))


def m_widen_transition(conn):
    conn.execute(text("ALTER TABLE export_validity_events DROP CONSTRAINT ck_export_validity_events_transition_valid"))
    conn.execute(text(
        "ALTER TABLE export_validity_events ADD CONSTRAINT ck_export_validity_events_transition_valid"
        " CHECK (transition IN ('INVALIDATED', 'RESTORED', 'PAUSED'))"))


reverse("[V1] 摘掉追加一致性触发器 ⇒ “seq 跳号被拒”探针报红",
        {"mutate": m_drop_consistency_trigger, "judge": lambda c: run_probe(c, "seq_gap")})
reverse("[V2] 摘掉追加一致性触发器 ⇒ 触发器结构判据报红",
        {"mutate": m_drop_consistency_trigger, "judge": j_triggers})
reverse("[V3] 把一致性函数换成空操作 ⇒ “INVALIDATED 观测指纹等于冻结指纹被拒”探针报红",
        {"mutate": m_noop_function, "judge": lambda c: run_probe(c, "inv_same_hash")})
reverse("[V4] 摘掉事件表不可变触发器 ⇒ “拒绝 UPDATE”探针报红",
        {"mutate": m_drop_immutable, "judge": lambda c: run_probe(c, "imm_upd")})
reverse("[V5] 摘掉 reason_paired CHECK ⇒ CHECK 名集合判据报红",
        {"mutate": m_drop_reason_paired, "judge": j_check_names})
reverse("[V6] 摘掉 reason_paired CHECK ⇒ “INVALIDATED 无 reason 被拒”探针报红",
        {"mutate": m_drop_reason_paired, "judge": lambda c: run_probe(c, "inv_no_reason")})
reverse("[V7] export_items 外键改成 ON DELETE CASCADE ⇒ FK 判据报红",
        {"mutate": m_fk_cascade, "judge": j_fks})
reverse("[V8] 摘掉 UQ(export_item_id, seq) ⇒ 唯一约束判据报红",
        {"mutate": m_drop_uq, "judge": j_uniques})
reverse("[V9] 摘掉 UQ(export_item_id, seq) ⇒ “同 seq 重复被拒（23505）”探针报红",
        {"mutate": m_drop_uq, "judge": lambda c: run_probe(c, "uq_dup")})
reverse("[V10] transition 的 CHECK 放宽成含 PAUSED ⇒ 取值域判据报红",
        {"mutate": m_widen_transition, "judge": j_domains})
reverse("[V11] 摘掉 export_items 不可变触发器 ⇒ T-EXP-01 判据报红（直接 UPDATE 不再被拒）",
        {"mutate": m_drop_item_immutable, "judge": texp01})
reverse("[V12] 追加事件之间对被引用行做了 UPDATE ⇒ T-EXP-01 的 xmin / ctid / md5 快照判据报红",
        {"mutate": lambda c: None, "judge": lambda c: texp01(c, tamper=tamper_update)})
for label, bad_fn in (
    ("[V13] 遍历实现遇环抛错 ⇒ 环判据报红", bad_traverse_raises_on_cycle),
    ("[V14] 遍历实现忽略深度 / 节点上限 ⇒ 上限判据报红", bad_traverse_no_limits),
):
    bad = [(lb, d) for lb, ok, d in run_graph(bad_fn) if not ok]
    emit(len(bad) > 0, label, f"坏实现下报红 {len(bad)} 条：{[b[0][:24] for b in bad]}")
bad_src = source + "\nfrom rednote.modules.versions.impact.types import TRANSITIONS\n"
ok, detail = j_migration_imports(bad_src)
emit(not ok, "[V15] 迁移源码混入 import impact.types ⇒ “不 import 取值域”判据报红",
     f"坏源码下判据报红={not ok}（{detail}）")
bad_src = source.replace("op.drop_table('export_validity_events')",
                         "op.drop_table('export_validity_events')\n    op.execute('DROP FUNCTION IF EXISTS rednote_reject_mutation()')")
ok, detail = j_downgrade(bad_src)
emit(not ok, "[V16] downgrade 里混入 DROP FUNCTION rednote_reject_mutation ⇒ downgrade 范围判据报红",
     f"坏源码下判据报红={not ok}")
bad = dict(sources)
bad["service.py"] = sources["service.py"] + "\nQ = 'SELECT 1 FROM x ORDER BY version_no DESC LIMIT 1'\n"
ok, detail = j_no_order_by_version_no(bad)
emit(not ok, "[V17] 注入 ORDER BY version_no ⇒ 静态守卫报红", f"坏源码下判据报红={not ok}（{detail}）")
bad = dict(sources)
bad["service.py"] = "import hashlib\n" + sources["service.py"]
ok, detail = j_no_hashlib(bad)
emit(not ok, "[V18] 注入 import hashlib ⇒ 静态守卫报红", f"坏源码下判据报红={not ok}（{detail}）")
bad = dict(sources)
bad["repository.py"] = sources["repository.py"] + "\nQ = 'SELECT * FROM media_renditions'\n"
ok, detail = j_no_media_renditions(bad)
emit(not ok, "[V19] 注入 media_renditions 查询 ⇒ 静态守卫报红", f"坏源码下判据报红={not ok}（{detail}）")
bad = dict(sources)
bad["service.py"] = "from rednote.modules.exports.service import ExportError\n" + sources["service.py"]
ok, detail = j_exports_import_only_bridge(bad)
emit(not ok, "[V20] service.py 越界 import exports ⇒ 接缝守卫报红", f"坏源码下判据报红={not ok}（{detail}）")

# --- Z：探针零残留 --------------------------------------------------------------
with engine.connect() as conn:
    n = conn.execute(
        text(
            "SELECT count(*) FROM export_validity_events e JOIN export_items x ON x.id = e.export_item_id"
            " JOIN projects p ON p.id = x.project_id WHERE p.name = :n"
        ),
        {"n": PROBE_PROJECT_NAME},
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 export_validity_events（探针项目）", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM export_items x JOIN projects p ON p.id = x.project_id WHERE p.name = :n"),
        {"n": PROBE_PROJECT_NAME},
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 export_items（探针项目）", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM version_nodes WHERE payload_hash LIKE :p"),
        {"p": PROBE_HASH_PREFIX + "%"},
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 version_nodes（探针）", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM blobs WHERE object_key LIKE :p"), {"p": PROBE_BLOB_PREFIX + "%"}
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 blobs / file_objects（探针）", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM users WHERE issuer = :iss"), {"iss": PROBE_ISSUER}
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 users（探针）", f"{n} 行")
    n = conn.execute(text("SELECT count(*) FROM export_validity_events")).scalar_one()
    emit(n == 0, "[Z] 事件表整体 0 行（探针全部回滚，没有任何提交）", f"{n} 行")
    emit(all(j(conn)[0] for j in (j_check_names, j_all_names, j_domains, j_fks, j_uniques,
                                  j_indexes, j_triggers)),
         "[Z] 反向验证后目标库结构原样（CHECK / 13 约束名 / 取值域 / FK / UQ / 索引 / 触发器）",
         "全部复原")
    ok, _ = texp01(conn)
    conn.rollback()
    emit(ok, "[Z] 反向验证后 export_items 不可变触发器仍在（T-EXP-01 判据再次通过）", "复原")

print(f"RESULT_EMITTED (期望 {EXPECTED_CHECKS} 条)")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def collect_wp34() -> tuple[Path, bool]:
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
        "# WP34 回退影响图 / 导出失效：事件表逐列 / 13 个约束名 / FK / 唯一约束 / 索引 /"
        " 2 个触发器 / 行为探针 / T-EXP-01 证据版 / 临时库 upgrade-downgrade 往返 /"
        " 路由对账 / 遍历纯函数 / 静态源码守卫 / 反向验证 + 探针零残留",
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
    target = EVIDENCE_DIR / "wp34-export-validity.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    complete = len(checks) >= EXPECTED_CHECKS
    exact = len(checks) == EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete and exact)


def main() -> int:
    path, ok = collect_wp34()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
