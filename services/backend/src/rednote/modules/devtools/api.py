"""开发期登录旁路（**仅 dev/test 环境**）。

## 它做什么

提供一个 `POST /api/v1/dev/auth/login`，用一个邮箱标识直接在本地建会话，
不需要企业 IdP。前端 Demo 的登录页调它，就能拿到真实的
`rednote_session` + `rednote_csrf` 两个 Cookie，后续所有业务端点照常鉴权。

## 它**不**做什么（这几条是刻意的，不是漏做）

| 不做的事 | 为什么 |
|---|---|
| 不自己造 Cookie | 若直接 `response.set_cookie("rednote_session", "dev-fake")`，会话表里没有对应行，`current_user` 第一步就查不到会话 —— 那才是"绕过了鉴权"。此处**必须**走 `repo.create_session()` |
| 不自己校验口令 | 本系统**不存密码**（`identity/models.py` 明确）。dev 旁路只按邮箱找/建用户，不做密码比对 —— 加一个假的密码校验会让人误以为"这里真的验了口令" |
| 不签发 OIDC 令牌 | 没有 IdP 就没有令牌。`encrypted_oidc_tokens` 留 None，与"本地假 IdP 模式"一致（该列本身可空） |
| **不自动启用被停用的账号** | 停用是运维的显式决定。若登录时静默改回 ACTIVE，则"停用账号不能登录"这条本该被验的行为就永远验不到 —— 而且失败方式是安静的。恢复演示账号请用 `seed_demo_data.py --reset`（吵闹） |

## 为什么复用 `identity` 的领域对象

`repo.create_session()` 要求 `token_hash` / `csrf_secret_hash` 两个摘要。
本模块用 `platform.tokens.hash_token()` 现算 —— 与 `identity/api.py` 的回调
用**同一个函数**。这样"dev 登录建出来的会话行"与"真实 IdP 登录建出来的"
在结构上逐字段一致，唯一区别是 issuer/subject 的取值。

**这是本旁路能被用来验收的前提**：如果会话行结构不同，
那么"dev 登录能通"就不能推出"真实登录能通"，G1 的演示就没有意义。

## issuer / subject 的取值

用固定的 `issuer="urn:rednote:dev"` + `subject=<email>`。
固定 issuer 而非复用 `settings.oidc_issuer`：让 dev 建的账号在库中
一眼可辨（`users.issuer` 这一列就能区分），避免与真实 IdP 账号混淆。
`users` 表有 `(issuer, subject)` 唯一约束，因此同一邮箱重复登录
会命中已有账号，不会重复建用户。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from rednote.modules.identity.dependencies import RepositoryDep
from rednote.modules.identity.models import (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    STATUS_ACTIVE,
)
from rednote.platform.api_responses import (
    ACCESS_ERROR_RESPONSES,
    UNIVERSAL_ERROR_RESPONSES,
)
from rednote.platform.configuration import SettingsDep
from rednote.platform.csrf import set_csrf_cookie, set_session_cookie
from rednote.platform.errors import (
    ApiError,
    DataEnvelope,
    ErrorCode,
    build_meta,
)
from rednote.platform.tokens import (
    generate_csrf_secret,
    generate_session_token,
    hash_token,
)

logger = logging.getLogger(__name__)

# dev 旁路允许生效的环境。
# 取值与 `main._DOCS_ENABLED_ENVIRONMENTS`（dev/test/staging）**刻意不同**：
# 不含 staging —— staging 属于预发布环境，可以开放交互式文档（运维面），
# 但绝不该开放免密登录。把两者分开写，是为了让这个差异在代码里可见。
DEV_LOGIN_ENVIRONMENTS: frozenset[str] = frozenset({"dev", "test"})

# dev 账号的 issuer 前缀。固定值让 dev 账号在 users 表里可辨。
DEV_ISSUER: str = "urn:rednote:dev"

router = APIRouter(tags=["devtools"])


class DevLoginBody(BaseModel):
    """dev 登录请求体。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    email: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(
        default=None, alias="displayName", serialization_alias="displayName"
    )
    # 是否以管理员身份建号。默认 false（最小权限），演示"管理者视图"时显式传 true。
    as_admin: bool = Field(
        default=False, alias="asAdmin", serialization_alias="asAdmin"
    )


class DevLoginResultBody(BaseModel):
    """dev 登录响应。

    字段刻意与 `identity.MeBody` 对齐（`displayName` / `role` / `csrfToken`），
    前端可复用同一套解析代码。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    user_id: str = Field(alias="userId", serialization_alias="userId")
    display_name: str = Field(alias="displayName", serialization_alias="displayName")
    role: str
    csrf_token: str = Field(alias="csrfToken", serialization_alias="csrfToken")
    session_id: str = Field(alias="sessionId", serialization_alias="sessionId")
    expires_at: str = Field(alias="expiresAt", serialization_alias="expiresAt")


def _assert_dev_environment(environment: str) -> None:
    """运行期环境门（第二道防线）。

    第一道在 `main.create_app`（装配期不注册本 router → 生产 404）。
    本函数是冗余的 —— 但必须有：一次重构或一个 merge 冲突解决失误
    就可能让 include 逃出环境判断，那时这道门是唯一的拦截点。

    返回 **404** 而不是 403：403 等于承认"这个端点存在但你没权限"，
    给攻击者一个可枚举的信号。404 与"路径拼错了"无法区分。
    """
    if environment not in DEV_LOGIN_ENVIRONMENTS:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "资源不存在。",
            details={"reason": "dev_endpoint_disabled"},
        )


@router.post(
    "/dev/auth/login",
    summary="【仅 dev/test】按邮箱直接建会话，跳过 IdP",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[DevLoginResultBody],
    responses={**UNIVERSAL_ERROR_RESPONSES, **ACCESS_ERROR_RESPONSES},
    # **刻意不进 OpenAPI 契约**（`contracts/openapi.json` 保持 48 paths）。
    #
    # 判据是「契约描述什么」：本契约是前后端协作的冻结接口面，
    # 而本端点是**开发期脚手架**，生产环境的路由表里根本不存在它。
    # 若把它写进契约，会有两个具体危害：
    #   1) 下游按契约生成客户端时，会生成一个生产必然 404 的调用入口；
    #   2) 契约失去「有什么就一定能调」这一可推理性 —— 读者无法从
    #      契约本身分辨哪些端点是产品的、哪些是脚手架的。
    # 保持 48 paths 不变，同时把「dev 下实际是 49」这件事记录在此，
    # 避免下一个人以为是漏导出（导出脚本会报 diff，那不是 bug）。
    #
    # 前端 `api.js` 调用本端点时没有 schema 可参考，字段以上面的
    # `DevLoginResultBody` 为准（与 `identity.MeBody` 刻意对齐）。
    include_in_schema=False,
)
def dev_login(
    payload: DevLoginBody,
    settings: SettingsDep,
    repo: RepositoryDep,
) -> JSONResponse:
    """开发期登录。**生产的 app 上不存在这个路径。**

    复用真实会话创建路径（`create_session` + `set_session_cookie` +
    `set_csrf_cookie`），使"dev 登录成功"能推出"真实登录链路可用"。

    **本端点不校验 CSRF**：它本身就在建立会话，此时还没有 CSRF 密钥可校验。
    这与 `/auth/login`（GET，302 到 IdP）的处境一致 —— 登录入口天然免 CSRF，
    因为"诱导受害者去登录一个账号"不构成越权（受害者登进的是自己的账号）。
    真实的风险是**会话固定**（诱导受害者用攻击者的会话），
    而本端点每次都新建会话 + 新 CSRF 密钥，不存在可被固定的既有会话。
    """
    _assert_dev_environment(settings.environment)

    email = payload.email.strip().lower()
    if not email:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "邮箱不能为空。",
            details={"field": "email"},
        )

    role = ROLE_ADMIN if payload.as_admin else ROLE_OPERATOR
    display_name = (payload.display_name or email).strip()[:100]

    user = repo.find_user_by_identity(DEV_ISSUER, email)
    if user is None:
        user = repo.create_user(
            issuer=DEV_ISSUER,
            subject=email,
            display_name=display_name,
            role=role,
        )
        logger.info(
            "dev login created user", extra={"user_id": str(user.id), "email": email}
        )
    else:
        # 已存在账号：按本次请求对齐 display_name 与角色。
        # 这不是"静默提权"——角色只在显式传 asAdmin=true 时才提升，
        # 且只在 dev 环境且只对本功能的 dev issuer 账号生效。
        if display_name and user.display_name != display_name:
            user.display_name = display_name
        if role == ROLE_ADMIN and user.role != ROLE_ADMIN:
            user.role = ROLE_ADMIN
            logger.info("dev login promoted user", extra={"user_id": str(user.id)})

    if user.status != STATUS_ACTIVE:
        # 与真实登录**逐字段一致**：停用账号不能登录（需求 §2.4）。
        # 对照实现：`identity/api.py` 的回调在同一步骤抛同一个错误码与 reason。
        #
        # 这里**刻意不"自动启用"停用账号**（历史上曾这样写，已移除）。
        # 曾经的写法是 `if user.status != "ACTIVE": repo.enable_user(...)`，
        # 它有两个问题：
        #   1) 它让紧随其后的本判断**永不可达** —— 一个永远不会触发的防线，
        #      比没有防线更糟：读代码的人会以为"停用账号进不来"，而事实相反；
        #   2) 停用是运维的**显式决定**，被一次登录静默撤销，属于
        #      无告知的状态写入。撤销方向若错，没有任何信号。
        # 恢复被停用的演示账号有明确途径：`python tools/dev/seed_demo_data.py
        # --reset`（会重建 users 为 ACTIVE）。走那条路是**吵闹的**，
        # 而静默启用是**安静的** —— 失败模式应当选会吵的那个。
        #
        # 副作用（可接受）：dev 登录因此无法演示"停用后仍能登录"，
        # 但它本来就不该能 —— 那正是要验的行为。
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            "账号已停用，无法登录。",
            details={"reason": "user_disabled"},
        )

    session_token = generate_session_token()
    csrf_secret = generate_csrf_secret()
    expires_at = _now() + timedelta(seconds=settings.session_ttl_seconds)

    session_row = repo.create_session(
        user_id=user.id,
        token_hash=hash_token(session_token),
        csrf_secret_hash=hash_token(csrf_secret),
        expires_at=expires_at,
        # 无 IdP 即无令牌。该列可空，与"本地假 IdP 模式"一致。
        encrypted_oidc_tokens=None,
    )

    # 不在这里 commit。提交由 `session_scope` 在依赖清理阶段完成
    # （`identity/api.py` 的回调同样如此 —— 它的注释说"显式 commit"，
    # 但代码里没有该调用，两处实际都依赖 session_scope）。
    # 与既有路径保持一致比"在这里单独做对"更重要：若只有本端点显式
    # commit，就会出现"dev 登录的事务边界与真实登录不同"，
    # 而本旁路的全部价值就在于两条路径行为一致。

    body = DevLoginResultBody(
        userId=str(user.id),
        displayName=user.display_name,
        role=user.role,
        csrfToken=csrf_secret,
        sessionId=str(session_row.id),
        expiresAt=expires_at.isoformat(),
    )

    response = JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "data": body.model_dump(by_alias=True, mode="json"),
            "meta": build_meta(),
        },
    )
    set_session_cookie(
        response,
        session_token,
        secure=settings.session_cookie_secure,
        max_age_seconds=settings.session_ttl_seconds,
    )
    set_csrf_cookie(
        response,
        csrf_secret,
        secure=settings.session_cookie_secure,
        max_age_seconds=settings.session_ttl_seconds,
    )
    return response


def _now() -> datetime:
    """UTC 当前时间（带 tz），与身份模块一致。"""
    return datetime.now(timezone.utc)


__all__ = ["DEV_ISSUER", "DEV_LOGIN_ENVIRONMENTS", "router"]
