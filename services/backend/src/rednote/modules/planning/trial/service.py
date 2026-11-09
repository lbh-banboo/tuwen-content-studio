"""WP31 试产关口 / 淘汰补产的服务层。

事务边界：本层只 `flush`，不 `commit`（提交归请求级 `session_scope`）。
并发：先 `SELECT ... FOR UPDATE` 取轮行 -> 判前置 -> 写 -> 条件 UPDATE
（`WHERE lock_version = :v`）；412 在前（乐观锁）、409 在后（业务状态）。

跨模块：对他表只读（见 `repository.py`）；复用 planning 既有设施
（`get_effective_limits` / `validate_quantity` / `allocation` / `permille` / `plan_payload`），
以及 `audit.record_audit`、`versions.canonical_payload_hash`（Q15 待主线确认）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError

from rednote.modules.audit.repository import AuditRepository
from rednote.modules.audit.service import record_audit
from rednote.modules.planning import service as plan_service
from rednote.modules.planning import settings_service
from rednote.modules.planning.limits import QuantityOutOfRange, validate_quantity
from rednote.modules.planning.allocation import allocate_from_remaining
from rednote.modules.planning.permille import normalize_permille
from rednote.modules.planning.repository import PlanningRepository
from rednote.modules.planning.trial import seam
from rednote.modules.planning.trial.repository import TrialRepository
from rednote.modules.planning.trial.types import (
    AUDIT_ACTION_CREATE,
    AUDIT_ACTION_GATE,
    AUDIT_ACTION_REPLACE,
    AUDIT_ACTION_TRANSITION,
    AUDIT_OBJECT_ROUND,
    BATCH_KIND_TRIAL,
    CONTENT_SETTLED_STATES,
    ENDPOINT_FORBIDDEN_EDGES,
    ENTRY_ACTIVE_BASELINE,
    ENTRY_PLAN_BOUND_TO_ACTIVE_BASELINE,
    ENTRY_STEP1_APPROVED,
    ENTRY_STEP2_APPROVED,
    ENTRY_STEP3_APPROVED,
    GATE_DIRECTION_HAS_APPROVED_SAMPLE,
    GATE_HARD_CONDITIONS,
    GATE_NO_OPEN_BLOCKING_FEEDBACK,
    GATE_ROUND_ALL_DISPOSED,
    GATE_SELF_CHECK_COMPLETE,
    GENERATION_REQUEST_KEY_BATCH_KIND,
    MAX_IDEMPOTENCY_KEY_CHARS,
    MAX_QUANTITY_BOUND,
    MAX_REASON_CHARS,
    REASON_ACTIVE_ROUND_EXISTS,
    REASON_ALLOCATION_EMPTY,
    REASON_CONTENT_ALREADY_BOUND,
    REASON_CONTENT_BATCH_KIND_MISMATCH,
    REASON_CONTENT_NOT_FOUND,
    REASON_CONTENT_PROJECT_MISMATCH,
    REASON_CONTENT_SCENE_MISMATCH,
    REASON_DIRECTION_UNVERIFIED,
    REASON_GATE_NOT_SATISFIED,
    REASON_IDEMPOTENCY_KEY_REUSED,
    REASON_KIND_INVALID,
    REASON_LOCK_VERSION_MISMATCH,
    REASON_NO_ACTIVE_BASELINE,
    REASON_PLAN_BASELINE_STALE,
    REASON_PLAN_DRAFT_DRIFTED,
    REASON_PLAN_VERSION_INVALID,
    REASON_PROJECT_NOT_FOUND,
    REASON_QUANTITY_INVALID,
    REASON_REASON_REQUIRED,
    REASON_REPLACEMENT_CAP_REACHED,
    REASON_ROUND_NOT_FOUND,
    REASON_ROUND_NOT_GENERATING,
    REASON_ROUND_NOT_SETTLED,
    REASON_ROUND_TERMINAL,
    REASON_ROUND_TRANSITION_ILLEGAL,
    REASON_SCENE_KEY_UNKNOWN,
    REASON_SCENE_KEYS_REQUIRED,
    REASON_SLOT_ALREADY_BOUND,
    REASON_SLOT_ALREADY_REPLACED,
    REASON_SLOT_NOT_ELIMINATED,
    REASON_SLOT_NOT_FOUND,
    REASON_SOURCE_ROUND_NOT_PASSED,
    REASON_SOURCE_ROUND_REQUIRED,
    REASON_STEP_APPROVAL_MISSING,
    REASON_TO_STATE_INVALID,
    REASON_TO_STATE_USE_GATE_DECISION,
    ROUND_KIND_SUPPLEMENT,
    ROUND_KIND_TRIAL,
    ROUND_KINDS,
    ROUND_STATE_CANCELLED,
    ROUND_STATE_GATE_PASSED,
    ROUND_STATE_GENERATING,
    ROUND_STATE_PLANNED,
    ROUND_STATE_REVIEWING,
    ROUND_TERMINAL_STATES,
    ROUND_TRANSITIONS,
    SLOT_STATUS_APPROVED,
    SLOT_STATUS_AWAITING_REVIEW,
    SLOT_STATUS_ELIMINATED,
    SLOT_STATUS_REPLACED,
    SLOT_STATUS_UNBOUND,
    STEP_BASELINE,
    STEP_BRIEF,
    STEP_RESEARCH,
    SUBJECT_KIND_BASELINE,
    SUBJECT_KIND_BRIEF,
    SUBJECT_KIND_RESEARCH,
    TRANSITION_ENDPOINT_TARGETS,
    SlotFact,
    derive_slot_status,
    ratio,
    review_stale,
)
from rednote.modules.versions.service import canonical_payload_hash
from rednote.platform.concurrency import build_etag, version_conflict
from rednote.platform.errors import ApiError, ErrorCode
from rednote.platform.pagination import (
    CursorPosition,
    CursorSpec,
    PageInfo,
    PageParams,
    decode_cursor,
    encode_cursor,
)

# ---------------------------------------------------------------------------
# 错误构造
# ---------------------------------------------------------------------------


def _err(code: ErrorCode, message: str, reason: str, **extra: Any) -> ApiError:
    details: dict[str, Any] = {"reason": reason}
    details.update(extra)
    return ApiError(code, message, details=details)


def _invalid(message: str, reason: str, *, field: str, **extra: Any) -> ApiError:
    details: dict[str, Any] = {
        "reason": reason,
        "fieldErrors": [{"field": field, "type": reason, "message": message}],
    }
    details.update(extra)
    return ApiError(ErrorCode.VALIDATION_FAILED, message, details=details)


def _conflict(message: str, reason: str, **extra: Any) -> ApiError:
    return _err(ErrorCode.INVALID_TRANSITION, message, reason, **extra)


def _not_found(message: str, reason: str, **extra: Any) -> ApiError:
    return _err(ErrorCode.RESOURCE_NOT_FOUND, message, reason, **extra)


# ---------------------------------------------------------------------------
# 基础读
# ---------------------------------------------------------------------------


def round_etag(row: Any) -> str:
    return build_etag(row.id, row.lock_version)


def require_project(repo: TrialRepository, project_id: uuid.UUID) -> None:
    if not repo.project_exists(project_id):
        raise _not_found(
            f"项目 {project_id} 不存在。",
            REASON_PROJECT_NOT_FOUND,
            projectId=str(project_id),
        )


def require_round(
    repo: TrialRepository, project_id: uuid.UUID, round_id: uuid.UUID
) -> Any:
    """轮必须存在且属于该项目；否则统一 404（防枚举）。"""
    row = repo.get_round(round_id)
    if row is None or row.project_id != project_id:
        raise _not_found(
            "试产轮不存在或无权访问。",
            REASON_ROUND_NOT_FOUND,
            resource="trialRound",
            resourceId=str(round_id),
        )
    return row


def _lock_round(
    repo: TrialRepository,
    project_id: uuid.UUID,
    round_id: uuid.UUID,
    expected_lock_version: int,
) -> Any:
    """行锁取轮 + 版本比对（不匹配 412，回带当前 ETag）。"""
    row = repo.get_round_for_update(round_id)
    if row is None or row.project_id != project_id:
        raise _not_found(
            "试产轮不存在或无权访问。",
            REASON_ROUND_NOT_FOUND,
            resource="trialRound",
            resourceId=str(round_id),
        )
    if row.lock_version != expected_lock_version:
        raise _version_conflict(row)
    return row


def _version_conflict(row: Any) -> ApiError:
    return version_conflict(
        resource_id=row.id,
        current_version=row.lock_version,
        reason=REASON_LOCK_VERSION_MISMATCH,
        message="试产轮已被更新，请重新读取最新版本后再提交。",
    )


def _reload(repo: TrialRepository, round_id: uuid.UUID) -> Any:
    row = repo.get_round(round_id)
    assert row is not None
    return row


def _audit(
    repo: TrialRepository,
    *,
    action: str,
    row: Any,
    actor_id: uuid.UUID | None,
    request_id: str | None,
    before: dict[str, Any] | None,
    after: dict[str, Any],
    reason: str | None = None,
) -> None:
    if actor_id is None or request_id is None:
        return
    record_audit(
        AuditRepository(repo.session),
        action=action,
        object_type=AUDIT_OBJECT_ROUND,
        object_id=row.id,
        request_id=request_id,
        actor_id=actor_id,
        project_id=row.project_id,
        before=before,
        after=after,
        reason=reason,
    )


def _audit_shape(row: Any) -> dict[str, Any]:
    return {
        "roundNo": row.round_no,
        "kind": row.kind,
        "state": row.state,
        "lockVersion": row.lock_version,
        "plannedQuantity": row.planned_quantity,
    }


# ---------------------------------------------------------------------------
# 入口关口（纯函数 + 取数；创建 / 开工 / 预检共用同一个判定）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntryFacts:
    active_baseline_id: uuid.UUID | None
    step3: bool
    step1: bool
    step2: bool
    brief_version_id: uuid.UUID | None
    #: PLAN 快照里的 baselineId；`NOT_EVALUATED` 表示本次未带 PLAN。
    plan_baseline_id: str | None
    plan_evaluated: bool


def gather_entry_facts(
    repo: TrialRepository,
    *,
    project_id: uuid.UUID,
    plan_snapshot: dict[str, Any] | None,
) -> EntryFacts:
    active = repo.active_baseline_id(project_id)
    brief_version: uuid.UUID | None = None
    step3 = step1 = False
    if active is not None:
        step3 = repo.has_effective_approval(
            project_id=project_id,
            step_no=STEP_BASELINE,
            subject_kind=SUBJECT_KIND_BASELINE,
            subject_version_id=active,
        )
        brief_version = repo.baseline_brief_version_id(active)
        if brief_version is not None:
            step1 = repo.has_effective_approval(
                project_id=project_id,
                step_no=STEP_BRIEF,
                subject_kind=SUBJECT_KIND_BRIEF,
                subject_version_id=brief_version,
            )
    step2 = repo.has_effective_approval(
        project_id=project_id,
        step_no=STEP_RESEARCH,
        subject_kind=SUBJECT_KIND_RESEARCH,
        subject_version_id=None,
    )
    return EntryFacts(
        active_baseline_id=active,
        step3=step3,
        step1=step1,
        step2=step2,
        brief_version_id=brief_version,
        plan_baseline_id=None
        if plan_snapshot is None
        else plan_snapshot.get("baselineId"),
        plan_evaluated=plan_snapshot is not None,
    )


def evaluate_entry(facts: EntryFacts) -> list[dict[str, Any]]:
    """入口条件逐项。`satisfied=None` 表示本次未评估（不是 False）。"""
    plan_ok: bool | None = None
    if facts.plan_evaluated:
        plan_ok = facts.plan_baseline_id is None or (
            facts.active_baseline_id is not None
            and facts.plan_baseline_id == str(facts.active_baseline_id)
        )
    return [
        {
            "code": ENTRY_ACTIVE_BASELINE,
            "satisfied": facts.active_baseline_id is not None,
            "detail": {
                "activeBaselineId": None
                if facts.active_baseline_id is None
                else str(facts.active_baseline_id)
            },
        },
        {"code": ENTRY_STEP1_APPROVED, "satisfied": facts.step1, "detail": {
            "briefVersionId": None
            if facts.brief_version_id is None
            else str(facts.brief_version_id)
        }},
        {"code": ENTRY_STEP2_APPROVED, "satisfied": facts.step2, "detail": {}},
        {"code": ENTRY_STEP3_APPROVED, "satisfied": facts.step3, "detail": {}},
        {
            "code": ENTRY_PLAN_BOUND_TO_ACTIVE_BASELINE,
            "satisfied": plan_ok,
            "detail": {"planBaselineId": facts.plan_baseline_id},
        },
    ]


def require_entry(facts: EntryFacts) -> None:
    """入口不满足即 409。顺序：基线 -> 步骤批准 -> PLAN 绑定。"""
    results = {c["code"]: c for c in evaluate_entry(facts)}
    if not results[ENTRY_ACTIVE_BASELINE]["satisfied"]:
        raise _conflict(
            "项目没有生效基线，不能开试产。",
            REASON_NO_ACTIVE_BASELINE,
            conditions=[ENTRY_ACTIVE_BASELINE],
        )
    missing = [
        code
        for code in (ENTRY_STEP1_APPROVED, ENTRY_STEP2_APPROVED, ENTRY_STEP3_APPROVED)
        if not results[code]["satisfied"]
    ]
    if missing:
        raise _conflict(
            "八步关口未满足：步骤 1~3 需有效批准。",
            REASON_STEP_APPROVAL_MISSING,
            conditions=missing,
        )
    if results[ENTRY_PLAN_BOUND_TO_ACTIVE_BASELINE]["satisfied"] is False:
        raise _conflict(
            "PLAN 绑定的基线不是项目当前生效基线。",
            REASON_PLAN_BASELINE_STALE,
            planBaselineId=facts.plan_baseline_id,
            activeBaselineId=str(facts.active_baseline_id),
        )


def entry_check(
    repo: TrialRepository,
    planning_repo: PlanningRepository,
    *,
    project_id: uuid.UUID,
    plan_version_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """入口预检（纯读：`materialize=False`）。"""
    require_project(repo, project_id)
    snapshot: dict[str, Any] | None = None
    plan_error: str | None = None
    if plan_version_id is not None:
        try:
            snapshot = pin_plan(repo, planning_repo, project_id, plan_version_id)
        except ApiError as exc:
            plan_error = str((exc.details or {}).get("reason"))
    facts = gather_entry_facts(repo, project_id=project_id, plan_snapshot=snapshot)
    conditions = evaluate_entry(facts)
    if plan_error is not None:
        for c in conditions:
            if c["code"] == ENTRY_PLAN_BOUND_TO_ACTIVE_BASELINE:
                c["satisfied"] = False
                c["detail"] = {"reason": plan_error}
    effective = settings_service.get_effective_limits(
        planning_repo, "batch.limits", project_id, materialize=False
    )
    trial_limit: int | None = None
    if effective.configured:
        trial_limit = effective.as_limits().max_for(BATCH_KIND_TRIAL)
    return {
        "ok": all(c["satisfied"] is not False for c in conditions)
        and all(
            c["satisfied"]
            for c in conditions
            if c["code"] != ENTRY_PLAN_BOUND_TO_ACTIVE_BASELINE
        ),
        "conditions": conditions,
        "limits": {"trial": trial_limit, "source": effective.source},
    }


# ---------------------------------------------------------------------------
# PLAN 钉住（OP-230：重算草稿指纹与 payload_hash 比对）
# ---------------------------------------------------------------------------


def pin_plan(
    repo: TrialRepository,
    planning_repo: PlanningRepository,
    project_id: uuid.UUID,
    plan_version_id: uuid.UUID,
) -> dict[str, Any]:
    node = repo.version_node(plan_version_id)
    if (
        node is None
        or node["entity_type"] != "PLAN"
        or node["project_id"] != project_id
        or node["entity_id"] != project_id
    ):
        raise _invalid(
            "planVersionId 不是本项目的 PLAN 版本。",
            REASON_PLAN_VERSION_INVALID,
            field="planVersionId",
        )
    draft = planning_repo.get_draft(project_id)
    if draft is None:
        raise _conflict(
            "计划草稿不存在，无法核对 PLAN 指纹。",
            REASON_PLAN_DRAFT_DRIFTED,
            planVersionId=str(plan_version_id),
        )
    payload = plan_service.plan_payload(
        draft,
        scene_result=plan_service.dimension_allocation(
            draft, plan_service.DIMENSION_SCENE
        ),
        form_result=plan_service.dimension_allocation(
            draft, plan_service.DIMENSION_FORM
        ),
    )
    if canonical_payload_hash(payload) != node["payload_hash"]:
        raise _conflict(
            "计划草稿在提交后被改动，与已固化的 PLAN 版本不一致。",
            REASON_PLAN_DRAFT_DRIFTED,
            planVersionId=str(plan_version_id),
        )
    return payload


# ---------------------------------------------------------------------------
# 创建轮
# ---------------------------------------------------------------------------


def _normalize_keys(values: Sequence[Any] | None) -> list[str]:
    seen: list[str] = []
    for raw in values or ():
        if not isinstance(raw, str) or not raw.strip():
            raise _invalid(
                "sceneKeys 每项必须是非空字符串。",
                REASON_SCENE_KEY_UNKNOWN,
                field="sceneKeys",
            )
        key = raw.strip()
        if key not in seen:
            seen.append(key)
    return seen


def _scene_items(snapshot: dict[str, Any]) -> list[Any]:
    from rednote.modules.planning.types import AllocationItem

    return [
        AllocationItem(
            key=str(line["key"]),
            weight_permille=int(line.get("weightPermille", 0)),
            capacity=line.get("capacity"),
            sort_order=int(line.get("sortOrder", 0)),
        )
        for line in snapshot.get("scenes", [])
    ]


def create_round(
    repo: TrialRepository,
    planning_repo: PlanningRepository,
    *,
    project_id: uuid.UUID,
    kind: str,
    plan_version_id: uuid.UUID,
    quantity: int | None = None,
    source_round_id: uuid.UUID | None = None,
    scene_keys: Sequence[str] | None = None,
    idempotency_key: str | None = None,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> tuple[Any, bool]:
    """创建一轮。返回 `(轮, created)`；幂等命中返回 `(既有轮, False)`。"""
    if kind not in ROUND_KINDS:
        raise _invalid(
            f"kind 只能是 {sorted(ROUND_KINDS)}。", REASON_KIND_INVALID, field="kind"
        )
    key: str | None = None
    if idempotency_key is not None:
        key = idempotency_key.strip()
        if not key or len(key) > MAX_IDEMPOTENCY_KEY_CHARS:
            raise _invalid(
                "Idempotency-Key 不能为空且不超过 160 字符。",
                REASON_QUANTITY_INVALID,
                field="Idempotency-Key",
            )
    if quantity is not None and (
        isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1
    ):
        raise _invalid("quantity 必须是正整数。", REASON_QUANTITY_INVALID, field="quantity")
    keys = _normalize_keys(scene_keys)

    require_project(repo, project_id)
    repo.lock_project_rounds(project_id)

    request_hash = canonical_payload_hash(
        {
            "kind": kind,
            "planVersionId": str(plan_version_id),
            "quantity": quantity,
            "sourceRoundId": None if source_round_id is None else str(source_round_id),
            "sceneKeys": sorted(keys),
        }
    )
    if key is not None:
        existing = repo.find_round_by_idempotency(project_id, key)
        if existing is not None:
            if existing.request_hash == request_hash:
                return existing, False
            raise _err(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "同一 Idempotency-Key 对应了不同的请求内容。",
                REASON_IDEMPOTENCY_KEY_REUSED,
            )

    open_round = repo.find_open_round(project_id)
    if open_round is not None:
        raise _conflict(
            "项目已有一轮未终态的试产。",
            REASON_ACTIVE_ROUND_EXISTS,
            roundId=str(open_round.id),
            state=open_round.state,
        )

    # 入口：先基线 / 步骤批准（不带 PLAN），再钉 PLAN，再判 PLAN 绑定。
    require_entry(gather_entry_facts(repo, project_id=project_id, plan_snapshot=None))
    snapshot = pin_plan(repo, planning_repo, project_id, plan_version_id)
    facts = gather_entry_facts(repo, project_id=project_id, plan_snapshot=snapshot)
    require_entry(facts)
    assert facts.active_baseline_id is not None

    scene_lines = {str(l["key"]): l for l in snapshot.get("scenes", [])}
    items = _scene_items(snapshot)
    if kind == ROUND_KIND_SUPPLEMENT:
        if source_round_id is None:
            raise _invalid(
                "补充轮必须指明 sourceRoundId。",
                REASON_SOURCE_ROUND_REQUIRED,
                field="sourceRoundId",
            )
        source = repo.get_round(source_round_id)
        if source is None or source.project_id != project_id:
            raise _not_found(
                "源试产轮不存在。", REASON_ROUND_NOT_FOUND, resourceId=str(source_round_id)
            )
        if source.kind != ROUND_KIND_TRIAL or source.state != ROUND_STATE_GATE_PASSED:
            raise _conflict(
                "补充轮的源轮必须是已通过关口的 TRIAL 轮。",
                REASON_SOURCE_ROUND_NOT_PASSED,
                sourceState=source.state,
                sourceKind=source.kind,
            )
        if not keys:
            raise _invalid(
                "补充轮必须给出 sceneKeys。",
                REASON_SCENE_KEYS_REQUIRED,
                field="sceneKeys",
            )
        bad = [
            k
            for k in keys
            if k not in scene_lines or int(scene_lines[k].get("weightPermille", 0)) <= 0
        ]
        if bad:
            raise _invalid(
                "sceneKeys 含不在 PLAN 中或已停用的方向。",
                REASON_SCENE_KEY_UNKNOWN,
                field="sceneKeys",
                sceneKeys=bad,
            )
        items = normalize_permille([i for i in items if i.key in keys])
        n = quantity if quantity is not None else len(keys)
    else:
        if source_round_id is not None:
            raise _invalid(
                "TRIAL 轮不得带 sourceRoundId。",
                REASON_SOURCE_ROUND_REQUIRED,
                field="sourceRoundId",
            )
        n = quantity if quantity is not None else snapshot.get("totalQuantity")
        if n is None:
            raise _invalid("缺少 quantity。", REASON_QUANTITY_INVALID, field="quantity")

    effective = settings_service.require_configured(
        settings_service.get_effective_limits(
            planning_repo, "batch.limits", project_id, materialize=True
        )
    )
    limits = effective.as_limits()
    try:
        validate_quantity(limits, BATCH_KIND_TRIAL, int(n), source=effective.source)
    except QuantityOutOfRange as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            str(exc),
            details={
                "reason": exc.reason,
                "limit": exc.limit,
                "source": exc.source,
                "batchKind": exc.batch_kind,
                "quantity": exc.quantity,
                "fieldErrors": [
                    {"field": "quantity", "type": exc.reason, "message": str(exc)}
                ],
            },
        ) from exc

    result = allocate_from_remaining(
        items, repo.live_scene_counts(project_id), batch_total=int(n)
    )
    order = {i.key: (i.sort_order, i.key) for i in items}
    planned_lines = sorted(
        (l for l in result.lines if l.allocated > 0), key=lambda l: order[l.key]
    )
    planned = sum(l.allocated for l in planned_lines)
    if planned == 0:
        raise _invalid(
            "按当前比例与容量一个名额都分不出来。",
            REASON_ALLOCATION_EMPTY,
            field="quantity",
            unallocated=result.unallocated,
        )

    row = repo.insert_round(
        project_id=project_id,
        round_no=repo.next_round_no(project_id),
        kind=kind,
        state=ROUND_STATE_PLANNED,
        plan_version_id=plan_version_id,
        baseline_id=facts.active_baseline_id,
        source_round_id=source_round_id,
        requested_quantity=int(n),
        planned_quantity=planned,
        limit_applied=min(limits.max_for(BATCH_KIND_TRIAL), MAX_QUANTITY_BOUND),
        limit_source=effective.source,
        plan_snapshot=snapshot,
        idempotency_key=key,
        request_hash=None if key is None else request_hash,
        created_by=actor_id,
    )
    slot_no = 0
    for line in planned_lines:
        for _ in range(line.allocated):
            slot_no += 1
            repo.insert_slot(round_id=row.id, slot_no=slot_no, scene_key=line.key)
    repo.flush()
    row = _reload(repo, row.id)
    _audit(
        repo,
        action=AUDIT_ACTION_CREATE,
        row=row,
        actor_id=actor_id,
        request_id=request_id,
        before={},  # 审计要求 before/after 哈希成对；创建以空对象为 before
        after=_audit_shape(row),
    )
    return row, True


# ---------------------------------------------------------------------------
# 派生：统计与关口判定（纯函数）
# ---------------------------------------------------------------------------


def live_facts(facts: Sequence[SlotFact]) -> list[SlotFact]:
    return [f for f in facts if not f.replaced]


def compute_statistics(facts: Sequence[SlotFact]) -> dict[str, Any]:
    """计数总是完整扫描得出（0 即确为零）；比率分母 0 ⇒ permille null。"""
    live = live_facts(facts)
    status = {f.slot_id: derive_slot_status(f) for f in live}
    bound = [f for f in live if f.content_item_id is not None]
    approved = [f for f in live if status[f.slot_id] == SLOT_STATUS_APPROVED]
    eliminated = [f for f in live if status[f.slot_id] == SLOT_STATUS_ELIMINATED]
    awaiting = [f for f in live if status[f.slot_id] == SLOT_STATUS_AWAITING_REVIEW]
    unbound = [f for f in live if status[f.slot_id] == SLOT_STATUS_UNBOUND]
    by_scene: dict[str, dict[str, int]] = {}
    for f in live:
        bucket = by_scene.setdefault(
            f.scene_key, {"live": 0, "approved": 0, "eliminated": 0, "pending": 0}
        )
        bucket["live"] += 1
        s = status[f.slot_id]
        if s == SLOT_STATUS_APPROVED:
            bucket["approved"] += 1
        elif s == SLOT_STATUS_ELIMINATED:
            bucket["eliminated"] += 1
        else:
            bucket["pending"] += 1
    return {
        "liveSlots": len(live),
        "replacedSlots": len(facts) - len(live),
        "generated": sum(1 for f in bound if f.content_state == "GENERATED"),
        "failed": sum(1 for f in bound if f.content_state == "FAILED"),
        "approved": len(approved),
        "eliminated": len(eliminated),
        "pending": len(awaiting) + len(unbound),
        "unbound": len(unbound),
        "approvedOnFirstVersion": sum(1 for f in approved if f.review_on_first_version),
        "notSelfChecked": sum(
            1
            for f in bound
            if status[f.slot_id] != SLOT_STATUS_ELIMINATED and not f.self_checked
        ),
        "approvalRate": ratio(len(approved), len(approved) + len(eliminated)),
        "byScene": by_scene,
    }


def evaluate_gate(
    facts: Sequence[SlotFact], approved_scene_keys: Sequence[str] | None
) -> dict[str, Any]:
    """关口判定（派生，不是状态）。`approved_scene_keys=None` 表示尚未选方向。"""
    live = live_facts(facts)
    status = {f.slot_id: derive_slot_status(f) for f in live}
    undisposed = [
        f
        for f in live
        if status[f.slot_id] not in (SLOT_STATUS_APPROVED, SLOT_STATUS_ELIMINATED)
    ]
    blocking = [
        f
        for f in live
        if f.content_item_id is not None
        and status[f.slot_id] != SLOT_STATUS_ELIMINATED
        and f.open_blocking
    ]
    scenes: list[str] = []
    for f in sorted(live, key=lambda x: x.slot_no):
        if f.scene_key not in scenes:
            scenes.append(f.scene_key)
    directions = []
    for s in scenes:
        items = [f for f in live if f.scene_key == s]
        approved = sum(1 for f in items if status[f.slot_id] == SLOT_STATUS_APPROVED)
        directions.append(
            {
                "sceneKey": s,
                "liveSlots": len(items),
                "approved": approved,
                "eliminated": sum(
                    1 for f in items if status[f.slot_id] == SLOT_STATUS_ELIMINATED
                ),
                "pending": sum(
                    1
                    for f in items
                    if status[f.slot_id]
                    not in (SLOT_STATUS_APPROVED, SLOT_STATUS_ELIMINATED)
                ),
                "verified": approved >= 1,
            }
        )
    verified = {d["sceneKey"] for d in directions if d["verified"]}
    if approved_scene_keys is None:
        dir_ok = bool(verified)
        unverified: list[str] = []
    else:
        unverified = [k for k in approved_scene_keys if k not in verified]
        dir_ok = bool(approved_scene_keys) and not unverified
    not_checked = [
        f
        for f in live
        if f.content_item_id is not None
        and status[f.slot_id] != SLOT_STATUS_ELIMINATED
        and not f.self_checked
    ]
    conditions = {
        GATE_ROUND_ALL_DISPOSED: {
            "satisfied": bool(live) and not undisposed,
            "blocking": True,
            "detail": {"undisposedSlotIds": [str(f.slot_id) for f in undisposed]},
        },
        GATE_NO_OPEN_BLOCKING_FEEDBACK: {
            "satisfied": not blocking,
            "blocking": True,
            "detail": {"contentItemIds": [str(f.content_item_id) for f in blocking]},
        },
        GATE_DIRECTION_HAS_APPROVED_SAMPLE: {
            "satisfied": dir_ok,
            "blocking": True,
            "detail": {
                "selectionGiven": approved_scene_keys is not None,
                "unverifiedSceneKeys": unverified,
            },
        },
        GATE_SELF_CHECK_COMPLETE: {
            "satisfied": not not_checked,
            "blocking": False,
            "detail": {"notSelfChecked": len(not_checked)},
        },
    }
    return {
        "conditions": conditions,
        "directions": directions,
        "satisfiable": all(
            conditions[c]["satisfied"] for c in GATE_HARD_CONDITIONS
        ),
    }


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


def round_body(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "projectId": row.project_id,
        "roundNo": row.round_no,
        "kind": row.kind,
        "state": row.state,
        "planVersionId": row.plan_version_id,
        "baselineId": row.baseline_id,
        "sourceRoundId": row.source_round_id,
        "requestedQuantity": row.requested_quantity,
        "plannedQuantity": row.planned_quantity,
        "unallocatedQuantity": row.requested_quantity - row.planned_quantity,
        "limitApplied": row.limit_applied,
        "limitSource": row.limit_source,
        "cancelReason": row.cancel_reason,
        "createdBy": row.created_by,
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
        "etag": round_etag(row),
    }


def decision_body(
    repo: TrialRepository, row: Any, decision: Any
) -> dict[str, Any]:
    reasons = seam.decision_stale_reasons(
        repo.session,
        project_id=row.project_id,
        round_baseline_id=row.baseline_id,
        basis=list(decision.basis or []),
    )
    return {
        "id": decision.id,
        "roundId": decision.round_id,
        "approvedSceneKeys": list(decision.approved_scene_keys),
        "conditionResults": decision.condition_results,
        "statistics": decision.statistics,
        "basis": decision.basis,
        "reason": decision.reason,
        "decidedBy": decision.decided_by,
        "decidedAt": decision.decided_at,
        "stale": bool(reasons),
        "staleReasons": list(reasons),
    }


def _slot_bodies(repo: TrialRepository, row: Any) -> list[dict[str, Any]]:
    facts = repo.slot_facts(row.id)
    replaced_by = {
        f.replaces_slot_id: f.slot_id for f in facts if f.replaces_slot_id is not None
    }
    bound_at = {s.id: s.bound_at for s in repo.list_slots(row.id)}
    out = []
    for f in facts:
        bound = f.content_item_id is not None
        out.append(
            {
                "id": f.slot_id,
                "slotNo": f.slot_no,
                "sceneKey": f.scene_key,
                "formKey": f.form_key,
                "contentItemId": f.content_item_id,
                "boundAt": bound_at.get(f.slot_id),
                "replacesSlotId": f.replaces_slot_id,
                "replacedBySlotId": replaced_by.get(f.slot_id),
                "status": derive_slot_status(f),
                "reviewStale": review_stale(f),
                "contentState": f.content_state,
                "currentVersionId": f.current_version_id,
                "reviewId": f.review_id,
                "openBlockingFeedback": f.open_blocking if bound else None,
                "selfChecked": f.self_checked if bound else None,
            }
        )
    return out


def get_round_detail(
    repo: TrialRepository, project_id: uuid.UUID, round_id: uuid.UUID
) -> dict[str, Any]:
    row = require_round(repo, project_id, round_id)
    facts = repo.slot_facts(row.id)
    decision = repo.get_decision_by_round(row.id)
    body = round_body(row)
    body.update(
        {
            "planSnapshot": row.plan_snapshot,
            "statistics": compute_statistics(facts),
            "slots": _slot_bodies(repo, row),
            "decision": None
            if decision is None
            else decision_body(repo, row, decision),
        }
    )
    return body


def list_rounds(
    repo: TrialRepository,
    *,
    project_id: uuid.UUID,
    page_params: PageParams,
) -> tuple[list[dict[str, Any]], PageInfo]:
    require_project(repo, project_id)
    spec = CursorSpec(
        sort_fields=("roundNo",),
        direction="desc",
        filters={"projectId": str(project_id)},
    )
    before: int | None = None
    if page_params.cursor:
        pos = decode_cursor(page_params.cursor, spec)
        try:
            before = int(pos.sort_values[0])
        except (ValueError, TypeError, IndexError) as exc:
            raise ApiError(
                ErrorCode.MALFORMED_REQUEST,
                details={"reason": "cursor_malformed", "field": "cursor"},
            ) from exc
    rows = repo.list_rounds(
        project_id, before_round_no=before, limit=page_params.limit + 1
    )
    has_more = len(rows) > page_params.limit
    page_rows = rows[: page_params.limit]
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(
            spec,
            CursorPosition(sort_values=(last.round_no,), item_id=str(last.id)),
        )
    items = []
    for r in page_rows:
        b = round_body(r)
        b["statistics"] = compute_statistics(repo.slot_facts(r.id))
        items.append(b)
    return items, PageInfo(next_cursor=next_cursor, has_more=has_more)


def get_gate(
    repo: TrialRepository,
    project_id: uuid.UUID,
    round_id: uuid.UUID,
    *,
    scene_keys: Sequence[str] | None = None,
) -> dict[str, Any]:
    row = require_round(repo, project_id, round_id)
    facts = repo.slot_facts(row.id)
    keys = None if scene_keys is None else _normalize_keys(scene_keys)
    evaluation = evaluate_gate(facts if row.state != ROUND_STATE_CANCELLED else [], keys)
    decision = repo.get_decision_by_round(row.id)
    return {
        "roundId": row.id,
        "state": row.state,
        "etag": round_etag(row),
        "conditions": evaluation["conditions"],
        "directions": evaluation["directions"],
        "satisfiable": evaluation["satisfiable"],
        "statistics": compute_statistics(facts),
        "decision": None if decision is None else decision_body(repo, row, decision),
    }


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


def _require_edge(row: Any, to_state: str) -> None:
    if row.state in ROUND_TERMINAL_STATES:
        raise _conflict(
            f"轮已是终态 {row.state}，不能再迁移。",
            REASON_ROUND_TERMINAL,
            **{"from": row.state, "to": to_state, "allowed": []},
        )
    allowed = sorted(ROUND_TRANSITIONS[row.state])
    if to_state not in ROUND_TRANSITIONS[row.state]:
        raise _conflict(
            f"不允许从 {row.state} 迁移到 {to_state}。",
            REASON_ROUND_TRANSITION_ILLEGAL,
            **{"from": row.state, "to": to_state, "allowed": allowed},
        )


def transition_round(
    repo: TrialRepository,
    *,
    project_id: uuid.UUID,
    round_id: uuid.UUID,
    expected_lock_version: int,
    to_state: str,
    reason: str | None = None,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> Any:
    if to_state == ROUND_STATE_GATE_PASSED:
        raise _invalid(
            "进入扩产必须经 gate-decisions 人工批准。",
            REASON_TO_STATE_USE_GATE_DECISION,
            field="toState",
        )
    if to_state not in TRANSITION_ENDPOINT_TARGETS:
        raise _invalid(
            f"toState 只能是 {sorted(TRANSITION_ENDPOINT_TARGETS)}。",
            REASON_TO_STATE_INVALID,
            field="toState",
        )
    cancel_reason: str | None = None
    if to_state == ROUND_STATE_CANCELLED:
        cancel_reason = (reason or "").strip()
        if not cancel_reason or len(cancel_reason) > MAX_REASON_CHARS:
            raise _invalid(
                "取消必须填写理由（1~500 字符）。",
                REASON_REASON_REQUIRED,
                field="reason",
            )

    row = _lock_round(repo, project_id, round_id, expected_lock_version)
    before = _audit_shape(row)
    _require_edge(row, to_state)
    if (row.state, to_state) in ENDPOINT_FORBIDDEN_EDGES:
        raise _conflict(
            "REVIEWING -> GENERATING 只能由淘汰补产在同事务内推进。",
            REASON_ROUND_TRANSITION_ILLEGAL,
            **{"from": row.state, "to": to_state, "via": "replacements"},
        )

    if row.state == ROUND_STATE_PLANNED and to_state == ROUND_STATE_GENERATING:
        facts = gather_entry_facts(
            repo, project_id=project_id, plan_snapshot=row.plan_snapshot
        )
        require_entry(facts)
        if facts.active_baseline_id != row.baseline_id:
            raise _conflict(
                "项目生效基线已不是本轮钉住的基线。",
                REASON_PLAN_BASELINE_STALE,
                pinnedBaselineId=str(row.baseline_id),
                activeBaselineId=str(facts.active_baseline_id),
            )
    if row.state == ROUND_STATE_GENERATING and to_state == ROUND_STATE_REVIEWING:
        unsettled = [
            str(f.slot_id)
            for f in live_facts(repo.slot_facts(row.id))
            if f.content_item_id is None or f.content_state not in CONTENT_SETTLED_STATES
        ]
        if unsettled:
            raise _conflict(
                "仍有名额未绑定内容或内容未落定。",
                REASON_ROUND_NOT_SETTLED,
                slotIds=unsettled,
            )

    if (
        repo.update_round_state(
            round_id=row.id,
            expected_lock_version=expected_lock_version,
            state=to_state,
            cancel_reason=cancel_reason,
        )
        == 0
    ):
        raise _version_conflict(_reload(repo, row.id))
    repo.flush()
    fresh = _reload(repo, row.id)
    _audit(
        repo,
        action=AUDIT_ACTION_TRANSITION,
        row=fresh,
        actor_id=actor_id,
        request_id=request_id,
        before=before,
        after=_audit_shape(fresh),
        reason=cancel_reason,
    )
    return fresh


# ---------------------------------------------------------------------------
# 绑定内容
# ---------------------------------------------------------------------------


def bind_slot_content(
    repo: TrialRepository,
    *,
    project_id: uuid.UUID,
    round_id: uuid.UUID,
    slot_id: uuid.UUID,
    content_item_id: uuid.UUID,
) -> tuple[Any, bool]:
    """把已生成的内容绑到名额。返回 `(名额, changed)`；同内容重放 `changed=False`。"""
    require_round(repo, project_id, round_id)
    row = repo.get_round_for_update(round_id)
    assert row is not None
    slot = repo.get_slot(slot_id)
    if slot is None or slot.round_id != round_id:
        raise _not_found(
            "名额不存在。", REASON_SLOT_NOT_FOUND, resourceId=str(slot_id)
        )
    if slot.content_item_id is not None:
        if slot.content_item_id == content_item_id:
            return slot, False
        raise _conflict(
            "名额已绑定了另一条内容。",
            REASON_SLOT_ALREADY_BOUND,
            contentItemId=str(slot.content_item_id),
        )
    if row.state != ROUND_STATE_GENERATING:
        raise _conflict(
            "只有 GENERATING 轮可以绑定内容。",
            REASON_ROUND_NOT_GENERATING,
            state=row.state,
        )
    if repo.slot_replaced_by(slot_id) is not None:
        raise _conflict(
            "该名额已被替代。", REASON_SLOT_ALREADY_REPLACED, slotId=str(slot_id)
        )
    content = repo.content_row(content_item_id)
    if content is None:
        raise _not_found(
            "内容不存在。", REASON_CONTENT_NOT_FOUND, resourceId=str(content_item_id)
        )
    if content["project_id"] != project_id:
        raise _invalid(
            "内容不属于本项目。", REASON_CONTENT_PROJECT_MISMATCH, field="contentItemId"
        )
    request = content["generation_request"]
    batch_kind = request.get(GENERATION_REQUEST_KEY_BATCH_KIND) if isinstance(request, dict) else None
    if batch_kind != BATCH_KIND_TRIAL:
        raise _invalid(
            "内容的 generation_request.batchKind 不是 TRIAL（缺失即拒绝）。",
            REASON_CONTENT_BATCH_KIND_MISMATCH,
            field="contentItemId",
            batchKind=batch_kind,
        )
    for mine, theirs, name in (
        (slot.scene_key, content["scene_key"], "sceneKey"),
        (slot.form_key, content["form_key"], "formKey"),
    ):
        if mine is not None and theirs is not None and mine != theirs:
            raise _invalid(
                f"内容的 {name} 与名额不一致。",
                REASON_CONTENT_SCENE_MISMATCH,
                field="contentItemId",
            )
    holder = repo.slot_holding_content(content_item_id)
    if holder is not None:
        raise _conflict(
            "该内容已绑定到另一个名额。",
            REASON_CONTENT_ALREADY_BOUND,
            slotId=str(holder),
        )
    try:
        with repo.savepoint():
            changed = repo.bind_slot_content(
                slot_id=slot_id, content_item_id=content_item_id
            )
            repo.flush()
    except IntegrityError as exc:
        raise _conflict(
            "该内容已绑定到另一个名额。", REASON_CONTENT_ALREADY_BOUND
        ) from exc
    if changed == 0:
        raise _conflict("名额已绑定了另一条内容。", REASON_SLOT_ALREADY_BOUND)
    return repo.get_slot(slot_id), True


# ---------------------------------------------------------------------------
# 淘汰补产
# ---------------------------------------------------------------------------


def create_replacement(
    repo: TrialRepository,
    *,
    project_id: uuid.UUID,
    round_id: uuid.UUID,
    slot_id: uuid.UUID,
    expected_lock_version: int,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> tuple[Any, Any]:
    """为被淘汰的名额建替代名额（方向不变）。返回 `(新名额, 轮)`。"""
    row = _lock_round(repo, project_id, round_id, expected_lock_version)
    before = _audit_shape(row)
    if row.state in ROUND_TERMINAL_STATES:
        raise _conflict(
            f"轮已是终态 {row.state}，不能再补产。",
            REASON_ROUND_TERMINAL,
            **{"from": row.state},
        )
    slot = repo.get_slot(slot_id)
    if slot is None or slot.round_id != round_id:
        raise _not_found(
            "名额不存在。", REASON_SLOT_NOT_FOUND, resourceId=str(slot_id)
        )
    fact = next(f for f in repo.slot_facts(round_id) if f.slot_id == slot_id)
    status = derive_slot_status(fact)
    if status == SLOT_STATUS_REPLACED:
        raise _conflict(
            "该名额已有替代名额。", REASON_SLOT_ALREADY_REPLACED, slotId=str(slot_id)
        )
    if status != SLOT_STATUS_ELIMINATED:
        raise _conflict(
            "只有被淘汰的名额才能补产。",
            REASON_SLOT_NOT_ELIMINATED,
            slotStatus=status,
        )
    if repo.count_replacement_slots(round_id) + 1 > row.planned_quantity:
        raise _conflict(
            "补产次数已达本轮计划数量上限。",
            REASON_REPLACEMENT_CAP_REACHED,
            plannedQuantity=row.planned_quantity,
        )
    try:
        with repo.savepoint():
            new_slot = repo.insert_slot(
                round_id=round_id,
                slot_no=repo.max_slot_no(round_id) + 1,
                scene_key=slot.scene_key,
                form_key=slot.form_key,
                replaces_slot_id=slot_id,
            )
    except IntegrityError as exc:
        raise _conflict(
            "该名额已有替代名额。", REASON_SLOT_ALREADY_REPLACED, slotId=str(slot_id)
        ) from exc
    if row.state == ROUND_STATE_REVIEWING:
        updated = repo.update_round_state(
            round_id=row.id,
            expected_lock_version=expected_lock_version,
            state=ROUND_STATE_GENERATING,
        )
    else:
        updated = repo.touch_round(
            round_id=row.id, expected_lock_version=expected_lock_version
        )
    if updated == 0:
        raise _version_conflict(_reload(repo, row.id))
    repo.flush()
    fresh = _reload(repo, row.id)
    _audit(
        repo,
        action=AUDIT_ACTION_REPLACE,
        row=fresh,
        actor_id=actor_id,
        request_id=request_id,
        before=before,
        after={**_audit_shape(fresh), "replacedSlotId": str(slot_id)},
    )
    return new_slot, fresh


# ---------------------------------------------------------------------------
# 批准进入扩产
# ---------------------------------------------------------------------------


def approve_gate(
    repo: TrialRepository,
    *,
    project_id: uuid.UUID,
    round_id: uuid.UUID,
    expected_lock_version: int,
    approved_scene_keys: Sequence[str],
    reason: str,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> tuple[Any, Any]:
    """人工批准：写决定 + 轮 -> GATE_PASSED（同事务）。返回 `(轮, 决定)`。"""
    text_reason = (reason or "").strip()
    if not text_reason or len(text_reason) > MAX_REASON_CHARS:
        raise _invalid(
            "批准理由必填（1~500 字符）。", REASON_REASON_REQUIRED, field="reason"
        )
    keys = _normalize_keys(approved_scene_keys)
    if not keys:
        raise _invalid(
            "approvedSceneKeys 不能为空。",
            REASON_SCENE_KEYS_REQUIRED,
            field="approvedSceneKeys",
        )

    row = _lock_round(repo, project_id, round_id, expected_lock_version)
    before = _audit_shape(row)
    if row.state != ROUND_STATE_REVIEWING:
        _require_edge(row, ROUND_STATE_GATE_PASSED)  # 终态 / 非法边 -> 409
    # 先锁内容（挡并发改稿），再读事实。
    repo.lock_contents_shared(
        [s.content_item_id for s in repo.list_slots(row.id) if s.content_item_id]
    )
    facts = repo.slot_facts(row.id)
    round_scenes = {f.scene_key for f in facts}
    unknown = [k for k in keys if k not in round_scenes]
    if unknown:
        raise _invalid(
            "approvedSceneKeys 含不属于本轮的方向。",
            REASON_SCENE_KEY_UNKNOWN,
            field="approvedSceneKeys",
            sceneKeys=unknown,
        )
    evaluation = evaluate_gate(facts, keys)
    conditions = evaluation["conditions"]
    failed = [
        c
        for c in (GATE_ROUND_ALL_DISPOSED, GATE_NO_OPEN_BLOCKING_FEEDBACK)
        if not conditions[c]["satisfied"]
    ]
    if failed:
        raise _conflict(
            "关口硬条件未满足。",
            REASON_GATE_NOT_SATISFIED,
            conditions=failed,
        )
    unverified = conditions[GATE_DIRECTION_HAS_APPROVED_SAMPLE]["detail"][
        "unverifiedSceneKeys"
    ]
    if unverified:
        raise ApiError(
            ErrorCode.DIRECTION_UNVERIFIED,
            "所选方向缺少已通过的样例。",
            details={"reason": REASON_DIRECTION_UNVERIFIED, "sceneKeys": unverified},
        )

    live = live_facts(facts)
    basis = [
        {
            "slotId": str(f.slot_id),
            "sceneKey": f.scene_key,
            "contentItemId": str(f.content_item_id),
            "subjectVersionId": None
            if f.current_version_id is None
            else str(f.current_version_id),
            "reviewId": None if f.review_id is None else str(f.review_id),
            "status": derive_slot_status(f),
        }
        for f in live
        if f.content_item_id is not None
    ]
    decision = repo.insert_decision(
        round_id=row.id,
        approved_scene_keys=keys,
        condition_results=conditions,
        statistics=compute_statistics(facts),
        basis=basis,
        reason=text_reason,
        decided_by=actor_id,
    )
    if (
        repo.update_round_state(
            round_id=row.id,
            expected_lock_version=expected_lock_version,
            state=ROUND_STATE_GATE_PASSED,
        )
        == 0
    ):
        raise _version_conflict(_reload(repo, row.id))
    repo.flush()
    fresh = _reload(repo, row.id)
    _audit(
        repo,
        action=AUDIT_ACTION_GATE,
        row=fresh,
        actor_id=actor_id,
        request_id=request_id,
        before=before,
        after={**_audit_shape(fresh), "approvedSceneKeys": keys},
        reason=text_reason,
    )
    return fresh, decision


def slot_view(repo: TrialRepository, round_row: Any, slot_id: uuid.UUID) -> dict[str, Any]:
    """单个名额的响应体（含派生状态）。"""
    return next(b for b in _slot_bodies(repo, round_row) if b["id"] == slot_id)
