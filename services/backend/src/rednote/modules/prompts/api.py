"""Prompt 端点（协议 03 §9 第 11~13 行 + 两个本包补充端点）。

| 方法 | 路径 | 成功码 | 依据 |
|---|---|---|---|
| GET | `/prompts` | 200 | §9 第 11 行 |
| POST | `/prompts/{promptId}/versions` | 201 | §9 第 12 行 |
| POST | `/prompts/{promptId}/publications` | 200 | §9 第 13 行 |
| POST | `/prompts` | 201 | **本包补充**（协议无"建模板"端点，见下） |
| POST | `/prompts/{promptId}/tool-grants` | 200 | **本包补充**（平台侧工具授予，见下） |
| POST | `/knowledge/proposals` | 201 | **本包补充**（闭合 WP20 的 OP-150 产生方） |

---

## 一、为什么多了三个端点（协议里没有）

三个都不是"顺手加的"，每一个对应协议里的**一处空缺**，且都已在
说明文档 §1.4 登记：

| 端点 | 空缺 | 不加的后果 |
|---|---|---|
| `POST /prompts` | 协议 §9 第 12 行只有"给模板加版本"，**没有"建模板"**（疏漏 #8） | `prompt_templates` 有 `name` 却没有任何端点能设置它 —— 模板只能靠"加版本"隐式创建，而调用方**不知道模板 id**（还没建）。于是第一个版本永远加不上 |
| `POST /prompts/{id}/tool-grants` | §424 说工具列表"由平台配置管理"，但协议没给平台侧入口 | 工具集只能靠编辑路径设置 → 等于"平台配置管理"是句空话（见 `types.TemplateDraft` 的说明） |
| `POST /knowledge/proposals` | WP20 明确登记 OP-150："候选的**生产者**不在本包" | `GET /knowledge/proposals` 永远返回空列表 —— 一个只能读、不能写的资源 |

**"多三个端点"与"路径参数用 `{promptId}` 而不是 `{id}`"都是规格不一致**，
已在说明文档与登记册里显式记录，收口时由主 agent 与前端确认。

## 二、权限：读要登录，写要 `KNOWLEDGE_MANAGE`

协议 §424 原文「Prompt **全员可管理**沿用共享知识策略」→
写端点复用 `require_org_action(OrgAction.KNOWLEDGE_MANAGE)`
（与知识同一口径，**不新建动作**，理由见 `dependencies.py`）。

**读端点也要登录**（`CurrentUserDep`）：模板列表含 `system_template` 的
任务类型与名称，是内部配置信息，不是公网内容。

## 三、`GET /prompts` 的响应形状：摘要 + 详情二合一

协议 §9 第 11 行只写"模板摘要"，而入参只有 `taskKind,cursor,limit` ——
**没有 `promptId`**。也就是说协议里**没有"取单个模板详情"的端点**。

因此本实现把"详情"折进列表项：每一项带 `activeRevisionNo` 与
`allowedTools`（激活版本的工具集）。理由：

- 下游（WP26/WP30）要按 `taskKind` 取激活版本来组装生成上下文 ——
  它需要的是 `activeVersionId`，列表里就有；
- 而"这一版的模板文本是什么"用 `GET /prompts?taskKind=X` 也能拿到
  （`systemTemplate` 字段），不需要第二个端点。

**代价已登记**：列表项比"纯摘要"重（带模板文本，单条可达 20KB）。
若将来模板数量大，正确的做法是**加一个 `GET /prompts/{promptId}`
取详情、把文本从列表项里摘掉** —— 那是协议修订，属主 agent 收口事项。

## 四、`If-Match` 的取舍（与 WP20 同一处理）

协议 §9 第 13 行没有写 `If-Match`（只有第 12 行在协议原文里提了前置条件
的一般要求）。本实现**接收并校验它，但不强制要求**（缺省放行）——
理由与 WP20 完全一致：本包的写对象是**不可变的版本表与只追加的事件表**，
没有"读出来、改几个字段、写回去"的路径，因此没有旧值要携带。

真正的并发保护是 `prompt_publications` 的 `UQ(template_id, sequence)`
+ 重试（见 `service.publish`）—— 它是**真正的**串行化点。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field

from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.prompts import service
from rednote.modules.prompts.dependencies import PromptRepoDep, VersionRepoDep
from rednote.modules.prompts.types import (
    PLATFORM_TOOL_CEILING,
    TASK_KINDS,
    ProposalDraft,
    TemplateDraft,
    VersionDraft,
)
from rednote.modules.projects.dependencies import require_org_action
from rednote.modules.projects.permissions import OrgAction
from rednote.platform.api_responses import READ_RESPONSES, WRITE_RESPONSES
from rednote.platform.errors import DataEnvelope
from rednote.platform.pagination import (
    CursorSpec,
    PageParamsDep,
    PaginatedEnvelope,
    list_response,
    paginate_items,
    single_response,
)

router = APIRouter(tags=["prompts"])

# 列表端点的**全量扫描上限**。
#
# 见 `list_prompts` 里的说明：内存分页必须拿到全量数据，否则会静默截断。
# 上限存在的意义只是防止"表被意外撑到十万条"时把内存打满 ——
# 它不是分页参数。真正的修法是把锚点下推到 SQL（已登记待定项）。
_FULL_SCAN_LIMIT: int = 10_000

PROMPTS_READ_RESPONSES = READ_RESPONSES
PROMPTS_WRITE_RESPONSES = WRITE_RESPONSES

# 写端点的权限依赖。**读端点不用它**（读只需登录）。
#
# 必须包 `Depends(...)`：`require_org_action` 返回的是一个**依赖函数**，
# 而 `dependencies=[...]` 收的是"依赖声明对象"。直接传函数会报
# `AttributeError: 'function' object has no attribute 'dependency'`（本机实测）。
PROMPTS_WRITE = [Depends(require_org_action(OrgAction.KNOWLEDGE_MANAGE))]


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class CreateTemplateBody(BaseModel):
    """`POST /prompts` 的请求体（协议无此端点，字段对齐 §9 第 12 行 + 疏漏 #8）。

    **没有 `allowedTools`** —— 与 `types.TemplateDraft` 同一条理由：
    工具集由平台配置管理，编辑者没有传它的入口。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    task_kind: str = Field(alias="taskKind")
    name: str
    template: str
    input_schema: dict[str, Any] = Field(alias="inputSchema")
    output_schema: dict[str, Any] = Field(alias="outputSchema")
    change_reason: str = Field(alias="changeReason")


class AddVersionBody(BaseModel):
    """`POST /prompts/{promptId}/versions` 的请求体（协议 §9 第 12 行的四个字段）。

    **没有 `templateId`**（在路径里），**没有 `allowedTools`**（平台管理），
    **没有 `name`**（改名走管理后台，见疏漏 #8）。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    template: str
    input_schema: dict[str, Any] = Field(alias="inputSchema")
    output_schema: dict[str, Any] = Field(alias="outputSchema")
    change_reason: str = Field(alias="changeReason")


class PublicationBody(BaseModel):
    """`POST /prompts/{promptId}/publications` 的请求体（§9 第 13 行）。

    **没有 `action` 字段**（与知识的 `PublicationBody` 刻意不同）：
    协议原文只有"激活"一个动作，本包的动作集是 `ACTIVATE`/`SUPERSEDE`，
    而 `SUPERSEDE` 是 `ACTIVATE` 的**副作用**不是客户端可选动作 ——
    让它可选会出现"当前版本被标成取代、但没有版本接过它"的空窗。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    version_id: uuid.UUID = Field(alias="versionId")
    reason: str | None = None


class ToolGrantBody(BaseModel):
    """`POST /prompts/{promptId}/tool-grants` 的请求体（**平台侧**）。

    `allowedTools` 是**完整的目标集合**（不是增量）——
    增量语义会让"当前有哪些工具"取决于操作历史，而"把 A 换成 B"
    用两次增量表达时顺序会影响结果。完整集合让每次授予都是幂等的。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    allowed_tools: list[str] = Field(alias="allowedTools")
    change_reason: str = Field(alias="changeReason")


class CreateProposalBody(BaseModel):
    """`POST /knowledge/proposals` 的请求体（闭合 WP20 的 OP-150 产生方）。

    **没有 `state` 字段**：候选永远以 `PENDING` 创建（规格源 02 §165
    原文"AI 创建待确认，不直接生效"）。给它一个可传的 `state`
    等于给"绕过人工确认"留一个入口。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    target_asset_id: uuid.UUID | None = Field(default=None, alias="targetAssetId")
    proposed_payload: dict[str, Any] = Field(alias="proposedPayload")
    scope: str | None = None
    source_feedback_id: uuid.UUID | None = Field(
        default=None, alias="sourceFeedbackId"
    )
    reason: str = ""


# ---------------------------------------------------------------------------
# 响应体（供 OpenAPI 契约生成具体结构，而不是裸 object）
# ---------------------------------------------------------------------------


class TemplateSummaryBody(BaseModel):
    """`GET /prompts` 的一行（协议 §9 第 11 行"模板摘要"）。

    见模块 docstring 第三节：本项**比纯摘要重** —— 它带激活版本的
    模板文本与工具集，因为协议里没有"取单个模板详情"的端点。
    """

    model_config = ConfigDict(populate_by_name=True)

    id: uuid.UUID
    task_kind: str = Field(alias="taskKind")
    name: str
    active_version_id: uuid.UUID | None = Field(alias="activeVersionId")
    active_revision_no: int | None = Field(alias="activeRevisionNo")
    version_count: int = Field(alias="versionCount")
    state: str
    #: 激活版本的模板文本（无激活版本时为 None）。
    system_template: str | None = Field(alias="systemTemplate")
    input_schema: dict[str, Any] | None = Field(alias="inputSchema")
    output_schema: dict[str, Any] | None = Field(alias="outputSchema")
    #: 激活版本的工具集。**空列表 = 平台未授予任何工具**（fail-closed）。
    allowed_tools: list[str] = Field(alias="allowedTools")
    created_at: str = Field(alias="createdAt")


class CreatedTemplateBody(BaseModel):
    """`POST /prompts` 的 201 响应。"""

    model_config = ConfigDict(populate_by_name=True)

    prompt_id: uuid.UUID = Field(alias="promptId")
    version_id: uuid.UUID = Field(alias="versionId")
    revision_no: int = Field(alias="revisionNo")
    state: str


class CreatedVersionBody(BaseModel):
    """`POST /prompts/{promptId}/versions` 的 201 响应。"""

    model_config = ConfigDict(populate_by_name=True)

    version_id: uuid.UUID = Field(alias="versionId")
    revision_no: int = Field(alias="revisionNo")
    #: `created=False` 表示**幂等命中**（同样的内容已注册过）——
    #: 前端据此决定"要不要提示'内容未变化'"。**不能靠 state 推断**
    #: （WP13 ISS-35 的教训）。
    created: bool


class PublicationResultBody(BaseModel):
    """`POST /prompts/{promptId}/publications` 的响应。

    协议原文「激活，**记录影响范围**」→ `supersededVersionId` 与
    `affectedTaskKinds` 两个字段就是"影响范围"的落点。
    """

    model_config = ConfigDict(populate_by_name=True)

    template_id: uuid.UUID = Field(alias="templateId")
    version_id: uuid.UUID = Field(alias="versionId")
    revision_no: int = Field(alias="revisionNo")
    superseded_version_id: uuid.UUID | None = Field(alias="supersededVersionId")
    #: 目标版本**已经是**当前版本时为 True。此时**不写重复事件** ——
    #: 前端据此把"激活"按钮置灰而不是报错。
    already_active: bool = Field(alias="alreadyActive")
    affected_task_kinds: list[str] = Field(alias="affectedTaskKinds")


class ToolGrantResultBody(BaseModel):
    """`POST /prompts/{promptId}/tool-grants` 的响应。"""

    model_config = ConfigDict(populate_by_name=True)

    prompt_id: uuid.UUID = Field(alias="promptId")
    version_id: uuid.UUID = Field(alias="versionId")
    revision_no: int = Field(alias="revisionNo")
    #: `created=False` = 工具集未变化，**没有产生新版本**。
    created: bool
    allowed_tools: list[str] = Field(alias="allowedTools")
    ceiling: list[str]


class CreatedProposalBody(BaseModel):
    """`POST /knowledge/proposals` 的 201 响应。"""

    model_config = ConfigDict(populate_by_name=True)

    proposal_id: uuid.UUID = Field(alias="proposalId")
    state: str


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get(
    "/prompts",
    response_model=PaginatedEnvelope[TemplateSummaryBody],
    responses=PROMPTS_READ_RESPONSES,
    summary="提示词模板列表（含当前激活版本）",
)
def list_prompts(
    user: CurrentUserDep,
    repo: PromptRepoDep,
    params: PageParamsDep,
    task_kind: str | None = Query(default=None, alias="taskKind"),
) -> dict[str, Any]:
    """模板列表（协议 §9 第 11 行）。

    **`taskKind` 的取值会被校验**（`service.list_templates` 里调
    `_require_task_kind`）：不校验的话 `?taskKind=拼错` 会**静默返回空列表**，
    用户以为"没有这个模板"，实际是"拼错了"。
    """
    del user  # 全员可读，登录即通过（`CurrentUserDep` 已完成鉴权）

    # ⚠️ 这里**必须取全量**，不能用 `params.limit * 4` 这种"多取一批"的写法。
    #
    # `platform.pagination` 是**内存**分页：`paginate_items` 只在你交给它的
    # 那个列表里找锚点、切窗口、判 `hasMore`。若只交一个截断的子集，
    # 翻到子集末尾时它会报 `hasMore=False` —— **客户端以为到底了**，
    # 而库里剩下的模板**永远不会出现**，且没有任何错误。
    #
    # 本机实测（100 条数据 / limit=20 / 多取 4 倍 = 80 条）：
    # 客户端在第 4 页拿到 `hasMore=False`，共看到 80 条，**静默丢失 20 条**。
    #
    # 这正是本项目反复踩到的"静默失效"形态：没有异常、HTTP 200、
    # `data` 也正常，只是**少了一半**。因此宁可全量取回：
    # 模板数量级很小（一个月改两次提示词），而"少几条"是不可接受的。
    #
    # 收口方向（已登记待定项）：把锚点定位下推到 SQL
    # （`WHERE (created_at, id) < (:k, :i) ORDER BY ... LIMIT :n`），
    # 届时本处换成数据库游标分页，`paginate_items` 不再参与。
    summaries, _total = service.list_templates(
        repo, task_kind=task_kind, limit=_FULL_SCAN_LIMIT
    )
    items = [(_summary_view(repo, summary), summary) for summary in summaries]

    spec = CursorSpec(
        sort_fields=("createdAt",),
        direction="desc",
        filters={"taskKind": task_kind},
    )
    window, page = paginate_items(
        items,
        params,
        spec,
        sort_key=lambda pair: (pair[1].created_at.isoformat(),),
        id_of=lambda pair: str(pair[1].id),
    )
    return list_response([view for view, _summary in window], page)


@router.post(
    "/prompts",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[CreatedTemplateBody],
    responses=PROMPTS_WRITE_RESPONSES,
    dependencies=PROMPTS_WRITE,
    summary="创建提示词模板（含第一个版本）",
)
def create_prompt_template(
    user: CurrentUserDep,
    repo: PromptRepoDep,
    version_repo: VersionRepoDep,
    body: CreateTemplateBody,
) -> dict[str, Any]:
    """建模板 + 第一个版本（**协议无此端点**，见模块 docstring 第一节）。

    **同名同 `taskKind` 时幂等**：`UQ(task_kind, name)` 让重复创建
    命中既有模板并追加一个版本（`service.create_template` 的说明）——
    而不是报"名字已占用"。重试场景下后者会让调用方困惑。

    **工具集为空**：新模板在平台授予之前一个工具都不能用。
    """
    created = service.create_template(
        repo,
        version_repo,
        draft=TemplateDraft(
            task_kind=body.task_kind,
            name=body.name,
            system_template=body.template,
            input_schema=body.input_schema,
            output_schema=body.output_schema,
            change_reason=body.change_reason,
        ),
        created_by=user.id,
    )
    template = repo.get_template(created.template_id)
    return single_response(
        {
            "promptId": str(created.template_id),
            "versionId": str(created.version_id),
            "revisionNo": created.revision_no,
            "state": "ACTIVE" if template and template.active_version_id else "DRAFT",
        }
    )


@router.post(
    "/prompts/{promptId}/versions",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[CreatedVersionBody],
    responses=PROMPTS_WRITE_RESPONSES,
    dependencies=PROMPTS_WRITE,
    summary="给模板加新版本（需校验可渲染）",
)
def add_prompt_version(
    user: CurrentUserDep,
    repo: PromptRepoDep,
    version_repo: VersionRepoDep,
    body: AddVersionBody,
    prompt_id: Annotated[uuid.UUID, Path(alias="promptId")],
) -> dict[str, Any]:
    """新模板版本（协议 §9 第 12 行，**201**）。

    协议原文：「新模板版本，**需校验可渲染**」。
    判据见 `render.check_renderable`：占位符 ⊆ `input_schema` 声明字段。

    **不改旧版本是结构性的**：`prompt_versions` 表级不可变，
    本端点只插入新行。

    **工具集原样继承上一版**（请求体里没有这个字段）——
    "发个新版本顺便加个工具"这条路径在结构上不存在。
    """
    created = service.add_version(
        repo,
        version_repo,
        template_id=prompt_id,
        draft=VersionDraft(
            system_template=body.template,
            input_schema=body.input_schema,
            output_schema=body.output_schema,
            change_reason=body.change_reason,
        ),
        created_by=user.id,
    )
    return single_response(
        {
            "versionId": str(created.version_id),
            "revisionNo": created.revision_no,
            "created": created.created,
        }
    )


@router.post(
    "/prompts/{promptId}/publications",
    response_model=DataEnvelope[PublicationResultBody],
    responses=PROMPTS_WRITE_RESPONSES,
    dependencies=PROMPTS_WRITE,
    summary="激活模板版本（记录影响范围）",
)
def create_prompt_publication(
    user: CurrentUserDep,
    repo: PromptRepoDep,
    body: PublicationBody,
    prompt_id: Annotated[uuid.UUID, Path(alias="promptId")],
) -> dict[str, Any]:
    """激活某版本（协议 §9 第 13 行）。

    协议原文：「激活，**记录影响范围**」—— 三件事在同一事务里
    （写 `ACTIVATE` 事件、写 `SUPERSEDE` 事件、切 `active_version_id`），
    见 `service.publish`。

    **幂等**：目标版本已是当前版本时返回 `alreadyActive=True`
    且**不写重复事件**（重复事件会让"这次激活影响了什么"在历史里
    出现两条一模一样的记录）。
    """
    result = service.publish(
        repo,
        template_id=prompt_id,
        version_id=body.version_id,
        actor_id=user.id,
        reason=body.reason,
    )
    return single_response(
        {
            "templateId": str(result.template_id),
            "versionId": str(result.version_id),
            "revisionNo": result.revision_no,
            "supersededVersionId": (
                str(result.superseded_version_id)
                if result.superseded_version_id
                else None
            ),
            "alreadyActive": result.already_active,
            "affectedTaskKinds": list(result.affected_task_kinds),
        }
    )


@router.post(
    "/prompts/{promptId}/tool-grants",
    response_model=DataEnvelope[ToolGrantResultBody],
    responses=PROMPTS_WRITE_RESPONSES,
    dependencies=PROMPTS_WRITE,
    summary="平台侧授予工具集（产生新版本，不扩大上界）",
)
def grant_prompt_tools(
    user: CurrentUserDep,
    repo: PromptRepoDep,
    version_repo: VersionRepoDep,
    body: ToolGrantBody,
    prompt_id: Annotated[uuid.UUID, Path(alias="promptId")],
) -> dict[str, Any]:
    """授予工具集（**协议无此端点**，见模块 docstring 第一节）。

    这条路径的存在是"允许工具列表**由平台配置管理**"（§424）的落地：
    它**不在编辑路径上**，因此编辑者无法通过"发新版本"扩权。

    **能授予 ≠ 能扩大上界**：请求里的每个工具都必须在
    `PLATFORM_TOOL_CEILING` 内（子集关系，有反例，不是恒真判据），
    且**新增出网工具**（`RESEARCH_SEARCH`）会被拒 ——
    那是"扩大外部访问范围"，需要平台配置变更而不是一次 API 调用。

    响应里回带 `ceiling`，让调用方知道**当前平台的上界是什么** ——
    不给的话，一次"超出上界"的拒绝只会告诉用户"X 不在允许列表里"，
    而用户不知道该往哪改。
    """
    granted = service.grant_tools(
        repo,
        version_repo,
        template_id=prompt_id,
        allowed_tools=tuple(body.allowed_tools),
        change_reason=body.change_reason,
        granted_by=user.id,
    )
    version = repo.get_version(granted.version_id)
    return single_response(
        {
            "promptId": str(prompt_id),
            "versionId": str(granted.version_id),
            "revisionNo": granted.revision_no,
            "created": granted.created,
            "allowedTools": (
                list(version.allowed_tools) if version is not None else []
            ),
            "ceiling": list(PLATFORM_TOOL_CEILING),
        }
    )


@router.post(
    "/knowledge/proposals",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[CreatedProposalBody],
    responses=PROMPTS_WRITE_RESPONSES,
    dependencies=PROMPTS_WRITE,
    summary="产生一条知识候选（AI 创建待确认，不直接生效）",
)
def create_knowledge_proposal(
    user: CurrentUserDep,
    repo: PromptRepoDep,
    body: CreateProposalBody,
) -> dict[str, Any]:
    """产生知识候选（**闭合 WP20 的 OP-150 产生方**）。

    规格源 02 §165 原文："AI 创建待确认，**不直接生效**" ——
    因此候选永远以 `PENDING` 创建，生效必须走 WP20 的
    `POST /knowledge/proposals/{id}/decisions`（ACCEPT）。
    那是**人工确认**的落点，本端点刻意不提供绕过它的参数。

    **本包只做 `sourceFeedbackId` 这一路**：
    `source_experiment_id` 那一路依赖 `experiments` 表（WP41），
    现在填不出来 —— 登记为待定项。

    **路径与 WP20 的 `GET /knowledge/proposals` 同段、不同方法**：
    FastAPI 按 `(path, method)` 匹配，两者不冲突。
    `main.py` 里两个 router 都挂在 `/api/v1` 下，注册顺序不影响匹配。
    """
    created = service.create_proposal(
        repo,
        draft=ProposalDraft(
            target_asset_id=body.target_asset_id,
            proposed_payload=body.proposed_payload,
            scope=body.scope or repo.default_proposal_scope(),
            source_feedback_id=body.source_feedback_id,
            reason=body.reason,
        ),
        actor_id=user.id,
    )
    return single_response(
        {"proposalId": str(created.proposal_id), "state": created.state}
    )


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _summary_view(repo: Any, summary: Any) -> dict[str, Any]:
    """把 `TemplateSummary` 折成响应字典。

    **激活版本的内容与工具集要额外读一次 `prompt_versions`** ——
    摘要本身不带它们（`types.TemplateSummary` 是纯身份 + 计数）。
    这里显式读而不是让 repository 的列表查询 JOIN：列表端点的
    N+1 是**已知取舍**（模板数量少：本包一个月改两次提示词），
    而 JOIN 会让"列表"与"详情"两处的字段来源不同（两处漂移）。

    取舍已登记（收口方向：模板数量上来后改成一次 JOIN 或加详情端点）。

    **`activeRevisionNo` 直接取用 `summary.active_revision_no`** ——
    服务层已经算好（`service._active_revision_no`）。先前这里用一句
    裸 SQL 从 `version_nodes` 补这个字段，那是**分层越界**
    （端点不该知道表名），且与 service 里的同一查询各写一份。
    """
    version = None
    if summary.active_version_id is not None:
        version = repo.get_version(summary.active_version_id)

    return {
        "id": str(summary.id),
        "taskKind": summary.task_kind,
        "name": summary.name,
        "activeVersionId": (
            str(summary.active_version_id) if summary.active_version_id else None
        ),
        "activeRevisionNo": summary.active_revision_no,
        "versionCount": summary.version_count,
        "state": summary.state,
        "systemTemplate": version.system_template if version else None,
        "inputSchema": version.input_schema if version else None,
        "outputSchema": version.output_schema if version else None,
        "allowedTools": list(version.allowed_tools) if version else [],
        "createdAt": summary.created_at.isoformat(),
    }


__all__ = [
    "AddVersionBody",
    "CreateProposalBody",
    "CreateTemplateBody",
    "CreatedProposalBody",
    "CreatedTemplateBody",
    "CreatedVersionBody",
    "PROMPTS_READ_RESPONSES",
    "PROMPTS_WRITE_RESPONSES",
    "PublicationBody",
    "PublicationResultBody",
    "TASK_KINDS",
    "TemplateSummaryBody",
    "ToolGrantBody",
    "ToolGrantResultBody",
    "router",
]
