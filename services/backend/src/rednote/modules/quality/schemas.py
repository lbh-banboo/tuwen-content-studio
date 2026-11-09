"""WP33 请求体 / 响应体模型（协议 §1 的 camelCase 外部契约）。

* 请求体 `extra="forbid"`，只做类型约束；取值域与范围校验交服务层（422 + `details.reason`）。
* 响应体：`response_model` 是输出过滤器，声明里没有的键会被静默剥掉，所以显式列全。
* **缺失不填 0**：`NOT_COMPUTED` 维度的数值列一律 `| None`。
* 相似度是整数千分比（floor），响应带 `approximate: true`；阈值带 `thresholdBasis`
  （占位，待 OP-228），**不是产品口径**。
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field


class ScopeBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    kind: str
    content_ids: list[uuid.UUID] | None = Field(default=None, alias="contentIds")
    batch_id: uuid.UUID | None = Field(default=None, alias="batchId")


class GenerateReportRequestBody(BaseModel):
    """`POST /projects/{projectId}/quality-reports`。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    scope: ScopeBody


class ArtifactRequestBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    format: str


class _Response(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class ReportItemBody(_Response):
    contentItemId: str
    contentVersionId: str | None
    position: int
    status: str
    excludeReason: str | None
    paragraphCount: int | None
    generationContextId: str | None
    baselineId: str | None
    #: 仅详情端点 ③ 给出（CURRENT / STALE）；列表与 ④ 不给（避免假精确）。
    subjectState: str | None = None


class DimensionBody(_Response):
    dimension: str
    implemented: bool
    status: str
    notComputedReason: str | None
    verdict: str | None
    comparedItemCount: int | None
    flaggedItemCount: int | None
    pairThresholdPermille: int | None
    alertThresholdPermille: int | None
    maxSimilarityPermille: int | None
    #: 相似度是 floor 后的整数千分比，是近似值。
    approximate: bool


class AlignmentSummaryBody(_Response):
    aligned: int
    misaligned: int
    missing: int
    notEvaluable: int
    extraMediaCount: int | None


class VersionSummaryBody(_Response):
    id: str
    reportId: str
    revisionNo: int
    inputFingerprint: str
    algoVersion: int
    schemaVersion: int
    itemCount: int
    includedItemCount: int
    excludedItemCount: int
    okDimensionCount: int
    alertDimensionCount: int
    notComputedDimensionCount: int
    outcome: str
    thresholdBasis: str
    thresholdNote: str
    startedAt: str
    finishedAt: str
    createdAt: str


class VersionDetailBody(VersionSummaryBody):
    items: list[ReportItemBody]
    dimensions: list[DimensionBody]
    alignmentSummary: AlignmentSummaryBody
    staleItemCount: int | None = None


class GenerateReportBody(VersionDetailBody):
    #: 本次是否新建了版本（输入指纹与当前版本相同 ⇒ false，返回既有版本）。
    created: bool


class SeriesBody(_Response):
    id: str
    projectId: str
    scopeKind: str
    batchId: str | None
    currentVersionId: str | None
    currentRevisionNo: int
    createdAt: str
    updatedAt: str
    currentVersion: VersionSummaryBody | None


class SeriesDetailBody(_Response):
    id: str
    projectId: str
    scopeKind: str
    batchId: str | None
    currentVersionId: str | None
    currentRevisionNo: int
    createdAt: str
    updatedAt: str
    currentVersion: VersionDetailBody | None


class PairBody(_Response):
    id: str
    dimension: str
    contentItemAId: str
    contentItemBId: str
    intersectionCount: int
    unionCount: int
    similarityPermille: int
    approximate: bool


class ArtifactBody(_Response):
    id: str
    reportVersionId: str
    format: str
    fileObjectId: str
    contentSha256: str
    byteSize: int
    createdAt: str
    created: bool


class DimensionCatalogBody(_Response):
    dimension: str
    implemented: bool
    notComputedReason: str | None
    pairThresholdPermille: int | None
    alertThresholdPermille: int | None
    thresholdBasis: str | None
    thresholdNote: str | None


__all__ = [
    "AlignmentSummaryBody",
    "ArtifactBody",
    "ArtifactRequestBody",
    "DimensionBody",
    "DimensionCatalogBody",
    "GenerateReportBody",
    "GenerateReportRequestBody",
    "PairBody",
    "ReportItemBody",
    "ScopeBody",
    "SeriesBody",
    "SeriesDetailBody",
    "VersionDetailBody",
    "VersionSummaryBody",
]
