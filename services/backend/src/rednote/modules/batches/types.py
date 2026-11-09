"""WP32 扩产 / 批产的类型与常量（**不 import 任何 `rednote.modules.*`**，平级模块不可互 import）。

落库取值域三个（`batch_kind` / `state` / `limit_source`，DDL 里写字面量，ISS-191），
另有一个**不落库**的派生域 `progress.state`（读时折算，见 `progress.py`）。

## 关口检查依赖（`GateChecker`）

WP31 的试产关口尚未接线时，本包以**一个可调用对象**表达“关口”这个依赖：

    GateChecker = Callable[[uuid.UUID], GateVerdict]

* 输入：`project_id`；
* 输出：`GateVerdict(passed, decision_id, decided_at, reason)` ——
  `passed` 为 `True` 才允许建批；`decision_id` / `decided_at` 是“最近一次关口决定”的最小信息，
  仅用于 409 `details` 的回显（可空：从未有过决定时为 `None`）；
* 默认实现（`dependencies.py::default_gate_checker`）**未接线时 fail-closed**：
  抛 503 `DEPENDENCY_UNAVAILABLE` + `details.reason = trial_gate_not_wired`；
* 未通过：409 `INVALID_TRANSITION` + `details.reason = gate_not_satisfied`。

闭包签名只收 `project_id`，数据库会话由 `dependencies.py` 的工厂绑定
（主线接 WP31 时只需替换工厂，不动 service）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Final

# ---------------------------------------------------------------------------
# 落库取值域（字面量化石在 DDL；这里是应用层同值常量）
# ---------------------------------------------------------------------------

BATCH_KIND_BULK: Final[str] = "BULK"
BATCH_KIND_SCALE: Final[str] = "SCALE"
BATCH_KIND_TRIAL: Final[str] = "TRIAL"
BATCH_KINDS: Final[frozenset[str]] = frozenset(
    {BATCH_KIND_BULK, BATCH_KIND_SCALE, BATCH_KIND_TRIAL}
)
#: 本包能创建的档位。TRIAL 轮由 WP31 自建（D2），本包不创建，但 CHECK 仍留 TRIAL 取值。
CREATABLE_BATCH_KINDS: Final[frozenset[str]] = frozenset(
    {BATCH_KIND_BULK, BATCH_KIND_SCALE}
)

BATCH_STATE_SUBMITTED: Final[str] = "SUBMITTED"
BATCH_STATE_CANCEL_REQUESTED: Final[str] = "CANCEL_REQUESTED"
BATCH_STATES: Final[frozenset[str]] = frozenset(
    {BATCH_STATE_SUBMITTED, BATCH_STATE_CANCEL_REQUESTED}
)

LIMIT_SOURCE_GLOBAL: Final[str] = "GLOBAL"
LIMIT_SOURCE_INHERITED: Final[str] = "INHERITED"
LIMIT_SOURCE_MANUAL: Final[str] = "MANUAL"
LIMIT_SOURCES: Final[frozenset[str]] = frozenset(
    {LIMIT_SOURCE_GLOBAL, LIMIT_SOURCE_INHERITED, LIMIT_SOURCE_MANUAL}
)


def _sql_list(values: frozenset[str]) -> str:
    return ", ".join(f"'{name}'" for name in sorted(values))


BATCH_KIND_LIST_SQL: Final[str] = _sql_list(BATCH_KINDS)
BATCH_STATE_LIST_SQL: Final[str] = _sql_list(BATCH_STATES)
LIMIT_SOURCE_LIST_SQL: Final[str] = _sql_list(LIMIT_SOURCES)

# ---------------------------------------------------------------------------
# 派生域：progress.state（不落库、无 CHECK；T-ENUM 与 OpenAPI 对账）
# ---------------------------------------------------------------------------

PROGRESS_QUEUED: Final[str] = "QUEUED"
PROGRESS_RUNNING: Final[str] = "RUNNING"
PROGRESS_CANCELLING: Final[str] = "CANCELLING"
PROGRESS_SUCCEEDED: Final[str] = "SUCCEEDED"
PROGRESS_PARTIAL: Final[str] = "PARTIAL"
PROGRESS_FAILED: Final[str] = "FAILED"
PROGRESS_CANCELLED: Final[str] = "CANCELLED"
PROGRESS_UNKNOWN: Final[str] = "UNKNOWN"
PROGRESS_STATES: Final[frozenset[str]] = frozenset(
    {
        PROGRESS_QUEUED,
        PROGRESS_RUNNING,
        PROGRESS_CANCELLING,
        PROGRESS_SUCCEEDED,
        PROGRESS_PARTIAL,
        PROGRESS_FAILED,
        PROGRESS_CANCELLED,
        PROGRESS_UNKNOWN,
    }
)

# 基线相对批次钉住值的状态（读时折算，同 WP29 `anchorState` 手法）。
BASELINE_STATE_CURRENT: Final[str] = "CURRENT"
BASELINE_STATE_SUPERSEDED: Final[str] = "SUPERSEDED"
BASELINE_STATE_UNKNOWN: Final[str] = "UNKNOWN"

# ---------------------------------------------------------------------------
# details.reason（开集，不落库）
# ---------------------------------------------------------------------------

REASON_IDEMPOTENCY_KEY_REUSED: Final[str] = "idempotency_key_reused"
REASON_IDEMPOTENCY_KEY_REQUIRED: Final[str] = "idempotency_key_required"
REASON_ALLOCATION_INCOMPLETE: Final[str] = "allocation_incomplete"
REASON_BATCH_ESTIMATE_EXCEEDS_AVAILABLE: Final[str] = "batch_estimate_exceeds_available"
REASON_BATCH_BUDGET_NOT_CONFIGURED: Final[str] = "budget_not_configured"
REASON_BATCH_CANCELLED: Final[str] = "batch_cancelled"
REASON_NOTHING_TO_RETRY: Final[str] = "nothing_to_retry"
REASON_BASELINE_CHANGED: Final[str] = "baseline_changed"
REASON_BASELINE_NOT_READY: Final[str] = "baseline_not_ready"
REASON_PLAN_CHANGED: Final[str] = "plan_changed"
REASON_PLAN_VERSION_INVALID: Final[str] = "plan_version_invalid"
REASON_GATE_NOT_SATISFIED: Final[str] = "gate_not_satisfied"
REASON_GATE_NOT_WIRED: Final[str] = "trial_gate_not_wired"
REASON_BATCH_KIND_UNKNOWN: Final[str] = "batch_kind_unknown"
REASON_LIMITS_NOT_CONFIGURED: Final[str] = "limits_not_configured"
REASON_BATCH_NOT_FOUND: Final[str] = "BATCH_NOT_FOUND"
REASON_REQUEST_IN_PROGRESS: Final[str] = "request_in_progress"

# ---------------------------------------------------------------------------
# 作用域串 / 审计动作 / 依赖边 purpose（开集）
# ---------------------------------------------------------------------------

IDEMPOTENCY_SCOPE_BATCH_RETRY_PREFIX: Final[str] = "batch-retry:"
AUDIT_ACTION_CREATE: Final[str] = "batch.create"
AUDIT_ACTION_CANCEL: Final[str] = "batch.cancel"
AUDIT_ACTION_RETRY: Final[str] = "batch.retry"
AUDIT_OBJECT_TYPE: Final[str] = "batch"
PURPOSE_BATCH_PLAN: Final[str] = "BATCH_PLAN"
PURPOSE_BATCH_BASELINE: Final[str] = "BATCH_BASELINE"

#: 快照节点（PLAN 版本节点）的显式修订号。**确定值**，不依赖 `max+1`（接缝冻结 §5.2，OP-91）。
SNAPSHOT_REVISION_NO: Final[int] = 1
#: 序号冲突（`uq_batches_project_kind_seq`）时重取的上限。
MAX_SEQ_ATTEMPTS: Final[int] = 3
MAX_IDEMPOTENCY_KEY_CHARS: Final[int] = 200


def job_idempotency_key(content_id: uuid.UUID, batch_id: uuid.UUID, round_no: int) -> str:
    """批次内任务的幂等键：`{content_id}:batch:{batch_id}:r{n}`（轮次进键）。"""
    return f"{content_id}:batch:{batch_id}:r{round_no}"


def content_idempotency_key(batch_id: uuid.UUID) -> str:
    """物化内容时给 content 接缝的幂等键：`batch:{batch_id}`。"""
    return f"batch:{batch_id}"


# ---------------------------------------------------------------------------
# 值类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateVerdict:
    """关口检查的最小结论（见模块 docstring）。

    `passed` 之外的三个字段只用于回显：从未有过决定时 `decision_id` / `decided_at` 为 `None`。
    """

    passed: bool
    decision_id: uuid.UUID | None = None
    decided_at: datetime | None = None
    reason: str | None = None


GateChecker = Callable[[uuid.UUID], GateVerdict]


@dataclass(frozen=True)
class CreateBatchCommand:
    """创建 / 预览批次的请求内容（`request_hash` 的输入）。"""

    batch_kind: str
    quantity: int
    plan_version_id: uuid.UUID
    baseline_id: uuid.UUID | None
    model_profile_id: uuid.UUID
    scope: str


__all__ = [
    "AUDIT_ACTION_CANCEL",
    "AUDIT_ACTION_CREATE",
    "AUDIT_ACTION_RETRY",
    "AUDIT_OBJECT_TYPE",
    "BASELINE_STATE_CURRENT",
    "BASELINE_STATE_SUPERSEDED",
    "BASELINE_STATE_UNKNOWN",
    "BATCH_KINDS",
    "BATCH_KIND_BULK",
    "BATCH_KIND_LIST_SQL",
    "BATCH_KIND_SCALE",
    "BATCH_KIND_TRIAL",
    "BATCH_STATES",
    "BATCH_STATE_CANCEL_REQUESTED",
    "BATCH_STATE_LIST_SQL",
    "BATCH_STATE_SUBMITTED",
    "CREATABLE_BATCH_KINDS",
    "CreateBatchCommand",
    "GateChecker",
    "GateVerdict",
    "IDEMPOTENCY_SCOPE_BATCH_RETRY_PREFIX",
    "LIMIT_SOURCES",
    "LIMIT_SOURCE_GLOBAL",
    "LIMIT_SOURCE_INHERITED",
    "LIMIT_SOURCE_LIST_SQL",
    "LIMIT_SOURCE_MANUAL",
    "MAX_IDEMPOTENCY_KEY_CHARS",
    "MAX_SEQ_ATTEMPTS",
    "PROGRESS_CANCELLED",
    "PROGRESS_CANCELLING",
    "PROGRESS_FAILED",
    "PROGRESS_PARTIAL",
    "PROGRESS_QUEUED",
    "PROGRESS_RUNNING",
    "PROGRESS_STATES",
    "PROGRESS_SUCCEEDED",
    "PROGRESS_UNKNOWN",
    "PURPOSE_BATCH_BASELINE",
    "PURPOSE_BATCH_PLAN",
    "REASON_ALLOCATION_INCOMPLETE",
    "REASON_BASELINE_CHANGED",
    "REASON_BASELINE_NOT_READY",
    "REASON_BATCH_BUDGET_NOT_CONFIGURED",
    "REASON_BATCH_CANCELLED",
    "REASON_BATCH_ESTIMATE_EXCEEDS_AVAILABLE",
    "REASON_BATCH_KIND_UNKNOWN",
    "REASON_BATCH_NOT_FOUND",
    "REASON_GATE_NOT_SATISFIED",
    "REASON_GATE_NOT_WIRED",
    "REASON_IDEMPOTENCY_KEY_REQUIRED",
    "REASON_IDEMPOTENCY_KEY_REUSED",
    "REASON_LIMITS_NOT_CONFIGURED",
    "REASON_NOTHING_TO_RETRY",
    "REASON_PLAN_CHANGED",
    "REASON_PLAN_VERSION_INVALID",
    "REASON_REQUEST_IN_PROGRESS",
    "SNAPSHOT_REVISION_NO",
    "content_idempotency_key",
    "job_idempotency_key",
]
