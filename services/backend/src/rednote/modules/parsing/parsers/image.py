"""图片解析器：单图 OCR（WP17）。

**unit = 1**。一张图没有内部结构（除非做版面分析，那属 WP27），
因此 `total_units` 恒为 1，定位是 `WHOLE`（+ 每行 OCR 结果的 `bbox`）。

---

## 为什么"引擎不可用"必须让整张图失败

图片**唯一**的内容来源就是 OCR。若引擎不可用却报成功，
`parsed_units = 1 = total_units` → `state='SUCCEEDED'`，
而这个"成功"的图片解析**没有任何片段**。下游 WP21 会把它切块
入库、WP18 会认为"这个文件已解析完成"。

因此：`OcrEngineUnavailable` → `total_units=1, segments=()` →
`derive_state(1, 0)` = **FAILED**，且 errors 里写着引擎不可用。

## 为什么用 Pillow 先验证"这确实是张图片"

`file_objects.classification` 是**按扩展名与 MIME 判的**
（WP12 的 `classify_file` 明说了"真正的类型判定属 WP17"）。
把 `.exe` 改名成 `.png` 后，扩展名说它是图片，而字节不是 ——
Pillow 打开会失败，本模块据此报 `MALFORMED_DOCUMENT`。
这就是 WP12 留的那个"假阳性"的收口点。
"""

from __future__ import annotations

import io
from typing import Final

from rednote.modules.parsing.ocr import OcrEngine, OcrEngineUnavailable
from rednote.modules.parsing.types import (
    ERROR_MALFORMED_DOCUMENT,
    ERROR_OCR_ENGINE_UNAVAILABLE,
    ERROR_UNIT_FAILED,
    EXTRACTION_OCR,
    MAX_SEGMENT_CHARS,
    POSITION_WHOLE,
    ParseOutcome,
    SegmentDraft,
    SegmentPosition,
    UnitFailure,
)

# 单图像素上限（宽 × 高）。**防解压炸弹**：一张 100 字节的 PNG
# 可以解压成 5 万 × 5 万像素（约 100 亿像素），Pillow 会先把内存吃光。
# 这是"逐 unit 时间预算"拦不住的一类攻击（它不慢，它只是大）。
MAX_IMAGE_PIXELS: Final[int] = 40_000_000  # 4000 万像素（约 8000×5000）


def parse_image(raw: bytes, *, ocr: OcrEngine) -> ParseOutcome:
    """解析图片（jpg / png / webp）。`ocr` 由调用方注入。"""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 依赖已在 pyproject 锁定
        return ParseOutcome(
            total_units=None,
            file_error_code=ERROR_MALFORMED_DOCUMENT,
            file_error_message=f"解析依赖 Pillow 未安装（{type(exc).__name__}）。",
        )

    try:
        # `Image.open` 是惰性的（只读文件头），因此必须先验证再谈尺寸。
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
    except Exception as exc:  # noqa: BLE001 - Pillow 的异常类型很多
        return ParseOutcome(
            total_units=None,
            file_error_code=ERROR_MALFORMED_DOCUMENT,
            file_error_message=f"不是可识别的图片（{type(exc).__name__}）。",
        )

    if width <= 0 or height <= 0:
        return ParseOutcome(
            total_units=None,
            file_error_code=ERROR_MALFORMED_DOCUMENT,
            file_error_message="图片尺寸非法。",
        )
    if width * height > MAX_IMAGE_PIXELS:
        return ParseOutcome(
            total_units=None,
            file_error_code=ERROR_MALFORMED_DOCUMENT,
            file_error_message=(
                f"图片像素数 {width * height} 超过上限 {MAX_IMAGE_PIXELS}。"
            ),
        )

    try:
        result = ocr.recognize(image_bytes=raw)
    except OcrEngineUnavailable as exc:
        # **total_units = 1（客观事实：这是一张图），segments 为空。**
        # 于是 derive_state(1, 0) = FAILED —— 图片唯一的解析结果就是 OCR 文本，
        # 拿不到它就没有任何东西可交给下游。
        return ParseOutcome(
            total_units=1,
            file_error_code=ERROR_OCR_ENGINE_UNAVAILABLE,
            file_error_message=str(exc),
            ocr_engine=ocr.engine_name,
        )
    except Exception as exc:  # noqa: BLE001
        return ParseOutcome(
            total_units=1,
            file_error_code=ERROR_UNIT_FAILED,
            file_error_message=f"OCR 引擎报错（{type(exc).__name__}）。",
            ocr_engine=ocr.engine_name,
        )

    segments: list[SegmentDraft] = []
    failures: list[UnitFailure] = []
    for line in result.lines:
        if line.text.strip() == "":
            continue
        segments.append(
            SegmentDraft(
                position=SegmentPosition(
                    kind=POSITION_WHOLE, bbox=line.bbox
                ),
                text_content=line.text[:MAX_SEGMENT_CHARS],
                extraction_kind=EXTRACTION_OCR,
                confidence=line.confidence,
            )
        )
    if not segments:
        # 引擎可用但一个字都没认出来：**这是 FAILED，不是"成功但为空"**。
        failures.append(
            UnitFailure(
                unit=1,
                code=ERROR_UNIT_FAILED,
                message="图片中未识别到任何文字。",
            )
        )
    # **`units_parsed` 必须显式给**：一张图是 1 个 unit，但 OCR 出 N 行
    # 就是 N 个片段。默认退回 `len(segments)` 会让 `parsed_units=4 >
    # total_units=1` 被 `derive_state` 判成"计数自相矛盾" → FAILED，
    # 而这张图其实识别得很成功。
    return ParseOutcome(
        total_units=1,
        units_parsed=1 if segments else 0,
        segments=tuple(segments),
        failures=tuple(failures),
        ocr_engine=result.engine if segments else ocr.engine_name,
    )


__all__ = ["MAX_IMAGE_PIXELS", "parse_image"]
