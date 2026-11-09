"""解析端点（协议 03 §4.2 第 7 行，**只有 1 个**）。

| 方法 | 路径 | 成功码 | 依据 |
|---|---|---|---|
| POST | `/files/{fileId}/parse-jobs` | 202 | §4.2 第 7 行 |

**协议原文的输入是 `parserProfile?` + 幂等键，输出是 202**。
本实现的取舍：

| 协议字段 | 本实现 | 理由 |
|---|---|---|
| `parserProfile?`（可选） | **不接收** | "解析档案"这个概念在数据结构 02 §4 里不存在（没有 `parser_profiles` 表）。收一个无处落地的字段会让调用方以为它生效了。**当前解析器由文件类型决定**（`parsers.route_for`），不需要用户选。登记为待定项（OP-135） |
| 幂等键 | 用 `Idempotency-Key` 头（协议 §1.3 的通用机制） | 与其它端点一致 |
| 202 | ✅ | 端点只做"登记 + 入队"，实际解析由 Worker 执行 |

**为什么不新开查询端点**：协议 §4.2 第 6 行的 `GET /files/{fileId}`
的说明就是"文件元数据、**扫描解析状态**、权限"。解析状态属于它。
本包因此**扩展该端点的响应**（加 `latestParse` 字段），
而不是在协议之外造一个 `/files/{id}/parse-runs`。

**契约影响**：本包新增 **1 path / 1 operation**，并扩展
`GET /files/{fileId}` 的响应 schema。这是本包唯一允许动契约的地方。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Header, Response, status
from pydantic import BaseModel, ConfigDict, Field

from rednote.modules.files.dependencies import FileAccessDep, FileRepoDep
from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.jobs import service as jobs_service
from rednote.modules.parsing import service
from rednote.modules.parsing.dependencies import (
    JobRepoDep,
    ParseRepoDep,
    load_blob_with_repo,
)
from rednote.platform.api_responses import UPLOAD_READ_RESPONSES
from rednote.platform.errors import (
    ApiError,
    DataEnvelope,
    ErrorCode,
    build_meta,
)

router = APIRouter(tags=["parsing"])

# 响应集合与文件类端点共用（本端点可能返回 404 / 403 / 422 / 503）。
PARSE_RESPONSES = UPLOAD_READ_RESPONSES


class ParseRunBody(BaseModel):
    """解析运行（对外表示）。

    **容量/计数类字段一律十进制字符串**（协议 §1：不要根据数值大小
    在字符串和数字间切换）。`totalUnits` 可空 —— "不知道总量"与"总量是 0"
    是两件事，都输出 `"0"` 会让前端无法区分。
    """

    model_config = ConfigDict(populate_by_name=True)

    parse_run_id: uuid.UUID = Field(alias="parseRunId")
    file_id: uuid.UUID = Field(alias="fileId")
    job_id: uuid.UUID | None = Field(alias="jobId")
    parser_version: str = Field(alias="parserVersion")
    state: str
    # **不提供 `success`**：那个词会把 PARTIAL 卷进来。
    # `complete` 只对 SUCCEEDED 为真。
    complete: bool
    total_units: str | None = Field(alias="totalUnits")
    parsed_units: str = Field(alias="parsedUnits")
    errors: list[dict[str, Any]]
    ocr_engine: str = Field(alias="ocrEngine")


class ParseJobAcceptedBody(BaseModel):
    """202 响应体。

    `created=false` 表示复用了既有结果或已有排队中的任务 ——
    客户端据此可以区分"我刚发起的"与"系统里已经有了"。
    """

    model_config = ConfigDict(populate_by_name=True)

    parse_run: ParseRunBody = Field(alias="parseRun")
    created: bool


@router.post(
    "/files/{fileId}/parse-jobs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DataEnvelope[ParseJobAcceptedBody],
    responses=PARSE_RESPONSES,
    summary="发起文档解析任务",
)
def create_parse_job(
    access: FileAccessDep,
    user: CurrentUserDep,
    file_repo: FileRepoDep,
    parse_repo: ParseRepoDep,
    job_repo: JobRepoDep,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """发起解析（**202，不同步解析**）。

    顺序与判据：

    1. **只对扫描通过的文件**（协议 §4.2 第 7 行原文"只对扫描通过文件"）——
       `blob.scan_status` 不是 `CLEAN` 时报 409；
    2. **幂等**：同文件同解析器版本已有终态 run → 复用（`created=false`）；
    3. **建 run 与任务**（同一事务），返回 202。

    **UNSUPPORTED 的文件也允许发起解析**，且会得到一个
    `state='FAILED'` + `error_code='UNSUPPORTED_FORMAT'` 的 run ——
    而不是 400。理由：AC-07 要求这个事实是**一个可查询的状态**
    （"这个文件解析过、结论是不支持"），而不是一次性的错误响应。
    协议 §4.2 第 4 行对未知格式的要求同样是"返回文件归档结果与
    UNSUPPORTED 标记"，即它要被记录下来。
    """
    del user  # 权限已由 `FileAccessDep` 判定；这里不再用它做二次判断
    file_row = access.file_row
    blob = load_blob_with_repo(file_repo, access)

    if blob.scan_status != "CLEAN":
        # 协议原文"只对扫描通过文件"。**用 409 而不是 400**：
        # 请求本身没问题，是文件当前的状态不允许这个操作。
        raise ApiError(
            ErrorCode.INVALID_TRANSITION,
            "文件尚未通过扫描，不能发起解析。",
            details={
                "reason": "scan_not_clean",
                "scanStatus": blob.scan_status,
                "fileId": str(file_row.id),
            },
        )

    run, created = service.request_parse(parse_repo, file_row=file_row)
    if created:
        # **同一事务**里建任务（`file.uploaded` 的处理器也是这个顺序）。
        # 幂等键用 `fileId + parserVersion` 的摘要：同一文件的同版本解析
        # 不会因为用户点两次而建两个任务。
        view = jobs_service.enqueue_job(
            job_repo,
            kind=service.KIND_PARSE_DOCUMENT,
            queue=service.QUEUE_PARSING,
            idempotency_scope=service.IDEMPOTENCY_SCOPE_PARSE,
            idempotency_key=(
                f"{file_row.id}:{service.PARSER_VERSION}"
                if idempotency_key is None
                else f"{file_row.id}:{service.PARSER_VERSION}:{idempotency_key}"
            ),
            input_hash=service.parse_input_hash(
                file_id=file_row.id, parser_version=service.PARSER_VERSION
            ),
            project_id=file_row.project_id,
        )
        # 把 run 挂到任务上（处理器靠它反查 run）。
        run.job_id = view.job_id
        parse_repo.flush()
        refreshed = parse_repo.get_run(run.id)
        if refreshed is not None:
            run = refreshed

    # 202 是"已受理"。`response.status_code` 由装饰器给出，
    # 这里显式设置是为了让"复用"路径也能返回 202（而不是 200）——
    # 对客户端而言两种情况都是"受理了，去查状态"。
    response.status_code = status.HTTP_202_ACCEPTED

    return {
        "data": {
            "parseRun": service.describe_run(run),
            "created": created,
        },
        "meta": build_meta(),
    }


__all__ = [
    "PARSE_RESPONSES",
    "ParseJobAcceptedBody",
    "ParseRunBody",
    "router",
]
