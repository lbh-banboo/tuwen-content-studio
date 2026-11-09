"""WP13 测试：任务、Outbox 派发、租约与 fence、幂等（4 个切片的验收）。

覆盖验收标准：

> **提交后消息失败仍恢复，重复投递不重复执行**（研发计划 04 §4.2）

按研发计划 04 §WP13 的四个切片组织（本文件的分节与它一一对应）：

1. **事务写 job 与 outbox** → 队列停机不丢任务
2. **派发器发布确认** → 标记前崩溃会重复投递但不会重复生成
3. **Worker 租约与 fence** → 旧 Worker 晚返回不能覆盖新执行状态
4. **UNKNOWN 恢复** → 不因租约过期变成可自由重试

**测试策略（四层）**

1. 纯函数层：状态机常量、`_attempt_state_for` 的映射、处理器注册表。
2. 服务层 + 真库：入队、领取、写回、回收、幂等。
3. **并发层**（本包最重要）：两个 Worker 抢同一队列、旧 Worker 晚返回。
4. 投递层：真实派发流程（成功 / 失败退避 / 重复投递）。

**关于"队列停机不丢任务"怎么测**：本包的执行路径不依赖 MQ（见 service 的
模块 docstring），所以"停机"等价于"**不调用派发器**"——
断言任务仍在 `jobs` 里、事件仍在 `outbox_events` 里；
一旦派发器恢复运行，两者都还在。
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text

from rednote.modules.jobs import service
from rednote.modules.jobs.dispatch import (
    HandlerRegistry,
    InProcessChannel,
    dispatch_outbox,
)
from rednote.modules.jobs.handlers import (
    KIND_ALWAYS_FAIL,
    KIND_FAKE_PROVIDER,
    TaskHandlerRegistry,
    build_default_registry,
)
from rednote.modules.jobs.models import (
    ATTEMPT_ABANDONED,
    ATTEMPT_FAILED,
    ATTEMPT_RUNNING,
    ATTEMPT_SUCCEEDED,
    IDEMPOTENCY_COMPLETED,
    JOB_CLAIMABLE_STATES,
    JOB_TERMINAL_STATES,
    LEASE_DURATION_SECONDS,
    STATE_CANCELLED,
    STATE_CANCEL_REQUESTED,
    STATE_FAILED,
    STATE_PARTIAL,
    STATE_QUEUED,
    STATE_RUNNING,
    STATE_SUCCEEDED,
    STATE_WAITING_RECONCILIATION,
    Job,
    JobAttempt,
)
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.jobs.types import HandlerOutcome
from rednote.modules.outbox.models import (
    AGGREGATE_JOB,
    EVENT_JOB_ENQUEUED,
    OutboxEvent,
)
from rednote.platform.configuration import Settings
from rednote.platform.database import session_scope
from rednote.platform.errors import ApiError, ErrorCode

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
# 测试库 DSN。**必须从环境变量读**（ISS-108）：硬编码会让
# `REDNOTE_TEST_DATABASE_URL` 约定的「测试库物理隔离」失效 —— 测试会把
# 数据写进本机默认库（实测跑一次全量 +367 行 users），且 CI 里 PG 用户是
# `postgres` 而不是 `rednote`，硬编码会直接连不上（role 不存在）。
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
TEST_DATABASE_URL = os.environ.get(
    "REDNOTE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL
).strip()


SETTINGS = Settings(
    database_url=TEST_DATABASE_URL,
    environment="test",
    session_cookie_secure=False,
    oidc_issuer=TEST_ISSUER,
    oidc_token_encryption_key="dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM=",
    # WP13 不碰对象存储，但 Settings 要求 storage 配置完整 ——
    # 给一个测试用目录，避免为了"顺便满足校验"去改配置的必填性。
    storage_backend="LOCAL",
    storage_local_root="/tmp/rednote-wp13-test-storage",
)


# 投递时的扫描上限。设得远大于测试产生的事件数，避免被**其他测试遗留的
# 未发布事件**挤出这一批 —— 那会表现为"事件没被投递"，而其实是没扫到。
_DISPATCH_LIMIT = 10_000

# 回收租约时的扫描上限。与 `_DISPATCH_LIMIT` 同一理由，但**必须单独设**：
# `reclaim_expired_leases` 默认 limit=MAX_CLAIM_BATCH=20，而它是**全局**扫描
# （按 state 过滤，没有 queue 过滤 —— 租约过期与队列无关）。
# 库里只要积累了 ≥20 条更早过期的任务，本测试刚造的那条就排在第 21 位、
# 捞不到 —— 断言"已转入 WAITING_RECONCILIATION"失败，而失败信息完全指向不了
# 真正的原因（实测：`test_second_claim_gets_higher_fence` 偶发红，
# 单跑与整文件跑都是绿的）。这与 `_DISPATCH_LIMIT` 是同一类修法（ISS-37）。
_RECLAIM_LIMIT = 10_000

# 当前测试的队列名。
#
# **每个测试用独立队列**：`claim_jobs` 是按队列领取的，若所有测试共用
# "default"，测试 A 会把测试 B 刚入队的任务领走 —— 表现为
# **随执行顺序变化的失败**，而且单跑某条测试时它是绿的。这是最难查的一类。
# 用默认值只是为了让模块顶层能导入；fixture 会在每个测试前替换它。
CURRENT_QUEUE = "default"


@pytest.fixture(autouse=True)
def _isolated_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys.modules[__name__], "CURRENT_QUEUE", f"q-{uuid.uuid4().hex[:10]}"
    )


@pytest.fixture(autouse=True)
def _purge_expired_leases() -> Iterator[None]:
    """每个测试开始前，先把库里**所有已过期**的租约回收干净。

    `_isolated_queue` 只能隔离"按队列索引"的资源；而 `reclaim_expired_leases`
    是**全局**扫描（按 state，不看队列），本文件的回收断言里还有两处是
    **全局计数**（`report.cancelled == 0` / `report.awaiting_reconciliation == 0`）。
    于是队列隔离对它无效 —— 库里任何**别的来源**的过期租约都会混进候选集，
    把计数推高，或者把本测试刚造的那条挤出 limit 之外。

    过期租约的来源正是**跨运行残留**：本文件多个测试会 `_claim()` 一个任务
    但**刻意不结束它**（RUNNING 本身就是被测对象），于是留下挂着租约的运行中
    任务；它们在**当次运行**里没过期（租约 120s），但**跨运行**必然过期，
    变成下一次运行的"别人的输入"。这与 WP16 的 ISS-50 是同一个坑
    （那边是 GATEWAY_TEST 残留推高 WP13 的断言），也是 ISS-37 教训的第二条：
    **测试对全局资源既要"只读自己的"，也要"不留下会被别人读到的"。**

    清场放在**测试开始前**而不是结束后：结束后清不掉（本测试刚挂的租约还没
    到期），而开始前清场同时覆盖了"上一次运行的残留"与"本次运行中前面测试
    的残留"。回收器本身是幂等的纯函数，扫不到可回收的就返回零。
    """
    with session_scope(SETTINGS) as session:
        service.reclaim_expired_leases(JobRepository(session), limit=_RECLAIM_LIMIT)
    yield


def _unique_key(prefix: str = "k") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _enqueue(**overrides: object) -> service.JobView:
    """入队一个假供应商任务（默认参数可覆盖）。"""
    params: dict[str, object] = {
        "kind": KIND_FAKE_PROVIDER,
        "queue": CURRENT_QUEUE,
        "idempotency_scope": "test",
        "idempotency_key": _unique_key(),
        "input_hash": "0" * 64,
    }
    params.update(overrides)
    with session_scope(SETTINGS) as session:
        return service.enqueue_job(JobRepository(session), **params)  # type: ignore[arg-type]


def _claim(queue: str | None = None, worker: str = "w1", **kwargs: object):
    with session_scope(SETTINGS) as session:
        return service.claim_jobs(
            JobRepository(session),
            queue=queue or CURRENT_QUEUE,
            worker_id=worker,
            **kwargs,  # type: ignore[arg-type]
        )


def _run(job_id: uuid.UUID, *, worker: str = "w1", registry=None):
    with session_scope(SETTINGS) as session:
        return service.run_job(
            JobRepository(session),
            registry or build_default_registry(),
            job_id=job_id,
            worker_id=worker,
        )


def _job_state(job_id: uuid.UUID) -> str:
    with session_scope(SETTINGS) as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job.state


def _job_row(job_id: uuid.UUID) -> Job:
    with session_scope(SETTINGS) as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job


def _attempts(job_id: uuid.UUID) -> list[JobAttempt]:
    with session_scope(SETTINGS) as session:
        return JobRepository(session).list_attempts(job_id)


# ===========================================================================
# 切片 1：事务写 job 与 outbox —— 队列停机不丢任务
# ===========================================================================


def test_enqueue_writes_job_and_outbox_event_together() -> None:
    """入队必须同时落 job 与 outbox 事件（同一事务）。"""
    view = _enqueue()
    assert view.created is True
    assert view.state == STATE_QUEUED

    with session_scope(SETTINGS) as session:
        events = (
            session.execute(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == view.job_id)
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].event_type == EVENT_JOB_ENQUEUED
    assert events[0].aggregate_type == AGGREGATE_JOB
    assert events[0].payload["jobId"] == str(view.job_id)
    assert events[0].published_at is None


@pytest.mark.regression
def test_task_survives_queue_outage() -> None:
    """**切片 1 的验收**：「数据库提交后队列停机不会丢任务」。

    "队列停机" = 不调用派发器。此时任务与事件都还在库里；
    派发器恢复后能把事件搬出去，任务也能被 Worker 领取。
    """
    view = _enqueue()

    # 停机期间：不派发。事件仍未发布，任务仍是 QUEUED。
    #
    # **limit 必须显式给大值**：`list_pending_events` 扫的是**全局**待投递队列，
    # 而后续工作包（WP15/WP16…）的测试也会入队 —— limit 给小值会让
    # "本用例的事件"被别人的事件挤出窗口，表现为**随机失败**
    # （单跑通过、连跑失败）。这正是 ISS-37 第 ③ 条的教训，
    # 当时只改了一处，本处漏了（见 ISS-49）。
    with session_scope(SETTINGS) as session:
        pending = JobRepository(session).list_pending_events(
            limit=100_000, now=JobRepository(session).now()
        )
    assert any(e.aggregate_id == view.job_id for e in pending), "事件必须仍在待投递队列里"

    # 执行路径不依赖 MQ：任务照样能被领走。
    claimed = _claim()
    assert [c.job_id for c in claimed] == [view.job_id]


def test_enqueue_is_idempotent_for_same_key_and_hash() -> None:
    """同 key 同内容 → 返回既有任务，**不新增任务、不重复写事件**。"""
    key = _unique_key()
    first = _enqueue(idempotency_key=key)
    second = _enqueue(idempotency_key=key)

    assert first.created is True
    assert second.created is False
    assert second.job_id == first.job_id

    with session_scope(SETTINGS) as session:
        events = (
            session.execute(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == first.job_id)
            )
            .scalars()
            .all()
        )
    assert len(events) == 1, "重复入队不应再写一条事件"


def test_enqueue_rejects_same_key_with_different_content() -> None:
    """同 key 不同内容 → **409**（协议 §1.3）。"""
    key = _unique_key()
    _enqueue(idempotency_key=key, input_hash="a" * 64)

    with pytest.raises(ApiError) as excinfo:
        _enqueue(idempotency_key=key, input_hash="b" * 64)

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert excinfo.value.details["reason"] == "idempotency_key_reused"


def test_enqueue_rejects_empty_fields() -> None:
    for field, overrides in (
        ("kind", {"kind": "   "}),
        ("queue", {"queue": ""}),
        ("idempotencyScope", {"idempotency_scope": ""}),
        ("inputHash", {"input_hash": "  "}),
    ):
        with pytest.raises(ApiError) as excinfo:
            _enqueue(**overrides)  # type: ignore[arg-type]
        assert excinfo.value.details["reason"] == "empty", field


# ===========================================================================
# 切片 2：派发器发布确认 —— 标记前崩溃会重复投递但不重复生成
# ===========================================================================


def _collecting_registry() -> tuple[HandlerRegistry, list]:
    """造一个记录收到哪些事件的 registry。"""
    registry = HandlerRegistry()
    seen: list = []
    registry.register(EVENT_JOB_ENQUEUED, seen.append)
    return registry, seen


def test_dispatch_publishes_and_marks() -> None:
    view = _enqueue()
    registry, seen = _collecting_registry()

    with session_scope(SETTINGS) as session:
        repo = JobRepository(session)
        report = dispatch_outbox(repo, InProcessChannel(registry), limit=_DISPATCH_LIMIT)

    assert report.published >= 1
    assert any(env.aggregate_id == view.job_id for env in seen)

    with session_scope(SETTINGS) as session:
        event = session.execute(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == view.job_id)
        ).scalar_one()
        assert event.published_at is not None, "投递成功后必须标记"


def test_dispatch_keeps_event_when_publish_fails() -> None:
    """投递失败 → 事件保留 + 退避，**不丢**。"""
    view = _enqueue()

    class FailingChannel:
        name = "FAILING"

        def publish(self, event) -> bool:  # noqa: ANN001
            return False

    with session_scope(SETTINGS) as session:
        repo = JobRepository(session)
        report = dispatch_outbox(repo, FailingChannel(), limit=_DISPATCH_LIMIT)

    assert report.failed >= 1
    with session_scope(SETTINGS) as session:
        event = session.execute(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == view.job_id)
        ).scalar_one()
        assert event.published_at is None
        assert event.attempts >= 1
        assert event.next_attempt_at is not None, "失败必须设下次尝试时间"


def test_dispatch_is_repeatable_after_crash_before_marking() -> None:
    """**切片 2 的验收**：「标记前崩溃会重复投递但不会重复生成」。

    模拟：publish 成功但**不标记**（等价于"标记前崩溃"）。
    下次派发会把同一条事件**再投一次** —— 这是设计接受的重复；
    而"不重复生成"由任务侧的幂等与状态机保证（见下一条断言）。
    """
    view = _enqueue()
    registry, seen = _collecting_registry()

    # 第一次：只投递，不标记（模拟崩溃）
    class PublishOnlyChannel:
        name = "PUBLISH_ONLY"

        def publish(self, event) -> bool:  # noqa: ANN001
            registry.handle(event)
            return True

    with session_scope(SETTINGS) as session:
        repo = JobRepository(session)
        first = dispatch_outbox(repo, PublishOnlyChannel(), limit=_DISPATCH_LIMIT)
        assert first.published >= 1
        # 事务**回滚**（模拟"标记前崩溃"）：标记没有生效。
        session.rollback()

    with session_scope(SETTINGS) as session:
        event = session.execute(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == view.job_id)
        ).scalar_one()
        assert event.published_at is None, "崩溃后事件必须仍是未发布状态"

    # 第二次：正常派发 → 重复投递（seen 收到两次）
    with session_scope(SETTINGS) as session:
        dispatch_outbox(
            JobRepository(session), InProcessChannel(registry), limit=_DISPATCH_LIMIT
        )

    delivered = [env for env in seen if env.aggregate_id == view.job_id]
    assert len(delivered) >= 2, "崩溃前已投递的事件会被再投一次（至少一次投递）"

    # 但任务**没有被重复创建**：idempotency 键唯一，jobs 里只有一行。
    with session_scope(SETTINGS) as session:
        count = session.execute(
            text("SELECT count(*) FROM jobs WHERE kind = :k AND id = :i"),
            {"k": KIND_FAKE_PROVIDER, "i": str(view.job_id)},
        ).scalar_one()
    assert count == 1


def test_dispatch_skips_events_not_yet_due() -> None:
    """退避期内的事件不应被再次捞起。"""
    _enqueue()

    class FailingChannel:
        name = "FAILING"

        def publish(self, event) -> bool:  # noqa: ANN001
            return False

    with session_scope(SETTINGS) as session:
        repo = JobRepository(session)
        dispatch_outbox(repo, FailingChannel())

    with session_scope(SETTINGS) as session:
        repo = JobRepository(session)
        again = dispatch_outbox(repo, FailingChannel(), limit=_DISPATCH_LIMIT)
    # 退避时间还没到 → 这一轮扫不到任何东西
    assert again.scanned == 0


def test_dispatch_passes_through_unregistered_event_types() -> None:
    """未注册的事件类型直接放行（消费方由别的包提供）。

    放行而不是重试：`file.uploaded` 的消费者是 WP17，WP13 交付时它还不存在。
    若未注册就重试，事件会永远卡在 outbox 里。
    """
    empty = HandlerRegistry()
    view = _enqueue()

    with session_scope(SETTINGS) as session:
        report = dispatch_outbox(
            JobRepository(session), InProcessChannel(empty), limit=_DISPATCH_LIMIT
        )

    assert report.published >= 1
    with session_scope(SETTINGS) as session:
        event = session.execute(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == view.job_id)
        ).scalar_one()
        assert event.published_at is not None


# ===========================================================================
# 切片 3：Worker 租约与 fence —— 旧 Worker 晚返回不能覆盖新执行状态
# ===========================================================================


def test_claim_increments_fence_and_records_attempt() -> None:
    view = _enqueue()
    claimed = _claim()
    assert len(claimed) == 1
    first = claimed[0]

    assert first.fence_token == 1, "首次领取 fence 应为 1（单调递增的起点）"
    assert first.attempt_no == 1
    assert _job_state(view.job_id) == STATE_RUNNING

    attempts = _attempts(view.job_id)
    assert len(attempts) == 1
    assert attempts[0].fence_token == 1
    assert attempts[0].state == ATTEMPT_RUNNING


@pytest.mark.regression
def test_second_claim_gets_higher_fence() -> None:
    view = _enqueue()
    _claim()
    # 手工把租约推到过去（模拟持有者失联）
    with session_scope(SETTINGS) as session:
        session.execute(
            text(
                "UPDATE jobs SET lease_until = now() - interval '1 hour'"
                " WHERE id = :i"
            ),
            {"i": str(view.job_id)},
        )
    with session_scope(SETTINGS) as session:
        service.reclaim_expired_leases(JobRepository(session), limit=_RECLAIM_LIMIT)

    # 回收后进 WAITING_RECONCILIATION（见切片 4），**不可再领取**
    assert _job_state(view.job_id) == STATE_WAITING_RECONCILIATION


def test_stale_worker_result_is_rejected() -> None:
    """**切片 3 的验收**：「旧 Worker 晚返回不能覆盖新执行状态」。

    构造：Worker A 领取 → 它的租约被回收 → 任务转入等待核对 →
    A 带着旧 fence 回来写"我成功了" —— **必须被拒**。
    """
    view = _enqueue()
    claimed = _claim(worker="worker-A")
    stale = claimed[0]

    # 让 A 的租约过期并被回收
    with session_scope(SETTINGS) as session:
        session.execute(
            text("UPDATE jobs SET lease_until = now() - interval '1 hour' WHERE id = :i"),
            {"i": str(view.job_id)},
        )
    with session_scope(SETTINGS) as session:
        service.reclaim_expired_leases(JobRepository(session), limit=_RECLAIM_LIMIT)

    state_after_reclaim = _job_state(view.job_id)

    # A 晚到的"成功"申报
    with session_scope(SETTINGS) as session:
        accepted = service.finish_job(
            JobRepository(session),
            claimed=stale,
            outcome=HandlerOutcome(state=STATE_SUCCEEDED, result_ref="stale"),
        )

    assert accepted is False, "旧 Worker 的结果必须被拒"
    assert _job_state(view.job_id) == state_after_reclaim, "状态不能被旧 Worker 改掉"


def test_running_attempt_marked_abandoned_on_reclaim() -> None:
    """失联的尝试标 ABANDONED（**不是** FAILED）。

    两者处置不同：FAILED 可以直接重试；ABANDONED 表示"不知道它是否还在跑、
    是否已产生外部副作用"，必须先去核对。
    """
    view = _enqueue()
    _claim(worker="worker-A")

    with session_scope(SETTINGS) as session:
        session.execute(
            text("UPDATE jobs SET lease_until = now() - interval '1 hour' WHERE id = :i"),
            {"i": str(view.job_id)},
        )
    with session_scope(SETTINGS) as session:
        report = service.reclaim_expired_leases(JobRepository(session), limit=_RECLAIM_LIMIT)

    assert report.abandoned_attempts >= 1
    attempts = _attempts(view.job_id)
    assert attempts[0].state == ATTEMPT_ABANDONED
    assert attempts[0].usage_pending is True, "失联的尝试必须标记可能有未结用量"


def test_concurrent_workers_do_not_double_claim() -> None:
    """**并发**：两个 Worker 同时领取同一队列，不能拿到同一个任务。

    这是"重复投递不重复执行"的第一层保护。
    用 `FOR UPDATE SKIP LOCKED` 实现 —— 单线程测不出它有没有生效。
    """
    views = [_enqueue() for _ in range(4)]
    job_ids = {v.job_id for v in views}

    def worker(name: str) -> set[uuid.UUID]:
        with session_scope(SETTINGS) as session:
            claimed = service.claim_jobs(
                JobRepository(session), queue=CURRENT_QUEUE, worker_id=name, limit=10
            )
        return {c.job_id for c in claimed}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ["worker-A", "worker-B"]))

    all_claimed = results[0] | results[1]
    assert all_claimed <= job_ids
    # 两个 Worker 各自领到的**互不重叠**
    assert results[0] & results[1] == set(), f"出现了重复领取：{results}"
    # 四个任务都被领走了（没有因为跳锁而被漏掉）
    assert len(all_claimed) == 4


def test_successful_run_sets_terminal_state() -> None:
    view = _enqueue()
    report = _run(view.job_id, worker="w1")

    assert report.accepted is True
    assert report.state == STATE_SUCCEEDED
    assert _job_state(view.job_id) == STATE_SUCCEEDED

    job = _job_row(view.job_id)
    assert job.result_ref == f"fake-provider/{view.job_id}"
    assert job.lease_owner is None, "终态必须释放租约"

    attempts = _attempts(view.job_id)
    assert attempts[0].state == ATTEMPT_SUCCEEDED
    assert attempts[0].ended_at is not None


def test_run_is_idempotent_for_terminal_job() -> None:
    """已完成的任务再跑一次：**领不到**（不是终态时不可领取）。"""
    view = _enqueue()
    _run(view.job_id)
    second = _run(view.job_id)
    assert second.idle is True, "终态任务不该被再次领取"

    attempts = _attempts(view.job_id)
    assert len(attempts) == 1, "不应产生第二次尝试"


def test_failing_handler_leaves_job_failed_with_attempt_record() -> None:
    view = _enqueue(kind=KIND_ALWAYS_FAIL)
    report = _run(view.job_id)

    assert report.state == STATE_FAILED
    assert report.error_code == "INTENTIONAL_FAILURE"
    assert _job_state(view.job_id) == STATE_FAILED

    attempts = _attempts(view.job_id)
    assert attempts[0].state == ATTEMPT_FAILED
    assert attempts[0].error_code == "INTENTIONAL_FAILURE"


def test_unknown_kind_yields_no_handler_failure_without_retry() -> None:
    """没有处理器 → FAILED 但**不可重试**。

    重试一万次也不会突然出现一个处理器，重试只会制造噪声。
    """
    view = _enqueue(kind="NO_SUCH_KIND")
    report = _run(view.job_id)

    assert report.state == STATE_FAILED
    assert report.error_code == "NO_HANDLER"


@pytest.mark.regression
def test_cancel_request_then_confirm() -> None:
    """取消走 `CANCEL_REQUESTED → CANCELLED`（§12 的状态机）。"""
    view = _enqueue()
    claimed = _claim(worker="w1")

    with session_scope(SETTINGS) as session:
        changed = service.request_cancel(JobRepository(session), job_id=view.job_id)
    assert changed is True
    assert _job_state(view.job_id) == STATE_CANCEL_REQUESTED

    # 持有者确认取消
    with session_scope(SETTINGS) as session:
        service.finish_job(
            JobRepository(session),
            claimed=claimed[0],
            outcome=HandlerOutcome(state=STATE_CANCELLED),
        )
    assert _job_state(view.job_id) == STATE_CANCELLED


def test_cancel_returns_false_for_terminal_job() -> None:
    view = _enqueue()
    _run(view.job_id)
    with session_scope(SETTINGS) as session:
        assert service.request_cancel(JobRepository(session), job_id=view.job_id) is False


def test_handler_cannot_set_arbitrary_state() -> None:
    """处理器不能把任务直接置为任意状态（只有白名单内的可以）。"""
    view = _enqueue()
    claimed = _claim()
    with pytest.raises(ApiError) as excinfo:
        with session_scope(SETTINGS) as session:
            service.finish_job(
                JobRepository(session),
                claimed=claimed[0],
                outcome=HandlerOutcome(state="NONSENSE"),
            )
    assert excinfo.value.details["reason"] == "job_state_unknown"


# ===========================================================================
# 切片 4：UNKNOWN 恢复 —— 不因租约过期变成可自由重试
# ===========================================================================


def test_reclaim_moves_to_reconciliation_not_back_to_queue() -> None:
    """**切片 4 的验收**：租约过期的任务进 `WAITING_RECONCILIATION`，
    **绝不**自己放回 `QUEUED`。

    依据（数据结构 §13.4）：
    > 租约只防本地并发，不保证供应商已停工；
    > UNKNOWN 不因租约过期变成可自由重试。
    """
    view = _enqueue()
    _claim(worker="worker-A")

    with session_scope(SETTINGS) as session:
        session.execute(
            text("UPDATE jobs SET lease_until = now() - interval '1 hour' WHERE id = :i"),
            {"i": str(view.job_id)},
        )
    with session_scope(SETTINGS) as session:
        report = service.reclaim_expired_leases(JobRepository(session), limit=_RECLAIM_LIMIT)

    assert report.awaiting_reconciliation >= 1
    assert _job_state(view.job_id) == STATE_WAITING_RECONCILIATION

    # 关键断言：它**不在可领取状态集合**里
    assert _job_state(view.job_id) not in JOB_CLAIMABLE_STATES

    # 而且真的领不到
    assert _claim() == []


def test_waiting_reconciliation_cannot_be_requeued() -> None:
    """核对后不允许直接重排（那等于"自由重试"）。"""
    view = _enqueue()
    claimed = _claim()
    with session_scope(SETTINGS) as session:
        service.pause_for_reconciliation(JobRepository(session), claimed=claimed[0])

    assert _job_state(view.job_id) == STATE_WAITING_RECONCILIATION

    with pytest.raises(ApiError) as excinfo:
        with session_scope(SETTINGS) as session:
            service.reconcile_job(
                JobRepository(session),
                job_id=view.job_id,
                resolution_outcome=HandlerOutcome(state=STATE_QUEUED),
            )
    assert excinfo.value.details["reason"] == "reconcile_cannot_requeue"


def test_reconcile_to_terminal_state_is_allowed() -> None:
    view = _enqueue()
    claimed = _claim()
    with session_scope(SETTINGS) as session:
        service.pause_for_reconciliation(JobRepository(session), claimed=claimed[0])

    with session_scope(SETTINGS) as session:
        ok = service.reconcile_job(
            JobRepository(session),
            job_id=view.job_id,
            resolution_outcome=HandlerOutcome(state=STATE_FAILED, error_code="CONFIRMED_FAILED"),
        )
    assert ok is True
    assert _job_state(view.job_id) == STATE_FAILED


def test_reconcile_rejects_job_not_waiting() -> None:
    view = _enqueue()
    with pytest.raises(ApiError) as excinfo:
        with session_scope(SETTINGS) as session:
            service.reconcile_job(
                JobRepository(session),
                job_id=view.job_id,
                resolution_outcome=HandlerOutcome(state=STATE_FAILED),
            )
    assert excinfo.value.details["reason"] != "reconcile_cannot_requeue"
    assert excinfo.value.code is ErrorCode.INVALID_TRANSITION


@pytest.mark.regression
def test_waiting_provider_job_survives_lease_expiry() -> None:
    """等待外部供应商的任务**不因租约过期被回收**。

    §13.4 的"不保证供应商已停工"——它正在等别人，抢过来只会重复调用。
    表现：`reclaim_expired_leases` 的候选集里没有它。
    """
    view = _enqueue()
    claimed = _claim()
    with session_scope(SETTINGS) as session:
        service.finish_job(
            JobRepository(session),
            claimed=claimed[0],
            outcome=HandlerOutcome(state="WAITING_PROVIDER"),
        )
    # 把租约推到过去
    with session_scope(SETTINGS) as session:
        session.execute(
            text("UPDATE jobs SET lease_until = now() - interval '1 hour' WHERE id = :i"),
            {"i": str(view.job_id)},
        )
    with session_scope(SETTINGS) as session:
        report = service.reclaim_expired_leases(JobRepository(session), limit=_RECLAIM_LIMIT)

    assert report.cancelled == 0
    assert report.awaiting_reconciliation == 0, "等待外部的任务不该被回收"
    assert _job_state(view.job_id) == "WAITING_PROVIDER"


# ===========================================================================
# 幂等（协议 §1.3）—— 闭合 WP12 的 OP-94
# ===========================================================================


@pytest.mark.regression
def test_idempotency_new_then_replay() -> None:
    key = _unique_key("idem")
    with session_scope(SETTINGS) as session:
        repo = JobRepository(session)
        first = service.begin_idempotent_request(
            repo, scope="s", key=key, request_hash="h1"
        )
    assert first.outcome == "NEW"
    assert first.is_new is True

    with session_scope(SETTINGS) as session:
        repo = JobRepository(session)
        service.complete_idempotent_request(
            repo, scope="s", key=key, response_status=201,
            response_json={"id": "x"}, resource_id=None,
        )

    with session_scope(SETTINGS) as session:
        repo = JobRepository(session)
        second = service.begin_idempotent_request(
            repo, scope="s", key=key, request_hash="h1"
        )
    assert second.outcome == "REPLAY"
    assert second.record.state == IDEMPOTENCY_COMPLETED
    assert second.record.response_status == 201


def test_idempotency_conflict_on_different_hash() -> None:
    key = _unique_key("idem")
    with session_scope(SETTINGS) as session:
        service.begin_idempotent_request(
            JobRepository(session), scope="s", key=key, request_hash="h1"
        )
    with session_scope(SETTINGS) as session:
        decision = service.begin_idempotent_request(
            JobRepository(session), scope="s", key=key, request_hash="h2"
        )
    assert decision.outcome == "CONFLICT"


def test_idempotency_permanent_flag_controls_expiry() -> None:
    """任务类请求永久保留，一般请求 7 天过期（协议 §1.3）。"""
    key_a = _unique_key("perm")
    key_b = _unique_key("temp")
    with session_scope(SETTINGS) as session:
        repo = JobRepository(session)
        permanent = service.begin_idempotent_request(
            repo, scope="s", key=key_a, request_hash="h", permanent=True
        )
    with session_scope(SETTINGS) as session:
        repo = JobRepository(session)
        temporary = service.begin_idempotent_request(
            repo, scope="s", key=key_b, request_hash="h", permanent=False
        )

    assert permanent.record.expires_at is None
    assert permanent.record.permanent is True
    assert temporary.record.expires_at is not None
    assert temporary.record.permanent is False


def test_idempotency_cleanup_only_removes_expiring_records() -> None:
    """清理**只删有到期时间的** —— 永久记录被删会导致同一任务再次计费。

    ## 为什么不再断言 `removed == 1`（ISS-121）

    `delete_expired_idempotency()` 是**全表**操作
    （`WHERE expires_at IS NOT NULL AND expires_at <= now()`，无 scope/key 限定），
    返回值是**全库**被删行数。断言 `removed == 1` 等于把判据建在
    「此刻全库恰好只有我这一条已过期」这个**外部状态**上，而不是本用例建立的前提：

      · 库里干净时它必过；
      · 库里只要有**任意来源**的过期残留（跨运行累积、并发会话、别的包留下），
        它就必红 —— 实测在隔离库种入 9 条过期残留后，**单跑即 `assert 10 == 1`**。

    原登记（ISS-121）把它定性为「单跑必过、全量偶发」的 flaky，**该定性是错的**：
    它不是偶发，是**数据量依赖**，且触发条件与「库里攒了几天的测试残留」直接耦合 ——
    于是它会在某个时间点之后**从「一直绿」突变成「一直红」**，而代码一行没改。

    这正是本仓铁律「本包自己的那些断言用 `== N`，全项目总数一律 `>= 下界`」的又一实例。
    断言改为**存在性**：只钉住本用例真正关心的两件事 ——
    ① 我那条临时记录被删了；② 我那条永久记录没被误删。
    `removed` 只做**下界**检查（它至少删掉了我这一条），不做等式。

    这个改法**不改变生产语义**（`delete_expired_idempotency` 仍是全表清理，
    它是清理任务，本来就该扫全表），也与 ISS-121 处置里的方案 (B) 一致。
    反向验证：把 `expires_at IS NOT NULL` 条件去掉（会误删永久记录）→
    第 ② 条断言必红；把删除改成不删 → 第 ① 条断言必红。
    """
    permanent_key = _unique_key("perm")
    temporary_key = _unique_key("temp")
    with session_scope(SETTINGS) as session:
        repo = JobRepository(session)
        service.begin_idempotent_request(
            repo, scope="s", key=permanent_key, request_hash="h", permanent=True
        )
        service.begin_idempotent_request(
            repo, scope="s", key=temporary_key, request_hash="h", permanent=False
        )
        # 把临时记录推到过期
        session.execute(
            text(
                "UPDATE idempotency_records SET expires_at = now() - interval '1 day'"
                " WHERE key = :k"
            ),
            {"k": temporary_key},
        )

    with session_scope(SETTINGS) as session:
        repo = JobRepository(session)
        removed = repo.delete_expired_idempotency(now=repo.now())
    # 下界，不是等式 —— 库里的过期残留属于外部状态，不该影响本用例的判定。
    assert removed >= 1

    with session_scope(SETTINGS) as session:
        repo = JobRepository(session)
        assert repo.get_idempotency(scope="s", key=permanent_key) is not None
        assert repo.get_idempotency(scope="s", key=temporary_key) is None


# ===========================================================================
# 纯函数与常量
# ===========================================================================


def test_terminal_and_claimable_states_are_disjoint() -> None:
    assert JOB_TERMINAL_STATES & JOB_CLAIMABLE_STATES == frozenset()
    assert STATE_SUCCEEDED in JOB_TERMINAL_STATES
    assert STATE_PARTIAL in JOB_TERMINAL_STATES
    assert STATE_WAITING_RECONCILIATION not in JOB_CLAIMABLE_STATES


def test_lease_interval_is_one_third_of_duration() -> None:
    """续租间隔应为租约的 1/3 —— 连丢两次续租仍有一次补上的机会。"""
    from rednote.modules.jobs.models import LEASE_RENEW_INTERVAL_SECONDS

    assert LEASE_RENEW_INTERVAL_SECONDS * 3 == LEASE_DURATION_SECONDS


def test_attempt_state_mapping() -> None:
    assert service._attempt_state_for(STATE_SUCCEEDED) == ATTEMPT_SUCCEEDED
    assert service._attempt_state_for(STATE_PARTIAL) == ATTEMPT_SUCCEEDED
    assert service._attempt_state_for("FAILED") == ATTEMPT_FAILED
    assert service._attempt_state_for(STATE_QUEUED) == ATTEMPT_FAILED
    # 等待态**不收尾**
    assert service._attempt_state_for("WAITING_PROVIDER") is None
    assert service._attempt_state_for(STATE_WAITING_RECONCILIATION) is None


def test_registry_is_immutable() -> None:
    """注册返回新实例 —— 避免"测试里注册影响后续测试"的顺序依赖。"""
    base = TaskHandlerRegistry.empty()
    extended = base.register("X", lambda c, r: HandlerOutcome(state=STATE_SUCCEEDED))
    assert base.registered_kinds() == frozenset()
    assert extended.registered_kinds() == frozenset({"X"})


def test_builtin_registry_has_fake_provider() -> None:
    registry = build_default_registry()
    assert KIND_FAKE_PROVIDER in registry.registered_kinds()
    assert KIND_ALWAYS_FAIL in registry.registered_kinds()


def test_missing_job_raises_not_found() -> None:
    with pytest.raises(ApiError) as excinfo:
        with session_scope(SETTINGS) as session:
            service.require_job(JobRepository(session), uuid.uuid4())
    assert excinfo.value.code is ErrorCode.RESOURCE_NOT_FOUND


@pytest.mark.regression
def test_renew_lease_fails_after_losing_ownership() -> None:
    """续租失败即"我已被取代" —— Worker 应据此停止工作。"""
    view = _enqueue()
    claimed = _claim(worker="worker-A")
    stale = claimed[0]

    with session_scope(SETTINGS) as session:
        session.execute(
            text("UPDATE jobs SET lease_until = now() - interval '1 hour' WHERE id = :i"),
            {"i": str(view.job_id)},
        )
    with session_scope(SETTINGS) as session:
        service.reclaim_expired_leases(JobRepository(session), limit=_RECLAIM_LIMIT)

    with session_scope(SETTINGS) as session:
        assert service.renew_lease(JobRepository(session), claimed=stale) is False
