#!/usr/bin/env python
"""判据 F：`importlib` 按路径加载模块时，**必须**先把模块注册进 `sys.modules`。

## 为什么需要这个判据（ISS-164，2026-10-01 实测）

CPython 3.13 的 `dataclasses._is_type()` 为了解析 `ClassVar` / `InitVar` 注解，
会执行：

```python
ns = sys.modules.get(cls.__module__).__dict__
```

`importlib.util.module_from_spec()` **只构造模块对象，不注册** ——
`sys.modules` 里没有它 ⇒ `sys.modules.get(...)` 返回 `None` ⇒
`AttributeError: 'NoneType' object has no attribute '__dict__'`。

### 本仓实测到的后果（不是"可能"，是"已经"）

`tools/m1/rerun_all_evidence.py` 的 `_assert_shared_db_quiet()` 用
`module_from_spec` 加载 `tools/quality/check_shared_db_quiet.py`（内含 2 个
`@dataclass`），**没注册** ⇒ 抛 `AttributeError` ⇒ 被那层
`except Exception` 兜成"拒跑"：

```
**FAIL** 静止前提**无法确认**（AttributeError: 'NoneType' object has no attribute '__dict__'）⇒ 按 fail-closed 拒跑
```

于是 **收口第 11 步（复跑全部历史证据）从未真正跑通一次**，而证据文件里
写的拒跑原因还是「目标库上有他人连接」—— 当时 PG 上**零连接**。

三个缺陷叠在一起，每一个单独看都不致命，合起来让一条纪律**静默失效**：

| # | 形态 | 纪律 |
|---|---|---|
| ① | 加载器漏注册 | 本次新增（ISS-164） |
| ② | `except Exception` 把"工具坏了"与"环境不满足"打印成同一句话 | ISS-143（"没跑"与"跑失败"同形） |
| ③ | 注释里**写明了**这个坑（`collect_discipline_reverse_evidence.py`），却漏在唯一真正生效的路径上 | ISS-95（"声称自己有防线"比"没有防线"更危险） |

### 为什么"当前不报错"不等于"没问题"

全仓 8 处 `spec_from_file_location`，其中 `check_evidence_scope.py` 也漏注册
—— 它当时**不报错**，只因为它加载的 `rerun_all_evidence.py` /
`check_evidence_scripts.py` 恰好都没有 `@dataclass`。
**这是一颗定时炸弹**：哪天被加载的模块引入 dataclass（或给现有类加个
`field(default_factory=...)`），炸的是**那一刻在改别的包的人**。

## 判据 F 的做法（铁律 14：范围由磁盘决定）

不维护"哪些文件要检查"的名单 —— 用 AST 扫**全仓** `*.py`，凡是调用了
`spec_from_file_location` 的函数体，就要求同一个作用域内出现
`sys.modules[...] = ...`（或 `setdefault`）。范围 100% 来自磁盘。

| # | 判据 | 含义 |
|---|---|---|
| F1 | **每个加载点都注册** | 主判据。扫到 N 个加载点，每个都必须注册 |
| F2 | **扫到的加载点 >= 5**（活性下界） | 防"AST 规则失效导致扫到 0 个"被当成"全通过"（ISS-143 同族） |
| F3 | **AST 解析失败必须报红** | 语法错误的文件不能被静默跳过（那会让它躲过 F1） |

跑法：

    python tools/quality/check_loader_registration.py

退出码 0 = 全部加载点都注册了 `sys.modules`。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 扫哪些后缀（本仓只有 Python 用 importlib，但写成元组便于将来扩）。
SUFFIXES = (".py",)

#: 跳过这些目录（不参与判据，且它们是第三方/生成物）。
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".workbuddy",
        "site-packages",
    }
)

#: 加载点的活性下界。**刻意写下界而不是等式**（铁律 1）：删除一个加载点
#: 是合法重构，不能因此报红；但"扫到 0 个"必须报红 —— 那说明判据自身失效了。
#: 2026-10-01 实测为 8，取下界 5（留出合理的重构余量）。
MIN_LOAD_SITES = 5

#: 触发本判据的调用名（`importlib.util.*` 或 `_ilu.*` 都算，只看属性名）。
_LOAD_FUNC = "spec_from_file_location"
#: 注册动作的判定：`sys.modules[...] = ...` 或 `sys.modules.setdefault/update`。
_REGISTER_ATTRS = frozenset({"setdefault", "update"})


class LoadSite:
    """一个 `spec_from_file_location` 调用点。"""

    __slots__ = ("rel", "line", "func", "registered", "module_var")

    def __init__(self, rel: str, line: int, func: str, registered: bool) -> None:
        self.rel = rel
        self.line = line
        self.func = func
        self.registered = registered

    def describe(self) -> str:
        return f"{self.rel}:{self.line}（{self.func}）"


def _iter_py_files() -> list[Path]:
    """磁盘决定范围：全仓 `*.py`，跳过 SKIP_DIRS。"""
    out: list[Path] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in SUFFIXES:
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        out.append(path)
    return out


def _sys_aliases(tree: ast.AST) -> set[str]:
    """收集绑定到 `sys` 模块的**所有本地名字**（`import sys` / `import sys as _sys`）。

    ⚠️ **为什么必须做别名解析**（判据 F 自己的一次假红，2026-10-01）：
    首版只认 `ast.Name(id="sys")`，于是
    `collect_dependency_declaration_code_review.py` 里的
    `import sys as _sys … _sys.modules["_cdd_cr"] = mod` 被判成"未注册" ——
    **它其实注册了**。那是判据的假红，而假红和漏报同样是缺陷：
    它会让 CI 常红，然后有人去"关掉这条判据"，防线就真的没了（ISS-95）。

    判据必须按"**这个事实是什么**"判，而不是按"**它长什么样**"判（ISS-82 同族）。
    """
    names: set[str] = {"sys"}  # `sys` 自身也算（未 import 时的隐式假定由 F3 兜）
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sys":
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            # `from sys import modules as _mods` 这类也纳入
            if node.module == "sys":
                for alias in node.names:
                    names.add(alias.asname or alias.name)
    return names


def _is_sys_modules_store(node: ast.AST, sys_aliases: set[str]) -> bool:
    """`sys.modules[...] = ...` / `sys.modules.setdefault(...)` ?（含别名）

    `sys_aliases` 是绑定到 `sys` 的本地名字集合（见 `_sys_aliases`）。
    """
    def _is_modules_attr(value: ast.AST) -> bool:
        return (
            isinstance(value, ast.Attribute)
            and value.attr == "modules"
            and isinstance(value.value, ast.Name)
            and value.value.id in sys_aliases
        )

    if isinstance(node, ast.Assign):
        return any(
            isinstance(target, ast.Subscript) and _is_modules_attr(target.value)
            for target in node.targets
        )
    if isinstance(node, ast.Call):
        func = node.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr in _REGISTER_ATTRS
            and _is_modules_attr(func.value)
        )
    return False


def _enclosing_spans(tree: ast.AST) -> list[tuple[int, int, str]]:
    """所有函数/模块级的 (起行, 止行, 名称)，用于把加载点归属到作用域。"""
    spans: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.append((node.lineno, node.end_lineno or node.lineno, node.name))
    return spans


def _scan_file(path: Path) -> tuple[list[LoadSite], str | None]:
    """扫一个文件，返回 (加载点列表, 解析错误说明)。"""
    rel = str(path.relative_to(REPO_ROOT))
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        # F3：解析失败**必须报红**，不能静默跳过（否则它躲过 F1）。
        return [], f"{rel} 无法解析（{type(exc).__name__}: {exc}）"

    spans = _enclosing_spans(tree)
    line_count = len(source.splitlines())
    sys_aliases = _sys_aliases(tree)
    sites: list[LoadSite] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == _LOAD_FUNC):
            continue

        # 归属到**最内层**（跨度最小）的包裹函数；模块级的加载点取 `<module>`。
        enclosing = [
            (end - start, start, end, name)
            for start, end, name in spans
            if start <= node.lineno <= end
        ]
        if enclosing:
            _, lo, hi, func_name = min(enclosing)
        else:
            func_name, lo, hi = "<module>", 1, line_count

        # 注册判定：**同一作用域内**出现 `sys.modules[...] = ...` / `setdefault`（含别名）。
        registered = any(
            _is_sys_modules_store(sub, sys_aliases)
            for sub in ast.walk(tree)
            if lo <= getattr(sub, "lineno", -1) <= hi
        )
        sites.append(LoadSite(rel, node.lineno, func_name, registered))

    return sites, None


def main() -> int:
    print("判据 F：importlib 加载点必须注册 sys.modules（ISS-164）")
    print()

    all_sites: list[LoadSite] = []
    problems: list[str] = []

    for path in _iter_py_files():
        sites, err = _scan_file(path)
        if err:
            problems.append(err)
            print(f"  [FAIL] {err}")
            continue
        all_sites.extend(sites)

    print(f"  扫到 {len(all_sites)} 个 `{_LOAD_FUNC}` 调用点")
    print()

    unregistered = [site for site in all_sites if not site.registered]
    for site in all_sites:
        mark = "OK  " if site.registered else "FAIL"
        print(f"  [{mark}] {site.describe()}")

    print()

    # ---- F1：每个加载点都必须注册
    f1_ok = not unregistered
    print(
        f"  [{'PASS' if f1_ok else 'FAIL'}] F1 每个加载点都注册 sys.modules"
        f" —— 未注册 {len(unregistered)} 个"
    )
    if unregistered:
        for site in unregistered:
            print(f"         ★ {site.describe()}")
        problems.append(
            f"{len(unregistered)} 个加载点未注册 sys.modules："
            + "、".join(s.describe() for s in unregistered)
            + " —— 被加载模块若含 @dataclass，CPython 3.13 会抛 "
            "AttributeError: 'NoneType' object has no attribute '__dict__'（ISS-164）"
        )

    # ---- F2：活性下界
    f2_ok = len(all_sites) >= MIN_LOAD_SITES
    print(
        f"  [{'PASS' if f2_ok else 'FAIL'}] F2 加载点 >= {MIN_LOAD_SITES}（活性下界）"
        f" —— {len(all_sites)} 个"
    )
    if not f2_ok:
        problems.append(
            f"只扫到 {len(all_sites)} 个加载点（下界 {MIN_LOAD_SITES}）"
            " —— 判据自身的 AST 规则可能已失效，它扫不到东西时会是'全通过'"
        )

    # ---- F3
    f3_ok = not any("无法解析" in p for p in problems)
    print(f"  [{'PASS' if f3_ok else 'FAIL'}] F3 全部文件可解析")

    ok = not problems
    print()
    if ok:
        print(
            f"[PASS] 全部 {len(all_sites)} 个加载点均已注册 `sys.modules`"
            f"（活性下界 {MIN_LOAD_SITES}）。"
        )
        return 0
    print(f"[FAIL] RESULT=FAIL 共 {len(problems)} 处问题：")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
