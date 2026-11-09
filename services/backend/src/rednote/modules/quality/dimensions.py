"""7 个维度的计算与图文顺序对位。**纯函数，无 IO。** 只 import `types` / `similarity` 与标准库。

* 4 个文本维度实算（`TITLE / OPENING / ENDING / BODY`）；
* 3 个恒 `NOT_COMPUTED`（`STRUCTURE / IMAGE_SIMILARITY / SCENE_CONCENTRATION`）带原因码——
  缺席会被前端渲染成「通过」，明说「没算 + 原因」才是防线。
* 「没算」（`NOT_COMPUTED`，全部数值列 `None`）与「算了得 0」（`COMPUTED` 且 `flagged=0`）数据形态不同。
* 对位：期望 ref 由 media 的纯函数给出（经 bridge，**本文件不复刻对位规则**），这里只比对。
  **不断言 ref 唯一或一一对应**（多张落同一段是合法的）。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from rednote.modules.quality.similarity import (
    alert_verdict_is_alert,
    is_similar,
    pair_counts,
    similarity_permille,
)
from rednote.modules.quality.types import (
    ALIGN_ALIGNED,
    ALIGN_MISALIGNED,
    ALIGN_MISSING,
    ALIGN_NOT_EVALUABLE,
    ALIGN_REASON_PARAGRAPH_REF_UNKNOWN,
    ALWAYS_NOT_COMPUTED,
    DIMENSION_STATUS_COMPUTED,
    DIMENSION_STATUS_NOT_COMPUTED,
    MIN_COMPARED_ITEMS,
    NOT_COMPUTED_REASON_INSUFFICIENT_ITEMS,
    VERDICT_ALERT,
    VERDICT_OK,
    AlignmentRow,
    DimensionOutcome,
    MediaSlot,
    PairHit,
    Thresholds,
)


def not_computed(dimension: str, reason: str) -> DimensionOutcome:
    """没算：所有数值列与 verdict 为 `None`。"""
    return DimensionOutcome(
        dimension=dimension,
        status=DIMENSION_STATUS_NOT_COMPUTED,
        not_computed_reason=reason,
    )


def always_not_computed(dimension: str) -> DimensionOutcome:
    return not_computed(dimension, ALWAYS_NOT_COMPUTED[dimension])


def compute_text_dimension(
    dimension: str,
    grams_by_item: Mapping[uuid.UUID, frozenset[str]],
    thresholds: Thresholds,
) -> DimensionOutcome:
    """对一个文本维度做两两比较。

    `grams_by_item`：content_item_id → 该篇该维度的 2-gram 集合；**空集的篇目视为不可比，不计入 compared**。
    可比篇数 < 2 ⇒ `NOT_COMPUTED(INSUFFICIENT_ITEMS)`（不是 OK，更不是 0）。
    """
    comparable = sorted(
        ((item_id, g) for item_id, g in grams_by_item.items() if g), key=lambda x: x[0]
    )
    if len(comparable) < MIN_COMPARED_ITEMS:
        return not_computed(dimension, NOT_COMPUTED_REASON_INSUFFICIENT_ITEMS)

    hits: list[PairHit] = []
    flagged: set[uuid.UUID] = set()
    max_permille = 0
    for i, (a_id, a_grams) in enumerate(comparable):
        for b_id, b_grams in comparable[i + 1 :]:
            counts = pair_counts(a_grams, b_grams)
            if counts is None:  # 不可达（已滤空集），保留作兜底而非默默当 0
                raise ValueError("可比篇目的 2-gram 集合不应为空")
            inter, union = counts
            max_permille = max(max_permille, similarity_permille(inter, union))
            if is_similar(inter, union, thresholds.pair_permille):
                hits.append(PairHit(a_id, b_id, inter, union))
                flagged.update((a_id, b_id))

    compared = len(comparable)
    verdict = (
        VERDICT_ALERT
        if alert_verdict_is_alert(len(flagged), compared, thresholds.alert_permille)
        else VERDICT_OK
    )
    return DimensionOutcome(
        dimension=dimension,
        status=DIMENSION_STATUS_COMPUTED,
        verdict=verdict,
        compared_item_count=compared,
        flagged_item_count=len(flagged),
        pair_threshold_permille=thresholds.pair_permille,
        alert_threshold_permille=thresholds.alert_permille,
        max_similarity_permille=max_permille,
        pairs=tuple(hits),
    )


def align_media(
    expected_refs: Sequence[str] | None, slots: Sequence[MediaSlot]
) -> tuple[tuple[AlignmentRow, ...], int]:
    """顺序对位。返回 `(逐槽位结果, 多余图张数)`。

    * `expected_refs is None`：期望 ref 算不出（零段正文，fail-closed）⇒ 每个现有图位一行
      `NOT_EVALUABLE`（`expected_ref=None`），**不编 ref、不判 ALIGNED**；
    * 否则槽位数 = `len(expected_refs)`（不写死 3）：槽位 i 取 `sort_order == i` 的图；
      无图 ⇒ `MISSING`；`actual == expected` ⇒ `ALIGNED`，否则 `MISALIGNED`；
    * `sort_order` 超出槽位的图不落对位行，计入「多余图张数」。
    """
    ordered = sorted(slots, key=lambda s: (s.sort_order, str(s.media_item_id)))
    if expected_refs is None:
        rows = tuple(
            AlignmentRow(
                sort_order=slot.sort_order,
                verdict=ALIGN_NOT_EVALUABLE,
                expected_ref=None,
                media_item_id=slot.media_item_id,
                media_state=slot.state,
                actual_ref=slot.paragraph_ref,
                reason=ALIGN_REASON_PARAGRAPH_REF_UNKNOWN,
            )
            for slot in ordered
        )
        return rows, 0

    by_order: dict[int, MediaSlot] = {}
    for slot in ordered:
        by_order.setdefault(slot.sort_order, slot)
    rows_list: list[AlignmentRow] = []
    for index, expected in enumerate(expected_refs):
        slot = by_order.get(index)
        if slot is None:
            rows_list.append(
                AlignmentRow(index, ALIGN_MISSING, expected, None, None, None)
            )
            continue
        verdict = ALIGN_ALIGNED if slot.paragraph_ref == expected else ALIGN_MISALIGNED
        rows_list.append(
            AlignmentRow(
                index, verdict, expected, slot.media_item_id, slot.state, slot.paragraph_ref
            )
        )
    used = sum(1 for row in rows_list if row.media_item_id is not None)
    extra = len(ordered) - used
    return tuple(rows_list), extra


__all__ = [
    "align_media",
    "always_not_computed",
    "compute_text_dimension",
    "not_computed",
]
