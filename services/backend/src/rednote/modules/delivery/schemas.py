"""交付与发布登记的纯函数层（WP36）：入参校验 + 状态机合法迁移表。

本文件**不读库、不写库**，只吃值返回值或抛 `SchemaViolation`。
`service.py` 把 `SchemaViolation.reason` 原样转成 `DeliveryError`，`api.py` 再翻码。

## 状态机为什么只有两条边

`docs/m4/M4契约与幂等口径.md:95` 只给了 `PREPARED → DELIVERED` 与
`PREPARED → CANCELLED`，**没有任何回退边**。于是：

```
PREPARED ──confirm──▶ DELIVERED   （终态）
    │
    └────cancel────▶ CANCELLED    （终态）
```

- **「撤回为 PREPARED」刻意不做**：交付是对外发生过的事实，撤回它等于声称
  "那次交付没发生过"。改稿后要重新给同一位 KOC，走**替换**（追加一行新交付），
  这才是可追溯的表达。
- **已 `DELIVERED` 再 confirm ⇒ 幂等 200，`delivered_at` 不变**（不是 409）：
  重复点击"标记已交付"是用户的正常手滑，返回错误会诱导前端去改状态；
  而真正非法的是 `CANCELLED → DELIVERED` 这种**换了方向**的迁移 ⇒ 409。
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from rednote.modules.delivery.types import (
    DELIVERY_STATE_CANCELLED,
    DELIVERY_STATE_DELIVERED,
    DELIVERY_STATE_PREPARED,
    MAX_DISPLAY_NAME_CHARS,
    MAX_IDEMPOTENCY_KEY_CHARS,
    MAX_PUBLICATION_KEY_CHARS,
    PUBLICATION_STATE_PUBLISHED,
    REASON_DISPLAY_NAME_BLANK,
    REASON_INVALID_TRANSITION,
    REASON_PUBLICATION_KEY_INVALID,
    REASON_PUBLISHED_AT_SHAPE,
    is_valid_publication_key,
)

#: 合法迁移表：`当前状态 → 允许的目标状态集合`。
#: 终态的集合是空的 —— 不是"没写"，是"没有出边"。
DELIVERY_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    DELIVERY_STATE_PREPARED: frozenset(
        {DELIVERY_STATE_DELIVERED, DELIVERY_STATE_CANCELLED}
    ),
    DELIVERY_STATE_DELIVERED: frozenset(),
    DELIVERY_STATE_CANCELLED: frozenset(),
}


class SchemaViolation(Exception):
    """入参不合法。`reason` 是 `types.REASON_*`。"""

    def __init__(self, reason: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details


def validate_display_name(value: str) -> str:
    """KOC 展示名：两端去空白后必须非空、不超长。返回**去空白后**的值。

    为什么在这里 strip 而不是让库的 CHECK 兜着：CHECK 只会拒，不会清洗，
    于是 `"  张三  "` 与 `"张三"` 会成为两条看起来重名的记录。
    """
    cleaned = value.strip()
    if not cleaned:
        raise SchemaViolation(
            REASON_DISPLAY_NAME_BLANK, "KOC 展示名不能为空白。", field="displayName"
        )
    if len(cleaned) > MAX_DISPLAY_NAME_CHARS:
        raise SchemaViolation(
            REASON_DISPLAY_NAME_BLANK,
            f"KOC 展示名不能超过 {MAX_DISPLAY_NAME_CHARS} 个字符。",
            field="displayName",
        )
    return cleaned


def validate_publication_key(value: str) -> str:
    """业务主键格式校验。**不做任何清洗** —— 它是 WP39 导入的对账键，改一个字就对不上。"""
    if not is_valid_publication_key(value):
        raise SchemaViolation(
            REASON_PUBLICATION_KEY_INVALID,
            f"发布业务键只允许字母、数字、下划线、点、连字符，长度 1~{MAX_PUBLICATION_KEY_CHARS}。",
            field="publicationKey",
        )
    return value


def validate_published_shape(state: str, published_at: datetime | None) -> None:
    """`state = PUBLISHED` ⇔ `published_at` 非空（与库侧 CHECK 同一条规则）。

    为什么应用层也判一遍：库的 CHECK 抛的是 `sqlstate 23514`，翻出来是一条
    "数据规则被违反"的笼统 422；在这里判能点名是哪个字段错了。
    """
    published = state == PUBLICATION_STATE_PUBLISHED
    if published and published_at is None:
        raise SchemaViolation(
            REASON_PUBLISHED_AT_SHAPE,
            "state=PUBLISHED 时必须给出 publishedAt。",
            field="publishedAt",
        )
    if not published and published_at is not None:
        raise SchemaViolation(
            REASON_PUBLISHED_AT_SHAPE,
            "只有 state=PUBLISHED 才能带 publishedAt。",
            field="publishedAt",
        )


def validate_idempotency_key(value: str | None) -> None:
    """`Idempotency-Key` 的长度边界。`None`（不传）是合法的 —— 不传就不做幂等。"""
    if value is None:
        return
    if not 1 <= len(value) <= MAX_IDEMPOTENCY_KEY_CHARS:
        raise SchemaViolation(
            "idempotency_key_invalid",
            f"Idempotency-Key 长度须在 1~{MAX_IDEMPOTENCY_KEY_CHARS} 之间。",
            field="Idempotency-Key",
        )


def require_transition(current: str, target: str) -> None:
    """断言 `current → target` 是合法迁移，否则抛 `INVALID_TRANSITION`。

    ⚠️ **"已经在目标状态"不走这里** —— 那是幂等，由 `service` 在调用本函数
    **之前**直接回放。本函数只回答"方向对不对"。
    """
    if target not in DELIVERY_TRANSITIONS.get(current, frozenset()):
        raise SchemaViolation(
            REASON_INVALID_TRANSITION,
            f"交付状态不能从 {current} 迁移到 {target}。",
            fromState=current,
            toState=target,
        )


__all__ = [
    "DELIVERY_TRANSITIONS",
    "SchemaViolation",
    "require_transition",
    "validate_display_name",
    "validate_idempotency_key",
    "validate_publication_key",
    "validate_published_shape",
]
