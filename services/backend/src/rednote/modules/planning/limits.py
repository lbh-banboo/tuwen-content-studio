"""批次数量的越界校验 —— **全仓唯一入口**。**纯函数**。

承载 ADR-13 的验证方式第 6 条「**无硬编码残留**」与代价 #2
（"需覆盖创建、调整、局部重做、批量操作，漏一个入口就是漏洞"）。

---

## 一、为什么"唯一入口"是硬要求，而不只是代码整洁

ADR-13 的决策把数量上下限从 DDL 的 CHECK 移到了 `runtime_settings` +
应用层校验。这个移动有一个**必须被代价覆盖**的后果：

| | 上界在 DDL 时 | 上界在 runtime_settings 时 |
|---|---|---|
| 谁拦越界 | 数据库（挡得住任何写入路径） | **应用层**（只挡得住调用它的路径） |

也就是说：**移动上界 = 把一道数据库防线换成了一道应用防线**。
那么"这道防线覆盖了多少路径"就直接等于"安全性有多少"。

若校验逻辑散落在 WP31 试产关口、WP32 扩产、WP23 局部重做三处，
漏掉任何一处就是一个**静默的口子**（"能提交超过上限的批次"、
"没有任何异常"）。因此本模块的做法是：

  · 校验函数**只有一个**：`validate_quantity()`；
  · 上界**只有一个来源**：`get_effective_limits()` 读出的 `BatchLimits`；
  · 判据：全仓 grep `DEFAULT_BATCH_MAX` / 字面量 `10`/`30`/`50`
    作为上界的用法（证据脚本做，见 `collect_wp24_evidence.py`）。

## 二、为什么"下界"仍留在 DDL（ADR-13 决策表的原文）

`quantity > 0` 是**数据结构层面的不可能**（0 条或负条数的批次没有意义），
与"业务参数"不同。把它留在 CHECK 里的好处：
绕过应用层直接写库也造不出 `quantity = 0` 的行 —— 而"0 条的批次"
会被下游当成"一个已经做完的批次"，**不报错**。

因此本模块的 `MIN_QUANTITY` 与 DDL 的 CHECK **同值**（单一来源在 `types.py`），
`validate_quantity` 也检查下界：不是重复防线，而是**更早、错误信息更可读**的那一道
（DDL 报的是约束名，应用层报的是"试产批次 0 条，最少 1 条"）。

## 三、错误信息必须带**生效值与来源**（ADR-13 验证方式第 3 条）

> "运营能在页面上自助确认当前生效值"是硬要求。
> 不能只留一条数据库错误，也不能只在日志里。

因此越界错误必须携带：

  · `limit`（生效上界）、`quantity`（提交值）、`batch_kind`（哪一档）；
  · `source`（这个上界来自 `GLOBAL` / `INHERITED` / `MANUAL`）——
    这一条是 OP-60 的延伸：**覆盖过的项目上限与全局不同**，
    而运营看到的报错要说清"你是按哪个值被拒的"。

`describe_limits()` 提供"当前生效值是什么"的可读描述，供前端展示。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.planning.types import (
    BATCH_KINDS,
    DEFAULT_BATCH_MAX,
    MAX_QUANTITY_BOUND,
    MIN_QUANTITY,
    REASON_BATCH_KIND_UNKNOWN,
    REASON_QUANTITY_ABOVE_MAX,
    REASON_QUANTITY_BELOW_MIN,
    BatchLimits,
    EffectiveSetting,
)

#: 供 `describe_limits` 用的档位中文名。**只为可读性**，不参与任何判断。
_KIND_LABELS: Final[dict[str, str]] = {
    "TRIAL": "试产",
    "SCALE": "扩产",
    "BULK": "批产",
}


class QuantityOutOfRange(ValueError):
    """数量越界。携带结构化字段，供 API 层组装 `details`。

    **不继承 `ApiError`**：本模块是纯函数层，不能依赖 `platform.errors`
    （那会让"纯函数"带上 Web 框架的味道，也会让测试库不必启动就能测的性质
    变成必须 import FastAPI 的东西）。API 层捕获它并转成 `VALIDATION_FAILED`。

    字段都是公开属性 —— 证据脚本与测试直接读它们，
    **不去匹配 `str(exc)` 的文案**（文案会变，字段不会）。
    """

    def __init__(
        self,
        *,
        reason: str,
        batch_kind: str,
        quantity: int,
        limit: int | None,
        source: str | None,
    ) -> None:
        self.reason = reason
        self.batch_kind = batch_kind
        self.quantity = quantity
        self.limit = limit
        self.source = source

        label = _KIND_LABELS.get(batch_kind, batch_kind)
        if reason == REASON_BATCH_KIND_UNKNOWN:
            text = f"未知的批次档位：{batch_kind!r}（合法取值：{sorted(BATCH_KINDS)}）"
        elif reason == REASON_QUANTITY_BELOW_MIN:
            text = f"{label}批次至少 {MIN_QUANTITY} 条，收到 {quantity} 条。"
        else:
            suffix = f"（生效上界来自 {source}）" if source else ""
            text = (
                f"{label}批次最多 {limit} 条，收到 {quantity} 条{suffix}。"
            )
        super().__init__(text)


def validate_quantity(
    limits: BatchLimits,
    batch_kind: str,
    quantity: int,
    *,
    source: str | None = None,
) -> None:
    """校验 `quantity` 是否在 `batch_kind` 档位的生效区间内。

    通过则返回 `None`；越界抛 `QuantityOutOfRange`。

    ## 为什么签名是 `(limits, kind, quantity)` 而不是 `(session, kind, quantity)`

    见模块 docstring 第一节：校验逻辑必须是**纯函数**，
    这样它才能在没有任何数据库的情况下被穷举测试
    （`kind × quantity ∈ [下界-1, 下界, 上界, 上界+1]` 四个边界 × 三档）。
    若它自己读库，"上界从哪来"就与"怎么校验"混在一起，
    而后者是本包唯一需要被证明的部分。

    调用方约定（**唯一入口清单**，收口时逐条核对）：

    | 调用方 | 何时 |
    |---|---|
    | WP31 试产关口 | `TRIAL` |
    | WP32 扩产 | `SCALE` |
    | WP32 批产 | `BULK` |
    | WP23 局部重做 | 沿用原批次档位 |

    `source` 是可选参数（来自 `EffectiveSetting.source`），
    只影响错误文案 —— **它不参与判断**，因此不传也不会算错。
    """
    if batch_kind not in BATCH_KINDS:
        raise QuantityOutOfRange(
            reason=REASON_BATCH_KIND_UNKNOWN,
            batch_kind=batch_kind,
            quantity=quantity,
            limit=None,
            source=source,
        )

    if quantity < MIN_QUANTITY:
        raise QuantityOutOfRange(
            reason=REASON_QUANTITY_BELOW_MIN,
            batch_kind=batch_kind,
            quantity=quantity,
            limit=None,
            source=source,
        )

    limit = limits.max_for(batch_kind)
    # 上界本身也要在上界内 —— 否则"配置被写成 10 亿"会让这道防线形同虚设
    # （任何数量都不越界）。`MAX_QUANTITY_BOUND` 是这类脏数据的拦截面。
    effective_limit = min(limit, MAX_QUANTITY_BOUND)
    if quantity > effective_limit:
        raise QuantityOutOfRange(
            reason=REASON_QUANTITY_ABOVE_MAX,
            batch_kind=batch_kind,
            quantity=quantity,
            limit=effective_limit,
            source=source,
        )


def is_within_limits(limits: BatchLimits, batch_kind: str, quantity: int) -> bool:
    """`validate_quantity` 的布尔形态，供"批量预检"场景使用。

    **它只是薄包装**，不复制任何判断 —— 复制就会出现两处口径，
    而"预检说可以、提交说不行"是最难查的一类缺陷。
    """
    try:
        validate_quantity(limits, batch_kind, quantity)
    except QuantityOutOfRange:
        return False
    return True


def default_limits() -> BatchLimits:
    """ADR-13 的初始值（10 / 30 / 50）。

    **只在两处使用**：迁移写入 `GLOBAL` 行、以及测试断言"迁移写的与这里一致"。
    读取路径**不得**用它兜底 —— 兜底会让"读不到配置"表现为"用默认值"
    且不报错，而"为什么我改的值没生效"就无从查起（见 `types.py` 的说明）。
    """
    return BatchLimits(
        trial=DEFAULT_BATCH_MAX["TRIAL"],
        scale=DEFAULT_BATCH_MAX["SCALE"],
        bulk=DEFAULT_BATCH_MAX["BULK"],
    )


def describe_limits(setting: EffectiveSetting) -> dict[str, object]:
    """把生效配置整理成前端可直接展示的形状（含 `diverged` 提示）。

    返回：

        {
          "key": "batch.limits",
          "value": {"TRIAL": 10, "SCALE": 30, "BULK": 50},
          "source": "MANUAL",
          "globalValue": {...},
          "diverged": true,
          "summary": "试产 ≤10 条 · 扩产 ≤30 条 · 批产 ≤50 条",
          "divergenceNote": "本项目已覆盖 · 全局为 试产 ≤12 条 · …"
        }

    **`divergenceNote` 是本包对 OP-60 的直接产出**：老大拍板"项目级必须有副本"，
    而副本与全局一旦不同就必须**有人看得见**（否则就是助手当初指出的
    "悄悄分叉"）。这条文案是"看得见"的最后一公里。
    """
    limits = setting.as_limits()
    summary = " · ".join(
        f"{_KIND_LABELS[kind]} ≤{limits.max_for(kind)} 条"
        for kind in ("TRIAL", "SCALE", "BULK")
    )
    result: dict[str, object] = {
        "key": setting.key,
        "value": limits.as_dict(),
        "source": setting.source,
        "globalValue": setting.global_value,
        "diverged": setting.diverged,
        "summary": summary,
    }

    if setting.global_value is not None:
        try:
            global_limits = BatchLimits(
                trial=int(setting.global_value["TRIAL"]),
                scale=int(setting.global_value["SCALE"]),
                bulk=int(setting.global_value["BULK"]),
            )
            global_summary = " · ".join(
                f"{_KIND_LABELS[kind]} ≤{global_limits.max_for(kind)} 条"
                for kind in ("TRIAL", "SCALE", "BULK")
            )
        except (KeyError, TypeError, ValueError):
            global_summary = "（全局值形状不完整）"
        if setting.diverged:
            result["divergenceNote"] = (
                f"本项目已覆盖 · 当前 {summary} · 全局 {global_summary}"
            )
        else:
            result["divergenceNote"] = ""
    else:
        result["divergenceNote"] = ""

    return result


__all__ = [
    "QuantityOutOfRange",
    "default_limits",
    "describe_limits",
    "is_within_limits",
    "validate_quantity",
]
