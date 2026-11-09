"""WP22：信息审核与基线（4 张表 + 9 个触发器）。

revision: 185ec01bed91
down_revision: 7a4c1e93b6d2

**本迁移包含 4 张表**：

| 对象 | 性质 | 说明 |
|---|---|---|
| `information_drafts` | **可变**工作区 | 步骤 3 的审核区（事实 / 冲突 / 场景 / 边界 / 研究选用）。一项目一份 |
| `production_baselines` | **不可变** | 基线。`id` 同时 FK `version_nodes.id`；`ref` 钉死 Brief 版本 |
| `baseline_items` | **不可变** | 基线内条目，含**未纳入项与原因** |
| `step_approvals` | **不可变** | 跨步骤批准（**闭合 OP-151** 的形状未定义） |

---

## 一、G2 铁律的执行记录（本文件是手工写全的）

与 WP18 / WP20 / WP21 / WP23 同一做法：**只取 `revision` / `down_revision`
两个值，其余手工写全**。理由见 `docs/m2/WP22-ddl-草案.py` §一 ——
autogenerate 在本项目反复产出"删探针表 / 丢部分索引 WHERE / 不含触发器"
的产物，逐条核对的手工成本高于直接写全。

三件事已逐条处理（G2 铁律）：

1. **删掉 M0 探针表的 `op.drop_table`** —— 见 `downgrade()` 末尾的注释块；
2. **`downgrade` 不重建任何本包不拥有的对象** —— 尤其**不删
   `rednote_reject_mutation()`**（它属于 `platform`，被全项目共用）；
3. **`from sqlalchemy.dialects import postgresql` 已补** —— 本迁移用了
   `postgresql.JSONB`。autogenerate 用了它却**不会自动加这行 import**，
   结果是迁移文件语法正确、diff 看着正常，**执行时 `NameError`**。
   本坑在本项目已连续触发 9 次（登记册 G2 / ISS-02）。

## 二、两条**部分唯一索引** —— 本迁移最容易被 autogenerate 悄悄毁掉的东西

| 索引 | `WHERE` | 漏写 `WHERE` 的后果 |
|---|---|---|
| `uq_production_baselines_project_root` | `supersedes_id IS NULL` | 退化成"每项目只能有一条基线" → 与 AC-08（修改创建新版本）**直接冲突** |
| `uq_step_approvals_root` | `supersedes_id IS NULL` | 退化成"同一版只能有一条批准" → "先拒后批"变成不可能 |

两处的症状都是**静默且迟到**的（第二次创建/批准时才报错），中间所有测试全绿。
**本机实测已确认**：`pg_indexes.indexdef` 里两条 `WHERE` 都在位。

⚠️ **部分唯一索引在 `pg_constraint` 里根本不出现** —— 只查约束表的核对
**看不到**这两条防线。核对必须查 `pg_indexes`。

## 三、`DEFERRABLE INITIALLY DEFERRED` 的 6 条外键

| 表 | 外键 | 为什么必须延迟 |
|---|---|---|
| `information_drafts` | `brief_version_id` | 草稿要先指向 Brief 版本，而该版本可能在同一事务的后半段才注册 |
| `production_baselines` | `id` | 注册表行与子表行由**同一事务、两个不同代码路径**写入（`register_version` 内部有 SAVEPOINT + 取号重试，**不暴露节点插入时机**） |
| `production_baselines` | `brief_version_id` | 同第 1 条（基线"绑定 Brief v2"） |
| `production_baselines` | `supersedes_id` | 替代链在同一批量写里插入：新行可能先于旧行被 `INSERT` |
| `step_approvals` | `subject_version_id` | 被批准的版本节点由 `register_version` 产出，时机不可猜 |
| `step_approvals` | `supersedes_id` | 同上（"先拒后批"两条批准同事务写） |

`baseline_items` 的 3 条 FK **全部立即检查** —— 它的父行
（`production_baselines`）与两条知识引用行都由**同事务的前序步骤**写入，
不存在需要推迟的顺序问题。（与 WP20 的表不同：那里节点行与 asset 行交错。）

**必须显式说明**：`DEFERRABLE` 管的是**事务提交期**，它**管不了 DDL 期的
表存在性** —— 被引用的表在 `CREATE TABLE` 那一刻必须已经存在。
表的存在性靠建表顺序排，行的先后靠 `DEFERRABLE` 绕开，两者是两件事。

## 四、建表顺序（依赖序，表级无环）

    information_drafts          （引用 version_nodes / projects，均已存在）
        ↓
    production_baselines        （引用 version_nodes / projects / users）
        ↓
    baseline_items              （引用 production_baselines / knowledge_assets / knowledge_versions）
        ↓
    step_approvals              （引用 version_nodes / projects / users）

`production_baselines.supersedes_id` 与 `step_approvals.supersedes_id` 是
**自引用**，PG 允许在 `CREATE TABLE` 内声明，不构成表级环。
因此**不需要 `ALTER TABLE ... ADD CONSTRAINT` 分两步**（与 WP18 不同：
WP18 的 `briefs` ↔ `brief_versions` 是真环，那里必须拆）。

## 五、9 个触发器（DDL 不写在本文件里）

触发器 DDL 来自 `modules/baseline/ddl.py` 的 `all_trigger_ddl()` ——
与测试完全同源。两份不一致的 DDL 是本项目反复踩过的坑
（WP11 / WP16 / WP17 / WP18 各一次）。

| # | 触发器 | 挂在哪张表 | 类型 |
|---|---|---|---|
| 1~3 | `trg_{production_baselines,baseline_items,step_approvals}_immutable` | 各自表 | BEFORE UPDATE/DELETE（复用共用函数） |
| 4 | `trg_production_baselines_node_consistency` | `production_baselines` | 延迟约束（OP-88 判据 1~3） |
| 5 | `trg_version_nodes_baseline_paired` | **`version_nodes`（共用表）** | 延迟约束（OP-88 反向，带 `WHEN entity_type='BASELINE'` 收窄） |
| 6 | `trg_baseline_items_cross_brand` | `baseline_items` | 延迟约束（AC-24 跨品牌） |
| 7 | `trg_baseline_items_asset_scope` | `baseline_items` | 延迟约束（版本必须属于声明的资产） |
| 8 | `trg_production_baselines_approval_required` | `production_baselines` | 延迟约束（OP-151 同事务批准） |
| 9 | `trg_production_baselines_no_blocking` | `production_baselines` | 延迟约束（AC-09 阻断项） |

**`trg_version_nodes_baseline_paired` 挂在共用表上，但它是本包建的 →
本包负责删。绝不能 `DROP TABLE version_nodes`。**

**`CREATE TRIGGER` 不是幂等的**（重复执行报 `DuplicateObject`，本机实测复现）。
本迁移的 `upgrade` 走 `CREATE OR REPLACE FUNCTION` + `CREATE TRIGGER`，
在**空库 / 该迁移首次执行**的前提下次次成功；若要在已有触发器的库上重跑，
必须先 `ddl.all_trigger_ddl()` 的 `_drop_trigger` 清一遍。

## 六、前置条件

无外部扩展依赖。**本迁移刻意不动 `projects.active_baseline_id`** ——
那一列是 WP11 已建的（`projects/models.py` L227），本包只写它的**值**
（`repository.set_active_baseline`），不建它。

## 七、落地实测记录（2026-09-26，一次性库 `rednote_mig_wp22`）

不是"应该能跑"，是**跑过了**。四步逐条留痕：

1. **`upgrade head` 一次通过** —— 全链 15 步无中断，末步
   `Running upgrade 7a4c1e93b6d2 -> 185ec01bed91, WP22…`。
2. **结构实测对账**（查的是 `pg_indexes` / `pg_constraint` / `pg_trigger`，不是模型定义）：
   - 列：`information_drafts` 13 / `production_baselines` 10 /
     `baseline_items` 13 / `step_approvals` 11 = **47**（0 缺失、0 多余）；
   - 约束：**CHECK 31 / FK 14 / PK 4 / UQ 3**（另有 `t` 型 5 条 = 约束触发器，
     见第 4 步）；
   - 索引行 **20**（含 13 条显式 `CREATE INDEX`）；
   - **6 条 `DEFERRABLE` FK 逐条点名核对**（`condeferrable=True, condeferred=True`）：
     `drafts.brief_version_id`、`baselines.{id,brief_version_id,supersedes_id}`、
     `approvals.{subject_version_id,supersedes_id}`；
     `baseline_items` 的 3 条 FK 实测 `False/False`（**立即检查，与设计一致**）。
3. **两条部分索引的 `WHERE` 用 `indexdef` 逐字核对在位**（**不看 `pg_constraint`** ——
   部分唯一索引在那里根本不出现）：
   ```
   CREATE UNIQUE INDEX uq_production_baselines_project_root ON public.production_baselines
     USING btree (project_id) WHERE (supersedes_id IS NULL)
   CREATE UNIQUE INDEX uq_step_approvals_root ON public.step_approvals
     USING btree (project_id, step_no, subject_kind, subject_version_id)
     WHERE (supersedes_id IS NULL)
   ```
4. **9 个触发器按 `ddl.trigger_names()` 精确点名核对**（不是"数个数"—— 数个数会把
   WP18/W20/W23 挂在同一张 `version_nodes` 上的 3 个 `*_paired` 混进来）：
   缺失集合为**空集**；挂载表逐条正确，其中
   `trg_version_nodes_baseline_paired -> version_nodes`（共用表，本包建的）。
   6 个 `rednote_check_baseline_*` 函数全部在位且 `lanname='plpgsql'`。

5. **`downgrade 7a4c1e93b6d2` → 再 `upgrade head` 往返**，清点边界：
   | 对象 | 回退后 | 期望 |
   |---|---|---|
   | 本包 4 张表 | 0 | 0 |
   | 本包 6 个一致性函数 | 0 | 0 |
   | **共用函数 `rednote_reject_mutation`** | **1（存活）** | 1 |
   | `version_nodes` 上剩余触发器 | 4（brief/knowledge/prompt/immutable） | 本包那个已摘除 |
   | 别人家的表（`briefs`/`knowledge_assets`/`prompt_templates`/`version_nodes`） | 4 | 4 |
   **这一行是本迁移最该验的一条** —— `downgrade` 误删共用函数的后果是
   **全项目三张不可变表同时失去防线，且没有任何测试会立刻变红**。
   往返后 9 个触发器与 6 个函数**全部重新到位**。

### 7.1 `alembic check` 的唯一残差与本包无关（对照实验结论）

`alembic check` 报 `Detected removed/added foreign key (active_version_id)(id)
on table prompt_templates` —— 这是 **WP23 的既有残差**，成因是
`prompt_templates ↔ prompt_versions` 的**循环外键**让 autogenerate
`Cannot correctly sort tables`（env.py:170 的 `SAWarning` 原文），
于是把同一条 FK 既报"删"又报"加"。

**对照实验（可复现，两步）**：建一个只到 WP21 的库 → `alembic stamp 185ec01bed91`
（表结构不含 WP22，版本号骗到 head）→ 跑 `check`：

- 报出了 WP22 的 17 条 "Detected added table/index"（**证明本包对象确实
  在 `Base.metadata` 里被 autogenerate 看见**，即 `migrations/env.py` 的
  import 生效）；
- **同时那条 `prompt_templates` FK 依旧报删/加** —— 与 WP22 在场与否无关。

因此残差归 WP23 一侧，**不在本包修复范围内**（改它要动别人的迁移与模型）。
本包收口时**须**把它作为一条新问题登记进 `docs/待定项与问题登记册.md`
（`prompt_templates ↔ prompt_versions` 循环 FK 导致 `alembic check` 恒失败，
使 `check` 作为"模型与迁移一致"的机械判据**整体不可用** —— 这与判据
纪律第 9 条同族：一个永远失败或永远被忽略的判据，等于没有判据）。
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
revision: str = "185ec01bed91"
down_revision: str | Sequence[str] | None = "7a4c1e93b6d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """正向迁移。建表顺序见模块 docstring 第四节（依赖序，表级无环）。"""
    # ### commands auto generated by Alembic - please adjust! ###
    # （本文件手工写全，理由见模块 docstring 第一节）

    # ---- 第 1 张：information_drafts（**可变**工作区）-------------------
    # 只引用 version_nodes / projects，因此排在最前。
    #
    # ⚠️ 本表与 WP18 的 brief_drafts 名字极像、作用完全不同：
    #    brief_drafts = Brief **解构前**的工作区，产出版本；
    #    information_drafts = 步骤 3 的**审核**工作区，基线只读到那一版为止。
    # 独立成表是刻意的：共用会让"审核时改了字段 → 解构结果被覆盖"双向踩踏。
    #
    # **本表没有 `state` 列**：状态由 claims/conflicts/scenes 三个数组算出（决策 D7）。
    # **本表没有不可变触发器**：工作区反复改是正常用法；靠 lock_version 做 ETag。
    op.create_table(
        "information_drafts",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        # 钉住"本工作区正在审核哪一版 Brief"（与 brief_drafts.base_version_id 同义）。
        sa.Column("brief_version_id", sa.UUID(), nullable=True),
        # --- 五块审核内容：运营正在编辑的内容，不是已固化的历史 -----------
        # 形状由 readiness.py 与 api.py 的请求模型约束（本迁移只落形状 CHECK）。
        sa.Column(
            "claims",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        # 冲突与其处置：每行 {conflictId, kind, blocking, state, action?, reason?}
        # ⚠️ 第 5 层防线（trg_production_baselines_no_blocking）读的就是这一列。
        sa.Column(
            "conflicts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "scenes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        # 表达边界：{mustMention[], mustNotMention[], bannedPhrases[], risks[]}
        sa.Column(
            "boundaries",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        # 研究资料选用：每行 {fileId, title, included, exclusionReason?}
        sa.Column(
            "research_selection",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        # PRD §7.3 条件 ③「运营明确点击确认」的载体。
        # **显式布尔，而不是从"草稿有内容"推断** —— 条件 ③ 的语义是"人工点过确认"，
        # 系统代判会让它失效（readiness.py 的说明）。
        sa.Column(
            "operator_confirmed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "lock_version",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        # --- 形状 CHECK：数组列必须是数组，对象列必须是对象 ----------------
        # **不判非空**（与 WP20 的 structured_payload_not_empty 刻意不同）：
        # 一份刚创建的草稿本来就什么都没审核（`[]` 是合法初值）。
        # WP20 判非空是因为那里 structured_payload 承载"事实字段"，空对象 = 没写事实。
        # 两处判据不同是因为**语义不同**，不是不一致。
        sa.CheckConstraint(
            "jsonb_typeof(claims) = 'array'",
            name=op.f("ck_information_drafts_claims_is_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(conflicts) = 'array'",
            name=op.f("ck_information_drafts_conflicts_is_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(scenes) = 'array'",
            name=op.f("ck_information_drafts_scenes_is_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(research_selection) = 'array'",
            name=op.f("ck_information_drafts_research_selection_is_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(boundaries) = 'object'",
            name=op.f("ck_information_drafts_boundaries_is_object"),
        ),
        # 上界：防止某次导入把整本书塞进 claims（MAX_DRAFT_ITEMS = 2000）。
        sa.CheckConstraint(
            "jsonb_array_length(claims) <= 2000",
            name=op.f("ck_information_drafts_claims_within_bound"),
        ),
        sa.CheckConstraint(
            "jsonb_array_length(conflicts) <= 2000",
            name=op.f("ck_information_drafts_conflicts_within_bound"),
        ),
        sa.CheckConstraint(
            "jsonb_array_length(scenes) <= 2000",
            name=op.f("ck_information_drafts_scenes_within_bound"),
        ),
        sa.CheckConstraint(
            "jsonb_array_length(research_selection) <= 2000",
            name=op.f("ck_information_drafts_research_within_bound"),
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name=op.f("ck_information_drafts_schema_version_positive"),
        ),
        # 延迟：草稿与 Brief 版本的写入顺序不由本包控制（模块 docstring 第三节）。
        sa.ForeignKeyConstraint(
            ["brief_version_id"],
            ["version_nodes.id"],
            name=op.f("fk_information_drafts_brief_version_id_version_nodes"),
            ondelete="RESTRICT",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_information_drafts_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_information_drafts")),
        # 一项目一份（R6）—— 与 briefs.project_id 同构。
        sa.UniqueConstraint("project_id", name="uq_information_drafts_project_id"),
    )
    op.create_index(
        "ix_information_drafts_project_id",
        "information_drafts",
        ["project_id"],
        unique=False,
    )
    # 排序用（原型 L42 的"最近更新"）。
    op.create_index(
        "ix_information_drafts_updated_at",
        "information_drafts",
        ["updated_at"],
        unique=False,
    )

    # ---- 第 2 张：production_baselines（**不可变**）----------------------
    # 引用 version_nodes / projects / users，三者都已存在。
    #
    # ⚠️ **本表有意没有 `state` 列** —— 这是与设计文档 §2.1 第 1 层的冲突取舍：
    # "ACTIVE → SUPERSEDED"这个动作本身就是"原地改写旧行"，而 AC-08 说
    # 修改必须创建新版本。本实现取 AC-08 那一侧：
    #   · "当前生效的是哪条" → 读 projects.active_baseline_id（**投影列**）
    #     —— 一个列物理上装不下两个值，比部分唯一索引**更硬**；
    #   · 部分唯一索引并未取消，它守的是另一条真实不变量：**每个项目至多一条链根**。
    # 完整论证见 docs/m2/WP22-ddl-草案.py §5.1（收口时同步登记册）。
    op.create_table(
        "production_baselines",
        # ⚠️ 无 server_default：id 由调用方从 register_version 的返回值得来，
        # 服务端生成会让"注册表行与子表行用同一个 id"这件事失去保证。
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        # 钉住"哪一版 Brief"（原型 L60「BL-v3 绑定 Brief v2」）。
        sa.Column("brief_version_id", sa.UUID(), nullable=True),
        # 审核规则集的版本标签（原型 L61「审核规则 v5（9 阻断 · 14 提醒）」）。
        # **只被快照，不被执行** —— 规则的判定归 WP30。
        sa.Column("ruleset_version", sa.String(length=200), nullable=False),
        # **基线级**快照（与 baseline_items 的**条目级**快照严格区分）：
        # 原型 L56~64 的 9 项里属于"整条基线"的那几项
        # （批准人 / 批准时间 / 规则集读数 / 已确认限制 / 事实清单摘要）。
        sa.Column(
            "snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        # 被本条替代的那条基线。**单向**：旧行上不存 superseded_by_id ——
        # 那一列意味着"改写旧行"，而旧行不可变（决策 D5）。
        # "谁替代了 BL-v2"由反向查询 `WHERE supersedes_id = <id>` 得到。
        sa.Column("supersedes_id", sa.UUID(), nullable=True),
        sa.Column("change_reason", sa.String(length=1000), nullable=False),
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
            "char_length(ruleset_version) > 0",
            name=op.f("ck_production_baselines_ruleset_version_not_empty"),
        ),
        sa.CheckConstraint(
            "char_length(change_reason) > 0",
            name=op.f("ck_production_baselines_change_reason_not_empty"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name=op.f("ck_production_baselines_snapshot_is_object"),
        ),
        # 自引用必须排除自身 —— 否则"我替代了我自己"这条无意义的关系合法。
        sa.CheckConstraint(
            "supersedes_id IS NULL OR supersedes_id <> id",
            name=op.f("ck_production_baselines_supersedes_not_self"),
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name=op.f("ck_production_baselines_schema_version_positive"),
        ),
        # ⚠️ id 这条是本表最关键的 FK：它就是 OP-88 的"版本表 id 同时 FK 注册表"。
        # 延迟的理由见模块 docstring 第三节。
        sa.ForeignKeyConstraint(
            ["id"],
            ["version_nodes.id"],
            name=op.f("fk_production_baselines_id_version_nodes"),
            ondelete="RESTRICT",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["brief_version_id"],
            ["version_nodes.id"],
            name=op.f("fk_production_baselines_brief_version_id_version_nodes"),
            ondelete="RESTRICT",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["production_baselines.id"],
            name=op.f("fk_production_baselines_supersedes_id_production_baselines"),
            ondelete="RESTRICT",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_production_baselines_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_production_baselines_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_production_baselines")),
    )
    # ------------------------------------------------------------------
    # ⚠️⚠️ **第 1 层防线：部分唯一索引。`WHERE` 是判据的一部分。**
    #
    # 漏掉 WHERE → 退化成"每项目只能有一条基线" → 与 AC-08
    # （修改必须创建新版本）**直接冲突**，且症状**迟到**
    # （第二次创建基线时才报错），中间所有测试全绿。本机实测确认。
    #
    # 它守的不变量：**每个项目至多一条链根**（替代关系是链，
    # 两条互不相干的链会让"当前生效"有第二个候选）。
    #
    # ⚠️ 这条索引**不会出现在 `pg_constraint` 里** —— 核对时必须查
    # `pg_indexes.indexdef`，只查约束表会完全看不到这条防线。
    # ------------------------------------------------------------------
    op.create_index(
        "uq_production_baselines_project_root",
        "production_baselines",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
    )
    op.create_index(
        "ix_production_baselines_project_id",
        "production_baselines",
        ["project_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_production_baselines_supersedes_id",
        "production_baselines",
        ["supersedes_id"],
        unique=False,
    )
    op.create_index(
        "ix_production_baselines_brief_version_id",
        "production_baselines",
        ["brief_version_id"],
        unique=False,
    )

    # ---- 第 3 张：baseline_items（**不可变**）---------------------------
    # 引用 production_baselines（上一张）与两张 WP20 的表。
    #
    # 三处刻意的列设计（AC-24 原文 + 决策 D1/D3）：
    #   ① ref + snapshot 双存；② inclusion + exclusion_reason；③ snapshot_text 与 snapshot 并存。
    op.create_table(
        "baseline_items",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("baseline_id", sa.UUID(), nullable=False),
        sa.Column("item_no", sa.Integer(), nullable=False),
        sa.Column("item_kind", sa.String(length=20), nullable=False),
        sa.Column("inclusion", sa.String(length=20), nullable=False),
        # 人读的引用标签（原型「事实 v7」「R-014」这种短标识）。
        sa.Column("ref_label", sa.String(length=100), nullable=False),
        # --- ref：钉死"哪条知识的哪一版"（决策 D1）------------------------
        sa.Column("knowledge_asset_id", sa.UUID(), nullable=True),
        # **钉死版本**，而不是让读方去追 knowledge_assets.active_version_id
        # —— 那正是 R1（基线被知识更新悄悄污染）。设计文档 §2.2 的次场景
        # （9-15 批准 62%，9-20 知识更正为 60%）验证的就是这一列。
        sa.Column("knowledge_version_id", sa.UUID(), nullable=True),
        # --- snapshot：当时批准的值（决策 D3）------------------------------
        # 结构化负载：值 + 单位 + 限定条件 + 来源片段 id，与 WP20 的
        # knowledge_versions.structured_payload 同构 —— 不重新解释，只复制一份，
        # 因此它**不引入新的真值来源**。
        sa.Column(
            "snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        # 当时人读的原文（可由 WP26 直接进上下文，也是半年后查事故的第一手材料）。
        sa.Column("snapshot_text", sa.Text(), nullable=True),
        # --- 未纳入原因（AC-24 原文「…与未纳入原因」）----------------------
        # 排除项**也落行** —— 若"没纳入"只是"没有那一行"，
        # "为什么没纳入"就永远查不到。
        sa.Column("exclusion_reason", sa.String(length=1000), nullable=True),
        # 动态上下文顺序（由 context_order.py 派生后写入）。
        # **列上有值，不是每次读时再算**：它是"批准那一刻的顺序"，属于快照的一部分。
        # 重算会随 CONTEXT_PRIORITY_ORDER 的演进变化，而基线必须固定（AC-08）。
        sa.Column("context_order", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "item_no >= 1", name=op.f("ck_baseline_items_item_no_positive")
        ),
        sa.CheckConstraint(
            "context_order >= 1",
            name=op.f("ck_baseline_items_context_order_positive"),
        ),
        sa.CheckConstraint(
            "item_kind IN ('FACT', 'RESEARCH', 'RULE', 'SCENE', 'STYLE')",
            name=op.f("ck_baseline_items_item_kind_valid"),
        ),
        sa.CheckConstraint(
            "inclusion IN ('EXCLUDED', 'INCLUDED')",
            name=op.f("ck_baseline_items_inclusion_valid"),
        ),
        sa.CheckConstraint(
            "char_length(ref_label) > 0",
            name=op.f("ck_baseline_items_ref_label_not_empty"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name=op.f("ck_baseline_items_snapshot_is_object"),
        ),
        # 空快照 = 只有引用没有值 —— 那正是"只存 ref"的形状，也就是 R1。必须拦。
        sa.CheckConstraint(
            "snapshot <> '{}'::jsonb",
            name=op.f("ck_baseline_items_snapshot_not_empty"),
        ),
        # 排除必须说为什么（纳入不必 —— 纳入的理由在 ref 与 snapshot 里）。
        sa.CheckConstraint(
            "inclusion <> 'EXCLUDED' "
            "OR (exclusion_reason IS NOT NULL AND char_length(exclusion_reason) > 0)",
            name=op.f("ck_baseline_items_excluded_needs_reason"),
        ),
        # 纳入的项目不该带排除原因（带了说明状态与原因不一致）。
        sa.CheckConstraint(
            "inclusion <> 'INCLUDED' OR exclusion_reason IS NULL",
            name=op.f("ck_baseline_items_included_has_no_reason"),
        ),
        # 有版本引用就必须有资产引用 —— 否则"版本指向谁"无从判断。
        sa.CheckConstraint(
            "knowledge_version_id IS NULL OR knowledge_asset_id IS NOT NULL",
            name=op.f("ck_baseline_items_version_ref_needs_asset_ref"),
        ),
        # item_no 是 int 列；上界显式写出，避免溢出时报出"整数超出范围"
        # （那种错误读起来像程序 bug，其实是数据到了极限）。
        sa.CheckConstraint(
            "item_no <= 2147483647",
            name=op.f("ck_baseline_items_item_no_in_range"),
        ),
        # ⚠️ 三张 FK **全部立即检查**（不是延迟）—— 见模块 docstring 第三节。
        sa.ForeignKeyConstraint(
            ["baseline_id"],
            ["production_baselines.id"],
            name=op.f("fk_baseline_items_baseline_id_production_baselines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_asset_id"],
            ["knowledge_assets.id"],
            name=op.f("fk_baseline_items_knowledge_asset_id_knowledge_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_version_id"],
            ["knowledge_versions.id"],
            name=op.f("fk_baseline_items_knowledge_version_id_knowledge_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_baseline_items")),
        sa.UniqueConstraint(
            "baseline_id", "item_no", name="uq_baseline_items_baseline_item_no"
        ),
        # **同一条知识不能既纳入又排除**：两条行同时存在时，"这条被采纳了吗"
        # 有两个矛盾答案，而下游（WP26）只会取先读到的那条。
        # 排除项的 knowledge_asset_id 可为 NULL（研究资料不一定是知识资产），
        # 而 NULL 不参与唯一性 —— 这是想要的（多条无资产引用的研究资料可并存）。
        sa.UniqueConstraint(
            "baseline_id", "knowledge_asset_id", name="uq_baseline_items_baseline_asset"
        ),
    )
    op.create_index(
        "ix_baseline_items_baseline_id",
        "baseline_items",
        ["baseline_id", "item_no"],
        unique=False,
    )
    op.create_index(
        "ix_baseline_items_knowledge_asset_id",
        "baseline_items",
        ["knowledge_asset_id"],
        unique=False,
    )
    op.create_index(
        "ix_baseline_items_knowledge_version_id",
        "baseline_items",
        ["knowledge_version_id"],
        unique=False,
    )

    # ---- 第 4 张：step_approvals（**不可变**，闭合 OP-151）---------------
    # OP-151 原文（登记册 L486）说协议**未定义**这个形状。本包必须现在定 ——
    # 因为第 4 层防线要校验"基线在 COMMIT 时有对应批准"，**没有形状就写不出校验**。
    #
    # 批准的**对象是「步骤 + 版本」的组合**，不是"步骤"：Brief 出了 v2 之后，
    # 步骤 1 的"已批准"指的是 **v2 已批准**，而不是"步骤 1 曾经被批过"。
    # 只记步骤会让"这一步批的是哪一版"在数据里**没有答案**，
    # 而八步闭环的验收（证据 = 「固定版本」）恰恰要回答它。
    op.create_table(
        "step_approvals",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("subject_kind", sa.String(length=30), nullable=False),
        # 被批准的**具体那一版**。指向共用注册表 version_nodes ——
        # 因此同一张表服务所有步骤：步骤 1 存 Brief 版本节点、
        # 步骤 3 存基线版本节点，不需要为每步加一列。
        sa.Column("subject_version_id", sa.UUID(), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("decided_by", sa.UUID(), nullable=True),
        # **显式的列，不靠 created_at**：两者在本表里通常相同，但
        # "决策时刻"与"落库时刻"是两件事（补录历史批准时它们不同），
        # 而 S2 的 D4 要求"谁、何时、为什么"三个答案都显式。
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        # 被本条替代的上一条批准（撤销靠新增，不靠改写 —— 与决策 D5 同款）。
        sa.Column("supersedes_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "step_no >= 1 AND step_no <= 8",
            name=op.f("ck_step_approvals_step_no_in_range"),
        ),
        sa.CheckConstraint(
            "subject_kind IN ('BASELINE', 'BRIEF', 'RESEARCH')",
            name=op.f("ck_step_approvals_subject_kind_valid"),
        ),
        sa.CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')",
            name=op.f("ck_step_approvals_decision_valid"),
        ),
        sa.CheckConstraint(
            "char_length(reason) > 0",
            name=op.f("ck_step_approvals_reason_not_empty"),
        ),
        sa.CheckConstraint(
            "supersedes_id IS NULL OR supersedes_id <> id",
            name=op.f("ck_step_approvals_supersedes_not_self"),
        ),
        sa.ForeignKeyConstraint(
            ["subject_version_id"],
            ["version_nodes.id"],
            name=op.f("fk_step_approvals_subject_version_id_version_nodes"),
            ondelete="RESTRICT",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["step_approvals.id"],
            name=op.f("fk_step_approvals_supersedes_id_step_approvals"),
            ondelete="RESTRICT",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_step_approvals_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"],
            ["users.id"],
            name=op.f("fk_step_approvals_decided_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_step_approvals")),
    )
    # ------------------------------------------------------------------
    # ⚠️ **第二条部分唯一索引。`WHERE` 同样是判据的一部分。**
    #
    # 它守的不变量：每个 (项目, 步骤, 对象类型, 版本) 至多一条**链根**批准。
    # 漏掉 WHERE → 退化成"同一版只能有一条批准" → 让"先拒后批"变成不可能。
    # 它同样是**静默**的（症状迟到：第二次批准同一版时）。
    #
    # 与基线的链根唯一性同一手法：撤销靠**新增**一条带 supersedes_id 的。
    # 同样**不会出现在 `pg_constraint` 里** —— 核对必须查 `pg_indexes`。
    # ------------------------------------------------------------------
    op.create_index(
        "uq_step_approvals_root",
        "step_approvals",
        ["project_id", "step_no", "subject_kind", "subject_version_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
    )
    op.create_index(
        "ix_step_approvals_project_step",
        "step_approvals",
        ["project_id", "step_no", "decided_at"],
        unique=False,
    )
    op.create_index(
        "ix_step_approvals_subject_version_id",
        "step_approvals",
        ["subject_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_step_approvals_supersedes_id",
        "step_approvals",
        ["supersedes_id"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # 触发器：autogenerate **不生成触发器**，必须手工补。
    # DDL 来源与测试完全同源（modules/baseline/ddl.py 的 all_trigger_ddl），
    # 因此这里**不重写 SQL**，而是直接调用它。
    #
    # 9 个触发器，分两类：
    #   1. 不可变保护 3 个（复用跨包共用的 rednote_reject_mutation()）
    #      —— 只挂触发器，**不重建函数**。
    #   2. 跨表一致性 6 个（本模块自己的函数）：
    #       · trg_production_baselines_node_consistency —— OP-88 判据 1~3（正向）
    #       · trg_version_nodes_baseline_paired       —— OP-88 反向（**挂共用表 version_nodes**）
    #       · trg_baseline_items_cross_brand          —— AC-24 跨品牌
    #       · trg_baseline_items_asset_scope          —— 版本必须属于声明的资产
    #       · trg_production_baselines_approval_required —— OP-151 同事务批准
    #       · trg_production_baselines_no_blocking    —— AC-09 阻断项
    #
    # ⚠️ `CREATE CONSTRAINT TRIGGER` 在 PG 里**不做依赖检查** ——
    # plpgsql 函数体在首次调用时才解析，因此"触发器先于函数存在"其实是安全的；
    # 但本循环仍按 (函数, 触发器) 顺序执行，读日志时更直观。
    # ------------------------------------------------------------------
    from rednote.modules.baseline import ddl as baseline_ddl

    for _table, _function_ddl, _trigger_ddl, _drop_trigger, _drop_function in (
        baseline_ddl.all_trigger_ddl()
    ):
        op.execute(_function_ddl)
        op.execute(_trigger_ddl)

    # ### end Alembic commands ###


def downgrade() -> None:
    """反向迁移。

    必须补全，不能留 `NotImplementedError` —— 那会让任何回滚尝试直接失败，
    与 WP47 的恢复演练（RPO/RTO）目标冲突。

    **只删本迁移自己建出来的对象。** 判据不是"它在哪张表上"，
    而是"**这个对象是不是我建的**"：

      · 9 个触发器 → **全是我建的，全删**。
        包括挂在 WP11 的 `version_nodes` 上的 `trg_version_nodes_baseline_paired`
        —— 它与"删共用函数"是两件不同的事。**绝不能 `DROP TABLE version_nodes`**。
      · 6 个 `rednote_check_baseline_*` 函数 → 我建的，删。
      · `rednote_reject_mutation()` → 别人建的共用函数，**不删**。
        删它会撞依赖错误，并让 WP11/WP15/WP16/WP17/WP18/WP20/WP23 的保护
        **一起失效**（WP16 差点踩、WP18/WP20/WP23 明确防了）。
      · 4 张表 → 我建的，删（**反序**：先删引用别人的）。

    `ddl.consistency_items()` 存在的意义就是让第 2 步自动只覆盖本模块自己的
    6 个函数 —— 手写一遍会漏、会多删。**不得**改用 `all_trigger_ddl()`
    做第 2 步（它混着不可变保护那 3 项，其 `drop_function` 指向共用函数）。
    """
    # ### commands auto generated by Alembic - please adjust! ###
    from rednote.modules.baseline import ddl as baseline_ddl

    # ---- 1. 先删触发器（触发器 DROP 必须先于函数 DROP）----
    # 顺序不能反：函数仍被触发器引用时，PG 会拒绝删除
    # （"cannot drop function ... because other objects depend on it"）。
    for _table, _function_ddl, _trigger_ddl, _drop_trigger, _drop_function in (
        baseline_ddl.all_trigger_ddl()
    ):
        op.execute(_drop_trigger)

    # ---- 2. 只删本模块自己的函数（**绝不碰 rednote_reject_mutation**）----
    for _table, _function_ddl, _trigger_ddl, _drop_trigger, _drop_function in (
        baseline_ddl.consistency_items()
    ):
        op.execute(_drop_function)

    # ---- 3. 再删表（**反序**：先删引用别人的，后删被引用的）----
    op.drop_index("ix_step_approvals_supersedes_id", table_name="step_approvals")
    op.drop_index("ix_step_approvals_subject_version_id", table_name="step_approvals")
    op.drop_index("ix_step_approvals_project_step", table_name="step_approvals")
    # ⚠️ 部分唯一索引在 drop_table 时也会被一并删掉；显式写出来是为了让
    # "我删了这条防线"在迁移日志里**可见**（与 WP20 同一做法）。
    op.drop_index("uq_step_approvals_root", table_name="step_approvals")
    op.drop_table("step_approvals")

    op.drop_index("ix_baseline_items_knowledge_version_id", table_name="baseline_items")
    op.drop_index("ix_baseline_items_knowledge_asset_id", table_name="baseline_items")
    op.drop_index("ix_baseline_items_baseline_id", table_name="baseline_items")
    op.drop_table("baseline_items")

    op.drop_index(
        "ix_production_baselines_brief_version_id", table_name="production_baselines"
    )
    op.drop_index(
        "ix_production_baselines_supersedes_id", table_name="production_baselines"
    )
    op.drop_index(
        "ix_production_baselines_project_id", table_name="production_baselines"
    )
    op.drop_index(
        "uq_production_baselines_project_root", table_name="production_baselines"
    )
    op.drop_table("production_baselines")

    op.drop_index("ix_information_drafts_updated_at", table_name="information_drafts")
    op.drop_index("ix_information_drafts_project_id", table_name="information_drafts")
    op.drop_table("information_drafts")

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
    # 本迁移只碰 information_drafts / production_baselines / baseline_items /
    # step_approvals 四张表 + 9 个触发器 + 6 个函数。
    # ------------------------------------------------------------------
    # ### end Alembic commands ###
