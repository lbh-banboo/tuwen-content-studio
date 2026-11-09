"""`quality` → `media` 的唯一接缝（WP33）。**本包零切段实现，也不复刻顺序对位规则。**

两个转发目标都是 `media.schemas` 的**纯函数**：

* `paragraph_refs_for_body(body)` → `(序号, 原文, ref)`（切段口径三处实现之一，接缝冻结 §5.1；
  `check_paragraph_split_parity.py::_IMPL_FILES` 必须仍是 3）；
* `assign_paragraph_refs(paragraphs)` → 顺序对位的期望 ref 元组（OP-199 的唯一落点）。
  零段时抛 `SchemaViolation(reason=PARAGRAPH_REF_UNKNOWN)`，**不编 ref**。

`SchemaViolation` 原样冒出（不转换类型）。
"""

from __future__ import annotations

from rednote.modules.media.schemas import (
    SchemaViolation,
    assign_paragraph_refs,
    paragraph_refs_for_body,
)


def split_body_into_paragraphs(body: str) -> tuple[tuple[int, str, str], ...]:
    """把正文切成 `(序号, 原文, ref)` 三元组，逐字转发给 media。"""
    return paragraph_refs_for_body(body)


def expected_refs_for(
    paragraphs: tuple[tuple[int, str, str], ...],
) -> tuple[str, ...]:
    """顺序对位的期望 ref（长度 = `images_per_content()`），逐字转发给 media。"""
    return assign_paragraph_refs(paragraphs)


__all__ = ["SchemaViolation", "expected_refs_for", "split_body_into_paragraphs"]
