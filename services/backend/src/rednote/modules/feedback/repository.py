"""`content_feedback_items` / `content_feedback_events` / `content_reviews` 的仓储。

跨模块取数走**裸 SQL**（平级模块不可互 import），列名即对方契约：
写错列名当场 `UndefinedColumn`，不会静默。

## 当前正文的口径（与 `media/repository.py` 同一条）

**当前正文版本** = `content_items.current_version_id` 投影；投影为 NULL 时
回落 `ORDER BY version_no DESC LIMIT 1`。**禁止**直接 `ORDER BY version_no DESC`
取末版：回滚后投影指向旧版，而末版是回滚前的那一版。

本方法只负责"读出正文"，**不做切段** —— 切段是 media 的职责，反馈模块
不复制第二份切段实现（否则 `paragraph_ref` 校验会与 media 生成时的
口径分叉，而分叉是静默的）。

## 四个只读方法的用途

| 方法 | 谁用 | 为什么仓储层要提供 |
|---|---|---|
| `current_content_body` | 服务层校验 `paragraph_ref` 是否在**当前**正文里 | 反馈定位必须对着"现在这一版"判，而不是 `observed_version_id`（见 module docstring）；后者决定列表页的锚点展示 |
| `media_item_content_id` | 服务层校验媒体锚点归属（可读的 4xx） | DDL 触发器是最后一道，但它只在提交时以 23514 的形式冒出来，且无法定位到字段 |
| `list_media_items_for_content` | 预览页把图片与正文段落对位 | `media_items` 是对方的表；本包不 import 它的 ORM 模型 |
| `list_title_candidates_for_version` | 预览页展示标题候选与"已采纳"标记 | 同上；"采纳"记在 `content_versions.adopted_title_candidate_id` 上，不在候选表里 |
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.orm import Session

from rednote.modules.feedback.models import (
    ContentFeedbackEvent,
    ContentFeedbackItem,
    ContentReview,
)


class FeedbackRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # ------------------------------------------------------------------
    # 跨模块只读（裸 SQL）
    # ------------------------------------------------------------------

    def current_content_body(self, content_item_id: uuid.UUID) -> str | None:
        """当前正文（`content_versions.body`）；内容不存在或一版都没有时 `None`。

        投影优先，投影为 NULL 时回落末版（见模块 docstring）。

        ⚠️ 裸 SQL，列名是 `content_items` / `content_versions` 的契约
        （`current_version_id` / `content_item_id` / `version_no` / `body`）。
        """
        row = self._session.execute(
            text(
                "SELECT cv.body FROM content_items ci "
                "JOIN content_versions cv ON cv.id = ci.current_version_id "
                "WHERE ci.id = :cid"
            ),
            {"cid": content_item_id},
        ).first()
        if row is not None:
            return row[0]
        # 回落：投影为 NULL（"先建身份、后建版本"的合法中间态）时取最后落库的一版。
        fallback = self._session.execute(
            text(
                "SELECT body FROM content_versions "
                "WHERE content_item_id = :cid ORDER BY version_no DESC LIMIT 1"
            ),
            {"cid": content_item_id},
        ).first()
        return None if fallback is None else fallback[0]

    def current_content_version_id(
        self, content_item_id: uuid.UUID
    ) -> uuid.UUID | None:
        """当前正文版本的 id（投影优先，投影为 NULL 时回落末版）。

        **与 `current_content_body` 同一条口径**（见模块 docstring）：
        批量审核要为每条内容绑定 `subject_version_id`，而"当前版本"不能用
        `ORDER BY version_no DESC` 直接取末版 —— 回滚后投影指向旧版，
        末版是回滚前的那一版，审核结论会绑到用户看不到的版本上。
        """
        row = self._session.execute(
            text(
                "SELECT ci.current_version_id FROM content_items ci "
                "WHERE ci.id = :cid"
            ),
            {"cid": content_item_id},
        ).first()
        if row is not None and row[0] is not None:
            return row[0]
        fallback = self._session.execute(
            text(
                "SELECT id FROM content_versions "
                "WHERE content_item_id = :cid ORDER BY version_no DESC LIMIT 1"
            ),
            {"cid": content_item_id},
        ).first()
        return None if fallback is None else fallback[0]

    def media_item_content_id(self, media_item_id: uuid.UUID) -> uuid.UUID | None:
        """媒体位 → 所属内容 id；不存在时 `None`。

        服务层据此判"媒体锚点是否属于这条内容"，给出可读的 4xx；
        DDL 触发器（`ddl.anchor_consistency_ddl`）是最后一道。
        """
        row = self._session.execute(
            text("SELECT content_item_id FROM media_items WHERE id = :mid"),
            {"mid": media_item_id},
        ).first()
        return None if row is None else row[0]

    def list_media_items_for_content(
        self, content_item_id: uuid.UUID
    ) -> tuple[tuple[uuid.UUID, str, int, str], ...]:
        """一条内容下的全部媒体位 `(id, paragraph_ref, sort_order, state)`。

        **按 `sort_order` 排序**：预览页用它在"6 格"里与段落对位展示。
        ⚠️ 裸 SQL，列名是 `media_items` 的契约（`content_item_id` /
        `paragraph_ref` / `sort_order` / `state`）。
        """
        rows = self._session.execute(
            text(
                "SELECT id, paragraph_ref, sort_order, state "
                "FROM media_items WHERE content_item_id = :cid "
                "ORDER BY sort_order"
            ),
            {"cid": content_item_id},
        ).all()
        return tuple((row[0], row[1], int(row[2]), row[3]) for row in rows)

    def list_title_candidates_for_version(
        self, content_version_id: uuid.UUID
    ) -> tuple[tuple[uuid.UUID, str, bool], ...]:
        """某版内容的标题候选 `(id, title, is_adopted)`。

        **为什么把 `is_adopted` 放在 SQL 里算**：候选表本身**没有**采纳列
        （"哪一版用了哪个标题"只记在 `content_versions.adopted_title_candidate_id` 上，
        候选表保持不可变）。预览页要"哪些候选、哪个被采纳"，若把两个查询分开，
        调用方就得自己比对 id —— 那个比对正好是给前端契约"已采纳"标记的来源，
        分开做迟早有人漏。

        **按 `candidate_index` 排序**：输入顺序是模型给的优先级信号
        （小红书第一个标题权重最高），预览页按输入顺序展示。
        """
        rows = self._session.execute(
            text(
                "SELECT tc.id, tc.title, "
                "COALESCE(cv.adopted_title_candidate_id = tc.id, false) AS is_adopted "
                "FROM title_candidates tc "
                "JOIN content_versions cv ON cv.id = tc.content_version_id "
                "WHERE tc.content_version_id = :vid "
                "ORDER BY tc.candidate_index"
            ),
            {"vid": content_version_id},
        ).all()
        return tuple((row[0], row[1], bool(row[2])) for row in rows)

    # ------------------------------------------------------------------
    # content_feedback_items（本模块表，可变）
    # ------------------------------------------------------------------

    def insert_feedback_item(self, **columns: Any) -> ContentFeedbackItem:
        row = ContentFeedbackItem(**columns)
        self._session.add(row)
        return row

    def get_feedback_item(
        self, feedback_item_id: uuid.UUID
    ) -> ContentFeedbackItem | None:
        return self._session.get(ContentFeedbackItem, feedback_item_id)

    def feedback_item_project_id(
        self, feedback_item_id: uuid.UUID
    ) -> uuid.UUID | None:
        """反馈项 → 所属项目 id；不存在时 `None`。

        **为什么单独一个方法而不是让端点先 `get_feedback_item`**：
        `PATCH /feedback/{feedbackId}` 的路径里没有 `projectId`，而权限判定
        需要它（两步骤鉴权的第一步）。这一步只问"它属于哪个项目"，
        不需要把整行载入 session —— 载入会让一个纯鉴权读参与后续的
        ORM 身份映射，而随后的 `service.advance_feedback` 还要重读一次
        并在锁内比对，两次读的口径越少交叉越好。

        裸 SQL：列名 `project_id` 是本表自己的契约（同一文件既有风格）。
        """
        row = self._session.execute(
            text(
                "SELECT project_id FROM content_feedback_items WHERE id = :fid"
            ),
            {"fid": feedback_item_id},
        ).first()
        return None if row is None else row[0]

    def list_feedback_items(
        self,
        *,
        content_item_id: uuid.UUID,
        state: str | None = None,
        category: str | None = None,
        severity: str | None = None,
        anchor_kind: str | None = None,
        exclude_states: Sequence[str] | None = None,
        before: tuple[datetime, uuid.UUID] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ContentFeedbackItem]:
        """一条内容的反馈，新的在前。各筛选参数为可选、彼此取交集。

        ## 为什么多了 `exclude_states` / `category` / `severity`

        · `exclude_states`：审核通过前查"未解决的 BLOCKING"（排除
          `RESOLVED`/`REJECTED`）—— 若先取全量再在 Python 里过滤，
          窗口一旦被大量已解决项占满，未解决项就会被漏判。
        · `category` / `severity`：列表端点的筛选条件要下推到 SQL，
          否则分页窗口会在"过滤前"切，返回条数看起来时多时少。

        ## 为什么新增 `before`（keyset）而保留 `offset`

        `created_at` 是持续写入的排序键，`OFFSET` 在并发新增时会**漏项或重复**。
        列表端点用 `before = (上一页末条的 created_at, id)` 继续读（与
        `created_at DESC, id DESC` 的排序严格对应：`created_at` 更小，
        或相等且 `id` 更小）。`offset` 保留给既有调用方，两者互斥使用。
        """
        stmt = select(ContentFeedbackItem).where(
            ContentFeedbackItem.content_item_id == content_item_id
        )
        if state is not None:
            stmt = stmt.where(ContentFeedbackItem.state == state)
        if category is not None:
            stmt = stmt.where(ContentFeedbackItem.category == category)
        if severity is not None:
            stmt = stmt.where(ContentFeedbackItem.severity == severity)
        if anchor_kind is not None:
            stmt = stmt.where(ContentFeedbackItem.anchor_kind == anchor_kind)
        if exclude_states:
            stmt = stmt.where(
                ContentFeedbackItem.state.not_in(list(exclude_states))
            )
        if before is not None:
            before_created_at, before_id = before
            stmt = stmt.where(
                or_(
                    ContentFeedbackItem.created_at < before_created_at,
                    and_(
                        ContentFeedbackItem.created_at == before_created_at,
                        ContentFeedbackItem.id < before_id,
                    ),
                )
            )
        stmt = (
            stmt.order_by(
                ContentFeedbackItem.created_at.desc(),
                ContentFeedbackItem.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.execute(stmt).scalars().all())

    def count_feedback_items(
        self,
        *,
        content_item_id: uuid.UUID,
        state: str | None = None,
    ) -> int:
        """一条内容的反馈条数（预览页只要计数，不拉明细）。

        为什么不在预览页用 `list_feedback_items(limit=...)` 的 `len()`：
        预览页需要的是"一共多少条"，而列表窗口有上限；用窗口长度冒充总数
        会让前端在反馈超过一页时显示错误的计数。
        """
        stmt = select(func.count()).select_from(ContentFeedbackItem).where(
            ContentFeedbackItem.content_item_id == content_item_id
        )
        if state is not None:
            stmt = stmt.where(ContentFeedbackItem.state == state)
        return int(self._session.execute(stmt).scalar_one())

    def advance_feedback_state(
        self,
        *,
        feedback_item_id: uuid.UUID,
        expected_lock_version: int,
        to_state: str,
        reject_reason: str | None = None,
        resolved_version_id: uuid.UUID | None = None,
    ) -> int:
        """条件 UPDATE 迁移反馈项状态，返回受影响行数。

        **为什么用条件 UPDATE 而不是读-改-写**：`WHERE id = :id AND
        lock_version = :expected` 把"检查乐观锁"与"写入"压在同一条语句里。
        两个并发迁移只有一个能改成功（另一个 `rowcount = 0`），
        失败的那个**不会静默覆盖**已前进的状态。

        `reject_reason` / `resolved_version_id` **总是整列写入**：调用方须按
        **目标状态**给出完整取值（非 `REJECTED` 传 `None` 即清空）。
        这与 `content_reviews` 的不可变不同 —— 这里的状态列本就是"当前值"。

        **必须**随后 `reload_feedback_item()`：批量 UPDATE 用了
        `synchronize_session=False`，session 里的旧对象还带着旧状态。
        """
        result = self._session.execute(
            update(ContentFeedbackItem)
            .where(
                ContentFeedbackItem.id == feedback_item_id,
                ContentFeedbackItem.lock_version == expected_lock_version,
            )
            .values(
                state=to_state,
                reject_reason=reject_reason,
                resolved_version_id=resolved_version_id,
                lock_version=ContentFeedbackItem.lock_version + 1,
            ),
            execution_options={"synchronize_session": False},
        )
        return int(result.rowcount or 0)

    def reload_feedback_item(
        self, feedback_item_id: uuid.UUID
    ) -> ContentFeedbackItem | None:
        """强制从库重新读（`populate_existing=True`）。

        **为什么不用 `session.expire_all()`**：它会把调用方正在编辑的
        其它对象一起失效（WP17 ISS-53 的教训）。只刷这一个对象。
        """
        return self._session.get(
            ContentFeedbackItem, feedback_item_id, populate_existing=True
        )

    # ------------------------------------------------------------------
    # content_feedback_events（不可变，只追加）
    # ------------------------------------------------------------------

    def next_event_seq_no(self, feedback_item_id: uuid.UUID) -> int:
        """本反馈项的下一个事件序号（`max + 1`）。

        **同一事务内取**，并发撞车由 `uq_content_feedback_events_item_seq`
        兜住 —— 调用方用 `savepoint()` 包住"取号 + 插入"，撞车后重试即可
        （与 `content/repository.py next_version_no` 同款手法）。
        """
        stmt = select(func.coalesce(func.max(ContentFeedbackEvent.seq_no), 0)).where(
            ContentFeedbackEvent.feedback_item_id == feedback_item_id
        )
        return int(self._session.execute(stmt).scalar_one()) + 1

    def insert_feedback_event(
        self,
        *,
        feedback_item_id: uuid.UUID,
        from_state: str | None,
        to_state: str,
        actor_kind: str,
        note: str | None = None,
        actor_id: uuid.UUID | None = None,
        resulting_version_id: uuid.UUID | None = None,
    ) -> ContentFeedbackEvent:
        """追加一条状态迁移事件；`seq_no` 在写入前取号。"""
        row = ContentFeedbackEvent(
            feedback_item_id=feedback_item_id,
            from_state=from_state,
            to_state=to_state,
            note=note,
            actor_kind=actor_kind,
            actor_id=actor_id,
            resulting_version_id=resulting_version_id,
            seq_no=self.next_event_seq_no(feedback_item_id),
        )
        self._session.add(row)
        return row

    def list_feedback_events(
        self, feedback_item_id: uuid.UUID
    ) -> list[ContentFeedbackEvent]:
        """一条反馈的状态迁移历史，按 `seq_no` 升序（**不**按 `created_at`：
        同一事务里插入的多行时间戳相同，排不出唯一答案）。"""
        stmt = (
            select(ContentFeedbackEvent)
            .where(ContentFeedbackEvent.feedback_item_id == feedback_item_id)
            .order_by(ContentFeedbackEvent.seq_no)
        )
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # content_reviews（不可变，只追加）
    # ------------------------------------------------------------------

    def insert_review(self, **columns: Any) -> ContentReview:
        row = ContentReview(**columns)
        self._session.add(row)
        return row

    def flush(self) -> None:
        self._session.flush()


__all__ = ["FeedbackRepository"]
