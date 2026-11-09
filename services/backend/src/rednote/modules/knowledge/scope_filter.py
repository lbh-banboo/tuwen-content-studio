"""范围可见性谓词（**纯函数**，验收原文的落点）。

验收原文：**「近似品牌案例不串事实」**。前半句「近似品牌案例」由
`chunking.py` + embedding 负责；后半句「**不串事实**」由本模块负责。

---

## 一、为什么必须是"下推到 SQL"而不是"取回来再筛"

HNSW 的 `LIMIT k` 是在**索引扫描时**取的（它做的是"无过滤的 k 近邻"）。
若先取 `k` 条再在 Python 里按范围筛，会出现：

    同品牌只有 2 条，k = 8
    → 索引返回 8 条（含 6 条跨品牌的）
    → Python 筛掉 6 条
    → 返回 2 条

看起来"过滤了"，**但结果集合已经错了**：正确做法是"在符合范围的集合里
取 8 条"（可能凑得出 8 条同品牌的），而上面只返回了 2 条。
更糟的是 `limit=8` 时返回 6 条跨品牌的那一版 —— 它**看起来正常**
（有结果、有分数、有排序），只是内容是别人的。

**因此**：范围谓词进 `WHERE`，与部分索引的 `WHERE` 对齐。

⚠️ **一个必须显式承认的取舍**：pgvector 的 HNSW 在 `WHERE` 存在时
可能**退化为精确扫描**（近似搜索要求无过滤的 k 近邻）。
本规模（远未到 ADR-15 的 200 万阈值）下精确扫描完全可接受，
且**正确性没有替代品**。已登记为 OP-167。

---

## 二、谓词的形状：**OR 而非 AND**

```sql
-- 一条知识可见，当且仅当它满足以下任一条：
scope = 'GLOBAL'
OR (scope = 'BRAND'   AND brand_id   = :subject_brand_id)
OR (scope = 'PRODUCT' AND product_id = :subject_product_id)
OR (scope = 'PROJECT' AND project_id = :subject_project_id)
```

**为什么是 OR 不是 AND**：这四个 scope 是**互斥的四种"适用范围"**，
不是"四个必须同时满足的条件"。一条 `BRAND` 范围的知识
与"subject 的 brand_id"只在一个分支上比较。

**为什么每个分支都要重复 `scope = 'X'`**：
因为"限定列相等"**单独不成立**。一条 `scope='PROJECT'` 的知识
若 `brand_id` 恰好等于 subject 的 brand_id，它**不应该**因为
"brand 匹配"而被召回 —— 它是项目私有的，跨项目就该看不见。
只比限定列会让"项目私有知识"因为品牌相同而泄漏到别的项目。
这是**最容易写错的一处**，且写错后症状是"知识比预期多"，
不会报错。

---

## 三、缺字段 → fail-closed（看不见，而不是不过滤）

`ScopeSubject` 的三个字段都可空。**缺字段时该分支恒假**
（`:subject_brand_id IS NULL` → 不匹配任何 `brand_id`），
于是只有 `GLOBAL` 的知识可见。

**为什么不做"缺字段就不过滤该维度"**：那会让"忘记传 `productId`"
变成"看到全部产品的事实" —— 一个**越权**的默认值。
"缺 = 看不见"是安全的默认方向。

**这个选择是可诊断的**：`SearchOutcome.scope_filter_applied` 与
`subject` 会被带回给调用方，于是"为什么没召回"有答案（风险 R5）。

---

## 四、唯一来源

`embedding_service` 的检索路径与（未来的）列表路径都必须调本模块。
**若各写一套映射，"按品牌过滤"在两条路径上会给出不同结果**，
而不会有任何测试发现 —— 因为两条路径各自都自洽。
这与 WP15 的 ISS-47 同族：**同一个事实有两处实现，它们迟早不一致**。

**本模块不 import `models.py`**（避免把 ORM 依赖引入纯函数层）——
它接受**列对象**作为参数，调用方传 `KnowledgeAsset.scope` 等。
这样纯函数可以被单测直接调用（不需要真库）。
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import and_, false, or_
from sqlalchemy.sql.elements import ColumnElement

from rednote.modules.knowledge.embedding_types import ScopeSubject
from rednote.modules.knowledge.types import (
    SCOPE_BRAND,
    SCOPE_GLOBAL,
    SCOPE_PRODUCT,
    SCOPE_PROJECT,
    SCOPE_REQUIRED_COLUMN,
    SCOPES,
)

#: 本模块依赖的三个限定列名（`GLOBAL` 不需要限定列，因此不在其中）。
SCOPE_QUALIFIER_COLUMNS: Final[tuple[str, ...]] = (
    "brand_id",
    "product_id",
    "project_id",
)

#: 范围过滤**永远被应用**（谓词无条件进 `WHERE`）。
#:
#: **为什么不写成"按 subject 判断要不要过滤"的函数**：那会引入一个
#: 恒真的分支 —— `scope_visibility_predicate` 无论如何都会被调用，
#: 因为 `GLOBAL` 的可见性**本身就是过滤条件**（它排除掉所有非 GLOBAL 的）。
#: 写成一个"可能返回 False"的函数，只会制造"存在不过滤的路径"的错觉，
#: 而那条路径不存在。ISS-75 的教训：
#: **判据不能建立在一个恒真的条件上**。
#:
#: 它的用途是进响应 `meta.scopeFilterApplied`，让调用方能区分
#: "库里没有"与"被范围过滤掉了"（风险 R5）。
SCOPE_FILTER_ALWAYS_APPLIED: Final[bool] = True


def scope_visibility_predicate(
    *,
    scope_column: Any,
    brand_column: Any,
    product_column: Any,
    project_column: Any,
    subject: ScopeSubject,
) -> ColumnElement[bool]:
    """构造范围可见性的 SQL 谓词（**唯一来源**）。

    参数是**列对象**（不是模型类），因此本函数保持纯 —— 可被单测
    直接用真列或构造的列调用，不需要 ORM 实例。

    返回的谓词可直接用于 `select(...).where(predicate)`。

    **四分支结构见模块 docstring 第二节**。三个 `None` 分支的处理：
    用 `false()` 而不是"省略该分支" —— 省略会让 `or_()` 少一项，
    行为上等价，但**读起来像"忘了写"**；显式 `false()` 让
    "这个维度没有可见性主张"成为代码里看得见的事实。
    """
    branches: list[ColumnElement[bool]] = [scope_column == SCOPE_GLOBAL]

    pairs = (
        (SCOPE_BRAND, brand_column, subject.brand_id),
        (SCOPE_PRODUCT, product_column, subject.product_id),
        (SCOPE_PROJECT, project_column, subject.project_id),
    )
    for scope_value, column, subject_value in pairs:
        if subject_value is None:
            # 缺该维度 → 该分支恒假（fail-closed，见模块 docstring 第三节）。
            branches.append(false())
            continue
        branches.append(and_(scope_column == scope_value, column == subject_value))

    return or_(*branches)


def scope_filter_is_active(subject: ScopeSubject) -> bool:
    """这次检索是否真的**收窄了**可见范围（进响应 `meta`，供排障）。

    **它恒为 True，且这是有意的** —— 见 `SCOPE_FILTER_ALWAYS_APPLIED` 的说明。
    保留函数形态（而不是直接引常量）是为了让调用点读起来是
    "问了一个问题"，且将来若要引入"管理员可跨范围检索"时，
    改这一处即可，不必改所有调用点。

    `subject` 参数**刻意保留但当前不用**：将来若出现
    "某类 subject 可以看全部"的规则，它会是判断依据。
    现在不读它，是为了避免写一个"看起来在判断、实际恒真"的分支 ——
    ISS-82 的教训：**判据必须能区分两种实现**。
    """
    del subject  # 见 docstring：当前恒真，参数为将来预留
    return SCOPE_FILTER_ALWAYS_APPLIED


def scope_coverage_note(subject: ScopeSubject) -> str:
    """给调用方的一句人话说明（进响应 meta，供排障）。

    **为什么不返回结构体**：它只用于日志与响应里的 `note` 字段，
    调用方不会拿它做判断。返回结构体会诱导调用方"解析它"，
    而"解析一句人话"是脆弱耦合。
    """
    if subject.is_empty():
        return (
            "未提供范围主体：仅 GLOBAL 范围的知识可见"
            "（BRAND/PRODUCT/PROJECT 范围的知识全部被过滤）。"
        )
    parts = []
    if subject.brand_id is not None:
        parts.append("BRAND")
    if subject.product_id is not None:
        parts.append("PRODUCT")
    if subject.project_id is not None:
        parts.append("PROJECT")
    return f"可见范围：GLOBAL + {' / '.join(parts)}。"


def assert_scope_values_covered() -> None:
    """自检：`SCOPES` 里的每一个取值都被谓词覆盖到，且限定列名一致。

    **为什么需要它**：`scope_visibility_predicate` 里硬编码了
    `SCOPE_GLOBAL` / `SCOPE_BRAND` / `SCOPE_PRODUCT` / `SCOPE_PROJECT`
    四个分支。若 WP20 将来新增一个 scope（例如 `ORG`），
    本函数会**静默漏掉它** —— 新 scope 的知识**永远召回不到**，
    而不会有任何错误。这是一个"新增取值必须显式回答"的守卫
    （与 WP20 的 `CATEGORIES_REQUIRING_CONCRETE_SCOPE` 同一手法）。

    第二个检查：谓词里用的限定列名必须与 WP20 的
    `SCOPE_REQUIRED_COLUMN` 一致。两处不一致的后果是
    "按品牌过滤"在检索路径与校验路径上针对**不同的列** ——
    而两边各自都自洽，不会有测试发现。

    由测试与证据脚本调用；**不在生产路径上调用**（它是断言，
    不是业务逻辑 —— 放在生产路径上会让每次检索多一次集合运算）。
    """
    covered = {SCOPE_GLOBAL, SCOPE_BRAND, SCOPE_PRODUCT, SCOPE_PROJECT}
    missing = sorted(set(SCOPES) - covered)
    if missing:
        raise AssertionError(
            f"scope_visibility_predicate 未覆盖这些 scope 取值：{missing}。"
            "新增 scope 时必须显式决定它的可见性语义 —— "
            "漏掉它的后果是「该 scope 的知识永远召回不到」且不报错。"
        )

    # WP20 的权威映射：scope → 需要的限定列（`GLOBAL` 是空串，不需要列）。
    expected = {
        SCOPE_BRAND: "brand_id",
        SCOPE_PRODUCT: "product_id",
        SCOPE_PROJECT: "project_id",
    }
    # **先与模块常量对齐**：`SCOPE_QUALIFIER_COLUMNS` 与本函数的
    # `expected` 描述的是同一个事实（"哪些列是限定列"）。若只校验
    # `expected`，这个常量就变成一个**没有任何消费者的声明** ——
    # 它写错了不会有任何东西发现（ISS-78 同族：文档/常量是唯一
    # 声称方时，没有机械判据与它对账）。先让两处互相钉住。
    if set(SCOPE_QUALIFIER_COLUMNS) != set(expected.values()):
        raise AssertionError(
            f"SCOPE_QUALIFIER_COLUMNS={SCOPE_QUALIFIER_COLUMNS} 与本函数"
            f"用到的限定列 {sorted(expected.values())} 不一致。"
            "两处描述同一个事实，不一致会让「哪些列是限定列」有第二个答案。"
        )
    for scope_value, column_name in expected.items():
        actual = SCOPE_REQUIRED_COLUMN.get(scope_value, "")
        if actual != column_name:
            raise AssertionError(
                f"scope={scope_value} 的限定列在 WP20 的 SCOPE_REQUIRED_COLUMN "
                f"里是 {actual!r}，而本模块用的是 {column_name!r}。"
                "两处不一致会让检索路径与校验路径针对不同的列过滤。"
            )
    if SCOPE_REQUIRED_COLUMN.get(SCOPE_GLOBAL, "") != "":
        raise AssertionError(
            f"SCOPE_REQUIRED_COLUMN[{SCOPE_GLOBAL!r}] 应为空串（GLOBAL 不带限定列），"
            f"实际是 {SCOPE_REQUIRED_COLUMN.get(SCOPE_GLOBAL)!r}。"
        )


__all__ = [
    "SCOPE_FILTER_ALWAYS_APPLIED",
    "SCOPE_QUALIFIER_COLUMNS",
    "assert_scope_values_covered",
    "scope_coverage_note",
    "scope_filter_is_active",
    "scope_visibility_predicate",
]
