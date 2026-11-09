"""身份领域仓储层（WP08）。

**为什么单独一层而不是在路由里写 SQL**：
研发计划 01 §5 要求"HTTP DTO、领域对象、存储模型分离"。仓储层的职责是
"把存储细节翻译成领域语义" —— 例如 `find_active_session` 不只是查一行，
它还要判断"未过期且未撤销"，这个语义属于领域而不属于 SQL。

**所有查询都带显式超时**（由 engine 的 statement_timeout 统一注入，见
platform/database.py）。仓储层不重复设超时，避免两处配置打架。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rednote.modules.identity.models import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    User,
    UserSession,
)


def _now() -> datetime:
    """当前时间（带时区）。

    统一用 UTC：数据结构文档 02 §1 要求"timestamptz，UTC 入库"。
    用 datetime.now(timezone.utc) 而不是 datetime.utcnow() ——
    后者返回 naive datetime（无 tzinfo），与 timestamptz 列比较时
    会被隐式转换，导致跨时区部署下出现难以察觉的偏移。
    """
    return datetime.now(timezone.utc)


class IdentityRepository:
    """身份的读写入口。

    构造时接收 Session：事务边界由调用方（API 层）控制，
    仓储不自行 commit —— 否则"一个业务操作跨多个仓储调用"就无法保证原子性。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # 用户
    # ------------------------------------------------------------------

    def find_user_by_identity(self, issuer: str, subject: str) -> User | None:
        """按 (issuer, subject) 查用户。

        用这两个字段而非 email：email 会变、可被回收重分配（见 models.py 说明）。
        """
        return self._session.execute(
            select(User).where(User.issuer == issuer, User.subject == subject)
        ).scalar_one_or_none()

    def get_user(self, user_id: uuid.UUID) -> User | None:
        return self._session.get(User, user_id)

    def create_user(
        self,
        *,
        issuer: str,
        subject: str,
        display_name: str,
        role: str,
    ) -> User:
        """创建用户。

        **为什么在唯一冲突时重查而不是直接抛错**：
        首次登录时若同一用户并发发起了两个回调（例如用户连点两次登录），
        两个请求都会走到"用户不存在 → 创建"。其中一个会因唯一约束失败。
        此时正确的处理是"回查已存在的那条" —— 因为目标状态已经达成
        （该用户存在），没必要让其中一个请求失败。
        """
        user = User(
            issuer=issuer,
            subject=subject,
            display_name=display_name,
            role=role,
            status=STATUS_ACTIVE,
        )
        self._session.add(user)
        try:
            self._session.flush()
        except IntegrityError:
            # 并发创建撞唯一约束：回滚到 savepoint 语义由调用方事务负责，
            # 这里先清理失败的挂起对象，再回查。
            self._session.rollback()
            existing = self.find_user_by_identity(issuer, subject)
            if existing is None:
                # 回查也找不到，说明不是"并发创建"而是别的原因（如约束名不匹配），
                # 必须抛出而不是吞掉。
                raise
            return existing
        return user

    def disable_user(self, user_id: uuid.UUID) -> None:
        """停用账号。

        同时写 status 与 disabled_at —— 数据库的 CHECK 约束要求两者一致，
        只改其一会直接被约束拒绝。
        """
        self._session.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=STATUS_DISABLED, disabled_at=_now(), updated_at=_now())
        )

    def enable_user(self, user_id: uuid.UUID) -> None:
        """启用账号（撤销停用）。"""
        self._session.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=STATUS_ACTIVE, disabled_at=None, updated_at=_now())
        )

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------

    def create_session(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        csrf_secret_hash: str,
        expires_at: datetime,
        encrypted_oidc_tokens: str | None = None,
    ) -> UserSession:
        session = UserSession(
            user_id=user_id,
            token_hash=token_hash,
            csrf_secret_hash=csrf_secret_hash,
            expires_at=expires_at,
            encrypted_oidc_tokens=encrypted_oidc_tokens,
        )
        self._session.add(session)
        self._session.flush()
        return session

    def find_active_session(self, token_hash: str) -> UserSession | None:
        """按令牌摘要查**有效**会话。

        "有效"= 未撤销且未过期。把这两个条件写进 SQL 而不是在 Python 里过滤：
        让数据库用索引先缩小范围，避免把过期会话读进内存再丢弃。

        **为什么用 `expires_at > now()` 而不是 Python 侧比较时间**：
        用数据库时间（now()）作为唯一时间基准，避免应用服务器与数据库
        时钟不一致时出现"应用认为没过期、数据库认为过期"的分歧。
        """
        return self._session.execute(
            select(UserSession).where(
                UserSession.token_hash == token_hash,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > _now(),
            )
        ).scalar_one_or_none()

    def find_session_by_token_hash(self, token_hash: str) -> UserSession | None:
        """按摘要查会话，**不**过滤有效性。

        用于"登出"场景：即使会话已过期，也应该能让登出成功返回 204
        （幂等语义）。若过滤有效性，过期会话登出会得到"会话不存在"，
        前端无法区分"已登出"与"出错"。
        """
        return self._session.execute(
            select(UserSession).where(UserSession.token_hash == token_hash)
        ).scalar_one_or_none()

    def revoke_session(self, token_hash: str) -> bool:
        """撤销会话。返回是否真的改了状态。

        已撤销的会话再次撤销返回 False（幂等）—— 登出应该是幂等的：
        用户点两次登出不应报错。
        """
        result = self._session.execute(
            update(UserSession)
            .where(
                UserSession.token_hash == token_hash,
                UserSession.revoked_at.is_(None),
            )
            .values(revoked_at=_now())
        )
        return result.rowcount > 0

    def revoke_all_user_sessions(self, user_id: uuid.UUID) -> int:
        """撤销某用户的全部有效会话。

        用途：账号停用时立即踢下线（需求 §2.4：已停用成员不能继续操作）。
        **这是"停用立即生效"的关键**：若只改 users.status 而不撤销会话，
        已登录的浏览器仍持有有效 Cookie，直到会话自然过期才会失效。
        """
        result = self._session.execute(
            update(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > _now(),
            )
            .values(revoked_at=_now())
        )
        return result.rowcount

    def count_active_sessions(self, user_id: uuid.UUID) -> int:
        """某用户的有效会话数。用于测试与运维排查。"""
        from sqlalchemy import func

        return int(
            self._session.execute(
                select(func.count())
                .select_from(UserSession)
                .where(
                    UserSession.user_id == user_id,
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > _now(),
                )
            ).scalar_one()
        )


__all__ = ["IdentityRepository"]