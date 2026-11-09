"""SSE 任务事件端点（协议 03 §11，1 个端点）。

```
端点：GET /api/v1/events?projectId=...
使用同域 Cookie 会话，Content-Type: text/event-stream、Cache-Control: no-cache，
反向代理关闭事件流缓冲。每个浏览器页签尽量复用一条连接。
```

**关于 `Last-Event-ID` 的读法**（容易写错的一处）：
浏览器 `EventSource` 在自动重连时把它放在**请求头**里；
而有些客户端会放在**查询参数**里（因为无法自定义头）。
两者都要支持，但**都不能当作授权凭证**（协议 §1.1 明写）。
本端点读两处，然后只把它当"位置"用。

**为什么这个端点不能用 `response_model`**：SSE 的响应是一个**无限流**，
不是一次性的 JSON 文档。FastAPI 的 `response_model` 会试图把返回值
序列化成固定结构 —— 那对事件流没有意义。因此本端点返回 `StreamingResponse`，
并在 `responses` 里声明 200 的 content type 供契约使用。

**关于错误响应**：SSE 建连阶段的失败（未登录、越权、410 超限）
必须是**普通 JSON 错误响应**（协议 §1.2 的形状），而不是事件流里的一条事件 ——
因为此时前端还没有进入"解析事件流"的状态。
410 是个例外：它可以作为流正常结束，但用 JSON 响应更明确，
且 `EventSource` 会自动重连（前端据此改为全量刷新）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from rednote.modules.events import service
from rednote.modules.events.models import EVENT_SYNC_REQUIRED
from rednote.modules.events.notifier import (
    EventNotifier,
    NotificationBatch,
    SubscriberRegistry,
)
from rednote.modules.events.repository import EventRepository
from rednote.modules.identity.dependencies import CurrentUser, CurrentUserDep, DBSession
from rednote.platform.api_responses import ACCESS_ERROR_RESPONSES, UNIVERSAL_ERROR_RESPONSES
from rednote.platform.errors import (
    ApiError,
    ErrorCode,
    ErrorEnvelopeForOpenApi,
    error_response,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

# SSE 心跳间隔。协议 §11 原文："默认 15 秒心跳，网络重连退避；
# 代理超时大于心跳间隔。"
#
# 15 秒这个数字不是随手取的：它必须**明显小于**反向代理的空闲超时
# （常见默认 60 秒），否则代理会先掐断连接，前端表现为"连接不稳定"。
# 心跳同时也是前端判断"连接还活着"的依据 —— 进度条停住时，
# 有心跳说明"任务没动"，没心跳说明"连接断了"。两者对用户的含义完全不同。
HEARTBEAT_SECONDS: Final[float] = 15.0

# `Last-Event-ID` 的两个来源。
LAST_EVENT_ID_HEADER: Final[str] = "Last-Event-ID"

# 收到一条通知后最多等多久去凑下一批（毫秒级）。
# 不设等待会变成"一条通知读一次库"，通知风暴时读放大明显。
_BATCH_COALESCE_SECONDS: Final[float] = 0.05


def _sse_message(*, event_id: int, event_type: str, data: dict[str, Any]) -> bytes:
    """按 SSE 规范拼一条消息。

    格式要求（很重要，写错前端就解析不了）：

        id: 18492\\n
        event: job.progress\\n
        data: {...}\\n
        \\n            ← 空行表示消息结束

    **三条不能省的细节**：
      · `data` 里若含换行必须逐行前缀 `data:`（这里用 `json.dumps` 的
        紧凑形式避免换行）；
      · 消息**必须以空行结束**，否则浏览器会一直等下一行；
      · `id` 与 `event` 在 `data` **之前**（顺序虽不强制，但保持与协议示例一致）。

    `ensure_ascii=False` 让中文按 UTF-8 原样发出（SSE 规范要求 UTF-8）；
    用 `separators` 去掉空格，减少无效字节。
    """
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (
        f"id: {event_id}\n"
        f"event: {event_type}\n"
        f"data: {body}\n"
        f"\n"
    ).encode("utf-8")


def _comment(text: str) -> bytes:
    """SSE 注释行。用作心跳。

    **为什么用心跳用注释而不是自定义事件**：注释行（以 `:` 开头）
    被规范定义为"接收方应忽略"，因此前端**不需要**为心跳写任何处理逻辑，
    但它仍能让 TCP 连接与代理知道"连接是活的"。
    若用心跳事件，前端就得给自己发的东西写一个忽略分支 —— 那是自找的复杂度。
    """
    return f": {text}\n\n".encode("utf-8")


def _read_last_event_id(request: Request) -> int | None:
    """读取 `Last-Event-ID`。头优先，其次查询参数。

    读两处是必要的：浏览器 `EventSource` 自动重连时放在**头**里；
    而一些客户端（含手工构造的重连）放在**查询参数**里。
    只支持一处会让某一类客户端的补漏完全失效 —— 表现为"重连后丢事件"，
    而那正是本包的核心验收项。

    **解析失败一律返回 None**（当作新连接），不报错：
    它是定位信息、且前端可能传来任意字符串。为一个格式错误的
    "位置标记"拒绝整条 SSE 连接，会让一个可用性问题变成不可用。
    """
    raw = request.headers.get(LAST_EVENT_ID_HEADER)
    if raw is None:
        raw = request.query_params.get("lastEventId")
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def get_notifier(request: Request) -> EventNotifier | None:
    """取本实例的订阅器（在 app 装配时挂到 `app.state`）。

    **为什么从 `app.state` 取而不是全局单例**：与 `get_db_session` 同一理由 ——
    多实例部署下，"本进程的订阅器"是一个**进程级资源**。
    全局单例在测试里会跨测试复用（上一个测试的连接还开着），
    而 `app.state` 跟着 app 实例走，测试天然隔离。
    """
    return getattr(request.app.state, "event_notifier", None)


def get_registry(request: Request) -> SubscriberRegistry | None:
    return getattr(request.app.state, "event_registry", None)


@router.get(
    "/events",
    # SSE 的响应是无限流，无法用 response_model 描述。这里显式声明
    # content type 与错误响应，让契约里 200 的形状是"事件流"而不是 JSON。
    responses={
        status.HTTP_200_OK: {
            "description": "SSE 事件流（text/event-stream）。",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        **UNIVERSAL_ERROR_RESPONSES,
        **ACCESS_ERROR_RESPONSES,
        # 410 用协议错误体（与其它 4xx 同一形状）。**不能写 `"model": None`**
        # ——FastAPI 会把它当成"这个响应没有 body"，契约里就少了 410 的结构，
        # 前端无法据此生成类型。
        status.HTTP_410_GONE: {
            "model": ErrorEnvelopeForOpenApi,
            "description": (
                "事件历史超出补漏上限（EVENT_HISTORY_EXPIRED）："
                "前端应关闭流、重新查询快照后再连接。"
            ),
        },
    },
    summary="SSE 任务事件流（含断线补漏）",
)
async def stream_events(
    request: Request,
    user: CurrentUserDep,
    db: DBSession,
    project_id: Annotated[uuid.UUID | None, Query(alias="projectId")] = None,
) -> StreamingResponse:
    """订阅事件流。

    **建连阶段的顺序（不可调换）**：

      1. 鉴权（`CurrentUserDep` 已经做了 —— 未登录在依赖阶段就 401）；
      2. **补漏**（按 `Last-Event-ID` 取缺失事件，逐事件过滤）；
      3. 补漏超限 → **410**（前端改为全量刷新）；
      4. 注册到订阅表；
      5. 进入"补漏 → 实时"的循环。

    **为什么"先补漏再转实时"**（ADR-14 边界要求）：补漏过程中若有新事件
    通过 NOTIFY 推来，两条路径会**交叉**。先补漏 + 用序号水位线去重，
    就能保证"不重不漏"。反过来的话（先转实时再补漏）会出现
    "新事件先到、旧事件后到"的乱序，前端渲染出的进度会先跳后回退。
    """
    if project_id is not None and not _may_access_project(db, user=user, project_id=project_id):
        # 越权订阅必须**被拒**（ADR-14 V6）。用 404 而不是 403 ——
        # 防"这个项目存在吗"的枚举（延续 WP09/WP11/WP12 的口径）。
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "项目不存在或无权访问。",
            details={"projectId": str(project_id)},
        )

    repo = EventRepository(db)
    last_event_id = _read_last_event_id(request)
    backfill = service.backfill_events(
        repo,
        db,
        user=user,
        last_event_id=last_event_id,
        project_id=project_id,
    )

    if backfill.overflowed:
        # 协议 §1.2：410 → 前端"改为全量同步"。
        # **不静默截断**：前端必须知道"你拿到的不完整"，
        # 否则会基于一堆过期中间态渲染出错误界面。
        return error_response(
            ErrorCode.EVENT_HISTORY_EXPIRED,
            "断线时间过长，请重新拉取当前状态。",
            details={
                "reason": "backfill_limit_exceeded",
                "lastEventId": str(last_event_id) if last_event_id is not None else None,
                "limit": str(service.MAX_BACKFILL_EVENTS),
                "hint": "请关闭事件流、重新查询快照，再建立连接。",
            },
        )

    notifier = get_notifier(request)
    registry = get_registry(request)

    # 本连接的唤醒队列。
    #
    # **为什么用队列而不是直接在通知回调里写响应**：通知到达发生在
    # 订阅器自己的线程/调用栈里，而写响应必须发生在事件循环里。
    # 队列是两者之间唯一的线程安全通道（`asyncio.Queue` 的
    # `put_nowait` 可从任意线程调用 —— 它内部做了线程安全处理）。
    queue: asyncio.Queue[NotificationBatch | None] = asyncio.Queue()

    def _on_notify(batch: NotificationBatch) -> None:
        try:
            queue.put_nowait(batch)
        except Exception:  # noqa: BLE001 - 队列满等情况不能影响订阅器
            logger.debug("dropping notification batch for slow subscriber")

    if notifier is not None:
        notifier.subscribe(_on_notify)
    if registry is not None:
        registry.add(project_id=project_id, callback=_on_notify)

    # 初始水位线与待发内容。
    watermark = service.merge_watermark(
        pushed_sequences=[view.sequence for view in backfill.events],
        last_event_id=last_event_id,
    )
    pending = list(backfill.events)

    async def _stream() -> AsyncIterator[bytes]:
        try:
            # SSE 建连后**立刻发一个注释行**：让前端与反向代理
            # 确认"连接真的建立了"，而不是等到 15 秒后的第一个心跳。
            # 没有它，某些代理会把"迟迟没有响应体"的连接判为坏连接。
            yield _comment("stream opened")

            for view in pending:
                yield _sse_message(
                    event_id=view.sequence,
                    event_type=view.event_type,
                    data=view.to_sse_data(),
                )

            while True:
                # 每轮循环都重查权限 —— **这是"逐事件鉴权"的落地方式**。
                # 不能只在建连时判一次（ADR-14 关键约束 3）。
                # 重查的粒度是"一次通知"或"一次心跳"，因此权限变化的
                # 生效延迟最多是 15 秒，而不是整条连接的生命周期。
                member_ids = service.active_member_project_ids(db, user_id=user.id)

                try:
                    batch = await asyncio.wait_for(
                        queue.get(), timeout=HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    # 心跳。同时顺带做一次补漏 —— 这正好覆盖
                    # **"NOTIFY 全失效"** 的场景：即使一条通知都没收到，
                    # 靠心跳周期的补漏仍能把新事件发出去（只是慢 15 秒）。
                    # ADR-14 说 NOTIFY 是 at-most-once，这条路径就是那个兜底。
                    missed = await _poll_missed(
                        db, user=user, watermark=watermark, project_id=project_id,
                        member_ids=member_ids,
                    )
                    if missed is None:
                        yield _comment("heartbeat")
                        continue
                    for view in missed:
                        watermark = view.sequence
                        yield _sse_message(
                            event_id=view.sequence,
                            event_type=view.event_type,
                            data=view.to_sse_data(),
                        )
                    continue

                if batch is None:  # 关闭信号
                    break

                events = service.load_events_by_sequence(
                    repo, db, sequences=batch.sequences
                )
                visible = service.filter_visible(
                    db, events=events, user=user, member_project_ids=member_ids
                )
                for view in visible:
                    # **按水位线去重**（ADR-14 边界要求）。通知可能与
                    # 心跳补漏重叠（同一条事件既被 NOTIFY 带来、
                    # 又落进了补漏窗口），严格大于水位线才发。
                    if not service.should_emit(sequence=view.sequence, watermark=watermark):
                        continue
                    watermark = view.sequence
                    yield _sse_message(
                        event_id=view.sequence,
                        event_type=view.event_type,
                        data=view.to_sse_data(),
                    )
        except asyncio.CancelledError:
            # 客户端断开（浏览器切页、网络断）会走到这里。
            # **必须重新抛出** —— 吞掉它会让 Starlette 无法正确收尾
            # 这个连接，表现为连接泄漏（`netstat` 里一堆 ESTABLISHED）。
            raise
        finally:
            # 清理订阅：不清理的话，每条断开过的连接的回调都会留在
            # 订阅器的列表里 —— 通知到达时会往一堆已经不存在的队列里塞东西，
            # 而队列没人消费 → 内存无界增长（最典型的 SSE 连接泄漏）。
            if notifier is not None:
                notifier.unsubscribe(_on_notify)
            if registry is not None:
                registry.remove(project_id=project_id, callback=_on_notify)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            # 协议 §11 明写这两条。
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 协议 §11："反向代理关闭事件流缓冲"。
            # nginx 认 X-Accel-Buffering；不设它的话 nginx 会把整个流
            # 缓冲到自己那儿再转发 —— 表现为"连上了但一直没有数据"。
            "X-Accel-Buffering": "no",
        },
    )


async def _poll_missed(
    db: Session,
    *,
    user: CurrentUser,
    watermark: int | None,
    project_id: uuid.UUID | None,
    member_ids: frozenset[uuid.UUID],
) -> list[service.EventView] | None:
    """心跳周期的一次补漏（返回 None 表示没有新事件）。

    **这是"NOTIFY 全失效"时的唯一数据通道**，因此它不是可选的优化 ——
    没有它，一次通知丢失就等于这条事件永远发不出去（直到用户重连）。
    """
    repo = EventRepository(db)
    if watermark is None:
        # 还没有水位线（新连接且初始补齐为空）→ 从 0 开始找。
        # 注意这里不能用 `backfill` 的 overflowed 语义 ——
        # 心跳补漏超限不该把整条连接打掉（连接已经建立了），
        # 而是让前端自己发现"落得太远"：发出 sync.required 提示。
        rows = repo.list_after(after_sequence=0, limit=service.MAX_BACKFILL_EVENTS,
                               project_id=project_id)
    else:
        rows = repo.list_after(
            after_sequence=watermark,
            limit=service.MAX_BACKFILL_EVENTS + 1,
            project_id=project_id,
        )

    if len(rows) > service.MAX_BACKFILL_EVENTS:
        # 落得太远：发一条 `sync.required` 事件让前端全量刷新，
        # 然后**继续**（连接不必断）。协议 §11 的事件类型列表里有它。
        logger.info("subscriber too far behind; signalling sync.required")
        return [
            service.EventView(
                event_id=uuid.uuid4(),
                sequence=watermark or 0,
                event_type=EVENT_SYNC_REQUIRED,
                project_id=project_id,
                job_id=None,
                schema_version=1,
                payload={"reason": "backfill_limit_exceeded"},
                occurred_at=repo.now(),
            )
        ]

    visible = service.filter_visible(
        db, events=rows, user=user, member_project_ids=member_ids
    )
    return list(visible) if visible else None


def _may_access_project(
    db: Session, *, user: CurrentUser, project_id: uuid.UUID
) -> bool:
    """该用户能否订阅这个项目的事件流。

    复用 WP09 的成员判据（`removed_at IS NULL`）——
    **不自己写一套**：两套判据迟早漂移，而这里漂移的后果是越权（V6/V7）。
    """
    if user.is_admin:
        return True
    from sqlalchemy import select

    from rednote.modules.projects.models import Project

    exists = db.execute(
        select(Project.id).where(Project.id == project_id).limit(1)
    ).first()
    if exists is None:
        return False
    return service.is_active_member(db, project_id=project_id, user_id=user.id)


__all__ = [
    "HEARTBEAT_SECONDS",
    "LAST_EVENT_ID_HEADER",
    "router",
]