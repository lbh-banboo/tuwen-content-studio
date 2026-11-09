"""Prompt 版本的业务编排（WP23）。

**本层是唯一对外入口**：端点只做参数解析与响应组装，全部判定在这里。
判定所需的数据由 `repository` 在**同一事务内**读出 —— 若调用方先读一次、
再传参进来，两次读之间就有窗口，并发时能构造出"校验通过但写入时前提已变"。

---

## 两条验收不变量在这里如何落地

验收原文：「**模板变更可追溯，不扩大工具权限**」。

| 不变量 | 本层的动作 |
|---|---|
| **可追溯** | 每个版本走 WP11 `register_version`（`ENTITY_TYPE_PROMPT`）→ `version_nodes` 里有节点、有 `revision_no`、有 `payload_hash`；每次激活写一条 `prompt_publications` 事件（含 `superseded_version_id` 与 `affected_task_kinds`） |
| **不扩大工具权限** | ① 编辑路径（`create_template` / `add_version`）**不接收工具集**，新版本继承上一版 → 结构上无法通过"发新版本"扩权；② 平台侧 `grant_tools` 走 `validate_allowed_tools`（子集 + 新增出网工具两条判据）；③ 数据库 CHECK 兜第三道 |

## 一个刻意的设计：校验顺序

`add_version` 的校验顺序是 **① 模板存在 → ② 可渲染 → ③ 系统指令 → ④ 工具上界**。
这个顺序不是随意的：

- 先判"模板存在"（404）而不是先判内容 —— 否则一个不存在的模板会返回
  "模板不可渲染"这种**指向错误方向**的错误消息；
- **② 在 ④ 之前**：可渲染是**内容本身**的问题（作者写错了），
  工具越界是**权限**问题。两件事都发生时，先说内容问题能让作者一次改对。

## ⚠️ 工具集**不在编辑路径上**（本包最容易做错的一处）

协议 03 §9 第 12 行的入参原文是：

    POST /prompts/{id}/versions  ←  template,inputSchema,outputSchema,changeReason

**没有 `allowedTools`**。而同一张表连 `cursor,limit` 都逐个列了，
因此这不是漏写 —— 它与 §424 的原文严丝合缝：

> 允许工具列表**由平台配置管理**，普通模板编辑**不能扩大工具权限**。

初稿曾把 `allowed_tools` 放进 `VersionDraft`，那等于把上界校验做完之后
**又把决定权交回编辑者** —— 而校验仍然会通过（只要填的值在上界内）。
**校验通过 ≠ 权限没被扩大**：真正的越权是"编辑者拿到了本不该由他决定的
那个决定权"。因此：

| 路径 | 入口 | 能否改工具集 |
|---|---|---|
| 编辑（协议端点） | `create_template` / `add_version` | **不能**。新版本**原样继承**上一版的工具集 |
| 平台侧 | `grant_tools`（本包新增，非协议端点） | 能，且必须给出理由、单独写一条版本 |

`grant_tools` 也走版本机制（它产生一个新版本）—— 这样"某模板在某个时间点
有哪些工具"在 `version_nodes` 里是可追溯的，而不是一个被就地改写的列。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from rednote.modules.prompts.render import check_renderable
from rednote.modules.prompts.repository import PromptRepository
from rednote.modules.prompts.tools_policy import (
    contains_system_directive,
    normalize_tools,
    validate_allowed_tools,
)
from rednote.modules.prompts.types import (
    MAX_PROMPT_VERSION_NO,
    PLATFORM_DEFAULT_TOOLS,
    PLATFORM_TOOL_CEILING,
    PUBLICATION_ACTION_ACTIVATE,
    PUBLICATION_ACTION_SUPERSEDE,
    REASON_CHANGE_REASON_REQUIRED,
    REASON_NOT_RENDERABLE,
    REASON_PROPOSAL_SOURCE_REQUIRED,
    REASON_SCHEMA_NOT_OBJECT,
    REASON_SYSTEM_DIRECTIVE_PRESENT,
    REASON_TASK_KIND_INVALID,
    REASON_TEMPLATE_EMPTY,
    REASON_TEMPLATE_NOT_FOUND,
    REASON_TOOL_CEILING_EXCEEDED,
    REASON_VERSION_MISMATCH,
    REASON_VERSION_NOT_FOUND,
    TASK_KINDS,
    TEMPLATE_STATE_ACTIVE,
    TEMPLATE_STATE_DRAFT,
    ActivationResult,
    CreatedProposal,
    ProposalDraft,
    RegisteredPromptVersion,
    TemplateDetail,
    TemplateDraft,
    TemplateSummary,
    VersionDraft,
)
from rednote.modules.versions.repository import VersionRepository
from rednote.modules.versions.service import register_version
from rednote.platform.errors import ApiError, ErrorCode

# 激活时的取号重试次数。与 WP11 的 `MAX_REGISTER_ATTEMPTS`、WP20 的
# `MAX_PUBLISH_ATTEMPTS` 同一手法：并发下撞唯一约束后重新取号。
# 3 次足够 —— 每次重试都重新读 max，而冲突窗口是微秒级。
MAX_PUBLISH_ATTEMPTS: int = 3

# `service.list_templates` 的默认取数上限。
#
# **刻意只保留 `DEFAULT_LIMIT`，没有 `MAX_LIMIT`**：上界（100）的权威定义在
# `platform.pagination.MAX_LIMIT`，由 `PageParamsDep` 的 `Query(le=MAX_LIMIT)`
# 强制执行。本模块再写一份 `MAX_LIMIT = 100` 会造出**第二个定义** ——
# 两处一旦不一致，**放宽的那一处会生效**（框架层的校验先执行）。
# 这是 ISS-66 的形态（同一常量两处各写一遍）。
DEFAULT_LIMIT: int = 20


def _validation_error(
    field: str, message: str, *, reason: str, **extra: Any
) -> ApiError:
    """构造 422 校验错误。与 WP11/WP18/WP20 同一形状。

    `details` 里带 `field` 与 `reason`：前端按 `reason` 分支（而不是匹配文案），
    文案会随本地化变化。
    """
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        message,
        details={"field": field, "reason": reason, **extra},
    )


def _not_found(message: str, *, reason: str) -> ApiError:
    return ApiError(
        ErrorCode.RESOURCE_NOT_FOUND,
        message,
        details={"reason": reason},
    )


def _require_task_kind(task_kind: str) -> None:
    """`task_kind` 必须是受控取值域内的值。

    **为什么在服务层也判一次**（数据库已有 CHECK）：CHECK 给出的是
    `sqlstate=23514`，用户看到的是"字段校验未通过"而不知道**合法值是什么**。
    服务层这一层的作用是给出**可操作的错误消息**（列出合法取值）。
    两层缺一不可，与 `allowed_tools` 同一理由。
    """
    if task_kind not in TASK_KINDS:
        raise _validation_error(
            "taskKind",
            f"taskKind 不在受控取值域内。合法取值：{', '.join(sorted(TASK_KINDS))}",
            reason=REASON_TASK_KIND_INVALID,
            allowed=sorted(TASK_KINDS),
        )


def _require_schemas(
    input_schema: dict[str, Any], output_schema: dict[str, Any]
) -> None:
    """两个 schema 必须是 object。

    **不判非空**（与 WP20 的 `structured_payload_not_empty` 刻意不同）：
    一个不需要输入字段的模板（例如纯格式转换）写 `{}` 是合法的。
    WP20 判非空是因为那里 `structured_payload` 承载"事实字段"，
    空对象等于没写事实 —— **语义不同，判据就不同**，这是刻意的。
    """
    for name, schema in (
        ("inputSchema", input_schema),
        ("outputSchema", output_schema),
    ):
        if not isinstance(schema, dict):
            raise _validation_error(
                name,
                f"{name} 必须是 JSON 对象。",
                reason=REASON_SCHEMA_NOT_OBJECT,
            )


def _require_template_text(system_template: str) -> None:
    if not system_template.strip():
        raise _validation_error(
            "template",
            "模板文本不能为空。",
            reason=REASON_TEMPLATE_EMPTY,
        )


def _require_change_reason(change_reason: str) -> None:
    """变更理由必填。

    验收原文「模板变更**可追溯**」的落点之一：版本不可变，
    这份理由是一经写入就无法补充的唯一解释 —— 允许为空会让
    "为什么又发了一版"永远没有答案。
    """
    if not change_reason.strip():
        raise _validation_error(
            "changeReason",
            "变更理由必填（模板版本不可变，理由是唯一的解释来源）。",
            reason=REASON_CHANGE_REASON_REQUIRED,
        )


def _require_tools(
    allowed_tools: tuple[str, ...], *, previous: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """校验并归一化工具集。返回归一化后的值。

    **只有平台侧路径（`grant_tools`）调用本函数** —— 编辑路径不接收工具集
    （见模块 docstring 的说明）。因此本函数是"工具集能否被写进库"的**唯一入口**，
    不是"两处共用的一份实现"（那种形态更容易漂移）。

    两条判据都在 `tools_policy.validate_allowed_tools`（单一实现）：
      ① `allowed_tools ⊆ PLATFORM_TOOL_CEILING`（子集关系，有反例，不是恒真）；
      ② 本次**新增**的出网工具（扩大外部访问范围）需要平台配置变更。
    """
    normalized = normalize_tools(allowed_tools)
    problems = validate_allowed_tools(normalized, previous=previous)
    if problems:
        raise _validation_error(
            "allowedTools",
            "；".join(problems),
            reason=REASON_TOOL_CEILING_EXCEEDED,
            ceiling=list(PLATFORM_TOOL_CEILING),
        )
    return normalized


def _require_renderable(
    system_template: str, input_schema: dict[str, Any]
) -> None:
    """「需校验可渲染」（协议 §9 第 12 行）的落点。

    判据见 `render.check_renderable`：占位符 ⊆ 声明字段。

    **未闭合占位符（`{{x`）也拒绝** —— 它几乎必然是作者写错了。
    默认从严：把"疑似"当"确定"处理，代价是作者多看一眼；
    反过来（放过一个写错的占位符）的代价是**生成时才炸**，那时已经烧了模型调用。
    """
    result = check_renderable(system_template, input_schema)
    problems = list(result.problems)
    if result.unclosed:
        problems.append(
            "模板存在未闭合的占位符（`{{` 没有对应的 `}}`），位置："
            + ", ".join(str(pos) for pos in result.unclosed)
        )
    if problems:
        raise _validation_error(
            "template",
            "；".join(problems),
            reason=REASON_NOT_RENDERABLE,
            undeclared=list(result.undeclared),
            unclosed=list(result.unclosed),
        )


def _require_no_system_directive(system_template: str) -> None:
    """模板文本里不得出现系统安全指令标记。

    协议 §9 第 424 行：「系统安全指令及允许工具列表由平台配置管理，
    普通模板编辑**不能扩大工具权限或外部访问范围**」。
    系统安全指令由平台在调用时注入，**不在模板里** ——
    模板里出现它说明有人在尝试覆盖它。

    **这不是完整的注入防护**（那是 WP30 的 AI 自检范围）。
    它的作用是让越界尝试**显式失败**而不是静默通过。
    """
    if contains_system_directive(system_template):
        raise _validation_error(
            "template",
            "模板不得包含系统安全指令标记（它由平台配置管理）。",
            reason=REASON_SYSTEM_DIRECTIVE_PRESENT,
        )


# ---------------------------------------------------------------------------
# 读路径
# ---------------------------------------------------------------------------


def get_template_detail(
    repo: PromptRepository, *, template_id: uuid.UUID
) -> TemplateDetail:
    """取模板详情（含激活版本的内容）。

    **本函数目前没有端点调用** —— 协议里没有"取单个模板详情"的端点
    （§9 第 11 行的入参只有 `taskKind,cursor,limit`，没有 `promptId`），
    本包的列表端点把详情折进了列表项（见 `api` 模块 docstring 第三节）。

    保留它是因为它是**列表端点组装逻辑的一份可测替身**：单测可以
    直接断言"激活版本的内容取自哪一版"，而不必起 HTTP。
    **若收口时决定加详情端点，它已经是现成的**（但要补 HTTP 测试）。

    **`active_version_id` 为 NULL 时四个内容字段也是 NULL** ——
    不返回"最近一版"的内容：那会让前端显示一个**尚未生效**的模板，
    而用户以为它在用（这是"看起来有内容、实际不该有"的形态）。
    """
    template = repo.get_template(template_id)
    if template is None:
        raise _not_found("提示词模板不存在。", reason=REASON_TEMPLATE_NOT_FOUND)

    version = None
    if template.active_version_id is not None:
        version = repo.get_version(template.active_version_id)

    return TemplateDetail(
        id=template.id,
        task_kind=template.task_kind,
        name=template.name,
        active_version_id=template.active_version_id,
        active_revision_no=_active_revision_no(repo, template.active_version_id),
        state=_template_state(template.active_version_id),
        created_at=template.created_at,
        system_template=version.system_template if version else None,
        input_schema=version.input_schema if version else None,
        output_schema=version.output_schema if version else None,
        allowed_tools=tuple(version.allowed_tools) if version else (),
    )


def _active_revision_no(
    repo: PromptRepository, active_version_id: uuid.UUID | None
) -> int | None:
    """当前生效版本的修订号（无生效版本时为 None）。

    **在服务层算好，不让端点补**：先前 `service` 把这个字段恒填 `None`，
    由 `api` 层再用裸 SQL 补上 —— 于是"这个字段是什么"取决于端点有没有
    记得补，而 `api` 层还因此知道了表名。收敛到这里之后，
    `TemplateSummary` / `TemplateDetail` 两个值类型的该字段都**必然已填**。
    """
    if active_version_id is None:
        return None
    return repo.revision_no_of(active_version_id)


def _template_state(active_version_id: uuid.UUID | None) -> str:
    """模板状态是**算出来的**（投影），不是存在某一列上被就地改写的。

    与 WP20 的 `_project_asset` 同一手法：状态由事件流派生，
    这样"模板有没有激活版本"只有一个答案来源。
    """
    return TEMPLATE_STATE_ACTIVE if active_version_id else TEMPLATE_STATE_DRAFT


def list_templates(
    repo: PromptRepository,
    *,
    task_kind: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[TemplateSummary], int]:
    """列出模板摘要 + 总数。

    `task_kind` 显式传 None 表示不过滤；传了值则**先校验取值合法** ——
    不校验的话 `?taskKind=拼错` 会静默返回空列表，
    用户以为"没有这个模板"，实际是"拼错了"。
    """
    if task_kind is not None:
        _require_task_kind(task_kind)

    rows = repo.list_templates(
        task_kind=task_kind, limit=limit, offset=offset
    )
    total = repo.count_templates(task_kind=task_kind)

    summaries: list[TemplateSummary] = []
    for row in rows:
        summaries.append(
            TemplateSummary(
                id=row.id,
                task_kind=row.task_kind,
                name=row.name,
                active_version_id=row.active_version_id,
                active_revision_no=_active_revision_no(
                    repo, row.active_version_id
                ),
                version_count=repo.count_versions(template_id=row.id),
                state=_template_state(row.active_version_id),
                created_at=row.created_at,
            )
        )
    return summaries, total


# ---------------------------------------------------------------------------
# 写路径
# ---------------------------------------------------------------------------


def create_template(
    repo: PromptRepository,
    version_repo: VersionRepository,
    *,
    draft: TemplateDraft,
    created_by: uuid.UUID | None,
) -> RegisteredPromptVersion:
    """建模板身份 + 第一个版本，**同一事务**。

    **为什么一次建两个**：协议 §9 第 12 行只有"给模板加版本"，
    **没有"建模板"端点**（见说明文档 §1.4 疏漏 #8）。
    而 `prompt_templates` 与 `prompt_versions` 的关系是"没有版本的模板"
    是一个没有内容可用的壳 —— 它除了占一个名字没有任何作用。
    因此本包把"建模板"实现为**第一个版本的副作用**：
    模板身份在第一次加版本时隐式创建。

    这样做的另一个好处：`UQ(task_kind, name)` 让"同名重复创建"能幂等命中
    （返回既有模板的新版本），而不是报一个"名字已占用"的错误 ——
    后者在重试场景下会让调用方困惑。

    **工具集固定为 `PLATFORM_DEFAULT_TOOLS`（空集）**：
    `TemplateDraft` 没有这个字段（见其说明）。新模板在平台明确授予之前
    **一个工具都不能用** —— fail-closed。
    """
    _require_task_kind(draft.task_kind)
    _require_schemas(draft.input_schema, draft.output_schema)
    _require_template_text(draft.system_template)
    _require_change_reason(draft.change_reason)
    _require_renderable(draft.system_template, draft.input_schema)
    _require_no_system_directive(draft.system_template)

    template = repo.find_template_by_name(
        task_kind=draft.task_kind, name=draft.name
    )
    if template is None:
        template = repo.insert_template(
            task_kind=draft.task_kind, name=draft.name
        )

    return _append_version(
        repo,
        version_repo,
        template_id=template.id,
        system_template=draft.system_template,
        input_schema=draft.input_schema,
        output_schema=draft.output_schema,
        allowed_tools=PLATFORM_DEFAULT_TOOLS,
        change_reason=draft.change_reason,
        created_by=created_by,
    )


def add_version(
    repo: PromptRepository,
    version_repo: VersionRepository,
    *,
    template_id: uuid.UUID,
    draft: VersionDraft,
    created_by: uuid.UUID | None,
) -> RegisteredPromptVersion:
    """给已有模板追加一个版本。

    校验顺序见模块 docstring（模板存在 → 可渲染 → 系统指令）。

    **工具集原样继承上一版**（`VersionDraft` 没有这个字段）：
    这是"不扩大工具权限"最直接的一条落地 —— **结构上不存在**
    "发个新版本顺便加个工具"这条路径。要改工具集必须走
    `grant_tools`（平台侧，单独一条路径 + 单独的理由）。
    """
    template = repo.get_template(template_id)
    if template is None:
        raise _not_found("提示词模板不存在。", reason=REASON_TEMPLATE_NOT_FOUND)

    _require_schemas(draft.input_schema, draft.output_schema)
    _require_template_text(draft.system_template)
    _require_change_reason(draft.change_reason)
    _require_renderable(draft.system_template, draft.input_schema)
    _require_no_system_directive(draft.system_template)

    previous_row = repo.latest_version(template_id=template_id)
    # 一个模板必然至少有一版（身份随第一版创建）—— 但 `latest_version`
    # 返回 None 的可能性存在（数据回填只建了模板身份）。此时给空集，
    # **不给全集**：fail-closed。
    inherited = (
        tuple(previous_row.allowed_tools)
        if previous_row is not None
        else PLATFORM_DEFAULT_TOOLS
    )

    return _append_version(
        repo,
        version_repo,
        template_id=template_id,
        system_template=draft.system_template,
        input_schema=draft.input_schema,
        output_schema=draft.output_schema,
        allowed_tools=inherited,
        change_reason=draft.change_reason,
        created_by=created_by,
    )


def grant_tools(
    repo: PromptRepository,
    version_repo: VersionRepository,
    *,
    template_id: uuid.UUID,
    allowed_tools: tuple[str, ...],
    change_reason: str,
    granted_by: uuid.UUID | None,
) -> RegisteredPromptVersion:
    """**平台侧**路径：给模板授予一组工具（产生一个新版本）。

    ## 为什么它必须存在，而不是"干脆不支持改工具"

    完全不支持改工具集会让 `PLATFORM_DEFAULT_TOOLS`（空集）成为永久状态 ——
    于是任何模板都用不了工具，等于把"允许工具列表由平台配置管理"实现成
    "没有任何工具"。**平台配置管理是一个真实的需求**，只是它不该在编辑路径上。

    ## 为什么它产生**新版本**，而不是就地改某一版的列

    `prompt_versions` 是不可变表（触发器拦 UPDATE）。这不是障碍而是**正确性**：
    "某模板在某个时间点有哪些工具"必须是可追溯的 —— 若就地改写，
    一次"临时给某模板开了出网"会**抹掉**它之前没有出网这件事，
    于是事后无法回答"那次生成时它能不能出网"。

    ## 为什么 `change_reason` 必填

    与编辑路径同一条理由：版本不可变，这份理由是一经写入就无法补充的
    唯一解释。工具集变更比提示词文本变更**更需要解释** ——
    文本改动影响输出质量，工具改动影响**系统能触达什么**。

    ## 与编辑路径的关系

    两条路径都写 `prompt_versions` 行，但**工具集的来源不同**：
    编辑路径继承，平台路径显式给。**`_require_tools` 只在这里被调用** ——
    因此"工具集能否落库"只有一个入口，不存在两处实现漂移的可能。
    """
    template = repo.get_template(template_id)
    if template is None:
        raise _not_found("提示词模板不存在。", reason=REASON_TEMPLATE_NOT_FOUND)

    _require_change_reason(change_reason)

    previous_row = repo.latest_version(template_id=template_id)
    if previous_row is None:
        raise _validation_error(
            "templateId",
            "模板还没有任何版本，无法单独授予工具（先建第一个版本）。",
            reason=REASON_VERSION_NOT_FOUND,
        )
    previous = tuple(previous_row.allowed_tools)
    tools = _require_tools(allowed_tools, previous=previous)

    # 工具集没变时**不产生新版本**：否则一次误操作的"重新授予"
    # 会在版本历史里留下一个与上一版完全相同的内容 —— 而历史里的
    # 每一版都应该对应一次真实的变更。
    if tools == previous:
        return RegisteredPromptVersion(
            version_id=previous_row.id,
            template_id=template_id,
            revision_no=_revision_no_of(repo, previous_row.id),
            created=False,
        )

    return _append_version(
        repo,
        version_repo,
        template_id=template_id,
        system_template=previous_row.system_template,
        input_schema=previous_row.input_schema,
        output_schema=previous_row.output_schema,
        allowed_tools=tools,
        change_reason=change_reason,
        created_by=granted_by,
    )


def _version_payload(
    *,
    template_id: uuid.UUID,
    system_template: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    allowed_tools: tuple[str, ...],
) -> dict[str, Any]:
    """构造交给 `register_version` 的 payload（它据此算 `payload_hash`）。

    **这是一份内容指纹，字段顺序与内容都必须稳定**：`payload_hash` 用于
    "同一修订号重复注册时内容是否一致"的幂等判定 —— 若同一个版本两次
    注册算出不同的 hash，幂等会失效并凭空多出一个版本。

    因此 `allowed_tools` 转成 `list`（JSON 没有 tuple），
    且**不做排序**：顺序是内容的一部分（`text[]` 也按顺序存），
    排序会让"把工具顺序换一下"这个真实变更被判为幂等命中。

    **单独成函数**是为了让"什么算这一版的内容"只有一处定义 ——
    调用方（`create_template` / `add_version` / `grant_tools`）
    都通过它构造，没有第二处可以漂移。
    """
    return {
        "templateId": str(template_id),
        "systemTemplate": system_template,
        "inputSchema": input_schema,
        "outputSchema": output_schema,
        "allowedTools": list(allowed_tools),
    }


def _require_existing_version_belongs(
    repo: PromptRepository, *, node_id: uuid.UUID, template_id: uuid.UUID
) -> None:
    """幂等命中时，核对"已存在的那一版"确实属于本模板。

    **为什么必须核对**：`register_version` 判定为幂等命中时，
    领域行**已存在且不可变**，本层不重写它。但调用方拿到的是一个
    `version_id`，它会以为"这是我刚创建的版本" —— 若那一版其实属于
    别的模板（修订号在不同实体间理论上不该撞，但注册表的幂等键
    是 `(entity_type, entity_id, revision_no)`，绕过 service 的写入
    可以造出不一致），调用方后续用它做激活就会失败在一个
    离这里很远的地方。

    报出来比静默返回一个"不属于你的 version_id"好。
    """
    existing = repo.get_version(node_id)
    if existing is None or existing.template_id != template_id:
        raise _validation_error(
            "templateId",
            "该修订号已被其它模板的版本占用。",
            reason=REASON_VERSION_MISMATCH,
        )


def _append_version(
    repo: PromptRepository,
    version_repo: VersionRepository,
    *,
    template_id: uuid.UUID,
    system_template: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    allowed_tools: tuple[str, ...],
    change_reason: str,
    created_by: uuid.UUID | None,
) -> RegisteredPromptVersion:
    """注册版本节点 + 写领域行（**同一事务**）。

    `register_version` 的 `entity_id` 是**模板 id**（不是版本 id）——
    这是 WP18/WP20 都踩过的坑（把 `entity_id` 传成自己生成的 uuid，
    导致 `version_nodes` 的外键失败）。返回的 `node_id` 才是版本 id。

    **两条分支都是"不重写已有一行"**：`created=True` 时插入新领域行；
    `created=False`（幂等命中）时领域行已在，只核对归属。
    `prompt_versions` 表级不可变，重写会被触发器拦下 ——
    但**在那之前**本层就不该去写（不可变表的正确用法是"不产生 UPDATE"，
    而不是"产生 UPDATE 然后被拦"）。
    """
    registered = register_version(
        version_repo,
        entity_type="PROMPT",
        entity_id=template_id,
        payload=_version_payload(
            template_id=template_id,
            system_template=system_template,
            input_schema=input_schema,
            output_schema=output_schema,
            allowed_tools=allowed_tools,
        ),
        created_by=created_by,
    )

    if registered.revision_no > MAX_PROMPT_VERSION_NO:  # pragma: no cover
        raise RuntimeError("修订号超出 Prompt 版本上界，检查常量")

    if registered.created:
        repo.insert_version(
            version_id=registered.node_id,
            template_id=template_id,
            system_template=system_template,
            input_schema=input_schema,
            output_schema=output_schema,
            allowed_tools=allowed_tools,
            change_reason=change_reason,
            created_by=created_by,
        )
    else:
        _require_existing_version_belongs(
            repo, node_id=registered.node_id, template_id=template_id
        )

    return RegisteredPromptVersion(
        version_id=registered.node_id,
        template_id=template_id,
        revision_no=registered.revision_no,
        created=registered.created,
    )


def publish(
    repo: PromptRepository,
    *,
    template_id: uuid.UUID,
    version_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    reason: str | None,
) -> ActivationResult:
    """激活某版本（协议 §9 第 13 行：「激活，**记录影响范围**」）。

    **三件事在同一事务里**：
      1. 写一条 `ACTIVATE` 事件（含 `affected_task_kinds` = 影响范围）；
      2. 若上一版存在且不同，写一条 `SUPERSEDE` 事件；
      3. 条件 UPDATE 切换 `active_version_id`。

    **幂等**：目标版本已是当前版本时直接返回 `already_active=True`，
    **不写重复事件**（重复事件会让"这次激活影响了什么"在历史里
    出现两条一模一样的记录）。

    **并发**：入口 `get_template_for_update` 先加行锁，让同模板的并发激活
    在**取号之前**串行化 —— 后到的请求会阻塞到前一个提交，然后读到新的
    `active_version_id`。取号与写事件的细节见 `_write_activation_events`。
    """
    template = repo.get_template_for_update(template_id)
    if template is None:
        raise _not_found("提示词模板不存在。", reason=REASON_TEMPLATE_NOT_FOUND)

    version = repo.get_version(version_id)
    if version is None:
        raise _not_found(
            "提示词版本不存在。", reason=REASON_VERSION_NOT_FOUND
        )
    if version.template_id != template_id:
        raise _validation_error(
            "versionId",
            "该版本不属于此模板。",
            reason=REASON_VERSION_MISMATCH,
        )

    affected = (template.task_kind,)

    if template.active_version_id == version_id:
        return ActivationResult(
            template_id=template_id,
            version_id=version_id,
            revision_no=_revision_no_of(repo, version_id),
            superseded_version_id=None,
            already_active=True,
            affected_task_kinds=affected,
        )

    superseded = _write_activation_events(
        repo,
        template_id=template_id,
        version_id=version_id,
        affected=affected,
        actor_id=actor_id,
        reason=reason,
    )

    repo.set_active_version(template_id=template_id, version_id=version_id)
    repo.flush()

    return ActivationResult(
        template_id=template_id,
        version_id=version_id,
        revision_no=_revision_no_of(repo, version_id),
        superseded_version_id=superseded,
        already_active=False,
        affected_task_kinds=affected,
    )


def _write_activation_events(
    repo: PromptRepository,
    *,
    template_id: uuid.UUID,
    version_id: uuid.UUID,
    affected: tuple[str, ...],
    actor_id: uuid.UUID | None,
    reason: str | None,
) -> uuid.UUID | None:
    """写一对激活事件（`ACTIVATE` + 可选的 `SUPERSEDE`），返回被取代的版本。

    撞 `uq_prompt_publications_template_sequence` 时**重新取号重试** ——
    `SAVEPOINT` 回滚后不重试会把整个事务打成 aborted。

    ## 每次尝试都重读 `active_version_id`（这是本函数存在的第二个理由）

    它决定 `SUPERSEDE` 事件的 `version_id`。重试意味着"序号被别人抢先取了"，
    那么"当前生效的是谁"**也可能同时变了** —— 若沿用进入循环前读到的旧值，
    会写出一条**指向错误版本**的 SUPERSEDE 事件（声称取代了 v1、
    实际取代的是 v2），而这条事件是审计链的一部分，写错就是静默的历史失真。

    重读用 `reload_template`（**不加锁**）：调用方 `publish` 已通过
    `get_template_for_update` 持有该行的 `FOR UPDATE` 锁直到提交，
    因此重读拿到的是本事务视角下的最新值，不会与别的写者竞争。

    ## 调用方前提

    入口已确认"目标版本不是当前版本"。由于行锁把同模板的并发激活串行化，
    本函数内重读到的 `superseded` 不会等于 `version_id`。
    真发生那种情况（只可能来自不遵守本协议的裸 SQL 写入）时，
    写出的是一条"自己取代自己"的事件 —— 语义退化但**不会**污染
    `active_version_id`（那由调用方在事件写完后切换）。
    """
    for _attempt in range(MAX_PUBLISH_ATTEMPTS):
        current = repo.reload_template(template_id)
        superseded = current.active_version_id if current is not None else None
        try:
            with repo.session.begin_nested():
                sequence = repo.next_sequence(template_id=template_id)
                repo.insert_publication(
                    template_id=template_id,
                    version_id=version_id,
                    action=PUBLICATION_ACTION_ACTIVATE,
                    sequence=sequence,
                    superseded_version_id=superseded,
                    affected_task_kinds=affected,
                    actor_id=actor_id,
                    reason=reason,
                )
                if superseded is not None:
                    repo.insert_publication(
                        template_id=template_id,
                        version_id=superseded,
                        action=PUBLICATION_ACTION_SUPERSEDE,
                        sequence=sequence + 1,
                        superseded_version_id=None,
                        affected_task_kinds=affected,
                        actor_id=actor_id,
                        reason=reason,
                    )
            return superseded
        except IntegrityError:
            # 撞 `uq_prompt_publications_template_sequence` —— 并发激活。
            # SAVEPOINT 已回滚，下一轮重新读当前版本并重新取号。
            continue

    raise ApiError(  # pragma: no cover - 极端并发
        ErrorCode.IDEMPOTENCY_CONFLICT,
        "激活事件序号冲突，请重试。",
        details={"reason": "publication_sequence_conflict"},
    )


def _revision_no_of(repo: PromptRepository, version_id: uuid.UUID) -> int:
    """取某个版本节点的 `revision_no`。

    薄封装，仅为让调用处读起来短一些。**真正的实现在仓储层**
    （`PromptRepository.revision_no_of`）—— 这里不再自写查询：
    先前 `service` 与 `api` 各写了一份同样的 `text()`，两处一旦漂移，
    同一响应里的 `revisionNo` 会来自两个不同的判据（ISS-66 的形态）。
    """
    return repo.revision_no_of(version_id)


def rebuild_active_version(
    repo: PromptRepository, *, template_id: uuid.UUID
) -> uuid.UUID | None:
    """从事件流**重算** `active_version_id`（投影的可重建性）。

    **为什么需要它**：`active_version_id` 是一列投影，它可以被裸 SQL
    就地改写。本函数提供"从权威来源重算"的能力 ——
    测试会验证"重算结果与增量维护的结果一致"，
    这是"投影列不是权威"这句话的**可执行证据**（WP20 同一手法）。

    规则：按 `sequence` 升序取事件流，最后一条 `ACTIVATE` 的 `version_id`
    即当前生效版本；`SUPERSEDE` 不改 `active_version`（它只记录"谁被取代"）。
    """
    events = repo.list_publications(template_id=template_id, limit=10_000)
    active: uuid.UUID | None = None
    for event in events:
        if event.action == PUBLICATION_ACTION_ACTIVATE:
            active = event.version_id
    return active


def create_proposal(
    repo: PromptRepository,
    *,
    draft: ProposalDraft,
    actor_id: uuid.UUID | None,
) -> CreatedProposal:
    """产生一条知识候选（闭合 WP20 的 OP-150 的**产生方**一半）。

    **本包只做这一条路径**（`source_feedback_id`）：
    `source_experiment_id` 那一路依赖 `experiments` 表（WP41），
    现在填不出来 —— 登记为待定项。

    **为什么不直接生效**：规格源 02 §165 原文"**AI 创建待确认，
    不直接生效**"。候选永远停在 `PENDING`，生效必须走 WP20 的
    `decide_proposal`（ACCEPT）—— 那是**人工确认**的落点。
    本函数刻意**不提供**任何"直接置为 ACCEPTED"的参数：
    提供它等于给"绕过人工确认"留了一个入口。

    ## `actor_id` 只用于审计，**不写进候选表**

    见 `repository.insert_proposal` 的说明：`knowledge_proposals` 的
    `confirmed_by` 是"**确认人**"，而 `PENDING` 状态上有一条 CHECK
    要求它为 NULL。把产生方写进去会让每次产生都撞约束。

    因此本函数的 `actor_id` 目前**不落库** —— 它是"谁触发的"这一信息的
    传递通道，正确落点是 WP15 的审计日志（由端点侧写）。
    参数保留而不是删掉，是为了让签名在"表补了 `created_by` 列"之后
    不需要改（改签名会波及端点与测试）。
    """
    if draft.source_feedback_id is None:
        raise _validation_error(
            "sourceFeedbackId",
            "候选必须说明来源（当前只支持由反馈产生）。",
            reason=REASON_PROPOSAL_SOURCE_REQUIRED,
        )

    del actor_id  # 见 docstring：正确落点是审计日志，不是候选表。

    row = repo.insert_proposal(
        target_asset_id=draft.target_asset_id,
        proposed_payload=draft.proposed_payload,
        scope=draft.scope,
        source_feedback_id=draft.source_feedback_id,
    )
    return CreatedProposal(proposal_id=row.id, state=row.state)


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_PUBLISH_ATTEMPTS",
    "add_version",
    "create_proposal",
    "create_template",
    "get_template_detail",
    "grant_tools",
    "list_templates",
    "publish",
    "rebuild_active_version",
]
