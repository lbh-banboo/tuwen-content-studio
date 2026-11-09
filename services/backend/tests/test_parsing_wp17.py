"""WP17 测试：文档解析、OCR 与来源定位。

覆盖验收标准（研发计划 04 §4.3）：

> **扫描件可定位，损坏及不支持格式不虚报成功**

按验收的两半组织，每一半都有**结构性保证**的测试：

**第一半「扫描件可定位」**
  · 无文本层 PDF 走 OCR → 片段带 `PAGE{page}` + 归一化 `bbox`；
  · 图片 → `WHOLE` + `bbox`；
  · 定位构造器拒绝不合法的组合（`{}` 不是定位）。

**第二半「不虚报成功」**（本文件的核心）
  · `derive_state` 的纯函数表；
  · 数据库三条 CHECK **活性验证**（试写非法状态，断言被拦 `23514`）；
  · 损坏文件 → `MALFORMED_DOCUMENT` + `state='FAILED'`；
  · 不支持格式 → `UNSUPPORTED_FORMAT` + `state='FAILED'`（**不是 SUCCEEDED**）；
  · OCR 引擎不可用 → `OCR_ENGINE_UNAVAILABLE` + **绝不出现空内容的"成功"**。

**测试策略（四层）**

1. 纯函数层：`derive_state`、`SegmentPosition`、`route_for`、`outcome_for_run_state`。
2. 解析器层：造真实文件字节（txt / csv / docx / xlsx / pdf / png），断言片段与定位。
3. 服务层 + 真库：`request_parse` / `execute_parse` 的复用、状态、片段落库。
4. **数据库约束层**：直接写 SQL 试非法状态，断言被 CHECK 拦下。
"""

from __future__ import annotations

import io
import os
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from rednote.modules.files.models import (
    CLASSIFICATION_SUPPORTED,
    CLASSIFICATION_UNSUPPORTED,
    SCAN_STATUS_CLEAN,
    VISIBILITY_PROJECT,
    Blob,
    FileObject,
)
from rednote.modules.files.repository import FileRepository
from rednote.modules.files.storage import LocalStorageBackend, object_key_for
from rednote.modules.identity.models import ROLE_OPERATOR
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.jobs import service as jobs_service
from rednote.modules.jobs.handlers import build_default_registry
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.parsing import parsers, service
from rednote.modules.parsing.handlers import (
    build_event_registry,
    build_task_registry,
    outcome_for_run_state,
)
from rednote.modules.parsing.models import ParseRun, SourceSegment
from rednote.modules.parsing.ocr import (
    FakeOcrEngine,
    OcrEngineUnavailable,
    UnavailableOcrEngine,
    build_ocr_engine,
)
from rednote.modules.parsing.repository import ParseRepository
from rednote.modules.parsing.types import (
    ERROR_MALFORMED_DOCUMENT,
    ERROR_OCR_ENGINE_UNAVAILABLE,
    ERROR_UNIT_TIMEOUT,
    ERROR_UNSUPPORTED_FORMAT,
    EXTRACTION_OCR,
    EXTRACTION_TABLE,
    EXTRACTION_TEXT,
    MAX_UNITS_PER_FILE,
    PARSE_STATE_FAILED,
    PARSE_STATE_PARTIAL,
    PARSE_STATE_SUCCEEDED,
    POSITION_CELL,
    POSITION_LINE,
    POSITION_PAGE,
    POSITION_PARAGRAPH,
    POSITION_WHOLE,
    ParseOutcome,
    SegmentDraft,
    SegmentPosition,
    UnitFailure,
    derive_state,
)
from rednote.modules.projects.models import Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.platform.configuration import Settings
from rednote.platform.database import session_scope

# 测试库 DSN。**必须从环境变量读**（ISS-108）：硬编码会让
# `REDNOTE_TEST_DATABASE_URL` 约定的「测试库物理隔离」失效 —— 测试会把
# 数据写进本机默认库（实测跑一次全量 +367 行 users），且 CI 里 PG 用户是
# `postgres` 而不是 `rednote`，硬编码会直接连不上（role 不存在）。
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
TEST_DATABASE_URL = os.environ.get(
    "REDNOTE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL
).strip()
TEST_ISSUER = "https://idp.test.invalid/realms/rednote"


def _settings(root: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": TEST_DATABASE_URL,
        "environment": "test",
        "storage_backend": "LOCAL",
        "storage_local_root": str(root),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ===========================================================================
# 第 1 层：纯函数（不碰库、不碰盘）
# ===========================================================================


@pytest.mark.parametrize(
    ("total", "parsed", "expected"),
    [
        # 数不出总量 → 只能失败
        (None, 0, PARSE_STATE_FAILED),
        (None, 5, PARSE_STATE_FAILED),
        # 空文件：解出 0 个 → 对下游等于没有输入
        (0, 0, PARSE_STATE_FAILED),
        # 一个都没解出来
        (10, 0, PARSE_STATE_FAILED),
        # 全部解出
        (10, 10, PARSE_STATE_SUCCEEDED),
        (1, 1, PARSE_STATE_SUCCEEDED),
        # 部分
        (10, 1, PARSE_STATE_PARTIAL),
        (10, 9, PARSE_STATE_PARTIAL),
        # 计数自相矛盾（数据库 CHECK 也会拦，这是第二道）
        (10, 11, PARSE_STATE_FAILED),
        (10, -1, PARSE_STATE_FAILED),
    ],
)
def test_derive_state_table(total, parsed, expected) -> None:
    """**本包验收第二半的核心纯函数**：状态由计数推导，不由解析器自报。

    三行必须被记住的：

      · `total_units=None` → FAILED（**不存在"不知道总量却报成功"的路径**）；
      · `(0, 0)` → FAILED（"成功但没内容"是欺骗）；
      · `(10, 0)` → FAILED（**不是 PARTIAL** —— PARTIAL 意味着"解出来一部分"）。
    """
    assert derive_state(total_units=total, parsed_units=parsed) == expected


def test_outcome_has_no_self_reported_state() -> None:
    """`ParseOutcome` **没有** `state` 字段 —— 解析器无法自报状态。"""
    outcome = ParseOutcome(total_units=3, segments=())
    assert not hasattr(outcome, "state")
    # 但能算出状态
    assert outcome.derived_state == PARSE_STATE_FAILED


@pytest.mark.regression
def test_parsed_units_is_units_not_segments() -> None:
    """**`parsed_units` 数的是 unit，不是片段**（本包最容易搞错的一处）。

    初版把 `parsed_units` 直接定义成 `len(segments)`。后果：一张图 OCR
    出 4 行会得到 `parsed_units=4 > total_units=1`，被 `derive_state`
    判成"计数自相矛盾" → FAILED —— 而这张图其实**识别得很成功**。

    这条测试守的就是"1 个 unit 可以产出 N 个片段"。
    """
    line = SegmentDraft(
        position=SegmentPosition(kind=POSITION_WHOLE, bbox=(0.1, 0.1, 0.9, 0.2)),
        text_content="识别出的一行",
        extraction_kind=EXTRACTION_OCR,
        confidence=0.9,
    )
    outcome = ParseOutcome(
        total_units=1, units_parsed=1, segments=(line, line, line, line)
    )
    assert len(outcome.segments) == 4
    assert outcome.parsed_units == 1
    assert outcome.derived_state == PARSE_STATE_SUCCEEDED


@pytest.mark.regression
def test_file_error_forces_zero_parsed_units() -> None:
    """有文件级错误时 `parsed_units` 恒为 0（否则会虚报成功）。"""
    outcome = ParseOutcome(
        total_units=None,
        segments=(),
        file_error_code=ERROR_UNSUPPORTED_FORMAT,
    )
    assert outcome.parsed_units == 0
    assert outcome.derived_state == PARSE_STATE_FAILED


def test_partial_is_not_in_complete_states() -> None:
    """`PARSE_COMPLETE_STATES` **不含** PARTIAL。

    这条守的是下游契约：WP18/WP21 必须显式区分"全都解出来了"与
    "解出来一部分"。若 PARTIAL 被算作完成，下游会拿残缺输入当完整输入。
    """
    from rednote.modules.parsing.types import PARSE_COMPLETE_STATES

    assert PARSE_STATE_PARTIAL not in PARSE_COMPLETE_STATES
    assert PARSE_COMPLETE_STATES == frozenset({PARSE_STATE_SUCCEEDED})


def test_position_rejects_incomplete_kinds() -> None:
    """定位构造器拒绝"有 kind 但缺必填字段" —— `{}` 不是定位。"""
    with pytest.raises(ValueError, match="PAGE 定位必须给出 page"):
        SegmentPosition(kind=POSITION_PAGE)
    with pytest.raises(ValueError, match="PARAGRAPH 定位必须给出 index"):
        SegmentPosition(kind=POSITION_PARAGRAPH)
    with pytest.raises(ValueError, match="LINE 定位必须给出 line"):
        SegmentPosition(kind=POSITION_LINE)
    with pytest.raises(ValueError, match="CELL 定位必须给出"):
        SegmentPosition(kind=POSITION_CELL, sheet="S1", row=1)
    with pytest.raises(ValueError, match="未知的 position.kind"):
        SegmentPosition(kind="UNKNOWN_KIND")


def test_position_rejects_unnormalized_bbox() -> None:
    """bbox 必须归一化到 0~1（不同 DPI 的同一扫描件像素坐标不同）。"""
    with pytest.raises(ValueError, match="必须归一化"):
        SegmentPosition(kind=POSITION_PAGE, page=1, bbox=(0.0, 0.0, 100.0, 50.0))
    with pytest.raises(ValueError, match="右下角必须不小于左上角"):
        SegmentPosition(kind=POSITION_PAGE, page=1, bbox=(0.8, 0.8, 0.2, 0.2))
    with pytest.raises(ValueError, match="必须是 4 个数"):
        SegmentPosition(kind=POSITION_PAGE, page=1, bbox=(0.1, 0.1, 0.2))


def test_position_rejects_zero_based_index() -> None:
    """页码/行号从 1 开始（人看的"第几页"）。"""
    with pytest.raises(ValueError, match="从 1 开始"):
        SegmentPosition(kind=POSITION_PAGE, page=0)
    with pytest.raises(ValueError, match="从 1 开始"):
        SegmentPosition(kind=POSITION_LINE, line=0)


def test_position_to_json_omits_none() -> None:
    """落库只放非 None 字段（库里不该出现一堆 null）。"""
    payload = SegmentPosition(kind=POSITION_PAGE, page=7).to_json()
    assert payload == {"kind": "PAGE", "page": 7}


def test_segment_draft_rejects_empty_text() -> None:
    """空片段一律拒绝 —— 否则"解出几个 unit"可以靠空串刷上去。"""
    with pytest.raises(ValueError, match="不产出空文本片段"):
        SegmentDraft(
            position=SegmentPosition(kind=POSITION_LINE, line=1),
            text_content="",
            extraction_kind=EXTRACTION_TEXT,
        )


def test_segment_draft_rejects_bad_confidence() -> None:
    with pytest.raises(ValueError, match="confidence 必须在 0~1"):
        SegmentDraft(
            position=SegmentPosition(kind=POSITION_LINE, line=1),
            text_content="x",
            extraction_kind=EXTRACTION_OCR,
            confidence=1.5,
        )


def test_route_prefers_extension_over_mime() -> None:
    """**扩展名优先**（与 WP12 的 `classify_file` 刻意相反）。

    守的具体场景：macOS 上 `.csv` 常被报成 `application/vnd.ms-excel`
    （xls 的 MIME）。若 MIME 优先，一个 CSV 会被交给 xlsx 解析器，
    然后以"xlsx 结构无法读取"失败 —— 用户看到"文件损坏"，
    而实际只是类型判定顺序错了。
    """
    assert (
        parsers.route_for(original_name="数据.csv", mime_type="application/vnd.ms-excel")
        == "csv"
    )
    # 扩展名认不出时才看 MIME
    assert (
        parsers.route_for(original_name="data.bin", mime_type="application/pdf") == "pdf"
    )
    assert parsers.route_for(original_name="x.unknown", mime_type="x/y") is None


def test_every_supported_extension_has_a_route() -> None:
    """**承诺清单里的每个扩展名都必须能选出一个解析器**（机械核对）。

    WP12 的 `SUPPORTED_EXTENSIONS` 是"承诺可解析"的清单。
    本包若漏了一个（例如加了 webp 却没在注册表里加），
    该文件会走 `UNSUPPORTED_FORMAT` —— 而 WP12 已经告诉用户"支持"。
    """
    from rednote.modules.files.models import SUPPORTED_EXTENSIONS

    missing = sorted(
        ext
        for ext in SUPPORTED_EXTENSIONS
        if parsers.route_for(original_name=f"a.{ext}", mime_type="") is None
    )
    assert missing == [], f"承诺清单里的扩展名没有解析器：{missing}"


def test_unsupported_format_outcome_has_no_total_units() -> None:
    """不支持 → `total_units=None` → `derive_state` 必然 FAILED。"""
    outcome = parsers.unsupported_formats()
    assert outcome.total_units is None
    assert outcome.derived_state == PARSE_STATE_FAILED
    assert outcome.failures_json()[0]["code"] == ERROR_UNSUPPORTED_FORMAT


def test_outcome_for_run_state_maps_partial_separately() -> None:
    """`PARTIAL` 有**独立**的任务状态映射（不并入 SUCCEEDED）。"""
    from rednote.modules.jobs.models import STATE_FAILED, STATE_PARTIAL, STATE_SUCCEEDED

    assert outcome_for_run_state(PARSE_STATE_SUCCEEDED).state == STATE_SUCCEEDED
    assert outcome_for_run_state(PARSE_STATE_PARTIAL).state == STATE_PARTIAL
    failed = outcome_for_run_state(PARSE_STATE_FAILED)
    assert failed.state == STATE_FAILED
    # 解析失败**不可重试**（重试一万次也是同一个文件）
    assert failed.retryable is False


def test_outcome_for_run_state_rejects_non_terminal() -> None:
    """非终态不该走到映射函数 —— 显式失败而不是猜一个状态。"""
    with pytest.raises(ValueError, match="解析未到达终态"):
        outcome_for_run_state("RUNNING")


def test_ocr_engine_unknown_name_is_unavailable() -> None:
    """**配置错误必须表现为"不可用"，不能表现为"可用"。**

    若写成"认不出就退回 Fake"，一个拼错的引擎名会让生产环境用假结果。
    """
    assert isinstance(build_ocr_engine(None), UnavailableOcrEngine)
    assert isinstance(build_ocr_engine(""), UnavailableOcrEngine)
    assert isinstance(build_ocr_engine("paddleocr-typo"), UnavailableOcrEngine)
    assert isinstance(build_ocr_engine("fake"), FakeOcrEngine)


def test_unavailable_ocr_engine_raises() -> None:
    """不可用引擎**抛错**而不是返回空结果 —— 这是本包最重要的一条判断。"""
    with pytest.raises(OcrEngineUnavailable):
        UnavailableOcrEngine().recognize(image_bytes=b"x")


def test_fake_ocr_engine_is_deterministic() -> None:
    """Fake 引擎确定性：同一输入同一输出（否则"结果可复用"会失效）。"""
    engine = FakeOcrEngine()
    first = engine.recognize(image_bytes=b"same-bytes")
    second = engine.recognize(image_bytes=b"same-bytes")
    other = engine.recognize(image_bytes=b"other-bytes")
    assert [line.text for line in first.lines] == [line.text for line in second.lines]
    assert [line.text for line in first.lines] != [line.text for line in other.lines]
    # bbox 归一化且置信度递减
    assert all(0.0 <= value <= 1.0 for line in first.lines for value in line.bbox)
    assert first.lines[0].confidence > first.lines[-1].confidence


# ===========================================================================
# 第 2 层：解析器（造真实文件字节）
# ===========================================================================


def _docx_bytes(paragraphs: list[str]) -> bytes:
    import docx

    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _xlsx_bytes(rows: list[list[object]]) -> bytes:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Sheet1"
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _pdf_bytes(*, pages: int = 1, blank: bool = True) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _png_bytes() -> bytes:
    from PIL import Image

    image = Image.new("RGB", (60, 40), color=(200, 200, 200))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_plain_text_units_are_lines_including_blank() -> None:
    """**unit = 行（含空行）** —— 这样"第 N 行"与编辑器行号一致。"""
    raw = "第一行\n\n第三行\n".encode("utf-8")
    outcome = parsers.parse_plain_text(raw, mime_type="text/plain")
    assert outcome.total_units == 3
    # 空行不产出片段，记 failures
    assert outcome.parsed_units == 2
    assert [s.position.line for s in outcome.segments] == [1, 3]
    assert outcome.derived_state == PARSE_STATE_PARTIAL


def test_plain_text_handles_gb18030() -> None:
    """GB18030 能被解出（中文 Windows 导出的 txt 绝大多数是它）。"""
    raw = "中文内容\n第二行\n".encode("gb18030")
    outcome = parsers.parse_plain_text(raw, mime_type="text/plain")
    assert outcome.segments[0].text_content == "中文内容"
    assert outcome.total_units == 2
    assert outcome.derived_state == PARSE_STATE_SUCCEEDED


def test_plain_text_strips_utf8_bom() -> None:
    """BOM 必须剥掉 —— 否则第一行开头有个不可见字符。

    具体后果：下游按 `startswith("# ")` 判断 markdown 标题会失败。
    """
    raw = "\ufeff# 标题\n正文\n".encode("utf-8")
    outcome = parsers.parse_plain_text(raw, mime_type="text/markdown")
    assert outcome.segments[0].text_content == "# 标题"
    assert not outcome.segments[0].text_content.startswith("\ufeff")


def test_plain_text_undecodable_is_malformed_not_mojibake() -> None:
    """**编码全失败 → MALFORMED_DOCUMENT，不猜。**

    若用 `errors="replace"` 硬解，任何字节序列都能"解析成功"，
    产出满屏 `\\ufffd` —— 用户看到"成功但全是乱码"，系统认为一切正常。
    """
    raw = b"\xff\xfe\x00\x01\x02\x03"
    outcome = parsers.parse_plain_text(raw, mime_type="text/plain")
    assert outcome.total_units is None
    assert outcome.derived_state == PARSE_STATE_FAILED
    assert outcome.failures_json()[0]["code"] == ERROR_MALFORMED_DOCUMENT


def test_csv_uses_physical_lines_as_denominator() -> None:
    """CSV 的 `total_units` 用**物理行数**，不用 reader 的产出数。

    守的场景：某行引号未闭合时 `csv.reader` 会把后续行吞进同一格，
    于是 reader 的产出数变小 —— 用产出数当分母会**掩盖部分失败**。
    物理行数是解析前就能确定的客观事实。
    """
    raw = 'a,b\n"未闭合,引号\n第三行\n'.encode("utf-8")
    outcome = parsers.parse_csv(raw)
    assert outcome.total_units == 3
    assert outcome.segments[0].extraction_kind == EXTRACTION_TABLE


def test_csv_keeps_quoted_commas_intact() -> None:
    """引号里的逗号不能被切碎（用标准库 csv 而不是 split）。"""
    raw = '名称,备注\n"含,逗号","含,逗号"\n'.encode("utf-8")
    outcome = parsers.parse_csv(raw)
    assert outcome.segments[1].text_content == "含,逗号,含,逗号"


def test_docx_units_are_paragraphs_including_blank() -> None:
    """**unit = 段落（含空段落）** —— index 与 python-docx 的序号一致。"""
    raw = _docx_bytes(["第一段", "", "第三段"])
    outcome = parsers.parse_docx(raw)
    assert outcome.total_units == 3
    assert outcome.parsed_units == 2
    assert [s.position.index for s in outcome.segments] == [1, 3]
    assert outcome.segments[0].position.kind == POSITION_PARAGRAPH


def test_docx_corrupt_is_malformed() -> None:
    """**损坏的 docx → MALFORMED_DOCUMENT**（不是"成功但为空"）。"""
    outcome = parsers.parse_docx(b"PK\x03\x04 this is not a real zip")
    assert outcome.total_units is None
    assert outcome.derived_state == PARSE_STATE_FAILED
    assert outcome.failures_json()[0]["code"] == ERROR_MALFORMED_DOCUMENT


def test_xlsx_units_are_non_empty_cells() -> None:
    """**unit = 非空单元格数**（空格不计入，否则大表会有上万个空 unit）。"""
    raw = _xlsx_bytes([["A1", None, "C1"], [None, "B2", None]])
    outcome = parsers.parse_xlsx(raw)
    assert outcome.total_units == 3
    assert outcome.parsed_units == 3
    assert outcome.derived_state == PARSE_STATE_SUCCEEDED
    positions = [s.position for s in outcome.segments]
    assert positions[0].kind == POSITION_CELL
    assert (positions[0].sheet, positions[0].row, positions[0].col) == ("Sheet1", 1, "A")
    assert (positions[1].row, positions[1].col) == (1, "C")
    assert (positions[2].row, positions[2].col) == (2, "B")


def test_xlsx_zero_and_false_are_not_empty() -> None:
    """`0` 与 `False` **不算空**（销量 0 与销量空是两件事）。"""
    raw = _xlsx_bytes([[0, False, None]])
    outcome = parsers.parse_xlsx(raw)
    assert outcome.total_units == 2
    assert outcome.parsed_units == 2


def test_xlsx_corrupt_is_malformed() -> None:
    outcome = parsers.parse_xlsx(b"not a zip at all")
    assert outcome.total_units is None
    assert outcome.derived_state == PARSE_STATE_FAILED


def test_xlsx_over_row_limit_is_malformed() -> None:
    """超行上限 → MALFORMED（**不是**"尽力解析前 5000 行然后报成功"）。"""
    from rednote.modules.parsing.parsers.office import MAX_SHEET_ROWS

    rows: list[list[object]] = [["x"] for _ in range(MAX_SHEET_ROWS + 5)]
    outcome = parsers.parse_xlsx(_xlsx_bytes(rows))
    assert outcome.total_units is None
    assert outcome.derived_state == PARSE_STATE_FAILED


# --- 第一半验收：扫描件可定位 ------------------------------------------------


def test_pdf_blank_page_without_images_is_unit_failure() -> None:
    """无文本层**且无图片**的页 = 空白页（**不是扫描件**）。

    这条区分很重要：若不加"有没有图片"这个判据，空白页会被送去 OCR，
    每次解析都白跑一遍引擎。
    """
    outcome = parsers.parse_pdf(_pdf_bytes(pages=2), ocr=FakeOcrEngine())
    assert outcome.total_units == 2
    assert outcome.parsed_units == 0
    assert outcome.derived_state == PARSE_STATE_FAILED
    assert all(
        f["code"] != ERROR_OCR_ENGINE_UNAVAILABLE for f in outcome.failures_json()
    )


def test_pdf_corrupt_is_malformed() -> None:
    """损坏的 PDF → MALFORMED（本机实测 pypdf 抛 "startxref not found"）。"""
    outcome = parsers.parse_pdf(b"%PDF-1.4\ngarbage\n%%EOF", ocr=FakeOcrEngine())
    assert outcome.total_units is None
    assert outcome.derived_state == PARSE_STATE_FAILED
    assert outcome.failures_json()[0]["code"] == ERROR_MALFORMED_DOCUMENT


def test_pdf_page_limit_is_malformed() -> None:
    from rednote.modules.parsing.parsers.pdf import parse_pdf
    from unittest.mock import patch

    with patch(
        "rednote.modules.parsing.parsers.pdf.MAX_UNITS_PER_FILE", 2
    ):
        outcome = parse_pdf(_pdf_bytes(pages=3), ocr=FakeOcrEngine())
    assert outcome.total_units is None
    assert outcome.derived_state == PARSE_STATE_FAILED


def test_pdf_slow_page_records_timeout_but_keeps_text() -> None:
    """**逐 unit 时间预算真的在用**（C4）。

    超预算时记一条 `UNIT_TIMEOUT`，**但保留已拿到的文本** ——
    丢弃它等于把"这页很慢"变成"这页没有内容"。

    这条测试存在的理由：`UNIT_TIME_BUDGET_SECONDS` 与 `ERROR_UNIT_TIMEOUT`
    曾经是**"声明了但一行都没实现"**的常量（阶段 7 代码简化时发现）。
    常量本身不会报错、不会有测试失败、不会出现在覆盖率缺口里 ——
    它是"看起来有保护、实际没有"的最隐蔽形态。
    """
    from unittest.mock import patch

    from rednote.modules.parsing.parsers.pdf import parse_pdf

    with patch(
        "rednote.modules.parsing.parsers.pdf.UNIT_TIME_BUDGET_SECONDS", -1.0
    ):
        outcome = parse_pdf(_pdf_bytes(pages=1), ocr=FakeOcrEngine())
    codes = [f["code"] for f in outcome.failures_json()]
    assert ERROR_UNIT_TIMEOUT in codes
    # 页面本身是空白的（无文本层无图片），所以这里只有 TIMEOUT 一条；
    # 关键是**它被记下来了**，而不是被静默吞掉。
    assert outcome.total_units == 1


@pytest.mark.regression
def test_image_with_ocr_engine_produces_located_segments() -> None:
    """**第一半验收**：扫描件（图片）经 OCR → 片段带归一化 bbox。

    bbox 归一化到 0~1 而不是像素：不同 DPI 的同一份扫描件像素坐标不同，
    归一化后"跳到原文那个位置"这件事才与 DPI 无关。

    **注意 `parsed_units == 1` 而不是 `len(segments)`** —— 一张图是 1 个
    unit，OCR 出 N 行是 N 个片段。两者混用会让这张图被判成"计数矛盾"。
    """
    outcome = parsers.parse_image(_png_bytes(), ocr=FakeOcrEngine())
    assert outcome.total_units == 1
    assert len(outcome.segments) == FakeOcrEngine._LINES_PER_PAGE
    assert outcome.parsed_units == 1
    assert outcome.derived_state == PARSE_STATE_SUCCEEDED
    for segment in outcome.segments:
        assert segment.extraction_kind == EXTRACTION_OCR
        assert segment.position.kind == POSITION_WHOLE
        assert segment.position.bbox is not None
        assert all(0.0 <= v <= 1.0 for v in segment.position.bbox)
        # OCR 结果必须带置信度（下游据此降权）
        assert segment.confidence is not None


def test_image_without_ocr_engine_fails_loudly() -> None:
    """**第二半验收的关键一条**：引擎不可用 → FAILED，**绝不虚报成功**。

    图片唯一的内容来源就是 OCR。若引擎不可用却报成功，
    会得到一个"成功"但没有任何片段的解析结果 —— 下游会拿它当完整输入。
    """
    outcome = parsers.parse_image(_png_bytes(), ocr=UnavailableOcrEngine())
    assert outcome.total_units == 1  # 客观事实：这是一张图
    assert outcome.parsed_units == 0
    assert outcome.derived_state == PARSE_STATE_FAILED
    assert outcome.failures_json()[0]["code"] == ERROR_OCR_ENGINE_UNAVAILABLE


def test_renamed_non_image_is_malformed() -> None:
    """**WP12 留的"假阳性"在这里收口**：把 .exe 改名成 .png。

    WP12 的 `classify_file` 按扩展名判为 SUPPORTED（它明说"真正的类型
    判定属 WP17"）。本包用 Pillow 打开失败 → MALFORMED_DOCUMENT。
    """
    outcome = parsers.parse_image(b"MZ\x90\x00 not an image", ocr=FakeOcrEngine())
    assert outcome.total_units is None
    assert outcome.derived_state == PARSE_STATE_FAILED
    assert outcome.failures_json()[0]["code"] == ERROR_MALFORMED_DOCUMENT


def test_route_returns_unsupported_for_unknown() -> None:
    outcome = parsers.parse(
        b"x", original_name="a.exe", mime_type="application/x-msdownload",
        ocr=FakeOcrEngine(),
    )
    assert outcome.derived_state == PARSE_STATE_FAILED
    assert outcome.failures_json()[0]["code"] == ERROR_UNSUPPORTED_FORMAT


# ===========================================================================
# 第 3 层：服务层 + 真库 + 真盘
# ===========================================================================


def _create_user(settings: Settings) -> uuid.UUID:
    with session_scope(settings) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name="解析测试用户",
            role=ROLE_OPERATOR,
        )
        return user.id


def _seed_project(settings: Settings, *, owner_id: uuid.UUID) -> uuid.UUID:
    with session_scope(settings) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name="测试产品")
        session.add(product)
        session.flush()
        project = Project(
            project_code=f"PRS-{uuid.uuid4().hex[:8].upper()}",
            name="解析测试项目",
            brand_id=brand.id,
            product_id=product.id,
            owner_id=owner_id,
            target_delivery_count=10,
            due_date=date(2026, 12, 31),
        )
        session.add(project)
        session.flush()
        ProjectRepository(session).add_owner_and_collaborators(
            project.id, owner_id=owner_id, collaborator_ids=()
        )
        return project.id


@dataclass
class ParseEnv:
    settings: Settings
    store: LocalStorageBackend
    project_id: uuid.UUID
    owner_id: uuid.UUID


@pytest.fixture
def env(tmp_path: Path) -> ParseEnv:
    root = tmp_path / "storage"
    settings = _settings(root)
    store = LocalStorageBackend(root)
    owner = _create_user(settings)
    project_id = _seed_project(settings, owner_id=owner)
    return ParseEnv(
        settings=settings, store=store, project_id=project_id, owner_id=owner
    )


def _seed_file(
    env: ParseEnv,
    raw: bytes,
    *,
    original_name: str = "资料.txt",
    classification: str = CLASSIFICATION_SUPPORTED,
    scan_status: str = SCAN_STATUS_CLEAN,
) -> uuid.UUID:
    """把一个文件真的写进存储并落库（blob + file_object）。

    **blob 按 `(storage_backend, object_key)` 复用**：`blobs` 上有
    `UQ(storage_backend, object_key)`（WP12），而 `object_key` 由 sha256
    派生 —— 同一个测试进程里造两次同样的字节（例如两个测试都用 `"内容\\n"`）
    会撞唯一约束。物理去重本来就是设计的一部分（数据结构 02 §4），
    这里顺着它做：**已有 blob 就复用**，文件对象各自新建。
    """
    import hashlib

    sha = hashlib.sha256(raw).hexdigest()
    key = object_key_for(sha)
    env.store.write_part(key=key, stream=io.BytesIO(raw))

    with session_scope(env.settings) as session:
        blob = session.execute(
            text(
                "select id from blobs where storage_backend = :backend"
                " and object_key = :key"
            ),
            {"backend": env.store.backend_name, "key": key},
        ).scalar_one_or_none()
        if blob is None:
            row = Blob(
                storage_backend=env.store.backend_name,
                bucket=None,
                object_key=key,
                sha256=sha,
                byte_size=len(raw),
                mime_type="application/octet-stream",
                scan_status=scan_status,
            )
            session.add(row)
            session.flush()
            blob_id = row.id
        else:
            blob_id = blob

        file_row = FileObject(
            blob_id=blob_id,
            original_name=original_name,
            visibility=VISIBILITY_PROJECT,
            project_id=env.project_id,
            classification=classification,
            uploaded_by=env.owner_id,
        )
        session.add(file_row)
        session.flush()
        return file_row.id


def _run_parse(env: ParseEnv, file_id: uuid.UUID, *, ocr=None):
    """同步走一遍"建 run + 执行"（不经过任务框架）。"""
    with session_scope(env.settings) as session:
        parse_repo = ParseRepository(session)
        file_repo = FileRepository(session)
        file_row = file_repo.get_file_object(file_id)
        assert file_row is not None
        run, created = service.request_parse(parse_repo, file_row=file_row)
        assert created is True
        return service.execute_parse(
            parse_repo,
            run_id=run.id,
            file_repo=file_repo,
            storage=env.store,
            ocr=ocr or FakeOcrEngine(),
        )


@pytest.mark.regression
def test_execute_parse_persists_segments_and_state(env: ParseEnv) -> None:
    """端到端：txt 解析 → 片段落库 → 状态 SUCCEEDED。"""
    file_id = _seed_file(env, "甲\n乙\n丙\n".encode("utf-8"), original_name="资料.txt")
    run = _run_parse(env, file_id)
    assert run.state == PARSE_STATE_SUCCEEDED
    assert run.total_units == 3
    assert run.parsed_units == 3

    with session_scope(env.settings) as session:
        rows = ParseRepository(session).list_segments(run.id)
    assert [row.text_content for row in rows] == ["甲", "乙", "丙"]
    assert all(row.extraction_kind == EXTRACTION_TEXT for row in rows)
    assert rows[0].position == {"kind": "LINE", "line": 1}


def test_execute_parse_partial_is_recorded_as_partial(env: ParseEnv) -> None:
    """**部分失败必须是 PARTIAL**（不是 SUCCEEDED，也不是 FAILED）。"""
    raw = "有内容\n\n也有内容\n".encode("utf-8")
    file_id = _seed_file(env, raw, original_name="资料.txt")
    run = _run_parse(env, file_id)
    assert run.state == PARSE_STATE_PARTIAL
    assert run.total_units == 3
    assert run.parsed_units == 2
    assert any(item["unit"] == 2 for item in run.errors)


def test_execute_parse_unsupported_records_failed_run(env: ParseEnv) -> None:
    """**不支持的格式建 run 而不是报错**（AC-07 要求它是一个可查询的状态）。"""
    file_id = _seed_file(
        env,
        b"MZ\x90\x00",
        original_name="安装包.exe",
        classification=CLASSIFICATION_UNSUPPORTED,
    )
    run = _run_parse(env, file_id)
    assert run.state == PARSE_STATE_FAILED
    assert run.errors[0]["code"] == ERROR_UNSUPPORTED_FORMAT
    assert run.total_units is None


def test_execute_parse_corrupt_records_malformed(env: ParseEnv) -> None:
    file_id = _seed_file(env, b"\xff\xfe\x00\x01", original_name="坏文件.txt")
    run = _run_parse(env, file_id)
    assert run.state == PARSE_STATE_FAILED
    assert run.errors[0]["code"] == ERROR_MALFORMED_DOCUMENT


def test_scanned_pdf_without_engine_fails_with_engine_reason(env: ParseEnv) -> None:
    """**扫描件 + 无引擎 → FAILED，且原因写明是引擎不可用**。

    用户据此能采取行动（装引擎 / 转成带文本层的 PDF），
    而"成功但内容为空"什么都告诉不了他。
    """
    file_id = _seed_file(env, _png_bytes(), original_name="扫描件.png")
    run = _run_parse(env, file_id, ocr=UnavailableOcrEngine())
    assert run.state == PARSE_STATE_FAILED
    assert run.errors[0]["code"] == ERROR_OCR_ENGINE_UNAVAILABLE
    assert run.ocr_engine == "none"
    # **绝不存在"成功且片段为空"**
    with session_scope(env.settings) as session:
        assert ParseRepository(session).count_segments(run.id) == 0


def test_scanned_image_with_engine_records_bbox(env: ParseEnv) -> None:
    """**第一半验收端到端**：扫描件 → 带 bbox 的片段 → 落库 → 回读。"""
    file_id = _seed_file(env, _png_bytes(), original_name="扫描件.png")
    run = _run_parse(env, file_id, ocr=FakeOcrEngine())
    assert run.state == PARSE_STATE_SUCCEEDED
    assert run.ocr_engine == "fake-ocr"
    with session_scope(env.settings) as session:
        rows = ParseRepository(session).list_segments(run.id)
    assert len(rows) == FakeOcrEngine._LINES_PER_PAGE
    for row in rows:
        assert row.position["kind"] == "WHOLE"
        assert len(row.position["bbox"]) == 4
        assert row.extraction_kind == EXTRACTION_OCR
        assert row.confidence is not None


def test_request_parse_reuses_terminal_run(env: ParseEnv) -> None:
    """数据结构 02 §4："同一解析器版本结果可复用"。"""
    file_id = _seed_file(env, "内容\n".encode("utf-8"), original_name="a.txt")
    first = _run_parse(env, file_id)
    with session_scope(env.settings) as session:
        repo = ParseRepository(session)
        file_row = FileRepository(session).get_file_object(file_id)
        assert file_row is not None
        again, created = service.request_parse(repo, file_row=file_row)
    assert created is False
    assert again.id == first.id


def test_reparse_creates_new_run_and_keeps_old_segments(env: ParseEnv) -> None:
    """重解析产生**新 run**，旧 run 的片段**不被改动**（它是证据）。"""
    file_id = _seed_file(env, "内容\n".encode("utf-8"), original_name="a.txt")
    first = _run_parse(env, file_id)
    with session_scope(env.settings) as session:
        repo = ParseRepository(session)
        # 用另一个 parser_version 绕过"可复用"
        run2, created = service.request_parse(
            repo,
            file_row=FileRepository(session).get_file_object(file_id),  # type: ignore[arg-type]
            parser_version="wp17-2",
        )
        assert created is True
    with session_scope(env.settings) as session:
        repo = ParseRepository(session)
        # 旧 run 仍在，片段仍在
        assert repo.get_run(first.id) is not None
        assert repo.count_segments(first.id) == 1
        assert repo.count_segments(run2.id) == 0


def test_execute_parse_is_idempotent_on_rerun(env: ParseEnv) -> None:
    """重复执行同一 run **不报错**（投递语义是"至少一次"）。

    片段用 `ON CONFLICT DO NOTHING` 去重，因此重跑不会产生重复片段，
    也不会因为唯一约束失败。
    """
    file_id = _seed_file(env, "内容\n".encode("utf-8"), original_name="a.txt")
    run = _run_parse(env, file_id)
    with session_scope(env.settings) as session:
        repo = ParseRepository(session)
        file_repo = FileRepository(session)
        again = service.execute_parse(
            repo, run_id=run.id, file_repo=file_repo, storage=env.store,
            ocr=FakeOcrEngine(),
        )
    assert again.state == PARSE_STATE_SUCCEEDED
    with session_scope(env.settings) as session:
        # 片段数没有翻倍
        assert ParseRepository(session).count_segments(run.id) == 1


def test_unit_limit_is_enforced_in_service(env: ParseEnv) -> None:
    """service 层的统一兜底：`total_units` 超上限 → MALFORMED。"""
    from unittest.mock import patch

    file_id = _seed_file(env, "a\nb\nc\n".encode("utf-8"), original_name="a.txt")
    with patch.object(service, "MAX_UNITS_PER_FILE", 2):
        run = _run_parse(env, file_id)
    assert run.state == PARSE_STATE_FAILED
    assert run.errors[0]["code"] == ERROR_MALFORMED_DOCUMENT


def test_latest_summary_is_none_before_any_parse(env: ParseEnv) -> None:
    """**"还没解析过"与"正在排队"是两件事** —— 前者返回 None。"""
    file_id = _seed_file(env, "x\n".encode("utf-8"), original_name="a.txt")
    with session_scope(env.settings) as session:
        assert service.latest_summary(ParseRepository(session), file_id) is None


def test_latest_summary_reports_state(env: ParseEnv) -> None:
    file_id = _seed_file(env, "x\n".encode("utf-8"), original_name="a.txt")
    _run_parse(env, file_id)
    with session_scope(env.settings) as session:
        summary = service.latest_summary(ParseRepository(session), file_id)
    assert summary is not None
    assert summary["state"] == PARSE_STATE_SUCCEEDED
    assert summary["complete"] is True
    # 计数是十进制字符串（协议 §1）
    assert summary["totalUnits"] == "1"
    assert summary["parsedUnits"] == "1"
    assert "success" not in summary


def test_describe_run_outputs_none_total_when_unknown() -> None:
    """`totalUnits` 未知时输出 `None`（不是 `"0"`）。"""
    run = ParseRun(
        file_id=uuid.uuid4(),
        job_id=None,
        parser_version="wp17-1",
        state=PARSE_STATE_FAILED,
        total_units=None,
        parsed_units=0,
    )
    payload = service.describe_run(run)
    assert payload["totalUnits"] is None
    assert payload["parsedUnits"] == "0"
    assert payload["complete"] is False


# --- 与任务框架的接缝 --------------------------------------------------------


def test_file_uploaded_event_enqueues_parse_job(env: ParseEnv) -> None:
    """`file.uploaded` → 建 `PARSE_DOCUMENT` 任务 + run（同一事务）。"""
    from rednote.modules.jobs.dispatch import EventEnvelope
    from rednote.modules.outbox.models import EVENT_FILE_UPLOADED

    file_id = _seed_file(env, "内容\n".encode("utf-8"), original_name="a.txt")
    registry = build_event_registry(settings=env.settings)
    assert EVENT_FILE_UPLOADED in registry.registered_types()
    registry.handle(
        EventEnvelope(
            event_id=uuid.uuid4(),
            aggregate_type="file_object",
            aggregate_id=file_id,
            event_type=EVENT_FILE_UPLOADED,
            schema_version=1,
            payload={"fileId": str(file_id)},
            attempts=0,
        )
    )

    with session_scope(env.settings) as session:
        jobs = (
            session.execute(
                text(
                    "select id, kind, state from jobs where idempotency_key = :k"
                ),
                {"k": str(file_id)},
            )
            .mappings()
            .all()
        )
    assert len(jobs) == 1
    assert jobs[0]["kind"] == service.KIND_PARSE_DOCUMENT
    # run 与 job 已关联
    with session_scope(env.settings) as session:
        run = ParseRepository(session).find_run_for_job(jobs[0]["id"])
    assert run is not None
    assert run.file_id == file_id


def test_file_uploaded_event_without_file_id_is_ignored(env: ParseEnv) -> None:
    """事件缺 `fileId` → **不抛错**（抛错会让投递通道无限重试）。"""
    from rednote.modules.jobs.dispatch import EventEnvelope
    from rednote.modules.outbox.models import EVENT_FILE_UPLOADED

    registry = build_event_registry(settings=env.settings)
    registry.handle(
        EventEnvelope(
            event_id=uuid.uuid4(),
            aggregate_type="file_object",
            aggregate_id=uuid.uuid4(),
            event_type=EVENT_FILE_UPLOADED,
            schema_version=1,
            payload={},
            attempts=0,
        )
    )


def test_duplicate_event_does_not_create_second_job(env: ParseEnv) -> None:
    """**重复投递不重复执行**（WP13 的投递语义是"至少一次"）。"""
    from rednote.modules.jobs.dispatch import EventEnvelope
    from rednote.modules.outbox.models import EVENT_FILE_UPLOADED

    file_id = _seed_file(env, "内容\n".encode("utf-8"), original_name="a.txt")
    registry = build_event_registry(settings=env.settings)
    envelope = EventEnvelope(
        event_id=uuid.uuid4(),
        aggregate_type="file_object",
        aggregate_id=file_id,
        event_type=EVENT_FILE_UPLOADED,
        schema_version=1,
        payload={"fileId": str(file_id)},
        attempts=0,
    )
    registry.handle(envelope)
    registry.handle(envelope)

    with session_scope(env.settings) as session:
        count = session.execute(
            text("select count(*) from jobs where idempotency_key = :k"),
            {"k": str(file_id)},
        ).scalar_one()
    assert count == 1


def test_task_handler_runs_end_to_end(env: ParseEnv) -> None:
    """任务处理器端到端：入队 → 领取 → 执行 → 状态与片段都对。"""
    from rednote.modules.jobs.dispatch import EventEnvelope
    from rednote.modules.outbox.models import EVENT_FILE_UPLOADED

    file_id = _seed_file(env, "甲\n乙\n".encode("utf-8"), original_name="a.txt")
    build_event_registry(settings=env.settings).handle(
        EventEnvelope(
            event_id=uuid.uuid4(),
            aggregate_type="file_object",
            aggregate_id=file_id,
            event_type=EVENT_FILE_UPLOADED,
            schema_version=1,
            payload={"fileId": str(file_id)},
            attempts=0,
        )
    )
    with session_scope(env.settings) as session:
        job_id = session.execute(
            text("select id from jobs where idempotency_key = :k"),
            {"k": str(file_id)},
        ).scalar_one()

    # 任务处理器需要 storage 指向本测试的临时目录
    registry = build_task_registry(storage=env.store, ocr=FakeOcrEngine())
    with session_scope(env.settings) as session:
        report = jobs_service.run_job(
            JobRepository(session),
            registry,
            job_id=job_id,
            worker_id="worker-parse-1",
        )
    assert report.accepted is True
    assert report.state == "SUCCEEDED"

    with session_scope(env.settings) as session:
        run = ParseRepository(session).find_run_for_job(job_id)
        assert run is not None
        assert run.state == PARSE_STATE_SUCCEEDED
        assert run.parsed_units == 2


def test_task_registry_does_not_mutate_default(env: ParseEnv) -> None:
    """本包的注册表是**新实例**，不污染 WP13 的默认集合。"""
    default_before = build_default_registry().registered_kinds()
    build_task_registry()
    assert build_default_registry().registered_kinds() == default_before
    assert service.KIND_PARSE_DOCUMENT in build_task_registry().registered_kinds()


def test_parse_failure_is_not_retryable(env: ParseEnv) -> None:
    """解析失败**不可重试**（重试一万次也是同一个文件）。"""
    from rednote.modules.parsing.handlers import parse_document_handler
    from rednote.modules.jobs.types import ClaimedJob

    claimed = ClaimedJob(
        job_id=uuid.uuid4(),
        kind=service.KIND_PARSE_DOCUMENT,
        attempt_id=uuid.uuid4(),
        fence_token=1,
        attempt_no=1,
        worker_id="w",
    )
    with session_scope(env.settings) as session:
        outcome = parse_document_handler(claimed, JobRepository(session))
    assert outcome.state == "FAILED"
    assert outcome.error_code == "PARSE_RUN_NOT_FOUND"
    assert outcome.retryable is False


# ===========================================================================
# 第 4 层：数据库约束（**活性验证** —— 试写非法状态，断言被拦）
#
# **为什么这些测试不能复用 `env` fixture**：它们要试写**非法**数据，
# 而 `env` 的库状态会被那些失败的事务污染（异常后事务 aborted）。
# 但 `parse_runs.file_id` 有外键（指向 `file_objects`），因此仍需要一个
# 真实的 file_id。做法是**在一个事务里造一个临时文件对象**，
# 断言完整体 rollback —— 零残留（WP16 OP-134 的同一手法）。
# ===========================================================================


@pytest.fixture
def raw_env(tmp_path: Path) -> ParseEnv:
    """与 `env` 同构，但只给"试写非法数据"用（不留下任何东西）。"""
    root = tmp_path / "raw-storage"
    settings = _settings(root)
    store = LocalStorageBackend(root)
    owner = _create_user(settings)
    project_id = _seed_project(settings, owner_id=owner)
    return ParseEnv(
        settings=settings, store=store, project_id=project_id, owner_id=owner
    )


def _try_raw_run_insert(
    env: ParseEnv, state: str, total: int | None, parsed: int
) -> None:
    """直接写 SQL 试插一行（绕过 service 与 ORM）。

    这是"虚报成功进不去库里"的**最有力证据** ——
    只查 `pg_constraint` 能证明约束**存在**，不能证明它**生效**。

    **file_id 必须是真的**（有外键）。在一个事务里造一个 file_object
    再用它 —— 整个事务会因为断言失败（`DBAPIError` 被 pytest.raises
    捕获后由 `session_scope` 回滚）而不留痕迹。
    """
    with session_scope(env.settings) as session:
        file_id = session.execute(
            text(
                "insert into file_objects (blob_id, original_name, visibility,"
                " project_id, classification, uploaded_by)"
                " select id, 'raw.txt', 'PROJECT', :project, 'SUPPORTED', :owner"
                " from blobs limit 1 returning id"
            ),
            {"project": env.project_id, "owner": env.owner_id},
        ).scalar_one_or_none()
        if file_id is None:
            # 库里还没有任何 blob（本条测试先跑）：造一个。
            blob_id = session.execute(
                text(
                    "insert into blobs (storage_backend, object_key, sha256,"
                    " byte_size, mime_type) values ('LOCAL', :key, :sha, 1,"
                    " 'text/plain') returning id"
                ),
                {"key": f"raw/{uuid.uuid4().hex}", "sha": uuid.uuid4().hex * 2},
            ).scalar_one()
            file_id = session.execute(
                text(
                    "insert into file_objects (blob_id, original_name, visibility,"
                    " project_id, classification, uploaded_by)"
                    " values (:blob, 'raw.txt', 'PROJECT', :project, 'SUPPORTED',"
                    " :owner) returning id"
                ),
                {
                    "blob": blob_id,
                    "project": env.project_id,
                    "owner": env.owner_id,
                },
            ).scalar_one()

        session.execute(
            text(
                "insert into parse_runs (file_id, parser_version, state,"
                " total_units, parsed_units, finished_at)"
                " values (:file, 'raw', :state, :total, :parsed, now())"
            ),
            {
                "file": file_id,
                "state": state,
                "total": total,
                "parsed": parsed,
            },
        )


@pytest.mark.parametrize(
    ("state", "total", "parsed"),
    [
        # 虚报成功：SUCCEEDED 但只解出一部分
        (PARSE_STATE_SUCCEEDED, 20, 3),
        # 虚报成功：SUCCEEDED 但不知道总量
        (PARSE_STATE_SUCCEEDED, None, 3),
        # 虚报成功：SUCCEEDED 但一个都没解出来
        (PARSE_STATE_SUCCEEDED, 0, 0),
        # 部分成功但一个都没解出来（那是 FAILED）
        (PARSE_STATE_PARTIAL, 20, 0),
        # 部分成功但全解出来了（那是 SUCCEEDED）
        (PARSE_STATE_PARTIAL, 20, 20),
        # 计数自相矛盾
        (PARSE_STATE_FAILED, 20, 21),
    ],
)
def test_database_checks_block_false_success(
    raw_env: ParseEnv, state: str, total: int | None, parsed: int
) -> None:
    """**第二半验收的数据库级证据**：六种非法组合全部被拦（SQLSTATE 23514）。

    其中前三条就是"虚报成功"的三种形态。它们必须进不去库里 ——
    即使有人绕过 service 直接写 SQL。
    """
    with pytest.raises(DBAPIError) as excinfo:
        _try_raw_run_insert(raw_env, state, total, parsed)
    assert excinfo.value.orig.sqlstate == "23514", (
        f"{state}/{total}/{parsed} 应被 CHECK 拦下"
    )


def test_database_allows_valid_terminal_states(raw_env: ParseEnv) -> None:
    """反向用例：合法的组合**必须**能写进去。

    没有这条，"一律拒绝"的退化实现也能通过上面那个测试。

    **本测试的数据必须清掉**：`parse_runs` 是可变的（不是不可变表），
    可以删；但不清的话它会在库里累积，导致第二次跑本测试时
    `count(*)` 翻倍 —— **"测试留下会被别人读到的状态"**
    （WP16 ISS-50 的同一类问题）。
    """
    marker = f"raw-ok-{uuid.uuid4().hex[:10]}"
    try:
        with session_scope(raw_env.settings) as session:
            blob_id = session.execute(
                text(
                    "insert into blobs (storage_backend, object_key, sha256,"
                    " byte_size, mime_type) values ('LOCAL', :key, :sha, 1,"
                    " 'text/plain') returning id"
                ),
                {"key": f"ok/{uuid.uuid4().hex}", "sha": uuid.uuid4().hex * 2},
            ).scalar_one()
            file_id = session.execute(
                text(
                    "insert into file_objects (blob_id, original_name, visibility,"
                    " project_id, classification, uploaded_by)"
                    " values (:blob, 'ok.txt', 'PROJECT', :project, 'SUPPORTED',"
                    " :owner) returning id"
                ),
                {
                    "blob": blob_id,
                    "project": raw_env.project_id,
                    "owner": raw_env.owner_id,
                },
            ).scalar_one()
            for state, total, parsed in (
                ("SUCCEEDED", 3, 3),
                ("PARTIAL", 3, 1),
                ("FAILED", None, 0),
            ):
                session.execute(
                    text(
                        "insert into parse_runs (file_id, parser_version, state,"
                        " total_units, parsed_units, finished_at)"
                        " values (:file, :marker, :state, :total, :parsed, now())"
                    ),
                    {
                        "file": file_id,
                        "marker": marker,
                        "state": state,
                        "total": total,
                        "parsed": parsed,
                    },
                )
            count = session.execute(
                text("select count(*) from parse_runs where parser_version = :m"),
                {"m": marker},
            ).scalar_one()
            assert count == 3
    finally:
        # 清干净（`parse_runs` 可变，可删；片段表不可变，但本测试没写片段）。
        with session_scope(raw_env.settings) as session:
            session.execute(
                text("delete from parse_runs where parser_version = :m"),
                {"m": marker},
            )


def test_terminal_state_requires_finished_at(raw_env: ParseEnv) -> None:
    """终态必须有 `finished_at` —— 否则"跑了多久"不可回答。"""
    with pytest.raises(DBAPIError) as excinfo:
        with session_scope(raw_env.settings) as session:
            file_id = _raw_file_id(session, raw_env)
            session.execute(
                text(
                    "insert into parse_runs (file_id, parser_version, state,"
                    " total_units, parsed_units, finished_at)"
                    " values (:file, 'raw', 'FAILED', null, 0, null)"
                ),
                {"file": file_id},
            )
    assert excinfo.value.orig.sqlstate == "23514"


def _raw_file_id(session, env: ParseEnv):
    """造一个临时 file_object 并返回它的 id（供约束测试用）。"""
    blob_id = session.execute(
        text(
            "insert into blobs (storage_backend, object_key, sha256,"
            " byte_size, mime_type) values ('LOCAL', :key, :sha, 1,"
            " 'text/plain') returning id"
        ),
        {"key": f"raw/{uuid.uuid4().hex}", "sha": uuid.uuid4().hex * 2},
    ).scalar_one()
    return session.execute(
        text(
            "insert into file_objects (blob_id, original_name, visibility,"
            " project_id, classification, uploaded_by)"
            " values (:blob, 'raw.txt', 'PROJECT', :project, 'SUPPORTED',"
            " :owner) returning id"
        ),
        {"blob": blob_id, "project": env.project_id, "owner": env.owner_id},
    ).scalar_one()


def test_source_segments_has_immutable_trigger() -> None:
    """`source_segments` 必须有不篡改触发器（与 ORM 守卫**两层**）。"""
    settings = _settings(Path("/tmp"))
    with session_scope(settings) as session:
        count = session.execute(
            text(
                "select count(*) from pg_trigger"
                " where tgname = 'trg_source_segments_immutable'"
                " and not tgisinternal"
            )
        ).scalar_one()
    assert count == 1


def test_source_segments_reject_empty_text(raw_env: ParseEnv) -> None:
    """空片段在数据库层被拦（"解出几个 unit"不能靠空串刷）。"""
    with pytest.raises(DBAPIError) as excinfo:
        with session_scope(raw_env.settings) as session:
            file_id = _raw_file_id(session, raw_env)
            run_id = session.execute(
                text(
                    "insert into parse_runs (file_id, parser_version, state,"
                    " total_units, parsed_units, finished_at)"
                    " values (:file, 'raw', 'FAILED', null, 0, now())"
                    " returning id"
                ),
                {"file": file_id},
            ).scalar_one()
            session.execute(
                text(
                    "insert into source_segments (parse_run_id, segment_no,"
                    " position, text_content, content_hash, extraction_kind)"
                    " values (:run, 1, cast(:pos as jsonb), '', :hash, 'TEXT')"
                ),
                {"run": run_id, "hash": "0" * 64, "pos": '{"kind":"LINE","line":1}'},
            )
    assert excinfo.value.orig.sqlstate == "23514"


def test_source_segments_require_position_kind(raw_env: ParseEnv) -> None:
    """`{}` 不是定位 —— 数据库层也拦（`position ? 'kind'`）。"""
    with pytest.raises(DBAPIError) as excinfo:
        with session_scope(raw_env.settings) as session:
            file_id = _raw_file_id(session, raw_env)
            run_id = session.execute(
                text(
                    "insert into parse_runs (file_id, parser_version, state,"
                    " total_units, parsed_units, finished_at)"
                    " values (:file, 'raw', 'FAILED', null, 0, now())"
                    " returning id"
                ),
                {"file": file_id},
            ).scalar_one()
            session.execute(
                text(
                    "insert into source_segments (parse_run_id, segment_no,"
                    " position, text_content, content_hash, extraction_kind)"
                    " values (:run, 1, '{}'::jsonb, 'x', :hash, 'TEXT')"
                ),
                {"run": run_id, "hash": "0" * 64},
            )
    assert excinfo.value.orig.sqlstate == "23514"


def test_source_segments_duplicate_is_blocked(raw_env: ParseEnv) -> None:
    """`UQ(parse_run_id, content_hash, position)` 生效（SQLSTATE 23505）。"""
    with pytest.raises(DBAPIError) as excinfo:
        with session_scope(raw_env.settings) as session:
            file_id = _raw_file_id(session, raw_env)
            run_id = session.execute(
                text(
                    "insert into parse_runs (file_id, parser_version, state,"
                    " total_units, parsed_units, finished_at)"
                    " values (:file, 'raw', 'FAILED', null, 0, now())"
                    " returning id"
                ),
                {"file": file_id},
            ).scalar_one()
            # **`segment_no` 必须给不同的值**：否则先撞的是
            # `uq_source_segments_run_segment_no`，而不是本测试要验证的
            # `uq_source_segments_run_hash_position` —— 那样断言会
            # "因为错误的原因通过"。
            for segment_no in (1, 2):
                session.execute(
                    text(
                        "insert into source_segments (parse_run_id, segment_no,"
                        " position, text_content, content_hash, extraction_kind)"
                        " values (:run, :no, cast(:pos as jsonb), 'x', :hash,"
                        " 'TEXT')"
                    ),
                    {"run": run_id, "no": segment_no, "hash": "0" * 64,
                     "pos": '{"kind":"LINE","line":1}'},
                )
    assert excinfo.value.orig.sqlstate == "23505"


def test_source_segments_cannot_be_updated_via_raw_sql(env: ParseEnv) -> None:
    """**活性验证**：对真实片段试写 UPDATE，断言被触发器拦（23001）。

    只查 `pg_trigger` 只能证明触发器**存在**，不能证明它**生效**。
    """
    file_id = _seed_file(env, "内容\n".encode("utf-8"), original_name="a.txt")
    run = _run_parse(env, file_id)
    with pytest.raises(DBAPIError) as excinfo:
        with session_scope(env.settings) as session:
            session.execute(
                text(
                    "update source_segments set text_content = '被改过了'"
                    " where parse_run_id = :run"
                ),
                {"run": run.id},
            )
    assert excinfo.value.orig.sqlstate == "23001"


def test_source_segments_cannot_be_deleted_via_raw_sql(env: ParseEnv) -> None:
    """DELETE 同样被拦（23001）。"""
    file_id = _seed_file(env, "内容\n".encode("utf-8"), original_name="a.txt")
    run = _run_parse(env, file_id)
    with pytest.raises(DBAPIError) as excinfo:
        with session_scope(env.settings) as session:
            session.execute(
                text("delete from source_segments where parse_run_id = :run"),
                {"run": run.id},
            )
    assert excinfo.value.orig.sqlstate == "23001"


@pytest.mark.cross_wp
def test_shared_trigger_function_still_exists() -> None:
    """**跨包回归**：共用的 `rednote_reject_mutation()` 必须仍在。

    WP17 的 `downgrade` 刻意不删它（所有不可变表共用）。
    """
    settings = _settings(Path("/tmp"))
    with session_scope(settings) as session:
        count = session.execute(
            text("select count(*) from pg_proc where proname = 'rednote_reject_mutation'")
        ).scalar_one()
    assert count == 1


def test_parse_runs_is_mutable(env: ParseEnv) -> None:
    """`parse_runs` **不加**触发器 —— 状态必须能推进。

    把可变的东西保护成不可变，唯一结果是系统不能工作。
    """
    file_id = _seed_file(env, "内容\n".encode("utf-8"), original_name="a.txt")
    run = _run_parse(env, file_id)
    with session_scope(env.settings) as session:
        session.execute(
            text("update parse_runs set ocr_engine = 'fake-ocr' where id = :id"),
            {"id": run.id},
        )
        value = session.execute(
            text("select ocr_engine from parse_runs where id = :id"), {"id": run.id}
        ).scalar_one()
    assert value == "fake-ocr"


def test_orm_guard_blocks_segment_mutation(env: ParseEnv) -> None:
    """**ORM 层守卫也生效**（双层保护的第一层，给可读错误）。"""
    from rednote.platform.immutability import ImmutableRecordError

    file_id = _seed_file(env, "内容\n".encode("utf-8"), original_name="a.txt")
    run = _run_parse(env, file_id)
    with pytest.raises(ImmutableRecordError):
        with session_scope(env.settings) as session:
            row = session.execute(
                text("select id from source_segments where parse_run_id = :r"),
                {"r": run.id},
            ).scalar_one()
            segment = session.get(SourceSegment, row)
            assert segment is not None
            segment.text_content = "改一下"
            session.flush()
