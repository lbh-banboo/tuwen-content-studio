"""WP13：任务、执行尝试与通用幂等记录（3 张表）。

revision: 0894cc890962
down_revision: fa5fe0facfb1

**本迁移包含三张表**（数据结构文档 02 §11 里属于 WP13 的部分）：

| 表 | 作用 |
|---|---|
| `jobs` | 任务的**业务状态权威**（§11 原文），含租约三列（`lease_owner` / `lease_until` / `fence_token`） |
| `job_attempts` | 每次执行尝试的记录；"迟到 attempt 不可覆盖新结果"由 `jobs.fence_token` 的条件写回实现，本表负责留下"发生过哪些尝试" |
| `idempotency_records` | 通用幂等（协议 §1.3）：同 key 同 hash 回放、同 key 不同内容 409 |

**§11 的其余表不在这里**，它们分属别的包：
  · `providers` / `model_profiles` / `provider_calls` / `capability_policies` → WP16（模型网关）
  · `pricing_versions` / `cost_estimates` / `project_budgets` / `budget_reservations` / `usage_ledger` → WP25（费用）
  · `domain_events` → WP14（SSE）；`audit_events` / `notification_receipts` → WP15；`runtime_settings` → WP24
  · `outbox_events` → **已由 WP12 建**（协议 §4.1 要求完成上传时同事务写），本包只做它的消费者

**两个部分索引的 WHERE 必须保留**（`ix_jobs_lease_until` / `ix_idempotency_expires_at`）：
  · `ix_jobs_lease_until ... WHERE lease_until IS NOT NULL` —— 少了它，
    索引会包含全部未领取的任务（绝大多数行的 `lease_until` 为空），
    回收过期租约的扫描退化成全表扫描；
  · `ix_idempotency_expires_at ... WHERE expires_at IS NOT NULL` —— 少了它，
    **永久保留的记录（任务关联）会被扫进来并被误删**，
    而删掉它们意味着"同一任务在第 8 天再次计费"（协议 §1.3 明确要求避免）。
  （WP10 与 WP12 都踩过同类坑：部分索引少了 WHERE 会静默退化。）

**关于 `input_version_ids` 的默认值**：`server_default = text("'{}'")` 与模型保持一致
（空数组字面量，PG 按列类型隐式转换）。两边写不同文本会让 autogenerate
每次都比出差异、反复提议重建默认值。

---

## 【铁律 G2 —— 每次 autogenerate 后必读】

本次 autogenerate **第四次**提议删除 `smoke_meta` 与 `wp05_vec_probe`
（M0 冒烟脚本建的探针表，不在 SQLAlchemy 模型里）。
已人工删除全部 `op.drop_table` / `op.drop_index` 语句。

**这两张探针表不属于本项目模型，永远不要删**：WP11、WP12 的每一次迁移
都用它们的行数作为"残留表未被误删"的证据（`evidence/m1/*-versions.log` 的核对项）。
来源：登记册 §4.3 的 ISS-13 / ISS-14，以及 ISS-32（同一问题的第三次复现）。

**另外两处**（前两次也出现过，已一并修掉）：
  · autogenerate 生成的 `downgrade` 又一次试图**重建**这两张表，
    且 `wp05_vec_probe.embedding` 被渲染成 `sa.NullType()`
    （autogenerate 不识别 `vector` 类型）—— 重建出来的结构是错的，已整段删除；
  · 生成的文件用了 `postgresql.JSONB` 与 `postgresql.ARRAY`，
    但文件头**没有** `from sqlalchemy.dialects import postgresql`
    —— 执行 `upgrade` 会直接 `NameError`。已补上。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
# 用 repr() 而非 "Ellipsis"：字符串插值会把 Python 的 None 渲染成字符串 "None"，
# 导致 Alembic 解析 down_revision 时 KeyError: 'None'（本机实测踩到过）。
revision: str = "0894cc890962"
down_revision: str | Sequence[str] | None = "fa5fe0facfb1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """正向迁移：建 3 张表与它们的索引。

    本包不新增触发器：租约与 fence 的正确性靠**条件 UPDATE**
    （`WHERE fence_token = ?` / `WHERE state = ?`）表达 ——
    那些条件需要与业务字段出现在同一条语句里（还要 `RETURNING fence_token`），
    写成触发器反而会把判断逻辑拆到两个地方。
    """
    op.create_table(
        "idempotency_records",
        sa.Column("scope", sa.String(length=120), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column(
            "response_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("resource_id", sa.UUID(), nullable=True),
        # NULL = 永久保留（任务关联）。协议 §1.3 要求生产任务"以数据库业务
        # 请求标识永久关联，避免缓存过期造成同一任务再次计费"。
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
            "state IN ('COMPLETED', 'IN_PROGRESS')",
            name=op.f("ck_idempotency_records_state_valid"),
        ),
        sa.PrimaryKeyConstraint("scope", "key", name=op.f("pk_idempotency_records")),
    )
    # 部分索引：只扫有到期时间的（永久保留的不该被清理路径碰到）。
    op.create_index(
        "ix_idempotency_expires_at",
        "idempotency_records",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )

    op.create_table(
        "jobs",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("project_id", sa.UUID(), nullable=True),
        # batch_id / content_id **刻意不加外键**：batches / contents 表属
        # WP24 / WP29，现在建外键会指向不存在的表。它们是预留的关联位，
        # 待对应工作包落地时补 FK（已登记为待定项）。
        sa.Column("batch_id", sa.UUID(), nullable=True),
        sa.Column("content_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("queue", sa.String(length=40), nullable=False),
        sa.Column(
            "priority", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("idempotency_scope", sa.String(length=120), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "input_version_ids",
            postgresql.ARRAY(sa.UUID()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        # 单调递增；写回结果时必须匹配（数据结构 §13.4）。
        sa.Column(
            "fence_token", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("result_ref", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=60), nullable=True),
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
            "state IN ('CANCELLED', 'CANCEL_REQUESTED', 'FAILED', 'PARTIAL', "
            "'QUEUED', 'RUNNING', 'SUCCEEDED', 'WAITING_PROVIDER', "
            "'WAITING_RECONCILIATION')",
            name=op.f("ck_jobs_state_valid"),
        ),
        # 租约的两个字段必须同时有或同时无 —— 只有 owner 没有 until
        # 意味着"占着但永不到期"，那个任务会永远卡住。
        sa.CheckConstraint(
            "(lease_owner IS NULL) = (lease_until IS NULL)",
            name=op.f("ck_jobs_lease_pair_consistent"),
        ),
        sa.CheckConstraint(
            "fence_token >= 0", name=op.f("ck_jobs_fence_token_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_jobs_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint(
            "idempotency_scope", "idempotency_key", name="uq_jobs_scope_key"
        ),
    )
    op.create_index(
        "ix_jobs_claim", "jobs", ["queue", "state", "next_run_at"], unique=False
    )
    # 部分索引：未领取的任务 lease_until 为空，把它们排除在索引外。
    op.create_index(
        "ix_jobs_lease_until",
        "jobs",
        ["lease_until"],
        unique=False,
        postgresql_where=sa.text("lease_until IS NOT NULL"),
    )
    op.create_index("ix_jobs_project_id", "jobs", ["project_id"], unique=False)

    op.create_table(
        "job_attempts",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("worker_id", sa.String(length=120), nullable=False),
        sa.Column("fence_token", sa.BigInteger(), nullable=False),
        sa.Column("error_code", sa.String(length=60), nullable=True),
        # 费用对账的输入（WP25）：这次尝试是否还有未结的外部用量。
        sa.Column(
            "usage_pending",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('ABANDONED', 'FAILED', 'RUNNING', 'SUCCEEDED')",
            name=op.f("ck_job_attempts_state_valid"),
        ),
        sa.CheckConstraint(
            "attempt_no >= 1", name=op.f("ck_job_attempts_attempt_no_positive")
        ),
        sa.CheckConstraint(
            "fence_token >= 0", name=op.f("ck_job_attempts_fence_token_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_job_attempts_job_id_jobs"),
            # 尝试记录是任务的从属数据：任务被删时一并清理。
            # 与 WP11 的 version_nodes 不同（那里必须 RESTRICT）——
            # 任务不是"历史事实"，它可以被真正删除。
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_attempts")),
        sa.UniqueConstraint("job_id", "attempt_no", name="uq_job_attempts_job_attempt"),
    )
    op.create_index(
        "ix_job_attempts_job", "job_attempts", ["job_id", "attempt_no"], unique=False
    )


def downgrade() -> None:
    """反向迁移：**按依赖逆序**删表。

    `job_attempts` 必须先删（它 FK `jobs`）。虽然它的外键是 CASCADE，
    但这里仍显式先删：回滚演练需要的是**每一步都可核对**的状态，
    而不是"PG 帮我顺带删了一些行"。
    """
    op.drop_index("ix_job_attempts_job", table_name="job_attempts")
    op.drop_table("job_attempts")

    op.drop_index("ix_jobs_project_id", table_name="jobs")
    op.drop_index("ix_jobs_lease_until", table_name="jobs")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_table("jobs")

    op.drop_index("ix_idempotency_expires_at", table_name="idempotency_records")
    op.drop_table("idempotency_records")
