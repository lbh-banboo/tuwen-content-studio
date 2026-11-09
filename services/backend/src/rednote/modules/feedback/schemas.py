"""WP29 请求体 / 响应体模型（协议 §1 的 camelCase 外部契约）。

## 为什么请求体用 `alias` 而不是"命名转换器"

本仓的字段命名约定是**逐字段显式 alias**（`projects/api.py:271`、
`content/api.py:452`），不是全局 `alias_generator`。理由不是风格：

· 全局转换器会把**每一个**字段都改一个名字，包括那些契约里本来就该
  与列名一致的键（例如 `body` / `reason`）。一旦某天某个键**不该**转换，
  就得为它写一份例外表 —— 而例外表与转换器共同决定输出，漂移时
  谁也说不清是哪个生效的。
· 显式 alias 让"对外键名"在模型定义处一眼可见，契约核对时不需要
  在脑内跑一遍转换规则。

## 请求体为什么**不做**取值域校验（枚举、长度）

`ANCHOR_KINDS` / `FEEDBACK_CATEGORIES` / `FEEDBACK_STATES` / `REVIEW_DECISIONS`
这些受控取值的校验在 `service.py` 里已经做了，且失败时给出
`details.reason`（机器可判）+ 出错字段（`details.field`）—— 那是前端与
断言共同依赖的形状。

若这里再用 `Literal[...]` 或 `Enum` 声明一遍：

1. 取值域就有了**两处**定义，服务层加一个分类而模型没加 ⇒ 请求在进
   `service` 之前被 pydantic 拦下，错误体形状变成 `HTTPValidationError`
   （协议 §1.2 之外）；
2. 同一个错误会按"哪一个先触发"给出两种不同的 `reason`。

因此请求体只做**类型**约束（`str` / `uuid.UUID` / `list[str]`），
取值域与边界（长度 / 三选一锚点）**全部交给服务层**。

## 响应体为什么是显式模型而不是 `dict`

`response_model` 是 FastAPI 的**输出过滤器**：声明里没有的键会被静默剥掉
（`platform/pagination.py::PaginatedEnvelope` 的 docstring 记着 WP10 踩过的
那一次）。本包把 `service.py` 返回的 dataclass / ORM 行的可见字段逐条列出，
形状就以本文件为唯一契约。

⚠️ 嵌套块（`bindings` / `selfCheckSummary`）用 `dict`：前者的键名由
`content/types.BINDING_KEY_*` 定死、后者属于 WP30（`modules/review`）且
当前恒为 `None`。在端点层再展开一遍就是同一形状的第二处定义。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _RequestBody(BaseModel):
    """请求体的共同配置：接受字段名与 alias 两种写法，多余键即拒。

    `extra="forbid"` 是刻意的：前端把 `contentId` 之类的路径参数误塞进
    body 时，静默忽略会让调用方以为它生效了（与 `ProjectUpdateBody`
    拒绝 `phase` / `ownerId` 同一条理由）。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class _ResponseBody(BaseModel):
    """响应体的共同配置：序列化时输出 alias（camelCase）。"""

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# 一、请求体
# ---------------------------------------------------------------------------


class ContentEditRequestBody(_RequestBody):
    """`POST /contents/{contentId}/edits` 的请求体（协议 §1.1 的"改内容"）。

    **四个字段全可选，未传的沿用上一版**：`content.service.append_edited_version`
    按字段合并（`None` ⇒ 取上一版），因此"只改标题"不会把正文清空。
    这个语义**不能**在端点层用"先读再拼全量"复现 —— 两次读之间可以插进
    一次编辑，拼出来的全量会静默覆盖别人的修改。

    **没有 `expectedLockVersion`**：版本前置条件的载体是 `If-Match` 头
    （`platform/concurrency.require_if_match`），不是 body 字段。让同一件事
    有两个来源，就会出现"头说 A、body 说 B"时没有正确答案的状态。
    """

    title: str | None = None
    body: str | None = None
    tags: list[str] | None = None
    #: 证据绑定的原始形状（键名由 `content/types.BINDING_KEY_*` 定死，camelCase）。
    bindings: list[dict[str, Any]] | None = None


class FeedbackCreateRequestBody(_RequestBody):
    """`POST /contents/{contentId}/feedback` 的请求体。

    **不含 `projectId` / `contentId`**：两者都在路径里，且已由两步骤鉴权
    校验过所属关系。再让客户端传一份，只会多一条"体里的与路径里的不一致"
    的失败路径。

    **不含 `authorKind` / `createdBy`**：这两个是**来源**信息，不是反馈内容。
    端点固定记 `USER` + 当前登录用户 —— 让调用方声明"我是 AI"没有任何
    业务收益，却会让审计面（`content_feedback_events.actor_kind`）失真。
    AI 提出的反馈由 WP31/WP32 的编排路径**直接调服务函数**写入，
    不经过这个 HTTP 端点。
    """

    #: 提意见时看到的是哪一版（反馈定位的前提）。
    observed_version_id: uuid.UUID = Field(alias="observedVersionId")
    #: `WHOLE` / `PARAGRAPH` / `MEDIA` 三选一（取值域由服务层校验）。
    anchor_kind: str = Field(alias="anchorKind")
    category: str
    severity: str
    #: `CURRENT_ONLY` / `FUTURE_GENERATIONS`。
    scope: str
    body: str
    paragraph_ref: str | None = Field(default=None, alias="paragraphRef")
    selected_text: str | None = Field(default=None, alias="selectedText")
    media_item_id: uuid.UUID | None = Field(default=None, alias="mediaItemId")
    suggested_category: str | None = Field(
        default=None, alias="suggestedCategory"
    )


class FeedbackAdvanceRequestBody(_RequestBody):
    """`PATCH /feedback/{feedbackId}` 的请求体（推进反馈状态）。

    **不含 `actorKind` / `actorId`**：端点固定记 `USER` + 当前登录用户。
    状态迁移的执行者是**谁在发这个请求**这一事实，不是客户端可以填写的
    一个字段（与 `FeedbackCreateRequestBody` 同一条理由）。

    **`rejectReason` 在 `toState = REJECTED` 时必填**：判定在服务层
    （它给出可读的 422 与 `details.reason = FEEDBACK_REJECT_REASON_REQUIRED`），
    这里不重复。非 `REJECTED` 的目标状态下服务层会把该列归一为 `NULL`。
    """

    to_state: str = Field(alias="toState")
    note: str | None = None
    reject_reason: str | None = Field(default=None, alias="rejectReason")
    #: 这次迁移产出的版本（"改到哪一版算解决"）。
    resulting_version_id: uuid.UUID | None = Field(
        default=None, alias="resultingVersionId"
    )


class ReviewDecisionRequestBody(_RequestBody):
    """`POST /contents/{contentId}/reviews` 的请求体（单条人工审核）。

    **`subjectVersionId` 必填**：审核结论永远绑定到**具体版本**
    （`content_reviews.subject_version_id` 非空）。允许省略它、由服务端
    取"当前版本"，会让"审核的到底是哪一版"取决于提交时刻的投影 ——
    用户在页面上看到的版本与结论绑定的版本可以不是同一版。

    **不含 `projectId` / `contentItemId`**：前者由路径资源反查，后者在路径里。
    **不含 `decidedBy`**：审核人就是当前登录用户。
    """

    subject_version_id: uuid.UUID = Field(alias="subjectVersionId")
    #: `APPROVED` / `ELIMINATED`（取值域由服务层校验）。
    decision: str
    reason: str
    #: 改判时指回被取代的旧决定（审核行不可变，改判靠追加）。
    supersedes_id: uuid.UUID | None = Field(default=None, alias="supersedesId")


class ReviewBatchRequestBody(_RequestBody):
    """`POST /projects/{projectId}/content-reviews` 的请求体（批量审核）。

    **版本不由客户端给**：批量请求只给内容 id 列表，服务层按"当前版本"
    （投影优先）为每条绑定 `subject_version_id`。若让客户端逐条带版本，
    一个 50 条的批量请求就要传 50 个版本 id —— 而它们与"当前版本"不一致
    时，批量结论会绑定到用户看不到的版本上。

    **结果逐条返回、不整批失败**：因此端点返回 200 而不是 201 ——
    请求可能部分成功、部分失败，"统一成功"的状态码会说谎。
    """

    content_item_ids: list[uuid.UUID] = Field(alias="contentItemIds")
    decision: str
    reason: str


# ---------------------------------------------------------------------------
# 二、响应体：预览
# ---------------------------------------------------------------------------


class PreviewParagraphBody(_ResponseBody):
    """切段结果的一项（`service.build_preview` 的 `paragraphs`）。"""

    index: int
    text: str
    #: `p<序号>-<8位哈希>`，由 media 的切段纯函数生成（本包零切段实现）。
    ref: str


class PreviewMediaItemBody(_ResponseBody):
    """图片位及其段落对位（`feedback_repository.list_media_items_for_content`）。"""

    media_item_id: uuid.UUID = Field(alias="mediaItemId")
    paragraph_ref: str | None = Field(default=None, alias="paragraphRef")
    sort_order: int = Field(alias="sortOrder")
    state: str


class PreviewTitleCandidateBody(_ResponseBody):
    """标题候选与"已采纳"标记。"""

    title_candidate_id: uuid.UUID = Field(alias="titleCandidateId")
    title: str
    is_adopted: bool = Field(alias="isAdopted")


class PreviewResponseBody(_ResponseBody):
    """预览页聚合快照（`service.PreviewPayload.to_dict()` 的逐字段镜像）。

    `etag` 同时出现在 body 与响应头里：响应头是协议 §1.1 的载体，
    body 里的那份让前端不必读 header（读取非简单响应头需要
    `Access-Control-Expose-Headers`，那是额外的部署配置项）。

    `selfCheckSummary` **当前恒为 `None`**：自检属 WP30，本波不编造数据。
    """

    content_id: uuid.UUID = Field(alias="contentId")
    project_id: uuid.UUID = Field(alias="projectId")
    current_version_id: uuid.UUID = Field(alias="currentVersionId")
    version_no: int = Field(alias="versionNo")
    lock_version: int = Field(alias="lockVersion")
    etag: str
    title: str
    body: str
    tags: list[str]
    bindings: list[dict[str, Any]]
    paragraphs: list[PreviewParagraphBody]
    media_items: list[PreviewMediaItemBody] = Field(alias="mediaItems")
    title_candidates: list[PreviewTitleCandidateBody] = Field(
        alias="titleCandidates"
    )
    feedback_count: int = Field(alias="feedbackCount")
    self_check_summary: dict[str, Any] | None = Field(
        default=None, alias="selfCheckSummary"
    )


# ---------------------------------------------------------------------------
# 三、响应体：编辑
# ---------------------------------------------------------------------------


class EditedVersionResponseBody(_ResponseBody):
    """编辑产生的**新版本**摘要（`content.types.ContentVersionSummary`）。

    201 返回的是"新版本"，不是"被修改的内容" —— 编辑落成追加一版，
    旧版原样留在历史里（`content_versions` 不可变）。
    """

    id: uuid.UUID
    version_no: int = Field(alias="versionNo")
    title: str
    blueprint_id: uuid.UUID | None = Field(default=None, alias="blueprintId")
    adopted_title_candidate_id: uuid.UUID | None = Field(
        default=None, alias="adoptedTitleCandidateId"
    )
    generation_context_id: uuid.UUID | None = Field(
        default=None, alias="generationContextId"
    )
    payload_hash: str = Field(alias="payloadHash")
    schema_version: int = Field(alias="schemaVersion")
    created_at: datetime = Field(alias="createdAt")
    created_by: uuid.UUID | None = Field(default=None, alias="createdBy")


# ---------------------------------------------------------------------------
# 四、响应体：反馈项
# ---------------------------------------------------------------------------


class FeedbackItemResponseBody(_ResponseBody):
    """一条反馈（`service._feedback_item_dict` 的逐字段镜像）。

    `anchorState` 是**读时判定**的 `FRESH` / `STALE`：`STALE` 只表示
    锚点随编辑失效，**不**表示这条反馈可以丢弃（R5，列表不过滤）。
    """

    id: uuid.UUID
    content_item_id: uuid.UUID = Field(alias="contentItemId")
    project_id: uuid.UUID = Field(alias="projectId")
    observed_version_id: uuid.UUID = Field(alias="observedVersionId")
    anchor_kind: str = Field(alias="anchorKind")
    anchor_state: str = Field(alias="anchorState")
    paragraph_ref: str | None = Field(default=None, alias="paragraphRef")
    selected_text: str | None = Field(default=None, alias="selectedText")
    media_item_id: uuid.UUID | None = Field(default=None, alias="mediaItemId")
    category: str
    suggested_category: str | None = Field(
        default=None, alias="suggestedCategory"
    )
    severity: str
    scope: str
    state: str
    author_kind: str = Field(alias="authorKind")
    body: str
    reject_reason: str | None = Field(default=None, alias="rejectReason")
    resolved_version_id: uuid.UUID | None = Field(
        default=None, alias="resolvedVersionId"
    )
    created_by: uuid.UUID | None = Field(default=None, alias="createdBy")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    #: 反馈行自己的乐观锁（下一次 PATCH 的 `If-Match` 版本位）。
    lock_version: int = Field(alias="lockVersion")


# ---------------------------------------------------------------------------
# 五、响应体：审核
# ---------------------------------------------------------------------------


class ReviewResponseBody(_ResponseBody):
    """一次审核决定（`service._review_dict` 的逐字段镜像）。"""

    id: uuid.UUID
    project_id: uuid.UUID = Field(alias="projectId")
    content_item_id: uuid.UUID = Field(alias="contentItemId")
    subject_version_id: uuid.UUID = Field(alias="subjectVersionId")
    decision: str
    reason: str
    decided_by: uuid.UUID | None = Field(default=None, alias="decidedBy")
    decided_at: datetime | None = Field(default=None, alias="decidedAt")
    supersedes_id: uuid.UUID | None = Field(default=None, alias="supersedesId")


class ReviewBatchItemResponseBody(_ResponseBody):
    """批量审核的**逐条**结果（一条失败不影响其余条目）。

    `ok = False` 时 `error` 是**人类可读的诊断串**，不是协议错误码 ——
    这是服务层的既有契约（`service.decide_reviews_batch`）。要机器可判的
    `reason` 请走单条端点。
    """

    content_item_id: uuid.UUID = Field(alias="contentItemId")
    ok: bool
    review: ReviewResponseBody | None = None
    error: str | None = None


class ReviewBatchResponseBody(_ResponseBody):
    """批量审核的整体响应：`{"results": [...]}`。"""

    results: list[ReviewBatchItemResponseBody]


__all__ = [
    "ContentEditRequestBody",
    "EditedVersionResponseBody",
    "FeedbackAdvanceRequestBody",
    "FeedbackCreateRequestBody",
    "FeedbackItemResponseBody",
    "PreviewMediaItemBody",
    "PreviewParagraphBody",
    "PreviewResponseBody",
    "PreviewTitleCandidateBody",
    "ReviewBatchItemResponseBody",
    "ReviewBatchRequestBody",
    "ReviewBatchResponseBody",
    "ReviewDecisionRequestBody",
    "ReviewResponseBody",
]
