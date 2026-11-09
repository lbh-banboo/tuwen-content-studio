"""上传会话、配额预占与文件固化（WP12）。

本模块是 WP12 的**唯一对外入口** —— API 层只调这里的函数。

**四条贯穿全模块的规则**：

1. **配额只经条件 UPDATE 变动**，且"释放只允许一次"由状态迁移的
   `rowcount` 决定（见 `repository` 的说明）。本模块不做任何
   "读余量 → 判断 → 扣减"的序列。

2. **顺序：先判状态，再动资源**。取消/完成都先做条件状态迁移，
   迁移成功（`rowcount == 1`）才去释放预占或固化文件。
   反过来的话，重复请求会把额度多还一次。

3. **客户端的一切声明都只是提示**：`byteSize` 用于预占、`sha256`
   用于提前比对 —— 权威值一律由服务端从真实字节流算出
   （文档 02 §4："SHA 为服务端结果"）。

4. **任何进入存储的 key 都由服务端拼**，不含用户输入（见 `storage`）。

**关于幂等键**：协议 §4.2 要求 `completions` 带 `Idempotency-Key` 头。
本包**接受但不用它做幂等** —— 因为 `uploadId` 本身就是一个更强的
天然幂等键（一次上传意图 = 一个会话），状态机已经保证了重复请求
返回同一结果。不引入 `idempotency_records`（§11 / WP13）的理由
与差异已登记为待定项 OP-94。
"""

from __future__ import annotations

import hashlib
import io
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Final

from rednote.modules.files.models import (
    CLASSIFICATION_SUPPORTED,
    CLASSIFICATION_UNSUPPORTED,
    DEFAULT_PART_SIZE_BYTES,
    FILE_PURPOSES,
    PURPOSE_GENERATED,
    QUOTA_LIMIT_BYTES,
    SHA256_HEX_LENGTH,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_MIME_TYPES,
    UPLOAD_SESSION_TTL_SECONDS,
    UPLOAD_STATE_CANCELLED,
    UPLOAD_STATE_COMPLETED,
    UPLOAD_STATE_COMPLETING,
    UPLOAD_STATE_EXPIRED,
    UPLOAD_STATE_FAILED,
    UPLOAD_STATE_UPLOADING,
    UPLOAD_STATES_HOLDING_RESERVATION,
    VISIBILITY_PROJECT,
    Blob,
    FileObject,
    UploadPart,
    UploadSession,
)
from rednote.modules.files.repository import FileRepository
from rednote.modules.files.storage import (
    StorageBackend,
    object_key_for,
    part_key_for,
    staging_key_for,
    upload_prefix,
)
from rednote.modules.outbox.models import AGGREGATE_FILE_OBJECT, EVENT_FILE_UPLOADED
from rednote.platform.errors import ApiError, ErrorCode

# 原名长度上限，与 `file_objects.original_name` 的列宽一致。
# 在一处定义、两处使用（列宽 + 校验），避免"列能存 512 但校验只让 255"
# 这种永远碰不到的边界。
MAX_ORIGINAL_NAME_LENGTH: Final[int] = 512

# MIME 长度上限，与列宽一致。
MAX_MIME_TYPE_LENGTH: Final[int] = 255

# 一次可以携带的分片数上限。用于 `orderedParts` 的长度校验，
# 防御"传一个百万长度的数组"这类内存放大攻击。
MAX_PARTS_PER_COMPLETION: Final[int] = 100_000


# ---------------------------------------------------------------------------
# 返回值
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadPartView:
    """已收到的一个分片。

    单独一个不可变类型而不是直接回带 ORM 的 `UploadPart`：
    快照会被端点持有到响应构造完成，期间事务可能已结束，
    而 ORM 实例在 session 关闭后访问属性会抛 `DetachedInstanceError`。
    """

    part_no: int
    byte_size: int
    sha256: str | None


@dataclass(frozen=True)
class UploadSessionView:
    """一次上传会话的快照。

    **为什么带 `part_size_bytes`**：它是服务端的决定（协议 §4.2 返回
    `partSizeBytes`），客户端据此切片。它不存在会话行里 —— 因为它是
    配置而不是状态，存下来会与配置漂移。这里在读取时现算。
    """

    session_id: uuid.UUID
    project_id: uuid.UUID
    purpose: str
    declared_size: int
    reserved_bytes: int
    received_bytes: int
    state: str
    expires_at: datetime
    part_size_bytes: int
    received_parts: tuple[UploadPartView, ...]
    file_id: uuid.UUID | None


@dataclass(frozen=True)
class StoredPartView:
    """一次分片上传的结果。

    `already_existed=True` 表示这一片之前就传过、内容一致，
    本次**没有改动任何数据** —— 这正是协议 §4.2
    "重复一致返回已有结果"的含义。
    """

    part_no: int
    byte_size: int
    sha256: str
    already_existed: bool


@dataclass(frozen=True)
class CompletedUpload:
    """一次完成上传的结果。"""

    file_id: uuid.UUID
    blob_id: uuid.UUID
    original_name: str
    byte_size: int
    sha256: str
    classification: str
    # 本次调用是否真的做了固化（False = 幂等命中，之前就完成了）。
    created: bool

    @property
    def supported(self) -> bool:
        """是否在"承诺可解析"的类型清单内（AC-07）。"""
        return self.classification == CLASSIFICATION_SUPPORTED


@dataclass(frozen=True)
class CompletionOutcome:
    """完成上传的结果：成功带 `result`，不可恢复失败带 `failure`。

    **为什么这里用返回值而不是异常（全模块唯一一处）**：

    不可恢复的失败必须在**同一事务**里做完两件事 ——
    ① 会话置 `FAILED`；② 预占归还账面。
    而这两件事要生效，前提是**事务正常提交**。

    若用异常传播，外层 `session_scope` 会在退栈时回滚整个事务，
    ①②**一起被撤销** —— 于是预占一直挂在账上，要等 24 小时 TTL 才还。
    现场表现是最难查的那种：用户传了一个损坏的文件，界面明确提示
    "文件摘要与声明不一致"，**但额度不退**；重试几次就把项目堵死，
    而账面上看不出任何异常（只看到 reserved 莫名其妙高了）。

    **哪些失败走这条路**（不可恢复）：
      · 摘要不符（文件已损坏，重传也必须重新开始）；
      · 实际字节数超出剩余余量（这个文件就是放不下）；
      · 拼装时分片在存储上缺失（临时对象已不在，重试也没用）。
    它们的共同点是：**这个会话已经没救了**，只能终结并还额度。

    **哪些失败仍然抛 `ApiError`**（可重试）：
      · 分片不连续 / 分片数不符 / 一片都没收到；
      · 会话状态不对（已取消、已完成）。
    它们共同点是：**会话还能救** —— 用户补传缺失的分片后可以重试，
    因此绝不能顺手把会话置 FAILED、更不能释放预占。
    调用方（API 层）拿到异常后直接抛给框架，事务回滚是正确行为
    （本次没写任何东西）。
    """

    result: CompletedUpload | None = None
    failure: ApiError | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


@dataclass(frozen=True)
class ExpireReport:
    """一次 TTL 回收的结果。"""

    expired_sessions: int
    released_bytes: int
    # 存储上被清掉的临时对象个数（分片行数由 delete_parts 处理，不在此统计）。
    deleted_objects: int


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """当前 UTC 时间。

    单独一个函数是为了让测试能在一个地方替换它，而不是在业务代码里
    到处 monkeypatch `datetime`（那种写法会让"时间从哪来"变得不可追踪）。
    """
    return datetime.now(timezone.utc)


def _validation_error(field: str, message: str, *, reason: str, **extra: Any) -> ApiError:
    details: dict[str, Any] = {
        "fieldErrors": [{"field": field, "type": reason, "message": message}],
        "reason": reason,
    }
    details.update(extra)
    return ApiError(ErrorCode.VALIDATION_FAILED, message, details=details)


def normalize_original_name(raw: str) -> str:
    """校验并归一化原始文件名。

    **只做三件事**：去首尾空白、长度上限、拒绝控制字符。

    **不做**"替换 `..` / 去掉 `/`"这类看起来更安全的操作 ——
    文件名在本系统里**从来不参与路径拼接**（`object_key` 由服务端生成），
    所以那些替换是纯粹的装饰：它们防的不是本系统的攻击面，
    却会让用户看到"我的文件名被改了"（`报表/2026.xlsx` 变成 `报表_2026.xlsx`）。

    控制字符必须拒（不能靠替换）：它们会进入 JSON 响应、日志、
    以及将来的下载响应头，是头注入与日志注入的载体。这类输入
    **没有"安全化"的余地** —— 直接拒绝比悄悄改写更容易被发现。
    """
    name = raw.strip()
    if not name:
        raise _validation_error(
            "fileName", "文件名不能为空。", reason="file_name_empty"
        )
    if len(name) > MAX_ORIGINAL_NAME_LENGTH:
        raise _validation_error(
            "fileName",
            f"文件名过长（上限 {MAX_ORIGINAL_NAME_LENGTH} 字符）。",
            reason="file_name_too_long",
            maxLength=MAX_ORIGINAL_NAME_LENGTH,
        )
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
        raise _validation_error(
            "fileName",
            "文件名包含控制字符。",
            reason="file_name_control_chars",
        )
    return name


def normalize_mime_type(raw: str) -> str:
    mime = raw.strip().lower()
    if not mime:
        raise _validation_error(
            "mimeType", "mimeType 不能为空。", reason="mime_type_empty"
        )
    if len(mime) > MAX_MIME_TYPE_LENGTH:
        raise _validation_error(
            "mimeType",
            f"mimeType 过长（上限 {MAX_MIME_TYPE_LENGTH} 字符）。",
            reason="mime_type_too_long",
            maxLength=MAX_MIME_TYPE_LENGTH,
        )
    return mime


def normalize_purpose(raw: str) -> str:
    purpose = raw.strip().upper()
    if purpose not in FILE_PURPOSES:
        raise _validation_error(
            "purpose",
            "未知的文件用途。",
            reason="purpose_unknown",
            allowedPurposes=sorted(FILE_PURPOSES),
        )
    return purpose


def require_non_negative_size(raw: int, *, field: str = "byteSize") -> int:
    if raw < 0:
        raise _validation_error(field, "大小不能为负。", reason="size_negative")
    return raw


def classify_file(*, original_name: str, mime_type: str) -> str:
    """判定"是否承诺可解析"（AC-07 的判据）。

    **扩展名与 MIME 取并集**，两者任一命中即视为支持：
      · 只看 MIME 会误判 —— 浏览器对 `.md` 常报 `text/plain`、
        对 `.csv` 在 macOS 上可能报 `application/vnd.ms-excel`，
        这些文件明明在承诺清单里却会被标成 UNSUPPORTED，
        用户看到"未解析"而实际能解析，是**假阴性**；
      · 只看扩展名会被改名绕过（把 `.exe` 改成 `.pdf`）——
        但那是"假阳性"，代价是解析时才发现真类型不对，
        而**真正的类型判定（magic bytes）属 WP17**，
        本包只回答"是否在承诺清单内"这个问题。

    所以这里的选择是：**宁可把可能不支持的标成支持**（由解析阶段
    给出最终结论），也不要把支持的文件标成不支持（用户会以为要转格式）。
    """
    extension = ""
    if "." in original_name:
        extension = original_name.rsplit(".", 1)[-1].strip().lower()

    if extension in SUPPORTED_EXTENSIONS:
        return CLASSIFICATION_SUPPORTED
    if mime_type in SUPPORTED_MIME_TYPES:
        return CLASSIFICATION_SUPPORTED
    return CLASSIFICATION_UNSUPPORTED


def max_part_no_for(*, declared_size: int, part_size_bytes: int) -> int:
    """按声明大小推出合法分片号上限。

    空文件也算 1 片（分片号从 1 开始，不允许 0）——
    否则 0 字节文件永远无法完成上传。
    """
    if part_size_bytes <= 0:
        raise RuntimeError("part_size_bytes 必须为正，检查配置")
    return max(1, math.ceil(declared_size / part_size_bytes))


def _require_part_size(
    *, part_no: int, byte_size: int, max_part_no: int, part_size_bytes: int
) -> None:
    """校验单片大小。

    **非末片必须恰好等于 `part_size_bytes`**。少了说明客户端切片有洞
    （拼出来会缺内容），多了说明它想用一片绕过大小限制
    （一片 300 MB 等于分片机制形同虚设）。
    末片允许更小，但不得超过一片上限。
    """
    if byte_size > part_size_bytes:
        raise _validation_error(
            "part",
            f"分片超过上限 {part_size_bytes} 字节。",
            reason="part_too_large",
            partNo=part_no,
            maxPartSizeBytes=part_size_bytes,
        )
    if part_no < max_part_no and byte_size != part_size_bytes:
        raise _validation_error(
            "part",
            f"非末片必须恰好 {part_size_bytes} 字节。",
            reason="part_size_mismatch",
            partNo=part_no,
            expectedBytes=part_size_bytes,
            actualBytes=byte_size,
        )


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


def _to_session_view(
    session_row: UploadSession,
    *,
    parts: Sequence[UploadPart],
    part_size_bytes: int,
) -> UploadSessionView:
    """把会话行转成不可变快照。

    **`received_bytes` 用实时求和，而不是读 `upload_sessions.received_bytes` 列**：
    那一列只在完成上传时写入固化快照，上传期间恒为 0。
    实时求和永远与分片表一致，不会出现"列说 8 MB、实际只收到 3 片"
    这类漂移 —— 而后者会让前端以为还差 5 MB 就传完了。
    """
    return UploadSessionView(
        session_id=session_row.id,
        project_id=session_row.project_id,
        purpose=session_row.purpose,
        declared_size=session_row.declared_size,
        reserved_bytes=session_row.reserved_bytes,
        received_bytes=(
            session_row.received_bytes
            if session_row.received_bytes
            else sum(part.byte_size for part in parts)
        ),
        state=session_row.state,
        expires_at=session_row.expires_at,
        part_size_bytes=part_size_bytes,
        received_parts=tuple(
            UploadPartView(
                part_no=part.part_no,
                byte_size=part.byte_size,
                sha256=part.sha256,
            )
            for part in parts
        ),
        file_id=session_row.file_id,
    )


def require_session(
    repo: FileRepository, session_id: uuid.UUID, *, for_update: bool = False
) -> UploadSession:
    """取会话，取不到报 404。

    **不区分"不存在"与"无权访问"**：调用方（API 层）会先做项目权限校验，
    到这里说明"该用户对项目有权限"，那么会话不存在就是真的不存在。
    若把两者分开报，就等于给出"某个 uploadId 是否存在"的枚举能力。
    """
    session_row = (
        repo.get_session(session_id)
        if for_update
        else repo.get_session_readonly(session_id)
    )
    if session_row is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "上传会话不存在。",
            details={"resourceId": str(session_id)},
        )
    return session_row


def get_upload_session(
    repo: FileRepository,
    *,
    session_id: uuid.UUID,
    part_size_bytes: int = DEFAULT_PART_SIZE_BYTES,
) -> UploadSessionView:
    """查询会话（断点续传的"续"）。

    一次查询取全部所需数据（会话行 + 分片明细），由 `_to_session_view`
    组装成不可变快照 —— 调用方不必也不应再查一遍分片表。
    """
    session_row = require_session(repo, session_id)
    parts = repo.list_parts(session_id)
    return _to_session_view(
        session_row, parts=parts, part_size_bytes=part_size_bytes
    )


def get_file(
    repo: FileRepository, *, file_id: uuid.UUID
) -> tuple[FileObject, Blob]:
    """取文件元数据与它的物理对象。"""
    file_row = repo.get_file_object(file_id)
    if file_row is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "文件不存在。",
            details={"resourceId": str(file_id)},
        )
    blob = repo.get_blob(file_row.blob_id)
    if blob is None:  # pragma: no cover - 外键保证不会发生
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "文件内容不存在。",
            details={"resourceId": str(file_id)},
        )
    return file_row, blob


# ---------------------------------------------------------------------------
# 创建会话 + 预占
# ---------------------------------------------------------------------------


def create_upload_session(
    repo: FileRepository,
    *,
    project_id: uuid.UUID,
    file_name: str,
    byte_size: int,
    mime_type: str,
    purpose: str,
    created_by: uuid.UUID,
    sha256: str | None = None,
    part_size_bytes: int = DEFAULT_PART_SIZE_BYTES,
) -> UploadSessionView:
    """创建上传会话并**原子预占**配额。

    **顺序不可调换**：先 `ensure_quota_account`（保证有账户可扣）、
    再 `reserve_quota`（条件 UPDATE 原子预占）、最后插会话。
    反过来的话，会话已经存在但预占失败 —— 留下一个"看起来在上传、
    实际上没有额度"的孤儿会话，而用户会一直传分片，直到完成时才发现问题。

    **预占失败报 413 `UPLOAD_TOO_LARGE`**（协议 §1.2 把它映射到 413）：
    对用户而言，这就是"你要传的东西超出了允许大小"。
    用 422 会让前端以为是字段格式问题，提示不到点子上。

    返回里带 `remainingQuotaBytes` 所需的一切（调用方用
    `limit - effective - reserved` 算）—— 见 `quota_snapshot()`。
    """
    name = normalize_original_name(file_name)
    mime = normalize_mime_type(mime_type)
    normalized_purpose = normalize_purpose(purpose)
    declared = require_non_negative_size(byte_size)

    if sha256 is not None:
        normalized_hash = sha256.strip().lower()
        if len(normalized_hash) != SHA256_HEX_LENGTH or not all(
            char in "0123456789abcdef" for char in normalized_hash
        ):
            raise _validation_error(
                "sha256",
                "sha256 必须是 64 位十六进制字符串。",
                reason="sha256_malformed",
            )
    else:
        normalized_hash = None

    repo.ensure_quota_account(project_id)

    reserved = declared
    if not repo.reserve_quota(project_id=project_id, delta=reserved):
        account = repo.get_quota_account(project_id)
        limit = account.limit_bytes if account else 0
        used = (
            (account.effective_bytes + account.reserved_bytes) if account else 0
        )
        raise ApiError(
            ErrorCode.UPLOAD_TOO_LARGE,
            "上传会超出该项目的研究容量上限。",
            details={
                "reason": "quota_exceeded",
                "requestedBytes": str(declared),
                "limitBytes": str(limit),
                "usedBytes": str(used),
                "remainingQuotaBytes": str(max(0, limit - used)),
            },
        )

    # 类别在创建时就定死并落库（AC-07 的判据）：
    # 完成上传时只读它，不再重新判定 —— 否则"创建时说支持、完成时说
    # 不支持"这种自相矛盾的结果会在客户端看起来像是系统在随机改结论。
    classification = classify_file(original_name=name, mime_type=mime)

    session_id = uuid.uuid4()
    session_row = repo.insert_session(
        session_id=session_id,
        project_id=project_id,
        purpose=normalized_purpose,
        original_name=name,
        classification=classification,
        declared_size=declared,
        reserved_bytes=reserved,
        state=UPLOAD_STATE_UPLOADING,
        expires_at=_utcnow() + timedelta(seconds=UPLOAD_SESSION_TTL_SECONDS),
        temporary_key=upload_prefix(session_id),
        created_by=created_by,
        expected_sha256=normalized_hash,
    )
    repo.flush()
    return _to_session_view(session_row, parts=(), part_size_bytes=part_size_bytes)


def quota_snapshot(repo: FileRepository, project_id: uuid.UUID) -> dict[str, int]:
    """账户账面快照（供端点组装 `remainingQuotaBytes` 等字段）。

    返回的三个数都是**十进制字符串**（协议 §1："所有数据库 bigint
    对外固定为十进制字符串"）——在端点层转，本函数给 int。
    """
    account = repo.get_quota_account(project_id)
    if account is None:
        return {
            "limitBytes": QUOTA_LIMIT_BYTES,
            "effectiveBytes": 0,
            "reservedBytes": 0,
        }
    return {
        "limitBytes": account.limit_bytes,
        "effectiveBytes": account.effective_bytes,
        "reservedBytes": account.reserved_bytes,
    }


# ---------------------------------------------------------------------------
# 分片上传
# ---------------------------------------------------------------------------


def store_part(
    repo: FileRepository,
    storage: StorageBackend,
    *,
    session_id: uuid.UUID,
    part_no: int,
    stream: BinaryIO,
    part_size_bytes: int = DEFAULT_PART_SIZE_BYTES,
) -> StoredPartView:
    """接收一片。

    **为什么先写 staging 再落位**（而不是直接覆盖正式 key）：
    重复分片的内容可能**不一致**（客户端记错了分片、或本地文件被改过），
    文档 02 §4 要求这种情况拒绝。若直接覆盖正式 key，等发现不一致时
    旧内容已经被冲掉了 —— "拒绝"就变成了一句空话：用户拿到 409，
    但服务端的数据已经坏了。

    因此：写 staging → 算摘要 → **与库中已记录比对** → 通过才 replace。
    不通过就把 staging 删掉，正式 key 分毫未动。
    """
    session_row = require_session(repo, session_id, for_update=True)

    # 状态判定先于一切写入。
    if session_row.state != UPLOAD_STATE_UPLOADING:
        raise ApiError(
            ErrorCode.INVALID_TRANSITION,
            f"当前会话状态（{session_row.state}）不接受分片。",
            details={
                "reason": "session_not_uploading",
                "state": session_row.state,
                "uploadId": str(session_id),
            },
        )

    if _utcnow() >= session_row.expires_at:
        # 不在这里改状态：过期回收是 TTL 扫描的职责，若这里顺手改，
        # 会出现"回收器与请求线程同时释放同一份预占"的竞争。
        # 这里只拒绝写入，账面留给回收器统一处理。
        raise ApiError(
            ErrorCode.INVALID_TRANSITION,
            "上传会话已过期。",
            details={
                "reason": "session_expired",
                "expiresAt": session_row.expires_at.isoformat(),
                "uploadId": str(session_id),
            },
        )

    declared_size = session_row.declared_size
    max_part_no = max_part_no_for(
        declared_size=declared_size, part_size_bytes=part_size_bytes
    )
    if part_no < 1 or part_no > max_part_no:
        raise _validation_error(
            "partNo",
            f"分片号超出范围（允许 1 ~ {max_part_no}）。",
            reason="part_no_out_of_range",
            partNo=part_no,
            maxPartNo=max_part_no,
        )

    formal_key = part_key_for(session_id, part_no)
    staging_key = f"{formal_key}.staging"

    stored = storage.write_part(key=staging_key, stream=stream)

    try:
        _require_part_size(
            part_no=part_no,
            byte_size=stored.byte_size,
            max_part_no=max_part_no,
            part_size_bytes=part_size_bytes,
        )

        existing = repo.get_part(session_id=session_id, part_no=part_no)
        if existing is not None:
            if existing.sha256 != stored.sha256:
                raise ApiError(
                    ErrorCode.INVALID_TRANSITION,
                    "该分片已存在且内容不同，拒绝覆盖。",
                    details={
                        "reason": "part_content_mismatch",
                        "partNo": part_no,
                        "uploadId": str(session_id),
                        # 只回带摘要，不回带内容 —— 回带内容等于把
                        # 服务端已存的数据暴露给可能不同的调用方。
                        "existingSha256": existing.sha256,
                        "providedSha256": stored.sha256,
                    },
                )
            # 内容一致 → 协议 §4.2「重复一致返回已有结果」。
            # 本次不触碰任何数据（staging 在 finally 里删）。
            return StoredPartView(
                part_no=part_no,
                byte_size=existing.byte_size,
                sha256=existing.sha256 or stored.sha256,
                already_existed=True,
            )

        # 落位：staging → 正式 key。
        # 用 assemble 做单元素"搬移"而不是再加一个 move —— 分片只有 8 MiB，
        # 多读一遍的代价远小于让 Protocol 多出一个方法（两处实现都要维护）。
        final = storage.assemble(part_keys=[staging_key], target_key=formal_key)
        repo.insert_part(
            session_id=session_id,
            part_no=part_no,
            byte_size=final.byte_size,
            sha256=final.sha256,
            storage_etag=None,
        )
        repo.flush()
        return StoredPartView(
            part_no=part_no,
            byte_size=final.byte_size,
            sha256=final.sha256,
            already_existed=False,
        )
    finally:
        # 无论走哪条路径，staging 都不该留下 —— 它不在任何清理路径的
        # 覆盖范围内（TTL 扫的是 parts/<id>/ 前缀，staging 混在里面
        # 虽然会被删，但"上传中途失败"的场景不会走到 TTL）。
        storage.delete(key=staging_key)


# ---------------------------------------------------------------------------
# 完成上传
# ---------------------------------------------------------------------------


def _load_parts_in_order(
    repo: FileRepository,
    *,
    session_id: uuid.UUID,
    ordered_parts: Sequence[Any],
) -> list[UploadPart]:
    """校验客户端给的 `orderedParts` 与服务端已收分片一致。

    **拼接顺序由服务端按 `part_no` 升序决定，不由客户端给的顺序决定。**
    客户端传 `orderedParts` 的语义是"我确认按这个顺序拼"，
    服务端用它做一次**一致性核对**，而不是拿它当指令。
    若按客户端给的顺序拼，一个顺序传错（或恶意构造）的请求
    就能拼出一个内容合法但语义错乱的文件，而摘要校验只能发现
    "和声明的不一样"，发现不了"顺序被人换了"。
    """
    stored = repo.list_parts(session_id)
    if not stored:
        raise _validation_error(
            "orderedParts", "没有收到任何分片。", reason="no_parts_received"
        )

    if len(ordered_parts) > MAX_PARTS_PER_COMPLETION:
        raise _validation_error(
            "orderedParts",
            f"分片数超过上限 {MAX_PARTS_PER_COMPLETION}。",
            reason="too_many_parts",
            maxParts=MAX_PARTS_PER_COMPLETION,
        )

    # 分片号必须连续从 1 开始：否则拼出来的文件缺内容，
    # 而这种缺失在拼装阶段完全看不出来（只是短了一截）。
    expected_numbers = list(range(1, len(stored) + 1))
    actual_numbers = [part.part_no for part in stored]
    if actual_numbers != expected_numbers:
        raise _validation_error(
            "orderedParts",
            "分片不连续，无法拼装。",
            reason="parts_not_contiguous",
            receivedPartNumbers=actual_numbers,
        )

    if len(ordered_parts) != len(stored):
        raise _validation_error(
            "orderedParts",
            f"分片数量不一致（服务端 {len(stored)} 片）。",
            reason="part_count_mismatch",
            serverPartCount=len(stored),
            providedPartCount=len(ordered_parts),
        )

    for index, (declared, part) in enumerate(
        zip(ordered_parts, stored, strict=True), start=1
    ):
        declared_no = getattr(declared, "part_no", None)
        if declared_no != index:
            raise _validation_error(
                "orderedParts",
                f"第 {index} 项的分片号应为 {index}。",
                reason="ordered_parts_mismatch",
                position=index,
                providedPartNo=declared_no,
            )
        declared_hash = getattr(declared, "sha256", None)
        if declared_hash is not None and declared_hash != part.sha256:
            raise _validation_error(
                "orderedParts",
                f"第 {index} 片摘要与服务端记录不一致。",
                reason="part_hash_mismatch",
                position=index,
                providedSha256=declared_hash,
            )

    # 末片大小随实际内容而定，总量与声明量的关系由完成流程的
    # "按实际修正"处理（OP-93）。
    return stored


def complete_upload(
    repo: FileRepository,
    storage: StorageBackend,
    *,
    session_id: uuid.UUID,
    ordered_parts: Sequence[Any],
) -> CompletionOutcome:
    """完成上传：核实 → 固化 → 预占转有效 → 写 Outbox。

    **幂等**：重复调用返回第一次的结果（`created=False`），
    不重新固化、不重复计入配额。靠状态机实现 ——
    会话进入 `COMPLETED` 后，本函数在入口处就返回已有结果。

    **实际大小与声明不一致时（OP-93 选定的口径）**：按实际修正。
    `finalize_reservation` 一次完成"归还预占 + 记入有效"，
    差额实时反映到余量。

    **为什么返回 `CompletionOutcome` 而不是直接返回结果**：
    不可恢复的失败需要带着"已置 FAILED + 已释放预占"**一起提交**，
    所以不能走异常传播（外层事务会把这两步一起回滚掉）。
    哪些算不可恢复、哪些仍抛 `ApiError`，见 `CompletionOutcome` 的说明。
    """
    session_row = require_session(repo, session_id, for_update=True)

    if session_row.state == UPLOAD_STATE_COMPLETED:
        return CompletionOutcome(result=_completed_from_existing(repo, session_row))

    if session_row.state not in UPLOAD_STATES_HOLDING_RESERVATION:
        raise ApiError(
            ErrorCode.INVALID_TRANSITION,
            f"当前会话状态（{session_row.state}）不允许完成。",
            details={
                "reason": "session_not_completable",
                "state": session_row.state,
                "uploadId": str(session_id),
            },
        )

    if _utcnow() >= session_row.expires_at:
        raise ApiError(
            ErrorCode.INVALID_TRANSITION,
            "上传会话已过期。",
            details={
                "reason": "session_expired",
                "expiresAt": session_row.expires_at.isoformat(),
                "uploadId": str(session_id),
            },
        )

    # 标量先取：后面会走 UPDATE 语句，执行后 session 内的 ORM 实例可能
    # 被 expire，那时再读属性会触发惰性加载（WP10 踩过 PendingRollbackError）。
    project_id = session_row.project_id
    purpose = session_row.purpose
    reserved_bytes = session_row.reserved_bytes
    expected_sha256 = session_row.expected_sha256
    original_name = session_row.original_name
    classification = session_row.classification
    declared_size = session_row.declared_size

    parts = _load_parts_in_order(
        repo, session_id=session_id, ordered_parts=ordered_parts
    )

    # 进入 COMPLETING：并发到达的第二个 completions 会在这里失败，
    # 从而不会重复固化。行锁 + 条件迁移两道保险。
    if not repo.transition_session(
        session_id=session_id,
        from_states=[UPLOAD_STATE_UPLOADING],
        to_state=UPLOAD_STATE_COMPLETING,
    ):
        raise ApiError(
            ErrorCode.INVALID_TRANSITION,
            "该上传正在完成中，请稍后查询结果。",
            details={"reason": "completion_in_progress", "uploadId": str(session_id)},
        )

    part_keys = [part_key_for(session_id, part.part_no) for part in parts]
    assembled_key = f"{upload_prefix(session_id)}assembled"
    assembled = storage.assemble(part_keys=part_keys, target_key=assembled_key)

    try:
        if expected_sha256 is not None and assembled.sha256 != expected_sha256:
            # 不可恢复：文件已损坏，重传也得从头来 —— 会话没救了。
            # 走返回值而不是抛异常，让"置 FAILED + 释放预占"能被提交。
            _fail_completion(
                repo,
                storage,
                session_id=session_id,
                project_id=project_id,
                reserved_bytes=reserved_bytes,
            )
            return CompletionOutcome(
                failure=_checksum_mismatch_error(
                    expected_sha256=expected_sha256,
                    actual_sha256=assembled.sha256,
                    session_id=session_id,
                )
            )

        final_key = object_key_for(assembled.sha256)
        if storage.exists(key=final_key):
            # 同内容已在库里（物理去重命中）：丢弃本次拼装结果。
            # **但配额照记**（文档 02 §4："不因跨项目共用 Blob 免掉该项目配额"）。
            storage.delete(key=assembled_key)
        else:
            storage.move(from_key=assembled_key, to_key=final_key)

        blob, _blob_created = repo.get_or_create_blob(
            storage_backend=storage.backend_name,
            bucket=None,
            object_key=final_key,
            sha256=assembled.sha256,
            byte_size=assembled.byte_size,
            mime_type="application/octet-stream",
        )
    except ApiError as exc:
        # `storage.assemble` 抛的 `part_missing` 会走到这里：分片在存储上
        # 已经不在了，重试也没用 —— 同样按不可恢复处理。
        _fail_completion(
            repo,
            storage,
            session_id=session_id,
            project_id=project_id,
            reserved_bytes=reserved_bytes,
        )
        return CompletionOutcome(failure=exc)
    # 其它异常（`OSError`、程序缺陷）**刻意不捕获**：它们不该被伪装成业务失败。
    # 此时会话留在 COMPLETING、预占仍挂在账上 —— 由 TTL 回收兜底
    # （`UPLOAD_STATES_HOLDING_RESERVATION` 刻意包含 COMPLETING 正是为了这个）。
    # 这里不释放是有意的：在一个**注定被回滚**的事务里做补偿动作，
    # 只是自欺欺人 —— 那一步会被一并撤销，而代码看起来像是处理了。

    # 预占 → 有效（按实际大小）。失败说明实际超出了余量。
    if not repo.finalize_reservation(
        project_id=project_id,
        reserved_release=reserved_bytes,
        effective_add=assembled.byte_size,
    ):
        failure = _quota_exceeded_on_actual_error(
            actual_bytes=assembled.byte_size,
            declared_bytes=declared_size,
            snapshot=quota_snapshot(repo, project_id),
            session_id=session_id,
        )
        _fail_completion(
            repo,
            storage,
            session_id=session_id,
            project_id=project_id,
            reserved_bytes=reserved_bytes,
        )
        return CompletionOutcome(failure=failure)

    file_row = repo.insert_file_object(
        blob_id=blob.id,
        original_name=original_name,
        visibility=VISIBILITY_PROJECT,
        project_id=project_id,
        classification=classification,
        uploaded_by=session_row.created_by,
    )
    repo.flush()

    repo.insert_link(project_id=project_id, file_id=file_row.id, purpose=purpose)

    if not repo.transition_session(
        session_id=session_id,
        from_states=[UPLOAD_STATE_COMPLETING],
        to_state=UPLOAD_STATE_COMPLETED,
        actual_sha256=assembled.sha256,
        received_bytes=assembled.byte_size,
        file_id=file_row.id,
    ):  # pragma: no cover - 行锁下不可能发生
        raise RuntimeError("会话状态在完成流程中被外部改动，检查并发路径")

    # 与业务变更同事务写 Outbox（协议 §4.1 第 8 步）。
    # 事件先落库，WP13 的投递器上线后自然开始消费 ——
    # 这样"文件已固化但没有扫描任务"的窗口不存在。
    repo.insert_outbox_event(
        aggregate_type=AGGREGATE_FILE_OBJECT,
        aggregate_id=file_row.id,
        event_type=EVENT_FILE_UPLOADED,
        payload={
            "fileId": str(file_row.id),
            "projectId": str(project_id),
            "blobId": str(blob.id),
            "byteSize": str(assembled.byte_size),
            "sha256": assembled.sha256,
            "classification": file_row.classification,
            "purpose": purpose,
        },
    )

    repo.flush()

    # 分片已完成使命（内容已拼装并落到 object_key 上），临时分片必须清掉。
    #
    # **不清的后果不是"浪费一点空间"，而是账实不符**：
    # 300 MB 的文件会永久留下 300 MB 的分片副本（外加正式对象 300 MB），
    # 而配额只按 1 倍计 —— 磁盘被填满时账面上还显示大量余量。
    # 更麻烦的是**没有任何清理路径会碰到它**：TTL 回收只捞"仍持有预占"
    # 的会话（UPLOADING / COMPLETING），已完成的会话不在候选集里。
    # 本机实测确认过这个残留（`objects/...` 与 `parts/<id>/1` 同时在盘上）。
    storage.delete_prefix(prefix=upload_prefix(session_id))

    return CompletionOutcome(
        result=CompletedUpload(
            file_id=file_row.id,
            blob_id=blob.id,
            original_name=file_row.original_name,
            byte_size=assembled.byte_size,
            sha256=assembled.sha256,
            classification=file_row.classification,
            created=True,
        )
    )


def _checksum_mismatch_error(
    *, expected_sha256: str, actual_sha256: str, session_id: uuid.UUID
) -> ApiError:
    """摘要不符的错误体。

    抽出来的理由不是"少几行"，而是：`details` 的键名是**前端契约的一部分**，
    而 `complete_upload` 的主线是"核实 → 固化 → 转配额 → 写事件"。
    把一段纯数据组装塞在主线中间，会让读的人以为它参与了流程控制。
    """
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        "文件摘要与声明不一致，文件可能已损坏。",
        details={
            "reason": "checksum_mismatch",
            "expectedSha256": expected_sha256,
            "actualSha256": actual_sha256,
            "uploadId": str(session_id),
        },
    )


def _quota_exceeded_on_actual_error(
    *,
    actual_bytes: int,
    declared_bytes: int,
    snapshot: dict[str, int],
    session_id: uuid.UUID,
) -> ApiError:
    """实际大小超余量的错误体（OP-93 口径下的失败分支）。

    回带 `declaredBytes` 是为了让前端能直接说清
    "你声明的是 X、实际是 Y、还差 Z" —— 少了它，
    用户只会看到"太大了"，而不知道该清理多少。
    """
    return ApiError(
        ErrorCode.UPLOAD_TOO_LARGE,
        "文件实际大小超出该项目剩余的研究容量。",
        details={
            "reason": "quota_exceeded_on_actual_size",
            "actualBytes": str(actual_bytes),
            "declaredBytes": str(declared_bytes),
            "limitBytes": str(snapshot["limitBytes"]),
            "remainingQuotaBytes": str(
                max(
                    0,
                    snapshot["limitBytes"]
                    - snapshot["effectiveBytes"]
                    - snapshot["reservedBytes"],
                )
            ),
            "uploadId": str(session_id),
        },
    )


def _fail_completion(
    repo: FileRepository,
    storage: StorageBackend,
    *,
    session_id: uuid.UUID,
    project_id: uuid.UUID,
    reserved_bytes: int,
) -> None:
    """把会话落到 `FAILED` 并归还预占（**调用方随后必须返回、不能抛**）。

    这三步必须随本事务提交才生效，因此调用点都在"返回 `CompletionOutcome
    (failure=...)`"之前，而不是在异常处理器里 —— 见 `CompletionOutcome`
    的 docstring 对"为什么异常传播会撤销这三步"的说明。

    清临时分片与拼装残留：会话已终结，这些字节不该留在盘上
    （它们不在 TTL 的候选集里 —— 回收只捞"仍持有预占"的会话，
    而这里刚刚把状态改成了 FAILED）。
    """
    repo.transition_session(
        session_id=session_id,
        from_states=[UPLOAD_STATE_COMPLETING],
        to_state=UPLOAD_STATE_FAILED,
    )
    repo.release_reservation(project_id=project_id, delta=reserved_bytes)
    storage.delete_prefix(prefix=upload_prefix(session_id))


def _completed_from_existing(
    repo: FileRepository, session_row: UploadSession
) -> CompletedUpload:
    """重复完成时构造返回值（不触碰任何数据）。"""
    assert session_row.file_id is not None  # CHECK 保证 COMPLETED 必有 file_id
    file_row, blob = get_file(repo, file_id=session_row.file_id)
    return CompletedUpload(
        file_id=file_row.id,
        blob_id=blob.id,
        original_name=file_row.original_name,
        byte_size=blob.byte_size,
        sha256=blob.sha256,
        classification=file_row.classification,
        created=False,
    )


# 注：本项目**不使用任何进程内状态**来携带"会话的附加信息"。
#
# 开发本模块时曾用模块级 dict 暂存 `original_name` / `classification`
# （当时的理由是文档 02 §4 的 upload_sessions 列清单里没有这两列，
# 不想擅自加列）。随即自查发现它在**选定架构下必然失效**：
# 部署模式是"API 双实例"（研发执行计划 §5 的 D4 选定），
# 创建会话的请求可能落在实例 A、完成上传的请求落在实例 B。
#
# 而它的失败还是**概率性**的（取决于负载均衡把请求分到哪台），
# 单机测试**永远发现不了** —— 这是最坏的一类缺陷：测试全绿、上线随机报错。
#
# 因此两列最终落在 `upload_sessions` 表上（见 models.py 的说明）。
# 它们也只有这一个可行位置：文件名的权威来源必须是创建会话那一次请求，
# 让客户端在 `completions` 里重传反而制造了"上传报 A、完成报 B"的替换面。


# ---------------------------------------------------------------------------
# 服务端生成资产落库（WP27 §2.4 跨包接缝）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredGeneratedAsset:
    """一次"服务端生成的字节"落库后的结果。

    **为什么 `blob` 与 `file_object` 要一起返回**：它们代表两件不同的事实。
    `blob` 是**物理内容**（按 `sha256` 去重，同内容全库一份）；
    `file_object` 是**一次逻辑引用**（同内容被两个业务对象引用时是两个 id）。
    调用方要拿 `file_id` 落业务外键，但**绝不能**拿 `blob_id` 当业务外键 ——
    那会让"删 A 不影响 B"（R9）在数据层就做不到。
    """

    file_id: uuid.UUID
    blob_id: uuid.UUID
    original_name: str
    byte_size: int
    sha256: str
    classification: str
    #: 物理内容是否本次新写入（False = 命中物理去重，字节早就在盘上）。
    blob_created: bool


def store_generated_asset(
    repo: FileRepository,
    storage: StorageBackend,
    *,
    project_id: uuid.UUID,
    original_name: str,
    content: bytes,
    mime_type: str,
    uploaded_by: uuid.UUID,
    classification: str | None = None,
    purpose: str = PURPOSE_GENERATED,
) -> StoredGeneratedAsset:
    """把服务端生成的字节（图片等）落成 `blob` + `file_object` + `project_file_link`。

    **这是 `files` 模块对其它模块公开的**唯一"非上传"写入接缝。
    M3 的 `media` 需要把生成图存成正式资产，而全仓唯一的 `FileObject`
    构造点此前只被 `complete_upload`（上传会话）驱动 ——
    也就是说**服务端没有"把已在内存里的字节直接落成 file_object"的公开路径**。
    本函数补的就是这条路径，且**刻意不改动 `complete_upload`**：
    上传链路有分片、预占、幂等状态机，生成链路一样都没有，
    两条链路强行合并会让上传的状态机被生成场景污染。

    **为什么接缝必须由 `files` 拥有**（而不是 `media` 直插表或直调 repo）：
    `object_key` 规则、物理去重、配额口径、`visibility_matches_project`
    CHECK、Outbox 事件形状，全都在本模块内已经有唯一实现；
    在 `media` 里重写一遍就是"同一事实两处实现"。

    **配额**：`GENERATED` 不在 `QUOTA_PURPOSES` 里（models.py 明确
    "不包含 Brief、生成媒体和系统提取出的内部索引"），
    所以本函数**刻意不调 `reserve_quota` / `finalize_reservation`** ——
    调了就是给生成资产计研究配额，与需求 §6.2 原文冲突。

    **物理去重与逻辑独立是分开的**：
    同内容第二次调用 → `get_or_create_blob` 返回同一个 `blob`（`blob_created=False`），
    但 `insert_file_object` 每次都新建一行 ⇒ 两个不同的 `file_id`。
    这正是 R9（预览资产与正式资产互不影响）要的性质。

    **先写物理、后建行**：`STORAGE_STATES` 没有"字节缺失"这一档，
    所以顺序不能反 —— 先建行会在字节写失败时留下一个指向空对象的对象。
    反过来字节写成功但事务回滚，只会留下一个**无引用**的孤儿 blob，
    它由内容寻址保证下次同内容进来时会被复用（`blob_created=False`），不构成泄漏。
    """
    safe_name, safe_purpose, safe_classification = _normalize_generated_args(
        original_name=original_name,
        mime_type=mime_type,
        purpose=purpose,
        classification=classification,
    )

    byte_size = len(content)
    if byte_size == 0:
        raise _validation_error(
            "content",
            "生成资产的字节不能为空。",
            reason="content_empty",
        )

    # 摘要在**服务端**算，不接受调用方传入 ——
    # 否则调用方给错摘要就会让 object_key 与实际内容不一致（去重静默失效）。
    sha256 = hashlib.sha256(content).hexdigest()

    return _persist_generated_stream(
        repo,
        storage,
        stream=io.BytesIO(content),
        sha256=sha256,
        byte_size=byte_size,
        project_id=project_id,
        safe_name=safe_name,
        safe_purpose=safe_purpose,
        safe_classification=safe_classification,
        uploaded_by=uploaded_by,
    )


def store_generated_asset_from_path(
    repo: FileRepository,
    storage: StorageBackend,
    *,
    project_id: uuid.UUID,
    original_name: str,
    path: Path,
    mime_type: str,
    uploaded_by: uuid.UUID,
    classification: str | None = None,
    purpose: str = PURPOSE_GENERATED,
) -> StoredGeneratedAsset:
    """`store_generated_asset` 的**流式**孪生：内容来自磁盘上的一个文件，不进内存。

    存在的理由（WP35）：批量 ZIP 导出最多 50 个内容、每个多张图，
    整包读成 `bytes` 会让内存随批次线性增长。本函数分两遍**流式**读该文件
    （先算 sha256，再 `write_part`），常驻内存只有一个读块。

    **与 bytes 版共用同一条落库尾巴**（`_persist_generated_stream`）：
    `object_key` 规则、物理去重、`file_object`/`link` 的构造、配额口径
    （`GENERATED`/`EXPORT` 都不计研究配额）只有一份实现。
    两个入口对同样的字节必须得到同样的 `sha256`/`object_key`，
    由 `tests/test_files_generated_asset.py` 的同值判据钉住。
    """
    safe_name, safe_purpose, safe_classification = _normalize_generated_args(
        original_name=original_name,
        mime_type=mime_type,
        purpose=purpose,
        classification=classification,
    )

    byte_size = path.stat().st_size
    if byte_size == 0:
        raise _validation_error(
            "content",
            "生成资产的字节不能为空。",
            reason="content_empty",
        )

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_GENERATED_READ_CHUNK), b""):
            digest.update(chunk)

    with path.open("rb") as handle:
        return _persist_generated_stream(
            repo,
            storage,
            stream=handle,
            sha256=digest.hexdigest(),
            byte_size=byte_size,
            project_id=project_id,
            safe_name=safe_name,
            safe_purpose=safe_purpose,
            safe_classification=safe_classification,
            uploaded_by=uploaded_by,
        )


#: 流式读块大小。1 MiB：足够摊薄系统调用，又不会让常驻内存显眼。
_GENERATED_READ_CHUNK: Final[int] = 1024 * 1024


def _normalize_generated_args(
    *,
    original_name: str,
    mime_type: str,
    purpose: str,
    classification: str | None,
) -> tuple[str, str, str]:
    """两个入口共用的入参归一化与校验。返回 `(name, purpose, classification)`。"""
    safe_name = normalize_original_name(original_name)
    safe_mime = normalize_mime_type(mime_type)
    safe_purpose = normalize_purpose(purpose)
    safe_classification = (
        classify_file(original_name=safe_name, mime_type=safe_mime)
        if classification is None
        else classification
    )
    if safe_classification not in (CLASSIFICATION_SUPPORTED, CLASSIFICATION_UNSUPPORTED):
        raise _validation_error(
            "classification",
            "classification 只能是 SUPPORTED 或 UNSUPPORTED。",
            reason="classification_invalid",
        )
    return safe_name, safe_purpose, safe_classification


def _persist_generated_stream(
    repo: FileRepository,
    storage: StorageBackend,
    *,
    stream: BinaryIO,
    sha256: str,
    byte_size: int,
    project_id: uuid.UUID,
    safe_name: str,
    safe_purpose: str,
    safe_classification: str,
    uploaded_by: uuid.UUID,
) -> StoredGeneratedAsset:
    """落库尾巴：写暂存 → 去重/移动 → `blob` → `file_object` → `link`。

    调用方负责**已经算好** `sha256`/`byte_size` 并保证它们与 `stream` 一致；
    后端回报的摘要若不一致视为契约违约（`RuntimeError`）。
    """
    final_key = object_key_for(sha256)

    # 追加式写不能直接落到最终 key（同内容已存在时会把内容追加坏），
    # 所以先写暂存 key，再按"最终 key 是否已存在"决定 move 还是丢弃。
    staging_key = staging_key_for(
        purpose=safe_purpose.lower(), token=uuid.uuid4()
    )
    stored = storage.write_part(key=staging_key, stream=stream)
    if stored.sha256 != sha256 or stored.byte_size != byte_size:  # pragma: no cover - 后端契约违约
        storage.delete(key=staging_key)
        raise RuntimeError("存储后端返回的摘要与内容不一致，违反 StorageBackend 契约")

    if storage.exists(key=final_key):
        # 物理去重命中：本次字节不重复落盘，暂存件直接丢。
        storage.delete(key=staging_key)
    else:
        storage.move(from_key=staging_key, to_key=final_key)

    blob, blob_created = repo.get_or_create_blob(
        storage_backend=storage.backend_name,
        bucket=None,
        object_key=final_key,
        sha256=sha256,
        byte_size=byte_size,
        # 与 `complete_upload` 同口径：blob 的 mime 记成通用二进制，
        # 真实类型由 file_object 侧的分类与调用方自己的记录承载。
        mime_type="application/octet-stream",
    )

    file_row = repo.insert_file_object(
        blob_id=blob.id,
        original_name=safe_name,
        visibility=VISIBILITY_PROJECT,
        project_id=project_id,
        classification=safe_classification,
        uploaded_by=uploaded_by,
    )
    repo.flush()

    repo.insert_link(
        project_id=project_id, file_id=file_row.id, purpose=safe_purpose
    )

    repo.flush()

    return StoredGeneratedAsset(
        file_id=file_row.id,
        blob_id=blob.id,
        original_name=file_row.original_name,
        byte_size=byte_size,
        sha256=sha256,
        classification=file_row.classification,
        blob_created=blob_created,
    )


# ---------------------------------------------------------------------------
# 取消 / 过期回收
# ---------------------------------------------------------------------------


def cancel_upload(
    repo: FileRepository, storage: StorageBackend, *, session_id: uuid.UUID
) -> bool:
    """取消上传并**幂等**释放预占。返回是否本次真的取消了。

    协议 §4.2 要求"取消并幂等释放预占"，文档 02 §13.1 要求
    "每次释放只允许一次" —— 两者靠**条件状态迁移**同时满足：
    只有 `UPLOADING` / `COMPLETING` → `CANCELLED` 这次迁移成功
    （`rowcount == 1`），才去归还额度。重复的取消请求拿到 False，
    直接返回 204（而不是 404/409）—— 客户端的目的已经达成。
    """
    session_row = require_session(repo, session_id, for_update=True)

    # 幂等：已取消/已过期/已完成都直接返回"没有发生变化"。
    # **不报错**是刻意的：用户点两次"取消"、或网络重试，
    # 报错只会让他以为取消失败。
    if session_row.state not in UPLOAD_STATES_HOLDING_RESERVATION:
        return False

    project_id = session_row.project_id
    reserved_bytes = session_row.reserved_bytes

    if not repo.transition_session(
        session_id=session_id,
        from_states=sorted(UPLOAD_STATES_HOLDING_RESERVATION),
        to_state=UPLOAD_STATE_CANCELLED,
    ):
        return False

    repo.release_reservation(project_id=project_id, delta=reserved_bytes)
    repo.delete_parts(session_id)
    storage.delete_prefix(prefix=upload_prefix(session_id))
    return True


def expire_stale_sessions(
    repo: FileRepository,
    storage: StorageBackend,
    *,
    now: datetime | None = None,
    limit: int = 200,
) -> ExpireReport:
    """回收过期会话的预占与临时分片（文档 02 §4「TTL 清理」）。

    **没有它，用户关掉浏览器就永久吃掉一块额度**：
    预占还挂在账上、文件没有固化、"有效占用"也没增加 ——
    账面看起来正常，余量却少了。这类问题在用户视角表现为
    "明明没传什么却传不上去了"，且**无法自查**。

    本函数是**可被反复调用的纯回收**（幂等）：已处理的会话不在
    候选集里。调度由部署层决定（内网接定时任务），本包不自己做调度 ——
    调度器属 WP13 的租约范畴。
    """
    moment = now or _utcnow()
    candidates = repo.list_expired_sessions(
        now=moment, limit=limit, holding_states=sorted(UPLOAD_STATES_HOLDING_RESERVATION)
    )

    expired = 0
    released = 0
    deleted_objects = 0

    for session_row in candidates:
        # **标量先取，再迁移**：`transition_session` 走的是 UPDATE 语句，
        # 执行后 session 内的 ORM 实例可能被 expire，之后再读属性会触发
        # 惰性加载（WP10 踩过：aborted 事务上读属性抛 PendingRollbackError）。
        session_id = session_row.id
        project_id = session_row.project_id
        reserved_bytes = session_row.reserved_bytes

        if not repo.transition_session(
            session_id=session_id,
            from_states=sorted(UPLOAD_STATES_HOLDING_RESERVATION),
            to_state=UPLOAD_STATE_EXPIRED,
        ):
            continue

        repo.release_reservation(project_id=project_id, delta=reserved_bytes)
        repo.delete_parts(session_id)
        deleted_objects += storage.delete_prefix(prefix=upload_prefix(session_id))

        expired += 1
        released += reserved_bytes

    return ExpireReport(
        expired_sessions=expired,
        released_bytes=released,
        deleted_objects=deleted_objects,
    )


# ---------------------------------------------------------------------------
# 可见性
# ---------------------------------------------------------------------------


def can_view_file(*, file_row: FileObject, is_admin: bool, is_project_member: bool) -> bool:
    """纯函数：该用户能否看到这个文件。

    规则与 WP11 的版本可见性一致（不可见一律 404 而非 403）：
      · 管理者可见全部；
      · PROJECT 可见性 → 该项目活跃成员；
      · KNOWLEDGE 可见性 → 全员（知识库是共享主数据）。
    """
    if is_admin:
        return True
    if file_row.visibility == VISIBILITY_PROJECT:
        return is_project_member
    return True


__all__ = [
    "MAX_ORIGINAL_NAME_LENGTH",
    "MAX_PARTS_PER_COMPLETION",
    "CompletedUpload",
    "CompletionOutcome",
    "ExpireReport",
    "StoredGeneratedAsset",
    "StoredPartView",
    "UploadSessionView",
    "can_view_file",
    "cancel_upload",
    "classify_file",
    "complete_upload",
    "create_upload_session",
    "expire_stale_sessions",
    "get_file",
    "get_upload_session",
    "max_part_no_for",
    "normalize_mime_type",
    "normalize_original_name",
    "normalize_purpose",
    "quota_snapshot",
    "require_non_negative_size",
    "require_session",
    "store_generated_asset",
    "store_generated_asset_from_path",
    "store_part",
]
