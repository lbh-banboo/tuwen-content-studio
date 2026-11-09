"""检索端点（协议 §9 第 8 行 `POST /knowledge/searches`）。

| 方法 | 路径 | 成功码 | 依据 |
|---|---|---|---|
| POST | `/knowledge/searches` | 200 | §9 第 8 行 |

**本仓唯一未实现的 §9 端点** —— WP20 做了 10 行里的 9 行，
把第 8 行留给本包（`docs/m2/WP20-侦察交接.md` L132 原文）。
登记为 OP-146，本包闭合它。

---

## 一、协议原文不可得（**必须显式声明**）

协议 §9 第 8 行的**原文不在本机**（规格源 `Rednote研发计划_01~04 V1.0`
全盘无命中，见说明文档 §0.1）。本端点的形状是**回溯结论**：

| 项 | 本包取值 | 回溯依据 |
|---|---|---|
| 信封 | `data` + `meta` | WP20 的 9 个端点全部如此（`DataEnvelope`） |
| 字段命名 | camelCase | 同上（`populate_by_name` + `alias`） |
| 成功码 | 200（不是 201） | 检索**不创建资源**，与 `GET` 同族 |
| 分页 | **不用** `platform.pagination` | 检索不是"翻页"而是"取最相近的 k 条"（`limit` 语义） |
| 权限 | 只读 → 只需登录 | 与 WP20 的读端点同口径（知识"全员可查"） |

**已登记 OP-164**：原件到位后需按原文复核。**本端点的每个字段都
在 `_meta` 里回报了它的来源口径**，因此复核时不必读代码。

---

## 二、为什么用 `POST` 而不是 `GET`

协议定的是 `POST`（原文如此）。它也是**正确**的：

  · 查询是一个**结构化对象**（query + subject 三元组 + category + mode），
    塞进 query string 会需要 `subject.brandId` 这种点号编码；
  · `query` 可能较长（一段 Brief 摘要），URL 长度有限制；
  · **它不产生副作用**，但"用 POST 表达复杂查询"是 REST 的常见做法。

**注意：不要因为"它是 POST"就给它写权限依赖。**
`KNOWLEDGE_WRITE` 依赖（`require_org_action(KNOWLEDGE_MANAGE)`）
管的是"能不能改知识"。检索是**读**，加写权限会让只读用户
（例如只做查看的管理者）无法检索 —— 而他们本来就该能。

---

## 三、`subject` 为什么在请求体里而不是从会话推断

`subject`（`brandId` / `productId` / `projectId`）是**这次检索的可见范围**，
不是"当前用户的权限"（见 `embedding_service.search` 的说明）。

⚠️ **实测口径（收口时核实，必须写明）**：本端点的依赖是
`CurrentUserDep` —— **只校验登录，没有品牌/项目级权限校验**。
因此 `subject` 由调用方**自己声明**，一个登录用户传任意 `brandId`
即可拿到该品牌的 `BRAND` 范围知识正文。

- 本端点保证的是**"不串事实"**（检索 A 品牌的范围不会混进 B 品牌的事实）；
- 它**不**保证**"不越权"**（不阻止调用方声称自己是 B 品牌）。

**这是 WP20 既定口径的延续**（WP20 的读端点也是"全员可查，登录即通过"），
不是本包引入的。**但检索返回的是事实正文，后果比列表的摘要重**，
因此本包显式登记了"是否需要品牌级权限"这一待定项（需产品定模型）。
若将来收紧，落点就在这里的依赖里加一层"user 对 `subject` 是否有权"的校验。

**为什么不从会话推断**：同一个用户在不同项目里工作时，可见范围不同
（项目私有的知识只在该项目可见），而会话只有一个用户身份。
从会话推断意味着"项目私有的知识永远可见或永远不可见" ——
两种都不对。

**为什么它是必填**：缺 `subject` 时只有 `GLOBAL` 的知识可见
（fail-closed）。这是**安全**的，但调用方往往不是故意的 ——
一个必填字段让"我忘了传"在**请求时**就暴露，而不是在
"检索结果少得可疑"时才被怀疑。

**但允许三个字段都为空对象**（`subject: {}`）：那是**显式声明**
"我只要 GLOBAL 的知识"。显式声明与"忘了传"是两件事 ——
前者是一个合法的窄检索，后者是 bug。
（Pydantic 无法区分"字段缺失"与"传了空对象"，因此请求体里
`subject` 必填、其内部字段全可空 —— 这个组合恰好表达了上述语义。）

---

## 四、响应里为什么报告这么多"元信息"

`_meta` 里有 `profileId` / `profileName` / `mode` / `subject` /
`scopeFilterApplied` / `scanned` / `indexReady` / `limit`。

**这不是冗余**，它们回答的是四个**排障问题**：

| 字段 | 回答的问题 |
|---|---|
| `profileId` / `profileName` | 这次用的是哪个索引空间？（换版期间最关键） |
| `subject` + `scopeFilterApplied` | 为什么没召回？（库里没有 vs 被范围过滤） |
| `scanned` | 为什么只返回 2 条？（符合条件的就是 2 条） |
| `indexReady` | 为什么这么慢？（索引没建 → 精确扫描） |

四个问题的共同点：**症状不报错**。检索返回 200 + 空数组/慢响应，
而调用方无法区分原因。把这些事实回报是最低成本的诊断能力。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from rednote.modules.identity.dependencies import CurrentUserDep, DBSession
from rednote.modules.knowledge import embedding_service
from rednote.modules.knowledge.embedding_repository import EmbeddingRepository
from rednote.modules.knowledge.embedding_types import (
    DEFAULT_SEARCH_LIMIT,
    DEFAULT_SEARCH_MODE,
    MAX_SEARCH_LIMIT,
    MIN_SEARCH_LIMIT,
    ScopeSubject,
    SearchOutcome,
)
from rednote.modules.knowledge.indexing import DeterministicEmbedder, Embedder
from rednote.platform.api_responses import READ_RESPONSES
from rednote.platform.errors import DataEnvelope

router = APIRouter(tags=["knowledge"])

SEARCH_RESPONSES = READ_RESPONSES


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class SearchSubjectBody(BaseModel):
    """检索的可见范围主体（见模块 docstring 第三节）。"""

    # `extra="forbid"` 是本项目的请求体铁律（WP23 的 ISS-80）：
    # 缺它 → Pydantic 默认 `ignore` → 越权/拼错的字段被**静默丢弃**，
    # 而调用方合理地以为"我设置了它"。实测后果：一个拼错的
    # `brandID`（大写 D）会被忽略，于是检索范围比预期宽 —— 而 200。
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    brand_id: uuid.UUID | None = Field(default=None, alias="brandId")
    product_id: uuid.UUID | None = Field(default=None, alias="productId")
    project_id: uuid.UUID | None = Field(default=None, alias="projectId")

    def to_value(self) -> ScopeSubject:
        return ScopeSubject(
            brand_id=self.brand_id,
            product_id=self.product_id,
            project_id=self.project_id,
        )


class SearchBody(BaseModel):
    """`POST /knowledge/searches` 的请求体。

    **`extra="forbid"` 是必需的**（见 `SearchSubjectBody` 的说明）。
    本端点尤其需要它：`subject` 是一个嵌套对象，一个拼错的顶层字段
    （例如 `filters`）会被静默忽略 —— 于是"我加了筛选"与
    "结果变多了"同时发生，而**没有任何错误**。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    query: str = Field(min_length=1)
    # 必填（见模块 docstring 第三节）。内部字段全可空 —— 那是
    # "显式声明只要 GLOBAL"的合法表达。
    subject: SearchSubjectBody
    mode: str = Field(default=DEFAULT_SEARCH_MODE)
    category: str | None = None
    limit: int = Field(
        default=DEFAULT_SEARCH_LIMIT, ge=MIN_SEARCH_LIMIT, le=MAX_SEARCH_LIMIT
    )
    profile_id: uuid.UUID | None = Field(default=None, alias="profileId")


# ---------------------------------------------------------------------------
# 响应体
# ---------------------------------------------------------------------------


class SearchHitBody(BaseModel):
    """一条检索结果。"""

    model_config = ConfigDict(populate_by_name=True)

    chunk_id: uuid.UUID = Field(alias="chunkId")
    chunk_no: int = Field(alias="chunkNo")
    asset_id: uuid.UUID = Field(alias="assetId")
    asset_title: str = Field(alias="assetTitle")
    version_id: uuid.UUID = Field(alias="versionId")
    category: str
    scope: str
    text: str
    similarity: float
    distance: float


class SearchMetaBody(BaseModel):
    """检索的元信息（见模块 docstring 第四节）。

    ⚠️ **它不在协议 §1 的 `meta` 块里** —— 那一块是固定形状
    （`{"requestId": ...}`，见 `platform.errors.Meta`），
    往里塞检索字段会让所有响应共享一个不断膨胀的 meta。

    因此本端点把检索元信息放在 `data` 的**同级包装**里：

        {"data": {"hits": [...], "search": {...}}, "meta": {"requestId": ...}}

    **为什么不让 `data` 直接是数组**：数组无法携带"这次检索用了什么口径"。
    包一层之后，`data.hits` 是结果、`data.search` 是口径 ——
    两者在同一个对象里，调用方不可能只拿到其中一个。
    """

    model_config = ConfigDict(populate_by_name=True)

    profile_id: uuid.UUID = Field(alias="profileId")
    profile_name: str = Field(alias="profileName")
    mode: str
    subject: dict[str, str | None]
    scope_filter_applied: bool = Field(alias="scopeFilterApplied")
    scope_note: str = Field(alias="scopeNote")
    scanned: int
    limit: int
    index_ready: bool = Field(alias="indexReady")
    chunk_count_for_profile: int = Field(alias="chunkCountForProfile")


class SearchResultBody(BaseModel):
    """`data` 的包装（结果 + 口径）。"""

    model_config = ConfigDict(populate_by_name=True)

    hits: list[SearchHitBody]
    search: SearchMetaBody


# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------


def get_embedding_repository(db: DBSession) -> EmbeddingRepository:
    return EmbeddingRepository(db)


EmbeddingRepoDep = Annotated[EmbeddingRepository, Depends(get_embedding_repository)]


def get_embedder(request: Request) -> Embedder:
    """取"文本 → 向量"的实现。

    **从 `app.state` 取，不建模块级单例**（本项目 ISS-09/54 的教训：
    无请求上下文的入口退回全局单例，会连到另一个库 / 用错配置）。

    **默认是确定性替身**：本机没有真实 embedding 模型
    （OP-44 未定 + ADR-10 E-4 默认不出网）。
    真实实现由 `app.state.embedder` 注入（WP26 接入时）。

    **维度取 1024**：与 `PLANNED_EMBEDDING_DIMENSIONS` 一致。
    但**真正决定维度的是 profile** —— `embedding_service.search`
    会校验 `len(vector) == profile.dimensions`，不匹配就 422。
    因此替身的维度**不是**"真相"，它只是一个默认值；
    真相在 profile 那一行。
    """
    from rednote.platform.capacity import PLANNED_EMBEDDING_DIMENSIONS

    embedder = getattr(request.app.state, "embedder", None)
    if embedder is None:
        embedder = DeterministicEmbedder(
            dimensions=PLANNED_EMBEDDING_DIMENSIONS
        )
    return embedder


EmbedderDep = Annotated[Embedder, Depends(get_embedder)]


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.post(
    "/knowledge/searches",
    status_code=status.HTTP_200_OK,
    response_model=DataEnvelope[SearchResultBody],
    responses=SEARCH_RESPONSES,
    summary="语义检索知识（含范围过滤）",
    description=(
        "按 query 检索知识片段。**检索结果按 `subject` 声明的可见范围过滤**："
        "`GLOBAL` 的知识永远可见；`BRAND`/`PRODUCT`/`PROJECT` 范围的"
        "知识只在对应限定列匹配时可见。未提供某个维度时该维度的知识"
        "**不可见**（fail-closed，不是不过滤）。\n\n"
        "`data.search` 报告 `subject` / `scopeFilterApplied` / `scanned` / "
        "`indexReady`，用于区分「库里没有」与「被范围过滤掉了」。"
    ),
)
def search_knowledge(
    body: SearchBody,
    repo: EmbeddingRepoDep,
    embedder: EmbedderDep,
    _user: CurrentUserDep,
) -> dict[str, Any]:
    """检索知识（协议 §9 第 8 行）。

    **权限：只需登录**（见模块 docstring 第二节）。

    **本函数不做参数校验**（`mode` / `limit` / `query` 全部由
    `embedding_service.search` 校验）。端点曾经在这里重复了一遍
    `mode` 校验，而两处的报错文案**已经不一致**（这里是
    `sorted(SEARCH_MODES)` 的 Python 列表表示，服务层是
    `SEARCH_MODE_LIST_SQL` 的 SQL 字面量表示）—— 同一个事实有两处实现，
    迟早分叉（WP15 的 ISS-47 同族）。收敛到服务层后，"什么 mode 合法"
    只有一处定义，且**服务层单测与 HTTP 用例覆盖的是同一段代码**。

    **`scanned` 与 `len(hits)` 的关系**：`scanned >= len(hits)`。
    两者不等时说明"符合条件的行多于 limit"（正常），
    或"`limit` 截断了结果"（也是正常，但调用方应知道）。
    它们相等且都小于 `limit` 时，说明"库里符合条件的就这么多" ——
    那正是"为什么只返回 2 条"的答案。
    """
    outcome = embedding_service.search(
        repo,
        query=body.query,
        subject=body.subject.to_value(),
        profile_id=body.profile_id,
        mode=body.mode,
        category=body.category,
        limit=body.limit,
        embedder=embedder,
    )
    return _envelope(repo, outcome=outcome, limit=body.limit)


def _envelope(
    repo: EmbeddingRepository,
    *,
    outcome: SearchOutcome,
    limit: int,
) -> dict[str, Any]:
    """组装响应（结果 + 口径 + 协议 §1 的 meta）。

    **`chunkCountForProfile` 是一次额外查询**（`count_chunks_for_profile`）。
    它的价值：调用方看到 `hits: []` 时能立刻区分
    「这个 profile 下一条都没有」与「有 N 条但都被范围过滤了」。
    没有它，"空结果"是一个无法归因的状态。
    """
    from rednote.modules.knowledge.scope_filter import scope_coverage_note
    from rednote.platform.pagination import single_response

    profile = repo.get_profile(outcome.profile_id)
    profile_name = profile.name if profile is not None else ""
    index_ready = bool(profile.index_ready) if profile is not None else False
    chunk_count = repo.count_chunks_for_profile(outcome.profile_id)

    hits = [
        SearchHitBody(
            chunk_id=hit.chunk_id,
            chunk_no=hit.chunk_no,
            asset_id=hit.asset_id,
            asset_title=hit.asset_title,
            version_id=hit.version_id,
            category=hit.category,
            scope=hit.scope,
            text=hit.text_content,
            similarity=hit.similarity,
            distance=hit.distance,
        )
        for hit in outcome.hits
    ]
    search_meta = SearchMetaBody(
        profile_id=outcome.profile_id,
        profile_name=profile_name,
        mode=outcome.mode,
        subject=outcome.subject.as_dict(),
        scope_filter_applied=outcome.scope_filter_applied,
        scope_note=scope_coverage_note(outcome.subject),
        scanned=outcome.scanned,
        limit=limit,
        index_ready=index_ready,
        chunk_count_for_profile=chunk_count,
    )
    payload = SearchResultBody(hits=hits, search=search_meta)
    return single_response(payload.model_dump(by_alias=True, mode="json"))


__all__ = [
    "EmbedderDep",
    "EmbeddingRepoDep",
    "SearchBody",
    "SearchHitBody",
    "SearchMetaBody",
    "SearchResultBody",
    "SearchSubjectBody",
    "get_embedder",
    "get_embedding_repository",
    "router",
    "search_knowledge",
]
