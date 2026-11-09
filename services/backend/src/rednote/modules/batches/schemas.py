"""WP32 请求体 / 响应体模型（协议 §1 的 camelCase 外部契约）。

* 请求体：逐字段 `alias`（本仓约定），`extra="forbid"`（多余键即拒，OP-156）；
  **只做类型约束**，取值域与数量边界全部交给服务层（失败给 422 + `details.reason`）。
  `quantity` 刻意**不声明 `ge=1`**：下界由 `batch.limits` 的 `validate_quantity` 判，
  错误体才带 `limit` + `source`（ADR-13）。
* 响应体：`response_model` 是输出过滤器，声明里没有的键会被静默剥掉 ——
  所以显式列全。响应字段直接用 camelCase 属性名（`service.render_batch` 返回的就是这个形状）。
* 缺失不填 0：凡可能“尚未算出 / 无权看”的量一律 `| None`。
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CreateBatchRequestBody(BaseModel):
    """`POST /projects/{projectId}/batches` 与 `.../batch-previews` 的请求体。"""

    model_config = ConfigDict(
        populate_by_name=True, extra="forbid", protected_namespaces=()
    )

    batch_kind: str = Field(alias="batchKind")
    quantity: int
    plan_version_id: uuid.UUID = Field(alias="planVersionId")
    baseline_id: uuid.UUID | None = Field(default=None, alias="baselineId")
    model_profile_id: uuid.UUID = Field(alias="modelProfileId")
    scope: str


class _Response(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class LimitBody(_Response):
    applied: int
    source: str


class EstimateBody(_Response):
    """预估（`isEstimate` 恒 true：预估不与实际金额同字段）。无权 / 未预估时金额与币种为 null。"""

    amount: str | None
    currency: str | None
    isEstimate: bool
    refs: list[dict[str, Any]]


class ProgressBody(_Response):
    state: str
    total: int
    counts: dict[str, int]
    #: `WAITING_RECONCILIATION` 的条数，单列。
    needsReconciliation: int
    #: 没有 run / job 的成员数（>0 时 state 必为 UNKNOWN）。
    missing: int


class BatchBody(_Response):
    id: str
    projectId: str
    batchKind: str
    seqNo: int
    displayCode: str
    quantity: int
    state: str
    planVersionId: str
    baselineId: str
    #: 列表里为 null（不逐条读基线投影）；详情里 CURRENT / SUPERSEDED / UNKNOWN。
    baselineState: str | None
    allocation: dict[str, Any]
    limit: LimitBody
    estimate: EstimateBody
    #: 审核汇总：本包不聚合（WP29 / WP31 提供），恒 null。
    reviewSummary: dict[str, Any] | None
    progress: ProgressBody
    cancelRequestedAt: str | None
    createdBy: str | None
    createdAt: str
    updatedAt: str
    etag: str


class CreateBatchResponseBody(BatchBody):
    """创建响应 = 批次详情 + `replayed`（重放为 true，状态码 200；首次 202）。"""

    replayed: bool


class BatchItemBody(_Response):
    ordinal: int
    contentId: str
    sceneKey: str | None
    formKey: str | None
    latestRound: int | None
    jobId: str | None
    jobState: str | None


class RetriedItemBody(_Response):
    contentId: str
    jobId: str
    roundNo: int


class RetryResponseBody(_Response):
    batchId: str
    retriedCount: int
    retried: list[RetriedItemBody]


class PreviewEstimateBody(_Response):
    amount: str | None
    currency: str | None
    isEstimate: bool
    lines: list[dict[str, Any]]


class PreviewBudgetBody(_Response):
    configured: bool
    currency: str | None
    available: str | None


class PreviewGateBody(_Response):
    """关口结论：`passed` 为 null = 关口尚未接线 / 未知（**不是** false）。"""

    passed: bool | None
    decisionId: str | None
    decidedAt: str | None
    reason: str | None


class BatchPreviewBody(_Response):
    batchKind: str
    quantity: int
    limit: LimitBody
    allocation: dict[str, Any]
    estimate: PreviewEstimateBody
    budget: PreviewBudgetBody
    gate: PreviewGateBody


__all__ = [
    "BatchBody",
    "BatchItemBody",
    "BatchPreviewBody",
    "CreateBatchRequestBody",
    "CreateBatchResponseBody",
    "EstimateBody",
    "LimitBody",
    "PreviewBudgetBody",
    "PreviewEstimateBody",
    "PreviewGateBody",
    "ProgressBody",
    "RetriedItemBody",
    "RetryResponseBody",
]
