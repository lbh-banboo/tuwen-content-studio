#!/usr/bin/env python
"""WP31 证据采集脚本（试产关口 / 淘汰补产）。

产出的文件（落在 `evidence/m3/`）：

| 文件 | 内容 |
|---|---|
| `wp31-trial-gate.log` | 3 表逐列 / 类型 / 可空 / 默认；29 条 CHECK 逐名对账模型并逐条阴性探针；10 条 FK（ON DELETE RESTRICT、无 DEFERRABLE 点名）；5 个唯一约束 + 2 个部分唯一索引 WHERE + 其余索引；6 个触发器与 5 个自有函数，函数体迁移矩阵（MATRIX-BEGIN/END）与 `types.ROUND_TRANSITIONS` 逐边对账；状态迁移 5x4 全矩阵行为探针；不可变列 / write-once / DELETE / 决定不可变；到 GATE_PASSED 必有决定且反向；跨包共存（`rednote_reject_mutation` 与其它包触发器，含一次性临时库 upgrade -> downgrade -> upgrade 往返）；9 个路由 path/method/operationId；`trial_gate_status` 接缝签名 / 返回字段 / 纯读；13 条反向验证；探针零残留 |

## 目标库与迁移的关系

本包迁移已存在（revision `a1f31c0de001`，见 `WP31_MIGRATION_REVISION`），所以与 WP26 同款：
默认库 `rednote_verify_wp31` 是**迁移从空库建出**的（`alembic upgrade head`），
"迁移 head 不早于 WP31 引入点"是一条**真 CHECK**，不是 SKIP。库名可被环境变量
`REDNOTE_WP31_EVIDENCE_DATABASE_URL` 覆盖。重建办法：

    createdb rednote_verify_wp31 && psql -d rednote_verify_wp31 -c 'CREATE EXTENSION vector'
    REDNOTE_DATABASE_URL=postgresql+psycopg://rednote@localhost:5432/rednote_verify_wp31 \
        services/backend/.venv/bin/python -m alembic -c migrations/alembic.ini upgrade head

⚠️ **本脚本对目标库不建表、不改结构、不提交**：结构不全时报 FAIL 并提示先跑迁移；
自己建表再核对自己建的表是循环判据（恒绿）。所有探针各用独立连接 + 无条件 `rollback()`，
反向验证的 DDL 变异（换函数体 / 删触发器 / 删 CHECK / 改索引 / 改 FK）同样只活在回滚的事务里。
唯一"建出来又删掉"的东西是**一次性临时库** `<目标库>_tmp_<pid>`（只用于迁移往返，
`finally` 里 DROP；名字前缀硬校验，绝不会误删别的库）。

## 本包的结构性特点

- `trial_rounds` 是**有限可变**（只有 `state` / `cancel_reason` / `updated_at` / `lock_version` 能改），
  `state` 变化必须落在迁移矩阵的边上；矩阵在触发器函数体里由 `types.ROUND_TRANSITIONS` 生成，
  本脚本从函数体**反解析**再逐边对账。
- `trial_round_slots` 名额 write-once（`content_item_id` / `bound_at` 只能 NULL -> 值 一次）。
- `trial_gate_decisions` 不可变；"到 `GATE_PASSED` 必有决定行"与"决定存在 => 轮必须 `GATE_PASSED`"
  是**一对延迟约束触发器**，双向合成一个不变量。
- 取值域落库 CHECK 的只有 `kind` / `state` / `limit_source`，且迁移里是**化石字面量**（ISS-191），
  本脚本据此核对"迁移不从应用代码 import 取值域"。

## 探针自身踩过的坑（沿用 WP26/29/30 的教训）

1. **★ 期望条数必须实测，不能凭印象**：另加 `exact = len(checks) == EXPECTED_CHECKS` 并入 verdict。
2. **一致性触发器是延迟的**：必须显式 `SET CONSTRAINTS <本包三条> IMMEDIATE` 才能看到拒绝。
   ⚠️ 不能用 `SET CONSTRAINTS ALL IMMEDIATE`：会连带触发 `production_baselines` 的审批 / 无阻断
   延迟触发器，而探针造的基线没有审批。只点名本包三条，且按存在性过滤（反向验证删触发器后点名会炸）。
3. **GATE_PASSED 与决定行互为前提**（轮需要决定行、决定行需要轮已 GATE_PASSED）：造"已通过的轮"
   必须在 DEFERRED 下先插决定、再把轮推到 GATE_PASSED，最后才 IMMEDIATE；**先 IMMEDIATE 再造会自己卡死**。
4. **一旦 IMMEDIATE，之后的造数据语句也立刻校验**：需要"先造名额、再把轮取消"这类序列时，
   必须先 `force()` 把挂起的事件消化掉，否则取消轮后再 IMMEDIATE 会让早先挂起的"终态轮不得加名额"误报。
5. **CHECK 与触发器同为 23514，必须靠消息 / 约束名区分**：迁移矩阵拒绝看 `message_primary`，
   CHECK 看 `diag.constraint_name`。BEFORE 触发器先于 CHECK 执行，所以非法迁移永远先报触发器消息。
6. **`requested_quantity_positive` / `limit_applied_positive` 在逻辑上被其它 CHECK 蕴含**
   （`planned>=1` 且 `planned<=requested` 蕴含 `requested>=1`），无法单独触发；PG 按约束名字母序报第一条，
   探针对这两条接受"目标名或蕴含它的那条"。
7. **SQLAlchemy `text()` 会把 `:1` 之类当绑定参数**：JSON 字面量用 `jsonb_build_object`，不手写 `{"x":1}`。
8. **内联脚本只允许单行注释且不能出现三引号**（ISS-45），由 `check_evidence_scripts.py` 机械核对。
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
EXPECTED_CHECKS: int = 299

#: 目标库：迁移从空库建出。
DEFAULT_DB = "postgresql+psycopg://rednote@localhost:5432/rednote_verify_wp31"
DB_ENV = "REDNOTE_WP31_EVIDENCE_DATABASE_URL"

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

INSPECT_SCRIPT = r"""
# 内联脚本：只允许单行注释，且不能出现三引号（三引号会提前终止外层包裹，见 ISS-45）。

import ast
import inspect
import json
import os
import re
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from rednote.modules.planning.trial import ddl as tddl
from rednote.modules.planning.trial import models as tmodels
from rednote.modules.planning.trial import types as ttypes
from rednote.modules.review import ddl as review_ddl

EXPECTED_CHECKS = __EXPECTED__

DB = os.environ.get(
    "REDNOTE_WP31_EVIDENCE_DATABASE_URL",
    "postgresql+psycopg://rednote@localhost:5432/rednote_verify_wp31",
)
engine = create_engine(DB)
REPO = Path(os.environ["WP31_REPO_ROOT"])

WP31_MIGRATION_REVISION = "a1f31c0de001"
MIGRATION_FILE = REPO / "migrations" / "versions" / "20261021_0248_a1f31c0de001_wp31_trial_gate.py"

OWN_TABLES = tmodels.OWN_TABLES
ROUNDS, SLOTS, DECISIONS = OWN_TABLES
IMMUTABLE_SQLSTATE = "23001"
CONSISTENCY_SQLSTATE = "23514"
UNIQUE_SQLSTATE = "23505"
FK_SQLSTATE = "23503"
MAX_IDENTIFIER_BYTES = 63

PROBE_ISSUER = "wp31-probe"
PROBE_PROJECT_NAME = "WP31 探针项目"
PROBE_PROJECT_PREFIX = "WP31PROBE"

CONS_TRIGGERS = (
    tddl.ROUND_CONSISTENCY_TRIGGER,
    tddl.SLOT_CONSISTENCY_TRIGGER,
    tddl.DECISION_CONSISTENCY_TRIGGER,
)

# 手写的期望表（与模型 / 迁移**独立**抄一份，再与库对账）。
COLUMN_SPEC = {
    "trial_rounds": [
        ("id", "uuid", "NO", "gen_random_uuid()"),
        ("project_id", "uuid", "NO", "-"),
        ("round_no", "integer", "NO", "-"),
        ("kind", "character varying(12)", "NO", "-"),
        ("state", "character varying(16)", "NO", "'PLANNED'::character varying"),
        ("plan_version_id", "uuid", "NO", "-"),
        ("baseline_id", "uuid", "NO", "-"),
        ("source_round_id", "uuid", "YES", "-"),
        ("requested_quantity", "integer", "NO", "-"),
        ("planned_quantity", "integer", "NO", "-"),
        ("limit_applied", "integer", "NO", "-"),
        ("limit_source", "character varying(12)", "NO", "-"),
        ("plan_snapshot", "jsonb", "NO", "-"),
        ("cancel_reason", "character varying(500)", "YES", "-"),
        ("idempotency_key", "character varying(160)", "YES", "-"),
        ("request_hash", "character varying(64)", "YES", "-"),
        ("created_by", "uuid", "YES", "-"),
        ("created_at", "timestamp with time zone", "NO", "now()"),
        ("updated_at", "timestamp with time zone", "NO", "now()"),
        ("lock_version", "bigint", "NO", "1"),
    ],
    "trial_round_slots": [
        ("id", "uuid", "NO", "gen_random_uuid()"),
        ("round_id", "uuid", "NO", "-"),
        ("slot_no", "integer", "NO", "-"),
        ("scene_key", "character varying(200)", "NO", "-"),
        ("form_key", "character varying(200)", "YES", "-"),
        ("content_item_id", "uuid", "YES", "-"),
        ("bound_at", "timestamp with time zone", "YES", "-"),
        ("replaces_slot_id", "uuid", "YES", "-"),
        ("created_at", "timestamp with time zone", "NO", "now()"),
        ("updated_at", "timestamp with time zone", "NO", "now()"),
    ],
    "trial_gate_decisions": [
        ("id", "uuid", "NO", "gen_random_uuid()"),
        ("round_id", "uuid", "NO", "-"),
        ("approved_scene_keys", "jsonb", "NO", "-"),
        ("condition_results", "jsonb", "NO", "-"),
        ("statistics", "jsonb", "NO", "-"),
        ("basis", "jsonb", "NO", "-"),
        ("reason", "character varying(500)", "NO", "-"),
        ("decided_by", "uuid", "YES", "-"),
        ("decided_at", "timestamp with time zone", "NO", "now()"),
    ],
}

FK_SPEC = {
    "fk_trial_rounds_project_id_projects": "FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT",
    "fk_trial_rounds_plan_version_id_version_nodes": "FOREIGN KEY (plan_version_id) REFERENCES version_nodes(id) ON DELETE RESTRICT",
    "fk_trial_rounds_baseline_id_production_baselines": "FOREIGN KEY (baseline_id) REFERENCES production_baselines(id) ON DELETE RESTRICT",
    "fk_trial_rounds_source_round_id_trial_rounds": "FOREIGN KEY (source_round_id) REFERENCES trial_rounds(id) ON DELETE RESTRICT",
    "fk_trial_rounds_created_by_users": "FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT",
    "fk_trial_round_slots_round_id_trial_rounds": "FOREIGN KEY (round_id) REFERENCES trial_rounds(id) ON DELETE RESTRICT",
    "fk_trial_round_slots_content_item_id_content_items": "FOREIGN KEY (content_item_id) REFERENCES content_items(id) ON DELETE RESTRICT",
    "fk_trial_round_slots_replaces_slot_id_trial_round_slots": "FOREIGN KEY (replaces_slot_id) REFERENCES trial_round_slots(id) ON DELETE RESTRICT",
    "fk_trial_gate_decisions_round_id_trial_rounds": "FOREIGN KEY (round_id) REFERENCES trial_rounds(id) ON DELETE RESTRICT",
    "fk_trial_gate_decisions_decided_by_users": "FOREIGN KEY (decided_by) REFERENCES users(id) ON DELETE RESTRICT",
}

UQ_SPEC = {
    "uq_trial_rounds_project_id_round_no": "UNIQUE (project_id, round_no)",
    "uq_trial_round_slots_round_id_slot_no": "UNIQUE (round_id, slot_no)",
    "uq_trial_round_slots_content_item_id": "UNIQUE (content_item_id)",
    "uq_trial_round_slots_replaces_slot_id": "UNIQUE (replaces_slot_id)",
    "uq_trial_gate_decisions_round_id": "UNIQUE (round_id)",
}

PK_NAMES = ("pk_trial_rounds", "pk_trial_round_slots", "pk_trial_gate_decisions")

ALL_INDEX_NAMES = {
    "pk_trial_rounds", "pk_trial_round_slots", "pk_trial_gate_decisions",
    "uq_trial_rounds_project_id_round_no", "uq_trial_round_slots_round_id_slot_no",
    "uq_trial_round_slots_content_item_id", "uq_trial_round_slots_replaces_slot_id",
    "uq_trial_gate_decisions_round_id",
    "uq_trial_rounds_project_open", "uq_trial_rounds_project_idempotency",
    "ix_trial_rounds_source_round", "ix_trial_round_slots_round_scene",
}

TRIGGER_SPEC = {
    "trg_trial_rounds_transition_guard": (
        ROUNDS, ("BEFORE DELETE OR UPDATE", "FOR EACH ROW", "EXECUTE FUNCTION rednote_guard_trial_round_transition()"), False),
    "trg_trial_rounds_consistency": (
        ROUNDS, ("AFTER INSERT OR UPDATE", "FOR EACH ROW", "EXECUTE FUNCTION rednote_check_trial_round_consistency()"), True),
    "trg_trial_round_slots_guard": (
        SLOTS, ("BEFORE DELETE OR UPDATE", "FOR EACH ROW", "EXECUTE FUNCTION rednote_guard_trial_round_slot()"), False),
    "trg_trial_round_slots_consistency": (
        SLOTS, ("AFTER INSERT OR UPDATE", "FOR EACH ROW", "EXECUTE FUNCTION rednote_check_trial_round_slot_consistency()"), True),
    "trg_trial_gate_decisions_immutable": (
        DECISIONS, ("BEFORE DELETE OR UPDATE", "FOR EACH ROW", "EXECUTE FUNCTION rednote_reject_mutation()"), False),
    "trg_trial_gate_decisions_consistency": (
        DECISIONS, ("AFTER INSERT ON", "FOR EACH ROW", "EXECUTE FUNCTION rednote_check_trial_gate_decision_consistency()"), True),
}

ROUTE_SPEC = (
    ("GET", "/api/v1/projects/{projectId}/trial-entry-check",
     "get_api_v1_projects_projectId_trial-entry-check", "200"),
    ("POST", "/api/v1/projects/{projectId}/trial-rounds",
     "post_api_v1_projects_projectId_trial-rounds", "201"),
    ("GET", "/api/v1/projects/{projectId}/trial-rounds",
     "get_api_v1_projects_projectId_trial-rounds", "200"),
    ("GET", "/api/v1/projects/{projectId}/trial-rounds/{roundId}",
     "get_api_v1_projects_projectId_trial-rounds_roundId", "200"),
    ("POST", "/api/v1/projects/{projectId}/trial-rounds/{roundId}/transitions",
     "post_api_v1_projects_projectId_trial-rounds_roundId_transitions", "200"),
    ("PUT", "/api/v1/projects/{projectId}/trial-rounds/{roundId}/slots/{slotId}/content",
     "put_api_v1_projects_projectId_trial-rounds_roundId_slots_slotId_content", "200"),
    ("POST", "/api/v1/projects/{projectId}/trial-rounds/{roundId}/slots/{slotId}/replacements",
     "post_api_v1_projects_projectId_trial-rounds_roundId_slots_slotId_replacements", "201"),
    ("GET", "/api/v1/projects/{projectId}/trial-rounds/{roundId}/gate",
     "get_api_v1_projects_projectId_trial-rounds_roundId_gate", "200"),
    ("POST", "/api/v1/projects/{projectId}/trial-rounds/{roundId}/gate-decisions",
     "post_api_v1_projects_projectId_trial-rounds_roundId_gate-decisions", "201"),
)


def emit(ok, label, detail):
    print(f"CHECK {'OK  ' if ok else 'FAIL'} {label}: {detail}")


def sqlstate(exc):
    return getattr(exc.orig, "sqlstate", None)


@contextmanager
def tx(mutation=None):
    # 独立连接；无条件 rollback（不可变表的探针一旦提交就永远删不掉）。
    conn = engine.connect()
    try:
        conn.execute(text("SET lock_timeout = '5s'"))
        if mutation is not None:
            mutation(conn)
        yield conn
    finally:
        conn.rollback()
        conn.close()


def sql_set(conn, sql):
    return {row[0] for row in conn.execute(text(sql))}


def quoted_words(definition):
    return set(re.findall(r"'([A-Z_]+)'", definition))


# --- 造数据 -------------------------------------------------------------------
def ins(conn, table, row, jsonb=(), raw=None):
    raw = raw or {}
    cols = list(row) + list(raw)
    vals = [f"CAST(:{c} AS jsonb)" if c in jsonb else f":{c}" for c in row] + list(raw.values())
    params = {}
    for c, v in row.items():
        if c in jsonb:
            params[c] = json.dumps(v)
        elif isinstance(v, uuid.UUID):
            params[c] = str(v)
        else:
            params[c] = v
    conn.execute(
        text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(vals)})"),
        params,
    )
    return row.get("id")


def seed(conn):
    uid, brand, prod, proj, plan, base = (uuid.uuid4() for _ in range(6))
    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, display_name, role, status)"
            " VALUES (:u, :iss, :sub, 'WP31 探针', 'ADMIN', 'ACTIVE')"
        ),
        {"u": str(uid), "iss": PROBE_ISSUER, "sub": f"probe-{uid}"},
    )
    conn.execute(
        text("INSERT INTO brands (id, name, status) VALUES (:b, :n, 'ACTIVE')"),
        {"b": str(brand), "n": f"WP31 品牌 {str(brand)[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO products (id, brand_id, name, model_code, status)"
            " VALUES (:p, :b, :n, :c, 'ACTIVE')"
        ),
        {"p": str(prod), "b": str(brand), "n": f"WP31 产品 {str(prod)[:8]}",
         "c": f"WP31-{str(prod)[:8]}"},
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
    for node, kind in ((plan, "PLAN"), (base, "BASELINE")):
        conn.execute(
            text(
                "INSERT INTO version_nodes (id, entity_type, entity_id, project_id,"
                " revision_no, payload_hash, schema_version)"
                " VALUES (:n, :k, :e, :p, 1, :h, 1)"
            ),
            {"n": str(node), "k": kind, "e": str(proj), "p": str(proj),
             "h": f"h-{str(node)[:12]}"},
        )
    ins(conn, "production_baselines",
        {"id": base, "project_id": proj, "ruleset_version": "wp31-probe",
         "snapshot": {}, "change_reason": "wp31 probe"}, jsonb=("snapshot",))
    return {"user": uid, "project": proj, "plan": plan, "base": base, "n": 0, "sn": {}}


def mk_round(conn, s, **o):
    s["n"] += 1
    row = {
        "id": uuid.uuid4(), "project_id": s["project"], "round_no": s["n"],
        "kind": "TRIAL", "state": "PLANNED", "plan_version_id": s["plan"],
        "baseline_id": s["base"], "requested_quantity": 3, "planned_quantity": 3,
        "limit_applied": 10, "limit_source": "GLOBAL", "plan_snapshot": {},
        "created_by": s["user"],
    }
    row.update(o)
    if row["state"] == "CANCELLED" and "cancel_reason" not in row:
        row["cancel_reason"] = "wp31 probe"
    if row.get("idempotency_key") is not None and "request_hash" not in row:
        row["request_hash"] = "a" * 64
    ins(conn, ROUNDS, row, jsonb=("plan_snapshot",))
    return row["id"]


def mk_content(conn, project_id):
    cid = uuid.uuid4()
    ins(conn, "content_items", {"id": cid, "project_id": project_id})
    return cid


def mk_slot(conn, s, rid, **o):
    s["sn"][rid] = s["sn"].get(rid, 0) + 1
    row = {"id": uuid.uuid4(), "round_id": rid, "slot_no": s["sn"][rid], "scene_key": "s1"}
    row.update(o)
    raw = {}
    if row.get("content_item_id") is not None and "bound_at" not in o:
        raw["bound_at"] = "now()"
    ins(conn, SLOTS, row, raw=raw)
    return row["id"]


def mk_decision(conn, rid, **o):
    row = {
        "id": uuid.uuid4(), "round_id": rid, "approved_scene_keys": ["s1"],
        "condition_results": {}, "statistics": {}, "basis": [], "reason": "wp31 probe",
    }
    row.update(o)
    ins(conn, DECISIONS, row,
        jsonb=("approved_scene_keys", "condition_results", "statistics", "basis"))
    return row["id"]


def mk_passed_round(conn, s, keys=("s1", "s2"), basis=None, **over):
    # 在 DEFERRED 下：先插决定，再把轮推到 GATE_PASSED；调用方随后 force()。
    rid = mk_round(conn, s, state="REVIEWING", **over)
    mk_decision(conn, rid, approved_scene_keys=list(keys), basis=basis or [])
    conn.execute(text("UPDATE trial_rounds SET state='GATE_PASSED' WHERE id=:i"),
                 {"i": str(rid)})
    return rid


def force(conn):
    # 只点名本包三条延迟触发器（ALL 会连带触发基线的审批触发器，见 docstring 坑 2）。
    have = [
        row[0]
        for row in conn.execute(
            text("SELECT tgname FROM pg_trigger WHERE tgname = ANY(:n) AND NOT tgisinternal"),
            {"n": list(CONS_TRIGGERS)},
        )
    ]
    if have:
        conn.execute(text("SET CONSTRAINTS " + ", ".join(have) + " IMMEDIATE"))


def run_probe(setup, action, expect=None, msg=None, cname=None, force_after=False,
              mutation=None):
    # 返回 (ok, detail)。expect=None 表示这条写入必须被放行。
    conn = engine.connect()
    try:
        conn.execute(text("SET lock_timeout = '5s'"))
        if mutation is not None:
            mutation(conn)
        s = seed(conn)
        ctx = setup(conn, s) if setup else None
        sp = conn.begin_nested()
        code, got_msg, got_c = None, "", None
        try:
            action(conn, s, ctx)
            if force_after:
                force(conn)
            sp.commit()
        except DBAPIError as exc:
            code = sqlstate(exc)
            got_msg = str(exc.orig.diag.message_primary or "")
            got_c = exc.orig.diag.constraint_name
            sp.rollback()
        if expect is None:
            ok = code is None
        else:
            allowed = (cname,) if isinstance(cname, str) else (cname or ())
            ok = (
                code == expect
                and (msg is None or msg in got_msg)
                and (not allowed or got_c in allowed)
            )
        shown = got_msg[:48] if got_msg else ""
        return ok, f"sqlstate={code} 期望={expect} constraint={got_c} {shown}".rstrip()
    except Exception as exc:
        return False, f"探针异常 {type(exc).__name__}: {str(exc)[:120]}"
    finally:
        conn.rollback()
        conn.close()


def probe(label, setup, action, expect=None, **kw):
    ok, detail = run_probe(setup, action, expect, **kw)
    emit(ok, label, detail)
    return ok


def rev(label, judged):
    # 反向验证：把错误实现放进去，判据必须报红；判据仍绿说明判据失效。
    try:
        ok, detail = judged()
    except Exception as exc:
        ok, detail = False, f"判据异常 {type(exc).__name__}: {str(exc)[:100]}"
    if ok:
        emit(False, label, f"判据仍报绿，反向验证失效: {detail}")
    else:
        emit(True, label, f"判据报红（符合预期）: {detail[:140]}")


def upd(conn, sql, **p):
    conn.execute(text(sql), {k: str(v) for k, v in p.items()})


def set_state(conn, rid, dst):
    conn.execute(
        text(
            "UPDATE trial_rounds SET state = CAST(:d AS varchar),"
            " cancel_reason = CASE WHEN CAST(:d AS varchar) = 'CANCELLED'"
            " THEN 'wp31 probe' ELSE NULL END WHERE id = :i"
        ),
        {"d": dst, "i": str(rid)},
    )


# --- 判据（正向与反向共用同一份）------------------------------------------------
def j_matrix(conn):
    src = conn.execute(
        text("SELECT prosrc FROM pg_proc WHERE proname = :f"), {"f": tddl.TRANSITION_FUNCTION}
    ).scalar_one_or_none()
    if src is None:
        return False, "函数不存在"
    b, e = src.count(tddl.MATRIX_BEGIN), src.count(tddl.MATRIX_END)
    if (b, e) != (1, 1) or src.index(tddl.MATRIX_BEGIN) > src.index(tddl.MATRIX_END):
        return False, f"标记个数 BEGIN={b} END={e} 或顺序错"
    block = src.split(tddl.MATRIX_BEGIN, 1)[1].split(tddl.MATRIX_END, 1)[0]
    parsed = {}
    for m in re.finditer(r"OLD\.state = '(\w+)' AND NEW\.state IN \(([^)]*)\)", block):
        parsed[m.group(1)] = frozenset(re.findall(r"'(\w+)'", m.group(2)))
    expected = {k: v for k, v in ttypes.ROUND_TRANSITIONS.items() if v}
    n_old = len(re.findall(r"OLD\.state = '", block))
    ok = parsed == expected and n_old == len(expected)
    return ok, f"反解析 {{{', '.join(f'{k}->{sorted(v)}' for k, v in sorted(parsed.items()))}}} OLD.state 出现 {n_old} 次"


def j_edge_behavior(mutation=None):
    return run_probe(
        lambda conn, s: mk_round(conn, s, state="GATE_PASSED"),
        lambda conn, s, rid: set_state(conn, rid, "PLANNED"),
        CONSISTENCY_SQLSTATE, msg="不在合法迁移矩阵内", mutation=mutation,
    )


def j_gate_needs_decision(mutation=None):
    return run_probe(
        lambda conn, s: mk_round(conn, s, state="REVIEWING"),
        lambda conn, s, rid: upd(conn, "UPDATE trial_rounds SET state='GATE_PASSED' WHERE id=:i", i=rid),
        CONSISTENCY_SQLSTATE, msg="没有关口决定", force_after=True, mutation=mutation,
    )


def j_decision_needs_passed(mutation=None):
    return run_probe(
        lambda conn, s: mk_round(conn, s, state="REVIEWING"),
        lambda conn, s, rid: mk_decision(conn, rid),
        CONSISTENCY_SQLSTATE, msg="决定存在而状态未推进", force_after=True, mutation=mutation,
    )


def j_decision_immutable(mutation=None):
    def setup(conn, s):
        rid = mk_round(conn, s, state="GATE_PASSED")
        return mk_decision(conn, rid)

    return run_probe(
        setup,
        lambda conn, s, did: upd(conn, "UPDATE trial_gate_decisions SET reason='x' WHERE id=:i", i=did),
        IMMUTABLE_SQLSTATE, mutation=mutation,
    )


def j_slot_write_once(mutation=None):
    def setup(conn, s):
        rid = mk_round(conn, s)
        c = mk_content(conn, s["project"])
        return mk_slot(conn, s, rid, content_item_id=c)

    def action(conn, s, sid):
        c2 = mk_content(conn, s["project"])
        upd(conn, "UPDATE trial_round_slots SET content_item_id=:c WHERE id=:i", c=c2, i=sid)

    return run_probe(setup, action, IMMUTABLE_SQLSTATE, mutation=mutation)


def j_checks(conn):
    diffs = []
    total = 0
    for table in OWN_TABLES:
        expected = {c.name for c in tmodels.Base.metadata.tables[table].constraints
                    if c.__class__.__name__ == "CheckConstraint"}
        actual = sql_set(conn, f"SELECT conname FROM pg_constraint WHERE contype='c'"
                               f" AND conrelid = CAST('{table}' AS regclass)")
        total += len(actual)
        if expected != actual:
            diffs.append(f"{table}: {sorted(expected ^ actual)}")
    return (not diffs and total == 29), f"库 {total} 条 / 期望 29；差集 {diffs}"


def j_where(conn):
    defs = {r[0]: r[1] for r in conn.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='public'"
             " AND indexname IN ('uq_trial_rounds_project_open','uq_trial_rounds_project_idempotency')"))}
    o = defs.get("uq_trial_rounds_project_open", "")
    i = defs.get("uq_trial_rounds_project_idempotency", "")
    o_where = o.split(" WHERE ", 1)[1] if " WHERE " in o else ""
    i_where = i.split(" WHERE ", 1)[1] if " WHERE " in i else ""
    ok_o = (o.startswith("CREATE UNIQUE INDEX") and "(project_id)" in o
            and quoted_words(o_where) == set(ttypes.ROUND_OPEN_STATES)
            and "state" in o_where)
    ok_i = (i.startswith("CREATE UNIQUE INDEX") and "(project_id, idempotency_key)" in i
            and i_where.replace("(", "").replace(")", "") == "idempotency_key IS NOT NULL")
    return ok_o and ok_i, f"open WHERE={o_where!r} idempotency WHERE={i_where!r}"


def j_fk(conn):
    rows = conn.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid), confdeltype, condeferrable"
            " FROM pg_constraint WHERE contype='f' AND conrelid = ANY("
            "SELECT oid FROM pg_class WHERE relname = ANY(:n))"
        ),
        {"n": list(OWN_TABLES)},
    ).fetchall()
    bad = [r[0] for r in rows if r[2] != "r" or r[3]]
    names = {r[0] for r in rows}
    defs_ok = all(FK_SPEC.get(r[0]) == r[1] for r in rows)
    return (not bad and names == set(FK_SPEC) and defs_ok), f"{len(rows)} 条 非RESTRICT或DEFERRABLE {bad}"


def j_triggers(conn):
    have = sql_set(
        conn,
        "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgrelid = ANY("
        "SELECT oid FROM pg_class WHERE relname IN ('trial_rounds','trial_round_slots','trial_gate_decisions'))",
    )
    return have == set(TRIGGER_SPEC), f"库 {sorted(have)}"


def j_purity(conn, fn):
    tables = list(OWN_TABLES) + ["projects", "content_items", "version_nodes",
                                 "production_baselines", "users", "audit_events"]

    def counts():
        return {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in tables}

    stmts = []

    def hook(c, cur, statement, params, ctx, many):
        stmts.append(statement)

    before = counts()
    event.listen(conn, "before_cursor_execute", hook)
    try:
        fn()
    finally:
        event.remove(conn, "before_cursor_execute", hook)
    after = counts()
    bad = [x[:40] for x in stmts if not re.match(r"\s*(SELECT|WITH)\b", x, re.I)]
    changed = {t: (before[t], after[t]) for t in tables if before[t] != after[t]}
    return (not bad and not changed), f"发出 {len(stmts)} 条语句，非 SELECT {bad}，计数变化 {changed}"


def j_migration_source(source):
    # 迁移不得 import 取值域 / 模型 / 类型；唯一允许的 rednote 依赖是触发器 DDL 生成器 ddl。
    tree = ast.parse(source)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = [a.name for a in node.names]
            if mod.startswith("rednote"):
                if not (mod == "rednote.modules.planning.trial" and names == ["ddl"]):
                    bad.append(f"from {mod} import {names}")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("rednote"):
                    bad.append(f"import {a.name}")
    return not bad, f"越界 import {bad}"


def j_shared(conn, baseline_triggers):
    fn = conn.execute(text("SELECT count(*) FROM pg_proc WHERE proname='rednote_reject_mutation'")).scalar_one()
    have = {(r[0], r[1]) for r in conn.execute(
        text("SELECT tgname, tgrelid::regclass::text FROM pg_trigger WHERE NOT tgisinternal"))}
    missing = sorted(baseline_triggers - have)
    return (fn == 1 and not missing), f"rednote_reject_mutation {fn} 个，缺失其它包触发器 {missing[:5]}"


# --- 启动时的全局计数（Z 节对账）-------------------------------------------------
with engine.connect() as conn:
    START_COUNTS = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
                    for t in list(OWN_TABLES) + ["projects", "users", "content_items", "version_nodes"]}
    conn.rollback()

# =============================================================================
# S：结构
# =============================================================================
with engine.connect() as conn:
    has_alembic = conn.execute(text("SELECT to_regclass('public.alembic_version') IS NOT NULL")).scalar_one()
    current = None
    if has_alembic:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        current = row[0] if row else None
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(REPO / "migrations" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    chain = [r.revision for r in ScriptDirectory.from_config(cfg).walk_revisions(base="base", head="heads")]
    # walk_revisions 自新到旧：current 的下标越小越新。
    at_least = (current in chain and WP31_MIGRATION_REVISION in chain
                and chain.index(current) <= chain.index(WP31_MIGRATION_REVISION))
    emit(has_alembic and at_least, "[S] 迁移 head 不早于 WP31 引入点（真 CHECK，非 SKIP）",
         f"current={current} 引入点={WP31_MIGRATION_REVISION} alembic_version表={has_alembic}")

    n = conn.execute(
        text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name = ANY(:n)"),
        {"n": list(OWN_TABLES)},
    ).scalar_one()
    emit(n == 3, "[S] 本包 3 张表存在", f"{n}/3（缺则先跑 alembic upgrade head；本脚本不建表）")
    emit(set(COLUMN_SPEC) == set(OWN_TABLES) and tuple(tmodels.IMMUTABLE_TABLES) == (DECISIONS,),
         "[S] 本包表清单与不可变表清单", f"OWN_TABLES={list(OWN_TABLES)} IMMUTABLE={list(tmodels.IMMUTABLE_TABLES)}")

    for table in OWN_TABLES:
        rows = conn.execute(
            text(
                "SELECT column_name, data_type, character_maximum_length, is_nullable,"
                " coalesce(column_default, '-') FROM information_schema.columns"
                " WHERE table_schema='public' AND table_name=:t ORDER BY ordinal_position"
            ),
            {"t": table},
        ).fetchall()
        actual = {r[0]: (r[1] + (f"({r[2]})" if r[2] else ""), r[3], r[4]) for r in rows}
        spec = COLUMN_SPEC[table]
        emit(len(rows) == len(spec), f"[S] {table} 列数", f"实际 {len(rows)} / 期望 {len(spec)}")
        for name, typ, nullable, default in spec:
            got = actual.get(name)
            emit(got == (typ, nullable, default), f"[S] {table}.{name}",
                 f"实际 {got} / 期望 {(typ, nullable, default)}")
        model_cols = {c.name for c in tmodels.Base.metadata.tables[table].columns}
        emit(model_cols == set(actual) == {c[0] for c in spec}, f"[S] {table} 列名集合：模型 = 库 = 期望表",
             f"差集 {sorted(model_cols ^ set(actual))}")

    # CHECK：逐名对账模型，再逐条点名
    total_checks = 0
    for table in OWN_TABLES:
        expected = {c.name for c in tmodels.Base.metadata.tables[table].constraints
                    if c.__class__.__name__ == "CheckConstraint"}
        defs = {r[0]: r[1] for r in conn.execute(
            text("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE contype='c'"
                 " AND conrelid = CAST(:t AS regclass)"), {"t": table})}
        total_checks += len(defs)
        emit(expected == set(defs), f"[S] {table} CHECK 名集合与模型一致",
             f"模型 {len(expected)} / 库 {len(defs)} 差集 {sorted(expected ^ set(defs))}")
        for name in sorted(expected):
            emit(name in defs, f"[S] CHECK {name}", defs.get(name, "缺失"))
    emit(total_checks == 29, "[S] CHECK 总数 29（18 + 5 + 6）", f"{total_checks}")

    # 取值域化石字面量与 types 常量一致（⑧ 的库内一面）
    def check_def(table, name):
        return conn.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) AND conname = :n"),
            {"t": table, "n": name}).scalar_one_or_none() or ""

    d = check_def(ROUNDS, "ck_trial_rounds_kind_valid")
    emit(quoted_words(d) == set(ttypes.ROUND_KINDS), "[S] kind_valid 字面量 = ROUND_KINDS", d)
    d = check_def(ROUNDS, "ck_trial_rounds_state_valid")
    emit(quoted_words(d) == set(ttypes.ROUND_STATES), "[S] state_valid 字面量 = ROUND_STATES", d)
    d = check_def(ROUNDS, "ck_trial_rounds_limit_source_valid")
    emit(quoted_words(d) == set(ttypes.LIMIT_SOURCES), "[S] limit_source_valid 字面量 = LIMIT_SOURCES", d)
    d = check_def(ROUNDS, "ck_trial_rounds_requested_within_bound")
    emit(str(ttypes.MAX_QUANTITY_BOUND) in d, "[S] requested_within_bound 字面量 = MAX_QUANTITY_BOUND", d)
    d = check_def(ROUNDS, "ck_trial_rounds_request_hash_length")
    emit(f"= {ttypes.REQUEST_HASH_CHARS}" in d, "[S] request_hash_length 字面量 = REQUEST_HASH_CHARS", d)

    # FK
    ok, detail = j_fk(conn)
    emit(ok, "[S] 10 条 FK 全集、定义全等、全部 ON DELETE RESTRICT 且无 DEFERRABLE", detail)
    fk_rows = {r[0]: r for r in conn.execute(
        text("SELECT conname, pg_get_constraintdef(oid), confdeltype, condeferrable FROM pg_constraint"
             " WHERE contype='f' AND conrelid = ANY(SELECT oid FROM pg_class WHERE relname = ANY(:n))"),
        {"n": list(OWN_TABLES)})}
    for name, expected_def in FK_SPEC.items():
        r = fk_rows.get(name)
        emit(r is not None and r[1] == expected_def and r[2] == "r" and r[3] is False,
             f"[S] FK {name}", f"{r[1] if r else '缺失'} confdeltype={r[2] if r else '-'} deferrable={r[3] if r else '-'}")
    model_fk = set()
    for table in OWN_TABLES:
        model_fk |= {c.name for c in tmodels.Base.metadata.tables[table].constraints
                     if c.__class__.__name__ == "ForeignKeyConstraint"}
    emit(model_fk == set(FK_SPEC), "[S] FK 名集合与模型一致", f"差集 {sorted(model_fk ^ set(FK_SPEC))}")

    # 唯一约束 / PK / 部分唯一索引 / 其余索引
    uq_rows = {r[0]: r[1] for r in conn.execute(
        text("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE contype='u'"
             " AND conrelid = ANY(SELECT oid FROM pg_class WHERE relname = ANY(:n))"), {"n": list(OWN_TABLES)})}
    emit(set(uq_rows) == set(UQ_SPEC), "[S] 唯一约束名集合（5 条）", f"库 {sorted(uq_rows)}")
    for name, expected_def in UQ_SPEC.items():
        emit(uq_rows.get(name) == expected_def, f"[S] 唯一约束 {name}", uq_rows.get(name, "缺失"))
    pk_rows = sql_set(conn, "SELECT conname FROM pg_constraint WHERE contype='p' AND conrelid = ANY("
                            "SELECT oid FROM pg_class WHERE relname IN ('trial_rounds','trial_round_slots','trial_gate_decisions'))")
    emit(pk_rows == set(PK_NAMES), "[S] 3 个主键", f"库 {sorted(pk_rows)}")

    ok, detail = j_where(conn)
    emit(ok, "[S] 两个部分唯一索引的 WHERE（open = 未终态集合；idempotency = IS NOT NULL）", detail)
    all_ix = sql_set(conn, "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename = ANY(ARRAY['trial_rounds','trial_round_slots','trial_gate_decisions'])")
    emit(all_ix == ALL_INDEX_NAMES, "[S] 索引全集（12 个：3 PK + 5 唯一约束 + 2 部分唯一 + 2 普通）", f"差集 {sorted(all_ix ^ ALL_INDEX_NAMES)}")
    model_ix = {i.name for t in OWN_TABLES for i in tmodels.Base.metadata.tables[t].indexes}
    emit(model_ix <= all_ix and len(model_ix) == 4, "[S] 模型声明的 4 个 Index 全部存在于库", f"模型 {sorted(model_ix)}")
    for name in ("ix_trial_rounds_source_round", "ix_trial_round_slots_round_scene"):
        d = conn.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname=:n"), {"n": name}).scalar_one_or_none()
        emit(d is not None and "UNIQUE" not in d, f"[S] 普通索引 {name}", d)

    too_long = [r[0] for r in conn.execute(
        text("SELECT conname FROM pg_constraint WHERE length(conname) > :m AND conrelid = ANY("
             "SELECT oid FROM pg_class WHERE relname = ANY(:n))"), {"m": MAX_IDENTIFIER_BYTES, "n": list(OWN_TABLES)})]
    emit(not too_long, "[S] 全部约束名在 63 字节内（不会被静默截断）", f"超限 {too_long}")
    conn.rollback()

# =============================================================================
# F：触发器 / 函数 / 迁移矩阵
# =============================================================================
with engine.connect() as conn:
    ok, detail = j_triggers(conn)
    emit(ok, "[F] 6 个触发器名集合一致（1 不可变 + 2 BEFORE 守卫 + 3 约束触发器）", detail)
    emit(set(tddl.trigger_names()) == set(TRIGGER_SPEC), "[F] ddl.trigger_names() 与期望表一致", f"{sorted(tddl.trigger_names())}")
    for name, (table, frags, deferred) in TRIGGER_SPEC.items():
        row = conn.execute(
            text("SELECT pg_get_triggerdef(oid), tgdeferrable, tginitdeferred, tgrelid::regclass::text"
                 " FROM pg_trigger WHERE tgname=:n AND NOT tgisinternal"), {"n": name}).first()
        if row is None:
            emit(False, f"[F] 触发器 {name}", "缺失")
            continue
        okf = (row[3] == table and all(f in row[0] for f in frags)
               and bool(row[1]) == deferred and bool(row[2]) == deferred)
        emit(okf, f"[F] 触发器 {name}", f"表={row[3]} deferrable={row[1]} initdeferred={row[2]} :: {row[0][:110]}")

    emit(len(tddl.OWN_FUNCTIONS) == 5 and tddl.consistency_function_names() == tddl.OWN_FUNCTIONS,
         "[F] 本包自有函数恰 5 个且不含 rednote_reject_mutation",
         f"{list(tddl.OWN_FUNCTIONS)}")
    fn_body = {}
    for fn in tddl.OWN_FUNCTIONS:
        rows = conn.execute(
            text("SELECT p.prosrc, l.lanname, pg_get_function_result(p.oid) FROM pg_proc p"
                 " JOIN pg_language l ON l.oid = p.prolang JOIN pg_namespace n ON n.oid = p.pronamespace"
                 " WHERE p.proname=:f AND n.nspname='public'"), {"f": fn}).fetchall()
        if len(rows) != 1:
            emit(False, f"[F] 函数 {fn}", f"{len(rows)} 个")
            continue
        fn_body[fn] = rows[0][0]
        emit(rows[0][1] == "plpgsql" and rows[0][2] == "trigger", f"[F] 函数 {fn} 存在一个且为 plpgsql trigger",
             f"lang={rows[0][1]} result={rows[0][2]}")

    body = fn_body.get(tddl.TRANSITION_FUNCTION, "")
    miss = [c for c in tddl.ROUND_IMMUTABLE_COLUMNS if f"NEW.{c} IS DISTINCT FROM OLD.{c}" not in body]
    emit(not miss and "ERRCODE = '23001'" in body and "ERRCODE = '23514'" in body and "TG_OP = 'DELETE'" in body,
         "[F] 迁移守卫函数体：DELETE 拒 + 16 个不可变列逐列 + 23001/23514 两种 ERRCODE", f"缺列 {miss}")
    body = fn_body.get(tddl.SLOT_GUARD_FUNCTION, "")
    miss = [c for c in tddl.SLOT_IMMUTABLE_COLUMNS if f"NEW.{c} IS DISTINCT FROM OLD.{c}" not in body]
    emit(not miss and "write-once" in body and "TG_OP = 'DELETE'" in body,
         "[F] 名额守卫函数体：DELETE 拒 + 7 个不可变列逐列 + write-once", f"缺列 {miss}")
    body = fn_body.get(tddl.ROUND_CONSISTENCY_FUNCTION, "")
    emit("trial_gate_decisions" in body and "GATE_PASSED" in body and "version_nodes" in body
         and "production_baselines" in body and "TRIAL" in body,
         "[F] 轮一致性函数体：PLAN 节点 / 基线 / 源轮(TRIAL) / 到 GATE_PASSED 必有决定", "关键词齐全")
    body = fn_body.get(tddl.SLOT_CONSISTENCY_FUNCTION, "")
    emit("content_items" in body and "replaces_slot_id" in body and "TG_OP = 'INSERT'" in body,
         "[F] 名额一致性函数体：内容同项目 / 替代同轮同方向 / 终态轮只拦 INSERT", "关键词齐全")
    body = fn_body.get(tddl.DECISION_CONSISTENCY_FUNCTION, "")
    emit("GATE_PASSED" in body and "trial_rounds" in body, "[F] 决定一致性函数体：所属轮必须 GATE_PASSED", "关键词齐全")

    ok, detail = j_matrix(conn)
    emit(ok, "[F] 函数体迁移矩阵（MATRIX-BEGIN/END 反解析）与 types.ROUND_TRANSITIONS 逐边一致", detail)
    src = fn_body.get(tddl.TRANSITION_FUNCTION, "")
    block = src.split(tddl.MATRIX_BEGIN, 1)[1].split(tddl.MATRIX_END, 1)[0] if tddl.MATRIX_BEGIN in src else ""
    emit("OLD.state = 'GATE_PASSED'" not in block and "OLD.state = 'CANCELLED'" not in block,
         "[F] 终态（GATE_PASSED / CANCELLED）在矩阵里没有出边", f"终态集合 {sorted(ttypes.ROUND_TERMINAL_STATES)}")
    emit(all(not ttypes.ROUND_TRANSITIONS[t] for t in ttypes.ROUND_TERMINAL_STATES)
         and set(ttypes.ROUND_TRANSITIONS) == set(ttypes.ROUND_STATES)
         and all(d <= set(ttypes.ROUND_STATES) for d in ttypes.ROUND_TRANSITIONS.values()),
         "[F] ROUND_TRANSITIONS 自身：键 = 全部状态、终态无出边、目标态都在状态集内", f"{ {k: sorted(v) for k, v in ttypes.ROUND_TRANSITIONS.items()} }")
    conn.rollback()

# =============================================================================
# X：跨包共存（共用函数与其它包触发器）
# =============================================================================
with engine.connect() as conn:
    n = conn.execute(text("SELECT count(*) FROM pg_proc WHERE proname='rednote_reject_mutation'")).scalar_one()
    emit(n == 1, "[X] 跨包共用不可变函数 rednote_reject_mutation 仍存在", f"{n} 个")
    users_of = sql_set(conn, "SELECT tgname FROM pg_trigger WHERE tgfoid = 'rednote_reject_mutation'::regproc AND NOT tgisinternal")
    wp30_names = set(review_ddl.trigger_names())
    emit(len(users_of) >= 30 and "trg_trial_gate_decisions_immutable" in users_of,
         "[X] 依赖 rednote_reject_mutation 的不可变触发器（含本包 1 条）", f"共 {len(users_of)} 条")
    sample = {"trg_version_nodes_immutable", "trg_production_baselines_immutable", "trg_content_versions_immutable",
              "trg_content_reviews_immutable", "trg_step_approvals_immutable", "trg_audit_events_immutable"}
    emit(sample <= users_of, "[X] 其它包的代表性不可变触发器仍在（版本 / 基线 / 内容 / 审核 / 审批 / 审计）", f"缺 {sorted(sample - users_of)}")
    emit(wp30_names <= sql_set(conn, "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"),
         "[X] WP30 的 5 个触发器仍在（review_ddl.trigger_names()）", f"{sorted(wp30_names)}")
    other_fn = sql_set(conn, "SELECT proname FROM pg_proc WHERE proname ~ '^rednote_check_(baseline|brief|content|knowledge|media|prompt)_node_paired$'")
    emit(len(other_fn) == 6, "[X] 其它包挂在 version_nodes 上的 6 个配对函数仍在", f"{sorted(other_fn)}")
    emit(len(sql_set(conn, "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgrelid = 'version_nodes'::regclass")) >= 7,
         "[X] version_nodes 上的 1 不可变 + 6 配对触发器未被误删", "至少 7 条")
    conn.rollback()

BASELINE_TRIGGERS = None  # 在 D 节由临时库的 downgrade 后快照填充

# =============================================================================
# M：迁移文本（取值域不 import 进迁移）
# =============================================================================
source = MIGRATION_FILE.read_text(encoding="utf-8")
ok, detail = j_migration_source(source)
emit(ok, "[M] 迁移只 import 触发器 DDL 生成器 ddl，不 import 取值域 / models / types", detail)
for lit in ("kind IN ('SUPPLEMENT', 'TRIAL')", "limit_source IN ('GLOBAL', 'INHERITED', 'MANUAL')",
            "state IN ('CANCELLED', 'GATE_PASSED', 'GENERATING', 'PLANNED', 'REVIEWING')"):
    emit(lit in source, "[M] 迁移里是化石字面量", lit)
emit(not re.search(r"\b(ROUND_KINDS|ROUND_STATES|LIMIT_SOURCES|ROUND_KIND_LIST_SQL|ROUND_STATE_LIST_SQL|LIMIT_SOURCE_LIST_SQL|MAX_QUANTITY_BOUND)\b", source),
     "[M] 迁移文本不引用任何取值域常量名", "无命中")
emit("down_revision: str | Sequence[str] | None = 'f3a7c9d15b28'" in source
     and "revision: str = 'a1f31c0de001'" in source, "[M] 迁移 revision / down_revision", "a1f31c0de001 <- f3a7c9d15b28")
emit(not re.search(r"DROP FUNCTION[^\n]*rednote_reject_mutation", source)
     and "trial_ddl.consistency_function_names()" in source.split("def downgrade", 1)[1],
     "[M] downgrade 只按 consistency_function_names() 删函数，没有 DROP rednote_reject_mutation", "只删本包 5 个自有函数")

# =============================================================================
# P：29 条 CHECK 各一条阴性探针（命名全覆盖）
# =============================================================================
PROBED_CHECKS = set()


def chk(label, cname, setup, action, accept=None):
    PROBED_CHECKS.add(cname)
    ok, detail = run_probe(setup, action, CONSISTENCY_SQLSTATE, cname=accept or cname)
    emit(ok, f"[P] {label}", detail)


def round_bad(**o):
    return lambda conn, s, ctx: mk_round(conn, s, **o)


R = "ck_trial_rounds_"
chk("kind 非法值被拒", R + "kind_valid", None, round_bad(kind="X"))
chk("state 非法值被拒", R + "state_valid", None, round_bad(state="X"))
chk("limit_source 非法值被拒", R + "limit_source_valid", None, round_bad(limit_source="X"))
chk("round_no=0 被拒", R + "round_no_positive", None, round_bad(round_no=0))
chk("requested=0 被拒（被 planned_quantity_positive 蕴含，接受二者之一）", R + "requested_quantity_positive", None,
    round_bad(requested_quantity=0, planned_quantity=0), accept=(R + "planned_quantity_positive", R + "requested_quantity_positive"))
chk("planned=0 被拒", R + "planned_quantity_positive", None, round_bad(planned_quantity=0))
chk("planned>requested 被拒", R + "planned_not_above_requested", None, round_bad(planned_quantity=4))
chk("requested>100000 被拒", R + "requested_within_bound", None,
    round_bad(requested_quantity=100001, limit_applied=200000))
chk("limit_applied=0 被拒", R + "limit_applied_positive", None, round_bad(limit_applied=0),
    accept=(R + "limit_applied_positive",))
chk("requested>limit_applied 被拒", R + "requested_within_limit_applied", None, round_bad(requested_quantity=11))
chk("plan_snapshot 非对象被拒", R + "plan_snapshot_is_object", None, round_bad(plan_snapshot=[]))
chk("SUPPLEMENT 无源轮被拒", R + "supplement_has_source", None, round_bad(kind="SUPPLEMENT"))
chk("TRIAL 带源轮被拒", R + "supplement_has_source", None,
    lambda conn, s, ctx: mk_round(conn, s, source_round_id=mk_round(conn, s, state="CANCELLED")))


def _self_source(conn, s, ctx):
    rid = uuid.uuid4()
    mk_round(conn, s, id=rid, kind="SUPPLEMENT", source_round_id=rid)


chk("源轮指向自己被拒", R + "source_not_self", None, _self_source)
chk("CANCELLED 无 cancel_reason 被拒", R + "cancel_reason_paired_with_cancelled", None,
    round_bad(state="CANCELLED", cancel_reason=None))
chk("非 CANCELLED 却有 cancel_reason 被拒", R + "cancel_reason_paired_with_cancelled", None,
    round_bad(cancel_reason="x"))
chk("cancel_reason 全空白被拒", R + "cancel_reason_not_blank", None, round_bad(state="CANCELLED", cancel_reason="   "))
chk("idempotency_key 无 request_hash 被拒", R + "idempotency_paired", None,
    round_bad(idempotency_key="k", request_hash=None))
chk("idempotency_key 全空白被拒", R + "idempotency_key_not_blank", None,
    round_bad(idempotency_key="  ", request_hash="a" * 64))
chk("request_hash 长度非 64 被拒", R + "request_hash_length", None,
    round_bad(idempotency_key="k", request_hash="abc"))

S_ = "ck_trial_round_slots_"
chk("slot_no=0 被拒", S_ + "slot_no_positive", lambda conn, s: mk_round(conn, s),
    lambda conn, s, rid: mk_slot(conn, s, rid, slot_no=0))
chk("scene_key 全空白被拒", S_ + "scene_key_not_blank", lambda conn, s: mk_round(conn, s),
    lambda conn, s, rid: mk_slot(conn, s, rid, scene_key="  "))
chk("form_key 全空白被拒", S_ + "form_key_not_blank", lambda conn, s: mk_round(conn, s),
    lambda conn, s, rid: mk_slot(conn, s, rid, form_key="  "))


def _slot_self_replace(conn, s, rid):
    sid = uuid.uuid4()
    mk_slot(conn, s, rid, id=sid, replaces_slot_id=sid)


chk("替代指向自己被拒", S_ + "replaces_not_self", lambda conn, s: mk_round(conn, s), _slot_self_replace)
chk("有内容无 bound_at 被拒", S_ + "bound_at_paired_with_content", lambda conn, s: mk_round(conn, s),
    lambda conn, s, rid: mk_slot(conn, s, rid, content_item_id=mk_content(conn, s["project"]), bound_at=None))

D_ = "ck_trial_gate_decisions_"
chk("approved_scene_keys 非数组被拒", D_ + "approved_scene_keys_is_array",
    lambda conn, s: mk_round(conn, s, state="REVIEWING"),
    lambda conn, s, rid: mk_decision(conn, rid, approved_scene_keys={"a": 1}))
chk("approved_scene_keys 空数组被拒", D_ + "approved_scene_keys_not_empty",
    lambda conn, s: mk_round(conn, s, state="REVIEWING"),
    lambda conn, s, rid: mk_decision(conn, rid, approved_scene_keys=[]))
chk("condition_results 非对象被拒", D_ + "condition_results_is_object",
    lambda conn, s: mk_round(conn, s, state="REVIEWING"),
    lambda conn, s, rid: mk_decision(conn, rid, condition_results=[]))
chk("statistics 非对象被拒", D_ + "statistics_is_object",
    lambda conn, s: mk_round(conn, s, state="REVIEWING"),
    lambda conn, s, rid: mk_decision(conn, rid, statistics=[]))
chk("basis 非数组被拒", D_ + "basis_is_array",
    lambda conn, s: mk_round(conn, s, state="REVIEWING"),
    lambda conn, s, rid: mk_decision(conn, rid, basis={}))
chk("reason 全空白被拒", D_ + "reason_not_blank",
    lambda conn, s: mk_round(conn, s, state="REVIEWING"),
    lambda conn, s, rid: mk_decision(conn, rid, reason="   "))

model_checks = {c.name for t in OWN_TABLES for c in tmodels.Base.metadata.tables[t].constraints
                if c.__class__.__name__ == "CheckConstraint"}
emit(PROBED_CHECKS == model_checks and len(model_checks) == 29,
     "[P] 29 条 CHECK 每条至少有一条阴性探针（探针命名集合 = 模型命名集合）",
     f"探针覆盖 {len(PROBED_CHECKS)} / 模型 {len(model_checks)} 差集 {sorted(PROBED_CHECKS ^ model_checks)}")

ok, detail = run_probe(
    lambda conn, s: mk_round(conn, s, state="REVIEWING"),
    lambda conn, s, rid: mk_decision(conn, rid, approved_scene_keys="x"),
    CONSISTENCY_SQLSTATE)
emit(ok, "[P] approved_scene_keys 为标量时报 23514 而非 22023（CASE 短路，不是 AND）", detail)
ok, detail = run_probe(
    lambda conn, s: mk_round(conn, s, state="REVIEWING"),
    lambda conn, s, rid: mk_decision(conn, rid, reason="x" * 501), "22001")
emit(ok, "[P] reason 超 500 字符被拒（22001）", detail)
ok, detail = run_probe(
    lambda conn, s: mk_round(conn, s, state="REVIEWING"),
    lambda conn, s, rid: mk_decision(conn, rid, reason="x" * 500))
emit(ok, "[P] reason 恰 500 字符放行（阴性对照）", detail)

# =============================================================================
# T：状态迁移 5x4 全矩阵行为 + 不可变列 + write-once + DELETE
# =============================================================================
STATES = sorted(ttypes.ROUND_STATES)
for src_state in STATES:
    bad, n_legal, n_illegal = [], 0, 0
    for dst in STATES:
        if dst == src_state:
            continue
        legal = dst in ttypes.ROUND_TRANSITIONS[src_state]
        n_legal += legal
        n_illegal += not legal
        ok, detail = run_probe(
            lambda conn, s, st=src_state: mk_round(conn, s, state=st),
            lambda conn, s, rid, d=dst: set_state(conn, rid, d),
            None if legal else CONSISTENCY_SQLSTATE,
            msg=None if legal else "不在合法迁移矩阵内",
        )
        if not ok:
            bad.append(f"{src_state}->{dst}: {detail}")
    emit(not bad, f"[T] 自 {src_state} 出发：{n_illegal} 条非法边全被触发器拒、{n_legal} 条合法边全放行",
         "; ".join(bad) or "全部符合矩阵")

probe("[T] 同态更新（仅 lock_version / updated_at）放行", lambda conn, s: mk_round(conn, s, state="REVIEWING"),
      lambda conn, s, rid: upd(conn, "UPDATE trial_rounds SET lock_version = lock_version + 1, updated_at = now() WHERE id=:i", i=rid))
probe("[T] 取消：PLANNED -> CANCELLED 带 cancel_reason 放行", lambda conn, s: mk_round(conn, s),
      lambda conn, s, rid: set_state(conn, rid, "CANCELLED"))
ok, detail = run_probe(lambda conn, s: mk_round(conn, s),
                       lambda conn, s, rid: upd(conn, "UPDATE trial_rounds SET state='CANCELLED' WHERE id=:i", i=rid),
                       CONSISTENCY_SQLSTATE, cname=R + "cancel_reason_paired_with_cancelled")
emit(ok, "[T] 取消却不给 cancel_reason：触发器放行但 CHECK 拦下", detail)

ROUND_IMM_EXPR = {
    "id": "gen_random_uuid()", "project_id": "gen_random_uuid()", "round_no": "round_no + 1",
    "kind": "'SUPPLEMENT'", "plan_version_id": "gen_random_uuid()", "baseline_id": "gen_random_uuid()",
    "source_round_id": "gen_random_uuid()", "requested_quantity": "requested_quantity - 1",
    "planned_quantity": "planned_quantity - 1", "limit_applied": "limit_applied + 1",
    "limit_source": "'MANUAL'", "plan_snapshot": "jsonb_build_object('x', 1)",
    "idempotency_key": "'k'", "request_hash": "'h'", "created_by": "gen_random_uuid()",
    "created_at": "created_at + interval '1 day'",
}
MUTABLE_ROUND_COLS = {"state", "cancel_reason", "updated_at", "lock_version"}
with engine.connect() as _c:
    all_round_cols = sql_set(_c, "SELECT column_name FROM information_schema.columns WHERE table_name='trial_rounds'")
    all_slot_cols = sql_set(_c, "SELECT column_name FROM information_schema.columns WHERE table_name='trial_round_slots'")
emit(set(ROUND_IMM_EXPR) == set(tddl.ROUND_IMMUTABLE_COLUMNS)
     and set(tddl.ROUND_IMMUTABLE_COLUMNS) | MUTABLE_ROUND_COLS == all_round_cols
     and not set(tddl.ROUND_IMMUTABLE_COLUMNS) & MUTABLE_ROUND_COLS,
     "[T] trial_rounds：不可变列(16) + 可变列(4) = 全部 20 列，不多不少", f"差集 {sorted(all_round_cols ^ (set(tddl.ROUND_IMMUTABLE_COLUMNS) | MUTABLE_ROUND_COLS))}")
bad = []
for col, expr in ROUND_IMM_EXPR.items():
    ok, detail = run_probe(lambda conn, s: mk_round(conn, s),
                           lambda conn, s, rid, c=col, e=expr: upd(conn, f"UPDATE trial_rounds SET {c} = {e} WHERE id=:i", i=rid),
                           IMMUTABLE_SQLSTATE)
    if not ok:
        bad.append(f"{col}: {detail}")
emit(not bad, "[T] trial_rounds 16 个不可变列逐列 UPDATE 皆被拒（23001）", "; ".join(bad) or "16/16")
probe("[T] trial_rounds 拒绝 DELETE（23001）", lambda conn, s: mk_round(conn, s),
      lambda conn, s, rid: upd(conn, "DELETE FROM trial_rounds WHERE id=:i", i=rid), IMMUTABLE_SQLSTATE)

SLOT_IMM_EXPR = {
    "id": "gen_random_uuid()", "round_id": "gen_random_uuid()", "slot_no": "slot_no + 1",
    "scene_key": "'zz'", "form_key": "'zz'", "replaces_slot_id": "gen_random_uuid()",
    "created_at": "created_at + interval '1 day'",
}
emit(set(SLOT_IMM_EXPR) == set(tddl.SLOT_IMMUTABLE_COLUMNS)
     and set(tddl.SLOT_IMMUTABLE_COLUMNS) | {"content_item_id", "bound_at", "updated_at"} == all_slot_cols,
     "[T] trial_round_slots：不可变列(7) + write-once(2) + updated_at = 全部 10 列", f"库 {sorted(all_slot_cols)}")
bad = []
for col, expr in SLOT_IMM_EXPR.items():
    ok, detail = run_probe(lambda conn, s: mk_slot(conn, s, mk_round(conn, s)),
                           lambda conn, s, sid, c=col, e=expr: upd(conn, f"UPDATE trial_round_slots SET {c} = {e} WHERE id=:i", i=sid),
                           IMMUTABLE_SQLSTATE)
    if not ok:
        bad.append(f"{col}: {detail}")
emit(not bad, "[T] trial_round_slots 7 个不可变列逐列 UPDATE 皆被拒（23001）", "; ".join(bad) or "7/7")


def _bind(conn, s, sid):
    c = mk_content(conn, s["project"])
    upd(conn, "UPDATE trial_round_slots SET content_item_id=:c, bound_at=now() WHERE id=:i", c=c, i=sid)


probe("[T] 名额首次绑定内容（NULL -> 值）放行", lambda conn, s: mk_slot(conn, s, mk_round(conn, s)), _bind, force_after=True)
j_ok, j_detail = j_slot_write_once()
emit(j_ok, "[T] 名额内容已绑定后改绑他内容被拒（write-once，23001）", j_detail)
probe("[T] 名额内容已绑定后清空（值 -> NULL）被拒", lambda conn, s: mk_slot(conn, s, mk_round(conn, s), content_item_id=mk_content(conn, s["project"])),
      lambda conn, s, sid: upd(conn, "UPDATE trial_round_slots SET content_item_id=NULL, bound_at=NULL WHERE id=:i", i=sid), IMMUTABLE_SQLSTATE)
probe("[T] 名额 bound_at 已写后单独改被拒", lambda conn, s: mk_slot(conn, s, mk_round(conn, s), content_item_id=mk_content(conn, s["project"])),
      lambda conn, s, sid: upd(conn, "UPDATE trial_round_slots SET bound_at = bound_at + interval '1 day' WHERE id=:i", i=sid), IMMUTABLE_SQLSTATE)
probe("[T] 名额只写 content_item_id 不写 bound_at：触发器放行但配对 CHECK 拦下", lambda conn, s: mk_slot(conn, s, mk_round(conn, s)),
      lambda conn, s, sid: upd(conn, "UPDATE trial_round_slots SET content_item_id=:c WHERE id=:i", c=mk_content(conn, s["project"]), i=sid),
      CONSISTENCY_SQLSTATE, cname=S_ + "bound_at_paired_with_content")
probe("[T] 名额 updated_at 可改（放行）", lambda conn, s: mk_slot(conn, s, mk_round(conn, s)),
      lambda conn, s, sid: upd(conn, "UPDATE trial_round_slots SET updated_at = now() WHERE id=:i", i=sid))
probe("[T] trial_round_slots 拒绝 DELETE（23001）", lambda conn, s: mk_slot(conn, s, mk_round(conn, s)),
      lambda conn, s, sid: upd(conn, "DELETE FROM trial_round_slots WHERE id=:i", i=sid), IMMUTABLE_SQLSTATE)


def _decided(conn, s):
    rid = mk_round(conn, s, state="GATE_PASSED")
    return mk_decision(conn, rid)


j_ok, j_detail = j_decision_immutable()
emit(j_ok, "[T] trial_gate_decisions 拒绝 UPDATE（不可变，23001）", j_detail)
probe("[T] trial_gate_decisions 拒绝 DELETE（23001）", _decided,
      lambda conn, s, did: upd(conn, "DELETE FROM trial_gate_decisions WHERE id=:i", i=did), IMMUTABLE_SQLSTATE)
bad = []
for col, expr in (("approved_scene_keys", "jsonb_build_array('zz')"), ("statistics", "jsonb_build_object('x', 1)"),
                  ("decided_at", "decided_at + interval '1 day'"), ("round_id", "gen_random_uuid()")):
    ok, detail = run_probe(_decided, lambda conn, s, did, c=col, e=expr: upd(conn, f"UPDATE trial_gate_decisions SET {c} = {e} WHERE id=:i", i=did),
                           IMMUTABLE_SQLSTATE)
    if not ok:
        bad.append(f"{col}: {detail}")
emit(not bad, "[T] trial_gate_decisions 任意列 UPDATE 皆被拒", "; ".join(bad) or "4/4")

# =============================================================================
# G：到 GATE_PASSED 必有决定行，且反向
# =============================================================================
ok, detail = j_gate_needs_decision()
emit(ok, "[G] 轮推到 GATE_PASSED 却没有决定行：延迟触发器拒（23514）", detail)
ok, detail = j_decision_needs_passed()
emit(ok, "[G] 存在决定行但轮不是 GATE_PASSED：延迟触发器拒（23514，反向）", detail)


def _pair(conn, s, ctx):
    rid = mk_round(conn, s, state="REVIEWING")
    mk_decision(conn, rid)
    upd(conn, "UPDATE trial_rounds SET state='GATE_PASSED' WHERE id=:i", i=rid)


probe("[G] 决定行 + 轮 GATE_PASSED 同事务成对：放行（阴性对照）", None, _pair, force_after=True)
probe("[G] 决定行直接挂在 CANCELLED 轮上被拒", lambda conn, s: mk_round(conn, s, state="CANCELLED"),
      lambda conn, s, rid: mk_decision(conn, rid), CONSISTENCY_SQLSTATE, force_after=True)
probe("[G] 同一轮第二条决定被唯一约束拒（23505）", lambda conn, s: (lambda r: (mk_decision(conn, r), r)[1])(mk_round(conn, s, state="GATE_PASSED")),
      lambda conn, s, rid: mk_decision(conn, rid), UNIQUE_SQLSTATE, cname="uq_trial_gate_decisions_round_id")
probe("[G] 决定行缺所属轮被 FK 拒（23503）", None,
      lambda conn, s, ctx: mk_decision(conn, uuid.uuid4()), FK_SQLSTATE, cname="fk_trial_gate_decisions_round_id_trial_rounds")

# =============================================================================
# K：轮一致性（INSERT 归属）
# =============================================================================
probe("[K] 钉住他项目的 PLAN 版本被拒", lambda conn, s: seed(conn),
      lambda conn, s, o: mk_round(conn, s, plan_version_id=o["plan"]), CONSISTENCY_SQLSTATE, msg="不是本项目的 PLAN 版本", force_after=True)
probe("[K] 钉住的版本类型不是 PLAN（用 BASELINE 节点冒充）被拒", None,
      lambda conn, s, ctx: mk_round(conn, s, plan_version_id=s["base"]), CONSISTENCY_SQLSTATE, msg="不是本项目的 PLAN 版本", force_after=True)
probe("[K] 钉住不存在的 PLAN 版本被 FK 拒（23503）", None,
      lambda conn, s, ctx: mk_round(conn, s, plan_version_id=uuid.uuid4()), FK_SQLSTATE,
      cname="fk_trial_rounds_plan_version_id_version_nodes")
probe("[K] 钉住他项目的基线被拒", lambda conn, s: seed(conn),
      lambda conn, s, o: mk_round(conn, s, baseline_id=o["base"]), CONSISTENCY_SQLSTATE, msg="基线不属于本项目", force_after=True)
probe("[K] 补充轮的源轮在他项目被拒", lambda conn, s: (lambda o: (o, mk_round(conn, o, state="CANCELLED"))[1])(seed(conn)),
      lambda conn, s, src: mk_round(conn, s, kind="SUPPLEMENT", source_round_id=src), CONSISTENCY_SQLSTATE,
      msg="源轮必须是同项目的 TRIAL 轮", force_after=True)


def _supplement_of_supplement(conn, s):
    r1 = mk_round(conn, s, state="CANCELLED")
    return mk_round(conn, s, kind="SUPPLEMENT", source_round_id=r1, state="CANCELLED")


probe("[K] 补充轮的源轮是 SUPPLEMENT（不是 TRIAL）被拒", _supplement_of_supplement,
      lambda conn, s, s1: mk_round(conn, s, kind="SUPPLEMENT", source_round_id=s1), CONSISTENCY_SQLSTATE,
      msg="源轮必须是同项目的 TRIAL 轮", force_after=True)
probe("[K] 补充轮的源轮是同项目 TRIAL：放行（阴性对照）", lambda conn, s: mk_round(conn, s, state="CANCELLED"),
      lambda conn, s, src: mk_round(conn, s, kind="SUPPLEMENT", source_round_id=src), force_after=True)

# =============================================================================
# L：名额一致性
# =============================================================================
probe("[L] 名额绑定他项目的内容被拒", lambda conn, s: (mk_round(conn, s), seed(conn)),
      lambda conn, s, ctx: mk_slot(conn, s, ctx[0], content_item_id=mk_content(conn, ctx[1]["project"])),
      CONSISTENCY_SQLSTATE, msg="不属于本轮所在项目", force_after=True)
probe("[L] 名额绑定同项目的内容：放行（阴性对照）", lambda conn, s: mk_round(conn, s),
      lambda conn, s, rid: mk_slot(conn, s, rid, content_item_id=mk_content(conn, s["project"])), force_after=True)


def _base_slot(conn, s):
    rid = mk_round(conn, s)
    return rid, mk_slot(conn, s, rid, scene_key="s1")


probe("[L] 替代名额同轮同方向：放行（阴性对照）", _base_slot,
      lambda conn, s, ctx: mk_slot(conn, s, ctx[0], scene_key="s1", replaces_slot_id=ctx[1]), force_after=True)
probe("[L] 替代名额方向不同被拒", _base_slot,
      lambda conn, s, ctx: mk_slot(conn, s, ctx[0], scene_key="s2", replaces_slot_id=ctx[1]),
      CONSISTENCY_SQLSTATE, msg="同一轮且方向相同", force_after=True)


def _cross_round(conn, s):
    r1, a = _base_slot(conn, s)
    force(conn)
    set_state(conn, r1, "CANCELLED")
    r2 = mk_round(conn, s)
    return r2, a


probe("[L] 替代名额跨轮被拒", _cross_round,
      lambda conn, s, ctx: mk_slot(conn, s, ctx[0], scene_key="s1", replaces_slot_id=ctx[1]),
      CONSISTENCY_SQLSTATE, msg="同一轮且方向相同", force_after=True)
probe("[L] CANCELLED 轮不得再加名额（INSERT 被拒）", lambda conn, s: mk_round(conn, s, state="CANCELLED"),
      lambda conn, s, rid: mk_slot(conn, s, rid), CONSISTENCY_SQLSTATE, msg="不得再加名额", force_after=True)


def _passed_setup(conn, s):
    rid = mk_passed_round(conn, s)
    force(conn)
    return rid


probe("[L] GATE_PASSED 轮不得再加名额（INSERT 被拒）", _passed_setup,
      lambda conn, s, rid: mk_slot(conn, s, rid), CONSISTENCY_SQLSTATE, msg="不得再加名额", force_after=True)


def _cancelled_with_slot(conn, s):
    rid = mk_round(conn, s)
    sid = mk_slot(conn, s, rid)
    force(conn)
    set_state(conn, rid, "CANCELLED")
    return sid


probe("[L] 终态轮上已有名额的绑定内容仍可做（终态只拦 INSERT：对照）", _cancelled_with_slot, _bind, force_after=True)

# =============================================================================
# U：唯一约束 / 部分唯一索引
# =============================================================================
probe("[U] 同项目重复 round_no 被拒", lambda conn, s: mk_round(conn, s, state="CANCELLED", round_no=1),
      lambda conn, s, ctx: mk_round(conn, s, state="CANCELLED", round_no=1), UNIQUE_SQLSTATE,
      cname="uq_trial_rounds_project_id_round_no")
probe("[U] 同项目第二个未终态轮被拒（部分唯一索引）", lambda conn, s: mk_round(conn, s),
      lambda conn, s, ctx: mk_round(conn, s, state="GENERATING"), UNIQUE_SQLSTATE, cname="uq_trial_rounds_project_open")
probe("[U] 前一轮 CANCELLED 后可再开一轮（索引 WHERE 排除终态）", lambda conn, s: mk_round(conn, s, state="CANCELLED"),
      lambda conn, s, ctx: mk_round(conn, s))
probe("[U] 前一轮 GATE_PASSED 后可再开一轮（索引 WHERE 排除终态）", lambda conn, s: mk_round(conn, s, state="GATE_PASSED"),
      lambda conn, s, ctx: mk_round(conn, s))
probe("[U] 同项目重复 Idempotency-Key 被拒", lambda conn, s: mk_round(conn, s, state="CANCELLED", idempotency_key="k1"),
      lambda conn, s, ctx: mk_round(conn, s, state="CANCELLED", idempotency_key="k1"), UNIQUE_SQLSTATE,
      cname="uq_trial_rounds_project_idempotency")
probe("[U] 他项目同一 Idempotency-Key 放行（键按项目隔离）",
      lambda conn, s: (mk_round(conn, s, state="CANCELLED", idempotency_key="k1"), seed(conn)),
      lambda conn, s, ctx: mk_round(conn, ctx[1], state="CANCELLED", idempotency_key="k1"))
probe("[U] 无 Idempotency-Key 的多轮不互斥（NULL 不入索引）", lambda conn, s: mk_round(conn, s, state="CANCELLED"),
      lambda conn, s, ctx: mk_round(conn, s, state="CANCELLED"))
probe("[U] 同轮重复 slot_no 被拒", lambda conn, s: (lambda r: (mk_slot(conn, s, r, slot_no=1), r)[1])(mk_round(conn, s)),
      lambda conn, s, rid: mk_slot(conn, s, rid, slot_no=1), UNIQUE_SQLSTATE, cname="uq_trial_round_slots_round_id_slot_no")


def _bound_slot(conn, s):
    rid = mk_round(conn, s)
    c = mk_content(conn, s["project"])
    mk_slot(conn, s, rid, content_item_id=c)
    return rid, c


probe("[U] 一条内容绑到两个名额被拒", _bound_slot,
      lambda conn, s, ctx: mk_slot(conn, s, ctx[0], content_item_id=ctx[1]), UNIQUE_SQLSTATE,
      cname="uq_trial_round_slots_content_item_id")


def _replaced(conn, s):
    rid, a = _base_slot(conn, s)
    mk_slot(conn, s, rid, scene_key="s1", replaces_slot_id=a)
    return rid, a


probe("[U] 一个名额被替代两次被拒", _replaced,
      lambda conn, s, ctx: mk_slot(conn, s, ctx[0], scene_key="s1", replaces_slot_id=ctx[1]), UNIQUE_SQLSTATE,
      cname="uq_trial_round_slots_replaces_slot_id")
probe("[U] 多个未绑定名额（content_item_id 均 NULL）不互斥", lambda conn, s: (lambda r: (mk_slot(conn, s, r), r)[1])(mk_round(conn, s)),
      lambda conn, s, rid: mk_slot(conn, s, rid))

# =============================================================================
# R：路由（create_app 的 OpenAPI）
# =============================================================================
from rednote.main import create_app

spec = create_app().openapi()
paths = spec["paths"]
trial_ops = [(m.upper(), p, o["operationId"]) for p, v in paths.items() if "trial-" in p for m, o in v.items()]
emit(len(trial_ops) == 9 and len(ROUTE_SPEC) == 9, "[R] OpenAPI 里 trial-* 路径下恰 9 个操作", f"实际 {len(trial_ops)}")
for method, path, op_id, status in ROUTE_SPEC:
    op = paths.get(path, {}).get(method.lower())
    got_status = sorted(k for k in (op or {}).get("responses", {}) if k.startswith("2"))
    emit(op is not None and op["operationId"] == op_id and "planning-trial" in op.get("tags", []) and got_status == [status],
         f"[R] {method} {path}",
         f"operationId={(op or {}).get('operationId')} tags={(op or {}).get('tags')} 2xx={got_status} 期望 {status}")
emit({(m, p, i) for m, p, i, _ in ROUTE_SPEC} == set(trial_ops), "[R] 路由全集与点名清单逐项相等（不多不少）",
     f"差集 {sorted(set(trial_ops) ^ {(m, p, i) for m, p, i, _ in ROUTE_SPEC})}")
emit(sum(1 for p in paths if p.endswith("/trial-rounds")) == 1 and "/api/v1/projects/{projectId}/trial-rounds" in paths,
     "[R] /trial-rounds 集合路径存在且唯一（GET 列表 + POST 创建）",
     f"方法 {sorted(paths.get('/api/v1/projects/{projectId}/trial-rounds', {}))}")

# =============================================================================
# Q：只读接缝 trial_gate_status
# =============================================================================
from rednote.modules.planning import trial as trial_pkg
from rednote.modules.planning.trial import seam

emit(trial_pkg.trial_gate_status is seam.trial_gate_status and "trial_gate_status" in trial_pkg.__all__,
     "[Q] from rednote.modules.planning.trial import trial_gate_status 可 import 且即 seam 里的那个", "同一对象")
sig = inspect.signature(seam.trial_gate_status)
params = list(sig.parameters.values())
emit([p.name for p in params] == ["session", "project_id", "plan_version_id", "baseline_id"]
     and params[0].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
     and all(p.kind == inspect.Parameter.KEYWORD_ONLY for p in params[1:])
     and params[2].default is None and params[3].default is None and params[1].default is inspect.Parameter.empty,
     "[Q] 签名 (session, *, project_id, plan_version_id=None, baseline_id=None)", str(sig))
emit(str(sig.return_annotation) == "TrialGateStatus", "[Q] 返回类型 TrialGateStatus", str(sig.return_annotation))
import dataclasses

emit([f.name for f in dataclasses.fields(seam.TrialGateStatus)] == ["passed", "latest", "verified_scene_keys"]
     and seam.TrialGateStatus.__dataclass_params__.frozen,
     "[Q] TrialGateStatus 字段 passed / latest / verified_scene_keys 且 frozen", str([f.name for f in dataclasses.fields(seam.TrialGateStatus)]))
emit([f.name for f in dataclasses.fields(seam.TrialGateDecisionInfo)] == [
    "decision_id", "round_id", "round_no", "round_kind", "plan_version_id", "baseline_id",
    "approved_scene_keys", "decided_at", "decided_by", "stale", "stale_reasons"]
     and seam.TrialGateDecisionInfo.__dataclass_params__.frozen,
     "[Q] TrialGateDecisionInfo 11 个字段点名且 frozen", str([f.name for f in dataclasses.fields(seam.TrialGateDecisionInfo)]))
emit({ttypes.STALE_BASELINE_CHANGED, ttypes.STALE_CONTENT_REVISED, ttypes.STALE_REVIEW_CHANGED}
     == {"BASELINE_CHANGED", "CONTENT_REVISED", "REVIEW_CHANGED"}, "[Q] 过期原因码 BASELINE_CHANGED / CONTENT_REVISED / REVIEW_CHANGED", "三码")

# 静态：seam 源码里没有写调用，也没有非 SELECT 的 SQL 字符串。
seam_tree = ast.parse(inspect.getsource(seam))
doc_ids = set()
for node in ast.walk(seam_tree):
    if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr) \
            and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
        doc_ids.add(id(node.body[0].value))
write_calls = [n.func.attr for n in ast.walk(seam_tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr in ("commit", "flush", "add", "delete", "merge", "begin", "rollback", "refresh")]
write_sql = [n.value[:30] for n in ast.walk(seam_tree)
             if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_ids
             and re.search(r"\b(INSERT|UPDATE|DELETE|FOR UPDATE|FOR SHARE|pg_advisory)\b", n.value)]
emit(not write_calls and not write_sql, "[Q] seam 源码静态：无 commit/flush/add/delete 调用，无写 SQL / 行锁字面量",
     f"写调用 {write_calls} 写SQL {write_sql}")


def seam_world(conn, s, keys=("s1", "s2"), active=True):
    rid = mk_passed_round(conn, s, keys=keys)
    force(conn)
    if active:
        upd(conn, "UPDATE projects SET active_baseline_id=:b WHERE id=:p", b=s["base"], p=s["project"])
    return rid


def seam_run(conn, **kw):
    sess = Session(bind=conn, join_transaction_mode="rollback_only")
    try:
        return seam.trial_gate_status(sess, **kw)
    finally:
        sess.close()


with tx() as conn:
    s = seed(conn)
    r = seam_run(conn, project_id=s["project"])
    emit(r.passed is False and r.latest is None and r.verified_scene_keys == (),
         "[Q] 无决定的项目：passed=False / latest=None / verified=()", repr(r))
with tx() as conn:
    try:
        r = seam_run(conn, project_id=uuid.uuid4())
        emit(r.passed is False and r.latest is None, "[Q] 不存在的项目：不抛业务异常，passed=False / latest=None", repr(r))
    except Exception as exc:
        emit(False, "[Q] 不存在的项目：不抛业务异常", f"抛了 {type(exc).__name__}: {exc}")
with tx() as conn:
    s = seed(conn)
    rid = seam_world(conn, s)
    r = seam_run(conn, project_id=s["project"])
    li = r.latest
    emit(r.passed is True and li is not None and li.round_id == rid and li.round_no == 1 and li.round_kind == "TRIAL"
         and li.plan_version_id == s["plan"] and li.baseline_id == s["base"] and li.stale is False
         and li.stale_reasons == () and li.approved_scene_keys == ("s1", "s2") and li.decided_by is None
         and r.verified_scene_keys == ("s1", "s2"),
         "[Q] 已通过且未过期：passed=True，latest 各字段与造数一致，verified=(s1,s2)", repr(r)[:200])
    emit(isinstance(li.decision_id, uuid.UUID) and li.decided_at is not None, "[Q] latest.decision_id 为 UUID、decided_at 非空", str(li.decision_id))
    r2 = seam_run(conn, project_id=s["project"], plan_version_id=s["plan"], baseline_id=s["base"])
    emit(r2.passed is True, "[Q] 过滤：plan_version_id / baseline_id 命中则仍通过", f"passed={r2.passed}")
    r3 = seam_run(conn, project_id=s["project"], plan_version_id=uuid.uuid4())
    emit(r3.passed is False and r3.latest is None and r3.verified_scene_keys == (),
         "[Q] 过滤：plan_version_id 不命中则 passed=False / latest=None", repr(r3)[:120])
    r4 = seam_run(conn, project_id=s["project"], baseline_id=uuid.uuid4())
    emit(r4.passed is False and r4.latest is None, "[Q] 过滤：baseline_id 不命中则 passed=False / latest=None", repr(r4)[:120])
with tx() as conn:
    s = seed(conn)
    seam_world(conn, s, active=False)
    r = seam_run(conn, project_id=s["project"])
    emit(r.passed is False and r.latest is not None and r.latest.stale is True
         and r.latest.stale_reasons == ("BASELINE_CHANGED",) and r.verified_scene_keys == (),
         "[Q] 基线已不是生效基线：passed=False，latest.stale=True 且原因 BASELINE_CHANGED，verified=()", repr(r)[:200])
with tx() as conn:
    s = seed(conn)
    mk_round(conn, s, state="REVIEWING")
    r = seam_run(conn, project_id=s["project"])
    emit(r.passed is False and r.latest is None, "[Q] 只有未终态轮（无 GATE_PASSED）：passed=False / latest=None", repr(r)[:120])
with tx() as conn:
    s = seed(conn)
    seam_world(conn, s)

    def _call():
        seam_run(conn, project_id=s["project"])
        seam_run(conn, project_id=s["project"], plan_version_id=s["plan"])
        seam_run(conn, project_id=uuid.uuid4())

    ok, detail = j_purity(conn, _call)
    emit(ok, "[Q] 纯读：3 次调用前后 9 张表计数不变，且发出的语句全是 SELECT", detail)

# =============================================================================
# V：反向验证（错误实现 / 错误数据放进去，判据必须报红）
# =============================================================================
def mut_matrix(conn):
    src = conn.execute(text("SELECT prosrc FROM pg_proc WHERE proname=:f"), {"f": tddl.TRANSITION_FUNCTION}).scalar_one()
    bad_src = src.replace("IF NOT (", "IF NOT ((OLD.state = 'GATE_PASSED' AND NEW.state IN ('PLANNED')) OR ", 1)
    assert bad_src != src
    conn.exec_driver_sql(f"CREATE OR REPLACE FUNCTION {tddl.TRANSITION_FUNCTION}() RETURNS trigger AS $wp31$" + bad_src + "$wp31$ LANGUAGE plpgsql")


def mut_drop_trigger(name, table):
    return lambda conn: conn.exec_driver_sql(f"DROP TRIGGER {name} ON {table}")


def mut_drop_check(conn):
    conn.exec_driver_sql(f"ALTER TABLE {ROUNDS} DROP CONSTRAINT ck_trial_rounds_kind_valid")


def mut_open_index(conn):
    conn.exec_driver_sql("DROP INDEX uq_trial_rounds_project_open")
    conn.exec_driver_sql("CREATE UNIQUE INDEX uq_trial_rounds_project_open ON trial_rounds (project_id) WHERE state = 'PLANNED'")


def mut_fk_cascade(conn):
    conn.exec_driver_sql(
        "ALTER TABLE trial_round_slots DROP CONSTRAINT fk_trial_round_slots_round_id_trial_rounds,"
        " ADD CONSTRAINT fk_trial_round_slots_round_id_trial_rounds FOREIGN KEY (round_id)"
        " REFERENCES trial_rounds(id) ON DELETE CASCADE")


def with_conn(mutation, judge):
    with tx(mutation) as conn:
        return judge(conn)


rev("[V1] 反向：迁移矩阵函数体被塞进一条越权边（GATE_PASSED->PLANNED）→ 矩阵反解析判据报红",
    lambda: with_conn(mut_matrix, j_matrix))
rev("[V2] 反向：同一错误函数体 → 终态回退 GATE_PASSED->PLANNED 的行为探针发现被放行",
    lambda: j_edge_behavior(mutation=mut_matrix))
rev("[V3] 反向：删掉 trg_trial_rounds_consistency → 「到 GATE_PASSED 必有决定」探针发现被放行",
    lambda: j_gate_needs_decision(mutation=mut_drop_trigger(tddl.ROUND_CONSISTENCY_TRIGGER, ROUNDS)))
rev("[V4] 反向：删掉 trg_trial_gate_decisions_consistency → 「决定存在 => 轮已 GATE_PASSED」探针发现被放行",
    lambda: j_decision_needs_passed(mutation=mut_drop_trigger(tddl.DECISION_CONSISTENCY_TRIGGER, DECISIONS)))
rev("[V5] 反向：删掉 ck_trial_rounds_kind_valid → CHECK 名集合 / 总数 29 判据报红",
    lambda: with_conn(mut_drop_check, j_checks))
rev("[V6] 反向：把 uq_trial_rounds_project_open 的 WHERE 改成只含 PLANNED → 部分唯一索引 WHERE 判据报红",
    lambda: with_conn(mut_open_index, j_where))
rev("[V7] 反向：把名额->轮的 FK 改成 ON DELETE CASCADE → FK 全 RESTRICT 判据报红",
    lambda: with_conn(mut_fk_cascade, j_fk))
rev("[V8] 反向：删掉 trg_trial_gate_decisions_immutable → 决定不可变探针发现 UPDATE 被放行",
    lambda: j_decision_immutable(mutation=mut_drop_trigger("trg_trial_gate_decisions_immutable", DECISIONS)))
rev("[V9] 反向：删掉 trg_trial_round_slots_guard → 名额 write-once 探针发现改绑被放行",
    lambda: j_slot_write_once(mutation=mut_drop_trigger(tddl.SLOT_GUARD_TRIGGER, SLOTS)))
rev("[V10] 反向：删掉名额守卫触发器 → 触发器名集合判据报红",
    lambda: with_conn(mut_drop_trigger(tddl.SLOT_GUARD_TRIGGER, SLOTS), j_triggers))


def _impure(conn):
    s = seed(conn)

    def write():
        mk_round(conn, s, state="CANCELLED")

    return j_purity(conn, write)


def _impure_via_session():
    with tx() as conn:
        return _impure(conn)


rev("[V11] 反向：「接缝」里夹带一次 INSERT → 纯读判据（语句类型 + 表计数）报红", _impure_via_session)
rev("[V12] 反向：迁移文本里加一行 from rednote.modules.planning.trial.types import ROUND_KINDS → 取值域不 import 判据报红",
    lambda: j_migration_source(source + "\nfrom rednote.modules.planning.trial.types import ROUND_KINDS\n"))

# =============================================================================
# D：一次性临时库：迁移 upgrade -> downgrade -> upgrade 往返 + 共用函数反向
# =============================================================================
base_url = make_url(DB)
TMP_NAME = f"{base_url.database}_tmp_{os.getpid()}"
assert "_tmp_" in TMP_NAME and TMP_NAME != base_url.database
admin = create_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
tmp_url = base_url.set(database=TMP_NAME)


def alembic(*args):
    env = dict(os.environ)
    env["REDNOTE_DATABASE_URL"] = tmp_url.render_as_string(hide_password=False)
    p = subprocess.run([sys.executable, "-m", "alembic", "-c", str(REPO / "migrations" / "alembic.ini"), *args],
                       cwd=str(REPO), env=env, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)[-300:]


def catalog(tmp_engine):
    with tmp_engine.connect() as c:
        trig = {(r[0], r[1]) for r in c.execute(text("SELECT tgname, tgrelid::regclass::text FROM pg_trigger WHERE NOT tgisinternal"))}
        funcs = {r[0] for r in c.execute(text("SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public'"))}
        tables = {r[0] for r in c.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))}
        ver = c.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
        c.rollback()
    return trig, funcs, tables, ver


tmp_engine = None
try:
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{TMP_NAME}"'))
        c.execute(text(f'CREATE DATABASE "{TMP_NAME}"'))
    tmp_engine = create_engine(tmp_url)
    with tmp_engine.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    code, tail = alembic("upgrade", WP31_MIGRATION_REVISION)
    trig_a, funcs_a, tables_a, ver_a = catalog(tmp_engine)
    emit(code == 0 and ver_a == WP31_MIGRATION_REVISION and set(OWN_TABLES) <= tables_a,
         "[D] 临时库：空库 upgrade 到 a1f31c0de001 成功，3 表建出", f"exit={code} version={ver_a}")
    own_trig_a = {(t, tbl) for t, tbl in trig_a if t in TRIGGER_SPEC}
    emit(len(own_trig_a) == 6 and set(tddl.OWN_FUNCTIONS) <= funcs_a, "[D] 临时库：6 个触发器与 5 个自有函数随迁移建出",
         f"触发器 {len(own_trig_a)} 函数 {sorted(set(tddl.OWN_FUNCTIONS) & funcs_a)}")
    code, tail = alembic("downgrade", "f3a7c9d15b28")
    trig_b, funcs_b, tables_b, ver_b = catalog(tmp_engine)
    emit(code == 0 and ver_b == "f3a7c9d15b28" and not (set(OWN_TABLES) & tables_b),
         "[D] 临时库：downgrade 回 f3a7c9d15b28 成功，3 表被删", f"exit={code} version={ver_b}")
    emit(not (set(tddl.OWN_FUNCTIONS) & funcs_b) and funcs_a - funcs_b == set(tddl.OWN_FUNCTIONS),
         "[D] downgrade 只删本包 5 个自有函数（函数集合差恰为 OWN_FUNCTIONS）", f"差 {sorted(funcs_a - funcs_b)}")
    emit("rednote_reject_mutation" in funcs_b, "[D] downgrade 之后跨包共用函数 rednote_reject_mutation 仍在", "存在")
    emit(trig_a - trig_b == own_trig_a and trig_b <= trig_a,
         "[D] downgrade 只删本包 6 个触发器，其它包的触发器一个未少", f"少了 {sorted(trig_a - trig_b - own_trig_a)}")
    BASELINE_TRIGGERS = set(trig_b)
    code, tail = alembic("upgrade", WP31_MIGRATION_REVISION)
    trig_c, funcs_c, tables_c, ver_c = catalog(tmp_engine)
    emit(code == 0 and ver_c == WP31_MIGRATION_REVISION and trig_c == trig_a and funcs_c == funcs_a,
         "[D] 临时库：再 upgrade 回 a1f31c0de001，触发器 / 函数集合与第一次完全一致（往返幂等）", f"exit={code}")

    def mut_drop_shared(conn):
        conn.exec_driver_sql("DROP FUNCTION rednote_reject_mutation() CASCADE")

    def judge_shared_tmp():
        conn = tmp_engine.connect()
        try:
            mut_drop_shared(conn)
            return j_shared(conn, set(trig_b))
        finally:
            conn.rollback()
            conn.close()

    with tmp_engine.connect() as c:
        ok, detail = j_shared(c, set(trig_b))
        c.rollback()
    emit(ok, "[D] 临时库（正向）：共用函数在、downgrade 基线里的其它包触发器全在", detail)
    rev("[V13] 反向：一个坏 downgrade 误删 rednote_reject_mutation(CASCADE) → 跨包共存判据报红", judge_shared_tmp)
finally:
    if tmp_engine is not None:
        tmp_engine.dispose()
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{TMP_NAME}" WITH (FORCE)'))
    except Exception as exc:
        print(f"# 临时库清理失败 {TMP_NAME}: {exc}")
    admin.dispose()

with engine.connect() as c:
    ok, detail = j_shared(c, BASELINE_TRIGGERS or set())
    c.rollback()
emit(BASELINE_TRIGGERS is not None and ok, "[X] 目标库（head）上：共用函数在，且临时库 downgrade 基线里的其它包触发器在目标库全部存在", detail)

# =============================================================================
# Z：零残留 + 变异已回滚（结构复核）
# =============================================================================
with engine.connect() as conn:
    for table, join in ((ROUNDS, "t JOIN projects p ON p.id = t.project_id"),
                        (SLOTS, "t JOIN trial_rounds r ON r.id = t.round_id JOIN projects p ON p.id = r.project_id"),
                        (DECISIONS, "t JOIN trial_rounds r ON r.id = t.round_id JOIN projects p ON p.id = r.project_id")):
        n = conn.execute(text(f"SELECT count(*) FROM {table} {join} WHERE p.name = :n"), {"n": PROBE_PROJECT_NAME}).scalar_one()
        emit(n == 0, f"[Z] 零残留 {table}（探针项目）", f"{n} 行")
    n = conn.execute(text("SELECT count(*) FROM projects WHERE name=:n"), {"n": PROBE_PROJECT_NAME}).scalar_one()
    emit(n == 0, "[Z] 零残留 projects（探针）", f"{n} 行")
    n = conn.execute(text("SELECT count(*) FROM users WHERE issuer=:iss"), {"iss": PROBE_ISSUER}).scalar_one()
    emit(n == 0, "[Z] 零残留 users（探针）", f"{n} 行")
    now_counts = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in START_COUNTS}
    emit(now_counts == START_COUNTS, "[Z] 全程前后目标库 3 表 + projects/users/content_items/version_nodes 行数不变", f"{now_counts}")
    n = conn.execute(text("SELECT count(*) FROM pg_database WHERE datname LIKE :p"), {"p": "rednote_verify_wp31_tmp_%"}).scalar_one()
    emit(n == 0, "[Z] 一次性临时库已 DROP", f"{n} 个残留")
    for label, (jok, jdetail) in (
        ("CHECK 29 条未被反向验证的 DDL 变异改动", j_checks(conn)),
        ("两个部分唯一索引 WHERE 未被变异改动", j_where(conn)),
        ("10 条 FK 仍全 RESTRICT 无 DEFERRABLE", j_fk(conn)),
        ("6 个触发器仍在", j_triggers(conn)),
        ("迁移矩阵函数体仍与 ROUND_TRANSITIONS 一致", j_matrix(conn)),
    ):
        emit(jok, f"[Z] 变异已回滚：{label}", jdetail)
    conn.rollback()

print(f"RESULT_EMITTED (期望 {EXPECTED_CHECKS} 条)")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def collect_wp31() -> tuple[Path, bool]:
    env = os.environ.copy()
    env.update(
        {
            "NO_PROXY": "*",
            "no_proxy": "*",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "PYTHONPATH": str(BACKEND_DIR / "src"),
            "WP31_REPO_ROOT": str(REPO_ROOT),
            DB_ENV: os.environ.get(DB_ENV, DEFAULT_DB),
        }
    )
    script = INSPECT_SCRIPT.replace("__EXPECTED__", str(EXPECTED_CHECKS))
    completed = subprocess.run(
        [PYTHON, "-c", script],
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
        "# WP31 试产关口 / 淘汰补产：3 表逐列 / 29 CHECK / 10 FK / 5 唯一 + 2 部分唯一索引 /"
        " 6 触发器 + 5 自有函数 / 迁移矩阵反解析 / 5x4 状态迁移全矩阵 / 不可变与 write-once /"
        " GATE_PASSED 与决定双向 / 跨包共存 / 9 路由 / trial_gate_status 接缝纯读 /"
        " 13 条反向验证 / 临时库迁移往返 / 探针零残留",
        f"# 生成时间: {_now()}",
        f"# 目标库: {env[DB_ENV]}（迁移从空库建出；非测试夹具建表）",
        "# 命令: cd services/backend && python -c '<information_schema + pg_catalog + 试写 + 试插 + 回滚的 DDL 变异>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp31-trial-gate.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(lines[-1])
    complete = len(checks) >= EXPECTED_CHECKS
    exact = len(checks) == EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete and exact)


def main() -> int:
    path, ok = collect_wp31()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
