"""与 WP13（任务与 Outbox）的接缝（WP16）。

**本模块只做一件事：把网关的调用结果翻译成任务框架的申报**。

为什么不把 `dispatch_call` 直接塞进一个 `TaskHandlerFn`：WP13 的处理器签名是
`(ClaimedJob, JobRepository) -> HandlerOutcome`，而派发需要 `GatewayRepository`
（以及适配器与请求体）。硬塞进去要么给 JobRepository 加一个"造网关仓库"的方法
（让任务框架知道网关的存在，方向反了），要么让处理器去碰 `JobRepository` 的私有 session。
两者都比"提供一个映射函数、由 WP17/WP18 在自己的处理器里调用"更差。

**真正的处理器（构造请求体、选适配器）由 WP17/WP18 写** ——
它们才知道"这类任务该带什么上下文"。本包只负责把结果翻译对。
这与 WP13 的立场一致："本包提供注册点，但不替它们决定业务语义"。

---

## `UNKNOWN → WAITING_RECONCILIATION`（**不是 FAILED**）

这是本模块**唯一需要想清楚**的一步：

| 若映射到 | 会发生什么 | 对吗 |
|---|---|---|
| `FAILED`（可重试） | WP13 requeue → 新 attempt → 网关按新 `round_no`？**不**，`round_no` 默认 0 → **找到既有 UNKNOWN 调用 → 不重发**。所以即使映射到 FAILED 也不会立刻重复计费 | 侥幸对，但语义错 |
| `FAILED`（不可重试） | 任务永久失败，而**真实结果可能已经成功**了 —— 用户看到"生成失败"，实际供应商早已完成并计费 | ❌ 错 |
| **`WAITING_RECONCILIATION`** | 任务停在"等核对"；WP13 的 `reconcile_job` **明确禁止 requeue**；有人拿依据核对后才落终态 | ✅ **对** |

选它的理由不是"更安全"，而是**它是唯一状态语义正确的那个**：
`UNKNOWN` 表达的是"我们不知道成没成"，而 `WAITING_RECONCILIATION`
正是系统里为"不知道成没成"准备的状态。
数据结构 §13.4 的"UNKNOWN 不因租约过期变成可自由重试"，
在任务侧就体现为"它进的是一个禁止 requeue 的状态"。

## `retryable` 的默认值是 `False`

调用方必须**显式**说"这次失败值得再试"。
理由：重试一次外部调用可能**再买一次**，所以"可重试"不能是默认行为。
判断"供应商 429/503 是否值得重试"需要供应商侧的知识，属 WP17/WP18 与该供应商的适配器。
本包给一个**保守的**默认值（不重试），让"多花一次钱"永远是一个需要有人签字的决定。
"""

from __future__ import annotations

from rednote.modules.gateway.types import (
    ADAPTER_ACCEPTED,
    ADAPTER_FAILED,
    ADAPTER_SUCCEEDED,
    ADAPTER_UNKNOWN,
    COST_CONFIRMED,
    CapabilityResult,
)
from rednote.modules.jobs.models import (
    STATE_FAILED,
    STATE_SUCCEEDED,
    STATE_WAITING_PROVIDER,
    STATE_WAITING_RECONCILIATION,
)
from rednote.modules.jobs.types import HandlerOutcome


def outcome_for_result(
    result: CapabilityResult, *, retryable: bool = False
) -> HandlerOutcome:
    """`CapabilityResult` → `HandlerOutcome`。

    | 适配器结果（协议 §12.1） | 任务状态（数据结构 §12） | 说明 |
    |---|---|---|
    | `SUCCEEDED` | `SUCCEEDED` | `resultRef` 用 `externalRequestId`（可回溯到供应商侧） |
    | `ACCEPTED` | `WAITING_PROVIDER` | 已受理，等它；WP13 在等待态**保留租约**，不放开重领 |
    | `UNKNOWN` | `WAITING_RECONCILIATION` | 见模块 docstring |
    | `FAILED` | `FAILED` | `retryable` 由调用方显式给（默认 `False`） |
    """
    if result.state == ADAPTER_SUCCEEDED:
        return HandlerOutcome(
            state=STATE_SUCCEEDED,
            result_ref=result.external_request_id,
            usage_pending=_usage_pending(result),
        )
    if result.state == ADAPTER_ACCEPTED:
        return HandlerOutcome(
            state=STATE_WAITING_PROVIDER,
            result_ref=result.external_request_id,
            usage_pending=_usage_pending(result),
        )
    if result.state == ADAPTER_UNKNOWN:
        # 这次可能已经产生了未结用量 —— 一律按"可能有"处理，
        # 否则真实产生的费用会无声消失（与 WP13 对失联 attempt 的处理同一理由）。
        return HandlerOutcome(
            state=STATE_WAITING_RECONCILIATION,
            error_code=result.error_code,
            usage_pending=True,
        )
    if result.state == ADAPTER_FAILED:
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code=result.error_code,
            retryable=retryable,
            usage_pending=_usage_pending(result),
        )
    # 未知状态由 `validate_capability_result` 在更靠近适配器的地方拦下；
    # 走到这里说明校验被绕过，属缺陷，让它显式失败而不是猜一个状态。
    raise ValueError(f"未知的适配器结果状态: {result.state}")


def _usage_pending(result: CapabilityResult) -> bool:
    """这次调用是否留下了"未结的外部用量"（WP25 费用对账的输入）。

    判据：**有过用量，且计费未确认**。`CONFIRMED` 表示供应商已给出确定账单，
    那时用量已结转、不再"未结"。
    """
    return bool(result.usage) and result.cost_status != COST_CONFIRMED


__all__ = ["outcome_for_result"]
