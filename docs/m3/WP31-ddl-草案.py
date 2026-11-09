"""WP31 DDL 草案：`trial_rounds` / `trial_round_slots` / `trial_gate_decisions`。

**这不是迁移文件。** 迁移由主 agent 串行收口（接缝冻结 §4.2）。
本文件是**规格**：表名、列、类型、可空、默认值、全部约束与索引、外键、
触发器清单，逐条可核对。

**为什么用 `.py` 而不是 `.md`**：草案里的每一列 / 每个约束都将对应
`modules/planning/trial/models.py` 的一行。用 Python 写，读者可以把两边并排比对；
写成散文则必须来回翻。**本文件不 import 本包任何模块** ——
它要能在"包还没装好"的环境里被打开阅读，而 import 会让它连带失败。
（与 `WP30-ddl-草案.py` / `WP29-ddl-草案.py` 同款。）

⚠️ **验证状态（如实登记，不假装已核对）**：
本草案是**设计阶段产物**。写本文件时 `rednote_test_wp31` 库**不存在**、
`modules/planning/trial/` 下只有空骨架（`__init__.py`），因此：
  · SQL 文本**没有**在真库上执行过、`pg_constraint` / `pg_indexes` / `pg_trigger`
    **没有**导出核对过（WP30 草案抬头那段"已由验证库实测导出核对"在本文件**不成立**）；
  · 约束名长度（≤ 63 字节）已用脚本对本文件文本逐个量过，但 PostgreSQL 实际
    生成的 FK/UQ/IX 自动名未经库端确认；
  · 实现阶段第一件事：在 `rednote_test_wp31` 上 `create_all` 后导出对账，
    并把本段改写成与 WP30 同款的"已核对"声明。

---

## 一、文件怎么用

1. 主 agent 用 `models.py` 生成迁移（`alembic revision --autogenerate`），
   然后用本文件**逐条核对**生成的 DDL（G2 三件事照常）。
   迁移的 `down_revision` 取**收口时的当前 head**（写本文件时磁盘上最新是
   `d6074ae4bc53`，WP36；以收口时 `grep -E '^(revision|down_revision)'` 为准）。
2. 迁移必须调 `trial/ddl.py::all_trigger_ddl()` 把**六个**触发器一并建出
   （一个不可变 + 五个业务触发器，见第五节）。
3. **升级与降级都必须只碰本包自己的对象**：降级只删 `OWN_FUNCTIONS` 列出的
   **五个**函数，**不含**跨包共用的 `rednote_reject_mutation`（ISS-79）。
4. **取值域全部写成字面量**（ISS-191）。迁移里**不得**
   `from rednote.modules.planning.trial.types import ...` 再循环它 ——
   那会让 `check_migration_model_parity.py` 恒绿。本包一次引入 **3 个**新取值域
   （`trial_rounds.kind` / `trial_rounds.state` / `trial_rounds.limit_source`），
   汇总见第七节。`limit_source` 与 `planning.types.SETTING_ORIGINS` 同值但
   **是第二处声称**，必须在迁移里写字面量，由 parity 判据反向核对。
5. ⚠️ **状态迁移矩阵（第五节 `TRANSITIONS`）同时存在于三处**：
   `trial/types.py` 的常量表（服务层 409 的依据）、触发器函数体（数据库最后一道）、
   本文件。三处必须逐项一致 —— 实现阶段要有一条判据把触发器函数体里的矩阵
   与 `types.py` 对账（否则就是"两处各写一遍，改一处不报错"）。
6. ⚠️ **逻辑里没有任何业务数字**：试产条数上限（ADR-13 的 10）**不在**本文件。
   `trial_rounds.limit_applied` 只是**创建那一刻生效值的快照**，
   `ck_trial_rounds_requested_within_limit_applied` 比的是两列之间的关系，
   不是 `<= 10`。是否接受这条"列间比较"见说明文档开放问题 Q14。

## 二、三张表的角色与可变性

| 表 | 角色 | 可变性 | 触发器 |
|---|---|---|---|
| `trial_rounds` | 一轮试产 / 补充试产（状态机宿主）。**只有 `state` / `cancel_reason` / `updated_at` / `lock_version` 可变** | **可变**（`lock_version` 是 ETag 载体，DELETE 被拒） | `trg_trial_rounds_transition_guard`（BEFORE，状态矩阵 + 列不可变）+ `trg_trial_rounds_consistency`（约束触发器，延迟） |
| `trial_round_slots` | 本轮的一个名额（场景 × 可选形式 + 绑定的内容 + 替代关系）。**只有 `content_item_id` / `bound_at` 从 NULL 写一次**，其余列不可变 | **有限可变**（write-once），DELETE 被拒 | `trg_trial_round_slots_guard`（BEFORE）+ `trg_trial_round_slots_consistency`（约束触发器，延迟） |
| `trial_gate_decisions` | 一次"批准进入扩产"的人工决定 + 当时的条件快照 / 统计 / 依据。每轮至多一条 | **不可变** | `trg_trial_gate_decisions_immutable` + `trg_trial_gate_decisions_consistency`（约束触发器，延迟） |

**为什么 `trial_round_slots` 不是不可变表**：绑定内容发生在名额创建之后
（先按 PLAN 切出名额，再去生成内容），所以必须有一次 `NULL → 值` 的写。
把"绑定"拆成第四张不可变表可以保住纯追加，但会引入
"名额表与绑定表行数一致"这类没有需求支撑的不变量（与 `plan_drafts` 不拆
子表同一理由）。代价由 `trg_trial_round_slots_guard` 兜住：绑定后的任何改动、
任何 DELETE 一律拒绝。

**为什么试产条目的"处置结论"没有列**：通过 / 淘汰是 WP29 `content_reviews`
的事实（含改判链 `supersedes_id`，且绑定 `subject_version_id`）。本包若再存一列
`disposition` 就是**第二个真值来源**，两者分叉不会报错。处置状态是**读时派生**的
（第六节），本包只存"名额—内容"的绑定关系与"替代"关系。

**为什么 `trial_gate_decisions` 与 `step_approvals` 并存而不合并**：
`step_approvals.subject_kind` 受 CHECK 限定为 `BRIEF` / `RESEARCH` / `BASELINE`
（`baseline_types.SUBJECT_KINDS`），要记"试产轮已批准"必须改 baseline 的
CHECK 与 `_SUBJECT_KIND_ENTITY_TYPE` —— 接缝冻结 §2.1 规定"必须改 baseline 时
停下来报告主 agent"，故本草案**不绕**，见说明文档开放问题 Q1。

## 三、建表顺序与拆环

```
projects / users / version_nodes / production_baselines / content_items
     ↑        ↑            ↑                 ↑                  ↑
     └────────┴──── trial_rounds ────────────┘                  │
                        ↑   ↑(source_round_id 自引用)            │
                        │                                        │
                  trial_round_slots ─────────────────────────────┘
                        ↑   (replaces_slot_id 自引用)
                        │
                  trial_gate_decisions (round_id)
```

**建表顺序：`trial_rounds` → `trial_round_slots` → `trial_gate_decisions`。**

两处自引用（`trial_rounds.source_round_id`、`trial_round_slots.replaces_slot_id`）
都指向**更早插入**的行，因此**不需要延迟外键**。本包**没有表级环**。
⚠️ 若 autogenerate 生成了 `DEFERRABLE` 的 FK，那是生成器抄了别的包。

三张表的外键**全部指向已存在的表**（`projects` / `users` / `version_nodes` /
`production_baselines` / `content_items`），**没有任何别的包的表指向本包**
（无跨包"入向 FK"）。但本包有**指向 `content_items` 的出向 FK**
（`trial_round_slots.content_item_id`），它与 WP27/WP29/WP35 同形，
会被 `test_content_wp26.py::_KNOWN_CROSS_PACKAGE_FKS` 判据当作"未登记的跨包 FK"
——见第六节。

## 四、三张表的逐列 DDL（specs，不是可执行代码）

### 4.1 `trial_rounds`

**状态与时间 / 取消理由的配对**：`cancel_reason` 与 `state = 'CANCELLED'`
写成**双向等值**（取消必须留理由；没取消不得带理由）。

**为什么 `planned_quantity` 与 `requested_quantity` 分两列**：
分配可能"分不完"（`allocation.py` 第四节：`unallocated > 0` 是合法结果，
不硬塞，C14）。运营要的是 `requested_quantity`，实际切出的名额数是
`planned_quantity`；差值 = `requested - planned` 即未分配数。
**不加 `unallocated_count` 冗余列**（本仓纪律：每个投影列都要配触发器钉住，
否则是第二处声称）。`planned_quantity = 0`（一个名额都切不出）不是合法的一轮，
由 `ck_trial_rounds_planned_quantity_positive` 拒绝。

**为什么不加 `(project_id, kind)` 或 `(project_id, plan_version_id)` 唯一约束**：
同一项目允许多轮试产（含基于同一 PLAN 版本的补充试产）。
"同一时刻至多一轮在进行"由**部分唯一索引** `uq_trial_rounds_project_open` 表达
（是否要这条规则见说明文档开放问题 Q9；去掉只需删这一个索引，不牵动别处）。
"当前轮"因此定义为**未终态的那一轮**，不是 `ORDER BY round_no DESC` 的末轮
（与接缝冻结 / 契约 §2 的取数纪律同向）。

**幂等**：`idempotency_key` + `request_hash` 成对出现或同时为空；
`(project_id, idempotency_key)` 部分唯一（`WHERE idempotency_key IS NOT NULL`）。
同键同 hash → 命中既有一轮；同键不同 hash → 409 `IDEMPOTENCY_CONFLICT`
（判断在服务层，数据库只保证"同键只有一行"）。
"""

TRIAL_ROUNDS_DDL = """
-- 表名：trial_rounds。一轮试产 / 补充试产。**可变**（lock_version 是 ETag 载体）。
--
-- 约束命名遵循 platform/base.py 的 NAMING_CONVENTION：
--   CheckConstraint(name="state_valid") → ck_trial_rounds_state_valid
--   ForeignKey 自动命名为 fk_<table>_<column_0_N_name>_<referred_table>
--
-- ⚠️ 本表最长的约束名是
--    ck_trial_rounds_cancel_reason_paired_with_cancelled（51 字节）与
--    fk_trial_rounds_baseline_id_production_baselines（48 字节），
--    都在 PostgreSQL 的 63 字节上限内（脚本实量）。超限会被**静默截断**并追加
--    哈希，导致 ORM 元数据里的名字与 pg_constraint 里的实际名字永久不一致。
--
-- ★ plan_snapshot 为什么必须存（本包设计阶段的一处实测发现）：
--   WP24 的 submit_draft 调 register_version(payload=...) 时，version_nodes 只落
--   payload_hash，**不落 payload 正文**（versions/models.py：注册表"不是万能 JSON 仓库"）；
--   分配结果（scenes / forms 的 allocated）因此在库里**没有第二个落点**，
--   唯一的来源是 plan_drafts 那一行**可变**草稿（一项目一份，且 save_draft 对 SUBMITTED 草稿不设防）。
--   ⇒ 『读 PLAN 版本的分配结果』（WP24 说明 §9 对 WP31 的接缝承诺）只能经草稿重算。
--   处置：创建本轮时，服务层重算草稿的 plan_payload，**与 plan_version_id 对应节点的
--   payload_hash 比对**（canonical_payload_hash），一致才放行并把这份 payload 冻结进本列；
--   不一致 ⇒ 409 PLAN_DRAFT_DRIFTED（fail-closed）。此后补充轮 / 补产的容量与权重一律读本列，
--   不再回头读会漂移的草稿。『payload_hash 与本列一致』无法写成 CHECK（规范化 JSON 的 sha256
--   在 SQL 里复刻等于第二份实现），由服务层 + 测试钉住（说明文档 §10 用例 U-12）。
CREATE TABLE trial_rounds (
    id                 uuid         NOT NULL DEFAULT gen_random_uuid(),
    project_id         uuid         NOT NULL,
    round_no           integer      NOT NULL,   -- 项目内序号，从 1 起；由服务层在项目级 advisory lock 下取号
    kind               varchar(12)  NOT NULL,   -- SUPPLEMENT / TRIAL
    state              varchar(16)  NOT NULL DEFAULT 'PLANNED',
    plan_version_id    uuid         NOT NULL,   -- 本轮钉住的 PLAN 版本（version_nodes.id）；**显式钉住，不取"最新"**
    baseline_id        uuid         NOT NULL,   -- 创建那一刻的 projects.active_baseline_id（投影），不是"最新创建的基线"
    source_round_id    uuid,                    -- 仅 SUPPLEMENT：补充的是哪一轮（须为 GATE_PASSED 的 TRIAL）
    requested_quantity integer      NOT NULL,   -- 运营要的条数（已过 limits.validate_quantity）
    planned_quantity   integer      NOT NULL,   -- 实际切出的名额数（= 分配结果之和；< requested 即"分不完"）
    limit_applied      integer      NOT NULL,   -- 创建时 TRIAL 档生效上限的**快照**（审计用，不是配置源）
    limit_source       varchar(12)  NOT NULL,   -- GLOBAL / INHERITED / MANUAL（EffectiveSetting.source 的快照）
    plan_snapshot      jsonb        NOT NULL,   -- 钉住那一刻的 PLAN payload（planning.service.plan_payload 的形状）；见下方说明
    cancel_reason      varchar(500),            -- 仅 CANCELLED 时非空
    idempotency_key    varchar(160),
    request_hash       varchar(64),             -- canonical_payload_hash（sha256 十六进制）
    created_by         uuid,
    created_at         timestamptz  NOT NULL DEFAULT now(),
    updated_at         timestamptz  NOT NULL DEFAULT now(),
    lock_version       bigint       NOT NULL DEFAULT 1,

    CONSTRAINT pk_trial_rounds PRIMARY KEY (id),

    -- --- 取值域（与 trial/types.py 的 frozenset 逐项一致，**字面量化石**） --
    CONSTRAINT ck_trial_rounds_kind_valid
        CHECK (kind IN ('SUPPLEMENT', 'TRIAL')),
    CONSTRAINT ck_trial_rounds_state_valid
        CHECK (state IN ('CANCELLED', 'GATE_PASSED', 'GENERATING', 'PLANNED', 'REVIEWING')),
    CONSTRAINT ck_trial_rounds_limit_source_valid
        CHECK (limit_source IN ('GLOBAL', 'INHERITED', 'MANUAL')),

    -- --- 数量：下界 + 防御性上界（与 plan_drafts 的 total_quantity_within_bound 同值同因） --
    CONSTRAINT ck_trial_rounds_round_no_positive
        CHECK (round_no >= 1),
    CONSTRAINT ck_trial_rounds_requested_quantity_positive
        CHECK (requested_quantity >= 1),
    CONSTRAINT ck_trial_rounds_planned_quantity_positive
        CHECK (planned_quantity >= 1),
    CONSTRAINT ck_trial_rounds_planned_not_above_requested
        CHECK (planned_quantity <= requested_quantity),
    CONSTRAINT ck_trial_rounds_requested_within_bound
        CHECK (requested_quantity <= 100000),   -- = planning.types.MAX_QUANTITY_BOUND（防脏数据，非业务上限）
    CONSTRAINT ck_trial_rounds_limit_applied_positive
        CHECK (limit_applied >= 1),
    -- 见说明文档 Q14：列间比较，不含任何业务数字。
    CONSTRAINT ck_trial_rounds_requested_within_limit_applied
        CHECK (requested_quantity <= limit_applied),

    -- --- 方案快照必须是 JSON 对象 -------------------------------------------
    CONSTRAINT ck_trial_rounds_plan_snapshot_is_object
        CHECK (jsonb_typeof(plan_snapshot) = 'object'),

    -- --- 补充轮必须指明补的是哪一轮；普通试产不得带 -----------------------
    CONSTRAINT ck_trial_rounds_supplement_has_source
        CHECK ((kind = 'SUPPLEMENT') = (source_round_id IS NOT NULL)),
    CONSTRAINT ck_trial_rounds_source_not_self
        CHECK (source_round_id IS NULL OR source_round_id <> id),

    -- --- 取消必须留理由（双向等值） ---------------------------------------
    CONSTRAINT ck_trial_rounds_cancel_reason_paired_with_cancelled
        CHECK ((state = 'CANCELLED') = (cancel_reason IS NOT NULL)),
    CONSTRAINT ck_trial_rounds_cancel_reason_not_blank
        CHECK (cancel_reason IS NULL OR char_length(btrim(cancel_reason)) > 0),

    -- --- 幂等键与请求摘要成对 ---------------------------------------------
    CONSTRAINT ck_trial_rounds_idempotency_paired
        CHECK ((idempotency_key IS NULL) = (request_hash IS NULL)),
    CONSTRAINT ck_trial_rounds_idempotency_key_not_blank
        CHECK (idempotency_key IS NULL OR char_length(btrim(idempotency_key)) > 0),
    CONSTRAINT ck_trial_rounds_request_hash_length
        CHECK (request_hash IS NULL OR char_length(request_hash) = 64),

    -- --- 项目内序号唯一（并发取号撞车时的最后一道；唯一约束自带索引） ------
    CONSTRAINT uq_trial_rounds_project_id_round_no UNIQUE (project_id, round_no),

    -- --- 外键：全部 ON DELETE RESTRICT（本包无级联删除） -------------------
    CONSTRAINT fk_trial_rounds_project_id_projects
        FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_trial_rounds_plan_version_id_version_nodes
        FOREIGN KEY (plan_version_id) REFERENCES version_nodes (id) ON DELETE RESTRICT,
    CONSTRAINT fk_trial_rounds_baseline_id_production_baselines
        FOREIGN KEY (baseline_id) REFERENCES production_baselines (id) ON DELETE RESTRICT,
    CONSTRAINT fk_trial_rounds_source_round_id_trial_rounds
        FOREIGN KEY (source_round_id) REFERENCES trial_rounds (id) ON DELETE RESTRICT,
    CONSTRAINT fk_trial_rounds_created_by_users
        FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT
);

-- ★ 同一项目同一时刻至多一轮"未终态"（见说明文档 Q9）。
--   WHERE 子句是判据的一部分：漏写会退化成"一个项目只能有一轮"，
--   而那是**静默**的（第二轮创建时才发现）。取值列表按字母序排（与 CHECK 同源口径）。
CREATE UNIQUE INDEX uq_trial_rounds_project_open
    ON trial_rounds (project_id)
    WHERE state IN ('GENERATING', 'PLANNED', 'REVIEWING');

-- ★ 幂等：同一项目下同一个 Idempotency-Key 只有一行。
CREATE UNIQUE INDEX uq_trial_rounds_project_idempotency
    ON trial_rounds (project_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- 补充轮回指源轮（"这一轮补的是哪一轮"的反向查询）。
CREATE INDEX ix_trial_rounds_source_round
    ON trial_rounds (source_round_id);
"""

TRIAL_ROUND_SLOTS_DDL = """
-- 表名：trial_round_slots。本轮的一个名额。**有限可变**（content_item_id / bound_at 只能 NULL → 值 一次）。
--
-- 名额的"生死"是**读时派生**的，不存列：
--   · 已被替代  ⇔ 存在别的名额 replaces_slot_id = 本行 id
--   · 在役(live) ⇔ 未被替代 AND 所在轮 state <> 'CANCELLED'
--   · 处置状态   ⇔ 由 content_reviews 在"内容当前版本"上的有效结论派生（说明文档 §6）
-- 不存 disposition：它是 WP29 content_reviews 的事实，再存一列就是第二个真值来源。
--
-- 并发：本表的写入全部发生在"已锁住所属 trial_rounds 行（FOR UPDATE）"的事务里，
-- 因此**不设 lock_version**（没有独立的乐观锁载体；ETag 用所属轮的 lock_version）。
CREATE TABLE trial_round_slots (
    id               uuid         NOT NULL DEFAULT gen_random_uuid(),
    round_id         uuid         NOT NULL,
    slot_no          integer      NOT NULL,   -- 轮内序号，从 1 起；替代名额取新号，不复用
    scene_key        varchar(200) NOT NULL,   -- 方向 = PLAN payload 里 scenes[].key（见说明文档 Q4）
    form_key         varchar(200),            -- 形式 key；NULL = 尚未指派（见说明文档 Q5，**不是**空串）
    content_item_id  uuid,                    -- NULL = 还没出活（**不是**"0 条"）；write-once
    bound_at         timestamptz,             -- 与 content_item_id 同生同灭；write-once
    replaces_slot_id uuid,                    -- 淘汰补产：本名额替代了哪个名额
    created_at       timestamptz  NOT NULL DEFAULT now(),
    updated_at       timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT pk_trial_round_slots PRIMARY KEY (id),

    CONSTRAINT ck_trial_round_slots_slot_no_positive
        CHECK (slot_no >= 1),
    CONSTRAINT ck_trial_round_slots_scene_key_not_blank
        CHECK (char_length(btrim(scene_key)) > 0),
    CONSTRAINT ck_trial_round_slots_form_key_not_blank
        CHECK (form_key IS NULL OR char_length(btrim(form_key)) > 0),
    CONSTRAINT ck_trial_round_slots_replaces_not_self
        CHECK (replaces_slot_id IS NULL OR replaces_slot_id <> id),
    -- 绑定与绑定时刻同生同灭（双向等值）。
    CONSTRAINT ck_trial_round_slots_bound_at_paired_with_content
        CHECK ((content_item_id IS NULL) = (bound_at IS NULL)),

    -- 轮内序号唯一。
    CONSTRAINT uq_trial_round_slots_round_id_slot_no UNIQUE (round_id, slot_no),
    -- 一条内容全局至多属于一个名额（并发把同一内容绑到两个名额时的最后一道）。
    -- 唯一约束允许多个 NULL，所以"未绑定"的名额不互相冲突。
    CONSTRAINT uq_trial_round_slots_content_item_id UNIQUE (content_item_id),
    -- 一个名额至多被替代一次（并发双击"补产"时的最后一道）。
    CONSTRAINT uq_trial_round_slots_replaces_slot_id UNIQUE (replaces_slot_id),

    CONSTRAINT fk_trial_round_slots_round_id_trial_rounds
        FOREIGN KEY (round_id) REFERENCES trial_rounds (id) ON DELETE RESTRICT,
    CONSTRAINT fk_trial_round_slots_content_item_id_content_items
        FOREIGN KEY (content_item_id) REFERENCES content_items (id) ON DELETE RESTRICT,
    CONSTRAINT fk_trial_round_slots_replaces_slot_id_trial_round_slots
        FOREIGN KEY (replaces_slot_id) REFERENCES trial_round_slots (id) ON DELETE RESTRICT
);

-- "本轮各方向有几个名额"（门槛判定与统计的读路径）。
CREATE INDEX ix_trial_round_slots_round_scene
    ON trial_round_slots (round_id, scene_key);
"""

TRIAL_GATE_DECISIONS_DDL = """
-- 表名：trial_gate_decisions。一次"批准进入扩产"的人工决定。**不可变**；每轮至多一条。
--
-- 为什么存 condition_results / statistics / basis 三份快照：
--   · 关口条件是**读时派生**的（会随审核、反馈、改稿而变）；决定那一刻的判定
--     必须冻结，否则"当时凭什么放行"事后不可回答。
--   · basis 逐名额记录 (slotId, contentItemId, subjectVersionId, reviewId)：
--     之后任何一方改动，读端据此判 STALE（与 WP29 anchorState / WP30 STALE 同款手法）。
--   · statistics 里 NULL / 0 / 约数三者分离（说明文档 §8）：
--     分母为 0 的比率写 JSON null，**不写 0**。
--
-- 本表**没有** project_id 冗余列：它能经 round_id 一跳取得；
-- 冗余列就要配触发器钉住（本仓纪律），买不起。
CREATE TABLE trial_gate_decisions (
    id                 uuid         NOT NULL DEFAULT gen_random_uuid(),
    round_id           uuid         NOT NULL,
    approved_scene_keys jsonb       NOT NULL,   -- 批准进入扩产的方向（scene_key 数组，非空）
    condition_results  jsonb        NOT NULL,   -- {条件码: {satisfied, blocking, detail}}
    statistics         jsonb        NOT NULL,   -- 计数 + 比率（比率分母为 0 ⇒ null）
    basis              jsonb        NOT NULL,   -- 逐名额依据数组
    reason             varchar(500) NOT NULL,   -- 决定理由（人填）
    decided_by         uuid,
    decided_at         timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT pk_trial_gate_decisions PRIMARY KEY (id),

    CONSTRAINT ck_trial_gate_decisions_approved_scene_keys_is_array
        CHECK (jsonb_typeof(approved_scene_keys) = 'array'),
    -- 批准 0 个方向的"关口通过"没有意义（那应当是不通过）。
    -- 用 CASE 而不是 AND：AND 不保证短路，标量上调 jsonb_array_length 会抛 22023
    -- 而不是让 CHECK 返回 false（sqlstate 就不是 23514 了）。
    CONSTRAINT ck_trial_gate_decisions_approved_scene_keys_not_empty
        CHECK (CASE WHEN jsonb_typeof(approved_scene_keys) = 'array'
                    THEN jsonb_array_length(approved_scene_keys) >= 1
                    ELSE false END),
    CONSTRAINT ck_trial_gate_decisions_condition_results_is_object
        CHECK (jsonb_typeof(condition_results) = 'object'),
    CONSTRAINT ck_trial_gate_decisions_statistics_is_object
        CHECK (jsonb_typeof(statistics) = 'object'),
    CONSTRAINT ck_trial_gate_decisions_basis_is_array
        CHECK (jsonb_typeof(basis) = 'array'),
    CONSTRAINT ck_trial_gate_decisions_reason_not_blank
        CHECK (char_length(btrim(reason)) > 0),

    -- 每轮至多一条决定（唯一约束自带索引）。
    CONSTRAINT uq_trial_gate_decisions_round_id UNIQUE (round_id),

    CONSTRAINT fk_trial_gate_decisions_round_id_trial_rounds
        FOREIGN KEY (round_id) REFERENCES trial_rounds (id) ON DELETE RESTRICT,
    CONSTRAINT fk_trial_gate_decisions_decided_by_users
        FOREIGN KEY (decided_by) REFERENCES users (id) ON DELETE RESTRICT
);
"""

# ---------------------------------------------------------------------------
# 五、状态迁移矩阵（trial_rounds.state）与触发器清单（六个）
# ---------------------------------------------------------------------------

#: 合法迁移（from → 允许的 to 集合）。**不在表内的一律非法**。
#: 同态"迁移"（OLD.state = NEW.state，例如只 +1 lock_version）不是迁移，放行。
#: 终态（GATE_PASSED / CANCELLED）无出边。
TRANSITIONS = {
    "PLANNED": ("CANCELLED", "GENERATING"),
    "GENERATING": ("CANCELLED", "REVIEWING"),
    "REVIEWING": ("CANCELLED", "GATE_PASSED", "GENERATING"),
    "GATE_PASSED": (),
    "CANCELLED": (),
}

TRANSITION_NOTES = {
    "PLANNED→GENERATING": "开工：重判入口关口（步骤 1~3 批准 + 基线仍为钉住的那条）",
    "GENERATING→REVIEWING": "出活完毕：在役名额全部已绑定内容，且内容状态已落定（GENERATED / FAILED）",
    "REVIEWING→GENERATING": "淘汰补产：新增了尚未出活的替代名额（服务层在建替代名额的同一事务里推进）",
    "REVIEWING→GATE_PASSED": "只能经 trial_gate_decisions 的写入推进（约束触发器 + 唯一约束双保险）",
    "*→CANCELLED": "任意非终态可取消，必须留理由；已出的内容与已花的钱不回收、不删除",
}

TRIGGERS = (
    {
        "table": "trial_rounds",
        "name": "trg_trial_rounds_transition_guard",
        "timing": "BEFORE UPDATE OR DELETE, FOR EACH ROW",
        "function": "rednote_guard_trial_round_transition()",
        "sqlstate": "非法迁移 → 23514（check_violation）；改不可变列 / DELETE → 23001（restrict_violation）",
        "why": (
            "状态机的数据库最后一道。三条判据：① DELETE 一律拒（轮只取消、不删除）；"
            "② 除 state / cancel_reason / updated_at / lock_version 外的列不得变"
            "（project_id、round_no、kind、plan_version_id、baseline_id、source_round_id、"
            "requested/planned_quantity、limit_*、plan_snapshot、idempotency_*、created_*）；"
            "③ state 变化必须落在 TRANSITIONS 的边上。服务层先判并给出可读 409，"
            "这里是『绕过服务层直写 SQL』时的防线。"
        ),
    },
    {
        "table": "trial_rounds",
        "name": "trg_trial_rounds_consistency",
        "timing": (
            "AFTER INSERT OR UPDATE, FOR EACH ROW, CONSTRAINT TRIGGER "
            "DEFERRABLE INITIALLY DEFERRED"
        ),
        "function": "rednote_check_trial_round_consistency()",
        "sqlstate": "23514",
        "why": (
            "两组判据。INSERT：plan_version_id 指向的节点必须是 entity_type='PLAN' 且 "
            "entity_id=NEW.project_id 且 project_id=NEW.project_id；baseline_id 指向的基线"
            "必须属于 NEW.project_id；source_round_id（若有）必须属于同一项目且 kind='TRIAL'。"
            "FK 只保证被引用行存在、不管它属于谁 —— 钉错的 PLAN / 基线会让『这轮用的是哪份方案』"
            "有两个答案。INSERT OR UPDATE：state='GATE_PASSED' 的轮必须存在 trial_gate_decisions 行"
            "（延迟到提交时判，使『写决定』与『改状态』可以同事务任意顺序）。"
            "这条把『非法推进被拒』（AC-08~09 的验收语）落到数据库：没有人工决定就到不了 GATE_PASSED。"
        ),
    },
    {
        "table": "trial_round_slots",
        "name": "trg_trial_round_slots_guard",
        "timing": "BEFORE UPDATE OR DELETE, FOR EACH ROW",
        "function": "rednote_guard_trial_round_slot()",
        "sqlstate": "23001（restrict_violation）",
        "why": (
            "名额 write-once：DELETE 一律拒；UPDATE 只允许 content_item_id / bound_at 从 NULL 写成值"
            "（及 updated_at）；其余列、以及已非 NULL 的 content_item_id / bound_at 任何改动都拒。"
            "绑定改不了，『这个名额对应哪条内容』才不会在审核之后悄悄换人。"
        ),
    },
    {
        "table": "trial_round_slots",
        "name": "trg_trial_round_slots_consistency",
        "timing": (
            "AFTER INSERT OR UPDATE, FOR EACH ROW, CONSTRAINT TRIGGER "
            "DEFERRABLE INITIALLY DEFERRED"
        ),
        "function": "rednote_check_trial_round_slot_consistency()",
        "sqlstate": "23514",
        "why": (
            "三条判据，都是 FK 表达不了的『跨表归属』：① content_item_id（若有）所属 content_items.project_id "
            "必须等于本名额所属轮的 project_id（否则别的项目的内容可混进本轮统计）；"
            "② replaces_slot_id（若有）必须在同一轮、且 scene_key 相同（替代不改方向）；"
            "③ INSERT 时所属轮不得处于终态（GATE_PASSED / CANCELLED 之后不得再加名额）。"
        ),
    },
    {
        "table": "trial_gate_decisions",
        "name": "trg_trial_gate_decisions_immutable",
        "timing": "BEFORE UPDATE OR DELETE, FOR EACH ROW",
        "function": "rednote_reject_mutation()",  # 跨包共用，downgrade 不删
        "sqlstate": "23001",
        "why": (
            "决定是『那一刻凭什么放行』的证据，落库即冻结。要推翻不是改这一行，"
            "而是（若业务需要，见说明文档 Q12）追加新一轮。"
        ),
    },
    {
        "table": "trial_gate_decisions",
        "name": "trg_trial_gate_decisions_consistency",
        "timing": (
            "AFTER INSERT, FOR EACH ROW, CONSTRAINT TRIGGER "
            "DEFERRABLE INITIALLY DEFERRED"
        ),
        "function": "rednote_check_trial_gate_decision_consistency()",
        "sqlstate": "23514",
        "why": (
            "反向那一半：存在决定行 ⇒ 所属轮 state 必须是 'GATE_PASSED'。"
            "与 trg_trial_rounds_consistency 合成双向：『到了 GATE_PASSED 必有决定』+"
            "『有决定必到了 GATE_PASSED』，缺一个方向都能造出半成品（决定在、状态没推进）。"
        ),
    },
)

#: 本包**自己的**函数（downgrade 只删这五个）。`rednote_reject_mutation`
#: 是跨包共用函数，**不在此列** —— 删它会让所有已建表的不可变守卫
#: 静默失效（ISS-79）。
OWN_FUNCTIONS = (
    "rednote_guard_trial_round_transition",
    "rednote_check_trial_round_consistency",
    "rednote_guard_trial_round_slot",
    "rednote_check_trial_round_slot_consistency",
    "rednote_check_trial_gate_decision_consistency",
)

# ---------------------------------------------------------------------------
# 六、与别包的耦合：出向 FK、测试夹具、以及"读时派生"所依赖的别人的列
# ---------------------------------------------------------------------------

CROSS_PACKAGE_COUPLING_NOTE = """
## 6.1 本包的跨包 FK 全部是"出向"（本包的表引用别人的表）

| FK | 指向 | 备注 |
|---|---|---|
| `fk_trial_round_slots_content_item_id_content_items` | WP26 `content_items` | 与 WP27/WP29/WP35 同形：会被 `test_content_wp26.py::_KNOWN_CROSS_PACKAGE_FKS` 判据当作『未登记的跨包 FK』并红灯 |
| `fk_trial_rounds_plan_version_id_version_nodes` | WP11 `version_nodes` | 同类，需核对 `test_versions_wp11.py` 是否有同类白名单 |
| `fk_trial_rounds_baseline_id_production_baselines` | WP22 `production_baselines` | 同类，需核对 `test_baseline_wp22.py` 是否有同类白名单 |

**没有任何别的包的表指向本包的表**，所以不会改变别包夹具的 `drop_own_tables` 保留语义；
但**反过来**，`content_items` 因此多了一条被引用边，会改变 WP26 夹具的
`truncate_targets` 闭包（把 `trial_round_slots` 拖进去，沿 `trial_round_slots.round_id`
继续扩散到 `trial_rounds` / `trial_gate_decisions`）。

**处置（沿用 WP27 / WP29 / WP30 的先例）**：
1. 实现阶段在 `rednote_test_wp31` 上实测 `drop_own_tables` 的 `(dropped, kept)` 返回，
   把结果写进说明文档，不凭推断；
2. 登记到 `test_content_wp26.py::_KNOWN_CROSS_PACKAGE_FKS`（**主 agent 动作**，别人包的测试文件，
   分支禁写），并同步 `docs/regression-register.md` 的 RG-130；
3. 本包测试里放一条判据钉住前提（`content_item_id` FK 存在），FK 若被去掉改成只存 uuid
   它会红并提醒同步修正文档。

## 6.2 "读时派生"所依赖的别人的表与列（列名即对方契约，写错当场 UndefinedColumn）

| 派生量 | 读的表.列 | 口径 |
|---|---|---|
| 内容的**当前版本** | `content_items.current_version_id` → 为 NULL 时 `content_versions` 按 `content_item_id` 取末版 | 契约 §2：**投影优先 + NULL 回落**，**禁**把 `ORDER BY version_no DESC` 当主路径 |
| 某名额的**有效审核结论** | `content_reviews`：`content_item_id`、`subject_version_id`、`decision`、`supersedes_id` | 取『未被任何行 supersede 的那条』（`NOT EXISTS` 子查询用**另一行**的 `supersedes_id`；MAJOR-4 教训：自比恒假）；且 `subject_version_id` 必须等于内容当前版本，否则视为未审 |
| 未处理的**阻断意见** | `content_feedback_items`：`content_item_id`、`severity='BLOCKING'`、`state NOT IN ('RESOLVED','REJECTED')` | 与 WP29 `decide_review` 的闸同口径（判据下推 SQL，不拉全量） |
| **自检**情况 | `self_check_runs`：`content_item_id`、`subject_version_id`、`outcome`、`created_at`、`id` | 『最新一轮』= `(content_item_id, created_at DESC, id DESC)`（WP30 草案原话）；轮次的 `subject_version_id` ≠ 当前版 ⇒ STALE ⇒ 视为未自检 |
| 步骤批准 | `step_approvals`：`project_id`、`step_no`、`subject_kind`、`subject_version_id`、`decision`、`supersedes_id` | 『当前生效』= 未被取代的那条（`find_effective_approval` 同口径，MAJOR-6：**不是链根**） |
| 基线 | `projects.active_baseline_id`（投影）、`production_baselines.brief_version_id` | 不取『最新创建的基线』 |
| PLAN 内容 | `version_nodes` + 具体 PLAN 版本的 payload（WP24 `plan_payload` 形状） | 本轮钉住的 `plan_version_id`，不取『最新 PLAN』 |
"""

# ---------------------------------------------------------------------------
# 七、新取值域清单（迁移里必须写字面量，ISS-191）
# ---------------------------------------------------------------------------

VALUE_DOMAINS = {
    "trial_rounds.kind": ("SUPPLEMENT", "TRIAL"),
    "trial_rounds.state": ("CANCELLED", "GATE_PASSED", "GENERATING", "PLANNED", "REVIEWING"),
    "trial_rounds.limit_source": ("GLOBAL", "INHERITED", "MANUAL"),
}

#: 下列取值域**刻意不落数据库 CHECK**（开集 / 读时派生 / 代码常量），
#: 迁移 docstring 必须写明理由，否则下一个人会"顺手补上"而把它冻成化石。
NOT_IN_DB_DOMAINS = {
    "slot 处置状态（UNBOUND / AWAITING_REVIEW / APPROVED / ELIMINATED / REPLACED）":
        "读时派生，不存列；存了就是 content_reviews 之外的第二个真值来源。",
    "关口条件码（ROUND_ALL_DISPOSED / NO_OPEN_BLOCKING_FEEDBACK / DIRECTION_HAS_APPROVED_SAMPLE / "
    "SELF_CHECK_COMPLETE）":
        "只出现在 condition_results 的 JSON 键里；条件会随产品口径增减，"
        "落 CHECK 后每加一条就要一次迁移。代偿：服务层 types.GATE_CONDITION_CODES 校验写入，"
        "未知码 422（不落库）。",
    "入口关口条件码（STEP1_APPROVED / STEP2_APPROVED / STEP3_APPROVED / ACTIVE_BASELINE / "
    "PLAN_BOUND_TO_ACTIVE_BASELINE / QUANTITY_WITHIN_LIMIT）":
        "只在读接口响应里出现，不落库。",
    "details.reason 码（ROUND_TRANSITION_ILLEGAL 等）":
        "机器可判的错误原因，属服务层常量，与 planning/types.py 的 REASON_* 同手法，不落库。",
}

# ---------------------------------------------------------------------------
# 八、迁移核对清单
# ---------------------------------------------------------------------------

CHECKLIST = (
    "三张表的 CHECK 名带 ck_<table>_ 前缀（NAMING_CONVENTION），逐个核对取值域与 "
    "trial/types.py 的 frozenset 一致（本草案脚本实数共 29 条 CHECK：rounds 18 / slots 5 / decisions 6，"
    "以导出为准，数字不写进代码）。",
    "★ trial_rounds 的两条部分唯一索引必须齐全且 WHERE 子句逐字一致："
    "uq_trial_rounds_project_open（state IN ('GENERATING','PLANNED','REVIEWING')）与 "
    "uq_trial_rounds_project_idempotency（idempotency_key IS NOT NULL）。"
    "只比名字不够（名字相同、WHERE 不同是最可能的漂移形态），要比键列含顺序 + WHERE 规范化文本"
    "（沿用 planning/ddl.py 的 index_spec_drift 手法）。",
    "★ cancel_reason 与 state='CANCELLED' 的双向等值 CHECK 必须存在。",
    "★ supplement_has_source 的双向等值 CHECK 必须存在：少了它，普通试产能带 source_round_id、"
    "补充试产能不带，『补的是哪一轮』就查不到。",
    "★ trial_round_slots 的三条唯一约束必须齐全：(round_id, slot_no)、(content_item_id)、"
    "(replaces_slot_id)。后两条是并发下『一条内容不进两个名额』『一个名额不被补两次』的最后一道。",
    "★ bound_at 与 content_item_id 的双向等值 CHECK 必须存在。",
    "trial_gate_decisions.round_id 唯一约束必须存在。",
    "全部 FK 必须 ON DELETE RESTRICT（本包没有任何级联删除）。",
    "本包**没有**延迟外键（两处自引用都指向更早的行）——若 autogenerate 生成了 DEFERRABLE 的 FK，"
    "那是生成器抄了别的包。",
    "六个触发器由 trial/ddl.all_trigger_ddl() 建出：1 个不可变（decisions_immutable，复用 "
    "rednote_reject_mutation()）+ 2 个 BEFORE 守卫（rounds_transition_guard / slots_guard）+ "
    "3 个约束触发器（rounds_consistency / slots_consistency / decisions_consistency，必须是 "
    "CONSTRAINT TRIGGER 且 DEFERRABLE INITIALLY DEFERRED）。",
    "触发器函数体里的状态迁移矩阵与 trial/types.py 的 TRANSITIONS 逐项对账（本文件第五节也是一处）。",
    "downgrade 只删 OWN_FUNCTIONS 这五个，**不删** rednote_reject_mutation。",
    "全部约束名在 63 字节内。",
    "取值域全部是字面量，迁移里没有 `from rednote.modules...types import`（ISS-191）。",
    "跨包 FK 登记：test_content_wp26.py 的 _KNOWN_CROSS_PACKAGE_FKS + RG-130 同步（见第六节）。",
    "迁移 down_revision = 收口时的当前 head（写本文件时磁盘最新为 d6074ae4bc53，以收口时重取为准）。",
)
