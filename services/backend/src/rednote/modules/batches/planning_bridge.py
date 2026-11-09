"""`batches` → `planning` 的接缝（WP32，**只读**）：数量上限、校验、分配、草稿。

* `get_effective_limits('batch.limits')` + `validate_quantity`（`limits.py`）：数量上界只有这一处真值；
  创建 / 预览是写事务，`materialize=True`（默认语义）；纯读路径一律 `materialize=False`；
* `next_batch_allocation` / `plan_payload` / `dimension_allocation`（`service.py`）：
  分配与 PLAN 载荷由 planning 自己算，**本包不重写算法**；
* `get_draft`：PLAN 版本节点只存摘要不存载荷，分配输入（场景 / 形式比例）在 `plan_drafts`，
  本包用 `plan_payload(draft)` 重算摘要与钉住的 PLAN 节点比对（“前提变化”判定）。

ISS-128：`get_effective_limits` 不校验项目存在，调用前必须已过 `ProjectAccessDep`。
"""

from __future__ import annotations

import uuid
from typing import Any

from rednote.modules.planning.limits import QuantityOutOfRange, validate_quantity
from rednote.modules.planning.repository import PlanningRepository
from rednote.modules.planning.service import (
    DIMENSION_FORM,
    DIMENSION_SCENE,
    dimension_allocation,
    next_batch_allocation,
    plan_payload,
)
from rednote.modules.planning.settings_service import get_effective_limits
from rednote.modules.planning.types import (
    MAX_QUANTITY_BOUND,
    SETTING_KEY_BATCH_LIMITS,
    AllocationResult,
    BatchLimits,
    EffectiveSetting,
)


def read_batch_limits(
    session: Any, *, project_id: uuid.UUID, materialize: bool
) -> EffectiveSetting:
    """读本项目 `batch.limits` 的生效值。写事务传 `True`，纯读路径必须传 `False`。"""
    return get_effective_limits(
        PlanningRepository(session),
        SETTING_KEY_BATCH_LIMITS,
        project_id,
        materialize=materialize,
    )


def get_plan_draft(session: Any, *, project_id: uuid.UUID) -> Any | None:
    return PlanningRepository(session).get_draft(project_id)


def draft_plan_payload(draft: Any) -> dict[str, Any]:
    """按草稿重算 PLAN 载荷（与 `submit_draft` 固化时同一函数，摘要可比）。"""
    return plan_payload(
        draft,
        scene_result=dimension_allocation(draft, DIMENSION_SCENE),
        form_result=dimension_allocation(draft, DIMENSION_FORM),
    )


def allocate_batch(
    draft: Any,
    dimension: str,
    *,
    batch_total: int,
    allocated_so_far: dict[str, int],
) -> AllocationResult:
    return next_batch_allocation(
        draft, dimension, batch_total=batch_total, allocated_so_far=allocated_so_far
    )


__all__ = [
    "MAX_QUANTITY_BOUND",
    "DIMENSION_FORM",
    "DIMENSION_SCENE",
    "AllocationResult",
    "BatchLimits",
    "EffectiveSetting",
    "QuantityOutOfRange",
    "allocate_batch",
    "draft_plan_payload",
    "get_plan_draft",
    "read_batch_limits",
    "validate_quantity",
]
