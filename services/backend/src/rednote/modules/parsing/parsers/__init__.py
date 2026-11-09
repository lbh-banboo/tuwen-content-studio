"""解析器注册表：按（MIME，扩展名）选解析器（WP17）。

**为什么不用"一个 if/elif 长链"**：分发规则要被三处使用 ——
service（选解析器）、测试（断言"每个承诺格式都有解析器"）、
证据脚本（核对清单）。写成注册表后"承诺格式都有实现"这件事
**可以被机械验证**（见 `unsupported_formats()` 与测试）。

---

## 分发的判据顺序（与 WP12 的 `classify_file` 刻意相反）

WP12 判"是否承诺可解析"时是**扩展名与 MIME 取并集**（宁可假阳性）。
本模块是**取交集之后仍要能选出一个解析器**，因此顺序是：

1. **扩展名优先**（它来自原始文件名，是用户意图的直接表达）；
2. 扩展名认不出时看 MIME（浏览器给的 MIME 常不准，但它比"什么都没有"强）；
3. 都认不出 → `UNSUPPORTED_FORMAT`。

**为什么扩展名优先而不是 MIME 优先**：macOS 上 `.csv` 常被报成
`application/vnd.ms-excel`（那是 xls 的 MIME），若 MIME 优先，
一个 CSV 会被交给 xlsx 解析器，然后以"xlsx 结构无法读取"失败 ——
用户看到的是"文件损坏"，而实际只是类型判定顺序错了。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from rednote.modules.parsing.ocr import OcrEngine
from rednote.modules.parsing.parsers.image import parse_image
from rednote.modules.parsing.parsers.office import parse_docx, parse_xlsx
from rednote.modules.parsing.parsers.pdf import parse_pdf
from rednote.modules.parsing.parsers.text import parse_csv, parse_plain_text
from rednote.modules.parsing.types import (
    ERROR_UNSUPPORTED_FORMAT,
    ParseOutcome,
)

# 解析器签名：字节 + MIME + OCR 引擎 → 结果。
# `mime_type` 传进去是为了让文本解析器能区分 csv 与 txt（见下）。
ParserFn = Callable[..., ParseOutcome]

# 扩展名 → 解析器名（**解析器名**而不是函数：注册表与实现分开，
# 便于测试断言"某个格式由某个解析器处理"）。
_EXTENSION_ROUTES: Final[dict[str, str]] = {
    "txt": "text",
    "md": "text",
    "markdown": "text",
    "csv": "csv",
    "docx": "docx",
    "xlsx": "xlsx",
    "pdf": "pdf",
    "jpg": "image",
    "jpeg": "image",
    "png": "image",
    "webp": "image",
}

# MIME → 解析器名（**仅在扩展名认不出时使用**）。
_MIME_ROUTES: Final[dict[str, str]] = {
    "text/plain": "text",
    "text/markdown": "text",
    "text/csv": "csv",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "image/jpeg": "image",
    "image/png": "image",
    "image/webp": "image",
}


def extension_of(original_name: str) -> str:
    """取小写扩展名（不含点）。没有扩展名返回空串。"""
    if "." not in original_name:
        return ""
    return original_name.rsplit(".", 1)[-1].strip().lower()


def route_for(*, original_name: str, mime_type: str) -> str | None:
    """选出解析器名。认不出返回 `None`。"""
    extension = extension_of(original_name)
    if extension in _EXTENSION_ROUTES:
        return _EXTENSION_ROUTES[extension]
    normalized_mime = (mime_type or "").strip().lower()
    return _MIME_ROUTES.get(normalized_mime)


def supported_formats() -> frozenset[str]:
    """本包**承诺**能处理的扩展名（测试与证据脚本用）。"""
    return frozenset(_EXTENSION_ROUTES)


def unsupported_formats() -> ParseOutcome:
    """统一的"不支持"结果。

    `total_units=None` → `derive_state` 必然给出 FAILED。
    这条路径上不存在"猜一个解析器试试"的分支 ——
    猜错会把 xlsx 交给 docx 解析器，然后以"结构损坏"失败，
    用户会去检查一个其实没坏的文件。
    """
    return ParseOutcome(
        total_units=None,
        file_error_code=ERROR_UNSUPPORTED_FORMAT,
        file_error_message="该格式不在本系统承诺可解析的清单内。",
    )


def parse(
    raw: bytes,
    *,
    original_name: str,
    mime_type: str,
    ocr: OcrEngine,
) -> ParseOutcome:
    """分发并解析。**本模块唯一的对外入口。**"""
    route = route_for(original_name=original_name, mime_type=mime_type)
    if route is None:
        return unsupported_formats()

    if route == "text":
        return parse_plain_text(raw, mime_type=mime_type)
    if route == "csv":
        return parse_csv(raw)
    if route == "docx":
        return parse_docx(raw)
    if route == "xlsx":
        return parse_xlsx(raw)
    if route == "pdf":
        return parse_pdf(raw, ocr=ocr)
    if route == "image":
        return parse_image(raw, ocr=ocr)
    # 注册表里出现了没有实现的解析器名 —— 属程序缺陷，显式失败而不是猜。
    raise RuntimeError(f"注册表里的解析器 {route!r} 没有实现")


__all__ = [
    "ParserFn",
    "extension_of",
    "parse",
    "route_for",
    "supported_formats",
    "unsupported_formats",
]
