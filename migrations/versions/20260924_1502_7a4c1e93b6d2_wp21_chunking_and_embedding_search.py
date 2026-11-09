"""WP21：切块 + embedding + 范围过滤 + 检索（2 张表 + 1 个触发器函数）。

revision: 7a4c1e93b6d2
down_revision: 3cb74d02edc5

**本迁移包含 2 张表**：

| 对象 | 性质 | 说明 |
|---|---|---|
| `embedding_profiles` | **配置** | 一个 embedding 模型 + 维度 + 度量的绑定 —— ADR-15 说的「**索引空间**」。`state` 可变（切换流程会改），行本身不被删除 |
| `chunk_embeddings` | **物化结果** | 一个 chunk 在一个 profile 下的向量。**可重建**（换模型时全量重算） |

**为什么与 WP20 的 `knowledge_*` 分属不同迁移**：它们是**检索域**，
生命周期与知识域完全不同（知识是业务事实，向量是可重建的物化结果）。

---

## 一、`embedding` 列是**无维度** `vector`（本包最要紧的一处设计判断）

**实测依据**（pgvector 0.8.6 / PG 17.10）：

```
create table _p3 (v vector);
create index on _p3 using hnsw (v vector_cosine_ops);
→ ERROR: column does not have dimensions
```

pgvector 的 HNSW 索引**必须在列声明处知道维度**。这是**类型系统的硬约束**，
不是配置问题。因此两条路：

| 方案 | 换维度模型 | 与 ADR-15 的对应 |
|---|---|---|
| **无维度列 + 每 profile 部分索引**（采用） | 加一个 profile + 一个索引 | 「按 profile 分区」**逐字对应** |
| 固定维度 `vector(1024)` | `ALTER COLUMN TYPE` + 重建全部索引 | 无对应 |

实测的替代路径矩阵（哪些能建 HNSW / 多维度能否共存 / 查询能否命中索引）：

| 方案 | 建 HNSW | 多维度共存 | 命中索引 |
|---|---|---|---|
| 无维度 `vector` 裸列 | ❌ | ✅ | — |
| **无维度 + 每 profile 部分索引**（采用） | ✅ | ✅ | ✅ `Index Scan` |
| 无维度 + 表达式索引 `(v::vector(1024))` | ✅ | ✅ | ✅ `Index Scan` |
| 无维度 + 声明式分区（子表建索引） | ❌ 继承列仍无维度 | — | — |
| 固定维度 `vector(1024)` | ✅ | ❌ | ✅ |

**HNSW 索引不在本迁移里建** —— 维度是**数据**
（`embedding_profiles.dimensions`），因此索引必须**按 profile 动态生成**：

```sql
CREATE INDEX ix_chunk_embeddings_hnsw_<profile_uuid_sanitized>
  ON chunk_embeddings USING hnsw ((embedding::vector(<dims>)) vector_cosine_ops)
  WITH (m = <m>, ef_construction = <ef>)
  WHERE profile_id = '<uuid>';
```

DDL 由 `modules/knowledge/embedding_ddl.py` 的 `hnsw_index_ddl()` 生成
（**不在本文件里手写** —— 与测试/服务同源，避免两份不一致的 DDL，
本项目 WP11 / WP16 / WP17 / WP18 各踩过一次）。

## 二、混用守卫（触发器）—— ADR-15 判据「混用被阻止」的唯一落点

**为什么必须有数据库层的守卫**：绕过 ORM 的写入路径
（psql / 数据回填 / 运维脚本）**没有代码评审**。
若守卫只写在 service 里，那些路径会静默把新模型的向量写进旧索引空间 ——
而 ADR-15 原文说这危害「**不是报错而是静默劣化**」。

**三条判据**（缺任何一条都会留下一个静默口子）：

| # | 判据 | 不写的后果 |
|---|---|---|
| 1 | `NEW.model_version = profile.model_version` | **同维度混用**（两个模型版本可以维度相同，例如 `bge-m3@v1` 与 `bge-m3@v2` 都是 1024 维）→ 新模型向量静默写进旧索引空间。**实测确认仅比维度拦不住** |
| 2 | `vector_dims(NEW.embedding) = profile.dimensions` | 异维度混用（写入会成功，但距离比较失去意义） |
| 3 | `NEW.dimensions = vector_dims(NEW.embedding)` | 冗余的 `dimensions` 列撒谎（容量统计与异常排查都读它） |

判据 1 是**本包最关键的负向测试**的落点（见
`tests/test_knowledge_wp21.py::TestMixingGuardLive`）。

## 三、`ON DELETE CASCADE`（与 `knowledge_chunks` 的 FK 方向相反）

`knowledge_chunks.knowledge_version_id` 是 `ON DELETE RESTRICT`
（WP20 的决定：版本删不掉，因为它是历史事实）。

而 `chunk_embeddings.chunk_id` 是 **`ON DELETE CASCADE`**：

  · chunk 是**可重建的物化结果**（WP20 原文：`models.py` L467
    「**不建不可变触发器** —— 允许『同一版本重新切块』这一操作」）；
  · 重切块 = 删旧 chunk + 写新 chunk。若这里是 RESTRICT，
    重切会**因为向量存在而失败** —— 于是"换模型不混向量"（ADR-15 硬约束）
    在实现层无法完成。

**这正是 WP20 那句"加锁会让 WP21 无法实现换模型不混向量"的兑现点。**

## 四、`UQ(chunk_id, profile_id)` 而不是 `UQ(chunk_id)`

ADR-15 切换流程第 3 步原文：「检索按 profile 路由，
**切换期间旧 profile 保持可用**」。"两个 profile 同时可用"意味着
**同一个 chunk 有两个向量**（每个 profile 一个）。

若唯一键是 `chunk_id`，"新 profile 就绪前旧 profile 仍可用"**无法表达** ——
写新向量会覆盖旧的，切换期检索直接返回空。

---

## 五、G2 铁律的执行记录（本文件是手工写全的）

`alembic revision --autogenerate` 在本包产出的是**垃圾**（实测）：

  · 测试库里表**已存在**（测试夹具 `create_all` 建的），
    因此 autogenerate 提议的是「删 8 个探针 HNSW 索引」+
    「一个 `prompt_templates` 外键的 drop/create 抖动」，
    **完全没有 `CREATE TABLE`**；
  · 其中 `op.create_index(... [sa.literal_column('(embedding::vector(64))')] ...)`
    这类语句若被留下，会在别的库上**创建一个指向不存在 profile 的索引**。

因此本文件**只取 `revision` / `down_revision` 两个值**，其余手工写全
（与 WP18 / WP20 / WP23 同一做法）。

**`vector` 类型必须显式写 `Vector()`**：autogenerate 不认识它，
会渲染成 `sa.NullType()` —— 那是**建不出正确表**的（G2 第 2 条）。

**`from sqlalchemy.dialects import postgresql` 本文件用不到**
（没有 JSONB 列），因此不引入 —— 但 `pgvector.sqlalchemy.Vector` 必须引入。

## 六、前置条件：pgvector 扩展

本迁移**不建扩展**（`CREATE EXTENSION` 失败会中断整条迁移链，
而扩展的安装属于部署层的 initdb —— 见 `deploy/dev/initdb/01_init.sql`）。

**实测发现的一个真实风险**：本机 `createdb` 建的库**没有** vector 扩展
（`postgres` 主库有，是手工装的；`initdb/01_init.sql` 只在 Docker 容器
首次启动时执行）。因此 `alembic upgrade head` 在**新库**上会因
`type "vector" does not exist` 失败。

本迁移在 `upgrade()` 开头**显式检查**并给出可操作的错误信息 ——
fail-closed 而不是留下一个难懂的 `UndefinedObject`。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "7a4c1e93b6d2"
down_revision: str | Sequence[str] | None = "3cb74d02edc5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 守卫函数与触发器名（与 `embedding_ddl.py` 同值）。
#: **两处都要**：迁移需要它们的名字来写 `DROP`，而模块需要它们来建。
_GUARD_FUNCTION = "rednote_check_embedding_profile_binding"
_GUARD_TRIGGER = "trg_chunk_embeddings_profile_binding"


def _assert_vector_extension() -> None:
    """前置检查：pgvector 扩展必须已装。

    **为什么在迁移里做检查而不是让它自然失败**：
    `type "vector" does not exist` 是 PG 报的，它不会告诉运维
    "你需要先装 pgvector 扩展" —— 排障时容易误判成"迁移写错了"。
    显式检查把一条**可操作的**信息给出来。

    本包实测踩到：本机 `createdb` 建的新库没有 vector 扩展
    （只有 `postgres` 主库有，是手工装的）。
    """
    bind = op.get_bind()
    installed = bind.execute(
        sa.text("select 1 from pg_extension where extname = 'vector'")
    ).scalar()
    if not installed:
        available = bind.execute(
            sa.text("select 1 from pg_available_extensions where name = 'vector'")
        ).scalar()
        if available:
            raise RuntimeError(
                "pgvector 扩展可用但未启用。执行："
                "CREATE EXTENSION IF NOT EXISTS vector; 后再跑迁移。"
                "（本迁移刻意不代建扩展 —— CREATE EXTENSION 失败会中断"
                "整条迁移链，扩展安装属于部署层 initdb 的职责。）"
            )
        raise RuntimeError(
            "本 PG 实例未提供 pgvector 扩展（缺少二进制）。"
            "本迁移的两张表需要 vector 类型，无法继续。"
            "请在内网 pgvector 镜像或装了 pgvector 的实例上执行。"
        )


def upgrade() -> None:
    """正向迁移。

    **建表顺序是依赖序**（表级无环，不需要 `ALTER TABLE` 分步补外键）：

        embedding_profiles（引用 model_profiles，WP16 已存在）
            ↑ profile_id
        chunk_embeddings（引用 embedding_profiles 与 knowledge_chunks）

    `knowledge_chunks` 由 WP20 的迁移建出 —— 本迁移**不建它**。
    """
    _assert_vector_extension()

    # ### commands auto generated by Alembic - please adjust! ###
    # （autogenerate 在本包产出的是删探针索引的垃圾，见模块 docstring 第五节；
    #   以下为手工写全）

    # ---- 第 1 张：embedding_profiles ----
    op.create_table(
        "embedding_profiles",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        # FK 到 WP16 的模型档案。`ON DELETE RESTRICT`：模型档案被引用时删不掉
        # —— 与 WP20 对 `knowledge_assets.brand_id` 的处理同一口径。
        sa.Column("model_profile_id", sa.UUID(), nullable=False),
        sa.Column("model_version", sa.String(length=120), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default=sa.text("'ACTIVE'"),
            nullable=False,
        ),
        # HNSW 参数**存下来而不是吃 pgvector 默认值**：ADR-15 判据 7 要求
        # 每次换模型做质量回归，而"回归对比"必须知道两边参数是否一致 ——
        # 参数不在库里就无法回答。
        sa.Column("hnsw_m", sa.Integer(), server_default=sa.text("16"), nullable=False),
        sa.Column(
            "hnsw_ef_construction",
            sa.Integer(),
            server_default=sa.text("64"),
            nullable=False,
        ),
        sa.Column(
            "hnsw_ef_search",
            sa.Integer(),
            server_default=sa.text("40"),
            nullable=False,
        ),
        # 索引是否已建。索引 DDL 按 profile 动态生成（维度是数据），
        # 因此"这个 profile 的索引建了没有"是一个**真实状态**。
        # 检索前必须检查它 —— 否则会退化成精确扫描（能出结果但慢），
        # 而**没有任何信号**说明索引没建。
        sa.Column(
            "index_ready", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embedding_profiles")),
        sa.UniqueConstraint("name", name="uq_embedding_profiles_name"),
        sa.CheckConstraint(
            "dimensions >= 1 AND dimensions <= 4096",
            name="dimensions_in_range",
        ),
        sa.CheckConstraint(
            "metric IN ('COSINE', 'INNER_PRODUCT', 'L2')",
            name="metric_is_known",
        ),
        sa.CheckConstraint(
            "state IN ('ACTIVE', 'DEPRECATED')",
            name="state_is_known",
        ),
        sa.CheckConstraint(
            "char_length(name) > 0 AND char_length(model_version) > 0",
            name="identity_not_empty",
        ),
        # `ef_search = 0` 在部分 pgvector 版本里被当成"用默认值" ——
        # 静默回退到一个未知值，而质量回归的对比就失去了基线。
        sa.CheckConstraint(
            "hnsw_m > 0 AND hnsw_ef_construction > 0 AND hnsw_ef_search > 0",
            name="hnsw_params_positive",
        ),
        sa.ForeignKeyConstraint(
            ["model_profile_id"],
            ["model_profiles.id"],
            name=op.f("fk_embedding_profiles_model_profile_id_model_profiles"),
            ondelete="RESTRICT",
        ),
    )
    # 检索默认目标按 state 取（`get_active_profile`），这条索引让它不必全表扫。
    op.create_index(
        "ix_embedding_profiles_state", "embedding_profiles", ["state"], unique=False
    )

    # ---- 第 2 张：chunk_embeddings ----
    op.create_table(
        "chunk_embeddings",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # **`ON DELETE CASCADE`**（见模块 docstring 第三节）——
        # 与 `knowledge_chunks.knowledge_version_id` 的 RESTRICT 相反。
        sa.Column("chunk_id", sa.UUID(), nullable=False),
        # **`ON DELETE RESTRICT`**：删一个还有向量的 profile 会让那些向量
        # 变成孤儿（而它们占的空间不小）。ADR-15 第 5 步说"再回收"，
        # 回收是显式动作。
        sa.Column("profile_id", sa.UUID(), nullable=False),
        # **冗余列，守卫需要它**（见模块 docstring 第二节判据 1）。
        sa.Column("model_version", sa.String(length=120), nullable=False),
        # **无维度 `vector`**（见模块 docstring 第一节）。
        # ⚠️ 必须显式写 `Vector()` —— autogenerate 会渲染成 `sa.NullType()`。
        sa.Column("embedding", Vector(), nullable=False),
        # chunk 文本的哈希副本：重算时的一致性检查（换模型时只重算向量、
        # 不重切块，若有人改了 chunk 文本，向量就与文本对不上了）。
        sa.Column("chunk_text_hash", sa.String(length=64), nullable=False),
        # 向量维度**也冗余一份**：守卫要比较 `vector_dims(NEW.embedding)`
        # 与 profile 的 `dimensions`，而 `vector_dims()` 是 PG 函数、不是列。
        # 冗余它让"这条向量的维度是多少"成为**可被 SQL 直接查的列** ——
        # 容量统计与异常排查都要它（大表上 `vector_dims()` 是一次全扫）。
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunk_embeddings")),
        # 见模块 docstring 第四节：ADR-15 切换期要求两个 profile 同时可用。
        sa.UniqueConstraint(
            "chunk_id", "profile_id", name="uq_chunk_embeddings_chunk_profile"
        ),
        sa.CheckConstraint(
            "dimensions >= 1 AND dimensions <= 4096",
            name="dimensions_in_range",
        ),
        sa.CheckConstraint(
            "char_length(chunk_text_hash) = 64", name="chunk_text_hash_length"
        ),
        sa.CheckConstraint(
            "char_length(model_version) > 0", name="model_version_not_empty"
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["knowledge_chunks.id"],
            name=op.f("fk_chunk_embeddings_chunk_id_knowledge_chunks"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["embedding_profiles.id"],
            name=op.f("fk_chunk_embeddings_profile_id_embedding_profiles"),
            ondelete="RESTRICT",
        ),
    )
    # 检索按 profile 过滤（范围谓词与部分索引都按它）——
    # 这条索引让"某个 profile 下有多少向量"与"取某个 profile 的候选"
    # 都不必全表扫。
    op.create_index(
        "ix_chunk_embeddings_profile_id", "chunk_embeddings", ["profile_id"], unique=False
    )
    # 重切块时按 chunk 找向量（级联删除走外键索引，这条供显式查询）。
    op.create_index(
        "ix_chunk_embeddings_chunk_id", "chunk_embeddings", ["chunk_id"], unique=False
    )

    # ---- 混用守卫（触发器）----
    # **DDL 不写在这里**，而是调用模块自己的函数（与测试同源）。
    # 见模块 docstring 第二节。
    from rednote.modules.knowledge.embedding_ddl import mixing_guard_ddl

    _fn_ddl, _tg_ddl, _drop_tg, _drop_fn = mixing_guard_ddl()
    op.execute(_fn_ddl)
    op.execute(_tg_ddl)

    # ### end Alembic commands ###


def downgrade() -> None:
    """反向迁移。

    必须补全，不能留 `NotImplementedError` —— 那会让任何回滚尝试直接失败，
    与 WP47 的恢复演练（RPO/RTO）目标冲突。

    **只删本迁移自己建出来的对象**：

      · 两张表（`chunk_embeddings` 先删 —— 它引用 `embedding_profiles`）；
      · 守卫函数与触发器（本包引入的，生命周期归本迁移）。

    **不删 `knowledge_chunks`**：它是 WP20 的表，本迁移只引用它。
    **不删 `vector` 扩展**：它由部署层 initdb 管理，可能有别的对象在用
    （本包实测：`postgres` 主库的 vector 扩展在本迁移之前就存在）。
    """
    # ### commands auto generated by Alembic - please adjust! ###
    op.execute(f"DROP TRIGGER IF EXISTS {_GUARD_TRIGGER} ON chunk_embeddings")
    op.execute(f"DROP FUNCTION IF EXISTS {_GUARD_FUNCTION}()")

    # 索引随表删除，但显式 drop 让"删了什么"在迁移日志里可见。
    op.drop_index("ix_chunk_embeddings_chunk_id", table_name="chunk_embeddings")
    op.drop_index("ix_chunk_embeddings_profile_id", table_name="chunk_embeddings")
    op.drop_table("chunk_embeddings")

    op.drop_index("ix_embedding_profiles_state", table_name="embedding_profiles")
    op.drop_table("embedding_profiles")
    # ### end Alembic commands ###
