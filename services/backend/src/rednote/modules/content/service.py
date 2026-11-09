"""WP26 的业务规则层：**本模块是本包唯一的对外入口**。

一次内容生成的全部编排都在这一个文件里，顺序固定、每步可判：

    解析请求 → 解析提示词（R6）→ 解析基线（R4 的判据域）→ 解析模型档案
    → 逐 task_kind〔预估 → 预占 → 派发 → 校验（R1）→ 落库 → 结算〕

`api.py` 只做协议（HTTP 形状 / 权限 / 分页），`handlers.py` 只做任务框架的
适配（`ClaimedJob` → 本模块 → `HandlerOutcome`）。**两者都不含业务判断** ——
把判断放在那两处会让"同一规则两条路径"，而分叉的表现通常是**静默**的
（一条路径生效、另一条不生效，且不报错）。

---

## 预占为什么必须在执行阶段

（`types.py` 的 `GenerationRequest` docstring 引用了这一节，两处必须一致。）

「发起」与「执行」是**两次事务**：`POST /projects/{id}/content-generations`
建身份行并入队（WP13），Worker 领取后调本模块。钱必须在**执行**那次事务里
预占，理由逐条：

| 若在发起阶段预占 | 后果 |
|---|---|
| 预占后任务排队几小时才跑 | 额度被长期占住，而"这一批到底跑不跑"在排队期间是**未知**的；用户取消任务时还要补一次释放 |
| 发起阶段是**批量**的（N 条内容一个请求） | 预占要按 N 条算；而执行是**逐条**的（一条内容一个任务）⇒ 一条失败时"退多少钱"要按条数折算，而折算口径必然与预占口径分叉（R3） |
| 发起阶段的内容条数**只是请求里的意图** | 真正要生成几条由执行阶段落库的身份行决定 —— 中途删掉一条内容，预占就多了一笔 |

因此本模块的口径是：**每条内容、每个 `task_kind` 各预占一次**
（`calls_for(task_kind, 1)`），预占与派发在**同一个事务**里，且预占
**严格早于**派发（R2：先生成后记账 = 花了钱却没有账面）。

发起端点那次事务只做一件与钱无关的事：按整批做**展示用**的预估
（§1.7 前端消费面），让用户在点确认之前看到一个金额。

---

## 三件事本模块**不做**

1. **不包装网关协议错误。** WP16 的 `ApiError` 原样透传 —— 它是"协议不对"，
   而本包的三类失败（`ValueError` + 提示词码 / `ValueError` + 输出码 /
   `GatewayUnavailable`）都不是它。包一层会让上游的错误码在两层之间被重命名，
   而排障时要读的正是**上游那个**码。

2. **不实现重试。** `MAX_STEPS` / 轮次上限是任务框架（WP13）的事。
   本模块一次调用做一轮；重试由 `handlers.py` 折成 `HandlerOutcome.retryable`
   后交给框架。在这里写 `for attempt in range(3)` 会让"重试一次"变成
   "重试三次"，而每次重试都是一次真实出网（R5 的幂等键能兜住重复计费，
   但兜不住三次延迟叠加）。

3. **不决定 `UNKNOWN` 怎么处置。** `gateway_bridge` 如实报出 `state`；
   本模块把它折成任务级失败；**"等核对还是重发"是 WP16 `outcome_for_result`
   的决定**。本模块不重复那个映射（两个真值来源 ⇒ 分叉 ⇒ 重发一次真实调用）。

---

## 失败路径的裁决表（唯一口径）

| 抛出 | 归类 | 释放预占 | 落内容版本 | `handlers` 折成 |
|---|---|---|---|---|
| `ContentGenerationError`（本模块） | 业务前置不成立 | ✅ | ❌ | `FAILED`，`retryable=False` |
| `PromptUnavailable` | 部署没配提示词 | ✅ | ❌ | `FAILED`，`retryable=False` |
| `SchemaViolation` | 模型输出不合法 | ✅ | ❌ | `FAILED`，`retryable=False` |
| `GatewayUnavailable` | 通道问题 | ✅ | ❌ | `FAILED`，按 `state` 决定 `retryable` |
| 上游 `ApiError` | 协议错误 | ✅ | ❌ | 原样上报（本模块不包装） |

**所有路径都不产出内容版本** —— 这是 R2 / R8 的公共前提。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

from rednote.modules.content.billing_bridge import (
    estimate_generation_cost,
    release_generation_budget,
    reserve_generation_budget,
    settle_generation_budget,
)
# 顶层 import `gateway_bridge` 不违反 R7：`gateway_bridge` 自己顶层只 import
# `gateway.types`（轻模块，纯取值域），真正重的 `gateway.service` 与各适配器
# 由它在**函数内**本地导入。因此本模块导入期不会把网关依赖树拉进来。
from rednote.modules.content.gateway_bridge import (
    GatewayUnavailable,
    dispatch_for_generation,
    generation_idempotency_key,
)
from rednote.modules.content.prompts_bridge import (
    ActivePromptVersion,
    render_system_prompt,
    resolve_active_version,
)
from rednote.modules.content.repository import ContentRepository
from rednote.platform.concurrency import build_etag, version_conflict
from rednote.modules.content.schemas import (
    split_paragraphs,
    validate_bindings,
    validate_blueprint,
    validate_copy,
)
from rednote.modules.content.types import (
    BINDING_KEY_CLAIM_ID,
    BINDING_KEY_PARAGRAPH_INDEX,
    BINDING_KEY_REASON,
    BINDING_KEY_TEXT,
    BLUEPRINT_FIELDS,
    CONTENT_STATE_FAILED,
    CONTENT_STATE_GENERATED,
    CONTENT_STATE_STRUCTURED,
    REASON_BASELINE_MISMATCH,
    REASON_BASELINE_NOT_READY,
    REASON_CONTENT_NOT_FOUND,
    REASON_CONTENT_VERSION_NOT_FOUND,
    REASON_EDIT_LOCK_CONFLICT,
    REASON_GENERATION_REQUEST_INVALID,
    REASON_MODEL_PROFILE_NOT_FOUND,
    REASON_PROJECTION_CONFLICT,
    REASON_SCOPE_REQUIRES_BLUEPRINT,
    REASON_TITLE_CANDIDATE_MISMATCH,
    REASON_TITLE_CANDIDATE_NOT_FOUND,
    TASK_CONTENT_COPY,
    TASK_CONTENT_STRUCTURE,
    ContentDetail,
    ContentSummary,
    ContentVersionSummary,
    EditTarget,
    GenerationContextView,
    GenerationRequest,
    task_kinds_for_scope,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期存在
    from rednote.modules.baseline.repository import BaselineRepository
    from rednote.modules.billing.repository import BillingRepository
    from rednote.modules.billing.types import CostEstimateView
    from rednote.modules.content.models import ContentItem
    from rednote.modules.gateway.repository import GatewayRepository
    from rednote.modules.prompts.repository import PromptRepository
    from rednote.modules.versions.repository import VersionRepository

_log = logging.getLogger(__name__)

#: E1/E2/E3 依赖边（version_dependencies）的 purpose；方向 parent（内容版本）→ dependency。
DEPENDENCY_PURPOSE_GENERATION_PROMPT: Final[str] = "GENERATION_PROMPT"
DEPENDENCY_PURPOSE_GENERATION_BASELINE: Final[str] = "GENERATION_BASELINE"
DEPENDENCY_PURPOSE_CONTENT_REVISION: Final[str] = "CONTENT_REVISION"


# ---------------------------------------------------------------------------
# 提示词占位符的键名（本包与提示词模板之间的契约）
# ---------------------------------------------------------------------------
# `render_system_prompt(prompt, values=...)` 判的是"模板里的占位符有没有值"，
# 而**模板声明了哪些字段**在 `ActivePromptVersion.input_schema` 里。
# 本模块给不出模板要的键时，`render_system_prompt` 会 fail-closed 抛出并
# **列出缺的键** —— 部署者据此改模板。这是刻意的：本包**不猜**键名。
#
# 这四个键是本包能提供的**全部**输入。它们登记为待复核项（附录 A）：
# 键名一旦确定，提示词模板必须与之对齐。
PROMPT_VALUE_CONTENT_ID: Final[str] = "contentId"
PROMPT_VALUE_PROJECT_ID: Final[str] = "projectId"
PROMPT_VALUE_BATCH_KIND: Final[str] = "batchKind"
PROMPT_VALUE_BLUEPRINT: Final[str] = "blueprint"

#: 本包能提供值的全部键（`generation_prompt_values` 的取值域）。
PROMPT_VALUE_KEYS: Final[frozenset[str]] = frozenset(
    {
        PROMPT_VALUE_CONTENT_ID,
        PROMPT_VALUE_PROJECT_ID,
        PROMPT_VALUE_BATCH_KIND,
        PROMPT_VALUE_BLUEPRINT,
    }
)


class ContentGenerationError(ValueError):
    """内容生成的**业务前置**不成立（内容不存在 / 档案不存在 / 基线不匹配）。

    **继承 `ValueError`**，与 `schemas.SchemaViolation` /
    `prompts_bridge.PromptUnavailable` 同族 —— 调用方（`handlers.py`）
    在一条 `except ValueError` 里接住全部"输入/配置不对"，靠 `.reason` 分细类。
    `GatewayUnavailable` **刻意不进这一族**（它是 `RuntimeError`），
    因为它的语义是"通道没通"，处置（可能需要重试）与"输入不对"不同。

    **`.reason` 的取值域**：全部来自 `types.py` 的 `REASON_*`（本模块不另起码 ——
    `api.py` 要按码翻 HTTP 状态，两处各定义一份就是 ISS-66 形态）。
    本类**不携带**额外的字段 —— 需要细节就写进 `message`，而机器可判的部分
    一律走 `reason`（与 `ApiError` 的 `details` 约定同形）。
    """

    def __init__(self, *, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


# ---------------------------------------------------------------------------
# 生成器接缝（R7 的落点：本包**不**在顶层 import 网关）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GenerationDispatch:
    """一次派发的**原始**结果（**未过校验**）。

    `structured_output` 是原始 mapping —— 校验（R1）是 `execute_generation`
    的活，本结构只负责把它从生成器那里**搬过来**。
    与 `gateway_bridge.GenerationOutput` 分开展示，是为了让
    `ContentGenerator` 的实现者（含测试里的桩）**不必构造**一个
    `GatewayRepository` 才能返回结果。
    """

    structured_output: Mapping[str, Any]
    #: 网关的调用 id（落 `generation_contexts.provider_call_id`）。
    #: 桩实现可以给 `None` —— 那一列可空。
    provider_call_id: uuid.UUID | None
    #: 网关报出的调用状态（**如实**，不做映射）。
    state: str


class ContentGenerator(Protocol):
    """把一次生成请求发给模型（本包的**出网边界**）。

    只一个方法、只收关键字参数：`FULL` 重做的两次调用（结构 / 文案）
    走**同一条**路径，区别只在 `task_kind` —— 两个方法
    （`generate_blueprint` / `generate_copy`）会造出"两条调用路径"，
    而两条路径的差异（比如一条传了 `round_no`、另一条没传）
    **不会报错**，只会让某一类生成的行为悄悄不同。

    实现方（`GatewayGenerator` / `UnavailableGenerator` / 测试桩）
    **必须**在失败时抛 `GatewayUnavailable`，不得返回空 mapping：
    空 mapping 会被 `schemas` 判成"字段缺失"，从而把"通道不通"
    误报成"模型输出不合法"——排障方向完全错。
    """

    def generate(
        self,
        *,
        task_kind: str,
        prompt: str,
        prompt_version_id: uuid.UUID,
        content_id: uuid.UUID,
        content_version_no: int,
        model_profile_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> GenerationDispatch:  # pragma: no cover - 协议声明
        ...


@dataclass(frozen=True, slots=True)
class UnavailableGenerator:
    """未接线时的生成器：**调用即抛**（fail-closed），不静默出空结果。

    与范本 `brief/decomposer.py` 的 `UnavailableDecomposer` 同款：
    它的存在是为了让"忘了注入真实生成器"这件事在**第一次调用**时
    以一条明确的错误暴露，而不是让整条链路跑完、产出空内容。
    """

    reason: str = "GENERATOR_NOT_CONFIGURED"
    message: str = (
        "内容生成器未接线（没有注入真实的 `ContentGenerator`）。"
        "本包 fail-closed：不生成，而不是产出空内容。"
        "装配入口见 `handlers.py`。"
    )

    def generate(
        self,
        *,
        task_kind: str,
        prompt: str,
        prompt_version_id: uuid.UUID,
        content_id: uuid.UUID,
        content_version_no: int,
        model_profile_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> GenerationDispatch:
        raise GatewayUnavailable(reason=self.reason, message=self.message)


@dataclass(frozen=True, slots=True)
class GatewayGenerator:
    """真实接线：`gateway_bridge.dispatch_for_generation` 的薄包装。

    ## 为什么 `output_schema_version` / `policy_version` 在**构造期**注入

    照范本 `brief/decomposer.py:476`（`output_schema_version: int = 1` /
    `policy_version: str = "v1"` 存在 `self._output_schema_version` /
    `self._policy_version`，构造 `CapabilityRequest` 时用它们）。

    **不放在 `generate()` 的入参里**：它们是**部署级**配置（由
    `build_task_registry` 注入一次），而不是每请求变化的量。
    放进 `generate()` 会让每个调用点都要记得传 —— 而"忘传"的默认值
    与部署约定不一致时**不报错**，只是网关侧的策略版本悄悄不同。

    ## `job_id` / `attempt_id` 可为 `None`，但 `gateway_bridge` 会拒

    `dispatch_for_generation` 在两者为 `None` 时抛 `GatewayUnavailable`
    （"没有留痕的出网是 ADR-10 的违规盲区"）。本类**如实透传** `None`，
    不在这里预判 —— 判定只有一处（`gateway_bridge`）。
    """

    gateway_repo: GatewayRepository
    adapter: Any  # CapabilityAdapter；此处不 import gateway.adapters（R7）
    job_id: uuid.UUID | None
    attempt_id: uuid.UUID | None
    output_schema_version: int = 1
    policy_version: str = "v1"
    round_no: int = 0
    data_classes: tuple[str, ...] = ()

    def generate(
        self,
        *,
        task_kind: str,
        prompt: str,
        prompt_version_id: uuid.UUID,
        content_id: uuid.UUID,
        content_version_no: int,
        model_profile_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> GenerationDispatch:
        out = dispatch_for_generation(
            self.gateway_repo,
            adapter=self.adapter,
            project_id=project_id,
            content_id=content_id,
            content_version_no=content_version_no,
            task_kind=task_kind,
            model_profile_id=model_profile_id,
            prompt_version_id=prompt_version_id,
            prompt=prompt,
            job_id=self.job_id,
            attempt_id=self.attempt_id,
            output_schema_version=self.output_schema_version,
            policy_version=self.policy_version,
            data_classes=self.data_classes,
            round_no=self.round_no,
        )
        return GenerationDispatch(
            structured_output=out.structured_output,
            provider_call_id=out.call_id,
            state=out.state,
        )


# ---------------------------------------------------------------------------
# 返回结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GenerationOutcome:
    """一次执行的结果（**含结算状态**）。

    为什么不是直接返回 `ContentDetail`：`handlers.py` 要把"结算是否成功"
    折进 `HandlerOutcome.usage_pending`。若只返回详情，那一项就没有来源，
    而"结算失败但内容已落库"这个状态**必须**能被上报 —— 它是运维
    找回那笔挂账预占的唯一线索（内容在、版本在、账面上那笔还挂着）。
    """

    detail: ContentDetail
    #: `/ 钱还没落定`：预占已发生但结算未成功。此时**不能**释放预占
    #: （钱可能已经花了），只能如实上报，由运维按 `ref` 对账。
    usage_pending: bool
    #: 本次执行涉及的预占单 id（成功结算的与待结算的都在内，**不含**已释放的）。
    reservation_ids: tuple[uuid.UUID, ...]


# ---------------------------------------------------------------------------
# 提示词取值
# ---------------------------------------------------------------------------


def generation_prompt_values(
    *,
    content_id: uuid.UUID,
    project_id: uuid.UUID,
    batch_kind: str,
    blueprint: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """构造提示词渲染用的值。**只提供本包能提供的四个键**。

    `blueprint` 只在文案调用（`TASK_CONTENT_COPY`）时给 —— 结构调用时
    蓝图还不存在，而"给它上一轮的旧蓝图"会让结构与文案基于**不同轮次**的
    蓝图工作，且**不报错**（旧蓝图是一个结构合法的 mapping）。

    蓝图序列化成 `json.dumps(..., sort_keys=True)`：同一个蓝图在两次
    调用里必须序列化成同一个字符串，否则"同样的输入"会产出不同的
    `context_digest`（`gateway_bridge` 用它做留痕摘要），而摘要不同
    意味着幂等命中判不出来。
    """
    values: dict[str, str] = {
        PROMPT_VALUE_CONTENT_ID: str(content_id),
        PROMPT_VALUE_PROJECT_ID: str(project_id),
        PROMPT_VALUE_BATCH_KIND: batch_kind,
    }
    if blueprint is not None:
        values[PROMPT_VALUE_BLUEPRINT] = json.dumps(
            dict(blueprint), ensure_ascii=False, sort_keys=True
        )
    return values


# ---------------------------------------------------------------------------
# 模型档案 → provider_id（**全包唯一的解析点**）
# ---------------------------------------------------------------------------


def resolve_provider_id(
    gateway_repo: GatewayRepository, *, model_profile_id: uuid.UUID
) -> uuid.UUID:
    """由模型档案 id 解出 `provider_id`。**全包唯一的解析点**。

    ## 为什么必须是一处

    `types.py` 的 `GenerationRequest` docstring 原文：

    > **`provider_id` 不在其中**：它由 `model_profile_id` 唯一解出
    > （`gateway_repository.get_model_profile(id).provider_id`）——
    > 存进来就有两个来源，而两者分叉的表现是"预占记在 A 供应商、
    > 实际调用走 B"，账面与留痕对不上却都不报错。**解析在 `service.py`，一处。**

    本函数就是那个"一处"。它有**两个**调用方：

    | 调用方 | 什么时候 | 拿它做什么 |
    |---|---|---|
    | `execute_generation` | 执行阶段第 4 步 | 真正预占与派发时的 `provider_id` |
    | `api.py` 的发起端点 | 发起那次事务 | 展示用预估（§1.7 前端消费面），"让用户在点确认之前看到一个金额" |

    若各自写一遍解析，就是"同一个判据两处实现"（ISS-66 形态）——
    而这一处分叉的后果是**预占与派发记到不同供应商**，
    两种账面各自自洽，都不报错。

    ## 抛什么

    `ContentGenerationError(MODEL_PROFILE_NOT_FOUND)`：档案不存在是
    "请求里的字段值指向一个不存在的对象"，端点把它翻成 422
    （不是 404 —— 它是被引用的**字段值**，不是被寻址的资源；
    与 `brief/api.py` 里 `baseVersionId` 报 422 同款判据）。

    `gateway_repo` 的类型只在模块顶部的 `TYPE_CHECKING` 块里 import ——
    理由见模块顶部 R7 那一段（重的仓库模块不进导入期）。
    """
    profile = gateway_repo.get_model_profile(model_profile_id)
    if profile is None:
        raise ContentGenerationError(
            reason=REASON_MODEL_PROFILE_NOT_FOUND,
            message=(
                f"模型档案 {model_profile_id} 不存在。"
                "（provider_id 由它唯一解出 —— 请求里不存 provider_id，"
                "存了就有两个来源，而分叉的表现是"
                "『预占记在 A 供应商、实际调用走 B』。）"
            ),
        )
    return profile.provider_id


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def execute_generation(
    content_repo: ContentRepository,
    *,
    content_id: uuid.UUID,
    generator: ContentGenerator,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> GenerationOutcome:
    """跑完一条内容的一次生成。**本包唯一的生成入口**。

    ## 事务与锁

    全部仓库由 `content_repo.session` 派生（**同一个事务**）：
    详情响应的字段分属本模块五张表与 WP11 的 `version_nodes`，
    不复用 session 会让同一个响应里的字段来自不同事务。

    开头用 `get_content_item_for_update()` 取**行锁**：同一内容的重做
    由此串行化。代价是行锁在**整段执行期间**（含一次出网调用）持有 ——
    这是刻意的：`target_version_no`（幂等键的成分）必须在锁内取一次、
    到落库时**仍是同一个值**，否则"幂等键里的版本号"与"实际写下的版本号"
    会不一致，而两者不一致的表现是**重放时查不到已有上下文行** ⇒
    同一次生成被买两次（R5 失效，且不报错）。

    ## 步骤（每步都可判）

    1. 取内容行（加锁）→ 不存在即拒
    2. `GenerationRequest.from_payload(item.generation_request)` → 缺/坏即拒
    3. **全部** `task_kind` 先解析提示词（R6，在任何预占之前）
    4. 解析 `provider_id`（一处）与基线判据域（R4）
    5. 逐个 `task_kind`：幂等检查 → 预估 → 预占 → 渲染 → 派发 → 校验
       → 落库 → 结算

    第 3 步在预占**之前**是刻意的：没有激活提示词就不该花一分钱
    （§2.6 R6 "fail-closed"，且报错位置就是根因）。
    """
    # --- 本地 import：仓库模块与 versions.service（避免顶层依赖树变重）------
    from rednote.modules.baseline.repository import BaselineRepository
    from rednote.modules.billing.repository import BillingRepository
    from rednote.modules.gateway.repository import GatewayRepository
    from rednote.modules.prompts.repository import PromptRepository
    from rednote.modules.versions.repository import VersionRepository

    session = content_repo.session
    billing_repo = BillingRepository(session)
    prompt_repo = PromptRepository(session)
    version_repo = VersionRepository(session)
    gateway_repo = GatewayRepository(session)
    baseline_repo = BaselineRepository(session)

    # --- 1) 取内容行（行锁）------------------------------------------------
    item = content_repo.get_content_item_for_update(content_id)
    if item is None:
        raise ContentGenerationError(
            reason=REASON_CONTENT_NOT_FOUND,
            message=f"内容条目 {content_id} 不存在。",
        )
    project_id = item.project_id

    # --- 2) 解析请求载荷（fail-closed：缺键/坏键一律拒）-------------------
    try:
        request = GenerationRequest.from_payload(item.generation_request)
    except ValueError as exc:
        raise ContentGenerationError(
            reason=REASON_GENERATION_REQUEST_INVALID,
            message=(
                f"内容 {content_id} 的 generation_request 无法解析：{exc}。"
                "（处理器读到它是装配故障的信号：端点没写参数，"
                "或有人手工插了内容行。不给默认值 —— 默认值会把装配故障"
                "变成静默的错误行为。）"
            ),
        ) from exc

    # --- 3) 全部 task_kind 先解析提示词（R6：预占之前）---------------------
    resolved: list[tuple[str, Any]] = []
    for task_kind in request.task_kinds:
        # `resolve_active_version` 在任何一步查不到时抛 `PromptUnavailable`
        # （ValueError 族），本函数**不包装**它 —— `handlers` 按 `.reason`
        # 区分"没模板"与"没激活版本"。
        active = resolve_active_version(prompt_repo, task_kind=task_kind)
        resolved.append((task_kind, active))

    # --- 4) 模型档案（provider_id 的唯一解析点，见 `resolve_provider_id`）----
    provider_id = resolve_provider_id(
        gateway_repo, model_profile_id=request.model_profile_id
    )

    # --- 5) 基线判据域（R4 的两个入参 + 溯源用 baseline_id）----------------
    baseline_id = baseline_repo.active_baseline_id(project_id)
    known_claim_ids: frozenset[uuid.UUID] = frozenset()
    total_claim_count = 0
    if baseline_id is not None:
        baseline_project = content_repo.baseline_project(baseline_id)
        if baseline_project is not None and baseline_project != project_id:
            raise ContentGenerationError(
                reason=REASON_BASELINE_MISMATCH,
                message=(
                    f"项目 {project_id} 的生效基线 {baseline_id} 属于另一个项目"
                    f"（{baseline_project}）。跨项目基线是语义错误："
                    "基线承载本品牌的口径。"
                ),
            )
        if content_repo.baseline_exists(baseline_id):
            brief_version_id = content_repo.baseline_brief_version(baseline_id)
            known_claim_ids, total_claim_count = content_repo.claim_scope(
                brief_version_id
            )
            # ★ ISS-177：判据域为空必须拒 —— 但**只在基线真的关联了 Brief 时**。
            #
            # `claim_scope` 对两种情况都如实报 `(frozenset(), 0)`：① 基线关联了
            # 一个 Brief 但那个 Brief 一条 claim 都没有；② `brief_version_id
            # IS NULL`（合法的"这个项目本就没走 Brief 流程"，见
            # `repository.baseline_brief_version` 的 docstring）。区分这两个 0
            # 的职责在服务层 —— 只有这里知道基线的语义，仓储层如实报数就够了。
            #
            # 若不加 `brief_version_id is not None` 这个条件，② 也会被判成未就绪，
            # 整类不走 Brief 的项目将无法生成 —— 那比原来的洞更糟。
            if brief_version_id is not None and total_claim_count == 0:
                raise ContentGenerationError(
                    reason=REASON_BASELINE_NOT_READY,
                    message=(
                        f"项目 {project_id} 的生效基线 {baseline_id} 关联的 Brief"
                        f" 版本 {brief_version_id} 没有任何 claim —— 判据域为空。"
                        "此时放行会让 `validate_bindings` 拿到 (frozenset(), 0)："
                        "'绑定的 claim 必须存在'的判据域是空集，而'绑满即视为没绑'"
                        "那条规则的分母是 0 ⇒ 该规则不启用 ⇒ 模型绑 0 条就能通过，"
                        "R4 整条静默降级为'不判绑定'。"
                    ),
                )
        else:
            # `active_baseline_id` 指向一个不存在的基线 ⇒ 投影坏了。
            # 不当"没有基线"处理：那样会让 R4 静默降级成"不判绑定"。
            raise ContentGenerationError(
                reason=REASON_BASELINE_MISMATCH,
                message=(
                    f"项目 {project_id} 的 active_baseline_id={baseline_id} "
                    "指向的基线行不存在（投影与实现表不一致）。"
                ),
            )

    # --- 6) 目标版本号：锁内取一次，全程复用 ------------------------------
    target_version_no = content_repo.next_version_no(content_item_id=content_id)
    current_lock_version = int(item.lock_version)

    # --- 7) 逐 task_kind 执行 ---------------------------------------------
    pending_reservations: list[uuid.UUID] = []
    settled_reservations: list[uuid.UUID] = []
    usage_pending = False
    blueprint_id: uuid.UUID | None = None
    blueprint_fields: dict[str, str] | None = None

    try:
        for task_kind, active in resolved:
            idempotency_key = generation_idempotency_key(
                content_id=content_id,
                content_version_no=target_version_no,
                task_kind=task_kind,
            )

            # 幂等：同一次生成（同一内容 + 同一目标版本 + 同一 kind）已经跑过
            # ⇒ 直接跳过，不预占、不派发。这是 R5 的存储侧一半。
            if (
                content_repo.find_generation_context(
                    content_id=content_id, idempotency_key=idempotency_key
                )
                is not None
            ):
                _log.info(
                    "内容 %s 的 %s 已存在生成上下文（幂等键 %s），跳过。",
                    content_id,
                    task_kind,
                    idempotency_key,
                )
                continue

            # --- 预估 + 预占（R2：严格早于派发）---------------------------
            estimate = estimate_generation_cost(
                billing_repo,
                project_id=project_id,
                task_kind=task_kind,
                # 任务粒度是单条内容 ⇒ 恒为 1。批量请求的整批预估在端点侧。
                content_count=1,
                batch_kind=request.batch_kind,
                provider_id=provider_id,
                model_profile_id=request.model_profile_id,
                actor_id=actor_id,
                request_id=request_id,
            )
            reservation = reserve_generation_budget(
                billing_repo,
                project_id=project_id,
                estimate=estimate,
                content_id=content_id,
                actor_id=actor_id,
                request_id=request_id,
                note=(
                    f"WP26 内容生成：scope={request.scope}，"
                    f"task_kind={task_kind}，目标版本 v{target_version_no}"
                ),
            )
            pending_reservations.append(reservation.id)

            # --- 渲染提示词（缺值即抛 PromptUnavailable）-------------------
            values = generation_prompt_values(
                content_id=content_id,
                project_id=project_id,
                batch_kind=request.batch_kind,
                # 蓝图只给文案调用：结构调用时它还不存在。
                blueprint=blueprint_fields if task_kind == TASK_CONTENT_COPY else None,
            )
            prompt = render_system_prompt(active, values=values)

            # --- 派发 -----------------------------------------------------
            dispatch = generator.generate(
                task_kind=task_kind,
                prompt=prompt,
                prompt_version_id=active.version_id,
                content_id=content_id,
                content_version_no=target_version_no,
                model_profile_id=request.model_profile_id,
                project_id=project_id,
            )

            # --- 校验（R1 的唯一落点）-------------------------------------
            if task_kind == TASK_CONTENT_STRUCTURE:
                blueprint_id, blueprint_fields = _apply_blueprint(
                    content_repo,
                    content_id=content_id,
                    dispatch=dispatch,
                    created_by=actor_id,
                )
                content_repo.set_state(
                    content_id=content_id, state=CONTENT_STATE_STRUCTURED
                )
                reloaded = content_repo.reload_content_item(content_id)
                current_lock_version = (
                    int(reloaded.lock_version) if reloaded is not None else current_lock_version
                )
                version_id: uuid.UUID | None = None
                # 结构那一路没有版本行 ⇒ 没有一侧需要提前知道上下文 id，
                # 走列的 `server_default=gen_random_uuid()`。
                context_id: uuid.UUID | None = None
            else:
                # ★ 环 2 的**版本侧 id 必须先定**：`content_versions` 不可变
                # （事后 UPDATE 撞 `23001`），所以 `generation_context_id`
                # 只能在 INSERT 时给出。而上下文行要引用本函数的产出
                # （`version_id` 是 `register_version` 现取的）——
                # 两条 FK 都是 DEFERRABLE，COMMIT 期一起结算，
                # 因此只要**这一个 id 提前生成**，环就能解开。
                context_id = uuid.uuid4()
                # E3 的"上一版"必须在 `_apply_copy` **之前**取：之后 `latest_version`
                # 就是刚插入的这一版。首版（`target_version_no == 1`）没有上一版。
                previous_version = (
                    content_repo.latest_version(content_item_id=content_id)
                    if target_version_no > 1
                    else None
                )
                version_id, copy_title = _apply_copy(
                    content_repo,
                    version_repo=version_repo,
                    content_id=content_id,
                    project_id=project_id,
                    target_version_no=target_version_no,
                    dispatch=dispatch,
                    known_claim_ids=known_claim_ids,
                    total_claim_count=total_claim_count,
                    blueprint_id=blueprint_id,
                    generation_context_id=context_id,
                    created_by=actor_id,
                )
                # E1/E2：版本节点已注册（派发已结束，不在网关调用期间写边）。
                _link_generation_edges(
                    version_repo,
                    version_id=version_id,
                    prompt_version_id=active.version_id,
                    baseline_id=baseline_id,
                )
                # E3：再生成（v2 及以后）也是一次修订，与人工编辑 / 采纳标题同口径，
                # 只连上一版，不复制上游 E1/E2（用户 2026-10-20 已定）。
                if previous_version is not None:
                    _link_revision_edge(
                        version_repo,
                        version_id=version_id,
                        previous_version_id=previous_version.id,
                    )
                affected = content_repo.advance_projection(
                    content_id=content_id,
                    expected_lock_version=current_lock_version,
                    version_id=version_id,
                    version_no=target_version_no,
                    title=copy_title,
                    state=CONTENT_STATE_GENERATED,
                )
                if affected == 0:
                    # 持行锁的前提下不该发生（那是"并发改投影"的形态）。
                    # 抛而不是忽略：忽略会让投影落后于实际版本，
                    # 而列表页显示旧版本号**不报错**。
                    raise ContentGenerationError(
                        reason=REASON_PROJECTION_CONFLICT,
                        message=(
                            f"内容 {content_id} 的投影推进失败"
                            f"（期望 lock_version={current_lock_version}）。"
                            "持行锁的前提下这意味着投影被本事务之外改写。"
                        ),
                    )
                reloaded = content_repo.reload_content_item(content_id)
                current_lock_version = (
                    int(reloaded.lock_version) if reloaded is not None else current_lock_version
                )

            # --- 生成上下文（OP-160 的落点）-------------------------------
            # ★ `context_id` 是文案那一路**提前生成**的那个 id（环 2 的另一半）；
            # 结构那一路为 `None` ⇒ 走列的服务端默认值。
            content_repo.insert_generation_context(
                project_id=project_id,
                content_id=content_id,
                content_version_id=version_id,
                task_kind=task_kind,
                idempotency_key=idempotency_key,
                context_id=context_id,
                prompt_template_id=active.template_id,
                prompt_version_id=active.version_id,
                baseline_id=baseline_id,
                model_profile_id=request.model_profile_id,
                provider_call_id=dispatch.provider_call_id,
                created_by=actor_id,
            )

            # --- 结算（金额口径见 `billing_bridge`：本包按预估金额结算）----
            # 包在 savepoint 里：结算失败**不回滚**已经落库的内容
            # （内容在、版本在、钱已花），只把这一笔记成"待结算"。
            settled = _try_settle(
                billing_repo,
                reservation_id=reservation.id,
                amount=estimate.total_amount,
                note=(
                    f"WP26 内容生成结算：task_kind={task_kind}，"
                    f"按预估金额结算（网关不返回真实成本，见 gateway_bridge）"
                ),
                actor_id=actor_id,
                request_id=request_id,
                content_repo=content_repo,
            )
            if settled:
                pending_reservations.remove(reservation.id)
                settled_reservations.append(reservation.id)
            else:
                usage_pending = True

    except Exception:
        # --- 失败路径：释放全部**未结算**的预占，然后原样重抛 ---------------
        # 原样重抛（不包装）：`handlers` 要按类型区分"输入不对"与"通道不通"。
        for reservation_id in list(pending_reservations):
            try:
                release_generation_budget(
                    billing_repo,
                    reservation_id=reservation_id,
                    reason="WP26 内容生成失败，全额释放（未产出内容版本）",
                    actor_id=actor_id,
                    request_id=request_id,
                )
            except Exception:  # pragma: no cover - 释放失败不应掩盖原始异常
                _log.exception(
                    "释放预占 %s 失败（内容 %s）。原始异常优先上报。",
                    reservation_id,
                    content_id,
                )
            pending_reservations.remove(reservation_id)

        # 内容落 FAILED：它是"这一条生成没成功"的可见痕迹。
        # 不动投影（`set_state` 与 `advance_projection` 分开的正是这个理由）。
        try:
            content_repo.set_state(content_id=content_id, state=CONTENT_STATE_FAILED)
        except Exception:  # pragma: no cover - 状态落库失败不应掩盖原始异常
            _log.exception("内容 %s 落 FAILED 失败。原始异常优先上报。", content_id)
        raise

    detail = read_content_detail(content_repo, content_id=content_id)
    return GenerationOutcome(
        detail=detail,
        usage_pending=usage_pending,
        reservation_ids=tuple(settled_reservations),
    )


# ---------------------------------------------------------------------------
# 依赖边（供 WP34 回退影响图遍历）
# ---------------------------------------------------------------------------


def _link_generation_edges(
    version_repo: VersionRepository,
    *,
    version_id: uuid.UUID,
    prompt_version_id: uuid.UUID | None,
    baseline_id: uuid.UUID | None,
) -> None:
    """写 E1（CONTENT→PROMPT）/ E2（CONTENT→BASELINE）依赖边。

    `version_id` 是内容版本的节点 id（须已 `register_version`）；
    `baseline_id` 即 `production_baselines.id`，同时是 BASELINE 的版本节点 id。
    与版本注册同一事务；`link_dependency` 出错直接上抛让整个事务回滚，不吞。
    可空外键判空跳过。
    """
    from rednote.modules.versions.service import link_dependency

    if prompt_version_id is not None:
        link_dependency(
            version_repo,
            parent_version_id=version_id,
            dependency_version_id=prompt_version_id,
            purpose=DEPENDENCY_PURPOSE_GENERATION_PROMPT,
        )
    if baseline_id is not None:
        link_dependency(
            version_repo,
            parent_version_id=version_id,
            dependency_version_id=baseline_id,
            purpose=DEPENDENCY_PURPOSE_GENERATION_BASELINE,
        )


def _link_revision_edge(
    version_repo: VersionRepository,
    *,
    version_id: uuid.UUID,
    previous_version_id: uuid.UUID,
) -> None:
    """写 E3（新内容版本→上一版内容版本）。只写这一条，不复制上一版的 E1/E2。"""
    from rednote.modules.versions.service import link_dependency

    link_dependency(
        version_repo,
        parent_version_id=version_id,
        dependency_version_id=previous_version_id,
        purpose=DEPENDENCY_PURPOSE_CONTENT_REVISION,
    )


# ---------------------------------------------------------------------------
# 两个 task_kind 各自的"校验 → 落库"
# ---------------------------------------------------------------------------


def _apply_blueprint(
    content_repo: ContentRepository,
    *,
    content_id: uuid.UUID,
    dispatch: GenerationDispatch,
    created_by: uuid.UUID | None,
) -> tuple[uuid.UUID, dict[str, str]]:
    """校验蓝图并落库，返回 `(blueprint_id, fields)`。

    `fields` 的键取自 `BLUEPRINT_FIELDS`（**不**用 `dataclasses.asdict`）：
    这样"校验器查了哪些字段"与"落库写了哪些字段"由**同一个常量**决定，
    而 `insert_blueprint` 手工展开的 9 个列名与 `models.py` 的列名
    由 `test_content_wp26.py` 的"三方集合相等"判据对账。
    """
    draft = validate_blueprint(dispatch.structured_output)
    fields = {name: getattr(draft, name) for name in BLUEPRINT_FIELDS}
    row = content_repo.insert_blueprint(
        content_item_id=content_id, fields=fields, created_by=created_by
    )
    return row.id, fields


def _apply_copy(
    content_repo: ContentRepository,
    *,
    version_repo: VersionRepository,
    content_id: uuid.UUID,
    project_id: uuid.UUID,
    target_version_no: int,
    dispatch: GenerationDispatch,
    known_claim_ids: frozenset[uuid.UUID],
    total_claim_count: int,
    blueprint_id: uuid.UUID | None,
    generation_context_id: uuid.UUID,
    created_by: uuid.UUID | None,
) -> tuple[uuid.UUID, str]:
    """校验文案、注册版本节点、落内容版本与标题候选。返回 `(version_id, title)`。

    ## 三段顺序不能换

    1. `validate_copy(...)` —— **三合一的唯一闸门**（标题/正文/标签 +
       绑定 + 标题候选都在它内部判完）。任何一项不合法都在此抛出，
       **在任何写入之前**。
    2. `register_version(...)` —— 注册表节点（WP11）。它**自己取**
       `node_id` 与 `revision_no`：本包不传 `revision_no`（照范本
       `planning/service.py:918`），`entity_id` 传**内容身份**
       （`content_items.id`），不是版本 id。
    3. `insert_version(version_id=registered.node_id, ...)` —— 领域行，
       `id` 必须等于节点 id（OP-88 正向一半）。两者同事务插入，
       `content_versions.id` 的 FK 是 `DEFERRABLE INITIALLY DEFERRED`，
       因此顺序无关。

    `version_no` 用的是**调用方在行锁内取好的** `target_version_no`，
    而不是在这里重取一次。`next_version_no` 的 docstring 要求"取号与插入
    在同一个 `savepoint()` 内"，其**目的**是"撞唯一约束时只回滚这一段、
    调用方可以重取号再试"。本模块用**更强的手段**达到同一目的：
    `execute_generation` 开头取了内容行的 `FOR UPDATE` 行锁，同一内容的
    重做由此串行化，取号到插入之间**不可能**出现第二次插入。因此
    `savepoint()` 仍包住插入段（防"意外的唯一冲突把整个事务打成
    aborted"），但**不重取号** —— 重取会让"幂等键里的版本号"与
    "实际写下的版本号"分叉，而那正是 R5 失效的形态
    （重放查不到已有上下文行 ⇒ 同一次生成被买两次，且不报错）。

    ## 为什么 `payload_hash` 用注册表返回的那个

    `register_version` 内部已经算过 `payload_hash`。本包**不自己再算一次**
    （`canonical_payload_hash(payload)`）—— 两处算同一个值就是两个来源，
    而分叉的表现是"注册表说 hash 是 A、领域表说 hash 是 B"，
    对账时无法判断哪一侧是坏的。用注册表返回的值：**唯一来源**。

    ## ★ `generation_context_id` 必须由**调用方先定好**（环 2 的唯一解）

    `content_versions` 是**不可变**的（触发器拦 UPDATE，撞 `23001`），因此
    `generation_context_id` 只能在 **INSERT 那一刻**给出 —— 事后回填**必然失败**。

    而环 2 的另一侧 `generation_contexts.content_version_id` 又要引用本函数
    产出的 `version_id`。两条 FK 都是 `DEFERRABLE INITIALLY DEFERRED`
    （`models.py` docstring 七），所以**两侧都可以先插、COMMIT 期一起结算**。
    这在插入顺序上给出两条都可行的路：

    | 顺序 | 可行性 |
    |---|---|
    | ① 先插版本（`generation_context_id=<ctx>`）→ 再插上下文（`content_version_id=<ver>`） | ✅ 本实现的顺序 |
    | ② 先插上下文（`content_version_id=NULL`）→ 再插版本 | ✅ 但上下文行随后仍要**回填** `content_version_id`，而该表也不可变 ⇒ 走不通 |

    ⇒ 实际只有 ① 一条路，而 ① 要求上下文行的 id **在本函数调用之前**就已知。
    `insert_generation_context(context_id=...)` 正是为此提供的入参
    （`repository.py:769-784` 的 docstring 已论证）。

    ## 由此得到的调用契约（**这是修掉缺陷 2 的关键**）

    调用方 `execute_generation` 必须：

    1. 在**派发之后、落库之前**生成 `context_id = uuid.uuid4()`（Python 侧）；
    2. 把 `generation_context_id=context_id` 传给本函数（版本行 INSERT 时带上）；
    3. 再用**同一个** `context_id` 调 `insert_generation_context(context_id=...)`。

    顺序 ③ **必须在 ① 之后**（上下文行带着 `provider_call_id` /
    `dispatch.state` 等本次执行的量，只有派发完成后才有）。两侧 FK 都延迟了，
    所以这个顺序不是**被 FK 逼出来的**，而是被"数据的可得时点"逼出来的：
    固定一个顺序，让"到底先插哪边"不再是一个每处都要重新判断的问题。
    """
    # 本地 import（R7 同款理由：`versions.service` 拖 `versions` 的模型与注册表）。
    from rednote.modules.versions.models import ENTITY_TYPE_CONTENT
    from rednote.modules.versions.service import register_version

    copy = validate_copy(
        dispatch.structured_output,
        known_claim_ids=known_claim_ids,
        total_claim_count=total_claim_count,
    )

    payload: dict[str, Any] = {
        "title": copy.title,
        "body": copy.body,
        "tags": list(copy.tags),
    }

    registered = register_version(
        version_repo,
        entity_type=ENTITY_TYPE_CONTENT,
        entity_id=content_id,
        project_id=project_id,
        payload=payload,
        created_by=created_by,
    )
    version_id = registered.node_id

    with content_repo.savepoint():
        content_repo.insert_version(
            version_id=version_id,
            content_item_id=content_id,
            version_no=target_version_no,
            title=copy.title,
            body=copy.body,
            tags=copy.tags,
            bindings=[
                {
                    BINDING_KEY_PARAGRAPH_INDEX: binding.paragraph_index,
                    # 存**字符串**而不是 UUID 对象：`bindings` 是 JSONB，
                    # 没有类型信息，`json.dumps` 对 uuid.UUID 直接报
                    # `TypeError: Object of type UUID is not JSON serializable`。
                    # 而列注释（`models.py:508`）写的形状就是 `"<uuid>"`。
                    # 序列化在这里做一次、且只有这一处（另一处写它是采纳端点，
                    # 走的是"原样复用上一版"而不是重新序列化）。
                    BINDING_KEY_CLAIM_ID: (
                        None if binding.claim_id is None else str(binding.claim_id)
                    ),
                    BINDING_KEY_REASON: binding.reason,
                    BINDING_KEY_TEXT: binding.text,
                }
                for binding in copy.bindings
            ],
            payload=payload,
            payload_hash=registered.payload_hash,
            blueprint_id=blueprint_id,
            # 生成产生的版本行**恒为 None**：采纳端点是唯一写它的地方
            # （`models.py` docstring 六）。
            adopted_title_candidate_id=None,
            # ★ 环 2 的版本侧。必须在 INSERT 时给出（不可变表，事后补不了）；
            # 上下文行由调用方用**同一个 id** 随后插入。
            generation_context_id=generation_context_id,
            created_by=created_by,
        )
        content_repo.insert_title_candidates(
            content_version_id=version_id,
            candidates=[
                {
                    "title": candidate.title,
                    "scores": dict(candidate.scores),
                    "keywords": list(candidate.keywords),
                    "highlight": candidate.highlight,
                    "risk_note": candidate.risk_note,
                }
                for candidate in copy.title_candidates
            ],
        )

    return version_id, copy.title


def _try_settle(
    billing_repo: BillingRepository,
    *,
    reservation_id: uuid.UUID,
    amount: Any,
    note: str,
    actor_id: uuid.UUID | None,
    request_id: str | None,
    content_repo: ContentRepository,
) -> bool:
    """结算，包在 savepoint 里。返回是否成功。

    **失败不回滚内容**：内容与版本已经落库、钱已经花掉，回滚它们
    只会造出"花了钱但没有产出"的状态。这里只把这一笔记成待结算，
    由调用方上报 `usage_pending`（运维按 `budget_reservations.ref` 对账）。
    """
    try:
        with content_repo.savepoint():
            settle_generation_budget(
                billing_repo,
                reservation_id=reservation_id,
                actual_amount=amount,
                note=note,
                actor_id=actor_id,
                request_id=request_id,
            )
        return True
    except Exception:
        _log.exception(
            "结算预占 %s 失败（内容已落库，钱可能已花）。记为待结算。",
            reservation_id,
        )
        return False


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


def read_content_detail(
    content_repo: ContentRepository,
    *,
    content_id: uuid.UUID,
) -> ContentDetail:
    """读一条内容的完整详情（`GET /contents/{id}`）。

    一次给出：身份 + 当前版本（payload / 绑定）+ 蓝图 + 标题候选 +
    生成上下文。**全部复用同一个 session**（`content_repo.session`）——
    分属本模块五张表与 WP11 的 `version_nodes`，不同事务会让同一个响应
    里的字段来自不同时刻。

    蓝图的取法有**两级**：
    - 当前版本有 `blueprint_id` ⇒ 取它（那一版依据的蓝图）；
    - 否则取本内容**最近**的一条蓝图 —— 这让 `STRUCTURED`（结构成功、
      文案未完成）这个中间态也能看到蓝图，而"看不到"会让前端以为
      结构根本没跑。

    内容不存在 ⇒ 抛 `ContentGenerationError(CONTENT_NOT_FOUND)`。
    （`api.py` 把它翻成 404。）
    """
    item = content_repo.get_content_item(content_id)
    if item is None:
        raise ContentGenerationError(
            reason=REASON_CONTENT_NOT_FOUND,
            message=f"内容条目 {content_id} 不存在。",
        )

    version = (
        content_repo.get_version(item.current_version_id)
        if item.current_version_id is not None
        else None
    )

    blueprint_row = None
    if version is not None and version.blueprint_id is not None:
        blueprint_row = content_repo.get_blueprint(version.blueprint_id)
    if blueprint_row is None:
        blueprint_row = content_repo.latest_blueprint(content_item_id=content_id)

    candidates = (
        content_repo.list_title_candidates(content_version_id=version.id)
        if version is not None
        else []
    )
    bindings = tuple(version.bindings or ()) if version is not None else ()

    return ContentDetail(
        id=item.id,
        project_id=item.project_id,
        state=item.state,
        current_version_id=item.current_version_id,
        current_version_no=int(item.current_version_no),
        payload=dict(version.payload) if version is not None else None,
        blueprint=(
            {name: getattr(blueprint_row, name) for name in BLUEPRINT_FIELDS}
            if blueprint_row is not None
            else None
        ),
        title_candidates=tuple(
            {
                "id": candidate.id,
                "candidate_index": candidate.candidate_index,
                "title": candidate.title,
                "scores": dict(candidate.scores or {}),
                "keywords": list(candidate.keywords or ()),
                "highlight": candidate.highlight,
                "risk_note": candidate.risk_note,
            }
            for candidate in candidates
        ),
        bindings=tuple(dict(binding) for binding in bindings),
        contexts=_context_views(content_repo, content_id=content_id),
    )


def list_contents(
    content_repo: ContentRepository,
    *,
    project_id: uuid.UUID,
    limit: int,
    offset: int,
) -> tuple[tuple[ContentSummary, ...], int]:
    """按项目的内容列表（`GET /projects/{projectId}/contents`）。返回 `(行, 总数)`。

    ## 为什么把 `total` 一起返回（而不是只回行）

    端点是**游标分页**（照 `baseline/api.py` 先例：取全量 + `paginate_items`
    在内存里切片）。`total` 不进响应体（`PageInfo` 只有 `nextCursor`/`hasMore`，
    硬塞会被 `response_model` 剥掉 —— `platform/pagination.py:242-253` 的教训），
    但**服务层仍要给出它**：否则"这一页之后还有没有"这个判断只能靠
    `len(rows) == limit` 猜，而"正好整页"时猜错的方向是"显示还有更多"。

    ## 为什么不在服务层分页

    `limit`/`offset` 原样下推给仓储（`list_content_items` 已经带
    `order_by(updated_at.desc(), id)`）。**排序键由仓储定**，不由端点定 ——
    让端点在内存里重排一次，就会出现"仓储按 A 排、端点按 B 排"，
    而分页游标是按端点的排序编码的（`paginate_items` 的 `spec`），
    两者不一致的表现是**翻页重复或漏行**（不报错）。

    内容条目**不存在不算错误**：项目里一条内容都没有时返回空元组。
    这与 `read_content_detail` 的 `CONTENT_NOT_FOUND` 不同 ——
    那是"点开了一个不存在的资源"，这是"这个项目底下就是空的"。
    """
    rows = content_repo.list_content_items(
        project_id=project_id, limit=limit, offset=offset
    )
    total = content_repo.count_content_items(project_id=project_id)
    return (
        tuple(
            ContentSummary(
                id=row.id,
                project_id=row.project_id,
                title=row.title,
                state=row.state,
                current_version_no=int(row.current_version_no),
                created_at=row.created_at,
                updated_at=row.updated_at,
                scene_key=row.scene_key,
                form_key=row.form_key,
            )
            for row in rows
        ),
        total,
    )


def read_version_history(
    content_repo: ContentRepository,
    *,
    content_id: uuid.UUID,
    limit: int,
    offset: int,
) -> tuple[tuple[ContentVersionSummary, ...], int]:
    """一条内容的版本历史（`GET /contents/{contentId}/versions`）。`(行, 总数)`。

    ## 内容不存在 ⇒ 抛，而不是回空列表

    "空版本历史"与"内容根本不存在"是**两件不同的事**，而空列表让两者同形
    —— 前端会把一个拼错的内容 id 显示成"这条内容还没有版本"。
    因此先判内容在不在（`get_content_item`），不存在抛
    `REASON_CONTENT_NOT_FOUND`（`api.py` 翻成 404）。

    已存在但还没有任何版本（`current_version_no == 0`）⇒ 合法的空历史。

    ## 为什么这里给 `adopted_title_candidate_id`

    `types.ContentVersionSummary` 的 docstring 已论证：采纳记在版本行上，
    于是"这个候选有没有被采纳过"只能**反查版本** —— 版本历史正是那个反查面。
    少给这一列，前端就只能拿标题做字符串比较，而两个候选取同一个标题
    是完全合法的 ⇒ 字符串相等**不等于**被采纳。本条判据的载体是
    `test_content_wp26.py` 的"采纳后历史里那一版的 `adoptedTitleCandidateId`
    等于被采纳候选"用例。
    """
    item = content_repo.get_content_item(content_id)
    if item is None:
        raise ContentGenerationError(
            reason=REASON_CONTENT_NOT_FOUND,
            message=f"内容条目 {content_id} 不存在。",
        )

    rows = content_repo.list_versions(
        content_item_id=content_id, limit=limit, offset=offset
    )
    total = content_repo.count_versions(content_item_id=content_id)
    return (
        tuple(
            ContentVersionSummary(
                id=row.id,
                version_no=int(row.version_no),
                # `title` 在列上是 NOT NULL（`models.py` `title_not_empty`），
                # 但 schema_version 演进后旧行可能……不，这里不设 `or ""`：
                # 造一个默认值会把"读到坏数据"变成"读到空标题"（静默）。
                title=row.title,
                blueprint_id=row.blueprint_id,
                adopted_title_candidate_id=row.adopted_title_candidate_id,
                generation_context_id=row.generation_context_id,
                payload_hash=row.payload_hash,
                schema_version=int(row.schema_version),
                created_at=row.created_at,
                created_by=row.created_by,
            )
            for row in rows
        ),
        total,
    )


def read_generation_contexts_by_prompt_version(
    content_repo: ContentRepository,
    *,
    prompt_version_id: uuid.UUID,
    limit: int,
    offset: int,
) -> list[GenerationContextView]:
    """**OP-160 的影响面查询**：某一版提示词被哪些生成消费过。

    与 `read_content_detail` 分开两个函数（而不是同一个带开关的）：
    两者的**权限面与分页语义**不同（详情是单条资源，这里是跨内容的列表），
    合成一个会让"分页参数忘了透传"变成静默返回第一页。
    """
    rows = content_repo.list_contexts_by_prompt_version(
        prompt_version_id=prompt_version_id, limit=limit, offset=offset
    )
    return [_context_view(row) for row in rows]


# ---------------------------------------------------------------------------
# 写：发起（物化身份行 + 入队参数）
# ---------------------------------------------------------------------------


def start_generation(
    content_repo: ContentRepository,
    *,
    project_id: uuid.UUID,
    request: GenerationRequest,
    content_count: int,
    idempotency_key: str,
    created_by: uuid.UUID | None = None,
) -> tuple[tuple[ContentItem, ...], bool]:
    """发起一次内容生成：物化 `content_count` 条身份行，返回 `(条目, created)`。

    ## 这一层**不预占**（R2 的边界）

    预占在执行阶段（`execute_generation`），理由见模块 docstring
    「预占为什么必须在执行阶段」。本函数只做**与钱无关**的事：
    建身份行、把生成参数写进去、把幂等键留在行上。

    端点接着对每条内容各入一个任务（`jobs_service.enqueue_job(content_id=…)`）——
    入队也不在这里，因为入队要 `JobRepository` 而本函数只收 `ContentRepository`：
    把两个仓库都塞进来会让"发起"变成又一个能碰 `jobs` 的地方
    （本包不改 `jobs` 的资产，接缝冻结）。

    ## 幂等：同一个请求键第二次到来 ⇒ 复用第一批，不新建

    先按 `last_idempotency_key` 找回那一批（`list_content_items_by_idempotency`）。
    命中 ⇒ `created=False`，原样返回既有行。

    **为什么键落在 `content_items` 而不是另建一张请求表**：
    见 `types.py` 的「★ 一次生成请求的载荷」整节 —— 与 WP18 的
    `brief_drafts.enrichment_request` 同一处置。

    **重放时参数不覆盖**：命中即返回，**不**调 `set_generation_request`。
    覆盖会让"我第一次说 3 条、第二次说 5 条"这种**同键不同体**的请求
    悄悄改掉第一批的参数，而那一批的任务可能已经跑完了 ——
    改了参数、结果还是旧的，且**没有任何报错**。同键不同体是**客户端的错**，
    正确处置是让端点按 `IDEMPOTENCY_CONFLICT` 拒（本函数不做 HTTP 判断，
    只保证"命中即原样返回"这个不含歧义的行为）。

    ## 场景 / 形式（接缝 §5.15）

    `request.scene_key` / `request.form_key`（可选，默认 None）随身份行
    同一次 INSERT 落到 `content_items` 的同名列，不进 `generation_request`。
    **幂等语义不变**：命中即原样返回既有行，不覆盖这两列。重放时两列与
    首批不一致（含"首批有、重放没带"或反之）属于同键不同体，由端点按
    `IDEMPOTENCY_CONFLICT` 拒，与载荷、条数不一致同一惯例；两次都不带则与旧行为完全一致。

    `content_count` 的下界由端点按请求体校验（≥1）；这里再判一次
    —— 0 条会让端点入 0 个任务而**返回 202**，调用方以为提交成功。
    """
    if content_count < 1:
        raise ContentGenerationError(
            reason=REASON_GENERATION_REQUEST_INVALID,
            message=(
                f"要生成的内容条数必须 ≥ 1，实际是 {content_count}。"
                "（0 条会建 0 个任务却返回 202 —— 调用方以为提交了）"
            ),
        )

    existing = content_repo.list_content_items_by_idempotency(
        project_id=project_id, idempotency_key=idempotency_key
    )
    if existing:
        return tuple(existing), False

    payload = request.as_payload()
    created_rows = tuple(
        content_repo.insert_content_item(
            project_id=project_id,
            created_by=created_by,
            idempotency_key=idempotency_key,
            # 与身份行**同一次 INSERT** 写入：分开写会造出
            # "身份在、参数还没写"的中间态，而那个中间态恰好是
            # 处理器读到的样子（读到 None ⇒ 判装配故障）。
            generation_request=payload,
            scene_key=request.scene_key,
            form_key=request.form_key,
        )
        for _ in range(content_count)
    )
    return created_rows, True


def prepare_regeneration(
    content_repo: ContentRepository,
    *,
    content_id: uuid.UUID,
    request: GenerationRequest,
    current_blueprint_id: uuid.UUID | None,
) -> dict[str, Any]:
    """局部重做（`POST /contents/{contentId}/regenerations`）的前置。

    重做**不建身份行**（内容已存在），只做两件事：

    1. 把新的生成参数覆盖上去（`set_generation_request`）——
       "仅标题"→"整篇"本来就要换参数，`repository` 的 docstring 已论证
       覆盖是**正确**行为（它是"最后一次意图"，不是历史事实；
       历史事实在每次生成一行的 `generation_contexts`）。
    2. 校验范围与蓝图的**配对**（`REASON_SCOPE_REQUIRES_BLUEPRINT`）。

    ## 为什么要打这条配对判据

    `SCOPE_COPY` / `SCOPE_TITLE` 只跑 `CONTENT_COPY`，而文案调用要拿
    **蓝图**渲染提示词（`generation_prompt_values(blueprint=…)`）。
    若这条内容从来只跑过"仅标题"、没有任何蓝图行，那么文案调用会在
    `render_system_prompt` 处抛"缺 `blueprint` 值" —— 报错点是提示词渲染，
    而根因是"没有蓝图"。两者隔了一层，排障会去查提示词模板。

    本函数在**发起**那一刻就拒（错误码 + 可读理由），让报错点即根因。
    `FULL` 不受限：它自己会跑 `CONTENT_STRUCTURE` 产出蓝图。

    返回回填给响应的 dict（**如实**说明实际重做的范围）——
    用 `task_kinds_for_scope(request.scope)` 而不是在端点里再写一份映射
    （`types.normalize_scope` 的 docstring 已论证分叉的后果是**响应撒谎**）。
    """
    item = content_repo.get_content_item(content_id)
    if item is None:
        raise ContentGenerationError(
            reason=REASON_CONTENT_NOT_FOUND,
            message=f"内容条目 {content_id} 不存在。",
        )

    task_kinds = task_kinds_for_scope(request.scope)
    if TASK_CONTENT_STRUCTURE not in task_kinds and current_blueprint_id is None:
        raise ContentGenerationError(
            reason=REASON_SCOPE_REQUIRES_BLUEPRINT,
            message=(
                f"内容 {content_id} 尚无蓝图，无法只重做 {request.scope}"
                f"（该范围只跑 {list(task_kinds)}，而文案调用需要蓝图"
                "渲染提示词）。请改用 FULL 范围（它会先跑结构）。"
            ),
        )

    content_repo.set_generation_request(
        content_id=content_id, request=request.as_payload()
    )
    return {
        "contentId": str(content_id),
        # `request.scope` 已经是归一的（构造器保证），此处直接用。
        "scope": request.scope,
        "taskKinds": list(task_kinds),
    }


# ---------------------------------------------------------------------------
# 写：采纳标题候选（**不调模型、不计费**）
# ---------------------------------------------------------------------------


def adopt_title_candidate(
    content_repo: ContentRepository,
    *,
    version_repo: VersionRepository,
    content_id: uuid.UUID,
    candidate_id: uuid.UUID,
    created_by: uuid.UUID | None = None,
) -> ContentVersionSummary:
    """采纳一个标题候选 ⇒ **追加一版**（不是 UPDATE）。返回新版本的摘要。

    ## 为什么是"追加一版"（`models.py` docstring 六 原文）

    `content_versions` 是**不可变**的，事后 `UPDATE adopted_title_candidate_id`
    必撞 `23001`。而"采纳"发生在生成之后，那一刻版本行早已存在。
    三条出路里选的是"新版本行带着 `adopted_title_candidate_id`"：
    它与「仅标题重做」**同形**（都是一版），列表页照样显示，没有新增形态。

    ## 落库规则（逐条照 docstring 六）

    | 字段 | 新版本行取什么 |
    |---|---|
    | `title` | **被采纳候选的标题** |
    | `body` / `tags` / `bindings` / `blueprint_id` / `generation_context_id` | **原样复用上一版** |
    | `adopted_title_candidate_id` | 被采纳的候选 id |

    **没有模型调用 ⇒ 没有新的 `generation_contexts` 行** ——
    这也解释了 `content_versions.generation_context_id` 为什么可空。
    **计费路径为空**（`billing_bridge` 不参与）。

    ## 两条跨内容不法只能在本层判

    ① 候选不存在 ⇒ `TITLE_CANDIDATE_NOT_FOUND`；
    ② 候选存在但属于**别的内容** ⇒ `TITLE_CANDIDATE_MISMATCH`。
    ②这条**FK 管不住**（FK 只保证"候选存在"），落点是
    `repository.candidate_owner` + `ddl.py` 触发器 3.2。
    本层给的是**可读的 4xx**，DDL 给的是"绕过本层也拦得住"。

    ## 没有上一版时拒（不用空 body 造版本）

    内容一条版本都没有（`latest_version` 为 None）⇒ 拒。用空 body 造一版
    会让"采纳"这个动作产出一条**正文为空**的内容 —— 而它在列表页
    与正常内容长得一样。
    """
    item = content_repo.get_content_item_for_update(content_id)
    if item is None:
        raise ContentGenerationError(
            reason=REASON_CONTENT_NOT_FOUND,
            message=f"内容条目 {content_id} 不存在。",
        )

    owner = content_repo.candidate_owner(candidate_id)
    if owner is None:
        raise ContentGenerationError(
            reason=REASON_TITLE_CANDIDATE_NOT_FOUND,
            message=f"标题候选 {candidate_id} 不存在。",
        )
    if owner != content_id:
        # 跨内容采纳：AQ-01 要按内容算事实正确率，采纳别人家的标题
        # 会让"这条内容的标题是哪来的"无法回答。
        raise ContentGenerationError(
            reason=REASON_TITLE_CANDIDATE_MISMATCH,
            message=(
                f"标题候选 {candidate_id} 属于另一条内容（{owner}），"
                f"不能采纳到内容 {content_id} 上。"
            ),
        )

    new_title = content_repo.candidate_title(candidate_id)
    if new_title is None:  # pragma: no cover - 被 CHECK `title_not_empty` 挡住
        raise ContentGenerationError(
            reason=REASON_TITLE_CANDIDATE_NOT_FOUND,
            message=(
                f"标题候选 {candidate_id} 的标题为空。"
                "（该列有 CHECK 约束，此行说明库里的数据被绕过本层写坏了）"
            ),
        )

    previous = content_repo.latest_version(content_item_id=content_id)
    if previous is None:
        raise ContentGenerationError(
            reason=REASON_CONTENT_VERSION_NOT_FOUND,
            message=(
                f"内容 {content_id} 还没有任何版本，无法采纳标题。"
                "（采纳是『在已有候选里挑一个』，而候选随版本行产生）"
            ),
        )

    from rednote.modules.versions.models import ENTITY_TYPE_CONTENT
    from rednote.modules.versions.service import register_version

    # 采纳后的正文三件：标题换成被采纳的，正文与标签原样复用。
    # 用**上一版的 payload 作为基线**而不是重新拼 dict：
    # 重新拼会让"payload 里还有别的键"（将来 schema 演进）在这一版上
    # 悄悄丢掉，而两版对比时表现为"采纳把某个字段清空了"。
    payload: dict[str, Any] = dict(previous.payload)
    payload["title"] = new_title

    registered = register_version(
        version_repo,
        entity_type=ENTITY_TYPE_CONTENT,
        entity_id=content_id,
        project_id=item.project_id,
        payload=payload,
        created_by=created_by,
    )

    target_version_no = content_repo.next_version_no(content_item_id=content_id)
    current_lock_version = int(item.lock_version)

    with content_repo.savepoint():
        new_version = content_repo.insert_version(
            version_id=registered.node_id,
            content_item_id=content_id,
            version_no=target_version_no,
            title=new_title,
            body=previous.body,
            tags=previous.tags or (),
            # `bindings` 是 JSONB，原样搬运**不重新序列化**：
            # 重新序列化会再造一次 `claimId` 的 str 转换，而两处
            # 序列化分叉的表现是"同一段绑定在两版里长得不一样"。
            bindings=previous.bindings or (),
            payload=payload,
            payload_hash=registered.payload_hash,
            blueprint_id=previous.blueprint_id,
            adopted_title_candidate_id=candidate_id,
            # ★ 原样复用上一版的上下文 id（**不新建上下文行**）：
            # 这一版没有模型调用，造一条上下文行等于报出一次没发生的调用。
            generation_context_id=previous.generation_context_id,
            created_by=created_by,
        )
        # 候选行**不动**（候选表纯不可变，"采纳了哪一个"记在版本行上，
        # 见说明文档 §1.7 的对比表）。
    _link_revision_edge(
        version_repo,
        version_id=new_version.id,
        previous_version_id=previous.id,
    )

    affected = content_repo.advance_projection(
        content_id=content_id,
        expected_lock_version=current_lock_version,
        version_id=new_version.id,
        version_no=target_version_no,
        title=new_title,
        state=item.state,
    )
    if affected == 0:
        raise ContentGenerationError(
            reason=REASON_PROJECTION_CONFLICT,
            message=(
                f"内容 {content_id} 的投影推进失败"
                f"（期望 lock_version={current_lock_version}）。"
            ),
        )

    return ContentVersionSummary(
        id=new_version.id,
        version_no=int(new_version.version_no),
        title=new_version.title,
        blueprint_id=new_version.blueprint_id,
        adopted_title_candidate_id=new_version.adopted_title_candidate_id,
        generation_context_id=new_version.generation_context_id,
        payload_hash=new_version.payload_hash,
        schema_version=int(new_version.schema_version),
        created_at=new_version.created_at,
        created_by=new_version.created_by,
    )


# ---------------------------------------------------------------------------
# 写：人工编辑（WP29 的接缝；端点归 WP29 的 `feedback` 模块）
# ---------------------------------------------------------------------------


def read_edit_target(
    content_repo: ContentRepository, *, content_id: uuid.UUID
) -> EditTarget:
    """取编辑前的**读侧快照**（当前版本 + 它的 ETag），供展示与 `If-Match` 比对。

    **为什么这是一个独立函数而不是让端点自己查两次**：编辑是"读 → 展示 →
    改 → 带 `If-Match` 提交"的两步流程。若端点先查 `content_items` 拿
    `lock_version`、再查版本行拿正文，两次查询之间可以插进一次别人的写入
    —— 前端拿到的正文与 ETag 不属于同一版，提交时 412，而它展示的差异
    与库里的当前版本对不上。装配只在这里做一次，调用方没有机会拼错。

    只读：用 `get_content_item`（不加行锁）。加锁会让"点开编辑"这个纯读
    请求参与并发串行化，而它没有任何写意图。
    """
    item = content_repo.get_content_item(content_id)
    if item is None:
        raise ContentGenerationError(
            reason=REASON_CONTENT_NOT_FOUND,
            message=f"内容条目 {content_id} 不存在。",
        )

    previous = content_repo.latest_version(content_item_id=content_id)
    if previous is None:
        # 零版本的内容无法编辑：没有"上一版"可供合并，用空正文造一版会让
        # 编辑产出一条正文为空的内容 —— 那个状态与"还没生成的草稿"同形，
        # 但它在列表里看起来像一条正常内容。生成一版之后再编辑。
        raise ContentGenerationError(
            reason=REASON_CONTENT_VERSION_NOT_FOUND,
            message=(
                f"内容 {content_id} 还没有任何版本，无法编辑。"
                "（编辑是在已有版本上改字段；空正文造出的版本会与"
                "'尚未生成'的草稿混在一起）"
            ),
        )

    return EditTarget(
        content_id=item.id,
        project_id=item.project_id,
        lock_version=int(item.lock_version),
        etag=content_edit_etag(item),
        current_version_id=previous.id,
        version_no=int(previous.version_no),
        title=previous.title,
        body=previous.body,
        tags=tuple(previous.tags or ()),
        bindings=tuple(dict(binding) for binding in (previous.bindings or ())),
        blueprint_id=previous.blueprint_id,
        generation_context_id=previous.generation_context_id,
    )


def content_edit_etag(item: ContentItem) -> str:
    """**ETag 的唯一读法**：`build_etag(item.id, item.lock_version)`。

    与 `platform.concurrency.parse_etag` / `require_if_match` 同一格式。
    不在端点里手拼 `f'"{id}:{version}"'`：那是一份会与 `build_etag`
    分叉的副本，而分叉的表现是"服务端签发的 ETag 自己校验不过"。
    """
    return build_etag(item.id, item.lock_version)


def append_edited_version(
    content_repo: ContentRepository,
    *,
    version_repo: VersionRepository,
    baseline_repo: BaselineRepository,
    content_id: uuid.UUID,
    expected_lock_version: int,
    title: str | None = None,
    body: str | None = None,
    tags: Sequence[str] | None = None,
    bindings: Sequence[Mapping[str, Any]] | None = None,
    created_by: uuid.UUID | None = None,
) -> ContentVersionSummary:
    """人工编辑 ⇒ **追加一个内容版本**（与 `adopt_title_candidate` 同形）。

    ## 为什么编辑也是"追加一版"

    `content_versions` 不可变（触发器拦 UPDATE/DELETE）。"人工编辑"若想
    就地改行，必撞 `23001`；而版本历史的价值恰恰是"改之前长什么样"。
    因此编辑与采纳、重做一样，落成**新版本行** —— 列表页的 `current_*`
    投影指向它，旧版原样保留。

    ## 字段合并规则（未传的字段沿用上一版）

    `title` / `body` / `tags` / `bindings` 任一为 `None` ⇒ 取
    `latest_version` 的对应字段。"只改标题"的请求因此不会把正文清空 ——
    这正是 `EditTarget` 把当前版本一次读出、接口按字段传参的理由。

    ## ★ 证据绑定校验**不能跳过**（R1 / R4）

    改动正文会让段数变化，而 `bindings` 的 `paragraphIndex` 是**按段**
    索引的。若不重跑 `validate_bindings`，"删掉两段"会留下越界/缺段的
    绑定，而它在库里看起来仍然"有 bindings"—— R4 静默降级成不判。
    因此合并后的正文与绑定必须**成对**过一次校验，且判据域
    （`known_claim_ids` / `total_claim_count`）来自**当前基线**，
    与 `execute_generation` 完全同源（见 `service.py:539-595` 的编排）。
    这里刻意**没有**旁路参数：调用方不能声明"这次不用判"。

    ## ★ 为什么本函数抛 **412**，而 WP26 既有路径抛 **409**

    WP26 的 `adopt_title_candidate` 在投影推进失败时抛
    `ContentGenerationError(REASON_PROJECTION_CONFLICT)`，`api.py`
    的 `_HTTP_BY_REASON` 把它映成 **409 `INVALID_TRANSITION`**（`api.py:304`）。
    那对"采纳"是合适的：候选已存在，用户刷新后可以再点一次。

    人工编辑不同：它是**并发写同一份正文**。协议 §1.2 要求带 `If-Match`
    的修改在版本不匹配时回 **412 `VERSION_CONFLICT`**，并回带
    `currentEtag` —— 前端据此直接拿到"该改成什么"，把用户正在编辑的
    内容与新版本做差异展示。若这里回 409，客户端会把它当成
    "当前状态不允许这个操作"（类似状态机迁移失败），**不会**重新读取，
    用户手打的修改就白丢了（或更糟：在旧正文上覆盖新版本）。

    因此本函数**不经** `ContentGenerationError` + `_ERROR_MAP`，而是用
    `platform.concurrency.version_conflict(...)` 构造 `ApiError`
    （`ErrorCode.VERSION_CONFLICT` → 412）并直接 `raise`。
    这是本函数与 `adopt_title_candidate` 唯一的编排性分叉，其余
    （行锁 → 取上一版 → 合并 → 校验 → 注册节点 → savepoint 插版本 →
    条件 UPDATE 推进投影）逐字同形 —— 复用同一套原语，不另发明一套。
    """
    # --- 1) 取内容行（行锁）------------------------------------------------
    item = content_repo.get_content_item_for_update(content_id)
    if item is None:
        raise ContentGenerationError(
            reason=REASON_CONTENT_NOT_FOUND,
            message=f"内容条目 {content_id} 不存在。",
        )
    project_id = item.project_id
    current_lock_version = int(item.lock_version)

    # --- 1.5) If-Match：在任何**写入**之前判 ------------------------------
    #
    # 不把这件事只留给最后的 `advance_projection(rowcount == 0)`：
    # 本函数会先 `register_version` + `insert_version`（不可变表，写进去
    # 事务内就删不掉），若等到推进投影时才发现锁过期，只能靠回滚兜住。
    # 早判让"过期"与"零写入"是同一件事，也让 412 的 `currentEtag` 用的是
    # 锁内读到的真值（不是客户端传来的那个旧值）。
    if expected_lock_version != current_lock_version:
        raise version_conflict(
            resource_id=content_id,
            current_version=current_lock_version,
            reason=REASON_EDIT_LOCK_CONFLICT,
            message="内容已被更新，请重新读取最新版本后再提交。",
        )

    # --- 2) 上一版（编辑的基线）-------------------------------------------
    previous = content_repo.latest_version(content_item_id=content_id)
    if previous is None:
        raise ContentGenerationError(
            reason=REASON_CONTENT_VERSION_NOT_FOUND,
            message=(
                f"内容 {content_id} 还没有任何版本，无法编辑。"
                "（编辑是在已有版本上改字段，不是凭空造正文）"
            ),
        )

    # --- 3) 字段合并：未传的沿用上一版 -------------------------------------
    final_title = title if title is not None else previous.title
    final_body = body if body is not None else previous.body
    final_tags = tuple(tags) if tags is not None else tuple(previous.tags or ())
    final_bindings: Sequence[Mapping[str, Any]] = (
        bindings if bindings is not None else (previous.bindings or ())
    )

    # --- 4) 段数以**合并后的正文**为准 -------------------------------------
    paragraph_count = len(split_paragraphs(final_body))

    # --- 5) 基线判据域（与 `execute_generation` 第 5 步逐字同源）------------
    baseline_id = baseline_repo.active_baseline_id(project_id)
    known_claim_ids: frozenset[uuid.UUID] = frozenset()
    total_claim_count = 0
    if baseline_id is not None:
        baseline_project = content_repo.baseline_project(baseline_id)
        if baseline_project is not None and baseline_project != project_id:
            raise ContentGenerationError(
                reason=REASON_BASELINE_MISMATCH,
                message=(
                    f"项目 {project_id} 的生效基线 {baseline_id} 属于另一个项目"
                    f"（{baseline_project}）。跨项目基线是语义错误："
                    "基线承载本品牌的口径。"
                ),
            )
        if content_repo.baseline_exists(baseline_id):
            brief_version_id = content_repo.baseline_brief_version(baseline_id)
            known_claim_ids, total_claim_count = content_repo.claim_scope(
                brief_version_id
            )
            # ★ ISS-177 的同款守卫：判据域为空必须拒 —— 且**只在基线真的
            # 关联了 Brief 时**。`brief_version_id IS NULL` 是合法的
            # "这个项目本就没走 Brief 流程"，把它也判成未就绪会误伤整类项目。
            if brief_version_id is not None and total_claim_count == 0:
                raise ContentGenerationError(
                    reason=REASON_BASELINE_NOT_READY,
                    message=(
                        f"项目 {project_id} 的生效基线 {baseline_id} 关联的 Brief"
                        f" 版本 {brief_version_id} 没有任何 claim —— 判据域为空。"
                        "此时放行会让 `validate_bindings` 拿到 (frozenset(), 0)："
                        "'绑定的 claim 必须存在'的判据域是空集，而'绑满即视为没绑'"
                        "那条规则的分母是 0 ⇒ 该规则不启用 ⇒ 模型绑 0 条就能通过，"
                        "R4 整条静默降级为'不判绑定'。"
                    ),
                )
        else:
            # `active_baseline_id` 指向一个不存在的基线 ⇒ 投影坏了。
            raise ContentGenerationError(
                reason=REASON_BASELINE_MISMATCH,
                message=(
                    f"项目 {project_id} 的 active_baseline_id={baseline_id} "
                    "指向的基线行不存在（投影与实现表不一致）。"
                ),
            )

    # 合并后的正文与绑定必须**成对**过校验：这条调用不是可选的（见 docstring
    # 「证据绑定校验不能跳过」）。`validate_bindings` 要 list，而上游可能给
    # 元组（`Sequence` 入参）⇒ 归一在这里做一次。
    validated_bindings = validate_bindings(
        [dict(binding) for binding in final_bindings],
        known_claim_ids=known_claim_ids,
        total_claim_count=total_claim_count,
        paragraph_count=paragraph_count,
    )

    from rednote.modules.versions.models import ENTITY_TYPE_CONTENT
    from rednote.modules.versions.service import register_version

    # --- 6) payload：以上一版为基线，只覆盖**真正传了**的键 ----------------
    #
    # 不重新拼 dict：重新拼会让 payload 里"将来的键"在这一版上悄悄丢掉，
    # 而两版对比时表现为"编辑清空了某个字段"（与采纳路径同一条理由）。
    payload: dict[str, Any] = dict(previous.payload)
    if title is not None:
        payload["title"] = final_title
    if body is not None:
        payload["body"] = final_body
    if tags is not None:
        payload["tags"] = list(final_tags)
    # `bindings` 不在 payload 里：它是 `content_versions` 的独立 JSONB 列
    # （`COPY_FIELDS` 只有 title/body/tags），与生成路径的 payload 同形。
    # 把它塞进 payload 会造出第二个存储位置，两处迟早不一致。

    registered = register_version(
        version_repo,
        entity_type=ENTITY_TYPE_CONTENT,
        entity_id=content_id,
        project_id=project_id,
        payload=payload,
        created_by=created_by,
    )

    # --- 7) 目标版本号：锁内取一次，全程复用 ------------------------------
    target_version_no = content_repo.next_version_no(content_item_id=content_id)

    # --- 8) savepoint 包住插入（撞唯一约束时只回滚这一段）------------------
    with content_repo.savepoint():
        new_version = content_repo.insert_version(
            version_id=registered.node_id,
            content_item_id=content_id,
            version_no=target_version_no,
            title=final_title,
            body=final_body,
            tags=final_tags,
            # 校验产物是 `ParagraphBinding`，入库要的是线上键名的 dict：
            # 序列化在这里做一次、且只有这一处（与生成路径逐字相同）。
            bindings=[
                {
                    BINDING_KEY_PARAGRAPH_INDEX: binding.paragraph_index,
                    # JSONB 不认 UUID 对象 ⇒ 存字符串（列注释的形状就是 "<uuid>"）。
                    BINDING_KEY_CLAIM_ID: (
                        None if binding.claim_id is None else str(binding.claim_id)
                    ),
                    BINDING_KEY_REASON: binding.reason,
                    BINDING_KEY_TEXT: binding.text,
                }
                for binding in validated_bindings
            ],
            payload=payload,
            payload_hash=registered.payload_hash,
            blueprint_id=previous.blueprint_id,
            # 编辑不是采纳：这一版没有采纳任何标题候选。
            adopted_title_candidate_id=None,
            # 原样复用上一版的上下文 id（**不新建上下文行**）：
            # 人工编辑没有模型调用，造一条上下文行等于报出一次没发生的调用。
            generation_context_id=previous.generation_context_id,
            created_by=created_by,
        )
    _link_revision_edge(
        version_repo,
        version_id=new_version.id,
        previous_version_id=previous.id,
    )

    # --- 9) 条件 UPDATE 推进投影 ------------------------------------------
    affected = content_repo.advance_projection(
        content_id=content_id,
        expected_lock_version=current_lock_version,
        version_id=new_version.id,
        version_no=target_version_no,
        title=final_title,
        state=item.state,
    )
    if affected == 0:
        # 行锁已经串行化了同内容的写入，走到这里说明读与写之间投影被
        # 别的路径改过（例如绕过本函数的直接 UPDATE）。仍然按协议回 412：
        # 客户端需要的是"重新读一次"，而不是"这个操作当前不允许"。
        reloaded = content_repo.reload_content_item(content_id)
        current_version = (
            int(reloaded.lock_version)
            if reloaded is not None
            else current_lock_version
        )

        raise version_conflict(
            resource_id=content_id,
            current_version=current_version,
            reason=REASON_EDIT_LOCK_CONFLICT,
            message="内容已被更新，请重新读取最新版本后再提交。",
        )

    return ContentVersionSummary(
        id=new_version.id,
        version_no=int(new_version.version_no),
        title=new_version.title,
        blueprint_id=new_version.blueprint_id,
        adopted_title_candidate_id=new_version.adopted_title_candidate_id,
        generation_context_id=new_version.generation_context_id,
        payload_hash=new_version.payload_hash,
        schema_version=int(new_version.schema_version),
        created_at=new_version.created_at,
        created_by=new_version.created_by,
    )


def _context_views(
    content_repo: ContentRepository, *, content_id: uuid.UUID
) -> tuple[GenerationContextView, ...]:
    return tuple(
        _context_view(row)
        for row in content_repo.list_generation_contexts(content_id=content_id)
    )


def _context_view(row: Any) -> GenerationContextView:
    return GenerationContextView(
        id=row.id,
        project_id=row.project_id,
        content_id=row.content_id,
        content_version_id=row.content_version_id,
        task_kind=row.task_kind,
        prompt_template_id=row.prompt_template_id,
        prompt_version_id=row.prompt_version_id,
        baseline_id=row.baseline_id,
        model_profile_id=row.model_profile_id,
        created_at=row.created_at,
    )


__all__ = [
    "ContentGenerationError",
    "ContentGenerator",
    "GatewayGenerator",
    "GenerationDispatch",
    "GenerationOutcome",
    "PROMPT_VALUE_BATCH_KIND",
    "PROMPT_VALUE_BLUEPRINT",
    "PROMPT_VALUE_CONTENT_ID",
    "PROMPT_VALUE_KEYS",
    "PROMPT_VALUE_PROJECT_ID",
    "REASON_GENERATION_REQUEST_INVALID",
    "REASON_MODEL_PROFILE_NOT_FOUND",
    "REASON_PROJECTION_CONFLICT",
    "UnavailableGenerator",
    "adopt_title_candidate",
    "append_edited_version",
    "content_edit_etag",
    "execute_generation",
    "generation_prompt_values",
    "list_contents",
    "prepare_regeneration",
    "read_content_detail",
    "read_edit_target",
    "read_generation_contexts_by_prompt_version",
    "read_version_history",
    "resolve_provider_id",
    "start_generation",
]
