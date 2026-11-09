"""WP27 的服务层编排：一次静态图生成的**唯一**执行入口。

**分层**：`types ← schemas / models / repository / bridges ← service ← handlers / api`。
本模块**不 import** `content` / `billing` / `gateway` / `versions` / `files` 的
**重模块顶层**（R7：`gateway` / `billing` / `files` 走**本地导入**），
也不 import `content` 的任何符号（平级模块，见 `prompts_bridge` 的理由）。

---

## 七步编排（与 WP26 的 `execute_generation` 同构，顺序**不可调换**）

| 步 | 做什么 | 为什么不能挪|
|---|---|---|
| 1 | 取媒体行（`get_media_item_for_update` 行锁） | 不锁则并发重做会各取一个版本号 |
| 2 | 解析 `generation_request`（R6 载荷） | 载荷坏了却已经预占 ⇒ 白占额度 |
| 3 | **先**解析提示词（R6，在任何预占之前） | 同上，且"提示词没配"是部署问题，不该花钱 |
| 4 | 解析 `provider_id` | 预占必须用它，而它由档案解出 |
| 5 | 参考素材 claim + 绑定校验（R1） | 素材校验必须在预占之前 —— 越界/停用不该花钱 |
| 6 | 幂等查重→ 预估 → 预占 → 渲染 → 派发 | R2：先占后发 |
| 7 | R2 四道图片校验 → `store_generated_asset` → `register_version` → `insert_version` → `advance_projection` → `insert_generation_context` → 结算 | 落库顺序由 DEFERRABLE FK 决定，见下|

## ★ 为什么"先校验素材与提示词、后预占"

WP26 的 `execute_generation` docstring 已把这条定为纪律（"R6：全部task_kind
先解析提示词，在任何预占之前"）。静态图侧的**代价更高**：图片模型按**张**计费
（`PRICING_UNIT_IMAGE`），一次预占 = `content_count × 3` 张的钱 ⇒
"提示词没配"这种部署问题若在预占之后才暴露，每次都要**占住 3 张的额度**再释放。

## ★ 环 FK 的解法（与 WP26 同款）

`media_generation_contexts.media_version_id` FK 指向 `media_versions.id`，
而 `media_versions.generation_context_id` 又指回来 ⇒ **互相引用**。
两条 FK 都是 `DEFERRABLE INITIALLY DEFERRED`（`models.py`）⇒ 顺序无关。
本模块仍显式预生成 `context_id`：与 WP26 同理由 —— 让"上下文行的 id"与
"版本行里的 context_id"在**代码里就是同一个值**，而不是靠"插入后回查"得到。

## R2：四道图片入库前校验的**唯一**落点

| 故障 | 码 | 判据 |
|---|---|---|
| Pillow 打不开（坏字节 / 非图片） | `MALFORMED_MEDIA` | `Image.open` 抛 |
| 能打开但宽或高为 0 | `EMPTY_MEDIA` | `w <= 0 or h <= 0` |
| 尺寸低于合法下界（1×1 占位图） | `MEDIA_TOO_SMALL` | `min(w,h) < MIN_IMAGE_SIDE` |
| 超过像素上限（解压炸弹） | `MEDIA_TOO_LARGE` | `w*h > MAX_IMAGE_PIXELS` |

★ **像素上限的取数点唯一**：`parsing.parsers.image.MAX_IMAGE_PIXELS`。
**本地导入**它而不是复制常量 —— 复制就是第二真值来源（铁律 26），
而两处分叉的表现是"研究侧拒了这个尺寸、生成侧收了"。

## ★ OP-195 在本层的落点

`gateway_bridge.dispatch_for_media_generation` 在拿不到字节时**已经抛**
`GatewayUnavailable(REASON_GATEWAY_NO_IMAGE_REF)` ⇒ 本模块**不会**走到
第7 步的落库。因此本模块**不需要**再判"字节是不是空" ——
**判一次就够，且判据在派发那一侧**（那是有字节的那一层）。
本模块在第 7 步只做 R2 的**像素/格式**校验，不做"有没有字节"的校验。
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol, runtime_checkable

from rednote.modules.media.billing_bridge import (
    estimate_generation_cost,
    note_for_form,
    release_generation_budget,
    reserve_generation_budget,
    settle_generation_budget,
)
# `ImageByteSource` 只在 docstring 里被引用（OP-195 的说明），本模块
# **不在代码层**依赖它 —— 它的装配点是 `handlers`（由 `GatewayMediaGenerator`
# 持有）。因此不 import：import 一个"只出现在注释里的名字"会让
# `unused-import` 检查长期为红，从而**掩盖**真正的漏导入。
from rednote.modules.media.gateway_bridge import (
    GatewayUnavailable,
    ImageByteSource,
    MediaGenerationOutput,
    dispatch_for_media_generation,
    media_generation_idempotency_key,
)
from rednote.modules.media.live_pack import (
    DEFAULT_MOTION_DURATION_MS,
    LIVE_GROUP_MAX_BYTES_DEFAULT,
    STATE_SUCCEEDED,
    LiveMotionGenerator,
    LivePackRequest,
    LivePackStub,
    MotionOutput,
)
from rednote.modules.media.prompts_bridge import (
    render_system_prompt,
    resolve_active_version,
)
from rednote.modules.media.repository import MediaRepository
from rednote.modules.media.schemas import (
    SchemaViolation,
    assign_paragraph_refs,
    binding_asset_ids,
    paragraph_ref_for_sort_order,
    validate_generation_request_payload,
    validate_paragraph_ref,
    validate_reference_bindings,
)
# `paragraph_refs_for_body` 的**实现**在 `schemas`（纯函数层、已入库），
# 本模块只转出同名函数对外 ⇒ 导入时用别名避免与下面的 def 同名自递归。
from rednote.modules.media.schemas import (
    paragraph_refs_for_body as _paragraph_refs_for_body,
)
from rednote.modules.media.types import (
    EGRESS_DATA_CLASSES,
    COMPONENT_ROLE_STILL,
    LIVE_GROUPS_PER_CONTENT,
    MAX_REGENERATION_REASON_CHARS,
    MEDIA_FORM_LIVE,
    MEDIA_FORM_STATIC,
    MEDIA_SCHEMA_VERSION,
    MEDIA_STATE_FAILED,
    MEDIA_STATE_READY,
    RENDITION_KINDS,
    RENDITION_STATE_INITIAL,
    REASON_CONTENT_NOT_FOUND,
    REASON_CONTENT_VERSION_NOT_FOUND,
    REASON_GATEWAY_NO_IMAGE_REF,
    REASON_GENERATION_REQUEST_INVALID,
    REASON_GENERATOR_NOT_CONFIGURED,
    REASON_MEDIA_FORM_CONFLICT,
    REASON_MEDIA_ITEM_NOT_FOUND,
    REASON_MEDIA_EMPTY,
    REASON_MEDIA_FORMAT_UNSUPPORTED,
    REASON_MEDIA_MALFORMED,
    REASON_MEDIA_TOO_LARGE,
    REASON_MEDIA_TOO_SMALL,
    REASON_MEDIA_VERSION_NOT_FOUND,
    REASON_MODEL_PROFILE_NOT_FOUND,
    REASON_PARAGRAPH_REF_UNKNOWN,
    REASON_PROJECTION_CONFLICT,
    REASON_PROJECT_NOT_FOUND,
    REASON_REFERENCE_ASSET_NOT_FOUND,
    REASON_REFERENCE_ASSET_MISMATCH,
    REASON_REFERENCE_BINDING_MALFORMED,
    REASON_REFERENCE_BINDING_MISSING,
    REASON_RENDITION_KIND_UNKNOWN,
    REQUEST_KEY_MEDIA_FORM,
    ComponentView,
    MediaDetail,
    MediaGenerationRequest,
    MediaReferenceBinding,
    MediaSummary,
    MediaVersionSummary,
    RenditionView,
)
from rednote.modules.versions.models import ENTITY_TYPE_MEDIA

#: 图片最小合法边长（像素）。**1×1 的占位图不是"图"**。
#:
#: ⚠️ 这个下界的**依据**是产品判断而不是技术限制：下游前端要按
#: `media_renditions.width/height` 算预览布局，而 1×1 的图在布局里等价于
#: "图不存在"却占一个 `<img>`。因此它**写在这里显式判**，而不是"由前端兜"。
MIN_IMAGE_SIDE: Final[int] = 64

#: 发起期探测"同篇内容已有哪些形态"时最多读多少行（一篇内容至多 3 行静态，留余量）。
MAX_EXISTING_ROWS_PROBE: Final[int] = 50

#: Live 组 manifest 里的档案键。`live_profiles` 占位档案只能是 `PLACEHOLDER` 状态，
#: 本阶段不查表（档案选择属真机验证后的事），键名只作 manifest 留痕。
LIVE_PROFILE_KEY_DEMO: Final[str] = "placeholder-v1"

#: 可接受的图片格式（大写）。落 `media_renditions.image_format` 的取值域。
SUPPORTED_IMAGE_FORMATS: Final[frozenset[str]] = frozenset({"PNG", "JPEG", "WEBP"})

#: `measure_image` 归一化后的格式 → 落 `file_objects` 的扩展名 / MIME。
#:
#: ⚠️ **键必须与 `SUPPORTED_IMAGE_FORMATS` 逐字一致**：`measure_image`
#: 已把 `JPG` 归一成 `JPEG`，而"归一"这件事只能有一处（在那里），
#: 因此这里只认归一后的三个大写键 —— 表里再写一个 `"JPG"` 就是第二份
#: 归一口径，而两处分叉的表现是 `KeyError`（`measure_image` 放行、
#: 这里查不到），报的是"字典缺键"而不是"格式不支持"。
#:
#: ⚠️ **不是查表默认值**：查不到即 `KeyError` 是有意的 fail-closed。
#: 用 `.get(fmt, "bin")` 兜底会把"上游塞了 GIF"变成"存成一个 .bin 的
#: 资产"—— 那时 `media_renditions.image_format` 的 CHECK 才报错，
#: 而错误发生在**已经写了 blob 之后**（② 不可回退）。
_EXT_BY_FORMAT: Final[Mapping[str, str]] = {
    "PNG": "png",
    "JPEG": "jpg",
    "WEBP": "webp",
}
_MIME_BY_FORMAT: Final[Mapping[str, str]] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}

#: 提示词渲染值的键名。**与 WP26 的 `content/types.PROMPT_VALUE_*` 同形**
#: 但不同名 —— 静态图侧的值与文案侧的值不同（带参考素材），共用键名会让
#: "模板声明的字段"在两个包间看起来一样而实际不同。
PROMPT_VALUE_CONTENT_ID: Final[str] = "contentId"
PROMPT_VALUE_PROJECT_ID: Final[str] = "projectId"
PROMPT_VALUE_BATCH_KIND: Final[str] = "batchKind"
PROMPT_VALUE_REFERENCE_ASSETS: Final[str] = "referenceAssets"
PROMPT_VALUE_PARAGRAPH: Final[str] = "paragraph"


class MediaGenerationError(ValueError):
    """一次媒体生成的业务失败。**机器可判**（`.reason`）。

    **继承 `ValueError`**：与 `schemas.SchemaViolation` 同族——调用方
    （`handlers`）在一条 `except ValueError` 里接住全部"请求/数据不对"，
    靠 `reason` 区分报哪个码。
    """

    def __init__(self, *, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@runtime_checkable
class MediaGenerator(Protocol):
    """一次**出网**的能力边界（`handlers` 注入的实现）。

    ## ★ 为什么只声明 `dispatch` 一项（而不是把预占/结算/提示词也塞进来）

    照 WP26 的 `content/service.py::ContentGenerator` 同一手法：**Protocol 只
    描述"本包无法自己决定的那一步"**（此处 = 出网派发并取回图片字节），
    其余能力（提示词解析、预占/结算、落库）由 service **直调对应 bridge /
    repo**。

    初稿曾把 `resolve_prompt` / `estimate` / `reserve` / `settle` / `release`
    / `gateway_repo` 全写进 Protocol，而 `service` 也导入了三个 bridge 的函数
    —— **两套并存**，且：
    - 每个调用点都得能拿到全部依赖，缺一个就是 `AttributeError`
      （**装配故障伪装成业务失败**，排障方向错）；
    - `runtime_checkable` 的 `isinstance` 只查方法**是否存在**，
      于是"注入了但缺方法"在装配时是绿的、在运行时才炸。

    ⇒ 现在 Protocol 只留 `dispatch`。`gateway_repo` 由 `handlers` 传给
    `resolve_provider_id`（它需要网关侧读模型档案的 provider），
    **不走** generator 属性。

    ## ★ 为什么 `dispatch` 返回**出网结果**而不是 `MediaGenerationOutcome`

    初稿让它返回 `MediaGenerationOutcome`（含 `version_id` /
    `generation_context_id` / `image_file_id`）—— 那等于把**第7 步的整条
    落库链**塞进注入的实现里，与本模块 docstring 第 20 行声明的分工**矛盾**，
    且与 WP26 的 `generate() -> GenerationOutput` 不同形。

    分岔的表现不是"跑不起来"，而是**换实现就换落库语义**：
    落库顺序（`register_version` → `insert_version` → `advance_projection`
    → `insert_generation_context`）由 DEFERRABLE FK 环与不可变表共同决定，
    让它随注入的实现变化 = 让"图能不能查到"取决于装配代码的写法。

    ⇒ `dispatch` 只负责"出去、把字节与调用 id 带回来"，
    落库由 service 按固定顺序做（`_persist_generation`）。

    ## ★ 为什么入参里**没有** `bindings` / `actor_id`

    初稿的 `dispatch` 声明了这两项，而**两个真实实现（`GatewayMediaGenerator` /
    `UnavailableGenerator` / 测试桩）都没有一处读它们** —— 这正是"声称有
    能力而实际没有"（铁律 3）：接口上写着"实现方知道绑定与操作者"，
    而实现方拿到的其实是一个必须收下并丢掉的值。

    - `bindings`：参考素材在 `_execute_locked` 里已**折进 `prompt`**
      （`PROMPT_VALUE_REFERENCE_ASSETS`）⇒ 再传一份结构化绑定就是
      **第二份输入**，而两份输入分叉的表现是"提示词里写着 A 素材、
      结构化绑定写着 B"，两边都不报错。将来若真有"把素材图当图生图输入"
      的需求（WP28+），那是 `dispatch` 的**新契约**，应显式改签名而不是
      先埋一个没人读的参数。
    - `actor_id`：出网留痕（`gateway_egress` / `provider_calls`）**没有
      操作者列**；actor 的落点是**费用**（`billing_bridge` 的
      `actor_id`）与**文件归属**（`store_generated_asset(uploaded_by=…)`），
      这两件事都由 service 直做，不经过 `dispatch`。

    ⇒ 入参只留"出网必需且无法从构造期注入"的九项。
    """

    def dispatch(
        self,
        *,
        media_item_id: uuid.UUID,
        content_id: uuid.UUID,
        project_id: uuid.UUID,
        task_kind: str,
        model_profile_id: uuid.UUID,
        prompt_version_id: uuid.UUID,
        prompt: str,
        target_version_no: int,
    ) -> MediaGenerationOutput:  # pragma: no cover - 协议声明
        """出网生成一张图并取回字节。失败时抛 `GatewayUnavailable` / `ValueError` 族。"""
        ...


@dataclass(frozen=True, slots=True)
class UnavailableGenerator:
    """未接线时的生成器：**调用即抛**（fail-closed），不静默返回空输出。

    与 `content/service.py::UnavailableGenerator` 同款：它的存在是为了让
    "忘了注入真实生成器"这件事在**第一次调用**时以一条明确的错误暴露，
    而不是让整条链路跑完、最后在"没有图片字节"处才报一个含糊的错。

    ⚠️ **它必须在任何预占之前就炸**：`_execute_locked` 的第 6 步是
    "预估 → 预占 → 渲染 → 派发"，而预占发生在**派发之前**。若本类
    静默返回空输出，那笔预占要等到派发返回后才释放 —— 而静态图按张计费，
    空转一次就是 3 张的额度被占住又释放。**真实接线时的处置**：
    `handlers._build_generator` 在 `adapter is None` 时**直接不建生成器**，
    由 `service` 的 `prompt_repo is None` 分支在第 3 步就拒（更早、更准）。
    本类兜的是"注入了 UnavailableGenerator 却没配 adapter"这条路径。
    """

    reason: str = REASON_GENERATOR_NOT_CONFIGURED
    message: str = (
        "静态图生成器未接线（没有注入真实的 `MediaGenerator`）。"
        "本包 fail-closed：不生成，而不是产出一张空图。"
        "装配入口见 `handlers.py::build_task_registry`。"
    )

    def dispatch(
        self,
        *,
        media_item_id: uuid.UUID,
        content_id: uuid.UUID,
        project_id: uuid.UUID,
        task_kind: str,
        model_profile_id: uuid.UUID,
        prompt_version_id: uuid.UUID,
        prompt: str,
        target_version_no: int,
    ) -> MediaGenerationOutput:
        raise GatewayUnavailable(reason=self.reason, message=self.message)


@dataclass(frozen=True, slots=True)
class GatewayMediaGenerator:
    """真实接线：`gateway_bridge.dispatch_for_media_generation` 的薄包装。

    与 `content/service.py::GatewayGenerator` **逐条同构**（差异只有两项：
    多一个 `byte_source`，返回值就是 `MediaGenerationOutput` 而不再折一次）。
    同构是刻意的 —— 两个包的"出网"这一层若形状不同，排障时读第二遍。

    ## 为什么 `output_schema_version` / `policy_version` / `data_classes` / `round_no` 在**构造期**注入

    照 `content/service.py::GatewayGenerator` 的同一理由：它们是**部署级**
    配置（由 `build_task_registry` 注入一次），不是每请求变化的量。
    放进 `dispatch()` 会让每个调用点都要记得传 —— 而"忘传"的默认值与部署
    约定不一致时**不报错**，只是网关侧的策略版本悄悄不同。

    ## `job_id` / `attempt_id` 可为 `None`，但 `gateway_bridge` 会拒

    `dispatch_for_media_generation` 在两者为 `None` 时抛 `GatewayUnavailable`
    （"没有留痕的出网是 ADR-10 的违规盲区"）。本类**如实透传** `None`，
    不在这里预判 —— 判定只有一处（`gateway_bridge`）。

    ## `byte_source` 允许为 `None`（**这在本机是常态**）

    `byte_source is None` ⇒ `dispatch_for_media_generation` 抛
    `GatewayUnavailable(REASON_GATEWAY_NO_IMAGE_REF)`（OP-195 未关闭）。
    本类**不替它兜底**、也**不预判**：本机当前确实无人提供字节来源，
    生产必然走 fail-closed，**这是正确状态**而不是缺陷 ——
    在 `handlers` 里给一个"看起来能取到字节"的默认实现，
    会让"链路已验证"变成假话（铁律 3）。
    """

    gateway_repo: Any  # `GatewayRepository`；此处不 import（R7：重模块本地导入）
    adapter: Any  # `CapabilityAdapter`
    byte_source: ImageByteSource | None
    job_id: uuid.UUID | None
    attempt_id: uuid.UUID | None
    output_schema_version: int = 1
    policy_version: str = "v1"
    round_no: int = 0
    data_classes: tuple[str, ...] = EGRESS_DATA_CLASSES

    def dispatch(
        self,
        *,
        media_item_id: uuid.UUID,
        content_id: uuid.UUID,
        project_id: uuid.UUID,
        task_kind: str,
        model_profile_id: uuid.UUID,
        prompt_version_id: uuid.UUID,
        prompt: str,
        target_version_no: int,
    ) -> MediaGenerationOutput:
        return dispatch_for_media_generation(
            self.gateway_repo,
            adapter=self.adapter,
            byte_source=self.byte_source,
            project_id=project_id,
            content_id=content_id,
            media_item_id=media_item_id,
            target_version_no=target_version_no,
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


@dataclass(frozen=True, slots=True)
class MediaGenerationOutcome:
    """一次生成的**最终**结果（回给 `handlers` 与 `api` 的唯一形状）。

    ⚠️ 它是 **service 的产物**，不是 `dispatch` 的返回值 ——
    落库链在 `_persist_generation` 里做（理由见 `MediaGenerator` docstring
    第二节）。

    `detail` 是**重读出来**的版本行，不是刚插进去的 ORM 对象 ——
    ORM 对象绑定在 session 上，返回给 `handlers` 后再访问属性会抛
    `DetachedInstanceError`（与 `versions.service._as_registered` 同款理由）。
    """

    media_item_id: uuid.UUID
    version_id: uuid.UUID
    version_no: int
    paragraph_ref: str
    image_file_id: uuid.UUID
    provider_call_id: uuid.UUID
    generation_context_id: uuid.UUID
    state: str
    detail: MediaVersionSummary
    #: 结算失败而未结清（额度差异由 WP25 的对账路径兜）。
    usage_pending: bool = False
    #: 本次产出的形态；Live 时 `components` 为四个组件视图（静态恒为空）。
    media_form: str = MEDIA_FORM_STATIC
    components: tuple[ComponentView, ...] = ()


# ---------------------------------------------------------------------------
# 模型档案 → provider_id（**全包唯一的解析点**）
# ---------------------------------------------------------------------------


def resolve_provider_id(gateway_repo: Any, *, model_profile_id: uuid.UUID) -> uuid.UUID:
    """由模型档案 id 解出 `provider_id`。**全包唯一的解析点**。

    与 WP26 的 `content/service.py::resolve_provider_id` 同理由：
    `MediaGenerationRequest` **刻意不含** `provider_id`（`types.py` 已论证），
    存进来就有两个来源，而分叉的表现是"预占记在 A 供应商、实际调用走 B"，
    账面与留痕对不上却都**不报错**。

    抛 `MediaGenerationError(MODEL_PROFILE_NOT_FOUND)`：档案不存在是
    "请求里的字段值指向一个不存在的对象" ⇒ 端点翻成 422（不是 404——
    它是被引用的**字段值**，不是被寻址的资源）。
    """
    profile = gateway_repo.get_model_profile(model_profile_id)
    if profile is None:
        raise MediaGenerationError(
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
# R2：图片入库前校验（**本模块唯一的像素测量点**）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImageMeasurement:
    """本机用 Pillow **实测**出来的图片事实。

    ⚠️ **实测而不是抄模型返回值**：`ImageByteSource` 声明的
    `width` / `height` / `image_format` 是**实现方填的**，本模块**不信任**
    它们 —— 它用同一份字节重新打开重测。理由：抄上游的自我描述，
    就等于把"这张图多大"建立在对方的诚实上。
    """

    width: int
    height: int
    image_format: str
    byte_size: int


def measure_image(content: bytes) -> ImageMeasurement:
    """打开字节、实测四件事实；任何一道不合法即 `MediaGenerationError`。

    这是**全包唯一**的像素测量点。`ImageByteSource` 声明的尺寸与格式
    **不被信任**（见 `ImageMeasurement` 的 docstring）。

    ## 四道判据的顺序（与 `types.py` 的 `REASON_*` 定义处一致）

    1. **打不开** ⇒ `MALFORMED_MEDIA`（坏字节/ 非图片）；
    2. **宽或高 ≤ 0** ⇒ `EMPTY_MEDIA`（能打开但没有面积）；
    3. **格式不在 `SUPPORTED_IMAGE_FORMATS`** ⇒ `MEDIA_FORMAT_UNSUPPORTED`；
    4. **像素超上限** ⇒ `MEDIA_TOO_LARGE`（解压炸弹防护）。

    ⚠️ **顺序 3 在 4 之前是刻意的**：格式不对时**先报格式**，
    因为"这根本不是我们能用的格式"比"它太大了"更接近根因，
    而一个超大尺寸的未知格式图若先撞 4，运维看到"太大了"会去调上限
    —— 调完仍然不可用。

    ⚠️ **像素上限取自 `parsing`（本地导入）**：`MAX_IMAGE_PIXELS` 的唯一
    定义在 `rednote.modules.parsing.parsers.image`。本模块**本地导入**它而
    不是复制常量 —— 复制就是第二真值来源（铁律 26），而两处分叉的表现是
    "研究侧拒了这个尺寸、生成侧收了"。

    ⚠️ **`min(w, h) < MIN_IMAGE_SIDE` 报 `MEDIA_TOO_SMALL`**：1×1 的占位图
    不是"小图"，它是"没有图"。判据在前端渲染之前拦住它。
    """
    # **本地导入**（R7）+ **单一取数点**（铁律 26）。
    from rednote.modules.parsing.parsers.image import MAX_IMAGE_PIXELS

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 环境缺依赖时的显式拒绝
        raise MediaGenerationError(
            reason=REASON_MEDIA_MALFORMED,
            message=(
                "本机没有装 Pillow，无法校验图片字节。"
                "（这是部署缺依赖，不是用户输入问题 —— "
                "但本包 fail-closed：验不了就不落库。）"
            ),
        ) from exc

    if not content:
        raise MediaGenerationError(
            reason=REASON_MEDIA_MALFORMED,
            message="图片字节为空字符串/空字节流。",
        )

    try:
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            image_format = (image.format or "").upper()
    except Exception as exc:
        #⚠️ 捕获范围**刻意放宽到 `Exception`**：Pillow 对坏字节抛的异常类型
        # 随版本变化（`UnidentifiedImageError` / `OSError` / `struct.error`
        # 都出现过），窄捕获会在换版本后变成 500 —— 而根因是"字节坏了"。
        raise MediaGenerationError(
            reason=REASON_MEDIA_MALFORMED,
            message=(
                f"图片字节无法被解析（{type(exc).__name__}: {exc}）。"
                "（要么不是图片，要么字节在传输中损坏。）"
            ),
        ) from exc

    if width <= 0 or height <= 0:
        raise MediaGenerationError(
            reason=REASON_MEDIA_EMPTY,
            message=(
                f"图片尺寸非法：{width}×{height}。"
                "（能被打开但没有面积 —— 通常是元数据被截断。）"
            ),
        )

    normalized_format = "JPEG" if image_format == "JPG" else image_format
    if normalized_format not in SUPPORTED_IMAGE_FORMATS:
        raise MediaGenerationError(
            reason=REASON_MEDIA_FORMAT_UNSUPPORTED,
            message=(
                f"不支持的图片格式：{image_format or '(空)'}。"
                f"本包只接受 {sorted(SUPPORTED_IMAGE_FORMATS)}。"
            ),
        )

    if width * height > MAX_IMAGE_PIXELS:
        raise MediaGenerationError(
            reason=REASON_MEDIA_TOO_LARGE,
            message=(
                f"图片像素数 {width}×{height}={width * height} "
                f"超过上限 {MAX_IMAGE_PIXELS}。"
                "（这是解压炸弹防护 —— 上限与研究侧解析图片共用同一个常量，"
                "两边各写一份会让『研究拒了、生成收了』。承 OP-164/171："
                "规格源原件的像素口径待复核。）"
            ),
        )

    if min(width, height) < MIN_IMAGE_SIDE:
        raise MediaGenerationError(
            reason=REASON_MEDIA_TOO_SMALL,
            message=(
                f"图片尺寸过小：{width}×{height}"
                f"（最小边要求 {MIN_IMAGE_SIDE}）。"
                "（1×1 之类的占位图不是『小图』，它是『没有图』——"
                "下游按宽高算预览布局，占位图在布局里与缺图等价。）"
            ),
        )

    return ImageMeasurement(
        width=width,
        height=height,
        image_format=normalized_format,
        byte_size=len(content),
    )


# ---------------------------------------------------------------------------
# 段落引用（R4）
# ---------------------------------------------------------------------------


def paragraph_refs_for_body(body: str) -> tuple[tuple[int, str, str], ...]:
    """把正文切成段，返回 `(序号, 原文, ref)` 三元组序列。

    ## ⚠️ 本函数**不再拥有实现**，它只是 `schemas` 那份的转出（OP-197）

    原本实现在这里，但 `service.py` 长期未入库 ⇒ 以它为输入的一致性判据
    `tools/quality/check_paragraph_split_parity.py` 在**干净检出**上必然
    FAIL（`ModuleNotFoundError: media.service`）—— 判据被绑在一个还没进
    版本库的实现上，等于"有防线但门打不开"（ISS-95 族）。

    实现已搬进 `media/schemas.py`（纯函数层，同模块内可引`types`），
    那边已逐字保留原docstring 的全部论证。**本模块不保留第二份实现** ——
    留一份就等于把"切段只有一处"这个前提又破掉（多一份 ⇒ 多一种切法）。

    之所以仍在本模块导出这个名字：调用方（`service.py` 内部、以及外部
    装配/测试）按 `media.service.paragraph_refs_for_body` 取它，
    改那些调用点属于跨包改动，不该由本次口径收口顺带做。
    """
    return _paragraph_refs_for_body(body)


# ---------------------------------------------------------------------------
# 素材登记（端点 1/ 2 的服务层）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssetInput:
    """一次素材登记的入参（**已经在端点校验过形状**）。"""

    file_object_id: uuid.UUID
    product_model: str
    product_color: str | None = None
    product_part: str | None = None
    source: str | None = None
    note: str | None = None
    supersedes_asset_id: uuid.UUID | None = None


def register_reference_asset(
    media_repo: MediaRepository,
    *,
    project_id: uuid.UUID,
    asset: AssetInput,
    actor_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """登记一条产品参考素材（不可变表，**只追加**）。返回 `asset_id`。

    三道校验，各自的失败**修法不同**：

    | 校验 | 失败码 | 为什么 |
    |---|---|---|
    | 项目存在 | `PROJECT_NOT_FOUND` | 不存在则 FK 会以 500 形式炸出来 |
    | `file_object_id` 存在 | `REFERENCE_ASSET_NOT_FOUND` | 同上（FK 兜不住"文件已删"） |
    | `file_object` 属于本项目 | `REFERENCE_ASSET_PROJECT_MISMATCH` | ★ **FK 管不住**：文件存在且有`project_id`，但它是别的项目的。挂跨项目素材 = 把别的品牌的产品画进这组图|
    | `supersedes_asset_id` 属于本项目 | `REFERENCE_ASSET_PROJECT_MISMATCH` | 同上，接替关系也**不得跨项目**（否则 A 项目的素材能接替 B 项目的） |

    ⚠️ **`supersedes_asset_id` 的存在性**由 `media_renditions`/`reference_assets`
    的 FK 兜住（不可变表的 FK 是 `DEFERRABLE`）⇒ 这里**只判归属**不判存在。
    这与素材本身的"存在 + 归属"两道判是有意的差别：**新登记的行**要判
    前者，**被接替的行**只需判后者。
    """
    if not media_repo.project_exists(project_id):
        raise MediaGenerationError(
            reason=REASON_PROJECT_NOT_FOUND,
            message=f"项目 {project_id} 不存在。",
        )

    if not media_repo.file_object_exists(asset.file_object_id):
        raise MediaGenerationError(
            reason=REASON_REFERENCE_ASSET_NOT_FOUND,
            message=(
                f"文件 {asset.file_object_id} 不存在。"
                "（素材必须挂在真实存在的文件对象上 —— "
                "FK 会在提交时报错，而那时错误指向 FK 而不是根因。）"
            ),
        )

    file_project = media_repo.file_object_project(asset.file_object_id)
    if file_project is None:
        raise MediaGenerationError(
            reason=REASON_REFERENCE_ASSET_NOT_FOUND,
            message=f"文件 {asset.file_object_id} 不存在。",
        )
    if file_project != project_id:
        raise MediaGenerationError(
            reason=REASON_REFERENCE_ASSET_MISMATCH,
            message=(
                f"文件 {asset.file_object_id} 属于项目 {file_project}，"
                f"而本次登记的项目是 {project_id}。"
                "（跨项目挂素材 = 把别的品牌的产品画进这组图。"
                "这一条 FK 管不住 —— 文件存在、有项目，只是不是本项目。）"
            ),
        )

    if asset.supersedes_asset_id is not None:
        superseded_project = _asset_project(media_repo, asset.supersedes_asset_id)
        if superseded_project != project_id:
            raise MediaGenerationError(
                reason=REASON_REFERENCE_ASSET_MISMATCH,
                message=(
                    f"被接替的素材 {asset.supersedes_asset_id} 属于项目 "
                    f"{superseded_project}，而本次登记的项目是 {project_id}。"
                    "（接替关系不得跨项目 —— 否则 A 项目的素材能接替 B 项目的。）"
                ),
            )

    row = media_repo.insert_reference_asset(
        project_id=project_id,
        file_object_id=asset.file_object_id,
        product_model=asset.product_model,
        product_color=asset.product_color,
        product_part=asset.product_part,
        source=asset.source,
        note=asset.note,
        supersedes_asset_id=asset.supersedes_asset_id,
        created_by=actor_id,
    )
    return row.id


def _asset_project(media_repo: MediaRepository, asset_id: uuid.UUID) -> uuid.UUID | None:
    """素材所属项目。**本模块唯一**读素材归属的地方。

    素材不存在返回 `None` —— 与"属于别的项目"是**两件事**，调用方要分别
    给出不同的码（前者是"引用的对象不存在"404，后者是"越界"422/403）。
    """
    row = media_repo.get_reference_asset(asset_id)
    return None if row is None else row.project_id


# ---------------------------------------------------------------------------
# 发起（**与钱无关**）与读 —— 五个对外入口
# ---------------------------------------------------------------------------
#
# 与 `content/service.py` 的 `start_generation` / `read_content_detail` 同一
# 分工：本组**不预占、不派发、不入队**。
#
# | 入口 | 端点 | 类型 |
# |---|---|---|
# | `start_media_generation` | ③ `POST /contents/{contentId}/media-generations` | 写（发号） |
# | `list_media` | ④ `GET /contents/{contentId}/media` | 读 |
# | `read_media_detail` | ⑤ `GET /media/{mediaId}` | 读 |
# | `start_regeneration` | ⑥ `POST /media/{mediaId}/regenerations` | 写（改暂存位） |
# | `start_rendition` | ⑦ `POST /media/{mediaId}/renditions` | 写（建加工行） |
#
# **为什么"发起"不预占**：与 WP26 的 `start_generation` 逐字同一条理由
# （见模块 docstring 与 `content/service.py` 的「预占为什么必须在执行阶段」）
# —— 「发起」与「执行」是**两次事务**，钱必须在执行那次事务里预占。
# 本组只做与钱无关的事：建身份行、把"要什么"落进暂存位、把幂等键留在行上。
#
# **为什么"发起"不调 `enqueue_job`**：入队要 `JobRepository`，而本组只收
# `MediaRepository` —— 把两个仓库都塞进来会让"发起"变成又一个能碰 `jobs`
# 的地方（本包**不改** `jobs` 的资产，接缝冻结铁律）。
# 入队由 `api.py` 做，顺序见该文件的端点 docstring。


def _parse_bindings_shape(
    raw_bindings: Any, *, what: str
) -> tuple[MediaReferenceBinding, ...]:
    """R1 段 1：只解析**形状**（键齐不多 / UUID 合法 / `part`·`note` 是 str 或 null）。

    ★ **本函数是"绑定形状解析"的唯一实现**，两个调用点：
    `_claim_and_validate`（执行期段 1）与 `start_media_generation`
    （发起期的入参校验）。抽出来是因为"发起时校验一次形状"与"执行时校验
    一次形状"是**同一件事** —— 分两处写，一处改了另一处不改的表现是
    "端点收了、处理器炸了"，而两侧各自的测试都过（铁律 26）。

    **不判存在性 / 归属 / 可用性**（那些要 `claim_assets`，只在执行期做）：
    发起期判它们会把 R1 的判据变成两处实现，而两处的"素材库快照"必然
    来自不同时刻 ⇒ 一次生成里绑定的合法性有两个答案。

    ⚠️ 与 `_claim_and_validate` 段 1 的**同一条**取舍：这里用
    `MediaReferenceBinding.from_payload`（fail-closed 的形状校验器）
    而**不是** `schemas._parse_binding`（私有，且它的职责是把
    `from_payload` 的 `ValueError` 翻成 `SchemaViolation`）。
    形状错翻成 `SchemaViolation` 是**本函数**的职责，带 `field` 定位
    （`referenceBindings[i]`）—— 于是"哪一条坏了"可判。
    """
    if not isinstance(raw_bindings, list):
        raise SchemaViolation(
            field="referenceBindings",
            reason=REASON_REFERENCE_BINDING_MALFORMED,
            message=(
                f"{what}参考素材绑定必须是数组，"
                f"实际是 {type(raw_bindings).__name__}。"
            ),
        )
    parsed: list[MediaReferenceBinding] = []
    for position, item in enumerate(raw_bindings):
        try:
            parsed.append(MediaReferenceBinding.from_payload(item))
        except ValueError as exc:
            raise SchemaViolation(
                field=f"referenceBindings[{position}]",
                reason=REASON_REFERENCE_BINDING_MALFORMED,
                message=(
                    f"{what}的第 {position} 条参考素材绑定形状不合法：{exc}"
                ),
            ) from exc
    return tuple(parsed)


def start_media_generation(
    media_repo: MediaRepository,
    *,
    project_id: uuid.UUID,
    content_id: uuid.UUID,
    request: MediaGenerationRequest,
    bindings_by_sort_order: Mapping[int, Any] | None = None,
    idempotency_key: str,
    created_by: uuid.UUID | None = None,
) -> tuple[tuple[Any, ...], bool]:
    """发起一次媒体生成：物化身份行。返回 `(行, created)`。

    ## 行数由形态决定（WP28）

    | 形态 | 物化几行 | 预占几张 |
    |---|---|---|
    | `STATIC`（缺省） | 恒 `images_per_content("STATIC")`（=3） | 每行 1 张，共 3 张 |
    | `LIVE` | 恒 `LIVE_GROUPS_PER_CONTENT`（=1，每内容一组，裁定 α） | 该行预占 `images_per_content("LIVE")`（=2：STILL+PREVIEW） |

    形态唯一来源是 `request.media_form`（铁律 26），不另设入参。
    同一篇内容上形态不一致 ⇒ 发起期抛 `MEDIA_FORM_CONFLICT`。

    与 `content/service.py::start_generation` 同构；媒体按形态物化身份行：
    静态三行，Live 一行。身份键为 `(content_item_id, sort_order)`，
    每行的 `paragraph_ref` 由 `assign_paragraph_refs` 派生（OP-199 顺序对位）。

    ## ★ 数量**不由调用方传**（R3：一个量只能有一个来源）

    静态行数来自 `images_per_content(STATIC)`；Live 组数来自
    `LIVE_GROUPS_PER_CONTENT`。张数只用于计费，不决定身份行数。

    ## `bindings_by_sort_order`：键是 `sort_order`，值是那一条的原始绑定

    | 形态 | 键域 | 含义 |
    |---|---|---|
    | `STATIC` | `{0, 1, 2}` | 三个身份行各有自己的绑定 |
    | `LIVE` | `{0}` | 一个 Live 组身份行有一组绑定 |
    | 任意形态 | 缺失或多余键 | 拒绝，不把漏绑定与明确空绑定混为一谈 |

    ★ **键域必须恰好是 `range(len(paragraph_refs))`，缺键/越界即拒**。
    不这么做的话，"漏了一张的绑定"与"那张本来就该没有绑定"在存储上同形
    （都是 `NULL`），而 R1 的"每张图至少绑定一条"就**退化成恒真**。

    ⚠️ **本函数只做绑定形状校验（段 1），不做存在性/归属/可用性（段 2）**：
    段 2 要 `claim_assets`，它在执行期做（`_claim_and_validate`）。
    这是刻意的 —— 发起期判"素材可用"会让同一次生成里绑定的合法性
    有两个来自不同时刻的答案。

    ## 幂等：命中即原样返回，**不覆盖**

    先按 `last_idempotency_key` 找回那一批（`list_media_items_by_idempotency`）。
    命中 ⇒ `created=False`，原样返回既有行。**不**调 `set_generation_request`
    —— 覆盖会让"第一次说绑定 A、第二次说绑定 B"这种**同键不同体**的请求
    悄悄改掉第一批（而那一批的任务可能已经跑完了）。
    同键不同体是**客户端的错**，正确处置是让端点按 `IDEMPOTENCY_CONFLICT` 拒
    （本函数不做 HTTP 判断，只保证"命中即原样返回"这个不含歧义的行为）。

    ## 为什么 `paragraph_ref` 在**发起时**就要派

    `media_items.paragraph_ref` 是 **NOT NULL**（身份行在生成之前就建），
    因此发起那一刻必须有值。派法是 `assign_paragraph_refs` ——
    **对位规则的唯一实现**（OP-199）。

    ⚠️ 这个值只是**当前版本的投影初值**：执行期会按**当时正文**重算
    （老大 2026-10-08 拍板「执行时按当时正文重算」，见
    `schemas.paragraph_ref_for_sort_order`）。发起时派它是为了满足
    NOT NULL 与"身份键的一半可读"，**不是**把它当权威。
    """
    # --- 第 1 步：内容存在 + 属于本项目（防枚举：两者报同一个码）---------
    content_project = media_repo.content_item_project(content_id)
    if content_project is None or content_project != project_id:
        raise MediaGenerationError(
            reason=REASON_CONTENT_NOT_FOUND,
            message=f"内容条目 {content_id} 不存在。",
        )

    # --- 第 2 步：幂等查重（命中即原样返回）-----------------------------
    existing = media_repo.list_media_items_by_idempotency(
        content_item_id=content_id, idempotency_key=idempotency_key
    )
    if existing:
        return tuple(existing), False

    # --- 第 2.5 步：形态冲突（WP28）---------------------------------------
    #
    # 形态只有一个来源：`request.media_form`（**不另设 `form` 入参** ——
    # 第二个取数来源，铁律 26）。
    # `uq_media_items_content_sort` 让 Live 组行（sort_order=0）与静态封面同位；
    # 同一篇内容上形态不一致 ⇒ 发起期可读拒绝，不让数据库抛 `IntegrityError`。
    form = request.media_form
    for existing_row in media_repo.list_media_items(
        content_item_id=content_id, limit=MAX_EXISTING_ROWS_PROBE, offset=0
    ):
        existing_form = (existing_row.generation_request or {}).get(
            REQUEST_KEY_MEDIA_FORM, MEDIA_FORM_STATIC
        )
        if existing_form != form:
            raise MediaGenerationError(
                reason=REASON_MEDIA_FORM_CONFLICT,
                message=(
                    f"内容 {content_id} 上已有 {existing_form} 形态的媒体，"
                    f"不能再发起 {form}（两者共用 sort_order 命名空间）。"
                ),
            )

    # --- 第 3 步：按**当前正文**派生每张图的初始 `paragraph_ref` ----------
    body = media_repo.content_body_for_paragraphs(content_id)
    if body is None:
        raise MediaGenerationError(
            reason=REASON_CONTENT_VERSION_NOT_FOUND,
            message=(
                f"内容 {content_id} 没有任何版本行（`content_versions` 为空）"
                "⇒ 没有正文可对齐，无法确定这 3 张图各自画哪一段。"
            ),
        )
    # 空正文 ⇒ 抛 `SchemaViolation(PARAGRAPH_REF_UNKNOWN)`（fail-closed）。
    # **不在发起期放过它**：放过的话图照生成、坐标是编的，而根因要等每一张
    # 各自失败一次才暴露（每张白买一次）。
    paragraph_refs = assign_paragraph_refs(
        paragraph_refs_for_body(body),
        what=f"内容 {content_id} 的图片段落对位",
    )
    if form == MEDIA_FORM_LIVE:
        # Live：每内容 `LIVE_GROUPS_PER_CONTENT`（1）个身份行，取首段。
        # 行数 ≠ 张数（张数是 `images_per_content("LIVE")`=2，只管计费）。
        paragraph_refs = paragraph_refs[:LIVE_GROUPS_PER_CONTENT]

    # --- 第 4 步：绑定形状 + 键域校验（**在第 5 步之前**）-----------------
    raw_by_sort: Mapping[int, Any] = bindings_by_sort_order or {}
    expected_orders = set(range(len(paragraph_refs)))
    given_orders = set(raw_by_sort)
    if given_orders != expected_orders:
        raise SchemaViolation(
            field="bindingsBySortOrder",
            reason=REASON_REFERENCE_BINDING_MISSING,
            message=(
                f"内容 {content_id} 的绑定必须**逐张**给出，键域恰好是 "
                f"{sorted(expected_orders)}（每篇内容恒 "
                f"{len(paragraph_refs)} 行，形态 {form}），"
                f"实际给了 {sorted(given_orders)}。"
                "（缺键会让『漏了一张』与『那张本来就没绑定』在存储上同形"
                " ⇒ R1 的『每张图至少绑定一条』退化成恒真。）"
            ),
        )
    parsed_bindings: dict[int, list[Any]] = {}
    for sort_order in sorted(raw_by_sort):
        parsed = _parse_bindings_shape(
            raw_by_sort[sort_order],
            what=f"内容 {content_id} 第 {sort_order} 张图的",
        )
        parsed_bindings[sort_order] = [b.as_payload() for b in parsed]

    # --- 第 5 步：物化 N 行身份行（与钱无关）----------------------------
    #
    # `generation_request` 与 `pending_reference_bindings` 与身份行**同一次
    # INSERT**（`insert_media_item` 的 docstring 已论证）：分开写会造出
    # "身份在、参数/绑定还没写"的中间态，而那个中间态**恰好就是处理器
    # 读到的样子**（读到 None ⇒ 判装配故障 / 判 R1 没有绑定）。
    payload = request.as_payload()
    created_rows = tuple(
        media_repo.insert_media_item(
            project_id=project_id,
            content_item_id=content_id,
            paragraph_ref=paragraph_refs[sort_order],
            sort_order=sort_order,
            created_by=created_by,
            idempotency_key=idempotency_key,
            generation_request=payload,
            pending_reference_bindings=parsed_bindings[sort_order],
        )
        for sort_order in range(len(paragraph_refs))
    )
    return created_rows, True


def list_media(
    media_repo: MediaRepository,
    *,
    content_id: uuid.UUID,
    limit: int,
    offset: int,
) -> tuple[tuple[MediaSummary, ...], int]:
    """一条内容的媒体列表（`GET /contents/{contentId}/media`）。返回 `(行, 总数)`。

    与 `content/service.py::list_contents` 同款：
    `limit`/`offset` 原样下推给仓储（**排序键由仓储定** —— 让端点在内存里
    重排会出现"仓储按 A 排、端点按 B 排"，而游标是按端点的排序编码的，
    不一致的表现是**翻页重复或漏行**，不报错）。

    排序是 `sort_order` 升序（`repository.list_media_items` 的 docstring
    已论证：`sort_order` 是"第几张图"这个问题的**显式答案**，
    按 `created_at` 排会在同一事务插入的多行上给出不确定的顺序）。

    ## 内容不存在 ⇒ 抛，而不是回空列表

    与 `read_version_history` **逐字同款**："这条内容一张图都没有"与
    "这条内容根本不存在"是**两件不同的事**，而空列表让两者同形 ——
    前端会把一个拼错的内容 id 显示成"还没有生成图片"。
    因此先判内容在不在（`content_item_project`），不存在抛
    `CONTENT_NOT_FOUND`（`api.py` 翻成 404）。
    """
    if media_repo.content_item_project(content_id) is None:
        raise MediaGenerationError(
            reason=REASON_CONTENT_NOT_FOUND,
            message=f"内容条目 {content_id} 不存在。",
        )

    rows = media_repo.list_media_items(
        content_item_id=content_id, limit=limit, offset=offset
    )
    total = media_repo.count_media_items(content_item_id=content_id)
    return (
        tuple(
            MediaSummary(
                id=row.id,
                content_id=row.content_item_id,
                project_id=row.project_id,
                state=row.state,
                # ⚠️ 这一列是**投影**（当前版本的投影，可被重算改写），
                # 不是历史事实 —— 历史事实在 `media_versions.paragraph_ref`。
                # 类型 docstring 已论证"它为什么是 ref 而不是下标"。
                paragraph_ref=row.paragraph_ref,
                sort_order=int(row.sort_order),
                current_version_id=row.current_version_id,
                current_version_no=int(row.current_version_no),
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ),
        total,
    )


def read_media_detail(
    media_repo: MediaRepository,
    *,
    media_item_id: uuid.UUID,
    version_limit: int,
    version_offset: int,
) -> MediaDetail:
    """读一条媒体的完整详情（`GET /media/{mediaId}`）。

    一次给出：身份 + 当前版本（`payload` / 绑定）+ 版本历史 + 加工产物。
    **全部复用同一个 session**（`media_repo.session`）—— 分属本模块四张表
    与 WP11 的 `version_nodes`，不同事务会让同一个响应里的字段来自不同时刻。

    ## `renditions` 取的是**当前版本**的产物，不是全部版本的

    "预览图"是**针对某一版**的加工产物（`media_renditions.media_version_id`
    是它的身份键的一半）—— 把历次版本的产物全拉回来，会让前端把
    "第 1 版的预览"显示在第 3 版下面，而两者画的**不是同一张图**。

    ## `reference_assets` 给的是**当前版本**绑定过的那几条

    它是 `media_versions.reference_bindings`（不可变的历史事实），
    不是 `media_items.pending_reference_bindings`（下一次要绑什么）——
    详情页要回答的是"**这一版**画的时候参照了哪些素材"（AQ-03 的对照面）。

    ## 为什么 `payload` 是"原始 dict"而不是某个值类型

    `MediaDetail.payload` 的类型是 `dict[str, Any] | None`：它的形状由
    `MEDIA_SCHEMA_VERSION` 定义，而"读出来是什么形状"这件事**由版本号
    决定**，不由 Python 类型决定（类型 docstring 已写明）。

    媒体不存在 ⇒ 抛 `MEDIA_ITEM_NOT_FOUND`（`api.py` 翻成 404）。
    """
    item = media_repo.get_media_item(media_item_id)
    if item is None:
        raise MediaGenerationError(
            reason=REASON_MEDIA_ITEM_NOT_FOUND,
            message=f"媒体条目 {media_item_id} 不存在。",
        )

    version = (
        media_repo.get_version(item.current_version_id)
        if item.current_version_id is not None
        else None
    )
    version_rows = media_repo.list_versions(
        media_item_id=media_item_id, limit=version_limit, offset=version_offset
    )
    renditions = (
        media_repo.list_renditions(media_version_id=version.id)
        if version is not None
        else []
    )
    # Live 组件只挂在 LIVE 版本上；静态版本不查表（零额外查询、零回归）。
    components = (
        media_repo.list_components(media_version_id=version.id)
        if version is not None and version.media_form == MEDIA_FORM_LIVE
        else []
    )

    return MediaDetail(
        id=item.id,
        content_id=item.content_item_id,
        project_id=item.project_id,
        state=item.state,
        paragraph_ref=item.paragraph_ref,
        sort_order=int(item.sort_order),
        current_version_id=item.current_version_id,
        current_version_no=int(item.current_version_no),
        # 造一个 `or {}` 兜底会把"读到坏数据"变成"读到空载荷"（静默）——
        # 与 `read_version_history` 里拒绝 `or ""` 是同一条理由。
        payload=dict(version.payload) if version is not None else None,
        reference_assets=(
            tuple(dict(binding) for binding in (version.reference_bindings or ()))
            if version is not None
            else ()
        ),
        renditions=tuple(_rendition_view(row) for row in renditions),
        versions=tuple(_version_summary(row) for row in version_rows),
        media_form=version.media_form if version is not None else MEDIA_FORM_STATIC,
        components=tuple(_component_view(row) for row in components),
    )


def _component_view(row: Any) -> ComponentView:
    """`MediaComponent` → 不可变视图。**本模块唯一**的组件视图构造点。"""
    return ComponentView(
        id=row.id,
        media_version_id=row.media_version_id,
        role=row.role,
        file_id=row.file_id,
        verification=row.verification,
        sha256=row.sha256,
        byte_size=int(row.byte_size),
    )


def _rendition_view(row: Any) -> RenditionView:
    """`MediaRendition` → 不可变视图。**本模块唯一**的产物视图构造点。"""
    return RenditionView(
        id=row.id,
        media_version_id=row.media_version_id,
        kind=row.kind,
        state=row.state,
        file_object_id=row.file_object_id,
        width=None if row.width is None else int(row.width),
        height=None if row.height is None else int(row.height),
        byte_size=None if row.byte_size is None else int(row.byte_size),
        image_format=row.image_format,
        note=row.note,
        created_at=row.created_at,
    )


def _version_summary(row: Any) -> MediaVersionSummary:
    """`MediaVersion` → 版本历史项。**本模块唯一**的版本摘要构造点。

    与 `_persist_generation` 末尾那份是**同一个形状**（`MediaVersionSummary`），
    但**不共用构造函数**：那一份的入参是刚插进去的 `stored_version`
    （它在事务里、有 `generation_context_id` 的确定值），这一份是重读出来的
    历史行。共用要先把行转成同一个中间形状，而那个中间形状**没有别的消费者**
    —— 多一层转换只会让"读路径"与"写路径"互相牵制。
    """
    return MediaVersionSummary(
        id=row.id,
        version_no=int(row.version_no),
        paragraph_ref=row.paragraph_ref,
        sort_order=int(row.sort_order),
        image_file_id=row.image_file_id,
        generation_context_id=row.generation_context_id,
        payload_hash=row.payload_hash,
        schema_version=int(row.schema_version),
        created_at=row.created_at,
        created_by=row.created_by,
        media_form=row.media_form,
    )


def start_regeneration(
    media_repo: MediaRepository,
    *,
    media_item_id: uuid.UUID,
    request: MediaGenerationRequest,
    reason: str | None,
    bindings: Any = None,
) -> Any:
    """单张重做的**前置**（`POST /media/{mediaId}/regenerations`）。返回被改的行。

    与 `content/service.py::prepare_regeneration` **同构**：重做**不建身份行**
    （媒体行已存在），只改**暂存位**：

    | 改哪一列 | 为什么 |
    |---|---|
    | `generation_request`（覆盖） | "换档位 / 换模型档案"本来就要换参数；它是**最后一次意图**，不是历史事实（历史事实在每次生成一行的 `media_generation_contexts`） |
    | `pending_reference_bindings` | R1 的下一次输入；**不覆盖成"没有绑定"**（见下） |
    | `pending_regeneration_reason` | F7：这条要落进**不可变版本行**的 `regeneration_reason`，丢了补不回来 |

    ## ★ 为什么 `reason` 与 `bindings` 要**落进暂存位**（而不是随任务参数带走）

    生成是**异步**的：端点收下之后入队，任务在**另一次事务**里才跑。
    而"这一格这一轮请求了什么/绑什么/为什么重做"三件事**只有端点那次事务
    知道** —— 中间那一列就是 `media_items` 上的三个暂存列。
    不落的话：绑定丢失（R1 退化成"没有绑定"，拿到空集合而不报错）、
    重做理由恒 NULL（列在、值恒空、不报错）。

    ## `bindings=None` 只是"**不改**这一列"，不是"清空"

    两条不同的语义，**刻意分开**：

    | 给法 | 效果 |
    |---|---|
    | 不传（`None`） | **保留**上一次留下的绑定 —— 重做同一张图通常绑的还是那几条素材 |
    | 传 `[]` | 会落一个空数组 ⇒ 被 `bindings_size` CHECK 拒（R1 要求至少一条） |

    ⚠️ **"清空"这个动作没有入口**：落库成功后的清空由
    `_persist_generation` 的第 ⑤ 步做（生成成功后那一列**必须**是 NULL），
    而不是由端点调 —— 端点清空会让"清空了但生成没跑"变成
    "下一次重做没有绑定"（静默退化成 R1 失效）。

    ## `reason` 的长度上界

    取 `types.MAX_REGENERATION_REASON_CHARS`（与
    `media_versions.regeneration_reason` 那一列**同源**）——
    两处各写一个上界的表现是"端点按 A 校验通过、DB 按 B 拒掉"，
    而报出来的是 SQLSTATE 而不是一个业务 reason 码。
    **`None` 是合法的**（与 `types.PENDING_REGENERATION_REASON_SQL` 的
    "不落非空 CHECK"一致：首次生成与"重做但没写理由"在列上同形）。

    媒体不存在 ⇒ 抛 `MEDIA_ITEM_NOT_FOUND`（`api.py` 翻成 404）。
    """
    item = media_repo.get_media_item(media_item_id)
    if item is None:
        raise MediaGenerationError(
            reason=REASON_MEDIA_ITEM_NOT_FOUND,
            message=f"媒体条目 {media_item_id} 不存在。",
        )

    if reason is not None:
        if not isinstance(reason, str) or not reason.strip():
            raise MediaGenerationError(
                reason=REASON_GENERATION_REQUEST_INVALID,
                message=(
                    f"媒体条目 {media_item_id} 的重做理由必须是**非空**字符串"
                    f"或 null，实际是 {reason!r}。"
                    "（空串与『没说』是两件事：前者说明写入方给了个坏值。）"
                ),
            )
        if len(reason) > MAX_REGENERATION_REASON_CHARS:
            raise MediaGenerationError(
                reason=REASON_GENERATION_REQUEST_INVALID,
                message=(
                    f"媒体条目 {media_item_id} 的重做理由超长："
                    f"{len(reason)} > {MAX_REGENERATION_REASON_CHARS}。"
                    "（上界与 `media_versions.regeneration_reason` 的列长同源 ——"
                    "两处各写一个的表现是端点放行、DB 按 SQLSTATE 拒。）"
                ),
            )

    media_repo.set_generation_request(
        media_item_id=media_item_id, request=request.as_payload()
    )
    if bindings is not None:
        parsed = _parse_bindings_shape(
            bindings, what=f"媒体条目 {media_item_id} 重做的"
        )
        media_repo.set_pending_reference_bindings(
            media_item_id=media_item_id,
            bindings=[b.as_payload() for b in parsed],
        )
    media_repo.set_pending_regeneration_reason(
        media_item_id=media_item_id, reason=reason
    )
    return item


def start_rendition(
    media_repo: MediaRepository,
    *,
    media_item_id: uuid.UUID,
    media_version_id: uuid.UUID,
    kind: str,
    note: str | None = None,
) -> RenditionView:
    """登记一次**加工**（`POST /media/{mediaId}/renditions`）。返回新的产物行。

    ## ★ 这一层**不做加工**，只建一条 `PENDING` 行

    全仓检索：`media_renditions` 只有仓储原语（`next_attempt_no` /
    `insert_rendition`）与取值常量（`RENDITION_KINDS` / `RENDITION_STATES`），
    **没有任何加工 worker**（FFmpeg / Pillow 转码的调用点一处都没有）。
    因此本函数的职责边界是**登记**："这一次要加工，档位是 X" ——
    真实加工（生成预览 / 转正式资产）是另一个工作包的事。

    ⚠️ **不在这里假装加工成功**：`state` 落 `RENDITION_STATE_INITIAL`
    （= `PENDING`）。写 `READY` 会当场撞 `ready_requires_artifact` CHECK
    （`READY` 要求五列齐全），而绕过 CHECK 去编一个 `file_object_id`
    会留下一行**指向不存在资产**的产物 —— 而 WP35 的 ZIP 打包会把它
    当成可交付资产打进去（`RENDITION_STATE_PENDING` 的 docstring 原文）。

    ## 为什么要 `next_attempt_no` 而不是"每个 (版本, 档位) 一行"

    加工会失败（源图损坏 / 格式不支持 / 磁盘满），失败之后要能**再试**。
    用 `max + 1` 取号 + `uq_media_renditions_version_kind_attempt` 兜并发，
    于是历史尝试全部留痕（"第 2 次的产物为什么和第 1 次不一样"可查），
    而"同一档位只有一行"会把重试的历史抹掉。

    ## `media_version_id` 必须属于 `media_item_id`

    ★ **这条 FK 管不住**：`media_renditions.media_version_id` 指向
    `media_versions.id`，而"那一版是不是这一格的"要跨表判
    （`media_versions.media_item_id`）。不判的话，可以把**别的媒体格**
    的版本拿来加工，产出一条挂在 A 格、内容是 B 格的产物行 ——
    它在两个格子的详情页里都显示，且**都不报错**。

    媒体不存在 ⇒ `MEDIA_ITEM_NOT_FOUND`；
    版本不存在 / 不属于这一格 ⇒ `MEDIA_VERSION_NOT_FOUND`；
    档位不在 `RENDITION_KINDS` ⇒ `RENDITION_KIND_UNKNOWN`。
    """
    item = media_repo.get_media_item(media_item_id)
    if item is None:
        raise MediaGenerationError(
            reason=REASON_MEDIA_ITEM_NOT_FOUND,
            message=f"媒体条目 {media_item_id} 不存在。",
        )

    version = media_repo.get_version(media_version_id)
    if version is None or version.media_item_id != media_item_id:
        raise MediaGenerationError(
            reason=REASON_MEDIA_VERSION_NOT_FOUND,
            message=(
                f"媒体版本 {media_version_id} 不存在，"
                f"或不属于媒体条目 {media_item_id}。"
                "（后一种 FK 管不住：版本表只保证『那一版存在』，"
                "『它属不属于这一格』要跨 `media_versions.media_item_id` 判 ——"
                "不判就能拿别的格的版本加工出挂在格子下的产物，"
                "两边详情页都显示且都不报错。）"
            ),
        )

    if kind not in RENDITION_KINDS:
        raise MediaGenerationError(
            reason=REASON_RENDITION_KIND_UNKNOWN,
            message=(
                f"未知的加工档位：{kind!r}（本包只消费 "
                f"{sorted(RENDITION_KINDS)} —— PREVIEW 是预览、"
                "DELIVERY 是正式交付资产，两档**分开存**："
                "『没有预览』与『预览失败』在列上必须可区分）。"
            ),
        )

    # 取号 + 插入同一个 savepoint：唯一约束兜住并发（与
    # `execute_media_generation` 里"取版本号 + 插入"同款）。
    with media_repo.savepoint():
        attempt_no = media_repo.next_attempt_no(
            media_version_id=media_version_id, kind=kind
        )
        row = media_repo.insert_rendition(
            media_version_id=media_version_id,
            kind=kind,
            attempt_no=attempt_no,
            state=RENDITION_STATE_INITIAL,
            note=note,
        )
    # 重新包成视图再返回：ORM 行绑定在 session 上，返回给 `api.py` 后
    # 再访问属性会抛 `DetachedInstanceError`（与 `MediaGenerationOutcome`
    # 的 `detail` 同款理由）。
    return _rendition_view(row)


# ---------------------------------------------------------------------------
# 执行（**本包唯一的生成入口**）
# ---------------------------------------------------------------------------


def execute_media_generation(
    media_repo: MediaRepository,
    *,
    content_id: uuid.UUID,
    media_item_id: uuid.UUID,
    generator: MediaGenerator,
    prompt_repo: Any = None,
    billing_repo: Any = None,
    gateway_repo: Any = None,
    file_repo: Any = None,
    storage: Any = None,
    version_repo: Any = None,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
    motion_generator: LiveMotionGenerator | None = None,
    live_group_max_bytes: int = LIVE_GROUP_MAX_BYTES_DEFAULT,
) -> MediaGenerationOutcome:
    """跑完一张图的一次生成。**本包唯一的生成入口**。

    `motion_generator` / `live_group_max_bytes`（WP28）只在 `request.media_form == LIVE`
    时使用；Live 请求却没有注入 `motion_generator` ⇒ fail-closed（在预占之前），
    **不**静默回落到某个缺省桩（装配缺陷要在装配处暴露，不在这里替它兜）。

    ## 事务与锁

    开头的 `get_media_item_for_update()` 取**行锁**（`SELECT ... FOR UPDATE`），
    同一张图的并发重做由此串行化 ⇒ `next_version_no` 取到的号与插入之间
    **不可能**出现第二次插入。这比"撞唯一约束后重取号"更强，
    因为重取号会让**幂等键里的版本号**与**实际写下的版本号**分叉
    （那正是 R5 失效的形态：重放查不到已有上下文行 ⇒ 同一次生成被买两次，
    且不报错）。

    但 `savepoint()` 仍包住"取号 + 插入"这一段—— 防"意外的唯一冲突把整个
    事务打成 aborted"，此时**不重取号**（理由同上）。

    ## 失败路径：释放**全部**未结算预占，然后置 `FAILED`，原样重抛

    释放失败**不掩盖**原异常（`except Exception` 后 `pass` 再重抛原异常）：
    两个都报的话，运维看到的是"释放失败"而**看不到真正的原因**
    （如"提示词没配"）。额度释放失败由WP25 的对账路径兜。

    ## 结算包在 savepoint 里

    结算失败**不回滚已落的内容**（图已经落库了），只置
    `usage_pending=True` 的语义由调用方承担 —— 本模块**把结算包在
    savepoint 里**，让"结算失败"不把"图已生成"这个事实一起回滚掉。
    """
    # --- 第1 步：取媒体行（行锁）----------------------------------------
    item = media_repo.get_media_item_for_update(media_item_id)
    if item is None:
        raise MediaGenerationError(
            reason=REASON_MEDIA_ITEM_NOT_FOUND,
            message=f"媒体条目 {media_item_id} 不存在。",
        )
    if item.content_item_id != content_id:
        # 两步骤鉴权的第二步：寻址到的图不属于这条内容。
        raise MediaGenerationError(
            reason=REASON_MEDIA_ITEM_NOT_FOUND,
            message=(
                f"媒体条目 {media_item_id} 不属于内容 {content_id}。"
                "（这与『不存在』报同一个码 —— 但对外都是 404："
                "不该让调用方靠错误码区分『没有』与『不是你的』。）"
            ),
        )

    content_project = media_repo.content_item_project(content_id)
    if content_project is None:
        raise MediaGenerationError(
            reason=REASON_CONTENT_NOT_FOUND,
            message=f"内容 {content_id} 不存在。",
        )

    # --- 第 2 步：解析 generation_request ------------------------------
    raw_request = item.generation_request
    if not raw_request:
        raise MediaGenerationError(
            reason=REASON_GENERATION_REQUEST_INVALID,
            message=(
                f"媒体条目 {media_item_id} 没有 generation_request。"
                "（这是执行阶段的入口，正常流程必经`api.py` 的入队；"
                "缺它说明这条任务是绕过端点进来的。）"
            ),
        )
    payload = validate_generation_request_payload(raw_request)
    request = MediaGenerationRequest.from_payload(payload)

    return _execute_locked(
        media_repo,
        item=item,
        content_id=content_id,
        content_project=content_project,
        request=request,
        generator=generator,
        prompt_repo=prompt_repo,
        billing_repo=billing_repo,
        gateway_repo=gateway_repo,
        file_repo=file_repo,
        storage=storage,
        version_repo=version_repo,
        actor_id=actor_id,
        request_id=request_id,
        motion_generator=motion_generator,
        live_group_max_bytes=live_group_max_bytes,
    )


def _execute_locked(
    media_repo: MediaRepository,
    *,
    item: Any,
    content_id: uuid.UUID,
    content_project: uuid.UUID,
    request: MediaGenerationRequest,
    generator: MediaGenerator,
    prompt_repo: Any,
    billing_repo: Any,
    gateway_repo: Any,
    file_repo: Any,
    storage: Any,
    version_repo: Any,
    actor_id: uuid.UUID | None,
    request_id: str | None,
    motion_generator: LiveMotionGenerator | None = None,
    live_group_max_bytes: int = LIVE_GROUP_MAX_BYTES_DEFAULT,
) -> MediaGenerationOutcome:
    """行锁已持有后的编排（第 3~7 步）。**私有**。

    拆出来是因为 `execute_media_generation` 的前半段（锁 + 载荷解析）
    与后半段（编排）职责不同：前半段是"这个任务能不能跑"，后半段是
    "跑"。合成一个函数会让 60 行的函数里混着两种关注点。
    """
    media_item_id = item.id
    project_id = content_project
    # ★ 形态的**唯一来源**是 `request.media_form`（铁律 26：同一个量只有一个取数点）。
    form = request.media_form
    if form == MEDIA_FORM_LIVE and motion_generator is None:
        raise MediaGenerationError(
            reason=REASON_GENERATOR_NOT_CONFIGURED,
            message=(
                "Live 生成未注入 motion_generator。本包 fail-closed：不预占、不派发"
                "（装配入口见 handlers.py::build_task_registry）。"
            ),
        )

    # --- 第 3 步：先解析提示词（R6，在任何预占之前）--------------------
    # ⚠️ 这一步**故意放在预占之前**：提示词没配是**部署问题**，
    # 而静态图按张计费 —— 先预占再发现没配 = 白占 3 张的额度再释放。
    # ★ `task_kind` 来自载荷（用户意图），不是本模块的判断。
    if prompt_repo is None:
        raise MediaGenerationError(
            reason=REASON_GENERATOR_NOT_CONFIGURED,
            message=(
                "未注入 prompt_repo，无法解析静态图提示词。"
                "本包 fail-closed：不预占、不派发。"
                "（装配入口见 handlers.py::build_task_registry。）"
            ),
        )
    prompt = _resolve_media_prompt(prompt_repo, task_kind=request.task_kind)

    # --- 第 4 步：解析 provider_id -------------------------------------
    if gateway_repo is None:
        raise MediaGenerationError(
            reason=REASON_GENERATOR_NOT_CONFIGURED,
            message=(
                "未注入 gateway_repo，无法解析模型档案的 provider_id。"
                "（没有 provider 就没有计价口径，而计价在预占之前。）"
            ),
        )
    provider_id = resolve_provider_id(
        gateway_repo, model_profile_id=request.model_profile_id
    )

    # --- 第 5 步：R4 段落 + R1 素材 -------------------------------------
    #
    # ★ **执行时按当时正文重算**（老大 2026-10-08 拍板）。
    #   这里读的正文是**本次事务**里的当前版本正文，不是入队那一刻的快照
    #   （`media_items.paragraph_ref`）—— 从入队到执行之间正文可能已改：
    #   读快照会让"该段被改写"变成 `PARAGRAPH_REF_UNKNOWN`（整批图失败、
    #   钱已花），或让"段落重排"变成**静默错位**（图画的是旧位置）。
    #   重算的唯一实现是 `schemas.paragraph_ref_for_sort_order`（它就落在
    #   `assign_paragraph_refs` 上，因此对位规则仍只有一处）。
    body = media_repo.content_body_for_paragraphs(content_id)
    if body is None:
        raise MediaGenerationError(
            reason=REASON_CONTENT_VERSION_NOT_FOUND,
            message=(
                f"内容 {content_id} 没有任何版本行（`content_versions` 为空）"
                "⇒ 没有正文可对齐。"
            ),
        )
    paragraphs = paragraph_refs_for_body(body)
    if not paragraphs:
        raise MediaGenerationError(
            reason=REASON_PARAGRAPH_REF_UNKNOWN,
            message=(
                f"内容 {content_id} 的正文切不出任何段落（正文为空或全是空白）。"
                "（这是真的事故，不是『跳过校验』的理由 —— "
                "跳过会让图照生成、只是没有段落坐标。）"
            ),
        )

    # 本次要生成/重做的那一张：由**本行的 `sort_order`** 在**当前正文**上
    # 按顺序对位重算（OP-199）。不是 `item.paragraph_ref` —— 那是快照。
    # `sort_order` 越界 ⇒ 抛 `SchemaViolation`（装配/数据缺陷，不猜）。
    target_ref = paragraph_ref_for_sort_order(
        paragraphs,
        int(item.sort_order),
        what=f"媒体条目 {media_item_id} 的段落对位",
    )
    # 仍然复核一次"重算出来的 ref 确实在当前段落集合里" —— 它是自洽的
    # （重算的输入就是这份集合），但**这是防"将来有人换掉重算实现"**：
    # 换成一个会拼错的实现时，这一步当场报 `PARAGRAPH_REF_UNKNOWN`，
    # 而不是让一个拼错的 ref 落进不可变版本行。
    validate_paragraph_ref(
        target_ref,
        known_paragraph_refs=frozenset(p[2] for p in paragraphs),
        what=f"媒体条目 {media_item_id} 的段落引用",
    )
    target_paragraph = next(p[1] for p in paragraphs if p[2] == target_ref)

    # ★ R1 的绑定从**投影的待生成列**读（`item.pending_reference_bindings`），
    #   不是从 `generation_request`（它只有三键）、也不是从
    #   `item.reference_bindings`（**MediaItem 没有这一列** ——
    #   `reference_bindings` 只存在于不可变的 `MediaVersion`，
    #   那是**生成结果**，不是本次输入）。
    #
    #   为什么必须从投影读：生成是**异步**的，端点入队后立刻返回，
    #   此刻 `media_versions` 一行都还没有 ⇒ 绑定只存版本行的话，
    #   处理器读到的会是"没有绑定"，而空集合**不报错**（R1 静默失效）。
    #   详见 `types.PENDING_BINDINGS_IS_ARRAY_SQL` 与迁移 `b7c1e4a9d203` §一。
    if item.pending_reference_bindings is None:
        raise MediaGenerationError(
            reason=REASON_REFERENCE_BINDING_MISSING,
            message=(
                f"媒体条目 {media_item_id} 没有待生成的参考素材绑定"
                "（`media_items.pending_reference_bindings` 为 NULL）。"
                "这与『明确传了空数组』是两种不同的事实："
                "前者是装配/调用方漏传，后者违反 R1『至少一条』。"
                "本包不猜默认绑定 —— 猜出来的素材会让 AQ-03 的对照关系失真。"
            ),
        )
    bindings = _claim_and_validate(
        media_repo,
        project_id=project_id,
        media_item_id=media_item_id,
        raw_bindings=item.pending_reference_bindings,
    )

    # --- 第 6 步：幂等 → 预估 → 预占→ 渲染 → 派发 -----------------------
    if billing_repo is None:
        raise MediaGenerationError(
            reason=REASON_GENERATOR_NOT_CONFIGURED,
            message=(
                "未注入 billing_repo，无法预估与预占。"
                "本包 fail-closed：不派发（先派发后补账 = 白花钱）。"
            ),
        )
    target_version_no = media_repo.next_version_no(media_item_id=media_item_id)
    current_lock_version = int(item.lock_version)

    prompt_text = render_system_prompt(
        prompt,
        values={
            PROMPT_VALUE_CONTENT_ID: str(content_id),
            PROMPT_VALUE_PROJECT_ID: str(project_id),
            PROMPT_VALUE_BATCH_KIND: request.batch_kind,
            PROMPT_VALUE_PARAGRAPH: target_paragraph,
            PROMPT_VALUE_REFERENCE_ASSETS: _bindings_prompt_text(bindings),
        },
    )

    # ★ 数量口径：一次生成 = **一张**图（`content_count=1`）⇒
    #   `calls_for` 折出 1 次调用。批量端点会为每张图各起一个媒体位
    #   与一个任务，因此这里**不是** `images_per_content()`。
    #   唯一定义在 `types.calls_for`，本行不写算式。
    estimate = estimate_generation_cost(
        billing_repo,
        project_id=project_id,
        task_kind=request.task_kind,
        content_count=1,
        batch_kind=request.batch_kind,
        provider_id=provider_id,
        model_profile_id=request.model_profile_id,
        actor_id=actor_id,
        request_id=request_id,
        # Live：一组预占 2 张（STILL + PREVIEW，X2a）；静态仍是 1 张。
        form=form,
    )
    # Live 的动态段成本没有计入（OP-206，归属 WP25）⇒ 账本 note 如实标注；
    # 预占与结算共用同一个 note，静态原样 None（零回归）。
    ledger_note = note_for_form(form)
    reservation = reserve_generation_budget(
        billing_repo,
        project_id=project_id,
        estimate=estimate,
        content_id=content_id,
        note=ledger_note,
        actor_id=actor_id,
        request_id=request_id,
    )

    # --- 第 6 步：派发（**只出网**，不落库）-----------------------------
    try:
        dispatch = generator.dispatch(
            media_item_id=media_item_id,
            content_id=content_id,
            project_id=project_id,
            task_kind=request.task_kind,
            model_profile_id=request.model_profile_id,
            prompt_version_id=prompt.version_id,
            prompt=prompt_text,
            target_version_no=target_version_no,
        )
    except Exception:
        _release_quietly(
            billing_repo,
            reservation.id,
            actor_id=actor_id,
            request_id=request_id,
        )
        media_repo.set_state(media_item_id=media_item_id, state=MEDIA_STATE_FAILED)
        raise

    # ★ 派发返回了但**没有字节** —— fail-closed。
    #   `gateway_bridge` 已判过一次（`REASON_GATEWAY_NO_IMAGE_REF`），这里是
    #   **第二道**：Protocol 只承诺"出网并取回字节"，而
    #   `runtime_checkable` 的 `isinstance` **不校验返回形状** ⇒
    #   "注入了一个不取字节的实现"在装配时是绿的，在这里才炸。
    #   不判的后果不是"崩"，而是 `image.content` 抛 `AttributeError`
    #   —— 装配缺陷伪装成业务失败（与 WP26 的 `AttributeError` 理由同款）。
    if dispatch.image is None:
        _release_quietly(
            billing_repo,
            reservation.id,
            actor_id=actor_id,
            request_id=request_id,
        )
        media_repo.set_state(media_item_id=media_item_id, state=MEDIA_STATE_FAILED)
        raise GatewayUnavailable(
            reason=REASON_GATEWAY_NO_IMAGE_REF,
            message=(
                f"生成器 {type(dispatch).__name__} 返回了调用记录 "
                f"{dispatch.call_id} 但没有图片字节。"
                "（`ImageByteSource` 未注入，或 `fetch` 返回了 None —— "
                "OP-195 未关闭。）本包 fail-closed：不落版本、不推进投影。"
            ),
            state=dispatch.state,
        )

    # --- 第 6.5 步（仅 Live）：动态段出网 ------------------------------
    # 与静帧派发同一处置：失败 ⇒ 释放预占、置 FAILED、原样重抛。
    motion: MotionOutput | None = None
    if form == MEDIA_FORM_LIVE:
        assert motion_generator is not None  # noqa: S101 - 上面已 fail-closed
        try:
            motion = motion_generator.dispatch_motion(
                still_content=dispatch.image.content,
                duration_ms=DEFAULT_MOTION_DURATION_MS,
            )
        except Exception:
            _release_quietly(
                billing_repo,
                reservation.id,
                actor_id=actor_id,
                request_id=request_id,
            )
            media_repo.set_state(
                media_item_id=media_item_id, state=MEDIA_STATE_FAILED
            )
            raise

    # --- 第 7 步：R2 校验 → 落库 → 结算 --------------------------------
    # ★ 落库链的顺序**不可调换**（理由见 `_persist_generation` 的 docstring）。
    #   整段包 savepoint：**图已经付过费生成出来了**，落库失败不能把
    #   "已生成"这个事实一起回滚掉 —— 代价是失败时留下孤儿 blob
    #   （无引用、但内容寻址保证下次同内容复用，不构成泄漏，
    #   与 `files.store_generated_asset` 的"先写物理后建行"同款理由）。
    usage_pending = False
    try:
        with media_repo.savepoint():
            outcome = _persist_generation(
                media_repo,
                dispatch=dispatch,
                project_id=project_id,
                content_id=content_id,
                item=item,
                request=request,
                prompt=prompt,
                prompt_text=prompt_text,
                bindings=bindings,
                target_version_no=target_version_no,
                # ★ 执行期重算出来的段落坐标（不是 `item.paragraph_ref` 快照）。
                #   落进不可变版本行 + 推进投影，两者**必须是同一个值** ——
                #   分叉的表现是"版本行记着 A 段、投影指向 B 段"，
                #   而列表页与详情页会给出互相矛盾的答案且都不报错。
                target_paragraph_ref=target_ref,
                expected_lock_version=current_lock_version,
                provider_call_id=dispatch.call_id,
                file_repo=file_repo,
                storage=storage,
                version_repo=version_repo,
                actor_id=actor_id,
                motion=motion,
                live_group_max_bytes=live_group_max_bytes,
            )
    except Exception:
        media_repo.set_state(media_item_id=media_item_id, state=MEDIA_STATE_FAILED)
        _release_quietly(
            billing_repo,
            reservation.id,
            actor_id=actor_id,
            request_id=request_id,
        )
        raise

    # --- 第 7 步末：结算 -----------------------------------------------
    # 包 savepoint：结算失败**不回滚已落的图**（图已经生成出来了）。
    try:
        with media_repo.savepoint():
            settle_generation_budget(
                billing_repo,
                reservation_id=reservation.id,
                actual_amount=estimate.total_amount,
                note=ledger_note,
                actor_id=actor_id,
                request_id=request_id,
            )
    except Exception:
        # 结算失败**不掩盖**已成功的结果：图已落库是事实。
        # 额度差异由 WP25 的对账路径处理（承 `billing_bridge` 的口径：
        # 本机没有可信的实际成本来源，成功路径按预估结算）。
        usage_pending = True

    if usage_pending:
        # ★ 用 `dataclasses.replace` 而不是 `object.__setattr__`：
        #   frozen dataclass 的"可变性"只该通过重建表达，且 outcome 是
        #   **返回值**（已被 `_persist_generation` 的调用方之外持有）。
        outcome = replace(outcome, usage_pending=True)
    return outcome


def _persist_generation(
    media_repo: MediaRepository,
    *,
    dispatch: MediaGenerationOutput,
    project_id: uuid.UUID,
    content_id: uuid.UUID,
    item: Any,
    request: MediaGenerationRequest,
    prompt: Any,
    prompt_text: str,
    bindings: Sequence[MediaReferenceBinding],
    target_version_no: int,
    target_paragraph_ref: str,
    expected_lock_version: int,
    provider_call_id: uuid.UUID,
    file_repo: Any,
    storage: Any,
    version_repo: Any,
    actor_id: uuid.UUID | None,
    motion: MotionOutput | None = None,
    live_group_max_bytes: int = LIVE_GROUP_MAX_BYTES_DEFAULT,
) -> MediaGenerationOutcome:
    """第 7 步的落库链。**顺序不可调换** —— 下面每一对相邻步都有一条硬约束。

    | # | 做什么 | 为什么不能挪|
    |---|---|---|
    | ① | `measure_image(bytes)` —— R2 四道校验 | 坏字节进 `file_objects` 之后再发现，就已经在**不可变**的 blob 上了（`objects` 换不掉） |
    | ② | `files.store_generated_asset` → `image_file_id` | 必须**先有文件 id**才能填`media_versions.image_file_id`（NOT NULL FK），而它的入参是**字节** |
    | ③ | 预生成 `context_id` + `register_version` → `version_id` | 版本行的 `generation_context_id` **只能在 INSERT 时给出**（不可变表 + DEFERRABLE 环）⇒ `context_id` 必须先于③ 定好；而 `version_id` 由 `register_version` 现取，`insert_version` 要用 |
    | ④ | `insert_version` | 必须先于 ⑤：`advance_projection` 要用 `version_id`；且 ④ 与 ⑤ 的 `paragraph_ref` 必须传**同一个变量**（`target_paragraph_ref`），否则版本行与投影指向不同段 |
    | ⑤ | `advance_projection` → 清空 `pending_reference_bindings` + `pending_regeneration_reason` | 投影推进用**条件 UPDATE**（`lock_version`）⇒ 要在改任何其他列**之前**做，否则自己的写入会让条件失配（自锁）。两个 pending 列**不**推进 `lock_version`，所以放在它之后仍安全 |
    | ⑥ | `insert_generation_context(context_id=同一个)` | 环的另一侧。最后插 ⇒ DEFERRABLE 期一起结算 |
    | ⑦ | 重读版本行 → `MediaVersionSummary` | 返回的是**不可变值对象**，不是绑在 session 上的 ORM 行（`DetachedInstanceError`） |

    ## 为什么② 之前不做 R2

    `store_generated_asset` 是**物理写**（先写暂存 key再 move 到内容寻址的
    最终 key）。它不抛"这不是图片"，但它**已经占了一个 blob**。因此顺序
    必须是"先验字节、再落物理"。

    ⚠️ 本函数**不判 `dispatch.image is None`** —— 判在 `_execute_locked`
    的派发正后方（第 6 步）。**同一件事只判一次**，判两处的后果是
    "两处错误码不同 ⇒ 排障时不知道看哪个"。

    ## 为什么⑤ 里把"清空待生成绑定"与"推进投影"放在一起

    两者都改`media_items`，而 `advance_projection` 用**条件 UPDATE**判
    `lock_version`。若先清 pending（它**不**推进 `lock_version`）再推进
    投影，条件仍成立 —— 这正是它不推进 `lock_version` 的原因
    （见 `repository.set_pending_reference_bindings` docstring）。
    放在同一个 savepoint 里是为了"投影推进成功但清空失败"不留半状态。

    ## ★ `target_paragraph_ref` 是一个**入参**（不是一个读点）

    本函数**不读** `item.paragraph_ref` 去落版本行与推进投影。理由：
    `item` 是**入队那一刻**的行快照，而生成是异步的 —— 从入队到执行之间
    正文可能已改（老大 2026-10-08 拍板「执行时按当时正文重算」）。
    权威值由 `_execute_locked` 第 5 步在**本次事务**里按当前正文重算，
    经入参传进来 ⇒ ④ 与 ⑤ 用的是**同一个值**（同一个变量），
    不可能分叉（分叉的表现：版本行记 A 段、投影指向 B 段，两侧都不报错）。
    """
    # 本地import（R7：`files` 与 `versions` 都是重模块）。
    from rednote.modules.files.service import store_generated_asset
    from rednote.modules.versions.service import register_version

    # --- ① R2：四道图片校验（**唯一的像素测量点**）---------------------
    if file_repo is None or storage is None:
        raise MediaGenerationError(
            reason=REASON_GENERATOR_NOT_CONFIGURED,
            message=(
                "未注入 file_repo / storage，无法把生成图落成正式资产。"
                "本包 fail-closed：不落悬空的 image_file_id。"
                "（装配入口见 handlers.py::build_task_registry。）"
            ),
        )
    if version_repo is None:
        raise MediaGenerationError(
            reason=REASON_GENERATOR_NOT_CONFIGURED,
            message=(
                "未注入 version_repo，无法注册媒体版本节点。"
                "（`media_versions.id` 必须等于 `version_nodes.node_id`，"
                "这是 OP-88 的正向一半。）"
            ),
        )

    assert dispatch.image is not None  # noqa: S101 - 由调用方（第 6 步）判过
    image = dispatch.image
    measurement = measure_image(image.content)

    # --- ② 生成资产落 `blob` + `file_object` ----------------------------
    # `uploaded_by` 是**必填**（`store_generated_asset` 的签名）⇒
    # 本包在 actor 缺失时**不猜**：派生的图没有真实上传者，
    # 而 `files` 把它NOT NULL 化正是为了让"谁的资产"永远有答案。
    # 任务处理器侧的 actor 来自 `enqueue_job` 的入参（`api.py` 必填）。
    if actor_id is None:
        raise MediaGenerationError(
            reason=REASON_GENERATOR_NOT_CONFIGURED,
            message=(
                "生成静态图必须带actor_id（`file_objects.uploaded_by` 非空）。"
                "本包 fail-closed：不用系统用户冒名顶替 —— "
                "那样'这张图是谁的'就再也答不出来。"
            ),
        )
    stored = store_generated_asset(
        file_repo,
        storage,
        project_id=project_id,
        original_name=f"media-{item.id}-v{target_version_no}.{_EXT_BY_FORMAT[measurement.image_format]}",
        content=image.content,
        mime_type=_MIME_BY_FORMAT[measurement.image_format],
        uploaded_by=actor_id,
    )

    # --- ③ 注册版本节点（环 2 的两个 id 一起定）------------------------
    context_id = uuid.uuid4()
    payload: dict[str, Any] = {
        "width": measurement.width,
        "height": measurement.height,
        "byteSize": measurement.byte_size,
        "imageFormat": measurement.image_format,
        "referenceAssetIds": [str(b.asset_id) for b in bindings],
        "providerCallId": str(provider_call_id),
    }
    registered = register_version(
        version_repo,
        entity_type=ENTITY_TYPE_MEDIA,
        entity_id=item.id,
        project_id=project_id,
        payload=payload,
        created_by=actor_id,
        schema_version=MEDIA_SCHEMA_VERSION,
    )
    version_id = registered.node_id

    # --- ④ 版本行（不可变 ⇒ INSERT 时给全）-----------------------------
    media_repo.insert_version(
        version_id=version_id,
        media_item_id=item.id,
        version_no=target_version_no,
        # ★ 用执行期重算出来的 `target_paragraph_ref`，**不是** `item.paragraph_ref`。
        #   两者在"入队后正文改过"时会分叉，而这一列是不可变的历史事实
        #   （"这一版画的是哪一段"）⇒ 写快照就是写下一个**错的**历史。
        #   重算的唯一实现是 `schemas.paragraph_ref_for_sort_order`。
        paragraph_ref=target_paragraph_ref,
        sort_order=item.sort_order,
        # ★ R1 的落点：绑定在这里**变成不可变事实**。
        #   之后清空投影上的 pending 列（⑤）不会丢它—— 版本行才是归宿。
        reference_bindings=[b.as_payload() for b in bindings],
        image_file_id=stored.file_id,
        prompt=prompt_text,
        payload=payload,
        # 用 `register_version` 算出的摘要，**不自己再算一次**
        # （两次算不一致 = 同一份内容两个指纹，对账时分叉）。
        payload_hash=registered.payload_hash,
        regeneration_reason=item_pending_regeneration_reason(item),
        generation_context_id=context_id,
        created_by=actor_id,
        media_form=request.media_form,
    )

    # --- ④b（仅 Live）封装四个组件 -------------------------------------
    # 挂在 ④ 之后：`media_components.media_version_id` FK **不延迟**，组件行必须晚于版本行。
    # 任一环节失败 ⇒ 抛异常，由 `_execute_locked` 的 savepoint 整体回滚（不写半组）。
    components = _pack_live_components(
        media_repo,
        request=request,
        motion=motion,
        still_content=image.content,
        still_file_id=stored.file_id,
        version_id=version_id,
        item=item,
        target_version_no=target_version_no,
        project_id=project_id,
        file_repo=file_repo,
        storage=storage,
        actor_id=actor_id,
        live_group_max_bytes=live_group_max_bytes,
    )

    # --- ⑤ 投影推进 + 清空待生成绑定 -----------------------------------
    affected = media_repo.advance_projection(
        media_item_id=item.id,
        expected_lock_version=expected_lock_version,
        version_id=version_id,
        version_no=target_version_no,
        # ★ 与 ④ **同一个值**（同一个变量）—— 见 ④ 的注释：
        #   两处各写一次的表现是"版本行记 A 段、投影指向 B 段"。
        paragraph_ref=target_paragraph_ref,
        sort_order=item.sort_order,
        state=MEDIA_STATE_READY,
    )
    if affected == 0:
        # 持行锁（第 1 步）的前提下不该发生 —— 那意味着投影被本事务之外改写。
        # 抛而不是忽略：忽略会让投影落后于实际版本，
        # 而列表页显示旧版本号**不报错**。
        raise MediaGenerationError(
            reason=REASON_PROJECTION_CONFLICT,
            message=(
                f"媒体条目 {item.id} 的投影推进失败"
                f"（期望 lock_version={expected_lock_version}）。"
                "持行锁的前提下这意味着投影被本事务之外改写。"
            ),
        )
    # 生成成功 ⇒ 本次输入的绑定已进不可变版本行，投影上的暂存位清空。
    # **不是第二个真值来源**：清空后只有版本行持有绑定。
    media_repo.set_pending_reference_bindings(media_item_id=item.id, bindings=None)
    # ★ 重做理由**同样清空**（与绑定逐字同一条理由）：
    #   理由已进不可变版本行（④），投影上的暂存就过期了。
    #   不清空的话，下一次"首次生成"（若同一行先失败后重来）
    #   会把上一轮的重做理由**误报**成这一轮的理由 —— 留痕失真，
    #   且与 `media_versions.regeneration_reason` 的口径分叉。
    #   本方法同样不推进 `lock_version`（在 `advance_projection` 之后调用，
    #   条件 UPDATE 已执行完毕，不会自锁）。
    media_repo.set_pending_regeneration_reason(
        media_item_id=item.id, reason=None
    )

    # --- ⑥ 生成上下文（环 2 的另一侧）---------------------------------
    media_repo.insert_generation_context(
        project_id=project_id,
        content_item_id=content_id,
        media_version_id=version_id,
        task_kind=request.task_kind,
        # ★ 与模型调用层**同一个**派生点（`gateway_bridge` 是唯一定义处）。
        #   自己再拼一遍就是第二个格式来源 ⇒ 幂等查不到 ⇒ 重复建上下文行。
        idempotency_key=media_generation_idempotency_key(
            media_item_id=item.id,
            target_version_no=target_version_no,
            task_kind=request.task_kind,
        ),
        context_id=context_id,
        prompt_template_id=prompt.template_id,
        prompt_version_id=prompt.version_id,
        model_profile_id=request.model_profile_id,
        provider_call_id=provider_call_id,
        created_by=actor_id,
    )

    # --- ⑦ 重读版本行 → 不可变值对象 ----------------------------------
    stored_version = media_repo.get_version(version_id)
    if stored_version is None:
        # 刚INSERT 的一行读不到 = 装配/事务层异常，不是业务分支。
        raise MediaGenerationError(
            reason=REASON_MEDIA_ITEM_NOT_FOUND,
            message=(
                f"媒体版本 {version_id} 在插入后读不到"
                "（事务层异常 —— 刚写入的行不应消失）。"
            ),
        )

    return MediaGenerationOutcome(
        media_item_id=item.id,
        version_id=version_id,
        version_no=target_version_no,
        paragraph_ref=stored_version.paragraph_ref,
        image_file_id=stored_version.image_file_id,
        provider_call_id=provider_call_id,
        generation_context_id=context_id,
        state=MEDIA_STATE_READY,
        detail=MediaVersionSummary(
            id=stored_version.id,
            version_no=stored_version.version_no,
            paragraph_ref=stored_version.paragraph_ref,
            sort_order=stored_version.sort_order,
            image_file_id=stored_version.image_file_id,
            generation_context_id=stored_version.generation_context_id,
            payload_hash=stored_version.payload_hash,
            schema_version=stored_version.schema_version,
            created_at=stored_version.created_at,
            created_by=stored_version.created_by,
            media_form=stored_version.media_form,
        ),
        media_form=stored_version.media_form,
        components=components,
    )


class _ServiceComponentSink:
    """`ComponentSink` 的落库实现：把 Live 组件字节落成 `file_objects`。

    ★ STILL 角色**不再存一次**：② 已经把静帧存成了 `image_file_id`，而
    `store_generated_asset` 每次调用都会新建一行 `file_objects`（同字节只共用 blob）。
    若 STILL 再存一次，组件的 `file_id` 就会与版本的 `image_file_id` 不同 ⇒
    触发 S2 一致性触发器。所以 STILL 直接返回 ② 的 id —— 同一个量只有一个来源。
    """

    def __init__(
        self,
        *,
        file_repo: Any,
        storage: Any,
        project_id: uuid.UUID,
        actor_id: uuid.UUID,
        still_file_id: uuid.UUID,
    ) -> None:
        self._file_repo = file_repo
        self._storage = storage
        self._project_id = project_id
        self._actor_id = actor_id
        self._still_file_id = still_file_id

    def store(
        self, *, role: str, name: str, content: bytes, mime_type: str
    ) -> uuid.UUID:
        if role == COMPONENT_ROLE_STILL:
            return self._still_file_id
        from rednote.modules.files.service import store_generated_asset

        stored = store_generated_asset(
            self._file_repo,
            self._storage,
            project_id=self._project_id,
            original_name=name,
            content=content,
            mime_type=mime_type,
            uploaded_by=self._actor_id,
        )
        return stored.file_id


def _pack_live_components(
    media_repo: MediaRepository,
    *,
    request: MediaGenerationRequest,
    motion: MotionOutput | None,
    still_content: bytes,
    still_file_id: uuid.UUID,
    version_id: uuid.UUID,
    item: Any,
    target_version_no: int,
    project_id: uuid.UUID,
    file_repo: Any,
    storage: Any,
    actor_id: uuid.UUID | None,
    live_group_max_bytes: int,
) -> tuple[ComponentView, ...]:
    """Live 版本：封装 → 逐个插入 `media_components`；静态版本直接返回空。

    封装失败（体积超限 / 缺 ffmpeg / 半组落盘失败）⇒ 抛 `MediaGenerationError`，
    `reason` 取封装结果的错误码（`GROUP_TOO_LARGE` 等），由调用方的 savepoint 整体回滚。
    """
    if request.media_form != MEDIA_FORM_LIVE:
        return ()
    if motion is None or actor_id is None:
        # 调用方（`_execute_locked`）已保证；这里是防"将来有人绕过它"。
        raise MediaGenerationError(
            reason=REASON_GENERATOR_NOT_CONFIGURED,
            message="Live 落库缺少动态段或 actor_id（装配缺陷）。",
        )
    sink = _ServiceComponentSink(
        file_repo=file_repo,
        storage=storage,
        project_id=project_id,
        actor_id=actor_id,
        still_file_id=still_file_id,
    )
    result = LivePackStub(sink=sink, max_group_bytes=live_group_max_bytes).pack(
        LivePackRequest(
            request_id=f"{item.id}-v{target_version_no}",
            job_id=str(item.id),
            still_content=still_content,
            motion_content=motion.content,
            live_profile_key=LIVE_PROFILE_KEY_DEMO,
        )
    )
    if result.state != STATE_SUCCEEDED:
        raise MediaGenerationError(
            reason=result.error_code or REASON_GENERATOR_NOT_CONFIGURED,
            message=result.error_message or "Live 封装失败。",
        )
    views: list[ComponentView] = []
    for comp in result.components:
        row = media_repo.insert_component(
            media_version_id=version_id,
            role=comp.role,
            file_id=comp.file_id,
            verification=comp.verification,
            sha256=comp.sha256,
            byte_size=comp.byte_size,
        )
        views.append(_component_view(row))
    return tuple(views)


def item_pending_regeneration_reason(item: Any) -> str | None:
    """读"这一次重做为什么重做"，写进不可变版本行的 `regeneration_reason`。

    ★ **缺口已闭合**（迁移 `c4d9f2a17e35` §二）：本函数**曾经恒返回
    `None`** —— 那时 `media_items` 没有暂存位，而 `regeneration_reason`
    只在不可变的 `media_versions` 上，理由是"端点收下 → 异步任务才知道"
    的跨界数据。恒 `None` 的后果是**每一次重做的版本行 `regeneration_reason`
    都是 NULL**，而那一列存在的唯一理由就是记录"第 2 版与第 1 版为什么
    不一样"（`models.py` docstring 十）—— 列在、值恒空、且不报错。

    现在读投影上的暂存列 `media_items.pending_regeneration_reason`，
    与 R1 的 `pending_reference_bindings` **完全同一条链路**
    （同一迁移族、同一事务边界、同一"异步所以需要暂存"的理由）。

    **取 `None` 是常态**：首次生成没有理由可言 ——
    `media_versions.regeneration_reason` 同一取值口径（首次的行恒 NULL）。
    因此本函数**不区分**"首次生成"与"重做但没写理由"：
    两者在列上同形（都是 NULL），那是刻意的
    （见 `types.PENDING_REGENERATION_REASON_SQL` 的"不落非空 CHECK"）。

    **为什么用 `getattr` 兜底**：入参在类型上是 `Any`（`_persist_generation`
    要能接受测试里的轻量替身），而本函数的失败方式若是 `AttributeError`，
    装配缺陷就伪装成了业务失败（WP26 的同款教训）。缺列 ⇒ 返回 `None`，
    即"没有理由"这个**合法**语义。
    """
    return getattr(item, "pending_regeneration_reason", None)


def _release_quietly(
    billing_repo: Any,
    reservation_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None,
    request_id: str | None,
) -> None:
    """释放预占，**释放失败不掩盖原异常**。

    理由：`execute_*` 的 `except` 里若让释放异常穿出去，运维看到的是
    "释放失败"而**看不到真正的原因**（如"提示词没配"）。
    两个都报的话，真因被吞掉。
    """
    try:
        release_generation_budget(
            billing_repo,
            reservation_id=reservation_id,
            actor_id=actor_id,
            request_id=request_id,
        )
    except Exception:
        pass


def _claim_and_validate(
    media_repo: MediaRepository,
    *,
    project_id: uuid.UUID,
    media_item_id: uuid.UUID,
    raw_bindings: Any,
) -> tuple[MediaReferenceBinding, ...]:
    """R1：**两段式** —— 先抽 id 列表，再一次读取三个集合，最后整体校验。

    ## 为什么必须两段式（这是 R1 落地的真正难点）

    `schemas.validate_reference_bindings` 需要**三个上游事实**（`existing_ids` /
    `foreign_ids` / `usable_ids`），而这三个集合只有在 `repository.claim_assets`
    一次读完才能保证自洽。但**它的入参里就有 `raw` 绑定** ⇒ 出现一个**先有鸡还是先有蛋**
    的循环：要拿三个集合就得先有 id 列表，而要拿 id 列表得先解析 `raw`。

    破法是把它拆成两段，**且两段的判据不重叠**：

    | 段 | 做| 不做 |
    |---|---|---|
    | 段 1 | `MediaReferenceBinding.from_payload`逐条解析**形状**（键齐不多 / UUID 合法 / `part`·`note` 是 str 或 null / 长度上界） | **不判存在性、不判归属、不判可用性** |
    | 段 2 | 用段 1 抽出的 id 列表一次 `claim_assets`，再整体过 `validate_reference_bindings`（判存在 / 先判跨项目 / 判可用 / 判重复 / 判数量） | **不再解析形状** |

    **为什么段 1 不用 `schemas._parse_binding`**：它是**私有**的
    （单下划线，且它的存在意义是"把 `from_payload` 的 `ValueError` 翻成
    `SchemaViolation`"）。段 1 若调它，等于在 `service` 里用一个"为别的调用方
    准备"的私有函数——而它抛的 `SchemaViolation` 在段 1 这个阶段还没法判
    归属（我们还不知道素材存不存在）。段 1 只做"形状对不对"，用
    `from_payload` 的 `ValueError` **正合适**：它本来就是 fail-closed 的
    形状校验器。
    ⇒ `SchemaViolation` **只在段 2 产生**（那时才有 `field` / `reason` 可判），
    而段 1 的形状错会穿成 `ValueError` —— 与 `types.MediaGenerationRequest`
    的处理一致（handler 侧折成任务失败，api 侧折成 422）。

    ★ **`from_payload` 逐条解析而不是"先取所有 assetId 再解析"**：因为
    "键不齐"与"id 不合法"是**两条不同的错误消息**，逐条解析能指出是哪一条
    （带序号）；一次性抽取会把它们都压成"第 N 条解析失败"。

    ★ **一次读是硬要求**（`schemas` 模块 docstring 已论证）：分三次读会在一个
    "中途变过的素材库"上判合法性 —— 判 `usable` 之后、插入之前素材被停用，
    就会落一条绑着已停用素材的**不可变**版本行（不可删除，只能追加）。
    """
    # --- 段 1：纯形状解析（不判存在性）--------------------------------
    #
    # ★ 委托 `_parse_bindings_shape`（形状解析的**唯一实现**，另一个调用点是
    #   `start_media_generation`）。这里**不保留第二份**逐条解析循环 ——
    #   留一份就是同一件事两个说法，而分叉的表现是"端点收了、处理器炸了"。
    #   `what` 的措辞保持原样（含"的参考素材绑定"这个后缀），
    #   以免错误消息在两个调用点之间出现无意义的差异。
    provisional = _parse_bindings_shape(
        raw_bindings, what=f"媒体条目 {media_item_id} 的参考素材绑定"
    )

    asset_ids = binding_asset_ids(provisional)

    # --- 段 2：一次读拿三个集合，再整体校验 ------------------------------
    existing_ids, foreign_ids, usable_ids = media_repo.claim_assets(
        project_id=project_id, asset_ids=asset_ids
    )
    return validate_reference_bindings(
        raw_bindings,
        existing_ids=existing_ids,
        foreign_ids=foreign_ids,
        usable_ids=usable_ids,
        what=f"媒体条目 {media_item_id} 的参考素材绑定",
    )


def _resolve_media_prompt(
    prompt_repo: Any, *, task_kind: str
) -> Any:
    """第 3 步：解析提示词（**在任何预占之前**）。

    `task_kind` 由调用方传（而不是这里读常量）—— 因为它来自
    `generation_request`（用户的意图），不是本模块的判断。

    ★ **为什么直接调 `prompts_bridge` 而不是 `generator.resolve_prompt`**：
    照WP26 的 `content/service.py` 同款切分——`ContentGenerator` 只声明
    "出网"一项能力，预占/结算/提示词解析都由 service **直调 bridge**。
    把它们塞进 Protocol 的后果是每个调用点都要能拿到全部依赖，
    而"忘注入某一个"的表现是 `AttributeError`（装配故障伪装成业务失败）。
    """
    return resolve_active_version(prompt_repo, task_kind=task_kind)


def _bindings_prompt_text(bindings: Sequence[MediaReferenceBinding]) -> str:
    """把绑定渲染成提示词里的一个文本块。**本模块唯一的渲染点**。

    用 `json.dumps(sort_keys=True)` 而不是手拼字符串：同样的绑定集合
    必须序列化成同样的字符串，否则"同样的输入"会产出不同的
    `context_digest`（网关用它做留痕摘要），而摘要不同意味着幂等命中
    判不出来。
    """
    import json

    return json.dumps(
        [b.as_payload() for b in bindings],
        ensure_ascii=False,
        sort_keys=True,
    )


TASK_STATIC_IMAGE_PLACEHOLDER: Final[str] = "STATIC_IMAGE"


__all__ = [
    "MIN_IMAGE_SIDE",
    "SUPPORTED_IMAGE_FORMATS",
    "AssetInput",
    "GatewayMediaGenerator",
    "ImageMeasurement",
    "MediaGenerationError",
    "MediaGenerationOutcome",
    "MediaGenerator",
    "UnavailableGenerator",
    "execute_media_generation",
    "item_pending_regeneration_reason",
    "list_media",
    "measure_image",
    "paragraph_refs_for_body",
    "read_media_detail",
    "register_reference_asset",
    "resolve_provider_id",
    "start_media_generation",
    "start_regeneration",
    "start_rendition",
]