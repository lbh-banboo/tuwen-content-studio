"""WP23 的不可变值类型与受控取值域。

**为什么单独一个模块**（与 WP11/WP16/WP20 同一手法）：这些取值同时被
`models.py`（CHECK 约束）、`render.py`（渲染校验）、`tools_policy.py`（权限上界）、
`repository.py`（存储）与 `service.py`（编排）使用。定义在 service 里会让
其它四个反向导入 service —— 而本模块的分层刻意是单向的：

    types ← render
    types ← tools_policy
    types ← models
    types ← repository
    types ← service

**跨层传递的值用 frozen dataclass 而不是 ORM 对象**（ORM 实例绑在 session 上，
session 一关就 `DetachedInstanceError`）。

---

## 本模块承载的两条验收不变量

验收原文（04 §4.3）：「**模板变更可追溯，不扩大工具权限**」。两条都在这里定取值：

| 不变量 | 本模块的落点 |
|---|---|
| **可追溯** | `PUBLICATION_ACTIONS` / `MAX_CHANGE_REASON_CHARS` —— 变更与激活都必须带理由，理由的长度上界显式写出来（不然一个 1MB 的理由能把审计表撑爆） |
| **不扩大工具权限** | `PLATFORM_TOOL_CEILING` / `TOOL_CEILING_LIST_SQL` —— **上界是一个可枚举的闭集**，不是"非空"这类恒真条件 |

## 为什么 `allowed_tools` 的上界必须是闭集（这是本包最要紧的一句话）

"不扩大工具权限"最容易做成**假防线**的写法是：

    if not allowed_tools:  reject()      # ← 只判非空

这条判据**恒真地为"通过"**（只要填了任何东西就放行），
于是"不能扩大"退化成"必须填点什么"。它与 WP19 的 ISS-75 是**同一个形态**：
**判据看起来在，实际不起任何约束作用，而且不报错**。

因此本模块定义的是**闭集**：`PLATFORM_TOOL_CEILING` 里的每一个名字都是
平台配置管理方（不是模板编辑者）能授予的**全部**工具。模板版本只能取它的**子集**，
数据库层用 CHECK 兜住（`allowed_tools <@ ARRAY[...]`），
**绕过 service 直接写库也扩不了权**。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

# ---------------------------------------------------------------------------
# task_kind：任务类型的受控取值域
# ---------------------------------------------------------------------------
# 协议 03 §9 第 11 行 `GET /prompts` 的入参只有 "taskKind" 这个**参数名**，
# 没有给取值域。若不定义，`?taskKind=X` 的 X 就是自由字符串 ——
# 与 WP11 拒绝"万能表"的理由完全相同：同一任务类型会出现多种拼写，
# 而**没有任何机制会报错**。
#
# ## ⚠️ 取值必须**对齐**既有包，不能另起拼写（本包实际踩过一次）
#
# WP19（研究关键词与提示词）已经在 `research/types.py` 里定义了
# `TASK_FACT_VERIFICATION` / `TASK_EVIDENCE_COLLECTION`，并且它们已经
# **出现在已交付的 API 响应里**（`GET /projects/{projectId}/research-prompts`
# 的 `prompts[].taskKind`）。
#
# 本包初稿曾另起一套拼写（`RESEARCH_EVIDENCE`），后果是：**同一个任务类型
# 在库里会出现两种写法，而没有任何机制会报错** —— 这正是下面"风险"里
# 写过的形态，结果自己踩了。修正为直接采用 WP19 的拼写。
#
# **防线不能只靠"记得对齐"**：`tests/test_prompts_wp23.py` 有一条
# 跨包断言 `research.types.TASK_KINDS ⊆ prompts.types.TASK_KINDS`。
# 任一侧改了取值，那条测试会红 —— 把"对齐"从约定变成机械判据。
#
# ## 其余取值来自哪里
#
#   · `BRIEF_DECOMPOSE` / `BRIEF_ENRICH` —— WP18 的硬编码提示词
#     （`decomposer.py` 的 `_build_decompose_prompt` / `_build_enrich_prompt`，
#     OP-141 要求它们接到本包的版本机制上）；
#   · `CONTENT_STRUCTURE` / `CONTENT_COPY` —— WP26 的内容生成；
#   · `QUALITY_CHECK` —— WP30 的 AI 自检；
#   · `KNOWLEDGE_SEARCH` —— WP21 的语义检索；
#   · `REPORT_GENERATE` —— 阶段报告；
#   · `STATIC_IMAGE` —— WP27 的静态图生成。
#
# 说明：本包**只定义取值域与落 CHECK**，不为每个 task_kind 写实现 ——
# 实现归各自的包。这是"有设计空间"与"只是取值域"的边界：
# 取值域不定义就会出现拼写漂移，属于本包的职责。
#
# ## ★ `STATIC_IMAGE` 是**补登**的，不是一开始就有（2026-10-07 WP27 落库期实测）
#
# WP23 落这份取值域时按当时的规划手写了 9 个值，**漏了 WP27 的静态图**。
# 它是"覆盖范围由人写"的第 N 次现身，且漏的方式最隐蔽：
#
# | 层 | 漏了会怎样 |
# |---|---|
# | `TASK_KINDS` | `media` 侧的 `resolve_active_version(task_kind="STATIC_IMAGE")` **第一道门就抛** `TASK_KIND_UNKNOWN` |
# | `prompt_templates.task_kind` 的 CHECK | 库里**永远插不进**一行静态图模板（`ck_prompt_templates_task_kind_valid` 拦） |
#
# 两者合起来：WP27 的 R5（"没有激活提示词版本 ⇒ fail-closed 不调网关"）
# 会从"**遇到缺口就停**"退化成"**永远停**" —— 而这两种形态在响应里
# **长得一样**（都是 `TASK_KIND_UNKNOWN`），排障时会一路去查 WP23，
# 而真因是"这一侧的名单漏了一个值"。
#
# ⚠️ 之所以**必须改在这一侧**（而不是让 WP27 自带一份取值域）：
# `prompt_templates` 的 CHECK 是**数据库层**的，`media` 侧改任何代码
# 都绕不过它。取值域的主人只能是 `prompts` 本包。
# `media/types.py::TASK_STATIC_IMAGE` 保留（那是"本包对外承诺的取值"，
# 它允许与提示词侧**不同步**）。
#
# ⛔ **已知缺口（2026-10-07 实测更正）**：本处原先写"两侧的一致性由
# test_media_wp27.py::test_task_kind_constants_match_prompts 守" ——
# 该文件**全仓不存在**（`media` 包在 `services/backend/tests/` 下零覆盖）。
# 同名的 `test_content_wp26.py::test_task_kind_constants_match_prompts`
# 守的是 **content ↔ prompts**，与本处无关。⇒ 本侧与 `media` 侧的一致性
# **无用例守**；能机械拦住的只有本侧的闭集断言
# （`test_prompts_wp23.py::test_task_kinds_are_a_closed_set`，10 个成员）
# 与库里的 `ck_prompt_templates_task_kind_valid`（迁移字面量），
# 它们都只管"这一侧"，管不到"另一侧漏没漏"。
TASK_FACT_VERIFICATION: Final[str] = "FACT_VERIFICATION"  # 对齐 WP19
TASK_EVIDENCE_COLLECTION: Final[str] = "EVIDENCE_COLLECTION"  # 对齐 WP19
TASK_BRIEF_DECOMPOSE: Final[str] = "BRIEF_DECOMPOSE"
TASK_BRIEF_ENRICH: Final[str] = "BRIEF_ENRICH"
TASK_CONTENT_STRUCTURE: Final[str] = "CONTENT_STRUCTURE"
TASK_CONTENT_COPY: Final[str] = "CONTENT_COPY"
TASK_QUALITY_CHECK: Final[str] = "QUALITY_CHECK"
TASK_KNOWLEDGE_SEARCH: Final[str] = "KNOWLEDGE_SEARCH"
TASK_REPORT_GENERATE: Final[str] = "REPORT_GENERATE"
TASK_STATIC_IMAGE: Final[str] = "STATIC_IMAGE"  # 对齐 WP27（补登）

TASK_KINDS: Final[frozenset[str]] = frozenset(
    {
        TASK_FACT_VERIFICATION,
        TASK_EVIDENCE_COLLECTION,
        TASK_BRIEF_DECOMPOSE,
        TASK_BRIEF_ENRICH,
        TASK_CONTENT_STRUCTURE,
        TASK_CONTENT_COPY,
        TASK_QUALITY_CHECK,
        TASK_KNOWLEDGE_SEARCH,
        TASK_REPORT_GENERATE,
        TASK_STATIC_IMAGE,
    }
)

# 与 CHECK 约束、迁移里的取值列表保持一致的极简形式 —— 生成 SQL 片段用。
TASK_KIND_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(TASK_KINDS)
)

# ---------------------------------------------------------------------------
# 模板状态
# ---------------------------------------------------------------------------
# 模板身份本身**没有生命周期状态**（协议未定义停用端点，见说明文档 §1.4 疏漏 #8），
# 因此这里只有"是否已有激活版本"这一个由投影得出的状态 ——
# 它是**算出来的**，不是存在某一列上被就地改写的（与 WP20 的投影同一手法）。
TEMPLATE_STATE_DRAFT: Final[str] = "DRAFT"
TEMPLATE_STATE_ACTIVE: Final[str] = "ACTIVE"

TEMPLATE_STATES: Final[frozenset[str]] = frozenset(
    {TEMPLATE_STATE_DRAFT, TEMPLATE_STATE_ACTIVE}
)

# ---------------------------------------------------------------------------
# 激活动作
# ---------------------------------------------------------------------------
# 与 WP20 的 `PUBLICATION_ACTIONS` 刻意**不同**：知识有 ARCHIVE/RESTORE
# （整条资产退出/恢复可用），而模板没有这两个动作 ——
# 协议 §9 第 13 行只写"激活，记录影响范围"。
# **不照抄知识的动作集**：模板"停用"的语义（是让某版失效，还是让整个模板不可用？）
# 协议没定义，照抄会凭空发明一个语义。
PUBLICATION_ACTION_ACTIVATE: Final[str] = "ACTIVATE"
PUBLICATION_ACTION_SUPERSEDE: Final[str] = "SUPERSEDE"

PUBLICATION_ACTIONS: Final[tuple[str, ...]] = (
    PUBLICATION_ACTION_ACTIVATE,
    PUBLICATION_ACTION_SUPERSEDE,
)
PUBLICATION_ACTION_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in PUBLICATION_ACTIONS
)

# ---------------------------------------------------------------------------
# 工具权限上界（验收原文「不扩大工具权限」的落点）
# ---------------------------------------------------------------------------
# **这是平台配置管理方能授予的全部工具**，不是"常用工具列表"。
# 每个名字对应一个**外部访问能力**，而不是一个内部函数：
#   · `KNOWLEDGE_READ`     读共享知识（项目私有数据不在其中，见 ADR-16）
#   · `PROJECT_READ`       读本项目内容
#   · `RESEARCH_SEARCH`    出网检索（受 ADR-10 出网策略约束）
#   · `IMAGE_ANALYZE`      调用视觉模型
#   · `CONTENT_DRAFT`      生成内容草稿
#   · `METRIC_READ`        读指标（WP38 起）
#
# **加一个工具名 = 扩大平台能力**，必须走配置变更（将来的配置表 + ADR），
# 而不是改模板版本。这正是"普通模板编辑不能扩大工具权限"的表达。
PLATFORM_TOOL_CEILING: Final[tuple[str, ...]] = (
    "KNOWLEDGE_READ",
    "PROJECT_READ",
    "RESEARCH_SEARCH",
    "IMAGE_ANALYZE",
    "CONTENT_DRAFT",
    "METRIC_READ",
)

# CHECK 约束用：`allowed_tools <@ ARRAY['a','b',...]::text[]`
TOOL_CEILING_ARRAY_SQL: Final[str] = (
    "ARRAY[" + ", ".join(f"'{name}'" for name in PLATFORM_TOOL_CEILING) + "]::text[]"
)

# ---------------------------------------------------------------------------
# 首版的默认工具集：**空集**
# ---------------------------------------------------------------------------
# 这不是"随便选了个默认值"，而是**fail-closed**：一个新建的模板在平台
# 明确授予之前**一个工具都不能用**。
#
# 反过来（默认给全集）的失败形态是静默的：新建的模板自动获得出网检索能力，
# 而没有任何人做过"这个模板需要出网吗"的判断。这与
# `allowed_tools` 的 CHECK 是同一取向 —— **默认必须是最小的那一档**。
#
# 具体某个模板需要哪些工具，走 `service.grant_tools`（平台侧路径）。
PLATFORM_DEFAULT_TOOLS: Final[tuple[str, ...]] = ()

# ---------------------------------------------------------------------------
# 长度与数值上界
# ---------------------------------------------------------------------------
# 长度上界**显式写出来**而不是靠 `text` 类型的隐式无限：
# 审计表与版本表的行会长期保留，一个 1MB 的 change_reason 是容量事故。
MAX_NAME_CHARS: Final[int] = 200
MAX_CHANGE_REASON_CHARS: Final[int] = 500
MAX_REASON_CHARS: Final[int] = 500
MAX_TEMPLATE_CHARS: Final[int] = 20_000
MAX_PROMPT_VERSION_NO: Final[int] = 2_147_483_647

# 发布事件的序号从 1 开始（与 WP20 同一手法）。
MIN_PUBLICATION_SEQUENCE: Final[int] = 1

# `input_schema` / `output_schema` 的形状版本。与 WP11 的 `schema_version`、
# WP17 的 `position_schema_version`、WP18 的 `BRIEF_SCHEMA_VERSION` 同一手法：
# 将来解析历史 payload 时靠它选择正确的 Schema。
PROMPT_SCHEMA_VERSION: Final[int] = 1

# ---------------------------------------------------------------------------
# 渲染校验：占位符语法
# ---------------------------------------------------------------------------
# 协议 §9 第 12 行原文「需校验可渲染」没有定义占位符语法。
# 本包定义为 `{{field_name}}`（双花括号），理由：
#   · 单花括号 `{x}` 与 JSON 字面量、代码片段冲突（提示词里常有 JSON 示例）；
#   · `%s` / `$1` 与 psycopg 的 paramstyle 冲突（本项目已踩过，见 WP17 ISS-55）。
# 因此选一个**在提示词文本里几乎不会自然出现**的写法。
PLACEHOLDER_OPEN: Final[str] = "{{"
PLACEHOLDER_CLOSE: Final[str] = "}}"

# 字段名允许的字符：与 JSON 键的常见约定一致（小写字母/数字/下划线/点号，
# 点号用于表达嵌套路径，如 `product.model_code`）。
PLACEHOLDER_NAME_PATTERN: Final[str] = r"[A-Za-z_][A-Za-z0-9_.]*"

# ---------------------------------------------------------------------------
# 错误码
# ---------------------------------------------------------------------------
# 与既有包的 `REASON_*` 同一手法：错误码是**契约的一部分**，
# 前端按它分支，因此必须集中定义、不能散写成字符串字面量。
REASON_TEMPLATE_NOT_FOUND: Final[str] = "prompt_template_not_found"
REASON_VERSION_NOT_FOUND: Final[str] = "prompt_version_not_found"
REASON_TASK_KIND_INVALID: Final[str] = "task_kind_invalid"
REASON_NOT_RENDERABLE: Final[str] = "template_not_renderable"
REASON_TOOL_CEILING_EXCEEDED: Final[str] = "allowed_tools_exceed_ceiling"
REASON_TEMPLATE_EMPTY: Final[str] = "system_template_empty"
REASON_SCHEMA_NOT_OBJECT: Final[str] = "prompt_schema_not_object"
REASON_CHANGE_REASON_REQUIRED: Final[str] = "change_reason_required"
REASON_VERSION_MISMATCH: Final[str] = "prompt_version_template_mismatch"
REASON_PROPOSAL_SOURCE_REQUIRED: Final[str] = "proposal_source_required"
#: 模板里出现了系统安全指令标记（协议 §424 的第二个列举项）。
#: **单独一个码**，不与"超出上界"共用：两件事的处置完全不同 ——
#: 前者是"你试图覆盖系统指令"（可疑行为），后者是"你要的工具平台没授予"
#: （正常申请流程）。混在一个码里会让前端只能提示"权限问题"。
REASON_SYSTEM_DIRECTIVE_PRESENT: Final[str] = "system_directive_present"
#: 工具集**只能由平台侧路径修改**（`service.grant_tools`）。
#: 编辑路径收到工具集时的拒绝码 —— 见 `TemplateDraft` 的说明。
REASON_TOOLS_PLATFORM_ONLY: Final[str] = "allowed_tools_platform_managed"

# ---------------------------------------------------------------------------
# 值类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TemplateDraft:
    """创建一个模板身份 + 它的第一个版本。

    **没有 `allowed_tools` 字段**（这是本包的一处关键取舍）：

    协议 03 §9 第 12 行的入参原文是
    `template,inputSchema,outputSchema,changeReason` —— **没有工具集**。
    而同一张表连 `cursor,limit` 都逐个列了，因此这不是漏写。

    它与 §424 的原文严丝合缝：

    > 供应商密钥、系统安全指令及**允许工具列表由平台配置管理**，
    > 普通模板编辑**不能扩大工具权限或外部访问范围**。

    若把 `allowed_tools` 放进这个 draft，端点就会把它当请求体字段暴露出去 ——
    于是"平台配置管理"退化成"编辑者自己填"，而**上界校验仍然会通过**
    （只要填的值在上界内）。**校验通过 ≠ 权限没被扩大**：真正的越权是
    "编辑者拿到了本不该由他决定的那个决定权"，而不是"填了超界的值"。

    工具集的授予走 `service.grant_tools`（**平台侧路径**，与编辑路径分开）。
    新建模板的工具集是 `PLATFORM_DEFAULT_TOOLS`（空集，fail-closed）。
    """

    task_kind: str
    name: str
    system_template: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    change_reason: str


@dataclass(frozen=True, slots=True)
class VersionDraft:
    """给已有模板追加一个版本。

    **同样没有 `allowed_tools`** —— 理由见 `TemplateDraft`。

    追加版本时工具集**原样继承上一版**：这样"新版本"与"旧版本"的工具权限
    必然相同，从结构上排除了"顺手在新版本里加了出网工具"这条路径。
    要改工具集必须显式走 `grant_tools`（平台侧，会写一条自己的变更理由）。
    """

    system_template: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    change_reason: str


@dataclass(frozen=True, slots=True)
class RegisteredPromptVersion:
    """`register_version` 的结果在 Prompt 语境下的视图。

    `created=False` 表示**幂等命中**（同样的 payload 已注册过）——
    与 WP11 的 `RegisteredVersion.created` 同义。
    **不能靠 state 推断"是否首次"**（WP13 ISS-35 的教训）。
    """

    version_id: uuid.UUID
    template_id: uuid.UUID
    revision_no: int
    created: bool


@dataclass(frozen=True, slots=True)
class TemplateSummary:
    """`GET /prompts` 的一行。"""

    id: uuid.UUID
    task_kind: str
    name: str
    active_version_id: uuid.UUID | None
    active_revision_no: int | None
    version_count: int
    state: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TemplateDetail:
    """`GET /prompts` 的详情视图（含激活版本的内容）。"""

    id: uuid.UUID
    task_kind: str
    name: str
    active_version_id: uuid.UUID | None
    active_revision_no: int | None
    state: str
    created_at: datetime
    system_template: str | None
    input_schema: dict[str, Any] | None
    output_schema: dict[str, Any] | None
    allowed_tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ActivationResult:
    """一次激活的结果。

    `superseded_version_id` 是"被这次激活取代的那一版"——
    协议原文「激活，**记录影响范围**」的落点之一：
    没有它，"影响范围"就只剩"谁被激活了"，而查不出"谁因此失效"。
    """

    template_id: uuid.UUID
    version_id: uuid.UUID
    revision_no: int
    superseded_version_id: uuid.UUID | None
    already_active: bool
    affected_task_kinds: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProposalDraft:
    """一条知识候选（`knowledge_proposals` 的产生方）。

    **本包只生产 `source_feedback_id` 这一路**（闭合 WP20 的 OP-150 一半）：
    `source_experiment_id` 那一路依赖 `experiments` 表（WP41），
    现在填不出来 —— 登记为待定项。
    """

    target_asset_id: uuid.UUID | None
    proposed_payload: dict[str, Any]
    scope: str
    source_feedback_id: uuid.UUID | None
    reason: str


@dataclass(frozen=True, slots=True)
class CreatedProposal:
    proposal_id: uuid.UUID
    state: str


__all__ = [
    "MAX_CHANGE_REASON_CHARS",
    "MAX_NAME_CHARS",
    "MAX_PROMPT_VERSION_NO",
    "MAX_REASON_CHARS",
    "MAX_TEMPLATE_CHARS",
    "MIN_PUBLICATION_SEQUENCE",
    "PLACEHOLDER_CLOSE",
    "PLACEHOLDER_NAME_PATTERN",
    "PLACEHOLDER_OPEN",
    "PLATFORM_DEFAULT_TOOLS",
    "PLATFORM_TOOL_CEILING",
    "PROMPT_SCHEMA_VERSION",
    "PUBLICATION_ACTIONS",
    "PUBLICATION_ACTION_ACTIVATE",
    "PUBLICATION_ACTION_LIST_SQL",
    "PUBLICATION_ACTION_SUPERSEDE",
    "REASON_CHANGE_REASON_REQUIRED",
    "REASON_NOT_RENDERABLE",
    "REASON_PROPOSAL_SOURCE_REQUIRED",
    "REASON_SCHEMA_NOT_OBJECT",
    "REASON_SYSTEM_DIRECTIVE_PRESENT",
    "REASON_TASK_KIND_INVALID",
    "REASON_TEMPLATE_EMPTY",
    "REASON_TEMPLATE_NOT_FOUND",
    "REASON_TOOL_CEILING_EXCEEDED",
    "REASON_TOOLS_PLATFORM_ONLY",
    "REASON_VERSION_MISMATCH",
    "REASON_VERSION_NOT_FOUND",
    "TASK_BRIEF_DECOMPOSE",
    "TASK_BRIEF_ENRICH",
    "TASK_CONTENT_COPY",
    "TASK_CONTENT_STRUCTURE",
    "TASK_EVIDENCE_COLLECTION",
    "TASK_FACT_VERIFICATION",
    "TASK_KNOWLEDGE_SEARCH",
    "TASK_KINDS",
    "TASK_KIND_LIST_SQL",
    "TASK_QUALITY_CHECK",
    "TASK_REPORT_GENERATE",
    "TASK_STATIC_IMAGE",
    "TEMPLATE_STATE_ACTIVE",
    "TEMPLATE_STATE_DRAFT",
    "TEMPLATE_STATES",
    "TOOL_CEILING_ARRAY_SQL",
    "ActivationResult",
    "CreatedProposal",
    "ProposalDraft",
    "RegisteredPromptVersion",
    "TemplateDetail",
    "TemplateDraft",
    "TemplateSummary",
    "VersionDraft",
]
