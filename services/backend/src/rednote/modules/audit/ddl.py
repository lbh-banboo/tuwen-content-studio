"""本模块表的**数据库级保证**（DDL）。

**本文件只有一个内容：`audit_events` 的不可变触发器。**

数据结构 §11 对审计的约束原文：

> audit_events | ... | **追加；应用账户无 UPDATE/DELETE 权限**

**为什么本机用触发器而不是 GRANT**（§2 选型确认）：

| | GRANT 级隔离 | 触发器 |
|---|---|---|
| 拦谁 | 非授权账户 | **所有账户**（含超级用户） |
| 前提 | 需要**多账户**（应用账户 vs 运维账户） | 单账户即可 |
| 本机可验证 | ✗（只有一个账户，"谁能写"分不出来） | ✓（试写即知） |

本机只有单数据库账户，做 GRANT 等于**没做**，还会给出"已经隔离了"的假象。
而触发器**对所有账户都拦** —— 它比 GRANT 更强（GRANT 拦不住超级用户）。
GRANT 级账户隔离登记为 OP-118，留内网多账户后做。

**为什么复用 `platform/immutability.py` 的 DDL 生成器而不是自己写一份**：
WP11 已经用它建过 `version_nodes` / `version_dependencies` 的不可变触发器，
且本机实测验证过（试写 UPDATE/DELETE 被拦，sqlstate=23001）。
再写一份的风险是"两处实现有细微差异"（比如 ERRCODE 不同），
而那会让"审计被篡改"与"版本被篡改"这两件事**无法用同一个判据识别**。

**为什么审计的不可变与版本的不可变共用一个触发器函数**：
行为完全一致（一律拒绝 UPDATE/DELETE），分成多个函数只会让
"DBA 要改一处行为得改 N 个函数"（与 WP11 同一判断）。
"""

from __future__ import annotations

from typing import Final

from rednote.platform.immutability import immutable_trigger_ddl

# 本模块需要不可变保护的表。
#
# **只有 audit_events**：`notification_receipts` 是**可变**的
# （`read_at` / `dismissed_at` 会被写）—— 它与审计的约束正好相反，
# 这也是数据结构 §11 把它们列成两张表的原因。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = ("audit_events",)


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块**自己的**函数（`downgrade` 第 2 步的唯一覆盖来源）。

    **本包为空**（返回空列表），且这是**有意的显式形状**而不是"忘了写"：

    本包只有 `audit_events` 一张不可变表，它挂的 `trg_audit_events_immutable`
    引用的是**跨包共用的** `rednote_reject_mutation()`（WP11 建），
    本包**没有创建任何自己的函数**。因此"删本模块的东西"退化为"什么都不删"
    —— 这恰好是**正确**的行为。

    ★ **ISS-79 的根因就在这个空集上**：WP15 原 `downgrade()` 第 2 步写成

        for drop_function in {item[4] for item in all_trigger_ddl()}:
            op.execute(sa.text(drop_function))

    `all_trigger_ddl()` 里唯一一项的第 5 项正是
    `DROP FUNCTION IF EXISTS rednote_reject_mutation()` ——
    于是 WP15 的 `downgrade` 会去删**别人建的共用函数**。真实失败点：
    WP15 回退时 WP11 的 `trg_version_nodes_immutable` /
    `trg_version_dependencies_immutable` 仍引用该函数 ⇒ PG 拒绝：

        ERROR: cannot drop function rednote_reject_mutation() because
               other objects depend on it

    本函数让"我的函数"与"别人的函数"在**类型层面**分开：第 2 步只迭代
    本函数的返回值，共用函数永远进不来（空集 ⇒ 循环体一次都不执行）。
    """
    return []


def all_trigger_ddl() -> list[tuple[str, str, str, str, str]]:
    """本模块需要建立的全部触发器。

    返回 `[(表名, 函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP), …]`。

    **顺序有意义**：函数 DDL 用 `CREATE OR REPLACE`，
    多张表共用同一个不可变函数时重复执行是安全的（幂等）；
    触发器 DROP 也必须先于函数 DROP，否则函数仍被触发器引用，
    PG 会拒绝删除（WP11 实测踩到过：`cannot drop function ... because
    other objects depend on it`，修法是 downgrade **分两趟**）。

    ⚠️ 本函数的第 5 项指向**共用函数**的 `DROP`。因此 `downgrade`
    **不得**遍历本函数的返回值去 `DROP FUNCTION`（那是 ISS-79）。
    第一步用它取 `_drop_trigger`（正确），第二步用 `consistency_items()`。
    """
    items: list[tuple[str, str, str, str, str]] = []
    for table in IMMUTABLE_TABLES:
        function_ddl, trigger_ddl, drop_trigger, drop_function = (
            immutable_trigger_ddl(table)
        )
        items.append((table, function_ddl, trigger_ddl, drop_trigger, drop_function))

    items.extend(consistency_items())
    return items


def trigger_names() -> tuple[str, ...]:
    """全部应存在的触发器名（验证脚本与测试用）。

    **名字只有一个来源**：由 `immutable_trigger_name` 生成。
    若迁移与验证脚本各写一遍，会出现"迁移建了 A、验证脚本找 B"
    这种**看起来通过、实际没保护**的情况（WP11 的核心教训）。
    """
    from rednote.platform.immutability import immutable_trigger_name

    return tuple(immutable_trigger_name(table) for table in IMMUTABLE_TABLES)


def immutable_function_names() -> tuple[str, ...]:
    """本模块的不可变触发器**引用的**函数名：只有共用那一个。

    它是**跨包共用**的，`downgrade` 不许删（删了会让其它 8 个包的保护
    一起失效）。把这个事实写成一个可被判据读取的函数，而不是一行注释
    ——"声称自己有防线"与"真有防线"的区别（ISS-95）。
    """
    return ("rednote_reject_mutation",)


__all__ = [
    "IMMUTABLE_TABLES",
    "all_trigger_ddl",
    "consistency_items",
    "immutable_function_names",
    "trigger_names",
]