#!/usr/bin/env python
"""WP-DEBT-01 代码审查证据（三遍法：结构 → 逐行 → 边界与加固）。

产出 `evidence/m3/dependency-declaration-code-review.log`。

**为什么要把审查结果落成文件**：审查结论是"声称"，而声称需要证据。
本项目已踩过"文档/看板是唯一声称方时没有任何机械判据与它对账"
（ISS-78/84/85/86/94/95），因此本记录里每一处"通过"都必须附上
**能在代码里当场指认的判据**（判据纪律第 9 条：凡是文档说"这条有判据"，
必须能当场指出脚本里的哪个函数）。

本包只有两个源文件：
  · `tools/quality/check_dependency_declarations.py`（新建判据本体）
  · `tools/quality/collect_dependency_declaration_reverse_evidence.py`（反向验证矩阵）
外加 `services/backend/pyproject.toml`（声明补全，非代码）。

## 审查方式

按 `code-review` 技能的三遍法（结构 → 逐行 → 边界与加固），
五维度（Security / Performance / Correctness / Maintainability / Testing）。

## ⚠️ 本记录里最该记住的一条

**第一遍读代码时发现的第一条缺陷，是"判据自己"的**（不是它检查的对象）：

  ① `scan()` 初版在遍历 `runtime_declared` 的循环里写
     `if dist not in runtime_declared:` —— **恒假**，永不触发（ISS-137 同族）；
  ② `EXPECTED_CHECKS` 初版随依赖数浮动（22），删一条声明会让
     "条数不足"守卫**抢先报警**，把真正的诊断盖掉；
  ③ `DECLARED_NOT_IMPORTED_ALLOWLIST` 里登记的 `psycopg` **永不触发**
     （它实际被 `tools/` import）—— 装饰性登记（ISS-72 同族）。

三条都是"判据自身缺陷"，且**都在它抓别人之前先在自己身上出现**。
这正是本项目判据纪律的核心：防线要能被反向验证，包括防线自己。

跑法：

    python tools/quality/collect_dependency_declaration_code_review.py

退出码 0 = 全部 CHECK 通过。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"
OUT_FILE = EVIDENCE_DIR / "dependency-declaration-code-review.log"

TOOL = REPO_ROOT / "tools" / "quality" / "check_dependency_declarations.py"
REVERSE = REPO_ROOT / "tools" / "quality" / "collect_dependency_declaration_reverse_evidence.py"
PYPROJECT = REPO_ROOT / "services" / "backend" / "pyproject.toml"

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
    """去掉整行注释（保留代码）。

    判据扫"代码"不扫"解释它的文字" —— 本项目已有 6 个脚本因
    "注释里写了反例、判据把反例当违规"而集体假红（`_strip_comments` 的教训）。
    """
    return "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )


def sans_doc(src: str) -> str:
    """把**注释与文档字符串**替换成空白，保留行结构与普通字符串字面量。

    ⚠️ **本脚本第一版就在这上面栽了**（实测 [结构-5] 假红）：
    `_count_py_files_independently` 的 **docstring 里讲解"为什么不能用 rglob"**
    —— 它自己写了 `rglob` 这个词。只剥 `#` 注释（`sans_comments`）时，
    判断"该函数里有没有用 rglob"的判据会把**解释文字当成违规代码**。

    这与 `tools/m1/collect_wp22_code_review.py` 记录的根因**完全同型**
    （它的 5 条假发现全来自"模块 docstring 里写了反例"）。本项目已现身 6 次，
    因此这里必须剥 docstring，而不是只剥注释。

    实现：用 `ast` 取所有字符串表达式节点（模块/类/函数首条 expr）的
    行列区间，把那些行替换成空串。比正则可靠（正则认不出三引号嵌套）。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return sans_comments(src)

    # 收集所有 docstring 节点（ast.get_docstring 的那一类：body[0] 是 Expr(Constant str)）
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

    lines = src.splitlines()
    for start, end in spans:
        for i in range(start - 1, min(end, len(lines))):
            # 保留缩进之外的内容置空，但行数不变（行号判据仍可用）。
            lines[i] = ""
    stripped = "\n".join(lines)
    return sans_comments(stripped)


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


def _funcs_containing(path: Path, needle: str, *, require_independent_of_doc: bool = False) -> list[str]:
    """返回本文件中**函数体**（剥掉注释与 docstring 后）含 `needle` 的顶层函数名列表。

    **到哪儿找由磁盘决定，不写死函数名**（铁律 14：凡覆盖范围由人写，
    就必须改由磁盘决定）。本脚本第一版把被检查对象写成 `_scan_imports`
    这一个函数名，而扫描实现后来被挪进同文件另一个顶层函数 ⇒ 判据
    "搜不到"就报红，报的却是**位置变了**而不是**缺陷**（实测 [结构-3]/[结构-6] 假红）。

    与判据纪律第 9 条同型：判据不该依赖"某个人写的某个名字还在不在"。
    改为遍历全部顶层函数后，**重构挪函数不会假红**，而**真正删掉/改窄扫描**
    （哪个函数里都没有 `rglob(` / `ast.walk(`）**仍会报红**。

    `require_independent_of_doc=True` 时先剥 docstring —— 否则"讲解为什么不能用
    `rglob` 的那段文字"会让判据以为自己找到了用法（本项目已现身 6 次的形态）。
    """
    src = _read(path)
    tree = ast.parse(src)
    lines = src.splitlines()
    hits: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        end = node.end_lineno or node.lineno
        body = "\n".join(lines[start - 1 : end])
        haystack = sans_doc(body) if require_independent_of_doc else sans_comments(body)
        if needle in haystack:
            hits.append(node.name)
    return hits


def _find_loop_source(
    path: Path, func_name: str, *, iter_call_name: str, iter_arg: str
) -> str:
    """在顶层函数 `func_name` 内找 `for x in <iter_call_name>(<iter_arg>):` 循环体源码。

    **为什么需要它**（实测 [逐行-4] 假红的根因）：第一版用注释文字切片定位
    "互补判据那一段"，而锚点字符串本身写在注释里、切片前已被剥掉 ⇒ 恒为空。
    **判据不能建立在"人写的一句话还在不在"上** —— 改一句注释就会静默改变判据行为。
    用 AST 定位循环体，锚点是**语法结构**，不随措辞变化。
    """
    src = _read(path)
    tree = ast.parse(src)
    for node in tree.body:
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.For):
                continue
            it = sub.iter
            if not (isinstance(it, ast.Call) and isinstance(it.func, ast.Name)):
                continue
            if it.func.id != iter_call_name or not it.args:
                continue
            arg = it.args[0]
            if isinstance(arg, ast.Name) and arg.id == iter_arg:
                lines = src.splitlines()
                start = sub.body[0].lineno if sub.body else sub.lineno
                end = sub.end_lineno or sub.lineno
                return "\n".join(lines[start - 1 : end])
    return ""


def main() -> int:
    tool_src = _read(TOOL)
    rev_src = _read(REVERSE)
    tool_tree = _tree(TOOL)
    rev_tree = _tree(REVERSE)

    lines.append("== WP-DEBT-01 三遍法代码审查证据（机械可复现） ==")
    lines.append(f"判据本体：{TOOL.relative_to(REPO_ROOT)}")
    lines.append(f"反向验证：{REVERSE.relative_to(REPO_ROOT)}")
    lines.append(f"声明补全：{PYPROJECT.relative_to(REPO_ROOT)}")
    lines.append("")
    lines.append("本脚本只做一件事：把 docs/m3/工程债-依赖声明一致性说明.md 里")
    lines.append("**每一条「已修」**变成能在代码里当场指认的判据。")
    lines.append("文档声称 = 待验证假设。")

    # ==================================================================
    head("--- 第一遍：结构与正确性 ---")

    # 1. 判据方向必须是"反向覆盖"（本工具唯一重点）
    tool_code = sans_comments(tool_src)
    # 正确方向：遍历 imported_distributions（代码侧），或直接遍历各 scope 的 imports。
    has_reverse_loop = bool(
        re.search(r"for\s+\w+\s*,\s*\w+\s+in\s+sorted\(imports\.items\(\)\)", tool_code)
    )
    # 错误方向的特征：在遍历"已声明集合"的循环体内做成员断言。
    bad_pattern = bool(
        re.search(
            r"for\s+\w+\s+in\s+sorted\(runtime_declared\):\s*\n\s*if\s+\w+\s+not\s+in\s+runtime_declared",
            tool_code,
        )
    )
    emit(
        has_reverse_loop and not bad_pattern,
        "[结构-1] 判据方向是「代码 import → 声明必须有」（反向覆盖）",
        f"反向循环={'有' if has_reverse_loop else '无'} 恒假式={'有' if bad_pattern else '无'}",
    )

    # 2. 反向覆盖必须遍历代码侧而不是声明侧 —— AST 判据：循环对象来自 _scan_imports
    ok_scan_used = "_scan_imports(" in tool_code
    emit(
        ok_scan_used,
        "[结构-2] 判据确实调用 _scan_imports（代码侧是独立的一侧）",
        f"_scan_imports 调用={'有' if ok_scan_used else '无'}",
    )

    # 3. 扫描范围是递归的（rglob），且另有独立实现对账（铁律 14 第 5 次复发候选）
    #
    # ⚠️ **锚点不能写死函数名**（本脚本第一版就在这上面栽，实测 [结构-3]/[结构-6] 假红）：
    #    第一版把被检查对象写成 `_scan_imports` 这一个函数名，而扫描实现后来被
    #    挪进同文件的另一个顶层函数 ⇒ 判据"搜不到"就报红 —— 报的却是**位置变了**
    #    而不是**缺陷**。这与本项目判据纪律第 9 条同型：判据不该依赖"某个人写的
    #    某句话/某个名字还在不在"。
    #    修法：把"到哪儿找"改由**磁盘决定** —— 遍历本工具**全部顶层函数体**，
    #    任一命中即通过。这样重构挪动函数不会造成假红，而**真正删掉/改窄扫描**仍会报红。
    rglob_found_in = _funcs_containing(TOOL, "rglob(", require_independent_of_doc=True)
    has_independent = "def _count_py_files_independently" in tool_src
    emit(
        bool(rglob_found_in),
        "[结构-3] 扫描用 rglob（递归）—— 非递归会让 src 从 174 掉到 0",
        f"rglob 出现在函数={rglob_found_in or '（无）'}",
    )
    emit(
        has_independent,
        "[结构-4] 另有 os.walk 独立实现做覆盖范围对账（不是同一 API 的两种写法）",
        f"_count_py_files_independently={'有' if has_independent else '无'}",
    )

    # 4. 独立对账用的必须是 os.walk（另一套遍历机制），不能是 rglob
    indep_body = _func_source(TOOL, "_count_py_files_independently")
    # ⚠️ 必须用 sans_doc（不只 sans_comments）：该函数的 docstring **自己写了
    #    `rglob` 这个词**来讲"为什么不能用 rglob"（实测 [结构-5] 因此假红）。
    indep_code = sans_doc(indep_body)
    uses_walk = "os.walk(" in indep_code
    indep_uses_rglob = "rglob(" in indep_code
    emit(
        uses_walk and not indep_uses_rglob,
        "[结构-5] 独立对账用 os.walk（若也用 rglob 则两侧会一起缩水，对账恒等）",
        f"os.walk={'有' if uses_walk else '无'} 该函数内rglob={'有' if indep_uses_rglob else '无'}",
    )

    # 5. 扫描必须深入函数体（ast.walk），而非只看模块顶层。
    #    锚点同样**不写死函数名**（见 [结构-3] 的说明）。
    walk_found_in = _funcs_containing(TOOL, "ast.walk(", require_independent_of_doc=True)
    emit(
        bool(walk_found_in),
        "[结构-6] AST 遍历用 ast.walk（含函数体内延迟导入）",
        f"ast.walk 出现在函数={walk_found_in or '（无）'}",
    )

    # 6. 迁移版本判据：本工具是纯静态、不涉及迁移版本，不应出现等式判据
    has_rev_eq = bool(re.search(r"(?:==|!=)\s*(?:EXPECTED_REVISION|REVISION_INTRODUCED)", tool_code))
    emit(
        not has_rev_eq,
        "[结构-7] 无「恰好等于某迁移版本」等式判据（本工具纯静态，不该有）",
        f"命中={1 if has_rev_eq else 0} 处",
    )

    # 7. 无「以两个表达式的长度比大小」式恒真判据（ISS-115 同族）
    #
    # ⚠️ 判据名与注释**不能写出那个被禁止的字面模式** —— 本脚本第一版把它原样
    #    写进了行 316 的判据名里，于是 `check_evidence_scripts.py` 的
    #    `_REVISION_LENGTH_HACK_RE`（只剥 `#` 注释、不剥字符串）把**描述文字**
    #    当成了违规代码（本项目已现身 6 次的形态，此处第 7 次）。
    #    这也正说明：把本脚本纳入 `check_evidence_scripts.py` 的覆盖面是有价值的
    #    —— 它当场就抓到了我这处问题。
    has_len_hack = bool(
        re.search(r"len\(\s*\w+\s*\)\s*(?:>=|<=|>|<)\s*len\(\s*\w+\s*\)", tool_code)
    )
    emit(
        not has_len_hack,
        "[结构-8] 无以两个表达式长度比大小的恒真判据（ISS-115 同族）",
        f"命中={1 if has_len_hack else 0} 处",
    )

    # ==================================================================
    head("--- 第二遍：逐行（EXPECTED_CHECKS 与诊断顺序） ---")

    # 8. EXPECTED_CHECKS 必须是**与依赖数量无关的固定值**
    m_const = re.search(r"^EXPECTED_CHECKS(?:\s*:\s*int)?\s*=\s*(\d+)\s*$", tool_src, re.M)
    fixed_value = int(m_const.group(1)) if m_const else -1
    # 反例特征：由 len(...) 推出
    computed = bool(re.search(r"^EXPECTED_CHECKS\s*=\s*\d+\s*\+\s*", tool_src, re.M))
    emit(
        fixed_value > 0 and not computed,
        "[逐行-1] EXPECTED_CHECKS 是固定值（不随 dependencies 条数浮动）",
        f"值={fixed_value} 由表达式算出={'是' if computed else '否'}",
    )

    # 9. main() 里**先输出语义诊断，再判条数**（否则条数守卫会盖掉真诊断）
    main_body = _func_source(TOOL, "main")
    if_pos = main_body.find("if findings:")
    checks_pos = main_body.find("if checks < EXPECTED_CHECKS:")
    ordered = 0 < if_pos < checks_pos
    emit(
        ordered,
        "[逐行-2] main() 先输出 findings 诊断、再判条数（else 会盖掉真诊断）",
        f"findings@{if_pos} < 条数守卫@{checks_pos} → {'是' if ordered else '否'}",
    )

    # 10. 互补判据整段只计 1 条 CHECK（不按依赖逐个计）
    #     特征：声明方向的循环体内**没有** checks += 1
    emits_per_dep = bool(
        re.search(
            r"for\s+dist\s+in\s+sorted\(runtime_declared\):[\s\S]{0,1200}?checks\s*\+=\s*1",
            tool_code,
        )
    )
    emit(
        not emits_per_dep,
        "[逐行-3] 互补判据整段只计 1 条 CHECK（按依赖计数会引发条数误报）",
        f"循环内 checks+=1={'有' if emits_per_dep else '无'}",
    )

    # 11. 未登记的"多余声明"必须**判红**（finding），不是只打印警告
    #
    # ⚠️ 第一版用注释文字切片（`tool_code.find("互补判据（声明方向）")`）——
    #    但那个锚点**本身在注释行里**，而 `tool_code` 是 `sans_comments` 的结果
    #    ⇒ 切片恒为空、判据恒 FAIL（实测 [逐行-4] 假红）。
    #    **用注释当锚点是把判据绑在"人写的一句话"上** —— 改注释就会静默改变判据行为。
    #    改为 AST 精确定位：`scan()` 里遍历 `sorted(runtime_declared)` 的那个 For 循环体。
    decl_loop_src = _find_loop_source(TOOL, "scan", iter_call_name="sorted", iter_arg="runtime_declared")
    emits_finding = "findings.append(" in decl_loop_src
    only_warn = "⚠️" in decl_loop_src and "findings.append(" not in decl_loop_src
    emit(
        emits_finding and not only_warn,
        "[逐行-4] 未登记的多余声明走 findings.append（判红），不是只打印警告（AST 定位循环体）",
        f"循环体取自 AST（{len(decl_loop_src)} 字符）findings.append={'有' if emits_finding else '无'} 仅警告={'是' if only_warn else '否'}",
    )

    # 12. 声明里的别名不得写回自己（映射表自检）
    mapping_body = _func_source(TOOL, "_check_mapping_table")
    guard = 'imp == dist and imp in {"docx", "PIL"}' in mapping_body
    emit(
        guard,
        "[逐行-5] 映射表自检：别名不得写回自己（docx / PIL 有实测别名）",
        f"自检分支={'有' if guard else '无'}",
    )

    # 13. 映射表的别名值必须是**实测**的，不是导入名本身
    m = re.search(r'"docx":\s*"([^"]+)"', tool_code)
    m2 = re.search(r'"PIL":\s*"([^"]+)"', tool_code)
    emit(
        bool(m and m.group(1) != "docx" and m2 and m2.group(1).lower() != "pil"),
        "[逐行-6] 别名是实测发行名：docx→python-docx、PIL→Pillow",
        f"docx→{m.group(1) if m else '?'} PIL→{m2.group(1) if m2 else '?'}",
    )

    # 14. 崩溃必须响亮失败（不能伪装成通过）
    crash_guard = bool(re.search(r"except Exception as exc:.*?\n.*?return 2", main_body, re.S))
    emit(
        crash_guard,
        "[逐行-7] 自检器自身崩溃 → 返回 2（崩溃 ≠ 通过，ISS-43）",
        f"崩溃守卫={'有' if crash_guard else '无'}",
    )

    # ==================================================================
    head("--- 第三遍：边界与加固 ---")

    # 15. 未登记的导入名必须报红，不静默跳过
    unreg_marker = "未登记映射" in tool_src
    emit(
        unreg_marker,
        "[边界-1] 未登记的导入名**报红**（不静默跳过 —— 静默会让写错名字看起来通过）",
        f"未登记映射 判据={'有' if unreg_marker else '无'}",
    )

    # 16. 解析失败必须响亮报出（不静默跳过那个文件）
    parse_err = "<PARSE-ERROR" in tool_src
    emit(
        parse_err,
        "[边界-2] 源码解析失败 → 以合成标记报出（静默跳过 = 对该文件失效）",
        f"PARSE-ERROR 标记={'有' if parse_err else '无'}",
    )

    # 17. 已知盲区必须**显式记录**（不做假保证）
    blind_spot = "动态导入" in tool_src and "importlib.import_module" in tool_src
    emit(
        blind_spot,
        "[边界-3] 动态导入盲区在 docstring 显式记录（不声称 100% 覆盖）",
        f"盲区记录={'有' if blind_spot else '无'}",
    )

    # 18. ALLOWLIST 必须逐条给理由（空白名单也要有说明）
    allowlist_has_reason = bool(
        re.search(r'DECLARED_NOT_IMPORTED_ALLOWLIST\s*:\s*dict\[str,\s*str\]\s*=\s*\{', tool_src)
    )
    # 每条登记项都要形如 "name": "理由…"
    allowlist_block = re.search(
        r"DECLARED_NOT_IMPORTED_ALLOWLIST[^{]*\{(.*?)\n\}", tool_src, re.S
    )
    entries = re.findall(r'"([\w.-]+)"\s*:\s*"([^"]{10,})"', allowlist_block.group(1)) if allowlist_block else []
    emit(
        allowlist_has_reason and len(entries) >= 1,
        "[边界-4] DECLARED_NOT_IMPORTED_ALLOWLIST 逐条给理由（≥10 字符）",
        f"登记 {len(entries)} 条：{sorted(n for n, _ in entries)}",
    )

    # 19. 登记项必须**真的会触发**（不能是装饰性登记，ISS-72 同族）
    #     反例：把实际会被 import 的包登记进来，那条理由永远不打印。
    import sys as _sys
    import importlib.util as _ilu

    spec = _ilu.spec_from_file_location("_cdd_cr", TOOL)
    mod = _ilu.module_from_spec(spec)
    _sys.modules["_cdd_cr"] = mod
    spec.loader.exec_module(mod)
    runtime_declared, _extras = mod._load_declarations()
    imported: set[str] = set()
    for base in (mod.SRC_DIR, mod.TESTS_DIR, mod.TOOLS_DIR, mod.MIGRATIONS_DIR):
        imps, _n, _top, _lazy = mod._scan_imports(base)
        for name in imps:
            if name.startswith("<") or name in _sys.stdlib_module_names:
                continue
            dist = mod.IMPORT_TO_DISTRIBUTION.get(name)
            if dist:
                imported.add(mod._norm(dist))
    dead_entries = [n for n in (n for n, _ in entries) if n in imported]
    emit(
        not dead_entries,
        "[边界-5] 登记项没有「永不触发」的（装饰性登记 = 防线变装饰）",
        f"永不触发的登记={dead_entries if dead_entries else '无'}",
    )

    # 20. 反向验证矩阵必须**每条变异都断言"红的形状"**（不只断言退出码非 0）
    #     特征：expect_marker 参与断言
    rev_code = sans_comments(rev_src)
    marker_asserted = bool(re.search(r"hit\s*=\s*expect_marker\s+in\s+out", rev_code))
    code_and_marker = bool(re.search(r"code\s*!=\s*0\s+and\s+hit", rev_code))
    emit(
        marker_asserted and code_and_marker,
        "[边界-6] 反向验证断言「红的形状」（退出码非 0 **且** 命中预期标记）",
        f"标记断言={'有' if marker_asserted else '无'} 组合条件={'有' if code_and_marker else '无'}",
    )

    # 21. 反向验证必须在**副本**里做（真树只读），且 finally 还原源是变异前的文本
    makes_copy = "shutil.copytree(" in rev_code
    restore_from_original = bool(
        re.search(r"original_texts\s*:\s*dict\[Path,\s*str\]\s*=\s*\{\}", rev_code)
    ) and bool(re.search(r"for\s+path,\s*text\s+in\s+original_texts\.items\(\)", rev_code))
    emit(
        makes_copy,
        "[边界-7] 反向验证在副本里做（真树只读，进程被强杀也不污染，ISS-103）",
        f"copytree={'有' if makes_copy else '无'}",
    )
    emit(
        restore_from_original,
        "[边界-8] 还原源是**变异前**的文本（第一版写成变异后 → 副本越跑越脏）",
        f"original_texts 只读源={'有' if restore_from_original else '无'}",
    )

    # 22. 真树未被触碰的取证范围是**整树**，不是"我打算改的那几个文件"
    tree_scope = re.search(r"TREE_SCOPE\s*=\s*\(([^)]*)\)", rev_code)
    scope_items = re.findall(r'"([^"]+)"', tree_scope.group(1)) if tree_scope else []
    emit(
        len(scope_items) >= 3,
        "[边界-9] 真树取证范围是整树（≥3 个顶层目录），不是自证清白的文件清单",
        f"范围={scope_items}",
    )

    # 23. 反向验证必须有 EXPECTED_CHECKS 且成功判定核对条数
    rev_has_expected = bool(re.search(r"^EXPECTED_CHECKS\s*=\s*\d+", rev_code, re.M))
    rev_complete = bool(re.search(r"complete\s*=\s*len\(checks\)\s*>=\s*EXPECTED_CHECKS", rev_code))
    emit(
        rev_has_expected and rev_complete,
        "[边界-10] 反向验证脚本自身有 EXPECTED_CHECKS 且成功判定核对它（ISS-43）",
        f"常量={'有' if rev_has_expected else '无'} 核对={'有' if rev_complete else '无'}",
    )

    # 24. 反向验证覆盖 7 种变异（用 run_case 调用数反推）
    run_case_calls = len(re.findall(r"run_case\(", rev_code)) - 1  # 减去 def 那一处
    emit(
        run_case_calls >= 7,
        "[边界-11] 反向验证覆盖 ≥7 种变异（每个对应一条真实失效面）",
        f"run_case 调用={run_case_calls} 处",
    )

    # ==================================================================
    head("--- 第三遍（续）：声明补全本身 ---")
    import tomllib

    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    deps = data["project"]["dependencies"]
    names = {d.split("[")[0].split("=")[0].split(">")[0].strip().lower() for d in deps}
    # 三个真实失效面的核心包
    for pkg, why in (
        ("cryptography", "顶层硬导入，是 CSRF 链上游（缺它迁移直接崩）"),
        ("pgvector", "顶层硬导入（WP21 真上 pgvector）"),
        ("httpx", "生产代码顶层导入，此前只在 [dev] ⇒ 不带 extra 装完即崩"),
    ):
        emit(
            pkg in names,
            f"[声明] {pkg} 在 dependencies 里（{why}）",
            f"{'在' if pkg in names else '**不在**'}",
        )
    for pkg in ("python-docx", "openpyxl", "pypdf", "pillow"):
        emit(
            pkg in names,
            f"[声明] {pkg} 在 dependencies 里（parsers 函数内延迟导入，同样必须声明）",
            f"{'在' if pkg in names else '**不在**'}",
        )
    # httpx 不得只在 extra 里
    extra_names = {
        d.split("[")[0].split("=")[0].strip().lower()
        for group in data["project"].get("optional-dependencies", {}).values()
        for d in group
    }
    emit(
        "httpx" in names and "httpx" not in extra_names,
        "[声明] httpx 已从 [dev] 提升为运行时（不是'两边都有'）",
        f"deps={'在' if 'httpx' in names else '不在'} dev={'在' if 'httpx' in extra_names else '不在'}",
    )
    # 每条依赖都锁到补丁号
    unlocked = [d for d in deps if "==" not in d]
    emit(
        not unlocked,
        "[声明] 每条 dependencies 都锁到具体版本（研发计划 §5：不用浮动 latest）",
        f"未锁版本={unlocked if unlocked else '无'}",
    )

    # ==================================================================
    head("--- 结论 ---")
    passed = sum(1 for c in checks if c)
    failed = len(checks) - passed
    complete = len(checks) >= EXPECTED_CHECKS
    lines.append("")
    lines.append(f"共 {len(checks)} 条 CHECK：PASS {passed} / FAIL {failed}（完整={complete}，期望 ≥{EXPECTED_CHECKS}）")
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


#: 本脚本应产生的 CHECK 条数下界（结构 8 + 逐行 7 + 边界 11 + 声明 9 = 35）。
EXPECTED_CHECKS = 35


if __name__ == "__main__":
    raise SystemExit(main())
