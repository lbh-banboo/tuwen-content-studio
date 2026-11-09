#!/usr/bin/env python
"""`*_node_paired` 触发器**必须有"放行守卫"且顺序正确**（ISS-175 的判据化）。

## 为什么需要这个（这条纪律历史上被咬了两次，都是"本包全绿、别包全红"）

`version_nodes` 是**共用表**。每个包都在它上面挂一个
`CREATE CONSTRAINT TRIGGER ... WHEN (NEW.entity_type = '<自己的>')`，
函数里是**两道判断**：

```sql
BEGIN
    -- 第 1 道：放行"不归我管"的节点（跨包契约）
    IF NOT EXISTS (
        SELECT 1 FROM <本包身份表> WHERE id = NEW.entity_id
    ) THEN
        RETURN NULL;
    END IF;
    -- 第 2 道：判配对是否存在
    IF NOT EXISTS (
        SELECT 1 FROM <本包领域表> WHERE id = NEW.id
    ) THEN
        RAISE EXCEPTION ... ;
    END IF;
END;
```

**第 1 道的作用**：别的包（含 WP11 的通用用例）会注册一个 `<X>` 类型的节点，
而**没有**对应的本包身份行。那不是"孤立节点"，是"不归我管"。去掉第 1 道
⇒ 别的包的测试当场红。

**顺序的作用**：两道的逻辑是"先排除不归我管的，再判我的"，写反了第 1 道
永远不可达。

### 实测的两次事故（都不是假设）

| # | 包 | 形态 | 症状 |
|---|---|---|---|
| 1 | WP25 | 第 5 个 `*_node_paired` 漏了第 1 道 | **WP11 既有 2 例红**，本包全绿 |
| 2 | WP26 | 第一版同样漏了第 1 道 | 同上（本包文档 §1.6 R8 已列为"最容易漏的一条"） |

两次的共同特征 —— 也正是它至今**没有任何判据**的原因：

> **漏掉时，本包的测试全绿；红的是别的包的测试。**
> 而那时排障的人会先怀疑"别的那个包最近改了什么"。

### 为什么现有四个工具都抓不到（第 15 条纪律：先证明盲区，再造工具）

| 工具 | 它判什么 | 为什么抓不到 |
|---|---|---|
| `check_all_exports.py` | `__all__` 条目是否已绑定 | 只看**名字**，不看函数体 |
| `check_undefined_globals.py` | 名字被引用而模块级未定义 | `RETURN NULL` 是**合法 plpgsql 字符串常量**，symtable 里什么都没有 |
| `check_evidence_scope.py` | 证据脚本覆盖范围 | 与 DDL 内容无关 |
| `collect_wp26_evidence.py` 的 C7/C8 | **运行期**探针（拦/放行成对） | 只覆盖**它自己那一个**触发器；且要连真库 |

⇒ 缺口是**静态结构层面**的：**没有任何东西**看着"第 7 个包会不会漏第 1 道"。
WP27 之后还有 7 个包要挂 `version_nodes`，靠人记必然复发（这就是 ISS-175 报红
时写下的处置原则：**不要停在改数字，要改工具**）。

## 判据

| # | 判据 | 抓什么 |
|---|---|---|
| G1 | 每个配对函数都有**放行守卫** | 漏第 1 道（ISS-175 本体） |
| G2 | 守卫在配对判定**之前** | 两道顺序写反 ⇒ 守卫不可达 |
| G3 | 配对判定 + `_raise_block(...)` 都在 | 只放行不拦截 = 触发器成了摆设 |
| G4 | 触发器 DDL 有 `WHEN (NEW.entity_type = ...)` 收窄 | 没收窄 ⇒ 误拦别的包的节点 |
| G5 | **两个独立来源对账**：A（名字约定）== B（行为约定） | 改名后静默漏（铁律 14） |
| G6 | 守卫表名 ∈ 全仓 `__tablename__` 字面量集合 | 守卫表名拼错 ⇒ `relation does not exist` 延迟到运行时才炸 |
| G7 | 活性下界：配对函数 >= `MIN_PAIRED` | 判据自身 AST 规则失效后"全通过"（ISS-143 同族） |

### G5 是铁律 14 的落地：覆盖范围不许由"名字"单方面决定

```
A = { 函数 | 它叫 *_node_paired_ddl }                 ← 名字约定
B = { 函数 | 它体内出现 "AFTER INSERT ON version_nodes" }  ← 行为约定（AST 反推）
```

两者是**独立事实**（前者由"叫什么"决定，后者由"做什么"决定）。
`A == B` 才能挡住两个方向的静默漏：

· 换个名字（如 `media_nodes_guard_ddl`）⇒ A 少一个 ⇒ 报红；
· 名字对但忘挂到 `version_nodes`（挂错表）⇒ B 少一个 ⇒ 报红。

若只用 A，改名是静默的；只用 B，则"函数体里恰好写了那句话但没生成触发器"
也会被当成通过。**两者相遇**才是判据（判据纪律第 4 条：两件独立事实不可
互相替代）。

## 不做假保证（已知盲区，写在这里而不是假装没有）

本判据是**纯 AST**，因此：

  · 它判的是**字符串常量**。若某包把守卫段写成 f-string 插值
    （`f"SELECT 1 FROM {TABLE} ..."`），静态拼不出表名 ⇒ **G1 报红**
    （要求改回字面量）。这是刻意的：扫不到就报红，不静默 —— ISS-95。
    本仓 6 个包实测**全部是字面量**（G6 的实测值 6/6 是活证据）。
  · 它判不了 **`entity_type` 的值对不对**（那行是 f-string 插值，静态取不到
    值）。值的正确性由**运行期**守：各包的证据脚本（如 WP26 的 C7/C8、
    WP27 的同款成对探针）会在真库上验"该拦的拦、该过的过"。
    **静态判结构与顺序，运行期判值** —— 两者互补，本条不重复造后者。

跑法：
    python tools/quality/check_node_paired_guard.py
退出码 0 = 七条判据全过。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 扫哪些后缀。
SUFFIXES = (".py",)

#: 跳过这些目录（第三方 / 生成物 / 证据文本）。
SKIP_DIRS: Final[frozenset[str]] = frozenset(
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
        "evidence",
    }
)

#: A 侧的命名约定后缀。
_PAIRED_SUFFIX = "_node_paired_ddl"

#: B 侧的行为标记：触发器挂在共用表 `version_nodes` 上。
#: 用**常量**而不是函数名 —— 这正是 B 与 A 相互独立的原因。
_PAIRED_MARK = "AFTER INSERT ON version_nodes"

#: 放行守卫的签名（第 1 道）。新行用 `\s*` 吞掉拼接带来的换行与缩进。
#: 匹配 `IF NOT EXISTS ( SELECT 1 FROM <表> WHERE id = NEW.entity_id ) THEN RETURN NULL;`
_GUARD_RE = (
    r"IF NOT EXISTS\s*\(\s*SELECT 1 FROM (\w+)\s+WHERE id = NEW\.entity_id\s*\)"
    r"\s*THEN\s*RETURN NULL;"
)

#: 配对判定的签名（第 2 道）。
_PAIR_RE = r"IF NOT EXISTS\s*\(\s*SELECT 1 FROM (\w+)\s+WHERE id = NEW\.id\s*\)"

#: WHEN 收窄的前缀（值部分是 f-string 插值，静态取不到值，只判"有没有"）。
_WHEN_PREFIX = "WHEN (NEW.entity_type ="

#: `_raise_block` 的私有约定名（本仓 6 个包一致）。改动它必须同步本判据。
_RAISE_HELPER = "_raise_block"

#: 活性下界。**下界而非等式**（铁律 1）：删一个包是合法重构，不该报红；
#: 但"扫到 0 个"说明判据自身的 AST 规则失效了，必须报红。
#: 2026-10-07 实测 6 个（brief / knowledge / baseline / content / prompts / media）。
MIN_PAIRED = 6

#: 全仓 `__tablename__` 字面量集合的活性下界（G6 的输入）。
#: 2026-10-07 实测 66 张。取明显低于现状的值 —— 它只用于"G6 的输入来源整体
#: 失效"这种灾难性回归（那样 G6 会把所有守卫表误报为"不在表集合里"）。
MIN_TABLES = 40

#: 已知盲区的显式登记：若将来某包的守卫段写成非字面量，把它加进来**必须**
#: 同时写理由，并且该文件从此不再受 G1 保护 —— 因此当前为空。
#: **空是刻意的**（不是忘了填）。
EXEMPT_FILES: Final[dict[str, str]] = {}


class PairedFunc:
    """一个 `*_node_paired` 触发器生成函数。"""

    __slots__ = (
        "rel",
        "name",
        "lineno",
        "text",
        "in_a",
        "in_b",
        "guard_table",
        "pair_table",
        "guard_before_pair",
        "has_pair",
        "has_raise",
        "has_when",
    )

    def __init__(self, rel: str, name: str, lineno: int, text: str, in_a: bool, in_b: bool) -> None:
        self.rel = rel
        self.name = name
        self.lineno = lineno
        self.text = text
        self.in_a = in_a
        self.in_b = in_b
        self.guard_table: str | None = None
        self.pair_table: str | None = None
        self.guard_before_pair = False
        self.has_pair = False
        self.has_raise = False
        self.has_when = False

    @property
    def where(self) -> str:
        return f"{self.rel}:{self.lineno}（{self.name}）"


def _iter_py_files() -> list[Path]:
    """覆盖范围由**磁盘**决定（铁律 14）：全仓 `*.py`，不写文件名名单。"""
    out: list[Path] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if path.suffix not in SUFFIXES or not path.is_file():
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        out.append(path)
    return out


def _docstring_node(func: ast.FunctionDef) -> ast.Constant | None:
    """函数的 docstring 节点（若有）。

    ⚠️ **必须排除它**：docstring 也是 `ast.Constant`，会被 `ast.walk` 收进
    字符串常量集合。本仓的 `*_node_paired_ddl` docstring 里**恰恰**写着
    「挂 `version_nodes` 是刻意的」这类话 —— 虽然当前不含 B 侧的完整标记
    （`AFTER INSERT ON version_nodes`），但那是**巧合**，不是设计。
    判据不能靠巧合成立。
    """
    if not func.body:
        return None
    first = func.body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return first.value
    return None


def _string_consts(func: ast.FunctionDef) -> str:
    """函数体内**全部字符串常量**按位置排序后拼接（排除 docstring）。

    ⚠️ 为什么按 `(lineno, col_offset)` 排序而不是直接 `ast.walk` 顺序：
    拼接出的文本要与**源码书写顺序**一致，否则"守卫在配对判定之前"这个
    **顺序判据**（G2）会因为遍历顺序而与源码实际顺序脱节。

    排序键用 `(lineno, col_offset)`：同一行上的多个常量按列序，跨行按行序。
    """
    skip = _docstring_node(func)
    items: list[tuple[int, int, str]] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if node is skip:
            continue
        items.append((node.lineno, node.col_offset, node.value))
    items.sort()
    return "".join(text for _, _, text in items)


def _calls_helper(func: ast.FunctionDef, helper: str) -> bool:
    """函数体内是否**引用了** `helper`（本仓约定：`_raise_block(...)`）。

    ⚠️ **不能用字符串常量判它** —— 这是本判据初版的一处假红（2026-10-07 实测）：
    `_raise_block` 是**外层 Python 函数**，它的 SQL 字符串在 `_raise_block`
    **定义处**，不在 `*_node_paired_ddl` 体内。函数体内只看得到
    `+ _raise_block(` 这个**引用**（`ast.Name`），字符串常量集合里没有它。
    初版用 `helper in text` 判 ⇒ 6/6 全部误报"缺 `_raise_block`"。

    这正是判据自己的纪律：**按事实判，不按长相判**。
    要判"调用了某个 Python 函数"，就得看 AST 里的名字引用。
    """
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and node.id == helper:
            return True
    return False


def _collect(files: list[Path]) -> tuple[list[PairedFunc], set[str], list[str]]:
    """扫全部文件，返回 (配对函数列表, 全仓表名集合, 解析错误)。

    同时用 **A（名字约定）** 与 **B（行为约定）** 两个独立来源标记每个函数，
    供 G5 对账。
    """
    found: list[PairedFunc] = []
    tables: set[str] = set()
    errors: list[str] = []

    for path in files:
        rel = str(path.relative_to(REPO_ROOT))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            # 解析失败**必须报红**，不能静默跳过（否则该文件躲过全部判据）。
            errors.append(f"{rel} 无法解析（{type(exc).__name__}: {exc}）")
            continue

        # 全仓表名（G6 的输入来源）。
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id == "__tablename__"
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)
                    ):
                        tables.add(node.value.value)

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            text = _string_consts(node)  # type: ignore[arg-type]
            in_a = node.name.endswith(_PAIRED_SUFFIX)
            in_b = _PAIRED_MARK in text
            if not (in_a or in_b):
                continue
            func = PairedFunc(rel, node.name, node.lineno, text, in_a, in_b)

            guard = re.search(_GUARD_RE, text)
            pair = re.search(_PAIR_RE, text)
            if guard:
                func.guard_table = guard.group(1)
            if pair:
                func.pair_table = pair.group(1)
                func.has_pair = True
            if guard and pair:
                func.guard_before_pair = guard.start() < pair.start()
            func.has_raise = _calls_helper(node, _RAISE_HELPER)
            func.has_when = _WHEN_PREFIX in text
            found.append(func)

    return found, tables, errors


def main() -> int:
    print("判据：`*_node_paired` 触发器必须有放行守卫且顺序正确（ISS-175）")
    print()

    files = _iter_py_files()
    funcs, tables, errors = _collect(files)
    funcs.sort(key=lambda f: (f.rel, f.lineno))

    print(f"  扫描 {len(files)} 个 .py 文件")
    print(f"  全仓 __tablename__ 字面量：{len(tables)} 张")
    print(f"  配对触发器生成函数：{len(funcs)} 个")
    print()
    print(f"  {'文件:行':<58} {'函数':<30} {'守卫表':<20} {'配对表':<22} {'顺序':<5} {'RAISE':<6} {'WHEN'}")
    print("  " + "-" * 152)
    for func in funcs:
        mark = "" if (func.in_a and func.in_b) else "  ★A/B 不一致★"
        print(
            f"  {func.rel + ':' + str(func.lineno):<58}"
            f" {func.name:<30}"
            f" {(func.guard_table or '★缺★'):<20}"
            f" {(func.pair_table or '★缺★'):<22}"
            f" {('是' if func.guard_before_pair else '★否★'):<5}"
            f" {('有' if func.has_raise else '★无★'):<6}"
            f" {('有' if func.has_when else '★无★')}{mark}"
        )
    print()

    problems: list[str] = []

    # ---- G1：放行守卫
    g1_bad = [f for f in funcs if not f.guard_table]
    print(f"  [{'PASS' if not g1_bad else 'FAIL'}] G1 每个配对函数都有放行守卫 —— 缺 {len(g1_bad)} 个")
    for func in g1_bad:
        print(f"         ★ {func.where}")
    if g1_bad:
        problems.append(
            f"{len(g1_bad)} 个配对函数缺『放行不归我管的节点』这一道"
            " —— 去掉它直接破坏别的包的既有用例（ISS-175 本体：本包全绿、别包全红）"
        )

    # ---- G2：守卫在配对判定之前
    g2_bad = [f for f in funcs if f.guard_table and f.has_pair and not f.guard_before_pair]
    print(f"  [{'PASS' if not g2_bad else 'FAIL'}] G2 守卫在配对判定之前 —— 顺序错 {len(g2_bad)} 个")
    for func in g2_bad:
        print(f"         ★ {func.where}")
    if g2_bad:
        problems.append(
            f"{len(g2_bad)} 个配对函数把两道判断写反了 —— 守卫永远不可达，等价于漏掉它"
        )

    # ---- G3：配对判定 + _raise_block
    g3_bad = [f for f in funcs if not (f.has_pair and f.has_raise)]
    print(f"  [{'PASS' if not g3_bad else 'FAIL'}] G3 配对判定 + _raise_block 都在 —— 缺 {len(g3_bad)} 个")
    for func in g3_bad:
        lack = "、".join(
            [n for n, ok in (("配对判定", func.has_pair), (f"{_RAISE_HELPER}()", func.has_raise)) if not ok]
        )
        print(f"         ★ {func.where} —— 缺 {lack}")
    if g3_bad:
        problems.append(
            f"{len(g3_bad)} 个配对函数只放行不拦截（缺配对判定 / 缺 {_RAISE_HELPER}())"
            " —— 触发器成了摆设，OP-88 的反向判据失效"
        )

    # ---- G4：WHEN 收窄
    g4_bad = [f for f in funcs if not f.has_when]
    print(f"  [{'PASS' if not g4_bad else 'FAIL'}] G4 触发器有 WHEN 收窄 —— 缺 {len(g4_bad)} 个")
    for func in g4_bad:
        print(f"         ★ {func.where}")
    if g4_bad:
        problems.append(
            f"{len(g4_bad)} 个配对函数的触发器没有 WHEN (NEW.entity_type = ...) 收窄"
            " —— 会误拦别的包注册到 version_nodes 的节点"
        )

    # ---- G5：两个独立来源对账（铁律 14）
    a_names = {f.where for f in funcs if f.in_a}
    b_names = {f.where for f in funcs if f.in_b}
    only_a = sorted(a_names - b_names)
    only_b = sorted(b_names - a_names)
    g5_ok = not only_a and not only_b
    print(
        f"  [{'PASS' if g5_ok else 'FAIL'}] G5 两个独立来源对账"
        f"（A 名字约定 vs B 行为约定）—— A={len(a_names)} B={len(b_names)}"
    )
    for name in only_a:
        print(f"         ★ A 有而 B 无（名字像配对函数，但没挂到 version_nodes）：{name}")
    for name in only_b:
        print(f"         ★ B 有而 A 无（挂到了 version_nodes，但命名不符合约定）：{name}")
    if not g5_ok:
        problems.append(
            f"两个独立来源分叉（A-B={len(only_a)}，B-A={len(only_b)}）"
            " —— 覆盖范围不能由『名字』单方面决定，换个名字就静默漏（铁律 14）"
        )

    # ---- G6：守卫表名 ∈ 全仓表集合
    g6_bad = [f for f in funcs if f.guard_table and f.guard_table not in tables]
    print(
        f"  [{'PASS' if not g6_bad else 'FAIL'}] G6 守卫表名是真实存在的表"
        f"（对照 {len(tables)} 张 __tablename__）—— 可疑 {len(g6_bad)} 个"
    )
    for func in g6_bad:
        print(f"         ★ {func.where} —— 守卫表 {func.guard_table!r} 不在全仓表集合里")
    if g6_bad:
        problems.append(
            f"{len(g6_bad)} 个守卫表名在全仓 __tablename__ 里找不到"
            " —— `SELECT 1 FROM <不存在的表>` 在 plpgsql 里延迟到首次执行才炸，"
            "表现为运行期 relation does not exist（建表期全绿）"
        )

    # ---- 解析错误（不能静默跳过）
    if errors:
        print(f"  [FAIL] 全部文件可解析 —— {len(errors)} 个失败")
        for err in errors:
            print(f"         ★ {err}")
        problems.extend(errors)

    # ---- G7：活性下界
    g7_ok = len(funcs) >= MIN_PAIRED
    print(f"  [{'PASS' if g7_ok else 'FAIL'}] G7 配对函数 >= {MIN_PAIRED}（活性下界）—— {len(funcs)} 个")
    if not g7_ok:
        problems.append(
            f"只扫到 {len(funcs)} 个配对函数（下界 {MIN_PAIRED}）"
            " —— 判据自身的 AST 规则可能已失效，它扫不到东西时会是『全通过』"
        )
    t_ok = len(tables) >= MIN_TABLES
    print(f"  [{'PASS' if t_ok else 'FAIL'}] G7b 表名集合 >= {MIN_TABLES}（G6 的输入活性）—— {len(tables)} 张")
    if not t_ok:
        problems.append(
            f"只扫到 {len(tables)} 张 __tablename__（下界 {MIN_TABLES}）"
            " —— G6 的输入来源已失效，它会把所有守卫表误报为『不在表集合里』"
        )

    print()
    if not problems:
        print(
            f"[PASS] {len(funcs)} 个配对触发器全部合规"
            f"（守卫齐全、顺序正确、WHEN 收窄、两来源一致；下界 {MIN_PAIRED}）。"
        )
        return 0
    print(f"[FAIL] RESULT=FAIL 共 {len(problems)} 处问题：")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())