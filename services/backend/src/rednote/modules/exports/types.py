"""固定版本 ZIP 导出的取值域、原因码与纯值对象（WP35）。

本文件**只放常量与不可变值对象**，不 import 任何 `rednote.modules.*`
（平级模块不可互 import；跨模块取数走 `repository.py` 的裸 SQL）。

## 取值域为什么是这几个

| 常量 | 取值 | 依据 |
|---|---|---|
| `EXPORT_KIND_*` | `SINGLE` / `BATCH_ZIP` | M4 DDL 草案 §1.3 原样；一组只有 1 条内容 = `SINGLE`，≥2 条 = `BATCH_ZIP` |
| `EXPORT_FORMAT_STATIC` | `STATIC` | 本包只导静态图；`LIVE` 在 WP28 的真机验证前**一律拒绝**（R11），不产出任何"已验证"字样 |
| `RENDITION_*` | `DELIVERY` / `READY` | 与 `media.types.RENDITION_DELIVERY` / `RENDITION_STATE_READY` **同值、各写一份**（平级不可互 import）；一致性由测试对账 |

## 「正式资产」的唯一判据

`media_renditions` **没有 `formal` 字段**。正式资产 =
`kind = 'DELIVERY' AND state = 'READY'`，每 `(media_version_id, kind)` 取
`MAX(attempt_no)` 那一行。**取最新一次加工后再判 READY**，而不是"在所有 READY
里取最大"：后者会让"最新一次加工失败"的版本回落到上一次成功的旧产物，
即导出一张已被判定作废的图（R3）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Final

# --- 取值域（迁移里写化石字面量，本处是模型侧的活常量） -----------------------
EXPORT_KIND_SINGLE: Final[str] = "SINGLE"
EXPORT_KIND_BATCH_ZIP: Final[str] = "BATCH_ZIP"
EXPORT_KINDS: Final[tuple[str, ...]] = (EXPORT_KIND_SINGLE, EXPORT_KIND_BATCH_ZIP)
EXPORT_KIND_LIST_SQL: Final[str] = ", ".join(f"'{k}'" for k in EXPORT_KINDS)

EXPORT_FORMAT_STATIC: Final[str] = "STATIC"
EXPORT_FORMATS: Final[tuple[str, ...]] = (EXPORT_FORMAT_STATIC,)
EXPORT_FORMAT_LIST_SQL: Final[str] = ", ".join(f"'{k}'" for k in EXPORT_FORMATS)

#: 与 media 侧同值（各写一份，见模块 docstring）。
RENDITION_KIND_DELIVERY: Final[str] = "DELIVERY"
RENDITION_STATE_READY: Final[str] = "READY"

# --- 边界 ----------------------------------------------------------------------
#: 一次导出的内容条数上限。同步打包的请求时长与条数线性相关（V2 取舍），
#: 超限直接 422，不静默截断。
MAX_CONTENT_ITEMS_PER_EXPORT: Final[int] = 50
MIN_CONTENT_ITEMS_PER_EXPORT: Final[int] = 1

MAX_IDEMPOTENCY_KEY_CHARS: Final[int] = 160
SHA256_HEX_CHARS: Final[int] = 64

#: ZIP 内成员的固定时间戳（1980-01-01 00:00:00，ZIP 格式可表示的最小值）。
#: **钉死它是"固定版本"的前提**：`zipfile` 默认取当前时间写入成员头，
#: 同一份输入两次打包字节就不同 ⇒ `sha256` 不稳定 ⇒ ETag 失去意义。
ZIP_FIXED_DATE_TIME: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)

#: 导出产物文件名后缀与 MIME。
ZIP_MIME_TYPE: Final[str] = "application/zip"

# --- 原因码（`details.reason`，字面量，测试里逐条对账） --------------------------
REASON_PROJECT_NOT_FOUND: Final[str] = "PROJECT_NOT_FOUND"
REASON_EXPORT_NOT_FOUND: Final[str] = "EXPORT_NOT_FOUND"
REASON_CONTENT_NOT_FOUND: Final[str] = "CONTENT_NOT_FOUND"
REASON_CONTENT_IDS_EMPTY: Final[str] = "CONTENT_IDS_EMPTY"
REASON_CONTENT_IDS_TOO_MANY: Final[str] = "CONTENT_IDS_TOO_MANY"
REASON_CONTENT_IDS_DUPLICATE: Final[str] = "CONTENT_IDS_DUPLICATE"
REASON_CONTENT_NO_VERSION: Final[str] = "CONTENT_NO_VERSION"
REASON_NO_DELIVERY_ASSET: Final[str] = "NO_DELIVERY_ASSET"
REASON_FORMAT_NOT_SUPPORTED: Final[str] = "FORMAT_NOT_SUPPORTED"
REASON_IDEMPOTENCY_KEY_REUSED: Final[str] = "idempotency_key_reused"
REASON_ARTIFACT_MISSING: Final[str] = "ARTIFACT_MISSING"

IDEMPOTENCY_SCOPE_EXPORT_CREATE: Final[str] = "EXPORT_CREATE"


@dataclass(frozen=True)
class DeliveryAsset:
    """一张已冻结的正式图（来自 `media_renditions` 的一行）。"""

    media_item_id: uuid.UUID
    media_version_id: uuid.UUID
    sort_order: int
    rendition_id: uuid.UUID
    attempt_no: int
    file_object_id: uuid.UUID
    byte_size: int
    image_format: str


@dataclass(frozen=True)
class FrozenContent:
    """一条内容冻结后的快照：正文版本 + 该版本下的正式图（按 `sort_order` 升序）。"""

    content_item_id: uuid.UUID
    version_node_id: uuid.UUID
    version_no: int
    title: str
    body: str
    assets: tuple[DeliveryAsset, ...]


def eligibility_hash(frozen: FrozenContent) -> str:
    """导出资格指纹：正文版本 + 每张图的 `rendition_id`/`attempt_no` 的摘要。

    用途：导出后任一环节变化（回滚到别的版本、图被重新加工出更新的
    `attempt_no`）指纹即不同，WP34 据此判"导出资格失效"。
    纯函数；只吃冻结快照，不读库。
    """
    payload = {
        "content": str(frozen.content_item_id),
        "version": str(frozen.version_node_id),
        "assets": [
            [str(a.rendition_id), a.attempt_no, str(a.file_object_id)]
            for a in frozen.assets
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def request_hash(content_item_ids: tuple[uuid.UUID, ...], export_format: str) -> str:
    """请求摘要（同键异体的判据）。内容 id 排序后哈希 ⇒ 顺序无关。"""
    payload = {
        "ids": sorted(str(i) for i in content_item_ids),
        "format": export_format,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def export_kind_for(count: int) -> str:
    """条数 → 导出种类。1 条 = SINGLE，其余 = BATCH_ZIP。"""
    return EXPORT_KIND_SINGLE if count == 1 else EXPORT_KIND_BATCH_ZIP


__all__ = [
    "EXPORT_FORMATS",
    "EXPORT_FORMAT_LIST_SQL",
    "EXPORT_FORMAT_STATIC",
    "EXPORT_KINDS",
    "EXPORT_KIND_BATCH_ZIP",
    "EXPORT_KIND_LIST_SQL",
    "EXPORT_KIND_SINGLE",
    "IDEMPOTENCY_SCOPE_EXPORT_CREATE",
    "MAX_CONTENT_ITEMS_PER_EXPORT",
    "MAX_IDEMPOTENCY_KEY_CHARS",
    "MIN_CONTENT_ITEMS_PER_EXPORT",
    "REASON_ARTIFACT_MISSING",
    "REASON_CONTENT_IDS_DUPLICATE",
    "REASON_CONTENT_IDS_EMPTY",
    "REASON_CONTENT_IDS_TOO_MANY",
    "REASON_CONTENT_NOT_FOUND",
    "REASON_CONTENT_NO_VERSION",
    "REASON_EXPORT_NOT_FOUND",
    "REASON_FORMAT_NOT_SUPPORTED",
    "REASON_IDEMPOTENCY_KEY_REUSED",
    "REASON_NO_DELIVERY_ASSET",
    "REASON_PROJECT_NOT_FOUND",
    "RENDITION_KIND_DELIVERY",
    "RENDITION_STATE_READY",
    "SHA256_HEX_CHARS",
    "ZIP_FIXED_DATE_TIME",
    "ZIP_MIME_TYPE",
    "DeliveryAsset",
    "FrozenContent",
    "eligibility_hash",
    "export_kind_for",
    "request_hash",
]
