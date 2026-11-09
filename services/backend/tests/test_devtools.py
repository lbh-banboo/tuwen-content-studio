"""开发期登录旁路（G1 演示）与静态托管的行为护栏。

## 这些测试守的是什么

不是"dev 登录能返回 200"这种是否可用的浅层断言，而是三件**容易静默退化**的事：

1. **环境门是装配期的，不是运行期的。**
   `prod` 环境下 `/api/v1/dev/auth/login` 必须**不出现在路由表里**（404），
   而不是"出现在路由表里但在函数内部返回 403"。
   两者都让探测者拿不到数据，但后者意味着这块攻击面**已经被发布出去**了 ——
   一次重构、一个 merge 冲突、一行 `include_router` 被移到 if 外面，
   差距就出现了，且**没有任何报错**。
   本文件用 `create_app` 两个环境各建一次 app、直接比对路由表，
   把"装配期"这个字面承诺变成可执行的判据。

2. **dev 登录建的会话必须与真实登录结构一致。**
   如果 dev 登录自己造 Cookie（或跳过 `create_session`），
   那么"dev 登录能通"就推不出"真实登录链路能通"，G1 演示的结论就是假的。
   判据：会话行真的落在 `sessions` 表里、`token_hash` 是订单哈希、
   Cookie 属性（HttpOnly / SameSite / Path）与生产策略一致。

3. **静态托管是配置驱动且默认关闭的。**
   CI 与生产都不设 `REDNOTE_STATIC_DIR`，此时路由表里不能多出任何条目。
   同时把"挂载后唯一的行为变更"钉死：未注册路径 + 写方法会从
   404 变成 400（根因见 `main.py` Mount 段落）。**这条是防回归的** ——
   它现在是个已知的、被接受的行为，需要有人看着它别扩大。

## 关于数据库

dev 登录要写 `users` / `sessions` 两表，因此这些用例走真库（不 mock）。
读库用 `session_scope(settings)` 直查，**不用 ORM 对象比较** ——
"会话真的落库了"这句话的价值就在于它是从数据库里读出来的事实，
而不是从被测代码的返回值里推出来的。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from rednote.main import create_app
from rednote.modules.devtools.api import (
    DEV_ISSUER,
    DEV_LOGIN_ENVIRONMENTS,
)
from rednote.platform.configuration import Settings
from rednote.platform.csrf import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from rednote.platform.database import session_scope

pytestmark = pytest.mark.smoke

# 静态目录：相对仓库根定位，不依赖 cwd（CI 里 cwd 不固定）。
_REPO_ROOT = Path(__file__).resolve().parents[3]
_STATIC_DIR = _REPO_ROOT / "services" / "backend" / "static"

DEV_PATH = "/api/v1/dev/auth/login"

# 测试库 DSN 从环境变量读，与 conftest 及 test_identity 的约定一致
# （ISS-108：硬编码会让「测试库物理隔离」失效，CI 里 PG 用户也不是 rednote）。
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
TEST_DATABASE_URL = os.environ.get(
    "REDNOTE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL
).strip()


def _make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": TEST_DATABASE_URL,
        "environment": "test",
        "session_cookie_secure": False,  # 本机是 http，Secure Cookie 不会被发送
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _fetch_one(sql: str, **params: object):
    """从真库读一行。断言基于**库里的事实**，不基于被测代码的返回值。"""
    with session_scope(_make_settings()) as session:
        return session.execute(text(sql), params).one()


def _scalar(sql: str, **params: object):
    with session_scope(_make_settings()) as session:
        return session.execute(text(sql), params).scalar()


def _paths_of(app: FastAPI) -> set[str]:
    return {getattr(r, "path", "") for r in app.routes}


# ---------------------------------------------------------------------------
# fixture
#
# 沿用 `test_identity.py` 的做法：**自己建 app / client**，不用 `conftest` 的
# `client_with_database`。理由是本文件需要 `environment="test"` 之外的
# 多种环境（prod / staging / 空串）来验证装配期环境门，而 conftest 的
# fixture 固定了 environment，扩参会让它的语义变得含糊。
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    """连真库的 client（dev 登录需要写 users / sessions）。"""
    app = create_app(_make_settings())
    with TestClient(app) as test_client:
        yield test_client


# ===========================================================================
# 一、装配期环境门
# ===========================================================================


@pytest.mark.parametrize("environment", ["dev", "test"])
def test_dev_route_is_registered_in_dev_environments(environment: str) -> None:
    """dev / test 环境：路径**必须**在路由表里，否则演示无法登录。"""
    assert environment in DEV_LOGIN_ENVIRONMENTS

    app = create_app(Settings(database_url=None, environment=environment))

    assert DEV_PATH in _paths_of(app), (
        f"environment={environment} 下 {DEV_PATH} 未注册。"
        "G1 演示依赖它，注册条件被改坏了。"
    )


@pytest.mark.parametrize(
    "environment",
    ["prod", "production", "staging", "qa", ""],
)
def test_dev_route_is_absent_in_non_dev_environments(environment: str) -> None:
    """非 dev 环境：路径**必须不在**路由表里。

    判据刻意选"路由表里有没有"，而不是"请求返回什么状态码" ——
    后者只能证明"这次请求没成功"，证明不了"这个端点没被发布出去"。
    端点若被注册但内部判环境，攻击面依然是存在的（它可被枚举到）。

    `""` 也一并覆盖：空字符串不是合法环境名，但 `in` 判断若被写成
    `not in ("prod",)` 这类否定式，空串会意外通过。用正列举集合
    （`DEV_LOGIN_ENVIRONMENTS`）就不会有这个问题，此处把它钉住。
    """
    app = create_app(Settings(database_url=None, environment=environment))

    assert DEV_PATH not in _paths_of(app), (
        f"environment={environment!r} 下 {DEV_PATH} 竟然被注册了！"
        "这是安全事故：开发期旁路被带进了非开发环境的路由表。"
    )


def test_dev_route_guard_uses_positive_allowlist() -> None:
    """装配期的判据必须是**正列举白名单**，不是排除黑名单。

    黑名单（`if environment not in ("prod", "production")`）会漏掉
    未列举的环境名（"prd" / "PROD" 大小写 / "live"…），漏掉的后果是
    "以为关了其实开着"。白名单漏掉环境名的后果是"以为开着其实是 404"，
    排查成本低得多，且不会造成越权。两种失败模式的代价不对等，
    因此必须白名单。
    """
    assert isinstance(DEV_LOGIN_ENVIRONMENTS, frozenset), (
        "应为 frozenset（不可变、可做集合运算），实际是 "
        f"{type(DEV_LOGIN_ENVIRONMENTS).__name__}"
    )
    assert DEV_LOGIN_ENVIRONMENTS == frozenset({"dev", "test"}), (
        "dev 旁路的放行环境集合被改动了："
        f"{sorted(DEV_LOGIN_ENVIRONMENTS)}。"
        "新增环境前请确认它不会连到真实数据。"
    )
    assert "prod" not in DEV_LOGIN_ENVIRONMENTS
    assert "staging" not in DEV_LOGIN_ENVIRONMENTS


def test_dev_route_is_not_in_openapi_contract() -> None:
    """dev 端点是脚手架，**不进** OpenAPI 契约。

    契约是前后端协作的冻结接口面。把脚手架写进去会让下游按契约生成
    一个生产必然 404 的调用入口，也让"有什么就一定能调"这条可推理性失效。

    判据直接看 `app.openapi()` 的 paths（这是导出脚本读的同一份东西），
    而不是看代码里有没有写 `include_in_schema=False` ——
    后者是手段，前者才是效果。
    """
    app = create_app(Settings(database_url=None, environment="dev"))

    schema_paths = set(app.openapi()["paths"])

    assert DEV_PATH not in schema_paths, (
        f"{DEV_PATH} 出现在了 OpenAPI 里。"
        "它应该只在路由表中、不在契约中（include_in_schema=False）。"
    )
    # 反向确认：路由表里**有**它。若两处都没有，说明端点是整个丢了，
    # 而不是"正确地不进契约"—— 那是另一种故障。
    assert DEV_PATH in _paths_of(app)


# ===========================================================================
# 二、dev 登录的会话结构必须与真实登录一致
# ===========================================================================


def _login(client: TestClient, *, email: str, as_admin: bool = False):
    return client.post(
        DEV_PATH,
        json={"email": email, "displayName": email.split("@")[0], "asAdmin": as_admin},
        headers={"Origin": "http://testserver"},
    )


def test_dev_login_creates_real_session_row(client: TestClient) -> None:
    """会话行必须真的落库，且 `token_hash` 是摘要而不是明文。

    这是本旁路"没有绕过鉴权"的唯一硬证据：
      - 若直接 `set_cookie("rednote_session", "dev-fake")`，
        会话表里不会有对应的行，后续 `current_user` 第一步就查不到会话；
      - 若把明文令牌直接存进 `token_hash`，会话就退化成"拿着明文即可用"，
        与"服务端只存摘要"的设计相悖。
    """
    email = f"guardrail-{uuid.uuid4().hex[:8]}@example.test"
    resp = _login(client, email=email)

    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]

    row = _fetch_one(
        "select s.token_hash, s.csrf_secret_hash, s.expires_at "
        "from user_sessions s join users u on u.id = s.user_id "
        "where u.subject = :subject",
        subject=email,
    )

    token = client.cookies.get(SESSION_COOKIE_NAME)
    assert token, "响应未下发会话 Cookie"

    assert row.token_hash != token, "token_hash 存了明文令牌"
    assert len(row.token_hash) == 64, "token_hash 不是 SHA-256 十六进制摘要"
    assert len(row.csrf_secret_hash) == 64
    assert row.expires_at is not None
    # 响应体里的 csrfToken 是**明文**（前端要放进请求头），
    # 与库里存的摘要不同 —— 两者不能相等。
    assert body["csrfToken"] != row.csrf_secret_hash


def test_dev_login_user_is_tagged_with_dev_issuer(client: TestClient) -> None:
    """dev 建的账号必须带 `urn:rednote:dev` issuer，与真实 IdP 账号可区分。

    这条不是为了好看：库里混进一个 issuer 相同的假账号后，
    无法判断它是演示残留还是真实身份，"账号停用"之类的运维动作就失去依据。
    """
    email = f"issuer-{uuid.uuid4().hex[:8]}@example.test"
    assert _login(client, email=email).status_code == 201

    issuer = _scalar("select issuer from users where subject = :s", s=email)

    assert issuer == DEV_ISSUER


def test_dev_login_is_idempotent_per_email(client: TestClient) -> None:
    """同一邮箱重复登录命中同一账号，不重复建用户。

    `users` 有 `(issuer, subject)` 唯一约束兜底，但唯一约束只会让
    第二次插入报错（500），而不是"复用已有账号"。因此这里断言的是
    **用户数不涨**，把"find-or-create"里的 find 分支钉住。
    """
    email = f"repeat-{uuid.uuid4().hex[:8]}@example.test"

    before = _scalar("select count(*) from users where subject = :s", s=email)

    first = _login(client, email=email)
    second = _login(client, email=email)

    assert (first.status_code, second.status_code) == (201, 201)

    after = _scalar("select count(*) from users where subject = :s", s=email)

    assert before == 0
    assert after == 1, f"重复登录建了 {after} 个用户，find-or-create 的 find 分支失效"

    # 两次会话不同：每次都新建会话，不存在"会话被复用/可被固定"的问题。
    assert first.json()["data"]["sessionId"] != second.json()["data"]["sessionId"]


def test_dev_login_sets_cookies_with_production_attributes(
    client: TestClient,
) -> None:
    """Cookie 属性必须与生产策略一致（`csrf.py` 是唯一真相）。

    特别钉住 **CSRF Cookie 不加 HttpOnly**：这是双提交模式的硬要求
    （前端 JS 必须能读到它）。读者常误以为"所有 Cookie 都该 HttpOnly"，
    改掉它就等于把双提交模式变成不可用 —— 而且失败方式是
    "前端每个写请求都 403"，排查时会往 CSRF 配置上绕一大圈。
    """
    email = f"cookie-{uuid.uuid4().hex[:8]}@example.test"

    # cookie jar 只给出值，属性从响应头的 Set-Cookie 读。
    resp = _login(client, email=email)
    assert resp.status_code == 201, resp.text
    set_cookie = resp.headers.get_list("set-cookie")
    joined = " | ".join(set_cookie)

    assert SESSION_COOKIE_NAME in joined
    assert CSRF_COOKIE_NAME in joined

    session_line = next(c for c in set_cookie if c.startswith(SESSION_COOKIE_NAME))
    csrf_line = next(c for c in set_cookie if c.startswith(CSRF_COOKIE_NAME))

    assert "httponly" in session_line.lower(), "会话 Cookie 必须 HttpOnly"
    assert "httponly" not in csrf_line.lower(), (
        "CSRF Cookie 不能 HttpOnly —— 前端 JS 读不到它，双提交模式即失效"
    )
    for line in (session_line, csrf_line):
        assert "samesite=lax" in line.lower()
        assert "path=/" in line.lower()


def test_dev_login_rejects_extra_fields(client: TestClient) -> None:
    """请求体 `extra="forbid"`：拼错的字段名必须报错，不能被静默忽略。

    静默忽略的表现是"传了 asAdmin 却还是 OPERATOR"，然后去查权限系统 ——
    实际原因是字段名拼错了。早失败省一次排查。
    """
    resp = client.post(
        DEV_PATH,
        json={"email": "x@example.test", "asAdmim": True},  # 拼错
        headers={"Origin": "http://testserver"},
    )
    assert resp.status_code == 422, resp.text


# ===========================================================================
# 三、静态托管：默认关闭 + 已接受的行为变更被钉住
# ===========================================================================


def _static_app() -> FastAPI:
    return create_app(
        Settings(
            database_url=None,
            environment="dev",
            frontend_static_dir=str(_STATIC_DIR),
        )
    )


def test_static_mount_is_off_by_default() -> None:
    """不设 `frontend_static_dir` 时，路由表里不能多出任何条目。

    这是"本次改动不破坏已交付工作包"的可执行判据：
    CI 与生产都不设该配置，因此它们的路由表必须与加本功能之前一致。
    """
    without = create_app(Settings(database_url=None, environment="dev"))
    with_static = _static_app()

    assert len(with_static.routes) > len(without.routes), (
        "设了 frontend_static_dir 却没有多出 Mount，静态托管没生效"
    )

    # 反向：不设时不能有静态相关的 Mount。
    names = {getattr(r, "name", None) for r in without.routes}
    assert "frontend" not in names


def test_static_mount_rejects_nonexistent_directory() -> None:
    """配了但目录不存在 → **启动即失败**，不静默跳过。

    静默跳过的表现是"前端页面 404 但 API 正常"，排查时会先怀疑前端代码，
    而真正的原因（路径写错）要兜一大圈才发现。
    """
    with pytest.raises(RuntimeError, match="不存在"):
        create_app(
            Settings(
                database_url=None,
                environment="dev",
                frontend_static_dir="/no/such/directory/at/all",
            )
        )


def test_static_pages_are_served() -> None:
    """挂载后静态页可由后端托管（同源的物理前提）。"""
    app = _static_app()
    with TestClient(app) as client:
        resp = client.get("/login.html")
    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers.get("content-type", "")
    assert "<html" in resp.text.lower()


def test_root_still_returns_json_after_mount() -> None:
    """挂载静态目录后 `GET /` 仍返回那段 JSON 提示，而不是 index.html。

    `_root` 路由在 Mount 之前定义，Starlette 的显式路由优先于 Mount。
    这条是防"某天有人把 mount 提到 _root 之前"——
    那会让排障入口变成一张 HTML 页面，而 meter 类监控探针会开始报格式错。
    """
    app = _static_app()
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("application/json")
    assert "apiPrefix" in resp.json()


def test_mount_does_not_change_any_api_route() -> None:
    """挂载不得改变任何 `/api/v1/**` 端点的响应。

    这是"通配路由吃掉了 API"那个经典故障的正面判据。
    用**已注册端点**做样本：若挂载后它们变成 HTML 或换了状态码，立刻红。
    """
    without = create_app(Settings(database_url=None, environment="dev"))
    with_static = _static_app()

    samples = [
        ("GET", "/api/v1/health"),
        ("GET", "/api/v1/nope"),          # 未注册路径 + 安全方法
        ("POST", "/api/v1/work-items"),   # 已注册路径 + 未注册方法
    ]
    for method, path in samples:
        with TestClient(without) as c1, TestClient(with_static) as c2:
            r1 = c1.request(method, path, json={} if method == "POST" else None)
            r2 = c2.request(method, path, json={} if method == "POST" else None)

        assert r1.status_code == r2.status_code, (
            f"{method} {path} 挂载前后状态码不同："
            f"{r1.status_code} -> {r2.status_code}。"
            "静止态挂载不得改变既有端点的可观测行为。"
        )
        assert r1.headers.get("content-type") == r2.headers.get("content-type")


def test_mount_known_delta_is_pinned() -> None:
    """**已接受的已知行为变更**：未注册路径 + 写方法：404 → 400。

    根因链：
      `Mount("/")` 兜住请求 → StaticFiles 对非 GET 抛 **405**
      → 全局处理器 `errors._CODE_BY_STATUS` 里**没有 405 条目**
      → 落到"未映射的 4xx 统一按 MALFORMED_REQUEST"→ **400**。

    为什么不修：要修就得引入 405 映射，那会改变"未挂载"时既有的一批
    响应（现在未映射的 4xx 都是 400），属于改契约，代价大于收益。
    影响面仅"打错路径且用了写方法"，同为 4xx 结构化错误体，
    不触及任何既有端点的正常路径。

    **本用例的存在意义不是"期望它变成 400"，而是让这个边界被显式看着**：
    若某天有人改了错误映射或挂载方式导致它再次变化，
    这里会红，强制改的人意识到自己动了什么，而不是静默扩大。
    """
    without = create_app(Settings(database_url=None, environment="dev"))
    with_static = _static_app()

    for method in ("POST", "PUT", "DELETE"):
        with TestClient(without) as c1, TestClient(with_static) as c2:
            r1 = c1.request(method, "/api/v1/nope", json={})
            r2 = c2.request(method, "/api/v1/nope", json={})

        assert r1.status_code == 404
        assert r2.status_code == 400

        assert r1.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert r2.json()["error"]["code"] == "MALFORMED_REQUEST"


# ===========================================================================
# 四、停用账号：dev 登录必须与真实登录**同一条判定**
# ===========================================================================


def test_dev_login_rejects_disabled_account(client: TestClient) -> None:
    """停用账号不能登录，且**不得被登录动作静默改回启用**。

    本用例的出现源于一个真实缺陷：原实现里有一段
    `if user.status != "ACTIVE": repo.enable_user(...)`，它放在本判定**之前**，
    使本判定永不可达 —— 于是"dev 登录会拒绝停用账号"这句话在代码注释里成立、
    在运行时是假的，而 20 条既有用例**没有一条**能发现（全文件无 disable 相关断言）。

    因此这里断言的**不止是状态码**，还有两件更关键的事：
      - 登录**没有**新建会话（若只改状态码而放行了会话，状态断言会漏掉它）；
      - `users.status` **在登录后仍是 DISABLED**（回读数据库，不看返回值）。

    后者才是本用例的核心：它把"静默的状态写入"这个失败模式钉住。
    只断言 403 不够 —— 一个"先启用、再因为别的原因报 403"的实现也能通过。
    """
    email = f"disabled-{uuid.uuid4().hex[:8]}@example.test"

    # 正向前置：先建号并确认可用，否则"登录被拒"可能只是因为账号根本不存在，
    # 那样本用例会永远通过，与它要守的行为无关。
    assert _login(client, email=email).status_code == 201
    user_id = _scalar("select id from users where subject = :s", s=email)
    assert user_id is not None, "前置失败：dev 登录没有建出账号"
    sessions_before = _scalar(
        "select count(*) from user_sessions where user_id = :u", u=user_id
    )

    # 停用（直写数据库，两条 UPDATE 值同时给 —— CHECK 要求 status 与 disabled_at 一致）
    with session_scope(_make_settings()) as session:
        session.execute(
            text(
                "update users set status = 'DISABLED', disabled_at = now() "
                "where id = :u"
            ),
            {"u": user_id},
        )

    assert _scalar("select status from users where id = :u", u=user_id) == "DISABLED"

    # 被停用后再登录。**故意带上 asAdmin=true**：本端点在停用判定之前有一段
    # "按请求对齐角色"的写入，若它没被回滚，一次被拒的登录就会留下提权副作用。
    resp = _login(client, email=email, as_admin=True)

    assert resp.status_code == 403, (
        f"停用账号竟然登录成功了（{resp.status_code}）：{resp.text}"
    )
    body = resp.json()["error"]
    assert body["code"] == "PERMISSION_DENIED"
    # reason 与 `identity/api.py` 的回调**逐字一致** —— 这是"两条路径同一条判定"
    # 唯一可机检的形式化表达。
    assert body["details"]["reason"] == "user_disabled"

    # 核心断言①：状态未被登录动作改动。
    after = _fetch_one(
        "select status, disabled_at, role from users where id = :u", u=user_id
    )
    assert after.status == "DISABLED", (
        "dev 登录把停用账号改回了启用。这是静默的状态写入 —— "
        "停用是运维的显式决定，不该被一次登录撤销。"
    )
    assert after.disabled_at is not None, "disabled_at 被清空，与 status 不一致"

    # 核心断言③：被拒的登录不得留下**任何**写入副作用。
    #
    # 停用判定发生在"对齐 display_name / 提升角色"之后，因此这两处写入
    # 在被拒路径上必须被事务回滚掉。当前它成立靠的是 `session_scope`
    # 在异常路径 rollback —— 一条**隐式的、不在本端点代码里**的依赖。
    # 若有人给那段写入补一个显式 `session.commit()`（对本模块而言很诱人：
    # "让 dev 的提交时机更像显式"），提权就会在一次 403 里静默泄漏出去，
    # 而 403 本身看起来完全正常。
    #
    # 因此这里不看返回值、只看库里的角色：把"顺序"与"回滚"两条防线
    # 合并成一个可观测事实。
    assert after.role == "OPERATOR", (
        f"被拒的登录把角色改成了 {after.role} —— 提前于停用判定的写入"
        "没有被回滚掉。停用判定应当发生在任何状态写入之前。"
    )

    # 核心断言②：被拒的登录不得留下会话。
    sessions_after = _scalar(
        "select count(*) from user_sessions where user_id = :u", u=user_id
    )
    assert sessions_after == sessions_before, (
        f"登录被拒却新建了会话：{sessions_before} -> {sessions_after}。"
        "拒绝必须发生在建会话之前。"
    )


def test_dev_login_still_succeeds_after_account_is_reenabled(
    client: TestClient,
) -> None:
    """反向对照：账号被重新启用后，dev 登录**应当**恢复正常。

    与上一条构成一对：上一条证明"停用 → 拒绝"，本一条证明"启用 → 放行"。
    只有上一条的话，一个"永远拒绝所有人"的实现也能通过 ——
    那同样是错的（它会让 G1 演示整体不可用，但目前没有任何判据会红）。
    """
    email = f"reenabled-{uuid.uuid4().hex[:8]}@example.test"

    assert _login(client, email=email).status_code == 201
    user_id = _scalar("select id from users where subject = :s", s=email)

    with session_scope(_make_settings()) as session:
        session.execute(
            text(
                "update users set status = 'DISABLED', disabled_at = now() "
                "where id = :u"
            ),
            {"u": user_id},
        )
    assert _login(client, email=email).status_code == 403

    with session_scope(_make_settings()) as session:
        session.execute(
            text(
                "update users set status = 'ACTIVE', disabled_at = null "
                "where id = :u"
            ),
            {"u": user_id},
        )

    resp = _login(client, email=email)
    assert resp.status_code == 201, (
        f"账号已重新启用，登录仍被拒（{resp.status_code}）—— "
        "这说明拒绝逻辑比「停用」更宽，G1 演示会被整体挡住。"
    )

