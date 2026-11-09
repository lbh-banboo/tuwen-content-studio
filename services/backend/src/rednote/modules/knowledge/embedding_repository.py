"""检索两张表的存储访问（WP21）。

**本层职责边界**：只做存储访问，不含业务规则判定。
"这个 profile 能不能建"、"这个向量该不该写"的判定在 `embedding_service.py`，
但**判定所需的数据必须由本层在同一事务内读出**。

**本层特有的四条约定**：

1. **向量的读写一律走裸 SQL + 绑定参数**。
   `pgvector.sqlalchemy.Vector` 支持 ORM 读写，但本包的**检索查询**
   必须写 `ORDER BY embedding <=> :q` 并带 `LIMIT` ——
   而"排序表达式 + 距离运算符 + 相似度换算"三者是**同一条 SQL 的
   三个部分**，用 ORM 拼会把它拆成三段（易漂移）。
   本层把这条查询写成一个方法，运算符与索引 ops 都从
   `METRIC_DISTANCE_OPERATOR` 取（**单一来源**）。

2. **向量参数用 `CAST(:vec AS vector)`**。
   psycopg 不认识 `vector` 类型，直接传 Python list 会被当成数组。
   本层把 list 渲染成 pgvector 的字面量字符串 `'[0.1,0.2,...]'`
   再 `CAST(:vec AS vector)` —— 与 WP17 的
   "JSONB 字面量一律 `cast(:x as jsonb)`"同一手法（本项目高频坑）。

3. **`SET LOCAL hnsw.ef_search`** 在检索事务内设置。
   它是**查询期**参数（不需要重建索引），因此不进 profile 的身份，
   但必须来自 profile 那一行（ADR-15 判据 7 的质量回归要能复现）。

4. **不提供 `chunk_embeddings` 的 update 方法**。
   向量的生命周期是"写 / 删"，没有"改一个向量"这个操作 ——
   改就是重算（删了重写）。给一个 update 入口会诱导出
   "就地更新向量"的写法，而那是**混用**的另一种形态。

**分层**（单向，与 WP20 同构）：

    embedding_types ← embedding_models → platform.base
    embedding_types ← embedding_repository → embedding_models / chunking / scope_filter
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import Select, func, select, text
from sqlalchemy.orm import Session, aliased

from rednote.modules.knowledge.chunking import ChunkDraft
from rednote.modules.knowledge.embedding_models import (
    ChunkEmbedding,
    EmbeddingProfile,
)
from rednote.modules.knowledge.embedding_types import (
    METRIC_DISTANCE_OPERATOR,
    PROFILE_STATE_ACTIVE,
    ScopeSubject,
)
from rednote.modules.knowledge.models import (
    KnowledgeAsset,
    KnowledgeChunk,
    KnowledgeVersion,
)
from rednote.modules.knowledge.scope_filter import scope_visibility_predicate

#: 一次 INSERT 里最多几行。**分块插入**而不是逐行：
#: 一条知识可能切成上千块，逐行 INSERT 是上千次往返。
#: 分块而非一次性大 INSERT 的理由：一次性插入会让单条 SQL 的
#: 参数个数爆掉（PG 的上限是 65535 个绑定参数，
#: 每行 8 列 → 8000 行就到顶）。
_INSERT_BATCH_ROWS: Final[int] = 500

#: 裸 SQL 里 `knowledge_assets` 的别名（两条检索查询都用它）。
#:
#: **为什么必须是一个共享常量而不是各写一处字面量**：
#: 范围谓词是**编译出来**的 SQL 片段（`knowledge_assets.scope = ...`），
#: 而查询体里的表名是**手写**的别名（`JOIN knowledge_assets a`）。
#: 两处一旦不一致，PG 报 `UndefinedTable: invalid reference to FROM-clause
#: entry for table "knowledge_assets"` —— 实测踩到过。
#: 用同一个常量拼出别名，让"改一处必须改另一处"变成"改一处就够"。
_ASSET_ALIAS: Final[str] = "a"

#: 每行 8 个绑定参数。
_PARAMS_PER_ROW: Final[int] = 8


def vector_literal(values: list[float]) -> str:
    """把 Python list 渲染成 pgvector 的字面量。

    格式：`[0.1,0.2,0.3]`（**无空格**，pgvector 接受带空格的形式但
    无空格更短；大量向量下字符串长度会进入网络开销）。

    **为什么不用 repr(list)**：`repr([0.1])` 是 `'[0.1]'`（Python 的
    float repr 与 pgvector 的解析器都接受），但 `repr` 对 `float('inf')`
    会给出 `'inf'` —— 而 pgvector 期望 `'Infinity'`。
    本函数显式处理三个特殊值。

    **为什么不让 psycopg 直接传 list**：psycopg 会把 list 当成
    PostgreSQL 数组（`float8[]`），而 `embedding` 列是 `vector` ——
    类型不匹配会报错。必须传字符串 + `CAST(... AS vector)`。
    """
    parts: list[str] = []
    for value in values:
        if value != value:  # NaN
            raise ValueError("向量里含 NaN —— pgvector 不接受它。")
        if value == float("inf"):
            parts.append("Infinity")
        elif value == float("-inf"):
            parts.append("-Infinity")
        else:
            parts.append(repr(float(value)))
    return "[" + ",".join(parts) + "]"


class EmbeddingRepository:
    """检索两张表的读写入口。事务边界由调用方控制。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _scope_predicate_sql(self, subject: ScopeSubject) -> str:
        """把范围谓词编译成可嵌进裸 SQL 的片段（**唯一来源**）。

        ### 为什么必须用 `aliased()` 而不是直接用 `KnowledgeAsset`

        谓词编译出来的是 `knowledge_assets.scope = 'GLOBAL' OR ...`
        —— 用的是**表的全名**。而检索查询里那张表被起了别名
        （`JOIN knowledge_assets a`），SQL 里**不存在** `knowledge_assets`
        这个名字，于是 PG 报：

            UndefinedTable: invalid reference to FROM-clause entry
            for table "knowledge_assets"
            HINT: Perhaps you meant to reference the table alias "a".

        **这是纯函数测试发现不了的**：`scope_filter` 的测试只看编译出的
        字符串里有没有 `scope = 'BRAND'`，字符串是对的。
        只有把谓词真的嵌进查询、真的发给 PG 才会暴露。
        本包在真库活性验证时踩到 —— 因此这里用
        `aliased(KnowledgeAsset, _ASSET_ALIAS)` 取列对象，
        让编译出的表名与查询里的别名**天然一致**。

        ### 为什么 `literal_binds=True`

        谓词里没有用户输入（scope 值是受控常量，brand/product/project
        是 UUID），内联绑定是安全的；而 `text()` 的命名参数与谓词内部的
        绑定参数**分属两套命名空间**，混在一起会冲突。
        内联让"谓词是一段纯 SQL 文本"这件事成立。
        """
        asset = aliased(KnowledgeAsset, name=_ASSET_ALIAS)
        predicate = scope_visibility_predicate(
            scope_column=asset.scope,
            brand_column=asset.brand_id,
            product_column=asset.product_id,
            project_column=asset.project_id,
            subject=subject,
        )
        return str(
            predicate.compile(
                compile_kwargs={"literal_binds": True},
                dialect=self._session.bind.dialect,  # type: ignore[union-attr]
            )
        )

    @property
    def session(self) -> Session:
        """本仓库持有的 session（供下游在同一事务内造别的仓库）。

        与 `KnowledgeRepository.session` 同一条理由：检索要一次给出
        chunk + 向量 + 知识 + 版本（分属两张表与 WP20 的四张表）。
        不复用同一个 session 的话，同一个响应里的字段会来自不同事务。
        """
        return self._session

    def flush(self) -> None:
        """把待写变更立刻发给数据库（让约束错误在预期位置抛出）。

        本包最要紧的一处：**混用守卫是 BEFORE 触发器**，
        它在 `flush()` 时执行。不 flush 的话错误会推迟到
        `session_scope` 退出时提交 —— 那时已经离开能给出可读 4xx 的位置
        （WP12/WP13/WP16/WP18/WP20 的共同教训）。
        """
        self._session.flush()

    # ------------------------------------------------------------------
    # embedding_profiles
    # ------------------------------------------------------------------

    def get_profile(self, profile_id: uuid.UUID) -> EmbeddingProfile | None:
        return self._session.get(EmbeddingProfile, profile_id)

    def get_profile_by_name(self, name: str) -> EmbeddingProfile | None:
        return self._session.execute(
            select(EmbeddingProfile).where(EmbeddingProfile.name == name)
        ).scalar_one_or_none()

    def get_active_profile(self) -> EmbeddingProfile | None:
        """取当前默认检索目标。

        **规则：`state='ACTIVE'` 里 `created_at` 最新的那个。**

        为什么不加唯一约束强制"至多一个 ACTIVE"：见
        `embedding_models.EmbeddingProfile` 的 docstring ——
        ADR-15 第 3 步要求切换期旧 profile 仍可用，而"可用"与"是默认"
        是两件事。用唯一约束会强制"先降旧的再升新的"，
        中间出现"没有 ACTIVE profile"的窗口，检索直接失败。

        **为什么按 `created_at DESC, id DESC` 排序**：与 WP20 的
        `list_assets` 同一手法 —— 排序键必须**唯一**（`created_at`
        在 PostgreSQL 里是事务开始时刻，同事务插入的多行值相同），
        因此追加 `id` 兜底。不加的话"最新的那个"是不确定的。
        """
        return self._session.execute(
            select(EmbeddingProfile)
            .where(EmbeddingProfile.state == PROFILE_STATE_ACTIVE)
            .order_by(EmbeddingProfile.created_at.desc(), EmbeddingProfile.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def list_profiles(
        self, *, state: str | None = None
    ) -> list[EmbeddingProfile]:
        stmt: Select[Any] = select(EmbeddingProfile)
        if state is not None:
            stmt = stmt.where(EmbeddingProfile.state == state)
        stmt = stmt.order_by(
            EmbeddingProfile.created_at.desc(), EmbeddingProfile.id.desc()
        )
        return list(self._session.execute(stmt).scalars().all())

    def insert_profile(
        self,
        *,
        name: str,
        model_profile_id: uuid.UUID,
        model_version: str,
        dimensions: int,
        metric: str,
        hnsw_m: int,
        hnsw_ef_construction: int,
        hnsw_ef_search: int,
        state: str,
    ) -> EmbeddingProfile:
        row = EmbeddingProfile(
            name=name,
            model_profile_id=model_profile_id,
            model_version=model_version,
            dimensions=dimensions,
            metric=metric,
            hnsw_m=hnsw_m,
            hnsw_ef_construction=hnsw_ef_construction,
            hnsw_ef_search=hnsw_ef_search,
            state=state,
            index_ready=False,
        )
        self._session.add(row)
        return row

    def set_profile_state(
        self, *, profile_id: uuid.UUID, state: str
    ) -> EmbeddingProfile | None:
        """改 profile 的状态（**裸 SQL + `populate_existing` 读回**）。

        本项目高频坑第 3 条：裸 SQL 改完读回 ORM 对象是陈旧副本。
        这里用 SQLAlchemy 的 `update()`（它仍然不经属性赋值），
        因此同样需要强制刷新。
        """
        from sqlalchemy import update

        self._session.execute(
            update(EmbeddingProfile)
            .where(EmbeddingProfile.id == profile_id)
            .values(state=state)
        )
        return self._session.get(
            EmbeddingProfile, profile_id, populate_existing=True
        )

    def mark_index_ready(
        self, *, profile_id: uuid.UUID, ready: bool
    ) -> EmbeddingProfile | None:
        """标记索引已建 / 已删（与索引 DDL 成对，见 `embedding_ddl` 第二节）。"""
        from sqlalchemy import update

        self._session.execute(
            update(EmbeddingProfile)
            .where(EmbeddingProfile.id == profile_id)
            .values(index_ready=ready)
        )
        return self._session.get(
            EmbeddingProfile, profile_id, populate_existing=True
        )

    def count_profiles(self, *, state: str | None = None) -> int:
        stmt = select(func.count()).select_from(EmbeddingProfile)
        if state is not None:
            stmt = stmt.where(EmbeddingProfile.state == state)
        return int(self._session.execute(stmt).scalar_one())

    # ------------------------------------------------------------------
    # chunk 生产（本包闭合 OP-149 的落点）
    # ------------------------------------------------------------------

    def get_version(self, version_id: uuid.UUID) -> KnowledgeVersion | None:
        return self._session.get(KnowledgeVersion, version_id)

    def count_chunks_for_version(self, version_id: uuid.UUID) -> int:
        """某个版本已有的 chunk 数。

        **它是 `INDEXING` 状态的判据**（WP20 的 `projection.py`：
        "生效版本存在但 `knowledge_chunks` 无行 → `INDEXING`"）。
        本包生产 chunk 之后，这个数从 0 变成 N，
        于是 `INDEXING → ACTIVE` 这条路径**第一次真实可达**（闭合 OP-149）。
        """
        return int(
            self._session.execute(
                select(func.count())
                .select_from(KnowledgeChunk)
                .where(KnowledgeChunk.knowledge_version_id == version_id)
            ).scalar_one()
        )

    def delete_chunks_for_version(self, version_id: uuid.UUID) -> int:
        """删掉某个版本的全部 chunk（**重切块的第一步**）。

        向量随外键 `ON DELETE CASCADE` 一起被删（见
        `embedding_models` 模块 docstring 第四节）—— 因此这里**不需要**
        显式删 `chunk_embeddings`。显式删是多余的，且会引入
        "两处删同一批数据"的不一致风险。

        返回被删的 chunk 行数（供调用方报告"重切掉了多少块"）。
        """
        rows = self._session.execute(
            select(KnowledgeChunk.id).where(
                KnowledgeChunk.knowledge_version_id == version_id
            )
        ).scalars().all()
        if not rows:
            return 0
        from sqlalchemy import delete

        self._session.execute(
            delete(KnowledgeChunk).where(
                KnowledgeChunk.knowledge_version_id == version_id
            )
        )
        return len(rows)

    def insert_chunks(
        self, *, version_id: uuid.UUID, drafts: tuple[ChunkDraft, ...]
    ) -> list[uuid.UUID]:
        """写入 chunk 行，返回它们的 id（**按 `chunk_no` 升序**）。

        **为什么返回 id**：紧接着要为这些 chunk 写向量，而
        `chunk_embeddings.chunk_id` 需要它。返回而不是"再查一次" ——
        再查一次会有"查到别人刚插的行"的可能（同版本并发重切）。

        **一次 `flush()` 拿 id**：`uuid_pk()` 是 Python 侧生成的 UUID
        （见 `platform.base`），因此不需要 flush 就能拿到 id ——
        但 flush 是**必须的**：混用守卫是 BEFORE 触发器，
        它要在写向量之前先把 chunk 行落库（FK 要求）。
        让 `flush()` 在此处显式发生，错误位置就可控。
        """
        rows = [
            KnowledgeChunk(
                knowledge_version_id=version_id,
                chunk_no=draft.chunk_no,
                text_content=draft.text_content,
                text_hash=draft.text_hash,
                token_count=draft.token_count,
                locator=draft.locator,
                keywords=list(draft.keywords),
            )
            for draft in drafts
        ]
        self._session.add_all(rows)
        self.flush()
        return [row.id for row in rows]

    # ------------------------------------------------------------------
    # chunk_embeddings
    # ------------------------------------------------------------------

    def insert_embeddings(
        self,
        *,
        profile: EmbeddingProfile,
        items: list[tuple[uuid.UUID, str, list[float]]],
    ) -> int:
        """批量写入向量。`items` 是 `(chunk_id, chunk_text_hash, vector)`。

        **为什么不用 ORM 的 `add_all`**：`Vector` 类型经 ORM 写入时，
        pgvector 的适配器会把 list 转成它自己的格式 —— 那条路径能用，
        但它与**检索查询**（必须裸 SQL）不一致：一处 ORM、一处裸 SQL，
        两边对"向量怎么进库"的理解会漂移。本层统一用裸 SQL。

        **分块插入**：见 `_INSERT_BATCH_ROWS` 的说明。

        **绑定参数命名带序号**（`:c0, :h0, :v0, :c1, ...`）：
        psycopg 的 `executemany` 也能做，但它的 `executemany` 对
        `CAST(:v AS vector)` 这类表达式是**逐行执行**的（不是真正的
        批量），失去分块的意义。手写多值 INSERT 是真正的单条 SQL。

        **`model_version` 与 `dimensions` 从 profile 取，不调用方传**：
        它们必须与 profile 一致（守卫会拦不一致的），
        让调用方传只会多一个"传错就被拦"的失败面。
        冗余列的值**由本层从 profile 派生** —— 守卫校验的是
        "落库的值与 profile 一致"，因此派生点必须唯一。
        """
        if not items:
            return 0
        total = 0
        for start in range(0, len(items), _INSERT_BATCH_ROWS):
            batch = items[start : start + _INSERT_BATCH_ROWS]
            params: dict[str, Any] = {
                "p": profile.id,
                "mv": profile.model_version,
                "d": profile.dimensions,
            }
            for index, (chunk_id, text_hash, vector) in enumerate(batch):
                params[f"c{index}"] = chunk_id
                params[f"v{index}"] = vector_literal(vector)
                params[f"h{index}"] = text_hash
            # 每行 8 列，但 `id` 用 `gen_random_uuid()`（服务端生成）
            # 且 `created_at` 用 `now()` —— 因此只有 6 个绑定参数。
            values_sql = ", ".join(
                f"(gen_random_uuid(), :c{index}, :p, :mv,"
                f" CAST(:v{index} AS vector), :h{index}, :d, now())"
                for index in range(len(batch))
            )
            self._session.execute(
                text(
                    "INSERT INTO chunk_embeddings"
                    " (id, chunk_id, profile_id, model_version, embedding,"
                    "  chunk_text_hash, dimensions, created_at)"
                    f" VALUES {values_sql}"
                ),
                params,
            )
            total += len(batch)
        return total

    def delete_embeddings_for_profile(self, profile_id: uuid.UUID) -> int:
        """删掉某个 profile 的全部向量（回收 profile 前的一步）。"""
        from sqlalchemy import delete

        result = self._session.execute(
            delete(ChunkEmbedding).where(ChunkEmbedding.profile_id == profile_id)
        )
        return int(result.rowcount or 0)

    def count_embeddings(self, *, profile_id: uuid.UUID | None = None) -> int:
        """向量条数。

        **ADR-15 判据 2（计数口径正确）的落点**：它按 `profile_id`
        分别统计，而不是全库合计。ADR-15 原文：

        > | 计数口径正确 | 检查监控指标按 embedding profile 分别统计 chunk 数，
        >   而非全库合计 |

        **为什么必须按 profile 计**：向量检索在单 profile 内进行，
        跨 profile 不比较距离，因此规模压力天然按 profile 计。
        全库合计会误判（多个小 profile 合计超阈值但各自远未超限）。
        """
        stmt = select(func.count()).select_from(ChunkEmbedding)
        if profile_id is not None:
            stmt = stmt.where(ChunkEmbedding.profile_id == profile_id)
        return int(self._session.execute(stmt).scalar_one())

    def count_chunks_for_profile(self, profile_id: uuid.UUID) -> int:
        """某个 profile 下的 **chunk 数**（ADR-15 的阈值计数对象）。

        与 `count_embeddings` 的区别：那条数**向量行**，这条数
        **chunk 数**（通过 `chunk_embeddings` 去重）。
        正常情况下两者相等（`UQ(chunk_id, profile_id)`），
        但"向量行数 != chunk 数"本身就是需要被发现的状态
        （例如同一个 chunk 被写了两次到同一 profile —— 唯一约束会拦，
        因此只可能是"数据被绕过约束写入"，值得报告）。

        ADR-15 的 200 万阈值计数对象是**这条**（它数 chunk），
        不是向量行数 —— 虽然当前两者相等，但口径必须写清楚。
        """
        return int(
            self._session.execute(
                select(func.count(func.distinct(ChunkEmbedding.chunk_id))).where(
                    ChunkEmbedding.profile_id == profile_id
                )
            ).scalar_one()
        )

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search_semantic(
        self,
        *,
        profile: EmbeddingProfile,
        query_vector: list[float],
        subject: ScopeSubject,
        category: str | None,
        limit: int,
        scan_limit: int,
    ) -> list[dict[str, Any]]:
        """语义检索（**范围谓词下推 SQL**，验收原文的落点）。

        返回 `[{chunk_id, chunk_no, version_id, asset_id, asset_title,
        category, scope, text_content, distance}, …]`（按距离升序）。

        ### SQL 的结构

        ```sql
        SELECT ... , (e.embedding::vector(<dims>) <op> CAST(:q AS vector)) AS distance
          FROM chunk_embeddings e
          JOIN knowledge_chunks  c ON c.id = e.chunk_id
          JOIN knowledge_versions v ON v.id = c.knowledge_version_id
          JOIN knowledge_assets   a ON a.id = v.asset_id
         WHERE e.profile_id = :profile
           AND a.active_version_id = v.id      -- 只召回当前生效版本
           AND a.archived_at IS NULL            -- 归档知识不召回
           AND <范围谓词>                        -- 验收原文
         ORDER BY distance
         LIMIT :limit
        ```

        ### 四个 WHERE 条件各自的理由

        | 条件 | 不写的后果 |
        |---|---|
        | `e.profile_id = :profile` | 混进别的 profile 的向量 → **距离比较失去意义**（ADR-15 说的静默劣化） |
        | `a.active_version_id = v.id` | 召回**旧版本**的内容 → 知识已更新但检索给旧的。这正是 WP20 §277 说的"不能因索引延迟读取错误旧事实" |
        | `a.archived_at IS NULL` | 召回**已归档**的知识 → 它已退出可用 |
        | **范围谓词** | **验收原文直接不满足**（跨品牌串事实） |

        第 2、3 条容易漏，且漏了**不报错**：结果看起来正常
        （有内容、有分数），只是内容是旧的或已归档的。

        ### 为什么 `ORDER BY distance` 而不是 `ORDER BY similarity`

        `distance` 是索引的排序键（`<op>` 与索引的 ops 必须**同族**，
        否则索引用不上）。相似度是它的单调变换，在服务层换算即可。
        反过来写（`ORDER BY 1 - distance`）会让**索引完全用不上** ——
        表达式不是索引的键。实测确认过：`ORDER BY <op>` 命中 `Index Scan`。

        ### 为什么 `embedding::vector(<dims>)` 要写出来

        列是无维度的，而**索引建在表达式 `embedding::vector(<dims>)` 上**。
        查询侧必须写**同一个表达式**，否则索引用不上。
        这不是风格问题 —— 写 `e.embedding <op> ...` 会让计划变成
        `Seq Scan`，而结果完全一样（**静默退化**）。

        ### `scan_limit` 的作用

        它是 `LIMIT` 之外的**独立上限**（防爆）。这里的做法是：
        把 `scan_limit` 作为 SQL 的 `LIMIT`，再在服务层截到 `limit`。
        **为什么不直接用 `limit`**：因为范围谓词可能让符合条件的行
        远少于 `limit` —— 那正是"要更多候选"的场景。
        用 `max(limit, scan_limit)` 会让 SQL 拉回过多行；
        本实现用 `scan_limit` 作为 SQL 侧上限，服务层截断到 `limit`，
        并在 `SearchOutcome.scanned` 里报告**实际扫了多少行** ——
        让"为什么只返回了 2 条"可诊断。
        """
        operator = METRIC_DISTANCE_OPERATOR[profile.metric]
        # 范围谓词：用**与 SQL 别名一致的**列对象构造（见 _scope_predicate_sql）。
        predicate_sql = self._scope_predicate_sql(subject)

        category_clause = ""
        params: dict[str, Any] = {
            "profile_id": profile.id,
            "q": vector_literal(query_vector),
            "limit": scan_limit,
        }
        if category is not None:
            category_clause = f" AND {_ASSET_ALIAS}.category = :category"
            params["category"] = category

        statement = text(
            f"""
            SELECT c.id            AS chunk_id,
                   c.chunk_no      AS chunk_no,
                   c.text_content  AS text_content,
                   v.id            AS version_id,
                   a.id            AS asset_id,
                   a.title         AS asset_title,
                   a.category      AS category,
                   a.scope         AS scope,
                   (e.embedding::vector({profile.dimensions}) {operator}
                    CAST(:q AS vector)) AS distance
              FROM chunk_embeddings e
              JOIN knowledge_chunks   c ON c.id = e.chunk_id
              JOIN knowledge_versions v ON v.id = c.knowledge_version_id
              JOIN knowledge_assets   {_ASSET_ALIAS} ON {_ASSET_ALIAS}.id = v.asset_id
             WHERE e.profile_id = :profile_id
               AND {_ASSET_ALIAS}.active_version_id = v.id
               AND {_ASSET_ALIAS}.archived_at IS NULL
               AND ({predicate_sql}){category_clause}
             ORDER BY distance
             LIMIT :limit
            """
        )
        rows = self._session.execute(statement, params).mappings().all()
        return [dict(row) for row in rows]

    def search_keyword(
        self,
        *,
        keywords: list[str],
        subject: ScopeSubject,
        category: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """关键词兜底检索（`knowledge_chunks.keywords` 的 `&&` 重叠运算）。

        规格源 02 §8 对 `keywords` 的备注原文是「**供关键词召回兜底**」——
        因此这是**兜底路径**，不是主路径（默认模式是 `SEMANTIC`）。

        范围谓词与"只召回生效版本"的条件与语义检索**完全一致** ——
        两条路径若口径不同，"兜底"会变成一个越权的旁路。
        这也是本层把两条查询放在同一个类里的理由：
        **它们必须一起改**。

        `distance` 字段在关键词模式下**没有意义**（没有向量比较），
        返回 `None`，相似度按"命中关键词个数 / 查询关键词个数"算 ——
        这是可解释的，且让两条路径的响应形状一致（调用方不必分支）。
        """
        predicate_sql = self._scope_predicate_sql(subject)
        category_clause = ""
        params: dict[str, Any] = {
            "keywords": keywords,
            "limit": limit,
        }
        if category is not None:
            category_clause = f" AND {_ASSET_ALIAS}.category = :category"
            params["category"] = category
        statement = text(
            f"""
            SELECT c.id            AS chunk_id,
                   c.chunk_no      AS chunk_no,
                   c.text_content  AS text_content,
                   v.id            AS version_id,
                   {_ASSET_ALIAS}.id            AS asset_id,
                   {_ASSET_ALIAS}.title         AS asset_title,
                   {_ASSET_ALIAS}.category      AS category,
                   {_ASSET_ALIAS}.scope         AS scope,
                   c.keywords      AS keywords
              FROM knowledge_chunks   c
              JOIN knowledge_versions v ON v.id = c.knowledge_version_id
              JOIN knowledge_assets   {_ASSET_ALIAS} ON {_ASSET_ALIAS}.id = v.asset_id
             WHERE c.keywords && CAST(:keywords AS text[])
               AND {_ASSET_ALIAS}.active_version_id = v.id
               AND {_ASSET_ALIAS}.archived_at IS NULL
               AND ({predicate_sql}){category_clause}
             ORDER BY cardinality(c.keywords) DESC, c.chunk_no ASC
             LIMIT :limit
            """
        )
        rows = self._session.execute(statement, params).mappings().all()
        return [dict(row) for row in rows]

    def set_ef_search(self, value: int) -> None:
        """在**当前事务内**设置 `hnsw.ef_search`。

        `SET LOCAL` 而不是 `SET`：`SET` 是**会话级**的，而连接池会复用
        会话 —— 一次检索改了它，下一个用同一连接的请求会继承这个值。
        症状是"检索质量随并发请求的顺序变化"（**随机**，最难查的一类）。

        本项目已踩过同族问题（WP12 ISS-28：进程内缓存跨实例失效；
        WP13 ISS-59：全局扫描被历史数据挤出窗口）——
        共同点是**一个全局资源被当成局部状态用**。
        """
        self._session.execute(
            text(f"SET LOCAL hnsw.ef_search = {int(value)}")
        )

    def count_assets_for_profile_scope(
        self, *, profile_id: uuid.UUID
    ) -> dict[str, int]:
        """按 scope 分组统计某个 profile 的 chunk 数（容量复核用）。

        ADR-15 的监控信号原文：「chunk 数逼近 200 万即触发容量复核」。
        这个统计让"哪个范围的知识在膨胀"可见 —— 只有总数的话，
        容量复核无从下手（不知道该清理哪个范围）。
        """
        rows = self._session.execute(
            text(
                """
                SELECT a.scope AS scope, count(DISTINCT e.chunk_id) AS chunks
                  FROM chunk_embeddings e
                  JOIN knowledge_chunks   c ON c.id = e.chunk_id
                  JOIN knowledge_versions v ON v.id = c.knowledge_version_id
                  JOIN knowledge_assets   a ON a.id = v.asset_id
                 WHERE e.profile_id = :profile_id
                 GROUP BY a.scope
                 ORDER BY a.scope
                """
            ),
            {"profile_id": profile_id},
        ).mappings().all()
        return {str(row["scope"]): int(row["chunks"]) for row in rows}


__all__ = [
    "EmbeddingRepository",
    "vector_literal",
]
