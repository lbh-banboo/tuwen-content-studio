"""向量容量估算（ADR-15 的度量口径）。

**为什么这是 platform 而不是 knowledge 模块的**：容量估算的口径来自
ADR-15（`docs/adr/ADR-15-向量检索选型门槛.md`），它管的是
**整个系统的 PG 容量规划**，不只是知识库。将来其它模块（WP26 的
生成上下文、WP34 的回退影响图）若也要存向量，必须用**同一套估算口径** ——
否则"总容量"会变成几个模块各自估算的和，而每个的假设都不同。

---

## 一、ADR-15 给出的估算口径（逐条照抄）

| 项 | ADR-15 原文 |
|---|---|
| 向量维度 | 1024 维 float32 |
| 单条向量原始大小 | 1024 × 4 字节 ≈ 4 KB |
| HNSW 索引放大 | 约 1.2~1.5 倍（§6.1 采用 **1.3 倍**作为规划值） |
| 单条 chunk 向量 + 索引合计 | 约 5.2~6 KB |
| 200 万 chunk 上限场景 | 向量本体约 8 GB；含索引约 10~12 GB |
| 加上 chunk 文本 | 约 30~40 GB |

ADR-15 的一个关键提醒原文：

> **索引放大不是可忽略项**。1.2~1.5 倍意味着 200 万 chunk 场景下
> 索引本身可能比向量本体更大，容量规划必须计入，
> 不能只按向量条数 × 4 KB 估算。

**因此本模块的估算必须包含放大系数**，且**把放大系数作为参数暴露出来** ——
ADR-15 的两个 `[待核实]` 之一就是它（`OP-45`）。

---

## 二、为什么估算函数要接受 `dimensions` 参数

ADR-15 按 1024 维规划，但 **OP-44（模型与维度）未定**。若把 1024 写死在
估算里，选了一个 1536 维的模型后，估算会**系统性低估 50%** ——
而低估的后果是容量规划不足，且**在写满磁盘之前没有任何信号**。

因此本模块的每个函数都接受 `dimensions`，并提供一个
`planned_dimensions` 参数用于"与规划值对比"。

---

## 三、阈值与预警

ADR-15 原文：

> | chunk 数（单 profile） | 处置 |
> | ≤ 200 万 | pgvector HNSW，单表 + 索引，不分区 |
> | > 200 万 | ① 按 embedding profile 分区（首选）；② 若单 profile 仍超限，
>   评估独立向量库（需单独立项与 ADR） |
>
> 监控信号：chunk 数逼近 200 万即触发容量复核（对应风险 R8）。

**"逼近"的定义本包定为 80%**（160 万）—— ADR-15 没有给数字，
而"逼近"没有数字就**无法触发**（这正是 ADR-15 的评审意见 🟡9 的原话：
"没有数字，无法判定"）。本包把它显式化为常量，
并登记为待定项（阈值需在 OP-166 的实测校准中一并确认）。

**为什么用 80% 而不是 90%**：容量复核是一个**人工动作**
（评估要不要分区），它需要提前量。90% 时距离触发只剩 20 万 chunk，
而"评估分区方案"是一个需要数天的工作 —— 提前量不够。

---

## 四、本模块**不做**的事

· **不做实测**。ADR-15 判据 1（阈值已校准）与判据 4（空间估算准确）
  要求"用真实数据量测"与"实测 200 万量级下实际占用"。本机不可能构造 ——
  它们登记为 OP-166。本模块交付的是**度量口径**（公式与常量），
  让实测有一个可对比的基线。
· **不读 `pg_total_relation_size`**。那是"实测"，属 OP-166 的验证脚本；
  本模块是纯计算（给定 chunk 数与维度 → 估算字节数），
  因此可被单测穷举，不需要真库。
"""

from __future__ import annotations

from typing import Any, Final

# ---------------------------------------------------------------------------
# ADR-15 的常量（**唯一来源**）
# ---------------------------------------------------------------------------

#: pgvector 量化门槛：单 profile 的 chunk 数。
#: ADR-15 原文：「单个 embedding profile 的 chunk 数 ≤ 200 万时使用
#: pgvector HNSW；超出后按 profile 分区，或评估独立向量库。」
PGVECTOR_CHUNK_THRESHOLD: Final[int] = 2_000_000

#: "逼近阈值"的触发比例。ADR-15 只说"逼近"，没给数字 ——
#: 没有数字就无法触发（评审意见 🟡9 的原话）。本包显式化为 80%。
CAPACITY_WARNING_RATIO: Final[float] = 0.8

#: float32 的字节数。pgvector 的 `vector` 用 4 字节浮点。
BYTES_PER_FLOAT32: Final[int] = 4

#: HNSW 索引放大系数的**规划值**。ADR-15 §6.1 采用 1.3 倍。
HNSW_AMPLIFICATION_PLANNED: Final[float] = 1.3

#: HNSW 放大系数的**实测区间**（ADR-15 原文「约 1.2~1.5 倍」）。
#: 估算给区间而不只给一个数：**规划用中值、容量上限用上界** ——
#: 只给一个数会让"最坏情况"无处表达。
HNSW_AMPLIFICATION_MIN: Final[float] = 1.2
HNSW_AMPLIFICATION_MAX: Final[float] = 1.5

#: 规划用的向量维度（ADR-15 §6.1 的 1024 维 float32）。
#: **它是规划值不是约束**（OP-44 未定）—— 所有估算函数都接受
#: 真实维度作为参数。
PLANNED_EMBEDDING_DIMENSIONS: Final[int] = 1024

#: 每条 chunk 的文本平均字节数估算。ADR-15 §6.1 的口径是
#: "文本 ÷ 500 字/chunk" —— 中文 UTF-8 一个字符 3 字节。
BYTES_PER_CHUNK_TEXT: Final[int] = 500 * 3

#: pgvector 每行的元组开销（头部 + null bitmap + 对齐）。
#: ADR-15 没有列它（它的口径是"向量本体 + 索引"），但**真实占用**里
#: 它是存在的。本模块显式给一个保守值并**在报告里标注它是估算的
#: 额外项**，而不是悄悄塞进放大系数里 —— 塞进去会让"放大系数"
#: 与 ADR-15 的值对不上，而那个值是要被实测校准的（OP-45）。
ROW_OVERHEAD_BYTES: Final[int] = 64


def vector_bytes(*, dimensions: int) -> int:
    """单条向量的原始字节数（不含索引与行开销）。

    `dimensions × 4`。ADR-15 原文：「1024 × 4 字节 ≈ 4 KB」。
    """
    if dimensions <= 0:
        raise ValueError(f"维度必须为正，收到 {dimensions}")
    return dimensions * BYTES_PER_FLOAT32


def estimate_vector_bytes(
    *,
    chunk_count: int,
    dimensions: int,
    amplification: float = HNSW_AMPLIFICATION_PLANNED,
) -> dict[str, Any]:
    """给定 chunk 数与维度，估算向量本体 + HNSW 索引的占用。

    返回一个**分项**字典（不是单个数字）：分项让"哪一项超预期"
    可查，而单个数字只能回答"总共多少"。

    | 字段 | 含义 |
    |---|---|
    | `vectorBytes` | 向量本体（`chunk × dims × 4`） |
    | `indexBytes` | HNSW 索引估算（`vectorBytes × (amplification - 1)`） |
    | `rowOverheadBytes` | 行开销（`chunk × ROW_OVERHEAD_BYTES`） |
    | `totalBytes` | 三者之和 |
    | `totalWithTextBytes` | 再加 chunk 文本 |
    | `amplification` | 本次用的放大系数（**回报出来**，否则"这个数是怎么来的"无从查起） |

    **`indexBytes` 的算法**：ADR-15 说"含索引约 1.3 倍"，
    即索引使总量变成 `vectorBytes × 1.3`。因此索引本身是
    `vectorBytes × 0.3`。**这个"扣掉本体"的算法必须写清楚** ——
    把 `amplification` 直接乘在总量上（`chunk × dims × 4 × 1.3`）
    与"本体 + 索引"在数值上等价，但分项会变成一个包含本体的
    怪数字，读的人会以为索引比向量还大。
    """
    if chunk_count < 0:
        raise ValueError(f"chunk_count 不能为负，收到 {chunk_count}")
    if amplification < 1.0:
        raise ValueError(
            f"放大系数不能小于 1.0（索引不会让占用变小），收到 {amplification}"
        )
    body = chunk_count * vector_bytes(dimensions=dimensions)
    index = int(body * (amplification - 1.0))
    overhead = chunk_count * ROW_OVERHEAD_BYTES
    total = body + index + overhead
    return {
        "chunkCount": chunk_count,
        "dimensions": dimensions,
        "amplification": amplification,
        "vectorBytes": body,
        "indexBytes": index,
        "rowOverheadBytes": overhead,
        "totalBytes": total,
        "totalWithTextBytes": total + chunk_count * BYTES_PER_CHUNK_TEXT,
        "plannedDimensions": PLANNED_EMBEDDING_DIMENSIONS,
    }


def capacity_warning(
    chunk_count: int, *, threshold: int = PGVECTOR_CHUNK_THRESHOLD
) -> dict[str, Any]:
    """容量预警（ADR-15 判据 3「预警有效」的落点）。

    ADR-15 原文：「构造逼近阈值的测试数据，确认触发容量复核告警」。

    返回：

    | 字段 | 含义 |
    |---|---|
    | `level` | `OK` / `WARNING` / `EXCEEDED` |
    | `ratio` | `chunkCount / threshold` |
    | `message` | 人话说明（含**该做什么**） |

    **为什么 `level` 是三值而不是布尔**：布尔无法区分
    "该开始评估分区"（WARNING，还能用）与"必须处置"（EXCEEDED，
    已超 ADR-15 的适用范围）。两者的动作完全不同 ——
    合并成一个"有告警"会让运维不知道该立刻动手还是可以先排期。
    """
    if threshold <= 0:
        raise ValueError(f"阈值必须为正，收到 {threshold}")
    ratio = chunk_count / threshold
    if chunk_count >= threshold:
        level = "EXCEEDED"
        message = (
            f"单 profile 的 chunk 数（{chunk_count}）已达到/超过 ADR-15 阈值"
            f"（{threshold}）。按 ADR-15：① 优先按 embedding profile 分区；"
            "② 若单 profile 仍超限，评估独立向量库（需单独立项与 ADR）。"
        )
    elif ratio >= CAPACITY_WARNING_RATIO:
        level = "WARNING"
        message = (
            f"单 profile 的 chunk 数（{chunk_count}）已逼近 ADR-15 阈值"
            f"（{threshold}，{ratio:.1%}）。触发容量复核："
            "评估分区方案与索引维护成本（ADR-15 判据 1）。"
        )
    else:
        level = "OK"
        message = (
            f"单 profile 的 chunk 数（{chunk_count}）在阈值"
            f"（{threshold}）的 {ratio:.1%}，无需处置。"
        )
    return {
        "level": level,
        "ratio": ratio,
        "chunkCount": chunk_count,
        "threshold": threshold,
        "warningRatio": CAPACITY_WARNING_RATIO,
        "message": message,
    }


def estimate_space_range(
    *, chunk_count: int, dimensions: int
) -> dict[str, Any]:
    """按 ADR-15 的放大系数**区间**给出占用范围。

    规划用 `HNSW_AMPLIFICATION_PLANNED`（1.3），容量上限用
    `HNSW_AMPLIFICATION_MAX`（1.5）—— 只给中值会让"最坏情况"无处表达，
    而容量规划的失败模式恰恰是最坏情况。

    ADR-15 原文（200 万 chunk / 1024 维）：

    > 向量本体约 8 GB；含索引约 10~12 GB；加上 chunk 文本约 30~40 GB

    本函数的输出应能与这段话对上 —— 对不上说明公式或常量错了。
    测试里有一条专门做这个交叉核对（**这是"估算准确"的最低成本验证**，
    它不需要 200 万行数据）。
    """
    low = estimate_vector_bytes(
        chunk_count=chunk_count,
        dimensions=dimensions,
        amplification=HNSW_AMPLIFICATION_MIN,
    )
    high = estimate_vector_bytes(
        chunk_count=chunk_count,
        dimensions=dimensions,
        amplification=HNSW_AMPLIFICATION_MAX,
    )
    planned = estimate_vector_bytes(
        chunk_count=chunk_count,
        dimensions=dimensions,
        amplification=HNSW_AMPLIFICATION_PLANNED,
    )
    return {
        "chunkCount": chunk_count,
        "dimensions": dimensions,
        "planned": planned,
        "low": low,
        "high": high,
        "amplificationRange": [HNSW_AMPLIFICATION_MIN, HNSW_AMPLIFICATION_MAX],
    }


def human_bytes(value: int) -> str:
    """把字节数渲染成人话（供日志与证据文件）。

    **用 1024 进制并标注单位**：ADR-15 用的是 GB，而"GB"在
    存储语境下通常是 10^9（厂商口径）还是 2^30（操作系统口径）
    会造成 7% 的差异 —— 在 30~40 GB 的量级上那是 2~3 GB。
    本函数用 2^30（GiB 口径）并**在字符串里写 `GiB`**，
    让口径不可能被误读。
    """
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    size = float(value)
    index = 0
    while size >= 1024.0 and index < len(units) - 1:
        size /= 1024.0
        index += 1
    if index == 0:
        return f"{int(size)} {units[index]}"
    return f"{size:.1f} {units[index]}"


__all__ = [
    "BYTES_PER_CHUNK_TEXT",
    "BYTES_PER_FLOAT32",
    "CAPACITY_WARNING_RATIO",
    "HNSW_AMPLIFICATION_MAX",
    "HNSW_AMPLIFICATION_MIN",
    "HNSW_AMPLIFICATION_PLANNED",
    "PGVECTOR_CHUNK_THRESHOLD",
    "PLANNED_EMBEDDING_DIMENSIONS",
    "ROW_OVERHEAD_BYTES",
    "capacity_warning",
    "estimate_space_range",
    "estimate_vector_bytes",
    "human_bytes",
    "vector_bytes",
]
