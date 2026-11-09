"""WP32 扩产 / 批产的存储模型：三张表（逐条对应 `docs/m3/WP32-ddl-草案.py`）。

| 表 | 一句话 | 可变性 |
|---|---|---|
| `batches` | 一个批次：档位 / 序号 / 数量 / 钉住的方案与基线 / 分配快照 / 创建时刻上限快照 / 预估 / 幂等键 | `state` 与 `lock_version` 等“命令位”可变，其余写入后不改 |
| `batch_items` | 批次内第 n 条内容；`UNIQUE(content_item_id)` | **不可变** |
| `batch_item_runs` | 某条内容的第 n 轮执行 = 一个 job；`UNIQUE(batch_item_id, round_no)` / `UNIQUE(job_id)` | **不可变** |

## 批次行只存“命令”，执行进度永远从 `jobs` 读时折算（`progress.py`）

执行状态的权威是 `jobs`；批次行再存一份就是第二处声称，且没有钩子能回写（`jobs/*.py` 禁改）。

## ADR-13：`quantity` 只有下界 `> 0`

上界在 `runtime_settings['batch.limits']` + 应用层校验；`limit_applied` / `limit_source`
只快照**创建时刻**的生效上界与来源。**本文件不得出现 10 / 30 / 50 作为 quantity 的比较右值。**

## 写路径

`batches` 的列写入面只有两个：INSERT 与 `repository.cas_update_batch`（改 `state` /
`lock_version` / `updated_at` / `cancel_requested_*`）。不做列级守卫触发器（说明 §1.5 的已知取舍）。

## 关于 `jobs.batch_id` 的 FK

目标 FK 与索引是主线收口动作（`docs/m3/WP32-ddl-草案.py` 第六节），本包不改 `jobs`。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.batches.types import (
    BATCH_KIND_LIST_SQL,
    BATCH_STATE_CANCEL_REQUESTED,
    BATCH_STATE_LIST_SQL,
    BATCH_STATE_SUBMITTED,
    LIMIT_SOURCE_LIST_SQL,
)
from rednote.platform.base import (
    Base,
    created_at_column,
    lock_version_column,
    updated_at_column,
    uuid_pk,
)
from rednote.platform.immutability import guard_immutable

#: 不可变表（`ddl.py` 从同一常量派生 `trg_<table>_immutable`）。**顺序即建表顺序**。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = ("batch_items", "batch_item_runs")

#: 本模块拥有的全部表（建表顺序：`batches` → `batch_items` → `batch_item_runs`）。
OWN_TABLES: Final[tuple[str, ...]] = ("batches", "batch_items", "batch_item_runs")


class Batch(Base):
    """一个批次。**可变**，但只有两个命令位会变（`state`、取消三列 + `lock_version`）。"""

    __tablename__ = "batches"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    batch_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    #: 项目内同档位序号。展示码 `BATCH-S01` 读时拼，不存。
    seq_no: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 本批条数（只有下界；上界见模块 docstring）。
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text(f"'{BATCH_STATE_SUBMITTED}'")
    )

    #: 钉住的 PLAN 版本节点（`version_nodes.id`）。
    plan_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("version_nodes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: 提交时钉住的基线（= `projects.active_baseline_id` 的快照）。不存 revision。
    baseline_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("production_baselines.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: 本批分配快照（scenes / forms / unallocated）；与 PLAN 快照节点 payload 同一份内容。
    allocation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    #: 创建时刻该档位的生效上界与来源（ADR-13 迁移影响 #4）。
    limit_applied: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_source: Mapped[str] = mapped_column(String(16), nullable=False)

    #: 提交时预估总额；NULL = 未预估（**不是 0**）。与币种同有同无。
    estimated_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8), nullable=True
    )
    estimated_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    #: `[{taskKind, estimateId}]`；**无 FK 的审计指针**（代价见 DDL 草案注释）。
    estimate_refs: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    #: ETag 载体（`build_etag(id, lock_version)`）；架构文档 `batch.revision` 的本仓对应物。
    lock_version: Mapped[int] = lock_version_column()

    __table_args__ = (
        # --- 取值域（字面量化石在迁移；这里与 types.py 同值） ----------------
        CheckConstraint(f"batch_kind IN ({BATCH_KIND_LIST_SQL})", name="batch_kind_valid"),
        CheckConstraint(f"state IN ({BATCH_STATE_LIST_SQL})", name="state_valid"),
        CheckConstraint(
            f"limit_source IN ({LIMIT_SOURCE_LIST_SQL})", name="limit_source_valid"
        ),
        # --- 数值边界（ADR-13：只有下界，无 10/30/50） -----------------------
        CheckConstraint("seq_no >= 1", name="seq_no_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("limit_applied >= 1", name="limit_applied_positive"),
        CheckConstraint("lock_version >= 1", name="lock_version_positive"),
        # --- 预估：金额与币种同有同无；NULL / 0 分离 -------------------------
        CheckConstraint(
            "(estimated_amount IS NULL) = (estimated_currency IS NULL)",
            name="estimate_pair_consistent",
        ),
        CheckConstraint(
            "estimated_amount IS NULL OR estimated_amount >= 0",
            name="estimated_amount_non_negative",
        ),
        CheckConstraint(
            "jsonb_typeof(estimate_refs) = 'array'", name="estimate_refs_is_array"
        ),
        CheckConstraint(
            "jsonb_typeof(allocation) = 'object'", name="allocation_is_object"
        ),
        # --- 取消：state 与取消时刻双向等值 ----------------------------------
        CheckConstraint(
            f"(state = '{BATCH_STATE_CANCEL_REQUESTED}') = (cancel_requested_at IS NOT NULL)",
            name="cancel_pair_consistent",
        ),
        CheckConstraint(
            "cancel_requested_by IS NULL OR cancel_requested_at IS NOT NULL",
            name="cancel_actor_requires_time",
        ),
        # --- 幂等载体不得为空串 ----------------------------------------------
        CheckConstraint("char_length(idempotency_key) > 0", name="idempotency_key_not_blank"),
        CheckConstraint("char_length(request_hash) > 0", name="request_hash_not_blank"),
        # --- 唯一约束：幂等与序号的数据库级载体 ------------------------------
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_batches_project_idempotency_key"
        ),
        UniqueConstraint(
            "project_id", "batch_kind", "seq_no", name="uq_batches_project_kind_seq"
        ),
        Index("ix_batches_project_created", "project_id", "created_at"),
        Index("ix_batches_plan_version_id", "plan_version_id"),
        Index("ix_batches_baseline_id", "baseline_id"),
    )


class BatchItem(Base):
    """批次内第 n 条内容。**不可变**。`UNIQUE(content_item_id)`：一条内容至多属于一个批次。"""

    __tablename__ = "batch_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    batch_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("batches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: 与 `content_items.scene_key` / `form_key` 保持一致（接缝 §5.15）；NULL = 未指派。
    #: 联合展开规则未定义（OP-227），本包不编造：当前恒由 content 行回填（现为 NULL）。
    scene_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    form_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("ordinal >= 1", name="ordinal_positive"),
        CheckConstraint(
            "(scene_key IS NULL) = (form_key IS NULL)", name="assignment_paired"
        ),
        CheckConstraint(
            "scene_key IS NULL OR char_length(scene_key) > 0", name="scene_key_not_blank"
        ),
        CheckConstraint(
            "form_key IS NULL OR char_length(form_key) > 0", name="form_key_not_blank"
        ),
        UniqueConstraint("batch_id", "ordinal", name="uq_batch_items_batch_ordinal"),
        UniqueConstraint("content_item_id", name="uq_batch_items_content_item_id"),
    )


class BatchItemRun(Base):
    """某条内容的第 n 轮执行 = 一个 job。**不可变**。第 1 轮 = 初次提交，第 2 轮起 = 失败项重做。"""

    __tablename__ = "batch_item_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    batch_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("batch_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("round_no >= 1", name="round_no_positive"),
        UniqueConstraint(
            "batch_item_id", "round_no", name="uq_batch_item_runs_item_round"
        ),
        UniqueConstraint("job_id", name="uq_batch_item_runs_job_id"),
    )


guard_immutable(BatchItem, BatchItemRun)

assert {BatchItem.__tablename__, BatchItemRun.__tablename__} == set(IMMUTABLE_TABLES)

__all__ = [
    "IMMUTABLE_TABLES",
    "OWN_TABLES",
    "Batch",
    "BatchItem",
    "BatchItemRun",
]
