"""WP16：模型网关（5 张表 + 1 个不可变触发器）。

revision: 515fb8b0d31a
down_revision: 005e53eaed23

**本迁移包含 5 张表**：

| 对象 | 来源 | 说明 |
|---|---|---|
| `providers` | 数据结构 §11 | 供应商与**网络模式**（LOCAL/EXTERNAL）；**不存密钥正文** |
| `model_profiles` | §11 | 模型档案（模型名 / 修订 / 能力 / 参数 schema） |
| `capability_policies` | §11 | 项目级**出网策略**；`external_policy` 列默认 `LOCAL_ONLY`（缺省拒绝） |
| `provider_calls` | §11 | 外部调用记录；`UQ(provider_id, request_key)` 是"未知状态不盲重试"的地基 |
| `gateway_egress` | ADR-10「网关出网记录」 | 出网留痕（摘要 + 提示词版本 + 字段类别 + traceId）；**追加且不可篡改** |
| `trg_gateway_egress_immutable` | ADR-10 凭据性质 | 拦 UPDATE / DELETE |

**本迁移刻意不建的表**（同属 §11，但属其它包或未定）：
`pricing_versions` / `cost_estimates` / `project_budgets` / `budget_reservations` /
`usage_ledger`（**WP25** 费用体系）、`job_dependencies`（WP32）、`runtime_settings`（无包认领）。

**为什么出网留痕有自己的表而不是复用 WP15 的 `audit_events`**：
审计表有一条 `hash_pair_consistent` CHECK（`before_hash` / `after_hash` 必须成对），
那是为"**变更**记录"设的；出网留痕是**事件**，没有 before。
硬塞进去只能靠伪造一个 before 来满足约束 —— 那是"让数据适配约束"的反面做法。
详见 `services/backend/src/rednote/modules/gateway/models.py` 的模块 docstring 与 ISS-47。

**G2 铁律第七次触发**：autogenerate 又一次提议
`op.drop_table('smoke_meta')` / `op.drop_table('wp05_vec_probe')`，
并在 `downgrade` 里试图重建它们（`embedding` 被渲染成 `sa.NullType()`）。
这些语句**已全部人工删除** —— 那两张探针表不属于本项目模型，
是"残留表未被误删"这条核对的凭据（见 §4.3 的 ISS-13 / ISS-14 / ISS-32）。
文件头另补了 `postgresql` 导入（本迁移用了 `postgresql.JSONB` / `postgresql.ARRAY`，
缺了执行即 `NameError` —— ISS-32 的教训）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from rednote.modules.gateway.ddl import all_trigger_ddl

# revision identifiers, used by Alembic.
# 用 repr() 而非 "Ellipsis"：字符串插值会把 Python 的 None 渲染成字符串 "None"，
# 导致 Alembic 解析 down_revision 时 KeyError: 'None'（本机实测踩到过）。
revision: str = "515fb8b0d31a"
down_revision: str | Sequence[str] | None = "005e53eaed23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CAPABILITY_JSON = (
    '\'["EMBEDDING", "LIVE_MOTION", "RERANK", "STATIC_IMAGE", '
    '"TEXT_STRUCTURED", "VISION_CHECK"]\'::jsonb'
)
_DATA_CLASS_JSON = (
    '\'["COLLECTION", "EMBEDDING", "LIVE_MOTION", "STATIC_IMAGE", '
    '"TEXT_STRUCTURED"]\'::jsonb'
)


def upgrade() -> None:
    """正向迁移：建 5 张表 → 建索引 → 建不可变触发器。"""
    _create_providers()
    _create_model_profiles()
    _create_capability_policies()
    _create_provider_calls()
    _create_gateway_egress()

    # 触发器最后建：它引用表，表必须已存在。
    for _table, function_ddl, trigger_ddl, _drop_trigger, _drop_function in (
        all_trigger_ddl()
    ):
        op.execute(sa.text(function_ddl))
        op.execute(sa.text(trigger_ddl))


def _create_providers() -> None:
    op.create_table(
        "providers",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "capability_types",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("network_mode", sa.String(length=20), nullable=False),
        sa.Column("endpoint", sa.String(length=300), nullable=False),
        sa.Column("secret_ref", sa.String(length=200), nullable=True),
        sa.Column(
            "status", sa.String(length=20), server_default="ACTIVE", nullable=False
        ),
        sa.Column(
            "policy_version", sa.String(length=50), server_default="v1", nullable=False
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
            "network_mode IN ('LOCAL', 'EXTERNAL')",
            name=op.f("ck_providers_network_mode_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')",
            name=op.f("ck_providers_status_valid"),
        ),
        # 取值域已在协议 §12.1 定死，因此用 CHECK 兜住 ——
        # 允许任意字符串会让同一类能力出现三种拼写，策略按类别比对时静默漏放行或漏拦。
        sa.CheckConstraint(
            f"jsonb_typeof(capability_types) = 'array' "
            f"AND capability_types <@ {_CAPABILITY_JSON}",
            name=op.f("ck_providers_capability_types_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_providers")),
        sa.UniqueConstraint("name", name="uq_providers_name"),
    )


def _create_model_profiles() -> None:
    op.create_table(
        "model_profiles",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("provider_id", sa.UUID(), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("revision", sa.String(length=100), nullable=True),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "parameters_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        # 计价版本属 WP25；只存引用，**不建外键**（目标表尚不存在）。
        sa.Column("pricing_version_id", sa.UUID(), nullable=True),
        sa.Column("local_weights_hash", sa.String(length=64), nullable=True),
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
            f"jsonb_typeof(capabilities) = 'array' "
            f"AND capabilities <@ {_CAPABILITY_JSON}",
            name=op.f("ck_model_profiles_capabilities_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name=op.f("fk_model_profiles_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_profiles")),
        # NULLS NOT DISTINCT：让"revision 未填"也纳入唯一性（PG 15+，本项目锁 PG 17）。
        sa.UniqueConstraint(
            "provider_id",
            "model_name",
            "revision",
            name="uq_model_profiles_provider_model_revision",
            postgresql_nulls_not_distinct=True,
        ),
    )


def _create_capability_policies() -> None:
    op.create_table(
        "capability_policies",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("policy_version", sa.String(length=50), nullable=False),
        # **列默认值是 LOCAL_ONLY**：缺省拒绝要同时落在列默认值与判定函数两处。
        sa.Column(
            "external_policy",
            sa.String(length=30),
            server_default="LOCAL_ONLY",
            nullable=False,
        ),
        sa.Column(
            "allowed_provider_ids",
            postgresql.ARRAY(sa.UUID()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column(
            "allowed_data_classes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "concurrency_limits",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
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
            "external_policy IN ('ALLOW_EXTERNAL', 'LOCAL_ONLY')",
            name=op.f("ck_capability_policies_external_policy_valid"),
        ),
        sa.CheckConstraint(
            f"jsonb_typeof(allowed_data_classes) = 'array' "
            f"AND allowed_data_classes <@ {_DATA_CLASS_JSON}",
            name=op.f("ck_capability_policies_allowed_data_classes_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_capability_policies_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capability_policies")),
        sa.UniqueConstraint(
            "project_id",
            "policy_version",
            name="uq_capability_policies_project_policy_version",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_capability_policies_project",
        "capability_policies",
        ["project_id", "created_at"],
        unique=False,
    )


def _create_provider_calls() -> None:
    op.create_table(
        "provider_calls",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("job_attempt_id", sa.UUID(), nullable=False),
        sa.Column("provider_id", sa.UUID(), nullable=False),
        sa.Column("request_key", sa.String(length=200), nullable=False),
        sa.Column("external_request_id", sa.String(length=200), nullable=True),
        sa.Column(
            "state", sa.String(length=20), server_default="PREPARED", nullable=False
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_evidence_ref", sa.String(length=200), nullable=False),
        sa.Column("response_evidence_ref", sa.String(length=200), nullable=True),
        sa.Column("status_unknown_reason", sa.String(length=200), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
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
            "state IN ('PREPARED', 'SUBMITTED', 'POLLING', 'SUCCEEDED', 'FAILED',"
            " 'UNKNOWN')",
            name=op.f("ck_provider_calls_state_valid"),
        ),
        # 允许终态保留 status_unknown_reason（核对后的历史）——
        # 写成"只有 UNKNOWN 允许有值"会让 reconcile 必然撞 CHECK（见 ISS-46）。
        sa.CheckConstraint(
            "status_unknown_reason IS NULL "
            "OR state IN ('UNKNOWN', 'SUCCEEDED', 'FAILED')",
            name=op.f("ck_provider_calls_status_unknown_reason_scope"),
        ),
        sa.ForeignKeyConstraint(
            ["job_attempt_id"],
            ["job_attempts.id"],
            name=op.f("fk_provider_calls_job_attempt_id_job_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name=op.f("fk_provider_calls_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_calls")),
        # **幂等键的唯一性 —— "未知状态不盲重试"的地基。**
        sa.UniqueConstraint(
            "provider_id", "request_key", name="uq_provider_calls_provider_request"
        ),
    )
    op.create_index(
        "ix_provider_calls_poll_due",
        "provider_calls",
        ["state", "next_poll_at"],
        unique=False,
        postgresql_where=sa.text("state IN ('SUBMITTED', 'POLLING')"),
    )
    op.create_index(
        "ix_provider_calls_provider_created",
        "provider_calls",
        ["provider_id", "created_at"],
        unique=False,
    )


def _create_gateway_egress() -> None:
    op.create_table(
        "gateway_egress",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("provider_call_id", sa.UUID(), nullable=True),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("provider_id", sa.UUID(), nullable=False),
        sa.Column("capability", sa.String(length=50), nullable=False),
        sa.Column(
            "data_classes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("blocked_reason", sa.String(length=50), nullable=True),
        sa.Column("policy_version", sa.String(length=50), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=True),
        sa.Column("trace_id", sa.String(length=100), nullable=False),
        # 出网内容的规范化摘要（**不是正文**）：最长的文本列是 64 位十六进制。
        sa.Column("payload_digest", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('ALLOWED', 'DENIED')",
            name=op.f("ck_gateway_egress_decision_valid"),
        ),
        # "允许却没留下调用记录"在结构上不可能 —— 那意味着凭据丢了。
        sa.CheckConstraint(
            "decision = 'DENIED' OR provider_call_id IS NOT NULL",
            name=op.f("ck_gateway_egress_allowed_requires_call"),
        ),
        sa.CheckConstraint(
            "(decision = 'DENIED') = (blocked_reason IS NOT NULL)",
            name=op.f("ck_gateway_egress_blocked_reason_consistent"),
        ),
        sa.CheckConstraint(
            f"jsonb_typeof(data_classes) = 'array' "
            f"AND data_classes <@ {_DATA_CLASS_JSON}",
            name=op.f("ck_gateway_egress_data_classes_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["provider_call_id"],
            ["provider_calls.id"],
            name=op.f("fk_gateway_egress_provider_call_id_provider_calls"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_gateway_egress_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name=op.f("fk_gateway_egress_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gateway_egress")),
    )
    op.create_index(
        "ix_gateway_egress_project_created",
        "gateway_egress",
        ["project_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_gateway_egress_trace", "gateway_egress", ["trace_id"], unique=False
    )


def downgrade() -> None:
    """反向迁移：先删自己的触发器，再按依赖倒序删表。

    **不删共用的触发器函数**：`rednote_reject_mutation()` 是所有不可变表
    共用的（WP11 的 `version_nodes` / `version_dependencies`、
    WP15 的 `audit_events` 都在用它）。本迁移的 `downgrade` 若执行
    `DROP FUNCTION`，PG 会因为 `audit_events` 的触发器仍依赖它而**拒绝删除** ——
    这正是 WP11 踩过的 "cannot drop function ... because other objects depend on it"。
    函数的生命周期归**引入它的迁移**（WP11/WP15）；Alembic 的逆序回滚
    会让那一步在最后发生。

    **删表顺序**：`gateway_egress` 引用 `provider_calls` 与 `providers`；
    `provider_calls` 引用 `providers` 与 `job_attempts`；`model_profiles` 引用 `providers`。
    顺序反了会被外键挡住。
    """
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_gateway_egress_immutable ON gateway_egress"
        )
    )

    op.drop_index("ix_gateway_egress_trace", table_name="gateway_egress")
    op.drop_index("ix_gateway_egress_project_created", table_name="gateway_egress")
    op.drop_table("gateway_egress")

    op.drop_index("ix_provider_calls_provider_created", table_name="provider_calls")
    op.drop_index(
        "ix_provider_calls_poll_due",
        table_name="provider_calls",
        postgresql_where=sa.text("state IN ('SUBMITTED', 'POLLING')"),
    )
    op.drop_table("provider_calls")

    op.drop_index("ix_capability_policies_project", table_name="capability_policies")
    op.drop_table("capability_policies")

    op.drop_table("model_profiles")
    op.drop_table("providers")
