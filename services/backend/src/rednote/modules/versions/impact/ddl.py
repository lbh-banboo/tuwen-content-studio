"""`export_validity_events` 的 DDL：1 个不可变触发器 + 1 个一致性触发器（本包函数 1 个）。

## 一致性触发器在守什么（FK / CHECK 管不住的那部分）

`AFTER INSERT`、**即时**（非延迟）：每条事件是自洽的单行事实，不需要等同事务其他行就位。

| 坏数据 | 为什么 FK / CHECK 挡不住 |
|---|---|
| 首条不是 `seq=1` 的 INVALIDATED / seq 跳号 / 连续两条同 transition | 「最大 seq 即当前态」的前提；缺了它，当前态不可信 |
| INVALIDATED 的观测指纹等于冻结指纹 | FK 只保证 `export_items` 行存在，不保证事件说的是真话 |
| VERSION_SUPERSEDED 但观测版本仍是冻结版本 / ASSET_CHANGED 但版本已变 | reason 与版本自洽 |
| RESTORED 的观测值没回到冻结值 | 恢复必须有依据 |

## downgrade 的删除范围

`OWN_FUNCTIONS` **只列本包自己的函数**，**不含** `rednote_reject_mutation`（跨包共用，ISS-79）。
RAISE 用字符串拼接，不用 `%` 占位符（psycopg paramstyle 会把 `%` 当占位符）。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.versions.impact.models import IMMUTABLE_TABLES
from rednote.platform.immutability import immutable_trigger_ddl, immutable_trigger_name

CONSISTENCY_SQLSTATE: Final[str] = "23514"

CONSISTENCY_FUNCTION: Final[str] = "rednote_check_export_validity_event"
CONSISTENCY_TRIGGER: Final[str] = "trg_export_validity_events_consistency"

#: 本包**自己的**函数（downgrade 只删这个）。
OWN_FUNCTIONS: Final[tuple[str, ...]] = (CONSISTENCY_FUNCTION,)


def _raise(message_expr: str, indent: str = "            ") -> str:
    return (
        f"{indent}RAISE EXCEPTION USING\n"
        f"{indent}    MESSAGE = {message_expr},\n"
        f"{indent}    ERRCODE = '{CONSISTENCY_SQLSTATE}';\n"
    )


def consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。"""
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {CONSISTENCY_FUNCTION}() RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    frozen_version uuid;\n"
        "    frozen_hash varchar(64);\n"
        "    prev_seq integer;\n"
        "    prev_transition varchar(12);\n"
        "BEGIN\n"
        "    SELECT version_node_id, eligibility_hash INTO frozen_version, frozen_hash\n"
        "      FROM export_items WHERE id = NEW.export_item_id;\n"
        "    SELECT seq, transition INTO prev_seq, prev_transition\n"
        "      FROM export_validity_events\n"
        "     WHERE export_item_id = NEW.export_item_id AND id <> NEW.id\n"
        "     ORDER BY seq DESC LIMIT 1;\n"
        "    IF NOT FOUND THEN\n"
        "        IF NEW.seq <> 1 OR NEW.transition <> 'INVALIDATED' THEN\n"
        + _raise(
            "'导出行 ' || NEW.export_item_id::text\n"
            "                          || ' 的首条事件必须是 seq=1 的 INVALIDATED'",
            "            ",
        )
        + "        END IF;\n"
        "    ELSIF NEW.seq <> prev_seq + 1 OR NEW.transition = prev_transition THEN\n"
        + _raise(
            "'导出行 ' || NEW.export_item_id::text\n"
            "                      || ' 的事件序号不连续或未交替（上一条 seq=' || prev_seq::text || '）'",
            "        ",
        )
        + "    END IF;\n"
        "    IF NEW.transition = 'INVALIDATED' THEN\n"
        "        IF NEW.observed_eligibility_hash = frozen_hash THEN\n"
        + _raise("'INVALIDATED 事件的观测指纹与冻结指纹相同'")
        + "        END IF;\n"
        "        IF NEW.reason = 'VERSION_SUPERSEDED' AND NEW.observed_version_id = frozen_version THEN\n"
        + _raise("'VERSION_SUPERSEDED 但观测版本仍等于冻结版本'")
        + "        END IF;\n"
        "        IF NEW.reason = 'ASSET_CHANGED' AND NEW.observed_version_id <> frozen_version THEN\n"
        + _raise("'ASSET_CHANGED 但观测版本已不是冻结版本'")
        + "        END IF;\n"
        "    ELSE\n"
        "        IF NEW.observed_eligibility_hash <> frozen_hash\n"
        "           OR NEW.observed_version_id <> frozen_version THEN\n"
        + _raise("'RESTORED 事件的观测值未回到冻结值'")
        + "        END IF;\n"
        "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE TRIGGER {CONSISTENCY_TRIGGER}\n"
        "AFTER INSERT ON export_validity_events\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {CONSISTENCY_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {CONSISTENCY_TRIGGER} ON export_validity_events"
    drop_function = f"DROP FUNCTION IF EXISTS {CONSISTENCY_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def all_trigger_ddl() -> list[tuple[str, str, str, str, str]]:
    """全部触发器五元组 `(表, 函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)`：不可变在前。"""
    items: list[tuple[str, str, str, str, str]] = []
    for table in IMMUTABLE_TABLES:
        f, t, dt, df = immutable_trigger_ddl(table)
        items.append((table, f, t, dt, df))
    f, t, dt, df = consistency_ddl()
    items.append(("export_validity_events", f, t, dt, df))
    return items


def trigger_names() -> tuple[str, ...]:
    return tuple(immutable_trigger_name(t) for t in IMMUTABLE_TABLES) + (CONSISTENCY_TRIGGER,)


def install_statements() -> list[str]:
    """建触发器的语句序列（DROP 触发器 → CREATE OR REPLACE 函数 → CREATE 触发器）。"""
    statements: list[str] = []
    for _table, func_ddl, trigger_ddl, drop_trigger, _drop_func in all_trigger_ddl():
        statements.extend([drop_trigger, func_ddl, trigger_ddl])
    return statements


__all__ = [
    "CONSISTENCY_FUNCTION",
    "CONSISTENCY_SQLSTATE",
    "CONSISTENCY_TRIGGER",
    "OWN_FUNCTIONS",
    "all_trigger_ddl",
    "consistency_ddl",
    "install_statements",
    "trigger_names",
]
