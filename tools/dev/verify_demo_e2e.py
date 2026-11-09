#!/usr/bin/env python
"""G1 端到端活性验证：把"种子数据真的能通过真实 HTTP 端点读出来"这件事跑成证据。

## 为什么需要这个脚本（而不是只跑 pytest）

`tests/test_devtools.py` 用 `TestClient`（进程内 ASGI 调用）验证行为，
它证明不了**跨进程、跨 socket、真 uvicorn** 的那条链路。两者的差别不是
"更严格"这种程度问题，而是**覆盖了不同的失败模式**：

| 只有 TestClient 会漏掉 | 本脚本能抓到 |
|---|---|
| 端口没起 / 起在别的 host | 真连 `127.0.0.1:<port>` |
| 静态目录挂载的实际路径解析（相对 cwd 的差异） | 真取 `/login.html` 的 HTML |
| Cookie 在真实响应里是否带属性 | 读 Set-Cookie 原始行 |
| CSRF 的 `Origin` 校验在真请求上的行为 | 换 Origin 发真请求 |

## 它验的六件事

1. **静态页由后端托管**（同源的物理前提 —— CSRF 要求 Origin 严格相等）
2. **dev 登录旁路**：无 Keycloak 拿到会话 Cookie
3. **Cookie 属性分野**：会话 Cookie HttpOnly、CSRF Cookie 不 HttpOnly
4. **带会话读业务数据**：`/me`、`/projects` 返回种子数据
5. **权限维度是真的**：非 ADMIN 只看到自己参与的项目（若种子 `project_members`
   是摆设，非 ADMIN 也会看到全部 8 条 —— 那样"演示能跑"就不可信）
6. **CSRF 三道防线**：跨源被拒、缺头被拒、令牌错被拒；且合法请求能成功

## 用法

```bash
PY=/Users/<user>/.workbuddy/binaries/python/envs/default/bin/python
NO_PROXY='*' $PY tools/dev/verify_demo_e2e.py
```

脚本**自己拉起服务**（uvicorn 子进程）、跑完**自己关掉** ——
不要求调用者先手动起服务，否则"忘了起服务"会表现成"验证失败"，
而真相是环境没准备好。这与本仓"让失败原因唯一"的取向一致。

## 两条自我约束（缺了它们，这个脚本自己就会变成新的坑）

**1. 自己造的残留自己收。** 脚本的对照实验会真写一条项目、
每轮登录都真建一个 `user_sessions` 行。跑一轮全量回归实测留下
**上百行会话**。因此收尾会精确删掉本次创建的探测项目与会话 id
（按 id 删，不按时间范围、不全表清空 —— 手工调试留下的数据不该被顺手抹掉），
并在 `finally` 里执行，异常中途退出也照清。

**2. 判据总数护栏。** `EXPECTED_MIN_CHECKS` 是实测基线（当前 36）。
**判据被 `if` 条件包住时，条件不成立会让它整条消失，而脚本仍打印
"N/N 全过"** —— 这个形态在本脚本的开发过程中真实出现过（注入缺陷时
判据数从 36 缩到 34 而结果仍是"全过"）。因此判据数少于基线即判失败。
新增判据时必须同步把基线 +1，否则护栏会自己报警。

## 退出码

0 = 全部通过；1 = 有失败（逐条打印哪一条、实际值是什么）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path

from sqlalchemy import create_engine, text

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _REPO_ROOT / "services" / "backend"
_STATIC_DIR = _BACKEND_DIR / "static"

HOST = "127.0.0.1"
PORT = 8899  # 与 `configuration.py::oidc_redirect_uri` 的默认端口一致
BASE = f"http://{HOST}:{PORT}"

ADMIN_EMAIL = "member06@example.com"
OPERATOR_EMAILS = (
    ("member11@example.com", "周予桐"),
    ("member01@example.com", "陈默"),
    ("member10@example.com", "许南嘉"),
)

# 种子里项目总数（8 个）。ADMIN 无成员过滤，因此应看到全部。
EXPECTED_PROJECT_COUNT = 8

# 项目编号长度。格式见 `platform/codes.py`：
#   `PRJ` + `-` + `YYYYMM` + `-` + 6 位 Crockford Base32  =  3+1+6+1+6 = 17
# 特意做成常量并附算式：这个数字被写错过两次（曾误记为 20），
# 而写错的表现是"编号格式不符"这种看起来像产品问题的假失败。
PROJECT_CODE_LENGTH = 17

# Crockford Base32 字母表（去 I/L/O/U），与 `platform/codes.py::CODE_ALPHABET` 一致。
# **刻意在本脚本内重复定义**：判据若从被测代码里 import 常量，
# 代码把字母表改错时判据会跟着一起错，测不出来。
PROJECT_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# 对照实验临时项目的名字前缀。清理逻辑按这个前缀精确匹配删除，
# 因此这也是"清理不会误伤种子数据"的依据（种子项目名不含该前缀）。
PROBE_PROJECT_PREFIX = "E2E 验证临时项目 "

# 判据总数基线。**这是防"判据静默跳过"的机械护栏**：
# 本脚本的判据分布是固定的（六组各若干条），一旦某条判据被 `if` 条件
# 包住而条件不成立，它会整条消失 —— 脚本仍然打印"N/N 全过"，
# 看起来一切正常。反向验证（注入缺陷）时实测到过 36 → 34 的缩水。
# 因此显式记录期望值：实际判据数少于基线即判失败。
#
# **这个数字是实测出来的，不是估的**：第一版凭印象写 38，当场被护栏
# 抓成"静默跳过"，而实际就是 36。护栏的基线本身也不能靠猜 ——
# 猜高会让脚本永远报警，猜低则失去意义。
# **维护方式**：新增判据时必须同步 +1，否则本护栏会自己报警。
EXPECTED_MIN_CHECKS = 36

# 服务启动等待上限（秒）。不再往上加 —— 超时说明启动真的失败了，
# 应该报出来而不是无限等。
STARTUP_TIMEOUT_SECONDS = 30.0

_FAILURES: list[str] = []
_CHECKS = 0

# 本脚本创建的 sessionId 集合。`login()` 每调用一次就建一个会话；
# 六组验证累计调用约十来次，不清就会在演示库里越积越多
# （实测一次完整的全量回归留下一百余行）。只记录"本次运行"创建的，
# 清理时按这些 id 精确删除，**不按时间范围或全表删** —— 后者会误伤
# 手工调试留下的会话。
_CREATED_SESSION_IDS: list[str] = []


# ---------------------------------------------------------------------------
# 断言辅助
# ---------------------------------------------------------------------------


def check(ok: bool, label: str, detail: str = "") -> None:
    """记一条判据。失败不抛异常 —— 让脚本把所有问题一次报完。

    为什么不用 assert：一个失败的 assert 会中断脚本，后面的判据就看不到。
    验证脚本的价值之一是"一次跑完知道全部问题"，逐条 assert 会把它变成
    "改一个跑一次"。
    """
    global _CHECKS
    _CHECKS += 1
    if ok:
        print(f"  [PASS] {label}")
    else:
        print(f"  [FAIL] {label}")
        if detail:
            print(f"         {detail}")
        _FAILURES.append(label if not detail else f"{label} :: {detail}")


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------


class ResponseHeaders:
    """响应头容器，**保留同名头的多值语义**。

    为什么不用 `dict(resp.headers)`：`resp.headers` 是 `email.message.Message`，
    同一个头可以合法地出现多次 —— `Set-Cookie` 就是典型（本项目一次登录下发
    两个 Cookie：会话 + CSRF）。`dict()` 会让后一行覆盖前一行，于是只剩一个
    Cookie，判据"CSRF Cookie 已下发"必然失败，而失败原因看起来像"后端没下发"。

    **这正是本仓反复出现的缺陷形态**：防御代码写了、但代码路径走不到 ——
    本脚本第一版就踩了这个坑（`set_cookie_lines` 里那句"urllib 会把同名头
    聚合成列表"的注释，在 `dict()` 之后永远为假）。因此这里显式保存全部
    (name, value) 对，并只通过 `get()` / `get_all()` 取值，不再暴露 dict 形态。
    """

    __slots__ = ("_items",)

    def __init__(self, message: object) -> None:
        items = getattr(message, "items", None)
        self._items: list[tuple[str, str]] = list(items()) if callable(items) else []

    def get(self, name: str, default: str = "") -> str:
        """取同名头的第一个值；不存在时返回 `default`。"""
        values = self.get_all(name)
        return values[0] if values else default

    def get_all(self, name: str) -> list[str]:
        """取同名头的全部值，**顺序与响应中的出现顺序一致**。"""
        lowered = name.lower()
        return [value for key, value in self._items if key.lower() == lowered]


def _new_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar())
    )


def call(
    opener: urllib.request.OpenerDirector,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict | str, ResponseHeaders]:
    """发一个请求，返回 (状态码, JSON 或原文, 响应头)。不抛异常。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with opener.open(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", "replace")
            hdrs = ResponseHeaders(resp.headers)
            try:
                return resp.status, json.loads(raw), hdrs
            except json.JSONDecodeError:
                return resp.status, raw, hdrs
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        hdrs = ResponseHeaders(exc.headers)
        try:
            return exc.code, json.loads(raw), hdrs
        except json.JSONDecodeError:
            return exc.code, raw, hdrs
    except Exception as exc:  # noqa: BLE001 - 记录后继续，不中断整轮
        return -1, {"_err": f"{type(exc).__name__}: {exc}"}, ResponseHeaders(None)


def error_code(payload: dict | str) -> str:
    if isinstance(payload, dict):
        return payload.get("error", {}).get("code", "")
    return ""


def error_reason(payload: dict | str) -> str:
    if isinstance(payload, dict):
        return payload.get("error", {}).get("details", {}).get("reason", "")
    return ""


def set_cookie_lines(headers: ResponseHeaders) -> list[str]:
    """取全部 `Set-Cookie` 原始行（保持顺序，不合并、不覆盖）。

    一次登录会下发**两条** `Set-Cookie`（`rednote_session` + `rednote_csrf`），
    因此这里必须拿到全部，不能只取第一条。
    """
    return headers.get_all("Set-Cookie")


def login(
    email: str, *, as_admin: bool = False, origin: str | None = BASE
) -> tuple[urllib.request.OpenerDirector, int, dict, ResponseHeaders]:
    opener = _new_opener()
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    status, payload, resp_headers = call(
        opener,
        "POST",
        "/api/v1/dev/auth/login",
        body={"email": email, "asAdmin": as_admin},
        headers=headers,
    )
    # 记录本脚本创建的会话 id，供收尾清理。`sessionId` 由 dev 登录端点返回，
    # 对应 `user_sessions.id`（不是 token）。取不到就不记 —— 宁可有残留
    # 也不要瞎删。
    if isinstance(payload, dict):
        session_id = payload.get("data", {}).get("sessionId")
        if isinstance(session_id, str) and session_id:
            _CREATED_SESSION_IDS.append(session_id)
    return opener, status, payload, resp_headers


# ---------------------------------------------------------------------------
# 服务生命周期
# ---------------------------------------------------------------------------


def _port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((HOST, PORT)) == 0


def start_server() -> subprocess.Popen:
    """拉起 uvicorn。返回进程句柄，调用方负责终止。

    `REDNOTE_ENVIRONMENT=dev` 是必须的：非 dev 环境下 dev 路由不会装配，
    脚本的第二步就会 404，而那看起来像"脚本坏了"而不是"环境不对"。
    """
    env = os.environ.copy()
    env.update(
        {
            "REDNOTE_ENVIRONMENT": "dev",
            "REDNOTE_DATABASE_URL": _database_url(),
            "REDNOTE_FRONTEND_STATIC_DIR": str(_STATIC_DIR),
            "REDNOTE_LOG_LEVEL": "WARNING",
            "PYTHONPATH": "src",
            # 本机代理会劫持 localhost 连接（G3 铁律）。
            "NO_PROXY": "*",
        }
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            # **必须 --factory**：`rednote.main` 没有模块级 `app`，只有
            # `create_app()`。漏了这个参数，uvicorn 会报
            # "Attribute 'app' not found in module" 然后退出。
            "--factory",
            "rednote.main:create_app",
            "--host",
            HOST,
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        cwd=str(_BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"服务启动即退出（返回码 {proc.returncode}）：\n{out}")
        if _port_in_use():
            # 端口通了不等于健康检查就绪（可能还在 import）。
            # 再探一次健康端点，确认真的能响应。
            status, _, _ = call(_new_opener(), "GET", "/api/v1/health")
            if status == 200:
                return proc
        time.sleep(0.3)

    proc.terminate()
    raise RuntimeError(f"服务在 {STARTUP_TIMEOUT_SECONDS}s 内未就绪")


def is_valid_project_code(code: object) -> bool:
    """校验 `PRJ-YYYYMM-XXXXXX` 的**结构**，而非仅长度。

    只查 `len == 17` 不足以说明编号合法：`XXXXXXXXXXXXXXX` 也是 17 字符。
    逐段校验（前缀 / 6 位数字月份 / 6 位 Crockford 字母表）才排得掉
    "长度对但形态错"的假通过。
    """
    if not isinstance(code, str) or len(code) != PROJECT_CODE_LENGTH:
        return False
    prefix, _, rest = code.partition("-")
    month, _, tail = rest.partition("-")
    return (
        prefix == "PRJ"
        and month.isdigit()
        and len(month) == 6
        and len(tail) == 6
        and all(char in PROJECT_CODE_ALPHABET for char in tail)
    )


# ---------------------------------------------------------------------------
# 六项验证
# ---------------------------------------------------------------------------


def verify_static_serving() -> None:
    print("\n[1/6] 静态页由后端托管（同源的物理前提）")
    status, body, headers = call(_new_opener(), "GET", "/login.html")
    check(status == 200, "GET /login.html 返回 200", f"实际 {status}")
    content_type = headers.get("content-type")
    check(
        "text/html" in content_type,
        "响应是 HTML",
        f"实际 content-type={content_type!r}",
    )
    # **不能写成 `if isinstance(body, str):`** —— 那样响应体异常时这两条判据
    # 会静默消失，脚本仍然"全过"。改为无条件执行，非字符串即判失败。
    body_text = body if isinstance(body, str) else ""
    check(
        bool(body_text) and "<html" in body_text.lower(),
        "响应体是 HTML 文档",
        f"响应体类型 {type(body).__name__}" if not body_text else "",
    )
    check(
        "member06@example.com" in body_text,
        "登录页仍含硬编码演示邮箱（种子第一用户与之对齐）",
    )


def verify_dev_login() -> None:
    print("\n[2/6] dev 登录旁路（无 Keycloak）")
    opener, status, payload, headers = login(ADMIN_EMAIL, as_admin=True)
    check(status == 201, "POST /api/v1/dev/auth/login 返回 201", f"实际 {status}")

    if not isinstance(payload, dict) or "data" not in payload:
        check(False, "响应含 data 段", str(payload)[:200])
        return

    data = payload["data"]
    check(bool(data.get("userId")), "返回 userId")
    check(data.get("role") == "ADMIN", "asAdmin=true 得到 ADMIN 角色", str(data.get("role")))
    check(bool(data.get("csrfToken")), "返回 csrfToken")
    check(bool(payload.get("meta", {}).get("requestId")), "meta 含 requestId")


def verify_cookie_attributes() -> None:
    print("\n[3/6] Cookie 属性分野（HttpOnly 与 JS 可读性）")
    _, status, _, headers = login(ADMIN_EMAIL, as_admin=True)
    check(status == 201, "登录成功", f"实际 {status}")

    lines = set_cookie_lines(headers)
    joined = " | ".join(lines)
    check("rednote_session" in joined, "下发 rednote_session")
    check("rednote_csrf" in joined, "下发 rednote_csrf")

    session_line = next((c for c in lines if c.startswith("rednote_session")), "")
    csrf_line = next((c for c in lines if c.startswith("rednote_csrf")), "")

    # **这三条不能用 `if session_line:` 包起来**：Cookie 缺失时它们会整条消失，
    # 判据总数从 36 缩到 34，而脚本仍然"全过" —— 防线静默缩水比防线报警更危险。
    # 因此改为无条件执行、缺失即判失败（空串进不去 httponly / samesite 分支）。
    check(
        bool(session_line) and "httponly" in session_line.lower(),
        "会话 Cookie 带 HttpOnly",
        "未找到会话 Cookie" if not session_line else "",
    )
    check(
        bool(session_line) and "samesite=lax" in session_line.lower(),
        "会话 Cookie SameSite=Lax",
        "未找到会话 Cookie" if not session_line else "",
    )
    check(
        bool(csrf_line) and "httponly" not in csrf_line.lower(),
        "CSRF Cookie **不**带 HttpOnly（双提交模式要求 JS 可读）",
        "未找到 CSRF Cookie" if not csrf_line else "",
    )


def verify_business_data() -> int:
    print("\n[4/6] 带会话读业务数据（种子 8 个项目）")
    opener, status, _, _ = login(ADMIN_EMAIL, as_admin=True)
    check(status == 201, "管理员登录成功")

    status, payload, _ = call(opener, "GET", "/api/v1/me")
    check(status == 200, "GET /api/v1/me 返回 200", f"实际 {status}")
    # 同样不能加 `if ...:` 守卫 —— 条件不成立时判据会消失而非失败。
    me = payload.get("data", {}) if isinstance(payload, dict) else {}
    check(me.get("role") == "ADMIN", "/me 报出 ADMIN 角色", str(me.get("role")))
    check(
        "capabilities" in me,
        "/me 含 capabilities 段（前端据此渲染能力开关）",
    )

    status, payload, _ = call(opener, "GET", "/api/v1/projects?limit=50")
    check(status == 200, "GET /api/v1/projects 返回 200", f"实际 {status}")

    items = payload.get("data", []) if isinstance(payload, dict) else []
    check(
        len(items) == EXPECTED_PROJECT_COUNT,
        f"ADMIN 看到 {EXPECTED_PROJECT_COUNT} 个项目",
        f"实际 {len(items)} 条",
    )
    # 分页形状：/projects 用 nextCursor（不是 nextAnchor）。同样无条件执行。
    page = payload.get("page", {}) if isinstance(payload, dict) else {}
    check("nextCursor" in page, "/projects 分页用 nextCursor", f"实际 page={page}")

    # 编号必须是真实形态 PRJ-YYYYMM-XXXXXX，不是"恰好 17 个字符"。
    bad = [i.get("projectCode") for i in items if not is_valid_project_code(i.get("projectCode"))]
    check(not bad, "所有 projectCode 是 PRJ-YYYYMM-XXXXXX 形态", str(bad[:3]))

    # 容量字段是十进制字符串（协议 §1）
    return len(items)


def verify_permission_dimension() -> None:
    print("\n[5/6] 权限维度是真的（种子 project_members 不是摆设）")
    counts: dict[str, int] = {}
    for email, name in OPERATOR_EMAILS:
        opener, status, payload, _ = login(email, as_admin=False)
        if status != 201:
            check(False, f"{name} 登录成功", f"实际 {status} {payload}")
            continue
        status, payload, _ = call(opener, "GET", "/api/v1/projects?limit=50")
        items = payload.get("data", []) if isinstance(payload, dict) else []
        counts[name] = len(items)
        check(
            0 < len(items) < EXPECTED_PROJECT_COUNT,
            f"{name} 只看到自己参与的项目（0 < n < {EXPECTED_PROJECT_COUNT}）",
            f"实际 {len(items)} 条",
        )

    # 各人可见集合必须**不同** —— 若全相等，说明过滤没按成员生效。
    # 这是本项最关键的判据：只看"少于 8"的话，一个"所有人都是同一批项目成员"
    # 的种子也能通过，但那不证明过滤逻辑对。
    # **无条件执行**：不加 `if len(counts) >= 2`，否则登录全失败时这条判据
    # 会静默消失（判据数为 0 时"各不相同"无从谈起，恰好是最该报错的情形）。
    check(
        len(counts) == len(OPERATOR_EMAILS) and len(set(counts.values())) >= 2,
        "不同操作员看到不同数量（说明按成员过滤，不是一刀切）",
        f"各人条数 {counts}（应 {len(OPERATOR_EMAILS)} 人且数量不全相等）",
    )


def verify_csrf_defenses() -> None:
    print("\n[6/6] CSRF 三道防线（写请求，非登录）")

    # 6.1 跨源 Origin 必须被拒
    for label, origin in [
        ("localhost（主机名不同）", "http://localhost:8899"),
        ("其它端口 5173", "http://127.0.0.1:5173"),
        ("外部站点", "https://evil.example"),
    ]:
        opener, _, payload, _ = login(ADMIN_EMAIL, as_admin=True)
        status, resp, _ = call(
            opener,
            "POST",
            "/api/v1/projects",
            body={"name": "probe"},
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": "probe",
                "Origin": origin,
            },
        )
        check(
            status == 403 and error_reason(resp) == "origin_mismatch",
            f"跨源被拒：{label}",
            f"实际 {status} {error_code(resp)}/{error_reason(resp)}",
        )

    # 6.2 缺来源头 / 缺令牌 / 令牌错，三个独立子判据
    opener, _, payload, _ = login(ADMIN_EMAIL, as_admin=True)
    csrf = payload["data"]["csrfToken"]

    status, resp, _ = call(
        opener,
        "POST",
        "/api/v1/projects",
        body={"name": "probe"},
        headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
    )
    check(
        status == 403 and error_reason(resp) == "missing_origin_and_referer",
        "缺 Origin 与 Referer 被拒",
        f"实际 {status} {error_reason(resp)}",
    )

    status, resp, _ = call(
        opener,
        "POST",
        "/api/v1/projects",
        body={"name": "probe"},
        headers={"Content-Type": "application/json", "Origin": BASE},
    )
    check(
        status == 403 and error_reason(resp) == "missing_csrf_token",
        "缺 X-CSRF-Token 被拒",
        f"实际 {status} {error_reason(resp)}",
    )

    status, resp, _ = call(
        opener,
        "POST",
        "/api/v1/projects",
        body={"name": "probe"},
        headers={
            "Content-Type": "application/json",
            "Origin": BASE,
            "X-CSRF-Token": "A" * 43,
        },
    )
    check(
        status == 403 and error_reason(resp) == "csrf_token_mismatch",
        "伪造令牌被拒",
        f"实际 {status} {error_reason(resp)}",
    )

    # 6.3 对照：全条件齐备时必须成功。
    # **缺了这条，上面四个 403 无法排除"端点是坏的"这一解释。**
    # 因此这里必须真发一个合法写请求并断言成功。
    opener, _, payload, _ = login(ADMIN_EMAIL, as_admin=True)
    csrf = payload["data"]["csrfToken"]
    status, listing, _ = call(opener, "GET", "/api/v1/projects?limit=1")
    sample = listing["data"][0]
    marker = f"{PROBE_PROJECT_PREFIX}{datetime.now(timezone.utc):%H%M%S}"
    status, resp, _ = call(
        opener,
        "POST",
        "/api/v1/projects",
        body={
            "name": marker,
            "brand_id": sample["brandId"],
            "product_id": sample["productId"],
            "owner_id": sample["ownerId"],
            "target_delivery_count": 1,
            "due_date": "2026-12-31",
        },
        headers={
            "Content-Type": "application/json",
            "Origin": BASE,
            "X-CSRF-Token": csrf,
        },
    )
    created_ok = status == 201 and isinstance(resp, dict) and "data" in resp
    check(
        created_ok,
        "对照：条件齐备的合法写入成功（201）",
        f"实际 {status} {str(resp)[:160]}",
    )
    # **下面两条无条件执行**：创建失败时它们必须报"失败"，而不是消失。
    # 若包在 `if created_ok:` 里，创建失败会让判据数从 36 缩到 34，
    # 而脚本仍打印"34/34 全过" —— 这正是反向验证 2 抓到的形态。
    code = resp.get("data", {}).get("projectCode", "") if isinstance(resp, dict) else ""
    check(
        bool(code) and is_valid_project_code(code),
        "新建项目的编号符合 PRJ-YYYYMM-XXXXXX 形态",
        code or "未创建成功，无编号可校验",
    )
    # **清理必须真的执行**。第一版脚本在这里打印"需清理，见下"，
    # 而下面没有任何清理代码 —— 正是本仓 ISS-95 那类缺陷：
    # 声称有防线、防线不存在，且不报错。所以这里直接调用并断言结果。
    # 未创建成功时也调用一次（应删除 0 行），并断言"库里确实没有这个残留"。
    cleaned = cleanup_probe_project(marker)
    check(
        cleaned,
        "探测项目无残留（已清理）",
        "清理失败，演示库里会残留脏数据",
    )


def _run_cleanup(delete_sql: str, count_sql: str, params: dict) -> tuple[int, int] | None:
    """执行清理并复核残留，返回 (删除行数, 残留行数)。

    两个清理函数共用同一套 engine 生命周期，避免重复。
    失败时返回 `None` 并打印原因 —— 清理失败必须被看见，
    但它不该把整轮验证中断掉（其余判据的结果仍然有价值）。

    `count_sql` 与 `delete_sql` 显式成对传入（而不是用一张 dict 按语句查）：
    用语句字符串当 key 时，拼错一个字符就会 `KeyError`，
    而它会被这里的 `except Exception` 吞成"清理异常" —— 又一个静默失败。
    """
    try:
        engine = create_engine(_database_url())
        try:
            with engine.begin() as conn:
                deleted = conn.execute(text(delete_sql), params).rowcount
                remaining = conn.execute(text(count_sql), params).scalar()
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 - 清理失败要报出来，不能让异常中断整轮
        print(f"         （清理异常：{type(exc).__name__}: {exc}）")
        return None
    return deleted, remaining


def cleanup_sessions() -> int:
    """删除本脚本创建的会话行。返回实际删除行数。

    **为什么必须做**：`login()` 每调用一次建一个会话，六组验证累计十来次；
    跑一轮全量回归实测在演示库里留下**上百行** `user_sessions`。
    脚本"自己造的残留自己不收"是本仓反复禁止的形态。

    按 `_CREATED_SESSION_IDS` 精确删除，不做全表清空 ——
    手工调试留下的会话不该被这个脚本顺手抹掉。
    """
    if not _CREATED_SESSION_IDS:
        return 0
    result = _run_cleanup(
        "DELETE FROM user_sessions WHERE id::text = ANY(:ids)",
        "SELECT count(*) FROM user_sessions WHERE id::text = ANY(:ids)",
        {"ids": _CREATED_SESSION_IDS},
    )
    if result is None:
        return -1
    deleted, remaining = result
    print(
        f"         （清理本次会话：删除 {deleted} 行 / 共 {len(_CREATED_SESSION_IDS)} 个，"
        f"残留 {remaining} 行）"
    )
    # 会话清理不作为 check 计分（属于收尾动作而非验证项），
    # 但残留行数会打出来，异常时也不会静默。
    return deleted


def cleanup_probe_project(name: str) -> bool:
    """删除对照实验写入的探测项目。返回"库里已无该残留"。

    **为什么要直连数据库**：契约里没有"删除项目"端点（只有成员删除），
    所以只能落库清理。清理只按 `name` 精确匹配探测前缀，
    不会误伤种子数据 —— 种子项目名不含该前缀。

    `project_members` 是 `ON DELETE CASCADE`，随项目一并消失；
    实测的 20 张引用表中，探测项目只写入了这一张。

    **判据是 `remaining == 0` 而不是 `deleted == 1`**：
    对照实验若创建失败，本来就没什么可删（`deleted == 0`），
    用 `deleted == 1` 会报出"清理失败、库里会残留脏数据"这种与事实相反的结论。
    "无残留"才是这里真正要保证的不变量；创建失败另有一条判据负责。
    """
    params = {"name": name}
    result = _run_cleanup(
        "DELETE FROM projects WHERE name = :name",
        "SELECT count(*) FROM projects WHERE name = :name",
        params,
    )
    if result is None:
        return False
    deleted, remaining = result
    print(f"         （清理探测项目：删除 {deleted} 行，残留 {remaining} 行）")
    return remaining == 0


def _database_url() -> str:
    """脚本自身用于清理的 DSN，与 `start_server()` 传给子进程的保持一致。"""
    return os.environ.get(
        "REDNOTE_DATABASE_URL", "postgresql+psycopg://rednote@localhost:5432/rednote_dev"
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 78)
    print("G1 端到端活性验证")
    print(f"  后端目录 : {_BACKEND_DIR}")
    print(f"  静态目录 : {_STATIC_DIR}")
    print(f"  监听     : {BASE}")
    print("=" * 78)

    if not _STATIC_DIR.is_dir():
        print(f"致命：静态目录不存在 {_STATIC_DIR}")
        return 1

    if _port_in_use():
        print(f"致命：{PORT} 已被占用。请先停掉占用进程再跑本脚本。")
        return 1

    print("\n启动服务 ...")
    proc: subprocess.Popen | None = None
    try:
        proc = start_server()
        print(f"服务已就绪（pid={proc.pid}）")

        verify_static_serving()
        verify_dev_login()
        verify_cookie_attributes()
        verify_business_data()
        verify_permission_dimension()
        verify_csrf_defenses()

    except Exception as exc:  # noqa: BLE001 - 顶层兜底，保证 finally 执行
        print(f"\n致命错误：{type(exc).__name__}: {exc}")
        _FAILURES.append(f"脚本异常：{type(exc).__name__}")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            print("\n服务已停止")

        # 服务停了才清会话：服务在跑时删会话行不会有问题，
        # 但放在 finally 末尾能保证"无论中途成功还是异常都会执行"。
        cleanup_sessions()

    print("\n" + "=" * 78)

    # **判据总数护栏**：实际执行的判据数少于基线，说明有判据被条件守卫
    # 静默跳过了 —— 那种情况下"全过"是假的，必须报出来。
    # 放在汇总处（而不是某条 check 内部）是为了避免计数自指。
    if _CHECKS < EXPECTED_MIN_CHECKS:
        _FAILURES.append(
            f"判据静默跳过：实际执行 {_CHECKS} 条 < 基线 {EXPECTED_MIN_CHECKS} 条"
        )

    if _FAILURES:
        print(f"结果：{_CHECKS - len(_FAILURES)}/{_CHECKS} 通过，{len(_FAILURES)} 条失败")
        for item in _FAILURES:
            print(f"  - {item}")
        print("=" * 78)
        return 1

    print(f"结果：{_CHECKS}/{_CHECKS} 全部通过（基线 {EXPECTED_MIN_CHECKS}）")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
