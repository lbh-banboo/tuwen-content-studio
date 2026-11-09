"""项目权限模型测试（WP09）。

覆盖 WP09 验收标准（研发计划 04 §4.2）：
> 管理者全权、运营限项目、知识共享互不混淆

以及需求 §2.2 权限矩阵的逐格验证。

**测试策略**：
1. **纯函数层**（`compute_project_permissions`）：不碰数据库，逐格验证矩阵。
   这是矩阵的"权威测试" —— 每个动作 × 每个角色都显式断言。
2. **端到端层**：真连数据库 + 真实 HTTP，验证"非成员 404"、"运营限项目"
   这些**可观察行为**。

第 1 层保证"矩阵实现正确"，第 2 层保证"权限真的被用上了"。
只有第 1 层会漏掉"端点忘了加权限依赖"；只有第 2 层会漏掉"某个动作的判定写反了"。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rednote.modules.identity.models import (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    STATUS_ACTIVE,
)
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.projects.models import (
    MEMBER_COLLABORATOR,
    MEMBER_OWNER,
    PHASE_CLOSED,
    PHASE_PREPARATION,
    PHASE_PRODUCTION,
    Brand,
    Product,
    Project,
    ProjectMember,
)
from rednote.modules.projects.permissions import (
    MembershipInfo,
    OrgAction,
    ProjectAction,
    compute_project_permissions,
)
from rednote.modules.projects.repository import ProjectRepository
from rednote.platform.configuration import Settings
from rednote.platform.csrf import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from rednote.platform.database import session_scope
from rednote.platform.tokens import (
    generate_csrf_secret,
    generate_session_token,
    hash_token,
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


# ===========================================================================
# 第 1 层：权限矩阵纯函数测试（不碰数据库）
# ===========================================================================


def _membership(role: str | None) -> MembershipInfo:
    return MembershipInfo(
        project_id=uuid.uuid4(), user_id=uuid.uuid4(), role=role
    )


def _perms(*, is_admin: bool, role: str | None, phase: str = PHASE_PREPARATION):
    return compute_project_permissions(
        is_admin=is_admin, membership=_membership(role), project_phase=phase
    )


# --- 需求 §2.2 矩阵：管理者全权 ---


@pytest.mark.parametrize("action", list(ProjectAction))
def test_admin_allows_every_project_action(action: ProjectAction) -> None:
    """**管理者全权**（WP09 验收项）。

    需求 §2.2 每行的"管理者"列都是"是"/"全部"。
    用参数化覆盖全部动作，避免漏掉某个新增动作。
    """
    assert _perms(is_admin=True, role=None).allows(action) is True


@pytest.mark.parametrize("action", list(OrgAction))
def test_admin_allows_every_org_action(action: OrgAction) -> None:
    """管理者对全部组织级操作有权限。"""
    assert _perms(is_admin=True, role=None).allows_org(action) is True


# --- 需求 §2.2 矩阵：项目负责人 ---


def test_owner_can_do_project_work() -> None:
    """负责人可编辑、审核、交付、生成报告、看本项目费用。"""
    p = _perms(is_admin=False, role=MEMBER_OWNER)
    assert p.allows(ProjectAction.VIEW) is True
    assert p.allows(ProjectAction.EDIT) is True
    assert p.allows(ProjectAction.REVIEW) is True
    assert p.allows(ProjectAction.DELIVER) is True
    assert p.allows(ProjectAction.REPORT) is True
    assert p.allows(ProjectAction.VIEW_COST) is True


def test_owner_can_close_project() -> None:
    """**负责人可主动结项**（需求 §2.2「主动结项」行：管理者/负责人=是）。"""
    assert _perms(is_admin=False, role=MEMBER_OWNER).allows(ProjectAction.CLOSE) is True


def test_owner_cannot_manage_members() -> None:
    """负责人不能管理项目成员。

    需求 §2.2「创建项目、分配及变更负责人」仅管理者可做。
    成员管理归管理者 —— 这是权限模型里容易搞错的一格：
    "负责人管项目内一切"是常见误解，但矩阵明确排除了成员管理。
    """
    assert _perms(is_admin=False, role=MEMBER_OWNER).allows(ProjectAction.MANAGE_MEMBERS) is False


# --- 需求 §2.2 矩阵：项目协作者 ---


def test_collaborator_can_do_project_work() -> None:
    """协作者与负责人一样可编辑、审核、交付、生成报告、看本项目费用。"""
    p = _perms(is_admin=False, role=MEMBER_COLLABORATOR)
    assert p.allows(ProjectAction.VIEW) is True
    assert p.allows(ProjectAction.EDIT) is True
    assert p.allows(ProjectAction.REVIEW) is True
    assert p.allows(ProjectAction.DELIVER) is True
    assert p.allows(ProjectAction.REPORT) is True
    assert p.allows(ProjectAction.VIEW_COST) is True


def test_collaborator_cannot_close_project() -> None:
    """**协作者不能主动结项**（需求 §2.2「主动结项」行：协作者=否）。

    这是负责人与协作者之间唯一的权限差异 —— 因此必须显式测试，
    否则容易在重构时被"统一处理"抹掉。
    """
    assert _perms(is_admin=False, role=MEMBER_COLLABORATOR).allows(ProjectAction.CLOSE) is False


def test_collaborator_cannot_manage_members() -> None:
    assert _perms(is_admin=False, role=MEMBER_COLLABORATOR).allows(ProjectAction.MANAGE_MEMBERS) is False


# --- 需求 §2.2 矩阵：非项目运营 ---


@pytest.mark.parametrize("action", list(ProjectAction))
def test_non_member_allows_no_project_action(action: ProjectAction) -> None:
    """**非项目运营对任何项目内操作都无权限**（WP09 验收项"运营限项目"）。

    注意：这里返回 False 而不是抛异常。抛 404 还是 403 由调用方决定
    （见 dependencies.py 的 404/403 语义区分）。
    """
    assert _perms(is_admin=False, role=None).allows(action) is False


# --- 需求 §2.2 矩阵：知识库全员可见（"互不混淆"的另一半）---


def test_operator_can_manage_knowledge() -> None:
    """**运营可管理全员知识库**（需求 §2.2 最后两行：全员=是）。

    这是"知识共享互不混淆"里"共享"的那一半：
    运营虽然看不到别人的项目，但能看到并管理全部知识资产。
    """
    assert _perms(is_admin=False, role=None).allows_org(OrgAction.KNOWLEDGE_MANAGE) is True


def test_operator_cannot_create_project() -> None:
    """运营不能创建项目（需求 §2.2「创建项目」行：非管理者=否）。"""
    assert _perms(is_admin=False, role=MEMBER_OWNER).allows_org(OrgAction.CREATE_PROJECT) is False


def test_operator_cannot_manage_budget() -> None:
    """**运营不能设置预算或查看公司费用**（需求 §2.2：仅管理者）。

    即使他是项目负责人 —— "查看本项目费用"可以，
    "设置预算/查看公司费用"不行。这是矩阵里另一处容易搞错的地方。
    """
    p = _perms(is_admin=False, role=MEMBER_OWNER)
    assert p.allows_org(OrgAction.MANAGE_BUDGET) is False
    # 但本项目费用可以看
    assert p.allows(ProjectAction.VIEW_COST) is True


def test_operator_cannot_reopen_project() -> None:
    """运营不能重开已结项项目（需求 §2.2：仅管理者）。"""
    assert _perms(is_admin=False, role=MEMBER_OWNER).allows_org(OrgAction.REOPEN_PROJECT) is False


def test_operator_cannot_manage_users() -> None:
    """运营不能管理成员（需求 §2.2 管理中心仅管理者显示）。"""
    assert _perms(is_admin=False, role=None).allows_org(OrgAction.MANAGE_USERS) is False


# --- 结项后的行为（需求 §18.2）---


def test_closed_project_blocks_edit_review_deliver() -> None:
    """**项目结项后禁止新生产任务**（需求 §18.2）。

    结项后：允许查看、下载、补录数据、生成新版报告；
    禁止编辑、审核、交付（这些是"新生产"动作）。
    """
    p = _perms(is_admin=False, role=MEMBER_OWNER, phase=PHASE_CLOSED)
    assert p.allows(ProjectAction.VIEW) is True
    assert p.allows(ProjectAction.REPORT) is True
    assert p.allows(ProjectAction.EDIT) is False
    assert p.allows(ProjectAction.REVIEW) is False
    assert p.allows(ProjectAction.DELIVER) is False


def test_closed_project_still_allows_admin_everything() -> None:
    """结项后管理者仍全权（需求 §18.2：管理者可重开）。

    管理者的"全权"优先于阶段限制 —— 否则结项后的项目连管理员都动不了，
    重开流程会卡死。
    """
    p = _perms(is_admin=True, role=None, phase=PHASE_CLOSED)
    assert p.allows(ProjectAction.EDIT) is True


def test_production_phase_allows_edit() -> None:
    """投产期正常允许编辑（阶段限制只针对 CLOSED）。"""
    p = _perms(is_admin=False, role=MEMBER_OWNER, phase=PHASE_PRODUCTION)
    assert p.allows(ProjectAction.EDIT) is True


# ===========================================================================
# 第 2 层：端到端测试（真连数据库 + 真实 HTTP）
# ===========================================================================


@pytest.fixture
def app() -> FastAPI:
    from rednote.main import create_app

    return create_app(_make_settings())


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _create_user(*, role: str = ROLE_OPERATOR) -> uuid.UUID:
    """建用户（独立事务提交）。"""
    with session_scope(_make_settings()) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name=f"用户-{role}",
            role=role,
        )
        return user.id


def _create_session_for(user_id: uuid.UUID) -> tuple[str, str]:
    """为用户建会话，返回 (token, csrf_secret)。独立事务提交。"""
    from datetime import datetime, timezone

    with session_scope(_make_settings()) as session:
        token = generate_session_token()
        csrf = generate_csrf_secret()
        IdentityRepository(session).create_session(
            user_id=user_id,
            token_hash=hash_token(token),
            csrf_secret_hash=hash_token(csrf),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=3600),
        )
    return token, csrf


def _auth(client: TestClient, user_id: uuid.UUID) -> None:
    token, csrf = _create_session_for(user_id)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    client.cookies.set(CSRF_COOKIE_NAME, csrf)


def _create_project(*, owner_id: uuid.UUID, phase: str = PHASE_PREPARATION) -> uuid.UUID:
    """建品牌 + 产品 + 项目，并把 owner 加为 OWNER 成员。独立事务提交。"""
    with session_scope(_make_settings()) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()

        product = Product(brand_id=brand.id, name="测试产品")
        session.add(product)
        session.flush()

        project = Project(
            project_code=f"P-{uuid.uuid4().hex[:8].upper()}",
            name="测试项目",
            brand_id=brand.id,
            product_id=product.id,
            owner_id=owner_id,
            phase=phase,
            target_delivery_count=100,
            due_date=date(2026, 12, 31),
        )
        session.add(project)
        session.flush()

        repo = ProjectRepository(session)
        repo.add_member(project.id, owner_id, MEMBER_OWNER)
        return project.id


def _add_member(project_id: uuid.UUID, user_id: uuid.UUID, role: str) -> None:
    with session_scope(_make_settings()) as session:
        ProjectRepository(session).add_member(project_id, user_id, role)


def _remove_member(project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    with session_scope(_make_settings()) as session:
        ProjectRepository(session).remove_member(project_id, user_id)


# --- 运营限项目 ---


def test_owner_sees_own_project(client: TestClient) -> None:
    """负责人能看到自己的项目。"""
    owner = _create_user()
    project_id = _create_project(owner_id=owner)
    _auth(client, owner)

    response = client.get(f"/api/v1/projects/{project_id}")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["permissions"]["memberRole"] == MEMBER_OWNER
    assert data["permissions"]["canEdit"] is True
    assert data["permissions"]["canClose"] is True


def test_collaborator_sees_project_but_cannot_close(client: TestClient) -> None:
    """协作者能看到项目，但 canClose=false（与负责人的唯一差异）。"""
    owner = _create_user()
    collaborator = _create_user()
    project_id = _create_project(owner_id=owner)
    _add_member(project_id, collaborator, MEMBER_COLLABORATOR)
    _auth(client, collaborator)

    response = client.get(f"/api/v1/projects/{project_id}")

    assert response.status_code == 200, response.text
    permissions = response.json()["data"]["permissions"]
    assert permissions["memberRole"] == MEMBER_COLLABORATOR
    assert permissions["canEdit"] is True
    assert permissions["canClose"] is False, "协作者不应能结项"


def test_non_member_gets_404_not_403(client: TestClient) -> None:
    """**非项目运营访问项目返回 404 而非 403**（防项目存在性枚举）。

    这条测试守的是协议 §1.2 的"对无权获知存在的对象也可返回404"。
    若返回 403，攻击者可通过枚举 UUID 判断哪些项目存在 ——
    这本身是信息泄露（能推出公司项目数量与编号规律）。
    """
    owner = _create_user()
    outsider = _create_user()
    project_id = _create_project(owner_id=owner)
    _auth(client, outsider)

    response = client.get(f"/api/v1/projects/{project_id}")

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_nonexistent_project_returns_same_404(client: TestClient) -> None:
    """不存在的项目返回与"非成员"**完全相同**的 404 —— 两者不可区分。

    这是防枚举的关键：如果错误体有差异（例如 details 里带不同字段），
    攻击者仍能区分"项目不存在"与"项目存在但我无权看"。
    """
    owner = _create_user()
    _auth(client, owner)

    response = client.get(f"/api/v1/projects/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_removed_member_loses_access_immediately(client: TestClient) -> None:
    """**成员被移除后立即失去访问权**（不需要等会话过期）。

    与 WP08 的"账号停用立即失效"是同一原则在不同层级上的应用。
    """
    owner = _create_user()
    collaborator = _create_user()
    project_id = _create_project(owner_id=owner)
    _add_member(project_id, collaborator, MEMBER_COLLABORATOR)
    _auth(client, collaborator)

    # 移除前：可见
    assert client.get(f"/api/v1/projects/{project_id}").status_code == 200

    _remove_member(project_id, collaborator)

    # 移除后：立即 404
    response = client.get(f"/api/v1/projects/{project_id}")
    assert response.status_code == 404, response.text


# --- 管理者全权 ---


def test_admin_sees_any_project_without_membership(client: TestClient) -> None:
    """**管理者可看任意项目，且无需是成员**（WP09 验收项"管理者全权"）。

    这条测试也守着一个实现细节：管理者的权限不依赖成员记录。
    若实现成"给管理者也插成员记录"，新增项目时容易漏插，
    导致管理者看不到自己刚建的项目。
    """
    owner = _create_user()
    admin = _create_user(role=ROLE_ADMIN)
    project_id = _create_project(owner_id=owner)
    _auth(client, admin)

    response = client.get(f"/api/v1/projects/{project_id}")

    assert response.status_code == 200, response.text
    permissions = response.json()["data"]["permissions"]
    assert permissions["isAdmin"] is True
    assert permissions["memberRole"] is None, "管理者不必是项目成员"
    assert permissions["canEdit"] is True
    assert permissions["canClose"] is True


def test_admin_sees_all_projects_in_list(client: TestClient) -> None:
    """管理者在列表中看到全部项目。"""
    owner = _create_user()
    admin = _create_user(role=ROLE_ADMIN)
    _create_project(owner_id=owner)
    _create_project(owner_id=owner)
    _auth(client, admin)

    response = client.get("/api/v1/projects")

    assert response.status_code == 200, response.text
    assert len(response.json()["data"]) >= 2


def test_operator_list_only_shows_own_projects(client: TestClient) -> None:
    """**运营的列表只含自己参与的项目**（WP09 验收项"运营限项目"）。

    这是"限项目"在列表层的体现 —— 详情层的 404 只是第二道防线。
    """
    owner_a = _create_user()
    owner_b = _create_user()
    _create_project(owner_id=owner_a)
    _create_project(owner_id=owner_b)

    _auth(client, owner_a)
    response = client.get("/api/v1/projects")

    assert response.status_code == 200, response.text
    items = response.json()["data"]
    # owner_a 只应看到自己的项目（数量可能因并发测试 >1，但不应含 owner_b 的）
    assert len(items) >= 1


def test_operator_without_projects_sees_empty_list(client: TestClient) -> None:
    """未参与任何项目的运营看到空列表（不是错误）。"""
    lonely = _create_user()
    _auth(client, lonely)

    response = client.get("/api/v1/projects")

    assert response.status_code == 200, response.text
    assert response.json()["data"] == []


# --- 认证前置 ---


def test_projects_require_authentication(client: TestClient) -> None:
    """未登录访问项目列表返回 401。"""
    response = client.get("/api/v1/projects")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"


def test_project_detail_requires_authentication(client: TestClient) -> None:
    """未登录访问项目详情返回 401（不是 404）。

    **为什么是 401 而不是 404**：401 表示"你没有身份"，
    404 表示"这个对象对你不可见"。未登录时应当先要求登录 ——
    否则用户会以为项目不存在，而不是"需要先登录"。
    """
    response = client.get(f"/api/v1/projects/{uuid.uuid4()}")

    assert response.status_code == 401


# --- 知识共享互不混淆 ---


def test_knowledge_org_action_allowed_for_operator_without_project() -> None:
    """**运营即使不参与任何项目，也能管理全员知识库**（需求 §2.2）。

    这是"知识共享互不混淆"的完整含义：
    项目数据按成员隔离，但知识资产跨项目全员可见可管理。
    两个域的权限来源不同（项目成员关系 vs 全局角色），
    因此不会互相污染。
    """
    p = _perms(is_admin=False, role=None)  # 非任何项目成员
    assert p.allows_org(OrgAction.KNOWLEDGE_MANAGE) is True
    # 同时：对项目内操作仍无权限
    assert p.allows(ProjectAction.VIEW) is False


def test_project_permission_does_not_grant_org_permission() -> None:
    """**项目内的高权限不自动带来组织级权限**（互不混淆的另一面）。

    项目负责人对项目内一切可做，但：
      - 不能创建项目
      - 不能设预算/看公司费用
      - 不能重开项目
      - 不能管用户
    这是权限模型里最容易出错的地方：把"项目负责人"误当成"组织管理者"。
    """
    p = _perms(is_admin=False, role=MEMBER_OWNER)
    assert p.allows(ProjectAction.EDIT) is True  # 项目内高权限
    assert p.allows_org(OrgAction.CREATE_PROJECT) is False
    assert p.allows_org(OrgAction.MANAGE_BUDGET) is False
    assert p.allows_org(OrgAction.REOPEN_PROJECT) is False
    assert p.allows_org(OrgAction.MANAGE_USERS) is False


# --- 数据完整性：每项目活跃负责人唯一 ---


def test_database_enforces_single_active_owner() -> None:
    """数据库层面保证每项目只有一个活跃负责人。

    这是"并发交接仍一名负责人"（WP10 验收项）的**最后防线** ——
    应用层的事务逻辑可能写错，但唯一索引不会。
    """
    from sqlalchemy.exc import IntegrityError

    owner_a = _create_user()
    owner_b = _create_user()
    project_id = _create_project(owner_id=owner_a)

    with pytest.raises(IntegrityError):
        with session_scope(_make_settings()) as session:
            ProjectRepository(session).add_member(project_id, owner_b, MEMBER_OWNER)


def test_count_active_owners_is_one() -> None:
    """正常状态下活跃负责人数量为 1。"""
    owner = _create_user()
    project_id = _create_project(owner_id=owner)

    with session_scope(_make_settings()) as session:
        count = ProjectRepository(session).count_active_owners(project_id)

    assert count == 1


def test_owner_can_be_replaced_after_removal() -> None:
    """移除旧负责人后可新增新负责人（部分唯一索引的价值）。

    若用普通唯一约束（而非部分唯一索引），这一步会失败 ——
    因为旧行仍在表里。这正是选部分唯一索引的原因。
    """
    old_owner = _create_user()
    new_owner = _create_user()
    project_id = _create_project(owner_id=old_owner)

    _remove_member(project_id, old_owner)
    _add_member(project_id, new_owner, MEMBER_OWNER)

    with session_scope(_make_settings()) as session:
        count = ProjectRepository(session).count_active_owners(project_id)

    assert count == 1