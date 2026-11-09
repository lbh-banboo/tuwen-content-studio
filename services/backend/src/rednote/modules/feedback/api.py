"""WP29 反馈定位 / 预览编辑 / 审核决定端点（**七个**）。

| # | 方法 | 路径 | 成功码 | 服务函数 |
|---|---|---|---|---|
| ① | GET | `/contents/{contentId}/preview` | 200 | `service.build_preview` |
| ② | POST | `/contents/{contentId}/edits` | 201 | `service.edit_content` |
| ③ | GET | `/contents/{contentId}/feedback` | 200 | `service.list_feedback` |
| ④ | POST | `/contents/{contentId}/feedback` | 201 | `service.create_feedback` |
| ⑤ | PATCH | `/feedback/{feedbackId}` | 200 | `service.advance_feedback` |
| ⑥ | POST | `/contents/{contentId}/reviews` | 201 | `service.decide_review` |
| ⑦ | POST | `/projects/{projectId}/content-reviews` | 200 | `service.decide_reviews_batch` |

---

## 一、权限：两种寻址方式，动作逐个给理由

| 端点 | 寻址 | 权限判定 | 动作 |
|---|---|---|---|
| ① 预览 | `{contentId}` | 两步骤 | `VIEW` |
| ② 编辑 | `{contentId}` | 两步骤 | `EDIT` |
| ③ 反馈列表 | `{contentId}` | 两步骤 | `VIEW` |
| ④ 新建反馈 | `{contentId}` | 两步骤 | `REVIEW` |
| ⑤ 推进反馈 | `{feedbackId}` | 两步骤（反查 `project_id`） | `REVIEW` |
| ⑥ 单条审核 | `{contentId}` | 两步骤 | `REVIEW` |
| ⑦ 批量审核 | `{projectId}` | `ProjectAccessDep` | `REVIEW` |

**两步骤鉴权的顺序不可调换**：权限判定需要资源的 `project_id`，而它只能
从资源本身来。反查不到 ⇒ **404 且不区分"不存在"与"不属于你"**
（协议 §1.2 防枚举）。

### 为什么 ④⑤⑥⑦ 用 `REVIEW` 而不是 `EDIT`

`permissions.py:85` 对 `REVIEW` 的原文定义是「**生成、反馈、人工审核**」，
`content/api.py` 的模块 docstring 也明写「那是……其落点是 WP29~WP31 的
审核动作」。本包的四个动作逐条对得上：④ 提反馈、⑤ 处理反馈、⑥ 单条人工
审核、⑦ 批量人工审核 —— 它们**不是**"编辑内容"（②），而是对内容做出
判定。`EDIT` 与 `REVIEW` 在当前矩阵下逐行相同（`permissions.py:128-150`），
因此这个选择**只能靠语义判对错** —— 这正是要写清、而不是随手挑一个的原因。

**不新建权限动作**：与 WP22~WP28 同一条取向。

## 二、并发：两条写路径、两把锁、两个判断点

| 端点 | ETag 载体 | 前置校验处 | 权威判断处 |
|---|---|---|---|
| ② 编辑 | `content_items.lock_version` | `require_if_match`（端点，写之前） | `content.service.append_edited_version`（**行锁内**） |
| ⑤ 推进反馈 | `content_feedback_items.lock_version` | `require_if_match`（端点，写之前） | `service.advance_feedback`（读后立比 + 条件 UPDATE） |

两处都调 `platform.concurrency.require_if_match`，**不自己拼 ETag 比较**：
缺失 ⇒ 428、格式非法 ⇒ 400、不匹配 ⇒ 412 且回带 `details.currentEtag`。
这三条分支的键名是前端契约（协议 §1.2），手写一份就是会漂移的第二处定义。

**为什么端点还要"先读一次当前版本"**：`require_if_match` 的
`current_version` 必须来自**本次请求内的读数**。② 用
`content_bridge.read_content_edit_target`（一次读，身份侧 + 版本侧同快照，
且顺带给出 `etag`）；⑤ 用 `feedback_repo.get_feedback_item`（反馈行的锁版本）。
这次读**不是**并发保证 —— 真正的保证在服务层；它只负责把 412 尽量早、
尽量可读地给出。

**② 的 `expected_lock_version` 取自同一份快照**：`EditTarget.lock_version`
就是 `If-Match` 校验通过的那个值。若两次分别取，客户端拿 A 版正文、
服务端用 B 版期望值，412 会以"你明明带对了"的形式出现（ISS-66 形态）。

## 三、三码分工（协议 §1.2）

· **412 `VERSION_CONFLICT`** —— 锁版本不一致（内容锁 / 反馈锁，两个判断点）。
· **409 `INVALID_TRANSITION`** —— 当前数据状态不允许（如有未解决的
  `BLOCKING` 反馈时标记通过；或内容还没有任何版本）。
· **422 `VALIDATION_FAILED`** —— 请求里的值与当前数据对不上（锚点形状、
  分类取值、证据绑定段数）。`details.reason` 机器可判、`details.field` 定位。

## 四、异常怎么到这里

`service.py` 抛 `platform.errors.ApiError`，本文件**不 catch 它**：
`platform/errors.py::register_exception_handlers` 已注册全局处理器
（`main.py:214`），端点函数体内的 `try` 只会把 `details` 的键名抄歪。

需要转译的只有两类**不属于 `ApiError`** 的异常：

1. `content.service.ContentGenerationError`（`ValueError` 族）——
   `read_edit_target` / `append_edited_version` 的业务前置不成立；
2. `content.schemas.SchemaViolation` / `media.schemas.SchemaViolation` ——
   证据绑定或段落 ref 校验失败。

两者的映射表在本文件（`_CONTENT_HTTP_BY_REASON` 与两个 `_raise_*`），
理由是 `content/api.py` 自己的映射表是**它的**契约，跨模块 import 私有名
会让"它改一个 reason 的码"静默改变本包的行为。

## 五、`decision` / `state` 等受控取值不由端点判

取值域在 `service.py`（`_required_choice`），失败给 422 + `reason`。
端点再声明一遍 `Literal[...]` 会让同一错误有两种形状 —— 详见 `schemas.py`。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final, NoReturn

from fastapi import APIRouter, Path, Query, Request, Response, status

from rednote.modules.content.repository import ContentRepository
from rednote.modules.content.schemas import SchemaViolation as ContentSchemaViolation
from rednote.modules.content.service import ContentGenerationError
from rednote.modules.content.types import (
    REASON_BASELINE_MISMATCH,
    REASON_BASELINE_NOT_READY,
    REASON_CONTENT_NOT_FOUND,
    REASON_CONTENT_VERSION_NOT_FOUND,
)
from rednote.modules.feedback import content_bridge, media_bridge, service
from rednote.modules.feedback.dependencies import (
    FeedbackBaselineRepoDep,
    FeedbackContentRepoDep,
    FeedbackRepoDep,
    FeedbackVersionRepoDep,
)
from rednote.modules.feedback.repository import FeedbackRepository
from rednote.modules.feedback.schemas import (
    ContentEditRequestBody,
    EditedVersionResponseBody,
    FeedbackAdvanceRequestBody,
    FeedbackCreateRequestBody,
    FeedbackItemResponseBody,
    PreviewResponseBody,
    ReviewBatchRequestBody,
    ReviewBatchResponseBody,
    ReviewDecisionRequestBody,
    ReviewResponseBody,
)
from rednote.modules.feedback.types import ACTOR_KIND_USER, ANCHOR_KIND_PARAGRAPH
from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.media.schemas import SchemaViolation as MediaSchemaViolation
from rednote.modules.projects.dependencies import (
    ProjectAccessDep,
    ProjectRepoDep,
    resolve_project_access,
)
from rednote.modules.projects.permissions import ProjectAction
from rednote.platform.api_responses import READ_RESPONSES, WRITE_RESPONSES
from rednote.platform.concurrency import build_etag, require_if_match
from rednote.platform.errors import ApiError, DataEnvelope, ErrorCode
from rednote.platform.pagination import (
    PageParamsDep,
    PaginatedEnvelope,
    list_response,
    single_response,
)

router = APIRouter(tags=["feedback"])

# 只读端点（①③）与写端点（②④⑤⑥⑦）各自的错误响应集合。
# 写集合已含 409 / 412 / 428（`CONCURRENCY_ERROR_RESPONSES`），
# 不必再单独合并 —— 与 `content/api.py` 的 `CONTENT_READ/WRITE_RESPONSES` 同款。
FEEDBACK_READ_RESPONSES = READ_RESPONSES
FEEDBACK_WRITE_RESPONSES = WRITE_RESPONSES

#: 反馈项不存在时的 `details.reason`。反馈行被 `ON DELETE RESTRICT` 守着，
#: 正常路径下不会被删；反查不到只可能是 id 拼错或从未存在 —— 两者同码。
REASON_FEEDBACK_ITEM_NOT_FOUND: Final[str] = "FEEDBACK_ITEM_NOT_FOUND"


# ---------------------------------------------------------------------------
# 零、本文件自己的错误工厂与翻码表（**不跨模块借私有名**）
# ---------------------------------------------------------------------------


def _not_found(kind: str, resource_id: uuid.UUID, *, reason: str) -> ApiError:
    """构造 404 `RESOURCE_NOT_FOUND`（两步骤鉴权的第一步失败）。

    `kind` 是协议 §1.2 的 `details.resource` 口径（`content` 侧用
    `contentItem`，反馈侧用 `feedbackItem`），便于前端区分"哪个对象不在"。
    """
    return ApiError(
        ErrorCode.RESOURCE_NOT_FOUND,
        details={"reason": reason, "resource": kind, "resourceId": str(resource_id)},
    )


#: `ContentGenerationError.reason` → `(协议错误码, 出错字段)`。
#:
#: 只登记 `read_edit_target` / `append_edited_version` 可能抛出的四个 ——
#: 不登记的原因会显式崩（见 `_raise_content_error`），而不是静默变 422：
#: "新增了一个 reason 却忘了登记"若被静默兜住，排障时无法从状态码看出原因。
#:
#: | reason | 码 | 判据 |
#: |---|---|---|
#: | `CONTENT_NOT_FOUND` | 404 | 被寻址的内容不存在 |
#: | `CONTENT_VERSION_NOT_FOUND` | 409 | 内容还没有任何版本 —— 换任何参数都不行，要先跑一次生成 |
#: | `BASELINE_PROJECT_MISMATCH` | 422 | 基线属于另一个项目 ⇒ 数据装配错误，但请求/项目状态可修 |
#: | `BASELINE_NOT_READY` | 409 | 生效基线关联的 Brief 没有 claim ⇒ 判据域为空，重试无用，要先补 Brief |
_CONTENT_HTTP_BY_REASON: Final[dict[str, tuple[ErrorCode, str | None]]] = {
    REASON_CONTENT_NOT_FOUND: (ErrorCode.RESOURCE_NOT_FOUND, None),
    REASON_CONTENT_VERSION_NOT_FOUND: (ErrorCode.INVALID_TRANSITION, None),
    REASON_BASELINE_MISMATCH: (ErrorCode.VALIDATION_FAILED, None),
    REASON_BASELINE_NOT_READY: (ErrorCode.INVALID_TRANSITION, None),
}


def _raise_content_error(exc: ContentGenerationError) -> NoReturn:
    """把 `content.service` 的业务前置失败翻成协议错误，**总是抛**。

    `NoReturn` 让调用点在 `except` 分支后不必再补 `raise` / `assert False`。
    """
    entry = _CONTENT_HTTP_BY_REASON.get(exc.reason)
    if entry is None:  # pragma: no cover - 新增 reason 未登记时的显式崩溃
        raise RuntimeError(
            f"未登记 HTTP 映射的 reason={exc.reason!r}"
            f"（已登记：{sorted(_CONTENT_HTTP_BY_REASON)}）。"
            "新增 REASON_* 时必须同步登记 —— 否则它会静默变成 422。"
        )
    code, field = entry
    details: dict[str, Any] = {"reason": exc.reason}
    if field is not None:
        details["fieldErrors"] = [
            {"field": field, "type": exc.reason, "message": str(exc)}
        ]
    raise ApiError(code, str(exc), details=details)


def _raise_content_schema_error(exc: ContentSchemaViolation) -> NoReturn:
    """证据绑定校验失败 ⇒ 422（`reason` / `field` 原样透出）。

    `validate_bindings` 的失败是**请求里的绑定与合并后的正文对不上**
    （缺段 / 越界 / 绑满 / claim 不存在）—— 换一组绑定就能成功，
    因此是 422 而不是 409。
    """
    raise ApiError(
        ErrorCode.VALIDATION_FAILED,
        str(exc),
        details={
            "reason": exc.reason,
            "fieldErrors": [
                {"field": exc.field, "type": exc.reason, "message": str(exc)}
            ],
        },
    )


def _raise_paragraph_ref_error(exc: MediaSchemaViolation) -> NoReturn:
    """段落 ref 校验失败 ⇒ 422（**与 media 自己的映射刻意不同**）。

    media 模块把 `PARAGRAPH_REF_UNKNOWN` 映成 **409**（`media/api.py:276`）：
    在它的流程里 ref 是**系统自己**从上一版正文派生的，指不到当前段落意味着
    "正文状态变了，重试同一个请求无用"。

    反馈创建不同：`paragraphRef` 是**客户端提交的请求字段**。用户刷新预览、
    重新选一段之后**同一个请求形状**就能成功 —— 这是 422 的定义
    （"改请求参数能成功"），也是本包服务层对锚点类错误的一贯码
    （`service._invalid`）。两处码不同不是疏漏，是两种流程的修法不同。
    """
    raise ApiError(
        ErrorCode.VALIDATION_FAILED,
        str(exc),
        details={
            "reason": exc.reason,
            "fieldErrors": [
                {"field": exc.field, "type": exc.reason, "message": str(exc)}
            ],
        },
    )


# ---------------------------------------------------------------------------
# 一、两步骤鉴权的前置：由资源 id 反查 project_id
# ---------------------------------------------------------------------------


def _content_project_id(
    repo: ContentRepository, content_id: uuid.UUID
) -> uuid.UUID:
    """内容 → 项目 id。**不存在直接 404**（两步骤鉴权第一步）。

    用 `get_content_item` 而不是 `read_content_detail`：这一步只问
    "它属于哪个项目"，不需要把 payload / 蓝图 / 候选 / 上下文全读出来。
    与 `content/api.py::_content_project_id` 同款，但**各写一份**：
    两个模块的 404 `details.reason` 不同（这是各自的对外契约）。
    """
    item = repo.get_content_item(content_id)
    if item is None:
        raise _not_found(
            "contentItem", content_id, reason=REASON_CONTENT_NOT_FOUND
        )
    return item.project_id


def _feedback_project_id(
    repo: FeedbackRepository, feedback_id: uuid.UUID
) -> uuid.UUID:
    """反馈项 → 项目 id。**不存在直接 404**（两步骤鉴权第一步）。

    ⑤ 的路径里没有 `contentId` / `projectId`，只能从反馈行反查。
    这里用仓储的**单列只读**（`feedback_item_project_id`），不把整行载入
    session —— 随后的 `get_feedback_item` 会在同一请求里再读一次拿锁版本，
    两次读各自只承担一件事。
    """
    project_id = repo.feedback_item_project_id(feedback_id)
    if project_id is None:
        raise _not_found(
            "feedbackItem", feedback_id, reason=REASON_FEEDBACK_ITEM_NOT_FOUND
        )
    return project_id


# ---------------------------------------------------------------------------
# 二、反馈行的响应视图与锚点新鲜度
# ---------------------------------------------------------------------------


def _anchor_state(repo: FeedbackRepository, item: Any) -> str:
    """读时判定段落锚点是否还指着当前正文（`FRESH` / `STALE`）。

    判据与 `service._feedback_item_dict` 是**同一行规则**（段落锚点且 ref
    不在当前正文的段落集合里 ⇒ `STALE`）。这里不 import 服务层的私有名 ——
    本仓没有任何 `api.py` 从 `service` 借私有名，代价是这一行的重复，
    收益是两层边界清楚。

    基准是 `current_content_body`（投影优先），**不是** `observed_version_id`：
    后者记录"提意见时看到哪一版"，不用于判锚点现在是否还有效。
    """
    if item.anchor_kind != ANCHOR_KIND_PARAGRAPH:
        return service.ANCHOR_STATE_FRESH
    body = repo.current_content_body(item.content_item_id)
    if body is None:
        return service.ANCHOR_STATE_STALE
    known_refs = {
        ref for _index, _text, ref in media_bridge.split_body_into_paragraphs(body)
    }
    return (
        service.ANCHOR_STATE_FRESH
        if item.paragraph_ref in known_refs
        else service.ANCHOR_STATE_STALE
    )


def _feedback_item_view(
    repo: FeedbackRepository, item: Any
) -> FeedbackItemResponseBody:
    """ORM 行 → 响应体。

    逐字段取 ORM 同名列（字段名与列名一致，这正是 `models.py` 的命名），
    再补上**不是列**的 `anchorState`（读时判定）。不手写一份 20 行的
    camelCase 映射：那是 `service._feedback_item_dict` 的第二份副本。
    """
    data = {
        name: getattr(item, name)
        for name in FeedbackItemResponseBody.model_fields
        # `anchor_state` 不是列，读时判定后单独补。
        if name != "anchor_state"
    }
    data["anchor_state"] = _anchor_state(repo, item)
    return FeedbackItemResponseBody.model_validate(data)


# ---------------------------------------------------------------------------
# ① 预览聚合
# ---------------------------------------------------------------------------


@router.get(
    "/contents/{contentId}/preview",
    response_model=DataEnvelope[PreviewResponseBody],
    responses=FEEDBACK_READ_RESPONSES,
    summary="预览页聚合快照（当前版本 + 段落 + 图片位 + 标题候选 + 反馈计数）",
)
def get_content_preview(
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    feedback_repo: FeedbackRepoDep,
    content_repo: FeedbackContentRepoDep,
    version_repo: FeedbackVersionRepoDep,
    baseline_repo: FeedbackBaselineRepoDep,
    response: Response,
    content_id: Annotated[uuid.UUID, Path(alias="contentId")],
) -> dict[str, Any]:
    """预览页的全部聚合数据（协议 03 原件不在本机，形状由本包定义）。

    **权限**：两步骤 —— 先由 `contentId` 反查 `projectId`（不存在 ⇒ 404），
    再 `resolve_project_access` + `access.require(VIEW)`。预览是只读动作。

    **ETag 来源**：`PreviewPayload.etag` = `content_edit_etag(内容行)` =
    `build_etag(contentId, lock_version)`，与编辑端点 ② 的 `If-Match`
    **同一个值**。响应头回带它，客户端保存后发起编辑时原样回传即可。
    body 里另有一份 `etag`，让前端不必读响应头。

    **一次装配**：服务层先取一次 `EditTarget`，段落 / 图片位 / 标题候选
    都以它的 `current_version_id` 与 `body` 为锚点展开。端点不做第二次
    "当前版本"查询 —— 两次读之间可以插进一次编辑，那样展示的正文与
    ETag 就不属于同一版了。
    """
    project_id = _content_project_id(content_repo, content_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.VIEW)

    try:
        preview = service.build_preview(
            feedback_repo,
            content_repo,
            version_repo,
            baseline_repo,
            content_id=content_id,
        )
    except ContentGenerationError as exc:
        # 反查已确认内容存在 ⇒ 这里只剩"零版本"这一种（两次读之间被回滚）。
        _raise_content_error(exc)

    response.headers["ETag"] = preview.etag
    return single_response(preview.to_dict())


# ---------------------------------------------------------------------------
# ② 编辑追加版（If-Match）
# ---------------------------------------------------------------------------


@router.post(
    "/contents/{contentId}/edits",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[EditedVersionResponseBody],
    responses=FEEDBACK_WRITE_RESPONSES,
    summary="人工编辑（追加一个新版本，If-Match 乐观并发）",
)
def create_content_edit(
    request: Request,
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    content_repo: FeedbackContentRepoDep,
    version_repo: FeedbackVersionRepoDep,
    baseline_repo: FeedbackBaselineRepoDep,
    payload: ContentEditRequestBody,
    content_id: Annotated[uuid.UUID, Path(alias="contentId")],
) -> dict[str, Any]:
    """人工编辑正文 / 标题 / 标签 / 绑定，落成**新版本**（201）。

    **权限**：两步骤 + `EDIT`（`permissions.py:84`「编辑 Brief、方案、
    **内容**」）。编辑是本包唯一直接改内容的动作，因此是唯一的 `EDIT`。

    **并发（两层，位置不同）**：
      1. 端点先 `read_content_edit_target` 取本次请求的 `lock_version`；
      2. `require_if_match` 与请求头比对 —— 缺失 428 / 格式非法 400 /
         不匹配 412（回带 `details.currentEtag`）；
      3. 把**同一个** `lock_version` 作为 `expected_lock_version` 传给
         `service.edit_content`，权威判断在
         `content.service.append_edited_version` 的**行锁内**（读锁版本 →
         写前比对 → 条件 UPDATE）。端点这一层只负责"尽量早、可读地拒绝"，
         不承担并发保证。

    **201 而不是 200**：追加了一条 `content_versions` 行（内容版本不可变），
    返回的是"新版本"，不是"被修改的内容"。响应体是版本摘要；
    新的内容 ETag 由下一次 `GET /preview` 给出（客户端编辑后本来就要
    重新取段落与图片位）。

    **422 的来源**：合并后的正文与 `bindings` 必须成对过 `validate_bindings`
    （R1/R4 在编辑路径上不旁路），失败抛 `content.schemas.SchemaViolation`，
    本文件映成 422 并带 `reason` / `field`。
    """
    project_id = _content_project_id(content_repo, content_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.EDIT)

    try:
        # 读侧快照：lock_version / etag / 当前版本来自同一次读取。
        target = content_bridge.read_content_edit_target(
            content_repo, content_id=content_id
        )
    except ContentGenerationError as exc:
        _raise_content_error(exc)

    require_if_match(
        request,
        resource_id=content_id,
        current_version=target.lock_version,
        resource_label="内容",
    )

    try:
        version = service.edit_content(
            content_repo,
            version_repo=version_repo,
            baseline_repo=baseline_repo,
            content_id=content_id,
            # 与 If-Match 校验通过的是**同一个值**（同一份 EditTarget）。
            expected_lock_version=target.lock_version,
            title=payload.title,
            body=payload.body,
            tags=payload.tags,
            bindings=payload.bindings,
            created_by=user.id,
        )
    except ContentGenerationError as exc:
        _raise_content_error(exc)
    except ContentSchemaViolation as exc:
        _raise_content_schema_error(exc)

    # `ContentVersionSummary` 是 dataclass，字段名与响应模型一一对应 ——
    # 用 `from_attributes` 读，不手写一份 10 行的字典（ISS-66 形态）。
    return single_response(
        EditedVersionResponseBody.model_validate(version, from_attributes=True)
    )


# ---------------------------------------------------------------------------
# ③ 反馈列表（游标分页）
# ---------------------------------------------------------------------------


@router.get(
    "/contents/{contentId}/feedback",
    response_model=PaginatedEnvelope[FeedbackItemResponseBody],
    responses=FEEDBACK_READ_RESPONSES,
    summary="一条内容的反馈列表（新到旧，游标分页，含锚点新鲜度）",
)
def list_content_feedback(
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    feedback_repo: FeedbackRepoDep,
    content_repo: FeedbackContentRepoDep,
    params: PageParamsDep,
    content_id: Annotated[uuid.UUID, Path(alias="contentId")],
    state: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    anchor_kind: Annotated[str | None, Query(alias="anchorKind")] = None,
) -> dict[str, Any]:
    """反馈列表（协议 03 原件不在本机，形状由本包定义）。

    **权限**：两步骤 + `VIEW`。

    **分页在服务层**：`service.list_feedback` 自己解码游标、按
    `created_at DESC, id DESC` 做 keyset 查询、组装 `PageInfo`。端点
    **不再调** `paginate_items` —— 那是内存分页，二次分页会把
    `hasMore` / `nextCursor` 算成"页面中页面"的错位结果。
    这里只把 `FeedbackPage.items` 与 `.page` 交给 `list_response` 包装。

    **游标绑定筛选**：`CursorSpec.filters` 含 `contentItemId` / `state` /
    `category` / `anchorKind`。把"按 OPEN 筛选"的游标用到"全部"的查询上
    会被 400 拒绝（`cursor_binding_mismatch`），而不是静默返回错位的一页。

    **R5**：`STALE` 的反馈**照常返回**，只带 `anchorState` 标记 ——
    条数不因锚点失效而减少。
    """
    project_id = _content_project_id(content_repo, content_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.VIEW)

    page = service.list_feedback(
        feedback_repo,
        content_id=content_id,
        page_params=params,
        state=state,
        category=category,
        anchor_kind=anchor_kind,
    )
    return list_response(list(page.items), page.page)


# ---------------------------------------------------------------------------
# ④ 新建反馈
# ---------------------------------------------------------------------------


@router.post(
    "/contents/{contentId}/feedback",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[FeedbackItemResponseBody],
    responses=FEEDBACK_WRITE_RESPONSES,
    summary="新建一条反馈（锚点三选一，首行事件与反馈行同事务）",
)
def create_content_feedback(
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    feedback_repo: FeedbackRepoDep,
    content_repo: FeedbackContentRepoDep,
    payload: FeedbackCreateRequestBody,
    content_id: Annotated[uuid.UUID, Path(alias="contentId")],
) -> dict[str, Any]:
    """新建反馈（201）。

    **权限**：两步骤 + `REVIEW`（"反馈"是 `REVIEW` 的原文定义之一）。
    反馈不是"编辑内容"，它是审核工作流里的一个动作。

    **来源固定**：`authorKind = USER`、`createdBy = 当前用户`。请求体里
    没有这两个字段 —— 让调用方自报"我是 AI"没有业务收益，却会让审计面
    （`content_feedback_events.actor_kind`）失真。

    **没有 If-Match**：新建不覆盖任何既有行，因此没有可冲突的锁版本。
    锚点的新鲜度由服务层对着**当前正文**校验（`observedVersionId` 只记录
    "提意见时看到哪一版"，不用于校验）。

    **事务**：反馈行与首行事件（`from_state = NULL`）由
    `service.create_feedback` 在同一次 flush 里写入，提交边界归请求级
    `session_scope` —— 两者不可能一个落库一个没落（R6）。

    **422**：锚点形状（三选一 / `paragraphRef` 必须对应当前段落 /
    `mediaItemId` 必须属于本条内容）、分类取值、正文边界都在服务层判，
    `details.reason` 机器可判。段落 ref 由 `media_bridge` 转发 media 的
    纯函数校验，失败在本文件映成 422（理由见 `_raise_paragraph_ref_error`）。
    """
    project_id = _content_project_id(content_repo, content_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.REVIEW)

    try:
        item = service.create_feedback(
            feedback_repo,
            project_id=project_id,
            content_id=content_id,
            observed_version_id=payload.observed_version_id,
            anchor_kind=payload.anchor_kind,
            category=payload.category,
            severity=payload.severity,
            scope=payload.scope,
            author_kind=ACTOR_KIND_USER,
            body=payload.body,
            paragraph_ref=payload.paragraph_ref,
            selected_text=payload.selected_text,
            media_item_id=payload.media_item_id,
            suggested_category=payload.suggested_category,
            created_by=user.id,
        )
    except MediaSchemaViolation as exc:
        _raise_paragraph_ref_error(exc)

    return single_response(_feedback_item_view(feedback_repo, item))


# ---------------------------------------------------------------------------
# ⑤ 推进反馈状态（If-Match）
# ---------------------------------------------------------------------------


@router.patch(
    "/feedback/{feedbackId}",
    response_model=DataEnvelope[FeedbackItemResponseBody],
    responses=FEEDBACK_WRITE_RESPONSES,
    summary="推进反馈状态（If-Match 保护反馈行自己的 lock_version）",
)
def advance_feedback(
    request: Request,
    response: Response,
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    feedback_repo: FeedbackRepoDep,
    payload: FeedbackAdvanceRequestBody,
    feedback_id: Annotated[uuid.UUID, Path(alias="feedbackId")],
) -> dict[str, Any]:
    """推进一条反馈的状态（OPEN → IN_PROGRESS → RESOLVED / REJECTED）。

    **权限**：路径里没有 `projectId` ⇒ 两步骤鉴权：先
    `feedback_item_project_id` 反查（不存在 ⇒ 404），再
    `resolve_project_access` + `access.require(REVIEW)`。

    **并发（两层）**：
      1. `require_if_match` 的 `current_version` 来自本请求刚读到的
         `lock_version`，ETag 形如 `"{feedbackId}:{lockVersion}"`
         （`build_etag`，**不是**内容 id —— 这里保护的是反馈行自己的锁）；
      2. `service.advance_feedback` 在读行之后**立即**比对，
         并用条件 UPDATE（`WHERE lock_version = ?`）兜第二道。
         两处失败都回 412，且 `details.currentEtag` 是库里读到的真值。

    **本端点没有 409**：状态迁移矩阵（"终态不可再迁移"）**不在本波落地** ——
    `service.advance_feedback` 只校验 `toState` 在取值域内、`REJECTED`
    必须带理由（422），并对锁版本不匹配回 412。任何合法状态之间都可以
    迁移。这条缺口已登记在说明文档 §1.4 第 10 条，不在端点层临时加一份
    矩阵（那是第二个真值来源）。

    **响应头回带新 ETag**：成功迁移后 `lock_version` 已 +1，客户端拿
    旧 ETag 再 PATCH 必然 412 —— 回带新的，让连续操作不必多发一次 GET。
    """
    project_id = _feedback_project_id(feedback_repo, feedback_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.REVIEW)

    current = feedback_repo.get_feedback_item(feedback_id)
    if current is None:  # 反查与这次读之间被删；RESTRICT 下几乎不可达。
        raise _not_found(
            "feedbackItem", feedback_id, reason=REASON_FEEDBACK_ITEM_NOT_FOUND
        )
    current_lock_version = int(current.lock_version)

    require_if_match(
        request,
        resource_id=feedback_id,
        current_version=current_lock_version,
        resource_label="反馈",
    )

    item = service.advance_feedback(
        feedback_repo,
        feedback_id=feedback_id,
        expected_lock_version=current_lock_version,
        to_state=payload.to_state,
        # 执行者 = 当前登录用户；客户端不能自报 actorKind / actorId。
        actor_kind=ACTOR_KIND_USER,
        note=payload.note,
        reject_reason=payload.reject_reason,
        resulting_version_id=payload.resulting_version_id,
        actor_id=user.id,
    )

    # `advance_feedback` 已 reload，`lock_version` 是迁移后的真值。
    response.headers["ETag"] = build_etag(item.id, int(item.lock_version))
    return single_response(_feedback_item_view(feedback_repo, item))


# ---------------------------------------------------------------------------
# ⑥ 单条审核
# ---------------------------------------------------------------------------


@router.post(
    "/contents/{contentId}/reviews",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[ReviewResponseBody],
    responses=FEEDBACK_WRITE_RESPONSES,
    summary="记录一次人工审核决定（通过 / 毙掉）",
)
def create_content_review(
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    feedback_repo: FeedbackRepoDep,
    content_repo: FeedbackContentRepoDep,
    payload: ReviewDecisionRequestBody,
    content_id: Annotated[uuid.UUID, Path(alias="contentId")],
) -> dict[str, Any]:
    """为某条内容的某个版本记录审核决定（201，**追加一行**）。

    **权限**：两步骤 + `REVIEW`（"人工审核"）。

    **为什么没有 If-Match**：`content_reviews` **不可变**（只追加），
    没有"覆盖别人的决定"这回事。改判是**再追加一行**并用
    `supersedesId` 指回旧决定 —— 用 ETag 保护一个只增不改的表，
    只会给正常改判多加一个 412 失败分支。

    **409 `INVALID_TRANSITION`**：`decision = APPROVED` 而该内容存在
    未解决的 `BLOCKING` 反馈时，服务层拒绝（判据下推 SQL）。这不是
    "版本过期"也不是"请求格式错"，而是"当前数据不允许通过" ——
    先处理阻断项再来，**刷新重试无用**。

    **201 而不是 200**：审核结论是**新资源**（一行不可变历史）。
    """
    project_id = _content_project_id(content_repo, content_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.REVIEW)

    review = service.decide_review(
        feedback_repo,
        project_id=project_id,
        content_item_id=content_id,
        subject_version_id=payload.subject_version_id,
        decision=payload.decision,
        reason=payload.reason,
        decided_by=user.id,
        supersedes_id=payload.supersedes_id,
    )

    return single_response(
        ReviewResponseBody.model_validate(review, from_attributes=True)
    )


# ---------------------------------------------------------------------------
# ⑦ 批量审核
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{projectId}/content-reviews",
    response_model=DataEnvelope[ReviewBatchResponseBody],
    responses=FEEDBACK_WRITE_RESPONSES,
    summary="批量审核（逐条独立判定，部分成功也返回 200）",
)
def create_content_reviews_batch(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    feedback_repo: FeedbackRepoDep,
    payload: ReviewBatchRequestBody,
) -> dict[str, Any]:
    """批量记录审核决定。

    **权限**：路径里带 `projectId` ⇒ 直接用 `ProjectAccessDep`
    （它做成员级判定）+ `access.require(REVIEW)`。不需要两步骤。

    **200 而不是 201**：请求可能**部分成功、部分失败**。统一 201 会让
    "20 条里 3 条失败"看起来像全部成功；逐条结果在
    `data.results[]` 的 `ok` / `review` / `error` 里。

    **逐条独立子事务**：服务层给每条包一个 SAVEPOINT，一条因
    `BLOCKING` 失败只回滚那一条（PostgreSQL 语句报错会把整个事务标成
    aborted，不带 savepoint 就无法继续处理后面的条目）。

    **版本由服务层取"当前版本"**（投影优先，不是末版）：批量请求只给
    内容 id，审核结论仍必须绑定具体版本。
    """
    access.require(ProjectAction.REVIEW)

    results = service.decide_reviews_batch(
        feedback_repo,
        project_id=access.project_id,
        content_item_ids=payload.content_item_ids,
        decision=payload.decision,
        reason=payload.reason,
        decided_by=user.id,
    )

    # 服务层返回的是 camelCase 字典（`_review_dict` 的形状）；
    # `response_model` 负责逐条校验与序列化，这里不再逐字段转换。
    return single_response({"results": results})


__all__ = ["router"]
