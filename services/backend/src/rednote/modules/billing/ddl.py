"""WP25 的触发器 DDL 与**一条部分唯一索引**的规格来源。

**为什么 DDL 生成放在模块里而不是迁移文件里**（与 WP11/WP18/WP20/WP22/WP24
同一理由）：证据脚本要按名字回查 `pg_indexes` / `pg_trigger`。若名字与
`WHERE` 子句由迁移文件手写，证据脚本也得手写一遍同样的字符串，两处一旦
不一致，核对就会**假失败**或**假通过**。

## 本包有**三处**会写出索引规格，因此"唯一来源"必须靠判据而不是靠约定

| 处 | 为什么必须有 | 谁消费 |
|---|---|---|
| `models.py` 的 `__table_args__` | 测试用 `Base.metadata.create_all(tables=[…])` 建表，索引随之创建 | 全部单测的 module 级 fixture |
| 迁移文件 | 生产库靠它建索引 | deploy |
| `ddl.py`（本模块） | 迁移与证据脚本**取值**的地方；`downgrade` 与核对按名字点名 | `collect_wp25_evidence.py` |

前两处**不可能合并**（`models.py` 不能 import 本模块 —— 本模块 import 它的
`IMMUTABLE_TABLES`，会成环）。因此处置是：**规格在本模块定义，
`models.py` 写同值副本，并用 `model_index_specs()` 把 ORM 元数据里的实际值
提取出来，供一条判据与 `PARTIAL_UNIQUE_INDEXES` **逐字段比对**。**

> 只比对名字是不够的（名字相同、`WHERE` 不同是最可能的漂移形态），
> 必须同时比对**键列含顺序**与 **`WHERE` 的规范化文本**。

---

## 一、不可变保护（`pricing_versions` / `usage_ledger`）

复用跨包共用的 `rednote_reject_mutation()` 函数（WP11 建）。
**本包只挂触发器，不重建函数**，且 `downgrade` **不删它** ——
删了会撞依赖错误并让 WP15/WP16/WP17/WP18/WP20/WP22/WP24 的保护一起失效。

* `pricing_versions` 挂（R8）：改一个单价 ⇒ **历史所有预估与账单全部失真**，
  且改的人无感（他以为只影响未来）。
* `usage_ledger` 挂（R5）：ADR-13 原话要求审计"不可被后续修改或删除，
  与**费用账本**、版本快照同级对待"。账本可改 = 整个审计体系可改。
* `cost_estimates` / `project_budgets` / `budget_reservations` **刻意不挂**：
  它们是活的账面，靠 CHECK + 条件 UPDATE + 明细对账（R10）保证正确性。

### ★ 账本不可变 ⇒ 冲正是**追加**，不是改状态

`usage_ledger` **没有 `state` 列**（见 `models.py` docstring §2）。这不是省略，
而是让 R5 的判据**成为可能**：UPDATE / DELETE 各断言被 `23001` 拒绝。
若真有 `state` 列，"确认"必须就地 UPDATE —— 那条判据根本写不出来，
只能退化为"断言状态列存在"这种恒真式（判据纪律第 11 条）。

## 二、部分唯一索引：`uq_usage_ledger_idempotency`

| 索引 | 键 | `WHERE` |
|---|---|---|
| `uq_usage_ledger_idempotency` | `(idempotency_key)` | `idempotency_key IS NOT NULL` |

### 为什么需要它

同一笔用量被记两次（worker 重试、worker 崩溃后重投、人工补录）在
**金额上完全看不出异常** —— 账面多扣一倍，而每一笔分录本身都合法。
唯一能拦的位置是入账前，而"是不是同一笔"只能靠调用方给的幂等键。

### 为什么是**部分**索引而不是普通 `UNIQUE`

普通 `UNIQUE(idempotency_key)` 对多行 NULL 是允许的（PG 认为 `NULL != NULL`），
语义上"能用"，但两处不好：

1. 人工分录（ADJUSTMENT）**没有天然幂等键**，只能留 NULL。
   普通 UNIQUE 让人误以为"全部行都被约束住了"，而实际只覆盖了一部分；
2. 一旦将来有人加了 `NOT NULL DEFAULT gen_random_uuid()` 来"让唯一约束生效"，
   幂等就**彻底失效**且不报错 —— 每行的键都不同，重复入账畅通无阻。

部分索引把"只有非空值受约束"**写进 DDL 文本**，这个意图无法被静默改掉。

### ⚠️ 部分索引的 `WHERE` 写错 = 索引**静默失效**

写 `WHERE idempotency_key IS NULL`、或拼成 `WHERE true`，
PostgreSQL **不会报错** —— 索引照建，只是守错了范围（前者永远命不中，
后者退化成"每行都必须唯一"从而拦住第二条人工分录）。

WP22 已在本仓实测过这个形态（`uq_production_baselines_project_root` 漏写
`WHERE` 会退化成"每项目只能有一条基线"）。因此证据脚本必须做**双向**活性验证：

  1. **反向（证明它确实在按 `WHERE` 过滤）**：造一个**不匹配** `WHERE` 的
     重复行（两条 `idempotency_key IS NULL`），断言它**能插进去** ——
     若这一条被拒，说明索引退化成了全局唯一，那它守错了范围；
  2. **正向（证明约束在生效）**：造一个**匹配** `WHERE` 的重复行，
     断言被拒且 `sqlstate = 23505`。

只做第 2 条会漏掉"`WHERE` 永远命不中"这一整类失效。

## 三、部分唯一索引**不在 `pg_constraint` 里**

核对时必须查 `pg_indexes.indexdef`。只查约束表**完全看不到**这条防线
—— WP22 的迁移 docstring 已登记此坑。

因此 `partial_unique_index_ddl()` 同时供三条路径使用：
迁移（建索引）、测试（建索引 + 活性验证）、证据脚本（按 `indexdef` 核对）。

## 四、`model_index_specs()` 为什么遍历**全部 5 张表**而不是只查 `usage_ledger`

（本仓"覆盖范围由人写"已现身 4 次：ISS-104→111→139→143。）

写死 `usage_ledger` 一张表时，若将来有人在 `cost_estimates` 上加一条部分索引，
**对账判据不会报红** —— 它会安静地漏检，而"漏检"与"没有漂移"同形。
因此本模块从 `OWN_TABLES` 现场取表清单，让人写的清单退化为零。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.billing.models import IMMUTABLE_TABLES
from rednote.platform.immutability import immutable_trigger_ddl

# ---------------------------------------------------------------------------
# SQLSTATE
# ---------------------------------------------------------------------------

# PostgreSQL unique_violation。**与不可变的 23001 刻意区分**：
# 前者是"这笔已经记过了"（并发重试的正常可能性，调用方应当幂等吸收），
# 后者是"这张表根本不许改"（程序缺陷，必须暴露成 500）。
# 证据脚本与测试要能精确分开断言，不能靠匹配错误文案。
UNIQUE_VIOLATION_SQLSTATE: Final[str] = "23505"


# ---------------------------------------------------------------------------
# ★ 本包全部 5 张表（`model_index_specs()` 的覆盖范围来源）
# ---------------------------------------------------------------------------

# **逐字与 `__tablename__` 相同**，且由 `test_billing_wp25.py` 与 ORM 元数据
# 交叉核对（不是靠人记得同步）。
OWN_TABLES: Final[tuple[str, ...]] = (
    "pricing_versions",
    "cost_estimates",
    "project_budgets",
    "budget_reservations",
    "usage_ledger",
)


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
        "uq_usage_ledger_idempotency",
        "usage_ledger",
        ("idempotency_key",),
        "idempotency_key IS NOT NULL",
        "人工分录（ADJUSTMENT）没有天然幂等键只能留 NULL，"
        "而普通 UNIQUE 让多行 NULL 合法却看起来'已覆盖全部行'；"
        "去掉 WHERE 又会退化成'每行都必须唯一'从而拦住第二条人工分录",
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
    重排空白并加外层括号（`WHERE (idempotency_key IS NOT NULL)`），
    而人写的版本没有那层括号。逐字比会**假失败**，而假失败的核对最终会被
    人关掉 —— 那比没有核对更糟（ISS-95：文档声称有防线、实际没人看着）。
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


def _model_tables() -> tuple[type, ...]:
    """从 ORM 元数据现场取本包 5 个模型类。

    **不用人写的清单**：若在别处维护一份"要核对哪些表"的名单，新增表时
    会漏检（判据纪律第 13 条）。这里靠 `Base.metadata` 的表名反查，
    覆盖范围由模型注册决定。
    """
    from rednote.platform.base import Base

    wanted = set(OWN_TABLES)
    found = {
        table.name: table
        for table in Base.metadata.tables.values()
        if table.name in wanted
    }
    missing = wanted - set(found)
    if missing:
        raise RuntimeError(
            f"ORM 元数据里缺这些本包表：{sorted(missing)}"
            "（`models.py` 未被 import，或表名改了而 `OWN_TABLES` 未同步）"
        )
    return tuple(found[name] for name in OWN_TABLES)


def model_index_specs() -> dict[str, dict[str, object]]:
    """从**已构造的 ORM 元数据**里提取本包 5 张表的全部索引实际规格。

    返回 `{索引名: {"columns": (…), "unique": bool, "where": str | None,
    "table": str}}`。

    **为什么要这个函数而不是在测试里手写**：测试若要手写一遍列名与 `WHERE`，
    它就变成了第四处副本 —— 而本函数的全部意义是让核对**不依赖人记得同步**。
    规格从 `__table_args__` 现场读出，改了没同步就立刻可见。
    """
    specs: dict[str, dict[str, object]] = {}
    for table in _model_tables():
        for index in table.indexes:
            where = index.dialect_options.get("postgresql", {}).get("where")
            specs[index.name] = {
                "columns": tuple(col.name for col in index.columns),
                "unique": bool(index.unique),
                "where": None if where is None else str(where),
                "table": table.name,
            }
    return specs


def partial_index_specs() -> dict[str, dict[str, object]]:
    """`model_index_specs()` 里**只保留部分唯一索引**（`unique` + 有 `WHERE`）。

    **为什么必须收窄**：本包 5 张表上还有 9 条**普通**索引
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
      · 表名；
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

    本包 5 张表全部是**本包私有**的（`providers` / `model_profiles` /
    `projects` 上的 FK 是**出向**的，不构成跨包一致性不变量）。
    WP18/WP20/WP22/WP23 的 OP-88 那一半在各自包里闭合 ——
    那批触发器要守的是"注册表（`version_nodes`）+ 领域表必须配对"，
    而本包**没有任何表挂到 `version_nodes` 上**：
    计价版本是运营维护的价目，不是内容产物，没有版本节点。

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

    **2 项**（`pricing_versions` + `usage_ledger` 的不可变保护，由
    `IMMUTABLE_TABLES` 驱动）。另外 3 张表可变，不挂触发器；
    本包没有跨表一致性触发器（见 `consistency_items`）。

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

    **为什么不数个数**：`immutable_trigger_names()` 由 `IMMUTABLE_TABLES`
    驱动，而该元组将来可能因新表加入而变长；数个数会把"别人的触发器"
    算进来或把自己的漏掉。点名核对才能给出"缺失集合为空集"这种可断言的结论。
    """
    return immutable_trigger_names() + consistency_trigger_names()


def immutable_function_names() -> tuple[str, ...]:
    """本模块**建立的函数名**。只有共用那一个 —— 且它是**跨包共用**的，
    `downgrade` 不许删（删了会让其它 8 个包的保护一起失效）。
    """
    return ("rednote_reject_mutation",)


__all__ = [
    "OWN_TABLES",
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
