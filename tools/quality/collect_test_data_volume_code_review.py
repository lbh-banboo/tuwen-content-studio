#!/usr/bin/env python
"""WP-DEBT-05 代码审查证据（三遍法：结构 → 逐行 → 边界与加固）。

产出 `evidence/m3/test-data-volume-code-review.log`。

按 `code-review` 技能的三遍法，五维度（Security / Performance / Correctness /
Maintainability / Testing）。每条"通过"都必须附上能在代码里当场指认的判据——
文档说"这条有判据"，必须能当场指出脚本里哪个函数（判据纪律第 9 条）。

## 本包的文件

| 文件 | 角色 |
|---|---|
| `tools/quality/check_test_data_volume.py` | 判据 D 本体 |
| `tools/dev/reset_shared_test_db.py` | 手动重置工具 |
| `services/backend/src/rednote/platform/db_graph.py` | 共享闭包算法 |
| `services/backend/tests/test_test_data_volume.py` | 判据 D 自身的判据 |
| `services/backend/tests/test_reset_shared_test_db.py` | 重置工具自身的判据 |
| `tools/quality/collect_test_data_volume_reverse_evidence.py` | 反向验证矩阵 |

跑法：

    python tools/quality/collect_test_data_volume_code_review.py

退出码 0 = 全部 CHECK 通过。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"
OUT_FILE = EVIDENCE_DIR / "test-data-volume-code-review.log"

TOOL_D = REPO_ROOT / "tools" / "quality" / "check_test_data_volume.py"
RESET = REPO_ROOT / "tools" / "dev" / "reset_shared_test_db.py"
DB_GRAPH = REPO_ROOT / "services" / "backend" / "src" / "rednote" / "platform" / "db_graph.py"
REVERSE = REPO_ROOT / "tools" / "quality" / "collect_test_data_volume_reverse_evidence.py"
TEST_D = REPO_ROOT / "services" / "backend" / "tests" / "test_test_data_volume.py"
TEST_RESET = REPO_ROOT / "services" / "backend" / "tests" / "test_reset_shared_test_db.py"

lines: list[str] = []
checks: list[bool] = []


def emit(ok: bool, label: str, detail: object) -> None:
    checks.append(bool(ok))
    lines.append("CHECK " + ("OK  " if ok else "FAIL") + " " + label + ": " + str(detail))


def head(title: str) -> None:
    lines.append("")
    lines.append(title)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def sans_doc(src: str) -> str:
    """把注释与文档字符串替换成空白——判据扫"代码"不扫"解释它的文字"。

    本包判据 D 的 docstring 里故意写了反例/示例文案（例如 `TRUNCATE ... CASCADE`
    这个词本身就出现在"为什么不用它"的解释里）。只剥 `#` 行注释不够，必须连
    docstring 一起剥，否则"注释里写了反例，判据把反例当违规"（本仓已现身多次）。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))

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
    return "\n".join(
        line for line in src_lines if not line.lstrip().startswith("#")
    )


def _func_source(path: Path, name: str) -> str:
    src = _read(path)
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            end = node.end_lineno or node.lineno
            return "\n".join(src.splitlines()[start - 1 : end])
    return ""


def main() -> int:
    tool_src = _read(TOOL_D)
    reset_src = _read(RESET)

    # ================================================================== 结构
    head("--- 第一遍：结构 ---")

    # 1. ROOT_TABLES 恰为三张无主根表，且 docstring 交代了"为何不测闭包"。
    tree = ast.parse(tool_src)
    root_tables_val = None
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "ROOT_TABLES" for t in targets):
                if isinstance(node.value, ast.Tuple):
                    root_tables_val = tuple(
                        e.value for e in node.value.elts if isinstance(e, ast.Constant)
                    )
    emit(
        root_tables_val == ("projects", "users", "brands"),
        "[结构-1] ROOT_TABLES 恰为三张无主根表",
        f"实际={root_tables_val}",
    )
    emit(
        "不测闭包全表" in tool_src or "闭包全表" in tool_src,
        "[结构-1b] docstring 交代了为何只测根表、不测闭包全表",
        "命中关键词" if "闭包全表" in tool_src else "未命中",
    )

    # 2. MAX_ROWS 必须由 MEASURED_ROWS_PER_RUN × SAFETY_FACTOR 机械派生（铁律 14）。
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_test_data_volume_cr", TOOL_D)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_test_data_volume_cr"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    derived_ok = all(
        mod.MAX_ROWS[t] == mod._round_up(mod.MEASURED_ROWS_PER_RUN[t] * mod.SAFETY_FACTOR, 1000)
        for t in mod.ROOT_TABLES
    )
    emit(derived_ok, "[结构-2] MAX_ROWS 由 MEASURED_ROWS_PER_RUN × SAFETY_FACTOR 机械派生", f"ROOT_TABLES={mod.ROOT_TABLES}")
    emit(
        2 <= mod.SAFETY_FACTOR <= 8,
        "[结构-2b] SAFETY_FACTOR 在合理区间 [2, 8] 内",
        f"SAFETY_FACTOR={mod.SAFETY_FACTOR}",
    )
    emit(
        all(v > 0 for v in mod.MAX_ROWS.values()),
        "[结构-2c] 三个阈值均为正数（没有占位 0）",
        f"MAX_ROWS={mod.MAX_ROWS}",
    )

    # 3. 判定用严格大于，不是 >=（等于阈值必须放行）。
    evaluate_src = _func_source(TOOL_D, "evaluate")
    emit(
        "rows > limit" in evaluate_src and "rows >= limit" not in evaluate_src,
        "[结构-3] evaluate() 用严格大于（等于阈值放行）",
        "命中 'rows > limit'" if "rows > limit" in evaluate_src else "未命中",
    )

    # 4. read_counts 的计数 SQL 必须纯只读。
    read_counts_src = _func_source(TOOL_D, "read_counts")
    forbidden = ("insert ", "update ", "delete ", "truncate", "drop ", "alter ")
    offenders = [w for w in forbidden if w in read_counts_src.lower()]
    emit(not offenders, "[结构-4] read_counts() 不含任何写操作关键字", f"命中={offenders or '无'}")

    # 5. 缺表/读不到是独立 problem 路径，不折算成 0（evaluate 不把 None 当 0 用）。
    evaluate_ast = ast.parse(evaluate_src)
    has_none_check = any(
        isinstance(n, ast.Compare)
        and any(isinstance(c, ast.Constant) and c.value is None for c in n.comparators)
        for n in ast.walk(evaluate_ast)
    )
    emit(has_none_check, "[结构-5] evaluate() 显式判断 rows is None（不折算成 0）", f"命中 None 比较={has_none_check}")

    # 6. main() 在有 problem 或 violation 时返回非零；connection failure 也必须 fail-closed。
    main_src = _func_source(TOOL_D, "main")
    emit(
        "return 1 if (violations or problems) else 0" in main_src.replace(" ", "")
        .replace("\n", "")
        or "violationsorproblems" in main_src.replace(" ", "").replace("\n", ""),
        "[结构-6] main() 的退出码同时取决于 violations 与 problems（fail-closed）",
        "命中 'violations or problems' 形态",
    )

    # ================================================================== 逐行
    head("--- 第二遍：逐行 ---")

    # 7. docstring 里引用的路径必须在磁盘存在。
    referenced = re.findall(r"`(tools/[\w./]+\.py|docs/[\w./\-]+\.md)`", tool_src)
    missing = [rel for rel in referenced if not (REPO_ROOT / rel).exists()]
    emit(not missing, "[逐行-1] 判据 D docstring 引用的路径均存在于磁盘", f"缺失={missing or '无'}（共引用 {len(referenced)} 处）")

    # 8. 失败文案含表名/实际数/阈值/倍数，并提示重置工具。
    format_report_src = _func_source(TOOL_D, "format_report")
    has_hint = "reset_shared_test_db.py" in format_report_src
    has_ratio = "ratio" in format_report_src or "倍" in format_report_src
    emit(has_hint and has_ratio, "[逐行-2] 失败文案含倍数提示且指向重置工具", f"hint={has_hint}, ratio={has_ratio}")

    # 9. 阈值常量旁有带日期的实测来源（不是拍脑袋的数字）。
    dated_measurement = bool(re.search(r"2026-\d\d-\d\d 实测", tool_src))
    emit(dated_measurement, "[逐行-3] MEASURED_ROWS_PER_RUN 旁有带日期的实测记录", f"命中={dated_measurement}")

    # ================================================================== 边界
    head("--- 第三遍：边界与加固 ---")

    # 10. 反向验证的变异条数必须与 EXPECTED_CHECKS 常量核对（不能悄悄多/少）。
    if str(REVERSE.parent) not in sys.path:
        sys.path.insert(0, str(REVERSE.parent))
    rev_src = _read(REVERSE)
    run_case_calls = len(re.findall(r"^\s{8}run_case\(", rev_src, flags=re.MULTILINE))
    expected_match = re.search(r"^EXPECTED_CHECKS = (\d+)", rev_src, flags=re.MULTILINE)
    expected_val = int(expected_match.group(1)) if expected_match else -1
    # run_case 调用数 + probe-sound + own-writes-disjoint + real-tree-untouched 三条固定 CHECK
    emit(
        expected_val == run_case_calls + 3,
        "[边界-1] 反向验证 EXPECTED_CHECKS 与实际 run_case 调用数 + 3 条固定 CHECK 核对一致",
        f"EXPECTED_CHECKS={expected_val}, run_case 调用={run_case_calls}",
    )
    emit(
        "len(checks) == EXPECTED_CHECKS" in rev_src,
        "[边界-1b] 反向验证用 == 而不是只用 >= 核对条数（ISS-153 教训）",
        "命中 'len(checks) == EXPECTED_CHECKS'" in rev_src,
    )

    # 11. 重置工具安全门齐全：保留库拒绝 / 双确认 / 先探活再建写引擎 / 设超时 / 不用 CASCADE。
    main_fn_reset = _func_source(RESET, "main")
    execute_fn_reset = _func_source(RESET, "execute")
    gates = {
        "保留库拒绝": "_guard_reserved" in main_fn_reset,
        "双确认(--yes --confirm)": "args.confirm!=database" in main_fn_reset.replace(" ", ""),
        "先探活(assert_quiet)": "assert_quiet" in main_fn_reset,
        "设超时": "lock_timeout" in execute_fn_reset and "statement_timeout" in execute_fn_reset,
        "不用CASCADE": "CASCADE" not in execute_fn_reset,
    }
    emit(all(gates.values()), "[边界-2] 重置工具五道安全门齐全", f"{gates}")

    # 12. 闭包算法全仓只有一处实现（conftest.py 已改为 re-export，不是另一份拷贝）。
    sys.path.insert(0, str(DB_GRAPH.parent.parent.parent.parent.parent))
    impl_count = 0
    conftest = REPO_ROOT / "services" / "backend" / "tests" / "conftest.py"
    for path in (DB_GRAPH, conftest):
        code = sans_doc(_read(path))
        impl_count += len(re.findall(r"^def truncate_targets\(", code, flags=re.MULTILINE))
    emit(
        impl_count == 1,
        "[边界-3] truncate_targets 全仓只有一处真实现（conftest 是 re-export 不是拷贝）",
        f"实现数={impl_count}（应为 1：db_graph.py 持有，conftest.py 只 import）",
    )

    # ------------------------------------------------------------------
    head("--- 结论 ---")
    passed = sum(1 for c in checks if c)
    failed = len(checks) - passed
    #: 门禁 `check_evidence_scripts.py` 认的是这个**字面形态**
    #: `complete = len(checks) >= EXPECTED_CHECKS`（ISS-82 族：判据被变量名/
    #: 形态决定），因此即便本脚本想要的是精确相等，也要保留这一行、
    #: 再另加 `exact` 做真正的精确核对（ISS-153：`>=` 只拦变少不拦变多）。
    complete = len(checks) >= EXPECTED_CHECKS
    exact = len(checks) == EXPECTED_CHECKS
    lines.append("")
    lines.append(
        f"共 {len(checks)} 条 CHECK：PASS {passed} / FAIL {failed}（条数核对 {'一致' if exact else '不一致'}，期望 {EXPECTED_CHECKS}）"
    )
    verdict = failed == 0 and complete and exact
    lines.append(f"RESULT={'PASS' if verdict else 'FAIL'} checks={passed} PASS / {failed} FAIL")

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写出 {OUT_FILE.relative_to(REPO_ROOT)}")
    print(f"RESULT={'PASS' if verdict else 'FAIL'} checks={passed} PASS / {failed} FAIL（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）")
    for line in lines:
        if line.startswith("CHECK FAIL"):
            print("  " + line)
    return 0 if verdict else 1


#: 本脚本应产生的 CHECK 条数（结构 9 + 逐行 3 + 边界 4 = 16）。
EXPECTED_CHECKS = 16


if __name__ == "__main__":
    raise SystemExit(main())
