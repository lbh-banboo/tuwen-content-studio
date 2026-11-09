"""KOC 交付与发布登记的存储模型（WP36）：四张表。

| 表 | 角色 | 可变性 |
|---|---|---|
| `koc_records` | 项目外的素人/达人的**轻量记录**（展示名 + 主页链接 + 联系方式引用） | 可变 |
| `deliveries` | 「谁在什么时候拿到了哪一篇的哪一版」 | **列级**可变（见下） |
| `publications` | 「我方交付的那一版 ↔ KOC 实际发出的那条笔记」的登记 | 可变 |
| `publication_links` | 一条发布下的若干原始/规范化链接（**本包只建表**，判定归 WP37） | 可变（只追加） |

## ⚠️ 本仓有三个「publication」，语义互不相干

| 表 | 属于 | 「发布」指什么 |
|---|---|---|
| `knowledge_publications`（`modules/knowledge/models.py:401`） | WP20 | 把某个**知识资产版本**发布上线（内部资产的生效） |
| `prompt_publications`（`modules/prompts/models.py:315`） | WP23 | 把某个**提示词版本**发布上线（内部资产的生效） |
| **`publications`（本表）** | WP36 | **KOC 在小红书上实际发出的那条笔记**的登记（外部事实） |

裸名 `publications` 是契约草案与 DDL 草案已冻结的名字，不改（改名要连带改
契约、mock、WP37/38/39/41 的全部引用）。ORM 类名 `Publication` 只在本模块
出现；平级模块本来就不可互 import，混淆风险止于文档。

## `deliveries` 为什么是「列级」可变

它既要有状态机（`PREPARED → DELIVERED / CANCELLED`），又必须保证
**原交付版本不随改稿变化**。平台现成的 `guard_immutable` 是**整行**不可变
（`BEFORE UPDATE OR DELETE`），用不上。于是：

1. **可变列是白名单**：`MUTABLE_DELIVERY_COLUMNS`（7 列）。
2. **冻结列由磁盘派生**：`frozen_delivery_columns()` = 全部列 − 白名单。
   新增一列若不显式进白名单，**默认被冻结** —— fail-closed。
   `ddl.py` 的 `BEFORE UPDATE` 触发器与测试的逐列变异断言都从这个函数取名单，
   **没有任何地方由人手写冻结列清单**（人写的名单在加列时会静默漏，铁律 14）。

`modules/content/models.py:18` 的警告原文：「『只许改某一列』的触发器不好写…
写错的后果是历史事实可以被整行改写 —— 那是一张『看起来不可变、实际有一扇
后门』的表」。本表用"派生 + 变异断言"两道一起堵这扇门。

## 刻意不做的唯一约束

| 不做 | 为什么 |
|---|---|
| `(content_item_id, koc_record_id)` 唯一 | 一条内容可分配给多位 KOC（原型 `static/delivery-koc.html:148`），交付还可超额（`static/delivery.html:42`）。加唯一会误拒合法业务 |
| `koc_records.platform_home_url` 唯一 | 同名不同人、同链不同参 ⇒ 唯一约束会误拒（草案 `:54` 明写「不做」） |
| `publication_links.canonical_url` 全局唯一 | 与 WP37 的「跨项目同笔记需管理者确认」互斥：全局唯一会让第二个项目**物理上**无法登记同一条笔记，确认分支永不可达。本包只建普通索引，唯一性留给 WP37 按「项目内唯一 + 跨项目需确认」补（先宽后紧只需 ADD，先紧后宽要 DROP） |

## 本包只建列、恒留 NULL 的两列（WP37 规范器产）

`koc_records.home_url_canonical`、`publications.url_canonical`。
本包**零 URL 规范化代码** —— 写一行就等于造第二份实现（OP-197 同族）。
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
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.delivery.types import (
    CONTENT_STATE_LIST_SQL,
    DELIVERY_STATE_LIST_SQL,
    LINK_KIND_LIST_SQL,
    MAX_DISPLAY_NAME_CHARS,
    MAX_IDEMPOTENCY_KEY_CHARS,
    MAX_PUBLICATION_KEY_CHARS,
    PUBLICATION_KEY_REGEX_SQL,
    PUBLICATION_STATE_LIST_SQL,
    SHA256_HEX_CHARS,
)
from rednote.platform.base import (
    Base,
    created_at_column,
    lock_version_column,
    updated_at_column,
    uuid_pk,
)

#: 本模块拥有的全部表（迁移、测试夹具、证据脚本的唯一来源）。
#: 顺序 = 建表顺序（`publications` FK → `deliveries` FK → `koc_records`）。
OWN_TABLES: Final[tuple[str, ...]] = (
    "koc_records",
    "deliveries",
    "publications",
    "publication_links",
)

#: ⚠️ 本模块**没有**整行不可变表 ⇒ 不调 `guard_immutable`、不挂
#: `trg_*_immutable`。`deliveries` 的不可变性是**列级**的（见模块 docstring）；
#: `publication_links` 留作可变表，因为 WP37 要往里写、要改 `link_kind`。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = ()

#: ★ `deliveries` 上**唯一**允许被 UPDATE 的列。这张白名单是本包最要紧的七个名字。
#:
#: 为什么是这七个：
#: · `state` / `delivered_at` / `cancelled_at` —— 状态机本体；
#: · `delivered_by` —— 与 `delivered_at` 同形（谁标记的，原型「操作人」列）；
#: · `note` —— 运营的备注，事后可补；
#: · `updated_at` / `lock_version` —— 平台约定的可变表附加列。
#:
#: 其余每一列都是「交付这件事本身」：改了就是改写历史。
MUTABLE_DELIVERY_COLUMNS: Final[tuple[str, ...]] = (
    "state",
    "delivered_at",
    "delivered_by",
    "cancelled_at",
    "note",
    "updated_at",
    "lock_version",
)


class KocRecord(Base):
    """一位 KOC 的轻量记录。**不是系统用户**，KOC 本人不登录本系统。

    `contact_ref` 是联系方式的**引用**（例如外部 CRM 的条目号），不是联系方式
    本体。**禁止**任何存放密码/令牌的列（草案 `:55` 明文）。
    """

    __tablename__ = "koc_records"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    display_name: Mapped[str] = mapped_column(
        String(MAX_DISPLAY_NAME_CHARS), nullable=False
    )

    #: 主页链接。只做去重**提示**，不做硬唯一（见模块 docstring）。
    platform_home_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: WP37 规范器产。**本包只建列、恒 NULL**。
    home_url_canonical: Mapped[str | None] = mapped_column(Text, nullable=True)

    contact_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    lock_version: Mapped[int] = lock_version_column()

    __table_args__ = (
        # 空白名（"   "）在应用层被 strip 后是空串，但裸 SQL 能绕过应用层。
        CheckConstraint(
            "char_length(btrim(display_name)) >= 1", name="display_name_not_blank"
        ),
        Index("ix_koc_records_project", "project_id"),
        Index("ix_koc_records_home_canonical", "home_url_canonical"),
    )


class Delivery(Base):
    """一次交付：**把一个固定版本的内容包显式交给某位 KOC**。

    「交付」不是「下载」。下载端点属 `exports` 模块且全程只读，物理上触不到
    本表的 `delivered_at`（`exports/api.py:327-368` 零写操作）。

    三个核心冻结字段（`content_item_id` / `version_node_id` / `eligibility_hash`）
    **从 `export_items` 逐列复制**，不接受客户端传版本：客户端传版本等于让陈旧
    缓存决定交付哪一版（WP35 已为同一问题否决过这个形态，`exports/api.py:117-122`）。
    """

    __tablename__ = "deliveries"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: 可空：允许先建交付行、后指派 KOC（草案 §1.2 原形态）。
    #: ⚠️ 但它是**冻结列** —— 一旦落行就不能换人（换人 = 另一次交付事实）。
    koc_record_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("koc_records.id", ondelete="RESTRICT"),
        nullable=True,
    )

    #: ★ 反向引用 WP35 的冻结行。方向由 WP35 写死（`exports/models.py:21-25`
    #: 原文「WP36 建表时反向引用 `export_items`」）。
    #: **NOT NULL**：交付必须基于一个已经打好的包 —— 没有包就没有交付物。
    export_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("export_items.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: 从 `export_items.content_item_id` 复制。★ 这是一条**跨包入向 FK**
    #: （RG-130），`test_content_wp26.py` 的白名单须由主 agent 同步 +1。
    content_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_items.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: 从 `export_items.version_node_id` 复制。**固定导出版本**，不随改稿变。
    version_node_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("version_nodes.id", ondelete="RESTRICT"),
        nullable=False,
    )

    state: Mapped[str] = mapped_column(String(20), nullable=False)

    #: 从 `export_items.eligibility_hash` 复制。形状与 WP35 **同形**
    #: （`String(64)` + 十六进制 CHECK）：否则"复制过来的指纹"无法与重算值比对。
    eligibility_hash: Mapped[str] = mapped_column(
        String(SHA256_HEX_CHARS), nullable=False
    )

    #: 替换链：指向被本行替换掉的那一行。**追加，不覆盖** —— 旧行一个字不改。
    replaces_delivery_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("deliveries.id", ondelete="RESTRICT"),
        nullable=True,
    )

    idempotency_key: Mapped[str | None] = mapped_column(
        String(MAX_IDEMPOTENCY_KEY_CHARS), nullable=True
    )
    #: 请求摘要。同键再来时与它比对，不等 ⇒ 409（同键异体）。
    #: ★ 草案与契约都没有这一列，**但没有它幂等就是假的**
    #: （`M4契约与幂等口径.md:83`「必须比对请求体指纹，不能只看键存在」）。
    request_hash: Mapped[str] = mapped_column(String(SHA256_HEX_CHARS), nullable=False)

    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: 操作人（原型的「操作人」列，`static/delivery-koc.html:317`）。
    #: 与 `delivered_at` 同形：两者同时有或同时无（`ck_deliveries_delivered_by_shape`）。
    delivered_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    lock_version: Mapped[int] = lock_version_column()

    __table_args__ = (
        CheckConstraint(
            f"state IN ({DELIVERY_STATE_LIST_SQL})", name="state_valid"
        ),
        # ★ 这条 CHECK 是「下载不计交付」的**结构性**载体之一：想置
        # `delivered_at` 就必须同时把 `state` 推到 `DELIVERED`，而推 `state`
        # 只有服务层一个入口。双向等式，单向判会放过"DELIVERED 但没时间戳"。
        CheckConstraint(
            "(state = 'DELIVERED') = (delivered_at IS NOT NULL)"
            " AND (state = 'CANCELLED') = (cancelled_at IS NOT NULL)",
            name="state_timestamps",
        ),
        CheckConstraint(
            "(delivered_at IS NULL) = (delivered_by IS NULL)",
            name="delivered_by_shape",
        ),
        # 自指替换是一个无意义的环。`IS DISTINCT FROM` 而非 `<>`：
        # `replaces_delivery_id` 可空，`NULL <> id` 求值为 NULL ⇒ CHECK 放行，
        # 看起来"有约束"其实对空值无效（这里恰好无害，但口径必须一致）。
        CheckConstraint(
            "replaces_delivery_id IS DISTINCT FROM id", name="no_self_replace"
        ),
        CheckConstraint(
            f"char_length(eligibility_hash) = {SHA256_HEX_CHARS}"
            " AND eligibility_hash ~ '^[0-9a-f]+$'",
            name="eligibility_hash_shape",
        ),
        CheckConstraint(
            f"char_length(request_hash) = {SHA256_HEX_CHARS}"
            " AND request_hash ~ '^[0-9a-f]+$'",
            name="request_hash_shape",
        ),
        # 幂等：一键一行（WP35 的同名索引是三列，因为那是"一键一组 N 行"）。
        Index(
            "uq_deliveries_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        # 一条旧交付**最多被替换一次** —— 否则「替换链」会变成「替换树」，
        # 「当前有效的那一版」就有了两个答案。
        # ⚠️ 这条部分唯一索引同时承担了草案 §1.2 `ix_deliveries_replaces` 的
        #    反查职责：`WHERE replaces_delivery_id = :id` 只会查非空值，
        #    恰在本索引的覆盖范围内 ⇒ 不再另建普通索引（见 DDL 草案差异登记）。
        Index(
            "uq_deliveries_replaces",
            "replaces_delivery_id",
            unique=True,
            postgresql_where=text("replaces_delivery_id IS NOT NULL"),
        ),
        Index("ix_deliveries_project_state", "project_id", "state"),
        # 「这次导出被交付给了谁」的反查。
        Index("ix_deliveries_export_item", "export_item_id"),
        # 原型的「同内容多 KOC 提示」要按内容聚合。
        Index("ix_deliveries_content", "project_id", "content_item_id"),
    )


class Publication(Base):
    """一条发布登记：**我方交付的那一版 ↔ KOC 实际发出的那条笔记**。

    本包只做**登记**：`note_id` / `note_url` 存**原样值**，不做任何清洗。
    `url_canonical` 只建列恒 NULL（WP37）。

    ⚠️ `content_state = UNVERIFIED` **不等于**「已删除」。本包不做抓取 ⇒
    一律落 `UNVERIFIED`；取值域只有两个值，**结构上就推不出「删除」**。
    """

    __tablename__ = "publications"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: 草稿 → 实发映射（**包内** FK）。可空：先有笔记后补映射是合法的。
    delivery_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("deliveries.id", ondelete="RESTRICT"),
        nullable=True,
    )

    #: 业务主键。WP39 的指标导入按这个键找发布 ⇒ **格式不得自行放宽**。
    publication_key: Mapped[str] = mapped_column(
        String(MAX_PUBLICATION_KEY_CHARS), nullable=False
    )

    note_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    note_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: WP37 规范器产。**本包只建列、恒 NULL**。
    url_canonical: Mapped[str | None] = mapped_column(Text, nullable=True)

    state: Mapped[str] = mapped_column(String(20), nullable=False)
    content_state: Mapped[str] = mapped_column(String(20), nullable=False)

    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    idempotency_key: Mapped[str | None] = mapped_column(
        String(MAX_IDEMPOTENCY_KEY_CHARS), nullable=True
    )
    request_hash: Mapped[str] = mapped_column(String(SHA256_HEX_CHARS), nullable=False)

    created_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    lock_version: Mapped[int] = lock_version_column()

    __table_args__ = (
        CheckConstraint(
            f"state IN ({PUBLICATION_STATE_LIST_SQL})", name="state_valid"
        ),
        CheckConstraint(
            f"content_state IN ({CONTENT_STATE_LIST_SQL})", name="content_state_valid"
        ),
        CheckConstraint(
            f"publication_key ~ '{PUBLICATION_KEY_REGEX_SQL}'",
            name="publication_key_format",
        ),
        CheckConstraint(
            "(state = 'PUBLISHED') = (published_at IS NOT NULL)",
            name="published_shape",
        ),
        CheckConstraint(
            f"char_length(request_hash) = {SHA256_HEX_CHARS}"
            " AND request_hash ~ '^[0-9a-f]+$'",
            name="request_hash_shape",
        ),
        # 业务键。⚠️ `uq` 命名约定不含 `constraint_name` 令牌，显式名原样落库
        # ⇒ **必须写全名**，否则会被改成
        # `uq_publications_project_id_publication_key`（`exports/models.py:160`
        # 为此留过告警）。
        UniqueConstraint(
            "project_id", "publication_key", name="uq_publications_project_key"
        ),
        # 幂等键与业务键**并存**，各管一头：业务键挡"同一条笔记登记两次"，
        # 幂等键挡"同一个 Idempotency-Key 配了不同的登记内容"。
        Index(
            "uq_publications_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_publications_project_state", "project_id", "state"),
        Index("ix_publications_canonical", "url_canonical"),
        Index("ix_publications_delivery", "delivery_id"),
    )


class PublicationLink(Base):
    """一条发布下的链接（原始 + 规范化）。（WP36 建表；WP37 起由 `service.add_publication_link` 写入。）

    建表归 WP36（它 FK 到 `publications`，必须与之同一条迁移链上游），
    规范化与去重判定归 WP37。本包**没有**任何写这张表的服务层代码。
    """

    __tablename__ = "publication_links"

    id: Mapped[uuid.UUID] = uuid_pk()

    publication_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("publications.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: WP37 新增的冗余列（拍板 Q4）：= 所属发布的 `project_id`，由服务层写入。
    #: 冗余的唯一理由是让 `(project_id, canonical_url)` 能建成唯一索引。
    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    raw_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    link_kind: Mapped[str] = mapped_column(String(20), nullable=False)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(f"link_kind IN ({LINK_KIND_LIST_SQL})", name="link_kind_valid"),
        Index("ix_publication_links_publication", "publication_id"),
        Index("ix_publication_links_raw", "raw_url"),
        # ⚠️ **普通索引，不是唯一索引**（见模块 docstring 的取舍表）。
        Index("ix_publication_links_canonical", "canonical_url"),
        # WP37：项目内唯一（跨项目允许，由服务层按管理者确认放行）。
        Index(
            "uq_publication_links_project_canonical",
            "project_id",
            "canonical_url",
            unique=True,
        ),
    )


def frozen_delivery_columns() -> tuple[str, ...]:
    """`deliveries` 上**不可被 UPDATE** 的列，**由磁盘派生**。

    = 表的全部列 − `MUTABLE_DELIVERY_COLUMNS`。按列在表上的声明顺序返回
    （触发器 DDL 与测试的变异顺序因此稳定，diff 可读）。

    为什么必须派生而不是手写名单：手写的名单在**加列**时会静默漏掉那一列，
    于是表上多出一扇没人知道的后门，而库全绿、测试全绿。这正是铁律 14
    （"不许由单方面的名字决定覆盖范围"）要防的形态。
    """
    mutable = set(MUTABLE_DELIVERY_COLUMNS)
    return tuple(
        column.name
        for column in Delivery.__table__.columns
        if column.name not in mutable
    )


# 白名单里每一个名字都必须真的是表上的列：打错一个字母就会让那一列
# **悄悄变成可变列**（白名单比对的是字符串，不报错）。
assert set(MUTABLE_DELIVERY_COLUMNS) <= set(Delivery.__table__.columns.keys()), (
    "MUTABLE_DELIVERY_COLUMNS 里有 deliveries 表上不存在的列名："
    f"{sorted(set(MUTABLE_DELIVERY_COLUMNS) - set(Delivery.__table__.columns.keys()))}"
)
assert OWN_TABLES == (
    KocRecord.__tablename__,
    Delivery.__tablename__,
    Publication.__tablename__,
    PublicationLink.__tablename__,
)

__all__ = [
    "IMMUTABLE_TABLES",
    "MUTABLE_DELIVERY_COLUMNS",
    "OWN_TABLES",
    "Delivery",
    "KocRecord",
    "Publication",
    "PublicationLink",
    "frozen_delivery_columns",
]
