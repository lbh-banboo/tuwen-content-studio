"""WP15：审计事件与通知回执（2 张表 + 1 个不可变触发器）。

revision: 005e53eaed23
down_revision: 41b468c478a4

**本迁移包含两张表与一个触发器**：

| 对象 | 来源 | 说明 |
|---|---|---|
| `audit_events` | 数据结构 §11 | 审计日志；**追加且不可篡改** |
| `notification_receipts` | 数据结构 §11 | 通知的送达/已读回执；**可变** |
| `trg_audit_events_immutable` | §11「应用账户无 UPDATE/DELETE 权限」 | 拦 UPDATE / DELETE |

**`work_items` 不在本迁移里** —— 它由 WP10 建（WP10 的验收含"待办和历史正确"）。
本包只消费它。**不要**在本迁移里重建或改它的结构。

**两张表的可变性正好相反，这是它们必须分开的原因**：
  · `audit_events` 是**凭据**：写完不改，靠触发器拦；
  · `notification_receipts` 是**状态**：`read_at` / `dismissed_at` 会被写。
合并成一张表的后果是"要么审计可改、要么已读写不了"，两者都不可接受。

**为什么用触发器而不是 GRANT**（§2 选型确认）：
本机只有单数据库账户，GRANT 级隔离**做不出来**（"谁能写"分不出来），
做了只会给出"已经隔离了"的假象。触发器对**所有**账户都拦
（含超级用户）—— 它比 GRANT 更强。GRANT 级账户隔离登记为 OP-118，
留内网多账户后做。

触发器 DDL 由 `rednote.modules.audit.ddl` 生成（它又复用
`platform/immutability.py` 的生成器 —— WP11 已用它建过版本表的不可变触发器，
且本机实测验证过：试写 UPDATE/DELETE 被拦，sqlstate=23001）。
**名字与 SQL 只有一处来源**：迁移与验证脚本各自手写会出现
"迁移建了 A、验证脚本找 B"这种**看起来通过、实际没保护**的情况。

---

## 【铁律 G2 —— 每次 autogenerate 后必读】

本次 autogenerate **第六次**提议删除 `smoke_meta` 与 `wp05_vec_probe`
（M0 冒烟脚本建的探针表，不在 SQLAlchemy 模型里）。
已人工删除全部 `op.drop_table` / `op.drop_index` 语句。

**这两张探针表不属于本项目模型，永远不要删**：WP11~WP14 的每一次迁移
都用它们的行数作为"残留表未被误删"的证据（`evidence/m1/*.log` 的核对项）。
来源：登记册 §4.3 的 ISS-13 / ISS-14、ISS-32。

**另外两处**（前几次都出现过，已一并修掉）：
  · autogenerate 生成的 `downgrade` 又一次试图**重建**这两张表
    （`wp05_vec_probe.embedding` 会被渲染成 `sa.NullType()`）—— 已整段删除；
  · 本文件用到 `postgresql.JSONB` 时需要补
    `from sqlalchemy.dialects import postgresql`（本迁移实际没用到 JSONB，
    因此没有这一处；但**每次生成后都要检查**，这是 ISS-32 的教训）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from rednote.modules.audit.ddl import all_trigger_ddl, consistency_items

# revision identifiers, used by Alembic.
# 用 repr() 而非 "Ellipsis"：字符串插值会把 Python 的 None 渲染成字符串 "None"，
# 导致 Alembic 解析 down_revision 时 KeyError: 'None'（本机实测踩到过）。
revision: str = "005e53eaed23"
down_revision: str | Sequence[str] | None = "41b468c478a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """正向迁移：建表 → 建索引 → 建不可变触发器。"""
    op.create_table(
        "audit_events",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("actor_kind", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("object_type", sa.String(length=50), nullable=False),
        sa.Column("object_id", sa.UUID(), nullable=False),
        # 对象所属项目。可空：平台级动作（账号停用、字典维护）不属于任何项目。
        # 数据结构 §11 没列这一列，但 §10 要求 `GET /audit-events?projectId=`
        # → 已登记为 OP-116。
        sa.Column("project_id", sa.UUID(), nullable=True),
        # 对象对应的版本。可空；它让"想看正文"的人**自己去查那一版** ——
        # 审计不该自己复现正文。
        sa.Column("version_id", sa.UUID(), nullable=True),
        # 规范化摘要（**不是正文**）。char(64) 与版本注册的 payload_hash
        # 同型同义，便于"审计哈希 ↔ 版本哈希"的对照（见 models.py 的论证）。
        sa.Column("before_hash", sa.String(length=64), nullable=True),
        sa.Column("after_hash", sa.String(length=64), nullable=True),
        # reason 可以是"运营填的理由"（给人看的），但不是业务对象正文。
        # 限长 512：允许无限长度会让它成为"顺便把内容塞进来"的通道。
        sa.Column("reason", sa.String(length=512), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor_kind IN ('SERVICE', 'SYSTEM', 'USER')",
            name=op.f("ck_audit_events_actor_kind_valid"),
        ),
        # before/after 必须**同时有或同时无** —— "从哪改到哪"是一对的，
        # 只记一边没有意义。
        sa.CheckConstraint(
            "(before_hash IS NULL) = (after_hash IS NULL)",
            name=op.f("ck_audit_events_hash_pair_consistent"),
        ),
        sa.CheckConstraint(
            "char_length(before_hash) = 64 OR before_hash IS NULL",
            name=op.f("ck_audit_events_before_hash_length"),
        ),
        sa.CheckConstraint(
            "char_length(after_hash) = 64 OR after_hash IS NULL",
            name=op.f("ck_audit_events_after_hash_length"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_audit_events_actor_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_audit_events_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    # §10 的主过滤路径（按项目 + 时间）。
    op.create_index(
        "ix_audit_events_project_created",
        "audit_events",
        ["project_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_object", "audit_events", ["object_type", "object_id"], unique=False
    )
    op.create_index(
        "ix_audit_events_actor",
        "audit_events",
        ["actor_kind", "actor_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_request_id", "audit_events", ["request_id"], unique=False
    )

    op.create_table(
        "notification_receipts",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        # read_at 与 dismissed_at **分开**（§11 原文）：
        # "我没看到"与"我不想再看到"是两件事，前者不该影响任何业务。
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["domain_events.id"],
            name=op.f("fk_notification_receipts_event_id_domain_events"),
            # 回执是事件的从属数据：事件被清理时一并清理。
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_receipts_user_id_users"),
            ondelete="RESTRICT",
        ),
        # 复合主键：每个用户对每个事件只有一条回执。
        sa.PrimaryKeyConstraint(
            "user_id", "event_id", name=op.f("pk_notification_receipts")
        ),
    )
    # 小红点路径：部分索引只覆盖未读 —— 已读历史不回响、只占存储。
    op.create_index(
        "ix_notification_receipts_unread",
        "notification_receipts",
        ["user_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("read_at IS NULL AND dismissed_at IS NULL"),
    )
    op.create_index(
        "ix_notification_receipts_event",
        "notification_receipts",
        ["event_id"],
        unique=False,
    )

    # 触发器最后建：它引用表，表必须已存在。
    for _table, function_ddl, trigger_ddl, _drop_trigger, _drop_function in (
        all_trigger_ddl()
    ):
        op.execute(sa.text(function_ddl))
        op.execute(sa.text(trigger_ddl))


def downgrade() -> None:
    """反向迁移：先删触发器、再删**本模块自己的**函数，最后删表。

    **顺序不可颠倒**：先删表的话，`DROP TABLE` 会连带删掉触发器（PG 允许），
    但**函数不会自动删除** —— 残留的 `rednote_reject_mutation()` 会在
    下次重装时与新函数同名（`CREATE OR REPLACE` 能覆盖），
    但"删干净"才是可验证的状态（验证脚本会查 `pg_proc`）。

    ---

    ## ★ ISS-79：本函数的第 2 步**曾经删错了函数**（2026-10-05 修复）

    原写法（**已废止，不要改回去**）：

        for drop_function in {item[4] for item in all_trigger_ddl()}:
            op.execute(sa.text(drop_function))

    `all_trigger_ddl()` 里每一项的第 5 项都是 `immutable_trigger_ddl()`
    给出的 `DROP FUNCTION IF EXISTS rednote_reject_mutation()` ——
    那是**跨包共用**的函数，WP11 建的，WP16/17/18/20/22/23/25 都挂着它的
    触发器。于是本迁移回退时会去删一个**别人建的**函数。

    为什么当时"看起来"没事：注释里写的是"此时已无触发器引用它"
    —— 那句只在**本模块自己的表**范围内成立（本包只有一张不可变表，
    `trg_audit_events_immutable` 确实已被第 1 步删掉）。
    它没考虑**别的包的表**还在引用同一函数。真实失败点：

        $ alembic downgrade base      # WP15 这一步
        ERROR: cannot drop function rednote_reject_mutation() because
               other objects depend on it
        DETAIL: trigger trg_version_nodes_immutable on table version_nodes
                depends on function rednote_reject_mutation()
                trigger trg_version_dependencies_immutable on table
                version_dependencies depends on function rednote_reject_mutation()
        [SQL: DROP FUNCTION IF EXISTS rednote_reject_mutation()]

    **为什么它阻塞 WP47**：`downgrade` 不可用 ⇒ 回滚路径不存在 ⇒
    ADR-11 的 RPO ≤ 24h / RTO ≤ 8h 无法演练与验收。

    ## 修法：第 2 步的覆盖范围由 `consistency_items()` 决定

    对该函数断言的是"**是不是我建的**"，而不是"它在哪张表上"：
      · 本模块**没有**自己的函数（`audit/ddl.py::consistency_items()` 返回空）
        ⇒ 第 2 步**一次都不执行**，共用函数安然无恙；
      · 若将来本包新增一致性触发器（自带函数），只需在 `consistency_items()`
        里加一项，本函数**结构不用改**。

    **不得**把第 2 步改回 `all_trigger_ddl()` —— 它的第 5 项指向共用函数。
    该不变式有测试守着：`test_migration_roundtrip.py` 静态检查全部迁移
    的 `downgrade` 里不存在"遍历 `all_trigger_ddl()` 去 DROP FUNCTION"的形态，
    并在真库上跑 `upgrade head → downgrade base → upgrade head` 往返。
    """
    ddl_items = all_trigger_ddl()

    # ---- 1. 先删触发器（全部是我建的，含不可变保护）----
    # 顺序不能反：函数仍被触发器引用时，PG 会拒绝删除。
    for _table, _function_ddl, _trigger_ddl, drop_trigger, _drop_function in ddl_items:
        op.execute(sa.text(drop_trigger))

    # ---- 2. 只删本模块自己的函数（**绝不碰 rednote_reject_mutation**）----
    for _table, _function_ddl, _trigger_ddl, _drop_trigger, drop_function in (
        consistency_items()
    ):
        op.execute(sa.text(drop_function))

    op.drop_index("ix_notification_receipts_event", table_name="notification_receipts")
    op.drop_index("ix_notification_receipts_unread", table_name="notification_receipts")
    op.drop_table("notification_receipts")

    op.drop_index("ix_audit_events_request_id", table_name="audit_events")
    op.drop_index("ix_audit_events_actor", table_name="audit_events")
    op.drop_index("ix_audit_events_object", table_name="audit_events")
    op.drop_index("ix_audit_events_project_created", table_name="audit_events")
    op.drop_table("audit_events")