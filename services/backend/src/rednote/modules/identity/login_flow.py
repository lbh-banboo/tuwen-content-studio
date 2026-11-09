"""登录流程状态的服务端暂存与签名（WP08）。

**为什么需要这一层**：
授权跳转（`/auth/login`）与回调（`/auth/callback`）是两次独立 HTTP 请求。
state / nonce / code_verifier 必须跨这两次请求传递。

**为什么不直接放普通 Cookie**：
普通 Cookie 的内容可以被用户（或 XSS）改写。若 code_verifier 可被改写，
攻击者就能把 PKCE 的防护绕开（用自己的 verifier 配合自己的 challenge）。
因此内容必须**签名**：服务端能验证"这些值确实是我签发的且未被改动"。

**为什么不用服务端存储（Redis/表）**：
骨架期不引入额外组件。签名 Cookie 同样安全 —— 它保证了完整性与真实性；
一次性由"回调成功后清除 Cookie"保证，同时用签发时间限制有效期
（`login_flow_ttl_seconds`，默认 10 分钟），过期即使未被清除也无法使用。

**与 OIDC 适配器里 LoginFlowState 的关系**：
`oidc.LoginFlowState` 是纯数据结构（谁生成都一样）；
本模块负责它的**传输与完整性**。分开是为了让 OIDC 逻辑不依赖 HTTP 细节。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Final

from fastapi import Request, Response

from rednote.platform.errors import ApiError, ErrorCode
from rednote.platform.tokens import constant_time_equals
from rednote.modules.identity.oidc import LoginFlowState

# 登录流程 Cookie 名。与会话 Cookie 分开 —— 它们的生命周期与用途完全不同，
# 共用一个名字会导致"登录中途把已有会话覆盖掉"。
LOGIN_FLOW_COOKIE_NAME: Final[str] = "rednote_login_flow"

# 签名密钥的环境变量名由配置层提供。这里只声明用途常量，
# 避免把密钥读写散落在多个模块。
_SIGNATURE_ALGORITHM: Final[str] = "sha256"


def _sign(payload_b64: str, secret: str) -> str:
    """对 base64 载荷签名。

    HMAC-SHA256：既验证完整性（内容没被改），也验证真实性（确实是我签的）。
    普通哈希只能验完整性 —— 攻击者可改内容后重算哈希，普通哈希拦不住。
    """
    return hmac.new(
        secret.encode("utf-8"),
        payload_b64.encode("ascii"),
        getattr(hashlib, _SIGNATURE_ALGORITHM),
    ).hexdigest()


def encode_login_flow(flow: LoginFlowState, secret: str) -> str:
    """把登录流程状态编码成签名串：`base64(payload).signature`。

    载荷里含签发时间戳（`iat`），用于过期判断 —— 不能只依赖 Cookie 的
    max-age，因为攻击者可以手工构造一个不带过期的 Cookie 发过来。
    服务端必须自己能判断时效。
    """
    payload = {
        "state": flow.state,
        "nonce": flow.nonce,
        "verifier": flow.code_verifier,
        "return_to": flow.return_to,
        # 秒级时间戳。用于服务端侧的时效校验。
        "iat": int(time.time()),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def decode_login_flow(encoded: str, secret: str, *, ttl_seconds: int) -> LoginFlowState:
    """解码并校验登录流程状态。

    校验顺序刻意如此：**先验签名，再看内容**。
    若先解析内容再验签，畸形载荷会先进入 JSON 解析器，
    扩大了解析器的攻击面（恶意构造的深层嵌套 JSON 可致栈溢出）。
    """
    if not encoded:
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "登录流程状态缺失。",
            details={"reason": "login_flow_missing"},
        )

    payload_b64, separator, signature = encoded.rpartition(".")
    if not separator or not payload_b64 or not signature:
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "登录流程状态格式非法。",
            details={"reason": "login_flow_malformed"},
        )

    # 恒定时间比较签名：防时序侧信道推断签名值。
    if not constant_time_equals(signature, _sign(payload_b64, secret)):
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "登录流程状态签名无效。",
            details={"reason": "login_flow_signature_mismatch"},
        )

    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "登录流程状态无法解析。",
            details={"reason": "login_flow_decode_failed"},
        ) from exc

    if not isinstance(payload, dict):
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "登录流程状态结构非法。",
            details={"reason": "login_flow_not_object"},
        )

    issued_at = payload.get("iat")
    if not isinstance(issued_at, int):
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "登录流程状态缺少签发时间。",
            details={"reason": "login_flow_missing_iat"},
        )

    # 服务端侧时效校验。允许 60 秒负偏移以容忍轻微的时钟不同步
    # （负载均衡后的多台应用服务器之间），但不允许"未来时间"被无限制接受。
    age = int(time.time()) - issued_at
    if age > ttl_seconds:
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "登录流程已超时，请重新登录。",
            details={"reason": "login_flow_expired", "ageSeconds": age},
        )
    if age < -60:
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "登录流程时间戳异常。",
            details={"reason": "login_flow_future_timestamp"},
        )

    state = payload.get("state")
    nonce = payload.get("nonce")
    verifier = payload.get("verifier")
    return_to = payload.get("return_to")

    if not all(isinstance(value, str) and value for value in (state, nonce, verifier)):
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "登录流程状态字段不完整。",
            details={"reason": "login_flow_incomplete"},
        )

    return LoginFlowState(
        state=state,  # type: ignore[arg-type]
        nonce=nonce,  # type: ignore[arg-type]
        code_verifier=verifier,  # type: ignore[arg-type]
        return_to=return_to if isinstance(return_to, str) else "/",
    )


def set_login_flow_cookie(
    response: Response,
    encoded: str,
    *,
    secure: bool,
    max_age_seconds: int,
) -> None:
    """写入登录流程 Cookie。"""
    response.set_cookie(
        key=LOGIN_FLOW_COOKIE_NAME,
        value=encoded,
        max_age=max_age_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_login_flow_cookie(response: Response, *, secure: bool) -> None:
    """清除登录流程 Cookie（回调成功后立即调用）。

    一次性保证：即使攻击者截获了该 Cookie，回调完成后它已失效。
    """
    response.delete_cookie(
        key=LOGIN_FLOW_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def read_login_flow_cookie(request: Request) -> str | None:
    return request.cookies.get(LOGIN_FLOW_COOKIE_NAME)


__all__ = [
    "LOGIN_FLOW_COOKIE_NAME",
    "clear_login_flow_cookie",
    "decode_login_flow",
    "encode_login_flow",
    "read_login_flow_cookie",
    "set_login_flow_cookie",
]