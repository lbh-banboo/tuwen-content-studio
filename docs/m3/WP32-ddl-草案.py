"""WP32 DDL 草案：`batches` / `batch_items` / `batch_item_runs`（+ 主线收口动作：`jobs.batch_id` 补 FK）。

**这不是迁移文件。** 迁移由主 agent 串行收口（接缝冻结 §4.2）。
本文件是**规格**：表名、列、类型、可空、默认值、全部约束与索引、外键、
触发器清单，逐条可核对。**设计阶段产物**：对应的 `modules/batches/models.py`
尚不存在；落地后应按 WP30 草案 "第七节" 的做法再核对一次。

**为什么用 `.py` 而不是 `.md`**：草案里的每一列 / 每个约束将来都对应
`modules/batches/models.py` 的一行，用 Python 写便于并排比对。
**本文件不 import 本包任何模块**（包还不存在；且 import 会让它在"包没装好"的
环境里连带失败）。与 `WP30-ddl-草案.py` / `WP29-ddl-草案.py` 同款。

**本文件的 DDL 文本尚未在验证库实测导出核对**（设计阶段，未建
`rednote_test_wp32`，也未执行任何 DDL）。已做的只有静态核对：
① 约束名全部 <= 63 字节（见第七节清单第 11 条）；② 被引用表在磁盘上都有
`__tablename__`（projects / users / version_nodes / production_baselines /
content_items / jobs）。落地后必须补做 pg_constraint / pg_indexes / pg_trigger
三项实测对账。

---

## 一、文件怎么用

1. 主 agent 用 `models.py` 生成迁移，然后用本文件**逐条核对**生成的 DDL。
2. 迁移顺序见第三节。**`jobs.batch_id` 补 FK 与新建索引是主线收口动作**
   （接缝冻结 §1.1 第 22 行、§5.8、§7.2 第 4 步），分支不碰 `jobs/*.py`。
3. **取值域全部写成字面量**（ISS-191 / 接缝冻结 §4.5）。迁移里**不得**
   `from rednote.modules.batches.types import ...` 再循环它。
   本包一次引入 **3 个**落库取值域：`batch_kind` / `state` / `limit_source`
   （另有 1 个**不落库**的派生域 `progress`，见说明文档 §2.9）。
4. **ADR-13 硬约束**：`batches.quantity` **只有下界** `quantity > 0`。
   **禁止**在 DDL 里出现 10 / 30 / 50 作为上界（含 `quantity <= 30` 之类写法）。
   上界归 `runtime_settings['batch.limits']` + 应用层校验。
   这也是 `planning/types.py:112` 那段注释预告的"batch 档位在库里没有列"
   在本包被**第一次**改变：本包的 `batch_kind` CHECK 是它的首个消费者。
5. **downgrade 只删本包自己的函数**（第五节 `OWN_FUNCTIONS`），
   **不删** 跨包共用的 `rednote_reject_mutation`（ISS-79）。

## 二、三张表的角色与可变性

| 表 | 角色 | 可变性 | 触发器 |
|---|---|---|---|
| `batches` | 一个批次（档位 / 序号 / 数量 / 钉住的方案与基线 / 分配快照 / 生效上限快照 / 预估 / 幂等键） | **可变，但只有两个"命令位"会变**：`state`（SUBMITTED -> CANCEL_REQUESTED）与 `lock_version`/`updated_at`/取消三列 | `trg_batches_item_count_consistency`（约束触发器，延迟） |
| `batch_items` | 批次内第 n 条内容（`(batch_id, ordinal)` 唯一；一条内容至多属于一个批次） | **不可变**（建批次时一次写满） | `trg_batch_items_immutable` + `trg_batch_items_count_consistency` + `trg_batch_items_ownership_consistency` |
| `batch_item_runs` | 某条内容的第 n 轮执行 = 一个 job（第 1 轮 = 初次提交，第 2 轮起 = 失败项重做） | **不可变**（重做 = 追加一行） | `trg_batch_item_runs_immutable` + `trg_batch_item_runs_ownership_consistency` |

**为什么 `batches` 不存"进度"**：执行状态的权威是 `jobs`（`jobs/models.py` 模块
docstring："业务状态权威"）。批次行再存一份 RUNNING/SUCCEEDED/PARTIAL，就是
第二处声称，而且没有钩子能在 job 完成时回写它（`jobs/*.py` 禁改）。
进度在读时由 `batch_item_runs` 取每条内容的**最新轮** job 状态折算，见说明文档 §2.2。
**这是本草案最重要的一处取舍，已列入开放问题（说明文档 §3.7 Q6）。**

**为什么要 `batch_item_runs`**：`jobs` 没有"轮次"列且禁改；用 `created_at` 排序取
"最新 job"在同事务同时刻失效（`now()` 是事务开始时间，同事务插入的行时间戳相同）。
显式的 `round_no`（整数、唯一）既给出确定的"最新轮"，又让"同一条内容同一轮不会
有两个 job"成为数据库级保证 —— 重试幂等的第二道闸（第一道是 job 自己的
`UQ(idempotency_scope, idempotency_key)`）。

## 三、建表顺序与主线收口顺序

```
projects / users / version_nodes / production_baselines / content_items
      ↑        ↑            ↑                  ↑                 ↑
      └────────┴──────── batches ──────────────┘                 │
                            ↑ (batch_id)                          │
                       batch_items ───────────────────────────────┘
                            ↑ (batch_item_id)
                      batch_item_runs ───→ jobs (job_id)
                                             ↑
            jobs.batch_id ──(主线补 FK)──→ batches.id
```

**建表顺序：`batches` -> `batch_items` -> `batch_item_runs`；
然后才是 `jobs.batch_id` 的 FK 与索引。**

* 三张新表引用的都是**已存在**的表，或本包内更早建的表。
  **没有表级环、没有自引用 ⇒ 本包不需要任何 DEFERRABLE 外键。**
  （`jobs -> batches` 与 `batch_item_runs -> jobs` 方向一致，不成环：
  `batches` 不引用 `jobs`。）
* **FK 补全必须晚于 `batches` 建表**：见第六节。
* **downgrade 顺序严格相反**：先删 `jobs` 上的 FK 与索引，再删
  `batch_item_runs` -> `batch_items` -> `batches`，最后删本包 3 个函数。

## 四、三张表的逐列 DDL（specs，不是可执行代码）
"""

BATCHES_DDL = """
-- 表名：batches。一个批次。可变（state / lock_version），其余列写入后不改。
--
-- 约束命名遵循 platform/base.py 的 NAMING_CONVENTION：
--   CheckConstraint(name="state_valid") -> ck_batches_state_valid
--   ForeignKey 自动命名为 fk_<table>_<column_0_N_name>_<referred_table>
--   UniqueConstraint 本表全部显式给全名（与 jobs 的 uq_jobs_scope_key 同款）
--
-- ★ ADR-13：quantity 只有下界（> 0）。**不得**出现 10/30/50 上界 CHECK。
--   上界在 runtime_settings['batch.limits']；创建时刻的生效上界与来源
--   快照到 limit_applied / limit_source（ADR-13 迁移影响 #4：
--   "以创建时刻的生效配置为准"），供事后核对 AC-10~12。
CREATE TABLE batches (
    id                  uuid          NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid          NOT NULL,
    batch_kind          varchar(8)    NOT NULL,   -- BULK / SCALE / TRIAL（见 Q4：TRIAL 是否入表）
    seq_no              integer       NOT NULL,   -- 项目内同档位的序号；展示码 BATCH-S01 由读时拼，不存
    quantity            integer       NOT NULL,   -- 本批条数（只有下界）
    state               varchar(20)   NOT NULL DEFAULT 'SUBMITTED',  -- SUBMITTED / CANCEL_REQUESTED
    plan_version_id     uuid          NOT NULL,   -- 钉住的 PLAN 版本节点（WP24 submit_draft 产出）
    baseline_id         uuid          NOT NULL,   -- 提交时钉住的基线（= projects.active_baseline_id 的快照）
    allocation          jsonb         NOT NULL,   -- 本批分配快照（scenes/forms/unallocated）
    limit_applied       integer       NOT NULL,   -- 创建时刻该档位的生效上界
    limit_source        varchar(16)   NOT NULL,   -- GLOBAL / INHERITED / MANUAL
    estimated_amount    numeric(20,8),            -- 提交时预估总额；NULL = 未预估（**不是 0**）
    estimated_currency  varchar(3),               -- 与 estimated_amount 同有同无
    estimate_refs       jsonb         NOT NULL DEFAULT '[]'::jsonb,
                                                  -- [{taskKind, estimateId}]；无 FK 的审计指针（见下注）
    idempotency_key     varchar(200)  NOT NULL,   -- 客户端 Idempotency-Key
    request_hash        varchar(64)   NOT NULL,   -- 规范化请求体摘要（同键不同体 -> 409）
    cancel_requested_at timestamptz,
    cancel_requested_by uuid,
    created_by          uuid,
    created_at          timestamptz   NOT NULL DEFAULT now(),
    updated_at          timestamptz   NOT NULL DEFAULT now(),
    lock_version        bigint        NOT NULL DEFAULT 1,   -- ETag 载体（platform.base.lock_version_column）

    CONSTRAINT pk_batches PRIMARY KEY (id),

    -- --- 取值域（与 types.py 的 frozenset 逐项一致，**字面量化石**） --------
    CONSTRAINT ck_batches_batch_kind_valid
        CHECK (batch_kind IN ('BULK', 'SCALE', 'TRIAL')),
    CONSTRAINT ck_batches_state_valid
        CHECK (state IN ('CANCEL_REQUESTED', 'SUBMITTED')),
    CONSTRAINT ck_batches_limit_source_valid
        CHECK (limit_source IN ('GLOBAL', 'INHERITED', 'MANUAL')),

    -- --- 数值边界（只有下界；无任何 10/30/50） ----------------------------
    CONSTRAINT ck_batches_seq_no_positive
        CHECK (seq_no >= 1),
    CONSTRAINT ck_batches_quantity_positive
        CHECK (quantity > 0),
    CONSTRAINT ck_batches_limit_applied_positive
        CHECK (limit_applied >= 1),
    CONSTRAINT ck_batches_lock_version_positive
        CHECK (lock_version >= 1),

    -- --- 预估：金额与币种同有同无；NULL / 0 分离 ----------------------------
    CONSTRAINT ck_batches_estimate_pair_consistent
        CHECK ((estimated_amount IS NULL) = (estimated_currency IS NULL)),
    CONSTRAINT ck_batches_estimated_amount_non_negative
        CHECK (estimated_amount IS NULL OR estimated_amount >= 0),
    CONSTRAINT ck_batches_estimate_refs_is_array
        CHECK (jsonb_typeof(estimate_refs) = 'array'),
    CONSTRAINT ck_batches_allocation_is_object
        CHECK (jsonb_typeof(allocation) = 'object'),

    -- --- 取消：state 与取消时刻**双向等值**；取消人只能在有取消时刻时出现 --
    CONSTRAINT ck_batches_cancel_pair_consistent
        CHECK ((state = 'CANCEL_REQUESTED') = (cancel_requested_at IS NOT NULL)),
    CONSTRAINT ck_batches_cancel_actor_requires_time
        CHECK (cancel_requested_by IS NULL OR cancel_requested_at IS NOT NULL),

    -- --- 幂等载体不得为空串 ------------------------------------------------
    CONSTRAINT ck_batches_idempotency_key_not_blank
        CHECK (char_length(idempotency_key) > 0),
    CONSTRAINT ck_batches_request_hash_not_blank
        CHECK (char_length(request_hash) > 0),

    -- --- 唯一约束（幂等与序号的数据库级载体；M4 幂等口径 §0："载体是 DB 约束，不是应用层 if"） --
    -- 同项目同幂等键只能有一个批次。并发双提交的输家撞这条（SQLSTATE 23505），
    -- 服务层须在 SAVEPOINT 内捕获后回查并比对 request_hash（ISS-22 教训）。
    CONSTRAINT uq_batches_project_idempotency_key
        UNIQUE (project_id, idempotency_key),
    -- 项目内同档位序号唯一。序号分配 = max+1，撞键则 SAVEPOINT 内重取（<=3 次）。
    CONSTRAINT uq_batches_project_kind_seq
        UNIQUE (project_id, batch_kind, seq_no),

    -- --- 外键：全部 ON DELETE RESTRICT（落库即证据，本包无级联删除） ------
    CONSTRAINT fk_batches_project_id_projects
        FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_batches_plan_version_id_version_nodes
        FOREIGN KEY (plan_version_id) REFERENCES version_nodes (id) ON DELETE RESTRICT,
    CONSTRAINT fk_batches_baseline_id_production_baselines
        FOREIGN KEY (baseline_id) REFERENCES production_baselines (id) ON DELETE RESTRICT,
    CONSTRAINT fk_batches_cancel_requested_by_users
        FOREIGN KEY (cancel_requested_by) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_batches_created_by_users
        FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT
);

-- estimate_refs 刻意**不是**子表也不加 FK：一次 FULL 范围的批次要预估两个 task_kind
-- （CONTENT_STRUCTURE / CONTENT_COPY，见 content/billing_bridge.py 的说明），
-- 一列 FK 装不下；为它建 batch_estimates 子表又是"为一次展示建表"。
-- 代价已登记：指针失效（cost_estimates 被清理）不会被数据库发现。
-- cost_estimates 是否可清理见说明文档 §3.7 Q3 的附带核对项。

-- 项目内批次列表（按创建时间倒序读；btree 方向无影响）。
CREATE INDEX ix_batches_project_created
    ON batches (project_id, created_at);
-- "这份 PLAN 版本被哪些批次用了"（累计配额 allocated_so_far 与 WP34 影响面的读路径）。
CREATE INDEX ix_batches_plan_version_id
    ON batches (plan_version_id);
-- "这份基线被哪些批次用了"（基线漂移提示与 WP34 影响面）。
CREATE INDEX ix_batches_baseline_id
    ON batches (baseline_id);
"""

BATCH_ITEMS_DDL = """
-- 表名：batch_items。批次内第 n 条内容。**不可变**（建批次时一次写满）。
--
-- ★ UNIQUE (content_item_id)：一条内容至多属于一个批次。
--   这是"扩产内容不含原试产内容"（原型 scale-batch.html 原话：
--   "均为整数且不含原试产内容"）的数据库级落点：批次总是**新建**内容行，
--   同一条内容不可能同时出现在试产批与扩产批里。
--
-- scene_key / form_key 可空且同有同无：NULL = "尚未指派"（**不是**某个默认值）。
-- 联合展开规则（两个边际分配如何配对成每条内容的 (场景, 形式)）在仓内没有定义
-- （说明文档 §3.7 Q2，已登记 OP-227，待定未决）。
-- D1 已决策（2026-10-19）：主线在 content 根层开接缝，content_items 增可空列
-- scene_key / form_key，GenerationRequest 可带入 —— 即生成链路将消费这两个值（Q1 已答），
-- 本表的 scene_key / form_key 经该接缝写入，不再恒 NULL。
-- 但在 OP-227 拍板前，本包不编造 form_key 的配对：form_key 仍写 NULL；
-- 下方「同有同无」CHECK 是否需放宽为允许 scene_key 单独非空，待主线确认。
CREATE TABLE batch_items (
    id              uuid         NOT NULL DEFAULT gen_random_uuid(),
    batch_id        uuid         NOT NULL,
    ordinal         integer      NOT NULL,   -- 批次内序号，从 1 起
    content_item_id uuid         NOT NULL,
    scene_key       varchar(100),            -- plan payload scenes[].key；NULL = 未指派
    form_key        varchar(100),            -- plan payload forms[].key；NULL = 未指派
    created_at      timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT pk_batch_items PRIMARY KEY (id),

    CONSTRAINT ck_batch_items_ordinal_positive
        CHECK (ordinal >= 1),
    CONSTRAINT ck_batch_items_assignment_paired
        CHECK ((scene_key IS NULL) = (form_key IS NULL)),
    CONSTRAINT ck_batch_items_scene_key_not_blank
        CHECK (scene_key IS NULL OR char_length(scene_key) > 0),
    CONSTRAINT ck_batch_items_form_key_not_blank
        CHECK (form_key IS NULL OR char_length(form_key) > 0),

    -- 批次内序号唯一（自带 (batch_id, ordinal) 索引，覆盖"按批次顺序读"）。
    CONSTRAINT uq_batch_items_batch_ordinal
        UNIQUE (batch_id, ordinal),
    -- ★ 一条内容至多属于一个批次。
    CONSTRAINT uq_batch_items_content_item_id
        UNIQUE (content_item_id),

    CONSTRAINT fk_batch_items_batch_id_batches
        FOREIGN KEY (batch_id) REFERENCES batches (id) ON DELETE RESTRICT,
    CONSTRAINT fk_batch_items_content_item_id_content_items
        FOREIGN KEY (content_item_id) REFERENCES content_items (id) ON DELETE RESTRICT
);
"""

BATCH_ITEM_RUNS_DDL = """
-- 表名：batch_item_runs。某条内容的第 n 轮执行 = 一个 job。**不可变**。
--
-- round_no = 1 是初次提交；>= 2 是"失败项重做"（POST /batches/{id}/retries）。
-- "这条内容现在的状态" = 取 round_no 最大的那一行对应的 job.state，
-- **不是** ORDER BY created_at / jobs.updated_at（见第二节）。
CREATE TABLE batch_item_runs (
    id            uuid         NOT NULL DEFAULT gen_random_uuid(),
    batch_item_id uuid         NOT NULL,
    round_no      integer      NOT NULL,
    job_id        uuid         NOT NULL,
    created_at    timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT pk_batch_item_runs PRIMARY KEY (id),

    CONSTRAINT ck_batch_item_runs_round_no_positive
        CHECK (round_no >= 1),

    -- 同一条内容同一轮只能有一个 job（重试幂等的第二道闸）。
    CONSTRAINT uq_batch_item_runs_item_round
        UNIQUE (batch_item_id, round_no),
    -- 一个 job 只属于一个 (内容, 轮次)。
    CONSTRAINT uq_batch_item_runs_job_id
        UNIQUE (job_id),

    CONSTRAINT fk_batch_item_runs_batch_item_id_batch_items
        FOREIGN KEY (batch_item_id) REFERENCES batch_items (id) ON DELETE RESTRICT,
    CONSTRAINT fk_batch_item_runs_job_id_jobs
        FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE RESTRICT
);
"""

# ---------------------------------------------------------------------------
# 五、触发器清单（六个；其中 2 个复用跨包函数，4 个用本包 3 个函数）
# ---------------------------------------------------------------------------

TRIGGERS = (
    {
        "table": "batch_items",
        "name": "trg_batch_items_immutable",
        "timing": "BEFORE UPDATE OR DELETE, FOR EACH ROW",
        "function": "rednote_reject_mutation()",  # 跨包共用，downgrade 不删
        "why": "批次成员建批次时一次写满；改成员 = 改『这批到底含哪些内容』，必须是新批次。",
    },
    {
        "table": "batch_item_runs",
        "name": "trg_batch_item_runs_immutable",
        "timing": "BEFORE UPDATE OR DELETE, FOR EACH ROW",
        "function": "rednote_reject_mutation()",  # 跨包共用，downgrade 不删
        "why": "执行轮次是历史事实；重做 = 追加下一轮，不改上一轮。",
    },
    {
        "table": "batches",
        "name": "trg_batches_item_count_consistency",
        "timing": (
            "AFTER INSERT, FOR EACH ROW, CONSTRAINT TRIGGER "
            "DEFERRABLE INITIALLY DEFERRED"
        ),
        "function": "rednote_check_batch_item_count()",
        "predicate": (
            "SELECT count(*) FROM batch_items WHERE batch_id = NEW.id"
            "  必须 = NEW.quantity（提交时检查）"
        ),
        "why": (
            "quantity 与成员行数是两处声称。不一致的表现不是报错，而是"
            "『提交了 30 条，只生成了 28 条』且批次看起来正常。"
            "延迟到提交时检查，所以同事务内先建批次、后建成员的写法不受影响。"
        ),
    },
    {
        "table": "batch_items",
        "name": "trg_batch_items_count_consistency",
        "timing": (
            "AFTER INSERT, FOR EACH ROW, CONSTRAINT TRIGGER "
            "DEFERRABLE INITIALLY DEFERRED"
        ),
        "function": "rednote_check_batch_item_count()",  # 与上一条同一个函数，按 TG_TABLE_NAME 分支
        "predicate": (
            "SELECT count(*) FROM batch_items WHERE batch_id = NEW.batch_id"
            "  必须 = (SELECT quantity FROM batches WHERE id = NEW.batch_id)"
        ),
        "why": (
            "堵另一个方向：批次提交后有人再往里塞成员（成员表不可变，但 INSERT 是允许的），"
            "行数会超过 quantity。"
        ),
    },
    {
        "table": "batch_items",
        "name": "trg_batch_items_ownership_consistency",
        "timing": (
            "AFTER INSERT, FOR EACH ROW, CONSTRAINT TRIGGER "
            "DEFERRABLE INITIALLY DEFERRED"
        ),
        "function": "rednote_check_batch_item_ownership()",
        "predicate": (
            "(SELECT project_id FROM content_items WHERE id = NEW.content_item_id)"
            " = (SELECT project_id FROM batches WHERE id = NEW.batch_id)"
        ),
        "why": (
            "FK 只保证被引用行存在，不管它属于哪个项目。批次把别的项目的内容"
            "算进自己的进度与费用，不会报错。"
        ),
    },
    {
        "table": "batch_item_runs",
        "name": "trg_batch_item_runs_ownership_consistency",
        "timing": (
            "AFTER INSERT, FOR EACH ROW, CONSTRAINT TRIGGER "
            "DEFERRABLE INITIALLY DEFERRED"
        ),
        "function": "rednote_check_batch_item_run_ownership()",
        "predicate": (
            "jobs.batch_id = batch_items.batch_id"
            " AND jobs.content_id = batch_items.content_item_id"
            "（job 经 NEW.job_id、成员经 NEW.batch_item_id 取得；"
            "jobs.batch_id 必须非空且相等）"
        ),
        "why": (
            "把 jobs.batch_id / jobs.content_id 这两个『预留关联位』与本包的"
            "成员关系钉死成同一件事。否则进度折算读 batch_item_runs、"
            "按 batch 统计 job 读 jobs.batch_id，两条路径会各说各话。"
        ),
    },
)

#: 本包**自己的**函数（downgrade 只删这三个）。`rednote_reject_mutation`
#: 是跨包共用函数，**不在此列** —— 删它会让所有已建表的不可变守卫
#: 静默失效（ISS-79）。
OWN_FUNCTIONS = (
    "rednote_check_batch_item_count",
    "rednote_check_batch_item_ownership",
    "rednote_check_batch_item_run_ownership",
)

# ---------------------------------------------------------------------------
# 六、主线收口动作：`jobs.batch_id` 补 FK + 索引（分支不做，写在这里供核对）
# ---------------------------------------------------------------------------

MAIN_AGENT_JOBS_DDL = """
-- ===== 以下语句由主 agent 在 batches 建表之后的同一迁移（或紧随其后的迁移）执行 =====
-- 目标 FK 语句（目标名按 NAMING_CONVENTION 自动生成，27 字节）：
ALTER TABLE jobs
    ADD CONSTRAINT fk_jobs_batch_id_batches
    FOREIGN KEY (batch_id) REFERENCES batches (id) ON DELETE RESTRICT;

-- jobs 当前没有 batch_id 上的索引（jobs 的索引只有 ix_jobs_claim / ix_jobs_project_id /
-- ix_jobs_lease_until）。FK 本身不会在引用列上自动建索引；
-- "这个批次的任务有哪些 / 批次下各状态各多少"依赖它。部分索引：绝大多数 job 没有批次。
CREATE INDEX ix_jobs_batch_id
    ON jobs (batch_id)
    WHERE batch_id IS NOT NULL;
"""

MAIN_AGENT_JOBS_ORDER_NOTES = (
    "FK 必须晚于 batches 建表，早了会撞 UndefinedTable。",
    "加 FK 之前先预检：SELECT count(*) FROM jobs WHERE batch_id IS NOT NULL 与"
    " NOT EXISTS (SELECT 1 FROM batches b WHERE b.id = jobs.batch_id)。"
    " 磁盘上没有任何写入路径会设 batch_id（content/api.py 入队时只传 content_id），"
    " 但这是推断而非实测；若有孤儿值，FK 会在 ALTER 时失败。"
    " 稳妥写法：ADD CONSTRAINT ... NOT VALID，再 VALIDATE CONSTRAINT。",
    "同时要改 jobs/models.py：batch_id 列加 ForeignKey('batches.id', ondelete='RESTRICT')，"
    " 并更正 :195-197 的过期注释（它写『属 WP24 / WP29』）。主线动作，分支不改。",
    "⚠ 副作用（推理，未实测）：jobs 的 ORM 一旦带上指向 batches 的 FK，任何只"
    " create_all(tables=[jobs 相关表]) 而没有导入 batches.models 的测试夹具，"
    " 会在建表时报 NoReferencedTableError。收口时需要把 batches.models 的导入"
    " 放到所有会 create_all jobs 的地方（或放进公共的模型注册点）。见说明文档 §3.7 Q12。",
    "jobs.content_id 的 FK（-> content_items）不是 WP32 的前置，也不在本草案范围；"
    " 批次通过 batch_items / batch_item_runs 已经有了完整的 (批次, 内容, job) 关系。",
    "downgrade：先 DROP INDEX ix_jobs_batch_id、DROP CONSTRAINT fk_jobs_batch_id_batches，"
    " 再删本包三张表。",
)

# ---------------------------------------------------------------------------
# 七、迁移核对清单
# ---------------------------------------------------------------------------

CHECKLIST = (
    "三张表的 CHECK 名带 ck_<table>_ 前缀（NAMING_CONVENTION），逐个核对取值域"
    "与 types.py 的 frozenset 一致（草案共 15 + 4 + 1 = 20 条 CHECK："
    "batches 15 / batch_items 4 / batch_item_runs 1）。",
    "★ ADR-13：batches.quantity 的 CHECK 只能是 quantity > 0。"
    "grep 迁移文本不得出现 30 / 50 / 10 作为 quantity 的比较右值。",
    "★ ck_batches_cancel_pair_consistent 是双向等值（state 与 cancel_requested_at）。"
    "少了反向，就会出现 state='SUBMITTED' 却带着取消时刻的行。",
    "★ ck_batches_estimate_pair_consistent：金额与币种同有同无；NULL 表示未预估，"
    "不得用 0 代替（接缝冻结 §6.3 第 7 条）。",
    "uq_batches_project_idempotency_key 必须存在 —— 它是批次级幂等的数据库载体，"
    "服务层的『先查再插』在并发下有窗口。",
    "uq_batch_items_content_item_id 必须存在 —— 否则一条内容可以同时属于试产批和扩产批。",
    "uq_batch_item_runs_item_round / uq_batch_item_runs_job_id 必须存在。",
    "全部 FK 必须 ON DELETE RESTRICT（本包没有任何级联删除）。",
    "本包没有延迟外键（无自引用、无表级环）—— 若 autogenerate 生成了 DEFERRABLE 的 FK，"
    "那是生成器抄了别的包。",
    "六个触发器：2 个不可变复用 rednote_reject_mutation()；4 个一致性是"
    " CONSTRAINT TRIGGER 且 DEFERRABLE INITIALLY DEFERRED，由 ddl.all_trigger_ddl() 建出。",
    "全部约束 / 索引名在 63 字节内（草案文本静态核对：共 43 个名字，最长 44 字节，"
    "fk_batch_items_content_item_id_content_items 与 fk_batch_item_runs_batch_item_id_batch_items；"
    "无重名）。落地后以 pg_constraint 实测为准。",
    "downgrade 只删 OWN_FUNCTIONS 这三个，不删 rednote_reject_mutation。",
    "取值域写字面量，不 import 活常量（ISS-191；"
    "check_migration_value_domain_no_live_import.py 的射程内）。",
    "jobs 侧收口（第六节）：FK + ix_jobs_batch_id 都要在，且在 batches 之后。",
    "迁移落地后补做：pg_constraint / pg_indexes / pg_trigger 三项实测对账"
    "（WP30 草案 CHECKLIST 同款），并把『实测』字样补回本文件头部。",
)
