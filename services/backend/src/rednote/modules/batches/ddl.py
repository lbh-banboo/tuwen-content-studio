"""WP32 三张表的 DDL：2 个不可变触发器 + 4 个延迟一致性约束触发器（共 6 个，3 个本包函数）。

## 四个一致性触发器在守什么（FK / CHECK 管不住的那部分）

| 坏数据 | 为什么 FK / CHECK 挡不住 | 症状 |
|---|---|---|
| 批次 `quantity` 与成员行数不一致 | 两处声称，没有约束把它们连起来 | “提交了 30 条，只生成了 28 条”且批次看起来正常 |
| 成员内容属于别的项目 | FK 只保证被引用行存在，不管归属 | 批次把别的项目的内容算进自己的进度与费用 |
| 执行轮次的 job 其 `batch_id` / `content_id` 与成员不一致 | `jobs.batch_id` / `content_id` 是无 FK 预留位 | 进度折算读 `batch_item_runs`、按批次统计 job 读 `jobs.batch_id`，两条路径各说各话 |

守法：`CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED`，**提交时**检查。
延迟是必须的：同一事务里先建批次、后建成员、再建 job 与轮次，逐行即时检查会误报。

**代价要如实登记**：错误在 `COMMIT`（或显式 `SET CONSTRAINTS ALL IMMEDIATE`）那一刻才冒出来 ——
测试必须显式 `SET CONSTRAINTS ALL IMMEDIATE` 才看得见拒绝。

## downgrade 的删除范围

`consistency_function_names()` **只列本包自己的三个函数**，**不含** `rednote_reject_mutation`
（跨包共用，ISS-79：删它会让所有已建表的不可变守卫变成空触发器）。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.batches.models import IMMUTABLE_TABLES
from rednote.platform.immutability import immutable_trigger_ddl, immutable_trigger_name

#: PostgreSQL check_violation（与其他模块的 CONSISTENCY_SQLSTATE 同码）。
CONSISTENCY_SQLSTATE: Final[str] = "23514"

COUNT_FUNCTION: Final[str] = "rednote_check_batch_item_count"
BATCH_COUNT_TRIGGER: Final[str] = "trg_batches_item_count_consistency"
ITEM_COUNT_TRIGGER: Final[str] = "trg_batch_items_count_consistency"

ITEM_OWNERSHIP_FUNCTION: Final[str] = "rednote_check_batch_item_ownership"
ITEM_OWNERSHIP_TRIGGER: Final[str] = "trg_batch_items_ownership_consistency"

RUN_OWNERSHIP_FUNCTION: Final[str] = "rednote_check_batch_item_run_ownership"
RUN_OWNERSHIP_TRIGGER: Final[str] = "trg_batch_item_runs_ownership_consistency"


def _raise_block(message_expr: str, indent: str = "        ") -> str:
    """生成一段 RAISE（**字符串拼接，不用 `%`**：psycopg 会把 `%` 当占位符）。"""
    return (
        f"{indent}RAISE EXCEPTION USING\n"
        f"{indent}    MESSAGE = {message_expr},\n"
        f"{indent}    ERRCODE = '{CONSISTENCY_SQLSTATE}';\n"
    )


def count_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP) —— **批次侧**触发器；
    同一函数按 `TG_TABLE_NAME` 分支，另一个触发器见 `item_count_trigger_ddl`。"""
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {COUNT_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    expected integer;\n"
        "    actual integer;\n"
        "    target_batch uuid;\n"
        "BEGIN\n"
        "    IF TG_TABLE_NAME = 'batches' THEN\n"
        "        target_batch := NEW.id;\n"
        "        expected := NEW.quantity;\n"
        "    ELSE\n"
        "        target_batch := NEW.batch_id;\n"
        "        SELECT quantity INTO expected FROM batches WHERE id = target_batch;\n"
        "        IF NOT FOUND THEN\n"
        + _raise_block(
            "'批次成员 ' || NEW.id::text || ' 的批次 ' || target_batch::text || ' 不存在'",
            "            ",
        )
        + "        END IF;\n"
        "    END IF;\n"
        "    SELECT count(*) INTO actual FROM batch_items WHERE batch_id = target_batch;\n"
        "    IF actual IS DISTINCT FROM expected THEN\n"
        + _raise_block(
            "'批次 ' || target_batch::text || ' 的 quantity=' || expected::text\n"
            "                      || '，而成员行数=' || actual::text\n"
            "                      || '（quantity 与成员行数必须一致）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {BATCH_COUNT_TRIGGER}\n"
        "AFTER INSERT ON batches\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {COUNT_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {BATCH_COUNT_TRIGGER} ON batches"
    drop_function = f"DROP FUNCTION IF EXISTS {COUNT_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def item_count_trigger_ddl() -> tuple[str, str, str]:
    """成员侧触发器（复用 `COUNT_FUNCTION`）：返回 (触发器 DDL, 触发器 DROP, 触发器名)。"""
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {ITEM_COUNT_TRIGGER}\n"
        "AFTER INSERT ON batch_items\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {COUNT_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {ITEM_COUNT_TRIGGER} ON batch_items"
    return trigger_ddl, drop_trigger, ITEM_COUNT_TRIGGER


def item_ownership_ddl() -> tuple[str, str, str, str]:
    """成员内容必须与批次同项目。"""
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {ITEM_OWNERSHIP_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    content_owner uuid;\n"
        "    batch_owner uuid;\n"
        "BEGIN\n"
        "    SELECT project_id INTO content_owner\n"
        "      FROM content_items WHERE id = NEW.content_item_id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'批次成员 ' || NEW.id::text || ' 的内容 '\n"
            "                      || NEW.content_item_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        "    SELECT project_id INTO batch_owner FROM batches WHERE id = NEW.batch_id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'批次成员 ' || NEW.id::text || ' 的批次 ' || NEW.batch_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        "    IF content_owner IS DISTINCT FROM batch_owner THEN\n"
        + _raise_block(
            "'批次成员 ' || NEW.id::text || ' 的内容属于项目 '\n"
            "                      || COALESCE(content_owner::text, '(未知)')\n"
            "                      || '，而批次属于项目 '\n"
            "                      || COALESCE(batch_owner::text, '(未知)')\n"
            "                      || '（批次只能含本项目的内容）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {ITEM_OWNERSHIP_TRIGGER}\n"
        "AFTER INSERT ON batch_items\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {ITEM_OWNERSHIP_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {ITEM_OWNERSHIP_TRIGGER} ON batch_items"
    drop_function = f"DROP FUNCTION IF EXISTS {ITEM_OWNERSHIP_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def run_ownership_ddl() -> tuple[str, str, str, str]:
    """执行轮次的 job：`jobs.batch_id` 必须非空且 = 成员的批次，`jobs.content_id` = 成员的内容。"""
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {RUN_OWNERSHIP_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    item_batch uuid;\n"
        "    item_content uuid;\n"
        "    job_batch uuid;\n"
        "    job_content uuid;\n"
        "BEGIN\n"
        "    SELECT batch_id, content_item_id INTO item_batch, item_content\n"
        "      FROM batch_items WHERE id = NEW.batch_item_id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'执行轮次 ' || NEW.id::text || ' 的成员 '\n"
            "                      || NEW.batch_item_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        "    SELECT batch_id, content_id INTO job_batch, job_content\n"
        "      FROM jobs WHERE id = NEW.job_id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'执行轮次 ' || NEW.id::text || ' 的任务 ' || NEW.job_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        "    IF job_batch IS DISTINCT FROM item_batch THEN\n"
        + _raise_block(
            "'执行轮次 ' || NEW.id::text || ' 的任务 batch_id='\n"
            "                      || COALESCE(job_batch::text, '(空)')\n"
            "                      || '，而成员属于批次 ' || item_batch::text\n"
            "                      || '（jobs.batch_id 必须非空且与成员批次一致）'"
        )
        + "    END IF;\n"
        "    IF job_content IS DISTINCT FROM item_content THEN\n"
        + _raise_block(
            "'执行轮次 ' || NEW.id::text || ' 的任务 content_id='\n"
            "                      || COALESCE(job_content::text, '(空)')\n"
            "                      || '，而成员内容是 ' || item_content::text"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {RUN_OWNERSHIP_TRIGGER}\n"
        "AFTER INSERT ON batch_item_runs\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {RUN_OWNERSHIP_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {RUN_OWNERSHIP_TRIGGER} ON batch_item_runs"
    drop_function = f"DROP FUNCTION IF EXISTS {RUN_OWNERSHIP_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块**自己的**业务触发器五元组（`(表, 函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)`）。

    成员侧的计数触发器**复用** `COUNT_FUNCTION`：它的函数 DDL 是同一条 `CREATE OR REPLACE`
    （幂等），函数 DROP 是同一条 `DROP FUNCTION IF EXISTS`（第二次执行为空操作）。
    这样五元组形状与其他模块完全一致，迁移里 `op.execute` 逐项执行不需要特判。
    """
    items: list[tuple[str, str, str, str, str]] = []
    f_ddl_count, t_ddl, d_trg, d_fn_count = count_consistency_ddl()
    items.append(("batches", f_ddl_count, t_ddl, d_trg, d_fn_count))
    item_trg, item_drop, _name = item_count_trigger_ddl()
    items.append(("batch_items", f_ddl_count, item_trg, item_drop, d_fn_count))
    f_ddl, t_ddl, d_trg, d_fn = item_ownership_ddl()
    items.append(("batch_items", f_ddl, t_ddl, d_trg, d_fn))
    f_ddl, t_ddl, d_trg, d_fn = run_ownership_ddl()
    items.append(("batch_item_runs", f_ddl, t_ddl, d_trg, d_fn))
    return items


def all_trigger_ddl() -> list[tuple[str, str, str, str, str]]:
    """全部触发器：不可变保护在前，业务不变量在后。

    成员侧计数触发器与批次侧复用同一个函数（见 `consistency_items`）。
    """
    items: list[tuple[str, str, str, str, str]] = []
    for table in IMMUTABLE_TABLES:
        function_ddl, trigger_ddl, drop_trigger, drop_function = immutable_trigger_ddl(
            table
        )
        items.append((table, function_ddl, trigger_ddl, drop_trigger, drop_function))
    items.extend(consistency_items())
    return items


def trigger_names() -> tuple[str, ...]:
    return tuple(immutable_trigger_name(t) for t in IMMUTABLE_TABLES) + (
        BATCH_COUNT_TRIGGER,
        ITEM_COUNT_TRIGGER,
        ITEM_OWNERSHIP_TRIGGER,
        RUN_OWNERSHIP_TRIGGER,
    )


def consistency_function_names() -> tuple[str, ...]:
    """downgrade 只删这三个；**不含** `rednote_reject_mutation`（跨包共用）。"""
    return (COUNT_FUNCTION, ITEM_OWNERSHIP_FUNCTION, RUN_OWNERSHIP_FUNCTION)


def install_statements() -> list[str]:
    """建触发器的语句序列（DROP 触发器 → CREATE OR REPLACE 函数 → CREATE 触发器）。

    `CREATE TRIGGER` 不幂等，所以先 DROP；`CREATE OR REPLACE FUNCTION` 幂等。
    测试夹具与主线迁移可共用同一份来源。
    """
    statements: list[str] = []
    for _table, func_ddl, trigger_ddl, drop_trigger, _drop_func in all_trigger_ddl():
        statements.append(drop_trigger)
        statements.append(func_ddl)
        statements.append(trigger_ddl)
    return statements


__all__ = [
    "BATCH_COUNT_TRIGGER",
    "CONSISTENCY_SQLSTATE",
    "COUNT_FUNCTION",
    "ITEM_COUNT_TRIGGER",
    "ITEM_OWNERSHIP_FUNCTION",
    "ITEM_OWNERSHIP_TRIGGER",
    "RUN_OWNERSHIP_FUNCTION",
    "RUN_OWNERSHIP_TRIGGER",
    "all_trigger_ddl",
    "consistency_function_names",
    "consistency_items",
    "count_consistency_ddl",
    "install_statements",
    "item_count_trigger_ddl",
    "item_ownership_ddl",
    "run_ownership_ddl",
    "trigger_names",
]
