"""项目领域仓储层（WP09 建立，WP10 扩展）。

**本层职责边界**：把"存储细节"翻译成"领域事实"。
权限判定本身是纯函数（`permissions.py`），仓储只负责查数据、写数据。

**WP10 新增的三件事**：
1. **键集（keyset）分页下推 SQL** —— 项目数量按需求 §2.3 不设业务上限，
   `OFFSET` 分页在并发写入下会漏项/重复，必须下推成
   `WHERE (due_date, id) > (:due, :id)`。
2. **带版本条件的更新** —— `update_*_versioned` 把 `lock_version` 放进
   `UPDATE ... WHERE` 而不是只在校验阶段比对。只在应用层比对会有
   "校验通过到写入之间"的窗口；把版本写进 WHERE 才是原子的。
3. **字典的引用计数** —— 归档品牌/产品前要确认没有项目在用。

**为什么所有查询都依赖索引**：
- `ix_project_members_active_user`（部分索引，WHERE removed_at IS NULL）
  支撑"查某用户在某项目的活跃成员关系"。
- `ix_projects_phase_due_date` 支撑项目列表的排序。
- `uq_project_members_active_owner` 是"每项目活跃负责人唯一"的**数据库级**保证。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select, tuple_, update
from sqlalchemy.orm import Session

from rednote.modules.projects.models import (
    EXTERNAL_POLICY_LOCAL_ONLY,
    MEMBER_COLLABORATOR,
    MEMBER_OWNER,
    PHASE_PREPARATION,
    Brand,
    Product,
    Project,
    ProjectAssignment,
    ProjectMember,
)
from rednote.modules.projects.permissions import MembershipInfo

# 项目列表的默认时区（与 projects.timezone 的 server_default 一致）。
DEFAULT_PROJECT_TIMEZONE = "Asia/Shanghai"


def _escape_like(value: str) -> str:
    """转义 LIKE 的通配符。

    不做转义的话，用户搜 "100%" 会变成"以 100 开头的任意串"，
    搜 "_" 会匹配任意单字符 —— 搜索结果看起来"很智能"，实际是错的。
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ProjectRepository:
    """项目的读写入口。

    构造时接收 Session：事务边界由调用方（API 层）控制。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        """在 SAVEPOINT 内执行一段写入，失败只回滚这一段。

        **为什么需要**：项目编号是随机生成的，仍有极小概率与既有编号相撞。
        撞了要能"只回滚这一次尝试、保留外层事务"再试一次。
        若不用 SAVEPOINT，一次 IntegrityError 会让整个事务进入 aborted 状态，
        后续任何语句都报 `current transaction is aborted` —— 重试无从谈起。
        """
        with self._session.begin_nested():
            yield

    def flush(self) -> None:
        """把待写入的变更立即发给数据库，让约束错误在预期位置抛出。"""
        self._session.flush()

    # ------------------------------------------------------------------
    # 项目：读
    # ------------------------------------------------------------------

    def get_project(self, project_id: uuid.UUID) -> Project | None:
        return self._session.get(Project, project_id)

    def list_projects_filtered(
        self,
        *,
        restrict_to_user_id: uuid.UUID | None,
        limit: int,
        phase: str | None = None,
        owner_id: uuid.UUID | None = None,
        keyword: str | None = None,
        after: tuple[date, uuid.UUID] | None = None,
    ) -> list[Project]:
        """按键集分页取项目，返回**最多 limit+1 条**。

        为什么多取一条：调用方用"是否拿到了 limit+1 条"判断 `hasMore`，
        这样不必再发一次 `COUNT(*)`（在并发写入下 COUNT 还会与实际结果不一致）。

        `restrict_to_user_id=None` 表示不限可见范围（管理者视角）。
        非 None 时必须传入**该用户 ID**，且只返回他是活跃成员的项目 ——
        "看不到别人的项目"这条隔离规则由 SQL 保证，不依赖调用方记得过滤。

        `after=(due_date, id)` 是上一页最后一条的排序值。
        用行值比较 `(due_date, id) > (:due, :id)` 而不是
        `due_date > :due OR (due_date = :due AND id > :id)`：
        前者能直接用复合索引，后者在 PG 里通常也能优化但可读性差且容易写错括号。
        """
        stmt = select(Project)

        if restrict_to_user_id is not None:
            stmt = stmt.join(
                ProjectMember, ProjectMember.project_id == Project.id
            ).where(
                ProjectMember.user_id == restrict_to_user_id,
                # 只认活跃成员关系：被移除后立即失效（与 WP09 同一原则）。
                ProjectMember.removed_at.is_(None),
            )

        if phase is not None:
            stmt = stmt.where(Project.phase == phase)
        if owner_id is not None:
            stmt = stmt.where(Project.owner_id == owner_id)
        if keyword:
            pattern = f"%{_escape_like(keyword)}%"
            stmt = stmt.where(
                or_(
                    Project.name.ilike(pattern, escape="\\"),
                    # 展示编号也参与搜索：运营通常拿着编号来查。
                    Project.project_code.ilike(pattern, escape="\\"),
                )
            )

        # 排序：截止日期升序 + id 升序（id 打破平局，保证游标锚点唯一）。
        stmt = stmt.order_by(Project.due_date, Project.id)

        if after is not None:
            after_due, after_id = after
            stmt = stmt.where(
                tuple_(Project.due_date, Project.id) > tuple_(after_due, after_id)
            )

        return list(self._session.execute(stmt.limit(limit + 1)).scalars().all())

    # ------------------------------------------------------------------
    # 项目：写
    # ------------------------------------------------------------------

    def create_project(
        self,
        *,
        project_code: str,
        name: str,
        brand_id: uuid.UUID,
        product_id: uuid.UUID,
        owner_id: uuid.UUID,
        target_delivery_count: int,
        due_date: date,
        target_publication_count: int | None = None,
        external_policy: str = EXTERNAL_POLICY_LOCAL_ONLY,
        description: str | None = None,
        timezone_name: str = DEFAULT_PROJECT_TIMEZONE,
    ) -> Project:
        """新建项目。

        **显式传 phase 与 timezone**（而不是依赖 server_default）：
        依赖默认值时，插入后 ORM 对象上这两个属性仍是 None，
        必须 refresh 才能读到 —— 那会让"创建后立即返回完整对象"
        多一次查询，且容易漏。
        """
        project = Project(
            project_code=project_code,
            name=name,
            brand_id=brand_id,
            product_id=product_id,
            owner_id=owner_id,
            phase=PHASE_PREPARATION,
            target_delivery_count=target_delivery_count,
            target_publication_count=target_publication_count,
            due_date=due_date,
            timezone=timezone_name,
            external_policy=external_policy,
            description=description,
        )
        self._session.add(project)
        self._session.flush()
        return project

    def update_project_versioned(
        self,
        project: Project,
        *,
        expected_version: int,
        values: dict[str, Any],
    ) -> bool:
        """带版本条件的字段更新。返回 False 表示版本已变（未写入）。

        **版本条件写在 WHERE 里**，不是在 Python 里比对 —— 后者存在
        "读到旧版本 → 校验通过 → 另一个请求先写入 → 本请求覆盖"的窗口。
        写进 WHERE 后，PG 保证"读到的版本"与"写入时的版本"一致，
        失败时 rowcount=0，调用方据此返回 412。
        """
        stmt = (
            update(Project)
            .where(Project.id == project.id, Project.lock_version == expected_version)
            .values(**values, lock_version=expected_version + 1)
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(stmt)
        if result.rowcount != 1:
            return False
        # 让 ORM 身份映射里的旧值失效，调用方随后读到的是刚写入的值。
        self._session.expire(project)
        self._session.flush()
        return True

    # ------------------------------------------------------------------
    # 成员关系
    # ------------------------------------------------------------------

    def get_membership(
        self, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> MembershipInfo:
        """查活跃成员关系。

        **只认 `removed_at IS NULL`**：项目成员被移除后立即失效，
        不需要等会话过期。这与"账号停用立即失效"（WP08）是同一原则
        在不同层级上的应用。

        未找到时返回 role=None 的 MembershipInfo 而不是 None ——
        让调用方少一层判空，且语义更明确（"不是成员"是一个正常状态）。
        """
        row = self._session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
                ProjectMember.removed_at.is_(None),
            )
        ).scalar_one_or_none()

        return MembershipInfo(
            project_id=project_id,
            user_id=user_id,
            role=row.member_role if row is not None else None,
        )

    def list_members(
        self, project_id: uuid.UUID, *, include_removed: bool = False
    ) -> list[ProjectMember]:
        """列出项目成员。

        `include_removed=True` 用于审计场景（需求 §2.3：
        历史审核、评论、交付记录保留原操作人）。
        """
        stmt = select(ProjectMember).where(ProjectMember.project_id == project_id)
        if not include_removed:
            stmt = stmt.where(ProjectMember.removed_at.is_(None))
        return list(
            self._session.execute(stmt.order_by(ProjectMember.joined_at)).scalars().all()
        )

    def add_member(
        self, project_id: uuid.UUID, user_id: uuid.UUID, role: str
    ) -> ProjectMember:
        """添加成员。

        **唯一约束会拦重复活跃成员**（`uq_project_members_active`）。
        这里不预先查重再插入 —— 那有并发窗口（两个请求同时查到"不存在"，
        然后都插入）。让数据库约束做最后防线，捕获 IntegrityError 由调用方处理。
        """
        member = ProjectMember(project_id=project_id, user_id=user_id, member_role=role)
        self._session.add(member)
        self._session.flush()
        return member

    def add_owner_and_collaborators(
        self,
        project_id: uuid.UUID,
        *,
        owner_id: uuid.UUID,
        collaborator_ids: Sequence[uuid.UUID] = (),
    ) -> list[ProjectMember]:
        """建项目时一次性写入负责人与协作者（需求 §2.3）。

        **负责人会从协作者列表里剔除**：同一用户在同一项目只能有一条
        活跃成员记录（`uq_project_members_active`），若 ownerId 同时出现在
        collaboratorIds 里，插入第二条会撞约束。这里静默去重而不是报错 ——
        前端传重复是常见的（"默认勾上负责人"），抛错对用户没有任何价值。

        **去重保序**：用 dict.fromkeys 保持传入顺序，便于回显与断言。
        """
        members = [self.add_member(project_id, owner_id, MEMBER_OWNER)]
        for user_id in dict.fromkeys(collaborator_ids):
            if user_id == owner_id:
                continue
            members.append(self.add_member(project_id, user_id, MEMBER_COLLABORATOR))
        return members

    def remove_member(self, project_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """移除成员（标记 removed_at，不删行）。

        **不物理删除**：需求 §2.3 要求历史记录保留原操作人。
        删除行会让历史归属失去依据。

        返回是否真的改了状态（幂等）。
        """
        row = self._session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
                ProjectMember.removed_at.is_(None),
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        row.removed_at = datetime.now(timezone.utc)
        self._session.flush()
        return True

    def count_active_owners(self, project_id: uuid.UUID) -> int:
        """项目活跃负责人数量。

        用于验收"并发交接仍一名负责人"（WP10 验收项）与
        验证 `uq_project_members_active_owner` 约束确实在生效。
        """
        return int(
            self._session.execute(
                select(func.count())
                .select_from(ProjectMember)
                .where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.removed_at.is_(None),
                    ProjectMember.member_role == MEMBER_OWNER,
                )
            ).scalar_one()
        )

    # ------------------------------------------------------------------
    # 负责人交接
    # ------------------------------------------------------------------

    def transfer_owner(
        self,
        project: Project,
        *,
        expected_version: int,
        new_owner_id: uuid.UUID,
        keep_old_collaborator: bool,
    ) -> bool:
        """把项目负责人从 `project.owner_id` 换成 `new_owner_id`。

        返回 False 表示版本冲突（调用方回 412，事务由上层回滚）。

        **三步的顺序不可调换**：
          1. 先处理旧负责人的成员行（移除，或降级为协作者）
          2. 再让新负责人取得 OWNER 位（已有活跃成员行则**升格**，没有才新增）
          3. 最后更新 `projects.owner_id` + `lock_version`

        第 1 步必须在第 2 步之前：`uq_project_members_active_owner`
        是"每项目至多一名活跃负责人"的**数据库级**约束。若先插新负责人，
        此刻旧负责人仍活跃，插入会直接撞唯一索引。
        这正是"并发交接仍一名负责人"的实现机制 ——
        两个并发交接请求中，后到的那个在插 OWNER 时必然失败。

        第 3 步带版本条件：版本冲突时返回 False，上层抛 412 并回滚事务，
        **成员变更一起撤销** —— 不会留下"负责人成员换了但 projects.owner_id 没换"
        的半成品状态。

        **第 2 步为什么必须先查再决定"升格 or 新增"**：
        把项目交给一个**已经在项目里的协作者**是最常见的交接场景之一。
        若无条件 `INSERT` 一条 OWNER 行，会撞 `uq_project_members_active`
        （(project_id, user_id) WHERE removed_at IS NULL —— 同一项目同一用户
        只能有一条活跃成员行）。实测后果不是 201 也不是 412，
        而是 500：异常处理里回读被 expire 的 ORM 对象又触发了
        `PendingRollbackError`（事务已被那次失败语句标记为 aborted）。
        正确做法是**改现有行的 `member_role`**，让"协作者 → 负责人"
        表现为状态迁移而不是新增成员。
        """
        now = datetime.now(timezone.utc)

        old_member = self._session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == project.owner_id,
                ProjectMember.removed_at.is_(None),
            )
        ).scalar_one_or_none()

        if old_member is not None:
            if keep_old_collaborator:
                # 降级为协作者：保留在项目里，但让出 OWNER 位。
                # 不置 removed_at，所以不触发唯一索引冲突 ——
                # 该索引的 WHERE 条件包含 member_role = 'OWNER'。
                old_member.member_role = MEMBER_COLLABORATOR
            else:
                old_member.removed_at = now
            self._session.flush()

        # 第 2 步：新负责人取得 OWNER 位。
        # 先查活跃成员行 —— 存在（只能是协作者，同人交接已被上层拦下）
        # 就升格，不存在才新增。见 docstring。
        existing_new_member = self._session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == new_owner_id,
                ProjectMember.removed_at.is_(None),
            )
        ).scalar_one_or_none()

        if existing_new_member is not None:
            existing_new_member.member_role = MEMBER_OWNER
            self._session.flush()
        else:
            new_member = ProjectMember(
                project_id=project.id,
                user_id=new_owner_id,
                member_role=MEMBER_OWNER,
            )
            self._session.add(new_member)
            self._session.flush()

        return self.update_project_versioned(
            project,
            expected_version=expected_version,
            values={"owner_id": new_owner_id},
        )

    # ------------------------------------------------------------------
    # 字典（品牌/产品）—— 全员可见，不做项目隔离
    # ------------------------------------------------------------------

    def get_brand(self, brand_id: uuid.UUID) -> Brand | None:
        return self._session.get(Brand, brand_id)

    def get_product(self, product_id: uuid.UUID) -> Product | None:
        return self._session.get(Product, product_id)

    def list_brands(
        self,
        *,
        limit: int,
        status: str | None = None,
        keyword: str | None = None,
    ) -> list[Brand]:
        """字典列表。**取全部匹配行供内存游标分页**（不返回 limit+1）。

        为什么字典不像项目那样下推键集分页：
        字典是**全组织共享的主数据**，行数在千级以内（品牌 = 合作品牌数，
        产品 = 每个品牌的 SKU 数），一次取回的内存与查询成本都可忽略。
        项目列表则不同 —— 需求 §2.3 明确"项目数量不设业务上限"，
        必须下推。两处用不同策略是有依据的，不是不一致。
        """
        stmt = select(Brand).order_by(Brand.name, Brand.id)
        if status is not None:
            stmt = stmt.where(Brand.status == status)
        if keyword:
            stmt = stmt.where(Brand.name.ilike(f"%{_escape_like(keyword)}%", escape="\\"))
        return list(self._session.execute(stmt.limit(limit)).scalars().all())

    def list_products(
        self,
        *,
        limit: int,
        brand_id: uuid.UUID | None = None,
        status: str | None = None,
        keyword: str | None = None,
    ) -> list[Product]:
        stmt = select(Product).order_by(Product.name, Product.id)
        if brand_id is not None:
            stmt = stmt.where(Product.brand_id == brand_id)
        if status is not None:
            stmt = stmt.where(Product.status == status)
        if keyword:
            pattern = f"%{_escape_like(keyword)}%"
            stmt = stmt.where(
                or_(
                    Product.name.ilike(pattern, escape="\\"),
                    Product.model_code.ilike(pattern, escape="\\"),
                )
            )
        return list(self._session.execute(stmt.limit(limit)).scalars().all())

    def create_brand(self, *, name: str) -> Brand:
        """新建品牌。

        **不做重名预检**：品牌名可重名（models.py 明确不加唯一约束），
        重复由应用层提示而非数据库拦截。因此这里直接插入。
        """
        brand = Brand(name=name)
        self._session.add(brand)
        self._session.flush()
        return brand

    def create_product(
        self, *, brand_id: uuid.UUID, name: str, model_code: str | None = None
    ) -> Product:
        """新建产品。`(brand_id, model_code)` 非空时唯一（部分唯一索引）。"""
        product = Product(brand_id=brand_id, name=name, model_code=model_code)
        self._session.add(product)
        self._session.flush()
        return product

    def update_brand_versioned(
        self, brand: Brand, *, expected_version: int, values: dict[str, Any]
    ) -> bool:
        """带版本条件更新品牌。语义同 `update_project_versioned`。"""
        stmt = (
            update(Brand)
            .where(Brand.id == brand.id, Brand.lock_version == expected_version)
            .values(**values, lock_version=expected_version + 1)
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(stmt)
        if result.rowcount != 1:
            return False
        self._session.expire(brand)
        self._session.flush()
        return True

    def update_product_versioned(
        self, product: Product, *, expected_version: int, values: dict[str, Any]
    ) -> bool:
        """带版本条件更新产品。语义同 `update_brand_versioned`。"""
        stmt = (
            update(Product)
            .where(Product.id == product.id, Product.lock_version == expected_version)
            .values(**values, lock_version=expected_version + 1)
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(stmt)
        if result.rowcount != 1:
            return False
        self._session.expire(product)
        self._session.flush()
        return True

    def count_projects_for_brand(self, brand_id: uuid.UUID) -> int:
        """引用该品牌的项目数（任何阶段都算）。

        归档前检查用：已归档的品牌不应还能新建项目引用它，
        但**已经引用它的项目必须继续正常工作** —— 所以是"禁止归档"，
        而不是"归档时清空引用"。
        """
        return int(
            self._session.execute(
                select(func.count()).select_from(Project).where(Project.brand_id == brand_id)
            ).scalar_one()
        )

    def count_projects_for_product(self, product_id: uuid.UUID) -> int:
        """引用该产品的项目数（任何阶段都算）。"""
        return int(
            self._session.execute(
                select(func.count())
                .select_from(Project)
                .where(Project.product_id == product_id)
            ).scalar_one()
        )

    def count_active_members(self, project_id: uuid.UUID) -> int:
        """项目活跃成员数（含负责人）。"""
        return int(
            self._session.execute(
                select(func.count())
                .select_from(ProjectMember)
                .where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.removed_at.is_(None),
                )
            ).scalar_one()
        )

    def get_member_row(
        self, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> ProjectMember | None:
        """取活跃成员行。

        与 `get_membership` 的区别：这里返回整行。
        用于"幂等返回现有成员信息"与"并发插入冲突后回查"两个场景 ——
        两者都需要 `joined_at` 等字段来组装响应。
        """
        return self._session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
                ProjectMember.removed_at.is_(None),
            )
        ).scalar_one_or_none()

    # ------------------------------------------------------------------
    # 负责人变更记录
    # ------------------------------------------------------------------

    def record_assignment(
        self,
        *,
        project_id: uuid.UUID,
        previous_owner_id: uuid.UUID,
        new_owner_id: uuid.UUID,
        keep_old_collaborator: bool,
        reason: str,
        transferred_work_item_count: int,
        created_by: uuid.UUID,
    ) -> ProjectAssignment:
        """写下一条负责人变更记录（需求 §2.3 第 1 条"保存变更原因"）。"""
        assignment = ProjectAssignment(
            project_id=project_id,
            previous_owner_id=previous_owner_id,
            new_owner_id=new_owner_id,
            keep_old_collaborator=keep_old_collaborator,
            reason=reason,
            transferred_work_item_count=transferred_work_item_count,
            created_by=created_by,
        )
        self._session.add(assignment)
        self._session.flush()
        return assignment

    def list_assignments(self, project_id: uuid.UUID) -> list[ProjectAssignment]:
        """某项目的负责人变更历史（按时间正序）。"""
        return list(
            self._session.execute(
                select(ProjectAssignment)
                .where(ProjectAssignment.project_id == project_id)
                .order_by(ProjectAssignment.created_at, ProjectAssignment.id)
            )
            .scalars()
            .all()
        )


__all__ = ["DEFAULT_PROJECT_TIMEZONE", "ProjectRepository"]
