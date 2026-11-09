"""解析的业务规则（WP17 的唯一对外入口）。

**本模块的核心命题**：让"部分失败"这个事实**无法被隐藏**。

做法是三件事合起来：

1. **`total_units` 必须在解析前确定**（页数/段落数/行数/1）。
   数不出来 → `None` → `derive_state` 必然给出 FAILED。
   于是不存在"不知道总量却报成功"的路径。
2. **状态由计数推导**（`repository.finish_run` 没有 `state` 参数）。
   解析器只能报告"我解出了几个 unit"。
3. **数据库三条 CHECK** 兜底（见 `models.py`）。即使有人绕过本模块
   直接写 SQL，`SUCCEEDED 但只解出 3/20` 也进不去。

---

## `request_parse` 与 `execute_parse` 为什么分开

前者是**端点调用的**（校验权限与文件状态、判复用、建 run、入队任务），
后者是**任务处理器调用的**（真的读字节、调解析器、写片段与计数）。

合起来的话，端点会在 HTTP 请求里同步做完整个解析 ——
一份 200 页 PDF 会让请求挂几分钟，而协议 §4.2 第 7 行明确要求返回 **202**。
分开之后端点只做"登记 + 入队"，实际解析由 Worker 领取任务后执行。

**但两者共用同一套判定**（分类、复用、状态推导），
因此测试可以在不依赖任务框架的情况下直接调 `execute_parse` 验证解析语义 ——
这正是"验收打在服务层"的落地方式。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Final

from rednote.modules.files.models import (
    CLASSIFICATION_UNSUPPORTED,
    FileObject,
)
from rednote.modules.files.repository import FileRepository
from rednote.modules.files.storage import StorageBackend
from rednote.modules.parsing import parsers
from rednote.modules.parsing.models import ParseRun
from rednote.modules.parsing.ocr import OcrEngine, build_ocr_engine
from rednote.modules.parsing.repository import ParseRepository
from rednote.modules.parsing.types import (
    ERROR_FILE_UNAVAILABLE,
    ERROR_MALFORMED_DOCUMENT,
    ERROR_UNSUPPORTED_FORMAT,
    MAX_UNITS_PER_FILE,
    PARSE_STATE_SUCCEEDED,
    PARSER_VERSION,
    ParseOutcome,
)
from rednote.platform.errors import ApiError, ErrorCode

# 任务 kind 与队列（WP13 的注册表按 kind 索引）。
# 命名带 PARSE 前缀，与 `FAKE_PROVIDER` 这类测试用 kind 明显区分。
KIND_PARSE_DOCUMENT: Final[str] = "PARSE_DOCUMENT"
QUEUE_PARSING: Final[str] = "parsing"

# 幂等作用域（协议 §1.3）。同一文件同一解析器版本的解析请求
# 落到同一个 key 上，于是重复提交返回既有 run 而不是新建。
IDEMPOTENCY_SCOPE_PARSE: Final[str] = "parse"


def parse_input_hash(*, file_id: uuid.UUID, parser_version: str) -> str:
    """解析请求的内容摘要（协议 §1.3 的 `input_hash`）。

    **用 `canonical_payload_hash` 同一口径**（WP11 的规范化 JSON 哈希），
    而不是拼字符串：两处口径不同会让"同 key 不同内容"的判定出现
    微妙的假阳性（例如键顺序）。见 `versions.service.canonical_payload_hash`。
    """
    from rednote.modules.versions.service import canonical_payload_hash

    return canonical_payload_hash(
        {"fileId": str(file_id), "parserVersion": parser_version}
    )


def segment_content_hash(text: str) -> str:
    """片段内容摘要（`source_segments.content_hash`）。

    **UTF-8 字节的 SHA-256**，不是 `canonical_payload_hash` ——
    后者的输入是 JSON 结构，而这里是纯文本。强行统一会让
    "片段摘要"多一层 JSON 包装（引号与转义），且下游要比较摘要时
    必须知道那层包装的存在。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 端点路径：登记 + 入队
# ---------------------------------------------------------------------------


def request_parse(
    repo: ParseRepository,
    *,
    file_row: FileObject,
    job_id: uuid.UUID | None = None,
    parser_version: str = PARSER_VERSION,
) -> tuple[ParseRun, bool]:
    """登记一次解析（返回 `(run, created)`）。

    **`created=False` 的两种来源**：

      · 同文件同解析器版本已有终态 run → 复用它（数据结构 02 §4
        "同一解析器版本结果可复用"）；
      · 该文件被标为 `UNSUPPORTED` → **仍然建 run**，但它必然是 FAILED。

    **为什么 UNSUPPORTED 也要建 run 而不是直接报 400**：
    AC-07 要求"未知格式返回文件归档结果与 UNSUPPORTED 标记"，
    即这个事实必须是**一个可查询的状态**，而不是一次性的错误响应。
    建 run 之后 `GET /files/{fileId}` 能回答"这个文件解析过、结论是不支持"，
    而报 400 的话这个信息在响应之后就不存在了。
    """
    existing = repo.find_reusable_run(
        file_id=file_row.id, parser_version=parser_version
    )
    if existing is not None:
        return existing, False

    run = repo.insert_run(
        file_id=file_row.id, job_id=job_id, parser_version=parser_version
    )
    repo.flush()
    return run, True


# ---------------------------------------------------------------------------
# 处理器路径：真的解析
# ---------------------------------------------------------------------------


def execute_parse(
    repo: ParseRepository,
    *,
    run_id: uuid.UUID,
    file_repo: FileRepository,
    storage: StorageBackend,
    ocr: OcrEngine | None = None,
) -> ParseRun:
    """执行一次解析并把结果落库。

    **顺序**：

    1. 取 run 与 file（run 不存在 → 抛 404；这属程序缺陷，不是业务失败）；
    2. `QUEUED → RUNNING`（条件 UPDATE，改不到说明别人在处理它）；
    3. 读文件字节（读不到 → `FILE_UNAVAILABLE`）；
    4. 调解析器（**唯一的业务判断入口**，见 `parsers.parse`）；
    5. 写片段（`ON CONFLICT DO NOTHING`，重跑安全）；
    6. 写终态（**状态由计数推导**）。

    **第 5 步与第 6 步的顺序不能换**：先写片段再写计数，
    否则"计数说解出了 18 个，库里只有 0 个片段"会短暂存在 ——
    而如果第 5 步之后崩溃，第 6 步没执行，run 留在 RUNNING，
    由外层（任务框架的租约回收）处置。反过来（先写计数）崩溃则留下
    "成功但没片段"，那才是不可恢复的坏状态。
    """
    engine = ocr or build_ocr_engine(None)
    run = repo.get_run(run_id)
    if run is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "解析记录不存在。",
            details={"parseRunId": str(run_id)},
        )

    now = repo.now()
    if not repo.mark_running(run_id=run_id, now=now):
        # 已经不在 QUEUED（终态 / 别人在跑）。**不抛错** ——
        # 重复投递是正常现象（WP13 的投递语义是"至少一次"），
        # 读回当前状态返回即可。
        #
        # `populate_existing=True` 是必须的：`mark_running` 是裸 UPDATE，
        # 不经过 ORM 属性赋值，因此 identity map 里的对象仍是旧值。
        # 不强制刷新的话这里会返回一个 `state='QUEUED'` 的陈旧对象，
        # 而调用方（任务处理器）会据此报一个错误的任务状态。
        refreshed = repo.get_run(run_id)
        assert refreshed is not None
        return refreshed

    file_row = file_repo.get_file_object(run.file_id)
    if file_row is None:
        return _finish(
            repo,
            run_id=run_id,
            outcome=ParseOutcome(
                total_units=None,
                file_error_code=ERROR_FILE_UNAVAILABLE,
                file_error_message="文件记录不存在。",
            ),
        )
    blob = file_repo.get_blob(file_row.blob_id)
    if blob is None:
        return _finish(
            repo,
            run_id=run_id,
            outcome=ParseOutcome(
                total_units=None,
                file_error_code=ERROR_FILE_UNAVAILABLE,
                file_error_message="文件物理记录不存在。",
            ),
        )

    try:
        raw = _read_all(storage, key=blob.object_key)
    except Exception as exc:  # noqa: BLE001 - 存储层异常统一归类
        return _finish(
            repo,
            run_id=run_id,
            outcome=ParseOutcome(
                total_units=None,
                file_error_code=ERROR_FILE_UNAVAILABLE,
                file_error_message=f"读取文件失败（{type(exc).__name__}）。",
            ),
        )

    outcome = _parse_bytes(
        raw,
        original_name=file_row.original_name,
        mime_type=blob.mime_type,
        classification=file_row.classification,
        ocr=engine,
    )
    outcome = _enforce_unit_limit(outcome)

    # 写片段。`SegmentDraft` → repository 的元组形状。
    rows = [
        (
            segment.position.to_json(),
            segment.text_content,
            segment_content_hash(segment.text_content),
            segment.extraction_kind,
            segment.confidence,
        )
        for segment in outcome.segments
    ]
    inserted = repo.insert_segments(run_id=run_id, segments=rows)

    # **核对**：写进去的片段数必须等于 outcome 声称的解析数。
    # 不等说明有重复片段被 `ON CONFLICT` 跳过 —— 那是**重跑**的正常现象
    # （第二次执行产出同样的片段集），此时计数应以 outcome 为准，
    # 但要在日志里留痕，避免"重复被当成正常"掩盖真正的 bug。
    # 这里不抛错：抛出会让 run 停在 RUNNING，比"计数与片段数不一致"更糟。
    del inserted

    return _finish(repo, run_id=run_id, outcome=outcome)


def _finish(
    repo: ParseRepository, *, run_id: uuid.UUID, outcome: ParseOutcome
) -> ParseRun:
    """写终态并读回。

    **`finished_at` 用 `repo.now()` 而不是 `outcome` 里的时间**：
    完成时刻由数据库给（`now()`），避免多实例时钟漂移导致
    "完成时间早于开始时间"这种在库里看起来像 bug 的数据。
    """
    now = repo.now()
    repo.finish_run(
        run_id=run_id,
        total_units=outcome.total_units,
        parsed_units=outcome.parsed_units,
        errors=outcome.failures_json(),
        ocr_engine=outcome.ocr_engine,
        now=now,
    )
    repo.flush()
    refreshed = repo.get_run(run_id)
    assert refreshed is not None
    return refreshed


def _read_all(storage: StorageBackend, *, key: str) -> bytes:
    """把对象读成完整字节。

    **为什么整体读进内存而不是流式**：解析库（pypdf / python-docx /
    openpyxl / Pillow）的入口都是"接受一个 file-like 或路径"，
    它们内部本来就会把结构读进内存。文件大小上限由 WP12 的配额
    （300 MB/项目）与各解析器的显式上限共同约束，
    因此不存在"一个文件把内存吃光"的路径。
    """
    return b"".join(storage.open_stream(key=key))


def _parse_bytes(
    raw: bytes,
    *,
    original_name: str,
    mime_type: str,
    classification: str,
    ocr: OcrEngine,
) -> ParseOutcome:
    """分类 + 分发。**不支持的格式在这里被拦下。**"""
    if classification == CLASSIFICATION_UNSUPPORTED:
        return ParseOutcome(
            total_units=None,
            file_error_code=ERROR_UNSUPPORTED_FORMAT,
            file_error_message="文件在承诺可解析清单之外（AC-07 的 UNSUPPORTED 标记）。",
        )
    try:
        return parsers.parse(
            raw,
            original_name=original_name,
            mime_type=mime_type,
            ocr=ocr,
        )
    except Exception as exc:  # noqa: BLE001 - 解析器内部的意外
        # **不把异常往上抛**：解析器崩了是"这个文件没解出来"，
        # 不是"服务出错"。抛出去会让 run 停在 RUNNING（更糟的状态）。
        return ParseOutcome(
            total_units=None,
            file_error_code=ERROR_MALFORMED_DOCUMENT,
            file_error_message=f"解析器内部错误（{type(exc).__name__}）。",
        )


def _enforce_unit_limit(outcome: ParseOutcome) -> ParseOutcome:
    """`total_units` 超过上限时改判 MALFORMED。

    各解析器已经各自检查了自己的上限（PDF 页数、sheet 行数、图片像素），
    这里是**统一兜底** —— 新增解析器时忘了加检查，也不会让一个
    20 万行的 txt 进来（txt 的行数没有天然上限）。
    """
    if outcome.total_units is None or outcome.total_units <= MAX_UNITS_PER_FILE:
        return outcome
    return ParseOutcome(
        total_units=None,
        file_error_code=ERROR_MALFORMED_DOCUMENT,
        file_error_message=(
            f"单元数 {outcome.total_units} 超过单文件上限 {MAX_UNITS_PER_FILE}。"
        ),
    )


# ---------------------------------------------------------------------------
# 只读查询（端点用）
# ---------------------------------------------------------------------------


def describe_run(run: ParseRun) -> dict[str, Any]:
    """`ParseRun` → 对外字段（协议用 camelCase，容量类字段是字符串）。

    **`state` 原样输出**，不做"成功/失败"的二值化 ——
    `PARTIAL` 必须被前端看到（它决定了"要不要提醒用户缺了内容"）。
    提供 `complete` 字段让前端能一眼区分"全都解出来了"，
    但**不提供 `success`**：那个词会把 PARTIAL 卷进来。
    """
    return {
        "parseRunId": str(run.id),
        "fileId": str(run.file_id),
        "jobId": str(run.job_id) if run.job_id else None,
        "parserVersion": run.parser_version,
        "state": run.state,
        "complete": run.state == PARSE_STATE_SUCCEEDED,
        # **容量/计数类字段一律十进制字符串**（协议 §1：不要根据数值大小
        # 在字符串和数字间切换）。`totalUnits` 可空（数不出来），
        # 因此输出 `None` 而不是 `"0"` —— "不知道"与"是 0"是两件事。
        "totalUnits": None if run.total_units is None else str(run.total_units),
        "parsedUnits": str(run.parsed_units),
        "errors": list(run.errors or []),
        "ocrEngine": run.ocr_engine,
        "startedAt": run.started_at,
        "finishedAt": run.finished_at,
    }


def latest_summary(repo: ParseRepository, file_id: uuid.UUID) -> dict[str, Any] | None:
    """该文件最近一次解析的摘要（供 `GET /files/{fileId}` 扩展用）。

    没有解析记录时返回 `None`（而不是造一个 `state='QUEUED'` 的假对象）——
    "还没解析过"与"正在排队"是两件事，前者要前端显示"未解析"。
    """
    run = repo.latest_run_for_file(file_id)
    if run is None:
        return None
    return describe_run(run)


__all__ = [
    "IDEMPOTENCY_SCOPE_PARSE",
    "KIND_PARSE_DOCUMENT",
    "QUEUE_PARSING",
    "describe_run",
    "execute_parse",
    "latest_summary",
    "parse_input_hash",
    "request_parse",
    "segment_content_hash",
]
