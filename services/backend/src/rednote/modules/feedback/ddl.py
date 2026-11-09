"""`content_feedback_*` / `content_reviews` 的 DDL：不可变触发器 + 锚点一致性触发器。

## 锚点一致性触发器在守什么

`content_feedback_items` 的三列（`media_item_id` / `observed_version_id` /
`resolved_version_id`）都是**指向别处的 FK**，而 FK 只保证"被引用的行存在"，
不保证"它属于同一条内容"。没有触发器时，一条反馈可以引用**另一条内容**的
图或版本 —— 列表页仍能渲染，但"这段意见对应哪张图"从此有第二个答案。

守法：`CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED`，提交时检查。
延迟是必须的：内容身份行、版本行、媒体行与本行可能在同一事务里、以任意顺序
插入（投影列 FK 本身也是延迟的），逐行即时检查会误报。

## 为什么两条判据合在一个函数里

它们**同属插入同一行**、修法也是同一类（改这一行的外键列），拆成两个触发器
只会让"这一行插入失败"出现两条同样的 `sqlstate=23514` 而无法区分。
与 `content/ddl.py` 把五条判据合一、`baseline/ddl.py` 把不同修法的判据拆开
是同一条取舍标准的两侧。

`RETURN NULL` 而不是 `RETURN NEW`：**AFTER 触发器的返回值被忽略**，
写 `NULL` 是明确的"我不改写行"。

## downgrade 的删除范围

`consistency_function_names()` **只列本包自己的函数**，**不含**
`rednote_reject_mutation`（跨包共用，ISS-79 的教训：删它会让所有已建表的
不可变守卫变成空触发器）。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.feedback.models import IMMUTABLE_TABLES
from rednote.platform.immutability import immutable_trigger_ddl, immutable_trigger_name

# PostgreSQL check_violation。与其他模块的 CONSISTENCY_SQLSTATE 同码：
# 语义上都是"某条数据规则被违反了"，由 API 层映射到 422 VALIDATION_FAILED。
CONSISTENCY_SQLSTATE: Final[str] = "23514"

ANCHOR_FUNCTION: Final[str] = "rednote_check_feedback_anchor_consistency"
ANCHOR_TRIGGER: Final[str] = "trg_content_feedback_items_anchor_consistency"


def _raise_block(message_expr: str) -> str:
    """生成一段 RAISE 语句（**字符串拼接，不用 `%`**）。

    psycopg 的 paramstyle 会把 `%` 当参数占位符，未提供参数时直接报
    "unsupported format character"。字符串拼接没有这个坑。
    """
    return (
        "        RAISE EXCEPTION USING\n"
        f"            MESSAGE = {message_expr},\n"
        f"            ERRCODE = '{CONSISTENCY_SQLSTATE}';\n"
    )


def anchor_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    三条判据：
      1. `media_item_id`（可空）→ 该图必须属于 `NEW.content_item_id`；
      2. `observed_version_id`（必填）→ 该版必须属于 `NEW.content_item_id`；
      3. `resolved_version_id`（可空）→ 同上。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {ANCHOR_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    media_owner uuid;\n"
        "    observed_owner uuid;\n"
        "    resolved_owner uuid;\n"
        "BEGIN\n"
        # --- 判据 1：媒体锚点必须属于同一条内容 -----------------------------
        "    IF NEW.media_item_id IS NOT NULL THEN\n"
        "        SELECT content_item_id INTO media_owner\n"
        "          FROM media_items WHERE id = NEW.media_item_id;\n"
        "        IF NOT FOUND THEN\n"
        + _raise_block(
            "'反馈项 ' || NEW.id::text || ' 引用的媒体 '\n"
            "                      || NEW.media_item_id::text || ' 不存在'"
        )
        + "        END IF;\n"
        "        IF media_owner IS DISTINCT FROM NEW.content_item_id THEN\n"
        + _raise_block(
            "'反馈项 ' || NEW.id::text || ' 引用的媒体属于内容 '\n"
            "                      || COALESCE(media_owner::text, '(未知)')\n"
            "                      || '，而本行属于内容 '\n"
            "                      || NEW.content_item_id::text\n"
            "                      || '（媒体的段落绑定只在同一条内容内有意义）'"
        )
        + "        END IF;\n"
        "    END IF;\n"
        # --- 判据 2：观察版本必须属于同一条内容 -----------------------------
        "    SELECT content_item_id INTO observed_owner\n"
        "      FROM content_versions WHERE id = NEW.observed_version_id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'反馈项 ' || NEW.id::text || ' 观察的版本 '\n"
            "                      || NEW.observed_version_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        "    IF observed_owner IS DISTINCT FROM NEW.content_item_id THEN\n"
        + _raise_block(
            "'反馈项 ' || NEW.id::text || ' 观察的版本属于内容 '\n"
            "                      || COALESCE(observed_owner::text, '(未知)')\n"
            "                      || '，而本行属于内容 '\n"
            "                      || NEW.content_item_id::text\n"
            "                      || '（反馈必须锚定在被反馈的那条内容上）'"
        )
        + "    END IF;\n"
        # --- 判据 3：解决版本必须属于同一条内容 -----------------------------
        "    IF NEW.resolved_version_id IS NOT NULL THEN\n"
        "        SELECT content_item_id INTO resolved_owner\n"
        "          FROM content_versions WHERE id = NEW.resolved_version_id;\n"
        "        IF NOT FOUND THEN\n"
        + _raise_block(
            "'反馈项 ' || NEW.id::text || ' 的解决版本 '\n"
            "                      || NEW.resolved_version_id::text || ' 不存在'"
        )
        + "        END IF;\n"
        "        IF resolved_owner IS DISTINCT FROM NEW.content_item_id THEN\n"
        + _raise_block(
            "'反馈项 ' || NEW.id::text || ' 的解决版本属于内容 '\n"
            "                      || COALESCE(resolved_owner::text, '(未知)')\n"
            "                      || '，而本行属于内容 '\n"
            "                      || NEW.content_item_id::text"
        )
        + "        END IF;\n"
        "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {ANCHOR_TRIGGER}\n"
        "AFTER INSERT ON content_feedback_items\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {ANCHOR_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {ANCHOR_TRIGGER} ON content_feedback_items"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {ANCHOR_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块**自己的**业务触发器（downgrade 只删这些，不碰共用函数，ISS-79）。"""
    function_ddl, trigger_ddl, drop_trigger, drop_function = (
        anchor_consistency_ddl()
    )
    return [
        (
            "content_feedback_items",
            function_ddl,
            trigger_ddl,
            drop_trigger,
            drop_function,
        )
    ]


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
    return tuple(immutable_trigger_name(t) for t in IMMUTABLE_TABLES) + (
        ANCHOR_TRIGGER,
    )


def consistency_function_names() -> tuple[str, ...]:
    """downgrade 只删这些；**不含** `rednote_reject_mutation`（跨包共用）。"""
    return (ANCHOR_FUNCTION,)


__all__ = [
    "ANCHOR_FUNCTION",
    "ANCHOR_TRIGGER",
    "CONSISTENCY_SQLSTATE",
    "all_trigger_ddl",
    "anchor_consistency_ddl",
    "consistency_function_names",
    "consistency_items",
    "trigger_names",
]
