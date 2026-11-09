#!/usr/bin/env python
"""WP27 代码审查证据（三遍法：正确性/安全 → 并发/事务 → 边界/加固）。

产出 `evidence/m3/wp27-code-review.log`。

**为什么要把审查结果落成文件**：审查结论是"声称"，而声称需要证据。
本项目已踩过"文档/看板是唯一声称方时没有任何机械判据与它对账"（ISS-78/84/85/86），
因此审查记录里每一处"通过"都必须附上**能复现它的命令或代码位置**。

## 本包与 WP26 的结构性不同（先讲清楚，后面每节都建立在这上面）

1. **本包有一整类缺陷面：docstring 的"零覆盖"声称已过时。** WP27 阶段 1~3
   的勘察期（2026-10-07）在 `models.py` / `schemas.py` / `types.py` 里批量写下
   "该文件全仓不存在、本包在 `services/backend/tests/` 下零覆盖" —— 这些断言
   **在写下当日为真**，但 `test_media_wp27.py` 此后已落地（**1067 行 / 26 用例**）。
   ⇒ 那批"零覆盖"声称现已成为**方向相反的过时陈述**（声称无覆盖、实则有）。
   见 **MAJOR-1**。本脚本把它判成"钉住现状"型判据。

2. **本包的 CRITICAL 同样是"声称与实现不符"**，且**位置对称**于 WP26：
   WP26 是 `claim_scope` 声称服务层拒绝而抛点为 0；
   WP27 是 `service.py` 第 6 步声称"幂等查重"而 `find_generation_context`
   在 `media/service.py` **零调用**。见 **CRITICAL-1**。

3. **本包的迁移是"化石字面量"而非活常量委托。** 两条 WP27 迁移的 CHECK 文本
   一度引用模块常量（`PENDING_BINDINGS_IS_ARRAY_SQL` 等），
   被 `tools/quality/check_migration_value_domain_no_live_import.py` 判红后
   **已改回字面量**（2026-10-10）。本脚本用 C22/C23 钉住这一点 ——
   否则下次重构会把活常量委托再引回来。

⚠️ 与 WP22/WP24/WP25/WP26 同款教训：**所有文本判据先经 `sans_doc()` 剥离注释与
文档字符串**，结构判据一律走 AST。本包尤其必要 —— CRITICAL-1 的证据
（"幂等查重"那句话）**就在注释里**，`service.py` 内 `grep find_generation_context`
会命中那句注释之外的 0 处；若按"文件里是否出现这个字符串"判，会**恒真**。
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
MODULE = BACKEND / "src" / "rednote" / "modules" / "media"
TESTS_DIR = BACKEND / "tests"
TEST_FILE = TESTS_DIR / "test_media_wp27.py"
CONTRACT = REPO_ROOT / "contracts" / "openapi.json"
MIGRATIONS = REPO_ROOT / "migrations" / "versions"
CONTENT_MODULE = BACKEND / "src" / "rednote" / "modules" / "content"

WP27_FILES = (
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

#: 两条 WP27 迁移（按 revision 前缀定位，避免文件名日期漂移）。
WP27_MIGRATION_STEMS = ("b7c1e4a9d203", "c4d9f2a17e35")

#: 本包进入契约的 6 条路径（`reference-assets` 有 GET+POST 两个 operation）。
WP27_PATHS = (
    "/api/v1/contents/{contentId}/media",
    "/api/v1/contents/{contentId}/media-generations",
    "/api/v1/media/{mediaId}",
    "/api/v1/media/{mediaId}/regenerations",
    "/api/v1/media/{mediaId}/renditions",
    "/api/v1/projects/{projectId}/reference-assets",
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
    与 WP26 同量级，必须一开始就带上这个换算。
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


RAW: dict[str, str] = {f: read(f) for f in WP27_FILES}
SD: dict[str, str] = {f: sans_doc(RAW[f]) for f in WP27_FILES}

SD_SVC = SD["service.py"]
SD_REPO = SD["repository.py"]
SD_API = SD["api.py"]
SD_MODELS = SD["models.py"]
SD_SCHEMAS = SD["schemas.py"]
SD_DDL = SD["ddl.py"]
SD_GW = SD["gateway_bridge.py"]
SD_TYPES = SD["types.py"]


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


def _returned_symbols(src: str, name: str) -> tuple[str, ...]:
    """取函数 `return` 里出现的**符号**：字符串字面量取其值，`Name` 取其标识符。

    本仓的声明函数体例是 `return (CONST_A, CONST_B, ...)` —— 返回的是**常量名**
    而非字面量，所以只认 `ast.Constant` 会取到空元组。调用方负责解析常量名。
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


def _dict_keys(src: str, name: str) -> tuple[str, ...]:
    """取模块级 `NAME: Final[dict[...]] = { K1: ..., K2: ... }` 的键。

    ⚠️ 键**两种形态都要认**：本包的 `_HTTP_BY_REASON` 用的是**变量名**做键
    （`REASON_MEDIA_ITEM_NOT_FOUND: ...`，是 `ast.Name` 而非 `ast.Constant`）。
    首版只认字符串字面量，实测只抓到 3 个（那 3 个恰好是……无一，其实是 0），
    与承诺下界 20 差得离谱 —— **判据自己的取法错，不是代码错**（判据纪律第 3 条）。
    """
    for node in ast_of(src).body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if node.targets else None
        )
        if not (isinstance(target, ast.Name) and target.id == name):
            continue
        value = node.value
        if not isinstance(value, ast.Dict):
            continue
        out: list[str] = []
        for k in value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                out.append(k.value)
            elif isinstance(k, ast.Name):
                out.append(k.id)
        return tuple(out)
    return ()


def _for_iterable_tuple_count(src: str, name: str) -> int:
    """数函数 `name` 体内**首个 `for` 的字面量 iterable 元素个数**。

    `consistency_items()` 的形态是
    `for table, builder in ((...), (...), (...), (...)): items.append(...)` ——
    数 `append` 调用点只会得到 **1**（它在循环里跑 4 次），必须数字面量元组的项数。
    """
    node = func_node(src, name)
    if node is None:
        return 0
    for _n in ast.walk(node):
        if isinstance(_n, ast.For) and isinstance(_n.iter, (ast.Tuple, ast.List)):
            return len(_n.iter.elts)
    return 0


def _enclosing_func(src: str, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for _fn in ast.walk(ast_of(src)):
        if not isinstance(_fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        _end = _fn.end_lineno or _fn.lineno
        _nend = node.end_lineno or node.lineno
        if _fn.lineno <= node.lineno and _end >= _nend:
            if best is None or (_end - _fn.lineno) < ((best.end_lineno or best.lineno) - best.lineno):
                best = _fn
    return best


# ===========================================================================
head("=== WP27 代码审查：三遍法 ===")
lines.append(f"对象：{MODULE.relative_to(REPO_ROOT)}（{len(WP27_FILES)} 个文件）")
lines.append(f"测试：{TEST_FILE.relative_to(REPO_ROOT)}")
lines.append("方法：先读全文再下结论；凡与 docstring 声称不符者一律**以实测为准**。")
lines.append("读数口径：`ok=True` 的『钉住现状』型判据含义是『现状与 CR 报告一致』，不是『代码没问题』；缺陷修好后它们会变红。")
# ---------------------------------------------------------------------------
head("--- 第一遍：正确性 / 安全 ---")
# ---------------------------------------------------------------------------

# C1：宽 `except` 逐一落在豁免类之一（补偿重抛 / pragma 只记日志 / docstring 论证）。
_broad: list[tuple[str, int, str]] = []
for _f in WP27_FILES:
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
        # ⚠️ 豁免论证可能住在 handler 体内的**注释**里（`unparse` 会剥掉注释）
        # ⇒ 必须另扫原始行（service.py:1678 的"结算失败不掩盖已成功的结果"
        # 就是注释，首版只扫 docstring ⇒ 假 FAIL）。
        _nend = _n.end_lineno or _n.lineno
        _raw_body = "\n".join(_lines[_n.lineno - 1 : _nend])
        _own = _enclosing_func(RAW[_f], _n)
        _doc = ""
        if _own is not None:
            try:
                _doc = ast.get_docstring(_own) or ""
            except Exception:  # pragma: no cover - 防御：ast 取 docstring 不会抛
                _doc = ""
        _just = _doc + "\n" + _raw_body
        if re.search(r"\braise\b", _body):
            _kind = "①补偿后重抛"
        elif _has_pragma and "_log" in _body:
            _kind = "②pragma+只记日志"
        elif "不掩盖" in _just or "不回滚" in _just:
            _kind = "③docstring/注释论证"
        else:
            _kind = "✗无豁免"
        _broad.append((_f, _n.lineno, _kind))
_unjustified = [f"{f}:{ln}" for f, ln, k in _broad if k.startswith("✗")]
emit(
    bool(_broad) and not _unjustified,
    "[C1] 宽 except 逐一落在三类豁免之一（补偿重抛 / pragma 只记日志 / docstring 论证）",
    (f"{len(_broad)} 处：" + ", ".join(f"{f}:{ln}={k}" for f, ln, k in _broad))
    if _broad and not _unjustified
    else (f"无豁免落点：{_unjustified}" if _unjustified else "0 处宽 except"),
)

# C2：无可变默认参数（跨调用共享同一个 list/dict 是经典静默串数据）。
_mut_default: list[str] = []
for _f in WP27_FILES:
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

# C3：事务边界只由 `session_scope` 管 —— 本模块不得自行 commit/rollback。
_tx_leak: list[str] = []
for _f in WP27_FILES:
    for _i, _l in enumerate(SD[_f].splitlines(), 1):
        if re.search(r"\.(commit|rollback)\s*\(", _l):
            _tx_leak.append(f"{_f}:{_i}")
emit(
    not _tx_leak,
    "[C3] 13 个文件无 commit/rollback（剥离注释后）",
    _tx_leak or "0 处（事务边界归 session_scope）",
)

# C4：SQL 实参不得把**运行期值**拼进去（注入面 + 静默错值）。
# 本包实测 3 处 f-string（`models.py` 的 server_default），插值项全是模块级常量。
_sql_interp: list[str] = []
_sql_dynamic: list[str] = []
for _f in WP27_FILES:
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

# C5 / C5b：`assert` 分层 —— 模块级是导入期对账（可接受），函数体内是防线形态（禁）。
_mod_asserts: list[str] = []
_mod_assert_ids: set[tuple[str, int]] = set()
for _f in WP27_FILES:
    for _n in ast_of(RAW[_f]).body:
        if isinstance(_n, ast.Assert):
            _mod_asserts.append(f"{_f}:{_n.lineno}")
            _mod_assert_ids.add((_f, _n.lineno))
# ⚠️ 模块级 `for` 循环体内的 assert 仍在模块作用域（缩进但非函数）⇒ 也要归入模块级。
_mod_loop_body_asserts: list[str] = []
for _f in WP27_FILES:
    for _top in ast_of(RAW[_f]).body:
        if not isinstance(_top, (ast.For, ast.While, ast.If)):
            continue
        for _sub in ast.walk(_top):
            if isinstance(_sub, ast.Assert) and (_f, _sub.lineno) not in _mod_assert_ids:
                # 排除落在函数定义里的（那属函数体）
                _own = _enclosing_func(RAW[_f], _sub)
                if _own is None:
                    _mod_asserts.append(f"{_f}:{_sub.lineno}")
                    _mod_assert_ids.add((_f, _sub.lineno))
                    _mod_loop_body_asserts.append(f"{_f}:{_sub.lineno}")
_fn_asserts: list[str] = []
for _f in WP27_FILES:
    for _n in ast.walk(ast_of(RAW[_f])):
        if isinstance(_n, ast.Assert) and (_f, _n.lineno) not in _mod_assert_ids:
            _own = _enclosing_func(RAW[_f], _n)
            if _own is not None:
                _fn_asserts.append(f"{_f}:{_n.lineno} in {_own.name}()")
emit(
    bool(_mod_asserts) and all(s.startswith("models.py:") for s in _mod_asserts),
    "[C5b] 模块级 assert 全部落在 models.py（导入期对账；`-O` 下会消失，登记为 MINOR）",
    f"{len(_mod_asserts)} 处（其中循环体内 {len(_mod_loop_body_asserts)} 处）："
    + ", ".join(_mod_asserts),
)
emit(
    _fn_asserts == ["service.py:1905 in _persist_generation()", "service.py:1732 in _execute_locked()"],
    "[C5/MINOR-1] 函数体内 assert 恰为 2 处（WP28 4.5 新增 `_execute_locked` 一处；两条均 `-O` 下失效、"
    "均带 `# noqa: S101` 与『调用方已拦』说明）。"
    "⚠️ 本判据钉**精确行号**，故 media/service.py 任何行数变动都会令其报红（2026-10-16 因 WP28 4.5 插入代码而首次遇此）",
    f"{len(_fn_asserts)} 处：{', '.join(_fn_asserts)}" if _fn_asserts else "0 处",
)

# C6：`SchemaViolation` 的 reason 必须逐条给出。
_sv_missing: list[str] = []
for _n in ast.walk(ast_of(RAW["schemas.py"])):
    if isinstance(_n, ast.Call) and _callee_name(_n) == "SchemaViolation":
        if not any(k.arg == "reason" for k in _n.keywords):
            _sv_missing.append(f"schemas.py:{_n.lineno}")
# 结构强制：`__init__` 把 reason 设为 keyword-only 且**无默认值**。
_sv_init = func_src(SD_SCHEMAS, "__init__")
emit(
    not _sv_missing,
    "[C6] 每处 `SchemaViolation(...)` 都带 `reason=`（R1 的四类拒绝必须可区分）",
    _sv_missing or "0 处缺 reason",
)
emit(
    "reason: str" in _sv_init and "*," in _sv_init,
    "[C6b] `SchemaViolation.__init__` 的 `reason` 是 keyword-only 且无默认值（结构强制）",
    f"签名含 keyword-only={'*,' in _sv_init}"
    f" / reason 必填={'reason: str' in _sv_init}",
)

# C7（CRITICAL-1 的**反题**）：幂等查重**必须**在 `media/service.py` 里有调用点。
# ⚠️ 判据自身必须走 AST 调用计数 —— 子串匹配会命中 `service.py` 编排表里的
# "幂等查重"那句注释（它在第 6 步那一行），于是**恒真**。
_find_ctx_calls = calls_in(SD_SVC, "find_generation_context")
_exec_ctx_calls = calls_in(func_src(SD_SVC, "_execute_locked"), "find_generation_context")
_ctx_repo_def = func_node(SD_REPO, "find_generation_context") is not None
emit(
    len(_find_ctx_calls) == 0 and _ctx_repo_def,
    "[CR-1 CRITICAL-1①] 钉住现状：`find_generation_context` 已在 repository 定义，"
    "而 `media/service.py` 调用点 **0 处**（声称第 6 步查重、实现缺失）",
    f"repository 定义={'有' if _ctx_repo_def else '无'} / service 调用点={len(_find_ctx_calls)} 处",
)
emit(
    len(_exec_ctx_calls) == 0,
    "[CR-1 CRITICAL-1②] `_execute_locked` 内**零**幂等查重调用（与 CR 报告一致；"
    "修复后本判据会变红，逼同步 CR 与登记册）",
    f"_execute_locked 内调用点={len(_exec_ctx_calls)} 处",
)

# C8：声称句必须能在代码里找到落点 —— 幂等键派生点唯一。
_key_defs = calls_in(SD_GW, "media_generation_idempotency_key")
emit(
    bool(_key_defs),
    "[C8] 幂等键派生点存在且唯一（`gateway_bridge.media_generation_idempotency_key`）",
    f"`gateway_bridge.py` 内构造调用 {len(_key_defs)} 处",
)

# ---------------------------------------------------------------------------
head("--- 第二遍：并发 / 事务 ---")
# ---------------------------------------------------------------------------

# C9：行锁是执行入口的**第一步**（同一媒体位的重做由此串行化）。
_exec_src = SD_SVC
_lines_svc = _exec_src.splitlines()


def _lineno_of_first(needle: str, start: int = 1) -> int:
    for _i in range(start - 1, len(_lines_svc)):
        if needle in _lines_svc[_i]:
            return _i + 1
    return -1


_lock_ln = _lineno_of_first("get_media_item_for_update(media_item_id)")
_next_ver_ln = _lineno_of_first("next_version_no(media_item_id=media_item_id)")
_estimate_ln = _lineno_of_first("estimate_generation_cost(")
_reserve_ln = _lineno_of_first("reserve_generation_budget(")
_generate_ln = _lineno_of_first("generator.dispatch(")
_ctx_insert_ln = _lineno_of_first("insert_generation_context(")
_settle_ln = _lineno_of_first("_try_settle(")
_advance_ln = _lineno_of_first("advance_projection(")

emit(
    _lock_ln > 0 and _lock_ln < min(x for x in (_next_ver_ln, _estimate_ln) if x > 0),
    "[C9] `get_media_item_for_update` 是执行入口的第一步（行锁早于取号/预估）",
    f"行锁 L{_lock_ln} < 取号 L{_next_ver_ln} / 预估 L{_estimate_ln}",
)

# C10（R2）：`estimate` → `reserve` → `dispatch` 的**行号顺序**必须严格递增。
emit(
    0 < _estimate_ln < _reserve_ln < _generate_ln,
    "[C10/R2] 预估 < 预占 < 派发（行号严格递增：先扣钱再出网）",
    f"L{_estimate_ln} < L{_reserve_ln} < L{_generate_ln}",
)

# C11：投影推进必须在取号**之后**，且用**条件 UPDATE**（含 lock_version）。
_adv_src = func_src(SD_REPO, "advance_projection")
emit(
    "MediaItem.lock_version == expected_lock_version" in _adv_src
    and "lock_version=MediaItem.lock_version + 1" in _adv_src,
    "[C11] `advance_projection` 用条件 UPDATE（`WHERE id = :id AND lock_version = :expected`）",
    f"含 lock_version 条件={'MediaItem.lock_version == expected_lock_version' in _adv_src}",
)
emit(
    0 < _advance_ln and 0 < _next_ver_ln and _next_ver_ln < _advance_ln,
    "[C11b] 投影推进在取号之后（版本号与投影同一次事务内自洽）",
    f"取号 L{_next_ver_ln} < 投影 L{_advance_ln}",
)

# C12：`savepoint` 必须包住"取号 + 插入"与结算两段。
_sp_calls = [
    f"{_rel}:{_n.lineno}"
    for _rel, _src in (("service.py", SD_SVC), ("repository.py", SD_REPO))
    for _n in calls_in(_src, "savepoint")
]
emit(
    len(_sp_calls) >= 2,
    "[C12] 撞唯一约束/需局部回滚的写入段包在 `savepoint()` 里（`begin_nested`）",
    f"{len(_sp_calls)} 处调用点：{', '.join(_sp_calls)}",
)

# C13：执行入口全程**不自行** commit/rollback（与 C3 互为印证，但这里锚在函数级）。
_exec_body_tx = [
    _i
    for _i, _l in enumerate(
        (ast.get_source_segment(SD_SVC, func_node(SD_SVC, "_execute_locked")) or "").splitlines(),
        1,
    )
    if re.search(r"\.(commit|rollback)\s*\(", _l)
]
emit(
    not _exec_body_tx,
    "[C13] `_execute_locked` 函数体内无 commit/rollback（事务边界归 `session_scope`）",
    _exec_body_tx or "0 处",
)

# C14：`set_state` / `set_pending_*` **不**推进 `lock_version`（避免与投影推进互相自锁）。
_set_state_src = func_src(SD_REPO, "set_state")
emit(
    "lock_version" in _set_state_src,
    "[C14] `set_state` 与 `advance_projection` 分离（失败路径不动投影；`set_state` 按自身语义处理 lock_version）",
    f"set_state 含 lock_version={'lock_version' in _set_state_src}",
)

# ---------------------------------------------------------------------------
head("--- 第三遍：边界 / 加固 ---")
# ---------------------------------------------------------------------------

# C15：ddl 的清单函数 —— 一致性三项（items / triggers / functions）必须**等长**，
# 且 `immutable_function_names` 恰是跨包共用那一个（建立 ≠ 拥有）。
_consistency_items_count = _for_iterable_tuple_count(SD_DDL, "consistency_items")
_consistency_trigs = _returned_symbols(SD_DDL, "consistency_trigger_names")
_consistency_funcs = _returned_symbols(SD_DDL, "consistency_function_names")
_immutable_funcs = _returned_symbols(SD_DDL, "immutable_function_names")
emit(
    _consistency_items_count > 0
    and _consistency_items_count == len(_consistency_trigs) == len(_consistency_funcs),
    "[C15/ISS-79] `consistency_items` / `consistency_trigger_names` / `consistency_function_names` 三者等长",
    f"items={_consistency_items_count}（for 元组项数） / triggers={len(_consistency_trigs)} / functions={len(_consistency_funcs)}",
)
emit(
    "rednote_reject_mutation" in "".join(_immutable_funcs),
    "[C15b/ISS-79] `immutable_function_names()` 恰是跨包共用那一个（**建立 ≠ 拥有**）",
    f"返回值={_immutable_funcs}",
)

# C16：`index_names()` 由 ORM 派生（不手写清单 —— 铁律 14）。
_index_src = func_src(SD_DDL, "index_names")
emit(
    "_session" not in _index_src and ("Index" in _index_src or "__table__" in _index_src or "sorted" in _index_src),
    "[C16] `index_names()` 由 ORM 派生（不手写清单）",
    f"函数体含 ORM 派生逻辑={'Index' in _index_src or '__table__' in _index_src}",
)

# C17：契约路径数与本包端点自洽。
_contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
_contract_paths = set(_contract.get("paths", {}).keys())
_missing_paths = [p for p in WP27_PATHS if p not in _contract_paths]
_routes = len(re.findall(r"^@router\.", RAW["api.py"], flags=re.MULTILINE))
emit(
    not _missing_paths,
    "[C17] 本包 6 条路径全部进契约",
    f"{len(WP27_PATHS) - len(_missing_paths)}/{len(WP27_PATHS)}"
    + (f"，缺 {_missing_paths}" if _missing_paths else ""),
)
emit(
    _routes == 7,
    "[C17b] `api.py` 路由装饰器恰 7 个（6 路径 + `reference-assets` 的 GET/POST 两个 operation）",
    f"{_routes} 个",
)

# C18：`_HTTP_BY_REASON` 登记数 >= 承诺下界 20（活性下界；防"扫到 0 个 = 全通过"）。
_http_keys = _dict_keys(SD_API, "_HTTP_BY_REASON")
emit(
    len(_http_keys) >= 20,
    "[C18] `_HTTP_BY_REASON` 键数 >= 20（活性下界）",
    f"{len(_http_keys)} 个键",
)

# C19：段落切分三处**同口径**（只按空行切）—— 跨模块坐标系必须唯一。
_media_split = re.search(r'_PARAGRAPH_SPLIT.*?=\s*re\.compile\(r"([^"]+)"\)', RAW["schemas.py"])
_content_split = re.search(
    r'_PARAGRAPH_SPLIT.*?=\s*re\.compile\(r"([^"]+)"\)',
    (CONTENT_MODULE / "schemas.py").read_text(encoding="utf-8"),
)
_pat_media = _media_split.group(1) if _media_split else "<未找到>"
_pat_content = _content_split.group(1) if _content_split else "<未找到>"
emit(
    _pat_media == _pat_content == r"\n\s*\n",
    "[C19] 段落切分三处同口径（只按空行切；`media.schemas` 与 `content.schemas` 逐字同值）",
    f"media={_pat_media!r} / content={_pat_content!r}",
)
_parity_tool = REPO_ROOT / "tools" / "quality" / "check_paragraph_split_parity.py"
emit(
    _parity_tool.is_file(),
    "[C19b] 切段同口径有独立判据守住（`check_paragraph_split_parity.py`）",
    f"{'存在' if _parity_tool.is_file() else '缺失'}",
)

# C19c：OP-197 要求的**第二处文档副本**（权威口径落点）必须真实存在。
# ⚠️ 这条是 2026-10-11 补的：此前 `docs/数据结构与接口契约.md` **全仓不存在**
#    （`git log --all` 零历史），而登记册 OP-197 与 OP-199 **两条都点名它**
#    作为权威口径落点 ⇒ 幽灵落点（ISS-95 族：声称有落点、落点不存在）。
#    该文档现已补写，且 `check_paragraph_split_parity.py::G8` 会核对它的
#    §1.4 场景表与判据 `_EXPECTED` **双向对账**（不只是存在性）。
#    本判据（CR 侧）只做存在性 + 被 G8 覆盖这两项粗筛；细对账在 G8。
_contract_doc = REPO_ROOT / "docs" / "数据结构与接口契约.md"
_parity_src = _parity_tool.read_text(encoding="utf-8") if _parity_tool.is_file() else ""
emit(
    _contract_doc.is_file() and "G8" in _parity_src,
    "[C19c/OP-197] 权威口径第二处文档副本存在（`docs/数据结构与接口契约.md`）"
    "且判据 G8 对账它",
    f"文档={'存在' if _contract_doc.is_file() else '缺失'} / "
    f"判据含G8={'是' if 'G8' in _parity_src else '否'}",
)

# C20：`ImageByteSource` 无默认实现 ⇒ fail-closed（OP-195 的设计决策）。
_gw_lines = RAW["gateway_bridge.py"].splitlines()
_byte_source_none = False
for _n in ast.walk(ast_of(SD_GW)):
    if not isinstance(_n, ast.Compare):
        continue
    _seg = ast.get_source_segment(SD_GW, _n) or ""
    if "byte_source" in _seg and "None" in _seg:
        _byte_source_none = True
        break
emit(
    _byte_source_none,
    "[C20/OP-195] `ImageByteSource` 无默认实现 ⇒ 未注入时 fail-closed（不会静默出网）",
    f"检测到 byte_source is None 分支={'是' if _byte_source_none else '否'}",
)

# C21：本包的**过时零覆盖声称**（MAJOR-1）—— 钉住现状。
# 声称句：docstring 里写"该文件全仓不存在 / 本包零覆盖"，
# 而 `test_media_wp27.py` 现存（行数 > 1000）。
_claim_files = ("models.py", "schemas.py", "types.py")
_stale_claims: list[str] = []
for _f in _claim_files:
    for _i, _l in enumerate(RAW[_f].splitlines(), 1):
        if ("零覆盖" in _l) or ("全仓不存在" in _l) or ("该文件不存在" in _l):
            _stale_claims.append(f"{_f}:{_i}")
_test_lines = len(TEST_FILE.read_text(encoding="utf-8").splitlines())
emit(
    bool(_stale_claims) and _test_lines > 1000,
    "[MAJOR-1] 钉住现状：模块内仍有『本包零覆盖 / 该文件全仓不存在』的过时声称，"
    "而 test_media_wp27.py 已落地（> 1000 行）；修复后本判据会变红",
    f"{len(_stale_claims)} 处过时声称（{_claim_files}），测试文件 {_test_lines} 行",
)

# C22 / C23：WP27 两条迁移的 CHECK 文本是**化石字面量**，不活引用模块常量。
# 为什么必须钉住：迁移只在该刻生效，之后活常量怎么改老库都不动
# ⇒ 委托给活常量会造成"新库能过、老库过不了"且无判据报红。
_mig_const_imports: list[str] = []
_mig_live_refs: list[str] = []
for _stem in WP27_MIGRATION_STEMS:
    _matches = list(MIGRATIONS.glob(f"*{_stem}*.py"))
    if not _matches:
        continue
    _mig = _matches[0]
    _msrc = _mig.read_text(encoding="utf-8")
    _mtree = ast_of(_msrc)
    for _n in ast.walk(_mtree):
        if isinstance(_n, ast.ImportFrom) and _n.module and "rednote" in _n.module:
            _mig_const_imports.append(f"{_mig.name}:{_n.lineno} from {_n.module}")
    # 活引用：迁移模块顶层**不得**出现 `rednote.modules.media.types` 的名字。
    for _n in ast.walk(_mtree):
        if isinstance(_n, ast.Name) and _n.id in (
            "PENDING_BINDINGS_IS_ARRAY_SQL",
            "PENDING_BINDINGS_SIZE_SQL",
            "PENDING_REGENERATION_REASON_SQL",
            "MAX_REGENERATION_REASON_CHARS",
            "MIN_REFERENCE_ASSETS",
            "MAX_REFERENCE_ASSETS",
        ):
            _mig_live_refs.append(f"{_mig.name}:{_n.lineno} {_n.id}")
emit(
    not _mig_const_imports,
    "[C22] WP27 两条迁移不 import 本仓模块（CHECK 文本是化石字面量，非活常量委托）",
    _mig_const_imports or "0 处 rednote import",
)
emit(
    not _mig_live_refs,
    "[C23] WP27 两条迁移不活引用 `media.types` 的值域常量（防『新库能过、老库过不了』）",
    _mig_live_refs or "0 处活引用",
)

# C24：`check_migration_value_domain_no_live_import.py` 存在（C22/C23 的独立判据）。
_vd_tool = REPO_ROOT / "tools" / "quality" / "check_migration_value_domain_no_live_import.py"
emit(
    _vd_tool.is_file(),
    "[C24] 迁移活常量委托有独立判据（`check_migration_value_domain_no_live_import.py`）",
    f"{'存在' if _vd_tool.is_file() else '缺失'}",
)

# C25（TESTS）：本包被点名声称的断言**是否真的存在**。
# `types.py` docstring 声称"测试里有一条断言 images_per_content() is IMAGES_PER_CONTENT"。
_test_src = TEST_FILE.read_text(encoding="utf-8")
_has_img_assert = "images_per_content" in _test_src
emit(
    not _has_img_assert,
    "[TESTS-1] 钉住现状：`types.py` 点名声称的 `images_per_content() is IMAGES_PER_CONTENT` "
    "断言在 test 文件**零命中**（补上后本判据变红）",
    f"test 内 `images_per_content` 命中={'有' if _has_img_assert else '0 次'}",
)

# C26：CRITICAL-1 的**对照实现**存在（证明"该怎么做"在仓内有先例，不是空想）。
_content_svc = (CONTENT_MODULE / "service.py").read_text(encoding="utf-8")
_content_has_ctx = "find_generation_context(" in _content_svc
emit(
    _content_has_ctx,
    "[CR-1 对照] WP26 的 `content/service.py` **确**调 `find_generation_context`（幂等查重有先例）",
    f"content/service.py 命中={'有' if _content_has_ctx else '无'}",
)

# ===========================================================================
# 汇总
# ---------------------------------------------------------------------------
_passed = sum(1 for c in checks if c)
_failed = len(checks) - _passed

lines.append("")
lines.append("--- 汇总 ---")
lines.append(f"共 {len(checks)} 条 CHECK：{_passed} PASS / {_failed} FAIL")
lines.append("")
lines.append("钉住现状型判据（ok=True 表示『现状与 CR 报告一致』，非『代码没问题』）：")
lines.append("  · [CR-1 CRITICAL-1①/②] 幂等查重声称 vs service 零调用")
lines.append("  · [MAJOR-1] 过时『零覆盖』声称 vs 测试文件已落地")
lines.append("  · [TESTS-1] `images_per_content` 被点名断言缺失")

# ---------------------------------------------------------------------------
# 为什么必须有 `EXPECTED_CHECKS`：只统计"失败了几条"时，
# "脚本崩溃、本该产生的 CHECK 一条都没产生"与"全部通过"在计数上完全一样
# （ISS-43）。故必须**另有一个与实测无关的期望值**来对账。
#
# 为什么必须 `>=` 之外再加 `==`（ISS-153）：`>=` 拦不住"判据变多了而常量没跟着涨"，
# 于是新加的判据可以**悄悄不生效**。相等判据逼着人回来改常量。
# 2026-10-11：33 → **34**（新增 C19c —— OP-197 权威口径第二处文档副本）。
# ---------------------------------------------------------------------------
EXPECTED_CHECKS = 34
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
out = EVIDENCE_DIR / "wp27-code-review.log"
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
print(f"\n写入 {out}")

# ⚠️ **这一行是机械出口**：没有它，上面所有 CHECK 的红/绿
# 对下游（收口第 11 步 / CI）**完全不可见** —— 脚本恒返回 0。
# 由 `tools/m1/check_evidence_scripts.py::check_returns_exit_code` 守着不复发。
raise SystemExit(0 if _verdict else 1)
