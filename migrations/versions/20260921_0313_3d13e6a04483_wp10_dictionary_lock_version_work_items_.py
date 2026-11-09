"""WP10：字典 lock_version、待办（work_items）与负责人变更记录（project_assignments）。

revision: 3d13e6a04483
down_revision: 4531692fa005

**本迁移包含三块变更**：

1. `brands` / `products` 补 `lock_version` 列 ——
   字典是可改名、可归档的 mutable 资源。按数据结构文档 02 §1
   "mutable 表带 updated_at + lock_version"，缺了它就没有 ETag 载体，
   PATCH 无法做乐观并发保护。WP09 建这两张表时遗漏，WP10 补上。

2. 新增 `work_items`（待办）—— 依据数据结构文档 02 §3 的字段定义。
   WP10 的验收标准含"待办和历史正确"：负责人交接时要把
   "未认领 + 原负责人"的未结待办转给新负责人（需求 §2.3 第 2 条），
   没有这张表，这项验收就只是一句话。

3. 新增 `project_assignments`（负责人变更记录）——
   依据需求 §2.3 第 1 条"选择新负责人并保存变更原因"与协议 §3 的
   `201 Assignment`。数据结构文档 02 的表清单漏列了该实体（属缺口，已登记），
   字段全部来自上述两处原文，没有自行发明的字段。

---

## 【铁律 G2 —— 每次 autogenerate 后必读】

本次 autogenerate **再次**提议删除 `smoke_meta` 与 `wp05_vec_probe`
（M0 冒烟脚本建的临时表，不在 SQLAlchemy 模型里），且 downgrade 还试图用
`sa.NullType()` 重建 `vector` 列 —— 那是 autogenerate 无法识别的类型，
重建出的表结构是错的。

已人工删除全部 `op.drop_table` / `op.drop_index` 语句，并重写 downgrade。
这两张探针表**不属于本项目的模型，永远不要删**：
上一轮迁移的双向验证用它们的行数作为"残留表未被误删"的证据。

来源：`docs/待定项与问题登记册.md` §4.3 的 ISS-13 / ISS-14。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
# 用 repr() 而非 "Ellipsis"：字符串插值会把 Python 的 None 渲染成字符串 "None"，
# 导致 Alembic 解析 down_revision 时 KeyError: 'None'（本机实测踩到过）。
revision: str = '3d13e6a04483'
down_revision: str | Sequence[str] | None = '4531692fa005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """正向迁移。

    顺序：先补列（无依赖），再建 work_items，最后建 project_assignments
    （后者有指向 projects 与 users 的外键，必须在两张表都存在之后建）。
    """
    # --- 1. 字典补乐观锁版本列 -------------------------------------------
    # server_default='1' 保证既有行也被填上，且 NOT NULL 能立即加上
    # （若不给默认值，PG 需要三步：加可空列 → 回填 → 改非空）。
    # 默认值保留在表上：应用侧插入时不传该列也能得到 1。
    op.add_column(
        'brands',
        sa.Column(
            'lock_version',
            sa.BigInteger(),
            server_default=sa.text('1'),
            nullable=False,
        ),
    )
    op.add_column(
        'products',
        sa.Column(
            'lock_version',
            sa.BigInteger(),
            server_default=sa.text('1'),
            nullable=False,
        ),
    )

    # --- 2. 待办 ----------------------------------------------------------
    op.create_table(
        'work_items',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        # 可空：存在不隶属任何项目的待办。
        sa.Column('project_id', sa.UUID(), nullable=True),
        # 可空：需求 §2.3 第 2 条提到的"未认领的待办"即 assignee_id IS NULL。
        sa.Column('assignee_id', sa.UUID(), nullable=True),
        sa.Column('kind', sa.String(length=50), nullable=False),
        sa.Column('subject_type', sa.String(length=50), nullable=False),
        # subject_id 不加外键：待办指向的对象类型是开放的（内容版本、事实条目…），
        # 不同类型在不同表，多态外键无法用单纯 FK 表达。文档 02 §3 也未加。
        sa.Column('subject_id', sa.UUID(), nullable=False),
        sa.Column('state', sa.String(length=20), server_default='OPEN', nullable=False),
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('dedupe_key', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("state IN ('OPEN', 'DONE', 'CANCELLED')", name=op.f('ck_work_items_state_valid')),
        sa.ForeignKeyConstraint(
            ['assignee_id'], ['users.id'],
            name=op.f('fk_work_items_assignee_id_users'), ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['project_id'], ['projects.id'],
            name=op.f('fk_work_items_project_id_projects'), ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_work_items')),
    )
    op.create_index(
        'ix_work_items_open_assignee', 'work_items', ['assignee_id'],
        unique=False, postgresql_where=sa.text("state = 'OPEN'"),
    )
    op.create_index('ix_work_items_project_id', 'work_items', ['project_id'], unique=False)
    op.create_index(
        'ix_work_items_subject', 'work_items', ['subject_type', 'subject_id'], unique=False
    )
    # 文档 02 §3："同未结待办 dedupe_key 条件 UQ"。
    # 部分唯一索引而非普通唯一约束：已结待办允许有相同 dedupe_key
    # （同一对象反复产生待办是正常的），只有"同时未结"才算冲突。
    op.create_index(
        'uq_work_items_open_dedupe', 'work_items', ['dedupe_key'],
        unique=True,
        postgresql_where=sa.text("state = 'OPEN' AND dedupe_key IS NOT NULL"),
    )

    # --- 3. 负责人变更记录 -------------------------------------------------
    op.create_table(
        'project_assignments',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('previous_owner_id', sa.UUID(), nullable=False),
        sa.Column('new_owner_id', sa.UUID(), nullable=False),
        sa.Column('keep_old_collaborator', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('transferred_work_item_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_by', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        # 交接必须真的换人 —— 同人交接是无效操作，拦在数据库层。
        sa.CheckConstraint(
            'previous_owner_id <> new_owner_id',
            name=op.f('ck_project_assignments_owner_changed'),
        ),
        sa.CheckConstraint(
            'transferred_work_item_count >= 0',
            name=op.f('ck_project_assignments_transferred_non_negative'),
        ),
        # RESTRICT：变更历史里引用的用户不能被物理删除，否则历史失去意义。
        # 账号停用是改状态（需求 §2.4），不删行，因此 RESTRICT 不妨碍业务。
        sa.ForeignKeyConstraint(
            ['created_by'], ['users.id'],
            name=op.f('fk_project_assignments_created_by_users'), ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['new_owner_id'], ['users.id'],
            name=op.f('fk_project_assignments_new_owner_id_users'), ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['previous_owner_id'], ['users.id'],
            name=op.f('fk_project_assignments_previous_owner_id_users'), ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['project_id'], ['projects.id'],
            name=op.f('fk_project_assignments_project_id_projects'), ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_project_assignments')),
    )
    op.create_index(
        'ix_project_assignments_project_id', 'project_assignments', ['project_id'], unique=False
    )


def downgrade() -> None:
    """反向迁移。

    **只回滚本迁移引入的东西**：不重建 `smoke_meta` / `wp05_vec_probe`
    （autogenerate 的提议已删除，理由见模块 docstring 的铁律 G2 段）。

    顺序与 upgrade 相反：先拆有外键的表，再拆无依赖的，最后删列。
    """
    op.drop_index('ix_project_assignments_project_id', table_name='project_assignments')
    op.drop_table('project_assignments')

    op.drop_index(
        'uq_work_items_open_dedupe', table_name='work_items',
        postgresql_where=sa.text("state = 'OPEN' AND dedupe_key IS NOT NULL"),
    )
    op.drop_index('ix_work_items_subject', table_name='work_items')
    op.drop_index('ix_work_items_project_id', table_name='work_items')
    op.drop_index(
        'ix_work_items_open_assignee', table_name='work_items',
        postgresql_where=sa.text("state = 'OPEN'"),
    )
    op.drop_table('work_items')

    op.drop_column('products', 'lock_version')
    op.drop_column('brands', 'lock_version')
