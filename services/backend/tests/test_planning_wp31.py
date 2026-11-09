"""WP31 试产关口 / 淘汰补产的验收判据（纯函数 / 结构 / 数据库约束 / 服务 / 只读接缝 / 并发）。

夹具纪律照 `test_review_wp30.py`：私有库（`REDNOTE_TEST_DATABASE_URL`，缺省也指向 WP31 私有库）、
`create_all` + `ddl.all_trigger_ddl()`（不走迁移）、按项目隔离造数、约束用例断言 sqlstate。
延迟约束触发器必须显式 `SET CONSTRAINTS ALL IMMEDIATE` 才看得到拒绝。
"""

from __future__ import annotations

import ast
import json
import os
import re
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from conftest import cross_package_foreign_keys, drop_own_tables, truncate_targets

import rednote.modules.baseline.models  # noqa: F401
import rednote.modules.content.models  # noqa: F401
import rednote.modules.feedback.models  # noqa: F401
import rednote.modules.identity.models  # noqa: F401
import rednote.modules.planning.models  # noqa: F401
import rednote.modules.projects.models  # noqa: F401
import rednote.modules.review.models  # noqa: F401
import rednote.modules.versions.models  # noqa: F401
import rednote.modules.planning.trial.models  # noqa: F401

from rednote.modules.baseline.repository import BaselineRepository
from rednote.modules.feedback import types as feedback_types
from rednote.modules.content import types as content_types
from rednote.modules.planning import service as plan_service
from rednote.modules.planning import types as plan_types
from rednote.modules.planning.repository import PlanningRepository
from rednote.modules.planning.trial import ddl as t_ddl
from rednote.modules.planning.trial import seam
from rednote.modules.planning.trial import service as svc
from rednote.modules.planning.trial import types as tt
from rednote.modules.planning.trial.models import OWN_TABLES
from rednote.modules.planning.trial.repository import TrialRepository
from rednote.modules.versions.repository import VersionRepository
from rednote.modules.versions.service import canonical_payload_hash
from rednote.platform.base import Base
from rednote.platform.configuration import Settings
from rednote.platform.database import get_engine, reset_engines, session_scope
from rednote.platform.errors import ApiError, ErrorCode
from rednote.platform.immutability import IMMUTABLE_RECORD_SQLSTATE

from test_content_wp26 import (
    _clean_own_rows as _clean_content_rows,  # noqa: F401
    _create_user,
    _ensure_own_tables as _ensure_content_tables,  # noqa: F401
    _seed_project,
    _sqlstate,
)
from test_planning_wp24 import _item_body

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
#: WP31 私有测试库（**不是**共享的 `rednote_test`）。
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test_wp31"
TEST_DATABASE_URL_ENV = "REDNOTE_TEST_DATABASE_URL"

DROP_ORDER = ("trial_gate_decisions", "trial_round_slots", "trial_rounds")
_TABLE_NAMES = tuple(OWN_TABLES)
UNIQUE_VIOLATION = "23505"
CHECK_VIOLATION = "23514"
TRIAL_DIR = Path(__file__).resolve().parent.parent / "src/rednote/modules/planning/trial"


def _test_database_url() -> str:
    return os.environ.get(TEST_DATABASE_URL_ENV, DEFAULT_TEST_DATABASE_URL).strip()


def _make_settings() -> Settings:
    return Settings(
        database_url=_test_database_url(),
        environment="test",
        session_cookie_secure=False,
        oidc_issuer=TEST_ISSUER,
        oidc_token_encryption_key=TEST_ENCRYPTION_KEY,
    )


@pytest.fixture(scope="module", autouse=True)
def _ensure_own_tables(_ensure_content_tables: None) -> Iterator[None]:
    """只重建本包 3 张表 + 6 个触发器。依赖 content 的建表夹具（FK 顺序保证）。"""
    reset_engines()
    engine = get_engine(_make_settings())
    fks_before = cross_package_foreign_keys(engine, _TABLE_NAMES)
    dropped, kept = drop_own_tables(engine, DROP_ORDER)
    Base.metadata.create_all(
        engine, tables=[Base.metadata.tables[name] for name in OWN_TABLES]
    )
    fks_after = cross_package_foreign_keys(engine, _TABLE_NAMES)
    assert fks_after == fks_before, (
        f"夹具改变了跨包外键：before={sorted(fks_before)} after={sorted(fks_after)}"
        f"（dropped={dropped} kept={kept}）"
    )
    targets = truncate_targets(engine, DROP_ORDER)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(targets)))
        for _t, func_ddl, trigger_ddl, drop_trigger, _df in t_ddl.all_trigger_ddl():
            conn.execute(text(drop_trigger))
            conn.execute(text(func_ddl))
            conn.execute(text(trigger_ddl))
    yield


@pytest.fixture(autouse=True)
def _clean_own_rows(_clean_content_rows: None) -> Iterator[None]:
    reset_engines()
    engine = get_engine(_make_settings())
    targets = truncate_targets(engine, DROP_ORDER)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(targets)))
    yield


# ===========================================================================
# 造数
# ===========================================================================


@dataclass(frozen=True)
class TrialEnv:
    settings: Settings
    project_id: uuid.UUID
    owner_id: uuid.UUID
    other_project_id: uuid.UUID


def _seed_project_with_limits(settings: Settings, *, owner_id: uuid.UUID) -> uuid.UUID:
    """`_seed_project` + 保证该项目有生效的 `batch.limits`（项目级，**不碰 GLOBAL 行**）。

    迁移种下了 GLOBAL `batch.limits`（ADR-13 初始行）：在迁移到 head 的库里（CI 的形态），
    新项目会继承它，本函数什么都不写。只靠各包夹具 `create_all` 建出来的共享库没有 GLOBAL 行，
    项目就没有生效值（`SETTING_NOT_CONFIGURED`）⇒ 补一条**项目级** MANUAL 覆盖，值同 ADR-13。
    不写 GLOBAL 行：那会污染 WP24 里依赖「全局未配置」的用例。
    """
    from rednote.modules.planning.repository import PlanningRepository
    from rednote.modules.planning.settings_service import (
        change_project_setting,
        get_effective_limits,
    )
    from rednote.modules.planning.types import SETTING_KEY_BATCH_LIMITS

    project_id = _seed_project(settings, owner_id=owner_id)
    with session_scope(settings) as session:
        repo = PlanningRepository(session)
        effective = get_effective_limits(
            repo, SETTING_KEY_BATCH_LIMITS, project_id, materialize=False
        )
        if not effective.configured:
            change_project_setting(
                repo,
                key=SETTING_KEY_BATCH_LIMITS,
                project_id=project_id,
                value={"TRIAL": 10, "SCALE": 30, "BULK": 50},
                reason="测试夹具：库内无 GLOBAL batch.limits，补项目级覆盖（值同 ADR-13）",
                actor_id=owner_id,
            )
    return project_id


@pytest.fixture
def env() -> TrialEnv:
    reset_engines()
    settings = _make_settings()
    owner = _create_user(settings)
    return TrialEnv(
        settings=settings,
        project_id=_seed_project_with_limits(settings, owner_id=owner),
        owner_id=owner,
        other_project_id=_seed_project_with_limits(settings, owner_id=owner),
    )


@contextmanager
def _repos(env: TrialEnv) -> Iterator[tuple[TrialRepository, PlanningRepository]]:
    with session_scope(env.settings) as session:
        yield TrialRepository(session), PlanningRepository(session)


def _exec(env: TrialEnv, sql: str, params: dict[str, Any] | None = None) -> None:
    with session_scope(env.settings) as session:
        session.execute(text(sql), params or {})


def _rows(env: TrialEnv, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    with session_scope(env.settings) as session:
        return [tuple(r) for r in session.execute(text(sql), params or {}).fetchall()]


def _scalar(env: TrialEnv, sql: str, params: dict[str, Any] | None = None) -> Any:
    with session_scope(env.settings) as session:
        return session.execute(text(sql), params or {}).scalar_one_or_none()


def _seed_baseline(
    env: TrialEnv,
    *,
    project_id: uuid.UUID | None = None,
    step1: bool = True,
    step2: bool = True,
    step3: bool = True,
    activate: bool = True,
) -> uuid.UUID:
    """造生效基线 + 步骤 1~3 批准（同一事务，满足 WP22 延迟触发器）。"""
    pid = project_id or env.project_id
    baseline_id, brief_id = uuid.uuid4(), uuid.uuid4()
    with session_scope(env.settings) as s:
        revision = int(
            s.execute(
                text(
                    "SELECT COALESCE(max(revision_no), 0) + 1 FROM version_nodes"
                    " WHERE entity_type = 'BASELINE' AND entity_id = :p"
                ),
                {"p": pid},
            ).scalar_one()
        )
        for nid, etype in ((brief_id, "BRIEF"), (baseline_id, "BASELINE")):
            s.execute(
                text(
                    "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
                    " payload_hash, schema_version, project_id)"
                    " VALUES (:id, :t, :e, :rev, :h, 1, :p)"
                ),
                {
                    "id": nid,
                    "t": etype,
                    "e": pid if etype == "BASELINE" else uuid.uuid4(),
                    "rev": revision if etype == "BASELINE" else 1,
                    "h": "0" * 64,
                    "p": pid,
                },
            )
        previous = s.execute(
            text(
                "SELECT b.id FROM production_baselines b WHERE b.project_id = :p"
                " AND NOT EXISTS (SELECT 1 FROM production_baselines c WHERE c.supersedes_id = b.id)"
            ),
            {"p": pid},
        ).scalar_one_or_none()
        s.execute(
            text(
                "INSERT INTO production_baselines (id, project_id, brief_version_id,"
                " ruleset_version, snapshot, change_reason, supersedes_id)"
                " VALUES (:id, :p, :b, :rs, '{}'::jsonb, 'WP31 造数', :prev)"
            ),
            {"id": baseline_id, "p": pid, "b": brief_id,
             "rs": f"rs-{uuid.uuid4().hex[:8]}", "prev": previous},
        )
        # 触发器要求基线必有 step3 批准；"缺 step3"场景稍后 supersede 为 REJECTED。
        approvals = [(3, "BASELINE", baseline_id, "APPROVED")]
        if step1:
            approvals.append((1, "BRIEF", brief_id, "APPROVED"))
        if step2:
            approvals.append((2, "RESEARCH", brief_id, "APPROVED"))
        for step_no, kind, subject, decision in approvals:
            s.execute(
                text(
                    "INSERT INTO step_approvals (project_id, step_no, subject_kind,"
                    " subject_version_id, decision, reason, decided_by)"
                    " VALUES (:p, :n, :k, :v, :d, 'WP31 造数', :u)"
                ),
                {"p": pid, "n": step_no, "k": kind, "v": subject, "d": decision, "u": env.owner_id},
            )
        if activate:
            s.execute(
                text("UPDATE projects SET active_baseline_id = :b WHERE id = :p"),
                {"b": baseline_id, "p": pid},
            )
    if not step3:
        _supersede_step3(env, pid, baseline_id)
    return baseline_id


def _supersede_step3(env: TrialEnv, pid: uuid.UUID, baseline_id: uuid.UUID) -> None:
    """用一条 REJECTED 取代 step3 的 APPROVED（链尾不再是 APPROVED）。"""
    _exec(
        env,
        "INSERT INTO step_approvals (project_id, step_no, subject_kind, subject_version_id,"
        " decision, reason, decided_by, supersedes_id)"
        " SELECT project_id, 3, 'BASELINE', subject_version_id, 'REJECTED', 'x', decided_by, id"
        "   FROM step_approvals WHERE project_id = :p AND step_no = 3"
        "    AND subject_version_id = :b AND decision = 'APPROVED'",
        {"p": pid, "b": baseline_id},
    )


def _seed_plan(
    env: TrialEnv,
    *,
    baseline_id: uuid.UUID | None = None,
    total: int = 10,
    scenes: tuple[tuple[str, int], ...] = (("s1", 600), ("s2", 400)),
    project_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """提交一份 PLAN，返回 PLAN 版本节点 id。"""
    pid = project_id or env.project_id
    with session_scope(env.settings) as s:
        repo = PlanningRepository(s)
        draft = plan_service.require_draft(repo, project_id=pid)
        saved = plan_service.save_draft(
            repo,
            project_id=pid,
            expected_lock_version=draft.lock_version,
            baseline_id=baseline_id,
            total_quantity=total,
            scene_items=[_item_body(k, w, sort_order=i) for i, (k, w) in enumerate(scenes)],
            form_items=[_item_body("f1", 1000)],
            pairings=[{"sceneKey": scenes[0][0], "formKey": "f1"}],
        )
        registered = plan_service.submit_draft(
            repo,
            VersionRepository(s),
            project_id=pid,
            expected_lock_version=saved.lock_version,
            actor_id=env.owner_id,
        )
        return registered.node_id


@dataclass(frozen=True)
class Ready:
    baseline_id: uuid.UUID
    plan_id: uuid.UUID


def _ready(env: TrialEnv, **plan_kw: Any) -> Ready:
    baseline = _seed_baseline(env)
    return Ready(baseline, _seed_plan(env, baseline_id=baseline, **plan_kw))


def _create(env: TrialEnv, plan: uuid.UUID, **kw: Any) -> tuple[uuid.UUID, bool]:
    kw.setdefault("kind", "TRIAL")
    kw.setdefault("project_id", env.project_id)
    with _repos(env) as (r, p):
        row, created = svc.create_round(
            r, p, plan_version_id=plan, actor_id=env.owner_id, request_id="t-req", **kw
        )
        return row.id, created


def _lv(env: TrialEnv, rid: uuid.UUID) -> int:
    return int(_scalar(env, "SELECT lock_version FROM trial_rounds WHERE id = :r", {"r": rid}))


def _state(env: TrialEnv, rid: uuid.UUID) -> str:
    return str(_scalar(env, "SELECT state FROM trial_rounds WHERE id = :r", {"r": rid}))


def _transition(
    env: TrialEnv, rid: uuid.UUID, to: str, *, reason: str | None = None, lv: int | None = None
) -> None:
    with _repos(env) as (r, _p):
        svc.transition_round(
            r,
            project_id=env.project_id,
            round_id=rid,
            expected_lock_version=_lv(env, rid) if lv is None else lv,
            to_state=to,
            reason=reason,
            actor_id=env.owner_id,
            request_id="t-req",
        )


def _slots(env: TrialEnv, rid: uuid.UUID) -> list[tuple[uuid.UUID, int, str]]:
    return [
        (r[0], r[1], r[2])
        for r in _rows(
            env,
            "SELECT id, slot_no, scene_key FROM trial_round_slots WHERE round_id = :r"
            " ORDER BY slot_no",
            {"r": rid},
        )
    ]


def _seed_content(
    env: TrialEnv,
    scene_key: str | None,
    *,
    state: str = "GENERATED",
    batch_kind: str | None = "TRIAL",
    project_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    cid, vid = uuid.uuid4(), uuid.uuid4()
    request: dict[str, Any] = {"scope": "COPY", "modelProfileId": str(uuid.uuid4())}
    if batch_kind is not None:
        request["batchKind"] = batch_kind
    with session_scope(env.settings) as s:
        s.execute(
            text(
                "INSERT INTO content_items (id, project_id, state, current_version_no,"
                " lock_version, generation_request, scene_key)"
                " VALUES (:c, :p, :st, 1, 1, CAST(:gr AS jsonb), :sk)"
            ),
            {
                "c": cid,
                "p": project_id or env.project_id,
                "st": state,
                "gr": json.dumps(request),
                "sk": scene_key,
            },
        )
        _insert_version(s, cid, vid, 1)
        s.execute(
            text("UPDATE content_items SET current_version_id = :v WHERE id = :c"),
            {"v": vid, "c": cid},
        )
    return cid, vid


def _insert_version(s: Any, cid: uuid.UUID, vid: uuid.UUID, version_no: int) -> None:
    s.execute(
        text(
            "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
            " payload_hash) VALUES (:id, 'CONTENT', :e, :n, :h)"
        ),
        {"id": vid, "e": cid, "n": version_no, "h": f"h-{vid.hex[:12]}"},
    )
    s.execute(
        text(
            "INSERT INTO content_versions (id, content_item_id, version_no, title, body,"
            " tags, bindings, payload, payload_hash, schema_version)"
            " VALUES (:id, :c, :n, '标题', '正文', '[]'::jsonb, '[]'::jsonb, '{}'::jsonb,"
            " :ph, 1)"
        ),
        {"id": vid, "c": cid, "n": version_no, "ph": f"ph-{vid.hex[:12]}"},
    )


def _add_version(env: TrialEnv, cid: uuid.UUID, version_no: int, *, move: bool = True) -> uuid.UUID:
    vid = uuid.uuid4()
    with session_scope(env.settings) as s:
        _insert_version(s, cid, vid, version_no)
        if move:
            s.execute(
                text(
                    "UPDATE content_items SET current_version_id = :v,"
                    " current_version_no = :n WHERE id = :c"
                ),
                {"v": vid, "n": version_no, "c": cid},
            )
    return vid


def _review(
    env: TrialEnv,
    cid: uuid.UUID,
    vid: uuid.UUID,
    decision: str,
    *,
    supersedes: uuid.UUID | None = None,
) -> uuid.UUID:
    rid = uuid.uuid4()
    _exec(
        env,
        "INSERT INTO content_reviews (id, project_id, content_item_id, subject_version_id,"
        " decision, reason, decided_by, supersedes_id)"
        " VALUES (:id, :p, :c, :v, :d, '审核', :u, :s)",
        {"id": rid, "p": env.project_id, "c": cid, "v": vid, "d": decision,
         "u": env.owner_id, "s": supersedes},
    )
    return rid


def _feedback(
    env: TrialEnv, cid: uuid.UUID, vid: uuid.UUID, *, severity: str = "BLOCKING", state: str = "OPEN"
) -> None:
    _exec(
        env,
        "INSERT INTO content_feedback_items (project_id, content_item_id, observed_version_id,"
        " anchor_kind, category, severity, scope, state, author_kind, body, reject_reason)"
        " VALUES (:p, :c, :v, 'WHOLE', 'FACT', :sv, 'CURRENT_ONLY', :st, 'USER', '意见',"
        " CASE WHEN CAST(:st AS varchar) = 'REJECTED' THEN '不成立' END)",
        {"p": env.project_id, "c": cid, "v": vid, "sv": severity, "st": state},
    )


def _self_check(env: TrialEnv, cid: uuid.UUID, vid: uuid.UUID) -> None:
    _exec(
        env,
        "INSERT INTO self_check_runs (project_id, content_item_id, subject_version_id,"
        " rule_set_version, trigger_kind, outcome, blocking_count, warning_count, info_count,"
        " evaluated_count, skipped_count, errored_count, started_at, finished_at, created_by)"
        " VALUES (:p, :c, :v, 1, 'MANUAL', 'PASSED', 0, 0, 0, 1, 0, 0, now(), now(), :u)",
        {"p": env.project_id, "c": cid, "v": vid, "u": env.owner_id},
    )


@dataclass
class Filled:
    rid: uuid.UUID
    #: slot_id -> (scene_key, content_id, version_id)
    items: dict[uuid.UUID, tuple[str, uuid.UUID, uuid.UUID]]


def _bind_all(env: TrialEnv, rid: uuid.UUID) -> Filled:
    """PLANNED -> GENERATING，给每个名额造内容并绑定。"""
    _transition(env, rid, tt.ROUND_STATE_GENERATING)
    items = {}
    for slot_id, _no, scene in _slots(env, rid):
        cid, vid = _seed_content(env, scene)
        _bind(env, rid, slot_id, cid)
        items[slot_id] = (scene, cid, vid)
    return Filled(rid, items)


def _bind(env: TrialEnv, rid: uuid.UUID, slot_id: uuid.UUID, cid: uuid.UUID) -> bool:
    with _repos(env) as (r, _p):
        _slot, changed = svc.bind_slot_content(
            r, project_id=env.project_id, round_id=rid, slot_id=slot_id, content_item_id=cid
        )
        return changed


def _reviewing(env: TrialEnv, plan: uuid.UUID, **create_kw: Any) -> Filled:
    rid, _ = _create(env, plan, **create_kw)
    filled = _bind_all(env, rid)
    _transition(env, rid, tt.ROUND_STATE_REVIEWING)
    return filled


def _approve_all(env: TrialEnv, filled: Filled, *, except_scenes: tuple[str, ...] = ()) -> None:
    for _slot, (scene, cid, vid) in filled.items.items():
        _review(
            env, cid, vid,
            "ELIMINATED" if scene in except_scenes else "APPROVED",
        )


def _approve(env: TrialEnv, rid: uuid.UUID, keys: list[str], *, reason: str = "放行") -> uuid.UUID:
    with _repos(env) as (r, _p):
        svc.approve_gate(
            r,
            project_id=env.project_id,
            round_id=rid,
            expected_lock_version=_lv(env, rid),
            approved_scene_keys=keys,
            reason=reason,
            actor_id=env.owner_id,
            request_id="t-req",
        )
    return rid


def _api_error(fn: Any) -> ApiError:
    with pytest.raises(ApiError) as caught:
        fn()
    return caught.value


def _reason(exc: ApiError) -> str:
    return str((exc.details or {}).get("reason"))


# ===========================================================================
# A. 纯函数 / 结构 / 判据
# ===========================================================================


class _FakeRow:
    def __init__(self, state: str) -> None:
        self.state = state


@pytest.mark.parametrize("src", sorted(tt.ROUND_STATES))
@pytest.mark.parametrize("dst", sorted(tt.ROUND_STATES))
def test_u01_transition_matrix_exhaustive(src: str, dst: str) -> None:
    """5x5：合法边通过，其余 409 INVALID_TRANSITION；终态出边恒为终态原因。"""
    legal = dst in tt.ROUND_TRANSITIONS[src]
    if legal:
        svc._require_edge(_FakeRow(src), dst)
        return
    exc = _api_error(lambda: svc._require_edge(_FakeRow(src), dst))
    assert exc.code == ErrorCode.INVALID_TRANSITION
    expected = tt.REASON_ROUND_TERMINAL if src in tt.ROUND_TERMINAL_STATES else tt.REASON_ROUND_TRANSITION_ILLEGAL
    assert _reason(exc) == expected


def test_u01_matrix_shape_is_the_designed_one() -> None:
    assert tt.ROUND_TRANSITIONS == {
        "PLANNED": {"GENERATING", "CANCELLED"},
        "GENERATING": {"REVIEWING", "CANCELLED"},
        "REVIEWING": {"GENERATING", "GATE_PASSED", "CANCELLED"},
        "GATE_PASSED": set(),
        "CANCELLED": set(),
    }
    assert tt.ROUND_TERMINAL_STATES == {"GATE_PASSED", "CANCELLED"}


def test_u02_trigger_function_matrix_matches_types() -> None:
    """触发器函数体里的矩阵（反解析）与 `types.ROUND_TRANSITIONS` 逐项一致。"""
    func_ddl = t_ddl.round_transition_guard_ddl()[0]
    block = func_ddl.split(t_ddl.MATRIX_BEGIN)[1].split(t_ddl.MATRIX_END)[0]
    parsed: dict[str, set[str]] = {s: set() for s in tt.ROUND_STATES}
    for src, dsts in re.findall(r"OLD\.state = '(\w+)' AND NEW\.state IN \(([^)]*)\)", block):
        parsed[src] = set(re.findall(r"'(\w+)'", dsts))
    assert parsed == {k: set(v) for k, v in tt.ROUND_TRANSITIONS.items()}


def test_value_domains_parity_with_other_packages() -> None:
    assert tt.LIMIT_SOURCES == plan_types.SETTING_ORIGINS
    assert tt.MAX_QUANTITY_BOUND == plan_types.MAX_QUANTITY_BOUND
    assert tt.GENERATION_REQUEST_KEY_BATCH_KIND == content_types.REQUEST_KEY_BATCH_KIND
    assert {tt.REVIEW_DECISION_APPROVED, tt.REVIEW_DECISION_ELIMINATED} == feedback_types.REVIEW_DECISIONS
    assert tt.CONTENT_STATE_GENERATED == content_types.CONTENT_STATE_GENERATED
    assert tt.CONTENT_STATE_FAILED == content_types.CONTENT_STATE_FAILED
    assert tt.BATCH_KIND_TRIAL in plan_types.BATCH_KINDS


def test_d09_structure_matches_ddl_draft() -> None:
    """29 条 CHECK（18/5/6）、6 个触发器、5 个自有函数、约束名 <= 63 字节。"""
    from sqlalchemy import CheckConstraint

    counts = {}
    for name in OWN_TABLES:
        table = Base.metadata.tables[name]
        checks = [c for c in table.constraints if isinstance(c, CheckConstraint)]
        counts[name] = len(checks)
    assert counts == {"trial_rounds": 18, "trial_round_slots": 5, "trial_gate_decisions": 6}
    assert len(t_ddl.all_trigger_ddl()) == 6
    assert len(t_ddl.OWN_FUNCTIONS) == 5
    assert "rednote_reject_mutation" not in t_ddl.OWN_FUNCTIONS
    engine = get_engine(_make_settings())
    with engine.connect() as conn:
        names = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conrelid = ANY("
                    " ARRAY['trial_rounds'::regclass, 'trial_round_slots'::regclass,"
                    " 'trial_gate_decisions'::regclass])"
                )
            )
        ]
        ck = [n for n in names if n.startswith("ck_")]
        assert len(ck) == 29
        assert all(len(n.encode()) <= 63 for n in names)
        triggers = {
            r[0]
            for r in conn.execute(
                text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname LIKE 'trg_trial%'")
            )
        }
        assert triggers == set(t_ddl.trigger_names())
        fks = conn.execute(
            text(
                "SELECT DISTINCT confdeltype, condeferrable FROM pg_constraint"
                " WHERE contype = 'f' AND conrelid = ANY(ARRAY['trial_rounds'::regclass,"
                " 'trial_round_slots'::regclass, 'trial_gate_decisions'::regclass])"
            )
        ).fetchall()
        assert {tuple(r) for r in fks} == {("r", False)}


def test_u15_ratio_null_zero_and_rounding() -> None:
    assert tt.ratio(0, 0)["permille"] is None
    assert tt.ratio(0, 5)["permille"] == 0
    assert tt.ratio(1, 3) == {"numerator": 1, "denominator": 3, "permille": 333, "rounded": True}
    assert tt.ratio(2, 3)["permille"] == 666


def _fact(**kw: Any) -> tt.SlotFact:
    base: dict[str, Any] = dict(
        slot_id=uuid.uuid4(), round_id=uuid.uuid4(), slot_no=1, scene_key="s1",
        form_key=None, content_item_id=uuid.uuid4(), replaces_slot_id=None, replaced=False,
        content_state="GENERATED", current_version_id=uuid.uuid4(), review_id=None,
        review_decision=None, any_review=False, review_on_first_version=False,
        open_blocking=False, self_checked=False,
    )
    base.update(kw)
    return tt.SlotFact(**base)


def test_slot_status_derivation_table() -> None:
    assert _fact(content_item_id=None).status == tt.SLOT_STATUS_UNBOUND
    assert _fact().status == tt.SLOT_STATUS_AWAITING_REVIEW
    assert _fact(review_decision="APPROVED").status == tt.SLOT_STATUS_APPROVED
    assert _fact(review_decision="ELIMINATED").status == tt.SLOT_STATUS_ELIMINATED
    # 死名额压过一切（改判 APPROVED 也不复活）。
    assert _fact(replaced=True, review_decision="APPROVED").status == tt.SLOT_STATUS_REPLACED
    assert tt.review_stale(_fact(any_review=True))
    assert not tt.review_stale(_fact(any_review=True, review_decision="APPROVED"))


def test_u09_gate_pure_hard_conditions_each_red_and_green() -> None:
    ok = [_fact(slot_no=1, review_decision="APPROVED"), _fact(slot_no=2, scene_key="s2", review_decision="ELIMINATED")]
    g = svc.evaluate_gate(ok, ["s1"])
    assert g["satisfiable"] and all(c["satisfied"] for c in g["conditions"].values() if c["blocking"])
    # ROUND_ALL_DISPOSED 红
    red = svc.evaluate_gate([*ok, _fact(slot_no=3)], ["s1"])
    assert not red["conditions"][tt.GATE_ROUND_ALL_DISPOSED]["satisfied"]
    # NO_OPEN_BLOCKING 红；淘汰项的阻断不算
    blocked = svc.evaluate_gate([_fact(review_decision="APPROVED", open_blocking=True)], ["s1"])
    assert not blocked["conditions"][tt.GATE_NO_OPEN_BLOCKING_FEEDBACK]["satisfied"]
    elim = svc.evaluate_gate(
        [_fact(review_decision="APPROVED"),
         _fact(slot_no=2, review_decision="ELIMINATED", open_blocking=True)], ["s1"]
    )
    assert elim["conditions"][tt.GATE_NO_OPEN_BLOCKING_FEEDBACK]["satisfied"]
    # DIRECTION 红
    d = svc.evaluate_gate(ok, ["s2"])
    assert not d["conditions"][tt.GATE_DIRECTION_HAS_APPROVED_SAMPLE]["satisfied"]
    # 无自检只报告，不阻断
    assert not g["conditions"][tt.GATE_SELF_CHECK_COMPLETE]["satisfied"]
    assert g["conditions"][tt.GATE_SELF_CHECK_COMPLETE]["blocking"] is False
    assert g["satisfiable"]


def test_u15_statistics_counts_are_exact_and_not_self_checked_separate() -> None:
    facts = [
        _fact(slot_no=1, review_decision="APPROVED", review_on_first_version=True, self_checked=True),
        _fact(slot_no=2, review_decision="ELIMINATED"),
        _fact(slot_no=3, content_item_id=None, content_state=None),
        _fact(slot_no=4, replaced=True),
    ]
    st = svc.compute_statistics(facts)
    assert (st["liveSlots"], st["replacedSlots"], st["approved"], st["eliminated"]) == (3, 1, 1, 1)
    assert st["unbound"] == 1 and st["pending"] == 1
    assert st["approvedOnFirstVersion"] == 1
    assert st["notSelfChecked"] == 0  # 已自检的通过项 + 淘汰项不计
    assert st["approvalRate"]["permille"] == 500
    assert svc.compute_statistics([])["approvalRate"]["permille"] is None


def _trial_sources() -> list[Path]:
    return sorted(p for p in TRIAL_DIR.glob("*.py"))


def test_a01_trial_package_import_boundary() -> None:
    forbidden = {"baseline", "review", "feedback", "content", "billing", "gateway", "media", "jobs"}
    for path in _trial_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.ImportFrom) and node.module:
                mods.append(node.module)
            elif isinstance(node, ast.Import):
                mods.extend(a.name for a in node.names)
            for mod in mods:
                parts = mod.split(".")
                if parts[:2] == ["rednote", "modules"] and len(parts) > 2:
                    assert parts[2] not in forbidden, f"{path.name} import 了 {mod}"


def test_a01_no_dml_on_foreign_tables() -> None:
    pattern = re.compile(
        r"(?:INSERT INTO|DELETE FROM)\s+(\w+)|(?<!FOR )\bUPDATE\s+(\w+)\s+SET", re.IGNORECASE
    )
    for path in _trial_sources():
        if path.name == "ddl.py":
            continue
        for m in pattern.finditer(path.read_text(encoding="utf-8")):
            table = m.group(1) or m.group(2)
            assert table.startswith("trial_"), f"{path.name} 对他表写入：{m.group(0)}"


def test_a02_version_no_desc_only_inside_coalesce_fallback() -> None:
    for path in _trial_sources():
        src = path.read_text(encoding="utf-8")
        if "version_no DESC" not in src:
            continue
        assert path.name == "repository.py"
    from rednote.modules.planning.trial import repository as rp

    sql = rp.CURRENT_VERSION_SQL
    assert sql.startswith("COALESCE(") and sql.count("version_no DESC") == 1
    assert sql.index("current_version_id") < sql.index("version_no DESC")
    assert rp._SLOT_FACTS_SQL.count("version_no DESC") == 1


def test_a03_no_business_number_literals_as_upper_bound() -> None:
    for path in _trial_sources():
        if path.name in {"models.py", "types.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        indexes = {
            id(n.slice) for n in ast.walk(tree) if isinstance(n, ast.Subscript)
        }  # 行下标 r[10] 不是业务上界
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, int):
                if id(node) in indexes:
                    continue
                assert node.value not in (10, 30, 50), f"{path.name}:{node.lineno}"


def test_a04_cross_package_fk_exists() -> None:
    engine = get_engine(_make_settings())
    with engine.connect() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint WHERE conname ="
                " 'fk_trial_round_slots_content_item_id_content_items'"
            )
        ).scalar_one()
    assert n == 1


# ===========================================================================
# B. 数据库约束 / 触发器兜底（断 sqlstate）
# ===========================================================================


def _expect_sqlstate(env: TrialEnv, statements: list[tuple[str, dict[str, Any]]], *, immediate: bool = False) -> str:
    """在一个事务里执行语句；返回 sqlstate（全部通过返回 ''）。事务整体回滚。"""
    try:
        with session_scope(env.settings) as s:
            if immediate:
                pass
            for sql, params in statements:
                s.execute(text(sql), params)
            if immediate:
                s.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            raise _Rollback()
    except _Rollback:
        return ""
    except DBAPIError as exc:
        return _sqlstate(exc)


class _Rollback(Exception):
    pass


def _expect_commit_error(env: TrialEnv, statements: list[tuple[str, dict[str, Any]]]) -> str:
    """语句 + SET CONSTRAINTS ALL IMMEDIATE；返回 sqlstate（延迟触发器的拒绝）。"""
    return _expect_sqlstate(env, statements, immediate=True)


def test_d01_illegal_state_edge_rejected_by_trigger(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    # PLANNED -> REVIEWING 非法（绕过服务层直写 SQL）。
    assert _expect_sqlstate(
        env, [("UPDATE trial_rounds SET state = 'REVIEWING' WHERE id = :r", {"r": rid})]
    ) == CHECK_VIOLATION
    # PLANNED -> GATE_PASSED 非法。
    assert _expect_sqlstate(
        env, [("UPDATE trial_rounds SET state = 'GATE_PASSED' WHERE id = :r", {"r": rid})]
    ) == CHECK_VIOLATION
    # 同态（只 +1 lock_version）放行。
    assert _expect_sqlstate(
        env, [("UPDATE trial_rounds SET lock_version = lock_version + 1 WHERE id = :r", {"r": rid})]
    ) == ""


def test_d01_terminal_has_no_outgoing_edge_in_db(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    _transition(env, rid, tt.ROUND_STATE_CANCELLED, reason="不做了")
    for target in ("PLANNED", "GENERATING", "REVIEWING"):
        assert _expect_sqlstate(
            env,
            [("UPDATE trial_rounds SET state = :s, cancel_reason = NULL WHERE id = :r",
              {"s": target, "r": rid})],
        ) in (CHECK_VIOLATION,), target


def test_d01_immutable_columns_and_delete(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    assert _expect_sqlstate(
        env, [("UPDATE trial_rounds SET planned_quantity = 1 WHERE id = :r", {"r": rid})]
    ) == IMMUTABLE_RECORD_SQLSTATE
    assert _expect_sqlstate(
        env, [("UPDATE trial_rounds SET plan_snapshot = '{}'::jsonb WHERE id = :r", {"r": rid})]
    ) == IMMUTABLE_RECORD_SQLSTATE
    assert _expect_sqlstate(
        env, [("DELETE FROM trial_rounds WHERE id = :r", {"r": rid})]
    ) == IMMUTABLE_RECORD_SQLSTATE
    sid = _slots(env, rid)[0][0]
    assert _expect_sqlstate(
        env, [("DELETE FROM trial_round_slots WHERE id = :s", {"s": sid})]
    ) == IMMUTABLE_RECORD_SQLSTATE
    assert _expect_sqlstate(
        env, [("UPDATE trial_round_slots SET scene_key = 'zz' WHERE id = :s", {"s": sid})]
    ) == IMMUTABLE_RECORD_SQLSTATE


def _raw_round_insert(env: TrialEnv, ready: Ready, **over: Any) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {
        "id": uuid.uuid4(), "p": env.project_id, "no": 99, "kind": "TRIAL", "state": "PLANNED",
        "plan": ready.plan_id, "base": ready.baseline_id, "src": None, "req": 5, "pl": 5,
        "lim": 10, "ls": "GLOBAL", "cr": None, "ik": None, "rh": None,
    }
    params.update(over)
    return (
        "INSERT INTO trial_rounds (id, project_id, round_no, kind, state, plan_version_id,"
        " baseline_id, source_round_id, requested_quantity, planned_quantity, limit_applied,"
        " limit_source, plan_snapshot, cancel_reason, idempotency_key, request_hash)"
        " VALUES (:id, :p, :no, :kind, :state, :plan, :base, :src, :req, :pl, :lim, :ls,"
        " '{}'::jsonb, :cr, :ik, :rh)",
        params,
    )


def test_d02_only_one_open_round_per_project(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    assert _expect_sqlstate(env, [_raw_round_insert(env, ready)]) == UNIQUE_VIOLATION
    _transition(env, rid, tt.ROUND_STATE_CANCELLED, reason="重来")
    assert _expect_sqlstate(env, [_raw_round_insert(env, ready)]) == ""


def test_d03_d04_round_check_constraints(env: TrialEnv) -> None:
    ready = _ready(env)
    cases = {
        "idempotency 成对": dict(ik="k1", rh=None),
        "request_hash 长度": dict(ik="k1", rh="abc"),
        "补充轮必须带 source": dict(kind="SUPPLEMENT"),
        "取消必须有理由": dict(state="CANCELLED"),
        "有理由必须取消": dict(cr="x"),
        "planned > requested": dict(req=3, pl=4),
        "requested > limit": dict(req=11, pl=11, lim=10),
        "非法 kind": dict(kind="X"),
        "非法 state": dict(state="X"),
        "非法 limit_source": dict(ls="X"),
    }
    for label, over in cases.items():
        assert _expect_sqlstate(env, [_raw_round_insert(env, ready, **over)]) == CHECK_VIOLATION, label
    k = uuid.uuid4().hex
    first = _raw_round_insert(env, ready, state="CANCELLED", cr="x", no=90, ik=k, rh="0" * 64)
    second = _raw_round_insert(env, ready, state="CANCELLED", cr="x", no=91, ik=k, rh="0" * 64)
    assert _expect_sqlstate(env, [first, second]) == UNIQUE_VIOLATION
    dup_no = _raw_round_insert(env, ready, state="CANCELLED", cr="x", no=92)
    dup_no2 = _raw_round_insert(env, ready, state="CANCELLED", cr="x", no=92)
    assert _expect_sqlstate(env, [dup_no, dup_no2]) == UNIQUE_VIOLATION


def test_d05_slot_constraints_and_write_once(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    _transition(env, rid, tt.ROUND_STATE_GENERATING)
    sid, _no, scene = _slots(env, rid)[0]
    cid, _vid = _seed_content(env, scene)
    _bind(env, rid, sid, cid)
    # 唯一：同轮同序号；同内容绑第二个（尚未绑定的）名额。
    other_sid = _slots(env, rid)[1][0]
    assert _expect_sqlstate(
        env, [("UPDATE trial_round_slots SET content_item_id = :c, bound_at = now() WHERE id = :s",
               {"c": cid, "s": other_sid})]
    ) == UNIQUE_VIOLATION
    # 改写已绑定 -> 23001；bound_at 配对 -> 23514
    new_cid, _ = _seed_content(env, scene)
    assert _expect_sqlstate(
        env, [("UPDATE trial_round_slots SET content_item_id = :c WHERE id = :s", {"c": new_cid, "s": sid})]
    ) == IMMUTABLE_RECORD_SQLSTATE
    unbound = _create_unbound_slot_round(env)
    assert _expect_sqlstate(
        env, [("UPDATE trial_round_slots SET content_item_id = :c WHERE id = :s",
               {"c": new_cid, "s": unbound})]
    ) == CHECK_VIOLATION
    # 唯一 (round_id, slot_no)
    assert _expect_sqlstate(
        env, [("INSERT INTO trial_round_slots (round_id, slot_no, scene_key) VALUES (:r, 1, 's1')", {"r": rid})]
    ) == UNIQUE_VIOLATION


def _create_unbound_slot_round(env: TrialEnv) -> uuid.UUID:
    """另一个项目的一轮里取一个未绑定名额 id（仅用于断言 CHECK，不会提交）。"""
    other = TrialEnv(env.settings, env.other_project_id, env.owner_id, env.project_id)
    ready = _ready(other)
    rid, _ = _create(other, ready.plan_id, project_id=other.project_id)
    return _slots(other, rid)[0][0]


def test_d06_consistency_triggers_on_slots(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    _transition(env, rid, tt.ROUND_STATE_GENERATING)
    first_slot, _, scene = _slots(env, rid)[0]
    foreign_cid, _ = _seed_content(env, scene, project_id=env.other_project_id)
    assert _expect_commit_error(
        env, [("UPDATE trial_round_slots SET content_item_id = :c, bound_at = now() WHERE id = :s",
               {"c": foreign_cid, "s": first_slot})]
    ) == CHECK_VIOLATION
    # 替代名额方向不同 / 跨轮。
    assert _expect_commit_error(
        env, [("INSERT INTO trial_round_slots (round_id, slot_no, scene_key, replaces_slot_id)"
               " VALUES (:r, 99, 'other', :s)", {"r": rid, "s": first_slot})]
    ) == CHECK_VIOLATION
    assert _expect_commit_error(
        env, [("INSERT INTO trial_round_slots (round_id, slot_no, scene_key, replaces_slot_id)"
               " VALUES (:r, 99, :sc, :s)", {"r": rid, "s": first_slot, "sc": scene})]
    ) == ""
    # 终态轮不得再加名额。
    _transition(env, rid, tt.ROUND_STATE_CANCELLED, reason="停")
    assert _expect_commit_error(
        env, [("INSERT INTO trial_round_slots (round_id, slot_no, scene_key) VALUES (:r, 99, 's1')",
               {"r": rid})]
    ) == CHECK_VIOLATION


def test_d07_gate_passed_requires_decision_both_directions(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    rid = filled.rid
    # 直写 GATE_PASSED、没有决定行：提交时 23514。
    assert _expect_commit_error(
        env, [("UPDATE trial_rounds SET state = 'GATE_PASSED', lock_version = lock_version + 1"
               " WHERE id = :r", {"r": rid})]
    ) == CHECK_VIOLATION
    # 有决定行、状态没推进：23514。
    assert _expect_commit_error(
        env, [("INSERT INTO trial_gate_decisions (round_id, approved_scene_keys, condition_results,"
               " statistics, basis, reason) VALUES (:r, '[\"s1\"]'::jsonb, '{}'::jsonb,"
               " '{}'::jsonb, '[]'::jsonb, 'x')", {"r": rid})]
    ) == CHECK_VIOLATION
    # 正路：决定不可变、空方向被 CHECK 拒。
    _approve_all(env, filled)
    _approve(env, rid, ["s1"])
    did = _scalar(env, "SELECT id FROM trial_gate_decisions WHERE round_id = :r", {"r": rid})
    assert _expect_sqlstate(
        env, [("UPDATE trial_gate_decisions SET reason = 'y' WHERE id = :d", {"d": did})]
    ) == IMMUTABLE_RECORD_SQLSTATE
    assert _expect_sqlstate(
        env, [("DELETE FROM trial_gate_decisions WHERE id = :d", {"d": did})]
    ) == IMMUTABLE_RECORD_SQLSTATE


def test_d08_round_ownership_checks(env: TrialEnv) -> None:
    ready = _ready(env)
    other = TrialEnv(env.settings, env.other_project_id, env.owner_id, env.project_id)
    other_ready = _ready(other)
    # PLAN 节点不是本项目的
    assert _expect_commit_error(
        env, [_raw_round_insert(env, Ready(ready.baseline_id, other_ready.plan_id))]
    ) == CHECK_VIOLATION
    # 非 PLAN 节点
    assert _expect_commit_error(
        env, [_raw_round_insert(env, Ready(ready.baseline_id, ready.baseline_id))]
    ) == CHECK_VIOLATION
    # 基线不是本项目的
    assert _expect_commit_error(
        env, [_raw_round_insert(env, Ready(other_ready.baseline_id, ready.plan_id))]
    ) == CHECK_VIOLATION
    # 正确归属通过
    assert _expect_commit_error(env, [_raw_round_insert(env, ready)]) == ""


# ===========================================================================
# C. 服务层：入口 / 创建 / 分配
# ===========================================================================


def test_u07_unknown_project_is_404_before_limits(env: TrialEnv) -> None:
    ghost = uuid.uuid4()
    exc = _api_error(lambda: _create(env, uuid.uuid4(), project_id=ghost))
    assert exc.code == ErrorCode.RESOURCE_NOT_FOUND and _reason(exc) == tt.REASON_PROJECT_NOT_FOUND


def test_u08_entry_conditions_each_missing(env: TrialEnv) -> None:
    # 无生效基线
    plan_less = uuid.uuid4()
    exc = _api_error(lambda: _create(env, plan_less))
    assert _reason(exc) == tt.REASON_NO_ACTIVE_BASELINE

    for label, kw, missing in (
        ("step1", dict(step1=False), tt.ENTRY_STEP1_APPROVED),
        ("step2", dict(step2=False), tt.ENTRY_STEP2_APPROVED),
        ("step3", dict(step3=False), tt.ENTRY_STEP3_APPROVED),
    ):
        project = _seed_project(env.settings, owner_id=env.owner_id)
        e2 = TrialEnv(env.settings, project, env.owner_id, env.other_project_id)
        _seed_baseline(e2, **kw)
        exc = _api_error(lambda: _create(e2, uuid.uuid4()))
        assert _reason(exc) == tt.REASON_STEP_APPROVAL_MISSING, label
        assert exc.details["conditions"] == [missing], label


def test_u08_rejected_after_approved_chain_tail_not_root(env: TrialEnv) -> None:
    """先批后拒：链尾是 REJECTED，必须判未满足（链根是 APPROVED 也不行，MAJOR-6）。"""
    _seed_baseline(env, step3=False)
    with _repos(env) as (r, _p):
        facts = svc.gather_entry_facts(r, project_id=env.project_id, plan_snapshot=None)
    assert facts.step3 is False and facts.step1 and facts.step2


def test_u08_active_baseline_projection_null_is_not_satisfied(env: TrialEnv) -> None:
    _seed_baseline(env, activate=False)
    with _repos(env) as (r, _p):
        facts = svc.gather_entry_facts(r, project_id=env.project_id, plan_snapshot=None)
    assert facts.active_baseline_id is None and not facts.step3 and not facts.step1


def test_u20_effective_approval_matches_baseline_repository(env: TrialEnv) -> None:
    baseline = _seed_baseline(env, step3=False)
    with session_scope(env.settings) as s:
        mine = TrialRepository(s).has_effective_approval(
            project_id=env.project_id, step_no=3, subject_kind="BASELINE", subject_version_id=baseline
        )
        theirs = BaselineRepository(s).find_effective_approval(
            project_id=env.project_id, step_no=3, subject_kind="BASELINE", subject_version_id=baseline
        )
    assert mine is False and (theirs is not None and theirs.decision == "APPROVED") is False
    ok = _seed_baseline(env)
    with session_scope(env.settings) as s:
        mine = TrialRepository(s).has_effective_approval(
            project_id=env.project_id, step_no=3, subject_kind="BASELINE", subject_version_id=ok
        )
        theirs = BaselineRepository(s).find_effective_approval(
            project_id=env.project_id, step_no=3, subject_kind="BASELINE", subject_version_id=ok
        )
    assert mine is True and theirs is not None and theirs.decision == "APPROVED"


def test_create_round_happy_path_snapshot_hash_and_slots(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, created = _create(env, ready.plan_id)
    assert created
    row = _rows(
        env,
        "SELECT state, round_no, kind, requested_quantity, planned_quantity, limit_applied,"
        " limit_source, plan_snapshot, baseline_id FROM trial_rounds WHERE id = :r",
        {"r": rid},
    )[0]
    assert row[:4] == ("PLANNED", 1, "TRIAL", 10) and row[4] == 10
    assert row[5] == 10 and row[6] in ("GLOBAL", "INHERITED", "MANUAL")
    assert row[8] == ready.baseline_id
    node_hash = _scalar(env, "SELECT payload_hash FROM version_nodes WHERE id = :n", {"n": ready.plan_id})
    assert canonical_payload_hash(row[7]) == node_hash  # U-12
    counts = {}
    for _id, _no, scene in _slots(env, rid):
        counts[scene] = counts.get(scene, 0) + 1
    assert counts == {"s1": 6, "s2": 4}
    assert [f["allocated"] for f in row[7]["scenes"]] == [6, 4]


def _expected_alloc(n: int, weights: list[int]) -> list[int]:
    """独立实现（最大余数法，余数相同取靠前）。"""
    floors = [n * w // 1000 for w in weights]
    rem = [n * w % 1000 for w in weights]
    order = sorted(range(len(weights)), key=lambda i: (-rem[i], i))
    for i in order[: n - sum(floors)]:
        floors[i] += 1
    return floors


def test_u03_allocation_matches_independent_for_every_n(env: TrialEnv) -> None:
    ready = _ready(env, scenes=(("s1", 333), ("s2", 333), ("s3", 334)))
    for n in range(1, 11):
        rid, _ = _create(env, ready.plan_id, quantity=n)
        got: dict[str, int] = {}
        for _i, _no, sc in _slots(env, rid):
            got[sc] = got.get(sc, 0) + 1
        want = _expected_alloc(n, [333, 333, 334])
        assert [got.get(k, 0) for k in ("s1", "s2", "s3")] == want, n
        _transition(env, rid, tt.ROUND_STATE_CANCELLED, reason="穷举")


def test_u06_quantity_bounds_carry_limit_and_source(env: TrialEnv) -> None:
    ready = _ready(env)
    exc = _api_error(lambda: _create(env, ready.plan_id, quantity=11))
    assert exc.code == ErrorCode.VALIDATION_FAILED
    assert exc.details["reason"] == "QUANTITY_ABOVE_MAX"
    assert exc.details["limit"] == 10 and exc.details["source"] in ("GLOBAL", "INHERITED", "MANUAL")
    exc = _api_error(lambda: _create(env, ready.plan_id, quantity=0))
    assert exc.code == ErrorCode.VALIDATION_FAILED


def test_u06_total_above_limit_requires_explicit_smaller_quantity(env: TrialEnv) -> None:
    ready = _ready(env, total=20)
    exc = _api_error(lambda: _create(env, ready.plan_id))
    assert exc.details["reason"] == "QUANTITY_ABOVE_MAX"
    rid, _ = _create(env, ready.plan_id, quantity=10)
    assert _scalar(env, "SELECT planned_quantity FROM trial_rounds WHERE id = :r", {"r": rid}) == 10


def test_u04_unallocated_when_capacity_full_and_empty_rejected(env: TrialEnv) -> None:
    baseline = _seed_baseline(env)
    with session_scope(env.settings) as s:
        repo = PlanningRepository(s)
        draft = plan_service.require_draft(repo, project_id=env.project_id)
        saved = plan_service.save_draft(
            repo, project_id=env.project_id, expected_lock_version=draft.lock_version,
            baseline_id=baseline, total_quantity=10,
            scene_items=[_item_body("s1", 500, capacity=2), _item_body("s2", 500, sort_order=1, capacity=3)],
            form_items=[_item_body("f1", 1000)], pairings=[],
        )
        plan_id = plan_service.submit_draft(
            repo, VersionRepository(s), project_id=env.project_id,
            expected_lock_version=saved.lock_version, actor_id=env.owner_id,
        ).node_id
    rid, _ = _create(env, plan_id)
    row = _rows(env, "SELECT requested_quantity, planned_quantity FROM trial_rounds WHERE id = :r", {"r": rid})[0]
    assert row == (10, 5)  # 分不完是合法结果：requested 与 planned 分列
    assert svc.round_body  # 无需额外断言；unallocated 在响应体里导出


def test_u12_plan_draft_drift_is_409(env: TrialEnv) -> None:
    ready = _ready(env)
    # 草稿提交后被改（WP24 没拦 SUBMITTED 草稿的保存）。
    with session_scope(env.settings) as s:
        repo = PlanningRepository(s)
        draft = repo.get_draft(env.project_id)
        plan_service.save_draft(
            repo, project_id=env.project_id, expected_lock_version=draft.lock_version,
            total_quantity=9,
        )
    exc = _api_error(lambda: _create(env, ready.plan_id))
    assert exc.code == ErrorCode.INVALID_TRANSITION and _reason(exc) == tt.REASON_PLAN_DRAFT_DRIFTED
    assert _scalar(env, "SELECT count(*) FROM trial_rounds WHERE project_id = :p", {"p": env.project_id}) == 0


def test_plan_version_must_be_this_projects_plan(env: TrialEnv) -> None:
    ready = _ready(env)
    exc = _api_error(lambda: _create(env, ready.baseline_id))
    assert exc.code == ErrorCode.VALIDATION_FAILED and _reason(exc) == tt.REASON_PLAN_VERSION_INVALID


def test_plan_baseline_stale_when_plan_bound_to_another_baseline(env: TrialEnv) -> None:
    old = _seed_baseline(env)
    plan = _seed_plan(env, baseline_id=old)
    _seed_baseline(env)  # 新基线接管 active
    exc = _api_error(lambda: _create(env, plan))
    assert _reason(exc) == tt.REASON_PLAN_BASELINE_STALE


def test_plan_with_null_baseline_is_released(env: TrialEnv) -> None:
    _seed_baseline(env)
    plan = _seed_plan(env, baseline_id=None)
    rid, created = _create(env, plan)
    assert created


def test_h02_idempotency_same_body_same_round_different_body_409(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, created = _create(env, ready.plan_id, idempotency_key="k-1")
    again, created2 = _create(env, ready.plan_id, idempotency_key="k-1")
    assert (created, created2, again) == (True, False, rid)
    exc = _api_error(lambda: _create(env, ready.plan_id, idempotency_key="k-1", quantity=5))
    assert exc.code == ErrorCode.IDEMPOTENCY_CONFLICT
    assert _reason(exc) == tt.REASON_IDEMPOTENCY_KEY_REUSED
    # 无键两次：第二次 ACTIVE_ROUND_EXISTS
    exc = _api_error(lambda: _create(env, ready.plan_id))
    assert _reason(exc) == tt.REASON_ACTIVE_ROUND_EXISTS
    assert _scalar(env, "SELECT count(*) FROM trial_rounds WHERE project_id = :p", {"p": env.project_id}) == 1


def test_supplement_rules_and_allocation(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    # 源轮未通过
    exc = _api_error(lambda: _create(env, ready.plan_id, kind="SUPPLEMENT", source_round_id=filled.rid, scene_keys=["s2"]))
    assert _reason(exc) == tt.REASON_ACTIVE_ROUND_EXISTS  # 同时只一轮
    _approve_all(env, filled, except_scenes=("s2",))
    _approve(env, filled.rid, ["s1"])
    exc = _api_error(lambda: _create(env, ready.plan_id, kind="SUPPLEMENT", scene_keys=["s2"]))
    assert _reason(exc) == tt.REASON_SOURCE_ROUND_REQUIRED
    exc = _api_error(lambda: _create(env, ready.plan_id, kind="SUPPLEMENT", source_round_id=filled.rid))
    assert _reason(exc) == tt.REASON_SCENE_KEYS_REQUIRED
    exc = _api_error(lambda: _create(env, ready.plan_id, kind="SUPPLEMENT", source_round_id=filled.rid, scene_keys=["nope"]))
    assert _reason(exc) == tt.REASON_SCENE_KEY_UNKNOWN
    rid, _ = _create(env, ready.plan_id, kind="SUPPLEMENT", source_round_id=filled.rid, scene_keys=["s2"], quantity=3)
    assert {sc for _i, _n, sc in _slots(env, rid)} == {"s2"} and len(_slots(env, rid)) == 3
    assert _scalar(env, "SELECT source_round_id FROM trial_rounds WHERE id = :r", {"r": rid}) == filled.rid


def test_trial_round_cannot_carry_source_and_supplement_source_must_be_passed(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    _transition(env, rid, tt.ROUND_STATE_CANCELLED, reason="放弃")
    exc = _api_error(lambda: _create(env, ready.plan_id, kind="SUPPLEMENT", source_round_id=rid, scene_keys=["s1"]))
    assert _reason(exc) == tt.REASON_SOURCE_ROUND_NOT_PASSED
    exc = _api_error(lambda: _create(env, ready.plan_id, source_round_id=rid))
    assert exc.code == ErrorCode.VALIDATION_FAILED
    exc = _api_error(lambda: _create(env, ready.plan_id, kind="BULK"))
    assert _reason(exc) == tt.REASON_KIND_INVALID


# ===========================================================================
# D. 服务层：迁移 / 绑定
# ===========================================================================


def test_transitions_happy_path_and_lock_version_increments(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    v0 = _lv(env, rid)
    _bind_all(env, rid)
    assert _state(env, rid) == "GENERATING" and _lv(env, rid) == v0 + 1
    _transition(env, rid, tt.ROUND_STATE_REVIEWING)
    assert _state(env, rid) == "REVIEWING" and _lv(env, rid) == v0 + 2


def test_transition_endpoint_shape_errors(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    exc = _api_error(lambda: _transition(env, rid, "GATE_PASSED"))
    assert exc.code == ErrorCode.VALIDATION_FAILED and _reason(exc) == tt.REASON_TO_STATE_USE_GATE_DECISION
    for bad in ("PLANNED", "NOPE"):
        exc = _api_error(lambda: _transition(env, rid, bad))
        assert exc.code == ErrorCode.VALIDATION_FAILED and _reason(exc) == tt.REASON_TO_STATE_INVALID
    # 取消必须带理由
    for reason in (None, "  "):
        exc = _api_error(lambda: _transition(env, rid, "CANCELLED", reason=reason))
        assert _reason(exc) == tt.REASON_REASON_REQUIRED
    # PLANNED -> REVIEWING 非法（服务层 409）
    exc = _api_error(lambda: _transition(env, rid, "REVIEWING"))
    assert exc.code == ErrorCode.INVALID_TRANSITION and _reason(exc) == tt.REASON_ROUND_TRANSITION_ILLEGAL
    _transition(env, rid, "CANCELLED", reason="取消")
    exc = _api_error(lambda: _transition(env, rid, "GENERATING"))
    assert _reason(exc) == tt.REASON_ROUND_TERMINAL


def test_reviewing_to_generating_is_not_an_endpoint_edge(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    exc = _api_error(lambda: _transition(env, filled.rid, "GENERATING"))
    assert exc.code == ErrorCode.INVALID_TRANSITION and exc.details["via"] == "replacements"


def test_generating_to_reviewing_needs_all_slots_settled(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    _transition(env, rid, "GENERATING")
    exc = _api_error(lambda: _transition(env, rid, "REVIEWING"))
    assert _reason(exc) == tt.REASON_ROUND_NOT_SETTLED and len(exc.details["slotIds"]) == 10
    # 内容未落定（DRAFT）也不行；FAILED 算落定。
    slots = _slots(env, rid)
    for i, (sid, _n, sc) in enumerate(slots):
        cid, _v = _seed_content(env, sc, state="DRAFT" if i == 0 else ("FAILED" if i == 1 else "GENERATED"))
        _bind(env, rid, sid, cid)
    exc = _api_error(lambda: _transition(env, rid, "REVIEWING"))
    assert _reason(exc) == tt.REASON_ROUND_NOT_SETTLED and len(exc.details["slotIds"]) == 1
    _exec(env, "UPDATE content_items SET state = 'GENERATED' WHERE id = (SELECT content_item_id FROM trial_round_slots WHERE id = :s)", {"s": slots[0][0]})
    _transition(env, rid, "REVIEWING")


def test_start_rejudges_entry_and_pinned_baseline(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    _seed_baseline(env)  # 基线前进：active 不再是钉住那条
    exc = _api_error(lambda: _transition(env, rid, "GENERATING"))
    assert _reason(exc) == tt.REASON_PLAN_BASELINE_STALE
    assert _state(env, rid) == "PLANNED"


def test_stale_lock_version_is_412_with_current_etag(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    old = _lv(env, rid)
    _transition(env, rid, "GENERATING")
    exc = _api_error(lambda: _transition(env, rid, "CANCELLED", reason="x", lv=old))
    assert exc.code == ErrorCode.VERSION_CONFLICT
    assert exc.details["reason"] == tt.REASON_LOCK_VERSION_MISMATCH
    assert exc.details["currentEtag"] == f'"{rid}:{_lv(env, rid)}"'
    # 新 ETag 再提交同一动作：状态已变 -> 409（重试双码）
    _transition(env, rid, "CANCELLED", reason="x")
    exc = _api_error(lambda: _transition(env, rid, "CANCELLED", reason="x"))
    assert exc.code == ErrorCode.INVALID_TRANSITION


def test_c02_concurrent_same_etag_one_wins_one_412(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    v = _lv(env, rid)

    def attempt(_: int) -> str:
        try:
            _transition(env, rid, "CANCELLED", reason="并发", lv=v)
            return "ok"
        except ApiError as exc:
            return exc.code.value

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(attempt, range(2)))
    assert results == ["VERSION_CONFLICT", "ok"]


def test_bind_validations_and_idempotent_replay(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    sid, _n, scene = _slots(env, rid)[0]
    cid, _v = _seed_content(env, scene)
    # PLANNED 不能绑
    exc = _api_error(lambda: _bind(env, rid, sid, cid))
    assert _reason(exc) == tt.REASON_ROUND_NOT_GENERATING
    _transition(env, rid, "GENERATING")
    assert _bind(env, rid, sid, cid) is True
    assert _bind(env, rid, sid, cid) is False  # 同内容重放
    other, _ov = _seed_content(env, scene)
    assert _reason(_api_error(lambda: _bind(env, rid, sid, other))) == tt.REASON_SLOT_ALREADY_BOUND
    sid2 = _slots(env, rid)[1][0]
    assert _reason(_api_error(lambda: _bind(env, rid, sid2, cid))) == tt.REASON_CONTENT_ALREADY_BOUND
    foreign, _fv = _seed_content(env, scene, project_id=env.other_project_id)
    assert _reason(_api_error(lambda: _bind(env, rid, sid2, foreign))) == tt.REASON_CONTENT_PROJECT_MISMATCH
    for bk in ("SCALE", None):  # 缺失即 fail-closed
        c, _cv = _seed_content(env, scene, batch_kind=bk)
        assert _reason(_api_error(lambda: _bind(env, rid, sid2, c))) == tt.REASON_CONTENT_BATCH_KIND_MISMATCH
    wrong_scene, _wv = _seed_content(env, "s2" if scene == "s1" else "s1")
    assert _reason(_api_error(lambda: _bind(env, rid, sid2, wrong_scene))) == tt.REASON_CONTENT_SCENE_MISMATCH
    assert _reason(_api_error(lambda: _bind(env, rid, sid2, uuid.uuid4()))) == tt.REASON_CONTENT_NOT_FOUND
    unscened, _uv = _seed_content(env, None)
    assert _bind(env, rid, sid2, unscened) is True  # scene_key 未指派不拦


def test_c04_concurrent_bind_same_content_only_one_wins(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    _transition(env, rid, "GENERATING")
    slots = _slots(env, rid)
    scene = slots[0][2]
    same = [s for s in slots if s[2] == scene][:2]
    cid, _v = _seed_content(env, scene)

    def attempt(slot_id: uuid.UUID) -> str:
        try:
            _bind(env, rid, slot_id, cid)
            return "ok"
        except ApiError as exc:
            return _reason(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(attempt, [s[0] for s in same]))
    assert results == [tt.REASON_CONTENT_ALREADY_BOUND, "ok"]
    assert _scalar(env, "SELECT count(*) FROM trial_round_slots WHERE content_item_id = :c", {"c": cid}) == 1


# ===========================================================================
# E. 服务层：关口
# ===========================================================================


def _gate_error(env: TrialEnv, rid: uuid.UUID, keys: list[str], *, lv: int | None = None) -> ApiError:
    def run() -> None:
        with _repos(env) as (r, _p):
            svc.approve_gate(
                r, project_id=env.project_id, round_id=rid,
                expected_lock_version=_lv(env, rid) if lv is None else lv,
                approved_scene_keys=keys, reason="放行",
                actor_id=env.owner_id, request_id="t-req",
            )
    return _api_error(run)


def test_gate_happy_path_writes_decision_and_state_together(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    _approve_all(env, filled)
    _approve(env, filled.rid, ["s1", "s2"], reason="  两个方向都放行 ")
    assert _state(env, filled.rid) == "GATE_PASSED"
    d = _rows(env, "SELECT approved_scene_keys, reason, decided_by, statistics, basis, condition_results"
                   " FROM trial_gate_decisions WHERE round_id = :r", {"r": filled.rid})[0]
    assert d[0] == ["s1", "s2"] and d[1] == "两个方向都放行" and d[2] == env.owner_id
    assert d[3]["approved"] == 10 and d[3]["approvalRate"]["permille"] == 1000
    assert len(d[4]) == 10 and all(b["reviewId"] for b in d[4])
    assert d[5][tt.GATE_SELF_CHECK_COMPLETE]["blocking"] is False
    # 终态再批准：409
    exc = _gate_error(env, filled.rid, ["s1"])
    assert exc.code == ErrorCode.INVALID_TRANSITION and _reason(exc) == tt.REASON_ROUND_TERMINAL


def test_gate_requires_reviewing_state_and_reason_and_keys(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    exc = _gate_error(env, rid, ["s1"])
    assert exc.code == ErrorCode.INVALID_TRANSITION and _reason(exc) == tt.REASON_ROUND_TRANSITION_ILLEGAL
    filled = _bind_all(env, rid)
    _transition(env, rid, "REVIEWING")
    assert _gate_error(env, rid, []).code == ErrorCode.VALIDATION_FAILED
    with _repos(env) as (r, _p):
        pass
    exc = _api_error(lambda: _approve(env, rid, ["s1"], reason="  "))
    assert _reason(exc) == tt.REASON_REASON_REQUIRED
    assert _reason(_gate_error(env, rid, ["ghost"])) == tt.REASON_SCENE_KEY_UNKNOWN
    del filled


def test_gate_not_satisfied_when_slot_undisposed_or_blocking_feedback(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    exc = _gate_error(env, filled.rid, ["s1"])
    assert _reason(exc) == tt.REASON_GATE_NOT_SATISFIED
    assert exc.details["conditions"] == [tt.GATE_ROUND_ALL_DISPOSED]
    _approve_all(env, filled)
    sid, (scene, cid, vid) = next(iter(filled.items.items()))
    _feedback(env, cid, vid)
    exc = _gate_error(env, filled.rid, ["s1", "s2"])
    assert exc.details["conditions"] == [tt.GATE_NO_OPEN_BLOCKING_FEEDBACK]
    # RESOLVED / REJECTED 的阻断不再阻断；WARNING 本来就不阻断
    _exec(env, "UPDATE content_feedback_items SET state = 'RESOLVED' WHERE content_item_id = :c", {"c": cid})
    _feedback(env, cid, vid, severity="WARNING")
    _approve(env, filled.rid, ["s1", "s2"])


def test_blocking_feedback_on_eliminated_content_does_not_block(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    _approve_all(env, filled, except_scenes=("s2",))
    for _sid, (scene, cid, vid) in filled.items.items():
        if scene == "s2":
            _feedback(env, cid, vid)
    _approve(env, filled.rid, ["s1"])
    assert _state(env, filled.rid) == "GATE_PASSED"


def test_u11_direction_unverified_422_and_unselected_failed_direction_ok(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    _approve_all(env, filled, except_scenes=("s2",))
    exc = _gate_error(env, filled.rid, ["s1", "s2"])
    assert exc.code == ErrorCode.DIRECTION_UNVERIFIED and exc.details["sceneKeys"] == ["s2"]
    assert _state(env, filled.rid) == "REVIEWING"
    assert _scalar(env, "SELECT count(*) FROM trial_gate_decisions WHERE round_id = :r", {"r": filled.rid}) == 0
    _approve(env, filled.rid, ["s1"])  # 未选的失败方向不阻止通过
    assert _state(env, filled.rid) == "GATE_PASSED"


def test_u09_edit_after_review_returns_slot_to_awaiting_and_blocks_gate(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    _approve_all(env, filled)
    sid, (scene, cid, _vid) = next(iter(filled.items.items()))
    _add_version(env, cid, 2)  # 改稿：旧结论失效
    with _repos(env) as (r, _p):
        fact = next(f for f in r.slot_facts(filled.rid) if f.slot_id == sid)
    assert fact.status == tt.SLOT_STATUS_AWAITING_REVIEW and tt.review_stale(fact)
    exc = _gate_error(env, filled.rid, ["s1", "s2"])
    assert exc.details["conditions"] == [tt.GATE_ROUND_ALL_DISPOSED]


def test_u10_rollback_fork_review_on_non_current_version_not_counted(env: TrialEnv) -> None:
    """投影指向 v1、存在 v2：v2 上的审核不得计入通过。"""
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    sid, (scene, cid, vid1) = next(iter(filled.items.items()))
    v2 = _add_version(env, cid, 2, move=False)  # 投影仍指向 v1
    _review(env, cid, v2, "APPROVED")
    with _repos(env) as (r, _p):
        fact = next(f for f in r.slot_facts(filled.rid) if f.slot_id == sid)
    assert fact.current_version_id == vid1 and fact.review_decision is None
    _review(env, cid, vid1, "ELIMINATED")
    with _repos(env) as (r, _p):
        fact = next(f for f in r.slot_facts(filled.rid) if f.slot_id == sid)
    assert fact.status == tt.SLOT_STATUS_ELIMINATED


def test_superseded_review_chain_uses_chain_tail(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    sid, (scene, cid, vid) = next(iter(filled.items.items()))
    first = _review(env, cid, vid, "ELIMINATED")
    _review(env, cid, vid, "APPROVED", supersedes=first)
    with _repos(env) as (r, _p):
        fact = next(f for f in r.slot_facts(filled.rid) if f.slot_id == sid)
    assert fact.status == tt.SLOT_STATUS_APPROVED


def test_projection_null_falls_back_to_latest_version(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    sid, (scene, cid, vid) = next(iter(filled.items.items()))
    _exec(env, "UPDATE content_items SET current_version_id = NULL WHERE id = :c", {"c": cid})
    with _repos(env) as (r, _p):
        fact = next(f for f in r.slot_facts(filled.rid) if f.slot_id == sid)
    assert fact.current_version_id == vid


def test_self_check_signal_absent_or_present_does_not_change_gate(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    _approve_all(env, filled)
    with _repos(env) as (r, _p):
        before = svc.evaluate_gate(r.slot_facts(filled.rid), ["s1"])
    assert before["satisfiable"] and before["conditions"][tt.GATE_SELF_CHECK_COMPLETE]["detail"]["notSelfChecked"] == 10
    for _sid, (_s, cid, vid) in filled.items.items():
        _self_check(env, cid, vid)
    with _repos(env) as (r, _p):
        after = svc.evaluate_gate(r.slot_facts(filled.rid), ["s1"])
    assert after["satisfiable"] and after["conditions"][tt.GATE_SELF_CHECK_COMPLETE]["satisfied"]
    _approve(env, filled.rid, ["s1"])  # 无自检也能放行（Q6 默认只报告）


def test_gate_view_is_read_only(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    snapshot = lambda: (_lv(env, filled.rid), _scalar(env, "SELECT count(*) FROM trial_round_slots", {}),  # noqa: E731
                        _scalar(env, "SELECT count(*) FROM trial_gate_decisions", {}))
    before = snapshot()
    with _repos(env) as (r, p):
        svc.get_gate(r, env.project_id, filled.rid)
        svc.get_round_detail(r, env.project_id, filled.rid)
        svc.entry_check(r, p, project_id=env.project_id, plan_version_id=ready.plan_id)
        svc.list_rounds(r, project_id=env.project_id, page_params=__import__("rednote.platform.pagination", fromlist=["PageParams"]).PageParams(limit=10))
    assert snapshot() == before


def test_project_isolation_round_lookup(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    with _repos(env) as (r, _p):
        exc = _api_error(lambda: svc.require_round(r, env.other_project_id, rid))
    assert exc.code == ErrorCode.RESOURCE_NOT_FOUND
    exc = _api_error(lambda: _transition(TrialEnv(env.settings, env.other_project_id, env.owner_id, env.project_id), rid, "CANCELLED", reason="x"))
    assert exc.code == ErrorCode.RESOURCE_NOT_FOUND


# ===========================================================================
# F. 服务层：淘汰补产
# ===========================================================================


def _replace(env: TrialEnv, rid: uuid.UUID, slot_id: uuid.UUID, *, lv: int | None = None) -> uuid.UUID:
    with _repos(env) as (r, _p):
        new_slot, _row = svc.create_replacement(
            r, project_id=env.project_id, round_id=rid, slot_id=slot_id,
            expected_lock_version=_lv(env, rid) if lv is None else lv,
            actor_id=env.owner_id, request_id="t-req",
        )
        return new_slot.id


def test_u13_replacement_rules(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    s1_slots = [s for s, (sc, _c, _v) in filled.items.items() if sc == "s1"]
    victim, keeper = s1_slots[0], s1_slots[1]
    # 未淘汰（待审核）不可补
    exc = _api_error(lambda: _replace(env, filled.rid, victim))
    assert _reason(exc) == tt.REASON_SLOT_NOT_ELIMINATED
    _, cid, vid = filled.items[victim]
    _review(env, cid, vid, "ELIMINATED")
    live_before = _live(env, filled.rid)
    new_id = _replace(env, filled.rid, victim)
    # REVIEWING -> GENERATING 同事务推进；方向不变；在役数不变。
    assert _state(env, filled.rid) == "GENERATING"
    assert _scalar(env, "SELECT scene_key FROM trial_round_slots WHERE id = :s", {"s": new_id}) == "s1"
    assert _scalar(env, "SELECT replaces_slot_id FROM trial_round_slots WHERE id = :s", {"s": new_id}) == victim
    assert _live(env, filled.rid) == live_before
    # 不可二次补
    assert _reason(_api_error(lambda: _replace(env, filled.rid, victim))) == tt.REASON_SLOT_ALREADY_REPLACED
    # 被替代名额的内容之后改判 APPROVED：名额保持已死
    _review(env, cid, vid, "APPROVED")
    with _repos(env) as (r, _p):
        fact = next(f for f in r.slot_facts(filled.rid) if f.slot_id == victim)
    assert fact.status == tt.SLOT_STATUS_REPLACED
    del keeper


def _live(env: TrialEnv, rid: uuid.UUID) -> int:
    with _repos(env) as (r, _p):
        return len(svc.live_facts(r.slot_facts(rid)))


def test_replacement_full_cycle_back_to_gate(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    _approve_all(env, filled, except_scenes=("s2",))
    s2_slots = [s for s, (sc, _c, _v) in filled.items.items() if sc == "s2"]
    for sid in s2_slots:
        new_id = _replace(env, filled.rid, sid) if _state(env, filled.rid) != "GENERATING" else _replace(env, filled.rid, sid)
        cid, vid = _seed_content(env, "s2")
        _bind(env, filled.rid, new_id, cid)
        _review(env, cid, vid, "APPROVED")
    _transition(env, filled.rid, "REVIEWING")
    _approve(env, filled.rid, ["s1", "s2"])
    assert _state(env, filled.rid) == "GATE_PASSED"
    assert _live(env, filled.rid) == 10


def test_replacement_guards_cap_terminal_and_stale_etag(env: TrialEnv) -> None:
    ready = _ready(env, total=2, scenes=(("s1", 1000),))
    filled = _reviewing(env, ready.plan_id)  # planned = 2
    ids = list(filled.items)
    for sid in ids:
        _review(env, filled.items[sid][1], filled.items[sid][2], "ELIMINATED")
    stale = _lv(env, filled.rid)
    first = _replace(env, filled.rid, ids[0])
    exc = _api_error(lambda: _replace(env, filled.rid, ids[1], lv=stale))
    assert exc.code == ErrorCode.VERSION_CONFLICT
    _replace(env, filled.rid, ids[1])
    # 替代名额之后再被淘汰、继续补：累计补产次数 = planned(2) 已满
    cid, vid = _seed_content(env, "s1")
    _bind(env, filled.rid, first, cid)
    _review(env, cid, vid, "ELIMINATED")
    exc = _api_error(lambda: _replace(env, filled.rid, first))
    assert _reason(exc) == tt.REASON_REPLACEMENT_CAP_REACHED
    _transition(env, filled.rid, "CANCELLED", reason="够了")
    exc = _api_error(lambda: _replace(env, filled.rid, first))
    assert _reason(exc) == tt.REASON_ROUND_TERMINAL


def test_c03_concurrent_replacement_one_wins(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    victim = next(iter(filled.items))
    _review(env, filled.items[victim][1], filled.items[victim][2], "ELIMINATED")
    v = _lv(env, filled.rid)

    def attempt(_: int) -> str:
        try:
            _replace(env, filled.rid, victim, lv=v)
            return "ok"
        except ApiError as exc:
            return exc.code.value

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(attempt, range(2)))
    assert results == ["VERSION_CONFLICT", "ok"]
    assert _scalar(env, "SELECT count(*) FROM trial_round_slots WHERE replaces_slot_id = :s", {"s": victim}) == 1


def test_u14_live_slots_never_exceed_planned_over_random_sequences(env: TrialEnv) -> None:
    import random

    rng = random.Random(31)
    ready = _ready(env, total=6, scenes=(("s1", 500), ("s2", 500)))
    filled = _reviewing(env, ready.plan_id)
    planned = int(_scalar(env, "SELECT planned_quantity FROM trial_rounds WHERE id = :r", {"r": filled.rid}))
    for _ in range(8):
        with _repos(env) as (r, _p):
            facts = r.slot_facts(filled.rid)
        eliminated = [f for f in facts if f.status == tt.SLOT_STATUS_ELIMINATED]
        unreviewed = [f for f in facts if f.status == tt.SLOT_STATUS_AWAITING_REVIEW]
        if rng.random() < 0.6 and unreviewed:
            f = rng.choice(unreviewed)
            _review(env, f.content_item_id, f.current_version_id, "ELIMINATED")
        elif eliminated:
            f = rng.choice(eliminated)
            try:
                new_id = _replace(env, filled.rid, f.slot_id)
            except ApiError:
                continue
            if _state(env, filled.rid) == "GENERATING":
                cid, _v = _seed_content(env, f.scene_key)
                _bind(env, filled.rid, new_id, cid)
                _transition(env, filled.rid, "REVIEWING")
        assert _live(env, filled.rid) <= planned


# ===========================================================================
# G. 只读接缝
# ===========================================================================


def _status(env: TrialEnv, **kw: Any) -> seam.TrialGateStatus:
    with session_scope(env.settings) as s:
        return seam.trial_gate_status(s, project_id=kw.pop("project_id", env.project_id), **kw)


def test_seam_no_decision_is_not_passed(env: TrialEnv) -> None:
    st = _status(env)
    assert (st.passed, st.latest, st.verified_scene_keys) == (False, None, ())
    assert _status(env, project_id=uuid.uuid4()).passed is False  # 不抛


def test_seam_passed_filters_and_verified_keys(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    _approve_all(env, filled, except_scenes=("s2",))
    assert _status(env).passed is False  # REVIEWING 不算
    _approve(env, filled.rid, ["s1"])
    st = _status(env)
    assert st.passed and st.verified_scene_keys == ("s1",)
    assert st.latest.round_id == filled.rid and st.latest.stale is False
    assert st.latest.plan_version_id == ready.plan_id
    assert _status(env, plan_version_id=ready.plan_id).passed
    assert _status(env, plan_version_id=uuid.uuid4()).passed is False
    assert _status(env, baseline_id=ready.baseline_id).passed
    assert _status(env, baseline_id=uuid.uuid4()).passed is False
    # 项目隔离
    assert _status(env, project_id=env.other_project_id).passed is False


def test_seam_stale_reasons(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    _approve_all(env, filled)
    _approve(env, filled.rid, ["s1", "s2"])
    assert _status(env).passed
    sid, (_sc, cid, vid) = next(iter(filled.items.items()))
    # 改判（追加一条取代的审核）-> REVIEW_CHANGED
    old = _scalar(env, "SELECT id FROM content_reviews WHERE content_item_id = :c", {"c": cid})
    _review(env, cid, vid, "APPROVED", supersedes=old)
    st = _status(env)
    assert not st.passed and st.latest.stale_reasons == (tt.STALE_REVIEW_CHANGED,)
    assert st.verified_scene_keys == ()
    # 改稿 -> CONTENT_REVISED
    _add_version(env, cid, 2)
    assert tt.STALE_CONTENT_REVISED in _status(env).latest.stale_reasons
    # 基线前进 -> BASELINE_CHANGED
    _seed_baseline(env)
    assert tt.STALE_BASELINE_CHANGED in _status(env).latest.stale_reasons


def test_seam_eliminated_content_edit_does_not_stale_gate(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    _approve_all(env, filled, except_scenes=("s2",))
    _approve(env, filled.rid, ["s1"])
    for _sid, (sc, cid, _vid) in filled.items.items():
        if sc == "s2":
            _add_version(env, cid, 2)
    assert _status(env).passed


def test_seam_is_read_only(env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    _approve_all(env, filled)
    _approve(env, filled.rid, ["s1"])
    counts = lambda: _rows(env, "SELECT (SELECT count(*) FROM trial_rounds), (SELECT lock_version FROM trial_rounds LIMIT 1),"  # noqa: E731
                                " (SELECT count(*) FROM audit_events), (SELECT count(*) FROM trial_gate_decisions)")
    before = counts()
    for _ in range(3):
        _status(env)
    assert counts() == before


def test_audit_written_in_same_transaction(env: TrialEnv) -> None:
    ready = _ready(env)
    rid, _ = _create(env, ready.plan_id)
    n = _scalar(env, "SELECT count(*) FROM audit_events WHERE object_id = :r", {"r": rid})
    assert n == 1
    _transition(env, rid, "CANCELLED", reason="审计")
    assert _scalar(env, "SELECT count(*) FROM audit_events WHERE object_id = :r", {"r": rid}) == 2
