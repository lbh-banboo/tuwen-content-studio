"""项目领域存储模型（WP09）。

按数据结构文档 02 §3 建立 `brands`、`products`、`projects`、`project_members`。

**为什么 WP09 要建项目表**：
WP09 的验收标准是"管理者全权、运营限项目、知识共享互不混淆"。
"限项目"必须有一个项目对象作为权限载体 —— 没有 projects 表就无法表达
"这个运营能看这个项目、不能看那个项目"。因此项目表是权限模型的必要基础。

**为什么 brands/products 一并建**：
`projects.brand_id` 与 `products.brand_id` 有外键约束，
不建父表就无法建子表。品牌与产品字典本身属 WP10 范围，
但表结构在此建立（WP10 只做 CRUD 接口，不再改表）。
这是"共享迁移由技术负责人排顺序"（研发计划 04 §6）的实践：
表结构一次建对，避免后续模块各建一半。

**关于 projects 的阶段字段**：
需求 §4.2 的四个阶段（准备期/投产期/数据回收期/已结束）在文档 02 §12
里对应 `PREPARATION → PRODUCTION → DATA_COLLECTION → CLOSED`。
**阶段不能由客户端任意 PATCH**（文档 02 §12 明确），
因此这里的 CHECK 只约束取值合法，真正的转换规则由服务端状态机把关。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.platform.base import (
    Base,
    created_at_column,
    lock_version_column,
    updated_at_column,
    uuid_pk,
)

# ---------------------------------------------------------------------------
# 通用状态
# ---------------------------------------------------------------------------

# 字典类实体（品牌/产品）的启用状态。
DICT_ACTIVE: Final[str] = "ACTIVE"
DICT_ARCHIVED: Final[str] = "ARCHIVED"
DICT_STATUSES: Final[tuple[str, ...]] = (DICT_ACTIVE, DICT_ARCHIVED)

# ---------------------------------------------------------------------------
# 项目阶段（需求 §4.2 / 文档 02 §12）
# ---------------------------------------------------------------------------

PHASE_PREPARATION: Final[str] = "PREPARATION"
PHASE_PRODUCTION: Final[str] = "PRODUCTION"
PHASE_DATA_COLLECTION: Final[str] = "DATA_COLLECTION"
PHASE_CLOSED: Final[str] = "CLOSED"
PROJECT_PHASES: Final[tuple[str, ...]] = (
    PHASE_PREPARATION,
    PHASE_PRODUCTION,
    PHASE_DATA_COLLECTION,
    PHASE_CLOSED,
)

# ---------------------------------------------------------------------------
# 成员角色（文档 02 §3）
# ---------------------------------------------------------------------------

MEMBER_OWNER: Final[str] = "OWNER"
MEMBER_COLLABORATOR: Final[str] = "COLLABORATOR"
MEMBER_ROLES: Final[tuple[str, ...]] = (MEMBER_OWNER, MEMBER_COLLABORATOR)

# ---------------------------------------------------------------------------
# 出网策略（ADR-10）
# ---------------------------------------------------------------------------

# 项目可选择只用本地模型，或允许调用获批外部模型。
EXTERNAL_POLICY_LOCAL_ONLY: Final[str] = "LOCAL_ONLY"
EXTERNAL_POLICY_APPROVED: Final[str] = "APPROVED_EXTERNAL"
EXTERNAL_POLICIES: Final[tuple[str, ...]] = (
    EXTERNAL_POLICY_LOCAL_ONLY,
    EXTERNAL_POLICY_APPROVED,
)


class Brand(Base):
    """品牌字典。

    需求 §2.2 实施默认："知识库关联的品牌／产品字典全员可见"。
    因此品牌/产品不做项目级隔离 —— 它们是共享字典，
    真正的隔离发生在项目数据（内容、费用、KOC 记录）上。
    """

    __tablename__ = "brands"

    id: Mapped[uuid.UUID] = uuid_pk()
    # 名称可重名但需提示（文档 02 §3）—— 因此不加唯一约束，
    # 重复检测由应用层做提示，而不是用数据库约束硬拦。
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=DICT_ACTIVE
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    # WP10 补：字典是可改名、可归档的 mutable 资源。按数据结构文档 02 §1
    # "mutable 表带 updated_at + lock_version"，缺一不可 ——
    # 没有 lock_version 就没有 ETag 载体，PATCH 无法做乐观并发保护。
    # WP09 建这两张表时遗漏，WP10 补上。
    lock_version: Mapped[int] = lock_version_column()

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'ARCHIVED')", name="status_valid"
        ),
        Index("ix_brands_name", "name"),
    )


class Product(Base):
    """产品字典。

    `(brand_id, model_code)` 条件唯一：型号在同一品牌下不应重复，
    但 `model_code` 可空（有些产品没有型号），
    因此用**部分唯一索引**（WHERE model_code IS NOT NULL）而不是普通唯一约束 ——
    普通唯一约束下多个 NULL 是允许的（PG 语义），但可空列的语义更清晰。
    """

    __tablename__ = "products"

    id: Mapped[uuid.UUID] = uuid_pk()
    brand_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("brands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    model_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=DICT_ACTIVE
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    # 同 Brand：补 lock_version 作为 ETag 载体。
    lock_version: Mapped[int] = lock_version_column()

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'ARCHIVED')", name="status_valid"
        ),
        Index("ix_products_brand_id", "brand_id"),
        # 部分唯一：只约束非空型号。
        Index(
            "uq_products_brand_model_code",
            "brand_id",
            "model_code",
            unique=True,
            postgresql_where=text("model_code IS NOT NULL"),
        ),
    )


class Project(Base):
    """项目。权限隔离的基本单位。"""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = uuid_pk()

    # 展示编号。全局唯一、不可变（需求 §4.1）—— 因此不加 onupdate。
    project_code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    brand_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("brands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 负责人权威字段（文档 02 §3：projects.owner_id 是负责人权威字段）。
    # 与 project_members 里的 OWNER 必须一致，由负责人切换事务保证。
    owner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    phase: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=PHASE_PREPARATION
    )

    # 计划交付篇数。需求 §4.1 要求正整数。
    target_delivery_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # 计划发布次数（与内容篇数分开，需求 §4.1）。
    target_publication_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="Asia/Shanghai"
    )

    external_policy: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=EXTERNAL_POLICY_LOCAL_ONLY
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 当前生效的基线与方案（WP11/WP22 填充）。可空：准备期还没有。
    active_baseline_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    active_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    # mutable 表带 lock_version，对外映射 ETag（文档 02 §1）。
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )

    __table_args__ = (
        UniqueConstraint("project_code", name="uq_projects_project_code"),
        # 交付篇数必须为正 —— 0 篇的项目没有意义，负数更不可能。
        CheckConstraint("target_delivery_count > 0", name="target_delivery_positive"),
        CheckConstraint(
            "target_publication_count IS NULL OR target_publication_count >= 0",
            name="target_publication_non_negative",
        ),
        CheckConstraint(
            "phase IN ('PREPARATION', 'PRODUCTION', 'DATA_COLLECTION', 'CLOSED')",
            name="phase_valid",
        ),
        CheckConstraint(
            "external_policy IN ('LOCAL_ONLY', 'APPROVED_EXTERNAL')",
            name="external_policy_valid",
        ),
        # 阶段与结项时间必须一致：CLOSED 必须有 closed_at，反之不应有。
        CheckConstraint(
            "(phase = 'CLOSED' AND closed_at IS NOT NULL) "
            "OR (phase <> 'CLOSED' AND closed_at IS NULL)",
            name="closed_at_consistent_with_phase",
        ),
        Index("ix_projects_owner_id", "owner_id"),
        Index("ix_projects_phase", "phase"),
        # "我的项目"查询用（研发计划 02 §14 索引计划）：
        # 按 phase + due_date 排序列表。
        Index("ix_projects_phase_due_date", "phase", "due_date"),
    )


class ProjectMember(Base):
    """项目成员。

    **每项目活跃 OWNER 唯一**（文档 02 §3）—— 用部分唯一索引实现：
    只对 `removed_at IS NULL` 且 `member_role = 'OWNER'` 的行做唯一约束。
    这样"移除旧负责人后新增新负责人"不会被约束拦住
    （旧行有 removed_at，不参与唯一性），而"同时存在两个活跃负责人"会被拦。

    **为什么成员表用 removed_at 而不是删除行**：
    需求 §2.3 要求"历史审核、评论、交付记录保留原操作人"。
    物理删除成员行会让历史归属失去依据。
    """

    __tablename__ = "project_members"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    member_role: Mapped[str] = mapped_column(String(20), nullable=False)

    joined_at: Mapped[datetime] = created_at_column()
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "member_role IN ('OWNER', 'COLLABORATOR')", name="member_role_valid"
        ),
        # 活跃成员唯一：同一项目同一用户不能有两条活跃记录。
        # 用部分唯一索引而非普通唯一约束 —— 后者会让"移除后重新加入"失败。
        Index(
            "uq_project_members_active",
            "project_id",
            "user_id",
            unique=True,
            postgresql_where=text("removed_at IS NULL"),
        ),
        # **每项目活跃 OWNER 唯一** —— 权限模型的核心约束。
        Index(
            "uq_project_members_active_owner",
            "project_id",
            unique=True,
            postgresql_where=text("removed_at IS NULL AND member_role = 'OWNER'"),
        ),
        Index("ix_project_members_user_id", "user_id"),
        # "我的项目"查询用：按 user 找活跃成员关系。
        Index(
            "ix_project_members_active_user",
            "user_id",
            postgresql_where=text("removed_at IS NULL"),
        ),
    )


class ProjectAssignment(Base):
    """项目负责人变更记录（WP10）。

    **为什么要建这张表**（这是一个文档缺口，补建依据）：
    - 需求 §2.3 第 1 条要求交接时"**选择新负责人并保存变更原因**" ——
      没有这张表，"保存变更原因"无处落地；
    - 协议 §3 要求 `POST /projects/{id}/assignments` 返回 **201 Assignment** ——
      Assignment 是一个实体，不是一次性的响应体。

    而数据结构文档 02 §3 的表清单里没有列出 assignment 表 —— 属于漏列。
    WP10 依据需求与协议两处明文要求补建，字段全部来自这两处的原文，
    没有自行发明的字段。

    **为什么是追加型表（无 lock_version）**：
    变更记录一旦写下就不应被修改，没有"并发编辑同一条历史记录"的场景。
    `projects.lock_version` 负责保护"当前状态"，本表负责记录"历史事实"，
    两者职责不同。

    **为什么 previous_owner_id 用 RESTRICT**：
    变更历史里引用的用户如果被物理删除，这段历史就失去了意义。
    账号停用是改状态（需求 §2.4），不删行 —— 因此 RESTRICT 不会妨碍业务。
    """

    __tablename__ = "project_assignments"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    previous_owner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    new_owner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 需求 §2.3 第 4 条："管理者可同时勾选将其保留为协作者"。
    keep_old_collaborator: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # 需求 §2.3 第 1 条："保存变更原因"。必填 —— 没有原因的交接无法审计。
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    # 需求 §2.3 第 2 条的转交结果。记录条数让"待办转交正确"这个验收项可被追溯。
    transferred_work_item_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # 操作人（管理者）。与负责人不同 —— 这是"谁执行的变更"。
    created_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        # 交接必须真的换人 —— 同人交接是无效操作，拦在数据库层。
        CheckConstraint(
            "previous_owner_id <> new_owner_id", name="owner_changed"
        ),
        CheckConstraint(
            "transferred_work_item_count >= 0", name="transferred_non_negative"
        ),
        Index("ix_project_assignments_project_id", "project_id"),
    )


__all__ = [
    "DICT_ACTIVE",
    "DICT_ARCHIVED",
    "DICT_STATUSES",
    "EXTERNAL_POLICIES",
    "EXTERNAL_POLICY_APPROVED",
    "EXTERNAL_POLICY_LOCAL_ONLY",
    "MEMBER_COLLABORATOR",
    "MEMBER_OWNER",
    "MEMBER_ROLES",
    "PHASE_CLOSED",
    "PHASE_DATA_COLLECTION",
    "PHASE_PREPARATION",
    "PHASE_PRODUCTION",
    "PROJECT_PHASES",
    "Brand",
    "Product",
    "Project",
    "ProjectAssignment",
    "ProjectMember",
]