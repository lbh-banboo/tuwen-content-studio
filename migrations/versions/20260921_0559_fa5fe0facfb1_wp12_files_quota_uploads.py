"""WP12：文件、配额与分片上传（7 张表）。

revision: fa5fe0facfb1
down_revision: cedfe85c1bf9

**本迁移包含七张表**，覆盖数据结构文档 02 §4（文件、配额与解析）6 张 +
§11 的 `outbox_events` 1 张：

| 表 | 来源 | 作用 |
|---|---|---|
| `blobs` | §4 | 物理内容身份（sha256 + object_key），跨项目可共用 |
| `file_objects` | §4 | 逻辑文件（原名 + 可见性 + 上传人） |
| `project_file_links` | §4 | 项目↔文件↔用途；**研究配额按本表计算** |
| `quota_accounts` | §4 | 每项目账面：limit / effective / reserved |
| `upload_sessions` | §4 | 上传意图与状态机 |
| `upload_parts` | §4 | 已接收分片 |
| `outbox_events` | §11 | 完成上传时同事务写入的 `file.uploaded` 事件 |

**为什么 `outbox_events`（§11 的表）由 WP12 建**：协议 03 §4.1 的上传时序图
第 8 步写死了"固化文件 / 转换配额 / **Outbox**"三者同事务。若本包不写，
文件会算进配额却没有任何记录说它需要被扫描 —— 用户传的文件永不解析，
**且无任何报错**。界线是"有没有设计空间"而不是"表写在谁的章节"：
`outbox_events` 的列定义在 §11 已是完整的标准 Outbox 模式（无设计空间），
而 `jobs`（租约 / fence_token / 重试策略）有，故仍留给 WP13。

**两个部分唯一索引的 WHERE 必须保留**（第 118、119、151 行）：
  · `uq_project_file_links_active ... WHERE effective` —— 少了 WHERE，
    就退化成"同项目同文件同用途**历史**只能有一条"，于是"解绑后重新关联"
    会被唯一约束挡住，而重新关联是完全正常的操作。
  · `ix_upload_sessions_expires_at ... WHERE state IN ('UPLOADING','COMPLETING')` ——
    少了 WHERE，索引会包含全部已完结会话（只增不减），TTL 扫描的代价
    随时间线性上升。
  （WP10 踩过同类坑：`uq_project_members_active_owner` 少了 WHERE
  会退化成"每项目只能有一条成员记录"。）

---

## 【铁律 G2 —— 每次 autogenerate 后必读】

本次 autogenerate **又一次**提议删除 `smoke_meta` 与 `wp05_vec_probe`
（M0 冒烟脚本建的探针表，不在 SQLAlchemy 模型里）。
已人工删除全部 `op.drop_table` / `op.drop_index` 语句。

**这两张探针表不属于本项目模型，永远不要删**：本轮之前的每一次迁移
都用它们的行数作为"残留表未被误删"的证据
（`evidence/m1/wp11-versions.log` 里就有这条核对项）。
来源：`docs/待定项与问题登记册.md` §4.3 的 ISS-13 / ISS-14。

**另外**：autogenerate 生成的 `downgrade` 里试图**重建**这两张表，
且 `wp05_vec_probe.embedding` 被渲染成 `sa.NullType()`（autogenerate 无法
识别 `vector` 类型）—— 重建出的表结构是错的。已整段删除。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
# 用 repr() 而非 "Ellipsis"：字符串插值会把 Python 的 None 渲染成字符串 "None"，
# 导致 Alembic 解析 down_revision 时 KeyError: 'None'（本机实测踩到过）。
revision: str = "fa5fe0facfb1"
down_revision: str | Sequence[str] | None = "cedfe85c1bf9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """正向迁移：建 7 张表与它们的索引。

    表之间没有触发器（本包不新增数据库级守卫）：
    配额的 `effective + reserved <= limit` 不变量用 **CHECK** 表达就够
    （它只看当前行），不像 WP11 的跨行判断需要触发器。
    """
    op.create_table(
        "blobs",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("storage_backend", sa.String(length=20), nullable=False),
        sa.Column("bucket", sa.String(length=100), nullable=True),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column(
            "scan_status",
            sa.String(length=20),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column(
            "storage_state",
            sa.String(length=20),
            server_default=sa.text("'ACTIVE'"),
            nullable=False,
        ),
        sa.Column("retained_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("encryption_key_ref", sa.String(length=255), nullable=True),
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
        sa.CheckConstraint(
            "scan_status IN ('CLEAN', 'INFECTED', 'PENDING', 'SKIPPED')",
            name=op.f("ck_blobs_scan_status_valid"),
        ),
        sa.CheckConstraint(
            "storage_backend IN ('LOCAL', 'MINIO')",
            name=op.f("ck_blobs_storage_backend_valid"),
        ),
        sa.CheckConstraint(
            "storage_state IN ('ACTIVE', 'DELETED', 'PENDING_DELETE')",
            name=op.f("ck_blobs_storage_state_valid"),
        ),
        sa.CheckConstraint("byte_size >= 0", name=op.f("ck_blobs_byte_size_non_negative")),
        sa.CheckConstraint("char_length(sha256) = 64", name=op.f("ck_blobs_sha256_length")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blobs")),
        sa.UniqueConstraint(
            "storage_backend", "object_key", name="uq_blobs_storage_backend_object_key"
        ),
    )
    op.create_index(
        "ix_blobs_sha256", "blobs", ["storage_backend", "sha256"], unique=False
    )
    op.create_index("ix_blobs_storage_state", "blobs", ["storage_state"], unique=False)

    op.create_table(
        "outbox_events",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("aggregate_type", sa.String(length=50), nullable=False),
        sa.Column("aggregate_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column(
            "schema_version", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_outbox_events_attempts_non_negative")
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name=op.f("ck_outbox_events_schema_version_positive"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_events")),
    )
    op.create_index(
        "ix_outbox_events_aggregate",
        "outbox_events",
        ["aggregate_type", "aggregate_id"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL"),
    )

    op.create_table(
        "file_objects",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("blob_id", sa.UUID(), nullable=False),
        sa.Column("original_name", sa.String(length=512), nullable=False),
        sa.Column("visibility", sa.String(length=20), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("classification", sa.String(length=20), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploaded_by", sa.UUID(), nullable=False),
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
        sa.CheckConstraint(
            "(visibility = 'PROJECT' AND project_id IS NOT NULL)"
            " OR (visibility = 'KNOWLEDGE' AND project_id IS NULL)",
            name=op.f("ck_file_objects_visibility_matches_project"),
        ),
        sa.CheckConstraint(
            "classification IN ('SUPPORTED', 'UNSUPPORTED')",
            name=op.f("ck_file_objects_classification_valid"),
        ),
        sa.CheckConstraint(
            "visibility IN ('KNOWLEDGE', 'PROJECT')",
            name=op.f("ck_file_objects_visibility_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["blob_id"],
            ["blobs.id"],
            name=op.f("fk_file_objects_blob_id_blobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_file_objects_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
            name=op.f("fk_file_objects_uploaded_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_file_objects")),
    )
    op.create_index(
        "ix_file_objects_project_id", "file_objects", ["project_id"], unique=False
    )
    op.create_index(
        "ix_file_objects_uploaded_by", "file_objects", ["uploaded_by"], unique=False
    )

    op.create_table(
        "quota_accounts",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column(
            "limit_bytes",
            sa.BigInteger(),
            server_default=sa.text("300000000"),
            nullable=False,
        ),
        sa.Column(
            "effective_bytes",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "reserved_bytes",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
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
        sa.CheckConstraint(
            "effective_bytes + reserved_bytes <= limit_bytes",
            name=op.f("ck_quota_accounts_within_limit"),
        ),
        sa.CheckConstraint(
            "effective_bytes >= 0", name=op.f("ck_quota_accounts_effective_non_negative")
        ),
        sa.CheckConstraint(
            "limit_bytes >= 0", name=op.f("ck_quota_accounts_limit_non_negative")
        ),
        sa.CheckConstraint(
            "reserved_bytes >= 0", name=op.f("ck_quota_accounts_reserved_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_quota_accounts_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("project_id", name=op.f("pk_quota_accounts")),
    )

    op.create_table(
        "project_file_links",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("file_id", sa.UUID(), nullable=False),
        sa.Column("purpose", sa.String(length=20), nullable=False),
        sa.Column(
            "effective", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
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
        sa.CheckConstraint(
            "purpose IN ('BRIEF', 'EXPORT', 'GENERATED', 'PROOF', 'REFERENCE', "
            "'RESEARCH')",
            name=op.f("ck_project_file_links_purpose_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["file_objects.id"],
            name=op.f("fk_project_file_links_file_id_file_objects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_file_links_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_file_links")),
    )
    op.create_index(
        "ix_project_file_links_file_id",
        "project_file_links",
        ["file_id"],
        unique=False,
    )
    # 部分索引：WHERE 必须保留，理由见模块 docstring。
    op.create_index(
        "ix_project_file_links_quota",
        "project_file_links",
        ["project_id", "purpose"],
        unique=False,
        postgresql_where=sa.text("effective"),
    )
    op.create_index(
        "uq_project_file_links_active",
        "project_file_links",
        ["project_id", "file_id", "purpose"],
        unique=True,
        postgresql_where=sa.text("effective"),
    )

    op.create_table(
        "upload_sessions",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("purpose", sa.String(length=20), nullable=False),
        sa.Column("original_name", sa.String(length=512), nullable=False),
        sa.Column("classification", sa.String(length=20), nullable=False),
        sa.Column("declared_size", sa.BigInteger(), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "received_bytes",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=True),
        sa.Column("actual_sha256", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("temporary_key", sa.String(length=512), nullable=False),
        sa.Column("file_id", sa.UUID(), nullable=True),
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
        sa.CheckConstraint(
            "(state = 'COMPLETED') = (file_id IS NOT NULL)",
            name=op.f("ck_upload_sessions_completed_has_file"),
        ),
        sa.CheckConstraint(
            "classification IN ('SUPPORTED', 'UNSUPPORTED')",
            name=op.f("ck_upload_sessions_classification_valid"),
        ),
        sa.CheckConstraint(
            "purpose IN ('BRIEF', 'EXPORT', 'GENERATED', 'PROOF', 'REFERENCE', "
            "'RESEARCH')",
            name=op.f("ck_upload_sessions_purpose_valid"),
        ),
        sa.CheckConstraint(
            "state IN ('CANCELLED', 'COMPLETED', 'COMPLETING', 'EXPIRED', 'FAILED', "
            "'UPLOADING')",
            name=op.f("ck_upload_sessions_state_valid"),
        ),
        sa.CheckConstraint(
            "declared_size >= 0",
            name=op.f("ck_upload_sessions_declared_size_non_negative"),
        ),
        sa.CheckConstraint(
            "received_bytes >= 0",
            name=op.f("ck_upload_sessions_received_bytes_non_negative"),
        ),
        sa.CheckConstraint(
            "reserved_bytes >= 0",
            name=op.f("ck_upload_sessions_reserved_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_upload_sessions_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["file_objects.id"],
            name=op.f("fk_upload_sessions_file_id_file_objects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_upload_sessions_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_upload_sessions")),
    )
    op.create_index(
        "ix_upload_sessions_created_by",
        "upload_sessions",
        ["created_by"],
        unique=False,
    )
    op.create_index(
        "ix_upload_sessions_expires_at",
        "upload_sessions",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("state IN ('UPLOADING', 'COMPLETING')"),
    )
    op.create_index(
        "ix_upload_sessions_project_id",
        "upload_sessions",
        ["project_id"],
        unique=False,
    )

    op.create_table(
        "upload_parts",
        sa.Column("upload_id", sa.UUID(), nullable=False),
        sa.Column("part_no", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("storage_etag", sa.String(length=255), nullable=True),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "byte_size >= 0", name=op.f("ck_upload_parts_byte_size_non_negative")
        ),
        sa.CheckConstraint("part_no >= 1", name=op.f("ck_upload_parts_part_no_positive")),
        sa.ForeignKeyConstraint(
            ["upload_id"],
            ["upload_sessions.id"],
            name=op.f("fk_upload_parts_upload_id_upload_sessions"),
            # 分片是临时数据（不是历史事实）：会话被删时随之清理是正确语义。
            # 与 WP11 的 version_nodes 不同 —— 那里必须 RESTRICT，
            # 因为历史版本不允许被级联删除。
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("upload_id", "part_no", name=op.f("pk_upload_parts")),
    )


def downgrade() -> None:
    """反向迁移：**按依赖逆序**删表（先子表、后父表）。

    顺序不可随意调整：`project_file_links` 与 `upload_sessions` 都 FK
    `file_objects`，而 `file_objects` 又 FK `blobs`。若先删 `blobs`，
    PG 会因为仍被引用而拒绝（RESTRICT）。

    `upload_parts` 排在最前（它是 `upload_sessions` 的子表），
    且它的外键是 CASCADE —— 但这里仍显式先删它：
    依赖 CASCADE 意味着"删父表顺带删子表"，而回滚演练需要的是
    **每一步都可核对**的状态，不是"PG 帮我删了一些东西"。
    """
    op.drop_table("upload_parts")
    op.drop_index("ix_upload_sessions_project_id", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_expires_at", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_created_by", table_name="upload_sessions")
    op.drop_table("upload_sessions")

    op.drop_index("uq_project_file_links_active", table_name="project_file_links")
    op.drop_index("ix_project_file_links_quota", table_name="project_file_links")
    op.drop_index("ix_project_file_links_file_id", table_name="project_file_links")
    op.drop_table("project_file_links")

    op.drop_table("quota_accounts")

    op.drop_index("ix_file_objects_uploaded_by", table_name="file_objects")
    op.drop_index("ix_file_objects_project_id", table_name="file_objects")
    op.drop_table("file_objects")

    op.drop_index("ix_outbox_events_pending", table_name="outbox_events")
    op.drop_index("ix_outbox_events_aggregate", table_name="outbox_events")
    op.drop_table("outbox_events")

    op.drop_index("ix_blobs_storage_state", table_name="blobs")
    op.drop_index("ix_blobs_sha256", table_name="blobs")
    op.drop_table("blobs")
