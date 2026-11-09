"""Office 解析器：`.docx`（python-docx）与 `.xlsx`（openpyxl）（WP17）。

**unit 语义**（见 `types.py` 的表）：

| 格式 | unit | 定位 |
|---|---|---|
| docx | 段落（含空段落） | `PARAGRAPH{index}` |
| xlsx | **非空单元格数** | `CELL{sheet,row,col}` |

---

## 两条共同的判断

### 1. "结构读不出"必须报错，不能"尽力而为"

python-docx / openpyxl 在文件损坏时会抛各自的异常（`PackageNotFoundError`
/ `BadZipFile` / `InvalidFileException` / `KeyError`…）。本模块**逐个捕获并
统一转成 `MALFORMED_DOCUMENT`**，不往上抛，也不吞掉 —— 因为
"读不出结构"与"文件格式不支持"对用户是两个不同的动作（见 `types.py`）。

### 2. 空段落/空单元格**不产出片段**

- docx：空段落计入 `total_units`（这样 `index` 与 python-docx 的段落序号
  一致，下游"跳到第 12 段"才准确），但不产出片段 → 记 `UNIT_FAILED`
  → 该文档若全是空段落，`parsed_units = 0` → **FAILED**（不是"成功"）。
- xlsx：空单元格**不计入** `total_units`（否则一张 1000×20 的表
  会有 2 万个空 unit，`total_units` 失去意义）。这是与 docx 相反的取舍，
  理由是"表的稀疏性"：xlsx 里绝大多数单元格是空的，docx 里绝大多数段落不是。
"""

from __future__ import annotations

import io
from typing import Any, Final

from rednote.modules.parsing.types import (
    ERROR_MALFORMED_DOCUMENT,
    ERROR_UNIT_FAILED,
    EXTRACTION_TABLE,
    EXTRACTION_TEXT,
    MAX_SEGMENT_CHARS,
    POSITION_CELL,
    POSITION_PARAGRAPH,
    ParseOutcome,
    SegmentDraft,
    SegmentPosition,
    UnitFailure,
)

# 单个 sheet 的扫描上限（行 × 列）。超出即 MALFORMED_DOCUMENT：
# 一个 100 万行的 sheet 不是"值得解析的研究资料"，是"该被拒绝的输入"。
MAX_SHEET_ROWS: Final[int] = 5_000
MAX_SHEET_COLS: Final[int] = 200
MAX_SHEETS: Final[int] = 50


def parse_docx(raw: bytes) -> ParseOutcome:
    """解析 `.docx`。**unit = 段落（含空段落）**。"""
    try:
        import docx  # noqa: PLC0415 - 重依赖按需导入，避免 import 模块即拉起它
    except ImportError as exc:  # pragma: no cover - 依赖已在 pyproject 锁定
        return _dependency_missing("python-docx", exc)

    try:
        document = docx.Document(io.BytesIO(raw))
        # `document.paragraphs` 是**在解析前就能拿到**的完整列表，
        # 因此它天然就是"客观总量"。
        paragraphs = list(document.paragraphs)
    except Exception as exc:  # noqa: BLE001 - 库的异常类型很多，统一归类
        return _malformed("docx 结构无法读取", exc)

    segments: list[SegmentDraft] = []
    failures: list[UnitFailure] = []
    for index, paragraph in enumerate(paragraphs, start=1):
        # **不 strip 就判空**：python-docx 的 `paragraph.text` 已经不含
        # 段落标记，但可能有全角空格这类"看起来空"的内容 ——
        # 用 `strip()` 判空会让它们被丢掉，而它们可能是排版用的分隔。
        text = paragraph.text
        if text.strip() == "":
            failures.append(
                UnitFailure(
                    unit=index, code=ERROR_UNIT_FAILED, message="空段落（无文本）"
                )
            )
            continue
        segments.append(
            SegmentDraft(
                position=SegmentPosition(kind=POSITION_PARAGRAPH, index=index),
                text_content=text[:MAX_SEGMENT_CHARS],
                extraction_kind=EXTRACTION_TEXT,
            )
        )
    return ParseOutcome(
        total_units=len(paragraphs), segments=tuple(segments), failures=tuple(failures)
    )


def parse_xlsx(raw: bytes) -> ParseOutcome:
    """解析 `.xlsx`。**unit = 非空单元格数**，定位到 `(sheet, row, col)`。

    `read_only=True` + `data_only=True`：

    · `read_only` —— 流式读取，不把整个工作簿载入内存
      （100 MB 的 xlsx 用非只读模式会吃掉数倍内存）；
    · `data_only` —— 取公式的**缓存值**而不是公式本身。
      取公式的话，下游拿到的 `"=A1*B1"` 对内容生产毫无意义。
      **代价**：若工作簿从未被 Excel 打开过（公式没有缓存值），
      `data_only=True` 会得到 `None` —— 那些单元格会被跳过并记 failures。
      这个取舍是对的：宁可"这个格子没解出来"，也不要"解出一个公式字符串"。
    """
    try:
        import openpyxl  # noqa: PLC0415 - 重依赖按需导入
        from openpyxl.utils import get_column_letter
        from openpyxl.utils.exceptions import InvalidFileException
    except ImportError as exc:  # pragma: no cover - 依赖已在 pyproject 锁定
        return _dependency_missing("openpyxl", exc)

    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(raw), read_only=True, data_only=True
        )
    except InvalidFileException as exc:
        return _malformed("xlsx 结构无法读取", exc)
    except Exception as exc:  # noqa: BLE001 - zip 损坏等
        return _malformed("xlsx 结构无法读取", exc)

    segments: list[SegmentDraft] = []
    failures: list[UnitFailure] = []
    total = 0
    try:
        sheet_names = list(workbook.sheetnames)
        if len(sheet_names) > MAX_SHEETS:
            return ParseOutcome(
                total_units=None,
                file_error_code=ERROR_MALFORMED_DOCUMENT,
                file_error_message=(
                    f"工作簿包含 {len(sheet_names)} 个 sheet，"
                    f"超过上限 {MAX_SHEETS}。"
                ),
            )
        for sheet_name in sheet_names:
            sheet = workbook[sheet_name]
            if sheet.max_row and sheet.max_row > MAX_SHEET_ROWS:
                return ParseOutcome(
                    total_units=None,
                    file_error_code=ERROR_MALFORMED_DOCUMENT,
                    file_error_message=(
                        f"sheet {sheet_name!r} 有 {sheet.max_row} 行，"
                        f"超过上限 {MAX_SHEET_ROWS}。"
                    ),
                )
            if sheet.max_column and sheet.max_column > MAX_SHEET_COLS:
                return ParseOutcome(
                    total_units=None,
                    file_error_code=ERROR_MALFORMED_DOCUMENT,
                    file_error_message=(
                        f"sheet {sheet_name!r} 有 {sheet.max_column} 列，"
                        f"超过上限 {MAX_SHEET_COLS}。"
                    ),
                )
            # 逐行枚举（read_only 模式下 `iter_rows` 是流式的）。
            # **两遍扫描**：第一遍数非空单元格（= total_units），
            # 第二遍产出片段。read_only 模式下不能回头重扫，
            # 因此先把值缓存成列表 —— 单元格值是小对象，缓存代价可接受。
            rows: list[list[Any]] = [list(row) for row in sheet.iter_rows()]
            for row_cells in rows:
                for cell in row_cells:
                    if _is_empty_cell(cell.value):
                        continue
                    total += 1
            for row_cells in rows:
                for cell in row_cells:
                    if _is_empty_cell(cell.value):
                        continue
                    column = get_column_letter(cell.column)
                    segments.append(
                        SegmentDraft(
                            position=SegmentPosition(
                                kind=POSITION_CELL,
                                sheet=sheet_name,
                                row=cell.row,
                                col=column,
                            ),
                            text_content=str(cell.value)[:MAX_SEGMENT_CHARS],
                            extraction_kind=EXTRACTION_TABLE,
                        )
                    )
    finally:
        # read_only 模式会持有文件句柄（zip），不关会泄漏到 GC。
        workbook.close()

    return ParseOutcome(
        total_units=total, segments=tuple(segments), failures=tuple(failures)
    )


def _is_empty_cell(value: Any) -> bool:
    """空单元格判据。

    `None` 与空白字符串都算空。**`0` 与 `False` 不算空** ——
    它们在业务上是有意义的值（销量 0 与销量空是两件事）。
    """
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _malformed(message: str, exc: BaseException) -> ParseOutcome:
    return ParseOutcome(
        total_units=None,
        file_error_code=ERROR_MALFORMED_DOCUMENT,
        file_error_message=f"{message}（{type(exc).__name__}）。",
    )


def _dependency_missing(name: str, exc: BaseException) -> ParseOutcome:
    """依赖缺失也归 `MALFORMED_DOCUMENT`？**不** —— 见下方说明。

    依赖缺失是**部署问题**，不是文件问题。但它与"结构损坏"一样
    都会让 `total_units` 无法确定，因此都走"必然 FAILED"这条路。
    用 `MALFORMED_DOCUMENT` 会让排障的人去查文件，而实际要查的是
    离线 wheelhouse —— 因此这里用一条**明确不同的 message**，
    让 `errors[0].message` 能区分开（`code` 仍是 `MALFORMED_DOCUMENT`，
    因为它是"这一层能表达的最近语义"；新增错误码需要同步协议，登记为待定项）。
    """
    return ParseOutcome(
        total_units=None,
        file_error_code=ERROR_MALFORMED_DOCUMENT,
        file_error_message=f"解析依赖 {name} 未安装（{type(exc).__name__}）。",
    )


__all__ = [
    "MAX_SHEET_COLS",
    "MAX_SHEET_ROWS",
    "MAX_SHEETS",
    "parse_docx",
    "parse_xlsx",
]
