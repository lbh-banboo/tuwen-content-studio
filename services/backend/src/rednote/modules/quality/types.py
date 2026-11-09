"""WP33 的取值域、占位阈值与纯数据类型。**不 import 任何 `rednote.modules.*`**（平级模块不可互 import）。

## 阈值：**占位，待 OP-228**（不是产品口径）

「相似度多少算重复」「一个维度里多大比例的稿子重复算告警」**全仓没有需求依据**
（OP-228，用户尚未给数值；原型静态页上的 70% / 75% 无法反推口径，设计稿 §4.2）。
本文件只放**占位常量**，且：

* 取值刻意**保守**（宁可漏报也不误报，使 `ALERT` 不会被误读成产品结论）：
  单对阈值 `950‰`（近乎逐字雷同才算一对）、维度告警阈值 `500‰`（超过一半的稿子被标记才告警）。
  两个数都是整千分比的整数，**不与原型的 70 / 75 / 60 / 45 重合**，一眼看出不是业务数；
* 每份报告把**当时使用的阈值落库快照**（维度行的两个阈值列 + payload 的 `thresholdBasis`），
  所以将来改本文件的常量**不会改写任何历史报告**；
* 任何响应都带 `thresholdBasis = "PLACEHOLDER_PENDING_OP-228"`，`ALERT` 只是「相对占位阈值的提示」，
  **不得作为阻断条件**（设计稿 §4.5）。

## 整数运算

相似度是整数千分比（floor）；阈值比较用交叉相乘（`交集 * 1000 >= 阈值 * 并集`），全程无浮点。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------

#: 算法版本（落库快照）。「今天判重、上周没判」靠它区分「稿子变了」与「算法变了」。
ALGO_VERSION: Final[int] = 1
SCHEMA_VERSION: Final[int] = 1

#: 单份报告最多纳入的篇数（占位，设计稿 Q-04）；超限 422，**不抽样**。
MAX_REPORT_ITEMS: Final[int] = 200

# ---------------------------------------------------------------------------
# 占位阈值（OP-228）
# ---------------------------------------------------------------------------

THRESHOLD_BASIS_PLACEHOLDER: Final[str] = "PLACEHOLDER_PENDING_OP-228"
THRESHOLD_NOTE: Final[str] = (
    "阈值为占位值，待 OP-228 给出业务口径；ALERT 仅是相对占位阈值的提示，不是产品结论，不得作为阻断条件。"
)
PLACEHOLDER_PAIR_THRESHOLD_PERMILLE: Final[int] = 950
PLACEHOLDER_ALERT_THRESHOLD_PERMILLE: Final[int] = 500

# ---------------------------------------------------------------------------
# 维度
# ---------------------------------------------------------------------------

DIMENSION_TITLE: Final[str] = "TITLE"
DIMENSION_OPENING: Final[str] = "OPENING"
DIMENSION_ENDING: Final[str] = "ENDING"
DIMENSION_BODY: Final[str] = "BODY"
DIMENSION_STRUCTURE: Final[str] = "STRUCTURE"
DIMENSION_IMAGE_SIMILARITY: Final[str] = "IMAGE_SIMILARITY"
DIMENSION_SCENE_CONCENTRATION: Final[str] = "SCENE_CONCENTRATION"

#: 报告里恒有的 7 个维度（顺序即展示顺序）。
DIMENSIONS: Final[tuple[str, ...]] = (
    DIMENSION_TITLE,
    DIMENSION_OPENING,
    DIMENSION_ENDING,
    DIMENSION_BODY,
    DIMENSION_STRUCTURE,
    DIMENSION_IMAGE_SIMILARITY,
    DIMENSION_SCENE_CONCENTRATION,
)
#: 实算的 4 个文本维度。
TEXT_DIMENSIONS: Final[tuple[str, ...]] = (
    DIMENSION_TITLE,
    DIMENSION_OPENING,
    DIMENSION_ENDING,
    DIMENSION_BODY,
)

NOT_COMPUTED_REASON_INSUFFICIENT_ITEMS: Final[str] = "INSUFFICIENT_ITEMS"
NOT_COMPUTED_REASON_STRUCTURE: Final[str] = "STRUCTURE_NOT_STRUCTURED"
NOT_COMPUTED_REASON_IMAGE: Final[str] = "IMAGE_SIMILARITY_UNAVAILABLE"
#: 字面值与 WP30 `review.types.SKIP_REASON_NO_SCENE_LINKAGE_IN_SCHEMA` 一致（跨包 parity 测试钉住）。
NOT_COMPUTED_REASON_SCENE: Final[str] = "NO_SCENE_LINKAGE_IN_SCHEMA"

#: 恒 NOT_COMPUTED 的维度 → 原因码（algo_version=1）。
ALWAYS_NOT_COMPUTED: Final[dict[str, str]] = {
    DIMENSION_STRUCTURE: NOT_COMPUTED_REASON_STRUCTURE,
    DIMENSION_IMAGE_SIMILARITY: NOT_COMPUTED_REASON_IMAGE,
    DIMENSION_SCENE_CONCENTRATION: NOT_COMPUTED_REASON_SCENE,
}

#: 维度实算至少需要的篇数（<2 ⇒ NOT_COMPUTED，不是 0 也不是 OK）。
MIN_COMPARED_ITEMS: Final[int] = 2

DIMENSION_STATUS_COMPUTED: Final[str] = "COMPUTED"
DIMENSION_STATUS_NOT_COMPUTED: Final[str] = "NOT_COMPUTED"
DIMENSION_STATUSES: Final[tuple[str, ...]] = (
    DIMENSION_STATUS_COMPUTED,
    DIMENSION_STATUS_NOT_COMPUTED,
)
VERDICT_OK: Final[str] = "OK"
VERDICT_ALERT: Final[str] = "ALERT"
VERDICTS: Final[tuple[str, ...]] = (VERDICT_OK, VERDICT_ALERT)

# ---------------------------------------------------------------------------
# 范围 / 结论 / 篇目
# ---------------------------------------------------------------------------

SCOPE_CONTENT_SET: Final[str] = "CONTENT_SET"
SCOPE_PROJECT: Final[str] = "PROJECT"
SCOPE_BATCH: Final[str] = "BATCH"
SCOPE_KINDS: Final[tuple[str, ...]] = (SCOPE_CONTENT_SET, SCOPE_PROJECT, SCOPE_BATCH)

OUTCOME_ALERTED: Final[str] = "ALERTED"
OUTCOME_NO_ALERT: Final[str] = "NO_ALERT"
OUTCOMES: Final[tuple[str, ...]] = (OUTCOME_ALERTED, OUTCOME_NO_ALERT)

ITEM_INCLUDED: Final[str] = "INCLUDED"
ITEM_EXCLUDED: Final[str] = "EXCLUDED"
ITEM_STATUSES: Final[tuple[str, ...]] = (ITEM_EXCLUDED, ITEM_INCLUDED)
EXCLUDE_NO_VERSION: Final[str] = "NO_VERSION"
EXCLUDE_EMPTY_BODY: Final[str] = "EMPTY_BODY"

# 顺序对位
ALIGN_ALIGNED: Final[str] = "ALIGNED"
ALIGN_MISALIGNED: Final[str] = "MISALIGNED"
ALIGN_MISSING: Final[str] = "MISSING"
ALIGN_NOT_EVALUABLE: Final[str] = "NOT_EVALUABLE"
ALIGN_VERDICTS: Final[tuple[str, ...]] = (
    ALIGN_ALIGNED,
    ALIGN_MISALIGNED,
    ALIGN_MISSING,
    ALIGN_NOT_EVALUABLE,
)
#: 借用 media 现有字面量（`media.types.REASON_PARAGRAPH_REF_UNKNOWN`），不落 CHECK。
ALIGN_REASON_PARAGRAPH_REF_UNKNOWN: Final[str] = "PARAGRAPH_REF_UNKNOWN"

ARTIFACT_FORMAT_JSON: Final[str] = "JSON"
ARTIFACT_FORMATS: Final[tuple[str, ...]] = (ARTIFACT_FORMAT_JSON,)

SUBJECT_CURRENT: Final[str] = "CURRENT"
SUBJECT_STALE: Final[str] = "STALE"

#: 依赖边 E11（报告版本 → 各内容版本）的 purpose（`version_dependencies.purpose` 无 CHECK）。
PURPOSE_REPORT_INPUT: Final[str] = "REPORT_INPUT"

SHA256_HEX_CHARS: Final[int] = 64
DIMENSION_COUNT: Final[int] = 7

# ---------------------------------------------------------------------------
# 错误细分（`details.reason`）
# ---------------------------------------------------------------------------

REASON_REPORT_NOT_FOUND: Final[str] = "QUALITY_REPORT_NOT_FOUND"
REASON_REPORT_VERSION_NOT_FOUND: Final[str] = "QUALITY_REPORT_VERSION_NOT_FOUND"
REASON_SCOPE_EMPTY: Final[str] = "QUALITY_SCOPE_EMPTY"
REASON_SCOPE_TOO_LARGE: Final[str] = "QUALITY_SCOPE_TOO_LARGE"
REASON_CONTENT_NOT_IN_PROJECT: Final[str] = "QUALITY_CONTENT_NOT_IN_PROJECT"
REASON_ARTIFACT_FORMAT_UNSUPPORTED: Final[str] = "QUALITY_ARTIFACT_FORMAT_UNSUPPORTED"
REASON_SCOPE_INVALID: Final[str] = "QUALITY_SCOPE_INVALID"
REASON_BATCH_NOT_IN_PROJECT: Final[str] = "QUALITY_BATCH_NOT_IN_PROJECT"
REASON_DIMENSION_INVALID: Final[str] = "QUALITY_DIMENSION_INVALID"


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


SCOPE_KIND_LIST_SQL: Final[str] = _sql_list(SCOPE_KINDS)
OUTCOME_LIST_SQL: Final[str] = _sql_list(OUTCOMES)
DIMENSION_LIST_SQL: Final[str] = _sql_list(tuple(sorted(DIMENSIONS)))
TEXT_DIMENSION_LIST_SQL: Final[str] = _sql_list(tuple(sorted(TEXT_DIMENSIONS)))
DIMENSION_STATUS_LIST_SQL: Final[str] = _sql_list(DIMENSION_STATUSES)
VERDICT_LIST_SQL: Final[str] = _sql_list(tuple(sorted(VERDICTS)))
ITEM_STATUS_LIST_SQL: Final[str] = _sql_list(ITEM_STATUSES)
ALIGN_VERDICT_LIST_SQL: Final[str] = _sql_list(ALIGN_VERDICTS)
ARTIFACT_FORMAT_LIST_SQL: Final[str] = _sql_list(ARTIFACT_FORMATS)
UNIMPLEMENTED_DIMENSION_LIST_SQL: Final[str] = _sql_list(tuple(sorted(ALWAYS_NOT_COMPUTED)))

# ---------------------------------------------------------------------------
# 阈值取用（读时取常量；落库后是快照）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Thresholds:
    """某一维度本次使用的阈值（千分比）。"""

    pair_permille: int
    alert_permille: int


#: 维度 → 占位阈值。**占位，待 OP-228**；测试会替换本表来证明历史报告不被改写。
THRESHOLDS: Final[dict[str, Thresholds]] = {
    dimension: Thresholds(
        PLACEHOLDER_PAIR_THRESHOLD_PERMILLE, PLACEHOLDER_ALERT_THRESHOLD_PERMILLE
    )
    for dimension in TEXT_DIMENSIONS
}


def threshold_for(dimension: str) -> Thresholds:
    """读取某文本维度**此刻**的占位阈值（每次调用现读，不缓存）。"""
    return THRESHOLDS[dimension]


# ---------------------------------------------------------------------------
# 纯数据类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairHit:
    """一个命中的相似对（`a < b`）。"""

    content_item_a_id: uuid.UUID
    content_item_b_id: uuid.UUID
    intersection_count: int
    union_count: int

    @property
    def similarity_permille(self) -> int:
        return self.intersection_count * 1000 // self.union_count


@dataclass(frozen=True)
class DimensionOutcome:
    """一个维度的结论。`NOT_COMPUTED` 时数值列与 verdict 全为 `None`（不是 0）。"""

    dimension: str
    status: str
    not_computed_reason: str | None = None
    verdict: str | None = None
    compared_item_count: int | None = None
    flagged_item_count: int | None = None
    pair_threshold_permille: int | None = None
    alert_threshold_permille: int | None = None
    max_similarity_permille: int | None = None
    pairs: tuple[PairHit, ...] = ()


@dataclass(frozen=True)
class MediaSlot:
    """`media_items` 里的一行（只取对位需要的列）。"""

    media_item_id: uuid.UUID
    sort_order: int
    paragraph_ref: str
    state: str


@dataclass(frozen=True)
class AlignmentRow:
    """顺序对位的一个槽位结果。"""

    sort_order: int
    verdict: str
    expected_ref: str | None
    media_item_id: uuid.UUID | None
    media_state: str | None
    actual_ref: str | None
    reason: str | None = None


__all__ = [
    "ALGO_VERSION",
    "ALIGN_ALIGNED",
    "ALIGN_MISALIGNED",
    "ALIGN_MISSING",
    "ALIGN_NOT_EVALUABLE",
    "ALIGN_REASON_PARAGRAPH_REF_UNKNOWN",
    "ALIGN_VERDICTS",
    "ALIGN_VERDICT_LIST_SQL",
    "ALWAYS_NOT_COMPUTED",
    "ARTIFACT_FORMATS",
    "ARTIFACT_FORMAT_JSON",
    "ARTIFACT_FORMAT_LIST_SQL",
    "AlignmentRow",
    "DIMENSIONS",
    "DIMENSION_BODY",
    "DIMENSION_COUNT",
    "DIMENSION_ENDING",
    "DIMENSION_IMAGE_SIMILARITY",
    "DIMENSION_LIST_SQL",
    "DIMENSION_OPENING",
    "DIMENSION_SCENE_CONCENTRATION",
    "DIMENSION_STATUSES",
    "DIMENSION_STATUS_COMPUTED",
    "DIMENSION_STATUS_LIST_SQL",
    "DIMENSION_STATUS_NOT_COMPUTED",
    "DIMENSION_STRUCTURE",
    "DIMENSION_TITLE",
    "DimensionOutcome",
    "EXCLUDE_EMPTY_BODY",
    "EXCLUDE_NO_VERSION",
    "ITEM_EXCLUDED",
    "ITEM_INCLUDED",
    "ITEM_STATUSES",
    "ITEM_STATUS_LIST_SQL",
    "MAX_REPORT_ITEMS",
    "MIN_COMPARED_ITEMS",
    "MediaSlot",
    "NOT_COMPUTED_REASON_IMAGE",
    "NOT_COMPUTED_REASON_INSUFFICIENT_ITEMS",
    "NOT_COMPUTED_REASON_SCENE",
    "NOT_COMPUTED_REASON_STRUCTURE",
    "OUTCOMES",
    "OUTCOME_ALERTED",
    "OUTCOME_LIST_SQL",
    "OUTCOME_NO_ALERT",
    "PLACEHOLDER_ALERT_THRESHOLD_PERMILLE",
    "PLACEHOLDER_PAIR_THRESHOLD_PERMILLE",
    "PURPOSE_REPORT_INPUT",
    "PairHit",
    "REASON_ARTIFACT_FORMAT_UNSUPPORTED",
    "REASON_BATCH_NOT_IN_PROJECT",
    "REASON_CONTENT_NOT_IN_PROJECT",
    "REASON_DIMENSION_INVALID",
    "REASON_REPORT_NOT_FOUND",
    "REASON_REPORT_VERSION_NOT_FOUND",
    "REASON_SCOPE_EMPTY",
    "REASON_SCOPE_INVALID",
    "REASON_SCOPE_TOO_LARGE",
    "SCHEMA_VERSION",
    "SCOPE_BATCH",
    "SCOPE_CONTENT_SET",
    "SCOPE_KINDS",
    "SCOPE_KIND_LIST_SQL",
    "SCOPE_PROJECT",
    "SHA256_HEX_CHARS",
    "SUBJECT_CURRENT",
    "SUBJECT_STALE",
    "TEXT_DIMENSIONS",
    "TEXT_DIMENSION_LIST_SQL",
    "THRESHOLDS",
    "THRESHOLD_BASIS_PLACEHOLDER",
    "THRESHOLD_NOTE",
    "Thresholds",
    "UNIMPLEMENTED_DIMENSION_LIST_SQL",
    "VERDICTS",
    "VERDICT_ALERT",
    "VERDICT_LIST_SQL",
    "VERDICT_OK",
    "threshold_for",
]
