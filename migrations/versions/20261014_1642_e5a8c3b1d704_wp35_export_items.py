"""WP35：建 `export_items`（固定版本 ZIP 导出，形态 A'）。

revision: e5a8c3b1d704
down_revision: c4d9f2a17e35

## 一、本迁移做什么

建一张表 `export_items`，及其 1 个不可变触发器、1 个组一致性延迟触发器、
9 条 CHECK、4 条索引。M4 DDL 草案 §1.3 原属 WP36，2026-10-14 老大裁定
挪入 WP35（M4 总数仍 19 张）。

## 二、与草案的差异（均已回填 `docs/m4/M4-DDL草案.md` §1.3）

| 草案 | 本迁移 | 为什么 |
|---|---|---|
| 有 `delivery_id` FK | **没有** | `deliveries` 属 WP36、迁移序晚于本表，FK 无处可指；WP36 反向引用 |
| 无分组 | `export_group_id UUID NOT NULL` | 粒度改为"项目内一组 content_id"，形态 A' |
| `artifact_ref TEXT` | `artifact_file_id UUID FK file_objects` | 产物走 `files` 模块落盘，引用 `file_objects` 而不是裸 key |

## 三、G2 铁律执行记录

1. **不删任何非本迁移创建的对象**：`downgrade` 只删本表的 2 个触发器、
   1 个函数（`rednote_check_export_group_consistency`）与表本身；
   **不碰** `rednote_reject_mutation()`（跨包共用，ISS-79）。
2. **`op.f()` 包 CHECK 名**：与既有迁移同款，裸名会被 naming convention 再加一次前缀。
3. **取值域写化石字面量**，不从 `exports.types` 取（ISS-191）。
   迁移文本是"建库那一刻的快照"，此后 `types` 怎么改都不再影响老库。
4. 触发器 DDL 直接 import `rednote.modules.exports.ddl`（与 WP27 迁移同一做法：
   触发器 SQL 与测试、证据脚本同源，不手写第二份）。

## 四、⚠️ `downgrade` 有数据损失

导出记录是"当时导出了哪一版"的唯一凭据；回退前须导出全量。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e5a8c3b1d704"
down_revision: str | None = "c4d9f2a17e35"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "export_items",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("export_group_id", sa.UUID(), nullable=False),
        sa.Column("content_item_id", sa.UUID(), nullable=False),
        sa.Column("version_node_id", sa.UUID(), nullable=False),
        sa.Column("eligibility_hash", sa.String(length=64), nullable=False),
        sa.Column("export_kind", sa.String(length=20), nullable=False),
        sa.Column("export_format", sa.String(length=20), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("artifact_file_id", sa.UUID(), nullable=False),
        sa.Column("zip_sha256", sa.String(length=64), nullable=False),
        sa.Column("zip_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "export_kind IN ('SINGLE', 'BATCH_ZIP')",
            name=op.f("ck_export_items_export_kind_valid"),
        ),
        sa.CheckConstraint(
            "export_format IN ('STATIC')",
            name=op.f("ck_export_items_export_format_valid"),
        ),
        sa.CheckConstraint(
            "item_count BETWEEN 1 AND 50",
            name=op.f("ck_export_items_item_count_range"),
        ),
        sa.CheckConstraint(
            "(export_kind = 'SINGLE') = (item_count = 1)",
            name=op.f("ck_export_items_export_kind_matches_count"),
        ),
        sa.CheckConstraint(
            "zip_byte_size > 0",
            name=op.f("ck_export_items_zip_byte_size_positive"),
        ),
        sa.CheckConstraint(
            "char_length(zip_sha256) = 64 AND zip_sha256 ~ '^[0-9a-f]+$'",
            name=op.f("ck_export_items_zip_sha256_shape"),
        ),
        sa.CheckConstraint(
            "char_length(eligibility_hash) = 64 AND eligibility_hash ~ '^[0-9a-f]+$'",
            name=op.f("ck_export_items_eligibility_hash_shape"),
        ),
        sa.CheckConstraint(
            "char_length(request_hash) = 64 AND request_hash ~ '^[0-9a-f]+$'",
            name=op.f("ck_export_items_request_hash_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_export_items_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["content_items.id"],
            name=op.f("fk_export_items_content_item_id_content_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["version_node_id"],
            ["version_nodes.id"],
            name=op.f("fk_export_items_version_node_id_version_nodes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_file_id"],
            ["file_objects.id"],
            name=op.f("fk_export_items_artifact_file_id_file_objects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_export_items_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_export_items")),
        sa.UniqueConstraint(
            "export_group_id",
            "content_item_id",
            name=op.f("uq_export_items_group_content"),
        ),
    )
    op.create_index("ix_export_items_group", "export_items", ["export_group_id"])
    op.create_index(
        "ix_export_items_project_created",
        "export_items",
        ["project_id", "created_at"],
    )
    op.create_index("ix_export_items_version", "export_items", ["version_node_id"])
    op.create_index(
        "uq_export_items_idempotency",
        "export_items",
        ["project_id", "idempotency_key", "content_item_id"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    # 触发器：不可变保护在前，组一致性在后。DDL 与测试/证据脚本同源。
    from rednote.modules.exports import ddl as exports_ddl

    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in exports_ddl.all_trigger_ddl():
        op.execute(_function_ddl)
        op.execute(_trigger_ddl)


def downgrade() -> None:
    from rednote.modules.exports import ddl as exports_ddl

    # 1. 先删触发器（必须先于函数）。
    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in exports_ddl.all_trigger_ddl():
        op.execute(_drop_trigger)

    # 2. 只删本模块自己的函数；**不得**用 all_trigger_ddl()——
    #    其不可变项的 drop_function 指向跨包共用的 rednote_reject_mutation()。
    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in exports_ddl.consistency_items():
        op.execute(_drop_function)

    # 3. 索引（反序）→ 表。
    op.drop_index("uq_export_items_idempotency", table_name="export_items")
    op.drop_index("ix_export_items_version", table_name="export_items")
    op.drop_index("ix_export_items_project_created", table_name="export_items")
    op.drop_index("ix_export_items_group", table_name="export_items")
    op.drop_table("export_items")
