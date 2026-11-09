"""比例（千分位整数）的归一 / 校验 / 停用重算。**纯函数**。

与 `allocation.py` 同一理由做成纯函数：它是**分配函数的输入生产者**，
而输入的正确性（合计恒等于 1000）是分配结果不出错的必要条件。
做成纯函数才能在测试里穷举"任意启停组合 → 归一后的属性"。

---

## 一、为什么"合计必须等于 1000"是硬约束而不是警告

若合计是 990‰，分配函数会算出**少一条**（取整后余数不够补），
而账面完全正常 —— 合计数会等于"total - 1"？**不会**。
`allocation.py` 的补 1 会把剩余份额强行分下去，于是合计**照样等于 total**，
只是分给了**余数最大**的那一类，而不是业务上该拿的那一类。

也就是说：**比例和不对，症状不是"少一条"，而是"某几类悄悄换了位置"**。
这正是本包 R1 的形态，也是原型演示里实际发生过的事（见下）。

## 二、原型的两处数值缺陷（本包实测出的机械证据）

原型 `trial-config.html` 的「场景维度」表：

| 场景 | 比例 |
|---|---|
| 早八通勤急救 | 30% |
| 卸妆后二次清洁 | 25% |
| 健身后油汗清洁 | 20% |
| 学生党性价比替换 | 15% |
| 敏感肌换季维稳 | 6% |
| **合计（页面自称）** | **100%** |

**实测 `30+25+20+15+6 = 96`**。页面在表头写"场景比例合计 100% / 校验通过"，
在另一处又写"启用场景合计 100%" —— 而它自己的数据是 96%。

第二处缺陷更隐蔽：原型明写"按小数余数**由大到小**分配"，
而「敏感肌换季维稳」余数 0.60 **大于**两个并列的 0.50，原型却判它
"额度已被前序占满 ⇒ 0 条"。按 C19 的正确解是 `[3,3,2,1,1]`，
原型给的是 `[3,3,2,2,0]` —— **两者合计都等于 10**。

**处置**：原型只作**交互与列结构**的依据；算法与数值一律以 C14/C19 为准，
证据脚本**自己算**，不与原型数值比对。这两条已登记为说明文档 §1.4 疏漏 #13。

## 三、停用场景后必须重新归一（C19 / 原型"已停用 2 个"）

原型展示"共 7 个候选 · 停用 2 个"，且"启用场景合计 100%"。
若停用只把该行置 0 而**不归一**，合计变 990‰ —— 而**系统不会报错**，
只会让整数分配少分一条（见第一节）。

本模块因此定义两种操作，语义严格区分：

| 操作 | 语义 | 是否重新归一 |
|---|---|---|
| `normalize_permille(items, mode=KEEP_TOTAL)` | **停用/排除**：按启用项的原比例**等比例放大**到合计 1000 | ✅ 是 |
| `renormalize_after_disable(items)` | 同上（语义化别名，供服务层调用） | ✅ 是 |
| `validate_total(items)` | 只**校验**，不改值（用于"人工输入合计 ≠ 1000 时列出偏差"） | ❌ 否 |

**为什么不把"归一"做成自动副作用而在服务层显式调用**：
归一是一个**会改数值**的动作。若把它藏在"读取草稿"路径里，
运营看到的当前值会在**每次读**时被悄悄改写 —— 而他并不知道。
因此它是"保存草稿"的显式副作用（模块 docstring 与 `service.py` 一致）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from rednote.modules.planning.types import (
    MAX_PERMILLE,
    MIN_PERMILLE,
    PERMILLE_TOTAL,
    AllocationItem,
)

# 停用标记：权重 0 即"不进入本批次"（与原型的"0%（不进入本批次）"一致）。
DISABLED_PERMILLE: Final[int] = 0


def validate_permille_value(value: int, *, name: str = "") -> None:
    """单个比例值的取值域校验。非法即抛 `ValueError`。

    **为什么单独一个函数**：这个检查在三个地方需要（DTO 层、服务层、
    纯函数入口），而三处各写一遍 `0 <= v <= 1000` 的话，
    改上界时要改三处 —— 漏一处就会有一处放行非法值（且不报错）。
    """
    if not isinstance(value, int) or isinstance(value, bool):
        # `bool` 是 `int` 的子类，显式排除 —— `True` 会静默变成 1‰。
        raise ValueError(f"比例必须是整数：{name or 'value'} = {value!r}")
    if value < MIN_PERMILLE or value > MAX_PERMILLE:
        raise ValueError(
            f"比例超出取值域（{MIN_PERMILLE}~{MAX_PERMILLE}‰）："
            f"{name or 'value'} = {value}"
        )


def validate_total(items: Sequence[AllocationItem]) -> tuple[int, int]:
    """校验启用项比例合计。

    返回 `(合计, 偏差)`，偏差 = `合计 - 1000`。

    **不抛异常**：这个函数的用途是"告诉运营差多少"，
    而"差多少"是**正常状态**（他正在拖数字）。抛异常会把
    "还没配完"变成"请求失败"，而前端需要的是实时提示。
    真正要拒绝非法合计的是保存路径（它在调用本函数后自行决定）。
    """
    total = sum(item.weight_permille for item in items)
    return total, total - PERMILLE_TOTAL


def normalize_permille(items: Sequence[AllocationItem]) -> list[AllocationItem]:
    """把**启用项**的比例等比例归一，使合计**严格等于 1000**。

    返回新的 `AllocationItem` 元组（frozen，不原地改）。

    ## 算法与两处必须写清的取舍

    1. **等比例放大用的是"剩余份额法"，不是简单取整**：
       先把每一项按 `w × 1000 / sum(w)` 取整，再把不足的份额按
       "余数降序 → sort_order 升序 → key 字典序"逐条 +1。
       **与 `allocation.py` 用同一套三级次序** —— 若两处次序不同，
       "归一后的比例"与"归一后的分配"会对不上，而两者都"看起来对"。

    2. **停用项（0‰）不参与，且保持 0**：它不进入本批次。
       若把 0‰ 也按比例放大，0 仍然是 0（`0 × k = 0`），
       因此这一条是**自然成立**的 —— 但它必须被断言，
       因为实现里若把"0‰ 当作 1‰ 的占位"（想避免除零）就会破掉它。

    3. **`sum(items) == 0` 时全部保持 0，不报错**：
       "所有场景都停用"是一个合法状态（运营可以先把提案清空）。
       此时合计 0 ≠ 1000，`validate_total` 会报偏差 ——
       而**归一不负责把 0 变成 1000**（那需要凭空发明比例，是内容策略）。

    4. **已经合计 1000 时返回原值（不重算）**：
       重算会因取整把 `[333,333,334]` 变成别的组合（虽然和仍是 1000）。
       "保存时不改变运营看到的值"是一条该被保住的性质。
    """
    if not items:
        return []

    total, deviation = validate_total(items)
    if deviation == 0:
        return list(items)
    if total == 0:
        return list(items)

    active = [item for item in items if item.weight_permille > 0]
    if not active:
        return list(items)

    scaled: dict[str, int] = {}
    remainders: dict[str, int] = {}
    for item in active:
        numerator = item.weight_permille * PERMILLE_TOTAL
        scaled[item.key] = numerator // total
        remainders[item.key] = numerator % total

    leftover = PERMILLE_TOTAL - sum(scaled.values())
    if leftover > 0:
        order = sorted(
            active,
            key=lambda it: (-remainders[it.key], it.sort_order, it.key),
        )
        for item in order:
            if leftover == 0:
                break
            scaled[item.key] += 1
            leftover -= 1

    result: list[AllocationItem] = []
    for item in items:
        if item.weight_permille <= 0:
            result.append(item)
            continue
        # `dataclasses.replace` 不在 `types.AllocationItem` 的导入里，
        # 这里用显式构造而不是 replace：`AllocationItem` 是 frozen 的，
        # 显式构造让"哪些字段被改了"在 diff 里一眼可见。
        result.append(
            AllocationItem(
                key=item.key,
                weight_permille=scaled[item.key],
                capacity=item.capacity,
                sort_order=item.sort_order,
                name=item.name,
            )
        )
    return result


def renormalize_after_disable(
    items: Sequence[AllocationItem],
) -> list[AllocationItem]:
    """停用/排除若干条目后，把启用项比例重新归一到 1000。

    语义化别名 —— 存在的理由是**调用点的可读性**：
    `service.py` 里写 `renormalize_after_disable(items)` 比
    `normalize_permille(items)` 更能表达"这一步是因为有人停用了东西"。
    两者实现完全相同（都委托 `normalize_permille`），
    **不复制算法** —— 复制就会有两处需要同步。
    """
    return normalize_permille(items)


def apply_draft_permille(
    items: Sequence[AllocationItem],
    overrides: dict[str, int],
) -> list[AllocationItem]:
    """把运营填的"运营比例"覆盖到建议比例上。**不改顺序、不改容量**。

    `overrides` 的键必须都在 `items` 里；多余的键抛 `ValueError`。
    **为什么必须拒绝多余的键**：多出来的键说明前端与后端的条目集合已经漂移
    （例如前端还留着一个已被删除的场景），静默忽略会让"我明明改了它"没有效果
    且**没有任何提示**。
    """
    known = {item.key for item in items}
    unknown = set(overrides) - known
    if unknown:
        raise ValueError(f"覆盖了不存在的条目：{sorted(unknown)}")

    result: list[AllocationItem] = []
    for item in items:
        if item.key not in overrides:
            result.append(item)
            continue
        value = overrides[item.key]
        validate_permille_value(value, name=item.key)
        result.append(
            AllocationItem(
                key=item.key,
                weight_permille=value,
                capacity=item.capacity,
                sort_order=item.sort_order,
                name=item.name,
            )
        )
    return result


__all__ = [
    "DISABLED_PERMILLE",
    "apply_draft_permille",
    "normalize_permille",
    "renormalize_after_disable",
    "validate_permille_value",
    "validate_total",
]
