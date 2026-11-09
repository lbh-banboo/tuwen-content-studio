"""WP16 的不可变值类型与受控取值域。

**为什么单独一个模块**：这些类型同时被 `policy.py`（判定）、`adapters.py`（适配器）、
`repository.py`（存储）与 `service.py`（编排）使用。若定义在 service 里，
其它三个都要反向导入 service —— 而本模块的分层刻意是单向的：

    types ← policy
    types ← adapters
    types ← repository
    types ← service
    types ← handlers

与 WP11/WP12/WP13 的做法一致：**跨层传递的值用 frozen dataclass 而不是 ORM 对象**
（ORM 实例绑在 session 上，session 一关就 `DetachedInstanceError`）。

---

**两套状态值刻意分开，不做成一个**：

| 集合 | 谁的值 | 取值 |
|---|---|---|
| `CALL_STATES` | `provider_calls.state`（**落库**的业务状态） | PREPARED / SUBMITTED / POLLING / SUCCEEDED / FAILED / UNKNOWN |
| `ADAPTER_RESULT_STATES` | 适配器返回的 `CapabilityResult.state`（协议 §12.1 原样） | SUCCEEDED / ACCEPTED / FAILED / UNKNOWN |

它们**不是同一套**：协议 §12.1 里适配器返回 `ACCEPTED`（"已受理，去轮询"），
而落库状态是 `POLLING`。合并成一套会迫使某一方改名 ——
改协议的名字是越界，改落库的名字会让 `provider_calls.state` 与数据结构 §12 对不上。
因此保留两套，并提供一个**显式**的映射函数（`call_state_for_result`）。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

# ---------------------------------------------------------------------------
# 供应商网络模式（数据结构 §11：network_mode LOCAL/EXTERNAL）
# ---------------------------------------------------------------------------

NETWORK_LOCAL: Final[str] = "LOCAL"
NETWORK_EXTERNAL: Final[str] = "EXTERNAL"
NETWORK_MODES: Final[tuple[str, ...]] = (NETWORK_LOCAL, NETWORK_EXTERNAL)

# 供应商状态。
PROVIDER_ACTIVE: Final[str] = "ACTIVE"
PROVIDER_DISABLED: Final[str] = "DISABLED"
PROVIDER_STATUSES: Final[tuple[str, ...]] = (PROVIDER_ACTIVE, PROVIDER_DISABLED)

# ---------------------------------------------------------------------------
# 能力类型（协议 §12.1 的 capability 取值域，逐条照抄）
# ---------------------------------------------------------------------------

CAPABILITY_TEXT_STRUCTURED: Final[str] = "TEXT_STRUCTURED"
CAPABILITY_VISION_CHECK: Final[str] = "VISION_CHECK"
CAPABILITY_STATIC_IMAGE: Final[str] = "STATIC_IMAGE"
CAPABILITY_LIVE_MOTION: Final[str] = "LIVE_MOTION"
CAPABILITY_EMBEDDING: Final[str] = "EMBEDDING"
CAPABILITY_RERANK: Final[str] = "RERANK"

CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        CAPABILITY_TEXT_STRUCTURED,
        CAPABILITY_VISION_CHECK,
        CAPABILITY_STATIC_IMAGE,
        CAPABILITY_LIVE_MOTION,
        CAPABILITY_EMBEDDING,
        CAPABILITY_RERANK,
    }
)

# 出网矩阵 E-4：**embedding 默认不走外呼**（须单独列入白名单并重新审批）。
# 落地方式不做成单独的 if 分支，而是靠"数据类别白名单"：embedding 出网要求
# 策略的 `allowed_data_classes` 里显式含 `EMBEDDING` —— 缺省策略里没有它，
# 于是"默认不走外呼"自然成立，且**只有一处判据**（`policy.evaluate_egress` 第 7 步）。

# ---------------------------------------------------------------------------
# 数据类别（ADR-10 出网矩阵 E-1 ~ E-6 的左侧列）
# ---------------------------------------------------------------------------

DATA_CLASS_TEXT_STRUCTURED: Final[str] = "TEXT_STRUCTURED"
DATA_CLASS_STATIC_IMAGE: Final[str] = "STATIC_IMAGE"
DATA_CLASS_LIVE_MOTION: Final[str] = "LIVE_MOTION"
DATA_CLASS_EMBEDDING: Final[str] = "EMBEDDING"
DATA_CLASS_COLLECTION: Final[str] = "COLLECTION"

DATA_CLASSES: Final[frozenset[str]] = frozenset(
    {
        DATA_CLASS_TEXT_STRUCTURED,
        DATA_CLASS_STATIC_IMAGE,
        DATA_CLASS_LIVE_MOTION,
        DATA_CLASS_EMBEDDING,
        DATA_CLASS_COLLECTION,
    }
)

# E-5：**禁止出网**的数据类别。网关侧不存在对应通道即视为合规 ——
# 这里显式列出来是为了让"禁止清单"在代码里可被引用与断言，
# 而不是只写在 ADR 的表格里。
FORBIDDEN_EGRESS_DATA_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "KNOWLEDGE_FULLTEXT",
        "COST_LEDGER",
        "AUDIT_LOG",
        "VERSION_SNAPSHOT",
        "BACKUP_ARCHIVE",
    }
)

# ---------------------------------------------------------------------------
# 项目出网策略（capability_policies.external_policy）
# ---------------------------------------------------------------------------

EXTERNAL_POLICY_ALLOW: Final[str] = "ALLOW_EXTERNAL"
EXTERNAL_POLICY_LOCAL_ONLY: Final[str] = "LOCAL_ONLY"
EXTERNAL_POLICIES: Final[tuple[str, ...]] = (
    EXTERNAL_POLICY_ALLOW,
    EXTERNAL_POLICY_LOCAL_ONLY,
)

# ---------------------------------------------------------------------------
# provider_calls.state（数据结构 §12「外部调用」）
# ---------------------------------------------------------------------------

CALL_PREPARED: Final[str] = "PREPARED"
CALL_SUBMITTED: Final[str] = "SUBMITTED"
CALL_POLLING: Final[str] = "POLLING"
CALL_SUCCEEDED: Final[str] = "SUCCEEDED"
CALL_FAILED: Final[str] = "FAILED"
CALL_UNKNOWN: Final[str] = "UNKNOWN"

CALL_STATES: Final[tuple[str, ...]] = (
    CALL_PREPARED,
    CALL_SUBMITTED,
    CALL_POLLING,
    CALL_SUCCEEDED,
    CALL_FAILED,
    CALL_UNKNOWN,
)

# 终态：不会再变。`UNKNOWN` **不在其中** —— 它等核对，是一个"待办"状态，
# 而正是"把它当成终态可以重来"和"把它当成非终态可以重发"这两种误解
# 会造成重复计费。
CALL_TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {CALL_SUCCEEDED, CALL_FAILED}
)

# 在途：已经（或可能已经）把请求发出去了。**这些状态一律不得重新派发**。
# 验收第二半「未知调用状态不盲重试」的判据就是它。
CALL_IN_FLIGHT_STATES: Final[frozenset[str]] = frozenset(
    {CALL_PREPARED, CALL_SUBMITTED, CALL_POLLING, CALL_UNKNOWN}
)

# ---------------------------------------------------------------------------
# 适配器返回的状态（协议 §12.1 原样）
# ---------------------------------------------------------------------------

ADAPTER_SUCCEEDED: Final[str] = "SUCCEEDED"
ADAPTER_ACCEPTED: Final[str] = "ACCEPTED"
ADAPTER_FAILED: Final[str] = "FAILED"
ADAPTER_UNKNOWN: Final[str] = "UNKNOWN"

ADAPTER_RESULT_STATES: Final[tuple[str, ...]] = (
    ADAPTER_SUCCEEDED,
    ADAPTER_ACCEPTED,
    ADAPTER_FAILED,
    ADAPTER_UNKNOWN,
)

# 计费状态（协议 §12.1：ESTIMATED / CONFIRMED / PENDING）。
COST_ESTIMATED: Final[str] = "ESTIMATED"
COST_CONFIRMED: Final[str] = "CONFIRMED"
COST_PENDING: Final[str] = "PENDING"
COST_STATUSES: Final[tuple[str, ...]] = (COST_ESTIMATED, COST_CONFIRMED, COST_PENDING)

# ---------------------------------------------------------------------------
# 出网判定结论
# ---------------------------------------------------------------------------

EGRESS_ALLOWED: Final[str] = "ALLOWED"
EGRESS_DENIED: Final[str] = "DENIED"
EGRESS_DECISIONS: Final[tuple[str, ...]] = (EGRESS_ALLOWED, EGRESS_DENIED)

# 否决原因（**每一个都必须有名字**：它们会进留痕、进错误 details、进测试断言）。
REASON_LOCAL_PROVIDER: Final[str] = "local_provider_no_egress"
REASON_CAPABILITY_UNSUPPORTED: Final[str] = "capability_unsupported"
REASON_NO_POLICY: Final[str] = "no_policy_default_deny"
REASON_PROJECT_LOCAL_ONLY: Final[str] = "project_local_only"
REASON_PROVIDER_NOT_ALLOWED: Final[str] = "provider_not_allowed"
REASON_DATA_CLASS_NOT_ALLOWED: Final[str] = "data_class_not_allowed"
REASON_PROVIDER_DISABLED: Final[str] = "provider_disabled"


@dataclass(frozen=True)
class CapabilityRequest:
    """一次能力请求（协议 §12.1 的 `CapabilityRequest`，字段逐条对应）。

    **为什么带 `context_digest` 而不是 `context`**：网关要留痕（"送出了什么类别的字段"），
    但**不留正文**。因此调用方传进来的上下文以**规范化摘要**形式存在
    （复用 WP11 的哈希口径，与 WP15 的审计哈希同算法）。
    这样即使有人误把正文塞进这个字段，"库里存的也只是 64 个字符"。
    """

    request_id: str
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    project_id: uuid.UUID
    capability: str
    model_profile_id: uuid.UUID
    output_schema_version: int
    policy_version: str
    idempotency_key: str
    # 本次出网的数据类别（ADR-10 出网矩阵的左侧列）。
    data_classes: tuple[str, ...] = ()
    # 出网内容的规范化摘要（不是正文）。
    context_digest: str | None = None
    # 提示词版本（ADR-10 留痕要求里的"提示词版本"）。
    prompt_version: str | None = None
    trace_id: str | None = None


@dataclass(frozen=True)
class CapabilityResult:
    """适配器返回的结果（协议 §12.1 的 `CapabilityResult`，字段逐条对应）。

    `state` 用**适配器的**取值域（`ADAPTER_RESULT_STATES`），不是落库状态 ——
    落库状态由 `call_state_for_result` 映射，见模块 docstring。
    """

    state: str
    external_request_id: str | None = None
    next_poll_at: datetime | None = None
    output_file_ids: tuple[uuid.UUID, ...] = ()
    structured_output: Mapping[str, Any] | None = None
    usage: tuple[tuple[str, str], ...] = ()
    cost_amount: str | None = None
    cost_currency: str = "CNY"
    cost_status: str = COST_PENDING
    error_code: str | None = None


@dataclass(frozen=True)
class EgressDecision:
    """出网判定结论。

    `allowed=True` 时 `reason` 为 `REASON_LOCAL_PROVIDER`（本地调用，不出网）
    或 `EGRESS_ALLOWED`（确实要出网）。
    """

    allowed: bool
    reason: str
    policy_version: str | None = None
    detail: Mapping[str, Any] | None = None

    @property
    def is_local(self) -> bool:
        """允许，但因为是本地供应商（**没有发生出网**）。"""
        return self.allowed and self.reason == REASON_LOCAL_PROVIDER


def call_state_for_result(result_state: str) -> str:
    """适配器结果状态 → `provider_calls.state`。

    | 适配器（协议 §12.1） | 落库（数据结构 §12） | 说明 |
    |---|---|---|
    | `SUCCEEDED` | `SUCCEEDED` | 终态 |
    | `ACCEPTED` | `POLLING` | 已受理，去轮询 |
    | `FAILED` | `FAILED` | 终态 |
    | `UNKNOWN` | `UNKNOWN` | **待核对**，不是终态 |
    """
    mapping = {
        ADAPTER_SUCCEEDED: CALL_SUCCEEDED,
        ADAPTER_ACCEPTED: CALL_POLLING,
        ADAPTER_FAILED: CALL_FAILED,
        ADAPTER_UNKNOWN: CALL_UNKNOWN,
    }
    try:
        return mapping[result_state]
    except KeyError as exc:  # pragma: no cover - 由 validate_capability_result 先拦
        raise ValueError(f"未知的适配器结果状态: {result_state}") from exc


__all__ = [
    "ADAPTER_ACCEPTED",
    "ADAPTER_FAILED",
    "ADAPTER_RESULT_STATES",
    "ADAPTER_SUCCEEDED",
    "ADAPTER_UNKNOWN",
    "CAPABILITIES",
    "CAPABILITY_EMBEDDING",
    "CAPABILITY_LIVE_MOTION",
    "CAPABILITY_RERANK",
    "CAPABILITY_STATIC_IMAGE",
    "CAPABILITY_TEXT_STRUCTURED",
    "CAPABILITY_VISION_CHECK",
    "CALL_FAILED",
    "CALL_IN_FLIGHT_STATES",
    "CALL_POLLING",
    "CALL_PREPARED",
    "CALL_STATES",
    "CALL_SUBMITTED",
    "CALL_SUCCEEDED",
    "CALL_TERMINAL_STATES",
    "CALL_UNKNOWN",
    "COST_CONFIRMED",
    "COST_ESTIMATED",
    "COST_PENDING",
    "COST_STATUSES",
    "CapabilityRequest",
    "CapabilityResult",
    "DATA_CLASSES",
    "DATA_CLASS_COLLECTION",
    "DATA_CLASS_EMBEDDING",
    "DATA_CLASS_LIVE_MOTION",
    "DATA_CLASS_STATIC_IMAGE",
    "DATA_CLASS_TEXT_STRUCTURED",
    "EGRESS_ALLOWED",
    "EGRESS_DECISIONS",
    "EGRESS_DENIED",
    "EXTERNAL_POLICIES",
    "EXTERNAL_POLICY_ALLOW",
    "EXTERNAL_POLICY_LOCAL_ONLY",
    "EgressDecision",
    "FORBIDDEN_EGRESS_DATA_CLASSES",
    "NETWORK_EXTERNAL",
    "NETWORK_LOCAL",
    "NETWORK_MODES",
    "PROVIDER_ACTIVE",
    "PROVIDER_DISABLED",
    "PROVIDER_STATUSES",
    "REASON_CAPABILITY_UNSUPPORTED",
    "REASON_DATA_CLASS_NOT_ALLOWED",
    "REASON_LOCAL_PROVIDER",
    "REASON_NO_POLICY",
    "REASON_PROJECT_LOCAL_ONLY",
    "REASON_PROVIDER_DISABLED",
    "REASON_PROVIDER_NOT_ALLOWED",
    "call_state_for_result",
]
