"""WP34 服务层：回退 / 版本影响预览、导出有效性评估、失效扫描（追加事件）。

## 边界

* **不执行回退**（不移动 `content_items.current_version_id`）；**不自动触发**扫描；不改 `export_items`。
* 预览与评估是纯读；只有 `sweep` 写，且只往 `export_validity_events` 追加。
* 判定走 `export_bridge.observe`（S2 同快照二元组）；同一内容的多条导出行**共用一次观测**。

## 超限行为（OQ-34-08）

预览类：200 + `traversal.complete=false`；写类（`FROM_VERSION` 扫描）：遍历被截断 ⇒ 409
`IMPACT_TRUNCATED`，**一行事件都不写**（欠标记比多标记危险；部分扫描会制造「其余没问题」的假象）。
所有校验与遍历都在第一次写之前完成，所以任何 4xx / 409 都不留半截。
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

from rednote.modules.versions.impact import currentness_bridge, export_bridge, export_items_bridge
from rednote.modules.versions.impact.repository import (
    EventSeqConflict,
    ImpactRepository,
    recorded_state,
)
from rednote.modules.versions.impact.traversal import traverse_dependents
from rednote.modules.versions.impact.types import (
    ACTION_INVALIDATED,
    ACTION_RESTORED,
    ACTION_UNCHANGED,
    ACTION_UNKNOWN,
    ENTITY_TYPE_CONTENT,
    EXPORT_IMPACT_DEPARTING,
    EXPORT_IMPACT_DOWNSTREAM,
    EXPORT_IMPACT_TARGET,
    IMPACT_HISTORICAL,
    IMPACT_REVIEW_REQUIRED,
    IMPACT_UNRESOLVED,
    MAX_APPEND_ATTEMPTS,
    MAX_SWEEP_CONTENT_IDS,
    MAX_SWEEP_ITEMS,
    MAX_TRAVERSAL_DEPTH,
    MAX_TRAVERSAL_NODES,
    REASON_CONTENT_IDS_DUPLICATE,
    REASON_CONTENT_IDS_EMPTY,
    REASON_CONTENT_IDS_TOO_MANY,
    REASON_CONTENT_NOT_FOUND,
    REASON_ENTITY_TYPE_NOT_SUPPORTED,
    REASON_EXPORT_NOT_FOUND,
    REASON_IMPACT_TRUNCATED,
    REASON_NO_CURRENT_VERSION,
    REASON_SCOPE_INVALID,
    REASON_SWEEP_SCOPE_TOO_LARGE,
    REASON_TARGET_IS_CURRENT,
    REASON_TARGET_NOT_OF_ENTITY,
    REASON_VERSION_NOT_FOUND,
    SCOPE_CONTENTS,
    SCOPE_FROM_VERSION,
    STATE_INVALID,
    STATE_UNKNOWN,
    STATE_VALID,
    TRANSITION_INVALIDATED,
    TRANSITION_RESTORED,
    Derived,
    ExportRow,
    SweepRow,
    TraversalResult,
    derive,
)
from rednote.platform.errors import ApiError, ErrorCode


def _error(code: ErrorCode, reason: str, message: str, **details: Any) -> ApiError:
    return ApiError(code, message, details={"reason": reason, **details})


def _not_found(reason: str, message: str, **details: Any) -> ApiError:
    return _error(ErrorCode.RESOURCE_NOT_FOUND, reason, message, **details)


def _invalid(reason: str, message: str, field: str | None = None, **details: Any) -> ApiError:
    if field is not None:
        details["fieldErrors"] = [{"field": field, "type": reason, "message": message}]
    return _error(ErrorCode.VALIDATION_FAILED, reason, message, **details)


def _conflict(reason: str, message: str, **details: Any) -> ApiError:
    return _error(ErrorCode.INVALID_TRANSITION, reason, message, **details)


# ---------------------------------------------------------------------------
# 版本节点校验
# ---------------------------------------------------------------------------


def require_project_version(repo: ImpactRepository, project_id: uuid.UUID, version_id: uuid.UUID) -> Any:
    """版本必须存在且属本项目。不存在 / 他项目 / 平台级（`project_id IS NULL`）三者**同形** 404。"""
    node = repo.get_node(version_id)
    if node is None or node.project_id != project_id:
        raise _not_found(REASON_VERSION_NOT_FOUND, "版本不存在。", versionId=str(version_id))
    return node


# ---------------------------------------------------------------------------
# 预览（①②）
# ---------------------------------------------------------------------------


def _traverse(repo: ImpactRepository, project_id: uuid.UUID, start: uuid.UUID) -> TraversalResult:
    return traverse_dependents(
        repo.make_fetch_dependents(project_id),
        start,
        max_depth=MAX_TRAVERSAL_DEPTH,
        max_nodes=MAX_TRAVERSAL_NODES,
    )


def _node_views(
    repo: ImpactRepository, result: TraversalResult
) -> tuple[list[dict[str, Any]], dict[uuid.UUID, Any]]:
    metas = repo.node_metas([n.version_id for n in result.nodes])
    current = currentness_bridge.resolve_is_current(
        repo.session, [(n.version_id, metas[n.version_id].entity_type) for n in result.nodes]
    )
    views: list[dict[str, Any]] = []
    for n in result.nodes:
        meta = metas[n.version_id]
        is_current = current[n.version_id]
        views.append(
            {
                "versionId": str(n.version_id),
                "entityType": meta.entity_type,
                "entityId": str(meta.entity_id),
                "revisionNo": meta.revision_no,
                "depth": n.depth,
                "purposes": list(n.purposes),
                "isCurrent": is_current,
                "impactClass": IMPACT_UNRESOLVED
                if is_current is None
                else (IMPACT_REVIEW_REQUIRED if is_current else IMPACT_HISTORICAL),
            }
        )
    return views, metas


def _traversal_view(result: TraversalResult) -> dict[str, Any]:
    return {
        "complete": result.complete,
        "truncation": result.truncation,
        "maxDepthReached": result.max_depth_reached,
        "nodeCount": len(result.nodes),
        "nodeCountIsLowerBound": not result.complete,
        "anomalies": list(result.anomalies),
        "cycleNodeCount": result.cycle_node_count,
    }


def _exports_view(
    repo: ImpactRepository,
    project_id: uuid.UUID,
    *,
    departing: uuid.UUID,
    target: uuid.UUID | None,
    downstream_content_nodes: set[uuid.UUID],
    traversal_complete: bool,
) -> dict[str, Any]:
    ids = {departing} | downstream_content_nodes | ({target} if target else set())
    rows = export_items_bridge.rows_by_versions(
        repo.session, project_id, sorted(ids), limit=MAX_SWEEP_ITEMS + 1
    )
    exceeded = len(rows) > MAX_SWEEP_ITEMS
    rows = rows[:MAX_SWEEP_ITEMS]
    latest = repo.latest_events([r.id for r in rows])
    items = []
    for r in rows:
        if r.version_node_id == departing:
            impact = EXPORT_IMPACT_DEPARTING
        elif target is not None and r.version_node_id == target:
            impact = EXPORT_IMPACT_TARGET
        else:
            impact = EXPORT_IMPACT_DOWNSTREAM
        items.append(
            {
                "exportItemId": str(r.id),
                "exportGroupId": str(r.export_group_id),
                "contentItemId": str(r.content_item_id),
                "frozenVersionId": str(r.version_node_id),
                "exportImpact": impact,
                "recordedState": recorded_state(latest.get(r.id)),
            }
        )
    complete = traversal_complete and not exceeded
    return {"complete": complete, "countIsLowerBound": not complete, "items": items}


def _preview(
    repo: ImpactRepository,
    project_id: uuid.UUID,
    start_node: Any,
    *,
    target_node: Any | None,
    start_is_current: bool | None,
) -> dict[str, Any]:
    result = _traverse(repo, project_id, start_node.id)
    node_views, metas = _node_views(repo, result)
    content_nodes = {
        n.version_id for n in result.nodes if metas[n.version_id].entity_type == ENTITY_TYPE_CONTENT
    }
    by_type = dict(Counter(v["entityType"] for v in node_views))
    return {
        "start": {
            "versionId": str(start_node.id),
            "entityType": start_node.entity_type,
            "entityId": str(start_node.entity_id),
            "revisionNo": start_node.revision_no,
            "isCurrent": start_is_current,
        },
        "target": None
        if target_node is None
        else {"versionId": str(target_node.id), "revisionNo": target_node.revision_no},
        "nodes": node_views,
        "traversal": _traversal_view(result),
        "byType": by_type,
        "exports": _exports_view(
            repo,
            project_id,
            departing=start_node.id,
            target=None if target_node is None else target_node.id,
            downstream_content_nodes=content_nodes,
            traversal_complete=result.complete,
        ),
    }


def preview_version_impact(
    repo: ImpactRepository, *, project_id: uuid.UUID, version_id: uuid.UUID
) -> dict[str, Any]:
    """② 任一版本的下游影响（起点由调用方给）。"""
    node = require_project_version(repo, project_id, version_id)
    current = currentness_bridge.resolve_is_current(repo.session, [(node.id, node.entity_type)])
    return _preview(repo, project_id, node, target_node=None, start_is_current=current[node.id])


def preview_rollback(
    repo: ImpactRepository,
    *,
    project_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    target_version_id: uuid.UUID,
) -> dict[str, Any]:
    """① 回退预览：D = **服务端**读 `content_items.current_version_id` 投影，不信客户端。"""
    if entity_type != ENTITY_TYPE_CONTENT:
        raise _invalid(
            REASON_ENTITY_TYPE_NOT_SUPPORTED, "v1 回退预览仅支持 CONTENT。", "entityType",
            entityType=entity_type,
        )
    content = currentness_bridge.content_row(repo.session, entity_id)
    if content is None or content[0] != project_id:
        raise _not_found(REASON_CONTENT_NOT_FOUND, "内容不存在。", contentItemId=str(entity_id))
    target = require_project_version(repo, project_id, target_version_id)
    if target.entity_type != ENTITY_TYPE_CONTENT or target.entity_id != entity_id:
        raise _invalid(
            REASON_TARGET_NOT_OF_ENTITY, "目标版本不是该内容的版本。", "targetVersionId",
            targetVersionId=str(target_version_id),
        )
    departing_id = content[1]
    if departing_id is None:
        raise _conflict(
            REASON_NO_CURRENT_VERSION, "该内容当前没有投影版本，无法确定回退的起点。",
            contentItemId=str(entity_id),
        )
    if departing_id == target.id:
        raise _invalid(
            REASON_TARGET_IS_CURRENT, "目标版本就是当前版本，没有可预览的变化。", "targetVersionId",
            targetVersionId=str(target_version_id),
        )
    departing = repo.get_node(departing_id)
    return _preview(repo, project_id, departing, target_node=target, start_is_current=True)


# ---------------------------------------------------------------------------
# 有效性评估（③ 读）与扫描（④ 写）
# ---------------------------------------------------------------------------


def _observe_all(repo: ImpactRepository, rows: list[ExportRow]) -> dict[uuid.UUID, Any]:
    """按内容去重后各观测一次（同一内容的多条导出行共用）。"""
    return {
        cid: export_bridge.observe(repo.session, cid)
        for cid in sorted({r.content_item_id for r in rows})
    }


def evaluate_group(
    repo: ImpactRepository, *, project_id: uuid.UUID, export_group_id: uuid.UUID
) -> dict[str, Any]:
    """③ 读时评估一整个导出组；**不写库**。"""
    rows = export_items_bridge.rows_by_group(repo.session, project_id, export_group_id)
    if not rows:
        raise _not_found(REASON_EXPORT_NOT_FOUND, "导出不存在。", exportGroupId=str(export_group_id))
    rows.sort(key=lambda r: (r.created_at, r.id))
    observed = _observe_all(repo, rows)
    latest = repo.latest_events([r.id for r in rows])
    items = []
    counts: Counter[str] = Counter()
    for r in rows:
        d = derive(r.version_node_id, r.eligibility_hash, observed[r.content_item_id])
        recorded = recorded_state(latest.get(r.id))
        counts[d.state] += 1
        items.append(
            {
                "exportItemId": str(r.id),
                "contentItemId": str(r.content_item_id),
                "frozenVersionId": str(r.version_node_id),
                "derivedState": d.state,
                "derivedReason": d.reason,
                "unknownReason": d.unknown_reason,
                "recordedState": recorded,
                # UNKNOWN 无法与记录态对账：null，不是 true / false。
                "inSync": None if d.state == STATE_UNKNOWN else d.state == recorded,
            }
        )
    return {
        "exportGroupId": str(export_group_id),
        "summary": {
            "valid": counts[STATE_VALID],
            "invalid": counts[STATE_INVALID],
            "unknown": counts[STATE_UNKNOWN],
        },
        "items": items,
    }


def _validate_content_ids(content_ids: list[uuid.UUID]) -> None:
    if not content_ids:
        raise _invalid(REASON_CONTENT_IDS_EMPTY, "contentItemIds 不能为空。", "contentItemIds")
    if len(content_ids) > MAX_SWEEP_CONTENT_IDS:
        raise _invalid(
            REASON_CONTENT_IDS_TOO_MANY, f"一次最多 {MAX_SWEEP_CONTENT_IDS} 条内容。", "contentItemIds"
        )
    if len(set(content_ids)) != len(content_ids):
        raise _invalid(REASON_CONTENT_IDS_DUPLICATE, "contentItemIds 含重复项。", "contentItemIds")


def _append_if_changed(
    repo: ImpactRepository,
    row: ExportRow,
    derived: Derived,
    latest: tuple[int, str] | None,
    *,
    scope_kind: str,
    source_version_id: uuid.UUID | None,
    actor_id: uuid.UUID,
) -> SweepRow:
    """状态转移表（设计稿 §5.5）。撞 `UQ(export_item_id, seq)` ⇒ 重读最大 seq 重算，至多 3 次。"""
    recorded = recorded_state(latest)
    out = SweepRow(
        export_item_id=row.id,
        content_item_id=row.content_item_id,
        recorded_before=recorded,
        derived_state=derived.state,
        derived_reason=derived.reason,
        action=ACTION_UNKNOWN if derived.state == STATE_UNKNOWN else ACTION_UNCHANGED,
        unknown_reason=derived.unknown_reason,
    )
    if derived.state == STATE_UNKNOWN:
        return out
    for _ in range(MAX_APPEND_ATTEMPTS):
        recorded = recorded_state(latest)
        if recorded == derived.state:
            out.action = ACTION_UNCHANGED
            return out
        invalidating = derived.state == STATE_INVALID
        assert derived.observed is not None
        seq = (latest[0] if latest else 0) + 1
        try:
            repo.append_event(
                export_item_id=row.id,
                seq=seq,
                transition=TRANSITION_INVALIDATED if invalidating else TRANSITION_RESTORED,
                reason=derived.reason if invalidating else None,
                observed_version_id=derived.observed.version_id,
                observed_eligibility_hash=derived.observed.eligibility_hash,
                scope_kind=scope_kind,
                source_version_id=source_version_id,
                created_by=actor_id,
            )
        except EventSeqConflict:
            latest = repo.latest_events([row.id]).get(row.id)
            continue
        out.action = ACTION_INVALIDATED if invalidating else ACTION_RESTORED
        out.appended_seq = seq
        return out
    out.notes.append("APPEND_RETRIES_EXHAUSTED")
    return out


def sweep(
    repo: ImpactRepository,
    *,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    content_item_ids: list[uuid.UUID] | None,
    from_version_id: uuid.UUID | None,
) -> dict[str, Any]:
    """④ 扫描并追加事件。范围校验 / 遍历 / 观测全部在第一次写之前完成。"""
    if (content_item_ids is None) == (from_version_id is None):
        raise _invalid(
            REASON_SCOPE_INVALID, "contentItemIds 与 fromVersionId 必须恰给一个。", "contentItemIds"
        )
    session = repo.session
    source_version: uuid.UUID | None = None
    traversal: TraversalResult | None = None
    if content_item_ids is not None:
        _validate_content_ids(content_item_ids)
        owners = currentness_bridge.content_projects(session, content_item_ids)
        for cid in content_item_ids:
            if owners.get(cid) != project_id:
                raise _not_found(REASON_CONTENT_NOT_FOUND, "内容不存在。", contentItemId=str(cid))
        scope_kind = SCOPE_CONTENTS
        content_ids = list(content_item_ids)
    else:
        assert from_version_id is not None
        start = require_project_version(repo, project_id, from_version_id)
        traversal = _traverse(repo, project_id, start.id)
        if not traversal.complete:
            raise _conflict(
                REASON_IMPACT_TRUNCATED,
                "影响图遍历被截断，拒绝写入（部分扫描会制造『其余导出没问题』的假象）。",
                truncation=traversal.truncation,
                fromVersionId=str(from_version_id),
            )
        metas = repo.node_metas([n.version_id for n in traversal.nodes])
        content_ids = [
            metas[n.version_id].entity_id
            for n in traversal.nodes
            if metas[n.version_id].entity_type == ENTITY_TYPE_CONTENT
        ]
        if start.entity_type == ENTITY_TYPE_CONTENT:
            content_ids.append(start.entity_id)
        scope_kind = SCOPE_FROM_VERSION
        source_version = start.id

    rows = export_items_bridge.rows_by_contents(
        session, project_id, sorted(set(content_ids)), limit=MAX_SWEEP_ITEMS + 1
    )
    if len(rows) > MAX_SWEEP_ITEMS:
        raise _invalid(
            REASON_SWEEP_SCOPE_TOO_LARGE, f"命中的导出行超过 {MAX_SWEEP_ITEMS} 条，请分批扫描。",
            "contentItemIds", limit=MAX_SWEEP_ITEMS,
        )
    rows.sort(key=lambda r: r.id)  # 按 id 升序逐行处理，两个并发扫描不会以相反顺序互等
    observed = _observe_all(repo, rows)
    latest = repo.latest_events([r.id for r in rows])

    results: list[SweepRow] = []
    for r in rows:
        derived = derive(r.version_node_id, r.eligibility_hash, observed[r.content_item_id])
        results.append(
            _append_if_changed(
                repo, r, derived, latest.get(r.id),
                scope_kind=scope_kind, source_version_id=source_version, actor_id=actor_id,
            )
        )
    counts = Counter(x.action for x in results)
    return {
        "scope": {
            "kind": scope_kind,
            "contentItemIds": [str(c) for c in sorted(set(content_ids))],
            "fromVersionId": None if source_version is None else str(source_version),
        },
        "summary": {
            "invalidated": counts[ACTION_INVALIDATED],
            "restored": counts[ACTION_RESTORED],
            "unchanged": counts[ACTION_UNCHANGED],
            "unknown": counts[ACTION_UNKNOWN],
        },
        "items": [
            {
                "exportItemId": str(x.export_item_id),
                "contentItemId": str(x.content_item_id),
                "previousState": x.recorded_before,
                "derivedState": x.derived_state,
                "derivedReason": x.derived_reason,
                "unknownReason": x.unknown_reason,
                "action": x.action,
                "seq": x.appended_seq,
            }
            for x in results
        ],
    }


# ---------------------------------------------------------------------------
# 事件历史（⑤）
# ---------------------------------------------------------------------------


def list_events(
    repo: ImpactRepository,
    *,
    project_id: uuid.UUID,
    export_group_id: uuid.UUID | None,
    export_item_id: uuid.UUID | None,
) -> list[dict[str, Any]]:
    if export_group_id is not None and not export_items_bridge.rows_by_group(
        repo.session, project_id, export_group_id
    ):
        raise _not_found(REASON_EXPORT_NOT_FOUND, "导出不存在。", exportGroupId=str(export_group_id))
    if export_item_id is not None and export_items_bridge.get_row(
        repo.session, project_id, export_item_id
    ) is None:
        raise _not_found(REASON_EXPORT_NOT_FOUND, "导出行不存在。", exportItemId=str(export_item_id))
    return [
        {
            "id": str(e["id"]),
            "exportItemId": str(e["export_item_id"]),
            "exportGroupId": str(e["export_group_id"]),
            "contentItemId": str(e["content_item_id"]),
            "seq": e["seq"],
            "transition": e["transition"],
            "reason": e["reason"],
            "observedVersionId": str(e["observed_version_id"]),
            "observedEligibilityHash": e["observed_eligibility_hash"],
            "scopeKind": e["scope_kind"],
            "sourceVersionId": None if e["source_version_id"] is None else str(e["source_version_id"]),
            "createdBy": str(e["created_by"]),
            "createdAt": e["created_at"].isoformat(),
        }
        for e in repo.list_events(
            project_id=project_id, export_group_id=export_group_id, export_item_id=export_item_id
        )
    ]


__all__ = [
    "evaluate_group",
    "list_events",
    "preview_rollback",
    "preview_version_impact",
    "require_project_version",
    "sweep",
]
