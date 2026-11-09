"""`export_items` 的 DDL：不可变触发器 + 组一致性延迟触发器（WP35）。

## 组一致性触发器在守什么（A' 的最大设计风险，R4）

A' 不建组表 ⇒ "同一次导出"只是若干行共享一个 `export_group_id`。
没有任何东西保证这些行说的是同一件事，于是会出现：

| 失效 | 表现 |
|---|---|
| 同组两行 `zip_sha256` / `artifact_file_id` 不同 | 下载哪一行得到的字节不同 ⇒ "固定版本"落空 |
| 同组行数 ≠ `item_count` | 少一行 ⇒ 用户以为导出了 3 篇实际清单只有 2 篇 |
| 同组某行 `project_id` 不同 | 跨项目内容混进同一个包 |

守法：`CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED`，提交时按**组**检查。
延迟是必须的：一组的 N 行是逐条 INSERT 的，逐行即时检查会在第 1 行就因"行数不够"误报。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.exports.models import IMMUTABLE_TABLES
from rednote.platform.immutability import immutable_trigger_ddl

CONSISTENCY_SQLSTATE: Final[str] = "23514"

GROUP_FUNCTION: Final[str] = "rednote_check_export_group_consistency"
GROUP_TRIGGER: Final[str] = "trg_export_items_group_consistency"


def group_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    RAISE 用字符串拼接而不用 `%` 占位符（psycopg paramstyle 会把 `%` 当占位符）。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {GROUP_FUNCTION}() RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    distinct_attrs integer;\n"
        "    actual_rows integer;\n"
        "    declared integer;\n"
        "BEGIN\n"
        # 组级属性必须全组唯一：5 列的去重组合数应为 1。
        "    SELECT count(*) INTO distinct_attrs FROM (\n"
        "        SELECT DISTINCT project_id, export_kind, export_format,\n"
        "               item_count, artifact_file_id, zip_sha256, zip_byte_size,\n"
        "               created_by, request_hash\n"
        "        FROM export_items WHERE export_group_id = NEW.export_group_id\n"
        "    ) t;\n"
        "    IF distinct_attrs <> 1 THEN\n"
        "        RAISE EXCEPTION USING\n"
        "            MESSAGE = '导出组 ' || NEW.export_group_id::text\n"
        "                      || ' 的组级属性不一致（' || distinct_attrs::text\n"
        "                      || ' 种组合）',\n"
        f"            ERRCODE = '{CONSISTENCY_SQLSTATE}';\n"
        "    END IF;\n"
        "    SELECT count(*) INTO actual_rows FROM export_items\n"
        "        WHERE export_group_id = NEW.export_group_id;\n"
        "    declared := NEW.item_count;\n"
        "    IF actual_rows <> declared THEN\n"
        "        RAISE EXCEPTION USING\n"
        "            MESSAGE = '导出组 ' || NEW.export_group_id::text\n"
        "                      || ' 声明 ' || declared::text || ' 条，实际 '\n"
        "                      || actual_rows::text || ' 行',\n"
        f"            ERRCODE = '{CONSISTENCY_SQLSTATE}';\n"
        "    END IF;\n"
        # 内容必须属于本项目（FK 只保证内容存在，不保证归属）。
        "    IF EXISTS (\n"
        "        SELECT 1 FROM content_items ci\n"
        "        WHERE ci.id = NEW.content_item_id AND ci.project_id <> NEW.project_id\n"
        "    ) THEN\n"
        "        RAISE EXCEPTION USING\n"
        "            MESSAGE = '导出行引用了别的项目的内容 '\n"
        "                      || NEW.content_item_id::text,\n"
        f"            ERRCODE = '{CONSISTENCY_SQLSTATE}';\n"
        "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {GROUP_TRIGGER}\n"
        "AFTER INSERT ON export_items\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {GROUP_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {GROUP_TRIGGER} ON export_items"
    drop_function = f"DROP FUNCTION IF EXISTS {GROUP_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块**自己的**一致性触发器（downgrade 只删这些，不碰共用函数，ISS-79）。"""
    function_ddl, trigger_ddl, drop_trigger, drop_function = group_consistency_ddl()
    return [("export_items", function_ddl, trigger_ddl, drop_trigger, drop_function)]


def all_trigger_ddl() -> list[tuple[str, str, str, str, str]]:
    """全部触发器：不可变保护在前，业务不变量在后。"""
    items: list[tuple[str, str, str, str, str]] = []
    for table in IMMUTABLE_TABLES:
        function_ddl, trigger_ddl, drop_trigger, drop_function = (
            immutable_trigger_ddl(table)
        )
        items.append((table, function_ddl, trigger_ddl, drop_trigger, drop_function))
    items.extend(consistency_items())
    return items


def trigger_names() -> tuple[str, ...]:
    return tuple(f"trg_{t}_immutable" for t in IMMUTABLE_TABLES) + (GROUP_TRIGGER,)


def consistency_function_names() -> tuple[str, ...]:
    """downgrade 只删这些；**不含** `rednote_reject_mutation`（跨包共用）。"""
    return (GROUP_FUNCTION,)


__all__ = [
    "CONSISTENCY_SQLSTATE",
    "GROUP_FUNCTION",
    "GROUP_TRIGGER",
    "all_trigger_ddl",
    "consistency_function_names",
    "consistency_items",
    "group_consistency_ddl",
    "trigger_names",
]
