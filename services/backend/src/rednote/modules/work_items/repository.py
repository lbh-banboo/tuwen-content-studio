"""待办仓储层（WP10）。

**本层只做一件事：把"转交"表达成一条 SQL。**

需求 §2.3 第 2 条的三种归属：
| 待办当前归属 | 交接后 | SQL 条件 |
|---|---|---|
| 未认领（`assignee_id IS NULL`） | 转新负责人 | `assignee_id IS NULL` |
| 原负责人（`assignee_id = 旧负责人`） | 转新负责人 | `assignee_id = :old` |
| 其他协作者 | **保留** | 不匹配上面任一条件 |

用一条 `UPDATE ... WHERE` 完成，而不是"查出来、在 Python 里逐条判断、再逐条更新"：
- 逐条更新在待办量大时会产生 N 次往返；
- 更重要的是，逐条判断会引入"读与写之间的窗口"——
  在遍历过程中若有人新建了一条属于旧负责人的待办，它会被漏掉。
  单条 UPDATE 在同一语句内完成筛选与写入，没有这个窗口。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from rednote.modules.work_items.models import (
    WORK_ITEM_OPEN,
    WorkItem,
)


class WorkItemRepository:
    """待办的读写入口。事务边界由调用方控制。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        kind: str,
        subject_type: str,
        subject_id: uuid.UUID,
        project_id: uuid.UUID | None = None,
        assignee_id: uuid.UUID | None = None,
        dedupe_key: str | None = None,
    ) -> WorkItem:
        """建一条待办。

        **不预查 dedupe_key 是否存在再插入** —— 那有并发窗口。
        让 `uq_work_items_open_dedupe` 做最后防线，冲突由调用方处理。
        """
        item = WorkItem(
            project_id=project_id,
            assignee_id=assignee_id,
            kind=kind,
            subject_type=subject_type,
            subject_id=subject_id,
            state=WORK_ITEM_OPEN,
            dedupe_key=dedupe_key,
        )
        self._session.add(item)
        self._session.flush()
        return item

    def list_open_for_project(self, project_id: uuid.UUID) -> list[WorkItem]:
        return list(
            self._session.execute(
                select(WorkItem)
                .where(
                    WorkItem.project_id == project_id,
                    WorkItem.state == WORK_ITEM_OPEN,
                )
                .order_by(WorkItem.created_at, WorkItem.id)
            )
            .scalars()
            .all()
        )

    def count_open_for_assignee(
        self, project_id: uuid.UUID, assignee_id: uuid.UUID | None
    ) -> int:
        """某人在某项目的未结待办数。`assignee_id=None` 表示未认领。"""
        stmt = select(func.count()).select_from(WorkItem).where(
            WorkItem.project_id == project_id,
            WorkItem.state == WORK_ITEM_OPEN,
        )
        stmt = stmt.where(
            WorkItem.assignee_id.is_(None)
            if assignee_id is None
            else WorkItem.assignee_id == assignee_id
        )
        return int(self._session.execute(stmt).scalar_one())

    def transfer_open_items(
        self,
        project_id: uuid.UUID,
        *,
        from_assignee_id: uuid.UUID | None,
        to_assignee_id: uuid.UUID,
    ) -> int:
        """把"未认领 + 原负责人"的未结待办转给新负责人。返回受影响行数。

        `from_assignee_id=None` 表示只转交**未认领**的待办
        （用于"项目尚无负责人时认领"的场景）；
        传具体 UUID 则同时转交"未认领"与该负责人的待办。

        `synchronize_session=False`：这是批量 UPDATE，session 里已加载的
        WorkItem 实例不会逐个同步。调用方若需要最新对象应重新查询 ——
        交接流程本来就会重新读，因此跳过同步省一次全量刷新。
        """
        conditions = [WorkItem.assignee_id.is_(None)]
        if from_assignee_id is not None:
            conditions.append(WorkItem.assignee_id == from_assignee_id)

        stmt = (
            update(WorkItem)
            .where(
                WorkItem.project_id == project_id,
                WorkItem.state == WORK_ITEM_OPEN,
                or_(*conditions),
            )
            .values(assignee_id=to_assignee_id)
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(stmt)
        self._session.flush()
        return int(result.rowcount or 0)


__all__ = ["WorkItemRepository"]
