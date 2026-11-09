"""任务入队、租约、执行与幂等（WP13）。

**本模块的四个对外能力**，逐条对应研发计划 04 §WP13 的四个切片：

| 能力 | 函数 | 切片 |
|---|---|---|
| 事务写 job + outbox | `enqueue_job` | F1 |
| 投递与确认 | `dispatch.dispatch_outbox` | F2 |
| 领取、执行、写回（租约 + fence） | `claim_jobs` / `run_job` / `finish_*` | F3 |
| UNKNOWN 恢复 | `pause_for_reconciliation` / `reconcile_job` | F4 |

**一个关键的设计选择：执行路径不依赖消息队列。**

`enqueue_job` 会写一条 outbox 事件（协议要求"事务写 job 与 outbox"），
但 **Worker 的执行是通过轮询 `jobs` 表领取的，不是消费 MQ 消息**。

理由有两条：

1. **本机没有 RabbitMQ**。若执行依赖 MQ，本包的四个切片在本机一个都验证不了 ——
   而其中三条（不丢、不重复、不被旧 Worker 覆盖）恰恰只能在能跑起来的环境里验证。
2. **更稳健**：`jobs` 表本身就是"业务状态权威"（数据结构 §11 原文）。
   让它成为执行的事实来源，MQ 就退化为"加速与解耦"的可选件（内网接上后
   可以用来唤醒 Worker，但不影响正确性）。反过来若 MQ 是必需品，
    MQ 停机就等于系统停产 —— 而本包的验收第一句正是"队列停机不会丢任务"。

**关于"重复投递不重复执行"的三层保护**（缺一不可）：

| 层 | 挡什么 |
|---|---|
| `jobs` 的状态 + 租约 | 同一时刻只有一个 Worker 持有它 |
| `fence_token` 条件写回 | 旧 Worker 的**晚到结果**不会被采纳 |
| `idempotency_records` | 同一个业务请求重试时不会新建任务 |
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from rednote.modules.jobs.dispatch import to_envelope
from rednote.modules.jobs.handlers import TaskHandlerRegistry
from rednote.modules.jobs.models import (
    ATTEMPT_FAILED,
    ATTEMPT_SUCCEEDED,
    IDEMPOTENCY_IN_PROGRESS,
    IDEMPOTENCY_RETENTION_DAYS,
    JOB_STATES,
    JOB_TERMINAL_STATES,
    LEASE_DURATION_SECONDS,
    MAX_CLAIM_BATCH,
    STATE_CANCEL_REQUESTED,
    STATE_QUEUED,
    STATE_RUNNING,
    STATE_WAITING_PROVIDER,
    STATE_WAITING_RECONCILIATION,
    Job,
)
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.jobs.types import (
    ClaimedJob,
    HandlerOutcome,
    IdempotencyDecision,
    JobView,
    ReclaimReport,
    RunReport,
)
from rednote.modules.outbox.models import AGGREGATE_JOB, EVENT_JOB_ENQUEUED
from rednote.platform.errors import ApiError, ErrorCode

# 重试上限。超过后任务落 FAILED（不再自动重试），但**保留尝试记录**，
# 供人工判断是"暂时性故障"还是"这个任务根本做不了"。
#
# 取 5 的理由：与 WP11 的并发注册重试同一个数量级 ——
# 连续 5 次失败说明问题不在"运气"，继续重试只是推迟必然的结论。
MAX_ATTEMPTS: Final[int] = 5


# ---------------------------------------------------------------------------
# 返回值
# ---------------------------------------------------------------------------


# 值类型（JobView / ClaimedJob / HandlerOutcome / RunReport / ReclaimReport /
# IdempotencyDecision）定义在 types.py —— 它们同时被 service 与 handlers 使用，
# 放在这里会形成 service → handlers → service 的循环导入。见 types.py 的说明。


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _validation_error(field: str, message: str, *, reason: str, **extra: Any) -> ApiError:
    details: dict[str, Any] = {
        "fieldErrors": [{"field": field, "type": reason, "message": message}],
        "reason": reason,
    }
    details.update(extra)
    return ApiError(ErrorCode.VALIDATION_FAILED, message, details=details)


def _require_non_empty(value: str, *, field: str) -> str:
    text = value.strip()
    if not text:
        raise _validation_error(field, f"{field} 不能为空。", reason="empty")
    return text


def _require_state(state: str, *, field: str) -> str:
    if state not in JOB_STATES:
        raise _validation_error(
            field,
            "未知的任务状态。",
            reason="job_state_unknown",
            allowedStates=sorted(JOB_STATES),
        )
    return state


def _view(job: Job, *, created: bool = False) -> JobView:
    return JobView(
        job_id=job.id,
        kind=job.kind,
        queue=job.queue,
        state=job.state,
        project_id=job.project_id,
        fence_token=job.fence_token,
        result_ref=job.result_ref,
        error_code=job.error_code,
        created=created,
    )


def require_job(
    repo: JobRepository, job_id: uuid.UUID, *, for_update: bool = False
) -> Job:
    job = repo.get_job(job_id, for_update=for_update)
    if job is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "任务不存在。",
            details={"resourceId": str(job_id)},
        )
    return job


# ---------------------------------------------------------------------------
# F1：入队（事务写 job + outbox）
# ---------------------------------------------------------------------------


def enqueue_job(
    repo: JobRepository,
    *,
    kind: str,
    queue: str,
    idempotency_scope: str,
    idempotency_key: str,
    input_hash: str,
    project_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    content_id: uuid.UUID | None = None,
    priority: int = 0,
    input_version_ids: Sequence[uuid.UUID] = (),
    run_at: datetime | None = None,
) -> JobView:
    """建任务并**在同一事务**写一条 `job.enqueued` 事件。

    **幂等**（协议 §1.3 的三条规则）：

      · 同 key 同 `input_hash` → 返回既有任务，`created=False`（不新增任务）；
      · 同 key 不同 `input_hash` → **409 `IDEMPOTENCY_CONFLICT`**；
      · 进行中的同 key → 同样是返回既有任务（第二次调用不会新建）。

    第 2 条是安全属性而非便利性：同一个 key 配不同内容，说明调用方
    把 key 复用错了（或有人重放）。静默返回旧结果会让调用方以为
    "我新提交的任务已经建好了"，而它根本没建。

    **事件与任务同事务**（切片 F1）：队列停机时事件躺在表里，
    恢复后由派发器搬出去。**不丢**。
    """
    normalized_kind = _require_non_empty(kind, field="kind")
    normalized_queue = _require_non_empty(queue, field="queue")
    scope = _require_non_empty(idempotency_scope, field="idempotencyScope")
    key = _require_non_empty(idempotency_key, field="idempotencyKey")
    digest = _require_non_empty(input_hash, field="inputHash")

    job, created = repo.insert_job(
        kind=normalized_kind,
        queue=normalized_queue,
        state=STATE_QUEUED,
        idempotency_scope=scope,
        idempotency_key=key,
        input_hash=digest,
        project_id=project_id,
        batch_id=batch_id,
        content_id=content_id,
        priority=priority,
        input_version_ids=input_version_ids,
        next_run_at=run_at,
    )

    if not created:
        if job.input_hash != digest:
            raise ApiError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "相同幂等键对应了不同的请求内容。",
                details={
                    "reason": "idempotency_key_reused",
                    "idempotencyScope": scope,
                    "idempotencyKey": key,
                    "existingJobId": str(job.id),
                },
            )
        # 同 key 同内容 → 返回既有任务。**不重复写事件**：
        # 事件已经在上一次入队时写过了，再写一条会让消费方看到两次同样的入队。
        return _view(job, created=False)

    repo.flush()
    repo.insert_outbox_event(
        aggregate_type=AGGREGATE_JOB,
        aggregate_id=job.id,
        event_type=EVENT_JOB_ENQUEUED,
        payload={
            "jobId": str(job.id),
            "kind": normalized_kind,
            "queue": normalized_queue,
            "projectId": str(project_id) if project_id else None,
        },
    )
    repo.flush()
    return _view(job, created=True)


# ---------------------------------------------------------------------------
# F3：领取、执行、写回
# ---------------------------------------------------------------------------


def claim_jobs(
    repo: JobRepository,
    *,
    queue: str,
    worker_id: str,
    limit: int = MAX_CLAIM_BATCH,
    now: datetime | None = None,
    lease_duration: timedelta | None = None,
) -> list[ClaimedJob]:
    """领取最多 `limit` 个可执行任务。

    **两步走而不是一条 UPDATE**：先 `SELECT ... FOR UPDATE SKIP LOCKED`
    挑出 id（跳过别人正锁着的），再逐条 `UPDATE ... RETURNING fence_token`
    拿到自己的令牌。一条 UPDATE 也能做，但拿不到"这次领到的是哪几个"
    与"每个的新 fence"的对应关系 —— 而 Worker 必须知道自己手里任务的令牌。

    **领取即建 attempt**（`job_attempts` 第 1 行）：这样"失败了几次"是
    可查的事实，而不是靠状态猜。
    """
    moment = now or repo.now()
    duration = lease_duration or timedelta(seconds=LEASE_DURATION_SECONDS)

    claimed: list[ClaimedJob] = []
    for job_id in repo.claim_candidates(queue=queue, limit=limit, now=moment):
        fence = repo.claim_job(
            job_id=job_id, worker_id=worker_id, lease_duration=duration, now=moment
        )
        if fence is None:
            # SELECT 与 UPDATE 之间被别的实例抢先了。含 SKIP LOCKED 时
            # 这仍可能发生（锁在 SELECT 事务内有效，但我们的 SELECT 与
            # UPDATE 在同一事务里 —— 因此实际只会在极端情况下出现）。
            # 不报错：跳过它，下一个。
            continue

        job = repo.get_job(job_id)
        assert job is not None
        attempt_no = repo.next_attempt_no(job_id)
        attempt = repo.insert_attempt(
            job_id=job_id,
            attempt_no=attempt_no,
            worker_id=worker_id,
            fence_token=fence,
        )
        repo.flush()
        claimed.append(
            ClaimedJob(
                job_id=job_id,
                kind=job.kind,
                # attempt 的数据库 id（WP16 的 `provider_calls.job_attempt_id`
                # 要指向它）。`flush()` 之后已经取得到 —— 主键由 PG 生成并用
                # RETURNING 取回（`uuid_pk()` 的 server_default 就是为此）。
                attempt_id=attempt.id,
                fence_token=fence,
                attempt_no=attempt_no,
                worker_id=worker_id,
                input_version_ids=tuple(job.input_version_ids or ()),
            )
        )
    return claimed


def renew_lease(
    repo: JobRepository,
    *,
    claimed: ClaimedJob,
    now: datetime | None = None,
    lease_duration: timedelta | None = None,
) -> bool:
    """续租。返回 False 表示**我已经被取代**，应立刻停止工作。

    Worker 长任务的循环里必须定期调它 —— 不续租的话，
    租约到期后回收器会把任务交给别人，而此时我还在跑，
    结果是两人同时执行（fence 能保住数据不被写坏，但**外部副作用已经发生两次**）。
    """
    moment = now or repo.now()
    duration = lease_duration or timedelta(seconds=LEASE_DURATION_SECONDS)
    return repo.renew_lease(
        job_id=claimed.job_id,
        worker_id=claimed.worker_id,
        fence_token=claimed.fence_token,
        lease_duration=duration,
        now=moment,
    )


def finish_job(
    repo: JobRepository,
    *,
    claimed: ClaimedJob,
    outcome: HandlerOutcome,
    now: datetime | None = None,
) -> bool:
    """按处理器的申报落状态。返回**结果是否被采纳**。

    返回 False = 我的 fence 已过期（任务被别人接手了，或已被取消）。
    **这不是错误，也不该重试** —— 我的这次执行本来就不算数了。

    尝试记录**无论采纳与否都会被写**（"我跑过、结果如何"是排障需要的历史）。
    """
    moment = now or repo.now()
    target = _require_state(outcome.state, field="state")

    if target == STATE_WAITING_PROVIDER or target == STATE_WAITING_RECONCILIATION:
        # 等待态：保留租约（任务仍由我负责，只是此刻不占 CPU）。
        accepted = repo.move_to_waiting(
            job_id=claimed.job_id,
            fence_token=claimed.fence_token,
            to_state=target,
        )
    elif target in JOB_TERMINAL_STATES:
        accepted = repo.finish_job(
            job_id=claimed.job_id,
            fence_token=claimed.fence_token,
            # `CANCEL_REQUESTED` 也允许落终态：持有者看到取消标记后，
            # 应当停止工作并确认取消（§12 的 `CANCEL_REQUESTED → CANCELLED`）。
            # 少了它，"确认取消"会永远失败，任务卡在"已请求取消"这个中间态。
            from_states=[STATE_RUNNING, STATE_WAITING_PROVIDER, STATE_CANCEL_REQUESTED],
            to_state=target,
            result_ref=outcome.result_ref,
            error_code=outcome.error_code,
        )
    elif target == STATE_QUEUED:
        # 显式要求重排（处理器自己知道还没到重试的时候）。
        accepted = repo.requeue_job(
            job_id=claimed.job_id,
            fence_token=claimed.fence_token,
            next_run_at=moment,
        )
    else:
        raise _validation_error(
            "state",
            "处理器不能把任务直接置为该状态。",
            reason="state_not_allowed_from_handler",
            state=target,
        )

    attempt_state = _attempt_state_for(target)
    if attempt_state is not None:
        repo.finish_attempt(
            job_id=claimed.job_id,
            attempt_no=claimed.attempt_no,
            state=attempt_state,
            error_code=outcome.error_code,
            usage_pending=outcome.usage_pending or None,
        )
    repo.flush()
    return accepted


def _attempt_state_for(job_state: str) -> str | None:
    """任务状态 → 尝试状态；**返回 None 表示这次尝试不收尾**。

    `PARTIAL` 归到 SUCCEEDED：这次尝试**跑完了**（拿到了一部分结果），
    尝试级别的"成功"指"执行过程走完了"，不是"业务全部达成"。
    混淆两者会让排障时看到"尝试失败但任务 partial"这种读不通的组合。

    **等待态返回 None**：任务进入 `WAITING_PROVIDER` /
    `WAITING_RECONCILIATION` 时，它**仍由当前 Worker 负责**（租约保留），
    只是控制权交给了外部。此刻给 attempt 写"结束"会让记录与实际不符 ——
    真正的结束发生在它落到终态、或被回收器标为 ABANDONED 的时候。
    """
    if job_state in {STATE_WAITING_PROVIDER, STATE_WAITING_RECONCILIATION}:
        return None
    if job_state in {STATE_QUEUED, "FAILED"}:
        # QUEUED 表示"显式重排"，这次尝试没做成；FAILED 同理。
        # 两者都归 FAILED（**不是** ABANDONED）——
        # ABANDONED 专指"失联、不知是否仍在跑"，与"明确失败"处置不同。
        return ATTEMPT_FAILED
    return ATTEMPT_SUCCEEDED


def run_job(
    repo: JobRepository,
    registry: TaskHandlerRegistry,
    *,
    job_id: uuid.UUID,
    worker_id: str,
    now: datetime | None = None,
) -> RunReport:
    """领取指定任务并执行一次（Worker 的最小工作单元）。

    **只处理一个任务**，让调用方决定批量与并发 —— 与 `claim_jobs` 分开是
    刻意的：批量领取需要在一个事务里做完，而"执行"往往要开新事务
    （长任务不应一直占着锁）。
    """
    moment = now or repo.now()
    candidates = claim_jobs(
        repo,
        queue=require_job(repo, job_id).queue,
        worker_id=worker_id,
        limit=MAX_CLAIM_BATCH,
        now=moment,
    )
    claimed = next((item for item in candidates if item.job_id == job_id), None)
    if claimed is None:
        # 没领到：可能是已终态、被别人持有、或不到执行时间。
        # 都不是错误 —— 返回 idle 让调用方继续。
        return RunReport(job_id=None)

    outcome = _invoke_handler(registry, repo, claimed)
    accepted = finish_job(repo, claimed=claimed, outcome=outcome, now=moment)
    return RunReport(
        job_id=claimed.job_id,
        state=outcome.state,
        accepted=accepted,
        error_code=outcome.error_code,
    )


def _invoke_handler(
    registry: TaskHandlerRegistry, repo: JobRepository, claimed: ClaimedJob
) -> HandlerOutcome:
    """调用处理器并把异常折算成 FAILED（可重试）。

    **异常不向上抛**：任务执行失败是**预期内的常态**（网络抖、供应商 5xx），
    不是程序缺陷。把它抛出去会让 Worker 的主循环因为一个任务而中断 ——
    而正确行为是"这一个失败、继续下一个"。
    """
    handler = registry.handler_for(claimed.kind)
    if handler is None:
        # 没有处理器：**不重试**。重试一万次也不会突然出现一个处理器，
        # 而重试会把它变成噪声（且每次都要走一遍租约）。
        return HandlerOutcome(
            state="FAILED",
            error_code="NO_HANDLER",
            retryable=False,
        )
    try:
        return handler(claimed, repo)
    except Exception as exc:  # noqa: BLE001 - 处理器内部任何异常都算这次执行失败
        return HandlerOutcome(
            state="FAILED",
            error_code=type(exc).__name__,
            retryable=True,
            usage_pending=True,
        )


# ---------------------------------------------------------------------------
# F4：UNKNOWN 恢复
# ---------------------------------------------------------------------------


def pause_for_reconciliation(
    repo: JobRepository, *, claimed: ClaimedJob, error_code: str = "UNKNOWN"
) -> bool:
    """把任务挂起等待核对（外部调用状态未知）。

    **这是本包唯一正确的 UNKNOWN 处置**。数据结构 §13.4 原文：

    > 租约只防本地并发，**不保证供应商已停工**；
    > UNKNOWN **不因租约过期变成可自由重试**。

    因此 UNKNOWN 不能"重新入队再试一次"（那可能发出第二次计费请求），
    而必须进入一个**需要显式核对才能离开**的状态。
    本函数只做状态迁移；核对本身由 `reconcile_job` 完成。
    """
    return finish_job(
        repo,
        claimed=claimed,
        outcome=HandlerOutcome(
            state=STATE_WAITING_RECONCILIATION,
            error_code=error_code,
            usage_pending=True,
        ),
    )


def reconcile_job(
    repo: JobRepository,
    *,
    job_id: uuid.UUID,
    resolution_outcome: HandlerOutcome,
    now: datetime | None = None,
) -> bool:
    """核对完成：把 `WAITING_RECONCILIATION` 的任务落到一个终态。

    **调用方必须先真的查过外部状态**（这就是"先查询再决定重试"）。
    本函数不校验"你查了吗" —— 那无法自动验证；
    它只保证**状态机不允许从等待态直接跳回 QUEUED 重试**
    （见 `finish_job`：等待态只能走向终态或显式 requeue）。

    **`requeue` 在这里是被禁止的**：从 `WAITING_RECONCILIATION` 回到
    `QUEUED` 就等于"自由重试"，而那正是 §13.4 要防的。
    若核对结论是"确实没成功、可以重试"，正确做法是新建一个任务
    （新的幂等键），让"重试"成为一次**显式的业务决定**而不是自动行为。
    """
    job = require_job(repo, job_id, for_update=True)
    if job.state != STATE_WAITING_RECONCILIATION:
        raise ApiError(
            ErrorCode.INVALID_TRANSITION,
            f"当前状态（{job.state}）不需要核对。",
            details={
                "reason": "not_awaiting_reconciliation",
                "state": job.state,
                "jobId": str(job_id),
            },
        )

    target = _require_state(resolution_outcome.state, field="state")
    if target == STATE_QUEUED:
        raise _validation_error(
            "state",
            "核对后不允许直接重排任务；如确需重试，请以新的幂等键新建任务。",
            reason="reconcile_cannot_requeue",
        )

    result = repo.finish_job(
        job_id=job_id,
        fence_token=job.fence_token,
        from_states=[STATE_WAITING_RECONCILIATION],
        to_state=target,
        result_ref=resolution_outcome.result_ref,
        error_code=resolution_outcome.error_code,
    )
    if result:
        repo.flush()
    return result


def request_cancel(repo: JobRepository, *, job_id: uuid.UUID) -> bool:
    """请求取消。返回 False 表示任务已是终态（无需取消）。

    **只登记意图，不直接落终态**（§12：`CANCEL_REQUESTED → CANCELLED`）。
    持有者看到这个标记后停止工作并确认取消；
    若持有者已失联，则由 `reclaim_expired_leases` 落到 CANCELLED。
    """
    require_job(repo, job_id)
    changed = repo.mark_cancel_requested(job_id=job_id, now=repo.now())
    repo.flush()
    return changed


# ---------------------------------------------------------------------------
# 过期租约回收
# ---------------------------------------------------------------------------


def reclaim_expired_leases(
    repo: JobRepository, *, now: datetime | None = None, limit: int = MAX_CLAIM_BATCH
) -> ReclaimReport:
    """回收过期租约的任务。

    **两种处置**：

      · **已申请取消**（`CANCEL_REQUESTED`）→ 直接落 `CANCELLED`（终态）；
      · **其余**（`RUNNING`）→ **一律**转 `WAITING_RECONCILIATION` 等核对，
        **绝不自己放回 `QUEUED` 重试**。

    第二条是刻意的保守策略，依据是数据结构 §13.4 原文：

    > 租约只防本地并发，**不保证供应商已停工**；
    > UNKNOWN 不因租约过期变成可自由重试。

    一个 Worker 失联时，**我们不知道它有没有已经发出计费请求**。
    "失联"与"没在跑"是两件事，而重试的成本可能是重复计费 ——
    两害相权，宁可让任务停在等待核对的显式状态里让人看一眼。

    代价是**领了任务还没来得及调外部就崩了**的任务也要等核对。
    这是可接受的：等待核对可以走"确认外部确实没动过 → 以新幂等键新建任务"，
    而重复计费的损失无法撤销。

    真正的"供应商是否在跑"要看 `provider_calls`（WP16）——
    本包只能处理"状态可判断"的部分。
    """
    moment = now or repo.now()
    cancelled = 0
    waiting = 0
    abandoned_total = 0

    for job in repo.list_expired_leases(now=moment, limit=limit):
        # 标量先取，后面会走 UPDATE（执行后 ORM 实例可能被 expire）。
        job_id = job.id
        fence_token = job.fence_token
        was_cancel_requested = job.state == STATE_CANCEL_REQUESTED

        # 先把这次失联的尝试标为 ABANDONED（"失联"而非"失败"）。
        # 标记发生在状态迁移之前 —— 迁移之后任务已不归这个尝试了。
        abandoned_total += repo.abandon_running_attempts(job_id=job_id)

        if was_cancel_requested:
            if repo.finish_job(
                job_id=job_id,
                fence_token=fence_token,
                from_states=[STATE_CANCEL_REQUESTED],
                to_state="CANCELLED",
            ):
                cancelled += 1
            continue

        if repo.move_to_waiting(
            job_id=job_id,
            fence_token=fence_token,
            to_state=STATE_WAITING_RECONCILIATION,
            # **必须递增 fence**：否则失联的 Worker 回来后仍能续租成功
            # 并写回结果，覆盖掉这次回收 —— 那正是本包要防的
            # "旧 Worker 晚返回覆盖新状态"，只不过入口是回收器。
            bump_fence=True,
        ):
            waiting += 1

    repo.flush()
    return ReclaimReport(
        cancelled=cancelled,
        awaiting_reconciliation=waiting,
        abandoned_attempts=abandoned_total,
    )


# ---------------------------------------------------------------------------
# 幂等（协议 §1.3）—— 供各业务包使用
# ---------------------------------------------------------------------------


def begin_idempotent_request(
    repo: JobRepository,
    *,
    scope: str,
    key: str,
    request_hash: str,
    permanent: bool = False,
    now: datetime | None = None,
) -> IdempotencyDecision:
    """开始一次可能被重放的请求。

    三种结论（协议 §1.3）：

      · `NEW` —— 首次，调用方继续执行，完成后调 `complete_idempotent_request`；
      · `REPLAY` —— 同 key 同内容且已完成，调用方**回放** `record` 里的结果；
      · `CONFLICT` —— 同 key 不同内容，调用方报 409 `IDEMPOTENCY_CONFLICT`。

    `permanent=True` 用于**生产任务类**请求：`expires_at` 置 NULL = 永久保留。
    协议原文："生产任务还以数据库业务请求标识永久关联，
    避免缓存过期造成同一任务再次计费"。
    """
    moment = now or repo.now()
    expires_at = (
        None
        if permanent
        else moment + timedelta(days=IDEMPOTENCY_RETENTION_DAYS)
    )

    record, created = repo.insert_idempotency(
        scope=scope,
        key=key,
        request_hash=request_hash,
        state=IDEMPOTENCY_IN_PROGRESS,
        expires_at=expires_at,
    )
    repo.flush()

    # **`created` 是最可靠的判据**：`ON CONFLICT DO NOTHING` 插入成功，
    # 就说明"这是我第一次见到这个 key"。
    #
    # 用 `record.state == IN_PROGRESS` 去推断是**错的** ——
    # 刚插入的记录本来就是 IN_PROGRESS，那会把首次调用误判成
    # "另一个调用正在进行中"，于是调用方永远不执行、请求静默地什么都不做。
    # （本机实测：`test_idempotency_new_then_replay` 第一次就拿到 REPLAY。）
    if created:
        return IdempotencyDecision(outcome="NEW", record=record)

    if record.request_hash != request_hash:
        return IdempotencyDecision(outcome="CONFLICT", record=record)

    # 到这里有两种：已完成（回放结果）或另一路调用正在进行中。
    # 协议要求"进行中的同 key 返回同一任务，不新增任务"——
    # 调用方拿到记录后返回它已关联的资源，不再执行一次。
    return IdempotencyDecision(outcome="REPLAY", record=record)


def complete_idempotent_request(
    repo: JobRepository,
    *,
    scope: str,
    key: str,
    response_status: int,
    response_json: dict[str, Any] | None = None,
    resource_id: uuid.UUID | None = None,
) -> bool:
    """记录首次执行的结果，供后续重放。"""
    done = repo.complete_idempotency(
        scope=scope,
        key=key,
        response_status=response_status,
        response_json=response_json,
        resource_id=resource_id,
    )
    repo.flush()
    return done


__all__ = [
    "MAX_ATTEMPTS",
    "ClaimedJob",
    "HandlerOutcome",
    "IdempotencyDecision",
    "JobView",
    "ReclaimReport",
    "RunReport",
    "begin_idempotent_request",
    "claim_jobs",
    "complete_idempotent_request",
    "enqueue_job",
    "finish_job",
    "pause_for_reconciliation",
    "reclaim_expired_leases",
    "reconcile_job",
    "renew_lease",
    "request_cancel",
    "require_job",
    "run_job",
    "to_envelope",
]
