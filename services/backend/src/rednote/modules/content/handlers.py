"""与 WP13（任务与 Outbox）的接缝（WP26）。

**本模块是 WP26 与任务框架之间唯一的地方**。它注册一个任务处理器：

| kind | 做什么 | 由哪个端点入队 |
|---|---|---|
| `CONTENT_GENERATE` | 按 `content_items.generation_request` 跑一次生成（预占 → 派发 → 校验 → 落库 → 结算） | `POST /projects/{id}/content-generations` |

业务规则**一条都不在这里** —— 全部在 `service.execute_generation`。
本模块只做"任务框架的形状"与"服务层的形状"之间的翻译：
把 `ClaimedJob` 折成服务层入参，把服务层抛出的五类失败折成 `HandlerOutcome`。

---

## 一、目标内容从哪来：`jobs.content_id`（WP13 的预留位）

本次生成的**内容身份**从 `jobs.content_id` 读（`JobRepository.get_job` 一次）。
它是 WP13 在建 `jobs` 表时就留下的关联位（`jobs/models.py:201`，
"预留的关联位，待对应工作包落地时补 FK"）——本包正是那个工作包，
但**不在这里补 FK**：外键迁移由本包的第 10 号迁移统一做，
处理器只读列、不建约束。

**为什么不用 `input_version_ids`**：它是 `version_nodes` 的 id 数组，
语义是"这次任务的输入快照"。这里要的是**输出目标**（要生成哪条内容），
两者方向相反 —— 混用会让"输入"与"目标"在同一个字段里，
而排障时无法判断一个 id 到底指哪一个。

**`content_id` 为空 = 装配故障**（有人手工造了一个 `CONTENT_GENERATE`
任务、或端点忘了传 `enqueue_job(content_id=…)`）。**不重试、不给默认值**：
重试一万次也不会让它长出一个内容 id，而"猜一个目标"会生成到错的内容上。

## 二、`result_ref` 用**内容版本 id**

`jobs.result_ref` 的语义是**结果引用**（WP17/WP18 已论证"不得用输入占住它"）。
一次成功的内容生成产出的东西**就是内容版本**，因此 `result_ref` 落
`str(current_version_id)` —— 顺 `version_nodes` 可回溯到注册表里那一版。

`current_version_id` 为 `None` 的边界**按理不该出现**（成功路径必然落了一版），
但本模块**不假设**：为 `None` 时 `result_ref=None`（而不是拼一个 `"None"` 字符串，
那会让对账时把一个不存在的引用当成真的引用）。

## 三、失败映射（唯一口径）

| 服务层抛出 | 归类 | 任务状态 | `retryable` | `usage_pending` |
|---|---|---|---|---|
| `ContentGenerationError` | 业务前置不成立 | `FAILED` | `False` | `False` |
| `PromptUnavailable` | 部署没配提示词 | `FAILED` | `False` | `False` |
| `SchemaViolation` | 模型输出不合法 | `FAILED` | `False` | `False` |
| `GatewayUnavailable`（在途态） | 通道未定 | **`WAITING_RECONCILIATION`** | `False` | `True` |
| `GatewayUnavailable`（其余） | 通道问题 | `FAILED` | `False` | `False` |
| `ApiError` | 协议错误 | `FAILED` | `False` | `False` |

**`error_code` 取异常的 `reason`**（四类都带 `reason`；`ApiError` 取 `str(code)`）——
不另造一套前缀码：`reason` 已经是本包排障时按之过滤的口径，
两处各命名一次就是 ISS-66 形态（同一判据两处实现 ⇒ 必然分叉）。

**为什么 `ApiError` 就地折、不向上抛**：`jobs.service._invoke_handler` 对
**任何未捕获异常**的折算恒是 `retryable=True` + `usage_pending=True`
（`jobs/service.py:471~477`），也就是"值得再试一次"。而协议错误重试
**可能再买一次真实调用** —— 与 WP16 的保守默认（`retryable=False`）
和 WP18 的处置（`brief/handlers.py` 捕获 `ApiError`、不重试）同一条判据。
`service` 的"不包装 `ApiError`"指的是**服务层不换一个类型**，
折叠成任务状态是本模块的职责 —— 两者不矛盾。

**`retryable` 恒为 `False`**：上表六类的失败原因分别是"输入不含法"、
"部署没配"、"模型输出不合法"、"通道未定"、"通道问题"、"协议错误"——
**没有一类会因为重试而变好**。而重试一次外部调用可能再买一次调用，
因此"多花一次钱"永远要有人签字（WP16 `outcome_for_result` 的默认值同款）。

## 四、`UNKNOWN` 不进 `FAILED`（复用 WP16 的常量，不重打状态字面量）

`GatewayUnavailable.state` 是**网关调用状态**（`CALL_*` 取值域）。
`state` 落在 `CALL_IN_FLIGHT_STATES`（`PREPARED` / `SUBMITTED` / `POLLING` /
`UNKNOWN`）时映射为 `WAITING_RECONCILIATION` —— 与 WP16 `outcome_for_result`
把 `UNKNOWN` 映射为"等核对"的取向**一致**，理由同源：

- 映射成 `FAILED`（可重试）会让 WP13 requeue → 重发一次可能已经成功的调用；
- 映射成 `FAILED`（不可重试）会让"真实结果可能已经成功、且已计费"的调用
  被永久当成失败 —— 用户看到"生成失败"，供应商那边早已完成。

`WAITING_RECONCILIATION` 是系统里**唯一**为"不知道成没成"准备的状态，
且 WP13 的 `reconcile_job` 明确禁止 requeue。

**为什么不直接调 `outcome_for_result`**：它吃 `CapabilityResult`，
而 `gateway_bridge.dispatch_for_generation` 在失败路径上**只抛出异常、
不返回结果**（`CapabilityResult` 在 `dispatch_call` 内部已被消费）——
拿不到那个对象。因此本模块复用 WP16 的**常量**（`CALL_IN_FLIGHT_STATES`）
与**文档化的决策**（在途态 → 等核对），而**不重打状态字符串**。
把"网关状态字符串"再抄一份到本模块就是第二个真值来源。
该取舍登记在说明文档附录 A。
"""

from __future__ import annotations

import logging
import uuid
from functools import partial
from typing import Any, Final

from rednote.modules.content.gateway_bridge import GatewayUnavailable
from rednote.modules.content.prompts_bridge import PromptUnavailable
from rednote.modules.content.repository import ContentRepository
from rednote.modules.content.schemas import SchemaViolation
from rednote.modules.content.service import (
    ContentGenerationError,
    ContentGenerator,
    GatewayGenerator,
    UnavailableGenerator,
    execute_generation,
)
from rednote.modules.content.types import KIND_CONTENT_GENERATE
from rednote.modules.gateway.types import CALL_IN_FLIGHT_STATES
from rednote.modules.jobs.handlers import TaskHandlerRegistry
from rednote.modules.jobs.models import (
    STATE_FAILED,
    STATE_SUCCEEDED,
    STATE_WAITING_RECONCILIATION,
)
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.jobs.types import ClaimedJob, HandlerOutcome
from rednote.platform.errors import ApiError

_log = logging.getLogger(__name__)

#: 任务没有可生成的目标内容（`jobs.content_id` 为空或 job 行不存在）。
#: **装配故障**，不是业务失败 —— 端点忘了传 `enqueue_job(content_id=…)`，
#: 或有人手工造了任务。用一个显式常量而不是字面量：排障时按错误码过滤，
#: 字面量拼错会**静默地少捞到一批记录**（与 WP18 的 `ERROR_*` 同惯例）。
ERROR_TARGET_MISSING: Final[str] = "CONTENT_GENERATE_TARGET_MISSING"


def content_generate_handler(
    claimed: ClaimedJob,
    repo: JobRepository,
    *,
    adapter: Any = None,
    output_schema_version: int = 1,
    policy_version: str = "v1",
) -> HandlerOutcome:
    """`CONTENT_GENERATE` 的处理器：读目标 → 跑生成 → 折成申报。

    依赖可注入（测试与证据脚本用）。`adapter` 默认 `None`
    ⇒ 用 `UnavailableGenerator`（调用即抛，fail-closed）——
    给一个"看起来能调"的默认值会让"链路已验证"变成假话
    （与 WP18 的 `UnavailableDecomposer` 同款理由）。
    """
    job = repo.get_job(claimed.job_id)
    if job is None or job.content_id is None:
        _log.warning(
            "CONTENT_GENERATE 任务缺少目标内容",
            extra={"job_id": str(claimed.job_id)},
        )
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code=ERROR_TARGET_MISSING,
            retryable=False,
        )

    content_repo = ContentRepository(repo.session)
    generator = _build_generator(
        claimed,
        repo,
        adapter=adapter,
        output_schema_version=output_schema_version,
        policy_version=policy_version,
    )

    try:
        outcome = execute_generation(
            content_repo,
            content_id=job.content_id,
            generator=generator,
        )
    except ContentGenerationError as exc:
        # 业务前置不成立（内容不存在 / 档案不存在 / 基线不匹配 / 请求坏 /
        # 投影冲突）。原因**全是输入或数据的问题**，重试不会让它变好。
        return _failed_with_reason(exc.reason, claimed=claimed)
    except PromptUnavailable as exc:
        # 部署没配提示词（或没激活版本）。同上，不重试。
        return _failed_with_reason(exc.reason, claimed=claimed)
    except SchemaViolation as exc:
        # 模型输出不合法。同上，不重试 —— 重试是再买一次同样的模型输出。
        return _failed_with_reason(exc.reason, claimed=claimed)
    except GatewayUnavailable as exc:
        return _outcome_for_gateway_unavailable(exc, claimed=claimed)
    except ApiError as exc:
        # 协议错误。**就地折成不可重试的 FAILED**（理由见模块 docstring 第三节）：
        # 向上抛会让框架折成 `retryable=True`，那意味着可能再买一次。
        _log.info(
            "内容生成被协议层拒绝",
            extra={"job_id": str(claimed.job_id), "code": str(exc.code)},
        )
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code=str(exc.code),
            retryable=False,
        )

    version_id = outcome.detail.current_version_id
    return HandlerOutcome(
        state=STATE_SUCCEEDED,
        # 为 None 时如实留空，不拼 "None" 字符串（见模块 docstring 第二节）。
        result_ref=None if version_id is None else str(version_id),
        usage_pending=outcome.usage_pending,
    )


def _failed_with_reason(reason: str, *, claimed: ClaimedJob) -> HandlerOutcome:
    """`ValueError` 族（带 `.reason`）→ 不可重试的 `FAILED`。**唯一折算点**。"""
    _log.info(
        "内容生成失败（%s）",
        reason,
        extra={"job_id": str(claimed.job_id), "reason": reason},
    )
    return HandlerOutcome(state=STATE_FAILED, error_code=reason, retryable=False)


def _outcome_for_gateway_unavailable(
    exc: GatewayUnavailable, *, claimed: ClaimedJob
) -> HandlerOutcome:
    """`GatewayUnavailable` → `HandlerOutcome`（`state` 决定是"等核对"还是"失败"）。

    判据是 **WP16 的 `CALL_IN_FLIGHT_STATES` 常量**，不是本模块重打的状态字符串
    （见模块 docstring 第四节）。
    """
    if exc.state is not None and exc.state in CALL_IN_FLIGHT_STATES:
        # 请求可能已经发出去了（在途 / 未知）。**这不是失败**：
        # 进"等核对"，由人拿依据定终态；WP13 的 reconcile 禁止 requeue。
        _log.warning(
            "内容生成通道状态未定（%s，state=%s）",
            exc.reason,
            exc.state,
            extra={"job_id": str(claimed.job_id), "state": exc.state},
        )
        return HandlerOutcome(
            state=STATE_WAITING_RECONCILIATION,
            error_code=exc.reason,
            # 在途调用**可能已产生未结用量**，按"可能有"上报，
            # 否则真实产生的费用会无声消失（WP16 对 UNKNOWN 的处理同一理由）。
            usage_pending=True,
        )
    # 其余：策略否决（ADR-10 最终结论）/ 缺 jobId-attemptId 的装配缺陷 /
    # 调用失败 / 成功但无输出。**没有一类因重试而变好**。
    _log.warning(
        "内容生成通道不可用（%s，state=%s）",
        exc.reason,
        exc.state,
        extra={"job_id": str(claimed.job_id), "state": exc.state},
    )
    return HandlerOutcome(
        state=STATE_FAILED,
        error_code=exc.reason,
        retryable=False,
    )


def _build_generator(
    claimed: ClaimedJob,
    repo: JobRepository,
    *,
    adapter: Any,
    output_schema_version: int,
    policy_version: str,
) -> ContentGenerator:
    """按是否接线了适配器造生成器。

    `adapter is None` ⇒ `UnavailableGenerator`（fail-closed，第一次调用即抛）。
    否则造 `GatewayGenerator`，其中 `gateway_repo` 由**本次任务的 session** 派生
    （与 `content_repo` 同一个事务）—— 网关留痕与内容落库必须同事务，
    否则可能出现"内容落了但调用留痕没落"（ADR-10 的可追溯性要求）。
    """
    if adapter is None:
        return UnavailableGenerator()

    # 本地 import（R7 同款理由）：`gateway.repository` 拖网关的模型与仓储，
    # 而 `handlers` 只在装配期被 import —— 但把重模块留在函数内
    # 可以让"只导入本模块"（例如查常量）不付出它的代价。
    from rednote.modules.gateway.repository import GatewayRepository

    return GatewayGenerator(
        gateway_repo=GatewayRepository(repo.session),
        adapter=adapter,
        job_id=claimed.job_id,
        attempt_id=claimed.attempt_id,
        output_schema_version=output_schema_version,
        policy_version=policy_version,
    )


def build_task_registry(
    *,
    adapter: Any = None,
    output_schema_version: int = 1,
    policy_version: str = "v1",
) -> TaskHandlerRegistry:
    """本包要注册的任务处理器（**调用方与 WP13 的默认集合合并**）。

    返回**新实例**而不是原地改 WP13 的默认注册表：
    与 `TaskHandlerRegistry.register` 的不可变语义一致 ——
    "测试里注册了一个处理器，影响后续测试"是这类注册表最常见的坑
    （与 WP18 `brief/handlers.build_task_registry` 同款）。

    `adapter` 是**进程级**依赖（网关适配器），由装配点（`main.py`）注入一次；
    `service` / `batchKind` / `modelProfileId` 等**每请求**参数则不在这里 ——
    它们随 `content_items.generation_request` 走（见 `types.py` 的接缝缺口实测）。
    """
    return TaskHandlerRegistry.empty().register(
        KIND_CONTENT_GENERATE,
        partial(
            content_generate_handler,
            adapter=adapter,
            output_schema_version=output_schema_version,
            policy_version=policy_version,
        ),
    )


__all__ = [
    "ERROR_TARGET_MISSING",
    "build_task_registry",
    "content_generate_handler",
]
