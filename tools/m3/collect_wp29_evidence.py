#!/usr/bin/env python
"""WP29 证据采集脚本。

产出的文件（落在 `evidence/m3/`）：

| 文件 | 内容 |
|---|---|
| `wp29-feedback-reviews.log` | 3 张表结构 / CHECK / FK / 索引 / 3 个触发器（2 不可变 + 1 锚点一致性）/ 库内活性验证（不可变 2 条 + 可变 1 条 + 锚点一致性 6 条）/ 零残留 |

## 本包的结构性特点

- 3 张表全部在本包：`content_feedback_items`（可变，`lock_version` 是 ETag 载体）、
  `content_feedback_events`（不可变，追加式状态流转）、`content_reviews`
  （不可变，`supersedes_id` 自引用延迟 FK 改判）。
- **没有** OP-88 式的 `version_nodes` 配对（反馈/审核不是"内容的一个版本"，
  不进注册表），因此没有环 FK、没有"节点收窄守卫"判据——这是与 WP26/WP27 的
  结构性差异，不是漏做。
- 锚点一致性由**单个**延迟约束触发器 `rednote_check_feedback_anchor_consistency`
  同时守三条判据（media_item_id / observed_version_id / resolved_version_id
  各自必须属于同一条内容），仿 `content/ddl.py` "五条判据合一个函数"的理由：
  三条都是"这一行写错了外键列"，修法同类，不需要区分到条。
- 两条 FK（`content_feedback_events` 指向 `content_feedback_items` /
  `content_versions`）的约束名被**手动缩短**（`modules/feedback/models.py`
  已注明）：按命名约定自动生成的全名超过 PostgreSQL 63 字节标识符上限，
  会被库静默截断——本脚本按**缩短后的名字**核对，不是裸猜。

## 探针自身踩过的坑

1. **★ 期望条数必须实测，不能凭印象**（与 WP24~WP27 同型教训）：
   `complete = len(checks) >= EXPECTED_CHECKS` 在期望值偏低时恒真，
   本脚本另加 `exact = len(checks) == EXPECTED_CHECKS` 并入 verdict。
2. **锚点一致性触发器是 `CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED`**：
   INSERT 那一刻不检查，必须在事务里 `SET CONSTRAINTS ALL IMMEDIATE`
   才能看到它拒绝，否则"坏值写进去了"会被误读成"防线失效"。
3. **`observed_version_id` 非空、必须先有合法的 `content_versions` 行**：
   造它要连带造 `version_nodes` 配对行（`content_versions.id` 同时是
   `version_nodes.id` 的 FK），与 WP26 证据脚本的 `_pair_tx` 同一手法。
4. **不可变表探针绝不能 commit**：一旦提交就永远删不掉（触发器拦 DELETE）。
   每条探针独立连接 + 无条件 `rollback()`。
5. **内联脚本只允许单行注释且不能出现三引号**（ISS-45），由
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

#: 期望条数（与内联脚本里的 EXPECTED_CHECKS 保持同值，实测回填）。
EXPECTED_CHECKS: int = 27

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

INSPECT_SCRIPT = r"""
# 内联脚本：只允许单行注释，且不能出现三引号（三引号会提前终止外层包裹，见 ISS-45）。

import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from rednote.modules.feedback import ddl as feedback_ddl
from rednote.modules.feedback import models as feedback_models

EXPECTED_CHECKS = 27

DB = "postgresql+psycopg://rednote@localhost:5432/rednote_verify_wp29"
engine = create_engine(DB)

REVISION_INTRODUCED = "efa306a18363"

OWN_TABLES = ("content_feedback_items", "content_feedback_events", "content_reviews")

TABLE_COLUMNS = (
    ("content_feedback_items", 21),
    ("content_feedback_events", 10),
    ("content_reviews", 10),
)

TRIGGER_NAMES = (
    "trg_content_feedback_events_immutable",
    "trg_content_reviews_immutable",
    "trg_content_feedback_items_anchor_consistency",
)

CONSISTENCY_FUNCTION = "rednote_check_feedback_anchor_consistency"
IMMUTABLE_VIOLATION_SQLSTATE = "23001"
CONSISTENCY_SQLSTATE = feedback_ddl.CONSISTENCY_SQLSTATE

PROBE_ISSUER = "wp29-probe"
PROBE_PROJECT_NAME = "WP29 探针项目"
PROBE_PROJECT_PREFIX = "WP29PROBE"


def emit(ok, label, detail):
    print(f"CHECK {'OK  ' if ok else 'FAIL'} {label}: {detail}")


def sqlstate(exc):
    return getattr(exc.orig, "sqlstate", None)


def _revision_is_at_least(current, introduced):
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


def _people_tx(conn):
    uid = uuid.uuid4()
    brand = uuid.uuid4()
    prod = uuid.uuid4()
    proj = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, display_name, role, status)"
            " VALUES (:u, :iss, :sub, 'WP29 探针', 'ADMIN', 'ACTIVE')"
        ),
        {"u": str(uid), "iss": PROBE_ISSUER, "sub": f"probe-{uid}"},
    )
    conn.execute(
        text("INSERT INTO brands (id, name, status) VALUES (:b, :n, 'ACTIVE')"),
        {"b": str(brand), "n": f"WP29 品牌 {str(brand)[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO products (id, brand_id, name, model_code, status)"
            " VALUES (:p, :b, :n, :c, 'ACTIVE')"
        ),
        {
            "p": str(prod), "b": str(brand),
            "n": f"WP29 产品 {str(prod)[:8]}", "c": f"WP29-{str(prod)[:8]}",
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


def _feedback_item_tx(conn, ids, content_id, version_id, *, anchor_kind="WHOLE",
                       media_item_id=None, resolved_version_id=None):
    fb_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO content_feedback_items (id, project_id, content_item_id,"
            " observed_version_id, anchor_kind, media_item_id, category, severity,"
            " scope, author_kind, body, resolved_version_id, created_by)"
            " VALUES (:i, :p, :c, :v, :ak, :mi, 'OTHER', 'INFO', 'CURRENT_ONLY',"
            " 'USER', 'WP29 探针意见', :rv, :cb)"
        ),
        {
            "i": str(fb_id), "p": ids["project"], "c": str(content_id),
            "v": str(version_id), "ak": anchor_kind,
            "mi": str(media_item_id) if media_item_id else None,
            "rv": str(resolved_version_id) if resolved_version_id else None,
            "cb": ids["user"],
        },
    )
    return fb_id


checks = []

# --- S：结构 -----------------------------------------------------------------
with engine.connect() as conn:
    row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    current = row[0] if row else None
    emit(_revision_is_at_least(current, REVISION_INTRODUCED),
         "[S] 迁移 head 不早于 WP29 引入点", f"current={current}")

    n = conn.execute(
        text(
            "SELECT count(*) FROM information_schema.tables"
            " WHERE table_schema='public' AND table_name = ANY(:names)"
        ),
        {"names": list(OWN_TABLES)},
    ).scalar_one()
    emit(n == 3, "[S] 本包 3 张表存在", f"{n}/3")

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
        expected = {
            c.name for c in feedback_models.Base.metadata.tables[table].constraints
            if c.__class__.__name__ == "CheckConstraint"
        }
        actual = {
            r[0] for r in conn.execute(
                text(
                    "SELECT conname FROM pg_constraint"
                    " WHERE contype='c' AND conrelid = CAST(:t AS regclass)"
                ),
                {"t": table},
            )
        }
        emit(expected == actual, f"[S] {table} CHECK 名集合与模型一致",
             f"模型 {len(expected)} / 库 {len(actual)}")

    for table in OWN_TABLES:
        expected = {
            c.name for c in feedback_models.Base.metadata.tables[table].constraints
            if c.__class__.__name__ == "ForeignKeyConstraint"
        }
        actual = {
            r[0] for r in conn.execute(
                text(
                    "SELECT conname FROM pg_constraint"
                    " WHERE contype='f' AND conrelid = CAST(:t AS regclass)"
                ),
                {"t": table},
            )
        }
        emit(expected == actual, f"[S] {table} FK 名集合与模型一致",
             f"模型 {len(expected)} / 库 {len(actual)}")

    actual_triggers = {
        r[0] for r in conn.execute(
            text(
                "SELECT tgname FROM pg_trigger WHERE tgrelid = ANY("
                "SELECT oid FROM pg_class WHERE relname = ANY(:names)"
                ") AND NOT tgisinternal"
            ),
            {"names": list(OWN_TABLES)},
        )
    }
    emit(actual_triggers == set(TRIGGER_NAMES), "[S] 3 个触发器名集合一致",
         f"库 {sorted(actual_triggers)}")

    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname=:f"),
        {"f": CONSISTENCY_FUNCTION},
    ).scalar_one()
    emit(n == 1, "[S] 锚点一致性函数存在", CONSISTENCY_FUNCTION)

    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname='rednote_reject_mutation'")
    ).scalar_one()
    emit(n == 1, "[S] 跨包共用不可变函数仍存在", "rednote_reject_mutation")

# --- T：不可变性 / 可变性 -----------------------------------------------------
with engine.connect() as conn:
    ids = _people_tx(conn)
    content_id = _content_tx(conn, ids)
    version_id = _version_pair_tx(conn, content_id)
    fb_id = _feedback_item_tx(conn, ids, content_id, version_id)
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    ev_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO content_feedback_events (id, feedback_item_id, to_state,"
            " actor_kind, seq_no) VALUES (:i, :f, 'OPEN', 'USER', 1)"
        ),
        {"i": str(ev_id), "f": str(fb_id)},
    )
    rv_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO content_reviews (id, project_id, content_item_id,"
            " subject_version_id, decision, reason, decided_by)"
            " VALUES (:i, :p, :c, :v, 'APPROVED', 'WP29 探针', :u)"
        ),
        {"i": str(rv_id), "p": ids["project"], "c": str(content_id),
         "v": str(version_id), "u": ids["user"]},
    )

    with conn.begin_nested() as sp:
        try:
            conn.execute(
                text("UPDATE content_feedback_events SET note='x' WHERE id=:i"),
                {"i": str(ev_id)},
            )
            ok, code = False, None
        except DBAPIError as exc:
            ok, code = True, sqlstate(exc)
            sp.rollback()
        emit(ok and code == IMMUTABLE_VIOLATION_SQLSTATE,
             "[T] content_feedback_events 拒绝 UPDATE", f"sqlstate={code}")

    with conn.begin_nested() as sp:
        try:
            conn.execute(
                text("UPDATE content_reviews SET reason='y' WHERE id=:i"),
                {"i": str(rv_id)},
            )
            ok, code = False, None
        except DBAPIError as exc:
            ok, code = True, sqlstate(exc)
            sp.rollback()
        emit(ok and code == IMMUTABLE_VIOLATION_SQLSTATE,
             "[T] content_reviews 拒绝 UPDATE", f"sqlstate={code}")

    with conn.begin_nested():
        conn.execute(
            text(
                "UPDATE content_feedback_items SET state='RESOLVED',"
                " reject_reason=NULL, lock_version=lock_version+1 WHERE id=:i"
            ),
            {"i": str(fb_id)},
        )
    row = conn.execute(
        text("SELECT state FROM content_feedback_items WHERE id=:i"),
        {"i": str(fb_id)},
    ).first()
    emit(row is not None and row[0] == "RESOLVED",
         "[T] content_feedback_items 允许 UPDATE（可变身份行）", f"state={row}")

    conn.rollback()

# --- C：锚点一致性（DEFERRABLE INITIALLY DEFERRED，需 SET CONSTRAINTS IMMEDIATE） ---
with engine.connect() as conn:
    ids = _people_tx(conn)
    content_a = _content_tx(conn, ids)
    content_b = _content_tx(conn, ids)
    version_a = _version_pair_tx(conn, content_a)
    version_b = _version_pair_tx(conn, content_b)
    media_a = _media_item_tx(conn, ids, content_a)
    media_b = _media_item_tx(conn, ids, content_b)

    with conn.begin_nested() as sp:
        try:
            _feedback_item_tx(conn, ids, content_a, version_a,
                               anchor_kind="MEDIA", media_item_id=media_b)
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            ok, code = False, None
        except DBAPIError as exc:
            ok, code = True, sqlstate(exc)
            sp.rollback()
        emit(ok and code == CONSISTENCY_SQLSTATE,
             "[C1] media_item_id 跨内容被拒", f"sqlstate={code}")

    with conn.begin_nested() as sp:
        _feedback_item_tx(conn, ids, content_a, version_a,
                          anchor_kind="MEDIA", media_item_id=media_a)
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        sp.rollback()
    emit(True, "[C2] media_item_id 同内容放行", "无异常")

    with conn.begin_nested() as sp:
        try:
            fb = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO content_feedback_items (id, project_id,"
                    " content_item_id, observed_version_id, anchor_kind, category,"
                    " severity, scope, author_kind, body, created_by)"
                    " VALUES (:i, :p, :c, :v, 'WHOLE', 'OTHER', 'INFO',"
                    " 'CURRENT_ONLY', 'USER', 'WP29 探针意见', :cb)"
                ),
                {"i": str(fb), "p": ids["project"], "c": str(content_a),
                 "v": str(version_b), "cb": ids["user"]},
            )
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            ok, code = False, None
        except DBAPIError as exc:
            ok, code = True, sqlstate(exc)
            sp.rollback()
        emit(ok and code == CONSISTENCY_SQLSTATE,
             "[C3] observed_version_id 跨内容被拒", f"sqlstate={code}")

    with conn.begin_nested() as sp:
        _feedback_item_tx(conn, ids, content_a, version_a)
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        sp.rollback()
    emit(True, "[C4] observed_version_id 同内容放行", "无异常")

    with conn.begin_nested() as sp:
        try:
            _feedback_item_tx(conn, ids, content_a, version_a,
                               resolved_version_id=version_b)
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            ok, code = False, None
        except DBAPIError as exc:
            ok, code = True, sqlstate(exc)
            sp.rollback()
        emit(ok and code == CONSISTENCY_SQLSTATE,
             "[C5] resolved_version_id 跨内容被拒", f"sqlstate={code}")

    with conn.begin_nested() as sp:
        _feedback_item_tx(conn, ids, content_a, version_a,
                          resolved_version_id=version_a)
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        sp.rollback()
    emit(True, "[C6] resolved_version_id 同内容放行", "无异常")

    conn.rollback()

# --- Z：零残留 ---------------------------------------------------------------
with engine.connect() as conn:
    for table in OWN_TABLES:
        n = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        emit(n == 0, f"[Z] 零残留 {table}", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM users WHERE issuer=:iss"),
        {"iss": PROBE_ISSUER},
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 users（探针）", f"{n} 行")

print(f"RESULT_EMITTED (期望 {EXPECTED_CHECKS} 条)")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def collect_wp29() -> tuple[Path, bool]:
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
        "# WP29 预览/编辑/反馈定位：3 张表 / CHECK / FK / 触发器（2 不可变 + 1 锚点"
        " 一致性）/ 库内活性验证 + 零残留",
        f"# 生成时间: {_now()}",
        "# 目标库: rednote_verify_wp29（收口时由迁移从 bootstrap 建出，非测试夹具建表）",
        "# 命令: cd services/backend && python -c '<information_schema + 试写 + 试插>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp29-feedback-reviews.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    complete = len(checks) >= EXPECTED_CHECKS
    exact = len(checks) == EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete and exact)


def main() -> int:
    path, ok = collect_wp29()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
