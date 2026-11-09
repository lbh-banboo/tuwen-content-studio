"""纯文本类解析器：`text/plain`、`text/markdown`、`text/csv`（WP17）。

**unit = 行**（含空行）。理由见 `types.py` 的 unit 语义表：
行是最自然的定位单位，且空行也算进去才能让"第 N 行"与编辑器行号一致。

---

## 编码判定（这是本模块唯一需要想清楚的地方）

纯文本文件没有自描述编码。本模块的顺序是：

1. **BOM 优先**：`utf-8-sig` 能识别的（EF BB BF）按 UTF-8 解，
   并**剥掉 BOM** —— 否则文件第一行的第一个字符会是一个不可见的
   `\\ufeff`，下游按 `startswith("# ")` 判断 markdown 标题会失败；
2. 严格 UTF-8；
3. `gb18030`（中文 Windows 导出的 txt 绝大多数是它，且它是
   GBK/GB2312 的超集，解得出就解）；
4. 全失败 → **`MALFORMED_DOCUMENT`**，不猜。

**为什么不用 `errors="replace"` 硬解**：那样任何字节序列都能"解析成功"，
产出满屏 `\\ufffd`。用户看到的是"解析成功但全是乱码"，而系统认为一切正常 ——
这正是本包验收要排除的"虚报成功"。**宁可明确失败**。
"""

from __future__ import annotations

import csv
import io
from typing import Final

from rednote.modules.parsing.types import (
    ERROR_MALFORMED_DOCUMENT,
    ERROR_UNIT_FAILED,
    EXTRACTION_TABLE,
    EXTRACTION_TEXT,
    MAX_SEGMENT_CHARS,
    POSITION_LINE,
    ParseOutcome,
    SegmentDraft,
    SegmentPosition,
    UnitFailure,
)

# 尝试顺序固定（BOM → UTF-8 → GB18030）。顺序有意义：
# 先 UTF-8 是因为它是本项目的基准编码；GB18030 放最后是因为
# 它**几乎能解出任何字节序列**，放前面会把 UTF-8 文件解成乱码。
_ENCODINGS: Final[tuple[str, ...]] = ("utf-8-sig", "utf-8", "gb18030")

# 单元格/行文本上限之外的硬上限：单文件字节上限。
# 纯文本文件是最容易"一个文件撑爆内存"的格式（100 MB 的 txt
# 解出来就是 100 MB 字符串 + 100 万行列表）。
MAX_TEXT_BYTES: Final[int] = 64 * 1024 * 1024


def decode_text(raw: bytes) -> str | None:
    """按固定顺序尝试编码。全失败返回 `None`（调用方转成 MALFORMED）。"""
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def parse_plain_text(raw: bytes, *, mime_type: str) -> ParseOutcome:
    """解析 txt / md。**unit = 行**。"""
    if len(raw) > MAX_TEXT_BYTES:
        return _too_large(len(raw))
    text = decode_text(raw)
    if text is None:
        return ParseOutcome(
            total_units=None,
            file_error_code=ERROR_MALFORMED_DOCUMENT,
            file_error_message="无法判定文本编码（已尝试 UTF-8 与 GB18030）。",
        )
    # `splitlines()` 会把结尾换行产生的空串去掉（`"a\n".split("\n")`
    # 得到 `["a", ""]`，而 `splitlines()` 得到 `["a"]`）—— 这里要的是
    # "文件里有多少行"，用 `splitlines()` 更贴近编辑器看到的行数。
    lines = text.splitlines()
    return _lines_to_outcome(lines, extraction_kind=EXTRACTION_TEXT)


def parse_csv(raw: bytes) -> ParseOutcome:
    """解析 csv。**unit = 行（含表头）**，`extraction_kind = TABLE`。

    用标准库 `csv` 而不是 `text.split(",")`：引号包裹的字段里可以有逗号与换行，
    手切会把一行切成多行、把字段切碎，而**产出的片段看起来完全正常**。

    **逐行容错**：某一行格式异常（未闭合引号）时记 `UNIT_FAILED`
    并继续 —— 一行坏掉不该让整个文件没有结果。
    """
    if len(raw) > MAX_TEXT_BYTES:
        return _too_large(len(raw))
    text = decode_text(raw)
    if text is None:
        return ParseOutcome(
            total_units=None,
            file_error_code=ERROR_MALFORMED_DOCUMENT,
            file_error_message="无法判定 CSV 文本编码（已尝试 UTF-8 与 GB18030）。",
        )

    # 先把物理行数数出来当 `total_units`，再逐行解析。
    # **为什么 total_units 用物理行数而不是 reader 的行数**：
    # 若用 reader 的产出数当总量，那么"某行格式坏了被跳过"会让
    # total_units 与 parsed_units 一起变小 —— 部分失败就被掩盖了。
    # 物理行数是**解析前就能确定的客观事实**，用它当分母才拦得住虚报。
    physical_lines = text.splitlines()
    segments: list[SegmentDraft] = []
    failures: list[UnitFailure] = []
    reader = csv.reader(io.StringIO(text))
    row_index = 0
    for row_index, row in enumerate(reader, start=1):
        joined = ",".join(row)
        if joined == "":
            # 空行：不产出片段（`SegmentDraft` 拒绝空文本），记 failures。
            failures.append(
                UnitFailure(unit=row_index, code=ERROR_UNIT_FAILED, message="空行")
            )
            continue
        segments.append(
            SegmentDraft(
                position=SegmentPosition(kind=POSITION_LINE, line=row_index),
                text_content=joined[:MAX_SEGMENT_CHARS],
                extraction_kind=EXTRACTION_TABLE,
            )
        )
    # reader 可能比物理行数少（未闭合引号会把后续行吞进同一格），
    # 此时 `total_units` 仍用物理行数 —— 差出来的部分就是"没解出来的行"。
    total = max(len(physical_lines), row_index)
    return ParseOutcome(
        total_units=total, segments=tuple(segments), failures=tuple(failures)
    )


def _lines_to_outcome(lines: list[str], *, extraction_kind: str) -> ParseOutcome:
    """行 → 片段。空行记 `UNIT_FAILED`（**不产出空片段**）。"""
    segments: list[SegmentDraft] = []
    failures: list[UnitFailure] = []
    for line_no, line in enumerate(lines, start=1):
        if line.strip() == "":
            failures.append(
                UnitFailure(
                    unit=line_no, code=ERROR_UNIT_FAILED, message="空白行（无内容）"
                )
            )
            continue
        segments.append(
            SegmentDraft(
                position=SegmentPosition(kind=POSITION_LINE, line=line_no),
                text_content=line[:MAX_SEGMENT_CHARS],
                extraction_kind=extraction_kind,
            )
        )
    return ParseOutcome(
        total_units=len(lines), segments=tuple(segments), failures=tuple(failures)
    )


def _too_large(byte_size: int) -> ParseOutcome:
    return ParseOutcome(
        total_units=None,
        file_error_code=ERROR_MALFORMED_DOCUMENT,
        file_error_message=f"文件超过纯文本解析上限（{byte_size} 字节）。",
    )


__all__ = ["MAX_TEXT_BYTES", "decode_text", "parse_csv", "parse_plain_text"]
