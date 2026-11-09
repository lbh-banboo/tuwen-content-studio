"""`deliveries` / `publications` 的 DDL：列级冻结触发器 + 两条一致性触发器（WP36）。

## ① 列级冻结触发器在守什么（本包最核心的一处）

`deliveries` 是**可变表**（状态机 + `lock_version`），但「谁在什么时候拿到了
哪一篇的哪一版」这件事**本身**必须不可改写。平台的 `guard_immutable` 是整行
不可变（`BEFORE UPDATE OR DELETE`），拦住状态机就等于拦住业务。

守法：`BEFORE UPDATE ... FOR EACH ROW` 逐列比对，冻结列一变就 `RAISE`。
两个细节都是踩过的坑：

| 细节 | 为什么 |
|---|---|
| **全部用 `IS DISTINCT FROM`，不得用 `<>`** | 可空列上 `NULL <> NULL` 求值为 **NULL**，`IF NULL THEN` 不进分支 ⇒ 对 `koc_record_id` / `replaces_delivery_id` / `idempotency_key` 这三列，`<>` 写法的防线**对空值全面失效**。本仓已为此在四份 docstring 里留过告警（`modules/baseline/ddl.py:69-78`、`modules/content/ddl.py:167-171`、`:291`、`modules/knowledge/ddl.py:120`） |
| **列名单由 `models.frozen_delivery_columns()` 派生** | 手写名单在加列时会静默漏那一列 ⇒ 表上多出一扇没人知道的后门，而库全绿、测试全绿（铁律 14 的本体形态）。这里连 `RAISE` 的消息都是生成的 |

`RETURN NEW` 而不是 `RETURN NULL`：**BEFORE 触发器返回 NULL 会取消这次 UPDATE**
—— 那会把"拒绝"变成"静默什么都没发生"，调用方拿到 `rowcount = 0` 却不知道为什么。

## ② `deliveries` 的一致性触发器在守什么

FK 只保证"被引用的行存在"，不保证归属，也不保证"复制过来的值真的等于源值"。
没有这条触发器时，裸 SQL 可以插出：

| 失效 | 表现 |
|---|---|
| `content_item_id` 属于别的项目 | 跨项目的内容被登记成本项目的交付 |
| 三个冻结字段与 `export_item_id` 那一行**不一致** | 「固定导出版本」落空 —— 交付行声称交付了 v3，而被引的导出行冻结的是 v1 |
| `koc_record_id` 属于别的项目 | 把别的项目的 KOC 拉进本项目的交付清单 |
| `replaces_delivery_id` 指向别的项目 / 别的内容的交付 | 「替换链」跨内容，可追溯性变成噪声 |

延迟（`DEFERRABLE INITIALLY DEFERRED`）是必须的：被替换的旧行与新行可能在
同一事务里按任意顺序插入，逐行即时检查会误报。

## ③ downgrade 的删除范围

`consistency_function_names()` **只列本包自己的两个函数 + 冻结守卫函数**，
**不含** `rednote_reject_mutation`（跨包共用；ISS-79 的教训是删它会让所有
已建表的不可变守卫变成空触发器）。本包压根没用它 —— 没有整行不可变表。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.delivery.models import frozen_delivery_columns

#: PostgreSQL check_violation。与其他模块的 `CONSISTENCY_SQLSTATE` 同码：
#: 语义上都是"某条数据规则被违反了"。
CONSISTENCY_SQLSTATE: Final[str] = "23514"

FROZEN_FUNCTION: Final[str] = "rednote_guard_delivery_frozen_columns"
FROZEN_TRIGGER: Final[str] = "trg_deliveries_frozen_columns"

DELIVERY_CONSISTENCY_FUNCTION: Final[str] = "rednote_check_delivery_consistency"
DELIVERY_CONSISTENCY_TRIGGER: Final[str] = "trg_deliveries_consistency"

PUBLICATION_CONSISTENCY_FUNCTION: Final[str] = "rednote_check_publication_consistency"
PUBLICATION_CONSISTENCY_TRIGGER: Final[str] = "trg_publications_consistency"


def _raise_block(message_expr: str, indent: str = "        ") -> str:
    """生成一段 RAISE 语句（**字符串拼接，不用 `%`**）。

    psycopg 的 paramstyle 会把 `%` 当参数占位符，未提供参数时直接报
    "unsupported format character"。字符串拼接没有这个坑。
    """
    return (
        f"{indent}RAISE EXCEPTION USING\n"
        f"{indent}    MESSAGE = {message_expr},\n"
        f"{indent}    ERRCODE = '{CONSISTENCY_SQLSTATE}';\n"
    )


def frozen_columns_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    函数体**由 `models.frozen_delivery_columns()` 逐列生成**：加一列而忘了把它
    放进 `MUTABLE_DELIVERY_COLUMNS`，这里自动多出一条守卫（fail-closed）。
    """
    columns = frozen_delivery_columns()
    assert columns, "冻结列为空 —— 说明派生失效了，触发器会变成空壳"

    body = "".join(
        f"    IF NEW.{column} IS DISTINCT FROM OLD.{column} THEN\n"
        + _raise_block(
            f"'交付记录 ' || OLD.id::text || ' 的 {column} 是冻结列，"
            "不可改写（交付的内容与版本不随改稿变化）'",
            indent="        ",
        )
        + "    END IF;\n"
        for column in columns
    )
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {FROZEN_FUNCTION}() RETURNS trigger AS $$\n"
        "BEGIN\n"
        + body
        # ⚠️ RETURN NEW，不是 NULL：BEFORE 触发器返回 NULL 会**取消**这次 UPDATE。
        + "    RETURN NEW;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE TRIGGER {FROZEN_TRIGGER}\n"
        "BEFORE UPDATE ON deliveries\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {FROZEN_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {FROZEN_TRIGGER} ON deliveries"
    drop_function = f"DROP FUNCTION IF EXISTS {FROZEN_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def delivery_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。四条判据见模块 docstring ②。"""
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {DELIVERY_CONSISTENCY_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    content_owner uuid;\n"
        "    koc_owner uuid;\n"
        "    src record;\n"
        "    old_row record;\n"
        "BEGIN\n"
        # --- 判据 1：内容必须属于本项目 --------------------------------------
        "    SELECT project_id INTO content_owner FROM content_items\n"
        "      WHERE id = NEW.content_item_id;\n"
        "    IF content_owner IS DISTINCT FROM NEW.project_id THEN\n"
        + _raise_block(
            "'交付 ' || NEW.id::text || ' 引用了别的项目的内容 '\n"
            "                      || NEW.content_item_id::text",
            indent="        ",
        )
        + "    END IF;\n"
        # --- 判据 2：三个冻结字段必须与被引的导出行逐列相等 ---------------------
        "    SELECT project_id, content_item_id, version_node_id, eligibility_hash\n"
        "      INTO src FROM export_items WHERE id = NEW.export_item_id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'交付 ' || NEW.id::text || ' 引用的导出行 '\n"
            "                      || NEW.export_item_id::text || ' 不存在'",
            indent="        ",
        )
        + "    END IF;\n"
        "    IF src.project_id IS DISTINCT FROM NEW.project_id\n"
        "       OR src.content_item_id IS DISTINCT FROM NEW.content_item_id\n"
        "       OR src.version_node_id IS DISTINCT FROM NEW.version_node_id\n"
        "       OR src.eligibility_hash IS DISTINCT FROM NEW.eligibility_hash THEN\n"
        + _raise_block(
            "'交付 ' || NEW.id::text || ' 的冻结字段与导出行 '\n"
            "                      || NEW.export_item_id::text\n"
            "                      || ' 不一致（固定导出版本必须逐列复制，不得自行指定）'",
            indent="        ",
        )
        + "    END IF;\n"
        # --- 判据 3：KOC 必须属于本项目 --------------------------------------
        "    IF NEW.koc_record_id IS NOT NULL THEN\n"
        "        SELECT project_id INTO koc_owner FROM koc_records\n"
        "          WHERE id = NEW.koc_record_id;\n"
        "        IF koc_owner IS DISTINCT FROM NEW.project_id THEN\n"
        + _raise_block(
            "'交付 ' || NEW.id::text || ' 引用了别的项目的 KOC '\n"
            "                      || NEW.koc_record_id::text",
            indent="            ",
        )
        + "        END IF;\n"
        "    END IF;\n"
        # --- 判据 4：被替换的旧行必须同项目、同内容 ---------------------------
        "    IF NEW.replaces_delivery_id IS NOT NULL THEN\n"
        "        SELECT project_id, content_item_id INTO old_row FROM deliveries\n"
        "          WHERE id = NEW.replaces_delivery_id;\n"
        "        IF NOT FOUND THEN\n"
        + _raise_block(
            "'交付 ' || NEW.id::text || ' 要替换的交付 '\n"
            "                      || NEW.replaces_delivery_id::text || ' 不存在'",
            indent="            ",
        )
        + "        END IF;\n"
        "        IF old_row.project_id IS DISTINCT FROM NEW.project_id\n"
        "           OR old_row.content_item_id IS DISTINCT FROM NEW.content_item_id THEN\n"
        + _raise_block(
            "'交付 ' || NEW.id::text || ' 的替换链跨了项目或内容'\n"
            "                      || '（替换只在同一项目的同一条内容内有意义）'",
            indent="            ",
        )
        + "        END IF;\n"
        "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {DELIVERY_CONSISTENCY_TRIGGER}\n"
        "AFTER INSERT ON deliveries\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {DELIVERY_CONSISTENCY_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {DELIVERY_CONSISTENCY_TRIGGER} ON deliveries"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {DELIVERY_CONSISTENCY_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def publication_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    一条判据：`delivery_id` 指向的交付必须属于本项目。
    「草稿 → 实发」的映射跨项目没有任何业务含义，而 FK 不管归属。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {PUBLICATION_CONSISTENCY_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    delivery_owner uuid;\n"
        "BEGIN\n"
        "    IF NEW.delivery_id IS NOT NULL THEN\n"
        "        SELECT project_id INTO delivery_owner FROM deliveries\n"
        "          WHERE id = NEW.delivery_id;\n"
        "        IF delivery_owner IS DISTINCT FROM NEW.project_id THEN\n"
        + _raise_block(
            "'发布登记 ' || NEW.id::text || ' 引用了别的项目的交付 '\n"
            "                      || NEW.delivery_id::text",
            indent="            ",
        )
        + "        END IF;\n"
        "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {PUBLICATION_CONSISTENCY_TRIGGER}\n"
        "AFTER INSERT ON publications\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {PUBLICATION_CONSISTENCY_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {PUBLICATION_CONSISTENCY_TRIGGER} ON publications"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {PUBLICATION_CONSISTENCY_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块**自己的**业务触发器（downgrade 只删这些，不碰共用函数，ISS-79）。"""
    return [
        ("deliveries", *delivery_consistency_ddl()),
        ("publications", *publication_consistency_ddl()),
    ]


def all_trigger_ddl() -> list[tuple[str, str, str, str, str]]:
    """全部触发器，五元组 `(表名, 函数DDL, 触发器DDL, 触发器DROP, 函数DROP)`。

    顺序：**列级冻结在前**（它守的是最硬的不变量），业务一致性在后。
    本包**没有**整行不可变表 ⇒ 这里不遍历 `IMMUTABLE_TABLES`（它是空元组）。
    """
    items: list[tuple[str, str, str, str, str]] = [
        ("deliveries", *frozen_columns_ddl()),
    ]
    items.extend(consistency_items())
    return items


def trigger_names() -> tuple[str, ...]:
    return (
        FROZEN_TRIGGER,
        DELIVERY_CONSISTENCY_TRIGGER,
        PUBLICATION_CONSISTENCY_TRIGGER,
    )


def consistency_function_names() -> tuple[str, ...]:
    """downgrade 只删这些；**不含** `rednote_reject_mutation`（跨包共用）。"""
    return (
        FROZEN_FUNCTION,
        DELIVERY_CONSISTENCY_FUNCTION,
        PUBLICATION_CONSISTENCY_FUNCTION,
    )


__all__ = [
    "CONSISTENCY_SQLSTATE",
    "DELIVERY_CONSISTENCY_FUNCTION",
    "DELIVERY_CONSISTENCY_TRIGGER",
    "FROZEN_FUNCTION",
    "FROZEN_TRIGGER",
    "PUBLICATION_CONSISTENCY_FUNCTION",
    "PUBLICATION_CONSISTENCY_TRIGGER",
    "all_trigger_ddl",
    "consistency_function_names",
    "consistency_items",
    "delivery_consistency_ddl",
    "frozen_columns_ddl",
    "publication_consistency_ddl",
    "trigger_names",
]
