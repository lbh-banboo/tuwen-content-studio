"""WP30 三张表的 DDL：3 个不可变触发器 + 2 个一致性约束触发器。

## 两个一致性触发器在守什么（FK 管不住的那部分）

FK 只保证"被引用的行存在"，**不**保证"它属于同一条内容 / 同一个项目"。
没有这两条触发器时：

| 坏数据 | 为什么 FK 挡不住 | 症状 |
|---|---|---|
| 一轮自检的 `subject_version_id` 指向**另一条内容**的版本 | 两列各自的 FK 都满足 | "这轮判的是哪一版"有两个答案；STALE 判定永远为真 |
| 一轮自检的 `content_item_id` 不属于 `project_id` | 同上 | 两步骤鉴权反查到的项目与内容真实归属不一致 ⇒ 看板会把别的项目的内容算进来 |
| 一条 finding 的 `media_item_id` 指向**另一条内容**的图 | 同上，且**跨两跳 join**（finding → rule_result → run） | 前端点"去修这张图"跳到别人家的图 |
| 一条 finding 的 `feedback_item_id` 指向另一条内容的反馈 | 同上 | 阻断项与反馈的对应关系有两个答案；WP29 的审核闸拦的是另一条内容 |

守法：`CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED`，**提交时**检查。
延迟是必须的：一轮自检里 run / rule_result / finding / 反馈行可能在同一事务
里以任意顺序插入（服务层先建 finding 再回填 `feedback_item_id` 也是合法的），
逐行即时检查会误报。

**代价要如实登记**：延迟检查意味着错误在 `COMMIT`（或显式
`SET CONSTRAINTS ALL IMMEDIATE`）那一刻才冒出来 —— 证据脚本与测试必须
显式 `SET CONSTRAINTS ALL IMMEDIATE` 才看得到拒绝，否则"坏值写进去了"
会被误读成"防线失效"（WP29 的证据脚本踩过同一个坑）。

## 为什么判据按"表"分两个函数、而不是按"条"分四个

同一张表上的判据**同属插入同一行**、修法也是同一类（改这一行的外键列），
拆成多个触发器只会让"这一行插入失败"出现多条同样的 `sqlstate=23514`
而无法区分。跨表则必须分开：两张表的插入时机与修法都不同。
（与 `content/ddl.py` 把五条判据合一、`baseline/ddl.py` 把不同修法的判据
拆开，是同一条取舍标准的两侧。）

`RETURN NULL` 而不是 `RETURN NEW`：**AFTER 触发器的返回值被忽略**，
写 `NULL` 是明确的"我不改写行"。

## downgrade 的删除范围

`consistency_function_names()` **只列本包自己的两个函数**，**不含**
`rednote_reject_mutation`（跨包共用，ISS-79 的教训：删它会让所有已建表的
不可变守卫变成空触发器）。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.review.models import IMMUTABLE_TABLES
from rednote.platform.immutability import immutable_trigger_ddl, immutable_trigger_name

# PostgreSQL check_violation。与其他模块的 CONSISTENCY_SQLSTATE 同码：
# 语义上都是"某条数据规则被违反了"，由 API 层映射到 422 VALIDATION_FAILED。
CONSISTENCY_SQLSTATE: Final[str] = "23514"

RUN_FUNCTION: Final[str] = "rednote_check_self_check_run_consistency"
RUN_TRIGGER: Final[str] = "trg_self_check_runs_consistency"

FINDING_FUNCTION: Final[str] = "rednote_check_self_check_finding_anchor"
FINDING_TRIGGER: Final[str] = "trg_self_check_findings_anchor_consistency"


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


def run_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    两条判据：
      1. `subject_version_id` → 该版必须属于 `NEW.content_item_id`；
      2. `content_item_id` → 该内容必须属于 `NEW.project_id`。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {RUN_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    version_owner uuid;\n"
        "    content_owner uuid;\n"
        "BEGIN\n"
        # --- 判据 1：被判的那一版必须属于本轮的内容 -------------------------
        "    SELECT content_item_id INTO version_owner\n"
        "      FROM content_versions WHERE id = NEW.subject_version_id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'自检轮次 ' || NEW.id::text || ' 判定的版本 '\n"
            "                      || NEW.subject_version_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        "    IF version_owner IS DISTINCT FROM NEW.content_item_id THEN\n"
        + _raise_block(
            "'自检轮次 ' || NEW.id::text || ' 判定的版本属于内容 '\n"
            "                      || COALESCE(version_owner::text, '(未知)')\n"
            "                      || '，而本轮属于内容 '\n"
            "                      || NEW.content_item_id::text\n"
            "                      || '（自检结论必须锚定在被判的那条内容上）'"
        )
        + "    END IF;\n"
        # --- 判据 2：内容必须属于本轮的项目 ---------------------------------
        "    SELECT project_id INTO content_owner\n"
        "      FROM content_items WHERE id = NEW.content_item_id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'自检轮次 ' || NEW.id::text || ' 的内容 '\n"
            "                      || NEW.content_item_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        "    IF content_owner IS DISTINCT FROM NEW.project_id THEN\n"
        + _raise_block(
            "'自检轮次 ' || NEW.id::text || ' 的内容属于项目 '\n"
            "                      || COALESCE(content_owner::text, '(未知)')\n"
            "                      || '，而本轮记在项目 '\n"
            "                      || NEW.project_id::text\n"
            "                      || '（两步骤鉴权反查的项目必须与真实归属一致）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {RUN_TRIGGER}\n"
        "AFTER INSERT ON self_check_runs\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {RUN_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {RUN_TRIGGER} ON self_check_runs"
    drop_function = f"DROP FUNCTION IF EXISTS {RUN_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def finding_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    两条判据，**都跨两跳 join**（finding → rule_result → run）：
      1. `media_item_id`（可空）→ 该图必须属于本轮的 `content_item_id`；
      2. `feedback_item_id`（可空）→ 该反馈必须属于本轮的 `content_item_id`。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {FINDING_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    run_content uuid;\n"
        "    media_owner uuid;\n"
        "    feedback_owner uuid;\n"
        "BEGIN\n"
        "    IF NEW.media_item_id IS NULL AND NEW.feedback_item_id IS NULL THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        # 本轮判的是哪条内容（跨两跳）。
        "    SELECT r.content_item_id INTO run_content\n"
        "      FROM self_check_rule_results rr\n"
        "      JOIN self_check_runs r ON r.id = rr.run_id\n"
        "     WHERE rr.id = NEW.rule_result_id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'自检发现 ' || NEW.id::text || ' 的规则结论 '\n"
            "                      || NEW.rule_result_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        # --- 判据 1：媒体锚点必须属于本轮的内容 -----------------------------
        "    IF NEW.media_item_id IS NOT NULL THEN\n"
        "        SELECT content_item_id INTO media_owner\n"
        "          FROM media_items WHERE id = NEW.media_item_id;\n"
        "        IF NOT FOUND THEN\n"
        + _raise_block(
            "'自检发现 ' || NEW.id::text || ' 引用的媒体 '\n"
            "                      || NEW.media_item_id::text || ' 不存在'"
        )
        + "        END IF;\n"
        "        IF media_owner IS DISTINCT FROM run_content THEN\n"
        + _raise_block(
            "'自检发现 ' || NEW.id::text || ' 引用的媒体属于内容 '\n"
            "                      || COALESCE(media_owner::text, '(未知)')\n"
            "                      || '，而本轮判的是内容 '\n"
            "                      || run_content::text\n"
            "                      || '（媒体的段落绑定只在同一条内容内有意义）'"
        )
        + "        END IF;\n"
        "    END IF;\n"
        # --- 判据 2：回指的 AI 反馈必须属于本轮的内容 -----------------------
        "    IF NEW.feedback_item_id IS NOT NULL THEN\n"
        "        SELECT content_item_id INTO feedback_owner\n"
        "          FROM content_feedback_items WHERE id = NEW.feedback_item_id;\n"
        "        IF NOT FOUND THEN\n"
        + _raise_block(
            "'自检发现 ' || NEW.id::text || ' 回指的反馈 '\n"
            "                      || NEW.feedback_item_id::text || ' 不存在'"
        )
        + "        END IF;\n"
        "        IF feedback_owner IS DISTINCT FROM run_content THEN\n"
        + _raise_block(
            "'自检发现 ' || NEW.id::text || ' 回指的反馈属于内容 '\n"
            "                      || COALESCE(feedback_owner::text, '(未知)')\n"
            "                      || '，而本轮判的是内容 '\n"
            "                      || run_content::text\n"
            "                      || '（阻断项与反馈必须落在同一条内容上，"
            "否则 WP29 的审核闸拦的是另一条内容）'"
        )
        + "        END IF;\n"
        "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {FINDING_TRIGGER}\n"
        "AFTER INSERT ON self_check_findings\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {FINDING_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {FINDING_TRIGGER} ON self_check_findings"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {FINDING_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块**自己的**业务触发器（downgrade 只删这些，不碰共用函数，ISS-79）。"""
    items: list[tuple[str, str, str, str, str]] = []
    for table, builder in (
        ("self_check_runs", run_consistency_ddl),
        ("self_check_findings", finding_consistency_ddl),
    ):
        function_ddl, trigger_ddl, drop_trigger, drop_function = builder()
        items.append((table, function_ddl, trigger_ddl, drop_trigger, drop_function))
    return items


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
        RUN_TRIGGER,
        FINDING_TRIGGER,
    )


def consistency_function_names() -> tuple[str, ...]:
    """downgrade 只删这些；**不含** `rednote_reject_mutation`（跨包共用）。"""
    return (RUN_FUNCTION, FINDING_FUNCTION)


__all__ = [
    "CONSISTENCY_SQLSTATE",
    "FINDING_FUNCTION",
    "FINDING_TRIGGER",
    "RUN_FUNCTION",
    "RUN_TRIGGER",
    "all_trigger_ddl",
    "consistency_function_names",
    "consistency_items",
    "finding_consistency_ddl",
    "run_consistency_ddl",
    "trigger_names",
]
