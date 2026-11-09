"""WP30 AI 自检端点（**六个**，4 条新路径）。

| # | 方法 | 路径 | 成功码 | 服务函数 |
|---|---|---|---|---|
| ① | POST | `/contents/{contentId}/self-checks` | 201 | `service.run_self_check` |
| ② | GET | `/contents/{contentId}/self-checks` | 200 | `service.list_runs` |
| ③ | GET | `/self-checks/{runId}` | 200 | `service.get_run_detail` |
| ④ | POST | `/projects/{projectId}/self-checks` | 200 | `service.run_self_checks_batch` |
| ⑤ | GET | `/projects/{projectId}/self-checks` | 200 | `service.list_latest_runs` |
| ⑥ | GET | `/self-check-rules` | 200 | `service.rule_catalog` |

---

## 一、权限：三种寻址方式，动作逐个给理由

| 端点 | 寻址 | 权限判定 | 动作 |
|---|---|---|---|
| ① 跑自检 | `{contentId}` | 两步骤 | `REVIEW` |
| ② 轮次历史 | `{contentId}` | 两步骤 | `VIEW` |
| ③ 单轮详情 | `{runId}` | 两步骤（反查 `project_id`） | `VIEW` |
| ④ 批量自检 | `{projectId}` | `ProjectAccessDep` | `REVIEW` |
| ⑤ 项目看板 | `{projectId}` | `ProjectAccessDep` | `VIEW` |
| ⑥ 规则目录 | —— | 登录即可 | —— |

**两步骤鉴权的顺序不可调换**：权限判定需要资源的 `project_id`，而它只能从
资源本身来。反查不到 ⇒ **404 且不区分"不存在"与"不属于你"**（协议 §1.2 防枚举）。

### 为什么①④ 用 `REVIEW` 而不是 `EDIT`

`permissions.py:85` 对 `REVIEW` 的原文定义是「生成、**反馈**、**人工审核**」。
跑一轮自检**会写入 AI 反馈**（阻断项），而"提反馈"正是 `REVIEW` 的定义之一；
它**不是**"编辑内容"（自检一个字都不改稿）。与 WP29 把"提反馈 / 处理反馈 /
人工审核"落 `REVIEW` 是同一条口径（WP29 说明 §4.1 第 1 条已被主 agent 照准）。

**不新建 `ProjectAction`**：与 WP22~WP29 同一条取向。

### 为什么⑥ 只要登录

规则目录是**代码常量的只读投影**，没有项目维度、不含任何项目数据
（code / category / kind / 默认严重度 / 是否已实现 / 一句说明）。
给它加项目权限就得先要一个 `projectId` 参数，而那个参数不参与任何判定 ——
一个不参与判定的必填参数会让调用方以为"目录是按项目定制的"。

## 二、本包**零** `If-Match`（必须显式写出来，否则会被当成漏了并发保护）

三张表全是追加写，没有"原地改"的路径 ⇒ 没有乐观锁载体，也没有
"读-写窗口"要挡。`POST /contents/{id}/self-checks` 并发两次的结果是**两轮**
自检记录，这是正确行为（不是冲突）。唯一要防的是"同一阻断项被两轮各建一条
反馈"，那靠服务层的去重 + `(run_id, rule_code)` 唯一约束，不靠 ETag。

## 三、①为什么是 201、④为什么是 200

① 追加了一条 `self_check_runs` 行（**新资源**），返回它的详情 ⇒ 201。
④ 可能**部分成功、部分失败**；统一 201 会让"10 条里 3 条失败"看起来像
全部成功 ⇒ 200，逐条结果在 `data.results[]` 的 `ok` / `run` / `error` 里。

## 四、异常怎么到这里

`service.py` 抛 `platform.errors.ApiError`，本文件**不 catch 它**：
`platform/errors.py::register_exception_handlers` 已注册全局处理器，
端点函数体内的 `try` 只会把 `details` 的键名抄歪。

需要转译的只有一类**不属于 `ApiError`** 的异常：
`content.service.ContentGenerationError`（`ValueError` 族）——
`read_edit_target` 的业务前置不成立（内容不存在 / 一版都没有）。
映射表写在本文件（`_CONTENT_HTTP_BY_REASON`），理由是 `content/api.py`
自己的映射表是**它的**契约，跨模块 import 私有名会让"它改一个 reason 的码"
静默改变本包的行为。

**"内容还没有任何版本" ⇒ 409 而不是 422**：换任何请求参数都不行，
必须先跑一次生成。刷新重试无用。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final, NoReturn

from fastapi import APIRouter, Path, Query, status

from rednote.modules.content.repository import ContentRepository
from rednote.modules.content.service import ContentGenerationError
from rednote.modules.content.types import (
    REASON_BASELINE_MISMATCH,
    REASON_BASELINE_NOT_READY,
    REASON_CONTENT_NOT_FOUND,
    REASON_CONTENT_VERSION_NOT_FOUND,
)
from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.projects.dependencies import (
    ProjectAccessDep,
    ProjectRepoDep,
    resolve_project_access,
)
from rednote.modules.projects.permissions import ProjectAction
from rednote.modules.review import service
from rednote.modules.review.dependencies import (
    ReviewContentRepoDep,
    ReviewFeedbackRepoDep,
    ReviewPlanningRepoDep,
    ReviewRepoDep,
)
from rednote.modules.review.repository import ReviewRepository
from rednote.modules.review.schemas import (
    SelfCheckBatchRequestBody,
    SelfCheckBatchResponseBody,
    SelfCheckBoardResponseBody,
    SelfCheckCreateRequestBody,
    SelfCheckRuleCatalogResponseBody,
    SelfCheckRunDetailBody,
    SelfCheckRunSummaryBody,
)
from rednote.modules.review.types import REASON_SELF_CHECK_RUN_NOT_FOUND
from rednote.platform.api_responses import READ_RESPONSES, WRITE_RESPONSES
from rednote.platform.errors import ApiError, DataEnvelope, ErrorCode
from rednote.platform.pagination import (
    PageParamsDep,
    PaginatedEnvelope,
    list_response,
    single_response,
)

router = APIRouter(tags=["review"])

# 只读端点（②③⑤⑥）与写端点（①④）各自的错误响应集合。
# 写集合已含 409 / 412 / 428（`CONCURRENCY_ERROR_RESPONSES`）：本包不用
# 412 / 428（零 If-Match），但复用既有集合而不是手挑一个子集 ——
# 手挑的子集会在协议新增一个通用码时漏掉，而 `responses=` 的缺失是静默的。
REVIEW_READ_RESPONSES = READ_RESPONSES
REVIEW_WRITE_RESPONSES = WRITE_RESPONSES


# ---------------------------------------------------------------------------
# 零、本文件自己的错误工厂与翻码表（**不跨模块借私有名**）
# ---------------------------------------------------------------------------


def _not_found(kind: str, resource_id: uuid.UUID, *, reason: str) -> ApiError:
    """构造 404 `RESOURCE_NOT_FOUND`（两步骤鉴权的第一步失败）。

    `kind` 是协议 §1.2 的 `details.resource` 口径，便于前端区分
    "哪个对象不在"（`contentItem` / `selfCheckRun`）。
    """
    return ApiError(
        ErrorCode.RESOURCE_NOT_FOUND,
        details={"reason": reason, "resource": kind, "resourceId": str(resource_id)},
    )


#: `ContentGenerationError.reason` → 协议错误码。
#:
#: 只登记 `read_edit_target` 可能抛出的四个 —— 不登记的原因会**显式崩**
#: （见 `_raise_content_error`），而不是静默变 422："新增了一个 reason 却忘了
#: 登记"若被静默兜住，排障时无法从状态码看出原因。
#:
#: | reason | 码 | 判据 |
#: |---|---|---|
#: | `CONTENT_NOT_FOUND` | 404 | 被寻址的内容不存在 |
#: | `CONTENT_VERSION_NOT_FOUND` | 409 | 内容还没有任何版本 —— 换任何参数都不行，要先跑一次生成 |
#: | `BASELINE_PROJECT_MISMATCH` | 422 | 基线属于另一个项目 ⇒ 数据装配错误，但请求/项目状态可修 |
#: | `BASELINE_NOT_READY` | 409 | 生效基线关联的 Brief 没有 claim ⇒ 判据域为空，重试无用 |
_CONTENT_HTTP_BY_REASON: Final[dict[str, ErrorCode]] = {
    REASON_CONTENT_NOT_FOUND: ErrorCode.RESOURCE_NOT_FOUND,
    REASON_CONTENT_VERSION_NOT_FOUND: ErrorCode.INVALID_TRANSITION,
    REASON_BASELINE_MISMATCH: ErrorCode.VALIDATION_FAILED,
    REASON_BASELINE_NOT_READY: ErrorCode.INVALID_TRANSITION,
}


def _raise_content_error(exc: ContentGenerationError) -> NoReturn:
    """把 `content.service` 的业务前置失败翻成协议错误，**总是抛**。

    `NoReturn` 让调用点在 `except` 分支后不必再补 `raise` / `assert False`。
    """
    code = _CONTENT_HTTP_BY_REASON.get(exc.reason)
    if code is None:  # pragma: no cover - 新增 reason 未登记时的显式崩溃
        raise RuntimeError(
            f"未登记 HTTP 映射的 reason={exc.reason!r}"
            f"（已登记：{sorted(_CONTENT_HTTP_BY_REASON)}）。"
            "新增 REASON_* 时必须同步登记 —— 否则它会静默变成 422。"
        )
    raise ApiError(code, str(exc), details={"reason": exc.reason})


# ---------------------------------------------------------------------------
# 一、两步骤鉴权的前置：由资源 id 反查 project_id
# ---------------------------------------------------------------------------


def _content_project_id(repo: ContentRepository, content_id: uuid.UUID) -> uuid.UUID:
    """内容 → 项目 id。**不存在直接 404**（两步骤鉴权第一步）。

    用 `get_content_item` 而不是 `read_content_detail`：这一步只问
    "它属于哪个项目"。与 `content/api.py` / `feedback/api.py` 的同名函数
    **各写一份** —— 三个模块的 404 `details.resource` 口径是各自的对外契约。
    """
    item = repo.get_content_item(content_id)
    if item is None:
        raise _not_found("contentItem", content_id, reason=REASON_CONTENT_NOT_FOUND)
    return item.project_id


def _run_project_id(repo: ReviewRepository, run_id: uuid.UUID) -> uuid.UUID:
    """自检轮次 → 项目 id。**不存在直接 404**（两步骤鉴权第一步）。

    ③ 的路径里没有 `contentId` / `projectId`，只能从轮次行反查。
    这里用仓储的**单列只读**，不把整行载入 session —— 随后的
    `get_run_detail` 会在同一请求里再读一次（连带 rule_results 与 findings），
    两次读各自只承担一件事。
    """
    project_id = repo.run_project_id(run_id)
    if project_id is None:
        raise _not_found(
            "selfCheckRun", run_id, reason=REASON_SELF_CHECK_RUN_NOT_FOUND
        )
    return project_id


# ---------------------------------------------------------------------------
# ① 跑一轮自检
# ---------------------------------------------------------------------------


@router.post(
    "/contents/{contentId}/self-checks",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[SelfCheckRunDetailBody],
    responses=REVIEW_WRITE_RESPONSES,
    summary="对当前版本跑一轮 AI 自检（同步；阻断项同事务落成 AI 反馈）",
)
def create_self_check(
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    review_repo: ReviewRepoDep,
    content_repo: ReviewContentRepoDep,
    planning_repo: ReviewPlanningRepoDep,
    feedback_repo: ReviewFeedbackRepoDep,
    payload: SelfCheckCreateRequestBody,
    content_id: Annotated[uuid.UUID, Path(alias="contentId")],
) -> dict[str, Any]:
    """跑一轮自检（201），返回本轮的完整详情。

    **权限**：两步骤 + `REVIEW`（本动作会写入 AI 反馈，见模块 docstring）。

    **请求体是空的**，但仍声明了模型以启用 `extra="forbid"`：传任何未知字段
    （例如想"只跑某几条规则"的 `ruleCodes`）必须 **422**，而不是被静默忽略。

    **没有 If-Match**：追加写，无锁可冲突（见模块 docstring 二）。

    **事务**：三张表 + AI 反馈 + 反馈的首行事件由 `service.run_self_check`
    在同一个 session 里写入，提交边界归请求级 `session_scope` ——
    不可能出现"自检轮次落库了但阻断反馈没落"（那会让 WP29 的审核闸放行）。
    """
    _ = payload  # 空请求体：它的作用是 `extra="forbid"`（见 docstring）。
    project_id = _content_project_id(content_repo, content_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.REVIEW)

    try:
        detail = service.run_self_check(
            review_repo,
            content_repo,
            planning_repo,
            feedback_repo,
            content_id=content_id,
            created_by=user.id,
        )
    except ContentGenerationError as exc:
        # 反查已确认内容存在 ⇒ 这里只剩"零版本"等业务前置不成立的情形。
        _raise_content_error(exc)

    return single_response(detail.to_dict())


# ---------------------------------------------------------------------------
# ② 轮次历史
# ---------------------------------------------------------------------------


@router.get(
    "/contents/{contentId}/self-checks",
    response_model=PaginatedEnvelope[SelfCheckRunSummaryBody],
    responses=REVIEW_READ_RESPONSES,
    summary="一条内容的自检轮次历史（新到旧，游标分页，含 subjectState）",
)
def list_self_checks(
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    review_repo: ReviewRepoDep,
    content_repo: ReviewContentRepoDep,
    params: PageParamsDep,
    content_id: Annotated[uuid.UUID, Path(alias="contentId")],
) -> dict[str, Any]:
    """轮次历史（第一条即最新）。

    **权限**：两步骤 + `VIEW`。

    **分页在服务层**：`service.list_runs` 自己解码游标、按
    `created_at DESC, id DESC` 做 keyset 查询、组装 `PageInfo`。端点
    **不再调** `paginate_items` —— 那是内存分页，二次分页会把
    `hasMore` / `nextCursor` 算成"页面中页面"的错位结果。

    **`STALE` 的轮次照常返回**，只带 `subjectState` 标记 ——
    条数不因"判的不是当前版"而减少（与 WP29 的 `anchorState` 同一条纪律）。
    """
    project_id = _content_project_id(content_repo, content_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.VIEW)

    page = service.list_runs(
        review_repo, content_id=content_id, page_params=params
    )
    return list_response(list(page.items), page.page)


# ---------------------------------------------------------------------------
# ③ 单轮详情
# ---------------------------------------------------------------------------


@router.get(
    "/self-checks/{runId}",
    response_model=DataEnvelope[SelfCheckRunDetailBody],
    responses=REVIEW_READ_RESPONSES,
    summary="单轮自检详情（8 类聚合 + 逐规则结论 + 逐命中）",
)
def get_self_check(
    user: CurrentUserDep,
    project_repo: ProjectRepoDep,
    review_repo: ReviewRepoDep,
    run_id: Annotated[uuid.UUID, Path(alias="runId")],
) -> dict[str, Any]:
    """单轮详情。

    **权限**：路径里没有 `projectId` ⇒ 两步骤鉴权：先 `run_project_id`
    反查（不存在 ⇒ 404 `SELF_CHECK_RUN_NOT_FOUND`），再
    `resolve_project_access` + `access.require(VIEW)`。

    **8 类聚合一个都不少**（含恒 SKIPPED 的三类）：聚合从
    `REVIEW_CATEGORIES` 起算而不是从库里读到的行起算，因此某一类即便
    一行都没有也会给出一张全零的卡片，而**不是让那一类消失**。
    """
    project_id = _run_project_id(review_repo, run_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.VIEW)

    detail = service.get_run_detail(review_repo, run_id=run_id)
    return single_response(detail.to_dict())


# ---------------------------------------------------------------------------
# ④ 批量自检
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{projectId}/self-checks",
    response_model=DataEnvelope[SelfCheckBatchResponseBody],
    responses=REVIEW_WRITE_RESPONSES,
    summary="批量自检（逐条独立判定，部分成功也返回 200）",
)
def create_self_checks_batch(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    review_repo: ReviewRepoDep,
    content_repo: ReviewContentRepoDep,
    planning_repo: ReviewPlanningRepoDep,
    feedback_repo: ReviewFeedbackRepoDep,
    payload: SelfCheckBatchRequestBody,
) -> dict[str, Any]:
    """批量跑自检。

    **权限**：路径里带 `projectId` ⇒ 直接用 `ProjectAccessDep`（成员级）
    + `access.require(REVIEW)`。不需要两步骤。

    ⚠️ **本端点不校验 `contentIds` 是否属于本项目**，但越界不会得逞：
    每条内容的自检轮次带 `project_id`（取自**内容自己的**归属，由
    `_assemble_subject` 从 `read_edit_target` 读出），而
    `trg_self_check_runs_consistency` 要求"内容必须属于本轮的项目" ——
    传入别的项目的内容时，那一条会在它自己的 savepoint 里失败并在
    `results[].error` 里如实报出，而不是静默写进本项目的看板。

    **200 而不是 201**：请求可能部分成功、部分失败。

    **逐条独立子事务**：服务层给每条包一个 SAVEPOINT。
    """
    access.require(ProjectAction.REVIEW)

    results = service.run_self_checks_batch(
        review_repo,
        content_repo,
        planning_repo,
        feedback_repo,
        content_item_ids=payload.content_item_ids,
        created_by=user.id,
    )
    return single_response({"results": results})


# ---------------------------------------------------------------------------
# ⑤ 项目看板
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/self-checks",
    response_model=DataEnvelope[SelfCheckBoardResponseBody],
    responses=REVIEW_READ_RESPONSES,
    summary="项目看板：每条内容最新一轮的自检摘要",
)
def list_project_self_checks(
    access: ProjectAccessDep,
    review_repo: ReviewRepoDep,
    content_ids: Annotated[list[uuid.UUID] | None, Query(alias="contentIds")] = None,
) -> dict[str, Any]:
    """每条内容**最新一轮**的摘要（原型列表页的 `AI 自检` 列）。

    **权限**：`ProjectAccessDep` + `access.require(VIEW)`。

    `contentIds` 可选：给了就只取这几条（列表页只渲染当前这一页的内容，
    不必把全项目的轮次都拉回来）。

    **没有自检过的内容不在结果里** —— 前端把内容列表左连接本结果后缺失的
    那些渲染成「未自检」（原型列表页本来就有这一态）。本端点不为它们编造
    一行：那会让"未自检"与"自检过"在数据结构上同形。
    """
    access.require(ProjectAction.VIEW)

    runs = service.list_latest_runs(
        review_repo,
        project_id=access.project_id,
        content_item_ids=content_ids,
    )
    return single_response({"runs": list(runs)})


# ---------------------------------------------------------------------------
# ⑥ 规则目录
# ---------------------------------------------------------------------------


@router.get(
    "/self-check-rules",
    response_model=DataEnvelope[SelfCheckRuleCatalogResponseBody],
    responses=REVIEW_READ_RESPONSES,
    summary="自检规则目录（代码注册表的只读投影，含『是否已实现』）",
)
def list_self_check_rules(user: CurrentUserDep) -> dict[str, Any]:
    """规则目录。

    **权限**：登录即可（无项目维度，见模块 docstring 一末段）。

    **`implemented = False` 的三条照常列出**：前端据此把 `TONE` /
    `DUPLICATE` / `SCENE` 三张卡片画成"未判 · 原因"，而不是从
    "一直没有命中"猜成"通过"。

    响应**不连库**（`service.rule_catalog` 是纯函数）：它回答的是
    "这套代码里有哪些规则"，而那个答案不随环境变 —— 这正是把注册表放在
    代码里（而不是配置表里）换来的性质。
    """
    _ = user  # 仅用于"必须登录"；目录内容不随用户变。
    return single_response(service.rule_catalog())


__all__ = ["router"]
