"""WP31 试产关口 / 淘汰补产的取值域、状态迁移矩阵与值对象。

本文件**只放常量与纯值类型**，不 import 任何 `rednote.modules.*`
（其他模块的取值只"各写一份"，一致性由 `tests/test_planning_wp31.py` 的 parity 用例对账）。

## 轮的状态机（唯一存储的状态）

```
PLANNED ──开工──▶ GENERATING ──出活完毕──▶ REVIEWING ──批准(仅经 gate-decisions)──▶ GATE_PASSED
   │                  ▲  │                     │
   │                  └──┼── 淘汰补产回退 ─────┘
   └─────取消────▶ CANCELLED ◀──取消───────────┘
```

`ROUND_TRANSITIONS` 同时存在于三处：本文件（服务层 409 的依据）、触发器函数体
（`ddl.py`，数据库最后一道）、DDL 草案。`ddl.py` 从本常量**生成**触发器函数体里的矩阵，
并由测试对账，避免"各写一遍、改一处不报错"。

## 取值域哪些落库、哪些不落库

落库 CHECK（迁移须字面量，ISS-191）：`kind` / `state` / `limit_source`。
不落库（代码常量）：名额派生状态、关口条件码、入口条件码、`details.reason` 码
—— 理由见 DDL 草案第七节。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

# ---------------------------------------------------------------------------
# 轮的种类与状态（落库 CHECK）
# ---------------------------------------------------------------------------
ROUND_KIND_TRIAL: Final[str] = "TRIAL"
ROUND_KIND_SUPPLEMENT: Final[str] = "SUPPLEMENT"
ROUND_KINDS: Final[frozenset[str]] = frozenset(
    {ROUND_KIND_TRIAL, ROUND_KIND_SUPPLEMENT}
)

ROUND_STATE_PLANNED: Final[str] = "PLANNED"
ROUND_STATE_GENERATING: Final[str] = "GENERATING"
ROUND_STATE_REVIEWING: Final[str] = "REVIEWING"
ROUND_STATE_GATE_PASSED: Final[str] = "GATE_PASSED"
ROUND_STATE_CANCELLED: Final[str] = "CANCELLED"
ROUND_STATES: Final[frozenset[str]] = frozenset(
    {
        ROUND_STATE_PLANNED,
        ROUND_STATE_GENERATING,
        ROUND_STATE_REVIEWING,
        ROUND_STATE_GATE_PASSED,
        ROUND_STATE_CANCELLED,
    }
)

#: 终态（无出边）。
ROUND_TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {ROUND_STATE_GATE_PASSED, ROUND_STATE_CANCELLED}
)
#: 未终态（部分唯一索引 `uq_trial_rounds_project_open` 的 WHERE 同源）。
ROUND_OPEN_STATES: Final[frozenset[str]] = ROUND_STATES - ROUND_TERMINAL_STATES

#: 合法迁移（from -> 允许的 to）。**不在表内的一律非法**；同态不是迁移。
ROUND_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    ROUND_STATE_PLANNED: frozenset({ROUND_STATE_CANCELLED, ROUND_STATE_GENERATING}),
    ROUND_STATE_GENERATING: frozenset({ROUND_STATE_CANCELLED, ROUND_STATE_REVIEWING}),
    ROUND_STATE_REVIEWING: frozenset(
        {ROUND_STATE_CANCELLED, ROUND_STATE_GATE_PASSED, ROUND_STATE_GENERATING}
    ),
    ROUND_STATE_GATE_PASSED: frozenset(),
    ROUND_STATE_CANCELLED: frozenset(),
}

#: 经 `POST .../transitions` 可到达的目标态（`GATE_PASSED` 只能经 gate-decisions）。
TRANSITION_ENDPOINT_TARGETS: Final[frozenset[str]] = frozenset(
    {ROUND_STATE_GENERATING, ROUND_STATE_REVIEWING, ROUND_STATE_CANCELLED}
)
#: 矩阵里合法、但**不是**独立入口的边（由"淘汰补产"在同事务里推进）。
ENDPOINT_FORBIDDEN_EDGES: Final[frozenset[tuple[str, str]]] = frozenset(
    {(ROUND_STATE_REVIEWING, ROUND_STATE_GENERATING)}
)


def _sql_list(values: frozenset[str]) -> str:
    return ", ".join(f"'{name}'" for name in sorted(values))


ROUND_KIND_LIST_SQL: Final[str] = _sql_list(ROUND_KINDS)
ROUND_STATE_LIST_SQL: Final[str] = _sql_list(ROUND_STATES)
ROUND_OPEN_STATE_LIST_SQL: Final[str] = _sql_list(ROUND_OPEN_STATES)

# limit_source：与 `planning.types.SETTING_ORIGINS` 同值，**第二处声称**（测试 parity）。
LIMIT_SOURCES: Final[frozenset[str]] = frozenset({"GLOBAL", "INHERITED", "MANUAL"})
LIMIT_SOURCE_LIST_SQL: Final[str] = _sql_list(LIMIT_SOURCES)

# ---------------------------------------------------------------------------
# 边界常量
# ---------------------------------------------------------------------------
MAX_REASON_CHARS: Final[int] = 500
MAX_IDEMPOTENCY_KEY_CHARS: Final[int] = 160
MAX_SCENE_KEY_CHARS: Final[int] = 200
REQUEST_HASH_CHARS: Final[int] = 64
#: 防脏数据的数量上界，与 `planning.types.MAX_QUANTITY_BOUND` 同值（不是业务上限）。
MAX_QUANTITY_BOUND: Final[int] = 100_000
#: 试产档位（`planning.limits.validate_quantity` 的 batch_kind）。SUPPLEMENT 同按此档（Q10）。
BATCH_KIND_TRIAL: Final[str] = "TRIAL"

#: `content_items.generation_request` 里批次档位的键（与 `content.types.REQUEST_KEY_BATCH_KIND` 同值）。
GENERATION_REQUEST_KEY_BATCH_KIND: Final[str] = "batchKind"

# ---------------------------------------------------------------------------
# 名额派生状态（读时派生，不落列）
# ---------------------------------------------------------------------------
SLOT_STATUS_UNBOUND: Final[str] = "UNBOUND"
SLOT_STATUS_AWAITING_REVIEW: Final[str] = "AWAITING_REVIEW"
SLOT_STATUS_APPROVED: Final[str] = "APPROVED"
SLOT_STATUS_ELIMINATED: Final[str] = "ELIMINATED"
SLOT_STATUS_REPLACED: Final[str] = "REPLACED"
SLOT_STATUSES: Final[frozenset[str]] = frozenset(
    {
        SLOT_STATUS_UNBOUND,
        SLOT_STATUS_AWAITING_REVIEW,
        SLOT_STATUS_APPROVED,
        SLOT_STATUS_ELIMINATED,
        SLOT_STATUS_REPLACED,
    }
)

# WP29 审核决定（各写一份，parity 测试对账）。
REVIEW_DECISION_APPROVED: Final[str] = "APPROVED"
REVIEW_DECISION_ELIMINATED: Final[str] = "ELIMINATED"

# 内容状态（WP26，各写一份）。
CONTENT_STATE_GENERATED: Final[str] = "GENERATED"
CONTENT_STATE_FAILED: Final[str] = "FAILED"
#: "已落定"的内容状态（出活完毕的判据）。
CONTENT_SETTLED_STATES: Final[frozenset[str]] = frozenset(
    {CONTENT_STATE_GENERATED, CONTENT_STATE_FAILED}
)

# ---------------------------------------------------------------------------
# 关口 / 入口条件码（不落库）
# ---------------------------------------------------------------------------
GATE_ROUND_ALL_DISPOSED: Final[str] = "ROUND_ALL_DISPOSED"
GATE_NO_OPEN_BLOCKING_FEEDBACK: Final[str] = "NO_OPEN_BLOCKING_FEEDBACK"
GATE_DIRECTION_HAS_APPROVED_SAMPLE: Final[str] = "DIRECTION_HAS_APPROVED_SAMPLE"
GATE_SELF_CHECK_COMPLETE: Final[str] = "SELF_CHECK_COMPLETE"
GATE_CONDITION_CODES: Final[tuple[str, ...]] = (
    GATE_ROUND_ALL_DISPOSED,
    GATE_NO_OPEN_BLOCKING_FEEDBACK,
    GATE_DIRECTION_HAS_APPROVED_SAMPLE,
    GATE_SELF_CHECK_COMPLETE,
)
#: 硬条件（Q6 默认：SELF_CHECK_COMPLETE 只报告、不阻断）。
GATE_HARD_CONDITIONS: Final[frozenset[str]] = frozenset(
    {
        GATE_ROUND_ALL_DISPOSED,
        GATE_NO_OPEN_BLOCKING_FEEDBACK,
        GATE_DIRECTION_HAS_APPROVED_SAMPLE,
    }
)

ENTRY_ACTIVE_BASELINE: Final[str] = "ACTIVE_BASELINE"
ENTRY_STEP1_APPROVED: Final[str] = "STEP1_APPROVED"
ENTRY_STEP2_APPROVED: Final[str] = "STEP2_APPROVED"
ENTRY_STEP3_APPROVED: Final[str] = "STEP3_APPROVED"
ENTRY_PLAN_BOUND_TO_ACTIVE_BASELINE: Final[str] = "PLAN_BOUND_TO_ACTIVE_BASELINE"
ENTRY_CONDITION_CODES: Final[tuple[str, ...]] = (
    ENTRY_ACTIVE_BASELINE,
    ENTRY_STEP1_APPROVED,
    ENTRY_STEP2_APPROVED,
    ENTRY_STEP3_APPROVED,
    ENTRY_PLAN_BOUND_TO_ACTIVE_BASELINE,
)

# step_approvals 的口径（baseline 的取值，各写一份）。
STEP_BRIEF: Final[int] = 1
STEP_RESEARCH: Final[int] = 2
STEP_BASELINE: Final[int] = 3
SUBJECT_KIND_BRIEF: Final[str] = "BRIEF"
SUBJECT_KIND_RESEARCH: Final[str] = "RESEARCH"
SUBJECT_KIND_BASELINE: Final[str] = "BASELINE"

# ---------------------------------------------------------------------------
# details.reason 码（不落库）
# ---------------------------------------------------------------------------
REASON_ROUND_NOT_FOUND: Final[str] = "TRIAL_ROUND_NOT_FOUND"
REASON_SLOT_NOT_FOUND: Final[str] = "TRIAL_SLOT_NOT_FOUND"
REASON_PROJECT_NOT_FOUND: Final[str] = "PROJECT_NOT_FOUND"
REASON_ROUND_TRANSITION_ILLEGAL: Final[str] = "ROUND_TRANSITION_ILLEGAL"
REASON_ROUND_TERMINAL: Final[str] = "ROUND_TERMINAL"
REASON_ACTIVE_ROUND_EXISTS: Final[str] = "ACTIVE_ROUND_EXISTS"
REASON_STEP_APPROVAL_MISSING: Final[str] = "STEP_APPROVAL_MISSING"
REASON_NO_ACTIVE_BASELINE: Final[str] = "NO_ACTIVE_BASELINE"
REASON_PLAN_BASELINE_STALE: Final[str] = "PLAN_BASELINE_STALE"
REASON_PLAN_DRAFT_DRIFTED: Final[str] = "PLAN_DRAFT_DRIFTED"
REASON_SOURCE_ROUND_NOT_PASSED: Final[str] = "SOURCE_ROUND_NOT_PASSED"
REASON_ROUND_NOT_SETTLED: Final[str] = "ROUND_NOT_SETTLED"
REASON_GATE_NOT_SATISFIED: Final[str] = "GATE_NOT_SATISFIED"
REASON_SLOT_NOT_ELIMINATED: Final[str] = "SLOT_NOT_ELIMINATED"
REASON_SLOT_ALREADY_REPLACED: Final[str] = "SLOT_ALREADY_REPLACED"
REASON_SLOT_ALREADY_BOUND: Final[str] = "SLOT_ALREADY_BOUND"
REASON_CONTENT_ALREADY_BOUND: Final[str] = "CONTENT_ALREADY_BOUND"
REASON_REPLACEMENT_CAP_REACHED: Final[str] = "REPLACEMENT_CAP_REACHED"
#: 设计稿 §6.3 清单之外新增（实现回填登记）：绑定内容仅 GENERATING 轮可做。
REASON_ROUND_NOT_GENERATING: Final[str] = "ROUND_NOT_GENERATING"
REASON_IDEMPOTENCY_KEY_REUSED: Final[str] = "idempotency_key_reused"
REASON_LOCK_VERSION_MISMATCH: Final[str] = "TRIAL_ROUND_LOCK_VERSION_MISMATCH"

# 422 类
REASON_TO_STATE_USE_GATE_DECISION: Final[str] = "TO_STATE_USE_GATE_DECISION"
REASON_TO_STATE_INVALID: Final[str] = "TO_STATE_INVALID"
REASON_REASON_REQUIRED: Final[str] = "REASON_REQUIRED"
REASON_ALLOCATION_EMPTY: Final[str] = "ALLOCATION_EMPTY"
REASON_SCENE_KEY_UNKNOWN: Final[str] = "SCENE_KEY_UNKNOWN"
REASON_SCENE_KEYS_REQUIRED: Final[str] = "SCENE_KEYS_REQUIRED"
REASON_KIND_INVALID: Final[str] = "KIND_INVALID"
REASON_SOURCE_ROUND_REQUIRED: Final[str] = "SOURCE_ROUND_REQUIRED"
REASON_PLAN_VERSION_INVALID: Final[str] = "PLAN_VERSION_INVALID"
REASON_QUANTITY_INVALID: Final[str] = "QUANTITY_INVALID"
REASON_CONTENT_NOT_FOUND: Final[str] = "CONTENT_NOT_FOUND"
REASON_CONTENT_PROJECT_MISMATCH: Final[str] = "CONTENT_PROJECT_MISMATCH"
REASON_CONTENT_BATCH_KIND_MISMATCH: Final[str] = "CONTENT_BATCH_KIND_MISMATCH"
REASON_CONTENT_SCENE_MISMATCH: Final[str] = "CONTENT_SCENE_MISMATCH"
REASON_DIRECTION_UNVERIFIED: Final[str] = "DIRECTION_UNVERIFIED"

# 关口过期原因（只读接缝的 stale_reasons）。
STALE_BASELINE_CHANGED: Final[str] = "BASELINE_CHANGED"
STALE_CONTENT_REVISED: Final[str] = "CONTENT_REVISED"
STALE_REVIEW_CHANGED: Final[str] = "REVIEW_CHANGED"

# 审计动作。
AUDIT_OBJECT_ROUND: Final[str] = "trial_round"
AUDIT_ACTION_CREATE: Final[str] = "trial_round.create"
AUDIT_ACTION_TRANSITION: Final[str] = "trial_round.transition"
AUDIT_ACTION_REPLACE: Final[str] = "trial_round.replace_slot"
AUDIT_ACTION_GATE: Final[str] = "trial_round.gate_decision"


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SlotFact:
    """一个名额 + 其内容在"当前版本"上的全部事实（只读视图，一次查询得到）。"""

    slot_id: Any
    round_id: Any
    slot_no: int
    scene_key: str
    form_key: str | None
    content_item_id: Any | None
    replaces_slot_id: Any | None
    replaced: bool
    content_state: str | None
    current_version_id: Any | None
    #: 当前版本上未被取代的有效审核（无则 None）。
    review_id: Any | None
    review_decision: str | None
    #: 该内容存在过任何审核（用于 reviewStale：有旧结论但不在当前版本上）。
    any_review: bool
    #: 有效审核绑定的版本是否为该内容的首版（approvedOnFirstVersion）。
    review_on_first_version: bool
    open_blocking: bool
    #: 最新一轮自检判的是否就是当前版本（STALE/无轮次 ⇒ False）。
    self_checked: bool

    @property
    def status(self) -> str:
        return derive_slot_status(self)


def derive_slot_status(fact: SlotFact) -> str:
    """名额处置状态（读时派生）。`REPLACED` 压过一切：死名额不再被改判复活。"""
    if fact.replaced:
        return SLOT_STATUS_REPLACED
    if fact.content_item_id is None:
        return SLOT_STATUS_UNBOUND
    if fact.review_decision == REVIEW_DECISION_APPROVED:
        return SLOT_STATUS_APPROVED
    if fact.review_decision == REVIEW_DECISION_ELIMINATED:
        return SLOT_STATUS_ELIMINATED
    return SLOT_STATUS_AWAITING_REVIEW


def review_stale(fact: SlotFact) -> bool:
    """有审核历史，但当前版本上没有有效结论（改稿后旧结论失效）。"""
    return (
        fact.content_item_id is not None
        and fact.review_decision is None
        and fact.any_review
    )


def ratio(numerator: int, denominator: int) -> dict[str, Any]:
    """比率的响应形态。分母 0 ⇒ `permille: null`（**绝不写 0**）；约数向下取整并带 `rounded`。"""
    return {
        "numerator": numerator,
        "denominator": denominator,
        "permille": None if denominator == 0 else (numerator * 1000) // denominator,
        "rounded": True,
    }
