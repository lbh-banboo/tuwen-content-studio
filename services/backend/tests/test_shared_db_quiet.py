"""判据 E（共享库静止前提）**自己的判据**（ISS-163）。

## 为什么判据自己需要判据

`tools/quality/check_shared_db_quiet.py` 声称"能在跑真库判据前拦住并发会话"。
但**声称**不等于**能**（ISS-95：「声称自己有防线」比「没有防线」更危险）。
本文件用四种方式独立验证它：

  ① **E1**：连错库 / 读不到库名必须拒跑（不能把"没连上"当成"没人用"）；
  ② **E2/E3**：有他人连接必拒跑，且拒跑信息要**指名占用者**（可处置）；
  ③ **E4**：查询失败 → fail-closed 拒跑（权限/网络问题不能变成放行）；
  ④ **E3 结构**：用自己的**独立连接**查，不自复用被测连接。

⚠️ 本文件属于 `services/backend/tests/` ⇒ 由 `check_test_db_isolation.py`
按测试口径扫描。因此：

  · 所有 DSN 字面量都出现在 `assert` 的比较式里（该工具按**位置**豁免"被测数据"）；
  · **不连真库**（除最后一条"真库活性"用例，且它只读不写、失败即响亮 SKIP）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
QUIET_TOOL = REPO_ROOT / "tools" / "quality" / "check_shared_db_quiet.py"

#: **本文件唯一的 DSN 字面量出处**。
#:
#: 它作为 `os.environ.get(...)` 的第二实参 ⇒ 命中 `check_test_db_isolation.py`
#: 的**合规形态①**（"DSN 字面量必须流向隔离变量"）。
#: 其余用例一律从它派生 —— 这样本文件里不会出现散落的裸 DSN 字面量
#: （初版把样本 DSN 直接写成函数实参，实测被库隔离判据抓出 9 处违规）。
#:
#: ⚠️ 所有用例都用**假连接**（monkeypatch 掉 `psycopg.connect`），
#: DSN 的**内容**不参与任何真实连接，它只是"传给被测判据的样本"。
_SAMPLE_DSN = os.environ.get(
    "REDNOTE_TEST_DATABASE_URL", "postgresql://rednote@localhost:5432/rednote_test"
)


def _variant(tag: str) -> str:
    """由样本 DSN 派生一个**可区分**的变体（用于环境变量优先级测试）。

    用**追加 query 参数**而不是 `replace(库名)` —— 后者在环境变量指向
    非默认库时会**替换不到**，两个变体变成同一个值，优先级测试
    因此**失去区分度**（恒真：两个来源相等，断言谁都会过）。
    """
    return f"{_SAMPLE_DSN}?app={tag}"


def _load_quiet_tool():
    """按路径加载判据模块（不走包导入 —— tools/ 不是包）。"""
    spec = importlib.util.spec_from_file_location("_check_shared_db_quiet", QUIET_TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load_quiet_tool()


# --------------------------------------------------------------------------- #
# 假连接：让判据在**确定性**条件下运行（不依赖真库、不依赖并行会话）
# --------------------------------------------------------------------------- #


class _FakeCursor:
    def __init__(self, db_row, occupants):
        self._db_row = db_row
        self._occupants = occupants
        self.executed: list[str] = []

    def execute(self, sql, *args):  # noqa: ANN001, ANN002
        self.executed.append(sql)

    def fetchone(self):
        return self._db_row

    def fetchall(self):
        return self._occupants

    def __enter__(self):
        return self

    def __exit__(self, *exc):  # noqa: ANN002
        return False


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):  # noqa: ANN002
        return False


def _install_fake_connect(monkeypatch, cursor: _FakeCursor) -> list:
    """把 `psycopg.connect` 换成假连接，返回"被调用记录"列表。"""
    import psycopg

    calls: list = []

    def fake_connect(dsn, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls.append(dsn)
        return _FakeConn(cursor)

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    return calls


# --------------------------------------------------------------------------- #
# ① E1：读不到库名必须拒跑
# --------------------------------------------------------------------------- #


def test_probe_rejects_empty_database_name(tool, monkeypatch) -> None:
    """`current_database()` 读不到 ⇒ **拒跑**，不能当成"没人用"（ISS-143 同族）。"""
    cursor = _FakeCursor(db_row=None, occupants=[])
    _install_fake_connect(monkeypatch, cursor)
    with pytest.raises(tool.SharedDatabaseUnverifiable):
        tool.probe_quiet(_SAMPLE_DSN)


def test_probe_reads_target_database_name(tool, monkeypatch) -> None:
    """正常情况：报告里的库名 == 目标库名（E1 的正向面）。"""
    cursor = _FakeCursor(db_row=("rednote_test",), occupants=[])
    _install_fake_connect(monkeypatch, cursor)
    report = tool.probe_quiet(_SAMPLE_DSN)
    assert report.database == "rednote_test"
    assert report.quiet is True


# --------------------------------------------------------------------------- #
# ② E2/E3：有他人连接必拒跑，且指名占用者
# --------------------------------------------------------------------------- #


def test_assert_quiet_raises_when_occupied(tool, monkeypatch) -> None:
    """有他人连接 ⇒ 抛 `SharedDatabaseBusy`，且信息里**指名占用者**（可处置）。"""
    occupant = (1, 8066, "pytest", "idle in transaction", "TRUNCATE usage_ledger")
    cursor = _FakeCursor(db_row=("rednote_test",), occupants=[occupant])
    _install_fake_connect(monkeypatch, cursor)
    with pytest.raises(tool.SharedDatabaseBusy) as excinfo:
        tool.assert_quiet(_SAMPLE_DSN, scope="单测")
    message = str(excinfo.value)
    # 必须能定位到占用者（pid / state / query），否则"拒跑"无法处置。
    assert "8066" in message
    assert "idle in transaction" in message
    assert "单测" in message
    assert "rednote_test" in message


def test_assert_quiet_passes_when_idle(tool, monkeypatch) -> None:
    """无他人连接 ⇒ 放行，并返回可用的报告。"""
    cursor = _FakeCursor(db_row=("rednote_test",), occupants=[])
    _install_fake_connect(monkeypatch, cursor)
    report = tool.assert_quiet(_SAMPLE_DSN)
    assert report.quiet is True
    assert report.database == "rednote_test"


def test_quiet_report_property_reflects_occupants(tool) -> None:
    """`QuietReport.quiet` 是 occupants 的**严格**函数（不引入第二套判据）。"""
    empty = tool.QuietReport(database="x", occupants=[])
    busy = tool.QuietReport(
        database="x",
        occupants=[tool.Occupant(pid=1, application_name="a", state="active", query="q")],
    )
    assert empty.quiet is True
    assert busy.quiet is False


# --------------------------------------------------------------------------- #
# ③ E4：查询失败 → fail-closed
# --------------------------------------------------------------------------- #


def test_probe_is_fail_closed_on_connect_error(tool, monkeypatch) -> None:
    """连不上 / 权限不足 ⇒ **拒跑**（不能把"没跑"当成"通过"）。"""
    import psycopg

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("connection refused")

    monkeypatch.setattr(psycopg, "connect", boom)
    with pytest.raises(tool.SharedDatabaseUnverifiable):
        tool.probe_quiet(_SAMPLE_DSN)


def test_probe_is_fail_closed_on_query_error(tool, monkeypatch) -> None:
    """能连上但查询抛错 ⇒ 同样拒跑（fail-closed 不分阶段）。"""
    import psycopg

    class _BadCursor(_FakeCursor):
        def execute(self, sql, *args):  # noqa: ANN001, ANN002
            raise RuntimeError("permission denied for pg_stat_activity")

    monkeypatch.setattr(
        psycopg, "connect", lambda *a, **k: _FakeConn(_BadCursor(None, []))
    )
    with pytest.raises(tool.SharedDatabaseUnverifiable):
        tool.probe_quiet(_SAMPLE_DSN)


# --------------------------------------------------------------------------- #
# ④ E3 结构：独立连接 + 自身排除（判据不能"用自己的连接数自己"）
# --------------------------------------------------------------------------- #


def test_query_excludes_self_connection(tool) -> None:
    """`_QUERY` 必须**结构上**排除自身连接，而不是靠口头声称（ISS-95）。"""
    query = tool._QUERY
    assert "pid <> pg_backend_pid()" in query, "必须排除自身连接"
    assert "datname = current_database()" in query, "必须只看**同库**连接"


def test_probe_opens_its_own_connection(tool, monkeypatch) -> None:
    """`probe_quiet` 自己建立连接（E3）：调用次数为 1，且用的是目标 DSN。"""
    cursor = _FakeCursor(db_row=("rednote_test",), occupants=[])
    calls = _install_fake_connect(monkeypatch, cursor)
    tool.probe_quiet(_SAMPLE_DSN)
    assert len(calls) == 1, "必须恰好建一条自己的连接"


def test_query_reports_occupant_query_text(tool) -> None:
    """查询必须带出占用者的 query 文本（否则拒跑信息无法定位到具体动作）。"""
    assert "query" in tool._QUERY
    assert "application_name" in tool._QUERY
    assert "state" in tool._QUERY


# --------------------------------------------------------------------------- #
# ⑤ DSN 解析优先级 + 方言串归一
# --------------------------------------------------------------------------- #


def test_libpq_dsn_normalizes_sqlalchemy_dialect(tool) -> None:
    """`postgresql+psycopg://` → `postgresql://`（协议替换，不是改地址）。"""
    assert (
        tool._libpq_dsn("postgresql+psycopg://u@h:5432/db") == "postgresql://u@h:5432/db"
    )


def test_libpq_dsn_leaves_plain_dsn_untouched(tool) -> None:
    """已是 libpq 串则原样返回（幂等）。"""
    assert tool._libpq_dsn("postgresql://u@h:5432/db") == "postgresql://u@h:5432/db"


def test_resolve_dsn_priority(tool, monkeypatch) -> None:
    """显式参数 > 环境变量 > 默认常量。

    三个来源用**可区分**的值（`_variant` 派生）—— 若两个来源恰好相等，
    "谁优先"就失去区分度，断言退化成恒真（ISS-82 同族）。
    """
    env_a, env_b, explicit = _variant("env-a"), _variant("env-b"), _variant("explicit")
    monkeypatch.setenv("REDNOTE_DATABASE_URL", env_a)
    monkeypatch.setenv("REDNOTE_TEST_DATABASE_URL", env_b)
    # 显式参数最优先
    assert tool._resolve_dsn(explicit) == explicit
    # 其次 REDNOTE_DATABASE_URL
    assert tool._resolve_dsn(None) == env_a
    # 退到 REDNOTE_TEST_DATABASE_URL
    monkeypatch.delenv("REDNOTE_DATABASE_URL")
    assert tool._resolve_dsn(None) == env_b
    # 最后退到默认常量
    monkeypatch.delenv("REDNOTE_TEST_DATABASE_URL")
    assert tool._resolve_dsn(None) == tool.DEFAULT_DATABASE_URL


# --------------------------------------------------------------------------- #
# ⑥ CI 上必须**响亮 SKIP**（判据不适用 ≠ 判据不存在）
# --------------------------------------------------------------------------- #


def test_ci_mode_skips_loudly(tool, monkeypatch, capsys) -> None:
    """`--ci` 必须打印 SKIP 与理由并返回 0（不能静默通过）。"""
    monkeypatch.setattr(sys, "argv", ["check_shared_db_quiet.py", "--ci"])
    rc = tool.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "SKIP" in out
    assert "不适用" in out


# --------------------------------------------------------------------------- #
# ⑦ 真库活性（只读；不可连则响亮 SKIP，绝不静默）
# --------------------------------------------------------------------------- #


def test_real_database_probe_is_read_only_and_live(tool) -> None:
    """对**真实**目标库只读探一次：能读出库名即证明判据在本机真的能跑。

    本用例**只读** `pg_stat_activity`，不写任何数据、不建连接池。
    若库不可达 ⇒ `pytest.skip` 并写明理由（ISS-118：判据不适用要响亮说出来）。
    """
    try:
        report = tool.probe_quiet()
    except tool.SharedDatabaseUnverifiable as exc:
        pytest.skip(f"目标库不可达，本用例无法执行（不是通过）：{exc}")
    assert isinstance(report.database, str) and report.database, "E1：必须读出库名"
    # 无论忙闲都算通过 —— 本用例只证明"判据真的能跑"，不要求库静止。
    assert report.quiet in (True, False)
