#!/usr/bin/env python
"""WP-DEBT-03 代码审查证据（三遍法：结构 → 逐行 → 边界与加固）。

产出 `evidence/m3/discipline-code-review.log`。

**为什么要把审查结果落成文件**：审查结论是"声称"，而声称需要证据。
本项目已踩过"文档/看板是唯一声称方时没有任何机械判据与它对账"
（ISS-78/84/85/86/94/95），因此本记录里每一处「通过」都必须附上
**能在代码里当场指认的判据**（判据纪律第 9 条：文档说"这条有判据"，
必须能当场指出脚本里哪个函数）。

## 本包只有四个源文件（+两个判据自身的判据）

| 文件 | 角色 |
|---|---|
| `tools/quality/check_evidence_scope.py` | 判据 D 本体（铁律 14 / ISS-162） |
| `tools/quality/check_shared_db_quiet.py` | 判据 E 本体（铁律 17 / ISS-163） |
| `tools/quality/collect_discipline_reverse_evidence.py` | 反向验证（把错误实现放回去必须报红） |
| `services/backend/tests/test_evidence_scope.py` | 判据 D 自身的判据 |
| `services/backend/tests/test_shared_db_quiet.py` | 判据 E 自身的判据 |
| `docs/m3/工程债-纪律判据化说明.md` | 本包说明书（§4~§10 由审查驱动补齐） |

## 审查方式

按 `code-review` 技能的三遍法（结构 → 逐行 → 边界与加固），
五维度（Security / Performance / Correctness / Maintainability / Testing）。

## ⚠️ 本记录里最该记住的一条

**第一遍读代码时发现的第一条缺陷，是"判据自己"的**（不是它检查的对象）。
三遍法在本包当场抓出 **7 处判据自身的缺陷**，其中 **1 处会让 CI 门 3 直接红**：

| # | 缺陷 | 形态 | 后果 |
|---|---|---|---|
| C1 | 反向验证 `_default_dsn()` 读 `DEFAULT_DATABASE_URL` 常量 | 绕开判据自己的 `_resolve_dsn()` | CI 上连**不存在**的 `rednote_test` ⇒ fail-closed ⇒ **门 3 红** |
| C2 | 反向验证 `--fast` 是**空壳参数**（argparse 收下但从未使用） | "声称支持但没实现"（ISS-95 轻量版） | `--fast` 与完整跑完全同形，CI 无法挂载 |
| C3 | 两模式**共用一个产物文件名** | 覆盖范围不同而名字相同（铁律 14 长在证据命名上） | CI 的 fast 跑**覆盖**收口期完整证据 |
| C4 | 判据 D `_RESULT_RE` 定义后全文未用 | 死代码 | 读者以为有 RESULT 解析，其实没有 |
| C5 | 判据 D `run_checks` 的 `reason` 未使用 | 白名单**不核对理由**（纪律禁止的形态） | 白名单退化成"把报红的地方都加进来" |
| C6 | 判据 D docstring 写 A1=26/A2=27/B=27，实地 27/28/28 | 覆盖范围由人写（长在文档上） | 文档与磁盘脱节 |
| C7 | 反向验证 docstring 表标签 `D①~D⑥` 与代码节 `D①~D⑤` **整体错位一位** | 文档与代码各写一份编号 | 表说 `D③` 的地方代码是 `D②` |

C1 是**最危险**的那一类：它不是"判据抓到了问题"，而是"载体连错了库" ——
两者都打印 `FAIL`，形状同形（ISS-143）。
C2/C3/C6/C7 都是同一个病（**覆盖范围/编号由人写**）在不同藏身处复发。

跑法：

    python tools/quality/collect_discipline_code_review.py

退出码 0 = 全部 CHECK 通过。
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"
OUT_FILE = EVIDENCE_DIR / "discipline-code-review.log"

TOOL_D = REPO_ROOT / "tools" / "quality" / "check_evidence_scope.py"
TOOL_E = REPO_ROOT / "tools" / "quality" / "check_shared_db_quiet.py"
REVERSE = REPO_ROOT / "tools" / "quality" / "collect_discipline_reverse_evidence.py"

TEST_D = REPO_ROOT / "services" / "backend" / "tests" / "test_evidence_scope.py"
TEST_E = REPO_ROOT / "services" / "backend" / "tests" / "test_shared_db_quiet.py"

DOC = REPO_ROOT / "docs" / "m3" / "工程债-纪律判据化说明.md"

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


def sans_comments(src: str) -> str:
    """去掉整行注释（保留代码）。判据扫「代码」不扫「解释它的文字」。"""
    return "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )


def sans_doc(src: str) -> str:
    """把**注释与文档字符串**替换成空白，保留行结构与普通字符串字面量。

    ⚠️ 本包三处 docstring 里**故意**写了反例字面量（判据 D 写 `evidence-marker`
    讲"子串判定会误判"、反向验证写 `SCOPE_PATTERNS` 讲变异）。
    只剥 `#` 注释时，任何"扫代码特征"的判据都会把**解释文字当违规代码**
    —— 本项目已现身 6 次的形态。用 `ast` 取字符串表达式节点的行区间，比正则可靠。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return sans_comments(src)

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
    return sans_comments("\n".join(src_lines))


def _func_source(path: Path, name: str) -> str:
    """按行号取出**顶层**函数源码（含装饰器之前到函数结束）。"""
    src = _read(path)
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            end = node.end_lineno or node.lineno
            return "\n".join(src.splitlines()[start - 1 : end])
    return ""


def _method_source(path: Path, class_name: str, method_name: str) -> str:
    """取类方法源码。

    ⚠️ 必须显式下钻到类体 —— `_func_source` 只扫**顶层** `tree.body`，
    对类方法**恒返回空串**，判据于是恒红（本项目判据纪律的同款形态：
    **判据找不到东西时，报的是"位置不对"而不是"缺陷"**）。
    """
    src = _read(path)
    tree = ast.parse(src)
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        for sub in node.body:
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == method_name:
                start = min([sub.lineno] + [d.lineno for d in sub.decorator_list])
                end = sub.end_lineno or sub.lineno
                return "\n".join(src.splitlines()[start - 1 : end])
    return ""


def _load(path: Path, name: str):
    """按路径加载模块（tools/ 不是包）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _table_rows(block: str) -> list[list[str]]:
    """解析 markdown 表格的**数据**行（跳过表头与分隔线）。

    ⚠️ 必须显式跳过**表头**：`| # | 变异 | 期望 | … |` 也以 `|` 起头，
    只按"是不是分隔线"过滤会把它算成一行数据 —— 实测把 11 行表数成 12 行。
    """
    rows: list[list[str]] = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not cells or set("".join(cells)) <= set("-: "):
            continue
        rows.append(cells)
    # 第一行是表头（`| # | 变异 | … |`）—— 判据是"表格有几条变异"，
    # 表头不是变异（ISS-137 族：两种独立事实不可互相替代）。
    return rows[1:] if rows else rows


def main() -> int:  # noqa: C901 —— 三遍法逐条覆盖，拆开反而看不清顺序
    tool_d_src = _read(TOOL_D)
    tool_e_src = _read(TOOL_E)
    rev_src = _read(REVERSE)
    doc_src = _read(DOC)

    code_d = sans_comments(tool_d_src)
    code_e = sans_comments(tool_e_src)
    code_d_nodoc = sans_doc(tool_d_src)

    #: 反向验证的「分模式条数」判据 —— **只看形态，不写死数字**。
    #:
    #: ⚠️ **2026-10-01 三遍法自纠（C11：判据自己违反了它要守的纪律）**：
    #: 本 CHECK 初版写作 `"10 if args.fast else 17" in rev_src`。那正是本包
    #: `[逐行-2]` 明文禁止的「手写数字」—— 只不过这次手写数字藏在**判据里**，
    #: 于是它比藏在文档里更隐蔽：文档漂移只是文档不准，**判据漂移会恒红**
    #: （条数只要变一次，[边界-9]/[逐行-4] 就永远报红 ⇒ 永远红 = 没有判据，ISS-143）。
    #: 修法：断言**形态**（`EXPECTED_CHECKS = <整数> if args.fast else <整数>`），
    #: 由 `exact` 判据在**运行期**保证数字与实测一致 —— 数字的正确性只有一个来源，
    #: 且那个来源是运行时事实（现场条数），不是任何人写下的字面量。
    _split_expect_re = re.compile(
        r"EXPECTED_CHECKS\s*=\s*(\d+)\s*if\s+args\.fast\s+else\s+(\d+)"
    )
    split_expect = _split_expect_re.search(rev_src)
    split_expect_ok = bool(split_expect) and int(split_expect.group(1)) < int(split_expect.group(2))
    split_expect_desc = (
        f"fast={split_expect.group(1)}/完整={split_expect.group(2)}（形态匹配，数字由运行期 exact 判据保证）"
        if split_expect
        else "未匹配到分模式条数形态"
    )

    lines.append("== WP-DEBT-03 三遍法代码审查证据（机械可复现） ==")
    lines.append(f"判据 D：{TOOL_D.relative_to(REPO_ROOT)}")
    lines.append(f"判据 E：{TOOL_E.relative_to(REPO_ROOT)}")
    lines.append(f"反向验证：{REVERSE.relative_to(REPO_ROOT)}")
    lines.append("")
    lines.append("本脚本只做一件事：把 docs/m3/工程债-纪律判据化说明.md 里")
    lines.append("**每一条「已修 / 有判据」**变成能在代码里当场指认的判据。")
    lines.append("文档声称 = 待验证假设。")

    # ==================================================================
    head("--- 第一遍：结构与正确性 ---")

    # ---------- 判据 D ----------
    # D-1 A1/A2 必须由**现场加载载体**读出，不许正则猜 glob
    #     （判据 D 的初版正是"用正则扫 .glob 字面量"，实测读到 A1=0）。
    live_load = ("_load_module" in code_d) and ('getattr(module, "SCRIPTS"' in code_d)
    live_iter = ('getattr(module, "_iter_scripts"' in code_d) and "iterator()" in code_d
    emit(
        live_load and live_iter,
        "[结构-1] 判据D A1/A2 由**现场加载载体**读出（不手抄、不正则猜）",
        f"读 SCRIPTS={'有' if live_load else '无'} 调 _iter_scripts={'有' if live_iter else '无'}",
    )

    # D-2 B 必须按**行为**反推，不看文件名
    by_behavior = (
        "_WRITE_METHODS" in code_d
        and "_WRITE_OPEN_MODES" in code_d
        and "_points_at_evidence" in code_d
        and 'ast.walk(tree)' in code_d.replace("\n", "")
    )
    emit(
        by_behavior,
        "[结构-2] 判据D B 由**行为**反推（AST 扫写入调用 + 路径分类），不看文件名",
        f"写入方法集={'有' if '_WRITE_METHODS' in code_d else '无'} "
        f"open 模式集={'有' if '_WRITE_OPEN_MODES' in code_d else '无'}",
    )

    # D-3 活性下界（反推失效会报红，不静默）
    floor = re.search(r"^MIN_REVERSE\s*=\s*(\d+)", code_d, re.M)
    emit(
        bool(floor) and int(floor.group(1)) >= 1,
        "[结构-3] 判据D 有活性下界 MIN_REVERSE（反推不出东西时报红，不静默）",
        f"MIN_REVERSE={floor.group(1) if floor else '（缺）'}",
    )

    # D-4 三方对账必须**双向**差集 —— 只看一个方向会漏掉一半失效面
    two_dirs = ("a1_minus_b" in code_d or "a1 - b" in code_d) and (
        "b_minus_a1" in code_d or "b - a1" in code_d
    )
    emit(
        two_dirs,
        "[结构-4] 判据D 是**双向**差集（A1−B 与 B−A1 都在判，不只防一个方向）",
        f"A1−B={'有' if 'a1_minus_b' in code_d or 'a1 - b' in code_d else '无'} "
        f"B−A1={'有' if 'b_minus_a1' in code_d or 'b - a1' in code_d else '无'}",
    )

    # D-5 `_evidence_kind` 必须按**路径段**判（不是子串）
    kind_is_segment = ("re.split(" in code_d) and ("EVIDENCE_DIR_NAME not in segments" in code_d)
    emit(
        kind_is_segment,
        "[结构-5] 判据D `_evidence_kind` 按**路径段**判（独立 evidence 段），不按子串",
        f"按段切分={'有' if 're.split(' in code_d else '无'} "
        f"要求独立段={'有' if 'EVIDENCE_DIR_NAME not in segments' in code_d else '无'}",
    )

    # D-6 `_points_at_evidence` 必须"先拼完整路径再分类"
    #     （初版按"左半含 evidence 就 real"判 ⇒ test_contract.py 被当成证据脚本）
    points_impl = _func_source(TOOL_D, "_points_at_evidence")
    joins_then_classifies = "fragments" in points_impl and "_evidence_kind(" in points_impl
    emit(
        joins_then_classifies,
        "[结构-6] 判据D 路径判定是「先拼完整路径，再做一次分类」（不按中间步骤判）",
        f"_path_fragments→join→_evidence_kind={'有' if joins_then_classifies else '无'}"
        f"（取到 {len(points_impl)} 字符；为 0 说明又找错位置了）",
    )

    # D-7 D2b 必须**核对**豁免理由（不是裸名单）
    d2b = _func_source(TOOL_D, "run_checks")
    reason_used = ("for rel, reason in EXEMPT_FROM_A1.items()" in d2b) and (
        'f"（{rel}（' in d2b or "原理由：{reason}" in d2b
    )
    emit(
        reason_used,
        "[结构-7] 判据D D2b **核对**豁免理由（理由被拼进报红信息，不是裸名单）",
        f"reason 被使用={'有' if reason_used else '无'}",
    )

    # D-8 豁免要判**两个方向 + 路径存在**
    #     ⚠️ 源码写的是 `if rel not in b:`（否定形态）—— 断言必须按**实际形态**写。
    #     本 CHECK 初版写 `"rel in b" in d2b`，而 `"rel not in b"` **不含**子串
    #     `"rel in b"`（中间多了 `not `）⇒ 恒假、恒红（实测 [结构-8] 假红）。
    #     这是判据纪律的老形态：判据绑在"某段文本长什么样"上，而不是绑在事实上。
    exempt_dirs = (
        "rel not in b" in d2b
        and "rel in a1" in d2b
        and "(REPO_ROOT / rel).exists()" in d2b
    )
    emit(
        exempt_dirs,
        "[结构-8] 判据D 豁免判三个方向（仍写 evidence / 不在 A1 / 路径存在）",
        f"仍写evidence={'有' if 'rel not in b' in d2b else '无'} "
        f"不在A1={'有' if 'rel in a1' in d2b else '无'} "
        f"路径存在={'有' if '(REPO_ROOT / rel).exists()' in d2b else '无'}",
    )

    # ---------- 判据 E ----------
    # E-1 独立连接（E3）
    probe = _func_source(TOOL_E, "probe_quiet")
    own_conn = "psycopg.connect(" in probe
    emit(
        own_conn,
        "[结构-9] 判据E 用**独立连接**查占用（E3：不复用被测连接，否则自己数自己）",
        f"自带 psycopg.connect={'有' if own_conn else '无'}；"
        f"连接用后即关={'有' if 'with psycopg.connect' in probe else '无'}",
    )

    # E-2 E1 必须断言"确实读到库名"
    e1_guard = ("current_database()" in probe) and ("if not database:" in probe)
    emit(
        e1_guard,
        "[结构-10] 判据E E1 断言「确实读到目标库名」（防连错库伪装成无人用）",
        f"读库名={'有' if 'current_database()' in probe else '无'} "
        f"空值即拒跑={'有' if 'if not database:' in probe else '无'}",
    )

    # E-3 排除自身 pid
    self_excl = "pid <> pg_backend_pid()" in tool_e_src
    emit(
        self_excl,
        "[结构-11] 判据E E2 排除自身连接（`pid <> pg_backend_pid()`）",
        f"排除自身={'有' if self_excl else '无'}",
    )

    # E-4 fail-closed
    fail_closed = "SharedDatabaseUnverifiable" in tool_e_src and (
        "except Exception as exc:" in probe
    )
    emit(
        fail_closed,
        "[结构-12] 判据E E4 fail-closed（连接/查询异常 ⇒ 拒跑，不是放行）",
        f"专属异常类={'有' if 'SharedDatabaseUnverifiable' in tool_e_src else '无'} "
        f"兜底 except={'有' if 'except Exception as exc:' in probe else '无'}",
    )

    # E-5 DSN 单一入口 + 流向环境变量（ISS-108：不许硬编码）
    resolve = _func_source(TOOL_E, "_resolve_dsn")
    single_entry = ("DB_URL_ENV" in code_e) and ("TEST_DB_URL_ENV" in code_e) and (
        "os.environ.get(DB_URL_ENV)" in resolve
    )
    emit(
        single_entry,
        "[结构-13] 判据E DSN 走**单一入口** `_resolve_dsn`，且流向可覆盖的环境变量",
        f"两个环境变量名={'有' if 'DB_URL_ENV' in code_e and 'TEST_DB_URL_ENV' in code_e else '无'} "
        f"入口读环境变量={'有' if 'os.environ.get(DB_URL_ENV)' in resolve else '无'}",
    )

    # E-6 --ci 响亮 SKIP
    ci_skip = ("--ci" in code_e) and ("[SKIP]" in code_e) and (
        "在 CI 上不适用" in tool_e_src
    )
    emit(
        ci_skip,
        "[结构-14] 判据E `--ci` **响亮 SKIP** 并写明理由（ISS-118：不适用 ≠ 不存在）",
        f"分支={'有' if '--ci' in code_e else '无'} "
        f"显式 SKIP 行={'有' if '[SKIP]' in code_e else '无'}",
    )

    # ==================================================================
    head("--- 第二遍：逐行（文档声称 vs 磁盘事实） ---")

    # 15. 源码里 `` `tools/....py` `` 形式的引用必须**全部存在**
    #     （WP-DEBT-02 的三遍法实测出 3 处失效引用 —— 引用的路径写错，
    #      与「没有引用」在效果上同形）。
    claimed: set[tuple[str, str]] = set()
    for path, src in (
        (TOOL_D, tool_d_src),
        (TOOL_E, tool_e_src),
        (REVERSE, rev_src),
        (TEST_D, _read(TEST_D)),
        (TEST_E, _read(TEST_E)),
    ):
        for ref in re.findall(r"`((?:tools|services|docs)/[\w./\-\u4e00-\u9fff]+\.(?:py|md|yml))`", src):
            claimed.add((path.name, ref))
    missing = sorted({ref for _src, ref in claimed if not (REPO_ROOT / ref).exists()})
    emit(
        not missing,
        "[逐行-1] 源码里 `tools/…py` 形式的路径引用**全部存在**（引用的路径写错 = 没有引用）",
        f"检查 {len(claimed)} 处，不存在 {len(missing)} 处"
        + (f"：{missing}" if missing else ""),
    )

    # 16. 判据 D 的 docstring **不许手写覆盖范围数字**
    #     （本包审查抓到的真缺陷 C6：docstring 写 26/27/27，实地 27/28/28；
    #      而"改对数字"是**治不了**的 —— 本包自己新增一个脚本后它立刻又漂移。
    #      正解是**禁止手写**：数字一律以现场输出为准。）
    #
    #     ⚠️ 本 CHECK 初版写的是"断言 docstring 数字与现场一致"——
    #     那要求**每次**改动后手工同步，**注定再次漂移**（本包实测：写成 28/29/29
    #     之后增加一个脚本就立刻又红）。判据应当**消灭手写**，而不是**追赶手写**。
    stale_rows = re.findall(
        r"^\|\s*(?:A1|A2|B)\b[^|]*\|\s*(\d+)\s*\|", tool_d_src, re.M
    )
    emit(
        not stale_rows,
        "[逐行-2] 判据D docstring **不手写**覆盖范围数字（手写必然漂移，改数字治不了）",
        f"手写数字行={len(stale_rows)} 处"
        + (f"（{stale_rows}）" if stale_rows else "（数字一律以现场输出为准）"),
    )

    # 17. 反向验证的「变异清单表」与代码节**逐行对账**（本包抓到的真缺陷 C7）
    #
    #     ⚠️ **2026-10-01 三遍法自纠（C12：覆盖范围由人写，第 8 次）**：
    #     本条原先把"用例节"写成硬编码集合 `{0,1,2,3,4,5,7,8,9,10,11}` ——
    #     这正是本包判据 D 要消灭的东西：**一份由人维护的名单**。
    #     加一个用例（D⑥ 落在新的 `[6]`）它就立刻失准，而且失准的方式是
    #     「表行数与用例节数不等」—— 看起来像"文档漏写一行"，实则是**判据的名单过期**。
    #     修法：改用**排除法** —— 用例节 = 代码里实际出现的全部节号 − 已知的收尾节。
    #     收尾节是**结构性**的（还原 / 真树 / 结论），它们的身份由语义决定、不随用例增减，
    #     故只有这一小份名单可以留在代码里；而"哪些是用例节"完全由磁盘决定。
    section = re.search(r"## 变异清单(.*?)\n## ", rev_src, re.S)
    table_block = section.group(1) if section else ""
    rows = _table_rows(table_block)
    code_secs = {int(n) for n in re.findall(r'emit\("## \[(\d+)\]', rev_src)}
    #: 收尾节的节号（**它们的身份由语义决定，不随用例增减**）：
    #: `[9]`/`[15]` 是判据 D·F / 判据 E 的"还原后回绿"、`[16]` 是真树未触碰、`[17]` 是结论。
    #: ⚠️ 这不是"名单"：`code_secs` 里除这几节之外**全部**算用例节。
    #: ⚠️ **2026-10-01 加判据 F（ISS-164）时同步过**：新增 F①/F② 两个用例节
    #:    + 还原节由 `[7]` 变 `[9]`，故这一份也随之更新。**这正是它不该是"名单"的理由** ——
    #:    但收尾节的**身份**（还原/真树/结论）不随用例增减，所以它可以留在这里；
    #:    "哪些是用例节"仍完全由磁盘（`code_secs − tail_secs`）决定。
    tail_secs = {9, 15, 16, 17}
    case_secs = code_secs - tail_secs
    row_secs: list[int] = []
    for cells in rows:
        found = re.findall(r"`\[(\d+)\]`", " ".join(cells))
        if found:
            row_secs.append(int(found[0]))
    emit(
        len(rows) == len(case_secs)
        and case_secs <= code_secs
        and sorted(row_secs) == sorted(case_secs),
        "[逐行-3] 反向验证的表**每行写明代码节号**，且与代码节一一对上（防编号整体错位）",
        f"表 {len(rows)} 行 / 用例节 {len(case_secs)} 个 {sorted(case_secs)} / 行内节号 {sorted(row_secs)}",
    )

    # 18. 反向验证 docstring 的「两种模式」表必须与代码一致
    #     （模式名 + 两个产物文件名 + 两个期望条数）
    mode_rows = [r for r in rows if any("--fast" in c for c in r) or any("完整" in c for c in r)]
    modes_ok = (
        "| 完整（默认） |" in rev_src
        and "`--fast`" in rev_src
        and "OUT_FILE" in rev_src
        and "OUT_FILE_FAST" in rev_src
        and '"discipline-reverse.log"' in rev_src
        and '"discipline-reverse-fast.log"' in rev_src
        and split_expect_ok
    )
    emit(
        modes_ok,
        "[逐行-4] 反向验证 docstring 的模式表与代码一致（模式名 / 两个产物名 / **分模式条数形态**）",
        f"两个产物常量={'有' if 'OUT_FILE_FAST' in rev_src else '无'} "
        f"分模式条数形态={'有' if split_expect_ok else '无'}（{split_expect_desc}）",
    )

    # 19. 反向验证的 DSN 必须**从判据 E 派生**（不写第二个字面量 = 第二套口径）
    #
    #     ⚠️ 必须 `sans_doc` 之后再扫字面量 —— `_func_source` **含 docstring**，
    #     而该 docstring 里**故意**写了 `postgresql://rednote@localhost:5432/rednote_test`
    #     来讲"读常量会让 CI 连错库"。直接扫会把**解释文字当违规代码**
    #     （本项目已现身 6 次的形态）。实测 [逐行-5] 假红。
    dsn_fn = _func_source(REVERSE, "_default_dsn")
    dsn_fn_code = sans_doc(dsn_fn)
    derived = "_resolve_dsn(None)" in dsn_fn_code and "module._resolve_dsn" in dsn_fn_code
    no_literal_in_fn = not re.search(r"postgresql[^\"']*://", dsn_fn_code)
    emit(
        derived and no_literal_in_fn,
        "[逐行-5] 反向验证的 DSN **从判据 E 的解析器派生**（本包修复的 CI 阻断缺陷 C1）",
        f"调 _resolve_dsn={'有' if derived else '无'} "
        f"代码段内裸 DSN 字面量={'有' if not no_literal_in_fn else '无'}"
        f"（已剥 docstring；docstring 里的反例不计）",
    )

    # 20. 反向验证的 `_libpq` 与判据 E 的 `_libpq_dsn` 必须是**同一处**协议替换
    libpq_rev = _func_source(REVERSE, "_libpq")
    libpq_e = _func_source(TOOL_E, "_libpq_dsn")
    needle = 'dsn.replace("postgresql+psycopg://", "postgresql://", 1)'
    same_dialect = (needle in sans_doc(libpq_rev)) and (needle in sans_doc(libpq_e))
    emit(
        same_dialect,
        "[逐行-6] 两处的 SQLAlchemy 方言归一化是**同一处**（口径同源，不分叉）",
        f"反向={len(libpq_rev)} 字符 / 判据E={len(libpq_e)} 字符；同形态={'是' if same_dialect else '否'}",
    )

    # 21. 每处自纠必须带**日期**（认日期形态，不认 emoji 前缀）
    #
    #     ⚠️ 口径必须与 WP-DEBT-02 的 `[边界-14]` **一致**：
    #     那边用 `re.findall(..., src)`（**不剥** docstring），因为自纠记录正是
    #     写在 docstring 里的。本 CHECK 初版扫 `sans_doc(...)` ⇒ 把自纠记录全剥没了
    #     ⇒ 恒红（实测：自纠记录=0）。**同一件事的两处判据口径不同**，本身就是缺陷。
    #     改回同口径 `sans_comments`（去整行注释、保留 docstring），
    #     并把样本串写成不匹配正则的形态（见本脚本 docstring 里的 `2026-` 说明）。
    self_corrections = 0
    found_in: dict[str, int] = {}
    for path, src in (
        (TOOL_D, tool_d_src),
        (TOOL_E, tool_e_src),
        (REVERSE, rev_src),
        (TEST_D, _read(TEST_D)),
        (TEST_E, _read(TEST_E)),
    ):
        n = len(re.findall(r"2026-\d\d-\d\d\s+\S*自纠", sans_comments(src)))
        found_in[path.name] = n
        self_corrections += n
    emit(
        self_corrections >= 2,
        "[逐行-7] 「文档与磁盘脱节」在源码里留下**带日期**的自纠记录（≥2 条，与 WP-DEBT-02 同口径）",
        f"自纠记录={self_corrections} 条；分布={found_in}",
    )

    # 22. 判据 E 的 `_dsn_source` 回报**来源**，不把连接串写进日志
    dsn_source = _func_source(TOOL_E, "_dsn_source")
    returns_source = (
        "--dsn" in dsn_source
        and "DB_URL_ENV" in dsn_source
        and "DEFAULT_DATABASE_URL" in dsn_source
        and "return str" not in dsn_source
    )
    emit(
        returns_source,
        "[逐行-8] 判据E 的 `_dsn_source` 回报**来源**而非 DSN 本身（日志里不落连接串）",
        f"回报来源={'有' if returns_source else '无'}（不返回连接串）",
    )

    # ==================================================================
    head("--- 第三遍：边界与加固 ---")

    # 23. 反向验证在**副本**里做（真树只读）
    makes_copy = "shutil.copytree(" in rev_src
    emit(
        makes_copy,
        "[边界-1] 反向验证在**副本**里做（真树只读，进程被强杀也不污染）",
        f"copytree={'有' if makes_copy else '无'}；副本在 mkdtemp（原子唯一）="
        f"{'有' if 'mkdtemp' in rev_src else '无'}",
    )

    # 24. 还原源必须是**变异前**的文本
    original_texts = (
        "original_texts: dict[Path, str] = {}" in rev_src
        and "for path, text in original_texts.items()" in rev_src
    )
    emit(
        original_texts,
        "[边界-2] 反向验证的还原源是**变异前**的文本（不是变异后 ⇒ 副本越跑越脏）",
        f"original_texts 只读源={'有' if original_texts else '无'}",
    )

    # 25. 真树取证范围是**整树**
    scope = re.search(r"TREE_SCOPE\s*=\s*\(([^)]*)\)", rev_src)
    scope_items = re.findall(r'"([^"]+)"', scope.group(1)) if scope else []
    emit(
        len(scope_items) >= 3,
        "[边界-3] 反向验证的真树取证范围是**整树**（≥3 个顶层目录，不是自证清白的那几个文件）",
        f"范围={scope_items}",
    )

    # 26. 前置静止检查必须在 `_make_copy` **之前**（顺序是正确性问题）
    main_rev = _func_source(REVERSE, "main")
    quiet_at = main_rev.find("_assert_quiet_or_die()")
    copy_at = main_rev.find("_make_copy(")
    emit(
        0 <= quiet_at < copy_at,
        "[边界-4] 前置静止检查在 `_make_copy` **之前**（先确认库静止再动手，铁律 17）",
        f"_assert_quiet_or_die@{quiet_at} < _make_copy@{copy_at}",
    )

    # 27. 占用者用 `idle in transaction`（稳定停留）
    occupier = _method_source(REVERSE, "_Occupier", "__init__")
    stable_state = ("begin" in occupier) and ("select 1" in occupier)
    emit(
        stable_state,
        "[边界-5] 占用者用 `idle in transaction`（事务开着、语句已结束 ⇒ 稳定停留）",
        f"显式 begin={'有' if 'begin' in occupier else '无'}；"
        f"__init__ 取到 {len(occupier)} 字符",
    )

    # 28. 占用连接必须在 finally 里释放
    released_in_finally = bool(re.search(r"finally:\s*[\s\S]*?occupier\.close\(\)", main_rev))
    emit(
        released_in_finally,
        "[边界-6] 占用连接在 finally 里释放（不留幽灵占用给后续人工步骤）",
        f"finally 释放={'有' if released_in_finally else '无'}",
    )

    # 29. 门禁形态：`complete = len(checks) >= EXPECTED_CHECKS`
    #
    #     ⚠️ `EXPECTED_CHECKS` 在**完整模式**下是 `main()` 的**局部变量**
    #     （`EXPECTED_CHECKS = 10 if args.fast else 17`），而 fast 模式的分支体里
    #     写的是一次性前置返回 —— 因此模块级正则扫不到它（实测 [边界-7] 假红）。
    #     判据必须**到 `main()` 里去找**，而不是到模块顶层去找。
    gate_form = bool(re.search(r"complete\s*=\s*len\(checks\)\s*>=\s*EXPECTED_CHECKS", rev_src))
    has_expected = bool(re.search(r"^\s+EXPECTED_CHECKS\s*=\s*", rev_src, re.M))
    emit(
        has_expected and gate_form,
        "[边界-7] 反向验证有 `EXPECTED_CHECKS` 且按**门禁口径**核对条数（形态，不是变量名后缀）",
        f"常量={'有' if has_expected else '无'}（在 main() 内，非模块级）"
        f" 核对形态={'有' if gate_form else '无'}",
    )

    # 30. 另有**相等**判据（ISS-153：`>=` 拦不住"判据变多了而常量没跟着涨"）
    exact = bool(re.search(r"exact\s*=\s*len\(checks\)\s*==\s*EXPECTED_CHECKS", rev_src))
    emit(
        exact,
        "[边界-8] 另有**相等**判据（ISS-153：`>=` 拦不住常量没跟着涨）",
        f"exact 判据={'有' if exact else '无'}",
    )

    # 31. `--fast` 不是空壳（本包修复的缺陷 C2）
    #
    #     ⚠️ **2026-10-01 三遍法自纠（C9：元信息 ≠ 实现）**：本条原先只查
    #     「`args.fast` 被用于分支 + 分模式条数」——这些都属实，于是**全绿**。
    #     但 `--help` 的文案当时仍写着"保留参数（本脚本无慢用例）"，
    #     与 docstring「`--fast` 不是空壳参数」**直接矛盾**，而"这个参数是干什么的"
    #     对读 `--help` 的人而言**就是那句文案**。
    #     ⇒ 判据不能只查**代码行为**，还要查**对外承诺的文案**是否与行为一致：
    #     这是铁律 14 的又一形态（"行为是什么"被人写的字符串重新定义了一次）。
    #     下面用 AST 取 `add_argument("--fast", ...)` 的 `help=` 实参来对账。
    fast_used = len(re.findall(r"args\.fast", rev_src)) >= 3
    fast_skips_db = bool(re.search(r"if args\.fast:", main_rev))

    fast_help = ""
    for node in ast.walk(ast.parse(rev_src)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "add_argument" or not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and first.value == "--fast"):
            continue
        for kw in node.keywords:
            if kw.arg == "help" and isinstance(kw.value, ast.Constant):
                fast_help = str(kw.value.value)
    help_claims_shell = any(w in fast_help for w in ("保留参数", "无慢用例"))
    help_states_fact = "跳过" in fast_help
    emit(
        fast_used and fast_skips_db and split_expect_ok
        and bool(fast_help) and not help_claims_shell and help_states_fact,
        "[边界-9] `--fast` **不是空壳参数**（真被用于分支 + 分模式条数形态 + `--help` 文案与行为一致；"
        "本包修复的 C2/C9）",
        f"args.fast 使用 {len(re.findall(chr(97) + 'rgs.fast', rev_src))} 处；"
        f"skip 真库={'有' if fast_skips_db else '无'}；{split_expect_desc}；"
        f"help 文案={'与行为一致' if (fast_help and not help_claims_shell and help_states_fact) else '与行为不符/取不到'}",
    )

    # 32. 两种模式产物**文件名分流**（本包修复的缺陷 C3）
    split_out = (
        'OUT_FILE_FAST = EVIDENCE_DIR / "discipline-reverse-fast.log"' in rev_src
        and "out_file = OUT_FILE_FAST if args.fast else OUT_FILE" in rev_src
    )
    emit(
        split_out,
        "[边界-10] 两种模式产物**文件名分流**（fast 不覆盖收口期完整证据；本包修复的 C3）",
        f"文件名分流={'有' if split_out else '无'}",
    )

    # 33. `_OWN_WRITE_PATHS` 必须登记**两种模式**（写盘范围 ≠ 取证范围是结构证明）
    #
    #     ⚠️ 正则不能用非贪婪 `\((.*?)\)` —— 元组体里就有 `relative_to(REPO_ROOT)`，
    #     第一个 `)` 就把它截断了（实测只读到 `['OUT_FILE']`）。
    #     判据必须**按语法结构**取（AST），不是按"括号配对"猜。
    own_paths = _func_source(REVERSE, "_own_writes_are_disjoint")
    own_const = ""
    rev_tree = ast.parse(rev_src)
    for node in rev_tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "_OWN_WRITE_PATHS":
                value = node.value
                if isinstance(value, ast.Tuple):
                    own_const = "\n".join(ast.unparse(e) for e in value.elts)
    registered = re.findall(r"OUT_FILE_FAST|OUT_FILE\b", own_const)
    emit(
        len(registered) >= 2 and "_own_writes_are_disjoint" in rev_src,
        "[边界-11] `_OWN_WRITE_PATHS` 登记**两种模式**的产物（只登一个 = 另一模式成法外之地）",
        f"登记的常量={registered}（AST 取，不用括号正则）；"
        f"不相交判定函数={len(own_paths)} 字符",
    )

    # 34. 两个判据各自都有**自身判据**
    per_test = {
        "test_evidence_scope.py": TEST_D.is_file() and "check_evidence_scope.py" in _read(TEST_D),
        "test_shared_db_quiet.py": TEST_E.is_file() and "check_shared_db_quiet.py" in _read(TEST_E),
    }
    emit(
        all(per_test.values()) and REVERSE.is_file(),
        "[边界-12] 两个判据**各自**都有自身判据（判据纪律：声称有防线 = 待验证假设）",
        "; ".join(f"{n}={'有' if ok else '**无**'}" for n, ok in per_test.items()),
    )

    # 35. `--fast` 的**无真库**性质必须由代码结构保证（psycopg 是惰性导入）
    lazy_psycopg = True
    for node in ast.parse(rev_src).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in getattr(node, "names", [])]
            if any("psycopg" in n for n in names):
                lazy_psycopg = False
            if isinstance(node, ast.ImportFrom) and "psycopg" in (node.module or ""):
                lazy_psycopg = False
    psycopg_in_body = "import psycopg" in main_rev or "import psycopg" in _func_source(
        REVERSE, "_assert_quiet_or_die"
    )
    emit(
        lazy_psycopg and psycopg_in_body,
        "[边界-13] `psycopg` 是**惰性导入**（模块级不导入 ⇒ `--fast` 在无真库环境也能跑）",
        f"模块级无 psycopg={'有' if lazy_psycopg else '无'}；函数体内导入={'有' if psycopg_in_body else '无'}",
    )

    # 36. 说明书必须写全（审查记录不能是孤证 —— 文档声称要有判据，判据也要有说明书）
    doc_heads = re.findall(r"^## (\d+)\.", doc_src, re.M)
    need = [str(n) for n in range(0, 11)]
    missing_heads = [n for n in need if n not in doc_heads]
    emit(
        not missing_heads,
        "[边界-14] 说明书 `docs/m3/工程债-纪律判据化说明.md` 的 §0~§10 齐全",
        f"现有章节={sorted(doc_heads, key=int)}；缺={missing_heads}",
    )

    # ==================================================================
    head("--- 结论 ---")
    passed = sum(1 for c in checks if c)
    failed = len(checks) - passed
    complete = len(checks) >= EXPECTED_CHECKS
    lines.append("")
    lines.append(
        f"共 {len(checks)} 条 CHECK：PASS {passed} / FAIL {failed}"
        f"（完整={complete}，期望 ≥{EXPECTED_CHECKS}）"
    )
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


#: 本脚本应产生的 CHECK 条数下界（结构 14 + 逐行 8 + 边界 14 = 36）。
EXPECTED_CHECKS = 36


if __name__ == "__main__":
    raise SystemExit(main())
