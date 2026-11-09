"""content_items 加可空列 scene_key / form_key（M3 接缝 §5.15）。

revision: f3a7c9d15b28
down_revision: d6074ae4bc53

两列均可空、无默认值（NULL = 未指派，旧行与旧调用方不受影响），互相独立。
CHECK 取值域写化石字面量：非 NULL 时去空白后不得为空。长度上限 200 由 varchar 承担。

downgrade 有数据损失：两列内容会丢，回退前须导出。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f3a7c9d15b28"
down_revision: str | None = "d6074ae4bc53"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "content_items", sa.Column("scene_key", sa.String(length=200), nullable=True)
    )
    op.add_column(
        "content_items", sa.Column("form_key", sa.String(length=200), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_content_items_scene_key_not_blank"),
        "content_items",
        "scene_key IS NULL OR char_length(btrim(scene_key)) > 0",
    )
    op.create_check_constraint(
        op.f("ck_content_items_form_key_not_blank"),
        "content_items",
        "form_key IS NULL OR char_length(btrim(form_key)) > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_content_items_form_key_not_blank"), "content_items", type_="check"
    )
    op.drop_constraint(
        op.f("ck_content_items_scene_key_not_blank"), "content_items", type_="check"
    )
    op.drop_column("content_items", "form_key")
    op.drop_column("content_items", "scene_key")
