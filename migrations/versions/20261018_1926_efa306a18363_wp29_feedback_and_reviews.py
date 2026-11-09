"""WP29：预览/编辑/反馈定位 —— 建 `content_feedback_items` / `content_feedback_events` / `content_reviews`。

revision: efa306a18363
down_revision: a9d3f6c2b815

## 一、本迁移做什么

建三张表（1 张可变 + 2 张不可变）、2 个不可变触发器（挂在
`content_feedback_events` / `content_reviews` 上）、1 个锚点一致性延迟触发器
（挂在 `content_feedback_items` 上）。DDL 与测试/证据脚本同源
（`rednote.modules.feedback.ddl`），本文件只 `op.execute` 它生成的 SQL。

## 二、G2 铁律执行记录

1. **不删任何非本迁移创建的对象**：`downgrade` 只删本模块自己的 3 个触发器、
   1 个函数（`rednote_check_feedback_anchor_consistency`）与三张表；
   **不碰** `rednote_reject_mutation()`（跨包共用，ISS-79）。
2. **`op.f()` 包 CHECK/FK 名**：与既有迁移同款，裸名会被 naming convention
   再加一次前缀——**唯二例外**是 `content_feedback_events` 上两条指向
   `content_feedback_items` / `content_versions` 的 FK：按约定自动生成的全名
   分别是 66 / 64 字节，超过 PostgreSQL 63 字节标识符上限会被库静默截断
   并追加哈希，导致 ORM 元数据里的约束名与库里的实际名字永久不一致
   （已在 `modules/feedback/models.py` 用显式 `name=` 缩短，本迁移原样复刻
   那两个缩短后的名字，**不用 `op.f()` 包**——`op.f()` 只是加前缀，
   不解决长度问题，且这两个名字已经是最终形态）。
3. **取值域写化石字面量**，不从 `feedback.types` 取（ISS-191）：迁移文本是
   "建库那一刻的快照"，此后 `types` 怎么改都不再影响老库。
4. 触发器 DDL 直接 import `rednote.modules.feedback.ddl`（与 WP27/WP35 迁移
   同一做法：触发器 SQL 与测试、证据脚本同源，不手写第二份）。

## 三、与模型的对应关系

`sa.Column`/`sa.CheckConstraint`/`sa.ForeignKeyConstraint`/`sa.Index` 逐项对应
`services/backend/src/rednote/modules/feedback/models.py` 的
`ContentFeedbackItem` / `ContentFeedbackEvent` / `ContentReview` 三个类，
列名、约束、索引**逐一照抄**，不是独立设计。

## 四、⚠️ `downgrade` 有数据损失

反馈与审核记录是"运营对这篇内容提过什么意见、谁批准过"的唯一凭据；
回退前须导出全量。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "efa306a18363"
down_revision: str | None = "a9d3f6c2b815"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "content_feedback_items",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("content_item_id", sa.UUID(), nullable=False),
        sa.Column("observed_version_id", sa.UUID(), nullable=False),
        sa.Column("anchor_kind", sa.String(length=20), nullable=False),
        sa.Column("paragraph_ref", sa.String(length=200), nullable=True),
        sa.Column("selected_text", sa.Text(), nullable=True),
        sa.Column("media_item_id", sa.UUID(), nullable=True),
        sa.Column("category", sa.String(length=24), nullable=False),
        sa.Column("suggested_category", sa.String(length=24), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("scope", sa.String(length=24), nullable=False),
        sa.Column(
            "state",
            sa.String(length=20),
            server_default=sa.text("'OPEN'"),
            nullable=False,
        ),
        sa.Column("author_kind", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("reject_reason", sa.String(length=500), nullable=True),
        sa.Column("resolved_version_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
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
            "lock_version",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "anchor_kind IN ('MEDIA', 'PARAGRAPH', 'WHOLE')",
            name=op.f("ck_content_feedback_items_anchor_kind_valid"),
        ),
        sa.CheckConstraint(
            "category IN ('DUPLICATE', 'FACT', 'MEDIA_CONSISTENCY', 'OTHER',"
            " 'SCENE', 'STRUCTURE', 'TECH_FORMAT', 'TONE')",
            name=op.f("ck_content_feedback_items_category_valid"),
        ),
        sa.CheckConstraint(
            "suggested_category IS NULL OR suggested_category IN ('DUPLICATE',"
            " 'FACT', 'MEDIA_CONSISTENCY', 'OTHER', 'SCENE', 'STRUCTURE',"
            " 'TECH_FORMAT', 'TONE')",
            name=op.f("ck_content_feedback_items_suggested_category_valid"),
        ),
        sa.CheckConstraint(
            "severity IN ('BLOCKING', 'INFO', 'WARNING')",
            name=op.f("ck_content_feedback_items_severity_valid"),
        ),
        sa.CheckConstraint(
            "scope IN ('CURRENT_ONLY', 'FUTURE_GENERATIONS')",
            name=op.f("ck_content_feedback_items_scope_valid"),
        ),
        sa.CheckConstraint(
            "state IN ('IN_PROGRESS', 'OPEN', 'REJECTED', 'RESOLVED')",
            name=op.f("ck_content_feedback_items_state_valid"),
        ),
        sa.CheckConstraint(
            "author_kind IN ('AI', 'USER')",
            name=op.f("ck_content_feedback_items_author_kind_valid"),
        ),
        sa.CheckConstraint(
            "(anchor_kind <> 'PARAGRAPH') OR (paragraph_ref IS NOT NULL AND"
            " media_item_id IS NULL)",
            name=op.f("ck_content_feedback_items_anchor_paragraph_shape"),
        ),
        sa.CheckConstraint(
            "(anchor_kind <> 'MEDIA') OR (media_item_id IS NOT NULL AND"
            " paragraph_ref IS NULL)",
            name=op.f("ck_content_feedback_items_anchor_media_shape"),
        ),
        sa.CheckConstraint(
            "(anchor_kind <> 'WHOLE') OR (paragraph_ref IS NULL AND"
            " media_item_id IS NULL)",
            name=op.f("ck_content_feedback_items_anchor_whole_shape"),
        ),
        sa.CheckConstraint(
            "selected_text IS NULL OR char_length(selected_text) <= 4000",
            name=op.f("ck_content_feedback_items_selected_text_length"),
        ),
        sa.CheckConstraint(
            "char_length(body) > 0 AND char_length(body) <= 2000",
            name=op.f("ck_content_feedback_items_body_length"),
        ),
        sa.CheckConstraint(
            "(state = 'REJECTED') = (reject_reason IS NOT NULL)",
            name=op.f("ck_content_feedback_items_reject_reason_paired"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_content_feedback_items_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["content_items.id"],
            name=op.f("fk_content_feedback_items_content_item_id_content_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["observed_version_id"],
            ["content_versions.id"],
            name=op.f(
                "fk_content_feedback_items_observed_version_id_content_versions"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["media_item_id"],
            ["media_items.id"],
            name=op.f("fk_content_feedback_items_media_item_id_media_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_version_id"],
            ["content_versions.id"],
            name=op.f(
                "fk_content_feedback_items_resolved_version_id_content_versions"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_content_feedback_items_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_feedback_items")),
    )
    op.create_index(
        "ix_content_feedback_items_content_state",
        "content_feedback_items",
        ["content_item_id", "state"],
    )
    op.create_index(
        "ix_content_feedback_items_project_state",
        "content_feedback_items",
        ["project_id", "state"],
    )
    op.create_index(
        "ix_content_feedback_items_content_paragraph",
        "content_feedback_items",
        ["content_item_id", "paragraph_ref"],
    )
    op.create_index(
        "ix_content_feedback_items_media",
        "content_feedback_items",
        ["media_item_id"],
    )
    op.create_index(
        "ix_content_feedback_items_observed_version",
        "content_feedback_items",
        ["observed_version_id"],
    )

    op.create_table(
        "content_feedback_events",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("feedback_item_id", sa.UUID(), nullable=False),
        sa.Column("from_state", sa.String(length=20), nullable=True),
        sa.Column("to_state", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("resulting_version_id", sa.UUID(), nullable=True),
        sa.Column("seq_no", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor_kind IN ('AI', 'USER')",
            name=op.f("ck_content_feedback_events_actor_kind_valid"),
        ),
        # 这两条 FK 名是**手动缩短过的最终形态**（见模块二 ②），不用 `op.f()`：
        # `op.f()` 只加命名约定前缀，这两个名字已经带前缀，再包会重复加一次。
        sa.ForeignKeyConstraint(
            ["feedback_item_id"],
            ["content_feedback_items.id"],
            name="fk_content_feedback_events_feedback_item_id_feedback_items",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_content_feedback_events_actor_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resulting_version_id"],
            ["content_versions.id"],
            name="fk_content_feedback_events_resulting_version_id_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_feedback_events")),
        sa.UniqueConstraint(
            "feedback_item_id",
            "seq_no",
            name=op.f("uq_content_feedback_events_item_seq"),
        ),
    )

    op.create_table(
        "content_reviews",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("content_item_id", sa.UUID(), nullable=False),
        sa.Column("subject_version_id", sa.UUID(), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.UUID(), nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "supersedes_id",
            sa.UUID(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('APPROVED', 'ELIMINATED')",
            name=op.f("ck_content_reviews_decision_valid"),
        ),
        sa.CheckConstraint(
            "char_length(reason) > 0",
            name=op.f("ck_content_reviews_reason_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_content_reviews_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["content_items.id"],
            name=op.f("fk_content_reviews_content_item_id_content_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subject_version_id"],
            ["content_versions.id"],
            name=op.f("fk_content_reviews_subject_version_id_content_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"],
            ["users.id"],
            name=op.f("fk_content_reviews_decided_by_users"),
            ondelete="RESTRICT",
        ),
        # 自引用、延迟：改判可以在同一事务里先插新行再回填，顺序不重要
        # （与 `content_items.current_version_id` 同款，models.py 已注明）。
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["content_reviews.id"],
            name=op.f("fk_content_reviews_supersedes_id_content_reviews"),
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_reviews")),
    )
    op.create_index(
        "ix_content_reviews_content_decided",
        "content_reviews",
        ["content_item_id", "decided_at"],
    )
    op.create_index(
        "ix_content_reviews_project_decided",
        "content_reviews",
        ["project_id", "decided_at"],
    )
    op.create_index(
        "ix_content_reviews_subject_version",
        "content_reviews",
        ["subject_version_id"],
    )

    # 触发器：不可变保护在前，锚点一致性在后。DDL 与测试/证据脚本同源。
    from rednote.modules.feedback import ddl as feedback_ddl

    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in feedback_ddl.all_trigger_ddl():
        op.execute(_function_ddl)
        op.execute(_trigger_ddl)


def downgrade() -> None:
    from rednote.modules.feedback import ddl as feedback_ddl

    # 1. 先删触发器（必须先于函数）。
    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in feedback_ddl.all_trigger_ddl():
        op.execute(_drop_trigger)

    # 2. 只删本模块自己的函数；**不得**用 all_trigger_ddl()——
    #    其不可变项的 drop_function 指向跨包共用的 rednote_reject_mutation()。
    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in feedback_ddl.consistency_items():
        op.execute(_drop_function)

    # 3. 索引 → 表（`content_reviews` 先于 `content_feedback_items`：
    #    前者没有指向后者的 FK，但按与建表相反的顺序删更保守）。
    op.drop_index("ix_content_reviews_subject_version", table_name="content_reviews")
    op.drop_index("ix_content_reviews_project_decided", table_name="content_reviews")
    op.drop_index("ix_content_reviews_content_decided", table_name="content_reviews")
    op.drop_table("content_reviews")

    op.drop_table("content_feedback_events")

    op.drop_index(
        "ix_content_feedback_items_observed_version",
        table_name="content_feedback_items",
    )
    op.drop_index(
        "ix_content_feedback_items_media", table_name="content_feedback_items"
    )
    op.drop_index(
        "ix_content_feedback_items_content_paragraph",
        table_name="content_feedback_items",
    )
    op.drop_index(
        "ix_content_feedback_items_project_state",
        table_name="content_feedback_items",
    )
    op.drop_index(
        "ix_content_feedback_items_content_state",
        table_name="content_feedback_items",
    )
    op.drop_table("content_feedback_items")
