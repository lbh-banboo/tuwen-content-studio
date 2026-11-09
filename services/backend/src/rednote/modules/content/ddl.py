"""WP26 的触发器 DDL + 索引清单来源。

**为什么 DDL 生成放在模块里而不是迁移文件里**（与 WP11/WP18/WP20/WP22/WP23/WP24/WP25 同一理由）：
验证脚本要按名字回查 `pg_trigger` / `pg_indexes`。若名字由迁移文件手写，
验证脚本也得手写一遍同样的字符串，两处一旦不一致，"找回触发器"的检查
就会**假失败**或**假通过**。名字只有一个来源才靠得住。

---

## 一、不可变保护（4 张表）

复用跨包共用的 `rednote_reject_mutation()` 函数（WP11 建）。
**本包只挂触发器，不重建函数**，且 `downgrade` **不删它** ——
删了会撞依赖错误并让 WP11/WP15/WP16/WP17/WP18/WP20/WP22/WP23/WP24/WP25
的保护一起失效（WP16 差点踩，WP18/WP20/WP22/WP23/WP24/WP25 明确防了）。

挂哪四张：见 `models.IMMUTABLE_TABLES`（`content_versions` /
`narrative_blueprints` / `title_candidates` / `generation_contexts`）。
`content_items` **刻意不挂** —— 它是身份行，承载会前进的
`current_version_id` 投影与 `lock_version`（ETag 载体），必须可变。

## 二、OP-88：`CONTENT` 的节点一致性（正向 + 反向，**闭合第五半**）

数据结构 02 §5 的原句是：

> 每个具体版本表的 `id` 同时 FK `version_nodes.id`；注册表、具体表及引用
> 同一事务插入。**延迟约束检查 entity_type、entity_id 与实际子表一致，
> 禁止孤立节点。**

WP18 闭合 `BRIEF`、WP20 闭合 `KNOWLEDGE`、WP22 闭合 `BASELINE`、
WP23 闭合 `PROMPT`。**本包闭合 `CONTENT`** —— 闭合之后，
`versions/models.py` 里已定义的 5 个已落地取值**全部有生产者**。

### 正向判据（挂 `content_versions`，三条）

| # | 判据 | 为什么必须存在 |
|---|---|---|
| 1 | `content_versions.id` 必须有一个 `version_nodes` 行 | FK 在 `models.py` 里已声明，但 FK 是**独立**保证；本触发器让"节点缺失"在**同一处**给出与另三个包同款的错误消息（证据脚本按 `sqlstate` 断言，不按消息文本 —— 消息只是给人读的） |

> ⚠️ **判据 1 实测被 FK 遮蔽**（2026-10-06 私有库 `rednote_wp26` 实测）：
> 反例「版本行没有对应节点」报的是 **`23503`（FK）**，不是本触发器的 `23514`。
> `content_versions.id → version_nodes.id` 的延迟 FK 与这个 `CONSTRAINT TRIGGER`
> 都是 `DEFERRABLE INITIALLY DEFERRED`，在同一个结算点被检查，而 **FK 先出手**。
> **这不构成缺陷**（判据 1 的设计价值本就是"同款错误消息"而非独有防线），
> 但**必须写下来**：证据脚本对这条只能断言 `23503`，
> 谎称是触发器拦下的就是"声称自己有防线"（ISS-95 形态）。
> 判据 2/3 **不受遮蔽** —— 它们管的是 FK 管不住的方向（类型对不对、指向谁），
> 实测都报 `23514`。
| 2 | 那个节点的 `entity_type` 必须是 `'CONTENT'` | FK 管不住方向：它只保证被引用的行存在，不保证那行的 `entity_type` 对。错值时一个 `BRIEF` 节点被当成内容版本 —— 注册表与子表说的不是同一件事 |
| 3 | 那个节点的 `entity_id` 必须等于本行的 `content_item_id` | `CONTENT` 的"逻辑实体"是**内容身份**（`models.py` docstring 二）。不一致意味着"给内容 A 注册了一个声称属于内容 B 的版本" —— 而 `version_dependencies` 的边与 WP34 的影响图都按 `entity_id` 聚合，于是影响面**画到了别的内容上** |

### ★ 反向判据**收窄**（与另四个包同一处理）

完整的不变量是"一个 `CONTENT` 节点必须配对一条 `content_versions` 行"。
本实现把它收窄为：

> **只对"真实存在的内容身份"强制** —— 即 `content_items` 表里有
> `id = entity_id` 的行时，才要求配对。

收窄与 WP18/WP20/WP22/WP23 **完全一致**，且**理由也一致**：
`version_nodes` 是**跨包共用**的注册表，`ENTITY_TYPE_CONTENT` 是它
允收的 8 类实体类型之一（`ck_version_nodes_entity_type_valid`），
**不是本包专有**。WP11 的通用用例（`tests/test_versions_wp11.py`）
拿它当代表性样本造"无领域行的节点" —— 与 WP18 的 `BRIEF`、
WP20 的 `KNOWLEDGE` 完全同形。

#### ⚠️ 修正记录：本节原写"**不收窄**"，且论证有误（2026-10-06）

原论证主张"`content_items` 就在本包建出来，不存在'领域表还不存在'这回事"，
并据"`ENTITY_TYPE_CONTENT` 在此之前**业务代码零消费**"推出
"因此不收窄**不会误拦任何既有路径**"。

**错在推论跨度，不在事实**。已核实的现场事实是：
`docs/m3/WP26-…说明.md:104` **如实记录**了
"全仓仅 7 处引用，除定义外全部是 WP11 自己的测试 `test_versions_wp11.py`"
—— 作者**看见了** WP11 的消费。问题出在把它读成了"只是测试、不算业务代码"，
于是"业务代码零消费"被偷换成了"无既有消费方"。

而 WP18/WP20 收窄的理由**从来不是**"怕拦业务代码"，而是
**"WP11 夹具会造无领域行节点"**（其 docstring 原话）。所以：
**生产代码零消费 ≠ 无既有消费方** —— 测试夹具正是跨包契约最容易踩的一类消费方。

不收窄的实测后果是**直接破坏 WP11 既有 2 例**：

    test_cycle_detection_terminates_on_cyclic_graph   （造 19 个 CONTENT 节点）
    test_promote_draft_keeps_entity_type_and_id        （断言类型不被重置）

因果已用 `DISABLE/ENABLE TRIGGER` 钉死（DB 层实测，非推测）。

**为什么"收窄会让判据变成恒真式"这条顾虑不成立**：
收窄挡掉的是"`entity_id` 指向不存在的内容"这一类，而
**本包永远不产生这种节点** —— `service.py:840-847` 的写入顺序是
先落 `content_items` 身份行、再 `register_version`（造节点），
所以生产路径上 `content_items` 行**必然已存在**，守卫不会误放。
因此本判据真正要拦的形态 ——「**内容真实存在、但节点没有配对版本行**」
（`register_version` 成功而 `insert_version` 失败所致的静默分叉）——
**被完整保留**。测试里有一条对照判据专门锁这个行为
（`test_version_nodes_reverse_trigger_rejects_orphan_for_existing_content`）。

**这条收窄的代价已登记为待定项**（与 WP18/WP20 同款）：
`entity_id` 指向不存在的 `content_items.id` 的"悬空节点"不会被拦下。
危害等级低 —— 悬空节点不被任何东西引用（`content_versions` 里没有它），
`version_dependencies` 的溯源也走不到它，它只是一条无人引用的噪声记录。

### 触发器仍被 `WHEN` 收窄到本包的实体类型

`version_nodes` 是共用表，本触发器只被
`WHEN (NEW.entity_type = 'CONTENT')` 收窄到 `CONTENT` 这一类，
与另四个 `*_paired` 触发器在同一张表上并存、互不影响。

## 三、第 3 层：三条**跨表链接**不变量（挂两张表：`content_versions` + `generation_contexts`）

说明文档 §1.7 只规定了不可变与 OP-88。本节的判据是**实现期发现**的 ——
它们是同一类形态：**两条 FK 各自都绿，但合起来说的是错的**。

### 3.1 蓝图必须属于**同一条内容**（`blueprint_id`）

"仅重做标题"时复用上一版的蓝图（`models.py` docstring）——
"复用"的正确含义是**复用自己这条内容的蓝图**。

    content_versions(内容 A).blueprint_id → narrative_blueprints(内容 B)
    FK 表态：蓝图的 `id` 存在 ✅ / `content_item_id` 存在 ✅  → 全绿

后果：**内容 A 的正文是照内容 B 的蓝图写出来的**，而蓝图是内容的
**输入**（`narrative_blueprints` 在 `models.py` 里被列为不可变的原因
就是"改一条蓝图，'这篇内容为什么这么写'的答案就变了"）。
一旦挂错，那个答案**永久指向别的内容**，且**静默**。

### 3.2 采纳的候选必须属于**同一条内容**（`adopted_title_candidate_id`）

`models.py` docstring 六末段已说明这条为什么 FK 管不住：FK 只保证
"这个候选存在"，不保证"它属于我这条内容"。跨内容采纳是**语义错误** ——
而 AQ-01 要**按内容**算事实正确率，采纳了别家标题会让这个数算错。

### 3.1 / 3.2 函数里的 `IF NOT FOUND` 分支是**防御性**的（实测不可达）

两个判据都先 `SELECT … INTO` 再 `IF NOT FOUND THEN RAISE`。实测
（2026-10-06）：这两个分支**永远不会执行** ——

    content_versions.blueprint_id              → narrative_blueprints.id（IMMEDIATE FK）
    content_versions.adopted_title_candidate_id→ title_candidates.id   （IMMEDIATE FK）

两条 FK **未声明 deferrable**，在 INSERT 那一刻就要被引用行存在；
而 `narrative_blueprints` / `title_candidates` 都**不可变**（删不掉）。
因此"引用的行不存在"在到达触发器之前就被 FK 以 `23503` 拒掉，
实测两条反例都报 `23503`。

**为什么仍然保留这两个分支**：它们的成本是两行 SQL，收益是
"若将来有人把这两条 FK 改成 DEFERRED，触发器的语义不变"
（届时 `NOT FOUND` 会成为唯一防线）。若删掉，那条改动会**静默**
打开一个缺口。**这是有意的冗余，不是遗漏。**

### 3.3 生成上下文引用的版本必须属于**同一条内容**（吊 `generation_contexts`）

    generation_contexts(content_id = A).content_version_id → 属于内容 B 的版本
    FK 表态：两列各自存在 ✅  → 全绿

后果：**OP-160 的影响面分析被算到别的内容上**。OP-160 要回答
"改了提示词之后哪些历史生成受影响"，而答案按 `content_id` 归组 ——
两列不一致时，"受影响的内容"这个集合**错得无声无息**
（`(content_id, task_kind)` 的索引照常命中，只是命中的是两个不同内容的拼接）。

**为什么不合并成一个函数**：3.1 / 3.2 挂 `content_versions`、3.3 挂
`generation_contexts` —— 不是同一张表，`NEW` 的列也不同。
（WP20/WP23 把同表的判据合在一个函数里；本包按**表**分，与那张表对齐。）

**为什么全部用 `IS DISTINCT FROM` 而不是 `<>`**：
本判据要比较的列**全都可以为 NULL**（`blueprint_id` / `adopted_title_candidate_id` /
`content_version_id` 都是可空列，`narrative_blueprints.content_item_id` 虽非空，
但写成 `<>` 时若任一操作数为 NULL 会返回 NULL，`IF` 视为 false ⇒ **放行**。
`IS DISTINCT FROM` 是 NULL 安全的比较，把 NULL 当一个正常值参与相等判断。
这条与 `baseline/ddl.py` 第三节"实现里最容易写错的一处"是**同一条纪律**。

## 四、索引：本包**没有**部分唯一索引，但索引名仍须有唯一来源

`models.py` 里本模块声明了 17 条索引（14 条普通 + 3 条唯一）。与 WP24 不同，
本包**没有部分索引**（`WHERE` 子句），因此不存在"部分索引的 `WHERE`
写错导致索引静默退化"那一类风险 —— 但这**不代表**索引可以两处各写一遍。

`index_names()` **从 ORM 元数据派生**（铁律 14：凡"覆盖范围"由人写，
就必须改由**磁盘**决定）。证据脚本按它点名核对 `pg_indexes`，
迁移的 `create_index` 也从它取名 —— 三处不可能漂移。

**为什么不像 WP24 那样写一份 `PARTIAL_UNIQUE_INDEXES` 规格**：
本包的索引规格（列、顺序、唯一性）**唯一来源就是 `__table_args__`**，
没有第二处消费者需要提前拿到它（`planning/ddl.py` 需要那份规格是因为
部分索引要用 `op.execute(ddl)` 建、且证据脚本要比对 `WHERE` 文本）。
凭空造一份无消费方的规格表，就是"声称有防线"的同族。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.content.models import IMMUTABLE_TABLES
from rednote.modules.versions.models import ENTITY_TYPE_CONTENT
from rednote.platform.immutability import immutable_trigger_ddl

# ---------------------------------------------------------------------------
# SQLSTATE 与名字
# ---------------------------------------------------------------------------

# PostgreSQL check_violation。与 WP11 的 `DEPENDENCY_SCOPE_SQLSTATE`、
# WP18/WP20/WP22/WP23 的 `CONSISTENCY_SQLSTATE` 同一个码：
# 语义上它都是"某条数据规则被违反了"，与本模块的 CHECK 同族，
# 都该映射到 422 VALIDATION_FAILED。
CONSISTENCY_SQLSTATE: Final[str] = "23514"

# --- OP-88：节点一致性（正向 + 反向） ---------------------------------------
CONTENT_VERSION_FUNCTION: Final[str] = "rednote_check_content_version_consistency"
CONTENT_VERSION_TRIGGER: Final[str] = "trg_content_versions_node_consistency"
CONTENT_NODE_FUNCTION: Final[str] = "rednote_check_content_node_paired"
CONTENT_NODE_TRIGGER: Final[str] = "trg_version_nodes_content_paired"

# --- 第 3 层：跨表链接 ------------------------------------------------------
CONTEXT_LINK_FUNCTION: Final[str] = "rednote_check_generation_context_consistency"
CONTEXT_LINK_TRIGGER: Final[str] = "trg_generation_contexts_content_consistency"


def _raise_block(message_expr: str) -> str:
    """生成一段 RAISE 语句（**字符串拼接，不用 `%`**，见 WP20 踩过的坑）。

    psycopg 的 paramstyle 会把 `%` 当参数占位符，未提供参数时直接报
    "unsupported format character"。字符串拼接没有这个坑。
    """
    return (
        "        RAISE EXCEPTION USING\n"
        f"            MESSAGE = {message_expr},\n"
        f"            ERRCODE = '{CONSISTENCY_SQLSTATE}';\n"
    )


def content_version_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    挂 `content_versions` AFTER INSERT，延迟到事务提交时执行。
    含 OP-88 的判据 1~3（模块 docstring 二）与两条跨表链接判据
    （3.1 蓝图 / 3.2 采纳候选）。

    **五条判据合在一个函数里**（与 `baseline/ddl.py` 只放 OP-88 三条不同）：
    它们**同属一张表、同一次插入**（`content_versions` 的每一行都要同时
    满足这五条），拆成多个触发器只会让"这一行插入失败"出现多条同样的
    `sqlstate=23514`，而**无法区分**是哪一条 —— 消息文本是唯一线索，
    而消息文本是给人读的，不该被测试依赖。

    合在一起的代价是"失败原因是哪一个"要靠消息读出来。
    **这不构成问题**，因为这五条都是"这一行本身写错了"，
    修法是同一类（改这一行的外键列），不需要区分到条。
    与 `baseline/ddl.py` 把跨品牌与资产范围**拆开**的判断（那时"修法不同"）
    是同一条取舍标准的两侧。

    `RETURN NULL` 而不是 `RETURN NEW`：**AFTER 触发器的返回值被忽略**，
    写 `NULL` 是明确的"我不改写行"。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {CONTENT_VERSION_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    node_type text;\n"
        "    node_entity uuid;\n"
        "    bp_item uuid;\n"
        "    tc_item uuid;\n"
        "BEGIN\n"
        # --- OP-88 判据 1：节点必须存在 -------------------------------------
        "    SELECT entity_type, entity_id INTO node_type, node_entity\n"
        "      FROM version_nodes WHERE id = NEW.id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'content_versions.id 没有对应的 version_nodes 行：'\n"
            "                      || NEW.id::text"
        )
        + "    END IF;\n"
        # --- OP-88 判据 2：entity_type 必须是 CONTENT -----------------------
        f"    IF node_type <> '{ENTITY_TYPE_CONTENT}' THEN\n"
        + _raise_block(
            "'版本节点 ' || NEW.id::text || ' 的 entity_type 是 '\n"
            "                      || node_type || '，不是 "
            f"{ENTITY_TYPE_CONTENT}'"
        )
        + "    END IF;\n"
        # --- OP-88 判据 3：entity_id 必须是本行的内容身份 -------------------
        "    IF node_entity IS DISTINCT FROM NEW.content_item_id THEN\n"
        + _raise_block(
            "'版本节点 ' || NEW.id::text || ' 的 entity_id（'\n"
            "                      || COALESCE(node_entity::text, '(空)')\n"
            "                      || '）与 content_versions.content_item_id（'\n"
            "                      || NEW.content_item_id::text || '）不一致'"
        )
        + "    END IF;\n"
        # --- 3.1 蓝图必须属于同一条内容 -------------------------------------
        # 可空列，因此**必须**用 IS DISTINCT FROM（模块 docstring 三末段）。
        "    IF NEW.blueprint_id IS NOT NULL THEN\n"
        "        SELECT bp.content_item_id INTO bp_item\n"
        "          FROM narrative_blueprints bp WHERE bp.id = NEW.blueprint_id;\n"
        "        IF NOT FOUND THEN\n"
        + _raise_block(
            "'内容版本 ' || NEW.id::text || ' 引用的蓝图 '\n"
            "                      || NEW.blueprint_id::text || ' 不存在'"
        )
        + "        END IF;\n"
        "        IF bp_item IS DISTINCT FROM NEW.content_item_id THEN\n"
        + _raise_block(
            "'内容版本 ' || NEW.id::text || ' 引用的蓝图属于内容 '\n"
            "                      || COALESCE(bp_item::text, '(未知)')\n"
            "                      || '，而本行属于内容 '\n"
            "                      || NEW.content_item_id::text\n"
            "                      || '（蓝图是正文的输入，不得跨内容复用）'"
        )
        + "        END IF;\n"
        "    END IF;\n"
        # --- 3.2 采纳的候选必须属于同一条内容 -------------------------------
        # 判据穿透两层：候选 → 它所属的版本 → 那条版本的内容身份。
        # 不穿透就退化成"候选存在"（= FK 已保证的事，恒真）。
        "    IF NEW.adopted_title_candidate_id IS NOT NULL THEN\n"
        "        SELECT cv.content_item_id INTO tc_item\n"
        "          FROM title_candidates tc\n"
        "          JOIN content_versions cv ON cv.id = tc.content_version_id\n"
        "         WHERE tc.id = NEW.adopted_title_candidate_id;\n"
        "        IF NOT FOUND THEN\n"
        + _raise_block(
            "'内容版本 ' || NEW.id::text || ' 采纳的标题候选 '\n"
            "                      || NEW.adopted_title_candidate_id::text\n"
            "                      || ' 不存在'"
        )
        + "        END IF;\n"
        "        IF tc_item IS DISTINCT FROM NEW.content_item_id THEN\n"
        + _raise_block(
            "'内容版本 ' || NEW.id::text || ' 采纳的标题候选属于内容 '\n"
            "                      || COALESCE(tc_item::text, '(未知)')\n"
            "                      || '，而本行属于内容 '\n"
            "                      || NEW.content_item_id::text\n"
            "                      || '（不得采纳其它内容的标题）'"
        )
        + "        END IF;\n"
        "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {CONTENT_VERSION_TRIGGER}\n"
        "AFTER INSERT ON content_versions\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW EXECUTE FUNCTION {CONTENT_VERSION_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {CONTENT_VERSION_TRIGGER}"
        " ON content_versions"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {CONTENT_VERSION_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def content_node_paired_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    OP-88 的反向判据："一个 `CONTENT` 节点必须配对一条 `content_versions`
    行"。**本包收窄**（与 WP18/WP20/WP22/WP23 同一处理，
    理由见模块 docstring 二的"反向判据收窄"一节）。

    **挂 `version_nodes` 是刻意的**：这个不变量是"注册表侧不许出现
    没有领域行的节点"，它的检查点只能是被插入的那个节点。
    挂在 `content_versions` 上做不到 —— 那时节点可能还没插入。

    **与 WP18 的 `trg_version_nodes_brief_paired`、
    WP20 的 `trg_version_nodes_knowledge_paired`、
    WP22 的 `trg_version_nodes_baseline_paired`、
    WP23 的 `trg_version_nodes_prompt_paired` 在同一张表上并存**：
    五个触发器各自被 `WHEN` 收窄到自己的 `entity_type`，互不影响。
    这是"共用表上按实体类型分区加约束"的标准做法。

    ## 两道判断的顺序不能换

    1. **先判"归不归我管"**（`content_items` 里有 `id = entity_id` 的行）——
       没有就 `RETURN NULL` 放行。这一道是**跨包契约**：`version_nodes`
       是共用表，别的包（含 WP11 通用用例）会注册 `CONTENT` 节点而
       没有对应的内容身份行。
    2. **再判"配对是否存在"**（`content_versions` 里有 `id = NEW.id` 的行）——
       没有就 `RAISE`。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {CONTENT_NODE_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "BEGIN\n"
        # ★ 收窄：只对"真实存在的内容身份"强制。这一道是跨包契约，
        # 不是可选的优化 —— `version_nodes` 是共用表，别的包
        # （WP11 通用用例 / 将来的包）会注册 CONTENT 节点而没有
        # 对应的 `content_items` 行。去掉它 = 直接破坏 WP11 既有用例。
        # 顺序也不能换：必须先放行"不归我管"的，再判配对。
        "    IF NOT EXISTS (\n"
        "        SELECT 1 FROM content_items WHERE id = NEW.entity_id\n"
        "    ) THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "    IF NOT EXISTS (\n"
        "        SELECT 1 FROM content_versions WHERE id = NEW.id\n"
        "    ) THEN\n"
        + _raise_block(
            "'版本节点 ' || NEW.id::text\n"
            "                      || ' 声明属于内容 ' || NEW.entity_id::text\n"
            "                      || '，但没有任何 content_versions 行（孤立节点）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {CONTENT_NODE_TRIGGER}\n"
        "AFTER INSERT ON version_nodes\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW WHEN (NEW.entity_type = '{ENTITY_TYPE_CONTENT}')\n"
        f"EXECUTE FUNCTION {CONTENT_NODE_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {CONTENT_NODE_TRIGGER} ON version_nodes"
    drop_function = f"DROP FUNCTION IF EXISTS {CONTENT_NODE_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def generation_context_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    **第 3 层防线的 3.3**：`generation_contexts` 的两列必须指向
    **同一条内容**（模块 docstring 三）。

    `content_version_id IS NULL` 时直接放行：`CONTENT_STRUCTURE` 那一次调用
    发生时内容版本**还没有**（`models.py` docstring 的
    "`content_version_id` 为什么可空"）。这是**合法的常态**，不是漏判 ——
    若对这个 NULL 也要求配对，结构调用的上下文行就永远留不下来，
    于是"这批内容的蓝图是哪版提示词生成的"永远查不到，
    而那正是 OP-160 要回答的一半问题。

    `CONTENT_COPY` 那一次调用则**必然**有 `content_version_id`
    （它产出的就是那一版），因此本判据对它完整生效。
    "必然"由服务层保证，**没有**落成 CHECK（`task_kind` 与
    `content_version_id` 的非空关系是可以落 CHECK 的）—— 见下面
    "为什么没有反向的 CHECK" 一段。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {CONTEXT_LINK_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    cv_item uuid;\n"
        "BEGIN\n"
        "    IF NEW.content_version_id IS NULL THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "\n"
        "    SELECT cv.content_item_id INTO cv_item\n"
        "      FROM content_versions cv WHERE cv.id = NEW.content_version_id;\n"
        "    IF NOT FOUND THEN\n"
        # FK 已保证存在；走到这里只有一种可能：有人在同一事务里删了那行 ——
        # 而 content_versions 不可变（删不掉）。防御性分支。
        + _raise_block(
            "'生成上下文 ' || NEW.id::text || ' 引用的内容版本 '\n"
            "                      || NEW.content_version_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        "    IF cv_item IS DISTINCT FROM NEW.content_id THEN\n"
        + _raise_block(
            "'生成上下文 ' || NEW.id::text || ' 声明属于内容 '\n"
            "                      || NEW.content_id::text\n"
            "                      || '，但它引用的版本属于内容 '\n"
            "                      || COALESCE(cv_item::text, '(未知)')\n"
            "                      || '（OP-160 的影响面按 content_id 归组，错值会算到别的内容上）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {CONTEXT_LINK_TRIGGER}\n"
        "AFTER INSERT ON generation_contexts\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW EXECUTE FUNCTION {CONTEXT_LINK_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {CONTEXT_LINK_TRIGGER}"
        " ON generation_contexts"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {CONTEXT_LINK_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块的**跨表一致性**触发器（OP-88 第五半 + 三条链接不变量）。

    **为什么公开（去掉下划线）**：迁移的 `downgrade` 需要**只删本模块自己的
    函数**（`rednote_check_content_*`），而 `all_trigger_ddl()` 里同时混着
    不可变保护那 4 项 —— 它们的 `drop_function` 指向**跨包共用**的
    `rednote_reject_mutation()`，删了会撞依赖错误并让
    WP11/WP15/WP16/WP17/WP18/WP20/WP22/WP23/WP24/WP25 的保护一起失效
    （ISS-79 的教训：`versions/ddl.py` 的 `all_trigger_ddl()` 第 5 项
    混着共用函数，`downgrade` 直接遍历就会误删）。

    因此 downgrade 必须能单独拿到"一致性那 3 项"。
    判据：**这个对象是不是我建的** —— 是就删，不是就不删。

    ⚠️ **这个列表的长度是判据的一部分**：它与 `consistency_trigger_names()` /
    `consistency_function_names()` 必须**等长**（各 3 项）。收口时由
    `check_evidence_scripts.py` 与测试双向核对 —— 数错一个的后果是
    `downgrade` 漏删一个函数（残留对象会挡住后续迁移重建它）。
    """
    items: list[tuple[str, str, str, str, str]] = []
    for table, builder in (
        ("content_versions", content_version_consistency_ddl),
        ("version_nodes", content_node_paired_ddl),
        ("generation_contexts", generation_context_consistency_ddl),
    ):
        function_ddl, trigger_ddl, drop_trigger, drop_function = builder()
        items.append((table, function_ddl, trigger_ddl, drop_trigger, drop_function))
    return items


def all_trigger_ddl() -> list[tuple[str, str, str, str, str]]:
    """本模块需要建立的全部触发器。

    返回 `[(表名, 函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP), …]`。

    **顺序有意义**：函数 DDL 用 `CREATE OR REPLACE`，多张表共用同一个
    不可变函数，重复执行是安全的（幂等）；触发器 DROP 也必须先于函数 DROP，
    否则函数仍被触发器引用，PG 会拒绝删除。

    **注意 `version_nodes` 也在列表里**：`trg_version_nodes_content_paired`
    是本模块为了闭合 OP-88 第五半而挂到**共用注册表**上的触发器。
    它不修改 WP11 的任何代码或表结构 —— 它只是 `version_nodes` 上的
    一个新触发器对象，且被 `WHEN (NEW.entity_type = 'CONTENT')` 限定到
    本模块负责的实体类型。**落迁移时必须把它一并建出**，
    否则 OP-88 第五半只闭合了一半（正向判据有、反向判据没有）。

    **刻意把不可变保护排在前面**：让"不可变保护先建、业务不变量后建"
    在读日志时可见。（`CREATE CONSTRAINT TRIGGER` 在 PG 里不做依赖检查 ——
    plpgsql 函数体在首次调用时才解析，因此顺序其实是安全的。）
    """
    items: list[tuple[str, str, str, str, str]] = []
    for table in IMMUTABLE_TABLES:
        function_ddl, trigger_ddl, drop_trigger, drop_function = (
            immutable_trigger_ddl(table)
        )
        items.append((table, function_ddl, trigger_ddl, drop_trigger, drop_function))

    items.extend(consistency_items())
    return items


def immutable_trigger_names() -> tuple[str, ...]:
    """本模块的不可变触发器名（**不含**共用函数名）。"""
    return tuple(f"trg_{table}_immutable" for table in IMMUTABLE_TABLES)


def consistency_trigger_names() -> tuple[str, ...]:
    """本模块的跨表一致性触发器名。"""
    return (
        CONTENT_VERSION_TRIGGER,
        CONTENT_NODE_TRIGGER,
        CONTEXT_LINK_TRIGGER,
    )


def consistency_function_names() -> tuple[str, ...]:
    """本模块**自己的**函数名（downgrade 只删这些）。

    **不含** `rednote_reject_mutation` —— 它属于 `platform`，被全项目共用。
    """
    return (
        CONTENT_VERSION_FUNCTION,
        CONTENT_NODE_FUNCTION,
        CONTEXT_LINK_FUNCTION,
    )


def immutable_function_names() -> tuple[str, ...]:
    """本模块**建立**的函数名。只有共用那一个 —— 且它是**跨包共用**的，
    `downgrade` 不许删（删了会让其它 8 个包的保护一起失效）。
    """
    return ("rednote_reject_mutation",)


def trigger_names() -> tuple[str, ...]:
    """全部应存在的触发器名（验证脚本与测试用，**点名核对、不数个数**）。

    **为什么不数个数**：`version_nodes` 上同时挂着 WP18/WP20/WP22/WP23 与
    本包的五个 `*_paired` 触发器，数个数会把别包的算进来或把自己的漏掉。
    点名核对才能给出"缺失集合为空集"这种可断言的结论。
    """
    return immutable_trigger_names() + consistency_trigger_names()


def index_names() -> tuple[str, ...]:
    """本模块声明的全部索引名，**从 ORM 元数据派生**（铁律 14）。

    为什么派生而不是手写清单：手写清单会漏（漏了**不报错**，
    只是"索引存在性核对"少查一条，于是那条索引可以静默消失）。
    派生自 `__table__.indexes` 之后，`models.py` 是唯一来源。

    **不含主键索引**：`pk_*` 由主键约束隐含创建，不在
    `__table__.indexes` 里，也不由本模块显式声明。
    证据脚本核对的是**本模块显式声明的**那 17 条。

    ⚠️ **一个近似的老化风险**：本函数返回的是"模型里现在有哪些索引"，
    而证据脚本会拿它与迁移建出的索引对账。若某天有人在 `models.py`
    里删了一条索引但忘了改迁移，**两处都会少一条** ——
    对账看到的是"双方一致"，看不到"少了一条"。
    挡住它的是 `check_migration_model_parity.py`（它比的是**迁移库 ⟷ 模型库**，
    不是本函数 ⟷ 迁移），因此那条路径不经过本函数。
    本函数的职责只是"给证据脚本一份不手写的名字清单"。
    """
    from rednote.modules.content.models import (
        ContentItem,
        ContentVersion,
        GenerationContext,
        NarrativeBlueprint,
        TitleCandidate,
    )

    names: set[str] = set()
    for model in (
        ContentItem,
        ContentVersion,
        NarrativeBlueprint,
        TitleCandidate,
        GenerationContext,
    ):
        for index in model.__table__.indexes:
            names.add(index.name)
    return tuple(sorted(names))


__all__ = [
    "CONSISTENCY_SQLSTATE",
    "CONTENT_NODE_FUNCTION",
    "CONTENT_NODE_TRIGGER",
    "CONTENT_VERSION_FUNCTION",
    "CONTENT_VERSION_TRIGGER",
    "CONTEXT_LINK_FUNCTION",
    "CONTEXT_LINK_TRIGGER",
    "all_trigger_ddl",
    "consistency_function_names",
    "consistency_items",
    "consistency_trigger_names",
    "content_node_paired_ddl",
    "content_version_consistency_ddl",
    "generation_context_consistency_ddl",
    "immutable_function_names",
    "immutable_trigger_names",
    "index_names",
    "trigger_names",
]