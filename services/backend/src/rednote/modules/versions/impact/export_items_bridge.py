"""`impact` → `export_items` 的只读裸 SQL（WP34 S3，列名即对方契约）。

只发 SELECT；列：`id, project_id, export_group_id, content_item_id, version_node_id,
eligibility_hash, created_at`。写错列名当场 `UndefinedColumn`，不会静默。
**绝不对 `export_items` 写**（它是不可变表；失效事实写在本包的事件表）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text

from rednote.modules.versions.impact.types import ExportRow

_COLUMNS = (
    "id, project_id, export_group_id, content_item_id, version_node_id,"
    " eligibility_hash, created_at"
)
_ORDER = " ORDER BY created_at, id"


def _rows(session: Any, where: str, params: dict[str, Any], limit: int | None) -> list[ExportRow]:
    sql = f"SELECT {_COLUMNS} FROM export_items WHERE {where}{_ORDER}"
    if limit is not None:
        sql += " LIMIT :lim"
        params = {**params, "lim": limit}
    return [ExportRow(**dict(r)) for r in session.execute(text(sql), params).mappings()]


def rows_by_versions(
    session: Any, project_id: uuid.UUID, version_ids: Sequence[uuid.UUID], *, limit: int | None = None
) -> list[ExportRow]:
    """冻结在这些版本节点上的导出行（走 `ix_export_items_version`）。"""
    if not version_ids:
        return []
    return _rows(
        session, "project_id = :p AND version_node_id = ANY(:ids)",
        {"p": project_id, "ids": list(version_ids)}, limit,
    )


def rows_by_contents(
    session: Any, project_id: uuid.UUID, content_ids: Sequence[uuid.UUID], *, limit: int | None = None
) -> list[ExportRow]:
    if not content_ids:
        return []
    return _rows(
        session, "project_id = :p AND content_item_id = ANY(:ids)",
        {"p": project_id, "ids": list(content_ids)}, limit,
    )


def rows_by_group(session: Any, project_id: uuid.UUID, group_id: uuid.UUID) -> list[ExportRow]:
    return _rows(session, "project_id = :p AND export_group_id = :g", {"p": project_id, "g": group_id}, None)


def get_row(session: Any, project_id: uuid.UUID, item_id: uuid.UUID) -> ExportRow | None:
    found = _rows(session, "project_id = :p AND id = :i", {"p": project_id, "i": item_id}, None)
    return found[0] if found else None


__all__ = ["get_row", "rows_by_contents", "rows_by_group", "rows_by_versions"]
