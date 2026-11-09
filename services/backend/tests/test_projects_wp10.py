"""WP10 测试：字典、项目创建/列表/筛选、负责人交接。

覆盖 WP10 验收标准（研发计划 04 §4.2）：
> 并发交接仍一名负责人；待办和历史正确

以及协议 §3 第 95~101 行规定的七个端点的契约行为。

**测试策略（沿用 WP09 的两层结构）**：
1. **纯函数层**：编号生成（`codes`）、ETag/If-Match（`concurrency`）、
   编号冲突判定（`service`）。不碰数据库 —— 这些是"算法正确性"的权威测试。
2. **端到端层**：真连数据库 + 真实 HTTP。验证"并发交接仍一名负责人"、
   "待办转交"、"游标翻页不重不漏"这些**可观察行为**。

**为什么本文件重复定义了装配 helper（而不是抽到 conftest）**：
测试文件自包含的价值高于 DRY —— 一个测试文件应该能被单独读懂、
单独运行，不依赖隐藏的共享状态。WP09 的测试文件也是同样的自包含风格。

**关于写请求的两个必备请求头**（WP08 的 CSRF 双防线）：
`X-CSRF-Token`（与会话库中的摘要匹配）+ `Origin: http://testserver`
（TestClient 的 base_url）。缺任一个都是 403，这是设计如此。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request as StarletteRequest

from rednote.modules.identity.models import (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    STATUS_ACTIVE,
)
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.projects.models import (
    DICT_ACTIVE,
    DICT_ARCHIVED,
    MEMBER_COLLABORATOR,
    MEMBER_OWNER,
    PHASE_PREPARATION,
    Brand,
    Product,
    Project,
    ProjectMember,
)
from rednote.modules.projects.permissions import OrgAction
from rednote.modules.projects.repository import ProjectRepository
from rednote.modules.projects.service import _is_project_code_conflict
from rednote.modules.work_items.models import WORK_ITEM_OPEN, WorkItem
from rednote.modules.work_items.repository import WorkItemRepository
from rednote.platform.codes import (
    CODE_ALPHABET,
    generate_display_code,
    random_code_suffix,
)
from rednote.platform.concurrency import (
    IF_MATCH_HEADER,
    build_etag,
    parse_etag,
    require_if_match,
)
from rednote.platform.configuration import Settings
from rednote.platform.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
)
from rednote.platform.database import session_scope
from rednote.platform.errors import ApiError, ErrorCode
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
TEST_ORIGIN = "http://testserver"


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
# 第 1 层：纯函数测试（不碰数据库）
# ===========================================================================

# --- 编号生成（platform/codes.py）------------------------------------------


def test_display_code_has_expected_shape() -> None:
    """编号形状：`PRJ-YYYYMM-XXXXXX`。"""
    code = generate_display_code("PRJ", today=date(2026, 9, 21), suffix="7K3M2Q")
    assert code == "PRJ-202609-7K3M2Q"


def test_display_code_prefix_is_upper_cased() -> None:
    """前缀大小写归一：调用方写 "prj" 也应得到 "PRJ-..."。"""
    code = generate_display_code("prj", today=date(2026, 1, 5), suffix="ABC123")
    assert code == "PRJ-202601-ABC123"


def test_display_code_random_suffix_only_uses_safe_alphabet() -> None:
    """随机后缀只含 Crockford Base32 字符（不含 I/L/O/U）。

    这条断言的价值：编号会进文件名与邮件，一旦字母表混入 `I`/`O`，
    手抄就会与 `1`/`0` 混淆 —— 那是真实发生过的运维事故类型。
    """
    for _ in range(50):
        suffix = random_code_suffix()
        assert len(suffix) == 6
        assert all(char in CODE_ALPHABET for char in suffix)
        assert not set(suffix) & set("ILOU")


def test_display_code_rejects_bad_prefix() -> None:
    with pytest.raises(ValueError):
        generate_display_code("PRJ/../ETC", suffix="ABC123")


def test_display_code_rejects_empty_prefix() -> None:
    with pytest.raises(ValueError):
        generate_display_code("   ", suffix="ABC123")


def test_display_code_rejects_bad_suffix() -> None:
    with pytest.raises(ValueError):
        generate_display_code("PRJ", suffix="ABC-12")


def test_random_code_suffix_rejects_non_positive_length() -> None:
    with pytest.raises(ValueError):
        random_code_suffix(0)


def test_display_code_prefix_validation_does_not_require_alnum_only() -> None:
    """含连字符的前缀是合法的（例如 "PRJ-CN"）。"""
    code = generate_display_code("PRJ-CN", today=date(2026, 3, 1), suffix="ZZZZZZ")
    assert code == "PRJ-CN-202603-ZZZZZZ"


@pytest.mark.parametrize("prefix", ["-", "--", "---"])
def test_display_code_rejects_hyphen_only_prefix(prefix: str) -> None:
    """全连字符的前缀必须被拒 —— 否则编号会以分隔符开头（`--202609-...`）。

    这条守的是"逐字符白名单"与"前缀必须有内容"两件事的边界：
    只做逐字符检查时 `-` 是合法字符，`all(...)` 通过、`not normalized` 也通过，
    校验于是形同虚设。
    """
    with pytest.raises(ValueError):
        generate_display_code(prefix, suffix="ABC123")


@pytest.mark.parametrize("prefix", ["项目", "ＰＲＪ", "PRJ·CN", "PRJ CN"])
def test_display_code_rejects_non_ascii_or_space_prefix(prefix: str) -> None:
    """编号会进目录名与 URL，前缀必须限 ASCII。

    `str.isalnum()` 对中文与全角字符返回 True，单靠它无法保证
    "编号只含大写字母与数字"这个对外承诺。空格同理 ——
    带空格的目录名在脚本里处处要加引号，是可用性事故的常见源头。
    """
    with pytest.raises(ValueError):
        generate_display_code(prefix, suffix="ABC123")


# --- ETag 与 If-Match（platform/concurrency.py）-----------------------------


def test_build_etag_is_quoted() -> None:
    """ETag 必须带双引号 —— 不带引号不是合法 entity-tag，客户端会忽略它。"""
    etag = build_etag(uuid.UUID("11111111-1111-1111-1111-111111111111"), 18)
    assert etag.startswith('"') and etag.endswith('"')
    assert etag == '"11111111-1111-1111-1111-111111111111:18"'


def test_parse_etag_round_trip() -> None:
    resource_id = uuid.uuid4()
    assert parse_etag(build_etag(resource_id, 7)) == (str(resource_id), 7)


@pytest.mark.parametrize(
    "raw",
    [
        "no-quotes:1",          # 缺引号
        'W/"weak:1"',           # 弱 ETag（本系统从不签发）
        '"no-version"',         # 缺版本段
        '":1"',                 # 缺资源 ID
        '"abc:not-a-number"',   # 版本非数字
        '"abc:-1"',             # 版本非正整数
        '"：1"',                # 全角冒号
        "",
        '""',
    ],
)
def test_parse_etag_rejects_malformed(raw: str) -> None:
    assert parse_etag(raw) is None


def _request_with_headers(headers: dict[str, str]) -> StarletteRequest:
    scope = {
        "type": "http",
        "method": "PATCH",
        "path": "/api/v1/projects/x",
        "headers": [(k.lower().encode("ascii"), v.encode("utf-8")) for k, v in headers.items()],
    }
    return StarletteRequest(scope)


RESOURCE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _call_require_if_match(headers: dict[str, str], current_version: int = 3) -> None:
    require_if_match(
        _request_with_headers(headers),
        resource_id=RESOURCE_ID,
        current_version=current_version,
        resource_label="项目",
    )


def test_if_match_passes_with_matching_etag() -> None:
    _call_require_if_match({IF_MATCH_HEADER: build_etag(RESOURCE_ID, 3)})


def test_if_match_passes_when_any_candidate_matches() -> None:
    """RFC 允许逗号分隔的多值，任一匹配即通过。"""
    _call_require_if_match(
        {IF_MATCH_HEADER: f'{build_etag(uuid.uuid4(), 9)}, {build_etag(RESOURCE_ID, 3)}'}
    )


def test_if_match_missing_raises_precondition_required() -> None:
    """**缺失 If-Match 是 428 而不是 412** —— 两者对客户端的含义不同。"""
    with pytest.raises(ApiError) as excinfo:
        _call_require_if_match({})
    assert excinfo.value.code is ErrorCode.PRECONDITION_REQUIRED
    assert excinfo.value.details["reason"] == "if_match_missing"


def test_if_match_blank_value_raises_precondition_required() -> None:
    with pytest.raises(ApiError) as excinfo:
        _call_require_if_match({IF_MATCH_HEADER: "   "})
    assert excinfo.value.code is ErrorCode.PRECONDITION_REQUIRED
    assert excinfo.value.details["reason"] == "if_match_missing"


def test_if_match_wildcard_is_rejected() -> None:
    """**`*` 必须被拒绝**：接受它就等于让客户端一行字绕过全部并发检查。"""
    with pytest.raises(ApiError) as excinfo:
        _call_require_if_match({IF_MATCH_HEADER: "*"})
    assert excinfo.value.code is ErrorCode.PRECONDITION_REQUIRED
    assert excinfo.value.details["reason"] == "if_match_wildcard_unsupported"


def test_if_match_malformed_raises_bad_request() -> None:
    """格式错是 400，不是 412 —— 版本冲突与格式错误要分开。"""
    with pytest.raises(ApiError) as excinfo:
        _call_require_if_match({IF_MATCH_HEADER: "garbage"})
    assert excinfo.value.code is ErrorCode.MALFORMED_REQUEST
    assert excinfo.value.details["reason"] == "if_match_malformed"


def test_if_match_mismatched_version_raises_conflict() -> None:
    with pytest.raises(ApiError) as excinfo:
        _call_require_if_match({IF_MATCH_HEADER: build_etag(RESOURCE_ID, 2)})
    assert excinfo.value.code is ErrorCode.VERSION_CONFLICT
    # 回带当前 ETag，让前端不必多发一次 GET 就能重试。
    assert excinfo.value.details["currentEtag"] == build_etag(RESOURCE_ID, 3)


def test_if_match_other_resource_raises_conflict() -> None:
    """拿到别的资源的 ETag 也必须拒绝（不是"格式对就放行"）。"""
    with pytest.raises(ApiError) as excinfo:
        _call_require_if_match({IF_MATCH_HEADER: build_etag(uuid.uuid4(), 3)})
    assert excinfo.value.code is ErrorCode.VERSION_CONFLICT


def test_if_match_empty_list_raises_precondition_required() -> None:
    with pytest.raises(ApiError) as excinfo:
        _call_require_if_match({IF_MATCH_HEADER: ",,,"})
    assert excinfo.value.code is ErrorCode.PRECONDITION_REQUIRED
    assert excinfo.value.details["reason"] == "if_match_empty"


# --- 编号冲突判定（service.py）---------------------------------------------


class _FakeDiag:
    constraint_name = "uq_projects_project_code"


class _FakeOrig(Exception):
    diag = _FakeDiag()


def test_project_code_conflict_detected_by_constraint_name() -> None:
    """驱动给出约束名时按约束名判定。"""
    exc = IntegrityError("INSERT ...", {}, _FakeOrig("duplicate key"))
    assert _is_project_code_conflict(exc) is True


def test_other_constraint_is_not_project_code_conflict() -> None:
    """**必须精确判定**：其它约束冲突不能当成编号撞车去重试，
    否则真实业务错误会被"重试 5 次"掩盖掉。"""

    class _OtherDiag:
        constraint_name = "uq_project_members_active"

    class _OtherOrig(Exception):
        diag = _OtherDiag()

    exc = IntegrityError("INSERT ...", {}, _OtherOrig("duplicate key"))
    assert _is_project_code_conflict(exc) is False


def test_project_code_conflict_falls_back_to_text_match() -> None:
    """驱动不给约束名时退化为文本匹配（兼容不同驱动版本）。"""

    class _BareOrig(Exception):
        pass

    exc = IntegrityError(
        "INSERT ...", {}, _BareOrig("violates unique constraint \"uq_projects_project_code\"")
    )
    assert _is_project_code_conflict(exc) is True


# --- 权限矩阵新增项 --------------------------------------------------------


def test_operator_cannot_manage_dictionary() -> None:
    """**字典维护归管理者** —— 需求 §2.2 无对应行，按组织级主数据定位。"""
    from rednote.modules.projects.permissions import compute_project_permissions

    perms = compute_project_permissions(
        is_admin=False,
        membership=None,  # type: ignore[arg-type]
        project_phase=PHASE_PREPARATION,
    )
    assert perms.allows_org(OrgAction.MANAGE_DICTIONARY) is False


def test_admin_can_manage_dictionary() -> None:
    from rednote.modules.projects.permissions import compute_project_permissions

    perms = compute_project_permissions(
        is_admin=True,
        membership=None,  # type: ignore[arg-type]
        project_phase=PHASE_PREPARATION,
    )
    assert perms.allows_org(OrgAction.MANAGE_DICTIONARY) is True


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


def _auth(client: TestClient, user_id: uuid.UUID) -> str:
    """登录测试客户端，返回 CSRF 明文（写请求要用）。"""
    token, csrf = _create_session_for(user_id)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    return csrf


def _write_headers(csrf: str, **extra: str) -> dict[str, str]:
    headers = {CSRF_HEADER_NAME: csrf, "Origin": TEST_ORIGIN}
    headers.update(extra)
    return headers


# CSRF 双防线失败时返回的也是 403（PERMISSION_DENIED）。
# 因此凡是断言 403 的**权限**用例，都必须再确认拒绝原因不是 CSRF ——
# 否则用例漏带 `_write_headers(csrf)` 时会因为"缺 CSRF 令牌"而假通过，
# 真正的越权路径反而没人验证。（这是把 CSRF 校验统一挂到鉴权依赖后
# 新引入的歧义，必须有断言把它消掉。）
_CSRF_DENIAL_REASONS: frozenset[str] = frozenset(
    {
        "origin_mismatch",
        "referer_mismatch",
        "missing_origin_and_referer",
        "missing_csrf_token",
        "csrf_token_mismatch",
    }
)


def _assert_permission_denied(response: object) -> None:
    """断言这是**权限**拒绝，而不是 CSRF 拒绝。"""
    assert response.status_code == 403, response.text  # type: ignore[attr-defined]
    error = response.json().get("error", {})  # type: ignore[attr-defined]
    reason = (error.get("details") or {}).get("reason")
    assert reason not in _CSRF_DENIAL_REASONS, (
        f"该 403 来自 CSRF 校验（reason={reason}）而不是权限校验；"
        "权限用例必须带 `_write_headers(csrf)`"
    )


def _create_dictionary(*, brand_status: str = DICT_ACTIVE) -> tuple[uuid.UUID, uuid.UUID]:
    """建品牌 + 产品（独立事务提交）。"""
    with session_scope(_make_settings()) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}", status=brand_status)
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name="测试产品", model_code="M-1")
        session.add(product)
        session.flush()
        return brand.id, product.id


def _seed_project(
    *,
    owner_id: uuid.UUID,
    name: str = "测试项目",
    phase: str = PHASE_PREPARATION,
    due_date: date = date(2026, 12, 31),
    brand_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
    collaborators: tuple[uuid.UUID, ...] = (),
) -> uuid.UUID:
    """直接建库造项目（用于只读/写端点测试，不走 HTTP 建）。"""
    if brand_id is None or product_id is None:
        brand_id, product_id = _create_dictionary()
    with session_scope(_make_settings()) as session:
        project = Project(
            project_code=f"SEED-{uuid.uuid4().hex[:8].upper()}",
            name=name,
            brand_id=brand_id,
            product_id=product_id,
            owner_id=owner_id,
            phase=phase,
            target_delivery_count=100,
            due_date=due_date,
        )
        session.add(project)
        session.flush()
        repo = ProjectRepository(session)
        repo.add_owner_and_collaborators(
            project.id, owner_id=owner_id, collaborator_ids=collaborators
        )
        return project.id


def _seed_work_item(
    *, project_id: uuid.UUID, assignee_id: uuid.UUID | None, kind: str = "REVIEW"
) -> uuid.UUID:
    with session_scope(_make_settings()) as session:
        item = WorkItemRepository(session).create(
            kind=kind,
            subject_type="CONTENT_VERSION",
            subject_id=uuid.uuid4(),
            project_id=project_id,
            assignee_id=assignee_id,
            dedupe_key=f"dedupe-{uuid.uuid4().hex}",
        )
        return item.id


def _owner_of(project_id: uuid.UUID) -> uuid.UUID:
    with session_scope(_make_settings()) as session:
        project = ProjectRepository(session).get_project(project_id)
        assert project is not None
        return project.owner_id


def _lock_version_of(project_id: uuid.UUID) -> int:
    with session_scope(_make_settings()) as session:
        project = ProjectRepository(session).get_project(project_id)
        assert project is not None
        return project.lock_version


def _active_owner_count(project_id: uuid.UUID) -> int:
    with session_scope(_make_settings()) as session:
        return ProjectRepository(session).count_active_owners(project_id)


def _membership_role(project_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
    with session_scope(_make_settings()) as session:
        return ProjectRepository(session).get_membership(project_id, user_id).role


def _active_member_rows(project_id: uuid.UUID, user_id: uuid.UUID) -> int:
    """该用户在项目里的**活跃成员行数**。

    "升格"必须改现有行而不是新增行 —— 一旦新增就会出现两条活跃成员行，
    虽然 `uq_project_members_active` 会拦住，但把"被约束拦住"
    与"实现本来就正确"区分开，需要直接数行数。
    """
    with session_scope(_make_settings()) as session:
        return int(
            session.execute(
                select(func.count())
                .select_from(ProjectMember)
                .where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == user_id,
                    ProjectMember.removed_at.is_(None),
                )
            ).scalar_one()
        )


def _assignee_of(work_item_id: uuid.UUID) -> uuid.UUID | None:
    with session_scope(_make_settings()) as session:
        item = session.get(WorkItem, work_item_id)
        assert item is not None
        return item.assignee_id


def _brand_name(brand_id: uuid.UUID) -> str:
    """读库取品牌名。

    需要它是因为**测试库不清库**：品牌字典随每次跑测试持续累积，
    而 `GET /brands` 默认一页 20 条。若断言"刚建的品牌出现在第一页"，
    数据量一涨就会失败 —— 那是测试的错，不是代码的错。
    正确做法是用 `keyword` 把结果收敛到本次造的那一条（同 `_seed_pageable_projects`）。
    """
    with session_scope(_make_settings()) as session:
        brand = ProjectRepository(session).get_brand(brand_id)
        assert brand is not None
        return brand.name


# ---------------------------------------------------------------------------
# A. 创建项目
# ---------------------------------------------------------------------------


def test_admin_creates_project(client: TestClient) -> None:
    """管理者建项目：201 + 编号 + 负责人成员 + ETag。"""
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    brand_id, product_id = _create_dictionary()
    csrf = _auth(client, admin)

    response = client.post(
        "/api/v1/projects",
        headers=_write_headers(csrf),
        json={
            "name": "2026 秋季新品",
            "brandId": str(brand_id),
            "productId": str(product_id),
            "ownerId": str(owner),
            "targetDeliveryCount": 30,
            "dueDate": "2026-12-31",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["projectCode"].startswith("PRJ-")
    assert body["phase"] == PHASE_PREPARATION
    assert body["ownerId"] == str(owner)
    assert body["lockVersion"] == 1
    assert response.headers["ETag"] == f'"{body["id"]}:1"'

    project_id = uuid.UUID(body["id"])
    assert _active_owner_count(project_id) == 1
    assert _membership_role(project_id, owner) == MEMBER_OWNER


def test_operator_cannot_create_project(client: TestClient) -> None:
    """**只有管理者能建项目**（需求 §2.2 第 1 行）。"""
    operator = _create_user()
    owner = _create_user()
    brand_id, product_id = _create_dictionary()
    csrf = _auth(client, operator)

    response = client.post(
        "/api/v1/projects",
        headers=_write_headers(csrf),
        json={
            "name": "无权限项目",
            "brandId": str(brand_id),
            "productId": str(product_id),
            "ownerId": str(owner),
            "targetDeliveryCount": 10,
            "dueDate": "2026-12-31",
        },
    )

    _assert_permission_denied(response)
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


def test_create_project_rejects_archived_brand(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    brand_id, product_id = _create_dictionary(brand_status=DICT_ARCHIVED)
    csrf = _auth(client, admin)

    response = client.post(
        "/api/v1/projects",
        headers=_write_headers(csrf),
        json={
            "name": "归档品牌项目",
            "brandId": str(brand_id),
            "productId": str(product_id),
            "ownerId": str(owner),
            "targetDeliveryCount": 10,
            "dueDate": "2026-12-31",
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_create_project_rejects_mismatched_product_brand(client: TestClient) -> None:
    """**产品必须属于所选品牌** —— 否则会造出"华为品牌的苹果手机"。"""
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    brand_a, _product_a = _create_dictionary()
    _brand_b, product_b = _create_dictionary()
    csrf = _auth(client, admin)

    response = client.post(
        "/api/v1/projects",
        headers=_write_headers(csrf),
        json={
            "name": "错配项目",
            "brandId": str(brand_a),
            "productId": str(product_b),
            "ownerId": str(owner),
            "targetDeliveryCount": 10,
            "dueDate": "2026-12-31",
        },
    )

    assert response.status_code == 422, response.text
    fields = response.json()["error"]["details"]["fieldErrors"]
    assert fields[0]["field"] == "productId"


def test_create_project_rejects_unknown_owner(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    brand_id, product_id = _create_dictionary()
    csrf = _auth(client, admin)

    response = client.post(
        "/api/v1/projects",
        headers=_write_headers(csrf),
        json={
            "name": "幽灵负责人",
            "brandId": str(brand_id),
            "productId": str(product_id),
            "ownerId": str(uuid.uuid4()),
            "targetDeliveryCount": 10,
            "dueDate": "2026-12-31",
        },
    )

    assert response.status_code == 422, response.text


def test_create_project_rejects_disabled_owner(client: TestClient) -> None:
    """**不能把项目交给已停用的账号** —— 那会违反"必须有且只有一名主负责人"。"""
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    brand_id, product_id = _create_dictionary()
    csrf = _auth(client, admin)

    with session_scope(_make_settings()) as session:
        IdentityRepository(session).disable_user(owner)

    response = client.post(
        "/api/v1/projects",
        headers=_write_headers(csrf),
        json={
            "name": "停用负责人",
            "brandId": str(brand_id),
            "productId": str(product_id),
            "ownerId": str(owner),
            "targetDeliveryCount": 10,
            "dueDate": "2026-12-31",
        },
    )

    assert response.status_code == 422, response.text


def test_create_project_dedupes_owner_from_collaborators(client: TestClient) -> None:
    """负责人同时出现在协作者列表里 → 静默去重，成员表只有一条 OWNER。

    前端"默认勾上负责人"很常见，为此报错对用户没有价值。
    """
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    mate = _create_user()
    brand_id, product_id = _create_dictionary()
    csrf = _auth(client, admin)

    response = client.post(
        "/api/v1/projects",
        headers=_write_headers(csrf),
        json={
            "name": "去重项目",
            "brandId": str(brand_id),
            "productId": str(product_id),
            "ownerId": str(owner),
            "collaboratorIds": [str(owner), str(mate), str(mate)],
            "targetDeliveryCount": 10,
            "dueDate": "2026-12-31",
        },
    )

    assert response.status_code == 201, response.text
    project_id = uuid.UUID(response.json()["data"]["id"])
    with session_scope(_make_settings()) as session:
        members = ProjectRepository(session).list_members(project_id)
    assert len(members) == 2
    assert _membership_role(project_id, owner) == MEMBER_OWNER
    assert _membership_role(project_id, mate) == MEMBER_COLLABORATOR


def test_create_project_rejects_non_positive_delivery_count(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    brand_id, product_id = _create_dictionary()
    csrf = _auth(client, admin)

    response = client.post(
        "/api/v1/projects",
        headers=_write_headers(csrf),
        json={
            "name": "零篇项目",
            "brandId": str(brand_id),
            "productId": str(product_id),
            "ownerId": str(owner),
            "targetDeliveryCount": 0,
            "dueDate": "2026-12-31",
        },
    )

    assert response.status_code == 422, response.text


def test_create_project_requires_authentication(client: TestClient) -> None:
    brand_id, product_id = _create_dictionary()
    response = client.post(
        "/api/v1/projects",
        headers={CSRF_HEADER_NAME: "x", "Origin": TEST_ORIGIN},
        json={
            "name": "未登录",
            "brandId": str(brand_id),
            "productId": str(product_id),
            "ownerId": str(uuid.uuid4()),
            "targetDeliveryCount": 10,
            "dueDate": "2026-12-31",
        },
    )
    assert response.status_code == 401, response.text


@pytest.mark.regression
def test_create_project_requires_csrf(client: TestClient) -> None:
    """缺 CSRF 头的写请求必须被拒（WP08 的双防线）。"""
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    brand_id, product_id = _create_dictionary()
    _auth(client, admin)

    response = client.post(
        "/api/v1/projects",
        headers={"Origin": TEST_ORIGIN},
        json={
            "name": "无 CSRF",
            "brandId": str(brand_id),
            "productId": str(product_id),
            "ownerId": str(owner),
            "targetDeliveryCount": 10,
            "dueDate": "2026-12-31",
        },
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["reason"] == "missing_csrf_token"


def test_create_project_rejects_foreign_origin(client: TestClient) -> None:
    """CSRF 第一道防线：Origin 不属于本站 → 403（`origin_mismatch`）。

    令牌给对了也没用 —— 这正是"两道防线"的意义：
    只有双提交令牌时，若服务端存在 XSS，攻击者可先读 /me 再伪造请求头。
    """
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    brand_id, product_id = _create_dictionary()
    csrf = _auth(client, admin)

    response = client.post(
        "/api/v1/projects",
        headers=_write_headers(csrf, Origin="https://evil.example"),
        json={
            "name": "跨站建项目",
            "brandId": str(brand_id),
            "productId": str(product_id),
            "ownerId": str(owner),
            "targetDeliveryCount": 10,
            "dueDate": "2026-12-31",
        },
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["reason"] == "origin_mismatch"


def test_write_with_mismatched_csrf_token_is_403(client: TestClient) -> None:
    """CSRF 第二道防线：令牌与会话库中的摘要不符 → 403（`csrf_token_mismatch`）。"""
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    brand_id, product_id = _create_dictionary()
    _auth(client, admin)

    response = client.post(
        "/api/v1/projects",
        headers=_write_headers("not-the-token-for-this-session"),
        json={
            "name": "令牌不符",
            "brandId": str(brand_id),
            "productId": str(product_id),
            "ownerId": str(owner),
            "targetDeliveryCount": 10,
            "dueDate": "2026-12-31",
        },
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["reason"] == "csrf_token_mismatch"


def test_read_endpoints_do_not_require_csrf(client: TestClient) -> None:
    """读请求（GET）不带 Origin / CSRF 头也必须正常 —— 安全方法不触发校验。

    这条与"写请求必须带"成对：如果校验挂错了位置（例如无条件校验），
    所有读接口都会 403，前端首屏全挂。
    """
    admin = _create_user(role=ROLE_ADMIN)
    _seed_project(owner_id=admin)
    _auth(client, admin)

    response = client.get("/api/v1/projects")

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# B. 列表：游标分页与筛选
# ---------------------------------------------------------------------------


def _seed_pageable_projects(owner_id: uuid.UUID, prefix: str, count: int) -> list[uuid.UUID]:
    """造一批可被 keyword 收敛的项目（避免受库里其它数据影响）。"""
    brand_id, product_id = _create_dictionary()
    ids = []
    for index in range(count):
        ids.append(
            _seed_project(
                owner_id=owner_id,
                name=f"{prefix}-{index}",
                due_date=date(2027, 1, 1) + timedelta(days=index),
                brand_id=brand_id,
                product_id=product_id,
            )
        )
    return ids


@pytest.mark.regression
def test_project_list_cursor_pagination_is_complete_and_unique(client: TestClient) -> None:
    """**翻页不重不漏**：三页拿到的 id 集合恰好等于全部，且无重复。

    这是键集分页的核心断言。用 offset 分页时，若中途有插入/删除，
    这条断言就会失败 —— 那正是要防的缺陷。
    """
    admin = _create_user(role=ROLE_ADMIN)
    prefix = f"分页-{uuid.uuid4().hex[:6]}"
    expected = set(_seed_pageable_projects(admin, prefix, 5))
    csrf = _auth(client, admin)

    seen: list[uuid.UUID] = []
    cursor: str | None = None
    pages = 0
    while True:
        params = {"limit": 2, "keyword": prefix}
        if cursor:
            params["cursor"] = cursor
        response = client.get("/api/v1/projects", params=params)
        assert response.status_code == 200, response.text
        payload = response.json()
        seen.extend(uuid.UUID(item["id"]) for item in payload["data"])
        pages += 1
        if not payload["page"]["hasMore"]:
            break
        cursor = payload["page"]["nextCursor"]
        assert cursor
        assert pages < 10, "分页未收敛，可能陷入循环"

    assert pages == 3
    assert len(seen) == len(set(seen)) == 5
    assert set(seen) == expected


def test_project_list_filters_by_phase(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    prefix = f"筛选-{uuid.uuid4().hex[:6]}"
    _seed_project(owner_id=admin, name=f"{prefix}-准备期", phase=PHASE_PREPARATION)
    _seed_project(owner_id=admin, name=f"{prefix}-投产期", phase="PRODUCTION")
    _auth(client, admin)

    response = client.get(
        "/api/v1/projects", params={"keyword": prefix, "phase": "PRODUCTION"}
    )

    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    assert len(rows) == 1
    assert rows[0]["phase"] == "PRODUCTION"


def test_project_list_rejects_unknown_phase(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    _auth(client, admin)
    response = client.get("/api/v1/projects", params={"phase": "NOT_A_PHASE"})
    assert response.status_code == 422, response.text


def test_project_list_filters_by_owner(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    owner_a = _create_user()
    owner_b = _create_user()
    prefix = f"负责人-{uuid.uuid4().hex[:6]}"
    project_a = _seed_project(owner_id=owner_a, name=f"{prefix}-A")
    _seed_project(owner_id=owner_b, name=f"{prefix}-B")
    _auth(client, admin)

    response = client.get(
        "/api/v1/projects", params={"keyword": prefix, "ownerId": str(owner_a)}
    )

    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    assert [uuid.UUID(row["id"]) for row in rows] == [project_a]


def test_project_list_cursor_binding_rejects_changed_filter(client: TestClient) -> None:
    """**换了筛选条件的游标必须被拒**（400），而不是静默返回错位的一页。"""
    admin = _create_user(role=ROLE_ADMIN)
    prefix = f"绑定-{uuid.uuid4().hex[:6]}"
    _seed_pageable_projects(admin, prefix, 3)
    _auth(client, admin)

    first = client.get("/api/v1/projects", params={"limit": 1, "keyword": prefix})
    cursor = first.json()["page"]["nextCursor"]
    assert cursor

    reused = client.get(
        "/api/v1/projects",
        params={"limit": 1, "keyword": prefix, "cursor": cursor, "phase": "PREPARATION"},
    )

    assert reused.status_code == 400, reused.text
    assert reused.json()["error"]["details"]["reason"] == "cursor_binding_mismatch"


def test_project_list_operator_scope_is_bound_into_cursor(client: TestClient) -> None:
    """**运营不能复用管理者的游标** —— 可见范围被写进游标指纹。

    若指纹只绑排序与筛选、不绑可见范围，管理者把自己那一页的游标
    发给运营，运营就能拿到越界的一页。
    """
    admin = _create_user(role=ROLE_ADMIN)
    operator = _create_user()
    prefix = f"越界-{uuid.uuid4().hex[:6]}"
    _seed_pageable_projects(admin, prefix, 3)
    _auth(client, admin)
    admin_cursor = client.get(
        "/api/v1/projects", params={"limit": 1, "keyword": prefix}
    ).json()["page"]["nextCursor"]
    assert admin_cursor

    _auth(client, operator)
    response = client.get(
        "/api/v1/projects",
        params={"limit": 1, "keyword": prefix, "cursor": admin_cursor},
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["details"]["reason"] == "cursor_binding_mismatch"


def test_project_list_operator_sees_only_own_projects(client: TestClient) -> None:
    operator = _create_user()
    other = _create_user()
    prefix = f"隔离-{uuid.uuid4().hex[:6]}"
    mine = _seed_project(owner_id=operator, name=f"{prefix}-我的")
    _seed_project(owner_id=other, name=f"{prefix}-别人的")
    _auth(client, operator)

    response = client.get("/api/v1/projects", params={"keyword": prefix})

    assert response.status_code == 200, response.text
    assert [uuid.UUID(row["id"]) for row in response.json()["data"]] == [mine]


def test_project_list_keyword_matches_project_code(client: TestClient) -> None:
    """keyword 也匹配展示编号（运营常拿编号来查）。"""
    admin = _create_user(role=ROLE_ADMIN)
    project_id = _seed_project(owner_id=admin, name="编号搜索项目")
    _auth(client, admin)
    with session_scope(_make_settings()) as session:
        code = ProjectRepository(session).get_project(project_id).project_code  # type: ignore[union-attr]

    response = client.get("/api/v1/projects", params={"keyword": code})

    assert response.status_code == 200, response.text
    assert [uuid.UUID(row["id"]) for row in response.json()["data"]] == [project_id]


def test_project_list_keyword_wildcards_are_escaped(client: TestClient) -> None:
    """`%` 必须被转义为字面量，而不是通配符。

    否则搜 "100%" 会变成"以 100 开头的任意串" —— 结果看起来"很智能"，
    但语义是错的。
    """
    admin = _create_user(role=ROLE_ADMIN)
    prefix = f"通配-{uuid.uuid4().hex[:6]}"
    _seed_project(owner_id=admin, name=f"{prefix}%A")
    _seed_project(owner_id=admin, name=f"{prefix}B")
    _auth(client, admin)

    response = client.get("/api/v1/projects", params={"keyword": f"{prefix}%"})

    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    assert len(rows) == 1
    assert rows[0]["name"].endswith("%A")


# ---------------------------------------------------------------------------
# C. 详情与 ETag
# ---------------------------------------------------------------------------


def test_project_detail_returns_etag_header(client: TestClient) -> None:
    """协议 §3："Project + ETag" —— 详情必须带 ETag 响应头。"""
    admin = _create_user(role=ROLE_ADMIN)
    project_id = _seed_project(owner_id=admin)
    _auth(client, admin)

    response = client.get(f"/api/v1/projects/{project_id}")

    assert response.status_code == 200, response.text
    assert response.headers["ETag"] == f'"{project_id}:1"'
    assert response.json()["data"]["lockVersion"] == 1
    # 权限能力嵌在 data.permissions 下（不是 data 的顶层字段）。
    assert response.json()["data"]["permissions"]["canManageMembers"] is True


def test_project_detail_requires_authentication(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    project_id = _seed_project(owner_id=admin)
    response = client.get(f"/api/v1/projects/{project_id}")
    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# D. PATCH：乐观并发
# ---------------------------------------------------------------------------


def test_patch_without_if_match_is_428(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    project_id = _seed_project(owner_id=admin)
    csrf = _auth(client, admin)

    response = client.patch(
        f"/api/v1/projects/{project_id}",
        headers=_write_headers(csrf),
        json={"name": "改名"},
    )

    assert response.status_code == 428, response.text
    assert response.json()["error"]["code"] == "PRECONDITION_REQUIRED"


def test_patch_with_wrong_etag_is_412_with_current_etag(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    project_id = _seed_project(owner_id=admin)
    csrf = _auth(client, admin)

    response = client.patch(
        f"/api/v1/projects/{project_id}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:99"'}),
        json={"name": "改名"},
    )

    assert response.status_code == 412, response.text
    error = response.json()["error"]
    assert error["code"] == "VERSION_CONFLICT"
    assert error["details"]["currentEtag"] == f'"{project_id}:1"'


def test_patch_with_correct_etag_bumps_version(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    project_id = _seed_project(owner_id=admin)
    csrf = _auth(client, admin)

    response = client.patch(
        f"/api/v1/projects/{project_id}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={"name": "新名字", "targetDeliveryCount": 66},
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["name"] == "新名字"
    assert body["targetDeliveryCount"] == 66
    assert body["lockVersion"] == 2
    assert response.headers["ETag"] == f'"{project_id}:2"'
    assert _lock_version_of(project_id) == 2


def test_patch_reusing_stale_etag_after_update_is_412(client: TestClient) -> None:
    """**旧 ETag 在更新后必须失效** —— 这是"别人的改动不会被我覆盖"的保证。"""
    admin = _create_user(role=ROLE_ADMIN)
    project_id = _seed_project(owner_id=admin)
    csrf = _auth(client, admin)

    first = client.patch(
        f"/api/v1/projects/{project_id}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={"name": "第一次改名"},
    )
    assert first.status_code == 200

    second = client.patch(
        f"/api/v1/projects/{project_id}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={"name": "第二次改名"},
    )
    assert second.status_code == 412, second.text


def test_patch_rejects_phase_field(client: TestClient) -> None:
    """**不能直接改 phase**（协议 §3 明文）—— 传了得 422。"""
    admin = _create_user(role=ROLE_ADMIN)
    project_id = _seed_project(owner_id=admin)
    csrf = _auth(client, admin)

    response = client.patch(
        f"/api/v1/projects/{project_id}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={"phase": "CLOSED"},
    )

    assert response.status_code == 422, response.text


def test_patch_rejects_owner_id_field(client: TestClient) -> None:
    """**不能直接改 ownerId** —— 负责人变更走 assignments。"""
    admin = _create_user(role=ROLE_ADMIN)
    project_id = _seed_project(owner_id=admin)
    csrf = _auth(client, admin)

    response = client.patch(
        f"/api/v1/projects/{project_id}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={"ownerId": str(uuid.uuid4())},
    )

    assert response.status_code == 422, response.text


def test_patch_allows_collaborator_to_edit(client: TestClient) -> None:
    """协作者可编辑（需求 §2.2「编辑 Brief、方案与内容」行：协作者=是）。"""
    owner = _create_user()
    mate = _create_user()
    project_id = _seed_project(owner_id=owner, collaborators=(mate,))
    csrf = _auth(client, mate)

    response = client.patch(
        f"/api/v1/projects/{project_id}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={"name": "协作者改名"},
    )

    assert response.status_code == 200, response.text


def test_patch_by_non_member_is_404(client: TestClient) -> None:
    owner = _create_user()
    stranger = _create_user()
    project_id = _seed_project(owner_id=owner)
    csrf = _auth(client, stranger)

    response = client.patch(
        f"/api/v1/projects/{project_id}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={"name": "越权改名"},
    )

    assert response.status_code == 404, response.text


def test_patch_empty_body_does_not_bump_version(client: TestClient) -> None:
    """空 body 是合法请求（无变更），不应递增版本号。"""
    admin = _create_user(role=ROLE_ADMIN)
    project_id = _seed_project(owner_id=admin)
    csrf = _auth(client, admin)

    response = client.patch(
        f"/api/v1/projects/{project_id}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["lockVersion"] == 1


# ---------------------------------------------------------------------------
# E. 负责人交接
# ---------------------------------------------------------------------------


def test_assignment_transfers_owner_and_keeps_one_active_owner(client: TestClient) -> None:
    """**WP10 核心验收：并发交接仍一名负责人。**

    这里验证单次交接的终态；并发由数据库唯一索引保证（见下一条测试）。
    """
    admin = _create_user(role=ROLE_ADMIN)
    old_owner = _create_user()
    new_owner = _create_user()
    project_id = _seed_project(owner_id=old_owner)
    csrf = _auth(client, admin)

    response = client.post(
        f"/api/v1/projects/{project_id}/assignments",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={
            "newOwnerId": str(new_owner),
            "keepOldCollaborator": False,
            "reason": "原负责人调岗",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["previousOwnerId"] == str(old_owner)
    assert body["newOwnerId"] == str(new_owner)
    assert body["reason"] == "原负责人调岗"
    assert body["lockVersion"] == 2

    assert _owner_of(project_id) == new_owner
    assert _active_owner_count(project_id) == 1
    assert _membership_role(project_id, old_owner) is None


@pytest.mark.regression
def test_assignment_to_existing_collaborator_promotes_membership(
    client: TestClient,
) -> None:
    """**交接给项目内已有的协作者必须成功** —— 把成员行升格为 OWNER。

    这是最常见的交接场景之一（项目做大后交给已经在项目里的人）。
    若实现是"无条件插一条新的 OWNER 成员行"，会撞
    `uq_project_members_active`（(project_id, user_id) WHERE removed_at IS NULL），
    并被上层的 IntegrityError 处理误报成 412「负责人刚被其他操作变更」——
    错误信息与真实原因完全不符，而且用户永远无法完成这次交接。

    正确行为：查到该用户已有活跃成员行 → 改 `member_role` 为 OWNER，不新增行。
    """
    admin = _create_user(role=ROLE_ADMIN)
    old_owner = _create_user()
    collaborator = _create_user()
    project_id = _seed_project(owner_id=old_owner, collaborators=(collaborator,))
    csrf = _auth(client, admin)

    assert _membership_role(project_id, collaborator) == MEMBER_COLLABORATOR

    response = client.post(
        f"/api/v1/projects/{project_id}/assignments",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={
            "newOwnerId": str(collaborator),
            "keepOldCollaborator": True,
            "reason": "交给已在项目内的协作者",
        },
    )

    assert response.status_code == 201, response.text
    assert _owner_of(project_id) == collaborator
    assert _membership_role(project_id, collaborator) == MEMBER_OWNER
    assert _active_owner_count(project_id) == 1
    # 旧负责人被降级为协作者（keepOldCollaborator=True），仍在项目里。
    assert _membership_role(project_id, old_owner) == MEMBER_COLLABORATOR
    # 该用户只应有一条活跃成员行。
    assert _active_member_rows(project_id, collaborator) == 1


def test_assignment_to_collaborator_without_keeping_old_owner(
    client: TestClient,
) -> None:
    """同上的另一分支：不保留原负责人。"""
    admin = _create_user(role=ROLE_ADMIN)
    old_owner = _create_user()
    collaborator = _create_user()
    project_id = _seed_project(owner_id=old_owner, collaborators=(collaborator,))
    csrf = _auth(client, admin)

    response = client.post(
        f"/api/v1/projects/{project_id}/assignments",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={
            "newOwnerId": str(collaborator),
            "keepOldCollaborator": False,
            "reason": "原负责人离岗",
        },
    )

    assert response.status_code == 201, response.text
    assert _owner_of(project_id) == collaborator
    assert _membership_role(project_id, collaborator) == MEMBER_OWNER
    assert _active_owner_count(project_id) == 1
    assert _membership_role(project_id, old_owner) is None


@pytest.mark.regression
def test_add_member_duplicate_keeps_transaction_usable() -> None:
    """重复插入同一活跃成员撞唯一约束后，**事务必须仍然可用**。

    这是 `add_project_member` 的并发幂等分支能工作的前提：
    直接 `catch IntegrityError` 而不回滚到 SAVEPOINT，PG 会把整个事务
    标记为 aborted，随后那条"回查现有成员"的 SELECT 必然报
    `current transaction is aborted` —— 幂等分支退化成 500。
    """
    owner = _create_user()
    mate = _create_user()
    project_id = _seed_project(owner_id=owner)

    with session_scope(_make_settings()) as session:
        repository = ProjectRepository(session)
        repository.add_member(project_id, mate, MEMBER_COLLABORATOR)

        with pytest.raises(IntegrityError):
            with repository.savepoint():
                repository.add_member(project_id, mate, MEMBER_COLLABORATOR)

        # 关键断言：事务未 aborted，仍能查询。
        row = repository.get_member_row(project_id, mate)
        assert row is not None
        assert row.member_role == MEMBER_COLLABORATOR


def test_assignment_can_keep_old_owner_as_collaborator(client: TestClient) -> None:
    """需求 §2.3 第 4 条："管理者可同时勾选将其保留为协作者"。"""
    admin = _create_user(role=ROLE_ADMIN)
    old_owner = _create_user()
    new_owner = _create_user()
    project_id = _seed_project(owner_id=old_owner)
    csrf = _auth(client, admin)

    response = client.post(
        f"/api/v1/projects/{project_id}/assignments",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={
            "newOwnerId": str(new_owner),
            "keepOldCollaborator": True,
            "reason": "分工调整，原负责人继续参与",
        },
    )

    assert response.status_code == 201, response.text
    assert _owner_of(project_id) == new_owner
    assert _active_owner_count(project_id) == 1
    # 保留为协作者：仍是活跃成员，但角色降级。
    assert _membership_role(project_id, old_owner) == MEMBER_COLLABORATOR


def test_assignment_transfers_work_items_per_requirement(client: TestClient) -> None:
    """**需求 §2.3 第 2 条**：未认领的 + 原负责人的待办转给新负责人；
    明确分配给其他协作者的待办保留。
    """
    admin = _create_user(role=ROLE_ADMIN)
    old_owner = _create_user()
    new_owner = _create_user()
    mate = _create_user()
    project_id = _seed_project(owner_id=old_owner, collaborators=(mate,))

    unclaimed = _seed_work_item(project_id=project_id, assignee_id=None)
    former = _seed_work_item(project_id=project_id, assignee_id=old_owner)
    assigned_others = _seed_work_item(project_id=project_id, assignee_id=mate)

    csrf = _auth(client, admin)
    response = client.post(
        f"/api/v1/projects/{project_id}/assignments",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={
            "newOwnerId": str(new_owner),
            "keepOldCollaborator": False,
            "reason": "交接",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["data"]["transferredWorkItemCount"] == 2

    assert _assignee_of(unclaimed) == new_owner
    assert _assignee_of(former) == new_owner
    # 明确分配给其他协作者的保留 —— 这是需求原文的"保留"。
    assert _assignee_of(assigned_others) == mate


def test_assignment_records_reason_for_audit(client: TestClient) -> None:
    """需求 §2.3 第 1 条："选择新负责人并保存变更原因" —— 原因必须落库。"""
    admin = _create_user(role=ROLE_ADMIN)
    old_owner = _create_user()
    new_owner = _create_user()
    project_id = _seed_project(owner_id=old_owner)
    csrf = _auth(client, admin)

    client.post(
        f"/api/v1/projects/{project_id}/assignments",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={"newOwnerId": str(new_owner), "reason": "组织架构调整"},
    )

    with session_scope(_make_settings()) as session:
        rows = ProjectRepository(session).list_assignments(project_id)
    assert len(rows) == 1
    assert rows[0].reason == "组织架构调整"
    assert rows[0].previous_owner_id == old_owner
    assert rows[0].new_owner_id == new_owner
    assert rows[0].created_by == admin


def test_assignment_by_operator_is_403(client: TestClient) -> None:
    """需求 §2.2 第 1 行"创建项目、分配及变更负责人"仅管理者。"""
    operator = _create_user()
    owner = _create_user()
    newcomer = _create_user()
    project_id = _seed_project(owner_id=owner)
    csrf = _auth(client, operator)

    response = client.post(
        f"/api/v1/projects/{project_id}/assignments",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={"newOwnerId": str(newcomer), "reason": "越权"},
    )

    _assert_permission_denied(response)


def test_assignment_without_if_match_is_428(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    newcomer = _create_user()
    project_id = _seed_project(owner_id=owner)
    csrf = _auth(client, admin)

    response = client.post(
        f"/api/v1/projects/{project_id}/assignments",
        headers=_write_headers(csrf),
        json={"newOwnerId": str(newcomer), "reason": "缺前置条件"},
    )

    assert response.status_code == 428, response.text


def test_assignment_to_same_owner_is_422(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    csrf = _auth(client, admin)

    response = client.post(
        f"/api/v1/projects/{project_id}/assignments",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={"newOwnerId": str(owner), "reason": "同人"},
    )

    assert response.status_code == 422, response.text


def test_assignment_requires_reason(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    newcomer = _create_user()
    project_id = _seed_project(owner_id=owner)
    csrf = _auth(client, admin)

    response = client.post(
        f"/api/v1/projects/{project_id}/assignments",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={"newOwnerId": str(newcomer)},
    )

    assert response.status_code == 422, response.text


def test_assignment_with_stale_etag_is_412(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    newcomer = _create_user()
    project_id = _seed_project(owner_id=owner)
    csrf = _auth(client, admin)

    response = client.post(
        f"/api/v1/projects/{project_id}/assignments",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:7"'}),
        json={"newOwnerId": str(newcomer), "reason": "过期版本"},
    )

    assert response.status_code == 412, response.text
    assert _owner_of(project_id) == owner


def test_concurrent_assignment_keeps_single_owner_at_db_level() -> None:
    """**数据库层保证"任何时刻至多一名活跃负责人"。**

    这条不通过 HTTP：它直接验证 `uq_project_members_active_owner`
    这个部分唯一索引真的在拦截 —— 也就是"并发交接仍一名负责人"
    的最终防线。HTTP 层的 412 只是"表现得好看"，真正的保证在这里。
    """
    owner_a = _create_user()
    owner_b = _create_user()
    project_id = _seed_project(owner_id=owner_a)

    with session_scope(_make_settings()) as session:
        repo = ProjectRepository(session)
        # 不先移除 owner_a，直接插 owner_b —— 必须被拒。
        with pytest.raises(IntegrityError):
            repo.add_member(project_id, owner_b, MEMBER_OWNER)
        session.rollback()

    assert _active_owner_count(project_id) == 1
    assert _owner_of(project_id) == owner_a


def test_assignment_twice_sequentially_still_single_owner(client: TestClient) -> None:
    """**连续两次交接后仍只有一名负责人**（WP10 验收标准原文）。

    第二次用第一次返回的新 ETag，因此不是版本冲突，
    而是真的走完了第二次交接 —— 这验证了"移除旧负责人 + 插入新负责人"
    的顺序在重复执行下依然自洽。
    """
    admin = _create_user(role=ROLE_ADMIN)
    first_owner = _create_user()
    second_owner = _create_user()
    third_owner = _create_user()
    project_id = _seed_project(owner_id=first_owner)
    csrf = _auth(client, admin)

    one = client.post(
        f"/api/v1/projects/{project_id}/assignments",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:1"'}),
        json={"newOwnerId": str(second_owner), "reason": "第一次交接"},
    )
    assert one.status_code == 201, one.text
    version = one.json()["data"]["lockVersion"]

    two = client.post(
        f"/api/v1/projects/{project_id}/assignments",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{project_id}:{version}"'}),
        json={"newOwnerId": str(third_owner), "reason": "第二次交接"},
    )
    assert two.status_code == 201, two.text

    assert _owner_of(project_id) == third_owner
    assert _active_owner_count(project_id) == 1
    assert _membership_role(project_id, second_owner) is None
    with session_scope(_make_settings()) as session:
        rows = ProjectRepository(session).list_assignments(project_id)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# F. 协作者成员管理
# ---------------------------------------------------------------------------


def test_admin_adds_collaborator(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    mate = _create_user()
    project_id = _seed_project(owner_id=owner)
    csrf = _auth(client, admin)

    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=_write_headers(csrf),
        json={"userId": str(mate)},
    )

    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["userId"] == str(mate)
    assert body["memberRole"] == MEMBER_COLLABORATOR
    assert body["isOwner"] is False


def test_adding_existing_collaborator_is_idempotent(client: TestClient) -> None:
    """重复添加返回 200 与现有成员，不报错、不产生第二行。"""
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    mate = _create_user()
    project_id = _seed_project(owner_id=owner, collaborators=(mate,))
    csrf = _auth(client, admin)

    first = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=_write_headers(csrf),
        json={"userId": str(mate)},
    )
    second = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=_write_headers(csrf),
        json={"userId": str(mate)},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["data"]["userId"] == second.json()["data"]["userId"]


def test_adding_current_owner_as_collaborator_is_422(client: TestClient) -> None:
    """协议 §3："负责人经 assignments 修改"。"""
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    csrf = _auth(client, admin)

    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=_write_headers(csrf),
        json={"userId": str(owner)},
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["fieldErrors"][0]["field"] == "userId"


def test_operator_cannot_add_member(client: TestClient) -> None:
    """成员管理归管理者（当前矩阵：MANAGE_MEMBERS 仅管理者）。"""
    owner = _create_user()
    mate_operator = _create_user()
    newcomer = _create_user()
    project_id = _seed_project(owner_id=owner, collaborators=(mate_operator,))
    csrf = _auth(client, mate_operator)

    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=_write_headers(csrf),
        json={"userId": str(newcomer)},
    )

    _assert_permission_denied(response)


def test_remove_collaborator_is_204(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    mate = _create_user()
    project_id = _seed_project(owner_id=owner, collaborators=(mate,))
    csrf = _auth(client, admin)

    response = client.delete(
        f"/api/v1/projects/{project_id}/members/{mate}",
        headers=_write_headers(csrf),
    )

    assert response.status_code == 204, response.text
    assert _membership_role(project_id, mate) is None


def test_remove_owner_is_409(client: TestClient) -> None:
    """协议 §3："不能移除当前负责人"。"""
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    csrf = _auth(client, admin)

    response = client.delete(
        f"/api/v1/projects/{project_id}/members/{owner}",
        headers=_write_headers(csrf),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "INVALID_TRANSITION"
    assert _membership_role(project_id, owner) == MEMBER_OWNER


def test_remove_unknown_member_is_204(client: TestClient) -> None:
    """幂等：目标"不在项目里"已达成。"""
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    project_id = _seed_project(owner_id=owner)
    csrf = _auth(client, admin)

    response = client.delete(
        f"/api/v1/projects/{project_id}/members/{uuid.uuid4()}",
        headers=_write_headers(csrf),
    )

    assert response.status_code == 204, response.text


def test_removed_collaborator_loses_access_immediately(client: TestClient) -> None:
    """移除后立即失效（不需要等会话过期）——与 WP09 同一原则。"""
    admin = _create_user(role=ROLE_ADMIN)
    owner = _create_user()
    mate = _create_user()
    project_id = _seed_project(owner_id=owner, collaborators=(mate,))

    csrf = _auth(client, admin)
    client.delete(
        f"/api/v1/projects/{project_id}/members/{mate}",
        headers=_write_headers(csrf),
    )

    _auth(client, mate)
    response = client.get(f"/api/v1/projects/{project_id}")
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# G. 字典
# ---------------------------------------------------------------------------


def test_operator_can_read_dictionaries(client: TestClient) -> None:
    """需求 §2.2 实施默认："品牌／产品字典全员可见"。

    用 `keyword` 把结果收敛到本次造的品牌 —— 测试库不清库，
    依赖"默认第一页里有刚建的那条"会随数据累积而假失败。
    """
    operator = _create_user()
    brand_id, product_id = _create_dictionary()
    brand_name = _brand_name(brand_id)
    _auth(client, operator)

    brands = client.get("/api/v1/brands", params={"keyword": brand_name})
    products = client.get("/api/v1/products", params={"brandId": str(brand_id)})

    assert brands.status_code == 200, brands.text
    assert products.status_code == 200, products.text
    assert [row["id"] for row in brands.json()["data"]] == [str(brand_id)]
    assert [row["id"] for row in products.json()["data"]] == [str(product_id)]


def test_operator_cannot_create_brand(client: TestClient) -> None:
    operator = _create_user()
    csrf = _auth(client, operator)
    response = client.post(
        "/api/v1/brands", headers=_write_headers(csrf), json={"name": "运营建的品牌"}
    )
    _assert_permission_denied(response)


def test_admin_creates_and_renames_brand(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    csrf = _auth(client, admin)

    created = client.post(
        "/api/v1/brands", headers=_write_headers(csrf), json={"name": "新品牌"}
    )
    assert created.status_code == 201, created.text
    brand_id = created.json()["data"]["id"]
    assert created.json()["data"]["lockVersion"] == 1
    assert created.headers["ETag"] == f'"{brand_id}:1"'

    renamed = client.patch(
        f"/api/v1/brands/{brand_id}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{brand_id}:1"'}),
        json={"name": "改名后的品牌"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["data"]["name"] == "改名后的品牌"
    assert renamed.json()["data"]["lockVersion"] == 2


def test_brand_patch_without_if_match_is_428(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    brand_id, _product_id = _create_dictionary()
    csrf = _auth(client, admin)

    response = client.patch(
        f"/api/v1/brands/{brand_id}", headers=_write_headers(csrf), json={"name": "x"}
    )
    assert response.status_code == 428, response.text


def test_brand_patch_unknown_id_is_404(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    unknown = uuid.uuid4()
    csrf = _auth(client, admin)

    response = client.patch(
        f"/api/v1/brands/{unknown}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{unknown}:1"'}),
        json={"name": "x"},
    )
    assert response.status_code == 404, response.text


def test_archiving_referenced_brand_is_409(client: TestClient) -> None:
    """**被项目引用的品牌禁止归档**（OP-59 的保护）。"""
    admin = _create_user(role=ROLE_ADMIN)
    brand_id, product_id = _create_dictionary()
    _seed_project(owner_id=admin, brand_id=brand_id, product_id=product_id)
    csrf = _auth(client, admin)

    response = client.patch(
        f"/api/v1/brands/{brand_id}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{brand_id}:1"'}),
        json={"status": DICT_ARCHIVED},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["details"]["referencingProjectCount"] == 1


def test_archiving_unreferenced_brand_succeeds(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    brand_id, _product_id = _create_dictionary()
    csrf = _auth(client, admin)

    response = client.patch(
        f"/api/v1/brands/{brand_id}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{brand_id}:1"'}),
        json={"status": DICT_ARCHIVED},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == DICT_ARCHIVED


def test_archiving_referenced_product_is_409(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    brand_id, product_id = _create_dictionary()
    _seed_project(owner_id=admin, brand_id=brand_id, product_id=product_id)
    csrf = _auth(client, admin)

    response = client.patch(
        f"/api/v1/products/{product_id}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{product_id}:1"'}),
        json={"status": DICT_ARCHIVED},
    )

    assert response.status_code == 409, response.text


def test_product_patch_updates_model_code(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    _brand_id, product_id = _create_dictionary()
    csrf = _auth(client, admin)

    response = client.patch(
        f"/api/v1/products/{product_id}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{product_id}:1"'}),
        json={"modelCode": "M-2", "name": "改名产品"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["modelCode"] == "M-2"
    assert response.json()["data"]["name"] == "改名产品"


def test_product_patch_can_clear_model_code(client: TestClient) -> None:
    """显式传 null 是"清空型号"，与"没给这个字段"必须区分。"""
    admin = _create_user(role=ROLE_ADMIN)
    _brand_id, product_id = _create_dictionary()
    csrf = _auth(client, admin)

    response = client.patch(
        f"/api/v1/products/{product_id}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{product_id}:1"'}),
        json={"modelCode": None},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["modelCode"] is None


def test_creating_product_under_archived_brand_is_422(client: TestClient) -> None:
    admin = _create_user(role=ROLE_ADMIN)
    brand_id, _product_id = _create_dictionary()
    csrf = _auth(client, admin)

    # 先归档品牌（未被引用，可归档）。
    archived = client.patch(
        f"/api/v1/brands/{brand_id}",
        headers=_write_headers(csrf, **{IF_MATCH_HEADER: f'"{brand_id}:1"'}),
        json={"status": DICT_ARCHIVED},
    )
    assert archived.status_code == 200, archived.text

    response = client.post(
        "/api/v1/products",
        headers=_write_headers(csrf),
        json={"brandId": str(brand_id), "name": "归档品牌下的产品"},
    )

    assert response.status_code == 422, response.text


def test_brand_list_rejects_unknown_status_filter(client: TestClient) -> None:
    operator = _create_user()
    _auth(client, operator)
    response = client.get("/api/v1/brands", params={"status": "DELETED"})
    assert response.status_code == 422, response.text


def test_brand_list_paginates(client: TestClient) -> None:
    """字典列表也走游标分页（与项目列表同一套游标格式）。"""
    admin = _create_user(role=ROLE_ADMIN)
    prefix = f"字典-{uuid.uuid4().hex[:6]}"
    for index in range(3):
        with session_scope(_make_settings()) as session:
            session.add(Brand(name=f"{prefix}-{index}"))

    _auth(client, admin)
    first = client.get("/api/v1/brands", params={"limit": 2, "keyword": prefix})
    assert first.status_code == 200, first.text
    payload = first.json()
    assert len(payload["data"]) == 2
    assert payload["page"]["hasMore"] is True

    cursor = payload["page"]["nextCursor"]
    assert cursor

    second = client.get(
        "/api/v1/brands", params={"limit": 2, "keyword": prefix, "cursor": cursor}
    )
    assert second.status_code == 200, second.text
    assert len(second.json()["data"]) == 1
    assert second.json()["page"]["hasMore"] is False


# ---------------------------------------------------------------------------
# H. 契约完整性
# ---------------------------------------------------------------------------


def test_all_wp10_endpoints_are_present(app: FastAPI) -> None:
    """WP10 的 13 个端点必须在路由表里 —— 少一个就是漏实现。"""
    paths = {
        (method, route.path)
        for route in app.routes
        if hasattr(route, "methods")
        for method in (route.methods or set())
    }
    expected = {
        ("GET", "/api/v1/projects"),
        ("POST", "/api/v1/projects"),
        ("GET", "/api/v1/projects/{projectId}"),
        ("PATCH", "/api/v1/projects/{projectId}"),
        ("POST", "/api/v1/projects/{projectId}/assignments"),
        ("POST", "/api/v1/projects/{projectId}/members"),
        ("DELETE", "/api/v1/projects/{projectId}/members/{userId}"),
        ("GET", "/api/v1/brands"),
        ("POST", "/api/v1/brands"),
        ("PATCH", "/api/v1/brands/{brandId}"),
        ("GET", "/api/v1/products"),
        ("POST", "/api/v1/products"),
        ("PATCH", "/api/v1/products/{productId}"),
    }
    missing = expected - paths
    assert not missing, f"缺少端点：{sorted(missing)}"


def test_no_framework_error_schema_leaks_into_contract(app: FastAPI) -> None:
    """WP10 新增端点的错误响应声明必须齐全，不能把
    FastAPI 默认的 HTTPValidationError 漏进契约。"""
    import json

    schema = app.openapi()
    blob = json.dumps(schema, ensure_ascii=False)
    assert "HTTPValidationError" not in blob
