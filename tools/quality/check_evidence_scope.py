#!/usr/bin/env python
"""证据脚本覆盖范围三方对账（铁律第 14 条 / ISS-162）。

## 为什么需要这个

本项目最高频的缺陷族是：

> **凡"覆盖范围"由人写（名单 / glob / 目录判断 / 包号上界 / 函数名），
> 就必须改由磁盘决定，并另有一条判据对账它。**（铁律第 14 条）

它已在本仓现身 **6 次**（ISS-104 写死目录 → ISS-111 只换藏身处 →
ISS-139 glob 只扫 `tools/m1/` → ISS-143 复跑载体路径写死 →
ISS-144 判据自己的 `rglob`/`ast.walk` → ISS-144 两个自检载体的收录 glob）。
**6 次全部是事后发现的**，每次都要人复跑时才想起来。

而"事后发现"之所以可能，只因为有人**恰好**去复跑。这与 ISS-95 是同一条：

> **「声称自己有防线」比「没有防线」更危险。**

## 现状：两个载体 + 一处对账，但那个对账防不住"一起错"

```
A1 = tools/m1/rerun_all_evidence.py  . SCOPE_PATTERNS     （3 条 glob）
A2 = tools/m1/check_evidence_scripts.py . _iter_scripts()  （4 条 glob）
```

两者之间已有一条交叉核对（`rerun` 里 `expected_self = len(SCRIPTS) + 1`）。
**但它有 ISS-137 的病**：那两个 glob 名单若**同时**写成一样的错，
对账**恒等**、永不报红。它只防"两个实现分叉"，**不防"两个实现一起错"**。

## 判据 D 的做法：引入第三个来源，且按"行为"而非"名字"

```
B = { 脚本 | 该脚本会写入 evidence/ 下的文件 }      ← AST 反推，不看文件名
```

`B` 与 `A1`/`A2` 是**独立事实**：前者由"脚本做什么"决定，后者由"脚本叫什么"决定。
两者相遇才能真正对账 —— 而不是同一事实的两种写法（判据纪律第 4 条）。

四条判据：

| # | 判据 | 含义 |
|---|---|---|
| D1 | `A1 - B == ∅`（除白名单） | A1 收进来的必须**真是**证据脚本 —— 防 glob 过宽 |
| D2 | `B - A1 == ∅`（除白名单） | 写 evidence 的必须被 A1 覆盖 —— **防静默漏跑** |
| D3 | `A1 ⊆ A2` | 复跑范围必须是自检范围的子集 |
| D4 | `len(B) >= MIN_REVERSE` | 活性下界：反推若失效（反推不出东西），条数会掉下来 |

### 本包开发期实测（判据 D 当场抓到一处真缺口）

> ⚠️ **这里刻意不写数字** —— 本表初版手写了 A1=26/A2=27/B=27，
> 写完第二天就过期（现场已是 27/28/28，后来本包自己新增一个脚本又变成 28/29/29）。
> **手写数字必然漂移**，这正是铁律 14 的病长在文档上（覆盖范围由人写）。
> 现行数字一律以**本脚本每次运行的输出**为准（第 3~5 行会打印）。
>
> 数字不许手写这件事由一条判据守着：
> `tools/quality/collect_discipline_code_review.py` 的 `[逐行-2]` —— 它断言
> 本 docstring 里**不存在**「`| 集合名 | 数字 |`」形态的手写行。

| 集合 | 定性 |
|---|---|
| B（写 evidence，AST 反推） | 本质全集 |
| A1（复跑载体三约定 glob） | 复跑范围，**必须 ⊆ B** |
| A2（自检载体四约定 glob） | 自检范围，**必须 ⊇ A1** |
| **A1 − B** | 现有三约定**没有收错**，零假阳性 ✅ |
| **B − A1** | 仅剩 `tools/m1/rerun_all_evidence.py` **自己**（结构性缺口，进白名单） |

那个缺口是**结构性**的（复跑载体不可能复跑自己），因此进白名单；
但**"进白名单"不等于"不判"**：判据会断言它**确实写 evidence**
（否则豁免理由失效 ⇒ 报红）。见 `_EXEMPT_FROM_A1`。

## 不做假保证（已知盲区，写在这里而不是假装没有）

AST 反推只看**静态**写入路径。以下形态**反推不到**，本判据覆盖不了：

  · 用 `subprocess` 把 stdout 重定向到 `evidence/` 的脚本；
  · 把路径拼成运行期才知的形式（如从 argv 读、从配置读）；
  · **路径经函数局部变量间接引用** —— 如 `main()` 里先写
    `evidence_dir = REPO_ROOT / "evidence" / "m5"`，再写
    `(evidence_dir / f"x.json").write_text(...)`。`_module_consts()` **只收
    `tree.body` 的模块级赋值** ⇒ 函数内的 `evidence_dir` 解析不到 ⇒ 拼出的
    路径串里没有 `evidence` 段 ⇒ 该脚本被判"不写 evidence"（**假阴性**：
    真写证据却不在 B）。本仓的备份恢复与回滚两个演练脚本即此形态
    （`check_backup_restore_drill.py` / `check_rollback_drill.py`，
    均在 `main()` 内定义 `evidence_dir`）；
  · 用 `with open(EVIDENCE / "x.log", "w")` —— **这一种能覆盖**（`open` 的第二实参是模式常量）。

已知盲区必须写明 —— 否则就是"声称 100% 覆盖"的 ISS-95 变体。
`len(B)` 的活性下界（D4）只对**上面第一、二种**形态有效：把某个**已经在 B 里**
的脚本改成 subprocess / argv 写法，B 会掉一条、D4 报红。
⚠️ **D4 对第三种（函数局部变量）无效** —— 那是一个**从来没进过 B** 的脚本，
总量下界看不见它，属于**静默**。该形态的"加了脚本忘了登记"由另一条独立判据
兜住：CI 门 3 的 `ci-steps-covered`（`tools/regression/collect_regression_evidence.py`）
双向对账"`ci.yml` 里真跑的 tool 脚本 ⇄ `REQUIRED_CI_STEPS` 名单"。
两条判据口径不同（一条按**行为**反推、一条按**已声明步骤**对账），
合起来才盖住——但**覆盖不是靠"这两条加起来应该够"的推理**，
是靠这两个脚本确实逐条登记在 `REQUIRED_CI_STEPS` 里（见 ISS-193）。

跑法：
    python tools/quality/check_evidence_scope.py
退出码 0 = 三方一致；1 = 有分叉。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: A1：复跑载体的覆盖约定（**从实体读出来**，不手抄 —— 手抄会漂移）。
RERUN_SCRIPT = REPO_ROOT / "tools" / "m1" / "rerun_all_evidence.py"
#: A2：自检载体的覆盖约定（同上）。
SELF_CHECK_SCRIPT = REPO_ROOT / "tools" / "m1" / "check_evidence_scripts.py"

#: 证据目录（相对仓库根）。判据 B 的"写入目标"判定的锚点。
EVIDENCE_DIR_NAME = "evidence"

#: 临时目录名：`evidence/` 下若写入的是**临时文件**，不算"证据"。
#:
#: 依据：`services/backend/tests/test_contract.py` 的 `_TMP_ROOT` 写
#: `evidence/m1/tmp/openapi-<uuid>.json`，那是**测试中间产物**（用完即删），
#: 不是交付证据。若不排除，判据 D 会把一个**测试文件**当成"漏覆盖的证据脚本"。
#: 排除是按**路径结构**（`evidence/` 之后出现 `tmp`）判定的，不是按文件名白名单 ——
#: 白名单会让整个 `tmp` 目录成为"法外之地"。
TMP_SEGMENT = "tmp"

#: 活性下界：AST 反推至少要能找出这么多脚本。
#:
#: 刻意写成**下界**而不是等式（铁律 1：判据只能断言"我建的还在"，
#: 不能断言"全项目恰好有 N 个"）。本值取"本次实测值"，只增不减。
#: 若将来某脚本改成反推不到的写法，B 掉一条 ⇒ 本条报红（**这才是它存在的意义**）。
MIN_REVERSE = 27

#: `B - A1` 的豁免：逐条给理由，且**理由必须可被核对**。
#:
#: 每条豁免由判据做两件事：① 断言该文件**确实写 evidence**（否则豁免理由不成立 ⇒ 报红）；
#: ② 断言它**确实不在 A1 里**（否则豁免是多余的 ⇒ 报红，因为它掩盖了"覆盖范围已变化"）。
#: 不核对理由的白名单会退化成"把报红的地方都加进来"（`check_test_db_isolation.ALLOW_FILES`
#: 同纪律）。
EXEMPT_FROM_A1: dict[str, str] = {
    "tools/m1/rerun_all_evidence.py": (
        "复跑载体自己：它确实写 evidence/m1/rerun-all-evidence.log，"
        "但它**不可能复跑自己** ⇒ 按定义不可能进 A1 的 glob。"
        "由 A2（自检载体）覆盖 —— 它纳入自己，`A1 ⊆ A2` 由 D3 保证。"
    ),
}

#: `A1 - B` 的豁免：当前为空（实测 0 条）。留空而非删掉这个结构 ——
#: 它是一处"若将来 glob 收进非证据脚本，必须逐条解释"的**显式位置**。
EXEMPT_FROM_B: dict[str, str] = {}

#: 写入方法名：`Path.write_text` / `Path.mkdir` / ... 的 `attr`。
#:
#: ⚠️ **2026-10-01 三遍法自纠（死代码）**：本文件曾有一个
#: `_RESULT_RE = re.compile(r"RESULT[=:]")`，定义之后**全文未再使用**。
#: 死代码的害处不是"占一行"，而是**读者以为这里有 RESULT 解析**（ISS-95 轻量版：
#: 声称有某物比没有更危险）。判据：改动后跑一遍
#: `python tools/quality/collect_discipline_code_review.py`，它会扫"定义后从未引用"
#: 的模块级常量。
_WRITE_METHODS: frozenset[str] = frozenset(
    {"write_text", "write_bytes", "mkdir", "touch"}
)
#: `open(<evidence path>, <mode>)` 里"算写入"的模式。
_WRITE_OPEN_MODES: frozenset[str] = frozenset(
    {"w", "a", "wb", "ab", "x", "xb", "w+", "a+", "w+b", "a+b"}
)


# --------------------------------------------------------------------------- #
# A1 / A2：从两个载体**现场执行**读取它们的覆盖范围（不手抄、不正则猜）
# --------------------------------------------------------------------------- #
#
# ⚠️ 第一版用正则扫 `.glob("...")` 的**字面量**，实测读到 A1=0 ——
# 因为 `rerun_all_evidence.py` 的 glob 写在 `SCOPE_PATTERNS` 元组里，
# 调用处是 `.glob(pattern)`（变量）。那一刻我写了第二份"从哪里找 glob"的规则，
# 而**那正是铁律 14 的病**（覆盖范围由我写的正则决定）。
#
# 正解：**让载体自己报出它的范围** —— 加载其模块、读它模块级的 `SCRIPTS`
# 或调它的 `_iter_scripts()`。判据不猜、不抄，范围 100% 来自实体。


def _load_module(path: Path) -> object:
    """按文件路径加载一个模块（不执行 `__main__` 块）。

    ⚠️ **必须先 `sys.modules[name] = module` 再 `exec_module`**（ISS-164，2026-10-01）：
    CPython 3.13 的 `dataclasses._is_type()` 会执行
    `sys.modules.get(cls.__module__).__dict__` 来解析注解；模块不在 `sys.modules` 里时
    它是 `None` ⇒ `AttributeError: 'NoneType' object has no attribute '__dict__'`。

    本函数当前加载的两个目标（`rerun_all_evidence.py` / `check_evidence_scripts.py`）
    **恰好都没有 `@dataclass`**，所以缺这一行时**暂时不报错** —— 这正是最坏的情况：
    它是一颗定时炸弹，等哪天被加载的模块引入 dataclass（或加个 `field`）才炸，
    而那时炸的是**别的包**。同仓 `rerun_all_evidence.py` 就是被同一颗炸过的实例。

    全仓的注册纪律由 `tools/quality/check_loader_registration.py` 静态守。
    """
    import importlib.util

    name = f"_scope_probe_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _rel(paths: object, *, source: str) -> set[str]:
    """把载体报出的路径集规范化成"仓库根相对路径"字符串集。"""
    out: set[str] = set()
    for item in paths:  # type: ignore[union-attr]
        resolved = Path(item).resolve()
        try:
            out.add(str(resolved.relative_to(REPO_ROOT)))
        except ValueError:
            out.add(str(resolved))  # 仓外路径原样保留（不静默丢）
    if not out:
        raise RuntimeError(
            f"{source} 报出了空范围 —— 判据的输入契约已变，"
            f"不能把「读不到」当成「没有」（ISS-143 同族）"
        )
    return out


def discover_a1() -> set[str]:
    """A1 = 复跑载体**实际**会复跑的那批文件（读它模块级的 `SCRIPTS`）。"""
    module = _load_module(RERUN_SCRIPT)
    scripts = getattr(module, "SCRIPTS", None)
    if scripts is None:
        raise RuntimeError(
            "rerun_all_evidence.py 没有模块级 SCRIPTS —— "
            "判据的输入契约已变（不是通过，是无法判定）"
        )
    return _rel(scripts, source="rerun_all_evidence.SCRIPTS")


def discover_a2() -> set[str]:
    """A2 = 自检载体**实际**会自检的那批文件（调它 `_iter_scripts()`）。"""
    module = _load_module(SELF_CHECK_SCRIPT)
    iterator = getattr(module, "_iter_scripts", None)
    if not callable(iterator):
        raise RuntimeError(
            "check_evidence_scripts.py 没有 _iter_scripts() —— "
            "判据的输入契约已变（不是通过，是无法判定）"
        )
    names = [path for path, _requires_inline in iterator()]
    return _rel(names, source="check_evidence_scripts._iter_scripts()")


# --------------------------------------------------------------------------- #
# B：按"行为"反推 —— 会写入 evidence/ 下文件的脚本
# --------------------------------------------------------------------------- #


def _module_consts(tree: ast.Module) -> dict[str, ast.expr]:
    """模块级 `NAME = <expr>`（含注解赋值）→ `NAME → expr`（用于解析间接路径）。"""
    out: dict[str, ast.expr] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                out[target.id] = value
    return out


def _evidence_kind(value: str) -> str:
    """一个路径串在"证据路径"这个维度上的三分类。

    · `"tmp"`   —— 路径里 `evidence` 段之后出现 `tmp` 段 ⇒ **临时文件，不算证据**；
    · `"real"`  —— 含独立的 `evidence` 路径段且其后无 `tmp` ⇒ 证据；
    · `"none"`  —— 没有独立的 `evidence` 路径段。

    ⚠️ **必须按"路径段"判，绝不能按"子串"判**（本判据实测踩过两次）：

      ① 反例之一：`"/tmp/evidence-marker.txt"` —— `evidence` 只是**文件名的一部分**，
         它根本不在 `evidence/` 目录下。第一版用 `"evidence" in low` 判定，
         把它误判成 `real`（假阳性：一个与证据无关的文件被当成证据脚本）。

      ② 反例之二：`test_contract.py` 的 `_TMP_ROOT` 拼出来是
         `…/evidence/m1/tmp` —— 它是**测试中间产物**（用完即删），不是交付证据。
         若不排除，判据 D 会把一个**测试文件**当成"漏覆盖的证据脚本"，
         D2 一次报 27 个假阳性。

    正解：先把路径按 `/`、`\\` 切成**段**，要求存在**独立的 `evidence` 段**
    （不是子串），再看它**之后**的段里有没有独立的 `tmp` 段。

    排除按**路径结构**判，不建文件名白名单 —— 白名单会让整个目录成为法外之地。
    """
    low = value.lower()
    segments = [seg.strip() for seg in re.split(r"[\\/]+", low)]
    # 必须存在**独立的 `evidence` 路径段**：`/tmp/evidence-marker.txt` 的段是
    # `['', 'tmp', 'evidence-marker.txt']` —— 没有 `evidence` 段 ⇒ `none`。
    if EVIDENCE_DIR_NAME not in segments:
        return "none"
    tail = segments[segments.index(EVIDENCE_DIR_NAME) + 1 :]
    # `evidence` 之后出现独立的 `tmp` 段即视为临时文件。
    if TMP_SEGMENT in tail:
        return "tmp"
    return "real"


def _points_at_evidence(
    expr: ast.AST, consts: dict[str, ast.expr], depth: int = 0
) -> str:
    """表达式是否指向 evidence 路径。返回 `"real"` / `"tmp"` / `"none"`。

    ⚠️ **必须先把整个表达式拼成一个路径串，再做一次分类**（第一版按
    "左半 real 就 real" 判定，于是

        Path(__file__).resolve().parents[3] / "evidence" / "m1" / "tmp"

    被判成 `real` —— 因为左半含 `evidence`。而它其实是**临时目录**，
    于是 `services/backend/tests/test_contract.py`（一个测试文件）
    被当成"漏覆盖的证据脚本"，判据 D2 一次报了 27 个假阳性。

    **判据自己的实现也必须"按本质判，不按中间步骤判"** ——
    路径的本质是"拼完之后长什么样"，不是"左半段含不含 evidence"。

    支持的形态（都是本仓实际用法）：
      · 字面量              `"evidence/m1/x.log"`
      · 模块级常量          `EVIDENCE_DIR` / `OUT_LOG`（递归解析其赋值）
      · 二元拼接            `REPO_ROOT / "evidence" / "m1"` · `"a" + "b"`
      · 属性取值            `EVIDENCE_DIR / "m1"`
      · 调用实参            `Path("evidence")`

    递归有深度上限（防自引用常量导致死循环）。
    """
    fragments = _path_fragments(expr, consts, depth)
    return _evidence_kind("/".join(fragments))


def _path_fragments(
    expr: ast.AST, consts: dict[str, ast.expr], depth: int = 0
) -> list[str]:
    """递归收集表达式里所有字符串常量的片段（用于重建路径串）。"""
    if depth > 8:
        return []
    out: list[str] = []
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        out.append(expr.value)
    elif isinstance(expr, ast.Name):
        ref = consts.get(expr.id)
        if ref is not None:
            out.extend(_path_fragments(ref, consts, depth + 1))
    elif isinstance(expr, ast.BinOp):
        out.extend(_path_fragments(expr.left, consts, depth + 1))
        out.extend(_path_fragments(expr.right, consts, depth + 1))
    elif isinstance(expr, ast.Attribute):
        out.extend(_path_fragments(expr.value, consts, depth + 1))
    elif isinstance(expr, ast.Call):
        for arg in expr.args:
            out.extend(_path_fragments(arg, consts, depth + 1))
    return out


def writes_evidence(path: Path) -> tuple[bool, str]:
    """该文件是否会**写入** evidence/ 下的**非临时**文件。返回 `(是否, 依据)`。"""
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"读取失败：{type(exc).__name__}"
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        # 语法错由别的判据管（本判据不静默 —— 但也**不能**把它当"不写证据"而放过）。
        return False, f"语法错：{exc.msg}（该文件无法参与反推，请先修语法）"

    consts = _module_consts(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # ① `X.write_text(...)` / `X.mkdir(...)`
        if isinstance(func, ast.Attribute) and func.attr in _WRITE_METHODS:
            if _points_at_evidence(func.value, consts) == "real":
                hits.append(f"{func.attr}@{node.lineno}")
        # ② `open(<evidence>, "w")`
        if (
            isinstance(func, ast.Name)
            and func.id == "open"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _WRITE_OPEN_MODES
            and _points_at_evidence(node.args[0], consts) == "real"
        ):
            hits.append(f"open(w)@{node.lineno}")
    return bool(hits), (",".join(hits) if hits else "无 evidence 写操作")


def discover_b() -> tuple[set[str], dict[str, str]]:
    """B = 按行为反推的证据脚本全集。返回 `(集合, {路径: 依据})`。"""
    found: set[str] = set()
    reasons: dict[str, str] = {}
    for path in sorted(REPO_ROOT.rglob("*.py")):
        parts = path.parts
        if ".git" in parts or "__pycache__" in parts or "node_modules" in parts:
            continue
        ok, why = writes_evidence(path)
        if ok:
            rel = str(path.resolve().relative_to(REPO_ROOT))
            found.add(rel)
            reasons[rel] = why
    return found, reasons


# --------------------------------------------------------------------------- #
# 判据
# --------------------------------------------------------------------------- #

Check = tuple[str, bool, str]


def run_checks() -> tuple[list[Check], dict[str, set[str]], list[str]]:
    """跑四条判据 D1~D4。返回 `(checks, 明细, 说明行)`。"""
    checks: list[Check] = []
    notes: list[str] = []

    a1 = discover_a1()
    a2 = discover_a2()
    b, b_reasons = discover_b()

    notes.append(f"  A1（复跑载体 SCRIPTS）→ {len(a1)} 个脚本")
    notes.append(f"  A2（自检载体 _iter_scripts()）→ {len(a2)} 个脚本")
    notes.append(f"  B （AST 反推写 evidence）→ {len(b)} 个脚本")

    # ---- D1：A1 ⊆ B（除白名单）—— 防 glob 过宽
    a1_minus_b = sorted(a1 - b)
    exempt_b = sorted(p for p in a1_minus_b if p in EXEMPT_FROM_B)
    offending_d1 = sorted(p for p in a1_minus_b if p not in EXEMPT_FROM_B)
    checks.append(
        (
            "D1 A1⊆B（收进来的都是真证据脚本）",
            not offending_d1,
            (
                f"多余 {len(offending_d1)} 个"
                + (f"：{'、'.join(offending_d1)}" if offending_d1 else "")
                + (f"（另有 {len(exempt_b)} 个已豁免）" if exempt_b else "")
            ),
        )
    )

    # ---- D2：B ⊆ A1（除白名单）—— **防静默漏跑（本判据的核心）**
    b_minus_a1 = sorted(b - a1)
    exempt_a1 = sorted(p for p in b_minus_a1 if p in EXEMPT_FROM_A1)
    offending_d2 = sorted(p for p in b_minus_a1 if p not in EXEMPT_FROM_A1)
    checks.append(
        (
            "D2 B⊆A1（写 evidence 的都被复跑覆盖）",
            not offending_d2,
            (
                f"漏覆盖 {len(offending_d2)} 个"
                + (f"：{'、'.join(offending_d2)}" if offending_d2 else "")
                + (f"（另有 {len(exempt_a1)} 个已豁免）" if exempt_a1 else "")
            ),
        )
    )

    # ---- D2b：豁免白名单**不能腐烂** —— 每条理由必须仍然成立
    #
    # 四条子判据，缺一不可：
    #   ① 豁免的文件确实"写 evidence"（理由的前半段仍成立）；
    #   ② 豁免的文件确实**不在 A1 里**（reason 的后半段仍成立；
    #      若它哪天进了 A1，这条豁免就是多余的、且在掩盖"覆盖范围已变化"）；
    #   ③ 豁免的路径存在（防"文件改名后豁免变成僵尸条目"）；
    #   ④ 理由本身不能是空占位（**空理由的白名单 = 没有白名单**，ISS-95）。
    #
    # ⚠️ **2026-10-01 三遍法自纠**：本段初版写的是
    #     `for rel, reason in EXEMPT_FROM_A1.items():` —— 但 **`reason` 从头到尾没被用过**。
    # 后果正是纪律禁止的形态：**白名单不核对理由** ⇒ 它会退化成
    # "把报红的地方都加进来"（理由只是装饰）。现在 `reason` 被拼进报红信息，
    # 于是"理由写不出来"这件事在**报红时**会当场暴露。
    stale: list[str] = []
    for rel, reason in EXEMPT_FROM_A1.items():
        # ③ 路径存在（防"文件改名后豁免变成僵尸条目"）。
        # 注：这一条独立于 ① —— `rel not in b` 只能说明「它没被反推为证据脚本」，
        # 而"文件整个不见了"与"文件在但不再写 evidence"是两件事（ISS-137 纪律：
        # 两种独立事实不可互相替代）。
        if not (REPO_ROOT / rel).exists():
            stale.append(
                f"{rel}（文件已不存在 ⇒ 豁免理由失效，白名单成了僵尸条目。原理由：{reason}）"
            )
            continue
        if rel not in b:
            stale.append(
                f"{rel}（不再写 evidence ⇒ 豁免理由失效（前半段不成立）。原理由：{reason}）"
            )
        if rel in a1:
            stale.append(
                f"{rel}（已进入 A1 —— 豁免多余，应删除该条目。原理由：{reason}）"
            )
        # ④ 理由不能是空占位 —— 空理由的白名单等于没有白名单（ISS-95）。
        if len(reason.strip()) < 20:
            stale.append(f"{rel}（豁免理由过短，写不下可核对的事实：{reason!r}）")
    checks.append(
        (
            "D2b 豁免白名单不腐烂（理由仍成立）",
            not stale,
            f"{len(EXEMPT_FROM_A1)} 条豁免，{len(stale)} 条失效" + (
                f"：{'；'.join(stale)}" if stale else ""
            ),
        )
    )

    # ---- D3：A1 ⊆ A2（复跑范围 ⊆ 自检范围）
    a1_minus_a2 = sorted(a1 - a2)
    checks.append(
        (
            "D3 A1⊆A2（复跑 ⊆ 自检）",
            not a1_minus_a2,
            (
                f"复跑有而自检无 {len(a1_minus_a2)} 个"
                + (f"：{'、'.join(a1_minus_a2)}" if a1_minus_a2 else "")
            ),
        )
    )

    # ---- D4：活性下界（反推不能"反推不出东西"）
    checks.append(
        (
            f"D4 len(B)>={MIN_REVERSE}（反推活性）",
            len(b) >= MIN_REVERSE,
            f"{len(b)} 个（下界 {MIN_REVERSE}）",
        )
    )

    detail = {"A1": a1, "A2": a2, "B": b, "B_reasons": b_reasons}  # type: ignore[dict-item]
    return checks, detail, notes


def main() -> int:
    checks, detail, notes = run_checks()
    a1 = detail["A1"]
    a2 = detail["A2"]
    b = detail["B"]

    print("证据脚本覆盖范围三方对账（铁律 14 / ISS-162）")
    for note in notes:
        print(note)
    print()
    print(f"  三方对账：A1={len(a1)}  A2={len(a2)}  B={len(b)}")
    print(f"  差集：A1-B={len(a1 - b)}  B-A1={len(b - a1)}  A1-A2={len(a1 - a2)}")
    print()

    failed = [c for c in checks if not c[1]]
    for name, ok, detail_text in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} —— {detail_text}")

    print()
    if failed:
        print(f"[FAIL] {len(failed)} 条判据未通过：")
        for name, _, detail_text in failed:
            print(f"  · {name}：{detail_text}")
        print()
        print("处置：漏覆盖的脚本要么改名与既有约定一致，要么同步三处")
        print("（ci.yml 门 3 步骤列表 / check_evidence_scripts.py 收录 glob /")
        print(" rerun_all_evidence.py 的 SCOPE_PATTERNS + MIN_SCRIPTS）。")
        print("若确属「不可能复跑自己」这类结构性原因，必须在 EXEMPT_FROM_A1 里")
        print("登记并写明理由 —— 判据会自动核对理由是否仍然成立（D2b）。")
        return 1

    print(
        f"[PASS] 三方一致：A1={len(a1)} / A2={len(a2)} / B={len(b)}；"
        f"豁免 {len(EXEMPT_FROM_A1)} 条（理由均仍成立）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
