"""与 WP13（任务与 Outbox）的接缝（WP18）。

**本模块是 WP18 与任务框架之间唯一的地方**。它注册两个任务处理器：

| kind | 做什么 | 由哪个端点入队 |
|---|---|---|
| `BRIEF_PARSE` | 确保解析完成 → **解构** → 写草稿 | `POST /projects/{id}/brief-parse-jobs` |
| `BRIEF_ENRICH` | 基于允许的来源 → **产出建议** → 写草稿 | `POST /projects/{id}/brief-enrichment-jobs` |

---

## 一、为什么 `BRIEF_PARSE` **一个任务里既解析又解构**（而不是两个任务串联）

解析是 WP17 的 `PARSE_DOCUMENT` 任务。若 Brief 解构另起一个任务、
等解析完成后再跑，就需要一条"解析完成 → 触发解构"的链路。三条路都更差：

| 做法 | 问题 |
|---|---|
| 在 `PARSE_DOCUMENT` 处理器里顺手解构 | 让 WP17 的任务知道 Brief 的存在，**依赖方向反了**（WP17 的 handlers docstring 明确拒绝这类耦合） |
| 新发一个"解析完成"事件 | 需要往 `outbox_events` 加一个新事件类型，而事件类型是全系统共享的命名空间 —— 本包无权扩它 |
| 轮询 `parse_runs.state` 直到终态 | 任务会长期占着租约（`LEASE_DURATION_SECONDS=120`），一份 200 页 PDF 必然超时被抢，于是**同一份文档被解析两次** |

因此本包的做法是：**在一个任务里先确保解析完成、再解构**。
`execute_parse` 本身是幂等的（`mark_running` 是条件 UPDATE，
已在终态时读回当前状态返回），所以"复用已有解析结果"这条路径
不需要任何额外判断 —— 它天然就走通了。

**代价（诚实记录）**：一个 `BRIEF_PARSE` 任务要跑完解析 + 解构两段，
因此租约要够长。本包不调 `LEASE_DURATION_SECONDS`（那是 WP13 的资产），
而是把这个风险登记为待定项：**大文件的 Brief 解构需要 Worker 续租**。

## 二、任务 → 输入的关联方式（两个任务不同，理由不同）

**`BRIEF_PARSE`：用 `parse_runs.job_id` 反向查** —— 与 WP17 同一手法。
run 与 job 在**同一事务**里创建，因此 Worker 看得到 job 时 run 必然已存在，
不存在"任务没有指定 run"的竞态。

**`BRIEF_ENRICH`：参数落在 `brief_drafts.enrichment_request`**。
加厚没有 run 那样的天然落点，而三个"看似可以"的地方都不行：

| 候选 | 为什么不行 |
|---|---|
| `jobs.result_ref` | 它的语义是**结果**引用。用输入占住结果字段，任务终态时就再也放不下真正的产出（WP17 已论证过） |
| `jobs.input_version_ids` | 它是 `version_nodes` 的 id 数组 —— 放 `briefVersionId` 语义正确，但 `allowedSourceIds` 是 `source_segments` 的 id，放进去类型上说得通、语义上完全错 |
| 新建一张 `brief_enrichment_requests` 表 | 数据结构 02 §6 没有这张表，本包无权自创；而且这张表只为"传一次参数"存在，是一次性的 |

因此本包在 `brief_drafts` 上加一列 `enrichment_request`（jsonb，可空）。
**它不是"又一张表"**，而是"这一轮加厚请求了什么" ——
与"这一轮加厚产出了什么"（`suggestions`）是同一件事的两半，
放在同一行上才不会出现"请求在、产物没了"的中间态。

处理器读完就把它清空：**它是一次性的**，留着只会让下一轮误读到旧参数。

## 三、`HandlerOutcome` 的映射（PARTIAL 有独立映射）

| 解构结论 | 任务状态 | 理由 |
|---|---|---|
| `SUCCEEDED` | `SUCCEEDED` | 输入完整、解构完成 |
| `PARTIAL` | **`PARTIAL`** | **部分成功是独立状态**（WP17 的契约）。映射到 `SUCCEEDED` 会让"输入缺了 12 页"在任务层消失；映射到 `FAILED` 会让它可重试，而重试不会让缺的页长出来 |
| `FAILED` / `CANCELLED` | `FAILED` | `retryable=False`（见下） |

**`retryable` 恒为 `False`**：这些失败的原因是**输入本身**
（解析失败 / 出网策略否决 / 装配缺口），重试一万次也是同一个结果。
把它标成可重试只会退避重跑若干次并**再买一次模型调用** ——
与 WP16 的 `outcome_for_result` 默认 `retryable=False` 同一条判据。
"""

from __future__ import annotations

import logging
import uuid
from functools import partial
from typing import Any, Final

from rednote.modules.brief import service
from rednote.modules.brief.decomposer import (
    BriefDecomposer,
    DecomposerUnavailable,
    UnavailableDecomposer,
)
from rednote.modules.brief.repository import BriefRepository
from rednote.modules.brief.types import (
    KIND_BRIEF_ENRICH,
    KIND_BRIEF_PARSE,
    SOURCE_STATE_PARTIAL,
    SOURCE_STATE_SUCCEEDED,
)
from rednote.modules.files.repository import FileRepository
from rednote.modules.files.storage import StorageBackend, build_storage_backend
from rednote.modules.jobs.handlers import TaskHandlerRegistry
from rednote.modules.jobs.models import (
    STATE_FAILED,
    STATE_PARTIAL,
    STATE_SUCCEEDED,
)
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.jobs.types import ClaimedJob, HandlerOutcome
from rednote.modules.parsing.ocr import OcrEngine, build_ocr_engine
from rednote.modules.parsing.repository import ParseRepository
from rednote.modules.parsing.service import execute_parse
from rednote.modules.parsing.types import PARSE_TERMINAL_STATES
from rednote.platform.errors import ApiError

logger = logging.getLogger(__name__)

# 错误码（`HandlerOutcome.error_code` 的取值）。用显式常量而不是字面量：
# 排障时按错误码过滤，字面量拼错会静默地少捞到一批记录。
ERROR_RUN_NOT_FOUND: Final[str] = "BRIEF_PARSE_RUN_NOT_FOUND"
ERROR_BRIEF_NOT_FOUND: Final[str] = "BRIEF_NOT_FOUND"
ERROR_DECOMPOSER_UNAVAILABLE: Final[str] = "BRIEF_DECOMPOSER_UNAVAILABLE"
ERROR_DECOMPOSITION_REJECTED: Final[str] = "BRIEF_DECOMPOSITION_REJECTED"
ERROR_ENRICHMENT_REQUEST_MISSING: Final[str] = "BRIEF_ENRICHMENT_REQUEST_MISSING"


# ---------------------------------------------------------------------------
# BRIEF_PARSE
# ---------------------------------------------------------------------------


def brief_parse_handler(
    claimed: ClaimedJob,
    repo: JobRepository,
    *,
    decomposer: BriefDecomposer | None = None,
    storage: StorageBackend | None = None,
    ocr: OcrEngine | None = None,
) -> HandlerOutcome:
    """`BRIEF_PARSE` 的处理器：确保解析完成 → 解构 → 写草稿。

    依赖全部可注入（测试与证据脚本用）。`decomposer` 默认是
    `UnavailableDecomposer`（调用即抛）—— 见模块 docstring：
    给一个"看起来能调"的默认值会让"链路已验证"变成假话。
    """
    session = repo.session
    brief_repo = BriefRepository(session)
    parse_repo = ParseRepository(session)
    file_repo = FileRepository(session)

    run = parse_repo.find_run_for_job(claimed.job_id)
    if run is None:
        # 装配缺陷（有人手工造了一个 BRIEF_PARSE 任务，或 run 被删）。
        # **不重试**：重试不会让它长出一个 run。
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code=ERROR_RUN_NOT_FOUND,
            retryable=False,
        )

    # 解析未到终态 → 先跑解析。`execute_parse` 幂等（已终态时读回当前状态返回），
    # 因此"复用已有结果"这条路径不需要额外分支。
    if run.state not in PARSE_TERMINAL_STATES:
        run = execute_parse(
            parse_repo,
            run_id=run.id,
            file_repo=file_repo,
            storage=storage or build_storage_backend(backend="LOCAL", local_root=None),
            ocr=ocr or build_ocr_engine(None),
        )

    brief = _resolve_brief(brief_repo, file_repo=file_repo, file_id=run.file_id)
    if brief is None:
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code=ERROR_BRIEF_NOT_FOUND,
            retryable=False,
        )

    try:
        outcome = service.execute_decomposition(
            brief_repo,
            parse_repo,
            brief=brief,
            parse_run_id=run.id,
            decomposer=decomposer or UnavailableDecomposer(),
            job_id=claimed.job_id,
            attempt_id=claimed.attempt_id,
        )
    except DecomposerUnavailable as exc:
        # 出网策略否决 / 装配缺口（OP-130）/ 模型调用未完成。
        # **不重试**：这三类都不会因为重试而变好，而重试可能**再买一次调用**。
        logger.warning(
            "Brief 解构通道不可用",
            extra={"job_id": str(claimed.job_id), "reason": str(exc)},
        )
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code=ERROR_DECOMPOSER_UNAVAILABLE,
            retryable=False,
        )
    except ApiError as exc:
        # 解析失败/未完成（409）、草稿有未固化编辑（409）等业务拒绝。
        # 归成"解构被拒绝"并**不重试** —— 重试时输入不会变。
        logger.info(
            "Brief 解构被拒绝",
            extra={"job_id": str(claimed.job_id), "code": str(exc.code)},
        )
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code=ERROR_DECOMPOSITION_REJECTED,
            retryable=False,
        )

    return outcome_for_source_state(outcome.source_state)


def outcome_for_source_state(source_state: str) -> HandlerOutcome:
    """`source_state`（解构的输入完整度）→ `HandlerOutcome`（纯函数，便于单测）。

    **`PARTIAL` 有独立映射**（见模块 docstring 第三节）。
    """
    if source_state == SOURCE_STATE_SUCCEEDED:
        return HandlerOutcome(state=STATE_SUCCEEDED)
    if source_state == SOURCE_STATE_PARTIAL:
        return HandlerOutcome(state=STATE_PARTIAL)
    # 解构只在 `SUCCEEDED` / `PARTIAL` 上发生（`execute_decomposition`
    # 对 FAILED / CANCELLED / QUEUED / RUNNING 一律抛错）。
    # 走到这里说明状态集合与 `_CONSUMABLE_STATES` 漂移了 ——
    # **显式失败而不是猜一个状态**（与 WP17 的 `outcome_for_run_state` 同一手法）。
    raise ValueError(f"解构产出了不可映射的 source_state：{source_state}")


def _resolve_brief(
    brief_repo: BriefRepository, *, file_repo: FileRepository, file_id: uuid.UUID
) -> Any:
    """由文件反查它所属项目的逻辑 Brief。

    **走 `file_row.project_id` 而不是 job 的 `project_id`**：
    job 的 `project_id` 可空（平台级任务），而文件必然有项目
    （`visibility=PROJECT` 时 CHECK 保证）。用文件侧的那个，
    就不必处理"job 没有项目"这条分支 —— 而那条分支在正常流程里不存在。
    """
    file_row = file_repo.get_file_object(file_id)
    if file_row is None or file_row.project_id is None:
        return None
    return brief_repo.get_brief_by_project(file_row.project_id)


# ---------------------------------------------------------------------------
# BRIEF_ENRICH
# ---------------------------------------------------------------------------


def brief_enrich_handler(
    claimed: ClaimedJob,
    repo: JobRepository,
    *,
    decomposer: BriefDecomposer | None = None,
) -> HandlerOutcome:
    """`BRIEF_ENRICH` 的处理器：读草稿上的请求参数 → 产出建议 → 写回草稿。

    参数从 `brief_drafts.enrichment_request` 读（见模块 docstring 第二节），
    读完清空（它是一次性的）。
    """
    session = repo.session
    brief_repo = BriefRepository(session)
    parse_repo = ParseRepository(session)

    # 项目从 job 上取：加厚请求存在草稿行上，而草稿行不带项目
    # （项目在 `briefs` 上）。不带项目过滤会跨项目消费请求 ——
    # 见 `repository.list_drafts_with_pending_enrichment` 的说明。
    job = repo.get_job(claimed.job_id)
    if job is None or job.project_id is None:
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code=ERROR_BRIEF_NOT_FOUND,
            retryable=False,
        )

    request = service.read_enrichment_request(brief_repo, project_id=job.project_id)
    if request is None:
        # 没有待处理请求：可能已被另一个 attempt 消费（重复投递是正常的），
        # 也可能是装配缺陷。**不重试** —— 两种情况重试都不会让它出现。
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code=ERROR_ENRICHMENT_REQUEST_MISSING,
            retryable=False,
        )

    brief = brief_repo.get_brief(request.brief_id)
    if brief is None:
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code=ERROR_BRIEF_NOT_FOUND,
            retryable=False,
        )

    version = brief_repo.get_version(request.brief_version_id)
    segments = (
        tuple(parse_repo.list_segments(version.source_parse_run_id))
        if version is not None and version.source_parse_run_id is not None
        else ()
    )

    try:
        outcome = service.execute_enrichment(
            brief_repo,
            brief=brief,
            brief_version_id=request.brief_version_id,
            allowed_source_ids=request.allowed_source_ids,
            segments=segments,
            decomposer=decomposer or UnavailableDecomposer(),
            job_id=claimed.job_id,
            attempt_id=claimed.attempt_id,
        )
    except DecomposerUnavailable as exc:
        logger.warning(
            "Brief 加厚通道不可用",
            extra={"job_id": str(claimed.job_id), "reason": str(exc)},
        )
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code=ERROR_DECOMPOSER_UNAVAILABLE,
            retryable=False,
        )
    except ApiError as exc:
        logger.info(
            "Brief 加厚被拒绝",
            extra={"job_id": str(claimed.job_id), "code": str(exc.code)},
        )
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code=ERROR_DECOMPOSITION_REJECTED,
            retryable=False,
        )

    # 加厚产出的建议数**不影响任务状态**：一条建议都没产出不是失败
    # （模型认为现有内容已经够了），而 `PARTIAL` 在这里没有对应物 ——
    # 建议是"补充选项"，不像解构那样有"输入残缺"这个维度。
    del outcome
    return HandlerOutcome(state=STATE_SUCCEEDED)


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def build_task_registry(
    *,
    decomposer: BriefDecomposer | None = None,
    storage: StorageBackend | None = None,
    ocr: OcrEngine | None = None,
) -> TaskHandlerRegistry:
    """本包要注册的任务处理器（**调用方与 WP13 的默认集合合并**）。

    返回**新实例**而不是原地改 WP13 的默认注册表：
    与 `TaskHandlerRegistry.register` 的不可变语义一致 ——
    "测试里注册了一个处理器，影响后续测试"是这类注册表最常见的坑。
    """
    return TaskHandlerRegistry.empty().register(
        KIND_BRIEF_PARSE,
        partial(brief_parse_handler, decomposer=decomposer, storage=storage, ocr=ocr),
    ).register(
        KIND_BRIEF_ENRICH,
        partial(brief_enrich_handler, decomposer=decomposer),
    )


__all__ = [
    "ERROR_BRIEF_NOT_FOUND",
    "ERROR_DECOMPOSER_UNAVAILABLE",
    "ERROR_DECOMPOSITION_REJECTED",
    "ERROR_ENRICHMENT_REQUEST_MISSING",
    "ERROR_RUN_NOT_FOUND",
    "brief_enrich_handler",
    "brief_parse_handler",
    "build_task_registry",
    "outcome_for_source_state",
]
