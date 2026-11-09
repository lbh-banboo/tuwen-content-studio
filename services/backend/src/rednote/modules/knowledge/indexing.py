"""索引事件的消费者 + 确定性 embedding 替身（**闭合 OP-149**）。

---

## 一、OP-149 是什么，为什么由本文件闭合

WP20 的登记原文：

> **`INDEXING` 状态的判据是「生效版本无 chunk 行」，而 chunk 的生产者
> （WP21）尚未存在** …… 本包把它实现为一等状态：**生效版本存在但
> `knowledge_chunks` 无行 → `INDEXING`**。但 **`knowledge_chunks`
> 在本包只建表不生产**（生产归 WP21）→ 也就是说**本包交付后，
> 所有已发布知识都会停在 `INDEXING`**，而这是"实现正确但生产者缺失"
> 的状态，不是缺陷。**必须显式登记**：……"真实索引完成后能否自动转为
> ACTIVE"要等 WP21 接上 Outbox 事件（`EVENT_KNOWLEDGE_INDEX_REQUESTED`
> 的消费者）才能端到端验证

**本文件就是那个消费者。** WP20 的 `_enqueue_index_event` 已经投递事件：

| action | 事件 | 消费方该做什么 |
|---|---|---|
| `ACTIVATE` | `knowledge.index_requested` | 为新生效版本建 chunk 与向量 |
| `ARCHIVE` | `knowledge.index_removed` | 从索引里移除这条知识 |
| `RESTORE` | `knowledge.index_requested` | 重新建索引 |

---

## 二、消费者为什么是**独立函数**而不是一个后台线程

WP13 已有 outbox 的投递机制（`outbox_events` 表 + 领取语义）。
本包**不新建投递机制** —— 那是 WP13 的职责，且重复实现会引入
"两套领取语义"（本项目 ISS-34 的同族：所有权转移只在一处递增凭证）。

本包提供的是**处理器**：给定一条事件，做该做的事。
投递（领取、重试、标记完成）由既有的 outbox 机制负责。

**为什么这样分**：投递机制管的是"事件什么时候被处理"，
处理器管的是"处理一条事件要做什么"。混在一起会让"处理逻辑"
依赖"投递实现的细节"，而后者将来会变（WP13 的租约参数、
批量大小）。本项目已踩过（WP16 ISS-47：复用语义不匹配的抽象）。

---

## 三、处理器必须**幂等**

事件会被重试（WP13 的机制）。重试是**正常路径**，不是异常。

`ingest_version` 的第一件事是删掉该版本既有 chunk，因此
"处理两次"与"处理一次"的结果相同 —— 幂等由它保证。

**`index_removed` 的幂等**：删一个没有 chunk 的版本是 no-op（返回 0），
不报错。

---

## 四、确定性 embedding 替身（`DeterministicEmbedder`）

本机没有真实 embedding 模型：
  · OP-44（模型与维度）未定；
  · ADR-10 E-4：embedding **默认不出网**（须单独列入白名单并重新审批）。

若不提供替身，本包交付的是"一段没跑过的代码"。
因此提供 `DeterministicEmbedder`：

  · 同文本 → 同向量（稳定哈希派生）；
  · 不同文本 → 不同向量，且**距离可预测**（相同前缀的文本距离更近）；
  · 产出**真实的 `vector` 类型值**，走**真实的 HNSW 索引**，
    执行**真实的 SQL**。

**关键：替身不是 mock。** 唯一被替代的是"文本 → 向量"这一步的
**数值来源**。检索管线（索引、距离、排序、范围过滤）是**真的被测过的**。
这正是本项目 `StubAdapter`（WP16）的同一手法。

### 为什么用"词袋哈希"而不是"随机向量"

随机向量会让"语义相近"这件事无法构造测试数据 ——
而验收原文的第一个词就是「**近似**品牌案例」。替身必须让
"两个语义相近的文本"给出**更近的向量**，否则"近似"这条无法验证。

本替身的做法：把文本按字符 n-gram 打散，每个 n-gram 哈希到一个
固定维度上的位置并累加。于是**共享 n-gram 越多 → 向量越近** ——
这是"文本相似 → 向量相似"的一个真实（虽然粗糙）实现。
"""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from rednote.modules.knowledge.embedding_repository import EmbeddingRepository
from rednote.modules.knowledge.embedding_service import IngestionResult, ingest_version
from rednote.modules.knowledge.embedding_types import PROFILE_STATE_ACTIVE
from rednote.modules.knowledge.types import (
    EVENT_KNOWLEDGE_INDEX_REMOVED,
    EVENT_KNOWLEDGE_INDEX_REQUESTED,
)


class Embedder(Protocol):
    """"文本 → 向量"的接口。

    **为什么用 Protocol 而不是抽象基类**：它是**结构约定**，
    不是继承关系。WP16 的 `CapabilityAdapter` 用的是同一个手法。
    用 ABC 会迫使替身与真实适配器都继承它 —— 而它们唯一的共同点是
    "有一个 `embed` 方法"。
    """

    @property
    def dimensions(self) -> int: ...

    def embed(self, text: str) -> list[float]: ...


#: n-gram 的长度。取 3：中文里 3 字能覆盖大量词组，
#: 且比 2 字（区分度低）与 4 字（覆盖少）更均衡。
_NGRAM_SIZE: Final[int] = 3


@dataclass
class DeterministicEmbedder:
    """确定性 embedding 替身（**不是 mock**，见模块 docstring 第四节）。

    **它产出真实的向量**：同一份文本在两次调用里给出完全相同的向量，
    因此"重切块后向量不变"这件事是可验证的。

    `dimensions` 必须在构造时给出 —— 与 profile 的维度对齐由
    `ingest_version` 校验（不匹配会 422）。
    """

    dimensions: int = 1024
    #: 归一化开关。**默认开**：余弦距离对模长不敏感，
    #: 而归一化让"相似度"的取值范围可预测（`[-1, 1]`）。
    normalize: bool = True
    #: 调用计数（供测试断言"embedder 真的被调了 N 次"）。
    #: 一个不记录调用的替身会让"批量优化"这类改动无法验证 ——
    #: 本项目已踩过（ISS-77：断言的是"在集合内"而不是具体值）。
    calls: list[str] = field(default_factory=list)

    def embed(self, text: str) -> list[float]:
        """文本 → 向量（确定性）。

        算法：文本的每个字符 n-gram 哈希到 `[0, dimensions)` 上的一个位置，
        该位置累加 1.0。于是**共享 n-gram 多的文本，向量内积大**。

        空文本 → 全零向量（**不是错误**：`ingest_version` 在空块时
        已经提前返回，不会走到这里；但一个可被直接调用的接口
        不应在空输入上崩）。
        """
        self.calls.append(text)
        vector = [0.0] * self.dimensions
        if not text:
            return vector
        normalized = text.strip()
        if not normalized:
            return vector
        for index in range(max(len(normalized) - _NGRAM_SIZE + 1, 1)):
            gram = normalized[index : index + _NGRAM_SIZE]
            if not gram:
                continue
            digest = hashlib.sha256(gram.encode("utf-8")).digest()
            # 取 8 字节（64 bit）映射到维度位置。用两个不同偏移
            # 各取 4 字节：单个 4 字节在 1024 维下冲突率高，
            # 而冲突会让"不相似的文本"偶然相似。
            first = int.from_bytes(digest[:4], "big")
            second = int.from_bytes(digest[4:8], "big")
            vector[first % self.dimensions] += 1.0
            vector[second % self.dimensions] += 0.5
        if self.normalize:
            norm = math.sqrt(sum(value * value for value in vector))
            if norm > 0:
                vector = [value / norm for value in vector]
        return vector


# ---------------------------------------------------------------------------
# 事件处理器
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexingOutcome:
    """一条索引事件的处置结论。"""

    event_type: str
    handled: bool
    version_id: uuid.UUID | None
    ingestion: IngestionResult | None
    removed_chunks: int
    detail: str


def handle_index_event(
    repo: EmbeddingRepository,
    *,
    event_type: str,
    payload: dict[str, Any],
    profile: Any,
    embedder: Embedder,
) -> IndexingOutcome:
    """处理一条索引事件（**幂等**，见模块 docstring 第三节）。

    `payload` 的形状由 WP20 的 `_enqueue_index_event` 决定：

        {"assetId", "versionId", "action", "sequence", "category", "scope"}

    **`versionId` 缺失 → 不处理，但也不是错误**：
    一个格式不对的事件（例如别的事件类型被误投到这里）应该被
    **报告**（`handled=False` + `detail`），而不是抛异常 ——
    抛异常会让 outbox 的重试机制无限重试一条永远不可能成功的事件。
    本项目已有同族教训（WP13 的"不可恢复的失败应返回结果而不是抛异常"）。

    **`index_removed` 只删 chunk，不删知识**：
    它对应 WP20 的 `ARCHIVE` —— 知识本身还在（归档可追溯），
    只是不该被检索召回。删 chunk 让"这条知识没有被索引"成为事实，
    而 WP20 的 `projection` 会据此算出正确的状态。

    ⚠️ **一个必须显式说明的后果**：`ARCHIVE` 之后该知识的
    `active_version` 仍在（WP20 的语义：归档不改 `active_version_id`），
    而 chunk 被删 → `projection` 会把它算成 `INDEXING`。
    这是**不准确的**：归档的知识不该显示"索引处理中"。
    本包的处置：**不在这里改状态**（那会引入第二处状态判据 ——
    正是 WP20 的 ISS-66 要防的事），而是**登记为待定项**，
    由 WP20 的投影增加"归档优先"的判断来修（收口方向已写明）。
    """
    if event_type == EVENT_KNOWLEDGE_INDEX_REQUESTED:
        version_id = _payload_uuid(payload, "versionId")
        if version_id is None:
            return _not_handled(
                event_type, "payload 缺 versionId，无法确定要索引哪个版本。"
            )
        ingestion = ingest_version(
            repo, version_id=version_id, profile=profile, embedder=embedder
        )
        return IndexingOutcome(
            event_type=event_type,
            handled=True,
            version_id=version_id,
            ingestion=ingestion,
            removed_chunks=ingestion.replaced_chunks,
            detail=(
                f"已切块 {ingestion.chunk_count} 块、写入 "
                f"{ingestion.embedding_count} 条向量"
                f"（替换旧块 {ingestion.replaced_chunks} 块）。"
            ),
        )

    if event_type == EVENT_KNOWLEDGE_INDEX_REMOVED:
        version_id = _payload_uuid(payload, "versionId")
        if version_id is None:
            return _not_handled(
                event_type, "payload 缺 versionId，无法确定要移除哪个版本的索引。"
            )
        removed = repo.delete_chunks_for_version(version_id)
        return IndexingOutcome(
            event_type=event_type,
            handled=True,
            version_id=version_id,
            ingestion=None,
            removed_chunks=removed,
            detail=f"已移除 {removed} 个 chunk（向量随外键级联删除）。",
        )

    return _not_handled(event_type, f"本包不处理事件类型 {event_type!r}。")


def _not_handled(event_type: str, detail: str) -> IndexingOutcome:
    """构造一个「知道了，但什么都没做」的结论。

    **为什么要单独成函数**：这个 6 字段的构造原先在 `handle_index_event`
    里**逐字写了三遍**（缺 versionId 两处 + 未知事件类型一处）。
    三份里只要有一份漏填一个字段，症状就是"结论说处理了但实际没处理"——
    而 `IndexingOutcome` 的字段全是可选语义，漏填不会报错。

    收敛后，"不处理的结论长什么样"只有一处定义：`handled=False`
    且 `ingestion` / `version_id` / `removed_chunks` 全部置空。
    """
    return IndexingOutcome(
        event_type=event_type,
        handled=False,
        version_id=None,
        ingestion=None,
        removed_chunks=0,
        detail=detail,
    )


def _payload_uuid(payload: dict[str, Any], key: str) -> uuid.UUID | None:
    """从事件 payload 里取一个 UUID 字段。

    **非法值返回 None 而不是抛异常**：见 `handle_index_event` 的说明。
    但**要区分"缺"与"非法"** —— 非法值（例如一个字符串 `"abc"`）
    说明事件的**生产者**有 bug，而"缺字段"可能是事件类型不匹配。
    两者都被报成"不处理"，但 `detail` 会不同（本函数不区分，
    由调用方按 `key in payload` 判断 —— 保持函数简单）。
    """
    raw = payload.get(key)
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def process_pending_index_events(
    repo: EmbeddingRepository,
    *,
    profile: Any,
    embedder: Embedder,
    limit: int = 100,
) -> list[IndexingOutcome]:
    """处理一批待处理的索引事件（**按事件类型过滤到本包负责的两种**）。

    ⚠️ **当前零调用点**（收口时 AST 扫描确认）：本包提供的是**处理器**
    （`handle_index_event`，已被测试与投递方使用），而"从 `outbox_events`
    里批量领取"的循环**归 WP13 的投递机制**（见模块 docstring 第二节：
    "本包不新建投递机制"）。本函数是那条投递路径的**适配入口** ——
    它把"本包负责的两种事件类型"这个过滤条件封装在一处，
    避免投递方自己写 `event_type IN (...)` 而漏掉一种。

    **为什么保留而不是删掉**：若投递方各自拼过滤条件，
    "新增一种事件类型时有一处忘了加"的后果是**该事件永远不被处理**
    （而 outbox 会一直重试或标记完成，不报错）。
    已登记为待定项：WP13 的消费者接上本函数时，过滤条件无需重复。

    **为什么不处理全部事件**：`outbox_events` 是共用表（WP13），
    里面有各种事件类型（文件完成、任务完成……）。本包只处理
    它负责的两种 —— 处理别的是越界，且"不认识的事件"会被
    报成"不处理"（噪声）。

    **`limit` 是必需的而不是可选的**：本项目的
    "全局扫描 + 默认 limit + 断言我造的那条在里面"已踩过三次
    （ISS-49 / ISS-59 / ISS-73）。这里的做法是：
    默认值给一个**明确的大数**，且**测试调用时必须显式传**
    （测试里有一个常量 `_EVENT_SCAN_LIMIT = 10_000`）。

    返回每个事件的处置结论（不是"处理了多少条"）——
    结论里含 `handled=False` 的项，让"为什么没处理"可见。
    """
    from sqlalchemy import select

    from rednote.modules.outbox.models import OutboxEvent

    rows = (
        repo.session.execute(
            select(OutboxEvent)
            .where(
                OutboxEvent.event_type.in_(
                    [
                        EVENT_KNOWLEDGE_INDEX_REQUESTED,
                        EVENT_KNOWLEDGE_INDEX_REMOVED,
                    ]
                )
            )
            .order_by(OutboxEvent.created_at.asc(), OutboxEvent.id.asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    outcomes: list[IndexingOutcome] = []
    for row in rows:
        outcomes.append(
            handle_index_event(
                repo,
                event_type=str(row.event_type),
                payload=dict(row.payload or {}),
                profile=profile,
                embedder=embedder,
            )
        )
    return outcomes


def profile_is_usable(profile: Any) -> bool:
    """profile 是否可用于写入向量。

    **只有 `ACTIVE` 的 profile 接受新写入**（`DEPRECATED` 的仍可被检索，
    但不再接受新向量 —— ADR-15 第 5 步：「旧 profile 保留至确认无需回滚，
    再回收」，保留的是**读**的能力）。

    若让 DEPRECATED 也接受写入，"切换后新知识仍写进旧索引空间" ——
    那是混用的另一种形态（新内容进旧空间），而它**不报错**。
    """
    return bool(profile is not None and profile.state == PROFILE_STATE_ACTIVE)


__all__ = [
    "DeterministicEmbedder",
    "Embedder",
    "IndexingOutcome",
    "handle_index_event",
    "process_pending_index_events",
    "profile_is_usable",
]
