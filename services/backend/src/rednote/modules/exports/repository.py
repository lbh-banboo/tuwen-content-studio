"""`export_items` 的仓储与跨模块只读取数（WP35）。

跨模块取数走**裸 SQL**（平级模块不可互 import），列名即对方契约：
写错列名当场 `UndefinedColumn`，不会静默。

## 取数口径（R2 / R3，本文件最要紧的两处）

**R2 当前正文版本** = `content_items.current_version_id` 投影；投影为 NULL 时回落
`ORDER BY version_no DESC LIMIT 1`。与 `media/repository.py` 同口径，**禁止**直接
`ORDER BY version_no DESC`：回滚后投影指向旧版，而末版是回滚前的那一版。

**R3 正式图** = 每 `(media_version_id, 'DELIVERY')` 取 `MAX(attempt_no)` 那一行，
**再**要求它 `state = 'READY'`。顺序不能换：先过滤 READY 再取最大，会让
"最新一次加工失败"的版本回落到旧的成功产物，导出一张已被判定作废的图。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from rednote.modules.exports.models import ExportItem
from rednote.modules.exports.types import (
    RENDITION_KIND_DELIVERY,
    RENDITION_STATE_READY,
    DeliveryAsset,
)


class ExportRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # ------------------------------------------------------------------
    # 跨模块只读（裸 SQL）
    # ------------------------------------------------------------------

    def project_exists(self, project_id: uuid.UUID) -> bool:
        row = self._session.execute(
            text("SELECT 1 FROM projects WHERE id = :pid"), {"pid": project_id}
        ).first()
        return row is not None

    def content_projects(
        self, content_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID]:
        """内容 id → 所属项目 id。不存在的 id 不在结果里。"""
        if not content_ids:
            return {}
        rows = self._session.execute(
            text("SELECT id, project_id FROM content_items WHERE id = ANY(:ids)"),
            {"ids": list(content_ids)},
        ).all()
        return {r[0]: r[1] for r in rows}

    def current_content_version(
        self, content_item_id: uuid.UUID
    ) -> tuple[uuid.UUID, int, str, str] | None:
        """`(version_id, version_no, title, body)`；一版都没有时 `None`。

        投影优先，投影为 NULL 时回落末版（见模块 docstring R2）。
        """
        row = self._session.execute(
            text(
                "SELECT cv.id, cv.version_no, cv.title, cv.body "
                "FROM content_items ci "
                "JOIN content_versions cv ON cv.id = ci.current_version_id "
                "WHERE ci.id = :cid"
            ),
            {"cid": content_item_id},
        ).first()
        if row is None:
            row = self._session.execute(
                text(
                    "SELECT id, version_no, title, body FROM content_versions "
                    "WHERE content_item_id = :cid ORDER BY version_no DESC LIMIT 1"
                ),
                {"cid": content_item_id},
            ).first()
        if row is None:
            return None
        return row[0], int(row[1]), row[2], row[3]

    def delivery_assets(self, content_item_id: uuid.UUID) -> list[DeliveryAsset]:
        """一条内容当前的正式图，按 `sort_order` 升序。

        每个媒体位取其 `current_version_id`（投影）；再取该版本 DELIVERY 档
        `MAX(attempt_no)` 的那一行，**最后**才判 READY（R3）。
        最新一次加工不是 READY 的媒体位**不出现在结果里**——调用方据此判"缺图"。
        """
        rows = self._session.execute(
            text(
                "SELECT mi.id, mv.id, mi.sort_order, r.id, r.attempt_no, "
                "       r.file_object_id, r.byte_size, r.image_format "
                "FROM media_items mi "
                "JOIN media_versions mv ON mv.id = mi.current_version_id "
                "JOIN media_renditions r ON r.media_version_id = mv.id "
                "  AND r.kind = :kind "
                "  AND r.attempt_no = ( "
                "      SELECT MAX(r2.attempt_no) FROM media_renditions r2 "
                "      WHERE r2.media_version_id = mv.id AND r2.kind = :kind) "
                "WHERE mi.content_item_id = :cid AND r.state = :ready "
                "ORDER BY mi.sort_order, mi.id"
            ),
            {
                "cid": content_item_id,
                "kind": RENDITION_KIND_DELIVERY,
                "ready": RENDITION_STATE_READY,
            },
        ).all()
        return [
            DeliveryAsset(
                media_item_id=r[0],
                media_version_id=r[1],
                sort_order=int(r[2]),
                rendition_id=r[3],
                attempt_no=int(r[4]),
                file_object_id=r[5],
                byte_size=int(r[6]),
                image_format=r[7],
            )
            for r in rows
        ]

    def media_slot_count(self, content_item_id: uuid.UUID) -> int:
        """该内容有几个媒体位（用来判"缺图"：位数 > 正式图数）。"""
        row = self._session.execute(
            text("SELECT count(*) FROM media_items WHERE content_item_id = :cid"),
            {"cid": content_item_id},
        ).one()
        return int(row[0])

    # ------------------------------------------------------------------
    # export_items（本模块表）
    # ------------------------------------------------------------------

    def insert_export_item(self, **columns: Any) -> ExportItem:
        row = ExportItem(**columns)
        self._session.add(row)
        return row

    def flush(self) -> None:
        self._session.flush()

    def get_export_item(self, export_item_id: uuid.UUID) -> ExportItem | None:
        return self._session.get(ExportItem, export_item_id)

    def list_group(self, export_group_id: uuid.UUID) -> list[ExportItem]:
        stmt = (
            select(ExportItem)
            .where(ExportItem.export_group_id == export_group_id)
            .order_by(ExportItem.created_at, ExportItem.id)
        )
        return list(self._session.execute(stmt).scalars().all())

    def find_by_idempotency(
        self, *, project_id: uuid.UUID, idempotency_key: str
    ) -> list[ExportItem]:
        """同项目同键命中的行（一整组）。"""
        stmt = (
            select(ExportItem)
            .where(
                ExportItem.project_id == project_id,
                ExportItem.idempotency_key == idempotency_key,
            )
            .order_by(ExportItem.created_at, ExportItem.id)
        )
        return list(self._session.execute(stmt).scalars().all())

    def list_groups_for_project(
        self, project_id: uuid.UUID, *, limit: int
    ) -> list[ExportItem]:
        """项目下的导出行（全量，调用方按组聚合分页）。"""
        stmt = (
            select(ExportItem)
            .where(ExportItem.project_id == project_id)
            .order_by(ExportItem.created_at.desc(), ExportItem.id.desc())
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars().all())

    def file_blob_key(self, file_object_id: uuid.UUID) -> tuple[str, str] | None:
        """文件对象 → `(storage_backend, object_key)`（读字节用）。"""
        row = self._session.execute(
            text(
                "SELECT b.storage_backend, b.object_key FROM file_objects f "
                "JOIN blobs b ON b.id = f.blob_id WHERE f.id = :fid"
            ),
            {"fid": file_object_id},
        ).first()
        return None if row is None else (row[0], row[1])


__all__ = ["ExportRepository"]
