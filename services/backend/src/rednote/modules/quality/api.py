"""WP33 质量报告端点（**7 个 operation，6 条新路径**）。

| # | 方法 | 路径 | 成功码 | 权限 |
|---|---|---|---|---|
| ① | POST | `/projects/{projectId}/quality-reports` | **201** 新版本 / **200** 输入指纹相同（`created=false`） | `REPORT` |
| ② | GET | `/projects/{projectId}/quality-reports` | 200 | `VIEW` |
| ③ | GET | `/quality-reports/{reportId}` | 200 | `VIEW`（反查项目） |
| ④ | GET | `/quality-report-versions/{versionId}` | 200 | `VIEW`（反查项目） |
| ⑤ | GET | `/quality-report-versions/{versionId}/pairs` | 200 | `VIEW`（反查项目） |
| ⑥ | POST | `/quality-report-versions/{versionId}/artifacts` | **201** 新建 / **200** 已存在 | `REPORT`（反查项目） |
| ⑦ | GET | `/quality-dimensions` | 200 | 登录即可 |

* 按 `{reportId}` / `{versionId}` 寻址的走**两步骤鉴权**：先反查 `project_id`（不存在 ⇒ 404），再
  `resolve_project_access`（非成员也是 404，不区分「不存在」与「不属于你」）。
* **零 `If-Match`**：报告是追加写的观测，无「原地改用户资源」的读-写窗口（`lock_version` 只服务投影推进的 CAS）。
* 阈值是**占位**（待 OP-228）：响应带 `thresholdBasis` / `thresholdNote`，`ALERT` 不是阻断条件。
* 错误码只用既有 `ErrorCode`；细分走 `details.reason`。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Response, status

from rednote.modules.files.dependencies import StorageBackendDep
from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.projects.dependencies import (
    ProjectAccessDep,
    ProjectRepoDep,
    resolve_project_access,
)
from rednote.modules.projects.permissions import ProjectAction
from rednote.modules.quality import service
from rednote.modules.quality.dependencies import QualityRepoDep
from rednote.modules.quality.schemas import (
    ArtifactBody,
    ArtifactRequestBody,
    DimensionCatalogBody,
    GenerateReportBody,
    GenerateReportRequestBody,
    PairBody,
    SeriesBody,
    SeriesDetailBody,
    VersionDetailBody,
)
from rednote.modules.quality.types import (
    DIMENSIONS,
    REASON_DIMENSION_INVALID,
    REASON_REPORT_NOT_FOUND,
    REASON_REPORT_VERSION_NOT_FOUND,
)
from rednote.platform.api_responses import READ_RESPONSES, WRITE_RESPONSES
from rednote.platform.errors import ApiError, DataEnvelope, ErrorCode
from rednote.platform.pagination import (
    PageParamsDep,
    PaginatedEnvelope,
    list_response,
    single_response,
)

router = APIRouter(tags=["quality"])


def _missing(reason: str, message: str) -> ApiError:
    return ApiError(ErrorCode.RESOURCE_NOT_FOUND, message, details={"reason": reason})


@router.post(
    "/projects/{projectId}/quality-reports",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[GenerateReportBody],
    responses=WRITE_RESPONSES,
    summary="生成质量报告（落 REPORT 版本；输入指纹相同则 200 返回既有版本；阈值为占位，待 OP-228）",
)
def create_quality_report(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: QualityRepoDep,
    body: GenerateReportRequestBody,
    response: Response,
) -> dict[str, Any]:
    access.require(ProjectAction.REPORT)
    scope = service.ScopeRequest(
        kind=body.scope.kind,
        content_ids=None if body.scope.content_ids is None else tuple(body.scope.content_ids),
        batch_id=body.scope.batch_id,
    )
    result = service.generate_report(
        repo, project_id=access.project_id, scope=scope, actor_id=user.id
    )
    detail = service.render_version_detail(repo, result.version)
    detail["created"] = result.created
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return single_response(detail)


@router.get(
    "/projects/{projectId}/quality-reports",
    response_model=PaginatedEnvelope[SeriesBody],
    responses=READ_RESPONSES,
    summary="项目下的质量报告系列（新到旧，游标分页；每系列带当前版本摘要）",
)
def list_quality_reports(
    access: ProjectAccessDep, repo: QualityRepoDep, params: PageParamsDep
) -> dict[str, Any]:
    access.require(ProjectAction.VIEW)
    page = service.list_reports(repo, project_id=access.project_id, page_params=params)
    return list_response(list(page.items), page.page)


@router.get(
    "/quality-reports/{reportId}",
    response_model=DataEnvelope[SeriesDetailBody],
    responses=READ_RESPONSES,
    summary="质量报告系列详情（当前版本全摘要 + 逐篇 subjectState：CURRENT / STALE，读时判）",
)
def get_quality_report(
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    repo: QualityRepoDep,
    report_id: Annotated[uuid.UUID, Path(alias="reportId")],
) -> dict[str, Any]:
    series = repo.get_series(report_id)
    if series is None:
        raise _missing(REASON_REPORT_NOT_FOUND, "质量报告不存在或无权访问。")
    access = resolve_project_access(project_repo, project_id=series.project_id, user=user)
    access.require(ProjectAction.VIEW)
    return single_response(service.get_series_detail(repo, series))


def _version_access(
    repo: Any, project_repo: Any, user: Any, version_id: uuid.UUID, action: ProjectAction
) -> tuple[Any, uuid.UUID]:
    version = repo.get_version(version_id)
    project_id = None if version is None else repo.version_project(version_id)
    if version is None or project_id is None:
        raise _missing(REASON_REPORT_VERSION_NOT_FOUND, "质量报告版本不存在或无权访问。")
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(action)
    return version, project_id


@router.get(
    "/quality-report-versions/{versionId}",
    response_model=DataEnvelope[VersionDetailBody],
    responses=READ_RESPONSES,
    summary="质量报告版本明细（逐篇 / 7 个维度 / 对位汇总；相似对见 /pairs，不内联）",
)
def get_quality_report_version(
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    repo: QualityRepoDep,
    version_id: Annotated[uuid.UUID, Path(alias="versionId")],
) -> dict[str, Any]:
    version, _project_id = _version_access(repo, project_repo, user, version_id, ProjectAction.VIEW)
    return single_response(service.render_version_detail(repo, version))


@router.get(
    "/quality-report-versions/{versionId}/pairs",
    response_model=PaginatedEnvelope[PairBody],
    responses=READ_RESPONSES,
    summary="命中的相似对（相似度高的在前，游标分页；可按 dimension 过滤）",
)
def list_quality_pairs(
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    repo: QualityRepoDep,
    params: PageParamsDep,
    version_id: Annotated[uuid.UUID, Path(alias="versionId")],
    dimension: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    version, _project_id = _version_access(repo, project_repo, user, version_id, ProjectAction.VIEW)
    if dimension is not None and dimension not in DIMENSIONS:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            f"未知的维度：{dimension!r}",
            details={
                "reason": REASON_DIMENSION_INVALID,
                "fieldErrors": [
                    {"field": "dimension", "type": REASON_DIMENSION_INVALID, "message": "未知的维度"}
                ],
            },
        )
    page = service.list_pairs(repo, version=version, dimension=dimension, page_params=params)
    return list_response(list(page.items), page.page)


@router.post(
    "/quality-report-versions/{versionId}/artifacts",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[ArtifactBody],
    responses=WRITE_RESPONSES,
    summary="导出报告产物（PURPOSE_GENERATED；同 (版本, 格式) 幂等：新建 201 / 已存在 200）",
)
def create_quality_artifact(
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    repo: QualityRepoDep,
    storage: StorageBackendDep,
    body: ArtifactRequestBody,
    response: Response,
    version_id: Annotated[uuid.UUID, Path(alias="versionId")],
) -> dict[str, Any]:
    version, project_id = _version_access(repo, project_repo, user, version_id, ProjectAction.REPORT)
    artifact, created = service.create_artifact(
        repo,
        storage,
        version=version,
        project_id=project_id,
        fmt=body.format,
        actor_id=user.id,
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return single_response(artifact)


@router.get(
    "/quality-dimensions",
    response_model=DataEnvelope[list[DimensionCatalogBody]],
    responses=READ_RESPONSES,
    summary="维度目录（代码常量的只读投影；文本维度的阈值为占位，待 OP-228）",
)
def list_quality_dimensions(user: CurrentUserDep) -> dict[str, Any]:
    return single_response(service.dimension_catalog())
