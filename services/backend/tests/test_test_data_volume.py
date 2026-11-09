"""`check_test_data_volume.py` 自身的判据（ISS-223 / WP-DEBT-05）。

## 为什么判据也要有判据

按判据纪律第 9 条（ISS-95）：「声称自己有防线」比「没有防线」更危险。
本文件盯判据 D 最容易被悄悄削弱的几处：

1. **阈值必须由实测值 × 安全倍数机械派生**，不能是拍脑袋的数字（铁律 14）。
2. **严格大于才算超阈值**——等于阈值必须放行（测量基准本身就该是绿的）。
3. **缺表/连不上是独立的"问题"，不能折算成 0 行**（ISS-143 同族：
   "没读到"与"读到 0"在数值上同形，必须在语义上分开，否则库被删/连错时
   判据会安静地印 `[PASS]`）。
4. **DSN 必须可被环境变量覆盖**（并发开发线各自隔离）。
5. **计数 SQL 必须纯只读**。
6. **三处 DSN 常量不能漂移**：本判据、`check_test_residue.py`、
   `services/backend/tests/conftest.py` 各自持一份默认 DSN 常量
   （刻意不互相 import，见判据 D 模块 docstring），用对账代替耦合。

## 反向验证

`tools/quality/collect_test_data_volume_reverse_evidence.py`——与本文件互为两遍：
本文件验"函数被直接调用时判得对"，反向验证验"在真实仓库副本上端到端跑得对"。
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL = REPO_ROOT / "tools" / "quality" / "check_test_data_volume.py"

sys.path.insert(0, str(TOOL.parent))

import check_test_data_volume as tool  # noqa: E402


# ===========================================================================
# 判据 1：阈值必须由实测值机械派生（铁律 14：不许拍脑袋的数字）
# ===========================================================================


@pytest.mark.regression
def test_thresholds_are_derived_from_measured_baseline() -> None:
    """`MAX_ROWS` 必须等于 `_round_up(MEASURED_ROWS_PER_RUN[t] * SAFETY_FACTOR, 1000)`。

    这条防的是"常量漂移"：如果有人把 `MAX_ROWS` 改成一个随手写的大数字，
    这条用例当场报红——阈值永远必须能从"实测单次运行量"推导回去。
    """
    for table in tool.ROOT_TABLES:
        expected = tool._round_up(
            tool.MEASURED_ROWS_PER_RUN[table] * tool.SAFETY_FACTOR, 1000
        )
        assert tool.MAX_ROWS[table] == expected, (
            f"{table} 的阈值 {tool.MAX_ROWS[table]} 不等于"
            f" measured({tool.MEASURED_ROWS_PER_RUN[table]}) × factor({tool.SAFETY_FACTOR})"
            f" 向上取整 = {expected}——阈值已脱离实测依据"
        )


@pytest.mark.regression
def test_safety_factor_is_within_a_sane_range() -> None:
    """倍数必须在 [2, 8] 之间：太小会在正常跑两次测试后就误报；太大等于没设阈值。"""
    assert 2 <= tool.SAFETY_FACTOR <= 8, (
        f"SAFETY_FACTOR={tool.SAFETY_FACTOR} 超出 [2, 8] 的合理范围"
    )


@pytest.mark.regression
def test_measured_rows_per_run_covers_every_root_table() -> None:
    """`MEASURED_ROWS_PER_RUN` 必须覆盖 `ROOT_TABLES` 里的每一张表，且都是正数。"""
    for table in tool.ROOT_TABLES:
        assert table in tool.MEASURED_ROWS_PER_RUN, f"{table} 没有实测基线"
        assert tool.MEASURED_ROWS_PER_RUN[table] > 0, (
            f"{table} 的实测基线是 {tool.MEASURED_ROWS_PER_RUN[table]}——"
            "占位值未回填，还是真的量出来是 0？两者都不该直接上线"
        )


@pytest.mark.regression
@pytest.mark.parametrize(
    ("value", "step", "expected"),
    [
        (1, 1000, 1000),
        (1000, 1000, 1000),
        (1001, 1000, 2000),
        (6352, 1000, 7000),
        (0, 1000, 1000),
    ],
)
def test_round_up(value: int, step: int, expected: int) -> None:
    assert tool._round_up(value, step) == expected


# ===========================================================================
# 判据 2：严格大于才算超阈值；缺表/连不上是独立的"问题"（ISS-143 同族）
# ===========================================================================


@pytest.mark.regression
def test_reading_at_threshold_is_not_a_violation() -> None:
    """等于阈值必须放行——测量基准本身跑出来就该是绿的。"""
    readings = [tool.TableReading(table="projects", rows=tool.MAX_ROWS["projects"])]
    violations, problems = tool.evaluate(readings, tool.MAX_ROWS)
    assert not violations
    assert not problems


@pytest.mark.regression
def test_one_over_threshold_is_a_violation() -> None:
    readings = [
        tool.TableReading(table="projects", rows=tool.MAX_ROWS["projects"] + 1)
    ]
    violations, problems = tool.evaluate(readings, tool.MAX_ROWS)
    assert not problems
    assert len(violations) == 1
    assert violations[0].table == "projects"
    assert violations[0].rows == tool.MAX_ROWS["projects"] + 1


@pytest.mark.regression
def test_multiple_tables_over_limit_are_all_named() -> None:
    """三张表都超了，三张表都必须出现在结果里——不能只报第一个就停。"""
    readings = [
        tool.TableReading(table=t, rows=tool.MAX_ROWS[t] + 1) for t in tool.ROOT_TABLES
    ]
    violations, problems = tool.evaluate(readings, tool.MAX_ROWS)
    assert not problems
    assert {v.table for v in violations} == set(tool.ROOT_TABLES)


@pytest.mark.regression
def test_all_under_limit_yields_no_violation() -> None:
    readings = [tool.TableReading(table=t, rows=1) for t in tool.ROOT_TABLES]
    violations, problems = tool.evaluate(readings, tool.MAX_ROWS)
    assert not violations
    assert not problems


@pytest.mark.regression
def test_missing_table_is_problem_not_zero() -> None:
    """表不存在时必须走"问题"分支，绝不能悄悄当成 0 行放行（ISS-143 同族）。"""
    readings = [
        tool.TableReading(table="projects", rows=None, problem="表不存在")
    ]
    violations, problems = tool.evaluate(readings, tool.MAX_ROWS)
    assert not violations, "缺表不该被判成'未超阈值'——那等于把 None 当 0 用了"
    assert len(problems) == 1
    assert "projects" in problems[0]


@pytest.mark.regression
def test_unreadable_count_is_problem() -> None:
    readings = [
        tool.TableReading(table="users", rows=None, problem="连不上目标库")
    ]
    violations, problems = tool.evaluate(readings, tool.MAX_ROWS)
    assert not violations
    assert len(problems) == 1


@pytest.mark.regression
def test_read_counts_reports_connection_failure_as_problem_not_crash() -> None:
    """连不上库时 `read_counts` 必须优雅返回带 `problem` 的读数，不能抛异常让调用方崩溃。"""
    readings = tool.read_counts(
        "postgresql+psycopg://rednote@localhost:1/rednote_unreachable_xyz",
        tables=("projects",),
    )
    assert len(readings) == 1
    assert readings[0].rows is None
    assert readings[0].problem is not None


# ===========================================================================
# 判据 3：失败文案必须指名表/实际行数/阈值/重置工具（可操作，不是裸红）
# ===========================================================================


@pytest.mark.regression
def test_failure_text_names_table_actual_limit_and_reset_tool() -> None:
    violations = [
        tool.Violation(table="projects", rows=tool.MAX_ROWS["projects"] + 500, limit=tool.MAX_ROWS["projects"])
    ]
    report = tool.format_report(
        [tool.TableReading(table="projects", rows=violations[0].rows)],
        violations,
        [],
        dsn=os.environ.get("REDNOTE_TEST_DATABASE_URL", "postgresql+psycopg://rednote@localhost:5432/rednote_test"),
    )
    assert "projects" in report
    assert str(violations[0].rows) in report.replace(",", "")
    assert str(tool.MAX_ROWS["projects"]) in report.replace(",", "")
    assert "reset_shared_test_db.py" in report


@pytest.mark.regression
def test_pass_report_has_no_fail_marker() -> None:
    """绿要有形状（ISS-149 的镜像）：PASS 的报告里不能同时出现 `[FAIL]`。"""
    readings = [tool.TableReading(table=t, rows=1) for t in tool.ROOT_TABLES]
    report = tool.format_report(readings, [], [], dsn="x")
    assert "[PASS]" in report
    assert "[FAIL]" not in report


# ===========================================================================
# 判据 4：main() 的退出码必须与判定结果一致（monkeypatch，不连真库）
# ===========================================================================


@pytest.mark.regression
def test_main_exits_zero_on_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tool,
        "read_counts",
        lambda dsn, tables=tool.ROOT_TABLES: [
            tool.TableReading(table=t, rows=1) for t in tables
        ],
    )
    assert tool.main([]) == 0


@pytest.mark.regression
def test_main_exits_nonzero_on_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tool,
        "read_counts",
        lambda dsn, tables=tool.ROOT_TABLES: [
            tool.TableReading(table=t, rows=tool.MAX_ROWS[t] + 1) for t in tables
        ],
    )
    assert tool.main([]) == 1


@pytest.mark.regression
def test_main_exits_nonzero_on_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tool,
        "read_counts",
        lambda dsn, tables=tool.ROOT_TABLES: [
            tool.TableReading(table=t, rows=None, problem="连不上") for t in tables
        ],
    )
    assert tool.main([]) == 1, "连不上库必须 FAIL，不能 skip 式地返回 0"


# ===========================================================================
# 判据 5：DSN 口径——env 覆盖优先，默认值不是保留库，且与其它两处不漂移
# ===========================================================================


@pytest.mark.regression
def test_env_override_is_respected_for_the_target_database() -> None:
    saved = os.environ.get(tool.TEST_DATABASE_URL_ENV)
    try:
        os.environ[tool.TEST_DATABASE_URL_ENV] = (
            "postgresql+psycopg://rednote@h:5432/rednote_test_b"
        )
        assert tool._test_dsn().endswith("/rednote_test_b"), (
            "环境变量没能覆盖默认目标库——并发开发线无法各自隔离（§4.2-D 同族）"
        )
    finally:
        if saved is None:
            os.environ.pop(tool.TEST_DATABASE_URL_ENV, None)
        else:
            os.environ[tool.TEST_DATABASE_URL_ENV] = saved


@pytest.mark.regression
def test_default_dsn_is_not_a_reserved_database() -> None:
    """判据 D 自己的默认目标库不得是 `postgres`/`template*`（复用判据 A 的解析，不自证）。"""
    try:
        sys.path.insert(0, str(REPO_ROOT / "tools" / "quality"))
        import check_test_database_target as target_tool
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"无法 import check_test_database_target（{type(exc).__name__}）")

    db = target_tool.database_name(tool.DEFAULT_TEST_DSN)
    assert db is not None, f"判据自带的默认 DSN 解析不出库名：{tool.DEFAULT_TEST_DSN}"
    assert db not in target_tool.STATIC_RESERVED_DATABASES, (
        f"判据 D 的默认目标库是保留库 {db!r}"
    )


@pytest.mark.regression
def test_default_dsn_matches_conftest_and_residue_judge() -> None:
    """三处 DSN 常量（判据 D / 判据 B / conftest）刻意不互相 import，靠这条对账防漂移。"""
    sys.path.insert(0, str(REPO_ROOT / "tools" / "quality"))
    import check_test_residue as residue_tool

    conftest_path = REPO_ROOT / "services" / "backend" / "tests" / "conftest.py"
    conftest_src = conftest_path.read_text(encoding="utf-8")
    conftest_default = _extract_default_test_dsn(conftest_src)

    assert tool.DEFAULT_TEST_DSN == residue_tool.DEFAULT_TEST_DSN == conftest_default, (
        "三处默认测试 DSN 常量已经漂移：\n"
        f"  check_test_data_volume.DEFAULT_TEST_DSN = {tool.DEFAULT_TEST_DSN!r}\n"
        f"  check_test_residue.DEFAULT_TEST_DSN      = {residue_tool.DEFAULT_TEST_DSN!r}\n"
        f"  conftest.DEFAULT_TEST_DATABASE_URL        = {conftest_default!r}"
    )


def _extract_default_test_dsn(source: str) -> str | None:
    """从 `conftest.py` 源码里取 `DEFAULT_TEST_DATABASE_URL` 的字面量值（AST，不猜格式）。"""
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "DEFAULT_TEST_DATABASE_URL":
                if isinstance(node.value, ast.Constant):
                    return node.value.value
    return None


# ===========================================================================
# 判据 6：只测三张无主根表——不测闭包全表是刻意的简化，不是遗漏
# ===========================================================================


@pytest.mark.regression
def test_root_tables_are_exactly_the_three_unowned_roots() -> None:
    assert tool.ROOT_TABLES == ("projects", "users", "brands")


# ===========================================================================
# 判据 7：计数 SQL 必须纯只读（AST，不许出现写操作关键字）
# ===========================================================================


@pytest.mark.regression
def test_read_counts_contains_no_write_statements() -> None:
    """`read_counts` 函数体内不得出现 insert/update/delete/truncate/drop/alter 字样。"""
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    fn = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "read_counts"),
        None,
    )
    assert fn is not None, "找不到 read_counts —— 判据的结构变了"
    src = ast.get_source_segment(TOOL.read_text(encoding="utf-8"), fn) or ""
    forbidden = ("insert ", "update ", "delete ", "truncate", "drop ", "alter ")
    lowered = src.lower()
    for word in forbidden:
        assert word not in lowered, f"read_counts 里出现了写操作关键字 {word!r}——本判据必须纯只读"
