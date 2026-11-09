"""Prompt 三张表的存储访问（WP23）。

**本层职责边界**：只做存储访问，不含业务规则判定。
"能不能激活"（版本属于本模板吗、工具超界吗）的判定在 `service.py`，
但**判定所需的数据必须由本层在同一事务内读出** ——
与 WP11 的 `VersionRepository` / WP18 的 `BriefRepository` /
WP20 的 `KnowledgeRepository` 同一条理由：若调用方先读一次、再传参进来，
两次读之间就有窗口，并发时能构造出"校验通过但写入时前提已变"。

**四条本层特有的约定**：

1. **裸 SQL 改状态后读回必须 `populate_existing=True`**（本项目高频坑第 3 条）。
   `prompt_templates.active_version_id` 走**条件 UPDATE**（为的是把"检查"与
   "写入"压在同一条语句里），它**不经过 ORM 属性赋值**，于是 identity map 里
   那个对象仍是旧值。不强制刷新的话，激活端点的响应会回带**旧的**
   `activeVersionId` —— 前端下一次提交必然失败，而它明明刚拿到这个值。

2. **jsonb 的写入一律走 SQLAlchemy 构造**（`update().values(...)` 传 Python 对象），
   **不用 `text()` 拼字面量**。本项目高频坑第 2 条：`text()` 会把
   `{"page":1}` 里的 `:1` 当绑定参数，报出莫名其妙的错误。
   凡必须用 `text()` 的地方，jsonb 参数一律 `CAST(:x AS jsonb)`。

3. **事件序号（`sequence`）用 `max + 1` 在同一事务内取**，
   靠 `UQ(template_id, sequence)` 兜住并发。**不能"先查再插"** ——
   两个并发激活请求都会读到同一个 max，然后一个撞唯一约束。
   本层负责把冲突抛成 `IntegrityError`，由 `service.publish` 捕获后重试
   （与 WP11 的 `register_version`、WP20 的 `publish` 同一手法）。

4. **不可变表（`prompt_versions` / `prompt_publications`）不提供任何
   update / delete 方法** —— 与 WP15 审计表、WP17 片段表、WP18 的
   `brief_versions`、WP20 的 `knowledge_versions` 同一口径：
   "想改也改不了"（触发器 + ORM 守卫）是第二道，本层不给入口是第一道。

**`knowledge_proposals` 的写入为什么在本层**：本包是它的**产生方**
（闭合 WP20 的 OP-150 一半）。**复用 WP20 的表但不复用它的 repository** ——
`KnowledgeRepository` 是"知识资产"的仓库，让它承担"由反馈产生候选"
这条**与资产无关**的写入路径，会让那个模块多一个它不需要知道的来源。
本层直接用 ORM 写那张表，代价是两处都能写它（可接受：写入路径不同、不重叠）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select, text, update
from sqlalchemy.orm import Session

from rednote.modules.knowledge.models import KnowledgeProposal
from rednote.modules.knowledge.types import (
    PROPOSAL_STATE_PENDING,
    SCOPE_PROJECT,
)
from rednote.modules.prompts.models import (
    PromptPublication,
    PromptTemplate,
    PromptVersion,
)


class PromptRepository:
    """Prompt 三张表（+ `knowledge_proposals` 的写入）的读写入口。

    事务边界由调用方（service / 端点）控制。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        """本仓库持有的 session（供下游在同一事务内造别的仓库）。

        与 `BriefRepository.session` / `KnowledgeRepository.session` 同一条理由：
        `GET /prompts` 的详情要一次给出模板、版本、激活历史 ——
        它们分属本模块的三张表与 WP11 的 `version_nodes`。
        不复用同一个 session 的话，同一个响应里的字段会来自不同事务。
        """
        return self._session

    def flush(self) -> None:
        """把待写变更立刻发给数据库。

        让约束错误在**预期位置**抛出（那里能给出可读的 4xx），
        而不是等到 `session_scope` 退出时提交才炸 —— 那时已经离开
        能给出可读错误的代码位置（WP12/WP13/WP16/WP18/WP20 的教训）。
        """
        self._session.flush()

    def now(self) -> datetime:
        return self._session.execute(select(func.now())).scalar_one()

    # ------------------------------------------------------------------
    # prompt_templates
    # ------------------------------------------------------------------

    def get_template(self, template_id: uuid.UUID) -> PromptTemplate | None:
        return self._session.get(PromptTemplate, template_id)

    def get_template_for_update(
        self, template_id: uuid.UUID
    ) -> PromptTemplate | None:
        """取模板并**加行锁**（`SELECT ... FOR UPDATE`）。

        激活是一个"读当前 active_version → 校验 → 改"的序列。
        两个并发激活请求若都读到同一个旧版本，就会各自写出一条
        `ACTIVATE` 事件 —— 序列号唯一约束会让其中一条失败，
        但**失败的那条已经把模板行改了**（同事务可回滚，因此不会脏写）。
        加锁让冲突在**取号之前**就串行化，避免无谓的重试。
        """
        return self._session.execute(
            select(PromptTemplate)
            .where(PromptTemplate.id == template_id)
            .with_for_update()
        ).scalar_one_or_none()

    def find_template_by_name(
        self, *, task_kind: str, name: str
    ) -> PromptTemplate | None:
        """按 `(task_kind, name)` 找模板。

        `UQ(task_kind, name)` 保证最多一条。用于"建模板时是否已存在同名"的判定 ——
        但**判定不靠它**（先查再插之间有窗口），靠唯一约束兜底。
        本方法的作用是让幂等路径（同名同 task_kind 时复用）能先命中。
        """
        return self._session.execute(
            select(PromptTemplate)
            .where(
                PromptTemplate.task_kind == task_kind,
                PromptTemplate.name == name,
            )
        ).scalar_one_or_none()

    def insert_template(
        self, *, task_kind: str, name: str
    ) -> PromptTemplate:
        template = PromptTemplate(task_kind=task_kind, name=name)
        self._session.add(template)
        self._session.flush()
        return template

    def set_active_version(
        self, *, template_id: uuid.UUID, version_id: uuid.UUID | None
    ) -> int:
        """条件 UPDATE 设置 `active_version_id`，返回受影响行数。

        **为什么用条件 UPDATE 而不是读-改-写**：
        `WHERE id = :id AND active_version_id IS DISTINCT FROM :new` 把
        "检查是否已经是指定值"与"写入"压在同一条语句里 —— 幂等重试
        （目标版本已是当前版本）会让 `rowcount = 0`，调用方据此返回
        `already_active=True`，而不是产生一条重复的激活事件。

        读回时 `populate_existing=True`：本语句不经过 ORM 属性赋值，
        identity map 里的对象仍是旧值（本模块 docstring 第 1 条）。
        """
        # `synchronize_session=False`：本语句刻意**不同步** identity map。
        # 同步它需要 SQLAlchemy 逐行回读（或按条件求值），而本表可能被
        # 并发改动 —— 让调用方显式走 `reload_template()` 拿权威值，
        # 比让 ORM 猜一个"看起来同步了"的中间态安全（本模块 docstring 第 1 条）。
        result = self._session.execute(
            update(PromptTemplate)
            .where(
                PromptTemplate.id == template_id,
                PromptTemplate.active_version_id.is_distinct_from(version_id),
            )
            .values(active_version_id=version_id),
            execution_options={"synchronize_session": False},
        )
        return int(result.rowcount or 0)

    def reload_template(self, template_id: uuid.UUID) -> PromptTemplate | None:
        """强制从库重新读（`populate_existing=True`）。

        **为什么不用 `session.expire_all()`**：它会把调用方正在编辑的
        其它对象一起失效（WP17 ISS-53 的教训）。只刷这一个对象。
        """
        return self._session.get(
            PromptTemplate, template_id, populate_existing=True
        )

    def list_templates(
        self,
        *,
        task_kind: str | None,
        limit: int,
        offset: int,
    ) -> list[PromptTemplate]:
        """按创建时间倒序翻页。`task_kind` 为空则不过滤。"""
        stmt: Select[tuple[PromptTemplate]] = select(PromptTemplate)
        if task_kind is not None:
            stmt = stmt.where(PromptTemplate.task_kind == task_kind)
        stmt = (
            stmt.order_by(PromptTemplate.created_at.desc(), PromptTemplate.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.execute(stmt).scalars().all())

    def count_templates(self, *, task_kind: str | None) -> int:
        stmt = select(func.count()).select_from(PromptTemplate)
        if task_kind is not None:
            stmt = stmt.where(PromptTemplate.task_kind == task_kind)
        return int(self._session.execute(stmt).scalar_one())

    # ------------------------------------------------------------------
    # prompt_versions
    # ------------------------------------------------------------------

    def get_version(self, version_id: uuid.UUID) -> PromptVersion | None:
        return self._session.get(PromptVersion, version_id)

    def insert_version(
        self,
        *,
        version_id: uuid.UUID,
        template_id: uuid.UUID,
        system_template: str,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any],
        allowed_tools: tuple[str, ...],
        change_reason: str,
        created_by: uuid.UUID | None,
    ) -> PromptVersion:
        """插入一个版本行。

        **`version_id` 由调用方给**（不是数据库生成）：它必须等于 WP11
        `register_version` 返回的 `node_id` —— 两行同事务插入且 `id` 相同，
        这是"注册表与具体表"的配对方式（数据结构 02 §5）。
        """
        row = PromptVersion(
            id=version_id,
            template_id=template_id,
            system_template=system_template,
            input_schema=input_schema,
            output_schema=output_schema,
            allowed_tools=list(allowed_tools),
            change_reason=change_reason,
            created_by=created_by,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def list_versions(
        self, *, template_id: uuid.UUID, limit: int, offset: int
    ) -> list[PromptVersion]:
        stmt = (
            select(PromptVersion)
            .where(PromptVersion.template_id == template_id)
            .order_by(PromptVersion.created_at.desc(), PromptVersion.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.execute(stmt).scalars().all())

    def count_versions(self, *, template_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(PromptVersion)
            .where(PromptVersion.template_id == template_id)
        )
        return int(self._session.execute(stmt).scalar_one())

    def latest_version(
        self, *, template_id: uuid.UUID
    ) -> PromptVersion | None:
        """本模板最近创建的一版。

        用于"激活时取上一版作为 `previous`"（判断是否新增了出网工具）。
        """
        stmt = (
            select(PromptVersion)
            .where(PromptVersion.template_id == template_id)
            .order_by(PromptVersion.created_at.desc(), PromptVersion.id.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def revision_no_of(self, version_id: uuid.UUID) -> int:
        """取某个版本节点的 `revision_no`。

        **从 `version_nodes` 读而不是从 `prompt_versions` 读**：
        `revision_no` 只存在于注册表（`prompt_versions` 没有这一列）——
        这是"注册表是修订号的唯一权威"的体现（WP11 §5）。

        **为什么放在仓储层而不是让各层自己查**：它是"读另一个模块的表"，
        属于存储访问。先前 `service` 与 `api` 各写了一份同样的 `text()`
        查询（本包 Code Review 时收敛到这里）—— 两处一旦漂移，
        同一响应里的 `revisionNo` 会来自两个不同的判据。
        `api` 层发裸 SQL 本身也是分层越界：端点不该知道表名。
        """
        return int(
            self._session.execute(
                text("SELECT revision_no FROM version_nodes WHERE id = :vid"),
                {"vid": version_id},
            ).scalar_one()
        )

    # ------------------------------------------------------------------
    # prompt_publications
    # ------------------------------------------------------------------

    def next_sequence(self, *, template_id: uuid.UUID) -> int:
        """本模板下一条发布事件的序号（`max + 1`）。

        **同一事务内取**，靠 `UQ(template_id, sequence)` 兜住并发。
        冲突时抛 `IntegrityError`，由 `service.publish` 重试。
        """
        stmt = select(func.coalesce(func.max(PromptPublication.sequence), 0)).where(
            PromptPublication.template_id == template_id
        )
        return int(self._session.execute(stmt).scalar_one()) + 1

    def insert_publication(
        self,
        *,
        template_id: uuid.UUID,
        version_id: uuid.UUID,
        action: str,
        sequence: int,
        superseded_version_id: uuid.UUID | None,
        affected_task_kinds: tuple[str, ...],
        actor_id: uuid.UUID | None,
        reason: str | None,
    ) -> PromptPublication:
        row = PromptPublication(
            template_id=template_id,
            version_id=version_id,
            action=action,
            sequence=sequence,
            superseded_version_id=superseded_version_id,
            affected_task_kinds=list(affected_task_kinds),
            actor_id=actor_id,
            reason=reason,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def list_publications(
        self, *, template_id: uuid.UUID, limit: int
    ) -> list[PromptPublication]:
        """按序号升序取事件流（投影的输入）。

        **按 `sequence` 而不是 `created_at`**：`now()` 在事务内是常量，
        同一事务插入的多条事件时间戳完全相同（WP17 ISS-52 踩过）。
        """
        stmt = (
            select(PromptPublication)
            .where(PromptPublication.template_id == template_id)
            .order_by(PromptPublication.sequence)
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # knowledge_proposals（本包是产生方，见模块 docstring）
    # ------------------------------------------------------------------

    def insert_proposal(
        self,
        *,
        target_asset_id: uuid.UUID | None,
        proposed_payload: dict[str, Any],
        scope: str,
        source_feedback_id: uuid.UUID | None,
    ) -> KnowledgeProposal:
        """写一条知识候选。

        `state` 固定为 `PENDING` —— 规格源 02 §165 原文"**AI 创建待确认，
        不直接生效**"。这是**唯一**的合法初始状态：候选永远不能直接生效，
        生效必须走 WP20 的 `decide_proposal`（ACCEPT）。

        ## ⚠️ `actor_id` **不能**写进 `confirmed_by`（本包实际踩过一次）

        WP20 的 `knowledge_proposals` 上有一对**双向** CHECK：

            state <> 'PENDING' OR confirmed_by IS NULL      -- PENDING 不许有确认人
            state  = 'PENDING' OR confirmed_by IS NOT NULL  -- 已处置必须有确认人

        初稿把 `actor_id` 写进了 `confirmed_by`（"谁产生的"当成"谁确认的"），
        结果是**任何一次候选产生都会撞 CHECK** —— 而且报错信息是
        `ck_knowledge_proposals_pending_has_no_confirmer`，
        指向的是 WP20 的约束而不是本包的代码。

        根因是把**两个不同的角色**混成了一列：
          · `confirmed_by` 是**确认人**（人工决策的落点，只在 ACCEPT/REJECT 时填）；
          · "谁产生了这条候选"是**产生方**，而 `knowledge_proposals`
            **没有这一列**（WP20 的表结构里没有 `created_by`）。

        **本包的处置**：`confirmed_by` 一律传 `None`。
        "谁产生的"这一信息由 `service.create_proposal` 的调用方（端点）
        从会话拿到，写进**审计日志**（WP15 的 `audit_events`）——
        那是"谁做了什么"的正确落点，而不是借用"确认人"这一列。

        代价（诚实记录）：候选表里查不到"谁产生的"。
        已登记为待定项（收口方向：WP20 的表补一列 `created_by`，
        或明确"候选的产生方只从审计日志查"）。
        """
        row = KnowledgeProposal(
            target_asset_id=target_asset_id,
            proposed_payload=proposed_payload,
            scope=scope,
            source_feedback_id=source_feedback_id,
            state=PROPOSAL_STATE_PENDING,
            # **必须为 None** —— 见本方法 docstring 的说明。
            confirmed_by=None,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get_proposal(self, proposal_id: uuid.UUID) -> KnowledgeProposal | None:
        return self._session.get(KnowledgeProposal, proposal_id)

    def default_proposal_scope(self) -> str:
        """候选的默认范围。

        单独一个方法而不是散写 `SCOPE_PROJECT`：将来范围取值变化时
        只有一处要改（避免同一常量在两处各写一遍，ISS-66 的形态）。
        """
        return SCOPE_PROJECT


__all__ = ["PromptRepository"]
