"""WP34 导出失效事件表：`export_validity_events`（逐条对应 `docs/m3/WP34-ddl-草案.py`）。

**不可变、追加写**。当前态 = 同一 `export_item_id` 下**最大 `seq`** 那条的 `transition`
（无事件 = VALID）。`seq` 由一致性触发器钉成「从 1 连续、INVALIDATED / RESTORED 严格交替」，
所以最大 `seq` 恒唯一且有意义（这与对可被投影改指的版本链取末版不是一回事）。

`export_items` 零改动：本表只有一条指向它的出向 FK（INSERT 子表时取被引用行的 `FOR KEY SHARE`
行锁，不是 UPDATE，不触发其 `BEFORE UPDATE` 触发器——由 `test_versions_wp34.py::test_t_exp_01_*` 实测证明）。

刻意没有的列：`project_id` / `content_item_id` / 冻结版本 / 冻结指纹（均经 FK 取得，不复制）、
`updated_at` / `lock_version`（不可变表不带）、汇总 `status`（当前态是派生值）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.versions.impact.types import (
    EVENT_REASON_LIST_SQL,
    SCOPE_FROM_VERSION,
    SCOPE_KIND_LIST_SQL,
    SHA256_HEX_CHARS,
    TRANSITION_INVALIDATED,
    TRANSITION_LIST_SQL,
)
from rednote.platform.base import Base, created_at_column, uuid_pk
from rednote.platform.immutability import guard_immutable

IMMUTABLE_TABLES: Final[tuple[str, ...]] = ("export_validity_events",)
OWN_TABLES: Final[tuple[str, ...]] = ("export_validity_events",)


class ExportValidityEvent(Base):
    """某条导出行的资格失效 / 恢复事件。**不可变**。"""

    __tablename__ = "export_validity_events"

    id: Mapped[uuid.UUID] = uuid_pk()

    #: 指向 WP35 `export_items.id`（只读引用）。
    export_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("export_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: 该导出行的事件序号，从 1 连续。
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    transition: Mapped[str] = mapped_column(String(12), nullable=False)
    #: INVALIDATED 时必填；RESTORED 时 NULL（`reason_paired` 双向等价）。
    reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    #: 观测时该内容的当前正文版本。
    observed_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("version_nodes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    observed_eligibility_hash: Mapped[str] = mapped_column(
        String(SHA256_HEX_CHARS), nullable=False
    )
    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 仅 FROM_VERSION：影响图遍历的起点版本。
    source_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("version_nodes.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(f"transition IN ({TRANSITION_LIST_SQL})", name="transition_valid"),
        CheckConstraint(
            f"reason IS NULL OR reason IN ({EVENT_REASON_LIST_SQL})", name="reason_valid"
        ),
        CheckConstraint(
            f"(transition = '{TRANSITION_INVALIDATED}') = (reason IS NOT NULL)",
            name="reason_paired",
        ),
        CheckConstraint(f"scope_kind IN ({SCOPE_KIND_LIST_SQL})", name="scope_kind_valid"),
        CheckConstraint(
            f"(scope_kind = '{SCOPE_FROM_VERSION}') = (source_version_id IS NOT NULL)",
            name="scope_source_paired",
        ),
        CheckConstraint(
            f"char_length(observed_eligibility_hash) = {SHA256_HEX_CHARS}"
            " AND observed_eligibility_hash ~ '^[0-9a-f]+$'",
            name="observed_hash_shape",
        ),
        # 并发追加的唯一防线；同时是 (export_item_id, seq) 的查询索引，不另建降序索引。
        UniqueConstraint(
            "export_item_id", "seq", name="uq_export_validity_events_export_item_id_seq"
        ),
    )


guard_immutable(ExportValidityEvent)

assert {ExportValidityEvent.__tablename__} == set(IMMUTABLE_TABLES)

__all__ = ["IMMUTABLE_TABLES", "OWN_TABLES", "ExportValidityEvent"]
