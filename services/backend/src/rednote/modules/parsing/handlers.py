"""与 WP13（任务与 Outbox）的两个接缝（WP17）。

**本模块是 WP17 与任务框架之间唯一的地方**。它注册两样东西：

| 注册点 | 回答的问题 | 本包注册什么 |
|---|---|---|
| `jobs.handlers.TaskHandlerRegistry` | "这类活怎么干" | `PARSE_DOCUMENT` → 真的解析 |
| `jobs.dispatch.HandlerRegistry` | "某件事发生了，谁关心" | `file.uploaded` → 建解析任务 |

---

## 为什么 `file.uploaded` 的处理器要**建任务**而不是**直接解析**

协议 03 §4.1 的时序图第 8 步之后，Worker 才做"扫描后隔离解析"。
直接解析意味着事件投递的**调用栈里**要跑完整个解析 ——
一份 200 页 PDF 会占住投递通道（`InProcessChannel` 是同步调用），
而投递通道的超时与重试语义是为"投递"设计的，不是为"解析"设计的。

**更关键的是状态可见性**：建了任务之后，"这个文件的解析在排队"
是一个可查询的事实（`jobs.state='QUEUED'`）；
直接在事件处理器里解析的话，排队期间系统里没有任何记录，
用户看到的是"传上去了，什么都没发生"。

## 任务 → 解析运行：**用 `parse_runs.job_id` 反向查**，不用 `result_ref` 携带

一开始想的是"入队时把 run id 写进 `jobs.result_ref`，处理器读它"。
**这条路是错的**，有两个问题：

1. **竞态**：`enqueue_job` 与"写 `result_ref`"是两次写。Worker 若在
   两者之间领取任务，会看到一个 `result_ref` 为空的任务 ——
   于是"解析任务没有指定 run"这个失败会在**正常流程**里偶发。
2. **语义错位**：`jobs.result_ref` 的文档口径是"**结果**引用"
   （由 kind 解释），而 run 是任务的**输入**。用输入占住结果的字段，
   任务终态时就再也放不下真正的产出了。

改用**反向查**：`parse_runs.job_id` 指向产出它的任务（这一列本来就在
`models.py` 里，注释写着"产出这次解析的任务"）。处理器拿
`claimed.job_id` 去查 `parse_runs` 即可。

- **无竞态**：run 与 job 在**同一个事务**里创建，Worker 在提交前
  看不到 job，因此看得到 job 时 run 必然已存在；
- **一个真相**：任务与 run 的关联只存在一处（`parse_runs.job_id`），
  不存在"两处都要对"的问题。

## 为什么任务处理器要用 `JobRepository.session` 造出另外两个仓库

`TaskHandlerFn` 的签名是 `(ClaimedJob, JobRepository) -> HandlerOutcome`。
本处理器需要 `ParseRepository`（写 run 与片段）、`FileRepository`
（读文件与 blob）与 `StorageBackend`（读字节）。

三个选择：

| 做法 | 问题 |
|---|---|
| 给 `JobRepository` 加一个"造解析仓库"的方法 | 让任务框架知道解析模块的存在，**依赖方向反了** |
| 处理器自己 `session_scope()` 开新事务 | 解析结果与任务终态**不在同一事务**；崩溃窗口里片段已提交而任务仍是 RUNNING |
| **用 `JobRepository` 的同一个 session 造出这两个仓库** | 无。三个仓库共享一个 session = 一个事务 |

因此本模块用第三种，它需要 `JobRepository` 暴露自己持有的 session ——
`JobRepository.session` 就是为此而存在（WP16 曾在 gateway 里删掉过一个
同名 property，因为那个是"为缓解某个问题而生、问题消失后即噪声"；
**这个不同**：它是下游处理器访问**同事务**资源的唯一合法通道，
没有它，每个下游包都只能自己开事务，把任务终态与业务结果拆到两个事务里）。

## `HandlerOutcome` 的映射（与 WP16 的 `outcome_for_result` 同一条判据）

| 解析结论 | 任务状态 | 理由 |
|---|---|---|
| `SUCCEEDED` | `SUCCEEDED` | 全都解出来了 |
| `PARTIAL` | **`PARTIAL`** | **部分成功是独立状态**。映射到 `SUCCEEDED` 会让"缺了一半内容"在任务层消失；映射到 `FAILED` 会让它可重试（重试不改变结果，白跑） |
| `FAILED` | `FAILED` | `retryable=False`（见下） |

**`retryable` 恒为 `False`**：解析失败的原因是文件本身（损坏 / 不支持 /
编码判定不出），**重试一万次也是同一个结果**。把"解析失败"标成可重试
只会让它退避重跑若干次，纯粹浪费。
"""

from __future__ import annotations

import logging
import uuid
from functools import partial

from rednote.modules.files.repository import FileRepository
from rednote.modules.files.storage import StorageBackend, build_storage_backend
from rednote.modules.jobs.dispatch import EventEnvelope, HandlerRegistry
from rednote.modules.jobs.handlers import TaskHandlerRegistry
from rednote.modules.jobs.models import (
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_PARTIAL,
    STATE_SUCCEEDED,
)
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.jobs.types import ClaimedJob, HandlerOutcome
from rednote.modules.outbox.models import EVENT_FILE_UPLOADED
from rednote.modules.parsing import service
from rednote.modules.parsing.ocr import OcrEngine, build_ocr_engine
from rednote.modules.parsing.repository import ParseRepository
from rednote.modules.parsing.types import (
    PARSE_STATE_CANCELLED,
    PARSE_STATE_FAILED,
    PARSE_STATE_PARTIAL,
    PARSE_STATE_SUCCEEDED,
)

logger = logging.getLogger(__name__)


def parse_document_handler(
    claimed: ClaimedJob,
    repo: JobRepository,
    *,
    storage: StorageBackend | None = None,
    ocr: OcrEngine | None = None,
) -> HandlerOutcome:
    """`PARSE_DOCUMENT` 的处理器。

    `storage` 与 `ocr` 可注入（测试与证据脚本用）；生产装配时由调用方
    按配置构造。**默认 `build_ocr_engine(None)` = 不可用引擎** ——
    这保证了"没配 OCR 的部署绝不会虚报扫描件解析成功"。
    """
    session = repo.session
    parse_repo = ParseRepository(session)
    file_repo = FileRepository(session)

    run = parse_repo.find_run_for_job(claimed.job_id)
    if run is None:
        # 任务没有对应的 run：**不重试**。这是装配错误
        # （有人手工造了一个 `PARSE_DOCUMENT` 任务，或 run 被删了），
        # 重试不会让它长出一个 run。
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code="PARSE_RUN_NOT_FOUND",
            retryable=False,
        )

    finished = service.execute_parse(
        parse_repo,
        run_id=run.id,
        file_repo=file_repo,
        storage=storage or build_storage_backend(backend="LOCAL", local_root=None),
        ocr=ocr or build_ocr_engine(None),
    )
    return outcome_for_run_state(finished.state)


def outcome_for_run_state(run_state: str) -> HandlerOutcome:
    """`parse_runs.state` → `HandlerOutcome`（纯函数，便于单测）。

    **`PARTIAL` 有独立映射**（见模块 docstring）。
    """
    if run_state == PARSE_STATE_SUCCEEDED:
        return HandlerOutcome(state=STATE_SUCCEEDED)
    if run_state == PARSE_STATE_PARTIAL:
        return HandlerOutcome(state=STATE_PARTIAL)
    if run_state == PARSE_STATE_CANCELLED:
        return HandlerOutcome(state=STATE_CANCELLED)
    if run_state == PARSE_STATE_FAILED:
        return HandlerOutcome(
            state=STATE_FAILED,
            error_code="PARSE_FAILED",
            retryable=False,
        )
    # 非终态（QUEUED / RUNNING）：不该走到这里 —— 说明 `execute_parse`
    # 没能把状态推进到终态。**显式失败而不是猜一个状态**。
    raise ValueError(f"解析未到达终态：{run_state}")


def register_file_uploaded_handler(
    registry: HandlerRegistry, *, settings: object | None = None
) -> None:
    """注册 `file.uploaded` 的事件处理器。

    **事件处理器只建任务，不解析**（见模块 docstring）。

    **`settings` 必须能注入**（默认 `None` → 退回全局单例）：
    事件处理器发生在请求上下文之外，没有 `request.app.state.settings`
    可用，因此它天然会走 `session_scope()` 的全局单例路径。
    但**测试与证据脚本注入的是另一份配置**（临时库、临时存储根），
    全局单例在那种场景下没有 DSN，会直接报"database_url 未配置"。

    本机实测踩到过这个：事件处理器的测试全红，原因是它连的是
    "环境变量里的库"而不是"测试注入的库" —— 正是 WP08 踩过的
    "app 用注入配置装配、session 却按环境变量连库"的两套真相。
    因此这里把 settings 作为可注入参数显式暴露。
    """
    registry.register(
        EVENT_FILE_UPLOADED, partial(_handle_file_uploaded, settings=settings)
    )


def _handle_file_uploaded(
    event: EventEnvelope, *, settings: object | None = None
) -> None:
    """`file.uploaded` → 建一个 `PARSE_DOCUMENT` 任务。

    **它不消费事件里的 `sha256` / `byteSize` 等字段**：
    那些是给排障与审计看的（WP12 写事件时特意带上）。
    建任务只需要 `fileId` —— 其余的等处理器真去读文件时从库里取，
    保证"解析用的是当时的真实文件"，而不是事件里的历史快照。
    """
    raw_file_id = event.payload.get("fileId")
    if not isinstance(raw_file_id, str):
        # 事件里没有 fileId：**记日志不抛错**。
        # 抛错会让投递通道把这条事件标为失败并重试，而重试一万次
        # 也不会让 payload 长出 fileId —— 那是生产方（WP12）的缺陷，
        # 不该由消费方用无限重试来"惩罚"整个通道。
        logger.warning(
            "file.uploaded 事件缺少 fileId，跳过建任务",
            extra={"event_id": str(event.event_id)},
        )
        return
    try:
        file_id = uuid.UUID(raw_file_id)
    except ValueError:
        logger.warning(
            "file.uploaded 事件的 fileId 不是合法 UUID，跳过建任务",
            extra={"event_id": str(event.event_id)},
        )
        return

    _enqueue_parse_for_file(file_id, settings=settings)


def _enqueue_parse_for_file(file_id: uuid.UUID, *, settings: object | None = None) -> None:
    """按 `fileId` 建一个解析任务与对应的 run（**同一事务**）。

    幂等由两层承担：

      · `jobs` 的 `UQ(idempotency_scope, idempotency_key)` —— 同一文件
        不会因为事件重复投递而多建任务；
      · `find_reusable_run` —— 已有终态 run 时直接不建（数据结构 02 §4
        "同一解析器版本结果可复用"）。
    """
    from rednote.modules.jobs import service as jobs_service
    from rednote.platform.database import session_scope

    with session_scope(settings) as session:  # type: ignore[arg-type]
        job_repo = JobRepository(session)
        parse_repo = ParseRepository(session)
        file_repo = FileRepository(session)

        file_row = file_repo.get_file_object(file_id)
        if file_row is None:
            # 文件不存在（可能已被归档清理）：**不抛错**。
            # 抛错会让投递通道无限重试一条永远不会成功的投递。
            logger.warning(
                "file.uploaded 指向的文件不存在，跳过建任务",
                extra={"file_id": str(file_id)},
            )
            return

        existing = parse_repo.find_reusable_run(
            file_id=file_row.id, parser_version=service.PARSER_VERSION
        )
        if existing is not None:
            # 已有可复用结果 → 不重复建任务。
            # 重复上传同一文件（物理去重命中）不该每次都跑一遍解析。
            return

        view = jobs_service.enqueue_job(
            job_repo,
            kind=service.KIND_PARSE_DOCUMENT,
            queue=service.QUEUE_PARSING,
            idempotency_scope=service.IDEMPOTENCY_SCOPE_PARSE,
            idempotency_key=str(file_row.id),
            input_hash=service.parse_input_hash(
                file_id=file_row.id, parser_version=service.PARSER_VERSION
            ),
            project_id=file_row.project_id,
        )
        if not view.created:
            # 幂等命中：已有排队中/运行中的解析任务。**不重复建**。
            return

        # run 与 job 在**同一事务**里创建 → 处理器看得到 job 时
        # run 必然已存在（见模块 docstring 的"无竞态"说明）。
        service.request_parse(parse_repo, file_row=file_row, job_id=view.job_id)


def build_task_registry(
    *, storage: StorageBackend | None = None, ocr: OcrEngine | None = None
) -> TaskHandlerRegistry:
    """本包要注册的任务处理器（**调用方与 WP13 的默认集合合并**）。

    返回**新实例**而不是原地改 WP13 的默认注册表：
    与 `TaskHandlerRegistry.register` 的不可变语义一致 ——
    "测试里注册了一个处理器，影响后续测试"是这类注册表最常见的坑。
    """
    return TaskHandlerRegistry.empty().register(
        service.KIND_PARSE_DOCUMENT,
        partial(parse_document_handler, storage=storage, ocr=ocr),
    )


def build_event_registry(*, settings: object | None = None) -> HandlerRegistry:
    """本包要注册的事件处理器（新实例，理由同上）。

    `settings` 透传给处理器（见 `register_file_uploaded_handler`）——
    测试与证据脚本必须注入自己那一份配置，否则会连到环境变量里的库。
    """
    registry = HandlerRegistry()
    register_file_uploaded_handler(registry, settings=settings)
    return registry


__all__ = [
    "build_event_registry",
    "build_task_registry",
    "outcome_for_run_state",
    "parse_document_handler",
    "register_file_uploaded_handler",
]
