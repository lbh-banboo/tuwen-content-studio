"""WP32 扩产 / 批产端点（**7 个 operation，6 条新路径**）。

| # | 方法 | 路径 | 成功码 | 权限 | 服务函数 |
|---|---|---|---|---|---|
| ① | POST | `/projects/{projectId}/batch-previews` | 200 | `EDIT`（金额另需 `VIEW_COST`） | `service.preview_batch` |
| ② | POST | `/projects/{projectId}/batches` | **202**（重放 **200**） | `EDIT`（金额另需 `VIEW_COST`） | `service.create_batch` |
| ③ | GET | `/projects/{projectId}/batches` | 200 | `VIEW` | `service.list_batches` |
| ④ | GET | `/batches/{batchId}` | 200 + `ETag` | `VIEW` | `service.get_batch_detail` |
| ⑤ | GET | `/batches/{batchId}/items` | 200 | `VIEW` | `service.list_items` |
| ⑥ | POST | `/batches/{batchId}/cancellations` | 202（已取消 200） | `EDIT` + `If-Match` | `service.cancel_batch` |
| ⑦ | POST | `/batches/{batchId}/retries` | 202 | `EDIT` + `If-Match` + `Idempotency-Key` | `service.retry_failed` |

## 权限

* 项目在路径里（①②③）走 `ProjectAccessDep` + `access.require(...)`；按 `{batchId}` 寻址的（④~⑦）
  **两步骤鉴权**：先反查 `project_id`（不存在 ⇒ 404），再 `resolve_project_access`（非成员也是 404，
  防枚举）。`ProjectAccessDep` 只做成员级判定，动作级每个端点都显式 `require`。
* 动作用 `EDIT`（与 `content-generations` 一致：“会花钱”）；不新建 `ProjectAction`。
* 金额字段需 `VIEW_COST`：无权时为 `null`，**不写 0**。

## 错误

`service.py` 抛 `ApiError`，本文件不 catch。错误码只用既有 `ErrorCode`；细分走 `details.reason`。
**`Idempotency-Key` 必填**（缺失 ⇒ 422 `idempotency_key_required`）：批次会花钱，“不幂等”不应是可选项。

## 关口

`GateCheckerDep`（`dependencies.py`）。默认未接线时 fail-closed（503 `trial_gate_not_wired`）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Path, Query, Request, Response, status

from rednote.modules.batches import service
from rednote.modules.batches.dependencies import BatchRepoDep, GateCheckerDep
from rednote.modules.batches.repository import BatchRepository
from rednote.modules.batches.schemas import (
    BatchBody,
    BatchItemBody,
    BatchPreviewBody,
    CreateBatchRequestBody,
    CreateBatchResponseBody,
    RetryResponseBody,
)
from rednote.modules.batches.types import (
    BATCH_KINDS,
    BATCH_STATES,
    CreateBatchCommand,
)
from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.projects.dependencies import (
    ProjectAccessDep,
    ProjectRepoDep,
    resolve_project_access,
)
from rednote.modules.projects.permissions import ProjectAction
from rednote.platform.api_responses import READ_RESPONSES, WRITE_RESPONSES
from rednote.platform.concurrency import require_if_match
from rednote.platform.errors import ApiError, DataEnvelope, ErrorCode, get_request_id
from rednote.platform.pagination import (
    PageParamsDep,
    PaginatedEnvelope,
    list_response,
    single_response,
)

router = APIRouter(tags=["batches"])

BATCH_READ_RESPONSES = READ_RESPONSES
BATCH_WRITE_RESPONSES = WRITE_RESPONSES


def _command(body: CreateBatchRequestBody) -> CreateBatchCommand:
    return CreateBatchCommand(
        batch_kind=body.batch_kind,
        quantity=body.quantity,
        plan_version_id=body.plan_version_id,
        baseline_id=body.baseline_id,
        model_profile_id=body.model_profile_id,
        scope=body.scope,
    )


def _validation_error(field: str, message: str, *, reason: str) -> ApiError:
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        message,
        details={
            "fieldErrors": [{"field": field, "type": reason, "message": message}],
            "reason": reason,
        },
    )


def _batch_access(
    repo: BatchRepository,
    project_repo: Any,
    user: Any,
    batch_id: uuid.UUID,
    action: ProjectAction,
) -> tuple[Any, Any]:
    """两步骤鉴权：批次 → 项目 → 动作。不存在与不属于你都是 404。"""
    batch = service.require_batch(repo, batch_id)
    access = resolve_project_access(project_repo, project_id=batch.project_id, user=user)
    access.require(action)
    return batch, access


def _can_view_cost(access: Any) -> bool:
    return bool(access.permissions.allows(ProjectAction.VIEW_COST))


# ---------------------------------------------------------------------------
# ① 预览
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{projectId}/batch-previews",
    response_model=DataEnvelope[BatchPreviewBody],
    responses=BATCH_WRITE_RESPONSES,
    summary="批次预览（生效上限 + 分配 + 预估 + 预算 + 关口结论；不落批次）",
)
def create_batch_preview(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: BatchRepoDep,
    gate_checker: GateCheckerDep,
    body: CreateBatchRequestBody,
) -> dict[str, Any]:
    """预览不落批次，但会写一行 `cost_estimates`（`estimate_cost` 的语义），故用 POST。"""
    access.require(ProjectAction.EDIT)
    preview = service.preview_batch(
        repo,
        project_id=access.project_id,
        command=_command(body),
        gate_checker=gate_checker,
        actor_id=user.id,
        request_id=get_request_id(),
        can_view_cost=_can_view_cost(access),
    )
    return single_response(preview)


# ---------------------------------------------------------------------------
# ② 创建
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{projectId}/batches",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DataEnvelope[CreateBatchResponseBody],
    responses=BATCH_WRITE_RESPONSES,
    summary="创建扩产 / 批产批次（202 异步生成；同键同体重放 200）",
)
def create_batch(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: BatchRepoDep,
    gate_checker: GateCheckerDep,
    body: CreateBatchRequestBody,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """首次 **202**；同键同体重放 **200**（`replayed: true`）；同键异体 409。"""
    access.require(ProjectAction.EDIT)
    can_view_cost = _can_view_cost(access)
    result = service.create_batch(
        repo,
        project_id=access.project_id,
        command=_command(body),
        idempotency_key=idempotency_key,
        gate_checker=gate_checker,
        actor_id=user.id,
        request_id=get_request_id(),
        can_view_cost=can_view_cost,
    )
    batch = service.require_batch(repo, result.batch.id)
    detail = service.get_batch_detail(repo, batch, can_view_cost=can_view_cost)
    detail["replayed"] = result.replayed
    response.status_code = (
        status.HTTP_200_OK if result.replayed else status.HTTP_202_ACCEPTED
    )
    response.headers["ETag"] = service.batch_etag(batch)
    return single_response(detail)


# ---------------------------------------------------------------------------
# ③ 列表
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/batches",
    response_model=PaginatedEnvelope[BatchBody],
    responses=BATCH_READ_RESPONSES,
    summary="项目下的批次列表（新到旧，游标分页）",
)
def list_batches(
    access: ProjectAccessDep,
    repo: BatchRepoDep,
    params: PageParamsDep,
    batch_kind: Annotated[str | None, Query(alias="batchKind")] = None,
    state: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    access.require(ProjectAction.VIEW)
    if batch_kind is not None and batch_kind not in BATCH_KINDS:
        raise _validation_error(
            "batchKind", f"未知的批次档位：{batch_kind!r}", reason="batch_kind_unknown"
        )
    if state is not None and state not in BATCH_STATES:
        raise _validation_error("state", f"未知的批次状态：{state!r}", reason="state_unknown")
    page = service.list_batches(
        repo,
        project_id=access.project_id,
        batch_kind=batch_kind,
        state=state,
        page_params=params,
        can_view_cost=_can_view_cost(access),
    )
    return list_response(list(page.items), page.page)


# ---------------------------------------------------------------------------
# ④ 详情
# ---------------------------------------------------------------------------


@router.get(
    "/batches/{batchId}",
    response_model=DataEnvelope[BatchBody],
    responses=BATCH_READ_RESPONSES,
    summary="批次详情（含读时折算的进度；ETag = 批次锁版本）",
)
def get_batch(
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    repo: BatchRepoDep,
    response: Response,
    batch_id: Annotated[uuid.UUID, Path(alias="batchId")],
) -> dict[str, Any]:
    batch, access = _batch_access(repo, project_repo, user, batch_id, ProjectAction.VIEW)
    response.headers["ETag"] = service.batch_etag(batch)
    return single_response(
        service.get_batch_detail(repo, batch, can_view_cost=_can_view_cost(access))
    )


# ---------------------------------------------------------------------------
# ⑤ 成员
# ---------------------------------------------------------------------------


@router.get(
    "/batches/{batchId}/items",
    response_model=PaginatedEnvelope[BatchItemBody],
    responses=BATCH_READ_RESPONSES,
    summary="批次成员（按序号升序；最新轮任务状态，缺失为 null）",
)
def list_batch_items(
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    repo: BatchRepoDep,
    params: PageParamsDep,
    batch_id: Annotated[uuid.UUID, Path(alias="batchId")],
) -> dict[str, Any]:
    _batch_access(repo, project_repo, user, batch_id, ProjectAction.VIEW)
    page = service.list_items(repo, batch_id=batch_id, page_params=params)
    return list_response(list(page.items), page.page)


# ---------------------------------------------------------------------------
# ⑥ 取消
# ---------------------------------------------------------------------------


@router.post(
    "/batches/{batchId}/cancellations",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DataEnvelope[BatchBody],
    responses=BATCH_WRITE_RESPONSES,
    summary="请求取消批次（不可恢复；If-Match 必带；已取消 200）",
)
def create_batch_cancellation(
    request: Request,
    response: Response,
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    repo: BatchRepoDep,
    batch_id: Annotated[uuid.UUID, Path(alias="batchId")],
) -> dict[str, Any]:
    batch, access = _batch_access(repo, project_repo, user, batch_id, ProjectAction.EDIT)
    require_if_match(
        request,
        resource_id=batch.id,
        current_version=batch.lock_version,
        resource_label="批次",
    )
    fresh, changed = service.cancel_batch(
        repo,
        batch=batch,
        expected_lock_version=batch.lock_version,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    response.status_code = status.HTTP_202_ACCEPTED if changed else status.HTTP_200_OK
    response.headers["ETag"] = service.batch_etag(fresh)
    return single_response(
        service.get_batch_detail(repo, fresh, can_view_cost=_can_view_cost(access))
    )


# ---------------------------------------------------------------------------
# ⑦ 失败项重做
# ---------------------------------------------------------------------------


@router.post(
    "/batches/{batchId}/retries",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DataEnvelope[RetryResponseBody],
    responses=BATCH_WRITE_RESPONSES,
    summary="重做最新一轮失败的成员（If-Match + Idempotency-Key；未知态不重做）",
)
def create_batch_retry(
    request: Request,
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    repo: BatchRepoDep,
    batch_id: Annotated[uuid.UUID, Path(alias="batchId")],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    batch, _access = _batch_access(repo, project_repo, user, batch_id, ProjectAction.EDIT)

    def _check_if_match(lock_version: int) -> None:
        require_if_match(
            request,
            resource_id=batch.id,
            current_version=lock_version,
            resource_label="批次",
        )

    result = service.retry_failed(
        repo,
        batch=batch,
        idempotency_key=idempotency_key,
        check_if_match=_check_if_match,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    return single_response(result.body)
