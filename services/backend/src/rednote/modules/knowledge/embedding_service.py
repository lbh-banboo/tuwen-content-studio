"""检索域的业务规则（**唯一对外入口**）。

**本层职责**：判定 + 编排。存储访问在 `embedding_repository.py`，
纯函数在 `chunking.py` / `scope_filter.py`。

---

## 一、本包闭合的两个 OP

### OP-149：`INDEXING → ACTIVE` 首次真实可达

WP20 的登记原文：

> **`INDEXING` 状态的判据是「生效版本无 chunk 行」，而 chunk 的生产者
> （WP21）尚未存在** …… 也就是说**本包交付后，所有已发布知识都会停在
> `INDEXING`**，而这是"实现正确但生产者缺失"的状态，不是缺陷。

`ingest_version()` 是本包的生产者：它把 chunk 写进 `knowledge_chunks`。
于是 WP20 的 `projection.py` 判据（"有 chunk 行 → `ACTIVE`"）
**第一次有东西能满足它**。

**它不是"接上一个回调"那么简单** —— 判据是
「有代码路径 / 有测试 / 有落库行」三者齐全：
  · 代码路径：`ingest_version` → `insert_chunks` → 落库；
  · 测试：`test_knowledge_wp21.py` 断言"切块后状态从 INDEXING 变 ACTIVE"；
  · 落库行：证据脚本在真库上跑一遍并回查行数。

### OP-146：`POST /knowledge/searches` 实现

协议 §9 共 10 行，WP20 做了 9 行，第 8 行是本包的 `search()`。

---

## 二、三条设计要点

### 1. `ingest_version` 必须**幂等**（重切不产生重复 chunk）

Outbox 消费者会重试（WP13 的机制）。若重试产生第二批 chunk，
`UQ(knowledge_version_id, chunk_no)` 会拦下 —— **但拦下的是异常**，
而正确的行为是"重试是安全的，结果一样"。

因此 `ingest_version` 的第一件事是**删掉该版本的既有 chunk**
（向量随 CASCADE 一起删）。删了重写 = 幂等。

**代价**：每次重切都重算全部向量。对本规模可接受；
"只重算变化的块"需要 `text_hash` 对比（那是优化，不是正确性），
已登记为待定项。

### 2. 检索前**必须**检查 `index_ready`

若 profile 的索引没建（`index_ready = false`），检索会**退化成精确扫描** ——
能出结果，只是慢。而"慢"不是错误，**没有任何信号**。

本实现的选择：**不拦**（精确扫描的结果是**正确的**），
但在响应 `meta` 里报告 `indexReady: false` —— 让"为什么这么慢"
有答案。拦下它会让"索引还没建好"变成"检索完全不可用"，
那是更差的选择（正确性优先）。

### 3. 空查询 / 空结果都是**正常返回**，不是错误

`hits: []` 是合法响应。理由：检索是"找有没有"，"没有"是答案之一。
把它做成 404 会让调用方（WP26 生成）无法区分"检索失败"与"没有相关知识"，
而这两者的后续动作完全不同（前者该重试，后者该继续生成）。

**但**：`meta` 里必须报告 `subject` 与 `scopeFilterApplied` ——
否则"没有"与"被过滤掉了"不可区分（风险 R5）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy.exc import IntegrityError

from rednote.modules.knowledge.chunking import chunk_for_version
from rednote.modules.knowledge.embedding_models import (
    MAX_MODEL_VERSION_CHARS,
    MAX_PROFILE_NAME_CHARS,
    EmbeddingProfile,
)
from rednote.modules.knowledge.embedding_repository import EmbeddingRepository
from rednote.modules.knowledge.embedding_types import (
    DEFAULT_CHUNK_CHARS,
    DEFAULT_CHUNK_OVERLAP_CHARS,
    DEFAULT_EMBEDDING_DIMENSIONS,
    DEFAULT_HNSW_EF_SEARCH,
    DEFAULT_SEARCH_LIMIT,
    MAX_EMBEDDING_DIMENSIONS,
    MAX_HNSW_EF_SEARCH,
    MAX_SEARCH_LIMIT,
    MAX_SEARCH_SCAN,
    METRIC_COSINE,
    MIN_EMBEDDING_DIMENSIONS,
    MIN_HNSW_EF_SEARCH,
    MIN_SEARCH_LIMIT,
    PROFILE_STATE_ACTIVE,
    PROFILE_STATE_DEPRECATED,
    PROFILE_STATES,
    SEARCH_KEYWORD,
    SEARCH_MODE_LIST_SQL,
    SEARCH_MODES,
    SEARCH_SEMANTIC,
    ScopeSubject,
    SearchHit,
    SearchOutcome,
    distance_to_similarity,
)
from rednote.platform.errors import ApiError, ErrorCode

#: HNSW 构建参数的默认值（pgvector 的默认也是 16 / 64，
#: 但**显式给**，见 `embedding_models` 的说明）。
DEFAULT_HNSW_M: Final[int] = 16
DEFAULT_HNSW_EF_CONSTRUCTION: Final[int] = 64


@dataclass(frozen=True)
class IngestionResult:
    """一次切块入库的结论。"""

    version_id: uuid.UUID
    chunk_count: int
    embedding_count: int
    replaced_chunks: int
    chunker_version: str


def _validation_error(field: str, message: str, *, reason: str) -> ApiError:
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        message,
        details={
            "fieldErrors": [{"field": field, "type": reason, "message": message}]
        },
    )


def _require_non_empty(value: str, field: str, *, max_chars: int) -> str:
    """校验一个非空字符串（去首尾空白后判空，并卡长度）。"""
    normalized = value.strip()
    if not normalized:
        raise _validation_error(field, f"{field} 不能为空。", reason="required")
    if len(normalized) > max_chars:
        raise _validation_error(
            field,
            f"{field} 长度不能超过 {max_chars} 字符。",
            reason="too_long",
        )
    return normalized


# ---------------------------------------------------------------------------
# embedding profile
# ---------------------------------------------------------------------------


def create_profile(
    repo: EmbeddingRepository,
    *,
    name: str,
    model_profile_id: uuid.UUID,
    model_version: str,
    dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
    metric: str = METRIC_COSINE,
    hnsw_m: int = DEFAULT_HNSW_M,
    hnsw_ef_construction: int = DEFAULT_HNSW_EF_CONSTRUCTION,
    hnsw_ef_search: int = DEFAULT_HNSW_EF_SEARCH,
) -> EmbeddingProfile:
    """建一个 embedding profile（= ADR-15 的「索引空间」）。

    **它不建索引**。索引 DDL 需要 profile 的 id（索引名含它），
    而 id 在 flush 之前不可靠（虽然 `uuid_pk()` 是 Python 侧生成，
    但"先有行再有索引"的顺序更直白）。
    因此分两步：`create_profile()` → `build_index()`。

    **为什么分成两步而不是一步**：建索引是**重操作**（200 万行时是分钟级），
    它会长时间持有锁。把它与"插入一行配置"放在同一事务里，
    会让一个本该毫秒级的事务变成分钟级 —— 而失败时**两者一起回滚**，
    于是"配置建了但索引没建"这个状态**无法表达**，
    而它正是 `index_ready` 要表达的状态。

    **`model_version` 是必填**（不是从 `model_profiles` 派生）：
    ADR-15 的硬约束比较对象是它，而"同一模型的两个版本共用一条
    `model_profiles` 行"是合法配置（见 `embedding_models` 的说明）。
    派生会让这两个版本无法区分。
    """
    clean_name = _require_non_empty(
        name, "name", max_chars=MAX_PROFILE_NAME_CHARS
    )
    clean_version = _require_non_empty(
        model_version, "modelVersion", max_chars=MAX_MODEL_VERSION_CHARS
    )
    if not (MIN_EMBEDDING_DIMENSIONS <= dimensions <= MAX_EMBEDDING_DIMENSIONS):
        raise _validation_error(
            "dimensions",
            f"维度必须在 [{MIN_EMBEDDING_DIMENSIONS}, {MAX_EMBEDDING_DIMENSIONS}] 内。",
            reason="out_of_range",
        )
    if metric not in _metrics():
        raise _validation_error(
            "metric", f"度量必须是 {_metrics()} 之一。", reason="unknown_metric"
        )
    if not (MIN_HNSW_EF_SEARCH <= hnsw_ef_search <= MAX_HNSW_EF_SEARCH):
        raise _validation_error(
            "hnswEfSearch",
            f"efSearch 必须在 [{MIN_HNSW_EF_SEARCH}, {MAX_HNSW_EF_SEARCH}] 内。",
            reason="out_of_range",
        )
    if hnsw_m <= 0 or hnsw_ef_construction <= 0:
        raise _validation_error(
            "hnswM",
            "hnswM 与 hnswEfConstruction 必须为正整数。",
            reason="out_of_range",
        )
    if repo.get_profile_by_name(clean_name) is not None:
        # **用 `VALIDATION_FAILED`（422）而不是 409**：本项目
        # `ErrorCode` 枚举里**没有通用的 CONFLICT** —— 409 只有
        # `INVALID_TRANSITION` / `IDEMPOTENCY_CONFLICT` / `DUPLICATE_PUBLICATION`
        # 三个，它们的语义都是"状态机不允许这次转换"，与"名字撞了"不同。
        # 硬套 `IDEMPOTENCY_CONFLICT` 会让前端把"重名"当成"幂等键冲突"
        # 去处理（两者的正确动作不同：前者改名字，后者比对请求内容）。
        # 因此用 422 + `fieldErrors`，让调用方知道**改哪个字段**。
        raise _validation_error(
            "name",
            f"已存在同名 embedding profile：{clean_name}",
            reason="duplicate_name",
        )

    row = repo.insert_profile(
        name=clean_name,
        model_profile_id=model_profile_id,
        model_version=clean_version,
        dimensions=dimensions,
        metric=metric,
        hnsw_m=hnsw_m,
        hnsw_ef_construction=hnsw_ef_construction,
        hnsw_ef_search=hnsw_ef_search,
        state=PROFILE_STATE_ACTIVE,
    )
    try:
        repo.flush()
    except IntegrityError as exc:
        # 唯一约束（name）或 FK（model_profiles）—— 两者都是"输入问题"。
        raise _integrity_to_api_error(exc, name=clean_name) from exc
    return row


def _metrics() -> tuple[str, ...]:
    """度量取值域（**从 types 取，不在这里重写**）。"""
    from rednote.modules.knowledge.embedding_types import METRICS

    return METRICS


def _integrity_to_api_error(exc: IntegrityError, *, name: str) -> ApiError:
    """把 profile 插入的完整性错误折成可读的 4xx。

    **不笼统地报 500**：两种失败的原因完全不同（名字重复 vs
    模型档案不存在），而调用方需要知道该改哪个字段。
    """
    detail = str(getattr(exc, "orig", exc))
    if "uq_embedding_profiles_name" in detail:
        # 见 `create_profile` 里的说明：用 422 而不是 409（本项目没有通用 CONFLICT）。
        return _validation_error(
            "name",
            f"已存在同名 embedding profile：{name}",
            reason="duplicate_name",
        )
    if "model_profiles" in detail:
        return _validation_error(
            "modelProfileId",
            "模型档案不存在（modelProfileId 必须指向 WP16 的 model_profiles 行）。",
            reason="foreign_key_missing",
        )
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        "embedding profile 不满足数据库约束。",
        details={"detail": detail[:400]},
    )


def build_index(
    repo: EmbeddingRepository,
    *,
    profile: EmbeddingProfile,
) -> EmbeddingProfile:
    """为 profile 建 HNSW 索引，并把 `index_ready` 置 true。

    **DDL 从 `embedding_ddl.hnsw_index_ddl` 生成** —— 与迁移、证据脚本
    **同源**。手抄一份 SQL 会漂移，而漂移的方向是"库里的索引与
    代码以为的不是同一个"。

    **`CREATE INDEX`（不带 CONCURRENTLY）**：`CONCURRENTLY` 不能在
    事务块内执行，而本项目的所有写路径都在事务里。
    代价：建索引时表被锁。本规模（远未到 200 万）下可接受；
    大表重建索引的运维方案（`CONCURRENTLY` + 独立连接）已登记为待定项。

    **`index_ready` 与 DDL 成对**：见 `embedding_ddl` 第二节。
    两者若不成对，"检索前检查 index_ready"这个信号就是假的。
    """
    from sqlalchemy import text

    from rednote.modules.knowledge.embedding_ddl import hnsw_index_ddl

    ddl = hnsw_index_ddl(
        profile_id=str(profile.id),
        dimensions=profile.dimensions,
        metric=profile.metric,
        m=profile.hnsw_m,
        ef_construction=profile.hnsw_ef_construction,
    )
    repo.session.execute(text(ddl))
    updated = repo.mark_index_ready(profile_id=profile.id, ready=True)
    if updated is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND, "embedding profile 不存在。"
        )
    return updated


def switch_profile(
    repo: EmbeddingRepository, *, new_profile_id: uuid.UUID
) -> tuple[EmbeddingProfile, list[EmbeddingProfile]]:
    """把默认检索目标切到新 profile（ADR-15 切换流程的第 4 步）。

    返回 `(新的默认 profile, 被降级为 DEPRECATED 的 profile 列表)`。

    **顺序：先升新的，再降旧的**。反过来会在两步之间留下
    "没有 ACTIVE profile"的窗口 —— 而那期间并发检索会失败
    （`get_active_profile` 返回 None）。先升新的则最坏情况是
    "有两个 ACTIVE"，而 `get_active_profile` 按 `created_at DESC`
    取最新的那个 —— 即**新的那个**。于是窗口期的行为是
    "检索已经用上新的"，这是可接受的。

    **这是"顺序敏感"的一处，必须写清楚**：两种顺序在**串行测试**下
    完全等价（都不报错、结果一样），只有并发才暴露差异。
    本项目已踩过同族（ISS-82：顺序调用构造的判据恒真）。
    因此测试必须用**可控替身**造出"那一刻"，并做**反向验证**。
    """
    new_profile = repo.get_profile(new_profile_id)
    if new_profile is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "embedding profile 不存在。",
            details={"profileId": str(new_profile_id)},
        )
    if not new_profile.index_ready:
        # ADR-15 第 4 步的前置条件原文：「新 profile 检索质量经回归确认后，
        # 将流量切至新 profile」。索引没建就没有"质量"可确认。
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "新 profile 的索引尚未建立，不能切为默认检索目标。"
            "先调用建索引，确认检索质量后再切换。",
            details={"profileId": str(new_profile_id), "indexReady": False},
        )

    demoted: list[EmbeddingProfile] = []

    # **顺序：先升新的，再降旧的**（见 docstring 的窗口说明）。
    #
    # ⚠️ 本处曾经写反（循环里先降旧的，循环之后才升新的）——
    # 而 docstring 一开始就写明了正确的顺序。这类"文档对、代码错"的缺陷
    # 不会有任何机械判据发现：两种顺序在**串行测试**下完全等价
    # （都不报错、结果一样），只有并发或"断言调用顺序"的测试才暴露。
    # 本包的反向验证（`test_switch_profile_never_leaves_no_active`）
    # 就是靠断言**调用顺序**抓到的。
    promoted = repo.set_profile_state(
        profile_id=new_profile.id, state=PROFILE_STATE_ACTIVE
    )
    if promoted is None:
        raise ApiError(ErrorCode.RESOURCE_NOT_FOUND, "embedding profile 不存在。")

    for other in repo.list_profiles(state=PROFILE_STATE_ACTIVE):
        if other.id == new_profile.id:
            continue
        updated = repo.set_profile_state(
            profile_id=other.id, state=PROFILE_STATE_DEPRECATED
        )
        if updated is not None:
            demoted.append(updated)

    return promoted, demoted


def resolve_profile(
    repo: EmbeddingRepository, *, profile_id: uuid.UUID | None
) -> EmbeddingProfile:
    """解析这次检索用哪个 profile。

    · 显式给了 `profile_id` → 用它（**允许用 DEPRECATED 的** ——
      ADR-15 第 3 步："切换期间旧 profile 保持可用"）；
    · 没给 → 取当前 `ACTIVE` 的那个。

    **显式给了但不存在 → 404**（不是静默回退到默认）：
    静默回退会让"我指定了 profile A，结果拿到 profile B 的结果"
    变成一个不可诊断的行为 —— 而向量空间不同，结果是**语义无关**的。
    """
    if profile_id is not None:
        profile = repo.get_profile(profile_id)
        if profile is None:
            raise ApiError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "embedding profile 不存在。",
                details={"profileId": str(profile_id)},
            )
        return profile
    profile = repo.get_active_profile()
    if profile is None:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "系统还没有可用的 embedding profile（没有 ACTIVE 的 profile）。"
            "请先建 profile 并建立索引。",
            details={"profileStates": list(PROFILE_STATES)},
        )
    return profile


# ---------------------------------------------------------------------------
# 切块入库（闭合 OP-149）
# ---------------------------------------------------------------------------


def ingest_version(
    repo: EmbeddingRepository,
    *,
    version_id: uuid.UUID,
    profile: EmbeddingProfile,
    embedder: Any,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS,
) -> IngestionResult:
    """给一个知识版本切块并写向量。**幂等**（见模块 docstring 第二节第 1 点）。

    `embedder` 是"文本 → 向量"的可调用对象。**它必须是可注入的**：
    本机没有真实 embedding 模型（ADR-10 E-4 默认不出网 + OP-44 未定），
    因此测试与证据脚本传**确定性替身**（见 `indexing.DeterministicEmbedder`）。

    **为什么用参数注入而不是模块级单例**：模块级单例会让测试之间
    互相影响，且"这个函数依赖一个全局的模型"这件事从签名里看不见 ——
    本项目已踩过（WP08/ISS-09 的第三次现身：无请求上下文的入口
    退回全局单例，连到另一个库）。

    ### 执行顺序（**不能换**）

    1. 取版本行（不存在 → 404）；
    2. **删掉该版本既有 chunk**（幂等；向量随 CASCADE 删）；
    3. 切块（**纯函数**，不碰数据库）；
    4. 空块 → 提前返回（**不是错误**，见下）；
    5. 写 chunk 行（`insert_chunks` 内部 flush，FK 要求）；
    6. 算向量（**逐块调 embedder**）；
    7. 批量写向量（混用守卫在此执行）；
    8. 返回结论。

    ### 第 4 步：空块为什么不是错误

    一条只填了 `structured_payload = {"note": ""}` 的知识切不出任何块
    （空串被 `_render_field_value` 过滤）。此时：

      · **不是错误** —— 输入合法，只是没有可切的内容；
      · **但要显式返回 `chunk_count = 0`** —— 让调用方（Outbox 消费者）
        能报告"这条知识没有可索引的内容"，而不是静默成功。

    后果：该知识的状态**停在 `INDEXING`**（因为没有 chunk 行）。
    这是**正确的** —— 它确实没有被索引。把它强行标成 `ACTIVE`
    才是错的（那正是 WP20 的 ISS-66 要防的"两处判据"）。

    ### 第 6 步：为什么逐块调 embedder 而不是批量

    真实 embedding 服务的批量接口一次能收的文本数有上限（不同厂商不同），
    而本包**不知道**上限（OP-165）。逐块调是**保守但正确**的选择。
    批量优化已登记为待定项。
    """
    version = repo.get_version(version_id)
    if version is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "知识版本不存在。",
            details={"versionId": str(version_id)},
        )

    replaced = repo.delete_chunks_for_version(version_id)

    drafts = chunk_for_version(
        body=version.body or "",
        structured_payload=dict(version.structured_payload or {}),
        chunk_chars=chunk_chars,
        overlap_chars=overlap_chars,
    )
    if not drafts:
        repo.flush()
        return IngestionResult(
            version_id=version_id,
            chunk_count=0,
            embedding_count=0,
            replaced_chunks=replaced,
            chunker_version=_chunker_version(),
        )

    chunk_ids = repo.insert_chunks(version_id=version_id, drafts=drafts)

    items: list[tuple[uuid.UUID, str, list[float]]] = []
    for chunk_id, draft in zip(chunk_ids, drafts, strict=True):
        vector = embedder.embed(draft.text_content)
        if len(vector) != profile.dimensions:
            # **在写库前就报错**（而不是让守卫在库里报）：
            # 守卫的报错信息是给排障看的，而这里是**调用方的问题**
            # （embedder 与 profile 不匹配），必须在能给出可读 4xx 的位置拦下。
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                f"embedder 返回 {len(vector)} 维向量，而 profile "
                f"{profile.name} 声明 {profile.dimensions} 维。"
                "两者不匹配 —— 换模型必须建新 profile（ADR-15 硬约束）。",
                details={
                    "profileId": str(profile.id),
                    "expectedDimensions": profile.dimensions,
                    "actualDimensions": len(vector),
                },
            )
        items.append((chunk_id, draft.text_hash, vector))

    written = repo.insert_embeddings(profile=profile, items=items)
    return IngestionResult(
        version_id=version_id,
        chunk_count=len(drafts),
        embedding_count=written,
        replaced_chunks=replaced,
        chunker_version=_chunker_version(),
    )


def _chunker_version() -> str:
    from rednote.modules.knowledge.chunking import CHUNKER_VERSION

    return CHUNKER_VERSION


# ---------------------------------------------------------------------------
# 检索（闭合 OP-146）
# ---------------------------------------------------------------------------


def search(
    repo: EmbeddingRepository,
    *,
    query: str,
    subject: ScopeSubject,
    profile_id: uuid.UUID | None = None,
    mode: str = SEARCH_SEMANTIC,
    category: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    embedder: Any = None,
) -> SearchOutcome:
    """`POST /knowledge/searches` 的服务层实现。

    ### 校验顺序（**先校验再取 profile**）

    参数校验在前：`limit` / `mode` / `query` 都是**调用方的输入**，
    它们的错误与"库里有没有 profile"无关。先取 profile 会让
    "limit=999"这种错误在"没有 profile"时被报成"没有 profile" ——
    排障时会去查错的地方。

    ### `query` 为空 → 422 而不是"返回全部"

    空查询的语义在检索里**没有定义**（它不是"匹配所有"）。
    若实现成"返回全部"，它会变成一个**绕过范围过滤的隐患**：
    调用方传空串能拿到一大批结果，而这些结果的排序是任意的。
    因此显式 422。

    ### 范围主体**不在服务层做权限判定**（⚠️ 一处必须讲清的口径）

    本层的 `subject` 是**调用方声明的可见范围**，不是"当前用户的权限"。

    ⚠️ **实测口径（收口时核实）**：查端点只有 `CurrentUserDep`（**只校验登录**），
    **没有**品牌/项目级权限校验。也就是说：

      · `subject` 由调用方**自己声明** —— 一个登录用户传任意 `brandId`
        就能拿到该品牌的 `BRAND` 范围知识**正文**；
      · 本层的范围过滤保证的是**"不串事实"**（A 品牌的检索不会混进 B 品牌的
        事实），**不是"不越权"**（它不阻止调用方声称自己是 B 品牌）。

    **这是 WP20 既定口径的延续，不是本包引入的**：WP20 的读端点同样是
    "全员可查，登录即通过"（`api.py` 的 `del user  # 全员可读`），
    口径是"知识是组织内共享资产"。**但检索路径的后果比列表路径重** ——
    列表返回摘要，检索返回**事实正文**。

    因此本包做到的是：① 把口径**写清楚**，不留"已判权限"的假承诺；
    ② 把"是否需要品牌级权限"登记为待定项（需产品定模型）。
    若将来要收紧，落点在 `search_api.py` 的依赖里加一层
    "user 对 subject 声明的 brand/project 是否有权"的校验 ——
    而**不是**改本层（本层拿不到 user）。
    """
    # --- 1) 参数校验 ---------------------------------------------------
    clean_query = query.strip()
    if not clean_query:
        raise _validation_error(
            "query", "query 不能为空。", reason="required"
        )
    if not (MIN_SEARCH_LIMIT <= limit <= MAX_SEARCH_LIMIT):
        raise _validation_error(
            "limit",
            f"limit 必须在 [{MIN_SEARCH_LIMIT}, {MAX_SEARCH_LIMIT}] 内。",
            reason="out_of_range",
        )
    if mode not in SEARCH_MODES:
        raise _validation_error(
            "mode",
            f"mode 必须是 {SEARCH_MODE_LIST_SQL} 之一。",
            reason="unknown_mode",
        )

    # --- 2) 解析 profile ------------------------------------------------
    profile = resolve_profile(repo, profile_id=profile_id)
    repo.set_ef_search(profile.hnsw_ef_search)

    # --- 3) 按模式检索 --------------------------------------------------
    if mode == SEARCH_KEYWORD:
        hits, scanned = _keyword_hits(
            repo, query=clean_query, subject=subject, category=category, limit=limit
        )
    else:
        if embedder is None:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "语义检索需要一个 embedder（文本 → 向量）。",
                details={"mode": mode},
            )
        hits, scanned = _semantic_hits(
            repo,
            query=clean_query,
            subject=subject,
            category=category,
            limit=limit,
            profile=profile,
            embedder=embedder,
        )

    return SearchOutcome(
        hits=tuple(hits),
        profile_id=profile.id,
        mode=mode,
        subject=subject,
        scope_filter_applied=True,
        scanned=scanned,
    )


def _semantic_hits(
    repo: EmbeddingRepository,
    *,
    query: str,
    subject: ScopeSubject,
    category: str | None,
    limit: int,
    profile: EmbeddingProfile,
    embedder: Any,
) -> tuple[list[SearchHit], int]:
    """语义检索 + 相似度换算。

    **`scan_limit` 取 `max(limit, ...)` 的意义**：SQL 侧的 `LIMIT` 是
    `MAX_SEARCH_SCAN`（防爆上限），服务层截到 `limit`。
    这样"符合条件的行少于 limit"时，`scanned` 报告的是**实际找到的行数** ——
    它是"为什么只返回 2 条"的答案。

    为什么不用 `limit` 直接做 SQL 的 LIMIT：那会让 `scanned` 恒等于 `limit`，
    失去诊断价值（无法区分"库里只有 2 条"与"limit 就是 2"）。
    """
    query_vector = embedder.embed(query)
    if len(query_vector) != profile.dimensions:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            f"查询向量是 {len(query_vector)} 维，而 profile {profile.name} "
            f"声明 {profile.dimensions} 维。",
            details={
                "profileId": str(profile.id),
                "expectedDimensions": profile.dimensions,
                "actualDimensions": len(query_vector),
            },
        )
    rows = repo.search_semantic(
        profile=profile,
        query_vector=query_vector,
        subject=subject,
        category=category,
        limit=limit,
        scan_limit=MAX_SEARCH_SCAN,
    )
    hits: list[SearchHit] = []
    for row in rows[:limit]:
        distance = float(row["distance"])
        hits.append(
            SearchHit(
                chunk_id=row["chunk_id"],
                chunk_no=int(row["chunk_no"]),
                asset_id=row["asset_id"],
                asset_title=str(row["asset_title"]),
                version_id=row["version_id"],
                category=str(row["category"]),
                scope=str(row["scope"]),
                text_content=str(row["text_content"]),
                similarity=distance_to_similarity(profile.metric, distance),
                distance=distance,
            )
        )
    return hits, len(rows)


def _keyword_hits(
    repo: EmbeddingRepository,
    *,
    query: str,
    subject: ScopeSubject,
    category: str | None,
    limit: int,
) -> tuple[list[SearchHit], int]:
    """关键词兜底检索。

    **查询词也要过一遍 `derive_keywords`**（与写 chunk 时同一个派生器）——
    这是本函数最容易写错的一处：若查询侧用自己的切词规则，
    "写进去的关键词"与"查询用的关键词"会落在**不同的词形**上
    （大小写、标点、停用词处理不一致），于是兜底召回**永远返回空**，
    而**不报错**。

    判据：**同一个派生器，两处都用**。
    """
    from rednote.modules.knowledge.chunking import derive_keywords

    keywords = list(derive_keywords(query))
    if not keywords:
        # 查询词全部被停用词/长度规则过滤掉 —— 显式返回空，
        # 并在 `scanned=0` 上体现。不报错：它是"这个查询没有可用的关键词"，
        # 而不是"查询非法"。
        return [], 0
    rows = repo.search_keyword(
        keywords=keywords,
        subject=subject,
        category=category,
        limit=MAX_SEARCH_SCAN,
    )
    hits: list[SearchHit] = []
    for row in rows[:limit]:
        hit_keywords = set(row.get("keywords") or ())
        matched = len(hit_keywords & set(keywords))
        # 相似度 = 命中关键词数 / 查询关键词数（可解释，且与语义模式的
        # "越大越相似"方向一致）。
        similarity = matched / len(keywords)
        hits.append(
            SearchHit(
                chunk_id=row["chunk_id"],
                chunk_no=int(row["chunk_no"]),
                asset_id=row["asset_id"],
                asset_title=str(row["asset_title"]),
                version_id=row["version_id"],
                category=str(row["category"]),
                scope=str(row["scope"]),
                text_content=str(row["text_content"]),
                similarity=similarity,
                distance=-similarity,
            )
        )
    return hits, len(rows)


# ---------------------------------------------------------------------------
# 容量统计（ADR-15 判据 2/3 的落点）
# ---------------------------------------------------------------------------


def capacity_report(
    repo: EmbeddingRepository, *, profile: EmbeddingProfile
) -> dict[str, Any]:
    """按 profile 的容量报告（ADR-15 判据 2 与监控信号的落点）。

    ADR-15 原文：

    > | 计数口径正确 | 检查监控指标按 embedding profile 分别统计 chunk 数，
    >   而非全库合计 |
    > 监控信号：chunk 数逼近 200 万即触发容量复核（对应风险 R8）。

    **为什么报告里同时给 `chunkCount` 与 `embeddingRows`**：
    正常情况下两者相等。不相等说明"有向量行指向同一个 chunk" ——
    唯一约束会拦下重复写入，因此只可能是**数据被绕过约束写入**
    （例如运维脚本），值得被看见。给一个字段专门承载"两者是否一致"，
    比让调用方自己去比两个数可靠。
    """
    from rednote.modules.knowledge.embedding_types import (
        DEFAULT_EMBEDDING_DIMENSIONS,
    )
    from rednote.platform.capacity import (
        PGVECTOR_CHUNK_THRESHOLD,
        estimate_vector_bytes,
        capacity_warning,
    )

    chunk_count = repo.count_chunks_for_profile(profile.id)
    embedding_rows = repo.count_embeddings(profile_id=profile.id)
    by_scope = repo.count_assets_for_profile_scope(profile_id=profile.id)
    estimate = estimate_vector_bytes(
        chunk_count=chunk_count, dimensions=profile.dimensions
    )
    return {
        "profileId": str(profile.id),
        "profileName": profile.name,
        "modelVersion": profile.model_version,
        "dimensions": profile.dimensions,
        "metric": profile.metric,
        "indexReady": profile.index_ready,
        "state": profile.state,
        "chunkCount": chunk_count,
        "embeddingRows": embedding_rows,
        "countsConsistent": chunk_count == embedding_rows,
        "chunksByScope": by_scope,
        "threshold": PGVECTOR_CHUNK_THRESHOLD,
        "warning": capacity_warning(chunk_count),
        "estimate": estimate,
        # 显式回报"估算用的是哪个维度" —— 若 profile 维度与规划值不同，
        # 估算的可比性需要被看见。
        "plannedDimensions": DEFAULT_EMBEDDING_DIMENSIONS,
    }


__all__ = [
    "DEFAULT_HNSW_EF_CONSTRUCTION",
    "DEFAULT_HNSW_M",
    "IngestionResult",
    "build_index",
    "capacity_report",
    "create_profile",
    "ingest_version",
    "resolve_profile",
    "search",
    "switch_profile",
]
