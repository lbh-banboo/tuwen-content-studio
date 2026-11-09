"""projects: brands products projects project_members

WP09 项目领域迁移。按数据结构文档 02 §3 建立权限模型的载体表。

Revision ID: 4531692fa005
Revises: 236d8fd94c52
Create Date: 2026-09-21

**由 `--autogenerate` 生成后经人工审校修正。** 表结构部分（含部分唯一索引的
WHERE 条件）自动生成准确，直接采用；修正的是两处越界内容：

1. **删除 `op.drop_table('smoke_meta')` 与 `op.drop_table('wp05_vec_probe')`。**
   这是第二次遇到同一问题（WP08 迁移也出现过）：autogenerate 看到
   "数据库里存在但代码里没有对应模型"的表，就判定它们该被删。
   那两张是 M0 冒烟残留表，**不属任何迁移的职责** ——
   在真实环境执行会造成数据丢失。

   **这已经是可复现的规律，不是偶发**：只要仓库里存在"手工建的验证表"，
   每次 autogenerate 都会提议删除它们。因此**每次 autogenerate 后
   必须检查并删除 drop_table / drop_index 语句**。

2. **重写 downgrade。** 自动生成的版本把"重建残留表"插在
   "删除本项目表"之前，且用 `sa.NullType()` 表示 vector 列
   （autogenerate 无法推断类型时的占位，执行必然失败）。
   重写后只做本迁移的逆操作，顺序与 upgrade 严格相反。

**关于部分唯一索引 —— 本迁移的关键约束**：

| 索引 | WHERE 条件 | 作用 |
|---|---|---|
| `uq_project_members_active` | `removed_at IS NULL` | 同一项目同一用户不能有两条活跃成员记录 |
| `uq_project_members_active_owner` | `removed_at IS NULL AND member_role = 'OWNER'` | **每项目活跃负责人唯一** |

后一条是权限模型的核心：它保证"一个项目不会同时有两个活跃负责人"。
用部分唯一索引而非普通唯一约束，是因为普通约束会让
"移除旧负责人后新增新负责人"失败（旧行仍在表里）。

**downgrade 必须可执行**：WP47 要求备份恢复与回滚演练。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4531692fa005"
down_revision: str | Sequence[str] | None = "236d8fd94c52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立项目领域：brands → products → projects → project_members。

    顺序不可调换：子表的外键依赖父表。
    """

    # ------------------------------------------------------------------
    # brands：品牌字典（全员可见，不做项目隔离）
    # ------------------------------------------------------------------
    op.create_table(
        "brands",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        # 名称可重名但需提示（文档 02 §3）—— 不加唯一约束，重复检测由应用层提示。
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="ACTIVE", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name=op.f("ck_brands_status_valid")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brands")),
    )
    op.create_index("ix_brands_name", "brands", ["name"], unique=False)

    # ------------------------------------------------------------------
    # products：产品字典
    # ------------------------------------------------------------------
    op.create_table(
        "products",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("brand_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("model_code", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="ACTIVE", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name=op.f("ck_products_status_valid")),
        sa.ForeignKeyConstraint(
            ["brand_id"], ["brands.id"], name=op.f("fk_products_brand_id_brands"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
    )
    op.create_index("ix_products_brand_id", "products", ["brand_id"], unique=False)
    # 部分唯一：型号同一品牌下不重复，但允许 model_code 为空（多个 NULL 不算重复）。
    op.create_index(
        "uq_products_brand_model_code",
        "products",
        ["brand_id", "model_code"],
        unique=True,
        postgresql_where=sa.text("model_code IS NOT NULL"),
    )

    # ------------------------------------------------------------------
    # projects：项目（权限隔离的基本单位）
    # ------------------------------------------------------------------
    op.create_table(
        "projects",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("project_code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("brand_id", sa.UUID(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        # 负责人权威字段（文档 02 §3）。与 project_members 的 OWNER 由事务保持一致。
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("phase", sa.String(length=30), server_default="PREPARATION", nullable=False),
        sa.Column("target_delivery_count", sa.Integer(), nullable=False),
        sa.Column("target_publication_count", sa.Integer(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("timezone", sa.Text(), server_default="Asia/Shanghai", nullable=False),
        sa.Column(
            "external_policy", sa.String(length=30), server_default="LOCAL_ONLY", nullable=False
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("active_baseline_id", sa.UUID(), nullable=True),
        sa.Column("active_plan_id", sa.UUID(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # mutable 表带 lock_version，对外映射 ETag（文档 02 §1）。
        sa.Column("lock_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        # 阶段与结项时间必须一致 —— 防"状态与时间打架"导致审计无法判断结项时点。
        sa.CheckConstraint(
            "(phase = 'CLOSED' AND closed_at IS NOT NULL) "
            "OR (phase <> 'CLOSED' AND closed_at IS NULL)",
            name=op.f("ck_projects_closed_at_consistent_with_phase"),
        ),
        sa.CheckConstraint(
            "external_policy IN ('LOCAL_ONLY', 'APPROVED_EXTERNAL')",
            name=op.f("ck_projects_external_policy_valid"),
        ),
        sa.CheckConstraint(
            "phase IN ('PREPARATION', 'PRODUCTION', 'DATA_COLLECTION', 'CLOSED')",
            name=op.f("ck_projects_phase_valid"),
        ),
        # 交付篇数必须为正（需求 §4.1：正整数）。
        sa.CheckConstraint("target_delivery_count > 0", name=op.f("ck_projects_target_delivery_positive")),
        sa.CheckConstraint(
            "target_publication_count IS NULL OR target_publication_count >= 0",
            name=op.f("ck_projects_target_publication_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["brand_id"], ["brands.id"], name=op.f("fk_projects_brand_id_brands"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_projects_owner_id_users"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name=op.f("fk_projects_product_id_products"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
        sa.UniqueConstraint("project_code", name="uq_projects_project_code"),
    )
    op.create_index("ix_projects_owner_id", "projects", ["owner_id"], unique=False)
    op.create_index("ix_projects_phase", "projects", ["phase"], unique=False)
    # "我的项目"按阶段 + 截止日排序（研发计划 02 §14 索引计划）。
    op.create_index("ix_projects_phase_due_date", "projects", ["phase", "due_date"], unique=False)

    # ------------------------------------------------------------------
    # project_members：项目成员
    # ------------------------------------------------------------------
    op.create_table(
        "project_members",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("member_role", sa.String(length=20), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # 用 removed_at 而非删除行：需求 §2.3 要求历史记录保留原操作人。
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "member_role IN ('OWNER', 'COLLABORATOR')",
            name=op.f("ck_project_members_member_role_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_project_members_project_id_projects"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_project_members_user_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_members")),
    )
    op.create_index(
        "ix_project_members_user_id", "project_members", ["user_id"], unique=False
    )
    # "我的项目"查询：按 user 找活跃成员关系。
    op.create_index(
        "ix_project_members_active_user",
        "project_members",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    # 活跃成员唯一：同一项目同一用户不能有两条活跃记录。
    # 部分唯一索引而非普通约束 —— 后者会让"移除后重新加入"失败。
    op.create_index(
        "uq_project_members_active",
        "project_members",
        ["project_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    # **每项目活跃 OWNER 唯一** —— 权限模型的核心约束（文档 02 §3）。
    op.create_index(
        "uq_project_members_active_owner",
        "project_members",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL AND member_role = 'OWNER'"),
    )


def downgrade() -> None:
    """回滚项目领域。

    顺序与 upgrade 严格相反：先删索引、再删子表（project_members）、
    最后按依赖倒序删 projects → products → brands。
    反过来会因外键依赖而失败。

    **不重建任何"本迁移之前存在的其他表"。** autogenerate 曾试图重建
    M0 冒烟残留表，那是越界 —— 本迁移只对本次新建的四张表负责。
    """
    op.drop_index(
        "uq_project_members_active_owner",
        table_name="project_members",
        postgresql_where=sa.text("removed_at IS NULL AND member_role = 'OWNER'"),
    )
    op.drop_index(
        "uq_project_members_active",
        table_name="project_members",
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    op.drop_index(
        "ix_project_members_active_user",
        table_name="project_members",
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    op.drop_index("ix_project_members_user_id", table_name="project_members")
    op.drop_table("project_members")

    op.drop_index("ix_projects_phase_due_date", table_name="projects")
    op.drop_index("ix_projects_phase", table_name="projects")
    op.drop_index("ix_projects_owner_id", table_name="projects")
    op.drop_table("projects")

    op.drop_index(
        "uq_products_brand_model_code",
        table_name="products",
        postgresql_where=sa.text("model_code IS NOT NULL"),
    )
    op.drop_index("ix_products_brand_id", table_name="products")
    op.drop_table("products")

    op.drop_index("ix_brands_name", table_name="brands")
    op.drop_table("brands")