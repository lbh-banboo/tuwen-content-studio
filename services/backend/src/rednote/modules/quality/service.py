"""WP33 服务层：生成报告（落 REPORT 版本）/ 读取 / 导出。

## `generate_report` 的固定顺序（设计稿 §9）

```
1  校验范围：成员去重、数量上限、全部属于本项目（BATCH 范围成员取自 batch_items，只读 SQL）
2  身份：INSERT quality_reports ... ON CONFLICT DO NOTHING；再 SELECT ... FOR UPDATE
   ← 同范围的并发生成在此串行；不同范围互不阻塞；只锁这一行，无死锁面
3  逐篇经 content_bridge 钉死当前版本（投影优先）
4  逐篇读 media_items（只读）
5  input_fingerprint = sha256(各篇(内容id, 版本id, 媒体(sort_order, paragraph_ref)序列) + algo + 阈值)
6  若 == 当前版本的指纹 ⇒ 直接返回当前版本（created=False），不写任何行
7  纯函数计算 4 个文本维度 / 3 个恒 NOT_COMPUTED / 顺序对位
8  register_version(revision_no = current_revision_no + 1)；同事务插 versions + 5 张子表
9  UPDATE quality_reports SET current_version_id ... WHERE lock_version = :expected（CAS）
10 E11 依赖边（报告版本 → 各内容版本，purpose=REPORT_INPUT），失败整体回滚
```

* **幂等判据 = 输入指纹**（不是请求幂等键）；只与**当前**版本比较，输入 A→B→A 允许产生第 3 版。
* 任一步抛错 ⇒ 整个事务回滚（含第 2 步新建的系列行）：`QUALITY_SCOPE_EMPTY` 等 422 不落任何行。
* 阈值读 `types.threshold_for(...)` 的**此刻值**并随版本落库快照；改常量不改写历史。
* 本文件不含任何切段：切段 / 顺序对位的期望 ref 全部经 `media_bridge`。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from rednote.modules.quality import content_bridge, files_bridge, media_bridge, versions_bridge
from rednote.modules.quality.dimensions import (
    align_media,
    always_not_computed,
    compute_text_dimension,
)
from rednote.modules.quality.models import (
    QualityDimensionResult,
    QualityMediaAlignment,
    QualityReport,
    QualityReportArtifact,
    QualityReportItem,
    QualityReportVersion,
    QualitySimilarityPair,
)
from rednote.modules.quality.repository import QualityRepository
from rednote.modules.quality.similarity import grams
from rednote.modules.quality.types import (
    ALGO_VERSION,
    ALIGN_ALIGNED,
    ALIGN_MISALIGNED,
    ALIGN_MISSING,
    ALIGN_NOT_EVALUABLE,
    ALWAYS_NOT_COMPUTED,
    ARTIFACT_FORMATS,
    DIMENSION_BODY,
    DIMENSION_ENDING,
    DIMENSION_OPENING,
    DIMENSION_STATUS_COMPUTED,
    DIMENSION_STATUS_NOT_COMPUTED,
    DIMENSION_TITLE,
    DIMENSIONS,
    EXCLUDE_EMPTY_BODY,
    EXCLUDE_NO_VERSION,
    ITEM_EXCLUDED,
    ITEM_INCLUDED,
    MAX_REPORT_ITEMS,
    MIN_COMPARED_ITEMS,
    OUTCOME_ALERTED,
    OUTCOME_NO_ALERT,
    PURPOSE_REPORT_INPUT,
    REASON_ARTIFACT_FORMAT_UNSUPPORTED,
    REASON_BATCH_NOT_IN_PROJECT,
    REASON_CONTENT_NOT_IN_PROJECT,
    REASON_REPORT_NOT_FOUND,
    REASON_REPORT_VERSION_NOT_FOUND,
    REASON_SCOPE_EMPTY,
    REASON_SCOPE_INVALID,
    REASON_SCOPE_TOO_LARGE,
    SCHEMA_VERSION,
    SCOPE_BATCH,
    SCOPE_CONTENT_SET,
    SCOPE_KINDS,
    SCOPE_PROJECT,
    SUBJECT_CURRENT,
    SUBJECT_STALE,
    TEXT_DIMENSIONS,
    THRESHOLD_BASIS_PLACEHOLDER,
    THRESHOLD_NOTE,
    VERDICT_ALERT,
    VERDICT_OK,
    DimensionOutcome,
    threshold_for,
)
from rednote.platform.errors import ApiError, ErrorCode
from rednote.platform.pagination import (
    CursorPosition,
    CursorSpec,
    PageInfo,
    PageParams,
    decode_cursor,
    encode_cursor,
)

# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


def _not_found(reason: str, message: str, **details: Any) -> ApiError:
    return ApiError(
        ErrorCode.RESOURCE_NOT_FOUND, message, details={"reason": reason, **details}
    )


def _invalid(reason: str, message: str, **details: Any) -> ApiError:
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        message,
        details={
            "reason": reason,
            "fieldErrors": [{"field": "scope", "type": reason, "message": message}],
            **details,
        },
    )


def _malformed_cursor() -> ApiError:
    return ApiError(
        ErrorCode.MALFORMED_REQUEST, details={"reason": "cursor_malformed", "field": "cursor"}
    )


# ---------------------------------------------------------------------------
# 范围
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeRequest:
    kind: str
    content_ids: tuple[uuid.UUID, ...] | None = None
    batch_id: uuid.UUID | None = None


def scope_digest(project_id: uuid.UUID, scope: ScopeRequest, member_ids: list[uuid.UUID]) -> str:
    """系列身份摘要：CONTENT_SET 取排序后成员串；PROJECT / BATCH 取固定前缀 + id。"""
    if scope.kind == SCOPE_CONTENT_SET:
        raw = "CONTENT_SET:" + ",".join(str(i) for i in sorted(member_ids))
    elif scope.kind == SCOPE_PROJECT:
        raw = f"PROJECT:{project_id}"
    else:
        raw = f"BATCH:{scope.batch_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve_members(
    repo: QualityRepository, *, project_id: uuid.UUID, scope: ScopeRequest
) -> list[uuid.UUID]:
    """校验范围并返回有序成员；任何失败都是 422（不落行）。"""
    if scope.kind not in SCOPE_KINDS:
        raise _invalid(REASON_SCOPE_INVALID, f"未知的范围：{scope.kind!r}")
    if scope.kind != SCOPE_CONTENT_SET and scope.content_ids is not None:
        raise _invalid(REASON_SCOPE_INVALID, "contentIds 只用于 CONTENT_SET 范围。")
    if scope.kind != SCOPE_BATCH and scope.batch_id is not None:
        raise _invalid(REASON_SCOPE_INVALID, "batchId 只用于 BATCH 范围。")

    if scope.kind == SCOPE_CONTENT_SET:
        if scope.content_ids is None:
            raise _invalid(REASON_SCOPE_INVALID, "CONTENT_SET 范围必须给 contentIds。")
        members = sorted(set(scope.content_ids))
    elif scope.kind == SCOPE_PROJECT:
        members = repo.project_content_ids(project_id)
    else:
        if scope.batch_id is None:
            raise _invalid(REASON_SCOPE_INVALID, "BATCH 范围必须给 batchId。")
        if repo.batch_project(scope.batch_id) != project_id:
            raise _invalid(
                REASON_BATCH_NOT_IN_PROJECT,
                "批次不存在或不属于本项目。",
                batchId=str(scope.batch_id),
            )
        members = repo.batch_member_ids(scope.batch_id)

    if len(members) > MAX_REPORT_ITEMS:
        raise _invalid(
            REASON_SCOPE_TOO_LARGE,
            f"范围内共 {len(members)} 篇，超过单份报告上限 {MAX_REPORT_ITEMS}（不抽样）。",
            limit=MAX_REPORT_ITEMS,
            count=len(members),
        )
    if len(members) < MIN_COMPARED_ITEMS:
        raise _invalid(REASON_SCOPE_EMPTY, "范围内不足 2 篇，无法做跨篇比较。", count=len(members))

    if scope.kind == SCOPE_CONTENT_SET:
        owners = repo.content_projects(members)
        outside = [str(cid) for cid in members if owners.get(cid) != project_id]
        if outside:
            raise _invalid(
                REASON_CONTENT_NOT_IN_PROJECT,
                "contentIds 含不属于本项目（或不存在）的内容。",
                contentIds=outside,
            )
    return members


# ---------------------------------------------------------------------------
# 计算
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Item:
    content_item_id: uuid.UUID
    position: int
    status: str
    exclude_reason: str | None
    content_version_id: uuid.UUID | None
    paragraph_count: int | None
    generation_context_id: uuid.UUID | None
    baseline_id: uuid.UUID | None
    title: str
    body: str
    paragraphs: tuple[tuple[int, str, str], ...]


def _pin_items(session: Any, members: list[uuid.UUID]) -> list[_Item]:
    items: list[_Item] = []
    for position, content_id in enumerate(members):
        subject = content_bridge.read_current_subject(session, content_id=content_id)
        if subject is None:
            items.append(
                _Item(content_id, position, ITEM_EXCLUDED, EXCLUDE_NO_VERSION, None, None,
                      None, None, "", "", ())
            )
            continue
        paragraphs = media_bridge.split_body_into_paragraphs(subject.body)
        included = len(paragraphs) >= 1
        items.append(
            _Item(
                content_item_id=content_id,
                position=position,
                status=ITEM_INCLUDED if included else ITEM_EXCLUDED,
                exclude_reason=None if included else EXCLUDE_EMPTY_BODY,
                content_version_id=subject.content_version_id,
                paragraph_count=len(paragraphs),
                generation_context_id=subject.generation_context_id,
                baseline_id=subject.baseline_id,
                title=subject.title,
                body=subject.body,
                paragraphs=paragraphs,
            )
        )
    return items


def _dimension_text(item: _Item, dimension: str) -> str:
    """该篇该维度的比较文本；单段正文的 ENDING 为空（不与 OPENING 同文双算）。"""
    if dimension == DIMENSION_TITLE:
        return item.title
    if dimension == DIMENSION_OPENING:
        return item.paragraphs[0][1]
    if dimension == DIMENSION_ENDING:
        return item.paragraphs[-1][1] if len(item.paragraphs) >= 2 else ""
    if dimension == DIMENSION_BODY:
        return item.body
    raise ValueError(f"不是文本维度：{dimension}")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def input_fingerprint(
    items: list[_Item], media: dict[uuid.UUID, list[Any]], thresholds: dict[str, list[int]]
) -> str:
    rows = sorted(
        (
            [
                str(it.content_item_id),
                None if it.content_version_id is None else str(it.content_version_id),
                [[slot.sort_order, slot.paragraph_ref] for slot in media.get(it.content_item_id, [])],
            ]
            for it in items
        ),
        key=lambda r: r[0],
    )
    return _sha256(
        {"algo": ALGO_VERSION, "schema": SCHEMA_VERSION, "thresholds": thresholds, "items": rows}
    )


def _dimension_payload(outcome: DimensionOutcome) -> dict[str, Any]:
    return {
        "dimension": outcome.dimension,
        "status": outcome.status,
        "notComputedReason": outcome.not_computed_reason,
        "verdict": outcome.verdict,
        "comparedItemCount": outcome.compared_item_count,
        "flaggedItemCount": outcome.flagged_item_count,
        "pairThresholdPermille": outcome.pair_threshold_permille,
        "alertThresholdPermille": outcome.alert_threshold_permille,
        "maxSimilarityPermille": outcome.max_similarity_permille,
    }


@dataclass(frozen=True)
class GenerateResult:
    created: bool
    series: QualityReport
    version: QualityReportVersion


def generate_report(
    repo: QualityRepository,
    *,
    project_id: uuid.UUID,
    scope: ScopeRequest,
    actor_id: uuid.UUID | None,
) -> GenerateResult:
    session = repo.session
    started_at = datetime.now(timezone.utc)

    members = resolve_members(repo, project_id=project_id, scope=scope)  # 1
    digest = scope_digest(project_id, scope, members)
    repo.ensure_series(  # 2
        project_id=project_id,
        scope_kind=scope.kind,
        scope_digest=digest,
        batch_id=scope.batch_id if scope.kind == SCOPE_BATCH else None,
        created_by=actor_id,
    )
    series = repo.lock_series(project_id=project_id, scope_digest=digest)

    items = _pin_items(session, members)  # 3
    included = [it for it in items if it.status == ITEM_INCLUDED]
    if len(included) < MIN_COMPARED_ITEMS:
        raise _invalid(
            REASON_SCOPE_EMPTY,
            "范围内可比篇数不足 2（无版本或空正文的篇目被排除），无法做跨篇比较。",
            includedCount=len(included),
        )
    media = repo.media_slots([it.content_item_id for it in items])  # 4

    thresholds = {dim: threshold_for(dim) for dim in TEXT_DIMENSIONS}
    fingerprint = input_fingerprint(  # 5
        items,
        media,
        {dim: [t.pair_permille, t.alert_permille] for dim, t in thresholds.items()},
    )
    if series.current_version_id is not None:  # 6
        current = repo.get_version(series.current_version_id)
        if current is not None and current.input_fingerprint == fingerprint:
            return GenerateResult(False, series, current)

    outcomes: list[DimensionOutcome] = []  # 7
    for dimension in DIMENSIONS:
        if dimension in ALWAYS_NOT_COMPUTED:
            outcomes.append(always_not_computed(dimension))
            continue
        grams_by_item = {
            it.content_item_id: grams(_dimension_text(it, dimension)) for it in included
        }
        outcomes.append(compute_text_dimension(dimension, grams_by_item, thresholds[dimension]))

    alignments: list[tuple[uuid.UUID, Any]] = []
    extra_media = 0
    for it in items:
        if it.content_version_id is None:
            continue  # NO_VERSION 篇目无对位行
        expected = (
            media_bridge.expected_refs_for(it.paragraphs) if it.status == ITEM_INCLUDED else None
        )
        rows, extra = align_media(expected, media.get(it.content_item_id, []))
        extra_media += extra
        alignments.extend((it.content_item_id, row) for row in rows)

    ok = sum(1 for o in outcomes if o.verdict == VERDICT_OK)
    alert = sum(1 for o in outcomes if o.verdict == VERDICT_ALERT)
    not_computed = sum(1 for o in outcomes if o.status == DIMENSION_STATUS_NOT_COMPUTED)
    outcome_label = OUTCOME_ALERTED if alert > 0 else OUTCOME_NO_ALERT

    align_counts = {
        verdict: sum(1 for _c, row in alignments if row.verdict == verdict)
        for verdict in (ALIGN_ALIGNED, ALIGN_MISALIGNED, ALIGN_MISSING, ALIGN_NOT_EVALUABLE)
    }
    detail_digest = _sha256(
        {
            "items": [
                [str(it.content_item_id), it.position, it.status, it.exclude_reason,
                 None if it.content_version_id is None else str(it.content_version_id),
                 it.paragraph_count]
                for it in items
            ],
            "pairs": sorted(
                [o.dimension, str(p.content_item_a_id), str(p.content_item_b_id),
                 p.intersection_count, p.union_count]
                for o in outcomes
                for p in o.pairs
            ),
            "alignments": sorted(
                [str(cid), row.sort_order, row.verdict, row.expected_ref, row.actual_ref]
                for cid, row in alignments
            ),
        }
    )
    payload: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "algoVersion": ALGO_VERSION,
        "scope": {
            "kind": scope.kind,
            "batchId": None if scope.batch_id is None else str(scope.batch_id),
            "itemCount": len(items),
        },
        "inputFingerprint": fingerprint,
        "thresholdBasis": THRESHOLD_BASIS_PLACEHOLDER,
        "items": {"included": len(included), "excluded": len(items) - len(included)},
        "dimensions": [_dimension_payload(o) for o in outcomes],
        "alignment": {
            "aligned": align_counts[ALIGN_ALIGNED],
            "misaligned": align_counts[ALIGN_MISALIGNED],
            "missing": align_counts[ALIGN_MISSING],
            "notEvaluable": align_counts[ALIGN_NOT_EVALUABLE],
            "extraMediaCount": extra_media,
        },
        "detailDigest": detail_digest,
    }

    next_revision = series.current_revision_no + 1  # 8
    registered = versions_bridge.register_report_version(
        session,
        report_id=series.id,
        project_id=project_id,
        payload=payload,
        created_by=actor_id,
        revision_no=next_revision,
    )
    version = repo.insert_version(
        id=registered.node_id,
        report_id=series.id,
        revision_no=next_revision,
        input_fingerprint=fingerprint,
        algo_version=ALGO_VERSION,
        schema_version=SCHEMA_VERSION,
        item_count=len(items),
        included_item_count=len(included),
        excluded_item_count=len(items) - len(included),
        ok_dimension_count=ok,
        alert_dimension_count=alert,
        not_computed_dimension_count=not_computed,
        outcome=outcome_label,
        payload=payload,
        payload_hash=registered.payload_hash,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        created_by=actor_id,
    )
    repo.insert_rows(
        QualityReportItem(
            report_version_id=version.id,
            content_item_id=it.content_item_id,
            content_version_id=it.content_version_id,
            position=it.position,
            status=it.status,
            exclude_reason=it.exclude_reason,
            paragraph_count=it.paragraph_count,
            generation_context_id=it.generation_context_id,
            baseline_id=it.baseline_id,
        )
        for it in items
    )
    repo.insert_rows(
        QualityDimensionResult(
            report_version_id=version.id,
            dimension=o.dimension,
            status=o.status,
            not_computed_reason=o.not_computed_reason,
            verdict=o.verdict,
            compared_item_count=o.compared_item_count,
            flagged_item_count=o.flagged_item_count,
            pair_threshold_permille=o.pair_threshold_permille,
            alert_threshold_permille=o.alert_threshold_permille,
            max_similarity_permille=o.max_similarity_permille,
        )
        for o in outcomes
    )
    repo.insert_rows(
        [
            QualitySimilarityPair(
                report_version_id=version.id,
                dimension=o.dimension,
                content_item_a_id=p.content_item_a_id,
                content_item_b_id=p.content_item_b_id,
                intersection_count=p.intersection_count,
                union_count=p.union_count,
                similarity_permille=p.similarity_permille,
            )
            for o in outcomes
            for p in o.pairs
        ]
        + [
            QualityMediaAlignment(
                report_version_id=version.id,
                content_item_id=cid,
                sort_order=row.sort_order,
                media_item_id=row.media_item_id,
                media_state=row.media_state,
                expected_ref=row.expected_ref,
                actual_ref=row.actual_ref,
                verdict=row.verdict,
                reason=row.reason,
            )
            for cid, row in alignments
        ]
    )

    if repo.advance_projection(  # 9
        report_id=series.id,
        expected_lock_version=int(series.lock_version),
        version_id=version.id,
        revision_no=next_revision,
    ) != 1:
        # 持有 FOR UPDATE 时 CAS 必成功；rowcount=0 是实现缺陷，不吞（整体回滚）。
        raise RuntimeError(f"质量报告 {series.id} 的投影推进 CAS 落空（持锁下不应发生）")

    for dependency_version_id in sorted(  # 10：E11
        {it.content_version_id for it in items if it.content_version_id is not None}
    ):
        versions_bridge.link(
            session,
            parent_version_id=version.id,
            dependency_version_id=dependency_version_id,
            purpose=PURPOSE_REPORT_INPUT,
        )

    fresh = repo.get_series(series.id)
    assert fresh is not None
    return GenerateResult(True, fresh, version)


# ---------------------------------------------------------------------------
# 读（渲染）
# ---------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    return value.isoformat()


def _opt(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


def render_version_summary(version: QualityReportVersion) -> dict[str, Any]:
    return {
        "id": str(version.id),
        "reportId": str(version.report_id),
        "revisionNo": version.revision_no,
        "inputFingerprint": version.input_fingerprint,
        "algoVersion": version.algo_version,
        "schemaVersion": version.schema_version,
        "itemCount": version.item_count,
        "includedItemCount": version.included_item_count,
        "excludedItemCount": version.excluded_item_count,
        "okDimensionCount": version.ok_dimension_count,
        "alertDimensionCount": version.alert_dimension_count,
        "notComputedDimensionCount": version.not_computed_dimension_count,
        "outcome": version.outcome,
        # 阈值口径取自**该版本落库的快照**，不读现行常量（改常量不改写历史）。
        "thresholdBasis": str((version.payload or {}).get("thresholdBasis", THRESHOLD_BASIS_PLACEHOLDER)),
        "thresholdNote": THRESHOLD_NOTE,
        "startedAt": _iso(version.started_at),
        "finishedAt": _iso(version.finished_at),
        "createdAt": _iso(version.created_at),
    }


def _render_dimension(row: QualityDimensionResult) -> dict[str, Any]:
    return {
        "dimension": row.dimension,
        "implemented": row.dimension not in ALWAYS_NOT_COMPUTED,
        "status": row.status,
        "notComputedReason": row.not_computed_reason,
        "verdict": row.verdict,
        "comparedItemCount": row.compared_item_count,
        "flaggedItemCount": row.flagged_item_count,
        "pairThresholdPermille": row.pair_threshold_permille,
        "alertThresholdPermille": row.alert_threshold_permille,
        "maxSimilarityPermille": row.max_similarity_permille,
        "approximate": True,
    }


def _render_item(row: QualityReportItem, subject_state: str | None) -> dict[str, Any]:
    return {
        "contentItemId": str(row.content_item_id),
        "contentVersionId": _opt(row.content_version_id),
        "position": row.position,
        "status": row.status,
        "excludeReason": row.exclude_reason,
        "paragraphCount": row.paragraph_count,
        "generationContextId": _opt(row.generation_context_id),
        "baselineId": _opt(row.baseline_id),
        "subjectState": subject_state,
    }


def render_version_detail(
    repo: QualityRepository,
    version: QualityReportVersion,
    *,
    with_subject_state: bool = False,
) -> dict[str, Any]:
    rows = repo.list_items(version.id)
    states: dict[uuid.UUID, str] = {}
    if with_subject_state:
        for row in rows:
            current = content_bridge.current_version_id_of(
                repo.session, content_id=row.content_item_id
            )
            states[row.content_item_id] = (
                SUBJECT_CURRENT if current == row.content_version_id else SUBJECT_STALE
            )
    order = {dimension: index for index, dimension in enumerate(DIMENSIONS)}
    dims = sorted(repo.list_dimensions(version.id), key=lambda d: order[d.dimension])
    counts = {verdict: 0 for verdict in (ALIGN_ALIGNED, ALIGN_MISALIGNED, ALIGN_MISSING, ALIGN_NOT_EVALUABLE)}
    for alignment in repo.list_alignments(version.id):
        counts[alignment.verdict] += 1
    alignment_payload = (version.payload or {}).get("alignment", {})
    body = render_version_summary(version)
    body.update(
        {
            "items": [_render_item(r, states.get(r.content_item_id)) for r in rows],
            "dimensions": [_render_dimension(d) for d in dims],
            "alignmentSummary": {
                "aligned": counts[ALIGN_ALIGNED],
                "misaligned": counts[ALIGN_MISALIGNED],
                "missing": counts[ALIGN_MISSING],
                "notEvaluable": counts[ALIGN_NOT_EVALUABLE],
                "extraMediaCount": alignment_payload.get("extraMediaCount"),
            },
            "staleItemCount": (
                sum(1 for s in states.values() if s == SUBJECT_STALE) if with_subject_state else None
            ),
        }
    )
    return body


def _render_series_base(series: QualityReport) -> dict[str, Any]:
    return {
        "id": str(series.id),
        "projectId": str(series.project_id),
        "scopeKind": series.scope_kind,
        "batchId": _opt(series.batch_id),
        "currentVersionId": _opt(series.current_version_id),
        "currentRevisionNo": series.current_revision_no,
        "createdAt": _iso(series.created_at),
        "updatedAt": _iso(series.updated_at),
    }


def require_series(repo: QualityRepository, report_id: uuid.UUID) -> QualityReport:
    series = repo.get_series(report_id)
    if series is None:
        raise _not_found(REASON_REPORT_NOT_FOUND, "质量报告不存在或无权访问。")
    return series


def require_version(repo: QualityRepository, version_id: uuid.UUID) -> QualityReportVersion:
    version = repo.get_version(version_id)
    if version is None:
        raise _not_found(REASON_REPORT_VERSION_NOT_FOUND, "质量报告版本不存在或无权访问。")
    return version


def get_series_detail(repo: QualityRepository, series: QualityReport) -> dict[str, Any]:
    """系列 + 当前版本全摘要 + 逐篇 `subjectState`（读时判，不写入）。"""
    body = _render_series_base(series)
    version = (
        None if series.current_version_id is None else repo.get_version(series.current_version_id)
    )
    body["currentVersion"] = (
        None if version is None else render_version_detail(repo, version, with_subject_state=True)
    )
    return body


# ---------------------------------------------------------------------------
# 分页
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Page:
    items: tuple[dict[str, Any], ...]
    page: PageInfo


def _decode(params: PageParams, spec: CursorSpec) -> CursorPosition | None:
    return decode_cursor(params.cursor, spec) if params.cursor else None


def list_reports(
    repo: QualityRepository, *, project_id: uuid.UUID, page_params: PageParams
) -> Page:
    """系列列表（新到旧；每系列带当前版本摘要；**不含** `subjectState`）。"""
    spec = CursorSpec(
        sort_fields=("updatedAt", "id"), direction="desc", filters={"projectId": str(project_id)}
    )
    position = _decode(page_params, spec)
    before: tuple[datetime, uuid.UUID] | None = None
    if position is not None:
        try:
            before = (datetime.fromisoformat(str(position.sort_values[0])), uuid.UUID(position.item_id))
        except (ValueError, TypeError, IndexError) as exc:
            raise _malformed_cursor() from exc
    rows = repo.list_series(project_id=project_id, before=before, limit=page_params.limit + 1)
    has_more = len(rows) > page_params.limit
    page_rows = rows[: page_params.limit]
    versions = repo.versions_by_ids([r.current_version_id for r in page_rows if r.current_version_id])
    items = []
    for row in page_rows:
        body = _render_series_base(row)
        version = versions.get(row.current_version_id) if row.current_version_id else None
        body["currentVersion"] = None if version is None else render_version_summary(version)
        items.append(body)
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(
            spec,
            CursorPosition(sort_values=(last.updated_at.isoformat(), str(last.id)), item_id=str(last.id)),
        )
    return Page(tuple(items), PageInfo(next_cursor=next_cursor, has_more=has_more))


def list_pairs(
    repo: QualityRepository,
    *,
    version: QualityReportVersion,
    dimension: str | None,
    page_params: PageParams,
) -> Page:
    spec = CursorSpec(
        sort_fields=("similarityPermille", "id"),
        direction="desc",
        filters={"versionId": str(version.id), "dimension": dimension},
    )
    position = _decode(page_params, spec)
    before: tuple[int, uuid.UUID] | None = None
    if position is not None:
        try:
            before = (int(position.sort_values[0]), uuid.UUID(position.item_id))
        except (ValueError, TypeError, IndexError) as exc:
            raise _malformed_cursor() from exc
    rows = repo.list_pairs(
        version_id=version.id, dimension=dimension, before=before, limit=page_params.limit + 1
    )
    has_more = len(rows) > page_params.limit
    page_rows = rows[: page_params.limit]
    items = tuple(_render_pair(r) for r in page_rows)
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(
            spec,
            CursorPosition(sort_values=(last.similarity_permille, str(last.id)), item_id=str(last.id)),
        )
    return Page(items, PageInfo(next_cursor=next_cursor, has_more=has_more))


def _render_pair(row: QualitySimilarityPair) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "dimension": row.dimension,
        "contentItemAId": str(row.content_item_a_id),
        "contentItemBId": str(row.content_item_b_id),
        "intersectionCount": row.intersection_count,
        "unionCount": row.union_count,
        "similarityPermille": row.similarity_permille,
        "approximate": True,
    }


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


def _render_artifact(row: QualityReportArtifact, *, created: bool) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "reportVersionId": str(row.report_version_id),
        "format": row.format,
        "fileObjectId": str(row.file_object_id),
        "contentSha256": row.content_sha256,
        "byteSize": int(row.byte_size),
        "createdAt": _iso(row.created_at),
        "created": created,
    }


def create_artifact(
    repo: QualityRepository,
    storage: Any,
    *,
    version: QualityReportVersion,
    project_id: uuid.UUID,
    fmt: str,
    actor_id: uuid.UUID,
) -> tuple[dict[str, Any], bool]:
    """导出报告 JSON；`(version, format)` 幂等。并发由系列行 `FOR UPDATE` 串行。"""
    if fmt not in ARTIFACT_FORMATS:
        raise _invalid(
            REASON_ARTIFACT_FORMAT_UNSUPPORTED,
            f"不支持的导出格式：{fmt!r}（目前仅 {', '.join(ARTIFACT_FORMATS)}）。",
        )
    repo.lock_series_by_id(version.report_id)
    existing = repo.get_artifact(version_id=version.id, fmt=fmt)
    if existing is not None:
        return _render_artifact(existing, created=False), False

    document = render_version_detail(repo, version)
    document["pairs"] = [_render_pair(p) for p in repo.all_pairs(version.id)]
    document["alignments"] = [
        {
            "contentItemId": str(a.content_item_id),
            "sortOrder": a.sort_order,
            "verdict": a.verdict,
            "expectedRef": a.expected_ref,
            "actualRef": a.actual_ref,
            "mediaItemId": _opt(a.media_item_id),
            "mediaState": a.media_state,
            "reason": a.reason,
        }
        for a in repo.list_alignments(version.id)
    ]
    content = _canonical(document).encode("utf-8")
    stored = files_bridge.store_report_bytes(
        repo.session,
        storage,
        project_id=project_id,
        original_name=f"quality-report-r{version.revision_no}.json",
        content=content,
        mime_type="application/json",
        uploaded_by=actor_id,
    )
    row = QualityReportArtifact(
        report_version_id=version.id,
        format=fmt,
        file_object_id=stored.file_id,
        content_sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        created_by=actor_id,
    )
    repo.insert_rows([row])
    repo.session.refresh(row)
    return _render_artifact(row, created=True), True


# ---------------------------------------------------------------------------
# 维度目录
# ---------------------------------------------------------------------------


def dimension_catalog() -> list[dict[str, Any]]:
    """代码常量的只读投影（含 `implemented`）；文本维度的阈值标注占位。"""
    catalog: list[dict[str, Any]] = []
    for dimension in DIMENSIONS:
        reason = ALWAYS_NOT_COMPUTED.get(dimension)
        if reason is not None:
            catalog.append(
                {
                    "dimension": dimension,
                    "implemented": False,
                    "notComputedReason": reason,
                    "pairThresholdPermille": None,
                    "alertThresholdPermille": None,
                    "thresholdBasis": None,
                    "thresholdNote": None,
                }
            )
            continue
        thresholds = threshold_for(dimension)
        catalog.append(
            {
                "dimension": dimension,
                "implemented": True,
                "notComputedReason": None,
                "pairThresholdPermille": thresholds.pair_permille,
                "alertThresholdPermille": thresholds.alert_permille,
                "thresholdBasis": THRESHOLD_BASIS_PLACEHOLDER,
                "thresholdNote": THRESHOLD_NOTE,
            }
        )
    return catalog


__all__ = [
    "GenerateResult",
    "Page",
    "ScopeRequest",
    "create_artifact",
    "dimension_catalog",
    "generate_report",
    "get_series_detail",
    "input_fingerprint",
    "list_pairs",
    "list_reports",
    "render_version_detail",
    "render_version_summary",
    "require_series",
    "require_version",
    "resolve_members",
    "scope_digest",
]
