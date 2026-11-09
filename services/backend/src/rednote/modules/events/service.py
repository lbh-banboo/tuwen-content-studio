"""事件写入、逐事件鉴权与补漏（WP14）。

**本模块承载 ADR-14 的两条关键约束**：

> 2. 订阅实例收到通知后必须按 id 读库并做权限过滤，逐事件判定该连接
>    所属用户是否有权接收。**不允许把"通知到了"当作"可以推送"。**
> 3. **权限过滤必须逐事件做**，不得只在建立连接时校验一次。

第 3 条的理由（ADR-14 原文）："会话中权限可能变化（成员被移除、项目负责人交接）"。
它把 SSE 与异常矩阵的"成员移除后重新鉴权"绑在一起 —— 也就是说，
**本包必须与 WP09 的权限口径保持一致**，而不是自己另搞一套。

**关于 `Last-Event-ID`**：协议 §1.1 明写它"**不作为授权凭证**"。
因此本模块里它**只用于定位**（`sequence > last`），
"能不能看"一律重新查库判定。这条如果写反了，改一个数字就能读到别的项目的事件。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy.orm import Session

from rednote.modules.events.models import (
    EVENT_TYPES,
    EVENT_TYPE_LIST_SQL,
    DomainEvent,
)
from rednote.modules.events.notifier import NOTIFY_CHANNEL
from rednote.modules.events.repository import EventRepository
from rednote.modules.identity.dependencies import CurrentUser
from rednote.modules.projects.models import ProjectMember
from rednote.platform.errors import ApiError, ErrorCode

# 补漏上限（ADR-14 关键约束 5："补齐有上限"）。
#
# 取 1000 的理由：按"20 在线 / 50 成员"的规模，1000 条约等于断线十几分钟的量。
# 超过它就说明断得太久 —— 此时让前端**全量刷新**反而更快、更不容易出错：
# 用户看到的是一次干净的快照，而不是几千条事件重放（其中大部分是过期的中间态）。
#
# **上限的作用不是"省流量"，而是"给前端一个明确信号"**。
# 无界补发的真正问题是：前端会以为自己拿到了完整历史，
# 于是基于一堆早已过期的中间态渲染出一个错误的界面。
MAX_BACKFILL_EVENTS: Final[int] = 1000

# 新连接建立时的初始补齐条数。用户一进来至少要知道"现在是什么状态"，
# 而不是从此刻起才有事件。
INITIAL_BACKFILL_EVENTS: Final[int] = 50

# 单条事件的 payload 大小上限（字节序列化后）。
# 协议 §11 明写"事件数据不带 Prompt、Brief 全文、外部密钥或其他项目对象"。
# 一个 8 KB 的上限本身就是对"别把正文塞进来"的强制。
MAX_PAYLOAD_BYTES: Final[int] = 8192

# 命中即拒绝写入的敏感键名标记（小写比较，与 platform/errors.py 的
# `sanitize_details` 同一思路，但**处置不同**：那里是脱敏后返回，
# 这里是**拒绝写入**）。
_SENSITIVE_KEY_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "prompt",
        "brief",
        "apikey",
        "api_key",
        "secret",
        "token",
        "password",
        "credential",
        "privatekey",
        "dsn",
    }
)


# ---------------------------------------------------------------------------
# 返回值
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventView:
    """一条事件的不可变快照（要发给前端的那份）。"""

    event_id: uuid.UUID
    sequence: int
    event_type: str
    project_id: uuid.UUID | None
    job_id: uuid.UUID | None
    schema_version: int
    payload: dict[str, Any]
    occurred_at: datetime

    def to_sse_data(self) -> dict[str, Any]:
        """协议 §11 的 `data:` 体。

        **`sequence` 转成字符串**：协议 §1 要求"所有数据库 bigint 对外固定为
        十进制字符串，包括容量、**事件序号**"。它在 `id:` 行里本来就是字符串，
        在 `data.sequence` 里也必须是 —— 否则同一个值在一条消息里出现两种类型。
        """
        return {
            "schemaVersion": self.schema_version,
            "projectId": str(self.project_id) if self.project_id else None,
            "jobId": str(self.job_id) if self.job_id else None,
            "sequence": str(self.sequence),
            "occurredAt": self.occurred_at.isoformat(),
            "payload": self.payload,
        }


@dataclass(frozen=True)
class BackfillResult:
    """一次补漏的结果。"""

    events: tuple[EventView, ...]
    # 待补数量超过上限 → 前端应改为全量刷新（协议 §11 的 410 信号）。
    overflowed: bool
    # 补漏中**被权限过滤掉**的条数（诊断用；不告诉前端）。
    filtered: int


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _validation_error(field: str, message: str, *, reason: str, **extra: Any) -> ApiError:
    details: dict[str, Any] = {
        "fieldErrors": [{"field": field, "type": reason, "message": message}],
        "reason": reason,
    }
    details.update(extra)
    return ApiError(ErrorCode.VALIDATION_FAILED, message, details=details)


def _require_event_type(event_type: str) -> str:
    if event_type not in EVENT_TYPES:
        raise _validation_error(
            "eventType",
            "未知的事件类型。",
            reason="event_type_unknown",
            allowedEventTypes=sorted(EVENT_TYPES),
        )
    return event_type


def _scan_payload_for_sensitive(value: Any, *, path: str = "payload") -> str | None:
    """递归检查 payload 里有没有敏感键名或超长文本。

    返回**命中的路径**（便于产生方定位），没有则返回 None。

    **为什么是"拒绝"而不是"脱敏"**：`platform/errors.py` 的
    `sanitize_details` 是脱敏后返回（那里是错误响应的兜底，宁可打码也别泄露）。
    但事件不一样 —— 它会**落库**并被推送给多个连接。
    脱敏后写入意味着"库里躺着一份打过码的副本"，而且：
      · 产生方不会知道自己传错了（它看到的是 200）；
      · 将来某处直接读库取事件（比如补漏路径忘了走脱敏）就会原样发出。
    在入口拒绝能立刻让产生方发现问题，且库里不留脏数据。
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            normalized = key.lower().replace("_", "")
            if any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS):
                return f"{path}.{key}"
            hit = _scan_payload_for_sensitive(item, path=f"{path}.{key}")
            if hit is not None:
                return hit
        return None
    if isinstance(value, list):
        for index, item in enumerate(value):
            hit = _scan_payload_for_sensitive(item, path=f"{path}[{index}]")
            if hit is not None:
                return hit
        return None
    # 长文本本身可疑：事件只带"界面必要信息"，一段几千字的文本
    # 多半是正文（协议禁止）。阈值取 2048 —— 界面文案不可能这么长。
    if isinstance(value, str) and len(value) > 2048:
        return path
    return None


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def _to_view(event: DomainEvent) -> EventView:
    return EventView(
        event_id=event.id,
        sequence=event.sequence,
        event_type=event.event_type,
        project_id=event.project_id,
        job_id=event.job_id,
        schema_version=event.schema_version,
        payload=dict(event.payload),
        occurred_at=event.occurred_at,
    )


def publish_event(
    repo: EventRepository,
    *,
    event_type: str,
    payload: dict[str, Any] | None = None,
    project_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    aggregate_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
    channel: str = NOTIFY_CHANNEL,
) -> EventView:
    """写入一条事件并**在同一事务**发 `NOTIFY`。

    **为什么要"同事务"**：PG 的 `NOTIFY` 在事务提交时才真正投递，
    所以"事件可见"与"通知发出"是原子的。分成两步（先提交、再发通知）
    会在中间留下崩溃窗口：事件已落库但没人收到通知 ——
    而**通知丢失是 at-most-once 的，不会重试**，只能靠补漏兜底（慢）。
    放同一事务里则连这个窗口都没有。

    **payload 校验**（协议 §11："事件数据不带 Prompt、Brief 全文、外部密钥或其他项目对象"）：
    命中敏感键名或超长文本即拒绝。见 `_scan_payload_for_sensitive` 的说明。

    返回带 `sequence` 的 `EventView` —— 调用方通常把它交给前端或日志。
    """
    normalized_type = _require_event_type(event_type)
    body = payload or {}

    hit = _scan_payload_for_sensitive(body)
    if hit is not None:
        raise _validation_error(
            "payload",
            "事件 payload 含疑似敏感内容或超长文本，事件只应携带界面必要信息。",
            reason="payload_rejected",
            hitPath=hit,
            hint="事件数据不得包含 Prompt、Brief 全文、外部密钥或其他项目对象。",
        )

    event = repo.insert_event(
        event_type=normalized_type,
        payload=body,
        project_id=project_id,
        actor_id=actor_id,
        aggregate_id=aggregate_id,
        job_id=job_id,
        occurred_at=occurred_at,
    )

    size = len(str(body).encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        raise _validation_error(
            "payload",
            f"事件 payload 过大（上限 {MAX_PAYLOAD_BYTES} 字节）。",
            reason="payload_too_large",
            actualBytes=size,
            maxBytes=MAX_PAYLOAD_BYTES,
        )

    # ADR-14 关键约束 1：payload **只带序号**，不带正文。
    repo.notify(channel=channel, payload=str(event.sequence))
    return _to_view(event)


# ---------------------------------------------------------------------------
# 逐事件鉴权（ADR-14 关键约束 2、3）
# ---------------------------------------------------------------------------


def can_view_event(
    session: Session,
    *,
    event: DomainEvent,
    user: CurrentUser,
    member_project_ids: frozenset[uuid.UUID] | None = None,
) -> bool:
    """该用户能否看到这条事件。**必须逐事件调用**（不是建连时判一次）。

    规则（与 WP09/WP11/WP12 的可见性口径一致）：

    | 事件的 `project_id` | 管理者 | 该项目活跃成员 | 其他已登录用户 |
    |---|---|---|---|
    | 非空 | 可见 | 可见 | **不可见** |
    | 为空（平台级） | 可见 | —— | **不可见**（登记 OP-112） |

    **平台级事件为什么不对普通用户开放**：它们是平台配置类事件
    （如全库索引就绪、系统维护）。普通用户既不产生也不消费它们；
    开放只会扩大泄露面。若将来确有需要（例如公告），
    在此加一条规则并补测试 —— 而不是"反正也没人会订阅"。

    `member_project_ids` 是可选的**已算好的成员项目集合**：
    SSE 连接的存活期里会反复调用本函数，每次都查库太浪费。
    但注意它是**调用方在本次批量推送前查的** —— 不是连接建立时查的。
    批量推送的粒度是"一次 poll"，因此权限变化的生效延迟
    最多是"一次通知间隔"，而不是"一整条连接的生命周期"。
    """
    if user.is_admin:
        return True
    if event.project_id is None:
        return False
    if member_project_ids is not None:
        return event.project_id in member_project_ids
    return is_active_member(session, project_id=event.project_id, user_id=user.id)


def is_active_member(
    session: Session, *, project_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """该用户是否是该项目的**当前活跃**成员。

    `removed_at IS NULL` 不能省：被移除的成员不能因为"历史成员记录还在"
    而继续收到项目事件（异常矩阵"成员移除后重新鉴权"直接对应这条）。

    **公开（不带下划线）是因为 API 层要直接用它** —— 越权订阅的判定
    与逐事件过滤的判定必须是**同一个函数**。若 API 层自己写一遍，
    两处判据迟早漂移，而这里漂移的后果是越权（V6/V7 直接失败）。
    """
    from sqlalchemy import select

    row = session.execute(
        select(ProjectMember.id)
        .where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
            ProjectMember.removed_at.is_(None),
        )
        .limit(1)
    ).first()
    return row is not None


def active_member_project_ids(
    session: Session, *, user_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """该用户当前所属的全部活跃项目（一次查库，供批量过滤用）。

    与 WP11 的 `VersionRepository.is_active_member` 同一判据
    （`removed_at IS NULL`），但取的是集合而不是单个布尔 ——
    因为一次批量推送要判很多条事件。
    """
    from sqlalchemy import select

    rows = session.execute(
        select(ProjectMember.project_id).where(
            ProjectMember.user_id == user_id,
            ProjectMember.removed_at.is_(None),
        )
    ).scalars().all()
    return frozenset(rows)


def filter_visible(
    session: Session,
    *,
    events: Sequence[DomainEvent],
    user: CurrentUser,
    member_project_ids: frozenset[uuid.UUID] | None = None,
) -> tuple[EventView, ...]:
    """**逐事件**过滤（ADR-14 关键约束 3 的落地）。

    返回的元组顺序与输入一致（按 sequence 升序），调用方不必再排。
    """
    return tuple(
        _to_view(event)
        for event in events
        if can_view_event(
            session, event=event, user=user, member_project_ids=member_project_ids
        )
    )


# ---------------------------------------------------------------------------
# 补漏
# ---------------------------------------------------------------------------


def backfill_events(
    repo: EventRepository,
    session: Session,
    *,
    user: CurrentUser,
    last_event_id: int | None,
    project_id: uuid.UUID | None = None,
    limit: int = MAX_BACKFILL_EVENTS,
) -> BackfillResult:
    """断线补漏（ADR-14 补漏层）。

    `last_event_id` 是前端带回来的 `Last-Event-ID`，**只用于定位**：

      · 为 None → 新连接，取最近 `INITIAL_BACKFILL_EVENTS` 条（"现在是什么状态"）；
      · 有值 → 取 `sequence > last_event_id` 的事件。

    **超限不静默截断**（ADR-14 关键约束 5）：多取一条判断是否还有更多，
    有则 `overflowed=True` —— 调用方据此返回 **410 `EVENT_HISTORY_EXPIRED`**
    （协议 §1.2：前端"改为全量同步"）。
    静默截断会让前端以为拿到了完整历史，于是基于一堆过期的中间态渲染出错界面。

    **权限过滤在补漏路径上也逐事件做** —— 这一点容易被漏掉：
    补漏看起来是"把之前没收到的东西发回去"，很容易被当成"已经过了权限关"。
    但断线期间权限可能已经变了（用户被移出项目），
    而 `last_event_id` 完全不携带权限信息。
    """
    member_ids = active_member_project_ids(session, user_id=user.id)

    if last_event_id is None:
        rows = repo.list_recent(project_id=project_id, limit=INITIAL_BACKFILL_EVENTS)
        visible = filter_visible(
            session, events=rows, user=user, member_project_ids=member_ids
        )
        return BackfillResult(
            events=visible, overflowed=False, filtered=len(rows) - len(visible)
        )

    if last_event_id < 0:
        raise _validation_error(
            "lastEventId", "Last-Event-ID 不能为负。", reason="last_event_id_negative"
        )

    rows = repo.list_after(
        after_sequence=last_event_id, limit=limit, project_id=project_id
    )
    overflowed = len(rows) > limit
    window = rows[:limit]
    visible = filter_visible(
        session, events=window, user=user, member_project_ids=member_ids
    )
    return BackfillResult(
        events=visible,
        overflowed=overflowed,
        # 只统计"被过滤掉的可见条数"，不把"超限未取"的算进去 ——
        # 两者含义不同（一个是权限，一个是上限），混在一起会误导排障。
        filtered=len(window) - len(visible),
    )


def load_events_by_sequence(
    repo: EventRepository, session: Session, *, sequences: Sequence[int]
) -> tuple[DomainEvent, ...]:
    """按序号读事件（订阅器收到 NOTIFY 后调用）。

    ADR-14 关键约束 1："订阅实例收到通知后必须按 id 读库并做权限过滤"。
    本函数只做前半（读库）—— 权限过滤由调用方用 `filter_visible` 做，
    因为"过滤给谁看"取决于连接所属用户，而本函数不知道用户是谁。
    """
    return tuple(repo.list_by_ids(sequences=sequences))


def merge_watermark(
    *, pushed_sequences: Sequence[int], last_event_id: int | None
) -> int | None:
    """计算新的推送水位线。

    **水位线的作用是去重**（ADR-14 边界要求："补漏过程中新事件已通过 NOTIFY
    推来"会造成重复）。规则很简单：取已推送序号的最大值。

    `last_event_id` 也参与进来：它是**前端确认收到过的**最大序号 ——
    即使本次补漏一条都没补到（比如全被权限过滤了），水位线也不能倒退，
    否则前端下次重连会重复收到已经收到的内容。
    """
    candidates = [value for value in pushed_sequences if value > 0]
    if last_event_id is not None and last_event_id > 0:
        candidates.append(last_event_id)
    return max(candidates) if candidates else None


def should_emit(
    *, sequence: int, watermark: int | None
) -> bool:
    """该序号是否应当推送（严格大于水位线才推）。

    **严格大于而不是大于等于**：水位线本身就是"上一条已推送的序号"。
    用大于等于会把水位线那条重复推一次。
    """
    if watermark is None:
        return True
    return sequence > watermark


__all__ = [
    "INITIAL_BACKFILL_EVENTS",
    "MAX_BACKFILL_EVENTS",
    "MAX_PAYLOAD_BYTES",
    "BackfillResult",
    "EventView",
    "active_member_project_ids",
    "backfill_events",
    "can_view_event",
    "filter_visible",
    "is_active_member",
    "load_events_by_sequence",
    "merge_watermark",
    "publish_event",
    "should_emit",
]