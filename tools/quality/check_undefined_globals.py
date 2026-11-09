#!/usr/bin/env python
"""未定义全局名检查（stdlib `symtable` 实现的 F821 子集，零第三方依赖）。

**为什么需要它**（2026-09-26 实测）：WP22 收口后全量 pytest 是 **10 failed /
1501 passed**，10 条全是同一个根因 —— `service.py` 的 `from .baseline_types import (...)`
清单按字母序排，**漏了 `REASON_DECISION_INVALID` 一行**，而它在
`_require_decision()` 的非法分支里被引用。该常量在 `baseline_types.py:291`
定义了、也在 `__all__` 里，只是没被导入 → 只在"传非法 decision"这条路径上
`NameError`。

这类缺陷的三个特征，决定了**跑测试抓不住它**：

  1. `NameError` 是**运行到那一行**才抛的，静态导入期完全正常；
  2. 触发它需要走到**特定的错误分支**（正常路径 100% 绿）；
  3. 全仓 1501 条测试里只有 10 条走到那个分支 —— 而**没有人跑全量**
     （没有 CI，收口时也没复跑），于是它静静躺了整整一个工作包。

机械判据：符号在某个作用域里**被引用**、且解析到**模块级**、但模块级从未
定义它、也不是内建名 → 报错。这正是 `symtable` 能直接回答的问题。

**已知不适用**（脚本会显式打印 SKIP，不静默放过）：
  · `from x import *` —— 星号导入让名字集合不可判定，整文件跳过；
  · `exec` / `eval` / `globals()` —— 动态注入，整文件跳过。

跑法：
    python tools/quality/check_undefined_globals.py [路径...]
默认扫 `services/backend/src`、`services/backend/tests`、`tools`、`migrations`。
退出码 0 = 无未定义全局名。
"""

from __future__ import annotations

import ast
import builtins
import symtable
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TARGETS = (
    REPO_ROOT / "services" / "backend" / "src",
    REPO_ROOT / "services" / "backend" / "tests",
    REPO_ROOT / "tools",
    REPO_ROOT / "migrations",
)

# 动态特性会让"名字集合"不可判定 —— 见到就整文件跳过并打印原因，
# 而不是静默放过（ISS-95：静默的"通过"比失败更危险）。
# 判据见 _is_unsound()。
#
# 解释器隐式注入的模块级全局：`symtable` 看不到它们（它们不在源码里被赋值），
# 但它们在任何模块里都真实存在。不白名单会报 25 处 `__file__` 假阳性 ——
# 假阳性会让人放弃跑这个脚本，所以这条白名单不是"宽松"，是"必要精度"。
_IMPLICIT_GLOBALS = frozenset({
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__dict__", "__annotations__", "__cached__",
})


def _is_unsound(src: str) -> str | None:
    """用 AST 判"名字集合不可判定"，只认真实语法节点。

    不用字符串搜索：本文件自己的 docstring 里就写着 `globals()`，
    字符串搜索会把它自己判成不可判定 —— 一个把文档当代码的判据，
    在别的文件上同样会误判（ISS-95 同族：判据必须落在真实结构上）。
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if any(alias.name == "*" for alias in node.names):
                return "星号导入（import *）"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fname = node.func.id
            # `eval` / `exec` 任何调用都是动态求值；`globals` / `locals` 是
            # 名字表泄漏；而 `vars(obj)` 只是普通访问（`vars()` 无参才等价
            # `locals()`）—— 一律当成注入会把普通测试代码误判成盲区。
            if fname in {"eval", "exec"}:
                return f"{fname}() 动态求值"
            if fname in {"globals", "locals"} or (fname == "vars" and not node.args):
                return f"{fname}() 名字表泄漏"
    return None


def _defined_names(table: symtable.SymbolTable) -> set[str]:
    out: set[str] = set()
    for sym in table.get_symbols():
        if (
            sym.is_assigned()
            or sym.is_imported()
            or sym.is_namespace()
            or sym.is_parameter()
        ):
            out.add(sym.get_name())
    return out


def _walk(table: symtable.SymbolTable, module_names: set[str], path: Path, out: list[str]) -> None:
    lineno = table.get_lineno()
    for sym in table.get_symbols():
        name = sym.get_name()
        if not sym.is_referenced():
            continue
        # 只在"解析到模块级"的作用域里判；局部/闭包变量由 symtable 自己管。
        if table.get_type() == "module":
            is_module_level = True
        else:
            is_module_level = sym.is_global() and not sym.is_local() and not sym.is_free()
        if not is_module_level:
            continue
        if name in module_names or name in _IMPLICIT_GLOBALS or hasattr(builtins, name):
            continue
        kind = "模块顶层" if table.get_type() == "module" else f"{table.get_type()} {table.get_name()}"
        out.append(f"{path}:{lineno}: {name} 未定义（{kind} 引用）")
    for child in table.get_children():
        if child.get_type() == "class":
            # 类体本身是新作用域，但方法体内对模块级名字的引用仍走 module 解析，
            # 此处直接递归子节点即可（类体符号表的 is_global 行为不参与判据）。
            for gchild in child.get_children():
                _walk(gchild, module_names, path, out)
            continue
        _walk(child, module_names, path, out)


def check_file(path: Path) -> tuple[list[str], str | None]:
    src = path.read_text(encoding="utf-8")
    # 先建符号表（顺带兜住语法错误），再判"名字集合是否可判定" ——
    # 语法不过的文件 _is_unsound 里的 ast.parse 会重复抛同一个异常。
    try:
        table = symtable.symtable(src, str(path), "exec")
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}: SyntaxError: {exc.msg}"], None
    reason = _is_unsound(src)
    if reason:
        return [], reason
    out: list[str] = []
    _walk(table, _defined_names(table), path, out)
    return out, None


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv[1:]] or list(DEFAULT_TARGETS)
    files: list[Path] = []
    for t in targets:
        files.extend(sorted(t.rglob("*.py")) if t.is_dir() else [t])
    files = [f for f in files if "__pycache__" not in f.parts]

    problems: list[str] = []
    skipped: list[str] = []
    for f in files:
        found, reason = check_file(f)
        problems.extend(found)
        if reason:
            skipped.append(f"{f.relative_to(REPO_ROOT)}（{reason}）")

    print(f"扫描 {len(files)} 个文件；跳过 {len(skipped)} 个：")
    for s in skipped:
        print("  SKIP", s)
    for p in problems:
        print("FAIL", p)
    print(f"RESULT {'PASS' if not problems else 'FAIL'} checks=undefined_globals failures={len(problems)}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
