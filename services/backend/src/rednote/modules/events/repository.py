"""领域事件的存储访问（WP14）。

**本层的核心是两条查询**：

  · **补漏**：`WHERE sequence > :last ORDER BY sequence LIMIT :limit + 1`
    —— 多取一条用来判断"还有没有更多"（与 WP10/WP11 的键集分页同一手法，
    避免多发一次 `COUNT(*)`）；
  · **初始补齐**：按 `project_id` 取最近 N 条 —— 新建连接时前端
    至少要知道"现在是什么状态"，而不是从此刻起才有事件。

**关于"按 sequence 单调递增"**：PG 的序列在并发下**不保证提交顺序与序号顺序一致**
（事务 A 拿到 100、事务 B 拿到 101，B 先提交 —— 此时读到的 101 之后才出现 100）。
这意味着**补漏可能漏掉"序号更小但提交更晚"的事件**。

这不是本包能彻底解决的问题（它是序列与提交顺序的固有差异），处置方式是：

  · **水位线留一点余量**：补漏与实时合并时，不用"已推送的最大 sequence"
    直接当水位线，而是用它**减去一个很小的回看窗口**再查一次，重复的靠去重吸收。
    （本包在 `service.merge_events` 里用 `sequence > last` 严格判断，
     回看窗口在 §8 登记为待接入项 —— 本机单进程写事件时不会触发该场景。）

  · 并把这条限制**写进 docstring**，而不是假装不存在。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from rednote.modules.events.models import DomainEvent


class EventRepository:
    """领域事件的读写入口。事务边界由调用方（service）控制。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def flush(self) -> None:
        self._session.flush()

    def now(self) -> datetime:
        """取数据库时间（与 WP13 同一理由：租约/水位线判定要用服务端时间）。"""
        return self._session.execute(select(func.now())).scalar_one()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def insert_event(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        project_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        aggregate_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
        schema_version: int | None = None,
        occurred_at: datetime | None = None,
    ) -> DomainEvent:
        """插入事件并**返回带 `sequence` 的实例**。

        `sequence` 由数据库序列生成（`autoincrement`），因此必须 `flush()`
        之后才拿得到值 —— 而这个值正是要发给前端的 `id:`。
        所以本方法**内部就 flush**（与其它包的 repository 不同：
        那里把 flush 留给调用方是为了把约束错误控制在 SAVEPOINT 内，
        这里则是因为"没有 sequence 就无法继续"）。

        同时返回 sequence 供**NOTIFY 的 payload** 使用（ADR-14：
        "payload 只带 event id"）。
        """
        event = DomainEvent(
            event_type=event_type,
            payload=payload,
            project_id=project_id,
            actor_id=actor_id,
            aggregate_id=aggregate_id,
            job_id=job_id,
            occurred_at=occurred_at or func.now(),
        )
        if schema_version is not None:
            event.schema_version = schema_version
        self._session.add(event)
        self._session.flush()
        return event

    def notify(self, *, channel: str, payload: str) -> None:
        """在当前事务里发一条 `NOTIFY`（ADR-14 的分发送达层）。

        **用 `pg_notify()` 函数而不是 `NOTIFY` 语句**：前者接受**参数**
        （可以安全传字符串），后者只能拼接 SQL 字面量 ——
        而 channel 名与 payload 都是外部输入（channel 来自配置、payload 是
        序列号字符串），拼接是自找 SQL 注入。

        **通知与事件写在同一事务里**：PG 的 `NOTIFY` 会在事务提交时才真正投递
        （这是 PG 的保证），因此"事件可见"与"通知发出"是原子的。
        这比"先提交、再单独发通知"更稳 —— 后者在两步之间崩溃就丢了通知。
        """
        self._session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": channel, "payload": payload},
        )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def list_after(
        self,
        *,
        after_sequence: int,
        limit: int,
        project_id: uuid.UUID | None = None,
    ) -> list[DomainEvent]:
        """补漏：取序号大于 `after_sequence` 的事件，返回**最多 limit+1 条**。

        多取一条供调用方判断"是否还有更多"（补漏超限的信号）。

        `project_id` 为 None 表示"不做项目过滤"（管理者看全量）。
        **但调用方不应把它当成"免鉴权"** —— 拿到行之后仍要逐事件过滤
        （ADR-14 关键约束 2、3）。
        """
        stmt = select(DomainEvent).where(DomainEvent.sequence > after_sequence)
        if project_id is not None:
            stmt = stmt.where(DomainEvent.project_id == project_id)
        stmt = stmt.order_by(DomainEvent.sequence).limit(limit + 1)
        return list(self._session.execute(stmt).scalars().all())

    def list_recent(
        self, *, project_id: uuid.UUID | None, limit: int
    ) -> list[DomainEvent]:
        """初始补齐：某项目最近的事件（**按 sequence 升序返回**）。

        建连时前端至少要知道"现在是什么状态"，而不是从此刻起才有事件。
        取的是"最近 N 条"（倒序取、再正序返回），因为用户关心的是近况。
        """
        stmt = select(DomainEvent)
        if project_id is not None:
            stmt = stmt.where(DomainEvent.project_id == project_id)
        stmt = stmt.order_by(DomainEvent.sequence.desc()).limit(limit)
        rows = list(self._session.execute(stmt).scalars().all())
        rows.reverse()
        return rows

    def list_by_ids(self, *, sequences: Sequence[int]) -> list[DomainEvent]:
        """按序号取事件（订阅器收到 NOTIFY 后用它读库）。

        ADR-14 关键约束 1 要求"订阅实例按 id 读库并做权限过滤"，
        本方法就是那个"按 id 读"。用 `IN` 一次取多条，
        避免"通知风暴时逐条查库"（ADR-14 已把"集中读"列为代价）。
        """
        if not sequences:
            return []
        stmt = (
            select(DomainEvent)
            .where(DomainEvent.sequence.in_(list(sequences)))
            .order_by(DomainEvent.sequence)
        )
        return list(self._session.execute(stmt).scalars().all())

    def max_sequence(self) -> int:
        """当前最大序号；无事件时返回 0。

        用途是"新连接从哪儿开始"。用 `coalesce(max(...), 0)` 而不是
        `count(*)`：序号可能因清理出现空洞，计数得到的"第 N 条"
        与"第 N 号"不是一回事（与 WP11 的 `max_revision_no` 同一判断）。
        """
        return int(
            self._session.execute(
                select(func.coalesce(func.max(DomainEvent.sequence), 0))
            ).scalar_one()
        )


__all__ = ["EventRepository"]