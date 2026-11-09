"""能力适配器接口、注册表与结果校验（WP16）。

对应协议 §12.1 的原句：

> 适配器需实现 submit、query、可选 cancel；不支持能力明确返回 CAPABILITY_UNAVAILABLE。

**为什么适配器是"协议 + 注册表"而不是"if provider == 'x' 的分支"**：
研发计划 04 §9 的原句是「AI／媒体接口在 WP16 固定输入输出，**模型替换不改变项目业务接口**」。
分支写法会把这个承诺反过来 —— 每加一个供应商就要改业务代码，
而"改业务代码"意味着回归范围覆盖到内容生成链路。

**本包只交付接口 + 注册表 + 一个可配置的桩**（`StubAdapter`）。
真实适配器（文案/图片/Live/embedding）需要密钥、出网白名单与供应商合同，
三者本机都没有 —— 交付一个"看起来能调"的假适配器，比不交付更糟：
它会让"Live 链路已验证"这句话变成假的。登记 OP-130。

---

## 校验的是"结构"而不是"业务对错"

`validate_capability_result` 只回答"这个结果能不能被系统安全地接受"：

- 状态值是否合法；
- `ACCEPTED` 有没有 `externalRequestId`（**没有就无法轮询** ——
  那会得到一个"系统以为在等、其实永远等不到"的任务）；
- `SUCCEEDED` 有没有产出（`structuredOutput` 或 `outputFileIds` 至少一个）；
- `FAILED` 有没有 `errorCode`。

它**不**回答"模型说的对不对"（那是 WP30 的 AI 自检）。
两者混在一起会让"内容质量没过"和"适配器返回了畸形结果"都变成同一种失败，
而前者该重试、后者该修代码。协议 §13 第 4 条要求"模型返回 JSON、供应商响应…
使用单独 Schema 校验，不与浏览器请求 DTO 混用" —— 本模块就是那个"单独 Schema"。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from rednote.modules.gateway.types import (
    ADAPTER_ACCEPTED,
    ADAPTER_FAILED,
    ADAPTER_RESULT_STATES,
    ADAPTER_SUCCEEDED,
    COST_STATUSES,
    CapabilityRequest,
    CapabilityResult,
)


class AdapterContractError(ValueError):
    """适配器返回了不满足协议 §12.1 的结果。

    **它是一个"缺陷"信号，不是"业务失败"**：业务失败走
    `CapabilityResult(state=FAILED)`。抛这个异常意味着适配器实现写错了
    （返回了非法状态、`ACCEPTED` 却没有 `externalRequestId` 等）——
    此时应让任务以 500 暴露，而不是伪装成"外部调用失败"（那会触发重试，
    而重试一个"适配器 bug"只会重复报错）。
    """


@runtime_checkable
class CapabilityAdapter(Protocol):
    """能力适配器（协议 §12.1）。"""

    #: 该适配器负责的能力（`types.CAPABILITIES` 之一）。
    capability: str

    def submit(self, request: CapabilityRequest) -> CapabilityResult:
        """提交一次请求。

        **超时必须返回 `state=UNKNOWN`，不能抛异常**：
        抛异常会被上层当成"没发出去"从而允许重试，而超时的语义恰恰是
        "可能已经发出去了"（数据结构 §13.4 的口径）。
        """
        ...

    def query(self, *, external_request_id: str) -> CapabilityResult:
        """查询一次已提交请求的当前状态（`ACCEPTED` 之后的轮询）。"""
        ...

    def cancel(self, *, external_request_id: str) -> None:
        """可选：取消。不支持的实现应抛 `NotImplementedError`（由调用方决定是否调用）。"""
        ...


def validate_capability_result(result: CapabilityResult) -> None:
    """校验结果结构；不合法即抛 `AdapterContractError`。"""
    if result.state not in ADAPTER_RESULT_STATES:
        raise AdapterContractError(
            f"非法结果状态 {result.state!r}；合法值：{list(ADAPTER_RESULT_STATES)}"
        )
    if result.cost_status not in COST_STATUSES:
        raise AdapterContractError(
            f"非法计费状态 {result.cost_status!r}；合法值：{list(COST_STATUSES)}"
        )
    if result.state == ADAPTER_ACCEPTED and not result.external_request_id:
        # 没有 externalRequestId 就**无法轮询** —— 任务会永远停在"等外部"，
        # 而外部其实早就受理了。这类缺陷只在"该回来的时候没回来"时暴露。
        raise AdapterContractError("ACCEPTED 必须带 externalRequestId（否则无法轮询）")
    if result.state == ADAPTER_SUCCEEDED and not (
        result.structured_output is not None or result.output_file_ids
    ):
        raise AdapterContractError(
            "SUCCEEDED 必须带 structuredOutput 或 outputFileIds 之一"
        )
    if result.state == ADAPTER_FAILED and not result.error_code:
        raise AdapterContractError("FAILED 必须带 errorCode")


def validate_structured_output(
    output: Mapping[str, Any] | None, *, required_keys: Iterable[str]
) -> None:
    """轻量 schema 校验：`structuredOutput` 必须是对象且含全部必需键。

    **刻意不做类型/取值校验**：那属于"模型输出质量"（WP30 AI 自检）。
    网关只保证"下游拿到的形状是可预期的"，否则下游每一步都要写防御性代码。
    """
    if output is None:
        raise AdapterContractError("structuredOutput 为空，无法校验")
    if not isinstance(output, Mapping):
        raise AdapterContractError("structuredOutput 必须是对象")
    missing = [key for key in required_keys if key not in output]
    if missing:
        raise AdapterContractError(f"structuredOutput 缺少必需键：{missing}")


@dataclass(frozen=True)
class AdapterRegistry:
    """能力 → 适配器。

    **不可变**（`register` 返回新实例）：注册发生在应用装配期，
    不可变可以避免"测试里注册了一个适配器，影响后续测试"的顺序依赖
    —— 与 WP13 的 `TaskHandlerRegistry` 同一判断。
    """

    _adapters: Mapping[str, CapabilityAdapter] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> AdapterRegistry:
        return cls(_adapters={})

    def register(self, adapter: CapabilityAdapter) -> AdapterRegistry:
        """按适配器自报的 `capability` 注册（能力名只有一个来源：适配器自己）。"""
        return AdapterRegistry(_adapters={**self._adapters, adapter.capability: adapter})

    def adapter_for(self, capability: str) -> CapabilityAdapter | None:
        return self._adapters.get(capability)

    def registered_capabilities(self) -> frozenset[str]:
        return frozenset(self._adapters)


# ---------------------------------------------------------------------------
# 桩适配器（本机与测试用）
# ---------------------------------------------------------------------------


@dataclass
class StubAdapter:
    """可脚本化的假适配器 —— **只为验证网关语义而存在**。

    **它不是"假供应商"**：WP13 已有一个 `FAKE_PROVIDER` 任务处理器
    （研发计划 04 明写"假供应商只用于此阶段测试"）。两者的分工是：
    `FAKE_PROVIDER` 验证**任务框架**（不碰适配器），
    `StubAdapter` 验证**网关**（策略、幂等、状态机、结果校验）。

    名字里带 `Stub` 是刻意的：它在任何清单里都一眼可辨，
    不会被误当成真实供应商实现。
    """

    capability: str
    #: 依次返回的 submit 结果；用完后回落到 `default_result`。
    submit_results: Sequence[CapabilityResult] = ()
    #: 依次返回的 query 结果；用完后回落到 `submit_results` 的最后一个（或 default）。
    query_results: Sequence[CapabilityResult] = ()
    default_result: CapabilityResult | None = None
    #: 若设置，`submit` 直接抛它（用于验证"适配器抛异常"的路径）。
    raise_on_submit: Exception | None = None
    #: 若设置，`submit` 返回这个"畸形结果"（用于验证结果校验会拦下）。
    malformed_result: CapabilityResult | None = None
    #: 调用计数（测试断言"被拒时一次都没调用"用它）。
    submit_call_count: int = 0
    query_call_count: int = 0
    cancel_call_count: int = 0
    submitted_keys: list[str] = field(default_factory=list)
    submitted_ids: list[str] = field(default_factory=list)

    def submit(self, request: CapabilityRequest) -> CapabilityResult:
        self.submit_call_count += 1
        self.submitted_ids.append(request.request_id)
        self.submitted_keys.append(request.idempotency_key)
        if self.raise_on_submit is not None:
            raise self.raise_on_submit
        if self.malformed_result is not None:
            return self.malformed_result
        index = self.submit_call_count - 1
        if index < len(self.submit_results):
            return self.submit_results[index]
        return self._fallback()

    def query(self, *, external_request_id: str) -> CapabilityResult:
        self.query_call_count += 1
        index = self.query_call_count - 1
        if index < len(self.query_results):
            return self.query_results[index]
        if self.query_results:
            return self.query_results[-1]
        return self._fallback()

    def cancel(self, *, external_request_id: str) -> None:
        del external_request_id
        self.cancel_call_count += 1

    def _fallback(self) -> CapabilityResult:
        if self.default_result is not None:
            return self.default_result
        if self.submit_results:
            return self.submit_results[-1]
        return CapabilityResult(state=ADAPTER_SUCCEEDED, structured_output={})


__all__ = [
    "AdapterContractError",
    "AdapterRegistry",
    "CapabilityAdapter",
    "StubAdapter",
    "validate_capability_result",
    "validate_structured_output",
]
