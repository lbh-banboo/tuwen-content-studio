"""本模块表的**数据库级保证**（DDL）。

**本文件只有一个内容：`gateway_egress` 的不可变触发器。**

**为什么出网留痕要不可篡改**：ADR-10 把它列为"网关出网记录"，
是合规核查的凭据。凭据的意义就是"改不了" ——
若可以被 `psql` 手工改掉，那么"这个项目出过几次网"这个问题的答案
就取决于"有没有人改过"，而**没有人能审计'谁改过出网记录'**。

**为什么复用 `platform/immutability.py` 的生成器而不是自己写一份**：
WP11 建过 `version_nodes` / `version_dependencies`，WP15 建过 `audit_events`，
本机都实测验证过（试写 UPDATE/DELETE 被拦，sqlstate=23001）。
再写一份的风险是"两处实现有细微差异"（比如 ERRCODE 不同），
而那会让"审计被篡改"与"出网记录被篡改"**无法用同一个判据识别**。

**本机用触发器而不是 GRANT**（与 WP15 同一判断）：本机只有单数据库账户，
做 GRANT 等于没做，还会给出"已经隔离了"的假象；
而触发器对**所有账户**都拦（含超级用户），比 GRANT 更强。
GRANT 级账户隔离见 OP-118。
"""

from __future__ import annotations

from typing import Final

from rednote.platform.immutability import immutable_trigger_ddl

# 本模块需要不可变保护的表（与 `models.IMMUTABLE_TABLES` 同值）。
#
# **这里刻意再写一遍而不是 import 模型层的常量**：模型层 import 时会注册
# ORM 守卫（有副作用），而迁移脚本需要在不加载 ORM 元数据的前提下拿到表名。
# 两处一致性由 `test_immutability_trigger_exists_on_egress_table` 与
# `test_gateway_egress_cannot_be_updated_via_raw_sql` 保证。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = ("gateway_egress",)


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
