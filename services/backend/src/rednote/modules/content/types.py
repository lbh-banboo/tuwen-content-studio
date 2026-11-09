"""WP26 的不可变值类型与受控取值域。

**为什么单独一个模块**（与 WP11/WP16/WP20/WP23/WP24 同一手法）：这些取值同时被
`models.py`（CHECK 约束）、`schemas.py`（纯函数校验）、`repository.py`（存储）
与 `service.py`（编排）使用。定义在 service 里会让其它几个反向导入 service ——
而本模块的分层刻意是单向的：

    types ← schemas
    types ← models
    types ← repository
    types ← prompts_bridge
    types ← billing_bridge
    types ← gateway_bridge
    types ← service

**跨层传递的值用 frozen dataclass 而不是 ORM 对象或裸 dict**（ORM 实例绑在
session 上，session 一关就 `DetachedInstanceError`；裸 dict 则没有取值域，
写错键名不报错）。

---

## 本模块承载的三条硬性质

| 性质 | 本模块的落点 |
|---|---|
| **调用次数口径唯一**（R3） | `calls_for()` —— **全包唯一的算法**。两处各写一遍会算出两个预占数，而账面永远对不上 |
| **取值必须受控**（R1） | `CONCEPTION_STRENGTHS` / `REGENERATION_SCOPES` / `TITLE_SCORE_DIMENSIONS` —— 未知取值**拒绝**而不是放行 |
| **段落级证据绑定**（R4） | `ParagraphBinding` 的三元组 `(index, claim_id, text)`，`claim_id is None` 必须显式理由 —— 不允许"整篇引用整份基线" |

## 为什么把"调用次数"的口径放在这里，而不是在 `billing_bridge` 里

预占金额 = 调用次数 × 单价。这个"次数"若有两个来源（生成入口算一次、
重做入口再算一次），两者必然分叉，而分叉的表现是**钱**，不是报错。
分叉的典型演化：一处算"内容条数"、另一处算"内容条数 × 2"，
于是局部重做只预占一半的钱 —— 账面看起来正常，直到对账。

因此本模块只留**一个**函数（`calls_for`），且它的入参是**受控的** `task_kind`：
传入未知 kind 抛 `ValueError`，而不是 `.get(kind, 0)` 那样静默算成 0 次
（"0 次"会让预占金额为 0，而 `reserve_budget` 拒绝非正数 ⇒ 报错位置离根因很远）。

## ⚠️ `TASK_CONTENT_STRUCTURE` 与 `TASK_CONTENT_COPY` 为什么各算 1 次

两者是**两次独立调用**（不是一次调用出全部）。理由见说明文档 §1.3：
"只改标题"若要走整篇重做，会从 ≈9 千 tok 涨到 ≈86 千 tok。

**但"标题候选"与"正文"在**同一次**调用内** —— demo 把它们分开计价是因为
token 量不同（≈9 千 vs 正文那部分），**不是因为调用两次**。这条最容易写错：
若把标题候选也拆成第三次调用，`calls_for` 会多算一次，而用户看到的账单
会多一份。**唯一口径就是本模块这个函数**，改它必须同时改说明文档 §2.4 第 6 项。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

# ---------------------------------------------------------------------------
# 两个 task_kind：本包消费的取值域
# ---------------------------------------------------------------------------
# **为什么不从 `prompts.types` 顶层 import**：这是本包刻意的取舍，理由与
# `brief/decomposer.py` 用字符串字面量写 `_CAPABILITY_TEXT_STRUCTURED` 不同 ——
# `prompts` 是**轻模块**（只有 types/models/render/tools_policy，不 import
# 适配器与策略树），顶层 import 它的常量不会拖着依赖树。
#
# **真正的原因是别的**：本模块的这两个常量是**本包对外承诺的取值**，
# 而 `prompts.types.TASK_CONTENT_STRUCTURE` 是**提示词模块**的取值域。
# 两者当前同值，但它们**允许不同步**（提示词模块将来可能加 `CONTENT_REVIEW`
# 之类的第三种 kind，而本包只消费两种）。
# 若此处直接用 `prompts.types` 的常量，一次提示词侧的取值域扩张会让
# `calls_for()` 的 `TASK_KINDS` 判断**静默多接受一个 kind**，
# 而 `calls_for` 对它的分支是 `raise ValueError` ⇒ 变成"改提示词导致内容
# 生成报错"，且报错点与根因隔了两个模块。
#
# 因此这里写**本包自己的常量**，并由 `test_content_wp26.py` 里的
# `test_task_kind_constants_match_prompts` 断言"两者当前同值" ——
# 同步这件事**由判据守，不由 import 守**。若将来真的漂移，那条判据会红，
# 逼人做一次显式决定（而不是让行为悄悄改变）。
TASK_CONTENT_STRUCTURE: Final[str] = "CONTENT_STRUCTURE"
TASK_CONTENT_COPY: Final[str] = "CONTENT_COPY"

#: 本包消费的两个 task_kind（`calls_for()` 的受控入参域）。
TASK_KINDS: Final[frozenset[str]] = frozenset(
    {TASK_CONTENT_STRUCTURE, TASK_CONTENT_COPY}
)

#: 每次调用产出的对象数。**两个都是 1**，理由见模块 docstring 最后一节。
TASK_CALLS_PER_ITEM: Final[dict[str, int]] = {
    TASK_CONTENT_STRUCTURE: 1,
    TASK_CONTENT_COPY: 1,
}


# ---------------------------------------------------------------------------
# 重做范围（demo 的五个按钮）
# ---------------------------------------------------------------------------
# demo `content-feedback.html` 有五个按钮："仅标题 / 仅正文 / 仅标签 / 单张媒体 /
# 整篇"。本包只做**前三个 + 整篇**，媒体那一项归 WP27/28（见说明文档 §1.5）。
#
# **"仅标签"为什么也在本包里**：标签是 `TASK_CONTENT_COPY` 的输出之一，
# 不是独立调用。若落成独立的 scope + 独立的 task_kind，就需要
# WP23 定义第三种 kind —— 而 WP23 只定义两种，本包不擅自扩它的取值域。
# 处置：`SCOPE_TAGS` 接受请求，但**在 service 层折成 `SCOPE_COPY`**
# （重做正文时标签一起重做），并在响应里如实回填"实际重做的范围"。
# 登记为本包取舍（说明文档 §2.4 第 7 项）。
SCOPE_TITLE: Final[str] = "TITLE"
SCOPE_COPY: Final[str] = "COPY"
SCOPE_TAGS: Final[str] = "TAGS"
SCOPE_FULL: Final[str] = "FULL"

REGENERATION_SCOPES: Final[frozenset[str]] = frozenset(
    {SCOPE_TITLE, SCOPE_COPY, SCOPE_TAGS, SCOPE_FULL}
)

REGENERATION_SCOPE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(REGENERATION_SCOPES)
)

#: scope → 实际会跑的 `task_kind` 元组。**顺序有意义**（结构在前）。
#:
#: | scope | 实际跑什么 |
#: |---|---|
#: | `TITLE` | 只跑 `CONTENT_COPY`（标题候选是它的输出之一） |
#: | `COPY` | 只跑 `CONTENT_COPY` |
#: | `TAGS` | 只跑 `CONTENT_COPY`（折成 COPY，见上） |
#: | `FULL` | `CONTENT_STRUCTURE` **然后** `CONTENT_COPY`（结构必须在前，它产出文案的输入） |
#:
#: **`TITLE` 为什么不重跑结构**：蓝图是**不可变的历史**，且标题的质量
#: 只取决于提示词与蓝图 —— 蓝图没变就不必再算一遍（那正是说明文档 §1.3
#: 表格里"每次多花 ¥154.80 而不是 ¥16.20"要避免的事）。
SCOPE_TASK_KINDS: Final[dict[str, tuple[str, ...]]] = {
    SCOPE_TITLE: (TASK_CONTENT_COPY,),
    SCOPE_COPY: (TASK_CONTENT_COPY,),
    SCOPE_TAGS: (TASK_CONTENT_COPY,),
    SCOPE_FULL: (TASK_CONTENT_STRUCTURE, TASK_CONTENT_COPY),
}


# ---------------------------------------------------------------------------
# 生成状态：内容条目 / 内容版本
# ---------------------------------------------------------------------------
# **为什么需要状态列**：生成是异步的（经 WP13 任务框架），"这一条生成到
# 哪一步了"必须可查。缺了它，前端只能靠"有没有版本行"猜 —— 而
# "结构成功、文案失败"这个中间态**恰好是"有版本行但内容不完整"**，
# 靠猜就会显示成"已完成"。
CONTENT_STATE_DRAFT: Final[str] = "DRAFT"
CONTENT_STATE_STRUCTURED: Final[str] = "STRUCTURED"
CONTENT_STATE_GENERATED: Final[str] = "GENERATED"
CONTENT_STATE_FAILED: Final[str] = "FAILED"

CONTENT_STATES: Final[frozenset[str]] = frozenset(
    {
        CONTENT_STATE_DRAFT,
        CONTENT_STATE_STRUCTURED,
        CONTENT_STATE_GENERATED,
        CONTENT_STATE_FAILED,
    }
)

CONTENT_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(CONTENT_STATES)
)


# ---------------------------------------------------------------------------
# 生成上下文的 task_kind（OP-160 的落点）
# ---------------------------------------------------------------------------
# `generation_contexts` 每次生成落一行，钉住"这次用的是哪版提示词 / 哪个基线 /
# 哪个模型档案"。**取值域与 `TASK_KINDS` 同源**（故意）：上下文行的
# `task_kind` 就是"这次跑的是哪一种"，多一种就说明本包多消费了一个 kind，
# 而 `calls_for` 会因此抛错 —— 两处同时变才可能一致。
GENERATION_CONTEXT_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(TASK_KINDS)
)


# ---------------------------------------------------------------------------
# 蓝图 9 字段：结论强度是唯一的受控枚举
# ---------------------------------------------------------------------------
# 9 个字段里 **8 个是自由文本**（形状由 `schemas.py` 逐字段判"非空字符串 +
# 长度上界"），只有 `conclusion_strength` 是**受控枚举** —— 因为它是
# "这句话说得多满"的量度，而**"说得多满"决定了它需要多强的证据**
# （说明文档 §1.6 R10：模型天然会补全与夸大）。
#
# 自由文本落 CHECK 会造出"把一份提示词的措辞变成未来硬约束"的问题
# （§1.5 砍掉清单第 3 条的同一取向）；而枚举不落 CHECK 就等于没有约束。
# 因此：**文本字段用服务层校验，枚举字段用 DDL CHECK**。
# 三档**语义不重叠**：
#   · `DIRECT`       —— 直接断言（有基线事实直接支撑时用）
#   · `CONSERVATIVE` —— 保守表述（有事实但只覆盖部分场景）
#   · `HEDGED`       —— 明确留有余地（事实缺失、只有产品说明时用）
#
# **为什么不是"弱/中/强"这类有重叠感的形容词**：取值要在数据里被看见，
# 而"ASSERTIVE 与 CONSERVATIVE 之间是什么"没有答案。
# `HEDGED` 与 `CONSERVATIVE` 的边界是**可解释的**："这条内容是否明确
# 承认自己不知道"。三档各自能说出一句判据，才是可用的枚举。
STRENGTH_DIRECT: Final[str] = "DIRECT"
STRENGTH_CONSERVATIVE: Final[str] = "CONSERVATIVE"
STRENGTH_HEDGED: Final[str] = "HEDGED"

CONCLUSION_STRENGTHS: Final[frozenset[str]] = frozenset(
    {STRENGTH_DIRECT, STRENGTH_CONSERVATIVE, STRENGTH_HEDGED}
)

CONCLUSION_STRENGTH_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(CONCLUSION_STRENGTHS)
)

#: 9 个蓝图字段的**列名清单**（唯一来源）。
#:
#: `schemas.py` 的校验器按它逐字段检查，`models.py` 的列名与它一致，
#: 而 `test_content_wp26.py` 断言三者集合相等 ——
#: 今天"校验器少查一个字段"不会有任何报错（模型少填那个字段照样入库），
#: 只有这条对账能发现。
BLUEPRINT_FIELDS: Final[tuple[str, ...]] = (
    "opening_function",
    "context",
    "decision_process",
    "usage_scene",
    "evidence_beats",
    "limitation",
    "conclusion_strength",
    "interaction_question",
    "paragraph_plan",
)

#: 文案三件的**键名清单**（唯一来源）。
COPY_FIELDS: Final[tuple[str, ...]] = ("title", "body", "tags")

#: `content_versions.bindings` 每个元素的**线上键名**（camelCase）。
#:
#: ★ 形状由 `models.py` 的列注释定义（那里是唯一权威）：
#: `[{"paragraphIndex": 0, "claimId": "<uuid>"|null, "reason": "...", "text": "..."}]`
#: —— 与 WP24 的 `plan_drafts.scene_items`（`weightPermille` / `sortOrder` /
#: `disabledReason`）同款：**JSONB 内部的结构用线上键名（camelCase）**。
#:
#: 常量放在 `types.py` 而不是 `service.py`：`bindings` 有**两个**读写点
#: （`service.py` 落库、`api.py` 响应模型），两处各写一份字面量就是
#: ISS-66 形态 —— 一处写 `paragraphIndex`、另一处写 `paragraph_index`
#: 的表现是"存进去读不出来"，而且两边的测试**各自都过**。
#:
#: 与 `BLUEPRINT_FIELDS` 的区别：那是**列名**（snake_case，`insert_blueprint`
#: 的入参键），这里是**JSONB 内部的线上键名**（camelCase，直接进响应体）。
#: 两者**刻意不统一**：`bindings` 是 JSONB（无列可依），蓝图是 9 个真列。
BINDING_KEY_PARAGRAPH_INDEX: Final[str] = "paragraphIndex"
BINDING_KEY_CLAIM_ID: Final[str] = "claimId"
BINDING_KEY_REASON: Final[str] = "reason"
BINDING_KEY_TEXT: Final[str] = "text"

#: `bindings` 元素允许的键 —— **白名单**，多出来的键一律拒绝。
#: 与 WP24 的 `_ALLOWED_ITEM_KEYS` 同一取向：不做的话，一行里塞进
#: `{"claimId": "…", "claimID": "…"}` 也照收，而写的一方以为改了后者。
BINDING_KEYS: Final[frozenset[str]] = frozenset(
    {
        BINDING_KEY_PARAGRAPH_INDEX,
        BINDING_KEY_CLAIM_ID,
        BINDING_KEY_REASON,
        BINDING_KEY_TEXT,
    }
)


# ---------------------------------------------------------------------------
# 标题候选：数量上下界 + 6 维评分名
# ---------------------------------------------------------------------------
# **数量上下界落在本包，具体值待产品拍板**（说明文档 §2.4 第 4 项、
# 附录 A3）。demo 显示"2—3 个"、历史文档显示"12 个收敛到 3 个"。
#
# 本包给的是**可判的边界**而不是一个魔数：`MIN = 1`（至少得有一个候选，
# 否则"采纳一个标题"无从谈起）、`MAX = 12`（历史文档的上界，
# 且超过 12 个候选的 6 维评分表没有人会看完）。
# **`MIN` 取 1 而不是 2**：demo 的"2—3 个"是产品期望，不是硬约束；
# 落成 2 会让"模型只给了 1 个候选"变成生成失败，而那个候选本身可能是好的。
MIN_TITLE_CANDIDATES: Final[int] = 1
MAX_TITLE_CANDIDATES: Final[int] = 12

#: 标题评分 6 维（说明文档 §2.4 第 3 项）。
#:
#: **只存分数与维度名，不定义"多少分算好"** —— 那是产品与 AI 的口径
#: （附录 A4）。因此这里**没有** `TITLE_SCORE_MIN`/`MAX` 常量：
#: 一个写死的"0~100"会让"这一维用了 1~5 分制"变成静默的错数据，
#: 而没有任何判据会发现（量纲这件事本仓已在 ISS-137 踩过一次同族形态）。
TITLE_SCORE_DIMENSIONS: Final[tuple[str, ...]] = (
    "search_match",
    "clarity",
    "audience_relevance",
    "title_body_consistency",
    "freshness",
    "risk",
)

#: 6 维评分的键名集合（供 `schemas.py` 逐维检查"6 维齐全"）。
TITLE_SCORE_DIMENSION_SET: Final[frozenset[str]] = frozenset(
    TITLE_SCORE_DIMENSIONS
)


# ---------------------------------------------------------------------------
# 长度上界
# ---------------------------------------------------------------------------
# **为什么每个上界都要显式写出来**：这些值会进 `content_versions.payload`
# （JSONB）与错误消息。允许任意长度等于让一个 1MB 的字符串撑进版本行 ——
# 而 `content_versions` 是**不可变**的（触发器拦 UPDATE/DELETE），
# 一旦写进去就删不掉，同族的 `runtime_settings` 用同一理由设过
# `MAX_CHANGE_REASON_CHARS`（WP24 types.py）。
MAX_TITLE_CHARS: Final[int] = 200
#: `content_items.scene_key` / `form_key` 的长度上限（接缝 §5.15）。
#: 取 200：不小于 WP31 草案（200）与 WP32 草案（100）中较大者，
#: 两个下游包的 key 都装得下；planning 的 `scenes[].key` / `forms[].key`
#: 是自由字符串、无受控取值域，所以这里**只做格式校验、不做枚举**。
MAX_SCENE_FORM_KEY_CHARS: Final[int] = 200
MAX_BODY_CHARS: Final[int] = 20_000
MAX_TAG_CHARS: Final[int] = 50
MAX_TAGS: Final[int] = 30
MAX_BLUEPRINT_FIELD_CHARS: Final[int] = 4_000
MAX_PARAGRAPH_CHARS: Final[int] = 4_000
MAX_PARAGRAPHS: Final[int] = 200
MAX_BINDING_REASON_CHARS: Final[int] = 500
MAX_KEYWORD_CHARS: Final[int] = 100
MAX_HIGHLIGHT_CHARS: Final[int] = 300
MAX_RISK_NOTE_CHARS: Final[int] = 500
MAX_REGENERATION_REASON_CHARS: Final[int] = 500
MAX_PROMPT_TEMPLATE_NAME_CHARS: Final[int] = 200

#: 内容版本的 payload schema 版本起始值（与本仓惯例一致，说明文档 §2.4 第 9 项）。
CONTENT_SCHEMA_VERSION: Final[int] = 1


# ---------------------------------------------------------------------------
# 业务错误原因码
# ---------------------------------------------------------------------------
# **为什么要有独立的原因码**：错误响应里的 `message` 是给人读的文案，会变；
# 而 `details.reason` 是机器可判的。测试与前端要断言的是后者。
# 与 WP22/WP23/WP24/WP25 的 `REASON_*` 同一手法。
REASON_CONTENT_NOT_FOUND: Final[str] = "CONTENT_NOT_FOUND"
REASON_BLUEPRINT_NOT_FOUND: Final[str] = "BLUEPRINT_NOT_FOUND"
REASON_PROJECT_NOT_FOUND: Final[str] = "PROJECT_NOT_FOUND"
REASON_TASK_KIND_UNKNOWN: Final[str] = "TASK_KIND_UNKNOWN"
REASON_SCOPE_UNKNOWN: Final[str] = "SCOPE_UNKNOWN"
REASON_SCOPE_REQUIRES_BLUEPRINT: Final[str] = "SCOPE_REQUIRES_BLUEPRINT"
REASON_BASELINE_NOT_READY: Final[str] = "BASELINE_NOT_READY"
REASON_BASELINE_MISMATCH: Final[str] = "BASELINE_PROJECT_MISMATCH"

# --- R6：没有激活提示词版本（fail-closed）----------------------------------
REASON_PROMPT_NOT_ACTIVATED: Final[str] = "PROMPT_NOT_ACTIVATED"
REASON_PROMPT_TEMPLATE_MISSING: Final[str] = "PROMPT_TEMPLATE_MISSING"

# --- R1：结构化输出校验（四类必拒）------------------------------------------
# 四个码**按拒绝原因分开**，而不是一个笼统的 `SCHEMA_INVALID`：
# 排障时要能一眼看出"是空 dict"还是"缺字段"还是"类型错"还是"域外值" ——
# 这四类在 `schemas.py` 里是四条独立的分支，报同一个码会让
# "哪个分支生效了"无法从响应里读出来（与 WP23 的
# `REASON_ALLOWED_TOOLS_INVALID` 拆成三条同一取向）。
REASON_OUTPUT_NOT_OBJECT: Final[str] = "OUTPUT_NOT_OBJECT"
REASON_OUTPUT_FIELD_MISSING: Final[str] = "OUTPUT_FIELD_MISSING"
REASON_OUTPUT_FIELD_TYPE: Final[str] = "OUTPUT_FIELD_TYPE"
REASON_OUTPUT_FIELD_VALUE: Final[str] = "OUTPUT_FIELD_VALUE"

# --- R4：证据绑定 ------------------------------------------------------------
REASON_BINDING_MISSING_REASON: Final[str] = "BINDING_MISSING_REASON"
REASON_BINDING_UNKNOWN_CLAIM: Final[str] = "BINDING_UNKNOWN_CLAIM"
REASON_BINDING_COVERS_ALL_CLAIMS: Final[str] = "BINDING_COVERS_ALL_CLAIMS"

# --- R5 / R8：网关 -----------------------------------------------------------------
REASON_GATEWAY_DENIED: Final[str] = "GATEWAY_DENIED"
REASON_GATEWAY_NOT_SUCCEEDED: Final[str] = "GATEWAY_NOT_SUCCEEDED"
REASON_GATEWAY_NO_OUTPUT: Final[str] = "GATEWAY_NO_OUTPUT"

# --- 标题候选 ----------------------------------------------------------------
REASON_TITLE_CANDIDATE_NOT_FOUND: Final[str] = "TITLE_CANDIDATE_NOT_FOUND"
REASON_TITLE_CANDIDATE_MISMATCH: Final[str] = "TITLE_CANDIDATE_MISMATCH"
REASON_TOO_MANY_TITLE_CANDIDATES: Final[str] = "TOO_MANY_TITLE_CANDIDATES"
REASON_TOO_FEW_TITLE_CANDIDATES: Final[str] = "TOO_FEW_TITLE_CANDIDATES"

REASON_CONTENT_VERSION_NOT_FOUND: Final[str] = "CONTENT_VERSION_NOT_FOUND"
REASON_IDEMPOTENCY_CONFLICT: Final[str] = "IDEMPOTENCY_CONFLICT"

# --- 服务层的业务前置（`service.py` 的三个校验点）----------------------------
# **为什么定义在这里而不是 `service.py`**：这三个码有两个消费点 ——
# `service.py` 抛、`api.py` 翻成 HTTP 状态。两处各写一份字符串就是
# ISS-66 形态，而分叉的表现是"抛了这个码、响应里是另一个码"，
# 前端按码分支的逻辑静默失效（响应仍然 200，只是走了另一条路）。
REASON_MODEL_PROFILE_NOT_FOUND: Final[str] = "MODEL_PROFILE_NOT_FOUND"
REASON_GENERATION_REQUEST_INVALID: Final[str] = "GENERATION_REQUEST_INVALID"
REASON_PROJECTION_CONFLICT: Final[str] = "PROJECTION_CONFLICT"

# --- WP29：人工编辑的并发冲突 ------------------------------------------------
# **这个码刻意不进 `api.py` 的 `_HTTP_BY_REASON`**：编辑端点（WP29）要
# 412 `VERSION_CONFLICT`（协议 §1.2），而那条映射把乐观锁失败统一映成
# 409 `INVALID_TRANSITION`（`REASON_PROJECTION_CONFLICT`，见 `api.py:304`）。
# 因此 `service.append_edited_version` 用它去调
# `platform.concurrency.version_conflict(...)` 并**直接 raise 那个
# `ApiError`** —— 由 `platform` 统一翻成 412，而不是经 `_ERROR_MAP`
# 走 409。两个码分开命名，正是为了让"这一次该 412 还是 409"在调用点是
# 可读的（同一个字符串两处各自解释，就是 ISS-66 形态）。
REASON_EDIT_LOCK_CONFLICT: Final[str] = "CONTENT_LOCK_VERSION_MISMATCH"


# ---------------------------------------------------------------------------
# 值类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParagraphBinding:
    """正文里**一段**与 `brief_claims` 的绑定（R4 的落点）。

    | 字段 | 含义 |
    |---|---|
    | `paragraph_index` | 段序号（0 基，与 `paragraph_plan` 的下标对应） |
    | `claim_id` | 绑定的 claim；`None` 表示"这一段没有可绑的事实" |
    | `reason` | `claim_id is None` 时**必填**；有 claim 时是可选说明 |
    | `text` | 该段的正文原文（供 WP30 自检与 AQ-01 盲审逐段比对） |

    ## ★ 为什么必须**逐段**，而不是"整篇引用整份基线"

    那是 R4 的原始形态：若绑定退化成"这篇内容引用了基线里的全部 claim"，
    那么**每篇的绑定集合都相同** —— 而这个集合与正文内容无关，
    于是"这句话依据哪条证据"这个问题**仍然答不出来**。

    这是"看起来有防线"的典型：`len(bindings) > 0` 会恒真地通过，
    测试也"通过"，而 AQ-01 的盲审仍然只能靠人凭印象。

    **可判的形式**见 `schemas.py::validate_bindings`：
    构造一条正文，断言 **绑定的 claim 数 < 基线全部 claim 数**。
    相等即拒绝（`REASON_BINDING_COVERS_ALL_CLAIMS`）——
    每篇都绑全部 claim，等于没绑。

    ## `claim_id is None` 为什么允许，但必须带理由

    有些段落本来就没有对应事实（过渡句、氛围句、产品说明结构的套话）。
    强行要求每段都绑一条 claim 会逼模型**编造一个绑定** ——
    那比不绑更糟（错误的事实来源）。

    但"没有绑定"必须**显式**：`reason` 为空串或纯空白一律拒绝
    （`REASON_BINDING_MISSING_REASON`）。理由与 WP24 的
    "停用必须有停用原因"同一取向 —— 允许的例外必须留下痕迹，
    否则它就是一个可以随手用、事后查不出的后门。
    """

    paragraph_index: int
    claim_id: uuid.UUID | None
    reason: str | None = None
    text: str = ""


@dataclass(frozen=True, slots=True)
class TitleCandidateDraft:
    """一个标题候选（校验通过、尚未入库的形状）。

    `scores` 是**6 维齐全**的映射（缺一维即拒绝，见 `schemas.py`）。
    用 `dict[str, float]` 而不是 6 个具名字段，是因为量纲由产品定
    （附录 A4）—— 具名会隐含"6 个都是同一量纲"这个尚未确定的假设。
    """

    title: str
    scores: dict[str, float]
    keywords: tuple[str, ...] = ()
    highlight: str = ""
    risk_note: str = ""


@dataclass(frozen=True, slots=True)
class BlueprintDraft:
    """叙事蓝图草稿（校验通过、尚未入库的形状）。

    与 `BLUEPRINT_FIELDS` **一一对应**：9 个字段名就是 dataclass 的
    9 个字段名。`schemas.py` 的校验器按 `BLUEPRINT_FIELDS` 遍历，
    而 dataclass 的字段名由 `test_content_wp26.py` 断言与它相等 ——
    两处必须同源，否则"校验器查了 9 个字段、入库只写 8 个"不会报错。
    """

    opening_function: str
    context: str
    decision_process: str
    usage_scene: str
    evidence_beats: str
    limitation: str
    conclusion_strength: str
    interaction_question: str
    paragraph_plan: str


@dataclass(frozen=True, slots=True)
class GeneratedCopy:
    """一次文案生成的产出（校验通过、尚未入库的形状）。

    `bindings` 是**逐段**绑定（R4）；`title_candidates` 与正文**同属一次
    调用**（见模块 docstring 末节）。
    """

    title: str
    body: str
    tags: tuple[str, ...]
    bindings: tuple[ParagraphBinding, ...] = ()
    title_candidates: tuple[TitleCandidateDraft, ...] = ()


@dataclass(frozen=True, slots=True)
class GenerationContextView:
    """一条生成上下文的不可变视图（OP-160 的影响面分析按它反查）。

    **为什么把 6 个 id 一起返回而不是只给 `prompt_version_id`**：
    OP-160 的原话是"哪个提示词版本被哪些生成消费" —— 要回答它，
    光知道"哪些生成"不够，还要知道**那次生成同时用了哪个基线、哪个模型**。
    否则"改了提示词之后质量变了"与"换了模型之后质量变了"无法区分。
    """

    id: uuid.UUID
    project_id: uuid.UUID
    content_id: uuid.UUID
    content_version_id: uuid.UUID | None
    task_kind: str
    prompt_template_id: uuid.UUID | None
    prompt_version_id: uuid.UUID | None
    baseline_id: uuid.UUID | None
    model_profile_id: uuid.UUID | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ContentSummary:
    """内容列表项（不含正文 —— 列表页不需要把 20KB 正文全拉回来）。"""

    id: uuid.UUID
    project_id: uuid.UUID
    title: str | None
    state: str
    current_version_no: int
    created_at: datetime
    updated_at: datetime
    scene_key: str | None = None
    form_key: str | None = None


@dataclass(frozen=True, slots=True)
class ContentVersionSummary:
    """版本历史项（`GET /contents/{contentId}/versions`）。

    **为什么 `adopted_title_candidate_id` 必须在这里给出来**：
    说明文档 §3 的取舍是"采纳记在 `content_versions` 上，而不给
    `title_candidates` 加一列 `adopted`"，代价是"这个候选有没有被采纳过"
    只能**反查版本**。版本历史正是那个反查面 —— 少给这一列，前端就只能
    拿 `payload`（或 `title`）与候选标题做字符串比较，而那是**猜**：
    两个候选取同一个标题是完全合法的（`title_candidates` 没有标题唯一约束），
    字符串相等**不等于**"被采纳的是它"。

    **不给 `body` / `bindings` / `payload`**：那是详情端点的内容，
    版本历史是"挑一版去看"，把 20KB 正文乘上版本数拉回来是本包
    `ContentSummary` 同一条理由的复用（列表不拉正文）。
    """

    id: uuid.UUID
    version_no: int
    title: str
    blueprint_id: uuid.UUID | None
    adopted_title_candidate_id: uuid.UUID | None
    generation_context_id: uuid.UUID | None
    payload_hash: str
    schema_version: int
    created_at: datetime
    created_by: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class ContentDetail:
    """单条内容详情：当前版本 + 蓝图 + 候选 + 证据绑定 + 生成上下文。

    `payload` 是**原始 dict**（不是某个值类型）—— 因为它的形状由
    `CONTENT_SCHEMA_VERSION` 定义，而"读出来是什么形状"这件事
    由版本号决定，不由 Python 类型决定。写成值类型会让
    "schema_version=2 的行"在 v1 的代码里读不出来。
    """

    id: uuid.UUID
    project_id: uuid.UUID
    state: str
    current_version_id: uuid.UUID | None
    current_version_no: int
    payload: dict[str, Any] | None
    blueprint: dict[str, Any] | None
    title_candidates: tuple[dict[str, Any], ...]
    bindings: tuple[dict[str, Any], ...]
    contexts: tuple[GenerationContextView, ...]


@dataclass(frozen=True, slots=True)
class EditTarget:
    """一次编辑的读侧快照（所有字段同一次读取，避免跨快照拼装）。

    ## 为什么把"当前版本"的字段与 `lock_version` 放在**同一个**值类型里

    编辑是"读 → 展示/改 → 带 `If-Match` 提交"的两步。若读侧分两次查
    （先查 `content_items` 拿 `lock_version`，再查版本行拿标题正文），
    两次之间可以插进一次别的写入 —— 于是返回给前端的正文与它拿到的
    ETag **不属于同一版**。用户改完提交时 412，而前端展示的差异
    与数据库里的当前版本对不上（"我明明看到的是 A，你说现在是 B"）。

    因此本类型刻意把身份侧（`lock_version` / `etag`）与版本侧
    （`title` / `body` / `tags` / `bindings` / `blueprint_id` /
    `generation_context_id`）装进**一个** frozen 快照：装配点只有一个
    （`service.read_edit_target`），调用方没有机会把两个时刻的值拼起来。

    `etag` 是冗余的派生字段（= `build_etag(content_id, lock_version)`）：
    留一个现成的值，是为了让端点不必自己拼 ETag —— `platform.concurrency`
    的 docstring 已论证"各端点手拼 details 迟早漏 `currentEtag`"，
    同一个理由适用于响应头。
    """

    content_id: uuid.UUID
    project_id: uuid.UUID
    lock_version: int          # ETag 的版本位
    etag: str                  # = build_etag(content_id, lock_version)
    current_version_id: uuid.UUID
    version_no: int
    title: str
    body: str
    tags: tuple[str, ...]
    bindings: tuple[dict[str, Any], ...]
    blueprint_id: uuid.UUID | None
    generation_context_id: uuid.UUID | None


# ---------------------------------------------------------------------------
# ★ 一次生成请求的载荷（`content_items.generation_request`）
# ---------------------------------------------------------------------------
# **为什么需要这一块** —— 编码期实测的接缝缺口，不是设计冗余：
#
# 「生成参数」从**发起**（`POST /projects/{id}/content-generations`，端点那次
# 事务）到**执行**（WP13 的 Worker + 本包 `handlers.py`，另一次事务）之间，
# **没有任何既有传输通道**。逐条实测：
#
# | 候选通道 | 实测结论 |
# |---|---|
# | `ClaimedJob` | WP13 冻结形状：只有 `job_id` / `kind` / `attempt_id` / `fence_token` / `attempt_no` / `worker_id` / `input_version_ids`，无自由参数位 |
# | `jobs.result_ref` | 语义是**结果**引用。用输入占住结果字段，任务到终态时就再也放不下真正的产出（WP17/WP18 已论证并否决此路） |
# | `jobs.input_version_ids` | `version_nodes` 的 id 数组 —— 放 `batchKind` 这类标量在类型上就不成立 |
# | `jobs` / `generation_contexts` 的其它列 | 无 jsonb 参数列（逐列核对） |
# | `projects` 上的"项目默认模型档案" | **没有这一列**，且本包无权给 `projects` 加列（WP21 的资产） |
# | 处理器的依赖注入（`build_task_registry(…)`） | 那是**进程级**配置，而 `scope` / `batchKind` / `modelProfileId` 是**每请求**不同的 —— 注入它等于把 A 请求的档位用到 B 请求上 |
#
# ⇒ 缺口真实且**回避不了**：没有它，处理器拿不到 `batchKind`，
#   连一次预占都算不出来，整条链路一步都走不了。
#
# **处置借用 WP18 的同款先例**（`brief/handlers.py` 模块 docstring 第二节）：
# 那一次 `BRIEF_ENRICH` 的 `allowedSourceIds` 也没有落点，WP18 的处置是
# **在它自己模块的表上加一列一次性请求参数**（`brief_drafts.enrichment_request`），
# 并把三个候选（`jobs.result_ref` / `jobs.input_version_ids` / 新建一张表）
# 逐条否决。本包的理由与它逐条相同：
#
#   · 本包 **不修改** `jobs`（WP13 的资产，接缝冻结铁律）；
#   · 本包 **不新建**"只为传一次参数"的表（与 WP18 拒绝新建
#     `brief_enrichment_requests` 的理由同源）；
#   · 参数落在**本包自己的** `content_items` 上 ——
#     它不是"又一张表"，而是"这一轮为这条内容请求了什么"，
#     与"这一轮产出了什么"（`content_versions`）是同一件事的两半。
#
# **每行一份而不是整批一份**：任务粒度是**单条内容**
# （`enqueue_job(content_id=…)` 有这个参数），因此"一条内容一个参数集"
# 与执行粒度同形。批量生成时 N 条内容各带一份**内容相同**的载荷 ——
# 这与 `content_items.last_idempotency_key` 在那一批的每一行上都存同一个键
# 是**同一种做法**，不是重复存储的疏漏。
#
# **写一次、读一次**（与 `enrichment_request` 一样是一次性的）：
# 处理器读完即可清空；留着只会让下一次误读到旧档位。
# 本包选择**不清空**（`content_items` 是可变的，清空会多一次 UPDATE 与一次
# 乐观锁前进，而 `generation_request` 本身**不参与**任何判据），
# 代价是它作为"这条内容最后一次是按什么参数生成的"留痕 —— 那是收益。
# 该取舍登记在说明文档附录 A。

# ---------------------------------------------------------------------------
# 任务框架的取值（WP13）：kind / queue / 幂等域
# ---------------------------------------------------------------------------
# 与 `brief/types.py` 的 `QUEUE_BRIEF = "brief"` 同惯例：**放在 types.py**
# 而不是 service/handlers 里。三个值各有**两个**消费点（发起端 `api.py` 入队、
# 处理器侧 `handlers.py` 注册），字面量在两处各写一遍的表现是
# "入队用了 A 字符串、注册表挂了 B 字符串" ⇒ 任务永远没有处理器接手，
# 而它**不报错**：任务安安静静躺在 `QUEUED`（与 §1.6 R1 要防的形态同族）。
KIND_CONTENT_GENERATE: Final[str] = "CONTENT_GENERATE"

#: 队列名。与 `QUEUE_BRIEF`（"brief"）/ `QUEUE_PARSING`（"parsing"）同形。
QUEUE_CONTENT: Final[str] = "content"

#: 入队幂等域（`jobs.idempotency_scope`）。
#:
#: **与 `gateway_bridge` 的 `idempotency_key` 不是同一个东西**：
#: 这个是**任务层**的（"同一个入队请求不要建两个任务"），
#: 那个是**模型调用层**的（R5，"同一次生成不要买两次"）。
#: 两者混同的后果：重做（版本号前进）在任务层被当成重复请求 ⇒ 重做不生效。
#: 因此任务层的键**不含版本号**（同一内容的重做要建新任务），
#: 模型层的键**含版本号**（同一版本的重放不重发）。
IDEMPOTENCY_SCOPE_CONTENT_GENERATE: Final[str] = "content-generate"


#: `generation_request` 的键名。**用常量而不是散落的字面量**：
#: 载荷有**两个**读写点（`service.py` 构造、处理器解析），
#: 字面量拼错的表现是"缺键 ⇒ 参数没传" —— 而它**不会报错**，
#: 只会静默走到"用了一个错的档位"。这与 §1.6 R1 要防的形态同族。
REQUEST_KEY_SCOPE: Final[str] = "scope"
REQUEST_KEY_BATCH_KIND: Final[str] = "batchKind"
REQUEST_KEY_MODEL_PROFILE_ID: Final[str] = "modelProfileId"

#: 载荷的三个键（数量是**判据**：多一个键即视为载荷被写坏）。
GENERATION_REQUEST_KEYS: Final[frozenset[str]] = frozenset(
    {REQUEST_KEY_SCOPE, REQUEST_KEY_BATCH_KIND, REQUEST_KEY_MODEL_PROFILE_ID}
)


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """一次生成请求在**执行阶段**需要的全部参数（其余都能现场查出来）。

    ## 只有三个字段 —— 不是省，是"凡能查的都不存"

    | 字段 | 为什么必须存 | 为什么不能查 |
    |---|---|---|
    | `scope` | 决定跑哪几个 `task_kind`、以及响应要如实回填"实际重做的范围" | 它是**用户的意图**，库里没有任何列能反推（"这一版是新生成还是仅标题重做"在 `content_versions` 上无痕迹，两者同形） |
    | `batch_kind` | `estimate_cost` 的必填入参（WP24 的 10/30/50 硬约束靠它生效） | 它是**请求的属性**（用户选试产/扩产/批产），**不是条数的函数** —— 从内容条数反推档位会让"猜出的档位必然容纳该条数"，进而把 G3 的硬约束**静默关掉**（`billing_bridge.estimate_generation_cost` docstring 已论证） |
    | `model_profile_id` | `estimate_cost` 与 `CapabilityRequest` 都要它 | 它决定用哪个模型，而"用哪个模型"是**请求的选择**；本仓无"项目默认模型档案"概念 |

    **`provider_id` 不在其中**：它由 `model_profile_id` 唯一解出
    （`gateway_repository.get_model_profile(id).provider_id`）——
    存进来就有两个来源，而两者分叉的表现是"预占记在 A 供应商、实际调用走 B"，
    账面与留痕对不上却都不报错。解析在 `service.py`，一处。

    **`content_count` 不在其中**：任务粒度是**单条内容**，
    因此处理器侧的 `calls_for(task_kind, count)` 恒以 `count=1` 调用
    （一条内容各算各自的调用次数）。批量请求在端点侧按整批做**展示用**
    的预估（§1.7 前端消费面），真正的预占在执行阶段逐条发生 ——
    见 `service.py` 的模块 docstring「预占为什么必须在执行阶段」。
    """

    scope: str
    batch_kind: str
    model_profile_id: uuid.UUID
    #: ★ 接缝 §5.15：这条生成"挂在哪个场景 / 哪个形式"。**可选、默认 None**。
    #: 它们是用户意图，但**不进 `generation_request` 载荷**（`as_payload` /
    #: `from_payload` / `GENERATION_REQUEST_KEYS` 三键口径不变）：
    #: 执行阶段不消费它们，且 `content_items` 上有专列
    #: （`scene_key` / `form_key`），写两处就是两个真值来源。
    #: 因此 `from_payload(as_payload(r)) == r` 只在两者均为 None 时成立。
    #: 局部重做（`prepare_regeneration`）不改这两列。
    scene_key: str | None = None
    form_key: str | None = None

    def __post_init__(self) -> None:
        """**构造即归一、构造即校验**（fail-closed）。

        ## 为什么把归一放在构造器，而不是留给调用方

        编码期冒烟实测出的一处真实不对称：`service.py` 按用户的 `TAGS`
        请求构造本对象时，若构造器**不归一**，则 `as_payload()` 会原样写下
        `'scope': 'TAGS'`，而 `from_payload()` 会把它归一成 `'COPY'`
        —— 同一个值在"落库的张量"与"执行的依据"两处**不相等**
        （实测 `roundtrip eq = False`）。

        代价不是多一个 if。`scope` 有三个消费点，其中两个**都不会报错**：

        | 消费点 | 不归一时会发生什么 |
        |---|---|
        | `task_kinds` | 查 `SCOPE_TASK_KINDS`，而那里**恰好也有** `TAGS` 键（值同 `COPY`）⇒ 少跑的那次**不会被发现** |
        | 响应回填的"实际重做范围" | 读 `.scope` 答 `TAGS`、读 `normalize_scope(.scope)` 答 `COPY` ⇒ **响应撒谎**（用户看不出） |
        | `estimate_cost` 的调用次数 | `TAGS` 与 `COPY` 的 `task_kind` 元组相同 ⇒ 金额也**恰好**对得上，错误被进一步掩盖 |

        ⇒ 处置：把"只能持有已归一的 `scope`"做成**构造器不变量**，
        调用方**没有机会**写错。这是"由类型决定"而不是"由人记得写对"
        —— 与说明文档 §9 候选第 14 条纪律同一种形态。
        归一函数仍只有一处：`normalize_scope`（本处不写第二份映射）。

        未知 `scope` 也在**构造时**抛 —— 端点那次事务里抛，
        那里有响应可组装（service 翻成 422 `VALIDATION_FAILED`）。
        若留到执行阶段才抛，参数已经落库、任务已入队，
        报错位置离病根隔了一次事务边界（与 §1.6 R1 要防的形态同族）。
        """
        if not isinstance(self.scope, str):
            raise ValueError(
                f"scope 必须是字符串，实际是 {type(self.scope).__name__}。"
            )
        # 未知取值由 `normalize_scope` 自己抛（含合法取值清单），此处不重判名单。
        object.__setattr__(self, "scope", normalize_scope(self.scope))

        if not isinstance(self.batch_kind, str) or not self.batch_kind.strip():
            raise ValueError(
                f"{REQUEST_KEY_BATCH_KIND} 必须是非空字符串，"
                f"实际是 {self.batch_kind!r}。"
                "（它的受控取值域是 WP24 的 TRIAL/SCALE/BULK，"
                "校验由 `estimate_cost` → `validate_quantity` 承担 —— "
                "本处只判『给了一个能用的标量』，不复制一份档位名单，"
                "那是第二个真值来源）"
            )

        if not isinstance(self.model_profile_id, uuid.UUID):
            raise ValueError(
                f"model_profile_id 必须是 uuid.UUID 实例，"
                f"实际是 {type(self.model_profile_id).__name__}。"
            )

        # scene_key / form_key：None = 未指派；给了就去首尾空白，
        # 空白串与超长串都拒（不做枚举：planning 无受控取值域）。
        for name in ("scene_key", "form_key"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(
                    f"{name} 必须是字符串或 None，实际是 {type(value).__name__}。"
                )
            value = value.strip()
            if not value:
                raise ValueError(f"{name} 不能是空串或全空白（未指派请传 None）。")
            if len(value) > MAX_SCENE_FORM_KEY_CHARS:
                raise ValueError(
                    f"{name} 超过 {MAX_SCENE_FORM_KEY_CHARS} 字符（实际 {len(value)}）。"
                )
            object.__setattr__(self, name, value)

    @property
    def task_kinds(self) -> tuple[str, ...]:
        """本请求实际要跑的 `task_kind`（**顺序有意义**：结构在文案之前）。

        委托 `task_kinds_for_scope` 而不是在这里写一份映射：
        两处映射必然分叉，而分叉的表现是"端点说跑两个、处理器只跑了一个"
        （少跑的那次**不报错**，用户看到的只是一批没生成全的内容）。
        """
        return task_kinds_for_scope(self.scope)

    def as_payload(self) -> dict[str, Any]:
        """落库形状。`model_profile_id` 转 `str` —— JSONB 不认 UUID 对象。"""
        return {
            REQUEST_KEY_SCOPE: self.scope,
            REQUEST_KEY_BATCH_KIND: self.batch_kind,
            REQUEST_KEY_MODEL_PROFILE_ID: str(self.model_profile_id),
        }

    @classmethod
    def from_payload(cls, raw: Any) -> GenerationRequest:
        """解析落库形状。**任何一处不对即抛**（fail-closed）。

        **为什么不给默认值**：三个字段各有"看起来合理的默认"，
        而每个默认值都会把一次**真实的装配故障**变成一次**静默的错误行为**：

        | 缺哪个 | 若给默认 | 后果 |
        |---|---|---|
        | `scope` | `COPY` | 用户要"整篇重做"（`FULL`，含结构），实际只重做了文案，蓝图没更新 |
        | `batch_kind` | `TRIAL` | 请求说扩产 30 条，预占按试产 10 条算 ⇒ 撞 G3 上界时才发现，或干脆多花 |
        | `modelProfileId` | 任一档案 | 用了**别的**模型的价钱与策略 |

        抛 `ValueError` 而不是 `ApiError`：本函数在**处理器**侧被调，
        那里没有响应可组装；处理器把 `ValueError` 折成一个
        **不可重试**的任务失败（重试不会让参数长出来）。

        本函数只判**形状**（是不是对象 / 三键齐不齐 / 多不多键），
        类型与取值域全部交给构造器 —— 归一与校验各只有一处
        （`__post_init__`），此处不复制一份（复制就会分叉）。
        """
        if not isinstance(raw, dict):
            raise ValueError(
                f"generation_request 必须是 JSON 对象，实际是 {type(raw).__name__}。"
                "（这条拒掉的是『参数根本没写进去』—— 端点用了旧代码路径，"
                "或有人手工插了内容行）"
            )
        missing = GENERATION_REQUEST_KEYS - raw.keys()
        if missing:
            raise ValueError(
                f"generation_request 缺键：{sorted(missing)}。"
                f"应有三键：{sorted(GENERATION_REQUEST_KEYS)}。"
                "（不给默认值是刻意的：默认值会把装配故障变成静默的错误行为）"
            )
        extra = raw.keys() - GENERATION_REQUEST_KEYS
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
            # UUID 字面量 → 对象这一步只能在这里做（构造器要的是 uuid.UUID）。
            profile_id = uuid.UUID(profile_raw)
        except ValueError as exc:
            raise ValueError(
                f"{REQUEST_KEY_MODEL_PROFILE_ID} 不是合法 UUID：{profile_raw!r}。"
            ) from exc

        # `scope` 与 `batch_kind` 原样交给构造器：归一（`TAGS` → `COPY`）
        # 与类型/非空校验都在 `__post_init__` 里，且**只有那一次**。
        # 这里提前归一是多余的，而"多余的第二次归一"就是分叉的温床
        # （两处改一处 ⇒ 落库值 ≠ 执行依据，见构造器 docstring 的表）。
        return cls(
            scope=raw[REQUEST_KEY_SCOPE],
            batch_kind=raw[REQUEST_KEY_BATCH_KIND],
            model_profile_id=profile_id,
        )


# ---------------------------------------------------------------------------
# 解析 / 计算辅助（纯函数，供各层复用）
# ---------------------------------------------------------------------------


def calls_for(task_kind: str, content_count: int) -> int:
    """**全包唯一的调用次数口径**（R3）。

    一次生成要预占"几次调用"的钱，答案只能从这里来。

    | `task_kind` | 次数 |
    |---|---|
    | `CONTENT_STRUCTURE` | `content_count × 1` |
    | `CONTENT_COPY` | `content_count × 1` |

    ## 为什么标题候选不额外算一次

    标题候选与正文是**同一段提示词的输出**（`TASK_CONTENT_COPY` 一次调用
    同时产出标题候选、正文、标签）。demo 的 `trial-plan.html` 把
    「文案生成」与「标题候选（2—3 个）」**分开计价**，那是因为两者的
    **token 量差一个量级**（≈86 千 vs ≈9 千），不是因为调用了两次。
    把计价分列误读成"两次调用"，会让 `calls_for` 多算一次 ——
    而账面多预占的钱**不会报错**，只会在对账时表现为"永远差一笔"。

    ## 未知 `task_kind` 抛错而不是返回 0

    `0` 会让预占金额为 0，而 `reserve_budget` 拒绝非正数 ⇒
    报错点是"金额必须为正"，与真正的根因（kind 拼错）隔了两层。
    这里直接抛，报错点就是根因。

    ## 负数 `content_count`

    抛 `ValueError`。**不返回负数次调用** —— 那会让 `amount` 为负，
    而负数的预占在 WP25 的 `_require_positive_amount` 处才会被拒；
    更糟的是若将来某处忘了校验，负数预占会**反向增加**可用额度。
    """
    if task_kind not in TASK_CALLS_PER_ITEM:
        raise ValueError(
            f"未知的生成类型：{task_kind!r}（本包只消费 {sorted(TASK_KINDS)}）。"
        )
    if content_count < 0:
        raise ValueError(f"内容条数不得为负：{content_count}。")
    return content_count * TASK_CALLS_PER_ITEM[task_kind]


def task_kinds_for_scope(scope: str) -> tuple[str, ...]:
    """一个重做范围实际要跑哪些 `task_kind`（**顺序有意义**）。

    `FULL` 返回 `(CONTENT_STRUCTURE, CONTENT_COPY)` ——
    结构必须在文案之前：前者的产物是后者的输入（见说明文档 §9
    候选第 20 条："真正拆开的判据是前者的产物是后者的输入，且前者可复用"）。
    """
    if scope not in SCOPE_TASK_KINDS:
        raise ValueError(
            f"未知的重做范围：{scope!r}（合法取值 {sorted(REGENERATION_SCOPES)}）。"
        )
    return SCOPE_TASK_KINDS[scope]


def normalize_scope(scope: str) -> str:
    """把请求里的 scope 归一成**实际执行**的 scope。

    只有一处归一：`TAGS` → `COPY`（标签随正文一起重做，见
    `SCOPE_TAGS` 的说明）。其余原样返回。

    **为什么公开成一个函数**：响应里要如实回填"实际重做的范围"，
    而"实际"的定义必须与"执行"的依据同源。若 `service` 里写一处
    `if scope == "TAGS": scope = "COPY"`、`api` 里再写一处，
    两处改一处就分叉（分叉的表现是**响应撒谎**：说重做了标签，
    实际重做了整段正文且标签也变了 —— 用户看不出来）。
    """
    if scope not in REGENERATION_SCOPES:
        raise ValueError(
            f"未知的重做范围：{scope!r}（合法取值 {sorted(REGENERATION_SCOPES)}）。"
        )
    return SCOPE_COPY if scope == SCOPE_TAGS else scope


__all__ = [
    "BINDING_KEYS",
    "BINDING_KEY_CLAIM_ID",
    "BINDING_KEY_PARAGRAPH_INDEX",
    "BINDING_KEY_REASON",
    "BINDING_KEY_TEXT",
    "BLUEPRINT_FIELDS",
    "CONTENT_SCHEMA_VERSION",
    "CONTENT_STATES",
    "CONTENT_STATE_DRAFT",
    "CONTENT_STATE_FAILED",
    "CONTENT_STATE_GENERATED",
    "CONTENT_STATE_LIST_SQL",
    "CONTENT_STATE_STRUCTURED",
    "CONCLUSION_STRENGTHS",
    "CONCLUSION_STRENGTH_LIST_SQL",
    "COPY_FIELDS",
    "GENERATION_CONTEXT_LIST_SQL",
    "IDEMPOTENCY_SCOPE_CONTENT_GENERATE",
    "KIND_CONTENT_GENERATE",
    "MAX_BINDING_REASON_CHARS",
    "MAX_BLUEPRINT_FIELD_CHARS",
    "MAX_BODY_CHARS",
    "MAX_HIGHLIGHT_CHARS",
    "MAX_KEYWORD_CHARS",
    "MAX_PARAGRAPHS",
    "MAX_PARAGRAPH_CHARS",
    "MAX_PROMPT_TEMPLATE_NAME_CHARS",
    "MAX_REGENERATION_REASON_CHARS",
    "MAX_RISK_NOTE_CHARS",
    "MAX_TAGS",
    "MAX_TAG_CHARS",
    "MAX_TITLE_CANDIDATES",
    "MAX_TITLE_CHARS",
    "MIN_TITLE_CANDIDATES",
    "QUEUE_CONTENT",
    "REGENERATION_SCOPES",
    "REGENERATION_SCOPE_LIST_SQL",
    "REASON_BASELINE_MISMATCH",
    "REASON_BASELINE_NOT_READY",
    "REASON_BINDING_COVERS_ALL_CLAIMS",
    "REASON_BINDING_MISSING_REASON",
    "REASON_BINDING_UNKNOWN_CLAIM",
    "REASON_BLUEPRINT_NOT_FOUND",
    "REASON_CONTENT_NOT_FOUND",
    "REASON_CONTENT_VERSION_NOT_FOUND",
    "REASON_EDIT_LOCK_CONFLICT",
    "REASON_GATEWAY_DENIED",
    "REASON_GATEWAY_NOT_SUCCEEDED",
    "REASON_GATEWAY_NO_OUTPUT",
    "REASON_GENERATION_REQUEST_INVALID",
    "REASON_IDEMPOTENCY_CONFLICT",
    "REASON_MODEL_PROFILE_NOT_FOUND",
    "REASON_OUTPUT_FIELD_MISSING",
    "REASON_OUTPUT_FIELD_TYPE",
    "REASON_OUTPUT_FIELD_VALUE",
    "REASON_OUTPUT_NOT_OBJECT",
    "REASON_PROJECTION_CONFLICT",
    "REASON_PROJECT_NOT_FOUND",
    "REASON_PROMPT_NOT_ACTIVATED",
    "REASON_PROMPT_TEMPLATE_MISSING",
    "REASON_SCOPE_REQUIRES_BLUEPRINT",
    "REASON_SCOPE_UNKNOWN",
    "REASON_TASK_KIND_UNKNOWN",
    "REASON_TITLE_CANDIDATE_MISMATCH",
    "REASON_TITLE_CANDIDATE_NOT_FOUND",
    "REASON_TOO_FEW_TITLE_CANDIDATES",
    "REASON_TOO_MANY_TITLE_CANDIDATES",
    "REQUEST_KEY_BATCH_KIND",
    "REQUEST_KEY_MODEL_PROFILE_ID",
    "REQUEST_KEY_SCOPE",
    "SCOPE_COPY",
    "SCOPE_FULL",
    "SCOPE_TAGS",
    "SCOPE_TASK_KINDS",
    "SCOPE_TITLE",
    "STRENGTH_CONSERVATIVE",
    "STRENGTH_DIRECT",
    "STRENGTH_HEDGED",
    "TASK_CALLS_PER_ITEM",
    "TASK_CONTENT_COPY",
    "TASK_CONTENT_STRUCTURE",
    "TASK_KINDS",
    "TITLE_SCORE_DIMENSIONS",
    "TITLE_SCORE_DIMENSION_SET",
    "BlueprintDraft",
    "ContentDetail",
    "ContentSummary",
    "ContentVersionSummary",
    "EditTarget",
    "GENERATION_REQUEST_KEYS",
    "GeneratedCopy",
    "GenerationContextView",
    "GenerationRequest",
    "ParagraphBinding",
    "TitleCandidateDraft",
    "calls_for",
    "normalize_scope",
    "task_kinds_for_scope",
]