"""与 WP13（任务与 Outbox）的接缝（WP27）。

**本模块是 WP27 与任务框架之间唯一的地方**。它注册一个任务处理器：

| kind | 做什么 | 由哪个端点入队 |
|---|---|---|
| `MEDIA_GENERATE` | 按 `media_items.generation_request` + `pending_reference_bindings` 跑一次生成（校验提示词/素材 → 预估 → 预占 → 派发 → R2 校验 → 落库 → 结算） | `POST /contents/{id}/media-generations`、`POST /media/{id}/regenerations` |

业务规则**一条都不在这里** —— 全部在 `service.execute_media_generation`。
本模块只做"任务框架的形状"与"服务层的形状"之间的翻译：
把 `ClaimedJob` 折成服务层入参，把服务层抛出的六类失败折成 `HandlerOutcome`。

---

## 一、★ 目标从哪来：`media_items.job_id` **反查**，不用 `jobs.content_id`

本任务要产出"**某一张图**的新版本"，因此处理器必须同时知道
`media_item_id` 与 `content_id`（后者用于读正文切段、算 R4 段落对位）。

而任务框架给处理器的东西只有 `ClaimedJob`
（`job_id / kind / attempt_id / fence_token / attempt_no / worker_id /
input_version_ids`）—— **没有自由参数位**（`jobs/types.py`）。
`jobs` 表也没有 actor / payload 列，唯一能装关联对象的只有
`jobs.content_id`（裸 UUID），而它**只有一个位**：

| 装什么 | 后果 |
|---|---|
| 装 `media_item_id` | 处理器拿不到 `content_id` ⇒ **读不到正文**（`content_item_project` / `content_body_for_paragraphs` 都按内容 id 查） |
| 装 `content_id` | 处理器拿不到"是哪一张图" ⇒ 只会重做第一格，**其余两格永远不动** |

**两者都静默做错**（不报错），因此都不是选项。
⇒ 取 **WP17 同构**：`media_items.job_id`（唯一约束 `uq_media_items_job_id`）
+ `find_media_item_for_job(claimed.job_id)` 反查。

**为什么不改 `jobs` 表**：`content/types.py` 已把"本包不修改 `jobs`"
定为接缝冻结铁律（WP13 的资产）。加列会同时影响 WP17 / WP26 两条已在跑的
任务链，而收益只是省一次反查。

**反查不到 ⇒ fail-closed**（`ERROR_TARGET_MISSING`，`retryable=False`）：
有人手工造了一个 `MEDIA_GENERATE` 任务，或端点忘了在**同一事务**里
`set_job_id`。重试一万次也不会让它长出一行 —— **不重试、不猜一个目标**。

## 二、★ `actor_id` 取 `media_items.created_by`，取不到就**不生成**

`service._persist_generation` 第 ② 步要求 `actor_id` 非空
（`file_objects.uploaded_by` 是 NOT NULL 的 —— "这张图是谁的"必须永远有答案），
且它**明确拒绝**用系统用户冒名顶替。

而 `ClaimedJob` 里**没有** actor。唯一的可用来源是
`media_items.created_by` —— 端点在发起事务里用 `user.id` 写的那一列，
语义恰好是"这次生成的发起人"（不是"图的主人"，但两者在这里同一：
派生资产归属于触发它的人）。

**`created_by` 为空 ⇒ `ERROR_ACTOR_MISSING`，且发生在**预占之前：
本模块**早判**这道。服务层第 7 步也有一道（`actor_id is None` ⇒
`REASON_GENERATOR_NOT_CONFIGURED`），但那时**钱已经花过了**
（第 6 步的预占与派发都在它之前）—— 静态图按张计费，
"等到落库才发现没有 actor" 意味着每次白买一张。

⚠️ 两道判**不是**同一件事的两个说法，它们兜的调用面不同：

| 位置 | 兜谁 | 时刻 |
|---|---|---|
| 本模块（第 0 步） | **任务路径**：在花钱之前 fail-fast | 派发**之前** |
| `service._persist_generation` | **任何**调用方（含测试 / 证据脚本 / 将来的同步入口） | 落库时 |

服务层那道**必须留着**：它是"落悬空 `image_file_id`"的最后一道。
本模块这道是**优化**（把一次白花钱变成一次不花钱）。

## 三、`result_ref` 用**媒体版本 id**

`jobs.result_ref` 的语义是**结果引用**（WP17 / WP18 已论证"不得用输入占住它"）。
一次成功的静态图生成产出的东西**就是媒体版本**，因此 `result_ref` 落
`str(outcome.version_id)` —— 而 `media_versions.id` **同时是**
`version_nodes.node_id`（`models.py` 的"共享主键"），因此顺它可回溯到注册表里那一版。

## 四、失败映射（唯一口径，与 WP26 逐条同构）

| 服务层抛出 | 归类 | 任务状态 | `retryable` | `usage_pending` |
|---|---|---|---|---|
| `MediaGenerationError` | 业务前置不成立 | `FAILED` | `False` | `False` |
| `PromptUnavailable` | 部署没配提示词 | `FAILED` | `False` | `False` |
| `SchemaViolation` | 绑定/参数不合法 | `FAILED` | `False` | `False` |
| `GatewayUnavailable`（在途态） | 通道未定 | **`WAITING_RECONCILIATION`** | `False` | `True` |
| `GatewayUnavailable`（其余） | 通道问题 | `FAILED` | `False` | `False` |
| `ApiError` | 协议错误 | `FAILED` | `False` | `False` |

| 本模块自判的装配故障 | 任务状态 | `retryable` |
|---|---|---|
| `ERROR_TARGET_MISSING`（反查不到媒体行） | `FAILED` | `False` |
| `ERROR_ACTOR_MISSING`（`created_by` 为空） | `FAILED` | `False` |

**`error_code` 取异常的 `reason`**（四类都带 `reason`；`ApiError` 取 `str(code)`）——
不另造一套前缀码：`reason` 已经是本包排障时按之过滤的口径。

**为什么 `ApiError` 就地折、不向上抛**：`jobs.service._invoke_handler` 对
**任何未捕获异常**的折算恒是 `retryable=True` + `usage_pending=True`，
也就是"值得再试一次"。而协议错误重试**可能再买一次真实调用** ——
与 WP16 的保守默认（`retryable=False`）和 WP18 / WP26 的处置同一条判据。

**`retryable` 恒为 `False`**：上表八类的失败原因分别是"目标没关联"、
"没有 actor"、"输入不合法"、"部署没配"、"绑定不合法"、"通道未定"、
"通道问题"、"协议错误"—— **没有一类会因为重试而变好**。

## 五、`UNKNOWN` 不进 `FAILED`（复用 WP16 的常量，不重打状态字面量）

`GatewayUnavailable.state` 是**网关调用状态**（`CALL_*` 取值域）。
`state` 落在 `CALL_IN_FLIGHT_STATES`（`PREPARED` / `SUBMITTED` /
`POLLING` / `UNKNOWN`）时映射为 `WAITING_RECONCILIATION`：

- 映射成 `FAILED`（可重试）会让 WP13 requeue → **重发一次可能已经成功的调用**；
- 映射成 `FAILED`（不可重试）会让"真实结果可能已经成功、且已计费"的调用
  被永久当成失败 —— 用户看到"生成失败"，供应商那边早已完成。

`WAITING_RECONCILIATION` 是系统里**唯一**为"不知道成没成"准备的状态，
且 WP13 的 `reconcile_job` 明确禁止 requeue。

**为什么不直接调 `outcome_for_result`**：它吃 `CapabilityResult`，
而 `gateway_bridge.dispatch_for_media_generation` 在失败路径上
**只抛出异常、不返回结果** —— 拿不到那个对象。因此本模块复用
WP16 的**常量**与**文档化的决策**，而**不重打状态字符串**。

## 六、`adapter` / `byte_source` / `storage` 三个默认值全是 `None`（不是疏漏）

| 依赖 | 默认 `None` 时的行为 | 为什么不做成一个"看起来能用"的默认值 |
|---|---|---|
| `adapter`（网关能力适配器） | `UnavailableGenerator` ⇒ 第一次调用即抛 `GatewayUnavailable(GENERATOR_NOT_CONFIGURED)` | 给一个假适配器会让"链路已验证"变成假话（铁律 3；与 WP18 的 `UnavailableDecomposer` 同款） |
| `byte_source`（图片字节来源） | 透传 `None` ⇒ `gateway_bridge` 抛 `GatewayUnavailable(GATEWAY_NO_IMAGE_REF)`（**OP-195 未关闭，这在本机是常态**） | `GatewayMediaGenerator` 的 docstring 已论证：本机当前确实无人提供字节来源，在此处编一个"能取到字节"的实现会让 `fail-closed` 这条性质**恰好失效在最需要它的地方** |
| `storage`（对象存储后端） | 透传 `None` ⇒ `service._persist_generation` 第 ① 步 fail-closed（`GENERATOR_NOT_CONFIGURED`） | ⚠️ **刻意不照抄 `parsing/handlers.py` 的 `build_storage_backend(backend="LOCAL", local_root=None)` 默认**：那会在"部署配的是 MINIO"时**静默降级成本地盘**，而磁盘路径与 `settings.storage_local_root` 不一致时**不报错**，只是图写到了一个没人去读的地方。存储后端是**装配期**依赖（读 `settings`），由**将来的 worker 装配点**构造一次注入（⚠️ 该装配点当前全仓不存在，见第七节） |

## 七、★ 合并点当前全仓不存在（实测，必须写下来）

全仓检索 `run_job(` / `claim_jobs(` / `build_default_registry`：

| 命中位置 | 是什么 |
|---|---|
| `jobs/handlers.py`（`build_default_registry` 的定义）、`jobs/service.py`（`claim_jobs` / `run_job` 的定义） | **定义**，不是调用 |
| `tests/test_jobs_wp13.py` / `test_gateway_wp16.py` / `test_parsing_wp17.py` / `test_content_wp26_exec.py` | **测试**（唯一真正的调用点） |
| `main.py` / `tools/**` | **零命中** |

也就是说：WP13 交付的 `jobs` 框架有**领任务与跑任务的实现**，
但**没有生产侧的 worker 入口**。四个包（`brief` / `parsing` / `content` /
`media`）的 handler 都在等它。这是**既有状态**，不是本包引入的缺口。

⇒ 本包**不**在 `main.py` 里构造注册表挂到 `app.state` —— 那会是一个
**零消费者的悬空符号**，且它看起来完全像"已经接好了"
（ISS-95 的形态："声称有防线"比"没有防线"更危险，
因为下一个人会据此认为链路已经能跑）。

**本包交付的是"可被装配的入口"**（`build_task_registry`），
合并口径见该函数的 docstring。这条缺口登记进说明文档的收口事项。
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any, Final

from rednote.modules.gateway.types import CALL_IN_FLIGHT_STATES
from rednote.modules.jobs.handlers import TaskHandlerRegistry
from rednote.modules.jobs.models import (
    STATE_FAILED,
    STATE_SUCCEEDED,
    STATE_WAITING_RECONCILIATION,
)
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.jobs.types import ClaimedJob, HandlerOutcome
from rednote.modules.media.gateway_bridge import GatewayUnavailable
from rednote.modules.media.live_pack import LIVE_GROUP_MAX_BYTES_DEFAULT
from rednote.modules.media.prompts_bridge import PromptUnavailable
from rednote.modules.media.repository import MediaRepository
from rednote.modules.media.schemas import SchemaViolation
from rednote.modules.media.service import (
    GatewayMediaGenerator,
    MediaGenerationError,
    MediaGenerator,
    UnavailableGenerator,
    execute_media_generation,
)
from rednote.modules.media.types import KIND_MEDIA_GENERATE
from rednote.platform.errors import ApiError

_log = logging.getLogger(__name__)

#: 任务没有可产出的媒体行（`media_items.job_id` 反查不到）。
#: **装配故障**，不是业务失败 —— 端点忘了在同一事务里 `set_job_id`，
#: 或有人手工造了任务。用一个显式常量而不是字面量：排障时按错误码过滤，
#: 字面量拼错会**静默地少捞到一批记录**（与 WP18 / WP26 的 `ERROR_*` 同惯例）。
ERROR_TARGET_MISSING: Final[str] = "MEDIA_GENERATE_TARGET_MISSING"

#: 媒体行没有 `created_by` ⇒ 无法落 `file_objects.uploaded_by`（NOT NULL）。
#: **同样是装配故障**：`media_items.created_by` 由端点在发起事务里写
#: （`user.id`），为空说明那条路径绕过了端点。**不猜一个系统用户** ——
#: 那样"这张图是谁的"就再也答不出来（`service._persist_generation` 同款理由）。
ERROR_ACTOR_MISSING: Final[str] = "MEDIA_GENERATE_ACTOR_MISSING"


def media_generate_handler(
    claimed: ClaimedJob,
    repo: JobRepository,
    *,
    adapter: Any = None,
    byte_source: Any = None,
    storage: Any = None,
    output_schema_version: int = 1,
    policy_version: str = "v1",
    motion_generator: Any = None,
    live_group_max_bytes: int = LIVE_GROUP_MAX_BYTES_DEFAULT,
) -> HandlerOutcome:
    """`MEDIA_GENERATE` 的处理器：反查目标 → 跑生成 → 折成申报。

    依赖可注入（测试与证据脚本用）。三个默认值全是 `None` ⇒ fail-closed，
    理由见模块 docstring 第六节 —— **不在这里编任何一个"看起来能用"的默认实现**。

    WP28：`motion_generator`（动态段出网边界）同样缺省 `None`；Live 请求遇到 `None`
    会在服务层预占之前 fail-closed（`GENERATOR_NOT_CONFIGURED`，不可重试），
    **不**在此静默补一个 `LiveMotionStub`。静态请求不受影响。
    """
    session = repo.session
    media_repo = MediaRepository(session)

    # --- 第 0 步：反查目标（`media_items.job_id`，唯一真相）--------------
    item = media_repo.find_media_item_for_job(claimed.job_id)
    if item is None:
        _log.warning(
            "MEDIA_GENERATE 任务反查不到媒体行",
            extra={"job_id": str(claimed.job_id)},
        )
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code=ERROR_TARGET_MISSING,
            retryable=False,
        )

    # --- 第 0 步（第二道）：actor 必须在**预占之前**就有 -----------------
    # 理由见模块 docstring 第二节：服务层那道在落库时才判，
    # 而那时预占与派发（真花钱的两步）都已经走过了。
    if item.created_by is None:
        _log.warning(
            "MEDIA_GENERATE 任务的媒体行没有 created_by，无法落 uploaded_by",
            extra={"job_id": str(claimed.job_id), "media_item_id": str(item.id)},
        )
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code=ERROR_ACTOR_MISSING,
            retryable=False,
        )

    generator, gateway_repo = _build_generator(
        claimed,
        repo,
        adapter=adapter,
        byte_source=byte_source,
        output_schema_version=output_schema_version,
        policy_version=policy_version,
    )

    try:
        outcome = execute_media_generation(
            media_repo,
            # ★ 两个 id 都从同一行来（`find_media_item_for_job` 的返回值）——
            #   这正是"不用 `jobs.content_id`"换来的东西（docstring 第一节）。
            content_id=item.content_item_id,
            media_item_id=item.id,
            generator=generator,
            prompt_repo=_prompt_repository(session),
            billing_repo=_billing_repository(session),
            gateway_repo=gateway_repo,
            file_repo=_file_repository(session),
            storage=storage,
            version_repo=_version_repository(session),
            # ★ 发起人（不是"图的主人"的同义词，但派生资产归属于触发它的人）。
            actor_id=item.created_by,
            motion_generator=motion_generator,
            live_group_max_bytes=live_group_max_bytes,
            # `request_id` 刻意不传：`ClaimedJob` 里没有可用的请求 id，
            # 而编一个（如 `str(job_id)`）会让计费留痕里的 `request_id`
            # 看起来像一次 HTTP 请求的 id，实际是任务 id —— 排障时误导。
            # 传 `None` 是**如实**：这条链路确实没有 HTTP 请求上下文。
        )
    except MediaGenerationError as exc:
        # 业务前置不成立（媒体行/内容/版本不存在、档案不存在、请求坏、
        # 绑定不合法、投影冲突、未注入依赖）。原因**全是输入或数据的问题**。
        return _failed_with_reason(exc.reason, claimed=claimed)
    except PromptUnavailable as exc:
        # 部署没配提示词（或没激活版本）。同上，不重试。
        return _failed_with_reason(exc.reason, claimed=claimed)
    except SchemaViolation as exc:
        # 段落对位 / 绑定形状不合法。同上，不重试。
        return _failed_with_reason(exc.reason, claimed=claimed)
    except GatewayUnavailable as exc:
        return _outcome_for_gateway_unavailable(exc, claimed=claimed)
    except ApiError as exc:
        # 协议错误。**就地折成不可重试的 FAILED**（理由见 docstring 第四节）：
        # 向上抛会让框架折成 `retryable=True`，那意味着可能再买一次。
        _log.info(
            "静态图生成被协议层拒绝",
            extra={"job_id": str(claimed.job_id), "code": str(exc.code)},
        )
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code=str(exc.code),
            retryable=False,
        )

    return HandlerOutcome(
        state=STATE_SUCCEEDED,
        # `media_versions.id` 同时是 `version_nodes.node_id` ⇒ 可回溯（docstring 第三节）。
        result_ref=str(outcome.version_id),
        usage_pending=outcome.usage_pending,
    )


def _failed_with_reason(reason: str, *, claimed: ClaimedJob) -> HandlerOutcome:
    """`ValueError` 族（带 `.reason`）→ 不可重试的 `FAILED`。**唯一折算点**。"""
    _log.info(
        "静态图生成失败（%s）",
        reason,
        extra={"job_id": str(claimed.job_id), "reason": reason},
    )
    return HandlerOutcome(state=STATE_FAILED, error_code=reason, retryable=False)


def _outcome_for_gateway_unavailable(
    exc: GatewayUnavailable, *, claimed: ClaimedJob
) -> HandlerOutcome:
    """`GatewayUnavailable` → `HandlerOutcome`（`state` 决定是"等核对"还是"失败"）。

    判据是 **WP16 的 `CALL_IN_FLIGHT_STATES` 常量**，不是本模块重打的状态字符串
    （见 docstring 第五节）。
    """
    if exc.state is not None and exc.state in CALL_IN_FLIGHT_STATES:
        # 请求可能已经发出去了（在途 / 未知）。**这不是失败**：
        # 进"等核对"，由人拿依据定终态；WP13 的 reconcile 禁止 requeue。
        _log.warning(
            "静态图生成通道状态未定（%s，state=%s）",
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
    # 其余：未接线（`GENERATOR_NOT_CONFIGURED`）/ 策略否决 /
    # 缺 jobId-attemptId 的装配缺陷 / 调用失败 / 成功但无字节（OP-195）。
    # **没有一类因重试而变好**；而"无字节"重试一次 = 再买一张图。
    _log.warning(
        "静态图生成通道不可用（%s，state=%s）",
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
    byte_source: Any,
    output_schema_version: int,
    policy_version: str,
) -> tuple[MediaGenerator, Any]:
    """按是否接线了适配器造生成器，同时返回本次事务的 `GatewayRepository`。

    `adapter is None` ⇒ `UnavailableGenerator`（fail-closed，第一次调用即抛）。

    ★ **返回值是一个二元组**而不是只给 generator：`execute_media_generation`
    的第 4 步要用 `gateway_repo` 解 `provider_id`，而它必须与生成器里的
    那一个**同源**（同一个 session 上的两个实例即可，但**只有一处 import**）。
    让调用方各自本地 import 一次会造出两处 `GatewayRepository(session)` 的构造点 ——
    那本身无害，却让"网关留痕与媒体落库是否同事务"这个问题需要读两处才能回答。

    ⚠️ `adapter is None` 时**仍然**返回 gateway_repo：第 4 步的 provider 解析
    与"有没有接线适配器"无关，提前短路会让错误码从
    `GENERATOR_NOT_CONFIGURED`（准确）变成别的东西。
    """
    # 本地 import（R7 同款理由）：`gateway.repository` 拖网关的模型与仓储，
    # 而 `handlers` 在装配期才被 import —— 把重模块留在函数内，
    # 可以让"只导入本模块"（例如查常量）不付出它的代价。
    from rednote.modules.gateway.repository import GatewayRepository

    gateway_repo = GatewayRepository(repo.session)

    if adapter is None:
        return UnavailableGenerator(), gateway_repo

    return (
        GatewayMediaGenerator(
            gateway_repo=gateway_repo,
            adapter=adapter,
            # 透传 `None`：OP-195 未关闭时 `gateway_bridge` 抛
            # `GatewayUnavailable(GATEWAY_NO_IMAGE_REF)` —— **这是正确状态**
            # （docstring 第六节），本模块不替它编一个字节来源。
            byte_source=byte_source,
            job_id=claimed.job_id,
            attempt_id=claimed.attempt_id,
            output_schema_version=output_schema_version,
            policy_version=policy_version,
        ),
        gateway_repo,
    )


# ---------------------------------------------------------------------------
# 三个"同 session 派生"的仓储（本地 import，R7）
# ---------------------------------------------------------------------------
#
# 为什么不把这三个（+ prompts）做成 `build_task_registry` 的入参：
# 它们是**每个任务一次、绑定当前 session** 的东西，与 `adapter` /
# `byte_source` / `storage`（**进程级**配置）不是一类。
# 把进程级配置与请求级对象放进同一个签名，会让装配点看起来需要
# "为每个请求准备一份仓储"（实际按 session 现造即可）。


def _prompt_repository(session: Any) -> Any:
    """提示词仓储（`_resolve_media_prompt` 的输入）。"""
    from rednote.modules.prompts.repository import PromptRepository

    return PromptRepository(session)


def _billing_repository(session: Any) -> Any:
    """计费仓储（预估 / 预占 / 结算 / 释放的输入）。"""
    from rednote.modules.billing.repository import BillingRepository

    return BillingRepository(session)


def _file_repository(session: Any) -> Any:
    """文件仓储（`store_generated_asset` 的输入）。"""
    from rednote.modules.files.repository import FileRepository

    return FileRepository(session)


def _version_repository(session: Any) -> Any:
    """版本仓储（`register_version` 的输入，OP-88 正向一半）。"""
    from rednote.modules.versions.repository import VersionRepository

    return VersionRepository(session)


def build_task_registry(
    *,
    adapter: Any = None,
    byte_source: Any = None,
    storage: Any = None,
    output_schema_version: int = 1,
    policy_version: str = "v1",
    motion_generator: Any = None,
    live_group_max_bytes: int = LIVE_GROUP_MAX_BYTES_DEFAULT,
) -> TaskHandlerRegistry:
    """本包要注册的任务处理器（**调用方与 WP13 的默认集合合并**）。

    返回**新实例**而不是原地改 WP13 的默认注册表：
    与 `TaskHandlerRegistry.register` 的不可变语义一致 ——
    "测试里注册了一个处理器，影响后续测试"是这类注册表最常见的坑
    （与 WP18 / WP17 / WP26 同款）。

    ⚠️ **本包不是唯一的注册点**：`brief` / `content` / `parsing` 各有一份
    `build_task_registry`，四处都返回新实例。

    ★ **合并点当前全仓不存在**（实测，不是推测）：`run_job` / `claim_jobs` /
    `build_default_registry` **只在 `tests/` 里被调用**（`test_jobs_wp13` /
    `test_gateway_wp16` / `test_parsing_wp17` / `test_content_wp26_exec`），
    `main.py` 与 `tools/` 里**一处都没有**。也就是说 WP13 交付的
    `jobs` 框架有**领任务/跑任务的实现**但**没有生产侧的 worker 入口** ——
    这是既有状态（WP17 / WP18 / WP26 的 handler 同样在等它），
    不是本包引入的缺口。

    ⇒ 本包**不**在 `main.py` 里构造一个注册表挂到 `app.state`：
    那会是一个**零消费者的悬空符号**，而它看起来完全像"已经接好了"
    （本仓 ISS-95 的形态："声称有防线"比"没有防线"更危险）。
    将来补 worker 入口时的合并口径：

        registry = TaskHandlerRegistry.empty()  # 或 build_default_registry()
        for build in (brief_handlers, content_handlers, parsing_handlers, media_handlers):
            for kind in build.build_task_registry(...).registered_kinds():
                registry = registry.register(kind, build.build_task_registry(...).handler_for(kind))

    （`TaskHandlerRegistry.register` 返回新实例 ⇒ 逐 kind 搬运即可；
    更简洁的写法是给 `TaskHandlerRegistry` 加一个 `merge(other)`，
    那属于 WP13 的资产，不在本包改。）

    `adapter` / `byte_source` / `storage` 是**进程级**依赖，
    由**那个将来的 worker 装配点**注入一次；`batchKind` / `modelProfileId`
    等**每请求**参数不在这里 —— 它们随 `media_items.generation_request` 走，
    参考素材绑定随 `media_items.pending_reference_bindings` 走
    （两条暂存列的落点见 `models.py` 与两张迁移的 §三）。
    """
    return TaskHandlerRegistry.empty().register(
        KIND_MEDIA_GENERATE,
        partial(
            media_generate_handler,
            adapter=adapter,
            byte_source=byte_source,
            storage=storage,
            output_schema_version=output_schema_version,
            policy_version=policy_version,
            motion_generator=motion_generator,
            live_group_max_bytes=live_group_max_bytes,
        ),
    )


__all__ = [
    "ERROR_ACTOR_MISSING",
    "ERROR_TARGET_MISSING",
    "build_task_registry",
    "media_generate_handler",
]
