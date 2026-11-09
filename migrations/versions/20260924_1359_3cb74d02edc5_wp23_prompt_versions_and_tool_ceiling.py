"""WP23：Prompt 版本与工具权限（3 张表 + 6 个触发器）。

revision: 3cb74d02edc5
down_revision: ebc05dfca16b

**本迁移包含 3 张表**：

| 对象 | 来源 | 说明 |
|---|---|---|
| `prompt_templates` | 数据结构 02 §160 | 模板**身份**。**可变**（`active_version_id` 是被投影的列） |
| `prompt_versions` | 数据结构 02 §161 | 模板版本：**不可变**，`id` 同时 FK `version_nodes.id` |
| `prompt_publications` | **本包新增**（协议 03 §9 第 13 行「激活，记录影响范围」的落点） | 激活事件流：**不可变**，`sequence` 每模板内递增 |

**6 个触发器，分两类**：

| 触发器 | 挂在哪 | 作用 |
|---|---|---|
| `trg_prompt_versions_immutable` | `prompt_versions` | 拦 UPDATE / DELETE（不可变证据） |
| `trg_prompt_publications_immutable` | `prompt_publications` | 同上 |
| `trg_prompt_versions_node_consistency` | `prompt_versions` | **OP-88 正向**：延迟校验 `version_nodes` 的 entity 与本表一致 |
| `trg_version_nodes_prompt_paired` | `version_nodes` | **OP-88 反向**：`PROMPT` 节点必须有对应子表行（`WHEN` 限定到本包负责的 entity_type） |
| `trg_prompt_publications_template_scope` | `prompt_publications` | 激活事件引用的版本必须属于该模板（含 `superseded_version_id`） |
| `trg_prompt_templates_active_scope` | `prompt_templates` | 生效版本必须属于本模板 |

触发器 DDL **不写在本文件里**，而是调用 `modules/prompts/ddl.py` 的
`all_trigger_ddl()` —— 与测试完全同源。两份不一致的 DDL 是本项目
反复踩过的坑（WP11 / WP16 / WP17 / WP18 / WP20 各一次）。

---

## 一、建表顺序：依赖序，且**本包需要一次 `ALTER TABLE` 拆环**

    version_nodes（WP11 已存在）
        ↑ id                        ↑ id
    prompt_templates ──→ prompt_versions ──→ prompt_publications
        ↑ active_version_id             ↑ version_id
        └───────────────────────────────┘

**这是一个真环**（与 WP20 不同）：

  · `prompt_versions.template_id` → `prompt_templates.id`（版本属于模板）
  · `prompt_templates.active_version_id` → `prompt_versions.id`（模板的当前版本）

WP20 的 `knowledge_assets ↔ knowledge_versions` 恰好是**无环**的
（asset 只引用 `version_nodes`，不引用自己的版本表）——
所以 WP20 的迁移里没有 `ALTER TABLE`。本包**有**，必须拆：

  1. `CREATE TABLE prompt_templates`（**不带** `active_version_id` 的 FK）
  2. `CREATE TABLE prompt_versions`（带 `template_id` 的 FK）
  3. `ALTER TABLE prompt_templates ADD CONSTRAINT ... active_version_id → prompt_versions.id`
  4. `CREATE TABLE prompt_publications`

**这与 WP18 的 `briefs.draft_id ↔ brief_drafts.brief_id` 是同一手法**。
判据：**表级环必须靠建表顺序拆开，`DEFERRABLE` 救不了它**
（WP18 的 ISS-60：autogenerate 按字母序建表必失败，而延迟外键
只管行级插入顺序，不管"表是否存在"）。

## 二、`DEFERRABLE INITIALLY DEFERRED` 在这里管什么

管**行级**插入顺序，不管表存在性。`prompt_versions.id` 与
`prompt_templates.active_version_id` 都指向 `version_nodes` / 对方，
而节点行与业务行由同一个事务、两个不同代码路径写入
（`versions.service.register_version` 内部有 SAVEPOINT + 重试取号，
不暴露"节点行何时落库"）→ 若立即校验，应用必须猜对插入顺序。

WP18 已实测：延迟触发器在 INSERT 时静默通过、在 COMMIT 时抛
`sqlstate=23514`。

## 三、`allowed_tools` 用 `text[]` 而不是规格源写的 `jsonb`

规格源 02 §161 写的是 `allowed_tools jsonb`。**本包改用 `text[]`**：

    allowed_tools <@ ARRAY['KNOWLEDGE_READ', ...]::text[]

**`<@` 是"被包含于"，正好是子集关系**。用 `jsonb` 时数据库层**无法**
用 CHECK 表达子集（jsonb 的元素类型还不受约束：`["a", 1, {"b":2}]`
是合法 jsonb）。

**取舍判据**：验收原文「**不扩大工具权限**」必须有"绕过 service 也拦得住"
的防线，而"与规格源的列类型一致"是形式要求。**能落成列是设计约束，
不是风格偏好** —— 这一点在说明文档 §3 的 V3 对照里详细论证过。

代价（诚实记录）：与规格源 §161 的列类型不一致，已在说明文档标注。

## 四、autogenerate 产物手工改动的四处（G2 铁律）

1. **`postgresql` 导入必须补**：本包用了 `JSONB` + 两种 `ARRAY`
   （`ARRAY(Text)` 与 `postgresql.ARRAY(sa.Text())`），
   autogenerate 不会自动加这行 import —— 结果是迁移文件语法正确、
   diff 看着正常，**执行时 `NameError: name 'postgresql' is not defined`**。
2. **`upgrade` 的建表顺序与 `ALTER TABLE` 拆环**：autogenerate 按字母序，
   在真环上必失败。
3. **`downgrade` 不得 `drop_table` 探针表**：autogenerate 会重建
   `smoke_meta` / `wp05_vec_probe`（且 `vector` 列被渲染成 `sa.NullType()`）。
4. **触发器**：autogenerate 不生成，手工追加。

## 五、`downgrade` 的对象归属判据（ISS-62）

**判据是「哪些对象是我建的」，不是「哪些对象跟我的表有关」**：

  · 挂在我表上、由我建的 5 个触发器 + 1 个挂在**共用表**
    `version_nodes` 上的触发器 → **全删**；
    ⚠️ 但绝不能 `DROP TABLE version_nodes`；
  · 4 个 `rednote_check_prompt_*` 函数 → **是我建的，删**；
  · `rednote_reject_mutation()` → 别人建的共用函数，**不删**
    （删它会让 WP15/WP16/WP17/WP18/WP20 的保护一起失效，
    且撞 "cannot drop function ... because other objects depend on it"）。

`ddl.consistency_items()` 存在的意义就是让第 2 步自动只覆盖本模块自己的
函数 —— 手写一遍会漏、会多删。**不得**改用 `all_trigger_ddl()`。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# G2 铁律第三件：`postgresql` 导入必须补（见模块 docstring 第四节第 1 条）。

# revision identifiers, used by Alembic.
revision: str = "3cb74d02edc5"
down_revision: str | Sequence[str] | None = "ebc05dfca16b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: 工具上界的 CHECK 表达式。**与 `types.TOOL_CEILING_ARRAY_SQL` 同源** ——
#: 迁移里手写一遍会让"上界"有两个定义，改一处忘另一处就是**静默的扩权**。
#: 因此这里 import 常量而不是复制字面量。
def _tool_ceiling_sql() -> str:
    from rednote.modules.prompts.types import TOOL_CEILING_ARRAY_SQL

    return TOOL_CEILING_ARRAY_SQL


def _task_kind_sql() -> str:
    from rednote.modules.prompts.types import TASK_KIND_LIST_SQL

    return TASK_KIND_LIST_SQL


def _publication_action_sql() -> str:
    from rednote.modules.prompts.types import PUBLICATION_ACTION_LIST_SQL

    return PUBLICATION_ACTION_LIST_SQL


def upgrade() -> None:
    """正向迁移（建表顺序 + 拆环，见模块 docstring 第一节）。"""
    # ---- 第 1 张：prompt_templates（**先不带** active_version_id 的 FK）----
    # 环：prompt_versions.template_id → 本表，而本表的 active_version_id
    # → prompt_versions。因此先建本表但不加那个 FK，等第 2 张建完再 ALTER。
    op.create_table(
        "prompt_templates",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("task_kind", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        # 本列在第 3 步 ALTER 里补 FK。**先建列、后加约束**。
        sa.Column("active_version_id", sa.UUID(), nullable=True),
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
        sa.CheckConstraint(
            f"task_kind IN ({_task_kind_sql()})",
            name=op.f("ck_prompt_templates_task_kind_valid"),
        ),
        sa.CheckConstraint(
            "char_length(name) > 0",
            name=op.f("ck_prompt_templates_name_not_empty"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_templates")),
        # 同一 task_kind 下模板名唯一。**不是全局唯一**：
        # `CONTENT_COPY` 与 `REPORT_GENERATE` 各有一个叫"标准版"的模板
        # 是完全正常的，而全局唯一会拒绝它。
        sa.UniqueConstraint(
            "task_kind", "name", name=op.f("uq_prompt_templates_kind_name")
        ),
    )
    op.create_index(
        "ix_prompt_templates_task_kind",
        "prompt_templates",
        ["task_kind", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_prompt_templates_created_at",
        "prompt_templates",
        ["created_at"],
        unique=False,
    )

    # ---- 第 2 张：prompt_versions ----
    # `id` 无 DEFAULT：它由版本注册（WP11）给出，且同时 FK version_nodes.id。
    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("template_id", sa.UUID(), nullable=False),
        sa.Column("system_template", sa.Text(), nullable=False),
        sa.Column(
            "input_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "output_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        # **text[] 而不是 jsonb**（见模块 docstring 第三节）。
        sa.Column(
            "allowed_tools",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("change_reason", sa.String(length=500), nullable=False),
        sa.Column(
            "schema_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "char_length(system_template) > 0",
            name=op.f("ck_prompt_versions_system_template_not_empty"),
        ),
        sa.CheckConstraint(
            "char_length(system_template) <= 20000",
            name=op.f("ck_prompt_versions_system_template_length"),
        ),
        sa.CheckConstraint(
            "char_length(change_reason) > 0",
            name=op.f("ck_prompt_versions_change_reason_not_empty"),
        ),
        sa.CheckConstraint(
            "char_length(change_reason) <= 500",
            name=op.f("ck_prompt_versions_change_reason_length"),
        ),
        # 两个 schema 必须是 object（数组/标量都不是"结构声明"）。
        # **不判非空** —— 一个不需要输入字段的模板写 `{}` 是合法的。
        sa.CheckConstraint(
            "jsonb_typeof(input_schema) = 'object'",
            name=op.f("ck_prompt_versions_input_schema_is_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(output_schema) = 'object'",
            name=op.f("ck_prompt_versions_output_schema_is_object"),
        ),
        # --- 验收原文「不扩大工具权限」的**数据库层兜底** ------------------
        # `<@` 是 PostgreSQL 的"被包含于"（is contained by），正好是子集关系。
        # **为什么必须在数据库层**：service 层的校验给的是可读错误，
        # 但绕过 service 的写入路径（数据回填、导入脚本、psql 排障）它管不住。
        # 而"扩权"的失败形态是**静默的** —— 没有任何异常。
        sa.CheckConstraint(
            f"allowed_tools <@ {_tool_ceiling_sql()}",
            name=op.f("ck_prompt_versions_allowed_tools_within_ceiling"),
        ),
        # 延迟外键：注册表与具体表同事务插入，顺序由 WP11 的取号重试决定。
        sa.ForeignKeyConstraint(
            ["id"],
            ["version_nodes.id"],
            name=op.f("fk_prompt_versions_id_version_nodes"),
            ondelete="RESTRICT",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["prompt_templates.id"],
            name=op.f("fk_prompt_versions_template_id_prompt_templates"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_prompt_versions_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_versions")),
    )
    op.create_index(
        "ix_prompt_versions_template_id",
        "prompt_versions",
        ["template_id", "created_at"],
        unique=False,
    )

    # ---- 第 3 步：拆环 —— 补 prompt_templates.active_version_id 的 FK ----
    # 环的另一半在这里接上（第 1 步刻意留空）。
    # **判据是"表存在性"而不是"行存在性"**：此刻 prompt_versions 已建出，
    # 因此这条 FK 可以被立即校验。`DEFERRABLE INITIALLY DEFERRED` 管的是
    # 行级插入顺序 —— 激活事务里版本行与模板行同事务写入，顺序不可猜。
    op.create_foreign_key(
        op.f("fk_prompt_templates_active_version_id_prompt_versions"),
        "prompt_templates",
        "prompt_versions",
        ["active_version_id"],
        ["id"],
        ondelete="RESTRICT",
        initially="DEFERRED",
        deferrable=True,
    )

    # ---- 第 4 张：prompt_publications ----
    op.create_table(
        "prompt_publications",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("template_id", sa.UUID(), nullable=False),
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        # 事件在**本模板内**的序号（从 1 开始）。投影的排序键。
        # `now()` 在 PG 里是**事务开始时刻**，同一事务插入的
        # ACTIVATE(v2) 与 SUPERSEDE(v1) 时间戳完全相同，
        # 按 created_at 排序得到的是不确定的顺序（WP17 ISS-52 踩过）。
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("superseded_version_id", sa.UUID(), nullable=True),
        # 「记录影响范围」的落点。**冗余存一份 task_kind 快照**而不是
        # JOIN 回 prompt_templates：**事件是历史事实**，而 task_kind
        # 将来若被改，JOIN 出来的会是"现在的值"而不是"当时的值"。
        sa.Column(
            "affected_task_kinds",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"action IN ({_publication_action_sql()})",
            name=op.f("ck_prompt_publications_action_valid"),
        ),
        sa.CheckConstraint(
            "sequence >= 1",
            name=op.f("ck_prompt_publications_sequence_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_prompt_publications_actor_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_version_id"],
            ["prompt_versions.id"],
            name=op.f("fk_prompt_publications_superseded_version_id_prompt_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["prompt_templates.id"],
            name=op.f("fk_prompt_publications_template_id_prompt_templates"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["prompt_versions.id"],
            name=op.f("fk_prompt_publications_version_id_prompt_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_publications")),
        # 同一模板内序号唯一 —— 并发激活时"两人同时取到同一个号"的拦截面
        # （先查再插之间有窗口，唯一约束是唯一防线，同 WP11/WP20）。
        sa.UniqueConstraint(
            "template_id",
            "sequence",
            name=op.f("uq_prompt_publications_template_sequence"),
        ),
    )
    op.create_index(
        "ix_prompt_publications_template_sequence",
        "prompt_publications",
        ["template_id", "sequence"],
        unique=False,
    )
    op.create_index(
        "ix_prompt_publications_version_id",
        "prompt_publications",
        ["version_id"],
        unique=False,
    )

    # ---- 触发器：autogenerate **不生成触发器**，必须手工补 ----
    # DDL 来源与测试完全同源（modules/prompts/ddl.py 的 all_trigger_ddl），
    # 因此不会出现"迁移建的触发器与测试断言的名字不一致"。
    #
    # 6 个触发器，分两类：
    #   · 2 个不可变保护（prompt_versions / prompt_publications）
    #   · 4 个跨表一致性（OP-88 正向 + OP-88 反向 + 激活范围 + 生效版本范围）
    #
    # ⚠️ 反向触发器 `trg_version_nodes_prompt_paired` 挂在**共用表**
    # `version_nodes` 上，但它是**本包建的**（判据：这个对象是不是我建的）——
    # 因此它出现在本迁移里，也会出现在本迁移的 downgrade 里。
    from rednote.modules.prompts import ddl as prompts_ddl

    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in prompts_ddl.all_trigger_ddl():
        op.execute(_function_ddl)
        op.execute(_trigger_ddl)


def downgrade() -> None:
    """反向迁移。

    **手工重写，不用 autogenerate 的产物。** 原因有三：

    1. autogenerate 会在 downgrade 里**重建**探针表 smoke_meta / wp05_vec_probe，
       且 `wp05_vec_probe.embedding` 被渲染成 `sa.NullType()`（它不认识 vector
       类型）—— 重建出来是错的。G2 铁律要求连这条一起删。
    2. 触发器必须显式 DROP，且**顺序是先触发器后函数** ——
       函数仍被触发器引用时 PG 会拒绝删除（WP11 实测踩到过）。
    3. **拆环的反向**：`prompt_templates.active_version_id` 的 FK 必须先删，
       再删 `prompt_versions` —— 否则 PG 拒绝删被引用的表。

    **只删本包自己的触发器与函数，不删共用函数。**
    `rednote_reject_mutation()` 是所有不可变表共用的函数，
    生命周期归**引入它的那个迁移**（WP11）。删它会让 WP15/WP16/WP17/WP18/WP20
    的不可变保护一起失效，且撞 "cannot drop function ... because other
    objects depend on it"（WP16 差点再踩）。

    **判据是「哪些对象是我建的」，不是「哪些对象跟我的表有关」**（ISS-62）：
      · 挂在我表上、由我建的 5 个触发器 + 1 个挂在**共用表** version_nodes 上
        的触发器 → **全删**；⚠️ 但绝不能 `DROP TABLE version_nodes`；
      · 4 个 `rednote_check_prompt_*` 函数 → **是我建的，删**；
      · `rednote_reject_mutation()` → 别人建的共用函数，**不删**。

    `ddl.consistency_items()` 存在的意义就是让第 2 步自动只覆盖本模块自己的
    函数 —— 手写一遍会漏、会多删。**不得**改用 `all_trigger_ddl()`
    （它混着不可变保护那 2 项，其 `drop_function` 指向共用函数）。
    """
    from rednote.modules.prompts import ddl as prompts_ddl

    # ---- 1. 先删触发器（顺序：触发器 DROP 必须先于函数 DROP）----
    #
    # 注意 `trg_version_nodes_prompt_paired`：它挂在 WP11 的 version_nodes 上，
    # 但**它是我建的** → 我删。这与"删共用函数"是两件不同的事。
    for _table, _function_ddl, _trigger_ddl, _drop_trigger, _drop_function in (
        prompts_ddl.all_trigger_ddl()
    ):
        op.execute(_drop_trigger)

    # ---- 2. 只删本模块自己的函数（不碰 rednote_reject_mutation）----
    for _table, _function_ddl, _trigger_ddl, _drop_trigger, _drop_function in (
        prompts_ddl.consistency_items()
    ):
        op.execute(_drop_function)

    # ---- 3. 再删表（**反序**：先删引用别人的，后删被引用的）----
    # 3.1 prompt_publications：引用 prompt_versions 与 prompt_templates。
    op.drop_index(
        "ix_prompt_publications_version_id", table_name="prompt_publications"
    )
    op.drop_index(
        "ix_prompt_publications_template_sequence",
        table_name="prompt_publications",
    )
    op.drop_table("prompt_publications")

    # 3.2 **拆环的反向**：先摘掉 prompt_templates 指向 prompt_versions 的 FK，
    # 否则下一步 drop_table("prompt_versions") 会撞依赖错误。
    op.drop_constraint(
        op.f("fk_prompt_templates_active_version_id_prompt_versions"),
        "prompt_templates",
        type_="foreignkey",
    )

    # 3.3 prompt_versions：引用 version_nodes 与 prompt_templates。
    op.drop_index("ix_prompt_versions_template_id", table_name="prompt_versions")
    op.drop_table("prompt_versions")

    # 3.4 prompt_templates：被 prompt_versions 引用（FK 已在上一步随表删除）。
    op.drop_index("ix_prompt_templates_created_at", table_name="prompt_templates")
    op.drop_index("ix_prompt_templates_task_kind", table_name="prompt_templates")
    op.drop_table("prompt_templates")
