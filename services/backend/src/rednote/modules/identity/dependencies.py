"""认证依赖注入（WP08）。

**这是"停用立即失效"的落点。**

需求 §2.4：已停用成员不能继续操作。
研发计划 01 §10.1：账号停用立即使会话失效；项目成员移除后 SSE 订阅与下载票据
需重新鉴权。

**为什么每个请求都要查库，而不是信任 Cookie 里的信息**：
把角色/状态编码进 Cookie（或 JWT）会让鉴权变成"纯客户端验证"。
那样一旦管理员停用某账号，已登录浏览器在 Cookie 过期前仍可操作 ——
"立即失效"就退化成了"最多 8 小时后失效"。这是权限系统最典型的漏洞。

代价是每请求一次主键索引查询（会话表按 token_hash 唯一索引查一行），
在本项目 30 RPS 的规模下可以忽略。**用一次廉价查询换即时撤销能力，
这个交换是对的。**

**为什么不用缓存**：
缓存会话状态会重新引入"撤销延迟"，且需要一套失效通知（又多一个组件）。
骨架期不做缓存是最简单且正确的选择；若将来压测显示鉴权查询是瓶颈，
正确的做法是在缓存层订阅会话撤销事件，而不是简单地加 TTL。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from rednote.modules.identity.models import (
    ROLE_ADMIN,
    STATUS_ACTIVE,
)
from rednote.modules.identity.repository import IdentityRepository
from rednote.platform.configuration import get_app_settings
from rednote.platform.csrf import SESSION_COOKIE_NAME, is_write_method, verify_csrf
from rednote.platform.database import session_scope
from rednote.platform.errors import ApiError, ErrorCode
from rednote.platform.tokens import hash_token


@dataclass(frozen=True)
class CurrentUser:
    """已认证的当前用户。

    **为什么不直接返回 ORM 的 User 对象**：
    ORM 对象绑定在 session 上，session 关闭后访问属性会触发
    DetachedInstanceError。返回不可变的值对象后，业务代码可以自由持有它，
    不依赖 session 的生命周期。
    """

    id: uuid.UUID
    display_name: str
    role: str
    is_admin: bool
    # CSRF 密钥（明文）。来自会话表对应的明文值 —— 注意：
    # 表里存的是摘要，所以这里只能拿到"本次请求携带的密钥"用于比对，
    # 不能在服务端还原明文。见下方 `current_user` 的说明。
    session_id: uuid.UUID


def get_db_session(request: Request) -> Iterator[Session]:
    """请求级数据库 session。

    **关键：配置来自 `request.app.state.settings`，不是全局单例。**
    若这里调 `session_scope()`（无参 → 读全局 get_settings()），
    就会出现"app 用注入配置装配、session 却按环境变量连库"的两套真相。
    本机实测踩到过：测试注入 database_url 后，session 仍报"未配置"。

    **为什么用生成器而不是让业务自己管 session**：
    FastAPI 的依赖清理语义要求"yield 之前是建立、yield 之后是清理"。
    这里用 yield 让框架负责关闭；commit 由 session_scope 在正常路径完成，
    异常时 rollback 后重抛。
    """
    settings = get_app_settings(request.app)
    with session_scope(settings) as session:
        yield session


DBSession = Annotated[Session, Depends(get_db_session)]


def get_repository(db: DBSession) -> IdentityRepository:
    return IdentityRepository(db)


RepositoryDep = Annotated[IdentityRepository, Depends(get_repository)]


def current_user(request: Request, repo: RepositoryDep) -> CurrentUser:
    """解析当前登录用户，并对写请求强制 CSRF 校验。未登录或校验失败即抛错。

    校验链（任一失败即拒绝）：
      1. 会话 Cookie 存在
      2. 令牌摘要在会话表里能查到**有效**会话（未撤销、未过期）
      3. 对应用户存在
      4. 用户状态为 ACTIVE（**这是"停用立即失效"的关键一步**）
      5. **写方法（POST/PUT/PATCH/DELETE）额外校验 CSRF 与 Origin**

    **为什么 CSRF 校验挂在这里，而不是每个写端点各自声明一个依赖**：

    放在依赖里意味着"每个写端点都要记得加"—— 漏一个就是真实漏洞，
    而且漏掉的表现是"功能正常、测试也过"，只有专门的安全用例才会发现
    （WP10 实测：`verify_csrf` 当时只被 `/auth/logout` 调用，
    项目与字典的全部写端点**完全没有 CSRF 校验**）。

    这里做则结构上无法漏：所有需要登录的端点都必须经过本依赖，
    而写方法必然带会话（无会话时第 1 步就 401 了）。

    **代价为零**：会话行（`session_row`）本来就为鉴权查出来了，
    `csrf_secret_hash` 是同一行的字段，不产生额外查询；
    GET/HEAD/OPTIONS 由 `is_write_method` 直接短路，读请求无任何开销。

    **语义边界**：CSRF 校验的是"这个请求是否为用户本意"，不是"是否改数据"。
    因此它按 HTTP 方法判定，而不是按端点语义 —— 登出（不写业务数据但改变状态）
    同样需要校验（`/auth/logout` 因不走本依赖，自行调用 `verify_csrf`）。
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "未登录。",
            details={"reason": "no_session_cookie"},
        )

    token_digest = hash_token(raw_token)
    session_row = repo.find_active_session(token_digest)
    if session_row is None:
        # 不区分"会话不存在"与"已过期/已撤销"：对客户端而言处理方式相同
        # （重新登录），细分只会给攻击者提供"该令牌曾经有效"的信息。
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "会话已失效，请重新登录。",
            details={"reason": "session_invalid_or_expired"},
        )

    user = repo.get_user(session_row.user_id)
    if user is None:
        # 会话指向一个不存在的用户 —— 数据不一致，属于必须暴露的异常。
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "会话对应的账号不存在。",
            details={"reason": "session_user_missing"},
        )

    if user.status != STATUS_ACTIVE:
        # 停用立即失效的第二道保险。
        # 第一道是"停用时撤销全部会话"（revoke_all_user_sessions）；
        # 但如果停用操作恰好漏了撤销，这一道仍能挡住 —— 纵深防御。
        raise ApiError(
            ErrorCode.SESSION_EXPIRED,
            "账号已停用。",
            details={"reason": "user_disabled"},
        )

    # 第 5 步：写请求的 CSRF 与 Origin 校验（协议 §1.1）。
    # 放在最后一步 —— 前四步是"你是谁"，这一步才是"这是否你的本意"。
    # 顺序不可颠倒：对未认证的请求报 403 会泄露"该端点存在且鉴权已通过"的信息。
    if is_write_method(request.method):
        verify_csrf(request, session_row.csrf_secret_hash)

    return CurrentUser(
        id=user.id,
        display_name=user.display_name,
        role=user.role,
        is_admin=user.role == ROLE_ADMIN,
        session_id=session_row.id,
    )


CurrentUserDep = Annotated[CurrentUser, Depends(current_user)]


def require_admin(user: CurrentUserDep) -> CurrentUser:
    """要求管理员角色。

    权限检查必须在服务端做。前端隐藏菜单只是体验优化，
    不能作为权限边界（研发计划 01 §10.2：不能只在菜单隐藏管理入口）。
    """
    if not user.is_admin:
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            "该操作仅管理员可执行。",
            details={"requiredRole": ROLE_ADMIN},
        )
    return user


AdminDep = Annotated[CurrentUser, Depends(require_admin)]


__all__ = [
    "AdminDep",
    "CurrentUser",
    "CurrentUserDep",
    "DBSession",
    "RepositoryDep",
    "current_user",
    "get_db_session",
    "get_repository",
    "require_admin",
]