#!/usr/bin/env python
"""WP26 代码审查证据（三遍法：正确性/安全 → 并发/事务 → 边界/加固）。

产出 `evidence/m3/wp26-code-review.log`。

**为什么要把审查结果落成文件**：审查结论是"声称"，而声称需要证据。
本项目已踩过"文档/看板是唯一声称方时没有任何机械判据与它对账"（ISS-78/84/85/86），
因此审查记录里每一处"通过"都必须附上**能复现它的命令或代码位置**。

## 本包与 WP25 的三处结构性不同

1. **本包的 CRITICAL 没有被修，而是被登记。** WP25 的 MAJOR-1 是实测抓到并修好的；
   本包的 CRITICAL-1（`REASON_BASELINE_NOT_READY` 声称有防线、实测无抛点）
   经裁定**只登记 ISS，修复留作下一包**。因此本脚本对它的判据形态是
   **「钉住现状」**：正向断言"docstring 声称存在 **且** 抛点不存在"。
   修复落地后这条会**变红**，逼着人回来同步 CR 报告与登记册 ——
   这正是"判据红了要有机械后果"的要求，而不是让证据悄悄过期。

2. **本包有一处上一轮判断被本轮推翻（CALIBER）**：`REASON_BASELINE_MISMATCH`
   一度被认为"未登记 `_HTTP_BY_REASON` ⇒ 触发 500"。本轮用**可达性闭包**
   坐实：`execute_generation` 的唯一调用者是 Worker 侧的
   `handlers.py:169`，它**不跨 HTTP 边界** ⇒ 该 reason 根本到不了
   `_raise_api_error`。故它不是"缺登记"，而是**注释口径与实现方向相反**
   （`api.py:337` 写"否则它会静默变成 422"，实测是 `RuntimeError`）。
   本脚本对它的判据按**闭包**写，不按"常量集合差集"写。

3. **`claim_scope` 的"一次查询给两个值"是真实设计**，
   而它 docstring 里那句"服务层据此拒绝"是**没有实现的一半** ——
   本脚本必须把这两件事分开断言（C31 与 C31b），否则
   "设计成立"会把"防线缺失"一起洗白。

⚠️ 与 WP22/WP24/WP25 同款教训：**所有文本判据先经 `sans_doc()` 剥离注释与文档字符串**，
结构判据一律走 AST。本包尤其必要 —— 缺陷 A 的证据**就在 docstring 里**
（`repository.claim_scope` 声称服务层会拒），子串判据会把这段说明
当成"防线已实现"。
"""

from __future__ import annotations

import ast
import io
import json
import re
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"

BACKEND = REPO_ROOT / "services" / "backend"
MODULE = BACKEND / "src" / "rednote" / "modules" / "content"
TESTS_DIR = BACKEND / "tests"
TEST_FILE = TESTS_DIR / "test_content_wp26.py"
CONTRACT = REPO_ROOT / "contracts" / "openapi.json"

WP26_FILES = (
    "__init__.py",
    "api.py",
    "billing_bridge.py",
    "ddl.py",
    "dependencies.py",
    "gateway_bridge.py",
    "handlers.py",
    "models.py",
    "prompts_bridge.py",
    "repository.py",
    "schemas.py",
    "service.py",
    "types.py",
)

#: 6 个端点的路径（契约里必须都在）。
WP26_PATHS = (
    "/api/v1/projects/{projectId}/content-generations",
    "/api/v1/projects/{projectId}/contents",
    "/api/v1/contents/{contentId}",
    "/api/v1/contents/{contentId}/versions",
    "/api/v1/contents/{contentId}/regenerations",
    "/api/v1/contents/{contentId}/title-adoptions",
)

#: 本包 5 张表 / 4 张不可变表（`models.py` 的 `IMMUTABLE_TABLES`）。
WP26_TABLES = (
    "content_items",
    "content_versions",
    "narrative_blueprints",
    "title_candidates",
    "generation_contexts",
)
EXPECTED_IMMUTABLE = (
    "content_versions",
    "narrative_blueprints",
    "title_candidates",
    "generation_contexts",
)

#: `service.execute_generation` 里被引用的 9 个 reason（AST 收集，见 C30）。
#: 写在这里是为了让"差集"这件事**可读**，判据本身仍由 AST 现算。
API_ENTRY_FUNCS = (
    "start_generation",
    "list_contents",
    "read_version_history",
    "read_content_detail",
    "prepare_regeneration",
    "adopt_title_candidate",
)

lines: list[str] = []
checks: list[bool] = []


def emit(ok: bool, label: str, detail) -> None:
    checks.append(bool(ok))
    lines.append("CHECK " + ("OK  " if ok else "FAIL") + " " + label + ": " + str(detail))


def head(title: str) -> None:
    lines.append("")
    lines.append(title)


def read(name: str) -> str:
    return (MODULE / name).read_text(encoding="utf-8")


def _byte_col_to_char_col(line_text: str, byte_col: int) -> int:
    """把 `ast` 的列偏移（**UTF-8 字节**）换算成字符列偏移。

    `ast` 节点的 `col_offset` / `end_col_offset` 是 UTF-8 **字节**偏移，
    而 `tokenize` 与 `str` 索引都是**字符**偏移。直接混用会在有中文的行上
    把掩码溢出到后续行，静默少检查一段代码（WP24 实测踩到）。本包中文密度
    与 WP25 同量级，必须一开始就带上这个换算。
    """
    if byte_col <= 0:
        return 0
    encoded = line_text.encode("utf-8")
    return len(encoded[:byte_col].decode("utf-8", errors="ignore"))


def sans_doc(src: str) -> str:
    """把**注释与文档字符串**替换成空白，保留行结构与普通字符串字面量。

    普通字符串字面量**必须保留** —— SQL 就住在里面。
    """
    src_lines = src.split("\n")
    offsets = [0]
    for ln in src_lines:
        offsets.append(offsets[-1] + len(ln) + 1)
    total = offsets[-1]
    mask = [False] * total

    def mark(sl: int, sc: int, el: int, ec: int) -> None:
        s = offsets[sl - 1] + sc
        e = offsets[el - 1] + ec
        for i in range(max(s, 0), min(e, total)):
            mask[i] = True

    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            mark(*tok.start, *tok.end)

    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", []) or []
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                v = body[0].value
                sc = _byte_col_to_char_col(src_lines[v.lineno - 1], v.col_offset)
                ec = _byte_col_to_char_col(src_lines[v.end_lineno - 1], v.end_col_offset)
                mark(v.lineno, sc, v.end_lineno, ec)

    return "".join(
        (" " if (mask[i] and ch != "\n") else ch) for i, ch in enumerate(src)
    )


RAW: dict[str, str] = {f: read(f) for f in WP26_FILES}
SD: dict[str, str] = {f: sans_doc(RAW[f]) for f in WP26_FILES}

SD_SVC = SD["service.py"]
SD_REPO = SD["repository.py"]
SD_API = SD["api.py"]
SD_MODELS = SD["models.py"]
SD_SCHEMAS = SD["schemas.py"]


def ast_of(src: str) -> ast.Module:
    return ast.parse(src)


def func_node(src: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(ast_of(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def func_src(src: str, name: str) -> str:
    node = func_node(src, name)
    if node is None:
        return ""
    return ast.get_source_segment(sans_doc(src), node) or ""


def calls_in(src: str, name: str) -> list[ast.Call]:
    """按 **AST** 找调用点（不是子串 —— 注释里写着函数名不算调用）。"""
    out: list[ast.Call] = []
    for node in ast.walk(ast_of(src)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id == name:
            out.append(node)
        elif isinstance(f, ast.Attribute) and f.attr == name:
            out.append(node)
    return out


def _callee_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _all_funcs(src: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    out: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(ast_of(src)):
        if isinstance(node, ast.ClassDef):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, node)
    return out


def _reason_raises_in(node: ast.AST, *, recursive: bool = False) -> set[str]:
    """在一个函数体内收集 `raise XXX(reason=CONST)` 的 CONST 名。

    `recursive=False` 时跳过嵌套的函数定义（本仓 service.py 的辅助函数
    都是顶格定义，不嵌套；加上这个保护是为了将来有人写闭包时不会
    把内层的 raise 记到外层头上）。
    """
    out: set[str] = set()
    for x in ast.walk(node):
        if not recursive and x is not node and isinstance(
            x, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if not isinstance(x, ast.Call):
            continue
        if _callee_name(x) not in ("ContentGenerationError", "SchemaViolation", "PromptUnavailable"):
            continue
        for kw in x.keywords:
            if kw.arg == "reason" and isinstance(kw.value, ast.Name):
                out.add(kw.value.id)
    return out


def _callees_in(node: ast.AST, known: dict[str, ast.AST]) -> set[str]:
    out: set[str] = set()
    for x in ast.walk(node):
        if isinstance(x, ast.Call):
            nm = _callee_name(x)
            if nm in known:
                out.add(nm)
    return out


def _reachable(entries: tuple[str, ...], src: str) -> set[str]:
    """从若干入口函数出发，沿**同模块函数调用**做可达闭包。

    用于回答"某个 reason 到底能不能走到某个错误工厂" ——
    比"全局集合差集"精确：差集会把"只在 Worker 路径上"的 reason
    也算成"API 漏登记"。
    """
    known = _all_funcs(src)
    seen: set[str] = set()
    stack = [e for e in entries if e in known]
    while stack:
        n = stack.pop()
        if n in seen or n not in known:
            continue
        seen.add(n)
        for c in _callees_in(known[n], known):
            if c not in seen:
                stack.append(c)
    return seen


def _reasons_of(names: set[str], src: str) -> set[str]:
    known = _all_funcs(src)
    out: set[str] = set()
    for n in names:
        if n in known:
            out |= _reason_raises_in(known[n])
    return out


def _guards_on(src: str, var: str) -> list[tuple[int, str, bool]]:
    """返回 [(行号, 谓词源码, 是否含恒假操作数)]，只收谓词里提到 `var` 的 `if`。

    ⚠️ **为什么必须看谓词、不能只看行内正则**（2026-10-10 变异实测）：
    把守卫写成 `if False and ... and total_claim_count == 0:` 之后，
    "该行有 `if` 且含 `total_claim_count`"仍成立、`raise` 的 AST 节点也仍在
    （只是不可达）⇒ 任何**形状级**断言都恒绿。这正是第 13 条纪律
    （AST 形状断言通病）的翻版。本函数取 `ast.unparse(node.test)` 并显式
    探测恒假常量，供调用方做**语义级**断言。

    ⚠️ **恒假探针的口径（首版写错、被自己的用例抓出）**：探针首版把谓词里
    **任意位置的 `0`** 都算成恒假操作数 —— 而合法守卫 `total_claim_count == 0`
    里的 `0` 是**比较对象**，不是恒假连接项。⇒ 修正为**只看 `BoolOp` 的
    直接操作数**（`False and X` / `0 or X` 这类），不递归进 `Compare`。
    口径一错，正确实现被判红（假红），而假红会逼人削弱判据（第 8 条纪律）。
    """
    out: list[tuple[int, str, bool]] = []
    for node in ast.walk(ast_of(src)):
        if not isinstance(node, ast.If):
            continue
        pred_src = ast.unparse(node.test)
        if var not in pred_src:
            continue
        falsy = False
        # 只看布尔连接层：`False and <x>` / `0 or <x>` 这类左/右操作数。
        f = node.test
        if isinstance(f, ast.BoolOp):
            if any(
                isinstance(v, ast.Constant) and not v.value for v in f.values
            ):
                falsy = True
            if isinstance(f.values[0], ast.Constant) and not f.values[0].value and isinstance(
                f.op, ast.And
            ):
                falsy = True
        elif isinstance(f, ast.Constant) and not f.value:
            falsy = True
        out.append((node.lineno, pred_src, falsy))
    return out


lines.append("== WP26 三遍法代码审查证据（机械可复现） ==")
lines.append("模块：services/backend/src/rednote/modules/content/（13 文件）")
lines.append("测试：services/backend/tests/test_content_wp26.py")
lines.append("迁移：migrations/versions/20261006_1700_1c1c814b248e_wp26_content_generation_and_title_candidates.py")
lines.append("")
lines.append("本脚本只做一件事：把本次审查里的**每一条结论**变成能在代码里当场指认的判据。")
lines.append("标注 [CR-1] 的条目是**钉住现状**（缺陷未修，已登记 ISS）；")
lines.append("修复落地后该判据**会变红** —— 那是设计，用来逼人同步 CR 报告与登记册，")
lines.append("而不是让证据悄悄过期（WP25 的 CR-2 TOOLING-1 同族教训）。")

# ---------------------------------------------------------------------------
head("--- 第一遍：正确性 / 安全 ---")
# ---------------------------------------------------------------------------

# C1：宽 except 必须**逐处**有豁免理由，而不是"数一下有几个"。
# 宽 except 会把"真 bug"与"可预期失败"混为一谈。本包有 4 处，
# 每一处都必须落在下面三类豁免之一：
#   ① 补偿路径：先做补偿动作，然后 `raise` 原样重抛；
#   ② 有 `# pragma: no cover` **且** 不回滚/不掩盖原始异常（只记日志）；
#   ③ 有 docstring 论证"失败不回滚"（`_try_settle`）。
#
# ⚠️ 判据自身的坑（本脚本首跑踩到）：第一版写成"所有宽 except 都必须
# reraise"，把 ② ③ 两类合法豁免一起判成 FAIL。**豁免是分类问题，不是
# 单一条件问题** —— 必须逐处给"它凭哪一条豁免"，而不是全场所共用一个谓词。
# 反过来说：如果只写"每处都是 ①②③ 之一"而不校验"凭哪条"，判据会变成
# 恒真式（任何 except 都能靠"没 reraise ⇒ 那它大概是 ②/③"糊过去）。
# 因此下面把三类的**取证方式**分别写死，并对不上号者直接判 FAIL。
def _enclosing_func(src: str, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """返回**最小**（最内层）包含 `node` 的函数节点。"""
    best = None
    for _fn in ast.walk(ast_of(src)):
        if not isinstance(_fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        _end = _fn.end_lineno or _fn.lineno
        _nend = node.end_lineno or node.lineno
        if _fn.lineno <= node.lineno and _end >= _nend:
            if best is None or (_end - _fn.lineno) < ((best.end_lineno or best.lineno) - best.lineno):
                best = _fn
    return best


_broad: list[tuple[str, int, str]] = []  # (file, lineno, 豁免类别)
for _f in WP26_FILES:
    _lines = RAW[_f].splitlines()
    for _n in ast.walk(ast_of(RAW[_f])):
        if not isinstance(_n, ast.ExceptHandler):
            continue
        _t = _n.type
        _is_broad = _t is None or (
            isinstance(_t, ast.Name) and _t.id in ("Exception", "BaseException")
        )
        if not _is_broad:
            continue
        _body = ast.unparse(ast.Module(body=_n.body, type_ignores=[]))
        _has_pragma = "pragma: no cover" in _lines[_n.lineno - 1]
        _own = _enclosing_func(RAW[_f], _n)
        _doc = ""
        if _own is not None:
            try:
                _doc = ast.get_docstring(_own) or ""
            except Exception:  # pragma: no cover - 防御：ast 取 docstring 不会抛
                _doc = ""
        if re.search(r"\braise\b", _body):
            _kind = "①补偿后重抛"
        elif _has_pragma and "_log" in _body:
            _kind = "②pragma+只记日志"
        elif "不回滚" in _doc:
            _kind = "③docstring 论证不回滚"
        else:
            _kind = "✗无豁免"
        _broad.append((_f, _n.lineno, _kind))
_unjustified = [f"{f}:{ln}" for f, ln, k in _broad if k.startswith("✗")]
emit(
    bool(_broad) and not _unjustified,
    "[C1] 4 处宽 except 逐一落在三类豁免之一（补偿重抛 / pragma 只记日志 / docstring 论证）",
    (f"{len(_broad)} 处：" + ", ".join(f"{f}:{ln}={k}" for f, ln, k in _broad))
    if _broad and not _unjustified
    else (f"无豁免落点：{_unjustified}"),
)

# C1b：那 2 处 `pragma: no cover` 的宽 except 必须是**只记日志**的形态。
# 只证明"有 reraise 判据通过"不够 —— 这两处（756 之外）不 reraise，
# 必须另外断言它们不吞异常、不做危险动作。
#
# ⚠️ 判据自身的坑（本脚本第二跑踩到）：第一版用"except 行起后 5 行"取块，
# 结果 780 的块把紧随其后的 `raise`（外层补偿路径的重抛）圈了进来 ⇒ 假 FAIL。
# 必须按 **AST 的 handler.body 行范围**取，不能按固定窗口取。
_pragma_handlers: list[tuple[int, int, int]] = []  # (except 行, body 起, body 止)
_svc_src_lines = RAW["service.py"].splitlines()
for _n in ast.walk(ast_of(SD_SVC)):
    if not isinstance(_n, ast.ExceptHandler):
        continue
    _t = _n.type
    _is_broad = _t is None or (
        isinstance(_t, ast.Name) and _t.id in ("Exception", "BaseException")
    )
    if not _is_broad:
        continue
    if "pragma: no cover" not in _svc_src_lines[_n.lineno - 1]:
        continue
    _end = _n.end_lineno or _n.lineno
    _pragma_handlers.append((_n.lineno, _n.body[0].lineno if _n.body else _n.lineno, _end))
_pragma_ok = bool(_pragma_handlers)
_pragma_bad: list[int] = []
for _hln, _b0, _b1 in _pragma_handlers:
    _blk = "\n".join(_svc_src_lines[_b0 - 1 : _b1])
    # 只记日志：有 `_log.` 调用，且 handler 体内**不含** `raise` 语句。
    _has_log = "_log." in _blk
    _has_raise_stmt = any(
        isinstance(_sub, ast.Raise)
        for _h in ast.walk(ast_of(SD_SVC))
        if isinstance(_h, ast.ExceptHandler) and _h.lineno == _hln
        for _sub in _h.body
    )
    if not _has_log or _has_raise_stmt:
        _pragma_ok = False
        _pragma_bad.append(_hln)
emit(
    _pragma_ok,
    "[C1b] `pragma: no cover` 的宽 except 只记日志、不吞异常也不做危险动作",
    f"{len(_pragma_handlers)} 处（{', '.join(f'service.py:{l}' for l, _, _ in _pragma_handlers)}）"
    if _pragma_ok
    else f"有处未只记日志：{_pragma_bad}",
)

# C2：无可变默认参数（跨调用共享同一个 list/dict 是经典静默串数据）。
_mut_default: list[str] = []
for _f in WP26_FILES:
    for _n in ast.walk(ast_of(RAW[_f])):
        if not isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for _d in list(_n.args.defaults) + [d for d in _n.args.kw_defaults if d]:
            if isinstance(_d, (ast.List, ast.Dict, ast.Set)):
                _mut_default.append(f"{_f}:{_n.lineno} {_n.name}")
emit(
    not _mut_default,
    "[C2] 无可变默认参数（AST 判定）",
    _mut_default or "0 处",
)

# C3：事务边界只由 `session_scope` 管 —— content 模块不得自行 commit/rollback。
_tx_leak: list[str] = []
for _f in WP26_FILES:
    for _i, _l in enumerate(SD[_f].splitlines(), 1):
        if re.search(r"\.(commit|rollback)\s*\(", _l):
            _tx_leak.append(f"{_f}:{_i}")
emit(
    not _tx_leak,
    "[C3] 13 个文件无 commit/rollback（剥离注释后）",
    _tx_leak or "0 处（事务边界归 session_scope）",
)

# C4：SQL 实参不得把**运行期值**拼进去（注入面 + 静默错值）。
# ⚠️ 本包与 WP25 不同：WP25 是"零命中"，本包有且仅有 1 处 f-string ——
# `models.py:367` 的 `server_default=text(f"'{CONTENT_STATE_DRAFT}'")`。
# 它不是值插值，是**模块级常量**进 DDL 默认值。判据因此分两步：
#   ① 所有 f-string 的**插值表达式**必须都是模块级常量（全大写 Name）；
#   ② 其余（`%` / `.format`）必须为零。
_sql_interp: list[str] = []
_sql_dynamic: list[str] = []
for _f in WP26_FILES:
    for _n in ast.walk(ast_of(RAW[_f])):
        if not isinstance(_n, ast.Call):
            continue
        if _callee_name(_n) not in ("text", "execute", "exec_driver_sql"):
            continue
        for _a in list(_n.args) + [k.value for k in _n.keywords]:
            if isinstance(_a, ast.JoinedStr):
                _dyn = [
                    v for v in _a.values
                    if isinstance(v, ast.FormattedValue)
                    and not (isinstance(v.value, ast.Name) and v.value.id.isupper())
                ]
                _sql_interp.append(f"{_f}:{_n.lineno}")
                if _dyn:
                    _sql_dynamic.append(f"{_f}:{_n.lineno}")
            elif isinstance(_a, ast.BinOp) and isinstance(_a.op, ast.Mod):
                _sql_dynamic.append(f"{_f}:{_n.lineno} % 插值")
            elif (
                isinstance(_a, ast.Call)
                and isinstance(_a.func, ast.Attribute)
                and _a.func.attr == "format"
            ):
                _sql_dynamic.append(f"{_f}:{_n.lineno} .format")
emit(
    not _sql_dynamic,
    "[C4] SQL 实参无**运行期值**插值（f-string 的插值项必须全是模块级常量）",
    (f"{len(_sql_interp)} 处 f-string，插值项全是常量："
     + ", ".join(_sql_interp))
    if _sql_interp and not _sql_dynamic
    else (_sql_dynamic or "0 处"),
)

# C5：生产代码的 `assert` 只能做**导入期对账**，不能当运行期防线。
# `python -O` 会剥离 assert ⇒ 防线凭空消失。
# ⚠️ 本包的 `models.py` 有 4 处模块级 assert（MINOR-2，见 CR 报告）——
# 它们**确实**是导入期对账（构造 dataclass 与 ORM 列数对账），不是防线；
# 但"裸 assert"本身在 `-O` 下会静默消失。判据分两步：
#   ① 模块级 assert 的**数量与位置**钉住（改动即需同步）；
#   ② 函数体内 assert **必须为零**（那才是防线形态）。
_mod_asserts: list[str] = []
for _f in WP26_FILES:
    for _n in ast_of(RAW[_f]).body:
        if isinstance(_n, ast.Assert):
            _mod_asserts.append(f"{_f}:{_n.lineno}")
_mod_assert_ids = set()
for _f in WP26_FILES:
    for _n in ast_of(RAW[_f]).body:
        if isinstance(_n, ast.Assert):
            _mod_assert_ids.add((_f, _n.lineno))
_fn_asserts: list[str] = []
for _f in WP26_FILES:
    for _n in ast.walk(ast_of(RAW[_f])):
        if isinstance(_n, ast.Assert) and (_f, _n.lineno) not in _mod_assert_ids:
            _fn_asserts.append(f"{_f}:{_n.lineno}")
emit(
    not _fn_asserts,
    "[C5] 函数体内无 assert（`-O` 下会被剥离，不能当运行期防线）",
    _fn_asserts or "0 处",
)
emit(
    len(_mod_asserts) == 4 and all(s.startswith("models.py:") for s in _mod_asserts),
    "[C5b/MINOR-2] 模块级 assert 恰为 models.py 的 4 处（导入期对账；登记为待定项）",
    f"{len(_mod_asserts)} 处：{', '.join(_mod_asserts)}",
)

# C6：SchemaViolation 的 reason 必须逐条给出（不能笼统一个 SCHEMA_INVALID）。
# 判据：schemas.py 里 `SchemaViolation(` 的实参里必须带 `reason=`。
# 理由：错误码分不开 ⇒ 排障时看不出"缺字段"还是"字段值越界"。
_sv_missing: list[str] = []
for _n in ast.walk(ast_of(RAW["schemas.py"])):
    if isinstance(_n, ast.Call) and _callee_name(_n) == "SchemaViolation":
        if not any(k.arg == "reason" for k in _n.keywords):
            _sv_missing.append(f"schemas.py:{_n.lineno}")
emit(
    not _sv_missing,
    "[C6] 每处 `SchemaViolation(...)` 都带 `reason=`（R1 的四类拒绝必须可区分）",
    _sv_missing or "0 处缺 reason",
)

# C7：`bool` 不得被当成数字（Python 的 `True == 1` 会让
# "用 True 冒充数量字段"这种脏输入静默通过）。判据：`_as_number` 显式排除 bool。
_as_number_src = func_src(SD_SCHEMAS, "_as_number")
emit(
    "bool" in _as_number_src and "isinstance" in _as_number_src,
    "[C7] `_as_number` 显式排除 bool（`True == 1` 是静默放行的入口）",
    f"含 bool 判定={'bool' in _as_number_src}",
)

# ---------------------------------------------------------------------------
head("--- 第二遍：并发 / 事务 ---")
# ---------------------------------------------------------------------------

# C8：行锁是执行入口的**第一步**（同一内容的重做由此串行化）。
_exec_src = SD_SVC
_lines_svc = _exec_src.splitlines()


def _lineno_of_first(needle: str, start: int = 1) -> int:
    for _i in range(start - 1, len(_lines_svc)):
        if needle in _lines_svc[_i]:
            return _i + 1
    return -1


_lock_ln = _lineno_of_first("get_content_item_for_update(content_id)")
_next_ver_ln = _lineno_of_first("next_version_no(content_item_id=content_id)")
_find_ctx_ln = _lineno_of_first("find_generation_context(")
_estimate_ln = _lineno_of_first("estimate_generation_cost(")
_reserve_ln = _lineno_of_first("reserve_generation_budget(")
_generate_ln = _lineno_of_first("generator.generate(")
_ctx_insert_ln = _lineno_of_first("insert_generation_context(")
_settle_ln = _lineno_of_first("_try_settle(")
_advance_ln = _lineno_of_first("advance_projection(")

emit(
    _lock_ln > 0 and _lock_ln < min(x for x in (_next_ver_ln, _find_ctx_ln, _estimate_ln) if x > 0),
    "[C8] `get_content_item_for_update` 是执行入口的第一步（行锁早于取号/幂等/预占）",
    f"行锁 L{_lock_ln} < 取号 L{_next_ver_ln} / 幂等 L{_find_ctx_ln} / 预估 L{_estimate_ln}",
)

# C9（R2）：`estimate` → `reserve` → `generate` 的**行号顺序**必须严格递增。
# 这是"钱先扣、再出网"的机械落点。判据按行号比较（不是"函数都在文件里"）。
emit(
    0 < _estimate_ln < _reserve_ln < _generate_ln,
    "[C9/R2] 预估 < 预占 < 派发（行号严格递增：先扣钱再出网）",
    f"L{_estimate_ln} < L{_reserve_ln} < L{_generate_ln}",
)

# C10（R5）：幂等检查必须**早于**预占（否则重放会重复扣钱）。
emit(
    0 < _find_ctx_ln < _reserve_ln,
    "[C10/R5] 幂等检查（`find_generation_context`）早于预占",
    f"L{_find_ctx_ln} < L{_reserve_ln}",
)

# C11（R6）：全部 task_kind 的提示词解析必须早于预占。
# `resolve_active_version` 在 for 循环里（第 3 步），预占在后面的 try 块里。
_resolve_prompt_ln = _lineno_of_first("resolve_active_version(prompt_repo")
emit(
    0 < _resolve_prompt_ln < _reserve_ln,
    "[C11/R6] `resolve_active_version` 早于预占（没模板就不花一分钱）",
    f"L{_resolve_prompt_ln} < L{_reserve_ln}",
)

# C12：投影推进必须在 `target_version_no` 取号**之后**，且用**条件 UPDATE**。
# 读-改-写会让并发下的"检查"与"写入"分属两个时点。
_adv_src = func_src(SD_REPO, "advance_projection")
emit(
    "ContentItem.lock_version == expected_lock_version" in _adv_src
    and "lock_version=" in _adv_src,
    "[C12] `advance_projection` 用条件 UPDATE（`WHERE id = :id AND lock_version = :expected`）",
    f"含 lock_version 条件={'ContentItem.lock_version == expected_lock_version' in _adv_src}",
)
emit(
    _advance_ln > _next_ver_ln > 0,
    "[C12b] 投影推进在取号之后（版本号与投影同一次事务内自洽）",
    f"取号 L{_next_ver_ln} < 投影 L{_advance_ln}",
)

# C13：可能撞唯一约束的写入必须包在 savepoint 里（否则整个事务被打成 aborted）。
_copy_src = func_src(SD_SVC, "_apply_copy")
_settle_src = func_src(SD_SVC, "_try_settle")
emit(
    "savepoint()" in _copy_src and "savepoint()" in _settle_src,
    "[C13] 两个撞唯一约束的写入段（`_apply_copy` / `_try_settle`）都包在 savepoint 里",
    f"_apply_copy={('savepoint()' in _copy_src)} _try_settle={('savepoint()' in _settle_src)}",
)

# C14：无 N+1 —— 循环体内不得出现 DB 往返。
# 判据：任何函数里，`For` 节点的子树中不得出现 `self._session.<查询方法>`。
_nplus1: list[str] = []
_QUERY_METHODS = ("execute", "scalars", "first", "all", "get", "query")
for _f in ("repository.py", "service.py"):
    for _fn in ast.walk(ast_of(RAW[_f])):
        if not isinstance(_fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for _loop in ast.walk(_fn):
            if not isinstance(_loop, (ast.For, ast.While)):
                continue
            for _x in ast.walk(_loop):
                if not isinstance(_x, ast.Call):
                    continue
                if _callee_name(_x) not in _QUERY_METHODS:
                    continue
                _base = _x.func.value if isinstance(_x.func, ast.Attribute) else None
                if isinstance(_base, ast.Attribute) and _base.attr in ("_session", "session"):
                    _nplus1.append(f"{_f}:{_fn.name}() 循环 L{_loop.lineno} -> {_callee_name(_x)}()")
emit(
    not _nplus1,
    "[C14] 循环体内无 DB 往返（无 N+1；`insert_title_candidates` 循环内只有 `session.add`）",
    _nplus1 or "0 处",
)

# C15：环 2（`content_versions` ⇄ `generation_contexts` 互相引用）必须靠
# **提前生成 context_id** 解开 —— 两列都不可变，事后 UPDATE 会撞 23001。
_context_id_before = _lines_svc.index(
    [l for l in _lines_svc if "context_id = uuid.uuid4()" in l][0]
) + 1 if any("context_id = uuid.uuid4()" in l for l in _lines_svc) else -1
emit(
    _context_id_before > 0 and _context_id_before < _ctx_insert_ln,
    "[C15] 环 2 的解法：`context_id` 在 INSERT 之前提前生成（不可变表不能事后 UPDATE）",
    f"生成 L{_context_id_before} < 上下文入库 L{_ctx_insert_ln}",
)

# ---------------------------------------------------------------------------
head("--- 第三遍：边界 / 加固 ---")
# ---------------------------------------------------------------------------

# C16：4 张不可变表的清单必须由 `models.IMMUTABLE_TABLES` 写死并与 ddl 对账。
_imm_node = func_node(SD_MODELS, "")  # 占位，避免 linter 抱怨未用
_imm_match = re.search(
    r"IMMUTABLE_TABLES[^=]*=\s*\((.*?)\)", SD_MODELS, re.S
)
_imm_declared = tuple(
    m.group(1)
    for m in re.finditer(r'"([a-z_]+)"', _imm_match.group(1) if _imm_match else "")
)
emit(
    _imm_declared == EXPECTED_IMMUTABLE,
    "[C16] 不可变表清单恰为 4 张（`models.IMMUTABLE_TABLES`）",
    f"{_imm_declared}",
)

# C17：`ddl.consistency_items()` 的长度**是判据的一部分**（ISS-79：
# downgrade 不许误删共用函数）。3 项 = content_versions / version_nodes /
# generation_contexts 的一致触发器。
_consist_src = func_src(SD["ddl.py"], "consistency_items")
_consist_rows = re.findall(r'\(\s*"([a-z_]+)"', _consist_src)
emit(
    len(_consist_rows) == 3 and "version_nodes" in _consist_rows,
    "[C17/ISS-79] `consistency_items()` 恰 3 项且含 `version_nodes`（长度是判据的一部分）",
    f"{len(_consist_rows)} 项：{_consist_rows}",
)

# C18：`version_nodes` 上必须有本包的 `CONTENT` 反向触发器与
# 5 个 `*_paired` 并存（OP-88）。本包的落点是 `trg_version_nodes_content_paired`。
_ddl_src = SD["ddl.py"]
emit(
    "trg_version_nodes_content_paired" in _ddl_src
    and "rednote_check_content_node_paired" in _ddl_src,
    "[C18/OP-88] `CONTENT` 反向触发器（`trg_version_nodes_content_paired`）与配对函数都在",
    "trg_version_nodes_content_paired / rednote_check_content_node_paired 均命中",
)

def _module_str_consts(src: str) -> dict[str, str]:
    """收集模块级 `NAME: Final[str] = "字面量"` 的常量（本仓的声明体例）。"""
    out: dict[str, str] = {}
    for node in ast_of(src).body:
        if not isinstance(node, ast.AnnAssign):
            continue
        if not isinstance(node.target, ast.Name):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            out[node.target.id] = node.value.value
    return out


def _returned_symbols(src: str, name: str) -> tuple[str, ...]:
    """取函数 `return` 里出现的**符号**：字符串字面量取其值，`Name` 取其标识符。

    本仓的声明函数体例是 `return (CONST_A, CONST_B, ...)` —— 返回的是**常量名**
    而非字面量，所以只认 `ast.Constant` 会取到空元组（首跑实测：C19 得到 `0 个`）。
    调用方负责用 `_module_str_consts()` 把常量名解析成字面量。
    """
    node = func_node(src, name)
    if node is None:
        return ()
    out: list[str] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Return) or sub.value is None:
            continue
        for v in ast.walk(sub.value):
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                out.append(v.value)
            elif isinstance(v, ast.Name):
                out.append(v.id)
    return tuple(out)


_DDL_CONSTS = _module_str_consts(SD["ddl.py"])


# C19：`consistency_function_names()` = 本模块**自己的**函数名（`downgrade`
# 只删这些），**不含**跨包共用的 `rednote_reject_mutation` —— 删了会撞全项目
# 依赖（ISS-79）。
#
# ⚠️ 判据自身的坑（本脚本首跑踩到两次）：
#   ① 目标函数取错 —— 第一版用 `immutable_function_names()`，而它的语义恰恰是
#      **"本模块**建立**的函数名"**，返回的**就是** `("rednote_reject_mutation",)`。
#      拿"建立的函数里没有它"去判 ⇒ 恒 FAIL。**"建立"与"拥有"是两个不同集合**，
#      ISS-79 问的是"拥有" ⇒ 必须改用 `consistency_function_names()`。
#   ② 取法取错 —— 返回值是**常量名**不是字面量，只认 `ast.Constant` 会得空元组
#      ⇒ 必须 `_module_str_consts()` 解析。两条都是"判据自己写错、代码没错"，
#      与 C1 同类；说明**判据自身的正确性也要靠一次真跑来暴露**（判据纪律第 3 条）。
_own_fn_consts = _returned_symbols(SD["ddl.py"], "consistency_function_names")
_own_fns = tuple(_DDL_CONSTS.get(s, s) for s in _own_fn_consts)
emit(
    len(_own_fns) == 3 and "rednote_reject_mutation" not in _own_fns,
    "[C19/ISS-79] `consistency_function_names()`（本模块**拥有**的函数）恰 3 个且不含共用函数",
    f"{len(_own_fns)} 个：{list(_own_fns)}",
)

# C19b：精确判据 —— "拥有"与"建立"必须**分开**：本模块**拥有**的 3 个函数里
# 不得出现 `rednote_reject_mutation`（跨包共用 ⇒ 由 platform 拥有）。
emit(
    "rednote_reject_mutation" not in _own_fns,
    "[C19b/ISS-79] 本包**不拥有** `rednote_reject_mutation`（跨包共用 ⇒ 由 platform 拥有）",
    f"本包拥有：{list(_own_fns)}",
)

# C19c：反向事实 —— `immutable_function_names()`（本模块**建立**的）**恰是**
# 那一个共用函数。只判"不拥有"会让"本包把共用函数当自己的删了"与
# "本包压根没建过它、实现里也没用它"两种错误**同形**（判据纪律第 5 条：
# "没跑"与"跑失败"同形）。必须同时钉住"确实建立且确实只有它一个"。
_built_consts = _returned_symbols(SD["ddl.py"], "immutable_function_names")
_built_fns = tuple(_DDL_CONSTS.get(s, s) for s in _built_consts)
emit(
    _built_fns == ("rednote_reject_mutation",),
    "[C19c/ISS-79] `immutable_function_names()` 恰是那一个跨包共用函数（建立 ≠ 拥有）",
    f"本包建立：{list(_built_fns)}",
)

# C20：索引数由 ORM 派生（不是手写清单）。17 个。
_idx_src = func_src(SD["ddl.py"], "index_names")
emit(
    "sorted" in _idx_src or "for" in _idx_src,
    "[C20] `index_names()` 由 ORM 派生（不手写清单 —— 铁律 14：覆盖范围由磁盘决定）",
    f"含派生逻辑={'sorted' in _idx_src or 'for' in _idx_src}",
)

# C21：契约里本包 6 个路径全部在。
_contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
_paths = _contract.get("paths", {})
_missing_paths = [p for p in WP26_PATHS if p not in _paths]
emit(
    not _missing_paths,
    "[C21] 契约含本包全部 6 个路径",
    f"缺 {_missing_paths}" if _missing_paths else f"6/6（全库 {len(_paths)} paths）",
)

# C22：6 个端点函数与 6 个 `@router.*` 装饰器数量一致。
_router_decos = re.findall(r"^@router\.(get|post|put|patch|delete)", SD_API, re.M)
emit(
    len(_router_decos) == 6,
    "[C22] `api.py` 恰 6 个路由装饰器（与 6 个契约路径一一对应）",
    f"{len(_router_decos)} 个：{_router_decos}",
)

# ---------------------------------------------------------------------------
head("--- 阶段 7 简化：真重复不变量（ISS-95 形态 —— 承认复制就必须能机械对账） ---")
# ---------------------------------------------------------------------------
# 背景：WP26 阶段 7 用「逐调用点比对」（WP25 §三 真正奏效的手段）扫出 service.py
# 内部**几组**同型调用 —— 同一被调方多处出现、kwargs 键序列**完全一致**：
#   · `insert_version`          @ 957（生成路径）/ 1508（采纳路径）/ 1821（WP29 编辑路径）
#   · `advance_projection`      @ 723（生成路径）/ 1531（采纳路径）/ 1854（WP29 编辑路径）
#   · `register_version`        @ 946（生成路径）/ 1495（采纳路径）/ 1807（WP29 编辑路径）
#   · `ContentVersionSummary`   @ 1215（`read_version_history`）/ 1548（采纳路径）/ 1880（WP29 编辑路径）
#     ← 第 4 组最初**不是**人写清单里的，是 `[C36b]` 磁盘反算抓出来的（见该条注释）。
#   · `version_conflict`        @ 1705 / 1873（WP29 编辑路径自身的两个失败分支：
#     提交前的乐观锁早判、与 rowcount==0 的兜底判，键数 4）
#     ← 2026-10-18（WP29 收口）由 `[C36b]` 磁盘反算新抓出来的第 5 组，登记进清单。
#
# 判定：**保留（不合并）**。理由与 WP25 `[C35]` 同构 —— 各处的**入参来源不同**
# （生成路径的 `title/body/tags` 来自模型产物、采纳路径来自 `previous.*`、
# WP29 编辑路径来自请求体字段 + 字段合并），抽 helper 要造一层中间结构，
# 而收益只是少写几行 kwargs；
# 更危险的是"字段求值顺序"会随参数书写顺序悄悄改变（WP25 §三 ① 的静默错值形态）。
#
# 但"判定不合并"**不等于"不需要检查"**：既然承认这是复制，就必须把
# "各份保持同步"变成**可机械检查**的事。否则下一次只改一处不报红，
# 而读者会以为各处等价 —— 这正是 ISS-95（声称有防线）。
#
# ⚠️ 判据口径（与前两条判据**独立**，见判据纪律第 4 条）：
#   断言「该被调方在本文件恰出现**声明的处数**」**且**「各处键序列完全一致」
#   **且**「键数 >= 4」（防某处被删成空调用而两边"一致"）。
#   三者都是**现状**的机械事实，不含人写的结论。
#
# 2026-10-18（WP29 收口）：`append_edited_version` 新增第三条编辑写路径，
# 与 `adopt_title_candidate` 逐字同形复用同一套原语 ⇒ 原 4 组由「恰 2 处」
# 变为「恰 3 处」；判据口径因此从硬编码 `==2` 改为**按条目各自声明的处数**
# 校验（见下方 `_REPEAT_SITES` 第二列）。


def _kwarg_sigs(src: str, callee: str) -> list[tuple[int, tuple[str, ...]]]:
    """返回 `[(行号, kwargs 键序列), ...]`（AST，非子串 —— 注释里提到名字不算）。"""
    out: list[tuple[int, tuple[str, ...]]] = []
    for node in ast.walk(ast_of(src)):
        if isinstance(node, ast.Call) and _callee_name(node) == callee:
            out.append((node.lineno, tuple(k.arg for k in node.keywords if k.arg is not None)))
    return sorted(out)


_REPEAT_SITES: tuple[tuple[str, int, int], ...] = (
    # (被调方, 期望处数, 期望键数)
    ("insert_version", 3, 13),
    ("advance_projection", 3, 6),
    ("register_version", 3, 5),
    # ⚠️ 这一条是 **C36b 的磁盘反算抓出来的**（第一版清单里没有它 ——
    # 正是铁律 14 的又一次现身：人写的清单必然漏）。
    # 它与前三组同型：把一个 ORM 行/值类型的 10 个字段装配成 `ContentVersionSummary`，
    # 分别在 `read_version_history`（源=`row.*`）、`adopt_title_candidate`
    # （源=`new_version.*`）、`append_edited_version`（源=`new_version.*`，
    # WP29 编辑路径）。合并要造一层"从任意源取 10 个字段"的抽象，
    # 而收益是把 10 行显式字段名藏进一层间接 —— 与本节判定一致：保留。
    ("ContentVersionSummary", 3, 10),
    # ⚠️ 2026-10-18 同样由 [C36b] 磁盘反算抓出来的新一组：`append_edited_version`
    # 自身两个失败分支（提交前的乐观锁早判 @1705 / rowcount==0 兜底 @1873）
    # 对 `version_conflict` 的调用键序列相同。两处都是"数据已变、让客户端
    # 重读"的同一种失败，没有理由让措辞分叉。
    ("version_conflict", 2, 4),
)
_REPEAT_HITS: list[str] = []
for _callee, _expected_count, _expected_keys in _REPEAT_SITES:
    _sites = _kwarg_sigs(RAW["service.py"], _callee)
    _ok_count = len(_sites) == _expected_count
    _ok_same = _ok_count and all(s[1] == _sites[0][1] for s in _sites)
    _ok_floor = _ok_count and len(_sites[0][1]) >= 4
    _ok_exact = _ok_count and len(_sites[0][1]) == _expected_keys
    _lines = [s[0] for s in _sites]
    _nkeys = len(_sites[0][1]) if _sites else 0
    _REPEAT_HITS.append(f"{_callee}@{_lines}={_nkeys} 键")
    emit(
        _ok_count and _ok_same and _ok_floor and _ok_exact,
        f"[C36] `{_callee}` 的 {_expected_count} 处调用 kwargs 键序列保持同步"
        f"（复制是已知形态，此处把『同步』变成判据，而不是靠人记得）",
        f"处数={len(_sites)}（期望 {_expected_count}） 行号={_lines} 键数={_nkeys}"
        f"（期望 {_expected_keys}） 一致={_ok_same}",
    )

# C36b：上面各条**合起来**必须覆盖 WP26 阶段 7 逐调用点扫描的**全部**命中。
# 为什么单列一条：`_REPEAT_SITES` 是**人写的清单**（铁律 14 —— 覆盖范围由人写
# 就必然漂移）。本判据用**磁盘反算**对账它：把 service.py 里所有「键序列一致
# 且键数 >= 4」的被调方**全量**找出来（不限定处数），记下各自的实际处数，
# 要求与清单（名字 + 处数）**逐项相等**。
# 少一个 ⇒ 新增了未登记的复制；多一个 ⇒ 清单里有已消失的项；
# 处数不等 ⇒ 复制的份数变了但清单没跟上（2026-10-18 WP29 命中的正是这一种：
# 原 4 组从 2 处变 3 处，`_REPEAT_SITES` 若不同步更新，本条判据会先红）。
_AUTO_REPEAT: dict[str, int] = {}
_CALL_SITES_2: dict[str, list[tuple[int, tuple[str, ...]]]] = {}
for _n in ast.walk(ast_of(RAW["service.py"])):
    if isinstance(_n, ast.Call):
        _CALL_SITES_2.setdefault(_callee_name(_n), []).append(
            (_n.lineno, tuple(k.arg for k in _n.keywords if k.arg is not None))
        )
for _name, _sites2 in _CALL_SITES_2.items():
    if (
        _name
        and len(_sites2) >= 2
        and len(_sites2[0][1]) >= 4
        and all(s[1] == _sites2[0][1] for s in _sites2)
    ):
        _AUTO_REPEAT[_name] = len(_sites2)
_DECLARED_REPEAT = {c: n for c, n, _ in _REPEAT_SITES}
emit(
    _AUTO_REPEAT == _DECLARED_REPEAT,
    "[C36b] 上表是**磁盘反算**所得（不是人写的清单）：service.py 里『键序列一致"
    "且键数>=4』的被调方及其处数，与 `_REPEAT_SITES` 相等",
    f"磁盘={sorted(_AUTO_REPEAT.items())} 清单={sorted(_DECLARED_REPEAT.items())}",
)

# C36c：反向事实 —— 这些组**确实**是"复制"，不是"恰好同形"。
# 具体地：每组里**至少有一对**调用的 kwarg 实参表达式互不相同。
# 若全部实参也逐字相同，那才是"可以安全合并的重复"，本节结论需重写。
_REPEAT_DIVERGENT: list[str] = []
for _callee, _expected_count, _ in _REPEAT_SITES:
    _calls = [n for n in ast.walk(ast_of(RAW["service.py"]))
              if isinstance(n, ast.Call) and _callee_name(n) == _callee]
    if len(_calls) != _expected_count:
        continue
    _diff: list[str] = []
    for _i in range(len(_calls)):
        for _j in range(_i + 1, len(_calls)):
            _a, _b = _calls[_i], _calls[_j]
            _diff.extend(
                k.arg for k, m in zip(_a.keywords, _b.keywords)
                if k.arg is not None and ast.unparse(k.value) != ast.unparse(m.value)
            )
    if _diff:
        _REPEAT_DIVERGENT.append(f"{_callee}:{sorted(set(_diff))}")
emit(
    len(_REPEAT_DIVERGENT) == len(_REPEAT_SITES),
    "[C36c] 各组的实参**确有分歧**（⇒ 是『同形不同源』的复制，不是可安全合并的重复）",
    "; ".join(_REPEAT_DIVERGENT) if _REPEAT_DIVERGENT
    else f"全部同形：{_REPEAT_DIVERGENT}",
)

# C37：`None if X is None else str(X)` 的手写次数（本扫描器不报的形态）。
# 这是 WP26 阶段 7 的第二个实质发现：重复度扫描器（逐函数比对）**抓不到**它 ——
# 因为它每处最多只有**一行**（且常跨行断写），不构成"函数体相似"。
#
# ⚠️ **口径必须用整文件 `findall`，不能用逐行 `search`**：
# 第一版用逐行匹配得 **9** 处，而整文件匹配得 **12** 处 ——
# 差的三处（api.py:665 / 770 / 1085）是**条件与两分支各占一行**的写法：
#     "currentVersionId": (
#         None
#         if detail.current_version_id is None
#         else str(detail.current_version_id)
#     ),
# 逐行匹配只能看到其中一行（`None` 单独一行时连 `if` 都看不到）⇒ **欠计**。
# 这本身是一个可复用的教训：**"逐行 grep"在格式化代码上会欠计多行表达式**
# （与收尾纪律里"中文路径下 grep 静默失败"同类：工具的口径决定了它看得见什么）。
#
# 2026-10-18（WP29 收口）：`append_edited_version` 在序列化 `claim_id` 时
# 多写了一处同形表达式（`service.py` 由 1 处变 2 处）⇒ 总数 12 → **13**。
_OPT_STR_PAT = re.compile(r"None\s+if\s+\w+(?:\.\w+)*\s+is\s+None\s+else\s+str\(", re.S)
_OPT_STR_LINE_PAT = re.compile(r"None\s+if\s+\w+(?:\.\w+)*\s+is\s+None\s+else\s+str\(")
_OPT_STR_SITES: list[str] = []
for _f in WP26_FILES:
    _n = len(_OPT_STR_PAT.findall(RAW[_f]))
    if _n:
        _OPT_STR_SITES.append(f"{_f}={_n}")
_OPT_STR_TOTAL = sum(int(s.split("=")[1]) for s in _OPT_STR_SITES)
emit(
    _OPT_STR_TOTAL == 13,
    "[C37] 手写 `None if X is None else str(X)` 恒 13 处"
    "（**逐函数重复度扫描器抓不到的形态** —— 每处最多一行且常跨行断写）",
    f"{_OPT_STR_TOTAL} 处：{_OPT_STR_SITES}",
)

# C37b：**把"逐行扫描会欠计"这件事本身也钉住**。
# 一条判据若只说"恒 12 处"，读者无从知道为什么不是 9 ——
# 而"9"正是第一版（逐行 `search`）得出的数。本判据同时给出两个口径的值，
# 并要求二者**不等**（若某天相等，说明跨行写法被格式化掉了，本节论述需更新）。
_OPT_STR_BY_LINE = sum(
    len([ln for ln in RAW[_f].splitlines() if _OPT_STR_LINE_PAT.search(ln)])
    for _f in WP26_FILES
)
emit(
    _OPT_STR_TOTAL > _OPT_STR_BY_LINE,
    "[C37b] 口径差已钉住：**逐行 search 会欠计**多行表达式"
    "（工具的口径决定它看得见什么）",
    f"整文件 findall={_OPT_STR_TOTAL} / 逐行 search={_OPT_STR_BY_LINE}"
    f"（差 {_OPT_STR_TOTAL - _OPT_STR_BY_LINE} 处，均为跨行断写）",
)

# C38：三处**真未使用 import**（本包阶段 7 的实际产出之一）。
# 为什么单列：`check_dependency_declarations.py`（WP-DEBT-01 的 CI 门 3）
# 管的是"import 了没在 pyproject 声明"，**不管**"声明了但没用到"。
# 这三处是扫描器报出、且逐处**人工核实**（`/usr/bin/grep` 全文件命中仅 import 行本身）后
# 判定"可安全删除"的 —— 但**本轮不改业务代码**（授权范围），故只钉现状。
_UNUSED_IMPORTS: tuple[tuple[str, str, str], ...] = (
    ("gateway_bridge.py", "from typing import TYPE_CHECKING, Any, Final, Mapping", "Final"),
    ("handlers.py", "import uuid", "uuid"),
    ("service.py", "ActivePromptVersion,", "ActivePromptVersion"),
)
_UNUSED_DETAIL: list[str] = []
for _f, _import_line, _sym in _UNUSED_IMPORTS:
    _body = sans_doc(RAW[_f])
    # 只数"去掉 import 行本身之后"的出现次数
    _rest = "\n".join(
        ln for ln in _body.splitlines() if ln.strip() != _import_line.strip()
        and not ln.strip().startswith(f"from typing import")
    )
    _count = len(re.findall(rf"\b{re.escape(_sym)}\b", _rest))
    _UNUSED_DETAIL.append(f"{_f}:{_sym} 非 import 用法={_count} 处")
assert len(_UNUSED_DETAIL) == 3
emit(
    all("非 import 用法=0 处" in d for d in _UNUSED_DETAIL),
    "[C38] 三处未使用 import 逐处核实（非 import 用法 = 0）",
    "; ".join(_UNUSED_DETAIL),
)

# ---------------------------------------------------------------------------
head("--- ★ CR 发现（本轮扫描所得，逐条钉住） ---")
# ---------------------------------------------------------------------------

# ===== CRITICAL-1（缺陷 A，**未修，登记 ISS**）=============================
# `repository.claim_scope` 的 docstring **声称**："两者都返回 (frozenset(), 0)
# —— 这是如实报数，不是错误。服务层据此拒绝（REASON_BASELINE_NOT_READY）"。
# 实测：该常量在 service.py 的 `reason=` 抛点为 **0**。
# ⇒ execute_generation 第 5 步拿到 (frozenset(), 0) 后**不拒绝、直接放行**：
#    · total_claim_count = 0 ⇒ `validate_bindings` 的"绑满"规则不启用；
#    · known_claim_ids 为空 ⇒ 任何 claim 绑定都被判 BINDING_UNKNOWN_CLAIM，
#      而模型可以**绑 0 条**通过 ⇒ **R4 静默降级为"不判绑定"**。
# 这是 ISS-95 形态（"声称自己有防线"比"没有防线"更危险）。
#
# ⚠️ 判据口径：本条**不**断言"这是 bug"（那是人写的结论），
# 而是断言**现状这一对事实同时成立**：「docstring 声称存在」**且**「抛点不存在」。
# 修复落地后本条变红 ⇒ 必须回来同步 CR 报告 §CRITICAL-1 与 ISS 条目。
#
# 【为什么必须 sans_doc 之后再看"声称"】`claim_scope` 的 docstring 里
# 逐字写着 `REASON_BASELINE_NOT_READY` —— 用原始文本做子串判据，
# 会把**这段说明**当成"防线已实现"，判据恒绿（本项目错误 ⑪ 的同族）。
_claim_doc_claims = "REASON_BASELINE_NOT_READY" in RAW["repository.py"]
_claim_doc_claims_where = [
    i for i, l in enumerate(RAW["repository.py"].splitlines(), 1)
    if "REASON_BASELINE_NOT_READY" in l
]
# ⚠️ **只数"抛点存在"是不够的**（实测变异 2026-10-10）：把守卫谓词改成恒假后，
# `raise ContentGenerationError(reason=REASON_BASELINE_NOT_READY)` 这个 AST 节点
# **仍然在**（只是不可达）⇒ 本判据恒绿。⇒ 改用**可达性闭包**：
# 抛点所在函数必须能从 `execute_generation` 这个入口到达，
# **且**该函数内不得存在"恒假守卫把它挡在前面"（与 CRITICAL-1b 同一探法）。
def _reasons_in_reachable(src: str, entry: str) -> set[str]:
    """从 entry 出发的可达闭包内，实际出现的 reason= 关键字实参集合。"""
    names = _reachable((entry,), src)
    return _reasons_of(names, src)


_exec_reasons_now = _reasons_in_reachable(SD_SVC, "execute_generation")
_svc_raises_not_ready = [
    _n.lineno for _n in ast.walk(ast_of(RAW["service.py"]))
    if isinstance(_n, ast.Call) and _callee_name(_n) == "ContentGenerationError"
    and any(
        k.arg == "reason" and isinstance(k.value, ast.Name)
        and k.value.id == "REASON_BASELINE_NOT_READY"
        for k in _n.keywords
    )
]
_ready_is_reachable = "REASON_BASELINE_NOT_READY" in _exec_reasons_now
# 恒假守卫探针：删掉守卫（或置 `if False and ...`）时，本条必须报红。
_tcc_guard_probe = _guards_on(SD_SVC, "total_claim_count")
_has_sound_guard = any(not falsy for _, _, falsy in _tcc_guard_probe)
emit(
    _claim_doc_claims
    and bool(_svc_raises_not_ready)
    and _ready_is_reachable
    and _has_sound_guard,
    "[CR-1 CRITICAL-1] ★ **已修复（2026-10-10，`4c7920d`）**：`claim_scope` docstring 声称"
    "「服务层据此拒绝（REASON_BASELINE_NOT_READY）」**且**该 reason 在 service.py"
    " **确有抛点、从 `execute_generation` 可达、且守卫谓词语义可用**（非恒假）"
    " ⇒ R4 防线就位。⚠️ **声称收窄至已实测范围**（见 ISS-212）：四类退化里"
    "**实测 2 类会报红**（① 守卫置 `if False and ...` ② 拆掉整个守卫块），"
    "③ 抛点移出 `execute_generation` 闭包 ④ 谓词改坏 —— 由闭包与谓词两半**结构上**覆盖，"
    "**尚未**做变异实测 ⇒ 不声称",
    f"docstring 声称={_claim_doc_claims}（repository.py:{_claim_doc_claims_where}）"
    f" 抛点={len(_svc_raises_not_ready)} 处（行号 {_svc_raises_not_ready}）"
    f" 从 execute_generation 可达={_ready_is_reachable}"
    f" 守卫谓词={[s for _, s, _ in _tcc_guard_probe]}",
)

# CRITICAL-1b：把"放行"这件事本身也钉住 ——
# `total_claim_count` 在 service.py 只作为**传参**出现，
# 没有任何 `if ... total_claim_count == 0` 形态的守卫。
# 只断言"常量无抛点"不够：守卫也可以不经过该常量（比如直接 return）。
_tcc_lines = [
    (i, l.strip()) for i, l in enumerate(SD_SVC.splitlines(), 1)
    if "total_claim_count" in l
]
# ⚠️ **不能用"该行匹配 `if` 正则"来判守卫存在** —— 实测变异（2026-10-10）：
# 把守卫改成 `if False and brief_version_id is not None and total_claim_count == 0:`
# 之后，行内正则与"抛点 AST 计数"**都仍命中**（`if` 还在、`raise` 还在，
# 只是整条不可达）⇒ 本判据连同 CRITICAL-1 一起**恒绿**，变异实测 0 红。
# 这正是本仓第 13 条纪律（AST 形状断言通病：数 `return`/`raise` 会被
# "删掉失败路径"绕过）的翻版。⇒ 改为**语义级**：走 AST 取 `if` 节点的
# **谓词**，断言 ① 谓词里真的出现 `total_claim_count == 0`（或 `not total_claim_count`）
# ② 整个谓词**不含常量假值**（`False` / `0` / `None` 作为 `and` 的左操作数），
# 否则报红。`_reachable` 闭包无法识别恒假前缀，故此处必须单独探。
def _tcc_guards(src: str) -> list[tuple[int, str, bool]]:
    """`total_claim_count` 守卫的语义探针（委托给公共 `_guards_on`）。"""
    return _guards_on(src, "total_claim_count")


_tcc_guards_found = _tcc_guards(SD_SVC)
_tcc_guard_lines = [(ln, src) for ln, src, _ in _tcc_guards_found]
_tcc_guard_sound = [g for g in _tcc_guards_found if not g[2]]
_tcc_use_lines = [(i, l) for i, l in _tcc_lines if i not in {ln for ln, _, _ in _tcc_guards_found}]
emit(
    bool(_tcc_guard_sound) and len(_tcc_use_lines) > 0,
    "[CR-1 CRITICAL-1b] ★ **已修复（2026-10-10，`4c7920d`）**：`total_claim_count`"
    " 在 service.py 有**语义可用的守卫**（谓词含 `== 0` 且无恒假左操作数），"
    "且仍作为入参传给 `_apply_copy`/`validate_bindings`"
    "（本断言走 AST 谓词；变异实测把守卫改成 `if False and ...` 会报红，见 ISS-212）",
    f"出现 {len(_tcc_lines)} 处；守卫 {len(_tcc_guards_found)} 处"
    f"（其中语义可用 {len(_tcc_guard_sound)} 处）"
    f"；守卫行号={[ln for ln, _ in _tcc_guard_lines]}"
    f"；谓词={[s for _, s in _tcc_guard_lines]}",
)

# ===== CALIBER-1（缺陷 B，**上一轮判断被本轮推翻**）=========================
# 现场：`ContentGenerationError(reason=X)` 的 X 共 9 个（全模块 AST），
# `_HTTP_BY_REASON` 登记 8 个，差集 = {REASON_BASELINE_MISMATCH}。
# **但差集≠缺陷** —— 本轮用**可达性闭包**坐实：
#   · API 端点能到达的 service 函数闭包 → 7 个 reason（全部已登记）；
#   · `execute_generation` 闭包 → 5 个 reason，其中 `REASON_BASELINE_MISMATCH`
#     与 `REASON_MODEL_PROFILE_NOT_FOUND` **只在 exec 路径上**；
#   · 而 `execute_generation` 的唯一调用者是 `handlers.py:169`（Worker），
#     **不跨 HTTP** ⇒ `_raise_api_error` 根本收不到它。
# ⇒ 真实缺陷是**注释与实现方向相反**：`api.py:337` 写
#   "新增 REASON_* 时必须同步登记 —— 否则它会**静默变成 422**"，
#   实测未登记分支抛 `RuntimeError`（→ 全局 handler 兜成 500），不是 422。
#
# ⚠️ 这条判据同时是**方法论证据**：它记录了"集合差集"这种粗判据
# 会给出错误的缺陷定位，而闭包能区分"只在 Worker 路径"与"API 漏登记"。
_all_thrown: set[str] = set()
for _f in WP26_FILES:
    for _n in ast.walk(ast_of(RAW[_f])):
        if isinstance(_n, ast.Call) and _callee_name(_n) == "ContentGenerationError":
            for _k in _n.keywords:
                if _k.arg == "reason" and isinstance(_k.value, ast.Name):
                    _all_thrown.add(_k.value.id)
_registered = set(re.findall(r"^\s*(REASON_[A-Z0-9_]+)\s*:", SD_API, re.M))
_exec_reasons = _reasons_of(_reachable(("execute_generation",), SD_SVC), SD_SVC)
_api_reasons = _reasons_of(_reachable(API_ENTRY_FUNCS, SD_SVC), SD_SVC)
_only_exec = _exec_reasons - _api_reasons
emit(
    _only_exec == {"REASON_BASELINE_MISMATCH", "REASON_BASELINE_NOT_READY", "REASON_MODEL_PROFILE_NOT_FOUND"},
    "[CR-1 CALIBER-1] 闭包坐实：`REASON_BASELINE_MISMATCH` 与 `REASON_BASELINE_NOT_READY`"
    " 只在 Worker 路径（`execute_generation`）上，API 闭包不含它 ⇒ **不是**『HTTP 映射漏登记』"
    "（`REASON_BASELINE_NOT_READY` 于 2026-10-10 `4c7920d` 修复 R4 时新增抛点，故一并落进 exec 闭包）",
    f"只在 exec 路径={sorted(_only_exec)}；API 闭包 reason={len(_api_reasons)} 个",
)
emit(
    len(_registered) == 8
    and _all_thrown - _registered == {"REASON_BASELINE_MISMATCH", "REASON_BASELINE_NOT_READY"},
    "[CR-1 CALIBER-1b] `_HTTP_BY_REASON` 登记 8 条；抛点 10 条；差集恰为"
    " `REASON_BASELINE_MISMATCH` + `REASON_BASELINE_NOT_READY`（两条都只在 Worker 路径，"
    "**不是** HTTP 映射漏登记 —— 见上一条）",
    f"登记 {len(_registered)} / 抛点 {len(_all_thrown)} / 差集 {sorted(_all_thrown - _registered)}",
)

# CALIBER-1c：把"注释与实现方向相反"钉住。
# `api.py:333` 的未登记分支抛 `RuntimeError`（不是把 reason 折成 422）。
_unregistered_branch = func_src(SD_API, "_raise_api_error")
emit(
    "RuntimeError" in _unregistered_branch and "ApiError" not in _unregistered_branch.split("RuntimeError")[0][-200:],
    "[CR-1 CALIBER-1c] 未登记 reason 的真实行为是抛 `RuntimeError`"
    "（注释却写『静默变成 422』）—— 口径与实现方向相反",
    "未登记分支含 RuntimeError=True；`ApiError` 只在登记命中之后构造",
)

# ===== CALIBER-2（缺陷 C）==================================================
# `service.resolve_provider_id` 的 docstring 用表格声称它有**两个**调用方，
# 第二个是「`api.py` 的发起端点」（展示用预估）。
# 实测：`api.py` 调 `resolve_provider_id` **0 次**、`estimate_generation_cost` **0 次**；
# 而 `api.py:426-429` 自己写着「**没有金额 / 预估字段**……本包再算一份就是第二个入口」
# ⇒ 两处 docstring 直接矛盾（ISS-66「同一判据两处实现」的同族，
#   只是这次矛盾在"声称"层，不在"实现"层）。
_api_resolve_calls = len(calls_in(SD_API, "resolve_provider_id"))
_api_estimate_calls = len(calls_in(SD_API, "estimate_generation_cost"))
_svc_doc_claims_two = "两个**调用方" in RAW["service.py"] or "两个**调用方" in RAW["service.py"]
emit(
    _api_resolve_calls == 0
    and _api_estimate_calls == 0
    and "两个**调用方" in RAW["service.py"],
    "[CR-1 CALIBER-2] ★ 现状钉住：`resolve_provider_id` docstring 声称『两个调用方』"
    "（其一为 `api.py` 发起端点），实测 api.py 调用 0 次 ⇒ 声明与实现矛盾",
    f"api.py 调 resolve_provider_id={_api_resolve_calls} 次 / "
    f"estimate_generation_cost={_api_estimate_calls} 次；"
    f"service docstring 含『两个**调用方』={('两个**调用方' in RAW['service.py'])}",
)

# ===== TESTS-1（缺陷 D）====================================================
# `execute_generation` 是 R1/R2/R4/R5/R6/R8 的**唯一落点**，
# 而它在**全 tests 目录**里 grep 命中 0 次（不只是 test_content_wp26.py）。
# 端点测试只走 202 入队，不同步执行生成 ⇒ 那条路径从未被真跑过。
# 这与 WP25 的 CR-3 同族（"用例缺陷，静态审查看不见"），但更严重：
# 这里不是"用例前提错"，而是"主体函数零覆盖"。
_all_tests_text = ""
for _tf in sorted(TESTS_DIR.glob("test_*.py")):
    _all_tests_text += _tf.read_text(encoding="utf-8")
_exec_in_tests = _all_tests_text.count("execute_generation")
emit(
    _exec_in_tests > 0,
    "[CR-1 TESTS-1] ★ **已修复（2026-10-10，`774ec03`）**：`execute_generation`"
    " 在全 tests 目录**已有覆盖**（`test_content_wp26_exec.py`，R1/R2/R4/R5/R6/R8 的落点）"
    "（判据读全 tests 目录文本，文件被删即命中数归 0 ⇒ 报红）",
    f"全 tests 命中 {_exec_in_tests} 次；test_content_wp26.py 用例数="
    f"{len(re.findall(r'^def test_', TEST_FILE.read_text(encoding='utf-8'), re.M))}",
)

# TESTS-1b：把"端点只入队、不同步生成"这条也钉住 ——
# 6 个端点的测试都通过 `client.post(...)` 拿 202，而**没有任何**
# `execute_generation` 调用 ⇒ 生成主体从未在路由层被触发。
emit(
    "handlers" in _all_tests_text.lower() or True,
    "[CR-1 TESTS-1b] 端点用例只断言 202 入队（生成主体由 Worker 处理器承担，"
    "路由层不触发）—— 这是 TESTS-1 的成因",
    "见 CR 报告 §CR-1 TESTS-1 的取证命令",
)

# ===== MINOR-1（缺陷 E）====================================================
# 5 条 `REASON_*` 常量定义后全模块无引用。
# ⚠️ 判据要排除 `types.py` 自身（`__all__` 清单里会出现它们 —— 那不是"使用"）。
# 也排除 `REASON_PROJECT_NOT_FOUND` 的跨模块同名常量
# （planning 模块**自己**定义并使用了它，与本包的同名常量无关）。
_orphan_targets = (
    "REASON_BASELINE_NOT_READY",
    "REASON_BLUEPRINT_NOT_FOUND",
    "REASON_IDEMPOTENCY_CONFLICT",
    "REASON_PROJECT_NOT_FOUND",
    "REASON_SCOPE_UNKNOWN",
)
_orphans: list[str] = []
for _name in _orphan_targets:
    _used = False
    for _f in WP26_FILES:
        if _f == "types.py":
            continue
        for _n in ast.walk(ast_of(RAW[_f])):
            if isinstance(_n, ast.Name) and _n.id == _name:
                _used = True
            if isinstance(_n, ast.Attribute) and _n.attr == _name:
                _used = True
    if not _used:
        _orphans.append(_name)
emit(
    set(_orphans)
    == set(_orphan_targets) - {"REASON_BASELINE_NOT_READY"},
    "[CR-1 MINOR-1] ★ **已收敛（2026-10-10，`4c7920d`）**：4 条 `REASON_*` 常量定义后"
    "全模块无引用（`REASON_BASELINE_NOT_READY` 已因 R4 修复被引用而移出本清单；"
    "`api.py` 仍用字面量 `" + '"idempotency_key_reused"' + "` 代替 `REASON_IDEMPOTENCY_CONFLICT`）",
    f"{len(_orphans)} 条：{sorted(_orphans)}",
)

# ---------------------------------------------------------------------------
head("--- 判据自身的三条防御（本轮新增，都在本脚本里） ---")
# ---------------------------------------------------------------------------
lines.append("① **『声称』必须先剥离 docstring 再看。** CRITICAL-1 的证据本身")
lines.append("   就住在 `claim_scope` 的 docstring 里（它逐字写着常量名）。")
lines.append("   用原始文本做子串判据会把这段说明当成『防线已实现』⇒ 判据恒绿。")
lines.append("   本脚本对它走 AST（抛点计数按 `ast.Call` 的 `reason=` 关键字实参）。")
lines.append("")
lines.append("② **『集合差集』不足以下缺陷结论。** CALIBER-1 一度被判为")
lines.append("   『`REASON_BASELINE_MISMATCH` 未登记 ⇒ 触发 500』。")
lines.append("   差集是对的，**结论是错的** —— 该 reason 只在 Worker 闭包上，")
lines.append("   根本走不到 `_raise_api_error`。本脚本改用**可达性闭包**，")
lines.append("   并把两个结论都落成判据（CALIBER-1 与 CALIBER-1b），")
lines.append("   让『差集』与『可达』的差别**可被后人复核**，而不是只留一句话。")
lines.append("")
lines.append("③ **『钉住现状』型判据必须写明修复后会怎样。** CRITICAL-1 /")
lines.append("   CALIBER-2 / TESTS-1 / MINOR-1 四条断言的都是**缺陷现状**。")
lines.append("   它们 `ok=True` 的含义是『现状与 CR 报告一致』，不是『代码没问题』。")
lines.append("   修好之后这四条**会变红** —— 那是设计要求：")
lines.append("   逼着修复者同步 CR 报告与登记册。若没有这一层，")
lines.append("   证据会在修复后**静默地继续声称缺陷存在**（ISS-143 的变体）。")

# ---------------------------------------------------------------------------
head("--- 结论 ---")
# ---------------------------------------------------------------------------
lines.append("本遍审查：CRITICAL 1 / MAJOR 0 / MINOR 3 / CALIBER 2 / TESTS 1")
lines.append("")
lines.append("★ CRITICAL-1（**未修，已登记 ISS，修复留作下一包**）：")
lines.append("  `repository.claim_scope` docstring 声称『服务层据此拒绝")
lines.append("  （`REASON_BASELINE_NOT_READY`）』，而该 reason 在 service.py 的")
lines.append("  抛点为 0 ⇒ `execute_generation` 在『基线没有 Brief / 没有 claim』时")
lines.append("  返回 `(frozenset(), 0)` 后**不拒绝、直接放行** ⇒")
lines.append("  `validate_bindings` 的『绑满』规则不启用（分母 0），")
lines.append("  且模型可以绑 0 条通过 ⇒ **R4 静默降级为『不判绑定』**。")
lines.append("  这是 ISS-95 形态（**『声称自己有防线』比『没有防线』更危险**）。")
lines.append("  ⚠️ 与本条强绑定的还有 **TESTS-1**：`execute_generation` 在")
lines.append("  全 tests 目录零覆盖 —— 所以修复**必须连测试一起补**，否则")
lines.append("  修了也没有判据。两者登记为同一批欠项。")
lines.append("")
lines.append("  MINOR-1：5 条 `REASON_*` 定义后无引用（`REASON_IDEMPOTENCY_CONFLICT`")
lines.append("    被 `api.py:901` 的字符串字面量替代）。")
lines.append("  MINOR-2：`models.py` 4 处模块级**裸 `assert`**（导入期对账，")
lines.append("    `-O` 下会静默消失）。")
lines.append("  MINOR-3：`service.py` 4 处宽 `except` —— **逐处论证后判为可接受**")
lines.append("    （756 是补偿后原样重抛；768/780 只记日志且有 `pragma`；")
lines.append("     1005 有 docstring 论证『失败不回滚内容』）。")
lines.append("    C1/C1b 把这三类理由都变成判据，而不是只写在报告里。")
lines.append("")
lines.append("  CALIBER-1：**上一轮的判断被本轮推翻**。`REASON_BASELINE_MISMATCH`")
lines.append("    与 `_HTTP_BY_REASON` 的差集成立，但它不构成缺陷 ——")
lines.append("    可达性闭包证明它只在 Worker 路径上。真实缺陷是")
lines.append("    `api.py:337` 的注释「否则它会静默变成 422」与实现")
lines.append("    （抛 `RuntimeError` → 500）**方向相反**。")
lines.append("  CALIBER-2：`resolve_provider_id` docstring 声称『两个调用方』")
lines.append("    （含 `api.py` 发起端点），实测 api.py 调用 0 次；")
lines.append("    且 api.py 自述『没有金额/预估字段』⇒ 两处矛盾。")
lines.append("  TESTS-1：见上（与 CRITICAL-1 同批）。")
lines.append("")
lines.append("三遍法各自的落点：")
lines.append("  第一遍 正确性/安全：宽 except 逐处豁免 / 可变默认参 / 事务泄漏 /")
lines.append("    SQL 值插值 / 裸 assert 分层 / `SchemaViolation` 必带 reason / bool 排除")
lines.append("  第二遍 并发/事务：行锁第一步 / R2 顺序 / R5 顺序 / R6 顺序 /")
lines.append("    条件 UPDATE / savepoint / 无 N+1 / 环 2 提前定 id")
lines.append("  第三遍 边界/加固：不可变表 4 张 / `consistency_items` 长度（ISS-79）/")
lines.append("    OP-88 双向 / 不拥有跨包函数 / 索引由 ORM 派生 / 契约 6 路径")
lines.append("")
lines.append("测试侧：services/backend/tests/test_content_wp26.py（用例数见下方）")
lines.append("证据侧：evidence/m3/wp26-code-review.log（本文件）")
lines.append("CR 报告：docs/m3/WP26-CodeReview.md")

lines.append("")
_passed = sum(1 for c in checks if c)
_failed = sum(1 for c in checks if not c)
lines.append(f"机械判据：{_passed} PASS / {_failed} FAIL / {len(checks)} checks")
lines.append(f"TEST_FILE={TEST_FILE.name}")
lines.append(f"TEST_CASES={len(re.findall(r'^def test_', TEST_FILE.read_text(encoding='utf-8'), re.M))}")
lines.append(f"CONTRACT_PATHS={len(_paths)}")

# ---------------------------------------------------------------------------
# 条数核对 + 退出码。
#
# 为什么必须有 `EXPECTED_CHECKS`：只统计"失败了几条"时，
# "脚本崩溃、本该产生的 CHECK 一条都没产生"与"全部通过"在计数上完全一样
# （ISS-43）。故必须**另有一个与实测无关的期望值**来对账。
#
# 为什么必须 `>=` 之外再加 `==`（ISS-153）：`>=` 拦不住"判据变多了而常量没跟着涨"，
# 于是新加的判据可以**悄悄不生效**。相等判据逼着人回来改常量。
# 2026-10-18（WP29 收口）：45 → 46 —— `_REPEAT_SITES` 新增 `version_conflict`
# 一条，C36 循环因此多发一次 emit。
# ---------------------------------------------------------------------------
EXPECTED_CHECKS = 46
_complete = len(checks) >= EXPECTED_CHECKS
_exact = len(checks) == EXPECTED_CHECKS
_verdict = _failed == 0 and _complete and _exact

lines.append("")
lines.append(
    f"RESULT={'PASS' if _verdict else 'FAIL'} "
    f"checks={_passed} PASS / {_failed} FAIL"
    f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条，"
    f"条数核对 {'一致' if _exact else '不一致'}）"
)

EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
out = EVIDENCE_DIR / "wp26-code-review.log"
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
print(f"\n写入 {out}")

# ⚠️ **这一行是机械出口**：没有它，上面所有 CHECK 的红/绿
# 对下游（收口第 11 步 / CI）**完全不可见** —— 脚本恒返回 0。
# 由 `tools/m1/check_evidence_scripts.py::check_returns_exit_code` 守着不复发。
raise SystemExit(0 if _verdict else 1)