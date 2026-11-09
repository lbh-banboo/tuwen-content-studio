"""WP32 扩产 / 批产的服务层（唯一业务入口）。

## 创建事务（顺序不可调换；全程**一个事务**）

| 步 | 动作 | 失败时 |
|---|---|---|
| 0 | 构造 `GenerationRequest`（归一 + 校验 scope）、`request_hash` | 422 |
| 1 | **重放前置**：按 `(project_id, idempotency_key)` 查批次，命中则分流并结束 | 409 / 200 |
| 2 | 事务级 advisory lock（同项目创建串行化，保护 `seq_no` 与 `allocated_so_far`）；锁后**再查一次**重放 | — |
| 3 | 档位只接受 SCALE / BULK；数量走 `batch.limits` 生效值 + `validate_quantity` | 422 |
| 4 | 前提校验：PLAN 节点属于本项目、是 PLAN、是链头，且与 `plan_drafts` 重算摘要一致；基线 = 生效基线 | 409 / 422 |
| 5 | **关口**（`gate_checker`，见 `types.py` 模块 docstring） | 409 / 503 |
| 6 | 分配：场景、形式各一次 `next_batch_allocation`；任一维度 `unallocated > 0` 拒绝 | 422 |
| 7 | 预估（每个 `task_kind` 一次，经 content 的 billing 桥）+ 可用额度预检；**不预占**（方案 B） | 422 |
| 8 | INSERT `batches`（SAVEPOINT 内；撞幂等键回到重放分流，撞序号重取 <= 3 次） | — |
| 9 | PLAN 快照节点：`register_version(PLAN, entity_id=batch.id, revision_no=1)` + 两条依赖边 | — |
| 10 | 物化内容：`start_generation(count=quantity, idempotency_key='batch:{id}')` | — |
| 11 | INSERT `batch_items`（`scene_key` / `form_key` 取自 content 行，与 `content_items` 一致） | — |
| 12 | 逐条 `enqueue_job(batch_id, content_id)` + INSERT `batch_item_runs(round_no=1)` | — |
| 13 | 审计 `batch.create` | — |

**不存在“批次建了一半”的可见状态**：任何一步抛异常，`session_scope` 整体回滚。

## 批次层不预占

WP26 的 `execute_generation` 已在逐条执行时预估 + 预占 + 结算；批次层再预占一整批会双占（OP-231）。
本包只做预估与**建议性**可用额度预检（两个并发批次可能都通过预检，真正的闸在逐条 `reserve_budget`）。

## 写入的单一出口

所有写 `batches` 的 UPDATE 都在 `repository.py` 的 `cas_*`；本文件不拼 UPDATE。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rednote.modules.batches import (
    baseline_bridge,
    billing_bridge,
    content_bridge,
    jobs_bridge,
    planning_bridge,
    versions_bridge,
)
from rednote.modules.batches.audit_bridge import audit
from rednote.modules.batches.models import Batch
from rednote.modules.batches.progress import Progress, rollup
from rednote.modules.batches.repository import (
    CONSTRAINT_IDEMPOTENCY,
    BatchInsertConflict,
    BatchRepository,
)
from rednote.modules.batches.types import (
    AUDIT_ACTION_CANCEL,
    AUDIT_ACTION_CREATE,
    AUDIT_ACTION_RETRY,
    AUDIT_OBJECT_TYPE,
    BASELINE_STATE_CURRENT,
    BASELINE_STATE_SUPERSEDED,
    BASELINE_STATE_UNKNOWN,
    BATCH_KIND_BULK,
    BATCH_KIND_SCALE,
    BATCH_STATE_CANCEL_REQUESTED,
    BATCH_STATE_SUBMITTED,
    CREATABLE_BATCH_KINDS,
    IDEMPOTENCY_SCOPE_BATCH_RETRY_PREFIX,
    MAX_IDEMPOTENCY_KEY_CHARS,
    MAX_SEQ_ATTEMPTS,
    PURPOSE_BATCH_BASELINE,
    PURPOSE_BATCH_PLAN,
    REASON_ALLOCATION_INCOMPLETE,
    REASON_BASELINE_CHANGED,
    REASON_BASELINE_NOT_READY,
    REASON_BATCH_BUDGET_NOT_CONFIGURED,
    REASON_BATCH_CANCELLED,
    REASON_BATCH_ESTIMATE_EXCEEDS_AVAILABLE,
    REASON_BATCH_KIND_UNKNOWN,
    REASON_BATCH_NOT_FOUND,
    REASON_GATE_NOT_SATISFIED,
    REASON_GATE_NOT_WIRED,
    REASON_IDEMPOTENCY_KEY_REQUIRED,
    REASON_IDEMPOTENCY_KEY_REUSED,
    REASON_LIMITS_NOT_CONFIGURED,
    REASON_NOTHING_TO_RETRY,
    REASON_PLAN_CHANGED,
    REASON_PLAN_VERSION_INVALID,
    REASON_REQUEST_IN_PROGRESS,
    SNAPSHOT_REVISION_NO,
    CreateBatchCommand,
    GateChecker,
    GateVerdict,
    content_idempotency_key,
    job_idempotency_key,
)
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

_KIND_CODE = {BATCH_KIND_SCALE: "S", BATCH_KIND_BULK: "B", "TRIAL": "T"}
_DIMENSION_KEY = {
    planning_bridge.DIMENSION_SCENE: "scenes",
    planning_bridge.DIMENSION_FORM: "forms",
}
_CONTENT_ERROR_FIELDS = {
    "MODEL_PROFILE_NOT_FOUND": "modelProfileId",
    "GENERATION_REQUEST_INVALID": "scope",
}


# ---------------------------------------------------------------------------
# 错误工厂
# ---------------------------------------------------------------------------


def _validation_error(field: str, message: str, *, reason: str, **extra: Any) -> ApiError:
    details: dict[str, Any] = {
        "fieldErrors": [{"field": field, "type": reason, "message": message}],
        "reason": reason,
    }
    details.update(extra)
    return ApiError(ErrorCode.VALIDATION_FAILED, message, details=details)


def _conflict(message: str, *, reason: str, **extra: Any) -> ApiError:
    details: dict[str, Any] = {"reason": reason}
    details.update(extra)
    return ApiError(ErrorCode.INVALID_TRANSITION, message, details=details)


def not_found(batch_id: uuid.UUID) -> ApiError:
    return ApiError(
        ErrorCode.RESOURCE_NOT_FOUND,
        details={
            "reason": REASON_BATCH_NOT_FOUND,
            "resource": "batch",
            "resourceId": str(batch_id),
        },
    )


def _translate_content_error(exc: content_bridge.ContentGenerationError) -> ApiError:
    field = _CONTENT_ERROR_FIELDS.get(exc.reason)
    if field is None:
        raise exc
    return _validation_error(field, str(exc), reason=exc.reason.lower())


# ---------------------------------------------------------------------------
# 纯函数：请求摘要 / 分配快照
# ---------------------------------------------------------------------------


def build_request_hash(command: CreateBatchCommand, *, scope: str) -> str:
    """规范化请求摘要（键序无关；不含时间戳 / 操作人）。

    `baselineId` 取**请求里给的值**（省略为 `None`）：省略与显式等于当前基线是两个不同请求。
    `scope` 是归一后的（`TAGS` → `COPY`）。
    """
    return versions_bridge.canonical_payload_hash(
        {
            "batchKind": command.batch_kind,
            "quantity": command.quantity,
            "planVersionId": str(command.plan_version_id),
            "baselineId": None if command.baseline_id is None else str(command.baseline_id),
            "modelProfileId": str(command.model_profile_id),
            "scope": scope,
        }
    )


def _lines(result: Any) -> list[dict[str, Any]]:
    return [
        {
            "key": line.key,
            "weightPermille": line.weight_permille,
            "allocated": line.allocated,
            "capacity": line.capacity,
            "sortOrder": line.sort_order,
        }
        for line in result.lines
    ]


def build_allocation_snapshot(
    *,
    batch_kind: str,
    quantity: int,
    plan_version_id: uuid.UUID,
    baseline_id: uuid.UUID,
    scene_result: Any,
    form_result: Any,
) -> dict[str, Any]:
    """本批分配快照 = `batches.allocation` = PLAN 快照节点 payload（**同一个函数产出同一份内容**）。

    纯函数、确定性：不含时间戳 / 请求 ID / 操作人，否则同一批次重放会得到不同摘要，
    触发 `register_version` 的 409 `revision_payload_mismatch`。
    """
    return {
        "schemaVersion": 1,
        "batchKind": batch_kind,
        "quantity": quantity,
        "planVersionId": str(plan_version_id),
        "baselineId": str(baseline_id),
        "scenes": _lines(scene_result),
        "forms": _lines(form_result),
        "unallocated": {
            "scene": scene_result.unallocated,
            "form": form_result.unallocated,
        },
    }


def display_code(batch_kind: str, seq_no: int) -> str:
    """展示码 `BATCH-S01`（读时拼，不存；序号 >99 时自然变宽）。"""
    return f"BATCH-{_KIND_CODE.get(batch_kind, '?')}{seq_no:02d}"


# ---------------------------------------------------------------------------
# 创建 / 预览的共同前置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Prepared:
    generation_request: Any
    limit_applied: int
    limit_source: str
    plan_draft: Any
    baseline_id: uuid.UUID
    scene_result: Any
    form_result: Any
    snapshot: dict[str, Any]


def _require_key(idempotency_key: str | None) -> str:
    key = (idempotency_key or "").strip()
    if not key:
        raise _validation_error(
            "Idempotency-Key",
            "创建批次必须携带 Idempotency-Key（批次会花钱，不幂等不应是可选项）。",
            reason=REASON_IDEMPOTENCY_KEY_REQUIRED,
        )
    if len(key) > MAX_IDEMPOTENCY_KEY_CHARS:
        raise _validation_error(
            "Idempotency-Key",
            f"Idempotency-Key 不得超过 {MAX_IDEMPOTENCY_KEY_CHARS} 个字符。",
            reason=REASON_IDEMPOTENCY_KEY_REQUIRED,
        )
    return key


def _build_generation_request(command: CreateBatchCommand) -> Any:
    if command.batch_kind not in CREATABLE_BATCH_KINDS:
        raise _validation_error(
            "batchKind",
            f"批次档位只能是 {sorted(CREATABLE_BATCH_KINDS)}，收到 {command.batch_kind!r}。",
            reason=REASON_BATCH_KIND_UNKNOWN,
        )
    try:
        return content_bridge.build_generation_request(
            scope=command.scope,
            batch_kind=command.batch_kind,
            model_profile_id=command.model_profile_id,
        )
    except ValueError as exc:
        raise _validation_error("scope", str(exc), reason="generation_request_invalid") from exc


def _check_quantity(repo: BatchRepository, project_id: uuid.UUID, command: CreateBatchCommand) -> tuple[int, str]:
    """数量校验：返回创建时刻的 `(limit_applied, limit_source)`。未配置不兜底默认值。"""
    setting = planning_bridge.read_batch_limits(
        repo.session, project_id=project_id, materialize=True
    )
    try:
        limits = setting.as_limits()
    except ValueError as exc:
        raise _validation_error(
            "quantity",
            f"批量数量上限尚未配置：{exc}",
            reason=REASON_LIMITS_NOT_CONFIGURED,
            projectId=str(project_id),
        ) from exc
    try:
        planning_bridge.validate_quantity(
            limits, command.batch_kind, command.quantity, source=setting.source
        )
    except planning_bridge.QuantityOutOfRange as exc:
        raise _validation_error(
            "quantity",
            str(exc),
            reason=exc.reason.lower(),
            batchKind=exc.batch_kind,
            quantity=exc.quantity,
            limit=exc.limit,
            source=exc.source,
        ) from exc
    # 与 validate_quantity 同口径的生效上界（配置值被写成天文数字时按 MAX_QUANTITY_BOUND 截断）。
    applied = min(limits.max_for(command.batch_kind), planning_bridge.MAX_QUANTITY_BOUND)
    return applied, str(setting.source)


def _check_premise(
    repo: BatchRepository, project_id: uuid.UUID, command: CreateBatchCommand
) -> tuple[Any, uuid.UUID]:
    """前提校验（“不能先通过后补查”）：返回 `(plan_draft, 生效基线 id)`。"""
    session = repo.session
    node = versions_bridge.get_node(session, command.plan_version_id)
    if (
        node is None
        or node.entity_type != versions_bridge.ENTITY_TYPE_PLAN
        or node.project_id != project_id
        or node.entity_id != project_id
    ):
        raise _validation_error(
            "planVersionId",
            "planVersionId 必须是本项目的 PLAN 版本节点。",
            reason=REASON_PLAN_VERSION_INVALID,
        )
    head = versions_bridge.plan_chain_head_revision(session, project_id=project_id)
    if node.revision_no != head:
        raise _conflict(
            "该方案版本已不是最新（方案被重新提交过），请基于最新方案重新建批。",
            reason=REASON_PLAN_CHANGED,
            planVersionId=str(node.id),
            providedRevisionNo=node.revision_no,
            currentRevisionNo=head,
        )

    active = baseline_bridge.read_active_baseline_id(session, project_id=project_id)
    if active is None:
        raise _conflict("项目当前没有生效基线，不能建批。", reason=REASON_BASELINE_NOT_READY)
    if command.baseline_id is not None and command.baseline_id != active:
        raise _conflict(
            "请求里的基线已不是当前生效基线。",
            reason=REASON_BASELINE_CHANGED,
            providedBaselineId=str(command.baseline_id),
            currentBaselineId=str(active),
        )

    draft = planning_bridge.get_plan_draft(session, project_id=project_id)
    if draft is None:
        raise _conflict("项目没有计划草稿，无法延续配比。", reason=REASON_PLAN_CHANGED)
    # PLAN 节点只存摘要不存载荷：用草稿重算摘要，与钉住的节点比对。
    recomputed = versions_bridge.canonical_payload_hash(
        planning_bridge.draft_plan_payload(draft)
    )
    if recomputed != node.payload_hash:
        raise _conflict(
            "计划草稿在方案提交后被改动，与钉住的方案版本不一致。",
            reason=REASON_PLAN_CHANGED,
            planVersionId=str(node.id),
        )
    if draft.baseline_id is not None and draft.baseline_id != active:
        raise _conflict(
            "方案所基于的基线已不是当前生效基线。",
            reason=REASON_BASELINE_CHANGED,
            planBaselineId=str(draft.baseline_id),
            currentBaselineId=str(active),
        )
    return draft, active


def _gate_failure(verdict: GateVerdict) -> ApiError:
    return _conflict(
        "试产关口未通过，不能开扩产 / 批产。",
        reason=REASON_GATE_NOT_SATISFIED,
        decisionId=None if verdict.decision_id is None else str(verdict.decision_id),
        decidedAt=None if verdict.decided_at is None else verdict.decided_at.isoformat(),
        gateReason=verdict.reason,
    )


def _check_gate(gate_checker: GateChecker, project_id: uuid.UUID) -> GateVerdict:
    verdict = gate_checker(project_id)
    if not verdict.passed:
        raise _gate_failure(verdict)
    return verdict


def _allocate(
    repo: BatchRepository,
    *,
    project_id: uuid.UUID,
    command: CreateBatchCommand,
    draft: Any,
    baseline_id: uuid.UUID,
) -> tuple[Any, Any, dict[str, Any]]:
    results = {}
    for dimension, key in _DIMENSION_KEY.items():
        so_far = repo.allocated_so_far(
            project_id=project_id,
            plan_version_id=command.plan_version_id,
            dimension=key,
        )
        try:
            results[dimension] = planning_bridge.allocate_batch(
                draft, dimension, batch_total=command.quantity, allocated_so_far=so_far
            )
        except ValueError as exc:  # 累计配额已越界（allocate_from_remaining 抛）
            raise _validation_error(
                "quantity", str(exc), reason=REASON_ALLOCATION_INCOMPLETE, dimension=key
            ) from exc
        if results[dimension].unallocated > 0:
            raise _validation_error(
                "quantity",
                f"{key} 维度有 {results[dimension].unallocated} 条无法分配（容量不足）；"
                "批次承诺的是 quantity 条，不静默少产。",
                reason=REASON_ALLOCATION_INCOMPLETE,
                dimension=key,
                unallocated=results[dimension].unallocated,
            )
    scene_result = results[planning_bridge.DIMENSION_SCENE]
    form_result = results[planning_bridge.DIMENSION_FORM]
    snapshot = build_allocation_snapshot(
        batch_kind=command.batch_kind,
        quantity=command.quantity,
        plan_version_id=command.plan_version_id,
        baseline_id=baseline_id,
        scene_result=scene_result,
        form_result=form_result,
    )
    return scene_result, form_result, snapshot


def _prepare(
    repo: BatchRepository,
    *,
    project_id: uuid.UUID,
    command: CreateBatchCommand,
    gate_checker: GateChecker | None,
) -> _Prepared:
    """步 0、3、4、(5)、6。`gate_checker=None` 表示不在此处判关口（预览单独处理）。"""
    generation_request = _build_generation_request(command)
    limit_applied, limit_source = _check_quantity(repo, project_id, command)
    draft, baseline_id = _check_premise(repo, project_id, command)
    if gate_checker is not None:
        _check_gate(gate_checker, project_id)
    scene_result, form_result, snapshot = _allocate(
        repo,
        project_id=project_id,
        command=command,
        draft=draft,
        baseline_id=baseline_id,
    )
    return _Prepared(
        generation_request=generation_request,
        limit_applied=limit_applied,
        limit_source=limit_source,
        plan_draft=draft,
        baseline_id=baseline_id,
        scene_result=scene_result,
        form_result=form_result,
        snapshot=snapshot,
    )


@dataclass(frozen=True)
class _Estimates:
    refs: list[dict[str, str]]
    lines: list[dict[str, Any]]
    total: Any
    currency: str


def _estimate(
    repo: BatchRepository,
    *,
    project_id: uuid.UUID,
    command: CreateBatchCommand,
    prepared: _Prepared,
    actor_id: uuid.UUID | None,
    request_id: str | None,
) -> _Estimates:
    """预估：每个 `task_kind` 各一次（数量口径 `calls_for` 在 content 桥内）。**不预占**。"""
    try:
        provider_id = content_bridge.provider_id_of(
            repo.session, model_profile_id=command.model_profile_id
        )
    except content_bridge.ContentGenerationError as exc:
        raise _translate_content_error(exc) from exc
    task_kinds = content_bridge.task_kinds_for_scope(prepared.generation_request.scope)
    views = [
        billing_bridge.estimate_for_task_kind(
            repo.session,
            project_id=project_id,
            task_kind=task_kind,
            content_count=command.quantity,
            batch_kind=command.batch_kind,
            provider_id=provider_id,
            model_profile_id=command.model_profile_id,
            actor_id=actor_id,
            request_id=request_id,
        )
        for task_kind in task_kinds
    ]
    currencies = {view.currency for view in views}
    if len(currencies) != 1:  # pragma: no cover - 同一 provider 同一币种
        raise _validation_error("modelProfileId", "预估币种不一致。", reason="currency_mismatch")
    return _Estimates(
        refs=[
            {"taskKind": kind, "estimateId": str(view.id)}
            for kind, view in zip(task_kinds, views, strict=True)
        ],
        lines=[
            {
                "taskKind": kind,
                "amount": billing_bridge.amount_to_wire(view.total_amount),
            }
            for kind, view in zip(task_kinds, views, strict=True)
        ],
        total=billing_bridge.sum_amounts([view.total_amount for view in views]),
        currency=currencies.pop(),
    )


def _budget_precheck(
    repo: BatchRepository,
    *,
    project_id: uuid.UUID,
    estimates: _Estimates,
    can_view_cost: bool,
) -> Any:
    """可用额度预检（建议性）。未配置预算 ≠ 可用额度 0：分开报。"""
    budget = billing_bridge.read_budget(repo.session, project_id=project_id)
    if not budget.configured:
        raise ApiError(
            ErrorCode.BUDGET_EXCEEDED,
            "项目尚未配置预算，批次内的任务逐条预占时都会被拒绝。",
            details={
                "reason": REASON_BATCH_BUDGET_NOT_CONFIGURED,
                "budgetConfigured": False,
            },
        )
    if budget.currency != estimates.currency:
        raise _validation_error(
            "modelProfileId",
            f"预算币种 {budget.currency} 与预估币种 {estimates.currency} 不一致。",
            reason="currency_mismatch",
        )
    if billing_bridge.compare_amounts(estimates.total, budget.available) > 0:
        details: dict[str, Any] = {
            "reason": REASON_BATCH_ESTIMATE_EXCEEDS_AVAILABLE,
            "budgetConfigured": True,
        }
        if can_view_cost:
            details["estimatedAmount"] = billing_bridge.amount_to_wire(estimates.total)
            details["available"] = billing_bridge.amount_to_wire(budget.available)
            details["currency"] = estimates.currency
        raise ApiError(
            ErrorCode.BUDGET_EXCEEDED,
            "批次预估金额超过项目当前可用额度。",
            details=details,
        )
    return budget


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateResult:
    batch: Batch
    replayed: bool


def _replay(existing: Batch, request_hash: str) -> CreateResult:
    if existing.request_hash != request_hash:
        raise ApiError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "相同幂等键对应了不同的请求内容。",
            details={
                "reason": REASON_IDEMPOTENCY_KEY_REUSED,
                "idempotencyKey": existing.idempotency_key,
                "existingBatchId": str(existing.id),
            },
        )
    return CreateResult(batch=existing, replayed=True)


def create_batch(
    repo: BatchRepository,
    *,
    project_id: uuid.UUID,
    command: CreateBatchCommand,
    idempotency_key: str | None,
    gate_checker: GateChecker,
    actor_id: uuid.UUID | None,
    request_id: str | None,
    can_view_cost: bool = True,
) -> CreateResult:
    """创建一个批次（见模块 docstring 的 13 步）。权限由 API 层判（`EDIT`）。"""
    key = _require_key(idempotency_key)
    generation_request = _build_generation_request(command)
    request_hash = build_request_hash(command, scope=generation_request.scope)

    existing = repo.find_by_idempotency(project_id=project_id, idempotency_key=key)
    if existing is not None:
        return _replay(existing, request_hash)

    repo.advisory_lock_project(project_id)
    existing = repo.find_by_idempotency(project_id=project_id, idempotency_key=key)
    if existing is not None:
        return _replay(existing, request_hash)

    prepared = _prepare(
        repo, project_id=project_id, command=command, gate_checker=gate_checker
    )
    estimates = _estimate(
        repo,
        project_id=project_id,
        command=command,
        prepared=prepared,
        actor_id=actor_id,
        request_id=request_id,
    )
    _budget_precheck(
        repo, project_id=project_id, estimates=estimates, can_view_cost=can_view_cost
    )

    batch: Batch | None = None
    for _attempt in range(MAX_SEQ_ATTEMPTS):
        try:
            batch = repo.insert_batch(
                project_id=project_id,
                batch_kind=command.batch_kind,
                seq_no=repo.next_seq_no(
                    project_id=project_id, batch_kind=command.batch_kind
                ),
                quantity=command.quantity,
                plan_version_id=command.plan_version_id,
                baseline_id=prepared.baseline_id,
                allocation=prepared.snapshot,
                limit_applied=prepared.limit_applied,
                limit_source=prepared.limit_source,
                estimated_amount=estimates.total,
                estimated_currency=estimates.currency,
                estimate_refs=estimates.refs,
                idempotency_key=key,
                request_hash=request_hash,
                created_by=actor_id,
            )
            break
        except BatchInsertConflict as conflict:
            if conflict.constraint == CONSTRAINT_IDEMPOTENCY:
                winner = repo.find_by_idempotency(
                    project_id=project_id, idempotency_key=key
                )
                if winner is None:  # pragma: no cover - 唯一冲突后必能回查
                    raise
                return _replay(winner, request_hash)
            # 序号冲突：下一轮重取。
    if batch is None:
        raise _conflict(
            "批次序号分配冲突，请重试。", reason="seq_no_conflict", batchKind=command.batch_kind
        )

    # 步 9：PLAN 快照节点（显式 revision_no=1）+ 依赖边（接缝冻结 §5.16 E10/E11）。
    snapshot_node = versions_bridge.register_snapshot(
        repo.session,
        batch_id=batch.id,
        project_id=project_id,
        payload=prepared.snapshot,
        created_by=actor_id,
        revision_no=SNAPSHOT_REVISION_NO,
    )
    versions_bridge.link(
        repo.session,
        parent_version_id=snapshot_node.node_id,
        dependency_version_id=command.plan_version_id,
        purpose=PURPOSE_BATCH_PLAN,
    )
    versions_bridge.link(
        repo.session,
        parent_version_id=snapshot_node.node_id,
        dependency_version_id=prepared.baseline_id,
        purpose=PURPOSE_BATCH_BASELINE,
    )

    # 步 10：物化内容（场景 / 形式经 §5.15 接缝传入；联合展开规则未定义，恒不传）。
    try:
        rows = content_bridge.materialize_contents(
            repo.session,
            project_id=project_id,
            request=prepared.generation_request,
            content_count=command.quantity,
            idempotency_key=content_idempotency_key(batch.id),
            created_by=actor_id,
        )
    except content_bridge.ContentGenerationError as exc:
        raise _translate_content_error(exc) from exc
    if len(rows) != command.quantity:  # pragma: no cover - 新批次 id 不可能命中旧行
        raise RuntimeError(f"物化内容 {len(rows)} 条，应为 {command.quantity} 条")

    # 步 11 + 12：成员、任务、第 1 轮。
    for ordinal, row in enumerate(rows, start=1):
        item = repo.insert_item(
            batch_id=batch.id,
            ordinal=ordinal,
            content_item_id=row.id,
            scene_key=row.scene_key,
            form_key=row.form_key,
        )
        _enqueue_round(
            repo,
            project_id=project_id,
            batch_id=batch.id,
            item_id=item.id,
            content_id=row.id,
            round_no=1,
            generation_payload=prepared.generation_request.as_payload(),
        )

    audit(
        repo.session,
        action=AUDIT_ACTION_CREATE,
        object_type=AUDIT_OBJECT_TYPE,
        object_id=batch.id,
        project_id=project_id,
        actor_id=actor_id,
        request_id=request_id,
        before={},
        after={
            "batchKind": batch.batch_kind,
            "seqNo": batch.seq_no,
            "quantity": batch.quantity,
            "planVersionId": str(batch.plan_version_id),
            "baselineId": str(batch.baseline_id),
            "limitApplied": batch.limit_applied,
            "limitSource": batch.limit_source,
        },
    )
    return CreateResult(batch=batch, replayed=False)


def _enqueue_round(
    repo: BatchRepository,
    *,
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    item_id: uuid.UUID,
    content_id: uuid.UUID,
    round_no: int,
    generation_payload: dict[str, Any],
) -> tuple[uuid.UUID, int]:
    input_hash = versions_bridge.canonical_payload_hash(
        {
            **generation_payload,
            "projectId": str(project_id),
            "batchId": str(batch_id),
            "round": round_no,
        }
    )
    job = jobs_bridge.enqueue_batch_job(
        repo.session,
        kind=content_bridge.KIND_CONTENT_GENERATE,
        queue=content_bridge.QUEUE_CONTENT,
        idempotency_scope=content_bridge.IDEMPOTENCY_SCOPE_CONTENT_GENERATE,
        idempotency_key=job_idempotency_key(content_id, batch_id, round_no),
        input_hash=input_hash,
        project_id=project_id,
        batch_id=batch_id,
        content_id=content_id,
    )
    repo.insert_run(batch_item_id=item_id, round_no=round_no, job_id=job.job_id)
    return job.job_id, round_no


# ---------------------------------------------------------------------------
# 预览（无状态；会写 cost_estimates —— 文档化的副作用）
# ---------------------------------------------------------------------------


def preview_batch(
    repo: BatchRepository,
    *,
    project_id: uuid.UUID,
    command: CreateBatchCommand,
    gate_checker: GateChecker,
    actor_id: uuid.UUID | None,
    request_id: str | None,
    can_view_cost: bool,
) -> dict[str, Any]:
    """预览：生效上限 + 分配 + 预估 + 预算 + 关口结论。**不落批次**。

    关口在预览里是“结论”而不是“拒绝”：未通过 `passed=false`；未接线 `passed=null`
    （缺失不填 False）。其余校验失败照常抛。
    """
    prepared = _prepare(
        repo, project_id=project_id, command=command, gate_checker=None
    )
    gate: dict[str, Any]
    try:
        verdict = gate_checker(project_id)
        gate = {
            "passed": verdict.passed,
            "decisionId": None if verdict.decision_id is None else str(verdict.decision_id),
            "decidedAt": None if verdict.decided_at is None else verdict.decided_at.isoformat(),
            "reason": verdict.reason,
        }
    except ApiError as exc:
        reason = (exc.details or {}).get("reason")
        if reason != REASON_GATE_NOT_WIRED:
            raise
        gate = {"passed": None, "decisionId": None, "decidedAt": None, "reason": reason}

    estimates = _estimate(
        repo,
        project_id=project_id,
        command=command,
        prepared=prepared,
        actor_id=actor_id,
        request_id=request_id,
    )
    budget = billing_bridge.read_budget(repo.session, project_id=project_id)
    show = can_view_cost
    return {
        "batchKind": command.batch_kind,
        "quantity": command.quantity,
        "limit": {"applied": prepared.limit_applied, "source": prepared.limit_source},
        "allocation": prepared.snapshot,
        "estimate": {
            "amount": billing_bridge.amount_to_wire(estimates.total) if show else None,
            "currency": estimates.currency if show else None,
            "isEstimate": True,
            "lines": estimates.lines if show else [],
        },
        "budget": {
            "configured": budget.configured,
            "currency": budget.currency if (show and budget.configured) else None,
            "available": (
                billing_bridge.amount_to_wire(budget.available)
                if (show and budget.configured)
                else None
            ),
        },
        "gate": gate,
    }


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


def require_batch(repo: BatchRepository, batch_id: uuid.UUID) -> Batch:
    batch = repo.get_batch(batch_id)
    if batch is None:
        raise not_found(batch_id)
    return batch


def batch_etag(batch: Batch) -> str:
    """ETag 载体的唯一读法：`build_etag(batch.id, batch.lock_version)`。"""
    return build_etag(batch.id, batch.lock_version)


def _baseline_state(repo: BatchRepository, batch: Batch) -> str:
    active = baseline_bridge.read_active_baseline_id(
        repo.session, project_id=batch.project_id
    )
    if active is None:
        return BASELINE_STATE_UNKNOWN
    return BASELINE_STATE_CURRENT if active == batch.baseline_id else BASELINE_STATE_SUPERSEDED


def _progress_body(progress: Progress) -> dict[str, Any]:
    return {
        "state": progress.state,
        "total": progress.total,
        "counts": dict(progress.counts),
        "needsReconciliation": progress.needs_reconciliation,
        "missing": progress.missing,
    }


def render_batch(
    batch: Batch,
    progress: Progress,
    *,
    baseline_state: str | None,
    can_view_cost: bool,
) -> dict[str, Any]:
    """批次的对外形状。无权看金额时金额字段为 `null`，**不写 0**。"""
    has_estimate = batch.estimated_amount is not None and can_view_cost
    return {
        "id": str(batch.id),
        "projectId": str(batch.project_id),
        "batchKind": batch.batch_kind,
        "seqNo": batch.seq_no,
        "displayCode": display_code(batch.batch_kind, batch.seq_no),
        "quantity": batch.quantity,
        "state": batch.state,
        "planVersionId": str(batch.plan_version_id),
        "baselineId": str(batch.baseline_id),
        "baselineState": baseline_state,
        "allocation": batch.allocation,
        "limit": {"applied": batch.limit_applied, "source": batch.limit_source},
        "estimate": {
            "amount": (
                billing_bridge.amount_to_wire(batch.estimated_amount) if has_estimate else None
            ),
            "currency": batch.estimated_currency if has_estimate else None,
            "isEstimate": True,
            "refs": list(batch.estimate_refs),
        },
        "reviewSummary": None,
        "progress": _progress_body(progress),
        "cancelRequestedAt": (
            None if batch.cancel_requested_at is None else batch.cancel_requested_at.isoformat()
        ),
        "createdBy": None if batch.created_by is None else str(batch.created_by),
        "createdAt": batch.created_at.isoformat(),
        "updatedAt": batch.updated_at.isoformat(),
        "etag": batch_etag(batch),
    }


def get_batch_detail(
    repo: BatchRepository, batch: Batch, *, can_view_cost: bool
) -> dict[str, Any]:
    """详情：纯读（不物化配置、不写库）。"""
    members = repo.list_members(batch_ids=[batch.id])
    progress = rollup(batch.state, [m["job_state"] for m in members])
    return render_batch(
        batch,
        progress,
        baseline_state=_baseline_state(repo, batch),
        can_view_cost=can_view_cost,
    )


@dataclass(frozen=True)
class Page:
    items: tuple[dict[str, Any], ...]
    page: PageInfo


def _decode_position(params: PageParams, spec: CursorSpec) -> CursorPosition | None:
    return decode_cursor(params.cursor, spec) if params.cursor else None


def _malformed_cursor() -> ApiError:
    return ApiError(
        ErrorCode.MALFORMED_REQUEST,
        details={"reason": "cursor_malformed", "field": "cursor"},
    )


def list_batches(
    repo: BatchRepository,
    *,
    project_id: uuid.UUID,
    batch_kind: str | None,
    state: str | None,
    page_params: PageParams,
    can_view_cost: bool,
) -> Page:
    """项目下的批次（新的在前，keyset 分页；纯读）。"""
    spec = CursorSpec(
        sort_fields=("createdAt", "id"),
        direction="desc",
        filters={"projectId": str(project_id), "batchKind": batch_kind, "state": state},
    )
    position = _decode_position(page_params, spec)
    before: tuple[datetime, uuid.UUID] | None = None
    if position is not None:
        try:
            before = (
                datetime.fromisoformat(str(position.sort_values[0])),
                uuid.UUID(position.item_id),
            )
        except (ValueError, TypeError, IndexError) as exc:
            raise _malformed_cursor() from exc
    rows = repo.list_batches(
        project_id=project_id,
        batch_kind=batch_kind,
        state=state,
        before=before,
        limit=page_params.limit + 1,
    )
    has_more = len(rows) > page_params.limit
    page_rows = rows[: page_params.limit]
    members = repo.list_members(batch_ids=[b.id for b in page_rows])
    states_by_batch: dict[uuid.UUID, list[str | None]] = {b.id: [] for b in page_rows}
    for member in members:
        states_by_batch[member["batch_id"]].append(member["job_state"])
    items = tuple(
        render_batch(
            batch,
            rollup(batch.state, states_by_batch[batch.id]),
            baseline_state=None,
            can_view_cost=can_view_cost,
        )
        for batch in page_rows
    )
    next_cursor: str | None = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(
            spec,
            CursorPosition(
                sort_values=(last.created_at.isoformat(), str(last.id)),
                item_id=str(last.id),
            ),
        )
    return Page(items=items, page=PageInfo(next_cursor=next_cursor, has_more=has_more))


def list_items(
    repo: BatchRepository, *, batch_id: uuid.UUID, page_params: PageParams
) -> Page:
    """成员分页（按 `ordinal` 升序）。`latestRound` / `jobState` 缺失为 `null`，不折成 0。"""
    spec = CursorSpec(
        sort_fields=("ordinal",),
        direction="asc",
        filters={"batchId": str(batch_id)},
    )
    position = _decode_position(page_params, spec)
    after: int | None = None
    if position is not None:
        try:
            after = int(position.sort_values[0])
        except (ValueError, TypeError, IndexError) as exc:
            raise _malformed_cursor() from exc
    rows = repo.list_members(
        batch_ids=[batch_id], after_ordinal=after, limit=page_params.limit + 1
    )
    has_more = len(rows) > page_params.limit
    page_rows = rows[: page_params.limit]
    items = tuple(
        {
            "ordinal": row["ordinal"],
            "contentId": str(row["content_item_id"]),
            "sceneKey": row["scene_key"],
            "formKey": row["form_key"],
            "latestRound": row["latest_round"],
            "jobId": None if row["job_id"] is None else str(row["job_id"]),
            "jobState": row["job_state"],
        }
        for row in page_rows
    )
    next_cursor: str | None = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(
            spec,
            CursorPosition(
                sort_values=(last["ordinal"],), item_id=str(last["item_id"])
            ),
        )
    return Page(items=items, page=PageInfo(next_cursor=next_cursor, has_more=has_more))


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


def _stale(repo: BatchRepository, batch: Batch, message: str) -> ApiError:
    """CAS 未命中：**重读当前 lock_version** 后抛 412（不得用旧值）。"""
    fresh = require_batch(repo, batch.id)
    return version_conflict(
        resource_id=fresh.id,
        current_version=fresh.lock_version,
        reason="lock_version_mismatch",
        message=message,
    )


def cancel_batch(
    repo: BatchRepository,
    *,
    batch: Batch,
    expected_lock_version: int,
    actor_id: uuid.UUID | None,
    request_id: str | None,
) -> tuple[Batch, bool]:
    """`SUBMITTED → CANCEL_REQUESTED`（不可恢复）。返回 `(批次, 本次是否真的改了)`。

    已是 `CANCEL_REQUESTED` ⇒ 幂等返回，不改任何列。对每个**非终态**的最新 job
    登记取消意图（终态 job 不动）。
    """
    if batch.state == BATCH_STATE_CANCEL_REQUESTED:
        return batch, False
    if repo.cas_cancel(
        batch_id=batch.id, expected_lock_version=expected_lock_version, actor_id=actor_id
    ) == 0:
        raise _stale(repo, batch, "批次已被更新，请重新读取最新版本后再取消。")
    for member in repo.list_members(batch_ids=[batch.id]):
        job_id, job_state = member["job_id"], member["job_state"]
        if job_id is not None and job_state not in jobs_bridge.JOB_TERMINAL_STATES:
            jobs_bridge.cancel_job(repo.session, job_id=job_id)
    fresh = require_batch(repo, batch.id)
    audit(
        repo.session,
        action=AUDIT_ACTION_CANCEL,
        object_type=AUDIT_OBJECT_TYPE,
        object_id=batch.id,
        project_id=batch.project_id,
        actor_id=actor_id,
        request_id=request_id,
        before={"state": BATCH_STATE_SUBMITTED},
        after={"state": BATCH_STATE_CANCEL_REQUESTED},
    )
    return fresh, True


# ---------------------------------------------------------------------------
# 失败项重做
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryResult:
    body: dict[str, Any]
    replayed: bool


def retry_failed(
    repo: BatchRepository,
    *,
    batch: Batch,
    idempotency_key: str | None,
    check_if_match: Any,
    actor_id: uuid.UUID | None,
    request_id: str | None,
) -> RetryResult:
    """重做最新一轮为 `FAILED` 的成员（追加下一轮；只此一种）。

    * **只重做 `FAILED`**：`WAITING_RECONCILIATION` / `WAITING_PROVIDER`（外部调用状态未知）、
      `PARTIAL`、`CANCELLED`、`SUCCEEDED` 一律不动；
    * 幂等：通用幂等记录 `scope='batch-retry:{batch_id}'`，`permanent=True`；重放回放原响应；
    * 并发：对批次 CAS（`lock_version` + `state='SUBMITTED'`），与取消、与其它重做互斥；
    * `check_if_match`：API 层传入的闭包 `(lock_version) -> None`，仅在“首次执行”路径调用
      （重放不要求 ETag 仍然新鲜：首次重做已把 `lock_version` 推进了）。
    """
    key = _require_key(idempotency_key)
    scope = f"{IDEMPOTENCY_SCOPE_BATCH_RETRY_PREFIX}{batch.id}"
    request_hash = versions_bridge.canonical_payload_hash({"batchId": str(batch.id)})
    decision = jobs_bridge.begin_request(
        repo.session, scope=scope, key=key, request_hash=request_hash
    )
    if decision.outcome == "CONFLICT":
        raise ApiError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "相同幂等键对应了不同的请求内容。",
            details={"reason": REASON_IDEMPOTENCY_KEY_REUSED, "idempotencyKey": key},
        )
    if decision.outcome == "REPLAY":
        stored = decision.record.response_json
        if stored is None:
            raise _conflict("同一幂等键的请求仍在处理中。", reason=REASON_REQUEST_IN_PROGRESS)
        return RetryResult(body=dict(stored), replayed=True)

    check_if_match(batch.lock_version)
    if batch.state == BATCH_STATE_CANCEL_REQUESTED:
        raise _conflict("批次已请求取消，不能重做。", reason=REASON_BATCH_CANCELLED)

    members = repo.list_members(batch_ids=[batch.id])
    candidates = [m for m in members if m["job_state"] == jobs_bridge.STATE_FAILED]
    if not candidates:
        raise _conflict(
            "没有可重做的成员（只重做最新一轮为 FAILED 的成员）。",
            reason=REASON_NOTHING_TO_RETRY,
        )
    if repo.cas_bump(batch_id=batch.id, expected_lock_version=batch.lock_version) == 0:
        raise _stale(repo, batch, "批次已被更新，请重新读取最新版本后再重做。")

    # 生成参数：与首轮同一份（取自成员对应内容的 generation_request，不由请求再给一份）。
    generation_payload = _generation_payload_of(repo, batch)
    retried: list[dict[str, Any]] = []
    for member in candidates:
        round_no = int(member["latest_round"]) + 1
        job_id, _ = _enqueue_round(
            repo,
            project_id=batch.project_id,
            batch_id=batch.id,
            item_id=member["item_id"],
            content_id=member["content_item_id"],
            round_no=round_no,
            generation_payload=generation_payload,
        )
        retried.append(
            {
                "contentId": str(member["content_item_id"]),
                "jobId": str(job_id),
                "roundNo": round_no,
            }
        )
    body = {
        "batchId": str(batch.id),
        "retriedCount": len(retried),
        "retried": retried,
    }
    jobs_bridge.complete_request(
        repo.session,
        scope=scope,
        key=key,
        response_status=202,
        response_json=body,
        resource_id=batch.id,
    )
    audit(
        repo.session,
        action=AUDIT_ACTION_RETRY,
        object_type=AUDIT_OBJECT_TYPE,
        object_id=batch.id,
        project_id=batch.project_id,
        actor_id=actor_id,
        request_id=request_id,
        before={"retriedCount": 0},
        after={"retriedCount": len(retried)},
    )
    return RetryResult(body=body, replayed=False)


def _generation_payload_of(repo: BatchRepository, batch: Batch) -> dict[str, Any]:
    """首轮用的生成参数（`content_items.generation_request`，取第一条成员）。"""
    from sqlalchemy import text

    row = repo.session.execute(
        text(
            "SELECT ci.generation_request FROM batch_items bi"
            "  JOIN content_items ci ON ci.id = bi.content_item_id"
            " WHERE bi.batch_id = :b ORDER BY bi.ordinal LIMIT 1"
        ),
        {"b": batch.id},
    ).first()
    return dict(row[0]) if row is not None and row[0] is not None else {}


__all__ = [
    "CreateResult",
    "Page",
    "RetryResult",
    "batch_etag",
    "build_allocation_snapshot",
    "build_request_hash",
    "cancel_batch",
    "create_batch",
    "display_code",
    "get_batch_detail",
    "list_batches",
    "list_items",
    "not_found",
    "preview_batch",
    "render_batch",
    "require_batch",
    "retry_failed",
]
