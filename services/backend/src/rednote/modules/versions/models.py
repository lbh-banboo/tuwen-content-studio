"""通用版本注册表与依赖边的存储模型（WP11）。

按数据结构文档 02 §5「通用版本引用」建立两张表。

**为什么需要一张"通用注册表"**：
Brief 版本会被方案引用、被内容引用、被报告引用；知识版本会被内容引用、
被基线引用。若每个引用方各自建一列外键，那么"M 种版本类型 × N 个引用方"
就要建 M×N 个外键列，而且**反查"谁引用了这个版本"根本做不到** ——
没有任何一张表记录全部引用关系。`version_nodes` + `version_dependencies`
把这件事收敛成两张表：前者给每个版本一个全局身份证，后者记录边。

**注册表不是万能 JSON 仓库**（文档 02 §5 原文）：
本表只存 `payload_hash`（内容摘要），**不存正文**。正文永远在各领域的
具体版本表里（`brief_versions` / `content_versions` / …）。
一旦允许往注册表塞 payload，它就会退化成"什么都往里装的表"，
Schema 失控、索引失效、迁移无法演进。

**`entity_id` 是逻辑实体，`id` 才是版本**：
`UQ(entity_type, entity_id, revision_no)` 的含义是
"某类实体的某个对象，它的第 N 版"。因此：
  · `entity_id` = `briefs.id`（逻辑 Brief 的身份，跨版本不变）
  · `id`        = `brief_versions.id`（这一版自己的身份）
文档 02 §5 要求"每个具体版本表的 `id` 同时 FK `version_nodes.id`"，
指的就是后一种关系。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.platform.base import Base, created_at_column, uuid_pk
from rednote.platform.concurrency import build_immutable_etag
from rednote.platform.immutability import guard_immutable

# ---------------------------------------------------------------------------
# entity_type 的受控取值域
# ---------------------------------------------------------------------------
# 数据结构文档 02 §5 原句：「跨 Brief、方案、内容、知识、媒体、Prompt、报告的溯源」，
# 共 7 类。另加 BASELINE —— 文档 02 §6 的 `production_baselines` 同样是
# `id PK/FK version_nodes`，即它也是一个版本化实体，必须有自己的类型。
#
# **为什么必须是受控枚举而不是自由字符串**：
# 允许任意字符串，注册表就变成了"万能表"—— 今天写 "brief"、明天写 "Brief"、
# 后天写 "brief_version"，同一类实体在表里出现三种拼写，
# 唯一约束和查询全部失效，而且**没有任何机制会报错**。
ENTITY_TYPE_BRIEF: Final[str] = "BRIEF"
ENTITY_TYPE_PLAN: Final[str] = "PLAN"
ENTITY_TYPE_CONTENT: Final[str] = "CONTENT"
ENTITY_TYPE_KNOWLEDGE: Final[str] = "KNOWLEDGE"
ENTITY_TYPE_MEDIA: Final[str] = "MEDIA"
ENTITY_TYPE_PROMPT: Final[str] = "PROMPT"
ENTITY_TYPE_REPORT: Final[str] = "REPORT"
ENTITY_TYPE_BASELINE: Final[str] = "BASELINE"

ENTITY_TYPES: Final[frozenset[str]] = frozenset(
    {
        ENTITY_TYPE_BRIEF,
        ENTITY_TYPE_PLAN,
        ENTITY_TYPE_CONTENT,
        ENTITY_TYPE_KNOWLEDGE,
        ENTITY_TYPE_MEDIA,
        ENTITY_TYPE_PROMPT,
        ENTITY_TYPE_REPORT,
        ENTITY_TYPE_BASELINE,
    }
)

# 与 CHECK 约束、迁移里的取值列表保持一致的极简形式 —— 生成 SQL 片段用。
ENTITY_TYPE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(ENTITY_TYPES)
)

# revision_no 是 int 列。上界显式写在 CHECK 里，避免溢出时报出
# 一个"整数超出范围"的底层错误（那种错误读起来像是程序 bug，其实是数据到了极限）。
MAX_REVISION_NO: Final[int] = 2_147_483_647

# schema_version 从 1 开始。它不是"第几版"，而是"payload 的结构版本"，
# 用于将来解析历史 payload 时选择正确的 Schema。
MIN_SCHEMA_VERSION: Final[int] = 1

# payload_hash 是 SHA-256 的十六进制摘要。
PAYLOAD_HASH_LENGTH: Final[int] = 64

class VersionNode(Base):
    """版本节点：全系统"某一版"的统一身份证。

    **不可变**：ORM 事件（`guard_immutable`）+ 数据库触发器（迁移中建立）
    双重守卫。理由见 `platform/immutability.py`。

    **`project_id` 可空**，语义必须说清楚：
      · 非空 —— 该版本属于某个项目，可见范围 = 该项目活跃成员 + 管理者；
      · 为空 —— **平台级版本**（典型是 `PROMPT` 模板版本），不属于任何项目，
        仅管理者可见。
    这个规则由 `permissions.py` 落地并测试，不靠调用方自觉。

    **为什么没有 `updated_at` / `lock_version`**：
    数据结构文档 02 §1 规定"mutable 表有 updated_at、lock_version；
    历史版本和事件表不允许原地改写"。本表属后者，因此两个列都不该有 ——
    加了 `lock_version` 反而是在暗示"它会被改"。
    """

    __tablename__ = "version_nodes"

    id: Mapped[uuid.UUID] = uuid_pk()

    # 受控枚举，由 CHECK 兜底（应用层的 ENTITY_TYPES 校验在 service 层先做，
    # 那里能给出可读的 422；CHECK 是防绕过写入的最后一道）。
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # 逻辑实体的身份（如 briefs.id），跨版本不变。
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    # 项目归属。可空 = 平台级版本。ondelete RESTRICT：项目归档不等于删除版本，
    # 但真要物理删项目时，必须先把版本处理掉 —— 拒绝级联删除历史（文档 02 §1）。
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 这是该实体的第几版，从 1 开始。
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)

    # payload 的结构版本，用于将来选择解析 Schema（不是"第几版"）。
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(MIN_SCHEMA_VERSION))
    )

    # 内容摘要（SHA-256 十六进制）。**只存摘要不存正文**：
    # 正文在各领域的具体版本表里。摘要的两个用途：
    #   1. 幂等判定 —— 同一修订号重复注册时，摘要一致则视为同一次操作；
    #   2. 篡改检测 —— 它是本表不可变 ETag 的取值来源。
    payload_hash: Mapped[str] = mapped_column(
        String(PAYLOAD_HASH_LENGTH), nullable=False
    )

    created_at: Mapped[datetime] = created_at_column()
    # 可空：系统自动产生的版本（如解析结果）没有具体操作人。
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        # 同一实体的同一修订号只能有一个节点 —— 这是"版本号不会重复"的
        # 数据库级保证。并发注册靠它撞错 + 重试，而不是靠"先查再插"
        # （先查再插之间有窗口，两个请求都会读到"不存在"）。
        UniqueConstraint(
            "entity_type",
            "entity_id",
            "revision_no",
            name="uq_version_nodes_entity_type_entity_id_revision_no",
        ),
        CheckConstraint(
            f"entity_type IN ({ENTITY_TYPE_LIST_SQL})",
            name="entity_type_valid",
        ),
        CheckConstraint(
            f"revision_no >= 1 AND revision_no <= {MAX_REVISION_NO}",
            name="revision_no_in_range",
        ),
        CheckConstraint(
            f"schema_version >= {MIN_SCHEMA_VERSION}",
            name="schema_version_positive",
        ),
        # 版本历史的查询路径（文档 02 §14：version_nodes(entity_type,entity_id,revision_no)）。
        # **刻意用升序索引**：PG 可以反向扫描升序索引来满足
        # `ORDER BY revision_no DESC`，因此不必建降序索引 ——
        # 表达式索引会让 Alembic autogenerate 难以比对（它读不回表达式），
        # 每次 autogenerate 都会提议重建。
        Index(
            "ix_version_nodes_entity_revision",
            "entity_type",
            "entity_id",
            "revision_no",
        ),
        Index("ix_version_nodes_project_id", "project_id"),
    )

    @property
    def etag(self) -> str:
        """该版本的**不可变** ETag（由内容摘要决定，永不变化）。

        放在模型上而不是让每个调用方自己拼：各领域模块的 GET 端点
        （`GET /contents/{id}` 之类）都要返回它，各拼一次迟早拼错格式。
        """
        return build_immutable_etag(self.id, self.payload_hash)


class VersionDependency(Base):
    """版本依赖边：`parent_version_id` 这一版，用到了 `dependency_version_id` 那一版。

    方向约定（与文档 02 §5 的列名一致）：
        `parent_version_id`      = 引用方（下游，例如某个内容版本）
        `dependency_version_id`  = 被引用方（上游，例如它依据的 Brief 版本）

    **不可变**：边一旦成立就是历史事实。改一条边等于篡改溯源记录。
    写错了只能另加一条边并在上层解释 —— 这与"版本不可变"是同一个道理。

    **复合主键包含 `purpose`**：同一个下游版本可以因不同用途引用同一个上游版本
    （例如既把某条知识当 `FACT` 又当 `STYLE` 参考）。若主键只有两端，
    第二种用途就无法记录。文档 02 §5 只写了"复合 PK"没写明含哪几列，
    此处的取舍已登记为待定项。
    """

    __tablename__ = "version_dependencies"

    parent_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("version_nodes.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    dependency_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("version_nodes.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    purpose: Mapped[str] = mapped_column(String(30), primary_key=True)

    # 引用时选中的片段摘要。可空：引用整版内容时没有具体片段。
    selected_fragment_hash: Mapped[str | None] = mapped_column(
        String(PAYLOAD_HASH_LENGTH), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        # 自引用检查。数据库层给出唯一确定的名字：
        # 若只靠触发器的 RAISE 文本，验证脚本得匹配错误文案；
        # CHECK 名可以直接断言（`AssertionError: constraint_name`）。
        CheckConstraint(
            "parent_version_id <> dependency_version_id",
            name="no_self_reference",
        ),
        # 反查"谁依赖了我"（影响分析、回退影响图）的主查询路径。
        # 主键已覆盖 (parent, dependency, purpose) 前缀，故只需补这一条。
        Index("ix_version_dependencies_dependency", "dependency_version_id"),
    )


# 注册不可变守卫。放在模块级：只要 import 了本模块，守卫即生效，
# 不依赖应用启动钩子或初始化顺序。
guard_immutable(VersionNode, VersionDependency)


__all__ = [
    "ENTITY_TYPES",
    "ENTITY_TYPE_BASELINE",
    "ENTITY_TYPE_BRIEF",
    "ENTITY_TYPE_CONTENT",
    "ENTITY_TYPE_KNOWLEDGE",
    "ENTITY_TYPE_MEDIA",
    "ENTITY_TYPE_PLAN",
    "ENTITY_TYPE_PROMPT",
    "ENTITY_TYPE_REPORT",
    "ENTITY_TYPE_LIST_SQL",
    "MAX_REVISION_NO",
    "MIN_SCHEMA_VERSION",
    "PAYLOAD_HASH_LENGTH",
    "VersionDependency",
    "VersionNode",
]
