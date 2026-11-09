"""WP20 的不可变值类型与受控取值域。

**为什么单独一个模块**：这些取值同时被 `schema.py`（六类字段规格）、
`projection.py`（版本状态投影）、`models.py`（表约束）、`repository.py`
（存储）、`service.py`（业务规则）与 `api.py` 使用。定义在 service 里会让
其余几个反向导入 service —— 本模块的分层刻意单向：

    types ← schema
    types ← projection
    types ← models
    types ← repository
    types ← service
    types ← api

与 WP18 的 `brief/types.py` 同一条理由：**跨层传递的值用 frozen dataclass
而不是 ORM 对象**（ORM 实例绑在 session 上，session 一关就
`DetachedInstanceError`）。

---

## 本包的两条"不许省略"的判据（验收项的结构性落点）

### 1. `structured_payload` 不能是空对象（"禁止无结构散文替代事实字段"）

规格源 02 §8 的 `knowledge_versions` 备注原文：

> **类别对应 Schema，禁止无结构散文替代事实字段**

`body text` 是给人读的散文，`structured_payload jsonb` 是给下游（WP21 检索、
WP22 基线、WP23 Prompt）消费的结构。若允许 `structured_payload = {}`，
`body` 里那句"我们产品续航 18 小时"就成了一条**无法被程序消费、也无法被
程序校验**的事实 —— 下游只能整段塞进 Prompt，而"这个数字有没有条件限定"
"它适用于哪个型号"这类问题**永远问不出来**。

因此本包把"每一类必须有它的必填结构字段"做成三层落地（与 WP18 的
`PARTIAL` 硬约束同一手法）：
  1. `schema.py` 的 `CATEGORY_FIELDS`：六类各自的必填字段（本模块提供取值）；
  2. `models.py` 的 CHECK：`structured_payload` 必须是**非空** object；
  3. `service.py` 的写入门：按类别逐字段判定，缺失则 422 带 `missingFields`。

### 2. 事实范围必须明确（`category='FACT'` 不允许 `scope='GLOBAL'`）

同一行备注的后半句："**全员可管理；事实范围必须明确**"。

"全员可管理"意味着任何登录成员都能往知识库写东西 —— 那么一条
"适用于所有品牌"的事实断言（`scope='GLOBAL'`）就会**横穿所有项目**。
WP21 的验收原文里有一句直接相关的话：「**近似品牌案例不串事实**」——
跨品牌串事实的源头，正是一条没有范围限定的 `FACT`。

因此 `FACT` 必须落到 `BRAND` / `PRODUCT` / `PROJECT` 之一。
**其余五类不受此限**：场景、风格、规则、研究、反馈实验都允许
`GLOBAL`（"小红书图文首图要有人"这类风格约定本就不该绑定品牌），
把它们一起限死只会逼运营编一个假的 `brand_id`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

# ---------------------------------------------------------------------------
# 六类知识（规格源 02 §8 的 category）
# ---------------------------------------------------------------------------

CATEGORY_FACT: Final[str] = "FACT"
CATEGORY_RESEARCH: Final[str] = "RESEARCH"
CATEGORY_SCENE: Final[str] = "SCENE"
CATEGORY_STYLE: Final[str] = "STYLE"
CATEGORY_RULE: Final[str] = "RULE"
CATEGORY_FEEDBACK_EXPERIMENT: Final[str] = "FEEDBACK_EXPERIMENT"

#: **六级知识的唯一权威列表**（规格源 02 §8 原文的取值域）。
CATEGORIES: Final[tuple[str, ...]] = (
    CATEGORY_FACT,
    CATEGORY_RESEARCH,
    CATEGORY_SCENE,
    CATEGORY_STYLE,
    CATEGORY_RULE,
    CATEGORY_FEEDBACK_EXPERIMENT,
)
CATEGORY_LIST_SQL: Final[str] = ", ".join(f"'{name}'" for name in sorted(CATEGORIES))

#: **必须落到具体范围的类别**（"事实范围必须明确"）。
#: 抽成集合而不是在 CHECK 里写死一个字面量：将来新增类别（例如 `RULE`
#: 若被要求也绑定品牌）时，"它要不要明确范围"是一个必须显式回答的问题 ——
#: 散写的话新增类别会静默落进"不受限"那一侧。
CATEGORIES_REQUIRING_CONCRETE_SCOPE: Final[frozenset[str]] = frozenset(
    {CATEGORY_FACT}
)

# ---------------------------------------------------------------------------
# 知识范围（knowledge_assets.scope / knowledge_proposals.scope）
# ---------------------------------------------------------------------------
# 规格源只给了列名，**没给取值域**。本包的判据是"这条知识适用于谁"，
# 按**范围由宽到窄**排列：
#
#   GLOBAL  → 全组织（不限品牌/产品/项目）
#   BRAND   → 限定品牌
#   PRODUCT → 限定产品
#   PROJECT → 限定项目（项目私有，检查点 G2 的"项目私有数据边界"）
#
# **为什么不叫 `SCOPE_TENANT` 之类**：本项目没有多租户，组织是唯一顶层；
# 造一个不存在的层级会让权限判定凭空多一个分支。

SCOPE_GLOBAL: Final[str] = "GLOBAL"
SCOPE_BRAND: Final[str] = "BRAND"
SCOPE_PRODUCT: Final[str] = "PRODUCT"
SCOPE_PROJECT: Final[str] = "PROJECT"

SCOPES: Final[tuple[str, ...]] = (
    SCOPE_GLOBAL,
    SCOPE_BRAND,
    SCOPE_PRODUCT,
    SCOPE_PROJECT,
)
SCOPE_LIST_SQL: Final[str] = ", ".join(f"'{name}'" for name in sorted(SCOPES))

#: 每个范围**必须携带的列**。这是"范围"从一句声明变成可校验事实的地方：
#: 只说 `scope='BRAND'` 而不给 `brand_id`，范围就是一句无法执行的声明。
#: 与 `models.py` 的 CHECK 一一对应（那里逐条写，这里给出唯一来源供测试核对）。
SCOPE_REQUIRED_COLUMN: Final[dict[str, str]] = {
    SCOPE_GLOBAL: "",  # 不需要任何限定列
    SCOPE_BRAND: "brand_id",
    SCOPE_PRODUCT: "product_id",
    SCOPE_PROJECT: "project_id",
}

# ---------------------------------------------------------------------------
# 发布动作（knowledge_publications.action）
# ---------------------------------------------------------------------------
# 规格源 02 §8 原文：`action ACTIVATE/ARCHIVE/RESTORE/SUPERSEDE`。
#
# **四者的区别不是"哪一列被改"，而是"这条事件改变了什么可用性"**：
#
#   ACTIVATE → 某版本开始生效（`active_version_id` 指向它）
#   ARCHIVE  → 整条 asset 退出可用（`archived_at` 置位，active_version 不动）
#   RESTORE  → 从归档恢复（`archived_at` 清空）—— **通过新事件，不改历史**
#   SUPERSEDE→ 一个版本被另一个版本取代（旧版本不再 ACTIVE，但仍可读）
#
# 规格源 02 §12（交接文档 §4 引）的原文是：
# > `DRAFT → PENDING → ACTIVE → SUPERSEDED/ARCHIVED`；
# > **恢复通过新事件，不篡改旧状态历史**
#
# 最后一句就是 `projection.py` 存在的理由，也是本包验收原文
# 「归档可追溯，**生效版本与旧引用并存**」的落点。

ACTION_ACTIVATE: Final[str] = "ACTIVATE"
ACTION_ARCHIVE: Final[str] = "ARCHIVE"
ACTION_RESTORE: Final[str] = "RESTORE"
ACTION_SUPERSEDE: Final[str] = "SUPERSEDE"

PUBLICATION_ACTIONS: Final[tuple[str, ...]] = (
    ACTION_ACTIVATE,
    ACTION_ARCHIVE,
    ACTION_RESTORE,
    ACTION_SUPERSEDE,
)
PUBLICATION_ACTION_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(PUBLICATION_ACTIONS)
)

#: 协议 §9 的 `POST /knowledge/assets/{id}/publications` 只接受三个动作
#: （`versionId,action ACTIVATE/ARCHIVE/RESTORE,reason?`）。
#:
#: **`SUPERSEDE` 不在此列，是有意的**：协议里它不是一个客户端可选的动作 ——
#: 它是 ACTIVATE 的**副作用**（激活新版本时，旧版本自动变成 SUPERSEDED）。
#: 让客户端能直接选 SUPERSEDE，就会出现"把当前生效版本标成被取代、
#: 但没有任何版本接过它"的空窗 —— 知识库看起来有内容，实际无生效版本。
CLIENT_PUBLICATION_ACTIONS: Final[frozenset[str]] = frozenset(
    {ACTION_ACTIVATE, ACTION_ARCHIVE, ACTION_RESTORE}
)

# ---------------------------------------------------------------------------
# 版本投影状态（version_state，由事件流算出，不落库为可变列）
# ---------------------------------------------------------------------------
# 数据结构 02 §12 的原文链条：`DRAFT → PENDING → ACTIVE → SUPERSEDED/ARCHIVED`。
#
# **本实现里 `PENDING` 的归属与规格源不同，这是一个有意取舍**：
# 规格源把 PENDING 放在版本状态链上，但同一份规格源 §277 又说
# "索引尚未完成时**知识状态**显示'已生效／索引处理中'" ——
# 后者明写是 **asset 级**的显示状态。两者放一起会矛盾：
# 一个版本若因索引未完成而处于 PENDING，那"谁是当前生效版本"就没有答案了，
# 而 §277 要求"重要事实可以从数据库精确获取"（精确读取路径必须能拿到
# 一个确定的 `active_version_id`）。因此本实现：
#
#   · 版本状态 = {DRAFT, ACTIVE, SUPERSEDED, ARCHIVED}（纯由事件流决定）
#   · "索引处理中" = asset 级状态（`ASSET_STATE_INDEXING`），
#     判据是版本已 ACTIVE 但索引 Outbox 事件尚未投递
#
# 取舍已登记为待定项（见说明文档 §1）。

VERSION_STATE_DRAFT: Final[str] = "DRAFT"
VERSION_STATE_ACTIVE: Final[str] = "ACTIVE"
VERSION_STATE_SUPERSEDED: Final[str] = "SUPERSEDED"
VERSION_STATE_ARCHIVED: Final[str] = "ARCHIVED"

VERSION_STATES: Final[tuple[str, ...]] = (
    VERSION_STATE_DRAFT,
    VERSION_STATE_ACTIVE,
    VERSION_STATE_SUPERSEDED,
    VERSION_STATE_ARCHIVED,
)

#: 曾生效过的版本状态。用于"历史版本"列表的筛选与前端展示分组。
VERSION_STATES_PUBLISHED: Final[frozenset[str]] = frozenset(
    {VERSION_STATE_ACTIVE, VERSION_STATE_SUPERSEDED, VERSION_STATE_ARCHIVED}
)

# ---------------------------------------------------------------------------
# asset 显示状态（协议 §9 的 `state` 筛选参数、规格源 §277 的"索引处理中"）
# ---------------------------------------------------------------------------
# 协议 §9 的 `GET /knowledge/assets` 收 `state` 筛选，但**没给取值域**。
# 本实现定义为"asset 当前处于什么可用性"：

ASSET_STATE_DRAFT: Final[str] = "DRAFT"
ASSET_STATE_ACTIVE: Final[str] = "ACTIVE"
#: 已生效，但索引还没建好（规格源 §277 的"已生效／索引处理中"）。
ASSET_STATE_INDEXING: Final[str] = "INDEXING"
ASSET_STATE_SUPERSEDED: Final[str] = "SUPERSEDED"
ASSET_STATE_ARCHIVED: Final[str] = "ARCHIVED"

ASSET_STATES: Final[tuple[str, ...]] = (
    ASSET_STATE_DRAFT,
    ASSET_STATE_ACTIVE,
    ASSET_STATE_INDEXING,
    ASSET_STATE_SUPERSEDED,
    ASSET_STATE_ARCHIVED,
)
ASSET_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(ASSET_STATES)
)

#: **"仍然可读"的 asset 状态**（含已归档）。
#:
#: 归档不是删除 —— 验收原文「归档可追溯，**生效版本与旧引用并存**」要求
#: 旧引用仍能解析到内容。因此归档知识在"按 id 直读"时可见，
#: 只是不出现在默认列表里（默认列表排除它，由 `includeArchived` 参数放开）。
ASSET_STATES_READABLE: Final[frozenset[str]] = frozenset(ASSET_STATES)

#: 默认列表**排除**的状态。理由见 `ASSET_STATES_READABLE`。
ASSET_STATES_HIDDEN_BY_DEFAULT: Final[frozenset[str]] = frozenset(
    {ASSET_STATE_ARCHIVED}
)

# ---------------------------------------------------------------------------
# 提案状态与决策（knowledge_proposals）
# ---------------------------------------------------------------------------
# 规格源 02 §8 原文备注：「**AI 创建待确认，不直接生效**」。
# 协议 §9 的 `POST /knowledge/proposals/{id}/decisions` 收
# `decision ACCEPT/REJECT,scope,reason`。

PROPOSAL_STATE_PENDING: Final[str] = "PENDING"
PROPOSAL_STATE_ACCEPTED: Final[str] = "ACCEPTED"
PROPOSAL_STATE_REJECTED: Final[str] = "REJECTED"

PROPOSAL_STATES: Final[tuple[str, ...]] = (
    PROPOSAL_STATE_PENDING,
    PROPOSAL_STATE_ACCEPTED,
    PROPOSAL_STATE_REJECTED,
)
PROPOSAL_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(PROPOSAL_STATES)
)

#: 已处置的提案状态（不能再决策第二次）。
PROPOSAL_SETTLED_STATES: Final[frozenset[str]] = frozenset(
    {PROPOSAL_STATE_ACCEPTED, PROPOSAL_STATE_REJECTED}
)

DECISION_ACCEPT: Final[str] = "ACCEPT"
DECISION_REJECT: Final[str] = "REJECT"

PROPOSAL_DECISIONS: Final[tuple[str, ...]] = (DECISION_ACCEPT, DECISION_REJECT)
PROPOSAL_DECISION_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(PROPOSAL_DECISIONS)
)

#: 决策 → 提案终态的映射。**一处定义**：服务层写回状态、查询过滤
#: 都用它，两处各写一份 `if decision == "ACCEPT"` 迟早漂移。
DECISION_RESULT_STATE: Final[dict[str, str]] = {
    DECISION_ACCEPT: PROPOSAL_STATE_ACCEPTED,
    DECISION_REJECT: PROPOSAL_STATE_REJECTED,
}

# ---------------------------------------------------------------------------
# Outbox 事件（复用 WP12 的 `outbox_events`，不改它的表结构）
# ---------------------------------------------------------------------------
# 规格源 §277 末句："写索引 Outbox"。索引由 WP21 建，本包只负责
# **在同一事务里发出这条事件**（与 WP12 上传文件时写 `file.uploaded` 同一手法）。
#
# 事件名与聚合类型放在这里而不是散在 service 里：WP21 的投递器要按名字订阅，
# 两处各写一遍字符串、拼错时**不会报错**（`jobs.dispatch.HandlerRegistry`
# 对无处理器的任务名是放行的），只会静默不索引。

EVENT_KNOWLEDGE_INDEX_REQUESTED: Final[str] = "knowledge.index_requested"
EVENT_KNOWLEDGE_INDEX_REMOVED: Final[str] = "knowledge.index_removed"

AGGREGATE_KNOWLEDGE: Final[str] = "knowledge_asset"

# ---------------------------------------------------------------------------
# 长度与计数上限
# ---------------------------------------------------------------------------

#: 标题长度。与 WP10 的字典命名口径一致（200）。
MAX_TITLE_CHARS: Final[int] = 200

#: 正文长度上限。**刻意给得宽**（`body` 是给人读的完整叙述）：
#: 上限的作用只是防止某次导入把整本书塞进来，不是"够不够用"的判断。
MAX_BODY_CHARS: Final[int] = 200_000

#: 变更理由长度。与 WP18 的 `change_reason` 同一口径。
MAX_CHANGE_REASON_CHARS: Final[int] = 2_000

#: 发布/决策理由长度。
MAX_REASON_CHARS: Final[int] = 1_000

#: 单个结构化字段的长度上限（字符串值）。超长多半是前端贴错了整段正文。
MAX_FIELD_TEXT_CHARS: Final[int] = 20_000

#: 列表接口一次最多返回的版本数/引用数（游标分页的上限由 `platform.pagination` 管，
#: 这里的是"服务端内部一次最多取多少"的保护）。
MAX_PAGE_LIMIT: Final[int] = 100

#: `structured_payload` 的形状版本。与 WP11 的 `schema_version`、
#: WP17 的 `position_schema_version`、WP18 的 `BRIEF_SCHEMA_VERSION` 同一手法：
#: 写入方带上它，消费方据此选择解析方式（六类 Schema 将来会扩展字段）。
KNOWLEDGE_SCHEMA_VERSION: Final[int] = 1

#: `version_nodes.payload_hash` 的长度（SHA-256 十六进制）。
PAYLOAD_HASH_LENGTH: Final[int] = 64

# ---------------------------------------------------------------------------
# 值类型（跨层传递用，不传 ORM 对象）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublicationEvent:
    """**纯数据**的发布事件（`projection.py` 的唯一输入形状）。

    **为什么不用 ORM 的 `KnowledgePublication`**：投影是纯函数，
    它要能被单元测试独立验证（不需要真库、不需要 session）。
    用一个只含投影所需字段的 frozen dataclass，投影函数就与存储层解耦 ——
    这是 `projection.py` 可以被"喂构造出来的事件序列"测透的前提。

    `sequence` 是同一 asset 内的**事件序号**（从 1 开始），
    由 repository 在插入时按 `max + 1` 分配。投影按它排序，
    而不是按 `created_at`：同一事务内插入的两条事件时间戳可能完全相同
    （`now()` 在事务内是常量），按时间排序会得到不确定的顺序。
    """

    sequence: int
    version_id: Any
    action: str
    #: 该事件是否由"索引已建好"确认（见 asset 级 INDEXING 状态的判据）。
    index_confirmed: bool = False


@dataclass(frozen=True)
class VersionProjection:
    """一个版本在事件流下的投影结果。"""

    version_id: Any
    state: str
    #: 它是第几个生效的版本（1 = 首个）。用于 SUPERSEDED 的展示与排序。
    activation_no: int = 0


@dataclass(frozen=True)
class AssetProjection:
    """整个 asset 的投影结果（`projection.py` 的返回值）。"""

    active_version_id: Any | None
    archived: bool
    state: str
    #: 版本 id → 投影状态。历史上生效过的版本都在这里（含已 SUPERSEDED）。
    versions: dict[Any, VersionProjection]


@dataclass(frozen=True)
class VersionDraft:
    """创建 / 追加版本时的输入（`service.add_version` 的入参）。

    `structured_payload` 已按类别校验过；`source_segment_ids` 是**来源片段**
    （规格源 02 §8 的 `knowledge_versions.source_segment_ids UUID[]`），
    可空 —— 手写知识没有原文片段可指。
    """

    body: str
    structured_payload: dict[str, Any]
    source_segment_ids: tuple[Any, ...] = ()
    change_reason: str = ""
    effective_from: Any | None = None
    effective_until: Any | None = None


@dataclass(frozen=True)
class ProposalDecision:
    """一次提案决策（`service.decide_proposal` 的入参）。

    协议 §9 原文：`decision ACCEPT/REJECT,scope,reason`。
    `scope` 允许在采纳时**覆盖**提案自带的范围 —— AI 提的范围是建议，
    确认人有权改窄（"这条只适用于本项目"）。
    """

    decision: str
    scope: str | None = None
    reason: str = ""


__all__ = [
    "ACTION_ACTIVATE",
    "ACTION_ARCHIVE",
    "ACTION_RESTORE",
    "ACTION_SUPERSEDE",
    "AGGREGATE_KNOWLEDGE",
    "ASSET_STATES",
    "ASSET_STATES_HIDDEN_BY_DEFAULT",
    "ASSET_STATES_READABLE",
    "ASSET_STATE_ACTIVE",
    "ASSET_STATE_ARCHIVED",
    "ASSET_STATE_DRAFT",
    "ASSET_STATE_INDEXING",
    "ASSET_STATE_LIST_SQL",
    "ASSET_STATE_SUPERSEDED",
    "AssetProjection",
    "CATEGORIES",
    "CATEGORIES_REQUIRING_CONCRETE_SCOPE",
    "CATEGORY_FACT",
    "CATEGORY_FEEDBACK_EXPERIMENT",
    "CATEGORY_LIST_SQL",
    "CATEGORY_RESEARCH",
    "CATEGORY_RULE",
    "CATEGORY_SCENE",
    "CATEGORY_STYLE",
    "CLIENT_PUBLICATION_ACTIONS",
    "DECISION_ACCEPT",
    "DECISION_REJECT",
    "DECISION_RESULT_STATE",
    "EVENT_KNOWLEDGE_INDEX_REMOVED",
    "EVENT_KNOWLEDGE_INDEX_REQUESTED",
    "KNOWLEDGE_SCHEMA_VERSION",
    "MAX_BODY_CHARS",
    "MAX_CHANGE_REASON_CHARS",
    "MAX_FIELD_TEXT_CHARS",
    "MAX_PAGE_LIMIT",
    "MAX_REASON_CHARS",
    "MAX_TITLE_CHARS",
    "PAYLOAD_HASH_LENGTH",
    "PROPOSAL_DECISIONS",
    "PROPOSAL_DECISION_LIST_SQL",
    "PROPOSAL_SETTLED_STATES",
    "PROPOSAL_STATES",
    "PROPOSAL_STATE_ACCEPTED",
    "PROPOSAL_STATE_LIST_SQL",
    "PROPOSAL_STATE_PENDING",
    "PROPOSAL_STATE_REJECTED",
    "PUBLICATION_ACTIONS",
    "PUBLICATION_ACTION_LIST_SQL",
    "ProposalDecision",
    "PublicationEvent",
    "SCOPES",
    "SCOPE_BRAND",
    "SCOPE_GLOBAL",
    "SCOPE_LIST_SQL",
    "SCOPE_PRODUCT",
    "SCOPE_PROJECT",
    "SCOPE_REQUIRED_COLUMN",
    "VERSION_STATES",
    "VERSION_STATES_PUBLISHED",
    "VERSION_STATE_ACTIVE",
    "VERSION_STATE_ARCHIVED",
    "VERSION_STATE_DRAFT",
    "VERSION_STATE_SUPERSEDED",
    "VersionDraft",
    "VersionProjection",
]