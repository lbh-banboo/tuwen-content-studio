#!/usr/bin/env python
"""WP36 证据采集脚本。

产出的文件（落在 `evidence/m4/`）：

| 文件 | 内容 |
|---|---|
| `wp36-koc-delivery-publication.log` | 4 张表结构 / 列集 / CHECK / FK / 索引（含 3 条部分唯一）/ 3 个触发器 / 库内活性验证（列级冻结逐列变异 + 一致性 5 条 + 幂等载体 3 条）/ 零残留 |

## 本包的结构性特点

- 4 张表全部在本包：`koc_records`（可变）、`deliveries`（**列级**可变）、
  `publications`（可变）、`publication_links`（可变，只追加；本包只建表，
  判定归 WP37）。
- **没有整行不可变表** ⇒ 本包不挂 `trg_*_immutable`、不碰
  `rednote_reject_mutation()`（但探针要核对它**仍在**，ISS-79）。
  这是与 WP29/WP35 的结构性差异，不是漏做。
- `deliveries` 的不可变性是**列级**的：`trg_deliveries_frozen_columns`
  （`BEFORE UPDATE ... FOR EACH ROW`，逐列 `IS DISTINCT FROM`）。
  **冻结列名单由 `models.frozen_delivery_columns()` 从磁盘派生**
  （= 全部列 − 7 列可变白名单），探针按同一来源逐列变异，
  **不在脚本里人写冻结列清单**。
- 两条业务一致性判据由两个**延迟约束触发器**守：
  `rednote_check_delivery_consistency`（内容归属 / 冻结字段与导出行逐列一致 /
  KOC 归属 / 替换链同项目同内容）与 `rednote_check_publication_consistency`
  （`delivery_id` 归属）。

## 探针自身踩过的坑

1. **★ 期望条数必须实测，不能凭印象**（与 WP24~WP29 同型教训）：
   `complete = len(checks) >= EXPECTED_CHECKS` 在期望值偏低时恒真，
   本脚本另加 `exact = len(checks) == EXPECTED_CHECKS` 并入 verdict。
2. **两个一致性触发器是 `CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED`**：
   INSERT 那一刻不检查，必须在事务里 `SET CONSTRAINTS ALL IMMEDIATE`
   才能看到它拒绝，否则"坏值写进去了"会被误读成"防线失效"。
   而**列级冻结触发器是普通 BEFORE 触发器**，当场就拒 —— 两类探针的写法不同。
3. **`deliveries.export_item_id` 的上游是 WP35 的 `export_items`**，造它要连带造
   `blobs` + `file_objects`（产物文件）+ `content_items` + `content_versions`
   + `version_nodes` 配对行（`content_versions.id` 同时是 `version_nodes.id`
   的延迟 FK，与 WP26/WP29 证据脚本的 `_version_pair_tx` 同一手法）。
   且 `export_items` 自己有**组一致性延迟触发器**（组内属性 DISTINCT = 1、
   行数 = `item_count`）⇒ 探针按"1 行 1 组、`SINGLE`、`item_count=1`"造。
4. **本包表全部可变 ⇒ 探针可以整段 `rollback()`**（不像 WP29 的不可变表一旦
   提交就删不掉）。零残留因此是"回滚后再数"，而不是"逐行 DELETE"。
5. **本包尚无迁移**（建迁移是主 agent 的独占动作，接缝 §4.1）⇒ 脚本**自己**
   按 `modules/delivery/{models,ddl}.py` 这一份来源 `create_all` + 建触发器
   （与测试夹具同源、幂等）。收口落了迁移之后，主 agent 应把目标库换成由迁移
   建出的 `rednote_verify_wp36`，并把 `ENSURE_TABLES` 关掉 —— 脚本会打印一条
   `SKIP` 如实说明当前是哪种形态，**不装作是迁移建的**。
6. **内联脚本只允许单行注释且不能出现三引号**（ISS-45），由
   `tools/m1/check_evidence_scripts.py` 机械核对。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "services" / "backend"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m4"

#: 期望条数（与内联脚本里的 EXPECTED_CHECKS 保持同值，实测回填）。
EXPECTED_CHECKS: int = 53

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

INSPECT_SCRIPT = r"""
# 内联脚本：只允许单行注释，且不能出现三引号（三引号会提前终止外层包裹，见 ISS-45）。

import hashlib
import os
import uuid

from sqlalchemy import DateTime, String, Text, create_engine, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import DBAPIError

# FK 目标表必须在 create_all 之前 import（否则 NoReferencedTableError）。
import rednote.modules.content.models
import rednote.modules.exports.models
import rednote.modules.files.models
import rednote.modules.gateway.models
import rednote.modules.identity.models
import rednote.modules.jobs.models
import rednote.modules.media.models
import rednote.modules.projects.models
import rednote.modules.prompts.models
import rednote.modules.versions.models

from rednote.modules.delivery import ddl as delivery_ddl
from rednote.modules.delivery import models as delivery_models

EXPECTED_CHECKS = 53

# 目标库。默认指向本包的私有库；收口时由主 agent 换成迁移建出的验证库。
DEFAULT_DSN = "postgresql+psycopg://rednote@localhost:5432/rednote_test_wp36"
DSN = os.environ.get("REDNOTE_WP36_EVIDENCE_DSN", DEFAULT_DSN).strip()
ENSURE_TABLES = os.environ.get("REDNOTE_WP36_EVIDENCE_ENSURE", "1").strip() != "0"

engine = create_engine(DSN)

OWN_TABLES = delivery_models.OWN_TABLES
MUTABLE_TABLES = ("koc_records", "deliveries", "publications")
TRIGGER_NAMES = delivery_ddl.trigger_names()
CONSISTENCY_FUNCTIONS = delivery_ddl.consistency_function_names()
CONSISTENCY_SQLSTATE = delivery_ddl.CONSISTENCY_SQLSTATE
UNIQUE_SQLSTATE = "23505"

PARTIAL_UNIQUE_INDEXES = (
    "uq_deliveries_idempotency",
    "uq_deliveries_replaces",
    "uq_publications_idempotency",
)

PROBE_ISSUER = "wp36-probe"
PROBE_PROJECT_PREFIX = "WP36PROBE"


def emit(ok, label, detail):
    print("CHECK " + ("OK  " if ok else "FAIL") + " " + label + ": " + str(detail))


def sqlstate(exc):
    return getattr(exc.orig, "sqlstate", None)


def mutation_expression(column):
    # 按列类型生成一个"一定与原值不同"的 SQL 表达式。由磁盘派生，不写名单。
    if isinstance(column.type, PGUUID):
        return "gen_random_uuid()"
    if isinstance(column.type, DateTime):
        return "now() + interval '1 day'"
    if isinstance(column.type, (String, Text)):
        return "'wp36-mutated'"
    raise AssertionError("没有为列 " + column.name + " 准备变异表达式")


def digest(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def people_tx(conn):
    uid = uuid.uuid4()
    brand = uuid.uuid4()
    prod = uuid.uuid4()
    proj = uuid.uuid4()
    other = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, display_name, role, status)"
            " VALUES (:u, :iss, :sub, 'WP36 探针', 'ADMIN', 'ACTIVE')"
        ),
        {"u": str(uid), "iss": PROBE_ISSUER, "sub": "probe-" + str(uid)},
    )
    conn.execute(
        text("INSERT INTO brands (id, name, status) VALUES (:b, :n, 'ACTIVE')"),
        {"b": str(brand), "n": "WP36 品牌 " + str(brand)[:8]},
    )
    conn.execute(
        text(
            "INSERT INTO products (id, brand_id, name, model_code, status)"
            " VALUES (:p, :b, :n, :c, 'ACTIVE')"
        ),
        {"p": str(prod), "b": str(brand), "n": "WP36 产品 " + str(prod)[:8],
         "c": "WP36-" + str(prod)[:8]},
    )
    for pid in (proj, other):
        conn.execute(
            text(
                "INSERT INTO projects (id, project_code, name, brand_id, product_id,"
                " owner_id, target_delivery_count, due_date)"
                " VALUES (:p, :c, :n, :b, :pr, :o, 1,"
                " (now() + interval '30 days')::date)"
            ),
            {"p": str(pid), "c": PROBE_PROJECT_PREFIX + str(pid)[:8],
             "n": "WP36 探针项目", "b": str(brand), "pr": str(prod), "o": str(uid)},
        )
    return {"user": str(uid), "project": str(proj), "other": str(other)}


def file_object_tx(conn, ids, project_id):
    blob = uuid.uuid4()
    fid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO blobs (id, storage_backend, object_key, sha256, byte_size,"
            " mime_type) VALUES (:b, 'LOCAL', :k, :s, 10, 'application/zip')"
        ),
        {"b": str(blob), "k": "wp36/" + str(blob) + ".zip", "s": digest(str(blob))},
    )
    conn.execute(
        text(
            "INSERT INTO file_objects (id, blob_id, project_id, original_name,"
            " visibility, classification, uploaded_by)"
            " VALUES (:f, :b, :p, 'wp36.zip', 'PROJECT', 'SUPPORTED', :u)"
        ),
        {"f": str(fid), "b": str(blob), "p": str(project_id), "u": ids["user"]},
    )
    return fid


def content_tx(conn, ids, project_id):
    content_id = uuid.uuid4()
    node_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO content_items (id, project_id, state, current_version_no,"
            " lock_version) VALUES (:c, :p, 'DRAFT', 0, 0)"
        ),
        {"c": str(content_id), "p": str(project_id)},
    )
    conn.execute(
        text(
            "INSERT INTO version_nodes (id, entity_type, entity_id, project_id,"
            " revision_no, payload_hash, schema_version)"
            " VALUES (:n, 'CONTENT', :e, :p, 1, :h, 1)"
        ),
        {"n": str(node_id), "e": str(content_id), "p": str(project_id),
         "h": digest("vn-" + str(node_id))},
    )
    conn.execute(
        text(
            "INSERT INTO content_versions (id, content_item_id, version_no,"
            " title, body, tags, bindings, payload, payload_hash, schema_version)"
            " VALUES (:id, :c, 1, 'T', 'B', cast('[]' as jsonb),"
            " cast('[]' as jsonb), cast('{}' as jsonb), :ph, 1)"
        ),
        {"id": str(node_id), "c": str(content_id), "ph": digest("cv-" + str(node_id))},
    )
    conn.execute(
        text("UPDATE content_items SET current_version_id = :v,"
             " current_version_no = 1 WHERE id = :c"),
        {"v": str(node_id), "c": str(content_id)},
    )
    return {"content": str(content_id), "version": str(node_id)}


def export_item_tx(conn, ids, project_id):
    item = uuid.uuid4()
    group = uuid.uuid4()
    content = content_tx(conn, ids, project_id)
    artifact = file_object_tx(conn, ids, project_id)
    eligibility = digest("eh-" + str(item))
    conn.execute(
        text(
            "INSERT INTO export_items (id, project_id, export_group_id,"
            " content_item_id, version_node_id, eligibility_hash, export_kind,"
            " export_format, item_count, artifact_file_id, zip_sha256,"
            " zip_byte_size, request_hash, created_by)"
            " VALUES (:i, :p, :g, :c, :v, :eh, 'SINGLE', 'STATIC', 1, :a, :zs,"
            " 10, :rh, :by)"
        ),
        {"i": str(item), "p": str(project_id), "g": str(group),
         "c": content["content"], "v": content["version"], "eh": eligibility,
         "a": str(artifact), "zs": digest("zip-" + str(item)),
         "rh": digest("req-" + str(item)), "by": ids["user"]},
    )
    return {"item": str(item), "content": content["content"],
            "version": content["version"], "eligibility": eligibility}


def koc_tx(conn, project_id):
    koc = uuid.uuid4()
    conn.execute(
        text("INSERT INTO koc_records (id, project_id, display_name)"
             " VALUES (:k, :p, 'WP36 素人')"),
        {"k": str(koc), "p": str(project_id)},
    )
    return str(koc)


def delivery_tx(conn, ids, export, *, koc=None, replaces=None, project_id=None,
                state="PREPARED", over=None):
    row = uuid.uuid4()
    params = {
        "i": str(row), "p": project_id or ids["project"], "koc": koc,
        "e": export["item"], "c": export["content"], "v": export["version"],
        "eh": export["eligibility"], "r": replaces, "state": state,
        "rh": digest("req-" + str(row)), "by": ids["user"],
        "key": None,
    }
    if over:
        params.update(over)
    conn.execute(
        text(
            "INSERT INTO deliveries (id, project_id, koc_record_id, export_item_id,"
            " content_item_id, version_node_id, state, eligibility_hash,"
            " replaces_delivery_id, idempotency_key, request_hash, created_by)"
            " VALUES (:i, :p, :koc, :e, :c, :v, :state, :eh, :r, :key, :rh, :by)"
        ),
        params,
    )
    return str(row)


def publication_tx(conn, ids, *, delivery=None, project_id=None, key=None,
                   idem=None):
    row = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO publications (id, project_id, delivery_id, publication_key,"
            " state, content_state, idempotency_key, request_hash, created_by)"
            " VALUES (:i, :p, :d, :k, 'DRAFT', 'UNVERIFIED', :idem, :rh, :by)"
        ),
        {"i": str(row), "p": project_id or ids["project"], "d": delivery,
         "k": key or ("PK-" + str(row)[:8]), "idem": idem,
         "rh": digest("req-" + str(row)), "by": ids["user"]},
    )
    return str(row)


checks = []

# --- 0：目标库形态（如实说明表是迁移建的还是本脚本建的） ------------------------
from rednote.platform.base import Base  # noqa: E402  （下方 Base.metadata 引用无论 ENSURE_TABLES 取值都要用到）

if ENSURE_TABLES:
    Base.metadata.create_all(
        engine, tables=[Base.metadata.tables[n] for n in OWN_TABLES]
    )
    with engine.begin() as conn:
        for item in delivery_ddl.all_trigger_ddl():
            conn.execute(text(item[3]))
            conn.execute(text(item[1]))
            conn.execute(text(item[2]))
    print(
        "SKIP 目标库形态: 本包尚无迁移（建迁移是主 agent 独占动作）⇒"
        " 4 表与 3 触发器由本脚本按 modules/delivery/{models,ddl}.py 这一份来源"
        " create_all 建出（与测试夹具同源）。收口落了迁移后请把目标库换成"
        " rednote_verify_wp36 并置 REDNOTE_WP36_EVIDENCE_ENSURE=0。"
    )
else:
    print("SKIP 目标库形态: ENSURE_TABLES=0，表结构由迁移建出，本脚本只读核对。")


# --- S：结构 -----------------------------------------------------------------
with engine.connect() as conn:
    n = conn.execute(
        text(
            "SELECT count(*) FROM information_schema.tables"
            " WHERE table_schema='public' AND table_name = ANY(:names)"
        ),
        {"names": list(OWN_TABLES)},
    ).scalar_one()
    emit(n == 4, "[S] 本包 4 张表存在", str(n) + "/4")

    for table in OWN_TABLES:
        expected = set(Base.metadata.tables[table].columns.keys())
        actual = set(
            r[0] for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema='public' AND table_name=:t"
                ),
                {"t": table},
            )
        )
        emit(expected == actual, "[S] " + table + " 列集与模型一致",
             "模型 " + str(len(expected)) + " / 库 " + str(len(actual))
             + " 差集 " + str(sorted(expected ^ actual)))

    for table in OWN_TABLES:
        expected = set(
            c.name for c in Base.metadata.tables[table].constraints
            if c.__class__.__name__ == "CheckConstraint"
        )
        actual = set(
            r[0] for r in conn.execute(
                text(
                    "SELECT conname FROM pg_constraint"
                    " WHERE contype='c' AND conrelid = CAST(:t AS regclass)"
                ),
                {"t": table},
            )
        )
        emit(expected == actual, "[S] " + table + " CHECK 名集合与模型一致",
             "模型 " + str(len(expected)) + " / 库 " + str(len(actual)))

    for table in OWN_TABLES:
        expected = set(
            c.name for c in Base.metadata.tables[table].constraints
            if c.__class__.__name__ == "ForeignKeyConstraint"
        )
        actual = set(
            r[0] for r in conn.execute(
                text(
                    "SELECT conname FROM pg_constraint"
                    " WHERE contype='f' AND conrelid = CAST(:t AS regclass)"
                ),
                {"t": table},
            )
        )
        emit(expected == actual, "[S] " + table + " FK 名集合与模型一致",
             "模型 " + str(len(expected)) + " / 库 " + str(len(actual)))

    actual_triggers = set(
        r[0] for r in conn.execute(
            text(
                "SELECT tgname FROM pg_trigger WHERE tgrelid = ANY("
                "SELECT oid FROM pg_class WHERE relname = ANY(:names)"
                ") AND NOT tgisinternal"
            ),
            {"names": list(OWN_TABLES)},
        )
    )
    emit(actual_triggers == set(TRIGGER_NAMES), "[S] 3 个触发器名集合一致",
         "库 " + str(sorted(actual_triggers)))

    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname = ANY(:f)"),
        {"f": list(CONSISTENCY_FUNCTIONS)},
    ).scalar_one()
    emit(n == 3, "[S] 本包 3 个触发器函数存在", str(sorted(CONSISTENCY_FUNCTIONS)))

    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname='rednote_reject_mutation'")
    ).scalar_one()
    emit(n == 1 and "rednote_reject_mutation" not in CONSISTENCY_FUNCTIONS,
         "[S] 跨包共用不可变函数仍存在且不在本包删除清单里（ISS-79）",
         "rednote_reject_mutation count=" + str(n))

    rows = dict(
        (r[0], r[1]) for r in conn.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes"
                " WHERE schemaname='public' AND indexname = ANY(:names)"
            ),
            {"names": list(PARTIAL_UNIQUE_INDEXES)},
        )
    )
    missing = [n for n in PARTIAL_UNIQUE_INDEXES if n not in rows]
    not_partial = [
        n for n, d in rows.items()
        if "UNIQUE" not in d.upper() or "IS NOT NULL" not in d.upper()
    ]
    emit(not missing and not not_partial,
         "[S] 3 条部分唯一索引存在且带 WHERE ... IS NOT NULL",
         "缺 " + str(missing) + " 非部分唯一 " + str(not_partial))

    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conname='uq_publications_project_key'"
            "   AND conrelid = CAST('publications' AS regclass)"
        )
    ).scalar_one()
    emit(n == 1,
         "[S] 业务键显式全名落库（不是被命名约定改成的 _project_id_publication_key）",
         "uq_publications_project_key count=" + str(n))

    bad = list(
        r[0] for r in conn.execute(
            text(
                "SELECT indexname FROM pg_indexes"
                " WHERE tablename='publication_links'"
                "   AND upper(indexdef) LIKE '%UNIQUE%'"
            )
        )
    )
    # WP37 迁移 e5f37c0de005 补了 (project_id, canonical_url) 的**项目内**唯一索引（拍板项 1）。
    # 本判据的原意是「不存在 canonical_url 的**全局**唯一」——否则第二个项目物理上无法登记
    # 同一条笔记——所以改成：唯一索引集合恰为 {主键, 项目内唯一}，且项目内唯一必须带 project_id。
    defs = dict(
        conn.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes"
                " WHERE tablename='publication_links'"
                "   AND upper(indexdef) LIKE '%UNIQUE%'"
            )
        ).all()
    )
    emit(sorted(defs) == ["pk_publication_links", "uq_publication_links_project_canonical"]
         and "(project_id, canonical_url)"
         in defs.get("uq_publication_links_project_canonical", ""),
         "[S] publication_links 上没有 canonical_url 全局唯一（唯一索引恰为主键 + 项目内唯一）",
         "唯一索引 " + str(sorted(defs)))

    bad = list(
        (r[0], r[1]) for r in conn.execute(
            text(
                "SELECT conname, confdeltype FROM pg_constraint"
                " WHERE contype='f' AND conrelid = ANY("
                "SELECT oid FROM pg_class WHERE relname = ANY(:names))"
                "   AND confdeltype <> 'r'"
            ),
            {"names": list(OWN_TABLES)},
        )
    )
    emit(not bad, "[S] 全部外键 ON DELETE RESTRICT（零 CASCADE）", "违例 " + str(bad))

    rows = list(
        (r[0], r[1], r[2]) for r in conn.execute(
            text(
                "SELECT table_name, data_type, column_default"
                " FROM information_schema.columns"
                " WHERE table_schema='public' AND column_name='lock_version'"
                "   AND table_name = ANY(:names)"
            ),
            {"names": list(MUTABLE_TABLES)},
        )
    )
    ok = (
        len(rows) == 3
        and all(r[1] == "bigint" and r[2] == "1" for r in rows)
    )
    emit(ok, "[S] lock_version 是 BIGINT DEFAULT 1（草案 §0.1 的 INTEGER/0 已过期）",
         str(rows))

    frozen = delivery_models.frozen_delivery_columns()
    all_columns = set(Base.metadata.tables["deliveries"].columns.keys())
    mutable = set(delivery_models.MUTABLE_DELIVERY_COLUMNS)
    ok = (
        set(frozen) | mutable == all_columns
        and not set(frozen) & mutable
        and len(frozen) >= 10
    )
    emit(ok, "[S] 冻结列由磁盘派生（全列 − 7 列白名单），非空",
         str(len(frozen)) + " 冻结 / " + str(len(mutable)) + " 可变 / 共 "
         + str(len(all_columns)))

    function_ddl = delivery_ddl.frozen_columns_ddl()[0]
    per_column = all(
        ("NEW." + c + " IS DISTINCT FROM OLD." + c) in function_ddl for c in frozen
    )
    ok = (
        per_column
        and function_ddl.count("IS DISTINCT FROM") == len(frozen)
        and "<>" not in function_ddl
        and "RETURN NEW;" in function_ddl
    )
    emit(ok, "[S] 冻结触发器逐列用 IS DISTINCT FROM、零 <>、RETURN NEW",
         "逐列覆盖=" + str(per_column) + " 计数="
         + str(function_ddl.count("IS DISTINCT FROM")))

# --- T：列级冻结的库内活性（逐列变异，名单由磁盘派生） -------------------------
with engine.connect() as conn:
    ids = people_tx(conn)
    export = export_item_tx(conn, ids, ids["project"])
    koc = koc_tx(conn, ids["project"])
    delivery_id = delivery_tx(conn, ids, export, koc=koc)
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    frozen = delivery_models.frozen_delivery_columns()
    for name in frozen:
        column = Base.metadata.tables["deliveries"].columns[name]
        with conn.begin_nested() as sp:
            try:
                conn.execute(
                    text(
                        "UPDATE deliveries SET " + name + " = "
                        + mutation_expression(column) + " WHERE id = :i"
                    ),
                    {"i": delivery_id},
                )
                ok, code = False, None
            except DBAPIError as exc:
                ok, code = True, sqlstate(exc)
                sp.rollback()
            emit(ok and code == CONSISTENCY_SQLSTATE,
                 "[T] 冻结列 " + name + " 拒绝变异", "sqlstate=" + str(code))

    # NULL 方向：IS DISTINCT FROM 与 <> 的唯一区分点。
    with conn.begin_nested() as sp:
        try:
            conn.execute(
                text("UPDATE deliveries SET koc_record_id = NULL WHERE id = :i"),
                {"i": delivery_id},
            )
            ok, code = False, None
        except DBAPIError as exc:
            ok, code = True, sqlstate(exc)
            sp.rollback()
        emit(ok and code == CONSISTENCY_SQLSTATE,
             "[T] 冻结可空列被改成 NULL 也拒绝（<> 写法在此处会静默放过）",
             "sqlstate=" + str(code))

    # 对照：白名单里的列必须真的能改，否则状态机本身不工作（拦得过宽）。
    with conn.begin_nested() as sp:
        conn.execute(
            text("UPDATE deliveries SET note = 'wp36',"
                 " lock_version = lock_version + 1 WHERE id = :i"),
            {"i": delivery_id},
        )
        moved = conn.execute(
            text("SELECT note FROM deliveries WHERE id = :i"), {"i": delivery_id}
        ).scalar_one()
        sp.rollback()
    emit(moved == "wp36", "[T] 可变列（note / lock_version）允许 UPDATE",
         "note=" + str(moved))

    conn.rollback()

# --- C：业务一致性（延迟约束触发器，需 SET CONSTRAINTS IMMEDIATE） --------------
with engine.connect() as conn:
    ids = people_tx(conn)
    export = export_item_tx(conn, ids, ids["project"])
    other_export = export_item_tx(conn, ids, ids["project"])
    foreign_export = export_item_tx(conn, ids, ids["other"])
    koc = koc_tx(conn, ids["project"])
    foreign_koc = koc_tx(conn, ids["other"])
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    with conn.begin_nested() as sp:
        delivery_tx(conn, ids, export, koc=koc)
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        sp.rollback()
    emit(True, "[C1] 冻结字段与导出行逐列一致时放行", "无异常")

    with conn.begin_nested() as sp:
        try:
            delivery_tx(conn, ids, export, koc=koc,
                        over={"v": other_export["version"]})
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            ok, code = False, None
        except DBAPIError as exc:
            ok, code = True, sqlstate(exc)
            sp.rollback()
        emit(ok and code == CONSISTENCY_SQLSTATE,
             "[C2] version_node_id 与导出行不一致被拒（固定导出版本必须复制）",
             "sqlstate=" + str(code))

    with conn.begin_nested() as sp:
        try:
            delivery_tx(conn, ids, export, koc=foreign_koc)
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            ok, code = False, None
        except DBAPIError as exc:
            ok, code = True, sqlstate(exc)
            sp.rollback()
        emit(ok and code == CONSISTENCY_SQLSTATE,
             "[C3] 跨项目 KOC 被拒", "sqlstate=" + str(code))

    with conn.begin_nested() as sp:
        try:
            old = delivery_tx(conn, ids, export, koc=koc)
            delivery_tx(conn, ids, other_export, koc=koc, replaces=old)
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            ok, code = False, None
        except DBAPIError as exc:
            ok, code = True, sqlstate(exc)
            sp.rollback()
        emit(ok and code == CONSISTENCY_SQLSTATE,
             "[C4] 替换链跨内容被拒", "sqlstate=" + str(code))

    with conn.begin_nested() as sp:
        try:
            mine = delivery_tx(conn, ids, export, koc=koc)
            publication_tx(conn, ids, delivery=mine, project_id=ids["other"])
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            ok, code = False, None
        except DBAPIError as exc:
            ok, code = True, sqlstate(exc)
            sp.rollback()
        emit(ok and code == CONSISTENCY_SQLSTATE,
             "[C5] 发布登记引用跨项目交付被拒", "sqlstate=" + str(code))

    emit(foreign_export["item"] is not None,
         "[C6] 探针造数覆盖了另一个项目（跨项目判据的输入活性）",
         "other_project_export=" + foreign_export["item"][:8])

    conn.rollback()

# --- I：幂等载体的库内活性（部分唯一索引真的拦得住） ---------------------------
with engine.connect() as conn:
    ids = people_tx(conn)
    export = export_item_tx(conn, ids, ids["project"])
    second = export_item_tx(conn, ids, ids["project"])
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    with conn.begin_nested() as sp:
        try:
            delivery_tx(conn, ids, export, over={"key": "idem-1"})
            delivery_tx(conn, ids, export, over={"key": "idem-1"})
            ok, code = False, None
        except DBAPIError as exc:
            ok, code = True, sqlstate(exc)
            sp.rollback()
        emit(ok and code == UNIQUE_SQLSTATE,
             "[I1] uq_deliveries_idempotency 拦住同项目同键第二行",
             "sqlstate=" + str(code))

    with conn.begin_nested() as sp:
        try:
            old = delivery_tx(conn, ids, export)
            delivery_tx(conn, ids, export, replaces=old)
            delivery_tx(conn, ids, export, replaces=old)
            ok, code = False, None
        except DBAPIError as exc:
            ok, code = True, sqlstate(exc)
            sp.rollback()
        emit(ok and code == UNIQUE_SQLSTATE,
             "[I2] uq_deliveries_replaces 拦住第二次替换同一行（替换链不是树）",
             "sqlstate=" + str(code))

    with conn.begin_nested() as sp:
        try:
            publication_tx(conn, ids, idem="pidem-1")
            publication_tx(conn, ids, idem="pidem-1")
            ok, code = False, None
        except DBAPIError as exc:
            ok, code = True, sqlstate(exc)
            sp.rollback()
        emit(ok and code == UNIQUE_SQLSTATE,
             "[I3] uq_publications_idempotency 拦住同项目同键第二行",
             "sqlstate=" + str(code))

    emit(second["item"] is not None, "[I4] 探针造了两个导出行（幂等判据的输入活性）",
         "second=" + second["item"][:8])

    conn.rollback()

# --- Z：零残留 ---------------------------------------------------------------
with engine.connect() as conn:
    for table in OWN_TABLES:
        n = conn.execute(text("SELECT count(*) FROM " + table)).scalar_one()
        emit(n == 0, "[Z] 零残留 " + table, str(n) + " 行")
    n = conn.execute(
        text("SELECT count(*) FROM users WHERE issuer=:iss"),
        {"iss": PROBE_ISSUER},
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 users（探针）", str(n) + " 行")

print("RESULT_EMITTED (期望 " + str(EXPECTED_CHECKS) + " 条)")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def collect_wp36() -> tuple[Path, bool]:
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
        "# WP36 KOC 记录 / 交付与替换 / 发布登记：4 张表 / 列集 / CHECK / FK /"
        " 索引（含 3 条部分唯一）/ 3 个触发器（1 列级冻结 + 2 一致性）/"
        " 库内活性验证 + 零残留",
        f"# 生成时间: {_now()}",
        "# 目标库: 见下方 SKIP 行（本包尚无迁移 —— 建迁移是主 agent 独占动作）",
        "# 命令: cd services/backend && python -c '<information_schema + 试写 + 变异>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp36-koc-delivery-publication.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    complete = len(checks) >= EXPECTED_CHECKS
    exact = len(checks) == EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete and exact)


def main() -> int:
    path, ok = collect_wp36()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
