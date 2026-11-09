"""${message}

revision: ${repr(up_revision).strip("'")}
down_revision: ${repr(down_revision).strip("'") if down_revision else "None"}

**本文件由 `alembic revision` 生成，生成后必须人工过一遍 G2 三件事**
（见 `docs/待定项与问题登记册.md` §4.4）：
  1. 删掉 `op.drop_table` / `op.drop_index`（autogenerate 每次都提议删
     M0 冒烟探针表 `smoke_meta` / `wp05_vec_probe`）；
  2. 删掉 downgrade 里**重建**这两张表的语句（`wp05_vec_probe.embedding`
     会被渲染成 `sa.NullType()` —— 它不认识 `vector` 类型，重建即错的表）；
  3. 补 `from sqlalchemy.dialects import postgresql`
     （用了 `postgresql.JSONB` 但文件头没有 → 执行即 `NameError`，
     **只看 diff 发现不了**）。

**另需人工确认的两件事**：
  · **建表顺序**：autogenerate 按字母序建表，遇到**循环外键**
    （A 引用 B、B 引用 A）必然失败。解法是拆环 —— 先建一张（不建回指外键），
    建完另一张后用 `ALTER TABLE ... ADD CONSTRAINT` 补上。
    注意 `DEFERRABLE` 解决不了这个问题：它管的是**事务提交时**的行级校验，
    而 CREATE TABLE 需要的表**在 DDL 层面**就必须存在。
  · **触发器**：autogenerate **不生成触发器**，必须手工补，
    且 DDL 应调用模块自己的 `all_trigger_ddl()`（保持与测试同源，
    避免两份不一致的 DDL —— WP11/WP16/WP17 各踩过一次）。

**为什么 `message` 要渲染出来**：此前模板没引用 `${message}`，
于是 `alembic revision -m "WP18 brief structure and claims"` 传入的消息
被静默丢弃，生成的文件 docstring 与 alembic 运行日志都显示模板原文，
无法从日志判断「这次跑的是哪个迁移」（WP18 实际踩到，登记为 ISS-59）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
# 用 repr() 而非 "${...}"：字符串插值会把 Python 的 None 渲染成字符串 "None"，
# 导致 Alembic 解析 down_revision 时 KeyError: 'None'（本机实测踩到过）。
# repr() 输出的是 Python 字面量，None 就是 None，'abc123' 就是 'abc123'。
revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """正向迁移。"""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """反向迁移。

    必须补全，不能留 NotImplementedError —— 那会让任何回滚尝试直接失败，
    与 WP47 的恢复演练（RPO/RTO）目标冲突。

    **只删本迁移自己建出来的对象**：跨包共用的函数（如
    `rednote_reject_mutation()`）生命周期归**引入它的那个迁移**，
    在这里删会撞 "cannot drop function ... because other objects depend on it"
    （WP11 踩过、WP16 差点再踩）。
    """
    ${downgrades if downgrades else "pass"}