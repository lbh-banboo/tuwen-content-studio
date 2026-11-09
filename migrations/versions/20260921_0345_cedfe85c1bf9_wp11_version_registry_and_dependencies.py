"""WP11：通用版本注册表（version_nodes）与版本依赖边（version_dependencies）。

revision: cedfe85c1bf9
down_revision: 3d13e6a04483

**本迁移包含两块变更**：

1. `version_nodes` —— 数据结构文档 02 §5 的通用版本注册表。
   解决"跨模块引用无外键"：Brief / 方案 / 内容 / 知识 / 媒体 / Prompt / 报告 /
   基线八类版本共用一张身份表，`UQ(entity_type, entity_id, revision_no)`
   保证"某实体的第 N 版"唯一。

2. `version_dependencies` —— 版本间依赖边。
   `PK(parent_version_id, dependency_version_id, purpose)`；
   CHECK 禁止自引用；依赖图的有向无环由服务层检查（需遍历，CHECK 做不到）。

**外加数据库级保证（触发器）**，共 3 个：
  · `trg_version_nodes_immutable` / `trg_version_dependencies_immutable`
    —— 拒绝 UPDATE / DELETE。理由：只在 ORM 层拦，管不住手工 SQL、
    Alembic 数据回填、运维脚本，而"版本被改过"是**静默失败**
    （无异常、无日志，事后无法分辨是被改过还是本来如此）。
  · `trg_version_dependencies_scope` —— 两端都有项目归属时必须同项目。
    "跨项目版本引用拒绝"是 WP11 的验收项；CHECK 只能看当前行，
    跨行判断只能靠触发器。服务层也有一道（给出可读的 422），两道互补。

触发器 DDL 由 `rednote.modules.versions.ddl` 生成而不是在这里手写：
验证脚本要按名字回查 `pg_trigger`，两边各写一遍名字迟早不一致，
会造成"检查假通过"。

---

## 【铁律 G2 —— 每次 autogenerate 后必读】

本次 autogenerate **第 N 次**提议删除 `smoke_meta` 与 `wp05_vec_probe`
（M0 冒烟脚本建的探针表，不在 SQLAlchemy 模型里），且 downgrade 试图用
`sa.NullType()` 重建 `vector` 列 —— autogenerate 无法识别该类型，
重建出的表结构是错的。

已人工删除全部 `op.drop_table` / `op.drop_index` 语句，并重写 downgrade。
**这两张探针表不属于本项目模型，永远不要删**：上一轮迁移用它俩的行数
作为"残留表未被误删"的证据。
来源：`docs/待定项与问题登记册.md` §4.3 的 ISS-13 / ISS-14。

---

## downgrade 顺序（不可调换）

先删触发器，再删表。倒过来的话，`DROP TABLE` 会连带删掉触发器
（PG 允许），但**函数不会自动删除** —— 残留的 `rednote_reject_mutation()`
函数会在下次重装时与新函数同名，虽然 `CREATE OR REPLACE` 能覆盖，
但"删干净"才是可验证的状态（验证脚本会查 `pg_proc`）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from rednote.modules.versions.ddl import all_trigger_ddl, consistency_items

# revision identifiers, used by Alembic.
# 用 repr() 而非 "Ellipsis"：字符串插值会把 Python 的 None 渲染成字符串 "None"，
# 导致 Alembic 解析 down_revision 时 KeyError: 'None'（本机实测踩到过）。
revision: str = "cedfe85c1bf9"
down_revision: str | Sequence[str] | None = "3d13e6a04483"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """正向迁移：建表 → 建索引 → 建触发器。"""
    op.create_table(
        "version_nodes",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("entity_type", sa.String(length=30), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column(
            "schema_version", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "entity_type IN ('BASELINE', 'BRIEF', 'CONTENT', 'KNOWLEDGE', "
            "'MEDIA', 'PLAN', 'PROMPT', 'REPORT')",
            name=op.f("ck_version_nodes_entity_type_valid"),
        ),
        sa.CheckConstraint(
            "revision_no >= 1 AND revision_no <= 2147483647",
            name=op.f("ck_version_nodes_revision_no_in_range"),
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name=op.f("ck_version_nodes_schema_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_version_nodes_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_version_nodes_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_version_nodes")),
        sa.UniqueConstraint(
            "entity_type",
            "entity_id",
            "revision_no",
            name="uq_version_nodes_entity_type_entity_id_revision_no",
        ),
    )
    op.create_index(
        "ix_version_nodes_entity_revision",
        "version_nodes",
        ["entity_type", "entity_id", "revision_no"],
        unique=False,
    )
    op.create_index(
        "ix_version_nodes_project_id", "version_nodes", ["project_id"], unique=False
    )

    op.create_table(
        "version_dependencies",
        sa.Column("parent_version_id", sa.UUID(), nullable=False),
        sa.Column("dependency_version_id", sa.UUID(), nullable=False),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("selected_fragment_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "parent_version_id <> dependency_version_id",
            name=op.f("ck_version_dependencies_no_self_reference"),
        ),
        sa.ForeignKeyConstraint(
            ["dependency_version_id"],
            ["version_nodes.id"],
            name=op.f("fk_version_dependencies_dependency_version_id_version_nodes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_version_id"],
            ["version_nodes.id"],
            name=op.f("fk_version_dependencies_parent_version_id_version_nodes"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "parent_version_id",
            "dependency_version_id",
            "purpose",
            name=op.f("pk_version_dependencies"),
        ),
    )
    op.create_index(
        "ix_version_dependencies_dependency",
        "version_dependencies",
        ["dependency_version_id"],
        unique=False,
    )

    # 触发器最后建：它们引用表，表必须已存在。
    for _table, function_ddl, trigger_ddl, _drop_trigger, _drop_function in (
        all_trigger_ddl()
    ):
        op.execute(sa.text(function_ddl))
        op.execute(sa.text(trigger_ddl))


def downgrade() -> None:
    """反向迁移：先删**全部**触发器，再删**本模块自己的**函数，最后删表。

    **必须分两趟**，不能"删一个触发器、删一个函数"地配对处理：
    `rednote_reject_mutation()` 被 version_nodes 与 version_dependencies
    两张表的触发器共用。配对处理时，删到第一张表就会去 DROP FUNCTION，
    而此时第二张表的触发器还在引用它 —— PG 直接拒绝：

        ERROR: cannot drop function rednote_reject_mutation() because
               other objects depend on it
        HINT:  Use DROP ... CASCADE to drop the dependent objects too.

    （本机 downgrade 实测踩到。当时整条迁移在事务里回滚，
    所以没有留下半成品状态 —— 但 downgrade 从此不可用，
    而 WP47 要求恢复演练必须能回滚。）

    **不用 CASCADE**：CASCADE 会把"依赖它的其他对象"一起删掉，
    而这里唯一应当被"顺手删掉"的正是我们要显式删的那些触发器。
    用 CASCADE 等于把"删对了没有"这件事交给 PG 猜；
    分两趟则每一步都是显式且可核对的。

    ---

    ## ★ ISS-79（2026-10-05 修复）：第 2 步按**归属**而不是按"表清空了吗"取范围

    上面那段"分两趟就安全了"的判断**只在本迁移内部成立**，它漏掉了一个
    更强的情况：**别的包的表**也挂着同一个共用函数，而它们在本迁移回退时
    **还没有被删**（回退是逆序：WP25→…→WP15→**WP11**，但 WP15/WP16/WP17/
    WP18/WP20/WP22/WP23/WP25 的表在各自 `downgrade` 里删掉了表，
    触发器随之消失、函数却**仍在**；而 WP11 自己回退时，`version_nodes`
    的不可变触发器是**在同一个 downgrade 里**才被删的）。

    实测失败形态（`alembic downgrade base`，WP15 那一步先撞）：

        ERROR: cannot drop function rednote_reject_mutation() because
               other objects depend on it
        DETAIL: trigger trg_version_nodes_immutable on table version_nodes

    ★ **本文件的修正重点与 WP15 不同**：本包**同时**拥有两类函数 ——
      · `rednote_reject_mutation` → **别人的**（WP11 建的，但全项目共用）⇒ 不删；
      · `rednote_check_dependency_scope` → **我的**（只被本包的
        `trg_version_dependencies_scope` 引用）⇒ **必须删**。

    因此第 2 步的覆盖范围**不能**写成"空集"，也不能写成 `all_trigger_ddl()`
    （它混着不可变项）。唯一正确的来源是
    `versions/ddl.py::consistency_items()` —— 它**只**返回 scope 那一项，
    于是"删我的、留别人的"由代码结构保证，而不是靠人记得。

    不得改回 `{item[4] for item in all_trigger_ddl()}`：
    那会重新把 `DROP FUNCTION IF EXISTS rednote_reject_mutation()` 拉进来。
    该不变式由 `test_migration_roundtrip.py` 静态守（扫描全部迁移的
    downgrade 禁止该形态）+ 真库往返（`upgrade head → downgrade base →
    upgrade head`）动态守。
    """
    ddl_items = all_trigger_ddl()

    # ---- 1. 先删全部触发器（含不可变保护，它们全是我建的）----
    # 顺序不能反：函数仍被触发器引用时 PG 会拒绝 DROP FUNCTION。
    for _table, _function_ddl, _trigger_ddl, drop_trigger, _drop_function in ddl_items:
        op.execute(sa.text(drop_trigger))

    # ---- 2. 只删本模块自己的函数（scope 一个；**绝不碰 rednote_reject_mutation**）----
    for _table, _function_ddl, _trigger_ddl, _drop_trigger, drop_function in (
        consistency_items()
    ):
        op.execute(sa.text(drop_function))

    op.drop_index(
        "ix_version_dependencies_dependency", table_name="version_dependencies"
    )
    op.drop_table("version_dependencies")
    op.drop_index("ix_version_nodes_project_id", table_name="version_nodes")
    op.drop_index("ix_version_nodes_entity_revision", table_name="version_nodes")
    op.drop_table("version_nodes")
