"""PDF 解析器：文本层提取 + 无文本层转 OCR（WP17）。

**unit = 页数**。页是 PDF 的天然定位单位，且**页数在解析前就能确定**
（`len(reader.pages)`）—— 这正好满足 `types.py` 对 `total_units` 的要求：
它必须是"客观总量"，不能由解析结果反推。

---

## 每一页走哪条路（这是本模块的核心判断）

| 页的情况 | 处理 | 片段 |
|---|---|---|
| `extract_text()` 有内容 | 直接产出 `TEXT` 片段 | `PAGE{page}` |
| 无文本 + **有内嵌图片** | 走 OCR（扫描件的典型形态） | `PAGE{page}` + `bbox`，`OCR` |
| 无文本 + 无图片 | 记 `UNIT_FAILED`（空白页） | 无 |

**"无文本层"不等于"扫描件"**：一个空白页也没有文本层。因此判据里
必须包含"有没有内嵌图片" —— 否则空白页会被当成扫描件送去 OCR，
每次解析都白跑一遍引擎。

**OCR 引擎不可用时**：该页记 `OCR_ENGINE_UNAVAILABLE`，
`parsed_units` 不增加。因此一份纯扫描 PDF 在没配引擎的部署上
会得到 `state='FAILED'` + 每条 errors 都写着"引擎不可用" ——
**用户看到的是"没解出来"，而不是"解出来是空的"**。这正是本包的验收要求。

## 已知边界（显式登记，不假装解决）

`page.images` 给出的是**内嵌图片**，其像素尺寸与页面尺寸不一定一致
（PDF 可以用变换矩阵缩放图片）。因此 OCR 得到的归一化 bbox 是
**相对于图片本身**的，不是相对于页面的。要让 bbox 落在页面坐标系里，
需要读页面的 CTM 并做变换 —— 本包不做（登记为待定项）。
当前结论对"跳到原文第 7 页"够用，对"精确高亮到页面上的某个矩形"不够。
"""

from __future__ import annotations

import io
import time
from typing import Final

from rednote.modules.parsing.ocr import OcrEngine, OcrEngineUnavailable
from rednote.modules.parsing.types import (
    ERROR_MALFORMED_DOCUMENT,
    ERROR_OCR_ENGINE_UNAVAILABLE,
    ERROR_UNIT_FAILED,
    ERROR_UNIT_TIMEOUT,
    EXTRACTION_OCR,
    EXTRACTION_TEXT,
    MAX_SEGMENT_CHARS,
    MAX_UNITS_PER_FILE,
    POSITION_PAGE,
    UNIT_TIME_BUDGET_SECONDS,
    ParseOutcome,
    SegmentDraft,
    SegmentPosition,
    UnitFailure,
)

# 单张内嵌图片的字节上限。超出即跳过该图片（记 failures），
# 不整个文件失败 —— 一份 200 页扫描件里有一张超大插图，
# 不该让另外 199 页也没有结果。
MAX_EMBEDDED_IMAGE_BYTES: Final[int] = 20 * 1024 * 1024


def parse_pdf(raw: bytes, *, ocr: OcrEngine) -> ParseOutcome:
    """解析 PDF。`ocr` 由调用方注入（见 `ocr.build_ocr_engine`）。"""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover - 依赖已在 pyproject 锁定
        return ParseOutcome(
            total_units=None,
            file_error_code=ERROR_MALFORMED_DOCUMENT,
            file_error_message=f"解析依赖 pypdf 未安装（{type(exc).__name__}）。",
        )

    try:
        reader = PdfReader(io.BytesIO(raw))
        # 访问 `len(reader.pages)` 才会真正解析交叉引用表 ——
        # 损坏的 PDF 在这里就抛 `PdfReadError`（本机实测：
        # "startxref not found"），而不是等到提取文本时。
        page_count = len(reader.pages)
    except PdfReadError as exc:
        return ParseOutcome(
            total_units=None,
            file_error_code=ERROR_MALFORMED_DOCUMENT,
            file_error_message=f"PDF 结构无法读取（{type(exc).__name__}）。",
        )
    except Exception as exc:  # noqa: BLE001 - 加密/截断等都会在这里冒出来
        return ParseOutcome(
            total_units=None,
            file_error_code=ERROR_MALFORMED_DOCUMENT,
            file_error_message=f"PDF 结构无法读取（{type(exc).__name__}）。",
        )

    if page_count > MAX_UNITS_PER_FILE:
        return ParseOutcome(
            total_units=None,
            file_error_code=ERROR_MALFORMED_DOCUMENT,
            file_error_message=(
                f"PDF 有 {page_count} 页，超过单文件上限 {MAX_UNITS_PER_FILE}。"
            ),
        )

    segments: list[SegmentDraft] = []
    failures: list[UnitFailure] = []
    used_ocr = False
    # **按页计数**（不是按片段）—— 一页扫描件 OCR 出 N 行仍是 1 个 unit。
    parsed_pages = 0

    for page_no in range(1, page_count + 1):
        page = reader.pages[page_no - 1]
        # **逐 unit 时间预算**（C4：所有外部调用必须有显式超时）。
        #
        # 这是**软超时**：它只能拦住"单页提取很慢"，拦不住"解析器整体挂死"
        # （后者需要子进程 + kill，**显式登记为内网补做项** ——
        # 本机无容器编排可用链，不假装做了）。
        #
        # 为什么用 `time.monotonic()` 而不是 `time.time()`：
        # 前者不受系统时钟调整影响（NTP 回拨会让"耗时"变成负数）。
        started = time.monotonic()
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - 单页坏掉不该拖垮整份文件
            failures.append(
                UnitFailure(
                    unit=page_no,
                    code=ERROR_UNIT_FAILED,
                    message=f"该页文本提取失败（{type(exc).__name__}）。",
                )
            )
            continue
        elapsed = time.monotonic() - started
        if elapsed > UNIT_TIME_BUDGET_SECONDS:
            # **超预算也要产出已拿到的文本**，但记一条 TIMEOUT 失败 ——
            # 丢弃它等于把"这页很慢"变成"这页没有内容"，下游会以为
            # 原文就是空的。记 failures 让"这页的完整性存疑"可查。
            failures.append(
                UnitFailure(
                    unit=page_no,
                    code=ERROR_UNIT_TIMEOUT,
                    message=f"该页提取耗时 {elapsed:.1f}s，超过预算"
                    f" {UNIT_TIME_BUDGET_SECONDS:.0f}s（结果可能不完整）。",
                )
            )

        if text.strip():
            segments.append(
                SegmentDraft(
                    position=SegmentPosition(kind=POSITION_PAGE, page=page_no),
                    text_content=text[:MAX_SEGMENT_CHARS],
                    extraction_kind=EXTRACTION_TEXT,
                )
            )
            parsed_pages += 1
            continue

        # 无文本层：先看有没有内嵌图片（扫描件的判据）。
        images = _page_images(page)
        if not images:
            failures.append(
                UnitFailure(
                    unit=page_no,
                    code=ERROR_UNIT_FAILED,
                    message="该页无文本层且无内嵌图片（空白页）。",
                )
            )
            continue

        ocr_segments, ocr_failures = _ocr_page(
            page_no=page_no, images=images, ocr=ocr
        )
        if ocr_segments:
            used_ocr = True
            # 该页解出了东西 → 计 1 个 unit（**不是 len(ocr_segments)**）。
            parsed_pages += 1
        segments.extend(ocr_segments)
        failures.extend(ocr_failures)

    return ParseOutcome(
        total_units=page_count,
        units_parsed=parsed_pages,
        segments=tuple(segments),
        failures=tuple(failures),
        ocr_engine=ocr.engine_name if used_ocr else "none",
    )


def _page_images(page: object) -> list[bytes]:
    """取出该页的内嵌图片字节。**读不出图片不算错误**（返回空列表）。

    理由：`page.images` 是 pypdf 的便利属性，对某些压缩方式它自己会抛错。
    一个"读不出图片"的页与"没有图片"的页，在本模块的处理路径相同
    （都记 `UNIT_FAILED`），因此不必区分。
    """
    try:
        return [image.data for image in page.images]  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return []


def _ocr_page(
    *, page_no: int, images: list[bytes], ocr: OcrEngine
) -> tuple[list[SegmentDraft], list[UnitFailure]]:
    """对一页的内嵌图片做 OCR。

    **一张图一段失败记录**：一页里有多张图时，第一张识别成功、
    第二张超限，结果是"这一页解出了一部分" —— 而不是"整页失败"。
    逐图记录才能让 errors 精确到"哪张图出了问题"。
    """
    segments: list[SegmentDraft] = []
    failures: list[UnitFailure] = []
    for image_index, image_bytes in enumerate(images, start=1):
        if len(image_bytes) > MAX_EMBEDDED_IMAGE_BYTES:
            failures.append(
                UnitFailure(
                    unit=page_no,
                    code=ERROR_UNIT_FAILED,
                    message=(
                        f"第 {image_index} 张内嵌图片超过上限"
                        f"（{len(image_bytes)} 字节）。"
                    ),
                )
            )
            continue
        try:
            result = ocr.recognize(image_bytes=image_bytes)
        except OcrEngineUnavailable as exc:
            failures.append(
                UnitFailure(
                    unit=page_no,
                    code=ERROR_OCR_ENGINE_UNAVAILABLE,
                    message=str(exc),
                )
            )
            continue
        except Exception as exc:  # noqa: BLE001 - 引擎内部错误也算这一页没解出来
            failures.append(
                UnitFailure(
                    unit=page_no,
                    code=ERROR_UNIT_FAILED,
                    message=f"OCR 引擎报错（{type(exc).__name__}）。",
                )
            )
            continue

        for line in result.lines:
            if line.text.strip() == "":
                continue
            segments.append(
                SegmentDraft(
                    position=SegmentPosition(
                        kind=POSITION_PAGE, page=page_no, bbox=line.bbox
                    ),
                    text_content=line.text[:MAX_SEGMENT_CHARS],
                    extraction_kind=EXTRACTION_OCR,
                    confidence=line.confidence,
                )
            )
        if not result.lines:
            failures.append(
                UnitFailure(
                    unit=page_no,
                    code=ERROR_UNIT_FAILED,
                    message=f"第 {image_index} 张图无可识别文字。",
                )
            )
    return segments, failures


__all__ = ["MAX_EMBEDDED_IMAGE_BYTES", "parse_pdf"]
