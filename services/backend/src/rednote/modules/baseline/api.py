"""步骤 3 的端点（协议 §5 第 8 行 `/step-approvals` + 本包补充端点）。

| 方法 | 路径 | 成功码 | 依据 |
|---|---|---|---|
| GET | `/projects/{projectId}/information-draft` | 200 | **本包补充**（审核工作区读取，见下） |
| PUT | `/projects/{projectId}/information-draft` | 200 | **本包补充**（审核工作区保存，见下） |
| GET | `/projects/{projectId}/baselines` | 200 | **本包补充**（基线列表，见下） |
| POST | `/projects/{projectId}/baselines` | 201 | **本包补充**（创建基线，见下） |
| GET | `/projects/{projectId}/baselines/{baselineId}` | 200 | **本包补充**（基线详情，见下） |
| POST | `/projects/{projectId}/step-approvals` | 201 | 协议 §5 第 8 行（形状由本包定义，OP-151） |
| GET | `/projects/{projectId}/step-approvals` | 200 | **本包补充**（批准历史读取，见下） |

---

## 一、为什么"协议只给了一个端点，本包给了七个"

协议 §5 第 8 行只提到 `/step-approvals`（"步骤 1 与步骤 2 通过它完成"），
**且没有定义形状**（OP-151）。而设计文档 §1.3 的 MVP 第 6 条写着
「基线创建 / 查询 / 列表 + 审核工作区的读写 + `step-approvals`」——
也就是**七个端点里只有一个在协议里有名字**。

这不是"顺手加的"。每一个都对应一处**结构性必需**：

| 端点 | 不加的后果 |
|---|---|
| `GET .../information-draft` | 运营打开步骤 3 时没有任何数据来源；`If-Match` 的 ETag 也无从获得（保存一定会 428） |
| `PUT .../information-draft` | 处置冲突 / 确认事实 / 选用场景**没有任何落点** → `information_drafts` 是只读表 → 基线永远建不出来 |
| `GET .../baselines` | 原型 L130 的"基线 BL-v2 被替代 / BL-v3 生效"这条历史**无处可查** |
| `POST .../baselines` | 就是本包的主路径本身 |
| `GET .../baselines/{id}` | 原型 L93~102 的"实际选用条目 / 版本 / 片段 / 未纳入原因"没有读取点 → AC-24 的验收物**存了但取不出来** |
| `GET .../step-approvals` | 批准写进去之后**读不出来** → 八步闭环的"固定版本"证据只能查库 |

**路径参数用 `{baselineId}` 而不是 `{id}`**、**`/information-draft` 用连字符而不是
协议里别处出现的下划线**——这两处与既有命名（`{promptId}` / `{fileId}` /
`brief-draft`）保持一致。协议未定义本包端点，因此这里遵循的是**本仓惯例**，
已在收口清单里登记。

## 二、权限：读要 `VIEW`，写要 `REVIEW`（**不是 `EDIT`**）

`ProjectAction` 里有两个看着都能用的动作：

| 动作 | 注释原文 | 为什么这里不用它 |
|---|---|---|
| `EDIT` | "编辑 Brief、**方案、内容**" | `方案`/`内容` 指生成的方案与文案 —— 那是 WP26~WP32 的产物。审核工作区既不是 Brief 也不是方案 |
| `REVIEW` | "生成、反馈、**人工审核**" | **"人工审核"就是本包**：步骤 3 是八步里唯一的人工确认闸门（原型 L162"运营明确点击确认"） |

**取 `REVIEW`**。若不用它，`REVIEW` 这个动作在**整个系统里没有任何使用者** ——
而它存在于需求 §2.2 的权限矩阵里，说明矩阵的作者心里有一个"人工审核"的位置，
本包正是那个位置。

**当前的权限矩阵下 `REVIEW` 与 `EDIT` 逐行相同**（负责人 / 协作者都 True；
结项后都 False）。因此这个选择**现在没有可观察差异** —— 它是为将来
"审核权与编辑权分离"预留的语义锚点。这正是要写清楚而不是随手挑一个的原因：
一个没有差异的选择，错了也不会有人发现。

**已验证的动作 → 端点映射**：

| 端点 | 动作 |
|---|---|
| `GET .../information-draft`、`GET .../baselines*`、`GET .../step-approvals` | `VIEW` |
| `PUT .../information-draft`、`POST .../baselines`、`POST .../step-approvals` | `REVIEW` |

## 三、`If-Match`：**两个**写端点都要，且是**必带**

草稿（`PUT`）必带 `If-Match` 是协议 §1.1 的直接要求（可变资源），
与 WP18 的 `PUT /brief-draft` 完全同款。

**而 `POST .../baselines` 也必带** —— 这一条协议没要求（协议里根本
没有这个端点），是本包加的。理由是**存在一个静默缺口**：

| 数据 | 来源 |
|---|---|
| 基线条目的 `selected` | **请求体**（客户端屏幕上那份） |
| 快照里的 `readiness` / `counts` / `boundaries` | **服务端当时读到的草稿** |

两者若来自**不同版本**的草稿（另一个运营在这中间改了它），
创建出来的基线会把"客户端看到的条目"与"服务端当前的条件读数"**混在一起**，
且**不报错**：HTTP 201、条目数对、条件读数也是真的，只是它们不属于同一时刻。

`If-Match` 把"我批准的是哪一版草稿"变成前置条件，于是这种混合**无法成立**。
代价是客户端必须先 GET 一次 —— 而它本来就要 GET 才能拿到屏幕上的内容。

**与"超时重试必须成功"不冲突**：创建基线**不改** `information_drafts`
（`lock_version` 不动，见 `service.create_baseline` 的五个写动作清单），
因此重试时同一个 ETag 仍然有效。

## 四、`GET .../information-draft` 就是决策 6 说的"预检端点"

设计文档 §2.3 决策 6：

> `readiness.py` 是完成条件的**唯一来源**，API 预检与后端拒绝**调用同一个函数**。

本包的"API 预检"**不新开一个端点**，而是折进 `GET .../information-draft`
的响应（原型 L139~165 的三条条件与草稿内容在**同一块面板**里，分两个端点
只会让前端发两次请求、并引入"两次读之间条件已变"的窗口）。
`PUT` 的响应同样回带 —— 运营保存之后要**立刻**看到条件有没有变绿，
让它再发一次 GET 会引入同一个窗口（`service.update_draft` 的 docstring
已论证过这一点）。

两处都调 `service.step3_status*` → `readiness.step3_readiness`，
与 `create_baseline` 内部 `_require_readiness_for_creation` 是**同一个函数**。

## 五、`GET .../baselines` 的 `isActive` 是**算出来的**，不是一列

`production_baselines` 没有 `state` / `is_current` 列（决策 5、决策 7）。
列表项里的 `isActive` 由 `projects.active_baseline_id` 与行 id 现算 ——
**它在每次读取时重新算**，因此与"当前生效的是哪条"这个唯一答案
（投影列）不可能漂移。若把它落成一列，它就是第二个真值来源，
而它与投影列脱节时**不报错**（ISS-78/84/85/86 同族）。

## 六、`decidedBy` **不接受**客户端传入

`POST .../step-approvals` 的请求体里**没有** `decidedBy` ——
批准人取自**当前登录用户**（`user.id`）。给一个可传的字段等于给
"以别人的名义批准"留一个入口，而批准记录是八步闭环的凭据。

**同一判据也适用于 `supersedesId` 的取舍**：它在请求体里，因为
"我这次是在取代哪一条"是调用方的意图（先拒后批时服务端会自己算出来，
见 `service.approve_step` 的三种情形表）—— 意图可以传，身份不能传。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from rednote.modules.baseline import service
from rednote.modules.baseline.baseline_types import (
    BaselineDraft,
    ItemDraft,
    StepApprovalDraft,
)
from rednote.modules.baseline.dependencies import BaselineRepoDep, VersionRepoDep
from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.projects.dependencies import ProjectAccessDep
from rednote.modules.projects.permissions import ProjectAction
from rednote.platform.api_responses import READ_RESPONSES, WRITE_RESPONSES
from rednote.platform.concurrency import require_if_match
from rednote.platform.errors import ApiError, DataEnvelope, ErrorCode, get_request_id
from rednote.platform.pagination import (
    CursorSpec,
    PageParamsDep,
    PaginatedEnvelope,
    list_response,
    paginate_items,
    single_response,
)

router = APIRouter(tags=["baseline"])

# 列表端点的**全量扫描上限**。
#
# 见 `list_baselines` 里的说明：`platform.pagination` 是内存分页，
# 必须拿到全量数据，否则会静默截断（只交子集时
# `paginate_items` 会在子集末尾报 `hasMore=False`，客户端以为到底了）。
#
# 上限存在的意义只是防止"表被意外撑到十万条"时把内存打满 ——
# 它不是分页参数。真正的修法是把锚点下推到 SQL（已登记待定项，
# 与 WP23 的 `prompts/api.py` 同一处待办）。
_FULL_SCAN_LIMIT: int = 10_000

BASELINE_READ_RESPONSES = READ_RESPONSES
BASELINE_WRITE_RESPONSES = WRITE_RESPONSES


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class SaveDraftBody(BaseModel):
    """`PUT /projects/{projectId}/information-draft` 的请求体。

    **七个字段全部可选，`None` = 本次不动这一块**（与 WP18 的 `DraftBody`
    同款的部分更新语义）。写成"必填 + 空值清空"会让"只想处置一个冲突"的
    请求把另外五块**静默清空**（HTTP 200，响应里那几块也空着，看起来像成功）。

    ⚠️ **`operatorConfirmed` 的 `False` 是合法值**：
    `service.update_draft` 用 `is not None` 判定，因此
    `False`（取消确认）与 `None`（不改）**在类型层就是两件事**。
    Pydantic 的 `bool | None` 保留了这个区别 —— 若这里写成
    `bool = False`，取消确认就会被当成"没提这一块"而**静默无效**，
    于是完成条件 ③ 永远为真。

    **没有 `lockVersion`**：并发的期望版本走 `If-Match` 头
    （与 WP18 的 `PUT /brief-draft` 同款）—— 两处各带一个版本号
    会出现"头说 v3、体说 v5"这种没有正确答案的状态。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    brief_version_id: uuid.UUID | None = Field(default=None, alias="briefVersionId")
    claims: list[dict[str, Any]] | None = None
    conflicts: list[dict[str, Any]] | None = None
    scenes: list[dict[str, Any]] | None = None
    boundaries: dict[str, Any] | None = None
    research_selection: list[dict[str, Any]] | None = Field(
        default=None, alias="researchSelection"
    )
    operator_confirmed: bool | None = Field(default=None, alias="operatorConfirmed")


class BaselineItemBody(BaseModel):
    """基线里的一行（`POST .../baselines` 的 `selected[]` 元素）。

    **`snapshot` 必填且是结构化对象**（决策 3）：若它是我们自己拼的一句话，
    它**本身就成了一个新的真值来源** —— 半年后无法判断"这个值是从哪来的"。
    因此 `extra="forbid"` 且不给默认值：漏传 `snapshot` 必须是 422，
    不能是"存了个空对象"（那会让 AC-24 的溯源能力静默失效）。

    **`inclusion=EXCLUDED` 的行也必须传**（携 `exclusionReason`）——
    "未纳入原因"只在复盘时才被读，而那时**补不出来**。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    item_kind: str = Field(alias="itemKind")
    ref_label: str = Field(alias="refLabel")
    snapshot: dict[str, Any]
    inclusion: str | None = None
    knowledge_asset_id: uuid.UUID | None = Field(default=None, alias="knowledgeAssetId")
    knowledge_version_id: uuid.UUID | None = Field(
        default=None, alias="knowledgeVersionId"
    )
    snapshot_text: str | None = Field(default=None, alias="snapshotText")
    exclusion_reason: str | None = Field(default=None, alias="exclusionReason")
    context_order: int | None = Field(default=None, alias="contextOrder")


class CreateBaselineBody(BaseModel):
    """`POST /projects/{projectId}/baselines` 的请求体。

    **`baselineId` 不在请求体里**：它在 `version_nodes` 里由
    `register_version` 分配（`entity_id = projectId`），客户端造不出来。
    让客户端传一个 id 只会多一条"这个 id 与注册表不匹配"的失败路径。

    **没有 `state` / `isActive`**：基线完全不可变（AC-08），
    "当前生效的是哪一条"由 `projects.active_baseline_id` 回答。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    brief_version_id: uuid.UUID = Field(alias="briefVersionId")
    selected: list[BaselineItemBody]
    ruleset_version: str = Field(alias="rulesetVersion")
    change_reason: str = Field(alias="changeReason")
    approval_reason: str | None = Field(default=None, alias="approvalReason")
    supersedes_id: uuid.UUID | None = Field(default=None, alias="supersedesId")


class CreateApprovalBody(BaseModel):
    """`POST /projects/{projectId}/step-approvals` 的请求体（OP-151 的形状）。

    **没有 `decidedBy`**：批准人取自当前登录用户（模块 docstring 第六节）。

    **`decision` 只有 `APPROVED` / `REJECTED`** 两个受控取值 ——
    拒绝也要留痕（"曾经被拒过"是审计信息，也是先拒后批的链根）。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    step_no: int = Field(alias="stepNo")
    subject_kind: str = Field(alias="subjectKind")
    subject_version_id: uuid.UUID = Field(alias="subjectVersionId")
    decision: str
    reason: str
    supersedes_id: uuid.UUID | None = Field(default=None, alias="supersedesId")


# ---------------------------------------------------------------------------
# 响应体（供 OpenAPI 契约生成具体结构，而不是裸 object）
# ---------------------------------------------------------------------------


class ConditionBody(BaseModel):
    """一条完成条件的读数（`readiness.ConditionResult` 的线上形状）。"""

    model_config = ConfigDict(populate_by_name=True)

    key: str
    satisfied: bool
    #: **与人看的说明同源**：服务层的拒绝文案与这里的 `reason`
    #: 是同一段文字（`readiness.ConditionResult.reason`），不会一处说
    #: "1 项阻断"另一处说"存在阻断问题"。
    reason: str
    blocking_open: list[str] = Field(alias="blockingOpen")
    producible_scenes: list[str] = Field(alias="producibleScenes")


class ReadinessBody(BaseModel):
    """三条完成条件的整体读数（`readiness.Step3Readiness`）。"""

    model_config = ConfigDict(populate_by_name=True)

    #: 三条的合取 —— **唯一的"能不能创建基线"判据**（决策 6）。
    ready: bool
    conditions: list[ConditionBody]


class InformationDraftBody(BaseModel):
    """`GET` / `PUT .../information-draft` 的响应。

    **`etag` 与 `draftId` 都回带**：`PUT` 必带 `If-Match`，
    而它的值就是这把 `etag`。只给 `etag` 已经够用（它内含 id），
    但显式给出 `draftId` 让前端在"两个标签页各持一份草稿"时能自己发现不一致。

    **`activeBaselineId` / `activeBaselineRevisionNo` 折在这里**：
    原型 L33 的「基线 BL-v3 / 绑定 Brief v2 · 12 项事实」需要它，
    而 WP10 的 `GET /projects/{id}` 响应里**没有**这两个字段
    （`projects/api.py` 零命中）—— 那是别的包的文件，本包不改它。
    """

    model_config = ConfigDict(populate_by_name=True)

    draft_id: uuid.UUID = Field(alias="draftId")
    project_id: uuid.UUID = Field(alias="projectId")
    brief_version_id: uuid.UUID | None = Field(alias="briefVersionId")
    claims: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    scenes: list[dict[str, Any]]
    boundaries: dict[str, Any]
    research_selection: list[dict[str, Any]] = Field(alias="researchSelection")
    operator_confirmed: bool = Field(alias="operatorConfirmed")
    lock_version: int = Field(alias="lockVersion")
    updated_at: str | None = Field(alias="updatedAt")
    etag: str
    active_baseline_id: uuid.UUID | None = Field(alias="activeBaselineId")
    active_baseline_revision_no: int | None = Field(alias="activeBaselineRevisionNo")
    readiness: ReadinessBody


class BaselineSummaryBody(BaseModel):
    """`GET .../baselines` 的一行。

    **`isActive` 是算出来的**（模块 docstring 第五节）：与
    `projects.active_baseline_id` 现算现比，因此它不可能与该投影列漂移。
    """

    model_config = ConfigDict(populate_by_name=True)

    baseline_id: uuid.UUID = Field(alias="baselineId")
    revision_no: int = Field(alias="revisionNo")
    brief_version_id: uuid.UUID | None = Field(alias="briefVersionId")
    ruleset_version: str = Field(alias="rulesetVersion")
    supersedes_id: uuid.UUID | None = Field(alias="supersedesId")
    item_count: int = Field(alias="itemCount")
    excluded_count: int = Field(alias="excludedCount")
    is_active: bool = Field(alias="isActive")
    created_at: str = Field(alias="createdAt")


class BaselineItemViewBody(BaseModel):
    """基线详情里的一行（含**未纳入项** —— AC-24 要求）。"""

    model_config = ConfigDict(populate_by_name=True)

    item_no: int = Field(alias="itemNo")
    item_kind: str = Field(alias="itemKind")
    inclusion: str
    ref_label: str = Field(alias="refLabel")
    snapshot: dict[str, Any]
    snapshot_text: str | None = Field(alias="snapshotText")
    exclusion_reason: str | None = Field(alias="exclusionReason")
    context_order: int = Field(alias="contextOrder")
    knowledge_asset_id: uuid.UUID | None = Field(alias="knowledgeAssetId")
    knowledge_version_id: uuid.UUID | None = Field(alias="knowledgeVersionId")


class BaselineDetailBody(BaseModel):
    """`GET .../baselines/{baselineId}` 的响应。

    **`snapshot` 是基线级的快照**（规则集版本 / 完成条件读数 / 各块规模 /
    表达边界），与条目级的 `items[].snapshot` 是**两个层级**。
    它刻意**不含**批准人与批准时间（由 `step_approvals` 回答，
    再存一份就是第二个真值来源），也**不含** `isActive`
    （那会变 —— 一条不可变基线里存一个会变的判断，建成那一刻就过期）。
    """

    model_config = ConfigDict(populate_by_name=True)

    baseline_id: uuid.UUID = Field(alias="baselineId")
    project_id: uuid.UUID = Field(alias="projectId")
    revision_no: int = Field(alias="revisionNo")
    brief_version_id: uuid.UUID | None = Field(alias="briefVersionId")
    ruleset_version: str = Field(alias="rulesetVersion")
    snapshot: dict[str, Any]
    supersedes_id: uuid.UUID | None = Field(alias="supersedesId")
    created_at: str = Field(alias="createdAt")
    created_by: uuid.UUID | None = Field(alias="createdBy")
    items: list[BaselineItemViewBody]


class CreatedBaselineBody(BaseModel):
    """`POST .../baselines` 的 201 响应。

    **`approvalId` 回带**：创建基线与写步骤 3 的批准是**同一事务**
    （OP-151），客户端要能把它记下来 —— 否则"这条基线是谁批的"
    在响应里没有出口。

    **`created=False` 表示幂等命中**（同修订号已注册过同样的内容）——
    前端据此把"确认"按钮置灰而不是报错。**不能靠任何状态字段推断**
    （WP13 的 ISS-35 教训）。
    """

    model_config = ConfigDict(populate_by_name=True)

    baseline_id: uuid.UUID = Field(alias="baselineId")
    revision_no: int = Field(alias="revisionNo")
    item_count: int = Field(alias="itemCount")
    excluded_count: int = Field(alias="excludedCount")
    approval_id: uuid.UUID = Field(alias="approvalId")
    superseded_baseline_id: uuid.UUID | None = Field(alias="supersededBaselineId")
    created: bool


class ApprovalBody(BaseModel):
    """`POST` / `GET .../step-approvals` 的一行。

    **`reason` 不回带**：`service.list_approvals` 的 `ApprovalView`
    刻意不带它 —— 批准理由是审计信息（已进 `audit_events`），
    在列表里逐条回带会让它变成第二个正文存储（`_draft_audit_payload`
    的同一条取向）。单条详情需要一个协议里没有的端点，已登记。
    """

    model_config = ConfigDict(populate_by_name=True)

    approval_id: uuid.UUID = Field(alias="approvalId")
    project_id: uuid.UUID = Field(alias="projectId")
    step_no: int = Field(alias="stepNo")
    subject_kind: str = Field(alias="subjectKind")
    subject_version_id: uuid.UUID = Field(alias="subjectVersionId")
    decision: str
    decided_at: str = Field(alias="decidedAt")


# ---------------------------------------------------------------------------
# 端点：审核工作区
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/information-draft",
    response_model=DataEnvelope[InformationDraftBody],
    responses=BASELINE_READ_RESPONSES,
    summary="读取审核工作区（含三条完成条件的预检读数）",
)
def get_information_draft(
    access: ProjectAccessDep,
    repo: BaselineRepoDep,
) -> dict[str, Any]:
    """读审核工作区（模块 docstring 第四节：这就是"预检端点"）。

    **工作区不存在时会被建出来**（`service.require_draft` 的幂等懒创建）——
    因此这个 `GET` **会写库**。它幂等（`uq_information_drafts_project_id` 兜底），
    但违反了"GET 是安全方法"的约定。**这是已知取舍**，收口方向是让主 agent
    在项目创建事务里补一次调用（那时本端点退化成纯读）。
    """
    access.require(ProjectAction.VIEW)
    draft = service.require_draft(repo, project_id=access.project_id)
    readiness = service.step3_status_of_draft(draft)
    active_id = repo.active_baseline_id(access.project_id)
    return single_response(
        _draft_view(
            draft,
            readiness,
            active_baseline_id=active_id,
            active_revision_no=(
                None if active_id is None else repo.revision_no(active_id) or 0
            ),
        )
    )


@router.put(
    "/projects/{projectId}/information-draft",
    response_model=DataEnvelope[InformationDraftBody],
    responses=BASELINE_WRITE_RESPONSES,
    summary="保存审核工作区（部分更新，需 If-Match）",
)
def save_information_draft(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: BaselineRepoDep,
    body: SaveDraftBody,
    request: Request,
) -> dict[str, Any]:
    """保存工作区（模块 docstring 第三、四节）。

    **`If-Match` 必带**：草稿是可变资源（协议 §1.1）。两层并发保证都要有 ——
    前置校验（这里）挡"客户端拿的是过期值"，条件 UPDATE
    （`repository.update_draft` 的 `WHERE lock_version = ?`）挡
    "两个请求同时通过前置校验"。

    **响应回带新的 `etag` 与 `readiness`**：返回旧 ETag 会让客户端
    下一次保存必然 412（它其实什么都没做错）；而 readiness 不在这里
    回带、让前端再发一次 GET，会引入"两次读之间条件已变"的窗口 ——
    前端会拿着过期的条件读数去决定"确认"按钮是否可点。

    **为什么本函数不声明 `project_id` 路径参数**：它已经在
    `access: ProjectAccessDep` 里（`load_project_access` 自己声明了
    `Path(alias="projectId")`）。额外声明一份会拿到**同一个值**，
    于是只有两种结局 —— 写 `del project_id`（绕开"未使用变量"），
    或让读者以为两个值可能不同。`brief/api.py::update_brief_draft`
    与 `create_brief_version` 同样只取 `access`，此处保持一致。
    """
    access.require(ProjectAction.REVIEW)
    draft = service.require_draft(repo, project_id=access.project_id)

    # 抛 428（缺）/ 400（格式非法）/ 412（不匹配，回带 currentEtag）。
    require_if_match(
        request,
        resource_id=draft.id,
        current_version=draft.lock_version,
        resource_label="审核工作区",
    )

    updated, readiness = service.update_draft(
        repo,
        project_id=access.project_id,
        expected_lock_version=draft.lock_version,
        claims=body.claims,
        conflicts=body.conflicts,
        scenes=body.scenes,
        boundaries=body.boundaries,
        research_selection=body.research_selection,
        operator_confirmed=body.operator_confirmed,
        brief_version_id=body.brief_version_id,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    active_id = repo.active_baseline_id(access.project_id)
    return single_response(
        _draft_view(
            updated,
            readiness,
            active_baseline_id=active_id,
            active_revision_no=(
                None if active_id is None else repo.revision_no(active_id) or 0
            ),
        )
    )


# ---------------------------------------------------------------------------
# 端点：基线
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/baselines",
    response_model=PaginatedEnvelope[BaselineSummaryBody],
    responses=BASELINE_READ_RESPONSES,
    summary="基线列表（含当前生效标记）",
)
def list_baselines(
    access: ProjectAccessDep,
    repo: BaselineRepoDep,
    params: PageParamsDep,
) -> dict[str, Any]:
    """基线列表（新到旧，游标分页）。

    **必须取全量**（`_FULL_SCAN_LIMIT`），不能用 `params.limit * 4` 这种
    "多取一批"的写法：`platform.pagination` 是**内存**分页，
    `paginate_items` 只在交给它的那个列表里找锚点、切窗口、判 `hasMore`。
    只交截断的子集，翻到子集末尾会报 `hasMore=False` ——
    **客户端以为到底了**，而库里剩下的基线**永远不会出现**，且没有任何错误。

    **`repository.list_baselines` 用的是 `limit`/`offset`，而这里是游标** ——
    形状不一致，已登记待定项。当前数据量极小（一个项目几条基线），
    全量取回后内存分页是可接受的；真正的修法是把锚点下推到 SQL。
    """
    access.require(ProjectAction.VIEW)

    rows, _total = service.list_baselines(
        repo, project_id=access.project_id, limit=_FULL_SCAN_LIMIT, offset=0
    )
    # `total` 刻意丢弃：协议的 `page` 块只有 `nextCursor` / `hasMore`
    # （游标分页不需要总数，`PageInfo` 里没有这个字段，硬塞会被
    # `response_model` 静默剥掉 —— 与 WP10 踩过的 `page` 被剥是同一个机制）。
    # 服务层仍返回它，因为数据回填与测试对账需要。

    active_id = repo.active_baseline_id(access.project_id)
    views = [
        {
            "baselineId": str(row.baseline_id),
            "revisionNo": row.revision_no,
            "briefVersionId": (
                None if row.brief_version_id is None else str(row.brief_version_id)
            ),
            "rulesetVersion": row.ruleset_version,
            "supersedesId": (
                None if row.supersedes_id is None else str(row.supersedes_id)
            ),
            "itemCount": row.item_count,
            "excludedCount": row.excluded_count,
            "isActive": row.baseline_id == active_id,
            "createdAt": row.created_at.isoformat(),
        }
        for row in rows
    ]

    spec = CursorSpec(
        sort_fields=("createdAt",),
        direction="desc",
        # 过滤器参与游标指纹：跨项目的游标会因指纹不符被拒
        # （而不是静默地把另一个项目的行当成续页）。
        filters={"projectId": str(access.project_id)},
    )
    window, page = paginate_items(
        views,
        params,
        spec,
        sort_key=lambda view: (view["createdAt"],),
        id_of=lambda view: view["baselineId"],
    )
    return list_response(window, page)


@router.post(
    "/projects/{projectId}/baselines",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[CreatedBaselineBody],
    responses=BASELINE_WRITE_RESPONSES,
    summary="创建基线（同事务生成步骤 3 批准）",
)
def create_baseline(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: BaselineRepoDep,
    version_repo: VersionRepoDep,
    body: CreateBaselineBody,
    request: Request,
) -> dict[str, Any]:
    """创建一条不可变基线（**本包的主路径**，四写一读的事务）。

    **四道服务层预检 + 三道数据库延迟约束**（见 `service.create_baseline`
    的七步清单）。本端点只做参数解析、`If-Match` 前置校验与响应组装 ——
    **全部判定在服务层**。

    **`If-Match` 校验的是审核工作区**（模块 docstring 第三节）：
    请求体里的 `selected` 来自客户端屏幕，而快照里的 `readiness` /
    `counts` / `boundaries` 来自服务端读到的草稿。两者必须同版。
    """
    access.require(ProjectAction.REVIEW)
    draft = service.require_draft(repo, project_id=access.project_id)

    require_if_match(
        request,
        resource_id=draft.id,
        current_version=draft.lock_version,
        resource_label="审核工作区",
    )

    created = service.create_baseline(
        repo,
        version_repo,
        draft=BaselineDraft(
            project_id=access.project_id,
            brief_version_id=body.brief_version_id,
            selected=tuple(
                ItemDraft(
                    item_kind=item.item_kind,
                    ref_label=item.ref_label,
                    snapshot=item.snapshot,
                    # `None` → 交回值类型的默认值：把 `INCLUSION_INCLUDED`
                    # 在这里再写一遍，就是同一条默认值的第二处定义。
                    **(dict(inclusion=item.inclusion) if item.inclusion else {}),
                    knowledge_asset_id=item.knowledge_asset_id,
                    knowledge_version_id=item.knowledge_version_id,
                    snapshot_text=item.snapshot_text,
                    exclusion_reason=item.exclusion_reason,
                    context_order=item.context_order,
                )
                for item in body.selected
            ),
            ruleset_version=body.ruleset_version,
            change_reason=body.change_reason,
            supersedes_id=body.supersedes_id,
        ),
        approval_reason=body.approval_reason,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    return single_response(
        {
            "baselineId": str(created.baseline_id),
            "revisionNo": created.revision_no,
            "itemCount": created.item_count,
            "excludedCount": created.excluded_count,
            "approvalId": str(created.approval_id),
            "supersededBaselineId": (
                None
                if created.superseded_baseline_id is None
                else str(created.superseded_baseline_id)
            ),
            "created": created.created,
        }
    )


@router.get(
    "/projects/{projectId}/baselines/{baselineId}",
    response_model=DataEnvelope[BaselineDetailBody],
    responses=BASELINE_READ_RESPONSES,
    summary="基线详情（含全部条目与未纳入原因）",
)
def get_baseline_detail(
    access: ProjectAccessDep,
    repo: BaselineRepoDep,
    baseline_id: Annotated[uuid.UUID, Path(alias="baselineId")],
) -> dict[str, Any]:
    """基线详情（**含 `EXCLUDED` 条目**）。

    **不筛掉未纳入项**（AC-24）：过滤掉会让"为什么没纳入"在详情接口里
    无处可查 —— 而它正是半年后复盘时唯一想知道的事（设计文档 §1.2
    的"容易忽略的用户"）。

    **路径里的 `{projectId}` 不是装饰**：它既做权限校验，也做归属校验
    （`service.get_baseline_detail` 在 `baseline.project_id != project_id`
    时给 404）—— 只按 `baselineId` 寻址会让"换一个 projectId 就能读
    别人的基线"成为可能。归属不符给 **404 而不是 403**：让"不存在"
    与"不属于本项目"不可区分（协议 §1.2 的防枚举）。
    """
    access.require(ProjectAction.VIEW)
    detail = service.get_baseline_detail(
        repo, project_id=access.project_id, baseline_id=baseline_id
    )
    return single_response(
        {
            "baselineId": str(detail.baseline_id),
            "projectId": str(detail.project_id),
            "revisionNo": detail.revision_no,
            "briefVersionId": (
                None if detail.brief_version_id is None else str(detail.brief_version_id)
            ),
            "rulesetVersion": detail.ruleset_version,
            "snapshot": detail.snapshot,
            "supersedesId": (
                None if detail.supersedes_id is None else str(detail.supersedes_id)
            ),
            "createdAt": detail.created_at.isoformat(),
            "createdBy": None if detail.created_by is None else str(detail.created_by),
            "items": [_item_view(item) for item in detail.items],
        }
    )


# ---------------------------------------------------------------------------
# 端点：步骤批准（闭合 OP-151）
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{projectId}/step-approvals",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[ApprovalBody],
    responses=BASELINE_WRITE_RESPONSES,
    summary="记录一次步骤批准（不可变，含拒绝）",
)
def create_step_approval(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: BaselineRepoDep,
    body: CreateApprovalBody,
) -> dict[str, Any]:
    """记录步骤批准（协议 §5 第 8 行，**形状由本包定义**，OP-151）。

    **批准的对象是「步骤 + 版本」的组合**（决策 4）：Brief 出了 v2 之后，
    步骤 1 的"已批准"指的是 **v2 已批准**，不是"步骤 1 曾经被批过"。
    只记步骤会让"这一步批的是哪一版"在数据里没有答案。

    **`subjectVersionId` 与 `subjectKind` 会交叉核对**：声称 `BASELINE`
    的批准指向一个 `BRIEF` 节点显然是矛盾 —— 这类矛盾在写入前就能抓到
    （`service.approve_step` 里的 `_SUBJECT_KIND_ENTITY_TYPE`）。
    `RESEARCH` 没有对应的 `entity_type`，因此跳过核对 ——
    **"无法核对"与"核对失败"是两件事**。

    **重复批同一结论报 409**（不是幂等返回）：批准是人为动作，
    重投应当幂等，但"又批了一次"在数据里留下第二条记录会让
    "这条被批过几次"虚假增长（`service.approve_step` 的三种情形表）。
    """
    access.require(ProjectAction.REVIEW)
    approval = service.approve_step(
        repo,
        draft=StepApprovalDraft(
            project_id=access.project_id,
            step_no=body.step_no,
            subject_kind=body.subject_kind,
            subject_version_id=body.subject_version_id,
            decision=body.decision,
            reason=body.reason,
            # 批准人取自登录用户，**不接受客户端传入**（模块 docstring 第六节）。
            decided_by=user.id,
            supersedes_id=body.supersedes_id,
        ),
        request_id=get_request_id(),
    )
    return single_response(_approval_view(approval))


@router.get(
    "/projects/{projectId}/step-approvals",
    response_model=PaginatedEnvelope[ApprovalBody],
    responses=BASELINE_READ_RESPONSES,
    summary="批准历史（可按步骤过滤，含被取代的那些）",
)
def list_step_approvals(
    access: ProjectAccessDep,
    repo: BaselineRepoDep,
    params: PageParamsDep,
    step_no: int | None = Query(default=None, alias="stepNo"),
) -> dict[str, Any]:
    """批准历史（新到旧，游标分页）。

    **不筛掉被取代的批准**：`supersedes_id` 链是"先拒后批"的痕迹，
    而"曾经被拒过"是审计信息（决策 4）。筛掉会让"这一步被批了几次、
    中间有没有反复"查不到。

    **同样必须取全量**：审批数量随修订累积，内存分页只交子集会静默丢行
    （与 `list_baselines` 同一条说明）。

    **没有"超过上限就切片"的兜底** —— 那种写法本身就是静默截断：
    `approvals[:_FULL_SCAN_LIMIT]` 会让第 10001 条之后的批准**静默消失**
    （`hasMore=False`、HTTP 200、无任何错误），而这一端点的读者是
    "半年后查事故的人"。因此**超限时报错**（503，带可操作的信息），
    而不是给一个看起来正常的不完整列表。
    """
    access.require(ProjectAction.VIEW)
    approvals = service.list_approvals(
        repo, project_id=access.project_id, step_no=step_no
    )
    if len(approvals) > _FULL_SCAN_LIMIT:  # pragma: no cover - 防御性边界
        raise ApiError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            f"本项目的批准记录超过 {_FULL_SCAN_LIMIT} 条，"
            "内存分页无法保证完整性。请用 stepNo 过滤后分别读取 —— "
            "不给不完整的结果比给错的结果好。",
            details={
                "reason": "approval_scan_limit_exceeded",
                "limit": _FULL_SCAN_LIMIT,
                "total": len(approvals),
            },
        )

    views = [_approval_view(row) for row in approvals]
    spec = CursorSpec(
        sort_fields=("decidedAt",),
        direction="desc",
        filters={"projectId": str(access.project_id), "stepNo": step_no},
    )
    window, page = paginate_items(
        views,
        params,
        spec,
        sort_key=lambda view: (view["decidedAt"],),
        id_of=lambda view: view["approvalId"],
    )
    return list_response(window, page)


# ---------------------------------------------------------------------------
# 内部：值对象 → 线上字典
# ---------------------------------------------------------------------------


def _draft_view(
    draft: Any,
    readiness: Any,
    *,
    active_baseline_id: uuid.UUID | None,
    active_revision_no: int | None,
) -> dict[str, Any]:
    """组装工作区响应。

    **全部字段来自 `service.draft_view` 的返回值，不戳 ORM 实例**：
    ORM 实例绑在 session 上，而响应组装发生在 session 关闭之后
    （`DetachedInstanceError`）。`DraftView` 是 frozen dataclass，
    与 session 生命周期无关 —— **`draft` 参数只用于算 ETag**
    （`draft_etag` 需要 `id` 与 `lock_version`，两者在 `DraftView` 里也有，
    但 ETag 的算法必须只有一处，因此仍调 `service.draft_etag`）。
    """
    view = service.draft_view(draft)
    return {
        "draftId": str(view.draft_id),
        "projectId": str(view.project_id),
        "briefVersionId": (
            None if view.brief_version_id is None else str(view.brief_version_id)
        ),
        "claims": [dict(row) for row in view.claims],
        "conflicts": [dict(row) for row in view.conflicts],
        "scenes": [dict(row) for row in view.scenes],
        "boundaries": dict(view.boundaries),
        "researchSelection": [dict(row) for row in view.research_selection],
        "operatorConfirmed": view.operator_confirmed,
        "lockVersion": view.lock_version,
        "updatedAt": None if view.updated_at is None else view.updated_at.isoformat(),
        "etag": service.draft_etag(draft),
        "activeBaselineId": (
            None if active_baseline_id is None else str(active_baseline_id)
        ),
        "activeBaselineRevisionNo": active_revision_no,
        "readiness": {
            "ready": readiness.ready,
            "conditions": [
                {
                    "key": condition.key,
                    "satisfied": condition.satisfied,
                    "reason": condition.reason,
                    "blockingOpen": list(condition.blocking_open),
                    "producibleScenes": list(condition.producible_scenes),
                }
                for condition in readiness.conditions
            ],
        },
    }


def _item_view(item: Any) -> dict[str, Any]:
    """基线条目 → 线上字典。"""
    return {
        "itemNo": item.item_no,
        "itemKind": item.item_kind,
        "inclusion": item.inclusion,
        "refLabel": item.ref_label,
        "snapshot": dict(item.snapshot or {}),
        "snapshotText": item.snapshot_text,
        "exclusionReason": item.exclusion_reason,
        "contextOrder": item.context_order,
        "knowledgeAssetId": (
            None if item.knowledge_asset_id is None else str(item.knowledge_asset_id)
        ),
        "knowledgeVersionId": (
            None
            if item.knowledge_version_id is None
            else str(item.knowledge_version_id)
        ),
    }


def _approval_view(approval: Any) -> dict[str, Any]:
    """批准 → 线上字典。"""
    return {
        "approvalId": str(approval.approval_id),
        "projectId": str(approval.project_id),
        "stepNo": approval.step_no,
        "subjectKind": approval.subject_kind,
        "subjectVersionId": str(approval.subject_version_id),
        "decision": approval.decision,
        "decidedAt": approval.decided_at.isoformat(),
    }


__all__ = [
    "ApprovalBody",
    "BASELINE_READ_RESPONSES",
    "BASELINE_WRITE_RESPONSES",
    "BaselineDetailBody",
    "BaselineItemBody",
    "BaselineItemViewBody",
    "BaselineSummaryBody",
    "ConditionBody",
    "CreateApprovalBody",
    "CreateBaselineBody",
    "CreatedBaselineBody",
    "InformationDraftBody",
    "ReadinessBody",
    "SaveDraftBody",
    "router",
]
