"""WP24 的触发器 DDL 与**两条部分唯一索引**的规格来源。

**为什么 DDL 生成放在模块里而不是迁移文件里**（与 WP11/WP18/WP20/WP22/WP23 同一理由）：
证据脚本要按名字回查 `pg_indexes` / `pg_trigger`。若名字与 `WHERE` 子句由迁移
文件手写，证据脚本也得手写一遍同样的字符串，两处一旦不一致，核对就会
**假失败**或**假通过**。

## 本包有**三处**会写出索引规格，因此"唯一来源"必须靠判据而不是靠约定

| 处 | 为什么必须有 | 谁消费 |
|---|---|---|
| `models.py` 的 `__table_args__` | 测试用 `Base.metadata.create_all(tables=[…])` 建表，索引随之创建 | 全部单测的 module 级 fixture |
| 迁移文件 | 生产库靠它建索引 | deploy |
| `ddl.py`（本模块） | 迁移与证据脚本**取值**的地方；`downgrade` 与核对按名字点名 | `collect_wp24_evidence.py` |

前两处**不可能合并**（`models.py` 不能 import 本模块 —— 本模块 import 它的
`IMMUTABLE_TABLES`，会成环）。因此处置是：**规格在本模块定义，
`models.py` 写同值副本，并用 `model_index_specs()` 把 ORM 元数据里的实际值
提取出来，供一条判据与 `PARTIAL_UNIQUE_INDEXES` **逐字段比对**。**

> 只比对名字是不够的（名字相同、`WHERE` 不同是最可能的漂移形态），
> 必须同时比对**键列含顺序**与 **`WHERE` 的规范化文本**。
> 这正是本项目"两处各写一遍，某一处改了另一处不改且不报错"那条坑的处置。

---

## 一、不可变保护（`runtime_settings`）

复用跨包共用的 `rednote_reject_mutation()` 函数（WP11 建）。
**本包只挂触发器，不重建函数**，且 `downgrade` **不删它** ——
删了会撞依赖错误并让 WP15/WP16/WP17/WP18/WP20/WP22/WP23 的保护一起失效。

`plan_drafts` **刻意不挂**：它是工作区，运营反复调整（见 `models.py` docstring）。

## 二、★ 两条部分唯一索引 —— 本包最容易"看起来做了、其实没做"的地方

| 索引 | 键 | `WHERE` |
|---|---|---|
| `uq_runtime_settings_global` | `(key, revision_no)` | `scope_kind = 'GLOBAL'` |
| `uq_runtime_settings_project` | `(key, project_id, revision_no)` | `scope_kind = 'PROJECT'` |

### 为什么不能靠普通 `UNIQUE`

`UNIQUE(key, scope_kind, project_id, revision_no)` 对**全局行完全不生效** ——
`project_id IS NULL`，而 SQL 的三值逻辑认为 `NULL != NULL`，于是同一个
`(key, 'GLOBAL', NULL, 1)` 可以**无限重复插入**，**不报任何错**。

重复插入的后果不是"多几行垃圾"，而是**"当前生效值"这个概念的检索变成不确定的**：

    SELECT value FROM runtime_settings
     WHERE key = $1 AND scope_kind = 'GLOBAL'
     ORDER BY revision_no DESC LIMIT 1

`revision_no` 相同的两行里返回哪一行**没有保证**。于是 API 双实例
（ADR-11 C8）可能读到**不同的"当前上限"**，而两者都不会报错 ——
正是 R2/R3 那类"两个数字都对得上合计、但内容不同"的故障形态。

### 为什么用部分索引而不是 `COALESCE(project_id, sentinel)`

哨兵值（把 NULL 换成一个固定的"全局哨兵 UUID"）能让普通唯一约束生效，
但代价是：
  · 哨兵值**污染数据**（每行都存一个假的 project_id，"全局"变成"属于哨兵项目"）；
  · "哨兵是那一个 UUID"这个约定**没有任何约束保证** —— 有人写别的值就静默失效。

部分索引把 `project_id` 从**全局行的键里去掉**，问题从根上不存在。

### ⚠️ 部分索引的 `WHERE` 写错 = 索引**静默失效**

写 `WHERE scope_kind = 'PROJECT'` 到全局索引上、或拼成 `'GLOBALX'`，
PostgreSQL **不会报错** —— 索引照建，只是永远不命中任何行，于是约束不存在。

WP22 已在本仓实测过这个形态（`uq_production_baselines_project_root` 漏写
`WHERE` 会退化成"每项目只能有一条基线"）。因此证据脚本必须做**双向**活性验证：

  1. **反向（证明它确实在按 `WHERE` 过滤）**：造一个**不匹配** `WHERE` 的
     重复行，断言它**能插进去** —— 若这一条被拒，说明索引退化成了全局唯一，
     那它守错了范围；
  2. **正向（证明约束在生效）**：造一个**匹配** `WHERE` 的重复行，
     断言被拒且 `sqlstate = 23505`。

只做第 2 条会漏掉"`WHERE` 永远命不中"这一整类失效。

## 三、部分唯一索引**不在 `pg_constraint` 里**

核对时必须查 `pg_indexes.indexdef`（或 `pg_indexes` 的 `indexdef` 列），
只查约束表**完全看不到**这两条防线 —— WP22 的迁移 docstring 已登记此坑。

因此 `partial_unique_index_ddl()` 同时供三条路径使用：
迁移（建索引）、测试（建索引 + 活性验证）、证据脚本（按 `indexdef` 核对）。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.planning.models import IMMUTABLE_TABLES
from rednote.platform.immutability import immutable_trigger_ddl

# ---------------------------------------------------------------------------
# SQLSTATE
# ---------------------------------------------------------------------------

# PostgreSQL unique_violation。**与不可变的 23001 刻意区分**：
# 前者是"这个序号已被占用"（并发写入的正常可能性），后者是"这张表根本不许改"
# （程序缺陷）。证据脚本与测试要能精确分开断言，不能靠匹配错误文案。
UNIQUE_VIOLATION_SQLSTATE: Final[str] = "23505"

# 本包没有新的跨表一致性触发器（两张表都不与别的模块共享表结构），
# 因此不需要 CONSISTENCY_SQLSTATE。**刻意不定义** —— 定义一个用不到的
# 常量会让"本包有哪些防线"这件事多一个假线索。


# ---------------------------------------------------------------------------
# 部分唯一索引：名字 + 键 + WHERE 的**单一来源**
# ---------------------------------------------------------------------------

# 结构：`(索引名, 表名, 键列表, WHERE 子句, 为什么必须带这个 WHERE)`
#
# 元组的第四项是**逐字**用于 DDL 的，第五项只给人读，不进 SQL。
PARTIAL_UNIQUE_INDEXES: Final[
    tuple[tuple[str, str, tuple[str, ...], str, str], ...]
] = (
    (
        "uq_runtime_settings_global",
        "runtime_settings",
        ("key", "revision_no"),
        "scope_kind = 'GLOBAL'",
        "全局行（project_id IS NULL）不受普通唯一约束管辖，"
        "没有这条就能无限重复插同一 (key, revision_no)",
    ),
    (
        "uq_runtime_settings_project",
        "runtime_settings",
        ("key", "project_id", "revision_no"),
        "scope_kind = 'PROJECT'",
        "项目级副本与覆盖共用一张表，"
        "没有这条则同一项目的同一 key 可被并发写进两个相同的 revision_no",
    ),
)


def partial_unique_index_ddl() -> list[tuple[str, str]]:
    """返回 `[(索引名, CREATE UNIQUE INDEX 语句), …]`。

    迁移用 `op.execute()` 执行（**不用 `op.create_index`** —— 那会重复一遍
    名字与 `WHERE`，而本模块存在的全部理由就是消除这次重复）。

    证据脚本用同一函数的返回值构造期望的 `WHERE` 片段，与
    `pg_indexes.indexdef` 逐字比对。
    """
    return [
        (
            name,
            f"CREATE UNIQUE INDEX {name} ON {table} ({', '.join(columns)})"
            f" WHERE {where}",
        )
        for name, table, columns, where, _why in PARTIAL_UNIQUE_INDEXES
    ]


def partial_unique_index_names() -> tuple[str, ...]:
    """本模块建的部分唯一索引名（测试与证据脚本按名字点名核对）。"""
    return tuple(item[0] for item in PARTIAL_UNIQUE_INDEXES)


def partial_unique_index_where() -> dict[str, str]:
    """索引名 → `WHERE` 子句。**核对时必须比对这一项**，不能只比名字。"""
    return {item[0]: item[3] for item in PARTIAL_UNIQUE_INDEXES}


def partial_unique_index_columns() -> dict[str, tuple[str, ...]]:
    """索引名 → 键列（含顺序）。顺序影响 `indexdef` 的文本形态。"""
    return {item[0]: item[2] for item in PARTIAL_UNIQUE_INDEXES}


# ---------------------------------------------------------------------------
# ★ 与 ORM 元数据的**逐字段对账**（`models.py` 是三处之一，见模块 docstring）
# ---------------------------------------------------------------------------

_LOCAL_WHITESPACE: Final[tuple[str, ...]] = ("  ", "\n", "\t")


def _normalize_sql(sql: str) -> str:
    """把 SQL 片段规范化到"比较用"的形态。

    **为什么必须规范化而不能逐字比**：PostgreSQL 回写的 `indexdef` 会
    重排空白并加外层括号（`WHERE (scope_kind = 'GLOBAL')`），而人写的
    版本没有那层括号。逐字比会**假失败**，而假失败的核对最终会被人关掉 ——
    那比没有核对更糟（ISS-95：文档声称有防线、实际没人看着）。
    """
    text = sql
    for token in _LOCAL_WHITESPACE:
        text = text.replace(token, " ")
    text = " ".join(text.split())
    # 剥掉 PostgreSQL 为 WHERE 表达式加的外层括号（可能有多层）。
    while text.startswith("(") and text.endswith(")") and _balanced(text[1:-1]):
        text = text[1:-1].strip()
    return text


def _balanced(text: str) -> bool:
    """判断括号是否配平（避免把 `(a) AND (b)` 误剥成 `a) AND (b`）。"""
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def model_index_specs() -> dict[str, dict[str, object]]:
    """从**已构造的 ORM 元数据**里提取 `runtime_settings` 的全部索引实际规格。

    返回 `{索引名: {"columns": (…), "unique": bool, "where": str | None, "table": str}}`。

    **为什么要这个函数而不是在测试里手写**：测试若要手写一遍列名与 `WHERE`，
    它就变成了第四处副本 —— 而本函数的全部意义是让核对**不依赖人记得同步**。
    规格从 `RuntimeSetting.__table_args__` 现场读出，改了没同步就立刻可见。
    """
    from rednote.modules.planning.models import RuntimeSetting

    specs: dict[str, dict[str, object]] = {}
    for index in RuntimeSetting.__table__.indexes:
        where = index.dialect_options.get("postgresql", {}).get("where")
        specs[index.name] = {
            "columns": tuple(col.name for col in index.columns),
            "unique": bool(index.unique),
            "where": None if where is None else str(where),
            "table": RuntimeSetting.__tablename__,
        }
    return specs


def partial_index_specs() -> dict[str, dict[str, object]]:
    """`model_index_specs()` 里**只保留部分唯一索引**（`unique` + 有 `WHERE`）。

    **为什么必须收窄**：`runtime_settings` 上还有 4 条**普通**索引
    （查询路径用，`models.py` 单方面持有，不在本模块的规格里）。
    不收窄的话它们会被报成"多余"，而**永远为真的告警等于没有告警** ——
    真漂移会被淹没在噪音里。
    """
    return {
        name: spec
        for name, spec in model_index_specs().items()
        if spec["unique"] is True and spec["where"] is not None
    }


def index_spec_drift() -> list[str]:
    """对账 `PARTIAL_UNIQUE_INDEXES`（规格）与 `models.py`（实现）。

    返回**漂移描述列表**：空列表 = 完全一致。测试与证据脚本都断言它为空。

    逐字段核对：
      · 名字（集合相等，双向 —— 少了说明没建或退化了，多了说明建了规格外的）；
      · 键列**含顺序**（顺序不同会让 `indexdef` 不同，且影响索引可用性）；
      · `unique` 必须为 True（写成普通索引 = 约束不存在）；
      · `WHERE` 规范化后必须相等（**这是最会漂移的一项**）。

    **"实现丢了 `WHERE`"为什么会出现在"规格里有、实现里没有"这一条**：
    丢了 `WHERE` 的索引不再是部分索引 ⇒ 不在 `partial_index_specs()` 里 ⇒
    被本函数按缺失报出。这是刻意的归并 —— 那种退化的后果就是
    "索引范围变错"，与"索引不存在"同一处置（都必须 FAIL）。
    """
    specs = partial_index_specs()
    expected = {item[0]: item for item in PARTIAL_UNIQUE_INDEXES}
    drift: list[str] = []

    for missing in sorted(set(expected) - set(specs)):
        detail = "规格里有，models.py 里没有"
        all_specs = model_index_specs()
        if missing in all_specs:
            detail += (
                "（它存在但不是部分唯一索引："
                f"unique={all_specs[missing]['unique']}，"
                f"where={all_specs[missing]['where']!r}）"
            )
        drift.append(f"{missing}：{detail}")
    for extra in sorted(set(specs) - set(expected)):
        drift.append(f"{extra}：models.py 里有，规格里没有")

    for name in sorted(set(expected) & set(specs)):
        _n, table, columns, where, _why = expected[name]
        actual = specs[name]
        if actual["table"] != table:
            drift.append(f"{name}：表不符（规格 {table} vs 实现 {actual['table']}）")
        if tuple(actual["columns"]) != tuple(columns):
            drift.append(
                f"{name}：键列不符（规格 {columns} vs 实现 {actual['columns']}）"
            )
        if _normalize_sql(str(actual["where"])) != _normalize_sql(where):
            drift.append(
                f"{name}：WHERE 不符（规格 {where!r} vs 实现 {actual['where']!r}）"
            )
    return drift


# ---------------------------------------------------------------------------
# 触发器
# ---------------------------------------------------------------------------


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块的**跨表一致性**触发器。

    **本包为空**（返回空列表），且这是**有意的显式形状**而不是"忘了写"：

    `runtime_settings` 与 `plan_drafts` 都不与别的模块共享表结构 ——
    本包没有"注册表 + 领域表必须一致"这类跨表不变量（WP18/WP20/WP22/WP23
    的 OP-88 那一半在各自包里闭合，`PLAN` 的领域表就是 `plan_drafts`，
    而它**不参与** `version_nodes` 的配对：草稿是工作区，不是版本）。

    **保留这个函数**（即便返回空）的理由有两条：
      1. `all_trigger_ddl()` 的结构与其它模块一致，读代码的人不必判断
         "为什么这个模块少了一层"；
      2. `downgrade` 要用它区分"本模块建的一致性对象"（空集）与
         "不可变保护挂的共用函数"（不许删）—— 空集让"删本模块的东西"
         退化为"什么都不删"，这恰好是**正确**的行为。
    """
    return []


def all_trigger_ddl() -> list[tuple[str, str, str, str, str]]:
    """本模块需要建立的全部触发器：`[(表名, 函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP), …]`。

    **只有 1 项**（`runtime_settings` 的不可变保护）。`plan_drafts` 可变，
    不挂触发器；本包没有跨表一致性触发器（见 `consistency_items`）。

    **顺序有意义**：函数 DDL 用 `CREATE OR REPLACE`，多张表共用同一个函数时
    重复执行是安全的（幂等）；触发器 DROP 必须先于函数 DROP，
    否则函数仍被触发器引用，PG 会拒绝删除。
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
    """本模块的跨表一致性触发器名。本包为空集（见 `consistency_items`）。"""
    return tuple()


def trigger_names() -> tuple[str, ...]:
    """全部应存在的触发器名（测试与证据脚本按名字点名核对，**不数个数**）。

    **为什么不数个数**：`version_nodes` 上同时挂着 WP18/WP20/WP22/WP23
    四个 `*_paired` 触发器，`runtime_settings` 若将来也被别的包挂触发器，
    数个数就会把别人的算进来或把自己的漏掉。点名核对才能给出
    "缺失集合为空集"这种可断言的结论。
    """
    return immutable_trigger_names() + consistency_trigger_names()


def immutable_function_names() -> tuple[str, ...]:
    """本模块**建立的函数名**。只有共用那一个 —— 且它是**跨包共用**的，
    `downgrade` 不许删（删了会让其它 6 个包的保护一起失效）。
    """
    return ("rednote_reject_mutation",)


__all__ = [
    "PARTIAL_UNIQUE_INDEXES",
    "UNIQUE_VIOLATION_SQLSTATE",
    "all_trigger_ddl",
    "consistency_items",
    "consistency_trigger_names",
    "immutable_function_names",
    "immutable_trigger_names",
    "index_spec_drift",
    "model_index_specs",
    "partial_index_specs",
    "partial_unique_index_columns",
    "partial_unique_index_ddl",
    "partial_unique_index_names",
    "partial_unique_index_where",
    "trigger_names",
]
