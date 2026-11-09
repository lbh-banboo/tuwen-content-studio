"""WP21 的两张表（检索域）。

| 表 | 性质 | 来源 |
|---|---|---|
| `embedding_profiles` | **配置**。一个 embedding 模型 + 维度 + 度量的绑定，即 ADR-15 说的「**索引空间**」 | 前包划界（`docs/m2/WP19-研究关键词与提示词说明.md` L194 表名原文） |
| `chunk_embeddings` | **物化结果**。一个 chunk 在一个 profile 下的向量 | 同上 |

**为什么与 `models.py` 分开**：`models.py` 是 WP20 的**五张知识表**
（assets / versions / publications / chunks / proposals）。本包的两张表是
**检索域**，与知识域的生命周期完全不同：

  · 知识表是「业务事实」（一条知识、一版内容、一次发布）
  · 检索表是「可重建的物化结果」（换模型时全量重算）

混在一个文件里会让 WP20 的模块边界模糊 —— 而接缝冻结文档明确
「分支只写 `modules/<自己模块>/**`」，本包在 `knowledge/` 目录内
新增文件是允许的（同模块内扩展），**不改 WP20 的任何既有文件**。

---

## 一、`chunk_embeddings.embedding` 为什么是**无维度** `vector`

**实测依据（本包的关键结论，见说明文档 §0.3）**：

```
create table _p3 (v vector);
create index on _p3 using hnsw (v vector_cosine_ops);
→ ERROR: column does not have dimensions
```

pgvector 的 HNSW 索引必须在**列声明处**知道维度。因此：

| 方案 | 换维度模型 | 与 ADR-15 的对应 |
|---|---|---|
| **无维度列 + 每 profile 部分索引**（采用） | 加一个 profile + 一个索引 | 「按 profile 分区」**逐字对应** |
| 固定维度 `vector(1024)` | `ALTER COLUMN TYPE` + 重建全部索引 | 无对应 |

**为什么"每 profile 一个索引"能建在无维度列上**（实测通过）：

```sql
create index _s_p1 on chunk_embeddings
  using hnsw ((embedding::vector(1024)) vector_cosine_ops)
  where profile_id = '<uuid>';
```

—— 表达式 `embedding::vector(1024)` 在**索引定义里**给出维度，
于是索引知道维度，而列本身保持无维度（可容纳不同 profile 的不同维度）。
实测：两个 profile（1024 维 / 512 维）在同一张表上**共存**，
且查询计划命中 `Index Scan using _s_p1`。

**索引 DDL 不在本文件里**（它在 `embedding_ddl.py`）：维度是**数据**
（`embedding_profiles.dimensions`），因此索引必须**按 profile 动态生成**，
不能写死在 `Index(...)` 里。

---

## 二、`chunk_embeddings.model_version` 为什么冗余

它可以从 `profile_id` 推出。冗余的理由是**守卫需要一个可比较的列**：

若只比 profile 自己的值，守卫会退化成恒真（`profile.model_version`
当然等于 `profile.model_version`）。冗余这一列之后，
`NEW.model_version`（**调用方声称这个向量是哪个模型产的**）与
`profile.model_version`（**这个索引空间绑定的模型**）成为两个**独立**的值。

**这是 ADR-15「混用被阻止」判据的唯一落点**（实测：仅比维度拦不住
同维度的模型版本混用 —— 见说明文档 §0.4）。

---

## 三、`UQ(chunk_id, profile_id)` 而不是 `UQ(chunk_id)`

ADR-15 的切换流程第 3 步原文：

> 检索按 profile 路由，**切换期间旧 profile 保持可用**。

"两个 profile 同时可用"意味着**同一个 chunk 有两个向量**（每个 profile 一个）。
因此唯一键是 `(chunk_id, profile_id)` 而不是 `chunk_id`。

**这个选择有一个直接的可验证后果**：切换期检索旧 profile 返回旧向量，
检索新 profile 返回新向量，**两者都不为空**。若唯一键是 `chunk_id`，
"新 profile 就绪前旧 profile 仍可用"**无法表达** ——
写新向量会覆盖旧的，切换期检索直接返回空。

---

## 四、`ON DELETE CASCADE`（与 `knowledge_chunks` 的 FK 方向相反）

`knowledge_chunks.knowledge_version_id` 是 `ON DELETE RESTRICT`
（WP20 的决定：版本删不掉，因为它是历史事实）。

而 `chunk_embeddings.chunk_id` 是 **`ON DELETE CASCADE`**：

  · chunk 是**可重建的物化结果**（WP20 原文：`models.py` L467
    「**不建不可变触发器** —— 允许『同一版本重新切块』这一操作」）；
  · 重切块 = 删旧 chunk + 写新 chunk。若这里是 RESTRICT，
    重切会**因为向量存在而失败** —— 于是"换模型不混向量"（ADR-15 硬约束）
    在实现层无法完成。

**这正是 WP20 那句"加锁会让 WP21 无法实现换模型不混向量"的兑现点。**
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.knowledge.embedding_types import (
    MAX_EMBEDDING_DIMENSIONS,
    METRIC_LIST_SQL,
    MIN_EMBEDDING_DIMENSIONS,
    PROFILE_STATE_LIST_SQL,
)
from rednote.platform.base import Base, created_at_column, updated_at_column, uuid_pk

#: 名字的最大长度（与 `knowledge_assets.title` 的 200 同一量级）。
MAX_PROFILE_NAME_CHARS: Final[int] = 120

#: `model_version` 的最大长度。它是"模型 + 版本"的标识串
#: （例如 `bge-m3@v1`），不是自由文本描述。
MAX_MODEL_VERSION_CHARS: Final[int] = 120

#: profile 名的唯一约束名（证据脚本按名字回查）。
UNIQUE_PROFILE_NAME: Final[str] = "uq_embedding_profiles_name"


class EmbeddingProfile(Base):
    """一个 embedding 模型 + 维度 + 度量的绑定 —— **ADR-15 说的「索引空间」**。

    **它是配置，不是物化结果**：`state` 可变（切换流程会改它），
    但**行本身不被删除**（ADR-15 第 5 步：「旧 profile 保留至确认无需回滚，
    再回收」）。回收是显式动作，不在本包的端点里。

    **`dimensions` 是一列数据，不是 schema**：OP-44（模型与维度）未定，
    把它写死进列类型会让"换 1536 维模型"变成一次 `ALTER TABLE` + 重建索引。
    放进列之后，换维度是"建一个新 profile"。

    **`metric` 也是 profile 的属性**：换模型时度量可能变（归一化模型 → 未归一化）。
    度量写死在代码里会让"换模型"与"换度量"脱钩 —— 而它们其实是
    同一件事（都是"换索引空间"）。

    **`state` 的两值语义见 `embedding_types`**：`ACTIVE`（当前默认检索目标，
    同一时刻至多一个）/ `DEPRECATED`（仍可显式检索，但不再是默认）。

    ⚠️ **"同一时刻至多一个 ACTIVE"为什么不做成唯一约束**：
    唯一约束会拦下**切换期的中间态** —— ADR-15 第 3 步要求
    「切换期间旧 profile 保持可用」，而"可用"与"是默认"是两件事。
    若用 `UNIQUE(state) WHERE state='ACTIVE'`，切换时先把新的置 ACTIVE
    会撞约束（旧的还是 ACTIVE），必须先降旧的再升新的 ——
    **中间存在一个"没有 ACTIVE profile"的窗口**，检索会失败。
    因此本实现把"默认 profile"表达为**显式的选择规则**
    （见 `embedding_repository.get_active_profile`），并在服务层保证
    同一时刻只有一个（测试断言）。取舍已登记为待定项。
    """

    __tablename__ = "embedding_profiles"

    id: Mapped[uuid.UUID] = uuid_pk()

    name: Mapped[str] = mapped_column(
        String(MAX_PROFILE_NAME_CHARS), nullable=False
    )
    # 绑定到 WP16 的模型档案（"用哪个供应商/模型"的配置在那张表里）。
    # `ON DELETE RESTRICT`：模型档案被引用时删不掉 —— 与 WP20 对
    # `knowledge_assets.brand_id` 的处理同一口径。
    model_profile_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("model_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 模型版本标识（ADR-15 硬约束的比较对象）。**它不是 `model_profiles` 的列**：
    # `model_profiles` 描述"用哪个模型"（配置），而这里描述
    # "这个索引空间是由哪个模型版本产出的"（历史事实）。
    # 同一模型的两个版本可以共用一条 `model_profiles` 行。
    model_version: Mapped[str] = mapped_column(
        String(MAX_MODEL_VERSION_CHARS), nullable=False
    )
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)

    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'ACTIVE'")
    )

    # 索引构建参数。**存下来而不是吃 pgvector 默认值**：
    # ADR-15 判据 7 要求每次换模型做质量回归，而"回归对比"必须知道
    # 两边的参数是否一致 —— 参数不在库里就无法回答。
    # `ef_search` 是查询期参数（可在会话内 SET），`m` / `ef_construction`
    # 是构建期参数（改了必须重建索引，因此它们属于 profile 的身份）。
    hnsw_m: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("16")
    )
    hnsw_ef_construction: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("64")
    )
    hnsw_ef_search: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("40")
    )

    # 索引是否已建。**这一列存在的理由**：索引 DDL 是按 profile 动态生成的
    # （维度是数据），因此"这个 profile 的索引建了没有"是一个**真实的状态**，
    # 而不是"建了表就有"。检索前必须检查它 —— 否则会退化成精确扫描
    # （能出结果，但慢），而**没有任何信号**说明索引没建。
    index_ready: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        UniqueConstraint("name", name=UNIQUE_PROFILE_NAME),
        CheckConstraint(
            f"dimensions >= {MIN_EMBEDDING_DIMENSIONS}"
            f" AND dimensions <= {MAX_EMBEDDING_DIMENSIONS}",
            name="dimensions_in_range",
        ),
        CheckConstraint(
            f"metric IN ({METRIC_LIST_SQL})",
            name="metric_is_known",
        ),
        CheckConstraint(
            f"state IN ({PROFILE_STATE_LIST_SQL})",
            name="state_is_known",
        ),
        CheckConstraint(
            "char_length(name) > 0 AND char_length(model_version) > 0",
            name="identity_not_empty",
        ),
        # HNSW 参数必须为正。`m = 0` 会让 pgvector 报错（不是静默），
        # 但 `ef_search = 0` 在部分版本里被当成"用默认值" ——
        # 静默回退到一个未知值，而质量回归的对比就失去了基线。
        CheckConstraint(
            "hnsw_m > 0 AND hnsw_ef_construction > 0 AND hnsw_ef_search > 0",
            name="hnsw_params_positive",
        ),
        # 索引已就绪的前提是它已经建过 —— 这一列是**报告**，不是开关。
        Index("ix_embedding_profiles_state", "state"),
    )


class ChunkEmbedding(Base):
    """一个 chunk 在一个 profile 下的向量。**可重建的物化结果**。

    生命周期由三件事决定（WP20 原文：`models.py` L457「它完全由
    (版本内容, 切块器版本) 决定」+ 本包的 embedding 模型）：

        (knowledge_version.body, CHUNKER_VERSION, profile.model_version)

    三者任一变化 → 需要重算。**重算的方式是删了重写**，
    因此 `ON DELETE CASCADE`（见模块 docstring 第四节）。

    **为什么存 `chunk_text_hash` 的副本**：`knowledge_chunks.text_hash`
    已经有一份。这里再存一份的理由是**重算时的一致性检查**：
    重切块会先删 chunk（级联删向量）再写新 chunk，此时两边的哈希
    必然一致（因为是同一次写入）。但**换 embedding 模型时**只重算向量、
    不重切块 —— 此时若有人改了 chunk 的文本（例如通过 WP20 的
    "同版本重新切块"路径），向量就与文本对不上了。
    冗余的哈希让这个检查成为**一次本地比较**，不必 join。
    它是 WP23 的 ISS-82 的反面：**让"两边是否一致"有一个可比较的落点**。
    """

    __tablename__ = "chunk_embeddings"

    id: Mapped[uuid.UUID] = uuid_pk()

    # `ON DELETE CASCADE`：见模块 docstring 第四节。
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_chunks.id", ondelete="CASCADE"),
        nullable=False,
    )
    # `ON DELETE RESTRICT`：profile 被引用时删不掉 —— 删一个还有向量的
    # profile 会让那些向量变成孤儿（而它们占的空间不小）。
    # ADR-15 第 5 步说"再回收"，回收是显式动作。
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("embedding_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # **冗余列，守卫需要它**（见模块 docstring 第二节）。
    model_version: Mapped[str] = mapped_column(
        String(MAX_MODEL_VERSION_CHARS), nullable=False
    )

    # **无维度**：见模块 docstring 第一节。
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)

    # chunk 文本的哈希副本（见类 docstring）。
    chunk_text_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # 向量维度**也冗余一份**：守卫要比较 `vector_dims(NEW.embedding)` 与
    # profile 的 `dimensions`，而 `vector_dims()` 是 PG 函数、不是列。
    # 冗余这一列让"这条向量的维度是多少"成为一个**可被 SQL 直接查的列** ——
    # 容量统计与异常排查都要它（`vector_dims()` 在大表上是一次全扫）。
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        # 见模块 docstring 第三节：ADR-15 切换期要求两个 profile 同时可用。
        UniqueConstraint(
            "chunk_id",
            "profile_id",
            name="uq_chunk_embeddings_chunk_profile",
        ),
        CheckConstraint(
            f"dimensions >= {MIN_EMBEDDING_DIMENSIONS}"
            f" AND dimensions <= {MAX_EMBEDDING_DIMENSIONS}",
            name="dimensions_in_range",
        ),
        CheckConstraint(
            "char_length(chunk_text_hash) = 64", name="chunk_text_hash_length"
        ),
        CheckConstraint(
            "char_length(model_version) > 0", name="model_version_not_empty"
        ),
        # 检索按 profile 过滤（范围谓词与部分索引都按它）——
        # 这条索引让"某个 profile 下有多少向量"与"取某个 profile 的候选"
        # 都不必全表扫。
        Index("ix_chunk_embeddings_profile_id", "profile_id"),
        # 重切块时按 chunk 找向量（级联删除走外键索引，这条供显式查询）。
        Index("ix_chunk_embeddings_chunk_id", "chunk_id"),
    )


#: 本包的两张表（测试夹具与证据脚本共用，避免两处各写一份）。
OWN_TABLES: Final[tuple[str, ...]] = (
    "chunk_embeddings",
    "embedding_profiles",
)

#: 建表顺序（先 profile 后 embedding —— 外键方向）。
CREATE_ORDER: Final[tuple[str, ...]] = (
    "embedding_profiles",
    "chunk_embeddings",
)

#: 删表顺序（反序）。
DROP_ORDER: Final[tuple[str, ...]] = (
    "chunk_embeddings",
    "embedding_profiles",
)

__all__ = [
    "CREATE_ORDER",
    "DROP_ORDER",
    "MAX_MODEL_VERSION_CHARS",
    "MAX_PROFILE_NAME_CHARS",
    "OWN_TABLES",
    "UNIQUE_PROFILE_NAME",
    "ChunkEmbedding",
    "EmbeddingProfile",
]
