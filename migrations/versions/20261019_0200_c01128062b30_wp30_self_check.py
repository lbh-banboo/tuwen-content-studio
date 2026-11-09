"""WP30：AI 自检/硬软规则 —— 建 `self_check_runs` / `self_check_rule_results` / `self_check_findings`。

revision: c01128062b30
down_revision: efa306a18363

## 一、本迁移做什么

建三张不可变表、3 个不可变触发器（`rednote_reject_mutation`，跨包共用，
不重建）+ 2 个本模块自有的延迟一致性触发器。DDL 与测试/证据脚本同源
（`rednote.modules.review.ddl`），本文件只 `op.execute` 它生成的 SQL。

## 二、G2 铁律执行记录

1. **不删任何非本迁移创建的对象**：`downgrade` 只删本模块自己的 2 个一致性
   函数（`consistency_function_names()`），**不碰** `rednote_reject_mutation()`
   （跨包共用，ISS-79）。
2. **取值域写化石字面量**，不从 `review.types` 取（ISS-191）：迁移文本是
   "建库那一刻的快照"，此后 `types` 怎么改都不再影响老库。
3. **`rule_code` / `skip_reason` 刻意不落取值域 CHECK**：规则码是会增长的
   开集，权威在 `review.rules.RULES`；落 CHECK 会把它冻成化石，每加一条
   规则就要一次迁移，且化石与注册表的分叉不会报错（新规则的结果静默写
   不进库）。`category`/`rule_kind`/`status`/`severity`/`anchor_kind`/
   `outcome`/`trigger_kind` 是闭集，落 CHECK。
4. 触发器 DDL 直接 import `rednote.modules.review.ddl`（与 WP27/WP35/WP29
   迁移同一做法：触发器 SQL 与测试、证据脚本同源，不手写第二份）。

## 三、与模型的对应关系

`sa.Column`/`sa.CheckConstraint`/`sa.ForeignKeyConstraint`/`sa.Index` 逐项
对应 `services/backend/src/rednote/modules/review/models.py` 的
`SelfCheckRun` / `SelfCheckRuleResult` / `SelfCheckFinding` 三个类，列名、
约束、索引**逐一照抄**（已用 `check_migration_model_parity.py` 的同源脚本
对账过结构），不是独立设计。

## 四、⚠️ `downgrade` 有数据损失

自检轮次与逐规则结论是"系统判过什么、有没有阻断"的唯一凭据；
回退前须导出全量。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c01128062b30"
down_revision: str | None = "efa306a18363"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "self_check_runs",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("content_item_id", sa.UUID(), nullable=False),
        sa.Column("subject_version_id", sa.UUID(), nullable=False),
        sa.Column("rule_set_version", sa.Integer(), nullable=False),
        sa.Column("trigger_kind", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("blocking_count", sa.Integer(), nullable=False),
        sa.Column("warning_count", sa.Integer(), nullable=False),
        sa.Column("info_count", sa.Integer(), nullable=False),
        sa.Column("evaluated_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("errored_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "trigger_kind IN ('BATCH', 'MANUAL')",
            name=op.f("ck_self_check_runs_trigger_kind_valid"),
        ),
        sa.CheckConstraint(
            "outcome IN ('BLOCKED', 'ERRORED', 'PASSED', 'WARNED')",
            name=op.f("ck_self_check_runs_outcome_valid"),
        ),
        sa.CheckConstraint(
            "(outcome = 'BLOCKED') = (blocking_count > 0)",
            name=op.f("ck_self_check_runs_outcome_blocked_equivalence"),
        ),
        sa.CheckConstraint(
            "(outcome = 'ERRORED') = (blocking_count = 0 AND errored_count > 0)",
            name=op.f("ck_self_check_runs_outcome_errored_equivalence"),
        ),
        sa.CheckConstraint(
            "(outcome = 'WARNED') = (blocking_count = 0 AND errored_count = 0"
            " AND warning_count > 0)",
            name=op.f("ck_self_check_runs_outcome_warned_equivalence"),
        ),
        sa.CheckConstraint(
            "(outcome = 'PASSED') = (blocking_count = 0 AND errored_count = 0"
            " AND warning_count = 0)",
            name=op.f("ck_self_check_runs_outcome_passed_equivalence"),
        ),
        sa.CheckConstraint(
            "blocking_count >= 0",
            name=op.f("ck_self_check_runs_blocking_count_non_negative"),
        ),
        sa.CheckConstraint(
            "warning_count >= 0",
            name=op.f("ck_self_check_runs_warning_count_non_negative"),
        ),
        sa.CheckConstraint(
            "info_count >= 0", name=op.f("ck_self_check_runs_info_count_non_negative")
        ),
        sa.CheckConstraint(
            "evaluated_count >= 1",
            name=op.f("ck_self_check_runs_evaluated_count_positive"),
        ),
        sa.CheckConstraint(
            "skipped_count >= 0",
            name=op.f("ck_self_check_runs_skipped_count_non_negative"),
        ),
        sa.CheckConstraint(
            "errored_count >= 0",
            name=op.f("ck_self_check_runs_errored_count_non_negative"),
        ),
        sa.CheckConstraint(
            "rule_set_version >= 1",
            name=op.f("ck_self_check_runs_rule_set_version_positive"),
        ),
        sa.CheckConstraint(
            "finished_at >= started_at",
            name=op.f("ck_self_check_runs_finished_at_not_before_started_at"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_self_check_runs_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["content_items.id"],
            name=op.f("fk_self_check_runs_content_item_id_content_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subject_version_id"],
            ["content_versions.id"],
            name=op.f("fk_self_check_runs_subject_version_id_content_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_self_check_runs_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_self_check_runs")),
    )
    op.create_index(
        "ix_self_check_runs_content_created",
        "self_check_runs",
        ["content_item_id", "created_at"],
    )
    op.create_index(
        "ix_self_check_runs_project_created",
        "self_check_runs",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_self_check_runs_subject_version", "self_check_runs", ["subject_version_id"]
    )

    op.create_table(
        "self_check_rule_results",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("rule_code", sa.String(length=60), nullable=False),
        sa.Column("category", sa.String(length=24), nullable=False),
        sa.Column("rule_kind", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=True),
        sa.Column("skip_reason", sa.String(length=40), nullable=True),
        sa.Column("error_note", sa.String(length=500), nullable=True),
        sa.Column("finding_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(rule_code) > 0",
            name=op.f("ck_self_check_rule_results_rule_code_not_blank"),
        ),
        sa.CheckConstraint(
            "category IN ('DUPLICATE', 'FACT', 'MEDIA_CONSISTENCY', 'OTHER',"
            " 'SCENE', 'STRUCTURE', 'TECH_FORMAT', 'TONE')",
            name=op.f("ck_self_check_rule_results_category_valid"),
        ),
        sa.CheckConstraint(
            "rule_kind IN ('HARD', 'SOFT')",
            name=op.f("ck_self_check_rule_results_rule_kind_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('ERRORED', 'FAILED', 'PASSED', 'SKIPPED')",
            name=op.f("ck_self_check_rule_results_status_valid"),
        ),
        sa.CheckConstraint(
            "severity IS NULL OR severity IN ('BLOCKING', 'INFO', 'WARNING')",
            name=op.f("ck_self_check_rule_results_severity_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'FAILED') = (severity IS NOT NULL)",
            name=op.f("ck_self_check_rule_results_severity_paired_with_failed"),
        ),
        sa.CheckConstraint(
            "(status = 'SKIPPED') = (skip_reason IS NOT NULL)",
            name=op.f("ck_self_check_rule_results_skip_reason_paired_with_skipped"),
        ),
        sa.CheckConstraint(
            "(status = 'ERRORED') = (error_note IS NOT NULL)",
            name=op.f("ck_self_check_rule_results_error_note_paired_with_errored"),
        ),
        sa.CheckConstraint(
            "finding_count >= 0",
            name=op.f("ck_self_check_rule_results_finding_count_non_negative"),
        ),
        sa.CheckConstraint(
            "(status = 'FAILED') = (finding_count > 0)",
            name=op.f("ck_self_check_rule_results_finding_count_paired_with_failed"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["self_check_runs.id"],
            name=op.f("fk_self_check_rule_results_run_id_self_check_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_self_check_rule_results")),
        sa.UniqueConstraint(
            "run_id", "rule_code", name=op.f("uq_self_check_rule_results_run_code")
        ),
    )
    op.create_index(
        "ix_self_check_rule_results_run_category",
        "self_check_rule_results",
        ["run_id", "category"],
    )
    op.create_index(
        "ix_self_check_rule_results_run_status",
        "self_check_rule_results",
        ["run_id", "status"],
    )

    op.create_table(
        "self_check_findings",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("rule_result_id", sa.UUID(), nullable=False),
        sa.Column("anchor_kind", sa.String(length=20), nullable=False),
        sa.Column("paragraph_ref", sa.String(length=200), nullable=True),
        sa.Column("media_item_id", sa.UUID(), nullable=True),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("feedback_item_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "anchor_kind IN ('MEDIA', 'PARAGRAPH', 'WHOLE')",
            name=op.f("ck_self_check_findings_anchor_kind_valid"),
        ),
        sa.CheckConstraint(
            "(anchor_kind <> 'PARAGRAPH') OR (paragraph_ref IS NOT NULL AND"
            " media_item_id IS NULL)",
            name=op.f("ck_self_check_findings_anchor_paragraph_shape"),
        ),
        sa.CheckConstraint(
            "(anchor_kind <> 'MEDIA') OR (media_item_id IS NOT NULL AND"
            " paragraph_ref IS NULL)",
            name=op.f("ck_self_check_findings_anchor_media_shape"),
        ),
        sa.CheckConstraint(
            "(anchor_kind <> 'WHOLE') OR (paragraph_ref IS NULL AND"
            " media_item_id IS NULL)",
            name=op.f("ck_self_check_findings_anchor_whole_shape"),
        ),
        sa.CheckConstraint(
            "char_length(message) > 0",
            name=op.f("ck_self_check_findings_message_not_blank"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence) = 'object'",
            name=op.f("ck_self_check_findings_evidence_is_object"),
        ),
        sa.ForeignKeyConstraint(
            ["rule_result_id"],
            ["self_check_rule_results.id"],
            name=op.f("fk_self_check_findings_rule_result_id_self_check_rule_results"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["media_item_id"],
            ["media_items.id"],
            name=op.f("fk_self_check_findings_media_item_id_media_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["feedback_item_id"],
            ["content_feedback_items.id"],
            name="fk_self_check_findings_feedback_item_id_content_feedback_items",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_self_check_findings")),
    )
    op.create_index(
        "ix_self_check_findings_rule_result", "self_check_findings", ["rule_result_id"]
    )
    op.create_index(
        "ix_self_check_findings_media_item", "self_check_findings", ["media_item_id"]
    )
    op.create_index(
        "ix_self_check_findings_feedback_item",
        "self_check_findings",
        ["feedback_item_id"],
    )

    # 触发器：不可变保护在前，一致性在后。DDL 与测试/证据脚本同源。
    from rednote.modules.review import ddl as review_ddl

    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in review_ddl.all_trigger_ddl():
        op.execute(_function_ddl)
        op.execute(_trigger_ddl)


def downgrade() -> None:
    from rednote.modules.review import ddl as review_ddl

    # 1. 先删触发器（必须先于函数）。
    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in review_ddl.all_trigger_ddl():
        op.execute(_drop_trigger)

    # 2. 只删本模块自己的函数；**不得**用 all_trigger_ddl()——
    #    其不可变项的 drop_function 指向跨包共用的 rednote_reject_mutation()。
    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in review_ddl.consistency_items():
        op.execute(_drop_function)

    op.drop_index(
        "ix_self_check_findings_feedback_item", table_name="self_check_findings"
    )
    op.drop_index("ix_self_check_findings_media_item", table_name="self_check_findings")
    op.drop_index(
        "ix_self_check_findings_rule_result", table_name="self_check_findings"
    )
    op.drop_table("self_check_findings")

    op.drop_index(
        "ix_self_check_rule_results_run_status", table_name="self_check_rule_results"
    )
    op.drop_index(
        "ix_self_check_rule_results_run_category", table_name="self_check_rule_results"
    )
    op.drop_table("self_check_rule_results")

    op.drop_index("ix_self_check_runs_subject_version", table_name="self_check_runs")
    op.drop_index("ix_self_check_runs_project_created", table_name="self_check_runs")
    op.drop_index("ix_self_check_runs_content_created", table_name="self_check_runs")
    op.drop_table("self_check_runs")
