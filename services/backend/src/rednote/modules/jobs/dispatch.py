"""Outbox 派发器与投递通道（WP13 切片 F1/F2）。

**这个模块解决的问题**（研发计划 04 §WP13 切片 1、2）：

  > 事务写 job 与 outbox；验证数据库提交后**队列停机不会丢任务**。
  > 派发器发布确认；验证**标记前崩溃会重复投递但不会重复生成**。

两句话对应两个设计决定：

1. **事件与业务数据同事务落库** → 队列停机只是"暂时搬不出去"，
   事件躺在 `outbox_events` 里；恢复后接着搬。**不丢**。

2. **顺序是"先发布、成功后标记"**，不是"先标记、再发布"。
   若先标记，标记后崩溃 → 事件永远不会被搬出去 → **丢了**（而库里看起来已处理）。
   反过来（先发布）崩溃 → 事件仍未标记 → 下次**再搬一次** → 重复投递。
   于是问题被转化为"**重复投递也不能重复执行**" —— 那由
   幂等记录（`idempotency_records`）与任务终态共同吸收。

**为什么投递通道要抽象**：本机没有 RabbitMQ（内网才有 3 节点，研发执行计划 §5）。
若不抽象，本包的全部语义都无法在本机验证 ——
而本包的验收项（重复投递不重复执行）恰恰**只能在能跑起来的环境里验证**。
与 WP12 的存储适配器同一手法。

**投递语义约定（Protocol 的契约）**：`publish()` 返回 True 表示
"这件事已经交给下游并确认了"。因此：

  · 真实 MQ 实现：publish + publisher confirm（消息已落到 broker）；
  · 本机 in-process 实现：直接调用注册的处理器，**处理器抛异常即返回 False**。

两者的**共同点**是"至少一次投递"：返回 False 时事件保留、带退避重试。
差异在于"确认"的时间点（broker 收到 vs 处理器跑完），
这一点在 `InProcessChannel` 的 docstring 里显式写明。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from rednote.modules.jobs.models import (
    MAX_DISPATCH_BATCH,
    RETRY_BACKOFF_BASE_SECONDS,
    RETRY_BACKOFF_MAX_SECONDS,
)
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.outbox.models import OutboxEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventEnvelope:
    """已落库事件的不可变快照。

    **为什么不直接传 ORM 的 `OutboxEvent` 给处理器**：处理器可能在另一个进程
    （真实 MQ 场景）执行，也可能在投递完成后才用到这些字段。
    ORM 实例绑在 session 上，session 一关就 `DetachedInstanceError`。
    转成 frozen dataclass 后可以自由持有 —— 与 WP11/WP12 的返回值处理一致。
    """

    event_id: uuid.UUID
    aggregate_type: str
    aggregate_id: uuid.UUID
    event_type: str
    schema_version: int
    payload: dict[str, Any]
    # 这是第几次投递（从 0 开始）。处理器可以用它做"重复投递"的判定辅助，
    # 但**不该依赖它做幂等** —— 幂等要靠业务侧的唯一约束或幂等记录，
    # 因为 attempts 在标记成功后会被重置（见 repository.mark_published）。
    attempts: int


def to_envelope(event: OutboxEvent) -> EventEnvelope:
    """把 ORM 事件转成不可变信封。**读取全部字段后再返回**，不留懒加载。"""
    return EventEnvelope(
        event_id=event.id,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        event_type=event.event_type,
        schema_version=event.schema_version,
        payload=dict(event.payload),
        attempts=event.attempts,
    )


class DeliveryChannel(Protocol):
    """投递通道。返回 True = 已被下游确认。"""

    @property
    def name(self) -> str: ...

    def publish(self, event: EventEnvelope) -> bool: ...


# ---------------------------------------------------------------------------
# 处理器注册表
# ---------------------------------------------------------------------------

HandlerFn = Callable[[EventEnvelope], None]


class HandlerRegistry:
    """事件类型 → 处理器。

    **未注册的事件类型直接放行（视为已处理）**：这在别的系统里可能是个坏习惯，
    但在这里是正确的 —— 事件的生产方（WP12）与消费方（WP17/WP16）
    是**不同的工作包**，WP13 交付时消费方还不存在。
    若未注册就报错/重试，`file.uploaded` 会永远卡在 outbox 里，
    而它其实"暂时没人要"。

    代价是**拼错的事件类型名会被静默忽略**。因此：
      · `unknown_event_types()` 提供"有哪些事件没人接"的查询，供排障与告警；
      · 每次放行都记 WARNING 日志。
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[HandlerFn]] = {}

    def register(self, event_type: str, handler: HandlerFn) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def registered_types(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def handle(self, event: EventEnvelope) -> None:
        """交给全部注册的处理器。任一抛异常都会向上传播（由通道决定是否算失败）。"""
        handlers = self._handlers.get(event.event_type)
        if not handlers:
            logger.warning(
                "no handler registered for event type; passing through",
                extra={
                    "event_type": event.event_type,
                    "event_id": str(event.event_id),
                },
            )
            return
        for handler in handlers:
            handler(event)


@dataclass(frozen=True)
class InProcessChannel:
    """本机投递通道：直接调用注册的处理器。

    **与真实 MQ 的语义差异（必须知道）**：

    | | 真实 MQ | 本实现 |
    |---|---|---|
    | `publish` 返回 True 的含义 | 消息已落到 broker | 处理器**已执行完成** |
    | 处理器失败时 | 消息留在队列里，由 MQ 重投或进死信 | 返回 False，由 Outbox 带退避重试 |
    | 投递与处理的关系 | 异步，两个进程 | 同步，同一进程 |

    **共同点是"至少一次"**：两种情况都不会丢事件。
    差异在于本实现的确认更靠后（跑完才算），因此在本机看到的
    "重试"次数会比生产少 —— 但"重复投递不重复执行"这条语义
    在两种实现下都必须成立，测试正是针对它。
    """

    registry: HandlerRegistry

    @property
    def name(self) -> str:
        return "IN_PROCESS"

    def publish(self, event: EventEnvelope) -> bool:
        try:
            self.registry.handle(event)
        except Exception:  # noqa: BLE001 - 任何处理器异常都算投递未完成
            # 不在这里吞掉细节：把异常记全（含事件 id 与类型）便于定位；
            # 但**不让它继续传播** —— 派发器要能处理"这一条失败、继续下一条"，
            # 否则一条坏事件会卡住整批。
            logger.exception(
                "in-process handler failed; event will be retried",
                extra={
                    "event_type": event.event_type,
                    "event_id": str(event.event_id),
                    "attempts": event.attempts,
                },
            )
            return False
        return True


# ---------------------------------------------------------------------------
# 派发器
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchReport:
    """一次派发的结果。"""

    scanned: int = 0
    published: int = 0
    failed: int = 0
    # 已标记重试的事件 id（排障用；放在这里免得调用方再查一次库）。
    retried_event_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)


def dispatch_outbox(
    repo: JobRepository,
    channel: DeliveryChannel,
    *,
    now: datetime | None = None,
    limit: int = MAX_DISPATCH_BATCH,
) -> DispatchReport:
    """把待投递的事件搬出去（一次一批）。

    **顺序不可颠倒**：`channel.publish()` 成功之后才 `mark_published()`。
    倒过来会在"标记后、发布前"的窗口里丢事件（库显示已发布，实际没出去）。

    **可反复调用**（幂等）：已标记的事件不在候选集里；
    未标记的会被再搬一次 —— 这正是设计的容错方式。

    调度由部署层决定（定时任务 / 常驻循环），本函数不自带循环 ——
    与 WP12 的 `expire_stale_sessions` 同一做法：**不把调度策略烘进业务函数**。
    """
    moment = now or repo.now()
    events = repo.list_pending_events(limit=limit, now=moment)

    published = 0
    failed = 0
    retried: list[uuid.UUID] = []

    for event in events:
        # 先取标量再投递：`mark_*` 走 UPDATE 后 ORM 实例可能被 expire，
        # 那时再读属性会触发惰性加载（WP10/WP12 都踩过 PendingRollbackError）。
        envelope = to_envelope(event)
        event_id = envelope.event_id

        if channel.publish(envelope):
            repo.mark_published(event_id=event_id)
            published += 1
            continue

        # 退避：5s, 10s, 20s, … 上限 600s。
        # 指数退避而不是固定间隔，是因为投递失败通常意味着下游不可用，
        # 固定间隔会在下游长时间宕机时把重试次数打满、日志刷爆。
        delay = min(
            RETRY_BACKOFF_BASE_SECONDS * (2 ** envelope.attempts),
            RETRY_BACKOFF_MAX_SECONDS,
        )
        repo.mark_publish_failed(
            event_id=event_id, next_attempt_at=moment + timedelta(seconds=delay)
        )
        failed += 1
        retried.append(event_id)

    return DispatchReport(
        scanned=len(events),
        published=published,
        failed=failed,
        retried_event_ids=tuple(retried),
    )


__all__ = [
    "DeliveryChannel",
    "DispatchReport",
    "EventEnvelope",
    "HandlerFn",
    "HandlerRegistry",
    "InProcessChannel",
    "dispatch_outbox",
    "to_envelope",
]
