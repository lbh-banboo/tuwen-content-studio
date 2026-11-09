#!/usr/bin/env python
"""证据脚本自检：把"靠记性"的三件事变成机械检查。

**为什么需要这个脚本**（三条都是踩出来的）：

1. **内联脚本段必须能独立 compile**。
   证据脚本是"外层 Python 文件 + 内联 `r\"\"\"...\"\"\"` 字符串"的两层结构，
   而这两层**不共享任何东西**：
     · 内联段漏 `import` → `NameError`（ISS-43）；
     · 内联段出现三引号 → **提前终止外层字符串**，后面全被当外层代码解析，
       报出莫名其妙的 `SyntaxError: invalid character`（ISS-45）。
   同一个"两层结构"教训出现过三次，说明它不是疏忽而是**缺一道检查**。

2. **每个脚本都要有 `EXPECTED_CHECKS` 与条数核对**。
   RESULT 行若只统计"失败了几条"，那么"脚本崩溃、本该产生的 CHECK
   一条都没产生"与"全部通过"在计数上**完全一样**（ISS-43）。

3. **不能用"恰好等于某个迁移版本"做判据**。
   `alembic_version` 只存 head，每个新迁移都会推进它 ——
   用 `==` 写就注定在下一个工作包失效，表现为"历史证据集体变红"（ISS-44）。

跑法：

```bash
python tools/m1/check_evidence_scripts.py
```
退出码 0 = 全部通过。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parents[1]

# 内联脚本变量的命名约定：`<NAME>_SCRIPT = r"""..."""`
_INLINE_RE = re.compile(r'(\w+_SCRIPT) = r"""(.*?)"""', re.DOTALL)
# 允许出现在内联段里的三引号用法：**没有**。
_FORBIDDEN_IN_INLINE = ('"""', "'''")


def _iter_scripts() -> list[tuple[Path, bool]]:
    """返回 [(脚本路径, 是否要求"外层+内联段"两层结构)]。

    **为什么要带第二个字段**：本仓库有两类证据生成器 ——

    · `tools/m1/collect_wp<NN>_evidence.py`：**两层结构**（外层 Python + 内联
      `r\"\"\"...\"\"\"` 脚本，内联段在测试库/验证库上跑）。必须检查内联段能编译。
    · 其余（`refresh_*_evidence.py`、`tools/regression/collect_*_evidence.py`、
      `collect_*code_review.py` 等）：**单层**，只做交叉核对，没有内联段。对它们
      要求"必须找到内联脚本"是**假阳性**。

    ⚠️ **覆盖范围必须含 code_review 族**（2026-09-29 WP-DEBT-01 收口实测）：
    见下方 `_iter_scripts()` 里"第四类复发"的注释 —— 它们不以 `_evidence` 结尾，
    曾**完全不在覆盖内**。

    早期版本只扫 `tools/m1/` 下的两个 glob，于是新写的
    `tools/regression/collect_regression_evidence.py` **不在门禁覆盖内** ——
    门禁的覆盖范围本身成了靠记性维护的东西（这正是本脚本要消灭的病，
    ISS-95：「声称自己有防线」比「没有防线」更危险）。
    因此除 `tools/m1` 之外的工具目录**递归**纳入。

    ⚠️ **同类缺陷在本文件里复发过一次**（同一条病、不同的藏身处）：
    早期版本在递归那一步写了

        if path.parent == TOOLS_DIR:
            continue        # 「tools/m1 已由上面两条 glob 覆盖」

    那句话只对 `collect_wp*.py` / `refresh_*.py` **成立** ——
    `tools/m1/collect_schema_parity_reverse_evidence.py`
    既不是 `collect_wp*_evidence.py` 也不是 `refresh_*_evidence.py`，
    两条 glob 都不命中，递归又把它 skip 掉，于是**它完全不在自检范围内**：
    13 个来自 `tools/m1` 的脚本被报出来，而其实那里有 14 个。
    这个脚本自己就是"清点覆盖范围"的工具，却因为一条"我以为覆盖了"的
    早退而漏掉脚本 —— 正是它要防的事。

    现在改为**全量收集 + 去重**，不再用"父目录判断"来跳过：
    覆盖范围由"磁盘上有什么"决定，不由"我以为哪条 glob 命中了"决定。
    """
    seen: dict[Path, bool] = {}
    # 两类命名约定（内联两层结构 / 单层）分别声明其覆盖要求。
    #
    # ⚠️ **第三类复发（2026-09-29，WP24 收口实测）**：上面两条 glob 原本写成
    # `TOOLS_DIR.glob(...)`，于是 `tools/m3/collect_wp24_evidence.py`
    # **只被下面那条递归 glob 命中为 `requires=False`** ——
    # 它明明是两层结构（内联段 3.5 万字符），却**不要求内联段能编译**。
    # 症状与 ISS-43 同型：内联段里漏一个 import、或多写一个三引号，
    # 在门禁眼里完全正常（`requires=False` 时"没有内联段"都不算问题）。
    #
    # 修法：**按文件名在 `tools/**` 全仓匹配**，不再按目录。
    # 覆盖范围必须由"文件叫什么"决定，绝不能由"它住在哪个子目录"决定 ——
    # 后者会让每个新模块目录（`tools/m3`、`tools/m4`…）
    # 都要有人记得回来加一条 glob，而**忘记加是静默的**。
    for path in sorted(REPO_ROOT.glob("tools/**/collect_wp*_evidence.py")):
        seen[path.resolve()] = True
    for path in sorted(REPO_ROOT.glob("tools/**/refresh_*_evidence.py")):
        seen[path.resolve()] = False
    # 仓库里**所有**其余 `*_evidence.py`（单层结构，不要求内联段）。
    # 命中过的用 `setdefault` 保留原来的覆盖要求，避免把 require_inline 覆盖成 False。
    for path in sorted(REPO_ROOT.glob("tools/**/*_evidence.py")):
        seen.setdefault(path.resolve(), False)
    # ⚠️ **第四类复发（2026-09-29，WP-DEBT-01 收口实测）**：上面的 glob 全部以
    # `_evidence.py` 结尾，于是**四个 `collect_*_code_review.py` 全部不在覆盖内** ——
    # 包括本包为 CI 门 3 新建的 `tools/quality/collect_dependency_declaration_code_review.py`。
    # 它们是**同一类证据脚本**（逐条 CHECK + `EXPECTED_CHECKS` + `RESULT=` 约定），
    # 只是文件名不以 `_evidence` 结尾。这与 ISS-139（glob 只扫 `tools/m1/`）、
    # ISS-143（复跑载体路径写死）**是同一个病**：**覆盖范围由"名字长什么样"决定，
    # 而漏掉是静默的**。
    #
    # 实测价值：纳入后**当场**抓到本包 code_review 脚本里一处真问题
    # （判据名里写了它要禁止的字面模式，被长度判据误判为违规）——
    # 这正是"声称有防线 vs 真的有防线"的区别（ISS-95）。
    for path in sorted(REPO_ROOT.glob("tools/**/collect_*code_review.py")):
        seen.setdefault(path.resolve(), False)
    return [(path, requires) for path, requires in sorted(seen.items())]


def check_inline_compiles(path: Path, *, required: bool = True) -> list[str]:
    """内联段必须能独立 compile，且不能含三引号。

    `required=False` 时（单层脚本），没有内联段不算问题；但**一旦出现**
    `_SCRIPT = r\"\"\"` 约定，仍然照常检查 —— 半路改成两层结构也得守规矩。
    """
    problems: list[str] = []
    text = path.read_text(encoding="utf-8")
    matches = list(_INLINE_RE.finditer(text))
    if not matches:
        if required:
            problems.append("未找到内联脚本（约定：`<NAME>_SCRIPT = r\"\"\"...\"\"\"`）")
        return problems

    for match in matches:
        name, body = match.group(1), match.group(2)
        # 注：正则已经用非贪婪匹配到第一个 `"""`，因此"内联段里出现三引号"
        # 会表现为"匹配到的 body 结尾异常、剩余内容变成外层代码"。
        # 这里额外检查 body 结尾附近是否像是被截断（最后一行不以换行结束、
        # 或 body 里出现明显的外层语法痕迹）。
        if not body.endswith("\n"):
            problems.append(
                f"内联段 {name} 的结尾异常 —— 可能被内部的三引号截断了"
            )
        try:
            compile(body, f"<inline:{name}>", "exec")
        except SyntaxError as exc:
            problems.append(f"内联段 {name} 语法错误：{exc}")
    return problems


#: `checks` 容器的声明。**必须容得下带类型注解的写法** ——
#: `checks = []` 与 `checks: list[tuple[str, bool, str]] = []` 都是合法的。
#: 只认前者的话，给变量补个注解就能**绕过** `EXPECTED_CHECKS` 强制检查：
#: 判据被"实现的一个无关细节"决定，而不是被行为决定（ISS-82 同族）。
_CHECK_DECL_RE = re.compile(r"\bchecks\s*(?::[^=\n]+)?=\s*\[")


def _uses_check_pattern(text: str) -> bool:
    """该脚本是否使用"逐条 CHECK + RESULT 计数"模式。

    **不按"有没有 `_SCRIPT`"判断**（那是内联结构的标志，两种脚本都有）：
    `collect_wp10_evidence.py` 采集的是 pytest / 契约结果，用的是
    `_pytest_result()` 解析汇总行，**根本没有 `checks`/`failures` 这套** ——
    对它要求 `EXPECTED_CHECKS` 是**假阳性**（本机实测踩到）。
    判据应看它有没有这套变量，而不是看它有没有内联脚本。

    ⚠️ **本函数曾用 `"failures" in text` 当判据，导致 WP21 整条检查链路
    从未执行**（收口复跑实测踩到）。WP21 外层把失败数写成
    `sum(1 for c in checks if not c)`，**全文没有 `failures` 这个词**，
    于是被判为"非 CHECK 模式"，`check_expected_checks` 与
    `check_no_exact_revision` 双双早退 —— 它的等式判据与自检器新加的
    `head ==` 判据因此都形同虚设。

    教训：**判据不能建立在"某个变量恰好叫这个名字"上**。
    这里改为看"结构特征"：`checks` 容器 + `EXPECTED_CHECKS` 常量二者同在，
    就说明它走的是这套模式 —— 无论它怎么统计失败数。
    """
    return _CHECK_DECL_RE.search(text) is not None and "EXPECTED_CHECKS" in text


def _reports_expected_count(text: str) -> bool:
    """RESULT 输出里是否写出了"期望几条"。

    **判据按行为定，不按写法定**。早期版本只认 f-string 字面量
    `f"... (期望 {EXPECTED_CHECKS})"`，于是把
    `" checks (期望 " + str(EXPECTED_CHECKS) + ")"` 这种**字符串拼接**
    判成"没写期望条数"（WP21 实测假阳性 —— 它的输出里明明有）。

    **为什么不用"找出那一条 print"来实现**：本仓库的 RESULT 有**两种出口** ——
    一半脚本直接 `print(...)`，另一半先拼进 `lines = [...]` 再统一落盘
    （`collect_wp13` 等），它们那条语句里**根本没有 `print` 这个字**。
    按"找 print"写就会把后一半全部误报（实测 13 个集体变红）。

    因此判据简化为**行为本身**：RESULT 标记附近（原文、且已在
    `_strip_comments` 之后的代码里）同时出现"期望"与 `EXPECTED_CHECKS`。
    取小块窗口而不是整文件，是为了让"RESULT 写了、期望条数忘了写"
    真的能被判红 —— 否则整文件里别处的注释也能把它喂饱。
    """
    code_lines = _strip_comments(text).splitlines()
    for idx, line in enumerate(code_lines):
        if "RESULT" not in line:
            continue
        window = "\n".join(code_lines[idx : idx + 6])
        if "期望" in window and "EXPECTED_CHECKS" in window:
            return True
    return False


def check_expected_checks(path: Path) -> list[str]:
    """使用 CHECK 模式的脚本都要有 EXPECTED_CHECKS，且成功判定里用到它。"""
    problems: list[str] = []
    text = path.read_text(encoding="utf-8")

    if not _uses_check_pattern(text):
        return problems  # 不是这类脚本（如 collect_wp10），跳过

    if "EXPECTED_CHECKS" not in text:
        problems.append("缺少 EXPECTED_CHECKS 常量（条数不足会伪装成全部通过）")
        return problems

    if not re.search(r"complete\s*=\s*len\(checks\)\s*>=\s*EXPECTED_CHECKS", text):
        problems.append("成功判定里没有核对条数（应有 `complete = len(checks) >= EXPECTED_CHECKS`）")

    # RESULT 行里要写出期望条数 —— 读证据的人才知道"应有几条"。
    if not _reports_expected_count(text):
        problems.append(
            "RESULT 行没有写出期望条数（读证据的人看不到应有几条）"
        )
    return problems


def _strip_comments(text: str) -> str:
    """去掉整行注释。

    **必须去注释再匹配**：本仓库的脚本里**故意保留**了
    "原来写的是 `version == EXPECTED_REVISION`，那样会在下一个工作包失效"
    这类说明（它们是给后来者看的教训）。直接全文正则会把这段说明
    当成"还在用等于判据" —— 那是**假阳性**（本机实测踩到：6 个脚本全被误报）。
    只查真实代码，不查解释它的文字。
    """
    lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


#: 迁移版本常量的名字（判据里出现它就等于"在断言链上位置"）。
_REVISION_CONSTANT = r"(?:EXPECTED_REVISION|REVISION_INTRODUCED)"

#: **任意**与版本常量做相等/不等比较 —— 不限定左操作数叫什么。
#:
#: 早期版本写的是 `version\s*==\s*...`，只认 `version` —— 于是
#: `head == REVISION_INTRODUCED`（WP21，实测漏网）与
#: `rev == REVISION_INTRODUCED`（WP19，实测漏网）**两种写法全都躲过门禁**。
#: 判据被"变量叫什么名字"决定，而不是被"有没有做单调判断"决定 ——
#: 这正是本脚本存在的理由（ISS-105 同族：防线的判定依据可被改名绕过）。
_REVISION_EQUALITY_RE = re.compile(
    r"(?:==|!=)\s*" + _REVISION_CONSTANT + r"\b"
    r"|" + _REVISION_CONSTANT + r"\b\s*(?:==|!=)"
)

#: helper 的**调用**（`(?<!def )` 排除函数定义那一行，见下）。
#: 只定义不调用 = 死代码 = 那条"不早于"判据根本没生效 —— 而脚本仍会
#: 打印出一行 PASS 的"迁移 head ..."，读证据的人看不出它是恒真的
#: （ISS-82 同族：判据必须能区分两种实现；WP19 实测踩到）。
_REVISION_HELPER_CALL_RE = re.compile(r"(?<!def )_revision_is_at_least\s*\(")

#: **恒真判据**：`len(a) >= len(b)` / `len(a) <= len(b)` 形态。
#:
#: 这是 ISS-115（2026-09-27 收口轮实测）：`_revision_is_at_least` 的第一版
#: 修复为了避开"恰好等于"，写成 `len(current) >= len(introduced)` ——
#: 而本项目**全部 16 个 revision id 都是 12 位十六进制**，于是不相等时
#: 该式**恒为 True**。等式判据被换成了**恒绿判据**，比原缺陷更隐蔽：
#: 永不失败，因此永不报警。存量 12 个脚本全踩中。
#:
#: 判据：任何"拿两个表达式的长度比大小"的写法都拒绝 —— 长度不是顺序。
_REVISION_LENGTH_HACK_RE = re.compile(
    r"len\(\s*\w+\s*\)\s*(?:>=|<=|>|<)\s*len\(\s*\w+\s*\)"
)

#: **位置参数**调 `walk_revisions(...)` —— 顺序靠记性，实测传反过。
#:
#: ISS-116：WP19 的 helper 写的是 `walk_revisions(current, introduced)`，
#: 而真实签名是 `(base, head)`。参数传反后方向整体反转，实测 4 个场景错 3 个。
#: 只认位置参数、放行关键字参数：`(base=..., head=...)` 是自解释的，不可能传反。
_REV_WALK_POSITIONAL_RE = re.compile(
    r"walk_revisions\(\s*(?!\s*\w+\s*=\s*)(?!\s*base\s*=\s*)[^)=,]+(?:,|\s*\))"
)

#: 取真实迁移链的**正确**写法（关键字实参，顺序自解释）。
_REV_WALK_KEYWORD_RE = re.compile(r"walk_revisions\(\s*base\s*=[^)]*head\s*=")

#: 按真实链上位置比较先后。
_REV_CHAIN_INDEX_RE = re.compile(r"\.index\s*\(")


def check_no_exact_revision(path: Path) -> list[str]:
    """不能用"恰好等于某个迁移版本"做判据，且"不早于"helper 必须真被调用。"""
    problems: list[str] = []
    text = path.read_text(encoding="utf-8")
    if not _uses_check_pattern(text):
        return problems
    code = _strip_comments(text)

    if _REVISION_EQUALITY_RE.search(code):
        problems.append(
            "使用了 `<任意变量> == <迁移版本常量>` 判据 —— 迁移 head 是单调推进的，"
            "用等于写会在下一个工作包失效（改用 `_revision_is_at_least(...)`）"
        )

    if _REVISION_LENGTH_HACK_RE.search(code):
        problems.append(
            "用 `len(a) >= len(b)` 表示迁移版本先后 —— 本项目的 revision id 长度"
            "一致（都是 12 位），该式在 `a != b` 时**恒为 True**，是恒真判据"
            "（ISS-115）。应沿真实迁移链按位置比较："
            "`ScriptDirectory.walk_revisions(base='base', head='heads')` 取 chain，"
            "再比 `chain.index(current) <= chain.index(introduced)`"
        )

    # 死代码判据：声明了版本常量，却没有一处 helper 调用 ⇒
    # 那个"不早于"函数只是写着好看，实际判据仍是等式（或干脆没有）。
    declared = re.search(r"^\s*" + _REVISION_CONSTANT + r"\s*=", code, re.M)
    if declared and not _REVISION_HELPER_CALL_RE.search(code):
        problems.append(
            "声明了迁移版本常量，但全文没有调用 `_revision_is_at_least(...)` "
            "—— 只定义不调用是死代码，那条判据没有生效"
        )
    return problems


def _function_body(text: str, name: str) -> str | None:
    """取出顶层函数 `name` 的源码（从 `def` 到下一个顶层语句为止）。

    **为什么不能"全文搜特征就放行"**：`walk_revisions` 在两个地方会出现 ——
    函数体里（真实调用）与文件别处的解释性文字里。只查全文会让"函数体写错、
    但文件里另有一处正确写法的注释"通过。必须把范围收缩到函数体。

    **为什么不能直接用"下一个顶格行"当边界**（第一版实测全红）：函数签名本身
    可以换行，`def foo(\n    a, b)` 的续行以空格开头但**签名文本的行内换行**
    会让"从 `)` 之后开始找顶格行"把签名续行当成函数体结尾。
    因此这里先按**括号配平**吃掉整个签名（到配平后那行的 `:`），再从下一行
    开始找第一个顶格语句 —— 顶格定义（`def` / `class` / 装饰器）或顶格语句
    （赋值、with、调用）都算边界。
    """
    match = re.search(r"^[ \t]*def " + re.escape(name) + r"\s*\(", text, re.M)
    if not match:
        return None

    # 从 `def` 起按括号配平找签名结束（配平归零后的第一个 `:`）。
    depth = 0
    idx = match.end() - 1  # 指向签名的 `(`
    while idx < len(text):
        ch = text[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                # 配平归零：跳到该行末尾（签名可能还有 `-> T:`）。
                line_end = text.find("\n", idx)
                if line_end < 0:
                    return text[match.start():]
                idx = line_end + 1
                break
        idx += 1
    else:
        return text[match.start():]

    # 从函数体首行起，找第一个顶格语句作为结束边界。
    body_start = idx
    nxt = re.search(r"^(?=\S)", text[body_start:], re.M)
    if nxt:
        body_end = body_start + nxt.start()
    else:
        body_end = len(text)
    return text[match.start():body_end]


def check_revision_helper_implementation(path: Path) -> list[str]:
    """凡定义了 `_revision_is_at_least` 的脚本，"不早于"的判断必须是沿真实链序比较。

    **为什么单查"函数存在"与"函数被调用"还不够**（本仓实测三种形态）：

    | # | 形态 | 后果 | 现有判据 |
    |---|---|---|---|
    | 1 | 定义了但从不调用 | 死代码，那条判据没生效 | 已有（死代码判据） |
    | 2 | 调用了，内部写 `len(a) >= len(b)` | **恒真**，永久绿灯（ISS-115） | 已有（长度法判据） |
    | 3 | 调用了，内部 `walk_revisions(current, introduced)` **参数传反** | **方向全错**（ISS-116） | **本条新增** |

    第 3 种是本轮实测漏掉的那一个：批量修恒真判据时，修复名单是"12 个脚本"
    （靠记忆列举），而 WP19 的 helper 形状不同、不在名单里 —— 于是它带着
    传反的参数继续输出 `CHECK OK`。**修复名单本身没有任何判据**，这正是
    ISS-104/105/111/114 同族的第三次复发：凡"覆盖范围由人手写"，就必须有
    一条判据对账它。本条就是那条对账。
    """
    problems: list[str] = []
    text = path.read_text(encoding="utf-8")
    if not _uses_check_pattern(text):
        return problems
    body = _function_body(_strip_comments(text), "_revision_is_at_least")
    if body is None:
        return problems

    if _REV_WALK_POSITIONAL_RE.search(body):
        problems.append(
            "`_revision_is_at_least` 里用位置参数调 `walk_revisions(...)` —— "
            "真实签名是 `(base, head)`，顺序靠记性，实测传反过（ISS-116）。"
            "必须写成 `walk_revisions(base='base', head='heads')`"
        )
    if not _REV_WALK_KEYWORD_RE.search(body):
        problems.append(
            "`_revision_is_at_least` 里没有 `walk_revisions(base=..., head=...)` "
            "—— 没有取到真实迁移链"
        )
    if not _REV_CHAIN_INDEX_RE.search(body):
        problems.append(
            "`_revision_is_at_least` 里没有 `chain.index(...)` —— "
            "先后必须按真实链上位置判断，不能靠长度/命名（ISS-115/116）"
        )
    return problems


def _ast_exit_args(text: str) -> list[str]:
    """解析出脚本里**真正**的进程退出码实参（AST 口径）。

    认四种形态，全部是**语法节点**，字符串字面量里的同名文字一律不算：

      · `raise SystemExit(0 if verdict else 1)`   → `"0 if verdict else 1"`
      · `raise SystemExit`（无实参）              → `"(无实参)"`
      · `sys.exit(...)`                           → 实参
      · `SystemExit(...)`（被 return/赋值使用）    → 实参

    **为什么非走 AST 不可**：`_strip_comments()` 只删整行 `#` 注释，
    不删字符串字面量 —— 而本仓脚本**故意保留**说明文字（那是给后来者看的教训），
    其中就有用引号引起来的 `raise SystemExit(...)`。正则读不出引号，
    于是"把这段当退出码" ⇒ 删掉真代码后判据照样绿。

    解析失败（语法错误）时返回空列表 —— 那是 `check_inline_compiles`
    与 pytest 该报的事，不在这里越俎代庖。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    def _is_attr_sys_exit(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
            and node.attr == "exit"
        )

    args: list[str] = []
    for node in ast.walk(tree):
        call: ast.Call | None = None
        if isinstance(node, ast.Raise) and node.exc is not None:
            exc = node.exc
            if isinstance(exc, ast.Call) and (
                (isinstance(exc.func, ast.Name) and exc.func.id == "SystemExit")
                or _is_attr_sys_exit(exc.func)
            ):
                call = exc
            elif isinstance(exc, ast.Name) and exc.id == "SystemExit":
                args.append("(无实参)")
                continue
        elif isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id == "SystemExit")
            or _is_attr_sys_exit(node.func)
        ):
            call = node
        if call is None:
            continue
        args.append(ast.unparse(call.args[0]) if call.args else "(无实参)")
    return args


def check_returns_exit_code(path: Path) -> list[str]:
    """凡累积 `checks` 的脚本**必须**显式给出与结论一致的退出码（ISS-168）。

    ## 为什么需要这一条

    WP25 收尾时实测：`tools/m3/collect_wp25_code_review.py` 报
    `31 PASS / 4 FAIL`，而 `echo $?` 是 **0** —— 它结尾只有
    `write_text(...)` + `print(...)`，**没有任何退出码语句**。

    后果链（逐环都可核对）：

      ① `tools/m1/rerun_all_evidence.py`（收口第 11 步的载体）对每个脚本
         只断言两件事：`rc == 0` 与『证据文件 mtime 前进』；
      ② 一个**恒返回 0** 的脚本，这两条**都会 PASS**；
      ③ 它虽然把 `RESULT` 列进了表格，但那列**只打印、不参与任何断言**；
      ④ ⇒ **判据报红，没有任何机械后果。**

    这是 ISS-143（『没跑』与『跑失败』同形）的**变体**，而且更坏：
    整份证据的"红/绿"对下游**完全不可见**。同款缺陷当时也在
    `tools/m3/collect_wp24_code_review.py`（已随 `afdd91f` 提交），
    它**只因为『恰好 0 FAIL』而从未暴露** —— 那是运气，不是防线。

    ## 判据口径

    · **触发条件**：脚本累积 `checks`（即 `_uses_check_pattern`），
      说明它的结论是"若干条 CHECK 的红/绿汇总" ⇒ 必须有机械出口。
    · **要求的形态**：出现 `raise SystemExit(...)` 或 `sys.exit(...)`，
      且其**实参不是裸 `0`** —— 裸 `SystemExit(0)` 同样让红变绿，
      与"没有退出码"在行为上等价（只是看起来更努力）。

    ## 刻意不做的两件事（避免假阳性 / 假保证）

    · **不检查退出码的表达式长什么样**（`0 if all(checks) else 1` /
      `0 if verdict else 1` / 自定义 `main()` 返回 int 都合法）。
      判据按**行为**（"有没有机械出口"）定，不按**写法**定 ——
      按写法定会让"换个等价写法"就能绕过（ISS-82 同族）。
    · **不要求必须有 `RESULT=` 行**：那是另一件事（`_reports_expected_count`
      已管"RESULT 里要写期望条数"），两者不该混成一条，
      否则红的**原因**会被打印错（本项目错误 ⑫）。

    ## ⚠️ 本检查的第一版自己踩了同型的坑（必须记一笔）

    第一版用的是文本正则：

        re.findall(r"(?:raise\\s+SystemExit|sys\\.exit)\\s*\\(\\s*([^)]*)\\)", code)

    而 `_strip_comments()` **只删整行 `#` 注释，不删字符串字面量**
    （它的 docstring 里写明了：仓库脚本会**故意保留**旧写法的说明文字）。
    于是 `collect_wp25_code_review.py` 里那句用引号引起来的说明
    `"（本脚本自查：`raise SystemExit(0 if _verdict else 1)`；AST 判定）"`
    **被当成了真实退出码** —— 实测该正则在同一份文件里命中 **3 处，其中 2 处是引文**。

    后果：把真退出码删掉后，正则**照样命中引文** ⇒ 本检查**不报红** ⇒
    反向验证用例 `gate-detects-missing-exit-code` 永远拿不到 `rc=1`。
    这与 C31（docstring 里的 `inbound == 0` 反例）**是同一个坑**，
    只是这次坑在"判据自己的文档"里。**写判据的人也会犯他要防的那个错。**

    故改为 **AST**：`ast.parse` 之后只认真正的节点，
    字符串字面量里的文字**永远不是节点** —— 它不依赖"作者怎么写注释"，
    且顺带能识别 `sys.exit(...)` 与 `raise SystemExit` 两种形态。

    实测（本仓 26 个 CHECK 模式脚本）：AST 口径与正则口径**逐脚本零差异**
    （26/26 一致，无一例假阳性/假阴性）—— 改口径不会把好脚本判红。
    """
    problems: list[str] = []
    text = path.read_text(encoding="utf-8")
    if not _uses_check_pattern(text):
        return problems

    exit_args = _ast_exit_args(text)
    if not exit_args:
        problems.append(
            "累积 `checks` 却没有显式退出码（`raise SystemExit(...)` / `sys.exit(...)`）"
            "—— 脚本恒返回 0，判据报红对下游不可见（ISS-168；"
            "`rerun_all_evidence.py` 只判 `rc==0` + 证据刷新，两条都会 PASS）"
        )
        return problems

    #: 所有出口都是裸 `0`（或 `raise SystemExit` 无实参）⇒ 与"没有退出码"行为等价。
    if all(arg.strip() in ("0", "(无实参)", "") for arg in exit_args):
        problems.append(
            "退出码恒为 `0`（裸 `SystemExit(0)`）—— 与『没有退出码』行为等价，"
            "红仍然不可见（ISS-168）。应写成 `raise SystemExit(0 if <结论> else 1)`"
        )
    return problems


def main() -> int:
    scripts = _iter_scripts()
    if not scripts:
        print(
            "未找到证据脚本（tools/**/collect_*_evidence.py / "
            "tools/**/refresh_*_evidence.py）"
        )
        return 1

    total_problems = 0
    for path, requires_inline in scripts:
        problems = (
            check_inline_compiles(path, required=requires_inline)
            + check_expected_checks(path)
            + check_no_exact_revision(path)
            + check_revision_helper_implementation(path)
            + check_returns_exit_code(path)
        )
        if problems:
            print(f"FAIL {path.name}")
            for problem in problems:
                print(f"       - {problem}")
            total_problems += len(problems)
        else:
            print(f"OK   {path.name}")

    print()
    if total_problems:
        print(f"共 {len(scripts)} 个脚本，发现 {total_problems} 处问题")
        print(f"（仓库根：{REPO_ROOT}）")
        return 1
    print(f"全部 {len(scripts)} 个证据脚本通过自检")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
