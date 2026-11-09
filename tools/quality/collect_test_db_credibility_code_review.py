#!/usr/bin/env python
"""WP-DEBT-02 代码审查证据（三遍法：结构 → 逐行 → 边界与加固）。

产出 `evidence/m3/test-db-credibility-code-review.log`。

**为什么要把审查结果落成文件**：审查结论是"声称"，而声称需要证据。
本项目已踩过"文档/看板是唯一声称方时没有任何机械判据与它对账"
（ISS-78/84/85/86/94/95），因此本记录里每一处「通过」都必须附上
**能在代码里当场指认的判据**（判据纪律第 9 条：文档说"这条有判据"，
必须能当场指出脚本里哪个函数）。

## 本包只有三个源文件（+两个判据自身的判据）

| 文件 | 角色 |
|---|---|
| `tools/quality/check_test_database_target.py` | 判据 A 本体（默认 DSN 目标） |
| `tools/quality/check_test_residue.py` | 判据 B 本体（测试残留） |
| `tools/quality/check_migration_model_parity.py` | 判据 C 本体（迁移↔模型对账） |
| `services/backend/tests/test_test_database_target.py` | A 自身的判据 |
| `services/backend/tests/test_migration_model_parity.py` | C 自身的判据 |
| `services/backend/tests/test_test_residue.py` | B 自身的判据 |

## 审查方式

按 `code-review` 技能的三遍法（结构 → 逐行 → 边界与加固），
五维度（Security / Performance / Correctness / Maintainability / Testing）。

## ⚠️ 本记录里最该记住的一条

**第一遍读代码时发现的第一条缺陷，是"判据自己"的**（不是它检查的对象）：
本包三条判据的初版各有一处「判据自身缺陷」，且**都在它抓别人之前先在自己身上出现**：

  ① 判据 A：`DEFAULT_PYTEST_ARGS` 用 `str.split()` 拆坏 `-m "not slow"`
     → pytest 拿 `slow` 当路径 → **退出码 4** → 「没跑」伪装成「跑过且无残留」
     （ISS-143）。
  ② 判据 B：差分**看不到"测试一条没跑"与"跑过且无残留"的区别** ——
     两者都是"没多出东西"。因此必须另加"只有真跑过才满足"的前置条件。
  ③ 判据 C：排除集初版**写死了索引名** `ix_chunk_embeddings_embedding_hnsw`，
     而该名字**在磁盘上根本不存在**（真名带 profile uuid 后缀）——
     铁律 14 第 7 次现身。
  ④ **文档层**（本脚本新发现）：三条判据各自的 docstring 指向的反向验证
     脚本名有 **3 处与磁盘不符**（写成了三个不同的、都不存在的文件名），
     且变异清单表 7 条有 **4 条与实现不符**。引用的路径写错，
     与「没有引用」在效果上同形。

前三条是"判据自身缺陷"，第四条是"文档自身缺陷"——**都在抓别人之前先自己现形**。
这正是本项目判据纪律的核心：防线要能被反向验证，包括防线自己，包括它的说明书。

跑法：

    python tools/quality/collect_test_db_credibility_code_review.py

退出码 0 = 全部 CHECK 通过。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"
OUT_FILE = EVIDENCE_DIR / "test-db-credibility-code-review.log"

TOOL_A = REPO_ROOT / "tools" / "quality" / "check_test_database_target.py"
TOOL_B = REPO_ROOT / "tools" / "quality" / "check_test_residue.py"
TOOL_C = REPO_ROOT / "tools" / "quality" / "check_migration_model_parity.py"
REVERSE = REPO_ROOT / "tools" / "quality" / "collect_test_db_credibility_reverse_evidence.py"

TEST_A = REPO_ROOT / "services" / "backend" / "tests" / "test_test_database_target.py"
TEST_B = REPO_ROOT / "services" / "backend" / "tests" / "test_test_residue.py"
TEST_C = REPO_ROOT / "services" / "backend" / "tests" / "test_migration_model_parity.py"

#: 判据 B 的修复点（判据 B 抓到真缺陷的那一处夹具）。
WP21_FIXTURE = REPO_ROOT / "services" / "backend" / "tests" / "test_knowledge_wp21.py"

lines: list[str] = []
checks: list[bool] = []


def emit(ok: bool, label: str, detail) -> None:
    checks.append(bool(ok))
    lines.append("CHECK " + ("OK  " if ok else "FAIL") + " " + label + ": " + str(detail))


def head(title: str) -> None:
    lines.append("")
    lines.append(title)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_read(path))


def sans_comments(src: str) -> str:
    """去掉整行注释（保留代码）。判据扫「代码」不扫「解释它的文字」。"""
    return "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )


def sans_doc(src: str) -> str:
    """把**注释与文档字符串**替换成空白，保留行结构与普通字符串字面量。

    ⚠️ 本包三条判据的 docstring 里**故意**写了反例字面量（例如判据 B 的
    docstring 写了 `ix_chunk_embeddings_hnsw_` 来讲"名字名单没用"，
    判据 A 的 docstring 写了 `postgres` 来讲保留库）。
    只剥 `#` 注释时，任何"扫代码特征"的判据都会把**解释文字当违规代码**
    —— 本项目已现身 6 次的形态。因此这里必须剥 docstring。
    用 `ast` 取所有字符串表达式节点的行区间，比正则可靠。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return sans_comments(src)

    spans: list[tuple[int, int]] = []

    def collect(node: ast.AST) -> None:
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                spans.append((first.lineno, first.end_lineno or first.lineno))
        for child in ast.iter_child_nodes(node):
            collect(child)

    collect(tree)
    src_lines = src.splitlines()
    for start, end in spans:
        for i in range(start - 1, min(end, len(src_lines))):
            src_lines[i] = ""
    return sans_comments("\n".join(src_lines))


def _func_source(path: Path, name: str) -> str:
    """按行号取出顶层函数源码（含装饰器之前到函数结束）。"""
    src = _read(path)
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            end = node.end_lineno or node.lineno
            return "\n".join(src.splitlines()[start - 1 : end])
    return ""


def _method_source(path: Path, class_name: str, method_name: str) -> str:
    """取类方法源码。

    ⚠️ 本脚本第一版用 `_func_source(TOOL_A, "__str__")` 去找 `Finding.__str__` ——
    `_func_source` 只扫**顶层** `tree.body`，而 `__str__` 在类体里，
    于是**恒返回空串**、判据恒红（实测 [边界-1] 假红）。
    这正是本项目判据纪律的同款形态：**判据找不到东西时，报的是"位置不对"
    而不是"缺陷"**。改为显式下钻到类体，锚点是语法结构，不靠名字出现的位置。
    """
    src = _read(path)
    tree = ast.parse(src)
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        for sub in node.body:
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == method_name:
                start = min([sub.lineno] + [d.lineno for d in sub.decorator_list])
                end = sub.end_lineno or sub.lineno
                return "\n".join(src.splitlines()[start - 1 : end])
    return ""


def _dict_str_values(path: Path, const_name: str) -> dict[str, str]:
    """用 **AST** 取 `const_name = {k: "字符串"}` 里每个 key → 完整字符串值。

    ⚠️ 本脚本第一版用正则从源码里抠 SQL 字符串，失败（实测 [结构-9] 假红）：
    `SNAPSHOT_QUERIES` 的每个值写成**括号内的多段相邻字符串**
    （`("select a" " join b" ...)`），正则只匹配到第一段就要求收尾。
    **AST 里隐式字符串拼接（相邻字面量）在解析时就已合并成一个 `Constant`** ——
    所以 AST 能直接拿到完整 SQL，正则不能。
    判据应当建立在**语法结构**上，不是字符串的书写形式。
    """
    src = _read(path)
    tree = ast.parse(src)
    for node in tree.body:
        # ⚠️ 必须同时认 `Assign` 与 `AnnAssign`：本仓常量普遍带类型注解
        # （`SNAPSHOT_QUERIES: dict[str, str] = {...}` 是 `AnnAssign`）。
        # 只认前者 → 恒取不到 → 判据恒红（实测 [结构-9] 第二次假红的根因）。
        # 这与 `check_evidence_scripts._CHECK_DECL_RE` 的教训**完全同型**：
        # 「给变量补个注解就能绕过判据」——判据被实现的无关细节决定（ISS-82 族）。
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign):
            target = node.targets[0] if node.targets else None
            value = node.value
        else:
            continue
        if not (isinstance(target, ast.Name) and target.id == const_name):
            continue
        if not isinstance(value, ast.Dict):
            continue
        out: dict[str, str] = {}
        for key, val in zip(value.keys, value.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    out[key.value] = val.value
                elif isinstance(val, ast.BinOp):  # 显式 `"a" + "b"` 拼接
                    parts: list[str] = []
                    for sub in ast.walk(val):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            parts.append(sub.value)
                    out[key.value] = "".join(parts)
        return out
    return {}


def _funcs_containing(path: Path, needle: str, *, strip_doc: bool = False) -> list[str]:
    """返回文件中**函数体**含 `needle` 的顶层函数名列表。

    **到哪儿找由磁盘决定，不写死函数名**（铁律 14）。这样重构挪函数不会假红，
    而真正删掉/改窄实现仍会报红。
    """
    src = _read(path)
    tree = ast.parse(src)
    src_lines = src.splitlines()
    hits: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        end = node.end_lineno or node.lineno
        body = "\n".join(src_lines[start - 1 : end])
        haystack = sans_doc(body) if strip_doc else sans_comments(body)
        if needle in haystack:
            hits.append(node.name)
    return hits


def main() -> int:  # noqa: C901 —— 三遍法逐条覆盖，拆开反而看不清顺序
    tool_a_src = _read(TOOL_A)
    tool_b_src = _read(TOOL_B)
    tool_c_src = _read(TOOL_C)
    rev_src = _read(REVERSE)

    lines.append("== WP-DEBT-02 三遍法代码审查证据（机械可复现） ==")
    lines.append(f"判据 A：{TOOL_A.relative_to(REPO_ROOT)}")
    lines.append(f"判据 B：{TOOL_B.relative_to(REPO_ROOT)}")
    lines.append(f"判据 C：{TOOL_C.relative_to(REPO_ROOT)}")
    lines.append(f"反向验证：{REVERSE.relative_to(REPO_ROOT)}")
    lines.append("")
    lines.append("本脚本只做一件事：把 docs/m3/工程债-测试库可信度说明.md 里")
    lines.append("**每一条「已修 / 有判据」**变成能在代码里当场指认的判据。")
    lines.append("文档声称 = 待验证假设。")

    # ==================================================================
    head("--- 第一遍：结构与正确性 ---")

    # ---------- 判据 A ----------
    code_a = sans_comments(tool_a_src)

    # A-1 方向必须是"反向覆盖"：遍历代码里写的 DSN 字面量，而不是遍历一份保留名单。
    loops_literals = bool(re.search(r"for\s+node\s+in\s+ast\.walk\(", code_a))
    scans_files = bool(re.search(r"for\s+path\s+in\s+test_files", code_a))
    emit(
        loops_literals and scans_files,
        "[结构-1] 判据A 方向是「代码里写的 DSN → 库名不得是保留库」（反向覆盖）",
        f"ast.walk 遍历={'有' if loops_literals else '无'} 逐文件判定={'有' if scans_files else '无'}",
    )

    # A-2 保留库名必须只有 PG 自身固定的名字（不许夹带项目自己的名单）。
    m = re.search(r"STATIC_RESERVED_DATABASES[^=]*=\s*frozenset\(\s*\{([^}]*)\}", code_a)
    reserved = set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()
    assert_required = {"postgres", "template0", "template1"}
    emit(
        assert_required <= reserved,
        "[结构-2] 判据A 的保留库是 PG 固定名（postgres/template0/template1）",
        f"实际={sorted(reserved)}",
    )

    # A-3 保留名单是人写的 ⇒ 必须有对账判据（铁律 14）。到磁盘上找那条对账。
    reconcilers = _funcs_containing(TEST_A, "datistemplate")
    emit(
        bool(reconcilers),
        "[结构-3] 保留名单（人写）有对账判据：连真库读 pg_database.datistemplate",
        f"对账出现在={reconcilers or '（无）'}",
    )

    # A-4 扫描必须递归（rglob），且另有一套独立实现（os.walk）对账。
    rglob_in = _funcs_containing(TOOL_A, "rglob(", strip_doc=True)
    walk_in = _funcs_containing(TOOL_A, "os.walk(", strip_doc=True)
    emit(
        bool(rglob_in) and bool(walk_in),
        "[结构-4] 判据A 用 rglob 扫、另用 os.walk 独立对账（不是同一 API 的两种写法）",
        f"rglob={rglob_in or '（无）'} os.walk={walk_in or '（无）'}",
    )

    # A-5 覆盖范围必须有下界（否则"扫到但一个都没判"会打印漂亮 PASS）。
    has_floor = bool(re.search(r"MIN_JUDGED_LITERALS\s*=\s*\d+", code_a)) and bool(
        re.search(r"MIN_TEST_FILES\s*=\s*\d+", code_a)
    )
    emit(
        has_floor,
        "[结构-5] 判据A 的覆盖范围有下界（MIN_TEST_FILES / MIN_JUDGED_LITERALS）",
        f"两个下界常量={'都在' if has_floor else '**缺**'}",
    )

    # ---------- 判据 B ----------
    code_b = sans_comments(tool_b_src)

    # B-1 判定必须是"前后差分"，不是"查名字名单"。
    emit(
        "def _diff(" in code_b and "_snapshot()" in code_b,
        "[结构-6] 判据B 判定是「跑测试前后结构差分」（不需要知道垃圾长什么样）",
        f"_diff={'有' if 'def _diff(' in code_b else '无'} 快照={'_snapshot()' in code_b}",
    )

    # B-2 "测试真跑过"的前置条件必须在（ISS-143）。
    guard_zero = bool(re.search(r"if\s+collected\s*==\s*0\s*:", code_b))
    guard_rc = bool(
        re.search(r"if\s+proc\.returncode\s*!=\s*0\s*:", code_b)
    )
    emit(
        guard_zero and guard_rc,
        "[结构-7] 判据B 有「测试必须真跑过」的前置条件（collected==0 / rc!=0 → FAIL）",
        f"collected==0 拦截={'有' if guard_zero else '无'} rc!=0 拦截={'有' if guard_rc else '无'}",
    )

    # B-3 `-m` 表达式必须是一个 argv 元素（实测踩过的根因）。
    arr_ok = bool(
        re.search(r'DEFAULT_PYTEST_ARGS\s*:\s*tuple\[str,\s*\.\.\.\]\s*=\s*\(', code_b)
    ) and '"not slow"' in code_b
    shlex_ok = "shlex.split" in code_b
    emit(
        arr_ok and shlex_ok,
        "[结构-8] 判据B `-m` 表达式是一个 argv 元素 + 环境变量按 shell 语义解析",
        f"tuple 常量={'有' if arr_ok else '无'} shlex={'有' if shlex_ok else '无'}",
    )

    # B-4 快照 SQL 必须纯只读（判据不许成为被测对象的干扰源）。
    #     ⚠️ 必须用 AST 取（`SNAPSHOT_QUERIES` 的值是括号内**相邻字面量隐式拼接**，
    #     正则只会匹到第一段 —— 实测 [结构-9] 因此假红）。
    sqls = list(_dict_str_values(TOOL_B, "SNAPSHOT_QUERIES").values())
    writes = [
        w
        for w in ("drop ", "create ", "alter ", "insert ", "delete ", "truncate ")
        if any(w in s.lower() for s in sqls)
    ]
    all_select = bool(sqls) and all(s.lstrip().lower().startswith("select") for s in sqls)
    emit(
        all_select and not writes,
        "[结构-9] 判据B 的快照 SQL 纯只读（不建/不删，不污染基线）",
        f"AST 提取到 {len(sqls)} 段 SQL；全部以 select 开头={all_select}；写操作={writes if writes else '无'}",
    )

    # ---------- 判据 C ----------
    code_c = sans_comments(tool_c_src)

    # C-1 两条路径必须都**从零点建起**（否则"合理差异"会淹没真差异）。
    two_paths = ("_run_alembic_upgrade" in code_c) and ("_create_all" in code_c)
    recreate = "_recreate_database" in code_c
    emit(
        two_paths and recreate,
        "[结构-10] 判据C 两条路径都从零点建（alembic upgrade head vs create_all）",
        f"迁移路径={'有' if two_paths else '无'} 重建库={'有' if recreate else '无'}",
    )

    # C-2 用独立临时库，绝不碰共享测试库（ISS-108 一族）。
    tmp_db = bool(re.search(r"rednote_parity_audit_mig_\{os\.getpid\(\)\}", code_c))
    drops = bool(re.search(r'DROP DATABASE IF EXISTS "\{dbname\}"', code_c))
    emit(
        tmp_db and drops,
        "[结构-11] 判据C 用带 pid 的一次性临时库 + 跑完 DROP（不与并行会话抢锁）",
        f"临时库名带 pid={'有' if tmp_db else '无'} 收尾 DROP={'有' if drops else '无'}",
    )

    # C-3 排除集必须由磁盘/运行时派生（铁律 14），不是人写名单。
    derives = "_migration_bookkeeping_tables" in code_c
    ann_am = "ANN_ACCESS_METHODS" in code_c
    has_access_method_query = "pg_am" in code_c
    emit(
        derives and ann_am and has_access_method_query,
        "[结构-12] 判据C 排除集由磁盘派生：记账表三级派生 + ANN 按 access method 判定",
        f"记账表派生={'有' if derives else '无'} access method 查询={'有' if has_access_method_query else '无'}",
    )

    # C-4 判据 C 不许写死索引名（铁律 14 第 7 次的形态）。
    #     注意：docstring 里**故意**写了那个不存在的名字当教训 —— 必须剥 docstring。
    code_c_nodoc = sans_doc(tool_c_src)
    hardcoded = re.findall(r"['\"]ix_[A-Za-z0-9_]+['\"]", code_c_nodoc)
    emit(
        not hardcoded,
        "[结构-13] 判据C 判定逻辑里没有写死的索引名（剥 docstring 后扫）",
        f"命中={hardcoded if hardcoded else '无'}",
    )

    # C-5 排除集必须有自检（"派生名字在库里找不到" → 报红，不静默排除不存在的名字）。
    selfcheck = "排除集自检" in tool_c_src
    emit(
        selfcheck,
        "[结构-14] 判据C 有排除集自检（派生表名必须在迁移库找到、且不在模型库）",
        f"排除集自检={'有' if selfcheck else '无'}",
    )

    # ==================================================================
    head("--- 第二遍：逐行（文档声称 vs 磁盘事实） ---")

    # 15. **文档声称的路径必须存在**（本脚本自查出的新缺陷类别）。
    #     三条判据的 docstring 都写过反向验证脚本名，3 处全是错的。
    claimed = set()
    for path in (TEST_A, TEST_B, TEST_C, TOOL_A, TOOL_B, TOOL_C, REVERSE):
        src = _read(path)
        for ref in re.findall(r"`((?:tools|services)/[\w./\-]+\.py)`", src):
            claimed.add((path, ref))
    missing = [
        (p.relative_to(REPO_ROOT), ref)
        for p, ref in claimed
        if not (REPO_ROOT / ref).is_file()
    ]
    emit(
        not missing,
        "[逐行-1] 源码里 `` `tools/...py` `` 形式的路径引用**全部存在**（自纠 3 处失效引用）",
        f"检查 {len(claimed)} 处引用，不存在 {len(missing)} 处"
        + (f"：{missing}" if missing else ""),
    )

    # 16. 三条判据的测试都必须引到**真实存在**的反向验证脚本。
    #
    # ⚠️ 本脚本第一版断言 `refs == {反向验证}` —— 但那三个测试的 docstring
    # 也各自引用**自己**的判据文件（`tools/quality/check_*.py`），
    # 于是断言恒假、判据恒红（实测 [逐行-2] 假红）。
    # **判据不该要求"引用集合恰好等于某个值"** —— 那是把判据绑在
    # "当前这批文件恰好引用了什么"上，任何新增引用都会假红（ISS-137 族）。
    # 正确的不变式是**逐文件**的：每个测试都必须引用反向验证脚本，且该路径存在。
    rev_rel = str(REVERSE.relative_to(REPO_ROOT))
    per_file = {}
    for path in (TEST_A, TEST_B, TEST_C):
        src = _read(path)
        per_file[path.name] = rev_rel in src
    emit(
        all(per_file.values()) and REVERSE.is_file(),
        "[逐行-2] 三条判据的测试**各自**都引到那个真实存在的反向验证脚本",
        "; ".join(f"{n}={'有' if ok else '**无**'}" for n, ok in per_file.items()),
    )

    # 17. 反向验证的变异表条数 == `run_case(...)` 实际调用数（表与实现不许脱节）。
    #
    # ⚠️ 本脚本第一版两处都错了（实测 [逐行-3] 假红）：
    #   ① 用 `## 变异清单.*?\n\n` 取表格 —— 非贪婪匹配在**标题后的第一个空行**
    #      就收尾了，取到 0 行（"找错地方"报成"表是空的"）；
    #   ② 数 `run_case(` 时**连 docstring 里的那个词也数了** —— 本项目已现身
    #      6 次的形态（解释文字被当成代码）。两处都是**判据自己**的缺陷。
    # 修法：锚点取到下一个 `## ` 为止；计数前先剥 docstring 与注释。
    section = re.search(r"## 变异清单(.*?)\n## ", rev_src, re.S)
    table_block = section.group(1) if section else ""
    table_rows = len(re.findall(r"^\|\s*[①-⑨]\s*\|", table_block, re.M))
    rev_code_only = sans_doc(sans_comments(rev_src))
    run_case_calls = len(re.findall(r"\brun_case\(", rev_code_only)) - 1  # 减去 def 那一处
    emit(
        table_rows > 0 and table_rows == run_case_calls,
        "[逐行-3] 反向验证的「变异清单表」条数 == `run_case` 实调数（表与实现同步）",
        f"表格 {table_rows} 行 / 实调 {run_case_calls} 处",
    )

    # 18. 反向验证里**每条变异都断言"红的形状"**（不只断言 rc≠0）。
    marker_asserted = bool(re.search(r"hit\s*=\s*expect_marker\s+in\s+out", rev_src))
    code_and_marker = bool(re.search(r"exit_ok\s*=\s*\(code\s*!=\s*0\)", rev_src))
    emit(
        marker_asserted and code_and_marker,
        "[逐行-4] 反向验证断言「红的形状」（退出码非 0 **且** 命中预期标记）",
        f"标记断言={'有' if marker_asserted else '无'} 组合条件={'有' if code_and_marker else '无'}",
    )

    # 19. 成对变异必须有一条"期望假绿"（证明防线是必要的，不是装饰）。
    false_green = len(re.findall(r"expect_exit_nonzero=False", rev_src))
    emit(
        false_green >= 2,
        "[逐行-5] 有 ≥2 条「期望假绿」的成对变异（证明前置条件/比较是**必要**防线）",
        f"expect_exit_nonzero=False 出现 {false_green} 处",
    )

    # 20. 判据 A 的判定数必须分开统计（扫到的 / 豁免的 / 真判的），防"豁免吃光范围"。
    three_counts = bool(
        re.search(r"return\s+total,\s*exempt,\s*judged", code_a)
    ) or bool(re.search(r"literal_total\s*\+=\s*total", code_a))
    emit(
        three_counts,
        "[逐行-6] 判据A 分开统计「扫到 / 豁免 / 真判」（豁免吃光范围会可见，ISS-143）",
        f"三计数={'有' if three_counts else '无'}",
    )

    # 21. 白名单必须逐条给理由（否则退化成"把报红的地方都加进来"）。
    allow_block = re.search(r"ALLOW_FILES\s*:\s*dict\[str,\s*str\]\s*=\s*\{(.*?)\n\}", tool_a_src, re.S)
    entries = re.findall(r'"([\w.]+)"\s*:\s*"([^"]{10,})"', allow_block.group(1)) if allow_block else []
    emit(
        bool(allow_block) and len(entries) >= 1,
        "[逐行-7] 判据A 的白名单逐条给理由（≥10 字符），不是裸名单",
        f"登记 {len(entries)} 条：{[n for n, _ in entries]}",
    )

    # ==================================================================
    head("--- 第三遍：边界与加固 ---")

    # 22. 判据 A 的 `Finding.__str__` 必须容忍仓外路径（`relative_to` 会抛 ValueError）。
    #     ⚠️ 必须用 `_method_source`（方法在类体里，`_func_source` 只扫顶层 → 恒空）。
    finding_body = _method_source(TOOL_A, "Finding", "__str__")
    tolerant = "except ValueError" in finding_body
    emit(
        tolerant,
        "[边界-1] 打印一行日志不会成为崩溃原因（仓外路径 relative_to 已兜住）",
        f"Finding.__str__ 容错={'有' if tolerant else '无'}"
        f"（取到 {len(finding_body)} 字符；为 0 说明又找错位置了）",
    )

    # 23. 解析失败必须响亮报出（不静默跳过那个文件）。
    parse_err = bool(re.search(r"except SyntaxError as exc:", code_a))
    emit(
        parse_err,
        "[边界-2] 判据A 源码解析失败 → 以合成 finding 报出（静默跳过 = 对该文件失效）",
        f"SyntaxError 处理={'有' if parse_err else '无'}",
    )

    # 24. 已知边界必须在 docstring 里显式记录（不做假保证）。
    blind = ("已知边界" in tool_a_src) and ("已知边界" in tool_c_src) and (
        "已实测到的事实" in tool_b_src
    )
    emit(
        blind,
        "[边界-3] 三条判据都显式记录已知边界/盲区（不声称 100% 覆盖）",
        f"A={'有' if '已知边界' in tool_a_src else '无'} "
        f"B={'有' if '已实测到的事实' in tool_b_src else '无'} "
        f"C={'有' if '已知边界' in tool_c_src else '无'}",
    )

    # 25. 判据 C 的清理在 finally 里（中途失败也不留临时库）。
    main_c = _func_source(TOOL_C, "main")
    finally_cleanup = bool(re.search(r"finally:\s*[\s\S]*?DROP DATABASE", main_c))
    emit(
        finally_cleanup,
        "[边界-4] 判据C 的临时库清理在 finally 里（中途报错也不留垃圾）",
        f"finally 清理={'有' if finally_cleanup else '无'}",
    )

    # 26. 判据 C 的清理失败**要出声**（不能静默 —— 静默会累积孤儿库）。
    cleanup_warn = bool(re.search(r'清理 \{dbname\} 失败', tool_c_src))
    emit(
        cleanup_warn,
        "[边界-5] 判据C 临时库清理失败时打印警告（不静默累积孤儿库）",
        f"清理失败提示={'有' if cleanup_warn else '无'}",
    )

    # 27. 反向验证在**副本**里做（真树只读，ISS-103）。
    makes_copy = "shutil.copytree(" in rev_src
    emit(
        makes_copy,
        "[边界-6] 反向验证在副本里做（真树只读，进程被强杀也不污染）",
        f"copytree={'有' if makes_copy else '无'}",
    )

    # 28. 还原源必须是**变异前**的文本（第一版的坑：写成变异后 → 副本越跑越脏）。
    original_texts = bool(
        re.search(r"original_texts\s*:\s*dict\[Path,\s*str\]\s*=\s*\{\}", rev_src)
    ) and bool(re.search(r"for\s+path,\s*text\s+in\s+original_texts\.items\(\)", rev_src))
    emit(
        original_texts,
        "[边界-7] 反向验证还原源是**变异前**的文本（不是变异后）",
        f"original_texts 只读源={'有' if original_texts else '无'}",
    )

    # 29. 真树取证范围是**整树**（不是自证清白的文件清单）。
    scope = re.search(r"TREE_SCOPE\s*=\s*\(([^)]*)\)", rev_src)
    scope_items = re.findall(r'"([^"]+)"', scope.group(1)) if scope else []
    emit(
        len(scope_items) >= 3,
        "[边界-8] 反向验证的真树取证范围是整树（≥3 个顶层目录）",
        f"范围={scope_items}",
    )

    # 30. 反向验证脚本自身有 EXPECTED_CHECKS 且成功判定核对它（ISS-43）。
    #
    # ⚠️ 本脚本第一版断言的是 `complete = len(checks) >= expected` ——
    # 那是**变量名**，而门禁 `check_evidence_scripts.py` 认的是
    # `complete = len(checks) >= EXPECTED_CHECKS` 这个**形态**。
    # 实测被门 3 抓到：反向验证脚本因为"条数核对用的变量名带了 `_FAST/_FULL` 后缀"
    # 被判成"没有核对条数"。**判据被变量名决定**（ISS-82 族）——
    # 因此这里也改成认同一个形态，与门禁同口径。
    has_expected = bool(re.search(r"^EXPECTED_CHECKS_FAST\s*=\s*\d+", rev_src, re.M)) and bool(
        re.search(r"^EXPECTED_CHECKS_FULL\s*=\s*\d+", rev_src, re.M)
    )
    checks_it = bool(re.search(r"complete\s*=\s*len\(checks\)\s*>=\s*EXPECTED_CHECKS", rev_src))
    emit(
        has_expected and checks_it,
        "[边界-9] 反向验证自身有 EXPECTED_CHECKS（fast/full 各一）且成功判定按门禁形态核对它",
        f"两个常量={'有' if has_expected else '无'} 核对形态={'有' if checks_it else '无'}",
    )

    # 31. **CI 用 `--fast`**：反向验证必须支持无真库环境（判据 A 纯静态）。
    has_fast = "--fast" in rev_src and "TOOL_A" in rev_src
    emit(
        has_fast,
        "[边界-10] 反向验证支持 `--fast`（仅判据A 纯静态变异，可在 CI 跑）",
        f"--fast 分支={'有' if has_fast else '无'}",
    )

    # 32. 每个判据都有**自身的判据**（判据纪律第 9 条：声称有防线 = 待验证假设）。
    three_tests = all(p.is_file() for p in (TEST_A, TEST_B, TEST_C))
    emit(
        three_tests,
        "[边界-11] 三条判据各自都有自身判据（A/B/C 三个测试文件都在）",
        f"A={'有' if TEST_A.is_file() else '无'} "
        f"B={'有' if TEST_B.is_file() else '无'} "
        f"C={'有' if TEST_C.is_file() else '无'}",
    )

    # 33. 判据 B 抓到的那处**真缺陷**已修（WP21 夹具差分清理）。
    wp21 = _read(WP21_FIXTURE)
    has_before = "ann_before = _ann_index_names(engine)" in wp21
    has_residue = "residue = _ann_index_names(engine) - ann_before" in wp21
    has_assert = bool(re.search(r"assert not leftover", wp21))
    emit(
        has_before and has_residue and has_assert,
        "[边界-12] 判据B 抓到的真缺陷已修（WP21 夹具前后差分 + 断言为空）",
        f"ann_before={'有' if has_before else '无'} 差分={'有' if has_residue else '无'} 断言={'有' if has_assert else '无'}",
    )

    # 34. WP21 夹具按 **access method** 判定索引，不写死名字前缀（铁律 14）。
    ann_fn = _func_source(WP21_FIXTURE, "_ann_index_names")
    ann_body = sans_doc(ann_fn)
    by_am = ("hnsw" in ann_body.lower() or "ivfflat" in ann_body.lower()) and (
        "pg_am" in ann_body or "amname" in ann_body
    )
    emit(
        by_am and not re.search(r"['\"]ix_[A-Za-z0-9_]+", ann_body),
        "[边界-13] WP21 夹具按 access method 找 ANN 索引（不写死名字前缀）",
        f"按 pg_am 判定={'有' if by_am else '无'} 写死 ix_ 名={'有' if re.search(chr(39) + 'ix_', ann_body) else '无'}",
    )

    # 35. docstring 里写过的"自纠"必须**言之有物**（有日期 + 有后果说明）。
    #
    # ⚠️ 本脚本第一版只认 `⚠️ 2026-…` 前缀（实测 [边界-14] 假红：只数到 2 条）——
    # 而三处自纠里有**一处没加 ⚠️ 前缀**。判据绑在"某个 emoji 在不在"上，
    # 与本项目其他"绑在人写的一句话上"的缺陷同型。
    # 改为认**日期形态**（那是自纠记录真正的特征），前缀可有可无。
    self_corrections = 0
    for path in (TEST_A, TEST_C, REVERSE):
        src = _read(path)
        self_corrections += len(re.findall(r"2026-\d\d-\d\d 自纠", src))
    emit(
        self_corrections >= 3,
        "[边界-14] 三处「文档与磁盘脱节」已在源码里留下带日期的自纠记录（≥3 条）",
        f"自纠记录={self_corrections} 条（认日期形态，不认 emoji 前缀）",
    )

    # 36. 三条判据的默认目标 DSN 都不指向保留库（判据不许自己违反判据 A 的规则）。
    offenders = []
    for path, src in ((TOOL_A, tool_a_src), (TOOL_B, tool_b_src), (TOOL_C, tool_c_src)):
        code = sans_doc(sans_comments(src))
        for dsn in re.findall(r'"(postgresql[^"]*://[^"]+)"', code):
            db = dsn.rsplit("/", 1)[-1].split("?")[0]
            if db in assert_required:
                offenders.append((path.name, dsn))
    emit(
        not offenders,
        "[边界-15] 三条判据自带的默认 DSN 都不指向保留库（判据自己不违反判据A）",
        f"违规={offenders if offenders else '无'}",
    )

    # ==================================================================
    head("--- 结论 ---")
    passed = sum(1 for c in checks if c)
    failed = len(checks) - passed
    complete = len(checks) >= EXPECTED_CHECKS
    lines.append("")
    lines.append(
        f"共 {len(checks)} 条 CHECK：PASS {passed} / FAIL {failed}"
        f"（完整={complete}，期望 ≥{EXPECTED_CHECKS}）"
    )
    lines.append(
        f"RESULT={'PASS' if (failed == 0 and complete) else 'FAIL'} "
        f"checks={passed} PASS / {failed} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）"
    )

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"写出 {OUT_FILE.relative_to(REPO_ROOT)}")
    print(
        f"RESULT={'PASS' if (failed == 0 and complete) else 'FAIL'} "
        f"checks={passed} PASS / {failed} FAIL（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）"
    )
    for line in lines:
        if line.startswith("CHECK FAIL"):
            print("  " + line)
    return 0 if (failed == 0 and complete) else 1


#: 本脚本应产生的 CHECK 条数下界（结构 14 + 逐行 7 + 边界 15 = 36）。
EXPECTED_CHECKS = 36


if __name__ == "__main__":
    raise SystemExit(main())
