"""模型网关核心（WP16）—— 派发 / 轮询 / 核对。

**本模块承载验收的两半**：

| 半 | 函数 | 结构性保证 |
|---|---|---|
| **LOCAL_ONLY 无外部回退** | `dispatch_call` | 判定来自 `policy.evaluate_egress`（纯函数）；否决即**抛错返回**，代码里**不存在第二条路** |
| **未知调用状态不盲重试** | `dispatch_call` | 派发前按 `(provider_id, request_key)` 查库；**查到任何既有调用就不再发出去** |

---

## `request_key` 为什么不带 `attempt_id`（**本包最关键的一个判断**）

最自然的写法是 `f"{capability}:{provider_id}:{attempt_id}"` —— 但它是**错的**，
而且错得很隐蔽：

| 情形 | attempt 变化 | 若 key 带 attempt | 后果 |
|---|---|---|---|
| 正常的同一次执行 | 不变 | 查到既有调用 | 正确（不重发） |
| 租约过期 → 回收器接手 | **变**（新 attempt） | **查不到** | **重复派发 → 重复计费** |
| 处理器崩在"已发出、未写结果"之间 → 任务重试 | **变** | **查不到** | **重复派发 → 重复计费** |

数据结构 §13.4 的原句是"**租约只防本地并发，不保证供应商已停工**；
`UNKNOWN` 不因租约过期变成可自由重试" —— 恰恰在说上面第二种情形。
所以 key 必须**跨 attempt 稳定**：它锚定的是"哪件活"，而不是"谁在干"。

于是：`request_key = f"{capability}:{provider_id}:{job_id}:r{round_no}"`，
`round_no` 默认 `0`。**要开新一轮调用，调用方必须显式传 `round_no>0`** ——
"重试"是一个需要有人负责的决定，不能是默认行为。
（"什么时候该开新一轮"属产品策略，本包不替它决定，登记 OP-133。）

## 判据的顺序：**先判策略，再查幂等**

反过来（先查幂等、后判策略）会有一个恶劣后果：一个原本被拒绝的出网调用，
如果**曾经**存在同 key 的记录，就会被"幂等命中"直接返回 ——
等于用历史记录绕过当前策略。策略必须**每一次**都重新判。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from rednote.modules.gateway.adapters import (
    CapabilityAdapter,
    validate_capability_result,
)
from rednote.modules.gateway.models import ProviderCall
from rednote.modules.gateway.policy import EgressPolicyView, evaluate_egress
from rednote.modules.gateway.repository import GatewayRepository
from rednote.modules.gateway.types import (
    CALL_IN_FLIGHT_STATES,
    CALL_POLLING,
    CALL_SUBMITTED,
    CALL_TERMINAL_STATES,
    CALL_UNKNOWN,
    EGRESS_ALLOWED,
    EGRESS_DENIED,
    REASON_CAPABILITY_UNSUPPORTED,
    REASON_PROVIDER_DISABLED,
    CapabilityRequest,
    CapabilityResult,
    EgressDecision,
    call_state_for_result,
)
from rednote.platform.errors import ApiError, ErrorCode

# `ACCEPTED` 之后若适配器没给 nextPollAt，用这个间隔兜底。
# 取 30 秒：比 SSE 的心跳（15s）长、比常见供应商的异步完成时间短。
DEFAULT_POLL_INTERVAL_SECONDS: Final[int] = 30

# 轮询一次最多捞多少条（与其它模块的分页上限一致）。
DEFAULT_POLL_BATCH: Final[int] = 100


@dataclass(frozen=True)
class CallView:
    """一次调用的不可变快照。"""

    call_id: uuid.UUID
    provider_id: uuid.UUID
    state: str
    request_key: str
    external_request_id: str | None
    submitted_at: datetime | None
    next_poll_at: datetime | None
    status_unknown_reason: str | None
    last_error_code: str | None
    request_evidence_ref: str
    response_evidence_ref: str | None
    created_at: datetime

    @property
    def is_terminal(self) -> bool:
        return self.state in CALL_TERMINAL_STATES

    @property
    def is_unknown(self) -> bool:
        return self.state == CALL_UNKNOWN

    @property
    def is_in_flight(self) -> bool:
        """已（或可能已）发出、尚未终结。**这个状态一律不得重新派发。**"""
        return self.state in CALL_IN_FLIGHT_STATES


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_request_key(
    *,
    capability: str,
    provider_id: uuid.UUID,
    job_id: uuid.UUID,
    round_no: int = 0,
) -> str:
    """构造幂等键。**格式只有一个来源**（理由见模块 docstring 与 OP-129）。

    `round_no` 默认 `0` —— 安全的那个值。想开新一轮必须显式传 `>0`。
    """
    return f"{capability}:{provider_id}:{job_id}:r{round_no}"


def _policy_view(policy: Any) -> EgressPolicyView | None:
    """ORM 策略行 → 不可变视图（判定函数不许碰 ORM）。"""
    if policy is None:
        return None
    return EgressPolicyView(
        project_id=policy.project_id,
        policy_version=policy.policy_version,
        external_policy=policy.external_policy,
        allowed_provider_ids=tuple(policy.allowed_provider_ids or ()),
        allowed_data_classes=tuple(policy.allowed_data_classes or ()),
    )


def _to_call_view(call: ProviderCall) -> CallView:
    return CallView(
        call_id=call.id,
        provider_id=call.provider_id,
        state=call.state,
        request_key=call.request_key,
        external_request_id=call.external_request_id,
        submitted_at=call.submitted_at,
        next_poll_at=call.next_poll_at,
        status_unknown_reason=call.status_unknown_reason,
        last_error_code=call.last_error_code,
        request_evidence_ref=call.request_evidence_ref,
        response_evidence_ref=call.response_evidence_ref,
        created_at=call.created_at,
    )


# ---------------------------------------------------------------------------
# 出网留痕
# ---------------------------------------------------------------------------


def record_egress(
    repo: GatewayRepository,
    *,
    request: CapabilityRequest,
    decision: EgressDecision,
    provider_id: uuid.UUID,
    provider_call_id: uuid.UUID | None,
) -> uuid.UUID:
    """写一条出网留痕（`gateway_egress`，追加且不可篡改）。

    **为什么有自己的表而不是复用 WP15 的 `audit_events`**：见
    `models.GatewayEgress` 的 docstring —— 审计有 `hash_pair_consistent`
    （before/after 必须成对），那是为"**变更**记录"设的；出网留痕是**事件**，
    没有 before。硬塞进去只能靠伪造一个 before 来满足约束，而那是
    "让数据适配约束"的反面做法（ISS-47）。

    `provider_call_id` 在**被拒时为空** —— 策略判定发生在派发之前，
    那时确实没有调用可指；`decision` 与 `blocked_reason` 记录了完整的原因。
    """
    row = repo.insert_egress(
        project_id=request.project_id,
        provider_id=provider_id,
        provider_call_id=provider_call_id,
        capability=request.capability,
        data_classes=list(request.data_classes),
        decision=EGRESS_ALLOWED if decision.allowed else EGRESS_DENIED,
        blocked_reason=None if decision.allowed else decision.reason,
        policy_version=decision.policy_version or request.policy_version,
        prompt_version=request.prompt_version,
        trace_id=request.trace_id or request.request_id,
        payload_digest=request.context_digest,
    )
    repo.flush()
    return row.id


# ---------------------------------------------------------------------------
# 派发
# ---------------------------------------------------------------------------


def egress_error(decision: EgressDecision) -> ApiError:
    """把否决结论折成协议错误（原因进 `details`，便于前端与排障定位）。

    **为什么由调用方调用而不是在 `dispatch_call` 里抛**：见 `DispatchResult`
    的 docstring —— 被拒时要先让留痕提交，再报错。
    """
    details: dict[str, Any] = {"reason": decision.reason}
    if decision.detail:
        details.update(decision.detail)
    if decision.reason == REASON_CAPABILITY_UNSUPPORTED:
        # "能力不支持"不是"出网策略不允许" —— 协议 §1.2 里没有专门的错误码，
        # 且 ErrorCode 必须与协议 §1.2 逐条一致（有测试核对），因此不自创。
        # 用 422 + `reason` 表达：它是**请求与供应商不匹配**，重试无用。
        return ApiError(
            ErrorCode.VALIDATION_FAILED,
            "该供应商不支持所请求的能力。",
            details=details,
        )
    if decision.reason == REASON_PROVIDER_DISABLED:
        return ApiError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "该供应商已停用。",
            details=details,
        )
    # 其余（no_policy / project_local_only / provider_not_allowed / data_class_not_allowed）
    # 全是出网策略问题 → 403 EXTERNAL_POLICY_DENIED（协议 §1.2）。
    return ApiError(
        ErrorCode.EXTERNAL_POLICY_DENIED,
        "当前项目策略不允许该外部访问。",
        details=details,
    )


@dataclass(frozen=True)
class DispatchResult:
    """一次派发的结果。

    三种情形，用**返回值**表达（而不是异常），理由是**留痕必须活下来**：

    | 情形 | `dispatched` | `call` | `denial` |
    |---|---|---|---|
    | 真的发出去了 | `True` | 本条调用 | `None` |
    | 幂等命中 / 并发抢先 | `False` | 既有调用 | `None` |
    | **被出网策略拒绝** | `False` | `None` | 否决结论 |

    **被拒为什么用返回值而不是抛异常**：被拒时要写一条 DENIED 留痕
    （ADR-10 的违规判定口径是"任何一条 `LOCAL_ONLY` 项目的出网记录 = 违规"，
    因此"曾经试图出网并被拒"本身就是需要留档的安全事件）。
    而抛异常会让外层事务回滚，**把刚写的留痕一起撤销** ——
    那正是 WP12/ISS-30 踩过的坑（"补偿动作写在会抛异常的路径上"）。
    所以这里返回结论、让事务**正常提交**，调用方提交之后再调
    `egress_error(denial)` 把它折成协议错误。见 ISS-48。
    """

    call: CallView | None
    dispatched: bool
    egress_id: uuid.UUID | None
    denial: EgressDecision | None = None

    @property
    def denied(self) -> bool:
        return self.denial is not None

    @property
    def replayed(self) -> bool:
        """没发出去，但手上有既有调用（幂等命中或并发抢先）。"""
        return not self.dispatched and self.call is not None


def dispatch_call(
    repo: GatewayRepository,
    *,
    request: CapabilityRequest,
    adapter: CapabilityAdapter,
    round_no: int = 0,
) -> DispatchResult:
    """派发一次能力请求。

    顺序（**不能换**）：

    1. 解析 model_profile → provider（拿 `network_mode` / 能力 / 状态）；
    2. **判定出网策略**（纯函数）。否决 → 写 DENIED 留痕 + 抛错，**到此为止**；
    3. 按 `request_key` **查既有调用**。查到 → 直接返回，**不发**；
    4. 落 `PREPARED`（**先把凭据持久化**）→ 写 ALLOWED 留痕 → 调 `adapter.submit`；
    5. 校验结果 + 迁移状态。

    第 4 步的顺序是数据结构 §13.4 的原句要求："供应商调用前持久化 request_key"。
    先发后记的话，进程在两者之间崩溃就永远丢了这次调用的凭据。
    """
    profile = repo.get_model_profile(request.model_profile_id)
    if profile is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "模型档案不存在。",
            details={"modelProfileId": str(request.model_profile_id)},
        )
    provider = repo.get_provider(profile.provider_id)
    if provider is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "供应商不存在。",
            details={"providerId": str(profile.provider_id)},
        )

    # 1) 策略判定。**每一次调用都重新判**，不吃历史结论。
    decision = evaluate_egress(
        policy=_policy_view(repo.get_policy(request.project_id)),
        provider_id=provider.id,
        network_mode=provider.network_mode,
        provider_status=provider.status,
        provider_capabilities=provider.capability_types or (),
        capability=request.capability,
        data_classes=request.data_classes,
    )
    if not decision.allowed:
        # **不抛异常**：抛出去会让外层事务回滚，把这条 DENIED 留痕一起撤销。
        # 返回结论 → 事务正常提交 → 留痕生效 → 调用方（提交之后）再报错。
        egress_id = record_egress(
            repo,
            request=request,
            decision=decision,
            provider_id=provider.id,
            provider_call_id=None,
        )
        return DispatchResult(call=None, dispatched=False, egress_id=egress_id,
                              denial=decision)

    # 2) 幂等：按**跨 attempt 稳定**的键查既有调用。
    request_key = build_request_key(
        capability=request.capability,
        provider_id=provider.id,
        job_id=request.job_id,
        round_no=round_no,
    )
    existing = repo.find_call_by_request_key(
        provider_id=provider.id, request_key=request_key
    )
    if existing is not None:
        # 终态 → 回放既有结果；在途/UNKNOWN → 等它。**两种都不重新发。**
        return DispatchResult(
            call=_to_call_view(existing), dispatched=False, egress_id=None
        )

    # 3) 先持久化凭据（PREPARED），再出去。
    call, created = repo.insert_call_if_absent(
        job_attempt_id=request.attempt_id,
        provider_id=provider.id,
        request_key=request_key,
        request_evidence_ref=f"trace:{request.trace_id or request.request_id}",
    )
    repo.flush()
    if not created:
        # 并发下别人抢先插了同 key —— 同样不发。
        return DispatchResult(
            call=_to_call_view(call), dispatched=False, egress_id=None
        )

    # 4) 留痕 —— **只在真的出网时写**。
    #
    # 本地供应商的调用不出网，因此**不能**写 `gateway_egress`：
    # ADR-10 的违规判定口径是"任何一条 LOCAL_ONLY 项目的**出网记录** = 该次调用违规"。
    # 若把本地调用也记成出网记录，那条口径会退化成"本地调用也算违规"，
    # 而模式 B（全本地）下系统将寸步难行 —— 留痕本身反而成了错误来源。
    egress_id: uuid.UUID | None = None
    if not decision.is_local:
        egress_id = record_egress(
            repo,
            request=request,
            decision=decision,
            provider_id=provider.id,
            provider_call_id=call.id,
        )

    # 5) 真出去了。
    result = adapter.submit(request)
    validate_capability_result(result)
    _apply_result(repo, call_id=call.id, result=result, from_states=["PREPARED"])
    repo.flush()

    refreshed = repo.get_call(call.id)
    assert refreshed is not None
    return DispatchResult(
        call=_to_call_view(refreshed), dispatched=True, egress_id=egress_id
    )


def _apply_result(
    repo: GatewayRepository,
    *,
    call_id: uuid.UUID,
    result: CapabilityResult,
    from_states: list[str],
) -> bool:
    """把适配器结果落到 `provider_calls`（条件迁移）。"""
    target = call_state_for_result(result.state)
    next_poll_at = result.next_poll_at
    if target == CALL_POLLING and next_poll_at is None:
        next_poll_at = _now() + timedelta(seconds=DEFAULT_POLL_INTERVAL_SECONDS)
    return repo.transition_call(
        call_id=call_id,
        from_states=from_states,
        to_state=target,
        external_request_id=result.external_request_id,
        submitted_at=_now() if result.external_request_id else None,
        next_poll_at=next_poll_at,
        status_unknown_reason=(
            result.error_code or "provider_state_unknown"
            if target == CALL_UNKNOWN
            else None
        ),
        last_error_code=result.error_code,
    )


# ---------------------------------------------------------------------------
# 轮询与核对
# ---------------------------------------------------------------------------


def poll_call(
    repo: GatewayRepository, *, call_id: uuid.UUID, adapter: CapabilityAdapter
) -> CallView:
    """轮询一次在途调用。

    **只接受 `SUBMITTED` / `POLLING`**：终态不该被轮询（那是无意义的重复请求），
    `UNKNOWN` 也不该 —— 它走 `reconcile_call`（人工核对）。
    把 UNKNOWN 也允许轮询会给出一个"自动恢复了"的假象，
    而 §13.4 明确要求它**不因时间流逝而复位**。
    """
    call = repo.get_call(call_id)
    if call is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "调用记录不存在。",
            details={"providerCallId": str(call_id)},
        )
    if call.state not in {CALL_SUBMITTED, CALL_POLLING}:
        raise ApiError(
            ErrorCode.INVALID_TRANSITION,
            "当前状态不允许轮询。",
            details={"providerCallId": str(call_id), "state": call.state},
        )
    if not call.external_request_id:
        # 没有 externalRequestId 就查不了 —— 这是适配器契约被破坏的信号。
        raise ApiError(
            ErrorCode.INVALID_TRANSITION,
            "该调用缺少外部请求标识，无法轮询。",
            details={"providerCallId": str(call_id)},
        )

    result = adapter.query(external_request_id=call.external_request_id)
    validate_capability_result(result)
    _apply_result(
        repo,
        call_id=call_id,
        result=result,
        from_states=[CALL_SUBMITTED, CALL_POLLING],
    )
    repo.flush()
    refreshed = repo.get_call(call_id)
    assert refreshed is not None
    return _to_call_view(refreshed)


def reconcile_call(
    repo: GatewayRepository,
    *,
    call_id: uuid.UUID,
    resolution_state: str,
    reason: str,
    response_evidence_ref: str | None = None,
) -> CallView:
    """人工核对一条 `UNKNOWN` 调用，落到终态。

    **`UNKNOWN` 的唯一出口**（验收第二半的收尾）。两条硬约束：

    1. **只接受终态作为 `resolution_state`**。允许回到 `SUBMITTED`/`POLLING`
       等于"通过核对重新发一次" —— 而那正是 UNKNOWN 要防的事；
    2. 只能从 `UNKNOWN` 出发。已经是终态的调用不需要（也不允许）再核对。

    `reason` 是必填的：核对是**有人做了判断**，判断依据必须留下来。
    协议 §10 的 `POST /jobs/{id}/reconciliations` 收 `evidenceRef` 与 `resolution`，
    对应这里的 `response_evidence_ref` 与 `resolution_state`。
    """
    if resolution_state not in CALL_TERMINAL_STATES:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "核对只能落到终态（SUCCEEDED / FAILED）。",
            details={"resolutionState": resolution_state},
        )
    if not reason or not reason.strip():
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "核对必须给出依据。",
            details={"field": "reason", "reason": "reason_required"},
        )

    call = repo.get_call(call_id)
    if call is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "调用记录不存在。",
            details={"providerCallId": str(call_id)},
        )
    if call.state != CALL_UNKNOWN:
        raise ApiError(
            ErrorCode.INVALID_TRANSITION,
            "只有状态不明的调用需要核对。",
            details={"providerCallId": str(call_id), "state": call.state},
        )

    changed = repo.transition_call(
        call_id=call_id,
        from_states=[CALL_UNKNOWN],
        to_state=resolution_state,
        response_evidence_ref=response_evidence_ref,
        # 保留 status_unknown_reason：它是"曾经不明过"的历史，
        # 核对完成后仍然需要（排除故障时要能看到那次不明的原因）。
    )
    repo.flush()
    if not changed:  # pragma: no cover - 前置校验已保证单条路径；并发下才可能为 False
        raise ApiError(
            ErrorCode.INVALID_TRANSITION,
            "核对时该调用已被其它操作改动，请重新读取。",
            details={"providerCallId": str(call_id)},
        )
    refreshed = repo.get_call(call_id)
    assert refreshed is not None
    return _to_call_view(refreshed)


def list_due_polls(
    repo: GatewayRepository, *, limit: int = DEFAULT_POLL_BATCH
) -> list[CallView]:
    """到点该轮询的调用（部署层按周期调用它 —— 本包不自带循环）。"""
    return [
        _to_call_view(row)
        for row in repo.list_due_polls(now=_now(), limit=limit)
    ]


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "CallView",
    "DispatchResult",
    "build_request_key",
    "dispatch_call",
    "egress_error",
    "list_due_polls",
    "poll_call",
    "reconcile_call",
    "record_egress",
]
