"""WP30 DDL 草案：`self_check_runs` / `self_check_rule_results` / `self_check_findings`。

**这不是迁移文件。** 迁移由主 agent 串行收口（接缝冻结 §4.2）。
本文件是**规格**：表名、列、类型、可空、默认值、全部约束与索引、外键、
触发器清单，逐条可核对。

**为什么用 `.py` 而不是 `.md`**：草案里的每一列 / 每个约束都对应
`modules/review/models.py` 的一行。用 Python 写，读者可以把两边并排比对；
写成散文则必须来回翻。**本文件不 import 本包任何模块** ——
它要能在"包还没装好"的环境里被打开阅读，而 import 会让它连带失败。
（与 `WP29-ddl-草案.py` / `WP22-ddl-草案.py` 同款。）

**本文件的 DDL 文本已由验证库实测导出核对**（`rednote_test_wp30`，
`alembic upgrade head` 到 `efa306a18363` + 本包测试夹具的 `create_all`；
逐列 / `pg_constraint` / `pg_indexes` / `pg_trigger` 四项与下方文本一致）。
迁移落地后应再核对一次（与 WP22 草案第七节同款）。

---

## 一、文件怎么用

1. 主 agent 用 `models.py` 生成迁移（`alembic revision --autogenerate`），
   然后用本文件**逐条核对**生成的 DDL。
2. 迁移必须调 `ddl.all_trigger_ddl()` 把**五个**触发器一并建出
   （三个不可变 + 两个一致性）。
3. **升级与降级都必须只碰本模块自己的对象**：降级只删
   `consistency_function_names()` 列出的**两个**函数，**不含**跨包共用的
   `rednote_reject_mutation`（ISS-79：删它会让所有已建表的不可变守卫
   变成空触发器）。
4. **取值域全部写成字面量**（ISS-191）。迁移里**不得**
   `from rednote.modules.review.types import ...` 再循环它 ——
   那会让 `check_migration_model_parity.py` 恒绿（两侧取同一活常量、
   同时移动、永不分叉）。本包一次引入 **7 个**新取值域
   （`trigger_kind` / `outcome` / `rule_kind` / `status` / `category` /
   `severity` / `anchor_kind`），是本批的高危点之首。
5. ⚠️ **`rule_code` 与 `skip_reason` 刻意没有取值域 CHECK**，
   迁移的 docstring 必须写明这条取舍的理由（见第四节末），
   否则下一个人会"顺手补上"而把开集冻成化石。

## 二、三张表的角色与可变性

| 表 | 角色 | 可变性 | 触发器 |
|---|---|---|---|
| `self_check_runs` | 一轮自检（判的是哪一版 / 哪版规则 / 聚合结论 + 五个计数） | **不可变**（重跑 = 追加一行） | `trg_self_check_runs_immutable` + `trg_self_check_runs_consistency`（约束触发器，延迟） |
| `self_check_rule_results` | 一轮 × 一规则 = 一行 | **不可变** | `trg_self_check_rule_results_immutable` |
| `self_check_findings` | 一条命中（锚点三选一 + evidence + 回指 AI 反馈） | **不可变** | `trg_self_check_findings_immutable` + `trg_self_check_findings_anchor_consistency`（约束触发器，延迟） |

**为什么三张表全不可变**（与 WP29 的 `content_feedback_items` 恰好相反）：
自检结论是**观测**，不是工作区。"这一刻的规则对这一版稿子给出了什么结论"
是历史事实；需要新结论就再跑一轮、再追加一行。原地改会让
『上周判通过、今天判阻断』无从解释（也无从区分"稿子变了"与"规则变了"）。
运营要推进的状态机落在 WP29 的反馈行上 —— 本包只**建**反馈、不**改**反馈。

⇒ 本包**零 `If-Match`**：没有原地改的路径，就没有乐观锁载体。

## 三、建表顺序与拆环

```
projects / users / content_items / content_versions
      ↑        ↑          ↑              ↑
      └────────┴──── self_check_runs ────┘
                            ↑ (run_id)
                   self_check_rule_results
                            ↑ (rule_result_id)
                     self_check_findings
                       ↓               ↓
                  media_items   content_feedback_items
```

**建表顺序：`self_check_runs` → `self_check_rule_results`
→ `self_check_findings`。**

三张表引用的都是**已存在**的表（`projects` / `users` / `content_items` /
`content_versions` / `media_items` / `content_feedback_items`），
或本包内更早建的表。**没有表级环，也没有自引用** ⇒
本包**一条延迟外键都不需要**（与 `content_reviews.supersedes_id`、
`content_items.current_version_id` 不同）。

⚠️ **`self_check_findings.feedback_item_id → content_feedback_items.id`
是一条跨包入向 FK**，它会改变 WP29 测试夹具的 drop/keep 语义 —— 见第六节。

## 四、三张表的逐列 DDL（specs，不是可执行代码）

### 4.1 `self_check_runs`

**`outcome` 的四条双向等价式**是全表最重要的约束组。`outcome` 由服务层算、
五个计数也由服务层写 ⇒ 两处都可能漏改，而表现是
"`outcome='PASSED'` 但 `blocking_count=1`"——读端点通常**只看 outcome**，
于是阻断内容显示为通过。把等价式下推到数据库，这个漂移就不可能落库。

| outcome | 等价条件 |
|---|---|
| `BLOCKED` | `blocking_count > 0` |
| `ERRORED` | `blocking_count = 0 AND errored_count > 0` |
| `WARNED` | `blocking_count = 0 AND errored_count = 0 AND warning_count > 0` |
| `PASSED` | `blocking_count = 0 AND errored_count = 0 AND warning_count = 0` |

**定序 `BLOCKED > ERRORED > WARNED > PASSED`**：`BLOCKED` 已是"不能通过"的
最强结论，降级成 `ERRORED` 会让前端丢掉"为什么不能过"；而 `ERRORED`
（无阻断）必须压过 `WARNED`/`PASSED` ——"规则没跑成"绝不能被读成
"没问题"（fail-closed）。

**`skipped_count` 与 `info_count` 刻意不参与 outcome**：
未判既不是通过也不是不通过；`INFO` 是登记事实，不干预审核结论。
本包有**三类恒 SKIPPED** 的规则（`TONE`/`DUPLICATE`/`SCENE`），
若把 skipped 折算成 pass，「5 类判了 + 3 类没判」就会被渲染成
「8 类全通过」——而原型的批量通过按钮正是以"已完成自检、无阻断问题"为前提。

**不加 `(content_item_id, rule_set_version)` 唯一约束**：
同一版、同一规则集**允许重跑** —— 重跑是诊断手段，不是重复数据。
"哪一轮是最新的"由 `(content_item_id, created_at DESC, id DESC)` 回答
（`id` 是必须的第二排序键：同一事务里插入的多行时间戳相同）。
"""

SELF_CHECK_RUNS_DDL = """
-- 表名：self_check_runs。一轮自检。**不可变**（重跑 = 追加一行）。
--
-- 约束命名遵循 platform/base.py 的 NAMING_CONVENTION：
--   CheckConstraint(name="outcome_valid") → ck_self_check_runs_outcome_valid
--   ForeignKey 自动命名为 fk_<table>_<column_0_N_name>_<referred_table>
--
-- ⚠️ 本表最长的自动生成约束名是
--    ck_self_check_runs_finished_at_not_before_started_at（52 字节）与
--    fk_self_check_runs_subject_version_id_content_versions（54 字节），
--    都在 PostgreSQL 的 63 字节上限内（实测）。超限会被**静默截断**并追加
--    哈希，导致 ORM 元数据里的名字与 pg_constraint 里的实际名字永久不一致。
CREATE TABLE self_check_runs (
    id                 uuid        NOT NULL DEFAULT gen_random_uuid(),
    project_id         uuid        NOT NULL,
    content_item_id    uuid        NOT NULL,
    subject_version_id uuid        NOT NULL,   -- 这一轮判的是哪一版（必填）
    rule_set_version   integer     NOT NULL,   -- types.RULE_SET_VERSION 的快照
    trigger_kind       varchar(16) NOT NULL,   -- MANUAL / BATCH（**无 AUTO**）
    outcome            varchar(16) NOT NULL,   -- PASSED / WARNED / BLOCKED / ERRORED
    blocking_count     integer     NOT NULL,   -- 命中数按严重度分列
    warning_count      integer     NOT NULL,
    info_count         integer     NOT NULL,
    evaluated_count    integer     NOT NULL,   -- 给出 PASSED/FAILED 的规则数
    skipped_count      integer     NOT NULL,   -- **未判数单列**，不参与 outcome
    errored_count      integer     NOT NULL,
    started_at         timestamptz NOT NULL,
    finished_at        timestamptz NOT NULL,
    created_by         uuid,                   -- 谁触发了这一轮
    created_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pk_self_check_runs PRIMARY KEY (id),

    -- --- 取值域（与 types.py 的 frozenset 逐项一致，**字面量化石**） -------
    CONSTRAINT ck_self_check_runs_trigger_kind_valid
        CHECK (trigger_kind IN ('BATCH', 'MANUAL')),
    CONSTRAINT ck_self_check_runs_outcome_valid
        CHECK (outcome IN ('BLOCKED', 'ERRORED', 'PASSED', 'WARNED')),

    -- --- 计数的下界 -------------------------------------------------------
    CONSTRAINT ck_self_check_runs_rule_set_version_positive
        CHECK (rule_set_version >= 1),
    CONSTRAINT ck_self_check_runs_blocking_count_non_negative
        CHECK (blocking_count >= 0),
    CONSTRAINT ck_self_check_runs_warning_count_non_negative
        CHECK (warning_count >= 0),
    CONSTRAINT ck_self_check_runs_info_count_non_negative
        CHECK (info_count >= 0),
    CONSTRAINT ck_self_check_runs_skipped_count_non_negative
        CHECK (skipped_count >= 0),
    CONSTRAINT ck_self_check_runs_errored_count_non_negative
        CHECK (errored_count >= 0),
    -- 一轮至少跑成一条规则，否则是**空转轮次**（"自检过了"但什么都没判）。
    CONSTRAINT ck_self_check_runs_evaluated_count_positive
        CHECK (evaluated_count >= 1),
    CONSTRAINT ck_self_check_runs_finished_at_not_before_started_at
        CHECK (finished_at >= started_at),

    -- --- ★ outcome 的四条**双向等价式**（本表的核心，见第四节 4.1） -------
    CONSTRAINT ck_self_check_runs_outcome_blocked_equivalence
        CHECK ((outcome = 'BLOCKED') = (blocking_count > 0)),
    CONSTRAINT ck_self_check_runs_outcome_errored_equivalence
        CHECK ((outcome = 'ERRORED')
               = (blocking_count = 0 AND errored_count > 0)),
    CONSTRAINT ck_self_check_runs_outcome_warned_equivalence
        CHECK ((outcome = 'WARNED')
               = (blocking_count = 0 AND errored_count = 0
                  AND warning_count > 0)),
    CONSTRAINT ck_self_check_runs_outcome_passed_equivalence
        CHECK ((outcome = 'PASSED')
               = (blocking_count = 0 AND errored_count = 0
                  AND warning_count = 0)),

    -- --- 外键：全部 ON DELETE RESTRICT（落库即证据，本包无级联删除） ------
    CONSTRAINT fk_self_check_runs_project_id_projects
        FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_self_check_runs_content_item_id_content_items
        FOREIGN KEY (content_item_id) REFERENCES content_items (id) ON DELETE RESTRICT,
    CONSTRAINT fk_self_check_runs_subject_version_id_content_versions
        FOREIGN KEY (subject_version_id) REFERENCES content_versions (id) ON DELETE RESTRICT,
    CONSTRAINT fk_self_check_runs_created_by_users
        FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT
);

-- 看板与历史的读路径（倒序由查询给；索引方向对 btree 无影响）。
CREATE INDEX ix_self_check_runs_content_created
    ON self_check_runs (content_item_id, created_at);
CREATE INDEX ix_self_check_runs_project_created
    ON self_check_runs (project_id, created_at);
-- "这一版被自检过哪些轮"（STALE 判定与影响面）。
CREATE INDEX ix_self_check_runs_subject_version
    ON self_check_runs (subject_version_id);
"""

SELF_CHECK_RULE_RESULTS_DDL = """
-- 表名：self_check_rule_results。一轮 × 一规则 = 一行。**不可变**。
--
-- 四个状态各有自己的必填项，全部写成**双向等值** CHECK：
--
--   | status  | severity | skip_reason | error_note | finding_count |
--   |---------|----------|-------------|------------|---------------|
--   | PASSED  | NULL     | NULL        | NULL       | 0             |
--   | FAILED  | 非空     | NULL        | NULL       | > 0           |
--   | SKIPPED | NULL     | **非空**    | NULL       | 0             |
--   | ERRORED | NULL     | NULL        | **非空**   | 0             |
--
-- 写成等值而不是两条单向条件，是因为反向也是坏数据：
-- "没 FAILED 却带了严重度"会让统计把它算进命中；
-- "FAILED 却零 finding"会让前端显示"有问题"但点不开任何一处。
CREATE TABLE self_check_rule_results (
    id            uuid        NOT NULL DEFAULT gen_random_uuid(),
    run_id        uuid        NOT NULL,
    rule_code     varchar(60) NOT NULL,   -- MAX_RULE_CODE_CHARS；**无取值域 CHECK**
    category      varchar(24) NOT NULL,   -- 8 类之一（闭集）
    rule_kind     varchar(8)  NOT NULL,   -- HARD / SOFT（**不是**严重度的同义词）
    status        varchar(12) NOT NULL,   -- PASSED / FAILED / SKIPPED / ERRORED
    severity      varchar(16),            -- 仅 FAILED 时非空
    skip_reason   varchar(40),            -- 仅 SKIPPED 时非空；**无取值域 CHECK**
    error_note    varchar(500),           -- 仅 ERRORED 时非空（规则崩了的诊断串）
    finding_count integer     NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pk_self_check_rule_results PRIMARY KEY (id),

    -- --- 取值域（闭集的三个落 CHECK；rule_code / skip_reason 刻意不落） ----
    CONSTRAINT ck_self_check_rule_results_rule_code_not_blank
        CHECK (char_length(rule_code) > 0),
    CONSTRAINT ck_self_check_rule_results_category_valid
        CHECK (category IN ('DUPLICATE', 'FACT', 'MEDIA_CONSISTENCY', 'OTHER',
                            'SCENE', 'STRUCTURE', 'TECH_FORMAT', 'TONE')),
    CONSTRAINT ck_self_check_rule_results_rule_kind_valid
        CHECK (rule_kind IN ('HARD', 'SOFT')),
    CONSTRAINT ck_self_check_rule_results_status_valid
        CHECK (status IN ('ERRORED', 'FAILED', 'PASSED', 'SKIPPED')),
    CONSTRAINT ck_self_check_rule_results_severity_valid
        CHECK (severity IS NULL
               OR severity IN ('BLOCKING', 'INFO', 'WARNING')),

    -- --- 四态配对（**双向等值**，见本段开头的表） -------------------------
    CONSTRAINT ck_self_check_rule_results_severity_paired_with_failed
        CHECK ((status = 'FAILED') = (severity IS NOT NULL)),
    CONSTRAINT ck_self_check_rule_results_skip_reason_paired_with_skipped
        CHECK ((status = 'SKIPPED') = (skip_reason IS NOT NULL)),
    CONSTRAINT ck_self_check_rule_results_error_note_paired_with_errored
        CHECK ((status = 'ERRORED') = (error_note IS NOT NULL)),
    CONSTRAINT ck_self_check_rule_results_finding_count_non_negative
        CHECK (finding_count >= 0),
    CONSTRAINT ck_self_check_rule_results_finding_count_paired_with_failed
        CHECK ((status = 'FAILED') = (finding_count > 0)),

    -- 一轮里一条规则只能有一个结论（防重复写；唯一约束自带索引）。
    -- ⚠️ 名字**手动给短**：按约定自动生成的名字
    --    (uq_self_check_rule_results_run_id_rule_code) 长 46 字节，仍在上限内，
    --    但显式短名让『按 run+code 唯一』这件事在约束名里一眼可见。
    CONSTRAINT uq_self_check_rule_results_run_code UNIQUE (run_id, rule_code),

    CONSTRAINT fk_self_check_rule_results_run_id_self_check_runs
        FOREIGN KEY (run_id) REFERENCES self_check_runs (id) ON DELETE RESTRICT
);

-- 按分类分组（原型的 8 张卡片）与按结论筛选（"哪几条没判"）。
CREATE INDEX ix_self_check_rule_results_run_category
    ON self_check_rule_results (run_id, category);
CREATE INDEX ix_self_check_rule_results_run_status
    ON self_check_rule_results (run_id, status);
"""

SELF_CHECK_FINDINGS_DDL = """
-- 表名：self_check_findings。一条命中。**不可变**。
--
-- 锚点三选一（与 WP29 的 content_feedback_items 同值、三条 CHECK 同形）：
--
--   | anchor_kind | paragraph_ref | media_item_id |
--   |-------------|---------------|---------------|
--   | WHOLE       | NULL          | NULL          |
--   | PARAGRAPH   | 非空          | NULL          |
--   | MEDIA       | NULL          | 非空          |
--
-- paragraph_ref **刻意不加 FK**：段落不是表，它是正文切段后的产物
-- （p<序号>-<8位哈希>），只对本轮 subject_version_id 那一版正文有意义。
--
-- 本表**刻意不带** run_id / rule_code / category / severity 四个冗余投影列：
-- 本仓的纪律是"每个投影列都要配一条触发器钉住，否则是第二处声称"。
-- 这四列省下的只是一次 join（findings → rule_results → runs），
-- 买不起四条触发器。
CREATE TABLE self_check_findings (
    id               uuid         NOT NULL DEFAULT gen_random_uuid(),
    rule_result_id   uuid         NOT NULL,
    anchor_kind      varchar(20)  NOT NULL,
    paragraph_ref    varchar(200),           -- MAX_PARAGRAPH_REF_CHARS；无 FK
    media_item_id    uuid,
    message          varchar(500) NOT NULL,  -- 人读的一句话
    evidence         jsonb        NOT NULL,  -- **机器可判的事实**（camelCase 键）
    feedback_item_id uuid,                   -- 仅阻断项落；**跨包入向 FK**
    created_at       timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT pk_self_check_findings PRIMARY KEY (id),

    -- --- 锚点三选一：列占用必须与 anchor_kind 对得上 ----------------------
    -- 每条写成"不是这种锚点，或者列占用正确"，三条件合起来互斥且完备。
    CONSTRAINT ck_self_check_findings_anchor_kind_valid
        CHECK (anchor_kind IN ('MEDIA', 'PARAGRAPH', 'WHOLE')),
    CONSTRAINT ck_self_check_findings_anchor_paragraph_shape
        CHECK ((anchor_kind <> 'PARAGRAPH')
               OR (paragraph_ref IS NOT NULL AND media_item_id IS NULL)),
    CONSTRAINT ck_self_check_findings_anchor_media_shape
        CHECK ((anchor_kind <> 'MEDIA')
               OR (media_item_id IS NOT NULL AND paragraph_ref IS NULL)),
    CONSTRAINT ck_self_check_findings_anchor_whole_shape
        CHECK ((anchor_kind <> 'WHOLE')
               OR (paragraph_ref IS NULL AND media_item_id IS NULL)),

    -- --- 内容边界 ---------------------------------------------------------
    CONSTRAINT ck_self_check_findings_message_not_blank
        CHECK (char_length(message) > 0),
    -- evidence 必须是 JSON **对象**：数组或标量会让"读 evidence.xxx"这件事
    -- 在不同 finding 上形状不同（前端只能逐条猜）。
    CONSTRAINT ck_self_check_findings_evidence_is_object
        CHECK (jsonb_typeof(evidence) = 'object'),

    -- --- 外键：全部 ON DELETE RESTRICT ------------------------------------
    CONSTRAINT fk_self_check_findings_rule_result_id_self_check_rule_results
        FOREIGN KEY (rule_result_id) REFERENCES self_check_rule_results (id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_self_check_findings_media_item_id_media_items
        FOREIGN KEY (media_item_id) REFERENCES media_items (id) ON DELETE RESTRICT,
    -- ⚠️ 跨包入向 FK（61/62 字节，实测在 63 上限内）。见第六节。
    CONSTRAINT fk_self_check_findings_feedback_item_id_content_feedback_items
        FOREIGN KEY (feedback_item_id) REFERENCES content_feedback_items (id)
        ON DELETE RESTRICT
);

CREATE INDEX ix_self_check_findings_rule_result
    ON self_check_findings (rule_result_id);
-- "这条 AI 反馈是哪一条自检发现产生的"（去重查询与排障的读路径）。
CREATE INDEX ix_self_check_findings_feedback_item
    ON self_check_findings (feedback_item_id);
CREATE INDEX ix_self_check_findings_media_item
    ON self_check_findings (media_item_id);
"""

# ---------------------------------------------------------------------------
# 五、触发器清单（五个）
# ---------------------------------------------------------------------------

TRIGGERS = (
    {
        "table": "self_check_runs",
        "name": "trg_self_check_runs_immutable",
        "timing": "BEFORE UPDATE OR DELETE, FOR EACH ROW",
        "function": "rednote_reject_mutation()",  # 跨包共用，downgrade 不删
        "why": (
            "自检结论是观测，不是工作区。需要新结论就再跑一轮、再追加一行；"
            "原地改会让『上周判通过、今天判阻断』无从解释。"
        ),
    },
    {
        "table": "self_check_rule_results",
        "name": "trg_self_check_rule_results_immutable",
        "timing": "BEFORE UPDATE OR DELETE, FOR EACH ROW",
        "function": "rednote_reject_mutation()",  # 跨包共用，downgrade 不删
        "why": "逐规则结论同上：它是『那一刻的规则对那一版稿子的判断』。",
    },
    {
        "table": "self_check_findings",
        "name": "trg_self_check_findings_immutable",
        "timing": "BEFORE UPDATE OR DELETE, FOR EACH ROW",
        "function": "rednote_reject_mutation()",  # 跨包共用，downgrade 不删
        "why": (
            "命中记录同上。⇒ 服务层必须**先建 AI 反馈、再插 finding**，"
            "让 feedback_item_id 在插入那一刻就是终值（回填就是对不可变行的"
            "UPDATE，会撞 23001）。"
        ),
    },
    {
        "table": "self_check_runs",
        "name": "trg_self_check_runs_consistency",
        "timing": (
            "AFTER INSERT, FOR EACH ROW, CONSTRAINT TRIGGER "
            "DEFERRABLE INITIALLY DEFERRED"
        ),
        "function": "rednote_check_self_check_run_consistency()",
        "why": (
            "两条判据：subject_version_id 指向的版本必须属于 NEW.content_item_id；"
            "content_item_id 指向的内容必须属于 NEW.project_id。"
            "FK 只保证被引用行存在 —— 不管它属于谁。前者错了，"
            "『这轮判的是哪一版』就有两个答案且 STALE 判定永远为真；"
            "后者错了，两步骤鉴权反查到的项目与真实归属不一致，"
            "看板会把别的项目的内容算进来。"
        ),
    },
    {
        "table": "self_check_findings",
        "name": "trg_self_check_findings_anchor_consistency",
        "timing": (
            "AFTER INSERT, FOR EACH ROW, CONSTRAINT TRIGGER "
            "DEFERRABLE INITIALLY DEFERRED"
        ),
        "function": "rednote_check_self_check_finding_anchor()",
        "why": (
            "两条判据，**都跨两跳 join**（finding → rule_result → run）："
            "media_item_id（可空）必须属于本轮的 content_item_id；"
            "feedback_item_id（可空）必须属于本轮的 content_item_id。"
            "后者错了，WP29 的审核闸拦的是**另一条内容** —— "
            "而那种错不会报错，只表现为『这条内容明明有阻断项却审过了』。"
        ),
    },
)

#: 本包**自己的**函数（downgrade 只删这两个）。`rednote_reject_mutation`
#: 是跨包共用函数，**不在此列** —— 删它会让所有已建表的不可变守卫
#: 静默失效（ISS-79）。
OWN_FUNCTIONS = (
    "rednote_check_self_check_run_consistency",
    "rednote_check_self_check_finding_anchor",
)

# ---------------------------------------------------------------------------
# 六、跨包入向 FK 与 WP29 测试夹具（**本包最隐蔽的一处耦合**）
# ---------------------------------------------------------------------------

CROSS_PACKAGE_INBOUND_FK_NOTE = """
`self_check_findings.feedback_item_id → content_feedback_items.id` 是本包
**唯一**的跨包入向 FK（即"本包的表引用别人的表"之外，再让别人的表被本包引用）。

它的后果已**实测**（`rednote_test_wp30`）：`conftest.drop_own_tables` 的算法是
"只删没有跨包入向 FK 的表"，于是 WP29 夹具里
`drop_own_tables(engine, ('content_reviews','content_feedback_events',
'content_feedback_items'))` 的返回从

    dropped = [content_reviews, content_feedback_events, content_feedback_items]
    kept    = []

变成

    dropped = ['content_reviews', 'content_feedback_events']
    kept    = ['content_feedback_items']

⇒ `content_feedback_items` 不再被 WP29 的夹具重建，改由 `create_all` 保证
存在性 —— 而 `create_all` 对"表已存在但结构是旧的"**静默跳过**。
将来 WP29 的表结构若变，它的测试会**跑在旧结构上且全绿**。

⚠️ 注意 `test_feedback_wp29.py` 里那条 `fks_after == fks_before` 断言
**抓不到这件事**（集合前后一致，都等于本包这一条 FK）。

## 处置（已选）

**加 FK + 显式实测 + 登记**（与 WP27/WP35/WP29 三次先例一致）：
1. 在 `test_content_wp26.py` 的 `_KNOWN_CROSS_PACKAGE_FKS` 同款位置登记
   `fk_self_check_findings_feedback_item_id_content_feedback_items`
   （**主 agent 动作**：那是别人包的测试文件，分支禁写）；
2. 同步 `docs/regression-register.md` 的 **RG-130**（跨包入向 FK 集合）；
3. 本包测试里有一条判据钉住这个**前提**
   （`test_cross_package_inbound_fk_on_feedback_items_exists`）：
   若将来有人把 FK 去掉改成只存 uuid，它会红并提醒同步修正文档。

**备选（未选）**：`feedback_item_id` 不加 FK、只存 uuid + 靠一致性触发器。
代价是失去引用完整性（一条 finding 可以指向一条被删掉的反馈），
收益是不动别人的夹具语义。本包选"加 FK"，因为引用完整性是审核闸那条链的
最后一道，而夹具语义的变化只要**显式登记**就不是静默失败。
"""

# ---------------------------------------------------------------------------
# 七、迁移核对清单
# ---------------------------------------------------------------------------

CHECKLIST = (
    "三张表的 CHECK 名带 ck_<table>_ 前缀（NAMING_CONVENTION），"
    "逐个核对取值域与 types.py 的 frozenset 一致（实测共 30 条 CHECK："
    "runs 14 / rule_results 10 / findings 6）。",
    "★ self_check_runs 的**四条 outcome 双向等价式**必须齐全。"
    "少一条就让『outcome=PASSED 但 blocking_count=1』这种组合落库，"
    "而读端点通常只看 outcome。",
    "★ rule_code 与 skip_reason **不得**有取值域 CHECK（开集）。"
    "迁移 docstring 必须写明理由：落 CHECK 后每加一条规则就要一次迁移，"
    "且化石与注册表的分叉不会报错（新规则的结果静默写不进去）。"
    "代偿在服务层 `validate_rule_result`（未知值 422，不落库）。",
    "self_check_rule_results 的四条**双向等值**配对 CHECK 必须齐全"
    "（severity / skip_reason / error_note / finding_count 各一条）。",
    "self_check_findings 的三条锚点 CHECK 必须齐全 —— 少一条就让"
    "『MEDIA 不带 mediaItemId』这类坏数据落库。",
    "evidence 的 jsonb_typeof = 'object' CHECK 必须存在。",
    "(run_id, rule_code) 唯一约束必须存在 —— 它是『一轮里一条规则只有一个结论』的最后一道。",
    "全部 FK 必须 ON DELETE RESTRICT（本包没有任何级联删除）。",
    "本包**没有**延迟外键（无自引用、无表级环）——"
    "若 autogenerate 生成了 DEFERRABLE 的 FK，那是生成器抄了别的包。",
    "五个触发器由 ddl.all_trigger_ddl() 建出：三个不可变复用"
    "rednote_reject_mutation()，两个一致性是 CONSTRAINT TRIGGER 且延迟。",
    "downgrade 只删 OWN_FUNCTIONS 这两个，**不删** rednote_reject_mutation。",
    "全部约束名在 63 字节内（最长实测 62 字节："
    "fk_self_check_findings_feedback_item_id_content_feedback_items）。",
    "跨包入向 FK 登记：test_content_wp26.py 的 _KNOWN_CROSS_PACKAGE_FKS"
    " + RG-130 同步（见第六节）。",
)
