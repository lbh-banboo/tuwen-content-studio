"""WP18 DDL 草案：Brief 结构化解构的五张表 + 七个触发器。

**这不是迁移文件。** 迁移由主 agent 串行收口（接缝冻结 §4.2）。
本文件是**规格**：表名、列、类型、可空、默认值、全部约束与索引
（含部分索引的 `WHERE`）、外键、触发器需求，逐条可核对。

**为什么用 `.py` 而不是 `.md`**：本草案的每一列/每个约束都对应
`models.py` 里的一行。用 Python 写，读者可以直接把两边并排比对；
写成散文则必须来回翻。**本文件不 import 本包任何模块** ——
它要能在"包还没装好"的环境里被打开阅读，而 import 会让它连带失败。

---

## 一、文件怎么用

1. 主 agent 用 `models.py` 生成迁移（`alembic revision --autogenerate`），
   然后用本文件**逐条核对**生成的 DDL 是否与预期一致 ——
   尤其是三处 `DEFERRABLE INITIALLY DEFERRED` 与三处部分索引
   （autogenerate **读不回部分索引的 WHERE** 时最容易漏）。
2. 迁移必须调 `ddl.all_trigger_ddl()` 把七个触发器一并建出。
   **其中 `trg_version_nodes_brief_paired` 挂在共用表 `version_nodes` 上**
   （它是 OP-88 的反向判据），详见第三节。

## 二、与数据结构 02 §6 的对应关系

| 数据结构 02 §6 的表 | 本包是否落地 | 备注 |
|---|---|---|
| `briefs` | ✅ | 逻辑实体（一个项目一份） |
| `brief_drafts` | ✅ | 加了三列（`suggestions` / `suggestions_version_id` / `enrichment_request`），见 §四 |
| `brief_versions` | ✅ | 不可变 |
| `brief_claims` | ✅ | 不可变 |
| `brief_issues` | ✅ | 不可变 |
| `brief_field_definitions` | ❌ | 数据结构里没有这张表；字段 Schema 在 `schema.BRIEF_FIELDS`（代码即真相） |

## 三、触发器清单（7 个）

| # | 触发器 | 挂在哪张表 | 类型 | 作用 |
|---|---|---|---|---|
| 1 | `trg_brief_versions_immutable` | `brief_versions` | BEFORE UPDATE/DELETE | 拒绝原地改写（复用 `rednote_reject_mutation()`） |
| 2 | `trg_brief_claims_immutable` | `brief_claims` | 同上 | 同上 |
| 3 | `trg_brief_issues_immutable` | `brief_issues` | 同上 | 同上 |
| 4 | `trg_brief_versions_node_consistency` | `brief_versions` | **CONSTRAINT TRIGGER, DEFERRABLE INITIALLY DEFERRED** AFTER INSERT | OP-88 判据 1~3 + PARTIAL 必须留下疑问 |
| 5 | `trg_version_nodes_brief_paired` | **`version_nodes`（共用表）** | 同上 | OP-88 反向判据：BRIEF 节点必须配对 `brief_versions` 行 |
| 6 | `trg_brief_claims_evidence_scope` | `brief_claims` | 同上（带 `WHEN evidence_segment_id IS NOT NULL`） | 证据片段必须属于本版本那次解析（假溯源防护） |
| 7 | `trg_brief_issues_project_scope` | `brief_issues` | 同上 | `project_id` 必须等于版本所属项目 |

**#4~#7 必须是 `DEFERRABLE INITIALLY DEFERRED`**：它们校验的都是
"同一事务内另一次 INSERT 的结果"，而插入顺序由应用决定
（`versions.service.register_version` 内部有 SAVEPOINT + 重试，
不暴露"节点行何时落库"）。延迟到 COMMIT 校验，让顺序不再是必须猜对的事。

**#5 是本包唯一挂到非本模块表上的对象**：它不修改 WP11 的任何代码或表结构，
只是 `version_nodes` 上的一个新触发器，且被
`WHEN (NEW.entity_type = 'BRIEF')` 限定到本模块负责的实体类型 ——
不加这个条件会让每次 `CONTENT` / `PLAN` / `PROMPT` 版本注册都白跑一次函数。

**#1~#3 共用同一个函数 `rednote_reject_mutation()`**（`platform.immutability`）：
各写一份会在 downgrade 时撞
"cannot drop function ... because other objects depend on it"
（WP11 踩过、WP16 差点再踩）。

---

## 四、`brief_drafts` 为什么比数据结构 02 多三列

原表（数据结构 02 §6）的列见第五节。本包新增三列，理由都在 `models.py`：

| 新增列 | 为什么 |
|---|---|
| `suggestions` (jsonb) | 加厚产出。**不能混进 `claims`** —— 混进去之后运营再也分不清"这条是原文里的"还是"这条是模型想出来的"，而那正是需求 §5.2 要求把外部证据与原文事实分开的原因 |
| `suggestions_version_id` (uuid, nullable) | 建议基于哪一版。没有它，一份过期的建议与一份新鲜的建议看起来完全一样 |
| `enrichment_request` (jsonb, nullable) | 待处理的加厚请求参数（`{"briefVersionId", "allowedSourceIds"}`）。**落在这里的原因见 `handlers.py` 模块 docstring 第二节**：`jobs.result_ref` 是结果引用、`jobs.input_version_ids` 装不下 `source_segments` 的 id、新建表会自创结构。**处理器读完即清空**（一次性） |

**`enrichment_request` 的列类型必须是 `JSONB(none_as_null=True)`**：
默认的 `None` 会被序列化成 JSON `null`（非空值），
于是"清空请求"这个动作本身会撞 `ck_brief_drafts_enrichment_request_is_object`
（`jsonb_typeof('null') = 'null' ≠ 'object'`）。本机实测踩到过。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 生成迁移时必须逐条核对的形状（本文件是 specs，不是可执行代码）
# ---------------------------------------------------------------------------

BRIEFS_DDL = """
-- 表名：briefs。逻辑实体：**一个项目一份**。
CREATE TABLE briefs (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id        uuid        NOT NULL,
    active_version_id uuid        NULL,
    draft_id          uuid        NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_briefs_project_id UNIQUE (project_id),

    CONSTRAINT fk_briefs_project_id_projects
        FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE RESTRICT,

    -- **延迟**：`ensure_brief` 先插 briefs 行、再插 brief_drafts 行，
    -- 两者要互相指（brief.draft_id ↔ draft.brief_id），
    -- 不延迟就无法在同一事务里建出这对循环引用。
    CONSTRAINT fk_briefs_draft_id_brief_drafts
        FOREIGN KEY (draft_id) REFERENCES brief_drafts (id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,

    -- **延迟**：`active_version_id` 指向本项目的某一版。
    -- 固化时先插 brief_versions 再回填这里，顺序不确定。
    CONSTRAINT fk_briefs_active_version_id_version_nodes
        FOREIGN KEY (active_version_id) REFERENCES version_nodes (id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX ix_briefs_active_version_id ON briefs (active_version_id);

-- 说明：
--  · **没有 `state` 列**：Brief 的"进度"由"必填字段是否填满 + 有无未答复
--    BLOCK 疑问"推导，不是独立状态机。存一份就会与事实漂移。
--  · **没有 `lock_version`**：Brief 本身不被并发修改（改的是草稿）。
--    ETag 的载体是 `brief_drafts.lock_version`。
"""

BRIEF_DRAFTS_DDL = """
-- 表名：brief_drafts。可变（有 lock_version / updated_at），**一个 Brief 一份**。
CREATE TABLE brief_drafts (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    brief_id             uuid        NOT NULL,

    -- 基准版本="这一轮编辑基于哪一版"。可空（从未固化过时为空）。
    base_version_id      uuid        NULL,

    -- 结构化字段：{路径: {"status": ..., "value": ...}}。
    -- **值与状态必须在一起**：分成两列会出现"值在 A、状态在 B"的对不齐，
    -- 而这类错误不报错，只会让固化校验基于错误的完整度判断放行。
    structured_fields    jsonb       NOT NULL DEFAULT '{}'::jsonb,

    -- 结论列表（ClaimDraft.to_json() 的数组）。
    claims               jsonb       NOT NULL DEFAULT '[]'::jsonb,

    -- 疑问处置。
    -- **存完整疑问对象数组而不是"id→处置"映射**：草稿阶段的疑问还没有
    -- brief_issues 行（它们到固化时才落库），一个 id 映射无处可指。
    issue_resolutions    jsonb       NOT NULL DEFAULT '[]'::jsonb,

    -- ↓ 本包新增（见 §四）
    suggestions          jsonb       NOT NULL DEFAULT '[]'::jsonb,
    suggestions_version_id uuid      NULL,
    enrichment_request   jsonb       NULL,   -- ORM 侧 JSONB(none_as_null=True)

    -- 本轮解构的输入来源。
    source_file_id       uuid        NULL,
    source_parse_run_id  uuid        NULL,
    source_state         varchar(20) NOT NULL DEFAULT 'NONE',

    -- 解析错误明细（WP17 的 parse_runs.errors 原样带上）。
    -- 只存 {unit, code, message(截断)} —— 不放正文（WP17 §1.4 第 6 条）。
    source_errors        jsonb       NOT NULL DEFAULT '[]'::jsonb,

    schema_version       integer     NOT NULL DEFAULT 1,
    lock_version         integer     NOT NULL DEFAULT 1,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_brief_drafts_brief_id UNIQUE (brief_id),

    CONSTRAINT fk_brief_drafts_brief_id_briefs
        FOREIGN KEY (brief_id) REFERENCES briefs (id) ON DELETE RESTRICT,
    CONSTRAINT fk_brief_drafts_base_version_id_version_nodes
        FOREIGN KEY (base_version_id) REFERENCES version_nodes (id) ON DELETE RESTRICT,
    CONSTRAINT fk_brief_drafts_source_file_id_file_objects
        FOREIGN KEY (source_file_id) REFERENCES file_objects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_brief_drafts_source_parse_run_id_parse_runs
        FOREIGN KEY (source_parse_run_id) REFERENCES parse_runs (id) ON DELETE RESTRICT,
    CONSTRAINT fk_brief_drafts_suggestions_version_id_version_nodes
        FOREIGN KEY (suggestions_version_id) REFERENCES version_nodes (id) ON DELETE RESTRICT,

    -- jsonb 形状校验。少了任何一条，一个"claims 是对象而不是数组"的写入
    -- 会静默通过，而所有消费方都假设它是数组（`for item in draft.claims`
    -- 会按 key 迭代 —— 得到一个字符串，然后在下一层报一个看不懂的错）。
    CONSTRAINT ck_brief_drafts_structured_fields_is_object
        CHECK (jsonb_typeof(structured_fields) = 'object'),
    CONSTRAINT ck_brief_drafts_claims_is_array
        CHECK (jsonb_typeof(claims) = 'array'),
    CONSTRAINT ck_brief_drafts_issue_resolutions_is_array
        CHECK (jsonb_typeof(issue_resolutions) = 'array'),
    CONSTRAINT ck_brief_drafts_suggestions_is_array
        CHECK (jsonb_typeof(suggestions) = 'array'),
    CONSTRAINT ck_brief_drafts_source_errors_is_array
        CHECK (jsonb_typeof(source_errors) = 'array'),
    CONSTRAINT ck_brief_drafts_enrichment_request_is_object
        CHECK (enrichment_request IS NULL
               OR jsonb_typeof(enrichment_request) = 'object'),

    -- 建议必须能说清"基于哪一版"。
    -- 反方向（有版本没建议）是正常的：固化后建议被清空但版本保留，
    -- 用来显示"上一轮加厚基于 v3"。
    CONSTRAINT ck_brief_drafts_suggestions_require_version
        CHECK (jsonb_array_length(suggestions) = 0
               OR suggestions_version_id IS NOT NULL),

    -- 取值域。
    CONSTRAINT ck_brief_drafts_source_state_valid
        CHECK (source_state IN ('SUCCEEDED', 'PARTIAL', 'FAILED', 'NONE')),
    CONSTRAINT ck_brief_drafts_schema_version_positive CHECK (schema_version >= 1),
    CONSTRAINT ck_brief_drafts_lock_version_positive   CHECK (lock_version >= 1),

    -- **本包对 PARTIAL 的第二层保证**（第一层是上面的受控枚举）。
    -- 输入是"部分成功"时草稿里**必须**至少有一条疑问：
    -- 否则 PARTIAL 会静静地躺在库里 —— 字段缺了、疑问没有，
    -- 而"必填字段已填写或明确不适用"这条完成条件会在一堆空洞上通过。
    CONSTRAINT ck_brief_drafts_partial_requires_issue
        CHECK (source_state <> 'PARTIAL'
               OR jsonb_array_length(issue_resolutions) > 0)
);

CREATE INDEX ix_brief_drafts_base_version_id        ON brief_drafts (base_version_id);
CREATE INDEX ix_brief_drafts_source_parse_run_id    ON brief_drafts (source_parse_run_id);
CREATE INDEX ix_brief_drafts_suggestions_version_id ON brief_drafts (suggestions_version_id);

-- **没有部分索引**：本表的查询都是按 brief_id（唯一列）或 id 走，
-- 没有"只查某一状态"的热路径。加一个用不上的部分索引只是写入成本。
"""

BRIEF_VERSIONS_DDL = """
-- 表名：brief_versions。**不可变**（触发器 #1）。
CREATE TABLE brief_versions (
    -- **id 同时是 version_nodes.id**（数据结构 02 §5 的原文要求）。
    id                  uuid        PRIMARY KEY,
    brief_id            uuid        NOT NULL,
    source_file_id      uuid        NOT NULL,   -- 固化必须有来源文档
    source_parse_run_id uuid        NULL,       -- 可空：手工录入的 Brief 没有解析运行
    source_state        varchar(20) NOT NULL,
    product_name        varchar(200) NOT NULL,  -- 投影列（数据结构 02 §6 要求）
    model_code          varchar(100) NULL,      -- 投影列，可空
    summary             text        NOT NULL,   -- 固化时生成的可读摘要
    structured_fields   jsonb       NOT NULL,
    schema_version      integer     NOT NULL DEFAULT 1,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          uuid        NULL,       -- 可空：系统自动产生的版本没有操作人

    CONSTRAINT fk_brief_versions_brief_id_briefs
        FOREIGN KEY (brief_id) REFERENCES briefs (id) ON DELETE RESTRICT,
    CONSTRAINT fk_brief_versions_source_file_id_file_objects
        FOREIGN KEY (source_file_id) REFERENCES file_objects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_brief_versions_source_parse_run_id_parse_runs
        FOREIGN KEY (source_parse_run_id) REFERENCES parse_runs (id) ON DELETE RESTRICT,
    CONSTRAINT fk_brief_versions_created_by_users
        FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT,

    -- **这一条必须 DEFERRABLE INITIALLY DEFERRED**：`register_version`
    -- 插入注册表的顺序与子表插入的顺序由它自己决定（内部 SAVEPOINT + 重试）。
    -- 不延迟的话，子表行会先因外键失败。
    CONSTRAINT fk_brief_versions_id_version_nodes
        FOREIGN KEY (id) REFERENCES version_nodes (id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,

    CONSTRAINT ck_brief_versions_source_state_valid
        CHECK (source_state IN ('SUCCEEDED', 'PARTIAL', 'FAILED', 'NONE')),
    CONSTRAINT ck_brief_versions_structured_fields_is_object
        CHECK (jsonb_typeof(structured_fields) = 'object'),
    CONSTRAINT ck_brief_versions_schema_version_positive CHECK (schema_version >= 1),
    CONSTRAINT ck_brief_versions_product_name_not_empty
        CHECK (char_length(product_name) > 0),
    CONSTRAINT ck_brief_versions_summary_not_empty  CHECK (char_length(summary) > 0),
    CONSTRAINT ck_brief_versions_summary_length     CHECK (char_length(summary) <= 2000)
);

CREATE INDEX ix_brief_versions_brief_id            ON brief_versions (brief_id);
CREATE INDEX ix_brief_versions_source_file_id      ON brief_versions (source_file_id);
CREATE INDEX ix_brief_versions_source_parse_run_id ON brief_versions (source_parse_run_id);

-- **没有 `lock_version` / `updated_at`**：本表不可变。
-- 加了 lock_version 反而是在暗示"它会被改"（数据结构 02 §1）。
-- **没有 `revision_no`**：那是注册表 `version_nodes` 的列，不是本表的
-- （数据结构 02 §6 的 brief_versions 列清单里没有它）。
"""

BRIEF_CLAIMS_DDL = """
-- 表名：brief_claims。**不可变**（触发器 #2）。一条结论一行。
CREATE TABLE brief_claims (
    id                  uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    brief_version_id    uuid         NOT NULL,
    field_path          varchar(200) NOT NULL,
    claim_text          text         NOT NULL,

    -- 分类来源（需求 §5.2）。受控枚举。
    category            varchar(30)  NOT NULL,

    -- 证据。**单值**（数据结构 02 §6），协议 §5.1 的 evidence 是数组 ——
    -- 收敛点在 decomposer._read_segment_id（取第一个，忽略其余）。
    evidence_segment_id uuid         NULL,
    evidence_url        varchar      NULL,

    applicability       jsonb        NOT NULL DEFAULT '{}'::jsonb,
    resolution_status   varchar(20)  NOT NULL DEFAULT 'PENDING',
    resolution_reason   text         NULL,
    confirmed_by        uuid         NULL,
    claim_hash          varchar(64)  NOT NULL,   -- SHA-256 十六进制
    created_at          timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT fk_brief_claims_brief_version_id_brief_versions
        FOREIGN KEY (brief_version_id) REFERENCES brief_versions (id) ON DELETE RESTRICT,
    CONSTRAINT fk_brief_claims_evidence_segment_id_source_segments
        FOREIGN KEY (evidence_segment_id) REFERENCES source_segments (id) ON DELETE RESTRICT,
    CONSTRAINT fk_brief_claims_confirmed_by_users
        FOREIGN KEY (confirmed_by) REFERENCES users (id) ON DELETE RESTRICT,

    -- 同版本同字段同内容只能有一条。`claim_hash` 参与唯一键而不是
    -- `claim_text`：text 是 text 类型，长文本做唯一索引会撞 btree 行大小上限。
    CONSTRAINT uq_brief_claims_version_field_hash
        UNIQUE (brief_version_id, field_path, claim_hash),

    CONSTRAINT ck_brief_claims_category_valid
        CHECK (category IN ('SOURCE_FACT', 'EXTERNAL_EVIDENCE', 'STRATEGY', 'UNCONFIRMED')),
    CONSTRAINT ck_brief_claims_resolution_status_valid
        CHECK (resolution_status IN ('PENDING', 'CONFIRMED', 'NOT_APPLICABLE')),

    -- **"分类来源"的第一道（数据库）保证**：事实类必须有来源。
    -- 允许 evidence_url（EXTERNAL_EVIDENCE 没有片段可挂）。
    -- 只要求"有 evidence_segment_id"是不够的 —— 片段还必须属于本版本
    -- 那次解析，那是跨表不变量，见触发器 #6。
    CONSTRAINT ck_brief_claims_factual_category_requires_evidence
        CHECK (category NOT IN ('SOURCE_FACT', 'EXTERNAL_EVIDENCE')
               OR evidence_segment_id IS NOT NULL
               OR (evidence_url IS NOT NULL AND char_length(evidence_url) > 0)),

    -- "明确不适用"必须写原因（需求原文："由运营明确标记'不适用'并填写原因"）。
    CONSTRAINT ck_brief_claims_not_applicable_requires_reason
        CHECK (resolution_status <> 'NOT_APPLICABLE'
               OR (resolution_reason IS NOT NULL
                   AND char_length(resolution_reason) > 0)),

    -- 一条无人确认的 CONFIRMED 与 PENDING 语义上没区别。
    CONSTRAINT ck_brief_claims_confirmed_requires_actor
        CHECK (resolution_status <> 'CONFIRMED' OR confirmed_by IS NOT NULL),

    CONSTRAINT ck_brief_claims_claim_text_not_empty CHECK (char_length(claim_text) > 0),
    CONSTRAINT ck_brief_claims_claim_text_length    CHECK (char_length(claim_text) <= 2000),
    CONSTRAINT ck_brief_claims_field_path_not_empty CHECK (char_length(field_path) > 0),
    CONSTRAINT ck_brief_claims_claim_hash_length    CHECK (char_length(claim_hash) = 64),
    CONSTRAINT ck_brief_claims_applicability_is_object
        CHECK (jsonb_typeof(applicability) = 'object')
);

CREATE INDEX ix_brief_claims_version_field ON brief_claims (brief_version_id, field_path);
CREATE INDEX ix_brief_claims_evidence_segment_id ON brief_claims (evidence_segment_id);

-- **部分索引**：待确认结论的查询路径。
-- 这是"步骤 1 还有多少没确认"的热查询，而它只关心 PENDING 那一小部分；
-- 全表索引会把已确认的历史结论也扫进来。
-- **`WHERE` 子句必须与 models.py 逐字一致** ——
-- alembic autogenerate 读不回部分索引的 WHERE，会提议重建它。
CREATE INDEX ix_brief_claims_pending ON brief_claims (brief_version_id)
    WHERE resolution_status = 'PENDING';
"""

BRIEF_ISSUES_DDL = """
-- 表名：brief_issues。**不可变**（触发器 #3）。一条疑问一行。
CREATE TABLE brief_issues (
    id               uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    -- **冗余列**：等于"该版本所属 Brief 的 project_id"。
    -- 用途是让"本项目还有哪些未处置疑问"不必 join 三张表。
    -- 一致性由触发器 #7 保证 —— 不校验就会漂移成两个互相矛盾的答案，
    -- 而"核心疑问未解决阻断步骤 1"正是按它来查的。
    project_id       uuid         NOT NULL,
    brief_version_id uuid         NOT NULL,
    field_path       varchar(200) NOT NULL,
    severity         varchar(10)  NOT NULL,
    description      text         NOT NULL,
    state            varchar(20)  NOT NULL,
    resolution       text         NULL,
    resolved_by      uuid         NULL,
    created_at       timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT fk_brief_issues_project_id_projects
        FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_brief_issues_brief_version_id_brief_versions
        FOREIGN KEY (brief_version_id) REFERENCES brief_versions (id) ON DELETE RESTRICT,
    CONSTRAINT fk_brief_issues_resolved_by_users
        FOREIGN KEY (resolved_by) REFERENCES users (id) ON DELETE RESTRICT,

    CONSTRAINT ck_brief_issues_severity_valid CHECK (severity IN ('BLOCK', 'WARN')),
    CONSTRAINT ck_brief_issues_state_valid
        CHECK (state IN ('OPEN', 'RESOLVED', 'DISMISSED')),

    -- **"核心疑问未解决阻断步骤 1" 的数据库级表述**。
    -- 它防的是**服务层回归**：某天有人改了固化校验而没改这里，
    -- 一条"带着未答复核心疑问的版本"就会写进去 ——
    -- 而那正是本包存在的理由。
    CONSTRAINT ck_brief_issues_blocking_issue_cannot_be_open
        CHECK (severity <> 'BLOCK' OR state <> 'OPEN'),

    -- OPEN 是"还没人处置"：它不该有处置内容。
    CONSTRAINT ck_brief_issues_open_issue_has_no_resolution
        CHECK (state <> 'OPEN' OR (resolution IS NULL AND resolved_by IS NULL)),

    -- 已处置（RESOLVED / DISMISSED）必须有理由与处置人。
    -- "已解决"而不说怎么解决的，等于把一条疑问改成"看起来解决了"。
    CONSTRAINT ck_brief_issues_settled_issue_has_resolution
        CHECK (state = 'OPEN'
               OR (resolution IS NOT NULL AND char_length(resolution) > 0
                   AND resolved_by IS NOT NULL)),

    CONSTRAINT ck_brief_issues_description_not_empty CHECK (char_length(description) > 0),
    CONSTRAINT ck_brief_issues_description_length    CHECK (char_length(description) <= 500),
    CONSTRAINT ck_brief_issues_field_path_not_empty  CHECK (char_length(field_path) > 0)
);

CREATE INDEX ix_brief_issues_version_severity ON brief_issues (brief_version_id, severity);

-- **部分索引**：未处置疑问的查询路径（项目维度）。
-- 这个索引的两列顺序与 WHERE 都是刻意的：
-- 查询形如"某项目还有哪些 OPEN 的 BLOCK 疑问"，severity 作第二列
-- 让 `ORDER BY severity` / `WHERE severity='BLOCK'` 都在索引内完成。
CREATE INDEX ix_brief_issues_open ON brief_issues (project_id, severity)
    WHERE state = 'OPEN';
"""

# ---------------------------------------------------------------------------
# 触发器 DDL：**不要照抄这里**，用 ddl.all_trigger_ddl() 的返回值
# ---------------------------------------------------------------------------

TRIGGER_NOTE = """
七个触发器由 `rednote.modules.brief.ddl.all_trigger_ddl()` 生成，
返回 `[(表名, 函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP), …]`。

**迁移必须调它而不是手抄 SQL**：手抄会出现"迁移建了 trg_x、验证脚本找 trg_y"
这种**看起来通过、实际没有保护**的情况（WP11 §4.3 的教训）。

迁移里的建触发器顺序：

    1. CREATE OR REPLACE FUNCTION ...      （幂等）
    2. DROP TRIGGER IF EXISTS ...          （**CREATE TRIGGER 不是幂等的**）
    3. CREATE [CONSTRAINT] TRIGGER ...

**为什么必须 2 在 3 之前**：本机实测直接 CREATE 会报
`DuplicateObject: trigger "trg_..." for relation "..." already exists`。
测试的 module 级 fixture 会反复执行这段，所以 DROP 是必需的。

函数 DROP 在 downgrade 时才用，且**必须先 DROP TRIGGER 再 DROP FUNCTION** ——
否则函数仍被触发器引用，PG 会拒绝删除。
"""


def summary() -> dict[str, object]:
    """本草案的结构摘要（供核对脚本用）。

    **本函数不连数据库** —— 它只是把上面的常量整理成可断言的形式，
    让"迁移生成的表/索引/触发器是否齐全"这件事可以机器核对。
    """
    return {
        "tables": (
            "briefs",
            "brief_drafts",
            "brief_versions",
            "brief_claims",
            "brief_issues",
        ),
        "partial_indexes": (
            # (索引名, 表, WHERE 子句)
            ("ix_brief_claims_pending", "brief_claims", "resolution_status = 'PENDING'"),
            ("ix_brief_issues_open", "brief_issues", "state = 'OPEN'"),
        ),
        "deferrable_foreign_keys": (
            # (约束名, 表, 说明)
            ("fk_brief_versions_id_version_nodes", "brief_versions", "OP-88 判据 1"),
            ("fk_briefs_draft_id_brief_drafts", "briefs", "循环引用"),
            ("fk_briefs_active_version_id_version_nodes", "briefs", "固化回填顺序不确定"),
        ),
        "deferrable_triggers": (
            "trg_brief_versions_node_consistency",
            "trg_version_nodes_brief_paired",
            "trg_brief_claims_evidence_scope",
            "trg_brief_issues_project_scope",
        ),
        "immutable_triggers": (
            "trg_brief_versions_immutable",
            "trg_brief_claims_immutable",
            "trg_brief_issues_immutable",
        ),
        # **挂到非本模块表上的触发器** —— 主 agent 落迁移时最易漏的一个。
        "triggers_on_shared_tables": (
            ("trg_version_nodes_brief_paired", "version_nodes", "WP11 的共用注册表"),
        ),
    }