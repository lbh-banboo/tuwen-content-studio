"""本包表的**数据库级保证**（DDL）。

与 WP20 的 `ddl.py` 同一手法，两个消费方：
  1. 迁移（主 agent 串行落成）负责把它建出来；
  2. 测试 / 证据脚本负责回查它是否真的存在、真的生效。

**名字与 SQL 只有一处来源** —— 否则会出现"迁移建了 `trg_x`、
验证脚本找 `trg_y`"这种**看起来通过、实际没有保护**的情况。

`RAISE EXCEPTION ... USING MESSAGE = ...` 一律用**字符串拼接**而不用 `%`
占位符（psycopg 的 paramstyle 会把 `%` 当参数占位符）。

---

## 一、混用守卫：ADR-15「混用被阻止」判据的唯一落点

ADR-15 的验证方式原文：

> | 混用被阻止 | 负向测试：尝试向旧 profile 索引空间写入新模型向量应被拒绝 |

**本包实测发现：只比 `dimensions` 拦不住最危险的那一类**（见说明文档 §0.4）：

    两个模型版本可以维度相同（bge-m3@v1 与 bge-m3@v2 都是 1024 维）
    → 维度守卫无感
    → 新模型的向量静默写进旧 profile 的索引空间

而 ADR-15 对混用后果的描述是：

> 混用的危害不是报错而是**静默劣化**：向量空间不同，距离比较失去意义，
> 检索会返回语义无关的结果，表现为"AI 答非所问"而非"系统故障"。

**因此守卫必须双项比较**：`model_version` **与** `dimensions`。

### 为什么用触发器而不是 CHECK

CHECK 表达不了"跨表比较"（`chunk_embeddings.model_version` 要与
`embedding_profiles.model_version` 比）。这与 WP20 的
`publication_scope_ddl` 同一理由：**跨表不变量只能用触发器**。

### 为什么是 BEFORE 而不是 AFTER

它是**拒绝写入**，不是"写完了再检查"。BEFORE 让这次 INSERT 根本不发生 ——
而 AFTER 会先写入再抛错，回滚虽然也会撤销它，但触发器在
"写入失败但已产生行"的状态下运行，容易写出依赖"行已存在"的逻辑。
（WP20 的 OP-88 用 AFTER 是因为它**必须**延迟到提交时 ——
本守卫没有这个约束，它只依赖 `NEW.profile_id` 与一张已存在的 profile 行。）

---

## 二、索引：**每 profile 一个部分 HNSW 索引**（按 profile 动态生成）

**实测依据**（说明文档 §0.3）：

```
create table _p3 (v vector);
create index on _p3 using hnsw (v vector_cosine_ops);
→ ERROR: column does not have dimensions
```

`embedding` 列是**无维度** `vector`（容纳不同 profile 的不同维度），
因此索引必须在**索引定义里**给出维度：

```sql
create index <name> on chunk_embeddings
  using hnsw ((embedding::vector(<dims>)) <ops>)
  where profile_id = '<uuid>';
```

实测：这条索引**能建**（两个 profile 不同维度共存），
且查询计划命中 `Index Scan`。

### 为什么维度不能在 `models.py` 的 `Index(...)` 里写死

因为维度是**数据**（`embedding_profiles.dimensions`）。
写死一个维度会让"第二个 profile 用另一个维度"无法建索引 ——
而 ADR-15 的整个前提就是"会换模型"。

### `index_ready` 与索引 DDL 的成对关系

`embedding_profiles.index_ready` 报告"这个 profile 的索引建了没有"。
两者必须成对：**建索引的动作要同时把该列置 true**。
判据：证据脚本要能证明"建索引后 `index_ready = true`"，
以及"索引真的在 `pg_indexes` 里"。

---

## 三、为什么没有不可变触发器

WP20 的 `ddl.py` 明确把 `knowledge_chunks` 排除在不可变之外，原文：

> `knowledge_chunks` **不是**（历史事实）。它完全由 (版本内容, 切块器版本)
> 决定，是**可重建的物化结果**。

本包的两张表是**同一性质**（更弱）：`embedding_profiles` 是配置
（`state` 可变），`chunk_embeddings` 是物化结果。
对它们加不可变保护会**直接违背 ADR-15**：换模型时删掉旧向量是正常运维，
而不是篡改历史。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.knowledge.embedding_models import (
    ChunkEmbedding,
    EmbeddingProfile,
)
from rednote.modules.knowledge.embedding_types import (
    METRIC_INDEX_OPS,
)

# ---------------------------------------------------------------------------
# SQLSTATE 与名字
# ---------------------------------------------------------------------------

# PostgreSQL check_violation。与 WP20 的 `CONSISTENCY_SQLSTATE` 同一个码：
# 语义上它都是"某条数据规则被违反了"，都该映射到 422 VALIDATION_FAILED。
CONSISTENCY_SQLSTATE: Final[str] = "23514"

# --- 混用守卫 --------------------------------------------------------------
MIXING_GUARD_FUNCTION: Final[str] = "rednote_check_embedding_profile_binding"
MIXING_GUARD_TRIGGER: Final[str] = "trg_chunk_embeddings_profile_binding"

# --- 索引命名 --------------------------------------------------------------
#: 索引名前缀。**按 profile 生成时用它拼名字** ——
#: 证据脚本与迁移都从本函数取名，避免"迁移建了 A、脚本找 B"。
INDEX_NAME_PREFIX: Final[str] = "ix_chunk_embeddings_hnsw_"


def _raise_block(message_expr: str) -> str:
    """生成一段 RAISE 语句（**字符串拼接，不用 `%`**，见模块 docstring）。"""
    return (
        "        RAISE EXCEPTION USING\n"
        f"            MESSAGE = {message_expr},\n"
        f"            ERRCODE = '{CONSISTENCY_SQLSTATE}';\n"
    )


def mixing_guard_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    挂 `chunk_embeddings` BEFORE INSERT，拒绝两类混用：

    | # | 混用形态 | 判据 | 症状（若不拦） |
    |---|---|---|---|
    | 1 | **异维度混用** | `vector_dims(NEW.embedding) <> profile.dimensions` | 检索时维度不匹配 —— pgvector 会报错（**这一条相对好发现**） |
    | 2 | **同维度混用** | `NEW.model_version <> profile.model_version` | **完全静默**：距离比较失去意义，返回语义无关结果（ADR-15 原文的"AI 答非所问"） |

    **第 2 条是本函数存在的真正理由** —— 第 1 条 pgvector 自己也会拦
    （`expected 1024 dimensions, not 512`），但第 2 条**没有任何其他防线**。

    `BEFORE` 而不是 `AFTER`：见模块 docstring 第一节。

    `RETURN NEW`（BEFORE 触发器的返回值是**真正写入的行**）——
    与 WP20 的 AFTER 触发器写 `RETURN NULL` 形成对比。
    """

    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {MIXING_GUARD_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    bound_version text;\n"
        "    bound_dims int;\n"
        "    actual_dims int;\n"
        "BEGIN\n"
        "    SELECT model_version, dimensions\n"
        "      INTO bound_version, bound_dims\n"
        "      FROM embedding_profiles WHERE id = NEW.profile_id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'chunk_embeddings.profile_id 指向不存在的 embedding profile：'\n"
            "                      || NEW.profile_id::text"
        )
        + "    END IF;\n"
        "\n"
        "    actual_dims := vector_dims(NEW.embedding);\n"
        "\n"
        "    -- 判据 1：同维度混用（**最危险，完全静默**）\n"
        "    IF NEW.model_version <> bound_version THEN\n"
        + _raise_block(
            "'profile '\n"
            "                      || NEW.profile_id::text\n"
            "                      || ' 绑定的是模型版本 ' || bound_version\n"
            "                      || '，而这条向量的 model_version 是 ' || NEW.model_version\n"
            "                      || '。ADR-15 硬约束：换模型必须新建索引空间，'\n"
            "                      || '禁止在旧索引空间中混入新模型的向量（同维度混用'\n"
            "                      || '会导致检索静默劣化）。'"
        )
        + "    END IF;\n"
        "\n"
        "    -- 判据 2：异维度混用\n"
        "    IF actual_dims <> bound_dims THEN\n"
        + _raise_block(
            "'profile '\n"
            "                      || NEW.profile_id::text\n"
            "                      || ' 声明维度 ' || bound_dims::text\n"
            "                      || '，而这条向量是 ' || actual_dims::text || ' 维。'"
        )
        + "    END IF;\n"
        "\n"
        "    -- 判据 3：冗余列必须与向量实际维度一致\n"
        "    -- （`dimensions` 是冗余列，它会与 embedding 脱钩 —— 见 embedding_models.py）\n"
        "    IF NEW.dimensions <> actual_dims THEN\n"
        + _raise_block(
            "'chunk_embeddings.dimensions 是 '\n"
            "                      || NEW.dimensions::text\n"
            "                      || '，但 embedding 的实际维度是 ' || actual_dims::text\n"
            "                      || '。冗余列与向量脱钩会让容量统计失真。'"
        )
        + "    END IF;\n"
        "\n"
        "    RETURN NEW;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE TRIGGER {MIXING_GUARD_TRIGGER}\n"
        "BEFORE INSERT ON chunk_embeddings\n"
        f"FOR EACH ROW EXECUTE FUNCTION {MIXING_GUARD_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {MIXING_GUARD_TRIGGER} ON chunk_embeddings"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {MIXING_GUARD_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def hnsw_index_name(profile_id: str) -> str:
    """某个 profile 的 HNSW 索引名。

    **profile_id 里的 `-` 要换成 `_`**：PostgreSQL 的标识符允许 `-`，
    但前提是它被双引号包裹。不换会让所有调用点都必须记得加引号 ——
    而"忘记加引号"的症状是语法错误，不是静默问题（相对好发现），
    但换掉它更省事，且让名字可被直接拼进 SQL。

    **长度**：`ix_chunk_embeddings_hnsw_` (26) + 32（uuid hex 无横线）= 58，
    远小于 PG 的 63 字符标识符上限。
    """
    return INDEX_NAME_PREFIX + profile_id.replace("-", "_")


def hnsw_index_ddl(
    *,
    profile_id: str,
    dimensions: int,
    metric: str,
    m: int,
    ef_construction: int,
) -> str:
    """为**一个 profile** 生成部分 HNSW 索引 DDL。

    参数全部来自 `embedding_profiles` 那一行 —— 因此这个函数是
    "把 profile 的配置翻译成索引 DDL"的**唯一翻译点**。

    结构（实测通过，见模块 docstring 第二节）：

    ```sql
    CREATE INDEX <name> ON chunk_embeddings
      USING hnsw ((embedding::vector(<dims>)) <ops>)
      WITH (m = <m>, ef_construction = <ef>)
      WHERE profile_id = '<uuid>'
    ```

    **`WITH (m, ef_construction)` 必须显式给**：它们是**构建期**参数，
    改了必须重建索引。不给的话吃 pgvector 默认值（16 / 64），
    而"索引是按什么参数建的"就无从查起 —— ADR-15 判据 7 的
    质量回归需要它。

    **`WHERE profile_id = '...'` 是部分索引的谓词**：
    它让"这个索引只覆盖这个 profile 的向量"成为索引定义的一部分。
    ⚠️ 逐字核对过：**少了 `WHERE` 会让索引包含全部 profile 的向量**，
    而查询结果不变（谓词在查询里也会写）→ **退化不会被任何测试发现**。
    证据脚本按 `pg_indexes.indexdef` 逐条核对它含 ` WHERE `。

    **`metric` 必须在 `METRIC_INDEX_OPS` 里**：不在就抛错，
    而不是回退到 cosine —— 回退会让"索引建的是 cosine、profile 声明的是 L2"，
    于是查询用 L2 排序时**用不上这个索引**（静默退化成精确扫描）。
    """
    ops = METRIC_INDEX_OPS.get(metric)
    if ops is None:
        raise ValueError(
            f"未知的距离度量 {metric!r}；已知：{sorted(METRIC_INDEX_OPS)}。"
            "回退到默认度量会让索引与 profile 的声明脱钩，"
            "而症状是查询用不上索引（静默退化）。"
        )
    if dimensions <= 0:
        raise ValueError(f"维度必须为正，收到 {dimensions}")
    if m <= 0 or ef_construction <= 0:
        raise ValueError(
            f"HNSW 参数必须为正，收到 m={m} ef_construction={ef_construction}"
        )
    return (
        f"CREATE INDEX IF NOT EXISTS {hnsw_index_name(profile_id)}\n"
        f"  ON chunk_embeddings USING hnsw\n"
        f"  ((embedding::vector({dimensions})) {ops})\n"
        f"  WITH (m = {m}, ef_construction = {ef_construction})\n"
        f"  WHERE profile_id = '{profile_id}'"
    )


def hnsw_index_drop_ddl(profile_id: str) -> str:
    """删某个 profile 的 HNSW 索引（回收 profile 时用）。

    ⚠️ **当前零调用点**（收口时 AST 扫描确认）。它与 `hnsw_index_ddl`
    是**成对**的：建索引的路径已实现（`embedding_service.build_index`），
    而"回收 profile"（ADR-15 第 5 步的最后一步：确认无需回滚后删索引、
    降级数据）**尚未实现** —— 那是运维路径，需要先有"确认无需回滚"
    的人类签字，不属于本包的自动化范围。

    **为什么保留而不是删掉**：ADR-15 的 5 步切换流程里，
    "建新索引"与"删旧索引"是同一件事的两端，DDL 必须同源
    （否则回收时手写的 DROP 会跟建的名字不一致 —— 而 `DROP INDEX IF EXISTS`
    在名字错时**静默成功**，索引留在库里没人知道）。
    已登记为待定项：回收路径实现时直接调用本函数。
    """
    return f"DROP INDEX IF EXISTS {hnsw_index_name(profile_id)}"


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块的**跨表一致性**触发器（与 WP20 的 `consistency_items` 同形）。

    **为什么公开（去掉下划线）**：迁移的 `downgrade` 需要**只删本模块自己的
    函数**。判据：**这个对象是不是我建的** —— 是就删，不是就不删。
    本模块只有 1 个函数，且它是本模块建的，因此它与 `all_trigger_ddl()`
    目前内容一致；保留这个分层是为了与 WP20 同构，且将来若新增
    共用函数引用时不必改调用方。
    """
    function_ddl, trigger_ddl, drop_trigger, drop_function = mixing_guard_ddl()
    return [
        ("chunk_embeddings", function_ddl, trigger_ddl, drop_trigger, drop_function)
    ]


def all_trigger_ddl() -> list[tuple[str, str, str, str, str]]:
    """本模块需要建立的全部触发器。

    返回 `[(表名, 函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP), …]`。

    **本包只有 1 个触发器**（混用守卫），且**没有不可变触发器** ——
    理由见模块 docstring 第三节。这与 WP20 的 6 个形成对比，不是遗漏。
    """
    return consistency_items()


def trigger_names() -> tuple[str, ...]:
    """全部应存在的触发器名（验证脚本与测试用）。"""
    return (MIXING_GUARD_TRIGGER,)


def immutable_trigger_names() -> tuple[str, ...]:
    """本模块的不可变触发器名 —— **空元组，且这是有意的**。

    它的存在是为了让证据脚本能**显式断言"本包没有不可变触发器"**，
    而不是"忘了检查"。一个返回空集合的函数若不存在，
    证据脚本只能写"不检查" —— 而"不检查"与"检查了发现没有"看起来一样。
    """
    return ()


#: 本包的两张表（从模型取，避免两处各写一份）。
OWN_TABLES: Final[tuple[str, ...]] = (
    EmbeddingProfile.__tablename__,
    ChunkEmbedding.__tablename__,
)

__all__ = [
    "CONSISTENCY_SQLSTATE",
    "INDEX_NAME_PREFIX",
    "MIXING_GUARD_FUNCTION",
    "MIXING_GUARD_TRIGGER",
    "OWN_TABLES",
    "all_trigger_ddl",
    "consistency_items",
    "hnsw_index_ddl",
    "hnsw_index_drop_ddl",
    "hnsw_index_name",
    "immutable_trigger_names",
    "mixing_guard_ddl",
    "trigger_names",
]
