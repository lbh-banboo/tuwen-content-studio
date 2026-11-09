"""会话 Cookie 策略与 CSRF 校验（WP08）。

研发计划 01 §10.1 的原文要求，逐条落地：

> OIDC Authorization Code + PKCE，后端完成换取令牌，将令牌留服务端；
> 浏览器保存 Secure、HttpOnly、SameSite Cookie，写操作增加 CSRF 与 Origin 校验。
> 不把长期令牌放 localStorage。

**为什么令牌必须留服务端（而不是给浏览器一个 JWT）**：
浏览器持有 JWT 意味着"令牌一旦泄露，在过期前无法撤销"。而本系统要求
"账号停用立即使会话失效"（需求 §2.4）与"成员移除后 SSE 订阅与下载票据
需重新鉴权"（§10.1）。只有服务端持有会话状态，才能做到即时撤销 ——
每次请求查一次会话表，代价是一次主键索引查询，换来的是可撤销性。

**为什么 Cookie 属性缺一不可**：

| 属性 | 作用 | 漏掉的后果 |
|---|---|---|
| HttpOnly | JS 读不到 | XSS 可直接窃取会话令牌 |
| Secure | 只在 HTTPS 发送 | 明文网络下令牌被嗅探 |
| SameSite=Lax | 跨站请求不带 Cookie | CSRF 攻击面大幅扩大 |
| Path=/ | 全站可用 | 只作用于部分路径会导致某些接口拿不到会话 |

**关于 Secure 在开发环境的处理**：
本机是 http://127.0.0.1，Secure Cookie 不会被浏览器发送。因此按环境区分：
生产强制 Secure，开发/测试环境可关（由配置控制）。
**这不是"为了方便而降低安全"，而是"让开发环境能跑起来"** ——
如果开发环境也强制 Secure，开发者会倾向于全局关掉它，那才是真风险。
"""

from __future__ import annotations

from typing import Final

from fastapi import Request, Response
from starlette.datastructures import MutableHeaders

from rednote.platform.errors import ApiError, ErrorCode
from rednote.platform.tokens import constant_time_equals, hash_token

# Cookie 名。不带 __Host- 前缀是因为该前缀要求 Path=/ 且禁止 Domain 属性，
# 而内网部署可能需要按域名共享。用普通名字 + 显式属性更可控。
SESSION_COOKIE_NAME: Final[str] = "rednote_session"

# CSRF 令牌请求头名（协议 §1.1）。
CSRF_HEADER_NAME: Final[str] = "X-CSRF-Token"

# CSRF 明文 Cookie 名。
#
# **这个 Cookie 刻意不加 HttpOnly** —— 这是标准做法，不是疏漏。
# 双提交模式要求前端 JS 能读到明文令牌并放进请求头；加 HttpOnly 会让
# 前端读不到，模式无法工作。
# 读者常误以为"所有 Cookie 都该 HttpOnly"，但 HttpOnly 的目的是
# "防止 XSS 窃取可复用的长期凭据"。CSRF 令牌不是长期凭据：
#   - 它只在当前会话有效，且必须与 HttpOnly 的会话 Cookie 配合才有用；
#   - 攻击者即使读到它，还需能跨站发起请求（被同源策略阻断）。
# 真正需要保护的会话令牌在 SESSION_COOKIE_NAME 里，那个是 HttpOnly 的。
CSRF_COOKIE_NAME: Final[str] = "rednote_csrf"

# 写方法集合。GET/HEAD/OPTIONS 是安全方法，不需要 CSRF 校验
# （协议 §1：GET 请求无写副作用）。
_WRITE_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 会话 Cookie 有效期。8 小时覆盖一个工作日，避免运营中途被登出；
# 同时不至于长到"下班后电脑被他人使用仍有效"。
SESSION_MAX_AGE_SECONDS: Final[int] = 8 * 60 * 60


def set_session_cookie(
    response: Response,
    token: str,
    *,
    secure: bool,
    max_age_seconds: int = SESSION_MAX_AGE_SECONDS,
) -> None:
    """写入会话 Cookie。

    参数逐个显式传入而不读全局配置：让"这个 Cookie 是什么属性"
    在调用点可见，避免某处悄悄改了全局配置导致安全属性被削弱。
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age_seconds,
        # httponly=True 是硬要求，不提供关闭选项 —— 没有正当理由让 JS 读会话令牌。
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    """清除会话 Cookie。

    必须与设置时的属性一致（path/secure/samesite），否则浏览器会
    认为是"另一个 Cookie"而保留原值 —— 表现为"登出后仍是登录状态"。
    这是 Cookie 删除的经典坑。
    """
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def set_csrf_cookie(
    response: Response,
    csrf_plain: str,
    *,
    secure: bool,
    max_age_seconds: int = SESSION_MAX_AGE_SECONDS,
) -> None:
    """写入 CSRF 明文 Cookie。

    **不加 HttpOnly**（见 CSRF_COOKIE_NAME 的说明）：双提交模式要求
    前端 JS 能读到它。真正的会话令牌在另一个 HttpOnly Cookie 里。

    `samesite="lax"` 而非 `strict`：strict 会导致"从外部链接点进来时
    不带该 Cookie"，前端读不到令牌就无法发起写请求。
    lax 下跨站顶级导航（GET）会带 Cookie，而跨站的 POST 不会带 ——
    这正是我们要的：既让正常导航可用，又阻断跨站写请求。
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_plain,
        max_age=max_age_seconds,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_csrf_cookie(response: Response, *, secure: bool) -> None:
    """清除 CSRF Cookie。登出时与会话 Cookie 一起清。"""
    response.delete_cookie(
        key=CSRF_COOKIE_NAME,
        path="/",
        httponly=False,
        secure=secure,
        samesite="lax",
    )


def _origin_of(url: str) -> str | None:
    """提取 URL 的 origin（scheme://host[:port]）。

    用 urllib.parse 而不是字符串切分：字符串切分会在 IPv6 字面量
    （http://[::1]:8000）与带用户信息的 URL（http://u:p@h）上出错。
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        return None
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


def verify_csrf(request: Request, session_csrf_secret_hash: str) -> None:
    """校验 CSRF 令牌与 Origin。

    两道防线，任一不过即拒绝：

    **第一道：Origin / Referer 校验。**
    浏览器发跨站请求时会带 Origin 头，值是对端站点。同源请求的 Origin
    必须与本服务地址一致。为什么同时看 Referer：老浏览器或不带 Origin
    的场景（某些表单提交）只有 Referer。两者都没有时**拒绝**而不是放行 ——
    "没有来源信息"不应被解释为"同源"。

    **第二道：双提交令牌比对。**
    前端从 `/me` 拿到 csrfToken，写请求时放进 `X-CSRF-Token`。
    服务端对收到的令牌做**同样的 SHA-256**，再与会话表里存的摘要比对。

    为什么比对的是摘要而不是明文：会话表只存 `csrf_secret_hash`（见 models.py），
    服务端从未持有明文。这完全可行 —— 因为比对只需要"能验证相等"，
    不需要"能还原"。客户端持有明文，服务端持有摘要，两边各拿一半。

    为什么这能防 CSRF：攻击者的跨站请求受同源策略限制，
    **无法读取** csrfToken（它只在 /me 响应体里，而读取响应体需要跨域授权），
    因此无法伪造该请求头。

    **为什么两道都要**：
    - 只有 Origin 校验：某些代理会剥离 Origin 头，或同源子域被入侵；
    - 只有双提交：若服务端存在 XSS，攻击者可先读 /me 再伪造请求头。
    叠加后，攻击者需要同时突破"跨站"与"读取响应体"两个限制。
    """
    # --- 第一道：Origin / Referer ---
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")

    if origin is not None:
        # Origin 存在时必须与本服务的 host 匹配。
        request_origin = f"{request.url.scheme}://{request.url.netloc}"
        if _origin_of(origin) != _origin_of(request_origin):
            raise ApiError(
                ErrorCode.PERMISSION_DENIED,
                "请求来源不合法。",
                details={"reason": "origin_mismatch"},
            )
    elif referer is not None:
        request_origin = f"{request.url.scheme}://{request.url.netloc}"
        if _origin_of(referer) != _origin_of(request_origin):
            raise ApiError(
                ErrorCode.PERMISSION_DENIED,
                "请求来源不合法。",
                details={"reason": "referer_mismatch"},
            )
    else:
        # 两者都缺失：无法证明请求来自本站，拒绝。
        # 放行等于把"无来源信息"当成"可信"，这是 CSRF 防护的常见漏洞。
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            "缺少请求来源信息。",
            details={"reason": "missing_origin_and_referer"},
        )

    # --- 第二道：双提交令牌 ---
    provided = request.headers.get(CSRF_HEADER_NAME)
    if not provided:
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            "缺少 CSRF 令牌。",
            details={"reason": "missing_csrf_token"},
        )

    # 对客户端提供的令牌做同样的摘要，再与库中摘要比对。
    # 用恒定时间比较防时序侧信道。
    if not constant_time_equals(hash_token(provided), session_csrf_secret_hash):
        # 不区分"令牌错误"与"令牌不匹配"的细节，避免给攻击者反馈。
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            "CSRF 令牌无效。",
            details={"reason": "csrf_token_mismatch"},
        )


def is_write_method(method: str) -> bool:
    """是否为需要 CSRF 校验的写方法。"""
    return method.upper() in _WRITE_METHODS


def append_vary_cookie(headers: MutableHeaders) -> None:
    """给响应加 `Vary: Cookie`。

    为什么需要：响应内容（例如 /me 的 csrfToken）随会话变化。
    若中间缓存把 A 用户的响应返回给 B 用户，B 会拿到 A 的 CSRF 令牌，
    双提交校验随即失效。Vary: Cookie 告诉缓存"这个响应与会话相关"。
    内网环境通常没有共享缓存，但这是零成本的正确做法。
    """
    existing = headers.get("vary")
    if existing:
        if "cookie" not in existing.lower():
            headers["vary"] = f"{existing}, Cookie"
    else:
        headers["vary"] = "Cookie"


__all__ = [
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "SESSION_COOKIE_NAME",
    "SESSION_MAX_AGE_SECONDS",
    "append_vary_cookie",
    "clear_csrf_cookie",
    "clear_session_cookie",
    "is_write_method",
    "set_csrf_cookie",
    "set_session_cookie",
    "verify_csrf",
]