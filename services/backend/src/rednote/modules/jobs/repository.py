"""任务、尝试与幂等记录的存储访问（WP13）。

**本层最要紧的两件事**：

1. **领取任务用 `FOR UPDATE SKIP LOCKED`**。
   多实例同时领取时，不能互相阻塞（否则派发器变成串行），也不能领到同一批
   （否则同一任务被两个 Worker 同时执行）。`SKIP LOCKED` 正好表达
   "跳过别人正在处理的，拿自己那一份"。

2. **写结果必须带 `AND fence_token = :mine`**。
   这是数据结构 §13.4「写结果时必须匹配当前 token」的落地。
   若用"先查再判再写"，两个 Worker 的写回之间就有窗口 ——
   而那正是本包要防的竞争（旧 Worker 晚返回覆盖新状态）。

   **不用乐观锁那套"读出来比对再提交"**：那种模式要求调用方持有旧值，
   而本包的竞争发生在**两个独立的 Worker 进程**之间，
   它们各自持有的是自己领取时的 fence，比对必须发生在数据库里。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from rednote.modules.jobs.models import (
    ATTEMPT_ABANDONED,
    ATTEMPT_RUNNING,
    IDEMPOTENCY_COMPLETED,
    JOB_TERMINAL_STATES,
    Job,
    JobAttempt,
    IdempotencyRecord,
)
from rednote.modules.outbox.models import OutboxEvent


class JobRepository:
    """任务与幂等记录的读写入口。事务边界由调用方（service）控制。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        """本仓库持有的 session。**供任务处理器访问同事务资源**（WP17 起）。

        **为什么必须公开它**：`TaskHandlerFn` 的签名是
        `(ClaimedJob, JobRepository) -> HandlerOutcome`。下游包的处理器
        通常还要写自己的表（WP17 写 `parse_runs` / `source_segments`）。
        它只有三个选择：

          · 让 `JobRepository` 提供"造下游仓库"的方法 —— 依赖方向反了
            （任务框架不该知道解析模块的存在）；
          · 自己 `session_scope()` 开新事务 —— 于是**任务终态与业务结果
            落在两个事务里**，中间崩溃会留下"片段已提交但任务仍是 RUNNING"；
          · 用同一个 session 造出自己的仓库 —— 一个事务，无中间态。

        公开 session 是第三种做法的前提。

        **与 WP16 删掉 `GatewayRepository.session` 的区别**：那一个是为
        缓解某个具体问题（ISS-47 的抽象错配）而临时加的，问题消失后
        它就没有调用方了；这一个的调用方是**每一个**需要写自己表的下游处理器，
        属稳定契约。删它等于要求每个下游包自己开事务。
        """
        return self._session

    def flush(self) -> None:
        """把待写变更立即发给数据库，让约束错误在预期位置抛出。

        与 WP10/WP12 的做法一致：等到 `session_scope` 退出时才提交，
        错误会离开能给出可读 4xx 的代码位置，只剩 500。
        """
        self._session.flush()

    def now(self) -> datetime:
        """取数据库时间。

        **不取 Python 的 `datetime.now()`**：租约判定依赖"服务端认为的现在"。
        多个实例的时钟可能偏差（NTP 抖动、容器时区），
        用客户端时间会让"租约是否过期"在不同实例上有不同答案 ——
        一个说没过期、另一个说该接手了，于是两人同时执行。
        数据库时间是唯一权威。
        """
        return self._session.execute(select(func.now())).scalar_one()

    # ------------------------------------------------------------------
    # 任务：创建与查询
    # ------------------------------------------------------------------

    def insert_job(
        self,
        *,
        kind: str,
        queue: str,
        state: str,
        idempotency_scope: str,
        idempotency_key: str,
        input_hash: str,
        project_id: uuid.UUID | None = None,
        batch_id: uuid.UUID | None = None,
        content_id: uuid.UUID | None = None,
        priority: int = 0,
        input_version_ids: Sequence[uuid.UUID] = (),
        next_run_at: datetime | None = None,
    ) -> tuple[Job, bool]:
        """插入任务；幂等键冲突时返回既有任务，`created=False`。

        **为什么用 `ON CONFLICT DO NOTHING` + 回查，而不是"先查再插"**：
        协议 §1.3 要求"进行中的同 key 返回同一任务，不新增任务"。
        并发下两个请求会同时发现"不存在"，各自插入 —— 其中一个撞唯一约束。
        那会变成 500，而这件事在语义上完全正常（用户超时重试）。
        """
        stmt = (
            pg_insert(Job)
            .values(
                kind=kind,
                queue=queue,
                state=state,
                idempotency_scope=idempotency_scope,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                project_id=project_id,
                batch_id=batch_id,
                content_id=content_id,
                priority=priority,
                input_version_ids=list(input_version_ids),
                next_run_at=next_run_at,
            )
            .on_conflict_do_nothing(
                index_elements=["idempotency_scope", "idempotency_key"]
            )
            .returning(Job.id)
        )
        created_id = self._session.execute(stmt).scalar_one_or_none()
        if created_id is not None:
            job = self._session.get(Job, created_id)
            assert job is not None  # 同一事务内刚插入，必然可见
            return job, True

        existing = self._session.execute(
            select(Job).where(
                Job.idempotency_scope == idempotency_scope,
                Job.idempotency_key == idempotency_key,
            )
        ).scalar_one()
        return existing, False

    def get_job(self, job_id: uuid.UUID, *, for_update: bool = False) -> Job | None:
        return self._session.get(Job, job_id, with_for_update=for_update)

    def find_job(
        self, *, idempotency_scope: str, idempotency_key: str
    ) -> Job | None:
        return self._session.execute(
            select(Job).where(
                Job.idempotency_scope == idempotency_scope,
                Job.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()

    # ------------------------------------------------------------------
    # 任务：领取与租约
    # ------------------------------------------------------------------

    def claim_candidates(
        self, *, queue: str, limit: int, now: datetime
    ) -> list[uuid.UUID]:
        """挑出可领取的任务 id（带行锁，跳过已被别人锁住的）。

        `skip_locked=True` 是本方法的**全部意义**：
        没有它，两个派发器实例会阻塞在彼此的锁上、或者（更糟）拿到同一批。
        """
        stmt = (
            select(Job.id)
            .where(
                Job.queue == queue,
                Job.state == "QUEUED",
                (Job.next_run_at.is_(None)) | (Job.next_run_at <= now),
            )
            # 优先级高的先做；同优先级按入队顺序（FIFO）。
            .order_by(Job.priority.desc(), Job.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(self._session.execute(stmt).scalars().all())

    def claim_job(
        self, *, job_id: uuid.UUID, worker_id: str, lease_duration: timedelta, now: datetime
    ) -> int | None:
        """把一个任务领取到手，返回**新的 fence_token**；不可领取返回 None。

        条件里带 `state = 'QUEUED'`：即使调用方误传了一个已在跑的 id，
        也不会把别人的任务抢走（`rowcount = 0`）。

        `fence_token = fence_token + 1` **在数据库里做加法**，不在 Python 里
        算好写回去 —— 后者会把并发的两次领取算成同一个值。
        """
        stmt = (
            update(Job)
            .where(Job.id == job_id, Job.state == "QUEUED")
            .values(
                state="RUNNING",
                lease_owner=worker_id,
                lease_until=now + lease_duration,
                fence_token=Job.fence_token + 1,
                updated_at=func.now(),
            )
            .returning(Job.fence_token)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def renew_lease(
        self,
        *,
        job_id: uuid.UUID,
        worker_id: str,
        fence_token: int,
        lease_duration: timedelta,
        now: datetime,
    ) -> bool:
        """续租。只允许当前持有者续（owner + fence 双条件）。

        带 `fence_token` 的条件不是为了防"旧 Worker 续租成功"（owner 条件
        已经挡住了），而是为了**让"我已被取代"这件事显式可察觉** ——
        续租失败即应停止工作，而不是继续算下去再把结果丢掉。
        """
        result = self._session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.lease_owner == worker_id,
                Job.fence_token == fence_token,
                Job.state.not_in(sorted(JOB_TERMINAL_STATES)),
            )
            .values(lease_until=now + lease_duration, updated_at=func.now())
        )
        return result.rowcount == 1

    def list_expired_leases(self, *, now: datetime, limit: int) -> list[Job]:
        """租约已过期、且仍被某个 Worker 持有的任务（回收的输入）。

        **捞两个状态**：
          · `RUNNING` —— 正在跑但持有者失联；
          · `CANCEL_REQUESTED` —— 申请了取消但持有者失联，
            需要回收器替它落终态（否则任务会永远卡在"已请求取消"）。

        **不捞 `WAITING_PROVIDER` / `WAITING_RECONCILIATION`**：
        它们表示"外部还有事没落地"，不因租约过期而被抢（数据结构 §13.4）。
        """
        stmt = (
            select(Job)
            .where(
                Job.state.in_(["RUNNING", "CANCEL_REQUESTED"]),
                Job.lease_until.is_not(None),
                Job.lease_until < now,
            )
            .order_by(Job.lease_until)
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars().all())

    def abandon_running_attempts(self, *, job_id: uuid.UUID) -> int:
        """把该任务仍在 `RUNNING` 的尝试标为 `ABANDONED`，返回条数。

        **ABANDONED 与 FAILED 必须分开**（见 models 的说明）：
        FAILED 表示"明确失败了，可以重试"；
        ABANDONED 表示"失联，不知道它是否仍在跑、是否已产生外部副作用"。
        把失联写成失败，会让回收逻辑误以为可以安全重试。
        """
        result = self._session.execute(
            update(JobAttempt)
            .where(
                JobAttempt.job_id == job_id,
                JobAttempt.state == ATTEMPT_RUNNING,
            )
            .values(
                state=ATTEMPT_ABANDONED,
                ended_at=func.now(),
                # 失联的尝试**一律标记为有未结用量**：无法确定它有没有
                # 发出外部请求，按"可能有"处理让 WP25 去核对。
                # 反过来（按"没有"处理）会让真实产生的费用无声消失。
                usage_pending=True,
            )
        )
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------
    # 任务：写回结果（fence 受保护）
    # ------------------------------------------------------------------

    def finish_job(
        self,
        *,
        job_id: uuid.UUID,
        fence_token: int,
        from_states: Sequence[str],
        to_state: str,
        result_ref: str | None = None,
        error_code: str | None = None,
    ) -> bool:
        """把任务推进到终态（或等待态）。**必须匹配 fence_token**。

        返回 False 有两种含义，调用方都应视为"我的结果不被承认"：
          · fence 已过期（别人接手了）；
          · 状态已被改过（不满足 `from_states`）。
        两者都不该报错给用户 —— 它们正是本包要正确处理的情形。

        `input_hash` 等只读字段不动；`lease_*` 一并清空
        （任务不再被任何 Worker 持有）。
        """
        result = self._session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.fence_token == fence_token,
                Job.state.in_(list(from_states)),
            )
            .values(
                state=to_state,
                result_ref=result_ref,
                error_code=error_code,
                lease_owner=None,
                lease_until=None,
                updated_at=func.now(),
            )
        )
        return result.rowcount == 1

    def move_to_waiting(
        self,
        *,
        job_id: uuid.UUID,
        fence_token: int,
        to_state: str,
        bump_fence: bool = False,
    ) -> bool:
        """把任务移到等待态（`WAITING_PROVIDER` / `WAITING_RECONCILIATION`）。

        **保留租约**（不清 `lease_*`）：等待态仍然"被某个 Worker 负责"，
        只是它此刻不消耗 CPU。清空会让它对别的 Worker 看起来像无主任务。

        **`bump_fence=True` 供回收路径使用** —— 这一步不能省：

        回收只改状态的话，失联的 Worker 一旦回来，它的 `fence_token`
        仍然**匹配**（回收没有递增它），于是：
          · `renew_lease` 会成功 → 它以为自己还在负责，继续干下去；
          · 更糟的是它的 `finish_job` 也会成功 → **覆盖掉回收后的状态**。

        这正是切片 3 要防的"旧 Worker 晚返回覆盖新状态"，
        只不过入口不是"被别人接手"，而是"被回收器接手"。
        递增 fence 之后，旧 Worker 的续租与写回**一起失效**。
        （本机实测踩到：`test_renew_lease_fails_after_losing_ownership`
        在递增之前是失败的。）
        """
        values: dict[str, Any] = {"state": to_state, "updated_at": func.now()}
        if bump_fence:
            values["fence_token"] = Job.fence_token + 1
        result = self._session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.fence_token == fence_token,
                Job.state == "RUNNING",
            )
            .values(**values)
        )
        return result.rowcount == 1

    def requeue_job(
        self, *, job_id: uuid.UUID, fence_token: int, next_run_at: datetime
    ) -> bool:
        """把任务放回 `QUEUED` 等待重试（带退避时间）。"""
        result = self._session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.fence_token == fence_token,
                Job.state == "RUNNING",
            )
            .values(
                state="QUEUED",
                next_run_at=next_run_at,
                lease_owner=None,
                lease_until=None,
                updated_at=func.now(),
            )
        )
        return result.rowcount == 1

    def mark_cancel_requested(self, *, job_id: uuid.UUID, now: datetime) -> bool:
        """请求取消。**只改标记，不直接改状态**。

        §12 的状态机是 `CANCEL_REQUESTED → CANCELLED`：取消要先被登记，
        再由持有者（或回收器）落到终态。直接置 CANCELLED 会让"任务正在执行
        且已产生外部副作用"这件事失去记录 —— 而 §13.4 明确说
        租约不保证外部已停工。
        """
        result = self._session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.state.not_in([*sorted(JOB_TERMINAL_STATES), "CANCEL_REQUESTED"]),
            )
            .values(
                state="CANCEL_REQUESTED",
                cancel_requested_at=now,
                updated_at=func.now(),
            )
        )
        return result.rowcount == 1

    def confirm_cancelled(self, *, job_id: uuid.UUID, fence_token: int) -> bool:
        """把 `CANCEL_REQUESTED` 落成 `CANCELLED`（由持有者或回收器调用）。"""
        result = self._session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.fence_token == fence_token,
                Job.state == "CANCEL_REQUESTED",
            )
            .values(
                state="CANCELLED",
                lease_owner=None,
                lease_until=None,
                updated_at=func.now(),
            )
        )
        return result.rowcount == 1

    # ------------------------------------------------------------------
    # 执行尝试
    # ------------------------------------------------------------------

    def next_attempt_no(self, job_id: uuid.UUID) -> int:
        """下一个尝试序号（第一次为 1）。

        用 `max + 1` 而不是 `count + 1`：尝试记录只增不删，
        但万一将来做了归档清理，count 会算错。
        """
        current = self._session.execute(
            select(func.coalesce(func.max(JobAttempt.attempt_no), 0)).where(
                JobAttempt.job_id == job_id
            )
        ).scalar_one()
        return int(current) + 1

    def insert_attempt(
        self,
        *,
        job_id: uuid.UUID,
        attempt_no: int,
        worker_id: str,
        fence_token: int,
        state: str = ATTEMPT_RUNNING,
    ) -> JobAttempt:
        attempt = JobAttempt(
            job_id=job_id,
            attempt_no=attempt_no,
            worker_id=worker_id,
            fence_token=fence_token,
            state=state,
        )
        self._session.add(attempt)
        return attempt

    def finish_attempt(
        self,
        *,
        job_id: uuid.UUID,
        attempt_no: int,
        state: str,
        error_code: str | None = None,
        usage_pending: bool | None = None,
    ) -> bool:
        """给一次尝试收尾。

        **不带 fence 条件**：尝试记录是"历史事实"，它就该被写完整 ——
        即使这次尝试的结果不被采纳（fence 过期），
        "它跑过、它失败了"这件事本身仍是排障需要的。
        被保护的是 `jobs`（当前状态权威），不是历史记录。
        """
        values: dict[str, Any] = {
            "state": state,
            "error_code": error_code,
            "ended_at": func.now(),
        }
        if usage_pending is not None:
            values["usage_pending"] = usage_pending
        result = self._session.execute(
            update(JobAttempt)
            .where(
                JobAttempt.job_id == job_id,
                JobAttempt.attempt_no == attempt_no,
            )
            .values(**values)
        )
        return result.rowcount == 1

    def list_attempts(self, job_id: uuid.UUID) -> list[JobAttempt]:
        stmt = (
            select(JobAttempt)
            .where(JobAttempt.job_id == job_id)
            .order_by(JobAttempt.attempt_no)
        )
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # Outbox（WP12 建的表，本包做消费者）
    # ------------------------------------------------------------------

    def insert_outbox_event(
        self,
        *,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> OutboxEvent:
        """与业务变更**同事务**写入事件（切片 F1 的全部意义）。

        **调用方不加 commit**：由外层事务统一提交，这样"任务落库"与
        "事件落库"要么都在、要么都不在。若在这里 commit，
        就退化成"先提交业务、再单独写事件"——而那正是 Outbox 要消灭的窗口。
        """
        event = OutboxEvent(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
        )
        self._session.add(event)
        return event

    def list_pending_events(self, *, limit: int, now: datetime) -> list[OutboxEvent]:
        """待投递的事件。

        条件 `next_attempt_at IS NULL OR <= now` 让"投递失败后带退避重试"
        成为可能；`attempts` 的累加由 `mark_publish_failed` 负责。

        用 `FOR UPDATE SKIP LOCKED`：多实例的派发器同时捞时不能拿到同一批，
        否则同一条事件会被投递两次（虽然消费侧幂等能吸收，
        但那是在用下游的复杂度掩盖上游的浪费）。
        """
        stmt = (
            select(OutboxEvent)
            .where(
                OutboxEvent.published_at.is_(None),
                (OutboxEvent.next_attempt_at.is_(None))
                | (OutboxEvent.next_attempt_at <= now),
            )
            .order_by(OutboxEvent.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(self._session.execute(stmt).scalars().all())

    def mark_published(self, *, event_id: uuid.UUID) -> bool:
        """标记已投递。**必须在 publish 成功之后调用**（顺序不可颠倒）。"""
        result = self._session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id, OutboxEvent.published_at.is_(None))
            .values(published_at=func.now(), next_attempt_at=None)
        )
        return result.rowcount == 1

    def mark_publish_failed(
        self, *, event_id: uuid.UUID, next_attempt_at: datetime
    ) -> bool:
        """投递失败：累加次数并把下次尝试时间推后（退避）。

        **不设 attempts 上限**：达上限就放弃意味着"事件永久丢失"，
        而 Outbox 存在的意义正是不丢。次数只用于告警与排障。
        """
        result = self._session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id, OutboxEvent.published_at.is_(None))
            .values(
                attempts=OutboxEvent.attempts + 1,
                next_attempt_at=next_attempt_at,
            )
        )
        return result.rowcount == 1

    # ------------------------------------------------------------------
    # 幂等记录（协议 §1.3）
    # ------------------------------------------------------------------

    def get_idempotency(
        self, *, scope: str, key: str, for_update: bool = False
    ) -> IdempotencyRecord | None:
        return self._session.get(
            IdempotencyRecord, (scope, key), with_for_update=for_update
        )

    def insert_idempotency(
        self,
        *,
        scope: str,
        key: str,
        request_hash: str,
        state: str,
        expires_at: datetime | None,
    ) -> tuple[IdempotencyRecord, bool]:
        """插入幂等记录；已存在则返回既有记录（`created=False`）。

        与 `insert_job` 同一手法：`ON CONFLICT DO NOTHING` + 回查。
        并发下的两个同 key 请求，一个插入成功、另一个拿到既有行，
        然后由服务层判定"内容是否一致"（不一致 → 409）。
        """
        stmt = (
            pg_insert(IdempotencyRecord)
            .values(
                scope=scope,
                key=key,
                request_hash=request_hash,
                state=state,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(index_elements=["scope", "key"])
            .returning(IdempotencyRecord.scope)
        )
        created = self._session.execute(stmt).scalar_one_or_none()
        record = self.get_idempotency(scope=scope, key=key)
        assert record is not None  # 刚插入或本来就有
        return record, created is not None

    def complete_idempotency(
        self,
        *,
        scope: str,
        key: str,
        response_status: int,
        response_json: dict | None,
        resource_id: uuid.UUID | None,
    ) -> bool:
        """记录首次执行的结果，供后续重放。"""
        result = self._session.execute(
            update(IdempotencyRecord)
            .where(IdempotencyRecord.scope == scope, IdempotencyRecord.key == key)
            .values(
                state=IDEMPOTENCY_COMPLETED,
                response_status=response_status,
                response_json=response_json,
                resource_id=resource_id,
                updated_at=func.now(),
            )
        )
        return result.rowcount == 1

    def delete_expired_idempotency(self, *, now: datetime) -> int:
        """清理过期的幂等记录。

        **只删有到期时间的**（`expires_at IS NOT NULL`）——
        任务关联的记录是永久保留的（协议 §1.3），删了会导致同一任务再次计费。
        """
        result = self._session.execute(
            IdempotencyRecord.__table__.delete().where(
                IdempotencyRecord.expires_at.is_not(None),
                IdempotencyRecord.expires_at <= now,
            )
        )
        return int(result.rowcount or 0)


__all__ = ["JobRepository"]
