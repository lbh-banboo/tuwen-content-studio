#!/usr/bin/env python
"""WP30 证据采集脚本（AI 自检 / 硬软规则引擎）。

产出的文件（落在 `evidence/m3/`）：

| 文件 | 内容 |
|---|---|
| `wp30-self-check-rules.log` | 3 张表结构逐列 / CHECK / FK / 唯一约束 / 索引 / 5 个触发器 / 两个一致性函数 / 不可变性行为（3 表 UPDATE + 1 次 DELETE）/ `outcome` 四条等价式各一次阴性 / 规则结论四态配对 / 锚点三选一 / evidence 形状 / 两条一致性触发器（含阴性对照）/ 探针零残留 |

## 本包的结构性特点

- 3 张表**全部不可变**（与 WP29 不同：它的 `content_feedback_items` 恰恰必须
  可变）。因此本包**零 `If-Match`**：没有原地改的路径，就没有乐观锁载体。
- `self_check_runs` 的 `outcome` 与五个计数之间有**四条双向等价式 CHECK**
  —— 这是本包最核心的一组结构判据（R6：派生字段漂移时读端点只看 outcome，
  阻断内容会显示为通过）。
- `rule_code` / `skip_reason` **刻意没有取值域 CHECK**（开集，见
  `models.py` 的取舍说明）。本脚本为此加了一条**反向断言**：
  若将来有人"顺手补上"字面量 CHECK，判据会报 FAIL 而不是静默变严。
- 两条一致性触发器都是 `CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY
  DEFERRED`，且 finding 那条**跨两跳 join**（finding → rule_result → run）。

## 目标库与迁移的关系（**如实登记**）

本包的迁移文件由主 agent 串行收口（接缝冻结 §4.1「建迁移是主 agent 的独占
动作」）。因此本脚本当前指向**分支的私有测试库**
（`rednote_test_wp30`，schema 由 `alembic upgrade head` + 本包测试夹具的
`create_all` 建出），并把"迁移 head 不早于 WP30 引入点"这条判据显式
**SKIP** 掉 —— 不是跳过检查，而是如实报告"这条判据的前提还不存在"。

收口时主 agent 应：① 建 `rednote_verify_wp30` 并 `alembic upgrade head`；
② 把 `DEFAULT_DB` 指过去；③ 把 `WP30_MIGRATION_REVISION` 填成 WP30 迁移的
revision，SKIP 行即变成真正的 CHECK。
目标库也可用环境变量 `REDNOTE_WP30_EVIDENCE_DATABASE_URL` 覆盖。

⚠️ **本脚本不建任何表**。结构不全时它报 FAIL 并告诉你先跑迁移/测试 ——
自己建表再核对自己建的表是**循环判据**（恒绿）。

## 探针自身踩过的坑

1. **★ 期望条数必须实测，不能凭印象**（与 WP24~WP29 同型教训）：
   `complete = len(checks) >= EXPECTED_CHECKS` 在期望值偏低时恒真，
   本脚本另加 `exact = len(checks) == EXPECTED_CHECKS` 并入 verdict。
2. **一致性触发器是延迟的**：INSERT 那一刻不检查，必须在事务里
   `SET CONSTRAINTS ALL IMMEDIATE` 才能看到它拒绝，否则"坏值写进去了"
   会被误读成"防线失效"（WP29 的证据脚本踩过同一个坑）。
3. **不可变表探针绝不能 commit**：一旦提交就永远删不掉（触发器拦 DELETE）。
   每条探针独立连接 + 无条件 `rollback()`。
4. **造一条合法的 `content_versions` 行要连带造 `version_nodes` 配对行**
   （`content_versions.id` 同时是 `version_nodes.id` 的 FK）。
5. **造一条合法的反馈行要 `observed_version_id` 非空且属于同一条内容**
   （WP29 的锚点一致性触发器），否则探针自己先炸。
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
EXPECTED_CHECKS: int = 61

#: 目标库。收口时主 agent 改成 `rednote_verify_wp30`（见模块 docstring）。
DEFAULT_DB = "postgresql+psycopg://rednote@localhost:5432/rednote_test_wp30"
DB_ENV = "REDNOTE_WP30_EVIDENCE_DATABASE_URL"

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

INSPECT_SCRIPT = r"""
# 内联脚本：只允许单行注释，且不能出现三引号（三引号会提前终止外层包裹，见 ISS-45）。

import os
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from rednote.modules.review import ddl as review_ddl
from rednote.modules.review import models as review_models

EXPECTED_CHECKS = 61

DB = os.environ.get(
    "REDNOTE_WP30_EVIDENCE_DATABASE_URL",
    "postgresql+psycopg://rednote@localhost:5432/rednote_test_wp30",
)
engine = create_engine(DB)

# WP30 的迁移由主 agent 收口时建立。收口时把本常量填成那条 revision，
# 下面的 SKIP 行就会变成一条真正的 CHECK（判据实现见 _revision_is_at_least：
# 它沿真实的迁移链序比较，而不是 `head == <revision>` —— 迁移 head 是单调
# 推进的，用等于写会在下一个工作包失效）。
WP30_MIGRATION_REVISION = "c01128062b30"

OWN_TABLES = review_models.OWN_TABLES

TABLE_COLUMNS = (
    ("self_check_runs", 17),
    ("self_check_rule_results", 11),
    ("self_check_findings", 9),
)

TRIGGER_NAMES = review_ddl.trigger_names()
CONSISTENCY_FUNCTIONS = review_ddl.consistency_function_names()
IMMUTABLE_VIOLATION_SQLSTATE = "23001"
UNIQUE_VIOLATION_SQLSTATE = "23505"
CONSISTENCY_SQLSTATE = review_ddl.CONSISTENCY_SQLSTATE

# PostgreSQL 标识符上限（NAMEDATALEN - 1）。超限会被**静默截断**并追加哈希，
# 于是 ORM 元数据里的约束名与 pg_constraint 里的实际名字永久不一致。
MAX_IDENTIFIER_BYTES = 63

PROBE_ISSUER = "wp30-probe"
PROBE_PROJECT_NAME = "WP30 探针项目"
PROBE_PROJECT_PREFIX = "WP30PROBE"


def emit(ok, label, detail):
    print(f"CHECK {'OK  ' if ok else 'FAIL'} {label}: {detail}")


def skip(label, detail):
    print(f"SKIP {label}: {detail}")


def sqlstate(exc):
    return getattr(exc.orig, "sqlstate", None)


def _revision_is_at_least(current, introduced):
    # 沿**真实的迁移链序**比较"当前 head 不早于引入点"。
    # 不用 `current == introduced`：迁移 head 是单调推进的，等值写法会在
    # 下一个工作包落迁移时失效（而失效的表现是判据报红，不是静默）。
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
    if current not in chain or introduced not in chain:
        return False
    return chain.index(current) <= chain.index(introduced)


def model_constraint_names(table, kind):
    return {
        c.name
        for c in review_models.Base.metadata.tables[table].constraints
        if c.__class__.__name__ == kind
    }


def db_constraint_names(conn, table, contype):
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


def _people_tx(conn):
    uid = uuid.uuid4()
    brand = uuid.uuid4()
    prod = uuid.uuid4()
    proj = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, display_name, role, status)"
            " VALUES (:u, :iss, :sub, 'WP30 探针', 'ADMIN', 'ACTIVE')"
        ),
        {"u": str(uid), "iss": PROBE_ISSUER, "sub": f"probe-{uid}"},
    )
    conn.execute(
        text("INSERT INTO brands (id, name, status) VALUES (:b, :n, 'ACTIVE')"),
        {"b": str(brand), "n": f"WP30 品牌 {str(brand)[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO products (id, brand_id, name, model_code, status)"
            " VALUES (:p, :b, :n, :c, 'ACTIVE')"
        ),
        {
            "p": str(prod), "b": str(brand),
            "n": f"WP30 产品 {str(prod)[:8]}", "c": f"WP30-{str(prod)[:8]}",
        },
    )
    conn.execute(
        text(
            "INSERT INTO projects (id, project_code, name, brand_id, product_id,"
            " owner_id, target_delivery_count, due_date)"
            " VALUES (:p, :c, :n, :b, :pr, :o, 1, (now() + interval '30 days')::date)"
        ),
        {
            "p": str(proj), "c": f"{PROBE_PROJECT_PREFIX}{str(proj)[:8]}",
            "n": PROBE_PROJECT_NAME, "b": str(brand), "pr": str(prod), "o": str(uid),
        },
    )
    return {"user": str(uid), "project": str(proj)}


def _content_tx(conn, ids):
    content_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO content_items (id, project_id, state, current_version_no,"
            " lock_version) VALUES (:c, :p, 'DRAFT', 0, 0)"
        ),
        {"c": str(content_id), "p": ids["project"]},
    )
    return content_id


def _version_pair_tx(conn, content_id, version_no=1):
    node_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
            " payload_hash, schema_version)"
            " VALUES (:n, 'CONTENT', :e, :r, :h, 1)"
        ),
        {"n": str(node_id), "e": str(content_id), "r": version_no,
         "h": f"h-{str(node_id)[:12]}"},
    )
    conn.execute(
        text(
            "INSERT INTO content_versions (id, content_item_id, version_no,"
            " title, body, tags, bindings, payload, payload_hash, schema_version)"
            " VALUES (:id, :c, :vno, 'T', 'B', cast('[]' as jsonb),"
            " cast('[]' as jsonb), cast('{}' as jsonb), :ph, 1)"
        ),
        {"id": str(node_id), "c": str(content_id), "vno": version_no,
         "ph": f"ph-{str(node_id)[:12]}"},
    )
    return node_id


def _media_item_tx(conn, ids, content_id):
    item_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO media_items (id, project_id, content_item_id,"
            " paragraph_ref, sort_order, state, current_version_no, created_by,"
            " lock_version)"
            " VALUES (:i, :p, :c, :pr, 0, 'PENDING', 0, :cb, 0)"
        ),
        {"i": str(item_id), "p": ids["project"], "c": str(content_id),
         "pr": f"p0-{uuid.uuid4().hex[:8]}", "cb": ids["user"]},
    )
    return item_id


def _feedback_tx(conn, ids, content_id, version_id):
    fb_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO content_feedback_items (id, project_id, content_item_id,"
            " observed_version_id, anchor_kind, category, severity, scope,"
            " author_kind, body, created_by)"
            " VALUES (:i, :p, :c, :v, 'WHOLE', 'FACT', 'BLOCKING', 'CURRENT_ONLY',"
            " 'AI', 'WP30 探针阻断项', :cb)"
        ),
        {"i": str(fb_id), "p": ids["project"], "c": str(content_id),
         "v": str(version_id), "cb": ids["user"]},
    )
    return fb_id


def _run_tx(conn, ids, content_id, version_id, **over):
    run_id = over.get("run_id") or uuid.uuid4()
    params = {
        "i": str(run_id),
        "p": str(over.get("project_id") or ids["project"]),
        "c": str(over.get("content_id") or content_id),
        "v": str(over.get("subject_version_id") or version_id),
        "outcome": over.get("outcome", "PASSED"),
        "bc": over.get("blocking_count", 0),
        "wc": over.get("warning_count", 0),
        "ic": over.get("info_count", 0),
        "ec": over.get("evaluated_count", 1),
        "sc": over.get("skipped_count", 0),
        "errc": over.get("errored_count", 0),
        "by": ids["user"],
    }
    conn.execute(
        text(
            "INSERT INTO self_check_runs (id, project_id, content_item_id,"
            " subject_version_id, rule_set_version, trigger_kind, outcome,"
            " blocking_count, warning_count, info_count, evaluated_count,"
            " skipped_count, errored_count, started_at, finished_at, created_by)"
            " VALUES (:i, :p, :c, :v, 1, 'MANUAL', :outcome, :bc, :wc, :ic,"
            " :ec, :sc, :errc, now(), now(), :by)"
        ),
        params,
    )
    return run_id


def _result_tx(conn, run_id, **over):
    result_id = over.get("result_id") or uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO self_check_rule_results (id, run_id, rule_code, category,"
            " rule_kind, status, severity, skip_reason, error_note, finding_count)"
            " VALUES (:i, :r, :code, :cat, :kind, :st, :sev, :skip, :note, :fc)"
        ),
        {
            "i": str(result_id),
            "r": str(run_id),
            "code": over.get("rule_code", "STRUCTURE_VERSION_WITHOUT_BLUEPRINT"),
            "cat": over.get("category", "STRUCTURE"),
            "kind": over.get("rule_kind", "HARD"),
            "st": over.get("status", "PASSED"),
            "sev": over.get("severity"),
            "skip": over.get("skip_reason"),
            "note": over.get("error_note"),
            "fc": over.get("finding_count", 0),
        },
    )
    return result_id


def _finding_tx(conn, result_id, **over):
    finding_id = uuid.uuid4()
    evidence = over.get("evidence", "cast('{}' as jsonb)")
    conn.execute(
        text(
            "INSERT INTO self_check_findings (id, rule_result_id, anchor_kind,"
            " paragraph_ref, media_item_id, message, evidence, feedback_item_id)"
            f" VALUES (:i, :r, :ak, :ref, :mid, :msg, {evidence}, :fid)"
        ),
        {
            "i": str(finding_id),
            "r": str(result_id),
            "ak": over.get("anchor_kind", "WHOLE"),
            "ref": over.get("paragraph_ref"),
            "mid": over.get("media_item_id"),
            "msg": over.get("message", "WP30 探针命中"),
            "fid": over.get("feedback_item_id"),
        },
    )
    return finding_id


def expect_rejected(conn, label, expected_sqlstate, action):
    # 在 savepoint 里跑一次应当被拒的写入；拒了就报 OK 并回滚 savepoint。
    with conn.begin_nested() as sp:
        try:
            action()
            ok, code = False, None
        except DBAPIError as exc:
            ok, code = True, sqlstate(exc)
            sp.rollback()
        emit(ok and code == expected_sqlstate,
             label, f"sqlstate={code} 期望={expected_sqlstate}")


def expect_allowed(conn, label, action):
    # 阴性对照：合法写入必须放行（证明上面的拒绝不是"什么都拒"）。
    with conn.begin_nested() as sp:
        try:
            action()
            ok, detail = True, "无异常"
        except DBAPIError as exc:
            ok, detail = False, f"被拒 sqlstate={sqlstate(exc)}"
        sp.rollback()
    emit(ok, label, detail)


# --- S：结构 -----------------------------------------------------------------
with engine.connect() as conn:
    # ⚠️ 默认目标库 `rednote_test_wp30` 是 create_all 建的（本脚本其余全部
    # 60 条检查的正常前提），**没有** `alembic_version` 表——查它会是
    # `UndefinedTable`，不是"没升级"。2026-10-19 实测踩到过（全量复跑崩栈，
    # exit=1、零 RESULT 行）。先探测表是否存在，不存在就和
    # `WP30_MIGRATION_REVISION is None` 走同一条 SKIP 路径；只有指向真正
    # 迁移过的库（`rednote_verify_wp30`，经 `REDNOTE_WP30_EVIDENCE_DATABASE_URL`
    # 覆盖）时，这张表才存在，SKIP 才变成本节注释里说的那条真正 CHECK。
    has_alembic_table = conn.execute(
        text(
            "SELECT to_regclass('public.alembic_version') IS NOT NULL"
        )
    ).scalar_one()
    current = None
    if has_alembic_table:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        current = row[0] if row else None
    if WP30_MIGRATION_REVISION is None or not has_alembic_table:
        skip("[S] 迁移 head 不早于 WP30 引入点",
             f"WP30 迁移由主 agent 收口时建立（当前 head={current}"
             f"{'' if has_alembic_table else '；目标库无 alembic_version 表，非迁移建库'}）")
    else:
        emit(_revision_is_at_least(current, WP30_MIGRATION_REVISION),
             "[S] 迁移 head 不早于 WP30 引入点", f"current={current}")

    n = conn.execute(
        text(
            "SELECT count(*) FROM information_schema.tables"
            " WHERE table_schema='public' AND table_name = ANY(:names)"
        ),
        {"names": list(OWN_TABLES)},
    ).scalar_one()
    emit(n == 3, "[S] 本包 3 张表存在",
         f"{n}/3（缺则先跑 alembic upgrade head 或本包测试；本脚本不建表）")

    for table, expect in TABLE_COLUMNS:
        n = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_schema='public' AND table_name=:t"
            ),
            {"t": table},
        ).scalar_one()
        emit(n == expect, f"[S] {table} 列数", f"实际 {n} / 期望 {expect}")

    for table in OWN_TABLES:
        expected = model_constraint_names(table, "CheckConstraint")
        actual = db_constraint_names(conn, table, "c")
        emit(expected == actual, f"[S] {table} CHECK 名集合与模型一致",
             f"模型 {len(expected)} / 库 {len(actual)} 差集 {sorted(expected ^ actual)}")

    for table in OWN_TABLES:
        expected = model_constraint_names(table, "ForeignKeyConstraint")
        actual = db_constraint_names(conn, table, "f")
        emit(expected == actual, f"[S] {table} FK 名集合与模型一致",
             f"模型 {len(expected)} / 库 {len(actual)} 差集 {sorted(expected ^ actual)}")

    expected_uq = model_constraint_names("self_check_rule_results", "UniqueConstraint")
    actual_uq = db_constraint_names(conn, "self_check_rule_results", "u")
    emit(expected_uq == actual_uq and len(actual_uq) == 1,
         "[S] (run_id, rule_code) 唯一约束存在", f"库 {sorted(actual_uq)}")

    expected_ix = set()
    for table in OWN_TABLES:
        for index in review_models.Base.metadata.tables[table].indexes:
            expected_ix.add(index.name)
    actual_ix = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT indexname FROM pg_indexes"
                " WHERE schemaname='public' AND tablename = ANY(:names)"
            ),
            {"names": list(OWN_TABLES)},
        )
    }
    # 库里还有主键与唯一约束自带的索引，模型侧的 Index 只是其中一部分。
    emit(expected_ix <= actual_ix,
         "[S] 模型声明的索引全部存在于库",
         f"模型 {len(expected_ix)} 缺 {sorted(expected_ix - actual_ix)}")

    actual_triggers = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT tgname FROM pg_trigger WHERE tgrelid = ANY("
                "SELECT oid FROM pg_class WHERE relname = ANY(:names)"
                ") AND NOT tgisinternal"
            ),
            {"names": list(OWN_TABLES)},
        )
    }
    emit(actual_triggers == set(TRIGGER_NAMES),
         "[S] 5 个触发器名集合一致（3 不可变 + 2 一致性）",
         f"库 {sorted(actual_triggers)}")

    for function in CONSISTENCY_FUNCTIONS:
        n = conn.execute(
            text("SELECT count(*) FROM pg_proc WHERE proname=:f"), {"f": function}
        ).scalar_one()
        emit(n == 1, f"[S] 一致性函数存在 {function}", f"{n} 个")

    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname='rednote_reject_mutation'")
    ).scalar_one()
    emit(n == 1, "[S] 跨包共用不可变函数仍存在", "rednote_reject_mutation")

    # ★ 反向断言：rule_code / skip_reason 是**开集**，刻意不落取值域 CHECK。
    #   若有人"顺手补上"字面量 CHECK，每加一条规则就要一次迁移，
    #   且化石与注册表的分叉不会报错（新规则的结果静默写不进去）。
    defs = [
        row[0]
        for row in conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE contype='c'"
                " AND conrelid = CAST('self_check_rule_results' AS regclass)"
            )
        )
    ]
    leaked = [d for d in defs if "rule_code IN (" in d or "skip_reason IN (" in d]
    emit(not leaked, "[S] rule_code / skip_reason 无取值域 CHECK（刻意的取舍）",
         f"命中 {leaked}")

    # 全部 FK 必须 ON DELETE RESTRICT（本包没有任何级联删除）。
    non_restrict = [
        row[0]
        for row in conn.execute(
            text(
                "SELECT conname FROM pg_constraint"
                " WHERE contype='f' AND confdeltype <> 'r'"
                " AND conrelid = ANY("
                "SELECT oid FROM pg_class WHERE relname = ANY(:names))"
            ),
            {"names": list(OWN_TABLES)},
        )
    ]
    emit(not non_restrict, "[S] 全部 FK 都是 ON DELETE RESTRICT",
         f"非 RESTRICT {non_restrict}")

    # 标识符长度：超 63 字节会被库静默截断，导致模型名与库名永久不一致。
    too_long = [
        row[0]
        for row in conn.execute(
            text(
                "SELECT conname FROM pg_constraint"
                " WHERE length(conname) > :m AND conrelid = ANY("
                "SELECT oid FROM pg_class WHERE relname = ANY(:names))"
            ),
            {"m": MAX_IDENTIFIER_BYTES, "names": list(OWN_TABLES)},
        )
    ]
    emit(not too_long, "[S] 全部约束名在 63 字节内（不会被静默截断）",
         f"超限 {too_long}")

# --- T：不可变性 --------------------------------------------------------------
with engine.connect() as conn:
    ids = _people_tx(conn)
    content_id = _content_tx(conn, ids)
    version_id = _version_pair_tx(conn, content_id)
    run_id = _run_tx(conn, ids, content_id, version_id)
    result_id = _result_tx(conn, run_id)
    finding_id = _finding_tx(conn, result_id)
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    expect_rejected(
        conn, "[T] self_check_runs 拒绝 UPDATE", IMMUTABLE_VIOLATION_SQLSTATE,
        lambda: conn.execute(
            text("UPDATE self_check_runs SET trigger_kind='BATCH' WHERE id=:i"),
            {"i": str(run_id)},
        ),
    )
    expect_rejected(
        conn, "[T] self_check_rule_results 拒绝 UPDATE",
        IMMUTABLE_VIOLATION_SQLSTATE,
        lambda: conn.execute(
            text("UPDATE self_check_rule_results SET rule_code='X' WHERE id=:i"),
            {"i": str(result_id)},
        ),
    )
    expect_rejected(
        conn, "[T] self_check_findings 拒绝 UPDATE", IMMUTABLE_VIOLATION_SQLSTATE,
        lambda: conn.execute(
            text("UPDATE self_check_findings SET message='X' WHERE id=:i"),
            {"i": str(finding_id)},
        ),
    )
    expect_rejected(
        conn, "[T] self_check_runs 拒绝 DELETE", IMMUTABLE_VIOLATION_SQLSTATE,
        lambda: conn.execute(
            text("DELETE FROM self_check_runs WHERE id=:i"), {"i": str(run_id)}
        ),
    )
    conn.rollback()

# --- O：outcome 四条双向等价式各一次阴性 + 空转轮次 ----------------------------
with engine.connect() as conn:
    ids = _people_tx(conn)
    content_id = _content_tx(conn, ids)
    version_id = _version_pair_tx(conn, content_id)

    cases = (
        ("[O1] outcome=PASSED 但 blocking_count>0 被拒",
         {"outcome": "PASSED", "blocking_count": 1}),
        ("[O2] outcome=BLOCKED 但 blocking_count=0 被拒",
         {"outcome": "BLOCKED"}),
        ("[O3] outcome=WARNED 但 warning_count=0 被拒",
         {"outcome": "WARNED"}),
        ("[O4] outcome=ERRORED 但 errored_count=0 被拒",
         {"outcome": "ERRORED"}),
        ("[O5] outcome=PASSED 但 errored_count>0 被拒（fail-closed）",
         {"outcome": "PASSED", "errored_count": 1}),
        ("[O6] evaluated_count=0（空转轮次）被拒",
         {"evaluated_count": 0}),
    )
    for label, over in cases:
        expect_rejected(
            conn, label, CONSISTENCY_SQLSTATE,
            lambda over=over: _run_tx(conn, ids, content_id, version_id, **over),
        )
    expect_allowed(
        conn, "[O7] outcome=BLOCKED 且 blocking_count=1 放行（阴性对照）",
        lambda: _run_tx(
            conn, ids, content_id, version_id, outcome="BLOCKED", blocking_count=1
        ),
    )
    conn.rollback()

# --- R：规则结论四态配对 + 唯一约束 -------------------------------------------
with engine.connect() as conn:
    ids = _people_tx(conn)
    content_id = _content_tx(conn, ids)
    version_id = _version_pair_tx(conn, content_id)
    run_id = _run_tx(conn, ids, content_id, version_id)
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    bad_results = (
        ("[R1] FAILED 却无 severity 被拒",
         {"status": "FAILED", "finding_count": 1}),
        ("[R2] FAILED 却零 finding 被拒",
         {"status": "FAILED", "severity": "WARNING", "finding_count": 0}),
        ("[R3] 非 FAILED 却带 severity 被拒",
         {"status": "PASSED", "severity": "WARNING"}),
        ("[R4] SKIPPED 却无 skip_reason 被拒", {"status": "SKIPPED"}),
        ("[R5] 非 SKIPPED 却带 skip_reason 被拒",
         {"status": "PASSED", "skip_reason": "MODEL_RULE_NOT_ENABLED"}),
        ("[R6] ERRORED 却无 error_note 被拒", {"status": "ERRORED"}),
        ("[R7] 非 ERRORED 却带 error_note 被拒",
         {"status": "PASSED", "error_note": "x"}),
        ("[R8] 未知 status 被拒（闭集）", {"status": "MAYBE"}),
        ("[R9] 未知 category 被拒（闭集）",
         {"status": "PASSED", "category": "VIBES"}),
        ("[R10] 未知 rule_kind 被拒（闭集）",
         {"status": "PASSED", "rule_kind": "MEDIUM"}),
        ("[R11] 空 rule_code 被拒", {"status": "PASSED", "rule_code": ""}),
    )
    for label, over in bad_results:
        expect_rejected(
            conn, label, CONSISTENCY_SQLSTATE,
            lambda over=over: _result_tx(conn, run_id, **over),
        )

    # 开集放行（刻意的取舍）：未注册 code + 未登记 skip_reason，库里不拦。
    expect_allowed(
        conn, "[R12] 未注册 rule_code / 未登记 skip_reason 被放行（开集）",
        lambda: _result_tx(
            conn, run_id, rule_code="NOT_A_REGISTERED_RULE", status="SKIPPED",
            skip_reason="NOT_A_REGISTERED_REASON",
        ),
    )

    _result_tx(conn, run_id)
    expect_rejected(
        conn, "[R13] (run_id, rule_code) 唯一", UNIQUE_VIOLATION_SQLSTATE,
        lambda: _result_tx(conn, run_id),
    )
    conn.rollback()

# --- A：锚点三选一 + evidence 形状 --------------------------------------------
with engine.connect() as conn:
    ids = _people_tx(conn)
    content_id = _content_tx(conn, ids)
    version_id = _version_pair_tx(conn, content_id)
    media_id = _media_item_tx(conn, ids, content_id)
    run_id = _run_tx(conn, ids, content_id, version_id)
    result_id = _result_tx(conn, run_id)
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    bad_anchors = (
        ("[A1] PARAGRAPH 却无 paragraph_ref 被拒", {"anchor_kind": "PARAGRAPH"}),
        ("[A2] MEDIA 却无 media_item_id 被拒", {"anchor_kind": "MEDIA"}),
        ("[A3] WHOLE 却带 paragraph_ref 被拒",
         {"anchor_kind": "WHOLE", "paragraph_ref": "p0-aaaaaaaa"}),
        ("[A4] 未知 anchor_kind 被拒（闭集）", {"anchor_kind": "SENTENCE"}),
        ("[A5] evidence 不是 JSON 对象被拒",
         {"evidence": "cast('[]' as jsonb)"}),
        ("[A6] 空 message 被拒", {"message": ""}),
    )
    for label, over in bad_anchors:
        expect_rejected(
            conn, label, CONSISTENCY_SQLSTATE,
            lambda over=over: _finding_tx(conn, result_id, **over),
        )
    expect_allowed(
        conn, "[A7] MEDIA + 同内容 media_item_id 放行（阴性对照）",
        lambda: _finding_tx(
            conn, result_id, anchor_kind="MEDIA", media_item_id=str(media_id)
        ),
    )
    conn.rollback()

# --- C：两条一致性触发器（DEFERRABLE ⇒ 必须 SET CONSTRAINTS ALL IMMEDIATE） ---
with engine.connect() as conn:
    ids = _people_tx(conn)
    other = _people_tx(conn)
    content_a = _content_tx(conn, ids)
    content_b = _content_tx(conn, ids)
    version_a = _version_pair_tx(conn, content_a)
    version_b = _version_pair_tx(conn, content_b)
    media_a = _media_item_tx(conn, ids, content_a)
    media_b = _media_item_tx(conn, ids, content_b)
    feedback_b = _feedback_tx(conn, ids, content_b, version_b)
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    def _bad_version():
        _run_tx(conn, ids, content_a, version_a, subject_version_id=version_b)
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    expect_rejected(conn, "[C1] 轮次判定的版本跨内容被拒",
                    CONSISTENCY_SQLSTATE, _bad_version)

    def _bad_project():
        _run_tx(conn, ids, content_a, version_a, project_id=other["project"])
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    expect_rejected(conn, "[C2] 轮次记的项目与内容归属不一致被拒",
                    CONSISTENCY_SQLSTATE, _bad_project)

    def _good_run():
        _run_tx(conn, ids, content_a, version_a)
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    expect_allowed(conn, "[C3] 轮次归属一致放行（阴性对照）", _good_run)

    run_a = _run_tx(conn, ids, content_a, version_a)
    result_a = _result_tx(
        conn, run_a, rule_code="MEDIA_ITEM_NOT_READY",
        category="MEDIA_CONSISTENCY", status="FAILED", severity="WARNING",
        finding_count=1,
    )
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    def _bad_media():
        _finding_tx(conn, result_a, anchor_kind="MEDIA", media_item_id=str(media_b))
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    expect_rejected(conn, "[C4] finding 引用的媒体跨内容被拒（跨两跳 join）",
                    CONSISTENCY_SQLSTATE, _bad_media)

    def _bad_feedback():
        _finding_tx(conn, result_a, feedback_item_id=str(feedback_b))
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    expect_rejected(conn, "[C5] finding 回指的 AI 反馈跨内容被拒",
                    CONSISTENCY_SQLSTATE, _bad_feedback)

    def _good_media():
        _finding_tx(conn, result_a, anchor_kind="MEDIA", media_item_id=str(media_a))
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    expect_allowed(conn, "[C6] finding 引用同内容的媒体放行（阴性对照）",
                   _good_media)
    conn.rollback()

# --- Z：探针零残留（按探针项目隔离，不数全库行） -------------------------------
with engine.connect() as conn:
    n = conn.execute(
        text(
            "SELECT count(*) FROM self_check_runs r JOIN projects p"
            " ON p.id = r.project_id WHERE p.name = :n"
        ),
        {"n": PROBE_PROJECT_NAME},
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 self_check_runs（探针项目）", f"{n} 行")

    n = conn.execute(
        text(
            "SELECT count(*) FROM self_check_rule_results rr"
            " JOIN self_check_runs r ON r.id = rr.run_id"
            " JOIN projects p ON p.id = r.project_id WHERE p.name = :n"
        ),
        {"n": PROBE_PROJECT_NAME},
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 self_check_rule_results（探针项目）", f"{n} 行")

    n = conn.execute(
        text(
            "SELECT count(*) FROM self_check_findings f"
            " JOIN self_check_rule_results rr ON rr.id = f.rule_result_id"
            " JOIN self_check_runs r ON r.id = rr.run_id"
            " JOIN projects p ON p.id = r.project_id WHERE p.name = :n"
        ),
        {"n": PROBE_PROJECT_NAME},
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 self_check_findings（探针项目）", f"{n} 行")

    n = conn.execute(
        text("SELECT count(*) FROM users WHERE issuer=:iss"),
        {"iss": PROBE_ISSUER},
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 users（探针）", f"{n} 行")

print(f"RESULT_EMITTED (期望 {EXPECTED_CHECKS} 条)")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def collect_wp30() -> tuple[Path, bool]:
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
        "# WP30 AI 自检与硬软规则：3 张不可变表 / CHECK / FK / 唯一约束 / 索引 /"
        " 5 个触发器（3 不可变 + 2 一致性）/ outcome 四条等价式 / 规则结论四态配对"
        " / 锚点三选一 / 库内活性验证 + 探针零残留",
        f"# 生成时间: {_now()}",
        f"# 目标库: {env[DB_ENV]}",
        "#   （WP30 迁移由主 agent 收口时建立；届时改 DEFAULT_DB 指向"
        " rednote_verify_wp30 并填 WP30_MIGRATION_REVISION，SKIP 行变 CHECK）",
        "# 命令: cd services/backend && python -c '<information_schema + 试写 + 试插>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp30-self-check-rules.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    complete = len(checks) >= EXPECTED_CHECKS
    exact = len(checks) == EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete and exact)


def main() -> int:
    path, ok = collect_wp30()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
