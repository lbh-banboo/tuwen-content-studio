"""审计事件与通知回执的存储访问（WP15）。

**本层最要紧的三件事**：

1. **审计写入必须同事务**。审计是"业务动作发生了什么"的凭据，
   若业务提交了但审计没写（或反过来），凭据就不再可信。
   因此 `insert_audit_event` **不加 commit** —— 由外层事务统一提交，
   与 WP12/WP13 的 Outbox 同一模式。

2. **待办 dedupe 必须靠数据库唯一索引**，不能靠"先查再插"。
   并发下两个进程同时发现"该提醒"，先查再插会让两个都插入
   （同一对象被提醒两次）。`uq_work_items_open_dedupe`（部分唯一索引，
   `WHERE state='OPEN' AND dedupe_key IS NOT NULL`）才是唯一防线。
   本层用 `ON CONFLICT DO NOTHING` + 回查（WP12/WP13 已验证的手法）。

3. **分页用键集分页**（`tuple_(created_at, id) < tuple_(:cursor)` + `limit+1`），
   与 WP10/WP11 一致。`created_at` 不唯一，因此游标必须带 `id` 打破平局 —
   否则同一秒内的多条记录会在翻页时漏项或重复。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from rednote.modules.audit.models import (
    AuditEvent,
    NotificationReceipt,
)
from rednote.modules.work_items.models import (
    WORK_ITEM_OPEN,
    WorkItem,
)


class AuditRepository:
    """审计与待办的读写入口。事务边界由调用方（service / API 层）控制。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def flush(self) -> None:
        self._session.flush()

    # ------------------------------------------------------------------
    # 审计事件
    # ------------------------------------------------------------------

    def insert_audit_event(
        self,
        *,
        actor_kind: str,
        action: str,
        object_type: str,
        object_id: uuid.UUID,
        request_id: str,
        actor_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        version_id: uuid.UUID | None = None,
        before_hash: str | None = None,
        after_hash: str | None = None,
        reason: str | None = None,
    ) -> AuditEvent:
        """插入审计事件（**同事务**，不加 commit）。

        见模块 docstring：审计与业务数据必须一致，因此由外层事务统一提交。
        """
        event = AuditEvent(
            actor_kind=actor_kind,
            actor_id=actor_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            request_id=request_id,
            project_id=project_id,
            version_id=version_id,
            before_hash=before_hash,
            after_hash=after_hash,
            reason=reason,
        )
        self._session.add(event)
        return event

    def list_audit_events(
        self,
        *,
        project_id: uuid.UUID | None = None,
        object_type: str | None = None,
        object_id: uuid.UUID | None = None,
        limit: int,
        after: tuple[datetime, uuid.UUID] | None = None,
    ) -> list[AuditEvent]:
        """键集分页查询审计（返回**最多 limit+1 条**）。

        排序为 `created_at DESC, id DESC` —— 最新在最前（排障时先看到最近的）。
        `created_at` 不唯一，因此游标必须带 `id` 打破平局
        （同一秒内的多条记录否则会在翻页时漏项或重复）。

        `after=(created_at, id)` 是上一页最后一条的排序值，
        用行值比较 `(created_at, id) < (:t, :i)` 下推给 SQL。
        """
        stmt = select(AuditEvent)
        if project_id is not None:
            stmt = stmt.where(AuditEvent.project_id == project_id)
        if object_type is not None:
            stmt = stmt.where(AuditEvent.object_type == object_type)
        if object_id is not None:
            stmt = stmt.where(AuditEvent.object_id == object_id)
        if after is not None:
            stmt = stmt.where(
                tuple_(AuditEvent.created_at, AuditEvent.id) < tuple_(*after)
            )
        stmt = stmt.order_by(
            AuditEvent.created_at.desc(), AuditEvent.id.desc()
        ).limit(limit + 1)
        return list(self._session.execute(stmt).scalars().all())

    def list_audit_events_for_projects(
        self,
        *,
        project_ids: Sequence[uuid.UUID],
        object_type: str | None = None,
        object_id: uuid.UUID | None = None,
        limit: int,
        after: tuple[datetime, uuid.UUID] | None = None,
    ) -> list[AuditEvent]:
        """按**一组**项目查审计（非管理者且未指定项目时的路径）。

        **为什么不是"逐项目查一遍再合并"**：合并后的分页游标无法正确工作 ——
        每个子查询各自返回 limit+1 条，合并后再排序截断，会漏掉
        "某个项目的较早记录"与"另一个项目的较晚记录"之间的正确顺序。
        一条 `IN (...)` 查询 + 一次排序才是正确的键集分页。

        `project_ids` 为空时**返回空列表**（不是"不过滤"）——
        这是安全侧：非管理者不属于任何项目时，不该看到任何审计。
        若写成"空就不过滤"，他就看到了全量。
        """
        if not project_ids:
            return []
        stmt = select(AuditEvent).where(AuditEvent.project_id.in_(list(project_ids)))
        if object_type is not None:
            stmt = stmt.where(AuditEvent.object_type == object_type)
        if object_id is not None:
            stmt = stmt.where(AuditEvent.object_id == object_id)
        if after is not None:
            stmt = stmt.where(
                tuple_(AuditEvent.created_at, AuditEvent.id) < tuple_(*after)
            )
        stmt = stmt.order_by(
            AuditEvent.created_at.desc(), AuditEvent.id.desc()
        ).limit(limit + 1)
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # 待办（work_items 已在 WP10 建，本层只消费）
    # ------------------------------------------------------------------

    def get_work_item(self, work_item_id: uuid.UUID) -> WorkItem | None:
        return self._session.get(WorkItem, work_item_id)

    def find_open_by_dedupe_key(
        self, *, dedupe_key: str, project_id: uuid.UUID | None
    ) -> WorkItem | None:
        """按去重键找未结待办（dedupe 的回查）。"""
        stmt = select(WorkItem).where(
            WorkItem.dedupe_key == dedupe_key,
            WorkItem.state == WORK_ITEM_OPEN,
        )
        if project_id is None:
            stmt = stmt.where(WorkItem.project_id.is_(None))
        else:
            stmt = stmt.where(WorkItem.project_id == project_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def upsert_work_item(
        self,
        *,
        dedupe_key: str,
        kind: str,
        subject_type: str,
        subject_id: uuid.UUID,
        project_id: uuid.UUID | None = None,
        assignee_id: uuid.UUID | None = None,
        due_at: datetime | None = None,
    ) -> tuple[WorkItem, bool]:
        """按去重键建待办；已存在则返回既有（`created=False`）。

        **dedupe 的正确姿势**（数据结构 §3「同未结待办 dedupe_key 条件 UQ」）：
        `ON CONFLICT DO NOTHING` + 回查，而不是"先查再插"。
        并发下两个进程同时发现"该提醒"，先查再插会让两个都插入 ——
        同一对象被提醒两次，而这件事在语义上完全正常（重试、并发触发）。
        唯一索引是**唯一防线**，本方法只是正确地用它。

        `ON CONFLICT` 的目标必须是**部分唯一索引**（`uq_work_items_open_dedupe`）。
        由于它带 WHERE 条件，`index_elements` 也要带同样的
        `postgresql_where` 才能命中 —— 否则 `ON CONFLICT` 不会识别它。
        """
        stmt = (
            pg_insert(WorkItem)
            .values(
                dedupe_key=dedupe_key,
                kind=kind,
                subject_type=subject_type,
                subject_id=subject_id,
                project_id=project_id,
                assignee_id=assignee_id,
                due_at=due_at,
            )
            .on_conflict_do_nothing(
                index_elements=["dedupe_key"],
                index_where=(
                    (WorkItem.state == WORK_ITEM_OPEN)
                    & (WorkItem.dedupe_key.is_not(None))
                ),
            )
            .returning(WorkItem.id)
        )
        created_id = self._session.execute(stmt).scalar_one_or_none()
        if created_id is not None:
            item = self._session.get(WorkItem, created_id)
            assert item is not None
            return item, True

        existing = self.find_open_by_dedupe_key(
            dedupe_key=dedupe_key, project_id=project_id
        )
        assert existing is not None  # ON CONFLICT 未插入说明已存在
        return existing, False

    def list_work_items_visible(
        self,
        *,
        project_id: uuid.UUID | None,
        state: str,
        user_id: uuid.UUID,
        member_project_ids: Sequence[uuid.UUID],
        include_all: bool,
        limit: int,
        after: tuple[datetime, uuid.UUID] | None = None,
    ) -> list[WorkItem]:
        """按**可见范围**查待办（返回最多 limit+1 条）。

        可见范围的 SQL 表达（与 `service._may_see_item` 是同一规则的批量版）：

            include_all（管理者）
              OR assignee_id = :me
              OR (assignee_id IS NULL AND project_id IN (我所属项目))

        **为什么用 `or_` 而不是拆成两次查询再合并**：合并的分页游标不正确
        （同 `list_audit_events_for_projects` 的理由）。

        **`member_project_ids` 为空时**：`IN ()` 在 SQL 里是恒假，
        因此非管理者且不属于任何项目时只能看到"指派给自己的" —— 这是对的
        （未认领的项目待办他本来就无权看到）。

        **`project_id` 作为额外过滤**：它表示"我只要这个项目的"，
        与可见范围是两回事（可见范围是"我能看哪些"）。两者是 AND 关系。
        """
        visibility = or_(
            WorkItem.assignee_id == user_id,
            (
                WorkItem.assignee_id.is_(None)
                & WorkItem.project_id.in_(list(member_project_ids))
            ),
        )
        stmt = select(WorkItem).where(WorkItem.state == state)
        if not include_all:
            stmt = stmt.where(visibility)
        if project_id is not None:
            stmt = stmt.where(WorkItem.project_id == project_id)
        if after is not None:
            stmt = stmt.where(
                tuple_(WorkItem.created_at, WorkItem.id) < tuple_(*after)
            )
        stmt = stmt.order_by(
            WorkItem.created_at.desc(), WorkItem.id.desc()
        ).limit(limit + 1)
        return list(self._session.execute(stmt).scalars().all())

    def resolve_work_item(
        self,
        *,
        work_item_id: uuid.UUID,
        reason: str | None,
    ) -> bool:
        """把待办从 OPEN 置为 DONE。**只应在它当前是 OPEN 时生效**。

        返回 rowcount == 1（真的改了）还是 0（它已经不在 OPEN 状态）。
        这是完成待办的**幂等性保证**：重复调用时第二次 rowcount 为 0，
        调用方据此返回"已完成"而不是重复触发副作用（比如再发一条通知）。

        `reason` 写入 `updated_at`（ORM 的 `onupdate`）——
        不新增一列：`reason` 是用户填的，但"完成"这个动作的凭据
        应该由**审计**承载（`audit_events.reason`），而不是塞在 work_item 里。
        work_item 是状态，审计是凭据，两者分开。
        """
        result = self._session.execute(
            update(WorkItem)
            .where(
                WorkItem.id == work_item_id,
                WorkItem.state == WORK_ITEM_OPEN,
            )
            .values(
                state="DONE",
                updated_at=func.now(),
            )
        )
        return result.rowcount == 1

    # ------------------------------------------------------------------
    # 通知回执
    # ------------------------------------------------------------------

    def mark_delivered(
        self,
        *,
        user_id: uuid.UUID,
        event_id: uuid.UUID,
    ) -> tuple[NotificationReceipt, bool]:
        """记录一条通知已送达（**幂等**：重复送达不重复计数）。

        用 `ON CONFLICT DO NOTHING` + 回查：通知可能因重连/补漏被多次送达，
        而"这条通知发给这个人了"是**一条事实**而不是一个计数。
        """
        stmt = (
            pg_insert(NotificationReceipt)
            .values(user_id=user_id, event_id=event_id)
            .on_conflict_do_nothing(
                index_elements=["user_id", "event_id"]
            )
            .returning(NotificationReceipt.user_id)
        )
        created = self._session.execute(stmt).scalar_one_or_none()
        receipt = self._session.get(NotificationReceipt, (user_id, event_id))
        assert receipt is not None
        return receipt, created is not None

    def mark_read(
        self,
        *,
        user_id: uuid.UUID,
        event_id: uuid.UUID,
    ) -> bool:
        """把一条回执置为已读（`read_at`）。

        **幂等**：只在 `read_at IS NULL` 时写 —— 重复标记不会产生"第二次已读"，
        也不会覆盖第一次已读的时间（时间本身就是凭据）。
        """
        result = self._session.execute(
            update(NotificationReceipt)
            .where(
                NotificationReceipt.user_id == user_id,
                NotificationReceipt.event_id == event_id,
                NotificationReceipt.read_at.is_(None),
            )
            .values(read_at=func.now())
        )
        return result.rowcount == 1

    def mark_dismissed(
        self,
        *,
        user_id: uuid.UUID,
        event_id: uuid.UUID,
    ) -> bool:
        """把一条回执置为已忽略（`dismissed_at`）。与 mark_read 同一手法。"""
        result = self._session.execute(
            update(NotificationReceipt)
            .where(
                NotificationReceipt.user_id == user_id,
                NotificationReceipt.event_id == event_id,
                NotificationReceipt.dismissed_at.is_(None),
            )
            .values(dismissed_at=func.now())
        )
        return result.rowcount == 1

    def list_unread(
        self,
        *,
        user_id: uuid.UUID,
        limit: int,
    ) -> list[NotificationReceipt]:
        """该用户的未读且未忽略的通知（小红点的数据源）。

        用 `ix_notification_receipts_unread` 部分索引
        （`read_at IS NULL AND dismissed_at IS NULL`）。
        """
        stmt = (
            select(NotificationReceipt)
            .where(
                NotificationReceipt.user_id == user_id,
                NotificationReceipt.read_at.is_(None),
                NotificationReceipt.dismissed_at.is_(None),
            )
            .order_by(NotificationReceipt.created_at.desc())
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars().all())


__all__ = ["AuditRepository"]