"""WP34 回退影响图 / 导出失效：常量、取值域与不可变值对象。

**不 import 任何 `rednote.modules.*`**（平级模块不可互 import；跨模块只经 `*_bridge.py`
与 `repository.py` 的裸 SQL）。

## 取值域：哪些落库、哪些只在响应里

落库（闭集 + CHECK，迁移里写字面量）：`transition` / `reason` / `scope_kind`。
响应侧（不落库）：有效性状态 / `impactClass` / `exportImpact` / `truncation` / `anomaly`。
`UNKNOWN` 永不落库。`unknownReason` 是开集（透传 `ExportError.reason`），不加 CHECK。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Final

# ---------------------------------------------------------------------------
# 规模上限（OQ-34-08 默认；无仓内数据依据）
# ---------------------------------------------------------------------------

MAX_TRAVERSAL_DEPTH: Final[int] = 12
MAX_TRAVERSAL_NODES: Final[int] = 2000
FRONTIER_CHUNK: Final[int] = 500
#: 与 `exports.types.MAX_CONTENT_ITEMS_PER_EXPORT` 同值（平级不可互 import，测试对账）。
MAX_SWEEP_CONTENT_IDS: Final[int] = 50
MAX_SWEEP_ITEMS: Final[int] = 500
#: 单次尝试追加事件的并发重试次数（撞 UQ(export_item_id, seq) 后重读重算）。
MAX_APPEND_ATTEMPTS: Final[int] = 3

SHA256_HEX_CHARS: Final[int] = 64

# ---------------------------------------------------------------------------
# 落库取值域
# ---------------------------------------------------------------------------

TRANSITION_INVALIDATED: Final[str] = "INVALIDATED"
TRANSITION_RESTORED: Final[str] = "RESTORED"
TRANSITIONS: Final[frozenset[str]] = frozenset(
    {TRANSITION_INVALIDATED, TRANSITION_RESTORED}
)

REASON_VERSION_SUPERSEDED: Final[str] = "VERSION_SUPERSEDED"
REASON_ASSET_CHANGED: Final[str] = "ASSET_CHANGED"
EVENT_REASONS: Final[frozenset[str]] = frozenset(
    {REASON_VERSION_SUPERSEDED, REASON_ASSET_CHANGED}
)

SCOPE_CONTENTS: Final[str] = "CONTENTS"
SCOPE_FROM_VERSION: Final[str] = "FROM_VERSION"
SCOPE_KINDS: Final[frozenset[str]] = frozenset({SCOPE_CONTENTS, SCOPE_FROM_VERSION})


def _sql_list(values: frozenset[str]) -> str:
    return ", ".join(f"'{v}'" for v in sorted(values))


TRANSITION_LIST_SQL: Final[str] = _sql_list(TRANSITIONS)
EVENT_REASON_LIST_SQL: Final[str] = _sql_list(EVENT_REASONS)
SCOPE_KIND_LIST_SQL: Final[str] = _sql_list(SCOPE_KINDS)

# ---------------------------------------------------------------------------
# 响应侧取值域
# ---------------------------------------------------------------------------

STATE_VALID: Final[str] = "VALID"
STATE_INVALID: Final[str] = "INVALID"
STATE_UNKNOWN: Final[str] = "UNKNOWN"

IMPACT_REVIEW_REQUIRED: Final[str] = "REVIEW_REQUIRED"
IMPACT_HISTORICAL: Final[str] = "HISTORICAL"
IMPACT_UNRESOLVED: Final[str] = "UNRESOLVED"

EXPORT_IMPACT_DEPARTING: Final[str] = "FROZEN_ON_DEPARTING"
EXPORT_IMPACT_DOWNSTREAM: Final[str] = "FROZEN_ON_DOWNSTREAM"
EXPORT_IMPACT_TARGET: Final[str] = "FROZEN_ON_TARGET"

TRUNCATION_NODE_LIMIT: Final[str] = "NODE_LIMIT"
TRUNCATION_DEPTH_LIMIT: Final[str] = "DEPTH_LIMIT"

ANOMALY_CYCLE: Final[str] = "DEPENDENCY_CYCLE"

ACTION_INVALIDATED: Final[str] = "INVALIDATED"
ACTION_RESTORED: Final[str] = "RESTORED"
ACTION_UNCHANGED: Final[str] = "UNCHANGED"
ACTION_UNKNOWN: Final[str] = "UNKNOWN"

# ---------------------------------------------------------------------------
# 错误 reason（`details.reason`）
# ---------------------------------------------------------------------------

REASON_VERSION_NOT_FOUND: Final[str] = "VERSION_NOT_FOUND"
REASON_CONTENT_NOT_FOUND: Final[str] = "CONTENT_NOT_FOUND"
REASON_EXPORT_NOT_FOUND: Final[str] = "EXPORT_NOT_FOUND"
REASON_ENTITY_TYPE_NOT_SUPPORTED: Final[str] = "ENTITY_TYPE_NOT_SUPPORTED"
REASON_TARGET_NOT_OF_ENTITY: Final[str] = "TARGET_NOT_OF_ENTITY"
REASON_TARGET_IS_CURRENT: Final[str] = "TARGET_IS_CURRENT"
REASON_SCOPE_INVALID: Final[str] = "SCOPE_INVALID"
REASON_CONTENT_IDS_EMPTY: Final[str] = "CONTENT_IDS_EMPTY"
REASON_CONTENT_IDS_TOO_MANY: Final[str] = "CONTENT_IDS_TOO_MANY"
REASON_CONTENT_IDS_DUPLICATE: Final[str] = "CONTENT_IDS_DUPLICATE"
REASON_SWEEP_SCOPE_TOO_LARGE: Final[str] = "SWEEP_SCOPE_TOO_LARGE"
REASON_NO_CURRENT_VERSION: Final[str] = "NO_CURRENT_VERSION"
REASON_IMPACT_TRUNCATED: Final[str] = "IMPACT_TRUNCATED"

#: v1 回退预览只支持内容。
ENTITY_TYPE_CONTENT: Final[str] = "CONTENT"

# ---------------------------------------------------------------------------
# 不可变值对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Edge:
    """`version_dependencies` 一行：`parent` 用到了 `dependency`（下游 → 上游）。"""

    parent: uuid.UUID
    dependency: uuid.UUID
    purpose: str


@dataclass(frozen=True)
class TraversalNode:
    version_id: uuid.UUID
    depth: int
    purposes: tuple[str, ...]


@dataclass(frozen=True)
class TraversalResult:
    start: uuid.UUID
    nodes: tuple[TraversalNode, ...]
    complete: bool
    truncation: str | None
    max_depth_reached: int
    anomalies: tuple[str, ...] = ()
    cycle_node_count: int = 0
    round_trips: int = 0


@dataclass(frozen=True)
class Observed:
    version_id: uuid.UUID
    eligibility_hash: str


@dataclass(frozen=True)
class Unavailable:
    """算不出资格（透传上游 `ExportError.reason`）。**不是** VALID 也不是 INVALID。"""

    upstream_reason: str
    message: str


@dataclass(frozen=True)
class ExportRow:
    """`export_items` 的只读视图（S3：列名即对方契约）。"""

    id: uuid.UUID
    project_id: uuid.UUID
    export_group_id: uuid.UUID
    content_item_id: uuid.UUID
    version_node_id: uuid.UUID
    eligibility_hash: str
    created_at: object = None


@dataclass(frozen=True)
class Derived:
    state: str
    reason: str | None = None
    observed: Observed | None = None
    unknown_reason: str | None = None


@dataclass
class SweepRow:
    export_item_id: uuid.UUID
    content_item_id: uuid.UUID
    recorded_before: str
    derived_state: str
    derived_reason: str | None
    action: str
    unknown_reason: str | None = None
    appended_seq: int | None = None
    notes: list[str] = field(default_factory=list)


def derive(row_version: uuid.UUID, row_hash: str, observation: Observed | Unavailable) -> Derived:
    """唯一的判定点（设计稿 §5.4）：把冻结值与一次观测比较。

    `Unavailable` ⇒ UNKNOWN；指纹相同 ⇒ VALID；版本变了 ⇒ VERSION_SUPERSEDED；
    版本没变而指纹变了 ⇒ ASSET_CHANGED（同一正文版本、图被重新加工）。
    """
    if isinstance(observation, Unavailable):
        return Derived(STATE_UNKNOWN, unknown_reason=observation.upstream_reason)
    if observation.eligibility_hash == row_hash:
        return Derived(STATE_VALID, observed=observation)
    if observation.version_id != row_version:
        return Derived(STATE_INVALID, REASON_VERSION_SUPERSEDED, observation)
    return Derived(STATE_INVALID, REASON_ASSET_CHANGED, observation)

