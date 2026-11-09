"""身份领域存储模型（WP08）。

对应数据结构文档 02 §3 的 `users` 与 `user_sessions` 两张表。

**为什么 `user_sessions` 只存 token 摘要而不存原文**：
会话 Cookie 里的令牌是"持有即可用"的凭据。若数据库里存明文，
一次数据库只读泄露就等于所有在线会话被接管。存 SHA-256 摘要后，
即使库被读走也无法还原出可用令牌（攻击者拿摘要无法通过 Cookie 校验）。
这与密码存哈希是同一个道理 —— 只是这里的"密码"是随机令牌，无需加盐慢哈希。

**为什么 OIDC 令牌要加密而不是哈希**：
业务侧需要在令牌过期前用它去调 IdP 的 userinfo 或做令牌刷新，
因此必须能还原原文 → 用对称加密（Fernet）而非单向哈希。
加密密钥来自环境变量，不进版本库、不进日志。

**为什么 issuer + subject 是唯一键**：
同一个自然人可能在企业 IdP 里换过 subject（例如账号重建），
或企业同时接多个 IdP。用 (issuer, subject) 组合定位身份，
而不是用 email —— email 会变、可被回收重分配，不能作为身份主键。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.platform.base import Base, created_at_column, updated_at_column, uuid_pk

# 角色。公开接口用大写枚举（数据结构文档 02 §1）。
ROLE_ADMIN: Final[str] = "ADMIN"
ROLE_OPERATOR: Final[str] = "OPERATOR"
ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_OPERATOR)

# 账号状态。DISABLED 是"停用"：不能继续操作，但历史记录保留
# （需求 §2.4：已停用成员不能继续操作，历史记录保留）。
STATUS_ACTIVE: Final[str] = "ACTIVE"
STATUS_DISABLED: Final[str] = "DISABLED"
USER_STATUSES: Final[tuple[str, ...]] = (STATUS_ACTIVE, STATUS_DISABLED)

# 角色与状态的 CHECK 约束名。用命名常量而非内联字符串：
# 迁移文件要引用同一个名字，否则升级/降级会因名字不一致而失败。
_CK_USERS_ROLE: Final[str] = "role_valid"
_CK_USERS_STATUS: Final[str] = "status_valid"


class User(Base):
    """内部成员。

    **不存密码**（数据结构文档 02 §3 明确）。身份由企业 IdP 提供，
    本表只保存"这个 IdP 身份在本系统里是谁、什么角色、是否停用"。
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()

    # IdP 侧身份定位。issuer = 令牌签发方标识，subject = 该签发方下的用户标识。
    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)

    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=STATUS_ACTIVE
    )
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        # 身份唯一：同一 IdP 下同一 subject 只能对应一个本系统账号。
        UniqueConstraint("issuer", "subject", name="uq_users_issuer_subject"),
        CheckConstraint(
            "role IN ('ADMIN', 'OPERATOR')", name=_CK_USERS_ROLE
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')", name=_CK_USERS_STATUS
        ),
        # 停用必须有时间戳，反之未停用不应有时间戳 —— 防止"状态与时间打架"
        # 导致审计时无法判断停用究竟发生在何时。
        CheckConstraint(
            "(status = 'DISABLED' AND disabled_at IS NOT NULL) "
            "OR (status = 'ACTIVE' AND disabled_at IS NULL)",
            name="disabled_at_consistent_with_status",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅排障用
        # 不打印 issuer/subject：它们是外部身份标识，属于不该随手进日志的内容。
        return f"<User id={self.id} role={self.role} status={self.status}>"


class UserSession(Base):
    """会话。

    **只存令牌摘要**（见模块 docstring）。`csrf_secret_hash` 同理 ——
    CSRF 密钥虽然危害小于会话令牌，但没有理由明文存。
    """

    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # 会话令牌摘要（SHA-256 hex，64 字符）。唯一 —— 摘要碰撞在现实中不会发生，
    # 而唯一约束能在"生成器出错导致重复令牌"时立刻暴露。
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # CSRF 密钥摘要（SHA-256 hex）。
    csrf_secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # 加密后的 OIDC 令牌（Fernet 密文，Base64 文本）。
    # 可空：本地假 IdP 模式下没有真实令牌；企业接 Keycloak 时才有。
    encrypted_oidc_tokens: Mapped[str | None] = mapped_column(Text, nullable=True)

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_user_sessions_token_hash"),
        # 过期清理与"查某用户的有效会话"都按这两个条件过滤，
        # 单独建索引比复合索引更实用（两个查询各自只用到一个条件）。
        Index("ix_user_sessions_expires_at", "expires_at"),
        Index("ix_user_sessions_user_id", "user_id"),
        # 撤销时间与过期时间的关系：撤销时间不应晚于过期时间 ——
        # 晚于则意味着"已经过期的会话又被撤销"，属于逻辑错误，早暴露更好。
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at <= expires_at",
            name="revoked_before_expiry",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅排障用
        # 绝不打印 token_hash：虽然是摘要，但它仍是"可用于定位会话"的标识，
        # 且排障时不需要它 —— 用 session id 足够。
        return f"<UserSession id={self.id} user_id={self.user_id}>"


__all__ = [
    "ROLES",
    "ROLE_ADMIN",
    "ROLE_OPERATOR",
    "STATUS_ACTIVE",
    "STATUS_DISABLED",
    "USER_STATUSES",
    "User",
    "UserSession",
]