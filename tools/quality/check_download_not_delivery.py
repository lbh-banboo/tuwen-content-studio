#!/usr/bin/env python
"""「下载不计交付」+「替换是追加」两条产品规则的 **AST 判据**（WP36）。

## 为什么必须是 AST 而不是文本 grep

两个方向都会被文本搜索骗：

| 方向 | 文本搜索的表现 | 真相 |
|---|---|---|
| 「`modules/exports/**` 不得引用 `deliveries`」 | `grep -r deliveries modules/exports` **恒红** —— `exports/models.py:23` 的 docstring 原文就写着「`deliveries` 属 WP36」 | docstring 不是代码；它说的恰恰是"我不碰那张表" |
| 「`delivered_at` 的赋值点恰好 1 处」 | `grep -c 'delivered_at'` 把**列声明**（`models.py` 的 `mapped_column`、`api.py` 的 pydantic `Field`）一起数进来，恒 > 1 | 列声明不是写入点 |

手法照 `content/repository.py:250-254` 的 AST 取证先例（`WP29开工方案:325` 原文：
「文本搜索会被 docstring 弄成恒绿」）。

## 判据

| # | 判据 | 抓什么 |
|---|---|---|
| **G1** | `modules/exports/**` 的**代码**里零 `deliveries` 引用：① 不 import `rednote.modules.delivery`；② 非 docstring 的字符串常量里没有 `\\bdeliveries\\b`（裸 SQL 表名）；③ 没有名为 `delivery` / `deliveries` 的标识符 | WP35 侧偷偷写了交付表（那会让「下载」有机会置 `delivered_at`） |
| **G2** | `modules/delivery/**` 里 `delivered_at` 的**写入点恰好 1 处** | 多一处 = 「下载不计交付」多了一个可能的后门；零处 = 标记交付功能根本没实现 |
| **G3** | `service.replace_delivery` 的 AST 子树里**不调用任何会 UPDATE `deliveries` 的仓储方法**、不出现 `sqlalchemy.update(` / `.values(`、字符串里没有裸 `UPDATE` | 替换退化成"覆盖旧行"（计划点名的本包唯一高危风险 R5） |
| **G4** | 活性下界：扫到的文件数 / `delivered_at` 候选节点数 / 变异列数都不为 0 | 判据自身的 AST 规则失效后会是"全通过"（ISS-143 同族） |
| **G5** | 「会改 `deliveries` 的仓储方法」集合**两个独立来源对账**：A（名字约定 `mark_*`）== B（行为：函数体里有 `update(`） | 有人加了个会 UPDATE 的方法但不叫 `mark_*` ⇒ G3 的 denylist 漏掉它，而 G3 仍"全通过"（铁律 14） |

G5 是本脚本最要紧的一条：G3 的 denylist 若由"名字"单方面决定，那么
`def touch_delivery(...)` 里写一条 UPDATE，G3 就看不见了。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = REPO_ROOT / "services" / "backend" / "src" / "rednote"
EXPORTS_DIR = BACKEND_SRC / "modules" / "exports"
DELIVERY_DIR = BACKEND_SRC / "modules" / "delivery"

#: 「交付」这件事的唯一写入列。
DELIVERED_AT = "delivered_at"
#: 期望的写入点数（不是下界 —— 多一处与少一处都要报）。
EXPECTED_DELIVERED_AT_WRITES = 1

#: 替换路径的函数名。
REPLACE_FUNCTION = "replace_delivery"

#: G3 的硬 denylist（与 G5 对账出来的仓储方法合并使用）。
FORBIDDEN_IN_REPLACE = frozenset({"update", "values"})

#: 活性下界。
MIN_EXPORT_FILES = 8
MIN_DELIVERY_FILES = 8

_DELIVERIES_TABLE = re.compile(r"\bdeliveries\b", re.IGNORECASE)
_BARE_UPDATE = re.compile(r"\bUPDATE\b")
_FORBIDDEN_IDENTIFIERS = frozenset({"delivery", "deliveries"})


def _py_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """全部 docstring 字符串常量的 `id()`（用来把它们从"代码"里排除）。"""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.add(id(body[0].value))
    return out


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


# ---------------------------------------------------------------------------
# G1
# ---------------------------------------------------------------------------


def check_exports_never_touches_deliveries(problems: list[str]) -> int:
    files = _py_files(EXPORTS_DIR)
    for path in files:
        tree = _parse(path)
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("rednote.modules.delivery"):
                        problems.append(
                            f"G1 {_rel(path)}:{node.lineno} import 了 {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").startswith("rednote.modules.delivery"):
                    problems.append(
                        f"G1 {_rel(path)}:{node.lineno} from {node.module} import …"
                    )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                if _DELIVERIES_TABLE.search(node.value):
                    problems.append(
                        f"G1 {_rel(path)}:{node.lineno} 代码里的字符串提到了 deliveries 表"
                        f"：{node.value[:60]!r}"
                    )
            elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_IDENTIFIERS:
                problems.append(f"G1 {_rel(path)}:{node.lineno} 标识符 {node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_IDENTIFIERS:
                problems.append(f"G1 {_rel(path)}:{node.lineno} 属性 .{node.attr}")
    return len(files)


# ---------------------------------------------------------------------------
# G2
# ---------------------------------------------------------------------------


def _class_body_ids(tree: ast.Module) -> set[int]:
    """直接位于 `ClassDef` body 里的语句的 `id()`（= 列/字段**声明**，不是写入点）。"""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            out.update(id(stmt) for stmt in node.body)
    return out


def count_delivered_at_writes(problems: list[str]) -> tuple[int, int]:
    """返回 `(写入点数, 扫到的候选节点数)`。

    写入点 =
      · `f(..., delivered_at=...)` 的关键字实参（ORM 构造与 `update().values()` 都是这个形状）；
      · `x.delivered_at = ...`（属性赋值）；
      · 函数体内 `delivered_at = ...`（**不含** `ClassDef` body 里的列/字段声明）。
    """
    writes: list[str] = []
    candidates = 0
    for path in _py_files(DELIVERY_DIR):
        tree = _parse(path)
        declarations = _class_body_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == DELIVERED_AT:
                candidates += 1
                writes.append(f"{_rel(path)} 关键字实参 {DELIVERED_AT}=")
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == DELIVERED_AT:
                        candidates += 1
                        writes.append(f"{_rel(path)}:{node.lineno} 属性赋值")
                    elif isinstance(target, ast.Name) and target.id == DELIVERED_AT:
                        candidates += 1
                        if id(node) in declarations:
                            continue  # 列/字段声明，不是写入点
                        writes.append(f"{_rel(path)}:{node.lineno} 局部赋值")
    if len(writes) != EXPECTED_DELIVERED_AT_WRITES:
        problems.append(
            f"G2 {DELIVERED_AT} 写入点 {len(writes)} 处，期望"
            f" {EXPECTED_DELIVERED_AT_WRITES} 处：{writes}"
        )
    return len(writes), candidates


# ---------------------------------------------------------------------------
# G5（先算，G3 要用它的结果）
# ---------------------------------------------------------------------------


def mutating_repository_methods(problems: list[str]) -> frozenset[str]:
    """会 UPDATE `deliveries` 的仓储方法，**两个独立来源对账**。

    A = 名字叫 `mark_*` 的方法（名字约定）
    B = 函数体里出现 `update(...)` 调用的方法（行为约定，AST 反推）
    """
    path = DELIVERY_DIR / "repository.py"
    tree = _parse(path)
    by_name: set[str] = set()
    by_behavior: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name.startswith("mark_"):
            by_name.add(node.name)
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                func = inner.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else None
                )
                if name == "update":
                    by_behavior.add(node.name)
    if by_name != by_behavior:
        problems.append(
            "G5 两来源不一致：名字约定 mark_* ="
            f" {sorted(by_name)}，行为约定（体内有 update()）= {sorted(by_behavior)}"
            " —— 有会改 deliveries 的方法没按 mark_* 命名，G3 的 denylist 会漏掉它"
        )
    return frozenset(by_name | by_behavior)


# ---------------------------------------------------------------------------
# G3
# ---------------------------------------------------------------------------


def check_replace_is_append_only(
    problems: list[str], mutating: frozenset[str]
) -> int:
    """替换路径里不得出现任何对旧行的 UPDATE。返回检查到的调用数（活性）。"""
    path = DELIVERY_DIR / "service.py"
    tree = _parse(path)
    target = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == REPLACE_FUNCTION
        ),
        None,
    )
    if target is None:
        problems.append(
            f"G3 在 {_rel(path)} 里找不到 {REPLACE_FUNCTION}() —— 判据无从施加"
        )
        return 0

    denylist = FORBIDDEN_IN_REPLACE | mutating
    docstrings = _docstring_nodes(tree)
    calls = 0
    for node in ast.walk(target):
        if isinstance(node, ast.Call):
            calls += 1
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name in denylist:
                problems.append(
                    f"G3 {_rel(path)}:{node.lineno} {REPLACE_FUNCTION} 调用了"
                    f" {name}() —— 替换必须只 INSERT，对旧行零 UPDATE"
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            if _BARE_UPDATE.search(node.value):
                problems.append(
                    f"G3 {_rel(path)}:{node.lineno} {REPLACE_FUNCTION} 的字符串里有裸"
                    f" UPDATE：{node.value[:60]!r}"
                )
    return calls


# ---------------------------------------------------------------------------
# G4 的一部分：冻结列派生必须非空
# ---------------------------------------------------------------------------


def frozen_column_count(problems: list[str]) -> int:
    """`models.frozen_delivery_columns()` 必须非空，否则列级冻结触发器是空壳。"""
    sys.path.insert(0, str(REPO_ROOT / "services" / "backend" / "src"))
    from rednote.modules.delivery.models import frozen_delivery_columns

    columns = frozen_delivery_columns()
    if not columns:
        problems.append(
            "G4 frozen_delivery_columns() 为空 —— 列级冻结触发器会生成空函数体，"
            "表变成'看起来不可变、实际全是后门'"
        )
    return len(columns)


def main() -> int:
    problems: list[str] = []

    print("[WP36] 「下载不计交付」/「替换是追加」AST 判据")
    print()

    export_files = check_exports_never_touches_deliveries(problems)
    print(f"  G1 modules/exports/** 零 deliveries 引用 —— 扫了 {export_files} 个文件")

    writes, candidates = count_delivered_at_writes(problems)
    print(
        f"  G2 delivered_at 写入点 = {writes}"
        f"（期望 {EXPECTED_DELIVERED_AT_WRITES}；候选节点 {candidates} 个）"
    )

    mutating = mutating_repository_methods(problems)
    print(f"  G5 会改 deliveries 的仓储方法（两来源一致）—— {sorted(mutating)}")

    calls = check_replace_is_append_only(problems, mutating)
    print(f"  G3 {REPLACE_FUNCTION} 只 INSERT —— 检查了 {calls} 个调用")

    frozen = frozen_column_count(problems)
    print(f"  G4b 冻结列派生 = {frozen} 列")

    delivery_files = len(_py_files(DELIVERY_DIR))
    print(f"  G4 活性：delivery {delivery_files} 文件 / exports {export_files} 文件")
    if export_files < MIN_EXPORT_FILES:
        problems.append(
            f"G4 只扫到 {export_files} 个 exports 文件（下界 {MIN_EXPORT_FILES}）"
            " —— 扫不到东西时本脚本会是'全通过'"
        )
    if delivery_files < MIN_DELIVERY_FILES:
        problems.append(
            f"G4 只扫到 {delivery_files} 个 delivery 文件（下界 {MIN_DELIVERY_FILES}）"
        )
    if candidates == 0:
        problems.append("G4 delivered_at 候选节点为 0 —— AST 规则已失效")

    print()
    if not problems:
        print(
            "[PASS] RESULT=PASS 下载路径触不到 deliveries；delivered_at 写入点唯一；"
            "替换路径零 UPDATE。"
        )
        return 0
    print(f"[FAIL] RESULT=FAIL 共 {len(problems)} 处问题：")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
