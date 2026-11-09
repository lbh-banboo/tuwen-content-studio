"""本模块表的**数据库级保证**（DDL）—— 含 OP-88 的闭合点。

**为什么单独一个文件**：这些 DDL 有两个消费方 ——
  1. 迁移（主 agent 串行落成）负责把它建出来；
  2. 测试 / 验证脚本负责回查它是否真的存在、真的生效。

若两边各自手写 SQL 与名字，就会出现"迁移建了 `trg_x`、验证脚本找 `trg_y`"
这种**看起来通过、实际没有保护**的情况（WP11 §4.3 的教训）。
名字与 SQL 只有一处来源才靠得住。

`RAISE EXCEPTION ... USING MESSAGE = ...` 一律用字符串拼接而**不用 `%` 占位符**：
psycopg 的 paramstyle 会把 `%` 当参数占位符，未提供参数时直接报
"unsupported format character"。这是 WP11 真踩过的坑。

---

## 一、不可变保护（3 张表）

`brief_versions` / `brief_claims` / `brief_issues` 复用
`platform.immutability` 的**共享**触发器函数 `rednote_reject_mutation()` ——
**不自己写一份**：自己写会在 downgrade 时撞
"cannot drop function ... because other objects depend on it"
（WP11 踩过、WP16 差点再踩）。所有不可变表共用同一个函数，
行为完全一致（一律拒绝 UPDATE/DELETE）。

## 二、OP-88：`entity_type`/`entity_id` 与子表一致（**本包闭合**）

WP11 的登记原文（`docs/待定项与问题登记册.md` OP-88）：

> "延迟约束检查 `entity_type`/`entity_id` 与子表一致"（数据结构 02 §5）**未实现**
> —— 前提是子表存在；WP11 只建注册表与依赖边、没有子表，无从校验。

数据结构 02 §5 的原句是：

> 每个具体版本表的 `id` 同时 FK `version_nodes.id`；注册表、具体表及引用
> 同一事务插入。**延迟约束检查 entity_type、entity_id 与实际子表一致，
> 禁止孤立节点。**

本包是第一个领域版本表，因此在这里闭合。三个判据：

| # | 判据 | 为什么必须存在 |
|---|---|---|
| 1 | `brief_versions.id` 必须有一个 `version_nodes` 行 | FK 已经保证了"节点存在"，但 FK **管不住方向**：它只保证被引用的行存在，不保证那行的 `entity_type` 对 |
| 2 | 那个节点的 `entity_type` 必须是 `'BRIEF'` | 否则一个 `CONTENT` 节点被当成 Brief 版本 —— 注册表与子表说的不是同一件事 |
| 3 | 那个节点的 `entity_id` 必须等于本行的 `brief_id` | 否则"这一版属于哪个逻辑 Brief"有两个互相矛盾的答案，而下游（`version_dependencies` 的溯源）取的是节点侧的那个 |

**为什么必须是延迟（DEFERRABLE INITIALLY DEFERRED）**：
数据结构 02 §5 要求"注册表、具体表及引用**同一事务**插入"。
同事务插入意味着两行的插入顺序由应用决定，而 WP11 的 `register_version`
内部用 SAVEPOINT + 重试取修订号 —— 它**不暴露"节点行何时落库"的控制权**。
延迟到提交时校验，让"谁先谁后"不再是一个必须猜对的问题。
（本机已实测：延迟触发器在 INSERT 时静默通过、在 COMMIT 时抛
`sqlstate=23514`。）

### 反向判据（"禁止孤立节点"）的**有意收窄**

完整的不变量是"一个 `BRIEF` 节点必须配对一条 `brief_versions` 行"。
本实现把它收窄为：

> **只对"真实存在的逻辑 Brief"强制** —— 即 `briefs` 表里有
> `id = entity_id` 的行时，才要求配对。

收窄的理由是**它必须与"部分领域表尚未落地"这个现实共存**：
8 类版本实体里只有 `BRIEF` 与 `KNOWLEDGE`（WP20）在本批落地，
其余 6 类的领域表还不存在。若不加这一层收窄，任何"注册一个 BRIEF 节点
但暂时没有领域行"的路径都会在提交时失败 —— 而 WP11 的测试夹具
正是这么用的（`tests/test_versions_wp11.py` 的 `_register()` 默认
`entity_type=ENTITY_TYPE_BRIEF`，造的是**没有领域行的节点**）。

**这条收窄是有代价的，代价已登记为待定项**：
`entity_id` 指向一个不存在的 `briefs.id` 的"悬空节点"不会被拦下。
但它的危害等级低 —— 悬空节点不被任何东西引用（`brief_versions` 里没有它），
`version_dependencies` 的溯源也走不到它，它只是一条无人引用的噪声记录。
而"节点说 A、子表说 B"这类**互相矛盾**的节点才是会污染溯源的形态，
它被判据 1~3 完整覆盖。

## 三、`brief_claims` 的证据来源必须是**本版本那份文档**的片段

只要求"有 `evidence_segment_id`"是不够的：一个指向**别的项目、别的文件**
的片段 id 同样能满足它，而那是**假溯源** —— 它比"没有来源"更危险，
因为它看起来可追溯，人工审核不会去怀疑它。

因此 `brief_claims.evidence_segment_id` 必须属于
`brief_versions.source_parse_run_id` 那次解析产出的片段集
（`source_segments.parse_run_id`）。这是跨表不变量，CHECK 表达不了。

## 四、`source_state='PARTIAL'` 的版本必须留下疑问（**硬约束的第三层**）

前两层在 `models.py`：
  1. `brief_drafts.source_state` 是受控枚举，`PARTIAL` 是独立取值；
  2. 草稿上的 CHECK：`PARTIAL` 时 `issue_resolutions` 必须非空。

第三层在这里：**固化后的版本**若 `source_state='PARTIAL'`，
必须至少有一条 `brief_issues` 行。它必须放在触发器里，因为
`brief_issues` 是另一张表，且在同一事务里晚于 `brief_versions` 插入。

**为什么固化也要管**：`brief_drafts` 会被下一轮编辑覆盖
（"正式提交后保留新一轮草稿入口"，协议 §5 末段），而
`brief_versions` 是不可变的长期记录。若只在草稿层拦，固化就等于
**把残缺洗白** —— 三个月后有人问"这一版 Brief 是基于完整文档做的吗"，
答案取决于草稿还在不在。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.brief.models import IMMUTABLE_TABLES
from rednote.platform.immutability import immutable_trigger_ddl

# ---------------------------------------------------------------------------
# SQLSTATE 与名字
# ---------------------------------------------------------------------------

# PostgreSQL check_violation。与 WP11 的 `DEPENDENCY_SCOPE_SQLSTATE` 同一个码：
# 语义上它都是"某条数据规则被违反了"，与本模块的 CHECK 同族，
# 都该映射到 422 VALIDATION_FAILED。
CONSISTENCY_SQLSTATE: Final[str] = "23514"

# --- OP-88：节点一致性 ------------------------------------------------------
BRIEF_VERSION_FUNCTION: Final[str] = "rednote_check_brief_version_consistency"
BRIEF_VERSION_TRIGGER: Final[str] = "trg_brief_versions_node_consistency"
BRIEF_NODE_FUNCTION: Final[str] = "rednote_check_brief_node_paired"
BRIEF_NODE_TRIGGER: Final[str] = "trg_version_nodes_brief_paired"

# --- 证据来源 --------------------------------------------------------------
CLAIM_EVIDENCE_FUNCTION: Final[str] = "rednote_check_brief_claim_evidence_scope"
CLAIM_EVIDENCE_TRIGGER: Final[str] = "trg_brief_claims_evidence_scope"

# --- 疑问的项目归属 --------------------------------------------------------
ISSUE_SCOPE_FUNCTION: Final[str] = "rednote_check_brief_issue_scope"
ISSUE_SCOPE_TRIGGER: Final[str] = "trg_brief_issues_project_scope"


def _raise_block(message_expr: str) -> str:
    """生成一段 RAISE 语句（**字符串拼接，不用 `%`**，见模块 docstring）。"""
    return (
        "        RAISE EXCEPTION USING\n"
        f"            MESSAGE = {message_expr},\n"
        f"            ERRCODE = '{CONSISTENCY_SQLSTATE}';\n"
    )


def brief_version_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    挂 `brief_versions` AFTER INSERT，延迟到事务提交时执行。一次做两件事：

      1. **OP-88 判据 1~3**：节点存在、`entity_type='BRIEF'`、
         `entity_id = brief_id`；
      2. **PARTIAL 必须留下疑问**：`source_state='PARTIAL'` 时
         `brief_issues` 里必须至少有一条本版本的行。

    两件事合在一个函数里而不是两个触发器：它们都挂在同一张表上、
    都在提交时校验、都要读同一行 `NEW`。拆成两个只是多一份
    `CREATE OR REPLACE FUNCTION` 与一个 DROP 配对。

    `RETURN NULL` 而不是 `RETURN NEW`：**AFTER 触发器的返回值被忽略**，
    写 `NULL` 是明确的"我不改写行"。（`RETURN NEW` 在 AFTER 触发器里
    也不生效，但会让读者以为这里做了行替换。）
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {BRIEF_VERSION_FUNCTION}() RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    node_type text;\n"
        "    node_entity uuid;\n"
        "BEGIN\n"
        "    SELECT entity_type, entity_id INTO node_type, node_entity\n"
        "      FROM version_nodes WHERE id = NEW.id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'brief_versions.id 没有对应的 version_nodes 行：'\n"
            "                      || NEW.id::text"
        )
        + "    END IF;\n"
        "    IF node_type <> 'BRIEF' THEN\n"
        + _raise_block(
            "'版本节点 ' || NEW.id::text || ' 的 entity_type 是 '\n"
            "                      || node_type || '，不是 BRIEF'"
        )
        + "    END IF;\n"
        "    IF node_entity <> NEW.brief_id THEN\n"
        + _raise_block(
            "'版本节点 ' || NEW.id::text || ' 的 entity_id（'\n"
            "                      || node_entity::text\n"
            "                      || '）与 brief_versions.brief_id（'\n"
            "                      || NEW.brief_id::text || '）不一致'"
        )
        + "    END IF;\n"
        # PARTIAL 必须留下疑问（硬约束的第三层）。
        "    IF NEW.source_state = 'PARTIAL' THEN\n"
        "        IF NOT EXISTS (\n"
        "            SELECT 1 FROM brief_issues WHERE brief_version_id = NEW.id\n"
        "        ) THEN\n"
        + _raise_block(
            "'版本 ' || NEW.id::text\n"
            "                      || ' 的输入是 PARTIAL，但没有任何疑问记录：'\n"
            "                      || '残缺不得在固化时被洗白'"
        )
        + "        END IF;\n"
        "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {BRIEF_VERSION_TRIGGER}\n"
        "AFTER INSERT ON brief_versions\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW EXECUTE FUNCTION "
        f"{BRIEF_VERSION_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {BRIEF_VERSION_TRIGGER} ON brief_versions"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {BRIEF_VERSION_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def brief_node_paired_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    OP-88 的反向判据："一个 `BRIEF` 节点必须配对一条 `brief_versions` 行"。

    **挂 `version_nodes` 是刻意的**：这个不变量是"注册表侧不许出现
    没有领域行的节点"，它的检查点只能是被插入的那个节点。
    挂在 `brief_versions` 上做不到 —— 那时节点可能还没插入。

    `WHEN (NEW.entity_type = 'BRIEF')` 把触发器限定到本模块关心的实体类型：
    `version_nodes` 是**全系统共用**的表，不加这个条件会让每次
    `CONTENT` / `PLAN` / `PROMPT` 版本注册都白跑一次函数
    （而本包只对 `BRIEF` 有校验能力 —— 其余 6 类的领域表还不存在）。

    **`WHEN` 在 `CREATE CONSTRAINT TRIGGER` 上可用**（本机实测 PG 17.10）：
    若不可用，这个触发器会对全部 8 类实体生效，直接违反
    "分支只写自己模块"的边界。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {BRIEF_NODE_FUNCTION}() RETURNS trigger AS $$\n"
        "BEGIN\n"
        # 收窄：只对"真实存在的逻辑 Brief"强制。理由见模块 docstring
        # （部分领域表尚未落地；WP11 的测试夹具会造无领域行的 BRIEF 节点）。
        "    IF NOT EXISTS (SELECT 1 FROM briefs WHERE id = NEW.entity_id) THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "    IF NOT EXISTS (SELECT 1 FROM brief_versions WHERE id = NEW.id) THEN\n"
        + _raise_block(
            "'版本节点 ' || NEW.id::text\n"
            "                      || ' 声明属于逻辑 Brief ' || NEW.entity_id::text\n"
            "                      || '，但没有任何 brief_versions 行（孤立节点）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {BRIEF_NODE_TRIGGER}\n"
        "AFTER INSERT ON version_nodes\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW WHEN (NEW.entity_type = 'BRIEF')\n"
        f"EXECUTE FUNCTION {BRIEF_NODE_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {BRIEF_NODE_TRIGGER} ON version_nodes"
    drop_function = f"DROP FUNCTION IF EXISTS {BRIEF_NODE_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def claim_evidence_scope_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    规则：`brief_claims.evidence_segment_id` 必须属于本版本的
    `source_parse_run_id` 那次解析（见模块 docstring 第三节）。

    `WHEN (NEW.evidence_segment_id IS NOT NULL)`：只对"声称有原文来源"
    的结论做校验。`EXTERNAL_EVIDENCE` 用 `evidence_url` 时没有片段可查 ——
    外部链接的**可验证性**本机做不到（要真的去访问那个 URL），
    已登记为待定项。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {CLAIM_EVIDENCE_FUNCTION}() RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    run_id uuid;\n"
        "BEGIN\n"
        "    SELECT source_parse_run_id INTO run_id\n"
        "      FROM brief_versions WHERE id = NEW.brief_version_id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'结论 ' || NEW.id::text || ' 指向的 Brief 版本不存在'"
        )
        + "    END IF;\n"
        "    IF run_id IS NULL THEN\n"
        + _raise_block(
            "'版本 ' || NEW.brief_version_id::text\n"
            "                      || ' 没有 source_parse_run_id，无法校验证据来源'"
        )
        + "    END IF;\n"
        "    IF NOT EXISTS (\n"
        "        SELECT 1 FROM source_segments\n"
        "         WHERE id = NEW.evidence_segment_id AND parse_run_id = run_id\n"
        "    ) THEN\n"
        + _raise_block(
            "'证据片段 ' || NEW.evidence_segment_id::text\n"
            "                      || ' 不属于版本 ' || NEW.brief_version_id::text\n"
            "                      || ' 的解析运行 ' || run_id::text\n"
            "                      || '（假溯源）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {CLAIM_EVIDENCE_TRIGGER}\n"
        "AFTER INSERT ON brief_claims\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW WHEN (NEW.evidence_segment_id IS NOT NULL)\n"
        f"EXECUTE FUNCTION {CLAIM_EVIDENCE_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {CLAIM_EVIDENCE_TRIGGER} ON brief_claims"
    drop_function = f"DROP FUNCTION IF EXISTS {CLAIM_EVIDENCE_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def issue_scope_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    规则：`brief_issues.project_id` 必须等于"该版本所属 Brief 的 project_id"。

    **为什么必须校验**（而不是把这一列删掉）：
    数据结构 02 §6 明确列了 `brief_issues.project_id`，它的用途是让
    "本项目还有哪些未处置疑问"不必 join 三张表。
    但一个**冗余列**如果不校验一致性，就会漂移成"两个互相矛盾的答案" ——
    而"核心疑问未解决阻断步骤 1"这条判断正是按它来查的，
    漂移的方向恰好是"漏掉某个项目的阻断疑问"。

    **为什么不把 `brief_versions` 加进 `brief_issues` 的 FK 链**：
    那需要 `brief_versions(brief_id)` 唯一或 `(id, brief_id)` 复合外键，
    会让 `brief_versions` 多一个只为这个校验存在的索引 ——
    而一个延迟触发器能达到同样的强度，且不引入新的索引维护成本。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {ISSUE_SCOPE_FUNCTION}() RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    expected uuid;\n"
        "BEGIN\n"
        "    SELECT b.project_id INTO expected\n"
        "      FROM brief_versions v JOIN briefs b ON b.id = v.brief_id\n"
        "     WHERE v.id = NEW.brief_version_id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'疑问 ' || NEW.id::text || ' 指向的 Brief 版本不存在'"
        )
        + "    END IF;\n"
        "    IF expected <> NEW.project_id THEN\n"
        + _raise_block(
            "'疑问 ' || NEW.id::text || ' 的 project_id（'\n"
            "                      || NEW.project_id::text\n"
            "                      || '）与版本所属项目（' || expected::text\n"
            "                      || '）不一致'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {ISSUE_SCOPE_TRIGGER}\n"
        "AFTER INSERT ON brief_issues\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW EXECUTE FUNCTION {ISSUE_SCOPE_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {ISSUE_SCOPE_TRIGGER} ON brief_issues"
    drop_function = f"DROP FUNCTION IF EXISTS {ISSUE_SCOPE_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块的**跨表一致性**触发器（OP-88 等）。

    **为什么公开（去掉下划线）**：迁移的 `downgrade` 需要**只删本模块自己的
    函数**（`rednote_check_brief_*`），而 `all_trigger_ddl()` 里同时混着
    不可变保护那 3 项 —— 它们的 `drop_function` 指向**跨包共用**的
    `rednote_reject_mutation()`，删了会撞依赖错误并让 WP15/WP16/WP17
    的保护一起失效（WP16 差点踩）。

    因此 downgrade 必须能单独拿到"一致性那 4 项"。用私有函数会让迁移
    只能写 `_consistency_items()`（跨模块调私有名），那是把封装破坏得更彻底，
    不如显式公开。

    判据：**这个对象是不是我建的** —— 是就删，不是就不删。
    """
    items: list[tuple[str, str, str, str, str]] = []
    for table, builder in (
        ("brief_versions", brief_version_consistency_ddl),
        ("version_nodes", brief_node_paired_ddl),
        ("brief_claims", claim_evidence_scope_ddl),
        ("brief_issues", issue_scope_ddl),
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

    **注意 `version_nodes` 也在列表里**：`trg_version_nodes_brief_paired`
    是本模块为了闭合 OP-88 而挂到**共用注册表**上的触发器。
    它不修改 WP11 的任何代码或表结构 —— 它只是 `version_nodes` 上的
    一个新触发器对象，且被 `WHEN (NEW.entity_type = 'BRIEF')` 限定到
    本模块负责的实体类型。**主 agent 落迁移时必须把它一并建出**，
    否则 OP-88 只闭合了一半（判据 1~3 有、反向判据没有）。

    **刻意排在最后**：一致性触发器依赖 `brief_versions` / `brief_issues`
    的表结构存在，但 `CREATE CONSTRAINT TRIGGER` 在 PG 里不做依赖检查
    （plpgsql 函数体在首次调用时才解析）—— 顺序其实是安全的。
    排在最后只是为了让"不可变保护先建、业务不变量后建"在读日志时可见。
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
        BRIEF_VERSION_TRIGGER,
        BRIEF_NODE_TRIGGER,
        CLAIM_EVIDENCE_TRIGGER,
        ISSUE_SCOPE_TRIGGER,
    )


def trigger_names() -> tuple[str, ...]:
    """全部应存在的触发器名（验证脚本与测试用）。"""
    return immutable_trigger_names() + consistency_trigger_names()


__all__ = [
    "BRIEF_NODE_FUNCTION",
    "BRIEF_NODE_TRIGGER",
    "BRIEF_VERSION_FUNCTION",
    "BRIEF_VERSION_TRIGGER",
    "CLAIM_EVIDENCE_FUNCTION",
    "CLAIM_EVIDENCE_TRIGGER",
    "CONSISTENCY_SQLSTATE",
    "ISSUE_SCOPE_FUNCTION",
    "ISSUE_SCOPE_TRIGGER",
    "all_trigger_ddl",
    "brief_node_paired_ddl",
    "brief_version_consistency_ddl",
    "claim_evidence_scope_ddl",
    "consistency_items",
    "consistency_trigger_names",
    "immutable_trigger_names",
    "issue_scope_ddl",
    "trigger_names",
]
