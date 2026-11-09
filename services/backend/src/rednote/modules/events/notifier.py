"""跨实例事件分发：PG `LISTEN/NOTIFY` 订阅器（WP14，ADR-14 分发送达层）。

**这个模块解决 ADR-14 的 🔴5**：

> API 采用双实例部署，SSE 长连接建立在**某一个**实例上。
> 任务进度事件由 Worker 产生并写库，但 Worker 并不知道用户的 SSE 连接挂在哪个实例上。

做法是让**每个实例都监听同一个 PG channel**：事件无论是谁写的，通知会到所有实例，
每个实例再判断"我这儿有没有关心它的连接"。

**为什么必须用独立连接（ADR-14 明确列为代价）**：
`LISTEN` 是**连接级状态** —— 连接一关，监听就没了。而业务连接池里的连接会
在每次请求后被归还、可能被复用给别的请求、也可能被池回收。
把 LISTEN 挂在池里的连接上，会出现"监听时有时无"这种**最难查的间歇故障**。
因此本模块自己开一条专用连接，不经过 `platform/database.py` 的池。

**关于"pg_notify 是 at-most-once"**（ADR-14 反复强调）：
通知丢了不会有人告诉你。因此本模块的定位是**纯加速**：
  · 收到通知 → 读库 → 推送（快）；
  · 通知没来 → 靠心跳期间的补漏兜底（慢，但对）。
**"NOTIFY 全失效时系统仍正确"是本模块要保证的性质**，也是测试要验的。

**关于通知 payload 只带序号**（ADR-14 关键约束 1）：
两个理由 —— ①`NOTIFY` payload 有长度上限（8000 字节）；
②正文可能含敏感内容，而**通知会广播给所有实例**，不经任何权限过滤。
带序号之后，"能不能看"的判定发生在**读库环节**，而不是通知环节。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Final

import psycopg

logger = logging.getLogger(__name__)

# 事件通知的 channel 名。ADR-14 原文指定："channel 为 `domain_events_new`"。
# 写成常量而不是散落的字符串：写错一个字母不会报错，只会**静默收不到通知**，
# 而系统看起来一切正常（补漏仍在兜底）。
NOTIFY_CHANNEL: Final[str] = "domain_events_new"

# `select.select` 的超时（秒）。它决定"停止标志被设置后多久能退出循环"。
# 取 0.5 是"退出及时性"与"空转频率"的折中：再小则空闲时 CPU 空转明显，
# 再大则关闭连接时要等更久。
_POLL_TIMEOUT_SECONDS: Final[float] = 0.5


@dataclass(frozen=True)
class NotificationBatch:
    """一次通知携带的序号集合。

    **为什么是"一批"而不是"一条"**：PG 会把同一事务里的多条 `NOTIFY`
    合并投递；而且我们在读通知时本来就是一次 `read()` 拿一批。
    做成集合而非单值，调用方可以一把读库（`repository.list_by_ids`），
    避免"通知风暴时逐条查库"（ADR-14 已把"集中读"列为代价）。
    """

    sequences: tuple[int, ...]

    def __bool__(self) -> bool:
        return bool(self.sequences)


class EventNotifier:
    """一条独立的 `LISTEN` 连接 + 通知分发。

    **实例即"一个 API 实例"**（在本机测试里用它等价模拟双实例）：
    ADR-14 的 V3/V4 要求"连接挂 A、事件由 B 产生 → A 必须收到"。
    两个 `EventNotifier` 各自持一条独立连接、各自 `LISTEN`，
    那么在任何一个上面写事件，两个都会收到通知 ——
    这与"两个进程各持一条连接"在语义上等价（关键性质是
    **通知不依赖调用方自身**，而不是"必须在不同进程里"）。

    真实双实例（两个进程 + 反向代理 + 代理缓冲配置）留内网验证（登记为待办）。
    """

    def __init__(self, dsn: str, *, channel: str = NOTIFY_CHANNEL) -> None:
        """`dsn` 是**原生 psycopg DSN**（`postgresql://...`，不带 `+psycopg`）。

        **为什么直接收 DSN 而不是 SQLAlchemy 的 Engine**（本机实测踩到过）：
        走 `engine.raw_connection()` 拿到的连接处在 SQLAlchemy 的连接池里，
        池在归还/重置时会对它做 `rollback` —— 而那条连接上挂着 `LISTEN`。
        实测结果是：`LISTEN` 执行成功、`pg_notify` 也确实发出，
        但**本连接收不到任何通知**（`notifies()` 一直返回空），
        而换成一个独立的 psycopg 连接立刻就能收到。

        ADR-14 本来就要求"`LISTEN` 专用连接（**不进入业务连接池**）"，
        所以这里不是绕开池的权宜之计，而是照它说的做：
        **自己开一条完全独立的连接**。
        """
        self._dsn = dsn
        self._channel = channel
        self._connection: Any = None
        self._listeners: list[Callable[[NotificationBatch], None]] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # 诊断用：收到的通知批次数（健康检查与测试断言用）。
        self._received_batches = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def channel(self) -> str:
        return self._channel

    @property
    def connected(self) -> bool:
        """是否已建立并保持 LISTEN 连接（健康检查用）。

        ADR-14 把"连接泄漏"列为风险："LISTEN 专用连接断开会**静默**
        失去实时能力（仍可靠补漏兜底）。需健康检查与重连。"
        本属性就是那个健康检查的输入。
        """
        return self._connection is not None and not self._connection.closed

    @property
    def received_batches(self) -> int:
        """已处理的通道消息**条数**（不等于事件条数 —— 一条消息可带多个序号）。"""
        return self._received_batches

    def start(self) -> None:
        """建立 LISTEN 连接。

        **不启动后台线程**：线程由 `start_background()` 单独启动。
        分开的理由是测试 —— 大多数断言只需要"某条连接在监听"，
        不需要一个真实线程；而线程会让测试变得不确定（时序依赖）。
        本机"跑起来"的场景用 `start_background()`。
        """
        if self._connection is not None:
            return
        # 独立连接（不进连接池）。见 __init__ 的说明。
        self._connection = psycopg.connect(self._dsn, autocommit=True)
        # channel 名不能参数化（LISTEN 是 DDL 类语法）。
        # 因此这里**必须**做标识符校验，而不是"反正来自配置所以安全"。
        self._connection.execute(f'LISTEN "{_require_identifier(self._channel)}"')
        logger.info("listening on channel", extra={"channel": self._channel})

    def start_background(self) -> None:
        """起一个后台线程持续 `poll()` 并分发。"""
        self.start()
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"notifier-{self._channel}", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        """停止后台线程并关闭连接。

        **必须能被反复调用且不抛**（部署层的关闭钩子可能走多条路径）。
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        connection = self._connection
        if connection is not None:
            try:
                connection.close()
            finally:
                self._connection = None

    def _loop(self) -> None:
        """后台线程主循环（收到停止标志即退出）。"""
        while not self._stop.is_set():
            try:
                self.poll(timeout=_POLL_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001 - 循环不能因一次异常退出
                # 连接断了就重连（ADR-14：需健康检查与重连）。
                # **不退出循环**：退出等于"静默失去实时能力"，
                # 而系统看起来一切正常（补漏还在兜底）—— 那正是最难查的。
                logger.exception("notifier poll failed; will reconnect")
                self._reconnect()

    def _reconnect(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - 重连路径不能再抛
                logger.exception("closing broken notifier connection failed")
        try:
            self.start()
        except Exception:  # noqa: BLE001 - 下一轮循环会再试
            logger.exception("reconnect failed; will retry on next iteration")

    # ------------------------------------------------------------------
    # 订阅与分发
    # ------------------------------------------------------------------

    def subscribe(self, callback: Callable[[NotificationBatch], None]) -> None:
        self._listeners.append(callback)

    def unsubscribe(self, callback: Callable[[NotificationBatch], None]) -> None:
        """取消订阅。**连接断开时必须调**，否则回调持有连接对象导致泄漏。"""
        with self._lock:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

    def poll(self, *, timeout: float = 0.0) -> NotificationBatch:
        """读一次通道消息并分发给订阅者。

        **同步接口**（而不是只提供后台线程）：测试要能"写事件 → 手动 poll →
        断言收到"，不能依赖线程调度。这是本模块可测试性的关键。

        `timeout=0` 表示非阻塞（有多少读多少）。返回值供诊断与测试断言。

        **必须把 timeout 交给 psycopg 的 `notifies()`，不能用 `select` 自己等**
        （本机实测踩到过，见下面的说明）：psycopg3 的
        `Connection.notifies(timeout=None)` 在 `timeout=None` 时**会阻塞到
        收到通知为止** —— 直接对它做 `for` 循环或 `list()` 会让 poll **永久挂死**，
        而 SSE 的后台循环第一轮就会卡住。
        正确用法是 `notifies(timeout=...)`：它内部处理等待，
        超时后自然结束迭代。

        另外 `select.select([connection], ...)` 也是错的 ——
        它要的是文件描述符，而 psycopg 的 Connection 不是（会抛
        `TypeError` 或被解释成整数句柄）。**不要自己等 fd。**
        """
        if self._connection is None:
            raise RuntimeError("notifier 未启动，先调 start()")

        raw = self._connection
        sequences: list[int] = []
        for payload in self._drain_notifies(raw, timeout=timeout):
            sequence = _parse_payload(payload)
            if sequence is not None:
                sequences.append(sequence)

        self._received_batches += 1
        batch = NotificationBatch(sequences=tuple(sorted(sequences)))
        if batch:
            self._dispatch(batch)
        return batch

    def _drain_notifies(self, raw: Any, *, timeout: float) -> Iterator[Any]:
        """取出当前待处理通知的 payload（最多等 `timeout` 秒）。

        `notifies(timeout=...)` 的语义：阻塞至多 `timeout` 秒，
        期间到达的通知逐个产出，超时即结束迭代。
        `timeout=0` 时立即返回当前已缓冲的通知（非阻塞）。
        """
        for notify in raw.notifies(timeout=timeout):
            # psycopg3 的 Notify 对象：channel / payload 两个属性。
            channel = getattr(notify, "channel", None)
            if channel is not None and channel != self._channel:
                # 连接上可能监听了多个 channel（将来扩展），
                # 只处理自己的那个。
                continue
            yield getattr(notify, "payload", "")

    def _dispatch(self, batch: NotificationBatch) -> None:
        # **复制一份再遍历**：回调里可能会 unsubscribe（例如连接关闭），
        # 直接遍历原列表会在迭代中修改它。
        for callback in list(self._listeners):
            try:
                callback(batch)
            except Exception:  # noqa: BLE001 - 一个订阅者出错不能影响其他订阅者
                logger.exception(
                    "notifier listener raised; continuing with others",
                    extra={"sequences": batch.sequences},
                )


def native_dsn(database_url: str) -> str:
    """把 SQLAlchemy 风格的 URL 转成 psycopg 能直接用的 DSN。

    只做一件必要的事：**去掉驱动后缀 `+psycopg`**。
    `postgresql+psycopg://...` 是 SQLAlchemy 的写法，psycopg 不认识它
    （会报 "invalid URI"）。其余部分完全一致。

    不在这里做"解析成 host/port/..."的重建 —— 那会丢掉查询参数
    （如 `sslmode`、`connect_timeout`）并引入转义问题。
    **只做字符串替换是最不容易出错的做法。**
    """
    prefix = "postgresql+psycopg://"
    if database_url.startswith(prefix):
        return "postgresql://" + database_url[len(prefix):]
    return database_url


def _require_identifier(value: str) -> str:
    """校验 channel 名是安全的标识符。

    `LISTEN` 不接受参数占位符（它是 DDL 类语法），所以 channel 名必须
    拼进 SQL。**因此校验不能省** —— 即使当前调用方传的是常量。
    下一个人可能把它改成从配置读，而配置是外部输入。
    """
    if not value or len(value) > 63:
        raise ValueError("channel 名长度非法")
    if not value.replace("_", "").isalnum():
        # 只允许字母数字下划线：挡掉引号、分号、连字符注入。
        raise ValueError(f"channel 名含非法字符: {value!r}")
    return value


def _parse_payload(payload: str | bytes | None) -> int | None:
    """解析通知 payload 为序号。

    payload 只带序号（ADR-14 关键约束 1）。解析失败返回 None 而不是抛 ——
    一条畸形通知不该让整个 poll 循环崩掉（**它是不可信输入**：
    任何人都能在同一个 channel 上发通知）。
    """
    if payload is None:
        return None
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("ascii")
        except UnicodeDecodeError:
            return None
    try:
        value = int(payload.strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# ---------------------------------------------------------------------------
# 进程内订阅表
# ---------------------------------------------------------------------------


@dataclass
class SubscriberRegistry:
    """进程内的"哪个连接关心哪些事件"。

    **这不是"真相"**（真相在 `domain_events`），它只是本进程的路由表：
    通知到达后，用它决定"要不要为某个连接去读库"。

    **为什么放在进程内就够**：SSE 连接本身是进程内的资源 ——
    实例 A 的路由表只需要知道"A 上有哪些连接"。
    跨实例的部分由 PG 的 `LISTEN/NOTIFY` 承担（每个实例都收到通知）。
    这正是 ADR-14 否决"应用内内存广播"的原因：**广播跨不了实例，
    但路由表本来就不需要跨实例**。

    **权限不在这里判定**：路由表只按 `project_id` 粗筛，
    真正的鉴权在 `service.can_view_event` 里逐事件做（ADR-14 关键约束 3）。
    把鉴权放进路由表会退化成"建连时判一次" —— 而权限在会话期间会变。
    """

    _by_project: dict[Any, set[Callable[[NotificationBatch], None]]] = field(
        default_factory=dict
    )
    _all: set[Callable[[NotificationBatch], None]] = field(default_factory=set)

    def add(
        self, *, project_id: Any, callback: Callable[[NotificationBatch], None]
    ) -> None:
        """注册一个连接。

        `project_id=None` 表示"订阅全量"（管理者）—— 放 `_all` 里，
        任何通知都要叫醒它并让它自己读库鉴权。
        """
        with self._lock_guard():
            if project_id is None:
                self._all.add(callback)
            else:
                self._by_project.setdefault(project_id, set()).add(callback)

    def remove(
        self, *, project_id: Any, callback: Callable[[NotificationBatch], None]
    ) -> None:
        with self._lock_guard():
            if project_id is None:
                self._all.discard(callback)
                return
            bucket = self._by_project.get(project_id)
            if bucket is not None:
                bucket.discard(callback)
                if not bucket:
                    # 清掉空桶：否则"连接来来去去"会让这个 dict 无界增长
                    # （每个项目一个桶，桶里空集合）。
                    del self._by_project[project_id]

    def callbacks_for(self, *, project_id: Any) -> set[Callable[[NotificationBatch], None]]:
        """给定事件所属项目，返回需要被叫醒的连接。

        **宽进严出**：这里只做粗筛（项目匹配或全量订阅），
        真正的"能不能看这条"由鉴权决定。宁可多叫醒几个然后再过滤掉，
        也不要在这里精确判断 —— 那会把鉴权逻辑复制一份到路由层。
        """
        with self._lock_guard():
            matched = set(self._all)
            if project_id is not None:
                matched |= self._by_project.get(project_id, set())
            return matched

    def sizes(self) -> tuple[int, int]:
        """`(按项目订阅的连接数, 全量订阅的连接数)` —— 诊断用。"""
        with self._lock_guard():
            per_project = sum(len(bucket) for bucket in self._by_project.values())
            return per_project, len(self._all)

    @staticmethod
    def _lock_guard() -> threading.Lock:
        """懒初始化的锁。

        用类级静态锁而不是实例字段：`dataclass` 的默认工厂在
        `frozen=False` 时字段可被替换，而锁不该被替换。
        用一把全局锁足够 —— 这个表只在"连接建立/断开/通知到达"时被碰，
        不是热点路径（20 在线规模）。
        """
        global _REGISTRY_LOCK
        return _REGISTRY_LOCK


_REGISTRY_LOCK = threading.Lock()


__all__ = [
    "NOTIFY_CHANNEL",
    "EventNotifier",
    "NotificationBatch",
    "SubscriberRegistry",
    "native_dsn",
]