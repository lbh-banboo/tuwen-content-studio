"""WP33 的触发器 DDL：6 个不可变触发器 + 7 个延迟一致性约束触发器（本包自有函数 7 个）。

## 7 个一致性触发器在守什么（FK / CHECK 管不住的那部分）

| 触发器（表） | 坏数据 | 为什么 FK / CHECK 挡不住 |
|---|---|---|
| `trg_quality_reports_current_version`（`quality_reports`） | 投影指向别的系列的版本 / 修订号漂移 | FK 只保证版本存在，不管它属于谁 |
| `trg_quality_report_versions_consistency`（`quality_report_versions`） | 注册表节点对不上；汇总计数与明细分布不符 | 跨表聚合，CHECK 表达不了 |
| `trg_version_nodes_report_paired`（`version_nodes`） | 注册表里出现没有领域行的 REPORT 节点（OP-88 反向） | 反向配对 |
| `trg_quality_report_items_consistency`（`quality_report_items`） | 钉死的版本不属于该篇；该篇不属于报告所属项目 | FK 只保证行存在 |
| `trg_quality_dimension_results_flagged`（`quality_dimension_results`） | 「被标记 N 篇」与相似对行互相矛盾 | 跨表聚合 |
| `trg_quality_similarity_pairs_consistency`（`quality_similarity_pairs`） | 挂在 NOT_COMPUTED 维度上 / 低于阈值 / 涉及被排除的篇目 | 「没算」与「没命中」会同形 |
| `trg_quality_media_alignments_consistency`（`quality_media_alignments`） | 媒体位不属于该篇；NO_VERSION 篇有对位行；NOT_EVALUABLE 与 INCLUDED 状态矛盾 | 跨表 |

全部 `CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED`：同一事务里先建版本、后建子表行，逐行即时检查会误报。
**代价要如实登记**：错误在 `COMMIT`（或显式 `SET CONSTRAINTS ALL IMMEDIATE`）那一刻才冒出来。

## downgrade 的删除范围

`OWN_FUNCTIONS` **只列本包自己的 7 个函数**，**不含** `rednote_reject_mutation`（跨包共用，ISS-79）。
RAISE 用字符串拼接，不用 `%` 占位符（psycopg 会把 `%` 当占位符）。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.quality.models import IMMUTABLE_TABLES
from rednote.platform.immutability import immutable_trigger_ddl, immutable_trigger_name

CONSISTENCY_SQLSTATE: Final[str] = "23514"

CURRENT_VERSION_FUNCTION: Final[str] = "rednote_check_quality_report_current_version"
CURRENT_VERSION_TRIGGER: Final[str] = "trg_quality_reports_current_version"
VERSION_FUNCTION: Final[str] = "rednote_check_quality_report_version_consistency"
VERSION_TRIGGER: Final[str] = "trg_quality_report_versions_consistency"
NODE_FUNCTION: Final[str] = "rednote_check_quality_report_node_paired"
NODE_TRIGGER: Final[str] = "trg_version_nodes_report_paired"
ITEM_FUNCTION: Final[str] = "rednote_check_quality_report_item"
ITEM_TRIGGER: Final[str] = "trg_quality_report_items_consistency"
DIMENSION_FUNCTION: Final[str] = "rednote_check_quality_dimension_flagged"
DIMENSION_TRIGGER: Final[str] = "trg_quality_dimension_results_flagged"
PAIR_FUNCTION: Final[str] = "rednote_check_quality_pair"
PAIR_TRIGGER: Final[str] = "trg_quality_similarity_pairs_consistency"
ALIGNMENT_FUNCTION: Final[str] = "rednote_check_quality_alignment"
ALIGNMENT_TRIGGER: Final[str] = "trg_quality_media_alignments_consistency"

#: 本包**自己的**函数（downgrade 只删这 7 个）。
OWN_FUNCTIONS: Final[tuple[str, ...]] = (
    CURRENT_VERSION_FUNCTION,
    VERSION_FUNCTION,
    NODE_FUNCTION,
    ITEM_FUNCTION,
    DIMENSION_FUNCTION,
    PAIR_FUNCTION,
    ALIGNMENT_FUNCTION,
)


def _raise(message_expr: str, indent: str = "        ") -> str:
    return (
        f"{indent}RAISE EXCEPTION USING\n"
        f"{indent}    MESSAGE = {message_expr},\n"
        f"{indent}    ERRCODE = '{CONSISTENCY_SQLSTATE}';\n"
    )


def _function(name: str, declare: str, body: str) -> str:
    return (
        f"CREATE OR REPLACE FUNCTION {name}() RETURNS trigger AS $$\n"
        f"{declare}"
        "BEGIN\n"
        f"{body}"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )


def _constraint_trigger(
    trigger: str, function: str, table: str, event: str, when: str = ""
) -> str:
    return (
        f"CREATE CONSTRAINT TRIGGER {trigger}\n"
        f"AFTER {event} ON {table}\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW{when}\n"
        f"EXECUTE FUNCTION {function}()"
    )


def _quad(
    function: str, trigger: str, table: str, function_ddl: str, trigger_ddl: str
) -> tuple[str, str, str, str]:
    return (
        function_ddl,
        trigger_ddl,
        f"DROP TRIGGER IF EXISTS {trigger} ON {table}",
        f"DROP FUNCTION IF EXISTS {function}()",
    )


def current_version_ddl() -> tuple[str, str, str, str]:
    body = (
        "    IF NEW.current_version_id IS NULL THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "    SELECT report_id, revision_no INTO v_report, v_revision\n"
        "      FROM quality_report_versions WHERE id = NEW.current_version_id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise("'质量报告 ' || NEW.id::text || ' 的当前版本 ' || NEW.current_version_id::text || ' 不存在'", "        ")
        + "    END IF;\n"
        "    IF v_report IS DISTINCT FROM NEW.id THEN\n"
        + _raise(
            "'质量报告 ' || NEW.id::text || ' 的当前版本属于别的报告 ' || COALESCE(v_report::text, '(未知)')",
            "        ",
        )
        + "    END IF;\n"
        "    IF v_revision IS DISTINCT FROM NEW.current_revision_no THEN\n"
        + _raise(
            "'质量报告 ' || NEW.id::text || ' 的 current_revision_no=' || NEW.current_revision_no::text"
            " || '，而当前版本的 revision_no=' || v_revision::text",
            "        ",
        )
        + "    END IF;\n"
    )
    fn = _function(
        CURRENT_VERSION_FUNCTION,
        "DECLARE\n    v_report uuid;\n    v_revision integer;\n",
        body,
    )
    trg = _constraint_trigger(
        CURRENT_VERSION_TRIGGER, CURRENT_VERSION_FUNCTION, "quality_reports", "INSERT OR UPDATE"
    )
    return _quad(CURRENT_VERSION_FUNCTION, CURRENT_VERSION_TRIGGER, "quality_reports", fn, trg)


def version_consistency_ddl() -> tuple[str, str, str, str]:
    body = (
        "    SELECT entity_type, entity_id, revision_no, payload_hash\n"
        "      INTO n_type, n_entity, n_revision, n_hash\n"
        "      FROM version_nodes WHERE id = NEW.id;\n"
        "    IF NOT FOUND THEN\n"
        + _raise("'quality_report_versions.id 没有对应的 version_nodes 行：' || NEW.id::text")
        + "    END IF;\n"
        "    IF n_type <> 'REPORT' THEN\n"
        + _raise("'版本节点 ' || NEW.id::text || ' 的 entity_type 是 ' || n_type || '，不是 REPORT'")
        + "    END IF;\n"
        "    IF n_entity IS DISTINCT FROM NEW.report_id THEN\n"
        + _raise("'版本节点 ' || NEW.id::text || ' 的 entity_id 与 quality_report_versions.report_id 不一致'")
        + "    END IF;\n"
        "    IF n_revision IS DISTINCT FROM NEW.revision_no THEN\n"
        + _raise("'版本节点 ' || NEW.id::text || ' 的 revision_no 与 quality_report_versions 不一致'")
        + "    END IF;\n"
        "    IF n_hash IS DISTINCT FROM NEW.payload_hash THEN\n"
        + _raise("'版本节点 ' || NEW.id::text || ' 的 payload_hash 与 quality_report_versions 不一致'")
        + "    END IF;\n"
        # 聚合计数核对（提交时子表行已就位）
        "    SELECT count(*),\n"
        "           count(*) FILTER (WHERE verdict = 'OK'),\n"
        "           count(*) FILTER (WHERE verdict = 'ALERT'),\n"
        "           count(*) FILTER (WHERE status = 'NOT_COMPUTED')\n"
        "      INTO d_total, d_ok, d_alert, d_nc\n"
        "      FROM quality_dimension_results WHERE report_version_id = NEW.id;\n"
        "    IF d_total <> 7 THEN\n"
        + _raise("'报告版本 ' || NEW.id::text || ' 的维度行数是 ' || d_total::text || '，必须恰为 7'")
        + "    END IF;\n"
        "    IF d_ok <> NEW.ok_dimension_count OR d_alert <> NEW.alert_dimension_count\n"
        "       OR d_nc <> NEW.not_computed_dimension_count THEN\n"
        + _raise(
            "'报告版本 ' || NEW.id::text || ' 的维度汇总计数与 7 个维度行的实际分布不符'"
        )
        + "    END IF;\n"
        "    SELECT count(*) FILTER (WHERE status = 'INCLUDED'),\n"
        "           count(*) FILTER (WHERE status = 'EXCLUDED')\n"
        "      INTO i_inc, i_exc\n"
        "      FROM quality_report_items WHERE report_version_id = NEW.id;\n"
        "    IF i_inc <> NEW.included_item_count OR i_exc <> NEW.excluded_item_count THEN\n"
        + _raise("'报告版本 ' || NEW.id::text || ' 的纳入 / 排除篇数与 items 行的实际分布不符'")
        + "    END IF;\n"
    )
    fn = _function(
        VERSION_FUNCTION,
        "DECLARE\n"
        "    n_type text;\n    n_entity uuid;\n    n_revision integer;\n    n_hash text;\n"
        "    d_total integer;\n    d_ok integer;\n    d_alert integer;\n    d_nc integer;\n"
        "    i_inc integer;\n    i_exc integer;\n",
        body,
    )
    trg = _constraint_trigger(
        VERSION_TRIGGER, VERSION_FUNCTION, "quality_report_versions", "INSERT"
    )
    return _quad(VERSION_FUNCTION, VERSION_TRIGGER, "quality_report_versions", fn, trg)


def node_paired_ddl() -> tuple[str, str, str, str]:
    """OP-88 反向判据。**两道判断顺序不可换**：先判归不归我管（`quality_reports` 里有该 id），再判配对。"""
    body = (
        "    IF NOT EXISTS (SELECT 1 FROM quality_reports WHERE id = NEW.entity_id) THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "    IF NOT EXISTS (SELECT 1 FROM quality_report_versions WHERE id = NEW.id) THEN\n"
        + _raise(
            "'版本节点 ' || NEW.id::text || ' 声明属于质量报告 ' || NEW.entity_id::text"
            " || '，但没有任何 quality_report_versions 行（孤立节点）'"
        )
        + "    END IF;\n"
    )
    fn = _function(NODE_FUNCTION, "", body)
    trg = _constraint_trigger(
        NODE_TRIGGER,
        NODE_FUNCTION,
        "version_nodes",
        "INSERT",
        " WHEN (NEW.entity_type = 'REPORT')",
    )
    return _quad(NODE_FUNCTION, NODE_TRIGGER, "version_nodes", fn, trg)


def item_ddl() -> tuple[str, str, str, str]:
    body = (
        "    IF NEW.content_version_id IS NOT NULL THEN\n"
        "        SELECT content_item_id INTO v_owner\n"
        "          FROM content_versions WHERE id = NEW.content_version_id;\n"
        "        IF v_owner IS DISTINCT FROM NEW.content_item_id THEN\n"
        + _raise(
            "'报告成员 ' || NEW.id::text || ' 钉死的内容版本不属于内容 ' || NEW.content_item_id::text",
            "            ",
        )
        + "        END IF;\n"
        "    END IF;\n"
        "    SELECT project_id INTO c_project FROM content_items WHERE id = NEW.content_item_id;\n"
        "    SELECT qr.project_id INTO r_project\n"
        "      FROM quality_report_versions v JOIN quality_reports qr ON qr.id = v.report_id\n"
        "     WHERE v.id = NEW.report_version_id;\n"
        "    IF c_project IS DISTINCT FROM r_project THEN\n"
        + _raise(
            "'报告成员 ' || NEW.id::text || ' 的内容属于项目 ' || COALESCE(c_project::text, '(未知)')"
            " || '，而报告属于项目 ' || COALESCE(r_project::text, '(未知)')"
        )
        + "    END IF;\n"
    )
    fn = _function(
        ITEM_FUNCTION,
        "DECLARE\n    v_owner uuid;\n    c_project uuid;\n    r_project uuid;\n",
        body,
    )
    trg = _constraint_trigger(ITEM_TRIGGER, ITEM_FUNCTION, "quality_report_items", "INSERT")
    return _quad(ITEM_FUNCTION, ITEM_TRIGGER, "quality_report_items", fn, trg)


def dimension_ddl() -> tuple[str, str, str, str]:
    body = (
        "    IF NEW.status <> 'COMPUTED' THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "    SELECT count(DISTINCT x) INTO actual FROM (\n"
        "        SELECT content_item_a_id AS x FROM quality_similarity_pairs\n"
        "         WHERE report_version_id = NEW.report_version_id AND dimension = NEW.dimension\n"
        "        UNION\n"
        "        SELECT content_item_b_id FROM quality_similarity_pairs\n"
        "         WHERE report_version_id = NEW.report_version_id AND dimension = NEW.dimension\n"
        "    ) s;\n"
        "    IF actual <> NEW.flagged_item_count THEN\n"
        + _raise(
            "'维度 ' || NEW.dimension || ' 的 flagged_item_count=' || NEW.flagged_item_count::text"
            " || '，而相似对涉及的去重篇数=' || actual::text"
        )
        + "    END IF;\n"
        "    IF NEW.flagged_item_count > 0\n"
        "       AND NEW.max_similarity_permille < NEW.pair_threshold_permille THEN\n"
        + _raise(
            "'维度 ' || NEW.dimension || ' 有被标记的篇目，但最大相似度低于单对阈值'"
        )
        + "    END IF;\n"
    )
    fn = _function(DIMENSION_FUNCTION, "DECLARE\n    actual integer;\n", body)
    trg = _constraint_trigger(
        DIMENSION_TRIGGER, DIMENSION_FUNCTION, "quality_dimension_results", "INSERT"
    )
    return _quad(DIMENSION_FUNCTION, DIMENSION_TRIGGER, "quality_dimension_results", fn, trg)


def pair_ddl() -> tuple[str, str, str, str]:
    body = (
        "    SELECT status, pair_threshold_permille INTO d_status, d_threshold\n"
        "      FROM quality_dimension_results\n"
        "     WHERE report_version_id = NEW.report_version_id AND dimension = NEW.dimension;\n"
        "    IF NOT FOUND OR d_status <> 'COMPUTED' THEN\n"
        + _raise(
            "'相似对 ' || NEW.id::text || ' 所在维度 ' || NEW.dimension"
            " || ' 不是 COMPUTED（NOT_COMPUTED 维度不得有相似对）'"
        )
        + "    END IF;\n"
        "    IF NEW.intersection_count * 1000 < d_threshold * NEW.union_count THEN\n"
        + _raise("'相似对 ' || NEW.id::text || ' 的相似度低于该维度的单对阈值（只存命中）'")
        + "    END IF;\n"
        "    IF (SELECT count(*) FROM quality_report_items\n"
        "         WHERE report_version_id = NEW.report_version_id\n"
        "           AND content_item_id IN (NEW.content_item_a_id, NEW.content_item_b_id)\n"
        "           AND status = 'INCLUDED') <> 2 THEN\n"
        + _raise("'相似对 ' || NEW.id::text || ' 涉及的两篇必须都是 INCLUDED 的报告成员'")
        + "    END IF;\n"
    )
    fn = _function(
        PAIR_FUNCTION, "DECLARE\n    d_status text;\n    d_threshold integer;\n", body
    )
    trg = _constraint_trigger(PAIR_TRIGGER, PAIR_FUNCTION, "quality_similarity_pairs", "INSERT")
    return _quad(PAIR_FUNCTION, PAIR_TRIGGER, "quality_similarity_pairs", fn, trg)


def alignment_ddl() -> tuple[str, str, str, str]:
    body = (
        "    IF NEW.media_item_id IS NOT NULL THEN\n"
        "        SELECT content_item_id INTO m_owner FROM media_items WHERE id = NEW.media_item_id;\n"
        "        IF m_owner IS DISTINCT FROM NEW.content_item_id THEN\n"
        + _raise(
            "'对位行 ' || NEW.id::text || ' 的媒体位不属于内容 ' || NEW.content_item_id::text",
            "            ",
        )
        + "        END IF;\n"
        "    END IF;\n"
        "    SELECT status, content_version_id INTO i_status, i_version\n"
        "      FROM quality_report_items\n"
        "     WHERE report_version_id = NEW.report_version_id AND content_item_id = NEW.content_item_id;\n"
        "    IF NOT FOUND OR i_version IS NULL THEN\n"
        + _raise("'对位行 ' || NEW.id::text || ' 所属成员没有内容版本（NO_VERSION 篇目不应有对位行）'")
        + "    END IF;\n"
        "    IF (i_status = 'INCLUDED') <> (NEW.verdict <> 'NOT_EVALUABLE') THEN\n"
        + _raise(
            "'对位行 ' || NEW.id::text || ' 的 verdict 与成员状态矛盾"
            "（NOT_EVALUABLE 只出现在被排除的篇目上）'"
        )
        + "    END IF;\n"
    )
    fn = _function(
        ALIGNMENT_FUNCTION,
        "DECLARE\n    m_owner uuid;\n    i_status text;\n    i_version uuid;\n",
        body,
    )
    trg = _constraint_trigger(
        ALIGNMENT_TRIGGER, ALIGNMENT_FUNCTION, "quality_media_alignments", "INSERT"
    )
    return _quad(ALIGNMENT_FUNCTION, ALIGNMENT_TRIGGER, "quality_media_alignments", fn, trg)


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块**自己的**一致性触发器五元组 `(表, 函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)`。"""
    items: list[tuple[str, str, str, str, str]] = []
    for table, builder in (
        ("quality_reports", current_version_ddl),
        ("quality_report_versions", version_consistency_ddl),
        ("version_nodes", node_paired_ddl),
        ("quality_report_items", item_ddl),
        ("quality_dimension_results", dimension_ddl),
        ("quality_similarity_pairs", pair_ddl),
        ("quality_media_alignments", alignment_ddl),
    ):
        function_ddl, trigger_ddl, drop_trigger, drop_function = builder()
        items.append((table, function_ddl, trigger_ddl, drop_trigger, drop_function))
    return items


def all_trigger_ddl() -> list[tuple[str, str, str, str, str]]:
    """全部触发器：不可变保护在前，业务不变量在后。"""
    items: list[tuple[str, str, str, str, str]] = []
    for table in IMMUTABLE_TABLES:
        function_ddl, trigger_ddl, drop_trigger, drop_function = immutable_trigger_ddl(table)
        items.append((table, function_ddl, trigger_ddl, drop_trigger, drop_function))
    items.extend(consistency_items())
    return items


def immutable_trigger_names() -> tuple[str, ...]:
    return tuple(immutable_trigger_name(t) for t in IMMUTABLE_TABLES)


def consistency_trigger_names() -> tuple[str, ...]:
    return (
        CURRENT_VERSION_TRIGGER,
        VERSION_TRIGGER,
        NODE_TRIGGER,
        ITEM_TRIGGER,
        DIMENSION_TRIGGER,
        PAIR_TRIGGER,
        ALIGNMENT_TRIGGER,
    )


def trigger_names() -> tuple[str, ...]:
    return immutable_trigger_names() + consistency_trigger_names()


def consistency_function_names() -> tuple[str, ...]:
    """downgrade 只删这些；**不含** `rednote_reject_mutation`（跨包共用）。"""
    return OWN_FUNCTIONS


def install_statements() -> list[str]:
    """建触发器的语句序列（DROP 触发器 → CREATE OR REPLACE 函数 → CREATE 触发器；`CREATE TRIGGER` 不幂等）。"""
    statements: list[str] = []
    for _table, func_ddl, trigger_ddl, drop_trigger, _drop_func in all_trigger_ddl():
        statements.append(drop_trigger)
        statements.append(func_ddl)
        statements.append(trigger_ddl)
    return statements


__all__ = [
    "ALIGNMENT_TRIGGER",
    "CONSISTENCY_SQLSTATE",
    "CURRENT_VERSION_TRIGGER",
    "DIMENSION_TRIGGER",
    "ITEM_TRIGGER",
    "NODE_TRIGGER",
    "OWN_FUNCTIONS",
    "PAIR_TRIGGER",
    "VERSION_TRIGGER",
    "all_trigger_ddl",
    "consistency_function_names",
    "consistency_items",
    "consistency_trigger_names",
    "immutable_trigger_names",
    "install_statements",
    "trigger_names",
]
