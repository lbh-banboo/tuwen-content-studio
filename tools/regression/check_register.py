#!/usr/bin/env python
"""回归护栏登记册对账：登记册 ⇄ 代码里的 pytest 标记，双向逐行核对。

**为什么需要这个脚本**（ISS-95：「声称自己有防线」比「没有防线」更危险）：
登记册 `docs/regression-register.md` 是一张自由文本表，若没有任何机械判据
与它对账，它就会和 ISS-78/84/94 里的看板一样悄悄过期且不报错。

四条判据（逐行，不用「合计相等」代替 —— 合计相等会掩盖行间借用）：

  R1  登记册每一行的 (文件, 函数) 在代码中真实存在
  R2  登记册每一行声明的标记，与该函数上的 `@pytest.mark.<x>` 一致
  R3  代码里每一个带 regression / cross_wp 标记的函数，登记册里都有一行（无孤儿）
  R4  登记册每一行的 ISS 编号在 issue-log.jsonl 中存在（「ADHOC」除外 ——
      代码审查阶段发现、未单独登记 ISS 的，必须写 ADHOC + 来源说明）

只用 stdlib + ast（不 import 被测代码，不连库），任何环境秒级可跑。

跑法：
    python tools/regression/check_register.py
退出码 0 = 全部通过。
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "services" / "backend" / "tests"
REGISTER = REPO_ROOT / "docs" / "regression-register.md"
ISSUE_LOG = REPO_ROOT / "docs" / "issues" / "issue-log.jsonl"

GUARD_MARKS = frozenset({"regression", "cross_wp"})
KNOWN_MARKS = GUARD_MARKS | {"slow", "smoke"}

# 登记册表格行：| RG-xx | ISS-xx / ADHOC | 标记 | 文件 | 函数 | ... |
_ROW_RE = re.compile(r"^\|\s*(RG-\d{3})\s*\|(.*)$")


def _marks_of(node: ast.AST, module_marks: set[str]) -> set[str]:
    found = set(module_marks)
    for dec in getattr(node, "decorator_list", []):
        text = ast.unparse(dec)
        m = re.match(r"pytest\.mark\.(\w+)", text)
        if m and m.group(1) in KNOWN_MARKS:
            found.add(m.group(1))
    return found


def _module_marks(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets
        ):
            out |= set(re.findall(r"pytest\.mark\.(\w+)", ast.unparse(stmt.value)))
    return out & KNOWN_MARKS


def collect_code() -> dict[tuple[str, str], set[str]]:
    """(文件名, 函数名) → 函数自身装饰器上的护栏标记（不含模块级 smoke）。"""
    result: dict[tuple[str, str], set[str]] = {}
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                result[(path.name, node.name)] = _marks_of(node, set()) & GUARD_MARKS
    return result


def collect_register() -> list[dict[str, str]]:
    rows = []
    for lineno, line in enumerate(REGISTER.read_text(encoding="utf-8").splitlines(), 1):
        m = _ROW_RE.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            rows.append({"line": str(lineno), "error": f"列数不足：{line}"})
            continue
        rows.append({
            "line": str(lineno),
            "rg": cells[0],
            "iss": cells[1],
            "mark": cells[2].strip("`"),
            "file": cells[3].strip("`"),
            "func": cells[4].strip("`"),
        })
    return rows


def main() -> int:
    code = collect_code()
    rows = collect_register()
    issues = {json.loads(l)["id"] for l in ISSUE_LOG.read_text(encoding="utf-8").splitlines() if l.strip()}
    fails: list[str] = []

    seen_rg: set[str] = set()
    registered: set[tuple[str, str]] = set()
    for r in rows:
        if "error" in r:
            fails.append(f"L{r['line']} {r['error']}")
            continue
        if r["rg"] in seen_rg:
            fails.append(f"L{r['line']} {r['rg']} 编号重复")
        seen_rg.add(r["rg"])
        key = (r["file"], r["func"])
        registered.add(key)
        # R1
        if key not in code:
            fails.append(f"R1 L{r['line']} {r['rg']} 函数不存在：{r['file']}::{r['func']}")
            continue
        # R2
        if r["mark"] not in GUARD_MARKS:
            fails.append(f"R2 L{r['line']} {r['rg']} 标记列非法：{r['mark']!r}")
        elif r["mark"] not in code[key]:
            fails.append(f"R2 L{r['line']} {r['rg']} 登记为 {r['mark']}，代码标记为 {sorted(code[key]) or '无'}")
        # R4
        for iss in re.findall(r"ISS-\d+", r["iss"]):
            if iss not in issues:
                fails.append(f"R4 L{r['line']} {r['rg']} {iss} 不在 issue-log")
        if not re.search(r"ISS-\d+|ADHOC", r["iss"]):
            fails.append(f"R4 L{r['line']} {r['rg']} 来源列既无 ISS 编号也无 ADHOC：{r['iss']!r}")

    # R3 孤儿
    for key, marks in sorted(code.items()):
        if marks and key not in registered:
            fails.append(f"R3 孤儿：{key[0]}::{key[1]} 带 {sorted(marks)} 但登记册无此行")

    by_mark = {m: sum(1 for k, v in code.items() if m in v) for m in sorted(GUARD_MARKS)}
    print(f"登记册行数 = {len(rows)}；代码中带护栏标记函数 = {sum(1 for v in code.values() if v)} {by_mark}")
    for f in fails:
        print("FAIL", f)
    print(f"RESULT {'PASS' if not fails else 'FAIL'} checks=R1..R4 failures={len(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
