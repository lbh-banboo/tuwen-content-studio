"""WP16 接缝：把一次生成请求派给模型网关（R7 / R8 的落点）。

## 本文件的形状从哪来

说明文档 §1.4 第 3 条的原文——`brief/decomposer.py` 的 `GatewayDecomposer`
是**全仓唯一**走 `TEXT_STRUCTURED` 的真实实现，它的形态是**本包必须复用的**：

| 该实现的取舍 | 本文件怎么做 |
|---|---|
| ① 本地导入 `gateway.service.dispatch_call`（避免顶层拖着网关依赖树） | 照抄（R7） |
| ② `context_digest` 传**提示词的规范化摘要**而不是正文（网关留痕不留正文） | 照抄 |
| ③ **无降级分支**——`denied` 即抛（ADR-10 明令无外部回退） | 照抄 |
| ④ `state != "SUCCEEDED"` **一律不当成功**（`POLLING`/`PREPARED`/`UNKNOWN` 都不产出） | 照抄（R8） |

**两处有意偏离，理由逐步说明**（偏离不是随意，是范本那两条路各有问题）。

---

## 偏离一：结构化输出的取法（★ 本文件最关键的一处）

### 范本怎么取的，以及为什么那条路走不通

`brief/decomposer.py` 的 `_structured_output_for_call` 写的是：

```python
getter = getattr(repo, "get_structured_output", None)
if getter is None:
    raise DecomposerUnavailable("……这是装配缺口……")
return getter(call_id)
```

本会话实测（逐文件 `grep` + 逐列核对）：

| 事实 | 实测结论 |
|---|---|
| `GatewayRepository` 有 `get_structured_output` 吗 | **没有**。它的全部方法里没有这个入口 |
| 全仓有别的实现吗 | **没有**。除范本这一处 `getattr` 外，`get_structured_output` 零出现 |
| `dispatch_call` 返回的对象里有结构化输出吗 | **没有**。返回 `DispatchResult`，其 `call` 是 `CallView`，由 `_to_call_view(ProviderCall)` 构造 —— `ProviderCall` **没有** structured_output 列 |
| 那结构化输出在哪 | 只在 `CapabilityResult.structured_output` 里，而那个对象在 `dispatch_call` 内部被 `_apply_result` 消费后**没有被返回** |

结论：范本那条 `getattr` **在任何部署里都恒走 `raise` 分支**。
"抛一条装配缺口错误"看起来像防线，实际是一段**永不通过**的路径 ——
这正是本仓反复记录的 ISS-95 形态（"**声称自己有防线**比没有防线更危险"）。
本包**不复制这条路**。

### 本包怎么取：包一层记录适配器（当次事务内可取）

`adapter` 本来就是本包的入参（`dispatch_for_generation(..., adapter=...)`），
而 `dispatch_call` 内部正是靠 `adapter.submit(request)` 拿到
`CapabilityResult` 的。因此本包在**外层**包一个薄壳
（`_RecordingAdapter`），把 `submit` 的返回值记下来：

- **不改 WP16 一行**（接缝冻结铁律）；
- **不新增表 / 列**（`provider_calls` 该不该存输出是 WP16 的设计问题，
  不是本包能替它决定的）；
- **不写"永远不通过"的分支** —— 这条路径在 `StubAdapter` 下当场可验。

**记在哪个键上**：`CapabilityRequest.idempotency_key`。它由本包派生
（见 `_idempotency_key`），在同一个事务里**唯一**（含 `task_kind`，
故 `FULL` 重做的结构 / 文案两次调用互不相同），且**请求对象里就有它** ——
不需要 provider_id（`CapabilityRequest` 里没有这个字段，
它由 `dispatch_call` 从 model_profile 解出来）。

> 试过的另一条路：按 `build_request_key(capability, provider_id, job_id, round_no)`
> 记。**走不通** —— `submit(request)` 拿到的请求里没有 `provider_id`，
> 而那个值要等 `dispatch_call` 内部查完 `model_profiles` 才有。
> 硬要拿就得在 `submit` 路径上再查一次库，并且把
> `build_request_key` 的格式在**第二处**复现（ISS-66 形态）。
> `idempotency_key` 是本包自己造的，用它当键没有这两个问题。

**代价如实登记**：结构化输出**不落库**，因此只能在**当次事务内**取。
跨事务回查必须读 `content_versions.payload`（本包已把校验后的产出写进去）。
本包**不声称**"能按 `call_id` 回查原始输出" —— 那需要 WP16 先给出落点。

### 幂等命中时为什么没有输出（这不是缺陷）

`dispatch_call` 的第 2 / 3 步（`find_call_by_request_key` 命中、
`insert_call_if_absent` 返回 `created=False`）**都不调 `adapter.submit`**。
那时本包手上确实没有输出。

但**这两种情形本来就不该产出内容**：它们意味着"这次请求的调用已经发过了"。
若在那里硬造一个版本，同一次生成被重放一次就多一个版本 ——
而"重放不产生新版本"正是幂等要保证的事（R5）。
因此本包把"没有捕获到输出"与"本次不是首发"**分开报**
（`GenerationOutput.dispatched`），并在错误信息里点名 `dispatched`。

---

## 偏离二：`capability` / `prompt_version` 用真值而不是字符串字面量

`decomposer.py` 当年写 `_CAPABILITY_TEXT_STRUCTURED = "TEXT_STRUCTURED"`
与 `_PROMPT_VERSION = "brief-wp18-v1"` 两个字面量，注释理由是
"本地导入常量会形成顶层依赖"。

本文件**就是**那个"本地导入"的位置（函数内导入），因此在函数内取常量
**不产生**顶层依赖 —— 于是本包传 `CAPABILITY_TEXT_STRUCTURED`
（网关的常量）与 `str(prompt_version_id)`（真的提示词版本 id，
而不是一个写死的版本名）。这是比范本**更强**的一点，不是偏离其取向：

- 传真的 `prompt_version_id` 让"这次调用用了哪一版提示词"在
  **网关留痕**里也成立 —— 那是 OP-160 的另一半（本包落在
  `generation_contexts` 的那一半是主口径，两者互为印证）。

---

## 其余接缝缺口的本包处置（实测确认）

### `provider_calls` 无 cost / usage 列

`billing/types.py` 的 `PendingUsage` docstring 原话：
"`CapabilityResult.usage` / `cost_amount` **没有任何落库位置**"。

本文件**不把 cost 带出去**（`dispatch_call` 的返回里本来也没有），
且 `_RecordingAdapter` **有意只记 `structured_output`，不记 usage / cost** ——
记了也没有地方落，只会诱使调用方以为"拿到真实金额了"。
结算金额口径见 `billing_bridge.settle_generation_budget` 的 docstring。

### `UNKNOWN` 的处置权不在本层（R8）

`gateway/handlers.py` 的 `outcome_for_result` 已经把
`UNKNOWN → WAITING_RECONCILIATION`。本文件**不重复这个映射**：
它只把 `state` **如实报出**（`GatewayUnavailable.state`），
由 `service` 决定"这不是成功、不产出内容、释放预占"，
由任务框架决定"等核对还是重发"。
两处都写映射 = 两个真值来源，而分叉的表现是**重发一次真实调用**
（= 再买一次，且真实结果可能已经成功）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Mapping

from rednote.modules.content.types import (
    REASON_GATEWAY_DENIED,
    REASON_GATEWAY_NOT_SUCCEEDED,
    REASON_GATEWAY_NO_OUTPUT,
)
from rednote.modules.gateway.types import CALL_TERMINAL_STATES

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期存在
    from rednote.modules.gateway.adapters import CapabilityAdapter
    from rednote.modules.gateway.repository import GatewayRepository
    from rednote.modules.gateway.types import CapabilityRequest, CapabilityResult


class GatewayUnavailable(RuntimeError):
    """这次生成拿不到可用的模型输出（**不是** `ValueError`）。

    ## 为什么它独独不继承 `ValueError`

    `prompts_bridge.PromptUnavailable` 与 `schemas.SchemaViolation`
    都继承 `ValueError`，因为调用方要用一条 `except ValueError` + `reason`
    把"提示词没配"与"模型输出不合法"分开。本类**刻意不进那一族**：

    - `ValueError` 那一族说的是"**输入不对**"——换个输入就能成功
      （换个模板、让模型重出一次）；
    - 本类说的是"**通道没通**"——`denied`（策略否决，ADR-10 说这是最终结论）、
      依赖不可用、调用尚未完成。换输入没有用。

    这正是范本 `brief/decomposer.py:79` 的 `DecomposerUnavailable(RuntimeError)`
    的取向（它的 docstring 明写"刻意不继承 `ApiError`"）。本类与它同族，
    因此 `service` 能靠**类型**（而不是 `reason`）把三类失败分开：

    | 类型 | 含义 | `service` 的处置 |
    |---|---|---|
    | `ValueError` + 提示词码 | 部署没配好 | 任务级失败，**释放预占**，不重试 |
    | `ValueError` + 输出码 | 模型输出不合法 | 任务级失败，**释放预占**，不重试 |
    | `GatewayUnavailable` | 通道问题 | 任务级失败，**释放预占**，按 `state` 决定可否重试 |

    **不管哪一类都不算成功** —— 三条路径都不产出内容版本。
    这是 R2 / R8 的公共前提。
    """

    def __init__(self, *, reason: str, message: str, state: str | None = None) -> None:
        self.reason = reason
        #: 网关返回的调用状态（`denied` / 未捕获时为 `None`）。
        #: **如实报出**，不在这里做映射。
        self.state = state
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GenerationOutput:
    """一次成功的结构化输出。

    `structured_output` 是**原始 mapping**（尚未过 `schemas` 校验）——
    校验是 `service` 的活（R1 的唯一落点）。本层**只负责取到它**，
    不在这里做任何字段判断：那会让"校验逻辑"有两个位置，
    而 `schemas.py` 的 6 个纯函数就是为了让校验只有一处。

    `dispatched` 如实反映"这次是不是真的发出去了"（见模块 docstring
    最后一节）。它**不决定**动作，只让 `service` 能区分
    "首发拿到输出"与"重放没有输出"两种语境。
    """

    call_id: uuid.UUID
    state: str
    external_request_id: str | None
    structured_output: Mapping[str, Any]
    dispatched: bool


@dataclass
class _RecordingAdapter:
    """薄壳：包住真实适配器，把 `submit` 的结果按 `idempotency_key` 记下来。

    ## 为什么按 `idempotency_key` 记（而不是按 `request_key`）

    见模块 docstring 偏离一：`submit(request)` 拿到的 `CapabilityRequest`
    **不含 `provider_id`**，因此无法复现 `dispatch_call` 内部的 `request_key`。
    `idempotency_key` 由本包派生、请求对象里就有、且同一事务内唯一 ——
    用它当键既准确又不需要第二次查库。

    ## 为什么必须"按 key 记"而不是"记最后一次"

    `dispatch_call` 在**同一次事务里可能被调多次**（`FULL` 重做 = 结构 + 文案
    两次）。若只存"最后一次的 result"，第二次的文案输出会**覆盖**第一次的
    结构输出 —— 而 `service` 拿到的却是结构输出的期望类型，于是它会在
    `schemas.validate_blueprint` 处**全字段缺失**而失败。
    那种失败的信息指向"模型没给出字段"，而真实原因是"取错了调用的结果"——
    排障方向完全错。

    ## 为什么不继承 / 不实现 `CapabilityAdapter` 协议的校验

    它是**包装器**，不是适配器实现：`capability` / `query` / `cancel`
    三个成员**原样透传**给被包的适配器。真实适配器（含 `StubAdapter`）
    一个字节都不用改 —— 这是"接缝冻结"在适配器层的落地。

    `recorded` 是 `dict`（可变）：本类在一次派发的事务内存活，
    生命周期与 `dispatch_for_generation` 的一次调用相同，
    不存在跨线程共享（`dispatch_call` 是同步的）。
    """

    inner: CapabilityAdapter
    recorded: dict[str, CapabilityResult] = field(default_factory=dict)

    @property
    def capability(self) -> str:
        return self.inner.capability

    def submit(self, request: CapabilityRequest) -> CapabilityResult:
        result = self.inner.submit(request)
        self.recorded[request.idempotency_key] = result
        return result

    def query(self, *, external_request_id: str) -> CapabilityResult:
        return self.inner.query(external_request_id=external_request_id)

    def cancel(self, *, external_request_id: str) -> None:
        # `CapabilityAdapter.cancel` 的返回类型是 `None`（不是 `CapabilityResult`）
        # —— 实测自 `gateway/adapters.py:83`。这里如实照抄，
        # 免得本包给上层一个"取消也有结果"的错觉。
        self.inner.cancel(external_request_id=external_request_id)


def dispatch_for_generation(
    gateway_repo: GatewayRepository,
    *,
    adapter: CapabilityAdapter,
    project_id: uuid.UUID,
    content_id: uuid.UUID,
    content_version_no: int,
    task_kind: str,
    model_profile_id: uuid.UUID,
    prompt_version_id: uuid.UUID,
    prompt: str,
    job_id: uuid.UUID | None,
    attempt_id: uuid.UUID | None,
    output_schema_version: int,
    policy_version: str,
    data_classes: tuple[str, ...] = (),
    round_no: int = 0,
) -> GenerationOutput:
    """派发一次生成调用，成功则返回结构化输出。

    ## `idempotency_key` 的派生（R5）—— **本包唯一的派生点**

    `f"content-generate:{content_id}:v{content_version_no}:{task_kind}"`

    三个成分各自的理由：

    | 成分 | 去掉它会怎样 |
    |---|---|
    | `content_id` | 两条内容的生成互相顶掉（后者的键与前者相同 ⇒ 网关认为"已发过"，第二条内容永远拿不到输出，**且不报错**） |
    | `content_version_no` | "重做同一篇"与"第一次生成"撞键 ⇒ 重做被当成重复请求，**不产生新版本**，用户点了重做却什么都没变 |
    | `task_kind` | `FULL` 重做的两次调用（结构 + 文案）撞键 ⇒ 文案那次直接复用结构的输出，而它要的是**另一种** schema ⇒ `schemas` 全字段缺失而失败（同时也是本文件记录键的唯一性来源） |

    **不由调用方传 key**：那会让 `service` 与 `handlers` 两处各派生一次，
    而分叉的表现是"某一条路径上重做不生效"——只在特定入口出现，
    极难复现（与 R3 的 `calls_for` 同一手法：把口径压进唯一函数）。

    ## `round_no` 与幂等键**不是**同一件事

    `round_no` 进的是网关的 `request_key`（"哪一轮调用"），
    不进上面那个 `idempotency_key`（本包侧的"这次生成的身份"）。
    两者混同的后果：多跑一轮会被当成"同一次生成"而静默复用。

    ## 为什么 `job_id` / `attempt_id` 缺失就抛

    范本原话：网关要求 `CapabilityRequest.job_id` 与 `attempt_id` ——
    "没有它们就无法写调用留痕（数据结构 §13.4）"。
    而**没有留痕的调用**是 ADR-10 违规判定（"任何一条 `LOCAL_ONLY`
    项目的出网记录 = 违规"）的盲区：发生过但查不到。
    因此宁可拒绝，也不发一次不可追溯的请求。
    """
    if job_id is None or attempt_id is None:
        raise GatewayUnavailable(
            reason=REASON_GATEWAY_NOT_SUCCEEDED,
            message=(
                "内容生成必须由任务处理器驱动（缺少 jobId / attemptId）。"
                "网关要求 `CapabilityRequest.job_id` 与 `attempt_id` —— "
                "没有它们就无法写调用留痕（数据结构 §13.4），"
                "而不可追溯的出网是 ADR-10 的违规盲区。"
            ),
        )

    # **本地导入**（R7）：`gateway` 是重模块，顶层 import 会让每个请求
    # 都拖着它的适配器与策略树。范本 `brief/decomposer.py:551` 同款注释。
    from rednote.modules.gateway.service import dispatch_call
    from rednote.modules.gateway.types import (
        CAPABILITY_TEXT_STRUCTURED,
        CapabilityRequest,
    )
    from rednote.modules.versions.service import canonical_payload_hash

    idempotency_key = _idempotency_key(
        content_id=content_id,
        content_version_no=content_version_no,
        task_kind=task_kind,
    )
    request = CapabilityRequest(
        request_id=f"content-{uuid.uuid4().hex[:12]}",
        job_id=job_id,
        attempt_id=attempt_id,
        project_id=project_id,
        capability=CAPABILITY_TEXT_STRUCTURED,
        model_profile_id=model_profile_id,
        output_schema_version=output_schema_version,
        policy_version=policy_version,
        idempotency_key=idempotency_key,
        data_classes=data_classes,
        # 摘要而不是正文：网关留痕不留正文（数据结构 §13）。
        context_digest=canonical_payload_hash({"prompt": prompt}),
        # **真值**而不是范本当年的字面量（见模块 docstring 偏离二）。
        prompt_version=str(prompt_version_id),
    )

    recorder = _RecordingAdapter(inner=adapter)
    result = dispatch_call(
        gateway_repo, request=request, adapter=recorder, round_no=round_no
    )

    # --- ③ 无降级分支（ADR-10）------------------------------------------
    if result.denied:
        raise GatewayUnavailable(
            reason=REASON_GATEWAY_DENIED,
            message=(
                "出网策略拒绝了本次内容生成调用："
                f"{getattr(result.denial, 'reason', 'unknown')}。"
                "系统不会降级到本地模型 —— 策略否决是最终结论（ADR-10）。"
                "本次预占将全额释放。"
            ),
        )

    call = result.call
    if call is None:
        # 既没被拒、也没返回调用 —— 只可能是网关自身的装配缺陷。
        raise GatewayUnavailable(
            reason=REASON_GATEWAY_NOT_SUCCEEDED,
            message="网关既未拒绝也未返回调用记录（装配缺陷）。",
        )

    # --- ④ state != SUCCEEDED 一律不当成功（R8）-------------------------
    if call.state == "FAILED":
        raise GatewayUnavailable(
            reason=REASON_GATEWAY_NOT_SUCCEEDED,
            message=(
                f"模型调用失败：{call.last_error_code or 'unknown'}。"
                "本次生成不产出内容版本。"
            ),
            state=call.state,
        )
    if call.state != "SUCCEEDED":
        # POLLING / PREPARED / SUBMITTED / **UNKNOWN** —— 一律不当成功。
        # UNKNOWN 尤其不能当失败而重发（数据结构 §13.4：它在
        # `CALL_IN_FLIGHT_STATES` 里，"这些状态一律不得重新派发"）。
        raise GatewayUnavailable(
            reason=REASON_GATEWAY_NOT_SUCCEEDED,
            message=(
                f"模型调用尚未完成（state={call.state}），本次生成不产出内容。"
                "处置交给任务框架（WP16 的 `outcome_for_result` 会把 UNKNOWN "
                "映射成 WAITING_RECONCILIATION，而不是失败重发）。"
            ),
            state=call.state,
        )

    # --- 结构化输出：从记录适配器按 idempotency_key 取（★ 偏离一）--------
    captured = recorder.recorded.get(idempotency_key)
    if captured is None:
        # 走到这里只有一种可能：网关报 SUCCEEDED 但**本次没有真的 submit**
        # （幂等命中或并发抢先）。**这不是模型失败**，故用 NOT_SUCCEEDED
        # 而不是 NO_OUTPUT —— 后者会把人引向"模型没给输出"。
        raise GatewayUnavailable(
            reason=REASON_GATEWAY_NOT_SUCCEEDED,
            message=(
                f"本次调用未由本事务发出（dispatched={result.dispatched}，"
                f"replayed={result.replayed}），因此没有可用的结构化输出。"
                "同一次生成的重放不产生新版本（R5 幂等语义）——"
                "这不是模型失败，不要据此重发。"
            ),
            state=call.state,
        )

    output = captured.structured_output
    if output is None:
        raise GatewayUnavailable(
            reason=REASON_GATEWAY_NO_OUTPUT,
            message=(
                "模型调用成功但没有结构化输出。"
                f"（网关的终态由 CALL_TERMINAL_STATES 定义："
                f"{', '.join(sorted(CALL_TERMINAL_STATES))}；本包只认 SUCCEEDED。"
                "结构化输出属当次事务内可取，不落 provider_calls ——"
                "该表无对应列，跨事务回查请读 content_versions.payload。）"
            ),
            state=call.state,
        )

    return GenerationOutput(
        call_id=call.call_id,
        state=call.state,
        external_request_id=call.external_request_id,
        structured_output=output,
        dispatched=result.dispatched,
    )


def generation_idempotency_key(
    *, content_id: uuid.UUID, content_version_no: int, task_kind: str
) -> str:
    """R5 的幂等键。**唯一**的构造点（见 `dispatch_for_generation` 的表）。

    公开（不进 `_` 前缀）的理由：`service` 落 `generation_contexts` 时
    要用**同一个**键（`repository.find_generation_context` 按它查重）。
    让 `service` 自己再拼一遍就是第二个格式来源 ——
    而两处分叉的表现是"同一平台的幂等查不到已有行"，即**重复建上下文行**。
    """
    return f"content-generate:{content_id}:v{content_version_no}:{task_kind}"


#: 保留旧名（文件内自用）。**不导出** —— 对外只用 `generation_idempotency_key`。
_idempotency_key = generation_idempotency_key


__all__ = [
    "GatewayUnavailable",
    "GenerationOutput",
    "dispatch_for_generation",
    "generation_idempotency_key",
]