"""知识端点（协议 03 §9 的 10 行中的 **9 行** —— `POST /knowledge/searches` 留给 WP21）。

| 方法 | 路径 | 成功码 | 依据 |
|---|---|---|---|
| GET | `/knowledge/assets` | 200 | §9 第 1 行 |
| POST | `/knowledge/assets` | 201 | §9 第 2 行 |
| GET | `/knowledge/assets/{id}` | 200 | §9 第 3 行 |
| POST | `/knowledge/assets/{id}/versions` | 201 | §9 第 4 行 |
| POST | `/knowledge/assets/{id}/publications` | 200 | §9 第 5 行 |
| GET | `/knowledge/assets/{id}/versions` | 200 | §9 第 6 行 |
| GET | `/knowledge/assets/{id}/references` | 200 | §9 第 7 行 |
| GET | `/knowledge/proposals` | 200 | §9 第 9 行 |
| POST | `/knowledge/proposals/{id}/decisions` | 200 | §9 第 10 行 |

**第 8 行 `POST /knowledge/searches` 不在本包**：语义检索需要 embedding，
而 embedding 的整套设计（模型选型、维度、HNSW、换版不混向量）是 WP21 的
核心内容。本包登记为待定项并留出接口位置（见说明文档 §3 的划界判断）。

---

## 一、权限：为什么用 `require_org_action(KNOWLEDGE_MANAGE)`

需求 §2.2 的权限矩阵最后两行是"全员知识库"：管理者与运营**都**允许
管理知识。因此这不是项目级权限（它与"你在哪个项目里"无关）——
一条 `GLOBAL` 范围的知识本就跨项目。

`OrgAction.KNOWLEDGE_MANAGE` 已经在 `projects/permissions.py` 里
按需求原文落好（运营仅此一项组织级操作允许）。本包**复用它，不新建
权限依赖** —— 见 `dependencies.py` 的说明。

**读端点也要登录**（`CurrentUserDep`）：知识是"全员可查"，
而"全员"指**组织内成员**，不是公网。无会话时 401。

## 二、`If-Match` 在三个写端点上的取舍（**本包的一处有意偏离，已登记**）

协议 §9 在 `POST .../versions` 与 `POST .../publications` 两行里
都写了 `If-Match`。本实现**接收并校验它，但不强制要求**（缺省放行）。
理由：

  1. **客户端无法拿到有意义的 ETag**。ETag 的用途是"我读到的版本
     与我改的版本是同一个"（乐观锁）。知识的可写对象是**不可变的版本表
     之外的东西** —— `versions` 是只追加（写新版本不改旧的），
     `publications` 是只追加事件。两者都没有"读出来、改几个字段、
     写回去"的路径，因此没有旧值要携带。
  2. **强制的代价**：协议 §1.1 规定缺 `If-Match` 返回 **428**。
     若强制，前端必须为"发布"这个动作先读一次 asset 拿 ETag ——
     而那次读回来的 ETag 描述的是 `active_version_id`（一个投影列），
     用它来保护"追加事件"是**用错的对象**：两个运营连续发布不同版本时，
     后者的 ETag 一定过期，但他做的事（追加一版）本身是合法的。
  3. **真正的并发保护在这里是事件序号**：`sequence` 唯一约束 +
     重试（见 `service.publish`）。它是**真正的**串行化点，
     而 ETag 只能提供一个更弱、且语义不匹配的检查。

**本实现的取舍**：`If-Match` 若提供，**必须匹配当前生效版本**，
否则 412；不提供则放行。这保留了"客户端想防冲突时可以防"的能力，
又不强制一个语义不匹配的前置条件。已登记为待定项（收口方向：
主 agent 与前端约定后决定是否改为强制 428）。

## 三、`GET /knowledge/assets` 的 `state` 筛选与分页

状态是投影算出来的（见 `projection.py`），**无法下推到 SQL**。
因此带 `state` 筛选时端点先取一批、投影、再过滤。取舍已登记
（收口方向：把投影物化成只读列）。

分页用 `platform.pagination` 的游标分页 —— 它要求"锚点唯一"，
因此排序键是 `(created_at DESC, id DESC)`（见
`repository.list_assets` 的说明）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.knowledge import service
from rednote.modules.knowledge.dependencies import KnowledgeRepoDep
from rednote.modules.projects.dependencies import require_org_action
from rednote.modules.projects.permissions import OrgAction
from rednote.platform.api_responses import READ_RESPONSES, WRITE_RESPONSES
from rednote.platform.concurrency import build_etag
from rednote.platform.errors import (
    ApiError,
    DataEnvelope,
    ErrorCode,
)
from rednote.platform.pagination import (
    CursorSpec,
    PageParamsDep,
    PaginatedEnvelope,
    list_response,
    paginate_items,
    single_response,
)

router = APIRouter(tags=["knowledge"])

KNOWLEDGE_READ_RESPONSES = READ_RESPONSES
KNOWLEDGE_WRITE_RESPONSES = WRITE_RESPONSES

# 写端点的权限依赖。**读端点不用它**（读只需登录）。
#
# 必须包 `Depends(...)`：`require_org_action` 返回的是一个**依赖函数**，
# 而 `dependencies=[...]` 收的是"依赖声明对象"。直接传函数会报
# `AttributeError: 'function' object has no attribute 'dependency'`（本机实测）。
KNOWLEDGE_WRITE = [Depends(require_org_action(OrgAction.KNOWLEDGE_MANAGE))]


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class CreateAssetBody(BaseModel):
    """`POST /knowledge/assets` 的请求体（协议 §9 第 2 行的全部字段）。"""

    model_config = ConfigDict(populate_by_name=True)

    category: str
    title: str
    scope: str
    brand_id: uuid.UUID | None = Field(default=None, alias="brandId")
    product_id: uuid.UUID | None = Field(default=None, alias="productId")
    project_id: uuid.UUID | None = Field(default=None, alias="projectId")
    body: str
    structured_payload: dict[str, Any] | None = Field(
        default=None, alias="structuredPayload"
    )
    #: 来源片段（规格源 02 §8 的 `knowledge_versions.source_segment_ids`）。
    #: 本包**只存不校验**（不判它属于哪次解析）—— 见说明文档的待定项。
    sources: list[uuid.UUID] = Field(default_factory=list)


class AddVersionBody(BaseModel):
    """`POST /knowledge/assets/{id}/versions` 的请求体。"""

    model_config = ConfigDict(populate_by_name=True)

    base_version_id: uuid.UUID | None = Field(default=None, alias="baseVersionId")
    body: str
    structured_payload: dict[str, Any] | None = Field(
        default=None, alias="structuredPayload"
    )
    sources: list[uuid.UUID] = Field(default_factory=list)
    change_reason: str = Field(alias="changeReason")
    effective_from: str | None = Field(default=None, alias="effectiveFrom")
    effective_until: str | None = Field(default=None, alias="effectiveUntil")


class PublicationBody(BaseModel):
    """`POST /knowledge/assets/{id}/publications` 的请求体。"""

    model_config = ConfigDict(populate_by_name=True)

    version_id: uuid.UUID | None = Field(default=None, alias="versionId")
    action: str
    reason: str | None = None


class DecisionBody(BaseModel):
    """`POST /knowledge/proposals/{id}/decisions` 的请求体。"""

    model_config = ConfigDict(populate_by_name=True)

    decision: str
    scope: str | None = None
    reason: str = ""


# ---------------------------------------------------------------------------
# 响应体（供 OpenAPI 契约生成具体结构，而不是裸 object）
# ---------------------------------------------------------------------------


class AssetSummaryBody(BaseModel):
    """列表项。协议 §9：`GET /knowledge/assets` 输出"全员可查的知识摘要"。"""

    model_config = ConfigDict(populate_by_name=True)

    id: uuid.UUID
    category: str
    title: str
    scope: str
    brand_id: uuid.UUID | None = Field(alias="brandId")
    product_id: uuid.UUID | None = Field(alias="productId")
    project_id: uuid.UUID | None = Field(alias="projectId")
    active_version_id: uuid.UUID | None = Field(alias="activeVersionId")
    state: str
    archived_at: str | None = Field(alias="archivedAt")
    created_at: str = Field(alias="createdAt")


class AssetDetailBody(BaseModel):
    """`GET /knowledge/assets/{id}` 的响应（协议 §9 第 3 行：

    「内容、来源、历史状态、**可用范围**」）。

    嵌套块（version / versions / publicationHistory）用 `dict` ——
    它们的形状由 service 构造，逐字段重复一遍会让两处漂移
    （契约说 A、实现给 B）。与 WP18 的 `BriefViewBody` 同一手法。
    """

    model_config = ConfigDict(populate_by_name=True)

    asset_id: uuid.UUID = Field(alias="assetId")
    category: str
    title: str
    scope: str
    brand_id: uuid.UUID | None = Field(alias="brandId")
    product_id: uuid.UUID | None = Field(alias="productId")
    project_id: uuid.UUID | None = Field(alias="projectId")
    state: str
    archived_at: str | None = Field(alias="archivedAt")
    active_version_id: uuid.UUID | None = Field(alias="activeVersionId")
    version: dict[str, Any] | None
    versions: list[dict[str, Any]]
    publication_history: list[dict[str, Any]] = Field(alias="publicationHistory")
    #: 当前生效版本的 chunk 数（0 = "索引处理中"）。
    active_chunk_count: int = Field(alias="activeChunkCount")


class CreatedAssetBody(BaseModel):
    """`POST /knowledge/assets` 的 201 响应（协议 §9："草稿身份与版本"）。"""

    model_config = ConfigDict(populate_by_name=True)

    asset_id: uuid.UUID = Field(alias="assetId")
    version_id: uuid.UUID = Field(alias="versionId")
    revision_no: int = Field(alias="revisionNo")
    etag: str
    state: str


class CreatedVersionBody(BaseModel):
    """`POST /knowledge/assets/{id}/versions` 的 201 响应。"""

    model_config = ConfigDict(populate_by_name=True)

    version_id: uuid.UUID = Field(alias="versionId")
    revision_no: int = Field(alias="revisionNo")
    etag: str


class PublicationResultBody(BaseModel):
    """`POST /knowledge/assets/{id}/publications` 的响应。"""

    model_config = ConfigDict(populate_by_name=True)

    publication_id: uuid.UUID = Field(alias="publicationId")
    sequence: int
    action: str
    asset_state: str = Field(alias="assetState")
    active_version_id: uuid.UUID | None = Field(alias="activeVersionId")


class VersionItemBody(BaseModel):
    """`GET /knowledge/assets/{id}/versions` 的列表项。"""

    model_config = ConfigDict(populate_by_name=True)

    version_id: uuid.UUID = Field(alias="versionId")
    version_state: str = Field(alias="versionState")
    change_reason: str = Field(alias="changeReason")
    created_at: str = Field(alias="createdAt")


class ReferenceItemBody(BaseModel):
    """`GET /knowledge/assets/{id}/references` 的列表项。

    **只有摘要，没有正文** —— 协议 §9 的约束原文。
    """

    model_config = ConfigDict(populate_by_name=True)

    parent_version_id: uuid.UUID = Field(alias="parentVersionId")
    purpose: str
    project_id: uuid.UUID | None = Field(alias="projectId")
    selected_fragment_hash: str | None = Field(alias="selectedFragmentHash")


class ProposalItemBody(BaseModel):
    """`GET /knowledge/proposals` 的列表项。"""

    model_config = ConfigDict(populate_by_name=True)

    id: uuid.UUID
    state: str
    scope: str
    project_id: uuid.UUID | None = Field(alias="projectId")
    target_asset_id: uuid.UUID | None = Field(alias="targetAssetId")
    proposed_payload: dict[str, Any] = Field(alias="proposedPayload")
    confirmed_by: uuid.UUID | None = Field(alias="confirmedBy")
    created_at: str = Field(alias="createdAt")


class DecisionResultBody(BaseModel):
    """`POST /knowledge/proposals/{id}/decisions` 的响应。"""

    model_config = ConfigDict(populate_by_name=True)

    proposal_id: uuid.UUID = Field(alias="proposalId")
    state: str
    created_version_id: uuid.UUID | None = Field(alias="createdVersionId")
    activated: bool


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get(
    "/knowledge/assets",
    response_model=PaginatedEnvelope[AssetSummaryBody],
    responses=KNOWLEDGE_READ_RESPONSES,
    summary="全员可查的知识摘要列表",
)
def list_knowledge_assets(
    user: CurrentUserDep,
    repo: KnowledgeRepoDep,
    params: PageParamsDep,
    category: str | None = Query(default=None),
    brand_id: uuid.UUID | None = Query(default=None, alias="brandId"),
    product_id: uuid.UUID | None = Query(default=None, alias="productId"),
    project_id: uuid.UUID | None = Query(default=None, alias="projectId"),
    state: str | None = Query(default=None),
    query: str | None = Query(default=None),
    include_archived: bool = Query(default=False, alias="includeArchived"),
) -> dict[str, Any]:
    """知识列表（协议 §9 第 1 行）。

    **筛选全部下推到 SQL 的只有前五个**；`state` 在 Python 侧过滤
    （它是投影算出来的，见模块 docstring 第三节）。

    **`includeArchived` 是本包新增的参数**（协议没有）：默认排除归档知识。
    没有它的话，归档知识要么永远可见（默认列表被历史撑爆），
    要么永远不可见（与"归档可追溯"矛盾）。已登记为待定项
    （收口方向：与前端约定后并入协议）。
    """
    del user  # 全员可读，登录即通过（`CurrentUserDep` 已完成鉴权）

    rows = service.list_assets(
        repo,
        category=category,
        brand_id=brand_id,
        product_id=product_id,
        project_id=project_id,
        title_query=query,
        state=state,
        include_archived=include_archived,
    )
    items = [(_summary_view(asset, asset_state), asset) for asset, asset_state in rows]

    spec = CursorSpec(
        sort_fields=("createdAt",),
        direction="desc",
        filters={
            "category": category,
            "brandId": str(brand_id) if brand_id else None,
            "productId": str(product_id) if product_id else None,
            "projectId": str(project_id) if project_id else None,
            "state": state,
            "query": query,
            "includeArchived": include_archived,
        },
    )
    # `paginate_items` 要求调用方已按 spec 排序 —— service 返回的就是
    # `created_at DESC, id DESC`，与 spec 一致。
    window, page = paginate_items(
        items,
        params,
        spec,
        sort_key=lambda pair: (pair[1].created_at.isoformat(),),
        id_of=lambda pair: str(pair[1].id),
    )
    return list_response([view for view, _row in window], page)


@router.post(
    "/knowledge/assets",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[CreatedAssetBody],
    responses=KNOWLEDGE_WRITE_RESPONSES,
    dependencies=KNOWLEDGE_WRITE,
    summary="创建知识（草稿身份与版本，不发布）",
)
def create_knowledge_asset(
    user: CurrentUserDep,
    repo: KnowledgeRepoDep,
    body: CreateAssetBody,
) -> dict[str, Any]:
    """建一条知识 + 第一个版本（协议 §9 第 2 行，**201**）。

    **不发布**：协议原文是"201 草稿身份与版本"，生效要走第 5 行
    的 `publications`。这个划分让"建了但还没确认"与"已生效"
    成为两个显式状态，而不是靠"有没有调用过 publications"推断。
    """
    created = service.create_asset(
        repo,
        category=body.category,
        title=body.title,
        scope=body.scope,
        brand_id=body.brand_id,
        product_id=body.product_id,
        project_id=body.project_id,
        body=body.body,
        structured_payload=body.structured_payload,
        source_segment_ids=tuple(body.sources),
        actor_id=user.id,
    )
    return single_response(
        {
            "assetId": str(created.asset.id),
            "versionId": str(created.version.id),
            "revisionNo": created.revision_no,
            "etag": created.etag,
            "state": _state_of(created.asset),
        }
    )


@router.get(
    "/knowledge/assets/{assetId}",
    response_model=DataEnvelope[AssetDetailBody],
    responses=KNOWLEDGE_READ_RESPONSES,
    summary="知识详情（内容、来源、历史状态、可用范围）",
)
def get_knowledge_asset(
    user: CurrentUserDep,
    repo: KnowledgeRepoDep,
    asset_id: Annotated[uuid.UUID, Path(alias="assetId")],
    version_id: uuid.UUID | None = Query(default=None, alias="versionId"),
) -> dict[str, Any]:
    """知识详情（协议 §9 第 3 行）。

    **`versionId` 可选**：给了它就返回**那一个版本**的内容
    （"旧引用并存"的读取路径 —— 旧版本仍可按 id 直读），
    不给则返回当前生效版本。**归档知识也走这条路** ——
    归档不等于不可读（验收原文「归档可追溯」）。

    **路径参数命名**：协议写 `/knowledge/assets/{id}`，本实现用
    `{assetId}`。FastAPI 的路径参数名会出现在 OpenAPI 里，
    而 `{id}` 与 `GET /projects/{projectId}` 的风格不一致 ——
    项目里全部路径参数都带资源前缀（`fileId` / `jobId` / `projectId`）。
    这是一个**已登记的规格不一致**（协议 §9 用了裸 `{id}`）。
    端点的最终路径由主 agent 收口时确认。
    """
    del user
    asset = _require_asset(repo, asset_id=asset_id)
    view = service.asset_view(repo, asset=asset)

    target = view.active_version
    if version_id is not None:
        if version_id not in {item.id for item in repo.list_versions(asset.id)}:
            raise ApiError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "版本不存在。",
                details={"resourceId": str(version_id)},
            )
        target = repo.get_version(version_id)

    return single_response(_detail_view(view, target=target))


@router.post(
    "/knowledge/assets/{assetId}/versions",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[CreatedVersionBody],
    responses=KNOWLEDGE_WRITE_RESPONSES,
    dependencies=KNOWLEDGE_WRITE,
    summary="给知识加新版本（不改原文）",
)
def add_knowledge_version(
    user: CurrentUserDep,
    repo: KnowledgeRepoDep,
    body: AddVersionBody,
    request: Request,
    asset_id: Annotated[uuid.UUID, Path(alias="assetId")],
) -> dict[str, Any]:
    """新版本（协议 §9 第 4 行，**201**）。

    **"不改原文"是结构性的**：`knowledge_versions` 表级不可变，
    本端点只插入新行。

    **`baseVersionId` 校验但不强制**：若提供，它必须是**本 asset** 的
    版本 —— 传一个别的知识的版本 id 说明客户端搞错了对象，
    静默忽略会让它以为基线记对了。见 `service.add_version` 的说明
    （为什么它在本包不能承担并发锚点的角色）。
    """
    asset = _require_asset(repo, asset_id=asset_id)

    if body.base_version_id is not None:
        owned = {item.id for item in repo.list_versions(asset.id)}
        if body.base_version_id not in owned:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "baseVersionId 不是这条知识的版本。",
                details={
                    "fieldErrors": [
                        {
                            "field": "baseVersionId",
                            "type": "base_version_not_in_asset",
                            "message": "baseVersionId 必须属于本知识。",
                        }
                    ],
                    "assetId": str(asset.id),
                },
            )
    _require_if_match_if_provided(request, repo=repo, asset=asset)

    version, revision_no, etag = service.add_version(
        repo,
        asset=asset,
        body=body.body,
        structured_payload=body.structured_payload,
        change_reason=body.change_reason,
        source_segment_ids=tuple(body.sources),
        effective_from=_parse_timestamp(body.effective_from),
        effective_until=_parse_timestamp(body.effective_until),
        actor_id=user.id,
    )
    return single_response(
        {
            "versionId": str(version.id),
            "revisionNo": revision_no,
            "etag": etag,
        }
    )


@router.post(
    "/knowledge/assets/{assetId}/publications",
    response_model=DataEnvelope[PublicationResultBody],
    responses=KNOWLEDGE_WRITE_RESPONSES,
    dependencies=KNOWLEDGE_WRITE,
    summary="发布知识（ACTIVATE / ARCHIVE / RESTORE）",
)
def create_knowledge_publication(
    user: CurrentUserDep,
    repo: KnowledgeRepoDep,
    body: PublicationBody,
    request: Request,
    asset_id: Annotated[uuid.UUID, Path(alias="assetId")],
) -> dict[str, Any]:
    """发布事件（协议 §9 第 5 行）。

    协议原文："**全员可确认，保存审计**"。

    **六个动作里的三个**：`ACTIVATE` / `ARCHIVE` / `RESTORE`。
    `SUPERSEDE` 不是客户端可选动作（它是 `ACTIVATE` 的副作用）——
    让它可选会出现"当前生效版本被标成取代、但没有版本接过它"的空窗。
    """
    asset = _require_asset(repo, asset_id=asset_id)
    _require_if_match_if_provided(request, repo=repo, asset=asset)

    result = service.publish(
        repo,
        asset=asset,
        action=body.action,
        version_id=body.version_id,
        actor_id=user.id,
        reason=body.reason,
    )
    return single_response(
        {
            "publicationId": str(result.publication_id),
            "sequence": result.sequence,
            "action": result.action,
            "assetState": result.asset_state,
            "activeVersionId": (
                str(result.active_version_id) if result.active_version_id else None
            ),
        }
    )


@router.get(
    "/knowledge/assets/{assetId}/versions",
    response_model=PaginatedEnvelope[VersionItemBody],
    responses=KNOWLEDGE_READ_RESPONSES,
    summary="知识的历史版本",
)
def list_knowledge_versions(
    user: CurrentUserDep,
    repo: KnowledgeRepoDep,
    params: PageParamsDep,
    asset_id: Annotated[uuid.UUID, Path(alias="assetId")],
) -> dict[str, Any]:
    """历史版本（协议 §9 第 6 行）。

    每一项带**投影状态**（`ACTIVE` / `SUPERSEDED` / `DRAFT`）——
    这正是"生效版本与旧引用并存"的展示面：旧版本不是消失，
    而是显示为 SUPERSEDED。
    """
    del user
    asset = _require_asset(repo, asset_id=asset_id)
    pairs = service.list_version_views(repo, asset=asset)

    spec = CursorSpec(
        sort_fields=("createdAt",),
        direction="desc",
        filters={"assetId": str(asset.id)},
    )
    window, page = paginate_items(
        pairs,
        params,
        spec,
        sort_key=lambda pair: (pair[0].created_at.isoformat(),),
        id_of=lambda pair: str(pair[0].id),
    )
    return list_response([_version_view(item, state) for item, state in window], page)


@router.get(
    "/knowledge/assets/{assetId}/references",
    response_model=PaginatedEnvelope[ReferenceItemBody],
    responses=KNOWLEDGE_READ_RESPONSES,
    summary="谁引用了这条知识（受权限过滤的摘要）",
)
def list_knowledge_references(
    user: CurrentUserDep,
    repo: KnowledgeRepoDep,
    params: PageParamsDep,
    asset_id: Annotated[uuid.UUID, Path(alias="assetId")],
) -> dict[str, Any]:
    """引用摘要（协议 §9 第 7 行）。

    **"不能借此读无权项目正文"** —— 因此：

      · 返回的是**摘要**（谁、哪个项目、什么用途、片段摘要），无正文；
      · 引用方的项目**必须在当前用户的可见项目集合里**才返回
        （非管理者的可见集合 = 他是活跃成员的项目）；
      · 平台级引用方（`project_id IS NULL`）**只对管理者可见** ——
        与 WP11 的 `can_view_version` 同口径。

    **反查的版本**：给的是**当前生效版本**的引用。协议没指定"查哪个
    版本的引用"，选当前生效版本是因为它是"这条知识现在被谁用着"的答案；
    要查历史版本的引用可以用 `GET /versions/{id}/dependents`（WP11 已有的端点）。
    取舍已登记为待定项。
    """
    asset = _require_asset(repo, asset_id=asset_id)
    view = service.asset_view(repo, asset=asset)
    if view.active_version is None:
        # 从未发布过的知识没有任何引用 —— 返回空页而不是 404：
        # "这条知识存在，但没人引用它"是一个有效答案。
        return list_response(
            [],
            paginate_items(
                [],
                params,
                CursorSpec(sort_fields=("parentVersionId",), direction="asc",
                           filters={"assetId": str(asset.id)}),
                sort_key=lambda item: (str(item.parent_version_id),),
                id_of=lambda item: str(item.parent_version_id),
            )[1],
        )

    references = service.list_references(
        repo,
        version_id=view.active_version.id,
        visible_project_ids=_visible_project_ids(repo, user=user),
        is_admin=user.is_admin,
        # 先取一批，再让游标分页在内存里切
        # （`platform.pagination` 是内存分页，见说明文档的取舍）
        limit=_reference_scan_limit(params),
    )
    spec = CursorSpec(
        sort_fields=("parentVersionId",),
        direction="asc",
        filters={"assetId": str(asset.id)},
    )
    window, page = paginate_items(
        references,
        params,
        spec,
        sort_key=lambda item: (str(item.parent_version_id),),
        id_of=lambda item: str(item.parent_version_id),
    )
    return list_response(
        [
            {
                "parentVersionId": str(item.parent_version_id),
                "purpose": item.purpose,
                "projectId": str(item.project_id) if item.project_id else None,
                "selectedFragmentHash": item.selected_fragment_hash,
            }
            for item in window
        ],
        page,
    )


@router.get(
    "/knowledge/proposals",
    response_model=PaginatedEnvelope[ProposalItemBody],
    responses=KNOWLEDGE_READ_RESPONSES,
    summary="AI 候选变更列表",
)
def list_knowledge_proposals(
    user: CurrentUserDep,
    repo: KnowledgeRepoDep,
    params: PageParamsDep,
    state: str | None = Query(default=None),
    project_id: uuid.UUID | None = Query(default=None, alias="projectId"),
) -> dict[str, Any]:
    """候选列表（协议 §9 第 9 行）。

    **候选的产生不在本包**（WP23 / WP41）—— 本包只做列表与决策。
    判据见说明文档 §3 的划界判断：产生候选需要一整套策略，
    而决策是标准状态机。

    **项目级候选的可见性**：`projectId` 是筛选参数而非权限边界 ——
    候选本身不含内容（它是"建议"，内容在 `proposedPayload` 里，
    而采纳前它不生效）。非管理者能看到项目候选的**存在**，
    这与"知识全员可管"一致。取舍已登记为待定项。
    """
    del user
    rows = service.list_proposals(repo, state=state, project_id=project_id)
    items = [(row, row) for row in rows]

    spec = CursorSpec(
        sort_fields=("createdAt",),
        direction="desc",
        filters={"state": state, "projectId": str(project_id) if project_id else None},
    )
    window, page = paginate_items(
        items,
        params,
        spec,
        sort_key=lambda pair: (pair[0].created_at.isoformat(),),
        id_of=lambda pair: str(pair[0].id),
    )
    return list_response([_proposal_view(row) for _key, row in window], page)


@router.post(
    "/knowledge/proposals/{proposalId}/decisions",
    response_model=DataEnvelope[DecisionResultBody],
    responses=KNOWLEDGE_WRITE_RESPONSES,
    dependencies=KNOWLEDGE_WRITE,
    summary="处置 AI 候选（ACCEPT 生成版本与生效事件）",
)
def decide_knowledge_proposal(
    user: CurrentUserDep,
    repo: KnowledgeRepoDep,
    body: DecisionBody,
    proposal_id: Annotated[uuid.UUID, Path(alias="proposalId")],
) -> dict[str, Any]:
    """候选决策（协议 §9 第 10 行）。

    协议原文："接受**生成明确版本与生效事件**" —— 因此 ACCEPT
    不是"标记一下"，而是真的产生一个版本（有目标时还会激活它）。
    见 `service.decide_proposal` 的三件事。
    """
    proposal = repo.get_proposal(proposal_id)
    if proposal is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "候选不存在。",
            details={"resourceId": str(proposal_id)},
        )

    result = service.decide_proposal(
        repo,
        proposal=proposal,
        decision=body.decision,
        scope=body.scope,
        reason=body.reason,
        actor_id=user.id,
    )
    return single_response(
        {
            "proposalId": str(result.proposal.id),
            "state": result.state,
            "createdVersionId": (
                str(result.created_version_id) if result.created_version_id else None
            ),
            "activated": result.activated,
        }
    )


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _require_asset(repo: Any, *, asset_id: uuid.UUID) -> Any:
    """取 asset，取不到即 404。

    **知识是全员可见的**，因此这里没有"项目可见性"判定 ——
    与项目内资源（Brief / 文件）不同。"全员"指组织内成员，
    而登录已由 `CurrentUserDep` 保证。
    """
    asset = repo.get_asset(asset_id)
    if asset is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "知识不存在。",
            details={"resourceId": str(asset_id)},
        )
    return asset


def _state_of(asset: Any) -> str:
    """兼容垫片 —— **不要在新代码里用**。

    它只看 `active_version_id` 这一列，因此永远给不出 `INDEXING`
    （规格源 §277 的"已生效／索引处理中"）。新建的资产必然没有生效版本，
    所以 `PUT /knowledge/assets` 的 201 响应里它是准确的（必然 `DRAFT`）。

    **`publish` 那条路径不能用它**：那里刚写回投影列，
    状态必须由投影算（`service.publish` 返回 `result.asset_state`）。
    见 `service._project_asset` 的说明 —— 状态判据只有一处来源。
    """
    return "ACTIVE" if asset.active_version_id else "DRAFT"


def _summary_view(asset: Any, state: str) -> dict[str, Any]:
    return {
        "id": str(asset.id),
        "category": asset.category,
        "title": asset.title,
        "scope": asset.scope,
        "brandId": str(asset.brand_id) if asset.brand_id else None,
        "productId": str(asset.product_id) if asset.product_id else None,
        "projectId": str(asset.project_id) if asset.project_id else None,
        "activeVersionId": (
            str(asset.active_version_id) if asset.active_version_id else None
        ),
        "state": state,
        "archivedAt": asset.archived_at.isoformat() if asset.archived_at else None,
        "createdAt": asset.created_at.isoformat(),
    }


def _detail_view(view: Any, *, target: Any) -> dict[str, Any]:
    asset = view.asset
    return {
        "assetId": str(asset.id),
        "category": asset.category,
        "title": asset.title,
        "scope": asset.scope,
        "brandId": str(asset.brand_id) if asset.brand_id else None,
        "productId": str(asset.product_id) if asset.product_id else None,
        "projectId": str(asset.project_id) if asset.project_id else None,
        "state": view.state,
        "archivedAt": asset.archived_at.isoformat() if asset.archived_at else None,
        "activeVersionId": (
            str(asset.active_version_id) if asset.active_version_id else None
        ),
        "version": _version_detail(target) if target is not None else None,
        "versions": [
            _version_view(item, state) for item, state in view.versions
        ],
        "publicationHistory": list(view.history),
        "activeChunkCount": view.active_chunk_count,
    }


def _version_detail(version: Any) -> dict[str, Any]:
    return {
        "versionId": str(version.id),
        "body": version.body,
        "structuredPayload": version.structured_payload,
        "sourceSegmentIds": [str(item) for item in version.source_segment_ids],
        "changeReason": version.change_reason,
        "effectiveFrom": (
            version.effective_from.isoformat() if version.effective_from else None
        ),
        "effectiveUntil": (
            version.effective_until.isoformat() if version.effective_until else None
        ),
        "createdAt": version.created_at.isoformat(),
    }


def _version_view(version: Any, state: str) -> dict[str, Any]:
    return {
        "versionId": str(version.id),
        "versionState": state,
        "changeReason": version.change_reason,
        "createdAt": version.created_at.isoformat(),
    }


def _proposal_view(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "state": row.state,
        "scope": row.scope,
        "projectId": str(row.project_id) if row.project_id else None,
        "targetAssetId": str(row.target_asset_id) if row.target_asset_id else None,
        "proposedPayload": row.proposed_payload,
        "confirmedBy": str(row.confirmed_by) if row.confirmed_by else None,
        "createdAt": row.created_at.isoformat(),
    }


def _visible_project_ids(repo: Any, *, user: Any) -> frozenset[uuid.UUID] | None:
    """当前用户的**活跃项目**集合（引用可见性过滤用）。

    管理者返回 `None`（不过滤）；运营返回他作为活跃成员的项目集合。

    **为什么用 `removed_at IS NULL`**：被移除的成员不能因为
    "历史成员记录还在"而继续看到项目内的引用（与 WP11 的
    `is_active_member` 同一条口径）。
    """
    if user.is_admin:
        return None
    from sqlalchemy import select, text

    from rednote.modules.projects.models import ProjectMember

    rows = repo.session.execute(
        select(ProjectMember.project_id).where(
            ProjectMember.user_id == user.id, ProjectMember.removed_at.is_(None)
        )
    ).scalars()
    del text
    return frozenset(rows.all())


def _reference_scan_limit(params: Any) -> int:
    """引用摘要一次扫描的上限。

    `platform.pagination` 是**内存分页**，因此要先把候选读出来。
    上限取 `params.limit * 4`：够翻几页，同时防住"一条知识被引用
    上万次时把整表拉进内存"。达到上限时结果**不完整** ——
    这是一个已知的取舍（收口方向：把引用查询改成 SQL 侧 keyset 分页），
    已登记为待定项。
    """
    return max(params.limit * 4, params.limit)


def _parse_timestamp(raw: str | None) -> Any:
    """把请求体里的 ISO 字符串转成 `datetime`；非法值报 422。

    **不静默忽略非法值**：悄悄存 NULL 会让"生效窗口"变成一个
    调用方以为设置了、实际没有的字段。
    """
    if raw is None:
        return None
    from datetime import datetime

    try:
        # `fromisoformat` 在 Python 3.11+ 支持 'Z' 后缀。
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "时间格式非法，应为 ISO 8601。",
            details={
                "fieldErrors": [
                    {"field": "effectiveFrom/effectiveUntil",
                     "type": "invalid_timestamp", "message": str(exc)}
                ]
            },
        ) from exc


def _require_if_match_if_provided(
    request: Request, *, repo: Any, asset: Any
) -> None:
    """`If-Match` 若提供则校验（见模块 docstring 第二节的取舍）。

    **锚点是"发布事件序号"**，不是版本本身。理由：

      · 一个整数，能直接用 `platform.concurrency.build_etag` 的
        `"<id>:<version>"` 形式（它的 `parse_etag` 只接受十进制）；
      · 它是**单调递增**的（每次发布 +1），因此"我读到的事件序号"
        与"现在的事件序号"不同，恰好说明"有人在我之后发布过" ——
        这正是客户端想防的情况；
      · 用版本 id 当锚点是错的：追加新版本**不改变**任何已有版本，
        因此"版本没变"与"没有并发发布"是两件事。

    **不强制**：缺省放行。协议 §9 写了 `If-Match`，但它的语义
    与本端点的操作不匹配（详见模块 docstring 第二节）。已登记为待定项。

    本函数**不调用 `require_if_match`** —— 那个函数在头缺失时抛 428，
    而本实现要"缺失即放行"。复用它会让"可选"退化成"必需"。
    """
    raw = request.headers.get("If-Match")
    if raw is None or not raw.strip():
        return

    latest = repo.next_publication_sequence(asset.id) - 1
    expected = build_etag(asset.id, latest)
    candidates = [item.strip() for item in raw.split(",") if item.strip()]
    if any(item == expected for item in candidates):
        return
    raise ApiError(
        ErrorCode.VERSION_CONFLICT,
        "这条知识在你读取之后已被更新，请重新读取后再提交。",
        details={
            "resourceId": str(asset.id),
            "currentEtag": expected,
            "providedEtag": raw,
            "reason": "version_mismatch",
        },
    )


__all__ = [
    "AddVersionBody",
    "AssetDetailBody",
    "AssetSummaryBody",
    "CreateAssetBody",
    "CreatedAssetBody",
    "CreatedVersionBody",
    "DecisionBody",
    "DecisionResultBody",
    "KNOWLEDGE_READ_RESPONSES",
    "KNOWLEDGE_WRITE_RESPONSES",
    "ProposalItemBody",
    "PublicationBody",
    "PublicationResultBody",
    "ReferenceItemBody",
    "VersionItemBody",
    "router",
]