"""KOC 交付与发布登记的取值域、原因码与纯值对象（WP36）。

本文件**只放常量、纯函数与不可变值对象**，不 import 任何 `rednote.modules.*`
（平级模块不可互 import；跨模块取数走 `repository.py` / `export_bridge.py` 的裸 SQL）。

## 取值域为什么是这四个

| 常量族 | 取值 | 依据 |
|---|---|---|
| `DELIVERY_STATE_*` | `PREPARED` / `DELIVERED` / `CANCELLED` | `docs/m4/M4-DDL草案.md` §1.2 原样 |
| `PUBLICATION_STATE_*` | `DRAFT` / `PUBLISHED` / `UNKNOWN` | 同文 §2.1 |
| `CONTENT_STATE_*` | `VERIFIED` / `UNVERIFIED` | 同文 §2.1。★ **取值域里刻意没有"已删除"**：
  草案点名的反例是"抓取失败被推成已删除"，取值域只有两个值 ⇒ 结构上推不出来 |
| `LINK_KIND_*` | `SHORT` / `LONG` / `OTHER` | 同文 §2.2（本包只建表，判定归 WP37） |

## 三个 `request_hash` 为什么要分开写

`deliveries` 的两个创建入口（建交付 / 替换交付）**共用一张表、一个幂等索引**。
若两者的请求摘要口径相同，"对 A 的替换"与"建一条新交付"在同一个键下会被
判成同体而**静默回放**。因此摘要载荷里第一项就是 `scope`，且替换的载荷额外
吃进 `replaces`（被替换的旧行 id）—— 这正是口径文档「必须比对请求体指纹」
（`docs/m4/M4契约与幂等口径.md:83`）要求的那一层。

与 `exports/types.py:124-132` 同款：`json.dumps(sort_keys=True)` + sha256，
键的顺序不影响结果，`None` 与缺省同形（都序列化成 `null`）。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

# --- 取值域（迁移里写化石字面量，本处是模型侧的活常量） -----------------------
DELIVERY_STATE_PREPARED: Final[str] = "PREPARED"
DELIVERY_STATE_DELIVERED: Final[str] = "DELIVERED"
DELIVERY_STATE_CANCELLED: Final[str] = "CANCELLED"
DELIVERY_STATES: Final[tuple[str, ...]] = (
    DELIVERY_STATE_PREPARED,
    DELIVERY_STATE_DELIVERED,
    DELIVERY_STATE_CANCELLED,
)
DELIVERY_STATE_LIST_SQL: Final[str] = ", ".join(f"'{s}'" for s in DELIVERY_STATES)

PUBLICATION_STATE_DRAFT: Final[str] = "DRAFT"
PUBLICATION_STATE_PUBLISHED: Final[str] = "PUBLISHED"
PUBLICATION_STATE_UNKNOWN: Final[str] = "UNKNOWN"
PUBLICATION_STATES: Final[tuple[str, ...]] = (
    PUBLICATION_STATE_DRAFT,
    PUBLICATION_STATE_PUBLISHED,
    PUBLICATION_STATE_UNKNOWN,
)
PUBLICATION_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{s}'" for s in PUBLICATION_STATES
)

CONTENT_STATE_VERIFIED: Final[str] = "VERIFIED"
CONTENT_STATE_UNVERIFIED: Final[str] = "UNVERIFIED"
CONTENT_STATES: Final[tuple[str, ...]] = (
    CONTENT_STATE_VERIFIED,
    CONTENT_STATE_UNVERIFIED,
)
CONTENT_STATE_LIST_SQL: Final[str] = ", ".join(f"'{s}'" for s in CONTENT_STATES)

LINK_KIND_SHORT: Final[str] = "SHORT"
LINK_KIND_LONG: Final[str] = "LONG"
LINK_KIND_OTHER: Final[str] = "OTHER"
LINK_KINDS: Final[tuple[str, ...]] = (LINK_KIND_SHORT, LINK_KIND_LONG, LINK_KIND_OTHER)
LINK_KIND_LIST_SQL: Final[str] = ", ".join(f"'{k}'" for k in LINK_KINDS)

#: 本包登记发布时的 `content_state` 初值。
#: 本包**不做抓取** ⇒ 一律 `UNVERIFIED`（"没核验过"，不是"核验失败"，更不是"已删除"）。
CONTENT_STATE_INITIAL: Final[str] = CONTENT_STATE_UNVERIFIED

#: 本包创建交付的初始状态。`delivered_at` 只能由"标记已交付"端点置（§6.1）。
DELIVERY_STATE_INITIAL: Final[str] = DELIVERY_STATE_PREPARED

# --- 边界 ----------------------------------------------------------------------
#: 与 WP35 同值（`exports/types.py:51`）。契约草案写的是 128，**以磁盘为准**，
#: 两处不一致已列入契约修订清单。
MAX_IDEMPOTENCY_KEY_CHARS: Final[int] = 160
SHA256_HEX_CHARS: Final[int] = 64

MAX_DISPLAY_NAME_CHARS: Final[int] = 200
MAX_PUBLICATION_KEY_CHARS: Final[int] = 64

#: 业务主键的格式。**不得自行放宽** —— WP39 的指标导入按这个键找发布
#: （`templates/metric_import_field_guide.md` 口径；契约 `:1103` 同 pattern）。
PUBLICATION_KEY_REGEX_SQL: Final[str] = r"^[A-Za-z0-9_.\-]{1,64}$"
PUBLICATION_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9_.\-]{1,64}\Z")

# --- 原因码（`details.reason`，字面量，测试里逐条对账） --------------------------
REASON_PROJECT_NOT_FOUND: Final[str] = "PROJECT_NOT_FOUND"
REASON_KOC_NOT_FOUND: Final[str] = "KOC_NOT_FOUND"
REASON_DELIVERY_NOT_FOUND: Final[str] = "DELIVERY_NOT_FOUND"
REASON_EXPORT_ITEM_NOT_FOUND: Final[str] = "EXPORT_ITEM_NOT_FOUND"
REASON_DISPLAY_NAME_BLANK: Final[str] = "DISPLAY_NAME_BLANK"
REASON_PUBLICATION_KEY_INVALID: Final[str] = "PUBLICATION_KEY_INVALID"
REASON_PUBLICATION_KEY_TAKEN: Final[str] = "PUBLICATION_KEY_TAKEN"
REASON_PUBLISHED_AT_SHAPE: Final[str] = "PUBLISHED_AT_SHAPE"
REASON_INVALID_TRANSITION: Final[str] = "INVALID_TRANSITION"
REASON_DELIVERY_CANCELLED: Final[str] = "DELIVERY_CANCELLED"
REASON_ALREADY_REPLACED: Final[str] = "ALREADY_REPLACED"
REASON_ELIGIBILITY_STALE: Final[str] = "ELIGIBILITY_STALE"
REASON_ELIGIBILITY_UNAVAILABLE: Final[str] = "ELIGIBILITY_UNAVAILABLE"
REASON_IDEMPOTENCY_KEY_REUSED: Final[str] = "idempotency_key_reused"
# --- WP37 ---
REASON_PUBLICATION_NOT_FOUND: Final[str] = "PUBLICATION_NOT_FOUND"
REASON_LINK_URL_INVALID: Final[str] = "LINK_URL_INVALID"
REASON_LINK_CANONICAL_TAKEN: Final[str] = "LINK_CANONICAL_TAKEN"
REASON_CROSS_PROJECT_NOTE: Final[str] = "CROSS_PROJECT_NOTE"
REASON_VERIFY_RESULT_INVALID: Final[str] = "VERIFY_RESULT_INVALID"

#: 链接原文的长度上限（契约 `PublicationLinkCreate.rawUrl.maxLength` / 查重参数同值）。
MAX_LINK_URL_CHARS: Final[int] = 2048

IDEMPOTENCY_SCOPE_DELIVERY_CREATE: Final[str] = "DELIVERY_CREATE"
IDEMPOTENCY_SCOPE_DELIVERY_REPLACE: Final[str] = "DELIVERY_REPLACE"
IDEMPOTENCY_SCOPE_PUBLICATION_CREATE: Final[str] = "PUBLICATION_CREATE"


@dataclass(frozen=True)
class FrozenExportItem:
    """`export_items` 一行里**本包要复制的那几列**（由 `export_bridge` 读出）。

    为什么是"复制"而不是"引用后现查"：交付记录必须能独立回答"当时交付的是哪一版"。
    若渲染时现查 `export_items`，一旦上游行被重写（它是不可变表，但约束是库层的，
    不是宇宙定律），历史交付就会跟着漂。与 `report_versions.metrics_snapshot`
    「必须固化，否则历史报告随新指标漂移」是同一条论证。
    """

    export_item_id: uuid.UUID
    project_id: uuid.UUID
    content_item_id: uuid.UUID
    version_node_id: uuid.UUID
    eligibility_hash: str
    export_group_id: uuid.UUID


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _opt(value: object) -> str | None:
    return None if value is None else str(value)


def delivery_create_request_hash(
    *,
    export_item_id: uuid.UUID,
    koc_record_id: uuid.UUID | None,
    note: str | None,
) -> str:
    """`POST /projects/{pid}/deliveries` 的请求摘要（同键异体的判据）。

    **不吃** `content_item_id` / `version_node_id`：它们由 `export_item_id`
    唯一决定（从 `export_items` 复制），客户端根本不传。把派生值塞进摘要
    会让"同一个请求"在上游行被读到不同值时算出不同指纹 —— 那是假冲突。
    """
    return _digest(
        {
            "scope": IDEMPOTENCY_SCOPE_DELIVERY_CREATE,
            "exportItem": str(export_item_id),
            "koc": _opt(koc_record_id),
            "note": note,
        }
    )


def delivery_replace_request_hash(
    *,
    replaces_delivery_id: uuid.UUID,
    export_item_id: uuid.UUID,
    koc_record_id: uuid.UUID | None,
    note: str | None,
) -> str:
    """`POST /deliveries/{id}/replacements` 的请求摘要。

    ★ 载荷里**必须**有 `replaces`：替换与创建共用 `uq_deliveries_idempotency`，
    若两者摘要口径相同，"替换 A"与"替换 B"（或"直接建一条"）在同一个键下
    会被判成同体而静默回放首次结果 —— 用户以为换了版其实没换。
    """
    return _digest(
        {
            "scope": IDEMPOTENCY_SCOPE_DELIVERY_REPLACE,
            "replaces": str(replaces_delivery_id),
            "exportItem": str(export_item_id),
            "koc": _opt(koc_record_id),
            "note": note,
        }
    )


def publication_create_request_hash(
    *,
    publication_key: str,
    delivery_id: uuid.UUID | None,
    note_id: str | None,
    note_url: str | None,
    state: str,
    published_at: datetime | None,
) -> str:
    """`POST /projects/{pid}/publications` 的请求摘要。

    业务键 `uq_publications_project_key` 只挡"同键重复登记"，挡不住
    "同一个 `Idempotency-Key` 配了不同的登记内容"（审计 `:62` 的"⚠️ 部分"）。
    两条索引**并存**，各管一头。
    """
    return _digest(
        {
            "scope": IDEMPOTENCY_SCOPE_PUBLICATION_CREATE,
            "key": publication_key,
            "delivery": _opt(delivery_id),
            "noteId": note_id,
            "noteUrl": note_url,
            "state": state,
            "publishedAt": None if published_at is None else published_at.isoformat(),
        }
    )


def is_valid_publication_key(value: str) -> bool:
    """业务主键格式校验（纯函数）。与库侧 CHECK 同一条正则的两个方言写法。"""
    return bool(PUBLICATION_KEY_PATTERN.match(value))


__all__ = [
    "CONTENT_STATES",
    "CONTENT_STATE_INITIAL",
    "CONTENT_STATE_LIST_SQL",
    "CONTENT_STATE_UNVERIFIED",
    "CONTENT_STATE_VERIFIED",
    "DELIVERY_STATES",
    "DELIVERY_STATE_CANCELLED",
    "DELIVERY_STATE_DELIVERED",
    "DELIVERY_STATE_INITIAL",
    "DELIVERY_STATE_LIST_SQL",
    "DELIVERY_STATE_PREPARED",
    "IDEMPOTENCY_SCOPE_DELIVERY_CREATE",
    "IDEMPOTENCY_SCOPE_DELIVERY_REPLACE",
    "IDEMPOTENCY_SCOPE_PUBLICATION_CREATE",
    "LINK_KINDS",
    "MAX_LINK_URL_CHARS",
    "REASON_CROSS_PROJECT_NOTE",
    "REASON_LINK_CANONICAL_TAKEN",
    "REASON_LINK_URL_INVALID",
    "REASON_PUBLICATION_NOT_FOUND",
    "REASON_VERIFY_RESULT_INVALID",
    "LINK_KIND_LIST_SQL",
    "LINK_KIND_LONG",
    "LINK_KIND_OTHER",
    "LINK_KIND_SHORT",
    "MAX_DISPLAY_NAME_CHARS",
    "MAX_IDEMPOTENCY_KEY_CHARS",
    "MAX_PUBLICATION_KEY_CHARS",
    "PUBLICATION_KEY_PATTERN",
    "PUBLICATION_KEY_REGEX_SQL",
    "PUBLICATION_STATES",
    "PUBLICATION_STATE_DRAFT",
    "PUBLICATION_STATE_LIST_SQL",
    "PUBLICATION_STATE_PUBLISHED",
    "PUBLICATION_STATE_UNKNOWN",
    "REASON_ALREADY_REPLACED",
    "REASON_DELIVERY_CANCELLED",
    "REASON_DELIVERY_NOT_FOUND",
    "REASON_DISPLAY_NAME_BLANK",
    "REASON_ELIGIBILITY_STALE",
    "REASON_ELIGIBILITY_UNAVAILABLE",
    "REASON_EXPORT_ITEM_NOT_FOUND",
    "REASON_IDEMPOTENCY_KEY_REUSED",
    "REASON_INVALID_TRANSITION",
    "REASON_KOC_NOT_FOUND",
    "REASON_PROJECT_NOT_FOUND",
    "REASON_PUBLICATION_KEY_INVALID",
    "REASON_PUBLICATION_KEY_TAKEN",
    "REASON_PUBLISHED_AT_SHAPE",
    "SHA256_HEX_CHARS",
    "FrozenExportItem",
    "delivery_create_request_hash",
    "delivery_replace_request_hash",
    "is_valid_publication_key",
    "publication_create_request_hash",
]
