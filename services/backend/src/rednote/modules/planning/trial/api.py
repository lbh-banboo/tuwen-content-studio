"""WP31 试产关口端点（9 个）。

| # | 方法 路径 | 码 | 权限 |
|---|---|---|---|
| 1 | GET `/projects/{projectId}/trial-entry-check` | 200 | VIEW |
| 2 | POST `/projects/{projectId}/trial-rounds` | 201（幂等命中 200） | EDIT |
| 3 | GET `/projects/{projectId}/trial-rounds` | 200 | VIEW |
| 4 | GET `/projects/{projectId}/trial-rounds/{roundId}` | 200 | VIEW |
| 5 | POST `.../{roundId}/transitions` | 200 | EDIT，If-Match 必带 |
| 6 | PUT `.../{roundId}/slots/{slotId}/content` | 200 | EDIT（无 If-Match，行锁串行） |
| 7 | POST `.../{roundId}/slots/{slotId}/replacements` | 201 | EDIT，If-Match 必带 |
| 8 | GET `.../{roundId}/gate` | 200 | VIEW |
| 9 | POST `.../{roundId}/gate-decisions` | 201 | REVIEW，If-Match 必带 |

不新建 `ProjectAction`。错误码沿用协议封闭码，机器可判靠 `details.reason`。
异常由 service 抛 `ApiError`，全局处理器翻译，本文件不 catch。
412 在前（`require_if_match`）、409 在后（service 的业务状态判定）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Path, Query, Request, Response, status

from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.planning.trial import service
from rednote.modules.planning.trial.dependencies import (
    TrialPlanningRepoDep,
    TrialRepoDep,
)
from rednote.modules.planning.trial.schemas import (
    BindContentRequestBody,
    CreateRoundRequestBody,
    EntryCheckBody,
    GateBody,
    GateDecisionRequestBody,
    GateDecisionResultBody,
    ReplacementResultBody,
    RoundBody,
    RoundDetailBody,
    SlotBody,
    TransitionRequestBody,
)
from rednote.modules.projects.dependencies import ProjectAccessDep
from rednote.modules.projects.permissions import ProjectAction
from rednote.platform.api_responses import READ_RESPONSES, WRITE_RESPONSES
from rednote.platform.concurrency import require_if_match
from rednote.platform.errors import DataEnvelope, get_request_id
from rednote.platform.pagination import (
    PageParamsDep,
    PaginatedEnvelope,
    list_response,
    single_response,
)

router = APIRouter(tags=["planning-trial"])

RoundIdPath = Annotated[uuid.UUID, Path(alias="roundId")]
SlotIdPath = Annotated[uuid.UUID, Path(alias="slotId")]


def _if_match(request: Request, row: Any) -> None:
    require_if_match(
        request,
        resource_id=row.id,
        current_version=row.lock_version,
        resource_label="试产轮",
    )


@router.get(
    "/projects/{projectId}/trial-entry-check",
    response_model=DataEnvelope[EntryCheckBody],
    responses=READ_RESPONSES,
    summary="试产入口预检（纯读，逐项条件）",
)
def get_trial_entry_check(
    access: ProjectAccessDep,
    repo: TrialRepoDep,
    planning_repo: TrialPlanningRepoDep,
    plan_version_id: Annotated[uuid.UUID | None, Query(alias="planVersionId")] = None,
) -> dict[str, Any]:
    access.require(ProjectAction.VIEW)
    return single_response(
        service.entry_check(
            repo,
            planning_repo,
            project_id=access.project_id,
            plan_version_id=plan_version_id,
        )
    )


@router.post(
    "/projects/{projectId}/trial-rounds",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[RoundDetailBody],
    responses=WRITE_RESPONSES,
    summary="创建试产轮 / 补充试产轮（Idempotency-Key 可选）",
)
def create_trial_round(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: TrialRepoDep,
    planning_repo: TrialPlanningRepoDep,
    body: CreateRoundRequestBody,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    access.require(ProjectAction.EDIT)
    row, created = service.create_round(
        repo,
        planning_repo,
        project_id=access.project_id,
        kind=body.kind,
        plan_version_id=body.plan_version_id,
        quantity=body.quantity,
        source_round_id=body.source_round_id,
        scene_keys=body.scene_keys,
        idempotency_key=idempotency_key,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return single_response(service.get_round_detail(repo, access.project_id, row.id))


@router.get(
    "/projects/{projectId}/trial-rounds",
    response_model=PaginatedEnvelope[RoundBody],
    responses=READ_RESPONSES,
    summary="试产轮列表（游标分页，每轮带汇总统计）",
)
def list_trial_rounds(
    access: ProjectAccessDep, repo: TrialRepoDep, params: PageParamsDep
) -> dict[str, Any]:
    access.require(ProjectAction.VIEW)
    items, page = service.list_rounds(
        repo, project_id=access.project_id, page_params=params
    )
    return list_response(items, page)


@router.get(
    "/projects/{projectId}/trial-rounds/{roundId}",
    response_model=DataEnvelope[RoundDetailBody],
    responses=READ_RESPONSES,
    summary="试产轮详情（名额 + 派生状态 + etag；不写库）",
)
def get_trial_round(
    access: ProjectAccessDep, repo: TrialRepoDep, round_id: RoundIdPath
) -> dict[str, Any]:
    access.require(ProjectAction.VIEW)
    return single_response(service.get_round_detail(repo, access.project_id, round_id))


@router.post(
    "/projects/{projectId}/trial-rounds/{roundId}/transitions",
    response_model=DataEnvelope[RoundBody],
    responses=WRITE_RESPONSES,
    summary="推进 / 取消试产轮（需 If-Match）",
)
def transition_trial_round(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: TrialRepoDep,
    body: TransitionRequestBody,
    request: Request,
    round_id: RoundIdPath,
) -> dict[str, Any]:
    access.require(ProjectAction.EDIT)
    current = service.require_round(repo, access.project_id, round_id)
    _if_match(request, current)
    row = service.transition_round(
        repo,
        project_id=access.project_id,
        round_id=round_id,
        expected_lock_version=current.lock_version,
        to_state=body.to_state,
        reason=body.reason,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    return single_response(service.round_body(row))


@router.put(
    "/projects/{projectId}/trial-rounds/{roundId}/slots/{slotId}/content",
    response_model=DataEnvelope[SlotBody],
    responses=WRITE_RESPONSES,
    summary="把已生成的内容绑到名额（同内容重放 200）",
)
def bind_slot_content(
    access: ProjectAccessDep,
    repo: TrialRepoDep,
    body: BindContentRequestBody,
    round_id: RoundIdPath,
    slot_id: SlotIdPath,
) -> dict[str, Any]:
    access.require(ProjectAction.EDIT)
    service.bind_slot_content(
        repo,
        project_id=access.project_id,
        round_id=round_id,
        slot_id=slot_id,
        content_item_id=body.content_item_id,
    )
    row = service.require_round(repo, access.project_id, round_id)
    return single_response(service.slot_view(repo, row, slot_id))


@router.post(
    "/projects/{projectId}/trial-rounds/{roundId}/slots/{slotId}/replacements",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[ReplacementResultBody],
    responses=WRITE_RESPONSES,
    summary="淘汰补产：为被淘汰的名额建替代名额（需 If-Match）",
)
def create_slot_replacement(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: TrialRepoDep,
    request: Request,
    round_id: RoundIdPath,
    slot_id: SlotIdPath,
) -> dict[str, Any]:
    access.require(ProjectAction.EDIT)
    current = service.require_round(repo, access.project_id, round_id)
    _if_match(request, current)
    new_slot, row = service.create_replacement(
        repo,
        project_id=access.project_id,
        round_id=round_id,
        slot_id=slot_id,
        expected_lock_version=current.lock_version,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    return single_response(
        {
            "slot": service.slot_view(repo, row, new_slot.id),
            "round": service.round_body(row),
        }
    )


@router.get(
    "/projects/{projectId}/trial-rounds/{roundId}/gate",
    response_model=DataEnvelope[GateBody],
    responses=READ_RESPONSES,
    summary="关口判定：条件、统计、方向矩阵、决定与过期标记",
)
def get_trial_gate(
    access: ProjectAccessDep,
    repo: TrialRepoDep,
    round_id: RoundIdPath,
    scene_keys: Annotated[list[str] | None, Query(alias="sceneKeys")] = None,
) -> dict[str, Any]:
    access.require(ProjectAction.VIEW)
    return single_response(
        service.get_gate(repo, access.project_id, round_id, scene_keys=scene_keys)
    )


@router.post(
    "/projects/{projectId}/trial-rounds/{roundId}/gate-decisions",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[GateDecisionResultBody],
    responses=WRITE_RESPONSES,
    summary="批准进入扩产（人工；需 If-Match）",
)
def create_gate_decision(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: TrialRepoDep,
    body: GateDecisionRequestBody,
    request: Request,
    round_id: RoundIdPath,
) -> dict[str, Any]:
    access.require(ProjectAction.REVIEW)
    current = service.require_round(repo, access.project_id, round_id)
    _if_match(request, current)
    row, decision = service.approve_gate(
        repo,
        project_id=access.project_id,
        round_id=round_id,
        expected_lock_version=current.lock_version,
        approved_scene_keys=body.approved_scene_keys,
        reason=body.reason,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    return single_response(
        {
            "round": service.round_body(row),
            "decision": service.decision_body(repo, row, decision),
        }
    )
