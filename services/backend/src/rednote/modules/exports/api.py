"""固定版本 ZIP 导出的端点（WP35；协议原件不在本机，形状由本包定义）。

| # | 方法 路径 | 权限 | 说明 |
|---|---|---|---|
| ① | `POST /projects/{projectId}/exports` | `DELIVER` | 同步打包，201；支持 `Idempotency-Key` |
| ② | `GET /projects/{projectId}/exports` | `VIEW` | 导出组列表（游标分页） |
| ③ | `GET /projects/{projectId}/exports/{exportGroupId}/download` | `DELIVER` | 流式下载；`ETag = zip_sha256` |
| ④ | `GET /projects/{projectId}/exports/{exportGroupId}/verification` | `DELIVER` | 摘要核验：重算落盘 ZIP 的 sha256 并与登记值比对 |

鉴权两步：先 `resolve_project_access`（非成员 ⇒ 404，防枚举），再 `access.require(...)`。
`ProjectAccessDep` 只回答"能不能看到项目"，**每个端点必须自己 `require`**。

## 错误码翻译（唯一判据：换一个请求参数能否成功？）

| reason | 协议码 | 说明 |
|---|---|---|
| `PROJECT_NOT_FOUND` / `CONTENT_NOT_FOUND` / `EXPORT_NOT_FOUND` / `ARTIFACT_MISSING` | 404 | 找不到（含跨项目，与不存在同形） |
| `CONTENT_IDS_EMPTY` / `_TOO_MANY` / `_DUPLICATE` / `FORMAT_NOT_SUPPORTED` | 422 | 改请求即可成功 |
| `CONTENT_NO_VERSION` / `NO_DELIVERY_ASSET` | 409 `INVALID_TRANSITION` | 要等资源状态变化（出图/写正文），重试同一请求无用 |
| `idempotency_key_reused` | 409 `IDEMPOTENCY_CONFLICT` | 同键异体 |
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Header, Path, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from rednote.modules.exports import service
from rednote.modules.exports.dependencies import (
    ExportRepoDep,
    FileRepoDep,
    StorageBackendDep,
)
from rednote.modules.exports.types import (
    EXPORT_FORMAT_STATIC,
    MAX_IDEMPOTENCY_KEY_CHARS,
    REASON_ARTIFACT_MISSING,
    REASON_CONTENT_IDS_DUPLICATE,
    REASON_CONTENT_IDS_EMPTY,
    REASON_CONTENT_IDS_TOO_MANY,
    REASON_CONTENT_NOT_FOUND,
    REASON_CONTENT_NO_VERSION,
    REASON_EXPORT_NOT_FOUND,
    REASON_FORMAT_NOT_SUPPORTED,
    REASON_IDEMPOTENCY_KEY_REUSED,
    REASON_NO_DELIVERY_ASSET,
    REASON_PROJECT_NOT_FOUND,
    ZIP_MIME_TYPE,
)
from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.projects.dependencies import (
    ProjectRepoDep,
    resolve_project_access,
)
from rednote.modules.projects.permissions import ProjectAction
from rednote.platform.api_responses import READ_RESPONSES, WRITE_RESPONSES
from rednote.platform.errors import ApiError, DataEnvelope, ErrorCode, build_meta
from rednote.platform.pagination import (
    CursorSpec,
    PageParamsDep,
    PaginatedEnvelope,
    list_response,
    paginate_items,
)

router = APIRouter(tags=["exports"])

#: 列表端点的全量扫描上限（`platform.pagination` 是内存分页，须拿全量，否则静默截断）。
_FULL_SCAN_LIMIT: int = 10_000

_NOT_FOUND_REASONS = frozenset(
    {
        REASON_PROJECT_NOT_FOUND,
        REASON_CONTENT_NOT_FOUND,
        REASON_EXPORT_NOT_FOUND,
        REASON_ARTIFACT_MISSING,
    }
)
_VALIDATION_FIELD: dict[str, str] = {
    REASON_CONTENT_IDS_EMPTY: "contentItemIds",
    REASON_CONTENT_IDS_TOO_MANY: "contentItemIds",
    REASON_CONTENT_IDS_DUPLICATE: "contentItemIds",
    REASON_FORMAT_NOT_SUPPORTED: "format",
}
_STATE_REASONS = frozenset({REASON_CONTENT_NO_VERSION, REASON_NO_DELIVERY_ASSET})


def _fail(exc: service.ExportError) -> NoReturn:
    """把业务错误翻成协议错误（见模块 docstring 的翻码表）。未登记的 reason 当 500 暴露。"""
    reason = exc.reason
    details: dict[str, Any] = {"reason": reason, **exc.details}
    message = str(exc)
    if reason in _NOT_FOUND_REASONS:
        raise ApiError(ErrorCode.RESOURCE_NOT_FOUND, message, details=details)
    if reason in _VALIDATION_FIELD:
        details["fieldErrors"] = [
            {"field": _VALIDATION_FIELD[reason], "type": reason, "message": message}
        ]
        raise ApiError(ErrorCode.VALIDATION_FAILED, message, details=details)
    if reason in _STATE_REASONS:
        raise ApiError(ErrorCode.INVALID_TRANSITION, message, details=details)
    if reason == REASON_IDEMPOTENCY_KEY_REUSED:
        raise ApiError(ErrorCode.IDEMPOTENCY_CONFLICT, message, details=details)
    raise exc  # 未登记 ⇒ 500，不静默吞成别的码


# ---------------------------------------------------------------------------
# 请求 / 响应体
# ---------------------------------------------------------------------------


class ExportCreateBody(BaseModel):
    """`POST /projects/{projectId}/exports` 的请求体。

    **没有 `versionNodeId`**：版本由服务端按"当前正文版本"冻结，客户端传版本等于
    让客户端决定导出哪一版（陈旧缓存会导出旧版而不报错）。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    content_item_ids: list[uuid.UUID] = Field(alias="contentItemIds")
    export_format: str = Field(default=EXPORT_FORMAT_STATIC, alias="format")


class ExportGroupBody(BaseModel):
    """一组导出的响应形状（创建与列表共用）。"""

    model_config = ConfigDict(populate_by_name=True)

    export_group_id: uuid.UUID = Field(alias="exportGroupId")
    export_kind: str = Field(alias="exportKind")
    export_format: str = Field(alias="format")
    item_count: int = Field(alias="itemCount")
    content_item_ids: list[uuid.UUID] = Field(alias="contentItemIds")
    artifact_file_id: uuid.UUID = Field(alias="artifactFileId")
    zip_sha256: str = Field(alias="zipSha256")
    zip_byte_size: int = Field(alias="zipByteSize")
    created_by: uuid.UUID = Field(alias="createdBy")
    created_at: str | None = Field(default=None, alias="createdAt")
    created: bool | None = None


class ExportVerificationBody(BaseModel):
    """摘要核验结果。`matches=False` ⇒ 落盘字节与登记摘要不一致（损坏或被替换）。"""

    model_config = ConfigDict(populate_by_name=True)

    export_group_id: uuid.UUID = Field(alias="exportGroupId")
    matches: bool
    expected_sha256: str = Field(alias="expectedSha256")
    actual_sha256: str = Field(alias="actualSha256")
    expected_byte_size: int = Field(alias="expectedByteSize")
    actual_byte_size: int = Field(alias="actualByteSize")


def _group_view(rows: list, *, created: bool | None = None) -> dict[str, Any]:
    head = rows[0]
    return {
        "exportGroupId": str(head.export_group_id),
        "exportKind": head.export_kind,
        "format": head.export_format,
        "itemCount": int(head.item_count),
        "contentItemIds": [str(r.content_item_id) for r in rows],
        "artifactFileId": str(head.artifact_file_id),
        "zipSha256": head.zip_sha256,
        "zipByteSize": int(head.zip_byte_size),
        "createdBy": str(head.created_by),
        "createdAt": head.created_at.isoformat() if head.created_at else None,
        "created": created,
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


# ---------------------------------------------------------------------------
# ① 创建（同步打包）
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{projectId}/exports",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[ExportGroupBody],
    responses=WRITE_RESPONSES,
    summary="按一组内容 id 创建固定版本 ZIP 导出（同步，201；同键同体回放 200）",
)
def create_export(
    response: Response,
    user: CurrentUserDep,
    repo: ExportRepoDep,
    file_repo: FileRepoDep,
    storage: StorageBackendDep,
    project_repo: ProjectRepoDep,
    project_id: Annotated[uuid.UUID, Path(alias="projectId")],
    body: ExportCreateBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """版本由服务端冻结为"当前正文版本 + 该版本正式图"，客户端不传版本。

    幂等：同键同体 ⇒ 回放既有组（状态码 200，`created=false`）；同键异体 ⇒ 409。
    无键 ⇒ 不做幂等。并发同键撞部分唯一索引时回滚并重查回放。
    """
    _authorize(project_repo, user, project_id, ProjectAction.DELIVER)

    if idempotency_key is not None and not (
        1 <= len(idempotency_key) <= MAX_IDEMPOTENCY_KEY_CHARS
    ):
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "Idempotency-Key 长度不合法。",
            details={
                "reason": "idempotency_key_invalid",
                "fieldErrors": [
                    {
                        "field": "Idempotency-Key",
                        "type": "idempotency_key_invalid",
                        "message": f"长度须在 1~{MAX_IDEMPOTENCY_KEY_CHARS} 之间。",
                    }
                ],
            },
        )

    def _run() -> service.ExportResult:
        return service.create_export(
            repo,
            file_repo,
            storage,
            project_id=project_id,
            content_item_ids=body.content_item_ids,
            export_format=body.export_format,
            idempotency_key=idempotency_key,
            created_by=user.id,
        )

    try:
        result = _run()
    except service.ExportError as exc:
        _fail(exc)

    rows = repo.list_group(result.export_group_id)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return {"data": _group_view(rows, created=result.created), "meta": build_meta()}


# ---------------------------------------------------------------------------
# ② 列表
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/exports",
    response_model=PaginatedEnvelope[ExportGroupBody],
    responses=READ_RESPONSES,
    summary="项目下的导出组列表（按创建时间倒序，游标分页）",
)
def list_exports(
    user: CurrentUserDep,
    repo: ExportRepoDep,
    project_repo: ProjectRepoDep,
    params: PageParamsDep,
    project_id: Annotated[uuid.UUID, Path(alias="projectId")],
) -> dict[str, Any]:
    _authorize(project_repo, user, project_id, ProjectAction.VIEW)

    # 行 → 组：同组行共享组级属性，按 export_group_id 聚合后取首行做代表。
    grouped: dict[uuid.UUID, list] = {}
    for row in repo.list_groups_for_project(project_id, limit=_FULL_SCAN_LIMIT):
        grouped.setdefault(row.export_group_id, []).append(row)
    views = [
        _group_view(sorted(rows, key=lambda r: (r.created_at, str(r.id))))
        for rows in grouped.values()
    ]

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
        id_of=lambda view: view["exportGroupId"],
    )
    return list_response(window, page)



# ---------------------------------------------------------------------------
# ③ 下载（流式；ETag = zip_sha256）
# ---------------------------------------------------------------------------


def _artifact_key(repo: ExportRepoDep, group_rows: list) -> str:
    """产物文件对象 → 存储 key；文件记录丢失 ⇒ `ARTIFACT_MISSING`（404）。"""
    located = repo.file_blob_key(group_rows[0].artifact_file_id)
    if located is None:
        _fail(service.ExportError(REASON_ARTIFACT_MISSING, "导出产物的文件记录不存在。"))
    return located[1]


@router.get(
    "/projects/{projectId}/exports/{exportGroupId}/download",
    responses={
        200: {"content": {ZIP_MIME_TYPE: {}}, "description": "ZIP 字节流"},
        304: {"description": "If-None-Match 命中，客户端缓存仍有效"},
        **READ_RESPONSES,
    },
    summary="流式下载导出 ZIP（ETag = zip_sha256，支持 If-None-Match）",
)
def download_export(
    request: Request,
    user: CurrentUserDep,
    repo: ExportRepoDep,
    storage: StorageBackendDep,
    project_repo: ProjectRepoDep,
    project_id: Annotated[uuid.UUID, Path(alias="projectId")],
    export_group_id: Annotated[uuid.UUID, Path(alias="exportGroupId")],
) -> Response:
    """导出组按 `(project_id, export_group_id)` 双键寻址：别的项目的组 id 与不存在同形 404。

    ETag 是 `zip_sha256`（带引号的强校验器）：ZIP 确定性打包 ⇒ 同一组内容恒同摘要，
    所以缓存键有意义。`Content-Length` 取登记的 `zip_byte_size`。
    """
    _authorize(project_repo, user, project_id, ProjectAction.DELIVER)
    try:
        rows = service.get_group(
            repo, project_id=project_id, export_group_id=export_group_id
        )
    except service.ExportError as exc:
        _fail(exc)

    head = rows[0]
    etag = f'"{head.zip_sha256}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})

    key = _artifact_key(repo, rows)
    # 存在性在开流前探一次：否则缺字节要等到响应已开始才爆，客户端看到的是截断的 200。
    if not storage.exists(key=key):
        _fail(service.ExportError(REASON_ARTIFACT_MISSING, "导出产物的字节不存在。"))

    return StreamingResponse(
        storage.open_stream(key=key),
        media_type=ZIP_MIME_TYPE,
        headers={
            "ETag": etag,
            "Content-Length": str(int(head.zip_byte_size)),
            "Content-Disposition": f'attachment; filename="export-{str(export_group_id)[:8]}.zip"',
            "Cache-Control": "private, no-transform",
        },
    )


# ---------------------------------------------------------------------------
# ④ 摘要核验（增强项）
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/exports/{exportGroupId}/verification",
    response_model=DataEnvelope[ExportVerificationBody],
    responses=READ_RESPONSES,
    summary="核验落盘 ZIP 的 sha256 / 字节数是否与登记值一致",
)
def verify_export(
    user: CurrentUserDep,
    repo: ExportRepoDep,
    storage: StorageBackendDep,
    project_repo: ProjectRepoDep,
    project_id: Annotated[uuid.UUID, Path(alias="projectId")],
    export_group_id: Annotated[uuid.UUID, Path(alias="exportGroupId")],
) -> dict[str, Any]:
    """分块重算落盘字节的摘要，**不进内存**。不一致 ⇒ `matches=false`（200，不是错误）：
    核验的结论本身就是响应内容，用错误码会让客户端分不清"核验失败"与"核验没做成"。"""
    import hashlib

    _authorize(project_repo, user, project_id, ProjectAction.DELIVER)
    try:
        rows = service.get_group(
            repo, project_id=project_id, export_group_id=export_group_id
        )
    except service.ExportError as exc:
        _fail(exc)

    head = rows[0]
    key = _artifact_key(repo, rows)
    if not storage.exists(key=key):
        _fail(service.ExportError(REASON_ARTIFACT_MISSING, "导出产物的字节不存在。"))

    digest = hashlib.sha256()
    total = 0
    for chunk in storage.open_stream(key=key):
        digest.update(chunk)
        total += len(chunk)

    actual = digest.hexdigest()
    return {
        "data": {
            "exportGroupId": str(export_group_id),
            "matches": actual == head.zip_sha256 and total == int(head.zip_byte_size),
            "expectedSha256": head.zip_sha256,
            "actualSha256": actual,
            "expectedByteSize": int(head.zip_byte_size),
            "actualByteSize": total,
        },
        "meta": build_meta(),
    }


__all__ = ["router"]
