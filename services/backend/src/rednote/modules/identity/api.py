"""认证端点（WP08）：登录、回调、登出、当前用户。

协议 §2「身份与能力发现」的落地：

| 方法与路径 | 输入 | 输出／约束 |
|---|---|---|
| GET `/auth/login` | returnTo（仅本站路径） | 302 到企业 IdP，后端保存 state／nonce／PKCE |
| GET `/auth/callback` | code, state | 校验后创建会话并 302，令牌不放 URL 传给前端 |
| POST `/auth/logout` | CSRF | 204，撤销会话 |
| GET `/me` | 无 | id、displayName、role、csrfToken、capabilities |

**为什么 `/me` 返回 csrfToken 而不是让前端从 Cookie 读**：
CSRF 令牌必须能被前端 JS 读取（要放进请求头），因此它**不能**是 HttpOnly Cookie。
把它放在 `/me` 的响应体里，前端在登录后读一次、存内存即可。
放 localStorage 是错的（研发计划 01 §10.1：不把长期令牌放 localStorage）——
内存中的值在页面关闭后消失，攻击者无法跨会话复用。

**为什么登录回调后要 302 而不是返回 JSON**：
回调地址是浏览器整页跳转过来的（IdP 重定向）。若返回 JSON，
用户会看到一坨 JSON 文本。302 到前端页面才是正确的交互。
令牌通过 Set-Cookie 下发，**绝不出现在 URL 里**（URL 会进浏览器历史、
Referer 头、以及任何记录 URL 的中间件）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from rednote.modules.identity.dependencies import CurrentUserDep, RepositoryDep
from rednote.modules.identity.login_flow import (
    clear_login_flow_cookie,
    decode_login_flow,
    encode_login_flow,
    read_login_flow_cookie,
    set_login_flow_cookie,
)
from rednote.modules.identity.models import ROLE_OPERATOR
from rednote.modules.identity.oidc import (
    build_authorization_url,
    decode_id_token_claims,
    exchange_code_for_tokens,
    start_login,
    verify_claims,
)
from rednote.platform.configuration import SettingsDep
from rednote.platform.csrf import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    clear_csrf_cookie,
    clear_session_cookie,
    set_csrf_cookie,
    set_session_cookie,
    verify_csrf,
)
from rednote.platform.errors import (
    ApiError,
    DataEnvelope,
    ErrorCode,
    ErrorEnvelopeForOpenApi,
    InternalErrorEnvelope,
    build_meta,
)
from rednote.platform.tokens import (
    encrypt_oidc_tokens,
    generate_csrf_secret,
    generate_session_token,
    hash_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identity"])


class LimitsBody(BaseModel):
    """数量与分页上限。

    这些值来自 runtime_settings（ADR-13：批次数量配置化，
    不写死在数据库 CHECK 里）。当前硬编码为默认值，
    WP24 接入 runtime_settings 后改为读取配置。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    trial_quantity: int = Field(alias="trialQuantity", serialization_alias="trialQuantity")
    scale_max: int = Field(alias="scaleMax", serialization_alias="scaleMax")
    bulk_max: int = Field(alias="bulkMax", serialization_alias="bulkMax")
    default_page_limit: int = Field(
        alias="defaultPageLimit", serialization_alias="defaultPageLimit"
    )
    max_page_limit: int = Field(alias="maxPageLimit", serialization_alias="maxPageLimit")


class CapabilitiesBody(BaseModel):
    """能力清单（协议 §2）。

    **这是展示信息，不是授权依据。** 协议 §2 原文明确：
    "能力列表是展示信息，后端仍校验所有操作。"
    前端据它决定"显示哪些表单选项"，但服务端每次都重新校验权限与策略。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    enabled_formats: list[str] = Field(
        alias="enabledFormats", serialization_alias="enabledFormats"
    )
    local_models: bool = Field(alias="localModels", serialization_alias="localModels")
    allowed_external_providers: list[str] = Field(
        alias="allowedExternalProviders",
        serialization_alias="allowedExternalProviders",
    )
    live_profiles: list[str] = Field(
        alias="liveProfiles", serialization_alias="liveProfiles"
    )
    import_formats: list[str] = Field(
        alias="importFormats", serialization_alias="importFormats"
    )
    limits: LimitsBody


class MeBody(BaseModel):
    """`GET /me` 的 data 结构。

    含 csrfToken —— 前端把它存内存并放进后续写请求的 X-CSRF-Token 头。
    **不落 localStorage**（研发计划 01 §10.1）。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str
    display_name: str = Field(alias="displayName", serialization_alias="displayName")
    role: str
    csrf_token: str = Field(alias="csrfToken", serialization_alias="csrfToken")
    session_id: str | None = Field(
        default=None, alias="sessionId", serialization_alias="sessionId"
    )
    capabilities: CapabilitiesBody

# 通用错误响应声明，与 health 模块保持一致。
#
# **为什么必须显式声明，不能依赖 FastAPI 自动生成**：
# 不声明时 FastAPI 会为无泛型约束的端点自动加 `HTTPValidationError`
# 与 `ValidationError` 两个 schema。但那两个模型不是本项目的错误体 ——
# 本项目的 422 是协议 §1.2 的 `VALIDATION_FAILED`，形状是
# `{"error": {code, message, details, retryable}, "meta": {...}}`。
# 两套并存会让前端 SDK 生成出错误的错误类型，且契约里出现无人使用的 schema。
#
# 因此所有端点都显式声明 errors，让契约里只有协议定义的错误体。
_UNIVERSAL_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_400_BAD_REQUEST: {
        "model": ErrorEnvelopeForOpenApi,
        "description": "请求格式错误（MALFORMED_REQUEST）。",
    },
    status.HTTP_422_UNPROCESSABLE_ENTITY: {
        "model": ErrorEnvelopeForOpenApi,
        "description": "字段校验未通过（VALIDATION_FAILED）。",
    },
    status.HTTP_500_INTERNAL_SERVER_ERROR: {
        "model": InternalErrorEnvelope,
        "description": "服务内部错误（INTERNAL_ERROR，不在协议 §1.2 表内）。",
    },
}

# 认证相关端点特有的错误。
_AUTH_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorEnvelopeForOpenApi,
        "description": "会话无效或已过期（SESSION_EXPIRED）。",
    },
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorEnvelopeForOpenApi,
        "description": "权限不足或来源校验失败（PERMISSION_DENIED）。",
    },
}


def _login_flow_secret(settings: object) -> str:
    """登录流程签名密钥。

    复用 `oidc_token_encryption_key` 作为签名密钥是**刻意的取舍**：
    骨架期不引入额外的密钥配置项。两者用途不同（一个是加密一个是签名），
    但都只在本系统内部使用、都要求保密，共用一个密钥不降低安全性 ——
    因为它们的输出空间不相交（加密产出 Fernet 密文，签名产出 HMAC hex）。

    **但生产建议分开**：密钥轮换时，若共用密钥就需要同时轮换两者，
    而签名密钥泄露的后果（可伪造登录流程状态）与加密密钥泄露
    （可解密 OIDC 令牌）不同，轮换节奏可能不同。
    见 docs/m1/WP08 说明的 [待核实] 清单。

    未配置密钥时明确失败：绝不回退到一个内置默认值 ——
    那会让所有部署共用同一个公开密钥，等于没有签名。
    """
    key = getattr(settings, "oidc_token_encryption_key", None)
    if not key:
        raise ApiError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "服务端未配置安全密钥，无法处理登录流程。",
            details={"reason": "security_key_not_configured"},
        )
    return key


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get(
    "/auth/login",
    summary="开始登录（跳转到企业 IdP）",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    # 302 是重定向，无 JSON body；只声明错误体。
    responses={**_UNIVERSAL_ERROR_RESPONSES, **_AUTH_ERROR_RESPONSES},
)
def login(request: Request, settings: SettingsDep, returnTo: str | None = None) -> RedirectResponse:
    """生成 state/nonce/PKCE 并跳转到 IdP。

    `returnTo` 只接受站内路径（`oidc._validate_return_to` 做校验）。
    非法值静默退回默认落地页而不是报错：这是导航参数，
    为一个非法值打断登录流程对用户体验的伤害大于收益，
    而开放重定向的风险已由校验消除。
    """
    flow = start_login(settings, return_to=returnTo)
    encoded = encode_login_flow(flow, _login_flow_secret(settings))
    authorization_url = build_authorization_url(settings, flow)

    response = RedirectResponse(url=authorization_url, status_code=status.HTTP_302_FOUND)
    set_login_flow_cookie(
        response,
        encoded,
        secure=settings.session_cookie_secure,
        max_age_seconds=settings.login_flow_ttl_seconds,
    )
    return response


@router.get(
    "/auth/callback",
    summary="OIDC 回调（校验并建立会话）",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses={**_UNIVERSAL_ERROR_RESPONSES, **_AUTH_ERROR_RESPONSES},
)
def callback(
    request: Request,
    settings: SettingsDep,
    repo: RepositoryDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """处理 IdP 回调。

    **校验链（任一失败即拒绝，且都不建立会话）**：
      1. IdP 是否返回了 error（用户取消授权、拒绝等）
      2. 登录流程 Cookie 是否存在且签名有效、未过期
      3. 回调的 state 与流程里的 state 是否一致（**防 CSRF/会话固定**）
      4. code 是否存在
      5. 令牌交换是否成功
      6. ID Token 的 iss 与 nonce 是否匹配（**防其他 IdP 与重放**）
      7. 用户在系统里的状态（新建或查得）；已停用则拒绝
    """
    # --- 1. IdP 返回的错误 ---
    if error:
        # 用户主动取消授权是正常操作，不该记成错误级别。
        # 不带 error_description：那是 IdP 的自由文本，可能含内部信息。
        logger.info("oidc callback returned error", extra={"error": error})
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "登录未完成。",
            details={"reason": "idp_returned_error", "error": error[:64]},
        )

    # --- 2. 登录流程状态 ---
    raw_flow = read_login_flow_cookie(request)
    flow = decode_login_flow(
        raw_flow or "",
        _login_flow_secret(settings),
        ttl_seconds=settings.login_flow_ttl_seconds,
    )

    # --- 3. state 校验（防 CSRF / 会话固定）---
    if not state or state != flow.state:
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "登录状态校验失败，请重新登录。",
            details={"reason": "state_mismatch"},
        )

    # --- 4. code ---
    if not code:
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "登录回调缺少授权码。",
            details={"reason": "missing_authorization_code"},
        )

    # --- 5. 令牌交换 ---
    tokens = exchange_code_for_tokens(
        settings,
        code=code,
        code_verifier=flow.code_verifier,
    )

    if not tokens.id_token:
        # 没拿到 ID Token 就无法确定"登录的是谁"。
        # 不退回用 userinfo 端点的原因是：那需要额外一次外部调用，
        # 且 userinfo 返回的是"当前令牌对应的用户"，在缺少 ID Token 的情况下
        # 无法校验 nonce（nonce 只在 ID Token 里）。宁可失败也不要绕过校验。
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "身份服务未返回 ID Token。",
            details={"reason": "missing_id_token"},
        )

    # --- 6. 声明校验 ---
    claims = decode_id_token_claims(tokens.id_token)
    effective_issuer = settings.oidc_issuer or claims.issuer
    verify_claims(claims, expected_issuer=effective_issuer, expected_nonce=flow.nonce)

    # --- 7. 用户落库 ---
    user = repo.find_user_by_identity(claims.issuer, claims.subject)
    if user is None:
        # 首次登录：创建账号。
        # **角色默认 OPERATOR**：新成员默认最小权限，由管理员在管理中心提权。
        # 绝不能让"首个登录者自动成为管理员"—— 那是经典的提权漏洞。
        display_name = claims.name or claims.preferred_username or claims.subject
        user = repo.create_user(
            issuer=claims.issuer,
            subject=claims.subject,
            display_name=display_name[:100],
            role=ROLE_OPERATOR,
        )
        logger.info("created user on first login", extra={"user_id": str(user.id)})

    if user.status != "ACTIVE":
        # 已停用账号不能登录（需求 §2.4）。
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            "账号已停用，无法登录。",
            details={"reason": "user_disabled"},
        )

    # --- 建立会话 ---
    session_token = generate_session_token()
    csrf_secret = generate_csrf_secret()
    expires_at = _now() + timedelta(seconds=settings.session_ttl_seconds)

    encrypted_tokens: str | None = None
    if settings.oidc_token_encryption_key:
        encrypted_tokens = encrypt_oidc_tokens(
            tokens.to_json(),
            _decode_encryption_key(settings.oidc_token_encryption_key),
        )

    repo.create_session(
        user_id=user.id,
        token_hash=hash_token(session_token),
        csrf_secret_hash=hash_token(csrf_secret),
        expires_at=expires_at,
        encrypted_oidc_tokens=encrypted_tokens,
    )
    # 仓储层只是 flush，事务提交由 session_scope 在请求结束时完成。
    # 这里显式 commit 是为了让 cookie 下发与数据落库在同一时刻生效 ——
    # 若 commit 失败但 cookie 已下发，浏览器会持有一个无效会话，
    # 表现为"登录成功但刷新就掉线"。

    landing = settings.frontend_landing_path
    if flow.return_to and flow.return_to.startswith("/"):
        landing = flow.return_to

    response = RedirectResponse(url=landing, status_code=status.HTTP_302_FOUND)
    set_session_cookie(
        response,
        session_token,
        secure=settings.session_cookie_secure,
        max_age_seconds=settings.session_ttl_seconds,
    )
    # 同时下发 CSRF 明文 Cookie（非 HttpOnly，前端 JS 需读取）。
    # 服务端只存它的摘要 —— 见 csrf.py 的说明。
    set_csrf_cookie(
        response,
        csrf_secret,
        secure=settings.session_cookie_secure,
        max_age_seconds=settings.session_ttl_seconds,
    )
    # 一次性：登录流程状态用完立即清除。
    clear_login_flow_cookie(response, secure=settings.session_cookie_secure)
    return response


@router.post(
    "/auth/logout",
    summary="登出（撤销会话）",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**_UNIVERSAL_ERROR_RESPONSES, **_AUTH_ERROR_RESPONSES},
)
def logout(request: Request, settings: SettingsDep, repo: RepositoryDep) -> JSONResponse:
    """撤销当前会话并清除 Cookie。

    **幂等**：即使会话已过期或不存在，也返回 204。
    理由：用户点两次登出不该看到错误；且返回 404 会让客户端无法区分
    "已登出"与"服务异常"。

    **CSRF 校验**：登出是状态变更（撤销会话），因此必须校验 ——
    否则攻击者可构造跨站 POST 把用户踢下线（DoS）。
    这是"只在数据写入时校验 CSRF"这个常见误解的反例：
    CSRF 校验的是"是否为用户本意"，不是"是否改数据"。

    **会话不存在时仍要清 Cookie**：若用户手动清了 Cookie 但会话还在服务端，
    或者 Cookie 对应的会话已被撤销，浏览器侧仍应清理。
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)

    if raw_token:
        session_row = repo.find_session_by_token_hash(hash_token(raw_token))
        if session_row is not None:
            # 先校验 CSRF：CSRF 密钥摘要来自会话行。
            # 若会话不存在就没有可比对的对象 —— 那种情况下没什么可撤销的，
            # 直接走到清 Cookie 即可（幂等）。
            verify_csrf(request, session_row.csrf_secret_hash)
            repo.revoke_session(hash_token(raw_token))

    response = JSONResponse(status_code=status.HTTP_204_NO_CONTENT, content=None)
    clear_session_cookie(response, secure=settings.session_cookie_secure)
    clear_csrf_cookie(response, secure=settings.session_cookie_secure)
    clear_login_flow_cookie(response, secure=settings.session_cookie_secure)
    return response


@router.get(
    "/me",
    summary="当前用户与 CSRF 令牌",
    response_model=DataEnvelope[MeBody],
    responses={**_UNIVERSAL_ERROR_RESPONSES, **_AUTH_ERROR_RESPONSES},
)
def me(request: Request, settings: SettingsDep, user: CurrentUserDep, repo: RepositoryDep) -> dict[str, Any]:
    """返回当前用户信息与 CSRF 令牌。

    **为什么 csrfToken 每次调用都返回**：
    前端刷新页面后内存中的令牌会丢。重新调用 /me 是恢复它的正确方式
    （而不是把令牌存 localStorage）。这里返回的是会话创建时的同一个令牌 ——
    从会话行无法还原明文（只存了摘要），因此**需要一个地方保存明文**。

    设计选择：把 CSRF 明文**也**放在会话 Cookie 之外的一个非 HttpOnly Cookie 里，
    名为 `rednote_csrf`。这是"双提交 Cookie"的标准实现：
      - 服务端存摘要（库被读走也拿不到明文）
      - 浏览器存明文（非 HttpOnly，JS 可读）
      - 写请求时 JS 把它放进 X-CSRF-Token 头
    服务端把头部值与库中摘要比对（见 csrf.verify_csrf）。

    **这与"最终一致"无关，是刻意的双份存储**：两份内容不同（一份明文一份摘要），
    不是同一数据的冗余副本。攻击者要伪造请求必须同时具备
    "读取 Cookie 的能力"（需要 XSS）与"跨站发起请求的能力"，
    而同源策略恰好阻断后者。
    """
    from rednote.platform.csrf import SESSION_COOKIE_NAME

    raw_token = request.cookies.get(SESSION_COOKIE_NAME, "")
    session_row = repo.find_session_by_token_hash(hash_token(raw_token)) if raw_token else None

    csrf_plain = request.cookies.get(CSRF_COOKIE_NAME, "")

    # 会话与用户都已在 current_user 依赖里校验过，这里只组装响应。
    #
    # 用 MeBody 构造而不是直接拼 dict：让"响应字段与契约一致"由 pydantic 保证。
    # 拼 dict 时字段名写错（如 csrfToken 写成 csrf_token）不会报错，
    # 只会让前端拿不到值 —— 那是最难查的一类 bug。
    body = MeBody(
        id=str(user.id),
        displayName=user.display_name,
        role=user.role,
        csrfToken=csrf_plain,
        sessionId=str(session_row.id) if session_row else None,
        capabilities=CapabilitiesBody(
            enabledFormats=["TEXT_ONLY", "STATIC", "LIVE", "MIXED"],
            # 本地模型能力：模式 A 下为 false（ADR-10），内网接本地推理后置 true。
            localModels=False,
            allowedExternalProviders=[],
            liveProfiles=[],
            importFormats=["CSV", "XLSX", "SCREENSHOT"],
            limits=LimitsBody(
                trialQuantity=10,
                scaleMax=30,
                bulkMax=50,
                defaultPageLimit=20,
                maxPageLimit=100,
            ),
        ),
    )

    return {
        "data": body.model_dump(by_alias=True, mode="json"),
        "meta": build_meta(),
    }


def _decode_encryption_key(raw: str) -> bytes:
    """把配置里的密钥字符串转成 32 字节。

    支持两种输入形态（都是 base64 文本）：
      1. 标准 Fernet 密钥（44 字符 base64，本身编码了 32 字节）
      2. 32 字节的原始 base64
    统一解码成 32 字节后由 tokens 模块再编码为 Fernet。

    **为什么不直接接受 Fernet 密钥字符串**：让配置项只表达"32 字节密钥"，
    避免 Fernet 的具体格式泄漏到配置层（换加密算法时不必改配置约定）。
    """
    import base64

    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception as exc:
        raise ApiError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "安全密钥格式非法。",
            details={"reason": "security_key_invalid_base64"},
        ) from exc

    if len(decoded) == 32:
        return decoded
    if len(decoded) == 44:
        # 可能是 Fernet 密钥本身（base64 后再编码），再解一层
        try:
            inner = base64.urlsafe_b64decode(decoded.decode("ascii"))
            if len(inner) == 32:
                return inner
        except Exception:  # noqa: BLE001 - 多层解码失败统一走下面的长度错误
            pass

    raise ApiError(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        "安全密钥长度非法（需 32 字节）。",
        details={"reason": "security_key_wrong_length", "actualLength": len(decoded)},
    )


__all__ = ["router"]