"""WP29 反馈定位与审核决定的存储模型：三张表。

| 表 | 角色 | 可变性 |
|---|---|---|
| `content_feedback_items` | 一条反馈（锚点 + 分类 + 状态）。**可变**（`lock_version` 是 ETag 载体） | 可变 |
| `content_feedback_events` | 状态迁移历史（谁、从哪到哪、留了什么迹象） | **不可变**（只追加） |
| `content_reviews` | 审核决定（通过 / 毙掉），可 `supersedes` 改判 | **不可变** |

## 为什么反馈项可变、事件不可变

反馈项是**工作区**：状态从 `OPEN` 走到 `RESOLVED`/`REJECTED`，锁版本随每次
迁移前进（与 `content_items`、`media_items` 同款乐观锁）。而"这个意见是怎么
被处理的"必须能溯源，所以每次迁移另追加一行 `content_feedback_events`。
历史行一旦写入即不可改 —— 与 `content_versions` / `export_items` 同一条纪律，
DB 层由 `rednote_reject_mutation` 触发器兜底（见 `ddl.py`）。

## 锚点三选一：为什么用列上的 CHECK 而不是三张子表

`anchor_kind` + `paragraph_ref` + `media_item_id` 三列表达"这条意见针对哪里"：

| `anchor_kind` | `paragraph_ref` | `media_item_id` |
|---|---|---|
| `WHOLE` | NULL | NULL |
| `PARAGRAPH` | 非空 | NULL |
| `MEDIA` | NULL | 非空 |

三条件互斥且完备，写成三条 CHECK（每条是"不是这种锚点，或者列占用正确"）。
**`paragraph_ref` 刻意不加 FK**：段落不是表，它是正文切段后的产物
（`p<序号>-<8位哈希>`），只对"提意见时看到的那一版正文"有意义，而那个版本
由 `observed_version_id` 单独记录。

## 跨表归属为什么不在模型层表达

`media_item_id` 是否属于同一条内容、`observed_version_id` /
`resolved_version_id` 是否属于同一条内容，都**不是 FK 能表达的**（FK 只保证
被引用行存在）。它们由 `ddl.py` 的延迟约束触发器在提交时检查；服务层另有一道
可读的 4xx（`repository.media_item_content_id`）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.feedback.types import (
    ACTOR_KIND_LIST_SQL,
    ANCHOR_KIND_LIST_SQL,
    ANCHOR_KIND_MEDIA,
    ANCHOR_KIND_PARAGRAPH,
    ANCHOR_KIND_WHOLE,
    FEEDBACK_CATEGORY_LIST_SQL,
    FEEDBACK_SCOPE_LIST_SQL,
    FEEDBACK_SEVERITY_LIST_SQL,
    FEEDBACK_STATE_INITIAL,
    FEEDBACK_STATE_LIST_SQL,
    FEEDBACK_STATE_REJECTED,
    MAX_FEEDBACK_BODY_CHARS,
    MAX_PARAGRAPH_REF_CHARS,
    MAX_REJECT_REASON_CHARS,
    MAX_SELECTED_TEXT_CHARS,
    REVIEW_DECISION_LIST_SQL,
)
from rednote.platform.base import (
    Base,
    created_at_column,
    lock_version_column,
    updated_at_column,
    uuid_pk,
)
from rednote.platform.immutability import guard_immutable

# 本模块需要不可变保护的表。`ddl.py` 从同一常量派生触发器（名字只有一处来源）。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = (
    "content_feedback_events",
    "content_reviews",
)

# 本模块拥有的全部表（迁移与本模块对账用）。
OWN_TABLES: Final[tuple[str, ...]] = (
    "content_feedback_items",
    *IMMUTABLE_TABLES,
)


class ContentFeedbackItem(Base):
    """一条反馈意见。**可变**（状态机会前进，`lock_version` 是 ETag 载体）。"""

    __tablename__ = "content_feedback_items"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    content_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_items.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: 提意见时看到的是哪一版。**必填** —— "针对哪一版"是反馈定位的前提。
    observed_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )

    anchor_kind: Mapped[str] = mapped_column(String(20), nullable=False)

    #: 段落锚点（`p<序号>-<8位哈希>` 形状）。**不加 FK**：段落不是表。
    #: 长度与 `media/types.MAX_PARAGRAPH_REF_CHARS` 同值（见 types.py）。
    paragraph_ref: Mapped[str | None] = mapped_column(
        String(MAX_PARAGRAPH_REF_CHARS), nullable=True
    )

    #: 用户选中的原文片段（段落锚点时的证据）。整篇锚点通常为空。
    selected_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 媒体锚点。归属校验不在 FK 层，见模块 docstring。
    media_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("media_items.id", ondelete="RESTRICT"),
        nullable=True,
    )

    category: Mapped[str] = mapped_column(String(24), nullable=False)

    #: AI 建议分类（用户可改）。为空表示这条是用户直接选的。
    suggested_category: Mapped[str | None] = mapped_column(
        String(24), nullable=True
    )

    severity: Mapped[str] = mapped_column(String(16), nullable=False)

    #: `CURRENT_ONLY` / `FUTURE_GENERATIONS`（见 types.py）。
    scope: Mapped[str] = mapped_column(String(24), nullable=False)

    state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text(f"'{FEEDBACK_STATE_INITIAL}'"),
    )

    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)

    body: Mapped[str] = mapped_column(Text, nullable=False)

    #: 仅 `REJECTED` 时非空（双向 CHECK 见下）。
    reject_reason: Mapped[str | None] = mapped_column(
        String(MAX_REJECT_REASON_CHARS), nullable=True
    )

    #: 解决这条反馈时产出的版本（"改到哪一版算解决"）。
    resolved_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    lock_version: Mapped[int] = lock_version_column()

    __table_args__ = (
        CheckConstraint(
            f"anchor_kind IN ({ANCHOR_KIND_LIST_SQL})", name="anchor_kind_valid"
        ),
        CheckConstraint(
            f"category IN ({FEEDBACK_CATEGORY_LIST_SQL})", name="category_valid"
        ),
        CheckConstraint(
            "suggested_category IS NULL OR"
            f" suggested_category IN ({FEEDBACK_CATEGORY_LIST_SQL})",
            name="suggested_category_valid",
        ),
        CheckConstraint(
            f"severity IN ({FEEDBACK_SEVERITY_LIST_SQL})", name="severity_valid"
        ),
        CheckConstraint(f"scope IN ({FEEDBACK_SCOPE_LIST_SQL})", name="scope_valid"),
        CheckConstraint(f"state IN ({FEEDBACK_STATE_LIST_SQL})", name="state_valid"),
        CheckConstraint(
            f"author_kind IN ({ACTOR_KIND_LIST_SQL})", name="author_kind_valid"
        ),
        # --- 锚点一致性：列占用与 anchor_kind 必须对得上 --------------------
        # 每条写成"不是这种锚点，或者列占用正确"，三条件合起来互斥且完备。
        CheckConstraint(
            f"(anchor_kind <> '{ANCHOR_KIND_PARAGRAPH}')"
            " OR (paragraph_ref IS NOT NULL AND media_item_id IS NULL)",
            name="anchor_paragraph_shape",
        ),
        CheckConstraint(
            f"(anchor_kind <> '{ANCHOR_KIND_MEDIA}')"
            " OR (media_item_id IS NOT NULL AND paragraph_ref IS NULL)",
            name="anchor_media_shape",
        ),
        CheckConstraint(
            f"(anchor_kind <> '{ANCHOR_KIND_WHOLE}')"
            " OR (paragraph_ref IS NULL AND media_item_id IS NULL)",
            name="anchor_whole_shape",
        ),
        # --- 文本边界 -------------------------------------------------------
        CheckConstraint(
            "selected_text IS NULL OR"
            f" char_length(selected_text) <= {MAX_SELECTED_TEXT_CHARS}",
            name="selected_text_length",
        ),
        CheckConstraint(
            f"char_length(body) > 0 AND char_length(body) <= {MAX_FEEDBACK_BODY_CHARS}",
            name="body_length",
        ),
        # --- 拒绝理由：双向，避免"REJECTED 却没有理由"与"没拒绝却写了理由" ---
        CheckConstraint(
            f"(state = '{FEEDBACK_STATE_REJECTED}') = (reject_reason IS NOT NULL)",
            name="reject_reason_paired",
        ),
        # 列表与工作台的读路径。
        Index(
            "ix_content_feedback_items_content_state",
            "content_item_id",
            "state",
        ),
        Index(
            "ix_content_feedback_items_project_state", "project_id", "state"
        ),
        # 按段落聚合反馈（"这一段被提过哪些意见"）。
        Index(
            "ix_content_feedback_items_content_paragraph",
            "content_item_id",
            "paragraph_ref",
        ),
        Index("ix_content_feedback_items_media", "media_item_id"),
        # "这一版被提过哪些意见"（版本失效/重做的影响面）。
        Index(
            "ix_content_feedback_items_observed_version", "observed_version_id"
        ),
    )


class ContentFeedbackEvent(Base):
    """反馈项的状态迁移历史。**不可变**（只追加）。"""

    __tablename__ = "content_feedback_events"

    id: Mapped[uuid.UUID] = uuid_pk()

    # `name=` 手动给出：按 `platform/base.py` 的命名约定自动生成的全名
    # （`fk_content_feedback_events_feedback_item_id_content_feedback_items`）
    # 长 66 字节，超过 PostgreSQL 63 字节的标识符上限（NAMEDATALEN-1）——
    # 数据库会静默截断并追加哈希，导致 ORM 元数据里的约束名与 `pg_constraint`
    # 里的实际名字永久不一致（`test_schema_parity.py` 的外键延迟性对账
    # 因此找不到这个名字，误判成"模型声明的外键在库里不存在"）。
    # 手动给一个在约定前缀下仍可读、且在 63 字节内的名字来避免这个坑。
    feedback_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "content_feedback_items.id",
            ondelete="RESTRICT",
            name="fk_content_feedback_events_feedback_item_id_feedback_items",
        ),
        nullable=False,
    )

    #: 首行（创建）为 NULL —— 那时还没有"从哪个状态来"。
    from_state: Mapped[str | None] = mapped_column(String(20), nullable=True)

    to_state: Mapped[str] = mapped_column(String(20), nullable=False)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)

    #: 人操作时有值；AI 自己迁移时为空。
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    #: 这次迁移产出的版本（例如"已解决"指向的那一版）。
    # `name=` 同上一条理由：自动生成的全名长 64 字节，超过 63 字节上限。
    resulting_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "content_versions.id",
            ondelete="RESTRICT",
            name="fk_content_feedback_events_resulting_version_id_versions",
        ),
        nullable=True,
    )

    #: 本反馈项内的序号，由仓储层在写事务内取 `max + 1`（见 repository）。
    seq_no: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            f"actor_kind IN ({ACTOR_KIND_LIST_SQL})", name="actor_kind_valid"
        ),
        # 同一反馈项内序号唯一 —— 并发取号撞车时由它兜底（仓储层用 savepoint）。
        # 唯一约束自带索引，不再另建同列普通索引。
        UniqueConstraint(
            "feedback_item_id", "seq_no", name="uq_content_feedback_events_item_seq"
        ),
    )


class ContentReview(Base):
    """一次审核决定（通过 / 毙掉）。**不可变**；改判靠追加一行 + `supersedes`。"""

    __tablename__ = "content_reviews"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    content_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_items.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: 被审核的是哪一版（审核结论永远绑定到具体版本）。
    subject_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )

    decision: Mapped[str] = mapped_column(String(20), nullable=False)

    reason: Mapped[str] = mapped_column(Text, nullable=False)

    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    #: 决定时间。字段名不是 `created_at`，所以不复用 `created_at_column()`
    #: 的签名 —— 但取值口径一致：timestamptz + 服务端 `now()`。
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    #: 自引用：这一决定取代了哪一条旧决定。
    #: **延迟外键** —— 与 `content_items.current_version_id` 同款：
    #: 改判可以在同一事务里先插新行再回填，也可以反过来，两侧都不必排序。
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "content_reviews.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            f"decision IN ({REVIEW_DECISION_LIST_SQL})", name="decision_valid"
        ),
        CheckConstraint("char_length(reason) > 0", name="reason_not_blank"),
        # 审核时间线：项目/内容下的决定倒序读。
        Index("ix_content_reviews_content_decided", "content_item_id", "decided_at"),
        Index("ix_content_reviews_project_decided", "project_id", "decided_at"),
        Index("ix_content_reviews_subject_version", "subject_version_id"),
    )


# 不可变守卫：两张表各自登记（不是 `IMMUTABLE_TABLES` 循环 —— 本模块的
# `content_feedback_items` 恰恰必须可变，循环会把它也锁死）。
guard_immutable(ContentFeedbackEvent)
guard_immutable(ContentReview)

assert {ContentFeedbackEvent.__tablename__, ContentReview.__tablename__} == set(
    IMMUTABLE_TABLES
)

__all__ = [
    "IMMUTABLE_TABLES",
    "OWN_TABLES",
    "ContentFeedbackEvent",
    "ContentFeedbackItem",
    "ContentReview",
]
