"""单价解析与费用计算（WP25）—— 纯函数层。

本模块回答一个问题：**给定一份计价版本与一组用量，这次要花多少钱。**

## 为什么单独一层（而不是塞进 service）

与 :mod:`rednote.modules.billing.money` 同一理由：这里的逻辑
是**可穷举验证的**，而"可穷举"是有价值的性质 —— 把它混进要碰数据库的
service，验证就退化成抽样（WP24 §1.3 对 `allocation.py` 的同一判断）。

## 三条必须显式失败的路径

本模块最要紧的不是"能算出来"，而是**算不出来时必须报错**：

| 情形 | 若静默放过 | 本模块的行为 |
|---|---|---|
| `metric` 不在 :data:`~rednote.modules.billing.types.METRIC_UNITS` 里 | 该条用量**不计费** ⇒ 预估静默少算，账面看起来完全正常 | 抛 :class:`PricingError` |
| 计价版本缺少该 `unit` 的单价 | 按 0 算 ⇒ **整批免费** | 抛 :class:`PricingError` |
| 数量已给定但没有任何用量 | 得出 0 元 ⇒ 预占 0 元，预占形同不存在 | 抛 :class:`PricingError` |

第三条尤其要紧：**0 元的预占会通过所有并发判据**（`0 + 0 + 0 <= limit` 恒真），
于是"预算并发预占测试"会全绿，而系统根本没在管钱。

## 多币种：**不做换算**

同一份预估里混入两种币种的计价版本会抛错（:class:`PricingError`）。
理由：自动换算需要外部汇率源 ⇒ 新的出网通道，而 `COST_LEDGER`
在 `FORBIDDEN_EGRESS_DATA_CLASSES` 里（ADR-10 E-5）。
跨币种汇总属 WP43 报表，届时口径由它定。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Final

from rednote.modules.billing import money
from rednote.modules.billing.types import (
    CURRENCIES,
    METRIC_UNITS,
    PRICING_UNITS,
    PRICING_UNIT_CALL,
    PricingLine,
    UnitPrice,
    UsageItem,
)


class PricingError(ValueError):
    """计价路径上的输入无法得到一个确定的金额。

    继承 `ValueError` 而非 `ApiError`：本模块是纯函数层，不认识 HTTP。
    它是 :class:`money.MoneyError` 的**姊妹**，刻意不互相继承 ——
    "金额形态非法"与"这笔账没法算"是两类不同的失败，
    调用方可能想对前者做 400、对后者做 422。
    """


#: 一份计价版本里必须提供的单位。**`CALL` 不在其中** ——
#: 一个只做文本生成的供应商不会有"按次"计价，强制要求它会逼运营填一个
#: 永远不会用的假单价（而假单价一旦被某条路径读到，就是**凭空收费**）。
REQUIRED_UNITS: Final[tuple[str, ...]] = (
    "TOKEN",
    "IMAGE",
    "SECOND",
)


def _unit_of(metric: str) -> str:
    """供应商计量名 → 本包计量单位。**没有映射即报错**。

    报错而不跳过：见模块 docstring 的三条路径。
    """
    unit = METRIC_UNITS.get(metric)
    if unit is None:
        known = ", ".join(sorted(METRIC_UNITS))
        raise PricingError(
            f"未知的用量名 {metric!r}（无法确定按什么计价）。"
            f"已登记的用量名：{known}。"
            "若是新供应商带来的新指标，请在 types.METRIC_UNITS 里显式登记 —— "
            "跳过它会让预估静默少算"
        )
    return unit


def build_price_map(lines: tuple[PricingLine, ...] | list[PricingLine]) -> dict[str, Decimal]:
    """把计价版本的行转成 `unit → unit_price`。

    重复的 `unit` 抛错而不是"后者覆盖前者"：同一版本里两个单价
    意味着录入错误，而"取最后一个"会让**金额取决于行的顺序** ——
    一个随数据库返回顺序变化的金额是最难查的一类缺陷。
    """
    prices: dict[str, Decimal] = {}
    for line in lines:
        if line.unit not in PRICING_UNITS:
            raise PricingError(f"计价行含未知计量单位：{line.unit!r}")
        if line.unit in prices:
            raise PricingError(
                f"计价版本里 {line.unit!r} 出现了两次 —— "
                "单价必须唯一，否则金额取决于行的顺序"
            )
        if money.is_negative(line.unit_price):
            raise PricingError(f"单价不得为负：{line.unit}={line.unit_price}")
        prices[line.unit] = money.quantize(line.unit_price)
    return prices


def price_for(prices: dict[str, Decimal], unit: str) -> Decimal:
    """取该单位的单价。**缺失即报错**（不返回 0）。"""
    if unit not in PRICING_UNITS:
        raise PricingError(f"未知计量单位：{unit!r}")
    if unit not in prices:
        raise PricingError(
            f"计价版本缺少 {unit!r} 的单价 —— 按 0 计费会让这批调用免费，"
            "而账面上看不出任何异常"
        )
    return prices[unit]


def compute_usage_amount(
    prices: dict[str, Decimal],
    usage: tuple[UsageItem, ...] | list[UsageItem],
) -> Decimal:
    """按用量算金额（`Σ 数量 × 对应单位单价`）。

    逐项乘加，**每一项都过 :func:`money.mul_amount`**（严格口径）：
    乘积超精度即抛 —— 那是精度口径的设计缺陷，必须当场暴露。
    """
    total = money.ZERO
    for item in usage:
        unit = _unit_of(item.metric)
        unit_price = price_for(prices, unit)
        total = money.add(total, money.mul_amount(item.quantity, unit_price))
    return money.quantize(total)


def compute_amount_for_units(
    prices: dict[str, Decimal],
    quantities: dict[str, Decimal] | None = None,
) -> Decimal:
    """按**计量单位**（不是供应商的 metric 名）算金额。

    预占路径用它：预占发生在调用**之前**，此时还没有供应商返回的
    `usage`，只有"打算跑多少篇 / 多少张"这种本包自己的估计。
    """
    total = money.ZERO
    for unit, quantity in (quantities or {}).items():
        unit_price = price_for(prices, unit)
        total = money.add(total, money.mul_amount(quantity, unit_price))
    return money.quantize(total)


def assert_single_currency(currencies: set[str] | list[str]) -> str:
    """一组计价版本的币种必须唯一。返回那一个币种。

    混币种**不做换算**（见模块 docstring）。空集合抛错 ——
    "没有任何计价版本"与"币种冲突"都是"这笔账算不出来"，
    但如果返回一个默认币种，调用方会以为算出来了。
    """
    unique = set(currencies)
    if not unique:
        raise PricingError("没有任何计价版本 —— 无法确定币种")
    if len(unique) > 1:
        raise PricingError(
            f"同一份预估里出现了多种币种：{sorted(unique)}。"
            "自动汇率换算需要新的出网通道，而 COST_LEDGER 在禁止出网清单里"
            "（ADR-10 E-5）—— 请在预估时选定单一币种"
        )
    return next(iter(unique))


def validate_pricing_lines(
    lines: tuple[PricingLine, ...] | list[PricingLine],
    *,
    unit_prices: tuple[UnitPrice, ...] | None = None,
) -> dict[str, Decimal]:
    """校验一份计价版本的行**足以支撑一次预估**。

    这是"发布计价版本"时的校验入口：一份只填了 `TOKEN` 的版本能算文本
    但算不了图。因此**发布时**要求 :data:`REQUIRED_UNITS` 齐全；
    **引用时**只要求用到的那个单位存在（:func:`price_for`）。

    两处宽严不同是刻意的：
      · 发布是**低频、人工**动作 ⇒ 可以严格，一次说清缺什么；
      · 引用是**高频、自动**路径 ⇒ 太严会让"只要文本"的调用因为
        "没填图片单价"而失败，那是把两件无关的事绑在一起。

    `unit_prices` 是便捷参数（调用方常手上有 :class:`UnitPrice` 列表）。
    """
    source = unit_prices if unit_prices is not None else lines
    pairs: list[PricingLine] = []
    for item in source:
        if isinstance(item, UnitPrice):
            pairs.append(PricingLine(unit=item.unit, unit_price=item.unit_price))
        else:
            pairs.append(item)
    prices = build_price_map(pairs)
    missing = [unit for unit in REQUIRED_UNITS if unit not in prices]
    if missing:
        raise PricingError(
            f"计价版本缺少必需的单位单价：{missing}。"
            "发布一份不完整的计价版本，会让引用它的预估在某些能力上"
            "算不出金额（或被按 0 计费）"
        )
    return prices


def line_from_quantity(
    *, unit: str, quantity: Decimal, unit_price: Decimal
) -> object:
    """构造一条预估行（`数量 × 单价 = 金额`）。金额由本函数算，不由调用方传。

    调用方传金额是**第二个真值来源**：它可能与本模块算出的不一致，
    而那种不一致会静默地写进账面（预占用了 A、预估行写了 B）。
    """
    from rednote.modules.billing.types import EstimateLine

    return EstimateLine(
        unit=unit,
        quantity=money.quantize_quantity(quantity),
        unit_price=money.quantize(unit_price),
        amount=money.mul_amount(quantity, unit_price),
    )


def estimate_for_metrics(
    *,
    prices: dict[str, Decimal],
    metrics: dict[str, Decimal],
) -> tuple[object, ...]:
    """按 `metric → 数量` 给出一组预估行（每行一个 metric）。

    用于"预估"路径：运营说"这一批 50 篇"，模型侧换算成
    `input_tokens` / `output_tokens` / `images` 的估计量（换算口径由
    调用方给 —— 本包不自己数 token，那会是第二真值来源）。
    """
    lines = []
    for metric, quantity in metrics.items():
        unit = _unit_of(metric)
        lines.append(
            line_from_quantity(
                unit=unit,
                quantity=quantity,
                unit_price=price_for(prices, unit),
            )
        )
    return tuple(lines)


def total_of(lines: tuple[object, ...] | list[object]) -> Decimal:
    """预估行金额之和。**空集抛错**（与模块 docstring 第三条路径一致）。"""
    if not lines:
        raise PricingError(
            "没有任何预估行 —— 0 元的预占会通过所有并发判据，"
            "于是'预算并发预占测试'全绿而系统根本没在管钱"
        )
    return money.sum_amounts(getattr(line, "amount") for line in lines)


def call_unit_price(prices: dict[str, Decimal]) -> Decimal | None:
    """按次计价的单价（可选）。`None` = 该版本不支持按次计价。"""
    unit_price = prices.get(PRICING_UNIT_CALL)
    return None if unit_price is None else money.quantize(unit_price)


def pricing_version_fingerprint(
    *,
    version_id: uuid.UUID,
    currency: str,
    prices: dict[str, Decimal],
) -> str:
    """一份计价版本的稳定指纹（`unit=price` 按单位名排序拼接）。

    形态固定为 `v1:<currency>:<unit>=<price>;…`，含版本号前缀，
    将来换算法时新旧指纹不会撞。

    `version_id` 与 `currency` 都参与校验但**不进输出**：指纹的用途是
    "同一版本的两份价目是否一致"，混进 id 会让"改了 id 之外的东西"
    看起来像换了版本。id 由调用方在比对时自己带上。

    ## ⚠️ 当前状态：**原语已备，接线未做**（CR-1 MINOR-1，如实记录）

    设计意图是给 `pricing_versions` 的不可变保护加**第二道独立判据**：
    预估行冻结引用某个版本，若有人绕过不可变保护改了单价，指纹会变 ⇒
    对账应能发现。

    **但本包当前的接线状态是**：`reconciliation.py` 不 import 本模块，
    `cost_estimates` 也没有存指纹的列 —— 也就是说**这条第二道判据
    事实上并不存在**。本函数目前只被 `__all__` 导出，无生产调用点
    （`grep pricing_version_fingerprint` 在 `src/` 只命中定义与 `__all__`）。

    **为什么保留而不是删掉**：它是纯函数、零依赖、边界清楚，接线的
    增量成本很小；且 `test_billing_wp25.py` 已对它钉了判据，
    删除会让"将来有人想接线"重新踩一遍算法形态的坑。

    **为什么必须在 docstring 里写明"未接线"**：本仓已踩过
    ISS-95 —— **"声称自己有防线"比"没有防线"更危险**。若这里只写设计意图，
    读代码的人会以为靠它就能发现单价被绕过修改，从而**不再去接**真正的
    监控。故此处显式标注：**要真正获得这道判据，需要另开工作包把
    指纹持久化进 `cost_estimates` 并在 `reconciliation` 里比对**；
    在那之前，`pricing_versions` 的防线**只有行级不可变触发器**一条。

    判据在 `tests/test_billing_wp25.py::test_pricing_fingerprint_*`：
    它测的是**本函数的行为**（同一个体稳定、不同价目不等、未知币种报错），
    **不声称**"对账会用它" —— 后者目前不成立。
    """
    del version_id  # 刻意不进输出，见 docstring
    if currency not in CURRENCIES:
        raise PricingError(f"未知币种：{currency!r}（取值域 {sorted(CURRENCIES)}）")
    parts = ";".join(
        f"{unit}={money.to_wire(prices[unit])}" for unit in sorted(prices)
    )
    return f"v1:{currency}:{parts}"


__all__ = [
    "REQUIRED_UNITS",
    "PricingError",
    "assert_single_currency",
    "build_price_map",
    "call_unit_price",
    "compute_amount_for_units",
    "compute_usage_amount",
    "estimate_for_metrics",
    "line_from_quantity",
    "price_for",
    "pricing_version_fingerprint",
    "total_of",
    "validate_pricing_lines",
]
