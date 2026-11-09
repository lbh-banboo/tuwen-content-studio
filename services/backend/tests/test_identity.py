"""会话、CSRF 与激活/停用生命周期测试（WP08）。

覆盖 WP08 验收标准（研发计划 04 §4.2）：
> 过期和停用会话失效，浏览器无供应商令牌

以及研发计划 01 §10.1 的全部安全约束。

**本文件不 mock 数据库。** 会话的有效性判定（未撤销、未过期、用户 ACTIVE）
是这一层最核心的逻辑，mock 掉等于把验收变成 `assert True`。
测试连真库（本机 PG 17），每个测试用独立的 issuer/subject 前缀隔离数据。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rednote.modules.identity.models import ROLE_ADMIN, ROLE_OPERATOR, STATUS_ACTIVE
from rednote.modules.identity.repository import IdentityRepository
from rednote.platform.configuration import Settings
from rednote.platform.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
)
from rednote.platform.database import session_scope
from rednote.platform.tokens import (
    generate_csrf_secret,
    generate_session_token,
    hash_token,
)

# 本文件专用的 issuer，避免与其他测试的数据混淆。
TEST_ISSUER = "https://idp.test.invalid/realms/rednote"

# 测试用加密密钥（32 字节 base64）。**不是生产密钥** ——
# 生产由密钥管理系统注入（C6）。写在这里是为了让测试可复现。
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
        "session_cookie_secure": False,  # 本机是 http，Secure Cookie 不会被发送
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


@pytest.fixture
def repo() -> Iterator[IdentityRepository]:
    """直连数据库的仓储，用于在测试里**读取/检查**数据。

    **为什么准备数据不用这个 fixture**：`session_scope` 在退出时才 commit，
    而 API 请求走的是另一个 session/事务。在同一个 session 里 add 后不提交，
    API 那边看不到 —— 表现为"会话查不到"→ 401。
    因此准备数据统一走 `_create_user_with_session`（自己开会话并提交），
    本 fixture 只用于测试的断言与清理。

    **必须显式传 Settings**：`session_scope()` 无参时读进程级单例，
    而测试用注入配置（DSN 指向本机 PG）。不传会报"database_url 未配置"。
    """
    with session_scope(_make_settings()) as session:
        yield IdentityRepository(session)


def _unique_subject() -> str:
    """每次调用生成不同的 subject，避免测试间数据互相影响。"""
    return f"subject-{uuid.uuid4().hex[:12]}"


def _create_user_with_session(
    *,
    role: str = ROLE_OPERATOR,
    status: str = STATUS_ACTIVE,
    expires_in_seconds: int = 3600,
) -> tuple[uuid.UUID, str, str]:
    """建用户 + 会话，返回 (user_id, session_token, csrf_secret)。

    **自己开 session 并提交**，而不是用测试的 repo fixture：
    API 请求在独立事务里读数据，未提交的写入对它不可见。
    准备数据必须真正落库，否则会得到"会话查不到"的假失败。

    直接调仓储而不是走完整 OIDC 流程：本文件测"会话生命周期"，
    不是"OIDC 协议"。OIDC 流程另有专门测试。

    返回 user_id 供测试做后续操作（如停用账号）。
    """
    with session_scope(_make_settings()) as session:
        repo = IdentityRepository(session)
        user = repo.create_user(
            issuer=TEST_ISSUER,
            subject=_unique_subject(),
            display_name="测试用户",
            role=role,
        )
        if status != STATUS_ACTIVE:
            repo.disable_user(user.id)

        token = generate_session_token()
        csrf_secret = generate_csrf_secret()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
        repo.create_session(
            user_id=user.id,
            token_hash=hash_token(token),
            csrf_secret_hash=hash_token(csrf_secret),
            expires_at=expires_at,
        )
        user_id = user.id
    # 退出 with 时 session_scope 已 commit，数据对 API 可见。
    return user_id, token, csrf_secret


def _disable_user(user_id: uuid.UUID) -> None:
    """停用账号（独立事务，立即提交）。"""
    with session_scope(_make_settings()) as session:
        IdentityRepository(session).disable_user(user_id)


def _revoke_session(token: str) -> None:
    """撤销会话（独立事务，立即提交）。"""
    with session_scope(_make_settings()) as session:
        IdentityRepository(session).revoke_session(hash_token(token))


def _revoke_all_sessions(user_id: uuid.UUID) -> int:
    """撤销用户全部会话（独立事务，立即提交）。"""
    with session_scope(_make_settings()) as session:
        return IdentityRepository(session).revoke_all_user_sessions(user_id)


def _session_row(token: str) -> object:
    """读会话行（独立事务）。"""
    with session_scope(_make_settings()) as session:
        row = IdentityRepository(session).find_session_by_token_hash(hash_token(token))
        # 在 session 关闭前取需要的字段，避免 DetachedInstanceError。
        return None if row is None else {"revoked_at": row.revoked_at, "token_hash": row.token_hash}


def _auth_client(client: TestClient, token: str, csrf_secret: str) -> None:
    """把会话与 CSRF 注入 TestClient 的 Cookie。"""
    client.cookies.set(SESSION_COOKIE_NAME, token)
    client.cookies.set(CSRF_COOKIE_NAME, csrf_secret)


def _write_headers(csrf_secret: str) -> dict[str, str]:
    """构造合法写请求头（含 CSRF 与同源 Origin）。

    TestClient 默认 base_url 是 http://testserver，因此 Origin 用该值。
    """
    return {
        CSRF_HEADER_NAME: csrf_secret,
        "Origin": "http://testserver",
    }


# ---------------------------------------------------------------------------
# 会话有效性：这是"过期和停用会话失效"的直接验收
# ---------------------------------------------------------------------------


def test_me_requires_session_cookie(client: TestClient) -> None:
    """未带会话 Cookie 时 /me 返回 401 SESSION_EXPIRED。"""
    response = client.get("/api/v1/me")

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"
    assert response.json()["error"]["details"]["reason"] == "no_session_cookie"


def test_me_succeeds_with_valid_session(
    client: TestClient, repo: IdentityRepository
) -> None:
    """有效会话能拿到当前用户与 CSRF 令牌。"""
    _user_id, token, csrf_secret = _create_user_with_session()
    _auth_client(client, token, csrf_secret)

    response = client.get("/api/v1/me")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["role"] == ROLE_OPERATOR
    assert data["displayName"] == "测试用户"
    # csrfToken 必须回传 —— 前端靠它发起写请求。
    assert data["csrfToken"] == csrf_secret


def test_expired_session_is_rejected(
    client: TestClient, repo: IdentityRepository
) -> None:
    """**已过期的会话必须失效。**（WP08 验收项）

    构造一个 expires_at 在过去的会话，验证 /me 拒绝它。
    这条测试是"会话过期"的直接证据 —— 不是靠等 8 小时。
    """
    _user_id, token, csrf_secret = _create_user_with_session(expires_in_seconds=-60)
    _auth_client(client, token, csrf_secret)

    response = client.get("/api/v1/me")

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"


def test_revoked_session_is_rejected(
    client: TestClient, repo: IdentityRepository
) -> None:
    """已撤销的会话必须失效。"""
    _user_id, token, csrf_secret = _create_user_with_session()
    _revoke_session(token)
    _auth_client(client, token, csrf_secret)

    response = client.get("/api/v1/me")

    assert response.status_code == 401, response.text


def test_disabled_user_session_is_rejected_immediately(
    client: TestClient, repo: IdentityRepository
) -> None:
    """**账号停用后，已建立的会话必须立即失效。**（WP08 验收项核心）

    这是最关键的一条：用户先正常登录（会话有效），
    随后被停用 —— 此时**不撤销会话**，仅改 users.status，
    验证 /me 仍拒绝。

    为什么刻意不撤销会话：那样才能证明"状态检查"这道防线真的在工作。
    若测试里同时撤销了会话，就无法区分是"撤销生效"还是"状态检查生效"。
    生产上两道都会做（纵深防御），但测试要能分别验证。
    """
    user_id, token, csrf_secret = _create_user_with_session()
    _auth_client(client, token, csrf_secret)

    # 停用前：会话有效
    assert client.get("/api/v1/me").status_code == 200

    # 停用账号（不撤销会话）
    _disable_user(user_id)

    # 停用后：必须立即失效
    response = client.get("/api/v1/me")
    assert response.status_code == 401, response.text
    assert response.json()["error"]["details"]["reason"] == "user_disabled"


def test_revoke_all_sessions_kicks_out_user(
    client: TestClient, repo: IdentityRepository
) -> None:
    """撤销用户全部会话后，原会话失效（"踢下线"能力）。"""
    user_id, token, csrf_secret = _create_user_with_session()
    _auth_client(client, token, csrf_secret)

    assert client.get("/api/v1/me").status_code == 200

    revoked_count = _revoke_all_sessions(user_id)
    assert revoked_count >= 1

    assert client.get("/api/v1/me").status_code == 401


def test_session_token_is_not_stored_in_plaintext(repo: IdentityRepository) -> None:
    """会话令牌不得明文入库（只存摘要）。

    这是安全断言：一旦红了就是泄露事故。
    """
    _user_id, token, _csrf_secret = _create_user_with_session()

    session_row = _session_row(token)
    assert session_row is not None

    # 库里存的必须是摘要，不是原令牌。
    assert session_row["token_hash"] != token
    assert session_row["token_hash"] == hash_token(token)
    assert len(session_row["token_hash"]) == 64  # sha256 hex


def test_unknown_token_is_rejected(client: TestClient) -> None:
    """伪造的令牌摘要查不到会话，返回 401。"""
    client.cookies.set(SESSION_COOKIE_NAME, "rn1.forged-token-value")
    client.cookies.set(CSRF_COOKIE_NAME, "whatever")

    assert client.get("/api/v1/me").status_code == 401


# ---------------------------------------------------------------------------
# 浏览器无供应商令牌
# ---------------------------------------------------------------------------


def test_session_cookie_is_httponly(client: TestClient) -> None:
    """会话 Cookie 必须 HttpOnly（防 XSS 窃取）。

    通过登出响应检查 —— 登出会清除会话 Cookie，
    该 Set-Cookie 头里的属性应与设置时一致。
    """
    _user_id, token, csrf_secret = _create_user_with_session()
    _auth_client(client, token, csrf_secret)

    response = client.post("/api/v1/auth/logout", headers=_write_headers(csrf_secret))

    set_cookie_headers = [
        value for key, value in response.headers.multi_items() if key.lower() == "set-cookie"
    ]
    session_header = next(
        (h for h in set_cookie_headers if h.startswith(f"{SESSION_COOKIE_NAME}=")), None
    )
    assert session_header is not None, set_cookie_headers
    assert "httponly" in session_header.lower(), session_header


def test_csrf_cookie_is_readable_by_js(client: TestClient) -> None:
    """CSRF Cookie 必须**不是** HttpOnly（双提交模式要求 JS 可读）。

    这条测试守的是一个容易被"善意改错"的设计：
    有人看到"Cookie 应该 HttpOnly"的规则后会把这里也加上 HttpOnly，
    结果前端读不到 CSRF 令牌，所有写请求失败。
    """
    _user_id, token, csrf_secret = _create_user_with_session()
    _auth_client(client, token, csrf_secret)

    response = client.post("/api/v1/auth/logout", headers=_write_headers(csrf_secret))

    set_cookie_headers = [
        value for key, value in response.headers.multi_items() if key.lower() == "set-cookie"
    ]
    csrf_header = next(
        (h for h in set_cookie_headers if h.startswith(f"{CSRF_COOKIE_NAME}=")), None
    )
    assert csrf_header is not None, set_cookie_headers
    assert "httponly" not in csrf_header.lower(), csrf_header


def test_response_does_not_leak_oidc_tokens(client: TestClient) -> None:
    """响应体不得包含任何 OIDC 令牌。

    "浏览器无供应商令牌"（WP08 验收项）的直接断言：
    /me 返回的内容里不能出现 access_token / id_token / refresh_token 字段。
    """
    _user_id, token, csrf_secret = _create_user_with_session()
    _auth_client(client, token, csrf_secret)

    body = client.get("/api/v1/me").text

    assert "access_token" not in body
    assert "id_token" not in body
    assert "refresh_token" not in body


def test_no_token_in_url_on_logout(client: TestClient) -> None:
    """登出响应不得把令牌放进 URL 或 Location 头。"""
    _user_id, token, csrf_secret = _create_user_with_session()
    _auth_client(client, token, csrf_secret)

    response = client.post("/api/v1/auth/logout", headers=_write_headers(csrf_secret))

    assert token not in str(response.headers.get("location", ""))


# ---------------------------------------------------------------------------
# CSRF 校验
# ---------------------------------------------------------------------------


def test_write_without_csrf_header_is_rejected(
    client: TestClient, repo: IdentityRepository
) -> None:
    """写请求缺 CSRF 头必须被拒（403）。"""
    _user_id, token, csrf_secret = _create_user_with_session()
    _auth_client(client, token, csrf_secret)

    response = client.post(
        "/api/v1/auth/logout",
        headers={"Origin": "http://testserver"},  # 有 Origin，但无 CSRF 头
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["reason"] == "missing_csrf_token"


def test_write_with_wrong_csrf_token_is_rejected(
    client: TestClient, repo: IdentityRepository
) -> None:
    """CSRF 令牌不匹配必须被拒。"""
    _user_id, token, csrf_secret = _create_user_with_session()
    _auth_client(client, token, csrf_secret)

    response = client.post(
        "/api/v1/auth/logout",
        headers={CSRF_HEADER_NAME: "wrong-token", "Origin": "http://testserver"},
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["reason"] == "csrf_token_mismatch"


def test_write_from_cross_origin_is_rejected(
    client: TestClient, repo: IdentityRepository
) -> None:
    """跨站 Origin 必须被拒（即使 CSRF 令牌正确）。

    模拟"攻击者拿到了正确令牌但无法从本站发起请求"的场景 ——
    这正是 Origin 校验的价值。
    """
    _user_id, token, csrf_secret = _create_user_with_session()
    _auth_client(client, token, csrf_secret)

    response = client.post(
        "/api/v1/auth/logout",
        headers={CSRF_HEADER_NAME: csrf_secret, "Origin": "https://evil.example"},
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["reason"] == "origin_mismatch"


def test_write_without_origin_or_referer_is_rejected(
    client: TestClient, repo: IdentityRepository
) -> None:
    """Origin 与 Referer 都缺失时必须拒绝。

    放行等于把"无来源信息"当成"同源" —— 这是 CSRF 防护的常见漏洞。
    """
    _user_id, token, csrf_secret = _create_user_with_session()
    _auth_client(client, token, csrf_secret)

    # 显式清掉 Origin（TestClient 默认会带）
    response = client.post("/api/v1/auth/logout", headers={CSRF_HEADER_NAME: csrf_secret})

    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["reason"] == "missing_origin_and_referer"


def test_valid_write_succeeds(client: TestClient) -> None:
    """合法写请求（同源 + 正确 CSRF）成功，登出返回 204。"""
    _user_id, token, csrf_secret = _create_user_with_session()
    _auth_client(client, token, csrf_secret)

    response = client.post("/api/v1/auth/logout", headers=_write_headers(csrf_secret))

    assert response.status_code == 204, response.text


def test_logout_revokes_session(client: TestClient) -> None:
    """登出后原会话失效。"""
    _user_id, token, csrf_secret = _create_user_with_session()
    _auth_client(client, token, csrf_secret)

    assert client.post("/api/v1/auth/logout", headers=_write_headers(csrf_secret)).status_code == 204

    # 会话行应已标记撤销
    row = _session_row(token)
    assert row is not None
    assert row["revoked_at"] is not None


def test_logout_is_idempotent(client: TestClient) -> None:
    """重复登出不报错（幂等）。

    用户点两次登出不该看到错误；返回 404 会让前端无法区分
    "已登出"与"服务异常"。
    """
    _user_id, token, csrf_secret = _create_user_with_session()
    _auth_client(client, token, csrf_secret)

    first = client.post("/api/v1/auth/logout", headers=_write_headers(csrf_secret))
    assert first.status_code == 204

    # 第二次：Cookie 已被清除，请求不带会话。
    # 应仍返回 204（幂等），而不是 401。
    second = client.post("/api/v1/auth/logout", headers=_write_headers(csrf_secret))
    assert second.status_code == 204, second.text


# ---------------------------------------------------------------------------
# 登录流程参数校验
# ---------------------------------------------------------------------------


def test_open_redirect_is_blocked(client: TestClient) -> None:
    """`returnTo` 为外部 URL 时必须被替换为默认落地页（防开放重定向）。

    这是安全断言：开放重定向常用于钓鱼 ——
    链接看起来是本系统域名，跳转后却是伪造页面。
    """
    response = client.get(
        "/api/v1/auth/login",
        params={"returnTo": "https://evil.example/steal"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    # 应跳转到 IdP（带 state），而不是 evil.example
    location = response.headers["location"]
    assert "evil.example" not in location
    assert "state=" in location


def test_protocol_relative_redirect_is_blocked(client: TestClient) -> None:
    """`//evil.example` 形式也必须被拦。

    `//host/path` 被浏览器解释为协议相对 URL ——
    只检查"是否以 / 开头"的实现会在这里被绕过。
    """
    response = client.get(
        "/api/v1/auth/login", params={"returnTo": "//evil.example/x"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = response.headers["location"]
    assert "evil.example" not in location


def test_login_redirects_with_pkce_and_state(client: TestClient) -> None:
    """登录跳转必须带 state 与 PKCE 参数。"""
    response = client.get("/api/v1/auth/login", follow_redirects=False)

    assert response.status_code == 302
    location = response.headers["location"]
    assert "state=" in location
    assert "code_challenge=" in location
    assert "code_challenge_method=S256" in location
    assert "nonce=" in location


def test_login_sets_login_flow_cookie(client: TestClient) -> None:
    """登录跳转必须写入登录流程 Cookie（HttpOnly）。"""
    response = client.get("/api/v1/auth/login", follow_redirects=False)

    headers = [
        value for key, value in response.headers.multi_items() if key.lower() == "set-cookie"
    ]
    flow_header = next((h for h in headers if h.startswith("rednote_login_flow=")), None)
    assert flow_header is not None, headers
    assert "httponly" in flow_header.lower()


def test_login_without_security_key_fails_clearly(client: TestClient) -> None:
    """未配置安全密钥时明确失败，不静默使用弱默认值。"""
    from rednote.main import create_app

    app = create_app(_make_settings(oidc_token_encryption_key=None))
    with TestClient(app) as weak_client:
        response = weak_client.get("/api/v1/auth/login", follow_redirects=False)

    assert response.status_code == 503, response.text
    assert response.json()["error"]["details"]["reason"] == "security_key_not_configured"


# ---------------------------------------------------------------------------
# 角色与权限
# ---------------------------------------------------------------------------


def test_admin_role_is_reported(client: TestClient) -> None:
    """ADMIN 用户的角色正确返回。"""
    _user_id, token, csrf_secret = _create_user_with_session(role=ROLE_ADMIN)
    _auth_client(client, token, csrf_secret)

    data = client.get("/api/v1/me").json()["data"]
    assert data["role"] == ROLE_ADMIN


def test_capabilities_are_present(client: TestClient) -> None:
    """能力清单必须返回（协议 §2）。

    注意：能力清单是**展示信息**，不是授权依据 ——
    后端仍校验每个操作。这里只断言字段存在与关键值正确。
    """
    _user_id, token, csrf_secret = _create_user_with_session()
    _auth_client(client, token, csrf_secret)

    capabilities = client.get("/api/v1/me").json()["data"]["capabilities"]

    assert "TEXT_ONLY" in capabilities["enabledFormats"]
    assert "LIVE" in capabilities["enabledFormats"]
    # 模式 A：本地模型不可用（ADR-10）
    assert capabilities["localModels"] is False
    # 数量上限来自配置口径（ADR-13），不是写死在 DDL 里
    assert capabilities["limits"]["trialQuantity"] == 10
    assert capabilities["limits"]["scaleMax"] == 30
    assert capabilities["limits"]["bulkMax"] == 50