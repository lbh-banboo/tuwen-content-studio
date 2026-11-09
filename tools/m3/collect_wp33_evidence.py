#!/usr/bin/env python
"""WP33 证据采集脚本（重复检查 / 质量报告）。

产出的文件（落在 `evidence/m3/`）：

| 文件 | 内容 |
|---|---|
| `wp33-quality-reports.log` | [M] 迁移 head 真 CHECK（不早于 `d4f33c0de004`）+ 迁移文件机械判据（只 import `quality.ddl` / 取值域是化石字面量 / downgrade 不点名共用函数）；[S] 7 张表逐列 / 可空 / 无浮点列、约束与索引逐名对账模型（CHECK / FK / UQ / PK / 索引）、FK 全 RESTRICT 与 2 条延迟 FK、13 个触发器逐名（6 不可变 + 7 延迟一致性）与 7 个自有函数、阈值不落 CHECK；[F] 纯函数（规范化 / 2-gram / Jaccard 千分比 / 交叉相乘边界 / 缺失不填 0 / 7 维度 / 没算 != 算了得 0）；[O] OP-228 占位阈值如实标注 PLACEHOLDER；[P] 行为探针（独立事务 + rollback）：不可变 UPDATE / DELETE 被拒（23001）、7 个延迟一致性触发器逐个被各自的坏数据触发（23514，按消息区分）、CHECK / UQ / FK 阴性探针（核对约束名）；[R] 路由对账；[C] 静态源码守卫（AST）；[D] 一次性临时库 upgrade -> downgrade -> upgrade 往返；[V] 反向验证（错误实现 / 错误数据下判据必须报红）；[T] 本包 pytest（`test_quality_wp33.py` + `test_quality_wp33_api.py`）在目标库上全绿；[Z] 探针零残留 |

## 阈值是占位（OP-228）——不是产品口径

「相似度多少算重复」「多大比例重复算告警」全仓没有需求依据（OP-228，用户尚未给数值）。
`quality/types.py` 里的 950‰ / 500‰ 是 **PLACEHOLDER**，每份报告落库快照，`ALERT` 只是
「相对占位阈值的提示」，不得作为阻断条件。本脚本里所有出现具体阈值数字的探针，
只用来证明**机制**（边界 / 快照 / 不落 CHECK），**不构成**对业务阈值的任何验证。

## 目标库与迁移

目标库 `rednote_verify_wp33`（本脚本自建：`CREATE DATABASE` + `CREATE EXTENSION vector` +
`alembic upgrade head`；库名写死，不可用环境变量覆盖，且只对这一个名字做 DROP / CREATE）。
每次运行都 DROP 重建它，保证它是「迁移从空库建出」的，而不是上次 pytest 夹具 `create_all` 的产物。
不碰共享库 `rednote_test` 与其它任何已有库。迁移 head 判据是**真 CHECK**（不早于 `d4f33c0de004`）。

## 与仓库纪律对账

1. 期望条数实测回填，并用 `exact = len(checks) == EXPECTED_CHECKS` 防期望值偏低恒真。
2. 不可变表的探针永不 commit：每条探针独立连接 + 无条件 `rollback()`。
3. CHECK 类探针核对 `diag.constraint_name`；触发器类 23514 没有约束名，按消息区分。
4. 子进程一律 `PYTHONDONTWRITEBYTECODE=1`、pytest `-p no:cacheprovider`，不在真树里写 `.pyc` / 缓存
   （`credibility_reverse` 会校验真实代码树未被触碰）。
5. 本脚本不用 importlib 动态加载；若将来要加，必须在 `exec_module` 前 `sys.modules[spec.name] = module`。
6. 内联脚本只允许单行注释且不能出现三引号（ISS-45），由 `check_evidence_scripts.py` 机械核对。

## 刻意没做的（如实登记）

- 6 张不可变表里，`quality_similarity_pairs` / `quality_media_alignments` / `quality_report_artifacts`
  的**不可变行为**（UPDATE / DELETE 被拒）没有在本脚本里造行逐个探，只对账了触发器存在且指向
  `rednote_reject_mutation`；行为层由 pytest（[T]）覆盖。
- 服务层语义（幂等 / 投影 CAS / 阈值快照不改写历史 / E11 边）只由 pytest（[T]）覆盖。
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "services" / "backend"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"

#: 期望条数（与内联脚本里的 EXPECTED_CHECKS 保持同值，**实测回填**；
#: 总数 = 内联脚本条数 + 外层 [T] pytest 条数）。
EXPECTED_CHECKS: int = 102
INLINE_EXPECTED: int = 99
PYTEST_EXPECTED_PASSED: int = 145
PYTEST_EXPECTED_SKIPPED: int = 0

DB_NAME = "rednote_verify_wp33"
DB_URL = f"postgresql+psycopg://rednote@localhost:5432/{DB_NAME}"
# pg 工具路径只允许 `tools/ops/drill_db.py::DEFAULT_PG_BIN` 一处定义（回退知识不许复制），这里从它取。
_spec = importlib.util.spec_from_file_location("drill_db", REPO_ROOT / "tools" / "ops" / "drill_db.py")
drill_db = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = drill_db
_spec.loader.exec_module(drill_db)
PG_BIN = Path(drill_db.DEFAULT_PG_BIN)
PSQL = str(PG_BIN / "psql")

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

TEST_FILES = ("tests/test_quality_wp33.py", "tests/test_quality_wp33_api.py")

INSPECT_SCRIPT = r"""
# 内联脚本：只允许单行注释，且不能出现三引号（三引号会提前终止外层包裹，见 ISS-45）。

import ast
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

import rednote.modules.baseline.models  # noqa: F401
import rednote.modules.content.models  # noqa: F401
import rednote.modules.files.models  # noqa: F401
import rednote.modules.gateway.models  # noqa: F401
import rednote.modules.identity.models  # noqa: F401
import rednote.modules.jobs.models  # noqa: F401
import rednote.modules.media.models  # noqa: F401
import rednote.modules.projects.models  # noqa: F401
import rednote.modules.prompts.models  # noqa: F401
import rednote.modules.versions.models  # noqa: F401
from rednote.modules.quality import api as q_api
from rednote.modules.quality import ddl as q_ddl
from rednote.modules.quality import dimensions as q_dim
from rednote.modules.quality import models as q_models
from rednote.modules.quality import similarity as q_sim
from rednote.modules.quality import types as q_types
from rednote.platform.base import Base

EXPECTED_CHECKS = __EXPECTED__

DB = os.environ["WP33_DB_URL"]
engine = create_engine(DB)
REPO = Path(os.environ["WP33_REPO_ROOT"])
QDIR = Path(q_models.__file__).resolve().parent

WP33_MIGRATION_REVISION = "d4f33c0de004"
WP33_DOWN_REVISION = "c3f34c0de003"
MIGRATION_FILE = REPO / "migrations" / "versions" / "20261024_0650_d4f33c0de004_wp33_quality_reports.py"

OWN_TABLES = tuple(q_models.OWN_TABLES)
IMMUTABLE_TABLES = tuple(q_models.IMMUTABLE_TABLES)
IMMUTABLE_SQLSTATE = "23001"
CONSISTENCY_SQLSTATE = "23514"
UNIQUE_SQLSTATE = "23505"
FK_SQLSTATE = "23503"
PROBE_ISSUER = "wp33-probe"
PROBE_PROJECT_NAME = "WP33 探针项目"
PROBE_PROJECT_PREFIX = "WP33PROBE"
HA = "a" * 64
HB = "b" * 64

EXPECTED_ROUTE_COUNT = 7
EXPECTED_TRIGGER_COUNT = 13
EXPECTED_FUNCTION_COUNT = 7
DEFERRED_FKS = (
    "fk_quality_reports_current_version_id_quality_report_versions",
    "fk_quality_report_versions_id_version_nodes",
)


def emit(ok, label, detail):
    print(f"CHECK {'OK  ' if ok else 'FAIL'} {label}: {detail}")


def sqlstate(exc):
    return getattr(exc.orig, "sqlstate", None)


def constraint_of(exc):
    return getattr(getattr(exc.orig, "diag", None), "constraint_name", None)


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


def rows(conn, sql, **params):
    return conn.execute(text(sql), params).fetchall()


# --- 模型侧名字集合（迁移 vs 模型对账的「模型」一侧）-------------------------------
def model_names(table_name):
    table = Base.metadata.tables[table_name]
    out = {"c": set(), "f": set(), "u": set(), "p": set()}
    for con in table.constraints:
        kind = type(con).__name__
        name = str(con.name) if con.name is not None else None
        if name is None:
            continue
        if kind == "CheckConstraint":
            out["c"].add(name)
        elif kind == "ForeignKeyConstraint":
            out["f"].add(name)
        elif kind == "UniqueConstraint":
            out["u"].add(name)
        elif kind == "PrimaryKeyConstraint":
            out["p"].add(name)
    out["i"] = {str(ix.name) for ix in table.indexes}
    return out


def db_con_names(conn, table_name, contype):
    return {
        r[0]
        for r in rows(
            conn,
            "SELECT conname FROM pg_constraint WHERE contype = :t"
            " AND conrelid = CAST(:tb AS regclass)",
            t=contype,
            tb=table_name,
        )
    }


def db_index_names(conn, table_name):
    # 唯一约束背后的索引与主键索引不算「独立索引」
    return {
        r[0]
        for r in rows(
            conn,
            "SELECT i.relname FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid"
            " WHERE x.indrelid = CAST(:tb AS regclass)"
            " AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = x.indexrelid)",
            tb=table_name,
        )
    }


def j_names(conn, kind, contype):
    bad = []
    total = 0
    for table in OWN_TABLES:
        want = model_names(table)[kind]
        got = db_con_names(conn, table, contype)
        total += len(got)
        if want != got:
            bad.append(f"{table}: 模型有库无 {sorted(want - got)}；库有模型无 {sorted(got - want)}")
    return (not bad and total > 0), (f"共 {total} 个，逐名一致" if not bad else "；".join(bad))


def j_indexes(conn):
    bad = []
    total = 0
    for table in OWN_TABLES:
        want = model_names(table)["i"]
        got = db_index_names(conn, table)
        total += len(got)
        if want != got:
            bad.append(f"{table}: 模型有库无 {sorted(want - got)}；库有模型无 {sorted(got - want)}")
    return (not bad), (f"共 {total} 个独立索引，逐名一致" if not bad else "；".join(bad))


def j_columns(conn):
    bad = []
    total = 0
    for table in OWN_TABLES:
        mt = Base.metadata.tables[table]
        want = {c.name: bool(c.nullable) for c in mt.columns}
        got = {
            r[0]: r[1] == "YES"
            for r in rows(
                conn,
                "SELECT column_name, is_nullable FROM information_schema.columns"
                " WHERE table_schema='public' AND table_name = :t",
                t=table,
            )
        }
        total += len(got)
        if want != got:
            bad.append(f"{table}: 列名 / 可空不一致 {sorted(set(want.items()) ^ set(got.items()))}")
    return (not bad), (f"共 {total} 列，列名与可空逐列一致" if not bad else "；".join(bad))


def j_no_float(conn):
    r = rows(
        conn,
        "SELECT table_name, column_name, data_type FROM information_schema.columns"
        " WHERE table_schema='public' AND table_name = ANY(:ts)"
        " AND data_type IN ('real', 'double precision', 'numeric')",
        ts=list(OWN_TABLES),
    )
    return (not r), (f"无浮点 / numeric 列" if not r else f"{r}")


def j_fk_restrict(conn):
    r = rows(
        conn,
        "SELECT conname, confdeltype FROM pg_constraint"
        " WHERE contype='f' AND conrelid = ANY(CAST(:ts AS regclass[]))",
        ts=list(OWN_TABLES),
    )
    bad = [c for c, d in r if d != "r"]
    return (len(r) > 0 and not bad), (f"{len(r)} 条 FK 全部 ON DELETE RESTRICT" if not bad else f"非 RESTRICT：{bad}")


def j_deferred_fks(conn):
    r = {
        n: (d, i)
        for n, d, i in rows(
            conn,
            "SELECT conname, condeferrable, condeferred FROM pg_constraint WHERE conname = ANY(:n)",
            n=list(DEFERRED_FKS),
        )
    }
    ok = set(r) == set(DEFERRED_FKS) and all(v == (True, True) for v in r.values())
    return ok, f"{r}"


def j_threshold_not_in_check(conn):
    defs = rows(
        conn,
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE contype='c' AND conrelid = ANY(CAST(:ts AS regclass[]))",
        ts=list(OWN_TABLES),
    )
    bad = [n for n, d in defs if str(q_types.PLACEHOLDER_PAIR_THRESHOLD_PERMILLE) in d
           or str(q_types.PLACEHOLDER_ALERT_THRESHOLD_PERMILLE) in d]
    return (len(defs) > 0 and not bad), (f"{len(defs)} 条 CHECK 都不含占位阈值字面量" if not bad else f"含占位阈值：{bad}")


def trigger_rows(conn):
    return rows(
        conn,
        "SELECT t.tgname, c.relname, p.proname, t.tgdeferrable, t.tginitdeferred"
        " FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
        " JOIN pg_proc p ON p.oid = t.tgfoid"
        " WHERE NOT t.tgisinternal AND t.tgname = ANY(:n)",
        n=list(q_ddl.trigger_names()),
    )


def j_triggers(conn):
    got = trigger_rows(conn)
    names = {r[0] for r in got}
    want = set(q_ddl.trigger_names())
    ok = names == want and len(got) == EXPECTED_TRIGGER_COUNT
    return ok, f"{len(got)}/{EXPECTED_TRIGGER_COUNT}；缺 {sorted(want - names)}"


def j_immutable_triggers(conn):
    imm = set(q_ddl.immutable_trigger_names())
    bad = [r[0] for r in trigger_rows(conn) if r[0] in imm and r[2] != "rednote_reject_mutation"]
    n = sum(1 for r in trigger_rows(conn) if r[0] in imm)
    return (n == 6 and not bad), f"{n}/6 个不可变触发器，函数均为 rednote_reject_mutation；偏离 {bad}"


def j_consistency_triggers(conn):
    cons = set(q_ddl.consistency_trigger_names())
    got = [r for r in trigger_rows(conn) if r[0] in cons]
    bad = [r[0] for r in got if not (r[3] and r[4])]
    return (len(got) == 7 and not bad), f"{len(got)}/7 个一致性触发器，均 DEFERRABLE INITIALLY DEFERRED；偏离 {bad}"


def j_functions(conn):
    got = {
        r[0]
        for r in rows(
            conn,
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname='public' AND p.proname = ANY(:n)",
            n=list(q_ddl.OWN_FUNCTIONS) + ["rednote_reject_mutation"],
        )
    }
    want = set(q_ddl.OWN_FUNCTIONS) | {"rednote_reject_mutation"}
    return (got == want and len(q_ddl.OWN_FUNCTIONS) == EXPECTED_FUNCTION_COUNT), (
        f"自有函数 {len(q_ddl.OWN_FUNCTIONS)}/7 + 共用函数在；缺 {sorted(want - got)}"
    )


# --- 静态源码判据（可对内存里的坏源码复用）-------------------------------------------
def imports_of(source):
    mods = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
    return mods


def imports_full(source):
    mods = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.extend(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
    return mods


def j_migration_imports(source):
    mods = [m for m in imports_full(source) if m.startswith("rednote")]
    ok = bool(mods) and all(m == "rednote.modules.quality.ddl" for m in mods)
    return ok, f"rednote 导入 {sorted(set(mods))}"


def downgrade_code(source):
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                body = body[1:]
            return "\n".join(ast.unparse(b) for b in body)
    return ""


def j_downgrade(source):
    code = downgrade_code(source)
    ok = bool(code) and "rednote_reject_mutation" not in code and "consistency_function_names" in code
    return ok, f"downgrade 执行代码 {len(code)} 字符；点名共用函数={'rednote_reject_mutation' in code}"


def j_placeholder_source(types_source, service_source):
    ok = (
        'THRESHOLD_BASIS_PLACEHOLDER: Final[str] = "PLACEHOLDER_PENDING_OP-228"' in types_source
        and "待 OP-228" in types_source
        and "不得作为阻断条件" in types_source
        and '"thresholdBasis": THRESHOLD_BASIS_PLACEHOLDER' in service_source
    )
    return ok, "types.py 标 PLACEHOLDER / 待 OP-228 / 不得作为阻断条件；service 响应带 thresholdBasis"


def j_pure_modules(sim_source, dim_source, types_source):
    bad = []
    for label, src, allowed in (
        ("similarity", sim_source, {"rednote.modules.quality.types", "unicodedata", "__future__"}),
        ("dimensions", dim_source, {"rednote.modules.quality.types", "rednote.modules.quality.similarity",
                                    "uuid", "collections.abc", "__future__"}),
        ("types", types_source, {"uuid", "dataclasses", "typing", "__future__"}),
    ):
        extra = set(imports_of(src)) - allowed
        if extra:
            bad.append(f"{label} 多余导入 {sorted(extra)}")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                bad.append(f"{label} 出现 float 字面量 {node.value}")
            if isinstance(node, ast.Attribute) and node.attr == "split":
                bad.append(f"{label} 出现 .split（切段只许经 media 纯函数）")
            if isinstance(node, ast.Name) and node.id == "float":
                bad.append(f"{label} 出现 float 名字")
    return (not bad), ("三个纯模块导入白名单 + 无 float + 无 .split" if not bad else "；".join(bad))


# --- 探针夹具 ------------------------------------------------------------------
def mk_project(conn, uid, tag):
    brand = uuid.uuid4()
    prod = uuid.uuid4()
    proj = uuid.uuid4()
    conn.execute(text("INSERT INTO brands (id, name, status) VALUES (:b, :n, 'ACTIVE')"),
                 {"b": str(brand), "n": f"WP33 品牌 {tag} {str(brand)[:8]}"})
    conn.execute(
        text("INSERT INTO products (id, brand_id, name, model_code, status)"
             " VALUES (:p, :b, :n, :c, 'ACTIVE')"),
        {"p": str(prod), "b": str(brand), "n": f"WP33 产品 {str(prod)[:8]}", "c": f"WP33-{str(prod)[:8]}"})
    conn.execute(
        text("INSERT INTO projects (id, project_code, name, brand_id, product_id,"
             " owner_id, target_delivery_count, due_date)"
             " VALUES (:p, :c, :n, :b, :pr, :o, 1, (now() + interval '30 days')::date)"),
        {"p": str(proj), "c": f"{PROBE_PROJECT_PREFIX}{str(proj)[:8]}", "n": PROBE_PROJECT_NAME,
         "b": str(brand), "pr": str(prod), "o": str(uid)})
    return str(proj)


def mk_content(conn, project):
    cid = str(uuid.uuid4())
    conn.execute(
        text("INSERT INTO content_items (id, project_id, state, current_version_no, lock_version)"
             " VALUES (:c, :p, 'DRAFT', 0, 0)"),
        {"c": cid, "p": project})
    return cid


def mk_world(conn):
    w = {}
    uid = uuid.uuid4()
    conn.execute(
        text("INSERT INTO users (id, issuer, subject, display_name, role, status)"
             " VALUES (:u, :iss, :sub, 'WP33 探针', 'ADMIN', 'ACTIVE')"),
        {"u": str(uid), "iss": PROBE_ISSUER, "sub": f"probe-{uid}"})
    w["user"] = str(uid)
    w["project"] = mk_project(conn, uid, "A")
    w["project2"] = mk_project(conn, uid, "B")
    contents = sorted(mk_content(conn, w["project"]) for _ in range(2))
    w["a"], w["b"] = contents
    w["other"] = mk_content(conn, w["project2"])
    return w


TEXT_DIMS = tuple(q_types.TEXT_DIMENSIONS)
ALL_DIMS = tuple(q_types.DIMENSIONS)


def default_dims():
    out = {}
    for d in ALL_DIMS:
        reason = q_types.ALWAYS_NOT_COMPUTED.get(d, q_types.NOT_COMPUTED_REASON_INSUFFICIENT_ITEMS)
        out[d] = {"status": "NOT_COMPUTED", "reason": reason}
    return out


def computed(compared=2, flagged=0, pair=950, alert=500, maxs=0, verdict="OK"):
    return {"status": "COMPUTED", "reason": None, "compared": compared, "flagged": flagged,
            "pair": pair, "alert": alert, "max": maxs, "verdict": verdict}


def mk_report(conn, w, rev=1, scope_kind="CONTENT_SET", batch=None, digest=None):
    rid = str(uuid.uuid4())
    conn.execute(
        text("INSERT INTO quality_reports (id, project_id, scope_kind, scope_digest, batch_id)"
             " VALUES (:i, :p, :k, :d, :b)"),
        {"i": rid, "p": w["project"], "k": scope_kind, "d": digest or uuid.uuid4().hex * 2, "b": batch})
    return rid


def mk_version(conn, w, report=None, rev=1, dims=None, items="default", counts=None,
               node_hash=None, ver_hash=None):
    report = report or mk_report(conn, w)
    dims = dims if dims is not None else default_dims()
    if items == "default":
        items = [(w["a"], 0), (w["b"], 1)]
    vid = str(uuid.uuid4())
    h = ver_hash or ("c" * 63 + str(rev))
    conn.execute(
        text("INSERT INTO version_nodes (id, entity_type, entity_id, project_id,"
             " revision_no, payload_hash, schema_version)"
             " VALUES (:n, 'REPORT', :e, :p, :r, :h, 1)"),
        {"n": vid, "e": report, "p": w["project"], "r": rev, "h": node_hash or h})
    ok = sum(1 for v in dims.values() if v.get("verdict") == "OK")
    alert = sum(1 for v in dims.values() if v.get("verdict") == "ALERT")
    nc = sum(1 for v in dims.values() if v["status"] == "NOT_COMPUTED")
    inc, exc = 0, len(items)
    if counts is not None:
        ok, alert, nc = counts.get("dims", (ok, alert, nc))
        inc, exc = counts.get("items", (inc, exc))
    total = inc + exc
    conn.execute(
        text("INSERT INTO quality_report_versions (id, report_id, revision_no, input_fingerprint,"
             " algo_version, schema_version, item_count, included_item_count, excluded_item_count,"
             " ok_dimension_count, alert_dimension_count, not_computed_dimension_count, outcome,"
             " payload, payload_hash, started_at, finished_at)"
             " VALUES (:i, :r, :rev, :fp, 1, 1, :t, :inc, :exc, :ok, :al, :nc, :out,"
             " CAST('{}' AS jsonb), :h, now(), now())"),
        {"i": vid, "r": report, "rev": rev, "fp": HA, "t": total, "inc": inc, "exc": exc,
         "ok": ok, "al": alert, "nc": nc, "out": "ALERTED" if alert else "NO_ALERT", "h": h})
    for dim, spec in dims.items():
        conn.execute(
            text("INSERT INTO quality_dimension_results (report_version_id, dimension, status,"
                 " not_computed_reason, verdict, compared_item_count, flagged_item_count,"
                 " pair_threshold_permille, alert_threshold_permille, max_similarity_permille)"
                 " VALUES (:v, :d, :s, :r, :vd, :c, :f, :p, :a, :m)"),
            {"v": vid, "d": dim, "s": spec["status"], "r": spec.get("reason"), "vd": spec.get("verdict"),
             "c": spec.get("compared"), "f": spec.get("flagged"), "p": spec.get("pair"),
             "a": spec.get("alert"), "m": spec.get("max")})
    for content, pos in items:
        conn.execute(
            text("INSERT INTO quality_report_items (report_version_id, content_item_id, position,"
                 " status, exclude_reason) VALUES (:v, :c, :p, 'EXCLUDED', 'NO_VERSION')"),
            {"v": vid, "c": content, "p": pos})
    return {"report": report, "version": vid}


def settle(conn):
    # 延迟约束触发器在 COMMIT 时才检查；探针不提交，所以显式强制检查
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def probe(build, mutate=None):
    # 返回 (是否出错, sqlstate, 约束名, 消息)；独立连接 + 无条件 rollback
    conn = engine.connect()
    try:
        if mutate is not None:
            mutate(conn)
        w = mk_world(conn)
        try:
            build(conn, w)
            settle(conn)
            return False, None, None, ""
        except DBAPIError as exc:
            return True, sqlstate(exc), constraint_of(exc), str(exc.orig)
    finally:
        conn.rollback()
        conn.close()


def expect(build, state, contains=None, constraint=None, mutate=None):
    raised, got_state, got_con, msg = probe(build, mutate)
    ok = raised and got_state == state
    if contains is not None:
        ok = ok and contains in msg
    if constraint is not None:
        ok = ok and got_con == constraint
    return ok, f"raised={raised} sqlstate={got_state} constraint={got_con} msg={msg.splitlines()[0][:90] if msg else ''}"


# --- 具体坏数据构造 --------------------------------------------------------------
def b_valid(conn, w):
    v = mk_version(conn, w)
    conn.execute(text("UPDATE quality_reports SET current_version_id = :v, current_revision_no = 1 WHERE id = :r"),
                 {"v": v["version"], "r": v["report"]})


def b_valid_computed(conn, w):
    dims = default_dims()
    dims["TITLE"] = computed()
    mk_version(conn, w, dims=dims)


def b_update_version(conn, w):
    v = mk_version(conn, w)
    settle(conn)
    conn.execute(text("UPDATE quality_report_versions SET outcome = 'ALERTED' WHERE id = :v"), {"v": v["version"]})


def b_delete_item(conn, w):
    mk_version(conn, w)
    settle(conn)
    conn.execute(text("DELETE FROM quality_report_items WHERE content_item_id = :c"), {"c": w["a"]})


def b_update_dim(conn, w):
    mk_version(conn, w)
    settle(conn)
    conn.execute(text("UPDATE quality_dimension_results SET not_computed_reason = 'X' WHERE dimension = 'TITLE'"
                      " AND report_version_id IN (SELECT id FROM quality_report_versions)"))


def b_six_dims(conn, w):
    dims = default_dims()
    del dims["SCENE_CONCENTRATION"]
    mk_version(conn, w, dims=dims, counts={"dims": (0, 0, 7)})


def b_dim_counts_mismatch(conn, w):
    mk_version(conn, w, counts={"dims": (1, 0, 6)})


def b_item_counts_mismatch(conn, w):
    mk_version(conn, w, counts={"items": (1, 1)})


def b_orphan_node(conn, w):
    rid = mk_report(conn, w)
    node = str(uuid.uuid4())
    conn.execute(
        text("INSERT INTO version_nodes (id, entity_type, entity_id, project_id, revision_no,"
             " payload_hash, schema_version) VALUES (:n, 'REPORT', :e, :p, 1, :h, 1)"),
        {"n": node, "e": rid, "p": w["project"], "h": HB})


def b_foreign_report_node(conn, w):
    node = str(uuid.uuid4())
    conn.execute(
        text("INSERT INTO version_nodes (id, entity_type, entity_id, project_id, revision_no,"
             " payload_hash, schema_version) VALUES (:n, 'REPORT', :e, :p, 1, :h, 1)"),
        {"n": node, "e": str(uuid.uuid4()), "p": w["project"], "h": HB})


def b_hash_mismatch(conn, w):
    mk_version(conn, w, node_hash="d" * 64)


def b_cross_project_item(conn, w):
    mk_version(conn, w, items=[(w["a"], 0), (w["other"], 1)])


def b_flagged_without_pairs(conn, w):
    dims = default_dims()
    dims["TITLE"] = computed(compared=2, flagged=2, maxs=1000, verdict="ALERT")
    mk_version(conn, w, dims=dims)


def b_pair_on_not_computed(conn, w):
    v = mk_version(conn, w)
    conn.execute(
        text("INSERT INTO quality_similarity_pairs (report_version_id, dimension, content_item_a_id,"
             " content_item_b_id, intersection_count, union_count, similarity_permille)"
             " VALUES (:v, 'TITLE', :a, :b, 1, 1, 1000)"),
        {"v": v["version"], "a": w["a"], "b": w["b"]})


def b_pair_on_excluded(conn, w):
    dims = default_dims()
    dims["TITLE"] = computed(compared=2, flagged=2, maxs=1000, verdict="ALERT")
    v = mk_version(conn, w, dims=dims)
    conn.execute(
        text("INSERT INTO quality_similarity_pairs (report_version_id, dimension, content_item_a_id,"
             " content_item_b_id, intersection_count, union_count, similarity_permille)"
             " VALUES (:v, 'TITLE', :a, :b, 1, 1, 1000)"),
        {"v": v["version"], "a": w["a"], "b": w["b"]})


def b_alignment_no_version(conn, w):
    v = mk_version(conn, w)
    conn.execute(
        text("INSERT INTO quality_media_alignments (report_version_id, content_item_id, sort_order,"
             " expected_ref, verdict) VALUES (:v, :c, 0, 'p1', 'MISSING')"),
        {"v": v["version"], "c": w["a"]})


def b_current_of_other_report(conn, w):
    v_other = mk_version(conn, w)
    own = mk_report(conn, w)
    conn.execute(text("UPDATE quality_reports SET current_version_id = :v, current_revision_no = 1 WHERE id = :r"),
                 {"v": v_other["version"], "r": own})


def b_bad_scope_kind(conn, w):
    mk_report(conn, w, scope_kind="BOGUS")


def b_batch_unpaired(conn, w):
    mk_report(conn, w, scope_kind="BATCH", batch=None)


def b_dim_nc_with_count(conn, w):
    dims = default_dims()
    dims["TITLE"] = {"status": "NOT_COMPUTED", "reason": "INSUFFICIENT_ITEMS", "flagged": 0}
    mk_version(conn, w, dims=dims)


def b_dim_flagged_one(conn, w):
    dims = default_dims()
    dims["TITLE"] = computed(compared=2, flagged=1, maxs=1000, verdict="ALERT")
    mk_version(conn, w, dims=dims)


def b_dim_unimplemented_computed(conn, w):
    dims = default_dims()
    dims["STRUCTURE"] = computed()
    mk_version(conn, w, dims=dims)


def b_dim_verdict_wrong(conn, w):
    dims = default_dims()
    dims["TITLE"] = computed(compared=2, flagged=0, verdict="ALERT")
    mk_version(conn, w, dims=dims)


def b_dup_scope_digest(conn, w):
    mk_report(conn, w, digest="e" * 64)
    mk_report(conn, w, digest="e" * 64)


def b_item_fk(conn, w):
    v = mk_version(conn, w, items=[])
    conn.execute(
        text("INSERT INTO quality_report_items (report_version_id, content_item_id, position,"
             " status, exclude_reason) VALUES (:v, :c, 0, 'EXCLUDED', 'NO_VERSION')"),
        {"v": v["version"], "c": str(uuid.uuid4())})


PROBES = (
    ("[P] 不可变 UPDATE：quality_report_versions", b_update_version, IMMUTABLE_SQLSTATE, None, None),
    ("[P] 不可变 DELETE：quality_report_items", b_delete_item, IMMUTABLE_SQLSTATE, None, None),
    ("[P] 不可变 UPDATE：quality_dimension_results", b_update_dim, IMMUTABLE_SQLSTATE, None, None),
    ("[P] 一致性：版本缺维度行（不是恰 7 行）", b_six_dims, CONSISTENCY_SQLSTATE, "必须恰为 7", None),
    ("[P] 一致性：维度汇总计数与 7 个维度行实际分布不符", b_dim_counts_mismatch, CONSISTENCY_SQLSTATE, "维度汇总计数", None),
    ("[P] 一致性：纳入 / 排除篇数与 items 实际分布不符", b_item_counts_mismatch, CONSISTENCY_SQLSTATE, "纳入 / 排除篇数", None),
    ("[P] 一致性：孤立 REPORT 节点（OP-88 反向）", b_orphan_node, CONSISTENCY_SQLSTATE, "孤立节点", None),
    ("[P] 一致性：注册表节点 payload_hash 与领域行不一致", b_hash_mismatch, CONSISTENCY_SQLSTATE, "payload_hash", None),
    ("[P] 一致性：报告成员内容属于别的项目", b_cross_project_item, CONSISTENCY_SQLSTATE, "属于项目", None),
    ("[P] 一致性：flagged_item_count 与相似对涉及篇数矛盾", b_flagged_without_pairs, CONSISTENCY_SQLSTATE, "flagged_item_count", None),
    ("[P] 一致性：相似对挂在 NOT_COMPUTED 维度上", b_pair_on_not_computed, CONSISTENCY_SQLSTATE, "NOT_COMPUTED", None),
    ("[P] 一致性：相似对涉及被排除的篇目", b_pair_on_excluded, CONSISTENCY_SQLSTATE, "INCLUDED", None),
    ("[P] 一致性：NO_VERSION 篇目有对位行", b_alignment_no_version, CONSISTENCY_SQLSTATE, "NO_VERSION", None),
    ("[P] 一致性：投影指向别的报告的版本", b_current_of_other_report, CONSISTENCY_SQLSTATE, "属于别的报告", None),
    ("[P] CHECK：scope_kind 取值域", b_bad_scope_kind, CONSISTENCY_SQLSTATE, None, "ck_quality_reports_scope_kind_valid"),
    ("[P] CHECK：BATCH 范围必须带 batch_id", b_batch_unpaired, CONSISTENCY_SQLSTATE, None, "ck_quality_reports_batch_id_paired"),
    ("[P] CHECK：没算（NOT_COMPUTED）的维度不得带数值（没算 != 算了得 0）", b_dim_nc_with_count, CONSISTENCY_SQLSTATE, None, "ck_quality_dimension_results_not_computed_columns_absent"),
    ("[P] CHECK：flagged 恰为 1 不可能（一对至少 2 篇）", b_dim_flagged_one, CONSISTENCY_SQLSTATE, None, "ck_quality_dimension_results_flagged_not_one"),
    ("[P] CHECK：STRUCTURE 等未实现维度不得 COMPUTED", b_dim_unimplemented_computed, CONSISTENCY_SQLSTATE, None, "ck_quality_dimension_results_unimplemented_not_computed"),
    ("[P] CHECK：verdict 与 flagged / 占位阈值快照推出的结论等价", b_dim_verdict_wrong, CONSISTENCY_SQLSTATE, None, "ck_quality_dimension_results_verdict_alert_equivalence"),
    ("[P] UQ：同项目同 scope_digest 只能一个报告系列", b_dup_scope_digest, UNIQUE_SQLSTATE, None, "uq_quality_reports_project_id_scope_digest"),
    ("[P] FK：报告成员指向不存在的内容", b_item_fk, FK_SQLSTATE, None, None),
)

# =============================================================================
# M：迁移
# =============================================================================
with engine.connect() as conn:
    has_alembic_table = conn.execute(text("SELECT to_regclass('public.alembic_version') IS NOT NULL")).scalar_one()
    current = None
    if has_alembic_table:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        current = row[0] if row else None
    emit(has_alembic_table and revision_is_at_least(current, WP33_MIGRATION_REVISION),
         "[M] 迁移 head 不早于 WP33 引入点（真 CHECK，不 SKIP）",
         f"current={current} 引入点={WP33_MIGRATION_REVISION}")
    conn.rollback()

mig_source = MIGRATION_FILE.read_text(encoding="utf-8")
ok, detail = j_migration_imports(mig_source)
emit(ok, "[M] 迁移只 import quality.ddl，不 import types / models", detail)
emit("'CONTENT_SET', 'PROJECT', 'BATCH'" in mig_source and "'ALERTED', 'NO_ALERT'" in mig_source
     and "'COMPUTED', 'NOT_COMPUTED'" in mig_source,
     "[M] 迁移里取值域是化石字面量", "scope_kind / outcome / 维度状态三组 IN 列表字面量都在迁移源码里")
emit(f"down_revision: str | Sequence[str] | None = '{WP33_DOWN_REVISION}'" in mig_source
     and f"revision: str = '{WP33_MIGRATION_REVISION}'" in mig_source,
     "[M] revision / down_revision 指向 WP34 迁移", f"{WP33_MIGRATION_REVISION} <- {WP33_DOWN_REVISION}")
ok, detail = j_downgrade(mig_source)
emit(ok, "[M] downgrade 只删 consistency_function_names，执行代码里不点名 rednote_reject_mutation", detail)
emit(tuple(q_ddl.OWN_FUNCTIONS) == tuple(q_ddl.consistency_function_names()) and len(q_ddl.OWN_FUNCTIONS) == 7
     and "rednote_reject_mutation" not in q_ddl.OWN_FUNCTIONS,
     "[M] ddl.OWN_FUNCTIONS 恰为本包 7 个函数，不含 rednote_reject_mutation", f"{len(q_ddl.OWN_FUNCTIONS)} 个")
chain = revision_chain()
emit(len(chain) >= 2 and revision_is_at_least(chain[0], WP33_MIGRATION_REVISION),
     "[M] 迁移链 head 不早于 WP33 引入点", f"head={chain[0]} 链长 {len(chain)}")

# =============================================================================
# S：结构
# =============================================================================
with engine.connect() as conn:
    n = conn.execute(
        text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name = ANY(:n)"),
        {"n": list(OWN_TABLES)}).scalar_one()
    emit(n == 7 and len(OWN_TABLES) == 7, "[S] 本包 7 张表存在", f"{n}/7（缺则先跑 alembic upgrade head）")
    emit(set(IMMUTABLE_TABLES) == set(OWN_TABLES) - {"quality_reports"} and len(IMMUTABLE_TABLES) == 6,
         "[S] 6 张不可变表 + 1 张可变投影表（quality_reports）", f"{IMMUTABLE_TABLES}")
    for label, fn in (
        ("列名 + 可空逐列对账模型", j_columns),
        ("无浮点 / numeric 列（相似度是整数千分比）", j_no_float),
        ("FK 全部 ON DELETE RESTRICT", j_fk_restrict),
        ("2 条延迟 FK（版本回指 + version_nodes）DEFERRABLE INITIALLY DEFERRED", j_deferred_fks),
        ("占位阈值不落 CHECK（不把 PLACEHOLDER 冻成化石）", j_threshold_not_in_check),
        ("13 个触发器逐名存在", j_triggers),
        ("6 个不可变触发器指向 rednote_reject_mutation", j_immutable_triggers),
        ("7 个一致性触发器均为延迟约束触发器", j_consistency_triggers),
        ("7 个自有函数 + 共用函数 rednote_reject_mutation 在", j_functions),
        ("独立索引逐名对账模型", j_indexes),
    ):
        ok, detail = fn(conn)
        emit(ok, f"[S] {label}", detail)
    for label, kind, contype in (("CHECK", "c", "c"), ("FK", "f", "f"), ("UNIQUE", "u", "u"), ("PK", "p", "p")):
        ok, detail = j_names(conn, kind, contype)
        emit(ok, f"[S] {label} 约束名逐名对账模型", detail)
    conn.rollback()

# =============================================================================
# F：纯函数
# =============================================================================
norm = q_sim.normalize("Ａ，b！ 1")
emit(norm == "ab1", "[F] normalize：NFKC + casefold + 删空白 / 标点 / 符号", repr(norm))
emit(q_sim.grams("") == frozenset() and q_sim.grams("，！") == frozenset(),
     "[F] 规范化后为空 → 空集（没有可比文本，不是相似度 0）", "空集")
emit(q_sim.grams("a") == frozenset({"a"}) and q_sim.grams("abc") == frozenset({"ab", "bc"}),
     "[F] 1 字取 1-gram，>=2 字取字符 2-gram", "ok")
emit(q_sim.pair_counts(frozenset(), frozenset({"ab"})) is None and q_sim.pair_counts(frozenset({"ab"}), frozenset()) is None,
     "[F] pair_counts：任一侧为空 → None（缺失不填 0）", "None")
emit(q_sim.pair_counts(q_sim.grams("你好世界"), q_sim.grams("你好世界")) == (3, 3)
     and q_sim.similarity_permille(3, 3) == 1000 and q_sim.similarity_permille(1, 3) == 333,
     "[F] 相同文本 1000 千分比；floor(1000*1/3)=333", "ok")
emit(q_sim.is_similar(19, 20, 950) and not q_sim.is_similar(18, 20, 950),
     "[F] is_similar 交叉相乘边界：恰好等于阈值算相似（机制验证，阈值数字是占位）", "19/20>=950 真；18/20 假")
emit(q_sim.alert_verdict_is_alert(2, 2, 500) and not q_sim.alert_verdict_is_alert(1, 2, 500),
     "[F] 维度告警严格大于（机制验证，阈值数字是占位）", "2/2 告警；1/2 恰等于不告警")
try:
    q_sim.alert_verdict_is_alert(0, 1, 500)
    raised = False
except ValueError:
    raised = True
emit(raised, "[F] compared < 2 的维度不得进入判定（应为 NOT_COMPUTED）", "ValueError")
one = {uuid.UUID(int=1): q_sim.grams("唯一一篇")}
r1 = q_dim.compute_text_dimension("TITLE", one, q_types.threshold_for("TITLE"))
emit(r1.status == "NOT_COMPUTED" and r1.not_computed_reason == "INSUFFICIENT_ITEMS" and r1.flagged_item_count is None
     and r1.verdict is None, "[F] 可比篇数 < 2 → NOT_COMPUTED(INSUFFICIENT_ITEMS)，数值列全 None", f"{r1.status}/{r1.flagged_item_count}")
two_same = {uuid.UUID(int=1): q_sim.grams("完全相同的标题文字"), uuid.UUID(int=2): q_sim.grams("完全相同的标题文字")}
two_diff = {uuid.UUID(int=1): q_sim.grams("春日露营装备清单"), uuid.UUID(int=2): q_sim.grams("冬季护肤保湿心得")}
rs = q_dim.compute_text_dimension("TITLE", two_same, q_types.threshold_for("TITLE"))
rd = q_dim.compute_text_dimension("TITLE", two_diff, q_types.threshold_for("TITLE"))
emit(rs.status == "COMPUTED" and rs.flagged_item_count == 2 and rs.verdict == "ALERT" and len(rs.pairs) == 1,
     "[F] 两篇逐字相同 → COMPUTED / flagged=2 / ALERT / 1 个相似对", f"{rs.verdict}/{rs.flagged_item_count}")
emit(rd.status == "COMPUTED" and rd.flagged_item_count == 0 and rd.verdict == "OK" and not rd.pairs,
     "[F] 两篇不同 → COMPUTED 且 flagged=0 / OK（算了得 0 != 没算）", f"{rd.status}/{rd.flagged_item_count}/{rd.verdict}")
emit(r1.flagged_item_count is None and rd.flagged_item_count == 0 and r1.status != rd.status,
     "[F] 「没算」与「算了得 0」在数据形态上可区分", f"None vs {rd.flagged_item_count}")
emit(len(q_types.DIMENSIONS) == 7 and len(q_types.TEXT_DIMENSIONS) == 4
     and all(q_dim.always_not_computed(d).status == "NOT_COMPUTED" and q_dim.always_not_computed(d).not_computed_reason
             for d in q_types.ALWAYS_NOT_COMPUTED) and len(q_types.ALWAYS_NOT_COMPUTED) == 3,
     "[F] 7 个维度 = 4 实算文本维度 + 3 个恒 NOT_COMPUTED（带原因码）", f"{sorted(q_types.ALWAYS_NOT_COMPUTED)}")

# =============================================================================
# O：OP-228 占位阈值如实标注
# =============================================================================
types_source = (QDIR / "types.py").read_text(encoding="utf-8")
service_source = (QDIR / "service.py").read_text(encoding="utf-8")
sim_source = (QDIR / "similarity.py").read_text(encoding="utf-8")
dim_source = (QDIR / "dimensions.py").read_text(encoding="utf-8")
emit(q_types.THRESHOLD_BASIS_PLACEHOLDER == "PLACEHOLDER_PENDING_OP-228" and "OP-228" in q_types.THRESHOLD_NOTE
     and "不得作为阻断条件" in q_types.THRESHOLD_NOTE,
     "[O] 阈值基线常量标 PLACEHOLDER_PENDING_OP-228，说明文案写明待 OP-228、不得作为阻断条件", q_types.THRESHOLD_BASIS_PLACEHOLDER)
emit(q_types.PLACEHOLDER_PAIR_THRESHOLD_PERMILLE == 950 and q_types.PLACEHOLDER_ALERT_THRESHOLD_PERMILLE == 500
     and all(t.pair_permille == 950 and t.alert_permille == 500 for t in q_types.THRESHOLDS.values())
     and set(q_types.THRESHOLDS) == set(q_types.TEXT_DIMENSIONS),
     "[O] 占位阈值仅 950 / 500（PLACEHOLDER，OP-228 未给口径），只覆盖 4 个文本维度",
     f"{ {k: (v.pair_permille, v.alert_permille) for k, v in q_types.THRESHOLDS.items()} }")
emit(not ({700, 750, 600, 450} & {q_types.PLACEHOLDER_PAIR_THRESHOLD_PERMILLE, q_types.PLACEHOLDER_ALERT_THRESHOLD_PERMILLE}),
     "[O] 占位数不与原型静态页的 70 / 75 / 60 / 45 重合（一眼看出不是业务数）", "不重合")
ok, detail = j_placeholder_source(types_source, service_source)
emit(ok, "[O] 源码层：types 标注 + service 响应带 thresholdBasis", detail)

# =============================================================================
# C：静态源码守卫
# =============================================================================
ok, detail = j_pure_modules(sim_source, dim_source, types_source)
emit(ok, "[C] 纯模块（similarity / dimensions / types）导入白名单 + 无 float + 无 .split", detail)
all_py = sorted(QDIR.glob("*.py"))
def code_strings(source):
    tree = ast.parse(source)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def has_order_by_version_no(source):
    return any(re.search(r"ORDER BY\s+\w*\.?version_no", t, re.I) for t in code_strings(source)) or bool(
        re.search(r"order_by\([^)]*version_no", source))


bad = [p.name for p in all_py if has_order_by_version_no(p.read_text(encoding="utf-8"))]
emit(not bad, "[C] 包内代码（不含 docstring）无 ORDER BY version_no / order_by(version_no)", f"{len(all_py)} 个源文件；违规 {bad}")
bad = []
for p in all_py:
    for m in imports_of(p.read_text(encoding="utf-8")):
        if m.startswith("rednote.modules.") and not m.startswith("rednote.modules.quality"):
            bad.append((p.name, m))
direct = sorted({p for p, _ in bad} - {"content_bridge.py", "files_bridge.py", "media_bridge.py", "versions_bridge.py",
                                      "service.py", "repository.py", "api.py", "dependencies.py", "schemas.py", "models.py"})
emit(not direct, "[C] 跨包 import 只出现在 bridge / service / repository / api / models 等接缝文件", f"越界 {direct}")

# =============================================================================
# R：路由
# =============================================================================
ops = sorted((m, r.path) for r in q_api.router.routes for m in sorted(r.methods or ()))
emit(len(ops) == EXPECTED_ROUTE_COUNT, f"[R] quality 路由共 {EXPECTED_ROUTE_COUNT} 个 operation", f"{len(ops)}：{ops}")
emit(("POST", "/projects/{projectId}/quality-reports") in ops
     and ("POST", "/quality-report-versions/{versionId}/artifacts") in ops
     and ("GET", "/quality-dimensions") in ops,
     "[R] 含生成接口 POST、导出产物 POST、维度清单 GET", "ok")
contract = json.loads((REPO / "contracts" / "openapi.json").read_text(encoding="utf-8"))
cpaths = {(m.upper(), p[len("/api/v1"):]) for p, item in contract["paths"].items() if p.startswith("/api/v1/quality") or "/quality-reports" in p
          for m in item if m in ("get", "post", "put", "patch", "delete")}
emit(cpaths == set(ops), f"[R] contracts/openapi.json 里 quality 相关 operation 与路由逐条一致（只读对账，不改契约）", f"契约 {len(cpaths)} 路由 {len(ops)}")

# =============================================================================
# P：行为探针
# =============================================================================
raised, st, con, msg = probe(b_valid)
emit(not raised, "[P] 正向对照：合法的报告 + 版本 + 7 维度 + 成员 + 投影，强制检查后不报错", f"raised={raised} {msg[:80]}")
raised, st, con, msg = probe(b_valid_computed)
emit(not raised, "[P] 正向对照：含 COMPUTED 维度（算了得 0）的合法版本不报错", f"raised={raised} {msg[:80]}")
raised, st, con, msg = probe(b_foreign_report_node)
emit(not raised, "[P] 正向对照：不归本包管的 REPORT 节点（quality_reports 无该 id）不被误拒", f"raised={raised} {msg[:80]}")
for label, build, state, contains, constraint in PROBES:
    ok, detail = expect(build, state, contains, constraint)
    emit(ok, label, detail)

# =============================================================================
# D：一次性临时库 upgrade -> downgrade -> upgrade 往返
# =============================================================================
base_url = make_url(DB)
TMP_NAME = f"{base_url.database}_tmp_{os.getpid()}"
tmp_url = base_url.set(database=TMP_NAME)
admin = create_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")


def alembic(*args):
    env = os.environ.copy()
    env["REDNOTE_DATABASE_URL"] = tmp_url.render_as_string(hide_password=False)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "migrations/alembic.ini", *args],
        cwd=str(REPO), env=env, capture_output=True, text=True, check=False)
    return done.returncode, (done.stdout + done.stderr)[-300:]


def catalog(tmp_engine):
    with tmp_engine.connect() as c:
        trig = {r[0] for r in c.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))}
        funcs = {r[0] for r in c.execute(text(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public'"))}
        tables = {r[0] for r in c.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))}
        ver = c.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
        c.rollback()
    return trig, funcs, tables, ver


tmp_engine = None
try:
    assert TMP_NAME.startswith("rednote_verify_wp33_tmp_")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{TMP_NAME}"'))
        c.execute(text(f'CREATE DATABASE "{TMP_NAME}"'))
    tmp_engine = create_engine(tmp_url)
    with tmp_engine.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    code, tail = alembic("upgrade", "head")
    trig_a, funcs_a, tables_a, ver_a = catalog(tmp_engine)
    own_trig = set(q_ddl.trigger_names())
    emit(code == 0 and revision_is_at_least(ver_a, WP33_MIGRATION_REVISION) and set(OWN_TABLES) <= tables_a,
         "[D] 临时库：空库 upgrade head 成功，7 表建出", f"exit={code} version={ver_a}")
    emit(own_trig <= trig_a and set(q_ddl.OWN_FUNCTIONS) <= funcs_a,
         "[D] 临时库：13 个触发器与 7 个自有函数随迁移建出", f"触发器 {len(own_trig & trig_a)} 函数 {len(set(q_ddl.OWN_FUNCTIONS) & funcs_a)}")
    code, tail = alembic("downgrade", WP33_DOWN_REVISION)
    trig_b, funcs_b, tables_b, ver_b = catalog(tmp_engine)
    emit(code == 0 and ver_b == WP33_DOWN_REVISION and not (set(OWN_TABLES) & tables_b),
         "[D] 临时库：downgrade 回 WP34 迁移成功，7 表被删", f"exit={code} version={ver_b}")
    emit(funcs_a - funcs_b == set(q_ddl.OWN_FUNCTIONS) and "rednote_reject_mutation" in funcs_b,
         "[D] downgrade 只删本包 7 个自有函数，共用函数 rednote_reject_mutation 仍在", f"差 {sorted(funcs_a - funcs_b)}")
    emit(trig_a - trig_b == own_trig and trig_b <= trig_a,
         "[D] downgrade 只删本包 13 个触发器，其它包触发器一个未少", f"少了 {sorted(trig_a - trig_b - own_trig)}")
    code, tail = alembic("upgrade", "head")
    trig_c, funcs_c, tables_c, ver_c = catalog(tmp_engine)
    emit(code == 0 and ver_c == ver_a and trig_c == trig_a and funcs_c == funcs_a and tables_c == tables_a,
         "[D] 再 upgrade head：表 / 触发器 / 函数集合与第一次完全一致（往返幂等）", f"exit={code} version={ver_c}")
finally:
    if tmp_engine is not None:
        tmp_engine.dispose()
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{TMP_NAME}" WITH (FORCE)'))
    except Exception as exc:
        print(f"# 临时库清理失败 {TMP_NAME}: {exc}")

# =============================================================================
# V：反向验证（错误实现 / 错误数据下判据必须报红）
# =============================================================================
def rev(label, judge):
    # judge() 返回 True 表示「判据报红了」（即反向验证成功）
    try:
        red = bool(judge())
    except Exception as exc:
        red = False
        print(f"# 反向验证异常 {label}: {exc}")
    emit(red, label, "判据确实报红" if red else "判据没有报红（判据可能是恒绿）")


def mut_drop_version_trigger(conn):
    conn.exec_driver_sql(f"DROP TRIGGER {q_ddl.VERSION_TRIGGER} ON quality_report_versions")


def mut_noop_pair_function(conn):
    conn.exec_driver_sql(
        f"CREATE OR REPLACE FUNCTION {q_ddl.PAIR_FUNCTION}() RETURNS trigger AS $$ BEGIN RETURN NULL; END; $$ LANGUAGE plpgsql")


def mut_drop_immutable_items(conn):
    conn.exec_driver_sql("DROP TRIGGER trg_quality_report_items_immutable ON quality_report_items")


def mut_drop_nc_check(conn):
    conn.exec_driver_sql(
        "ALTER TABLE quality_dimension_results DROP CONSTRAINT ck_quality_dimension_results_not_computed_columns_absent")


def mut_drop_orphan_trigger(conn):
    conn.exec_driver_sql(f"DROP TRIGGER {q_ddl.NODE_TRIGGER} ON version_nodes")


def mut_drop_item_trigger(conn):
    conn.exec_driver_sql(f"DROP TRIGGER {q_ddl.ITEM_TRIGGER} ON quality_report_items")


def mut_drop_uq(conn):
    conn.exec_driver_sql("ALTER TABLE quality_reports DROP CONSTRAINT uq_quality_reports_project_id_scope_digest")


def not_raising(build, mutate, state, contains=None, constraint=None):
    ok, _ = expect(build, state, contains, constraint, mutate)
    return not ok


rev("[V1] 反向：摘掉版本一致性触发器 → 「缺维度行」探针报红",
    lambda: not_raising(b_six_dims, mut_drop_version_trigger, CONSISTENCY_SQLSTATE, "必须恰为 7"))
rev("[V2] 反向：相似对触发器函数换成空实现 → 「相似对挂在 NOT_COMPUTED 维度」探针报红",
    lambda: not_raising(b_pair_on_not_computed, mut_noop_pair_function, CONSISTENCY_SQLSTATE, "NOT_COMPUTED"))
rev("[V3] 反向：摘掉 quality_report_items 不可变触发器 → 「不可变 DELETE」探针报红",
    lambda: not_raising(b_delete_item, mut_drop_immutable_items, IMMUTABLE_SQLSTATE))
rev("[V4] 反向：删掉 not_computed_columns_absent CHECK → 「没算不得带数值」探针报红",
    lambda: not_raising(b_dim_nc_with_count, mut_drop_nc_check, CONSISTENCY_SQLSTATE, None,
                        "ck_quality_dimension_results_not_computed_columns_absent"))
rev("[V5] 反向：摘掉 REPORT 孤立节点触发器 → 「孤立 REPORT 节点」探针报红",
    lambda: not_raising(b_orphan_node, mut_drop_orphan_trigger, CONSISTENCY_SQLSTATE, "孤立节点"))
rev("[V6] 反向：摘掉成员一致性触发器 → 「成员内容属于别的项目」探针报红",
    lambda: not_raising(b_cross_project_item, mut_drop_item_trigger, CONSISTENCY_SQLSTATE, "属于项目"))
rev("[V7] 反向：删掉 (project_id, scope_digest) 唯一约束 → UQ 探针报红",
    lambda: not_raising(b_dup_scope_digest, mut_drop_uq, UNIQUE_SQLSTATE, None, "uq_quality_reports_project_id_scope_digest"))


def bad_is_similar(inter, union, threshold):
    return inter * 1000 > threshold * union


rev("[V8] 反向：把 >= 写成 > 的错误 is_similar → 边界判据报红",
    lambda: not (bad_is_similar(19, 20, 950) and not bad_is_similar(18, 20, 950)))


def bad_missing_as_zero(a, b):
    if not a or not b:
        return (0, 1)
    return (len(a & b), len(a | b))


rev("[V9] 反向：缺失填 0 的错误 pair_counts → 「缺失不填 0」判据报红",
    lambda: bad_missing_as_zero(frozenset(), frozenset({"ab"})) is not None)
rev("[V10] 反向：types 源码去掉 PLACEHOLDER 标注 → OP-228 占位标注判据报红",
    lambda: not j_placeholder_source(types_source.replace("PLACEHOLDER_PENDING_OP-228", "PENDING"), service_source)[0])
rev("[V11] 反向：service 响应去掉 thresholdBasis → OP-228 占位标注判据报红",
    lambda: not j_placeholder_source(types_source, service_source.replace('"thresholdBasis": THRESHOLD_BASIS_PLACEHOLDER', '"x": 1'))[0])
rev("[V12] 反向：downgrade 里加一句 DROP FUNCTION rednote_reject_mutation → downgrade 判据报红",
    lambda: not j_downgrade(mig_source.replace(
        "    for _fn in quality_ddl.consistency_function_names():",
        "    op.execute('DROP FUNCTION IF EXISTS rednote_reject_mutation()')\n"
        "    for _fn in quality_ddl.consistency_function_names():"))[0])
rev("[V13] 反向：迁移里 import quality.types → 迁移 import 判据报红",
    lambda: not j_migration_imports(mig_source + "\nfrom rednote.modules.quality import types as _t\n")[0])
rev("[V16] 反向：代码里出现 ORDER BY version_no 字符串 → 该守卫报红（docstring 里的说明文字不误报）",
    lambda: has_order_by_version_no('X = "SELECT 1 ORDER BY version_no DESC"') and not has_order_by_version_no('"' * 3 + "说明 ORDER BY version_no" + '"' * 3 + "\nX = 1"))
rev("[V14] 反向：similarity 源码混进 float 字面量 → 纯模块判据报红",
    lambda: not j_pure_modules(sim_source + "\nX = 0.5\n", dim_source, types_source)[0])


def mutated_names_red():
    saved = model_names

    def fake(table):
        out = saved(table)
        out["c"] = set(sorted(out["c"])[1:])
        return out

    globals()["model_names"] = fake
    try:
        with engine.connect() as c:
            res = j_names(c, "c", "c")[0]
            c.rollback()
        return not res
    finally:
        globals()["model_names"] = saved


rev("[V15] 反向：模型侧少一个 CHECK 名 → CHECK 逐名对账判据报红", mutated_names_red)

# =============================================================================
# Z：零残留 + 变异已回滚
# =============================================================================
with engine.connect() as conn:
    n = conn.execute(text("SELECT count(*) FROM projects WHERE name = :n"), {"n": PROBE_PROJECT_NAME}).scalar_one()
    emit(n == 0, "[Z] 零残留 projects（探针）", f"{n} 行")
    n = conn.execute(text("SELECT count(*) FROM users WHERE issuer = :i"), {"i": PROBE_ISSUER}).scalar_one()
    emit(n == 0, "[Z] 零残留 users（探针）", f"{n} 行")
    total = sum(conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in OWN_TABLES)
    emit(total == 0, "[Z] 本包 7 张表整体 0 行（探针全部回滚，没有任何提交）", f"{total} 行")
    n = conn.execute(text("SELECT count(*) FROM pg_database WHERE datname LIKE :p"),
                     {"p": "rednote_verify_wp33_tmp_%"}).scalar_one()
    emit(n == 0, "[Z] 一次性临时库已 DROP", f"{n} 个残留")
    ok_all = all(fn(conn)[0] for fn in (j_columns, j_fk_restrict, j_triggers, j_immutable_triggers,
                                         j_consistency_triggers, j_functions, j_indexes))
    emit(ok_all, "[Z] 反向验证后目标库结构原样（列 / FK / 触发器 / 函数 / 索引）", "全部复原")
    ok_all = all(j_names(conn, k, k)[0] for k in ("c", "f", "u", "p"))
    emit(ok_all, "[Z] 反向验证后目标库约束名原样（CHECK / FK / UNIQUE / PK）", "全部复原")
    conn.rollback()

print(f"RESULT_EMITTED (期望 {EXPECTED_CHECKS} 条)")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "NO_PROXY": "*",
            "no_proxy": "*",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(BACKEND_DIR / "src"),
            "WP33_DB_URL": DB_URL,
            "WP33_REPO_ROOT": str(REPO_ROOT),
        }
    )
    return env


def _psql(db: str, sql: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PSQL, "-h", "localhost", "-U", "rednote", "-d", db, "-v", "ON_ERROR_STOP=1", "-Atc", sql],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def rebuild_target_db(env: dict[str, str]) -> list[str]:
    """DROP + CREATE 目标库（库名写死，只动 rednote_verify_wp33）→ vector 扩展 → alembic upgrade head。"""
    assert DB_NAME == "rednote_verify_wp33"
    notes: list[str] = []
    steps = (
        ("postgres", f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)'),
        ("postgres", f'CREATE DATABASE "{DB_NAME}"'),
        (DB_NAME, "CREATE EXTENSION IF NOT EXISTS vector"),
    )
    for db, sql in steps:
        done = _psql(db, sql, env)
        notes.append(f"psql[{db}] {sql} -> rc={done.returncode} {(done.stdout + done.stderr).strip()[:120]}")
        if done.returncode != 0:
            return notes
    alembic_env = dict(env)
    alembic_env["REDNOTE_DATABASE_URL"] = DB_URL
    done = subprocess.run(
        [PYTHON, "-m", "alembic", "-c", "migrations/alembic.ini", "upgrade", "head"],
        cwd=str(REPO_ROOT),
        env=alembic_env,
        capture_output=True,
        text=True,
        check=False,
    )
    notes.append(f"alembic upgrade head -> rc={done.returncode} {(done.stdout + done.stderr).strip()[-160:]}")
    return notes


_PYTEST_SUMMARY_RE = re.compile(r"^(?:=+ )?(\d+ .*?) in [\d.]+s")


def parse_pytest(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in output.splitlines():
        match = _PYTEST_SUMMARY_RE.search(line)
        if match and re.search(r"\d+ (passed|failed|error|skipped)", match.group(1)):
            for number, word in re.findall(r"(\d+) (passed|failed|errors?|skipped|warnings?|deselected)", match.group(1)):
                counts[word.rstrip("s") if word in ("errors", "warnings") else word] = int(number)
    return counts


def run_pytest(env: dict[str, str]) -> tuple[list[str], str]:
    test_env = dict(env)
    # 每条 pytest 内联指向目标库；不在测试里加任何「库名须含」自检
    test_env["REDNOTE_TEST_DATABASE_URL"] = DB_URL
    completed = subprocess.run(
        [PYTHON, "-m", "pytest", "-p", "no:cacheprovider", "--no-header", *TEST_FILES],
        cwd=str(BACKEND_DIR),
        env=test_env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    counts = parse_pytest(output)
    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0) + counts.get("error", 0)
    skipped = counts.get("skipped", 0)
    lines = [
        f"CHECK {'OK  ' if completed.returncode == 0 and failed == 0 else 'FAIL'} "
        f"[T] pytest {' '.join(TEST_FILES)} 在目标库上退出码 0 且无失败 / 错误: "
        f"rc={completed.returncode} {counts}",
        f"CHECK {'OK  ' if passed == PYTEST_EXPECTED_PASSED else 'FAIL'} "
        f"[T] pytest 通过条数恰为 {PYTEST_EXPECTED_PASSED}（实测回填，防只跑了一半）: passed={passed}",
        f"CHECK {'OK  ' if skipped == PYTEST_EXPECTED_SKIPPED else 'FAIL'} "
        f"[T] pytest 跳过条数恰为 {PYTEST_EXPECTED_SKIPPED}（跳过不能悄悄变多）: skipped={skipped}",
    ]
    tail = "\n".join(output.rstrip().splitlines()[-12:])
    return lines, tail


def collect_wp33() -> tuple[Path, bool]:
    env = _env()
    setup_notes = rebuild_target_db(env)
    setup_ok = all(" rc=0" in note for note in setup_notes) and len(setup_notes) == 4

    if setup_ok:
        inline = INSPECT_SCRIPT.replace("__EXPECTED__", str(INLINE_EXPECTED))
        completed = subprocess.run(
            [PYTHON, "-c", inline],
            cwd=str(BACKEND_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        returncode = completed.returncode
        pytest_lines, pytest_tail = run_pytest(env)
    else:
        output = "建库 / 迁移失败，未进入核对：\n" + "\n".join(setup_notes)
        returncode = 1
        pytest_lines, pytest_tail = [], ""

    checks = [line for line in output.splitlines() if line.startswith(("CHECK", "SKIP"))]
    checks.extend(pytest_lines)
    failures = [line for line in checks if line.startswith("CHECK FAIL")]

    lines = [
        "# WP33 重复检查 / 质量报告：7 表逐列 / 约束与索引逐名 / 13 触发器 / 7 函数 / 纯函数 /"
        " OP-228 占位阈值标注 / 行为探针 / 路由 / 静态守卫 / 临时库 upgrade-downgrade 往返 / 反向验证 /"
        " pytest / 探针零残留",
        f"# 生成时间: {_now()}",
        f"# 目标库: {DB_URL}（本脚本每次 DROP + CREATE + alembic upgrade head 重建）",
        "# 阈值说明: 950 / 500 千分比是 PLACEHOLDER（待 OP-228，用户尚未给数值），不是产品口径；"
        "ALERT 不是阻断条件",
        "# 命令: cd services/backend && python -c '<pg_catalog + 试写 + 试插 + 路由 + 临时库 alembic>'"
        " ; pytest -p no:cacheprovider tests/test_quality_wp33.py tests/test_quality_wp33_api.py",
        "",
        "## 建库与迁移",
        *setup_notes,
        "",
        output.rstrip(),
        *pytest_lines,
        "",
        "## pytest 输出尾部",
        pytest_tail,
        "",
        f"EXIT={returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp33-quality-reports.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    complete = len(checks) >= EXPECTED_CHECKS
    exact = len(checks) == EXPECTED_CHECKS
    return target, (setup_ok and returncode == 0 and not failures and complete and exact)


def main() -> int:
    path, ok = collect_wp33()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
