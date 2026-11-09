"""WP27 的第三条接缝：把静态图生成派发接进 WP16 的网关。

**与 `content/gateway_bridge.py` 的关系：同构，但有一处**不可照抄**的差异
——见下面「OP-195」一节，那是本包真正的技术难点。

`prompts_bridge` 已把提示词准备好，`billing_bridge` 已把额度预占好，
本模块负责**唯一一次出网**，并把结果回传给 `service`。
本地导入 `gateway.service.dispatch_call`（R7）：`gateway` 是重模块，
顶层 import 会让每个请求都拖着它的适配器与策略树。

---

## ★ OP-195：图片字节从哪里来（本包**最硬的缺口**，显式处置而不是绕过）

### 已核实的事实（不是推测）

| 可能的位置 | 实测结论 |
|---|---|
| `CapabilityResult.structured_output` | 文本结构化输出的通道，**装不了二进制** |
| `CapabilityResult.output_file_ids` | 全仓仅 2 处出现（`gateway/types.py` 声明 + `gateway/adapters.py` 契约校验），**无任何落库位置**；且 `provider_calls` 表**无输出列** ⇒ 跨事务回查查不到 |
| `provider_calls` 表 | 无 cost / usage / output 列 |
| `CallView` / `DispatchResult` | 无 cost、无 output 字段 |

⇒ **网关上一次图片调用，本机拿不到图片字节**（`output_file_ids` 里的 id 是
**外部系统的文件 id**，不是本机 `file_objects.id`；拿它填 `media_versions.image_file_id`
会得到一个**悬空外键**，而 FK 约束会在提交时报错 —— 至少不会静默，但错误信息
会指向 FK 而不是根因）。

### 本模块的处置：`ImageByteSource` 协议 + 默认 fail-closed

本模块把"字节从哪来"抽成一个**显式注入点**：

    @runtime_checkable
    class ImageByteSource(Protocol):
        def fetch(self, *, call: GatewayCallView, project_id: uuid.UUID) -> ImagePayload | None: ...

- **传了** `byte_source` ⇒ 由它按 `call` 取回字节，本模块校验后返回；
- **没传**（默认 `None`）⇒ **抛 `GatewayUnavailable(REASON_GATEWAY_NO_IMAGE_REF)`**，
  **不是**落一个空 `image_file_id`，**不是**记 `state=READY` 但没图的假成功。

**为什么不用"默认实现"**：本机目前**没有**真实的字节来源（OP-195 未关闭，
责任方是图片适配器归属那一条）。写一个"从 `structured_output` 里挖 base64"
的默认实现，等于把一个**猜测**固化成生产路径 —— 一旦真适配器的形态不同，
这条路径会静默返回错误的东西。**宁可在装配期就炸**（`byte_source=None`
是一次显眼的装配决定），也不要一个"看起来能跑"的假实现。

### 谁来提供 `byte_source`（当前答案：**没有人**）

这意味着 **`dispatch_for_media_generation` 在本机上必然走 fail-closed 分支**。
这是**正确**的当前状态，不是缺陷：
- 它保证了"没有字节来源时绝不会落库一张指向空气的图"；
- 它让 §3.6 E 的 `BLOCKED_ON_EXTERNAL`（真实出图 → 样张目检 → AQ-03 判定）
  在代码层面**同样成立**，而不只是文档里的一句话；
- OP-195 关闭时，**改动范围是"装配层传进一个实现"，不是"改派发逻辑"** ——
  因为 `ImageByteSource` 的接口在这里已经定死。

⚠️ 这也意味着 **`test_media_wp27.py` 里不能断言"成功路径落了图"** ——
那需要一个 fake `byte_source`（测试里注入，生产装配里为 `None`）。
把两者混同 就是"测试证明了生产不成立的事"。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from rednote.modules.media.types import (
    CAPABILITY_STATIC_IMAGE,
    EGRESS_DATA_CLASSES,
    REASON_GATEWAY_DENIED,
    REASON_GATEWAY_NOT_SUCCEEDED,
    REASON_GATEWAY_NO_IMAGE_REF,
    REASON_GATEWAY_NO_OUTPUT,
)

#: 网关终态集合。**本地 import 的字面量在函数内**，这里只作错误消息里的展示。
_CALL_TERMINAL_STATES_HINT: str = "由 gateway.types.CALL_TERMINAL_STATES 定义"


class GatewayUnavailable(RuntimeError):
    """静态图出网不可用。

    **继承 `RuntimeError` 而不是 `ValueError`**：与 `content/gateway_bridge`
    对齐 —— "模型输出不合法"（`ValueError` 族）与"出网不可用"（本类）在
    `service` 侧要分开处理，两者的处置完全不同（前者是不可重试的业务失败，
    后者里`CALL_IN_FLIGHT_STATES` 映射成 `WAITING_RECONCILIATION`）。

    `.state` 承载网关调用状态：`handlers.py` 靠它判断是否进对账等待，
    **缺了它就把"还在飞"当成"失败"**（= 重发 = 重复扣费）。
    """

    def __init__(
        self, *, reason: str, message: str, state: str | None = None
    ) -> None:
        self.reason = reason
        self.state = state
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ImagePayload:
    """一次出网换回来的**图片字节**及其自描述元数据。

    ## 为什么 `bytes` 与元数据打包在一起

    `width` / `height` / `byte_size` / `image_format` 全部**由本机测量得到**
    （`service.py` 的 R2 校验里用 Pillow 打开同一份字节测出来），
    **不是**从模型返回值里抄的 —— 抄模型给的尺寸就是"信供应商的自我描述"，
    而 `media_renditions` 的 CHECK 与前端渲染都依赖它。
    ⇒ 本类里的四个字段是**本机的测量结果**，`byte_source` 的实现有义务
    在返回前填好；填 `None` 会被 `service.py` 拒绝（那里是唯一的落库点）。

    `image_format` 用**大写**（`PNG` / `JPEG` / `WEBP`），与
    `media_renditions.image_format` 的 CHECK 取值域一致（WP27 的
    `MAX_FORMAT_CHARS = 20`）。
    """

    content: bytes
    width: int
    height: int
    byte_size: int
    image_format: str


@runtime_checkable
class ImageByteSource(Protocol):
    """**OP-195 的注入点**：按一次成功的调用取回本机的图片字节。

    ## 为什么是 Protocol 而不是具体类

    字节从哪来取决于图片适配器的形态，而那件事（OP-195 / OP-130）**本包
    没有结论**。定义成接口，本包就能在结论出来之前把边界划好：
    实现方可以是"从对象存储下载"、"从供应商结果页抓"、"从本地缓存读"，
    都不需要改派发逻辑。

    ## `fetch` 的契约（三条，缺一条就是实现缺陷）

    1. **只在 `call.state == SUCCEEDED` 时被调用**（本模块保证）⇒
       实现不需要自己判状态；
    2. **拿不到就返回 `None`**（不是抛异常）—— 本模块会把 `None` 翻成
       `NO_IMAGE_REF`。理由：实现方**没有**本包的业务上下文，
       "为什么拿不到"（对象已被清理 / 供应商没给 / id 过期）该由本包决定
       报哪个码；
    3. **幂等**：同一个 `call` 多次 `fetch` 要给同样的字节 ——
       因为 `service.py` 的失败重试会重新走一遍派发（R5 幂等命中），
       每次都要能拿到同一份字节。

    ⚠️ **实现方必须自己保证 `image_format` / `width` / `height` 与 `content`
    一致**：本模块**不校验**这三个字段（校验的唯一落点是 `service.py` 的
    R2，那里用 Pillow 打开**字节本身**重测）。也就是说实现方填错尺寸时，
    拦它的是 R2 而不是本模块 —— 这个分工是刻意的（同一个测量只做一次）。
    """

    def fetch(self, *, call: Any, project_id: uuid.UUID) -> ImagePayload | None:
        """按 `call` 取回图片字节；取不到返回 `None`。"""
        ...


@dataclass(frozen=True, slots=True)
class MediaGenerationOutput:
    """一次成功派发的出网结果。

    与 `content/gateway_bridge.GenerationOutput` 的差异：**多了
    `image`（可能为 `None`）与 `call_id` 之外的 `provider_call_id` 语义**。
    """

    call_id: uuid.UUID
    state: str
    external_request_id: str | None
    structured_output: Mapping[str, Any]
    image: ImagePayload | None
    dispatched: bool


@dataclass
class _RecordingAdapter:
    """记录当次 `submit` 的返回，供同事务内取`structured_output`。

    ★ **为什么必须用这个而不是范本的 `get_structured_output`**：
    `gateway` 的 `CapabilityAdapter` **没有** `get_structured_output` 方法
    （实测 `gateway/adapters.py:83` 只有 `capability` / `submit` / `query` /
    `cancel`）⇒范本里那句"取当次结构化输出"在**本机没有落点**。
    这正是 ISS-95 形态的"声称有防线"：注释写了一个不存在的能力。
    本类把它落成真东西。

    `recorded` 是 `dict`（可变）：本类在一次派发的事务内存活，
    生命周期与 `dispatch_for_media_generation` 的一次调用相同，
    不存在跨线程共享（`dispatch_call` 是同步的）。
    """

    inner: Any
    recorded: dict[str, Any] = field(default_factory=dict)

    @property
    def capability(self) -> str:
        return self.inner.capability

    def submit(self, request: Any) -> Any:
        result = self.inner.submit(request)
        self.recorded[request.idempotency_key] = result
        return result

    def query(self, *, external_request_id: str) -> Any:
        return self.inner.query(external_request_id=external_request_id)

    def cancel(self, *, external_request_id: str) -> None:
        # `CapabilityAdapter.cancel` 的返回类型是 `None`（不是 `CapabilityResult`）
        # —— 实测自 `gateway/adapters.py:83`。这里如实照抄，
        # 免得本包给上层一个"取消也有结果"的错觉。
        self.inner.cancel(external_request_id=external_request_id)


def dispatch_for_media_generation(
    gateway_repo: Any,
    *,
    adapter: Any,
    byte_source: ImageByteSource | None,
    project_id: uuid.UUID,
    content_id: uuid.UUID,
    media_item_id: uuid.UUID,
    target_version_no: int,
    task_kind: str,
    model_profile_id: uuid.UUID,
    prompt_version_id: uuid.UUID,
    prompt: str,
    job_id: uuid.UUID | None,
    attempt_id: uuid.UUID | None,
    output_schema_version: int,
    policy_version: str,
    data_classes: tuple[str, ...] = EGRESS_DATA_CLASSES,
    round_no: int = 0,
) -> MediaGenerationOutput:
    """派发一次静态图调用，成功则返回结构化输出**与图片字节**。

    ## `idempotency_key` 的派生（R5）—— **本包唯一的派生点**

    `f"media-generate:{media_item_id}:v{target_version_no}:{task_kind}"`

    | 成分 | 去掉它会怎样 |
    |---|---|
    | `media_item_id` | 不同图的调用互相顶掉（后者的键与前者相同 ⇒ 网关认为"已发过"，第二张图永远拿不到输出，**且不报错**） |
    | `target_version_no` | "重做同一张图"与"第一次生成"撞键 ⇒ 重做被当成重复请求，**不产生新版本**，用户点了重做却什么都没变 |
    | `task_kind` | 本包只有一个 kind，但**仍然在键里**：将来 WP28（动态图）接进来时，"同一张图的静态版与动态版"必须撞不同的键。这个成分现在无害，将来救场 |

    ⚠️ **与 `media_items.last_idempotency_key`（投影列）的关系**：那一列是
    `IDEMPOTENCY_SCOPE_MEDIA_GENERATE = "media-generate"` 口径的**任务层**键
    （`api.py` 造，用于 `enqueue_job` 的幂等），与本函数派发用的**模型调用层**键
    是**两个不同的键**（前者不含版本号，后者含）。混用两者的后果是
    "重做被当成重复入队、永远排不上队"。层次对照：

    | 层 | 键形状 | 含版本号 | 作用 |
    |---|---|---|---|
    | 任务层（`api.py`） | `{media_item_id}:{request_key}` | ❌ | 同一入队请求不建两个任务 |
    | **模型调用层（本函数）** | `media-generate:{media_item_id}:v{n}:{task_kind}` | ✅ | 同一次生成不重复买 |
    | 单项上下文层（`service`） | `content_item_id + idempotency_key` | ✅ | `find_generation_context` 查重 |

    **不由调用方传 key**：那会让 `service` 与 `handlers` 两处各派生一次，
    而分叉的表现是"某一条路径上重做不生效"—— 只在特定入口出现，极难复现。

    ## `round_no` 与幂等键**不是**同一件事

    `round_no` 进的是网关的 `request_key`（"哪一轮调用"），不进上面那个
    `idempotency_key`。两者混同的后果：多跑一轮会被当成"同一次生成"而静默复用。

    ## 为什么 `job_id` / `attempt_id` 缺失就抛

    范本原话：网关要求 `CapabilityRequest.job_id` 与 `attempt_id` ——
    "没有它们就无法写调用留痕（数据结构 §13.4）"。而**没有留痕的调用**是
    ADR-10 违规判定（"任何一条 `LOCAL_ONLY` 项目的出网记录 = 违规"）的盲区：
    发生过但查不到。因此宁可拒绝，也不发一次不可追溯的请求。
    """
    if job_id is None or attempt_id is None:
        raise GatewayUnavailable(
            reason=REASON_GATEWAY_NOT_SUCCEEDED,
            message=(
                "静态图生成必须由任务处理器驱动（缺少 jobId / attemptId）。"
                "网关要求 `CapabilityRequest.job_id` 与 `attempt_id` —— "
                "没有它们就无法写调用留痕（数据结构 §13.4），"
                "而不可追溯的出网是 ADR-10 的违规盲区。"
            ),
        )

    # **本地导入**（R7）：`gateway` 是重模块，顶层 import 会让每个请求
    # 都拖着它的适配器与策略树。范本 `brief/decomposer.py:551` 同款注释。
    from rednote.modules.gateway.service import dispatch_call
    from rednote.modules.gateway.types import CapabilityRequest
    from rednote.modules.versions.service import canonical_payload_hash

    idempotency_key = media_generation_idempotency_key(
        media_item_id=media_item_id,
        target_version_no=target_version_no,
        task_kind=task_kind,
    )
    request = CapabilityRequest(
        request_id=f"media-{uuid.uuid4().hex[:12]}",
        job_id=job_id,
        attempt_id=attempt_id,
        project_id=project_id,
        capability=CAPABILITY_STATIC_IMAGE,
        model_profile_id=model_profile_id,
        output_schema_version=output_schema_version,
        policy_version=policy_version,
        idempotency_key=idempotency_key,
        data_classes=data_classes,
        # 摘要而不是正文：网关留痕不留正文（数据结构 §13）。
        #★ 这里只摘要 `prompt`：图片的字节**不在本请求里**（OP-195），
        #   所以摘要覆盖不到"出了什么图"。网关留痕对静态图天然只能到
        #   "用哪版提示词调了图片模型"这一层 —— 这一点写明，避免将来有人
        #   把 `context_digest` 当成"出图内容的指纹"。
        context_digest=canonical_payload_hash({"prompt": prompt}),
        # **真值**而不是字面量（范本偏离二）。
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
                "出网策略拒绝了本次静态图调用："
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
                f"图片模型调用失败：{call.last_error_code or 'unknown'}。"
                "本次生成不产出媒体版本。"
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
                f"图片模型调用尚未完成（state={call.state}），本次生成不产出媒体版本。"
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
                "图片模型调用成功但没有结构化输出。"
                f"（网关的终态由 {_CALL_TERMINAL_STATES_HINT}；本包只认 SUCCEEDED。"
                "结构化输出属当次事务内可取，不落 provider_calls ——"
                "该表无对应列。）"
            ),
            state=call.state,
        )

    # --- ★ OP-195：图片字节（**本包与范本最大的结构差异**）---------------
    if byte_source is None:
        raise GatewayUnavailable(
            reason=REASON_GATEWAY_NO_IMAGE_REF,
            message=(
                "本次装配没有提供 `byte_source`（OP-195 未关闭）："
                "网关只带回结构化输出与外部文件 id，"
                "`CapabilityResult.output_file_ids` 不是本机 `file_objects.id` "
                "（该字段全仓仅声明与契约校验两处，无落库位置，"
                "且 `provider_calls` 表无输出列）⇒ **拿不到图片字节**。"
                "本包 fail-closed：不落 `media_versions`、不推进投影。"
                "关闭 OP-195 的改动范围是「装配层注入一个 ImageByteSource 实现」，"
                "不是改派发逻辑。"
            ),
            state=call.state,
        )

    payload = byte_source.fetch(call=call, project_id=project_id)
    if payload is None:
        raise GatewayUnavailable(
            reason=REASON_GATEWAY_NO_IMAGE_REF,
            message=(
                f"`byte_source` 没有取回调用 {call.call_id} 的图片字节。"
                "（`fetch` 契约规定取不到返回 `None`，由本模块决定报哪个码 —— "
                "因为只有本模块知道'为什么取不到'该怎么分类。）"
                "本包 fail-closed：不落 `media_versions`、不推进投影。"
            ),
            state=call.state,
        )

    return MediaGenerationOutput(
        call_id=call.call_id,
        state=call.state,
        external_request_id=call.external_request_id,
        structured_output=output,
        image=payload,
        dispatched=result.dispatched,
    )


def media_generation_idempotency_key(
    *, media_item_id: uuid.UUID, target_version_no: int, task_kind: str
) -> str:
    """R5 的幂等键。**唯一**的构造点（见 `dispatch_for_media_generation` 的表）。

    公开（不进 `_` 前缀）的理由：`service` 落 `media_generation_contexts` 时
    要用**同一个**键（`repository.find_generation_context` 按它查重）。
    让 `service` 自己再拼一遍就是第二个格式来源—— 而两处分叉的表现是
    "同一张图的幂等查不到已有行"，即**重复建上下文行**。

    ⚠️ 注意它与 `types.IDEMPOTENCY_SCOPE_MEDIA_GENERATE`（`"media-generate"`）
    **同前缀但不同形状**：那个是任务层键的 scope 部分（不带 id），
    这个是完整的模型调用层键。共用前缀是刻意的（让运维在网关日志里一眼
    看出"这条调用来自 media 包的哪一层"）。
    """
    return f"media-generate:{media_item_id}:v{target_version_no}:{task_kind}"


#: 保留旧名（文件内自用）。**不导出** —— 对外只用 `media_generation_idempotency_key`。
_idempotency_key = media_generation_idempotency_key


__all__ = [
    "GatewayUnavailable",
    "ImageByteSource",
    "ImagePayload",
    "MediaGenerationOutput",
    "dispatch_for_media_generation",
    "media_generation_idempotency_key",
]