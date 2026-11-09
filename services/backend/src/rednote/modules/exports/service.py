"""固定版本 ZIP 导出的业务编排（WP35，形态 A' + 同步打包）。

## 一次导出的七步（顺序不可调换）

| # | 步 | 失败时 |
|---|---|---|
| 1 | 入参校验（条数 / 重复 / 格式） | 422，什么都没写 |
| 2 | 幂等：同项目同 `Idempotency-Key` 已有一组？同体 ⇒ **原样回放**；异体 ⇒ 409 | 不重复打包、不重复落盘 |
| 3 | 项目内归属：每个 `content_item_id` 必须属于本项目 | 404（防枚举，与"不存在"同形） |
| 4 | **冻结**：每条内容取"当前正文版本 + 该版本的正式图" | 逐条点名拒绝（见下） |
| 5 | 流式打包到**临时文件**（不进内存） | 清临时目录，什么都没写 |
| 6 | 流式落盘（`files.store_generated_asset_from_path`，`purpose=EXPORT`） | 事务回滚；孤儿 blob 由内容寻址复用 |
| 7 | 写组内全部 `export_items` 行（延迟触发器在提交时守组一致） | 事务回滚 |

## 第 4 步：拒绝路径（fail-closed，不静默跳过）

| reason | 触发 | 为什么拒而不是"导出能导的" |
|---|---|---|
| `CONTENT_NO_VERSION` | 内容一版正文都没有 | 没有可冻结的版本 |
| `NO_DELIVERY_ASSET` | 有媒体位，但**一张**可用的正式图都没有 | 交付包里只有文字，用户会以为配图丢了 |
| `NO_DELIVERY_ASSET` | 媒体位数 > 正式图数（缺图） | 同上：缺一张也是"以为齐了其实没齐" |

**无媒体位**（纯文字内容，`media_slot_count = 0`）是合法的：只打 `content.txt`。
这与"有媒体位但没出图"是两件事，判据是**位数**，不是图数。

一条内容不合格 ⇒ **整次导出拒绝**，而不是跳过它：跳过会让 `item_count` 与用户选的条数不一致。
响应的 `details.contentItemId` 点名是哪一条。

## 幂等

· 同键同体 ⇒ 回放既有组（`created=False`），**不**再打包、**不**再落盘；
· 同键异体 ⇒ `IDEMPOTENCY_CONFLICT`（409）；判据是 `request_hash`
  （内容 id 排序后 + 格式，顺序无关）；
· 无键 ⇒ 不做幂等，每次一组新的（协议 §1.3 通用语义）。

并发同键两个请求：部分唯一索引 `uq_export_items_idempotency` 兜底，
后到者在 INSERT 时撞唯一冲突 ⇒ 由 API 层回滚后重查回放。
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from rednote.modules.exports.repository import ExportRepository
from rednote.modules.exports.schemas import (
    SchemaViolation,
    build_zip,
    validate_content_item_ids,
)
from rednote.modules.exports.types import (
    EXPORT_FORMAT_STATIC,
    EXPORT_FORMATS,
    REASON_ARTIFACT_MISSING,
    REASON_CONTENT_NOT_FOUND,
    REASON_CONTENT_NO_VERSION,
    REASON_EXPORT_NOT_FOUND,
    REASON_FORMAT_NOT_SUPPORTED,
    REASON_IDEMPOTENCY_KEY_REUSED,
    REASON_NO_DELIVERY_ASSET,
    REASON_PROJECT_NOT_FOUND,
    ZIP_MIME_TYPE,
    FrozenContent,
    eligibility_hash,
    export_kind_for,
    request_hash,
)


class ExportError(Exception):
    """导出业务错误。`reason` 是 `types.REASON_*`，由 API 层翻成协议错误码。"""

    def __init__(self, reason: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details


@dataclass(frozen=True)
class ExportResult:
    """一次导出的结果（一整组）。"""

    export_group_id: uuid.UUID
    export_kind: str
    export_format: str
    item_count: int
    artifact_file_id: uuid.UUID
    zip_sha256: str
    zip_byte_size: int
    created: bool  # False = 幂等回放
    item_ids: tuple[uuid.UUID, ...]
    content_item_ids: tuple[uuid.UUID, ...]


def _result_from_rows(rows: Sequence, *, created: bool) -> ExportResult:
    head = rows[0]
    return ExportResult(
        export_group_id=head.export_group_id,
        export_kind=head.export_kind,
        export_format=head.export_format,
        item_count=int(head.item_count),
        artifact_file_id=head.artifact_file_id,
        zip_sha256=head.zip_sha256,
        zip_byte_size=int(head.zip_byte_size),
        created=created,
        item_ids=tuple(r.id for r in rows),
        content_item_ids=tuple(r.content_item_id for r in rows),
    )


def freeze_content(repo: ExportRepository, content_item_id: uuid.UUID) -> FrozenContent:
    """冻结一条内容：当前正文版本 + 该版本的正式图。拒绝路径见模块 docstring。"""
    version = repo.current_content_version(content_item_id)
    if version is None:
        raise ExportError(
            REASON_CONTENT_NO_VERSION,
            "该内容还没有正文版本，无法导出。",
            contentItemId=str(content_item_id),
        )
    version_id, version_no, title, body = version

    assets = repo.delivery_assets(content_item_id)
    slots = repo.media_slot_count(content_item_id)
    if slots > len(assets):
        raise ExportError(
            REASON_NO_DELIVERY_ASSET,
            "该内容有配图位没有可用的正式图，无法导出。",
            contentItemId=str(content_item_id),
            mediaSlotCount=slots,
            deliveryAssetCount=len(assets),
        )
    return FrozenContent(
        content_item_id=content_item_id,
        version_node_id=version_id,
        version_no=version_no,
        title=title,
        body=body,
        assets=tuple(assets),
    )


def current_eligibility_hash(repo: ExportRepository, *, content_item_id: uuid.UUID) -> str:
    """只读接缝（WP36 仲裁项①）：按**当前**正文版本与正式图重算资格指纹。

    零新逻辑 —— 逐字复用 `freeze_content` + `eligibility_hash`，与
    `create_export` 组装 `export_items` 时用的是同一对函数（`:253`）。
    存在的理由只是把这两步装进一个**只读**的公开签名，供 WP36 在"标记
    交付"时与交付行上复制的存量指纹比对，判断内容是否已经改稿。

    继承 `freeze_content` 的拒绝路径（无版本 / 配图不足 ⇒ `ExportError`）：
    调用方若把"算不出指纹"当成"资格已失效"处理是合理的，但那是调用方
    的判断，本函数不做任何降级或吞异常。
    """
    return eligibility_hash(freeze_content(repo, content_item_id))


def current_eligibility_snapshot(
    repo: ExportRepository, *, content_item_id: uuid.UUID
) -> tuple[uuid.UUID, str]:
    """只读接缝（WP34 S2）：同一次读取返回 `(version_node_id, eligibility_hash)`。

    为什么不让调用方自己 `freeze_content` + `eligibility_hash` 拼：那是第二份实现，
    而且两次读取之间当前版本可能前进（快照分裂）。WP34 要区分「版本被取代」与
    「版本没变但配图变了」，必须拿到**同一快照**下的当前版本节点与指纹。

    零新逻辑，拒绝路径与 `current_eligibility_hash` 完全相同（无版本 / 配图不足 ⇒
    `ExportError`，不降级、不吞异常）；`current_eligibility_hash` 的签名与行为不变。
    """
    frozen = freeze_content(repo, content_item_id)
    return frozen.version_node_id, eligibility_hash(frozen)


def create_export(
    repo: ExportRepository,
    file_repo,
    storage,
    *,
    project_id: uuid.UUID,
    content_item_ids: Sequence[uuid.UUID],
    export_format: str,
    idempotency_key: str | None,
    created_by: uuid.UUID,
) -> ExportResult:
    """同步创建一组导出。`file_repo` / `storage` 由 API 层注入（同 session）。"""
    # 本地导入：`files` 是重模块，且避免模块加载期的循环（同 media 先例）。
    from rednote.modules.files.models import PURPOSE_EXPORT
    from rednote.modules.files.service import store_generated_asset_from_path

    # --- 1. 入参 ----------------------------------------------------------
    if export_format not in EXPORT_FORMATS:
        raise ExportError(
            REASON_FORMAT_NOT_SUPPORTED,
            "当前只支持导出静态图（STATIC）。",
            exportFormat=export_format,
            supported=list(EXPORT_FORMATS),
        )
    try:
        ids = validate_content_item_ids(list(content_item_ids))
    except SchemaViolation as exc:
        raise ExportError(exc.reason, str(exc)) from exc

    if not repo.project_exists(project_id):
        raise ExportError(REASON_PROJECT_NOT_FOUND, "项目不存在。")

    req_hash = request_hash(ids, export_format)

    # --- 2. 幂等 ------------------------------------------------------------
    def _replay() -> ExportResult | None:
        """同键已有一组：同体 ⇒ 回放；异体 ⇒ 409；没有 ⇒ `None`。"""
        if idempotency_key is None:
            return None
        existing = repo.find_by_idempotency(
            project_id=project_id, idempotency_key=idempotency_key
        )
        if not existing:
            return None
        if existing[0].request_hash != req_hash:
            raise ExportError(
                REASON_IDEMPOTENCY_KEY_REUSED,
                "相同幂等键对应了不同的请求内容。",
                idempotencyKey=idempotency_key,
            )
        return _result_from_rows(existing, created=False)

    replayed = _replay()
    if replayed is not None:
        return replayed

    # --- 3. 归属 ------------------------------------------------------------
    owners = repo.content_projects(ids)
    for cid in ids:
        if owners.get(cid) != project_id:
            # 不存在 与 属于别的项目 同形（防枚举）。
            raise ExportError(
                REASON_CONTENT_NOT_FOUND, "内容不存在。", contentItemId=str(cid)
            )

    # --- 4. 冻结 ------------------------------------------------------------
    frozen = [freeze_content(repo, cid) for cid in ids]

    def _pack_and_write() -> ExportResult:
        # --- 5/6. 流式打包 → 流式落盘 -----------------------------------------
        with tempfile.TemporaryDirectory(prefix="rednote-export-") as tmp_dir:
            zip_path = Path(tmp_dir) / "export.zip"

            def open_asset(file_object_id: uuid.UUID) -> Iterator[bytes]:
                located = repo.file_blob_key(file_object_id)
                if located is None:
                    raise ExportError(
                        REASON_ARTIFACT_MISSING,
                        "正式图的文件记录不存在。",
                        fileObjectId=str(file_object_id),
                    )
                return storage.open_stream(key=located[1])

            with zip_path.open("wb") as handle:
                build_zip(handle, frozen, open_asset)

            group_id = uuid.uuid4()
            stored = store_generated_asset_from_path(
                file_repo,
                storage,
                project_id=project_id,
                original_name=f"export-{str(group_id)[:8]}.zip",
                path=zip_path,
                mime_type=ZIP_MIME_TYPE,
                uploaded_by=created_by,
                purpose=PURPOSE_EXPORT,
            )

        # --- 7. 组内全部行（延迟触发器在提交时守组一致） ------------------------
        kind = export_kind_for(len(frozen))
        rows = [
            repo.insert_export_item(
                project_id=project_id,
                export_group_id=group_id,
                content_item_id=item.content_item_id,
                version_node_id=item.version_node_id,
                eligibility_hash=eligibility_hash(item),
                export_kind=kind,
                export_format=EXPORT_FORMAT_STATIC,
                item_count=len(frozen),
                artifact_file_id=stored.file_id,
                zip_sha256=stored.sha256,
                zip_byte_size=stored.byte_size,
                idempotency_key=idempotency_key,
                request_hash=req_hash,
                created_by=created_by,
            )
            for item in frozen
        ]
        repo.flush()  # 并发同键的唯一冲突在这里抛
        return _result_from_rows(rows, created=True)

    # 5~7 包在保存点里：并发同键的后到者撞 `uq_export_items_idempotency` 时，
    # 只回滚这一段（不连带外层事务），随后重查回放。
    # ⚠️ 只在带键时才可能是"并发同键"；无键的 IntegrityError 一律原样抛出。
    try:
        with repo.session.begin_nested():
            return _pack_and_write()
    except IntegrityError:
        if idempotency_key is None:
            raise
        replayed = _replay()
        if replayed is None:
            raise
        return replayed


def get_group(
    repo: ExportRepository, *, project_id: uuid.UUID, export_group_id: uuid.UUID
) -> list:
    """取一整组。组不存在或不属本项目 ⇒ `EXPORT_NOT_FOUND`（两者同形，防枚举）。"""
    rows = repo.list_group(export_group_id)
    if not rows or rows[0].project_id != project_id:
        raise ExportError(REASON_EXPORT_NOT_FOUND, "导出记录不存在。")
    return rows


__all__ = [
    "ExportError",
    "ExportResult",
    "create_export",
    "current_eligibility_hash",
    "freeze_content",
    "get_group",
]
