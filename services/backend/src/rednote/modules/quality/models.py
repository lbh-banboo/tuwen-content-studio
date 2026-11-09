"""WP33 重复检查 / 质量报告的存储模型：7 张表（逐条对应 `docs/m3/WP33-ddl-草案.py`）。

| 表 | 一句话 | 可变性 |
|---|---|---|
| `quality_reports` | 报告系列身份（项目 × 范围）+ `current_version_id` 投影 | **可变**（仅投影列 / `updated_at` / `lock_version`） |
| `quality_report_versions` | 一次计算 = 一个 REPORT 版本（id 同时 FK `version_nodes.id`） | 不可变 |
| `quality_report_items` | 逐篇：纳入 / 排除 + 钉死的内容版本 + 可追溯上下文 | 不可变 |
| `quality_dimension_results` | 逐维度（每版本恰 7 行），NULL / 0 分离的 CHECK | 不可变 |
| `quality_similarity_pairs` | 命中的相似对（仅 COMPUTED 维度、仅 ≥ 单对阈值） | 不可变 |
| `quality_media_alignments` | 顺序对位的逐槽位结果 | 不可变 |
| `quality_report_artifacts` | 导出产物 ⇒ `file_objects`（`PURPOSE_GENERATED`） | 不可变 |

* **对 WP30 / WP31 / WP32 的表零外键**；`quality_reports.batch_id` 是**无 FK 的审计指针**（D2，设计稿 §3.1）。
* 取值域 CHECK 与迁移同值，迁移里写字面量（ISS-191）；`not_computed_reason` / `exclude_reason` /
  对位 `reason` / `media_state` **刻意不落 CHECK**（开集 / 借用他人词汇）。
* 复合外键与 `generation_context_id` 的 FK **显式 `name=`**，否则约定名超 PG 的 63 字节上限会被静默截断。
* 无 float / numeric 列：相似度与阈值全是整数千分比。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.quality.types import (
    ALIGN_VERDICT_LIST_SQL,
    ARTIFACT_FORMAT_LIST_SQL,
    DIMENSION_LIST_SQL,
    DIMENSION_STATUS_LIST_SQL,
    ITEM_STATUS_LIST_SQL,
    OUTCOME_LIST_SQL,
    SCOPE_KIND_LIST_SQL,
    TEXT_DIMENSION_LIST_SQL,
    UNIMPLEMENTED_DIMENSION_LIST_SQL,
    VERDICT_LIST_SQL,
)
from rednote.platform.base import (
    Base,
    created_at_column,
    updated_at_column,
    uuid_pk,
)
from rednote.platform.immutability import guard_immutable

#: 不可变表（`ddl.py` 从同一常量派生 `trg_<table>_immutable`）。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = (
    "quality_report_versions",
    "quality_report_items",
    "quality_dimension_results",
    "quality_similarity_pairs",
    "quality_media_alignments",
    "quality_report_artifacts",
)
#: 本模块拥有的全部表。**顺序即建表顺序**（`quality_reports` 的延迟 FK 由 `create_all` 自动拆环）。
OWN_TABLES: Final[tuple[str, ...]] = ("quality_reports", *IMMUTABLE_TABLES)


class QualityReport(Base):
    """报告系列身份。**可变**，但只有投影列（`current_version_id` / `current_revision_no`）会变。"""

    __tablename__ = "quality_reports"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 仅 BATCH 范围非空；指向 WP32 `batches.id`，**无 FK**（D2）。
    batch_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    #: 投影；NULL = 尚无任何版本（合法的中间态）。延迟 FK，与 `content_items.current_version_id` 同款。
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "quality_report_versions.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    current_revision_no: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    #: 只服务投影推进的 CAS，**不暴露为 ETag**（本包零 If-Match）。
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )

    __table_args__ = (
        CheckConstraint(f"scope_kind IN ({SCOPE_KIND_LIST_SQL})", name="scope_kind_valid"),
        CheckConstraint(
            "(scope_kind = 'BATCH') = (batch_id IS NOT NULL)", name="batch_id_paired"
        ),
        CheckConstraint("char_length(scope_digest) = 64", name="scope_digest_length"),
        CheckConstraint("current_revision_no >= 0", name="revision_no_non_negative"),
        CheckConstraint(
            "(current_version_id IS NULL) = (current_revision_no = 0)",
            name="projection_paired",
        ),
        CheckConstraint("lock_version >= 1", name="lock_version_positive"),
        UniqueConstraint("project_id", "scope_digest", name="uq_quality_reports_project_id_scope_digest"),
        Index("ix_quality_reports_project_id_updated_at", "project_id", "updated_at", "id"),
    )


class QualityReportVersion(Base):
    """一次计算 = 一个 REPORT 版本。**不可变**。"""

    __tablename__ = "quality_report_versions"

    #: = `register_version` 返回的 node_id（同时 FK `version_nodes.id`，延迟）。
    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "version_nodes.id", ondelete="RESTRICT", deferrable=True, initially="DEFERRED"
        ),
        primary_key=True,
    )
    report_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("quality_reports.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    algo_version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    included_item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    excluded_item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ok_dimension_count: Mapped[int] = mapped_column(Integer, nullable=False)
    alert_dimension_count: Mapped[int] = mapped_column(Integer, nullable=False)
    not_computed_dimension_count: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(f"outcome IN ({OUTCOME_LIST_SQL})", name="outcome_valid"),
        CheckConstraint("revision_no >= 1", name="revision_no_positive"),
        CheckConstraint("algo_version >= 1", name="algo_version_positive"),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        CheckConstraint("char_length(input_fingerprint) = 64", name="fingerprint_length"),
        CheckConstraint("char_length(payload_hash) = 64", name="payload_hash_length"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_is_object"),
        CheckConstraint(
            "included_item_count >= 0 AND excluded_item_count >= 0",
            name="item_counts_non_negative",
        ),
        CheckConstraint(
            "item_count = included_item_count + excluded_item_count", name="item_count_sum"
        ),
        CheckConstraint(
            "ok_dimension_count >= 0 AND alert_dimension_count >= 0"
            " AND not_computed_dimension_count >= 0",
            name="dimension_counts_non_negative",
        ),
        CheckConstraint(
            "ok_dimension_count + alert_dimension_count + not_computed_dimension_count = 7",
            name="dimension_count_sum",
        ),
        CheckConstraint(
            "(outcome = 'ALERTED') = (alert_dimension_count > 0)",
            name="outcome_alerted_equivalence",
        ),
        CheckConstraint("finished_at >= started_at", name="finished_not_before_started"),
        UniqueConstraint(
            "report_id", "revision_no", name="uq_quality_report_versions_report_id_revision_no"
        ),
        Index(
            "ix_quality_report_versions_report_id_revision_no", "report_id", "revision_no", "id"
        ),
    )


class QualityReportItem(Base):
    """报告的逐篇成员。**不可变**。EXCLUDED 的篇目也有行。"""

    __tablename__ = "quality_report_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    report_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "quality_report_versions.id",
            ondelete="RESTRICT",
            name="fk_quality_report_items_report_version_id_versions",
        ),
        nullable=False,
    )
    content_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: NULL ⇔ 该篇没有任何版本（NO_VERSION）。
    content_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 开集（不落 CHECK）：NO_VERSION / EMPTY_BODY。
    exclude_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    #: NULL=无版本；0=空正文；INCLUDED 必 >=1。
    paragraph_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: NULL = 该版本无生成上下文。约定名 65 字节超限 ⇒ 显式短名。
    generation_context_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "generation_contexts.id",
            ondelete="RESTRICT",
            name="fk_quality_report_items_generation_context_id",
        ),
        nullable=True,
    )
    baseline_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("production_baselines.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(f"status IN ({ITEM_STATUS_LIST_SQL})", name="status_valid"),
        CheckConstraint("position >= 0", name="position_non_negative"),
        CheckConstraint(
            "paragraph_count IS NULL OR paragraph_count >= 0",
            name="paragraph_count_non_negative",
        ),
        CheckConstraint(
            "(status = 'EXCLUDED') = (exclude_reason IS NOT NULL)",
            name="exclude_reason_paired",
        ),
        CheckConstraint(
            "status <> 'INCLUDED' OR (content_version_id IS NOT NULL"
            " AND paragraph_count IS NOT NULL AND paragraph_count >= 1)",
            name="included_has_subject",
        ),
        UniqueConstraint(
            "report_version_id", "content_item_id", name="uq_quality_report_items_version_content"
        ),
        UniqueConstraint(
            "report_version_id", "position", name="uq_quality_report_items_version_position"
        ),
        Index("ix_quality_report_items_content_item_id", "content_item_id"),
    )


class QualityDimensionResult(Base):
    """逐维度（每版本恰 7 行）。NULL / 0 / 约数分离的数据库落点。**不可变**。"""

    __tablename__ = "quality_dimension_results"

    id: Mapped[uuid.UUID] = uuid_pk()
    report_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "quality_report_versions.id",
            ondelete="RESTRICT",
            name="fk_quality_dimension_results_report_version_id_versions",
        ),
        nullable=False,
    )
    dimension: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    not_computed_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    verdict: Mapped[str | None] = mapped_column(String(8), nullable=True)
    compared_item_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flagged_item_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 阈值**快照**（占位，待 OP-228）：改常量不改写历史。
    pair_threshold_permille: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alert_threshold_permille: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_similarity_permille: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(f"dimension IN ({DIMENSION_LIST_SQL})", name="dimension_valid"),
        CheckConstraint(f"status IN ({DIMENSION_STATUS_LIST_SQL})", name="status_valid"),
        CheckConstraint(
            f"verdict IS NULL OR verdict IN ({VERDICT_LIST_SQL})", name="verdict_valid"
        ),
        CheckConstraint(
            "(status = 'COMPUTED') = (compared_item_count IS NOT NULL"
            " AND flagged_item_count IS NOT NULL"
            " AND pair_threshold_permille IS NOT NULL"
            " AND alert_threshold_permille IS NOT NULL"
            " AND max_similarity_permille IS NOT NULL AND verdict IS NOT NULL)",
            name="computed_columns_present",
        ),
        CheckConstraint(
            "(status = 'NOT_COMPUTED') = (compared_item_count IS NULL"
            " AND flagged_item_count IS NULL"
            " AND pair_threshold_permille IS NULL"
            " AND alert_threshold_permille IS NULL"
            " AND max_similarity_permille IS NULL AND verdict IS NULL)",
            name="not_computed_columns_absent",
        ),
        CheckConstraint(
            "(status = 'NOT_COMPUTED') = (not_computed_reason IS NOT NULL)",
            name="reason_paired",
        ),
        CheckConstraint(
            "compared_item_count IS NULL OR compared_item_count >= 2",
            name="compared_at_least_two",
        ),
        CheckConstraint(
            "flagged_item_count IS NULL OR (flagged_item_count >= 0"
            " AND flagged_item_count <= compared_item_count)",
            name="flagged_range",
        ),
        CheckConstraint(
            "flagged_item_count IS NULL OR flagged_item_count <> 1", name="flagged_not_one"
        ),
        CheckConstraint(
            "(pair_threshold_permille IS NULL OR pair_threshold_permille BETWEEN 0 AND 1000)"
            " AND (alert_threshold_permille IS NULL"
            " OR alert_threshold_permille BETWEEN 0 AND 1000)",
            name="thresholds_range",
        ),
        CheckConstraint(
            "max_similarity_permille IS NULL OR max_similarity_permille BETWEEN 0 AND 1000",
            name="max_similarity_range",
        ),
        CheckConstraint(
            "verdict IS NULL OR ((verdict = 'ALERT')"
            " = (flagged_item_count * 1000 > alert_threshold_permille * compared_item_count))",
            name="verdict_alert_equivalence",
        ),
        CheckConstraint(
            f"dimension NOT IN ({UNIMPLEMENTED_DIMENSION_LIST_SQL}) OR status = 'NOT_COMPUTED'",
            name="unimplemented_not_computed",
        ),
        UniqueConstraint(
            "report_version_id", "dimension", name="uq_quality_dimension_results_version_dimension"
        ),
    )


class QualitySimilarityPair(Base):
    """命中的相似对（`a < b`）。**不可变**。"""

    __tablename__ = "quality_similarity_pairs"

    id: Mapped[uuid.UUID] = uuid_pk()
    report_version_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    dimension: Mapped[str] = mapped_column(String(24), nullable=False)
    content_item_a_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    content_item_b_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    intersection_count: Mapped[int] = mapped_column(Integer, nullable=False)
    union_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: `floor(1000*∩/∪)`，由上两列派生并被 CHECK 钉住（近似值）。
    similarity_permille: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(f"dimension IN ({TEXT_DIMENSION_LIST_SQL})", name="dimension_is_text"),
        CheckConstraint("content_item_a_id < content_item_b_id", name="ordered"),
        CheckConstraint(
            "union_count >= 1 AND intersection_count >= 0"
            " AND intersection_count <= union_count",
            name="counts_valid",
        ),
        CheckConstraint(
            "similarity_permille = (intersection_count * 1000) / union_count",
            name="permille_derived",
        ),
        UniqueConstraint(
            "report_version_id",
            "dimension",
            "content_item_a_id",
            "content_item_b_id",
            name="uq_quality_similarity_pairs_version_dimension_pair",
        ),
        ForeignKeyConstraint(
            ["report_version_id", "content_item_a_id"],
            ["quality_report_items.report_version_id", "quality_report_items.content_item_id"],
            name="fk_quality_similarity_pairs_item_a",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["report_version_id", "content_item_b_id"],
            ["quality_report_items.report_version_id", "quality_report_items.content_item_id"],
            name="fk_quality_similarity_pairs_item_b",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["report_version_id", "dimension"],
            [
                "quality_dimension_results.report_version_id",
                "quality_dimension_results.dimension",
            ],
            name="fk_quality_similarity_pairs_dimension_result",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_quality_similarity_pairs_version_dimension_permille",
            "report_version_id",
            "dimension",
            "similarity_permille",
            "id",
        ),
        Index(
            "ix_quality_similarity_pairs_version_item_b", "report_version_id", "content_item_b_id"
        ),
    )


class QualityMediaAlignment(Base):
    """顺序对位的逐槽位结果。**不可变**。**没有** `expected_ref` 唯一约束（多张同段合法）。"""

    __tablename__ = "quality_media_alignments"

    id: Mapped[uuid.UUID] = uuid_pk()
    report_version_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    content_item_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    #: NULL ⇔ MISSING。
    media_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("media_items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    #: `media_items.state` 的快照；借用他人词汇 ⇒ 不落 CHECK。
    media_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    expected_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    actual_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(f"verdict IN ({ALIGN_VERDICT_LIST_SQL})", name="verdict_valid"),
        CheckConstraint("sort_order >= 0", name="sort_order_non_negative"),
        CheckConstraint(
            "expected_ref IS NOT NULL OR media_item_id IS NOT NULL", name="some_fact_present"
        ),
        CheckConstraint(
            "(verdict = 'MISSING') = (media_item_id IS NULL)", name="missing_equivalence"
        ),
        CheckConstraint(
            "(verdict = 'NOT_EVALUABLE') = (expected_ref IS NULL)",
            name="not_evaluable_equivalence",
        ),
        CheckConstraint(
            "(verdict = 'ALIGNED') = (expected_ref IS NOT NULL AND actual_ref IS NOT NULL"
            " AND expected_ref = actual_ref)",
            name="aligned_equivalence",
        ),
        CheckConstraint(
            "(verdict = 'MISALIGNED') = (expected_ref IS NOT NULL AND actual_ref IS NOT NULL"
            " AND expected_ref <> actual_ref)",
            name="misaligned_equivalence",
        ),
        CheckConstraint(
            "(media_item_id IS NULL) = (media_state IS NULL)"
            " AND (media_item_id IS NULL) = (actual_ref IS NULL)",
            name="media_columns_paired",
        ),
        CheckConstraint(
            "(verdict = 'NOT_EVALUABLE') = (reason IS NOT NULL)", name="reason_paired"
        ),
        UniqueConstraint(
            "report_version_id",
            "content_item_id",
            "sort_order",
            name="uq_quality_media_alignments_version_content_slot",
        ),
        UniqueConstraint(
            "report_version_id",
            "media_item_id",
            name="uq_quality_media_alignments_version_media_item",
        ),
        ForeignKeyConstraint(
            ["report_version_id", "content_item_id"],
            ["quality_report_items.report_version_id", "quality_report_items.content_item_id"],
            name="fk_quality_media_alignments_item",
            ondelete="RESTRICT",
        ),
    )


class QualityReportArtifact(Base):
    """报告导出产物（`file_objects`，`PURPOSE_GENERATED`，不占研究配额）。**不可变**。"""

    __tablename__ = "quality_report_artifacts"

    id: Mapped[uuid.UUID] = uuid_pk()
    report_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "quality_report_versions.id",
            ondelete="RESTRICT",
            name="fk_quality_report_artifacts_report_version_id_versions",
        ),
        nullable=False,
    )
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    file_object_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("file_objects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(f"format IN ({ARTIFACT_FORMAT_LIST_SQL})", name="format_valid"),
        CheckConstraint("char_length(content_sha256) = 64", name="sha256_length"),
        CheckConstraint("byte_size >= 0", name="byte_size_non_negative"),
        UniqueConstraint(
            "report_version_id", "format", name="uq_quality_report_artifacts_version_format"
        ),
        UniqueConstraint("file_object_id", name="uq_quality_report_artifacts_file_object_id"),
    )


guard_immutable(
    QualityReportVersion,
    QualityReportItem,
    QualityDimensionResult,
    QualitySimilarityPair,
    QualityMediaAlignment,
    QualityReportArtifact,
)

assert {
    QualityReportVersion.__tablename__,
    QualityReportItem.__tablename__,
    QualityDimensionResult.__tablename__,
    QualitySimilarityPair.__tablename__,
    QualityMediaAlignment.__tablename__,
    QualityReportArtifact.__tablename__,
} == set(IMMUTABLE_TABLES)

__all__ = [
    "IMMUTABLE_TABLES",
    "OWN_TABLES",
    "QualityDimensionResult",
    "QualityMediaAlignment",
    "QualityReport",
    "QualityReportArtifact",
    "QualityReportItem",
    "QualityReportVersion",
    "QualitySimilarityPair",
]
