"""本模块表的**数据库级保证**（DDL）。

**为什么单独一个文件**：这些 DDL 有两个消费方 ——
  1. 迁移（主 agent 串行落成）负责把它建出来；
  2. 测试 / 验证脚本负责回查它是否真的存在、真的生效。

若两边各自手写 SQL 与名字，就会出现"迁移建了 `trg_x`、验证脚本找 `trg_y`"
这种**看起来通过、实际没有保护**的情况（WP11 §4.3 的教训，WP18 沿用）。
名字与 SQL 只有一处来源才靠得住。

`RAISE EXCEPTION ... USING MESSAGE = ...` 一律用字符串拼接而**不用 `%` 占位符**：
psycopg 的 paramstyle 会把 `%` 当参数占位符，未提供参数时直接报
"unsupported format character"。这是 WP11 真踩过的坑。

---

## 一、不可变保护（**2 张表**，与交接文档写的"3 个"不同）

`knowledge_versions` / `knowledge_publications` 复用
`platform.immutability` 的**共享**触发器函数 `rednote_reject_mutation()` ——
**不自己写一份**：自己写会在 downgrade 时撞
"cannot drop function ... because other objects depend on it"
（WP11 踩过、WP16 差点再踩）。所有不可变表共用同一个函数，
行为完全一致（一律拒绝 UPDATE/DELETE）。

**`knowledge_chunks` 刻意不在其中**（这是与交接文档 §4 #27 的一处偏离，
已登记为待定项）。判据是**不可变的定义**，不是"它看起来像派生表"：

> 不可变 = 这个对象**一旦成立就是历史事实**，改它等于篡改溯源记录。

| 表 | 是历史事实吗 | 结论 |
|---|---|---|
| `knowledge_versions` | 是。它是"这一版知识是什么"的答案，改了溯源链就断了 | **不可变** |
| `knowledge_publications` | 是。它是"这条知识什么时候生效过"的答案 | **不可变** |
| `knowledge_chunks` | **不是**。它完全由 (版本内容, 切块器版本) 决定，是**可重建的物化结果** | 不加 |

第三行是本包的判断。两个理由：

1. **重建是幂等的**（同一份内容 + 同一个切块器 → 同一批 chunk）。
   对一段可重建的派生数据加"改了就是篡改历史"的语义，
   会让 WP21 无法实现它的核心验收项之一 ——
   **"模型换版不混向量"**：更换 embedding 模型时若需要重切，
   删掉旧 chunk 是正常运维，而不是篡改。
2. **引用不指向 chunk**。规格源 02 §8 明写 `context_items` 的
   「**保留实际片段而非仅知识 ID**」—— 引用方存的是**片段文本本身**，
   不存 chunk id。因此 chunk 被重建**不影响任何历史引用的可读性**，
   「旧引用并存」这条验收不受影响。

**代价**（诚实记录）：本包建了 `knowledge_chunks` 表却**不为它定重切策略**，
留给 WP21。若 WP21 认为"旧版本的 chunk 绝不该被删"，它可以在自己的
触发器里表达这件事 —— 那是它的设计空间（判断法见说明文档 §1.6）。

## 二、OP-88：`entity_type`/`entity_id` 与子表一致

WP11 的登记原文（`docs/待定项与问题登记册.md` OP-88）：

> "延迟约束检查 `entity_type`/`entity_id` 与子表一致"（数据结构 02 §5）**未实现**
> —— 前提是子表存在；WP11 只建注册表与依赖边、没有子表，无从校验。

数据结构 02 §5 的原句是：

> 每个具体版本表的 `id` 同时 FK `version_nodes.id`；注册表、具体表及引用
> 同一事务插入。**延迟约束检查 entity_type、entity_id 与实际子表一致，
> 禁止孤立节点。**

WP18 是第一个领域版本表，闭合了它对 `BRIEF` 的那一半。本包闭合
`KNOWLEDGE` 的那一半 —— 两个包各自只对自己负责的 `entity_type` 生效
（反向触发器用 `WHEN` 收窄，见下）。三个判据：

| # | 判据 | 为什么必须存在 |
|---|---|---|
| 1 | `knowledge_versions.id` 必须有一个 `version_nodes` 行 | FK 已经保证了"节点存在"，但 FK **管不住方向**：它只保证被引用的行存在，不保证那行的 `entity_type` 对 |
| 2 | 那个节点的 `entity_type` 必须是 `'KNOWLEDGE'` | 否则一个 `CONTENT` 节点被当成知识版本 —— 注册表与子表说的不是同一件事 |
| 3 | 那个节点的 `entity_id` 必须等于本行的 `asset_id` | 否则"这一版属于哪条知识"有两个互相矛盾的答案，而下游（`version_dependencies` 的溯源）取的是节点侧的那个 |

**为什么必须是延迟（DEFERRABLE INITIALLY DEFERRED）**：
数据结构 02 §5 要求"注册表、具体表及引用**同一事务**插入"。
同事务插入意味着两行的插入顺序由应用决定，而 WP11 的 `register_version`
内部用 SAVEPOINT + 重试取修订号 —— 它**不暴露"节点行何时落库"的控制权**。
延迟到提交时校验，让"谁先谁后"不再是一个必须猜对的问题。
（WP18 已实测：延迟触发器在 INSERT 时静默通过、在 COMMIT 时抛
`sqlstate=23514`。）

### 反向判据（"禁止孤立节点"）的**有意收窄**（与 WP18 同一处理）

完整的不变量是"一个 `KNOWLEDGE` 节点必须配对一条 `knowledge_versions` 行"。
本实现把它收窄为：

> **只对"真实存在的逻辑知识"强制** —— 即 `knowledge_assets` 表里有
> `id = entity_id` 的行时，才要求配对。

收窄的理由与 WP18 完全相同：8 类版本实体里只有 `BRIEF` 与 `KNOWLEDGE`
在本批落地，其余 6 类的领域表还不存在。若不加这一层收窄，
任何"注册一个 `KNOWLEDGE` 节点但暂时没有领域行"的路径都会在提交时失败。

**代价已登记**：`entity_id` 指向一个不存在的 `knowledge_assets.id`
的"悬空节点"不会被拦下。危害等级低（它不被任何东西引用，
溯源也走不到它），而"节点说 A、子表说 B"这类**互相矛盾**的形态
被判据 1~3 完整覆盖。

## 三、`knowledge_publications` 的事件范围必须与本 asset 一致

新增的跨表不变量（规格源没有明写，但它是投影正确性的前提）：

一条发布事件说"asset A 的版本 v 生效"，而 `v` 可能属于 asset B。
CHECK 表达不了这个（它跨表），FK 也表达不了
（`asset_id` 与 `version_id` 各自都有 FK，各自都成立）。

**为什么这条比 OP-88 更危险**：OP-88 的两个节点至少指向同一个实体的
两个说法；而这里会让**投影算出"asset A 的当前生效版本是 B 的版本"** ——
于是 `GET /knowledge/assets/A` 返回的是另一条知识的内容。
一条"看起来有内容、实际是别人的内容"的知识，比"没有内容"危险得多。

## 四、`knowledge_assets.active_version_id` 必须属于本 asset

`active_version_id` 是一列**投影**（见 `models.py`）。它可以被裸 SQL
就地改写 —— 而"这条知识的当前版本指向另一条知识的版本"同样是
一条静默的错误。因此对它加一条 AFTER UPDATE 校验。

**只在值真的变化时校验**（`IS DISTINCT FROM`）：这条 CHECK 会拦下
"每次更新都重跑一遍查询"的无谓开销，也避免与"归档时不改 active_version"
的正常路径互相干扰。

## 五、为什么没有"`KNOWLEDGE` 节点必须配对"之外的注册表触发器

`version_nodes` 是**全系统共用**的表。本包在它上面只加**一个**
被 `WHEN (NEW.entity_type = 'KNOWLEDGE')` 收窄的约束触发器 ——
不加这个条件会让每次 `BRIEF` / `CONTENT` / `PROMPT` 版本注册都白跑一次
函数（而本包只对 `KNOWLEDGE` 有校验能力）。

`WHEN` 在 `CREATE CONSTRAINT TRIGGER` 上可用（WP18 本机实测 PG 17.10）。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.knowledge.models import IMMUTABLE_TABLES
from rednote.platform.immutability import immutable_trigger_ddl

# ---------------------------------------------------------------------------
# SQLSTATE 与名字
# ---------------------------------------------------------------------------

# PostgreSQL check_violation。与 WP11 的 `DEPENDENCY_SCOPE_SQLSTATE`、
# WP18 的 `CONSISTENCY_SQLSTATE` 同一个码：语义上它都是"某条数据规则
# 被违反了"，与本模块的 CHECK 同族，都该映射到 422 VALIDATION_FAILED。
CONSISTENCY_SQLSTATE: Final[str] = "23514"

# --- OP-88：节点一致性 ------------------------------------------------------
KNOWLEDGE_VERSION_FUNCTION: Final[str] = "rednote_check_knowledge_version_consistency"
KNOWLEDGE_VERSION_TRIGGER: Final[str] = "trg_knowledge_versions_node_consistency"
KNOWLEDGE_NODE_FUNCTION: Final[str] = "rednote_check_knowledge_node_paired"
KNOWLEDGE_NODE_TRIGGER: Final[str] = "trg_version_nodes_knowledge_paired"

# --- 发布事件的范围 --------------------------------------------------------
PUBLICATION_SCOPE_FUNCTION: Final[str] = "rednote_check_knowledge_publication_scope"
PUBLICATION_SCOPE_TRIGGER: Final[str] = "trg_knowledge_publications_asset_scope"

# --- 生效版本的范围 --------------------------------------------------------
ACTIVE_VERSION_SCOPE_FUNCTION: Final[str] = "rednote_check_knowledge_active_scope"
ACTIVE_VERSION_SCOPE_TRIGGER: Final[str] = "trg_knowledge_assets_active_scope"


def _raise_block(message_expr: str) -> str:
    """生成一段 RAISE 语句（**字符串拼接，不用 `%`**，见模块 docstring）。"""
    return (
        "        RAISE EXCEPTION USING\n"
        f"            MESSAGE = {message_expr},\n"
        f"            ERRCODE = '{CONSISTENCY_SQLSTATE}';\n"
    )


def knowledge_version_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    挂 `knowledge_versions` AFTER INSERT，延迟到事务提交时执行。
    OP-88 的判据 1~3（见模块 docstring 第二节）。

    `RETURN NULL` 而不是 `RETURN NEW`：**AFTER 触发器的返回值被忽略**，
    写 `NULL` 是明确的"我不改写行"。（`RETURN NEW` 在 AFTER 触发器里
    也不生效，但会让读者以为这里做了行替换。）
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {KNOWLEDGE_VERSION_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    node_type text;\n"
        "    node_entity uuid;\n"
        "BEGIN\n"
        "    SELECT entity_type, entity_id INTO node_type, node_entity\n"
        "      FROM version_nodes WHERE id = NEW.id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'knowledge_versions.id 没有对应的 version_nodes 行：'\n"
            "                      || NEW.id::text"
        )
        + "    END IF;\n"
        "    IF node_type <> 'KNOWLEDGE' THEN\n"
        + _raise_block(
            "'版本节点 ' || NEW.id::text || ' 的 entity_type 是 '\n"
            "                      || node_type || '，不是 KNOWLEDGE'"
        )
        + "    END IF;\n"
        "    IF node_entity <> NEW.asset_id THEN\n"
        + _raise_block(
            "'版本节点 ' || NEW.id::text || ' 的 entity_id（'\n"
            "                      || node_entity::text\n"
            "                      || '）与 knowledge_versions.asset_id（'\n"
            "                      || NEW.asset_id::text || '）不一致'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {KNOWLEDGE_VERSION_TRIGGER}\n"
        "AFTER INSERT ON knowledge_versions\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW EXECUTE FUNCTION "
        f"{KNOWLEDGE_VERSION_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {KNOWLEDGE_VERSION_TRIGGER} ON knowledge_versions"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {KNOWLEDGE_VERSION_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def knowledge_node_paired_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    OP-88 的反向判据："一个 `KNOWLEDGE` 节点必须配对一条
    `knowledge_versions` 行"（含 WP18 的收窄，见模块 docstring 第二节）。

    **挂 `version_nodes` 是刻意的**：这个不变量是"注册表侧不许出现
    没有领域行的节点"，它的检查点只能是被插入的那个节点。
    挂在 `knowledge_versions` 上做不到 —— 那时节点可能还没插入。

    **与 WP18 的 `trg_version_nodes_brief_paired` 在同一张表上并存**：
    两个触发器各自被 `WHEN` 收窄到自己的 `entity_type`，
    互不影响。这是"共用表上按实体类型分区加约束"的标准做法，
    也是唯一能在不改 WP11 代码的前提下闭合 OP-88 的方式。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {KNOWLEDGE_NODE_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "BEGIN\n"
        # 收窄：只对"真实存在的逻辑知识"强制（理由见模块 docstring）。
        "    IF NOT EXISTS (\n"
        "        SELECT 1 FROM knowledge_assets WHERE id = NEW.entity_id\n"
        "    ) THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "    IF NOT EXISTS (\n"
        "        SELECT 1 FROM knowledge_versions WHERE id = NEW.id\n"
        "    ) THEN\n"
        + _raise_block(
            "'版本节点 ' || NEW.id::text\n"
            "                      || ' 声明属于逻辑知识 ' || NEW.entity_id::text\n"
            "                      || '，但没有任何 knowledge_versions 行"
            "（孤立节点）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {KNOWLEDGE_NODE_TRIGGER}\n"
        "AFTER INSERT ON version_nodes\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW WHEN (NEW.entity_type = 'KNOWLEDGE')\n"
        f"EXECUTE FUNCTION {KNOWLEDGE_NODE_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {KNOWLEDGE_NODE_TRIGGER} ON version_nodes"
    drop_function = f"DROP FUNCTION IF EXISTS {KNOWLEDGE_NODE_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def publication_scope_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    规则：`knowledge_publications.version_id` 所属的 asset 必须等于
    本行的 `asset_id`（见模块 docstring 第三节）。

    **可以和 `ACTIVATE`/`ARCHIVE`/`RESTORE` 各自的语义一起看**：
    这三者都用"事件 + 版本"两个坐标定位。若两个坐标指向不同的 asset，
    投影会把 B 的版本算成 A 的当前版本 —— 而 `GET /knowledge/assets/A`
    会返回 B 的内容。

    延迟到提交时校验：事件行与版本行在同一事务里插入，
    而 `version_id` 的 FK 已经保证版本行存在（那是立即的），
    这里查的是"版本的 asset_id"，它在同一事务里可能刚被写入。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {PUBLICATION_SCOPE_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    version_asset uuid;\n"
        "BEGIN\n"
        "    SELECT asset_id INTO version_asset\n"
        "      FROM knowledge_versions WHERE id = NEW.version_id;\n"
        "    IF NOT FOUND THEN\n"
        # FK 已保证存在，走到这里说明有人在同一事务里删了它 ——
        # 而 knowledge_versions 不可变（删不掉）。这是防御性分支。
        + _raise_block(
            "'发布事件引用的版本 ' || NEW.version_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        "    IF version_asset <> NEW.asset_id THEN\n"
        + _raise_block(
            "'发布事件声明的是知识 ' || NEW.asset_id::text\n"
            "                      || '，但版本 ' || NEW.version_id::text\n"
            "                      || ' 属于知识 ' || version_asset::text\n"
            "                      || '（投影会算出别人的版本是本知识的当前版本）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {PUBLICATION_SCOPE_TRIGGER}\n"
        "AFTER INSERT ON knowledge_publications\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW EXECUTE FUNCTION {PUBLICATION_SCOPE_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {PUBLICATION_SCOPE_TRIGGER}"
        " ON knowledge_publications"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {PUBLICATION_SCOPE_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def active_version_scope_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    规则：`knowledge_assets.active_version_id` 必须指向**本 asset 的**版本
    （见模块 docstring 第四节）。

    **AFTER UPDATE**（不是 INSERT）：新建 asset 时 `active_version_id`
    为 NULL；它第一次被赋值是在发布事务里，即一次 UPDATE。

    `IS DISTINCT FROM` 让"值没变"时直接短路：
      · 归档只改 `archived_at`，不触碰 `active_version_id` ——
        不该为此多跑一次查询；
      · 幂等重试会重复写入同样的值 —— 同样不必校验。

    **不是延迟的**：这条校验只读 `knowledge_versions`（本行 UPDATE 时
    它必然已经存在 —— FK 的立即性在 UPDATE 时就检查了被引用的节点，
    而版本行的存在由发布事务先插入或已存在保证）。不延迟让错误
    在 UPDATE 那一刻就暴露，位置更靠近犯错的地方。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {ACTIVE_VERSION_SCOPE_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    version_asset uuid;\n"
        "BEGIN\n"
        "    IF NEW.active_version_id IS NULL THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "    IF NEW.active_version_id IS NOT DISTINCT FROM OLD.active_version_id THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "    SELECT asset_id INTO version_asset\n"
        "      FROM knowledge_versions WHERE id = NEW.active_version_id;\n"
        "    IF NOT FOUND THEN\n"
        # `active_version_id` 的 FK 指向 version_nodes（延迟），
        # 因此**可能**在节点存在而领域行还没写入的瞬间被 UPDATE 到。
        # 那种情况下这里会命中 —— 报出来比静默通过好：
        # 一个指向"不存在领域行"的节点的 active_version_id 是坏数据。
        + _raise_block(
            "'生效版本 ' || NEW.active_version_id::text\n"
            "                      || ' 没有任何 knowledge_versions 行'"
        )
        + "    END IF;\n"
        "    IF version_asset <> NEW.id THEN\n"
        + _raise_block(
            "'知识 ' || NEW.id::text || ' 的生效版本指向 '\n"
            "                      || version_asset::text\n"
            "                      || ' 的版本（不是本知识的版本）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {ACTIVE_VERSION_SCOPE_TRIGGER}\n"
        "AFTER UPDATE ON knowledge_assets\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW EXECUTE FUNCTION {ACTIVE_VERSION_SCOPE_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {ACTIVE_VERSION_SCOPE_TRIGGER}"
        " ON knowledge_assets"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {ACTIVE_VERSION_SCOPE_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块的**跨表一致性**触发器（OP-88 等）。

    **为什么公开（去掉下划线）**：迁移的 `downgrade` 需要**只删本模块自己的
    函数**（`rednote_check_knowledge_*`），而 `all_trigger_ddl()` 里同时混着
    不可变保护那 2 项 —— 它们的 `drop_function` 指向**跨包共用**的
    `rednote_reject_mutation()`，删了会撞依赖错误并让 WP15/WP16/WP17/WP18
    的保护一起失效（WP16 差点踩、WP18 明确防了）。

    因此 downgrade 必须能单独拿到"一致性那 4 项"。
    判据：**这个对象是不是我建的** —— 是就删，不是就不删。
    """
    items: list[tuple[str, str, str, str, str]] = []
    for table, builder in (
        ("knowledge_versions", knowledge_version_consistency_ddl),
        ("version_nodes", knowledge_node_paired_ddl),
        ("knowledge_publications", publication_scope_ddl),
        ("knowledge_assets", active_version_scope_ddl),
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

    **注意 `version_nodes` 也在列表里**：`trg_version_nodes_knowledge_paired`
    是本模块为了闭合 OP-88 而挂到**共用注册表**上的触发器。
    它不修改 WP11 的任何代码或表结构 —— 它只是 `version_nodes` 上的
    一个新触发器对象，且被 `WHEN (NEW.entity_type = 'KNOWLEDGE')` 限定到
    本模块负责的实体类型。**主 agent 落迁移时必须把它一并建出**，
    否则 OP-88 只闭合了一半（判据 1~3 有、反向判据没有）。

    **同样注意 `knowledge_assets` 也在列表里**（它的 `active_scope` 触发器），
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
        KNOWLEDGE_VERSION_TRIGGER,
        KNOWLEDGE_NODE_TRIGGER,
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
    "KNOWLEDGE_NODE_FUNCTION",
    "KNOWLEDGE_NODE_TRIGGER",
    "KNOWLEDGE_VERSION_FUNCTION",
    "KNOWLEDGE_VERSION_TRIGGER",
    "PUBLICATION_SCOPE_FUNCTION",
    "PUBLICATION_SCOPE_TRIGGER",
    "active_version_scope_ddl",
    "all_trigger_ddl",
    "consistency_items",
    "consistency_trigger_names",
    "immutable_trigger_names",
    "knowledge_node_paired_ddl",
    "knowledge_version_consistency_ddl",
    "publication_scope_ddl",
    "trigger_names",
]