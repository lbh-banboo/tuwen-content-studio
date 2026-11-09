"""WP22 DDL 草案：信息审核与基线的四张表 + 九个触发器。

**这不是迁移文件。** 迁移由主 agent 串行收口（接缝冻结 §4.2）。
本文件是**规格**：表名、列、类型、可空、默认值、全部约束与索引
（含**两条部分索引的 `WHERE`**）、外键、触发器需求，逐条可核对。

**为什么用 `.py` 而不是 `.md`**：本草案的每一列/每个约束都对应
`models.py` 里的一行。用 Python 写，读者可以直接把两边并排比对；
写成散文则必须来回翻。**本文件不 import 本包任何模块** ——
它要能在"包还没装好"的环境里被打开阅读，而 import 会让它连带失败。
（与 `WP18-ddl-草案.py` / `WP20-ddl-草案.py` 同款。）

**本文件的所有结构性事实都来自 PostgreSQL 17.10 的一次性验证库
`rednote_verify_wp22` 的实测导出**（`pg_attribute` / `pg_constraint` /
`pg_indexes` / `pg_trigger` / `pg_proc`），不是从代码抄的 ——
代码是意图，库是事实，两者不符时以库为准并回头修代码。导出命令见第七节。

---

## 一、文件怎么用

1. 主 agent 用 `models.py` 生成迁移（`alembic revision --autogenerate`），
   然后用本文件**逐条核对**生成的 DDL 是否与预期一致 ——
   尤其是 **6 条 `DEFERRABLE INITIALLY DEFERRED` 外键**
   （`information_drafts` 1 条 / `production_baselines` 3 条 /
   `step_approvals` 2 条 / `baseline_items` 0 条）与**两条部分索引**
   （autogenerate **读不回部分索引的 `WHERE`** 时最容易漏，见 §7.1）。
2. 迁移必须调 `ddl.all_trigger_ddl()` 把**九个**触发器一并建出。
   **其中 `trg_version_nodes_baseline_paired` 挂在共用表 `version_nodes` 上**
   （它是 OP-88 的反向判据），详见第五节。
3. **升级与降级都必须只碰本模块自己的对象**（见第六节）。

## 二、与数据结构 02 的对应关系

来源：`Rednote研发计划_02_数据结构与一致性.md` §5（版本注册表）与
原型 `rednote-flow.html` 步骤 3（L42~270）。

| 数据结构 02 的概念 | 本包是否落地 | 备注 |
|---|---|---|
| 步骤 3 的**审核工作区**（事实 / 冲突 / 场景 / 边界 / 研究选用） | ✅ `information_drafts` | **可变**，一项目一份（`project_id` 唯一） |
| **基线**（BL-v3 这种，绑定 Brief 版本 + 规则集读数） | ✅ `production_baselines` | **不可变**；`id` 同时 FK `version_nodes.id` |
| 基线内的**条目**（含未纳入项与原因） | ✅ `baseline_items` | **不可变**；`ref` + `snapshot` 双存 |
| **跨步骤批准**（S2 的 D4「谁、何时、为什么」） | ✅ `step_approvals` | **不可变**；闭合 OP-151 的形状未定义 |
| 基线**生效状态** | ❌ **刻意不落列** | 由 `projects.active_baseline_id` 投影（见 §四.1） |

**本包没有新建"基线身份表"**：`production_baselines` 自己就是版本化实体
（`id PK` + `id FK version_nodes.id`），与 `brief_versions` / `knowledge_versions`
同一形态。因此 `version_nodes.entity_id` 对 `BASELINE` 取 **`project_id`**
—— 一个项目的基线的"逻辑实体"就是该项目的那条基线链。

## 三、建表顺序与拆环

依赖关系（`→` 读作"引用"）：

```
version_nodes            ← WP11 已建，本包只加一个触发器，不改结构
    ↑          ↑
    │ (id)     │ (id)
    │      production_baselines ──┐ (supersedes_id 自引用)
    │          ↑                 │
    │          │ (baseline_id)   │
    │      baseline_items        │
    │          │                 │
    │          ├─→ knowledge_assets（WP20 已建）
    │          └─→ knowledge_versions（WP20 已建）
    │
    ├── information_drafts ──→ projects（已建）
    ├── production_baselines ──→ projects / users
    └── step_approvals ──────→ projects / users
```

**建表顺序：`information_drafts` → `production_baselines` → `baseline_items`
→ `step_approvals`。**

**表级没有环**：四张表引用的都是**已存在**的表（`version_nodes` / `projects` /
`users` / `knowledge_assets` / `knowledge_versions`）或**本包内更早建**的表
（`baseline_items` → `production_baselines`）。`production_baselines.supersedes_id`
与 `step_approvals.supersedes_id` 是**自引用**，PG 允许在 `CREATE TABLE` 内声明。
因此按此序 `CREATE TABLE` 一次成功，不需要 `ALTER TABLE ... ADD CONSTRAINT` 分两步。

### 3.1 那 `DEFERRABLE` 在这里管什么？

管**行级**的插入顺序，不管表的存在性：

| 外键 | 为什么必须 `DEFERRABLE INITIALLY DEFERRED` |
|---|---|
| `production_baselines.id → version_nodes.id` | 注册表行与子表行由**同一个事务、两个不同代码路径**写入（`versions.service.register_version` 内部有 SAVEPOINT + 取号重试，**不暴露"节点行何时落库"**）。若立即校验，应用必须猜对顺序，而那个顺序不是应用能控制的 |
| `step_approvals.subject_version_id → version_nodes.id` | 批准的是"步骤 + 版本"的组合；被批准的版本节点由 `register_version` 产出，时机同上 |
| `information_drafts.brief_version_id → version_nodes.id` | 草稿要先指向 Brief 版本，而该版本可能在同一事务的后半段才注册 |
| `production_baselines.brief_version_id → version_nodes.id` | 同上（基线"绑定 Brief v2"） |
| `production_baselines.supersedes_id → production_baselines.id` | 替代链在**同一个 `insert_baselines` 批量写**里插入：新行可能在旧行之前被 `INSERT` |
| `step_approvals.supersedes_id → step_approvals.id` | 同上（"先拒后批"时两条批准同事务写） |

**必须显式说明的一点**：`DEFERRABLE` 管的是**事务提交期**
（约束检查推迟到 `COMMIT` / `SET CONSTRAINTS ALL IMMEDIATE`），
**它管不了 DDL 期的表存在性** —— 一个 `DEFERRABLE` 外键仍然要求
**被引用的表在 `CREATE TABLE` 那一刻已经存在**。所以：
表的存在性靠第三节的顺序排，行的先后靠 `DEFERRABLE` 绕开，两者是两件事。

### 3.2 `version_nodes` 是共用表，本包只加触发器不改结构

`version_nodes` 由 WP11 建，被 `BRIEF`(WP18) / `KNOWLEDGE`(WP20) /
`BASELINE`(WP22) / `PROMPT`(WP23) 共用。本包对它做的**唯一**一件事是
新增一个触发器 `trg_version_nodes_baseline_paired`，
且用 `WHEN (NEW.entity_type = 'BASELINE')` 收窄 ——
不加条件会让每次 `BRIEF` / `KNOWLEDGE` / `PROMPT` / `CONTENT` 版本注册
都白跑一次函数。**不改列、不改索引、不改既有触发器。**

⚠️ **本包之前，`ENTITY_TYPE_BASELINE` 是一个"有定义、无生产者"的类型**
（`versions/models.py` L67 已定义，`ck_version_nodes_entity_type_valid`
的 8 类里也含它，但在 WP22 之前**没有任何代码写过这个值**）。
本包是它的第一个生产者 —— 因此 `baseline_node_paired_ddl()` 的反向判据
（第五节 #2）也是**第一次真正生效**，此前它没有被触发过。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 四、四张表的逐列 DDL（本文件是 specs，不是可执行代码）
# ---------------------------------------------------------------------------

INFORMATION_DRAFTS_DDL = """
-- 表名：information_drafts。步骤 3 的**审核工作区**。
-- ⚠️ 与 WP18 的 brief_drafts 名字极像、作用完全不同（见说明文档 §1.2 R6）：
--    brief_drafts = Brief **解构前**的工作区，产出版本；
--    information_drafts = 步骤 3 的**审核**工作区，基线**只读到那一版为止**。
--    独立成表是刻意的：共用会让"审核时改了字段 → 解构结果被覆盖"双向踩踏，
--    而两边读起来都正常（不报错，只是数据在错误的地方被改了）。
--
-- **可变**（与另三张表相反）：运营在里面反复改是它的正常用法，
-- 因此本表**没有**不可变触发器，靠 `lock_version` 做 ETag 乐观锁。
-- **没有 state 列**：状态由 `claims` / `conflicts` / `scenes` 三个数组算出（决策 D7）。
CREATE TABLE information_drafts (
    id                 uuid         NOT NULL DEFAULT gen_random_uuid(),
    project_id         uuid         NOT NULL,
    brief_version_id   uuid,          -- 本工作区正在审核的 Brief 版本（钉住）
    claims             jsonb        NOT NULL DEFAULT '[]'::jsonb,
    conflicts          jsonb        NOT NULL DEFAULT '[]'::jsonb,
    scenes             jsonb        NOT NULL DEFAULT '[]'::jsonb,
    boundaries         jsonb        NOT NULL DEFAULT '{}'::jsonb,
    research_selection jsonb        NOT NULL DEFAULT '[]'::jsonb,
    operator_confirmed boolean      NOT NULL DEFAULT false,
    schema_version     integer      NOT NULL DEFAULT 1,
    created_at         timestamptz  NOT NULL DEFAULT now(),
    updated_at         timestamptz  NOT NULL DEFAULT now(),
    lock_version       bigint       NOT NULL DEFAULT 1,

    CONSTRAINT pk_information_drafts PRIMARY KEY (id),

    -- 一项目一份（与 briefs.project_id 同构）
    CONSTRAINT uq_information_drafts_project_id UNIQUE (project_id),

    -- 形状 CHECK：数组列必须是数组，对象列必须是对象。
    -- **不判非空**：一份刚创建的草稿本来就什么都没审核（`[]` 是合法初值）。
    -- WP20 的 structured_payload_not_empty 判非空是因为那里承载"事实字段"，
    -- 空对象等于没写事实 —— 两处判据不同是**语义不同**，刻意的。
    CONSTRAINT ck_information_drafts_claims_is_array CHECK (jsonb_typeof(claims) = 'array'),
    CONSTRAINT ck_information_drafts_conflicts_is_array CHECK (jsonb_typeof(conflicts) = 'array'),
    CONSTRAINT ck_information_drafts_scenes_is_array CHECK (jsonb_typeof(scenes) = 'array'),
    CONSTRAINT ck_information_drafts_research_selection_is_array CHECK (jsonb_typeof(research_selection) = 'array'),
    CONSTRAINT ck_information_drafts_boundaries_is_object CHECK (jsonb_typeof(boundaries) = 'object'),

    -- 上界：防止某次导入把整本书塞进 claims（MAX_DRAFT_ITEMS = 2000）
    CONSTRAINT ck_information_drafts_claims_within_bound CHECK (jsonb_array_length(claims) <= 2000),
    CONSTRAINT ck_information_drafts_conflicts_within_bound CHECK (jsonb_array_length(conflicts) <= 2000),
    CONSTRAINT ck_information_drafts_scenes_within_bound CHECK (jsonb_array_length(scenes) <= 2000),
    CONSTRAINT ck_information_drafts_research_within_bound CHECK (jsonb_array_length(research_selection) <= 2000),

    CONSTRAINT ck_information_drafts_schema_version_positive CHECK (schema_version >= 1),

    -- 全部外键 ON DELETE RESTRICT：落库即证据，不允许级联删除
    -- ⚠️ brief_version_id 这条**必须 DEFERRABLE INITIALLY DEFERRED**（见 §3.1）
    CONSTRAINT fk_information_drafts_brief_version_id_version_nodes
        FOREIGN KEY (brief_version_id) REFERENCES version_nodes (id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_information_drafts_project_id_projects
        FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE RESTRICT
);

CREATE INDEX ix_information_drafts_project_id ON information_drafts (project_id);
-- 排序用：草稿按最近更新排（原型 L42 的"最近更新"）
CREATE INDEX ix_information_drafts_updated_at ON information_drafts (updated_at);
"""


PRODUCTION_BASELINES_DDL = """
-- 表名：production_baselines。一条基线。**不可变**。
--
-- ⚠️ **本表有意没有 state 列**，这是与设计文档 §2.1 第 1 层的**冲突取舍**，
--    详见 §四.1。一句话：`ACTIVE → SUPERSEDED` 这个动作本身就是"原地改写旧行"，
--    而 AC-08 说修改必须创建新版本 —— 两者不能同时成立，本实现取 AC-08 那一侧。
--
-- snapshot 是**基线级**快照（原型 L56~64 的 9 项里属于"整条基线"的那几项：
-- 批准人 / 批准时间 / 规则集读数 / 已确认限制 / 事实清单摘要），
-- 与 baseline_items 的**条目级**快照严格区分。
CREATE TABLE production_baselines (
    id               uuid          NOT NULL,          -- = 版本节点 id（见 fk_production_baselines_id_version_nodes）
    project_id       uuid          NOT NULL,
    brief_version_id uuid,           -- 钉住"哪一版 Brief"（原型 L60「BL-v3 绑定 Brief v2」）
    ruleset_version  varchar(200)  NOT NULL,          -- 审核规则集标签。**只被快照，不被执行**（判定归 WP30）
    snapshot         jsonb         NOT NULL,
    supersedes_id    uuid,           -- 被本条替代的那条基线。**单向**：旧行上不存 superseded_by_id
    change_reason    varchar(1000) NOT NULL,
    schema_version   integer       NOT NULL DEFAULT 1,
    created_at       timestamptz   NOT NULL DEFAULT now(),
    created_by       uuid,

    CONSTRAINT pk_production_baselines PRIMARY KEY (id),

    CONSTRAINT ck_production_baselines_ruleset_version_not_empty CHECK (char_length(ruleset_version) > 0),
    CONSTRAINT ck_production_baselines_change_reason_not_empty CHECK (char_length(change_reason) > 0),
    CONSTRAINT ck_production_baselines_snapshot_is_object CHECK (jsonb_typeof(snapshot) = 'object'),
    -- 自引用必须排除自身 —— 否则"我替代了我自己"这条无意义的关系合法
    CONSTRAINT ck_production_baselines_supersedes_not_self CHECK (supersedes_id IS NULL OR supersedes_id <> id),
    CONSTRAINT ck_production_baselines_schema_version_positive CHECK (schema_version >= 1),

    -- ⚠️ id 这条是本表最关键的 FK：它就是 OP-88 的"版本表 id 同时 FK 注册表"。
    CONSTRAINT fk_production_baselines_id_version_nodes
        FOREIGN KEY (id) REFERENCES version_nodes (id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_production_baselines_brief_version_id_version_nodes
        FOREIGN KEY (brief_version_id) REFERENCES version_nodes (id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_production_baselines_supersedes_id_production_baselines
        FOREIGN KEY (supersedes_id) REFERENCES production_baselines (id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_production_baselines_project_id_projects
        FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_production_baselines_created_by_users
        FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT
);

-- ⚠️⚠️ **第 1 层防线：部分唯一索引。`WHERE` 是判据的一部分，漏写会静默退化。**
-- 漏掉 WHERE → 退化成"每项目只能有一条基线" → 与 AC-08（修改创建新版本）**直接冲突**，
-- 且症状**迟到**（第二次创建基线时才报错），中间所有测试全绿。本机实测确认。
-- 它守的不变量是：**每个项目至多一条链根**（替代关系是链，两条互不相干的链
-- 会让"当前生效"有第二个候选）。
CREATE UNIQUE INDEX uq_production_baselines_project_root
    ON production_baselines (project_id) WHERE supersedes_id IS NULL;

CREATE INDEX ix_production_baselines_project_id ON production_baselines (project_id, created_at);
CREATE INDEX ix_production_baselines_supersedes_id ON production_baselines (supersedes_id);
CREATE INDEX ix_production_baselines_brief_version_id ON production_baselines (brief_version_id);
"""


BASELINE_ITEMS_DDL = """
-- 表名：baseline_items。基线内的一条条目。**不可变**。
-- 三处刻意的列设计（说明文档 §2.3 决策 1、AC-24 原文）：
--   ① ref + snapshot 双存；② inclusion + exclusion_reason；③ snapshot_text 与 snapshot 并存。
CREATE TABLE baseline_items (
    id                   uuid          NOT NULL DEFAULT gen_random_uuid(),
    baseline_id          uuid          NOT NULL,
    item_no              integer       NOT NULL,
    item_kind            varchar(20)   NOT NULL,
    inclusion            varchar(20)   NOT NULL,
    ref_label            varchar(100)  NOT NULL,   -- 人读的短标识（原型「事实 v7」「R-014」）
    -- --- ref：钉死"哪条知识的哪一版"（决策 D1） --------------------------
    knowledge_asset_id   uuid,
    -- **钉死版本**，而不是让读方去追 knowledge_assets.active_version_id ——
    -- 那正是 R1（基线被知识更新悄悄污染）。设计文档 §2.2 的次场景
    -- （9-15 批准 62%，9-20 知识更正为 60%）验证的就是这一列。
    knowledge_version_id uuid,
    -- --- snapshot：当时批准的值（决策 D3） --------------------------------
    snapshot             jsonb         NOT NULL,
    snapshot_text        text,          -- 当时人读的原文（半年后查事故的第一手材料）
    -- --- 未纳入原因（AC-24 原文） ----------------------------------------
    exclusion_reason     varchar(1000),
    context_order        integer       NOT NULL,
    created_at           timestamptz   NOT NULL DEFAULT now(),

    CONSTRAINT pk_baseline_items PRIMARY KEY (id),

    -- 同一条基线内条目号唯一
    CONSTRAINT uq_baseline_items_baseline_item_no UNIQUE (baseline_id, item_no),
    -- **同一条知识不能既纳入又排除**：两条行同时存在时，"这条被采纳了吗"
    -- 有两个矛盾答案，而下游（WP26）只会取先读到的那条。
    -- 排除项的 knowledge_asset_id 可为 NULL（研究资料不一定是知识资产），
    -- 而 NULL 不参与唯一性 —— 这是想要的（多条无资产引用的研究资料可并存）。
    CONSTRAINT uq_baseline_items_baseline_asset UNIQUE (baseline_id, knowledge_asset_id),

    CONSTRAINT ck_baseline_items_item_no_positive CHECK (item_no >= 1),
    CONSTRAINT ck_baseline_items_context_order_positive CHECK (context_order >= 1),
    CONSTRAINT ck_baseline_items_item_kind_valid
        CHECK (item_kind IN ('FACT', 'RESEARCH', 'RULE', 'SCENE', 'STYLE')),
    CONSTRAINT ck_baseline_items_inclusion_valid
        CHECK (inclusion IN ('EXCLUDED', 'INCLUDED')),
    CONSTRAINT ck_baseline_items_ref_label_not_empty CHECK (char_length(ref_label) > 0),
    CONSTRAINT ck_baseline_items_snapshot_is_object CHECK (jsonb_typeof(snapshot) = 'object'),
    -- 空快照 = 只有引用没有值 —— 那正是"只存 ref"的形状，也就是 R1。必须拦。
    CONSTRAINT ck_baseline_items_snapshot_not_empty CHECK (snapshot <> '{}'::jsonb),
    -- 排除必须说为什么（纳入不必 —— 纳入的理由在 ref 与 snapshot 里）
    CONSTRAINT ck_baseline_items_excluded_needs_reason
        CHECK (inclusion <> 'EXCLUDED'
               OR (exclusion_reason IS NOT NULL AND char_length(exclusion_reason) > 0)),
    -- 纳入的项目不该带排除原因（带了说明状态与原因不一致）
    CONSTRAINT ck_baseline_items_included_has_no_reason
        CHECK (inclusion <> 'INCLUDED' OR exclusion_reason IS NULL),
    -- 有版本引用就必须有资产引用 —— 否则"版本指向谁"无从判断
    CONSTRAINT ck_baseline_items_version_ref_needs_asset_ref
        CHECK (knowledge_version_id IS NULL OR knowledge_asset_id IS NOT NULL),
    -- item_no 是 int 列；上界显式写出，避免溢出时报出"整数超出范围"
    -- （那种错误读起来像程序 bug，其实是数据到了极限）
    CONSTRAINT ck_baseline_items_item_no_in_range CHECK (item_no <= 2147483647),

    CONSTRAINT fk_baseline_items_baseline_id_production_baselines
        FOREIGN KEY (baseline_id) REFERENCES production_baselines (id) ON DELETE RESTRICT,
    CONSTRAINT fk_baseline_items_knowledge_asset_id_knowledge_assets
        FOREIGN KEY (knowledge_asset_id) REFERENCES knowledge_assets (id) ON DELETE RESTRICT,
    CONSTRAINT fk_baseline_items_knowledge_version_id_knowledge_versions
        FOREIGN KEY (knowledge_version_id) REFERENCES knowledge_versions (id) ON DELETE RESTRICT
);

CREATE INDEX ix_baseline_items_baseline_id ON baseline_items (baseline_id, item_no);
CREATE INDEX ix_baseline_items_knowledge_asset_id ON baseline_items (knowledge_asset_id);
CREATE INDEX ix_baseline_items_knowledge_version_id ON baseline_items (knowledge_version_id);
"""


STEP_APPROVALS_DDL = """
-- 表名：step_approvals。一次跨步骤批准。**不可变**。**闭合 OP-151**。
--
-- OP-151 原文（登记册 L486）说协议**未定义**这个形状。本包必须现在定 ——
-- 因为第 4 层防线（第五节 #4）要校验"基线在 COMMIT 时有对应批准"，
-- **没有形状就写不出校验**。
--
-- 批准的**对象是「步骤 + 版本」的组合**，不是"步骤"：
-- Brief 出了 v2 之后，步骤 1 的"已批准"指的是 **v2 已批准**，
-- 而不是"步骤 1 曾经被批过"。只记步骤会让"这一步批的是哪一版"
-- 在数据里**没有答案**，而八步闭环的验收（证据 = 「固定版本」）恰恰要回答它。
--
-- subject_version_id 指向 version_nodes.id（**不是领域表**），因此它**通用**：
-- 步骤 1 存 brief 版本节点、步骤 3 存基线版本节点，同一张表不需要为每步加一列。
CREATE TABLE step_approvals (
    id                 uuid          NOT NULL DEFAULT gen_random_uuid(),
    project_id         uuid          NOT NULL,
    step_no            integer       NOT NULL,
    subject_kind       varchar(30)   NOT NULL,
    subject_version_id uuid          NOT NULL,
    decision           varchar(20)   NOT NULL,
    decided_by         uuid,
    -- **显式的列，不靠 created_at**：两者在本表里通常相同，但
    -- "决策时刻"与"落库时刻"是两件事（补录历史批准时它们不同），
    -- 而 S2 的 D4 要求"谁、何时、为什么"三个答案都显式。
    decided_at         timestamptz   NOT NULL DEFAULT now(),
    reason             varchar(1000) NOT NULL,
    supersedes_id      uuid,          -- 被本条替代的上一条批准（撤销靠新增，不靠改写）
    created_at         timestamptz   NOT NULL DEFAULT now(),

    CONSTRAINT pk_step_approvals PRIMARY KEY (id),

    CONSTRAINT ck_step_approvals_step_no_in_range CHECK (step_no >= 1 AND step_no <= 8),
    CONSTRAINT ck_step_approvals_subject_kind_valid
        CHECK (subject_kind IN ('BASELINE', 'BRIEF', 'RESEARCH')),
    CONSTRAINT ck_step_approvals_decision_valid
        CHECK (decision IN ('APPROVED', 'REJECTED')),
    CONSTRAINT ck_step_approvals_reason_not_empty CHECK (char_length(reason) > 0),
    CONSTRAINT ck_step_approvals_supersedes_not_self
        CHECK (supersedes_id IS NULL OR supersedes_id <> id),

    CONSTRAINT fk_step_approvals_subject_version_id_version_nodes
        FOREIGN KEY (subject_version_id) REFERENCES version_nodes (id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_step_approvals_supersedes_id_step_approvals
        FOREIGN KEY (supersedes_id) REFERENCES step_approvals (id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_step_approvals_project_id_projects
        FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_step_approvals_decided_by_users
        FOREIGN KEY (decided_by) REFERENCES users (id) ON DELETE RESTRICT
);

-- ⚠️ **第二条部分唯一索引。`WHERE` 同样是判据的一部分。**
-- 它守的不变量：每个 (项目, 步骤, 对象类型, 版本) 至多一条**链根**批准。
-- 漏掉 WHERE → 退化成"同一版只能有一条批准" → 让"先拒后批"变成不可能。
-- 它同样是**静默**的（症状迟到：第二次批准同一版时）。
-- 与基线的链根唯一性同一手法：撤销靠**新增**一条带 supersedes_id 的。
CREATE UNIQUE INDEX uq_step_approvals_root
    ON step_approvals (project_id, step_no, subject_kind, subject_version_id)
    WHERE supersedes_id IS NULL;

CREATE INDEX ix_step_approvals_project_step ON step_approvals (project_id, step_no, decided_at);
CREATE INDEX ix_step_approvals_subject_version_id ON step_approvals (subject_version_id);
CREATE INDEX ix_step_approvals_supersedes_id ON step_approvals (supersedes_id);
"""


# ---------------------------------------------------------------------------
# 五、本包对设计文档的两处有意偏离（每一处都有理由，不是笔误）
# ---------------------------------------------------------------------------

DELIBERATE_DEVIATIONS = """
### 5.1 `production_baselines` **没有** `state` 列（与设计文档 §2.1 第 1 层的冲突取舍）

设计文档 §2.1 第 1 层写的是「部分唯一索引 —— 每项目一个 **ACTIVE** 基线」，
而 §2.3 决策 5 与 AC-08 要求「基线**不可原地修改**」。**两者不能同时成立**：

> 一个 `state` 从 `ACTIVE` 变成 `SUPERSEDED` 的动作，就是一次
> 「原地改写旧行」—— 而 AC-08 说修改必须创建新版本。

本实现取**忠实于 AC-08 的那一侧**，并在此登记该冲突（收口时同步登记册）：

| 层 | 设计文档原方案 | 本实现 | 为什么仍然成立 |
|---|---|---|---|
| 第 1 层 | 部分唯一索引 `WHERE state='ACTIVE'` | 部分唯一索引 `WHERE supersedes_id IS NULL`（**链根唯一**） | 见下 |
| "当前生效的是哪条" | 读 `state='ACTIVE'` 的那一行 | 读 `projects.active_baseline_id`（**投影列**） | 一个列**物理上装不下两个值** → "每项目一个生效基线"由结构保证，比索引更强 |

**这条取舍没有削弱第 1 层**：「每项目一个 ACTIVE 基线」这条不变量在本实现里由
**`projects.active_baseline_id` 是单值列**保证 —— 这比部分唯一索引**更硬**
（索引拦的是"插入第二行"，列拦的是"同时存在两个值"这件事本身）。
而部分唯一索引并未取消，它守的是另一条**真实且可插入期校验**的不变量：
**每个项目只能有一条链根**。

这与 WP20 的「状态由事件流投影算出，不是一列」是**同一条原则**
（`rebuild_active_version` ↔ `service.rebuild_active_baseline`）。

### 5.2 撤销/替代**不落** `superseded_by_id`（决策 D5）

新行记 `supersedes_id` 指向被它替代的那条；**旧行上不存 `superseded_by_id`**
—— 那一列意味着"改写旧行"，而旧行不可变。

**设计文档 §2.3 决策 4 的表格里出现了 `superseded_by_id` 这一列**，
而同一处备注说"不可变表的撤销靠新增"。本实现取**备注方向**
（表格那一行是笔误，收口时一并登记）。
"谁替代了 BL-v2" 由**反向查询** `WHERE supersedes_id = <id>` 得到。

### 5.3 `information_drafts` 的四个数组列**不判非空**（与 WP20 刻意不同）

WP20 的 `ck_knowledge_versions_structured_payload_not_empty` 判非空，
本包不判。**这不是不一致，是语义不同**：
`structured_payload` 承载"事实字段"，空对象 = 没写事实；
`claims` / `conflicts` / `scenes` / `research_selection` 是**运营正在编辑的内容**，
一份刚创建的草稿本来就什么都没审核（`[]` 是合法初值）。
"""


# ---------------------------------------------------------------------------
# 六、触发器清单（9 个）
# ---------------------------------------------------------------------------

TRIGGERS = """
| # | 触发器 | 挂在哪张表 | 类型 | 作用 |
|---|---|---|---|---|
| 1 | `trg_production_baselines_immutable` | `production_baselines` | BEFORE UPDATE/DELETE | 拒绝原地改写（复用 `rednote_reject_mutation()`） |
| 2 | `trg_baseline_items_immutable` | `baseline_items` | 同上 | 同上 |
| 3 | `trg_step_approvals_immutable` | `step_approvals` | 同上 | 同上 |
| 4 | `trg_production_baselines_node_consistency` | `production_baselines` | **CONSTRAINT TRIGGER, DEFERRABLE INITIALLY DEFERRED** AFTER INSERT | OP-88 判据 1~3（正向） |
| 5 | `trg_version_nodes_baseline_paired` | **`version_nodes`（共用表）** | 同上（带 `WHEN (NEW.entity_type = 'BASELINE')`） | OP-88 反向判据：`BASELINE` 节点必须配对 `production_baselines` 行 |
| 6 | `trg_baseline_items_cross_brand` | `baseline_items` | 同上 | 第 3 层：跨品牌不得引用（AC-24） |
| 7 | `trg_baseline_items_asset_scope` | `baseline_items` | 同上 | 第 3 层之二：引用的**版本**必须属于引用的**资产** |
| 8 | `trg_production_baselines_approval_required` | `production_baselines` | 同上 | 第 4 层：必须有**同事务**批准（OP-151） |
| 9 | `trg_production_baselines_no_blocking` | `production_baselines` | 同上 | 第 5 层：存在阻断项不得创建基线（AC-09） |

**#4~#9 必须 `DEFERRABLE INITIALLY DEFERRED`**：它们校验的都是
"同一事务内另一次 INSERT 的结果"，而顺序由应用与版本注册机制共同决定（见 §3.1）。

**#1~#3 共用同一个函数 `rednote_reject_mutation()`**（`platform.immutability`）：
各写一份会在 downgrade 时撞
"cannot drop function ... because other objects depend on it"（WP11 踩过、WP16 差点再踩）。
**本包只挂触发器，不重建函数**。

**#5 是本包唯一挂到非本模块表上的对象**：它不修改 WP11 的任何代码或表结构。

**#4、#8、#9 三个延迟触发器同挂 `production_baselines`** —— 这有一个
**必须记住的副作用**（见 §8.2 的诚实缺口说明）：同一行插入时三条都会跑，
**按触发器名字典序**，`approval_required` < `node_consistency` < `no_blocking`。
写测试时若不先插批准，断言"节点一致性拦住了"会**假通过** ——
实际拦下它的是 `approval_required`（同样是 `23514`）。

### 6.1 六个一致性函数的实际判据（从库导出，逐句核对过）

```
rednote_check_baseline_version_consistency   (ON production_baselines AFTER INSERT)
    ① version_nodes 里必须有一条 id = NEW.id 的行（否则 RAISE）
    ② 且它的 entity_type 必须是 'BASELINE'（否则 RAISE）
    ③ 且它的 entity_id 必须 = NEW.project_id（否则 RAISE）
    ⚠️ 注意 ① 在正常情况下**永远为真**：`fk_production_baselines_id_version_nodes`
       已经保证被引用的节点行存在。因此 ① 是**防御性分支**，不是主判据 ——
       它唯一能生效的场合是"同一事务内节点行被删"，而那被 #5 的反向判据
       与 version_nodes 自身的形态挡住。**测试时不要把 ① 当成可达路径。**

rednote_check_baseline_node_paired         (ON version_nodes AFTER INSERT, WHEN entity_type='BASELINE')
    ← 这条是**刻意收窄**的，见 §6.2
    若 projects 里**存在** id = NEW.entity_id 的行：
        则 production_baselines 里必须有 id = NEW.id 的行（否则 RAISE）
    若 projects 里不存在该项目：
        直接 RETURN NULL（放行）

rednote_check_baseline_item_scope          (ON baseline_items AFTER INSERT)
    NEW.knowledge_asset_id IS NULL → 放行（研究资料/风格/规则不一定引用知识资产）
    否则取基线所属项目的 (brand_id, product_id, id) 与知识资产的
    (scope, brand_id, product_id, project_id)，按 scope 分支：
        GLOBAL  → 放行（**不是放水**：FACT 类别被 WP20 的
                  ck_knowledge_assets_fact_requires_concrete_scope 禁止为 GLOBAL，
                  因此放行 GLOBAL 不会让任何一条**事实**跨界）
        BRAND   → k_brand 必须 ≡ proj_brand
        PRODUCT → k_brand ≡ proj_brand 且 k_product ≡ proj_product
        PROJECT → k_project 必须 ≡ proj_id
        其它取值 → RAISE（受控取值域外的值）
    ⚠️ **全部用 `IS DISTINCT FROM`（NULL 安全），不能用 `<>`** —— 见 §6.3

rednote_check_baseline_item_asset_scope    (ON baseline_items AFTER INSERT)
    NEW.knowledge_version_id IS NULL → 放行（同款收窄）
    knowledge_versions 里必须有该版本行（否则 RAISE）
    且它的 asset_id 必须 ≡ NEW.knowledge_asset_id（否则 RAISE）

rednote_check_baseline_approval            (ON production_baselines AFTER INSERT)
    存在一条 step_approvals 满足**全部**下列条件（否则 RAISE）：
      · project_id = NEW.project_id
      · step_no = 3（= STEP_MIN + 2，从 baseline_types 派生，不写字面量）
      · subject_kind = 'BASELINE'
      · subject_version_id = NEW.id
      · decision = 'APPROVED'（拒绝不构成依据）
      · 且**未被取代**（不存在另一行的 supersedes_id = 它的 id）
        —— 一条已被后续批准取代的旧批准不构成依据，否则"先批后撤"形同没撤

rednote_check_baseline_no_blocking         (ON production_baselines AFTER INSERT)
    对本项目 information_drafts.conflicts 这个 JSONB 数组做**否定式查询**：
      不存在 COALESCE((c->>'blocking')::boolean, false) 且
             COALESCE(c->>'state','') <> 'SETTLED' 的元素
    否则 RAISE（消息里回带数量与冲突 id 样本）
    ⚠️ **不读任何 count 列**（决策 D7）—— 一个由别处维护的 blocking_count
       与明细脱节时**不报错**，只会让 AC-09 失效
    ⚠️ **草稿不存在时放行**：`information_drafts` 一项目一份，但"没有草稿"
       是合法的中间态（还没开始审核）。此时明细行为空，"不存在未处置的阻断
       冲突"这个否定式**天然成立** —— 判据正确地放行，不是因为漏判。
       服务层的 readiness 在同情况下会因条件 ② 未满足而拒绝
       （没有场景就没有可生产场景），两层各管自己那一半。
```

### 6.2 **反向判据为什么必须收窄（OP-88 的关键，与另三个包同一处理）**

`trg_version_nodes_baseline_paired` 的字面表述是
"`BASELINE` 节点必须配对 `production_baselines` 行"。若照字面实现，
**它会拦下本包自己也不该拦的东西**：

`version_nodes` 是共用表。8 类版本实体里只有 `BRIEF` / `KNOWLEDGE` /
`BASELINE` / `PROMPT` 在本批落地，其余 4 类的领域表**还不存在**；
而"先注册节点、后落领域行"是一个合法的中间态。
若不做 `entity_id` 存在性前置判断，那个中间态会被判成"孤立节点"而报错 ——
于是本包的触发器把别的包的版本注册路径也拦了。

**因此实现里加了前置判断**：只有 `projects` 里确实存在 `id = entity_id`
的行时，才要求配对 `production_baselines` 行。这条不是"放宽"，
而是把判据限定在**本包确实负责的那一半语义**上：
"**如果这个节点确实是在描述本项目的一条基线**，那它必须有领域行"。

⚠️ **收窄的代价已登记**：`entity_id` 指向**不存在项目**的"悬空节点"
不会被拦。危害等级低（它不被任何东西引用，溯源也走不到它），
而"节点说 A、子表说 B"这类**互相矛盾**的形态被判据 ①②③ 完整覆盖。
**本机实测场景 5 就是这条收窄的对照组**（必须放行）。

### 6.3 ⚠️ 实现里最容易写错的一处：必须用 `IS DISTINCT FROM`

§6.1 的跨品牌判据要比较的列**全都可以为 NULL**
（`knowledge_assets.brand_id` 在 `scope='GLOBAL'` 时为 NULL）。若写成 `k_brand <> proj_brand`：

```
NULL <> uuid  →  NULL  →  IF 视为 false  →  **放行**
```

于是"知识没有品牌"与"项目有品牌"这个组合**静默通过** ——
而这正是跨品牌引用最常见的形态（一个 GLOBAL 形态的库里塞了一条带品牌的事实）。
`IS DISTINCT FROM` 是 NULL 安全的比较，它把 NULL 当成一个正常值参与相等判断。

**这条不变量不能用"只判非空"替代**：判非空**恒真**（任何写进去的值都非空），
与说明文档 §0.3 实测 1 试 3 是同一个形态（ISS-75 同族）。

**为什么跨品牌与"版本归属"拆成两个函数**（WP20/WP23 是合在一个里的）：
两条判据查的**不是同一张表** —— 跨品牌查 `knowledge_assets`，
版本归属查 `knowledge_versions`。合并后两条 `RAISE` 混在一起，
"这次失败是哪一个原因"只能靠读消息文本区分，而它们是两个不同的问题、两个不同的修法。

### 6.4 第 4 与第 5 层为什么必须是**数据库级**（不是"服务层自觉"）

说明文档 §0.3 的两条实测结论否定了服务层方案：

| 层 | 被否定的实现 | 实测结论 |
|---|---|---|
| 第 4 层（OP-151 同事务批准） | 布尔冗余列 `has_approval` + `CHECK` | 试 1：`has_approval=true` 而 approval 表里一行都没有，**CHECK 全绿** ✗（声称方与事实方分离，ISS-95 同族） |
| 第 5 层（AC-09 阻断项） | 退化成"服务层自觉" | 试 2c：**裸事务不报错**地长期开放 ✗ |

而第 5 层失效意味着**基线可以被非法推进** —— 这是本包最严重的后果
（说明文档 §1.6 R5）。`DEFERRABLE INITIALLY DEFERRED` 让校验发生在
**COMMIT 期**，因此"同事务"成为**硬约束**，并带来两个附带好处：
① 顺序不再是必须猜对的事；② **跨事务补批准这条路被物理封死**
（那是"事后补一张批准单"这种绕过的唯一入口，而它**不报错** ——
数据库里最终看起来是一致的：有基线、有批准，只是中间有一段时间不一致）。

### 6.5 与 `readiness.py` 的关系（**这不是"两处实现"，是两层**）

设计文档 §2.1 把这两者列为**第 5 层**（DB）与**第 7 层**（服务层纯函数）：

| | 第 5 层（本文件 #9） | 第 7 层（`readiness.py`） |
|---|---|---|
| 拦的是 | 绕过服务层的写入（裸 SQL / 数据回填） | 正常请求路径，给出**可读**的拒绝理由 |
| 判据来源 | 本文件拼接的 SQL | `step3_readiness()` |
| 失败时 | `sqlstate=23514` → 422 | 带 `reason` 与未满足条件的 422 |

**防止两层漂移的机制**：两处的**取值与键名都从 `baseline_types` 拼接**
（`CONFLICT_STATE_SETTLED`、JSONB 里的 `blocking` 键名），
且有**对账测试**：构造同一批冲突数据，断言
`readiness.blocking_open_conflicts()` 与数据库触发器的判定**同时**为真/为假。
若某天只改了一处，对账测试会红。**这是判据，不是声明** ——
测试文件 `tests/test_baseline_wp22.py` 里能指出对应的函数名。
"""


# ---------------------------------------------------------------------------
# 七、downgrade 的归属判据
# ---------------------------------------------------------------------------

DOWNGRADE_RULES = """
`downgrade` **必须只删本模块自己的对象**，顺序与建表相反：

```sql
-- 1. 先删九个触发器（本包建的，含挂在共用表 version_nodes 上的那一个）
DROP TRIGGER IF EXISTS trg_version_nodes_baseline_paired ON version_nodes;
DROP TRIGGER IF EXISTS trg_production_baselines_node_consistency ON production_baselines;
DROP TRIGGER IF EXISTS trg_production_baselines_approval_required ON production_baselines;
DROP TRIGGER IF EXISTS trg_production_baselines_no_blocking ON production_baselines;
DROP TRIGGER IF EXISTS trg_baseline_items_cross_brand ON baseline_items;
DROP TRIGGER IF EXISTS trg_baseline_items_asset_scope ON baseline_items;
DROP TRIGGER IF EXISTS trg_production_baselines_immutable ON production_baselines;
DROP TRIGGER IF EXISTS trg_baseline_items_immutable ON baseline_items;
DROP TRIGGER IF EXISTS trg_step_approvals_immutable ON step_approvals;

-- 2. 本包的**六个**一致性函数（不是共用函数，可以删）
DROP FUNCTION IF EXISTS rednote_check_baseline_version_consistency();
DROP FUNCTION IF EXISTS rednote_check_baseline_node_paired();
DROP FUNCTION IF EXISTS rednote_check_baseline_item_scope();
DROP FUNCTION IF EXISTS rednote_check_baseline_item_asset_scope();
DROP FUNCTION IF EXISTS rednote_check_baseline_approval();
DROP FUNCTION IF EXISTS rednote_check_baseline_no_blocking();

-- ⚠️⚠️ 绝不要 DROP FUNCTION rednote_reject_mutation() —— 它属于 platform，
--     被全项目的不可变表共用。删它会撞依赖错误，并让
--     WP11/WP15/WP16/WP17/WP18/WP20/WP23 的保护**一起失效**。

-- 3. 再删表（反序）
DROP TABLE IF EXISTS step_approvals;
DROP TABLE IF EXISTS baseline_items;
DROP TABLE IF EXISTS production_baselines;
DROP TABLE IF EXISTS information_drafts;
```

`ddl.consistency_items()` 存在的意义就是让第 1、2 步自动只覆盖
**本模块自己的**那 6 项，而不是手写一遍（手写会漏、会多删）：

```python
for table, _fn, _trg, drop_trigger, drop_function in ddl.consistency_items():
    op.execute(drop_trigger)
# 然后才是 drop_function —— 顺序不能反：函数仍被触发器引用时 PG 拒绝删除
for _t, _fn, _trg, _dt, drop_function in ddl.consistency_items():
    op.execute(drop_function)
```

**为什么必须用 `consistency_items()` 而不是 `all_trigger_ddl()`**：
后者混着不可变保护那 3 项，而它们的 `drop_function` 指向
**跨包共用**的 `rednote_reject_mutation()`。用后者会让 downgrade 删掉共用函数。

**`consistency_items()` 的长度是判据的一部分**：它与
`consistency_trigger_names()` / `consistency_function_names()` 必须**等长**（各 6 项）。
数错一个的后果是 downgrade **漏删一个函数**（残留对象会挡住后续迁移重建它）。
测试里有机械判据钉住这个等长关系。

### 7.1 autogenerate 做不到、必须手工补的三件事

| # | 症状 | 手工改动 |
|---|---|---|
| 1 | **部分索引的 `WHERE` 丢失** —— autogenerate 会把它写成普通索引 | 按 §四 补回 **两条**：`uq_production_baselines_project_root` 的 `WHERE supersedes_id IS NULL`；`uq_step_approvals_root` 的 `WHERE supersedes_id IS NULL` |
| 2 | **触发器不被包含** | 迁移里调 `ddl.all_trigger_ddl()`（**9 项**）。**且 `CREATE TRIGGER` 不是幂等的**（重复执行报 `DuplicateObject`，本机实测复现）→ 必须 `DROP TRIGGER IF EXISTS` → `CREATE OR REPLACE FUNCTION` → `CREATE TRIGGER` 三步 |
| 3 | **`postgresql` 方言未导入**（用 `ARRAY` / `UUID` 类型时会 NameError） | 补 `from sqlalchemy.dialects import postgresql` |

**G2 三件事**（若产出迁移草案要一并处理）：删 M0 探针表的 `op.drop_table` /
删 downgrade 里的重建语句 / 补 `from sqlalchemy.dialects import postgresql`。

**本包特有的第四件事**：`information_drafts` 的四个数组列
（`claims` / `conflicts` / `scenes` / `research_selection`）与 `boundaries`
用的是 `server_default=text("'[]'::jsonb")` / `text("'{}'::jsonb")`。
autogenerate **读不回 JSONB 的 server_default 文本**时会渲染成
`server_default=None`，导致 default 丢失 —— 逐条核对时看这五行。
"""


# ---------------------------------------------------------------------------
# 八、结构事实的导出命令（复现本节所有"实测"说法）
# ---------------------------------------------------------------------------

VERIFICATION_COMMANDS = """
⚠️ **本机 `psql` / `pg_isready` 不在 `PATH`**，因此下面的 SQL 实际是经
`psycopg`（3.3.5）与 SQLAlchemy 的 `engine.connect()` +
`conn.execute(text(sql))` 执行的。SQL 文本本身与 `psql -c` 完全一致，
换到有 `psql` 的机器上可直接用。

```bash
DB=rednote_verify_wp22
# 若从零建库：
#   createdb rednote_verify_wp22
#   然后 CREATE EXTENSION IF NOT EXISTS vector;（chunk_embeddings 依赖，缺了建表会报
#   type "vector" does not exist）
#   Base.metadata.create_all(conn) → 再按 ddl.all_trigger_ddl() 建九个触发器

# A. 列与默认值
SELECT c.relname, a.attname, format_type(a.atttypid,a.atttypmod), a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid)
FROM pg_class c
JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
WHERE c.relname IN ('information_drafts','production_baselines',
                    'baseline_items','step_approvals') AND c.relkind='r'
ORDER BY c.relname, a.attnum;

# B. 约束（含 CHECK 全文与 DEFERRABLE 标记）
SELECT conrelid::regclass::text, conname, contype, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid::regclass::text IN ('information_drafts','production_baselines',
                                   'baseline_items','step_approvals')
ORDER BY 1,3,2;

# C. 索引（**重点看两条部分索引的 WHERE 在不在**）
SELECT tablename, indexname, indexdef FROM pg_indexes
WHERE tablename IN ('information_drafts','production_baselines',
                    'baseline_items','step_approvals')
ORDER BY 1,2;

# D. 触发器（含挂到共用表 version_nodes 上的那一个）
SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid)
FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
WHERE NOT t.tgisinternal AND (c.relname IN ('information_drafts','production_baselines',
      'baseline_items','step_approvals') OR c.relname='version_nodes')
ORDER BY 1,2;

# E. 六个一致性函数体（**判据的唯一权威来源**）
SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname IN (
  'rednote_check_baseline_version_consistency',
  'rednote_check_baseline_node_paired',
  'rednote_check_baseline_item_scope',
  'rednote_check_baseline_item_asset_scope',
  'rednote_check_baseline_approval',
  'rednote_check_baseline_no_blocking') ORDER BY proname;

# F. 统计
SELECT conrelid::regclass::text AS tbl, contype, count(*) FROM pg_constraint
WHERE conrelid::regclass::text IN ('information_drafts','production_baselines',
      'baseline_items','step_approvals') GROUP BY 1,2 ORDER BY 1,2;
```

**为什么必须导出而不是读代码**：`models.py` 里的 `CheckConstraint` 字符串
只说明意图；PostgreSQL 重写后的表达式才是实际生效的规则
（例如 `item_kind IN (...)` 会变成
`(item_kind)::text = ANY ((ARRAY['FACT','RESEARCH',...])::text[])`）。
**两者不符时以库为准，并回头修代码。**

**为什么必须看 `pg_indexes.indexdef` 而不只看 `pg_constraint`**：
**部分唯一索引在 `pg_constraint` 里根本不出现**（它不是一个约束对象，
只是一个带 `WHERE` 的唯一索引）。只查约束表会**完全看不到**第 1 层防线 ——
这是本包最容易漏掉的一条检查。
"""


# ---------------------------------------------------------------------------
# 九、活性验证（30 条，真 PG 实测；这是"规格"里最硬的一半）
# ---------------------------------------------------------------------------

LIVE_VERIFICATION = """
**结构正确 ≠ 行为正确。** 上面八节的导出只能证明"对象都建出来了"；
一个**永远 `RAISE` 的触发器**也能让所有结构检查全绿。
因此每条不变量都必须有成对的正例/反例 —— **且反例必须能 FAIL**。

下表是一次性库 `rednote_verify_wp22` 上的实测结果（每条场景一个独立连接，
`SET CONSTRAINTS ALL IMMEDIATE` 强制在场景内触发延迟约束，
结束**无条件 `rollback()`** → 零残留）。

判据纪律（本脚本的第一版踩过，记录在此）：
**每条场景必须显式声明期望**（`None` = 必须通过；字符串 = 期望的 sqlstate）。
第一版让"期望为 None 时收到任何错误都算 PASS"，于是两条涉及
`knowledge_versions` 的场景双双栽在**与目标触发器无关**的外键 `23503` 上
却报 PASS —— 那正是本项目踩过十次的"判据不能区分两种实现"（ISS-82 同族）。
具体成因：`knowledge_versions.id` 有一条 `DEFERRABLE` FK 指向
`version_nodes.id`，造知识版本的探针**必须先注册 `KNOWLEDGE` 版本节点**。

| # | 场景 | 期望 | 实测 | 说明 |
|---|---|---|---|---|
| 1 | 正向判据① `id` 无对应 `version_nodes` 行 | 23503 | ✅ 23503 | FK 拦（**不是**触发器 —— 见 §6.1 的 ⚠️） |
| 2 | 正向判据② 节点 `entity_type≠BASELINE` | 23514 | ✅ 23514 | ⚠️ 见 §9.2 的诚实缺口 |
| 3 | 正向判据③ `entity_id` 与子表 `project_id` 不一致 | 23514 | ✅ 23514 | ⚠️ 见 §9.2 的诚实缺口 |
| 4 | 反向 `BASELINE` 节点无子表行（孤立节点） | 23514 | ✅ 23514 | 孤立节点被拦 |
| 5 | 反向的收窄【对照组】`entity_id` 非真实项目 | 通过 | ✅ 通过 | 收窄的行为证据（§6.2） |
| 6 | 合法配对【对照组】三层全满足 | 通过 | ✅ 通过 | 防"永远 RAISE" |
| 7 | 第 4 层 无同事务批准 | 23514 | ✅ 23514 | 未处置阻断也不存在，故只可能是它 |
| 8 | 第 4 层反例 `decision=REJECTED` | 23514 | ✅ 23514 | 拒绝不构成依据 |
| 9 | 第 4 层反例 生效批准已被取代 | 23514 | ✅ 23514 | "先批后撤"形同没撤 |
| 10 | 第 5 层 未处置阻断冲突 | 23514 | ✅ 23514 | 消息回带数量与 id 样本 |
| 11 | 第 5 层【对照组】阻断已 `SETTLED` | 通过 | ✅ 通过 | 处置后可推进 |
| 12 | 第 5 层【对照组】非阻断项未处置 | 通过 | ✅ 通过 | **不误拦** |
| 13 | 第 3 层 跨品牌（A 项目引 B 品牌知识） | 23514 | ✅ 23514 | AC-24 |
| 14 | 第 3 层【对照组】同品牌 `BRAND` scope | 通过 | ✅ 通过 | |
| 15 | 第 3 层【对照组】`PROJECT` scope 且是本项目 | 通过 | ✅ 通过 | |
| 16 | 第 3 层 别的项目的 `PROJECT` scope 知识 | 23514 | ✅ 23514 | 项目私有性 |
| 17 | 第 3 层【对照组】`GLOBAL` scope（`STYLE`） | 通过 | ✅ 通过 | 与 §6.1 的"不是放水"对应 |
| 18 | 第 3 层【对照组】同产品 `PRODUCT` scope | 通过 | ✅ 通过 | |
| 19 | 第 3 层之二 版本属于别的资产 | 23514 | ✅ 23514 | 本条**必须**先造 `KNOWLEDGE` 节点 |
| 20 | 第 3 层之二【对照组】版本确属该资产 | 通过 | ✅ 通过 | 防"永远 RAISE" |
| 21 | 不可变 UPDATE `production_baselines` | 23001 | ✅ 23001 | 两次注册用**不同** `revision_no`，否则 23505 会来自 `version_nodes` 的 UQ |
| 22 | 不可变 DELETE `baseline_items` | 23001 | ✅ 23001 | |
| 23 | 不可变 UPDATE `step_approvals` | 23001 | ✅ 23001 | |
| 24 | 可变【对照组】UPDATE `information_drafts` | 通过 | ✅ 通过 | 工作区必须可改 |
| 25 | 一项目一份 第二条 `information_drafts` | 23505 | ✅ 23505 | `uq_information_drafts_project_id` |
| 26 | 部分唯一索引 同项目第二条链根 | 23505 | ✅ 23505 | 报错约束名实测为 `uq_production_baselines_project_root`（**不是** `version_nodes` 的 UQ） |
| 27 | 部分唯一索引【对照组】第二条带 `supersedes_id` | 通过 | ✅ 通过 | **这条证明 `WHERE` 生效** —— 退化时会 23505 |
| 28 | OP-151【对照组】`step_no=1` / `BRIEF` 的批准 | 通过 | ✅ 通过 | 同表通用 |
| 29 | `step_no` 越界（9 > `STEP_MAX`=8） | 23514 | ✅ 23514 | |
| 30 | 跨包回归【对照组】`BRIEF`/`PROMPT`/`KNOWLEDGE`/`CONTENT` 节点注册 | 通过 | ✅ 通过 | 证明 `WHEN` 收窄**不误伤别包** |

**合计 30 条，PASS 30，FAIL 0。**

### 9.1 这张表里最不能删的两条

- **#27**：它是**唯一**能区分"部分唯一索引"与"普通唯一索引"的场景。
  退化时它从"通过"变成 `23505`。删掉它，第 1 层防线就完全没有行为证据。
- **#30**：`version_nodes` 上现在挂着**四个**包（WP18/WP20/WP22/WP23）
  各自 `WHEN` 收窄的约束触发器。若本包的 `WHEN` 写错或漏写，
  轻则性能、**重则误拦别包的版本注册**。这条是跨包回归的哨兵。

### 9.2 ⚠️ **诚实缺口：判据 ②③ 尚未被真正证明（必须在本包的测试里补正）**

场景 2 与 3 虽然断言 `23514` 并 PASS，**但实际报错消息来自
`trg_production_baselines_approval_required`**（"…在提交时没有同事务的批准…"），
**而不是**被测的 `trg_production_baselines_node_consistency`。

成因：**三条延迟触发器同挂 `production_baselines`**，同一行 `INSERT` 时三条都会执行，
**按触发器名字典序** `approval_required` < `no_blocking` < `node_consistency`。
场景 2/3 没插批准，于是 `approval_required` **抢先**抛错，`node_consistency` 根本没跑到。
两个 sqlstate 都是 `23514`，只断言 sqlstate 的判据**区分不了这两者**。

**因此本文件不能声称"判据 ②③ 已验证"。** 补正方式（写进
`tests/test_baseline_wp22.py`，属 Task #11）：

```
1. 先插一条合法的 APPROVED 批准（让 approval_required 与 no_blocking 都满足）；
2. 再构造 entity_type 错 / entity_id 错的场景；
3. 断言时**不只看 sqlstate，还看 pg_exception 的 message_primary**
   含 node_consistency 的专属措辞（"不是 BASELINE" / "不一致"）。
```

**这条缺口本身值得留在文件里**：它记录的正是本项目最高频的坑 ——
"一个碰巧同码的别的防线替被测防线报错，于是判据假通过"（ISS-77/82 同族）。
写成 sqlstate-only 的断言，永远不会自己暴露这件事。
"""


# ---------------------------------------------------------------------------
# 十、统计（供收口核对）
# ---------------------------------------------------------------------------

COUNTS = """
| 项 | 数量 |
|---|---|
| 表 | 4 |
| 列（合计） | 47 |
| PRIMARY KEY | 4 |
| CHECK 约束 | 31 |
| UNIQUE 约束 | 3 |
| FOREIGN KEY | 14 |
| 索引行（`pg_indexes` 全部，含 PK/UQ 隐式索引） | 20 |
| 索引（显式 `CREATE INDEX`） | 13 |
| **其中部分索引（带 `WHERE`）** | **2** |
| 触发器（本包建的） | 9 |
| 触发器（其中挂在非本模块表上） | 1（`trg_version_nodes_baseline_paired`） |
| 本包新增函数 | 6 |
| 复用的共用函数 | 1（`rednote_reject_mutation`） |

**列数明细**：`information_drafts` 13 + `production_baselines` 10 +
`baseline_items` 13 + `step_approvals` 11 = **47**。

**CHECK 明细**：drafts 10 + baselines 5 + items 11 + approvals 5 = **31**。
（`pg_constraint` 里另有 3 条 `contype='t'` 的记录 —— 那是**约束触发器**，
不是 CHECK，统计时不要混进来。）

**UNIQUE 明细**：drafts 1（`project_id`）+ items 2（`baseline_item_no` /
`baseline_asset`）= **3**。**`production_baselines` 与 `step_approvals` 的
UNIQUE 数为 0** —— 它们的"唯一性"由**部分唯一索引**承担，
而**部分唯一索引在 `pg_constraint` 里不出现**（见 §八 的 ⚠️）。

**FK 明细**：drafts 2 / baselines 5 / items 3 / approvals 4 = **14**。
其中 **6 条是 `DEFERRABLE INITIALLY DEFERRED`**：
drafts 1（`brief_version_id`）/ baselines 3（`id` / `brief_version_id` /
`supersedes_id`）/ approvals 2（`subject_version_id` / `supersedes_id`）。
**`baseline_items` 的 3 条全部是立即检查** —— 它的父行（`production_baselines`）
与两条知识引用行都由**同一事务的前序步骤**写入，不存在需要推迟的顺序问题。
其余 8 条指向既有主数据表（`projects` / `users` / `knowledge_assets` /
`knowledge_versions`），均为立即检查 + `ON DELETE RESTRICT`。

**索引明细（显式 13 条）**：drafts 2 / baselines 4 / items 3 / approvals 4。
其中**两条是部分索引**（带 `WHERE`），是 autogenerate 最容易漏 `WHERE` 的：
`uq_production_baselines_project_root`、`uq_step_approvals_root`。
**本机实测两条的 `WHERE` 都在位**（原文见 `pg_indexes.indexdef`）。

**触发器明细（9 = 3 + 6）**：3 个不可变（三条 DROP/UPDATE 保护，
`deferrable=False`，复用共用函数）+ 6 个约束触发器（`deferrable=True,
initdeferred=True`，本包自己的函数）。
6 个之中挂在**非本模块表**上的有 1 个（`version_nodes`），
挂在 `production_baselines` 上的有 3 个（`node_consistency` /
`approval_required` / `no_blocking`），挂在 `baseline_items` 上的有 2 个。
"""


if __name__ == "__main__":  # pragma: no cover
    # 本文件是**规格**，不是可执行代码。加这个 main 只是为了让
    # `python docs/m2/WP22-ddl-草案.py` 不报错并打印各节标题，
    # 方便主 agent 在终端里翻。
    for name, text in (
        ("INFORMATION_DRAFTS_DDL", INFORMATION_DRAFTS_DDL),
        ("PRODUCTION_BASELINES_DDL", PRODUCTION_BASELINES_DDL),
        ("BASELINE_ITEMS_DDL", BASELINE_ITEMS_DDL),
        ("STEP_APPROVALS_DDL", STEP_APPROVALS_DDL),
        ("DELIBERATE_DEVIATIONS", DELIBERATE_DEVIATIONS),
        ("TRIGGERS", TRIGGERS),
        ("DOWNGRADE_RULES", DOWNGRADE_RULES),
        ("VERIFICATION_COMMANDS", VERIFICATION_COMMANDS),
        ("LIVE_VERIFICATION", LIVE_VERIFICATION),
        ("COUNTS", COUNTS),
    ):
        print(f"===== {name} =====")
