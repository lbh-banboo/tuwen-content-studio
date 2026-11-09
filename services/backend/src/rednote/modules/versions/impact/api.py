"""WP34 端点（5 个 operation）。

| # | 方法 路径 | 权限 | 说明 |
|---|---|---|---|
| ① | GET `/projects/{projectId}/rollback-impacts?entityType&entityId&targetVersionId` | VIEW | 回退预览（D = 服务端读投影） |
| ② | GET `/projects/{projectId}/versions/{versionId}/impact` | VIEW | 任一版本的下游影响 |
| ③ | GET `/projects/{projectId}/exports/{exportGroupId}/validity` | VIEW | 读时评估一整组，不写库 |
| ④ | POST `/projects/{projectId}/export-validity-sweeps` | DELIVER | 扫描并追加事件，固定 200，天然幂等 |
| ⑤ | GET `/projects/{projectId}/export-validity-events` | VIEW | 事件历史（最新在前，游标分页） |

鉴权两步：`ProjectAccessDep`（非成员 ⇒ 404）→ `access.require(动作)`。不新建 `ProjectAction`。
`UNKNOWN`（算不出资格）不是错误，随 200 逐行返回。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query

from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.projects.dependencies import ProjectAccessDep
from rednote.modules.projects.permissions import ProjectAction
from rednote.modules.versions.impact import service
from rednote.modules.versions.impact.dependencies import ImpactRepoDep
from rednote.modules.versions.impact.schemas import (
    ImpactPreviewBody,
    SweepBody,
    SweepRequestBody,
    ValidityBody,
    ValidityEventBody,
)
from rednote.platform.api_responses import READ_RESPONSES, WRITE_RESPONSES
from rednote.platform.errors import DataEnvelope
from rednote.platform.pagination import (
    CursorSpec,
    PageParamsDep,
    PaginatedEnvelope,
    list_response,
    paginate_items,
    single_response,
)

router = APIRouter(tags=["versions-impact"])

#: 事件列表是内存分页，须取全量再分页（ISS-81），此处为全量扫描的上限。
_FULL_SCAN_LIMIT: int = 10_000


@router.get(
    "/projects/{projectId}/rollback-impacts",
    response_model=DataEnvelope[ImpactPreviewBody],
    responses=WRITE_RESPONSES,  # 含 409（NO_CURRENT_VERSION）
    summary="回退影响预览（起点取内容当前投影；纯读，不执行回退）",
)
def get_rollback_impact(
    access: ProjectAccessDep,
    repo: ImpactRepoDep,
    entity_type: Annotated[str, Query(alias="entityType")],
    entity_id: Annotated[uuid.UUID, Query(alias="entityId")],
    target_version_id: Annotated[uuid.UUID, Query(alias="targetVersionId")],
) -> dict[str, Any]:
    access.require(ProjectAction.VIEW)
    return single_response(
        service.preview_rollback(
            repo,
            project_id=access.project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            target_version_id=target_version_id,
        )
    )


@router.get(
    "/projects/{projectId}/versions/{versionId}/impact",
    response_model=DataEnvelope[ImpactPreviewBody],
    responses=READ_RESPONSES,
    summary="任一版本的下游影响（含导出行；纯读）",
)
def get_version_impact(
    access: ProjectAccessDep,
    repo: ImpactRepoDep,
    version_id: Annotated[uuid.UUID, Path(alias="versionId")],
) -> dict[str, Any]:
    access.require(ProjectAction.VIEW)
    return single_response(
        service.preview_version_impact(repo, project_id=access.project_id, version_id=version_id)
    )


@router.get(
    "/projects/{projectId}/exports/{exportGroupId}/validity",
    response_model=DataEnvelope[ValidityBody],
    responses=READ_RESPONSES,
    summary="导出有效性读时评估（逐行 derivedState / recordedState；不写库）",
)
def get_export_validity(
    access: ProjectAccessDep,
    repo: ImpactRepoDep,
    export_group_id: Annotated[uuid.UUID, Path(alias="exportGroupId")],
) -> dict[str, Any]:
    access.require(ProjectAction.VIEW)
    return single_response(
        service.evaluate_group(repo, project_id=access.project_id, export_group_id=export_group_id)
    )


@router.post(
    "/projects/{projectId}/export-validity-sweeps",
    response_model=DataEnvelope[SweepBody],
    responses=WRITE_RESPONSES,  # 含 409（IMPACT_TRUNCATED）
    summary="扫描导出有效性并追加失效 / 恢复事件（状态变化才写；固定 200）",
)
def create_export_validity_sweep(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: ImpactRepoDep,
    body: SweepRequestBody,
) -> dict[str, Any]:
    access.require(ProjectAction.DELIVER)
    return single_response(
        service.sweep(
            repo,
            project_id=access.project_id,
            actor_id=user.id,
            content_item_ids=body.content_item_ids,
            from_version_id=body.from_version_id,
        )
    )


@router.get(
    "/projects/{projectId}/export-validity-events",
    response_model=PaginatedEnvelope[ValidityEventBody],
    responses=READ_RESPONSES,
    summary="导出失效事件历史（最新在前，游标分页）",
)
def list_export_validity_events(
    access: ProjectAccessDep,
    repo: ImpactRepoDep,
    params: PageParamsDep,
    export_group_id: Annotated[uuid.UUID | None, Query(alias="exportGroupId")] = None,
    export_item_id: Annotated[uuid.UUID | None, Query(alias="exportItemId")] = None,
) -> dict[str, Any]:
    access.require(ProjectAction.VIEW)
    events = service.list_events(
        repo,
        project_id=access.project_id,
        export_group_id=export_group_id,
        export_item_id=export_item_id,
    )[:_FULL_SCAN_LIMIT]
    spec = CursorSpec(
        sort_fields=("createdAt", "seq"),
        direction="desc",
        filters={
            "projectId": str(access.project_id),
            "exportGroupId": str(export_group_id),
            "exportItemId": str(export_item_id),
        },
    )
    window, page = paginate_items(
        events,
        params,
        spec,
        sort_key=lambda e: (e["createdAt"], e["seq"]),
        id_of=lambda e: e["id"],
    )
    return list_response(window, page)
