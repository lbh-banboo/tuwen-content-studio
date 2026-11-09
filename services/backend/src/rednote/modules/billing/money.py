"""金额纯函数层（WP25）—— **本仓唯一的金额运算入口**。

## 为什么单独一个模块（R2 的处置）

AC-26 的三类核对（估算 / 账单 / 重试）本质都是**同一个数在不同时点的比较**：
预占是 `committed + reserved + delta <= limit`、结算是加减、对账是求和再比较。
三处都要求"同一个数只有一种写法"。

全仓此刻**零 `Decimal`**（本包勘察实测：`services/backend/src/rednote/` 下
`grep -rn "Decimal\\|Numeric" --include="*.py"` 零命中），协议只给了
`gateway/types.py` 的 `cost_amount: str | None`。也就是说**精度口径是本包从零定义的**。

若让 `float` 混进来，`0.1 + 0.2 != 0.3` 这类问题会**散落在三处**，且
**偏差在对账时才显形** —— 那时已无法回溯是哪一次丢的。因此：

| 层 | 表示 |
|---|---|
| 存储 | `Numeric(20,8)` / `Numeric(30,10)`（PostgreSQL 任意精度**定点**） |
| Python | 一律 `Decimal`，**签名里不许出现 `float`** |
| 传输 | 规范化**字符串**（`to_wire` / `parse_wire`） |

`float` 在三个地方被主动拒绝（不是"碰巧没写"）：
:func:`as_amount` / :func:`as_quantity` / :func:`parse_wire_optional`
—— 传 `float` 直接抛 :class:`MoneyError`。这是**唯一的入口**，
因此只要这里拦住，全仓就不可能有浮点金额。

## 为什么 8 位小数

token 级计价天然落在 1e-6 量级（"每千 token 0.12 元" ⇒ 每 token `1.2e-4`，
反向"每元多少 token"更小）。8 位留了四个数量级的余量，同时
`Numeric(20,8)` 的整数部分仍有 12 位 —— 足够表达"这一批 50 篇的总花费"。

## ★ 量化只有一个函数，序列化只有一个函数（R7）

同一个 `Decimal` 若有两种写法（`Decimal("100")` 与 `Decimal("100.00000000"`），
字符串比较时不等、`Decimal` 比较时相等 —— **对账会永远报差异**。
因此：

* 落库前必经 :func:`quantize`（或 :func:`normalize_for_storage`）；
* 出 JSON 必经 :func:`to_wire`。

两者都**幂等**（`quantize(quantize(x)) == quantize(x)`，
`to_wire(to_decimal(to_wire(x))) == to_wire(x)`）—— 这是可穷举验证的性质。

## 舍入方向：`ROUND_HALF_UP`，且**超精度不静默**

外边界的解析（:func:`parse_wire_optional`）**容忍**超精度并按 `ROUND_HALF_UP`
量化到 8 位；若需要"有损即报错"的严格口径，用 :func:`parse_wire_strict`。

**为什么外边界默认容忍**：`cost_amount` 来自供应商。一次调用因为
"小数位多了两位"而整体失败，比"记下金额并把有损这件事显式暴露出来"更糟 ——
后者可对账，前者让这笔用量连痕迹都没有（而钱已经花了）。

**为什么内部计算不容忍**：本包自己算出来的金额若超精度，那是**程序缺陷**，
必须当场暴露。因此 :func:`mul_amount` / :func:`normalize_for_storage`
走严格口径，且 :func:`mul_amount` 在乘积有损时抛
:class:`MoneyPrecisionError` —— 宁可在写库前失败，也不要让账面悄悄差一分钱。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

# ---------------------------------------------------------------------------
# 精度口径（与 models.py 的 Numeric 参数**必须逐字一致**，测试会对账）
# ---------------------------------------------------------------------------

#: 金额列的精度与标度：`NUMERIC(20, 8)`。
AMOUNT_PRECISION: Final[int] = 20
AMOUNT_SCALE: Final[int] = 8

#: 用量列的精度与标度：`NUMERIC(30, 10)`。
#:
#: 用量比金额需要更多整数位（token 数可以是百万级，秒数也可以是六位），
#: 而小数位比金额少（供应商按整数计数或两三位小数上报）。
QUANTITY_PRECISION: Final[int] = 30
QUANTITY_SCALE: Final[int] = 10

#: 量化单位：`Decimal("1E-8")` / `Decimal("1E-10")`。
#:
#: 用 `scaleb` 而不是 `Decimal("0.00000001")`：后者的有效位数依赖字符串，
#: 而 `scaleb` 生成的是**指数形式**的规范量子，量化结果一律 8 位小数。
AMOUNT_QUANTUM: Final[Decimal] = Decimal(1).scaleb(-AMOUNT_SCALE)
QUANTITY_QUANTUM: Final[Decimal] = Decimal(1).scaleb(-QUANTITY_SCALE)

ZERO: Final[Decimal] = Decimal(0)

#: 传输层的小数位数。**必须与 `AMOUNT_SCALE` 相等** —— `to_wire` 用它定长输出。
WIRE_DECIMALS: Final[int] = AMOUNT_SCALE

#: 允许接受的字符串长度上限。防御性：一个十兆字符的"数字"不该进解析器。
MAX_WIRE_CHARS: Final[int] = 64


class MoneyError(ValueError):
    """金额层的输入非法。

    继承 `ValueError` 而不是 `ApiError`：本模块是**纯函数层**，不认识 HTTP。
    把它翻译成哪个状态码是调用方（service / api）的判断。
    """


class MoneyPrecisionError(MoneyError):
    """把某个值量化到目标标度会**丢失有效位**。

    只在严格口径下抛（:func:`parse_wire_strict` / :func:`mul_amount`）。
    单独一个异常类型的理由：调用方可能想"容忍超精度但记一笔告警" ——
    那需要能精确捕获这一种，而不是把全部 `MoneyError` 一起吞掉。
    """


# ---------------------------------------------------------------------------
# 一、输入守卫 —— 本模块"零 float"的落点
# ---------------------------------------------------------------------------


def _reject_float(value: object, *, label: str) -> None:
    """拒绝 `float`（含 `bool`）。

    **为什么连 `bool` 一起拒**：`True` 是 `int` 的子类，`Decimal(True)` 得 1 ——
    一个"金额传了布尔"的程序缺陷会被静默地当成 1 元钱。
    """
    if isinstance(value, bool):
        raise MoneyError(f"{label} 不接受 bool：{value!r}")
    if isinstance(value, float):
        raise MoneyError(
            f"{label} 不接受 float（二进制浮点无法精确表示十进制小数，"
            f"偏差会在对账时才显形）：{value!r}。请传 Decimal / str / int"
        )


def _to_decimal(value: Decimal | str | int, *, label: str) -> Decimal:
    """把受支持的类型转成 `Decimal`。**不接受 `float`**。"""
    _reject_float(value, label=label)
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        result = _parse_text(value, label=label)
    else:
        raise MoneyError(f"{label} 的类型不受支持：{type(value).__name__}")
    if not result.is_finite():
        raise MoneyError(f"{label} 必须是有限数（不接受 NaN / Infinity）：{value!r}")
    return result


def _parse_text(raw: str, *, label: str) -> Decimal:
    """字符串 → `Decimal`。**不用 `Decimal(raw)` 的宽松构造**。

    `Decimal("  1 2 ")` 之类会被 `Decimal` 直接拒，但 `Decimal("1_0")`
    在部分 Python 版本上会通过（下划线分隔符），而它对一个金额是荒谬的形态。
    因此先做形态自检：只允许 `[+-]?digits[.digits]`。
    """
    text = raw.strip()
    if not text:
        raise MoneyError(f"{label} 是空白字符串 —— 空值请用 None 表达")
    if len(text) > MAX_WIRE_CHARS:
        raise MoneyError(f"{label} 超过 {MAX_WIRE_CHARS} 字符：长度 {len(text)}")
    if text.startswith(("+", "-")):
        text = text[1:]
    head, _, tail = text.partition(".")
    if not head and not tail:
        raise MoneyError(f"{label} 不是数字：{raw!r}")
    for part in (head, tail):
        if part and not part.isdigit():
            raise MoneyError(f"{label} 不是十进制数字：{raw!r}")
    if not head:
        # `.5` 这种形态 `Decimal` 能解析，但它不是一个"金额"的正常写法。
        raise MoneyError(f"{label} 缺少整数部分：{raw!r}")
    try:
        return Decimal(raw.strip())
    except InvalidOperation as exc:  # pragma: no cover - 上面的形态自检已覆盖
        raise MoneyError(f"{label} 不是合法数字：{raw!r}") from exc


def as_amount(value: Decimal | str | int) -> Decimal:
    """把输入转成**未量化**的 `Decimal` 金额（仍然拒绝 `float`）。

    与 :func:`quantize` 的分工：本函数只做类型守卫与合法性检查，
    不改变数值。需要落库的值一律再走一次 :func:`quantize`。
    """
    return _to_decimal(value, label="金额")


def as_quantity(value: Decimal | str | int) -> Decimal:
    """把输入转成**未量化**的 `Decimal` 用量（拒绝 `float`）。"""
    return _to_decimal(value, label="用量")


# ---------------------------------------------------------------------------
# 二、量化 —— 全仓唯一
# ---------------------------------------------------------------------------


def _quantize(value: Decimal, quantum: Decimal, *, label: str) -> Decimal:
    try:
        return value.quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        # 整数部分超出精度的容纳范围（例如 1e30 塞进 NUMERIC(20,8)）。
        raise MoneyError(
            f"{label} 超出 NUMERIC({AMOUNT_PRECISION},{AMOUNT_SCALE}) 的表示范围：{value}"
        ) from exc


def quantize(value: Decimal | str | int) -> Decimal:
    """量化到 8 位小数（`ROUND_HALF_UP`）。**幂等**。

    落库前必经此处。幂等性是可穷举验证的（见测试 `test_quantize_is_idempotent`），
    这条性质是 R7 的判据本体：若量化不幂等，"同一个数"就有了两种写法。
    """
    return _quantize(as_amount(value), AMOUNT_QUANTUM, label="金额")


def quantize_quantity(value: Decimal | str | int) -> Decimal:
    """把用量量化到 10 位小数。"""
    return _quantize(as_quantity(value), QUANTITY_QUANTUM, label="用量")


def is_quantized(value: Decimal | str | int) -> bool:
    """该值是否**已经是**规范形态（量化后与原值逐位相等）。"""
    original = as_amount(value)
    return _quantize(original, AMOUNT_QUANTUM, label="金额") == original


def normalize_for_storage(value: Decimal | str | int) -> Decimal:
    """落库前的唯一入口。当前等于 :func:`quantize`。

    单独一个名字的理由：让"写进数据库的每一个金额都过了哪一道"
    在代码里可 grep。将来若口径变化（例如改成 `ROUND_HALF_EVEN`），
    改动点只有本函数与 :func:`quantize` 一处，而不是散落的调用点。
    """
    return quantize(value)


# ---------------------------------------------------------------------------
# 三、序列化 —— 全仓唯一
# ---------------------------------------------------------------------------


def to_wire(value: Decimal | str | int) -> str:
    """`Decimal` → 规范化字符串（定长 8 位小数，无指数，无千分位）。

    **为什么不直接 `str(decimal)`**：`str(Decimal("1E-8"))` 得到 `"1E-8"`，
    `str(Decimal("100"))` 得到 `"100"` —— 同一个数值两种形态，
    而前端与对账脚本按字符串比较时会判为不等。

    **为什么不用 JSON number**：JSON 规范里 `0.1` 没有精确定义
    （解析器可以给出 float，也可以给出任意精度），协议因此已把金额
    定成字符串（`gateway/types.py` 的 `cost_amount: str | None`）。
    """
    return f"{quantize(value):.{WIRE_DECIMALS}f}"


def to_wire_optional(value: Decimal | str | int | None) -> str | None:
    """可空版本的 :func:`to_wire`（`None` 原样返回 `None`）。"""
    return None if value is None else to_wire(value)


def parse_wire_optional(raw: str | None, *, label: str = "金额") -> Decimal | None:
    """字符串 → `Decimal`（量化到 8 位）。**空与空白都归一为 `None`**。

    ## 容错边界（本包定死，闭合 OP-132）

    | 输入 | 结果 | 理由 |
    |---|---|---|
    | `None` | `None` | 供应商没给金额 |
    | `""` / `"   "` | `None` | 归一为"没给"—— **不当作 0**，两者必须可区分 |
    | `"0"` / `"0.00"` | `Decimal("0E-8")` | 明确的零 |
    | `"12.5"` | `Decimal("12.50000000")` | 正常 |
    | `"-3"` | `Decimal("-3E-8")` | **允许负号**：冲正分录要用 |
    | `"1.000000001"` | `Decimal("1.00000000")` | 超精度**量化**（`ROUND_HALF_UP`） |
    | `"abc"` / `"1e5"` / `"1,000"` | 抛 :class:`MoneyError` | 非法形态**绝不静默当 0** |
    | `float` 对象 | 抛 :class:`MoneyError` | 本模块零 float |

    `"1e5"` 被拒是**刻意的**：科学计数法不是一个金额的正常写法，
    而它在不同解析器下有不同结果 —— 接受它等于把"这个数是多少"
    交给解析器实现决定。
    """
    if raw is None:
        return None
    if isinstance(raw, float):  # 类型上不该出现，但守卫要显式
        _reject_float(raw, label=label)
    if not isinstance(raw, str):
        raise MoneyError(f"{label} 必须是 str 或 None，收到 {type(raw).__name__}")
    if not raw.strip():
        return None
    return quantize(_parse_text(raw, label=label))


def parse_wire_strict(raw: str, *, label: str = "金额") -> Decimal:
    """严格版解析：**超精度即抛错**，不静默舍入。

    用于内部计算路径（本包自己算出来的金额不允许有小数位浪费），
    以及需要"金额必须逐位精确"的人工录入入口。
    """
    value = _parse_text(raw, label=label)
    quantized = _quantize(value, AMOUNT_QUANTUM, label=label)
    if quantized != value:
        raise MoneyPrecisionError(
            f"{label} 超出 {AMOUNT_SCALE} 位小数（{value} → {quantized}）—— "
            "严格口径下不允许有损量化"
        )
    return quantized


def parse_quantity_wire(raw: str | None, *, label: str = "用量") -> Decimal | None:
    """用量字符串 → `Decimal`（量化到 10 位）。容错边界同 :func:`parse_wire_optional`。"""
    if raw is None:
        return None
    if not raw.strip():
        return None
    return quantize_quantity(_parse_text(raw, label=label))


def quantity_to_wire(value: Decimal | str | int) -> str:
    """用量 → 规范化字符串（定长 10 位小数）。"""
    return f"{quantize_quantity(value):.{QUANTITY_SCALE}f}"


# ---------------------------------------------------------------------------
# 四、运算
# ---------------------------------------------------------------------------


def add(left: Decimal | str | int, right: Decimal | str | int) -> Decimal:
    """定点加法（结果量化）。"""
    return quantize(as_amount(left) + as_amount(right))


def sub(left: Decimal | str | int, right: Decimal | str | int) -> Decimal:
    """定点减法（结果量化）。"""
    return quantize(as_amount(left) - as_amount(right))


def mul_amount(value: Decimal | str | int, factor: Decimal | str | int) -> Decimal:
    """乘法，**结果超精度即抛** :class:`MoneyPrecisionError`。

    ## 为什么这里用严格口径（与解析函数相反）

    本函数出现在**本包自己算钱**的路径上（预估 `单价 × 数量`）。
    乘积超精度说明所选精度口径不够用 —— 那是**设计缺陷**，
    应当当场暴露，而不是把它悄悄舍入后写进账面。

    舍入的后果不对称：
      · 预估多算一分：运营看到的数字略高，对账时发现差异 → 可查；
      · 结算少算一分：**累计 50 万次调用后账面少 5000 元**，而对账
        永远显示"两边自洽"（因为两边都用了同一个错的舍入）→ 不可查。

    因此宁可让一次预估失败并暴露出精度口径问题。
    """
    product = as_amount(value) * as_quantity(factor)
    quantized = _quantize(product, AMOUNT_QUANTUM, label="乘积")
    if quantized != product:
        raise MoneyPrecisionError(
            f"金额乘法结果超出 {AMOUNT_SCALE} 位小数（{product} → {quantized}）—— "
            "请调整单价或数量的精度口径，而不是让它静默舍入"
        )
    return quantized


def compare(left: Decimal | str | int, right: Decimal | str | int) -> int:
    """比较两个金额（**先量化再比**）。

    先量化的理由：`Decimal("100") != Decimal("100.00000000")` 在
    **字符串**层面成立、在 `Decimal` 层面不成立。本函数在 `Decimal` 层面
    比较（量化不改变数值，只是把"同一数值的不同写法"归一），
    因此 `compare("100", "100.00000000") == 0`。

    返回值语义与 `Decimal.compare` 一致：`-1` / `0` / `1`。
    """
    return quantize(left).compare(quantize(right))


def is_zero(value: Decimal | str | int) -> bool:
    return quantize(value) == ZERO


def is_negative(value: Decimal | str | int) -> bool:
    return quantize(value) < ZERO


def is_positive(value: Decimal | str | int) -> bool:
    return quantize(value) > ZERO


def sum_amounts(values: object) -> Decimal:
    """求和（结果量化）。空可迭代 → `ZERO`。

    `values` 声明为 `object` 而不是 `Iterable[...]`，是为了接受任意
    可迭代物（生成器、`Query` 的结果集）而不强迫调用方先物化。
    类型守卫在循环内逐项生效：**任何一项是 `float` 都会抛**。
    """
    total = ZERO
    for item in values:  # type: ignore[union-attr]
        total += as_amount(item)
    return quantize(total)


def abs_amount(value: Decimal | str | int) -> Decimal:
    """绝对值（结果量化）。对冲正分录取"发生了多少钱"时用。"""
    return quantize(abs(as_amount(value)))


__all__ = [
    "AMOUNT_PRECISION",
    "AMOUNT_QUANTUM",
    "AMOUNT_SCALE",
    "MAX_WIRE_CHARS",
    "MoneyError",
    "MoneyPrecisionError",
    "QUANTITY_PRECISION",
    "QUANTITY_QUANTUM",
    "QUANTITY_SCALE",
    "WIRE_DECIMALS",
    "ZERO",
    "abs_amount",
    "add",
    "as_amount",
    "as_quantity",
    "compare",
    "is_negative",
    "is_positive",
    "is_quantized",
    "is_zero",
    "mul_amount",
    "normalize_for_storage",
    "parse_quantity_wire",
    "parse_wire_optional",
    "parse_wire_strict",
    "quantize",
    "quantize_quantity",
    "quantity_to_wire",
    "sub",
    "sum_amounts",
    "to_wire",
    "to_wire_optional",
]
