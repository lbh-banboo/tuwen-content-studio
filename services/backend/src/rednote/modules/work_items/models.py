"""待办/工作项存储模型（WP10）。

按数据结构文档 02 §3 建立 `work_items`：

> work_items | id, project_id?, assignee_id FK, kind, subject_type, subject_id,
> state OPEN/DONE/CANCELLED, due_at?, dedupe_key
> | 同未结待办 dedupe_key 条件 UQ；对象访问需鉴权

---

**为什么 WP10 要建这张表**：
WP10 的验收标准是"并发交接仍一名负责人；**待办和历史正确**"。
后四个字必须有落点 —— 没有 `work_items` 表，"待办转交"就是一句
无法验证的话。需求 §2.3 第 2 条规定的转交语义需要表来表达。

**为什么 kind / subject_type 不加 CHECK 枚举**：
这两个字段的取值域属于内容、事实、交付等后续模块（WP12 起）。
在 WP10 就把取值域钉死等于**用猜测约束未来模块** ——
后续每新增一种待办都要改一次迁移。
这里只对文档 02 已明确的 `state` 三态加 CHECK，其余保持开放。
这与"表结构一次建对"并不矛盾：建对的是**已经定下来的部分**。

**为什么 assignee_id 可空**：
需求 §2.3 第 2 条明确存在"**未认领的**项目待办" ——
未认领在数据上就是 `assignee_id IS NULL`。可空不是妥协，是需求原文的一种状态。

**为什么 subject_id 不加外键**：
待办指向的对象类型是开放的（内容版本、事实条目、交付记录…），
不同类型的对象在不同表里。多态外键在 PG 里无法用单纯 FK 表达，
硬加一个指向某张表的 FK 会让其他类型无法使用。
文档 02 也只写 `subject_type, subject_id` 而没有 FK 约束 —— 遵循原文。
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
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.platform.base import Base, created_at_column, updated_at_column, uuid_pk

# 待办状态三态（文档 02 §3 明确）。
WORK_ITEM_OPEN: Final[str] = "OPEN"
WORK_ITEM_DONE: Final[str] = "DONE"
WORK_ITEM_CANCELLED: Final[str] = "CANCELLED"
WORK_ITEM_STATES: Final[tuple[str, ...]] = (
    WORK_ITEM_OPEN,
    WORK_ITEM_DONE,
    WORK_ITEM_CANCELLED,
)

# 未结 = OPEN。转交、dedupe 唯一性、待办计数都只关心未结的。
OPEN_STATE_FILTER: Final[str] = "state = 'OPEN'"


class WorkItem(Base):
    """待办/工作项。"""

    __tablename__ = "work_items"

    id: Mapped[uuid.UUID] = uuid_pk()

    # 可空：存在不隶属任何项目的待办（例如组织级的账号核对待办）。
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
    )

    # 可空 + RESTRICT：可空表达"未认领"；RESTRICT 保证有未结待办的账号
    # 不会被物理删除（账号停用是改状态，不是删行 —— 需求 §2.4）。
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 待办类型与指向对象。取值域开放，见模块 docstring。
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=WORK_ITEM_OPEN
    )

    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 去重键：同一未结待办只允许一条（文档 02 §3「同未结待办 dedupe_key 条件 UQ」）。
    # 用于"同一对象同一类型只提醒一次"—— 例如同一内容版本只生成一条待审待办。
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint(
            "state IN ('OPEN', 'DONE', 'CANCELLED')", name="state_valid"
        ),
        # 同未结待办 dedupe_key 条件唯一。
        # 用部分唯一索引而非普通唯一约束：已结待办的历史记录可以有相同 dedupe_key
        # （同一对象反复产生待办是正常的），只有"同时未结"才是冲突。
        Index(
            "uq_work_items_open_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=text("state = 'OPEN' AND dedupe_key IS NOT NULL"),
        ),
        Index("ix_work_items_project_id", "project_id"),
        # "我的待办"查询：按负责人 + 状态。部分索引只覆盖未结，
        # 因为待办列表绝大多数查询都带 state=OPEN。
        Index(
            "ix_work_items_open_assignee",
            "assignee_id",
            postgresql_where=text("state = 'OPEN'"),
        ),
        # 同一对象的待办归集（例如列出某内容版本的全部待办）。
        Index("ix_work_items_subject", "subject_type", "subject_id"),
    )


__all__ = [
    "OPEN_STATE_FILTER",
    "WORK_ITEM_CANCELLED",
    "WORK_ITEM_DONE",
    "WORK_ITEM_OPEN",
    "WORK_ITEM_STATES",
    "WorkItem",
]
