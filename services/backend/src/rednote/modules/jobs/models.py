"""任务、执行尝试与通用幂等记录（WP13）。

按数据结构文档 02 §11「任务、外部服务与费用」建立本包所需的 3 张表。
（§11 的其余表分属别的包：`providers` / `model_profiles` / `provider_calls` /
`capability_policies` 是 WP16 的模型网关；`pricing_versions` / `cost_estimates` /
`project_budgets` / `budget_reservations` / `usage_ledger` 是 WP25 的费用；
`domain_events` 属 WP14；`audit_events` / `notification_receipts` 属 WP15；
`runtime_settings` 属 WP24。）

**`outbox_events` 不在这里** —— 它由 WP12 建（协议 §4.1 要求完成上传时同事务写），
本包只是它的消费者（派发器在 `dispatch.py`）。

**为什么这三张表是"任务基础设施"而不是某个业务的功能**：
`jobs` 是"一件要做的事"的**权威状态**（§11 原文："业务状态权威"），
订阅它的有 WP16（外部调用）、WP17（解析）、WP25（费用）、WP32（批产）、WP14（界面进度）。
所以它不能只有状态，还必须承载**执行互斥**（租约）与**写回保护**（fence）——
否则多个 Worker 同时干同一件事时，"谁的结果算数"没有答案。

**关于三张表的可变性**：
  · `jobs` 高度可变（状态迁移 + 租约续期），带 `updated_at`；
  · `job_attempts` 只增（每次尝试一行），**刻意不加 `updated_at`** ——
    它记录的是"这一次尝试发生了什么"，没有"被更新"的语义；
  · `idempotency_records` 可变（从 IN_PROGRESS 到 COMPLETED），带 `updated_at`。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.platform.base import Base, created_at_column, updated_at_column, uuid_pk

# ---------------------------------------------------------------------------
# 任务状态机（数据结构 02 §12 原文）
#
# 「任务：QUEUED → RUNNING → WAITING_PROVIDER/WAITING_RECONCILIATION/
#   SUCCEEDED/PARTIAL/FAILED；取消为 CANCEL_REQUESTED → CANCELLED 或结果核对完成」
# ---------------------------------------------------------------------------

STATE_QUEUED: Final[str] = "QUEUED"
STATE_RUNNING: Final[str] = "RUNNING"
# 等外部供应商返回（WP16 驱动）。**不算完成，也不算失败** ——
# 它占着租约以外的等待时间，因此不因租约过期被抢。
STATE_WAITING_PROVIDER: Final[str] = "WAITING_PROVIDER"
# ★ UNKNOWN 的归宿（数据结构 §13.4）：外部调用状态未知时进入这里。
# **它不因租约过期变成"可自由重试"** —— 原文明写
# "UNKNOWN 不因租约过期变成可自由重试"。恢复路径必须显式"先查询再决定"。
STATE_WAITING_RECONCILIATION: Final[str] = "WAITING_RECONCILIATION"
STATE_SUCCEEDED: Final[str] = "SUCCEEDED"
# 部分成功（批量的部分条目失败）。§12 把它列为终态之一 ——
# 与 FAILED 的区别是"有些做成了"，WP32 的失败项重做依赖这个区分。
STATE_PARTIAL: Final[str] = "PARTIAL"
STATE_FAILED: Final[str] = "FAILED"
STATE_CANCEL_REQUESTED: Final[str] = "CANCEL_REQUESTED"
STATE_CANCELLED: Final[str] = "CANCELLED"

JOB_STATES: Final[frozenset[str]] = frozenset(
    {
        STATE_QUEUED,
        STATE_RUNNING,
        STATE_WAITING_PROVIDER,
        STATE_WAITING_RECONCILIATION,
        STATE_SUCCEEDED,
        STATE_PARTIAL,
        STATE_FAILED,
        STATE_CANCEL_REQUESTED,
        STATE_CANCELLED,
    }
)

# 终态：进入后不再迁移。**这一组是"重复投递不重复执行"的判定依据** ——
# 消费方看到终态就直接认为"这件事已经做完了"，不必也不能重做。
JOB_TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {STATE_SUCCEEDED, STATE_PARTIAL, STATE_FAILED, STATE_CANCELLED}
)

# **可被领取的状态**：只有 QUEUED 能被 Worker 拿走。
# WAITING_PROVIDER / WAITING_RECONCILIATION 刻意不在其中 ——
# 它们表示"外面还有事没落地"，此时重新执行会产生第二次外部调用（可能重复计费）。
JOB_CLAIMABLE_STATES: Final[frozenset[str]] = frozenset({STATE_QUEUED})

# **可取消的状态**：终态不可取消（已经结束了）。
JOB_CANCELLABLE_STATES: Final[frozenset[str]] = JOB_STATES - JOB_TERMINAL_STATES

JOB_STATE_LIST_SQL: Final[str] = ", ".join(f"'{name}'" for name in sorted(JOB_STATES))
JOB_TERMINAL_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(JOB_TERMINAL_STATES)
)

# ---------------------------------------------------------------------------
# 执行尝试状态
# ---------------------------------------------------------------------------

ATTEMPT_RUNNING: Final[str] = "RUNNING"
ATTEMPT_SUCCEEDED: Final[str] = "SUCCEEDED"
ATTEMPT_FAILED: Final[str] = "FAILED"
# 租约过期后被回收：这次尝试的 Worker 已失联，**无法确定它是否仍在执行**。
# 与 FAILED 分开是因为后续处置不同：FAILED 可以直接重试，
# 而 ABANDONED 要先确认外部副作用（§13.4 的"租约不保证供应商已停工"）。
ATTEMPT_ABANDONED: Final[str] = "ABANDONED"

ATTEMPT_STATES: Final[frozenset[str]] = frozenset(
    {ATTEMPT_RUNNING, ATTEMPT_SUCCEEDED, ATTEMPT_FAILED, ATTEMPT_ABANDONED}
)
ATTEMPT_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(ATTEMPT_STATES)
)

# ---------------------------------------------------------------------------
# 幂等记录状态（协议 §1.3）
# ---------------------------------------------------------------------------

IDEMPOTENCY_IN_PROGRESS: Final[str] = "IN_PROGRESS"
IDEMPOTENCY_COMPLETED: Final[str] = "COMPLETED"
IDEMPOTENCY_STATES: Final[frozenset[str]] = frozenset(
    {IDEMPOTENCY_IN_PROGRESS, IDEMPOTENCY_COMPLETED}
)
IDEMPOTENCY_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(IDEMPOTENCY_STATES)
)

# 一般响应缓存的保留期（协议 §1.3："至少保留 7 天"）。
IDEMPOTENCY_RETENTION_DAYS: Final[int] = 7

# ---------------------------------------------------------------------------
# 租约参数（待定项 OP-105：取值理由见下，待压测校准）
# ---------------------------------------------------------------------------

# 租约时长。Worker 领取后在这段时间内独占该任务。
#
# 取 120 秒的理由：它是"崩溃后任务能多快被别人接手"与"长任务被误抢"之间的折中。
# 太短（如 10 秒）→ 一个正常的慢任务会被误判为失联，被抢走后**两人同时执行**；
# 太长（如 1 小时）→ Worker 崩溃后任务要等一小时才恢复。
# 120 秒对"解析一个文件""调一次模型"这类任务是充裕的，而恢复延迟可接受。
# **Worker 必须在到期前续租**（见 `renew_lease`）。
LEASE_DURATION_SECONDS: Final[int] = 120

# 续租间隔建议值：租约的 1/3。这样即使连续两次续租失败，
# 仍有一次机会在租约到期前补上，不会因为一次网络抖动就丢掉任务。
LEASE_RENEW_INTERVAL_SECONDS: Final[int] = LEASE_DURATION_SECONDS // 3

# 一次领取的最大任务数（避免一次事务锁太多行）。
MAX_CLAIM_BATCH: Final[int] = 20

# 派发器一批最多搬多少条事件。
MAX_DISPATCH_BATCH: Final[int] = 100

# 失败重试的退避基数（秒）。第 n 次重试用 base * 2^(n-1)，上限见下。
RETRY_BACKOFF_BASE_SECONDS: Final[int] = 5
RETRY_BACKOFF_MAX_SECONDS: Final[int] = 600


class Job(Base):
    """一件要做的事。**业务状态的权威**（数据结构 02 §11 原文）。

    租约三列（`lease_owner` / `lease_until` / `fence_token`）是**执行互斥**的载体，
    语义见数据结构 §13.4：
    > Worker 获取租约并增加 fence_token；写结果时必须匹配当前 token。

    **`fence_token` 为什么必须是单调整数**：它是"这次执行是不是还被承认"的凭据。
    旧 Worker 在租约过期后被别人接手，此时它的 token 已经落后 ——
    写回时 `WHERE fence_token = :mine` 不成立，`rowcount = 0`，结果被丢弃。
    若 token 不单调（比如用随机值或时间戳），这种"晚到的写"就无法被识别，
    表现为**静默地覆盖掉新结果**。
    """

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = uuid_pk()

    # 归属。都可空：平台级任务（如 health-check）不属于任何项目。
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # `batch_id` 的外键已由 WP32 补上（`batches` 表归 WP32，建于 `modules/batches/**`；
    # 迁移里先 `NOT VALID` 再 `VALIDATE`）。`content_id` 的外键尚未补
    # （`contents` 即 `content_items`，不是 WP32 的前置，登记为待定项）。
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("batches.id", ondelete="RESTRICT"),
        nullable=True,
    )
    content_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    queue: Mapped[str] = mapped_column(String(40), nullable=False)
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    state: Mapped[str] = mapped_column(String(30), nullable=False)

    # 幂等键（协议 §1.3）。`UQ(scope, key)` 是"同一任务不会因为重试而多建一份"的
    # 数据库级保证 —— 服务层的"先查再插"在并发下有窗口。
    idempotency_scope: Mapped[str] = mapped_column(String(120), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    # 请求内容的摘要，用于"同 key 不同内容 → 409"的判定（协议 §1.3）。
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # 输入引用的版本节点（WP11 的 version_nodes）。用数组而不是关联表：
    # 它是"这次任务的输入快照"，只读、不参与查询，做成关联表只会多一次 join。
    input_version_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )

    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- 租约（执行互斥）---
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 单调递增。每次被领取 +1；写回结果必须匹配（见类 docstring）。
    fence_token: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )

    # 结果引用（由具体 kind 解释：文件 id / 报告 id / …）。
    result_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint(f"state IN ({JOB_STATE_LIST_SQL})", name="state_valid"),
        CheckConstraint("fence_token >= 0", name="fence_token_non_negative"),
        # 租约的两个字段必须同时有或同时无 —— 只有 owner 没有 until
        # 意味着"占着但永不到期"，那个任务会永远卡住。
        CheckConstraint(
            "(lease_owner IS NULL) = (lease_until IS NULL)",
            name="lease_pair_consistent",
        ),
        UniqueConstraint(
            "idempotency_scope", "idempotency_key", name="uq_jobs_scope_key"
        ),
        # 领取任务的查询路径：`WHERE state = 'QUEUED' AND next_run_at <= now()`
        # 按 queue 分流（Worker 分组消费各自的队列）。
        Index("ix_jobs_claim", "queue", "state", "next_run_at"),
        Index("ix_jobs_project_id", "project_id"),
        # "这个批次的任务有哪些 / 批次下各状态各多少"（WP32）。部分索引：
        # 绝大多数 job 没有批次。
        Index(
            "ix_jobs_batch_id",
            "batch_id",
            postgresql_where=text("batch_id IS NOT NULL"),
        ),
        # 回收过期租约的扫描路径。
        Index(
            "ix_jobs_lease_until",
            "lease_until",
            postgresql_where=text("lease_until IS NOT NULL"),
        ),
    )


class JobAttempt(Base):
    """一次执行尝试。

    数据结构 §11 的约束原句：「**迟到 attempt 不可覆盖新结果**」——
    这条不靠本表实现，靠 `jobs.fence_token` 的条件写回实现；
    本表的作用是**留下"发生过哪些尝试"的记录**，
    让"任务为什么失败"可以被回答（失败是第几次、哪个 Worker、什么错误码）。
    """

    __tablename__ = "job_attempts"

    id: Mapped[uuid.UUID] = uuid_pk()

    job_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    state: Mapped[str] = mapped_column(String(30), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(120), nullable=False)
    # 这次尝试拿到的 fence。结束后与 `jobs.fence_token` 比对即可知道
    # "我是不是已被取代"。
    fence_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)

    # 费用对账的输入（WP25）：这次尝试是否还有未结的外部用量。
    # 本包只负责在合适时机置位（失败/放弃时置 true），消费属 WP25。
    usage_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    __table_args__ = (
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        CheckConstraint(f"state IN ({ATTEMPT_STATE_LIST_SQL})", name="state_valid"),
        CheckConstraint("fence_token >= 0", name="fence_token_non_negative"),
        UniqueConstraint("job_id", "attempt_no", name="uq_job_attempts_job_attempt"),
        # "这个任务失败了几次"的查询路径。
        Index("ix_job_attempts_job", "job_id", "attempt_no"),
    )


class IdempotencyRecord(Base):
    """通用幂等记录（协议 §1.3）。

    协议原文：
    > key 作用域为用户／服务身份 + HTTP 方法 + 规范化路径。同 key 同 requestHash
    > 返回最初的资源与结果；**同 key 不同内容返回 409**。进行中的同 key 返回同一任务，
    > 不新增任务。
    >
    > 一般响应缓存至少保留 7 天；**生产任务还以数据库业务请求标识永久关联**，
    > 避免缓存过期造成同一任务再次计费。

    最后一句决定了 `expires_at` **必须可空**：
      · 一般请求（如创建字典项）→ 7 天后可清理；
      · 生产任务类请求 → `NULL` = 永久保留。若给它也设 7 天，
        第 8 天用户拿原 key 重试就会**再建一个任务并再计费一次** ——
        而协议明确要求避免这件事。

    主键是 `(scope, key)`。`scope` 已包含身份与路径（协议原文），
    因此不需要额外列。
    """

    __tablename__ = "idempotency_records"

    scope: Mapped[str] = mapped_column(String(120), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)

    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)

    # 首次执行的结果快照。重放时直接回放它，不再执行一次。
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    # NULL = 永久保留（任务关联）。见类 docstring。
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint(f"state IN ({IDEMPOTENCY_STATE_LIST_SQL})", name="state_valid"),
        # 清理路径：只扫有到期时间的（永久保留的不该被扫到）。
        Index(
            "ix_idempotency_expires_at",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL"),
        ),
    )

    @property
    def permanent(self) -> bool:
        """是否永久保留（任务关联的那种）。"""
        return self.expires_at is None


__all__ = [
    "ATTEMPT_ABANDONED",
    "ATTEMPT_FAILED",
    "ATTEMPT_RUNNING",
    "ATTEMPT_STATES",
    "ATTEMPT_STATE_LIST_SQL",
    "ATTEMPT_SUCCEEDED",
    "IDEMPOTENCY_COMPLETED",
    "IDEMPOTENCY_IN_PROGRESS",
    "IDEMPOTENCY_RETENTION_DAYS",
    "IDEMPOTENCY_STATES",
    "IDEMPOTENCY_STATE_LIST_SQL",
    "JOB_CLAIMABLE_STATES",
    "JOB_CANCELLABLE_STATES",
    "JOB_STATES",
    "JOB_STATE_LIST_SQL",
    "JOB_TERMINAL_STATES",
    "JOB_TERMINAL_STATE_LIST_SQL",
    "LEASE_DURATION_SECONDS",
    "LEASE_RENEW_INTERVAL_SECONDS",
    "MAX_CLAIM_BATCH",
    "MAX_DISPATCH_BATCH",
    "RETRY_BACKOFF_BASE_SECONDS",
    "RETRY_BACKOFF_MAX_SECONDS",
    "STATE_CANCELLED",
    "STATE_CANCEL_REQUESTED",
    "STATE_FAILED",
    "STATE_PARTIAL",
    "STATE_QUEUED",
    "STATE_RUNNING",
    "STATE_SUCCEEDED",
    "STATE_WAITING_PROVIDER",
    "STATE_WAITING_RECONCILIATION",
    "IdempotencyRecord",
    "Job",
    "JobAttempt",
]
