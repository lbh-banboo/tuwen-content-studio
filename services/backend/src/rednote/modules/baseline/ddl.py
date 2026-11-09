"""WP22 的触发器 DDL（不可变保护 + 三条不变量 + OP-88 第三半）。

**为什么 DDL 生成放在模块里而不是迁移文件里**（与 WP11/WP18/WP20/WP23 同一理由）：
验证脚本要按名字回查 `pg_trigger`。若名字由迁移文件手写，验证脚本也得
手写一遍同样的字符串，两处一旦不一致，"找回触发器"的检查就会**假失败**
或**假通过**。名字只有一个来源才靠得住。

---

## 一、不可变保护（`production_baselines` / `baseline_items` / `step_approvals`）

复用跨包共用的 `rednote_reject_mutation()` 函数（WP11 建）。
**本包只挂触发器，不重建函数**，且 `downgrade` **不删它** ——
删了会撞依赖错误并让 WP11/WP15/WP16/WP17/WP18/WP20/WP23 的保护一起失效
（WP16 差点踩、WP18/WP20/WP23 明确防了）。

## 二、OP-88：`BASELINE` 的节点一致性（正向 + 反向，**闭合第三半**）

数据结构 02 §5 的原句是：

> 每个具体版本表的 `id` 同时 FK `version_nodes.id`；注册表、具体表及引用
> 同一事务插入。**延迟约束检查 entity_type、entity_id 与实际子表一致，
> 禁止孤立节点。**

WP18 闭合了 `BRIEF` 那一半、WP20 闭合了 `KNOWLEDGE` 那一半、WP23 闭合了
`PROMPT` 那一半。**本包闭合 `BASELINE`** —— 这是说明文档 §1.1 所说
"一个已定义的类型**永远不可能有实例**"的那个口子
（`ENTITY_TYPE_BASELINE` 在 `versions/models.py` L67 已定义，但在此之前
**没有任何生产者**）。

三个判据（正向，挂 `production_baselines`）：

| # | 判据 | 为什么必须存在 |
|---|---|---|
| 1 | `production_baselines.id` 必须有一个 `version_nodes` 行 | FK 已保证"节点存在"，但 FK **管不住方向**：它只保证被引用的行存在，不保证那行的 `entity_type` 对 |
| 2 | 那个节点的 `entity_type` 必须是 `'BASELINE'` | 否则一个 `BRIEF` 节点被当成基线版本 —— 注册表与子表说的不是同一件事 |
| 3 | 那个节点的 `entity_id` 必须等于本行的 `project_id` | `BASELINE` 的"逻辑实体"就是该项目的那条基线链（`models.py` 模块 docstring 第六节）。不一致意味着"给 A 项目注册了一个声称属于 B 项目的基线" |

### 反向判据的**有意收窄**（与 WP18/WP20/WP23 同一处理）

完整的不变量是"一个 `BASELINE` 节点必须配对一条 `production_baselines` 行"。
本实现把它收窄为：

> **只对"真实存在的项目"强制** —— 即 `projects` 表里有
> `id = entity_id` 的行时，才要求配对。

收窄的理由与另三个包完全相同：8 类版本实体里只有 `BRIEF` / `KNOWLEDGE` /
`BASELINE` / `PROMPT` 在本批落地，其余 4 类的领域表还不存在；
而 `version_nodes` 是共用表，任何"注册一个 `BASELINE` 节点但没有领域行"
的中间态不该由本包的触发器拦下。

**代价已登记**：`entity_id` 指向不存在项目的"悬空节点"不会被拦。
危害等级低（它不被任何东西引用，溯源也走不到它），
而"节点说 A、子表说 B"这类**互相矛盾**的形态被判据 1~3 完整覆盖。

## 三、第 3 层防线：跨品牌不得引用（AC-24 的落点）

G2 出口原文：「**跨品牌事实不串**」。这条的形态是"**子行引用的资产，
其品牌必须与基线所属项目一致**"——**跨表条件**。

**为什么不能用 CHECK**：说明文档 §0.3 实测 1 已证 PG **直接拒绝**
CHECK 里的子查询（`cannot use subquery in check constraint`，FeatureNotSupported）。
这不是风格选择，是类型系统的硬约束。

**为什么不用"冗余列 + CHECK"折中**：实测 1 试 3 已证它**恒真** ——
冗余列与父行不一致时 CHECK 不会报错。这与 ISS-75（判据建立在数据库
已保证的事上）同族：一个恒真的判据从不报错，它只是让防线看起来存在。

### ⚠️ 实现里最容易写错的一处：必须用 `IS DISTINCT FROM`

本判据要比较的列**全都可以为 NULL**（`knowledge_assets.brand_id` 在
`scope='GLOBAL'` 时为 NULL）。若写成 `k_brand <> proj_brand`：

    NULL <> uuid  →  NULL  →  IF 视为 false  →  **放行**

于是"知识没有品牌"与"项目有品牌"这个组合**静默通过** ——
而这正是跨品牌引用最常见的形态（一个 GLOBAL 形态的库里塞了一条
带品牌的事实）。`IS DISTINCT FROM` 是 NULL 安全的比较，
它把 NULL 当成一个正常值参与相等判断。

**这条不变量不能用"只判非空"替代**：判非空恒真（任何写进去的值都非空），
与说明文档 §0.3 实测 1 试 3 是同一个形态。

### 第 3 层的另一半：引用的**版本**必须属于引用的**资产**

`ref` 在 `baseline_items` 里是**一对**列（决策 1）：`knowledge_asset_id` +
`knowledge_version_id`。两条 FK 各自只保证"存在"，
**没有任何东西**保证"这个版本属于这个资产"：

    本行：asset_id = A, version_id = V
    而 V 实际属于资产 B  →  两条 FK 全绿，基线照建

后果是 AC-24 那条「保存实际选用条目、**版本**、片段」的溯源链**指错地方**，
而它是**静默的**（基线能建、能读回、快照也在）。

WP20 的 `trg_knowledge_publications_asset_scope` 与
WP23 的 `trg_prompt_publications_template_scope` 对完全同构的场景
（"事件引用的版本必须属于声明的主体"）都建了这个触发器 ——
本包不建就成了全项目唯一的口子。落在 `asset_version_scope_ddl()`。

**与跨品牌判据分成两个函数**（WP20/WP23 是合在一个里的）：
前者查 `knowledge_assets` 的 scope 列，本条查 `knowledge_versions.asset_id` ——
不是同一张表。合并后两条 `RAISE` 混在一起，"失败原因是哪一个"
只能靠读消息文本区分，而它们是两个不同的问题。

## 四、第 4 层防线：基线必须有**同事务**批准（OP-151 原文的硬约束化）

协议原文（经 WP19 §6 转述）：「步骤 3 创建 baseline 时**同事务**
生成 step approval」。"同事务"三个字，能不能落成数据库级保证？

说明文档 §0.3 实测 2 已证**能**，且必须这样做：

```
试 1  布尔冗余列 + check (has_approval)
      → 实测：has_approval=true 而 approval 表里一行都没有，CHECK 全绿 ✗
      （声称方与事实方分离 —— 又一次同族）
试 2  CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED
试 2a 反例：只插 baseline，不插 approval → COMMIT 被拒 ✅  sqlstate=23514
试 2b 正例：**同一事务**先插 baseline 再插 approval → 通过 ✅
试 2c 反例：只有 baseline 的裸事务 → 被拒 ✅
```

`DEFERRABLE INITIALLY DEFERRED` 让校验发生在 **COMMIT 期**，因此
"同事务"成为硬约束，同时带来两个附带好处：

1. **顺序不再是必须猜对的事** —— 实现方不必猜"节点行何时落库"；
2. **跨事务补批准这条路被物理封死** —— 这是"事后补一张批准单"
   这种绕过的唯一入口，而它**不报错**（数据库里最终看起来是一致的：
   有基线、有批准，只是中间有一段时间不一致）。

**判据要求"生效的那条批准"**：`supersedes_id IS NULL` 收窄 ——
一条已被后续批准取代的旧批准不构成依据（否则"先批后撤"形同没撤）。

## 五、第 5 层防线：存在阻断项不得创建基线（AC-09 的落点）

原型 L170~174 原文：「真实环境下**后端将拒绝存在阻断项时推进**（AC-09）」。

判据是**对明细行的否定式查询**，不读任何 count 列（决策 D7）：

```sql
NOT EXISTS (未处置的阻断冲突 IN 本项目草稿)
```

**为什么必须是数据库级**：V3 方案（把这条退化成"服务层自觉"）
已被实测 2 试 2c 否定 —— 裸事务不报错地长期开放。而 AC-09 的失效
意味着**基线可以被非法推进**，这是本包最严重的后果（说明文档 §1.6 R5）。

### 与 `readiness.py` 的关系（**这不是"两处实现"，是两层**）

设计文档 §2.1 把这两者列为**第 5 层**（DB）与**第 7 层**（服务层纯函数），
它们是纵深防御的两层，不是一个判据的两处实现：

| | 第 5 层（本文件） | 第 7 层（`readiness.py`）|
|---|---|---|
| 拦的是 | 绕过服务层的写入（裸 SQL / 数据回填） | 正常请求路径，给出**可读**的拒绝理由 |
| 判据来源 | 本文件拼接的 SQL | `step3_readiness()` |
| 失败时 | `sqlstate=23514` → 422 | 带 `reason` 与未满足条件的 422 |

**防止两层漂移的机制**：两处的**取值与键名都从 `baseline_types` 拼接**
（`CONFLICT_STATE_SETTLED`、`blocking` 键名），且有**对账测试**：
构造同一批冲突数据，断言 `readiness.blocking_open_conflicts()`
与数据库触发器的判定**同时**为真/为假。若某天只改了一处，对账测试会红。

## 六、为什么 `version_nodes` 上只加**一个**被 `WHEN` 收窄的触发器

`version_nodes` 是**全系统共用**的表。本包在它上面只加一个被
`WHEN (NEW.entity_type = 'BASELINE')` 收窄的约束触发器 ——
不加这个条件会让每次 `BRIEF` / `KNOWLEDGE` / `PROMPT` / `CONTENT`
版本注册都白跑一次函数（而本包只对 `BASELINE` 有校验能力）。

`WHEN` 在 `CREATE CONSTRAINT TRIGGER` 上可用（WP18 本机实测 PG 17.10）。

**跨包回归要求**：测试要验证另三个 `entity_type` 的注册**不受影响**
（说明文档 §1.6 R8）—— 三个 `WHEN` 收窄的触发器挂同一张表上，
若 `WHEN` 写错或漏写，轻则性能、重则误拦别包的版本注册。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.baseline.baseline_types import (
    APPROVAL_DECISION_APPROVED,
    CONFLICT_STATE_SETTLED,
    KNOWLEDGE_SCOPE_MATCH_RULE,
    SCOPE_MATCH_ALWAYS,
    SCOPE_MATCH_SAME_BRAND,
    SCOPE_MATCH_SAME_PRODUCT,
    SCOPE_MATCH_SAME_PROJECT,
    STEP_MIN,
    SUBJECT_KIND_BASELINE,
)
from rednote.modules.baseline.models import IMMUTABLE_TABLES
from rednote.platform.immutability import immutable_trigger_ddl
from rednote.modules.versions.models import ENTITY_TYPE_BASELINE

# ---------------------------------------------------------------------------
# SQLSTATE 与名字
# ---------------------------------------------------------------------------

# PostgreSQL check_violation。与 WP11 的 `DEPENDENCY_SCOPE_SQLSTATE`、
# WP18/WP20/WP23 的 `CONSISTENCY_SQLSTATE` 同一个码：
# 语义上它都是"某条数据规则被违反了"，与本模块的 CHECK 同族，
# 都该映射到 422 VALIDATION_FAILED。
CONSISTENCY_SQLSTATE: Final[str] = "23514"

# --- OP-88：节点一致性（正向 + 反向） ---------------------------------------
BASELINE_VERSION_FUNCTION: Final[str] = "rednote_check_baseline_version_consistency"
BASELINE_VERSION_TRIGGER: Final[str] = "trg_production_baselines_node_consistency"
BASELINE_NODE_FUNCTION: Final[str] = "rednote_check_baseline_node_paired"
BASELINE_NODE_TRIGGER: Final[str] = "trg_version_nodes_baseline_paired"

# --- 第 3 层：跨品牌 --------------------------------------------------------
CROSS_BRAND_FUNCTION: Final[str] = "rednote_check_baseline_item_scope"
CROSS_BRAND_TRIGGER: Final[str] = "trg_baseline_items_cross_brand"

# --- 第 3 层（之二）：引用的版本必须属于引用的资产 ---------------------------
# 与跨品牌**拆成两个函数**（不像 WP20/WP23 那样合并，见 `asset_version_scope_ddl`
# 的 docstring）：两条判据查的**不是同一张表** —— 跨品牌查 `knowledge_assets`，
# 本条查 `knowledge_versions`。合并会让"这次失败是哪一个原因"读不出来。
ASSET_SCOPE_FUNCTION: Final[str] = "rednote_check_baseline_item_asset_scope"
ASSET_SCOPE_TRIGGER: Final[str] = "trg_baseline_items_asset_scope"

# --- 第 4 层：同事务批准 ----------------------------------------------------
APPROVAL_REQUIRED_FUNCTION: Final[str] = "rednote_check_baseline_approval"
APPROVAL_REQUIRED_TRIGGER: Final[str] = "trg_production_baselines_approval_required"

# --- 第 5 层：阻断项 --------------------------------------------------------
NO_BLOCKING_FUNCTION: Final[str] = "rednote_check_baseline_no_blocking"
NO_BLOCKING_TRIGGER: Final[str] = "trg_production_baselines_no_blocking"

#: 步骤 3 的步骤号。**从 `STEP_MIN` 派生而不是写字面量 `3`**：
#: 那个 `3` 是本包对"基线是步骤 3 的产物"的定义，
#: 散写成字面量会让它与 `STEP_NAMES[3]` 的改名不同步。
BASELINE_STEP_NO: Final[int] = STEP_MIN + 2


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


def _scope_match_case() -> str:
    """把 `KNOWLEDGE_SCOPE_MATCH_RULE` 编译成 plpgsql 的分支序列。

    **判据的取值来自 `baseline_types`，不在这里重写** ——
    见模块 docstring 第五节末尾的"防止两层漂移的机制"。
    """
    branches: list[str] = []
    for scope, rule in sorted(KNOWLEDGE_SCOPE_MATCH_RULE.items()):
        if rule == SCOPE_MATCH_ALWAYS:
            # GLOBAL：对任何项目都可用。**不是放水** ——
            # `FACT` 类别被 WP20 的 `ck_knowledge_assets_fact_requires_concrete_scope`
            # 禁止为 GLOBAL，因此放行 GLOBAL 不会让任何一条**事实**跨界。
            branches.append(
                f"    IF k_scope = '{scope}' THEN\n"
                "        RETURN NULL;\n"
                "    END IF;\n"
            )
        elif rule == SCOPE_MATCH_SAME_BRAND:
            branches.append(
                f"    IF k_scope = '{scope}' THEN\n"
                "        IF k_brand IS DISTINCT FROM proj_brand THEN\n"
                + _raise_block(
                    "'基线项引用的知识属于品牌 '\n"
                    "                          || COALESCE(k_brand::text, '(未声明)')\n"
                    "                          || '，而本项目属于品牌 '\n"
                    "                          || COALESCE(proj_brand::text, '(未声明)')\n"
                    "                          || '（AC-24：跨品牌事实不得迁移）'"
                )
                + "        END IF;\n"
                "        RETURN NULL;\n"
                "    END IF;\n"
            )
        elif rule == SCOPE_MATCH_SAME_PRODUCT:
            branches.append(
                f"    IF k_scope = '{scope}' THEN\n"
                "        IF k_brand IS DISTINCT FROM proj_brand\n"
                "           OR k_product IS DISTINCT FROM proj_product THEN\n"
                + _raise_block(
                    "'基线项引用的知识属于产品 '\n"
                    "                          || COALESCE(k_product::text, '(未声明)')\n"
                    "                          || '，而本项目属于产品 '\n"
                    "                          || COALESCE(proj_product::text, '(未声明)')\n"
                    "                          || '（AC-24：跨品牌事实不得迁移）'"
                )
                + "        END IF;\n"
                "        RETURN NULL;\n"
                "    END IF;\n"
            )
        elif rule == SCOPE_MATCH_SAME_PROJECT:
            branches.append(
                f"    IF k_scope = '{scope}' THEN\n"
                "        IF k_project IS DISTINCT FROM proj_id THEN\n"
                + _raise_block(
                    "'基线项引用的知识属于项目 '\n"
                    "                          || COALESCE(k_project::text, '(未声明)')\n"
                    "                          || '，不是本项目 ' || proj_id::text"
                )
                + "        END IF;\n"
                "        RETURN NULL;\n"
                "    END IF;\n"
            )
        else:  # pragma: no cover - 受控取值域已穷尽，这是防御
            raise AssertionError(f"未处理的 scope 匹配规则：{rule!r}")
    return "".join(branches)


def baseline_version_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    挂 `production_baselines` AFTER INSERT，延迟到事务提交时执行。
    OP-88 的判据 1~3（见模块 docstring 第二节）。

    `RETURN NULL` 而不是 `RETURN NEW`：**AFTER 触发器的返回值被忽略**，
    写 `NULL` 是明确的"我不改写行"。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {BASELINE_VERSION_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    node_type text;\n"
        "    node_entity uuid;\n"
        "BEGIN\n"
        "    SELECT entity_type, entity_id INTO node_type, node_entity\n"
        "      FROM version_nodes WHERE id = NEW.id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'production_baselines.id 没有对应的 version_nodes 行：'\n"
            "                      || NEW.id::text"
        )
        + "    END IF;\n"
        f"    IF node_type <> '{ENTITY_TYPE_BASELINE}' THEN\n"
        + _raise_block(
            "'版本节点 ' || NEW.id::text || ' 的 entity_type 是 '\n"
            "                      || node_type || '，不是 "
            f"{ENTITY_TYPE_BASELINE}'"
        )
        + "    END IF;\n"
        "    IF node_entity <> NEW.project_id THEN\n"
        + _raise_block(
            "'版本节点 ' || NEW.id::text || ' 的 entity_id（'\n"
            "                      || node_entity::text\n"
            "                      || '）与 production_baselines.project_id（'\n"
            "                      || NEW.project_id::text || '）不一致'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {BASELINE_VERSION_TRIGGER}\n"
        "AFTER INSERT ON production_baselines\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW EXECUTE FUNCTION "
        f"{BASELINE_VERSION_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {BASELINE_VERSION_TRIGGER}"
        " ON production_baselines"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {BASELINE_VERSION_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def baseline_node_paired_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    OP-88 的反向判据："一个 `BASELINE` 节点必须配对一条
    `production_baselines` 行"（含另三个包同款的收窄，见模块 docstring 第二节）。

    **挂 `version_nodes` 是刻意的**：这个不变量是"注册表侧不许出现
    没有领域行的节点"，它的检查点只能是被插入的那个节点。
    挂在 `production_baselines` 上做不到 —— 那时节点可能还没插入。

    **与 WP18 的 `trg_version_nodes_brief_paired`、
    WP20 的 `trg_version_nodes_knowledge_paired`、
    WP23 的 `trg_version_nodes_prompt_paired` 在同一张表上并存**：
    四个触发器各自被 `WHEN` 收窄到自己的 `entity_type`，互不影响。
    这是"共用表上按实体类型分区加约束"的标准做法。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {BASELINE_NODE_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "BEGIN\n"
        # 收窄：只对"真实存在的项目"强制（理由见模块 docstring）。
        "    IF NOT EXISTS (\n"
        "        SELECT 1 FROM projects WHERE id = NEW.entity_id\n"
        "    ) THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "    IF NOT EXISTS (\n"
        "        SELECT 1 FROM production_baselines WHERE id = NEW.id\n"
        "    ) THEN\n"
        + _raise_block(
            "'版本节点 ' || NEW.id::text\n"
            "                      || ' 声明属于项目 ' || NEW.entity_id::text\n"
            "                      || '，但没有任何 production_baselines 行（孤立节点）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {BASELINE_NODE_TRIGGER}\n"
        "AFTER INSERT ON version_nodes\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW WHEN (NEW.entity_type = '{ENTITY_TYPE_BASELINE}')\n"
        f"EXECUTE FUNCTION {BASELINE_NODE_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {BASELINE_NODE_TRIGGER} ON version_nodes"
    drop_function = f"DROP FUNCTION IF EXISTS {BASELINE_NODE_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def cross_brand_reference_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    **第 3 层防线**：基线项引用的知识不得跨品牌（AC-24 / G2 出口）。

    挂 `baseline_items` AFTER INSERT，延迟到事务提交时执行 ——
    理由与 OP-88 相同：同一事务内 `production_baselines` 与 `baseline_items`
    的插入顺序由服务层决定，且 `register_version` 的取号重试不暴露节点时机。

    `NEW.knowledge_asset_id IS NULL` 时直接放行：研究资料、风格、规则等
    条目不一定引用知识资产（原型 L93~102 的"21 份选用研究资料"就不是知识资产）。
    **本判据只对"确实引用了知识资产"的条目生效** ——
    对不适用的行强制执行会让合法的基线创建失败。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {CROSS_BRAND_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    proj_brand uuid;\n"
        "    proj_product uuid;\n"
        "    proj_id uuid;\n"
        "    k_scope text;\n"
        "    k_brand uuid;\n"
        "    k_product uuid;\n"
        "    k_project uuid;\n"
        "BEGIN\n"
        "    IF NEW.knowledge_asset_id IS NULL THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "\n"
        "    SELECT p.brand_id, p.product_id, p.id\n"
        "      INTO proj_brand, proj_product, proj_id\n"
        "      FROM production_baselines b\n"
        "      JOIN projects p ON p.id = b.project_id\n"
        "     WHERE b.id = NEW.baseline_id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'基线项 ' || NEW.item_no::text || ' 的基线 '\n"
            "                      || NEW.baseline_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        "\n"
        "    SELECT a.scope, a.brand_id, a.product_id, a.project_id\n"
        "      INTO k_scope, k_brand, k_product, k_project\n"
        "      FROM knowledge_assets a\n"
        "     WHERE a.id = NEW.knowledge_asset_id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'基线项引用的知识资产 '\n"
            "                      || NEW.knowledge_asset_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        "\n"
        # ⚠️ 全部用 IS DISTINCT FROM（NULL 安全），不能用 `<>`。
        # 理由见模块 docstring 第三节 —— 用 `<>` 时 NULL 比较返回 NULL，
        # IF 视为 false，跨品牌引用会**静默通过**。
        + _scope_match_case()
        + _raise_block(
            "'基线项引用的知识 scope 是 ' || COALESCE(k_scope, '(空)')\n"
            "                      || '，不是受控取值域内的值'"
        )
        + "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {CROSS_BRAND_TRIGGER}\n"
        "AFTER INSERT ON baseline_items\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW EXECUTE FUNCTION {CROSS_BRAND_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {CROSS_BRAND_TRIGGER} ON baseline_items"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {CROSS_BRAND_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def asset_version_scope_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    **第 3 层防线的另一半**：`baseline_items` 的 `ref` 是
    `knowledge_asset_id` + `knowledge_version_id` **一对**（决策 1），
    而两条 FK 各自只保证"这行资产存在""这行版本存在" ——
    **没有任何东西保证"这个版本属于这个资产"**。

    | 错误形态 | 两条 FK 的表态 | 后果 |
    |---|---|---|
    | `version_id` 指向**别的资产**的版本 | **全绿**（那个版本确实存在） | 断链：AC-24 要求「保存实际选用条目、**版本**、片段」，而"这条来自哪一版"的答案是错的 |
    | 该资产后来出了新版本 | **全绿** | 无（本行存的是**固定版本 id**，正是 R1 的防线） |

    **为什么这条判据必须存在**：WP20 的 `trg_knowledge_publications_asset_scope`
    与 WP23 的 `trg_prompt_publications_template_scope` 对**完全同构**的场景
    （"事件引用的版本必须属于声明的主体"）都建了这个触发器。
    本包若不建，`baseline_items` 就是全项目唯一一处
    "版本可以挂到任意资产上"的表 —— 而它的失效是**静默的**：
    基线能建出来、能读回、快照也在，只有溯源链是错的。

    **为什么不与 `cross_brand_reference_ddl` 合并成一个函数**
    （WP20/WP23 是把两条合在一个函数里的）：那两条查的是同一张
    `knowledge_assets` 表、同一个"这一版属于谁"的判据；
    本条查的是**另一张表** `knowledge_versions`。
    合并后两条 `RAISE` 混在一个函数体里，"这次失败是跨品牌还是挂错资产"
    只能靠读消息文本区分 —— 而它是两个不同的问题、两个不同的修法。

    `knowledge_version_id IS NULL` 时直接放行：研究资料、风格等条目
    不一定引用知识版本（与跨品牌判据同款收窄）。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {ASSET_SCOPE_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    version_asset uuid;\n"
        "BEGIN\n"
        "    IF NEW.knowledge_version_id IS NULL THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "\n"
        "    SELECT v.asset_id INTO version_asset\n"
        "      FROM knowledge_versions v\n"
        "     WHERE v.id = NEW.knowledge_version_id;\n"
        "    IF NOT FOUND THEN\n"
        # FK 已保证存在；走到这里只有一种可能：有人在同一事务里删了那行 ——
        # 而 knowledge_versions 不可变（删不掉）。防御性分支。
        + _raise_block(
            "'基线项 ' || NEW.item_no::text || ' 引用的知识版本 '\n"
            "                      || NEW.knowledge_version_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        "\n"
        # NULL 安全比较：本行的 `knowledge_asset_id` 由 CHECK
        # (`version_ref_needs_asset_ref`) 保证非空，但写成 `<>` 会让
        # "万一有 NULL"这条路径静默放行 —— 与跨品牌判据同一条纪律。
        "    IF version_asset IS DISTINCT FROM NEW.knowledge_asset_id THEN\n"
        + _raise_block(
            "'基线项引用的版本 ' || NEW.knowledge_version_id::text\n"
            "                      || ' 属于知识资产 '\n"
            "                      || COALESCE(version_asset::text, '(未知)')\n"
            "                      || '，而本行声明的是资产 '\n"
            "                      || COALESCE(NEW.knowledge_asset_id::text, '(空)')\n"
            "                      || '（AC-24：条目须保存实际选用的版本）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {ASSET_SCOPE_TRIGGER}\n"
        "AFTER INSERT ON baseline_items\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW EXECUTE FUNCTION {ASSET_SCOPE_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {ASSET_SCOPE_TRIGGER} ON baseline_items"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {ASSET_SCOPE_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def approval_required_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    **第 4 层防线**：基线必须在**同一事务**里有对应批准（OP-151 原文的
    硬约束化，说明文档 §0.3 实测 2）。

    判据（三条，缺一不可）：
      · 存在一条批准，其 `subject_version_id` = **本基线版本节点 id**；
      · 该批准的 `decision = 'APPROVED'`（拒绝不构成依据）；
      · 该批准**未被取代**（`supersedes_id IS NULL` 的收窄）——
        一条已被后续批准取代的旧批准不构成依据，否则"先批后撤"形同没撤。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {APPROVAL_REQUIRED_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "BEGIN\n"
        "    IF NOT EXISTS (\n"
        "        SELECT 1 FROM step_approvals sa\n"
        "         WHERE sa.project_id = NEW.project_id\n"
        f"           AND sa.step_no = {BASELINE_STEP_NO}\n"
        f"           AND sa.subject_kind = '{SUBJECT_KIND_BASELINE}'\n"
        "           AND sa.subject_version_id = NEW.id\n"
        "           AND sa.decision = '"
        + APPROVAL_DECISION_APPROVED
        + "'\n"
        "           AND NOT EXISTS (\n"
        "               SELECT 1 FROM step_approvals s2\n"
        "                WHERE s2.supersedes_id = sa.id\n"
        "           )\n"
        "    ) THEN\n"
        + _raise_block(
            "'基线 ' || NEW.id::text || ' 在提交时没有同事务的批准'\n"
            "                      || '（步骤 3 / BASELINE / APPROVED），'\n"
            "                      || '或该批准已被取代（OP-151：同事务生成 step approval）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {APPROVAL_REQUIRED_TRIGGER}\n"
        "AFTER INSERT ON production_baselines\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW EXECUTE FUNCTION {APPROVAL_REQUIRED_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {APPROVAL_REQUIRED_TRIGGER}"
        " ON production_baselines"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {APPROVAL_REQUIRED_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def no_blocking_conflict_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    **第 5 层防线**：存在未处置的**阻断**项时不得创建基线（AC-09）。

    数据源是本项目 `information_drafts.conflicts` 这个 JSONB 数组 ——
    判据写成**对明细行的否定式查询**（`NOT EXISTS`），**不读任何 count 列**：
    D7 的依据是本项目已踩 10 次的同族问题（ISS-78/84/85/86/94/95），
    一个由别处维护的 `blocking_count` 与明细脱节时不报错，
    只会让 AC-09 失效 —— 而它的失效意味着**基线可以被非法推进**。

    **`blocking` 与 `state` 的键名与本文件的取值都从 `baseline_types` 派生**
    （见模块 docstring 第五节的"防止两层漂移的机制"）。
    `blocking` 是 JSONB 里的键名，不是受控取值 —— 它对应
    `readiness.blocking_open_conflicts` 读的同一个键，两处必须一致
    （对账测试会红，若某天只改了一处）。

    **草稿不存在时放行**：`information_drafts` 一项目一份，但
    "没有草稿"是一个合法的中间态（还没开始审核）。此时没有明细行，
    "不存在未处置的阻断冲突"这个否定式**天然成立** —— 判据正确地放行，
    而不是因为漏判。服务层的 `readiness` 在同情况下会因条件 ② 未满足
    而拒绝（没有场景就没有可生产场景），两层各管自己那一半。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {NO_BLOCKING_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    open_count integer;\n"
        "    sample_ids text;\n"
        "BEGIN\n"
        "    SELECT COUNT(*),\n"
        "           COALESCE(string_agg(\n"
        "               COALESCE(c->>'conflictId', c->>'conflict_id', '?'),\n"
        "               '、' ORDER BY COALESCE(c->>'conflictId', c->>'conflict_id', '?')),\n"
        "               '')\n"
        "      INTO open_count, sample_ids\n"
        "      FROM information_drafts d,\n"
        "           LATERAL jsonb_array_elements(d.conflicts) AS c\n"
        "     WHERE d.project_id = NEW.project_id\n"
        "       AND COALESCE((c->>'blocking')::boolean, false)\n"
        f"       AND COALESCE(c->>'state', '') <> '{CONFLICT_STATE_SETTLED}';\n"
        "    IF COALESCE(open_count, 0) > 0 THEN\n"
        + _raise_block(
            "'本项目存在 ' || open_count::text || ' 项未处置的阻断问题（'\n"
            "                      || sample_ids\n"
            "                      || '），不得创建基线（AC-09）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {NO_BLOCKING_TRIGGER}\n"
        "AFTER INSERT ON production_baselines\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW EXECUTE FUNCTION {NO_BLOCKING_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {NO_BLOCKING_TRIGGER}"
        " ON production_baselines"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {NO_BLOCKING_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块的**跨表一致性**触发器（OP-88 第三半 + 三层不变量）。

    **为什么公开（去掉下划线）**：迁移的 `downgrade` 需要**只删本模块自己的
    函数**（`rednote_check_baseline_*`），而 `all_trigger_ddl()` 里同时混着
    不可变保护那 3 项 —— 它们的 `drop_function` 指向**跨包共用**的
    `rednote_reject_mutation()`，删了会撞依赖错误并让
    WP11/WP15/WP16/WP17/WP18/WP20/WP23 的保护一起失效。

    因此 downgrade 必须能单独拿到"一致性那 6 项"。
    判据：**这个对象是不是我建的** —— 是就删，不是就不删。

    ⚠️ **这个列表的长度是判据的一部分**：它与 `consistency_trigger_names()` /
    `consistency_function_names()` 必须**等长**（各 6 项）。收口时由
    `check_evidence_scripts.py` 与测试双向核对 —— 数错一个的后果是
    `downgrade` 漏删一个函数（残留对象会挡住后续迁移重建它）。
    """
    items: list[tuple[str, str, str, str, str]] = []
    for table, builder in (
        ("production_baselines", baseline_version_consistency_ddl),
        ("version_nodes", baseline_node_paired_ddl),
        ("baseline_items", cross_brand_reference_ddl),
        ("baseline_items", asset_version_scope_ddl),
        ("production_baselines", approval_required_ddl),
        ("production_baselines", no_blocking_conflict_ddl),
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

    **注意 `version_nodes` 也在列表里**：`trg_version_nodes_baseline_paired`
    是本模块为了闭合 OP-88 第三半而挂到**共用注册表**上的触发器。
    它不修改 WP11 的任何代码或表结构 —— 它只是 `version_nodes` 上的
    一个新触发器对象，且被 `WHEN (NEW.entity_type = 'BASELINE')` 限定到
    本模块负责的实体类型。**主 agent 落迁移时必须把它一并建出**，
    否则 OP-88 第三半只闭合了一半（判据 1~3 有、反向判据没有）。

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
        BASELINE_VERSION_TRIGGER,
        BASELINE_NODE_TRIGGER,
        CROSS_BRAND_TRIGGER,
        ASSET_SCOPE_TRIGGER,
        APPROVAL_REQUIRED_TRIGGER,
        NO_BLOCKING_TRIGGER,
    )


def consistency_function_names() -> tuple[str, ...]:
    """本模块**自己的**函数名（downgrade 只删这些）。

    **不含** `rednote_reject_mutation` —— 它属于 `platform`，被全项目共用。
    """
    return (
        BASELINE_VERSION_FUNCTION,
        BASELINE_NODE_FUNCTION,
        CROSS_BRAND_FUNCTION,
        ASSET_SCOPE_FUNCTION,
        APPROVAL_REQUIRED_FUNCTION,
        NO_BLOCKING_FUNCTION,
    )


def trigger_names() -> tuple[str, ...]:
    """全部应存在的触发器名（验证脚本与测试用）。"""
    return immutable_trigger_names() + consistency_trigger_names()


__all__ = [
    "APPROVAL_REQUIRED_FUNCTION",
    "APPROVAL_REQUIRED_TRIGGER",
    "ASSET_SCOPE_FUNCTION",
    "ASSET_SCOPE_TRIGGER",
    "BASELINE_NODE_FUNCTION",
    "BASELINE_NODE_TRIGGER",
    "BASELINE_STEP_NO",
    "BASELINE_VERSION_FUNCTION",
    "BASELINE_VERSION_TRIGGER",
    "CONSISTENCY_SQLSTATE",
    "CROSS_BRAND_FUNCTION",
    "CROSS_BRAND_TRIGGER",
    "NO_BLOCKING_FUNCTION",
    "NO_BLOCKING_TRIGGER",
    "all_trigger_ddl",
    "approval_required_ddl",
    "asset_version_scope_ddl",
    "baseline_node_paired_ddl",
    "baseline_version_consistency_ddl",
    "consistency_function_names",
    "consistency_items",
    "consistency_trigger_names",
    "cross_brand_reference_ddl",
    "immutable_trigger_names",
    "no_blocking_conflict_ddl",
    "trigger_names",
]
