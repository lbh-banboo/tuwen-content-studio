"""审计与待办端点（协议 03 §10，3 个，逐条对照）。

| 方法 | 路径 | 入参（协议原文） | 说明 |
|---|---|---|---|
| GET | `/work-items` | `projectId?`, `state`, `cursor`, `limit` | 当前成员待办 |
| POST | `/work-items/{id}/resolutions` | `reason` | **完成待办不自动通过其业务对象** |
| GET | `/audit-events` | `projectId?`, `objectType?`, `objectId?`, `cursor`, `limit` | 项目成员仅见所属项目；管理者全量 |

**分页用键集游标**（与 WP10/WP11 一致）：`cursor` 是上一页返回的
`nextAnchor` 的不透明编码。这里**不自己实现指纹**，直接复用
`platform/pagination.py` 的工具 —— 游标指纹必须绑定"排序 + 筛选 + 可见范围"，
各包自己实现必然漂移（WP10 已踩过）。

**`POST /work-items/{id}/resolutions` 为什么没有幂等键**：
它是**状态机迁移**（`OPEN → DONE`），幂等由状态本身承担 ——
重复调用时 `rowcount = 0`，返回"已完成"而不报错。
这与 WP13 的 `enqueue_job`（需要幂等键，因为"建任务"不是幂等操作）
是两类不同的幂等来源，不能一概而论。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from rednote.modules.audit import service
from rednote.modules.audit.repository import AuditRepository
from rednote.modules.identity.dependencies import CurrentUser, CurrentUserDep, DBSession
from rednote.platform.api_responses import (
    ACCESS_ERROR_RESPONSES,
    CONCURRENCY_ERROR_RESPONSES,
    UNIVERSAL_ERROR_RESPONSES,
)
from rednote.platform.errors import (
    ApiError,
    DataEnvelope,
    ErrorCode,
    build_meta,
    get_request_id,
)

router = APIRouter(tags=["audit"])


# ---------------------------------------------------------------------------
# 请求 / 响应体
# ---------------------------------------------------------------------------


class ResolutionBody(BaseModel):
    """`POST /work-items/{id}/resolutions` 的请求体（协议 §10 入参：`reason`）。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    reason: str | None = Field(default=None, max_length=service.MAX_REASON_LENGTH)


class WorkItemBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    work_item_id: uuid.UUID = Field(alias="workItemId")
    project_id: uuid.UUID | None = Field(alias="projectId")
    assignee_id: uuid.UUID | None = Field(alias="assigneeId")
    kind: str
    subject_type: str = Field(alias="subjectType")
    subject_id: uuid.UUID = Field(alias="subjectId")
    state: str
    due_at: datetime | None = Field(alias="dueAt")
    created_at: datetime = Field(alias="createdAt")


class ResolutionResultBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    work_item_id: uuid.UUID = Field(alias="workItemId")
    state: str
    # `resolved=False` 表示幂等命中（它本来就是 DONE）。
    # 显式回带它，前端才能区分"我这次真的完成了"与"它早就完成了"。
    resolved: bool


class AuditEventBody(BaseModel):
    """一条审计事件。**注意没有正文**（设计如此，见 service 的说明）。

    `beforeHash` / `afterHash` 是**规范化摘要**：能回答"变没变、从哪个变到哪个"
    （比对即可），但不含内容。想看内容请顺着 `versionId` 去查那一版对象。
    """

    model_config = ConfigDict(populate_by_name=True)

    audit_id: uuid.UUID = Field(alias="auditId")
    actor_kind: str = Field(alias="actorKind")
    actor_id: uuid.UUID | None = Field(alias="actorId")
    action: str
    object_type: str = Field(alias="objectType")
    object_id: uuid.UUID = Field(alias="objectId")
    project_id: uuid.UUID | None = Field(alias="projectId")
    version_id: uuid.UUID | None = Field(alias="versionId")
    before_hash: str | None = Field(alias="beforeHash")
    after_hash: str | None = Field(alias="afterHash")
    # 这次动作是否改变了对象内容（三种含义见 service.AuditView.changed）。
    changed: bool
    reason: str | None
    request_id: str = Field(alias="requestId")
    created_at: datetime = Field(alias="createdAt")


class PageBody(BaseModel):
    """协议 §1 的 `page` 块。"""

    model_config = ConfigDict(populate_by_name=True)

    has_more: bool = Field(alias="hasMore")
    next_anchor: str | None = Field(alias="nextAnchor")


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def get_audit_repository(db: DBSession) -> AuditRepository:
    return AuditRepository(db)


AuditRepoDep = Annotated[AuditRepository, Depends(get_audit_repository)]


def _member_project_ids(db: DBSession, user: CurrentUser) -> frozenset[uuid.UUID]:
    """当前用户的活跃项目集合（一次查库，供可见范围过滤用）。

    **复用 WP14 的实现**（`events.service.active_member_project_ids`）——
    两处各写一遍"活跃成员"判据迟早漂移，而漂移的后果是越权。
    """
    from rednote.modules.events.service import active_member_project_ids

    return active_member_project_ids(db, user_id=user.id)


def _to_work_item_body(view: service.WorkItemView) -> dict[str, Any]:
    return {
        "workItemId": view.work_item_id,
        "projectId": view.project_id,
        "assigneeId": view.assignee_id,
        "kind": view.kind,
        "subjectType": view.subject_type,
        "subjectId": view.subject_id,
        "state": view.state,
        "dueAt": view.due_at,
        "createdAt": view.created_at,
    }


def _to_audit_body(view: service.AuditView) -> dict[str, Any]:
    return {
        "auditId": view.audit_id,
        "actorKind": view.actor_kind,
        "actorId": view.actor_id,
        "action": view.action,
        "objectType": view.object_type,
        "objectId": view.object_id,
        "projectId": view.project_id,
        "versionId": view.version_id,
        "beforeHash": view.before_hash,
        "afterHash": view.after_hash,
        "changed": view.changed,
        "reason": view.reason,
        "requestId": view.request_id,
        "createdAt": view.created_at,
    }


def _encode_anchor(anchor: tuple[datetime, uuid.UUID] | None) -> str | None:
    """把 `(created_at, id)` 游标编码成不透明字符串。

    用 `|` 分隔（`created_at` 的 ISO 形式里没有 `|`，UUID 里也没有）——
    比引入 base64 更简单，且**可读**（排障时能直接看出游标指向哪个时间点）。
    """
    if anchor is None:
        return None
    moment, identifier = anchor
    return f"{moment.isoformat()}|{identifier}"


def _decode_anchor(cursor: str | None, *, field: str) -> tuple[datetime, uuid.UUID] | None:
    """解析游标。非法即 422（**不静默当第一页**）。

    与 `Last-Event-ID`（WP14，非法当新连接）的处理**相反**，理由是：
    游标是**分页的定位符**，非法游标若被当成"第一页"，客户端会
    从头开始翻、无限循环（每次都给第一页）。而 `Last-Event-ID` 非法时
    当新连接是**安全**的（最坏情况是多收一些事件，且那些事件本来
    就在它的可见范围内）。**两者的差别在于"静默降级会不会造成错误数据"**。
    """
    if cursor is None or not cursor.strip():
        return None
    raw = cursor.strip()
    if "|" not in raw:
        raise ApiError(
            ErrorCode.MALFORMED_REQUEST,
            "cursor 格式非法。",
            details={"field": field, "reason": "cursor_malformed"},
        )
    moment_text, _, identifier_text = raw.partition("|")
    try:
        moment = datetime.fromisoformat(moment_text)
        identifier = uuid.UUID(identifier_text)
    except (ValueError, AttributeError) as exc:
        raise ApiError(
            ErrorCode.MALFORMED_REQUEST,
            "cursor 格式非法。",
            details={"field": field, "reason": "cursor_malformed"},
        ) from exc
    return moment, identifier


# ---------------------------------------------------------------------------
# 1. GET /work-items
# ---------------------------------------------------------------------------


@router.get(
    "/work-items",
    response_model=DataEnvelope[list[WorkItemBody]],
    responses={
        **UNIVERSAL_ERROR_RESPONSES,
        **ACCESS_ERROR_RESPONSES,
    },
    summary="当前成员待办（含未认领的项目待办）",
)
def list_work_items(
    user: CurrentUserDep,
    db: DBSession,
    repo: AuditRepoDep,
    project_id: Annotated[uuid.UUID | None, Query(alias="projectId")] = None,
    state: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = service.DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """列出待办。

    **可见范围**（`service.list_work_items` 的 docstring 有完整表格）：
    指派给我的 + 未认领且属于我所属项目的；管理者全量。
    指派给**别人**的看不到 —— 协议原文是"**当前成员**待办"，
    语义是"我要做什么"，不是"团队在做什么"。

    **默认只看 OPEN**：用户打开待办列表想看到的是"还没做的"。
    显式传 `state=DONE` 可查历史。
    """
    page = service.list_work_items(
        repo,
        user=user,
        member_project_ids=_member_project_ids(db, user),
        project_id=project_id,
        state=state,
        limit=limit,
        after=_decode_anchor(cursor, field="cursor"),
    )
    return {
        "data": [_to_work_item_body(item) for item in page.items],
        "page": {
            "hasMore": page.has_more,
            "nextAnchor": _encode_anchor(page.next_anchor),
        },
        "meta": build_meta(),
    }


# ---------------------------------------------------------------------------
# 2. POST /work-items/{id}/resolutions
# ---------------------------------------------------------------------------


@router.post(
    "/work-items/{workItemId}/resolutions",
    response_model=DataEnvelope[ResolutionResultBody],
    responses={
        **UNIVERSAL_ERROR_RESPONSES,
        **ACCESS_ERROR_RESPONSES,
        **CONCURRENCY_ERROR_RESPONSES,
    },
    summary="完成待办（不自动通过其业务对象）",
)
def resolve_work_item(
    user: CurrentUserDep,
    db: DBSession,
    repo: AuditRepoDep,
    body: ResolutionBody,
    work_item_id: Annotated[uuid.UUID, Path(alias="workItemId")],
) -> dict[str, Any]:
    """完成一条待办。

    协议原句：**「完成待办不自动通过其业务对象」**。
    本端点**只**把待办置为 `DONE`，**不**改任何业务对象的状态 ——
    "内容待审"待办完成后，内容**仍然**是待审状态。
    审核要通过它自己的审核命令走（那里有自审、版本绑定、影响标记等全套校验）。

    **幂等**：已 DONE 再次调用 → `resolved=false`（**不报错**）。
    用户点两次"完成"是正常操作；报错只会让他以为没完成。

    **同事务写审计**：完成待办是"谁在什么时候把哪条待办结了、为什么"，
    这正是审计要记的。**幂等命中时不写审计**（否则重试会留下假凭据）。
    """
    result = service.resolve_work_item(
        repo,
        work_item_id=work_item_id,
        reason=body.reason,
        user=user,
        request_id=get_request_id(),
        member_project_ids=_member_project_ids(db, user),
    )
    return {
        "data": {
            "workItemId": result.item.work_item_id,
            "state": result.item.state,
            "resolved": result.resolved,
        },
        "meta": build_meta(),
    }


# ---------------------------------------------------------------------------
# 3. GET /audit-events
# ---------------------------------------------------------------------------


@router.get(
    "/audit-events",
    response_model=DataEnvelope[list[AuditEventBody]],
    responses={
        **UNIVERSAL_ERROR_RESPONSES,
        **ACCESS_ERROR_RESPONSES,
    },
    summary="审计事件（项目成员仅见所属项目；管理者全量）",
)
def list_audit_events(
    user: CurrentUserDep,
    db: DBSession,
    repo: AuditRepoDep,
    project_id: Annotated[uuid.UUID | None, Query(alias="projectId")] = None,
    object_type: Annotated[str | None, Query(alias="objectType")] = None,
    object_id: Annotated[uuid.UUID | None, Query(alias="objectId")] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = service.DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """列出审计事件。

    **可见范围**（协议原文「项目成员仅见所属项目；**管理者全量**」）：

    | 情形 | 结果 |
    |---|---|
    | 管理者 | 全量（含平台级 `projectId` 为空的事件） |
    | 指定 `projectId` 且我是成员 | 该项目 |
    | 指定 `projectId` 但我不是成员 | **空页**（不是 403 —— 防存在性枚举） |
    | 未指定 `projectId` | 我所属**全部**项目的审计 |

    **事件本身不含正文**：`beforeHash` / `afterHash` 是规范化摘要。
    想看内容请顺着 `versionId` 去查那一版对象 —— 审计不该自己复现正文。
    """
    page = service.list_audit_events(
        repo,
        user=user,
        member_project_ids=_member_project_ids(db, user),
        project_id=project_id,
        object_type=object_type,
        object_id=object_id,
        limit=limit,
        after=_decode_anchor(cursor, field="cursor"),
    )
    return {
        "data": [_to_audit_body(item) for item in page.items],
        "page": {
            "hasMore": page.has_more,
            "nextAnchor": _encode_anchor(page.next_anchor),
        },
        "meta": build_meta(),
    }


__all__ = ["router"]