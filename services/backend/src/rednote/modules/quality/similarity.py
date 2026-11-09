"""字面相似度：规范化 → 字符 2-gram 集合 → Jaccard（整数千分比）。**纯函数，无 IO，无浮点。**

只 import `types` 与标准库（AST 判据钉住）。**只捕捉字面重合，抓不到换词改写**（设计稿 §4.5）——
所以只能说「已检查字面相似」，不能宣称「已检查重复」。

## 约定

* `normalize`：NFKC → casefold → 删除所有空白 / 标点 / 符号 / 控制类字符（Unicode 类 `Z* P* S* C*`）
  以及变体选择符 `U+FE00..U+FE0F`（emoji 的附属字符，否则「只差 emoji」会残留孤儿选择符）。
  **会抹掉 emoji 差异**——有意为之（emoji 不是文字重复），已登记为已知取舍：只差 emoji 的两篇判 1000‰。
  不用 `str.split`（切段只许经 media 的纯函数转发，AST 判据钉住）。
* `grams`：规范化后长度 ≥2 取字符 2-gram 集合；长度 =1 取 1-gram；长度 =0 取空集——
  **空集表示「该篇该维度没有可比文本」，不是相似度 0**。
* `pair_counts`：任一侧为空集返回 `None`（**缺失不填 0**）；否则 `(交集数, 并集数)`。
* 阈值比较用交叉相乘 `交集 * 1000 >= 阈值 * 并集`，与「floor 后比较」等价但不丢精度。
"""

from __future__ import annotations

import unicodedata

from rednote.modules.quality.types import MIN_COMPARED_ITEMS

_DROPPED_CATEGORY_PREFIXES = ("Z", "P", "S", "C")
_VARIATION_SELECTOR_FIRST = 0xFE00
_VARIATION_SELECTOR_LAST = 0xFE0F


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        ch
        for ch in folded
        if not unicodedata.category(ch).startswith(_DROPPED_CATEGORY_PREFIXES)
        and not (_VARIATION_SELECTOR_FIRST <= ord(ch) <= _VARIATION_SELECTOR_LAST)
    )


def grams(text: str) -> frozenset[str]:
    """规范化后的字符 2-gram 集合；1 字取 1-gram；规范化后为空取空集。"""
    norm = normalize(text)
    if not norm:
        return frozenset()
    if len(norm) == 1:
        return frozenset({norm})
    return frozenset(norm[i : i + 2] for i in range(len(norm) - 1))


def pair_counts(a: frozenset[str], b: frozenset[str]) -> tuple[int, int] | None:
    """`(|A∩B|, |A∪B|)`；任一侧无可比文本 ⇒ `None`（不是 0）。"""
    if not a or not b:
        return None
    inter = len(a & b)
    return inter, len(a) + len(b) - inter


def similarity_permille(intersection: int, union: int) -> int:
    """`floor(1000 * ∩ / ∪)`，整数千分比 0..1000（近似值：展示时应标 approximate）。"""
    return intersection * 1000 // union


def is_similar(intersection: int, union: int, pair_threshold_permille: int) -> bool:
    """交叉相乘判相似：`∩ * 1000 >= 阈值 * ∪`（>=，恰好等于也算）。"""
    return intersection * 1000 >= pair_threshold_permille * union


def alert_verdict_is_alert(flagged: int, compared: int, alert_permille: int) -> bool:
    """维度告警：`flagged * 1000 > alert * compared`（**严格大于**）。"""
    if compared < MIN_COMPARED_ITEMS:
        raise ValueError("compared < 2 的维度不应进入判定（应为 NOT_COMPUTED）")
    return flagged * 1000 > alert_permille * compared


__all__ = [
    "alert_verdict_is_alert",
    "grams",
    "is_similar",
    "normalize",
    "pair_counts",
    "similarity_permille",
]
