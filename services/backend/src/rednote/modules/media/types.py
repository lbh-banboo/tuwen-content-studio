"""WP27 的不可变值类型与受控取值域。

**为什么单独一个模块**（与 WP11/WP16/WP20/WP23/WP24/WP26 同一手法）：这些取值同时被
`models.py`（CHECK 约束）、`schemas.py`（纯函数校验）、`repository.py`（存储）、
`billing_bridge.py` / `gateway_bridge.py`（接缝）与 `service.py`（编排）使用。
定义在 service 里会让其它几个反向导入 service —— 而本模块的分层刻意是单向的：

    types ← schemas / models / repository / bridges / service

**跨层传递的值用 frozen dataclass 而不是 ORM 对象或裸 dict**（ORM 实例绑在
session 上，session 一关就 `DetachedInstanceError`；裸 dict 则没有取值域，
写错键名不报错）。

---

## 本模块承载的四条硬性质

| 性质 | 本模块的落点 |
|---|---|
| **媒体张数口径唯一**（R3） | `images_for()` / `images_per_content()` —— **全包唯一的算法与唯一的读点**。两处各写一遍会算出两个预占数，而账面永远对不上 |
| **取值必须受控**（R1） | `MEDIA_STATES` / `RENDITION_KINDS` / `ASSET_STATES` —— 未知取值**拒绝**而不是放行 |
| **段落绑定是显式事实**（R4） | `MediaSummary.paragraph_ref` —— **不是数组下标**（见 `paragraph_ref` 的说明） |
| **出网数据类别最小化**（R10） | `EGRESS_DATA_CLASSES` —— 只含 `STATIC_IMAGE`，且是**受控元组**（不是"调用方随便传"） |

## ★ R3：为什么"每篇配几张图"必须只有一个来源

预占金额 = 张数 × 每张单价。这个"张数"若有两个来源（生成入口算一次、
重做入口再算一次），两者必然分叉，而分叉的表现是**钱**，不是报错。

分叉的典型演化：一处算"内容条数 × 3"、另一处算"内容条数"，
于是重做只预占三分之一的钱 —— 账面看起来正常，直到对账。

因此本模块只留**一个**算法（`images_for`）与**一个**读点
（`images_per_content`），且 `images_for` **不接受** `images_per_content`
入参 —— 若它是入参，调用方就能绕过读点自己传一个值，
而"传少了"不会报错，只会在对账时表现为"账面永远差一笔"。
把口径压成"只能从这里来"，就让算错变成**不可能**，而不是"不推荐"。

## ★★ 老大的拍板：`IMAGES_PER_CONTENT = 3`（固定值）—— 与原型的一处已知偏差

拍板原话：**「"每篇配几张图"从哪来：固定 3 张」**（2026-10-07）。

因此 `images_per_content()` **返回常量**，既不走 WP24 的 `form_items`
（说明文档 §3.6 A1）、也不走 `runtime_settings`（A2）、也不走请求参数（A3）。

**这是一处与原型不一致的显式偏差**，不是疏漏：

| 来源 | 说的张数 |
|---|---|
| `trial-config.html:296`「静态图数量」下拉 | **6 / 4 / 9 张**（可配置） |
| `trial-config.html:271`「每篇媒体数量**独立配置**」 | 每篇可不同 |
| `content-feedback.html:140`「文案 + 静态图 · **6 张**」 | 6 张 |
| **本包实现** | **固定 3 张** |

⇒ 该偏差**已登记为待定项**（说明文档 §2.4 的 OP-198），并**保留四条路径的
切换点**：`images_per_content()` 是唯一读点，将来若要改成可配置，
改**这一个函数**即可，`images_for` / `billing_bridge` / `service` 都不用动
—— 这正是"唯一读点"这个设计的收益。

**为什么不静默照做**：原型上的可配置下拉是**产品交互**，而"固定值"是
**当前实现**。两者不一致而不写下来，下一次有人看到原型会以为系统坏了
（本仓已多次记录"声称有防线而实际没有"的形态，反向的"声称有功能而实际是常量"
同样危险）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

# ---------------------------------------------------------------------------
# 本包消费的 task_kind（受控取值域）
# ---------------------------------------------------------------------------
# **为什么不从 `prompts.types` 顶层 import**：与 WP26 的 `TASK_CONTENT_*`
# 同一理由（`content/types.py` 的注释已论证）—— 本模块的这个常量是
# **本包对外承诺的取值**，而 `prompts.types` 的取值域是**提示词模块**的。
# 两者当前同值，但它们**允许不同步**。若此处直接用提示词侧的常量，
# 一次提示词侧的取值域扩张会让本包的判断**静默多接受一个 kind**。
#
# 因此这里写**本包自己的常量**。
#
# ⛔ **已知缺口（2026-10-07 实测更正）**：本处原先写"由 test_media_wp27.py
# 里的 `test_task_kind_constants_match_prompts` 断言两者当前同值"，
# 而那个文件**全仓不存在**（本包在 `services/backend/tests/` 下**零覆盖**：
# 没有任何测试 import 过 `rednote.modules.media`）。同名的
# `test_content_wp26.py::test_task_kind_constants_match_prompts` 守的是
# **content ↔ prompts**，与本包无关 —— 本段注释是从
# `content/types.py:76` 照抄的，照抄时把 `wp26` 改成了 `wp27`，而测试文件
# 从未落地。⇒ "两者当前同值"这句话**没有任何机械判据**，靠人读过。
# 这正是 ISS-95/169 的形态（声称有防线、入口打不开）。此处如实写下，
# **不**改指一个不存在的文件。
TASK_STATIC_IMAGE: Final[str] = "STATIC_IMAGE"

#: 本包消费的 task_kind（`calls_for` / `images_for` 的受控入参域）。
TASK_KINDS: Final[frozenset[str]] = frozenset({TASK_STATIC_IMAGE})


# ---------------------------------------------------------------------------
# 网关的能力与数据类别（R10：出网最小化）
# ---------------------------------------------------------------------------
# **同样在本包定义自己的常量**（理由同上：本包承诺的取值 ≠ 网关模块的取值）。
# 网关侧的位置（开工时实测）：`gateway/types.py:58` `CAPABILITY_STATIC_IMAGE`
# 与 `:84` `DATA_CLASS_STATIC_IMAGE`。
# ⛔ 原文紧接着写"并由测试断言与网关同值" —— **那个用例不存在**（见上方
# "已知缺口"）。"两侧同值"同样只有人读过，无判据。
CAPABILITY_STATIC_IMAGE: Final[str] = "STATIC_IMAGE"
DATA_CLASS_STATIC_IMAGE: Final[str] = "STATIC_IMAGE"

#: 本包**允许**出现在 `CapabilityRequest.data_classes` 里的类别 —— **受控元组**。
#:
#: 是元组而不是"调用方传什么就是什么"：ADR-10 的 E-2 把 `STATIC_IMAGE` 的
#: 出网数据写成**「批准的产品参考素材片段」**，而把整份素材库或正文全文
#: 发出去就**违反了最小化** —— 那是安全边界，不是性能问题。
#:
#: `FORBIDDEN_EGRESS_DATA_CLASSES`（含 `KNOWLEDGE_FULL` / 费用 / 审计 / 快照 /
#: 备份）**一律不得出现在本包的任何 request 里**；本常量只列**允许**的那个，
#: 于是"多传一个"在导出侧就被挡住（`gateway_bridge` 按它构造）。
EGRESS_DATA_CLASSES: Final[tuple[str, ...]] = (DATA_CLASS_STATIC_IMAGE,)


# ---------------------------------------------------------------------------
# 媒体形态（WP28，D3）：`media_versions.media_form`
# ---------------------------------------------------------------------------
# Live 与静态**共用**段落绑定与参考素材绑定，故用一列区分形态，而不是新增
# `task_kind`（D3）。⇒ `TASK_KINDS` 仍只有 `STATIC_IMAGE`，`calls_for` 的 `task_kind`
# 入参区分不了 Live，必须另给 `form`（见 `calls_for`）。
#
# 注意与 `REASON_MEDIA_FORMAT_UNSUPPORTED` 的 `FORMAT`（图片文件格式）无关：
# 此处是 `FORM`（媒体形态）。
MEDIA_FORM_STATIC: Final[str] = "STATIC"
MEDIA_FORM_LIVE: Final[str] = "LIVE"

#: 受控元组（不是"调用方传什么就是什么"）：其余取值一律 fail-closed。
MEDIA_FORMS: Final[tuple[str, ...]] = (MEDIA_FORM_STATIC, MEDIA_FORM_LIVE)
MEDIA_FORM_LIST_SQL: Final[str] = ", ".join(f"'{name}'" for name in MEDIA_FORMS)

#: Live 组件角色（`media_components.role`）。
#: **没有 `ORIGINAL`**（老大 2026-10-14 Q2）：原图即 `media_versions.image_file_id`，
#: `STILL` 组件指向同一文件，再设 `ORIGINAL` = 同一事实两个落点。
COMPONENT_ROLE_STILL: Final[str] = "STILL"
COMPONENT_ROLE_PAIRED_MOTION: Final[str] = "PAIRED_MOTION"
COMPONENT_ROLE_MANIFEST: Final[str] = "MANIFEST"
COMPONENT_ROLE_PREVIEW: Final[str] = "PREVIEW"
COMPONENT_ROLES: Final[tuple[str, ...]] = (
    COMPONENT_ROLE_STILL,
    COMPONENT_ROLE_PAIRED_MOTION,
    COMPONENT_ROLE_MANIFEST,
    COMPONENT_ROLE_PREVIEW,
)
COMPONENT_ROLE_LIST_SQL: Final[str] = ", ".join(f"'{name}'" for name in COMPONENT_ROLES)

#: 校验口径标记（`media_components.verification`）。**只有 `SYNTHETIC`**：
#: 没有真机，任何产出都不得宣称已验证（OP-183 / OP-76 红线）。
VERIFICATION_SYNTHETIC: Final[str] = "SYNTHETIC"
VERIFICATIONS: Final[tuple[str, ...]] = (VERIFICATION_SYNTHETIC,)
VERIFICATION_LIST_SQL: Final[str] = ", ".join(f"'{name}'" for name in VERIFICATIONS)

#: `live_profiles.state`。**只有 `PLACEHOLDER`**：占位不得当 `VERIFIED`。
LIVE_PROFILE_PLACEHOLDER: Final[str] = "PLACEHOLDER"
LIVE_PROFILE_STATES: Final[tuple[str, ...]] = (LIVE_PROFILE_PLACEHOLDER,)
LIVE_PROFILE_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in LIVE_PROFILE_STATES
)

#: `live_profiles` 的列长上界（4.2）。与 `MAX_*_CHARS` 同一理由：不可变/占位表一旦写进
#: 超长文本就难清，上界从 `types` 单点取，不在 `models` 里写字面量。
MAX_LIVE_PROFILE_KEY_CHARS: Final[int] = 100
MAX_LIVE_PROFILE_NOTE_CHARS: Final[int] = 500


# ---------------------------------------------------------------------------
# ★ 媒体张数：唯一的常量 + 唯一的算法 + 唯一的读点
# ---------------------------------------------------------------------------
#: **每篇内容配几张静态图** —— 老大 2026-10-07 拍板为**固定 3 张**。
#:
#: 见模块 docstring「老大的拍板」一节：这与原型的可配置下拉（6/4/9）
#: **不一致**，是一处**已登记的显式偏差**（OP-198）。
IMAGES_PER_CONTENT: Final[int] = 3

#: **每篇内容配几张 Live 计费图** —— X2a（老大 2026-10-14）：STILL + PREVIEW = 2 张。
#: `PAIRED_MOTION`（动态段）**不按张计**，成本漏计已登记为 OP-206（归属 WP25）。
LIVE_IMAGES_PER_CONTENT: Final[int] = 2

#: **每篇内容物化几个 Live 身份行（组）** —— 老大 2026-10-14 裁定 α：1 组。
#:
#: 与 `LIVE_IMAGES_PER_CONTENT` **不是同一个量**：前者数"身份行"（`media_items` 行数），
#: 后者数"计费张数"。一组 Live = STILL + PAIRED_MOTION + MANIFEST + PREVIEW 四个组件，
#: 其中 STILL + PREVIEW 两张按张计费（X2a）。两个数若混用（拿张数当行数），
#: 表现是"物化 2 行、每行又预占 2 张"⇒ 预占 4 张、实出 2 张，账面静默多占一倍。
LIVE_GROUPS_PER_CONTENT: Final[int] = 1

#: 一次生成请求允许的**内容条数**上界（防御性；真实上界由 WP24 的
#: `validate_quantity` 按批次档位判 —— 本处只挡"一个荒谬的大数"，
#: 因为 `images_for` 的结果要拿去乘单价、再进预占）。
MAX_CONTENT_COUNT: Final[int] = 1_000

#: 单条内容一次生成的**最大张数**（= 条数上界 × 每篇张数 的合理性上界）。
#: 用于 `images_for` 的返回值防御（见该函数）。
MAX_IMAGES_PER_REQUEST: Final[int] = MAX_CONTENT_COUNT * IMAGES_PER_CONTENT


def images_per_content(form: str = MEDIA_FORM_STATIC) -> int:
    """**全包唯一的读点**：每篇内容配几张图。

    `form` 默认 `STATIC`：现有 **30 处**无参调用语义不变（X2a）。
    `LIVE` 返回 `LIVE_IMAGES_PER_CONTENT`（2）。其余取值 **fail-closed**，
    不静默回落到静态 —— 回落的表现是"Live 请求按 3 张预占"，账面差一笔且不报错。

    静态返回值来自 `IMAGES_PER_CONTENT`（固定 3）。

    ## 为什么要有这个函数（而不是各处直接用常量）

    说明文档 §3.6 A 给了三条"可配置"路径，老大选了**第四条：固定值**。
    但"将来可能改"这件事必须留下一个**单一的切换点** ——
    否则一旦要改成可配置，就得在 `service` / `billing_bridge` /
    `api` 三处各改一次，而"改了两处漏一处"的表现是
    **预占按 3 张、实际出 6 张**（账面差一笔，不报错）。

    因此**所有需要这个数的地方都调本函数**，一处都不许直接读常量。
    测试里有一条断言 `images_per_content() is IMAGES_PER_CONTENT`
    （值同源）—— 它同时钉住"常量是唯一事实来源"。
    """
    if form == MEDIA_FORM_STATIC:
        return IMAGES_PER_CONTENT
    if form == MEDIA_FORM_LIVE:
        return LIVE_IMAGES_PER_CONTENT
    raise ValueError(f"未知的媒体形态：{form!r}（本包只认 {list(MEDIA_FORMS)}）。")


def _validate_content_count(content_count: int) -> int:
    """条数校验的**唯一实现**：`images_for` 与 `calls_for(form=LIVE)` 共用。

    抽出来的理由（WP28）：Live 路径不经 `images_for`（X2a），若在 `calls_for`
    里再抄一份校验，两份会各自演化 ⇒ 同一个输入在两条路径上一个过一个拒。
    """
    if not isinstance(content_count, int) or isinstance(content_count, bool):
        raise ValueError(
            f"内容条数必须是整数，实际是 {type(content_count).__name__}。"
        )
    if content_count < 0:
        raise ValueError(f"内容条数不得为负：{content_count}。")
    if content_count > MAX_CONTENT_COUNT:
        raise ValueError(
            f"内容条数超过本模块的防御上界：{content_count} > {MAX_CONTENT_COUNT}。"
            "（真实的批次上界由 WP24 的 `validate_quantity` 按档位判；"
            "本处只挡『一个荒谬的大数』—— 它会被乘上单价直接进预占）"
        )
    return content_count


def images_for(content_count: int) -> int:
    """**全包唯一的媒体张数算法**（R3）。**只算静态**（X2a）。

    一次生成要产多少张图 / 要预占几张的钱，答案只能从这里来。

    | 入参 | 结果 |
    |---|---|
    | `content_count` | `content_count × images_per_content()` |

    Live 不经本函数（X2a）：见 `calls_for(..., form=MEDIA_FORM_LIVE)`。

    ## 为什么签名里**没有** `images_per_content` 这个入参

    说明文档 §1.6 R3 的原始措辞是 `images_for(content_count, images_per_content)`。
    **本实现刻意去掉第二个入参**，理由是老大拍板"固定 3 张"之后，
    那个入参**没有任何合法来源**：

    - 若允许调用方传，它就能绕过 `images_per_content()` 自己给一个值
      ⇒ 同一个量有两个来源 ⇒ 违反本仓铁律 26（"同一个量只允许有一个取数来源"）；
    - 而"传少了"**不会报错**，只会在对账时表现为"账面永远差一笔"。

    去掉之后，"张数算错"这件事在**类型层面**就不可能发生。
    这与 `content/types.py::calls_for` 把入参定成 `task_kind` 而不是
    "调用次数"是同一种手法。

    ## 未知 `content_count` 抛错而不是返回 0

    `0` 会让张数为 0 ⇒ 预占金额为 0 ⇒ `reserve_budget` 拒绝非正数 ⇒
    报错点是"金额必须为正"，与真正的根因隔了两层。
    这里直接抛，报错点就是根因。

    ## 负数 `content_count`

    抛 `ValueError`。**不返回负数张数** —— 那会让 `amount` 为负，
    而负数的预占在 WP25 的 `_require_positive_amount` 处才会被拒；
    更糟的是若将来某处忘了校验，负数预占会**反向增加**可用额度。
    （这与 `calls_for` 的处理逐条同构。）
    """
    return _validate_content_count(content_count) * images_per_content()


def task_kinds_for_request() -> tuple[str, ...]:
    """一次媒体生成请求实际要跑的 `task_kind` 元组。

    本包只有一个 kind（`STATIC_IMAGE`），因此恒为单元素元组 ——
    但**仍然做成函数**：`handlers.py` 与 `service.py` 两处要用它，
    字面量在两处各写一遍的表现是"注册表挂了 A 字符串、派发用了 B"
    （任务永远没有处理器接手，**且不报错** —— 与 WP26 的
    `KIND_CONTENT_GENERATE` 注释里记录的同一种形态）。
    """
    return (TASK_STATIC_IMAGE,)


def calls_for(
    task_kind: str, content_count: int, *, form: str = MEDIA_FORM_STATIC
) -> int:
    """本包接缝用的**调用次数**口径（静态与 `images_for` **同源**，不是第二份算法）。

    `billing_bridge` 的 `estimate_cost` 要一个 `quantity`；图片模型**按张计费**
    （`PRICING_UNIT_IMAGE = "IMAGE"`，WP25 已定义）⇒ 这个 `quantity`
    **就是张数**，不是"内容条数"、也不是"调用次数 × 某系数"。

    - `form=STATIC`（默认）：**委托** `images_for` —— 不是第二份算法，而是
      "同一个算法在本包 task_kind 口径下的入口"。
    - `form=LIVE`（WP28，X2a）：`条数 × images_per_content("LIVE")`。
      **不经 `images_for`**；条数校验与静态共用 `_validate_content_count`，
      所以同一个非法输入在两条路径上同样被拒。

    `form` 是**仅关键字**参数：既有 `calls_for(kind, n)` 调用点不受影响，
    且不会出现位置参数顺序写反（`calls_for(kind, "LIVE", n)`）的静默错位。

    ⚠️ **`media_items` 的"条数"与 `images_for` 的"条数"是同一个量吗？**
    是 —— 本包一次请求处理**一条内容**（任务粒度同 WP26），
    `content_count` 就是"这一批有几条内容要出图"。
    这与 WP17 踩过的 ISS-51（"两个数数的是同一种东西吗"）是**显式核对过**
    的一处：两个数数的都是**内容条数**，不是"图数"与"内容数"混比。
    """
    if task_kind not in TASK_KINDS:
        raise ValueError(
            f"未知的生成类型：{task_kind!r}（本包只消费 {sorted(TASK_KINDS)}）。"
        )
    if form == MEDIA_FORM_STATIC:
        return images_for(content_count)
    if form == MEDIA_FORM_LIVE:
        return _validate_content_count(content_count) * images_per_content(form)
    raise ValueError(f"未知的媒体形态：{form!r}（本包只认 {list(MEDIA_FORMS)}）。")


# ---------------------------------------------------------------------------
# 媒体状态（`media_items.state` —— **投影**，可变表）
# ---------------------------------------------------------------------------
# `media_items` 是**身份**行（一"内容 × 段落"位一条，跨版本不变），
# 承载 `current_version_id` 这个会前进的投影与 `lock_version`（ETag 载体）。
# "这一次生成到哪一步了"必须可查：缺了状态列，前端只能靠"有没有版本行"猜
# —— 而"生成了但加工失败"这个中间态**恰好是"有版本行但不可交付"**，
# 靠猜会显示成"已完成"。
MEDIA_STATE_PENDING: Final[str] = "PENDING"
MEDIA_STATE_READY: Final[str] = "READY"
MEDIA_STATE_FAILED: Final[str] = "FAILED"

MEDIA_STATES: Final[frozenset[str]] = frozenset(
    {MEDIA_STATE_PENDING, MEDIA_STATE_READY, MEDIA_STATE_FAILED}
)

MEDIA_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(MEDIA_STATES)
)

#: `media_items` 的初始状态。**必须是 PENDING** —— 身份行在生成**之前**就建
#: （它与"这一段要配一张图"是同一件事），那时图还没有。
MEDIA_STATE_INITIAL: Final[str] = MEDIA_STATE_PENDING


# ---------------------------------------------------------------------------
# 加工产物档位（`media_renditions.kind`）
# ---------------------------------------------------------------------------
# 技术选型表 §5 原话：「媒体加工 | FFmpeg/ffprobe + Live 专用封装适配器 |
# **预览与正式资产分开存**」。两档，且**分成两行**（不是同行的两个 blob 列）
# —— 理由见说明文档 §1.7 的表格（"没有预览"与"预览失败"在列上同形）。
RENDITION_PREVIEW: Final[str] = "PREVIEW"
RENDITION_DELIVERY: Final[str] = "DELIVERY"

RENDITION_KINDS: Final[frozenset[str]] = frozenset(
    {RENDITION_PREVIEW, RENDITION_DELIVERY}
)

RENDITION_KIND_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(RENDITION_KINDS)
)

#: 加工产物的状态。
#:
#: **为什么需要它**（而不是"行存在 = 加工成功"）：加工是**本地 CPU 操作**，
#: 它会失败（源图损坏、Pillow 不支持该格式、磁盘满）。若"行存在即成功"，
#: 一次失败的加工会留下一个**指向 0 字节 blob 的 rendition 行**，
#: 而 WP35 的 ZIP 打包会把它当成可交付资产打进去。
RENDITION_STATE_PENDING: Final[str] = "PENDING"
RENDITION_STATE_READY: Final[str] = "READY"
RENDITION_STATE_FAILED: Final[str] = "FAILED"

RENDITION_STATES: Final[frozenset[str]] = frozenset(
    {RENDITION_STATE_PENDING, RENDITION_STATE_READY, RENDITION_STATE_FAILED}
)

RENDITION_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(RENDITION_STATES)
)

#: `media_renditions` 的初始状态。**必须是 PENDING** —— 与 `MEDIA_STATE_INITIAL`
#: 同一理由：行在**加工开始之前**就建（"这一次要加工"与"加工完了"是两件事），
#: 而"已经排上队但还没跑"正是 PENDING。若初始值给 READY，
#: 一行**还没产出 blob 的** rendition 会被当成可交付资产（而那条
#: `ready_requires_artifact` CHECK 会当场拒掉 —— 报错点离病根隔了一层）。
RENDITION_STATE_INITIAL: Final[str] = RENDITION_STATE_PENDING


# ---------------------------------------------------------------------------
# 参考素材状态（`reference_assets.state`）
# ---------------------------------------------------------------------------
# `reference_assets` **不可变**：一个新素材 = 一行；**"停用"是新行而不是改行**
# （说明文档 §1.7 表格）。因此 `state` 描述的是"这一行代表的那次登记动作
# 产出的素材，处在什么状态" —— 而不是"这一行被改成了什么状态"。
#
# 这条区分是必要的：若停用 = 改行，那张表就是**可变**的，
# 而"这张图画的是哪一版素材"的答案会随素材被停用而**悄悄改变**
# （历史图指向一个已被改写的素材行 ⇒ AQ-03 的对照关系失效）。
ASSET_STATE_ACTIVE: Final[str] = "ACTIVE"
ASSET_STATE_RETIRED: Final[str] = "RETIRED"

ASSET_STATES: Final[frozenset[str]] = frozenset(
    {ASSET_STATE_ACTIVE, ASSET_STATE_RETIRED}
)

ASSET_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(ASSET_STATES)
)


# ---------------------------------------------------------------------------
# 媒体顺序（`media_items.sort_order`）与封面位
# ---------------------------------------------------------------------------
# `trial-config.html:324` 有"封面固定第 1 位"的交互，而协议未提 ——
# 说明文档 §2.4 的 OP-204 登记的正是这一点。
#
# 本包**只记顺序**（`sort_order` 从 0 起，0 即封面位），**不做封面构图**。
# 为什么不落"封面必须是 0"的 CHECK：那是 OP-204 的待定内容，
# 落成 CHECK 会造出"把产品尚在讨论的交互变成未来硬约束"的问题
# （WP26 的 `types.py` 已就同一取向论证过：自由文本/待定项不落 CHECK）。
MIN_SORT_ORDER: Final[int] = 0

#: 封面位的 sort_order 值。**只是常量，不是约束**（见上）。
SORT_ORDER_COVER: Final[int] = 0


# ---------------------------------------------------------------------------
# 生成上下文的 task_kind（OP-160 的同构落点）
# ---------------------------------------------------------------------------
# `media_generation_contexts` 每次生成落一行，钉住"这次用的是哪版提示词 /
# 哪个模型档案 / 哪些参考素材"。**取值域与 `TASK_KINDS` 同源**（故意）：
# 上下文行的 `task_kind` 就是"这次跑的是哪一种"，多一种就说明本包多消费了
# 一个 kind，而 `calls_for` 会因此抛错 —— 两处同时变才可能一致。
GENERATION_CONTEXT_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(TASK_KINDS)
)


# ---------------------------------------------------------------------------
# 长度上界
# ---------------------------------------------------------------------------
# **为什么每个上界都要显式写出来**：这些值会进错误消息与响应体。
# 允许任意长度等于让一个 1MB 的字符串撑进不可变表 ——
# 而 `media_versions` / `reference_assets` / `media_renditions` /
# `media_generation_contexts` 都**不可变**（触发器拦 UPDATE/DELETE），
# 一旦写进去就删不掉。同族的 `content/types.py` 用同一理由设过
# `MAX_BODY_CHARS`（WP26）、`runtime_settings` 设过 `MAX_CHANGE_REASON_CHARS`（WP24）。
MAX_PROMPT_CHARS: Final[int] = 20_000
MAX_IMAGE_REF_CHARS: Final[int] = 500
MAX_STORAGE_KEY_CHARS: Final[int] = 512
MAX_MIME_TYPE_CHARS: Final[int] = 100
MAX_FORMAT_CHARS: Final[int] = 20
MAX_PRODUCT_MODEL_CHARS: Final[int] = 200
MAX_PRODUCT_COLOR_CHARS: Final[int] = 100
MAX_PRODUCT_PART_CHARS: Final[int] = 200
MAX_ASSET_SOURCE_CHARS: Final[int] = 500
MAX_ASSET_NOTE_CHARS: Final[int] = 500
MAX_REGENERATION_REASON_CHARS: Final[int] = 500
MAX_RENDITION_NOTE_CHARS: Final[int] = 500
MAX_PARAGRAPH_REF_CHARS: Final[int] = 200

#: 媒体版本的 payload schema 版本起始值（与本仓惯例一致）。
MEDIA_SCHEMA_VERSION: Final[int] = 1

#: 参考素材绑定的**数量上界**。一张图绑定"具体的几条素材"：
#: `MIN = 1`（至少一条，否则 AQ-03 的对照关系不成立）、`MAX = 8`
#: （多部件产品的上限；超过 8 条说明在做"整库引用"，正是 R1 要防的形态）。
MIN_REFERENCE_ASSETS: Final[int] = 1
MAX_REFERENCE_ASSETS: Final[int] = 8

#: `media_items.pending_reference_bindings` 的CHECK 片段（**唯一来源**）。
#:
#: ★ **为什么投影表要再存一份绑定**（这是本包唯一一个"看起来是重复"的列，
#:   值得把理由写全）：
#:
#: | | `media_versions.reference_bindings` | `media_items.pending_reference_bindings` |
#: |---|---|---|
#: | 语义 | **已生成**的那一版绑了什么 | **下一次生成**要绑什么 |
#: | 可变性 | 不可变（触发器拦UPDATE/DELETE） | 可变（投影，`lock_version` 保护） |
#: | 生命周期 | 生成成功后才有 | **入队时**就有 |
#:
#: 生成是**异步**的（`KIND_MEDIA_GENERATE` 走 `jobs` 队列，端点返回后任务才跑）。
#: 若绑定只在 `media_versions` 上，端点返回那一刻它**还不存在** ⇒ 任务重放时
#: 绑定已丢失，而 R1 会退化成"没有绑定"（拿到空集合而**不报错**）。
#:
#: ⚠️ **它不是第二个真值来源**：落版本时 `insert_version` 拿它写
#: `media_versions.reference_bindings`，生成成功后投影列即被 `NULL` 覆盖
#: ⇒ 同一时刻只有一处有值。判据见 `test_media_wp27.py` 的"投影清空"用例。
#:
#: **为什么不放进 `generation_request`**：那一列是 `MediaGenerationRequest`
#: 的三键白名单（`taskKind` / `modelProfileId` / `batchKind`），
#: 语义是"**怎么**生成"；绑定是"**绑什么**"。塞进去会让一个契约同时
#: 承载两类正交信息，而 `schemas.validate_generation_request_payload`
#: 是 fail-closed 拒多余键的 ⇒ 扩它要改三处，且让"请求形状"与"内容输入"分叉。
PENDING_BINDINGS_IS_ARRAY_SQL: Final[str] = (
    "jsonb_typeof(pending_reference_bindings) = 'array'"
)
PENDING_BINDINGS_SIZE_SQL: Final[str] = (
    "jsonb_array_length(pending_reference_bindings) BETWEEN "
    f"{MIN_REFERENCE_ASSETS} AND {MAX_REFERENCE_ASSETS}"
)

#: `media_items.pending_regeneration_reason` 的 CHECK 片段（**唯一来源**）。
#:
#: ★ **这是 `pending_reference_bindings` 的同族缺口**（F7「单张重做」）：
#:   与绑定完全同形 —— 端点在**发起**事务里拿到理由，任务在**另一次事务**
#:   里才写版本行，中间那一列就是本条。
#:
#: **不补这一列的后果**（实测于补列之前）：`service.item_pending_regeneration_reason`
#: 恒返回 `None`（没有可读的暂存位），于是**每一次重做**产生的版本行
#: `regeneration_reason` 都是 NULL —— 而 `media_versions.regeneration_reason`
#: 存在的**唯一理由**就是记录"第 2 版与第 1 版为什么不一样"
#: （models.py docstring 十）。**列在、值恒空**，且不报错
#: ⇒ 这是"看起来有留痕、实际没有留痕"，比没有这一列更坏
#: （ISS-95「声称有防线」同族）。
#:
#: **为什么不塞进 `pending_reference_bindings` 的 JSON**：
#: 那个列的 CHECK 是 `jsonb_array_length BETWEEN 1 AND 8`
#: （`PENDING_BINDINGS_SIZE_SQL`）—— 它约束的是**数组元素**，
#: 而理由是**标量**。混进去要么让数组多出一个非绑定元素
#: （`MediaReferenceBinding.from_payload` 当场抛 `ValueError`
#: ⇒ `REASON_REFERENCE_BINDING_MALFORMED`，把"重做"报成"绑定坏"），
#: 要么把 `jsonb_typeof = 'array'` 改成 `'object'` 而作废上面两条 CHECK。
#:
#: **可空，且 NULL 是常态**：首次生成**没有**理由可言
#: （`media_versions.regeneration_reason` 同一取值口径：首次生成的行恒为 NULL）。
#: ⇒ 因此**不落"非空"CHECK**，也不落"重做时必非空"CHECK ——
#: "这一行是不是重做产生的"**无法从列判断**（重做与首次生成在列上同形），
#: 那种 CHECK 会造出一条恒真或恒假的判据（ISS-115 族），
#: 与 models.py docstring 十对版本列的结论**逐字同一条理由**。
#:
#: **上界与版本列同源**：两列都引用 `MAX_REGENERATION_REASON_CHARS`
#: —— 同一个量（"理由最多多长"）只有一处来源。两列若各写一个上界，
#: 表现是"端点按 500 校验通过、DB 按另一个值拒掉"，而报出来的是
#: 一个 SQLSTATE 而不是一个 reason 码。
#:
#: **空串在这里合法，但端点必须拒它**：本片段沿用版本列的
#: `IS NULL OR char_length(...) <= N` 形状 ⇒ `''` 通过（`char_length('') = 0`）。
#: 这是**刻意的口径对齐**：SQL 层的职责是"拦超长"，"理由不能是空白"
#: 是**输入校验**（`schemas.py` 在端点层拒，报业务码，而不是让用户
#: 看见一个 CHECK violation 的 500）。两层分工与版本列一致。
PENDING_REGENERATION_REASON_SQL: Final[str] = (
    "pending_regeneration_reason IS NULL"
    f" OR char_length(pending_regeneration_reason)"
    f" <= {MAX_REGENERATION_REASON_CHARS}"
)


# ---------------------------------------------------------------------------
# 任务寻址：`media_items.job_id`（★ 本包最关键的接缝裁定）
# ---------------------------------------------------------------------------
# 问题：`KIND_MEDIA_GENERATE` 的任务要产出"**某一张图的新版本**"，因此
# 处理器必须同时知道 `media_item_id` **和** `content_id`（后者用于读正文
# 切段、算 R4）。而任务框架给处理器的东西只有 `ClaimedJob`
# （`job_id / kind / attempt_id / fence_token / attempt_no / worker_id /
# input_version_ids`）—— **没有自由参数位**（`jobs/types.py`）。
#
# `jobs` 表本身也没有 actor / payload 列，唯一能装关联对象的只有
# `jobs.content_id`（裸 UUID，**刻意无 FK**，见 `jobs/models.py:195`：
# "batches / contents 表属 WP24 / WP29，现在建外键会指向不存在的表"）。
#
# 三条先例，**形态相反**：
#
# | 包 | 任务目标 vs 入队对象 | 处置 |
# |---|---|---|
# | WP26 | 同一（都是内容） | 用 `jobs.content_id` 装 `content_items.id` |
# | WP17/parsing | **不同**（目标是产出 `parse_run`） | 加 `parse_runs.job_id`（FK→`jobs.id`）+ 索引，处理器 `claimed.job_id` **反查** |
# | WP27 | 目标是"单张图的新版本"，且**还要** `content_id` | ⇒ **本包取 WP17 同构**（见下） |
#
# **为什么不能取 WP26 那条**：`jobs.content_id` 只有一个位。
# 拿它装 `media_item_id` ⇒ 处理器拿不到 `content_id`
# （`execute_media_generation` 需要它算 R4 与读正文）；
# 拿它装 `content_id` ⇒ 处理器拿不到"是哪一张图"，只会重做第一格。
# 两者都会**静默做错**（不报错），因此都不是选项。
#
# **为什么不改 `jobs` 表**：`content/types.py:597` 已把"本包不修改 `jobs`"
# 定为接缝冻结铁律（WP13 的资产）。加列会同时影响 WP17/WP26 两条
# 已在跑的任务链，而**收益只是省一次反查**。
#
# ⇒ 落在**本包自己的表**上：`media_items.job_id`，处理器
# `find_media_item_for_job(claimed.job_id)` 反查。
#
# ⚠️ **反查返回 `None` 时必须 fail-closed**（`ERROR_TARGET_MISSING`，
# `retryable=False`），**不许**退回"取该内容的第一格"——
# 那会让装配故障伪装成"重做封面"，而没有任何地方报错。
#
# ⚠️ **本列是"当前在途任务的指针"，不是一份任务历史**：
# 同一 `media_item` 的**重做会覆盖**它（新 job 覆盖旧 job）。旧 job
# 若此时才被重放，反查不到 ⇒ `FAILED(TARGET_MISSING)` —— 这是**正确**的
# 终态：它的 `generation_request` 已被新一轮覆盖，继续跑会"按新参数出图、
# 按旧 job 记账"，**比失败更坏**。


# ---------------------------------------------------------------------------
# 业务错误原因码
# ---------------------------------------------------------------------------
# **为什么要有独立的原因码**：错误响应里的 `message` 是给人读的文案，会变；
# 而 `details.reason` 是机器可判的。测试与前端要断言的是后者。
# 与 WP22/WP23/WP24/WP25/WP26 的 `REASON_*` 同一手法。
REASON_MEDIA_NOT_FOUND: Final[str] = "MEDIA_NOT_FOUND"
REASON_MEDIA_VERSION_NOT_FOUND: Final[str] = "MEDIA_VERSION_NOT_FOUND"
REASON_MEDIA_ITEM_NOT_FOUND: Final[str] = "MEDIA_ITEM_NOT_FOUND"
REASON_CONTENT_NOT_FOUND: Final[str] = "CONTENT_NOT_FOUND"
REASON_CONTENT_VERSION_NOT_FOUND: Final[str] = "CONTENT_VERSION_NOT_FOUND"
REASON_PROJECT_NOT_FOUND: Final[str] = "PROJECT_NOT_FOUND"
REASON_REFERENCE_ASSET_NOT_FOUND: Final[str] = "REFERENCE_ASSET_NOT_FOUND"
REASON_REFERENCE_ASSET_NOT_ACTIVE: Final[str] = "REFERENCE_ASSET_NOT_ACTIVE"
REASON_REFERENCE_ASSET_MISMATCH: Final[str] = "REFERENCE_ASSET_PROJECT_MISMATCH"
REASON_TASK_KIND_UNKNOWN: Final[str] = "TASK_KIND_UNKNOWN"
REASON_MODEL_PROFILE_NOT_FOUND: Final[str] = "MODEL_PROFILE_NOT_FOUND"

# --- R4：段落绑定 ------------------------------------------------------------
REASON_PARAGRAPH_REF_MISSING: Final[str] = "PARAGRAPH_REF_MISSING"
REASON_PARAGRAPH_REF_UNKNOWN: Final[str] = "PARAGRAPH_REF_UNKNOWN"

# --- R1：参考素材绑定（★ AQ-03 的地基）--------------------------------------
# 三个码**按拒绝原因分开**，而不是一个笼统的 `BINDING_INVALID`：
# 排障时要能一眼看出"是没绑"还是"绑太多"还是"绑到了别的项目"
# —— 这三类在 `schemas.py` 里是三条独立分支，报同一个码会让
# "哪个分支生效了"无法从响应里读出来。
REASON_REFERENCE_BINDING_MISSING: Final[str] = "REFERENCE_BINDING_MISSING"
REASON_REFERENCE_BINDING_TOO_MANY: Final[str] = "REFERENCE_BINDING_TOO_MANY"
REASON_REFERENCE_BINDING_CROSS_PROJECT: Final[str] = "REFERENCE_BINDING_CROSS_PROJECT"

# --- R1 的两条**机械判据**（文档 §1.6 R1 的判据原文没写全，此处补足）--------
# R1 的判据原文：「断言『每张图绑定的素材 == 1 条』，且『全篇绑定的素材
# 去重后 < 素材库全部』」。这两句话里各有一个量**没被上面的码覆盖**，
# 而两者的失效都是静默的（`bindings_size` CHECK 照样全绿）：
#
# ① 「每张图绑定的素材 == 1 条」里的"素材数" —— 是 `jsonb_array_length`
#    （**绑定条目数**）还是去重后的 **asset_id 数**？同一条素材在一张图里
#    绑了两次时，条目数 = 2 而素材数 = 1 ⇒ `bindings_size`（闭区间
#    1..8）**照样通过** ⇒ "绑了两条"实际只绑了一个素材，没有任何地方知道。
#    处置：`schemas.py` **显式分开算这两个量**，不等即拒（本码）——
#    于是"绑定条目数"与"绑定素材数"各自只有一个来源（铁律 26）。
REASON_REFERENCE_BINDING_DUPLICATE: Final[str] = "REFERENCE_BINDING_DUPLICATE"

# ② 「每张图绑定的素材」这句还预设了"绑定元素**是可解析的**"。
#    元素键形状坏（缺键 / 多键 / `assetId` 不是 UUID）时，
#    `MediaReferenceBinding.from_payload` 会抛 `ValueError` —— 但那是
#    `types.py` 的异常；若 `schemas.py` 不接住并翻成本码，它会一路穿到
#    端点变成 **500**（"服务内部错误"），而真正的根因是"写入方写坏了
#    绑定形状"。本仓已多次记录"未登记的 reason ⇒ 静默 500"——
#    WP26 的 `REASON_BASELINE_MISMATCH` 就是同一形态（其
#    `collect_wp26_code_review.py` 的 CR-1 CALIBER-1 坐实了这一点）。
REASON_REFERENCE_BINDING_MALFORMED: Final[str] = "REFERENCE_BINDING_MALFORMED"

#: ⚠️ `REASON_REFERENCE_ASSET_MISMATCH`（值 = `REFERENCE_ASSET_PROJECT_MISMATCH`）
#: 与本组的 `REASON_REFERENCE_BINDING_CROSS_PROJECT` **都表达"跨项目"**，
#: 但它们是**两条不同的调用路径**，不可互相替代、也不可合并：
#:
#: | 码 | 路径 | 用户看到的是 |
#: |---|---|---|
#: | `REASON_REFERENCE_ASSET_MISMATCH` | **单资源寻址**（`GET`/`PATCH` 某个 asset_id） | "你寻址的这个素材不属于本项目" |
#: | `REASON_REFERENCE_BINDING_CROSS_PROJECT` | **绑定集合校验**（生成请求里的一批 asset_id） | "你的绑定里有一条属于别的项目" |
#:
#: 前者答"这个资源你能不能看"，后者答"这批引用里哪一条越了界"——
#: 而后者**必须能指出是集合里的哪一条**（`schemas.py` 把它作为 `field`
#: 定位），否则排障时只能看到"有一处跨项目"而不知是哪一处。
#: 两者合一的后果：单资源端点会对着一批 id 报"不属于本项目"，
#: 而那批 id 里可能只有一条越界 —— 信息量反而下降。

# --- R2：拿到之后、入库之前的四道校验 ----------------------------------------
# **四个码按拒绝原因分开**（与 WP26 的四类必拒同款）：
#   · `MALFORMED_MEDIA`   —— Pillow 打不开（坏字节 / 非图片）
#   · `EMPTY_MEDIA`       —— 能打开但宽或高为 0（空白图 / 0×0）
#   · `MEDIA_TOO_SMALL`   —— 尺寸低于合法下界（1×1 占位图）
#   · `MEDIA_TOO_LARGE`   —— 超过像素上限（解压炸弹）
#
# 像素上限**不在本模块定义**：唯一来源是
#   `rednote.modules.parsing.parsers.image.MAX_IMAGE_PIXELS`
# 本模块刻意 **不** 顶层 import 它 —— `types.py` 在本仓是叶子模块
# （对照 `content/types.py`：零顶层 `from rednote` 导入），拖 `parsing.ocr`
# 进来只为拿来一个 int，会把叶子变成依赖项。取数点唯一地放在
# `service.py` 的 R2 校验处（本地 import），**不是**第二个常量（铁律 26）。
# 四类的**修法不同**（换源图 / 换源图 / 换源图 / 缩图），
# 报同一个码会让"该做什么"不可判。
REASON_MEDIA_MALFORMED: Final[str] = "MALFORMED_MEDIA"
REASON_MEDIA_EMPTY: Final[str] = "EMPTY_MEDIA"
REASON_MEDIA_TOO_SMALL: Final[str] = "MEDIA_TOO_SMALL"
REASON_MEDIA_TOO_LARGE: Final[str] = "MEDIA_TOO_LARGE"
REASON_MEDIA_FORMAT_UNSUPPORTED: Final[str] = "MEDIA_FORMAT_UNSUPPORTED"

# --- R5：没有激活提示词版本（fail-closed）------------------------------------
REASON_PROMPT_NOT_ACTIVATED: Final[str] = "PROMPT_NOT_ACTIVATED"
REASON_PROMPT_TEMPLATE_MISSING: Final[str] = "PROMPT_TEMPLATE_MISSING"

# --- R7 / R8：网关 ----------------------------------------------------------
REASON_GATEWAY_DENIED: Final[str] = "GATEWAY_DENIED"
REASON_GATEWAY_NOT_SUCCEEDED: Final[str] = "GATEWAY_NOT_SUCCEEDED"
REASON_GATEWAY_NO_OUTPUT: Final[str] = "GATEWAY_NO_OUTPUT"
REASON_GATEWAY_NO_IMAGE_REF: Final[str] = "GATEWAY_NO_IMAGE_REF"

# --- 服务层的业务前置 -------------------------------------------------------
REASON_GENERATION_REQUEST_INVALID: Final[str] = "GENERATION_REQUEST_INVALID"

#: **装配故障**：某个依赖（`prompt_repo` / `billing_repo` / `gateway_repo`）
#: 没有注入。与"业务前置不成立"（内容不存在 / 素材失效）是**两类**失败。
#:
#: ★ **为什么要单列一个码**（而不是复用 `GENERATION_REQUEST_INVALID`）：
#: 两者的**责任方不同** ——
#: - `GENERATION_REQUEST_INVALID` ⇒ 调用方/数据的问题，改载荷能好；
#: - 本码 ⇒ **装配**的问题，改代码或改启动参数能好，重试一万次也不会变。
#:
#: 混成一个码时，排障会去查"谁传的载荷"，而真因在`main.py` 没注入依赖 ——
#: 这与ISS-95「声称有防线」同族：报出来的码不指向真正的原因。
#:
#: **不可重试**：`handlers` 把它折成 `retryable=False`（与 WP26 的
#: `UnavailableGenerator.reason` 同一处置）。
REASON_GENERATOR_NOT_CONFIGURED: Final[str] = "GENERATOR_NOT_CONFIGURED"
REASON_PROJECTION_CONFLICT: Final[str] = "PROJECTION_CONFLICT"
REASON_RENDITION_KIND_UNKNOWN: Final[str] = "RENDITION_KIND_UNKNOWN"
REASON_IDEMPOTENCY_CONFLICT: Final[str] = "IDEMPOTENCY_CONFLICT"
#: 同一篇内容上 Live 与静态不能共存（WP28）：`media_items` 唯一键
#: `(content_item_id, sort_order)` 让 Live 组行（sort_order=0）与静态封面同位。
#: 在发起期给出可读拒绝，而不是等数据库抛 `IntegrityError`。
REASON_MEDIA_FORM_CONFLICT: Final[str] = "MEDIA_FORM_CONFLICT"


# ---------------------------------------------------------------------------
# 任务框架的取值（WP13）：kind / queue / 幂等域
# ---------------------------------------------------------------------------
# 与 `content/types.py` 的 `KIND_CONTENT_GENERATE` 同惯例：**放在 types.py**
# 而不是 service/handlers 里。两个值各有**两个**消费点（发起端 `api.py` 入队、
# 处理器侧 `handlers.py` 注册），字面量在两处各写一遍的表现是
# "入队用了 A 字符串、注册表挂了 B 字符串" ⇒ 任务永远没有处理器接手，
# 而它**不报错**：任务安安静静躺在 `QUEUED`。
KIND_MEDIA_GENERATE: Final[str] = "MEDIA_GENERATE"

#: 队列名。与 `QUEUE_BRIEF`（"brief"）/ `QUEUE_CONTENT`（"content"）同形。
QUEUE_MEDIA: Final[str] = "media"

#: 入队幂等域（`jobs.idempotency_scope`）。
#:
#: **与 `gateway_bridge` 的 `idempotency_key` 不是同一个东西**：
#: 这个是**任务层**的（"同一个入队请求不要建两个任务"），
#: 那个是**模型调用层**的（R5，"同一次生成不要买两次"）。
#: 两者混同的后果：重做（版本号前进）在任务层被当成重复请求 ⇒ 重做不生效。
#: 因此任务层的键**不含版本号**（同一内容的重做要建新任务），
#: 模型层的键**含版本号**（同一版本的重放不重发）。
IDEMPOTENCY_SCOPE_MEDIA_GENERATE: Final[str] = "media-generate"


#: `generation_request` 的键名（借 WP26 与 WP18 的先例：
#: 生成参数落在**本包自己的表**上，不新建"只为传一次参数"的表）。
#: 载荷有**两个**读写点（`service.py` 构造、处理器解析），
#: 字面量拼错的表现是"缺键 ⇒ 参数没传" —— 而它**不会报错**。
REQUEST_KEY_TASK_KIND: Final[str] = "taskKind"
REQUEST_KEY_MODEL_PROFILE_ID: Final[str] = "modelProfileId"
REQUEST_KEY_BATCH_KIND: Final[str] = "batchKind"
#: 第四键（WP28，老大 2026-10-14 方案 A）：媒体形态。**唯一允许缺省的键**，
#: 缺键按 `STATIC` 解析 —— 这样 WP27 时代落库的旧行无需回填即可读。
REQUEST_KEY_MEDIA_FORM: Final[str] = "mediaForm"

#: 载荷的**必需**三键（缺一即视为载荷被写坏）。
MEDIA_REQUEST_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {
        REQUEST_KEY_TASK_KIND,
        REQUEST_KEY_MODEL_PROFILE_ID,
        REQUEST_KEY_BATCH_KIND,
    }
)

#: 载荷允许出现的**全部**键（= 必需三键 + 可选 `mediaForm`；多一个键即视为载荷被写坏）。
MEDIA_REQUEST_KEYS: Final[frozenset[str]] = MEDIA_REQUEST_REQUIRED_KEYS | {
    REQUEST_KEY_MEDIA_FORM
}


# ---------------------------------------------------------------------------
# ★ `media_versions.reference_bindings` 元素的键名 —— **三个消费点，一个来源**
# ---------------------------------------------------------------------------
# 这三个键名有**三个**消费点，且分属三个文件、三个层次：
#
#   ① `models.py` / 本模块 —— JSONB 的**写入**形状（`MediaReferenceBinding`）；
#   ② `schemas.py` —— 校验时**读出**这些键；
#   ③ `ddl.py` —— plpgsql 里 `elem->>'assetId'`（第 3.1/3.2 条一致性触发器）。
#
# 三处各写一遍字面量的表现是**静默的**（与 ISS-66 / ISS-137 同族）：
# 写入侧写 `asset_id`、读取侧读 `assetId` ⇒ 校验看到的是"每条绑定都没有 assetId"，
# 而 `jsonb_array_length` 仍然是 1 ⇒ **CHECK 照样过**，DDL 触发器也照常全绿
# （它是从同一个错键名里查，查不到 ⇒ `missing_asset IS NULL` ⇒ 放行）。
# 于是一列**指向空气**的绑定被全程放行，直到 AQ-03 盲审时才发现"这张图
# 画的型号对不对"答不出来 —— 而那已经是付过钱之后。
#
# ⚠️ 与 `content/types.py::BINDING_KEY_*` 的**区别**（不是照抄，是加强）：
# WP26 那边这两个键只有**两个**消费点（写侧 `service.py` / 读侧 `schemas.py`），
# 而 `schemas.py` 当时**手写了字面量**（`obj.get("paragraphIndex")`）——
# 常量只在一侧被消费，"唯一来源"实际没成立（ISS-182 的同族形态）。
# 本包多出 `ddl.py` 这第三个消费点，且它住在 plpgsql 字符串里，
# **不可能靠 import 拿到常量** —— 只能由 `ddl.py` 做 f-string 插值。
# 因此本包的写法是：**三处全部从本组常量派生**，一处都不许手写。
BINDING_KEY_ASSET_ID: Final[str] = "assetId"
BINDING_KEY_PART: Final[str] = "part"
BINDING_KEY_NOTE: Final[str] = "note"

#: 绑定的**白名单**键集（`part` / `note` 可为 null，但**键必须存在**）。
#:
#: 与 `content/types.py::BINDING_KEYS` 同一取向：多出来的键一律拒绝。
#: 不做的话，一行里塞进 `{"assetId": …, "asset_id": …}` 也照收，
#: 而写的一方以为改了后者 —— 两边各自"看着都对"。
BINDING_KEYS: Final[frozenset[str]] = frozenset(
    {
        BINDING_KEY_ASSET_ID,
        BINDING_KEY_PART,
        BINDING_KEY_NOTE,
    }
)


# ---------------------------------------------------------------------------
# 值类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MediaReferenceBinding:
    """一张生成图与**一条**参考素材的绑定（R1 的落点）。

    | 字段 | 含义 |
    |---|---|
    | `asset_id` | 绑定的参考素材行 |
    | `part` | 这张图画的是该产品的**哪个部件**（可选，供目检者对照） |
    | `note` | 说明（可选） |

    ## ★ 为什么绑定粒度必须到**素材行**，而不是"整份素材库"

    那是 R1 的原始形态：若绑定退化成"这张图引用了素材库里的全部素材"，
    那么**每张图的绑定集合都相同** —— 而这个集合与这张图画了什么无关，
    于是"这张图画的型号对不对"这个问题**仍然答不出来**，AQ-03 也就判不成。

    这是"看起来有防线"的典型：`len(bindings) > 0` 会恒真地通过，
    测试也"通过"，而盲审仍然只能靠人凭印象。

    **可判的形式**见 `schemas.py::validate_reference_bindings`：
    断言"绑定的素材数 <= `MAX_REFERENCE_ASSETS`"且"去重后 >= 1"；
    构造"每张图都绑全部素材"的场景必须被 `REASON_REFERENCE_BINDING_TOO_MANY`
    拒掉（素材库超过上界时）。
    """

    asset_id: uuid.UUID
    part: str | None = None
    note: str | None = None

    def as_payload(self) -> dict[str, Any]:
        """落库形状（**唯一的写入序列化点**）。

        与 `MediaGenerationRequest.as_payload` 同一条理由：
        键名只能从 `BINDING_KEY_*` 来。`asset_id` 转 `str` ——
        JSONB 不认 UUID 对象（`json.dumps` 会直接报 `TypeError`）。

        **`part` / `note` 即使为 None 也写出键**：形状是"三键齐"的，
        `ddl.py` 与 `schemas.py` 都按三键读。省略键会让
        "没有部件标注"与"键被写坏"在这两类读者眼里同形。
        """
        return {
            BINDING_KEY_ASSET_ID: str(self.asset_id),
            BINDING_KEY_PART: self.part,
            BINDING_KEY_NOTE: self.note,
        }

    @classmethod
    def from_payload(cls, raw: Any) -> MediaReferenceBinding:
        """解析落库形状（**唯一的读取反序列化点**）。fail-closed。

        **不给 `part` / `note` 默认值** —— 与 `MediaGenerationRequest`
        的取舍同款但理由不同：这两个字段本身可为 null，**省掉键的写法是合法的
        数据**（`None` 与"键不在"在读侧无法区分）。因此这里要求
        **键必须存在**（白名单 `BINDING_KEYS` 的用法），
        缺键即"写入方与读取方对形状的理解已经分叉"⇒ 抛出。
        """
        if not isinstance(raw, dict):
            raise ValueError(
                f"参考素材绑定必须是 JSON 对象，实际是 {type(raw).__name__}。"
            )
        missing = BINDING_KEYS - raw.keys()
        if missing:
            raise ValueError(
                f"参考素材绑定缺键：{sorted(missing)}。"
                f"应有三键：{sorted(BINDING_KEYS)}。"
                "（缺键即『写入方与读取方对形状的理解已分叉』；"
                "`part` / `note` 可为 null，但**键必须在**）"
            )
        extra = raw.keys() - BINDING_KEYS
        if extra:
            raise ValueError(
                f"参考素材绑定有多余键：{sorted(extra)}。"
                "（拒多余键与 `schemas.py` 一致：多一个键说明两边理解已经分叉）"
            )
        try:
            asset_id = uuid.UUID(str(raw[BINDING_KEY_ASSET_ID]))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError(
                f"{BINDING_KEY_ASSET_ID} 不是合法 UUID："
                f"{raw[BINDING_KEY_ASSET_ID]!r}。"
            ) from exc
        for key in (BINDING_KEY_PART, BINDING_KEY_NOTE):
            value = raw[key]
            if value is not None and not isinstance(value, str):
                raise ValueError(
                    f"{key} 必须是字符串或 null，"
                    f"实际是 {type(value).__name__}。"
                )
        return cls(
            asset_id=asset_id,
            part=raw[BINDING_KEY_PART],
            note=raw[BINDING_KEY_NOTE],
        )


@dataclass(frozen=True, slots=True)
class MediaGenerationRequest:
    """一次媒体生成请求在**执行阶段**需要的全部参数。

    ## 只有三个字段 —— 不是省，是"凡能查的都不存"

    | 字段 | 为什么必须存 | 为什么不能查 |
    |---|---|---|
    | `task_kind` | 决定跑哪个能力、以及响应要如实回填"实际跑了什么" | 它是**用户的意图**；库里没有列能反推 |
    | `batch_kind` | `estimate_cost` 的必填入参（WP24 的 10/30/50 硬约束靠它生效） | 它是**请求的属性**（用户选试产/扩产/批产），**不是条数的函数** —— 从条数反推档位会让"猜出的档位必然容纳该条数"，进而把 G3 的硬约束**静默关掉**（`billing_bridge` 已论证） |
    | `model_profile_id` | `estimate_cost` 与 `CapabilityRequest` 都要它 | 它决定用哪个模型，而"用哪个模型"是**请求的选择**；本仓无"项目默认模型档案"概念 |

    **`provider_id` 不在其中**：它由 `model_profile_id` 唯一解出 ——
    存进来就有两个来源，而两者分叉的表现是"预占记在 A 供应商、
    实际调用走 B"，账面与留痕对不上却都不报错。

    **`content_count` 不在其中**：任务粒度是**单条内容**，
    因此处理器侧恒以 `count=1` 调用 `images_for`。

    **`images_per_content` 不在其中**（★ 与说明文档 §3.6 A3 的一处偏离）：
    A3 的原方案是"张数走请求参数"，而老大拍板"固定 3 张"之后，
    让调用方能传张数就**制造了第二个取数来源**（违反铁律 26）。
    张数只能从 `images_per_content()` 来。
    """

    task_kind: str
    batch_kind: str
    model_profile_id: uuid.UUID
    #: 媒体形态（WP28）。**唯一带默认值的字段**：默认 `STATIC`，既有构造点零改动。
    media_form: str = MEDIA_FORM_STATIC

    def __post_init__(self) -> None:
        """**构造即校验**（fail-closed）。

        未知 `task_kind` 在**构造时**抛 —— 端点那次事务里抛，
        那里有响应可组装（service 翻成 422）。若留到执行阶段才抛，
        参数已经落库、任务已入队，报错位置离病根隔了一次事务边界。
        """
        if not isinstance(self.task_kind, str) or not self.task_kind.strip():
            raise ValueError(
                f"{REQUEST_KEY_TASK_KIND} 必须是非空字符串，"
                f"实际是 {self.task_kind!r}。"
            )
        if self.task_kind not in TASK_KINDS:
            raise ValueError(
                f"未知的生成类型：{self.task_kind!r}"
                f"（本包只消费 {sorted(TASK_KINDS)}）。"
            )

        # `batch_kind` 的取值域是 WP24 的 TRIAL/SCALE/BULK —— **本处不复制
        # 那份名单**（那是第二个真值来源），只判"给了一个能用的标量"；
        # 域校验由 `estimate_cost` → `validate_quantity` 承担。
        if not isinstance(self.batch_kind, str) or not self.batch_kind.strip():
            raise ValueError(
                f"{REQUEST_KEY_BATCH_KIND} 必须是非空字符串，"
                f"实际是 {self.batch_kind!r}。"
                "（它的受控取值域是 WP24 的 TRIAL/SCALE/BULK，"
                "校验由 `estimate_cost` → `validate_quantity` 承担）"
            )

        if not isinstance(self.model_profile_id, uuid.UUID):
            raise ValueError(
                f"model_profile_id 必须是 uuid.UUID 实例，"
                f"实际是 {type(self.model_profile_id).__name__}。"
            )
        if self.media_form not in MEDIA_FORMS:
            raise ValueError(
                f"{REQUEST_KEY_MEDIA_FORM} 未知：{self.media_form!r}"
                f"（本包只认 {list(MEDIA_FORMS)}）。"
            )

    @property
    def task_kinds(self) -> tuple[str, ...]:
        """本请求实际要跑的 `task_kind`（本包恒为单元素）。"""
        return task_kinds_for_request()

    def as_payload(self) -> dict[str, Any]:
        """落库形状。`model_profile_id` 转 `str` —— JSONB 不认 UUID 对象。

        **恒写出四键**（含 `STATIC`）：缺省只是**读侧**对旧行的宽容，
        写侧不省键 —— 否则新行里"静态"与"键被写坏"在读者眼里同形。
        """
        return {
            REQUEST_KEY_TASK_KIND: self.task_kind,
            REQUEST_KEY_BATCH_KIND: self.batch_kind,
            REQUEST_KEY_MODEL_PROFILE_ID: str(self.model_profile_id),
            REQUEST_KEY_MEDIA_FORM: self.media_form,
        }

    @classmethod
    def from_payload(cls, raw: Any) -> MediaGenerationRequest:
        """解析落库形状。**任何一处不对即抛**（fail-closed）。

        **必需三键不给默认值**：三个字段各有"看起来合理的默认"，
        而每个默认值都会把一次**真实的装配故障**变成一次**静默的错误行为**：

        | 缺哪个 | 若给默认 | 后果 |
        |---|---|---|
        | `taskKind` | `STATIC_IMAGE` | 现在是同值，但将来多一种 kind 时，旧行的语义会被**改成新的那一种** |
        | `batchKind` | `TRIAL` | 请求说扩产 30 条，预占按试产 10 条算 ⇒ 撞 G3 上界时才发现，或干脆多花 |
        | `modelProfileId` | 任一档案 | 用了**别的**模型的价钱与策略 |

        **`mediaForm` 是唯一例外（WP28）**：缺键 ⇒ `STATIC`。理由是它的缺省值
        不是"猜的"而是**历史事实**——WP27 全部落库行都是静态图，
        缺这一键恰好就是"WP28 之前写的行"；而显式写了未知取值仍由构造器拒绝。

        抛 `ValueError` 而不是 `ApiError`：本函数在**处理器**侧被调，
        那里没有响应可组装；处理器把 `ValueError` 折成一个
        **不可重试**的任务失败（重试不会让参数长出来）。

        本函数只判**形状**（是不是对象 / 必需键齐不齐 / 多不多键），
        类型与取值域全部交给构造器 —— 归一与校验各只有一处。
        """
        if not isinstance(raw, dict):
            raise ValueError(
                f"generation_request 必须是 JSON 对象，实际是 {type(raw).__name__}。"
                "（这条拒掉的是『参数根本没写进去』—— 端点用了旧代码路径，"
                "或有人手工插了媒体行）"
            )
        missing = MEDIA_REQUEST_REQUIRED_KEYS - raw.keys()
        if missing:
            raise ValueError(
                f"generation_request 缺键：{sorted(missing)}。"
                f"应有必需三键：{sorted(MEDIA_REQUEST_REQUIRED_KEYS)}"
                f"（可选 {REQUEST_KEY_MEDIA_FORM}）。"
                "（不给默认值是刻意的：默认值会把装配故障变成静默的错误行为）"
            )
        extra = raw.keys() - MEDIA_REQUEST_KEYS
        if extra:
            raise ValueError(
                f"generation_request 有多余键：{sorted(extra)}。"
                "（拒多余键与 `schemas.py` 的做法一致：多一个键说明"
                "写入方与读取方对形状的理解已经分叉）"
            )

        profile_raw = raw[REQUEST_KEY_MODEL_PROFILE_ID]
        if not isinstance(profile_raw, str):
            raise ValueError(
                f"{REQUEST_KEY_MODEL_PROFILE_ID} 必须是字符串"
                f"（JSONB 里存不下 UUID 对象），实际是 {type(profile_raw).__name__}。"
            )
        try:
            profile_id = uuid.UUID(profile_raw)
        except ValueError as exc:
            raise ValueError(
                f"{REQUEST_KEY_MODEL_PROFILE_ID} 不是合法 UUID：{profile_raw!r}。"
            ) from exc

        return cls(
            task_kind=raw[REQUEST_KEY_TASK_KIND],
            batch_kind=raw[REQUEST_KEY_BATCH_KIND],
            model_profile_id=profile_id,
            media_form=raw.get(REQUEST_KEY_MEDIA_FORM, MEDIA_FORM_STATIC),
        )


@dataclass(frozen=True, slots=True)
class ReferenceAssetView:
    """一条产品参考素材的不可变视图。"""

    id: uuid.UUID
    project_id: uuid.UUID
    file_object_id: uuid.UUID
    state: str
    product_model: str | None
    product_color: str | None
    product_part: str | None
    source: str | None
    note: str | None
    supersedes_asset_id: uuid.UUID | None
    created_at: datetime
    created_by: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class RenditionView:
    """一条加工产物的不可变视图。"""

    id: uuid.UUID
    media_version_id: uuid.UUID
    kind: str
    state: str
    file_object_id: uuid.UUID | None
    width: int | None
    height: int | None
    byte_size: int | None
    image_format: str | None
    note: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MediaVersionSummary:
    """媒体版本历史项（`GET /media/{mediaId}` 的版本段）。

    **不给 renditions / payload**：那是详情端点的主体内容，
    版本历史是"挑一版去看"，把全部 rendition 乘上版本数拉回来是
    与 `ContentSummary` 同一理由的复用（列表不拉重东西）。
    """

    id: uuid.UUID
    version_no: int
    paragraph_ref: str
    sort_order: int
    image_file_id: uuid.UUID
    generation_context_id: uuid.UUID | None
    payload_hash: str
    schema_version: int
    created_at: datetime
    created_by: uuid.UUID | None
    #: 版本形态（`STATIC` / `LIVE`）。放在最后并带缺省 ⇒ 既有静态构造点零改动。
    media_form: str = MEDIA_FORM_STATIC


@dataclass(frozen=True, slots=True)
class ComponentView:
    """Live 组的一个组件（`media_components` 行）的不可变视图。

    `verification` 恒为 `SYNTHETIC`（红线：未经真机验证的产出不得标「已验证」），
    读详情时原样带出 ⇒ 前端能显式展示"这是合成样本"。
    """

    id: uuid.UUID
    media_version_id: uuid.UUID
    role: str
    file_id: uuid.UUID
    verification: str
    sha256: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class MediaSummary:
    """一条媒体的汇总（列表项；不含版本历史与 rendition 明细）。

    ## ★ `paragraph_ref` 为什么是字符串而不是 `paragraph_index`

    R4 的原话：「对齐必须落成显式事实：`media_versions.paragraph_ref`
    （段落稳定标识，**不是下标**）」。

    用下标的问题：段落增删/重排后，所有图会**整体错位**，
    而"重做第 3 段"不知道该重做哪张图 —— 这是 ISS-52
    （"按某个恰好存在的列排序不是排序"）的同款：
    **顺序必须是显式落库的事实**。

    `paragraph_ref` 由 `service.py` 在**生成时**从正文段落派生
    （WP26 的 `content_versions.payload.paragraph_plan` 里那一段的
    稳定标识；派生函数只有一处，见 `paragraph_ref_for`）。
    """

    id: uuid.UUID
    content_id: uuid.UUID
    project_id: uuid.UUID
    state: str
    paragraph_ref: str
    sort_order: int
    current_version_id: uuid.UUID | None
    current_version_no: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MediaDetail:
    """单条媒体详情：当前版本 + 版本历史 + rendition + 素材绑定。

    `payload` 是**原始 dict**（不是某个值类型）—— 它的形状由
    `MEDIA_SCHEMA_VERSION` 定义，而"读出来是什么形状"这件事
    由版本号决定，不由 Python 类型决定。
    """

    id: uuid.UUID
    content_id: uuid.UUID
    project_id: uuid.UUID
    state: str
    paragraph_ref: str
    sort_order: int
    current_version_id: uuid.UUID | None
    current_version_no: int
    payload: dict[str, Any] | None
    reference_assets: tuple[dict[str, Any], ...]
    renditions: tuple[RenditionView, ...]
    versions: tuple[MediaVersionSummary, ...]
    #: 当前版本形态；无版本时为 `STATIC`（尚未生成，按默认形态呈现）。
    media_form: str = MEDIA_FORM_STATIC
    #: 当前版本的 Live 组件（静态版本恒为空元组）。
    components: tuple[ComponentView, ...] = ()


# ---------------------------------------------------------------------------
# 解析 / 计算辅助（纯函数，供各层复用）
# ---------------------------------------------------------------------------


def paragraph_ref_for(paragraph_index: int, paragraph_text: str) -> str:
    """由"段序号 + 段文本"派生一个**稳定的段落标识**（R4 的唯一落点）。

    形状：`"p{index}-{hash8}"`，其中 `hash8` 是段文本规范化后的
    前 8 位十六进制。

    ## 为什么带文本哈希

    只用序号（`p3`）的话，"第 3 段被删了、原来的第 4 段变成第 3 段"之后，
    原先绑在 `p3` 上的图会**静默改指**到另一段的内容 —— 而它画的还是旧内容，
    于是"图与正文段落对应"这个前提**已经破了**，系统却不知道。

    带上文本哈希之后，同样的重排会让那张图的 `paragraph_ref` **不再匹配
    任何当前段落** ⇒ 可判为"需要重做"（`REASON_PARAGRAPH_REF_UNKNOWN`），
    而不是静默错位。

    ## 为什么不做成"只哈希文本"

    因为文档顺序在**生成那一刻**是有意义的（demo 的"6 张图均对应正文段落"
    隐含顺序），而纯哈希会丢掉"这是第几段"。两者都要，故拼在一起。

    ## 归一化的口径

    只做两件事：去首尾空白、把内部连续空白折成单个空格。
    **不做大小写折叠**：中文没有大小写，而英文段落的大小写是有意义的。
    归一化口径必须**只有一处** —— 两处（生成时、校验时）不一致的表现是
    "刚生成的图，`paragraph_ref` 立刻被判为未知"。
    """
    if not isinstance(paragraph_index, int) or isinstance(paragraph_index, bool):
        raise ValueError(
            f"段序号必须是整数，实际是 {type(paragraph_index).__name__}。"
        )
    if paragraph_index < 0:
        raise ValueError(f"段序号不得为负：{paragraph_index}。")
    if not isinstance(paragraph_text, str):
        raise ValueError(
            f"段文本必须是字符串，实际是 {type(paragraph_text).__name__}。"
        )
    normalized = " ".join(paragraph_text.split())
    return f"p{paragraph_index}-{fragment_hash8(normalized)}"


def fragment_hash8(text: str) -> str:
    """短哈希（8 位十六进制）。

    **复用 WP11 的哈希口径**（`versions/service.py::fragment_hash` 同族），
    不引入第二种算法 —— 两套哈希在同一条数据上给出不同值，
    而"哪个才对"没有答案（本仓已在 ISS-137 踩过"同一事实两种写法"）。
    """
    from rednote.modules.versions.service import fragment_hash

    return fragment_hash(text)[:8]


def parse_paragraph_ref(ref: str) -> tuple[int, str]:
    """把 `paragraph_ref` 拆回 `(段序号, 哈希)`。

    拆不开即抛 —— 因为 `paragraph_ref` 是**由本模块唯一函数生成**的，
    拆不开说明数据被外部写坏了（手工插库 / 旧版本格式）。
    静默返回一个默认值会让"错的绑定"被当成"对的绑定"。
    """
    if not isinstance(ref, str) or not ref:
        raise ValueError(f"paragraph_ref 必须是非空字符串，实际是 {ref!r}。")
    if not ref.startswith("p") or "-" not in ref:
        raise ValueError(
            f"paragraph_ref 形状不合法：{ref!r}（应为 p<序号>-<8位哈希>）。"
            "（它只由 `paragraph_ref_for` 生成；拆不开说明数据被外部写坏）"
        )
    head, _, tail = ref.partition("-")
    index_part = head[1:]
    if not index_part.isdigit() or len(tail) != 8:
        raise ValueError(
            f"paragraph_ref 形状不合法：{ref!r}（应为 p<序号>-<8位哈希>）。"
        )
    return int(index_part), tail


__all__ = [
    "ASSET_STATES",
    "ASSET_STATE_ACTIVE",
    "ASSET_STATE_LIST_SQL",
    "ASSET_STATE_RETIRED",
    "BINDING_KEYS",
    "BINDING_KEY_ASSET_ID",
    "BINDING_KEY_NOTE",
    "BINDING_KEY_PART",
    "CAPABILITY_STATIC_IMAGE",
    "DATA_CLASS_STATIC_IMAGE",
    "EGRESS_DATA_CLASSES",
    "GENERATION_CONTEXT_LIST_SQL",
    "IDEMPOTENCY_SCOPE_MEDIA_GENERATE",
    "IMAGES_PER_CONTENT",
    "KIND_MEDIA_GENERATE",
    "MAX_ASSET_NOTE_CHARS",
    "MAX_ASSET_SOURCE_CHARS",
    "MAX_CONTENT_COUNT",
    "MAX_FORMAT_CHARS",
    "MAX_IMAGE_REF_CHARS",
    "MAX_IMAGES_PER_REQUEST",
    "MAX_MIME_TYPE_CHARS",
    "MAX_PARAGRAPH_REF_CHARS",
    "MAX_PRODUCT_COLOR_CHARS",
    "MAX_PRODUCT_MODEL_CHARS",
    "MAX_PRODUCT_PART_CHARS",
    "MAX_PROMPT_CHARS",
    "MAX_REFERENCE_ASSETS",
    "MAX_REGENERATION_REASON_CHARS",
    "MAX_RENDITION_NOTE_CHARS",
    "MAX_STORAGE_KEY_CHARS",
    "MEDIA_REQUEST_KEYS",
    "MEDIA_REQUEST_REQUIRED_KEYS",
    "MEDIA_SCHEMA_VERSION",
    "MEDIA_STATES",
    "MEDIA_STATE_FAILED",
    "MEDIA_STATE_INITIAL",
    "MEDIA_STATE_LIST_SQL",
    "MEDIA_STATE_PENDING",
    "MEDIA_STATE_READY",
    "MIN_REFERENCE_ASSETS",
    "MIN_SORT_ORDER",
    # 三条 CHECK 片段**登记为公开面**（`models.py` 与两个迁移都 import 它们）。
    # ⚠️ 这不是风格问题：本仓有 `tools/quality/check_all_exports.py`
    # 守"已列出的必须存在"（A1），而"漏登记"方向只作诊断输出
    # （那一边设计上不设硬门禁，见该脚本 docstring）。
    # ⇒ 漏登记的代价不是判据变红，而是**下一个消费者找不到这个唯一来源**
    # 而就地手写一份 SQL 字面量（`check_migration_model_parity.py`
    # 只比列、**比不到 CHECK 文本** ⇒ 分叉无判据会红）。
    "PENDING_BINDINGS_IS_ARRAY_SQL",
    "PENDING_BINDINGS_SIZE_SQL",
    "PENDING_REGENERATION_REASON_SQL",
    "QUEUE_MEDIA",
    "REASON_CONTENT_NOT_FOUND",
    "REASON_CONTENT_VERSION_NOT_FOUND",
    "REASON_GATEWAY_DENIED",
    "REASON_GATEWAY_NOT_SUCCEEDED",
    "REASON_GATEWAY_NO_IMAGE_REF",
    "REASON_GATEWAY_NO_OUTPUT",
    "REASON_GENERATION_REQUEST_INVALID",
    "REASON_IDEMPOTENCY_CONFLICT",
    "REASON_MEDIA_FORM_CONFLICT",
    "REASON_MEDIA_EMPTY",
    "REASON_MEDIA_FORMAT_UNSUPPORTED",
    "REASON_MEDIA_MALFORMED",
    "REASON_MEDIA_NOT_FOUND",
    "REASON_MEDIA_TOO_LARGE",
    "REASON_MEDIA_TOO_SMALL",
    "REASON_MEDIA_VERSION_NOT_FOUND",
    "REASON_MEDIA_ITEM_NOT_FOUND",
    "REASON_MODEL_PROFILE_NOT_FOUND",
    "REASON_PARAGRAPH_REF_MISSING",
    "REASON_PARAGRAPH_REF_UNKNOWN",
    "REASON_PROJECTION_CONFLICT",
    "REASON_PROJECT_NOT_FOUND",
    "REASON_PROMPT_NOT_ACTIVATED",
    "REASON_PROMPT_TEMPLATE_MISSING",
    "REASON_REFERENCE_ASSET_MISMATCH",
    "REASON_REFERENCE_ASSET_NOT_ACTIVE",
    "REASON_REFERENCE_ASSET_NOT_FOUND",
    "REASON_REFERENCE_BINDING_CROSS_PROJECT",
    "REASON_REFERENCE_BINDING_DUPLICATE",
    "REASON_REFERENCE_BINDING_MALFORMED",
    "REASON_REFERENCE_BINDING_MISSING",
    "REASON_REFERENCE_BINDING_TOO_MANY",
    "REASON_RENDITION_KIND_UNKNOWN",
    "REASON_TASK_KIND_UNKNOWN",
    "RENDITION_DELIVERY",
    "RENDITION_KINDS",
    "RENDITION_KIND_LIST_SQL",
    "RENDITION_PREVIEW",
    "RENDITION_STATES",
    "RENDITION_STATE_FAILED",
    "RENDITION_STATE_INITIAL",
    "RENDITION_STATE_LIST_SQL",
    "RENDITION_STATE_PENDING",
    "RENDITION_STATE_READY",
    "REQUEST_KEY_BATCH_KIND",
    "REQUEST_KEY_MEDIA_FORM",
    "REQUEST_KEY_MODEL_PROFILE_ID",
    "REQUEST_KEY_TASK_KIND",
    "SORT_ORDER_COVER",
    "TASK_KINDS",
    # WP28：媒体形态 / Live 组件 / 校验口径（唯一来源，models/ddl/迁移都 import）。
    "COMPONENT_ROLES",
    "ComponentView",
    "COMPONENT_ROLE_LIST_SQL",
    "COMPONENT_ROLE_MANIFEST",
    "COMPONENT_ROLE_PAIRED_MOTION",
    "COMPONENT_ROLE_PREVIEW",
    "COMPONENT_ROLE_STILL",
    "LIVE_GROUPS_PER_CONTENT",
    "LIVE_IMAGES_PER_CONTENT",
    "LIVE_PROFILE_PLACEHOLDER",
    "LIVE_PROFILE_STATES",
    "LIVE_PROFILE_STATE_LIST_SQL",
    "MAX_LIVE_PROFILE_KEY_CHARS",
    "MAX_LIVE_PROFILE_NOTE_CHARS",
    "MEDIA_FORMS",
    "MEDIA_FORM_LIST_SQL",
    "MEDIA_FORM_LIVE",
    "MEDIA_FORM_STATIC",
    "VERIFICATIONS",
    "VERIFICATION_LIST_SQL",
    "VERIFICATION_SYNTHETIC",
    "TASK_STATIC_IMAGE",
    "MediaDetail",
    "MediaGenerationRequest",
    "MediaReferenceBinding",
    "MediaSummary",
    "MediaVersionSummary",
    "ReferenceAssetView",
    "RenditionView",
    "calls_for",
    "fragment_hash8",
    "images_for",
    "images_per_content",
    "paragraph_ref_for",
    "parse_paragraph_ref",
    "task_kinds_for_request",
]