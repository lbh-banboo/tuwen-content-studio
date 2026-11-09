"""WP36：KOC 记录/交付与替换/发布登记 —— 建 `koc_records` / `deliveries` / `publications` / `publication_links`。

revision: d6074ae4bc53
down_revision: c01128062b30

## 一、本迁移做什么

建四张可变表、1 个列级冻结触发器（`deliveries`，守"原交付版本不随改稿
变化"）+ 2 个本模块自有的延迟一致性触发器。本包**没有整行不可变表**
（`IMMUTABLE_TABLES` 为空），因此不涉及跨包共用的 `rednote_reject_mutation`。
DDL 与测试/证据脚本同源（`rednote.modules.delivery.ddl`）。

## 二、G2 铁律执行记录

1. **不删任何非本迁移创建的对象**：`downgrade` 只删本模块自己的 3 个函数
   （`consistency_function_names()`）。
2. **取值域写化石字面量**（ISS-191）：4 个新域（`deliveries.state`、
   `publications.state`、`publications.content_state`、
   `publication_links.link_kind`）全部写成字面量，不从
   `delivery.types` 活引用。
3. **建表顺序**：`koc_records` → `deliveries`（FK 指向 `koc_records`/
   `export_items`/`content_items`/`version_nodes`/自引用）→
   `publications`（FK 指向 `deliveries`）→ `publication_links`（FK 指向
   `publications`）。无循环外键，不需要拆环。
4. **`publication_links` 刻意只建一条非唯一索引**
   `ix_publication_links_canonical`，不建全局唯一约束：全局唯一会与
   WP37"跨项目同笔记需管理者确认"的产品规则互斥（先宽后紧，唯一约束留给
   WP37 的迁移按"项目内唯一"补）。

## 三、与模型的对应关系

逐项对应 `services/backend/src/rednote/modules/delivery/models.py` 的
`KocRecord` / `Delivery` / `Publication` / `PublicationLink` 四个类（已用
`check_migration_model_parity.py` 的同源脚本对账过结构），不是独立设计。

## 四、⚠️ `downgrade` 有数据损失

交付与发布记录是"谁拿到了哪一版、发没发出来"的唯一凭据；回退前须导出全量。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d6074ae4bc53"
down_revision: str | None = "c01128062b30"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "koc_records",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("platform_home_url", sa.Text(), nullable=True),
        sa.Column("home_url_canonical", sa.Text(), nullable=True),
        sa.Column("contact_ref", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
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
        sa.Column(
            "lock_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False
        ),
        sa.CheckConstraint(
            "char_length(btrim(display_name)) >= 1",
            name=op.f("ck_koc_records_display_name_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_koc_records_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_koc_records")),
    )
    op.create_index("ix_koc_records_project", "koc_records", ["project_id"])
    op.create_index(
        "ix_koc_records_home_canonical", "koc_records", ["home_url_canonical"]
    )

    op.create_table(
        "deliveries",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("koc_record_id", sa.UUID(), nullable=True),
        sa.Column("export_item_id", sa.UUID(), nullable=False),
        sa.Column("content_item_id", sa.UUID(), nullable=False),
        sa.Column("version_node_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("eligibility_hash", sa.String(length=64), nullable=False),
        sa.Column("replaces_delivery_id", sa.UUID(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_by", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
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
        sa.Column(
            "lock_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False
        ),
        sa.CheckConstraint(
            "state IN ('PREPARED', 'DELIVERED', 'CANCELLED')",
            name=op.f("ck_deliveries_state_valid"),
        ),
        sa.CheckConstraint(
            "(state = 'DELIVERED') = (delivered_at IS NOT NULL) AND"
            " (state = 'CANCELLED') = (cancelled_at IS NOT NULL)",
            name=op.f("ck_deliveries_state_timestamps"),
        ),
        sa.CheckConstraint(
            "(delivered_at IS NULL) = (delivered_by IS NULL)",
            name=op.f("ck_deliveries_delivered_by_shape"),
        ),
        sa.CheckConstraint(
            "replaces_delivery_id IS DISTINCT FROM id",
            name=op.f("ck_deliveries_no_self_replace"),
        ),
        sa.CheckConstraint(
            "char_length(eligibility_hash) = 64 AND eligibility_hash ~ '^[0-9a-f]+$'",
            name=op.f("ck_deliveries_eligibility_hash_shape"),
        ),
        sa.CheckConstraint(
            "char_length(request_hash) = 64 AND request_hash ~ '^[0-9a-f]+$'",
            name=op.f("ck_deliveries_request_hash_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_deliveries_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["koc_record_id"],
            ["koc_records.id"],
            name=op.f("fk_deliveries_koc_record_id_koc_records"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["export_item_id"],
            ["export_items.id"],
            name=op.f("fk_deliveries_export_item_id_export_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["content_items.id"],
            name=op.f("fk_deliveries_content_item_id_content_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["version_node_id"],
            ["version_nodes.id"],
            name=op.f("fk_deliveries_version_node_id_version_nodes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["replaces_delivery_id"],
            ["deliveries.id"],
            name=op.f("fk_deliveries_replaces_delivery_id_deliveries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["delivered_by"],
            ["users.id"],
            name=op.f("fk_deliveries_delivered_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_deliveries_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deliveries")),
    )
    op.create_index(
        "uq_deliveries_idempotency",
        "deliveries",
        ["project_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "uq_deliveries_replaces",
        "deliveries",
        ["replaces_delivery_id"],
        unique=True,
        postgresql_where=sa.text("replaces_delivery_id IS NOT NULL"),
    )
    op.create_index(
        "ix_deliveries_project_state", "deliveries", ["project_id", "state"]
    )
    op.create_index("ix_deliveries_export_item", "deliveries", ["export_item_id"])
    op.create_index(
        "ix_deliveries_content", "deliveries", ["project_id", "content_item_id"]
    )

    op.create_table(
        "publications",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("delivery_id", sa.UUID(), nullable=True),
        sa.Column("publication_key", sa.String(length=64), nullable=False),
        sa.Column("note_id", sa.Text(), nullable=True),
        sa.Column("note_url", sa.Text(), nullable=True),
        sa.Column("url_canonical", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("content_state", sa.String(length=20), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
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
        sa.Column(
            "lock_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False
        ),
        sa.CheckConstraint(
            "state IN ('DRAFT', 'PUBLISHED', 'UNKNOWN')",
            name=op.f("ck_publications_state_valid"),
        ),
        sa.CheckConstraint(
            "content_state IN ('VERIFIED', 'UNVERIFIED')",
            name=op.f("ck_publications_content_state_valid"),
        ),
        sa.CheckConstraint(
            "(state = 'PUBLISHED') = (published_at IS NOT NULL)",
            name=op.f("ck_publications_published_shape"),
        ),
        sa.CheckConstraint(
            "publication_key ~ '^[A-Za-z0-9_.\\-]{1,64}$'",
            name=op.f("ck_publications_publication_key_format"),
        ),
        sa.CheckConstraint(
            "char_length(request_hash) = 64 AND request_hash ~ '^[0-9a-f]+$'",
            name=op.f("ck_publications_request_hash_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_publications_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["deliveries.id"],
            name=op.f("fk_publications_delivery_id_deliveries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_publications_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publications")),
        sa.UniqueConstraint(
            "project_id", "publication_key", name="uq_publications_project_key"
        ),
    )
    op.create_index(
        "uq_publications_idempotency",
        "publications",
        ["project_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_publications_project_state", "publications", ["project_id", "state"]
    )
    op.create_index("ix_publications_canonical", "publications", ["url_canonical"])
    op.create_index("ix_publications_delivery", "publications", ["delivery_id"])

    op.create_table(
        "publication_links",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("publication_id", sa.UUID(), nullable=False),
        sa.Column("raw_url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("link_kind", sa.String(length=20), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "link_kind IN ('SHORT', 'LONG', 'OTHER')",
            name=op.f("ck_publication_links_link_kind_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["publications.id"],
            name=op.f("fk_publication_links_publication_id_publications"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publication_links")),
    )
    op.create_index(
        "ix_publication_links_publication", "publication_links", ["publication_id"]
    )
    # 刻意只建非唯一索引（见文件头 §二.4），不建 `uq_publication_links_canonical`。
    op.create_index(
        "ix_publication_links_canonical", "publication_links", ["canonical_url"]
    )
    op.create_index("ix_publication_links_raw", "publication_links", ["raw_url"])

    # 触发器：列级冻结在前，一致性在后。DDL 与测试/证据脚本同源。
    from rednote.modules.delivery import ddl as delivery_ddl

    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in delivery_ddl.all_trigger_ddl():
        op.execute(_function_ddl)
        op.execute(_trigger_ddl)


def downgrade() -> None:
    from rednote.modules.delivery import ddl as delivery_ddl

    # 1. 先删触发器（必须先于函数）。
    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in delivery_ddl.all_trigger_ddl():
        op.execute(_drop_trigger)

    # 2. 本包无整行不可变表（`IMMUTABLE_TABLES` 为空）⇒ `all_trigger_ddl()`
    #    枚举的函数**全部自有**，没有跨包共用的 `rednote_reject_mutation`
    #    混进来，可以直接按它逐一删——与其它迁移"只删 consistency_items()"
    #    的写法不同，是因为那些迁移的 all_trigger_ddl() 混了共用函数。
    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in delivery_ddl.all_trigger_ddl():
        op.execute(_drop_function)

    op.drop_index("ix_publication_links_raw", table_name="publication_links")
    op.drop_index("ix_publication_links_canonical", table_name="publication_links")
    op.drop_index(
        "ix_publication_links_publication", table_name="publication_links"
    )
    op.drop_table("publication_links")

    op.drop_index("ix_publications_delivery", table_name="publications")
    op.drop_index("ix_publications_canonical", table_name="publications")
    op.drop_index("ix_publications_project_state", table_name="publications")
    op.drop_index(
        "uq_publications_idempotency",
        table_name="publications",
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.drop_table("publications")

    op.drop_index("ix_deliveries_content", table_name="deliveries")
    op.drop_index("ix_deliveries_export_item", table_name="deliveries")
    op.drop_index("ix_deliveries_project_state", table_name="deliveries")
    op.drop_index(
        "uq_deliveries_replaces",
        table_name="deliveries",
        postgresql_where=sa.text("replaces_delivery_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_deliveries_idempotency",
        table_name="deliveries",
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.drop_table("deliveries")

    op.drop_index("ix_koc_records_home_canonical", table_name="koc_records")
    op.drop_index("ix_koc_records_project", table_name="koc_records")
    op.drop_table("koc_records")
