"""`check_rollback_drill.py` 自身的判据（WP47 ⑤⑥）。

## 为什么判据还要有判据

`tools/quality/check_rollback_drill.py` 是**声称自己有防线**的一方（ISS-95）。
它声称三件事，而这三句话里每一句都能被悄悄削弱，且**削弱之后它照样全绿**：

1. **「指纹相等」是灵敏的** —— 幕 2 靠"删一个索引指纹必须变"自证。
   若 `_table_exists()` 被写成 `bool(rows)`（见下），幕 5 的"部分 DDL 已回滚"
   会**恒真**；若 `_fp_diff()` 退化成只比一类，幕 3 的"复原"会漏检。
   ⇒ 第 1/2 条把 `_table_exists` 的坑用**阳性/阴性对照**钉死。

2. **「停在上一条已提交的迁移」是真的** —— 幕 5② 的绿只在
   `transaction_per_migration=True` 时为真。幕 6 用**沙箱翻转那一行**做反向验证。
   若 `_stage_migrations()` 翻转失败却"照原样跑"，两臂变成同一个臂 ⇒
   幕 6 恒真。⇒ 第 11 条钉住翻转的唯一性前提。

3. **「推导不出来就报 FAIL，不跳过」** —— ISS-143：没跑与跑失败同形。
   幕 5 的两个 `is None` 分支若被改成 `pass`，判据条数会少 4~6 条并被
   `EXPECTED_CHECKS` 全等断言挡住；但**有人补几条无关判据凑数**就能绕过去。
   ⇒ 第 16 条按名点名那 6 条必须在 `is None` 分支里以 `False` 出现。

## ★ 本文件的核心教训：`bool(fetchall())` 恒真

`select to_regclass('public.不存在')` 返回 **一行 NULL**，不是零行。
故 `bool(conn.execute(...).fetchall())` 恒为 `True` —— 存在性判据恒真（ISS-115）。
开发期探针正是这么写错的（2026-10-07 实测抓出），故第 1 条用真实返回形态
（`(None,)` / `(None,)` 的**长度 1 列表**）做阴性对照，而不是用"空列表"糊过去。

## 与演练脚本的分工

本文件在**单元层**验"函数被直接调用时判得对"；`check_rollback_drill.py`
自己在私有库上跑六幕剧。两遍互补：本文件保证对照本身没被删空，
脚本保证对照真的在跑。
"""

from __future__ import annotations

import ast
import re
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_PATH = REPO_ROOT / "tools" / "quality" / "check_rollback_drill.py"
DRILL_DB_PATH = REPO_ROOT / "tools" / "ops" / "drill_db.py"
BACKUP_TOOL_PATH = REPO_ROOT / "tools" / "ops" / "backup_pg.py"
ENV_PY = REPO_ROOT / "migrations" / "env.py"

sys.path.insert(0, str(REPO_ROOT / "tools" / "ops"))
sys.path.insert(0, str(REPO_ROOT / "tools" / "quality"))

import check_rollback_drill as tool  # noqa: E402
import drill_db  # noqa: E402


# ===========================================================================
# 通用小工具：从**磁盘**抽判据名（不抄第二份名单，铁律 14）
# ===========================================================================


def _tool_source() -> str:
    return TOOL_PATH.read_text(encoding="utf-8")


def _tool_tree(src: str | None = None) -> ast.Module:
    return ast.parse(_tool_source() if src is None else src)


def _load_mutated(tmp_path: Path, old: str, new: str, tag: str) -> types.ModuleType:
    """把脚本源码改一个字后**独立加载**成模块（用于反向验证）。

    不碰仓库文件：写到 `tmp_path` 再 `exec_module`。被加载的模块里
    `REPO_ROOT = Path(__file__).parents[2]` 会算到一个不存在的路径，
    但它 `sys.path.insert(0, ...)` 的是**追加**项，`tools/ops` 早已由本文件
    挂在 `sys.path` 上 ⇒ `import drill_db` 仍解析得到。
    """
    import importlib.util
    import sys

    src = _tool_source()
    assert src.count(old) == 1, f"反向验证的锚点在源码里出现 {src.count(old)} 次：{old!r}"
    path = tmp_path / f"mut_{tag}.py"
    path.write_text(src.replace(old, new), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"mut_{tag}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # ⚠️ **必须先注册进 `sys.modules` 再 `exec_module`**（ISS-164 / 判据 F
    # `tools/quality/check_loader_registration.py` 的 F1 当场点名过这里）。
    # 不注册时 CPython 3.13 的 `dataclasses` 会 `sys.modules.get(cls.__module__)`
    # 拿到 `None` ⇒ `AttributeError: 'NoneType' object has no attribute '__dict__'`。
    # 被加载的 `check_rollback_drill.py` 当前无 `@dataclass`（实测 0 处）故今天
    # 不炸，但那是**运气不是防线**；且这里加载的是 `tmp_path` 里的**变异副本**，
    # 副本内容随 `old`/`new` 变化，将来更可能引入 dataclass。
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"脚本里找不到函数 {name}()")


def _undoc(fn: ast.FunctionDef) -> ast.FunctionDef:
    """去掉 docstring 后的**副本**。

    ⚠️ 本文件 2026-10-07 实测踩到两次同族假红：`ast.unparse(fn)` **会把 docstring
    一起吐出来**，于是在"源码里不许出现 X"这类断言上，**讲解这个坑的文案本身**
    成了唯一的 X。改为先摘掉 docstring 再 unparse。

    （另一次是 `_code_string_literals()` 那一处：同样是"文案里提到了该字面量"。）
    """
    import copy

    out = copy.deepcopy(fn)
    body = out.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        out.body = body[1:] or [ast.Pass()]
    return out


def _declared_check_names() -> set[str]:
    """从 AST 抽脚本里声明的判据名。

    ⚠️ 本脚本的写法与姊妹脚本不同：姊妹用 `checks.append(("名", ...))`，
    本脚本用闭包 `add("名", ok, detail)`。

    ⚠️ **必须用集合（去重）**：同一个判据名会在"无法构造"的兜底分支与
    真实分支里各出现一次（如 `reverse:fingerprint-detects-index-loss` 出现 2 次、
    `fail:obstacle-intact` 出现 3 次）。2026-10-07 首次对账时"31 次出现 / 23 个
    不同名 vs EXPECTED_CHECKS=22"看着像我自己数错，实际是**去重口径**没定。
    """

    def literal_str(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    names: set[str] = set()
    for node in ast.walk(_tool_tree()):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "add"):
            continue
        if not node.args:
            continue
        got = literal_str(node.args[0])
        if got is not None:
            names.add(got)
    return names


def _docstring_node_ids(tree: ast.Module) -> set[int]:
    """所有 docstring 的 `Constant` 节点 id（模块/类/函数体的第一条字符串语句）。"""
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            out.add(id(first.value))
    return out


def _code_string_literals(path: Path) -> list[str]:
    """文件里**作为代码**出现的字符串字面量（含注释/docstring 里的提及不算）。

    ⚠️ 不能用"源码里是否含这个子串"来判：本仓已实测踩到 —— 讲解这个坑的
    **docstring 本身**就写了那个路径，于是"字面量只出现在一处"的断言被
    文案判红（假红）。要从 `ast` 取**值**，docstring 先排除。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_node_ids(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
    ]


def _add_calls_with_literal(node: ast.AST, value: object) -> set[str]:
    """某个子树上 `add("名", <value>, ...)` 的名字集合（用于钉住兜底分支）。"""
    out: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn = sub.func
        if not (isinstance(fn, ast.Name) and fn.id == "add"):
            continue
        if len(sub.args) < 2:
            continue
        head, second = sub.args[0], sub.args[1]
        if not (isinstance(head, ast.Constant) and isinstance(head.value, str)):
            continue
        if isinstance(second, ast.Constant) and second.value is value:
            out.add(head.value)
    return out


# ===========================================================================
# 判据 1~2：`_table_exists()` 的 `bool(列表)` 恒真坑
# ===========================================================================


class _FakeCursor:
    def __init__(self, row: object) -> None:
        self._row = row

    def execute(self, *_a: object, **_k: object) -> "_FakeCursor":
        return self

    def fetchone(self) -> object:
        return self._row

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False


class _FakeConn:
    """只够 `_table_exists` 用：`connect(...)` → 上下文管理器 → `execute().fetchone()`。"""

    def __init__(self, row: object) -> None:
        self._row = row

    def execute(self, *_a: object, **_k: object) -> _FakeCursor:
        return _FakeCursor(self._row)

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False


@pytest.fixture
def fake_psycopg(monkeypatch: pytest.MonkeyPatch):
    """把 `psycopg` 换成返回**指定行**的假模块。

    `_table_exists()` 在函数内 `import psycopg` ⇒ 先命中 `sys.modules`，
    故这里替换模块即可，不需要真库、不需要 socket。
    """

    def _install(row: object) -> None:
        fake = types.ModuleType("psycopg")
        fake.connect = lambda *_a, **_k: _FakeConn(row)  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "psycopg", fake)

    return _install


@pytest.mark.regression
def test_table_exists_judges_null_not_row_list(fake_psycopg) -> None:
    """阴性对照：`to_regclass` 对不存在的表返回**一行 NULL** ⇒ 必须判 False。

    ⚠️ 这是本文件的**重点**。naive 写法 `bool(rows)` 在这里会返回 `True`
    （因为返回的是**长度 1** 的列表），而"表存在"恰好是幕 5①"部分 DDL 已回滚"
    唯一依据 ⇒ 判据会恒红（残留永远"存在"）或在这类空表上恒绿。
    两边都是"没在干活"。
    """
    # 前提：naive 写法拿到的 `fetchall()` 形态**truthiness 为真** ⇒ 坑真的存在
    assert bool([(None,)]) is True, "对照前提失效：[(None,)] 竟然为假"

    # ⚠️ 假游标要喂的是 `fetchone()` 的返回形态（**一行** `(None,)`），
    #    不是 `fetchall()` 的 `[(None,)]` —— 这两种喂法会得出相反结论，
    #    本文件首跑就因为喂错形态而假红（2026-10-07 实测）。
    fake_psycopg((None,))
    assert tool._table_exists("rednote_does_not_matter", "no_such_table") is False, (  # noqa: SLF001
        "`to_regclass` 返回一行 NULL 被判成'表存在' ⇒ 存在性判据恒真（ISS-115）"
    )


@pytest.mark.regression
def test_table_exists_judges_present_table_true(fake_psycopg) -> None:
    """阳性对照：真返回了 `public.<表>` 时必须判 True（否则反向变成恒假）。"""
    fake_psycopg(("public.users",))
    assert tool._table_exists("rednote_does_not_matter", "users") is True  # noqa: SLF001


@pytest.mark.regression
def test_table_exists_handles_zero_rows(fake_psycopg) -> None:
    """另一种"没有"的形态：零行（`fetchone()` 返回 `None`）也必须判 False。"""
    fake_psycopg(None)
    assert tool._table_exists("rednote_does_not_matter", "whatever") is False  # noqa: SLF001


@pytest.mark.regression
def test_table_exists_source_keeps_the_null_check() -> None:
    """源码层再钉一次：判据必须落在 `row[0] is not None` 上。

    上一条依赖假 `psycopg`；若有人把 `_table_exists` 改成先 `fetchall()`
    再 `bool(...)`（形态变了但仍错），源码这一条仍能抓到 ——
    "看起来像"与"确实是"必须各有一条。
    """
    body = ast.unparse(_undoc(_function(_tool_tree(), "_table_exists")))
    assert "row[0] is not None" in body, (
        "`_table_exists` 不再判 NULL ⇒ 存在性判据可能退化成 `bool(列表)` 恒真"
    )


# ===========================================================================
# 判据 3~4：迁移链解析（覆盖范围由 `glob` 决定，且 revision 与文件同源）
# ===========================================================================


@pytest.mark.regression
def test_parse_chain_is_linear_and_complete() -> None:
    """链必须单 head、线性、且 `rev_path` 覆盖每一个 revision。

    有分叉 ⇒ `downgrade -1` 的目标不确定 ⇒ 幕 3/4 的语义含糊，
    而判据**照样全绿**（它只走 `order[0]` 那条栈）。
    """
    down_of, order, rev_path = tool.parse_chain()
    assert down_of, "磁盘上没解析出任何迁移"
    assert len(order) == len(down_of), (
        f"从 head 走出的链长 {len(order)} ≠ revision 总数 {len(down_of)} ⇒ 有分叉或多 head"
    )
    assert set(rev_path) == set(down_of), "rev_path 没有覆盖全部 revision"
    assert len(order) >= tool.MIN_REVISIONS


@pytest.mark.regression
def test_parse_chain_revision_and_path_come_from_the_same_parse() -> None:
    """`rev_path[rev]` 必须是**真的声明了该 revision 的文件**。

    反例（被 docstring 点名禁止）：在外面按 `stem.endswith(f"_{rev}")` 猜文件名。
    那是手写规则，迁移文件一换命名风格就静默匹配不上 ⇒ 注入方案退化成 `None`。
    本条用**原始文本**独立复核：revision 字面量必须出现在它自己那个文件里。
    """
    _down_of, _order, rev_path = tool.parse_chain()
    for rev, path in rev_path.items():
        assert path.exists(), f"{rev} → {path} 不存在"
        assert rev in path.read_text(encoding="utf-8"), (
            f"{rev} 被指到了 {path.name}，但该文件里根本没有这个 revision 字面量"
            " ⇒ revision 与文件已经不同源"
        )


@pytest.mark.regression
def test_parse_chain_no_revision_has_two_children() -> None:
    """任何 revision 都不许被两个迁移当作 `down_revision`（分叉的直接特征）。"""
    down_of, _order, rev_path = tool.parse_chain()
    children: dict[str, int] = {}
    for rev, path in rev_path.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            got = tool._assign_targets(node)  # noqa: SLF001
            if got is None or got[0][0] != "down_revision":
                continue
            try:
                down = ast.literal_eval(got[1])
            except (ValueError, SyntaxError):
                continue
            for dep in ([down] if isinstance(down, str) else []):
                children[dep] = children.get(dep, 0) + 1
    forked = {r: n for r, n in children.items() if n > 1}
    assert not forked, f"迁移链分叉：{forked}（`downgrade -1` 的目标不再唯一）"
    assert set(children) <= set(down_of) | {None}


# ===========================================================================
# 判据 5~6：注入方案的推导（铁律 14：表名/约束名不写死）
# ===========================================================================


@pytest.mark.regression
def test_injection_is_derivable_for_the_two_revisions_the_drill_uses() -> None:
    """演练只用 head 与 head-1，这两个版本**必须**能推导出注入手段。

    ⚠️ 刻意**不**遍历全部版本断言"都能推"：链上确实存在既不加列、不建表、
    也不 drop check 的迁移（如"只加索引"的历史版本），断言"全部都能推"
    会造出一条与验收无关的红；断言错的对象同样是判据缺陷
    （判据纪律第 4 条：区分两种独立事实）。

    ⚠️ 2026-10-09 教训：`kind` 白名单**不能写死两种**。链上 head 换成
    "加列型"迁移后，`injection_for` 需要产出 `pre_create_column` /
    `pre_create_index` / `pre_create_constraint` 三种新手段 ——
    写死白名单会让这条断言在**正确的泛化**面前报红（忠实反映了
    "白名单与实际能力脱钩"，但脱钩的方向是判据太窄）。
    故这里改为断言"kind 落在脚本声明的手段集合里"，
    而那个集合本身在 `test_injection_kinds_match_apply_and_clear` 里被钉住。
    """
    _down_of, order, rev_path = tool.parse_chain()
    head, prev = order[0], order[1]
    for rev in (head, prev):
        inj = tool.injection_for(rev, rev_path)
        assert inj is not None, f"迁移 {rev} 推导不出注入手段 ⇒ 幕 5 会退化成'无法构造'"
        assert inj["kind"] in tool.INJECTION_KINDS, (
            f"{rev}: 注入 kind={inj['kind']!r} 不在脚本声明的手段集合 "
            f"{sorted(tool.INJECTION_KINDS)} 里"
        )
        assert rev_path[rev].name == inj["migration"], (
            "注入方案记的迁移文件名与链解析不一致"
        )

    # ---- 手段集合必须与 `_apply_injection` / `_clear_injection` 的分派一致 ----
    # 这三处是本仓典型的"一份口径写三遍"风险点：加一种手段时只改了
    # `injection_for` 而忘了 `_apply_injection` ⇒ 会**静默走兜底分支**
    # （注入没发生，幕 5 报"注入无效，upgrade 竟然成功了"）—— 比报错更坏。
    # 故用 AST 从两个函数里查每个 kind 的分支是否真的存在。
    #
    # ⚠️ `ast.unparse()` 用**单引号**渲染字符串常量 ⇒ 断言必须容忍两种引号形态
    # （2026-10-09 实测踩到：写死双引号会让这条断言恒假，红得毫无根据）。
    assert tool.INJECTION_KINDS, "手段集合为空 ⇒ 上面的白名单断言恒真"
    for fname in ("_apply_injection", "_clear_injection"):
        src = ast.unparse(_undoc(_function(_tool_tree(), fname)))
        for kind in tool.INJECTION_KINDS:
            assert re.search(rf"""["']{re.escape(kind)}["']""", src), (
                f"`{fname}()` 里没有对 kind={kind!r} 的分支 ⇒ 声明与实现脱钩"
            )


@pytest.mark.regression
def test_obstacle_is_the_last_created_object() -> None:
    """障碍必须是 `upgrade()` **最后**一个造出的对象（任何 kind）。

    挑中间那个 ⇒ 障碍之后的 op 一次都不会执行 ⇒ 观测不到"部分 DDL 被回滚"，
    而判据仍会绿（`prior` 里那些确实都回滚了）—— 只是**测的东西变少了**。

    ⚠️ 2026-10-09 泛化：旧版只认 `pre_create_table` 且只比表序列。
    现在任何 kind 都走同一套推导，故断言"障碍 == `steps[-1]`"。
    """
    _down_of, order, rev_path = tool.parse_chain()
    checked = 0
    for rev in order:
        inj = tool.injection_for(rev, rev_path)
        if inj is None:
            continue
        steps = inj.get("steps")
        idx = inj.get("obstacle_index")
        if inj["kind"] == "drop_owned_dependency" or not steps or idx is None:
            continue
        checked += 1
        obstacle = inj["obstacle"]
        assert idx == len(steps) - 1, (
            f"{rev}: 障碍下标 {idx} 不是最后一个（共 {len(steps)} 个）"
            " ⇒ 会少观测一部分'半成品被回滚'"
        )
        assert obstacle == steps[-1], f"{rev}: obstacle 与 steps[-1] 不一致"
        idents = [_ident_of(s) for s in steps]
        assert len(set(idents)) == len(idents), f"{rev}: 对象序列有重复 {idents}"
    assert checked >= 1, "没有任何迁移走 pre_create_* 路径 ⇒ 该分支未被覆盖"


def _ident_of(step: dict[str, str]) -> str:
    """与脚本里 `_ident()` 同口径的本地小工具（不 import 私有函数，避免耦合）。"""
    if step["kind"] == "table":
        return step["name"]
    return f"{step['table']}.{step['name']}"


@pytest.mark.regression
def test_created_tables_iterates_top_level_statements() -> None:
    """`_created_tables()` 的遍历必须按 `fn.body` 顶层语句走，**不能** `ast.walk(fn)`。

    `ast.walk` 是广度优先 ⇒ 对嵌套在 `if/for` 里的语句不保证源码序 ⇒
    "取最后一个"会取错，且错得**不报错**（只是观测变少）。

    ⚠️ 2026-10-09：遍历逻辑已上移到 `_upgrade_steps()`（`_created_tables`
    成为它的薄封装）。断言随之指向**真正做遍历的那个函数**。
    """
    body = ast.unparse(_undoc(_function(_tool_tree(), "_upgrade_steps")))
    assert "fn.body" in body, "不再按顶层语句遍历 ⇒ 对象顺序失去源码序保证"
    assert "ast.walk(fn)" not in body, (
        "用 `ast.walk(fn)` 收集对象 ⇒ 广度优先、非源码序"
    )


@pytest.mark.regression
def test_lower_bounds_are_non_zero() -> None:
    """四个下界都必须是正数：0 会让对应断言退化成恒真式（ISS-115）。"""
    assert tool.MIN_PRIOR_OBJECTS >= 2, (
        f"MIN_PRIOR_OBJECTS={tool.MIN_PRIOR_OBJECTS}：为 0 时 `not leftover` 恒真 "
        "⇒ '部分 DDL 已回滚'变成恒真式"
    )
    assert tool.MIN_HEAD_OBJECTS >= 1, (
        "MIN_HEAD_OBJECTS=0 ⇒ 幕 3 的 `not after_present` 在 head 不造对象时恒真"
    )
    assert tool.MIN_OBSERVED_OBJECTS >= 1, (
        "MIN_OBSERVED_OBJECTS=0 ⇒ 幕 5②/幕 6 的'全在'与'全丢'同形"
    )
    assert tool.MIN_TABLES > 0
    assert tool.MIN_REVISIONS >= 3, "`downgrade -2` 需要链上至少 3 个版本"
    assert tool.ROLLBACK_BUDGET_SECONDS > 0


# ===========================================================================
# 判据 6b：`drop_owned_dependency` 的效果谓词
# ===========================================================================
#
# ⚠️ **为什么这一组必须靠单测守**（2026-10-09 反向验证实测）：
# `main()` 里两条注入**当前都是** `pre_create_constraint`（head 与 head-1 都是
# 加列型迁移）⇒ `_obstacle_intact()` / `_obstacle_step()` 的
# `drop_owned_dependency` 分支在**判据层一次都不会执行**。把它改成恒真、
# 或者把 `_obstacle_step()` 的 drop 分支改成恒 `None`，`main()` 照样
# 24/24 全绿（变异 M5c / M7 实测）—— 这是"判据覆盖不到自己那条分支"的盲区，
# 在判据层写多少断言都补不上，只能下沉到纯函数单测。


@pytest.mark.regression
def test_rebuild_migration_falls_back_to_drop_tier_not_pre_create() -> None:
    """**重建同名对象**的迁移必须回落 tier-2，不得被 tier-1 当"新增对象"预建。

    ## 这条守的是什么（2026-10-09 **干净检出**实测暴露的缺陷）

    tier-1 的手法是"**预建**它要造的对象 ⇒ `create_*` 撞 `DuplicateObject`"，
    前提是那个对象**迁移前不存在**。而 `e8f07ba8f2bb`（WP27 加 `STATIC_IMAGE`）
    是**重建型**：先 `drop_constraint` 掉 `ck_prompt_templates_task_kind_valid`，
    再用**同一个名字** create 一个新取值域。旧版 `_upgrade_steps()` 不认
    `drop_*` ⇒ 把那条 CHECK 当"新增对象" ⇒ 预建桩**撞上迁移前本来就存在的那条**
    ⇒ 在**注入阶段** `DuplicateObject` 崩，整支脚本异常退出。

    ## 为什么这条缺陷在共享工作树上**看不见**（铁律 18 的形态）

    干净检出的链头是 `df6bb2f41f84`、head-1 恰好是 `e8f07ba8f2bb`；
    而当时共享工作树的链头是两个**纯加列型**迁移（`c4d9f2a17e35` / `b7c1e4a9d203`）
    ⇒ tier-1 挑中的都是有真正新名字的对象，撞不上。
    **同一份判据在两棵树上一绿一红，而绿的那棵恰好不是 CI 口径。**

    ## 因此本用例断言两件事

    ① **对每个"可推导"的迁移**，tier-1 挑出的障碍名都不得 ∈ 它自己 drop 掉的名字
       集合 —— 这是"预建不会撞上旧定义"的充分条件，且是**结构性**的（不依赖某个
       具体 revision，故新增/替换迁移都不会让它失效）；
    ② **活性**：整条链上至少各有一个迁移走 tier-1、一个走 tier-2（否则本用例要么
       没验到东西、要么说明 tier-2 已成死路径）。

    ⚠️ **不断言"所有迁移都可推"** —— 本仓本来就有推不出的迁移（纯触发器 DDL，
    如 `515fb8b0d31a`），判据也只对 `order[0]` / `order[1]`（演练实际用的两条）
    要求可推（那条由 `test_injection_is_derivable_for_the_two_revisions_the_drill_uses`
    单独守着）。此处越界断言会把"判据不需要的性质"变成红。

    ⚠️ 反向验证（改回不过滤 `drop_*`）时 `e8f07ba8f2bb` 会拿到
    `pre_create_constraint`，本用例第 ① 条即在它身上报红。
    """
    _down_of, order, rev_path = tool.parse_chain()
    assert len(order) >= 3, "链太短，本用例的语义（head-1 可能是重建型）不成立"
    n_pre = 0
    n_drop = 0
    for rev in order:
        path = rev_path[rev]
        inj = tool.injection_for(rev, rev_path)
        if inj is None:
            continue  # 推不出属既有事实，由另一条用例守住"演练用的那两条"
        if inj["kind"] == "drop_owned_dependency":
            n_drop += 1
            continue  # 回落 tier-2 是本用例**期望**的结果（重建型迁移就该走这里）
        n_pre += 1
        tree = ast.parse(path.read_text(encoding="utf-8"))
        dropped = tool._dropped_identifiers(tree)  # noqa: SLF001
        obstacle = inj["obstacle"]
        assert isinstance(obstacle, dict)
        key = (obstacle["table"], obstacle["name"])
        assert key not in dropped, (
            f"{rev}（{path.name}）：tier-1 挑中的桩 {tool._ident(obstacle)} 在本迁移的 "
            f"drop 集合里 {sorted(dropped)} ⇒ 该对象迁移前本来就存在，"
            "预建会撞上旧定义、在**注入阶段**抛 DuplicateObject（整个判据异常退出，"
            "而不是某条断言红）"
        )
    assert n_pre >= 1, "链上没有一个迁移走 tier-1 ⇒ 该路径未被覆盖"
    assert n_drop >= 1, "链上没有一个迁移走 tier-2 ⇒ 该路径未被覆盖"


@pytest.mark.regression
def test_obstacle_step_rebuilds_the_dropped_constraint() -> None:
    """`drop_owned_dependency` 没有 `obstacle` 键 ⇒ `_obstacle_step()` 必须重建它。

    该手段的 `injection_for()` 返回值里 `obstacle_index=None`、`steps=[]`、
    没有 `obstacle` 字段（它不做 pre_create，而是**删掉**迁移自己要 DROP 的
    CHECK）。故"障碍对象"只能从 `table` / `name` 重建。
    返回 `None` ⇒ `_obstacle_intact()` 走 fail-closed 分支 ⇒ 该手段永判红，
    而它**在判据层不可达** ⇒ 只能在这里看见。
    """
    inj: dict[str, object] = {
        "kind": "drop_owned_dependency",
        "constraint": "ck_demo_valid",
        "table": "demo_table",
        "name": "ck_demo_valid",
        "obstacle_index": None,
        "steps": [],
        "objects": [],
        "prior_objects": [],
    }
    step = tool._obstacle_step(inj)  # noqa: SLF001
    assert step is not None, (
        "drop_owned_dependency 的障碍推不出来 ⇒ `_obstacle_intact()` 恒 fail-closed"
    )
    assert step == {"kind": "constraint", "table": "demo_table", "name": "ck_demo_valid"}
    # `pre_create_*` 那边必须**原样透传** `obstacle`（不要多此一举重建）
    pre: dict[str, object] = {
        "kind": "pre_create_constraint",
        "obstacle": {"kind": "constraint", "table": "t", "name": "ck_t"},
    }
    assert tool._obstacle_step(pre) == {"kind": "constraint", "table": "t", "name": "ck_t"}  # noqa: SLF001


@pytest.mark.regression
def test_obstacle_intact_drop_branch_follows_state(fake_psycopg) -> None:
    """`drop_owned_dependency` 的效果 = "被我删的对象**仍然不在**"。

    ⚠️ 旧版这里写的是 `return True, "无桩对象"` —— **恒真**。合成后的整个谓词
    于是只有"必须为真"一个使用点 ⇒ 桩由 `_apply_injection()` 以 autocommit 建在
    Alembic 事务之外、而失败正是撞它造成的，"桩还在"是结构性必然
    （反向验证 M5 实测：改成 `return True` 后 24/24 全绿）。
    现在该分支依状态取值 ⇒ 必须**两个方向都验**（阳性 + 阴性），只验一边会
    漏掉"反向写死成恒假"这种对称失效（RG-145 同款纪律）。
    """
    inj: dict[str, object] = {
        "kind": "drop_owned_dependency",
        "constraint": "ck_demo_valid",
        "table": "demo_table",
        "name": "ck_demo_valid",
        "obstacle_index": None,
        "steps": [],
    }

    # 阴性：对象被建回来了（存在）⇒ 注入已被抹平 ⇒ 效果**不**成立
    fake_psycopg((1,))
    intact, detail = tool._obstacle_intact("rednote_does_not_matter", inj)  # noqa: SLF001
    assert intact is False, f"对象还在却判'注入仍生效' ⇒ 该分支恒真；detail={detail}"

    # 阳性：对象确实不在 ⇒ 效果成立
    fake_psycopg(None)
    intact, detail = tool._obstacle_intact("rednote_does_not_matter", inj)  # noqa: SLF001
    assert intact is True, f"对象已不在却判'注入被抹平' ⇒ 该分支恒假；detail={detail}"


@pytest.mark.regression
def test_obstacle_intact_is_fail_closed_when_step_underivable(fake_psycopg) -> None:
    """障碍推不出来 ⇒ 必须判 `False`（fail-closed），不许静默 True。

    「声称有防线而防线是虚的」比没有防线更危险：若这里返回 True，
    `drop_owned_dependency` 一旦将来被走到，就会给出一条**永远为真**的
    `fail:obstacle-intact` —— 与 2026-10-09 那次 M5 假绿同形。
    """
    fake_psycopg(None)
    inj: dict[str, object] = {"kind": "drop_owned_dependency", "table": "", "name": ""}
    intact, detail = tool._obstacle_intact("rednote_does_not_matter", inj)  # noqa: SLF001
    assert intact is False, f"推不出障碍却判完好 ⇒ 恒真式；detail={detail}"
    assert "无法自证" in detail


# ===========================================================================
# 判据 7~9：条数、全等断言、反向验证不许被删
# ===========================================================================


@pytest.mark.regression
def test_check_name_count_matches_expected_checks() -> None:
    """磁盘上声明的**不同**判据名数必须 == `EXPECTED_CHECKS`。

    `main()` 用**全等**（不是 `>=`）核对条数（ISS-153：`>=` 只防变少）。
    但"常量与真实声明"仍可能脱钩，本条让脱钩在 pytest 阶段就红。
    """
    names = _declared_check_names()
    assert len(names) == tool.EXPECTED_CHECKS, (
        f"脚本里声明 {len(names)} 个不同判据名，EXPECTED_CHECKS={tool.EXPECTED_CHECKS}；"
        f"声明={sorted(names)}"
    )


def _report_guard_test(src: str | None = None) -> str:
    """抽 `_report()` 里那个拿 `EXPECTED_CHECKS` 做判断的 `if` 的**谓词文本**。

    ⚠️ 必须精确到"那个 if 的谓词"，**不能**退化成"整段函数里出现过这个字符串"：
    `_report` 的 `return` 里同样写了 `total != EXPECTED_CHECKS`，
    于是"源码含该串"在把 `if` 改成 `<` 之后**照样成立** —— 2026-10-07
    反向验证当场抓出这条假绿（见 `test_reverse_verification_full_equality_...`）。
    又一次印证 ISS-110：**绿本身没有信息，绿的灵敏度才有**。
    """
    fn = _undoc(_function(_tool_tree(src), "_report"))
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and "EXPECTED_CHECKS" in ast.unparse(node.test):
            return ast.unparse(node.test)
    raise AssertionError("`_report()` 里找不到拿 EXPECTED_CHECKS 做判断的 if")


@pytest.mark.regression
def test_report_asserts_full_equality_not_at_least() -> None:
    """`_report()` 的条数守卫必须是 `!=` 全等，不是 `<` / `<=`。

    写成 `total < EXPECTED_CHECKS` ⇒ 多出来的判据（可能是误加的重复条）
    不会被发现。ISS-153 的原话就是"`>=` 只防变少不防变多"。
    """
    guard = _report_guard_test()
    assert guard == "total != EXPECTED_CHECKS", (
        f"条数守卫的谓词是 `{guard}`，不是全等 ⇒ 判据条数可能悄悄变多而无人发现"
    )


@pytest.mark.regression
def test_reverse_verification_checks_are_still_declared() -> None:
    """四条 `reverse:*` 必须在列 —— 删了它们判据仍会全绿但已失去信息。

    ISS-110：**"判据变绿了"必须伴随一次注入破坏的反向验证**，
    否则分不清"修好了"和"因为瞎所以绿"。
    条数断言挡得住"删两条"，但挡不住"删两条再补两条无关的" ⇒ 本条按名点名。
    """
    names = _declared_check_names()
    for required in (
        # 幕 2：指纹对"索引丢失"灵敏
        "reverse:fingerprint-detects-index-loss",
        "reverse:index-restored-exactly",
        # 幕 6：翻转 `transaction_per_migration` 后落点必须不同
        "reverse:tpm-false-lands-at-run-start",
        "reverse:tpm-off-discards-committed-migration",
    ):
        assert required in names, (
            f"反向验证 {required} 不在声明里 —— 演练会退回成无法自证灵敏的恒真式"
        )


@pytest.mark.regression
def test_every_declared_name_is_a_real_check_prefix() -> None:
    """判据名必须带已知前缀（静态/建库/回滚/拒绝/失败/粒度/反向/证据）。

    防的是"名字拼错导致的重复声明"：`rollback:` 写成 `rollbak:` 时，
    集合里会多出一个名字、少掉一个，而**总数可能不变**（一个拼错 + 一个补上）。
    """
    allowed = (
        "static:", "build:", "rollback:", "refuse:",
        "fail:", "granularity:", "reverse:", "evidence:",
    )
    bad = sorted(n for n in _declared_check_names() if not n.startswith(allowed))
    assert not bad, f"判据名不在已知前缀里：{bad}"


# ===========================================================================
# 判据 10：`transaction_per_migration` 的前提（声明侧）
# ===========================================================================


@pytest.mark.regression
def test_transaction_per_migration_is_declared_in_the_repo_env_py() -> None:
    """仓库 `migrations/env.py` 必须声明 `transaction_per_migration=True`。

    这是幕 5② 的**语义前提**：它决定"升级跑到一半失败"停在**起点**
    还是停在**上一条已提交的迁移**。声明没了 ⇒ 幕 5② 的断言会变成
    在错误前提下的绿（ISS-95：声称有防线而防线是虚的）。
    """
    src = ENV_PY.read_text(encoding="utf-8")
    n = src.count(tool._TPM_LITERAL)  # noqa: SLF001
    assert n == 1, (
        f"`{tool._TPM_LITERAL}` 在 env.py 里出现 {n} 次（应为 1）"
        " —— 幕 6 的沙箱翻转要求目标行唯一"
    )
    assert tool.declares_transaction_per_migration() is True


# ===========================================================================
# 判据 11：幕 6 的沙箱翻转（唯一性前提 + 不动仓库文件）
# ===========================================================================


def _fake_migrations_root(tmp: Path, env_text: str) -> Path:
    """造一棵最小的 `migrations/` 树，供 `_stage_migrations` 复制。"""
    root = tmp / "fake_migrations"
    (root / "versions").mkdir(parents=True)
    (root / "versions" / "v1_x.py").write_text(
        "revision = 'abc'\ndown_revision = None\n", encoding="utf-8"
    )
    (root / "alembic.ini").write_text("[alembic]\nscript_location = %(here)s\n",
                                      encoding="utf-8")
    (root / "env.py").write_text(env_text, encoding="utf-8")
    return root


@pytest.mark.regression
def test_stage_migrations_flips_exactly_one_literal(tmp_path, monkeypatch) -> None:
    """翻转必须**生效且只改那一处**，且**不许碰仓库文件**。

    退化成"没翻到就照原样跑"⇒ 幕 6 两臂是同一个臂 ⇒ 反向验证恒真。
    """
    root = _fake_migrations_root(
        tmp_path, f"x = 1\n{tool._TPM_LITERAL}\ny = 2\n"  # noqa: SLF001
    )
    monkeypatch.setattr(tool, "MIGRATIONS_DIR", root / "versions")

    repo_env_before = ENV_PY.read_text(encoding="utf-8")

    _ini, env_copy = tool._stage_migrations(tmp_path / "sb1", flip=True)
    got = env_copy.read_text(encoding="utf-8")
    assert tool._TPM_FLIPPED in got, "翻了但没翻到目标行"
    assert tool._TPM_LITERAL not in got, "原文仍在 ⇒ 两臂会变成同一个臂"

    # flip=False 时必须是**逐字节原样**（免得"顺手"改了别的）
    _ini2, env_copy2 = tool._stage_migrations(tmp_path / "sb2", flip=False)
    assert env_copy2.read_text(encoding="utf-8") == f"x = 1\n{tool._TPM_LITERAL}\ny = 2\n"  # noqa: SLF001

    # 仓库文件一个字节都不许变
    assert ENV_PY.read_text(encoding="utf-8") == repo_env_before, (
        "沙箱翻转改到了仓库里的 env.py —— 幕 6 必须是无副作用的"
    )


@pytest.mark.regression
def test_stage_migrations_refuses_when_the_literal_is_not_unique(tmp_path, monkeypatch) -> None:
    """目标行找不到 / 找到多处 ⇒ **必须抛错**，不许静默照原样跑。

    这是幕 6 唯一能自证"两臂确实不同"的地方：若它退化成"没翻到就继续"，
    两臂会跑出同样的结果，而反向验证那两条会**报红**——
    总比静默恒真好，但那时要查的是这段代码，不是迁移。
    """
    for bad_text, why in (
        ("x = 1\n", "完全没有那一行"),
        (f"{tool._TPM_LITERAL}\n{tool._TPM_LITERAL}\n", "那一行出现两次"),  # noqa: SLF001
    ):
        root = _fake_migrations_root(tmp_path / why.replace(" ", "_"), bad_text)
        monkeypatch.setattr(tool, "MIGRATIONS_DIR", root / "versions")
        with pytest.raises(RuntimeError):
            tool._stage_migrations(tmp_path / f"sb_{why}", flip=True)


# ===========================================================================
# 判据 12：「推不出来就报 FAIL，不跳过」（ISS-143）
# ===========================================================================


@pytest.mark.regression
def test_underivable_injection_reports_named_failures_not_skips() -> None:
    """两个 `is None` 兜底分支必须**按名**声明那 6 条为 `False`。

    若被改成 `pass` / `continue`，判据条数会少 6 条（被 `EXPECTED_CHECKS`
    全等挡住）—— 但"删 6 条再补 6 条无关的"能绕过去，故这里按名点名。
    ISS-143：**「没跑」与「跑失败」同形**，报 FAIL 才区分得开。
    """
    main = _function(_tool_tree(), "main")
    branches: dict[str, set[str]] = {}
    for node in ast.walk(main):
        if not isinstance(node, ast.If):
            continue
        src = ast.unparse(node.test)
        if "inj_first is None" in src:
            branches["first"] = _add_calls_with_literal(node, False)
        if "inj_last is None" in src:
            branches["last"] = _add_calls_with_literal(node, False)

    assert set(branches) == {"first", "last"}, (
        f"没找到两个 `is None` 兜底分支（找到 {sorted(branches)}）"
        " ⇒ 它们可能已被改成跳过"
    )
    assert branches["first"] == {
        "fail:upgrade-rc-nonzero",
        "fail:version-unchanged-when-first-migration-fails",
        "fail:partial-ddl-rolled-back",
        "fail:obstacle-intact",
    }, f"幕 5① 兜底分支的判据名单变了：{sorted(branches['first'])}"
    assert branches["last"] == {
        "granularity:version-advances-past-committed-migration",
        "granularity:committed-migration-objects-survive",
    }, f"幕 5② 兜底分支的判据名单变了：{sorted(branches['last'])}"


@pytest.mark.regression
def test_all_declared_names_are_reachable_in_main() -> None:
    """所有声明的判据名都必须真的在 `main()` 里出现。

    防"声明了但忘了调用"：那种缺失会让条数变少（被全等断言挡住），
    但本条给出更直接的定位 —— 直接把缺的名字打出来。
    """
    main_src = ast.unparse(_undoc(_function(_tool_tree(), "main")))
    missing = sorted(n for n in _declared_check_names() if n not in main_src)
    assert not missing, f"这些判据名声明了但没在 main() 里出现：{missing}"


# ===========================================================================
# 判据 13~15：DSN 方言 / 环境口径 / pg 工具路径（ISS-174 与 CI 角色同族）
# ===========================================================================


@pytest.mark.regression
def test_dsn_dialects_are_separated() -> None:
    """libpq 口径与 SQLAlchemy 口径必须是两个函数，不能混。

    ISS-174 同族：`migrations/env.py::_resolve_database_url` **强制要求**
    `postgresql+psycopg://`，而 libpq 系列（`pg_dump`/`pg_restore`/`psycopg.connect`）
    **不认**这个前缀 —— 喂错了会被 `except` 吞成"连不上库" ⇒ 判据降级成假绿。
    """
    assert drill_db.dsn("x").startswith("postgresql://")
    assert not drill_db.dsn("x").startswith("postgresql+psycopg://"), (
        "libpq 口径带上了 SQLAlchemy 方言后缀 ⇒ 被喂给 psycopg 时必错"
    )
    assert drill_db.dsn_alembic("x").startswith("postgresql+psycopg://"), (
        "alembic 口径丢了方言后缀 ⇒ env.py 当场拒收"
    )


@pytest.mark.regression
def test_base_dsn_follows_environment(monkeypatch) -> None:
    """基准 DSN 必须从环境变量派生，**不许自己拼本机角色**。

    ⚠️ 这是 CI 门 3 的一条硬前提（2026-10-07 抓出）：首版 `dsn()` 自己拼
    `rednote@localhost:5432`，本机跑得通、CI 上必红在 `role "rednote" does not exist`。
    那种红与"回滚路径坏了"毫无关系，是最容易误导人的一种红（ISS-108 原样复发）。
    """
    monkeypatch.setenv(
        "REDNOTE_TEST_DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@dbhost:5999/base",
    )
    got = drill_db.dsn("rednote_x_1")
    assert got == "postgresql://postgres:postgres@dbhost:5999/rednote_x_1", (
        f"没有只换库名而是重拼了 DSN：{got}"
    )
    assert "rednote" not in got, "本机角色泄漏进 DSN ⇒ CI 上会红在 role 不存在"

    # 只设 REDNOTE_DATABASE_URL 时也要认（与 `check_migration_model_parity.py` 同口径）
    monkeypatch.delenv("REDNOTE_TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "REDNOTE_DATABASE_URL", "postgresql+psycopg://u:p@other:1/db"
    )
    assert drill_db.dsn("zzz") == "postgresql://u:p@other:1/zzz"


@pytest.mark.regression
def test_pg_bin_prefers_env_then_which_then_homebrew(monkeypatch) -> None:
    """`pg_bin()` 三级回退：`REDNOTE_PG_BIN` → `which pg_dump` → Homebrew。

    写死 Homebrew ⇒ CI（客户端来自 apt）上 `pg_dump: not found`
    —— 与上面那条是同一族：**本机口径被当成通用口径**。
    """
    monkeypatch.setenv("REDNOTE_PG_BIN", "/opt/fake/bin")
    assert drill_db.pg_bin() == "/opt/fake/bin"

    monkeypatch.delenv("REDNOTE_PG_BIN", raising=False)
    monkeypatch.setattr(drill_db.shutil, "which", lambda _n: "/found/bin/pg_dump")
    assert drill_db.pg_bin() == "/found/bin"

    monkeypatch.setattr(drill_db.shutil, "which", lambda _n: None)
    assert drill_db.pg_bin() == drill_db.DEFAULT_PG_BIN


@pytest.mark.regression
def test_backup_tool_delegates_pg_bin_instead_of_hardcoding() -> None:
    """`backup_pg` 不许自己再写一份路径解析（回退知识只允许存在一份）。

    两处各写一份 ⇒ 将来改一处忘另一处，另一处**不报错**，只在 CI 上 not found。
    """
    src = BACKUP_TOOL_PATH.read_text(encoding="utf-8")
    assert "def _pg_bin()" in src, "`backup_pg` 不再有 `_pg_bin()`"
    assert "drill_db.pg_bin()" in src, (
        "`backup_pg._pg_bin()` 没有委托给 `drill_db.pg_bin()` ⇒ 路径回退出现第二份实现"
    )
    body = ast.unparse(_undoc(_function(ast.parse(src), "_pg_bin")))
    assert "DEFAULT_PG_BIN" not in body, "又把 Homebrew 常量抄回来了"


# ===========================================================================
# 判据 16：反向验证 —— 把判据改坏，它们**必须**报红（ISS-110）
# ===========================================================================
#
# 上面 25 条全绿这件事本身**没有信息**：绿只说明"在现在的源码下没报错"。
# ISS-110：**"判据变绿了"必须伴随一次注入破坏的反向验证** ——
# 否则分不清"真的在干活"和"因为瞎所以绿"。
#
# 这里对四条最关键的断言做注入破坏：每条都**只改一个字**，
# 断言"那条判据的谓词确实翻转了"。谓词没翻转 ⇒ 那条判据是恒真式。


@pytest.mark.regression
def test_reverse_verification_table_exists_predicate_flips(tmp_path, fake_psycopg) -> None:
    """破坏：`row[0] is not None` → `row[0:1]`（回到 naive 的"非空即存在"）。

    这条是**行为级**的反向验证，不是文案级：同一个假游标喂给原版与破坏版，
    断言两者的结论**相反** —— 原版 False、破坏版 True。
    结论没翻转 ⇒ 上面那条阴性对照是恒真式（ISS-110 要抓的正是这个）。
    """
    flipped = _load_mutated(
        tmp_path,
        "return bool(row and row[0] is not None)",
        "return bool(row and row[0:1])",
        "table_exists",
    )
    fake_psycopg((None,))          # `to_regclass` 对不存在的表返回一行 NULL
    assert tool._table_exists("rednote_x", "no_such") is False  # noqa: SLF001
    assert flipped._table_exists("rednote_x", "no_such") is True, (  # noqa: SLF001
        "把 NULL 判定改成 `row[0:1]` 之后结论没变 ⇒ 那条阴性对照没有区分力"
    )


@pytest.mark.regression
def test_reverse_verification_created_tables_order_predicate_flips() -> None:
    """破坏：顶层语句遍历 → `ast.walk(fn)`（广度优先，非源码序）。"""
    mutated = _tool_source().replace(
        "for stmt in fn.body:  # 顶层语句：源码序",
        "for stmt in ast.walk(fn):",
    )
    assert mutated != _tool_source(), "锚点没匹配上：`_upgrade_steps` 的遍历语句变了"
    body = ast.unparse(_undoc(_function(_tool_tree(mutated), "_upgrade_steps")))
    assert "fn.body" not in body, "破坏后判据仍能通过 ⇒ `fn.body` 断言是摆设"
    assert "ast.walk(fn)" in body


@pytest.mark.regression
def test_reverse_verification_full_equality_predicate_flips() -> None:
    """破坏：`total != EXPECTED_CHECKS` → `total < EXPECTED_CHECKS`（只防变少）。"""
    assert _report_guard_test() == "total != EXPECTED_CHECKS", "对照前提失效"
    mutated = _tool_source().replace(
        "if total != EXPECTED_CHECKS:", "if total < EXPECTED_CHECKS:"
    )
    assert mutated != _tool_source(), "锚点没匹配上：`_report` 的全等断言变了"
    assert _report_guard_test(mutated) == "total < EXPECTED_CHECKS", (
        "破坏成 `<` 之后谓词没变 ⇒ ISS-153 那条断言是摆设"
    )
    # 顺带钉住"为什么不能只查整段源码里有没有那个字符串"：
    # `return` 那句里同样写了全等形式，纯字符串断言在破坏后**照样通过**。
    assert "total != EXPECTED_CHECKS" in ast.unparse(
        _undoc(_function(_tool_tree(mutated), "_report"))
    ), "对照前提失效：破坏后整段源码里已不含该串（说明锚点选错了）"


@pytest.mark.regression
def test_reverse_verification_lower_bound_predicate_flips(tmp_path) -> None:
    """破坏：`MIN_PRIOR_OBJECTS = 2` → `= 0`（下界为 0 ⇒ 恒真式）。"""
    mutated = _load_mutated(tmp_path, "MIN_PRIOR_OBJECTS = 2", "MIN_PRIOR_OBJECTS = 0",
                            "lower_bound")
    assert tool.MIN_PRIOR_OBJECTS >= 2, "对照前提失效：原值已不足下界"
    assert not (mutated.MIN_PRIOR_OBJECTS >= 2), (
        "把下界改成 0 之后 `>= 2` 仍成立 ⇒ 那条判断没有区分力"
    )


@pytest.mark.regression
def test_reverse_verification_stage_flip_guard_predicate_flips(tmp_path, monkeypatch) -> None:
    """破坏：`_stage_migrations` 的唯一性守卫 → 静默照原样跑。

    这是幕 6 的**唯一**自证入口：守卫没了，两臂会跑出同样的结果，
    而反向验证那两条会报红（比恒真好，但那时该查的是这段代码）。
    """
    mutated = _load_mutated(
        tmp_path,
        "if src.count(_TPM_LITERAL) != 1:",
        "if False:",
        "stage_guard",
    )
    root = tmp_path / "fake_migrations"
    (root / "versions").mkdir(parents=True)
    (root / "versions" / "v1.py").write_text("revision = 'a'\n", encoding="utf-8")
    (root / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    (root / "env.py").write_text("x = 1\n", encoding="utf-8")   # 完全没有那一行
    monkeypatch.setattr(tool, "MIGRATIONS_DIR", root / "versions")
    mutated.MIGRATIONS_DIR = root / "versions"

    with pytest.raises(RuntimeError):
        tool._stage_migrations(tmp_path / "sb_ok", flip=True)  # noqa: SLF001
    # 守卫被拆掉后**不再抛错** —— 证明这条守卫真的在挡事，不是装饰
    mutated._stage_migrations(tmp_path / "sb_bad", flip=True)  # noqa: SLF001


@pytest.mark.regression
def test_drill_db_module_has_no_second_hardcoded_path_resolution() -> None:
    """反向兜底：全仓只允许 `drill_db.DEFAULT_PG_BIN` 一处出现该字面量。

    名字像缓存的判定不适用于此，这里是**唯一事实来源**的反向检查：
    字面量出现次数 > 1 ⇒ 有人又抄了一份。
    """
    literal = drill_db.DEFAULT_PG_BIN
    hits: list[str] = []
    for path in sorted((REPO_ROOT / "tools").rglob("*.py")):
        if literal in _code_string_literals(path):
            hits.append(str(path.relative_to(REPO_ROOT)))
    assert hits == [str(DRILL_DB_PATH.relative_to(REPO_ROOT))], (
        f"pg 工具路径字面量作为代码常量出现在多处：{hits} ⇒ 回退知识被复制了"
    )
