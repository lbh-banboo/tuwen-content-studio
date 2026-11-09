"""WP18 的不可变值类型与受控取值域。

**为什么单独一个模块**：这些取值同时被 `schema.py`（字段规格）、
`models.py`（表约束）、`repository.py`（存储）、`decomposer.py`（模型输出）
与 `service.py`（业务规则）使用。定义在 service 里会让其余几个反向导入 service ——
本模块的分层刻意单向：

    types ← schema
    types ← decomposer
    types ← models
    types ← repository
    types ← service
    types ← api

与 WP11~WP17 一致：**跨层传递的值用 frozen dataclass 而不是 ORM 对象**
（ORM 实例绑在 session 上，session 一关就 `DetachedInstanceError`）。

---

## 本包的两条"不许省略"的判据（验收项的结构性落点）

### 1. `category` 不可省略，且 `SOURCE_FACT`/`EXTERNAL_EVIDENCE` 必须带证据

验收原文之一是「**分类来源**」。需求 §5.2 的加厚输出分四类：
原文事实 / 外部证据 / 策略建议 / 待确认项。协议 §5.1 的原文是：

> 至少一个有效来源字段，字段级约束由 Schema 校验；
> **没有来源的建议不能改成 SOURCE_FACT 而不补证据**。

因此本模块把"分类 + 证据"做成一条**数据库级 CHECK**（见 `models.BriefClaim`），
而不是一句约定：`SOURCE_FACT` 与 `EXTERNAL_EVIDENCE` 必须有
`evidence_segment_id` 或 `evidence_url` 之一。绕过 service 直接写 SQL 也进不去。

**为什么 `UNCONFIRMED` 不需要证据**：它正是"还没有来源、需要人去补"的那一类。
要求它带证据等于取消这一类。

**为什么 `STRATEGY` 不需要证据**：需求 §5.2 明确"不当作产品事实，运营选用" ——
它是建议，不是事实断言。要求建议带证据会把"人群洞察"这类合理建议挡在门外。

### 2. `severity='BLOCK'` 的疑问未解决就阻断

验收原文之三是「**确认版本闭环**」。需求 §5.3 的完成条件第一条是
"必填字段已填写或明确不适用"，§6 的表定义写着"核心疑问未解决阻断步骤 1"。

`severity` 的两级不是"重要/不重要"，而是**失败模式不同**：

| severity | 含义 | 缺失时的失败模式 |
|---|---|---|
| `BLOCK` | 该疑问不解决，下游产出**必然失真或违规** | 编造事实 / 触碰禁提项 |
| `WARN` | 该疑问不解决，下游产出**变差但仍可用** | 表达弱、卖点不突出 |

因此固化版本时只阻断 `BLOCK`，`WARN` 允许带着走（否则"必须三项卖点摘要"
会变成一个无法满足的硬门槛，而需求 §5.3 明确允许"原材料不足三项时如实记录"）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

# ---------------------------------------------------------------------------
# 事实分类（数据结构 02 §6 的 category；需求 §5.2 的四类加厚输出）
# ---------------------------------------------------------------------------

CATEGORY_SOURCE_FACT: Final[str] = "SOURCE_FACT"
CATEGORY_EXTERNAL_EVIDENCE: Final[str] = "EXTERNAL_EVIDENCE"
CATEGORY_STRATEGY: Final[str] = "STRATEGY"
CATEGORY_UNCONFIRMED: Final[str] = "UNCONFIRMED"

CATEGORIES: Final[tuple[str, ...]] = (
    CATEGORY_SOURCE_FACT,
    CATEGORY_EXTERNAL_EVIDENCE,
    CATEGORY_STRATEGY,
    CATEGORY_UNCONFIRMED,
)

# **需要证据的分类**。抽成集合而不是散写两个 if：
# 将来新增分类（例如 `DERIVED`）时，"要不要证据"是一个必须显式回答的问题 ——
# 散写的话新增分类会静默地落进"不需要证据"那一侧。
CATEGORIES_REQUIRING_EVIDENCE: Final[frozenset[str]] = frozenset(
    {CATEGORY_SOURCE_FACT, CATEGORY_EXTERNAL_EVIDENCE}
)

CATEGORY_LIST_SQL: Final[str] = ", ".join(f"'{name}'" for name in sorted(CATEGORIES))
EVIDENCE_CATEGORY_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(CATEGORIES_REQUIRING_EVIDENCE)
)

# ---------------------------------------------------------------------------
# 确认状态（协议 §5.1 的 resolution ↔ 数据结构 02 §6 的 resolution_status）
# ---------------------------------------------------------------------------
# 名字不同、语义相同。本模块按**数据结构 02** 的列名落库（它管表结构），
# 协议层输出时映射成 `resolution`（协议 §5.1 的字段名）。
# 这是本包发现的第二处规格不一致，已登记为待定项。

RESOLUTION_PENDING: Final[str] = "PENDING"
RESOLUTION_CONFIRMED: Final[str] = "CONFIRMED"
RESOLUTION_NOT_APPLICABLE: Final[str] = "NOT_APPLICABLE"

RESOLUTIONS: Final[tuple[str, ...]] = (
    RESOLUTION_PENDING,
    RESOLUTION_CONFIRMED,
    RESOLUTION_NOT_APPLICABLE,
)
RESOLUTION_LIST_SQL: Final[str] = ", ".join(f"'{name}'" for name in sorted(RESOLUTIONS))

# **刻意不提供 `RESOLUTIONS_SETTLED`**：这个集合（"不再需要人回答"）
# 与服务层的 `ISSUE_SETTLED_STATES` 看起来对称，但**没有任何消费点** ——
# 结论的处置与疑问的处置走的是两条不同的校验路径
# （前者只判取值合法，后者才判"处置必须填理由"）。
# 一个零消费的集合会让人以为"结论也要过那道门"，而实际不会。

# ---------------------------------------------------------------------------
# 疑问（brief_issues）
# ---------------------------------------------------------------------------

SEVERITY_BLOCK: Final[str] = "BLOCK"
SEVERITY_WARN: Final[str] = "WARN"
SEVERITIES: Final[tuple[str, ...]] = (SEVERITY_BLOCK, SEVERITY_WARN)
SEVERITY_LIST_SQL: Final[str] = ", ".join(f"'{name}'" for name in sorted(SEVERITIES))

ISSUE_OPEN: Final[str] = "OPEN"
ISSUE_RESOLVED: Final[str] = "RESOLVED"
# "这条疑问不需要回答"（需求 §207："确实不适用的字段可由运营明确标记'不适用'
# 并填写原因"）。与 RESOLVED 分开是因为两者对下游的含义不同：
# RESOLVED 表示"事实已补齐"，DISMISSED 表示"这个字段对本次生产无关"。
ISSUE_DISMISSED: Final[str] = "DISMISSED"

ISSUE_STATES: Final[tuple[str, ...]] = (ISSUE_OPEN, ISSUE_RESOLVED, ISSUE_DISMISSED)
ISSUE_STATE_LIST_SQL: Final[str] = ", ".join(f"'{name}'" for name in sorted(ISSUE_STATES))

# 已处置的疑问状态（不再阻断）。
ISSUE_SETTLED_STATES: Final[frozenset[str]] = frozenset(
    {ISSUE_RESOLVED, ISSUE_DISMISSED}
)

# ---------------------------------------------------------------------------
# 字段状态（brief_drafts.structured_fields 的每个值）
# ---------------------------------------------------------------------------
# **值与其状态必须在一起**（同一个 jsonb 对象里），不能分成两列：
# 分成两列就会出现"值在 A 列、状态在 B 列"的对不齐 —— 改了值忘了改状态，
# 而这类错误不会报错，只会让固化校验基于错误的完整度判断放行。

FIELD_FILLED: Final[str] = "FILLED"
FIELD_MISSING: Final[str] = "MISSING"
FIELD_NOT_APPLICABLE: Final[str] = "NOT_APPLICABLE"

FIELD_STATUSES: Final[tuple[str, ...]] = (
    FIELD_FILLED,
    FIELD_MISSING,
    FIELD_NOT_APPLICABLE,
)

# **刻意不提供 `FIELD_STATUS_LIST_SQL`**：其余取值域各有一个 `*_LIST_SQL`
# 是为了拼 CHECK 约束（`status IN (...)`），但字段状态住在
# `brief_drafts.structured_fields` **内部**（一个 jsonb 对象里），
# CHECK 无法逐键校验它 —— 校验点是 `schema.normalize_field_values`。
# 留着那个常量只会让人以为"数据库层已经管住了"。

# ---------------------------------------------------------------------------
# 输入完整度（brief_drafts.source_state / brief_versions.source_state）
# ---------------------------------------------------------------------------
# **本包对 WP17 `state='PARTIAL'` 的显式处理**（硬约束，见 §模块说明）。
#
# WP17 的三个消费结论（`docs/m1/WP17-文档解析与OCR说明.md` §10.2）：
#
# | parse_runs.state | 本包该做什么 |
# |---|---|
# | `SUCCEEDED` | 正常消费全部片段 |
# | `PARTIAL` | **必须把缺失暴露出来**，不得当成完整输入 |
# | `FAILED` | **不得消费**（不调模型、不产出草稿） |
#
# 落地方式有三层，缺一不可：
#   1. 本常量集把 PARTIAL 做成**独立取值**（不是 SUCCEEDED 的变体，
#      也不提供会把两者合并的 `is_complete` 便利属性 —— 便利属性正是
#      让 PARTIAL 被当成 SUCCEEDED 的东西，与 WP17 的
#      `PARSE_COMPLETE_STATES` 刻意不含 PARTIAL 同一条理由）；
#   2. `brief_drafts` 上一条 CHECK：`source_state='PARTIAL'` 时
#      **必须**有至少一条疑问记录（数据库层的结构性保证）；
#   3. 固化版本时把 `source_state` 复制进 `brief_versions` ——
#      否则固化就等于把残缺洗白了。

SOURCE_STATE_SUCCEEDED: Final[str] = "SUCCEEDED"
SOURCE_STATE_PARTIAL: Final[str] = "PARTIAL"
SOURCE_STATE_FAILED: Final[str] = "FAILED"
# 尚未解析（例如项目刚创建时的空草稿）。
SOURCE_STATE_NONE: Final[str] = "NONE"

SOURCE_STATES: Final[tuple[str, ...]] = (
    SOURCE_STATE_SUCCEEDED,
    SOURCE_STATE_PARTIAL,
    SOURCE_STATE_FAILED,
    SOURCE_STATE_NONE,
)
SOURCE_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(SOURCE_STATES)
)

# **刻意不提供 `CONSUMABLE_SOURCE_STATES` 这类"可消费集合"常量**：
# `service.py` 里已有一个 `_CONSUMABLE_STATES`，但它的元素是
# **`parse_runs.state`**（`SUCCEEDED` / `PARTIAL` 都算"可消费，只是路径不同"），
# 与本模块的 `source_state`（"这次解构的输入完整度"）不是同一个取值域。
# 两个同名不同义的集合放在一起，下一个人一定会把
# `source_state` 拿去查 `parse_runs.state` 的表 ——
# 那是本包最容易犯的错（"PARTIAL 是输入残缺，不是解析失败"）。
# 因此这里只保留枚举本身，把"谁能消费"的判断留在它唯一的使用点。

# ---------------------------------------------------------------------------
# 解构与加厚的任务 kind（WP13 的注册表按 kind 索引）
# ---------------------------------------------------------------------------

KIND_BRIEF_PARSE: Final[str] = "BRIEF_PARSE"
KIND_BRIEF_ENRICH: Final[str] = "BRIEF_ENRICH"

QUEUE_BRIEF: Final[str] = "brief"

# 幂等作用域（协议 §1.3）。两个端点各自一个作用域 ——
# 共用一个会让"同一文件的解析"与"同一版本的加厚"撞上同一个 key 空间。
IDEMPOTENCY_SCOPE_BRIEF_PARSE: Final[str] = "brief_parse"
IDEMPOTENCY_SCOPE_BRIEF_ENRICH: Final[str] = "brief_enrich"

# ---------------------------------------------------------------------------
# 上限（C4：所有外部调用必须有显式上限）
# ---------------------------------------------------------------------------

# 单条 claim 的文本上限（字符）。超出的**截断并记 issue**，不静默丢。
# 一个跑偏的模型可能把整页原文当成一条 claim 返回 —— 那不是"结论"，
# 是"把输入复制了一遍"，而它会撑爆前端渲染与后续的 context 组装。
MAX_CLAIM_CHARS: Final[int] = 2_000

# 单条疑问描述上限。它是给人看的，不是数据传输通道。
MAX_ISSUE_DESCRIPTION_CHARS: Final[int] = 500

# 单个 Brief 版本的 claim 数上限。超出即拒绝固化 ——
# 一个 Brief 有 2000 条 claim 说明解构跑偏了（把每个句子都当成结论），
# 而下游（WP22 信息草案 / WP26 文案）会拿它当"事实清单"。
MAX_CLAIMS_PER_VERSION: Final[int] = 500

# 单个 Brief 版本的疑问数上限。同上，超出说明解构在制造噪声。
MAX_ISSUES_PER_VERSION: Final[int] = 200

# 固化时"核心卖点摘要"取几项（需求 §5.3："核心卖点可形成三项摘要"）。
SELLING_POINT_SUMMARY_COUNT: Final[int] = 3

# 卖点数低于这个值 → 产生一条 WARN 疑问（需求 §5.3：
# "原材料不足三项时如实记录，不虚构卖点凑数"）。
# **不阻断** —— 需求明确允许不足三项，只是要如实记录。
MIN_SELLING_POINTS_EXPECTED: Final[int] = 3

# `claim_hash` 是 SHA-256 十六进制摘要（与 WP11/WP17 同一算法族）。
CLAIM_HASH_LENGTH: Final[int] = 64


# ---------------------------------------------------------------------------
# 值类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimDraft:
    """一条待落库的结论（`brief_drafts.claims` 的元素 / `brief_claims` 行的来源）。

    **为什么 `evidence` 是单值而不是数组**：数据结构 02 §6 的
    `brief_claims` 只有 `evidence_segment_id?` 与 `evidence_url?` 两列 ——
    它表达的是"这条结论的**主证据**出自哪里"。
    协议 §5.1 的 `evidence` 是数组，本模块按数据结构落地（协议层把单条
    组装成单元素数组）。**这是一处真实的规格不一致，已登记为待定项**：
    若将来确实需要"一条结论多个来源"，那是一次 schema 变更，
    而不是在协议层悄悄扩展形状。
    """

    field_path: str
    text: str
    category: str
    # `{"productIds": [...], "modelCodes": [...]}`。空对象表示"不限定"。
    applicability: dict[str, Any] = field(default_factory=dict)
    evidence_segment_id: Any = None
    evidence_url: str | None = None
    resolution: str = RESOLUTION_PENDING
    resolution_reason: str | None = None

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"未知的 claim 分类：{self.category!r}")
        if self.resolution not in RESOLUTIONS:
            raise ValueError(f"未知的确认状态：{self.resolution!r}")
        if not self.text.strip():
            raise ValueError("claim 文本不能为空")
        if len(self.text) > MAX_CLAIM_CHARS:
            raise ValueError(
                f"claim 文本超过 {MAX_CLAIM_CHARS} 字符上限"
                "（超长文本是解构跑偏的信号，不是值得保留的结论）"
            )
        if (
            self.category in CATEGORIES_REQUIRING_EVIDENCE
            and self.evidence_segment_id is None
            and not self.evidence_url
        ):
            # 与数据库 CHECK 同一判据，在这里先报出可读错误。
            # **两层都要**：这一层给出可读的领域错误（能在测试里断言文案），
            # 数据库那一层管住绕过本构造器的写入（例如数据回填）。
            raise ValueError(
                f"分类 {self.category} 必须带来源（evidenceSegmentId 或 evidenceUrl）"
                "：没有来源的建议不能改成 SOURCE_FACT 而不补证据（协议 §5.1）。"
            )

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "fieldPath": self.field_path,
            "text": self.text,
            "category": self.category,
            "applicability": dict(self.applicability),
            "resolution": self.resolution,
        }
        if self.evidence_segment_id is not None:
            payload["evidence"] = {"segmentId": str(self.evidence_segment_id)}
        elif self.evidence_url:
            payload["evidence"] = {"url": self.evidence_url}
        if self.resolution_reason:
            payload["resolutionReason"] = self.resolution_reason
        return payload


@dataclass(frozen=True)
class IssueDraft:
    """一条待落库的疑问（`brief_drafts.issue_resolutions` 的元素）。

    `id` 由**服务端**生成（解构时）：运营在 `PUT /brief-draft` 里只能改
    `state` / `resolution`，不能新增疑问 —— 否则"核心疑问"就变成
    "运营自己说没有疑问"，而那正是阻断机制要防的。
    """

    id: Any
    field_path: str
    severity: str
    description: str
    state: str = ISSUE_OPEN
    resolution: str | None = None
    resolved_by: Any = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"未知的疑问级别：{self.severity!r}")
        if self.state not in ISSUE_STATES:
            raise ValueError(f"未知的疑问状态：{self.state!r}")
        if not self.description.strip():
            raise ValueError("疑问描述不能为空")
        if self.state in ISSUE_SETTLED_STATES and not (self.resolution or "").strip():
            # 与 WP11 的 `purpose_empty` 同一判据：处置必须留下理由。
            # 没有理由的"已解决"在下一次审查时无法复核 ——
            # 而"这条疑问当初为什么不算问题"正是最需要被复核的信息。
            raise ValueError(
                f"状态 {self.state} 必须填写 resolution（处置理由）"
            )

    @property
    def blocking(self) -> bool:
        """未解决的 BLOCK 级疑问 —— 它阻断固化。"""
        return self.severity == SEVERITY_BLOCK and self.state == ISSUE_OPEN

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": str(self.id),
            "fieldPath": self.field_path,
            "severity": self.severity,
            "description": self.description,
            "state": self.state,
        }
        if self.resolution:
            payload["resolution"] = self.resolution
        if self.resolved_by is not None:
            payload["resolvedBy"] = str(self.resolved_by)
        return payload


@dataclass(frozen=True)
class DecompositionResult:
    """一次结构化解构的产出（模型 → service 的唯一通道）。

    **没有 `state` 字段**：状态由**输入完整度**与**产出是否可用**共同决定，
    不由模型自报。这与 WP17 的 `ParseOutcome` 刻意没有 `state` 是同一条理由
    （`docs/m1/WP17-文档解析与OCR说明.md` §2 的"状态由计数推导"）。

    模型若能自报"我成功了"，"不虚报拆解成功"就只剩自觉。
    """

    fields: dict[str, Any] = field(default_factory=dict)
    claims: tuple[ClaimDraft, ...] = ()
    issues: tuple[IssueDraft, ...] = ()

    @property
    def claim_count(self) -> int:
        return len(self.claims)

    @property
    def blocking_issues(self) -> tuple[IssueDraft, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)


@dataclass(frozen=True)
class EnrichmentSuggestion:
    """一条加厚建议（**不自动生效**，协议 §5 第 5 行原文）。"""

    field_path: str
    text: str
    category: str
    # 建议命中的允许来源（`allowedSourceIds` 里的哪些）。空表示"没有来源"，
    # 那它只能是 `STRATEGY` 或 `UNCONFIRMED`（构造器会拦）。
    evidence_segment_id: Any = None
    evidence_url: str | None = None
    rationale: str = ""

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "fieldPath": self.field_path,
            "text": self.text,
            "category": self.category,
            "rationale": self.rationale,
        }
        if self.evidence_segment_id is not None:
            payload["evidence"] = {"segmentId": str(self.evidence_segment_id)}
        elif self.evidence_url:
            payload["evidence"] = {"url": self.evidence_url}
        return payload


@dataclass(frozen=True)
class EnrichmentResult:
    """一次加厚的结果（模型 → service）。"""

    suggestions: tuple[EnrichmentSuggestion, ...] = ()
    # 模型/网关不可用时的原因（非空即"没做成"，但**不抛错**：
    # 加厚是可选步骤，不可用不该让整个请求 500）。
    unavailable_reason: str | None = None


__all__ = [
    "CATEGORIES",
    "CATEGORIES_REQUIRING_EVIDENCE",
    "CATEGORY_EXTERNAL_EVIDENCE",
    "CATEGORY_LIST_SQL",
    "CATEGORY_SOURCE_FACT",
    "CATEGORY_STRATEGY",
    "CATEGORY_UNCONFIRMED",
    "CLAIM_HASH_LENGTH",
    "ClaimDraft",
    "DecompositionResult",
    "EVIDENCE_CATEGORY_LIST_SQL",
    "EnrichmentResult",
    "EnrichmentSuggestion",
    "FIELD_FILLED",
    "FIELD_MISSING",
    "FIELD_NOT_APPLICABLE",
    "FIELD_STATUSES",
    "IDEMPOTENCY_SCOPE_BRIEF_ENRICH",
    "IDEMPOTENCY_SCOPE_BRIEF_PARSE",
    "ISSUE_DISMISSED",
    "ISSUE_OPEN",
    "ISSUE_RESOLVED",
    "ISSUE_SETTLED_STATES",
    "ISSUE_STATES",
    "ISSUE_STATE_LIST_SQL",
    "IssueDraft",
    "KIND_BRIEF_ENRICH",
    "KIND_BRIEF_PARSE",
    "MAX_CLAIMS_PER_VERSION",
    "MAX_CLAIM_CHARS",
    "MAX_ISSUES_PER_VERSION",
    "MAX_ISSUE_DESCRIPTION_CHARS",
    "MIN_SELLING_POINTS_EXPECTED",
    "QUEUE_BRIEF",
    "RESOLUTIONS",
    "RESOLUTION_CONFIRMED",
    "RESOLUTION_LIST_SQL",
    "RESOLUTION_NOT_APPLICABLE",
    "RESOLUTION_PENDING",
    "SELLING_POINT_SUMMARY_COUNT",
    "SEVERITIES",
    "SEVERITY_BLOCK",
    "SEVERITY_LIST_SQL",
    "SEVERITY_WARN",
    "SOURCE_STATES",
    "SOURCE_STATE_FAILED",
    "SOURCE_STATE_LIST_SQL",
    "SOURCE_STATE_NONE",
    "SOURCE_STATE_PARTIAL",
    "SOURCE_STATE_SUCCEEDED",
]
