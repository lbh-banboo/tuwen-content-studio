"""WP34 DDL 草案：`export_validity_events`（**仅此一张新表，现有表零新列**）。

**这不是迁移文件。** 迁移由主 agent 串行收口（接缝冻结 §4.2：…→ WP33 → **WP34**）；本包设计阶段
**不建 `models.py` / `ddl.py`、不连库、不生成迁移**。本文件是**规格**：表名、列、类型、可空、
默认值、全部约束与索引、外键、触发器，逐条可核对。
体例同 `WP30-ddl-草案.py` / `WP33-ddl-草案.py`：**本文件不 import 本包任何模块**，只含字符串常量与元组。

⚠️ **诚实声明（与 WP33 草案同款）**：`rednote_test_wp34` 在本机不存在（设计时实测
`pg_database` 无此库），`modules/versions/impact/` 只有一个空 `__init__.py`。
⇒ 下方 DDL 与 plpgsql **没有被任何数据库执行过**，仅对照了既有表（`export_items` /
`version_nodes` / `users`）的列名、命名与约束惯例手写。实现落地后必须：
① 用 `models.py` 生成 DDL 并与本文件逐条比对；② 在验证库上导出 `pg_constraint` /
`pg_indexes` / `pg_trigger` 三项核对；③ 再回写本文件。

---

## 一、文件怎么用

1. 主 agent 用实现期的 `models.py` 生成迁移（`alembic revision --autogenerate`），再用本文件
   **逐条核对**（G2 三件事：删探针表 drop / 重写 downgrade / 补 postgresql 导入）。
2. 迁移必须调 `ddl.all_trigger_ddl()` 把触发器一并建出（见第四节：1 个不可变 + 1 个一致性）。
3. **升级与降级都只碰本模块自己的对象**：降级只删 `OWN_FUNCTIONS` 列出的 1 个函数，
   **不含**跨包共用的 `rednote_reject_mutation`（ISS-79）。
4. **取值域全部写成字面量**（ISS-191）：迁移里**不得** `from rednote.modules.versions.impact.types
   import ...` 再循环它。本包引入 3 个**闭集**取值域（`transition` / `reason` / `scope_kind`），
   在 `check_migration_value_domain_no_live_import.py` 射程内。
5. 同步点（均为共享文件，**分支不改**，由主 agent 做）：`migrations/env.py` import
   `versions.impact.models`；`main.py` include_router；迁移序接在 WP33 之后。
6. 约束命名遵循 `platform/base.py::NAMING_CONVENTION`（`ck_<table>_<name>` /
   `fk_<table>_<col0_N>_<referred>` / `uq_` / `ix_` / `pk_`）。本草案所有名字已脚本实算
   ≤ 63 字节（见 `CHECKLIST` 末条；其中 `observed_version_id` 列名是为压到 59 字节而取，
   取 `observed_version_node_id` 会得到 64 字节被 PG **静默截断**）。
7. **入向外键提示**：本表 FK 指向 `export_items`（WP35 的不可变表）。这是一条**新增的跨包入向 FK**
   （WP36 的 `deliveries.export_item_id` 是先例）。它改变 `conftest.truncate_targets /
   cross_package_foreign_keys` 的闭包计算（清 `export_items` 前必须先清本表），见第六节。

## 二、一张表的角色与可变性

| 表 | 角色 | 可变性 | 触发器 |
|---|---|---|---|
| `export_validity_events` | 某条导出行（`export_items` 一行）的资格失效/恢复事件日志；当前态 = 该行最大 `seq` 那一条的 `transition`（无事件 = VALID） | **不可变，追加写** | `trg_export_validity_events_immutable` + `trg_export_validity_events_consistency`（AFTER INSERT，即时） |

**为什么不是"在 export_items 上标记"**：`export_items` 受 `trg_export_items_immutable` 保护
（`BEFORE UPDATE OR DELETE`），且 `modules/exports/*.py` 是共享文件第 24 行（禁改）。
本表只对 `export_items` 做 **INSERT 时的外键存在性检查**（PG 对被引用行取 `FOR KEY SHARE` 行锁，
不是 UPDATE，不触发 `BEFORE UPDATE` 触发器，不改被引用行的 `xmin`）——
⚠️ 这条是 PG 语义推断，**本草案未实测**，由测试 T-EXP-01（断言 `export_items` 行 `xmin` 不变）兜底证明。

**为什么是事件日志而不是可变状态表**：状态会来回翻（INVALIDATED → 回退到被冻结版本 → RESTORED → 再改稿 →
INVALIDATED）。可变列只留终态，"这条导出何时、因何失效过"无从回答；事件日志逐次留痕，
当前态由最大 `seq` 回答。**取当前态用 `seq` 而不是时间戳**：同一事务内多行时间戳相同（WP30 同款论证）；
`seq` 由触发器钉死"从 1 连续、INVALIDATED/RESTORED 严格交替"，所以最大 `seq` 恒唯一且有意义。
（这与契约 §2 禁用的 `ORDER BY version_no DESC` 不是一回事：后者是对**可被投影改指的版本链**取末版；
本表没有投影，且 `seq` 的单调连续由触发器保证。）

**刻意没有的列**：
· `project_id`：经 `export_items.project_id` 取得；冗余列需要一致性触发器（WP36 为此写了一个），本表不为省一次 JOIN 付这个代价。
· `content_item_id` / `frozen_version_id` / `frozen_eligibility_hash`：同上，均可经 FK 取得，不复制。
· `updated_at` / `lock_version`：不可变表不带（同 `version_nodes` 论证）。
· `status` 汇总列：当前态是派生值，存了就有"汇总与明细双声称"（WP30 R6）。
"""

# ---------------------------------------------------------------------------
# 三、逐列 DDL（specs，不是可执行代码）
# ---------------------------------------------------------------------------

EXPORT_VALIDITY_EVENTS_DDL = """
-- 表名：export_validity_events。导出行资格失效/恢复事件。**不可变**（追加写）。
--
-- 约束命名遵循 platform/base.py 的 NAMING_CONVENTION。
-- 本表最长的约束名是 fk_export_validity_events_observed_version_id_version_nodes（59 字节），
-- 在 PostgreSQL 63 字节上限内。
CREATE TABLE export_validity_events (
    id                       uuid         NOT NULL DEFAULT gen_random_uuid(),
    export_item_id           uuid         NOT NULL,   -- 指向 WP35 的 export_items.id（只读引用）
    seq                      integer      NOT NULL,   -- 该导出行的事件序号，从 1 连续
    transition               varchar(12)  NOT NULL,   -- INVALIDATED / RESTORED（字面量化石）
    reason                   varchar(24),             -- INVALIDATED 时必填：VERSION_SUPERSEDED / ASSET_CHANGED；RESTORED 时 NULL
    observed_version_id      uuid         NOT NULL,   -- 观测时该内容的当前正文版本（version_nodes.id）
    observed_eligibility_hash varchar(64) NOT NULL,   -- 观测时重算的资格指纹（小写十六进制）
    scope_kind               varchar(16)  NOT NULL,   -- CONTENTS / FROM_VERSION（本次扫描的入口）
    source_version_id        uuid,                    -- 仅 FROM_VERSION：影响图遍历的起点版本；其余 NULL
    created_by               uuid         NOT NULL,   -- 发起扫描的用户（export_items.created_by 同为 NOT NULL）
    created_at               timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT pk_export_validity_events PRIMARY KEY (id),

    CONSTRAINT ck_export_validity_events_seq_positive
        CHECK (seq >= 1),
    CONSTRAINT ck_export_validity_events_transition_valid
        CHECK (transition IN ('INVALIDATED', 'RESTORED')),
    -- reason 是闭集；注意这里有取值域 CHECK（与 WP33 的开集 reason 不同）：本表 reason 只由本包
    -- 的判定函数产出，没有"借用他人词汇"。放开它必须走一次迁移，是有意的。
    CONSTRAINT ck_export_validity_events_reason_valid
        CHECK (reason IS NULL OR reason IN ('VERSION_SUPERSEDED', 'ASSET_CHANGED')),
    -- 双向等价：失效必有原因，恢复必无原因。
    CONSTRAINT ck_export_validity_events_reason_paired
        CHECK ((transition = 'INVALIDATED') = (reason IS NOT NULL)),
    CONSTRAINT ck_export_validity_events_scope_kind_valid
        CHECK (scope_kind IN ('CONTENTS', 'FROM_VERSION')),
    -- 双向等价：只有 FROM_VERSION 入口才有 source_version_id。
    CONSTRAINT ck_export_validity_events_scope_source_paired
        CHECK ((scope_kind = 'FROM_VERSION') = (source_version_id IS NOT NULL)),
    CONSTRAINT ck_export_validity_events_observed_hash_shape
        CHECK (char_length(observed_eligibility_hash) = 64
               AND observed_eligibility_hash ~ '^[0-9a-f]+$'),

    -- 同一导出行的事件序号唯一：并发追加的唯一防线（后到者撞 23505，重读后重算）。
    -- 它同时是 (export_item_id, seq) 的查询索引，取"最新一条"走它的反向扫描，不另建索引。
    CONSTRAINT uq_export_validity_events_export_item_id_seq
        UNIQUE (export_item_id, seq),

    CONSTRAINT fk_export_validity_events_export_item_id_export_items
        FOREIGN KEY (export_item_id) REFERENCES export_items (id) ON DELETE RESTRICT,
    CONSTRAINT fk_export_validity_events_observed_version_id_version_nodes
        FOREIGN KEY (observed_version_id) REFERENCES version_nodes (id) ON DELETE RESTRICT,
    CONSTRAINT fk_export_validity_events_source_version_id_version_nodes
        FOREIGN KEY (source_version_id) REFERENCES version_nodes (id) ON DELETE RESTRICT,
    CONSTRAINT fk_export_validity_events_created_by_users
        FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT
);
"""

# ---------------------------------------------------------------------------
# 四、触发器
# ---------------------------------------------------------------------------

#: 不可变触发器：直接复用 `platform.immutability.immutable_trigger_ddl(table)`，
#: 命名 `trg_<table>_immutable`，共用函数 `rednote_reject_mutation()`（**跨包共用，不得 DROP**）。
#: 拒绝时 SQLSTATE = 23001（`IMMUTABLE_RECORD_SQLSTATE`）。
IMMUTABLE_TABLES = ("export_validity_events",)

#: 一致性触发器的 plpgsql 草案（**未执行过**，实现期写进 `ddl.py` 后以真库核对）。
#: AFTER INSERT、即时（非延迟）：每条事件是自洽的单行事实，不需要等同事务其他行就位，
#: 即时检查让错误在 flush 处抛出，service 层可立刻翻译。
#: RAISE 用字符串拼接而不用 `%` 占位符（psycopg paramstyle 会把 `%` 当占位符，真踩过）。
CONSISTENCY_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION rednote_check_export_validity_event() RETURNS trigger AS $$
DECLARE
    frozen_version uuid;
    frozen_hash    varchar(64);
    prev_seq       integer;
    prev_transition varchar(12);
BEGIN
    SELECT version_node_id, eligibility_hash INTO frozen_version, frozen_hash
      FROM export_items WHERE id = NEW.export_item_id;

    -- ① 序号连续、交替：首条必须是 seq=1 的 INVALIDATED（无事件 = VALID，不能先 RESTORED）；
    --    其后每条 seq = 上一条 + 1，且 transition 与上一条不同。
    SELECT seq, transition INTO prev_seq, prev_transition
      FROM export_validity_events
     WHERE export_item_id = NEW.export_item_id AND id <> NEW.id
     ORDER BY seq DESC LIMIT 1;
    IF NOT FOUND THEN
        IF NEW.seq <> 1 OR NEW.transition <> 'INVALIDATED' THEN
            RAISE EXCEPTION USING
                MESSAGE = '导出行 ' || NEW.export_item_id::text
                          || ' 的首条事件必须是 seq=1 的 INVALIDATED',
                ERRCODE = '23514';
        END IF;
    ELSIF NEW.seq <> prev_seq + 1 OR NEW.transition = prev_transition THEN
        RAISE EXCEPTION USING
            MESSAGE = '导出行 ' || NEW.export_item_id::text
                      || ' 的事件序号不连续或未交替（上一条 seq=' || prev_seq::text || '）',
            ERRCODE = '23514';
    END IF;

    -- ② 事件与被冻结的事实相符（FK 只保证 export_items 行存在，不保证说的是真话）：
    IF NEW.transition = 'INVALIDATED' THEN
        -- 失效 ⇒ 观测指纹必须不等于冻结指纹（否则"失效"无依据）
        IF NEW.observed_eligibility_hash = frozen_hash THEN
            RAISE EXCEPTION USING
                MESSAGE = 'INVALIDATED 事件的观测指纹与冻结指纹相同',
                ERRCODE = '23514';
        END IF;
        -- 原因与版本自洽：VERSION_SUPERSEDED ⇒ 当前版本已不是冻结版本；ASSET_CHANGED ⇒ 版本未变
        IF NEW.reason = 'VERSION_SUPERSEDED' AND NEW.observed_version_id = frozen_version THEN
            RAISE EXCEPTION USING
                MESSAGE = 'VERSION_SUPERSEDED 但观测版本仍等于冻结版本',
                ERRCODE = '23514';
        END IF;
        IF NEW.reason = 'ASSET_CHANGED' AND NEW.observed_version_id <> frozen_version THEN
            RAISE EXCEPTION USING
                MESSAGE = 'ASSET_CHANGED 但观测版本已不是冻结版本',
                ERRCODE = '23514';
        END IF;
    ELSE
        -- 恢复 ⇒ 观测指纹与版本都必须回到冻结值
        IF NEW.observed_eligibility_hash <> frozen_hash
           OR NEW.observed_version_id <> frozen_version THEN
            RAISE EXCEPTION USING
                MESSAGE = 'RESTORED 事件的观测值未回到冻结值',
                ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

CONSISTENCY_TRIGGER_DDL = """
CREATE TRIGGER trg_export_validity_events_consistency
AFTER INSERT ON export_validity_events
FOR EACH ROW
EXECUTE FUNCTION rednote_check_export_validity_event()
"""

TRIGGERS = (
    {
        "table": "export_validity_events",
        "name": "trg_export_validity_events_consistency",
        "timing": "AFTER INSERT, FOR EACH ROW, 即时（非延迟）",
        "function": "rednote_check_export_validity_event()",
        "sqlstate": "23514",
        "why": (
            "① seq 从 1 连续、INVALIDATED/RESTORED 严格交替、首条必为 INVALIDATED；"
            "② INVALIDATED 的观测指纹 ≠ 冻结指纹、reason 与『观测版本是否等于冻结版本』自洽；"
            "③ RESTORED 的观测指纹与版本都回到冻结值。"
            "FK 只保证 export_items 行存在；缺了这个触发器，裸 SQL 能插出『声称失效但指纹其实相同』"
            "或『序号跳号』的事件，当前态（最大 seq）从此不可信。"
        ),
    },
)

#: 本包**自己的**函数（downgrade 只删这 1 个）。`rednote_reject_mutation` 是跨包共用函数，
#: **不在此列** —— 删它会让所有已建表的不可变守卫静默失效（ISS-79）。
OWN_FUNCTIONS = ("rednote_check_export_validity_event",)

# ---------------------------------------------------------------------------
# 五、取值域（迁移里写成字面量；模型侧在 `impact/types.py` 各写一份活常量，测试对账）
# ---------------------------------------------------------------------------

VALUE_DOMAINS = {
    "export_validity_events.transition": ("INVALIDATED", "RESTORED"),
    "export_validity_events.reason": ("VERSION_SUPERSEDED", "ASSET_CHANGED"),
    "export_validity_events.scope_kind": ("CONTENTS", "FROM_VERSION"),
}

#: 仅出现在 API 响应/服务层的取值域，**不落库、不进迁移**（列在这里是为了让主 agent 一眼看清
#: 『哪些词汇是落库的、哪些不是』；它们若落库就必须补 CHECK 与本节条目）。
RESPONSE_ONLY_DOMAINS = {
    "validity_state": ("VALID", "INVALID", "UNKNOWN"),          # UNKNOWN 永不落库
    "impact_class": ("REVIEW_REQUIRED", "HISTORICAL", "UNRESOLVED"),
    "export_impact": ("FROZEN_ON_DEPARTING", "FROZEN_ON_DOWNSTREAM", "FROZEN_ON_TARGET"),
    "truncation": ("NODE_LIMIT", "DEPTH_LIMIT"),
    "anomaly": ("DEPENDENCY_CYCLE",),
}

# ---------------------------------------------------------------------------
# 六、对现有对象的影响（本包刻意把它降到最低）
# ---------------------------------------------------------------------------

#: 现有表上新增的列：**无**。
NEW_COLUMNS_ON_EXISTING_TABLES: tuple = ()

#: 本包读取但不改的既有表（FK 目标或裸 SQL 只读）。
READ_ONLY_EXISTING_TABLES = (
    "export_items",          # FK 目标 + 裸 SQL 读（列：id/project_id/export_group_id/content_item_id/version_node_id/eligibility_hash）
    "version_nodes",         # FK 目标 + 遍历读
    "version_dependencies",  # 遍历读（唯一边数据源，见说明文档 §2.2 / OQ-34-01）
    "users",                 # FK 目标
    "content_items",         # 裸 SQL 读 current_version_id（当前性判定）
    "content_versions",      # 裸 SQL 读 content_item_id
    "media_items",           # 裸 SQL 读 current_version_id（当前性判定）
    "media_versions",        # 裸 SQL 读 media_item_id
)

CROSS_PACKAGE_NOTE = """
出向 FK 只指向：export_items / version_nodes / users（全部已存在于迁移链）。

**入向 FK**：无他表引用本表。但本表对 export_items 的出向 FK 对测试夹具有影响：
清空 export_items 前必须先清 export_validity_events。`tests/conftest.py` 的
`truncate_targets / cross_package_foreign_keys`（WP-DEBT-05 迁到 platform/db_graph.py）按 FK 闭包算，
理应自动覆盖；但 conftest 是共享文件第 18 行（禁改），实现期必须以真库验证该闭包把本表算进去了
（WP36 的 deliveries.export_item_id 是同形先例，可对照其测试）。

不引用 WP30/31/32/33 的任何表（它们的新节点与依赖边只经 version_nodes / version_dependencies 进入遍历，
不经表间 FK）。
"""

# ---------------------------------------------------------------------------
# 七、迁移核对清单
# ---------------------------------------------------------------------------

CHECKLIST = (
    "export_validity_events 的 CHECK 名带 ck_<table>_ 前缀（NAMING_CONVENTION），"
    "transition / reason / scope_kind 三个闭集与 types.py 的 frozenset 一致；迁移里取值域写字面量（ISS-191）。",
    "★ 两条双向等价式必须齐全：reason_paired（INVALIDATED ⇔ reason 非空）与 "
    "scope_source_paired（FROM_VERSION ⇔ source_version_id 非空）；少一条就让 NULL 与『有值』同形。",
    "★ UNIQUE(export_item_id, seq) 必须存在（并发追加的唯一防线）；不得再为 (export_item_id, seq DESC) "
    "建表达式降序索引（autogenerate 读不回表达式，WP11 同款论证）。",
    "全部 FK 必须 ON DELETE RESTRICT；本表无延迟 FK（单表自洽，无环）。",
    "触发器：1 个不可变（复用 rednote_reject_mutation，23001）+ 1 个一致性（AFTER INSERT 即时，23514）。",
    "downgrade 只删 OWN_FUNCTIONS 这 1 个函数，**不删** rednote_reject_mutation（ISS-79）；"
    "且先 DROP 本表再 DROP 函数（表依赖函数）。",
    "迁移不得对 export_items 做任何 ALTER（现有表零新列）；往返判据 upgrade head → downgrade base → "
    "upgrade head 之后 export_items 的列/约束/触发器与迁移前逐项相同。",
    "迁移 down_revision 接 WP33 的迁移（接缝冻结 §4.2 末位）；若 WP33 被降级不做，改接 WP32，"
    "并同步说明文档 OQ-34-12。",
    "全部约束/索引/函数名在 63 字节内（本草案已脚本实算：最长为 "
    "fk_export_validity_events_observed_version_id_version_nodes = 59 字节；"
    "若把列名写成 observed_version_node_id 则为 64 字节，会被 PG 静默截断）。",
    "往返判据 + parity 判据（ORM ⇄ 迁移）+ check_migration_value_domain_no_live_import.py 覆盖本包。",
    "验证库核对（本草案未做）：实现落地后在 rednote_test_wp34 上导出 pg_constraint、pg_indexes、"
    "pg_trigger 三项，与本文件逐条比对，再回写本文件；并实测第二节标 ⚠️ 的 FOR KEY SHARE 推断。",
)
