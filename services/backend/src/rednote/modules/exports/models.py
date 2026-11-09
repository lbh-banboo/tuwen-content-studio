"""固定版本 ZIP 导出的存储模型（WP35）：`export_items` 一张表。

## 形态 A'（老大 2026-10-14 裁定，M4 仍 19 张）

一行 = 一个 `(导出组, 内容, 冻结版本)`；同一次导出的多行共享 `export_group_id`。
**不建组表**。代价是"组级属性"（产物文件、`zip_sha256`、`created_by` ……）在组内每行重复，
一致性由 DB 守，不靠调用方自觉：

| 守卫 | 手段 | 抓什么 |
|---|---|---|
| 行不可变 | `rednote_reject_mutation` 触发器 + `guard_immutable` | 导出记录事后被改（冻结版本失去意义） |
| 组内属性一致 + 组完整 | **延迟约束触发器** `rednote_check_export_group_consistency` | 同组两行 `zip_sha256` 不同 / 行数 ≠ `item_count` / 内容不属本项目 |
| `kind` 与条数同构 | 表级 CHECK `export_kind_matches_count` | `SINGLE` 却有 3 条 |

## 为什么是"先打包、后落记录"，所以表可以不可变

产物字节在落记录**之前**就已写盘并算出 `sha256`，记录一次性 INSERT、此后只读。
因此不需要"PENDING → READY"状态机，也就没有"半成品行"这个失败形态。
异步化（V4）才需要状态机；它被裁定留到上线前（见 WP35 说明 §6）。

## 刻意没有的列

· `delivery_id`：`deliveries` 属 WP36、迁移序晚于本表，FK 无处可指；
  WP36 建表时反向引用 `export_items`（`deliveries.export_item_id`）。
· `updated_at` / `lock_version`：不可变表不带（同 `version_nodes` 的论证）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.exports.types import (
    EXPORT_FORMAT_LIST_SQL,
    EXPORT_KIND_LIST_SQL,
    MAX_CONTENT_ITEMS_PER_EXPORT,
    MAX_IDEMPOTENCY_KEY_CHARS,
    MIN_CONTENT_ITEMS_PER_EXPORT,
    SHA256_HEX_CHARS,
)
from rednote.platform.base import Base, created_at_column, uuid_pk
from rednote.platform.immutability import guard_immutable

IMMUTABLE_TABLES: Final[tuple[str, ...]] = ("export_items",)
OWN_TABLES: Final[tuple[str, ...]] = ("export_items",)


class ExportItem(Base):
    """一次导出里的一条内容（冻结版本）。**不可变**。"""

    __tablename__ = "export_items"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: 同一次导出的多行共享此值。**不是外键**（A' 不建组表）。
    export_group_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )

    content_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_items.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: 冻结的正文版本。`content_versions.id` 本身 FK `version_nodes.id`，二者同值。
    version_node_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("version_nodes.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: 导出时的资格指纹（`types.eligibility_hash`）。逐内容，**不**要求组内一致。
    eligibility_hash: Mapped[str] = mapped_column(
        String(SHA256_HEX_CHARS), nullable=False
    )

    # --- 组级属性（组内每行重复，由延迟触发器守一致） -------------------------
    export_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    export_format: Mapped[str] = mapped_column(String(20), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)

    #: 产物 ZIP 的文件对象（`files.store_generated_asset(purpose=EXPORT)` 的 `file_id`）。
    artifact_file_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("file_objects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    zip_sha256: Mapped[str] = mapped_column(String(SHA256_HEX_CHARS), nullable=False)
    zip_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    idempotency_key: Mapped[str | None] = mapped_column(
        String(MAX_IDEMPOTENCY_KEY_CHARS), nullable=True
    )
    #: 请求摘要。同键再来时与它比对，不等 ⇒ 409（同键异体）。
    request_hash: Mapped[str] = mapped_column(String(SHA256_HEX_CHARS), nullable=False)

    created_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            f"export_kind IN ({EXPORT_KIND_LIST_SQL})", name="export_kind_valid"
        ),
        CheckConstraint(
            f"export_format IN ({EXPORT_FORMAT_LIST_SQL})",
            name="export_format_valid",
        ),
        CheckConstraint(
            f"item_count BETWEEN {MIN_CONTENT_ITEMS_PER_EXPORT}"
            f" AND {MAX_CONTENT_ITEMS_PER_EXPORT}",
            name="item_count_range",
        ),
        # 1 条 ⇔ SINGLE。双向：只判单向会放过"BATCH_ZIP 却只有 1 条"。
        CheckConstraint(
            "(export_kind = 'SINGLE') = (item_count = 1)",
            name="export_kind_matches_count",
        ),
        CheckConstraint("zip_byte_size > 0", name="zip_byte_size_positive"),
        CheckConstraint(
            f"char_length(zip_sha256) = {SHA256_HEX_CHARS}"
            " AND zip_sha256 ~ '^[0-9a-f]+$'",
            name="zip_sha256_shape",
        ),
        CheckConstraint(
            f"char_length(eligibility_hash) = {SHA256_HEX_CHARS}"
            " AND eligibility_hash ~ '^[0-9a-f]+$'",
            name="eligibility_hash_shape",
        ),
        CheckConstraint(
            f"char_length(request_hash) = {SHA256_HEX_CHARS}"
            " AND request_hash ~ '^[0-9a-f]+$'",
            name="request_hash_shape",
        ),
        # 同组内一条内容只出现一次。
        # ⚠️ `uq` 命名约定不含 `constraint_name` 令牌，显式名原样落库 ⇒ 必须写全名。
        UniqueConstraint(
            "export_group_id", "content_item_id", name="uq_export_items_group_content"
        ),
        Index("ix_export_items_group", "export_group_id"),
        Index("ix_export_items_project_created", "project_id", "created_at"),
        # WP34「导出资格失效」的反查入口：哪些导出用过这一版。
        Index("ix_export_items_version", "version_node_id"),
        # 幂等：同项目同键同内容只能有一行；并发的第二个请求撞它后重读。
        Index(
            "uq_export_items_idempotency",
            "project_id",
            "idempotency_key",
            "content_item_id",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )


guard_immutable(ExportItem)

assert {ExportItem.__tablename__} == set(IMMUTABLE_TABLES)

__all__ = ["IMMUTABLE_TABLES", "OWN_TABLES", "ExportItem"]
