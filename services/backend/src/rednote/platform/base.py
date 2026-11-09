"""SQLAlchemy 声明式基类与通用列约定。

**为什么单独一个模块而不是放在 database.py**：
`database.py` 是"连接管理"，`base.py` 是"模型定义约定"。两者变化原因不同 ——
连接参数会因环境调整，模型基类会因数据规范调整。混在一起会让"改超时"和
"改主键类型"落在同一个文件里，diff 难读、评审难聚焦。

**全局约定落地（数据结构文档 02 §1）**：
- 主键 UUID，服务端生成（`gen_random_uuid()`，PG 13+ 内置，无需 pgcrypto）
- 时间 timestamptz，UTC 入库（`TIMESTAMP(timezone=True)`）
- 枚举 text + CHECK，迁移管理（不用 PG 原生 ENUM：加值要 ALTER TYPE，
  而 CHECK 约束改动更可控，且回滚简单）
- mutable 表带 updated_at + lock_version（从 1 开始，对应 ETag）
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 命名约定。显式指定后，Alembic autogenerate 与手写迁移产生的约束名一致，
# 否则"自动生成的索引名"与"手写的名字"会不同，导致后续迁移重复建索引。
# 这是踩过坑的实践：不约定命名时，PG 自动命名（如 users_email_key）
# 与 SQLAlchemy 默认命名（ix_users_email）会打架。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有存储模型的基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[uuid.UUID]:
    """UUID 主键列。

    用 `server_default=text("gen_random_uuid()")` 而不是 Python 侧生成：
    服务端生成意味着"直接用 SQL 插入"也能得到合法主键，
    排障与数据修复时不必先算一个 UUID。
    gen_random_uuid() 自 PG 13 起内置于核心（无需 pgcrypto 扩展），
    本项目锁定 PG 17，因此不引入额外扩展依赖。
    """
    return mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


def created_at_column() -> Mapped[datetime]:
    """创建时间。timestamptz，服务端取时间。"""
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


def updated_at_column() -> Mapped[datetime]:
    """更新时间。onupdate 让 ORM 侧更新时自动刷新。"""
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


def lock_version_column() -> Mapped[int]:
    """乐观锁版本号，从 1 开始递增，对外映射为 ETag。

    为什么用 bigint 而不是 int：int 上限 21 亿，看起来够用，
    但版本号是"每次编辑 +1"的累积值，长生命周期对象（例如被反复微调的内容）
    在极端情况下会逼近上限。bigint 的存储代价可以忽略。
    """
    return mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("1"),
    )


__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "created_at_column",
    "lock_version_column",
    "updated_at_column",
    "uuid_pk",
]