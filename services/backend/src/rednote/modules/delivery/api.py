"""KOC 交付与发布登记的端点（WP36）。

| # | 方法 路径 | 权限 | 说明 |
|---|---|---|---|
| ① | `GET /projects/{projectId}/koc-records` | `VIEW` | KOC 列表（游标分页） |
| ② | `POST /projects/{projectId}/koc-records` | `DELIVER` | 建 KOC，201 |
| ③ | `GET /projects/{projectId}/deliveries` | `VIEW` | 交付列表（游标分页） |
| ④ | `POST /projects/{projectId}/deliveries` | `DELIVER` | 建交付（`PREPARED`），201；支持 `Idempotency-Key` |
| ⑤ | `GET /deliveries/{deliveryId}` | `VIEW` | 交付详情（item 级，先反查项目再鉴权） |
| ⑥ | `POST /deliveries/{deliveryId}/confirmations` | `DELIVER` | **标记已交付**；资格复验通过才放行 |
| ⑦ | `POST /deliveries/{deliveryId}/cancellations` | `DELIVER` | 取消（仅 `PREPARED`） |
| ⑧ | `POST /deliveries/{deliveryId}/replacements` | `DELIVER` | 替换（**追加新行**），201；支持 `Idempotency-Key` |
| ⑨ | `GET /projects/{projectId}/publications` | `VIEW` | 发布登记列表 |
| ⑩ | `POST /projects/{projectId}/publications` | `DELIVER` | 登记发布，201；支持 `Idempotency-Key` |
| ⑪ | `GET /projects/{projectId}/unmarked-exports` | `VIEW` | 「已**导出**未标记」投影（见下） |
| ⑫ | `POST /publications/{publicationId}/links` | `DELIVER` | **WP37** 登记发布链接（规范化 + 查重），201 |
| ⑬ | `GET /publication-links` | 登录即可 | **WP37** 按 `canonicalUrl` 跨项目查重；非管理者只见自己所在项目的命中 |
| ⑭ | `POST /publications/{publicationId}/verifications` | `DELIVER` | **WP37** 写 `contentState`（仅 VERIFIED / UNVERIFIED） |

## 路径风格：为什么不是契约草案的平铺动词路径

契约草案给的是 `/deliveries`、`/koc-records`、`/deliveries/{id}/deliver`（平铺 +
动词）。实测**正式契约 `contracts/openapi.json` 的 86 条路径里这两种形态都不存在**：
项目下的资源集合一律 `/projects/{projectId}/xxx`（`/contents`、`/exports`、
`/budget-reservations`、`/content-reviews` …），动作一律是**名词化子资源**、零动词
（`/contents/{id}/regenerations`、`/budget-reservations/{id}/releases`、
`/uploads/{id}/completions`、`/contents/{id}/title-adoptions`）。
本包照仓内既有风格落地；契约草案的修订清单随交付报告一并交主 agent 串行应用。

## ⚠️ ⑪ 的口径名字

原型 `static/delivery.html:65-67` 的指标卡写「已下载未标记」。**本仓没有下载
事件表**（WP35 的 download 端点零写操作）⇒「下载过几次」在磁盘上没有任何载体。
本端点算的是「已**导出**未标记」，名字必须照实说，不得沿用原型字样。

## 鉴权两步

先 `resolve_project_access`（非成员 ⇒ **404 防枚举**），再 `access.require(action)`。
item 级路径（⑤⑥⑦⑧）**先按 id 反查 `project_id` 再鉴权** —— 跨项目的 id 与
"不存在"同形 404，不泄露存在性。
⚠️ 结项项目（`PHASE_CLOSED`）的 `DELIVER` 被 `permissions.py:229-234` 硬拒 ⇒
结项后不能新建交付/登记发布，这是既有设施给的，本包不实现、但有断言。

## 错误码翻译（唯一判据：换一个请求参数能否成功？）

| reason | 协议码 | 说明 |
|---|---|---|
| `PROJECT_NOT_FOUND` / `DELIVERY_NOT_FOUND` / `KOC_NOT_FOUND` / `EXPORT_ITEM_NOT_FOUND` | 404 | 找不到（含跨项目，与不存在同形） |
| `DISPLAY_NAME_BLANK` / `PUBLICATION_KEY_INVALID` / `PUBLISHED_AT_SHAPE` / `idempotency_key_invalid` | 422 | 改请求即可成功 |
| `INVALID_TRANSITION` / `DELIVERY_CANCELLED` / `ALREADY_REPLACED` / `ELIGIBILITY_STALE` / `ELIGIBILITY_UNAVAILABLE` | 409 `INVALID_TRANSITION` | 要等资源状态变化（重新导出 / 换一条交付），重试同一请求无用 |
| `idempotency_key_reused` | 409 `IDEMPOTENCY_CONFLICT` | 同键异体 |
| `PUBLICATION_KEY_TAKEN` | 409 `DUPLICATE_PUBLICATION` | 业务键在本项目已登记 |
| `PUBLICATION_NOT_FOUND`（WP37） | 404 | 含跨项目（先反查再鉴权，非成员同为 404） |
| `LINK_URL_INVALID` / `VERIFY_RESULT_INVALID`（WP37） | 422 | 改请求即可成功 |
| `LINK_CANONICAL_TAKEN`（WP37） | 409 `DUPLICATE_PUBLICATION` | 规范化后在本项目已登记 |
| `CROSS_PROJECT_NOTE`（WP37） | 409 `DUPLICATE_PUBLICATION` | 别的项目已登记且调用者不是管理者；`details.crossProject=true`，不带对方信息 |
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Header, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from rednote.modules.delivery import service
from rednote.modules.delivery.dependencies import DeliveryRepoDep
from rednote.modules.delivery.types import (
    PUBLICATION_STATE_DRAFT,
    PUBLICATION_STATES,
    REASON_ALREADY_REPLACED,
    REASON_CROSS_PROJECT_NOTE,
    REASON_DELIVERY_CANCELLED,
    REASON_DELIVERY_NOT_FOUND,
    REASON_DISPLAY_NAME_BLANK,
    REASON_ELIGIBILITY_STALE,
    REASON_ELIGIBILITY_UNAVAILABLE,
    REASON_EXPORT_ITEM_NOT_FOUND,
    REASON_IDEMPOTENCY_KEY_REUSED,
    REASON_INVALID_TRANSITION,
    REASON_KOC_NOT_FOUND,
    REASON_LINK_CANONICAL_TAKEN,
    REASON_LINK_URL_INVALID,
    REASON_PROJECT_NOT_FOUND,
    REASON_PUBLICATION_KEY_INVALID,
    REASON_PUBLICATION_KEY_TAKEN,
    REASON_PUBLICATION_NOT_FOUND,
    REASON_PUBLISHED_AT_SHAPE,
    REASON_VERIFY_RESULT_INVALID,
    MAX_LINK_URL_CHARS,
)
from rednote.modules.delivery.urls import UrlInvalid, normalize_url
from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.projects.dependencies import (
    ProjectRepoDep,
    resolve_project_access,
)
from rednote.modules.projects.permissions import (
    OrgAction,
    ProjectAction,
    allows_org_action,
)
from rednote.platform.api_responses import READ_RESPONSES, WRITE_RESPONSES
from rednote.platform.errors import ApiError, DataEnvelope, ErrorCode, build_meta
from rednote.platform.pagination import (
    CursorSpec,
    PageParamsDep,
    PaginatedEnvelope,
    list_response,
    paginate_items,
)

router = APIRouter(tags=["delivery"])

#: 列表端点的全量扫描上限（`platform.pagination` 是内存分页，须拿全量，
#: 否则静默截断）。与 `exports/api.py:74` 同值同理由。
_FULL_SCAN_LIMIT: int = 10_000

_NOT_FOUND_REASONS = frozenset(
    {
        REASON_PROJECT_NOT_FOUND,
        REASON_DELIVERY_NOT_FOUND,
        REASON_KOC_NOT_FOUND,
        REASON_EXPORT_ITEM_NOT_FOUND,
        REASON_PUBLICATION_NOT_FOUND,
    }
)
_VALIDATION_FIELD: dict[str, str] = {
    REASON_DISPLAY_NAME_BLANK: "displayName",
    REASON_PUBLICATION_KEY_INVALID: "publicationKey",
    REASON_PUBLISHED_AT_SHAPE: "publishedAt",
    "idempotency_key_invalid": "Idempotency-Key",
    REASON_LINK_URL_INVALID: "rawUrl",
    REASON_VERIFY_RESULT_INVALID: "result",
}
_STATE_REASONS = frozenset(
    {
        REASON_INVALID_TRANSITION,
        REASON_DELIVERY_CANCELLED,
        REASON_ALREADY_REPLACED,
        REASON_ELIGIBILITY_STALE,
        REASON_ELIGIBILITY_UNAVAILABLE,
    }
)


def _fail(exc: service.DeliveryError) -> NoReturn:
    """把业务错误翻成协议错误（见模块 docstring 的翻码表）。未登记的 reason 当 500 暴露。"""
    reason = exc.reason
    details: dict[str, Any] = {"reason": reason, **exc.details}
    message = str(exc)
    if reason in _NOT_FOUND_REASONS:
        raise ApiError(ErrorCode.RESOURCE_NOT_FOUND, message, details=details)
    if reason in _VALIDATION_FIELD:
        field = str(details.pop("field", _VALIDATION_FIELD[reason]))
        details["fieldErrors"] = [
            {"field": field, "type": reason, "message": message}
        ]
        raise ApiError(ErrorCode.VALIDATION_FAILED, message, details=details)
    if reason in _STATE_REASONS:
        raise ApiError(ErrorCode.INVALID_TRANSITION, message, details=details)
    if reason == REASON_IDEMPOTENCY_KEY_REUSED:
        raise ApiError(ErrorCode.IDEMPOTENCY_CONFLICT, message, details=details)
    if reason in (
        REASON_PUBLICATION_KEY_TAKEN,
        REASON_LINK_CANONICAL_TAKEN,
        REASON_CROSS_PROJECT_NOTE,
    ):
        raise ApiError(ErrorCode.DUPLICATE_PUBLICATION, message, details=details)
    raise exc  # 未登记 ⇒ 500，不静默吞成别的码


# ---------------------------------------------------------------------------
# 请求 / 响应体
# ---------------------------------------------------------------------------


class KocRecordCreateBody(BaseModel):
    """`POST /projects/{projectId}/koc-records` 的请求体。

    **没有 `homeUrlCanonical`**：它是 WP37 规范器的产物，客户端传了也该被忽略
    —— 与其静默忽略，不如 `extra="forbid"` 当场拒绝。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    display_name: str = Field(alias="displayName")
    platform_home_url: str | None = Field(default=None, alias="platformHomeUrl")
    contact_ref: str | None = Field(default=None, alias="contactRef")
    note: str | None = None


class DeliveryCreateBody(BaseModel):
    """`POST /projects/{projectId}/deliveries` 的请求体。

    **没有 `versionNodeId` / `contentItemId`**：三个冻结字段全部从
    `exportItemId` 指向的那一行复制。客户端传版本等于让陈旧缓存决定交付哪一版
    （WP35 已为同一问题否决过这个形态，`exports/api.py:117-122`）。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    export_item_id: uuid.UUID = Field(alias="exportItemId")
    koc_record_id: uuid.UUID | None = Field(default=None, alias="kocRecordId")
    note: str | None = None


class DeliveryReplaceBody(BaseModel):
    """`POST /deliveries/{deliveryId}/replacements` 的请求体。

    `kocRecordId` 不传 ⇒ 继承旧行的 KOC。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    export_item_id: uuid.UUID = Field(alias="exportItemId")
    koc_record_id: uuid.UUID | None = Field(default=None, alias="kocRecordId")
    note: str | None = None


class PublicationCreateBody(BaseModel):
    """`POST /projects/{projectId}/publications` 的请求体。

    **没有 `contentState`**：本包不抓取，一律落 `UNVERIFIED`，由客户端指定
    "已核验"就等于让客户端声称自己验过（而没人验过）。
    **没有 `urlCanonical`**：WP37 规范器产。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    publication_key: str = Field(alias="publicationKey")
    delivery_id: uuid.UUID | None = Field(default=None, alias="deliveryId")
    note_id: str | None = Field(default=None, alias="noteId")
    note_url: str | None = Field(default=None, alias="noteUrl")
    state: str = Field(default=PUBLICATION_STATE_DRAFT)
    published_at: datetime | None = Field(default=None, alias="publishedAt")


class KocRecordBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: uuid.UUID
    project_id: uuid.UUID = Field(alias="projectId")
    display_name: str = Field(alias="displayName")
    platform_home_url: str | None = Field(default=None, alias="platformHomeUrl")
    home_url_canonical: str | None = Field(default=None, alias="homeUrlCanonical")
    contact_ref: str | None = Field(default=None, alias="contactRef")
    note: str | None = None
    created_at: str | None = Field(default=None, alias="createdAt")


class DeliveryBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: uuid.UUID
    project_id: uuid.UUID = Field(alias="projectId")
    koc_record_id: uuid.UUID | None = Field(default=None, alias="kocRecordId")
    export_item_id: uuid.UUID = Field(alias="exportItemId")
    content_item_id: uuid.UUID = Field(alias="contentItemId")
    version_node_id: uuid.UUID = Field(alias="versionNodeId")
    state: str
    eligibility_hash: str = Field(alias="eligibilityHash")
    replaces_delivery_id: uuid.UUID | None = Field(
        default=None, alias="replacesDeliveryId"
    )
    delivered_at: str | None = Field(default=None, alias="deliveredAt")
    cancelled_at: str | None = Field(default=None, alias="cancelledAt")
    delivered_by: uuid.UUID | None = Field(default=None, alias="deliveredBy")
    created_by: uuid.UUID = Field(alias="createdBy")
    note: str | None = None
    created_at: str | None = Field(default=None, alias="createdAt")
    lock_version: int = Field(alias="lockVersion")
    #: 写端点回放时为 `False`（幂等命中），新建时 `True`；读端点不带。
    created: bool | None = None


class PublicationBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: uuid.UUID
    project_id: uuid.UUID = Field(alias="projectId")
    delivery_id: uuid.UUID | None = Field(default=None, alias="deliveryId")
    publication_key: str = Field(alias="publicationKey")
    note_id: str | None = Field(default=None, alias="noteId")
    note_url: str | None = Field(default=None, alias="noteUrl")
    url_canonical: str | None = Field(default=None, alias="urlCanonical")
    state: str
    content_state: str = Field(alias="contentState")
    published_at: str | None = Field(default=None, alias="publishedAt")
    created_at: str | None = Field(default=None, alias="createdAt")
    lock_version: int = Field(alias="lockVersion")
    created: bool | None = None


class UnmarkedExportBody(BaseModel):
    """「已导出未标记」的一行。**不是**「已下载未标记」（本仓无下载事件表）。"""

    model_config = ConfigDict(populate_by_name=True)

    content_item_id: uuid.UUID = Field(alias="contentItemId")
    last_export_at: str | None = Field(default=None, alias="lastExportAt")


class PublicationLinkCreateBody(BaseModel):
    """`POST /publications/{publicationId}/links` 的请求体。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    raw_url: str = Field(alias="rawUrl", max_length=MAX_LINK_URL_CHARS)


class PublicationVerifyBody(BaseModel):
    """`POST /publications/{publicationId}/verifications` 的请求体。

    `result` 在 service 层校验取值域（非法值 ⇒ 422 `VERIFY_RESULT_INVALID`）。
    `note` 契约里有，但 `publications` 没有对应列（本包不扩表）⇒ 接收、**不落库**。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    result: str
    note: str | None = None


class PublicationLinkBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: uuid.UUID
    publication_id: uuid.UUID = Field(alias="publicationId")
    raw_url: str = Field(alias="rawUrl")
    canonical_url: str = Field(alias="canonicalUrl")
    link_kind: str = Field(alias="linkKind")


class PublicationLinkCreatedBody(PublicationLinkBody):
    """登记响应。比契约多一个布尔 `crossProjectConfirmed`（约定 §3.3，OP）。"""

    cross_project_confirmed: bool = Field(alias="crossProjectConfirmed")


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _koc_view(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "projectId": str(row.project_id),
        "displayName": row.display_name,
        "platformHomeUrl": row.platform_home_url,
        "homeUrlCanonical": row.home_url_canonical,
        "contactRef": row.contact_ref,
        "note": row.note,
        "createdAt": _iso(row.created_at),
    }


def _delivery_view(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "projectId": str(row.project_id),
        "kocRecordId": None if row.koc_record_id is None else str(row.koc_record_id),
        "exportItemId": str(row.export_item_id),
        "contentItemId": str(row.content_item_id),
        "versionNodeId": str(row.version_node_id),
        "state": row.state,
        "eligibilityHash": row.eligibility_hash,
        "replacesDeliveryId": (
            None if row.replaces_delivery_id is None else str(row.replaces_delivery_id)
        ),
        "deliveredAt": _iso(row.delivered_at),
        "cancelledAt": _iso(row.cancelled_at),
        "deliveredBy": None if row.delivered_by is None else str(row.delivered_by),
        "createdBy": str(row.created_by),
        "note": row.note,
        "createdAt": _iso(row.created_at),
        "lockVersion": int(row.lock_version),
    }


def _publication_view(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "projectId": str(row.project_id),
        "deliveryId": None if row.delivery_id is None else str(row.delivery_id),
        "publicationKey": row.publication_key,
        "noteId": row.note_id,
        "noteUrl": row.note_url,
        "urlCanonical": row.url_canonical,
        "state": row.state,
        "contentState": row.content_state,
        "publishedAt": _iso(row.published_at),
        "createdAt": _iso(row.created_at),
        "lockVersion": int(row.lock_version),
    }


def _link_view(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "publicationId": str(row.publication_id),
        "rawUrl": row.raw_url,
        "canonicalUrl": row.canonical_url,
        "linkKind": row.link_kind,
    }


def _authorize(
    project_repo: ProjectRepoDep,
    user: CurrentUserDep,
    project_id: uuid.UUID,
    action: ProjectAction,
) -> None:
    """两步鉴权：能看到项目（否则 404）→ 具备动作权限（否则 403）。"""
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(action)


def _authorize_item(
    project_repo: ProjectRepoDep,
    user: CurrentUserDep,
    project_id: uuid.UUID,
    action: ProjectAction,
    not_found: service.DeliveryError,
) -> None:
    """item 级鉴权：非成员 ⇒ 重翻成与「不存在」**完全一致**的 404。

    `resolve_project_access` 对非成员的默认 404 带「项目不存在或无权访问」+ 所属
    `projectId`，与「X 不存在」的 message / details 不同形，调用者能据此区分
    「存在但无权」与「不存在」，还能拿到别的项目的 id。所以 item 级路径统一走这里。
    其它错误码（如成员但无权的 403）原样抛出。
    """
    try:
        _authorize(project_repo, user, project_id, action)
    except ApiError as exc:
        if exc.code != ErrorCode.RESOURCE_NOT_FOUND:
            raise
        _fail(not_found)


def _authorized_delivery(
    repo: DeliveryRepoDep,
    project_repo: ProjectRepoDep,
    user: CurrentUserDep,
    delivery_id: uuid.UUID,
    action: ProjectAction,
) -> Any:
    """item 级寻址：先按 id 反查项目再鉴权。跨项目 / 非成员 ⇒ 404（与不存在同形）。"""
    try:
        row = service.get_delivery(repo, delivery_id=delivery_id, project_id=None)
    except service.DeliveryError as exc:
        _fail(exc)
    _authorize_item(
        project_repo, user, row.project_id, action,
        service.DeliveryError(
            REASON_DELIVERY_NOT_FOUND, "交付记录不存在。", deliveryId=str(delivery_id)
        ),
    )
    return row


def _authorized_publication(
    repo: DeliveryRepoDep,
    project_repo: ProjectRepoDep,
    user: CurrentUserDep,
    publication_id: uuid.UUID,
    action: ProjectAction,
) -> Any:
    """item 级寻址：先按 id 反查项目再鉴权。跨项目 / 非成员 ⇒ 404（与不存在同形）。"""
    try:
        row = service.get_publication(repo, publication_id=publication_id)
    except service.DeliveryError as exc:
        _fail(exc)
    _authorize_item(
        project_repo, user, row.project_id, action,
        service.DeliveryError(
            REASON_PUBLICATION_NOT_FOUND, "发布记录不存在。", publicationId=str(publication_id)
        ),
    )
    return row


def _page(views: list[dict[str, Any]], params: PageParamsDep, project_id: uuid.UUID,
          *, id_key: str) -> dict[str, Any]:
    spec = CursorSpec(
        sort_fields=("createdAt",),
        direction="desc",
        filters={"projectId": str(project_id)},
    )
    window, page = paginate_items(
        views,
        params,
        spec,
        sort_key=lambda view: (view["createdAt"] or "",),
        id_of=lambda view: view[id_key],
    )
    return list_response(window, page)


# ---------------------------------------------------------------------------
# ①② KOC 记录
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/koc-records",
    response_model=PaginatedEnvelope[KocRecordBody],
    responses=READ_RESPONSES,
    summary="项目下的 KOC 记录列表（按创建时间倒序，游标分页）",
)
def list_koc_records(
    user: CurrentUserDep,
    repo: DeliveryRepoDep,
    project_repo: ProjectRepoDep,
    params: PageParamsDep,
    project_id: Annotated[uuid.UUID, Path(alias="projectId")],
) -> dict[str, Any]:
    _authorize(project_repo, user, project_id, ProjectAction.VIEW)
    views = [
        _koc_view(row) for row in repo.list_koc_records(project_id, limit=_FULL_SCAN_LIMIT)
    ]
    return _page(views, params, project_id, id_key="id")


@router.post(
    "/projects/{projectId}/koc-records",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[KocRecordBody],
    responses=WRITE_RESPONSES,
    summary="建一条 KOC 记录（轻量：不存平台密码，主页链接只做提示不做唯一）",
)
def create_koc_record(
    user: CurrentUserDep,
    repo: DeliveryRepoDep,
    project_repo: ProjectRepoDep,
    project_id: Annotated[uuid.UUID, Path(alias="projectId")],
    body: KocRecordCreateBody,
) -> dict[str, Any]:
    """KOC **不是系统用户** —— 本端点只登记展示名、主页链接与联系方式的**引用**。"""
    _authorize(project_repo, user, project_id, ProjectAction.DELIVER)
    try:
        row = service.create_koc_record(
            repo,
            project_id=project_id,
            display_name=body.display_name,
            platform_home_url=body.platform_home_url,
            contact_ref=body.contact_ref,
            note=body.note,
        )
    except service.DeliveryError as exc:
        _fail(exc)
    return {"data": _koc_view(row), "meta": build_meta()}


# ---------------------------------------------------------------------------
# ③④ 交付：列表 / 创建
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/deliveries",
    response_model=PaginatedEnvelope[DeliveryBody],
    responses=READ_RESPONSES,
    summary="项目下的交付记录列表（按创建时间倒序，游标分页）",
)
def list_deliveries(
    user: CurrentUserDep,
    repo: DeliveryRepoDep,
    project_repo: ProjectRepoDep,
    params: PageParamsDep,
    project_id: Annotated[uuid.UUID, Path(alias="projectId")],
) -> dict[str, Any]:
    _authorize(project_repo, user, project_id, ProjectAction.VIEW)
    views = [
        _delivery_view(row)
        for row in repo.list_deliveries(project_id, limit=_FULL_SCAN_LIMIT)
    ]
    return _page(views, params, project_id, id_key="id")


@router.post(
    "/projects/{projectId}/deliveries",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[DeliveryBody],
    responses=WRITE_RESPONSES,
    summary="基于一个导出行建交付（PREPARED，201；同键同体回放 200）",
)
def create_delivery(
    response: Response,
    user: CurrentUserDep,
    repo: DeliveryRepoDep,
    project_repo: ProjectRepoDep,
    project_id: Annotated[uuid.UUID, Path(alias="projectId")],
    body: DeliveryCreateBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """版本由服务端从 `exportItemId` 那一行**复制**，客户端不传版本。

    新建的交付一律是 `PREPARED` —— **建交付不等于已交付**，要显式调 ⑥。
    """
    _authorize(project_repo, user, project_id, ProjectAction.DELIVER)
    try:
        result = service.create_delivery(
            repo,
            project_id=project_id,
            export_item_id=body.export_item_id,
            koc_record_id=body.koc_record_id,
            note=body.note,
            idempotency_key=idempotency_key,
            created_by=user.id,
        )
    except service.DeliveryError as exc:
        _fail(exc)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    view = _delivery_view(result.delivery)
    view["created"] = result.created
    return {"data": view, "meta": build_meta()}


# ---------------------------------------------------------------------------
# ⑤ 交付详情（item 级）
# ---------------------------------------------------------------------------


@router.get(
    "/deliveries/{deliveryId}",
    response_model=DataEnvelope[DeliveryBody],
    responses=READ_RESPONSES,
    summary="交付详情（跨项目 id 与不存在同形 404）",
)
def get_delivery(
    user: CurrentUserDep,
    repo: DeliveryRepoDep,
    project_repo: ProjectRepoDep,
    delivery_id: Annotated[uuid.UUID, Path(alias="deliveryId")],
) -> dict[str, Any]:
    """`versionNodeId` 返回的是**交付时冻结的那一版** —— 内容后来改了多少稿都不变。"""
    row = _authorized_delivery(
        repo, project_repo, user, delivery_id, ProjectAction.VIEW
    )
    return {"data": _delivery_view(row), "meta": build_meta()}


# ---------------------------------------------------------------------------
# ⑥⑦ 交付：标记已交付 / 取消
# ---------------------------------------------------------------------------


@router.post(
    "/deliveries/{deliveryId}/confirmations",
    response_model=DataEnvelope[DeliveryBody],
    responses=WRITE_RESPONSES,
    summary="标记已交付（下载不计交付：只有本端点置 deliveredAt）",
)
def confirm_delivery(
    user: CurrentUserDep,
    repo: DeliveryRepoDep,
    project_repo: ProjectRepoDep,
    delivery_id: Annotated[uuid.UUID, Path(alias="deliveryId")],
) -> dict[str, Any]:
    """`PREPARED → DELIVERED`。

    - **先做资格复验**：按当前正文版本与正式图重算资格指纹，与交付行上的快照
      比对。不等 ⇒ 409 `INVALID_TRANSITION`（`reason=ELIGIBILITY_STALE`）。
    - 已 `DELIVERED` 再调 ⇒ **200 幂等**，`deliveredAt` 不变。
    - `CANCELLED → DELIVERED` ⇒ 409。
    """
    row = _authorized_delivery(
        repo, project_repo, user, delivery_id, ProjectAction.DELIVER
    )
    try:
        result = service.confirm_delivery(
            repo, delivery_id=row.id, confirmed_by=user.id
        )
    except service.DeliveryError as exc:
        _fail(exc)
    view = _delivery_view(result.delivery)
    view["created"] = result.created
    return {"data": view, "meta": build_meta()}


@router.post(
    "/deliveries/{deliveryId}/cancellations",
    response_model=DataEnvelope[DeliveryBody],
    responses=WRITE_RESPONSES,
    summary="取消一条尚未交付的交付（已交付是终态，不可撤回）",
)
def cancel_delivery(
    user: CurrentUserDep,
    repo: DeliveryRepoDep,
    project_repo: ProjectRepoDep,
    delivery_id: Annotated[uuid.UUID, Path(alias="deliveryId")],
) -> dict[str, Any]:
    row = _authorized_delivery(
        repo, project_repo, user, delivery_id, ProjectAction.DELIVER
    )
    try:
        result = service.cancel_delivery(repo, delivery_id=row.id)
    except service.DeliveryError as exc:
        _fail(exc)
    view = _delivery_view(result.delivery)
    view["created"] = result.created
    return {"data": view, "meta": build_meta()}


# ---------------------------------------------------------------------------
# ⑧ 交付：替换（追加一行）
# ---------------------------------------------------------------------------


@router.post(
    "/deliveries/{deliveryId}/replacements",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[DeliveryBody],
    responses=WRITE_RESPONSES,
    summary="替换交付（追加新行 + replacesDeliveryId，旧行一个字不改）",
)
def replace_delivery(
    response: Response,
    user: CurrentUserDep,
    repo: DeliveryRepoDep,
    project_repo: ProjectRepoDep,
    delivery_id: Annotated[uuid.UUID, Path(alias="deliveryId")],
    body: DeliveryReplaceBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """替换链是**追加**：旧行的 `state` / `deliveredAt` / `lockVersion` 全不变。

    一条旧交付最多被替换一次（再替换 ⇒ 409 `ALREADY_REPLACED`，请替换最新那条）。
    新的导出行必须属于**同一条内容** —— 换内容是另一次交付，不是替换。
    """
    row = _authorized_delivery(
        repo, project_repo, user, delivery_id, ProjectAction.DELIVER
    )
    try:
        result = service.replace_delivery(
            repo,
            delivery_id=row.id,
            export_item_id=body.export_item_id,
            koc_record_id=body.koc_record_id,
            note=body.note,
            idempotency_key=idempotency_key,
            created_by=user.id,
        )
    except service.DeliveryError as exc:
        _fail(exc)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    view = _delivery_view(result.delivery)
    view["created"] = result.created
    return {"data": view, "meta": build_meta()}


# ---------------------------------------------------------------------------
# ⑨⑩ 发布登记
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/publications",
    response_model=PaginatedEnvelope[PublicationBody],
    responses=READ_RESPONSES,
    summary="项目下的发布登记列表（按创建时间倒序，游标分页）",
)
def list_publications(
    user: CurrentUserDep,
    repo: DeliveryRepoDep,
    project_repo: ProjectRepoDep,
    params: PageParamsDep,
    project_id: Annotated[uuid.UUID, Path(alias="projectId")],
) -> dict[str, Any]:
    _authorize(project_repo, user, project_id, ProjectAction.VIEW)
    views = [
        _publication_view(row)
        for row in repo.list_publications(project_id, limit=_FULL_SCAN_LIMIT)
    ]
    return _page(views, params, project_id, id_key="id")


@router.post(
    "/projects/{projectId}/publications",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[PublicationBody],
    responses=WRITE_RESPONSES,
    summary="登记一条发布（草稿 → 实发映射；201，同键同体回放 200）",
)
def create_publication(
    response: Response,
    user: CurrentUserDep,
    repo: DeliveryRepoDep,
    project_repo: ProjectRepoDep,
    project_id: Annotated[uuid.UUID, Path(alias="projectId")],
    body: PublicationCreateBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """`noteId` / `noteUrl` 存**原样值**：规范化、去重、可达性验证全归 WP37。

    `contentState` 一律落 `UNVERIFIED`（本包不抓取）。
    ⚠️ `UNVERIFIED` **不等于**「笔记已删除」。
    """
    _authorize(project_repo, user, project_id, ProjectAction.DELIVER)
    if body.state not in PUBLICATION_STATES:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "发布状态取值不合法。",
            details={
                "reason": "publication_state_invalid",
                "fieldErrors": [
                    {
                        "field": "state",
                        "type": "publication_state_invalid",
                        "message": f"只允许 {', '.join(PUBLICATION_STATES)}。",
                    }
                ],
            },
        )
    try:
        result = service.create_publication(
            repo,
            project_id=project_id,
            publication_key=body.publication_key,
            delivery_id=body.delivery_id,
            note_id=body.note_id,
            note_url=body.note_url,
            state=body.state,
            published_at=body.published_at,
            idempotency_key=idempotency_key,
            created_by=user.id,
        )
    except service.DeliveryError as exc:
        _fail(exc)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    view = _publication_view(result.publication)
    view["created"] = result.created
    return {"data": view, "meta": build_meta()}


# ---------------------------------------------------------------------------
# ⑪ 「已导出未标记」投影
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/unmarked-exports",
    response_model=DataEnvelope[list[UnmarkedExportBody]],
    responses=READ_RESPONSES,
    summary="已导出但还没有任何「已交付」记录的内容（不是「已下载未标记」）",
)
def list_unmarked_exports(
    user: CurrentUserDep,
    repo: DeliveryRepoDep,
    project_repo: ProjectRepoDep,
    project_id: Annotated[uuid.UUID, Path(alias="projectId")],
) -> dict[str, Any]:
    """口径是「已**导出**未标记」。

    本仓**没有下载事件表**（WP35 的 download 端点零写操作）⇒「下载过但未标记」
    在磁盘上没有载体。沿用原型的「已下载未标记」字样就是声称有数据而没有。
    """
    _authorize(project_repo, user, project_id, ProjectAction.VIEW)
    rows = repo.exported_not_marked(project_id)
    return {
        "data": [
            {"contentItemId": str(cid), "lastExportAt": _iso(last)}
            for cid, last in rows
        ],
        "meta": build_meta(),
    }


# ---------------------------------------------------------------------------
# ⑫⑬⑭ 发布链接 / 笔记去重 / 内容状态（WP37）
# ---------------------------------------------------------------------------


@router.post(
    "/publications/{publicationId}/links",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[PublicationLinkCreatedBody],
    responses=WRITE_RESPONSES,
    summary="登记发布链接（规范化后入库；项目内重复 409，跨项目命中须管理者登记）",
)
def add_publication_link(
    user: CurrentUserDep,
    repo: DeliveryRepoDep,
    project_repo: ProjectRepoDep,
    publication_id: Annotated[uuid.UUID, Path(alias="publicationId")],
    body: PublicationLinkCreateBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """规范化只做平台无关的保守处理，**不识别短链**（`linkKind` 只会是 LONG / OTHER），
    因此同一条笔记的短链与长链会被当成两条不同链接（已知缺口，WP37 说明 §已知缺口）。

    跨项目命中：非管理者 ⇒ 409（`details.crossProject=true`，不泄露对方项目）；
    管理者本人调用 ⇒ 放行并带 `crossProjectConfirmed=true`。
    """
    row = _authorized_publication(
        repo, project_repo, user, publication_id, ProjectAction.DELIVER
    )
    try:
        result = service.add_publication_link(
            repo,
            publication_id=row.id,
            raw_url=body.raw_url,
            can_confirm_cross_project=allows_org_action(
                is_admin=user.is_admin, action=OrgAction.CONFIRM_CROSS_PROJECT_NOTE
            ),
            idempotency_key=idempotency_key,
        )
    except service.DeliveryError as exc:
        _fail(exc)
    view = _link_view(result.link)
    view["crossProjectConfirmed"] = result.cross_project_confirmed
    return {"data": view, "meta": build_meta()}


@router.get(
    "/publication-links",
    response_model=PaginatedEnvelope[PublicationLinkBody],
    responses=READ_RESPONSES,
    summary="按规范化链接查重（跨项目；非管理者只见自己所在项目的命中）",
)
def list_publication_links(
    user: CurrentUserDep,
    repo: DeliveryRepoDep,
    params: PageParamsDep,
    canonical_url: Annotated[
        str, Query(alias="canonicalUrl", max_length=MAX_LINK_URL_CHARS)
    ],
) -> dict[str, Any]:
    """查询值会先过一遍规范化（对已规范的值是恒等），所以贴原始链接也能命中。"""
    try:
        canonical = normalize_url(canonical_url).canonical
    except UrlInvalid as exc:
        _fail(
            service.DeliveryError(
                REASON_LINK_URL_INVALID, str(exc), field="canonicalUrl"
            )
        )
    rows = repo.list_links_by_canonical(
        canonical,
        visible_to_user=None if user.is_admin else user.id,
        limit=_FULL_SCAN_LIMIT,
    )
    views = [{**_link_view(r), "createdAt": _iso(r.created_at)} for r in rows]
    spec = CursorSpec(
        sort_fields=("createdAt",),
        direction="desc",
        filters={"canonicalUrl": canonical},
    )
    window, page = paginate_items(
        views,
        params,
        spec,
        sort_key=lambda view: (view["createdAt"] or "",),
        id_of=lambda view: view["id"],
    )
    for view in window:
        view.pop("createdAt", None)
    return list_response(window, page)


@router.post(
    "/publications/{publicationId}/verifications",
    response_model=DataEnvelope[PublicationBody],
    responses=WRITE_RESPONSES,
    summary="写入内容核验状态（只接受 VERIFIED / UNVERIFIED；不联网）",
)
def verify_publication(
    user: CurrentUserDep,
    repo: DeliveryRepoDep,
    project_repo: ProjectRepoDep,
    publication_id: Annotated[uuid.UUID, Path(alias="publicationId")],
    body: PublicationVerifyBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """**无法访问 ≠ 已删除**：取值域没有"已删除"。结果由调用方给出，本端点不抓取。"""
    row = _authorized_publication(
        repo, project_repo, user, publication_id, ProjectAction.DELIVER
    )
    try:
        fresh = service.verify_publication(
            repo,
            publication_id=row.id,
            result=body.result,
            idempotency_key=idempotency_key,
        )
    except service.DeliveryError as exc:
        _fail(exc)
    return {"data": _publication_view(fresh), "meta": build_meta()}


__all__ = ["router"]
