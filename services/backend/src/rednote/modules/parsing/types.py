"""WP17 的不可变值类型与受控取值域。

**为什么单独一个模块**：这些类型同时被 `parsers/`（各解析器）、
`ocr.py`（OCR 引擎）、`repository.py`（存储）与 `service.py`（编排）使用。
若定义在 service 里，其它几个都要反向导入 service —— 本模块的分层刻意单向：

    types ← ocr
    types ← parsers
    types ← repository
    types ← service

与 WP11~WP16 一致：**跨层传递的值用 frozen dataclass 而不是 ORM 对象**。

---

## 两件事在这里定死，因为它们决定"能不能虚报成功"

### 1. `total_units` 的语义（每个格式的 unit 是什么）

| 格式 | unit | 为什么 |
|---|---|---|
| `text/plain` `text/markdown` | **行**（按 `\\n` 切，空行也算） | 行是最自然的定位单位；空行也算进去才能让"第 N 行"与编辑器行号一致 |
| `text/csv` | **行**（含表头） | 与 csv 模块的 `reader` 行号一致，便于"第 7 行有错"这类提示 |
| `docx` | **段落**（`document.paragraphs`，含空段落） | python-docx 的段落索引就是"第几段"，含空段落才能让索引稳定 |
| `xlsx` | **非空单元格数** | 单元格是定位单位；空格不计入，否则一张 1000×20 的表会有 2 万个空 unit |
| `pdf`（有文本层） | **页数** | 页是 PDF 的天然定位单位 |
| `pdf`（无文本层） | **页数** | 同上 —— 走 OCR 时每页仍是一个 unit，失败页计入 errors |
| 图片 | **1** | 单图单 unit |

**"数不出 unit"不是错误，是"只能 FAILED"**：例如 docx 是 zip 但 XML 坏了，
`document.paragraphs` 会抛异常。这时 `total_units=None`，
`derive_state` 必然给出 FAILED —— **不存在"不知道总量却报成功"的路径**。

### 2. `state` 由计数推导，不由解析器自报

见 `derive_state`。这是本包验收第二半的结构性保证：
**解析器没有机会说"我成功了"**，它只能说"我解出了 k 个 unit"。

**但"解出几个 unit"与"产出几个片段"不是同一个数** ——
一页扫描件 OCR 出 4 行是 1 个 unit、4 个片段。两者混用会让
"识别成功"被算成"计数矛盾"从而报 FAILED。因此 `ParseOutcome.units_parsed`
是显式字段，见它的 docstring。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

# ---------------------------------------------------------------------------
# parse_runs.state（数据结构 02 §4）
# ---------------------------------------------------------------------------

PARSE_STATE_QUEUED: Final[str] = "QUEUED"
PARSE_STATE_RUNNING: Final[str] = "RUNNING"
PARSE_STATE_SUCCEEDED: Final[str] = "SUCCEEDED"
PARSE_STATE_PARTIAL: Final[str] = "PARTIAL"
PARSE_STATE_FAILED: Final[str] = "FAILED"
PARSE_STATE_CANCELLED: Final[str] = "CANCELLED"

PARSE_STATES: Final[tuple[str, ...]] = (
    PARSE_STATE_QUEUED,
    PARSE_STATE_RUNNING,
    PARSE_STATE_SUCCEEDED,
    PARSE_STATE_PARTIAL,
    PARSE_STATE_FAILED,
    PARSE_STATE_CANCELLED,
)

PARSE_TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {
        PARSE_STATE_SUCCEEDED,
        PARSE_STATE_PARTIAL,
        PARSE_STATE_FAILED,
        PARSE_STATE_CANCELLED,
    }
)

# **刻意不含 PARTIAL 的"成功"集合**。下游（WP18/WP21）必须显式区分
# "全都解出来了"与"解出来一部分"，因此不提供 `is_success` 这种会把
# 两者合并的便利属性 —— 便利属性正是让 PARTIAL 被当成 SUCCEEDED 的东西。
PARSE_COMPLETE_STATES: Final[frozenset[str]] = frozenset({PARSE_STATE_SUCCEEDED})

PARSE_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(PARSE_STATES)
)

# ---------------------------------------------------------------------------
# 提取方式（数据结构 02 §4：extraction_kind TEXT/OCR/TABLE）
# ---------------------------------------------------------------------------

EXTRACTION_TEXT: Final[str] = "TEXT"
EXTRACTION_OCR: Final[str] = "OCR"
EXTRACTION_TABLE: Final[str] = "TABLE"

EXTRACTION_KINDS: Final[tuple[str, ...]] = (
    EXTRACTION_TEXT,
    EXTRACTION_OCR,
    EXTRACTION_TABLE,
)

EXTRACTION_KIND_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(EXTRACTION_KINDS)
)

# ---------------------------------------------------------------------------
# position.kind（来源定位的种类）
# ---------------------------------------------------------------------------

POSITION_PAGE: Final[str] = "PAGE"
POSITION_PARAGRAPH: Final[str] = "PARAGRAPH"
POSITION_LINE: Final[str] = "LINE"
POSITION_CELL: Final[str] = "CELL"
POSITION_WHOLE: Final[str] = "WHOLE"

POSITION_KINDS: Final[tuple[str, ...]] = (
    POSITION_PAGE,
    POSITION_PARAGRAPH,
    POSITION_LINE,
    POSITION_CELL,
    POSITION_WHOLE,
)

# ---------------------------------------------------------------------------
# 错误码（**两个不同的"失败"必须分开**）
# ---------------------------------------------------------------------------

# 承诺清单之外（`file_objects.classification = UNSUPPORTED`）。
# 用户动作：转换格式。
ERROR_UNSUPPORTED_FORMAT: Final[str] = "UNSUPPORTED_FORMAT"
# 清单内但结构读不出来（zip 损坏、PDF 交叉引用表坏了、编码无法判定）。
# 用户动作：检查/重新导出这个文件。**与上一条不是同一件事**。
ERROR_MALFORMED_DOCUMENT: Final[str] = "MALFORMED_DOCUMENT"
# 无文本层（扫描件），且 OCR 引擎不可用。
# **绝不能降级成"成功但内容为空"** —— 那是"虚报成功"最严重的形态。
ERROR_OCR_ENGINE_UNAVAILABLE: Final[str] = "OCR_ENGINE_UNAVAILABLE"
# 单个 unit 解析失败（该页/该段读不出）。
ERROR_UNIT_FAILED: Final[str] = "UNIT_FAILED"
# 逐 unit 时间预算耗尽。
ERROR_UNIT_TIMEOUT: Final[str] = "UNIT_TIMEOUT"
# 文件在存储上不在了。
ERROR_FILE_UNAVAILABLE: Final[str] = "FILE_UNAVAILABLE"

PARSE_ERROR_CODES: Final[tuple[str, ...]] = (
    ERROR_UNSUPPORTED_FORMAT,
    ERROR_MALFORMED_DOCUMENT,
    ERROR_OCR_ENGINE_UNAVAILABLE,
    ERROR_UNIT_FAILED,
    ERROR_UNIT_TIMEOUT,
    ERROR_FILE_UNAVAILABLE,
)

# ---------------------------------------------------------------------------
# 上限（C4：所有外部调用必须有显式上限）
# ---------------------------------------------------------------------------

# 单个片段文本上限（字符）。超出**截断并记 error**，不静默丢。
# 一个 50 MB 的纯文本文件会被切成一堆 20 万字符的行 —— 那是数据问题，
# 不该让单行把整个解析拖死。
MAX_SEGMENT_CHARS: Final[int] = 200_000

# 单文件 unit 上限。超出即 MALFORMED_DOCUMENT（见 service 的说明）：
# 20 万页的 PDF 不是"值得解析的文件"，是"该被拒绝的输入"。
MAX_UNITS_PER_FILE: Final[int] = 20_000

# 逐 unit 时间预算（秒，**软超时**）。
#
# 只能拦住"单个 unit 提取很慢"，拦不住"解析器整体挂死" ——
# 后者需要子进程 + kill，**显式登记为内网补做项**（OP-136；本机无容器编排可用链）。
#
# **它确实在用**（`parsers/pdf.py` 逐页计时）：超过预算时记一条
# `ERROR_UNIT_TIMEOUT` 失败，**但保留已拿到的文本** ——
# 丢弃它等于把"这页很慢"变成"这页没有内容"。
UNIT_TIME_BUDGET_SECONDS: Final[float] = 5.0

# 解析器版本。**实现变更必须递增它**，否则"同一解析器版本结果可复用"
# 这条（数据结构 02 §4）会把旧结果当成新实现的产出返回。
# "如何保证必然递增"登记为待定项（无法靠代码自动保证）。
PARSER_VERSION: Final[str] = "wp17-1"

# OCR 引擎标识（落进 `source_segments` 便于"这条文本是谁识别的"可查）。
OCR_ENGINE_NONE: Final[str] = "none"


def derive_state(*, total_units: int | None, parsed_units: int) -> str:
    """由计数**推导**解析状态（本包验收第二半的结构性保证）。

    解析器没有机会自报状态 —— 它只能报告"我解出了几个 unit"，
    状态由本函数算出来。因此"虚报成功"在服务层就不存在路径。

    | total_units | parsed_units | state | 说明 |
    |---|---|---|---|
    | `None` | 任意 | `FAILED` | **数不出总量** = 无法证明"全解出来了" |
    | `0` | `0` | `FAILED` | 空文件：解出 0 个片段对下游等于没有输入 |
    | `k > 0` | `0` | `FAILED` | 一个都没解出来 |
    | `k > 0` | `k` | `SUCCEEDED` | 全部解出 |
    | `k > 0` | `0 < p < k` | `PARTIAL` | **部分失败：独立状态，不是成功** |
    | `k > 0` | `> k` | `FAILED` | 计数自相矛盾（数据库 CHECK 也会拦，这里是第二道） |

    **为什么 `total_units=0` 归 FAILED 而不是 SUCCEEDED**：
    一个 0 字节文件"全部解出来"了 0 个片段 —— 形式上成立，但语义上
    下游拿不到任何输入。报成功会让 WP18 认为"Brief 已解析完成"
    然后在一堆空字段上做拆解。**"成功"这个词必须意味着"有东西可用"**。
    """
    if total_units is None:
        return PARSE_STATE_FAILED
    if total_units == 0:
        return PARSE_STATE_FAILED
    if parsed_units < 0 or parsed_units > total_units:
        return PARSE_STATE_FAILED
    if parsed_units == 0:
        return PARSE_STATE_FAILED
    if parsed_units == total_units:
        return PARSE_STATE_SUCCEEDED
    return PARSE_STATE_PARTIAL


@dataclass(frozen=True)
class SegmentPosition:
    """来源定位（`source_segments.position`）。

    **为什么用构造器而不是自由 dict**：自由 jsonb 里 `{}` 也是合法 position，
    于是"每个片段都能定位回原文"这句话**无法被验证**。这里让 `kind`
    决定必填字段，构造时即拒绝不合法的组合。

    | kind | 必填 | 例 |
    |---|---|---|
    | `PAGE` | `page` | `{"kind":"PAGE","page":7}` |
    | `PARAGRAPH` | `index` | `{"kind":"PARAGRAPH","index":12}` |
    | `LINE` | `line` | `{"kind":"LINE","line":42}` |
    | `CELL` | `sheet`, `row`, `col` | `{"kind":"CELL","sheet":"Sheet1","row":3,"col":"B"}` |
    | `WHOLE` | 无 | `{"kind":"WHOLE"}`（图片这类没有内部结构的） |

    `bbox` 可选：OCR 行框 `[x0, y0, x1, y1]`（**归一化到 0~1**）。
    归一化而不是像素：不同 DPI 的同一份扫描件像素坐标不同，
    归一化后"跳到原文那个位置"这件事才与 DPI 无关。
    """

    kind: str
    page: int | None = None
    index: int | None = None
    line: int | None = None
    sheet: str | None = None
    row: int | None = None
    col: str | None = None
    bbox: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        if self.kind not in POSITION_KINDS:
            raise ValueError(f"未知的 position.kind: {self.kind!r}")
        if self.kind == POSITION_PAGE and self.page is None:
            raise ValueError("PAGE 定位必须给出 page")
        if self.kind == POSITION_PARAGRAPH and self.index is None:
            raise ValueError("PARAGRAPH 定位必须给出 index")
        if self.kind == POSITION_LINE and self.line is None:
            raise ValueError("LINE 定位必须给出 line")
        if self.kind == POSITION_CELL and (
            self.sheet is None or self.row is None or self.col is None
        ):
            raise ValueError("CELL 定位必须给出 sheet / row / col")
        if self.bbox is not None:
            if len(self.bbox) != 4:
                raise ValueError("bbox 必须是 4 个数")
            if any(value < 0.0 or value > 1.0 for value in self.bbox):
                raise ValueError("bbox 必须归一化到 0~1")
            x0, y0, x1, y1 = self.bbox
            if x1 < x0 or y1 < y0:
                raise ValueError("bbox 的右下角必须不小于左上角")
        # 页码/序号从 1 开始（人看的"第几页"），行列从 1 开始（Excel 习惯）。
        for name, value in (("page", self.page), ("index", self.index), ("line", self.line), ("row", self.row)):
            if value is not None and value < 1:
                raise ValueError(f"{name} 从 1 开始，收到 {value}")

    def to_json(self) -> dict[str, Any]:
        """落库用（只放非 None 字段，避免库里出现一堆 null）。"""
        payload: dict[str, Any] = {"kind": self.kind}
        for key in ("page", "index", "line", "sheet", "row", "col"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.bbox is not None:
            payload["bbox"] = list(self.bbox)
        return payload


@dataclass(frozen=True)
class SegmentDraft:
    """一个待落库的片段。解析器只产出它，不碰数据库。"""

    position: SegmentPosition
    text_content: str
    extraction_kind: str
    confidence: float | None = None
    source_url: str | None = None

    def __post_init__(self) -> None:
        if self.extraction_kind not in EXTRACTION_KINDS:
            raise ValueError(f"未知的 extraction_kind: {self.extraction_kind!r}")
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence 必须在 0~1")
        if self.text_content == "":
            # 空文本片段没有信息量，且会让 `UQ(parse_run_id, content_hash, position)`
            # 上出现大量"同位置同空串"的重复行。解析器应当**不产出**空片段
            # （跳过的 unit 计入 errors，而不是产出一个空片段）。
            raise ValueError("不产出空文本片段（跳过的 unit 应记入 errors）")


@dataclass(frozen=True)
class UnitFailure:
    """单个 unit 的失败（进 `parse_runs.errors`）。"""

    unit: int
    code: str
    # 只放简短说明，**不放正文**（与 WP15 审计"不存正文"同一判据）。
    message: str = ""

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"unit": self.unit, "code": self.code}
        if self.message:
            payload["message"] = self.message[:200]
        return payload


@dataclass(frozen=True)
class ParseOutcome:
    """一次解析的结果（解析器 → service 的唯一通道）。

    **注意这里没有 `state`** —— 状态由 `derive_state(total_units, parsed_units)`
    推导。这是刻意的：解析器若能自报状态，"不虚报成功"就只剩自觉。

    ---

    ## `units_parsed` 与 `len(segments)` **不是同一个数**（本包最容易搞错的一处）

    一个 unit 可以产出**多个**片段：

    | 场景 | unit | 片段数 |
    |---|---|---|
    | 一页 PDF 有正文 | 1 页 | 1（整页文本） |
    | 一页扫描件走 OCR | 1 页 | **N（每行一段，各带 bbox）** |
    | 一张图走 OCR | 1 张图 | **N（每行一段）** |
    | 一个 xlsx 单元格 | 1 格 | 1 |

    初版把 `parsed_units` 直接定义成 `len(segments)`。**那是错的**：
    一张图 OCR 出 4 行会得到 `parsed_units=4 > total_units=1`，
    被 `derive_state` 判成"计数自相矛盾"→ FAILED，
    而这张图其实**识别得很成功**。

    因此 `units_parsed` 是**显式字段**：由解析器报告"我解出了几个 unit"，
    默认值（`None`）才退回 `len(segments)` —— 那只在"一个 unit 一段"
    的格式（txt / csv / docx）上成立。
    """

    # `None` = 数不出总量（结构读不出）→ 必然 FAILED。
    total_units: int | None
    segments: tuple[SegmentDraft, ...] = ()
    failures: tuple[UnitFailure, ...] = ()
    # 解出的 **unit** 数。`None` = 按 `len(segments)` 推断
    # （只在"一个 unit 一段"的格式上正确，见 docstring）。
    units_parsed: int | None = None
    # 文件级错误（格式不支持 / 结构损坏 / OCR 不可用）。
    # 有它时 `total_units` 通常为 None。
    file_error_code: str | None = None
    file_error_message: str = ""
    # 实际用的 OCR 引擎名（进 `parse_runs.ocr_engine`）。
    ocr_engine: str = OCR_ENGINE_NONE
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def parsed_units(self) -> int:
        """**解出来的 unit 数**（不是片段数）。

        默认退回 `len(segments)`，但**有文件级错误时恒为 0** ——
        "格式不支持"却因为 errors 里带了东西而算出非零的 unit 数，
        会直接导致虚报成功。
        """
        if self.file_error_code is not None and self.units_parsed is None:
            return 0
        if self.units_parsed is not None:
            return self.units_parsed
        return len(self.segments)

    @property
    def derived_state(self) -> str:
        return derive_state(
            total_units=self.total_units, parsed_units=self.parsed_units
        )

    def failures_json(self) -> list[dict[str, Any]]:
        items = [failure.to_json() for failure in self.failures]
        if self.file_error_code is not None:
            items.insert(
                0,
                {
                    "unit": 0,
                    "code": self.file_error_code,
                    "message": self.file_error_message[:200],
                },
            )
        return items


__all__ = [
    "ERROR_FILE_UNAVAILABLE",
    "ERROR_MALFORMED_DOCUMENT",
    "ERROR_OCR_ENGINE_UNAVAILABLE",
    "ERROR_UNIT_FAILED",
    "ERROR_UNIT_TIMEOUT",
    "ERROR_UNSUPPORTED_FORMAT",
    "EXTRACTION_KINDS",
    "EXTRACTION_KIND_LIST_SQL",
    "EXTRACTION_OCR",
    "EXTRACTION_TABLE",
    "EXTRACTION_TEXT",
    "MAX_SEGMENT_CHARS",
    "MAX_UNITS_PER_FILE",
    "OCR_ENGINE_NONE",
    "PARSE_COMPLETE_STATES",
    "PARSE_ERROR_CODES",
    "PARSE_STATES",
    "PARSE_STATE_CANCELLED",
    "PARSE_STATE_FAILED",
    "PARSE_STATE_LIST_SQL",
    "PARSE_STATE_PARTIAL",
    "PARSE_STATE_QUEUED",
    "PARSE_STATE_RUNNING",
    "PARSE_STATE_SUCCEEDED",
    "PARSE_TERMINAL_STATES",
    "PARSER_VERSION",
    "POSITION_CELL",
    "POSITION_KINDS",
    "POSITION_LINE",
    "POSITION_PAGE",
    "POSITION_PARAGRAPH",
    "POSITION_WHOLE",
    "ParseOutcome",
    "SegmentDraft",
    "SegmentPosition",
    "UNIT_TIME_BUDGET_SECONDS",
    "UnitFailure",
    "derive_state",
]
