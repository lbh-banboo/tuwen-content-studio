"""知识 CRUD 与版本（WP20）。

**对外接口**：

| 用途 | 位置 |
|---|---|
| 端点 | `api.router`（九个，协议 03 §9 的 10 行减去 `POST /knowledge/searches`） |
| 业务规则 | `service`（**唯一对外入口**） |
| 数据库级保证 | `ddl.all_trigger_ddl` / `ddl.trigger_names` |
| 六类字段 Schema | `schema.CATEGORY_FIELDS` / `schema.field_spec` |
| 版本状态投影（纯函数） | `projection.project_versions` |

**五张表**（数据结构 02 §8）：

| 表 | 性质 |
|---|---|
| `knowledge_assets` | 一条逻辑知识（`active_version_id` 是**投影列**），可变 |
| `knowledge_versions` | 知识版本，**不可变**（`id` 同时 FK `version_nodes.id`） |
| `knowledge_publications` | 发布事件，**不可变**（规格源原文的"生效事件"） |
| `knowledge_chunks` | 切块结果，**本包只建表不生产**（生产归 WP21） |
| `knowledge_proposals` | AI 候选，可变（`state` / `confirmed_by`） |

**分层**（单向）：

    types ← schema ← projection
    types ← models → platform.immutability
    types ← repository → models
    types ← service → repository / schema / projection / versions（WP11 只读接缝）
    api → service / dependencies

显式 import 模型与 router，使 `migrations/env.py` 的 autogenerate
与 `main.py` 的装配都能看见它们。

---

## 本包的三条硬约束（违反即返工）

1. **"禁止无结构散文替代事实字段"必须三层落地** ——
   规格源 02 §8 的备注。三层：`schema.py` 的六类必填字段 /
   `models.py` 的 `structured_payload <> '{}'` CHECK /
   `service.validate_draft` 的 422 带 `missingFields`。
   只做服务层不够：导入脚本与数据回填都绕过它。

2. **状态是从事件流算出来的，不是一列** —— 规格源 02 §12
   「恢复通过新事件，不篡改旧状态历史」。落点是
   `projection.py` 是**纯函数**（可被构造的事件序列单测）。
   验收原文「归档可追溯」依赖这一点：若状态是一列，
   RESTORE 会让"曾经归档过"这件事从状态里消失。

3. **OP-88 的 `KNOWLEDGE` 那一半在本包闭合** ——
   `ddl.py` 的正向（子表 → 节点）与反向（节点 → 子表）两个延迟触发器，
   反向那个挂在**共用表 `version_nodes`** 上并用 `WHEN` 收窄到
   `entity_type = 'KNOWLEDGE'`（与 WP18 的 `BRIEF` 那个并存，互不干扰）。

## 接缝调用不得改签名

`versions.service.register_version(entity_type=ENTITY_TYPE_KNOWLEDGE, …)`、
`versions.repository.VersionRepository`（`savepoint` / `get_node` 等）、
`projects.dependencies.require_org_action` —— 全部**原样调用**。

---

# WP21：切块 · Embedding · 范围过滤 · 检索

> 以下内容由 **WP21** 追加。它在本模块目录内**新增文件**，
> **不改 WP20 的任何既有文件**（接缝冻结 §2 的"分支只写
> `modules/<自己模块>/**`" —— 同模块内扩展是允许的）。

**对外接口**：

| 用途 | 位置 |
|---|---|
| 端点 | `search_api.router`（**1 个**：`POST /knowledge/searches`，协议 §9 第 8 行） |
| 业务规则 | `embedding_service`（**唯一对外入口**） |
| 切块器（纯函数） | `chunking.chunk_for_version` / `CHUNKER_VERSION` |
| 范围谓词（纯函数） | `scope_filter.scope_visibility_predicate`（**验收原文的落点**） |
| 数据库级保证 | `embedding_ddl.all_trigger_ddl` / `hnsw_index_ddl` |
| Outbox 消费者 | `indexing.handle_index_event`（**闭合 OP-149**） |
| 容量估算（platform） | `rednote.platform.capacity`（ADR-15 的度量口径） |

**两张表**：

| 表 | 性质 |
|---|---|
| `embedding_profiles` | **配置**。一个 embedding 模型 + 维度 + 度量的绑定（ADR-15 的「索引空间」），`state` 可变 |
| `chunk_embeddings` | **物化结果**。一个 chunk 在一个 profile 下的向量，可重建 |

**分层**（单向）：

    embedding_types ← chunking
    embedding_types ← scope_filter → types（WP20 的 scope 取值）
    embedding_types ← embedding_models → platform.base
    embedding_types ← embedding_repository → embedding_models / chunking / scope_filter
    embedding_types ← embedding_service → embedding_repository / chunking / platform.capacity
    indexing → embedding_service / embedding_repository / outbox（WP13 只读）
    search_api → embedding_service / embedding_repository / indexing

## 本包的四条硬约束（违反即返工）

1. **范围谓词必须下推到 SQL，且是唯一来源** ——
   验收原文「近似品牌案例**不串事实**」的落点。
   取回来再筛会让 HNSW 的 `LIMIT k` 在**过滤前**生效
   （同品牌不足 k 条时返回跨品牌的），而**看起来正常**。
   检索路径与（未来的）列表路径必须**共用** `scope_filter`。

2. **混用守卫必须双项校验**（`model_version` **与** `dimensions`）——
   ADR-15 硬约束。**实测：仅比维度拦不住同维度混用**
   （两个模型版本可以都是 1024 维），而那是**完全静默**的
   （ADR-15 原文："混用的危害不是报错而是静默劣化"）。

3. **向量列是**无维度** `vector`，索引按 profile 分别建** ——
   实测：无维度列建不了 HNSW（`column does not have dimensions`），
   因此索引必须写成 `((embedding::vector(<dims>)) <ops>) WHERE profile_id = ...`。
   维度是**数据**（`embedding_profiles.dimensions`），不是 schema。

4. **`ingest_version` 必须幂等** —— 它先删该版本既有 chunk
   （向量随 `ON DELETE CASCADE` 一起删）再重写。
   事件会被重试，而"重试产生第二批 chunk"会撞唯一约束 ——
   那让"重试"从正常路径变成异常路径。

## 本包闭合的两个 OP

| OP | 内容 | 闭合判据 |
|---|---|---|
| **OP-149** | `INDEXING` 状态的 chunk 生产者不存在 → WP20 交付后所有已发布知识停在 `INDEXING` | 有代码路径（`ingest_version`）+ 有测试 + **有落库行**（证据脚本回查） |
| **OP-146** | `POST /knowledge/searches` 未实现（协议 §9 第 8 行） | 端点 + 契约含该路径 |
"""

from rednote.modules.knowledge import models  # noqa: F401
from rednote.modules.knowledge.api import router  # noqa: F401
from rednote.modules.knowledge.embedding_models import (  # noqa: F401
    ChunkEmbedding,
    EmbeddingProfile,
)
from rednote.modules.knowledge.search_api import router as search_router  # noqa: F401

#: **两个 router，刻意不合并**。
#:
#: WP20 的 `api.router` 挂 9 个知识端点（`/knowledge/assets*`、
#: `/knowledge/proposals*`）；WP21 的 `search_api.router` 挂 1 个
#: 检索端点（`/knowledge/searches`）。合并成一个 router 会让
#: "这个端点属于哪个包"从代码结构里消失 —— 而排障与划界都要回答它。
#:
#: `main.py` 分别 `include_router` 两者（各自 `prefix=api_prefix`）。
__all__: list[str] = ["router", "search_router"]