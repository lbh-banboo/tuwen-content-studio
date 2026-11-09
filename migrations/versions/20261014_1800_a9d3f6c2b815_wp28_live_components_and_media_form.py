"""wp28 live components and media form

revision: a9d3f6c2b815
down_revision: e5a8c3b1d704
create_date: 2026-10-14 18:00

WP28：Live 动态图 —— `media_versions.media_form` 列 + `media_components` + `live_profiles`
+ 3 个触发器（2 条不可变保护 + 1 条组件一致性）。

## 一、这一步做什么（5 件事，顺序有意义）

1. `media_versions` 加 `media_form`（NOT NULL，`server_default 'STATIC'`）。
   存量行由 `ADD COLUMN ... DEFAULT` 回填 `STATIC`，**不逐行 UPDATE**，
   因此不触发 `media_versions` 上的不可变触发器（已在私有库真跑验证）。
2. 加 CHECK `ck_media_versions_media_form_valid`。
3. 建 `media_components`（不可变，8 列）与 `live_profiles`（不可变，6 列）。
4. 建 3 条索引（2 条唯一）。
5. 建触发器：`trg_media_components_immutable` / `trg_live_profiles_immutable` /
   `trg_media_components_consistency`（S2：只有 LIVE 版本才有组件；STILL 组件的
   `file_id` 必须等于版本的 `image_file_id`）。

## 二、CHECK 文本为什么是字面量（"化石"口径）

`tools/quality/check_migration_value_domain_no_live_import.py`（G1）禁止在迁移里
引用活的 `media.types` 常量：取值域将来放宽时，历史迁移不得随之漂移。
故角色列表 / `'SYNTHETIC'` / `'PLACEHOLDER'` / `'STATIC','LIVE'` 都写成字面量。
⚠️ 列表**顺序须与模型一致**（`types` 元组顺序）：PG 把 `IN (…)` 存成 ARRAY，
元素顺序不同 = 文本不同，`check_migration_model_parity.py` 会判两条路径不一致。
触发器 DDL 仍从 `media.ddl` 取（G4 阴性对照允许），与测试、证据脚本同源。

## 三、对前一迁移 `df6bb2f41f84` 的联动修改

`media_ddl.all_trigger_ddl()` 是活的，WP28 往里加了本迁移才建的两张表的触发器。
`df6bb2f41f84` 若不排除它们，全新库从零迁移会在
`CREATE TRIGGER trg_media_components_immutable` 处报
`relation "media_components" does not exist`（已实测复现）。
故该迁移按表名冻结为当时的 8 项；本迁移负责新增的 3 项。

## 四、downgrade

完整实现，不留 `NotImplementedError`。**只删本迁移建的对象**：

  · 3 个触发器 → 删；
  · `rednote_check_media_component_consistency()` → 我建的，删
    （用 `consistency_items()` 过滤，**不得**遍历 `all_trigger_ddl()` 删函数 ——
    其 `drop_function` 含跨包共用的 `rednote_reject_mutation()`）；
  · 3 条索引、2 张表、1 条 CHECK、1 列 → 删。

⚠️ 有真实数据损失：`media_components` 记录每组 Live 的组件指针与摘要，
`media_form` 记录版本形态。回退前须导出。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9d3f6c2b815"
down_revision: str | Sequence[str] | None = "e5a8c3b1d704"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 本迁移新建的两张表（触发器按表名挑出）。字面量而非 import，同"化石"口径。
_OWN_TABLES = frozenset({"media_components", "live_profiles"})


def upgrade() -> None:
    """正向迁移：列 → CHECK → 2 张表 → 3 条索引 → 3 个触发器。"""
    # ------------------------------------------------------------------
    # 第 1 步：media_versions.media_form
    #   server_default 在 ADD COLUMN 时回填存量行；PG 11+ 对常量默认值不重写表。
    # ------------------------------------------------------------------
    op.add_column(
        "media_versions",
        sa.Column(
            "media_form",
            sa.String(length=20),
            server_default=sa.text("'STATIC'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_media_versions_media_form_valid"),
        "media_versions",
        "media_form IN ('STATIC', 'LIVE')",
    )

    # ------------------------------------------------------------------
    # 第 2 步：media_components（不可变）
    # ------------------------------------------------------------------
    op.create_table(
        "media_components",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("media_version_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("file_id", sa.UUID(), nullable=False),
        sa.Column("verification", sa.String(length=20), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('STILL', 'PAIRED_MOTION', 'MANIFEST', 'PREVIEW')",
            name=op.f("ck_media_components_role_valid"),
        ),
        sa.CheckConstraint(
            "verification IN ('SYNTHETIC')",
            name=op.f("ck_media_components_verification_valid"),
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_media_components_sha256_shape"),
        ),
        sa.CheckConstraint(
            "byte_size > 0",
            name=op.f("ck_media_components_byte_size_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["file_objects.id"],
            name=op.f("fk_media_components_file_id_file_objects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["media_version_id"],
            ["media_versions.id"],
            name=op.f("fk_media_components_media_version_id_media_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_components")),
    )

    # ------------------------------------------------------------------
    # 第 3 步：live_profiles（不可变；变更 = 追加新 version）
    # ------------------------------------------------------------------
    op.create_table(
        "live_profiles",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("profile_key", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('PLACEHOLDER')",
            name=op.f("ck_live_profiles_state_valid"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_live_profiles_version_positive"),
        ),
        sa.CheckConstraint(
            "char_length(profile_key) > 0",
            name=op.f("ck_live_profiles_profile_key_not_empty"),
        ),
        sa.CheckConstraint(
            "char_length(profile_key) <= 100",
            name=op.f("ck_live_profiles_profile_key_length"),
        ),
        sa.CheckConstraint(
            "notes IS NULL OR char_length(notes) <= 500",
            name=op.f("ck_live_profiles_notes_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_live_profiles")),
    )

    # ------------------------------------------------------------------
    # 第 4 步：3 条索引
    # ------------------------------------------------------------------
    op.create_index(
        "uq_media_components_version_role",
        "media_components",
        ["media_version_id", "role"],
        unique=True,
    )
    op.create_index("ix_media_components_file", "media_components", ["file_id"])
    op.create_index(
        "uq_live_profiles_key_version",
        "live_profiles",
        ["profile_key", "version"],
        unique=True,
    )

    # ------------------------------------------------------------------
    # 第 5 步：触发器 —— autogenerate 不生成，必须手工补。
    #   DDL 来源与测试/证据脚本同源（`media.ddl`），不重写 SQL；
    #   只取属于本迁移两张表的 3 项（2 不可变 + 1 组件一致性）。
    #   ⚠️ 必须在建表之后。
    # ------------------------------------------------------------------
    from rednote.modules.media import ddl as media_ddl

    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in media_ddl.all_trigger_ddl():
        if _table not in _OWN_TABLES:
            continue
        op.execute(_function_ddl)
        op.execute(_trigger_ddl)


def downgrade() -> None:
    """反向迁移，与 upgrade 严格反序，只删本迁移建的对象（见 docstring §四）。"""
    from rednote.modules.media import ddl as media_ddl

    # ---- 1. 先删触发器（必须先于函数 DROP）----
    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in media_ddl.all_trigger_ddl():
        if _table not in _OWN_TABLES:
            continue
        op.execute(_drop_trigger)

    # ---- 2. 只删本迁移自己的函数 ----
    # **不得**遍历 `all_trigger_ddl()` 删函数：不可变保护那几项的
    # `drop_function` 指向跨包共用的 `rednote_reject_mutation()`。
    # `consistency_items()` 里属于本迁移的只有 `media_components` 那一项。
    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in media_ddl.consistency_items():
        if _table not in _OWN_TABLES:
            continue
        op.execute(_drop_function)

    # ---- 3. 索引（先于表，显式写出以便迁移日志可见）----
    op.drop_index("uq_live_profiles_key_version", table_name="live_profiles")
    op.drop_index("ix_media_components_file", table_name="media_components")
    op.drop_index("uq_media_components_version_role", table_name="media_components")

    # ---- 4. 表（反序：先 media_components，其 FK 指向 media_versions）----
    op.drop_table("live_profiles")
    op.drop_table("media_components")

    # ---- 5. CHECK 与列 ----
    op.drop_constraint(
        op.f("ck_media_versions_media_form_valid"),
        "media_versions",
        type_="check",
    )
    op.drop_column("media_versions", "media_form")
