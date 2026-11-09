"""`check_test_residue.py` 自身的判据（ISS-147）。

## 为什么判据也要有判据

`tools/quality/check_test_residue.py` 是**声称自己有防线**的一方。按判据纪律
第 9 条（ISS-95）：「声称自己有防线」比「没有防线」更危险 —— 所有人以为
有人看着，就没人再手工复核。

本文件盯它**最容易被悄悄削弱的五处**：

1. **"测试真跑过"这条前置条件必须存在**（ISS-143）。
   本脚本的判定是"跑测试前后的结构差分"。而"测试一条都没跑"
   （pytest 退出码 4）与"跑了且无残留"在**差分上完全同形** —— 都是"没多出东西"。
   实测踩过一次：`DEFAULT_PYTEST_ARGS` 用 `str.split()` 把 `-m "not slow"`
   拆成 3 个 token，pytest 拿 `slow` 当路径 → `no tests ran` → 判据照常打印
   `[PASS]`。因此本条用 AST 断言 `main()` 里确实有那条 `collected == 0` 拦截。
2. **`-m` 表达式必须是一个 argv 元素**（上一条的根因）。断言
   `DEFAULT_PYTEST_ARGS` 的元素里存在完整的 `"not slow"`，
   且 `_parse_pytest_args` 走 shell 语义（尊重引号）。
3. **差分只看"多出来的"**：少了对象是清理（好事），不该报残留。
   断言 `_diff` 的语义方向。
4. **快照 SQL 必须纯只读**。判定脚本自己写库会污染基线 ——
   那是"判据成了被测对象的干扰源"，比不判更糟。
5. **不许写死索引名**（铁律 14）。本仓实测残留是
   `ix_chunk_embeddings_hnsw_<profile_uuid>`（8 个），名字里
   **没有任何测试特征词**。若哪天判据退回"查名字名单"，它会静默漏判 ——
   而它照样打印 `[PASS]`。

## 反向验证

`tools/quality/collect_test_db_credibility_reverse_evidence.py` 的 **§2**
（副本变异，真树只读）—— 与它互为两遍：本文件验"函数被直接调用时判得对"，
反向验证验"在真实仓库副本上端到端跑得对"（含 B①-a/B①-b 成对变异，
证明那条前置条件是**必要防线**而非装饰）。
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL = REPO_ROOT / "tools" / "quality" / "check_test_residue.py"

sys.path.insert(0, str(TOOL.parent))

import check_test_residue as tool  # noqa: E402


# ===========================================================================
# 判据 1：覆盖范围/输入不能是错的（ISS-118 同族：判据读到的值凭什么是现在的）
# ===========================================================================


@pytest.mark.regression
def test_default_pytest_args_keeps_m_expression_as_one_argv_element() -> None:
    """`-m` 的表达式必须是**一个** argv 元素。

    这是本脚本实测踩过的真坑：初版用 `"… -m not slow".split()`，
    `-m "not slow"` 被拆成 `['-m', '"not', 'slow"']` → pytest 拿 `slow`
    当路径 → `no tests ran`（退出码 4）→ **"没跑"伪装成"跑过且无残留"**。

    因此这里断言：`DEFAULT_PYTEST_ARGS` 里存在**完整的** `"not slow"`
    （带空格，是一个元素），而不是被拆开的三段。
    """
    args = tool.DEFAULT_PYTEST_ARGS
    assert isinstance(args, tuple), (
        f"DEFAULT_PYTEST_ARGS 必须是 tuple（当前 {type(args).__name__}）—— "
        f"str 会诱使调用方用 .split() 拆它，那正是本脚本踩过的坑"
    )
    assert "-m" in args, f"DEFAULT_PYTEST_ARGS 里没有 -m：{args}"
    idx = args.index("-m")
    assert idx + 1 < len(args), "`-m` 后面没有表达式 —— pytest 会报用法错"
    expr = args[idx + 1]
    assert expr == "not slow", (
        f"`-m` 的表达式被拆开了：{args}。\n"
        f"期望它是**一个**元素 'not slow'，实际取到 {expr!r}。\n"
        f"后果：pytest 拿 'slow' 当路径 → no tests ran → 与「跑过且无残留」同形（ISS-143）。"
    )
    assert not any(a in ('"not', 'slow"', "not'", "'slow") for a in args), (
        f"参数里出现被引号切碎的碎片：{args}（说明某处用了 str.split 而非 shlex）"
    )


@pytest.mark.regression
def test_parse_pytest_args_respects_shell_quoting() -> None:
    """环境变量里的参数串必须按 **shell 语义**解析（尊重引号）。

    与上一条互补：上一条查默认值，本条查"用环境变量覆盖时"的路径 ——
    两条路径都会走到 pytest，任何一条拆错都会让"没跑"变成"假绿"。
    """
    assert tool._parse_pytest_args('-m "not slow"') == ["-m", "not slow"], (
        "引号没有被尊重 —— `str.split()` 会把 -m 的表达式拆开"
    )
    assert tool._parse_pytest_args("-m not slow") == ["-m", "not", "slow"], (
        "无引号时按空格切（这是 shell 的正确语义，不是缺陷）"
    )
    assert tool._parse_pytest_args("") == []
    assert tool._parse_pytest_args("  -p no:cacheprovider  ") == ["-p", "no:cacheprovider"]


@pytest.mark.regression
@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        ("1749 passed, 1 skipped in 127.05s", (1749, 1749)),
        ("10 passed in 1.0s", (10, 10)),
        ("5 failed, 10 passed in 2.0s", (10, 15)),
        ("3 errors, 7 passed in 2.0s", (7, 10)),
        ("no tests ran in 0.05s", (0, 0)),
        ("", (0, 0)),
    ],
)
def test_parse_pytest_counts(tail: str, expected: tuple[int, int]) -> None:
    """`_parse_pytest_counts` 是"测试真跑过"这条前置条件的**输入**。

    ⚠️ 它错了，整条前置条件就失效 —— 与 `test_database_name_parsing`
    同一条纪律：判据的输入自己也得被判。

    `no tests ran`（退出码 4 的典型输出）必须解析成 `(0, 0)`；
    解析不出时返回 `(0, 0)` 而不是"乐观地当成跑了 1 条"。
    """
    assert tool._parse_pytest_counts(tail) == expected


# ===========================================================================
# 判据 2：那条前置条件必须真的在（ISS-143）
# ===========================================================================


@pytest.mark.regression
def test_main_guards_against_zero_collected_tests() -> None:
    """`main()` 里必须有「收集数为 0 → 直接 FAIL」的拦截。

    用 **AST** 而不是正则/字符串查找：判据自身的**文档字符串里**就写了
    `collected == 0` 这个式子（讲为什么需要它）。用文本查找会把解释文字
    当成实现 —— 本项目已现身 6 次的形态（"注释里写了反例，判据把反例当违规"）。

    断言两点，缺一不可：
      · 存在 `if collected == 0:` 这个比较（含对 `collected` 的比较）；
      · 该分支体里有 `return`（不是只 print 一句就往下走 —— 那等于没拦）。
    """
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))

    main_fn = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            main_fn = node
    assert main_fn is not None, "找不到顶层 main() —— 判据的结构变了"

    guards: list[ast.If] = []
    for node in ast.walk(main_fn):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare):
            continue
        if not (isinstance(test.left, ast.Name) and test.left.id == "collected"):
            continue
        ops = [type(op).__name__ for op in test.ops]
        consts = [c.value for c in test.comparators if isinstance(c, ast.Constant)]
        if "Eq" in ops and 0 in consts:
            guards.append(node)

    assert guards, (
        "main() 里找不到 `if collected == 0:` 的拦截。\n"
        "后果（ISS-143）：pytest 一条都没跑时退出码为 4，差分看不出任何区别，\n"
        "判据会打印 [PASS] —— 「没跑」与「跑过且无残留」同形。"
    )
    has_return = any(
        isinstance(sub, ast.Return) for guard in guards for sub in ast.walk(guard)
    )
    assert has_return, (
        "找到了 `collected == 0` 的比较，但分支体里没有 return —— "
        "只打印不返回等于没拦，判据仍会往下走并打印 [PASS]"
    )


@pytest.mark.regression
def test_main_fails_when_pytest_itself_fails() -> None:
    """pytest 自己红了时，差分"跑在错的底座上"，结论不可信 —— 必须拦截。

    断言 `main()` 里有对 `proc.returncode` 的非零比较且有 `return`。
    """
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    main_fn = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"),
        None,
    )
    assert main_fn is not None

    hits: list[ast.If] = []
    for node in ast.walk(main_fn):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare):
            continue
        src = ast.dump(test)
        if "returncode" in src and any(
            type(op).__name__ == "NotEq" for op in test.ops
        ):
            if any(isinstance(sub, ast.Return) for sub in ast.walk(node)):
                hits.append(node)

    assert hits, (
        "main() 里找不到「pytest 退出码非 0 → FAIL 并 return」的拦截。\n"
        "测试自己红了的时候，差分基线是坏的，此时的 [PASS] 是无意义的。"
    )


# ===========================================================================
# 判据 3：差分的语义方向（少了不算残留）
# ===========================================================================


@pytest.mark.regression
def test_diff_counts_only_additions() -> None:
    """`_diff` 只报**新增**对象；被删掉的对象不算残留（那是清理，属好事）。

    ⚠️ 快照必须**含全部类别**（本用例显式构造）—— 这是 `_diff` 的输入契约：
    真实路径上 before/after 都出自 `_snapshot()`，必然齐备。缺类别时
    `_diff` 会 `KeyError`，而这是**有意的**：静默跳过缺的类别会让
    「没观察这一类」与「这一类没变化」同形（ISS-143 族）。
    """
    before = {kind: set() for kind in tool.SNAPSHOT_QUERIES}
    after = {kind: set() for kind in tool.SNAPSHOT_QUERIES}
    before["table"], before["index"] = {"a", "b"}, {"i1"}
    after["table"] = {"a", "b", "c"}  # 新增 c
    after["trigger"] = {"t1"}  # 新增 t1
    # index 从 {i1} 变为空 —— 少了，不该报。

    diff = tool._diff(before, after)
    assert diff.get("table") == {"c"}, f"新增表没被判出：{diff}"
    assert diff.get("trigger") == {"t1"}, f"新增触发器没被判出：{diff}"
    assert "index" not in diff, (
        f"索引**少了**却被当成残留：{diff} —— 删除是清理，不是残留"
    )


@pytest.mark.regression
def test_diff_requires_every_snapshot_category() -> None:
    """缺类别时 `_diff` 必须**抛错**，不许静默跳过。

    真实路径上快照由 `_snapshot()` 产生、必然齐备。若哪天有人改成
    `after.get(kind, set())`，"少观察了一类"就与"这一类没变化"同形 ——
    判据会静默缩小观察面而照样打印 `[PASS]`（ISS-143 / ISS-137 族）。
    本用例把这个契约钉住。
    """
    full = {kind: set() for kind in tool.SNAPSHOT_QUERIES}
    incomplete = dict(full)
    del incomplete["trigger"]

    with pytest.raises(KeyError):
        tool._diff(full, incomplete)
    with pytest.raises(KeyError):
        tool._diff(incomplete, full)


@pytest.mark.regression
def test_diff_is_empty_when_structure_is_unchanged() -> None:
    """结构不变时差分必须为空（否则判据会在干净库上恒红）。"""
    snap = {kind: {f"{kind}_obj"} for kind in tool.SNAPSHOT_QUERIES}
    same = {k: set(v) for k, v in snap.items()}
    assert tool._diff(snap, same) == {}
    assert tool._diff(snap, snap) == {}  # 同一对象也不该炸


# ===========================================================================
# 判据 4：快照 SQL 必须纯只读（判据不许成为被测对象的干扰源）
# ===========================================================================


@pytest.mark.regression
def test_snapshot_queries_are_read_only() -> None:
    """每一条快照 SQL 都必须是纯 `select`。

    判据自己若建/删对象，它就会**污染基线** —— 那是"判据成了被测对象的
    干扰源"，比不判更糟：前后快照都被自己改过，差分失去意义。
    """
    banned = re.compile(r"\b(create|drop|alter|insert|update|delete|truncate|grant)\b", re.I)
    assert tool.SNAPSHOT_QUERIES, "SNAPSHOT_QUERIES 是空的 —— 判据没有观察对象"
    for kind, sql in tool.SNAPSHOT_QUERIES.items():
        assert re.match(r"^\s*select\b", sql, re.I), f"[{kind}] 不是以 select 开头：{sql[:60]}"
        m = banned.search(sql)
        assert not m, (
            f"[{kind}] 快照 SQL 里出现写操作 {m.group(0)!r}：{sql}\n"
            f"判据必须纯只读，否则它自己改变了基线。"
        )


@pytest.mark.regression
def test_snapshot_covers_the_object_kinds_that_actually_residue() -> None:
    """快照必须覆盖**本仓实测过的残留形态**，且索引条目要能定位到表。

    · `index` 是本仓残留主力（8 个 HNSW）；
    · `table` 是 §4.2-B 那 74,145 行的形态；
    · 索引/触发器/约束的名字必须拼上表名（`表.对象`），
      否则残留报告里只有一串看不出归属的索引名 —— 无法定位也就无法修。
    """
    for kind in ("table", "index", "view", "sequence", "trigger", "constraint"):
        assert kind in tool.SNAPSHOT_QUERIES, f"缺少对象类别 {kind}"

    # 索引条目必须是 `relname || '.' || indexrelname` 形态（能看出属于哪张表）。
    idx_sql = tool.SNAPSHOT_QUERIES["index"]
    assert "relname" in idx_sql and "||" in idx_sql, (
        f"索引快照没有把表名拼进去 —— 残留报告会看不出归属：{idx_sql}"
    )


# ===========================================================================
# 判据 5：不许写死对象名（铁律 14）
# ===========================================================================


@pytest.mark.regression
def test_no_hardcoded_residue_object_names_outside_docstrings() -> None:
    """判据的判定逻辑里不许出现写死的索引名/表名名单（铁律 14）。

    本仓实测残留是 `ix_chunk_embeddings_hnsw_<profile_uuid>` ——
    名字里**没有任何**测试特征词，看起来就是个正常业务索引。
    若判定退回"查名字名单"，它会静默漏判而照样打印 `[PASS]`。

    ⚠️ 必须**剥掉 docstring** 再查：本脚本的模块 docstring 里**故意**写了
    `ix_chunk_embeddings_hnsw_` 作为事实陈述（讲为什么名字名单没用）。
    只剥 `#` 注释会把这段解释文字当成写死的名单 —— 本项目已现身 6 次的形态。

    允许的例外只有 `_classify` 里那句**提示文案**（它只影响打印，
    不影响判定）—— 因此断言的范围限定为：`_classify` 之外不得出现 `ix_` 字面量。
    """
    src = TOOL.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 收集所有 docstring 节点的行区间（模块/类/函数的第一条 Expr(Constant str)）。
    doc_spans: set[int] = set()

    def collect_doc_lines(node: ast.AST) -> None:
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                start = first.lineno
                end = first.end_lineno or first.lineno
                doc_spans.update(range(start, end + 1))
        for child in ast.iter_child_nodes(node):
            collect_doc_lines(child)

    collect_doc_lines(tree)

    # `_classify` 的允许行区间（它只产出提示文案）。
    classify_span: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_classify":
            classify_span.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    offenders: list[tuple[int, str]] = []
    for lineno, line in enumerate(src.splitlines(), start=1):
        if lineno in doc_spans or lineno in classify_span:
            continue
        code = line.split("#", 1)[0]
        if re.search(r"['\"]ix_[A-Za-z0-9_]+", code):
            offenders.append((lineno, line.strip()))

    assert not offenders, (
        "判定逻辑里出现写死的索引名（应当由差分/磁盘决定，铁律 14）：\n"
        + "\n".join(f"  第 {n} 行: {t}" for n, t in offenders)
    )


@pytest.mark.regression
def test_classify_only_affects_the_message_not_the_verdict() -> None:
    """`_classify` 的启发式只许影响**提示文案**，不许参与判定。

    它是本脚本唯一允许做"名字启发式"的地方。若哪次重构把它接进判定路径
    （例如 `if _classify(...)` 决定报不报），判据就退化成"查名字名单"，
    一个名字里没有特征词的残留会被静默放过。
    """
    src = TOOL.read_text(encoding="utf-8")
    # 唯一允许的用法形态：`hint = _classify(names, kind)`
    assert re.search(r"^\s*hint\s*=\s*_classify\(", src, re.M), (
        "找不到 `hint = _classify(...)` —— 提示文案的接线变了，请复核本条"
    )
    other_uses = [
        line.strip()
        for line in src.splitlines()
        if "_classify(" in line and "hint = _classify(" not in line
    ]
    # 允许 `def _classify(` 这一行与 docstring 里提到它。
    other_uses = [
        t for t in other_uses
        if not t.startswith("def _classify(") and not t.startswith(("#", '"', "'", "·", "`"))
    ]
    assert not other_uses, (
        f"`_classify` 出现在判定路径上（不只影响文案）：\n" + "\n".join(other_uses)
        + "\n它只许产出提示文案（判定必须完全由差分决定）。"
    )


# ===========================================================================
# 判据 6：默认 DSN 不得指向系统库（与判据 A 同口径）
# ===========================================================================


@pytest.mark.regression
def test_default_dsn_does_not_point_at_a_reserved_database() -> None:
    """本判据默认要连的库不得是 `postgres` / `template*`（ISS-145）。

    用**判据 A 的解析函数**来判（`check_test_database_target.database_name`），
    不再自己实现一份 —— 两份解析会漂移，判据就变成自证。
    跨工具 import 失败时跳过（并写明理由），不让本用例成为脆弱点。
    """
    try:
        sys.path.insert(0, str(REPO_ROOT / "tools" / "quality"))
        import check_test_database_target as target_tool
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"无法 import check_test_database_target（{type(exc).__name__}）")

    db = target_tool.database_name(tool.DEFAULT_TEST_DSN)
    assert db is not None, f"判据自带的默认 DSN 解析不出库名：{tool.DEFAULT_TEST_DSN}"
    assert db not in target_tool.STATIC_RESERVED_DATABASES, (
        f"判据 B 的默认目标库是保留库 {db!r} —— 它自己就是 ISS-145 的违反者。\n"
        f"默认 DSN：{tool.DEFAULT_TEST_DSN}"
    )


@pytest.mark.regression
def test_env_override_is_respected_for_the_target_database() -> None:
    """目标库必须可被 `REDNOTE_TEST_DATABASE_URL` 覆盖（并发会话各自隔离，§4.2-D）。"""
    saved = os.environ.get("REDNOTE_TEST_DATABASE_URL")
    try:
        os.environ["REDNOTE_TEST_DATABASE_URL"] = "postgresql+psycopg://rednote@h:5432/rednote_test_b"
        assert tool._test_dsn().endswith("/rednote_test_b"), (
            "环境变量没能覆盖默认目标库 —— 并发开发线无法各自隔离（§4.2-D）"
        )
    finally:
        if saved is None:
            os.environ.pop("REDNOTE_TEST_DATABASE_URL", None)
        else:
            os.environ["REDNOTE_TEST_DATABASE_URL"] = saved
