"""`quality_*` 7 张表的仓储 + 对他包表的**只读裸 SQL**（列名即对方契约，写错当场 `UndefinedColumn`）。

只读依赖（均不建 FK）：

* `content_items`（`id, project_id, created_at`）——成员归属；
* `media_items`（`id, content_item_id, sort_order, paragraph_ref, state`）——顺序对位（接缝冻结 §5.4：不碰 media 源文件）；
* `batches`（`id, project_id`）与 `batch_items`（`batch_id, content_item_id, ordinal`）——D2：`scope_kind='BATCH'` 的成员。

`quality_reports` 的写入面只有两个：INSERT（`ON CONFLICT DO NOTHING`）与 `advance_projection`
（带 `lock_version` 的 CAS）。其余 6 张表只 INSERT。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from rednote.modules.quality.models import (
    QualityDimensionResult,
    QualityMediaAlignment,
    QualityReport,
    QualityReportArtifact,
    QualityReportItem,
    QualityReportVersion,
    QualitySimilarityPair,
)
from rednote.modules.quality.types import MediaSlot


class QualityRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def flush(self) -> None:
        self._session.flush()

    # ------------------------------------------------------------------
    # 系列
    # ------------------------------------------------------------------

    def ensure_series(
        self,
        *,
        project_id: uuid.UUID,
        scope_kind: str,
        scope_digest: str,
        batch_id: uuid.UUID | None,
        created_by: uuid.UUID | None,
    ) -> None:
        """`INSERT ... ON CONFLICT (project_id, scope_digest) DO NOTHING`。"""
        self._session.execute(
            pg_insert(QualityReport)
            .values(
                project_id=project_id,
                scope_kind=scope_kind,
                scope_digest=scope_digest,
                batch_id=batch_id,
                created_by=created_by,
            )
            .on_conflict_do_nothing(constraint="uq_quality_reports_project_id_scope_digest")
        )

    def lock_series(self, *, project_id: uuid.UUID, scope_digest: str) -> QualityReport:
        """`SELECT ... FOR UPDATE`：同范围的并发生成在此串行；不同范围互不阻塞。"""
        return self._session.execute(
            select(QualityReport)
            .where(
                QualityReport.project_id == project_id,
                QualityReport.scope_digest == scope_digest,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()

    def lock_series_by_id(self, report_id: uuid.UUID) -> QualityReport | None:
        return self._session.execute(
            select(QualityReport)
            .where(QualityReport.id == report_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def get_series(self, report_id: uuid.UUID) -> QualityReport | None:
        return self._session.get(QualityReport, report_id, populate_existing=True)

    def advance_projection(
        self,
        *,
        report_id: uuid.UUID,
        expected_lock_version: int,
        version_id: uuid.UUID,
        revision_no: int,
    ) -> int:
        """带 `lock_version` 的 CAS；返回受影响行数（持有 FOR UPDATE 时恒为 1，0 视为实现缺陷）。"""
        result = self._session.execute(
            update(QualityReport)
            .where(
                QualityReport.id == report_id,
                QualityReport.lock_version == expected_lock_version,
            )
            .values(
                current_version_id=version_id,
                current_revision_no=revision_no,
                lock_version=expected_lock_version + 1,
                updated_at=text("now()"),
            )
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    def list_series(
        self,
        *,
        project_id: uuid.UUID,
        before: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[QualityReport]:
        """项目下的系列，新的在前（keyset：`updated_at DESC, id DESC`）。"""
        stmt = select(QualityReport).where(QualityReport.project_id == project_id)
        if before is not None:
            stmt = stmt.where(
                tuple_(QualityReport.updated_at, QualityReport.id) < tuple_(before[0], before[1])
            )
        stmt = stmt.order_by(QualityReport.updated_at.desc(), QualityReport.id.desc()).limit(limit)
        return list(self._session.execute(stmt.execution_options(populate_existing=True)).scalars())

    # ------------------------------------------------------------------
    # 版本与子表（写）
    # ------------------------------------------------------------------

    def insert_version(self, **values: Any) -> QualityReportVersion:
        row = QualityReportVersion(**values)
        self._session.add(row)
        self._session.flush()
        return row

    def insert_rows(self, rows: Iterable[Any]) -> None:
        """按调用顺序一次 flush（调用方按 FK 依赖分组调用：items → dimensions → pairs / alignments）。"""
        self._session.add_all(list(rows))
        self._session.flush()

    # ------------------------------------------------------------------
    # 版本与子表（读）
    # ------------------------------------------------------------------

    def get_version(self, version_id: uuid.UUID) -> QualityReportVersion | None:
        return self._session.get(QualityReportVersion, version_id, populate_existing=True)

    def versions_by_ids(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, QualityReportVersion]:
        if not ids:
            return {}
        rows = self._session.execute(
            select(QualityReportVersion).where(QualityReportVersion.id.in_(list(ids)))
        ).scalars()
        return {row.id: row for row in rows}

    def list_items(self, version_id: uuid.UUID) -> list[QualityReportItem]:
        return list(
            self._session.execute(
                select(QualityReportItem)
                .where(QualityReportItem.report_version_id == version_id)
                .order_by(QualityReportItem.position, QualityReportItem.id)
            ).scalars()
        )

    def list_dimensions(self, version_id: uuid.UUID) -> list[QualityDimensionResult]:
        return list(
            self._session.execute(
                select(QualityDimensionResult).where(
                    QualityDimensionResult.report_version_id == version_id
                )
            ).scalars()
        )

    def list_pairs(
        self,
        *,
        version_id: uuid.UUID,
        dimension: str | None,
        before: tuple[int, uuid.UUID] | None,
        limit: int,
    ) -> list[QualitySimilarityPair]:
        """相似对，相似度高的在前（keyset：`similarity_permille DESC, id DESC`；`id` 为第二排序键）。"""
        stmt = select(QualitySimilarityPair).where(
            QualitySimilarityPair.report_version_id == version_id
        )
        if dimension is not None:
            stmt = stmt.where(QualitySimilarityPair.dimension == dimension)
        if before is not None:
            stmt = stmt.where(
                tuple_(QualitySimilarityPair.similarity_permille, QualitySimilarityPair.id)
                < tuple_(before[0], before[1])
            )
        stmt = stmt.order_by(
            QualitySimilarityPair.similarity_permille.desc(), QualitySimilarityPair.id.desc()
        ).limit(limit)
        return list(self._session.execute(stmt).scalars())

    def all_pairs(self, version_id: uuid.UUID) -> list[QualitySimilarityPair]:
        return list(
            self._session.execute(
                select(QualitySimilarityPair)
                .where(QualitySimilarityPair.report_version_id == version_id)
                .order_by(
                    QualitySimilarityPair.dimension,
                    QualitySimilarityPair.content_item_a_id,
                    QualitySimilarityPair.content_item_b_id,
                )
            ).scalars()
        )

    def list_alignments(self, version_id: uuid.UUID) -> list[QualityMediaAlignment]:
        return list(
            self._session.execute(
                select(QualityMediaAlignment)
                .where(QualityMediaAlignment.report_version_id == version_id)
                .order_by(QualityMediaAlignment.content_item_id, QualityMediaAlignment.sort_order)
            ).scalars()
        )

    def get_artifact(self, *, version_id: uuid.UUID, fmt: str) -> QualityReportArtifact | None:
        return self._session.execute(
            select(QualityReportArtifact).where(
                QualityReportArtifact.report_version_id == version_id,
                QualityReportArtifact.format == fmt,
            )
        ).scalar_one_or_none()

    # ------------------------------------------------------------------
    # 对他包表的只读裸 SQL
    # ------------------------------------------------------------------

    def content_projects(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
        if not ids:
            return {}
        rows = self._session.execute(
            text("SELECT id, project_id FROM content_items WHERE id = ANY(:ids)"),
            {"ids": list(ids)},
        ).all()
        return {row[0]: row[1] for row in rows}

    def project_content_ids(self, project_id: uuid.UUID) -> list[uuid.UUID]:
        rows = self._session.execute(
            text("SELECT id FROM content_items WHERE project_id = :p ORDER BY created_at, id"),
            {"p": project_id},
        ).all()
        return [row[0] for row in rows]

    def batch_project(self, batch_id: uuid.UUID) -> uuid.UUID | None:
        """WP32 `batches.project_id`（只读 SQL，D2）；批次不存在 ⇒ `None`。"""
        return self._session.execute(
            text("SELECT project_id FROM batches WHERE id = :b"), {"b": batch_id}
        ).scalar_one_or_none()

    def batch_member_ids(self, batch_id: uuid.UUID) -> list[uuid.UUID]:
        """WP32 `batch_items` 成员，按 `ordinal`（D2）。"""
        rows = self._session.execute(
            text("SELECT content_item_id FROM batch_items WHERE batch_id = :b ORDER BY ordinal"),
            {"b": batch_id},
        ).all()
        return [row[0] for row in rows]

    def media_slots(self, content_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[MediaSlot]]:
        """`media_items` 的 `(sort_order, paragraph_ref, state)`；没有图的内容键为空列表。"""
        result: dict[uuid.UUID, list[MediaSlot]] = {cid: [] for cid in content_ids}
        if not content_ids:
            return result
        rows = self._session.execute(
            text(
                "SELECT id, content_item_id, sort_order, paragraph_ref, state"
                "  FROM media_items WHERE content_item_id = ANY(:ids)"
                " ORDER BY content_item_id, sort_order, id"
            ),
            {"ids": list(content_ids)},
        ).all()
        for media_id, content_id, sort_order, ref, state in rows:
            result[content_id].append(MediaSlot(media_id, int(sort_order), ref, state))
        return result

    def version_project(self, version_id: uuid.UUID) -> uuid.UUID | None:
        """报告版本 → 所属项目（经系列）。"""
        return self._session.execute(
            text(
                "SELECT qr.project_id FROM quality_report_versions v"
                "  JOIN quality_reports qr ON qr.id = v.report_id WHERE v.id = :v"
            ),
            {"v": version_id},
        ).scalar_one_or_none()


__all__ = ["QualityRepository"]
