"""Brief 端点（协议 03 §5 的前五行，**五个**）。

| 方法 | 路径 | 成功码 | 依据 |
|---|---|---|---|
| POST | `/projects/{projectId}/brief-parse-jobs` | 202 | §5 第 1 行 |
| GET | `/projects/{projectId}/brief` | 200 | §5 第 2 行 |
| PUT | `/projects/{projectId}/brief-draft` | 200 | §5 第 3 行 |
| POST | `/projects/{projectId}/brief-versions` | 201 | §5 第 4 行 |
| POST | `/projects/{projectId}/brief-enrichment-jobs` | 202 | §5 第 5 行 |

---

## 一、协议原文与本实现的取舍

| 协议字段 | 本实现 | 理由 |
|---|---|---|
| `POST /brief-parse-jobs` 的 `幂等键` | 用 `Idempotency-Key` 头 | 协议 §1.3 的通用机制。**折进任务 key**（`brief_parse:{fileId}:{key}`），于是"运营点两次"与"事件自动解析"不会各建一个任务 |
| `PUT /brief-draft` 的 `If-Match` | 强制（缺 → 428） | 协议 §1.1：可变资源修改要求其当前 ETag。**先用 `If-Match` 前置校验，再用 `WHERE lock_version = ?` 兜住窗口** —— 两者都要（见 `platform/concurrency.py` 的 `require_if_match` docstring） |
| `POST /brief-versions` 的 `draftId` | 从请求体读，但**不强制等于当前草稿** | 一个项目只有一个草稿（`UQ(brief_drafts.brief_id)`）。传一个别的 id 是"客户端拿着过期页面"的信号，报 422 比静默忽略它更负责 |
| `POST /brief-enrichment-jobs` 的 `幂等键` | 同上 | — |
| 五个端点的错误体 | `READ_RESPONSES` / `WRITE_RESPONSES` | **每个端点都必须显式声明**：FastAPI 只检查 `responses` 里有没有 422，没有就自动挂上形状不同的 `HTTPValidationError`（`platform/api_responses.py` 的教训） |

## 二、`GET /brief` 会**写库**（已知代价，登记为待定项）

协议 §5 末段：

> Brief、信息和方案的初始空草稿由项目／上游结果事务创建；GET 返回其 ETag，
> 首次 PUT 也使用该 ETag，**不要求客户端猜测一个不存在资源的版本**。

协议把"创建初始草稿"归给项目创建事务，但项目创建是 WP10 的代码，
本包不能改它。因此本实现把它做成**幂等的懒创建**：
`GET /brief` 第一次被调用时建出 Brief 与空草稿，此后每次都是纯读。

**代价**：一个只读的预取/爬虫会造出草稿行。它违反"GET 是安全方法"的
HTTP 约定。收口方向是让主 agent 在 WP10 的项目创建事务里补一次
`ensure_brief`，那时本端点退化成纯读。**当前不做** ——
替代方案（首次 PUT 时才建）会让"GET 返回 ETag"这条协议原文无法满足。

## 三、为什么解析端点不接收 `parserProfile`

与 WP17 的 `POST /files/{fileId}/parse-jobs` 同一取舍：
数据结构 02 §4 里没有 `parser_profiles` 表，收一个无处落地的字段
会让调用方以为它生效了。解析器由文件类型决定（`parsers.route_for`）。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Header, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from rednote.modules.brief import service
from rednote.modules.brief.dependencies import (
    BriefRepoDep,
    FileRepoDep,
    JobRepoDep,
    ParseRepoDep,
)
from rednote.modules.brief.repository import BriefRepository
from rednote.modules.brief.types import (
    IDEMPOTENCY_SCOPE_BRIEF_ENRICH,
    IDEMPOTENCY_SCOPE_BRIEF_PARSE,
    KIND_BRIEF_ENRICH,
    KIND_BRIEF_PARSE,
    QUEUE_BRIEF,
)
from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.jobs import service as jobs_service
from rednote.modules.parsing.repository import ParseRepository
from rednote.modules.projects.dependencies import ProjectAccessDep
from rednote.modules.projects.permissions import ProjectAction
from rednote.platform.api_responses import READ_RESPONSES, WRITE_RESPONSES
from rednote.platform.concurrency import require_if_match
from rednote.platform.errors import DataEnvelope, build_meta

router = APIRouter(tags=["brief"])

# 端点可能返回 503（解构通道不可用时的任务失败会在后续查询里体现，
# 但入队本身不依赖外部服务）—— 因此用通用的读写集合。
BRIEF_READ_RESPONSES = READ_RESPONSES
BRIEF_WRITE_RESPONSES = WRITE_RESPONSES


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class ParseJobBody(BaseModel):
    """`POST /projects/{id}/brief-parse-jobs` 的请求体。"""

    model_config = ConfigDict(populate_by_name=True)

    file_id: uuid.UUID = Field(alias="fileId")


class DraftBody(BaseModel):
    """`PUT /projects/{id}/brief-draft` 的请求体。

    **三个可选块**（`None` = 本次不动这一块）。全量提交会让
    "两个运营分别改不同部分"变成互相覆盖 —— 见 `service.update_draft`。
    """

    model_config = ConfigDict(populate_by_name=True)

    base_version_id: uuid.UUID | None = Field(default=None, alias="baseVersionId")
    fields: dict[str, Any] | None = None
    claims: list[dict[str, Any]] | None = None
    issue_resolutions: list[dict[str, Any]] | None = Field(
        default=None, alias="issueResolutions"
    )


class VersionBody(BaseModel):
    """`POST /projects/{id}/brief-versions` 的请求体。"""

    model_config = ConfigDict(populate_by_name=True)

    draft_id: uuid.UUID = Field(alias="draftId")


class EnrichmentJobBody(BaseModel):
    """`POST /projects/{id}/brief-enrichment-jobs` 的请求体。"""

    model_config = ConfigDict(populate_by_name=True)

    brief_version_id: uuid.UUID = Field(alias="briefVersionId")
    allowed_source_ids: list[uuid.UUID] = Field(alias="allowedSourceIds")


# ---------------------------------------------------------------------------
# 响应体（供 OpenAPI 契约生成具体结构，而不是裸 object）
# ---------------------------------------------------------------------------


class BriefViewBody(BaseModel):
    """`GET /projects/{id}/brief` 的响应（协议 §5 第 2 行"BriefView"）。

    **`data` 的具体结构必须出现在契约里**（协议 §13.3：前端据此生成类型）。
    因此这里显式列出顶层字段；嵌套块（draft / version / versions）
    用 `dict` —— 它们的形状由 `service._draft_view` 等构造，
    逐字段重复一遍会让两处漂移（契约说 A、实现给 B）。
    """

    model_config = ConfigDict(populate_by_name=True)

    brief_id: uuid.UUID = Field(alias="briefId")
    project_id: uuid.UUID = Field(alias="projectId")
    active_version_id: uuid.UUID | None = Field(alias="activeVersionId")
    draft: dict[str, Any]
    version: dict[str, Any] | None
    versions: list[dict[str, Any]]
    blocking_issue_count: int = Field(alias="blockingIssueCount")


class SolidifiedVersionBody(BaseModel):
    """`POST /projects/{id}/brief-versions` 的 201 响应。"""

    model_config = ConfigDict(populate_by_name=True)

    version_id: uuid.UUID = Field(alias="versionId")
    revision_no: int = Field(alias="revisionNo")
    etag: str
    product_name: str = Field(alias="productName")
    summary: str
    claim_count: int = Field(alias="claimCount")
    issue_count: int = Field(alias="issueCount")
    created: bool


class DraftUpdatedBody(BaseModel):
    """`PUT /projects/{id}/brief-draft` 的 200 响应。"""

    model_config = ConfigDict(populate_by_name=True)

    draft: dict[str, Any]


class JobAcceptedBody(BaseModel):
    """两个 202 端点的响应体。

    `created=false` 表示复用了既有的排队中任务（幂等命中）——
    客户端据此区分"我刚发起的"与"系统里已经有了"。
    """

    model_config = ConfigDict(populate_by_name=True)

    job_id: uuid.UUID = Field(alias="jobId")
    kind: str
    state: str
    created: bool


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{projectId}/brief-parse-jobs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DataEnvelope[JobAcceptedBody],
    responses=BRIEF_WRITE_RESPONSES,
    summary="发起 Brief 解析与解构",
)
def create_brief_parse_job(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    brief_repo: BriefRepoDep,
    parse_repo: ParseRepoDep,
    file_repo: FileRepoDep,
    job_repo: JobRepoDep,
    body: ParseJobBody,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """发起 Brief 解析（**202，不同步解析也不同步解构**）。

    顺序（不可调换）：

      1. **权限**：`EDIT`（"编辑 Brief"是权限矩阵里的动作，
         而解构会改写草稿 —— 那是 Brief 的产物）；
      2. **判"这个文件是本项目的 BRIEF"** → 422 带 `hint`；
      3. **登记解析**（复用 WP17 的复用判据）→ 建任务，**同一事务**；
      4. 202。

    **为什么第 2 步在登记之前**：登记会写一行 `parse_runs`。
    先登记再判用途的话，一个"用途不对"的请求会留下一条永远没人消费的
    run —— 而它看起来和一次正常解析的 run 完全一样。
    """
    access.require(ProjectAction.EDIT)
    del user  # 权限已由 `access.require` 判定

    brief = service.ensure_brief(brief_repo, project_id=access.project_id)
    run, created = service.request_parse(
        parse_repo,
        file_repo,
        project_id=access.project_id,
        file_id=body.file_id,
    )

    view = jobs_service.enqueue_job(
        job_repo,
        kind=KIND_BRIEF_PARSE,
        queue=QUEUE_BRIEF,
        idempotency_scope=IDEMPOTENCY_SCOPE_BRIEF_PARSE,
        idempotency_key=(
            f"{body.file_id}:{run.parser_version}"
            if idempotency_key is None
            else f"{body.file_id}:{run.parser_version}:{idempotency_key}"
        ),
        input_hash=service.parse_input_hash(
            file_id=body.file_id, parser_version=run.parser_version
        ),
        project_id=access.project_id,
    )
    if view.created:
        # run 与 job 在**同一事务**里关联（处理器靠 `parse_runs.job_id` 反查）。
        run.job_id = view.job_id
        parse_repo.flush()
    del brief, created

    response.status_code = status.HTTP_202_ACCEPTED
    return {
        "data": {
            "jobId": str(view.job_id),
            "kind": view.kind,
            "state": view.state,
            "created": view.created,
        },
        "meta": build_meta(),
    }


@router.get(
    "/projects/{projectId}/brief",
    response_model=DataEnvelope[BriefViewBody],
    responses=BRIEF_READ_RESPONSES,
    summary="读取 Brief（原文链接、字段、claims、issues、版本）",
)
def get_brief(
    access: ProjectAccessDep,
    brief_repo: BriefRepoDep,
    parse_repo: ParseRepoDep,
    version_id: uuid.UUID | None = Query(default=None, alias="versionId"),
) -> dict[str, Any]:
    """读 Brief 视图（协议 §5 第 2 行）。

    **`versionId` 不传时返回当前生效版本**。传了但不存在/不属于本项目
    → 404（它是被寻址的资源，不是被引用的字段值 ——
    与 `PUT` 里 `baseVersionId` 报 422 的判据不同）。

    **`evidenceSegments` 只返回被引用的片段**：一份 200 页 PDF 有上万个片段，
    全量返回会让响应体失控，而"原文对照"需要的是**这条结论对应的那一段**。
    全文浏览需要一个协议里没有的端点，已登记为待定项。
    """
    access.require(ProjectAction.VIEW)
    brief = service.ensure_brief(brief_repo, project_id=access.project_id)
    # 先确定要展示哪一版，再按它的解析运行加载片段。
    resolved_version_id = version_id or brief.active_version_id
    return {
        "data": service.get_view(
            brief_repo,
            brief=brief,
            version_id=version_id,
            segments_by_id=_segments_by_id(
                parse_repo, brief_repo, version_id=resolved_version_id
            ),
        ),
        "meta": build_meta(),
    }


@router.put(
    "/projects/{projectId}/brief-draft",
    response_model=DataEnvelope[DraftUpdatedBody],
    responses=BRIEF_WRITE_RESPONSES,
    summary="更新 Brief 草稿（不覆盖已固化版本）",
)
def update_brief_draft(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    brief_repo: BriefRepoDep,
    body: DraftBody,
    request: Request,
) -> dict[str, Any]:
    """更新草稿（协议 §5 第 3 行）。

    **`If-Match` 必带**：草稿是可变资源，协议 §1.1 要求携带其当前 ETag。
    两层并发保证（前置校验 + `WHERE lock_version = ?`）都要有 ——
    只有前置校验会留下窗口，只有 WHERE 条件则无法在提交前给客户端
    一个明确的 412 与当前 ETag。

    **`draftId` 不在请求体里**：一个项目只有一个草稿
    （`UQ(brief_drafts.brief_id)`），因此它由路径里的项目唯一确定。
    让客户端传一个 id 只会多一条"这个 id 与项目不匹配"的失败路径。
    """
    access.require(ProjectAction.EDIT)
    brief = service.ensure_brief(brief_repo, project_id=access.project_id)
    draft = service.require_draft(brief_repo, brief=brief)

    # `require_if_match` 抛 428 / 400 / 412（协议 §1.1 的三种情形）。
    require_if_match(
        request,
        resource_id=draft.id,
        current_version=draft.lock_version,
        resource_label="Brief 草稿",
    )

    updated = service.update_draft(
        brief_repo,
        brief=brief,
        base_version_id=body.base_version_id,
        fields=body.fields,
        claims=body.claims,
        issue_resolutions=body.issue_resolutions,
        expected_lock_version=draft.lock_version,
        actor_id=user.id,
    )
    return {
        "data": {"draft": service.draft_view(updated)},
        "meta": build_meta(),
    }


@router.post(
    "/projects/{projectId}/brief-versions",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[SolidifiedVersionBody],
    responses=BRIEF_WRITE_RESPONSES,
    summary="固化 Brief 版本",
)
def create_brief_version(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    brief_repo: BriefRepoDep,
    body: VersionBody,
    request: Request,
) -> dict[str, Any]:
    """固化草稿为不可变版本（协议 §5 第 4 行，**201**）。

    **四道门**（见 `service.solidify` 的 docstring）：
    必填字段 → 无未答复核心疑问 → 内容真的变了 → 注册表与具体表同事务。

    **`If-Match` 校验的是草稿**：固化读的是草稿内容，
    因此"客户端看到的草稿"与"被固化的草稿"必须是同一个。
    用一个**已经固化过的**旧 ETag 重放，会在前置校验就报 412 ——
    而不是固化出一个内容不对的版本。
    """
    access.require(ProjectAction.EDIT)
    brief = service.ensure_brief(brief_repo, project_id=access.project_id)
    draft = service.require_draft(brief_repo, brief=brief)

    if body.draft_id != draft.id:
        # 一个项目只有一个草稿。传别的 id 说明客户端拿着过期页面 ——
        # 静默忽略它会让"我固化的是哪个草稿"变成一个谜。
        from rednote.platform.errors import ApiError, ErrorCode

        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "draftId 与本项目的当前草稿不一致，请重新读取后再提交。",
            details={
                "fieldErrors": [
                    {
                        "field": "draftId",
                        "type": "draft_id_mismatch",
                        "message": "draftId 不是本项目的当前草稿。",
                    }
                ],
                "currentDraftId": str(draft.id),
            },
        )

    require_if_match(
        request,
        resource_id=draft.id,
        current_version=draft.lock_version,
        resource_label="Brief 草稿",
    )

    solidified = service.solidify(brief_repo, brief=brief, actor_id=user.id)
    return {
        "data": {
            "versionId": str(solidified.version_id),
            "revisionNo": solidified.revision_no,
            "etag": solidified.etag,
            "productName": solidified.product_name,
            "summary": solidified.summary,
            "claimCount": solidified.claim_count,
            "issueCount": solidified.issue_count,
            "created": solidified.created,
        },
        "meta": build_meta(),
    }


@router.post(
    "/projects/{projectId}/brief-enrichment-jobs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DataEnvelope[JobAcceptedBody],
    responses=BRIEF_WRITE_RESPONSES,
    summary="发起 Brief 加厚（返回分类建议，不自动生效）",
)
def create_brief_enrichment_job(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    brief_repo: BriefRepoDep,
    job_repo: JobRepoDep,
    body: EnrichmentJobBody,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """发起加厚（协议 §5 第 5 行，**202**）。

    **建议不自动生效**：它落在 `brief_drafts.suggestions`，
    运营要采纳得在 `PUT /brief-draft` 里显式加进 `claims` ——
    那一步会走 `ClaimDraft` 的校验（事实类必须有来源）。
    于是"建议变成事实"永远要经过一次显式确认。

    **`allowedSourceIds` 非空**（否则 422）：空白名单意味着
    "允许引用零个来源"，而那只会让模型编造 —— 正是需求 §5.2 禁止的。
    """
    access.require(ProjectAction.EDIT)
    del user

    brief = service.ensure_brief(brief_repo, project_id=access.project_id)
    version, request_row = service.request_enrichment(
        brief_repo,
        brief=brief,
        brief_version_id=body.brief_version_id,
        allowed_source_ids=body.allowed_source_ids,
    )

    view = jobs_service.enqueue_job(
        job_repo,
        kind=KIND_BRIEF_ENRICH,
        queue=QUEUE_BRIEF,
        idempotency_scope=IDEMPOTENCY_SCOPE_BRIEF_ENRICH,
        idempotency_key=(
            f"{version.id}"
            if idempotency_key is None
            else f"{version.id}:{idempotency_key}"
        ),
        input_hash=service.enrichment_input_hash(request_row),
        project_id=access.project_id,
        input_version_ids=(version.id,),
    )
    response.status_code = status.HTTP_202_ACCEPTED
    return {
        "data": {
            "jobId": str(view.job_id),
            "kind": view.kind,
            "state": view.state,
            "created": view.created,
        },
        "meta": build_meta(),
    }


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _segments_by_id(
    parse_repo: ParseRepository, brief_repo: BriefRepository, *, version_id: uuid.UUID | None
) -> dict[uuid.UUID, Any]:
    """构造 `{segment.id: segment}` —— **只加载本版本那次解析的片段**。

    **为什么按 run 整批加载，而不是逐条按 id 查**：
    `ParseRepository` 没有"按 segment_id 反查"的入口（它的公开接口是
    `list_segments(parse_run_id)`），而本模块**不得改它的签名**
    （接缝冻结 §5.2）。逐条查需要拿到 `source_segments` 的模型直接查库，
    那会让本模块依赖 parsing 的表结构 —— 比"多加载一些片段"更坏的耦合。

    **加载范围是"这个版本那次解析的全部片段"**（几十到几万条），
    而 `service.get_view` 只对**被引用到的**做输出。
    多加载的代价是一次查询，收益是不碰别人的表结构。

    `version_id` 为 `None` 时返回空 dict：那时没有版本，
    也就没有 claims 需要对照原文。
    """
    if version_id is None:
        return {}
    version = brief_repo.get_version(version_id)
    if version is None or version.source_parse_run_id is None:
        return {}
    return {
        segment.id: segment
        for segment in parse_repo.list_segments(version.source_parse_run_id)
    }


__all__ = [
    "BRIEF_READ_RESPONSES",
    "BRIEF_WRITE_RESPONSES",
    "BriefViewBody",
    "DraftBody",
    "DraftUpdatedBody",
    "EnrichmentJobBody",
    "JobAcceptedBody",
    "ParseJobBody",
    "SolidifiedVersionBody",
    "VersionBody",
    "router",
]
