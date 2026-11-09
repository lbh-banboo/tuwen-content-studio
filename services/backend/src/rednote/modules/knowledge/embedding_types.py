"""WP21 的不可变值类型与受控取值域（检索域）。

**为什么与 `types.py` 分开**：`types.py` 是 WP20 的**知识域**取值
（六类 category / 四个 scope / 发布动作 / 版本状态），而本模块是
**检索域**取值（embedding profile 状态 / 距离度量 / 检索模式 / 范围主体）。
两者的消费者不同：`types.py` 被 `schema.py` / `projection.py` / `models.py`
使用；本模块被 `chunking.py` / `scope_filter.py` / `embedding_models.py` 使用。
合在一起会让 WP20 的五个文件被迫 import 检索域的常量。

分层与 WP20 保持同构（单向）：

    embedding_types ← chunking
    embedding_types ← scope_filter
    embedding_types ← embedding_models
    embedding_types ← embedding_repository
    embedding_types ← embedding_service
    embedding_types ← indexing
    embedding_types ← search_api

---

## 本包的三条"不许省略"的判据（验收项的结构性落点）

### 1. `scope` 四值 → 可见性谓词（验收原文「近似品牌案例不串事实」）

WP20 已把范围**落成数据**（`knowledge_assets.scope` + 三个限定列 +
`FACT` 必须有具体范围的 CHECK）。但**数据层有范围 ≠ 检索时按范围过滤** ——
WP20 交付后，范围只是一组**没人读的列**。本模块的
`SCOPE_VISIBILITY_COLUMN` 是让范围在召回路径上生效的**唯一来源**：

| scope | 可见性判据 | 限定列 |
|---|---|---|
| `GLOBAL` | 恒可见 | 无（且 CHECK 保证三个限定列全 NULL） |
| `BRAND` | `brand_id = subject.brand_id` | `brand_id` |
| `PRODUCT` | `product_id = subject.product_id` | `product_id` |
| `PROJECT` | `project_id = subject.project_id` | `project_id` |

**为什么必须是"唯一来源"**：若检索路径与列表路径各写一套映射，
"按品牌过滤"在两条路径上会给出不同结果 —— 而**不会有任何测试发现**，
因为两条路径各自都自洽。这与 WP15 的 ISS-47（复用语义不匹配的抽象）
同族：**同一个事实有两处实现，它们迟早不一致**。

### 2. embedding profile 的 `model_version` 与 `dimensions` 必须**双项**绑定

ADR-15 的硬约束原文：

> **换模型 / 换版本时，必须创建新的索引空间（新 profile），
> 禁止在旧索引空间中混入新模型产生的向量。**

**实测发现（本包的关键结论）**：仅校验 `dimensions` **拦不住最危险的那一类** ——
两个模型版本可以维度相同（例如 `bge-m3@v1` 与 `bge-m3@v2` 都是 1024 维），
此时维度守卫无感，新模型的向量会静默写进旧 profile 的索引空间。

ADR-15 原文对此的描述是：

> 混用的危害不是报错而是**静默劣化**：向量空间不同，距离比较失去意义，
> 检索会返回语义无关的结果，表现为"AI 答非所问"而非"系统故障"。

**"维度相同的混用连报错都没有"** —— 所以守卫必须同时比较
`model_version` 与 `dimensions` 两项。见 `embedding_ddl.py`。

### 3. `chunk_embeddings.model_version` 是一列**冗余**

它可以从 `profile_id` 推出（`embedding_profiles.model_version`）。
冗余的理由是**守卫需要一个可比较的列**：

若只比 profile 自己的值（`NEW.profile_id` → 查 profile → 比较 profile 的
`model_version` 与 profile 的 `model_version`），那是**恒真**的 ——
判据退化成一个永远为真的断言。ISS-75 的原话是：

> 判据不能建立在数据库已保证的事上

冗余这一列，是为了让 `NEW.model_version`（**调用方声称这个向量是哪个模型产的**）
与 `profile.model_version`（**这个索引空间绑定的模型**）成为两个**独立**的值。
只有当它们是两个独立的值时，比较才有意义。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

# ---------------------------------------------------------------------------
# embedding profile 状态（ADR-15 的「切换流程」需要一个状态机）
# ---------------------------------------------------------------------------
# ADR-15 的切换流程原文（5 步）：
#   1. 新 profile 建立独立索引空间；
#   2. 全量重算（或按需回填）新 profile 的向量；
#   3. 检索按 profile 路由，**切换期间旧 profile 保持可用**；
#   4. 新 profile 检索质量经回归确认后，将流量切至新 profile；
#   5. 旧 profile 保留至确认无需回滚，再回收。
#
# 第 3 步是状态机存在的理由：「切换期间旧 profile 保持可用」意味着
# **两个 profile 同时处于可用状态**。因此状态不是布尔（启用/停用），
# 而是"谁是默认"：
#
#   ACTIVE      —— 当前默认检索目标（同一时刻**至多一个**）
#   DEPRECATED  —— 仍可被显式检索，但不再是默认（切换期 / 待回收）
#
# **为什么不用 `is_default bool`**：布尔无法表达"待回收"这一档。
# 一个已被取代、但仍保留用于回滚的 profile，与一个从未启用过的 profile
# 在布尔下长得一样，而它们的运维含义完全不同（前者不能删）。

PROFILE_STATE_ACTIVE: Final[str] = "ACTIVE"
PROFILE_STATE_DEPRECATED: Final[str] = "DEPRECATED"

#: **profile 状态的唯一权威列表**。
PROFILE_STATES: Final[tuple[str, ...]] = (
    PROFILE_STATE_ACTIVE,
    PROFILE_STATE_DEPRECATED,
)
PROFILE_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(PROFILE_STATES)
)

# ---------------------------------------------------------------------------
# 距离度量（pgvector 的运算符类）
# ---------------------------------------------------------------------------
# pgvector 0.8 的三种可用度量与它们的运算符：
#
#   COSINE      <=>   vector_cosine_ops    余弦距离（1 - 余弦相似度）
#   L2          <->   vector_l2_ops        欧氏距离
#   INNER_PRODUCT <#> vector_ip_ops        负内积（越大越相似）
#
# **为什么默认 COSINE 而不是 L2**：文本 embedding 的**模长不承载语义**
# （它随文本长度与 token 频率变化），余弦距离对模长归一化，是文本检索的
# 通行选择。L2 在本场景下会把"长文本"与"短文本"拉开距离 ——
# 那不是语义差异。
#
# **为什么度量必须是 profile 的属性而不是全局常量**：换模型时度量可能变
# （例如从归一化的模型换到未归一化的）。度量写死在代码里会让"换模型"
# 与"换度量"脱钩 —— 而它们其实是一件事（都是"换索引空间"）。

METRIC_COSINE: Final[str] = "COSINE"
METRIC_L2: Final[str] = "L2"
METRIC_INNER_PRODUCT: Final[str] = "INNER_PRODUCT"

METRICS: Final[tuple[str, ...]] = (METRIC_COSINE, METRIC_L2, METRIC_INNER_PRODUCT)
METRIC_LIST_SQL: Final[str] = ", ".join(f"'{name}'" for name in sorted(METRICS))

#: 度量 → pgvector 的运算符类名（建索引用）。
#: **单一来源**：DDL 生成与查询构造都从这里取，避免"索引建的是 cosine、
#: 查询用的是 L2"这种**索引建了但查询用不上**的静默退化。
METRIC_INDEX_OPS: Final[dict[str, str]] = {
    METRIC_COSINE: "vector_cosine_ops",
    METRIC_L2: "vector_l2_ops",
    METRIC_INNER_PRODUCT: "vector_ip_ops",
}

#: 度量 → pgvector 的距离运算符（查询排序用）。
METRIC_DISTANCE_OPERATOR: Final[dict[str, str]] = {
    METRIC_COSINE: "<=>",
    METRIC_L2: "<->",
    METRIC_INNER_PRODUCT: "<#>",
}

#: 度量 → 距离到相似度的换算（Python 侧）。
#:
#: `<=>` 与 `<->` 返回**距离**（越小越相似），`<#>` 返回**负内积**。
#: 对外统一暴露"相似度"（越大越相似），因此需要显式换算。
#:
#: **为什么在 Python 侧换算而不是在 SQL 里算**：`ORDER BY` 的键必须是
#: 索引的表达式（`embedding <=> :q`），若在 SQL 里再套一层
#: `1 - (...)` 用于排序，**索引完全用不上**（表达式不是索引的键）。
#: 因此 SQL 只负责按距离排序，换算在 Python 侧做 ——
#: 换算公式在这里给出**唯一来源**，不在服务层重写一份。
#:
#: L2 用 `1 / (1 + d)` 而不是 `-d`：欧氏距离无上界，
#: 直接取负会让相似度分布极不均匀（大距离的差异被压缩）。
#: `1 / (1 + d)` 把它压进 `(0, 1]`，且单调（保持排序）。
def distance_to_similarity(metric: str, distance: float) -> float:
    """把 pgvector 的"距离"换算成"相似度"（越大越相似）。

    | 度量 | pgvector 返回 | 换算 | 取值范围 |
    |---|---|---|---|
    | `COSINE` | 余弦距离（`1 - 余弦相似度`） | `1 - d` | `[0, 2]`（同向 0，反向 2） |
    | `L2` | 欧氏距离 | `1 / (1 + d)` | `(0, 1]` |
    | `INNER_PRODUCT` | 负内积 | `-d` | 无界（越大越相似） |

    **`COSINE` 的取值范围是 `[0, 2]` 而不是 `[0, 1]`** —— 这是必须
    诚实写出来的：向量完全反向时余弦距离是 2。调用方若按
    "相似度 ≤ 1"设阈值，在反向向量上会判错。
    （embedding 的实际输出极少反向，但"极少"不是"不会"。）
    """
    if metric == METRIC_COSINE:
        return 1.0 - distance
    if metric == METRIC_L2:
        return 1.0 / (1.0 + distance)
    if metric == METRIC_INNER_PRODUCT:
        return -distance
    raise ValueError(
        f"未知的距离度量 {metric!r}；已知：{sorted(METRICS)}。"
        "不猜一个换算公式 —— 猜错会让相似度排序与距离排序不一致。"
    )

# ---------------------------------------------------------------------------
# 检索模式
# ---------------------------------------------------------------------------
# 验收原文「**近似**品牌案例不串事实」的第一个词是"近似" ——
# 它指的是**语义相近**（向量距离），而不是关键词匹配。
#
# 但 `knowledge_chunks` 有一列 `keywords text[]`（WP20 建的，规格源 02 §8
# 原文「供关键词召回兜底」）。规格源明写它是**兜底**，因此两种模式都要有，
# 且默认是语义。

SEARCH_SEMANTIC: Final[str] = "SEMANTIC"
SEARCH_KEYWORD: Final[str] = "KEYWORD"

SEARCH_MODES: Final[tuple[str, ...]] = (SEARCH_SEMANTIC, SEARCH_KEYWORD)
SEARCH_MODE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(SEARCH_MODES)
)

DEFAULT_SEARCH_MODE: Final[str] = SEARCH_SEMANTIC

# ---------------------------------------------------------------------------
# 数值边界
# ---------------------------------------------------------------------------

#: 检索返回条数。上界取 100 与 `platform.pagination.MAX_LIMIT` 一致 ——
#: 检索不是分页（它是一次"取最相近的 k 条"），但同一个上界避免前端
#: 把两个数混用。
MIN_SEARCH_LIMIT: Final[int] = 1
MAX_SEARCH_LIMIT: Final[int] = 100
DEFAULT_SEARCH_LIMIT: Final[int] = 8

#: HNSW 的 `ef_search`（查询期精度/召回权衡）。pgvector 默认 40。
#: **显式设置而不是吃默认值**：默认值是全局的，而"检索质量"是
#: 每 profile 的事（ADR-15 判据 7 要求每次换模型做质量回归）。
DEFAULT_HNSW_EF_SEARCH: Final[int] = 40
MIN_HNSW_EF_SEARCH: Final[int] = 1
MAX_HNSW_EF_SEARCH: Final[int] = 1000

#: 向量维度。ADR-15 按 1024 维 float32 规划（`[待核实: OP-44]`）。
#: **它只是默认值，不是约束** —— 真实维度由 `embedding_profiles.dimensions`
#: 决定（OP-44 未定，本包不把它写死进列类型，见 §0.3 实测）。
MIN_EMBEDDING_DIMENSIONS: Final[int] = 1
MAX_EMBEDDING_DIMENSIONS: Final[int] = 4096
DEFAULT_EMBEDDING_DIMENSIONS: Final[int] = 1024

#: 切块参数（见 `chunking.py`）。这些是**切块器版本的一部分** ——
#: 改它们必须同时改 `CHUNKER_VERSION`。
DEFAULT_CHUNK_CHARS: Final[int] = 500
DEFAULT_CHUNK_OVERLAP_CHARS: Final[int] = 80
MIN_CHUNK_CHARS: Final[int] = 50
MAX_CHUNK_CHARS: Final[int] = 4000

#: 一次检索最多扫描的候选行数（防爆上限）。
#: **它不是分页参数** —— 与 WP23 的 `_FULL_SCAN_LIMIT` 同一手法：
#: 它是"防止一条查询把整表拉进内存"的护栏，不参与"翻页"语义。
MAX_SEARCH_SCAN: Final[int] = 10_000


# ---------------------------------------------------------------------------
# 值类型（frozen dataclass，跨层传递用 —— 不用 ORM 对象）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeSubject:
    """检索的**范围主体**：这次检索是"替谁查"。

    三个字段都可空，但**语义不是"可选"**：缺哪个字段，就意味着
    "这次检索没有该维度的可见性主张"。后果是显式且可诊断的：

      · 缺 `brand_id`  → 看不到任何 `scope='BRAND'` 的知识
      · 缺 `product_id`→ 看不到任何 `scope='PRODUCT'` 的知识
      · 缺 `project_id`→ 看不到任何 `scope='PROJECT'` 的知识

    **为什么不做"缺字段就不过滤该维度"**：那会让"忘记传 productId"
    变成"看到全部产品的事实" —— 一个**越权**的默认值。
    "缺 = 看不见"是安全的默认方向（fail-closed）。

    `GLOBAL` 范围的知识**永远可见**（它不需要任何限定列，
    WP20 的 CHECK 保证它不带限定列）。
    """

    brand_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None

    def is_empty(self) -> bool:
        """三个维度全缺。此时只有 `GLOBAL` 的知识可见。"""
        return (
            self.brand_id is None
            and self.product_id is None
            and self.project_id is None
        )

    def as_dict(self) -> dict[str, str | None]:
        """对外表示（响应里报告用了哪个 subject —— 让"为什么没召回"可诊断）。"""
        return {
            "brandId": str(self.brand_id) if self.brand_id else None,
            "productId": str(self.product_id) if self.product_id else None,
            "projectId": str(self.project_id) if self.project_id else None,
        }


@dataclass(frozen=True)
class ChunkDraft:
    """切块器产出的一个块（**纯数据，不碰数据库**）。

    `locator` 的形状由本包定义（WP20 只要求它是 `jsonb object`，
    `models.py` L491 原文：「形状由 WP21 的切块器决定」）：

        {
          "start": 0,          # 在源文本中的起始字符偏移
          "end": 500,          # 结束字符偏移（不含）
          "unit": "CHAR",      # "CHAR"（正文）或 "FIELD"（结构化字段）
          "path": "productName"  # 仅 unit="FIELD" 时有
        }

    **为什么 `unit` 是必要的**：`body` 的切块有字符位置，
    而 `structured_payload` 的切块**没有**（它是字段路径）。
    把两者塞进同一个形状而不区分，会让"回指原文位置"这件事
    在结构化字段上变成一个假的偏移量。
    """

    chunk_no: int
    text_content: str
    text_hash: str
    token_count: int
    locator: dict[str, Any]
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchHit:
    """一条检索结果（服务层 → 端点）。"""

    chunk_id: uuid.UUID
    chunk_no: int
    asset_id: uuid.UUID
    asset_title: str
    version_id: uuid.UUID
    category: str
    scope: str
    text_content: str
    similarity: float
    distance: float


@dataclass(frozen=True)
class SearchOutcome:
    """检索的完整结论（含**可诊断的过滤信息**）。

    **为什么要把 `scope_filter_applied` 与 `subject` 带回给调用方**：
    风险 R5 —— 一条 `PRODUCT` 范围的事实，在调用方没传 `productId` 时
    **永远召不回**。若响应里只有一个空数组，调用方无法区分：

      · "库里真的没有相关知识"
      · "有，但被范围过滤掉了"

    这两种情况对调用方的后续动作完全不同（前者该去补知识，
    后者该去检查自己传的 subject）。把过滤事实显式回报，
    是让"为什么没召回"可诊断的最低成本做法。
    """

    hits: tuple[SearchHit, ...]
    profile_id: uuid.UUID
    mode: str
    subject: ScopeSubject
    scope_filter_applied: bool
    scanned: int


#: 本模块的对外符号（与其余知识模块的同构写法保持一致）。
#:
#: **为什么补上它**：本文件原先**没有** `__all__` —— 它是本模块目录里
#: 唯一缺这个声明的文件。后果在阶段 7 扫死代码时暴露：
#: `EmbeddingProfileDraft` 是一个**零引用的值类型**（服务层建 profile
#: 用的是 9 个关键字参数，从未采用它），而"它是不是对外接口的一部分"
#: 没有代码可以回答 —— 只能靠全仓 grep 猜。
#: `__all__` 让"这个模块承诺了什么"变成**可读且可机械核对**的事实。
__all__ = [
    "ChunkDraft",
    "DEFAULT_CHUNK_CHARS",
    "DEFAULT_CHUNK_OVERLAP_CHARS",
    "DEFAULT_EMBEDDING_DIMENSIONS",
    "DEFAULT_HNSW_EF_SEARCH",
    "DEFAULT_SEARCH_LIMIT",
    "DEFAULT_SEARCH_MODE",
    "MAX_CHUNK_CHARS",
    "MAX_EMBEDDING_DIMENSIONS",
    "MAX_HNSW_EF_SEARCH",
    "MAX_SEARCH_LIMIT",
    "MAX_SEARCH_SCAN",
    "METRICS",
    "METRIC_COSINE",
    "METRIC_DISTANCE_OPERATOR",
    "METRIC_INDEX_OPS",
    "METRIC_INNER_PRODUCT",
    "METRIC_L2",
    "METRIC_LIST_SQL",
    "MIN_CHUNK_CHARS",
    "MIN_EMBEDDING_DIMENSIONS",
    "MIN_HNSW_EF_SEARCH",
    "MIN_SEARCH_LIMIT",
    "PROFILE_STATES",
    "PROFILE_STATE_ACTIVE",
    "PROFILE_STATE_DEPRECATED",
    "PROFILE_STATE_LIST_SQL",
    "SEARCH_KEYWORD",
    "SEARCH_MODES",
    "SEARCH_MODE_LIST_SQL",
    "SEARCH_SEMANTIC",
    "ScopeSubject",
    "SearchHit",
    "SearchOutcome",
    "distance_to_similarity",
]
