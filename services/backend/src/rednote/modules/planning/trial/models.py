"""WP31 试产关口的存储模型：三张表。

| 表 | 角色 | 可变性 |
|---|---|---|
| `trial_rounds` | 一轮试产 / 补充试产（状态机宿主） | **可变**，仅 `state` / `cancel_reason` / `updated_at` / `lock_version` |
| `trial_round_slots` | 本轮的一个名额（方向 × 可选形式 + 绑定内容 + 替代关系） | **有限可变**：`content_item_id` / `bound_at` 从 NULL 写一次 |
| `trial_gate_decisions` | 一次"批准进入扩产"的人工决定 + 当时的条件 / 统计 / 依据 | **不可变** |

名额的"处置结论"**不存列**：它是 WP29 `content_reviews` 的事实（含改判链），
再存一列就是第二个真值来源。处置状态由 `types.derive_slot_status` 读时派生。

跨表归属（FK 管不住的）由 `ddl.py` 的触发器守；本文件只表达列与 CHECK / 唯一 / 索引。
取值域落库 CHECK 的只有 `kind` / `state` / `limit_source`；其余见 `types.py` 模块 docstring。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.planning.trial.types import (
    LIMIT_SOURCE_LIST_SQL,
    MAX_IDEMPOTENCY_KEY_CHARS,
    MAX_QUANTITY_BOUND,
    MAX_REASON_CHARS,
    MAX_SCENE_KEY_CHARS,
    REQUEST_HASH_CHARS,
    ROUND_KIND_LIST_SQL,
    ROUND_OPEN_STATE_LIST_SQL,
    ROUND_STATE_LIST_SQL,
    ROUND_STATE_PLANNED,
)
from rednote.platform.base import (
    Base,
    created_at_column,
    lock_version_column,
    updated_at_column,
    uuid_pk,
)
from rednote.platform.immutability import guard_immutable

#: 本模块拥有的全部表，**顺序即建表顺序**（引用方在后）。
OWN_TABLES: Final[tuple[str, ...]] = (
    "trial_rounds",
    "trial_round_slots",
    "trial_gate_decisions",
)

#: 其中完全不可变的表（`rednote_reject_mutation` 守卫）。其余两张用自己的守卫触发器。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = ("trial_gate_decisions",)


class TrialRound(Base):
    """一轮试产 / 补充试产。**可变**（`lock_version` 是 ETag 载体）。"""

    __tablename__ = "trial_rounds"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: 项目内序号，从 1 起；服务层在项目级 advisory lock 下取号。
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)

    kind: Mapped[str] = mapped_column(String(12), nullable=False)

    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text(f"'{ROUND_STATE_PLANNED}'")
    )

    #: 本轮钉住的 PLAN 版本（`version_nodes.id`）。显式钉住，不取"最新"。
    plan_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("version_nodes.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: 创建那一刻的 `projects.active_baseline_id`（投影），不是"最新创建的基线"。
    baseline_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("production_baselines.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: 仅 SUPPLEMENT：补充的是哪一轮（须为 GATE_PASSED 的 TRIAL）。
    source_round_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("trial_rounds.id", ondelete="RESTRICT"),
        nullable=True,
    )

    requested_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 实际切出的名额数；< requested 即"分不完"（`unallocated > 0` 是合法结果）。
    planned_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 创建时 TRIAL 档生效上限的**快照**（审计用，不是配置源）。
    limit_applied: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_source: Mapped[str] = mapped_column(String(12), nullable=False)

    #: 钉住那一刻的 PLAN payload（`planning.service.plan_payload` 的形状）。
    #: PLAN 正文不在 `version_nodes` 里（只有 payload_hash），见说明文档 §3.2。
    plan_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    cancel_reason: Mapped[str | None] = mapped_column(
        String(MAX_REASON_CHARS), nullable=True
    )

    idempotency_key: Mapped[str | None] = mapped_column(
        String(MAX_IDEMPOTENCY_KEY_CHARS), nullable=True
    )
    request_hash: Mapped[str | None] = mapped_column(
        String(REQUEST_HASH_CHARS), nullable=True
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    lock_version: Mapped[int] = lock_version_column()

    __table_args__ = (
        CheckConstraint(f"kind IN ({ROUND_KIND_LIST_SQL})", name="kind_valid"),
        CheckConstraint(f"state IN ({ROUND_STATE_LIST_SQL})", name="state_valid"),
        CheckConstraint(
            f"limit_source IN ({LIMIT_SOURCE_LIST_SQL})", name="limit_source_valid"
        ),
        CheckConstraint("round_no >= 1", name="round_no_positive"),
        CheckConstraint("requested_quantity >= 1", name="requested_quantity_positive"),
        CheckConstraint("planned_quantity >= 1", name="planned_quantity_positive"),
        CheckConstraint(
            "planned_quantity <= requested_quantity", name="planned_not_above_requested"
        ),
        CheckConstraint(
            f"requested_quantity <= {MAX_QUANTITY_BOUND}", name="requested_within_bound"
        ),
        CheckConstraint("limit_applied >= 1", name="limit_applied_positive"),
        # 列间比较，不含业务数字（Q14）。
        CheckConstraint(
            "requested_quantity <= limit_applied", name="requested_within_limit_applied"
        ),
        CheckConstraint(
            "jsonb_typeof(plan_snapshot) = 'object'", name="plan_snapshot_is_object"
        ),
        CheckConstraint(
            "(kind = 'SUPPLEMENT') = (source_round_id IS NOT NULL)",
            name="supplement_has_source",
        ),
        CheckConstraint(
            "source_round_id IS NULL OR source_round_id <> id", name="source_not_self"
        ),
        CheckConstraint(
            "(state = 'CANCELLED') = (cancel_reason IS NOT NULL)",
            name="cancel_reason_paired_with_cancelled",
        ),
        CheckConstraint(
            "cancel_reason IS NULL OR char_length(btrim(cancel_reason)) > 0",
            name="cancel_reason_not_blank",
        ),
        CheckConstraint(
            "(idempotency_key IS NULL) = (request_hash IS NULL)",
            name="idempotency_paired",
        ),
        CheckConstraint(
            "idempotency_key IS NULL OR char_length(btrim(idempotency_key)) > 0",
            name="idempotency_key_not_blank",
        ),
        CheckConstraint(
            f"request_hash IS NULL OR char_length(request_hash) = {REQUEST_HASH_CHARS}",
            name="request_hash_length",
        ),
        UniqueConstraint("project_id", "round_no"),
        # 同一项目同一时刻至多一轮"未终态"（Q9）。WHERE 子句是判据的一部分。
        Index(
            "uq_trial_rounds_project_open",
            "project_id",
            unique=True,
            postgresql_where=text(f"state IN ({ROUND_OPEN_STATE_LIST_SQL})"),
        ),
        # 幂等：同一项目下同一个 Idempotency-Key 只有一行。
        Index(
            "uq_trial_rounds_project_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_trial_rounds_source_round", "source_round_id"),
    )


class TrialRoundSlot(Base):
    """本轮的一个名额。**有限可变**（`content_item_id` / `bound_at` 只能 NULL → 值 一次）。"""

    __tablename__ = "trial_round_slots"

    id: Mapped[uuid.UUID] = uuid_pk()

    round_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("trial_rounds.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: 轮内序号，从 1 起；替代名额取新号，不复用。
    slot_no: Mapped[int] = mapped_column(Integer, nullable=False)

    #: 方向 = PLAN payload 里 `scenes[].key`。
    scene_key: Mapped[str] = mapped_column(
        String(MAX_SCENE_KEY_CHARS), nullable=False
    )
    #: 形式 key；NULL = 未指派（**不是**空串）。格级指派规则见 OP-227。
    form_key: Mapped[str | None] = mapped_column(
        String(MAX_SCENE_KEY_CHARS), nullable=True
    )

    #: NULL = 还没出活（**不是**"0 条"）；write-once。
    content_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    bound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: 淘汰补产：本名额替代了哪个名额。
    replaces_slot_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("trial_round_slots.id", ondelete="RESTRICT"),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint("slot_no >= 1", name="slot_no_positive"),
        CheckConstraint("char_length(btrim(scene_key)) > 0", name="scene_key_not_blank"),
        CheckConstraint(
            "form_key IS NULL OR char_length(btrim(form_key)) > 0",
            name="form_key_not_blank",
        ),
        CheckConstraint(
            "replaces_slot_id IS NULL OR replaces_slot_id <> id", name="replaces_not_self"
        ),
        CheckConstraint(
            "(content_item_id IS NULL) = (bound_at IS NULL)",
            name="bound_at_paired_with_content",
        ),
        UniqueConstraint("round_id", "slot_no"),
        # 一条内容全局至多属于一个名额；唯一约束允许多个 NULL。
        UniqueConstraint("content_item_id"),
        # 一个名额至多被替代一次（并发双击"补产"的最后一道）。
        UniqueConstraint("replaces_slot_id"),
        Index("ix_trial_round_slots_round_scene", "round_id", "scene_key"),
    )


class TrialGateDecision(Base):
    """一次"批准进入扩产"的人工决定。**不可变**；每轮至多一条。

    没有 `project_id` 冗余列：经 `round_id` 一跳可得，冗余列就要配触发器钉住。
    """

    __tablename__ = "trial_gate_decisions"

    id: Mapped[uuid.UUID] = uuid_pk()

    round_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("trial_rounds.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: 批准进入扩产的方向（scene_key 数组，非空）。
    approved_scene_keys: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    #: `{条件码: {satisfied, blocking, detail}}`。
    condition_results: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: 计数 + 比率（比率分母为 0 ⇒ null）。
    statistics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: 逐名额依据数组 `(slotId, contentItemId, subjectVersionId, reviewId)`。
    basis: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)

    reason: Mapped[str] = mapped_column(String(MAX_REASON_CHARS), nullable=False)

    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(approved_scene_keys) = 'array'",
            name="approved_scene_keys_is_array",
        ),
        # CASE 而非 AND：AND 不保证短路，标量上调 jsonb_array_length 会抛 22023 而非 23514。
        CheckConstraint(
            "CASE WHEN jsonb_typeof(approved_scene_keys) = 'array'"
            " THEN jsonb_array_length(approved_scene_keys) >= 1"
            " ELSE false END",
            name="approved_scene_keys_not_empty",
        ),
        CheckConstraint(
            "jsonb_typeof(condition_results) = 'object'",
            name="condition_results_is_object",
        ),
        CheckConstraint(
            "jsonb_typeof(statistics) = 'object'", name="statistics_is_object"
        ),
        CheckConstraint("jsonb_typeof(basis) = 'array'", name="basis_is_array"),
        CheckConstraint("char_length(btrim(reason)) > 0", name="reason_not_blank"),
        UniqueConstraint("round_id"),
    )


guard_immutable(TrialGateDecision)

assert tuple(
    t for t in OWN_TABLES if t in IMMUTABLE_TABLES
) == IMMUTABLE_TABLES
assert {
    TrialRound.__tablename__,
    TrialRoundSlot.__tablename__,
    TrialGateDecision.__tablename__,
} == set(OWN_TABLES)
