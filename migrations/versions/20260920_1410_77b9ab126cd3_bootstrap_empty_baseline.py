"""bootstrap: 空基线迁移

WP06 骨架的初始迁移。**不创建任何业务表** —— 业务表（project/content/batch 等）
由 M1 后续 WP11/WP12 按数据结构文档逐步加入。

为什么需要一个"什么都不做"的迁移：

它把 `alembic_version` 表建出来并写入一个已知 revision，从而给出一致的迁移
基线。没有它的话，第一个真实迁移会同时承担"建立版本表"与"建业务表"两件事，
一旦那个迁移有问题，就分不清是基线问题还是业务问题。有了空基线之后：

    alembic upgrade head   → 在全新库上得到干净的起点
    alembic downgrade base → 回到无版本表状态，可用于"重建环境"演练

downgrade 保持对称（同样不做事），这样 `upgrade head` + `downgrade base`
可以反复来回执行做冒烟，而不会残留状态。

Revision ID: 77b9ab126cd3
Revises: None
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "77b9ab126cd3"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """正向迁移：空基线，不创建任何业务对象。

    刻意留空而非放一个示例 `CREATE TABLE`：骨架期建表会诱导后续模块
    "顺手用这张表"，而真正的表结构必须由 WP11/WP12 按数据结构文档 02 定义
    （含外键、约束、索引、pgvector 列）。提前引入半成品 schema 的返工成本
    高于现在什么都不做。
    """
    pass


def downgrade() -> None:
    """反向迁移：与 upgrade 对称，同样不做事。

    必须显式实现而不是留 `NotImplementedError` —— 后者会让 WP47
    要求的"恢复/回滚演练"在第一步就失败。
    """
    pass