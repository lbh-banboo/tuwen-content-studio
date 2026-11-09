"""WP24：场景形式草稿与数量配置（2 张表 + 1 个触发器 + 2 条部分唯一索引）。

revision: 022ba9804d66
down_revision: 185ec01bed91

**本迁移包含 2 张表**：

| 对象 | 性质 | 说明 |
|---|---|---|
| `runtime_settings` | **不可变**（追加式） | ADR-13 的配置历史。每次变更插一行；当前生效值 = 同 `(key, scope_kind, project_id)` 下 `revision_no` 最大那行 |
| `plan_drafts` | **可变**（`lock_version`） | 步骤 4 的工作区（场景比例 + 形式比例 + 配对）。一项目一份 |

---

## 一、G2 铁律的执行记录（本文件是手工写全的）

与 WP18 / WP20 / WP21 / WP22 / WP23 同一做法：**只取 `revision` /
`down_revision` 两个值，其余手工写全**。理由见
`docs/m2/WP22-ddl-草案.py` §一 —— autogenerate 在本项目反复产出
"删探针表 / 丢部分索引 WHERE / 不含触发器"的产物，逐条核对的手工成本
高于直接写全。

三件事已逐条处理（G2 铁律）：

1. **删掉 autogenerate 提议的 M0 探针表 `op.drop_table`** ——
   见 `downgrade()` 末尾的注释块（列举了被刻意剔除的语句，便于评审复核）；
2. **`downgrade` 不重建任何本包不拥有的对象** —— 尤其**不删
   `rednote_reject_mutation()`**（它属于 `platform`，被全项目共用；
   删了会让 WP11/WP15/WP16/WP17/WP18/WP20/WP22/WP23 的保护一起失效）；
3. **`from sqlalchemy.dialects import postgresql` 已补** —— 本迁移用了
   `postgresql.JSONB`（两张表都有 JSONB 列）。autogenerate 用了它却
   **不会自动加这行 import**，结果是迁移文件语法正确、diff 看着正常，
   **执行时 `NameError`**。本坑在本项目已连续触发 9 次（登记册 G2 / ISS-02）。

## 二、★ 两条**部分唯一索引** —— 本迁移最容易被 autogenerate 悄悄毁掉的东西

| 索引 | 键 | `WHERE` | 漏写 `WHERE` 的后果 |
|---|---|---|---|
| `uq_runtime_settings_global` | `(key, revision_no)` | `scope_kind = 'GLOBAL'` | 全局行**无任何唯一性**（`project_id IS NULL`，PG 认为 `NULL != NULL`）⇒ 同一 `(key, 1)` 可无限重复插入 |
| `uq_runtime_settings_project` | `(key, project_id, revision_no)` | `scope_kind = 'PROJECT'` | 同一项目同一 key 可被并发写进两个相同的 `revision_no` |

**为什么这两条不是"性能优化"而是结构基础**：没有它们，
"当前生效值"这条查询

    SELECT value FROM runtime_settings
     WHERE key = $1 AND scope_kind = 'GLOBAL'
     ORDER BY revision_no DESC LIMIT 1

在 `revision_no` 相同的两行里**返回哪一行没有保证**。API 双实例
（ADR-11 C8）于是可能读到**不同的"当前上限"**，而两者都不报错 ——
正是 R2/R3 那类"两个数字都对得上合计、但内容不同"的故障形态。

**为什么用部分索引而不是 `COALESCE(project_id, sentinel)`**：
哨兵值会污染数据（每行都存一个假的 project_id），且"哨兵是那一个 UUID"
这个约定**本身没有任何约束保证** —— 有人写别的值就静默失效。
部分索引把 `project_id` 从**全局行的键里去掉**，问题从根上不存在。

⚠️ **部分唯一索引在 `pg_constraint` 里根本不出现** —— 只查约束表的核对
**看不到**这两条防线。核对必须查 `pg_indexes.indexdef`（WP22 已登记此坑）。

⚠️ **`WHERE` 写错 = 索引静默失效**（PG 不报错，索引照建、永远不命中）。
因此证据脚本做**双向**活性验证：造一个**不匹配** `WHERE` 的重复行断言它
**能插进去**（证明索引确实在按 `WHERE` 过滤），再造一个**匹配**的重复行
断言被拒（`23505`）。只做后者会漏掉"`WHERE` 永远命不中"这一整类失效。

**本迁移不重写这两条索引的 SQL**，而是调
`modules/planning/ddl.py` 的 `partial_unique_index_ddl()` ——
与测试、证据脚本**同源**。三处各写一遍是本项目反复踩过的坑
（`ddl.py` 模块 docstring 第 8~24 行有完整的"为什么"）。

## 三、1 个触发器（DDL 不写在本文件里）与"本包没有跨表一致性触发器"

触发器 DDL 来自 `modules/planning/ddl.py` 的 `all_trigger_ddl()` ——
与测试完全同源。

| # | 触发器 | 挂在哪张表 | 类型 |
|---|---|---|---|
| 1 | `trg_runtime_settings_immutable` | `runtime_settings` | BEFORE UPDATE/DELETE（复用共用函数 `rednote_reject_mutation`） |

**`plan_drafts` 刻意不挂不可变触发器**：它是工作区，运营反复调比例是
正常用法（`models.py` docstring 有论证）。

**本包没有跨表一致性触发器**（`ddl.consistency_items()` 返回空列表）——
这是**有意的显式形状**而不是"忘了写"：`runtime_settings` 与 `plan_drafts`
都不与别的模块共享表结构，本包没有"注册表 + 领域表必须一致"这类跨表
不变量。WP18/WP20/WP22/WP23 的 OP-88 那一半在各自包里闭合，
而 `PLAN` 的领域表 `plan_drafts` **不参与** `version_nodes` 配对 ——
草稿是工作区，不是版本。

**`CREATE TRIGGER` 不是幂等的**（重复执行报 `DuplicateObject`，本机实测
复现于 WP22）。本迁移的 `upgrade` 走 `CREATE OR REPLACE FUNCTION` +
`CREATE TRIGGER`，在**空库 / 该迁移首次执行**的前提下次次成功。

## 四、建表顺序（依赖序，表级无环）

    runtime_settings        （引用 projects / users，均已存在）
        ↓
    plan_drafts             （引用 projects / production_baselines / users，均已存在）

两张表的全部被引用对象都由**更早的迁移**建好（`projects`/`users` 属
M1，`production_baselines` 属 `185ec01bed91` = 本迁移的 `down_revision`）。
因此**不需要 `ALTER TABLE ... ADD CONSTRAINT` 分两步**，也不需要
`DEFERRABLE`（本包没有"注册表行与子表行由不同代码路径写入"那种时序问题）。

## 五、数据回填：调 `bootstrap_defaults`，**不写裸 SQL**

`upgrade()` 末尾调 `settings_service.bootstrap_defaults(...)` 写入
ADR-13 的初始 `GLOBAL` 行（`batch.limits = {TRIAL:10, SCALE:30, BULK:50}`），
并为**存量项目**回填 `INHERITED` 副本（OP-60 的 §3.6.3 路径 a）。

**为什么不在迁移里写一遍 `{"TRIAL":10,…}` 的裸 SQL**：
迁移写一遍、测试断言一遍 —— 两处一旦不一致，**测试会通过**
（它断言的是自己写的那份）。调同一个函数让值只有一个来源
（`types.DEFAULT_BATCH_MAX`）。

**幂等**：已存在 `GLOBAL` 行时不动它，也不产生新的修订号 ——
迁移可重入，而重复执行不该让"第 2 版"毫无理由地出现。

**为什么用 ORM Session 而不是 `op.execute` 裸 DML**：`_append_setting`
里的取号（`max_revision_no + 1`）与 `SAVEPOINT` 包裹是该模块的**唯一**
写入口径；在迁移里手写一遍 `INSERT ... SELECT max(...)+1` 就是第四处
副本，且它没有 `savepoint` 保护 —— 一旦撞唯一约束会把整个迁移事务
标记为 aborted。

⚠️ **会话绑定到 Alembic 的连接，不 commit**：`Session(bind=op.get_bind())`
让它加入 Alembic 已在跑的那个事务，提交由 Alembic 统一做。
**若在这里 `session.commit()`，会把"建表 + 回填"拆成两个事务** ——
中间态失败时留下"表建好了、配置是空的"，而 `plan_drafts.total_quantity`
的下游校验读到空配置会静默用默认值。

## 六、`downgrade()` 的代价（必须显式登记）

`downgrade()` 删表 ⇒ **配置历史一并丢失**。ADR-13 要求审计
"不可被后续修改或删除，与费用账本、版本快照同级对待"，
而 `DROP TABLE` 比"改一行"更彻底。

**这不构成"因此不准 downgrade"** —— 本仓的恢复演练（WP47 的 RPO/RTO）
需要 `downgrade` 可用（留 `NotImplementedError` 会让任何回滚尝试直接失败）。
正确处置是**把代价写明白**：回退前须导出 `runtime_settings` 全量
（`COPY ... TO`），恢复时用 `downgrade` 之后的 `upgrade` + 回灌。

## 七、落地实测记录（2026-09-28）

见本文件末尾的 `# 落地实测` 注释块（由收口脚本
`tools/m3/collect_wp24_evidence.py` 核对，不在这里重复计数）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# G2 铁律第三件：`postgresql` 导入必须补（本迁移用了 JSONB）。
# autogenerate 不会自动加这行 —— 结果是语法正确但执行时 NameError。
# 本坑在本项目已连续触发 9 次（登记册 G2 / ISS-02）。
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "022ba9804d66"
down_revision: str | Sequence[str] | None = "185ec01bed91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """正向迁移。建表顺序见模块 docstring 第四节（依赖序，表级无环）。"""
    # ### commands auto generated by Alembic - please adjust! ###
    # （本文件手工写全，理由见模块 docstring 第一节）

    # ---- 第 1 张：runtime_settings（**不可变**，追加式配置历史）---------
    # 只引用 projects / users，因此排在最前。
    #
    # ⚠️ **本表没有 update/delete 路径**（`repository.py` 只有
    # `insert_setting` / `insert_inherited_copy` 两个写入方法）。
    # 这不是"还没写"，是设计：当前生效值 = `revision_no` 最大那行，
    # 改配置 = 插新行。追加式让"变更前值"天然可读（就是上一行），
    # 而不是靠审计表自己带快照 —— 后者会造出**第二个真值来源**。
    op.create_table(
        "runtime_settings",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # 配置键。取值域是**闭集**（`types.SETTING_KEY_LIST_SQL`）。
        # 闭集拦的是"拼错键名会静默成为新配置项"（`batch.limt` → 改了不生效
        # 且不报错），**拦不住形状错** —— 形状由 `settings_service`
        # 按 key 分派校验（R8 的处置）。
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("scope_kind", sa.String(length=20), nullable=False),
        # 项目级配置指向的项目；`GLOBAL` 时**必须为 NULL**（见下方 CHECK）。
        # `ON DELETE RESTRICT`：项目被删时不允许静默丢掉它的配置历史。
        sa.Column("project_id", sa.UUID(), nullable=True),
        # JSONB 而不是 text：三种键的形状不同（`batch.limits` 是三档 map、
        # `research.maxEffectiveFiles` 是一个数、`platform.mediaLimits`
        # 是嵌套 map）。代价是数据库层无法对内容做 CHECK —— 已登记的取舍。
        sa.Column(
            "value",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        # 同一 `(key, scope_kind, project_id)` 下的**显式递增序号**。
        # **不用 `created_at` 排序**：`now()` 在 PostgreSQL 里是**事务开始
        # 时刻**，同一事务内插入的多行时间戳完全相同，按它排序得到的是
        # **不确定的顺序**（WP17 的 ISS-52 实证）。
        sa.Column("revision_no", sa.Integer(), nullable=False),
        # 行的来源（OP-60 三态）：GLOBAL / INHERITED / MANUAL。
        # "是否跟随全局变更"是**一行的属性**，所以它是一列而不是两张表 ——
        # 同一项目从"跟随"变"覆盖"只是插一行 MANUAL，不需要搬家。
        sa.Column("origin", sa.String(length=20), nullable=False),
        # --- 审计字段（ADR-13：前后值 / 人 / 时间 / 理由 / 生效范围 / requestId）---
        # "变更前值"**不需要一列** —— 它就是同一组合下 revision_no 的上一行。
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("request_id", sa.String(length=80), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "key IN ('batch.limits', 'platform.mediaLimits', "
            "'research.maxEffectiveFiles')",
            name=op.f("ck_runtime_settings_key_valid"),
        ),
        sa.CheckConstraint(
            "scope_kind IN ('GLOBAL', 'PROJECT')",
            name=op.f("ck_runtime_settings_scope_kind_valid"),
        ),
        sa.CheckConstraint(
            "origin IN ('GLOBAL', 'INHERITED', 'MANUAL')",
            name=op.f("ck_runtime_settings_origin_valid"),
        ),
        sa.CheckConstraint(
            "revision_no >= 1",
            name=op.f("ck_runtime_settings_revision_no_positive"),
        ),
        # 作用域与项目的**互相一致性**：
        #   · GLOBAL ⇒ project_id IS NULL（"全局"不该指向某个项目）
        #   · PROJECT ⇒ project_id IS NOT NULL（"项目级"必须说清是哪个项目）
        # 少了这条，一行 `(scope_kind='PROJECT', project_id=NULL)` 是合法数据，
        # 而它被读成"某个没有项目的项目级配置" —— **不报错**。
        sa.CheckConstraint(
            "(scope_kind = 'GLOBAL' AND project_id IS NULL)"
            " OR (scope_kind = 'PROJECT' AND project_id IS NOT NULL)",
            name=op.f("ck_runtime_settings_scope_matches_project"),
        ),
        # `origin` 与作用域的一致性：
        #   · GLOBAL 行只能是 origin='GLOBAL'（"全局真值"没有别的来源）
        #   · PROJECT 行只能是 INHERITED / MANUAL（副本或覆盖）
        # 少了这条，一行 `(scope_kind='GLOBAL', origin='MANUAL')` 会让
        # 级联刷新的判断逻辑（读 origin）得出错误结论。
        sa.CheckConstraint(
            "(scope_kind = 'GLOBAL' AND origin = 'GLOBAL')"
            " OR (scope_kind = 'PROJECT' AND origin IN ('INHERITED','MANUAL'))",
            name=op.f("ck_runtime_settings_origin_matches_scope"),
        ),
        # `value` 必须是 JSON **object**：三种键的形状都是对象，
        # 而标量会让 `value['TRIAL']` 抛 TypeError 而不是可读错误。
        sa.CheckConstraint(
            "jsonb_typeof(value) = 'object'",
            name=op.f("ck_runtime_settings_value_is_object"),
        ),
        # 理由不能是空串。**`NULL` 由服务层拦**（ADR-13 要求审计含"理由"）——
        # 这条 CHECK 只管"写了但是空的"这一层。
        sa.CheckConstraint(
            "reason IS NULL OR char_length(reason) > 0",
            name=op.f("ck_runtime_settings_reason_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_runtime_settings_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_runtime_settings_actor_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_settings")),
    )
    # 四条**普通**索引（查询路径用，不是约束）：
    #   ① 当前生效值：按 (key, scope_kind, project_id) 取 revision_no 最大
    #   ② 全局变更时**枚举所有项目**的副本（按 origin 定位"需要跟随"的行）
    #   ③ `GET /settings/projects/diverged`
    #   ④ 按 key 列历史（管理侧"这个上限被谁改过"）
    op.create_index(
        "ix_runtime_settings_lookup",
        "runtime_settings",
        ["key", "scope_kind", "project_id", "revision_no"],
        unique=False,
    )
    op.create_index(
        "ix_runtime_settings_origin",
        "runtime_settings",
        ["key", "origin"],
        unique=False,
    )
    op.create_index(
        "ix_runtime_settings_project_id",
        "runtime_settings",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_runtime_settings_key_created",
        "runtime_settings",
        ["key", "created_at"],
        unique=False,
    )
    # ------------------------------------------------------------------
    # ⚠️⚠️ **第 1~2 条防线：两条部分唯一索引。`WHERE` 是判据的一部分。**
    #
    # SQL **不写在这里** —— 来自 `modules/planning/ddl.py` 的
    # `partial_unique_index_ddl()`，与测试、证据脚本同源。
    # 本包有三处（models / 迁移 / ddl.py）会写出索引规格，
    # 而"只比对名字"是不够的（名字相同、WHERE 不同是最可能的漂移形态），
    # 因此 `ddl.index_spec_drift()` 逐字段对账 **键列含顺序** + **WHERE 的
    # 规范化文本**。
    #
    # ⚠️ 这两条**不会出现在 `pg_constraint` 里** —— 核对必须查
    # `pg_indexes.indexdef`，只查约束表会完全看不到这两条防线。
    # ------------------------------------------------------------------
    from rednote.modules.planning import ddl as planning_ddl

    for _index_name, _index_ddl in planning_ddl.partial_unique_index_ddl():
        op.execute(_index_ddl)

    # ---- 第 2 张：plan_drafts（**可变**工作区）---------------------------
    # 引用 projects / production_baselines / users，三者均已存在
    # （production_baselines 由本迁移的 down_revision 建）。
    op.create_table(
        "plan_drafts",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # 一项目一份（`UNIQUE`）—— 与 `quota_accounts` 用 project_id 做主键
        # 同一语义。本表用 surrogate PK + 唯一约束，理由是它将来可能被
        # 别的表引用（PLAN 版本指向草稿），surrogate 让这件事不必迁移。
        sa.Column("project_id", sa.UUID(), nullable=False),
        # 本草稿对应的基线。**可空**：项目可能还没固化基线就开始配计划。
        # 可空 + 显式记录，比"偷偷用当前基线"好 —— 后者会在基线切换后
        # 让草稿的语义悄悄改变。
        sa.Column("baseline_id", sa.UUID(), nullable=True),
        sa.Column(
            "state",
            sa.String(length=20),
            server_default=sa.text("'EDITING'"),
            nullable=False,
        ),
        # 本轮总量。**可空**：运营可能先配比例、后定总量。
        # **上界不在这里落 CHECK**（ADR-13 已把上界移到 runtime_settings），
        # 但**下界留着**（`>= 1`，结构性不可能）—— 见下方两条 CHECK。
        sa.Column("total_quantity", sa.Integer(), nullable=True),
        # --- 三个 JSONB 数组（一份草稿内部的结构）------------------------
        # **为什么不是两张子表**：这些数组不跨草稿被引用、不被外键指向、
        # 不需要独立查询（"哪些草稿用了某个场景"不是任何需求）。
        # 建子表会带来"子表与父表行数必须一致"这类**没有需求支撑的**不变量。
        # 代价（JSONB 内部无法落 CHECK）已登记：`weightPermille` 的和
        # 等于 1000 只能由服务层保证 —— 处置与 `runtime_settings.value`
        # 同一手法（服务层是唯一写入口 + 纯函数让非法比例算不出结果）。
        sa.Column(
            "scene_items",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "form_items",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "pairings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "schema_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
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
        # 乐观锁：原型是"拖数字 → 自动保存"，并发编辑是真实场景。
        # 客户端提交带 `If-Match`，版本不符返回 412 而不是静默覆盖。
        # **不用悲观锁**：拖数字会产生大量短暂写入，行锁会让人互相等。
        sa.Column(
            "lock_version",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('EDITING', 'SUBMITTED')",
            name=op.f("ck_plan_drafts_state_valid"),
        ),
        # 下界（结构性不可能，留在 DDL）；上界由 runtime_settings 管，
        # 但下面仍有一条**防御性**上界。
        sa.CheckConstraint(
            "total_quantity IS NULL OR total_quantity >= 1",
            name=op.f("ck_plan_drafts_total_quantity_positive"),
        ),
        # 防御性上界 100000：`runtime_settings` 是权威，但一行被裸 SQL
        # 写进 10 亿条时，所有下游的成本计算都会爆炸（拿它乘单价）。
        # 这条是"绕过应用层也拦得住"的那一层。
        sa.CheckConstraint(
            "total_quantity IS NULL OR total_quantity <= 100000",
            name=op.f("ck_plan_drafts_total_quantity_within_bound"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(scene_items) = 'array'",
            name=op.f("ck_plan_drafts_scene_items_is_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(form_items) = 'array'",
            name=op.f("ck_plan_drafts_form_items_is_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(pairings) = 'array'",
            name=op.f("ck_plan_drafts_pairings_is_array"),
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name=op.f("ck_plan_drafts_schema_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_plan_drafts_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_id"],
            ["production_baselines.id"],
            name=op.f("fk_plan_drafts_baseline_id_production_baselines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_plan_drafts_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_drafts")),
        # 一项目一份（与 `uq_information_drafts_project_id` 同构）。
        sa.UniqueConstraint("project_id", name="uq_plan_drafts_project_id"),
    )
    op.create_index(
        "ix_plan_drafts_updated_at",
        "plan_drafts",
        ["updated_at"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # 触发器：autogenerate **不生成触发器**，必须手工补。
    # DDL 来源与测试完全同源（`modules/planning/ddl.py` 的
    # `all_trigger_ddl`），因此这里**不重写 SQL**，而是直接调用它。
    #
    # 只有 1 个（`runtime_settings` 的不可变保护，复用跨包共用的
    # `rednote_reject_mutation()`）—— **只挂触发器，不重建函数**。
    #
    # ⚠️ `CREATE CONSTRAINT TRIGGER` 在 PG 里**不做依赖检查** ——
    # plpgsql 函数体在首次调用时才解析，因此"触发器先于函数存在"其实是
    # 安全的；但本循环仍按 (函数, 触发器) 顺序执行，读日志时更直观。
    # ------------------------------------------------------------------
    for _table, _function_ddl, _trigger_ddl, _drop_trigger, _drop_function in (
        planning_ddl.all_trigger_ddl()
    ):
        op.execute(_function_ddl)
        op.execute(_trigger_ddl)

    # ------------------------------------------------------------------
    # 数据回填：ADR-13 初始默认值 + OP-60 存量项目副本（模块 docstring 第五节）
    #
    # **不 commit**：会话绑定在 Alembic 的连接上，让它随迁移事务一起提交。
    # ------------------------------------------------------------------
    from sqlalchemy.orm import Session

    from rednote.modules.planning.repository import PlanningRepository
    from rednote.modules.planning.settings_service import bootstrap_defaults

    _bind = op.get_bind()
    _session = Session(bind=_bind)
    bootstrap_defaults(PlanningRepository(_session))
    _session.flush()

    # ### end Alembic commands ###


def downgrade() -> None:
    """反向迁移。

    必须补全，不能留 `NotImplementedError` —— 那会让任何回滚尝试直接失败，
    与 WP47 的恢复演练（RPO/RTO）目标冲突。

    **只删本迁移自己建出来的对象。** 判据不是"它在哪张表上"，
    而是"**这个对象是不是我建的**"：

      · `trg_runtime_settings_immutable` → 我建的，删。
      · `rednote_reject_mutation()` → 别人建的共用函数，**不删**。
        删它会撞依赖错误，并让 WP11/WP15/WP16/WP17/WP18/WP20/WP22/WP23
        的保护**一起失效**（WP16 差点踩、WP18/WP20/WP22/WP23 明确防了）。
        因此第 2 步用 `ddl.consistency_items()`（本包为空集），
        **不得**改用 `all_trigger_ddl()` —— 它的 `drop_function` 指向共用函数。
      · 2 张表 → 我建的，删（**反序**：先删引用别人的）。
      · 数据行 → 随表一并删除（见模块 docstring 第六节的代价登记）。

    ⚠️ **本 `downgrade` 有真实数据损失**：`runtime_settings` 是配置历史，
    ADR-13 要求它"与费用账本、版本快照同级对待"。回退前须导出全量。
    """
    # ### commands auto generated by Alembic - please adjust! ###
    from rednote.modules.planning import ddl as planning_ddl

    # ---- 1. 先删触发器（触发器 DROP 必须先于函数 DROP）----
    # 顺序不能反：函数仍被触发器引用时，PG 会拒绝删除
    # （"cannot drop function ... because other objects depend on it"）。
    for _table, _function_ddl, _trigger_ddl, _drop_trigger, _drop_function in (
        planning_ddl.all_trigger_ddl()
    ):
        op.execute(_drop_trigger)

    # ---- 2. 只删本模块自己的函数（**本包为空集，即什么都不删**）----
    # `consistency_items()` 返回空列表是**有意的显式形状**：
    # 本包没有跨表一致性触发器（见模块 docstring 第三节），
    # 而"删本模块的跨表对象"退化为"什么都不删"恰好是**正确**的行为 ——
    # 共用函数 `rednote_reject_mutation` 不属于本模块。
    for _table, _function_ddl, _trigger_ddl, _drop_trigger, _drop_function in (
        planning_ddl.consistency_items()
    ):
        op.execute(_drop_function)

    # ---- 3. 再删表（**反序**：先删引用别人的，后删被引用的）----
    op.drop_index("ix_plan_drafts_updated_at", table_name="plan_drafts")
    op.drop_table("plan_drafts")

    op.drop_index("ix_runtime_settings_key_created", table_name="runtime_settings")
    op.drop_index("ix_runtime_settings_project_id", table_name="runtime_settings")
    op.drop_index("ix_runtime_settings_origin", table_name="runtime_settings")
    op.drop_index("ix_runtime_settings_lookup", table_name="runtime_settings")
    # ⚠️ 两条部分唯一索引在 drop_table 时也会被一并删掉；显式写出来是为了让
    # "我删了这两条防线"在迁移日志里**可见**（与 WP22 同一做法）。
    # 名字从 `ddl.partial_unique_index_names()` 取，不手写 —— 手写会让
    # "改名后 downgrade 静默失效"（DROP INDEX IF EXISTS 不报错）。
    from rednote.modules.planning import ddl as _planning_ddl

    for _index_name in _planning_ddl.partial_unique_index_names():
        op.drop_index(_index_name, table_name="runtime_settings")
    op.drop_table("runtime_settings")

    # ------------------------------------------------------------------
    # ⚠️ G2 铁律第一件：**删掉 autogenerate 提议的 M0 探针表 drop**。
    #
    # autogenerate 在测试库上跑时，会把 M0 阶段留下的探针表当成"多余对象"
    # 并提议删掉它们。若把那些语句留在本迁移里，**在别的库上它们会删掉
    # 真实存在的表**（本坑已连续 9 次触发，见登记册 G2 与 ISS-02）。
    #
    # 被刻意**没有**写进本文件的内容（列举出来便于评审复核）：
    #
    #     op.drop_index(op.f('wp05_vec_probe_embedding_idx'), table_name='wp05_vec_probe', postgresql_using='hnsw')
    #     op.drop_table('wp05_vec_probe')
    #     op.drop_index(op.f('smoke_meta_created_at_idx'), table_name='smoke_meta')
    #     op.drop_table('smoke_meta')
    #
    # 本迁移只碰 runtime_settings / plan_drafts 两张表 + 1 个触发器
    # + 2 条部分唯一索引。
    # ------------------------------------------------------------------
    # ### end Alembic commands ###
