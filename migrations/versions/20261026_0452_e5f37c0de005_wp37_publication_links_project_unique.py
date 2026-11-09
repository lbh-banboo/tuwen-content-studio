"""WP37：`publication_links` 加冗余 `project_id`，并建 `(project_id, canonical_url)` 唯一索引。

revision: e5f37c0de005
down_revision: d4f33c0de004

## 一、做什么

1. `publication_links` 加 `project_id UUID NOT NULL`，FK `projects.id` RESTRICT；
2. 建唯一索引 `uq_publication_links_project_canonical (project_id, canonical_url)`。

## 二、为什么是「项目内唯一」而不是全局唯一

WP36 只建了普通索引（Q3）：全局唯一会让「跨项目同笔记需管理者确认」的分支物理上不可达。
拍板项 1（2026-10-24）选 `(project_id, canonical_url)`：项目内挡重复，跨项目命中交服务层判定。
`project_id` 是 `publications.project_id` 的**冗余副本**——唯一索引不能跨表，所以必须落在本表上；
一致性由服务层写入时从所属发布取值保证，不单独加触发器。

## 三、存量前提（⚠️ 升级前提）

升级直接 `ADD COLUMN ... NOT NULL`，**不做回填**。前提是 `publication_links` 为空：
WP36 没有任何写这张表的代码（模型 docstring 原文「本包没有任何写这张表的服务层代码」），
2026-10-26 实测共享库 0 行。若某环境已有行，`upgrade` 会在加列处直接失败——这是有意的：
静默按所属发布回填会掩盖「这些行是谁写的」这个本不该存在的问题。

## 四、G2 铁律执行记录

1. 只动本包自有对象；不删、不改任何其他迁移创建的对象；
2. 没有触发器、没有函数，`downgrade` 只删本迁移加的 FK、唯一索引与列；
3. 已在一次性库上 `upgrade head` → `downgrade -1` → `upgrade head` 往返实测。

## 五、⚠️ `downgrade` 有数据损失

`project_id` 列随降级丢弃；降级后项目内唯一性约束消失。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
# 用 repr() 而非 "Ellipsis"：字符串插值会把 Python 的 None 渲染成字符串 "None"，
# 导致 Alembic 解析 down_revision 时 KeyError: 'None'（本机实测踩到过）。
# repr() 输出的是 Python 字面量，None 就是 None，'abc123' 就是 'abc123'。
revision: str = 'e5f37c0de005'
down_revision: str | Sequence[str] | None = 'd4f33c0de004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """正向迁移。"""
    op.add_column('publication_links', sa.Column('project_id', sa.UUID(), nullable=False))
    op.create_index('uq_publication_links_project_canonical', 'publication_links', ['project_id', 'canonical_url'], unique=True)
    op.create_foreign_key(op.f('fk_publication_links_project_id_projects'), 'publication_links', 'projects', ['project_id'], ['id'], ondelete='RESTRICT')


def downgrade() -> None:
    """反向迁移。

    必须补全，不能留 NotImplementedError —— 那会让任何回滚尝试直接失败，
    与 WP47 的恢复演练（RPO/RTO）目标冲突。

    **只删本迁移自己建出来的对象**：跨包共用的函数（如
    `rednote_reject_mutation()`）生命周期归**引入它的那个迁移**，
    在这里删会撞 "cannot drop function ... because other objects depend on it"
    （WP11 踩过、WP16 差点再踩）。
    """
    op.drop_constraint(op.f('fk_publication_links_project_id_projects'), 'publication_links', type_='foreignkey')
    op.drop_index('uq_publication_links_project_canonical', table_name='publication_links')
    op.drop_column('publication_links', 'project_id')
