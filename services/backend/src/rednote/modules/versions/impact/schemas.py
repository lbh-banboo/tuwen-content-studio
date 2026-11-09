"""WP34 的请求 / 响应体（字段 camelCase；写端点 `extra="forbid"`）。

响应体与 `service.py` 返回的 dict 逐键对应；`response_model` 是输出过滤器，漏声明的键会被静默剥掉，
所以这里每个键都显式列出。可空字段保留 `null`（`isCurrent=null` 表示无法判断，**不是 false**）。
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class SweepRequestBody(BaseModel):
    """`contentItemIds` 与 `fromVersionId` 恰给一个（由 service 校验并返回 `SCOPE_INVALID`）。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    content_item_ids: list[uuid.UUID] | None = Field(default=None, alias="contentItemIds")
    from_version_id: uuid.UUID | None = Field(default=None, alias="fromVersionId")


class StartBody(_Out):
    version_id: uuid.UUID = Field(alias="versionId")
    entity_type: str = Field(alias="entityType")
    entity_id: uuid.UUID = Field(alias="entityId")
    revision_no: int = Field(alias="revisionNo")
    is_current: bool | None = Field(alias="isCurrent")


class TargetBody(_Out):
    version_id: uuid.UUID = Field(alias="versionId")
    revision_no: int = Field(alias="revisionNo")


class ImpactNodeBody(_Out):
    version_id: uuid.UUID = Field(alias="versionId")
    entity_type: str = Field(alias="entityType")
    entity_id: uuid.UUID = Field(alias="entityId")
    revision_no: int = Field(alias="revisionNo")
    depth: int
    purposes: list[str]
    is_current: bool | None = Field(alias="isCurrent")
    impact_class: str = Field(alias="impactClass")


class TraversalBody(_Out):
    complete: bool
    truncation: str | None
    max_depth_reached: int = Field(alias="maxDepthReached")
    node_count: int = Field(alias="nodeCount")
    node_count_is_lower_bound: bool = Field(alias="nodeCountIsLowerBound")
    anomalies: list[str]
    cycle_node_count: int = Field(alias="cycleNodeCount")


class ExportImpactItemBody(_Out):
    export_item_id: uuid.UUID = Field(alias="exportItemId")
    export_group_id: uuid.UUID = Field(alias="exportGroupId")
    content_item_id: uuid.UUID = Field(alias="contentItemId")
    frozen_version_id: uuid.UUID = Field(alias="frozenVersionId")
    export_impact: str = Field(alias="exportImpact")
    recorded_state: str = Field(alias="recordedState")


class ExportImpactBody(_Out):
    complete: bool
    count_is_lower_bound: bool = Field(alias="countIsLowerBound")
    items: list[ExportImpactItemBody]


class ImpactPreviewBody(_Out):
    start: StartBody
    target: TargetBody | None
    nodes: list[ImpactNodeBody]
    traversal: TraversalBody
    by_type: dict[str, int] = Field(alias="byType")
    exports: ExportImpactBody


class ValidityItemBody(_Out):
    export_item_id: uuid.UUID = Field(alias="exportItemId")
    content_item_id: uuid.UUID = Field(alias="contentItemId")
    frozen_version_id: uuid.UUID = Field(alias="frozenVersionId")
    derived_state: str = Field(alias="derivedState")
    derived_reason: str | None = Field(alias="derivedReason")
    unknown_reason: str | None = Field(alias="unknownReason")
    recorded_state: str = Field(alias="recordedState")
    in_sync: bool | None = Field(alias="inSync")


class ValiditySummaryBody(_Out):
    valid: int
    invalid: int
    unknown: int


class ValidityBody(_Out):
    export_group_id: uuid.UUID = Field(alias="exportGroupId")
    summary: ValiditySummaryBody
    items: list[ValidityItemBody]


class SweepScopeBody(_Out):
    kind: str
    content_item_ids: list[uuid.UUID] = Field(alias="contentItemIds")
    from_version_id: uuid.UUID | None = Field(alias="fromVersionId")


class SweepSummaryBody(_Out):
    invalidated: int
    restored: int
    unchanged: int
    unknown: int


class SweepItemBody(_Out):
    export_item_id: uuid.UUID = Field(alias="exportItemId")
    content_item_id: uuid.UUID = Field(alias="contentItemId")
    previous_state: str = Field(alias="previousState")
    derived_state: str = Field(alias="derivedState")
    derived_reason: str | None = Field(alias="derivedReason")
    unknown_reason: str | None = Field(alias="unknownReason")
    action: str
    seq: int | None


class SweepBody(_Out):
    scope: SweepScopeBody
    summary: SweepSummaryBody
    items: list[SweepItemBody]


class ValidityEventBody(_Out):
    id: uuid.UUID
    export_item_id: uuid.UUID = Field(alias="exportItemId")
    export_group_id: uuid.UUID = Field(alias="exportGroupId")
    content_item_id: uuid.UUID = Field(alias="contentItemId")
    seq: int
    transition: str
    reason: str | None
    observed_version_id: uuid.UUID = Field(alias="observedVersionId")
    observed_eligibility_hash: str = Field(alias="observedEligibilityHash")
    scope_kind: str = Field(alias="scopeKind")
    source_version_id: uuid.UUID | None = Field(alias="sourceVersionId")
    created_by: uuid.UUID = Field(alias="createdBy")
    created_at: str = Field(alias="createdAt")


__all__: list[str] = [name for name in dir() if name.endswith("Body")]
