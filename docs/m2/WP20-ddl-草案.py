"""WP20 DDL 草案：知识 CRUD 与版本的五张表 + 六个触发器。

**这不是迁移文件。** 迁移由主 agent 串行收口（接缝冻结 §4.2）。
本文件是**规格**：表名、列、类型、可空、默认值、全部约束与索引
（含部分索引的 `WHERE`）、外键、触发器需求，逐条可核对。

**为什么用 `.py` 而不是 `.md`**：本草案的每一列/每个约束都对应
`models.py` 里的一行。用 Python 写，读者可以直接把两边并排比对；
写成散文则必须来回翻。**本文件不 import 本包任何模块** ——
它要能在"包还没装好"的环境里被打开阅读，而 import 会让它连带失败。

**本文件的所有结构性事实都来自测试库 `rednote_test_wp20` 的实测导出**
（`pg_attribute` / `pg_constraint` / `pg_indexes` / `pg_trigger` /
`pg_proc`），不是从代码抄的 —— 代码是意图，库是事实，两者不符时以库为准
并回头修代码。导出命令见第七节。

---

## 一、文件怎么用

1. 主 agent 用 `models.py` 生成迁移（`alembic revision --autogenerate`），
   然后用本文件**逐条核对**生成的 DDL 是否与预期一致 ——
   尤其是三处 `DEFERRABLE INITIALLY DEFERRED` 与一处部分索引
   （autogenerate **读不回部分索引的 WHERE** 时最容易漏）。
2. 迁移必须调 `ddl.all_trigger_ddl()` 把六个触发器一并建出。
   **其中 `trg_version_nodes_knowledge_paired` 挂在共用表 `version_nodes` 上**
   （它是 OP-88 的反向判据），详见第五节。
3. **升级与降级都必须只碰本模块自己的对象**（见第六节）。

## 二、与数据结构 02 的对应关系

来源：`Rednote研发计划_02_数据结构与一致性.md` 第 154~165 行。

| 数据结构 02 的表 | 本包是否落地 | 备注 |
|---|---|---|
| `knowledge_assets` | ✅ | 逻辑实体（一条知识） |
| `knowledge_versions` | ✅ | **不可变**；`id` 同时 FK `version_nodes.id` |
| `knowledge_publications` | ✅ | **不可变**；生效事件流 |
| `knowledge_chunks` | ✅ | **只建表，不生产**（生产归 WP21）；**刻意不是不可变表**，见 §四.3 |
| `knowledge_proposals` | ✅ | AI 候选，加了两列（`decision_reason` / `decided_at`），见 §四.2 |

## 三、建表顺序与拆环（**这一节是本文件存在的主要理由之一**）

依赖关系（`→` 读作"引用"）：

```
version_nodes            ← WP11 已建，本包不改它
    ↑                       ↑
    │ (id)                  │ (id)
knowledge_versions     knowledge_assets
    ↑ (asset_id)           │
    └──────────────────────┘
```

**建表顺序：`version_nodes`（已存在） → `knowledge_assets` → `knowledge_versions`
→ `knowledge_publications` → `knowledge_chunks` → `knowledge_proposals`。**

按 `created_at` 之外的依赖排就是上面这个序：`knowledge_assets` 只引用
`version_nodes` 与既有主数据表（`brands` / `products` / `projects` / `users`），
不引用 `knowledge_versions`；`knowledge_versions` 同时引用
`knowledge_assets` 与 `version_nodes`。**因此表级没有环**，
`CREATE TABLE` 按此序可一次成功，不需要 `ALTER TABLE ... ADD CONSTRAINT` 分两步。

### 3.1 那 `DEFERRABLE` 在这里管什么？

管**行级**的插入顺序，不管表的存在性：

| 外键 | 为什么必须 `DEFERRABLE INITIALLY DEFERRED` |
|---|---|
| `knowledge_versions.id → version_nodes.id` | 两行由**同一个事务、两个不同代码路径**写入（`versions.service.register_version` 内部有 SAVEPOINT + 重试，不暴露"节点行何时落库"）。若立即校验，应用必须猜对"先插节点还是先插版本"，而那个顺序不是应用能控制的 |
| `knowledge_assets.active_version_id → version_nodes.id` | 发布事务里"切换 `active_version_id`"与"插入新版本"的先后由业务流程决定；同理不可猜 |

**必须显式说明的一点**：`DEFERRABLE` 管的是**事务提交期**
（约束检查推迟到 `COMMIT` / `SET CONSTRAINTS ALL IMMEDIATE`），
**它管不了 DDL 期的表存在性** —— 一个 `DEFERRABLE` 外键仍然要求
**被引用的表在 `CREATE TABLE` 那一刻已经存在**。所以：
表的存在性靠第三节的顺序排，行的先后靠 `DEFERRABLE` 绕开，两者是两件事。

### 3.2 `version_nodes` 是共用表，本包只加触发器不改结构

`version_nodes` 由 WP11 建，被 `BRIEF`(WP18) / `KNOWLEDGE`(WP20) /
`BASELINE`(WP22) / `PROMPT`(WP23) 共用。本包对它做的**唯一**一件事是
新增一个触发器 `trg_version_nodes_knowledge_paired`，
且用 `WHEN (NEW.entity_type = 'KNOWLEDGE')` 收窄 ——
不加条件会让每次 `CONTENT` / `PLAN` / `PROMPT` 版本注册都白跑一次函数。
**不改列、不改索引、不改既有触发器。**
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 生成迁移时必须逐条核对的形状（本文件是 specs，不是可执行代码）
# ---------------------------------------------------------------------------

KNOWLEDGE_ASSETS_DDL = """
-- 表名：knowledge_assets。逻辑实体：**一条知识**（跨项目的组织级资产）。
-- 验收原文「归档可追溯，生效版本与旧引用并存」的第一个落点：
-- `active_version_id` 是**投影列**（由 publication 事件流算出），
-- 而不是"版本表上的一个 is_active 布尔"。
CREATE TABLE knowledge_assets (
    id                uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    category          varchar(30)  NOT NULL,
    title             varchar(200) NOT NULL,
    brand_id          uuid,          -- scope=BRAND 时必填（见 ck_*_scope_brand_needs_brand_id）
    product_id        uuid,          -- scope=PRODUCT 时必填
    project_id        uuid,          -- scope=PROJECT 时必填
    scope             varchar(20)  NOT NULL,
    active_version_id uuid,          -- 投影列：当前生效版本。DRAFT 时为 NULL
    archived_at       timestamptz,   -- 归档时刻（可追溯）。**不删行、不改旧版本**
    created_at        timestamptz  NOT NULL DEFAULT now(),
    updated_at        timestamptz  NOT NULL DEFAULT now(),
    created_by        uuid,

    CONSTRAINT pk_knowledge_assets PRIMARY KEY (id),

    -- 范围必须明确（规格备注「事实范围必须明确」）
    CONSTRAINT ck_knowledge_assets_category_valid CHECK (
        category IN ('FACT', 'FEEDBACK_EXPERIMENT', 'RESEARCH', 'RULE', 'SCENE', 'STYLE')
    ),
    CONSTRAINT ck_knowledge_assets_scope_valid CHECK (
        scope IN ('BRAND', 'GLOBAL', 'PRODUCT', 'PROJECT')
    ),
    CONSTRAINT ck_knowledge_assets_title_not_empty CHECK (char_length(title) > 0),

    -- 范围与限定列的**双向**约束：给了限定列就必须是对应的 scope，
    -- 且 GLOBAL 不许带任何限定列（否则"全局"这条知识其实只对一个产品有效）。
    CONSTRAINT ck_knowledge_assets_scope_brand_needs_brand_id CHECK (
        scope <> 'BRAND' OR brand_id IS NOT NULL
    ),
    CONSTRAINT ck_knowledge_assets_scope_product_needs_product_id CHECK (
        scope <> 'PRODUCT' OR product_id IS NOT NULL
    ),
    CONSTRAINT ck_knowledge_assets_scope_project_needs_project_id CHECK (
        scope <> 'PROJECT' OR project_id IS NOT NULL
    ),
    CONSTRAINT ck_knowledge_assets_scope_global_has_no_qualifier CHECK (
        scope <> 'GLOBAL' OR (brand_id IS NULL AND product_id IS NULL AND project_id IS NULL)
    ),

    -- **本包最重要的一条 CHECK**：`FACT` 不许是 GLOBAL 范围。
    -- 理由见说明文档 §1.1 —— "事实"必须能回答"对哪个产品/品牌成立"，
    -- 一个全局事实在下游会被不加区分地写进任何产品的文案里。
    CONSTRAINT ck_knowledge_assets_fact_requires_concrete_scope CHECK (
        category <> 'FACT' OR scope <> 'GLOBAL'
    ),

    -- 归档意味着"曾经生效过"。从未生效（DRAFT）就归档 = 一条没人见过的东西
    -- 出现在"归档可追溯"列表里，那会让归档列表失去意义。
    CONSTRAINT ck_knowledge_assets_archived_implies_had_version CHECK (
        archived_at IS NULL OR active_version_id IS NOT NULL
    ),

    -- 全部外键 ON DELETE RESTRICT：不可变表 + 落库即证据，不允许级联删除
    CONSTRAINT fk_knowledge_assets_brand_id_brands
        FOREIGN KEY (brand_id) REFERENCES brands (id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_assets_product_id_products
        FOREIGN KEY (product_id) REFERENCES products (id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_assets_project_id_projects
        FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_assets_created_by_users
        FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT,

    -- 延迟外键（见 §3.1）
    CONSTRAINT fk_knowledge_assets_active_version_id_version_nodes
        FOREIGN KEY (active_version_id) REFERENCES version_nodes (id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
);

-- 精确读取索引（规格源 §287 明写这是本包的索引）。
-- **部分索引 —— WHERE 必须逐条核对**：少了 WHERE 会让它退化成普通索引，
-- 而"生效版本为空"的行（大量 DRAFT）会白占空间且污染统计。
CREATE INDEX ix_knowledge_assets_fact_lookup
    ON knowledge_assets (product_id, category, active_version_id)
    WHERE active_version_id IS NOT NULL AND archived_at IS NULL;

-- 列表筛选用的两条（brand 维度 / product 维度各自配 category + archived_at）
CREATE INDEX ix_knowledge_assets_brand_category
    ON knowledge_assets (brand_id, category, archived_at);
CREATE INDEX ix_knowledge_assets_product_category
    ON knowledge_assets (product_id, category, archived_at);
CREATE INDEX ix_knowledge_assets_project_id ON knowledge_assets (project_id);
-- 游标分页的排序键是 (created_at DESC, id DESC)（platform.pagination 要求锚点唯一）
CREATE INDEX ix_knowledge_assets_created_at ON knowledge_assets (created_at);
"""


KNOWLEDGE_VERSIONS_DDL = """
-- 表名：knowledge_versions。**不可变**（trg_knowledge_versions_immutable）。
--
-- `id` **同时**是主键与 `version_nodes.id` 的外键 —— 与 `brief_versions` 同构。
-- 这样 WP11 的版本注册机制（跨包统一的版本树）能直接覆盖本包的知识版本。
CREATE TABLE knowledge_versions (
    id                  uuid          PRIMARY KEY,   -- ⚠️ 无 DEFAULT：id 由版本注册给出
    asset_id            uuid          NOT NULL,
    body                text          NOT NULL,
    structured_payload  jsonb         NOT NULL,
    source_segment_ids  uuid[]        NOT NULL DEFAULT '{}'::uuid[],
    effective_from      timestamp,                    -- 无时区：这是"业务生效日"，不是时刻
    effective_until     timestamp,
    change_reason       varchar(2000) NOT NULL,
    schema_version      integer       NOT NULL DEFAULT 1,
    created_at          timestamptz   NOT NULL DEFAULT now(),
    created_by          uuid,

    CONSTRAINT pk_knowledge_versions PRIMARY KEY (id),

    CONSTRAINT ck_knowledge_versions_body_not_empty CHECK (char_length(body) > 0),
    -- 200000 是本项目的正文上限（与文件解析的正文上限同量级）
    CONSTRAINT ck_knowledge_versions_body_length CHECK (char_length(body) <= 200000),
    CONSTRAINT ck_knowledge_versions_change_reason_not_empty CHECK (
        char_length(change_reason) > 0
    ),

    -- 规格备注「类别对应 Schema，**禁止无结构散文替代事实字段**」的数据库层落点。
    -- `structured_payload` 必须是**非空对象**：空对象 = 只有散文、没有字段。
    CONSTRAINT ck_knowledge_versions_structured_payload_is_object CHECK (
        jsonb_typeof(structured_payload) = 'object'
    ),
    CONSTRAINT ck_knowledge_versions_structured_payload_not_empty CHECK (
        structured_payload <> '{}'::jsonb
    ),
    CONSTRAINT ck_knowledge_versions_schema_version_positive CHECK (schema_version >= 1),

    -- 生效窗口要么不设，要么有序。**允许只有 effective_from**（"从某日开始生效"）
    -- 也允许只有 effective_until，但不允许"结束早于开始"。
    CONSTRAINT ck_knowledge_versions_effective_window_ordered CHECK (
        effective_until IS NULL
        OR effective_from IS NULL
        OR effective_from < effective_until
    ),

    CONSTRAINT fk_knowledge_versions_asset_id_knowledge_assets
        FOREIGN KEY (asset_id) REFERENCES knowledge_assets (id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_versions_created_by_users
        FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_versions_id_version_nodes
        FOREIGN KEY (id) REFERENCES version_nodes (id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX ix_knowledge_versions_asset_id ON knowledge_versions (asset_id);
"""


KNOWLEDGE_PUBLICATIONS_DDL = """
-- 表名：knowledge_publications。**不可变**（trg_knowledge_publications_immutable）。
--
-- 这是**事件流**，不是状态表。规格源 §277 原文：
-- 「插入 ACTIVATE 事件，切换 active_version，写索引 Outbox」。
-- "恢复通过新事件，不篡改旧状态历史"（数据结构 02 §12）就落在这里：
-- 恢复 = 追加一条 RESTORE 事件，而不是把某条 ARCHIVE 改成 RESTORE。
CREATE TABLE knowledge_publications (
    id          uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_id    uuid          NOT NULL,
    version_id  uuid          NOT NULL,
    action      varchar(20)   NOT NULL,
    sequence    integer       NOT NULL,   -- 每 asset 内递增；并发保护的**真正**串行化点
    actor_id    uuid,
    reason      varchar(1000),
    created_at  timestamptz   NOT NULL DEFAULT now(),

    CONSTRAINT pk_knowledge_publications PRIMARY KEY (id),

    -- SUPERSEDE 在取值域里（数据库允许），但**不在客户端可选动作里** ——
    -- 它是 ACTIVATE 的副作用（见 types.CLIENT_PUBLICATION_ACTIONS）。
    CONSTRAINT ck_knowledge_publications_action_valid CHECK (
        action IN ('ACTIVATE', 'ARCHIVE', 'RESTORE', 'SUPERSEDE')
    ),
    CONSTRAINT ck_knowledge_publications_sequence_positive CHECK (sequence >= 1),

    -- **并发保护的真正落点**：同一 asset 下 sequence 唯一。
    -- 两个运营同时发布时，后者拿到的 sequence 会撞唯一约束 → 服务层重试。
    CONSTRAINT uq_publications_asset_sequence UNIQUE (asset_id, sequence),

    CONSTRAINT fk_knowledge_publications_asset_id_knowledge_assets
        FOREIGN KEY (asset_id) REFERENCES knowledge_assets (id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_publications_version_id_knowledge_versions
        FOREIGN KEY (version_id) REFERENCES knowledge_versions (id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_publications_actor_id_users
        FOREIGN KEY (actor_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_knowledge_publications_asset_sequence
    ON knowledge_publications (asset_id, sequence);
CREATE INDEX ix_knowledge_publications_version_id
    ON knowledge_publications (version_id);
"""


KNOWLEDGE_CHUNKS_DDL = """
-- 表名：knowledge_chunks。**只建表，不生产**（生产归 WP21：切块与 embedding）。
--
-- ⚠️ **它刻意不是不可变表** —— 理由见说明文档 §1.5。
-- 一句话：它是**可重建的物化结果**（由 (版本内容, 切块器版本) 决定），
-- 不是"历史事实"。对它加"改了就是篡改历史"会让 WP21 无法实现
-- "模型换版不混向量"（换版时必须重切同一版本的块）。
-- 这与 `knowledge_versions` / `knowledge_publications` 的性质完全不同。
CREATE TABLE knowledge_chunks (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_version_id uuid       NOT NULL,
    chunk_no            integer     NOT NULL,
    text_content        text        NOT NULL,
    text_hash           varchar(64) NOT NULL,   -- sha256 hex
    token_count         integer     NOT NULL,
    locator             jsonb       NOT NULL,   -- {"page": 3, "offset": 120} 之类
    keywords            text[]      NOT NULL DEFAULT '{}'::text[],
    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pk_knowledge_chunks PRIMARY KEY (id),

    -- 「原知识变更需新版本重新切块」→ 同一版本内 chunk_no 唯一。
    -- 换版本 = 新 version_id，于是同一版本可以有稳定的 chunk_no 序列。
    CONSTRAINT uq_knowledge_chunks_version_chunk_no
        UNIQUE (knowledge_version_id, chunk_no),

    CONSTRAINT ck_knowledge_chunks_chunk_no_in_range CHECK (
        chunk_no >= 1 AND chunk_no <= 2147483647
    ),
    CONSTRAINT ck_knowledge_chunks_text_content_not_empty CHECK (
        char_length(text_content) > 0
    ),
    -- 定长 64 = sha256 hex。写错长度说明算法换了但没人知道。
    CONSTRAINT ck_knowledge_chunks_text_hash_length CHECK (char_length(text_hash) = 64),
    CONSTRAINT ck_knowledge_chunks_token_count_non_negative CHECK (token_count >= 0),
    CONSTRAINT ck_knowledge_chunks_locator_is_object CHECK (
        jsonb_typeof(locator) = 'object'
    ),

    CONSTRAINT fk_knowledge_chunks_knowledge_version_id_knowledge_versions
        FOREIGN KEY (knowledge_version_id) REFERENCES knowledge_versions (id)
        ON DELETE RESTRICT
);
"""


KNOWLEDGE_PROPOSALS_DDL = """
-- 表名：knowledge_proposals。规格备注：「AI 创建待确认，**不直接生效**」。
--
-- 本包只做**决策**（ACCEPT/REJECT）；候选的**产生**归 WP23/WP41
-- （依赖反馈与实验数据）。因此本包不写 source_feedback_id / source_experiment_id
-- 的生产者 —— 只把列建好，等那两个包来填。
CREATE TABLE knowledge_proposals (
    id                   uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id           uuid,
    source_feedback_id   uuid,          -- 由 WP23/WP41 填（本包不生产）
    source_experiment_id uuid,          -- 同上
    target_asset_id      uuid,          -- 改哪条已有知识；NULL = 新建
    proposed_payload     jsonb         NOT NULL,
    scope                varchar(20)   NOT NULL,
    state                varchar(20)   NOT NULL,
    confirmed_by         uuid,
    decision_reason      varchar(1000),  -- ⚠️ 本包新增列，见说明文档 §2.4
    decided_at           timestamp,      -- ⚠️ 本包新增列，同上
    created_at           timestamptz   NOT NULL DEFAULT now(),
    updated_at           timestamptz   NOT NULL DEFAULT now(),

    CONSTRAINT pk_knowledge_proposals PRIMARY KEY (id),

    CONSTRAINT ck_knowledge_proposals_state_valid CHECK (
        state IN ('ACCEPTED', 'PENDING', 'REJECTED')
    ),
    CONSTRAINT ck_knowledge_proposals_scope_valid CHECK (
        scope IN ('BRAND', 'GLOBAL', 'PRODUCT', 'PROJECT')
    ),

    -- 「不直接生效」的数据库层落点：payload 必须非空对象（空提案没有意义）。
    -- 注意这里**没有** FK 到 source_feedback_id / source_experiment_id ——
    -- 那两张表属于后面的包，现在建 FK 会指向不存在的表。
    CONSTRAINT ck_knowledge_proposals_proposed_payload_is_object CHECK (
        jsonb_typeof(proposed_payload) = 'object'
    ),
    CONSTRAINT ck_knowledge_proposals_proposed_payload_not_empty CHECK (
        proposed_payload <> '{}'::jsonb
    ),

    -- PENDING ⇔ 没有确认人。两条 CHECK 合起来是一条双条件：
    -- 未决的提案不能有确认人；已决的提案必须有确认人（谁拍的板要能查）。
    CONSTRAINT ck_knowledge_proposals_pending_has_no_confirmer CHECK (
        state <> 'PENDING' OR confirmed_by IS NULL
    ),
    CONSTRAINT ck_knowledge_proposals_settled_needs_confirmer CHECK (
        state = 'PENDING' OR confirmed_by IS NOT NULL
    ),
    -- 驳回必须说为什么（接受不必 —— 接受的结果在生成的新版本里）
    CONSTRAINT ck_knowledge_proposals_reject_needs_reason CHECK (
        state <> 'REJECTED'
        OR (decision_reason IS NOT NULL AND char_length(decision_reason) > 0)
    ),

    CONSTRAINT fk_knowledge_proposals_project_id_projects
        FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_proposals_target_asset_id_knowledge_assets
        FOREIGN KEY (target_asset_id) REFERENCES knowledge_assets (id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_proposals_confirmed_by_users
        FOREIGN KEY (confirmed_by) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_knowledge_proposals_state ON knowledge_proposals (state);
CREATE INDEX ix_knowledge_proposals_project_id ON knowledge_proposals (project_id);
CREATE INDEX ix_knowledge_proposals_target_asset_id ON knowledge_proposals (target_asset_id);
"""


# ---------------------------------------------------------------------------
# 四、本包对数据结构 02 的三处有意偏离（每一处都有理由，不是笔误）
# ---------------------------------------------------------------------------

DELIBERATE_DEVIATIONS = """
### 4.1 `knowledge_versions` 多两列

| 新增列 | 为什么 |
|---|---|
| `schema_version` (int, default 1) | 六类字段规格（`schema.FIELD_BY_PATH`）会演进。没有它，一份按旧规格填的 payload 与一份按新规格填的看起来完全一样，而"该按哪版规格校验它"就成了不可知的事。**当前恒为 1**（`KNOWLEDGE_PAYLOAD_SCHEMA_VERSION`） |

（`body` 的上限 200000 也是本包定的，数据结构 02 只写 `text`。）

### 4.2 `knowledge_proposals` 多两列

| 新增列 | 为什么 |
|---|---|
| `decision_reason` (varchar(1000)) | 驳回必须说理由。没有这一列，"为什么这条 AI 建议被否了"只能靠翻聊天记录 |
| `decided_at` (timestamp) | 决策时刻。`updated_at` 会被任何后续写入刷新，不能当作决策时间 |

### 4.3 `knowledge_chunks` **不加**不可变触发器（与另两张表相反）

`knowledge_versions` 与 `knowledge_publications` 是**历史事实**：改了它就是篡改历史。
`knowledge_chunks` 不是 —— 它是**由 (版本内容, 切块器版本, embedding 模型) 决定的物化结果**，
可以随时从版本内容重算出来。给它加不可变约束会让 WP21 无法实现
"换 embedding 模型时不混新旧向量"（那需要重切/重算同一版本的块）。

**判据（本项目通用）**：一张表加不可变，要看"这行是不是一次业务的证据"。
是证据 → 不可变；是派生结果 → 可重建，别锁。

因此 `IMMUTABLE_TABLES = ('knowledge_versions', 'knowledge_publications')`
**不含** `knowledge_chunks`。这是有意的，不是漏写。
"""


# ---------------------------------------------------------------------------
# 五、触发器清单（6 个）
# ---------------------------------------------------------------------------

TRIGGERS = """
| # | 触发器 | 挂在哪张表 | 类型 | 作用 |
|---|---|---|---|---|
| 1 | `trg_knowledge_versions_immutable` | `knowledge_versions` | BEFORE UPDATE/DELETE | 拒绝原地改写（复用 `rednote_reject_mutation()`） |
| 2 | `trg_knowledge_publications_immutable` | `knowledge_publications` | 同上 | 同上 |
| 3 | `trg_knowledge_versions_node_consistency` | `knowledge_versions` | **CONSTRAINT TRIGGER, DEFERRABLE INITIALLY DEFERRED** AFTER INSERT | OP-88 判据 1~3 |
| 4 | `trg_version_nodes_knowledge_paired` | **`version_nodes`（共用表）** | 同上（带 `WHEN (NEW.entity_type = 'KNOWLEDGE')`） | OP-88 反向判据：KNOWLEDGE 节点必须配对 `knowledge_versions` 行 |
| 5 | `trg_knowledge_publications_asset_scope` | `knowledge_publications` | 同上 | 发布事件引用的版本必须属于该 asset |
| 6 | `trg_knowledge_assets_active_scope` | `knowledge_assets` | 同上，AFTER UPDATE | 生效版本必须属于本知识 |

**#3~#6 必须 `DEFERRABLE INITIALLY DEFERRED`**：它们校验的都是
"同一事务内另一次 INSERT/UPDATE 的结果"，而顺序由应用与版本注册机制共同决定（见 §3.1）。

**#1~#2 共用同一个函数 `rednote_reject_mutation()`**（`platform.immutability`）：
各写一份会在 downgrade 时撞
"cannot drop function ... because other objects depend on it"（WP11 踩过、WP16 差点再踩）。

**#4 是本包唯一挂到非本模块表上的对象**：它不修改 WP11 的任何代码或表结构。

**#6 的 `WHEN` 与函数里的前置判断是两层收窄，都不能省**：
`AFTER UPDATE` 已经排除 INSERT，但函数里还要判断
`NEW.active_version_id IS NOT DISTINCT FROM OLD.active_version_id` ——
否则"更新标题"也会跑一次一致性查询。

### 5.1 四个一致性函数的实际判据（从库导出，逐句核对过）

```
rednote_check_knowledge_version_consistency  (ON knowledge_versions AFTER INSERT)
    version_nodes 里必须有一条 id = NEW.id 的行（否则 RAISE）
    且它的 entity_type 必须是 'KNOWLEDGE'（否则 RAISE）
    且它的 entity_id 必须 = NEW.asset_id（否则 RAISE）

rednote_check_knowledge_node_paired          (ON version_nodes AFTER INSERT, WHEN entity_type='KNOWLEDGE')
    ← 这条是本包**刻意收窄**的，见 5.2
    若 knowledge_assets 里**存在** id = NEW.entity_id 的行：
        则 knowledge_versions 里必须有 id = NEW.id 的行（否则 RAISE）
    若 knowledge_assets 里不存在该 asset：
        直接 RETURN NULL（放行）

rednote_check_knowledge_publication_scope    (ON knowledge_publications AFTER INSERT)
    knowledge_versions 里必须有 id = NEW.version_id 的行（否则 RAISE）
    且它的 asset_id 必须 = NEW.asset_id（否则 RAISE）

rednote_check_knowledge_active_scope         (ON knowledge_assets AFTER UPDATE)
    NEW.active_version_id 为 NULL → 放行
    NEW.active_version_id = OLD.active_version_id → 放行（没变就别查）
    否则：knowledge_versions 里必须有该版本行，且其 asset_id 必须 = NEW.id
```

### 5.2 **反向判据为什么必须收窄（OP-88 的关键，本机实测推翻了第一判断）**

`trg_version_nodes_knowledge_paired` 的字面表述是
"`KNOWLEDGE` 节点必须配对 `knowledge_versions` 行"。若照字面实现，
**它会拦下本包自己也不该拦的东西**：

`version_nodes` 是共用表。WP11 的版本注册在**建 asset 与建 version 之前**
就会先写节点行（或由别的包按自己的顺序写）。若不做 `entity_id` 存在性判断，
一个**尚未建立 asset 的节点**（合法的中间态）会被判成"孤立节点"而报错 ——
于是本包的触发器把别的包的版本注册路径也拦了。

**因此实现里加了前置判断**：只有 `entity_id` 在 `knowledge_assets` 里
真实存在时，才要求配对 `knowledge_versions` 行。这条不是"放宽"，
而是把判据限定在**本包确实负责的那一半语义**上：
"如果这个节点确实是在描述一条知识，那它必须有版本行"。

**这一处必须配对照组测试**（否则一个永远 `RAISE` 的触发器也能让正向/反向两条全绿）：
活性测试里既要有"该拦的拦"，也要有"该过的过"。
"""


# ---------------------------------------------------------------------------
# 六、downgrade 的归属判据
# ---------------------------------------------------------------------------

DOWNGRADE_RULES = """
`downgrade` **必须只删本模块自己的对象**，顺序与建表相反：

```sql
-- 1. 先删触发器（本包 6 个）
--    ⚠️ trg_version_nodes_knowledge_paired 挂在**共用表 version_nodes** 上，
--    但它是本包建的 → 本包负责删。绝不能 DROP TABLE version_nodes。
DROP TRIGGER IF EXISTS trg_version_nodes_knowledge_paired ON version_nodes;
DROP TRIGGER IF EXISTS trg_knowledge_versions_node_consistency ON knowledge_versions;
DROP TRIGGER IF EXISTS trg_knowledge_publications_asset_scope ON knowledge_publications;
DROP TRIGGER IF EXISTS trg_knowledge_assets_active_scope ON knowledge_assets;
DROP TRIGGER IF EXISTS trg_knowledge_versions_immutable ON knowledge_versions;
DROP TRIGGER IF EXISTS trg_knowledge_publications_immutable ON knowledge_publications;

-- 2. 本包的四个函数（不是共用函数，可以删）
DROP FUNCTION IF EXISTS rednote_check_knowledge_version_consistency();
DROP FUNCTION IF EXISTS rednote_check_knowledge_node_paired();
DROP FUNCTION IF EXISTS rednote_check_knowledge_publication_scope();
DROP FUNCTION IF EXISTS rednote_check_knowledge_active_scope();

-- ⚠️ 绝不要 DROP FUNCTION rednote_reject_mutation() —— 它属于 platform，
--    被全项目的不可变表共用。删它会连带毁掉 WP18/WP11 的不可变性。

-- 3. 再删表（反序）
DROP TABLE IF EXISTS knowledge_proposals;
DROP TABLE IF EXISTS knowledge_chunks;
DROP TABLE IF EXISTS knowledge_publications;
DROP TABLE IF EXISTS knowledge_versions;
DROP TABLE IF EXISTS knowledge_assets;
```

`ddl.consistency_items()` 存在的意义就是让第 1、2 步自动只覆盖本模块自己的对象，
而不是手写一遍（手写会漏、会多删）。

### 6.1 autogenerate 做不到、必须手工补的三件事

| # | 症状 | 手工改动 |
|---|---|---|
| 1 | **部分索引的 `WHERE` 丢失** —— autogenerate 会把它写成普通索引 | 按 §三 的 `ix_knowledge_assets_fact_lookup` 补回 `WHERE active_version_id IS NOT NULL AND archived_at IS NULL` |
| 2 | **触发器不被包含** | 迁移里调 `ddl.all_trigger_ddl()`。**且 `CREATE TRIGGER` 不是幂等的**（重复执行报 `DuplicateObject`）→ 必须 `DROP TRIGGER IF EXISTS` → `CREATE OR REPLACE FUNCTION` → `CREATE TRIGGER` 三步 |
| 3 | **`postgresql` 方言未导入**（用 `ARRAY` / `UUID` 类型时会 NameError） | 补 `from sqlalchemy.dialects import postgresql` |

**G2 三件事**（若产出迁移草案要一并处理）：删 M0 探针表的 `op.drop_table` /
删 downgrade 里的重建语句 / 补 `from sqlalchemy.dialects import postgresql`。
"""


# ---------------------------------------------------------------------------
# 七、结构事实的导出命令（复现本节所有"实测"说法）
# ---------------------------------------------------------------------------

VERIFICATION_COMMANDS = """
```bash
DB=rednote_test_wp20
# 列与默认值
psql -h localhost -U rednote -d $DB -At -F'|' -c "
SELECT c.relname, a.attname, format_type(a.atttypid,a.atttypmod), a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid)
FROM pg_class c
JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
WHERE c.relname LIKE 'knowledge_%' AND c.relkind='r'
ORDER BY c.relname, a.attnum;"

# 约束（含 CHECK 全文与 DEFERRABLE 标记）
psql -h localhost -U rednote -d $DB -At -F'|' -c "
SELECT conrelid::regclass::text, conname, contype, pg_get_constraintdef(oid)
FROM pg_constraint WHERE conrelid::regclass::text LIKE 'knowledge_%'
ORDER BY 1,3,2;"

# 索引（**重点看部分索引的 WHERE 在不在**）
psql -h localhost -U rednote -d $DB -At -F'|' -c "
SELECT tablename, indexname, indexdef FROM pg_indexes
WHERE tablename LIKE 'knowledge_%' ORDER BY 1,2;"

# 触发器（含挂到共用表上的那一个）
psql -h localhost -U rednote -d $DB -At -F'|' -c "
SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid)
FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
WHERE NOT t.tgisinternal AND (c.relname LIKE 'knowledge_%' OR c.relname='version_nodes')
ORDER BY 1,2;"

# 四个一致性函数体（**判据的唯一权威来源**）
psql -h localhost -U rednote -d $DB -At -c "
SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname IN (
  'rednote_check_knowledge_version_consistency',
  'rednote_check_knowledge_node_paired',
  'rednote_check_knowledge_publication_scope',
  'rednote_check_knowledge_active_scope') ORDER BY proname;"
```

**为什么必须导出而不是读代码**：`models.py` 里的 `CheckConstraint` 字符串
只说明意图；PostgreSQL 重写后的表达式才是实际生效的规则
（例如 `category IN (...)` 会变成 `(category)::text = ANY ((ARRAY[...])::text[])`）。
**两者不符时以库为准，并回头修代码。**
"""


# ---------------------------------------------------------------------------
# 八、统计（供收口核对）
# ---------------------------------------------------------------------------

COUNTS = """
| 项 | 数量 |
|---|---|
| 表 | 5 |
| 列（合计） | 53 |
| PRIMARY KEY | 5 |
| CHECK 约束 | 30 |
| UNIQUE 约束 | 2 |
| FOREIGN KEY | 15 |
| 索引行（`pg_indexes` 全部，含 PK/UQ 隐式索引） | 18 |
| 索引（显式 `CREATE INDEX`） | 11 |
| 触发器（本包建的） | 6 |
| 触发器（其中挂在非本模块表上） | 1 |
| 本包新增函数 | 4 |
| 复用的共用函数 | 1（`rednote_reject_mutation`） |

**列数明细**：`knowledge_assets` 11 + `knowledge_versions` 11 +
`knowledge_publications` 8 + `knowledge_chunks` 9 + `knowledge_proposals` 14 = **53**。

**CHECK 明细**：assets 9 + versions 7 + publications 2 + chunks 5 + proposals 7 = **30**。

**FK 明细**：assets 5 / versions 3 / publications 3 / chunks 1 / proposals 3 = **15**。
其中指向**共用表 `version_nodes`** 的两条（`fk_knowledge_assets_active_version_id_version_nodes`、
`fk_knowledge_versions_id_version_nodes`）是 `DEFERRABLE INITIALLY DEFERRED`，
其余 13 条指向本模块表或既有主数据表，均为立即检查 + `ON DELETE RESTRICT`。

**索引明细（显式 11 条）**：assets 5 / versions 1 / publications 2 /
chunks 0 / proposals 3。其中 `ix_knowledge_assets_fact_lookup` 是**部分索引**
（带 `WHERE`），是 autogenerate 最容易漏 `WHERE` 的那一条。
"""


if __name__ == "__main__":  # pragma: no cover
    # 本文件是**规格**，不是可执行代码。加这个 main 只是为了让
    # `python docs/m2/WP20-ddl-草案.py` 不报错并打印各节标题，
    # 方便主 agent 在终端里翻。
    for name, text in (
        ("KNOWLEDGE_ASSETS_DDL", KNOWLEDGE_ASSETS_DDL),
        ("KNOWLEDGE_VERSIONS_DDL", KNOWLEDGE_VERSIONS_DDL),
        ("KNOWLEDGE_PUBLICATIONS_DDL", KNOWLEDGE_PUBLICATIONS_DDL),
        ("KNOWLEDGE_CHUNKS_DDL", KNOWLEDGE_CHUNKS_DDL),
        ("KNOWLEDGE_PROPOSALS_DDL", KNOWLEDGE_PROPOSALS_DDL),
        ("DELIBERATE_DEVIATIONS", DELIBERATE_DEVIATIONS),
        ("TRIGGERS", TRIGGERS),
        ("DOWNGRADE_RULES", DOWNGRADE_RULES),
        ("VERIFICATION_COMMANDS", VERIFICATION_COMMANDS),
        ("COUNTS", COUNTS),
    ):
        print(f"===== {name} =====")
        print(text)