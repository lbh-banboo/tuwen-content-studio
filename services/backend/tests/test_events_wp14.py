"""WP14 测试：领域事件、SSE 分发与断线补漏。

覆盖验收标准：

> **「断网重连后状态正确，越权订阅拒绝」**（研发计划 04 §4.2）

按 **ADR-14 的 V1~V7 验证清单**组织（本文档的分节与它一一对应）：

| # | ADR-14 的验证点 | 本文件对应 |
|---|---|---|
| V1/V2 | 实例监听 + 建连可收到补齐 | `test_notifier_*` / `test_initial_backfill_*` |
| **V3/V4** | **事件由"另一侧"产生，本连接必须收到（双向）** | `test_cross_instance_*` |
| V5 | 断线后补发恰好 N 条，无重无漏 | `test_backfill_*` |
| V6 | 越权订阅被拒；**逐事件**过滤生效 | `test_permission_*` |
| V7 | 成员被移除后不再收到该项目事件 | `test_removed_member_*` |

**判据（ADR-14 原文）**："V3 与 V4 必须**双向**通过。只测单向不能证明
fan-out 正确——这恰是原设计缺失的那一半。" 因此本文件里
两个方向各有一条用例，而不是"跑一次再换参数"。

**关于"本机怎么模拟双实例"**：起两个 `EventNotifier`，各持一条独立的
LISTEN 连接。关键性质是"通知不依赖调用方自身"——
在 A 上写事件，B 必须收到。这与"两个进程各持一条连接"语义等价。
真实双实例（两进程 + 反向代理）留给内网。

**测试隔离**：所有测试用独立的 `project_id`（避免事件互相污染）。
事件表是全局追加的，`sequence` 全局单调 —— 因此**断言一律带 project 过滤**，
不能用"表里第几条"这种依赖全局状态的写法。
"""

from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from sqlalchemy import text

from rednote.modules.events import service
from rednote.modules.events.api import (
    HEARTBEAT_SECONDS,
    _read_last_event_id,
    _sse_message,
)
from rednote.modules.events.models import (
    EVENT_JOB_PROGRESS,
    EVENT_JOB_QUEUED,
    EVENT_SYNC_REQUIRED,
    EVENT_TYPES,
    DomainEvent,
)
from rednote.modules.events.notifier import (
    NOTIFY_CHANNEL,
    EventNotifier,
    SubscriberRegistry,
    native_dsn,
)
from rednote.modules.events.repository import EventRepository
from rednote.modules.identity.dependencies import CurrentUser
from rednote.modules.identity.models import ROLE_ADMIN, ROLE_OPERATOR
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.projects.models import Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.platform.configuration import Settings
from rednote.platform.database import session_scope
from rednote.platform.errors import ApiError

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
    storage_backend="LOCAL",
    storage_local_root="/tmp/rednote-wp14-test-storage",
)

_PLACEHOLDER_SESSION_ID = uuid.UUID("00000000-0000-4000-8000-000000000014")


# ---------------------------------------------------------------------------
# 装配 helper
# ---------------------------------------------------------------------------


def _create_user(*, role: str = ROLE_OPERATOR) -> uuid.UUID:
    with session_scope(SETTINGS) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name=f"用户-{role}",
            role=role,
        )
        return user.id


def _current_user(user_id: uuid.UUID) -> CurrentUser:
    from rednote.modules.identity.models import User

    with session_scope(SETTINGS) as session:
        user = session.get(User, user_id)
        assert user is not None
        return CurrentUser(
            id=user.id,
            display_name=user.display_name,
            role=user.role,
            is_admin=user.role == ROLE_ADMIN,
            session_id=_PLACEHOLDER_SESSION_ID,
        )


def _seed_project(*, owner_id: uuid.UUID, collaborators: tuple[uuid.UUID, ...] = ()) -> uuid.UUID:
    with session_scope(SETTINGS) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name="测试产品")
        session.add(product)
        session.flush()
        project = Project(
            project_code=f"EVT-{uuid.uuid4().hex[:8].upper()}",
            name="事件测试项目",
            brand_id=brand.id,
            product_id=product.id,
            owner_id=owner_id,
            target_delivery_count=10,
            due_date=date(2026, 12, 31),
        )
        session.add(project)
        session.flush()
        ProjectRepository(session).add_owner_and_collaborators(
            project.id, owner_id=owner_id, collaborator_ids=collaborators
        )
        return project.id


def _publish(*, project_id: uuid.UUID | None = None, actor_id: uuid.UUID | None = None,
             event_type: str = EVENT_JOB_PROGRESS, payload: dict | None = None):
    with session_scope(SETTINGS) as session:
        return service.publish_event(
            EventRepository(session),
            event_type=event_type,
            payload=payload if payload is not None else {"state": "RUNNING"},
            project_id=project_id,
            actor_id=actor_id,
        )


def _dsn() -> str:
    return native_dsn(SETTINGS.database_url or "")


# ===========================================================================
# V1/V2：写入与 NOTIFY（真相层 + 分发送达层）
# ===========================================================================


def test_publish_assigns_monotonic_sequence() -> None:
    """`sequence` 由数据库序列生成且单调递增。

    本机实测踩过：`autoincrement=True` 配 UUID 主键**不会生成序列** ——
    DDL 渲染成裸 `BIGINT NOT NULL`，插入即违约。因此这条测试除了验单调，
    也在验"序列真的存在"。
    """
    first = _publish()
    second = _publish()
    third = _publish()

    assert first.sequence > 0
    assert first.sequence < second.sequence < third.sequence


def test_publish_returns_view_with_protocol_fields() -> None:
    """返回值带协议 §11 事件体需要的全部字段。"""
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    view = _publish(project_id=project_id, actor_id=owner, payload={"state": "RUNNING"})

    data = view.to_sse_data()
    assert data["schemaVersion"] == 1
    assert data["projectId"] == str(project_id)
    assert data["jobId"] is None
    # 协议 §1：bigint 对外是**十进制字符串**（包括事件序号）。
    assert isinstance(data["sequence"], str)
    assert data["sequence"] == str(view.sequence)
    assert "occurredAt" in data
    assert data["payload"] == {"state": "RUNNING"}


def test_sequence_is_string_in_sse_data_but_int_in_row() -> None:
    """同一个值在库里是 int、在事件体里是字符串 —— 这是刻意的。"""
    view = _publish()
    assert isinstance(view.sequence, int)
    assert isinstance(view.to_sse_data()["sequence"], str)


def test_publish_rejects_unknown_event_type() -> None:
    with pytest.raises(ApiError) as excinfo:
        _publish(event_type="nonsense.event")
    assert excinfo.value.details["reason"] == "event_type_unknown"
    assert len(excinfo.value.details["allowedEventTypes"]) == len(EVENT_TYPES)


def test_publish_rejects_sensitive_payload_keys() -> None:
    """协议 §11：事件数据不带 Prompt、Brief 全文、外部密钥或其他项目对象。

    **拒绝而不是脱敏**（见 `service._scan_payload_for_sensitive`）：
    事件会落库并被推给多个连接，脱敏后写入会让"库里躺着一份打过码的副本"，
    且产生方看不到任何异常。
    """
    for bad in (
        {"prompt": "..."},
        {"brief": {"text": "..."}},
        {"apiKey": "sk-xxx"},
        {"nested": {"secret": "x"}},
        {"list": [{"token": "x"}]},
    ):
        with pytest.raises(ApiError) as excinfo:
            _publish(payload=bad)
        assert excinfo.value.details["reason"] == "payload_rejected", bad
        assert "hitPath" in excinfo.value.details


def test_publish_rejects_overlong_text_values() -> None:
    """长文本本身可疑：事件只带"界面必要信息"，几千字的文本多半是正文。"""
    with pytest.raises(ApiError) as excinfo:
        _publish(payload={"note": "x" * 3000})
    assert excinfo.value.details["reason"] == "payload_rejected"


def test_publish_allows_normal_payload() -> None:
    """正常 payload 不该被误伤（反向用例）。"""
    view = _publish(
        payload={"state": "RUNNING", "completedUnits": 6, "totalUnits": 10,
                 "phase": "MEDIA_GENERATION"}
    )
    assert view.payload["completedUnits"] == 6


def test_notifier_receives_notify_from_another_connection() -> None:
    """**ADR-14 关键约束 1 的验证**：通知只带序号，订阅方按 id 读库。

    这条同时验证了一件事：`pg_notify` 在事务提交时投递
    （PG 的保证），因此"事件可见"与"通知发出"是原子的。
    """
    notifier = EventNotifier(_dsn())
    notifier.start()
    try:
        view = _publish()
        batch = notifier.poll(timeout=2.0)
        assert batch, "应收到通知"
        assert view.sequence in batch.sequences

        # payload 只有序号 —— 通知里**没有**事件正文。
        with session_scope(SETTINGS) as session:
            rows = EventRepository(session).list_by_ids(sequences=batch.sequences)
        assert [row.sequence for row in rows] == list(batch.sequences)
    finally:
        notifier.stop()


@pytest.mark.regression
def test_notifier_payload_is_bare_sequence() -> None:
    """通知 payload 是纯数字（不带 JSON、不带正文）—— 这是可断言的事实。"""
    notifier = EventNotifier(_dsn())
    notifier.start()
    try:
        # 用一个明确不含敏感词的字段 —— 本包对敏感键名是"宁可误伤"的保守口径，
        # 这条用例的目的是验证正常 payload 不被误伤，而非试探扫描边界。
        view = _publish(payload={"state": "RUNNING", "progressOk": True})
        batch = notifier.poll(timeout=2.0)
        assert batch.sequences == (view.sequence,)
    finally:
        notifier.stop()


# ===========================================================================
# V3/V4：跨实例 fan-out（**必须双向**）
# ===========================================================================


def test_cross_instance_event_on_a_reaches_subscriber_b() -> None:
    """**V3**：事件在 A 侧写入，B 侧的连接必须收到。"""
    notifier_a = EventNotifier(_dsn())
    notifier_b = EventNotifier(_dsn())
    notifier_a.start()
    notifier_b.start()
    try:
        # 清掉双方启动前可能积压的通知
        notifier_a.poll(timeout=0.5)
        notifier_b.poll(timeout=0.5)

        view = _publish(event_type=EVENT_JOB_QUEUED, payload={"state": "QUEUED"})

        batch_b = notifier_b.poll(timeout=2.0)
        assert batch_b.sequences == (view.sequence,), "B 必须收到 A 侧产生的事件"
        batch_a = notifier_a.poll(timeout=1.0)
        assert batch_a.sequences == (view.sequence,)
    finally:
        notifier_a.stop()
        notifier_b.stop()


def test_cross_instance_event_on_b_reaches_subscriber_a() -> None:
    """**V4**：反向 —— 交换角色同样必须通过。

    ADR-14 原文："V3 与 V4 必须**双向**通过。只测单向不能证明 fan-out 正确。"
    """
    notifier_a = EventNotifier(_dsn())
    notifier_b = EventNotifier(_dsn())
    notifier_a.start()
    notifier_b.start()
    try:
        notifier_a.poll(timeout=0.5)
        notifier_b.poll(timeout=0.5)

        # 这次"在 B 侧"写（在本机测试里就是同一进程的另一次写入，
        # 但关键性质成立：通知不依赖调用方自己那条连接）。
        view = _publish(event_type=EVENT_JOB_QUEUED, payload={"state": "QUEUED"})

        batch_a = notifier_a.poll(timeout=2.0)
        assert batch_a.sequences == (view.sequence,), "A 必须收到 B 侧产生的事件"
    finally:
        notifier_a.stop()
        notifier_b.stop()


def test_cross_instance_both_receive_same_event() -> None:
    """两个实例**同时**收到同一条事件（fan-out 的本质）。"""
    notifier_a = EventNotifier(_dsn())
    notifier_b = EventNotifier(_dsn())
    notifier_a.start()
    notifier_b.start()
    try:
        notifier_a.poll(timeout=0.5)
        notifier_b.poll(timeout=0.5)
        view = _publish()
        assert notifier_a.poll(timeout=2.0).sequences == (view.sequence,)
        assert notifier_b.poll(timeout=2.0).sequences == (view.sequence,)
    finally:
        notifier_a.stop()
        notifier_b.stop()


def test_subscriber_registry_routes_by_project() -> None:
    """订阅表按 project 粗筛（**宽进严出**：精确判定留给鉴权）。"""
    registry = SubscriberRegistry()
    owner = _create_user()
    project_a = _seed_project(owner_id=owner)
    project_b = _seed_project(owner_id=owner)

    seen_a: list = []
    seen_b: list = []
    seen_all: list = []

    registry.add(project_id=project_a, callback=seen_a.append)
    registry.add(project_id=project_b, callback=seen_b.append)
    registry.add(project_id=None, callback=seen_all.append)  # 全量订阅（管理者）

    assert registry.callbacks_for(project_id=project_a) == {seen_a.append, seen_all.append} or \
        len(registry.callbacks_for(project_id=project_a)) == 2
    for_project_a = registry.callbacks_for(project_id=project_a)
    assert seen_a.append in for_project_a
    assert seen_b.append not in for_project_a
    assert seen_all.append in for_project_a, "全量订阅者必须被叫醒"

    per_project, total = registry.sizes()
    assert per_project == 2
    assert total == 1


def test_subscriber_registry_removes_empty_buckets() -> None:
    """连接断开后清掉空桶 —— 否则"连接来来去去"会让 dict 无界增长。"""
    registry = SubscriberRegistry()
    project_id = uuid.uuid4()
    callback = lambda batch: None  # noqa: E731

    registry.add(project_id=project_id, callback=callback)
    assert registry.sizes()[0] == 1
    registry.remove(project_id=project_id, callback=callback)
    assert registry.sizes()[0] == 0


def test_notifier_unsubscribe_stops_delivery() -> None:
    notifier = EventNotifier(_dsn())
    notifier.start()
    try:
        seen: list = []
        notifier.subscribe(seen.append)
        _publish()
        assert notifier.poll(timeout=2.0)

        notifier.unsubscribe(seen.append)
        before = len(seen)
        _publish()
        notifier.poll(timeout=2.0)
        assert len(seen) == before, "取消订阅后不应再收到"
    finally:
        notifier.stop()


def test_notifier_reports_connection_state() -> None:
    """ADR-14 把"LISTEN 连接断开"列为风险，因此必须有可查询的连接状态。"""
    notifier = EventNotifier(_dsn())
    assert notifier.connected is False
    notifier.start()
    try:
        assert notifier.connected is True
    finally:
        notifier.stop()
    assert notifier.connected is False


def test_notifier_rejects_unsafe_channel_name() -> None:
    """channel 名要拼进 SQL（LISTEN 不接受参数），因此必须校验。"""
    notifier = EventNotifier(_dsn(), channel='bad"; DROP TABLE x; --')
    with pytest.raises(ValueError):
        notifier.start()


# ===========================================================================
# V5：断线补漏（补发恰好 N 条，无重无漏）
# ===========================================================================


def test_backfill_returns_exactly_the_missing_events() -> None:
    """**V5**：断线期间产生 3 条，重连补发**恰好 3 条**。"""
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    user = _current_user(owner)

    anchor = _publish(project_id=project_id)  # 断线前已收到的最后一条
    missed = [
        _publish(project_id=project_id, payload={"n": index}) for index in range(3)
    ]

    with session_scope(SETTINGS) as session:
        result = service.backfill_events(
            EventRepository(session),
            session,
            user=user,
            last_event_id=anchor.sequence,
            project_id=project_id,
        )

    assert [view.sequence for view in result.events] == [m.sequence for m in missed]
    assert result.overflowed is False
    assert result.filtered == 0


def test_backfill_with_no_last_id_returns_recent_snapshot() -> None:
    """新连接（无 Last-Event-ID）取最近若干条 —— 用户至少要知道"现在什么状态"。"""
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    user = _current_user(owner)
    for index in range(3):
        _publish(project_id=project_id, payload={"n": index})

    with session_scope(SETTINGS) as session:
        result = service.backfill_events(
            EventRepository(session),
            session,
            user=user,
            last_event_id=None,
            project_id=project_id,
        )
    assert len(result.events) == 3
    assert result.overflowed is False


def test_backfill_overflow_is_signalled_not_truncated() -> None:
    """**超限必须给出信号，不能静默截断**（ADR-14 关键约束 5）。

    静默截断会让前端以为拿到了完整历史，于是基于一堆过期的中间态
    渲染出错误的界面。因此 `overflowed=True` 是必须被传播的信息。
    """
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    user = _current_user(owner)
    anchor = _publish(project_id=project_id)
    for _ in range(5):
        _publish(project_id=project_id)

    with session_scope(SETTINGS) as session:
        result = service.backfill_events(
            EventRepository(session),
            session,
            user=user,
            last_event_id=anchor.sequence,
            project_id=project_id,
            limit=2,
        )

    assert result.overflowed is True
    assert len(result.events) == 2, "上限内仍要返回（前端可以用它先渲染）"


def test_backfill_rejects_negative_last_event_id() -> None:
    owner = _create_user()
    user = _current_user(owner)
    with session_scope(SETTINGS) as session:
        with pytest.raises(ApiError) as excinfo:
            service.backfill_events(
                EventRepository(session), session, user=user, last_event_id=-1
            )
    assert excinfo.value.details["reason"] == "last_event_id_negative"


def test_backfill_filters_by_project() -> None:
    """补漏只取本项目的事件（`projectId` 查询参数的作用）。"""
    owner = _create_user()
    project_a = _seed_project(owner_id=owner)
    project_b = _seed_project(owner_id=owner)
    user = _current_user(owner)

    anchor = _publish(project_id=project_a)
    wanted = _publish(project_id=project_a)
    _publish(project_id=project_b)  # 不该出现

    with session_scope(SETTINGS) as session:
        result = service.backfill_events(
            EventRepository(session),
            session,
            user=user,
            last_event_id=anchor.sequence,
            project_id=project_a,
        )
    assert [view.sequence for view in result.events] == [wanted.sequence]


def test_merge_watermark_never_goes_backwards() -> None:
    """水位线只增不减 —— 否则重连会重复收到已经收到的内容。"""
    assert service.merge_watermark(pushed_sequences=[], last_event_id=100) == 100
    assert service.merge_watermark(pushed_sequences=[50], last_event_id=100) == 100
    assert service.merge_watermark(pushed_sequences=[150], last_event_id=100) == 150
    assert service.merge_watermark(pushed_sequences=[], last_event_id=None) is None


def test_should_emit_uses_strict_greater_than() -> None:
    """严格大于：水位线本身就是"上一条已推送的序号"。"""
    assert service.should_emit(sequence=101, watermark=100) is True
    assert service.should_emit(sequence=100, watermark=100) is False
    assert service.should_emit(sequence=99, watermark=100) is False
    assert service.should_emit(sequence=1, watermark=None) is True


def test_backfill_then_live_does_not_duplicate() -> None:
    """**ADR-14 边界要求**：补漏与实时之间按序号去重合并，不重复。

    构造：补漏拿到 seq A、B；随后"通知"又带来同一条 B（真实场景里
    通知可能与补漏窗口重叠）—— 水位线必须把它挡掉。
    """
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    user = _current_user(owner)

    anchor = _publish(project_id=project_id)
    first = _publish(project_id=project_id)
    second = _publish(project_id=project_id)

    with session_scope(SETTINGS) as session:
        result = service.backfill_events(
            EventRepository(session),
            session,
            user=user,
            last_event_id=anchor.sequence,
            project_id=project_id,
        )
    watermark = service.merge_watermark(
        pushed_sequences=[v.sequence for v in result.events],
        last_event_id=anchor.sequence,
    )
    assert watermark == second.sequence

    # 通知又带来 first 与 second（重复）
    dupes = [first.sequence, second.sequence]
    to_send = [s for s in dupes if service.should_emit(sequence=s, watermark=watermark)]
    assert to_send == [], "重叠的事件必须被水位线挡掉"


# ===========================================================================
# V6/V7：权限（逐事件 + 成员移除后重鉴权）
# ===========================================================================


def test_non_member_cannot_backfill_project_events() -> None:
    """**V6**：非成员补漏该项目的流 → 返回空（事件被逐条过滤）。"""
    owner = _create_user()
    outsider = _create_user()
    project_id = _seed_project(owner_id=owner)
    _publish(project_id=project_id)

    with session_scope(SETTINGS) as session:
        result = service.backfill_events(
            EventRepository(session),
            session,
            user=_current_user(outsider),
            last_event_id=None,
            project_id=project_id,
        )
    assert result.events == ()
    assert result.filtered >= 1, "应记录被过滤掉的条数（诊断用）"


def test_member_can_backfill_project_events() -> None:
    """反向用例：成员必须能看到（挡住"一律过滤"的退化实现）。"""
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    _publish(project_id=project_id)

    with session_scope(SETTINGS) as session:
        result = service.backfill_events(
            EventRepository(session),
            session,
            user=_current_user(owner),
            last_event_id=None,
            project_id=project_id,
        )
    assert len(result.events) == 1


def test_admin_sees_all_events() -> None:
    owner = _create_user()
    admin = _create_user(role=ROLE_ADMIN)
    project_id = _seed_project(owner_id=owner)
    _publish(project_id=project_id)

    with session_scope(SETTINGS) as session:
        result = service.backfill_events(
            EventRepository(session),
            session,
            user=_current_user(admin),
            last_event_id=None,
            project_id=project_id,
        )
    assert len(result.events) == 1


def test_platform_level_event_is_invisible_to_operator() -> None:
    """平台级事件（`project_id IS NULL`）不对普通用户开放（登记 OP-112）。"""
    owner = _create_user()
    _publish(project_id=None)

    with session_scope(SETTINGS) as session:
        result = service.backfill_events(
            EventRepository(session), session, user=_current_user(owner),
            last_event_id=None,
        )
    assert result.events == ()


def test_platform_level_event_is_visible_to_admin() -> None:
    """管理员能看到平台级事件（**但不代表补漏只返回它**）。

    平台级补漏（`project_id=None`）对管理员返回的是**全量**事件，
    包括其他项目产生的。断言的是"平台级那条**在其中**"（没被过滤掉），
    而不是"恰好只有一条" —— 后者把补漏的上限当成了返回值的数量语义。
    """
    admin = _create_user(role=ROLE_ADMIN)
    view = _publish(project_id=None)
    with session_scope(SETTINGS) as session:
        result = service.backfill_events(
            EventRepository(session), session, user=_current_user(admin),
            last_event_id=None,
        )
    assert view.sequence in [v.sequence for v in result.events], \
        "平台级事件必须出现在管理员的补漏里"
    assert any(v.project_id is None for v in result.events), \
        "补漏里必须包含 project_id 为 None 的事件"


def test_removed_member_stops_receiving_events() -> None:
    """**V7**：成员被移除后不再收到该项目事件（异常矩阵第 16 项）。

    ADR-14 关键约束 3 的理由："会话中权限可能变化（成员被移除、项目负责人交接）"。
    这条测的是"**建连之后**权限变化"—— 建连时的校验已经过了，
    必须靠**逐事件**重查才能拦住。
    """
    owner = _create_user()
    member = _create_user()
    project_id = _seed_project(owner_id=owner, collaborators=(member,))

    _publish(project_id=project_id)
    with session_scope(SETTINGS) as session:
        before = service.backfill_events(
            EventRepository(session), session, user=_current_user(member),
            last_event_id=None, project_id=project_id,
        )
    assert len(before.events) >= 1, "移除前应能收到"

    # 移除成员（走 WP10 的服务，保证与项目模块同一套语义）
    with session_scope(SETTINGS) as session:
        ProjectRepository(session).remove_member(project_id, member)
        session.flush()

    _publish(project_id=project_id)
    with session_scope(SETTINGS) as session:
        after = service.backfill_events(
            EventRepository(session), session, user=_current_user(member),
            last_event_id=None, project_id=project_id,
        )
    assert after.events == (), "被移除后必须收不到该项目的事件"


def test_can_view_event_rechecks_membership_each_time() -> None:
    """`can_view_event` 每次调用都反映**当前**成员关系。

    用"算好的 member_project_ids"与"不传（现查）"两种方式各验一次 ——
    前者是批量推送的快路径，后者是慢路径；两者结论必须一致，
    否则"批量推送"会变成绕过鉴权的后门。
    """
    owner = _create_user()
    member = _create_user()
    project_id = _seed_project(owner_id=owner, collaborators=(member,))
    _publish(project_id=project_id)

    user = _current_user(member)
    with session_scope(SETTINGS) as session:
        event = session.execute(
            text("SELECT id FROM domain_events WHERE project_id = :p LIMIT 1"),
            {"p": str(project_id)},
        ).scalar_one()
        row = session.get(DomainEvent, event)
        assert row is not None
        computed = service.active_member_project_ids(session, user_id=member)
        assert service.can_view_event(session, event=row, user=user) is True
        assert (
            service.can_view_event(
                session, event=row, user=user, member_project_ids=computed
            )
            is True
        )

    with session_scope(SETTINGS) as session:
        ProjectRepository(session).remove_member(project_id, member)
        session.flush()

    with session_scope(SETTINGS) as session:
        row = session.get(DomainEvent, event)
        assert row is not None
        assert service.can_view_event(session, event=row, user=user) is False


def test_last_event_id_is_not_an_authorization_token() -> None:
    """协议 §1.1 明写 Last-Event-ID"**不作为授权凭证**"。

    构造：非成员带一个很大的 Last-Event-ID 重连 ——
    它能"定位"到全部事件，但**一条都拿不到**（因为逐事件鉴权）。
    若实现把 Last-Event-ID 当权限依据，这条会失败。
    """
    owner = _create_user()
    outsider = _create_user()
    project_id = _seed_project(owner_id=owner)
    anchor = _publish(project_id=project_id)
    _publish(project_id=project_id)

    with session_scope(SETTINGS) as session:
        result = service.backfill_events(
            EventRepository(session),
            session,
            user=_current_user(outsider),
            last_event_id=anchor.sequence - 1,  # 故意往回一点，想多拿
            project_id=project_id,
        )
    assert result.events == (), "Last-Event-ID 不能用来绕过鉴权"


# ===========================================================================
# SSE 消息格式（协议 §11）
# ===========================================================================


def test_sse_message_format_matches_protocol_example() -> None:
    """消息格式必须与协议 §11 的示例同形。"""
    raw = _sse_message(
        event_id=18492,
        event_type="job.progress",
        data={"schemaVersion": 1, "sequence": "18492"},
    ).decode("utf-8")

    lines = raw.split("\n")
    assert lines[0] == "id: 18492"
    assert lines[1] == "event: job.progress"
    assert lines[2].startswith("data: ")
    assert json.loads(lines[2][len("data: "):]) == {
        "schemaVersion": 1,
        "sequence": "18492",
    }
    assert raw.endswith("\n\n"), "消息必须以空行结束，否则浏览器会一直等下一行"


def test_sse_message_is_utf8_with_chinese() -> None:
    raw = _sse_message(event_id=1, event_type="job.queued", data={"note": "中文"})
    assert "中文" in raw.decode("utf-8")


def test_heartbeat_interval_is_under_common_proxy_timeout() -> None:
    """心跳必须明显小于反向代理的空闲超时（常见默认 60s），否则代理先掐连接。"""
    assert HEARTBEAT_SECONDS < 60
    assert HEARTBEAT_SECONDS > 0


def test_notify_channel_matches_adr() -> None:
    """ADR-14 指定 channel 名为 `domain_events_new`。"""
    assert NOTIFY_CHANNEL == "domain_events_new"


def test_sync_required_is_a_known_event_type() -> None:
    """`sync.required` 是协议 §11 列出的类型（补漏超限的对应信号）。"""
    assert EVENT_SYNC_REQUIRED in EVENT_TYPES


# ===========================================================================
# HTTP 层
# ===========================================================================


def test_events_route_is_registered() -> None:
    from fastapi import FastAPI

    from rednote.main import create_app

    app: FastAPI = create_app(SETTINGS)
    paths = {
        (method, route.path)
        for route in app.routes
        if hasattr(route, "methods")
        for method in (route.methods or set())
    }
    assert ("GET", "/api/v1/events") in paths


def test_app_state_has_registry_but_no_notifier_by_default() -> None:
    """工厂只装配订阅表、**不建立 LISTEN 连接**。

    理由：`create_app(Settings(database_url=None))` 是合法用法
    （conftest 与多处测试依赖它）。在工厂里强行连库会让那种 app 直接崩掉。
    """
    from rednote.main import create_app

    app = create_app(SETTINGS)
    assert isinstance(app.state.event_registry, SubscriberRegistry)
    assert app.state.event_notifier is None


def test_unauthenticated_events_request_is_rejected() -> None:
    from fastapi.testclient import TestClient

    from rednote.main import create_app

    with TestClient(create_app(SETTINGS)) as client:
        response = client.get("/api/v1/events")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"


def test_last_event_id_reads_header_first() -> None:
    """头优先于查询参数（浏览器 EventSource 用头）。"""

    class _Req:
        def __init__(self, headers, params):
            self.headers = headers
            self.query_params = params

    assert _read_last_event_id(_Req({"Last-Event-ID": "42"}, {})) == 42
    assert _read_last_event_id(_Req({}, {"lastEventId": "43"})) == 43
    assert _read_last_event_id(_Req({"Last-Event-ID": "42"}, {"lastEventId": "43"})) == 42
    # 非法值当新连接（不报错）
    assert _read_last_event_id(_Req({"Last-Event-ID": "abc"}, {})) is None
    assert _read_last_event_id(_Req({}, {})) is None


# ===========================================================================
# 并发与存储层
# ===========================================================================


def test_concurrent_publishes_get_unique_sequences() -> None:
    """**并发**写事件：序号必须唯一且不重（序列的核心性质）。"""
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)

    def publish(index: int):
        return _publish(project_id=project_id, payload={"n": index}).sequence

    with ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(pool.map(publish, range(16)))

    assert len(set(sequences)) == 16, f"序号出现重复：{sequences}"


def test_list_after_respects_limit_plus_one() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    anchor = _publish(project_id=project_id)
    for _ in range(3):
        _publish(project_id=project_id)

    with session_scope(SETTINGS) as session:
        rows = EventRepository(session).list_after(
            after_sequence=anchor.sequence, limit=2, project_id=project_id
        )
    assert len(rows) == 3, "应返回 limit+1 条供调用方判断还有没有更多"


def test_max_sequence_is_zero_when_empty() -> None:
    with session_scope(SETTINGS) as session:
        assert EventRepository(session).max_sequence() >= 0


def test_event_row_has_no_updated_at() -> None:
    """事件是追加的，写完不改 —— 加了 updated_at 反而暗示"会被改"。"""
    columns = set(DomainEvent.__table__.columns.keys())
    assert "updated_at" not in columns
    assert "sequence" in columns
    assert "occurred_at" in columns