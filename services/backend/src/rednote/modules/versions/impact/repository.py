"""影响图与失效事件的仓储：版本边 / 节点（读 `versions.models`）与本包事件表。

跨模块只读取数在 `export_items_bridge.py` / `currentness_bridge.py`（裸 SQL）。

## 写入面只有一个

`append_event`：INSERT 一行事件（SAVEPOINT 内，撞 `UQ(export_item_id, seq)` 时只回滚这一次尝试，
外层事务仍可用——PG 里任何失败语句都会让整个事务 aborted，ISS-22）。没有 UPDATE / DELETE。

## 当前态取数

`latest_events` 用 `DISTINCT ON (export_item_id) ... ORDER BY export_item_id, seq DESC`：
这是对**本表自己的事件序号**取最大（触发器钉了从 1 连续、交替），不是对版本链取末版。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rednote.modules.versions.impact.models import ExportValidityEvent
from rednote.modules.versions.impact.types import (
    FRONTIER_CHUNK,
    STATE_INVALID,
    STATE_VALID,
    TRANSITION_INVALIDATED,
    Edge,
)
from rednote.modules.versions.models import VersionNode

CONSTRAINT_SEQ: str = "uq_export_validity_events_export_item_id_seq"

_EDGES_SQL = text(
    "SELECT vd.parent_version_id AS parent, vd.dependency_version_id AS dependency,"
    "       vd.purpose AS purpose"
    "  FROM version_dependencies vd"
    "  JOIN version_nodes pn ON pn.id = vd.parent_version_id"
    " WHERE vd.dependency_version_id = ANY(:ids) AND pn.project_id = :pid"
    " ORDER BY 1, 2, 3"
)


class EventSeqConflict(Exception):
    """追加事件撞 `UQ(export_item_id, seq)`（已在 SAVEPOINT 内回滚，事务仍可用）。"""


def _constraint_name(exc: IntegrityError) -> str:
    diag = getattr(exc.orig, "diag", None)
    return str(getattr(diag, "constraint_name", None) or "")


class ImpactRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # ------------------------------------------------------------------
    # 版本图（只读）
    # ------------------------------------------------------------------

    def get_node(self, node_id: uuid.UUID) -> VersionNode | None:
        return self._session.get(VersionNode, node_id)

    def make_fetch_dependents(self, project_id: uuid.UUID):
        """返回 `fetch_dependents(frontier)`：一层一次批量反查（走 `ix_version_dependencies_dependency`），
        frontier 过大时分块；只取下游属于本项目的边（平台级 / 他项目节点不纳入）。"""

        def fetch(frontier: Sequence[uuid.UUID]) -> list[Edge]:
            out: list[Edge] = []
            ids = list(frontier)
            for i in range(0, len(ids), FRONTIER_CHUNK):
                rows = self._session.execute(
                    _EDGES_SQL, {"ids": ids[i : i + FRONTIER_CHUNK], "pid": project_id}
                ).all()
                out.extend(Edge(r[0], r[1], r[2]) for r in rows)
            return out

        return fetch

    def node_metas(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, VersionNode]:
        if not ids:
            return {}
        nodes = self._session.execute(
            select(VersionNode).where(VersionNode.id.in_(list(ids)))
        ).scalars()
        return {n.id: n for n in nodes}

    # ------------------------------------------------------------------
    # 事件表
    # ------------------------------------------------------------------

    def latest_events(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, tuple[int, str]]:
        """导出行 → `(最大 seq, transition)`；无事件的行不在结果里（= VALID）。"""
        if not item_ids:
            return {}
        rows = self._session.execute(
            text(
                "SELECT DISTINCT ON (export_item_id) export_item_id, seq, transition"
                "  FROM export_validity_events WHERE export_item_id = ANY(:ids)"
                " ORDER BY export_item_id, seq DESC"
            ),
            {"ids": list(item_ids)},
        ).all()
        return {r[0]: (r[1], r[2]) for r in rows}

    def append_event(
        self,
        *,
        export_item_id: uuid.UUID,
        seq: int,
        transition: str,
        reason: str | None,
        observed_version_id: uuid.UUID,
        observed_eligibility_hash: str,
        scope_kind: str,
        source_version_id: uuid.UUID | None,
        created_by: uuid.UUID,
    ) -> ExportValidityEvent:
        event = ExportValidityEvent(
            export_item_id=export_item_id,
            seq=seq,
            transition=transition,
            reason=reason,
            observed_version_id=observed_version_id,
            observed_eligibility_hash=observed_eligibility_hash,
            scope_kind=scope_kind,
            source_version_id=source_version_id,
            created_by=created_by,
        )
        try:
            with self._session.begin_nested():
                self._session.add(event)
                self._session.flush()
        except IntegrityError as exc:
            if _constraint_name(exc) == CONSTRAINT_SEQ:
                raise EventSeqConflict() from exc
            raise
        return event

    def list_events(
        self,
        *,
        project_id: uuid.UUID,
        export_group_id: uuid.UUID | None = None,
        export_item_id: uuid.UUID | None = None,
    ) -> list[Any]:
        """某项目的事件（经 `export_items` 取项目归属）。分页由调用方在内存里做，故取全量。"""
        where = ["x.project_id = :p"]
        params: dict[str, Any] = {"p": project_id}
        if export_group_id is not None:
            where.append("x.export_group_id = :g")
            params["g"] = export_group_id
        if export_item_id is not None:
            where.append("x.id = :i")
            params["i"] = export_item_id
        sql = (
            "SELECT e.id, e.export_item_id, x.export_group_id, x.content_item_id, e.seq,"
            "       e.transition, e.reason, e.observed_version_id, e.observed_eligibility_hash,"
            "       e.scope_kind, e.source_version_id, e.created_by, e.created_at"
            "  FROM export_validity_events e JOIN export_items x ON x.id = e.export_item_id"
            " WHERE " + " AND ".join(where) + " ORDER BY e.created_at DESC, e.export_item_id, e.seq DESC"
        )
        return list(self._session.execute(text(sql), params).mappings().all())


def recorded_state(latest: tuple[int, str] | None) -> str:
    """最大 seq 的 transition → 记录态；无事件 = VALID。"""
    if latest is None:
        return STATE_VALID
    return STATE_INVALID if latest[1] == TRANSITION_INVALIDATED else STATE_VALID


__all__ = ["CONSTRAINT_SEQ", "EventSeqConflict", "ImpactRepository", "recorded_state"]
