"""`impact` → `content_*` / `media_*` / `quality_*` 的只读裸 SQL（WP34 S4，列名即对方契约）。

「当前版本」= 逻辑实体上的**显式投影指针**（如 `content_items.current_version_id`）。
**投影为 NULL 时不回落**（OQ-34-07）：结论会被当作「该不该复核」的依据，猜一个版本比承认未知更危险。

REPORT 解析器**条件注册**（OQ-34-12）：WP33 的 `quality_reports` / `quality_report_versions` 表
可能不存在，先用 `to_regclass` 探测；不存在则 REPORT 类节点恒 UNRESOLVED，**绝不**发会
`UndefinedTable` 的 SQL（那会让整个事务进入 aborted 状态）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text

from rednote.modules.versions.models import (
    ENTITY_TYPE_CONTENT,
    ENTITY_TYPE_MEDIA,
    ENTITY_TYPE_REPORT,
)

#: 解析器 SQL：返回 `(node_id, current_version_id)`；`current_version_id` 为 NULL ⇒ 无法判断。
_RESOLVER_SQL: dict[str, str] = {
    ENTITY_TYPE_CONTENT: (
        "SELECT cv.id AS node_id, ci.current_version_id AS current_id"
        "  FROM content_versions cv JOIN content_items ci ON ci.id = cv.content_item_id"
        " WHERE cv.id = ANY(:ids)"
    ),
    ENTITY_TYPE_MEDIA: (
        "SELECT mv.id AS node_id, mi.current_version_id AS current_id"
        "  FROM media_versions mv JOIN media_items mi ON mi.id = mv.media_item_id"
        " WHERE mv.id = ANY(:ids)"
    ),
    ENTITY_TYPE_REPORT: (
        "SELECT qv.id AS node_id, qr.current_version_id AS current_id"
        "  FROM quality_report_versions qv JOIN quality_reports qr ON qr.id = qv.report_id"
        " WHERE qv.id = ANY(:ids)"
    ),
}
_REPORT_TABLES = ("quality_reports", "quality_report_versions")


def report_tables_exist(session: Any) -> bool:
    """只读探测（不抛错）：WP33 的两张表是否都已存在。"""
    return all(
        session.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name}).scalar()
        for name in _REPORT_TABLES
    )


def resolvable_types(session: Any) -> frozenset[str]:
    types = {ENTITY_TYPE_CONTENT, ENTITY_TYPE_MEDIA}
    if report_tables_exist(session):
        types.add(ENTITY_TYPE_REPORT)
    return frozenset(types)


def resolve_is_current(
    session: Any, nodes: Sequence[tuple[uuid.UUID, str]]
) -> dict[uuid.UUID, bool | None]:
    """节点 → 是否为其实体的当前版本。`True/False/None`（None = 无法判断，**不是 False**）。"""
    result: dict[uuid.UUID, bool | None] = {node_id: None for node_id, _ in nodes}
    by_type: dict[str, list[uuid.UUID]] = {}
    for node_id, entity_type in nodes:
        by_type.setdefault(entity_type, []).append(node_id)
    supported = resolvable_types(session) if by_type else frozenset()
    for entity_type, ids in by_type.items():
        if entity_type not in supported:
            continue
        for row in session.execute(text(_RESOLVER_SQL[entity_type]), {"ids": ids}).mappings():
            current = row["current_id"]
            result[row["node_id"]] = None if current is None else current == row["node_id"]
    return result


def content_row(session: Any, content_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID | None] | None:
    """内容 → `(project_id, current_version_id)`；内容不存在 ⇒ None。"""
    row = session.execute(
        text("SELECT project_id, current_version_id FROM content_items WHERE id = :c"),
        {"c": content_id},
    ).first()
    return None if row is None else (row[0], row[1])


def content_projects(
    session: Any, content_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    if not content_ids:
        return {}
    rows = session.execute(
        text("SELECT id, project_id FROM content_items WHERE id = ANY(:ids)"),
        {"ids": list(content_ids)},
    ).all()
    return {r[0]: r[1] for r in rows}


__all__ = [
    "content_projects",
    "content_row",
    "report_tables_exist",
    "resolvable_types",
    "resolve_is_current",
]
