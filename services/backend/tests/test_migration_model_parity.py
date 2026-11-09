"""`check_migration_model_parity.py` 自身的判据（ISS-146）。

## 为什么判据也要有判据

`tools/quality/check_migration_model_parity.py` 是**声称自己有防线**的一方。
按判据纪律第 9 条（ISS-95）：「声称自己有防线」比「没有防线」更危险。

本文件专盯它**最容易被悄悄削弱**的三处：

1. **排除集是不是由磁盘决定**（铁律 14）。判据靠"排除一类对象"才不误报；
   排除集一旦退回人写名单，就会**静默失效** —— 名单还在排旧名字，
   真对象跑进 diff 报红，人再去手改名单，循环往复。
   本仓此形态已现身 7 次，最近一次（第 7 次）**就发生在本脚本初版**：
   写死的索引名 `ix_chunk_embeddings_embedding_hnsw` **在磁盘上根本不存在**。

2. **派生值的对账**：`_migration_bookkeeping_tables()` 从 env.py / alembic.ini /
   alembic 包源码三级派生。派生得对不对，**不靠自证**，靠连真库看那张表在不在。

3. **`_without_bookkeeping` 只删该删的**：它必须剔掉记账表参与的四类对象
   （表/列/约束/索引），一处不漏；同时**不许碰其他表**。

## 反向验证

`tools/quality/collect_test_db_credibility_reverse_evidence.py` 的 **§3**
（副本变异，真树只读）—— 与它互为两遍：本文件验"函数被直接调用时判得对"，
反向验证验"在真实仓库副本上端到端跑得对"。

⚠️ 2026-09-30 自纠：本行原写的是一个**不存在**的文件名（`collect_migration_model_`…
`_reverse_evidence.py`），本包三条判据实际**共用一个**反向验证脚本。
这正是判据纪律第 9 条的反面教材 —— 文档声称"有那条防线"，却指不出真实文件
⇒ 没人再去看。**引用的文件名写错，与"没有引用"在效果上同形。**
（已由 `collect_test_db_credibility_code_review.py` 的
「文档声明的路径必须存在」判据机械守住。）
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL = REPO_ROOT / "tools" / "quality" / "check_migration_model_parity.py"

sys.path.insert(0, str(TOOL.parent))

import check_migration_model_parity as tool  # noqa: E402


def _test_dsn() -> str:
    """与本仓 `conftest.py` 同口径的测试库 DSN。"""
    return os.environ.get(
        "REDNOTE_TEST_DATABASE_URL",
        "postgresql+psycopg://rednote@localhost:5432/rednote_test",
    )


def _connect_or_skip():
    """连测试库；连不上就 skip（**并写明理由**，不静默通过）。"""
    import psycopg

    raw = _test_dsn().replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        return psycopg.connect(raw)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"测试库不可达（{type(exc).__name__}）—— 本条是「派生值 vs 磁盘真值」的"
            f"对账，没有真库就**没有可对账的对象**，跳过而非假装通过。"
        )


# ===========================================================================
# 判据 1：记账表名 —— 派生值必须命中磁盘上的真物（铁律 14 对账）
# ===========================================================================


@pytest.mark.regression
def test_derived_bookkeeping_name_exists_in_the_real_database() -> None:
    """三级派生出来的记账表名，必须**真的存在于迁移建过的库里**。

    这是本文件最重要的一条。派生逻辑写得多漂亮都不算数 ——
    **只有磁盘上有那张表**才证明派生依据与实现没脱节。

    ⚠️ 方向是单向的：只断言"派生名 ∈ 真库表名"，**不**断言"真库里只有它一张记账表"。
    反向（"没有别的记账表被漏掉"）由 `main()` 的排除集自检承担 ——
    那边拿的是**跑了完整迁移链后的库**，比这里的存量库更权威。
    """
    derived = tool._migration_bookkeeping_tables()
    assert derived, "派生集不得为空 —— 空集会把排除逻辑整个旁路掉"

    with _connect_or_skip() as conn:
        rows = conn.execute(
            "select tablename from pg_tables where schemaname = 'public'"
        ).fetchall()
    real = {r[0] for r in rows}

    missing = derived - real
    assert not missing, (
        f"派生出的记账表名 {sorted(missing)} 在真库里不存在（来源："
        f"{tool._bookkeeping_source()}）—— 排除集的依据与磁盘脱节了"
    )


@pytest.mark.regression
def test_bookkeeping_table_is_scanned_from_disk_not_hardcoded() -> None:
    """`_migration_bookkeeping_tables` 的**源码里不许出现具体表名字面量**。

    铁律 14 的机械护栏。为什么必须机械：人**看得懂**"别写死"这个道理，
    但下一次改这个函数时仍会顺手写 `"alembic_version"` —— 因为它最省事、
    而且当前看起来是对的。只有一条会报红的判据能拦住它。

    ⚠️ 断言口径：只看**函数体内**的字符串常量。合法的字面量是
    `"version_table"`（kwarg 名 / dict key），不是表名本身。
    """
    import ast

    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_migration_bookkeeping_tables"
    )
    body_literals = {
        n.value
        for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    # 表名形态：全是小写字母/下划线，含 `version`，且不是 kwarg 名本身。
    suspicious = {
        s
        for s in body_literals
        if re.fullmatch(r"[a-z][a-z0-9_]*", s)
        and "version" in s
        and s != "version_table"
    }
    assert not suspicious, (
        f"`_migration_bookkeeping_tables` 里出现了疑似**写死的表名** {sorted(suspicious)}。"
        f"该函数必须从 env.py / alembic.ini / alembic 包源码派生（铁律 14）。"
    )


# ===========================================================================
# 判据 2：ANN access method 名单里的名字必须是**真 access method**
# ===========================================================================


@pytest.mark.regression
def test_ann_access_methods_are_real_postgres_access_methods() -> None:
    """`ANN_ACCESS_METHODS` 里的每个名字，必须是 `pg_am` 里真实存在的 access method。

    为什么需要这条：判据**编一个不存在的名字**是它最隐蔽的失效方式 ——
    名单里写个 `"hnsw_ann"`，判据永远不排除任何东西（静默恒真，ISS-115 的亲戚），
    或者反过来永远排除不掉，报一堆假红让人去手改。
    本脚本初版正是这一类：**写死的索引名在磁盘上不存在**（铁律 14 第 7 次）。

    ⚠️ 方向单向：只要求 `ANN_ACCESS_METHODS ⊆ pg_am`。
    **不**要求"pg_am 里的非 btree 都在名单里"—— 那会把判据变成一份要跟
    PostgreSQL 版本走的维护负担。
    """
    with _connect_or_skip() as conn:
        rows = conn.execute("select amname from pg_am").fetchall()
    real = {r[0] for r in rows}

    missing = tool.ANN_ACCESS_METHODS - real
    assert not missing, (
        f"名单里有 PostgreSQL 不认识的 access method {sorted(missing)}"
        f"（本库实际有：{sorted(real)}）—— 编的名字让排除集失效"
    )


@pytest.mark.regression
def test_no_hardcoded_index_names_in_the_tool() -> None:
    """判据源码里不许出现**具体索引名**（`ix_...`）字面量。

    铁律 14 第 7 次的直接护栏。排除集必须按 **access method** 判，
    不能按名字 —— 因为真索引名由 `embedding_ddl.hnsw_index_name(profile_id)`
    在**运行期**拼出来（带 profile uuid 后缀），静态名字写不对。
    """
    src = TOOL.read_text(encoding="utf-8")
    # 去掉 docstring 与注释后再找：docstring 里**引用**旧名字做教训记录是允许的。
    code_only = _strip_docstrings_and_comments(src)
    offenders = re.findall(r"['\"](ix_[a-z0-9_]+)['\"]", code_only)
    assert not offenders, (
        f"判据源码里写死了索引名 {offenders} —— 排除集必须按 access method 判"
        f"（铁律 14：真索引名是运行期生成的）"
    )


def _strip_docstrings_and_comments(src: str) -> str:
    """把模块/类/函数 docstring 与 `#` 注释清掉，只留可执行代码。"""
    import ast
    import io
    import tokenize

    tree = ast.parse(src)
    doc_positions: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(
                body[0].value, ast.Constant
            ) and isinstance(body[0].value.value, str):
                doc_positions.add((body[0].lineno, body[0].col_offset))

    out: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and tok.start in doc_positions:
            continue
        out.append(tok.string)
    return "\n".join(out)


# ===========================================================================
# 判据 3：`_without_bookkeeping` 只删该删的
# ===========================================================================


@pytest.mark.regression
def test_without_bookkeeping_removes_all_four_object_kinds() -> None:
    """记账表参与的四类对象都要被剔掉；**其他表一个都不许碰**。

    这是"排除集只吃掉该吃的"的单元级证明。若只剔 `tables` 而漏了 `column`，
    diff 会在列这一层把记账表的 `version_num` 报成"模型漏了"，
    而人看到的是"表一致、列不一致"这种莫名其妙的话 —— 真差异会被淹在假红里。
    """
    snap = {
        "tables": {"alembic_version", "projects", "users"},
        "column": {
            ("alembic_version", "version_num"),
            ("projects", "id"),
        },
        "constraint": {
            ("alembic_version", "alembic_version_pkc"),
            ("projects", "projects_pkey"),
        },
        "index": {
            ("alembic_version", "alembic_version_pkc", "CREATE UNIQUE INDEX ..."),
            ("projects", "projects_pkey", "CREATE UNIQUE INDEX ..."),
        },
        "ann_indexes": {("chunk_embeddings", "ix_probe_hnsw")},
        "bookkeeping_tables": {"alembic_version"},
    }
    out = tool._without_bookkeeping(snap, frozenset({"alembic_version"}))

    for kind in ("tables", "column", "constraint", "index"):
        assert all(
            item[0] != "alembic_version" if isinstance(item, tuple) else item != "alembic_version"
            for item in out[kind]
        ), f"{kind} 里还留着记账表参与的对象"

    # 其他表一个都不许少
    assert out["tables"] == {"projects", "users"}
    assert out["column"] == {("projects", "id")}
    assert out["constraint"] == {("projects", "projects_pkey")}
    assert out["index"] == {("projects", "projects_pkey", "CREATE UNIQUE INDEX ...")}
    # ANN 分类与原始快照一并透传（它不经排除集处理）
    assert out["ann_indexes"] == {("chunk_embeddings", "ix_probe_hnsw")}


@pytest.mark.regression
def test_without_bookkeeping_is_identity_when_nothing_to_exclude() -> None:
    """排除集为空时必须是**恒等变换** —— 否则"没东西可排"这条路径没被覆盖。"""
    snap = {
        "tables": {"projects"},
        "column": {("projects", "id")},
        "constraint": set(),
        "index": set(),
        "ann_indexes": set(),
        "bookkeeping_tables": set(),
    }
    out = tool._without_bookkeeping(snap, frozenset())
    assert out["tables"] == snap["tables"]
    assert out["column"] == snap["column"]


# ===========================================================================
# 判据 4：派生失败必须**抛**，不许静默返回空集
# ===========================================================================


@pytest.mark.regression
def test_derivation_raises_when_no_source_is_readable(tmp_path, monkeypatch) -> None:
    """三个来源都读不到时，必须**抛异常**，不能返回空集。

    "没跑通"与"跑过且无排除项"在返回值上同形（ISS-143）：返回空集意味着
    排除逻辑被静默旁路 —— diff 会把记账表报成"模型漏了"，人再去手改名单。
    抛异常是唯一能让它**当场可见**的行为。
    """
    monkeypatch.setattr(tool, "REPO_ROOT", tmp_path)  # 空目录：env.py / alembic.ini 都不在

    import alembic

    upstream = Path(alembic.__file__).resolve().parent / "runtime" / "migration.py"
    real_read = Path.read_text

    def fake_read(self, *a, **kw):
        if self == upstream:
            # 让 alembic 包源码也解析不出 → 三个来源全断
            return "x = 1\n"
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", fake_read)
    with pytest.raises(RuntimeError, match="无法从"):
        tool._migration_bookkeeping_tables()


# ===========================================================================
# 判据 5：判据自身的覆盖范围（铁律 14 第 5 次形态）
# ===========================================================================


@pytest.mark.regression
def test_base_dsn_default_does_not_point_at_a_system_database() -> None:
    """判据自带的默认 DSN 也不许指向 `postgres`（ISS-145 一族的自查）。

    判据会**新建/删除**库。若基准 DSN 指向 `postgres`，管理连接就落在系统库上
    —— 与 ISS-145 同类：脚本自己犯它要抓的错。
    """
    assert tool.database_name_of(tool.DEFAULT_BASE_DSN) not in {
        "postgres",
        "template0",
        "template1",
    }, "判据自带的默认 DSN 指向了系统库"
