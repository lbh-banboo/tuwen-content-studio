"""文件上传端点（协议 03 §4.2，共 6 个，逐条对照不多不少）。

| 方法 | 路径 | 成功码 | 依据 |
|---|---|---|---|
| POST | `/projects/{projectId}/uploads` | 201 | §4.2 第 1 行 |
| GET | `/uploads/{uploadId}` | 200 | §4.2 第 2 行 |
| PUT | `/uploads/{uploadId}/parts/{partNo}` | 200 | §4.2 第 3 行 |
| POST | `/uploads/{uploadId}/completions` | 202 | §4.2 第 4 行 |
| DELETE | `/uploads/{uploadId}` | 204 | §4.2 第 5 行 |
| GET | `/files/{fileId}` | 200 | §4.2 第 6 行 |

**协议原文对路径的写法是 `/projects/{id}/uploads`**，本实现用
`{projectId}`：项目端点从 WP09 起就统一用 `projectId` 作为路径参数名
（`Path(alias="projectId")`），改这一个会让同一份契约里出现两种命名。
差异已登记为待定项。

**所有容量类字段一律十进制字符串**（协议 §1："所有数据库 bigint 对外
固定为十进制字符串，包括容量、事件序号……**不要根据数值大小在字符串和
数字间切换**"）。因此 `byteSize` / `declaredSize` / `remainingQuotaBytes` /
`partSizeBytes` 全部是字符串，包括 `partSizeBytes` —— 它虽然不是数据库列，
但属于"容量"这一族；若它是数字而 `byteSize` 是字符串，
客户端会因为"看起来都能算数"而写出 `partSize + received` 这种混型表达式。

**关于 `taskId`**：协议 §4.2 要求完成上传返回它，但 `jobs` 表属 WP13
（数据结构 §11）。本包返回 `null` 并显式登记（OP-95），
`outbox_events` 里已经落了 `file.uploaded` 事件 ——
WP13 的投递器上线后即可消费，WP17 不需要回头改本包。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from tempfile import SpooledTemporaryFile
from typing import Annotated, Any, BinaryIO

from fastapi import APIRouter, Path, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from rednote.modules.files import service
from rednote.modules.files.dependencies import (
    FileAccessDep,
    FileRepoDep,
    StorageBackendDep,
    UploadAccessDep,
)
from rednote.modules.files.models import (
    CLASSIFICATION_SUPPORTED,
    DEFAULT_PART_SIZE_BYTES,
)
from rednote.modules.files.repository import FileRepository
from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.projects.dependencies import ProjectAccessDep
from rednote.platform.api_responses import UPLOAD_READ_RESPONSES, UPLOAD_RESPONSES
from rednote.platform.errors import (
    ApiError,
    DataEnvelope,
    ErrorCode,
    build_meta,
    error_response,
)

router = APIRouter(tags=["files"])

# 接收请求体时的内存阈值。超过它 `SpooledTemporaryFile` 自动落盘 ——
# 分片默认 8 MiB，所以实际上总是落盘；设为 1 MiB 是为了在
# "客户端把分片调小"（OP-96）的将来仍然不把内容留在内存里。
_SPOOL_MEMORY_THRESHOLD = 1024 * 1024


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class CreateUploadBody(BaseModel):
    """`POST /projects/{projectId}/uploads` 的请求体（协议 §4.2 入参列）。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    file_name: str = Field(alias="fileName", min_length=1)
    byte_size: int = Field(alias="byteSize", ge=0)
    mime_type: str = Field(alias="mimeType", min_length=1)
    purpose: str = Field(min_length=1)
    # 可选。**仅作提示**：权威摘要在完成上传时由服务端重算
    # （文档 02 §4："SHA 为服务端结果"）。
    sha256: str | None = None


class OrderedPartBody(BaseModel):
    """完成上传时的单片声明。

    `part_no` 的顺序即客户端认为的拼接顺序。服务端**会核对它**
    但不按它拼接（服务端按 `part_no` 升序拼，见 `service._load_parts_in_order`）。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    part_no: int = Field(alias="partNo", ge=1)
    sha256: str | None = None


class CompleteUploadBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    ordered_parts: list[OrderedPartBody] = Field(alias="orderedParts")


# ---------------------------------------------------------------------------
# 响应体
# ---------------------------------------------------------------------------


class UploadSessionCreatedBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    upload_id: uuid.UUID = Field(alias="uploadId")
    # 字符串：见模块 docstring 的容量字段约定。
    part_size_bytes: str = Field(alias="partSizeBytes")
    expires_at: datetime = Field(alias="expiresAt")
    remaining_quota_bytes: str = Field(alias="remainingQuotaBytes")


class ReceivedPartBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    part_no: int = Field(alias="partNo")
    byte_size: str = Field(alias="byteSize")
    sha256: str | None = None


class UploadSessionBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    upload_id: uuid.UUID = Field(alias="uploadId")
    project_id: uuid.UUID = Field(alias="projectId")
    purpose: str
    state: str
    declared_size: str = Field(alias="declaredSize")
    received_bytes: str = Field(alias="receivedBytes")
    reserved_bytes: str = Field(alias="reservedBytes")
    part_size_bytes: str = Field(alias="partSizeBytes")
    expires_at: datetime = Field(alias="expiresAt")
    received_parts: list[ReceivedPartBody] = Field(alias="receivedParts")
    remaining_quota_bytes: str = Field(alias="remainingQuotaBytes")
    file_id: uuid.UUID | None = Field(alias="fileId")


class UploadPartBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    part_no: int = Field(alias="partNo")
    byte_size: str = Field(alias="byteSize")
    sha256: str
    # 协议 §4.2：「重复一致返回已有结果」——这个字段让客户端能区分
    # "这一片真的写进去了"与"服务端已经有一份一模一样的"。
    already_existed: bool = Field(alias="alreadyExisted")


class CompletionBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    file_id: uuid.UUID = Field(alias="fileId")
    # WP13 之前恒为 null，见模块 docstring。
    task_id: uuid.UUID | None = Field(alias="taskId")
    file_name: str = Field(alias="fileName")
    byte_size: str = Field(alias="byteSize")
    sha256: str
    # AC-07 的对外体现：UNSUPPORTED 必须是一个**明确可见的状态**，
    # 而不是"文件在但什么都没发生"。
    classification: str
    supported: bool
    created: bool


class FileMetadataBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    file_id: uuid.UUID = Field(alias="fileId")
    project_id: uuid.UUID | None = Field(alias="projectId")
    file_name: str = Field(alias="fileName")
    byte_size: str = Field(alias="byteSize")
    sha256: str
    mime_type: str = Field(alias="mimeType")
    classification: str
    supported: bool
    scan_status: str = Field(alias="scanStatus")
    storage_state: str = Field(alias="storageState")
    uploaded_by: uuid.UUID = Field(alias="uploadedBy")
    created_at: datetime = Field(alias="createdAt")
    # **WP17 扩展**：协议 §4.2 第 6 行的说明就是"文件元数据、**扫描解析状态**、
    # 权限"。解析状态属于这个端点，因此在这里加字段而不是在协议之外
    # 造一个 `/files/{id}/parse-runs`。
    #
    # `None` 表示"还没解析过"（与"正在排队"是两件事，前者要前端显示
    # "未解析"）。`Any` 而不是 `ParseRunBody`：直接 import 解析模块的
    # DTO 会让文件模块依赖解析模块，而两者是**平级**的业务模块。
    # 形状由 `parsing.service.describe_run` 保证（同一份字典）。
    latest_parse: dict[str, Any] | None = Field(default=None, alias="latestParse")


def _quota_fields(repo: FileRepository, project_id: uuid.UUID) -> dict[str, str]:
    """账面字段（全部十进制字符串）。"""
    snapshot = service.quota_snapshot(repo, project_id)
    remaining = (
        snapshot["limitBytes"]
        - snapshot["effectiveBytes"]
        - snapshot["reservedBytes"]
    )
    return {
        "remainingQuotaBytes": str(max(0, remaining)),
    }


# ---------------------------------------------------------------------------
# 请求体接收
# ---------------------------------------------------------------------------


async def _spool_body(request: Request, *, max_bytes: int) -> BinaryIO:
    """把请求体流式收进临时缓冲，**边收边判上限**。

    三个必须这样做、不能偷懒的理由：

    1. **不能 `await request.body()`** —— 它会把整个 body 读进内存。
       单片 8 MiB、并发 10 个请求就是 80 MB 常驻，
       而这只是"正常使用"的水位。
    2. **不能等收完再判上限** —— 那样一个恶意客户端可以发 10 GB，
       服务端先把它全部落盘才发现超限。这里逐块计数，超限**立即中断**。
    3. **不能只信 `Content-Length`** —— 那个头可以缺失或撒谎，
       真实大小只能靠实际读到的字节数说话。

    `max_bytes` 由调用方给（一片的上限），而不是复用普通请求体的 2 MB ——
    分片走的是另一套限制（协议 §1："普通请求体上限默认 2 MB，
    **文件走分片协议**"）。
    """
    buffer = SpooledTemporaryFile(max_size=_SPOOL_MEMORY_THRESHOLD)
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                raise ApiError(
                    ErrorCode.UPLOAD_TOO_LARGE,
                    "分片超过允许大小。",
                    details={
                        "reason": "part_too_large",
                        "maxPartSizeBytes": str(max_bytes),
                        "receivedBytes": str(total),
                    },
                )
            buffer.write(chunk)
        buffer.seek(0)
        return buffer
    except BaseException:
        buffer.close()
        raise


# ---------------------------------------------------------------------------
# 1. 创建上传会话
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{projectId}/uploads",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[UploadSessionCreatedBody],
    responses=UPLOAD_RESPONSES,
    summary="创建上传会话并预占配额",
)
def create_upload(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: FileRepoDep,
    body: CreateUploadBody,
) -> dict[str, Any]:
    """创建上传会话。

    **配额的原子预占发生在这里**，而不是在完成上传时 ——
    否则"上传中的文件"不占额度，用户可以并发发起 N 个 300 MB 的上传，
    每个都在完成时才检查，结果全部超限（AC-06 直接失败）。
    """
    view = service.create_upload_session(
        repo,
        project_id=access.project_id,
        file_name=body.file_name,
        byte_size=body.byte_size,
        mime_type=body.mime_type,
        purpose=body.purpose,
        created_by=user.id,
        sha256=body.sha256,
    )
    return {
        "data": {
            "uploadId": view.session_id,
            "partSizeBytes": str(view.part_size_bytes),
            "expiresAt": view.expires_at,
            **_quota_fields(repo, access.project_id),
        },
        "meta": build_meta(),
    }


# ---------------------------------------------------------------------------
# 2. 查询上传会话
# ---------------------------------------------------------------------------


@router.get(
    "/uploads/{uploadId}",
    response_model=DataEnvelope[UploadSessionBody],
    responses=UPLOAD_READ_RESPONSES,
    summary="查询上传会话（断点续传）",
)
def get_upload(
    access: UploadAccessDep,
    repo: FileRepoDep,
) -> dict[str, Any]:
    view = service.get_upload_session(repo, session_id=access.upload_id)
    return {
        "data": {
            "uploadId": view.session_id,
            "projectId": view.project_id,
            "purpose": view.purpose,
            "state": view.state,
            "declaredSize": str(view.declared_size),
            "receivedBytes": str(view.received_bytes),
            "reservedBytes": str(view.reserved_bytes),
            "partSizeBytes": str(view.part_size_bytes),
            "expiresAt": view.expires_at,
            "receivedParts": [
                {
                    "partNo": part.part_no,
                    "byteSize": str(part.byte_size),
                    "sha256": part.sha256,
                }
                for part in view.received_parts
            ],
            "fileId": view.file_id,
            **_quota_fields(repo, view.project_id),
        },
        "meta": build_meta(),
    }


# ---------------------------------------------------------------------------
# 3. 上传分片
# ---------------------------------------------------------------------------


@router.put(
    "/uploads/{uploadId}/parts/{partNo}",
    response_model=DataEnvelope[UploadPartBody],
    responses=UPLOAD_RESPONSES,
    summary="上传一个分片（重复一致返回已有结果）",
)
async def upload_part(
    request: Request,
    access: UploadAccessDep,
    repo: FileRepoDep,
    storage: StorageBackendDep,
    part_no: Annotated[int, Path(alias="partNo", ge=1)],
) -> dict[str, Any]:
    """接收一片 `application/octet-stream`。

    `Content-Type` **不做强制校验**：协议 §4.2 写的是
    `application/octet-stream`，但强制它只会拒绝掉那些自作主张填
    `application/octet-stream; charset=binary` 或干脆不填的客户端，
    而分片的内容完整性由**逐片 sha256** 保证，与声明的 MIME 无关。
    校验一个不影响正确性的字段，收益是零、成本是兼容性。
    """
    access.require_write()
    # 单片上限取自 `DEFAULT_PART_SIZE_BYTES`。
    #
    # **已知隐患**：`store_part` 的 `part_size_bytes` 是它自己的参数
    # （默认同值），所以这里是**两处独立来源** —— 若将来支持客户端
    # 上调分片大小（待定项 OP-96），两处必须一起改，否则会出现
    # "接口收了 16 MiB、服务层按 8 MiB 校验"的错位。
    # 当前都取默认值，行为一致；触发变更时以服务层的值为准。
    buffer = await _spool_body(request, max_bytes=DEFAULT_PART_SIZE_BYTES)
    try:
        result = service.store_part(
            repo,
            storage,
            session_id=access.upload_id,
            part_no=part_no,
            stream=buffer,
        )
    finally:
        buffer.close()

    return {
        "data": {
            "partNo": result.part_no,
            "byteSize": str(result.byte_size),
            "sha256": result.sha256,
            "alreadyExisted": result.already_existed,
        },
        "meta": build_meta(),
    }


# ---------------------------------------------------------------------------
# 4. 完成上传
# ---------------------------------------------------------------------------


@router.post(
    "/uploads/{uploadId}/completions",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DataEnvelope[CompletionBody],
    responses=UPLOAD_RESPONSES,
    summary="完成上传并固化文件",
)
def complete_upload(
    access: UploadAccessDep,
    repo: FileRepoDep,
    storage: StorageBackendDep,
    body: CompleteUploadBody,
) -> dict[str, Any] | Response:
    """核实大小与摘要 → 固化 → 预占转有效 → 写 Outbox。

    **202 而不是 200**：协议 §4.2 定的是 202 —— 扫描解析是**异步**的，
    返回的是"文件已收下、任务已登记"，不是"文件已解析完"。
    用 200 会让客户端以为可以立刻读解析结果。

    **`Idempotency-Key` 头**：协议要求带它，本实现**接受但不依赖**它 ——
    `uploadId` 本身就是更强的天然幂等键（一次上传意图 = 一个会话），
    状态机已保证重复请求返回同一结果。不引入 `idempotency_records`
    （数据结构 §11 / WP13）的理由与差异见待定项 OP-94。
    """
    access.require_write()
    outcome = service.complete_upload(
        repo,
        storage,
        session_id=access.upload_id,
        ordered_parts=body.ordered_parts,
    )

    if outcome.failure is not None:
        # **刻意不 raise**：不可恢复的失败已经在同一事务里把会话置 FAILED、
        # 把预占还了回去 —— 而那些写入要**提交**才生效。
        # raise 会让外层事务回滚，把这两步一起撤销：用户看到"文件损坏"，
        # 额度却被永久扣着（要等 24 小时 TTL）。因此这里把错误直接
        # 构造成响应返回（返回 `Response` 实例会跳过 response_model 校验）。
        return error_response(
            outcome.failure.code,
            outcome.failure.message,
            outcome.failure.details,
        )

    result = outcome.result
    assert result is not None  # failure 为 None 时必有 result（见该类型定义）
    return {
        "data": {
            "fileId": result.file_id,
            "taskId": None,
            "fileName": result.original_name,
            "byteSize": str(result.byte_size),
            "sha256": result.sha256,
            "classification": result.classification,
            "supported": result.supported,
            "created": result.created,
        },
        "meta": build_meta(),
    }


# ---------------------------------------------------------------------------
# 5. 取消上传
# ---------------------------------------------------------------------------


@router.delete(
    "/uploads/{uploadId}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=UPLOAD_RESPONSES,
    summary="取消上传并幂等释放预占",
)
def cancel_upload(
    access: UploadAccessDep,
    repo: FileRepoDep,
    storage: StorageBackendDep,
) -> Response:
    """取消并释放预占。

    **重复取消也返回 204**，不是 404 也不是 409：协议 §4.2 要求
    "幂等释放预占"，客户端的目的（这个上传不再占额度）已经达成。
    用 404 会让"网络重试"看起来像失败，用 409 更糟 ——
    它暗示调用方做错了什么，而调用方什么都没做错。
    """
    access.require_write()
    service.cancel_upload(repo, storage, session_id=access.upload_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# 6. 文件元数据
# ---------------------------------------------------------------------------


@router.get(
    "/files/{fileId}",
    response_model=DataEnvelope[FileMetadataBody],
    responses=UPLOAD_READ_RESPONSES,
    summary="文件元数据与扫描解析状态",
)
def get_file(
    access: FileAccessDep,
    repo: FileRepoDep,
) -> dict[str, Any]:
    file_row, blob = service.get_file(repo, file_id=access.file_id)
    return {
        "data": {
            "fileId": file_row.id,
            "projectId": file_row.project_id,
            "fileName": file_row.original_name,
            "byteSize": str(blob.byte_size),
            "sha256": blob.sha256,
            "mimeType": blob.mime_type,
            "classification": file_row.classification,
            "supported": file_row.classification == CLASSIFICATION_SUPPORTED,
            "scanStatus": blob.scan_status,
            "storageState": blob.storage_state,
            "uploadedBy": file_row.uploaded_by,
            "createdAt": file_row.created_at,
            "latestParse": _latest_parse(repo.session, file_row.id),
        },
        "meta": build_meta(),
    }


def _latest_parse(
    session: Session, file_id: uuid.UUID
) -> dict[str, Any] | None:
    """最近一次解析的摘要（WP17 扩展）。

    **函数内导入**而不是模块顶层：文件模块与解析模块是**平级**的业务模块，
    顶层互相 import 会形成循环（解析模块要 `FileObject` / `FileRepository`）。
    用延迟导入把这条依赖限定在"真正用到它的那一次调用"里。

    **复用调用方的 session**（不新开一个）：`_latest_parse` 只是多读一张表，
    新开 session 会让这次读取落在另一个事务里 —— 于是同一个响应里
    "文件元数据"与"解析状态"可能来自两个不同时点。
    请求级 session 由 `DBSession` 依赖提供，这里是同一个。

    解析记录不存在时返回 `None` —— "还没解析过"与"正在排队"是两件事。
    """
    from rednote.modules.parsing.repository import ParseRepository
    from rednote.modules.parsing.service import latest_summary

    return latest_summary(ParseRepository(session), file_id)


__all__ = ["router"]
