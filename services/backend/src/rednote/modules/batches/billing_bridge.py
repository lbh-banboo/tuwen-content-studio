"""`batches` → `billing` 的接缝（WP32，**只读 / 估算**）。

方案 B（OP-231 暂用默认）：批次层**只预估 + 可用额度预检**，**不预占**。
逐条预占 / 结算 / 释放留在 WP26 handler 执行阶段；本包**不调用** `reserve_budget` /
`settle_*` / `release_*`（否则同一笔钱占两次，G3）。

* 预估经 `content.billing_bridge.estimate_generation_cost`（`quantity` 的唯一口径 `calls_for` 在里面，
  本包不自己乘）；每个 `task_kind` 各一次；
* 预检读 `billing.service.get_budget_view`（`configured=False` 时报“未配置”，不报 `available=0`）。
"""

from __future__ import annotations

import uuid
from typing import Any

from rednote.modules.billing import money
from rednote.modules.billing.repository import BillingRepository
from rednote.modules.billing.service import get_budget_view
from rednote.modules.billing.types import BudgetView, CostEstimateView
from rednote.modules.content.billing_bridge import estimate_generation_cost


def estimate_for_task_kind(
    session: Any,
    *,
    project_id: uuid.UUID,
    task_kind: str,
    content_count: int,
    batch_kind: str,
    provider_id: uuid.UUID,
    model_profile_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    request_id: str | None,
) -> CostEstimateView:
    """预估一个 `task_kind`（写 `cost_estimates` 一行；**不预占**）。"""
    return estimate_generation_cost(
        BillingRepository(session),
        project_id=project_id,
        task_kind=task_kind,
        content_count=content_count,
        batch_kind=batch_kind,
        provider_id=provider_id,
        model_profile_id=model_profile_id,
        actor_id=actor_id,
        request_id=request_id,
    )


def read_budget(session: Any, *, project_id: uuid.UUID) -> BudgetView:
    """只读预算视图（四值 + `configured`）。"""
    return get_budget_view(BillingRepository(session), project_id=project_id)


def sum_amounts(values: list[Any]) -> Any:
    """金额求和（billing 的 `money.sum_amounts`，本包不自己做钱的算术）。"""
    return money.sum_amounts(values)


def compare_amounts(left: Any, right: Any) -> int:
    return money.compare(left, right)


def amount_to_wire(value: Any) -> str:
    """金额一律字符串上线（`(20,8)` 精度，jsonb number 会丢位数）。"""
    return money.to_wire(value)


__all__ = [
    "BudgetView",
    "CostEstimateView",
    "amount_to_wire",
    "compare_amounts",
    "estimate_for_task_kind",
    "read_budget",
    "sum_amounts",
]
