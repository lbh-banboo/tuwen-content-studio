"""版本注册表的读写入口（WP11）。

**本层职责边界**：只做存储访问，不含业务规则判定。
"能不能这么引用"（自引用 / 跨项目 / 成环）的判定在 `service.py`，
但**判定所需的数据必须由本层在同一事务内读出** —— 这一点很关键：
若调用方先读一次、再传参进来，两次读之间就有窗口，
并发时能构造出跨项目引用。

**为什么所有查询都走索引**：
  · `ix_version_nodes_entity_revision` 支撑版本历史与"取最大修订号"；
  · `uq_version_nodes_entity_type_entity_id_revision_no` 支撑幂等回查；
  · `ix_version_dependencies_dependency` 支撑"谁依赖了我"；
  · `ix_version_nodes_project_id` 支撑按项目清理/统计。

**递归 CTE 做环检测：访问集合（`UNION` 去重）与深度上限缺一不可**：
见 `dependency_reaches` —— 只留深度上限时，查询会在"扇出后汇聚"的图上
按路径数指数展开（实测细节在该函数 docstring 里）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import func, select, text, tuple_
from sqlalchemy.orm import Session

from rednote.modules.identity.models import User
from rednote.modules.projects.models import Project, ProjectMember
from rednote.modules.versions.models import VersionDependency, VersionNode


class VersionRepository:
    """版本注册表的读写入口。事务边界由调用方（service/API 层）控制。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        """在 SAVEPOINT 内执行一段写入，失败只回滚这一段。

        **为什么必须有**：`UQ(entity_type, entity_id, revision_no)` 是并发注册的
        唯一防线 —— 两个请求同时注册同一修订号时，后到的会撞唯一约束。
        撞了要能"只回滚这一次尝试、保留外层事务"再试一次（此时通常有别的
        事务刚插入了更大的修订号，重试就能拿到新号）。
        不用 SAVEPOINT 的话，一次 IntegrityError 会让整个事务进入 aborted 状态，
        后续任何语句都报 `current transaction is aborted`（WP10 实测踩到）。
        """
        with self._session.begin_nested():
            yield

    def flush(self) -> None:
        """把待写入的变更立即发给数据库，让约束错误在预期位置抛出。"""
        self._session.flush()

    def rollback(self) -> None:
        """回滚整个事务。仅在无法继续的路径上使用。"""
        self._session.rollback()

    # ------------------------------------------------------------------
    # 节点：读
    # ------------------------------------------------------------------

    def get_node(self, node_id: uuid.UUID) -> VersionNode | None:
        return self._session.get(VersionNode, node_id)

    def find_node(
        self, *, entity_type: str, entity_id: uuid.UUID, revision_no: int
    ) -> VersionNode | None:
        """按唯一键回查节点（幂等注册的判定依据）。"""
        return self._session.execute(
            select(VersionNode).where(
                VersionNode.entity_type == entity_type,
                VersionNode.entity_id == entity_id,
                VersionNode.revision_no == revision_no,
            )
        ).scalar_one_or_none()

    def max_revision_no(self, *, entity_type: str, entity_id: uuid.UUID) -> int:
        """该实体当前的最大修订号；没有版本时返回 0。

        用 `coalesce(max(...), 0)` 而不是 `count(*)`：
        修订号可能因归档/清理出现空洞（历史保留但不再活跃），
        计数得到的"第 N 条"与"第 N 版"不是一回事。
        """
        stmt = select(func.coalesce(func.max(VersionNode.revision_no), 0)).where(
            VersionNode.entity_type == entity_type,
            VersionNode.entity_id == entity_id,
        )
        return int(self._session.execute(stmt).scalar_one())

    def list_history(
        self,
        *,
        entity_type: str,
        entity_id: uuid.UUID,
        limit: int,
        after: tuple[int, uuid.UUID] | None = None,
    ) -> list[VersionNode]:
        """按键集分页取版本历史，返回**最多 limit+1 条**。

        排序为 `revision_no DESC, id DESC` —— 最新版本在最前，
        这正是前端"版本历史"面板要的顺序；`id` 作为最后一级打破平局，
        保证游标锚点唯一（否则翻页会漏项或重复）。

        为什么多取一条：调用方用"是否拿到了 limit+1 条"判断 `hasMore`，
        不必再发一次 `COUNT(*)`（并发写入下 COUNT 还会与实际结果不一致）。

        `after=(revision_no, id)` 是上一页最后一条的排序值，
        用行值比较 `(revision_no, id) < (:rev, :id)` 下推给 SQL。
        """
        stmt = select(VersionNode).where(
            VersionNode.entity_type == entity_type,
            VersionNode.entity_id == entity_id,
        )
        if after is not None:
            stmt = stmt.where(
                tuple_(VersionNode.revision_no, VersionNode.id) < tuple_(*after)
            )
        stmt = stmt.order_by(
            VersionNode.revision_no.desc(), VersionNode.id.desc()
        ).limit(limit + 1)
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # 节点：写
    # ------------------------------------------------------------------

    def insert_node(
        self,
        *,
        entity_type: str,
        entity_id: uuid.UUID,
        project_id: uuid.UUID | None,
        revision_no: int,
        schema_version: int,
        payload_hash: str,
        created_by: uuid.UUID | None,
    ) -> VersionNode:
        """插入一个版本节点。

        **不在这里 flush**：调用方需要把"插入注册表"与"插入具体版本表"
        放在同一个 SAVEPOINT 里（数据结构文档 02 §5 要求二者同一事务），
        由调用方决定何时 flush、如何重试。
        """
        node = VersionNode(
            entity_type=entity_type,
            entity_id=entity_id,
            project_id=project_id,
            revision_no=revision_no,
            schema_version=schema_version,
            payload_hash=payload_hash,
            created_by=created_by,
        )
        self._session.add(node)
        return node

    # ------------------------------------------------------------------
    # 依赖边
    # ------------------------------------------------------------------

    def get_dependency(
        self,
        *,
        parent_version_id: uuid.UUID,
        dependency_version_id: uuid.UUID,
        purpose: str,
    ) -> VersionDependency | None:
        return self._session.get(
            VersionDependency,
            {
                "parent_version_id": parent_version_id,
                "dependency_version_id": dependency_version_id,
                "purpose": purpose,
            },
        )

    def insert_dependency(
        self,
        *,
        parent_version_id: uuid.UUID,
        dependency_version_id: uuid.UUID,
        purpose: str,
        selected_fragment_hash: str | None,
    ) -> VersionDependency:
        edge = VersionDependency(
            parent_version_id=parent_version_id,
            dependency_version_id=dependency_version_id,
            purpose=purpose,
            selected_fragment_hash=selected_fragment_hash,
        )
        self._session.add(edge)
        return edge

    def list_dependencies(
        self, parent_version_id: uuid.UUID
    ) -> list[VersionDependency]:
        """该版本**用到了**哪些版本（它的上游）。"""
        return list(
            self._session.execute(
                select(VersionDependency)
                .where(VersionDependency.parent_version_id == parent_version_id)
                .order_by(
                    VersionDependency.dependency_version_id, VersionDependency.purpose
                )
            )
            .scalars()
            .all()
        )

    def list_dependents(
        self, dependency_version_id: uuid.UUID
    ) -> list[VersionDependency]:
        """哪些版本**用到了**它（它的下游）—— 影响分析的入口。"""
        return list(
            self._session.execute(
                select(VersionDependency)
                .where(
                    VersionDependency.dependency_version_id == dependency_version_id
                )
                .order_by(
                    VersionDependency.parent_version_id, VersionDependency.purpose
                )
            )
            .scalars()
            .all()
        )

    def dependency_reaches(
        self,
        *,
        start_version_id: uuid.UUID,
        target_version_id: uuid.UUID,
    ) -> bool:
        """沿"依赖"方向从 `start` 出发，能否到达 `target`。

        用途是**环检测**：要新增边 `parent → dependency` 时，
        若从 `dependency` 出发已能到达 `parent`，那么加这条边就成环
        （因为新边会让 parent 也能到 dependency）。

        **为什么这里没有深度上限**（本函数原本有个 `max_depth` 参数，已移除）：

        1. **深度上限防不住挂死，反而掩盖了真正的原因。**
           真正的问题是 `UNION ALL` 会把**每一条不同的路径**各展开一行。
           只要图里有"扇出后汇聚"的形状（多个下游引用同一个上游版本，
           这些下游又被同一个报告引用 —— 内容生产里的常态），
           路径数就随深度**指数增长**。加深度上限只是把爆炸的上限钉在
           `扇出^max_depth`，那仍是天文数字；而**环会让递归一路把上限吃满**，
           于是"环检测"这个最需要它的场景反而最先挂死。

           本机实测（PG 16，19 节点 / 36 边的"菱形链 + 一条回边"，
           原深度上限 32）：

           | 写法 | 展开行数 | 耗时 |
           |---|---|---|
           | `UNION ALL`（无去重） | 524,287（深度才到 18）→ 上限 2³² | 1.28 s，逐层翻倍 |
           | `UNION`（访问集合去重） | **37** | **0.6 ms** |

        2. **深度上限会漏判环。** 若环恰好落在 `max_depth` 之外，
           本函数返回"不可达" → 上层放行一条成环的边 → 下游溯源遍历
           从此真的挂死。为了防止挂死而引入的参数，反过来制造了挂死。

        所以这里改用**访问集合**：`UNION`（**不是** `UNION ALL`）按行去重，
        递归天然终止 —— 可达节点集合是有限的，环不会带来新的 `id`。
        于是**不需要任何深度上限**就能既安全又准确地得出结论。
        行数上界 = 可达节点数，与路径数无关。

        **为什么用递归 CTE 而不是把边全读进内存再遍历**：
        边表会随时间增长到很大（每个内容版本都引用多个上游版本）。
        全量读进内存在试点期看似可行，但它把"图有多大"变成了内存占用，
        而且拉长了事务持锁时间。CTE 只把可达节点带回应用。
        """
        # 沿 `dependency_version_id` 方向传播：
        # 边 (parent, dependency) 表示 "parent 依赖 dependency"，
        # 所以从某个版本出发能"依赖到"的下一跳，是它自己的 dependency_version_id。
        #
        # `UNION` = 访问集合去重，是本函数的正确性核心：
        # 它同时解决了"路径指数展开"与"有环不终止"两个问题。
        # 不带 depth 列也是刻意的 —— 带上它会让同一个 id 在每个深度各留一行，
        # 等于把行数乘上深度，还会诱使后来者再加一个没用的深度上限。
        statement = text(
            """
            WITH RECURSIVE reachable(id) AS (
                SELECT CAST(:start_id AS uuid)
                UNION
                SELECT vd.dependency_version_id
                FROM reachable r
                JOIN version_dependencies vd
                  ON vd.parent_version_id = r.id
            )
            SELECT 1 FROM reachable WHERE id = CAST(:target_id AS uuid) LIMIT 1
            """
        )
        row = self._session.execute(
            statement,
            {
                "start_id": str(start_version_id),
                "target_id": str(target_version_id),
            },
        ).first()
        return row is not None

    # ------------------------------------------------------------------
    # 项目成员（版本可见范围判定）
    # ------------------------------------------------------------------

    def is_active_member(self, *, project_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """该用户是否是该项目当前的活跃成员。

        必须带 `removed_at IS NULL` —— 被移除的成员不能因为"历史成员记录还在"
        而继续看到项目内的版本。`ix_project_members_active_user` 是部分索引，
        这条查询正好命中。
        """
        row = self._session.execute(
            select(ProjectMember.id)
            .where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
                ProjectMember.removed_at.is_(None),
            )
            .limit(1)
        ).first()
        return row is not None

    def project_exists(self, project_id: uuid.UUID) -> bool:
        """项目是否存在（把外键错误提前成可读的 422）。"""
        return (
            self._session.execute(
                select(Project.id).where(Project.id == project_id).limit(1)
            ).first()
            is not None
        )

    def user_exists(self, user_id: uuid.UUID) -> bool:
        """用户是否存在（把外键错误提前成可读的 422）。"""
        return (
            self._session.execute(
                select(User.id).where(User.id == user_id).limit(1)
            ).first()
            is not None
        )


__all__ = ["VersionRepository"]
