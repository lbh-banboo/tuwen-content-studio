"""WP11 测试：版本注册与引用、不可变保护、草稿升格（含 ETag 语义）。

覆盖 WP11 验收标准（研发计划 04 §4.2）：
> 旧版本不可改，跨项目版本引用拒绝

**测试策略（分三层，与 WP09/WP10 一致）**：

1. **纯函数层**（不碰数据库）：内容摘要规范化、不可变 ETag 形态、
   可见性矩阵、触发器命名。这些是"规则正确性"的权威测试。
2. **服务层 + 真库**：`register_version` / `link_dependency` / `promote_draft` /
   `list_version_history`。WP11 没有 HTTP 端点（协议未定义，见
   `docs/m1/WP11-版本注册与引用说明.md` §3），因此端到端验证打在服务层 ——
   本包的真实消费者是代码，不是浏览器。
3. **数据库层兜底**：用**原生 SQL** 绕过 ORM 与 service，直接验证
   触发器与 CHECK 真的拦得住。这一层是本包最重要的测试 ——
   "旧版本不可改"若只测 ORM 路径，等于没测（手工 SQL 才是真实威胁）。

**为什么第 3 层必须存在**：不可变保护的设计动机就是"绕过 ORM 的写入路径
（psql / 数据回填 / 运维脚本）没有代码评审"。若测试也只用 ORM，
那么测试与实现共享同一个盲区 —— 保护失效时两边一起不知道。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import date, datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from rednote.modules.identity.dependencies import CurrentUser
from rednote.modules.identity.models import (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    User,
)
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.projects.models import (
    Brand,
    Product,
    Project,
    ProjectMember,
)
from rednote.modules.projects.repository import ProjectRepository
from rednote.modules.versions import ddl as versions_ddl
from rednote.modules.versions.models import (
    ENTITY_TYPE_BRIEF,
    ENTITY_TYPE_CONTENT,
    ENTITY_TYPE_KNOWLEDGE,
    ENTITY_TYPE_PROMPT,
    ENTITY_TYPES,
    MAX_REVISION_NO,
    MIN_SCHEMA_VERSION,
    VersionDependency,
    VersionNode,
)
from rednote.modules.versions.permissions import (
    can_link_versions,
    can_view_version,
)
from rednote.modules.versions.repository import VersionRepository
from rednote.modules.versions.service import (
    canonical_payload_hash,
    fragment_hash,
    get_version,
    link_dependency,
    list_version_dependencies,
    list_version_dependents,
    list_version_history,
    promote_draft,
    register_version,
)
from rednote.platform.concurrency import build_immutable_etag, parse_etag
from rednote.platform.configuration import Settings
from rednote.platform.database import session_scope
from rednote.platform.errors import ApiError, ErrorCode
from rednote.platform.immutability import (
    IMMUTABLE_RECORD_SQLSTATE,
    ImmutableRecordError,
    immutable_trigger_name,
)

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
# 测试库 DSN。**必须从环境变量读**（ISS-108）：硬编码会让
# `REDNOTE_TEST_DATABASE_URL` 约定的「测试库物理隔离」失效 —— 测试会把
# 数据写进本机默认库（实测跑一次全量 +367 行 users），且 CI 里 PG 用户是
# `postgres` 而不是 `rednote`，硬编码会直接连不上（role 不存在）。
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
TEST_DATABASE_URL = os.environ.get(
    "REDNOTE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL
).strip()


def _make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": TEST_DATABASE_URL,
        "environment": "test",
        "session_cookie_secure": False,
        "oidc_issuer": TEST_ISSUER,
        "oidc_token_encryption_key": TEST_ENCRYPTION_KEY,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def app() -> FastAPI:
    from rednote.main import create_app

    return create_app(_make_settings())


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


# ===========================================================================
# 第 1 层：纯函数（不碰数据库）
# ===========================================================================


# --- 内容摘要规范化 --------------------------------------------------------


def test_payload_hash_is_64_hex_chars() -> None:
    digest = canonical_payload_hash({"summary": "v1"})
    assert len(digest) == 64
    assert all(char in "0123456789abcdef" for char in digest)


def test_payload_hash_ignores_key_order() -> None:
    """键顺序不同必须算出同一个摘要。

    **这是幂等注册的前提**：网络重试或不同序列化器产出的 JSON 键序可能不同，
    若摘要随键序变化，"重复请求"会被误判成"内容变了"而被拒 ——
    调用方拿到一个 409，却完全不知道自己做错了什么。
    """
    first = canonical_payload_hash({"a": 1, "b": 2, "c": 3})
    second = canonical_payload_hash({"c": 3, "b": 2, "a": 1})
    assert first == second


def test_payload_hash_ignores_nested_key_order() -> None:
    first = canonical_payload_hash({"outer": {"x": 1, "y": 2}})
    second = canonical_payload_hash({"outer": {"y": 2, "x": 1}})
    assert first == second


def test_payload_hash_changes_with_content() -> None:
    assert canonical_payload_hash({"a": 1}) != canonical_payload_hash({"a": 2})


def test_payload_hash_handles_non_json_native_types() -> None:
    """UUID / datetime 不能直接 JSON 序列化。

    不做 `default=str` 的话，`json.dumps` 会抛 TypeError，
    于是"能不能注册版本"取决于 payload 里恰好有没有这些类型 ——
    一个与业务无关的偶然。这里锁定"可以注册"。
    """
    digest = canonical_payload_hash(
        {"id": uuid.uuid4(), "nested": {"when": "2026-09-21T00:00:00Z"}}
    )
    assert len(digest) == 64


def test_fragment_hash_is_content_addressed() -> None:
    assert fragment_hash("片段") == fragment_hash("片段")
    assert fragment_hash("片段") != fragment_hash("片段2")


# --- 不可变 ETag -----------------------------------------------------------


def test_immutable_etag_has_h_marker_and_content_prefix() -> None:
    """形态：`"<uuid>:h<16 位摘要>"`。"""
    node_id = uuid.uuid4()
    digest = "a" * 64
    assert build_immutable_etag(node_id, digest) == f'"{node_id}:h{"a" * 16}"'


def test_immutable_etag_is_stable_for_same_content() -> None:
    """同一内容任何时刻算出同一个 ETag —— 这是"不可变"的可观察证据。"""
    node_id = uuid.uuid4()
    assert build_immutable_etag(node_id, "b" * 64) == build_immutable_etag(
        node_id, "b" * 64
    )


def test_immutable_etag_changes_when_content_changes() -> None:
    """内容被改则 ETag 变 —— 它同时是篡改检测器。"""
    node_id = uuid.uuid4()
    assert build_immutable_etag(node_id, "b" * 64) != build_immutable_etag(
        node_id, "c" * 64
    )


@pytest.mark.parametrize(
    "digest",
    [
        "0" * 64,  # 全 0
        "1" * 64,  # 全数字：若没有 h 前缀，parse_etag 会误判成版本号
        "1234567890123456" + "a" * 48,
        "f" * 64,
    ],
)
def test_immutable_etag_is_never_parseable_as_versioned_etag(digest: str) -> None:
    """不可变 ETag 在任何取值下都不能被 `parse_etag` 接受。

    **为什么专门测这个**：若不可变 ETag 直接写 16 位十六进制，
    那么当它恰好全是数字时（概率约 0.27%），`parse_etag` 会把它当成合法版本号，
    于是"把不可变 ETag 塞进 If-Match"这个错误用法会静默变成
    "版本不匹配"（412）而不是"格式错误"（400）—— 前端分支随之走错。
    加 `h` 前缀让这个边界不随运气漂移。
    """
    etag = build_immutable_etag(uuid.uuid4(), digest)
    assert parse_etag(etag) is None


# --- 可见性矩阵（纯函数）---------------------------------------------------


@pytest.mark.parametrize(
    ("project_id", "is_admin", "is_member", "expected"),
    [
        # 项目内版本
        (uuid.uuid4(), False, True, True),
        (uuid.uuid4(), False, False, False),
        (uuid.uuid4(), True, False, True),
        (uuid.uuid4(), True, True, True),
        # 平台级版本（project_id 为空）—— 仅管理者可见
        (None, False, True, False),
        (None, False, False, False),
        (None, True, False, True),
        (None, True, True, True),
    ],
)
def test_can_view_version_matrix(
    project_id: uuid.UUID | None,
    is_admin: bool,
    is_member: bool,
    expected: bool,
) -> None:
    assert (
        can_view_version(
            project_id=project_id, is_admin=is_admin, is_project_member=is_member
        )
        is expected
    )


def test_platform_version_is_hidden_from_non_admin_even_if_member() -> None:
    """平台级版本与项目无关，成员身份不能让它变可见。

    这条单独写出来，是因为它最容易被"顺手"改错：
    成员身份与可见性在项目内版本上是关联的，有人会把
    `is_member` 当成万能放行条件。
    """
    assert (
        can_view_version(project_id=None, is_admin=False, is_project_member=True)
        is False
    )


@pytest.mark.parametrize(
    ("is_admin", "is_member", "expected"),
    [(True, False, True), (False, True, True), (False, False, False)],
)
def test_can_link_versions_matches_visibility_rule(
    is_admin: bool, is_member: bool, expected: bool
) -> None:
    assert can_link_versions(is_admin=is_admin, is_project_member=is_member) is expected


# --- 实体类型取值域 --------------------------------------------------------


def test_entity_types_cover_documented_version_families() -> None:
    """数据结构文档 02 §5 的 7 类 + 基线（§6 的 production_baselines 也版本化）。"""
    assert ENTITY_TYPES == {
        "BRIEF",
        "PLAN",
        "CONTENT",
        "KNOWLEDGE",
        "MEDIA",
        "PROMPT",
        "REPORT",
        "BASELINE",
    }
    assert len(ENTITY_TYPES) == 8


@pytest.mark.cross_wp
def test_entity_type_check_constraint_matches_python_set() -> None:
    """CHECK 里的取值列表必须与 `ENTITY_TYPES` 完全一致。

    两处不一致的后果：Python 放行了一个 CHECK 不接受的值，
    于是"看起来合法"的请求在 flush 时抛 IntegrityError ——
    变成 500，而调用方只得到一个内部错误。
    """
    constraint = next(
        c
        for c in VersionNode.__table__.constraints
        if c.name == "ck_version_nodes_entity_type_valid"
    )
    sql = str(constraint.sqltext)
    for name in ENTITY_TYPES:
        assert f"'{name}'" in sql, f"CHECK 里缺少 {name}"
    # 反向：CHECK 里的引号字符串个数应与集合大小一致（无多余取值）
    assert sql.count("'") == len(ENTITY_TYPES) * 2


# --- 不可变守卫（不落库的形态检查）----------------------------------------


def test_immutable_record_error_is_not_business_error() -> None:
    """`ImmutableRecordError` 不能是 `ApiError`。

    理由：它代表**程序缺陷**（写代码的人搞错了对象类型），不是用户能纠正的
    请求问题。若折算成 4xx，这类 bug 会被当成"用户操作不对"长期潜伏，
    而不是被监控告警捞出来。
    """
    assert not issubclass(ImmutableRecordError, ApiError)
    assert issubclass(ImmutableRecordError, RuntimeError)


def test_immutable_trigger_name_is_table_scoped() -> None:
    assert immutable_trigger_name("version_nodes") == "trg_version_nodes_immutable"
    assert immutable_trigger_name("x") != immutable_trigger_name("y")


def test_version_node_has_no_mutable_columns() -> None:
    """不可变表不得带 `lock_version` / `updated_at`。

    数据结构文档 02 §1 规定 mutable 表才有这两个列。给不可变表加上
    `lock_version` 是在暗示"它会被改"，与不可变性自相矛盾，
    而且会让下游误以为可以对它用 `If-Match`。
    """
    columns = set(VersionNode.__table__.columns.keys())
    assert "lock_version" not in columns
    assert "updated_at" not in columns


def test_version_dependency_has_no_mutable_columns() -> None:
    columns = set(VersionDependency.__table__.columns.keys())
    assert "lock_version" not in columns
    assert "updated_at" not in columns


# ===========================================================================
# 第 2 + 3 层：真库
# ===========================================================================


# --- 装配 helper（本文件自包含，不依赖 conftest 的隐式状态）-----------------


def _create_user(*, role: str = ROLE_OPERATOR) -> uuid.UUID:
    with session_scope(_make_settings()) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name=f"用户-{role}",
            role=role,
        )
        return user.id


# 占位会话 id。`CurrentUser.session_id` 的唯一消费点是写请求的 CSRF 校验
# （identity/dependencies.py: if is_write_method(request.method): verify_csrf(...)）。
# WP11 不提供任何 HTTP 端点 —— 权限判定走的是 `permissions.py` 的纯函数，
# 入参只有 is_admin / is_project_member，根本不读 session_id。
# 故这里给一个固定常量，而不是伪造一条真实会话行：制造"这是个有效会话"的假象
# 只会让后来者误以为测试覆盖了 CSRF 链路。
_PLACEHOLDER_SESSION_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


def _current_user(user_id: uuid.UUID) -> CurrentUser:
    with session_scope(_make_settings()) as session:
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
    """直接建库造项目（版本表有指向 projects 的外键，必须先有项目）。"""
    with session_scope(_make_settings()) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name="测试产品")
        session.add(product)
        session.flush()
        project = Project(
            project_code=f"VER-{uuid.uuid4().hex[:8].upper()}",
            name="版本测试项目",
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


def _register(
    *,
    entity_type: str = ENTITY_TYPE_BRIEF,
    entity_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    payload: dict[str, object] | None = None,
    created_by: uuid.UUID | None = None,
    schema_version: int = MIN_SCHEMA_VERSION,
    revision_no: int | None = None,
):
    with session_scope(_make_settings()) as session:
        return register_version(
            VersionRepository(session),
            entity_type=entity_type,
            entity_id=entity_id or uuid.uuid4(),
            payload=payload if payload is not None else {"summary": "v1"},
            project_id=project_id,
            created_by=created_by,
            schema_version=schema_version,
            revision_no=revision_no,
        )


def _link(
    parent: uuid.UUID,
    dependency: uuid.UUID,
    *,
    purpose: str = "FACT",
    fragment: str | None = None,
):
    with session_scope(_make_settings()) as session:
        return link_dependency(
            VersionRepository(session),
            parent_version_id=parent,
            dependency_version_id=dependency,
            purpose=purpose,
            selected_fragment=fragment,
        )


def _raw_execute(sql: str, params: dict[str, object]) -> None:
    """用原生 SQL 执行一条语句（模拟绕过 ORM 的写入路径）。"""
    with session_scope(_make_settings()) as session:
        session.execute(text(sql), params)


# --- 注册版本 --------------------------------------------------------------


def test_register_version_starts_at_revision_one() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    result = _register(project_id=project_id, created_by=owner)

    assert result.revision_no == 1
    assert result.created is True
    assert result.entity_type == ENTITY_TYPE_BRIEF
    assert result.project_id == project_id


def test_register_version_auto_increments_revision() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    entity_id = uuid.uuid4()

    first = _register(entity_id=entity_id, project_id=project_id, payload={"n": 1})
    second = _register(entity_id=entity_id, project_id=project_id, payload={"n": 2})
    third = _register(entity_id=entity_id, project_id=project_id, payload={"n": 3})

    assert [first.revision_no, second.revision_no, third.revision_no] == [1, 2, 3]
    assert len({first.node_id, second.node_id, third.node_id}) == 3


def test_revision_numbers_are_per_entity_not_per_type() -> None:
    """修订号按 `(entity_type, entity_id)` 单独计数。

    两条 Brief 各有自己的第 1 版 —— 若误按 entity_type 全局计数，
    第二个实体的第一个版本会变成第 2 版，版本历史直接错乱。
    """
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    entity_a, entity_b = uuid.uuid4(), uuid.uuid4()

    a1 = _register(entity_id=entity_a, project_id=project_id)
    b1 = _register(entity_id=entity_b, project_id=project_id)

    assert a1.revision_no == 1
    assert b1.revision_no == 1


@pytest.mark.regression
def test_register_version_is_idempotent_for_identical_payload() -> None:
    """**幂等的前提是显式给出修订号。**

    WP11 文档 §2 F5 对幂等的定义是"同 `(entity_type, entity_id, revision_no)`
    已存在时比对 `payload_hash`" —— 幂等键就是这个三元组本身。
    因此这里两次都必须传 `revision_no=1`，否则第二次会拿到第 2 版，
    那属于"新增了一个版本"，不是"同一次操作重复抵达"。
    """
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    entity_id = uuid.uuid4()

    first = _register(
        entity_id=entity_id, project_id=project_id, payload={"a": 1}, revision_no=1
    )
    second = _register(
        entity_id=entity_id, project_id=project_id, payload={"a": 1}, revision_no=1
    )

    assert first.created is True
    assert second.created is False
    assert second.node_id == first.node_id
    assert second.revision_no == first.revision_no == 1


@pytest.mark.regression
def test_register_version_without_revision_is_not_idempotent() -> None:
    """自动追加（`revision_no=None`）**没有幂等键**：相同内容会落到新修订号上。

    这条测试的作用是把当前语义钉死，并同时记录它为什么是个缺口：
    "发布一个新版本"这个动作没有客户端可提供的幂等键（入参里没有
    request-id 之类的东西），所以一次超时重试会写出两个**内容摘要相同、
    修订号不同**的版本。

    后果是可预期的 —— 版本历史里堆出连续的同内容版本，下游"最近有没有变更"
    的判断失真。这与 `promote_draft` 专门用 422 `draft_unchanged` 拒绝
    "空保存"是同一类问题，只是一个拦在了入口、一个没有幂等键可拦。

    处理方式：已登记为待定项（缺幂等键），**不擅自在协议层拍板前改语义**。
    若将来要求"与最新版内容相同即视为幂等命中"，改这一条测试即可。
    """
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    entity_id = uuid.uuid4()

    first = _register(entity_id=entity_id, project_id=project_id, payload={"a": 1})
    second = _register(entity_id=entity_id, project_id=project_id, payload={"a": 1})

    assert first.revision_no == 1
    assert second.revision_no == 2
    assert second.created is True
    assert second.node_id != first.node_id
    # 摘要一致而修订号不同 —— 这正是"重复投递"的特征。
    assert second.payload_hash == first.payload_hash


def test_register_version_rejects_conflicting_payload_on_same_revision() -> None:
    """同一修订号装不同内容 → 409，不是静默覆盖。

    版本不可改写（触发器守着），因此"同一号装不同内容"只能是调用方搞错了
    版本号。静默覆盖会让已引用该版本的上下游全部指向错误内容，
    而且没有任何痕迹。
    """
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    entity_id = uuid.uuid4()

    _register(entity_id=entity_id, project_id=project_id, payload={"a": 1})

    with pytest.raises(ApiError) as excinfo:
        _register(
            entity_id=entity_id,
            project_id=project_id,
            payload={"a": 2},
            revision_no=1,
        )
    assert excinfo.value.code is ErrorCode.INVALID_TRANSITION
    assert excinfo.value.details["reason"] == "revision_payload_mismatch"
    assert excinfo.value.details["revisionNo"] == 1


def test_register_version_rejects_unknown_entity_type() -> None:
    with pytest.raises(ApiError) as excinfo:
        _register(entity_type="BRIEF_VERSION", payload={"a": 1})
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
    assert excinfo.value.details["reason"] == "entity_type_unknown"


def test_register_version_rejects_schema_version_below_one() -> None:
    with pytest.raises(ApiError) as excinfo:
        _register(schema_version=0)
    assert excinfo.value.details["reason"] == "schema_version_too_small"


def test_register_version_rejects_unknown_project() -> None:
    with pytest.raises(ApiError) as excinfo:
        _register(project_id=uuid.uuid4())
    assert excinfo.value.details["reason"] == "project_not_found"


def test_register_version_rejects_unknown_creator() -> None:
    with pytest.raises(ApiError) as excinfo:
        _register(created_by=uuid.uuid4())
    assert excinfo.value.details["reason"] == "user_not_found"


@pytest.mark.parametrize("bad_revision", [0, -1, MAX_REVISION_NO + 1])
def test_register_version_rejects_out_of_range_revision(bad_revision: int) -> None:
    with pytest.raises(ApiError) as excinfo:
        _register(revision_no=bad_revision)
    assert excinfo.value.details["reason"] == "revision_no_out_of_range"


def test_register_version_allows_platform_level_node_without_project() -> None:
    """平台级版本（`project_id IS NULL`）合法 —— Prompt 模板版本就是这样。"""
    result = _register(
        entity_type=ENTITY_TYPE_PROMPT, project_id=None, payload={"tpl": "x"}
    )
    assert result.project_id is None
    assert result.revision_no == 1


def test_concurrent_registration_retries_and_allocates_distinct_revisions() -> None:
    """并发注册不能出现同号两个节点。

    这里用"显式指定同一个修订号两次"来构造冲突路径：
    第一次成功后，第二次带不同内容会被 `revision_payload_mismatch` 拦下
    （这是正确的终态），关键是**不能出现两条 revision_no 相同的行**。
    数据库层的唯一约束是这条保证的真正来源。
    """
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    entity_id = uuid.uuid4()

    _register(entity_id=entity_id, project_id=project_id, payload={"n": 1})
    with pytest.raises(ApiError):
        _register(
            entity_id=entity_id, project_id=project_id, payload={"n": 2}, revision_no=1
        )

    with session_scope(_make_settings()) as session:
        count = session.execute(
            select(func.count())
            .select_from(VersionNode)
            .where(VersionNode.entity_id == entity_id)
        ).scalar_one()
    assert count == 1


# --- 不可变保护：ORM 路径 --------------------------------------------------


def test_immutable_node_update_via_orm_is_rejected() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    node = _register(project_id=project_id)

    with pytest.raises(ImmutableRecordError):
        with session_scope(_make_settings()) as session:
            row = session.get(VersionNode, node.node_id)
            assert row is not None
            row.payload_hash = "0" * 64
            session.flush()


def test_immutable_node_delete_via_orm_is_rejected() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    node = _register(project_id=project_id)

    with pytest.raises(ImmutableRecordError):
        with session_scope(_make_settings()) as session:
            row = session.get(VersionNode, node.node_id)
            assert row is not None
            session.delete(row)
            session.flush()


@pytest.mark.regression
def test_reading_a_node_without_changing_it_is_allowed() -> None:
    """只读不写不能触发守卫。

    单独测这条：`before_update` 只在实例处于 dirty 状态时才触发，
    若实现里误用 `before_flush` 或无条件守卫，**每一次普通查询都会炸** ——
    那是灾难性的回归，必须有测试守住。
    """
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    node = _register(project_id=project_id)

    with session_scope(_make_settings()) as session:
        row = session.get(VersionNode, node.node_id)
        assert row is not None
        assert row.revision_no == 1
        # 赋同样的值不算修改。
        row.revision_no = 1
        session.flush()


# --- 不可变保护：绕过 ORM 的原生 SQL（本包最重要的三条）--------------------


def test_raw_sql_update_on_version_node_is_rejected_by_trigger() -> None:
    """**验收项「旧版本不可改」的数据库层证据。**

    用原生 SQL 绕过 ORM 与 service 直接改版本内容 —— 若这一条能通过，
    说明不可变保护只是"代码里没写 UPDATE"，而不是真正的数据约束。
    """
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    node = _register(project_id=project_id)

    with pytest.raises(DBAPIError) as excinfo:
        _raw_execute(
            "UPDATE version_nodes SET payload_hash = :h WHERE id = :i",
            {"h": "0" * 64, "i": node.node_id},
        )
    assert excinfo.value.orig.sqlstate == IMMUTABLE_RECORD_SQLSTATE

    # 内容必须原样保留。
    with session_scope(_make_settings()) as session:
        row = session.get(VersionNode, node.node_id)
        assert row is not None
        assert row.payload_hash == node.payload_hash


def test_raw_sql_delete_on_version_node_is_rejected_by_trigger() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    node = _register(project_id=project_id)

    with pytest.raises(DBAPIError) as excinfo:
        _raw_execute("DELETE FROM version_nodes WHERE id = :i", {"i": node.node_id})
    assert excinfo.value.orig.sqlstate == IMMUTABLE_RECORD_SQLSTATE

    with session_scope(_make_settings()) as session:
        assert session.get(VersionNode, node.node_id) is not None


def test_raw_sql_update_on_dependency_edge_is_rejected_by_trigger() -> None:
    """依赖边同样是历史事实：改一条边等于篡改溯源记录。"""
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    parent = _register(project_id=project_id, payload={"p": 1})
    dependency = _register(project_id=project_id, payload={"d": 1})
    _link(parent.node_id, dependency.node_id)

    with pytest.raises(DBAPIError) as excinfo:
        _raw_execute(
            "UPDATE version_dependencies SET purpose = 'STYLE' "
            "WHERE parent_version_id = :p AND dependency_version_id = :d",
            {"p": parent.node_id, "d": dependency.node_id},
        )
    assert excinfo.value.orig.sqlstate == IMMUTABLE_RECORD_SQLSTATE


def test_raw_sql_delete_on_dependency_edge_is_rejected_by_trigger() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    parent = _register(project_id=project_id, payload={"p": 2})
    dependency = _register(project_id=project_id, payload={"d": 2})
    _link(parent.node_id, dependency.node_id)

    with pytest.raises(DBAPIError) as excinfo:
        _raw_execute(
            "DELETE FROM version_dependencies "
            "WHERE parent_version_id = :p AND dependency_version_id = :d",
            {"p": parent.node_id, "d": dependency.node_id},
        )
    assert excinfo.value.orig.sqlstate == IMMUTABLE_RECORD_SQLSTATE


def test_orm_enabled_update_statement_is_rejected_by_trigger_only() -> None:
    """ORM-enabled DML（`session.execute(update(...))`）**不走** `before_update`。

    锁定 `platform/immutability.py` 模块 docstring 里写明的那个盲区：
    mapper 级 `before_update` 只在 flush 流程里触发，而这条路径是直接执行 DML，
    因此**只有数据库触发器**能拦住它。

    为什么必须单独测：这段代码看起来是"ORM 写法"，很容易被误认为
    "有 ORM 守卫就够了"。一旦有人把触发器当成冗余删掉、只留 ORM 事件，
    本包的核心验收项（旧版本不可改）就会在**新人最可能写出的那种写法**上
    悄悄破掉 —— 而其余测试全走"改实例属性 + flush"那条路径，谁也发现不了。
    """
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    node = _register(project_id=project_id)

    with pytest.raises(DBAPIError) as excinfo:
        with session_scope(_make_settings()) as session:
            session.execute(
                update(VersionNode)
                .where(VersionNode.id == node.node_id)
                .values(payload_hash="0" * 64)
            )
            session.flush()

    assert excinfo.value.orig.sqlstate == IMMUTABLE_RECORD_SQLSTATE

    # 内容必须原样保留。
    with session_scope(_make_settings()) as session:
        row = session.get(VersionNode, node.node_id)
        assert row is not None
        assert row.payload_hash == node.payload_hash


def test_expected_triggers_exist_in_database() -> None:
    """迁移建出的触发器必须真的在库里，且名字与代码一致。

    只测行为（上面几条）不够：若触发器名变了而代码没跟上，
    验证脚本会查不到它；这里的断言让"名字"本身成为契约。
    """
    with session_scope(_make_settings()) as session:
        rows = session.execute(
            text(
                "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE NOT t.tgisinternal AND c.relname IN "
                "('version_nodes', 'version_dependencies')"
            )
        ).scalars()
        present = set(rows)
    missing = set(versions_ddl.trigger_names()) - present
    assert not missing, f"数据库里缺少触发器：{sorted(missing)}"


# --- DDL 归属：`downgrade` 能"只删自己建的"的前提（ISS-79）------------------
#
# 这三条合起来钉住一件事：**本包同时拥有两类函数，归属不同**。
# 原 `downgrade` 第 2 步遍历 `all_trigger_ddl()` 的第 5 项去 DROP FUNCTION，
# 把"别人的共用函数"也拉进来 ⇒ `alembic downgrade base` 在 WP15 那一步撞
# `cannot drop function rednote_reject_mutation() because other objects depend
# on it` ⇒ 回滚路径不存在 ⇒ 阻塞 WP47 的 RPO/RTO 演练。


def test_consistency_items_own_only_scope_function() -> None:
    """`consistency_items()` 恰好一项，且**只**指向本包自己的 scope 函数。

    ★ 本包与 WP23/WP25 不同：它们要么全是自己的函数，要么全无。
    本包 `all_trigger_ddl()` 里**混着**两类 ——
      · 2 项不可变保护 → `rednote_reject_mutation`（**别人的**，不许删）；
      · 1 项 scope      → `rednote_check_dependency_scope`（**我的**，必须删）。
    因此"第 2 步该删什么"必须由本函数显式回答，而不能从 `all_trigger_ddl()`
    的形状里推（它们的第 5 项长得一模一样）。
    """
    items = versions_ddl.consistency_items()
    assert len(items) == 1, items
    assert items[0][0] == "version_dependencies"
    assert versions_ddl.DEPENDENCY_SCOPE_FUNCTION in items[0][4]


def test_consistency_items_excludes_shared_function() -> None:
    """`consistency_items()` 的每个 `drop_function` 都不含共用函数名。

    这条是 `downgrade` 安全性的**唯一前提**：第 2 步只迭代本函数的返回值，
    因此共用函数永远进不来。若哪天有人把不可变项挪进 `consistency_items()`
    （或把 `all_trigger_ddl()` 的切片错写成范围），这条当场报红 ——
    而不是等到某次 `downgrade base` 才炸。
    """
    drops = {item[4] for item in versions_ddl.consistency_items()}
    assert drops, "本包**有**自己的函数，不能返回空集（与 WP24/WP25 不同）"
    assert not any("rednote_reject_mutation" in sql for sql in drops), drops


@pytest.mark.cross_wp
def test_all_trigger_ddl_mentions_shared_function() -> None:
    """`all_trigger_ddl()` 里的不可变保护**指向共用函数**（建它是对的）。

    与 WP23/WP25 同款反向断言：建用 `CREATE OR REPLACE` 是**幂等**的，
    删才是危险的。这条与上一条互为对照 —— 同一个函数名，
    在 `consistency_items()` 里**必须缺席**、在 `all_trigger_ddl()` 里
    **必须出现**。两条一起才能表达"建它但不删它"。
    """
    all_drops = {item[4] for item in versions_ddl.all_trigger_ddl()}
    assert any("rednote_reject_mutation" in sql for sql in all_drops)
    assert versions_ddl.immutable_function_names() == ("rednote_reject_mutation",)
    # 覆盖关系：consistency 项必须包含在 all 项里（否则 downgrade 会漏删）。
    all_items = versions_ddl.all_trigger_ddl()
    for own in versions_ddl.consistency_items():
        assert own in all_items, own


# --- 依赖边：服务层校验 ----------------------------------------------------


def test_link_dependency_creates_edge() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    parent = _register(project_id=project_id, payload={"p": "x"})
    dependency = _register(project_id=project_id, payload={"d": "x"})

    link = _link(parent.node_id, dependency.node_id, fragment="选中的片段")
    assert link.created is True
    assert link.purpose == "FACT"


def test_link_dependency_is_idempotent() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    parent = _register(project_id=project_id, payload={"p": "y"})
    dependency = _register(project_id=project_id, payload={"d": "y"})

    first = _link(parent.node_id, dependency.node_id)
    second = _link(parent.node_id, dependency.node_id)

    assert first.created is True
    assert second.created is False


def test_same_pair_with_different_purpose_is_allowed() -> None:
    """同一对版本可以因不同用途建立两条边。

    这是主键包含 `purpose` 的理由：同一个上游版本既可能被当 `FACT`
    依据、又可能被当 `STYLE` 参考。主键若只有两端，第二种用途就无法记录。
    """
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    parent = _register(project_id=project_id, payload={"p": "z"})
    dependency = _register(project_id=project_id, payload={"d": "z"})

    assert _link(parent.node_id, dependency.node_id, purpose="FACT").created is True
    assert _link(parent.node_id, dependency.node_id, purpose="STYLE").created is True

    with session_scope(_make_settings()) as session:
        edges = (
            session.execute(
                select(VersionDependency).where(
                    VersionDependency.parent_version_id == parent.node_id
                )
            )
            .scalars()
            .all()
        )
    assert len(edges) == 2


def test_link_dependency_rejects_self_reference() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    node = _register(project_id=project_id)

    with pytest.raises(ApiError) as excinfo:
        _link(node.node_id, node.node_id)
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
    assert excinfo.value.details["reason"] == "self_reference"


def test_link_dependency_rejects_unknown_endpoints() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    node = _register(project_id=project_id, payload={"p": "u"})

    with pytest.raises(ApiError) as excinfo:
        _link(node.node_id, uuid.uuid4())
    assert excinfo.value.details["reason"] == "version_not_found"

    with pytest.raises(ApiError) as excinfo2:
        _link(uuid.uuid4(), node.node_id)
    assert excinfo2.value.details["reason"] == "version_not_found"


def test_link_dependency_rejects_empty_purpose() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    parent = _register(project_id=project_id, payload={"p": "e"})
    dependency = _register(project_id=project_id, payload={"d": "e"})

    with pytest.raises(ApiError) as excinfo:
        _link(parent.node_id, dependency.node_id, purpose="   ")
    assert excinfo.value.details["reason"] == "purpose_empty"


# --- 跨项目引用拒绝（验收项，服务层 + 数据库层各一条）----------------------


def test_link_dependency_rejects_cross_project_reference() -> None:
    """**验收项「跨项目版本引用拒绝」的服务层证据。**"""
    owner = _create_user()
    project_a = _seed_project(owner_id=owner)
    project_b = _seed_project(owner_id=owner)

    parent = _register(project_id=project_a, payload={"p": "a"})
    dependency = _register(project_id=project_b, payload={"d": "b"})

    with pytest.raises(ApiError) as excinfo:
        _link(parent.node_id, dependency.node_id)

    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
    assert excinfo.value.details["reason"] == "cross_project_reference"
    assert excinfo.value.details["parentProjectId"] == str(project_a)
    assert excinfo.value.details["dependencyProjectId"] == str(project_b)


def test_cross_project_edge_is_rejected_at_database_level() -> None:
    """**验收项「跨项目版本引用拒绝」的数据库层证据。**

    绕过 service 用原生 SQL 直接插一条跨项目边 —— 触发器必须拦下。
    没有这一条，服务层的校验就只是"我们记得调用那个函数"。
    """
    owner = _create_user()
    project_a = _seed_project(owner_id=owner)
    project_b = _seed_project(owner_id=owner)
    parent = _register(project_id=project_a, payload={"p": "raw-a"})
    dependency = _register(project_id=project_b, payload={"d": "raw-b"})

    with pytest.raises(DBAPIError) as excinfo:
        _raw_execute(
            "INSERT INTO version_dependencies "
            "(parent_version_id, dependency_version_id, purpose) "
            "VALUES (:p, :d, 'FACT')",
            {"p": parent.node_id, "d": dependency.node_id},
        )
    assert excinfo.value.orig.sqlstate == versions_ddl.DEPENDENCY_SCOPE_SQLSTATE


def test_raw_self_reference_is_rejected_by_check_constraint() -> None:
    """自引用在数据库层也有 CHECK 兜底（绕过 service 也拦得住）。"""
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    node = _register(project_id=project_id, payload={"s": 1})

    with pytest.raises(IntegrityError) as excinfo:
        _raw_execute(
            "INSERT INTO version_dependencies "
            "(parent_version_id, dependency_version_id, purpose) "
            "VALUES (:p, :d, 'FACT')",
            {"p": node.node_id, "d": node.node_id},
        )
    assert "ck_version_dependencies_no_self_reference" in str(excinfo.value.orig)


def test_platform_level_edges_are_allowed() -> None:
    """两端都无项目归属时不受"同项目"限制（没有项目就不是跨项目）。"""
    template_a = _register(entity_type=ENTITY_TYPE_PROMPT, payload={"t": 1})
    template_b = _register(entity_type=ENTITY_TYPE_PROMPT, payload={"t": 2})

    link = _link(template_a.node_id, template_b.node_id, purpose="REFERENCE")
    assert link.created is True


# --- 依赖成环 --------------------------------------------------------------


def test_link_dependency_rejects_cycle() -> None:
    """A→B 之后，B→A 必须被拒（否则溯源遍历会转圈）。"""
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    a = _register(project_id=project_id, payload={"n": "a"})
    b = _register(project_id=project_id, payload={"n": "b"})

    _link(a.node_id, b.node_id)

    with pytest.raises(ApiError) as excinfo:
        _link(b.node_id, a.node_id)
    assert excinfo.value.details["reason"] == "dependency_cycle"


def test_link_dependency_rejects_three_node_cycle() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    a = _register(project_id=project_id, payload={"n": "3a"})
    b = _register(project_id=project_id, payload={"n": "3b"})
    c = _register(project_id=project_id, payload={"n": "3c"})

    _link(a.node_id, b.node_id, purpose="P1")
    _link(b.node_id, c.node_id, purpose="P1")

    with pytest.raises(ApiError) as excinfo:
        _link(c.node_id, a.node_id, purpose="P1")
    assert excinfo.value.details["reason"] == "dependency_cycle"


def test_diamond_dependency_is_not_a_cycle() -> None:
    """菱形依赖（A→B、A→C、B→D、C→D）不是环。

    必须测：环检测极易写成"只要两端有共同祖先就拒绝"，
    那会把合法的菱形依赖误判成环 —— 而菱形依赖在内容生产里非常常见
    （两段内容都引用同一条事实知识）。
    """
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    a = _register(project_id=project_id, payload={"n": "da"})
    b = _register(project_id=project_id, payload={"n": "db"})
    c = _register(project_id=project_id, payload={"n": "dc"})
    d = _register(project_id=project_id, payload={"n": "dd"})

    assert _link(a.node_id, b.node_id, purpose="P1").created is True
    assert _link(a.node_id, c.node_id, purpose="P1").created is True
    assert _link(b.node_id, d.node_id, purpose="P1").created is True
    assert _link(c.node_id, d.node_id, purpose="P1").created is True


@pytest.mark.regression
def test_cycle_detection_terminates_on_cyclic_graph() -> None:
    """**环检测必须在"已经有环"的图上、有限时间内返回。**

    这是 `dependency_reaches` 用 `UNION` 做访问集合去重的存在理由，
    也是它**不设深度上限**的理由：本机实测（PG 16）19 个节点、36 条边构成的
    "菱形链 + 一条回边"，在旧的 `UNION ALL + max_depth=32` 写法下会展开
    2³² 量级的行 —— 查询不返回；去重后只有几十行、毫秒级返回。

    **这条测试替换掉了旧版**（旧版只断言 `1 <= MAX_DEPENDENCY_DEPTH <= 1024`，
    即"常量取值看起来合理"）。那种断言在实现爆炸时照样通过 ——
    它验证的是常量，不是行为。

    怎么把"挂死"变成"可断言的失败"：用 PG 的 `statement_timeout`。
    查询若爆炸，3 秒后由服务端取消并抛 `OperationalError`，失败信息直接
    指向这里；否则整个测试进程会被拖住，在 CI 上表现为"无输出超时"，
    排查成本高得多。

    图必须手工造 —— 应用层禁止成环，所以这里用**原生 SQL 直插边**
    （依赖表的不可变触发器只拦 UPDATE / DELETE，插入是允许的）。
    这条路径正好模拟了"绕过应用层写入的脏数据"，也正是环检测要面对的输入。
    """
    depth = 9
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    entity_id = uuid.uuid4()

    # 1 + 2*depth 个节点：同一逻辑实体的连续修订号；图里只看 version id。
    with session_scope(_make_settings()) as session:
        repo = VersionRepository(session)
        nodes = [
            register_version(
                repo,
                entity_type=ENTITY_TYPE_CONTENT,
                entity_id=entity_id,
                project_id=project_id,
                payload={"n": index},
            ).node_id
            for index in range(2 * depth + 1)
        ]

    def layer(index: int, offset: int) -> uuid.UUID:
        """第 `index` 层（1..depth）的第 `offset` 个节点。"""
        return nodes[1 + 2 * (index - 1) + offset]

    edges: list[tuple[uuid.UUID, uuid.UUID]] = [
        (nodes[0], layer(1, 0)),
        (nodes[0], layer(1, 1)),
    ]
    for index in range(1, depth):
        for a in (0, 1):
            for b in (0, 1):
                edges.append((layer(index, a), layer(index + 1, b)))
    edges.append((layer(depth, 0), layer(1, 0)))  # ← 回边：制造环

    with session_scope(_make_settings()) as session:
        for parent_id, dependency_id in edges:
            session.execute(
                text(
                    "INSERT INTO version_dependencies"
                    " (parent_version_id, dependency_version_id, purpose)"
                    " VALUES (:p, :d, 'FACT')"
                ),
                {"p": parent_id, "d": dependency_id},
            )

    with session_scope(_make_settings()) as session:
        # 语句级超时：把"挂死"变成一次明确的失败。
        session.execute(text("SET LOCAL statement_timeout = '3000ms'"))
        repo = VersionRepository(session)

        # 沿回边出发必然能回到回边起点 —— 图内有环，遍历必须终止。
        assert (
            repo.dependency_reaches(
                start_version_id=layer(depth, 0),
                target_version_id=layer(1, 0),
            )
            is True
        )
        # 入口层不被任何节点依赖 —— 必须走完全部可达节点才能确认不可达，
        # 因此这一条最能压出"路径展开"的代价。
        assert (
            repo.dependency_reaches(
                start_version_id=layer(depth, 0),
                target_version_id=nodes[0],
            )
            is False
        )


# --- 依赖查询 --------------------------------------------------------------


def test_list_dependencies_and_dependents() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    parent = _register(project_id=project_id, payload={"n": "lp"})
    up1 = _register(project_id=project_id, payload={"n": "lu1"})
    up2 = _register(project_id=project_id, payload={"n": "lu2"})
    _link(parent.node_id, up1.node_id, purpose="FACT")
    _link(parent.node_id, up2.node_id, purpose="STYLE")

    user = _current_user(owner)
    with session_scope(_make_settings()) as session:
        repo = VersionRepository(session)
        _node, deps = list_version_dependencies(
            repo, version_id=parent.node_id, user=user
        )
        assert {edge.dependency_version_id for edge in deps} == {
            up1.node_id,
            up2.node_id,
        }

        _node2, dependents = list_version_dependents(
            repo, version_id=up1.node_id, user=user
        )
        assert {edge.parent_version_id for edge in dependents} == {parent.node_id}


# --- 可见性（真库）--------------------------------------------------------


def test_non_member_cannot_read_version() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    node = _register(project_id=project_id)
    outsider = _current_user(_create_user())

    with session_scope(_make_settings()) as session:
        with pytest.raises(ApiError) as excinfo:
            get_version(VersionRepository(session), version_id=node.node_id, user=outsider)
    assert excinfo.value.code is ErrorCode.RESOURCE_NOT_FOUND


def test_member_can_read_version() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    node = _register(project_id=project_id)

    with session_scope(_make_settings()) as session:
        loaded = get_version(
            VersionRepository(session),
            version_id=node.node_id,
            user=_current_user(owner),
        )
    assert loaded.id == node.node_id


def test_admin_can_read_any_version() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    node = _register(project_id=project_id)
    admin = _current_user(_create_user(role=ROLE_ADMIN))

    with session_scope(_make_settings()) as session:
        loaded = get_version(
            VersionRepository(session), version_id=node.node_id, user=admin
        )
    assert loaded.id == node.node_id


def test_platform_version_is_invisible_to_operator() -> None:
    node = _register(entity_type=ENTITY_TYPE_PROMPT, payload={"tpl": "hidden"})
    operator = _current_user(_create_user())

    with session_scope(_make_settings()) as session:
        with pytest.raises(ApiError) as excinfo:
            get_version(
                VersionRepository(session), version_id=node.node_id, user=operator
            )
    assert excinfo.value.code is ErrorCode.RESOURCE_NOT_FOUND


def test_platform_version_is_visible_to_admin() -> None:
    node = _register(entity_type=ENTITY_TYPE_PROMPT, payload={"tpl": "visible"})
    admin = _current_user(_create_user(role=ROLE_ADMIN))

    with session_scope(_make_settings()) as session:
        loaded = get_version(
            VersionRepository(session), version_id=node.node_id, user=admin
        )
    assert loaded.id == node.node_id


def test_removed_collaborator_loses_version_access() -> None:
    """被移除的成员不能因为"历史成员记录还在"而继续看到版本。

    这条对应需求 §2.4「已停用成员不能继续操作」在版本层面的落地。
    """
    owner = _create_user()
    collaborator = _create_user()
    project_id = _seed_project(owner_id=owner, collaborators=(collaborator,))
    node = _register(project_id=project_id)

    with session_scope(_make_settings()) as session:
        ok = get_version(
            VersionRepository(session),
            version_id=node.node_id,
            user=_current_user(collaborator),
        )
        assert ok.id == node.node_id

    # 直接移除成员行（模拟成员被删）。
    with session_scope(_make_settings()) as session:
        member = session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == collaborator,
            )
        ).scalar_one()
        member.removed_at = datetime.now(timezone.utc)

    with session_scope(_make_settings()) as session:
        with pytest.raises(ApiError) as excinfo:
            get_version(
                VersionRepository(session),
                version_id=node.node_id,
                user=_current_user(collaborator),
            )
    assert excinfo.value.code is ErrorCode.RESOURCE_NOT_FOUND


# --- 版本历史分页 ---------------------------------------------------------


def test_history_returns_newest_first() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    entity_id = uuid.uuid4()
    for n in range(3):
        _register(entity_id=entity_id, project_id=project_id, payload={"n": n})

    with session_scope(_make_settings()) as session:
        page = list_version_history(
            VersionRepository(session),
            user=_current_user(owner),
            entity_type=ENTITY_TYPE_BRIEF,
            entity_id=entity_id,
            limit=10,
        )
    assert [node.revision_no for node in page.items] == [3, 2, 1]
    assert page.has_more is False
    assert page.next_anchor is None


def test_history_pagination_is_complete_and_unique() -> None:
    """5 条、每页 2 条，翻完必须不重不漏。

    游标分页的经典失败是"漏项"（少了一版）或"重复"（同一版出现两次），
    而两者都不会报错 —— 只能靠这种遍历断言发现。
    """
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    entity_id = uuid.uuid4()
    for n in range(5):
        _register(entity_id=entity_id, project_id=project_id, payload={"n": n})

    user = _current_user(owner)
    seen: list[int] = []
    anchor = None
    for _ in range(10):  # 上界防死循环
        with session_scope(_make_settings()) as session:
            page = list_version_history(
                VersionRepository(session),
                user=user,
                entity_type=ENTITY_TYPE_BRIEF,
                entity_id=entity_id,
                limit=2,
                after=anchor,
            )
        seen.extend(node.revision_no for node in page.items)
        if not page.has_more:
            break
        anchor = page.next_anchor

    assert seen == [5, 4, 3, 2, 1]


def test_history_of_unknown_entity_is_empty_page() -> None:
    owner = _create_user()
    with session_scope(_make_settings()) as session:
        page = list_version_history(
            VersionRepository(session),
            user=_current_user(owner),
            entity_type=ENTITY_TYPE_BRIEF,
            entity_id=uuid.uuid4(),
            limit=5,
        )
    assert page.items == ()
    assert page.has_more is False


def test_history_rejects_unknown_entity_type() -> None:
    owner = _create_user()
    with session_scope(_make_settings()) as session:
        with pytest.raises(ApiError) as excinfo:
            list_version_history(
                VersionRepository(session),
                user=_current_user(owner),
                entity_type="NOPE",
                entity_id=uuid.uuid4(),
                limit=5,
            )
    assert excinfo.value.details["reason"] == "entity_type_unknown"


def test_history_of_invisible_project_is_404() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    entity_id = uuid.uuid4()
    _register(entity_id=entity_id, project_id=project_id, payload={"n": 1})

    outsider = _current_user(_create_user())
    with session_scope(_make_settings()) as session:
        with pytest.raises(ApiError) as excinfo:
            list_version_history(
                VersionRepository(session),
                user=outsider,
                entity_type=ENTITY_TYPE_BRIEF,
                entity_id=entity_id,
                limit=5,
            )
    assert excinfo.value.code is ErrorCode.RESOURCE_NOT_FOUND


# --- 草稿升格（含 ETag 语义切换）------------------------------------------


def _promote(
    base_version_id: uuid.UUID,
    *,
    payload: dict[str, object],
    created_by: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
):
    with session_scope(_make_settings()) as session:
        return promote_draft(
            VersionRepository(session),
            base_version_id=base_version_id,
            payload=payload,
            created_by=created_by,
            project_id=project_id,
        )


def test_promote_draft_creates_next_revision_with_immutable_etag() -> None:
    """升格后 ETag 变为**不可变**形态（`h` 前缀），且与草稿的版本号形态不同。

    这是"草稿 ETag → 版本 ETag"的语义切换点：
      · 草稿：`"<draftId>:<lockVersion>"`（每次保存都变）
      · 版本：`"<nodeId>:h<hash16>"`（此后永不变）
    """
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    entity_id = uuid.uuid4()
    base = _register(entity_id=entity_id, project_id=project_id, payload={"n": 1})

    promoted = _promote(base.node_id, payload={"n": 2}, created_by=owner)

    assert promoted.revision_no == 2
    assert promoted.entity_id == base.entity_id
    assert promoted.project_id == base.project_id
    assert promoted.etag.startswith(f'"{promoted.node_id}:h')
    assert parse_etag(promoted.etag) is None


def test_promote_draft_returns_stable_etag_across_calls() -> None:
    """同一版本的 ETag 两次读取必须一致 —— "不可变"的可观察证据。"""
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    base = _register(project_id=project_id, payload={"n": "s1"})
    promoted = _promote(base.node_id, payload={"n": "s2"})

    with session_scope(_make_settings()) as session:
        row = session.get(VersionNode, promoted.node_id)
        assert row is not None
        assert row.etag == promoted.etag


def test_promote_draft_rejects_unchanged_payload() -> None:
    """空保存不得生成新版本。

    否则版本历史会被"内容完全相同的新版本"灌满，
    下游基于"有没有新版本"的判断（如"结果已过期"）随之失真。
    """
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    base = _register(project_id=project_id, payload={"n": "same"})

    with pytest.raises(ApiError) as excinfo:
        _promote(base.node_id, payload={"n": "same"})
    assert excinfo.value.details["reason"] == "draft_unchanged"


def test_promote_draft_rejects_cross_project_target() -> None:
    owner = _create_user()
    project_a = _seed_project(owner_id=owner)
    project_b = _seed_project(owner_id=owner)
    base = _register(project_id=project_a, payload={"n": "cp"})

    with pytest.raises(ApiError) as excinfo:
        _promote(base.node_id, payload={"n": "cp2"}, project_id=project_b)
    assert excinfo.value.details["reason"] == "cross_project_reference"


def test_promote_draft_rejects_missing_base_version() -> None:
    with pytest.raises(ApiError) as excinfo:
        _promote(uuid.uuid4(), payload={"n": 1})
    assert excinfo.value.details["reason"] == "version_not_found"


def test_promote_draft_keeps_entity_type_and_id() -> None:
    owner = _create_user()
    entity_id = uuid.uuid4()
    base = _register(
        entity_type=ENTITY_TYPE_CONTENT,
        entity_id=entity_id,
        project_id=None,
        payload={"n": "keep"},
    )
    promoted = _promote(base.node_id, payload={"n": "keep2"})

    assert promoted.entity_type == ENTITY_TYPE_CONTENT
    assert promoted.entity_id == entity_id


def test_promote_draft_sequential_revisions_are_monotonic() -> None:
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    base = _register(project_id=project_id, payload={"n": 0})

    revisions = []
    current = base
    for n in range(1, 4):
        current = _promote(current.node_id, payload={"n": n})
        revisions.append(current.revision_no)

    assert revisions == [2, 3, 4]


def test_promote_draft_can_reference_the_base_version() -> None:
    """升格后应能把"基于哪一版"登记成依赖边 —— 版本与引用在同一套机制里闭环。"""
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    base = _register(project_id=project_id, payload={"n": "chain-1"})
    promoted = _promote(base.node_id, payload={"n": "chain-2"})

    link = _link(promoted.node_id, base.node_id, purpose="BASE_VERSION")
    assert link.created is True

    with session_scope(_make_settings()) as session:
        _node, deps = list_version_dependencies(
            VersionRepository(session),
            version_id=promoted.node_id,
            user=_current_user(owner),
        )
    assert [edge.dependency_version_id for edge in deps] == [base.node_id]


# --- 契约与路由：本包不新增端点 -------------------------------------------


def test_no_version_endpoints_are_exposed(app: FastAPI) -> None:
    """WP11 **刻意不新增 HTTP 端点**。

    协议 03 的端点表里没有任何 `/versions/*` 路径。各领域模块自己的
    版本历史端点（`GET /contents/{id}/versions` 等）才是真实入口。
    这条测试是一道"护栏"：将来有人顺手加一个通用版本端点时，
    会先看到这里，并被逼着去回答"协议里到底有没有这个入口"。
    """
    paths = {
        route.path
        for route in app.routes
        if hasattr(route, "methods") and isinstance(route.path, str)
    }
    assert not any(path.startswith("/api/v1/versions") for path in paths)


def test_importing_versions_module_keeps_app_startup_clean(
    client: TestClient,
) -> None:
    """导入 versions 模块（含模块级注册的不可变守卫）不能影响应用启动。

    守卫是在**模块级**注册的 —— 若注册方式有误（例如用了一个会立即求值的
    钩子，或把守卫挂到了 `before_flush` 上），整个应用会在 import 期或
    每条语句上崩掉。这里用一次真实请求确认健康检查仍可用。
    """
    response = client.get("/api/v1/health")
    assert response.status_code == 200


@pytest.mark.cross_wp
def test_knowledge_entity_type_is_usable() -> None:
    """抽查一个尚未有消费者的类型，确认受控枚举不会拦住合法用法。"""
    node = _register(entity_type=ENTITY_TYPE_KNOWLEDGE, payload={"kb": "x"})
    assert node.entity_type == "KNOWLEDGE"
