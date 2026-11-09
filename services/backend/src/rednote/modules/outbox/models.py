"""Outbox 事件表（数据结构文档 02 §11）。

**为什么这张表由 WP12 建，而它明明写在 §11（WP13 的章节）**：

协议 03 §4.1 的上传时序图第 8 步写死了三件事**同一事务**完成：

    API->>DB: 固化文件 / 转换配额 / Outbox
    API-->>UI: 202 扫描解析任务

也就是说，"文件固化"与"产生扫描事件"在协议里是一个原子动作。若 WP12
不写 Outbox，就会出现一个窗口：**文件已经算进配额、但没有任何记录说
它需要被扫描**。用户传的文件永远不会被解析，而且**没有任何报错** ——
字面上就是"传上去了，但系统当它不存在"。

这个窗口不能用"WP13 做完再补"来关：补的时候已经产生过历史数据，
哪些文件该扫、哪些不该扫无从分辨。

**为什么只建这一张，不建 `jobs` / `domain_events` / `idempotency_records`**：

界线是**有没有设计空间**：

| 表 | 有没有设计空间 | 结论 |
|---|---|---|
| `outbox_events` | 无。§11 已给出完整列定义，就是标准 Outbox 模式（落库 → 轮询 → 投递 → 标记） | **本包建**，列定义照抄 |
| `jobs` | 有。租约、`fence_token`、重试策略、失败传播都是 WP13 的核心设计 | 不建（越界建必返工） |
| `domain_events` | 有。它牵出 SSE（WP14）的整套语义 | 不建 |
| `idempotency_records` | 有。通用幂等中间件是 WP13/协议层的事 | 不建（本包的幂等由 upload_sessions 状态机承担） |

**"表在谁的章节"不是划界依据，"这张表要不要做设计决策"才是。**
（这条判断法与 WP11 §3 的 V4 否决理由一致。）

**本包只写不读**：`outbox_events` 的消费者是 WP13 的投递器。
本包提供 `list_unpublished_events()` 供它直接接上，不做轮询与重试。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.platform.base import Base, created_at_column, uuid_pk

# 事件 schema 版本。写入方带上它，消费方据此选择解析方式 ——
# 将来 payload 结构演进时，老事件仍能被正确解读。
OUTBOX_SCHEMA_VERSION: Final[int] = 1

# 事件类型。与事件名绑定成常量，避免字符串散落在服务层与测试里各写一遍
# （**写错了不会报错**，只会静默不触发消费 —— 而"没有处理器"是被放行的，见
# `jobs.dispatch.HandlerRegistry` 的说明，所以拼错连日志都不会显眼）。
EVENT_FILE_UPLOADED: Final[str] = "file.uploaded"
EVENT_JOB_ENQUEUED: Final[str] = "job.enqueued"

# 聚合类型（事件的"主体是什么"）。Outbox 的通用字段必须能表达
# "这条事件讲的是哪个对象"，因此用受控取值而不是自由字符串。
AGGREGATE_FILE_OBJECT: Final[str] = "file_object"
AGGREGATE_JOB: Final[str] = "job"


class OutboxEvent(Base):
    """待投递的领域事件。**与业务提交同事务写入**（§11 原文）。

    `attempts` / `next_attempt_at` / `published_at` 三列构成投递状态：
    未发布 = `published_at IS NULL`；重试退避 = `next_attempt_at`。
    本包写入时 `attempts = 0`、两个时间列均为 NULL。
    """

    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = uuid_pk()

    aggregate_type: Mapped[str] = mapped_column(String(50), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)

    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(OUTBOX_SCHEMA_VERSION))
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = created_at_column()
    # 已发布仍保留审计期（§11 原文），因此不删行、只标记。
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            f"schema_version >= {OUTBOX_SCHEMA_VERSION}",
            name="schema_version_positive",
        ),
        # 投递器的取件路径：未发布 + 到点。按 created_at 排序保证
        # "先产生先投递"，避免老事件被新事件无限压在后面。
        # 部分索引把已发布的行（会越来越多）排除在外，索引保持小而热。
        Index(
            "ix_outbox_events_pending",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
        # 按聚合反查（排障："这个文件的事件发出去没有"）。
        Index("ix_outbox_events_aggregate", "aggregate_type", "aggregate_id"),
    )


__all__ = [
    "AGGREGATE_FILE_OBJECT",
    "AGGREGATE_JOB",
    "EVENT_FILE_UPLOADED",
    "EVENT_JOB_ENQUEUED",
    "OUTBOX_SCHEMA_VERSION",
    "OutboxEvent",
]
