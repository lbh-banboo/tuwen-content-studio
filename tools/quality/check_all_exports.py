#!/usr/bin/env python
"""`__all__` 条目必须真的存在（铁律第 14 条的「公开面」形态）。

## 为什么需要这个（2026-10-07 实测）

WP27 落 `modules/media/types.py` 时，`__all__` 里手写了两个**模块体从未
定义**的名字（`MAX_IMAGE_PIXELS` —— 其实来源于别的模块；以及一个
`REASON_` 前缀写错的常量名）。本机当场跑「`__all__` ⊆ 模块命名空间」
才抓到，**此前全仓没有任何判据覆盖它**。

这类缺陷的三个特征，与 `check_undefined_globals.py` 记的那三个**恰好互补**：

  · `check_undefined_globals` 用的是 stdlib `symtable`，它判"名字**被引用**
    而模块级没定义"。而 `__all__` 里的是**字符串**，不是引用 ⇒
    `symtable` 的符号表里**根本不会出现**这两个名字（本机实测：源码
    `X = 1; __all__ = ["X", "NOT_DEFINED"]` 的符号表只有 `{'X','__all__'}`）。
    **两个工具是结构上的互补，不是重复。**
  · 全仓 172 个文件、2140 个 `__all__` 条目普查结果是 **A1 违规 0 例** ——
    也就是说这条缺陷**历史上从未发生过**，因此**从未有人写判据**。
    它今天发生了一次（我写的），所以今天补。
  · `__all__` 是**手写的公开面** ⇒ 属铁律第 14 条「凡覆盖范围由人写」，
    必须有一条判据对账它。本条就是那个对账。

## 四条判据

| # | 判据 | 抓什么 | 现状 |
|---|---|---|---|
| A1 | `__all__` 每个条目都**已绑定**在模块级 | 手写公开面时多写/写错名字 | 0 例 ⇒ 硬门禁 |
| A2 | `__all__` 无**重复**条目 | 手写列表时重复登记 | 0 例 ⇒ 硬门禁 |
| A3 | `__all__` 必须是**字面量**（list/tuple of str） | 变量拼装 ⇒ **本条判据对该文件失效** | 0 例 ⇒ 硬门禁 |
| A4 | 活性下界：被覆盖的文件数 / 条目数不低于阈值 | 判据自身 AST 规则失效后"全通过" | 见常量 |

A3 是**反 ISS-95** 的那一条：判据扫不到东西时最危险的形态是**静默通过**。
因此遇到非字面量 `__all__` 一律**报红**（要求改回字面量），
而不是"跳过"—— 跳过就等于该文件没人守。

### 为什么 A1 允许"来自 import 的名字"

`__all__` 里可以是 `from x import Y` 再转出的 `Y`（本仓有此用法，如
`service.py` 转出口）。所以 A1 判的是"**模块级是否绑定了这个名字**"
（`import` 也算绑定），**不是**"本模块是否自己定义了它"。
把后者当判据会误报一大片。

### 为什么"漏登记"方向不设硬门禁

反向（模块里自造的公开名未登记进 `__all__`）实测 **30 个文件 / 150 个名字**，
且绝大多数**本就不该登记**（`api.py` 的 7 个路由函数、各模块的 `logger`
—— 那是 `__all__` 自身的既有设计）。把它设成硬门禁需要 150 条豁免，
可维护性为零 ⇒ 改成 `--report-omissions` 的**诊断输出**（打印不报红），
定位是"公开面盘点"，**明确不进判据**。
漏登记方向的风险也小得多：它不影响任何运行时行为，只影响 `import *`。

跑法：
    python tools/quality/check_all_exports.py [路径...] [--report-omissions]
默认扫 `services/backend/src`、`services/backend/tests`、`tools`、`migrations`。
退出码 0 = 四条判据全过。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TARGETS = (
    REPO_ROOT / "services" / "backend" / "src",
    REPO_ROOT / "services" / "backend" / "tests",
    REPO_ROOT / "tools",
    REPO_ROOT / "migrations",
)

#: 活性下界（铁律：判据若因自身规则失效而"扫不到东西"，必须报红而不是静默全过）。
#: 定值的依据是 2026-10-07 实测 172 个文件 / 2140 个条目；
#: 取一个**明显低于现状**的数，只用于"AST 规则整体失效"这种灾难性回归，
#: 而不是"删了几个模块"这种正常演进（后者不该报红）。
MIN_ALL_FILES = 120
MIN_ALL_ENTRIES = 1500

#: **显式目录模式**的下界。分开定值的原因：显式传目录时（如变异测试、
#: 单模块核查）目标树本就很小，套全仓下界会让判据在**本该通过**的场景报红
#: —— 本机实测过：给单个文件跑，A4 报红把真正的 A1 报红**盖住了**。
#: 这两个值同时是反向验证的变异锚点（把它抬到 999 ⇒ A4 必须报红）。
MIN_SCOPED_FILES = 1
MIN_SCOPED_ENTRIES = 1

#: A1 的豁免：允许出现在 `__all__` 里但**确实不是模块级绑定**的名字。
#: **逐条给出理由**（与 `check_undefined_globals._IMPLICIT_GLOBALS` 同纪律）。
#: 现状为空 —— 保持为空是本判据有效性的前提。
EXEMPT_EXPORT_NAMES: Final[frozenset[str]] = frozenset()  # type: ignore[name-defined]

#: 递归进入的"不引入新作用域"的复合语句：
#: `if`/`try`/`with`/`for`/`while` 里的定义**仍是模块级绑定**
#: （`if TYPE_CHECKING:` 里 import 的名字就是典型）。
#: 而 `FunctionDef`/`ClassDef`/`Lambda` **会**引入新作用域，绝不递归进去。
_SCOPE_TRANSPARENT = (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)


class Found:
    """一条 A1/A2 违规。"""

    __slots__ = ("path", "kind", "names")

    def __init__(self, path: Path, kind: str, names: list[str]) -> None:
        self.path = path
        self.kind = kind
        self.names = names

    def describe(self) -> str:
        return f"{_rel(self.path)} —— {self.kind}：{'、'.join(self.names)}"


def _rel(path: Path) -> str:
    """仓内路径显示为相对路径；仓外（变异测试 / 临时文件）显示绝对路径。

    ⚠️ 这里**不能**直接 `path.relative_to(REPO_ROOT)`：传仓外路径时它抛
    `ValueError` —— 那是**未处理异常**，不是"报红"。判据在异常上崩掉，
    使用者看到的是 traceback、拿不到判定结论，比"报红"更难排查。
    （本机在变异测试时当场踩到。）
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _iter_py_files(targets: tuple[Path, ...]) -> list[Path]:
    """覆盖范围由**磁盘**决定（铁律 14）：rglob 全部 .py，不写文件名名单。"""
    out: list[Path] = []
    for target in targets:
        if target.is_file():
            out.append(target)
        elif target.is_dir():
            out.extend(target.rglob("*.py"))
    return sorted(p for p in set(out) if "__pycache__" not in p.parts)


def _bindings(body: list[ast.stmt], into: set[str]) -> None:
    """收集模块级**绑定**的名字（含 import），递归穿透不引入作用域的复合语句。"""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            into.add(node.name)  # 只收名字，不递归进去
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for tgt in targets:
                if isinstance(tgt, ast.Name):
                    into.add(tgt.id)
                elif isinstance(tgt, (ast.Tuple, ast.List)):
                    for elt in tgt.elts:
                        if isinstance(elt, ast.Name):
                            into.add(elt.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                into.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    into.add("*STAR*")  # 星号导入 ⇒ 名字集合不可判定，见 A3b
                else:
                    into.add(alias.asname or alias.name)
        elif isinstance(node, _SCOPE_TRANSPARENT):
            _bindings(node.body, into)
            _bindings(getattr(node, "orelse", []), into)
            _bindings(getattr(node, "finalbody", []), into)
            for handler in getattr(node, "handlers", []):
                _bindings(handler.body, into)


def _collect_all_assignments(tree: ast.Module) -> tuple[list[ast.AST], bool]:
    """找出所有 `__all__ = ...` / `__all__ += ...` 的赋值节点。"""
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            found.append(node)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == "__all__":
            found.append(node)
    return found, bool(found)


def _literal_entries(node: ast.AST) -> list[str] | None:
    """`__all__` 取值必须是字符串字面量序列；否则返回 None（⇒ A3 报红）。"""
    value = node.value  # type: ignore[attr-defined]
    try:
        literal = ast.literal_eval(value)
    except Exception:
        return None
    if isinstance(literal, (list, tuple)) and all(isinstance(x, str) for x in literal):
        return list(literal)
    return None


def main(argv: list[str]) -> int:
    report_omissions = "--report-omissions" in argv
    raw = [a for a in argv if not a.startswith("-")]
    targets = tuple(Path(a).resolve() for a in raw) if raw else DEFAULT_TARGETS

    # 覆盖范围由**磁盘**决定（铁律 14）：全仓模式扫默认目标；显式路径模式只扫给定路径。
    files = _iter_py_files(targets)
    if not raw and len(files) < MIN_ALL_FILES:
        print(
            f"[FAIL] RESULT=FAIL —— 默认目标只扫到 {len(files)} 个 .py 文件"
            f"（全仓下界 {MIN_ALL_FILES}）。覆盖范围由磁盘决定，"
            "这条前置说明『默认目标目录本身不见了』—— 判据会静默全过。"
        )
        return 1
    problems: list[str] = []
    reports: list[str] = []

    n_with_all = 0
    n_entries = 0
    a1_hits: list[Found] = []
    a2_hits: list[Found] = []
    a3_hits: list[str] = []
    star_files: list[str] = []

    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            problems.append(f"{_rel(path)} 无法解析：{exc}")
            continue

        assignments, has_all = _collect_all_assignments(tree)
        if not has_all:
            continue

        rel = _rel(path)
        bindings: set[str] = set()
        _bindings(tree.body, bindings)
        if "*STAR*" in bindings:
            star_files.append(str(rel))

        entries: list[str] = []
        for node in assignments:
            literal = _literal_entries(node)
            if literal is None:
                a3_hits.append(
                    f"{rel} —— `__all__` 不是字符串字面量序列"
                    "（变量拼装 / 非字符串元素）⇒ **本条判据对该文件失效**，"
                    "请改回字面量（ISS-95：判据扫不到时最危险的形态是静默通过）"
                )
                continue
            entries.extend(literal)

        if not entries:
            continue

        n_with_all += 1
        n_entries += len(entries)

        # ---- A1：多出（未绑定）
        missing = sorted({e for e in entries if e not in bindings and e not in EXEMPT_EXPORT_NAMES})
        if missing:
            a1_hits.append(Found(path, "条目未在模块级绑定", missing))

        # ---- A2：重复
        dups = sorted({e for e in entries if entries.count(e) > 1})
        if dups:
            a2_hits.append(Found(path, "条目重复", dups))

        # ---- 诊断：漏登记（不报红）
        if report_omissions:
            own_public = {
                n for n in bindings
                if not n.startswith("_") and n != "*STAR*"
            }
            omitted = sorted(own_public - set(entries))
            if omitted:
                reports.append(f"{rel} —— 未登记 {len(omitted)} 个：{'、'.join(omitted[:8])}")

    print(f"扫描目标   ：{len(files)} 个 .py 文件")
    print(f"含 __all__ ：{n_with_all} 个文件 / {n_entries} 个条目")
    print()

    a1_ok = not a1_hits
    print(f"  [{'PASS' if a1_ok else 'FAIL'}] A1 `__all__` 条目均已绑定 —— {len(a1_hits)} 处")
    for hit in a1_hits:
        print(f"         ★ {hit.describe()}")
    if not a1_ok:
        problems.append(
            f"{len(a1_hits)} 处 `__all__` 写了模块级不存在的名字"
            " —— `from x import *` 会当场 AttributeError，"
            "且任何『__all__ ⊆ 命名空间』校验立刻红（铁律 14 的公开面形态）"
        )

    a2_ok = not a2_hits
    print(f"  [{'PASS' if a2_ok else 'FAIL'}] A2 `__all__` 条目无重复 —— {len(a2_hits)} 处")
    for hit in a2_hits:
        print(f"         ★ {hit.describe()}")
    if not a2_ok:
        problems.append(f"{len(a2_hits)} 处 `__all__` 有重复条目")

    a3_ok = not a3_hits
    print(f"  [{'PASS' if a3_ok else 'FAIL'}] A3 `__all__` 均为字面量 —— {len(a3_hits)} 处")
    for hit in a3_hits:
        print(f"         ★ {hit}")
    if not a3_ok:
        problems.append(f"{len(a3_hits)} 处 `__all__` 非字面量 ⇒ 本判据对它们**失效**（不许静默）")

    if star_files:
        print(f"  [SKIP] 含 `from x import *` 的文件（名字集合不可判定）—— {len(star_files)} 个")
        for s in star_files:
            print(f"         · {s}")

    if not raw:
        floor_files, floor_entries = MIN_ALL_FILES, MIN_ALL_ENTRIES
        floor_desc = f"文件 >= {MIN_ALL_FILES} 且 条目 >= {MIN_ALL_ENTRIES}（全仓模式）"
    else:
        floor_files, floor_entries = MIN_SCOPED_FILES, MIN_SCOPED_ENTRIES
        floor_desc = (
            f"文件 >= {MIN_SCOPED_FILES} 且 条目 >= {MIN_SCOPED_ENTRIES}（显式路径模式）"
        )
    f4_ok = n_with_all >= floor_files and n_entries >= floor_entries
    print(
        f"  [{'PASS' if f4_ok else 'FAIL'}] A4 活性下界：{floor_desc}"
        f" —— {n_with_all} / {n_entries}"
    )
    if not f4_ok:
        problems.append(
            f"活性下界未达（{n_with_all} 个文件 / {n_entries} 个条目；{floor_desc}）"
            " —— 判据自身的 AST 规则可能已失效，扫不到东西时它会『全通过』"
        )

    if report_omissions:
        print()
        print(f"  —— 诊断：漏登记盘点（{len(reports)} 个文件，**不报红**，仅供公开面盘点）")
        for line in reports:
            print(f"     · {line}")

    print()
    if not problems:
        # ⚠️ 这里必须用**实际生效的**那组下界（`floor_files` / `floor_entries`），
        # 不能硬写 `MIN_ALL_*`：显式路径模式下实际判的是 `MIN_SCOPED_*`，
        # 硬写全仓常量会让输出与判据行为不一致 —— 而"总结句与判据说的不是
        # 同一件事"正是本仓反复踩的那类形态（ISS-51：这两个数数的是同一种
        # 东西吗）。工具自己的输出更要先过这一关。
        print(
            f"[PASS] {n_with_all} 个文件的 `__all__` 全部自洽"
            f"（{n_entries} 条目，{floor_desc}：{floor_files}/{floor_entries}）。"
        )
        return 0
    print(f"[FAIL] RESULT=FAIL 共 {len(problems)} 处问题：")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))