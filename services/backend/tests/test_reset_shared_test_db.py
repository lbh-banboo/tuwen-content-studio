"""`tools/dev/reset_shared_test_db.py` 自身的判据（WP-DEBT-05）。

本文件只测**不需要真连一次会执行 TRUNCATE 的真库**的部分（安全门的纯逻辑、
DSN 口径、与判据 D 的常量对账）。"真的清空一个库、复核行数归零"这条端到端路径
已经在 WP-DEBT-05 的止血步骤里人工验证过（私有库 `rednote_wpdebt05_measure1`：
61 张闭包表、清空前 1588/2303/1601 行、清空后 0/0/0），不在这里重复起一个临时库跑。
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL = REPO_ROOT / "tools" / "dev" / "reset_shared_test_db.py"

sys.path.insert(0, str(TOOL.parent))

import reset_shared_test_db as tool  # noqa: E402


# ===========================================================================
# 判据 1：安全门——保留库拒绝 / confirm 必须原样匹配
# ===========================================================================


@pytest.mark.regression
def test_guard_reserved_rejects_postgres() -> None:
    with pytest.raises(SystemExit):
        tool._guard_reserved("postgres")


@pytest.mark.regression
def test_guard_reserved_rejects_none() -> None:
    """DSN 解析不出库名时必须拒绝，不能当成"随便哪个库"放行。"""
    with pytest.raises(SystemExit):
        tool._guard_reserved(None)


@pytest.mark.regression
def test_guard_reserved_allows_ordinary_test_database() -> None:
    """阳性对照：正常测试库名不应被这道门拦住（否则是误杀）。"""
    tool._guard_reserved("rednote_test")  # 不抛即通过


@pytest.mark.regression
def test_main_rejects_mismatched_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--confirm` 必须原样等于解析出的库名——防止复制上一次命令忘了改库名。"""
    monkeypatch.setattr(tool, "_database_name", lambda dsn: "rednote_test")
    with pytest.raises(SystemExit):
        tool.main(["--yes", "--confirm", "some_other_name"])


# ===========================================================================
# 判据 2：真执行前必须先探活（ISS-160 教训落地）
# ===========================================================================


@pytest.mark.regression
def test_yes_path_calls_assert_quiet_before_building_write_engine() -> None:
    """`main()` 在 `--yes` 分支里必须调用 `check_shared_db_quiet.assert_quiet`。

    用 AST 而不是跑一次真流程：这条防线最容易在"图省事先建 engine"的重构里
    被悄悄挪到探活之后——那样探活就成了摆设。断言源码里 `assert_quiet(`
    这个调用**先于** `_build_engine(` 出现在 `main` 函数体内。
    """
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    main_fn = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"),
        None,
    )
    assert main_fn is not None

    call_order: list[str] = []
    for node in ast.walk(main_fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        if name in ("assert_quiet", "_build_engine"):
            call_order.append(name)

    assert "assert_quiet" in call_order, "main() 里找不到 assert_quiet 调用——ISS-160 的教训没落地"
    assert "_build_engine" in call_order, "main() 里找不到 _build_engine 调用"
    assert call_order.index("assert_quiet") < call_order.index("_build_engine"), (
        f"assert_quiet 必须先于 _build_engine 执行，实际顺序：{call_order}"
    )


# ===========================================================================
# 判据 3：DSN 口径——只认 REDNOTE_TEST_DATABASE_URL，不读 REDNOTE_DATABASE_URL
# ===========================================================================


@pytest.mark.regression
def test_dsn_ignores_redone_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """即便 `REDNOTE_DATABASE_URL` 指向别的库，本工具也只看测试库那一份。"""
    monkeypatch.setenv("REDNOTE_DATABASE_URL", "postgresql+psycopg://rednote@h:5432/rednote_dev")
    monkeypatch.delenv(tool.TEST_DATABASE_URL_ENV, raising=False)
    assert tool._test_dsn() == tool.DEFAULT_TEST_DSN


@pytest.mark.regression
def test_dsn_env_override_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(tool.TEST_DATABASE_URL_ENV, "postgresql+psycopg://rednote@h:5432/rednote_test_c")
    assert tool._test_dsn().endswith("/rednote_test_c")


# ===========================================================================
# 判据 4：根表清单不能跟判据 D 漂移（刻意不互相 import，靠对账）
# ===========================================================================


@pytest.mark.regression
def test_root_tables_match_the_volume_judge() -> None:
    sys.path.insert(0, str(REPO_ROOT / "tools" / "quality"))
    import check_test_data_volume as volume_tool

    assert tool.ROOT_TABLES == volume_tool.ROOT_TABLES, (
        f"reset_shared_test_db.ROOT_TABLES={tool.ROOT_TABLES} 与 "
        f"check_test_data_volume.ROOT_TABLES={volume_tool.ROOT_TABLES} 已经漂移"
    )


# ===========================================================================
# 判据 5：闭包算法用的是共享实现，不是本工具另写的一份（铁律 14）
# ===========================================================================


@pytest.mark.regression
def test_truncate_targets_is_the_shared_db_graph_implementation() -> None:
    from rednote.platform import db_graph

    assert tool.truncate_targets is db_graph.truncate_targets, (
        "reset_shared_test_db 必须复用 rednote.platform.db_graph.truncate_targets，"
        "不能另外实现一份闭包算法——两份实现会漂移"
    )


# ===========================================================================
# 判据 6：execute() 必须设超时（第 17 条纪律：中途来了并发必须是可读失败）
# ===========================================================================


@pytest.mark.regression
def test_execute_sets_lock_and_statement_timeout() -> None:
    src = ast.get_source_segment(
        TOOL.read_text(encoding="utf-8"),
        next(
            n
            for n in ast.parse(TOOL.read_text(encoding="utf-8")).body
            if isinstance(n, ast.FunctionDef) and n.name == "execute"
        ),
    ) or ""
    assert "lock_timeout" in src, "execute() 里没设 lock_timeout——并发写入时会无限等锁"
    assert "statement_timeout" in src, "execute() 里没设 statement_timeout"


# ===========================================================================
# 判据 7：TRUNCATE 不带 CASCADE（显式列全表，让"多清了谁"可见，同 ISS-106）
# ===========================================================================


@pytest.mark.regression
def test_truncate_statement_does_not_use_cascade() -> None:
    """只看 `execute()` 函数体本身，不看整份文件——文件 docstring 里正好用
    `CASCADE` 这个词解释"为什么不用它"，文本全文搜索会把解释文字当成实现
    （本项目已现身多次的形态："注释里写了反例，判据把反例当违规"）。
    """
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    execute_fn = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "execute"
    )
    src = ast.get_source_segment(TOOL.read_text(encoding="utf-8"), execute_fn) or ""
    assert "TRUNCATE {" in src, "execute() 里找不到 TRUNCATE 语句模板"
    assert "CASCADE" not in src, (
        "execute() 不应使用 TRUNCATE ... CASCADE——闭包已经显式列出了整条引用链，"
        "CASCADE 会在将来出现新引用时静默清空未被列出的表"
    )
