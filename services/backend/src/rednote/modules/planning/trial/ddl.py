"""WP31 三张表的 DDL：六个触发器（一个不可变 + 两个 BEFORE 守卫 + 三个约束触发器）。

## 六个触发器各守什么

| 触发器 | 表 | 时机 | 守什么 | sqlstate |
|---|---|---|---|---|
| `trg_trial_rounds_transition_guard` | `trial_rounds` | BEFORE UPDATE/DELETE | DELETE 一律拒；不可变列不得变；`state` 变化必须落在 `ROUND_TRANSITIONS` 的边上 | 23001 / 23514 |
| `trg_trial_rounds_consistency` | `trial_rounds` | AFTER INSERT/UPDATE，延迟 | INSERT：PLAN 节点 / 基线 / 源轮归属；INSERT OR UPDATE：到 `GATE_PASSED` 必有决定行 | 23514 |
| `trg_trial_round_slots_guard` | `trial_round_slots` | BEFORE UPDATE/DELETE | 名额 write-once：DELETE 拒；仅 `content_item_id`/`bound_at` 可 NULL→值 一次 | 23001 |
| `trg_trial_round_slots_consistency` | `trial_round_slots` | AFTER INSERT/UPDATE，延迟 | 内容属同项目；替代名额同轮同方向；终态轮不得再加名额 | 23514 |
| `trg_trial_gate_decisions_immutable` | `trial_gate_decisions` | BEFORE UPDATE/DELETE | 决定落库即冻结（复用跨包共用 `rednote_reject_mutation`） | 23001 |
| `trg_trial_gate_decisions_consistency` | `trial_gate_decisions` | AFTER INSERT，延迟 | 存在决定 ⇒ 所属轮必须 `GATE_PASSED`（与 rounds_consistency 合成双向） | 23514 |

状态迁移矩阵**不手抄**：触发器函数体里的矩阵由 `types.ROUND_TRANSITIONS` 生成，
测试再从函数体反解析出来与常量对账（DDL 草案第一节第 5 条的"一条判据"）。

延迟约束触发器的代价要如实登记：错误在 `COMMIT`（或显式 `SET CONSTRAINTS ALL IMMEDIATE`）
那一刻才冒出来，测试必须显式 `SET CONSTRAINTS ALL IMMEDIATE` 才看得到拒绝。

## downgrade 的删除范围

`OWN_FUNCTIONS` 只列本包自己的**五个**函数，**不含** `rednote_reject_mutation`（ISS-79）。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.planning.trial.models import IMMUTABLE_TABLES
from rednote.modules.planning.trial.types import (
    ROUND_KIND_TRIAL,
    ROUND_STATE_GATE_PASSED,
    ROUND_TERMINAL_STATES,
    ROUND_TRANSITIONS,
)
from rednote.platform.immutability import (
    IMMUTABLE_RECORD_SQLSTATE,
    immutable_trigger_ddl,
    immutable_trigger_name,
)

CONSISTENCY_SQLSTATE: Final[str] = "23514"

TRANSITION_FUNCTION: Final[str] = "rednote_guard_trial_round_transition"
TRANSITION_TRIGGER: Final[str] = "trg_trial_rounds_transition_guard"
ROUND_CONSISTENCY_FUNCTION: Final[str] = "rednote_check_trial_round_consistency"
ROUND_CONSISTENCY_TRIGGER: Final[str] = "trg_trial_rounds_consistency"
SLOT_GUARD_FUNCTION: Final[str] = "rednote_guard_trial_round_slot"
SLOT_GUARD_TRIGGER: Final[str] = "trg_trial_round_slots_guard"
SLOT_CONSISTENCY_FUNCTION: Final[str] = "rednote_check_trial_round_slot_consistency"
SLOT_CONSISTENCY_TRIGGER: Final[str] = "trg_trial_round_slots_consistency"
DECISION_CONSISTENCY_FUNCTION: Final[str] = (
    "rednote_check_trial_gate_decision_consistency"
)
DECISION_CONSISTENCY_TRIGGER: Final[str] = "trg_trial_gate_decisions_consistency"

#: 本包**自己的**函数（downgrade 只删这五个）。
OWN_FUNCTIONS: Final[tuple[str, ...]] = (
    TRANSITION_FUNCTION,
    ROUND_CONSISTENCY_FUNCTION,
    SLOT_GUARD_FUNCTION,
    SLOT_CONSISTENCY_FUNCTION,
    DECISION_CONSISTENCY_FUNCTION,
)

#: 触发器函数体里迁移矩阵的起止标记（测试据此反解析）。
MATRIX_BEGIN: Final[str] = "-- MATRIX-BEGIN"
MATRIX_END: Final[str] = "-- MATRIX-END"

#: `trial_rounds` 中除 state / cancel_reason / updated_at / lock_version 外的不可变列。
ROUND_IMMUTABLE_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "project_id",
    "round_no",
    "kind",
    "plan_version_id",
    "baseline_id",
    "source_round_id",
    "requested_quantity",
    "planned_quantity",
    "limit_applied",
    "limit_source",
    "plan_snapshot",
    "idempotency_key",
    "request_hash",
    "created_by",
    "created_at",
)

SLOT_IMMUTABLE_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "round_id",
    "slot_no",
    "scene_key",
    "form_key",
    "replaces_slot_id",
    "created_at",
)


def _raise(message_expr: str, sqlstate: str, *, indent: int = 8) -> str:
    """一段 RAISE（**字符串拼接，不用 `%`**：psycopg 会把 `%` 当占位符）。"""
    pad = " " * indent
    return (
        f"{pad}RAISE EXCEPTION USING\n"
        f"{pad}    MESSAGE = {message_expr},\n"
        f"{pad}    ERRCODE = '{sqlstate}';\n"
    )


def _in_list(values: frozenset[str]) -> str:
    return ", ".join(f"'{v}'" for v in sorted(values))


def _matrix_condition() -> str:
    """由 `ROUND_TRANSITIONS` 生成的"合法边"布尔表达式。"""
    clauses = [
        f"(OLD.state = '{src}' AND NEW.state IN ({_in_list(dst)}))"
        for src, dst in sorted(ROUND_TRANSITIONS.items())
        if dst
    ]
    return "\n             OR ".join(clauses)


def round_transition_guard_ddl() -> tuple[str, str, str, str]:
    immutable_checks = "\n             OR ".join(
        f"NEW.{col} IS DISTINCT FROM OLD.{col}" for col in ROUND_IMMUTABLE_COLUMNS
    )
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {TRANSITION_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "BEGIN\n"
        "    IF TG_OP = 'DELETE' THEN\n"
        + _raise(
            "'trial_rounds 只取消、不删除（轮 ' || OLD.id::text || '）'",
            IMMUTABLE_RECORD_SQLSTATE,
            indent=8,
        )
        + "    END IF;\n"
        f"    IF {immutable_checks} THEN\n"
        + _raise(
            "'trial_rounds ' || OLD.id::text"
            " || ' 除 state/cancel_reason/updated_at/lock_version 外的列不可修改'",
            IMMUTABLE_RECORD_SQLSTATE,
            indent=8,
        )
        + "    END IF;\n"
        "    IF NEW.state IS DISTINCT FROM OLD.state THEN\n"
        f"        {MATRIX_BEGIN}\n"
        f"        IF NOT ({_matrix_condition()}) THEN\n"
        + _raise(
            "'trial_rounds ' || OLD.id::text || ' 的状态迁移 '"
            " || OLD.state || ' -> ' || NEW.state || ' 不在合法迁移矩阵内'",
            CONSISTENCY_SQLSTATE,
            indent=12,
        )
        + "        END IF;\n"
        f"        {MATRIX_END}\n"
        "    END IF;\n"
        "    RETURN NEW;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE TRIGGER {TRANSITION_TRIGGER}\n"
        "BEFORE UPDATE OR DELETE ON trial_rounds\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {TRANSITION_FUNCTION}()"
    )
    return (
        function_ddl,
        trigger_ddl,
        f"DROP TRIGGER IF EXISTS {TRANSITION_TRIGGER} ON trial_rounds",
        f"DROP FUNCTION IF EXISTS {TRANSITION_FUNCTION}()",
    )


def round_consistency_ddl() -> tuple[str, str, str, str]:
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {ROUND_CONSISTENCY_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    node_type text;\n"
        "    node_entity uuid;\n"
        "    node_project uuid;\n"
        "    baseline_project uuid;\n"
        "    source_project uuid;\n"
        "    source_kind text;\n"
        "BEGIN\n"
        "    IF TG_OP = 'INSERT' THEN\n"
        # --- PLAN 节点归属 ---
        "        SELECT entity_type, entity_id, project_id\n"
        "          INTO node_type, node_entity, node_project\n"
        "          FROM version_nodes WHERE id = NEW.plan_version_id;\n"
        "        IF NOT FOUND THEN\n"
        + _raise("'试产轮 ' || NEW.id::text || ' 的 PLAN 版本不存在'", CONSISTENCY_SQLSTATE, indent=12)
        + "        END IF;\n"
        "        IF node_type IS DISTINCT FROM 'PLAN'\n"
        "           OR node_entity IS DISTINCT FROM NEW.project_id\n"
        "           OR node_project IS DISTINCT FROM NEW.project_id THEN\n"
        + _raise(
            "'试产轮 ' || NEW.id::text || ' 钉住的版本不是本项目的 PLAN 版本'",
            CONSISTENCY_SQLSTATE,
            indent=12,
        )
        + "        END IF;\n"
        # --- 基线归属 ---
        "        SELECT project_id INTO baseline_project\n"
        "          FROM production_baselines WHERE id = NEW.baseline_id;\n"
        "        IF NOT FOUND OR baseline_project IS DISTINCT FROM NEW.project_id THEN\n"
        + _raise(
            "'试产轮 ' || NEW.id::text || ' 钉住的基线不属于本项目'",
            CONSISTENCY_SQLSTATE,
            indent=12,
        )
        + "        END IF;\n"
        # --- 源轮归属 ---
        "        IF NEW.source_round_id IS NOT NULL THEN\n"
        "            SELECT project_id, kind INTO source_project, source_kind\n"
        "              FROM trial_rounds WHERE id = NEW.source_round_id;\n"
        "            IF NOT FOUND\n"
        "               OR source_project IS DISTINCT FROM NEW.project_id\n"
        f"               OR source_kind IS DISTINCT FROM '{ROUND_KIND_TRIAL}' THEN\n"
        + _raise(
            "'补充轮 ' || NEW.id::text || ' 的源轮必须是同项目的 TRIAL 轮'",
            CONSISTENCY_SQLSTATE,
            indent=16,
        )
        + "            END IF;\n"
        "        END IF;\n"
        "    END IF;\n"
        # --- 到 GATE_PASSED 必有决定行（延迟到提交时判） ---
        f"    IF NEW.state = '{ROUND_STATE_GATE_PASSED}' THEN\n"
        "        IF NOT EXISTS (\n"
        "            SELECT 1 FROM trial_gate_decisions d WHERE d.round_id = NEW.id\n"
        "        ) THEN\n"
        + _raise(
            "'试产轮 ' || NEW.id::text"
            " || ' 处于 GATE_PASSED 但没有关口决定（没有人工决定就到不了该状态）'",
            CONSISTENCY_SQLSTATE,
            indent=12,
        )
        + "        END IF;\n"
        "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {ROUND_CONSISTENCY_TRIGGER}\n"
        "AFTER INSERT OR UPDATE ON trial_rounds\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {ROUND_CONSISTENCY_FUNCTION}()"
    )
    return (
        function_ddl,
        trigger_ddl,
        f"DROP TRIGGER IF EXISTS {ROUND_CONSISTENCY_TRIGGER} ON trial_rounds",
        f"DROP FUNCTION IF EXISTS {ROUND_CONSISTENCY_FUNCTION}()",
    )


def slot_guard_ddl() -> tuple[str, str, str, str]:
    immutable_checks = "\n             OR ".join(
        f"NEW.{col} IS DISTINCT FROM OLD.{col}" for col in SLOT_IMMUTABLE_COLUMNS
    )
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {SLOT_GUARD_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "BEGIN\n"
        "    IF TG_OP = 'DELETE' THEN\n"
        + _raise(
            "'trial_round_slots 不允许 DELETE（名额 ' || OLD.id::text || '）'",
            IMMUTABLE_RECORD_SQLSTATE,
        )
        + "    END IF;\n"
        f"    IF {immutable_checks} THEN\n"
        + _raise(
            "'trial_round_slots ' || OLD.id::text || ' 的方向/序号/替代关系不可修改'",
            IMMUTABLE_RECORD_SQLSTATE,
        )
        + "    END IF;\n"
        "    IF (OLD.content_item_id IS NOT NULL\n"
        "        AND NEW.content_item_id IS DISTINCT FROM OLD.content_item_id)\n"
        "       OR (OLD.bound_at IS NOT NULL\n"
        "           AND NEW.bound_at IS DISTINCT FROM OLD.bound_at) THEN\n"
        + _raise(
            "'trial_round_slots ' || OLD.id::text || ' 的内容绑定是 write-once，不可改写'",
            IMMUTABLE_RECORD_SQLSTATE,
        )
        + "    END IF;\n"
        "    RETURN NEW;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE TRIGGER {SLOT_GUARD_TRIGGER}\n"
        "BEFORE UPDATE OR DELETE ON trial_round_slots\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {SLOT_GUARD_FUNCTION}()"
    )
    return (
        function_ddl,
        trigger_ddl,
        f"DROP TRIGGER IF EXISTS {SLOT_GUARD_TRIGGER} ON trial_round_slots",
        f"DROP FUNCTION IF EXISTS {SLOT_GUARD_FUNCTION}()",
    )


def slot_consistency_ddl() -> tuple[str, str, str, str]:
    terminal = _in_list(ROUND_TERMINAL_STATES)
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {SLOT_CONSISTENCY_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    round_project uuid;\n"
        "    round_state text;\n"
        "    content_project uuid;\n"
        "    replaced_round uuid;\n"
        "    replaced_scene text;\n"
        "BEGIN\n"
        "    SELECT project_id, state INTO round_project, round_state\n"
        "      FROM trial_rounds WHERE id = NEW.round_id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise("'名额 ' || NEW.id::text || ' 所属的轮不存在'", CONSISTENCY_SQLSTATE)
        + "    END IF;\n"
        # ① 内容属于同一项目
        "    IF NEW.content_item_id IS NOT NULL THEN\n"
        "        SELECT project_id INTO content_project\n"
        "          FROM content_items WHERE id = NEW.content_item_id;\n"
        "        IF NOT FOUND OR content_project IS DISTINCT FROM round_project THEN\n"
        + _raise(
            "'名额 ' || NEW.id::text || ' 绑定的内容不属于本轮所在项目'",
            CONSISTENCY_SQLSTATE,
            indent=12,
        )
        + "        END IF;\n"
        "    END IF;\n"
        # ② 替代名额同轮同方向
        "    IF NEW.replaces_slot_id IS NOT NULL THEN\n"
        "        SELECT round_id, scene_key INTO replaced_round, replaced_scene\n"
        "          FROM trial_round_slots WHERE id = NEW.replaces_slot_id;\n"
        "        IF NOT FOUND\n"
        "           OR replaced_round IS DISTINCT FROM NEW.round_id\n"
        "           OR replaced_scene IS DISTINCT FROM NEW.scene_key THEN\n"
        + _raise(
            "'名额 ' || NEW.id::text || ' 的替代对象必须在同一轮且方向相同'",
            CONSISTENCY_SQLSTATE,
            indent=12,
        )
        + "        END IF;\n"
        "    END IF;\n"
        # ③ 终态轮不得再加名额
        f"    IF TG_OP = 'INSERT' AND round_state IN ({terminal}) THEN\n"
        + _raise(
            "'轮 ' || NEW.round_id::text || ' 已是终态 ' || round_state"
            " || '，不得再加名额'",
            CONSISTENCY_SQLSTATE,
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {SLOT_CONSISTENCY_TRIGGER}\n"
        "AFTER INSERT OR UPDATE ON trial_round_slots\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {SLOT_CONSISTENCY_FUNCTION}()"
    )
    return (
        function_ddl,
        trigger_ddl,
        f"DROP TRIGGER IF EXISTS {SLOT_CONSISTENCY_TRIGGER} ON trial_round_slots",
        f"DROP FUNCTION IF EXISTS {SLOT_CONSISTENCY_FUNCTION}()",
    )


def decision_consistency_ddl() -> tuple[str, str, str, str]:
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {DECISION_CONSISTENCY_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    round_state text;\n"
        "BEGIN\n"
        "    SELECT state INTO round_state FROM trial_rounds WHERE id = NEW.round_id;\n"
        f"    IF NOT FOUND OR round_state IS DISTINCT FROM '{ROUND_STATE_GATE_PASSED}' THEN\n"
        + _raise(
            "'关口决定 ' || NEW.id::text || ' 所属的轮不是 GATE_PASSED（决定存在而状态未推进）'",
            CONSISTENCY_SQLSTATE,
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {DECISION_CONSISTENCY_TRIGGER}\n"
        "AFTER INSERT ON trial_gate_decisions\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        "FOR EACH ROW\n"
        f"EXECUTE FUNCTION {DECISION_CONSISTENCY_FUNCTION}()"
    )
    return (
        function_ddl,
        trigger_ddl,
        f"DROP TRIGGER IF EXISTS {DECISION_CONSISTENCY_TRIGGER} ON trial_gate_decisions",
        f"DROP FUNCTION IF EXISTS {DECISION_CONSISTENCY_FUNCTION}()",
    )


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块**自己的**业务触发器（downgrade 只删这些，不碰共用函数，ISS-79）。"""
    items: list[tuple[str, str, str, str, str]] = []
    for table, builder in (
        ("trial_rounds", round_transition_guard_ddl),
        ("trial_rounds", round_consistency_ddl),
        ("trial_round_slots", slot_guard_ddl),
        ("trial_round_slots", slot_consistency_ddl),
        ("trial_gate_decisions", decision_consistency_ddl),
    ):
        function_ddl, trigger_ddl, drop_trigger, drop_function = builder()
        items.append((table, function_ddl, trigger_ddl, drop_trigger, drop_function))
    return items


def all_trigger_ddl() -> list[tuple[str, str, str, str, str]]:
    """六个触发器：`(表名, 函数DDL, 触发器DDL, 触发器DROP, 函数DROP)`。

    顺序：不可变保护在前，业务守卫与一致性在后（与 review/ddl 同形）。
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
    return (
        *(immutable_trigger_name(t) for t in IMMUTABLE_TABLES),
        TRANSITION_TRIGGER,
        ROUND_CONSISTENCY_TRIGGER,
        SLOT_GUARD_TRIGGER,
        SLOT_CONSISTENCY_TRIGGER,
        DECISION_CONSISTENCY_TRIGGER,
    )


def consistency_function_names() -> tuple[str, ...]:
    """downgrade 只删这些；**不含** `rednote_reject_mutation`（跨包共用）。"""
    return OWN_FUNCTIONS


__all__ = [
    "CONSISTENCY_SQLSTATE",
    "DECISION_CONSISTENCY_TRIGGER",
    "MATRIX_BEGIN",
    "MATRIX_END",
    "OWN_FUNCTIONS",
    "ROUND_CONSISTENCY_TRIGGER",
    "SLOT_CONSISTENCY_TRIGGER",
    "SLOT_GUARD_TRIGGER",
    "TRANSITION_FUNCTION",
    "TRANSITION_TRIGGER",
    "all_trigger_ddl",
    "consistency_function_names",
    "consistency_items",
    "trigger_names",
]
