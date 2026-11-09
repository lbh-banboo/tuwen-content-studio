"""WP23 的触发器 DDL（不可变保护 + 跨表一致性）。

**为什么 DDL 生成放在模块里而不是迁移文件里**（与 WP11/WP18/WP20 同一理由）：
验证脚本要按名字回查 `pg_trigger`。若名字由迁移文件手写，验证脚本也得
手写一遍同样的字符串，两处一旦不一致，"找回触发器"的检查就会**假失败**
或**假通过**。名字只有一个来源才靠得住。

---

## 一、不可变保护（`prompt_versions` / `prompt_publications`）

复用跨包共用的 `rednote_reject_mutation()` 函数（WP11 建）。
**本包只挂触发器，不重建函数**，且 `downgrade` **不删它** ——
删了会撞依赖错误并让 WP15/WP16/WP17/WP18/WP20 的保护一起失效
（WP16 差点踩、WP18 明确防了）。

## 二、OP-88：`PROMPT` 的节点一致性（正向）

数据结构 02 §5 的原句是：

> 每个具体版本表的 `id` 同时 FK `version_nodes.id`；注册表、具体表及引用
> 同一事务插入。**延迟约束检查 entity_type、entity_id 与实际子表一致，
> 禁止孤立节点。**

WP18 闭合了 `BRIEF` 那一半、WP20 闭合了 `KNOWLEDGE` 那一半。
本包闭合 **`PROMPT`** 那一半 —— 三个包各自只对自己负责的 `entity_type` 生效
（反向触发器用 `WHEN` 收窄，见下）。三个判据：

| # | 判据 | 为什么必须存在 |
|---|---|---|
| 1 | `prompt_versions.id` 必须有一个 `version_nodes` 行 | FK 已保证"节点存在"，但 FK **管不住方向**：它只保证被引用的行存在，不保证那行的 `entity_type` 对 |
| 2 | 那个节点的 `entity_type` 必须是 `'PROMPT'` | 否则一个 `CONTENT` 节点被当成提示词版本 —— 注册表与子表说的不是同一件事 |
| 3 | 那个节点的 `entity_id` 必须等于本行的 `template_id` | 否则"这一版属于哪个模板"有两个互相矛盾的答案，而下游（`version_dependencies` 的溯源）取的是节点侧的那个 |

**为什么必须是延迟（DEFERRABLE INITIALLY DEFERRED）**：
数据结构 02 §5 要求"注册表、具体表及引用**同一事务**插入"。
同事务插入意味着两行的插入顺序由应用决定，而 WP11 的 `register_version`
内部用 SAVEPOINT + 重试取修订号 —— 它**不暴露"节点行何时落库"的控制权**。
（WP18 已实测：延迟触发器在 INSERT 时静默通过、在 COMMIT 时抛 `sqlstate=23514`。）

### 反向判据（"禁止孤立节点"）的**有意收窄**（与 WP18/WP20 同一处理）

完整的不变量是"一个 `PROMPT` 节点必须配对一条 `prompt_versions` 行"。
本实现把它收窄为：

> **只对"真实存在的模板"强制** —— 即 `prompt_templates` 表里有
> `id = entity_id` 的行时，才要求配对。

收窄的理由与 WP18/WP20 完全相同：8 类版本实体里只有 `BRIEF` / `KNOWLEDGE` /
`PROMPT` 在本批落地，其余 5 类的领域表还不存在。若不加这一层收窄，
任何"注册一个 `PROMPT` 节点但暂时没有领域行"的路径都会在提交时失败。

**代价已登记**：`entity_id` 指向一个不存在的 `prompt_templates.id` 的
"悬空节点"不会被拦下。危害等级低（它不被任何东西引用，溯源也走不到它），
而"节点说 A、子表说 B"这类**互相矛盾**的形态被判据 1~3 完整覆盖。

## 三、激活事件的范围必须与本模板一致

新增的跨表不变量（规格源没有明写，但它是投影正确性的前提）：

一条激活事件说"模板 T 的版本 v 生效"，而 `v` 可能属于模板 U。
CHECK 表达不了这个（它跨表），FK 也表达不了（`template_id` 与 `version_id`
各自都有 FK，各自都成立）。

**为什么这条比 OP-88 更危险**：OP-88 的两个节点至少指向同一个实体的两个说法；
而这里会让**投影算出"模板 T 的当前版本是 U 的版本"** ——
于是 `GET /prompts` 返回的 `system_template` 是**另一个任务类型的提示词**。
一个"看起来有内容、实际是别人的内容"的模板，比"没有内容"危险得多：
它会被下游（WP26）拿去生成内容，而生成出来的东西用错了系统指令。

## 四、`prompt_templates.active_version_id` 必须属于本模板

`active_version_id` 是一列**投影**（见 `models.py`）。它可以被裸 SQL
就地改写 —— 而"这个模板的当前版本指向另一个模板的版本"同样是静默的错误。
因此对它加一条 AFTER UPDATE 校验。

**只在值真的变化时校验**（`IS DISTINCT FROM`）：避免"每次更新都重跑一遍查询"
的无谓开销，也避免与幂等重试（写同样的值）互相干扰。

## 五、`superseded_version_id` 必须属于本模板

`prompt_publications.superseded_version_id` 是本包新增的列
（协议原文「记录影响范围」）。它同样可能指向**别的模板的版本** ——
若如此，"这次激活取代了谁"这个答案就是错的，而它是审计链的一部分。

## 六、为什么没有"`PROMPT` 节点必须配对"之外的注册表触发器

`version_nodes` 是**全系统共用**的表。本包在它上面只加**一个**
被 `WHEN (NEW.entity_type = 'PROMPT')` 收窄的约束触发器 ——
不加这个条件会让每次 `BRIEF` / `KNOWLEDGE` / `CONTENT` 版本注册都白跑一次
函数（而本包只对 `PROMPT` 有校验能力）。

`WHEN` 在 `CREATE CONSTRAINT TRIGGER` 上可用（WP18 本机实测 PG 17.10）。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.prompts.models import IMMUTABLE_TABLES
from rednote.platform.immutability import immutable_trigger_ddl

# ---------------------------------------------------------------------------
# SQLSTATE 与名字
# ---------------------------------------------------------------------------

# PostgreSQL check_violation。与 WP11 的 `DEPENDENCY_SCOPE_SQLSTATE`、
# WP18 的 `CONSISTENCY_SQLSTATE`、WP20 的 `CONSISTENCY_SQLSTATE` 同一个码：
# 语义上它都是"某条数据规则被违反了"，与本模块的 CHECK 同族，
# 都该映射到 422 VALIDATION_FAILED。
CONSISTENCY_SQLSTATE: Final[str] = "23514"

# --- OP-88：节点一致性 ------------------------------------------------------
PROMPT_VERSION_FUNCTION: Final[str] = "rednote_check_prompt_version_consistency"
PROMPT_VERSION_TRIGGER: Final[str] = "trg_prompt_versions_node_consistency"
PROMPT_NODE_FUNCTION: Final[str] = "rednote_check_prompt_node_paired"
PROMPT_NODE_TRIGGER: Final[str] = "trg_version_nodes_prompt_paired"

# --- 激活事件的范围 --------------------------------------------------------
PUBLICATION_SCOPE_FUNCTION: Final[str] = "rednote_check_prompt_publication_scope"
PUBLICATION_SCOPE_TRIGGER: Final[str] = "trg_prompt_publications_template_scope"

# --- 生效版本的范围 --------------------------------------------------------
ACTIVE_VERSION_SCOPE_FUNCTION: Final[str] = "rednote_check_prompt_active_scope"
ACTIVE_VERSION_SCOPE_TRIGGER: Final[str] = "trg_prompt_templates_active_scope"


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


def prompt_version_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    挂 `prompt_versions` AFTER INSERT，延迟到事务提交时执行。
    OP-88 的判据 1~3（见模块 docstring 第二节）。

    `RETURN NULL` 而不是 `RETURN NEW`：**AFTER 触发器的返回值被忽略**，
    写 `NULL` 是明确的"我不改写行"。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {PROMPT_VERSION_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    node_type text;\n"
        "    node_entity uuid;\n"
        "BEGIN\n"
        "    SELECT entity_type, entity_id INTO node_type, node_entity\n"
        "      FROM version_nodes WHERE id = NEW.id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'prompt_versions.id 没有对应的 version_nodes 行：'\n"
            "                      || NEW.id::text"
        )
        + "    END IF;\n"
        "    IF node_type <> 'PROMPT' THEN\n"
        + _raise_block(
            "'版本节点 ' || NEW.id::text || ' 的 entity_type 是 '\n"
            "                      || node_type || '，不是 PROMPT'"
        )
        + "    END IF;\n"
        "    IF node_entity <> NEW.template_id THEN\n"
        + _raise_block(
            "'版本节点 ' || NEW.id::text || ' 的 entity_id（'\n"
            "                      || node_entity::text\n"
            "                      || '）与 prompt_versions.template_id（'\n"
            "                      || NEW.template_id::text || '）不一致'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {PROMPT_VERSION_TRIGGER}\n"
        "AFTER INSERT ON prompt_versions\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW EXECUTE FUNCTION "
        f"{PROMPT_VERSION_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {PROMPT_VERSION_TRIGGER} ON prompt_versions"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {PROMPT_VERSION_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def prompt_node_paired_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    OP-88 的反向判据："一个 `PROMPT` 节点必须配对一条 `prompt_versions` 行"
    （含 WP18/WP20 同款的收窄，见模块 docstring 第二节）。

    **挂 `version_nodes` 是刻意的**：这个不变量是"注册表侧不许出现
    没有领域行的节点"，它的检查点只能是被插入的那个节点。
    挂在 `prompt_versions` 上做不到 —— 那时节点可能还没插入。

    **与 WP18 的 `trg_version_nodes_brief_paired`、WP20 的
    `trg_version_nodes_knowledge_paired` 在同一张表上并存**：
    三个触发器各自被 `WHEN` 收窄到自己的 `entity_type`，互不影响。
    这是"共用表上按实体类型分区加约束"的标准做法。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {PROMPT_NODE_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "BEGIN\n"
        # 收窄：只对"真实存在的模板"强制（理由见模块 docstring）。
        "    IF NOT EXISTS (\n"
        "        SELECT 1 FROM prompt_templates WHERE id = NEW.entity_id\n"
        "    ) THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "    IF NOT EXISTS (\n"
        "        SELECT 1 FROM prompt_versions WHERE id = NEW.id\n"
        "    ) THEN\n"
        + _raise_block(
            "'版本节点 ' || NEW.id::text\n"
            "                      || ' 声明属于模板 ' || NEW.entity_id::text\n"
            "                      || '，但没有任何 prompt_versions 行（孤立节点）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {PROMPT_NODE_TRIGGER}\n"
        "AFTER INSERT ON version_nodes\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW WHEN (NEW.entity_type = 'PROMPT')\n"
        f"EXECUTE FUNCTION {PROMPT_NODE_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {PROMPT_NODE_TRIGGER} ON version_nodes"
    drop_function = f"DROP FUNCTION IF EXISTS {PROMPT_NODE_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def publication_scope_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    规则（见模块 docstring 第三节）：
      · `prompt_publications.version_id` 所属的模板必须等于本行的 `template_id`；
      · `superseded_version_id`（若有）所属的模板也必须是本模板（第五节）。

    **两条合在一个函数里**：它们查的是同一张 `prompt_versions` 表、
    同一个判据（"这一版属于哪个模板"），拆成两个函数会让同一次插入
    跑两遍同样的查询 —— 而两者的失败处置完全相同（都是数据矛盾）。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {PUBLICATION_SCOPE_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    version_template uuid;\n"
        "    superseded_template uuid;\n"
        "BEGIN\n"
        "    SELECT template_id INTO version_template\n"
        "      FROM prompt_versions WHERE id = NEW.version_id;\n"
        "    IF NOT FOUND THEN\n"
        # FK 已保证存在，走到这里说明有人在同一事务里删了它 ——
        # 而 prompt_versions 不可变（删不掉）。这是防御性分支。
        + _raise_block(
            "'激活事件引用的版本 ' || NEW.version_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        "    IF version_template <> NEW.template_id THEN\n"
        + _raise_block(
            "'激活事件声明的是模板 ' || NEW.template_id::text\n"
            "                      || '，但版本 ' || NEW.version_id::text\n"
            "                      || ' 属于模板 ' || version_template::text\n"
            "                      || '（投影会算出别人的版本是本模板的当前版本）'"
        )
        + "    END IF;\n"
        "    IF NEW.superseded_version_id IS NOT NULL THEN\n"
        "        SELECT template_id INTO superseded_template\n"
        "          FROM prompt_versions WHERE id = NEW.superseded_version_id;\n"
        "        IF NOT FOUND THEN\n"
        + _raise_block(
            "'被取代的版本 ' || NEW.superseded_version_id::text || ' 不存在'"
        )
        + "        END IF;\n"
        "        IF superseded_template <> NEW.template_id THEN\n"
        + _raise_block(
            "'被取代的版本 ' || NEW.superseded_version_id::text\n"
            "                          || ' 属于模板 ' || superseded_template::text\n"
            "                          || '，不是本模板的版本（影响范围记错）'"
        )
        + "        END IF;\n"
        "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {PUBLICATION_SCOPE_TRIGGER}\n"
        "AFTER INSERT ON prompt_publications\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW EXECUTE FUNCTION {PUBLICATION_SCOPE_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {PUBLICATION_SCOPE_TRIGGER}"
        " ON prompt_publications"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {PUBLICATION_SCOPE_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def active_version_scope_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    规则：`prompt_templates.active_version_id` 必须指向**本模板的**版本
    （见模块 docstring 第四节）。

    **AFTER UPDATE**（不是 INSERT）：新建模板时 `active_version_id`
    为 NULL；它第一次被赋值是在激活事务里，即一次 UPDATE。

    `IS DISTINCT FROM` 让"值没变"时直接短路（幂等重试会重复写入同样的值）。

    **不是延迟的**：这条校验只读 `prompt_versions`（本行 UPDATE 时它必然
    已经存在 —— 版本行的存在由激活事务先插入或已存在保证）。
    不延迟让错误在 UPDATE 那一刻就暴露，位置更靠近犯错的地方。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {ACTIVE_VERSION_SCOPE_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    version_template uuid;\n"
        "BEGIN\n"
        "    IF NEW.active_version_id IS NULL THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "    IF NEW.active_version_id IS NOT DISTINCT FROM OLD.active_version_id THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "    SELECT template_id INTO version_template\n"
        "      FROM prompt_versions WHERE id = NEW.active_version_id;\n"
        "    IF NOT FOUND THEN\n"
        # `active_version_id` 的 FK 指向 prompt_versions（立即），
        # 但版本行与节点行是延迟配对的，因此**可能**在节点存在而领域行
        # 还没写入的瞬间被 UPDATE 到。那种情况下这里会命中 ——
        # 报出来比静默通过好：一个指向"不存在领域行"的节点的
        # active_version_id 是坏数据。
        + _raise_block(
            "'生效版本 ' || NEW.active_version_id::text\n"
            "                      || ' 没有任何 prompt_versions 行'"
        )
        + "    END IF;\n"
        "    IF version_template <> NEW.id THEN\n"
        + _raise_block(
            "'模板 ' || NEW.id::text || ' 的生效版本指向 '\n"
            "                      || version_template::text\n"
            "                      || ' 的版本（不是本模板的版本）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {ACTIVE_VERSION_SCOPE_TRIGGER}\n"
        "AFTER UPDATE ON prompt_templates\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW EXECUTE FUNCTION {ACTIVE_VERSION_SCOPE_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {ACTIVE_VERSION_SCOPE_TRIGGER}"
        " ON prompt_templates"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {ACTIVE_VERSION_SCOPE_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块的**跨表一致性**触发器（OP-88 等）。

    **为什么公开（去掉下划线）**：迁移的 `downgrade` 需要**只删本模块自己的
    函数**（`rednote_check_prompt_*`），而 `all_trigger_ddl()` 里同时混着
    不可变保护那 2 项 —— 它们的 `drop_function` 指向**跨包共用**的
    `rednote_reject_mutation()`，删了会撞依赖错误并让
    WP15/WP16/WP17/WP18/WP20 的保护一起失效（WP16 差点踩、WP18 明确防了）。

    因此 downgrade 必须能单独拿到"一致性那 4 项"。
    判据：**这个对象是不是我建的** —— 是就删，不是就不删。
    """
    items: list[tuple[str, str, str, str, str]] = []
    for table, builder in (
        ("prompt_versions", prompt_version_consistency_ddl),
        ("version_nodes", prompt_node_paired_ddl),
        ("prompt_publications", publication_scope_ddl),
        ("prompt_templates", active_version_scope_ddl),
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

    **注意 `version_nodes` 也在列表里**：`trg_version_nodes_prompt_paired`
    是本模块为了闭合 OP-88 而挂到**共用注册表**上的触发器。
    它不修改 WP11 的任何代码或表结构 —— 它只是 `version_nodes` 上的
    一个新触发器对象，且被 `WHEN (NEW.entity_type = 'PROMPT')` 限定到
    本模块负责的实体类型。**主 agent 落迁移时必须把它一并建出**，
    否则 OP-88 只闭合了一半（判据 1~3 有、反向判据没有）。

    **同样注意 `prompt_templates` 也在列表里**（它的 `active_scope` 触发器），
    但它是本模块自己的表，不涉及跨包。

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
        PROMPT_VERSION_TRIGGER,
        PROMPT_NODE_TRIGGER,
        PUBLICATION_SCOPE_TRIGGER,
        ACTIVE_VERSION_SCOPE_TRIGGER,
    )


def trigger_names() -> tuple[str, ...]:
    """全部应存在的触发器名（验证脚本与测试用）。"""
    return immutable_trigger_names() + consistency_trigger_names()


__all__ = [
    "ACTIVE_VERSION_SCOPE_FUNCTION",
    "ACTIVE_VERSION_SCOPE_TRIGGER",
    "CONSISTENCY_SQLSTATE",
    "PROMPT_NODE_FUNCTION",
    "PROMPT_NODE_TRIGGER",
    "PROMPT_VERSION_FUNCTION",
    "PROMPT_VERSION_TRIGGER",
    "PUBLICATION_SCOPE_FUNCTION",
    "PUBLICATION_SCOPE_TRIGGER",
    "active_version_scope_ddl",
    "all_trigger_ddl",
    "consistency_items",
    "consistency_trigger_names",
    "immutable_trigger_names",
    "prompt_node_paired_ddl",
    "prompt_version_consistency_ddl",
    "publication_scope_ddl",
    "trigger_names",
]
