"""WP33 DDL 草案：`quality_reports` / `quality_report_versions` / `quality_report_items` /
`quality_dimension_results` / `quality_similarity_pairs` / `quality_media_alignments` /
`quality_report_artifacts`。

**这不是迁移文件。** 迁移由主 agent 串行收口（接缝冻结 §4.2）；本包设计阶段
**不建 `modules/quality/`、不建 `models.py`、不连库**。本文件是**规格**：表名、列、
类型、可空、默认值、全部约束与索引、外键、触发器清单，逐条可核对。
体例同 `WP30-ddl-草案.py`：**本文件不 import 本包任何模块**，只含字符串常量与元组。

⚠️ **与 WP30 草案的一处诚实差别**：WP30 草案写成时 `models.py` 已存在，并由验证库
（`rednote_test_wp30`）实测导出核对过。本草案写成时**连 `models.py` 都不存在**，
`rednote_test_wp33` 在本机也不存在 ⇒ 下方 DDL **没有被任何数据库执行过**，
仅对照了既有表（`content_items` / `content_versions` / `media_items` / `version_nodes`）的
列名、命名与约束惯例手写。实现落地后必须：
① 用 `models.py` 生成 DDL 并与本文件逐条比对；② 在验证库上 `pg_constraint` /
`pg_indexes` / `pg_trigger` 三项导出核对；③ 再更新本文件（与 WP22 草案第七节同款）。

---

## 一、文件怎么用

1. 主 agent 用实现期的 `models.py` 生成迁移（`alembic revision --autogenerate`），
   然后用本文件**逐条核对**（G2 三件事：删探针表 drop / 重写 downgrade / 补 postgresql 导入）。
2. 迁移必须调 `ddl.all_trigger_ddl()` 把触发器一并建出（见第五节：6 个不可变 + 7 个一致性）。
3. **升级与降级都只碰本模块自己的对象**：降级只删 `OWN_FUNCTIONS` 列出的 7 个函数，
   **不含**跨包共用的 `rednote_reject_mutation`（ISS-79）。
4. **取值域全部写成字面量**（ISS-191）：迁移里**不得**
   `from rednote.modules.quality.types import ...` 再循环它。本包引入 8 个新的**闭集**取值域
   （`scope_kind` / `outcome` / `dimension` / 维度 `status` / `verdict` / 篇目 `status` /
   对位 `verdict` / 产物 `format`），在 `check_migration_value_domain_no_live_import.py` 射程内。
5. ⚠️ **`not_computed_reason` / `exclude_reason` / 对位 `reason` 刻意没有取值域 CHECK**
   （开集；与 WP30 `skip_reason` 同一取舍），迁移 docstring 必须写明理由，
   否则下一个人会"顺手补上"而把开集冻成化石。借用他人词汇的 `media_state` 同理**不落 CHECK**。
6. 同步点：`migrations/env.py` import quality models；`main.py` include_router；
   迁移序接在 WP32 之后（接缝冻结 §4.2：…→ WP31 → WP32 → **WP33** → WP34）。
7. 约束命名遵循 `platform/base.py::NAMING_CONVENTION`
   （`ck_<table>_<name>` / `fk_<table>_<col0_N>_<referred>` / `uq_` / `ix_` / `pk_`）。
   **本包的复合外键（pairs×3 / alignments×1）与 `quality_report_items.generation_context_id`
   的 FK 必须在 `models.py` 里显式 `name=`**，否则按约定生成的名字（`column_0_N_name` 是全部列名拼接，
   后者约定名 65 字节）会超过 PostgreSQL 的 63 字节上限而被**静默截断**，ORM 元数据与
   `pg_constraint` 的名字永久不一致。本草案里所有约束名均已脚本实算 ≤ 63 字节（见 `CHECKLIST`）。

## 二、七张表的角色与可变性

| 表 | 角色 | 可变性 | 触发器 |
|---|---|---|---|
| `quality_reports` | 系列身份（项目 × 范围）+ `current_version_id` 投影 | **可变**（只有投影列 / `updated_at` / `lock_version` 会变） | `trg_quality_reports_current_version`（约束触发器，延迟） |
| `quality_report_versions` | 一次计算 = 一个版本（id 同时 FK `version_nodes.id`） | **不可变** | `..._immutable` + `trg_quality_report_versions_consistency`（延迟，含聚合计数核对） |
| `quality_report_items` | 逐篇：纳入/排除 + 钉死的内容版本 + 可追溯上下文 | **不可变** | `..._immutable` + `trg_quality_report_items_consistency` |
| `quality_dimension_results` | 逐维度（每版本恰 7 行） | **不可变** | `..._immutable` + `trg_quality_dimension_results_flagged`（延迟） |
| `quality_similarity_pairs` | 命中相似对 | **不可变** | `..._immutable` + `trg_quality_similarity_pairs_consistency` |
| `quality_media_alignments` | 顺序对位逐槽位结果 | **不可变** | `..._immutable` + `trg_quality_media_alignments_consistency` |
| `quality_report_artifacts` | 导出产物 ⇒ `file_objects`（`PURPOSE_GENERATED`） | **不可变** | `..._immutable` |
| （共用表 `version_nodes`） | REPORT 节点必须配对版本行（OP-88 同款） | — | `trg_version_nodes_report_paired`（延迟，`WHEN entity_type='REPORT'`） |

**为什么 6 张不可变、1 张可变**：报告是**观测**，不是工作区（同 WP30 §二）。
要新结论就再算一版、追加一行；原地改会让"上周报告说没重复、今天说有"无从解释
（也无从区分"稿子变了"与"算法变了"）。唯一可变的 `quality_reports` 只承载
"当前版本是哪一个"这一个指针（契约 §2：投影优先，**禁 `ORDER BY revision_no DESC`**），
与 `content_items.current_version_id` 同款。

⇒ 本包**零 `If-Match`**（`lock_version` 只服务投影推进的 CAS，不暴露为 ETag）。

## 三、建表顺序与拆环

```
projects / users / content_items / content_versions / generation_contexts /
production_baselines / media_items / file_objects / version_nodes     （全部已存在）
        ↑
quality_reports ──(current_version_id，延迟 FK，建完 versions 后 ALTER 补)──┐
        ↑ (report_id)                                                      │
quality_report_versions  ←─────────────────────────────────────────────────┘
        ↑ (report_version_id)
   ├─ quality_report_items            UNIQUE(report_version_id, content_item_id)  ← 复合 FK 的被引用端
   ├─ quality_dimension_results       UNIQUE(report_version_id, dimension)        ← 复合 FK 的被引用端
   │        ↑
   ├─ quality_similarity_pairs        → items(×2) + dimension_results
   ├─ quality_media_alignments        → items
   └─ quality_report_artifacts        → file_objects
```

**建表顺序**：`quality_reports`（先不带 current_version FK）→ `quality_report_versions`
→ `ALTER TABLE quality_reports ADD CONSTRAINT ... DEFERRABLE INITIALLY DEFERRED`
→ `quality_report_items` → `quality_dimension_results` → `quality_similarity_pairs`
→ `quality_media_alignments` → `quality_report_artifacts`。

**唯一的表级环**是 `quality_reports.current_version_id` ⇄ `quality_report_versions.report_id`，
用延迟外键拆环（与 `content_items.current_version_id` 同款）。
`quality_report_versions.id → version_nodes.id` 也是延迟 FK（注册表与领域表同事务插入）。

⚠️ **本包对 WP30 / WP31 / WP32 的表零外键**（接缝冻结 §4.1：外键只指向已存在的表；
并避开 WP30 R8——跨包入向 FK 会改变他包测试夹具的 drop/keep 语义）。
D2（2026-10-19）之后，本包对 WP32 的 `batches` / `batch_items` 有**只读 SQL 依赖**
（`scope_kind='BATCH'` 的成员取自 `batch_items`），`quality_reports.batch_id` 是**无 FK 的审计指针**
——选只读关联而非引用外键的理由见说明文档 §3.1；迁移序因此必须在 WP32 之后。
"事实一致性 / 自检汇总"因此**不进快照**（说明文档 Q-07）。

## 四、七张表的逐列 DDL（specs，不是可执行代码）

### 4.1 `quality_reports`

`UNIQUE(project_id, scope_digest)` 是系列身份。`scope_digest` = sha256 十六进制：
`CONTENT_SET` 取排序后 `contentIds` 的规范化串；`PROJECT` 取固定前缀 + `project_id`；
`BATCH`（D2）取固定前缀 + `batch_id`。
"当前版本是哪一个"只由 `current_version_id` 回答；`(current_version_id IS NULL) =
(current_revision_no = 0)` 钉住投影与修订号不漂移（`content_items.current_version_no` 同款，
0 而非 NULL 的理由同其 docstring）。
"""

QUALITY_REPORTS_DDL = """
-- 表名：quality_reports。报告系列身份。**可变**（仅投影列）。
CREATE TABLE quality_reports (
    id                  uuid         NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid         NOT NULL,
    scope_kind          varchar(16)  NOT NULL,   -- CONTENT_SET / PROJECT / BATCH（字面量化石）
    scope_digest        varchar(64)  NOT NULL,   -- sha256 hex，见上
    batch_id            uuid,                    -- 仅 BATCH 范围非空；指向 WP32 batches.id，**无 FK**（D2，见说明文档 §3.1）
    current_version_id  uuid,                    -- 投影；NULL = 尚无任何版本（合法的中间态）
    current_revision_no integer      NOT NULL DEFAULT 0,
    created_by          uuid,
    created_at          timestamptz  NOT NULL DEFAULT now(),
    updated_at          timestamptz  NOT NULL DEFAULT now(),
    lock_version        integer      NOT NULL DEFAULT 1,

    CONSTRAINT pk_quality_reports PRIMARY KEY (id),

    CONSTRAINT ck_quality_reports_scope_kind_valid
        CHECK (scope_kind IN ('CONTENT_SET', 'PROJECT', 'BATCH')),
    CONSTRAINT ck_quality_reports_batch_id_paired
        CHECK ((scope_kind = 'BATCH') = (batch_id IS NOT NULL)),
    CONSTRAINT ck_quality_reports_scope_digest_length
        CHECK (char_length(scope_digest) = 64),
    CONSTRAINT ck_quality_reports_revision_no_non_negative
        CHECK (current_revision_no >= 0),
    CONSTRAINT ck_quality_reports_projection_paired
        CHECK ((current_version_id IS NULL) = (current_revision_no = 0)),
    CONSTRAINT ck_quality_reports_lock_version_positive
        CHECK (lock_version >= 1),

    CONSTRAINT uq_quality_reports_project_id_scope_digest
        UNIQUE (project_id, scope_digest),

    CONSTRAINT fk_quality_reports_project_id_projects
        FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_quality_reports_created_by_users
        FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT
    -- current_version_id 的 FK 见下方 ALTER（拆环）
);

-- 项目下系列列表（游标分页：updated_at 倒序 + id 第二排序键）。
CREATE INDEX ix_quality_reports_project_id_updated_at
    ON quality_reports (project_id, updated_at, id);
"""

QUALITY_REPORTS_CURRENT_VERSION_FK_DDL = """
-- 在 quality_report_versions 建好之后补；延迟 + RESTRICT（与 content_items 同款）。
-- 名字 60 字节，在 63 上限内（实算）。
ALTER TABLE quality_reports
    ADD CONSTRAINT fk_quality_reports_current_version_id_quality_report_versions
    FOREIGN KEY (current_version_id) REFERENCES quality_report_versions (id)
    ON DELETE RESTRICT
    DEFERRABLE INITIALLY DEFERRED;
"""

"""
### 4.2 `quality_report_versions`

**汇总计数不是自由字段**：`ok + alert + not_computed = 7`（维度恒 7 个）、
`item_count = included + excluded`、`outcome` 与 `alert_dimension_count` 双向等价。
`outcome` 刻意叫 `ALERTED / NO_ALERT` 而**不叫 PASSED**：3 个维度恒 NOT_COMPUTED 的
报告若叫 PASSED，就会被读成"全部通过"（WP30 R2 同款）。`not_computed_dimension_count`
**不参与** outcome，但并列单独给出。

**聚合计数与明细表的一致性**（`ok/alert/not_computed` 须等于 7 个维度行的实际分布、
`included/excluded` 须等于 items 行的实际分布、维度行恰 7 条）无法用 CHECK 表达（跨表），
由 `trg_quality_report_versions_consistency`（延迟约束触发器，提交时所有子表行都已就位）核对。

**`input_fingerprint` 不加唯一约束**：输入 A → B → 回到 A 时应允许产生第 3 个版本
（新的时间点观测）。幂等判据是"与**当前**版本指纹比较"，在服务层、持行锁时做，
不是数据库唯一性。
"""

QUALITY_REPORT_VERSIONS_DDL = """
-- 表名：quality_report_versions。一次计算 = 一个 REPORT 版本。**不可变**。
-- id 同时 FK version_nodes.id（延迟）：注册表与本表同一事务插入（接缝冻结 §5.2）。
CREATE TABLE quality_report_versions (
    id                           uuid         NOT NULL,   -- = register_version 返回的 node_id
    report_id                    uuid         NOT NULL,
    revision_no                  integer      NOT NULL,   -- 显式传给 register_version（OP-91）
    input_fingerprint            varchar(64)  NOT NULL,   -- sha256：各篇(内容id,版本id,媒体序列)+algo+阈值
    algo_version                 integer      NOT NULL,   -- types.ALGO_VERSION 的落库快照
    schema_version               integer      NOT NULL,
    item_count                   integer      NOT NULL,
    included_item_count          integer      NOT NULL,
    excluded_item_count          integer      NOT NULL,
    ok_dimension_count           integer      NOT NULL,
    alert_dimension_count        integer      NOT NULL,
    not_computed_dimension_count integer      NOT NULL,   -- 不参与 outcome，并列单给
    outcome                      varchar(16)  NOT NULL,   -- ALERTED / NO_ALERT
    payload                      jsonb        NOT NULL,   -- 摘要快照（无时间戳/随机量）
    payload_hash                 varchar(64)  NOT NULL,   -- = version_nodes.payload_hash
    started_at                   timestamptz  NOT NULL,
    finished_at                  timestamptz  NOT NULL,
    created_by                   uuid,
    created_at                   timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT pk_quality_report_versions PRIMARY KEY (id),

    CONSTRAINT ck_quality_report_versions_outcome_valid
        CHECK (outcome IN ('ALERTED', 'NO_ALERT')),
    CONSTRAINT ck_quality_report_versions_revision_no_positive
        CHECK (revision_no >= 1),
    CONSTRAINT ck_quality_report_versions_algo_version_positive
        CHECK (algo_version >= 1),
    CONSTRAINT ck_quality_report_versions_schema_version_positive
        CHECK (schema_version >= 1),
    CONSTRAINT ck_quality_report_versions_fingerprint_length
        CHECK (char_length(input_fingerprint) = 64),
    CONSTRAINT ck_quality_report_versions_payload_hash_length
        CHECK (char_length(payload_hash) = 64),
    CONSTRAINT ck_quality_report_versions_payload_is_object
        CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT ck_quality_report_versions_item_counts_non_negative
        CHECK (included_item_count >= 0 AND excluded_item_count >= 0),
    CONSTRAINT ck_quality_report_versions_item_count_sum
        CHECK (item_count = included_item_count + excluded_item_count),
    -- 维度恒 7 个（字面量 7：加维度必然同时改维度 CHECK，即一次迁移）。
    CONSTRAINT ck_quality_report_versions_dimension_counts_non_negative
        CHECK (ok_dimension_count >= 0 AND alert_dimension_count >= 0
               AND not_computed_dimension_count >= 0),
    CONSTRAINT ck_quality_report_versions_dimension_count_sum
        CHECK (ok_dimension_count + alert_dimension_count
               + not_computed_dimension_count = 7),
    -- ★ outcome 双向等价式（not_computed 不参与：未算既不是通过也不是告警）。
    CONSTRAINT ck_quality_report_versions_outcome_alerted_equivalence
        CHECK ((outcome = 'ALERTED') = (alert_dimension_count > 0)),
    CONSTRAINT ck_quality_report_versions_finished_not_before_started
        CHECK (finished_at >= started_at),

    CONSTRAINT uq_quality_report_versions_report_id_revision_no
        UNIQUE (report_id, revision_no),

    CONSTRAINT fk_quality_report_versions_id_version_nodes
        FOREIGN KEY (id) REFERENCES version_nodes (id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_quality_report_versions_report_id_quality_reports
        FOREIGN KEY (report_id) REFERENCES quality_reports (id) ON DELETE RESTRICT,
    CONSTRAINT fk_quality_report_versions_created_by_users
        FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT
);

-- 系列下版本历史（修订号倒序由查询给；id 第二排序键）。
CREATE INDEX ix_quality_report_versions_report_id_revision_no
    ON quality_report_versions (report_id, revision_no, id);
"""

"""
### 4.3 `quality_report_items`

逐篇一行，**钉死**报告基于哪一版稿子（契约 §2 口径取到的当前版本，之后不再变）。
`EXCLUDED` 的篇目**也要有行**：否则"3 篇被排除"与"批里本来只有 N-3 篇"在数据上同形。

可追溯性（接缝冻结 §5.3 把 `generation_contexts` 列为 WP33 读对象）：
`generation_context_id` / `baseline_id` 取自被钉死的版本；**NULL = 该版本没有生成上下文**
（与 0 / 空串区分）。`baseline_id` 只存 id、不复制基线内容。

`paragraph_count`：`INCLUDED` 必为 ≥1；`EXCLUDED(EMPTY_BODY)` 为 **0**（算了，确实零段）；
`EXCLUDED(NO_VERSION)` 为 **NULL**（没有版本，根本没法算）——这是 NULL / 0 分离在本表的落点。
"""

QUALITY_REPORT_ITEMS_DDL = """
-- 表名：quality_report_items。报告的逐篇成员。**不可变**。
CREATE TABLE quality_report_items (
    id                    uuid        NOT NULL DEFAULT gen_random_uuid(),
    report_version_id     uuid        NOT NULL,
    content_item_id       uuid        NOT NULL,
    content_version_id    uuid,                    -- NULL ⇔ 该篇没有任何版本（NO_VERSION）
    position              integer     NOT NULL,    -- 报告内稳定序（读排序用它，不用 created_at）
    status                varchar(16) NOT NULL,    -- INCLUDED / EXCLUDED
    exclude_reason        varchar(48),             -- 开集（不落 CHECK）：NO_VERSION / EMPTY_BODY
    paragraph_count       integer,                 -- NULL=无版本；0=空正文；INCLUDED 必 >=1
    generation_context_id uuid,                    -- NULL = 该版本无生成上下文
    baseline_id           uuid,                    -- 取自 generation_contexts.baseline_id
    created_at            timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pk_quality_report_items PRIMARY KEY (id),

    CONSTRAINT ck_quality_report_items_status_valid
        CHECK (status IN ('EXCLUDED', 'INCLUDED')),
    CONSTRAINT ck_quality_report_items_position_non_negative
        CHECK (position >= 0),
    CONSTRAINT ck_quality_report_items_paragraph_count_non_negative
        CHECK (paragraph_count IS NULL OR paragraph_count >= 0),
    -- 双向配对：EXCLUDED ⇔ 有排除原因（没有『被排除但不说为什么』）。
    CONSTRAINT ck_quality_report_items_exclude_reason_paired
        CHECK ((status = 'EXCLUDED') = (exclude_reason IS NOT NULL)),
    -- INCLUDED 必有版本、必有至少一段（否则它没有任何可比文本）。
    CONSTRAINT ck_quality_report_items_included_has_subject
        CHECK (status <> 'INCLUDED'
               OR (content_version_id IS NOT NULL
                   AND paragraph_count IS NOT NULL AND paragraph_count >= 1)),

    CONSTRAINT uq_quality_report_items_version_content
        UNIQUE (report_version_id, content_item_id),
    CONSTRAINT uq_quality_report_items_version_position
        UNIQUE (report_version_id, position),

    CONSTRAINT fk_quality_report_items_report_version_id_versions
        FOREIGN KEY (report_version_id) REFERENCES quality_report_versions (id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_quality_report_items_content_item_id_content_items
        FOREIGN KEY (content_item_id) REFERENCES content_items (id) ON DELETE RESTRICT,
    CONSTRAINT fk_quality_report_items_content_version_id_content_versions
        FOREIGN KEY (content_version_id) REFERENCES content_versions (id)
        ON DELETE RESTRICT,
    -- 约定名 fk_quality_report_items_generation_context_id_generation_contexts 为 65 字节，
    -- 超 63 上限 ⇒ 本条必须在 models.py 里显式 name=（下为实算 ≤63 的短名）。
    CONSTRAINT fk_quality_report_items_generation_context_id
        FOREIGN KEY (generation_context_id) REFERENCES generation_contexts (id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_quality_report_items_baseline_id_production_baselines
        FOREIGN KEY (baseline_id) REFERENCES production_baselines (id)
        ON DELETE RESTRICT
);

-- 反查"这篇内容出现在哪些报告里"（STALE 判定 / WP34 影响图）。
CREATE INDEX ix_quality_report_items_content_item_id
    ON quality_report_items (content_item_id);
"""

"""
### 4.4 `quality_dimension_results`

**本包最重要的约束组**：NULL / 0 / 约数 三者分离的数据库落点。

| 状态 | 列的形态 |
|---|---|
| `COMPUTED` | `compared_item_count` / `flagged_item_count` / 两个阈值 / `max_similarity_permille` / `verdict` **全部非 NULL**；`not_computed_reason` 为 NULL；`compared >= 2` |
| `NOT_COMPUTED` | 上述**全部 NULL**；`not_computed_reason` 非 NULL |

`verdict` 的判据下推数据库：`ALERT ⇔ flagged*1000 > alert_threshold*compared`（整数交叉相乘，
无浮点，**严格大于**对应原型"超阈值"）。`flagged_item_count <> 1`：一个相似对涉及两篇，
所以"恰 1 篇被标记"不可能。

**三个恒 NOT_COMPUTED 的维度**（`STRUCTURE` / `IMAGE_SIMILARITY` / `SCENE_CONCENTRATION`）
在 `algo_version=1` 下被 CHECK 钉成必须 `NOT_COMPUTED`：防止有人在服务层随手给它们填一个
伪造的 COMPUTED。**代价**：将来真能算时必须走一次迁移——这是有意的（改变"这个维度算不算"
本来就是需要显式拍板的事，说明文档 §4.3）。

**不存浮点**：相似度/阈值全是整数千分比；占比只存分子分母，展示值由读端算并标 `approximate`。
"""

QUALITY_DIMENSION_RESULTS_DDL = """
-- 表名：quality_dimension_results。每个报告版本恰 7 行（每维度一行）。**不可变**。
CREATE TABLE quality_dimension_results (
    id                       uuid        NOT NULL DEFAULT gen_random_uuid(),
    report_version_id        uuid        NOT NULL,
    dimension                varchar(24) NOT NULL,
    status                   varchar(16) NOT NULL,   -- COMPUTED / NOT_COMPUTED
    not_computed_reason      varchar(48),            -- 开集（不落 CHECK）
    verdict                  varchar(8),             -- OK / ALERT；NOT_COMPUTED 时 NULL
    compared_item_count      integer,                -- 本维度参与比较的篇数
    flagged_item_count       integer,                -- 至少在一个相似对里的篇数
    pair_threshold_permille  integer,                -- 相似对阈值快照
    alert_threshold_permille integer,                -- 维度告警阈值快照
    max_similarity_permille  integer,                -- 全部点对最大值（信息字段，floor）
    created_at               timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pk_quality_dimension_results PRIMARY KEY (id),

    CONSTRAINT ck_quality_dimension_results_dimension_valid
        CHECK (dimension IN ('BODY', 'ENDING', 'IMAGE_SIMILARITY', 'OPENING',
                             'SCENE_CONCENTRATION', 'STRUCTURE', 'TITLE')),
    CONSTRAINT ck_quality_dimension_results_status_valid
        CHECK (status IN ('COMPUTED', 'NOT_COMPUTED')),
    CONSTRAINT ck_quality_dimension_results_verdict_valid
        CHECK (verdict IS NULL OR verdict IN ('ALERT', 'OK')),

    -- ★ NULL/0 分离：COMPUTED ⇔ 数值列与 verdict 同时存在；NOT_COMPUTED ⇔ 同时缺席。
    CONSTRAINT ck_quality_dimension_results_computed_columns_present
        CHECK ((status = 'COMPUTED') = (
               compared_item_count IS NOT NULL AND flagged_item_count IS NOT NULL
               AND pair_threshold_permille IS NOT NULL
               AND alert_threshold_permille IS NOT NULL
               AND max_similarity_permille IS NOT NULL AND verdict IS NOT NULL)),
    CONSTRAINT ck_quality_dimension_results_not_computed_columns_absent
        CHECK ((status = 'NOT_COMPUTED') = (
               compared_item_count IS NULL AND flagged_item_count IS NULL
               AND pair_threshold_permille IS NULL
               AND alert_threshold_permille IS NULL
               AND max_similarity_permille IS NULL AND verdict IS NULL)),
    CONSTRAINT ck_quality_dimension_results_reason_paired
        CHECK ((status = 'NOT_COMPUTED') = (not_computed_reason IS NOT NULL)),

    -- 比较至少 2 篇才有意义（<2 篇一律 NOT_COMPUTED，不是 OK，更不是 0）。
    CONSTRAINT ck_quality_dimension_results_compared_at_least_two
        CHECK (compared_item_count IS NULL OR compared_item_count >= 2),
    CONSTRAINT ck_quality_dimension_results_flagged_range
        CHECK (flagged_item_count IS NULL
               OR (flagged_item_count >= 0
                   AND flagged_item_count <= compared_item_count)),
    -- 一个相似对涉及两篇 ⇒ 被标记篇数不可能恰为 1。
    CONSTRAINT ck_quality_dimension_results_flagged_not_one
        CHECK (flagged_item_count IS NULL OR flagged_item_count <> 1),
    CONSTRAINT ck_quality_dimension_results_thresholds_range
        CHECK ((pair_threshold_permille IS NULL
                OR pair_threshold_permille BETWEEN 0 AND 1000)
               AND (alert_threshold_permille IS NULL
                    OR alert_threshold_permille BETWEEN 0 AND 1000)),
    CONSTRAINT ck_quality_dimension_results_max_similarity_range
        CHECK (max_similarity_permille IS NULL
               OR max_similarity_permille BETWEEN 0 AND 1000),
    -- ★ verdict 双向等价式（整数交叉相乘；严格大于）。
    CONSTRAINT ck_quality_dimension_results_verdict_alert_equivalence
        CHECK (verdict IS NULL
               OR ((verdict = 'ALERT')
                   = (flagged_item_count * 1000
                      > alert_threshold_permille * compared_item_count))),
    -- algo_version=1：三个未实现维度不得出现 COMPUTED（防伪造；将来放开需走迁移）。
    CONSTRAINT ck_quality_dimension_results_unimplemented_not_computed
        CHECK (dimension NOT IN ('IMAGE_SIMILARITY', 'SCENE_CONCENTRATION', 'STRUCTURE')
               OR status = 'NOT_COMPUTED'),

    CONSTRAINT uq_quality_dimension_results_version_dimension
        UNIQUE (report_version_id, dimension),

    CONSTRAINT fk_quality_dimension_results_report_version_id_versions
        FOREIGN KEY (report_version_id) REFERENCES quality_report_versions (id)
        ON DELETE RESTRICT
);
"""

"""
### 4.5 `quality_similarity_pairs`

只存**命中**（`similarity >= pair_threshold`）的相似对。"某维度无 pair 行"**仅当**该维度行
`COMPUTED` 时才可解读为"比过且都低于阈值"；`NOT_COMPUTED` 维度**不得**有 pair 行
（触发器钉住），否则"没算"与"没命中"同形。

`(intersection_count, union_count)` 精确落库，`similarity_permille` 由它们**派生**并被 CHECK
钉住（`= intersection*1000 / union`，整数除法即 floor）——"派生字段双声称"（WP30 R6）的
数据库层消除。`content_item_a_id < content_item_b_id` 规范化（uuid 可比较）使 (A,B) 与 (B,A)
不会成为两行。两个复合 FK 保证两篇都是**本报告版本**的成员。
"""

QUALITY_SIMILARITY_PAIRS_DDL = """
-- 表名：quality_similarity_pairs。命中的相似对。**不可变**。
CREATE TABLE quality_similarity_pairs (
    id                 uuid        NOT NULL DEFAULT gen_random_uuid(),
    report_version_id  uuid        NOT NULL,
    dimension          varchar(24) NOT NULL,   -- 仅 4 个文本维度
    content_item_a_id  uuid        NOT NULL,
    content_item_b_id  uuid        NOT NULL,
    intersection_count integer     NOT NULL,   -- |A ∩ B|（规范化后 2-gram 集合）
    union_count        integer     NOT NULL,   -- |A ∪ B|
    similarity_permille integer    NOT NULL,   -- floor(1000*∩/∪)，由上两列派生
    created_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pk_quality_similarity_pairs PRIMARY KEY (id),

    CONSTRAINT ck_quality_similarity_pairs_dimension_is_text
        CHECK (dimension IN ('BODY', 'ENDING', 'OPENING', 'TITLE')),
    CONSTRAINT ck_quality_similarity_pairs_ordered
        CHECK (content_item_a_id < content_item_b_id),
    CONSTRAINT ck_quality_similarity_pairs_counts_valid
        CHECK (union_count >= 1 AND intersection_count >= 0
               AND intersection_count <= union_count),
    -- ★ 派生字段不得与来源漂移（整数除法 = floor）。
    CONSTRAINT ck_quality_similarity_pairs_permille_derived
        CHECK (similarity_permille = (intersection_count * 1000) / union_count),

    CONSTRAINT uq_quality_similarity_pairs_version_dimension_pair
        UNIQUE (report_version_id, dimension, content_item_a_id, content_item_b_id),

    -- 复合 FK：两篇都必须是本报告版本的成员；维度行必须存在。
    -- 这三条名字必须在 models.py 里显式 name=（约定名会超 63 字节）。
    CONSTRAINT fk_quality_similarity_pairs_item_a
        FOREIGN KEY (report_version_id, content_item_a_id)
        REFERENCES quality_report_items (report_version_id, content_item_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_quality_similarity_pairs_item_b
        FOREIGN KEY (report_version_id, content_item_b_id)
        REFERENCES quality_report_items (report_version_id, content_item_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_quality_similarity_pairs_dimension_result
        FOREIGN KEY (report_version_id, dimension)
        REFERENCES quality_dimension_results (report_version_id, dimension)
        ON DELETE RESTRICT
);

-- "这一维度下某篇和谁像"（报告详情下钻）。
CREATE INDEX ix_quality_similarity_pairs_version_dimension_permille
    ON quality_similarity_pairs (report_version_id, dimension, similarity_permille, id);
CREATE INDEX ix_quality_similarity_pairs_version_item_b
    ON quality_similarity_pairs (report_version_id, content_item_b_id);
"""

"""
### 4.6 `quality_media_alignments`

顺序对位（契约 §4，老大 2026-10-08 拍板）：第 i 张配第 i 段，段数不足时回落最后一段。
期望 ref 由 `media.schemas.assign_paragraph_refs`（纯函数，**经 bridge 转发，本包不复刻规则**）
给出；本表记录"槽位 i 的期望 ref vs `media_items` 里实际的 ref"。

- **不得断言 ref 唯一或一一对应**（契约 §4：多张落同一段是合法的）：本表**没有**
  `UNIQUE(report_version_id, content_item_id, expected_ref)`。
- 四种 verdict 的双向等价式见下；`MISSING` ⇔ 槽位无 `media_items` 行；
  `NOT_EVALUABLE` ⇔ 期望 ref 算不出（零段正文，fail-closed `PARAGRAPH_REF_UNKNOWN`），
  仅出现在 `EXCLUDED(EMPTY_BODY)` 且仍有图的篇目上（由触发器核对）。
- `media_state` 是 `media_items.state` 的**快照**（该表可变）；**不落 CHECK**（借用他人词汇）。
- 与 WP30 `MEDIA_PARAGRAPH_REF_STALE`（集合成员判定）的区别见说明文档 §6：两者结论可以不同且都对。
- `paragraph_ref` 长度上限 200（`media/types.py::MAX_PARAGRAPH_REF_CHARS`）。
"""

QUALITY_MEDIA_ALIGNMENTS_DDL = """
-- 表名：quality_media_alignments。顺序对位的逐槽位结果。**不可变**。
CREATE TABLE quality_media_alignments (
    id                 uuid         NOT NULL DEFAULT gen_random_uuid(),
    report_version_id  uuid         NOT NULL,
    content_item_id    uuid         NOT NULL,
    sort_order         integer      NOT NULL,   -- 槽位（图在内容内的顺序，0 起）
    media_item_id      uuid,                    -- NULL ⇔ MISSING（该槽位没有图）
    media_state        varchar(20),             -- media_items.state 快照；NULL ⇔ 无图位
    expected_ref       varchar(200),            -- 顺序对位期望；NULL ⇔ NOT_EVALUABLE
    actual_ref         varchar(200),            -- media_items.paragraph_ref；NULL ⇔ 无图位
    verdict            varchar(16)  NOT NULL,   -- ALIGNED / MISALIGNED / MISSING / NOT_EVALUABLE
    reason             varchar(48),             -- 开集；NOT_EVALUABLE 时必填（PARAGRAPH_REF_UNKNOWN）
    created_at         timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT pk_quality_media_alignments PRIMARY KEY (id),

    CONSTRAINT ck_quality_media_alignments_verdict_valid
        CHECK (verdict IN ('ALIGNED', 'MISALIGNED', 'MISSING', 'NOT_EVALUABLE')),
    CONSTRAINT ck_quality_media_alignments_sort_order_non_negative
        CHECK (sort_order >= 0),
    -- 期望与图位不能同时缺席（那一行没有任何可记录的事实）。
    CONSTRAINT ck_quality_media_alignments_some_fact_present
        CHECK (expected_ref IS NOT NULL OR media_item_id IS NOT NULL),
    -- ★ 四条 verdict 双向等价式。
    CONSTRAINT ck_quality_media_alignments_missing_equivalence
        CHECK ((verdict = 'MISSING') = (media_item_id IS NULL)),
    CONSTRAINT ck_quality_media_alignments_not_evaluable_equivalence
        CHECK ((verdict = 'NOT_EVALUABLE') = (expected_ref IS NULL)),
    CONSTRAINT ck_quality_media_alignments_aligned_equivalence
        CHECK ((verdict = 'ALIGNED')
               = (expected_ref IS NOT NULL AND actual_ref IS NOT NULL
                  AND expected_ref = actual_ref)),
    CONSTRAINT ck_quality_media_alignments_misaligned_equivalence
        CHECK ((verdict = 'MISALIGNED')
               = (expected_ref IS NOT NULL AND actual_ref IS NOT NULL
                  AND expected_ref <> actual_ref)),
    -- 图位三联一致：有图 ⇔ 有 state ⇔ 有 actual_ref。
    CONSTRAINT ck_quality_media_alignments_media_columns_paired
        CHECK ((media_item_id IS NULL) = (media_state IS NULL)
               AND (media_item_id IS NULL) = (actual_ref IS NULL)),
    CONSTRAINT ck_quality_media_alignments_reason_paired
        CHECK ((verdict = 'NOT_EVALUABLE') = (reason IS NOT NULL)),

    CONSTRAINT uq_quality_media_alignments_version_content_slot
        UNIQUE (report_version_id, content_item_id, sort_order),
    -- 同一版本里一个媒体位只出现一次（NULL 互不冲突）。
    CONSTRAINT uq_quality_media_alignments_version_media_item
        UNIQUE (report_version_id, media_item_id),

    CONSTRAINT fk_quality_media_alignments_item
        FOREIGN KEY (report_version_id, content_item_id)
        REFERENCES quality_report_items (report_version_id, content_item_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_quality_media_alignments_media_item_id_media_items
        FOREIGN KEY (media_item_id) REFERENCES media_items (id) ON DELETE RESTRICT
);
"""

"""
### 4.7 `quality_report_artifacts`

导出产物（接缝冻结 §5.10：报告产物用 `PURPOSE_GENERATED`，**不占研究配额**）。
`files.service.store_generated_asset` **每次调用都新建 `file_objects` 行，自身不幂等**，
所以幂等判据在本表：`UNIQUE(report_version_id, format)`——服务层先查再建，并发由
`quality_reports` 行锁串行；残余孤儿 `file_objects`（同 blob 无引用）是已登记的已知取舍。
是否 MVP 就落产物见说明文档 Q-08（可降级为只返回 JSON，则本表与端点⑥一并删去）。
"""

QUALITY_REPORT_ARTIFACTS_DDL = """
-- 表名：quality_report_artifacts。报告导出产物。**不可变**。
CREATE TABLE quality_report_artifacts (
    id                uuid         NOT NULL DEFAULT gen_random_uuid(),
    report_version_id uuid         NOT NULL,
    format            varchar(16)  NOT NULL,   -- 目前仅 JSON（格式待 Q-08）
    file_object_id    uuid         NOT NULL,
    content_sha256    varchar(64)  NOT NULL,   -- 产物字节摘要（可复验）
    byte_size         bigint       NOT NULL,
    created_by        uuid,
    created_at        timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT pk_quality_report_artifacts PRIMARY KEY (id),

    CONSTRAINT ck_quality_report_artifacts_format_valid
        CHECK (format IN ('JSON')),
    CONSTRAINT ck_quality_report_artifacts_sha256_length
        CHECK (char_length(content_sha256) = 64),
    CONSTRAINT ck_quality_report_artifacts_byte_size_non_negative
        CHECK (byte_size >= 0),

    CONSTRAINT uq_quality_report_artifacts_version_format
        UNIQUE (report_version_id, format),
    CONSTRAINT uq_quality_report_artifacts_file_object_id
        UNIQUE (file_object_id),

    CONSTRAINT fk_quality_report_artifacts_report_version_id_versions
        FOREIGN KEY (report_version_id) REFERENCES quality_report_versions (id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_quality_report_artifacts_file_object_id_file_objects
        FOREIGN KEY (file_object_id) REFERENCES file_objects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_quality_report_artifacts_created_by_users
        FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT
);
"""

# ---------------------------------------------------------------------------
# 五、触发器清单（描述，不是可执行 SQL；SQL 由实现期 ddl.py 写，复用 platform.immutability）
# ---------------------------------------------------------------------------

#: 不可变触发器：直接复用 `platform.immutability.immutable_trigger_ddl(table)`，
#: 命名 `trg_<table>_immutable`，共用函数 `rednote_reject_mutation()`（**跨包共用，不得 DROP**）。
IMMUTABLE_TABLES = (
    "quality_report_versions",
    "quality_report_items",
    "quality_dimension_results",
    "quality_similarity_pairs",
    "quality_media_alignments",
    "quality_report_artifacts",
)
# `quality_reports` 刻意不在此列：它是唯一可变表（只允许推进投影列）。

TRIGGERS = (
    {
        "table": "quality_reports",
        "name": "trg_quality_reports_current_version",
        "timing": "AFTER INSERT OR UPDATE, FOR EACH ROW, CONSTRAINT TRIGGER DEFERRABLE INITIALLY DEFERRED",
        "function": "rednote_check_quality_report_current_version()",
        "why": (
            "current_version_id 非空时：该版本必须属于本报告（report_id = NEW.id），"
            "且其 revision_no = NEW.current_revision_no。FK 只保证版本存在、不管它属于谁；"
            "指错系列时『当前报告』是别人的报告，且不会报错。"
        ),
    },
    {
        "table": "quality_report_versions",
        "name": "trg_quality_report_versions_consistency",
        "timing": "AFTER INSERT, FOR EACH ROW, CONSTRAINT TRIGGER DEFERRABLE INITIALLY DEFERRED",
        "function": "rednote_check_quality_report_version_consistency()",
        "why": (
            "与 trg_content_versions_node_consistency 同款：对应 version_nodes 行必须存在，"
            "entity_type = 'REPORT'，entity_id = NEW.report_id，revision_no / payload_hash 一致。"
            "★ 另核对聚合计数（提交时子表行已就位）：ok/alert/not_computed 等于 7 个维度行的"
            "实际分布且维度行恰 7 条；included/excluded 等于 items 行的实际分布。"
            "这是『汇总字段与明细双声称』（WP30 R6）无法用 CHECK 表达的那一半。"
        ),
    },
    {
        "table": "version_nodes",
        "name": "trg_version_nodes_report_paired",
        "timing": "AFTER INSERT, FOR EACH ROW WHEN (NEW.entity_type = 'REPORT'), CONSTRAINT TRIGGER DEFERRABLE INITIALLY DEFERRED",
        "function": "rednote_check_quality_report_node_paired()",
        "why": (
            "OP-88 反向判据同款：REPORT 节点必须配对 quality_report_versions 行。"
            "两道判断顺序不可换：先判『quality_reports 里有 id = entity_id』（不归我管的 REPORT 节点"
            "——WP11 通用用例会注册——直接放行），再判配对行是否存在。"
            "与 brief/knowledge/baseline/prompt/content/media 的 paired 触发器同表并存，"
            "各自被 WHEN 收窄到自己的 entity_type。"
        ),
    },
    {
        "table": "quality_report_items",
        "name": "trg_quality_report_items_consistency",
        "timing": "AFTER INSERT, FOR EACH ROW, CONSTRAINT TRIGGER DEFERRABLE INITIALLY DEFERRED",
        "function": "rednote_check_quality_report_item()",
        "why": (
            "两条判据：① content_version_id（非空时）必须属于 content_item_id；"
            "② content_item_id 必须属于本报告所属项目。FK 只保证行存在。"
            "①错了『钉死的是哪一版』就有两个答案，STALE 判定永远为真；"
            "②错了会把别的项目的内容算进本项目报告。"
        ),
    },
    {
        "table": "quality_dimension_results",
        "name": "trg_quality_dimension_results_flagged",
        "timing": "AFTER INSERT, FOR EACH ROW, CONSTRAINT TRIGGER DEFERRABLE INITIALLY DEFERRED",
        "function": "rednote_check_quality_dimension_flagged()",
        "why": (
            "提交时核对：flagged_item_count = 该 (版本, 维度) 下相似对涉及的去重篇数；"
            "flagged_item_count > 0 时 max_similarity_permille >= pair_threshold_permille。"
            "否则『被标记 N 篇』与实际 pair 行可以互相矛盾而不报错。"
        ),
    },
    {
        "table": "quality_similarity_pairs",
        "name": "trg_quality_similarity_pairs_consistency",
        "timing": "AFTER INSERT, FOR EACH ROW, CONSTRAINT TRIGGER DEFERRABLE INITIALLY DEFERRED",
        "function": "rednote_check_quality_pair()",
        "why": (
            "三条判据：对应维度行必须 COMPUTED（NOT_COMPUTED 维度不得有 pair 行——"
            "否则『没算』与『没命中』同形）；similarity_permille >= 该维度行的 pair_threshold_permille"
            "（只存命中）；两篇的 items 行均为 INCLUDED。"
        ),
    },
    {
        "table": "quality_media_alignments",
        "name": "trg_quality_media_alignments_consistency",
        "timing": "AFTER INSERT, FOR EACH ROW, CONSTRAINT TRIGGER DEFERRABLE INITIALLY DEFERRED",
        "function": "rednote_check_quality_alignment()",
        "why": (
            "三条判据：media_item_id（非空时）必须属于 content_item_id；"
            "对应 items 行的 content_version_id 非空（NO_VERSION 篇目不应有对位行）；"
            "items.status = 'INCLUDED' ⇔ verdict <> 'NOT_EVALUABLE'"
            "（NOT_EVALUABLE 只出现在 EXCLUDED(EMPTY_BODY) 且仍有图的篇目，保证该取值可达，"
            "不留空转取值）。"
        ),
    },
)

#: 本包**自己的**函数（downgrade 只删这 7 个）。`rednote_reject_mutation` 是跨包共用函数，
#: **不在此列** —— 删它会让所有已建表的不可变守卫静默失效（ISS-79）。
OWN_FUNCTIONS = (
    "rednote_check_quality_report_current_version",
    "rednote_check_quality_report_version_consistency",
    "rednote_check_quality_report_node_paired",
    "rednote_check_quality_report_item",
    "rednote_check_quality_dimension_flagged",
    "rednote_check_quality_pair",
    "rednote_check_quality_alignment",
)

# ---------------------------------------------------------------------------
# 六、跨包耦合说明（本包刻意把它降到最低）
# ---------------------------------------------------------------------------

CROSS_PACKAGE_NOTE = """
本包 **没有任何跨包入向 FK**（没有别人的表引用本包的表），也 **不以 FK 引用 WP30/WP31/WP32 的表**。
因此不触发 WP30 R8 那类『改变他包测试夹具 drop/keep 语义』的耦合。

出向 FK 只指向：projects / users / content_items / content_versions / generation_contexts /
production_baselines / media_items / file_objects / version_nodes（全部已存在于迁移链）。

三处只读依赖不是 FK 而是 SQL/函数读：
  · WP32 的 batch_items / batches（D2，2026-10-19）—— scope_kind='BATCH' 的成员取自
    `batch_items WHERE batch_id = :id`，并核对 `batches.project_id` = 报告所属项目；
    quality_reports.batch_id 为无 FK 的审计指针（理由：FK 会使 batches 被他包表引用，
    改变 WP32 夹具 drop/keep 语义，即 R8；成员已由 quality_report_items 钉住）。
    因此 WP33 迁移与实现必须排在 WP32 之后。
  · media_items 的 (paragraph_ref, sort_order, state) —— 裸 SQL 只读；
    若主 agent 不愿意让他人裸 SQL 读 media 表，改走『media 侧加只读接缝』（处置 A）。
  · WP30 的 self_check_* —— MVP 不读（说明文档 Q-07）。

WP30 回接（说明文档 §11）发生在 review → quality 方向（review 的 bridge 读本包的当前报告），
本包不 import review，所以无环。
"""

# ---------------------------------------------------------------------------
# 七、迁移核对清单
# ---------------------------------------------------------------------------

CHECKLIST = (
    "七张表的 CHECK 名带 ck_<table>_ 前缀（NAMING_CONVENTION），逐个核对闭集取值域与 "
    "types.py 的 frozenset 一致；迁移里取值域写成字面量（ISS-191）。",
    "★ quality_dimension_results 的 NULL/0 分离三条 CHECK 必须齐全"
    "（computed_columns_present / not_computed_columns_absent / reason_paired）；"
    "少一条就让『没算』与『算了得 0』同形。",
    "★ verdict 双向等价式（整数交叉相乘、严格大于）与 compared>=2、flagged<>1 必须存在。",
    "★ quality_report_versions 的 outcome 双向等价式与 ok+alert+not_computed=7 必须存在；"
    "outcome 刻意不叫 PASSED。",
    "★ quality_similarity_pairs 的 permille_derived（= ∩*1000/∪）与 ordered（a<b）必须存在。",
    "★ quality_media_alignments 的四条 verdict 双向等价式 + media_columns_paired 必须齐全；"
    "不得出现 expected_ref 唯一约束（契约 §4：多张同段合法）。",
    "not_computed_reason / exclude_reason / 对位 reason / media_state **不得**有取值域 CHECK（开集 / 借用词汇）。"
    "迁移 docstring 必须写明理由；证据脚本另有反向断言专守『没人顺手补上』。",
    "quality_dimension_results 的 unimplemented_not_computed CHECK 存在，且迁移 docstring 写明"
    "『放开它必须走一次迁移，是有意的』。",
    "唯一约束齐全：reports(project_id,scope_digest)、versions(report_id,revision_no)、"
    "items(version,content)与(version,position)、dims(version,dimension)、"
    "pairs(version,dimension,a,b)、alignments(version,content,sort_order)与(version,media_item)、"
    "artifacts(version,format)与(file_object_id)。"
    "★ versions 上**没有** input_fingerprint 唯一约束（A→B→A 场景需要第 3 版）。",
    "全部 FK 必须 ON DELETE RESTRICT（本包没有任何级联删除）；"
    "延迟 FK 恰两条：quality_reports.current_version_id 与 quality_report_versions.id。",
    "复合 FK 三条（pairs×3）+ alignments×1 必须用 models.py 里显式 name=，且与本草案一致。",
    "触发器：6 个不可变（IMMUTABLE_TABLES，复用 rednote_reject_mutation）+ 7 个一致性"
    "（TRIGGERS，均为延迟约束触发器）；其中 trg_version_nodes_report_paired 挂在共用表 version_nodes 上。",
    "downgrade 只删 OWN_FUNCTIONS 这 7 个，**不删** rednote_reject_mutation（ISS-79）。",
    "全部约束/索引名在 63 字节内（本草案已脚本实算 101 个名字；其中约定名会超限的"
    "fk_quality_report_items_generation_context_id_generation_contexts 为 65 字节，已改为显式短名 "
    "fk_quality_report_items_generation_context_id；实现期 models.py 须与之一致并再核）。",
    "往返判据 upgrade head → downgrade base → upgrade head；parity 判据（ORM ⇄ 迁移）；"
    "check_migration_value_domain_no_live_import.py 覆盖本包。",
    "验证库核对（本草案未做）：实现落地后在 rednote_test_wp33 / 验证库上导出 pg_constraint、"
    "pg_indexes、pg_trigger 三项，与本文件逐条比对，再回写本文件。",
)
