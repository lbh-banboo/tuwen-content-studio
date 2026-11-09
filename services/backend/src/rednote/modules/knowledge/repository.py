"""知识五张表的存储访问（WP20）。

**本层职责边界**：只做存储访问，不含业务规则判定。
"能不能发布"（版本属于本 asset 吗、重复归档吗、必填字段齐吗）的判定在
`service.py`，但**判定所需的数据必须由本层在同一事务内读出** ——
这一点与 WP11 的 `VersionRepository` / WP18 的 `BriefRepository` 同一条理由：
若调用方先读一次、再传参进来，两次读之间就有窗口，
并发时能构造出"校验通过但写入时前提已变"。

**三条本层特有的约定**：

1. **裸 SQL 改状态后读回必须 `populate_existing=True`**（本项目高频坑第 3 条）。
   `knowledge_assets` 的 `active_version_id` / `archived_at` 走
   **条件 UPDATE**（为的是把"检查"与"写入"压在同一条语句里），
   它**不经过 ORM 属性赋值**，于是 identity map 里那个对象仍是旧值。
   不强制刷新的话，发布端点的响应会回带**旧的** `activeVersionId` ——
   前端下一次提交必然失败，而它明明刚拿到这个值。

2. **jsonb 的写入一律走 SQLAlchemy 构造**（`update().values(...)` 传 Python 对象），
   **不用 `text()` 拼字面量**。本项目高频坑第 2 条：`text()` 会把
   `{"page":1}` 里的 `:1` 当绑定参数，报出莫名其妙的错误。
   凡必须用 `text()` 的地方，jsonb 参数一律 `CAST(:x AS jsonb)`。

3. **事件序号（`sequence`）用 `max + 1` 在同一事务内取**，
   靠 `UQ(asset_id, sequence)` 兜住并发。**不能"先查再插"** ——
   两个并发发布请求都会读到同一个 max，然后一个撞唯一约束。
   本层负责把冲突抛成 `IntegrityError`，由 `service.publish` 捕获后重试
   （与 WP11 的 `register_version` 同一手法）。

**不可变表（`knowledge_versions` / `knowledge_publications`）不提供任何
update / delete 方法** —— 与 WP15 审计表、WP17 片段表、WP18 的
`brief_versions` 同一口径："想改也改不了"（触发器 + ORM 守卫）是第二道，
本层不给入口是第一道。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.orm import Session

from rednote.modules.knowledge.models import (
    KnowledgeAsset,
    KnowledgeChunk,
    KnowledgeProposal,
    KnowledgePublication,
    KnowledgeVersion,
)
from rednote.modules.knowledge.types import (
    PROPOSAL_STATE_PENDING,
)


class KnowledgeRepository:
    """知识五张表的读写入口。事务边界由调用方（service / 端点）控制。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        """本仓库持有的 session（供下游在同一事务内造别的仓库）。

        与 `BriefRepository.session` / `JobRepository.session` 同一条理由：
        `GET /knowledge/assets/{id}` 要一次给出资产、版本、发布历史、
        以及引用摘要 —— 它们分属本模块的表与 WP11 的表。
        不复用同一个 session 的话，同一个响应里的字段会来自不同事务。
        """
        return self._session

    def flush(self) -> None:
        """把待写变更立刻发给数据库。

        让约束错误在**预期位置**抛出（那里能给出可读的 4xx），
        而不是等到 `session_scope` 退出时提交才炸 —— 那时已经离开
        能给出可读错误的代码位置（WP12/WP13/WP16/WP18 的教训）。
        """
        self._session.flush()

    def now(self) -> datetime:
        return self._session.execute(select(func.now())).scalar_one()

    # ------------------------------------------------------------------
    # knowledge_assets
    # ------------------------------------------------------------------

    def get_asset(self, asset_id: uuid.UUID) -> KnowledgeAsset | None:
        return self._session.get(KnowledgeAsset, asset_id)

    def get_asset_for_update(self, asset_id: uuid.UUID) -> KnowledgeAsset | None:
        """取 asset 并**加行锁**（`SELECT ... FOR UPDATE`）。

        规格源 §277 原文第一句："**锁定 `knowledge_asset`**"。
        锁的必要性：发布是一个"读当前 active_version → 校验 → 改"的序列，
        两个并发发布请求若都读到同一个旧版本，就会各自写出一条
        `ACTIVATE` 事件 —— 序列号唯一约束会让其中一条失败，
        但**失败的那条已经把 asset 行改了**（同事务可回滚，因此不会脏写）。
        加锁让冲突在**取号之前**就串行化，避免无谓的重试。
        """
        return self._session.execute(
            select(KnowledgeAsset)
            .where(KnowledgeAsset.id == asset_id)
            .with_for_update()
        ).scalar_one_or_none()

    def insert_asset(
        self,
        *,
        category: str,
        title: str,
        scope: str,
        brand_id: uuid.UUID | None,
        product_id: uuid.UUID | None,
        project_id: uuid.UUID | None,
        created_by: uuid.UUID | None,
    ) -> KnowledgeAsset:
        row = KnowledgeAsset(
            category=category,
            title=title,
            scope=scope,
            brand_id=brand_id,
            product_id=product_id,
            project_id=project_id,
            created_by=created_by,
        )
        self._session.add(row)
        return row

    def set_active_version(
        self, *, asset_id: uuid.UUID, version_id: uuid.UUID | None
    ) -> None:
        """设置生效版本投影列（**条件 UPDATE + `populate_existing` 读回**）。

        用 SQLAlchemy 的 `update()` 而不是 ORM 属性赋值：
        属性赋值会走 mapper 的 flush 流程，而那会触发 `updated_at` 的
        onupdate 与 identity map 的联动 —— 对"一列投影被服务端设置"
        这件事而言，直接 UPDATE 语义更清楚（与 `briefs.active_version_id`
        的更新方式一致）。
        """
        self._session.execute(
            update(KnowledgeAsset)
            .where(KnowledgeAsset.id == asset_id)
            .values(active_version_id=version_id)
        )
        self.refresh_asset(asset_id)

    def set_archived(
        self, *, asset_id: uuid.UUID, archived_at: datetime | None
    ) -> None:
        """设置 / 清空归档时间戳（同上，条件 UPDATE）。"""
        self._session.execute(
            update(KnowledgeAsset)
            .where(KnowledgeAsset.id == asset_id)
            .values(archived_at=archived_at)
        )
        self.refresh_asset(asset_id)

    def refresh_asset(self, asset_id: uuid.UUID) -> KnowledgeAsset | None:
        """从库里重读一行，**强制覆盖 identity map**（本项目高频坑第 3 条）。

        `populate_existing=True` 是这里的关键：没有它，上面两条
        `update()` 之后 `session.get()` 会**直接返回内存里的旧对象**
        （identity map 命中），于是端点响应回带旧值。
        症状是"发布成功了，但响应说还是旧版本" —— 而库里是对的，
        所以排障时会先怀疑前端。
        """
        return self._session.get(
            KnowledgeAsset, asset_id, populate_existing=True
        )

    def list_assets(
        self,
        *,
        category: str | None = None,
        brand_id: uuid.UUID | None = None,
        product_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        title_query: str | None = None,
        include_archived: bool = False,
    ) -> list[KnowledgeAsset]:
        """全员可查的知识摘要列表（筛选下推到 SQL）。

        **排序 `(created_at DESC, id DESC)`**：`created_at` 单独作排序键
        不唯一（同一事务插入的多行时间戳相同），而游标分页要求锚点唯一
        （`platform/pagination.py` 的说明）。id 作为最后一级打破平局。

        **`include_archived=False` 时排除归档知识**：默认列表只显示
        "还在用"的知识。归档仍可**按 id 直读**（`get_asset`），
        这是验收原文「归档可追溯」的要求。

        **为什么筛选在 SQL 而不是内存**：知识库会增长到几千条，
        而 `platform.pagination.paginate_items` 是内存分页。
        本方法只负责"取出符合条件的一批"，分页由 service 用
        游标在**这一批**上做 —— 对当前规模（几千条）够用，
        且不必先把全部行拉进内存（见说明文档的方案取舍）。
        """
        stmt: Select[Any] = select(KnowledgeAsset)
        stmt = self._apply_asset_filters(
            stmt,
            category=category,
            brand_id=brand_id,
            product_id=product_id,
            project_id=project_id,
            title_query=title_query,
            include_archived=include_archived,
        )
        return list(
            self._session.execute(
                stmt.order_by(
                    KnowledgeAsset.created_at.desc(), KnowledgeAsset.id.desc()
                )
            )
            .scalars()
            .all()
        )

    def count_assets(
        self,
        *,
        category: str | None = None,
        brand_id: uuid.UUID | None = None,
        product_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        title_query: str | None = None,
        include_archived: bool = False,
    ) -> int:
        """同一组筛选条件下的总数。

        **为什么需要它**：`GET /knowledge/assets` 的 `state` 筛选
        （如 `state=INDEXING`）**无法下推到 SQL** —— 它是投影算出来的
        （见 `projection.py`）。因此那条路径要"取一批 → 投影 → 按 state 过滤"，
        而在过滤后才知道总数。测试用它断言"筛选条件本身是下推的"。
        """
        stmt = select(func.count()).select_from(KnowledgeAsset)
        stmt = self._apply_asset_filters(
            stmt,
            category=category,
            brand_id=brand_id,
            product_id=product_id,
            project_id=project_id,
            title_query=title_query,
            include_archived=include_archived,
        )
        return int(self._session.execute(stmt).scalar_one())

    def _apply_asset_filters(
        self,
        stmt: Any,
        *,
        category: str | None,
        brand_id: uuid.UUID | None,
        product_id: uuid.UUID | None,
        project_id: uuid.UUID | None,
        title_query: str | None,
        include_archived: bool,
    ) -> Any:
        """把筛选条件下推到语句。

        **`title_query` 用 `ilike` 而不是 `pg_trgm`**：规格源 §14 的索引计划
        提到"名称 pg_trgm"，但 `pg_trgm` 是一个**扩展**（`CREATE EXTENSION`），
        而扩展的创建属于迁移（主 agent 的权限，且会影响共享库）。
        本包**不建扩展**，改用 `ilike '%x%'` —— 在当前的几千条规模下
        顺序扫描完全够用。升级到 pg_trgm 的路径已登记为待定项。
        """
        if category is not None:
            stmt = stmt.where(KnowledgeAsset.category == category)
        if brand_id is not None:
            stmt = stmt.where(KnowledgeAsset.brand_id == brand_id)
        if product_id is not None:
            stmt = stmt.where(KnowledgeAsset.product_id == product_id)
        if project_id is not None:
            stmt = stmt.where(KnowledgeAsset.project_id == project_id)
        if title_query:
            stmt = stmt.where(
                KnowledgeAsset.title.ilike(f"%{_escape_like(title_query)}%", escape="\\")
            )
        if not include_archived:
            stmt = stmt.where(KnowledgeAsset.archived_at.is_(None))
        return stmt

    # ------------------------------------------------------------------
    # knowledge_versions（不可变：只读 + 插入）
    # ------------------------------------------------------------------

    def get_version(self, version_id: uuid.UUID) -> KnowledgeVersion | None:
        return self._session.get(KnowledgeVersion, version_id)

    def insert_version(
        self,
        *,
        version_id: uuid.UUID,
        asset_id: uuid.UUID,
        body: str,
        structured_payload: dict[str, Any],
        source_segment_ids: tuple[uuid.UUID, ...],
        change_reason: str,
        effective_from: datetime | None,
        effective_until: datetime | None,
        created_by: uuid.UUID | None,
    ) -> KnowledgeVersion:
        """插入版本行。**`version_id` 由调用方给定** —— 它就是
        `register_version` 返回的 `node_id`（注册表与具体表同 id，
        数据结构 02 §5 的要求）。"""
        row = KnowledgeVersion(
            id=version_id,
            asset_id=asset_id,
            body=body,
            structured_payload=structured_payload,
            source_segment_ids=list(source_segment_ids),
            change_reason=change_reason,
            effective_from=effective_from,
            effective_until=effective_until,
            created_by=created_by,
        )
        self._session.add(row)
        return row

    def list_versions(self, asset_id: uuid.UUID) -> list[KnowledgeVersion]:
        """按 `created_at DESC, id DESC` 排序 —— **顺序必须确定**。

        `created_at` 是事务时间戳，同一批插入的行值相同（WP17 的 ISS-52
        实测：三行读回来顺序随机）。不显式排序的话，前端每次刷新
        看到的版本顺序都可能不同。
        """
        return list(
            self._session.execute(
                select(KnowledgeVersion)
                .where(KnowledgeVersion.asset_id == asset_id)
                .order_by(
                    KnowledgeVersion.created_at.desc(), KnowledgeVersion.id.desc()
                )
            )
            .scalars()
            .all()
        )

    def count_versions(self, asset_id: uuid.UUID) -> int:
        return int(
            self._session.execute(
                select(func.count())
                .select_from(KnowledgeVersion)
                .where(KnowledgeVersion.asset_id == asset_id)
            ).scalar_one()
        )

    # ------------------------------------------------------------------
    # knowledge_publications（不可变：只读 + 插入）
    # ------------------------------------------------------------------

    def list_publications(self, asset_id: uuid.UUID) -> list[KnowledgePublication]:
        """按 `sequence` 升序 —— **投影的输入顺序**（见 `projection.py`）。"""
        return list(
            self._session.execute(
                select(KnowledgePublication)
                .where(KnowledgePublication.asset_id == asset_id)
                .order_by(KnowledgePublication.sequence.asc())
            )
            .scalars()
            .all()
        )

    def next_publication_sequence(self, asset_id: uuid.UUID) -> int:
        """下一个事件序号（`max + 1`，从 1 开始）。

        **并发安全靠 `UQ(asset_id, sequence)`**，不靠这个函数 ——
        它只给出"当前看到的 max"。两个并发调用会拿到同一个值，
        其中一个撞唯一约束，由 `service.publish` 重试。
        （与 WP11 的 `max_revision_no` + 唯一约束重试同一手法。）
        """
        current = self._session.execute(
            select(func.max(KnowledgePublication.sequence)).where(
                KnowledgePublication.asset_id == asset_id
            )
        ).scalar_one_or_none()
        return int(current or 0) + 1

    def insert_publication(
        self,
        *,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        action: str,
        sequence: int,
        actor_id: uuid.UUID | None,
        reason: str | None,
    ) -> KnowledgePublication:
        row = KnowledgePublication(
            asset_id=asset_id,
            version_id=version_id,
            action=action,
            sequence=sequence,
            actor_id=actor_id,
            reason=reason,
        )
        self._session.add(row)
        return row

    # ------------------------------------------------------------------
    # knowledge_chunks（本包只建表，不生产 —— 读接口留给 WP21 的投递器自查）
    # ------------------------------------------------------------------

    def count_chunks(self, version_id: uuid.UUID) -> int:
        """某版本的 chunk 数。

        **本包唯一的 chunk 接口，且只读。** 它存在的理由是
        `GET /knowledge/assets/{id}` 要能显示"已生效／索引处理中" ——
        而"索引好了没有"在**本包能观察到的范围内**没有直接信号
        （索引由 WP21 建、Outbox 由 WP13 投递）。用"有没有 chunk"
        作为**辅助**信号，主信号是 Outbox 事件的投递状态
        （见 `service.asset_view` 的说明）。
        """
        return int(
            self._session.execute(
                select(func.count())
                .select_from(KnowledgeChunk)
                .where(KnowledgeChunk.knowledge_version_id == version_id)
            ).scalar_one()
        )

    # ------------------------------------------------------------------
    # knowledge_proposals
    # ------------------------------------------------------------------

    def get_proposal(self, proposal_id: uuid.UUID) -> KnowledgeProposal | None:
        return self._session.get(KnowledgeProposal, proposal_id)

    def get_proposal_for_update(
        self, proposal_id: uuid.UUID
    ) -> KnowledgeProposal | None:
        """取提案并加行锁 —— 决策是一个"读状态 → 校验 → 改"的序列，
        两个并发决策（一人 ACCEPT、一人 REJECT）必须串行化。"""
        return self._session.execute(
            select(KnowledgeProposal)
            .where(KnowledgeProposal.id == proposal_id)
            .with_for_update()
        ).scalar_one_or_none()

    def list_proposals(
        self, *, state: str | None = None, project_id: uuid.UUID | None = None
    ) -> list[KnowledgeProposal]:
        """AI 候选变更列表（按创建时间倒序，id 打破平局）。"""
        stmt: Select[Any] = select(KnowledgeProposal)
        if state is not None:
            stmt = stmt.where(KnowledgeProposal.state == state)
        if project_id is not None:
            stmt = stmt.where(KnowledgeProposal.project_id == project_id)
        return list(
            self._session.execute(
                stmt.order_by(
                    KnowledgeProposal.created_at.desc(), KnowledgeProposal.id.desc()
                )
            )
            .scalars()
            .all()
        )

    def insert_proposal(
        self,
        *,
        project_id: uuid.UUID | None,
        target_asset_id: uuid.UUID | None,
        proposed_payload: dict[str, Any],
        scope: str,
        source_feedback_id: uuid.UUID | None = None,
        source_experiment_id: uuid.UUID | None = None,
    ) -> KnowledgeProposal:
        """插入一条候选。**状态固定为 PENDING** ——
        "AI 创建待确认，不直接生效"（规格源 02 §8 备注）。

        `state` 不明文写 `PROPOSAL_STATE_PENDING` 的字符串而是引用常量：
        这个值在 `models.py` 的 CHECK 与查询过滤里各出现一次，
        散写三处迟早漂移。
        """
        row = KnowledgeProposal(
            project_id=project_id,
            target_asset_id=target_asset_id,
            proposed_payload=proposed_payload,
            scope=scope,
            state=PROPOSAL_STATE_PENDING,
            source_feedback_id=source_feedback_id,
            source_experiment_id=source_experiment_id,
        )
        self._session.add(row)
        return row

    def settle_proposal(
        self,
        *,
        proposal_id: uuid.UUID,
        state: str,
        confirmed_by: uuid.UUID,
        decision_reason: str | None,
        decided_at: datetime,
    ) -> None:
        """写入决策结果（条件 UPDATE + `populate_existing` 读回）。"""
        self._session.execute(
            update(KnowledgeProposal)
            .where(KnowledgeProposal.id == proposal_id)
            .values(
                state=state,
                confirmed_by=confirmed_by,
                decision_reason=decision_reason,
                decided_at=decided_at,
            )
        )
        self._session.get(KnowledgeProposal, proposal_id, populate_existing=True)

    def count_proposals(self, *, state: str | None = None) -> int:
        stmt = select(func.count()).select_from(KnowledgeProposal)
        if state is not None:
            stmt = stmt.where(KnowledgeProposal.state == state)
        return int(self._session.execute(stmt).scalar_one())

    # ------------------------------------------------------------------
    # 跨模块只读：字典与项目是否存在
    # ------------------------------------------------------------------
    # **本层不 import projects / identity 的模型** —— 与 WP18 的
    # `BriefRepository.project_exists` 同一手法：用一条 `SELECT 1` 判存在，
    # 而不是把别人的 ORM 类拉进来。理由是本包的 FK 校验只需要"存在性"，
    # 而 import 别人的模型会让"WP10 改一个列名"变成"本模块 import 失败"。

    def brand_exists(self, brand_id: uuid.UUID) -> bool:
        from sqlalchemy import text

        return (
            self._session.execute(
                text("select 1 from brands where id = :id"), {"id": brand_id}
            ).first()
            is not None
        )

    def product_exists(self, product_id: uuid.UUID) -> bool:
        from sqlalchemy import text

        return (
            self._session.execute(
                text("select 1 from products where id = :id"), {"id": product_id}
            ).first()
            is not None
        )

    def project_exists(self, project_id: uuid.UUID) -> bool:
        from sqlalchemy import text

        return (
            self._session.execute(
                text("select 1 from projects where id = :id"), {"id": project_id}
            ).first()
            is not None
        )

    def user_exists(self, user_id: uuid.UUID) -> bool:
        from sqlalchemy import text

        return (
            self._session.execute(
                text("select 1 from users where id = :id"), {"id": user_id}
            ).first()
            is not None
        )

    def list_live_assets_for_fact_lookup(
        self, *, product_id: uuid.UUID, category: str
    ) -> list[KnowledgeAsset]:
        """**事实精确读取**（规格源 §14 明写本包的索引计划）：

        > | 事实精确读取 | `knowledge_assets(product_id,category,active_version_id)` |

        规格源 §277 末句的要求据此落地：

        > 重要事实可以**从数据库精确获取**；不能因索引延迟读取错误旧事实。

        **它必须不经过任何 embedding / 向量索引** —— 这条路径的答案
        只在关系表里：命中的行给出 `active_version_id`，再按 id 取
        `knowledge_versions`（不可变，因此读到的必然是"发布那一刻定下的"
        内容，不会因为索引重建而变）。

        只取"有生效版本且未归档"的行 —— 与部分索引
        `ix_knowledge_assets_fact_lookup` 的 `WHERE` 完全一致。
        """
        return list(
            self._session.execute(
                select(KnowledgeAsset)
                .where(
                    KnowledgeAsset.product_id == product_id,
                    KnowledgeAsset.category == category,
                    KnowledgeAsset.active_version_id.is_not(None),
                    KnowledgeAsset.archived_at.is_(None),
                )
                .order_by(KnowledgeAsset.id.asc())
            )
            .scalars()
            .all()
        )

    def count_archived(self) -> int:
        """已归档的 asset 数。**测试用它做基线差分**（不是"全表 0 行"）——

        表里有别的包的合法遗留与本次运行之前的数据，
        "全表 0 行"这个判据在任何非空库上都会假失败（交接文档 §9）。
        """
        return int(
            self._session.execute(
                select(func.count())
                .select_from(KnowledgeAsset)
                .where(KnowledgeAsset.archived_at.is_not(None))
            ).scalar_one()
        )

    def state_counts(self) -> dict[str, int]:
        """按 `archived_at` 分组的计数（诊断用，**不涉及投影**）。"""
        rows = self._session.execute(
            select(
                func.count().label("total"),
                func.count()
                .filter(KnowledgeAsset.archived_at.is_not(None))
                .label("archived"),
            )
        ).one()
        return {"total": int(rows.total), "archived": int(rows.archived)}


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符。

    **不做这件事就是一个注入式的 bug**：用户搜 `50%` 时那个 `%`
    会被 LIKE 当成"任意字符"，于是搜出所有以 `50` 开头的东西；
    用户搜 `a_b` 时 `_` 会匹配任意单字符。两者都不会报错，
    只会给出**看起来合理但错误**的结果 —— 而用户无法察觉。

    与 `escape="\\"` 配对使用（见 `_apply_asset_filters`）。
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


__all__ = ["KnowledgeRepository"]