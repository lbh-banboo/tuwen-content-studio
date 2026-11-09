"""OIDC 适配器：Authorization Code + PKCE（WP08）。

研发计划 01 §10.1：
> OIDC Authorization Code + PKCE，后端完成换取令牌，将令牌留服务端。

**为什么用 Authorization Code + PKCE 而不是 Implicit Flow**：
Implicit 把令牌放在 URL fragment 里返回，会进入浏览器历史、Referer 头、
以及任何记录 URL 的中间件。Authorization Code 只返回一次性 code，
后端用 code + code_verifier 换取令牌，令牌全程不经过浏览器。

**为什么公开客户端（public client）也要 PKCE**：
传统上 PKCE 是为移动端设计的（无法安全保存 client_secret）。
但服务端 Web 应用同样受益：若授权码在传输中被截获，
没有 code_verifier 就无法换取令牌。PKCE 把"授权码泄露"从"直接失守"
降级为"无害"。

**为什么必须校验 state 与 nonce**：

| 参数 | 防的攻击 | 不校验的后果 |
|---|---|---|
| state | CSRF（登录流程劫持） | 攻击者可诱导受害者用**攻击者的**授权码完成登录，使受害者会话绑定到攻击者账号（会话固定） |
| nonce | ID Token 重放 | 攻击者可重放一个旧 ID Token 冒充用户 |

这两个校验是 OIDC 安全的核心，**不是可选项**。测试必须覆盖"state 不匹配"与
"nonce 不匹配"两个反例。

**关于 IdP 适配器边界**：
本模块只依赖标准 OIDC 端点（authorization/token/userinfo），不依赖 Keycloak
特有 API。这样内网换成企业 IdP（如 Azure AD、Okta）时只需改配置端点，
不改代码。研发计划 01 §4 的"身份：企业 OIDC；无现成 IdP 时自托管 Keycloak"
正是这个意思。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode

import httpx

from rednote.platform.configuration import Settings
from rednote.platform.errors import ApiError, ErrorCode

# PKCE 的 code_verifier 长度。RFC 7636 规定 43—128 字符。
# 取 64 字节随机 → base64url 后约 86 字符，落在合法区间且熵充足。
_CODE_VERIFIER_BYTES: Final[int] = 64

# state / nonce 的熵。32 字节足够（它们生命周期只有几分钟）。
_STATE_BYTES: Final[int] = 32
_NONCE_BYTES: Final[int] = 32


class OidcError(ApiError):
    """OIDC 流程错误。

    继承 ApiError 以便直接被异常处理器收敛成协议错误体。
    用 401 SESSION_EXPIRED 而不是 500：登录流程失败属于"会话建立失败"，
    是客户端可理解、可重试的状态，不是服务端缺陷。
    """


@dataclass(frozen=True)
class LoginFlowState:
    """登录流程的暂存状态（在授权跳转与回调之间传递）。

    **为什么不把 state/nonce/code_verifier 放 Cookie**：
    它们体积不小（每个约 40—90 字符），三个加起来接近 300 字符，
    再叠加会话 Cookie 会逼近部分代理的 Cookie 头长度限制。
    更重要的是：放在服务端可以用一次性的存储实现"用过即焚"，
    而 Cookie 无法可靠实现一次性（用户可回退页面重放）。

    本实现把它们放在**签名的临时 Cookie** 里而不是服务端存储，
    原因是骨架期没有引入缓存/临时表，而这是可以安全做到的：
    Cookie 内容被 HMAC 签名，攻击者无法篡改 code_verifier 或 nonce。
    一次性由"回调成功后立即清除该 Cookie"保证。

    生产建议改为服务端存储（Redis/表），届时本类的字段不用变。
    """

    state: str
    nonce: str
    code_verifier: str
    return_to: str


def generate_state() -> str:
    return secrets.token_urlsafe(_STATE_BYTES)


def generate_nonce() -> str:
    return secrets.token_urlsafe(_NONCE_BYTES)


def generate_code_verifier() -> str:
    """生成 PKCE code_verifier（RFC 7636 §4.1）。"""
    return secrets.token_urlsafe(_CODE_VERIFIER_BYTES)


def compute_code_challenge(verifier: str) -> str:
    """计算 PKCE code_challenge（S256 方式）。

    S256 = BASE64URL(SHA256(verifier))，去掉填充。
    为什么用 S256 而不是 plain：plain 方式下 challenge 等于 verifier，
    一旦授权请求被记录（浏览器历史、代理日志），攻击者直接拿到 verifier。
    S256 使"看到 challenge"无法反推出 verifier。
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorization_url(settings: Settings, flow: LoginFlowState) -> str:
    """构造授权跳转 URL。"""
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_uri,
        "scope": "openid profile",
        "state": flow.state,
        "nonce": flow.nonce,
        "code_challenge": compute_code_challenge(flow.code_verifier),
        "code_challenge_method": "S256",
    }
    separator = "&" if "?" in settings.oidc_authorization_endpoint else "?"
    return f"{settings.oidc_authorization_endpoint}{separator}{urlencode(params)}"


def _validate_return_to(return_to: str | None, default_path: str) -> str:
    """校验 returnTo 只允许站内路径（协议 §2）。

    **为什么必须校验**：若不校验，攻击者可构造
    `?returnTo=https://evil.example/` 使登录成功后跳转到外部站点 ——
    这是开放重定向（Open Redirect），常用于钓鱼：
    链接看起来是本系统域名，跳转后却是伪造的登录页。

    校验规则（严格白名单式）：
      1. 必须以单个 `/` 开头（排除 `//evil.com` —— 它被浏览器解释为协议相对 URL）
      2. 不得包含 `\\`（某些浏览器把 `\\` 当 `/` 处理）
      3. 不得包含控制字符（防 header 注入与解析歧义）
    """
    if not return_to:
        return default_path
    if not return_to.startswith("/") or return_to.startswith("//"):
        return default_path
    if "\\" in return_to:
        return default_path
    if any(ord(ch) < 0x20 for ch in return_to):
        return default_path
    return return_to


def start_login(settings: Settings, return_to: str | None = None) -> LoginFlowState:
    """开始登录流程：生成 state/nonce/PKCE。"""
    return LoginFlowState(
        state=generate_state(),
        nonce=generate_nonce(),
        code_verifier=generate_code_verifier(),
        return_to=_validate_return_to(return_to, settings.frontend_landing_path),
    )


@dataclass(frozen=True)
class TokenSet:
    """令牌交换结果。"""

    access_token: str
    id_token: str | None
    refresh_token: str | None
    expires_in: int | None
    raw: dict[str, Any]

    def to_json(self) -> str:
        """序列化为 JSON 文本，供加密存储。

        用 JSON 打包而不是拆成多个数据库列：OIDC 令牌集合可能随 IdP
        增加字段（如 `scope`、`token_type`），打包后新增字段不需要改表结构。
        """
        return json.dumps(
            {
                "access_token": self.access_token,
                "id_token": self.id_token,
                "refresh_token": self.refresh_token,
                "expires_in": self.expires_in,
            },
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True)
class IdTokenClaims:
    """从 ID Token 里解析出的、本系统需要的声明。"""

    subject: str
    issuer: str
    nonce: str | None
    preferred_username: str | None
    name: str | None
    email: str | None


def decode_id_token_claims(id_token: str) -> IdTokenClaims:
    """解析 ID Token 的声明（**不验签**）。

    **重要安全说明 —— 为什么这里不验签，以及为什么这是可接受的**：

    ID Token 由后端在**直接**的 HTTPS 响应里从 IdP 收到（不是经过浏览器转发），
    因此传输信道本身已由 TLS 保护，不存在中间人篡改的窗口。
    这正是 OIDC 规范允许在 Authorization Code Flow 中"依赖 TLS 而不验签"
    的场景（OIDC Core §3.1.3.7 说明可通过 TLS 保护或验签，二选一）。

    但**验签在生产必须做**，理由：
      1. 防御"IdP 被入侵后签发伪造令牌"以外的配置错误（如误配了错误的
         token endpoint 指向一个攻击者可控的服务）；
      2. 满足企业安全审计要求（多数内网安全规范要求验签）。
    验签需要 JWKS 端点与密钥缓存，属于本工作包之外的工作量。

    **因此这是一个已记录的待办，不是被忽略的风险**：
    见 docs/m1/WP08 说明的 [待核实] 清单第 1 项。
    当前实现强制校验 `iss`（签发方）与 `nonce` —— 这两项能挡住
    "令牌来自其他 IdP"与"令牌重放"两类主要攻击，不依赖签名。

    解析用 base64 手工解码而不是引入 PyJWT：骨架期少一个依赖，
    且能显式控制"只取需要的字段"（避免把整个 claims 带进业务上下文）。
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        raise OidcError(
            ErrorCode.SESSION_EXPIRED,
            "ID Token 格式非法。",
            details={"reason": "id_token_malformed"},
        )

    payload_segment = parts[1]
    padded = payload_segment + "=" * (-len(payload_segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        claims = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise OidcError(
            ErrorCode.SESSION_EXPIRED,
            "ID Token 无法解析。",
            details={"reason": "id_token_decode_failed"},
        ) from exc

    if not isinstance(claims, dict):
        raise OidcError(
            ErrorCode.SESSION_EXPIRED,
            "ID Token 声明结构非法。",
            details={"reason": "id_token_claims_not_object"},
        )

    subject = claims.get("sub")
    issuer = claims.get("iss")
    if not isinstance(subject, str) or not subject:
        raise OidcError(
            ErrorCode.SESSION_EXPIRED,
            "ID Token 缺少 sub 声明。",
            details={"reason": "id_token_missing_sub"},
        )
    if not isinstance(issuer, str) or not issuer:
        raise OidcError(
            ErrorCode.SESSION_EXPIRED,
            "ID Token 缺少 iss 声明。",
            details={"reason": "id_token_missing_iss"},
        )

    return IdTokenClaims(
        subject=subject,
        issuer=issuer,
        nonce=claims.get("nonce") if isinstance(claims.get("nonce"), str) else None,
        preferred_username=(
            claims.get("preferred_username")
            if isinstance(claims.get("preferred_username"), str)
            else None
        ),
        name=claims.get("name") if isinstance(claims.get("name"), str) else None,
        email=claims.get("email") if isinstance(claims.get("email"), str) else None,
    )


def verify_claims(
    claims: IdTokenClaims,
    *,
    expected_issuer: str,
    expected_nonce: str,
) -> None:
    """校验 ID Token 的 iss 与 nonce。

    这两项不依赖签名，是当前实现里最重要的两道防线：
      - iss 不匹配 → 令牌来自其他 IdP（可能是攻击者自建的 IdP）
      - nonce 不匹配 → 令牌重放（把用户 A 的旧令牌塞给用户 B 的会话）

    用恒定时间比较 nonce 吗？不需要：nonce 不是长期秘密，
    它只在一次登录流程里有效，且攻击者要利用它必须先能篡改令牌内容
    （而令牌经 TLS 直达后端）。普通比较足够。
    """
    if claims.issuer != expected_issuer:
        raise OidcError(
            ErrorCode.SESSION_EXPIRED,
            "ID Token 签发方不匹配。",
            details={"reason": "issuer_mismatch"},
        )

    if claims.nonce is None:
        # 请求时带了 nonce，响应里必须有 —— 缺失说明 IdP 未按规范回传，
        # 无法确认这是"我们发起的这次登录"的响应。
        raise OidcError(
            ErrorCode.SESSION_EXPIRED,
            "ID Token 缺少 nonce 声明。",
            details={"reason": "nonce_missing"},
        )

    if claims.nonce != expected_nonce:
        raise OidcError(
            ErrorCode.SESSION_EXPIRED,
            "ID Token 的 nonce 不匹配，疑似重放。",
            details={"reason": "nonce_mismatch"},
        )


def exchange_code_for_tokens(
    settings: Settings,
    *,
    code: str,
    code_verifier: str,
) -> TokenSet:
    """用授权码换取令牌。

    **不重试**（配置项 `oidc_token_exchange_max_retries` 默认 0）：
    授权码是一次性的，第一次请求即使"响应丢失"也可能已被 IdP 消费。
    盲目重试会得到 `invalid_grant`，把"网络抖动"误报成"授权码无效"，
    反而掩盖真实原因。正确做法是让本次登录失败、用户重新发起 ——
    登录是低频操作，重试收益远小于误判成本。

    **超时显式**（C4）：OIDC 端点是外部依赖，配置 `oidc_http_timeout_seconds`。
    用 httpx.Timeout 分开设置连接与读取超时：连接阶段可能因网络问题慢，
    读取阶段则应该快速返回（令牌交换是轻量操作）。
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.oidc_redirect_uri,
        "client_id": settings.oidc_client_id,
        "code_verifier": code_verifier,
    }
    if settings.oidc_client_secret:
        # confidential client 才需要。PKCE 公开客户端不需要 secret。
        data["client_secret"] = settings.oidc_client_secret

    timeout = httpx.Timeout(
        settings.oidc_http_timeout_seconds,
        connect=settings.oidc_http_timeout_seconds,
    )

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                settings.oidc_token_endpoint,
                data=data,
                headers={"Accept": "application/json"},
            )
    except httpx.TimeoutException as exc:
        raise OidcError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "身份服务响应超时。",
            details={"dependency": "oidc", "reason": "timeout"},
        ) from exc
    except httpx.HTTPError as exc:
        raise OidcError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "无法连接身份服务。",
            # 只带异常类名，不带异常正文（可能含 URL 与内部地址）—— C6。
            details={"dependency": "oidc", "reason": type(exc).__name__},
        ) from exc

    if response.status_code != 200:
        # 不回显 IdP 的错误正文：它可能包含 client_id、内部端点等。
        # 只记状态码，供排障时定位是"授权码问题"还是"IdP 配置问题"。
        raise OidcError(
            ErrorCode.SESSION_EXPIRED,
            "身份服务拒绝了令牌交换请求。",
            details={"reason": "token_exchange_rejected", "status": response.status_code},
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise OidcError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "身份服务返回了非 JSON 响应。",
            details={"dependency": "oidc", "reason": "invalid_json"},
        ) from exc

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OidcError(
            ErrorCode.SESSION_EXPIRED,
            "身份服务未返回访问令牌。",
            details={"reason": "missing_access_token"},
        )

    expires_in = payload.get("expires_in")
    return TokenSet(
        access_token=access_token,
        id_token=payload.get("id_token") if isinstance(payload.get("id_token"), str) else None,
        refresh_token=(
            payload.get("refresh_token")
            if isinstance(payload.get("refresh_token"), str)
            else None
        ),
        expires_in=expires_in if isinstance(expires_in, int) else None,
        raw=payload,
    )


__all__ = [
    "IdTokenClaims",
    "LoginFlowState",
    "OidcError",
    "TokenSet",
    "build_authorization_url",
    "compute_code_challenge",
    "decode_id_token_claims",
    "exchange_code_for_tokens",
    "generate_code_verifier",
    "generate_nonce",
    "generate_state",
    "start_login",
    "verify_claims",
]