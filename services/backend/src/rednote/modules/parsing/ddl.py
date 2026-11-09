"""本模块表的**数据库级保证**（DDL）。

**本文件只有一个内容：`source_segments` 的不可变触发器。**

**为什么片段要不可篡改**：它是"这条结论出自哪里"的唯一凭据。
若可以被 `psql` 手工改掉，那么"溯源"的答案就取决于"有没有人改过片段"，
而**没有人能审计'谁改过片段'** —— 与 WP15 审计表、WP16 出网留痕
是同一个论证。

**为什么复用 `platform/immutability.py` 的生成器而不是自己写一份**：
WP11 / WP15 / WP16 都用它，本机实测验证过（试写 UPDATE/DELETE 被拦，
sqlstate=23001）。再写一份的风险是"两处实现有细微差异"，
而那会让"审计被篡改"与"片段被篡改"**无法用同一个判据识别**。

**`parse_runs` 不加触发器**：它是过程记录，状态与计数**必须**能推进
（`QUEUED → RUNNING → SUCCEEDED`）。把可变的东西保护成不可变，
唯一的结果是系统不能工作。
"""

from __future__ import annotations

from typing import Final

from rednote.platform.immutability import immutable_trigger_ddl

# 本模块需要不可变保护的表（与 `models.IMMUTABLE_TABLES` 同值）。
#
# **这里刻意再写一遍而不是 import 模型层的常量**：模型层 import 时会注册
# ORM 守卫（有副作用），而迁移脚本需要在不加载 ORM 元数据的前提下拿到表名。
# 两处一致性由 `test_immutability_trigger_exists_on_source_segments` 与
# `test_source_segments_cannot_be_updated_via_raw_sql` 保证。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = ("source_segments",)


def all_trigger_ddl() -> list[tuple[str, str, str, str, str]]:
    """本模块需要建立的全部触发器。

    返回 `[(表名, 函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP), …]`。

    **顺序有意义**：函数 DDL 用 `CREATE OR REPLACE`，
    多张表共用同一个不可变函数时重复执行是安全的（幂等）；
    触发器 DROP 也必须先于函数 DROP，否则函数仍被触发器引用，
    PG 会拒绝删除（WP11 实测踩到过）。
    """
    items: list[tuple[str, str, str, str, str]] = []
    for table in IMMUTABLE_TABLES:
        function_ddl, trigger_ddl, drop_trigger, drop_function = (
            immutable_trigger_ddl(table)
        )
        items.append((table, function_ddl, trigger_ddl, drop_trigger, drop_function))
    return items


def trigger_names() -> tuple[str, ...]:
    """全部应存在的触发器名（验证脚本与测试用）。"""
    from rednote.platform.immutability import immutable_trigger_name

    return tuple(immutable_trigger_name(table) for table in IMMUTABLE_TABLES)


__all__ = [
    "IMMUTABLE_TABLES",
    "all_trigger_ddl",
    "trigger_names",
]
