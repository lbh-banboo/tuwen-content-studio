"""WP29 DDL 草案：`content_feedback_items` / `content_feedback_events` / `content_reviews`。

**这不是迁移文件。** 迁移由主 agent 串行收口（接缝冻结 §4.2）。
本文件是**规格**：表名、列、类型、可空、默认值、全部约束与索引、外键、
触发器清单，逐条可核对。

**为什么用 `.py` 而不是 `.md`**：草案里的每一列 / 每个约束都对应
`modules/feedback/models.py` 的一行。用 Python 写，读者可以把两边并排比对；
写成散文则必须来回翻。**本文件不 import 本包任何模块** ——
它要能在"包还没装好"的环境里被打开阅读，而 import 会让它连带失败。
（与 `WP18-ddl-草案.py` / `WP20-ddl-草案.py` / `WP22-ddl-草案.py` 同款。）

**本文件的结构性事实来自 `models.py` + `ddl.py` 的代码意图**，尚未经过
验证库实测导出（WP29 的测试库 `rednote_test_wp29` 接缝冻结 §4.3 已分配，
本波未连库）。迁移落地后应补一次 `pg_attribute` / `pg_constraint` /
`pg_indexes` / `pg_trigger` 导出核对，与 WP22 草案第七节同款。

---

## 一、文件怎么用

1. 主 agent 用 `models.py` 生成迁移（`alembic revision --autogenerate`），
   然后用本文件**逐条核对**生成的 DDL。
2. 迁移必须调 `ddl.all_trigger_ddl()` 把**三个**触发器一并建出
   （两个不可变 + 一个锚点一致性）。
3. **升级与降级都必须只碰本模块自己的对象**：降级只删
   `consistency_function_names()` 列出的函数，**不含**跨包共用的
   `rednote_reject_mutation`（ISS-79：删它会让所有已建表的不可变守卫
   变成空触发器）。

## 二、三张表的角色与可变性

| 表 | 角色 | 可变性 | 触发器 |
|---|---|---|---|
| `content_feedback_items` | 一条反馈（锚点 + 分类 + 状态） | **可变**（`lock_version` 是 ETag 载体） | 锚点一致性（约束触发器，延迟） |
| `content_feedback_events` | 状态迁移历史 | **不可变**（只追加） | `trg_content_feedback_events_immutable` |
| `content_reviews` | 审核决定（通过 / 毙掉） | **不可变**（只追加） | `trg_content_reviews_immutable` |

**为什么反馈项可变、事件不可变**：反馈项是工作区（状态会前进），
事件是历史（"这个意见是怎么被处理的"必须能溯源）。改动反馈项状态时，
同一事务里追加一行事件 —— 投影与历史要么一起落库、要么一起回滚。
`content_reviews` 不可变意味着**改判靠追加一行 + `supersedes_id`**，
而不是原地改 `decision`。

## 三、建表顺序与拆环

```
projects / users / content_items / content_versions / media_items
        ↑   ↑         ↑               ↑                 ↑
        │   │         │               │                 │
        └───┴──── content_feedback_items ───────────────┘
                        ↑                    ↑
                        │ (feedback_item_id) │ (content_item_id)
                content_feedback_events   content_reviews
```

**建表顺序：`content_feedback_items` → `content_feedback_events`
→ `content_reviews`。**

三张表引用的都是**已存在**的表（`projects` / `users` / `content_items` /
`content_versions` / `media_items`），或本包内更早建的表。没有表级环。

**`content_reviews.supersedes_id` 是自引用**，且**必须
`DEFERRABLE INITIALLY DEFERRED`**：改判既可能"先插新行再回填旧行"、
也可能反过来，两侧都不该被插入顺序约束。与
`content_items.current_version_id` 同款（`models.py:362`）。

**`content_feedback_events.feedback_item_id` 不需要延迟**：事件行永远
在反馈项行之后写（`insert_feedback_item` 先 `add`、`insert_feedback_event`
后 `add`，同一次 flush 里 SQLAlchemy 按依赖排序）。

## 四、三张表的逐列 DDL（specs，不是可执行代码）

### 4.1 `content_feedback_items`

**锚点三选一**是全表最重要的约束组。`anchor_kind` + `paragraph_ref` +
`media_item_id` 三列表达"针对哪里"，三条 CHECK 各写"不是这种锚点，
或者列占用正确"，合起来互斥且完备：

| `anchor_kind` | `paragraph_ref` | `media_item_id` |
|---|---|---|
| `WHOLE` | NULL | NULL |
| `PARAGRAPH` | 非空 | NULL |
| `MEDIA` | NULL | 非空 |

`paragraph_ref` **刻意不加 FK**：段落不是表，它是正文切段后的产物
（`p<序号>-<8位哈希>`），只对"提意见时看到的那一版正文"有意义；
那一版由 `observed_version_id` 单独记录。

**`reject_reason` 的双向 CHECK**：`(state = 'REJECTED') = (reject_reason IS NOT NULL)`。
写成等值而不是两条单向条件，是因为"没拒绝却写了理由"同样是坏数据 ——
它会让列表页显示一个不存在的拒绝原因。
"""

CONTENT_FEEDBACK_ITEMS_DDL = """
-- 表名：content_feedback_items。一条反馈。**可变**（lock_version 是 ETag 载体）。
--
-- 约束命名遵循 platform/base.py 的 NAMING_CONVENTION：
--   CheckConstraint(name="anchor_kind_valid") → ck_content_feedback_items_anchor_kind_valid
--   ForeignKey 自动命名为 fk_<table>_<column_0_N_name>_<referred_table>
CREATE TABLE content_feedback_items (
    id                  uuid         NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid         NOT NULL,
    content_item_id     uuid         NOT NULL,
    observed_version_id uuid         NOT NULL,   -- 提意见时看到的是哪一版（必填）
    anchor_kind         varchar(20)  NOT NULL,
    paragraph_ref       varchar(200),            -- MAX_PARAGRAPH_REF_CHARS；无 FK（段落不是表）
    selected_text       text,                    -- 段落锚点时的选中原文
    media_item_id       uuid,                    -- 媒体锚点
    category            varchar(24)  NOT NULL,
    suggested_category  varchar(24),             -- AI 建议分类（用户可改）
    severity            varchar(16)  NOT NULL,   -- BLOCKING / WARNING / INFO
    scope               varchar(24)  NOT NULL,   -- CURRENT_ONLY / FUTURE_GENERATIONS
    state               varchar(20)  NOT NULL DEFAULT 'OPEN',
    author_kind         varchar(16)  NOT NULL,   -- USER / AI
    body                text         NOT NULL,
    reject_reason       varchar(500),            -- MAX_REJECT_REASON_CHARS；仅 REJECTED 时非空
    resolved_version_id uuid,                    -- 解决这条反馈时产出的版本
    created_by          uuid,
    created_at          timestamptz  NOT NULL DEFAULT now(),
    updated_at          timestamptz  NOT NULL DEFAULT now(),
    lock_version        bigint       NOT NULL DEFAULT 1,

    CONSTRAINT pk_content_feedback_items PRIMARY KEY (id),

    -- --- 取值域（与 types.py 的 frozenset 逐项一致） ---------------------
    CONSTRAINT ck_content_feedback_items_anchor_kind_valid
        CHECK (anchor_kind IN ('MEDIA', 'PARAGRAPH', 'WHOLE')),
    CONSTRAINT ck_content_feedback_items_category_valid
        CHECK (category IN ('DUPLICATE', 'FACT', 'MEDIA_CONSISTENCY', 'OTHER',
                            'SCENE', 'STRUCTURE', 'TECH_FORMAT', 'TONE')),
    CONSTRAINT ck_content_feedback_items_suggested_category_valid
        CHECK (suggested_category IS NULL OR suggested_category IN
               ('DUPLICATE', 'FACT', 'MEDIA_CONSISTENCY', 'OTHER',
                'SCENE', 'STRUCTURE', 'TECH_FORMAT', 'TONE')),
    CONSTRAINT ck_content_feedback_items_severity_valid
        CHECK (severity IN ('BLOCKING', 'INFO', 'WARNING')),
    CONSTRAINT ck_content_feedback_items_scope_valid
        CHECK (scope IN ('CURRENT_ONLY', 'FUTURE_GENERATIONS')),
    CONSTRAINT ck_content_feedback_items_state_valid
        CHECK (state IN ('IN_PROGRESS', 'OPEN', 'REJECTED', 'RESOLVED')),
    CONSTRAINT ck_content_feedback_items_author_kind_valid
        CHECK (author_kind IN ('AI', 'USER')),

    -- --- 锚点三选一：列占用必须与 anchor_kind 对得上 ----------------------
    CONSTRAINT ck_content_feedback_items_anchor_paragraph_shape
        CHECK ((anchor_kind <> 'PARAGRAPH')
               OR (paragraph_ref IS NOT NULL AND media_item_id IS NULL)),
    CONSTRAINT ck_content_feedback_items_anchor_media_shape
        CHECK ((anchor_kind <> 'MEDIA')
               OR (media_item_id IS NOT NULL AND paragraph_ref IS NULL)),
    CONSTRAINT ck_content_feedback_items_anchor_whole_shape
        CHECK ((anchor_kind <> 'WHOLE')
               OR (paragraph_ref IS NULL AND media_item_id IS NULL)),

    -- --- 文本边界 ---------------------------------------------------------
    CONSTRAINT ck_content_feedback_items_selected_text_length
        CHECK (selected_text IS NULL OR char_length(selected_text) <= 4000),
    CONSTRAINT ck_content_feedback_items_body_length
        CHECK (char_length(body) > 0 AND char_length(body) <= 2000),

    -- --- 拒绝理由：双向，避免"REJECTED 却没有理由"与"没拒绝却写了理由" ---
    CONSTRAINT ck_content_feedback_items_reject_reason_paired
        CHECK ((state = 'REJECTED') = (reject_reason IS NOT NULL)),

    -- --- 外键：全部 ON DELETE RESTRICT（落库即证据） ----------------------
    CONSTRAINT fk_content_feedback_items_project_id_projects
        FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_content_feedback_items_content_item_id_content_items
        FOREIGN KEY (content_item_id) REFERENCES content_items (id) ON DELETE RESTRICT,
    CONSTRAINT fk_content_feedback_items_observed_version_id_content_versions
        FOREIGN KEY (observed_version_id) REFERENCES content_versions (id) ON DELETE RESTRICT,
    CONSTRAINT fk_content_feedback_items_media_item_id_media_items
        FOREIGN KEY (media_item_id) REFERENCES media_items (id) ON DELETE RESTRICT,
    CONSTRAINT fk_content_feedback_items_resolved_version_id_content_versions
        FOREIGN KEY (resolved_version_id) REFERENCES content_versions (id) ON DELETE RESTRICT,
    CONSTRAINT fk_content_feedback_items_created_by_users
        FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT
);

-- 工作台 / 列表的读路径。
CREATE INDEX ix_content_feedback_items_content_state
    ON content_feedback_items (content_item_id, state);
CREATE INDEX ix_content_feedback_items_project_state
    ON content_feedback_items (project_id, state);
-- 按段落聚合反馈（"这一段被提过哪些意见"）。
CREATE INDEX ix_content_feedback_items_content_paragraph
    ON content_feedback_items (content_item_id, paragraph_ref);
CREATE INDEX ix_content_feedback_items_media
    ON content_feedback_items (media_item_id);
-- "这一版被提过哪些意见"（版本失效 / 重做的影响面）。
CREATE INDEX ix_content_feedback_items_observed_version
    ON content_feedback_items (observed_version_id);
"""

CONTENT_FEEDBACK_EVENTS_DDL = """
-- 表名：content_feedback_events。状态迁移历史。**不可变**（只追加）。
--
-- seq_no 是"本反馈项内的序号"，由仓储层在写事务内取 max+1；
-- 并发撞车由 uq_content_feedback_events_item_seq 兜住（调用方用 savepoint 包住
-- "取号 + 插入"，与 content/repository.next_version_no 同款手法）。
CREATE TABLE content_feedback_events (
    id                  uuid        NOT NULL DEFAULT gen_random_uuid(),
    feedback_item_id    uuid        NOT NULL,
    from_state          varchar(20),             -- 首行（创建）为 NULL
    to_state            varchar(20) NOT NULL,
    note                text,
    actor_kind          varchar(16) NOT NULL,    -- USER / AI
    actor_id            uuid,                    -- AI 自己迁移时为空
    resulting_version_id uuid,                   -- 这次迁移产出的版本
    seq_no              integer     NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pk_content_feedback_events PRIMARY KEY (id),

    CONSTRAINT ck_content_feedback_events_actor_kind_valid
        CHECK (actor_kind IN ('AI', 'USER')),

    -- 同一反馈项内序号唯一（唯一约束自带索引，不另建普通索引）。
    CONSTRAINT uq_content_feedback_events_feedback_item_id_seq_no
        UNIQUE (feedback_item_id, seq_no),

    CONSTRAINT fk_content_feedback_events_feedback_item_id_content_feedback_items
        FOREIGN KEY (feedback_item_id) REFERENCES content_feedback_items (id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_content_feedback_events_actor_id_users
        FOREIGN KEY (actor_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_content_feedback_events_resulting_version_id_content_versions
        FOREIGN KEY (resulting_version_id) REFERENCES content_versions (id)
        ON DELETE RESTRICT
);

-- 不另建 feedback_item_id 的普通索引：唯一约束
-- (feedback_item_id, seq_no) 的左前缀已经覆盖"按反馈项取历史"的查询。
"""

CONTENT_REVIEWS_DDL = """
-- 表名：content_reviews。一次审核决定。**不可变**（只追加）；改判靠
-- 追加一行 + supersedes_id 指回旧决定。
CREATE TABLE content_reviews (
    id                  uuid        NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid        NOT NULL,
    content_item_id     uuid        NOT NULL,
    subject_version_id  uuid        NOT NULL,    -- 审核结论永远绑定到具体版本
    decision            varchar(20) NOT NULL,    -- APPROVED / ELIMINATED
    reason              text        NOT NULL,
    decided_by          uuid,
    decided_at          timestamptz NOT NULL DEFAULT now(),
    supersedes_id       uuid,                    -- 自引用，延迟外键
    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pk_content_reviews PRIMARY KEY (id),

    CONSTRAINT ck_content_reviews_decision_valid
        CHECK (decision IN ('APPROVED', 'ELIMINATED')),
    CONSTRAINT ck_content_reviews_reason_not_blank
        CHECK (char_length(reason) > 0),

    CONSTRAINT fk_content_reviews_project_id_projects
        FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_content_reviews_content_item_id_content_items
        FOREIGN KEY (content_item_id) REFERENCES content_items (id) ON DELETE RESTRICT,
    CONSTRAINT fk_content_reviews_subject_version_id_content_versions
        FOREIGN KEY (subject_version_id) REFERENCES content_versions (id) ON DELETE RESTRICT,
    CONSTRAINT fk_content_reviews_decided_by_users
        FOREIGN KEY (decided_by) REFERENCES users (id) ON DELETE RESTRICT,
    -- ⚠️ 必须 DEFERRABLE INITIALLY DEFERRED（见第三节）：
    --    改判可以在同一事务里先插新行再回填旧行，也可以反过来。
    CONSTRAINT fk_content_reviews_supersedes_id_content_reviews
        FOREIGN KEY (supersedes_id) REFERENCES content_reviews (id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
);

-- 审核时间线：项目 / 内容下的决定倒序读（索引名由 models.py 显式给定）。
CREATE INDEX ix_content_reviews_content_decided
    ON content_reviews (content_item_id, decided_at);
CREATE INDEX ix_content_reviews_project_decided
    ON content_reviews (project_id, decided_at);
CREATE INDEX ix_content_reviews_subject_version
    ON content_reviews (subject_version_id);
"""

# ---------------------------------------------------------------------------
# 五、触发器清单（三个）
# ---------------------------------------------------------------------------

TRIGGERS = (
    {
        "table": "content_feedback_events",
        "name": "trg_content_feedback_events_immutable",
        "timing": "BEFORE UPDATE OR DELETE, FOR EACH ROW",
        "function": "rednote_reject_mutation()",  # 跨包共用，downgrade 不删
        "why": (
            "状态历史一旦写入即不可改。与 content_versions / export_items "
            "同一条纪律（数据结构文档 02 §1）。"
        ),
    },
    {
        "table": "content_reviews",
        "name": "trg_content_reviews_immutable",
        "timing": "BEFORE UPDATE OR DELETE, FOR EACH ROW",
        "function": "rednote_reject_mutation()",  # 跨包共用，downgrade 不删
        "why": (
            "审核结论要能溯源：改判是追加一行并用 supersedes_id 指回旧决定，"
            "不是原地改 decision。"
        ),
    },
    {
        "table": "content_feedback_items",
        "name": "trg_content_feedback_items_anchor_consistency",
        "timing": (
            "AFTER INSERT, FOR EACH ROW, CONSTRAINT TRIGGER "
            "DEFERRABLE INITIALLY DEFERRED"
        ),
        "function": "rednote_check_feedback_anchor_consistency()",
        "why": (
            "三条判据：media_item_id（可空）必须属于 NEW.content_item_id；"
            "observed_version_id（必填）必须属于同一条内容；"
            "resolved_version_id（可空）必须属于同一条内容。"
            "FK 只保证被引用的行存在，不保证它属于同一条内容 —— "
            "没有触发器时，一条反馈可以引用另一条内容的图。"
        ),
    },
)

#: 本包**自己的**函数（downgrade 只删这些）。`rednote_reject_mutation` 是跨包
#: 共用函数，**不在此列** —— 删它会让所有已建表的不可变守卫静默失效（ISS-79）。
OWN_FUNCTIONS = ("rednote_check_feedback_anchor_consistency",)

# ---------------------------------------------------------------------------
# 六、迁移核对清单
# ---------------------------------------------------------------------------

CHECKLIST = (
    "三张表的 CHECK 名带 ck_<table>_ 前缀（NAMING_CONVENTION），"
    "逐个核对取值域与 types.py 的 frozenset 一致。",
    "content_feedback_items 的三条锚点 CHECK 必须齐全 —— 少一条就让"
    "「MEDIA 不带 mediaItemId」这类坏数据落库。",
    "reject_reason_paired 是双向等值 CHECK，不是两条单向 IS NULL 判据。",
    "content_reviews.supersedes_id 的 FK 必须 DEFERRABLE INITIALLY DEFERRED。",
    "全部 FK 必须 ON DELETE RESTRICT（本包没有任何级联删除）。",
    "content_feedback_events(feedback_item_id, seq_no) 的唯一约束必须存在 ——"
    "它是并发取号的最后一道。",
    "三个触发器由 ddl.all_trigger_ddl() 建出；不可变触发器复用"
    "rednote_reject_mutation()，锚点一致性触发器是 CONSTRAINT TRIGGER 且延迟。",
    "downgrade 只删 OWN_FUNCTIONS，不删 rednote_reject_mutation。",
)
