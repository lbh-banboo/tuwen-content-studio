"""审计事件与通知已读回执的存储模型（WP15）。

按数据结构文档 02 §11 建立两张表。

**两张表是两种不同的东西，不能合并**：

| 表 | 语义 | 回答的问题 |
|---|---|---|
| `audit_events` | **凭据**（事实，不可篡改） | "谁、在什么时候、对什么、做了什么、为什么" |
| `notification_receipts` | **状态**（送达/已读） | "这条通知发到谁手上了、他看到没有" |

合并的后果：审计要求**追加且不可篡改**（应用账户无 UPDATE/DELETE），
已读回执要求**可更新**（`read_at` 会写）—— 两种约束直接冲突。
数据结构 §11 把它们列成两张表，遵循原文。

---

**关于 `audit_events` 的 `before_hash` / `after_hash`**：

数据结构 §11 只列了列名，没说是"什么的哈希"。本包的做法是：

> **整个对象的规范化摘要**（复用 WP11 的 `canonical_payload_hash`：
> `sort_keys=True` + `separators=(",", ":")` + `default=str` + UTF-8）。

**为什么复用同一个函数而不是另写一套**：
如果审计哈希与版本哈希用**不同**的算法，那么"审计里记的 before_hash
和版本里存的 payload_hash 是不是同一份数据"就**永远没有答案** ——
而"审计说从 X 变到 Y，这个 X 对应哪一版"恰恰是审计最常见的核对场景。
用同一个函数，这个对照天然成立。

**为什么存"整个对象"而不是"只存变了的那部分"**：
"整个对象的哈希"能回答"变没变、从哪个变到哪个"（比对即可），
而"只存变了的那部分"需要**先知道哪里变了** —— 那个判断本身就是错的来源
（尤其是"客户端说只改了 A、实际还改了 B"的场景）。
整对象哈希不依赖"哪里变了"这个判断，它是**无歧义的凭据**。

**为什么不存正文**（这是验收的第一半）：见模块 docstring §「审计是凭据不是日志」。
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

from rednote.platform.base import Base, created_at_column, uuid_pk
from rednote.platform.immutability import guard_immutable

# ---------------------------------------------------------------------------
# actor_kind 的受控取值域
#
# 三种（§2 选型确认）：
#   USER    —— 人（前端操作）
#   SYSTEM  —— 系统（定时任务、Worker、回收器）
#   SERVICE —— 服务账户（内部模块间调用）
#
# 做成受控枚举而不是自由字符串：允许任意字符串会让同一类行为出现
# 三种拼写（"user" / "USER" / "user_action"），审计按 actor_kind 分组时
# 会漏掉其中一部分 —— 而**不会有任何机制报错**。
# ---------------------------------------------------------------------------

ACTOR_KIND_USER: Final[str] = "USER"
ACTOR_KIND_SYSTEM: Final[str] = "SYSTEM"
ACTOR_KIND_SERVICE: Final[str] = "SERVICE"
ACTOR_KINDS: Final[frozenset[str]] = frozenset(
    {ACTOR_KIND_USER, ACTOR_KIND_SYSTEM, ACTOR_KIND_SERVICE}
)
ACTOR_KIND_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(ACTOR_KINDS)
)

# ---------------------------------------------------------------------------
# 审计动作（action）—— **不**做成受控枚举
#
# action 是"做了什么"（如 `project.transfer_owner`、`work_item.resolve`）。
# 它的取值域属于**各业务包**（WP10 的交接、WP12 的上传、WP16 的模型调用…）。
# 在本包把它钉死等于**用猜测约束未来模块** —— 每新增一种动作都要改一次迁移。
# 与 WP10 的 `work_items.kind` 同一判断：建对的是**已经定下来的部分**（actor_kind），
# 开放的是**属于未来的部分**（action、object_type）。
# ---------------------------------------------------------------------------

# `object_type` 的取值域同样开放（由各业务包命名，如 "project" / "work_item" / "brief"）。
# 不加 CHECK：理由同上。


class AuditEvent(Base):
    """一条审计事件。**追加，且不可篡改**。

    **不可篡改的落地**（§11「应用账户无 UPDATE/DELETE 权限」）：
    本机用**触发器**拦 UPDATE/DELETE（复用 WP11 已验证的机制），
    本机对所有账户都拦 —— **比** GRANT 更强（GRANT 只拦非授权账户，root 不拦）。
    GRANT 级账户隔离留内网多账户后做（登记 OP-118）。

    **为什么没有 `updated_at`**：追加表，写完不改。
    加了它反而暗示"会被改" —— 与 WP11 的 `version_nodes` 同一判断。
    """

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = uuid_pk()

    # 行为主体类型（受控取值域）。actor_id 可空：系统产生的事件没有具体操作人。
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 动作与对象。取值域开放（各业务包命名），见模块 docstring。
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    object_type: Mapped[str] = mapped_column(String(50), nullable=False)
    object_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    # 对象所属项目。**可空**：平台级动作（如账号停用、字典维护）不属于任何项目。
    #
    # **为什么必须单列**（§10 要求 `GET /audit-events?projectId=`）：
    # `audit_events` 在数据结构 §11 没有 `project_id` 列，但 §10 的过滤是它。
    # 若靠 `object_id` 反查"这个对象属于哪个项目"，就得在**查询时**对每种
    # object_type 做不同的 join —— 而 object_type 是开放的，意味着每新增一种
    # 对象就要改一次这个查询。单列之后，过滤是**无条件**的 `project_id = :p`。
    # 数据结构没列 → 已登记为 OP-116。
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 对象对应的版本（若该对象有版本注册）。**可空**：不是所有对象都有版本。
    # 它让"想看正文"的人能**自己去查那一版** —— 审计不该自己复现正文。
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    # 规范化摘要（**不是正文**）。复用 WP11 的 `canonical_payload_hash`，
    # 见模块 docstring 对该选择的完整论证。char(64) 与版本注册的
    # `payload_hash` 同型同义，便于"审计哈希 ↔ 版本哈希"的对照。
    before_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 原因。它可以是**正文**（运营写的理由），所以不存哈希 ——
    # 它就是给人看的。但**不含正文**指的是业务对象的正文，不是这句话。
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # 请求追踪 id（与 platform/errors.py 的 `REQUEST_ID_HEADER` 对应）。
    # 它把"这次审计"与"这次 HTTP 请求的日志"关联起来 —— 排障时能顺着
    # requestId 找到完整请求上下文。**不存请求体**，只有 id。
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            f"actor_kind IN ({ACTOR_KIND_LIST_SQL})", name="actor_kind_valid"
        ),
        # before_hash / after_hash 必须**同时有或同时无**。
        # 只记一边没有意义 —— "从哪改到哪"是一对的。
        CheckConstraint(
            "(before_hash IS NULL) = (after_hash IS NULL)",
            name="hash_pair_consistent",
        ),
        CheckConstraint(
            "char_length(before_hash) = 64 OR before_hash IS NULL",
            name="before_hash_length",
        ),
        CheckConstraint(
            "char_length(after_hash) = 64 OR after_hash IS NULL",
            name="after_hash_length",
        ),
        # §10 的主过滤路径：按项目 + 时间。
        Index("ix_audit_events_project_created", "project_id", "created_at"),
        # "这个对象的所有变更"路径。
        Index("ix_audit_events_object", "object_type", "object_id"),
        # "这个人做了什么"路径（排障常见）。
        Index("ix_audit_events_actor", "actor_kind", "actor_id", "created_at"),
        # "这个请求做了什么"路径（顺 requestId 排障）。
        Index("ix_audit_events_request_id", "request_id"),
    )


class NotificationReceipt(Base):
    """一条通知的送达/已读回执。

    数据结构 §11 的约束原句：**「通知已读不代表业务已完成」**。
    这句话必须被显式守住 —— `read_at` 只表达"这个用户看过这条通知了"，
    它**绝不**意味着"这条通知对应的业务动作已经做完"。
    混淆两者的后果是：前端把"小红点消失"当成"任务完成"，
    用户以为事情做完了，实际上只是他看了一眼。

    **`read_at` 与 `dismissed_at` 分开**（§11 原文）：
      · `read_at` —— 用户**看到**了（点开/扫过）；
      · `dismissed_at` —— 用户**主动忽略**（不再提醒）。
    合并成一列会让"我没看到"与"我不想再看到"无法区分 ——
    前者不该影响任何业务，后者才是"别再提醒我"的信号。

    **本表无 HTTP 端点**（协议 §10 没有 receipts 端点）：
    它是内部设施 —— 已读标记的语义（是"送达"还是"已读"）需要协议明确（OP-117）。
    本包只建表 + 提供内部服务（`mark_delivered` / `mark_read` / `list_unread`）。
    """

    __tablename__ = "notification_receipts"

    # 复合主键：每个用户对每个事件只有一条回执。
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("domain_events.id", ondelete="CASCADE"),
        primary_key=True,
    )

    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dismissed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        # "这个人的未读通知"路径（小红点）。部分索引只覆盖未读 —
        # 已读的历史不回响，只占存储，不该进索引。
        Index(
            "ix_notification_receipts_unread",
            "user_id",
            "created_at",
            postgresql_where=text("read_at IS NULL AND dismissed_at IS NULL"),
        ),
        # "这个事件发给了谁"路径（送达核对）。
        Index("ix_notification_receipts_event", "event_id"),
    )


# 注册不可变守卫（**ORM 层**）。
#
# **这一行与 `ddl.py` 里的触发器是两层保护，缺一不可**（WP11 的核心设计）：
#
# | 层 | 管住什么 | 给出什么错误 |
# |---|---|---|
# | ORM 事件（本行） | 应用的正常写入路径 | **可读的领域错误**（`ImmutableRecordError`） |
# | 数据库触发器（`ddl.py`） | 所有写入（手工 SQL / 数据回填 / 运维脚本） | SQLSTATE `23001` |
#
# **为什么不能只有触发器**：触发器抛的是 `psycopg.errors.RestrictViolation`，
# 对开发者来说只是一条数据库错误 —— 而 `ImmutableRecordError` 的文案会
# 直接说明"审计是不可变记录"，让人一眼知道**是写错了对象**而不是"数据库抽风"。
#
# **为什么不能只有 ORM 事件**：它拦不住绕过 ORM 的写入
# （`psql` 手工 UPDATE、Alembic 数据回填、运维脚本 —— 这些路径**没有代码评审**）。
#
# **本包实测踩到过**：只建了触发器、漏了这一行，于是 ORM 路径抛的是
# `RestrictViolation` 而不是领域错误（见 ISS-42）。两层的**分工**必须都对上，
# 只做一层时"另一层"的存在感为零 —— 直到有人依赖它的错误类型。
guard_immutable(AuditEvent)


__all__ = [
    "ACTOR_KINDS",
    "ACTOR_KIND_LIST_SQL",
    "ACTOR_KIND_SERVICE",
    "ACTOR_KIND_SYSTEM",
    "ACTOR_KIND_USER",
    "AuditEvent",
    "NotificationReceipt",
]