"""WP15 测试：审计事件、待办去重与完成、通知回执。

覆盖验收标准（研发计划 04 §4.2）：

> **「日志可追踪且不泄露正文；待办去重」**

**按验收的两半组织**：

| 半 | 分节 | 关键断言 |
|---|---|---|
| 日志可追踪且**不泄露正文** | 「审计」 | 表里**没有存正文的列**；只存 64 位摘要；不可篡改 |
| **待办去重** | 「待办」 | `dedupe_key` 部分唯一索引在**并发**下也唯一 |

**为什么"不泄露正文"要用结构断言而不是行为断言**：
行为断言（"传入含正文的 payload 后库里查不到正文"）只能证明**当前实现**不泄露。
结构断言（"`audit_events` 没有可以存正文的列"）证明的是**这件事做不到** ——
后者强得多：将来有人想加一个 `payload` 列，会先撞上这条断言。

**测试隔离**：所有测试用独立的 `project_id` / `subject_id`，
避免"待办去重"与"审计分页"互相污染（`dedupe_key` 与审计查询都是全局的）。
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from rednote.modules.audit import service
from rednote.modules.audit.models import (
    ACTOR_KIND_SERVICE,
    ACTOR_KIND_SYSTEM,
    ACTOR_KIND_USER,
    ACTOR_KINDS,
    AuditEvent,
    NotificationReceipt,
)
from rednote.modules.audit.repository import AuditRepository
from rednote.modules.events import service as events_service
from rednote.modules.events.models import EVENT_JOB_PROGRESS
from rednote.modules.events.repository import EventRepository
from rednote.modules.identity.dependencies import CurrentUser
from rednote.modules.identity.models import ROLE_ADMIN, ROLE_OPERATOR
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.projects.models import Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.modules.versions.service import canonical_payload_hash
from rednote.modules.work_items.models import (
    WORK_ITEM_DONE,
    WORK_ITEM_OPEN,
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
    storage_backend="LOCAL",
    storage_local_root="/tmp/rednote-wp15-test-storage",
)

_PLACEHOLDER_SESSION_ID = uuid.UUID("00000000-0000-4000-8000-000000000015")


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


def _seed_project(
    *, owner_id: uuid.UUID, collaborators: tuple[uuid.UUID, ...] = ()
) -> uuid.UUID:
    with session_scope(SETTINGS) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name="测试产品")
        session.add(product)
        session.flush()
        project = Project(
            project_code=f"AUD-{uuid.uuid4().hex[:8].upper()}",
            name="审计测试项目",
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


def _member_ids(user_id: uuid.UUID) -> frozenset[uuid.UUID]:
    with session_scope(SETTINGS) as session:
        return events_service.active_member_project_ids(session, user_id=user_id)


def _record_audit(**overrides: object) -> service.AuditView:
    params: dict[str, object] = {
        "action": "test.action",
        "object_type": "test_object",
        "object_id": uuid.uuid4(),
        "request_id": f"req-{uuid.uuid4().hex[:12]}",
        "actor_kind": ACTOR_KIND_SYSTEM,
    }
    params.update(overrides)
    with session_scope(SETTINGS) as session:
        return service.record_audit(AuditRepository(session), **params)  # type: ignore[arg-type]


def _ensure_work_item(**overrides: object) -> service.WorkItemUpsert:
    params: dict[str, object] = {
        "dedupe_key": f"k-{uuid.uuid4().hex}",
        "kind": "REVIEW",
        "subject_type": "content",
        "subject_id": uuid.uuid4(),
    }
    params.update(overrides)
    with session_scope(SETTINGS) as session:
        return service.ensure_work_item(AuditRepository(session), **params)  # type: ignore[arg-type]


# ===========================================================================
# 第一半：日志可追踪
# ===========================================================================


def test_audit_event_has_no_column_capable_of_storing_body() -> None:
    """**结构性断言**：`audit_events` 里没有可以存正文的列。

    这条比行为断言强得多 —— 它证明的是"存正文这件事**做不到**"，
    而不是"当前实现没存"。将来有人想加一个 `payload` / `content` 列，
    会先撞上这条断言，被迫回答"为什么审计需要存正文"。

    允许的列只有三类：标识（id / actor / object / project / version）、
    摘要（before_hash / after_hash，各 64 字符）、元数据（action / reason / request_id / created_at）。
    """
    columns = set(AuditEvent.__table__.columns.keys())
    forbidden = {
        "payload",
        "content",
        "body",
        "text",
        "data",
        "snapshot",
        "detail",
        "diff",
        "before",
        "after",
    }
    assert columns & forbidden == set(), (
        f"审计表出现了疑似承载正文的列：{columns & forbidden}"
    )

    # 摘要列必须是定长 64（SHA-256 hex），不能是 TEXT。
    for name in ("before_hash", "after_hash"):
        column = AuditEvent.__table__.columns[name]
        assert column.type.length == 64, f"{name} 必须是 char(64)，实际 {column.type}"


def test_record_audit_stores_hashes_not_payload() -> None:
    """写入 payload 后，库里只有摘要，**没有任何原文字段**。"""
    secret_body = "这是不该出现在审计里的正文内容" * 10
    view = _record_audit(
        before={"content": "旧内容"},
        after={"content": secret_body},
    )

    assert view.before_hash == canonical_payload_hash({"content": "旧内容"})
    assert view.after_hash == canonical_payload_hash({"content": secret_body})
    assert len(view.before_hash or "") == 64

    # 把整行读出来，确认没有任何字段包含正文。
    with session_scope(SETTINGS) as session:
        row = session.get(AuditEvent, view.audit_id)
        assert row is not None
        for column in AuditEvent.__table__.columns:
            value = getattr(row, column.name)
            assert secret_body not in str(value), (
                f"审计列 {column.name} 里出现了正文"
            )


def test_hash_reuses_version_hash_algorithm() -> None:
    """审计哈希与版本哈希**用同一个算法**（可对照）。

    这条守住一个具体的核对场景："审计说从 X 变到 Y，这个 X 对应哪一版？"
    若两处算法不同，这个问题永远没有答案。
    """
    payload = {"b": 2, "a": 1, "c": {"z": 26, "y": 25}}
    assert service.hash_payload(payload) == canonical_payload_hash(payload)


def test_hash_ignores_key_order() -> None:
    """键序不同的等价 payload 必须算出同一摘要（否则"变没变"的判断会误报）。"""
    assert service.hash_payload({"a": 1, "b": 2}) == service.hash_payload(
        {"b": 2, "a": 1}
    )


def test_hash_of_none_is_none_not_empty_object_hash() -> None:
    """`None` 与"空对象"必须区分。

    `None` = 这次动作不涉及内容（查看、登录）；
    `{}` = 内容确实被清空了。
    算成同一个哈希就分不出这两种情况，而它们对排障的含义完全不同。
    """
    assert service.hash_payload(None) is None
    assert service.hash_payload({}) is not None
    assert service.hash_payload(None) != service.hash_payload({})


def test_changed_reflects_three_states() -> None:
    """`changed` 要能区分三种情况：不涉及内容 / 内容变了 / 写入了相同内容。"""
    no_content = _record_audit()
    assert no_content.changed is False

    changed = _record_audit(before={"a": 1}, after={"a": 2})
    assert changed.changed is True

    same = _record_audit(before={"a": 1}, after={"a": 1})
    assert same.changed is False, "写入相同内容不算改变"


def test_record_audit_requires_actor_id_for_user_kind() -> None:
    """`actorKind=USER` 必须有 `actorId` —— 否则"谁做的"无从查起。

    这条让"忘了传 actor_id 的 USER 审计"**当场失败**，
    而不是在几个月后回溯时才发现查不到人。
    """
    with pytest.raises(ApiError) as excinfo:
        _record_audit(actor_kind=ACTOR_KIND_USER, actor_id=None)
    assert excinfo.value.details["reason"] == "actor_id_required_for_user"


def test_record_audit_allows_system_without_actor() -> None:
    """SYSTEM / SERVICE 的 `actor_id` 可空（系统事件没有具体操作人）。"""
    for kind in (ACTOR_KIND_SYSTEM, ACTOR_KIND_SERVICE):
        view = _record_audit(actor_kind=kind, actor_id=None)
        assert view.actor_id is None


def test_record_audit_rejects_unknown_actor_kind() -> None:
    with pytest.raises(ApiError) as excinfo:
        _record_audit(actor_kind="ROBOT")
    assert excinfo.value.details["reason"] == "actor_kind_unknown"
    assert len(excinfo.value.details["allowedActorKinds"]) == len(ACTOR_KINDS)


def test_reason_is_length_limited() -> None:
    """`reason` 限长 512 —— 否则它会成为"顺便把内容塞进来"的通道。"""
    with pytest.raises(ApiError) as excinfo:
        _record_audit(reason="x" * (service.MAX_REASON_LENGTH + 1))
    assert excinfo.value.details["reason"] == "reason_too_long"

    # 边界：恰好 512 应当通过。
    ok = _record_audit(reason="x" * service.MAX_REASON_LENGTH)
    assert len(ok.reason or "") == service.MAX_REASON_LENGTH


def test_reason_blank_is_normalized_to_none() -> None:
    assert _record_audit(reason="   ").reason is None


# ===========================================================================
# 第一半：审计不可篡改（**活性验证**）
# ===========================================================================


def test_audit_events_cannot_be_updated_via_raw_sql() -> None:
    """**原生 SQL 也改不了**（手工 SQL、数据回填、运维脚本都走这条路）。

    这条是本包最重要的测试之一：若只测 ORM 路径，
    测试与实现就共享同一个盲区（"代码里没写 UPDATE"≠"改不了"）。
    """
    view = _record_audit(reason="原始理由")

    with session_scope(SETTINGS) as session:
        with pytest.raises(DBAPIError) as excinfo:
            session.execute(
                text("UPDATE audit_events SET reason = 'tampered' WHERE id = :i"),
                {"i": str(view.audit_id)},
            )
    assert excinfo.value.orig.sqlstate == "23001"

    with session_scope(SETTINGS) as session:
        row = session.get(AuditEvent, view.audit_id)
        assert row is not None
        assert row.reason == "原始理由", "内容必须原样保留"


def test_audit_events_cannot_be_deleted_via_raw_sql() -> None:
    view = _record_audit()
    with session_scope(SETTINGS) as session:
        with pytest.raises(DBAPIError) as excinfo:
            session.execute(
                text("DELETE FROM audit_events WHERE id = :i"),
                {"i": str(view.audit_id)},
            )
    assert excinfo.value.orig.sqlstate == "23001"

    with session_scope(SETTINGS) as session:
        assert session.get(AuditEvent, view.audit_id) is not None


def test_audit_events_cannot_be_updated_via_orm() -> None:
    """ORM 路径也要被拦（给开发者一个可读的领域错误）。

    **注意这里显式 `session.flush()`**，而不是让 `session_scope` 退出时提交：
    ORM 守卫在 flush 时抛出（`ImmutableRecordError`），
    而如果只依赖退出时的自动提交，异常会在**上下文管理器退出阶段**抛出 ——
    那时 `session_scope` 已经在做 rollback，最终冒出来的可能是
    数据库层（触发器）的 `IntegrityError` 而不是领域错误。
    两条路径都要有测试，但它们断言的是**不同的东西**：
    这一条验"开发者能看到可读错误"，另一条验"数据库层兜得住"。
    """
    from rednote.platform.immutability import ImmutableRecordError
    from rednote.platform.database import get_engine
    from sqlalchemy.orm import Session as SASession

    view = _record_audit()

    # **用独立 session 并显式 rollback**，不用 `session_scope`：
    # 守卫在 flush 时抛出后，实例仍处于 dirty 状态；
    # `session_scope` 退出时会再次 flush/rollback，从而**再触发一次守卫** ——
    # 那次异常发生在上下文管理器的收尾阶段，会掩盖掉我们要断言的领域错误。
    # 这里自己管 session 生命周期，让断言只面对一次、且是预期的那次异常。
    session = SASession(bind=get_engine(SETTINGS))
    try:
        row = session.get(AuditEvent, view.audit_id)
        assert row is not None
        row.reason = "tampered"
        with pytest.raises(ImmutableRecordError):
            session.flush()
    finally:
        session.rollback()
        session.close()


def test_immutability_trigger_exists_in_database() -> None:
    """触发器必须真的在库里，且名字与代码一致。"""
    from rednote.modules.audit.ddl import trigger_names

    with session_scope(SETTINGS) as session:
        present = set(
            session.execute(
                text(
                    "SELECT tgname FROM pg_trigger"
                    " JOIN pg_class c ON c.oid = tgrelid"
                    " WHERE NOT tgisinternal"
                )
            )
            .scalars()
            .all()
        )
    for name in trigger_names():
        assert name in present, f"缺少触发器 {name}"


# --- DDL 归属：`downgrade` 不得删"别人建的"共用函数（ISS-79）----------------
#
# ★ 本包正是 ISS-79 的**首发现场**：原 `downgrade` 第 2 步写成
#     for drop_function in {item[4] for item in all_trigger_ddl()}:
#         op.execute(sa.text(drop_function))
# 而本包 `all_trigger_ddl()` 里唯一一项的第 5 项就是
# `DROP FUNCTION IF EXISTS rednote_reject_mutation()` —— 共用函数。
# 于是 `alembic downgrade base`（倒序，WP15 先于 WP11 回退）时，
# WP11 的 `version_nodes` / `version_dependencies` 触发器仍引用它 ⇒
#     ERROR: cannot drop function rednote_reject_mutation()
#            because other objects depend on it
# 回滚路径整条不可用 ⇒ 阻塞 WP47 的 RPO/RTO 演练。


def test_consistency_items_is_empty_by_design() -> None:
    """`consistency_items()` 返回 `[]` 是**显式形状**，不是漏写。

    本包唯一一张不可变表挂的是**跨包共用**的 `rednote_reject_mutation()`
    —— 本包**没有自己的函数**。因此 `downgrade` 第 2 步"删本模块的东西"
    退化为"什么都不删"，而这恰好是**正确**的行为（ISS-79 的修法）。

    空集与"函数返回了空"在运行时同形，所以这里显式断言空；
    将来本包若新增一致性触发器（自带函数），这条会亮 ——
    提醒去 `consistency_items()` 里补那一项，`downgrade` 结构不用改。
    """
    from rednote.modules.audit.ddl import consistency_items

    assert consistency_items() == []


@pytest.mark.cross_wp
def test_all_trigger_ddl_points_at_shared_function() -> None:
    """`all_trigger_ddl()` 的第 5 项**指向共用函数** —— 这正是危险所在。

    这条断言与上一条互为对照，且**刻意为"危险形态"背书**：
    建它（`CREATE OR REPLACE`）是幂等的、必须的；删它才是错的。
    因此本包 `downgrade` 第 1 步用它取 `_drop_trigger`（正确），
    第 2 步**必须**改用 `consistency_items()`（空集）。

    两条判据合起来把"建它但不删它"表达完整；少任一条，
    `downgrade` 的修法就退化成"靠人记得别写错"（ISS-95）。
    """
    from rednote.modules.audit.ddl import all_trigger_ddl, immutable_function_names

    all_drops = {item[4] for item in all_trigger_ddl()}
    assert all_drops == {"DROP FUNCTION IF EXISTS rednote_reject_mutation()"}
    assert immutable_function_names() == ("rednote_reject_mutation",)


def test_notification_receipts_is_mutable() -> None:
    """`notification_receipts` **必须可改**（`read_at` 会被写）。

    反向断言：它与 `audit_events` 的约束正好相反 ——
    若哪天有人"统一"给两张表都加不可变触发器，这条会失败。
    """
    columns = set(NotificationReceipt.__table__.columns.keys())
    assert "read_at" in columns
    assert "dismissed_at" in columns
    # 两张表的约束相反，这正是它们必须分开的原因。
    with session_scope(SETTINGS) as session:
        rows = (
            session.execute(
                text(
                    "SELECT c.relname, count(t.tgname) FROM pg_class c"
                    " LEFT JOIN pg_trigger t ON t.tgrelid = c.oid AND NOT t.tgisinternal"
                    " WHERE c.relname IN ('audit_events','notification_receipts')"
                    " GROUP BY c.relname ORDER BY c.relname"
                )
            )
            .all()
        )
    counts = {name: count for name, count in rows}
    assert counts["audit_events"] == 1, "审计必须有不可变触发器"
    assert counts["notification_receipts"] == 0, "回执不能有不可变触发器"


# ===========================================================================
# 第一半：审计查询与可见范围
# ===========================================================================


def test_admin_sees_all_audit_events() -> None:
    admin = _create_user(role=ROLE_ADMIN)
    view = _record_audit(project_id=None)
    with session_scope(SETTINGS) as session:
        page = service.list_audit_events(
            AuditRepository(session),
            user=_current_user(admin),
            member_project_ids=_member_ids(admin),
            object_id=view.object_id,
        )
    assert [item.audit_id for item in page.items] == [view.audit_id]


def test_member_sees_own_project_audit() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    view = _record_audit(project_id=project_id)

    with session_scope(SETTINGS) as session:
        page = service.list_audit_events(
            AuditRepository(session),
            user=_current_user(owner),
            member_project_ids=_member_ids(owner),
            project_id=project_id,
        )
    assert [item.audit_id for item in page.items] == [view.audit_id]


def test_non_member_gets_empty_page_not_403() -> None:
    """非成员指定 `projectId` → **空页**（不是 403）。

    403 会告诉攻击者"这个项目存在" —— 延续 WP09/WP11/WP12/WP14 的口径。
    """
    owner = _create_user()
    outsider = _create_user()
    project_id = _seed_project(owner_id=owner)
    _record_audit(project_id=project_id)

    with session_scope(SETTINGS) as session:
        page = service.list_audit_events(
            AuditRepository(session),
            user=_current_user(outsider),
            member_project_ids=_member_ids(outsider),
            project_id=project_id,
        )
    assert page.items == ()
    assert page.has_more is False


def test_non_member_without_project_filter_sees_only_own_projects() -> None:
    """未指定 `projectId` 时，非管理者只能看**自己所属项目**的审计。

    这条是最容易写错的一处：`list_audit_events()` 不带 `project_id`
    在 repository 层是"全量"（管理者用）。若 service 层忘了区分，
    非管理者未指定过滤时会看到**全量审计**。
    """
    owner = _create_user()
    outsider = _create_user()
    project_a = _seed_project(owner_id=owner)
    mine = _seed_project(owner_id=outsider)
    _record_audit(project_id=project_a)
    visible = _record_audit(project_id=mine)

    with session_scope(SETTINGS) as session:
        page = service.list_audit_events(
            AuditRepository(session),
            user=_current_user(outsider),
            member_project_ids=_member_ids(outsider),
            object_id=visible.object_id,
        )
    # 用 object_id 过滤是为了避开"表里还有别的测试留下的事件"，
    # 但过滤掉 object_id 之后仍要验证"项目可见性"这条规则本身。
    assert [item.audit_id for item in page.items] == [visible.audit_id]

    # 直接查 project_a 的审计（outsider 不是成员）→ 空
    with session_scope(SETTINGS) as session:
        hidden = service.list_audit_events(
            AuditRepository(session),
            user=_current_user(outsider),
            member_project_ids=_member_ids(outsider),
            project_id=project_a,
        )
    assert hidden.items == ()


def test_user_with_no_projects_sees_nothing() -> None:
    """不属于任何项目的非管理者：**空**（不是全量）。

    `list_audit_events_for_projects` 在 `project_ids` 为空时返回 `[]` ——
    这是安全侧。若写成"空就不过滤"，他就看到了全量。
    """
    loner = _create_user()
    _record_audit()

    with session_scope(SETTINGS) as session:
        page = service.list_audit_events(
            AuditRepository(session),
            user=_current_user(loner),
            member_project_ids=frozenset(),
        )
    assert page.items == ()


def test_audit_pagination_is_complete_and_unique() -> None:
    """键集分页必须**不重不漏**（`created_at` 不唯一，游标要带 id 打破平局）。"""
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    # 同一毫秒内插多条（模拟 `created_at` 冲突）。
    for _ in range(5):
        _record_audit(project_id=project_id)

    seen: list[uuid.UUID] = []
    cursor: tuple[datetime, uuid.UUID] | None = None
    for _ in range(10):
        with session_scope(SETTINGS) as session:
            page = service.list_audit_events(
                AuditRepository(session),
                user=_current_user(owner),
                member_project_ids=_member_ids(owner),
                project_id=project_id,
                limit=2,
                after=cursor,
            )
        seen.extend(item.audit_id for item in page.items)
        if not page.has_more:
            break
        cursor = page.next_anchor

    assert len(seen) == len(set(seen)), f"分页出现重复：{seen}"
    assert len(seen) >= 5


# ===========================================================================
# 第二半：待办去重
# ===========================================================================


def test_dedupe_key_format_is_stable() -> None:
    """`dedupe_key` 的格式是**契约**（两处拼法不同会让去重静默失效）。"""
    subject = uuid.uuid4()
    key = service.build_dedupe_key(
        kind="REVIEW", subject_type="content", subject_id=subject
    )
    assert key == f"REVIEW:content:{subject}"
    # 不含 project_id / assignee_id（见函数 docstring 的论证）
    assert "project" not in key


def test_ensure_work_item_dedupes_by_key() -> None:
    """同一 `dedupe_key` 第二次调用返回既有待办（`created=False`）。"""
    key = f"k-{uuid.uuid4().hex}"
    first = _ensure_work_item(dedupe_key=key)
    second = _ensure_work_item(dedupe_key=key)

    assert first.created is True
    assert second.created is False
    assert second.item.work_item_id == first.item.work_item_id

    with session_scope(SETTINGS) as session:
        count = session.execute(
            text("SELECT count(*) FROM work_items WHERE dedupe_key = :k"),
            {"k": key},
        ).scalar_one()
    assert count == 1, "同一 dedupe_key 只能有一条待办"


def test_concurrent_ensure_creates_exactly_one() -> None:
    """**并发**下同一 `dedupe_key` 只能产生一条待办（验收第二半的核心）。

    这是"先查再插"必然失败、唯一索引必然成功的场景：
    两个进程同时发现"该提醒"，各自查到"不存在"，然后各自插入。
    唯一索引是**唯一防线**，`ON CONFLICT DO NOTHING` 只是正确地用它。
    """
    key = f"k-{uuid.uuid4().hex}"
    subject = uuid.uuid4()

    def attempt(_index: int) -> bool:
        result = _ensure_work_item(
            dedupe_key=key, kind="REVIEW", subject_type="content", subject_id=subject
        )
        return result.created

    with ThreadPoolExecutor(max_workers=6) as pool:
        created_flags = list(pool.map(attempt, range(6)))

    assert sum(1 for flag in created_flags if flag) == 1, (
        f"只应有 1 次真的创建，实际 {sum(1 for f in created_flags if f)} 次"
    )
    with session_scope(SETTINGS) as session:
        count = session.execute(
            text("SELECT count(*) FROM work_items WHERE dedupe_key = :k"),
            {"k": key},
        ).scalar_one()
    assert count == 1


def test_dedupe_does_not_block_after_previous_is_done() -> None:
    """**已结待办不参与去重** —— 同一对象反复产生待办是正常的。

    部分唯一索引的 `WHERE state='OPEN'` 就是为这条存在的。
    若唯一性不限于未结，第二次提醒会被**永远**挡住 ——
    而那正是"待办列表看起来正常、实际漏了提醒"的静默故障。
    """
    key = f"k-{uuid.uuid4().hex}"
    first = _ensure_work_item(dedupe_key=key)
    assert first.created is True

    # 把它结掉
    with session_scope(SETTINGS) as session:
        repo = AuditRepository(session)
        assert repo.resolve_work_item(work_item_id=first.item.work_item_id, reason=None)

    # 同一 key 再建 → 应当**成功**（新的未结待办）
    second = _ensure_work_item(dedupe_key=key)
    assert second.created is True, "已结待办不该挡住新的提醒"
    assert second.item.work_item_id != first.item.work_item_id


def test_ensure_work_item_rejects_empty_key() -> None:
    with pytest.raises(ApiError) as excinfo:
        _ensure_work_item(dedupe_key="   ")
    assert excinfo.value.details["reason"] == "empty"


def test_ensure_work_item_allows_null_assignee() -> None:
    """未认领待办（`assignee_id IS NULL`）是需求 §2.3 明确的一种状态。"""
    result = _ensure_work_item(assignee_id=None)
    assert result.item.assignee_id is None
    assert result.item.state == WORK_ITEM_OPEN


# ===========================================================================
# 第二半：待办列表与完成
# ===========================================================================


def _resolve(
    work_item_id: uuid.UUID, *, user_id: uuid.UUID, reason: str | None = None
) -> service.ResolutionResult:
    with session_scope(SETTINGS) as session:
        return service.resolve_work_item(
            AuditRepository(session),
            work_item_id=work_item_id,
            reason=reason,
            user=_current_user(user_id),
            request_id=f"req-{uuid.uuid4().hex[:12]}",
            member_project_ids=_member_ids(user_id),
        )


def test_assignee_sees_own_work_item() -> None:
    user = _create_user()
    item = _ensure_work_item(assignee_id=user)

    with session_scope(SETTINGS) as session:
        page = service.list_work_items(
            AuditRepository(session),
            user=_current_user(user),
            member_project_ids=_member_ids(user),
        )
    assert item.item.work_item_id in [i.work_item_id for i in page.items]


def test_work_item_assigned_to_others_is_invisible() -> None:
    """指派给**别人**的待办看不到（协议原文是"**当前成员**待办"）。"""
    me = _create_user()
    other = _create_user()
    item = _ensure_work_item(assignee_id=other)

    with session_scope(SETTINGS) as session:
        page = service.list_work_items(
            AuditRepository(session),
            user=_current_user(me),
            member_project_ids=_member_ids(me),
        )
    assert item.item.work_item_id not in [i.work_item_id for i in page.items]


def test_unassigned_project_work_item_is_visible_to_members() -> None:
    """**未认领**的项目待办对项目成员可见（否则它永远没人认领）。"""
    owner = _create_user()
    member = _create_user()
    project_id = _seed_project(owner_id=owner, collaborators=(member,))
    item = _ensure_work_item(project_id=project_id, assignee_id=None)

    with session_scope(SETTINGS) as session:
        page = service.list_work_items(
            AuditRepository(session),
            user=_current_user(member),
            member_project_ids=_member_ids(member),
        )
    assert item.item.work_item_id in [i.work_item_id for i in page.items]


def test_unassigned_project_work_item_is_invisible_to_outsiders() -> None:
    """未认领的项目待办对**非成员**不可见（否则是越权）。"""
    owner = _create_user()
    outsider = _create_user()
    project_id = _seed_project(owner_id=owner)
    item = _ensure_work_item(project_id=project_id, assignee_id=None)

    with session_scope(SETTINGS) as session:
        page = service.list_work_items(
            AuditRepository(session),
            user=_current_user(outsider),
            member_project_ids=_member_ids(outsider),
        )
    assert item.item.work_item_id not in [i.work_item_id for i in page.items]


def test_default_state_filter_is_open() -> None:
    """默认只看 OPEN —— 用户打开待办列表想看到的是"还没做的"。"""
    user = _create_user()
    item = _ensure_work_item(assignee_id=user)
    _resolve(item.item.work_item_id, user_id=user)

    with session_scope(SETTINGS) as session:
        page = service.list_work_items(
            AuditRepository(session),
            user=_current_user(user),
            member_project_ids=_member_ids(user),
        )
    assert item.item.work_item_id not in [i.work_item_id for i in page.items]

    # 显式查 DONE 能看到
    with session_scope(SETTINGS) as session:
        done_page = service.list_work_items(
            AuditRepository(session),
            user=_current_user(user),
            member_project_ids=_member_ids(user),
            state=WORK_ITEM_DONE,
        )
    assert item.item.work_item_id in [i.work_item_id for i in done_page.items]


def test_resolve_work_item_sets_done() -> None:
    user = _create_user()
    item = _ensure_work_item(assignee_id=user)
    result = _resolve(item.item.work_item_id, user_id=user, reason="已处理")

    assert result.resolved is True
    assert result.item.state == WORK_ITEM_DONE


def test_resolve_is_idempotent() -> None:
    """重复完成 → `resolved=False`，**不报错**。

    用户点两次"完成"是正常操作（网络重试、手滑）；
    报错只会让他以为没完成。
    """
    user = _create_user()
    item = _ensure_work_item(assignee_id=user)

    first = _resolve(item.item.work_item_id, user_id=user)
    second = _resolve(item.item.work_item_id, user_id=user)

    assert first.resolved is True
    assert second.resolved is False
    assert second.item.state == WORK_ITEM_DONE


def test_resolve_does_not_touch_business_object() -> None:
    """**「完成待办不自动通过其业务对象」**（协议 §10 原文）。

    待办是**提醒**，不是**决策**。把"完成待办"当成"通过审核"的快捷方式
    会绕过审核链路上的全部校验（自审、版本绑定、影响标记）。
    本测试用 `subject_id` 指向一个假对象，断言完成待办**没有**对它做任何事
    —— 即 `resolve_work_item` 只改 `work_items` 表。
    """
    user = _create_user()
    subject = uuid.uuid4()
    item = _ensure_work_item(assignee_id=user, subject_id=subject)

    with session_scope(SETTINGS) as session:
        before = session.execute(
            text("SELECT state FROM work_items WHERE id = :i"),
            {"i": str(item.item.work_item_id)},
        ).scalar_one()

    _resolve(item.item.work_item_id, user_id=user)

    with session_scope(SETTINGS) as session:
        after = session.execute(
            text("SELECT state FROM work_items WHERE id = :i"),
            {"i": str(item.item.work_item_id)},
        ).scalar_one()
        # 审计记的是**待办本身**（`object_type='work_item'` + 待办 id），
        # 而**不是** subject 指向的业务对象 —— 这正是"完成待办不自动通过
        # 其业务对象"在审计上的体现：审计里没有针对 subject 的任何记录。
        audit_for_subject = session.execute(
            text("SELECT count(*) FROM audit_events WHERE object_id = :s"),
            {"s": str(subject)},
        ).scalar_one()
        audit_for_item = session.execute(
            text(
                "SELECT count(*) FROM audit_events"
                " WHERE object_id = :i AND object_type = 'work_item'"
            ),
            {"i": str(item.item.work_item_id)},
        ).scalar_one()

    assert before == WORK_ITEM_OPEN
    assert after == WORK_ITEM_DONE
    assert audit_for_subject == 0, (
        "完成待办不该对 subject 指向的业务对象留下任何痕迹"
        "（协议：完成待办不自动通过其业务对象）"
    )
    assert audit_for_item >= 1, "完成待办必须留下审计（凭据）"


def test_resolve_writes_audit() -> None:
    """完成待办必须写审计（"谁在什么时候把哪条待办结了、为什么"）。"""
    user = _create_user()
    item = _ensure_work_item(assignee_id=user)
    _resolve(item.item.work_item_id, user_id=user, reason="核对完毕")

    with session_scope(SETTINGS) as session:
        row = session.execute(
            text(
                "SELECT actor_kind, action, reason FROM audit_events"
                " WHERE object_id = :i AND action = 'work_item.resolve'"
            ),
            {"i": str(item.item.work_item_id)},
        ).one()
    assert row[0] == ACTOR_KIND_USER
    assert row[1] == "work_item.resolve"
    assert row[2] == "核对完毕"


def test_idempotent_resolve_does_not_write_extra_audit() -> None:
    """**幂等命中时不写审计** —— 否则重试会留下两条"完成了待办"，其中一条是假的。"""
    user = _create_user()
    item = _ensure_work_item(assignee_id=user)
    _resolve(item.item.work_item_id, user_id=user)
    _resolve(item.item.work_item_id, user_id=user)

    with session_scope(SETTINGS) as session:
        count = session.execute(
            text(
                "SELECT count(*) FROM audit_events"
                " WHERE object_id = :i AND action = 'work_item.resolve'"
            ),
            {"i": str(item.item.work_item_id)},
        ).scalar_one()
    assert count == 1, f"幂等命中不该重复写审计，实际 {count} 条"


def test_cannot_resolve_others_work_item() -> None:
    """不能完成别人的待办（越权）。"""
    me = _create_user()
    other = _create_user()
    item = _ensure_work_item(assignee_id=other)

    with pytest.raises(ApiError) as excinfo:
        _resolve(item.item.work_item_id, user_id=me)
    assert excinfo.value.code is ErrorCode.RESOURCE_NOT_FOUND


def test_member_can_resolve_unassigned_project_work_item() -> None:
    """项目成员可以完成未认领的项目待办（"认领并做完"的落地就是完成它）。"""
    owner = _create_user()
    member = _create_user()
    project_id = _seed_project(owner_id=owner, collaborators=(member,))
    item = _ensure_work_item(project_id=project_id, assignee_id=None)

    result = _resolve(item.item.work_item_id, user_id=member)
    assert result.resolved is True


def test_admin_can_resolve_any_work_item() -> None:
    admin = _create_user(role=ROLE_ADMIN)
    other = _create_user()
    item = _ensure_work_item(assignee_id=other)

    result = _resolve(item.item.work_item_id, user_id=admin)
    assert result.resolved is True


def test_resolve_unknown_work_item_is_404() -> None:
    user = _create_user()
    with pytest.raises(ApiError) as excinfo:
        _resolve(uuid.uuid4(), user_id=user)
    assert excinfo.value.code is ErrorCode.RESOURCE_NOT_FOUND


def test_work_item_pagination_is_complete_and_unique() -> None:
    user = _create_user()
    for _ in range(5):
        _ensure_work_item(assignee_id=user)

    seen: list[uuid.UUID] = []
    cursor: tuple[datetime, uuid.UUID] | None = None
    for _ in range(10):
        with session_scope(SETTINGS) as session:
            page = service.list_work_items(
                AuditRepository(session),
                user=_current_user(user),
                member_project_ids=_member_ids(user),
                limit=2,
                after=cursor,
            )
        seen.extend(item.work_item_id for item in page.items)
        if not page.has_more:
            break
        cursor = page.next_anchor

    assert len(seen) == len(set(seen)), f"分页出现重复：{seen}"
    assert len(seen) >= 5


def test_limit_is_clamped_not_rejected() -> None:
    """`limit` 越界夹紧而不是报错（它是"我想看多少"的提示，不是业务约束）。"""
    assert service._clamp_limit(0) == 1
    assert service._clamp_limit(-5) == 1
    assert service._clamp_limit(10_000) == service.MAX_PAGE_SIZE


# ===========================================================================
# 通知回执（内部设施，无端点）
# ===========================================================================


def _publish_event(project_id: uuid.UUID | None = None):
    with session_scope(SETTINGS) as session:
        return events_service.publish_event(
            EventRepository(session),
            event_type=EVENT_JOB_PROGRESS,
            payload={"state": "RUNNING"},
            project_id=project_id,
        )


def test_mark_delivered_is_idempotent() -> None:
    """通知可能因重连/补漏被多次送达，但"发给这个人了"是**一条事实**。"""
    user = _create_user()
    event = _publish_event()

    with session_scope(SETTINGS) as session:
        repo = AuditRepository(session)
        first, created_first = repo.mark_delivered(user_id=user, event_id=event.event_id)
    with session_scope(SETTINGS) as session:
        repo = AuditRepository(session)
        second, created_second = repo.mark_delivered(user_id=user, event_id=event.event_id)

    assert created_first is True
    assert created_second is False
    assert second.read_at is None, "送达不等于已读"


def test_mark_read_is_idempotent_and_keeps_first_timestamp() -> None:
    """重复标记不覆盖第一次已读时间（时间本身就是凭据）。"""
    user = _create_user()
    event = _publish_event()
    with session_scope(SETTINGS) as session:
        AuditRepository(session).mark_delivered(user_id=user, event_id=event.event_id)

    with session_scope(SETTINGS) as session:
        assert AuditRepository(session).mark_read(user_id=user, event_id=event.event_id)
    with session_scope(SETTINGS) as session:
        repo = AuditRepository(session)
        assert repo.mark_read(user_id=user, event_id=event.event_id) is False
        receipt = repo._session.get(NotificationReceipt, (user, event.event_id))
        first_read_at = receipt.read_at if receipt else None

    with session_scope(SETTINGS) as session:
        receipt = session.get(NotificationReceipt, (user, event.event_id))
        assert receipt is not None
        assert receipt.read_at == first_read_at


def test_read_and_dismissed_are_separate() -> None:
    """`read_at` 与 `dismissed_at` 分开 —— "我没看到"与"我不想再看到"是两件事。"""
    user = _create_user()
    event = _publish_event()
    with session_scope(SETTINGS) as session:
        repo = AuditRepository(session)
        repo.mark_delivered(user_id=user, event_id=event.event_id)
        repo.mark_read(user_id=user, event_id=event.event_id)

    with session_scope(SETTINGS) as session:
        receipt = session.get(NotificationReceipt, (user, event.event_id))
        assert receipt is not None
        assert receipt.read_at is not None
        assert receipt.dismissed_at is None, "已读不等于已忽略"


def test_list_unread_excludes_read_and_dismissed() -> None:
    user = _create_user()
    unread_event = _publish_event()
    read_event = _publish_event()
    dismissed_event = _publish_event()

    with session_scope(SETTINGS) as session:
        repo = AuditRepository(session)
        for event in (unread_event, read_event, dismissed_event):
            repo.mark_delivered(user_id=user, event_id=event.event_id)
        repo.mark_read(user_id=user, event_id=read_event.event_id)
        repo.mark_dismissed(user_id=user, event_id=dismissed_event.event_id)

    with session_scope(SETTINGS) as session:
        rows = AuditRepository(session).list_unread(user_id=user, limit=50)
    ids = {row.event_id for row in rows}
    assert unread_event.event_id in ids
    assert read_event.event_id not in ids
    assert dismissed_event.event_id not in ids


def test_notification_receipts_has_no_endpoint() -> None:
    """**本表无 HTTP 端点**（协议 §10 没有 receipts 端点）。

    协议未定义就加端点 = 替协议做决定（WP12 §3 V4 的教训）。
    已读标记的语义（"送达"还是"已读"）需要协议明确（OP-117）。
    """
    from fastapi import FastAPI

    from rednote.main import create_app

    app: FastAPI = create_app(SETTINGS)
    paths = {
        route.path
        for route in app.routes
        if hasattr(route, "methods")
    }
    assert not any("receipt" in path for path in paths), (
        f"不该有 receipts 端点，实际：{sorted(p for p in paths if 'receipt' in p)}"
    )


# ===========================================================================
# HTTP 层
# ===========================================================================


def test_wp15_routes_are_registered() -> None:
    from fastapi import FastAPI

    from rednote.main import create_app

    app: FastAPI = create_app(SETTINGS)
    paths = {
        (method, route.path)
        for route in app.routes
        if hasattr(route, "methods")
        for method in (route.methods or set())
    }
    for method, path in (
        ("GET", "/api/v1/work-items"),
        ("POST", "/api/v1/work-items/{workItemId}/resolutions"),
        ("GET", "/api/v1/audit-events"),
    ):
        assert (method, path) in paths, f"缺少端点 {method} {path}"


def test_unauthenticated_requests_are_rejected() -> None:
    from fastapi.testclient import TestClient

    from rednote.main import create_app

    with TestClient(create_app(SETTINGS)) as client:
        for path in ("/api/v1/work-items", "/api/v1/audit-events"):
            response = client.get(path)
            assert response.status_code == 401, path
            assert response.json()["error"]["code"] == "SESSION_EXPIRED"


def test_cursor_encoding_roundtrip() -> None:
    """游标编解码必须能往返（编码与解析是两处，写错会让分页永远翻不动）。"""
    from rednote.modules.audit.api import _decode_anchor, _encode_anchor

    moment = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
    identifier = uuid.uuid4()
    encoded = _encode_anchor((moment, identifier))
    assert encoded is not None
    assert _decode_anchor(encoded, field="cursor") == (moment, identifier)
    assert _encode_anchor(None) is None
    assert _decode_anchor(None, field="cursor") is None


def test_malformed_cursor_is_rejected_not_silently_ignored() -> None:
    """非法游标报 400（**不静默当第一页**）。

    与 WP14 的 `Last-Event-ID`（非法当新连接）相反，理由是：
    游标非法若被当第一页，客户端会从头开始翻、**无限循环**。
    两者的差别在于"静默降级会不会造成错误数据"。
    """
    from rednote.modules.audit.api import _decode_anchor

    for bad in ("nonsense", "not-a-date|not-a-uuid", "|"):
        with pytest.raises(ApiError) as excinfo:
            _decode_anchor(bad, field="cursor")
        assert excinfo.value.code is ErrorCode.MALFORMED_REQUEST, bad