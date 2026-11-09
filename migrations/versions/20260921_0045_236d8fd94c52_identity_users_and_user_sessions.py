"""identity: users and user_sessions

WP08 首个业务迁移。按数据结构文档 02 §3 建立身份领域两张表。

Revision ID: 236d8fd94c52
Revises: 77b9ab126cd3
Create Date: 2026-09-21

**本文件由 `--autogenerate` 生成后经人工审校修正。** 修正了三处自动生成的错误：

1. **删除了 `op.drop_table('smoke_meta')` 与 `op.drop_table('wp05_vec_probe')`。**
   autogenerate 看到数据库里存在"代码里没有对应模型"的表，就判定它们该被删。
   但那两张是 M0 冒烟验证的残留表，不属于本迁移的职责 ——
   一个 identity 迁移去删无关的表，是典型的"迁移越界"，
   在真实环境会直接造成数据丢失。

2. **重写了 downgrade 的业务顺序。** 自动生成的 downgrade 把"重建残留表"
   放在"删除 users/user_sessions"之前，且用 `sa.NullType()` 表示 vector 列 ——
   那是 autogenerate 无法推断类型时的占位，执行必然失败。
   重写后只做本迁移的逆操作：先删索引、再删表（子表在前，满足外键依赖）。

3. **给 `encrypted_oidc_tokens` 加了显式注释。** 该列存 Fernet 密文，
   密钥来自环境变量。迁移里不留任何密钥线索。

**downgrade 必须可执行**：WP47 要求备份恢复与回滚演练，
留 `NotImplementedError` 会让演练在第一步就失败。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "236d8fd94c52"
down_revision: str | Sequence[str] | None = "77b9ab126cd3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立 identity 领域：users 与 user_sessions。"""

    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # IdP 身份定位。用 (issuer, subject) 而非 email —— email 会变、可被回收重分配。
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default="ACTIVE", nullable=False
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # 状态与停用时间必须一致：防止"状态=ACTIVE 但有 disabled_at"
        # 这类矛盾数据让审计无法判断停用何时发生。
        sa.CheckConstraint(
            "(status = 'DISABLED' AND disabled_at IS NOT NULL) "
            "OR (status = 'ACTIVE' AND disabled_at IS NULL)",
            name=op.f("ck_users_disabled_at_consistent_with_status"),
        ),
        sa.CheckConstraint("role IN ('ADMIN', 'OPERATOR')", name=op.f("ck_users_role_valid")),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')", name=op.f("ck_users_status_valid")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("issuer", "subject", name="uq_users_issuer_subject"),
    )

    op.create_table(
        "user_sessions",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.UUID(), nullable=False),
        # 会话令牌摘要（SHA-256 hex）。存摘要而非明文：库被只读泄露也无法接管会话。
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        # CSRF 密钥摘要。同样不存明文。
        sa.Column("csrf_secret_hash", sa.String(length=64), nullable=False),
        # 加密后的 OIDC 令牌（Fernet 密文）。业务需还原后调 IdP，故用加密而非哈希。
        # 密钥只从环境变量读，不落库、不进日志。
        sa.Column("encrypted_oidc_tokens", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # 撤销时间不应晚于过期时间 —— 晚于意味着逻辑错误，早暴露更好。
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at <= expires_at",
            name=op.f("ck_user_sessions_revoked_before_expiry"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_sessions")),
        sa.UniqueConstraint("token_hash", name="uq_user_sessions_token_hash"),
    )

    # 过期清理任务与"查某用户有效会话"各用一个索引。
    op.create_index(
        "ix_user_sessions_expires_at", "user_sessions", ["expires_at"], unique=False
    )
    op.create_index(
        "ix_user_sessions_user_id", "user_sessions", ["user_id"], unique=False
    )


def downgrade() -> None:
    """回滚 identity 领域。

    顺序与 upgrade 严格相反：先删索引，再删子表（user_sessions），最后删父表（users）。
    反过来会因外键依赖而失败。

    **不重建任何"本迁移之前存在的其他表"。** autogenerate 曾试图重建 M0 冒烟残留表，
    那是越界 —— 本迁移只对 users/user_sessions 负责。
    """
    op.drop_index("ix_user_sessions_user_id", table_name="user_sessions")
    op.drop_index("ix_user_sessions_expires_at", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_table("users")