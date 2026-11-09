"""知识五张表的存储模型（WP20）。

规格源 02 §8 的五张表（`knowledge_assets` / `knowledge_versions` /
`knowledge_publications` / `knowledge_chunks` / `knowledge_proposals`）。

**本包只建 `knowledge_chunks` 的表，不生产 chunk** —— 切块与 embedding
是 WP21 的核心设计（HNSW、维度、profile 分离），本包建表是因为
`knowledge_chunks.knowledge_version_id` 的外键目标是本包的表；
把它留给 WP21 会让"版本被删时 chunk 怎么办"（RESTRICT 还是 CASCADE）
由另一个包来定 —— 而它取决于本包的版本不可变语义。
表定义完整、无设计空间 → 本包建（判断法见说明文档 §1.6）。

---

## 三处需要解释的设计

### 1. `knowledge_versions.id` **同时**是主键与外键（指向 `version_nodes`）

规格源 02 §8 原文：`id PK/FK version_nodes`。
与 WP18 的 `brief_versions` 完全同构，理由同 WP11 §5：
"每个具体版本表的 `id` 同时 FK `version_nodes.id`；注册表、具体表及引用
**同一事务插入**"。

**延迟外键**（`DEFERRABLE INITIALLY DEFERRED`）：同事务插入意味着
两行的顺序由应用决定，而 `register_version` 内部用 SAVEPOINT + 重试取号，
不暴露"节点行何时落库"的控制权。延迟到提交时校验，"谁先谁后"就不再是
一个必须猜对的问题（WP18 已实测：延迟触发器在 INSERT 时静默通过、
在 COMMIT 时抛 `sqlstate=23514`）。

### 2. `source_segment_ids` 是 `UUID[]` 而不是关联表

规格源明写 `source_segment_ids UUID[]`。**它是一份"来源清单"而不是关系**：
版本不可变，这份清单一经写入就不再改变；没有"按片段反查版本"的查询需求
（`GET /knowledge/assets/{id}/references` 查的是**反向**——
谁引用了这条知识，那走 `version_dependencies`）。

用数组的代价是**数据库层无法用外键保证片段存在**。
这不是遗漏，而是取舍：数组元素的外键在 PostgreSQL 里需要触发器或
`unnest` 校验，而"片段已被删除"这件事在本项目里不会发生
（`source_segments` 随解析运行不可变）。已登记为待定项。

### 3. `knowledge_publications.sequence` 是本包**新增**的列

规格源给的列里没有它，但**没有它就无法确定性投影**：
投影要按事件发生顺序处理，而 `now()` 在 PostgreSQL 里是**事务开始时刻** ——
同一事务内插入的 `ACTIVATE(v2)` 与 `SUPERSEDE(v1)` 时间戳完全相同，
按 `created_at` 排序得到的是不确定的顺序（见 `projection.py` 的说明）。

因此加一列 `sequence int`，语义是"该 asset 内的第几条发布事件"，
由 repository 在插入时按 `max + 1` 分配，配 `UQ(asset_id, sequence)`。
这是本包发现的**规格缺口**，已登记为待定项。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.knowledge.types import (
    CATEGORY_LIST_SQL,
    KNOWLEDGE_SCHEMA_VERSION,
    MAX_BODY_CHARS,
    MAX_CHANGE_REASON_CHARS,
    MAX_REASON_CHARS,
    MAX_TITLE_CHARS,
    PROPOSAL_STATE_LIST_SQL,
    PUBLICATION_ACTION_LIST_SQL,
    SCOPE_LIST_SQL,
)
from rednote.platform.base import Base, created_at_column, updated_at_column, uuid_pk
from rednote.platform.immutability import guard_immutable

# 本模块需要不可变保护的表。`ddl.py` 里有同值副本（迁移要在不加载 ORM
# 元数据的前提下拿到表名），两处必须一致 —— 测试会交叉核对。
#
# **为什么 `knowledge_publications` 也在其中**：它是**生效事件**表
# （规格源 02 §8 备注的原文），事件一旦写上就是历史事实。
# 改一条事件 = 篡改"这条知识什么时候生效过"的答案，
# 而那正是验收原文「归档可追溯」所依赖的东西。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = (
    "knowledge_versions",
    "knowledge_publications",
)

# `structured_payload` 的形状版本。与 WP11 的 `schema_version` /
# WP17 的 `position_schema_version` / WP18 的 `BRIEF_SCHEMA_VERSION` 同一手法。
KNOWLEDGE_PAYLOAD_SCHEMA_VERSION: Final[int] = KNOWLEDGE_SCHEMA_VERSION

# chunk 序号从 1 开始。
MIN_CHUNK_NO: Final[int] = 1
MAX_CHUNK_NO: Final[int] = 2_147_483_647

# 发布事件的序号从 1 开始。
MIN_PUBLICATION_SEQUENCE: Final[int] = 1

# chunk 的 token 数下界（0 是合法的"空块"，但负数是错的）。
MIN_TOKEN_COUNT: Final[int] = 0


class KnowledgeAsset(Base):
    """一条逻辑知识。**可变**（`active_version_id` / `archived_at` 是被投影的投影列）。

    **`active_version_id` 是一列投影，不是权威**（这是本表最要紧的一句话）：
    权威是 `knowledge_publications` 的事件流。本列的作用是让
    「事实精确读取」不用扫事件表 —— 规格源 §14 的索引计划原文：

    > | 事实精确读取 | `knowledge_assets(product_id,category,active_version_id)` |

    **它是可重建的**：`service.rebuild_active_version` 能从事件流重算它
    （测试会验证"重算结果与增量维护的结果一致"）。允许例外地就地更新它，
    是因为规格源 §277 明写"切换 `active_version`"。

    **为什么 `archived_at` 用时间戳而不是 bool**：归档时间本身是审计信息
    （"这条知识是什么时候退出的"），而 bool 会把它丢掉；
    要再拿回来就得去翻事件表 —— 那正是本表想避免的。

    **为什么没有 `lock_version`**：与本表的可变列同 WP18 的 `briefs` 判断 ——
    两个可变列都由**服务端**在发布事务里设置，没有"客户端读出来、
    改几个字段、写回去"的路径，因此没有旧值要携带。
    （`If-Match` 在协议 §9 里确实出现在 `publications` / `versions` 两行，
    但那是防"拿着过期页面提交"；本实现用**事件序号**做前置校验，
    见 `service.publish` 的说明。）
    """

    __tablename__ = "knowledge_assets"

    id: Mapped[uuid.UUID] = uuid_pk()

    category: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(MAX_TITLE_CHARS), nullable=False)

    # 范围限定列。**可空是有意的**：GLOBAL 范围的知识不属于任何品牌/产品/项目。
    # 哪一列必须非空由 `scope` 决定（CHECK 逐条表达，见 __table_args__）。
    brand_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("brands.id", ondelete="RESTRICT"),
        nullable=True,
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=True,
    )
    scope: Mapped[str] = mapped_column(String(20), nullable=False)

    # 当前生效版本（投影列，见类 docstring）。
    # **延迟外键**：发布事务里先插版本行、再更新本列，也可能反过来 ——
    # 延迟到提交时校验让顺序不再重要（与 `briefs.active_version_id` 同构）。
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "version_nodes.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )

    # 归档时间戳（可空 = 未归档）。见类 docstring。
    archived_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            f"category IN ({CATEGORY_LIST_SQL})", name="category_valid"
        ),
        CheckConstraint(f"scope IN ({SCOPE_LIST_SQL})", name="scope_valid"),
        CheckConstraint("char_length(title) > 0", name="title_not_empty"),
        # --- "范围必须明确"的数据库级表达（三条，逐条对应一种矛盾） --------
        #
        # 为什么三条而不是一条复合 CHECK：复合的报错只有一个约束名，
        # 排障时无法分辨"是范围列缺了"还是"范围与列矛盾"。
        # 分开写让 `sqlstate` + 约束名能唯一定位。
        CheckConstraint(
            "scope <> 'BRAND' OR brand_id IS NOT NULL",
            name="scope_brand_needs_brand_id",
        ),
        CheckConstraint(
            "scope <> 'PRODUCT' OR product_id IS NOT NULL",
            name="scope_product_needs_product_id",
        ),
        CheckConstraint(
            "scope <> 'PROJECT' OR project_id IS NOT NULL",
            name="scope_project_needs_project_id",
        ),
        # 反向：GLOBAL 不该带任何限定列（自相矛盾的范围声明）。
        CheckConstraint(
            "scope <> 'GLOBAL' OR (brand_id IS NULL AND product_id IS NULL"
            " AND project_id IS NULL)",
            name="scope_global_has_no_qualifier",
        ),
        # --- "事实范围必须明确" ---------------------------------------------
        # 规格源 02 §8 备注：`FACT` 类的事实范围必须明确。
        # `scope='GLOBAL'` 的一条事实断言会横穿全部项目 —— 那正是
        # WP21 验收原文「近似品牌案例不串事实」要防的源头。
        CheckConstraint(
            "category <> 'FACT' OR scope <> 'GLOBAL'",
            name="fact_requires_concrete_scope",
        ),
        CheckConstraint(
            "archived_at IS NULL OR active_version_id IS NOT NULL",
            name="archived_implies_had_version",
        ),
        # --- 索引（规格源 §14 的查询计划）-----------------------------------
        # **事实精确读取**（规格源 §14 明写本包的索引）：
        #   knowledge_assets(product_id, category, active_version_id)
        #
        # **部分索引**，`WHERE` 逐字核对过：只覆盖"当前可用"的资产。
        # 已归档的资产不进这条索引 —— 精确读取的语义是"取当前生效的事实"，
        # 归档知识不该被它命中（归档知识走"按 id 直读"）。
        # 少写 WHERE 会让索引包含全部历史行，随归档增长而退化，
        # 而**查询结果不变** —— 于是退化不会被任何测试发现。
        Index(
            "ix_knowledge_assets_fact_lookup",
            "product_id",
            "category",
            "active_version_id",
            postgresql_where=text(
                "active_version_id IS NOT NULL AND archived_at IS NULL"
            ),
        ),
        # 知识列表的筛选组合（规格源 §14："category、brand_id、product_id、
        # archived_at 组合"）。拆成两条而不是一条四列：
        # 列表筛选是"任选其中几个"的组合，一条长索引只对最左前缀有效。
        Index(
            "ix_knowledge_assets_brand_category",
            "brand_id",
            "category",
            "archived_at",
        ),
        Index(
            "ix_knowledge_assets_product_category",
            "product_id",
            "category",
            "archived_at",
        ),
        # 项目私有知识的可见范围过滤（检查点 G2 的"项目私有数据边界"）。
        Index("ix_knowledge_assets_project_id", "project_id"),
        # 默认列表的排序路径：按创建时间倒序翻页（keyset 分页的锚点）。
        Index("ix_knowledge_assets_created_at", "created_at"),
    )


class KnowledgeVersion(Base):
    """知识的一个版本。**不可变**（`id` 同时 FK `version_nodes.id`）。

    `body` 是给人读的散文，`structured_payload` 是给下游消费的结构 ——
    规格源 02 §8 的备注「**禁止无结构散文替代事实字段**」正是指这一对：
    只写 `body` 而不写 `structured_payload` 的知识，下游只能整段塞进
    Prompt，而"这个数字适用于哪个型号"永远问不出来。

    **`source_segment_ids` 用数组**（取舍见模块 docstring 第 2 点）。
    **`effective_from` / `effective_until` 是可空的生效窗口**：它表达的是
    "这条知识在什么时间段内成立"（例如某次活动的规则），
    与"知识什么时候被激活"（`knowledge_publications` 记录的）是两件事 ——
    前者是**知识的语义属性**，后者是**系统事件**。
    """

    __tablename__ = "knowledge_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "version_nodes.id",
            ondelete="RESTRICT",
            # 延迟：注册表与具体表同事务插入，顺序由 WP11 的取号重试逻辑决定。
            deferrable=True,
            initially="DEFERRED",
        ),
        primary_key=True,
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_assets.id", ondelete="RESTRICT"),
        nullable=False,
    )

    body: Mapped[str] = mapped_column(Text, nullable=False)
    structured_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )

    # 来源片段（规格源 02 §8 的 `source_segment_ids UUID[]`）。
    # `nullable=False` + `server_default '{}'`：空数组是一个明确的事实
    # （"这条知识没有原文片段来源"），而 NULL 会让下游要为两种空写分支。
    source_segment_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)),
        nullable=False,
        server_default=text("'{}'::uuid[]"),
    )

    effective_from: Mapped[datetime | None] = mapped_column(nullable=True)
    effective_until: Mapped[datetime | None] = mapped_column(nullable=True)

    # 变更理由。**非空**：版本不可变，这份理由是一经写入就无法补充的
    # 唯一解释 —— 允许为空会让"为什么又发了一版"永远没有答案。
    change_reason: Mapped[str] = mapped_column(
        String(MAX_CHANGE_REASON_CHARS), nullable=False
    )

    schema_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text(str(KNOWLEDGE_PAYLOAD_SCHEMA_VERSION)),
    )

    created_at: Mapped[datetime] = created_at_column()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint("char_length(body) > 0", name="body_not_empty"),
        CheckConstraint(
            f"char_length(body) <= {MAX_BODY_CHARS}", name="body_length"
        ),
        CheckConstraint(
            "char_length(change_reason) > 0", name="change_reason_not_empty"
        ),
        # --- "禁止无结构散文替代事实字段"的数据库级表达 --------------------
        # 两条一起才够：
        #   1. 类型必须是 object（数组/标量都不是"结构化载荷"）；
        #   2. **必须非空** —— 这条是核心。空对象让"类别对应 Schema"
        #      退化成一句空话：任何类别都可以用一个 `{}` 满足它。
        # 单靠服务层做不到这件事（导入脚本、数据回填都绕过它），
        # 而这正是本包最要紧的判据之一。
        CheckConstraint(
            "jsonb_typeof(structured_payload) = 'object'",
            name="structured_payload_is_object",
        ),
        CheckConstraint(
            "structured_payload <> '{}'::jsonb",
            name="structured_payload_not_empty",
        ),
        # 生效窗口必须自洽（`from < until`）。只判一侧非空没有意义 ——
        # 一个倒置的窗口会被下游解释成"永远不生效"或"永远生效"，
        # 而两种解释都不会报错。
        CheckConstraint(
            "effective_until IS NULL OR effective_from IS NULL"
            " OR effective_from < effective_until",
            name="effective_window_ordered",
        ),
        CheckConstraint(
            "schema_version >= 1", name="schema_version_positive"
        ),
        # 版本列表的查询路径（按 asset 取全部版本）。
        Index("ix_knowledge_versions_asset_id", "asset_id"),
    )


class KnowledgePublication(Base):
    """一次发布事件。**不可变**（规格源 02 §8 的"生效事件"）。

    | action | 含义 |
    |---|---|
    | `ACTIVATE` | 某版本开始生效（同时切换 `active_version_id`） |
    | `ARCHIVE` | 整条 asset 退出可用（`archived_at` 置位，**active_version 不动**） |
    | `RESTORE` | 从归档恢复（`archived_at` 清空） |
    | `SUPERSEDE` | 一个版本被取代（由 ACTIVATE 自动产生，或显式发出） |

    `ARCHIVE` 的语义取舍见 `projection.py` 的模块 docstring
    （"不动 active_version"是验收原文「旧引用并存」的要求）。

    **`sequence` 列是本包新增的**（见模块 docstring 第 3 点）。
    """

    __tablename__ = "knowledge_publications"

    id: Mapped[uuid.UUID] = uuid_pk()

    asset_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False)

    # 事件在**本 asset 内**的序号（从 1 开始）。投影的排序键 ——
    # 理由见模块 docstring 第 3 点（`now()` 在事务内是常量）。
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(
        String(MAX_REASON_CHARS), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            f"action IN ({PUBLICATION_ACTION_LIST_SQL})", name="action_valid"
        ),
        CheckConstraint(
            f"sequence >= {MIN_PUBLICATION_SEQUENCE}",
            name="sequence_positive",
        ),
        # 同一 asset 内序号唯一 —— 这是并发发布时"两人同时取到同一个号"
        # 的拦截面（先查再插之间有窗口，唯一约束是唯一防线，同 WP11）。
        UniqueConstraint("asset_id", "sequence", name="uq_publications_asset_sequence"),
        # 取某 asset 的事件流（投影的输入）：按序号升序。
        Index("ix_knowledge_publications_asset_sequence", "asset_id", "sequence"),
        # 反查"哪些知识用过这个版本、以什么动作"（排障与引用摘要）。
        Index("ix_knowledge_publications_version_id", "version_id"),
    )


class KnowledgeChunk(Base):
    """知识版本切出的一块文本。**本包只建表，不生产 chunk**（生产归 WP21）。

    **为什么现在建**：`knowledge_version_id` 的外键指向本包的
    `knowledge_versions`。把它留给 WP21 会让"版本被删时 chunk 怎么办"
    这个决定由另一个包来做 —— 而它取决于本包的版本不可变语义
    （版本删不掉，因此 chunk 的 `RESTRICT` 永远不触发，是纯防御）。

    **为什么不可变与否要分开说**：本表**不加**不可变触发器。
    原知识变更时要重新切块（规格源 02 §8 备注："原知识变更需新版本
    重新切块"），而重切是"删掉这一版的旧 chunk、写新 chunk"。
    但"重新切块"应当发生在**新版本**上 —— 旧版本的 chunk 若被删，
    旧引用（内容曾引用的知识版本）的片段就没了，违反「旧引用并存」。

    因此本表的约束是：
      · `UQ(knowledge_version_id, chunk_no)` —— 同一版本内块号唯一；
      · `ON DELETE RESTRICT` —— 版本删不掉（触发器已保证）；
      · **不建不可变触发器** —— 允许"同一版本重新切块"这一操作，
        它由 WP21 按自己的策略决定（重切会写审计），
        本包不为另一个包的策略预先设限。

    这个边界取舍已登记为待定项（本包建表但不定重切策略）。
    """

    __tablename__ = "knowledge_chunks"

    id: Mapped[uuid.UUID] = uuid_pk()

    knowledge_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    chunk_no: Mapped[int] = mapped_column(Integer, nullable=False)

    text_content: Mapped[str] = mapped_column(Text, nullable=False)
    # 文本摘要（SHA-256 十六进制）。用于"重切后内容变没变"的判定 ——
    # 不靠它就得逐字符比对，而 chunk 数量可能上千。
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # 定位信息（在原文中的位置）。形状由 WP21 的切块器决定 ——
    # 本包只要求它是 object（数组/标量不是"定位信息"）。
    locator: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # 关键词（规格源 02 §8 原文 `keywords text[]`），供关键词召回兜底。
    keywords: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        # 规格源 02 §8 原文：`UQ(version_id,chunk_no)`。
        UniqueConstraint(
            "knowledge_version_id",
            "chunk_no",
            name="uq_knowledge_chunks_version_chunk_no",
        ),
        CheckConstraint(
            f"chunk_no >= {MIN_CHUNK_NO} AND chunk_no <= {MAX_CHUNK_NO}",
            name="chunk_no_in_range",
        ),
        CheckConstraint(
            f"token_count >= {MIN_TOKEN_COUNT}", name="token_count_non_negative"
        ),
        CheckConstraint(
            "char_length(text_content) > 0", name="text_content_not_empty"
        ),
        CheckConstraint("char_length(text_hash) = 64", name="text_hash_length"),
        CheckConstraint(
            "jsonb_typeof(locator) = 'object'", name="locator_is_object"
        ),
    )


class KnowledgeProposal(Base):
    """AI 提出的知识变更候选。**可变**（`state` / `confirmed_by` 由决策写入）。

    规格源 02 §8 备注原文：「**AI 创建待确认，不直接生效**」。

    这句话是本表的全部意义：`proposed_payload` 里可以有任何内容，
    但它**不产生版本、不产生生效事件**。只有
    `POST /knowledge/proposals/{id}/decisions` 的 ACCEPT 才会
    "生成明确版本与生效事件"（协议 §9 原文）。

    **`target_asset_id` 可空** —— 两种提案：
      · 有目标 = 对已有知识的修改建议；
      · 无目标 = 建议**新建**一条知识（`title` / `category` 从哪来？
        它们必须在 `proposed_payload` 里，见 `service.decide_proposal`）。

    **为什么 `source_feedback_id` / `source_experiment_id` 只是普通 UUID 列、
    没有外键**：它们指向的表（`feedback` / 实验）**尚未落地**
    （WP41 / WP30）。加一个指向不存在的表的外键**建不出来**；
    加一个指向错误表的更糟。因此本包显式声明"这两个引用暂不受外键保护"，
    并登记为待定项 —— 待那两张表落地时补外键。
    """

    __tablename__ = "knowledge_proposals"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # 见类 docstring：这两列**刻意无外键**（目标表尚未落地）。
    source_feedback_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    source_experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    target_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_assets.id", ondelete="RESTRICT"),
        nullable=True,
    )

    proposed_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)

    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # 决策理由（协议 §9 的 `reason`）。REJECT 时**必须**有理由 ——
    # 一条被拒的 AI 建议若无理由，下一个看到它的人只能重走一遍判断。
    decision_reason: Mapped[str | None] = mapped_column(
        String(MAX_REASON_CHARS), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint(
            f"state IN ({PROPOSAL_STATE_LIST_SQL})", name="state_valid"
        ),
        CheckConstraint(f"scope IN ({SCOPE_LIST_SQL})", name="scope_valid"),
        CheckConstraint(
            "jsonb_typeof(proposed_payload) = 'object'",
            name="proposed_payload_is_object",
        ),
        CheckConstraint(
            "proposed_payload <> '{}'::jsonb",
            name="proposed_payload_not_empty",
        ),
        # --- 状态与决策痕迹必须一致（双向）----------------------------------
        # `PENDING` 不该有 `confirmed_by` —— 那会让"谁确认的"在提案
        # 尚未确认时就有一个答案（假溯源）。
        CheckConstraint(
            "state <> 'PENDING' OR confirmed_by IS NULL",
            name="pending_has_no_confirmer",
        ),
        # 反过来：已处置的提案**必须有确认人** —— 一条无人确认的
        # ACCEPTED 与 PENDING 在审计上无法区分。
        CheckConstraint(
            "state = 'PENDING' OR confirmed_by IS NOT NULL",
            name="settled_needs_confirmer",
        ),
        # REJECT 必须有理由（见类 docstring）。
        CheckConstraint(
            "state <> 'REJECTED' OR (decision_reason IS NOT NULL"
            " AND char_length(decision_reason) > 0)",
            name="reject_needs_reason",
        ),
        # 候选列表的查询路径（按状态 + 项目筛选）。
        Index("ix_knowledge_proposals_state", "state"),
        Index("ix_knowledge_proposals_project_id", "project_id"),
        Index("ix_knowledge_proposals_target_asset_id", "target_asset_id"),
    )


# 注册不可变守卫。放在模块级：只要 import 了本模块，守卫即生效，
# 不依赖应用启动钩子或初始化顺序。
guard_immutable(KnowledgeVersion, KnowledgePublication)


__all__ = [
    "IMMUTABLE_TABLES",
    "KNOWLEDGE_PAYLOAD_SCHEMA_VERSION",
    "KnowledgeAsset",
    "KnowledgeChunk",
    "KnowledgeProposal",
    "KnowledgePublication",
    "KnowledgeVersion",
]