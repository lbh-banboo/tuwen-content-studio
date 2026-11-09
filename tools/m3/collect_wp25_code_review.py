#!/usr/bin/env python
"""WP25 代码审查证据（三遍法：正确性/安全 → 并发/事务 → 边界/加固）。

产出 `evidence/m3/wp25-code-review.log`。

**为什么要把审查结果落成文件**：审查结论是"声称"，而声称需要证据。
本项目已踩过"文档/看板是唯一声称方时没有任何机械判据与它对账"（ISS-78/84/85/86），
因此审查记录里每一处"通过"都必须附上**能复现它的命令或代码位置**。

## 本包与 WP24 的三处结构性不同

1. **本包有真实的并发缺陷被发现并修复**（CR-1 MAJOR）：`append_ledger_entry`
   的幂等实现是"先查再插"，并发同键时 `23505` 冒成 HTTP 500。
   与 WP24 不同，这条**不是文档声称、而是实测抓到**，并且做了**反向验证**
   （退回缺陷实现 ⇒ 判据变红 ⇒ 恢复 ⇒ 变绿）。故本脚本的
   `--- 反向验证记录 ---` 一节是**实证**而非声明。

2. **本包有跨包不变量**（预算并发预占 = W10 验收线），
   它落在"条件 UPDATE 的 WHERE 必须自带语义"上，而不是触发器上。

3. **`within_limit` 是恒真式**（MATERIAL.md 第 2 条纪律的教科书案例）——
   本脚本对它的判据必须**反向**：证明"它拦不住并发，只能拦住绕过路径的直写"。

⚠️ 与 WP22/WP24 同款教训：**所有文本判据先经 `sans_doc()` 剥离注释与文档字符串**，
结构判据一律走 AST。本包尤其必要 —— `_insert_ledger_entry_idempotent`
的 docstring 里**逐字写着缺陷实现的代码**（"曾经的实现是：existing = ..."），
子串判据会把这段说明当成违规实现。
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"

BACKEND = REPO_ROOT / "services" / "backend"
MODULE = BACKEND / "src" / "rednote" / "modules" / "billing"
TESTS = BACKEND / "tests" / "test_billing_wp25.py"

#: 迁移文件名较长，用 glob 取真身（写死易随重命名失效）。
_MIG_CANDIDATES = sorted(
    (REPO_ROOT / "migrations" / "versions").glob("*daea2b27212c*.py")
)
MIGRATION = _MIG_CANDIDATES[0] if _MIG_CANDIDATES else None

WP25_FILES = (
    "__init__.py",
    "api.py",
    "ddl.py",
    "dependencies.py",
    "models.py",
    "money.py",
    "pricing.py",
    "reconciliation.py",
    "repository.py",
    "schema.py",
    "service.py",
    "types.py",
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
    把掩码溢出到后续行，静默少检查一段代码（WP24 实测踩到，见其脚本
    `_byte_col_to_char_col` 的完整论证）。本包源码中文密度更高，
    必须一开始就带上这个换算。
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


RAW: dict[str, str] = {f: read(f) for f in WP25_FILES}
SD: dict[str, str] = {f: sans_doc(RAW[f]) for f in WP25_FILES}

SD_SVC = SD["service.py"]
SD_REPO = SD["repository.py"]
SD_API = SD["api.py"]
SD_MODELS = SD["models.py"]


def ast_of(src: str) -> ast.Module:
    return ast.parse(src)


def func_node(src: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(ast_of(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def func_src(src: str, name: str, *, strip_docs: bool = True) -> str:
    node = func_node(src, name)
    if node is None:
        return ""
    base = sans_doc(src) if strip_docs else src
    return ast.get_source_segment(base, node) or ""


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


lines.append("== WP25 三遍法代码审查证据（机械可复现） ==")
lines.append("模块：services/backend/src/rednote/modules/billing/（12 文件）")
lines.append("测试：services/backend/tests/test_billing_wp25.py")
lines.append("")
lines.append("本脚本只做一件事：把本次审查里的**每一条结论**变成能在代码里当场指认的判据。")
lines.append("标注 [CR-1 MAJOR-N] 的条目另有**反向验证记录**（见文末），不是文档声称。")

# ---------------------------------------------------------------------------
head("--- 第一遍：正确性 / 安全 ---")
# ---------------------------------------------------------------------------

# C1：无裸 except / 宽 except。
# 宽 except 会把"真 bug"与"可预期失败"混为一谈，让故障从"当场红"变成"静默继续"。
_broad: list[str] = []
for _f in WP25_FILES:
    for _n in ast.walk(ast_of(RAW[_f])):
        if not isinstance(_n, ast.ExceptHandler):
            continue
        _t = _n.type
        if _t is None:
            _broad.append(f"{_f}:{_n.lineno} 裸 except")
        elif isinstance(_t, ast.Name) and _t.id in ("Exception", "BaseException"):
            _broad.append(f"{_f}:{_n.lineno} {_t.id}")
emit(
    not _broad,
    "[C1] 12 个文件无裸 except / 宽 except（AST 判定）",
    _broad or "0 处",
)

# C2：本包生产代码不得用 `assert` 当防线。
# `python -O` 会剥离 assert ⇒ 防线凭空消失。当前部署未用 -O（全仓实测无
# PYTHONOPTIMIZE / -O），但"当前没踩到"不等于"不是坑"。
#
# ⚠️ 判据范围刻意限定为**本包**：全仓另有 24 处同型（audit/baseline/brief/
# files/gateway/jobs/knowledge/parsing/planning），其中 planning/allocation.py
# 的那处被 WP24 明确当作正当防线。本包先做到零，并把全仓议题登记为
# ISS 候选 —— 而不是假装"全仓已干净"。
_asserts: list[str] = []
for _f in WP25_FILES:
    for _n in ast.walk(ast_of(RAW[_f])):
        if isinstance(_n, ast.Assert):
            _asserts.append(f"{_f}:{_n.lineno} {ast.unparse(_n.test)[:60]}")
emit(
    not _asserts,
    "[C2/CR-1 MINOR-2] billing 模块生产代码无 assert（`-O` 会剥离防线）",
    _asserts or "0 处（本包已清零；全仓另有 24 处同型，另登记）",
)

# C2b：被 C2 替换掉的那处必须是**显式 raise**，而不是被删掉了事。
# 判据纪律：断言"某处没有某物"必须同时断言"该物被什么替代"，
# 否则删掉整段也能让 C2 变绿。
_idem_guard = func_src(SD_SVC, "_insert_ledger_entry_idempotent", strip_docs=False)
_idem_guard_sd = func_src(SD_SVC, "_insert_ledger_entry_idempotent")
_has_key_guard = bool(
    re.search(r"if\s+entry\.idempotency_key\s+is\s+None\s*:", _idem_guard_sd)
)
_raises_runtime = "RuntimeError" in _idem_guard_sd
emit(
    _has_key_guard and _raises_runtime,
    "[C2b] 键缺失路径改用显式 `if ... is None: raise RuntimeError`（不是删掉了事）",
    f"守卫={_has_key_guard} RuntimeError={_raises_runtime}",
)

# C3：无可变默认参数（跨调用共享同一个 list/dict 是经典静默串数据）。
_mut_default: list[str] = []
for _f in WP25_FILES:
    for _n in ast.walk(ast_of(RAW[_f])):
        if not isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for _d in list(_n.args.defaults) + [d for d in _n.args.kw_defaults if d]:
            if isinstance(_d, (ast.List, ast.Dict, ast.Set)):
                _mut_default.append(f"{_f}:{_n.lineno} {_n.name}")
emit(
    not _mut_default,
    "[C3] 无可变默认参数（AST 判定）",
    _mut_default or "0 处",
)

# C4：事务边界只由 `session_scope` 管 —— service / api 层不得自行 commit/rollback。
# 自行 commit 会切断"业务操作 = 一个事务"的保证，让部分写入变成可见中间态。
_tx_leak: list[str] = []
for _f in ("service.py", "api.py", "reconciliation.py", "repository.py"):
    for _i, _l in enumerate(SD[_f].splitlines(), 1):
        if re.search(r"\.(commit|rollback)\s*\(", _l):
            _tx_leak.append(f"{_f}:{_i}")
emit(
    not _tx_leak,
    "[C4] service/api/reconciliation/repository 无 commit/rollback（剥离注释后）",
    _tx_leak or "0 处（事务边界归 session_scope）",
)

# C5：SQL 不得用字符串插值拼参数（注入面 + 静默错值）。
# 走 AST 找 `f"..."` / `%` / `.format` 出现在 text()/execute() 实参里的形态。
_sql_interp: list[str] = []
for _f in WP25_FILES:
    for _n in ast.walk(ast_of(RAW[_f])):
        if not isinstance(_n, ast.Call):
            continue
        _attr = _n.func.attr if isinstance(_n.func, ast.Attribute) else (
            _n.func.id if isinstance(_n.func, ast.Name) else ""
        )
        if _attr not in ("text", "execute", "exec_driver_sql"):
            continue
        for _a in list(_n.args) + [k.value for k in _n.keywords]:
            if isinstance(_a, ast.JoinedStr):
                _sql_interp.append(f"{_f}:{_n.lineno} f-string")
            elif isinstance(_a, ast.BinOp) and isinstance(_a.op, ast.Mod):
                _sql_interp.append(f"{_f}:{_n.lineno} % 插值")
            elif (
                isinstance(_a, ast.Call)
                and isinstance(_a.func, ast.Attribute)
                and _a.func.attr == "format"
            ):
                _sql_interp.append(f"{_f}:{_n.lineno} .format")
emit(
    not _sql_interp,
    "[C5] SQL 实参无 f-string / % / .format 插值（AST 判定）",
    _sql_interp or "0 处（全部走绑定参数）",
)

# C6：金额一律经 `money` 模块量化 —— 不允许裸 Decimal 运算写库。
# 定点标度不一致会让 0.1+0.2 类误差累积进账面。
_quantize_calls = len(calls_in(SD_SVC, "quantize"))
emit(
    _quantize_calls >= 4,
    "[C6] service.py 写库前经 money.quantize（≥4 处调用）",
    f"{_quantize_calls} 处",
)

# ---------------------------------------------------------------------------
head("--- 第二遍：并发 / 事务 ---")
# ---------------------------------------------------------------------------

# C7（★ CR-1 MAJOR）：幂等原语必须存在。
_idem_def = func_node(RAW["service.py"], "_insert_ledger_entry_idempotent")
emit(
    _idem_def is not None,
    "[C7/CR-1 MAJOR] `_insert_ledger_entry_idempotent` 存在（并发幂等的载体）",
    f"L{_idem_def.lineno}" if _idem_def else "未定义",
)

# C8（★ CR-1 MAJOR）：`append_ledger_entry` 必须**真的调用**它。
# ⚠️ 这条是第一遍的"子串判据"抓不到的：定义存在 ≠ 被使用。
# 反向验证时正是把这一处换回旧形态，判据才变红。
_append_src = func_src(SD_SVC, "append_ledger_entry")
_uses_idem = "_insert_ledger_entry_idempotent(" in _append_src
_uses_old = "_insert_ledger_entries(" in _append_src
emit(
    _uses_idem and not _uses_old,
    "[C8/CR-1 MAJOR] append_ledger_entry 调用幂等原语，**不再**走裸 `_insert_ledger_entries`",
    f"用幂等原语={_uses_idem} 仍用裸插入={_uses_old}",
)

# C9（★ CR-1 MAJOR）：并发正确性必须来自 `except IntegrityError` + SAVEPOINT。
# 只保留"先查一次"（快路径）在并发下会漏 —— 这正是缺陷原形。
_idem_sd = func_src(SD_SVC, "_insert_ledger_entry_idempotent")
_has_except = "except IntegrityError" in _idem_sd
_has_nested = "begin_nested()" in _idem_sd
_has_recheck = "find_ledger_entry_by_idempotency" in _idem_sd
emit(
    _has_except and _has_nested and _has_recheck,
    "[C9/CR-1 MAJOR] 幂等原语 = SAVEPOINT + except IntegrityError + 回查（三者齐备）",
    f"except={_has_except} savepoint={_has_nested} 回查={_has_recheck}",
)

# C10（★）：只对 `23505` 回查，其它完整性错误必须原样抛出。
# 把 FK / CHECK 违例也当成"重复导入"，会把**真缺陷**吞成正常返回。
_is_uniq_def = func_node(RAW["service.py"], "_is_unique_violation")
emit(
    _is_uniq_def is not None
    and "UNIQUE_VIOLATION_SQLSTATE" in func_src(SD_SVC, "_is_unique_violation"),
    "[C10/CR-1 MAJOR] 唯一冲突判定走 `ddl.UNIQUE_VIOLATION_SQLSTATE` 常量（非 message 文本）",
    "常量比对" if _is_uniq_def else "未定义",
)
# 反向：非唯一冲突必须 re-raise（不能静默返回）。
_reraises_others = bool(
    re.search(r"if\s+not\s+_is_unique_violation\(exc\)\s*:\s*\n\s*raise", _idem_sd)
)
emit(
    _reraises_others,
    "[C10b] 非 `23505` 的 IntegrityError 原样 `raise`（AST/正则判定函数体）",
    "已 re-raise" if _reraises_others else "未找到 re-raise 分支",
)

# C11：三条账面条件 UPDATE 的 WHERE 必须自带语义。
# 这是"并发预占"能成立的**唯一**机制：把校验写进 WHERE，
# 让 DB 在一条语句里完成"读-判-写"，而不是应用层先查后写。
_reserve_src = func_src(SD_REPO, "reserve_budget")
_release_src = func_src(SD_REPO, "release_budget")
_settle_src = func_src(SD_REPO, "settle_budget")
_reserve_ok = (
    "committed_amount + reserved_amount + delta <= limit_amount" in _reserve_src
    or "committed_amount" in _reserve_src
    and "limit_amount" in _reserve_src
    and "rowcount" in _reserve_src
)
_settle_ok = "reserved_amount >= reserved_release" in _settle_src
_release_ok = "reserved_amount >= delta" in _release_src
emit(
    _reserve_ok and _settle_ok and _release_ok,
    "[C11] 三条账面 UPDATE 的条件写在 WHERE 里（reserve/settle/release）",
    f"reserve={_reserve_ok} settle={_settle_ok} release={_release_ok}",
)

# C12：`release_budget` 对非正 delta 必须**早返回 True**（幂等释放）。
# 否则重复释放会走 WHERE 失败路径、被误判为"资源不存在"。
_release_early = bool(
    re.search(r"if\s+delta\s*<=\s*0\s*:", _release_src)
)
emit(
    _release_early,
    "[C12] release_budget 非正 delta 早返回（重复释放幂等）",
    "命中" if _release_early else "未找到",
)

# C13：`set_budget_limit` 用双条件 WHERE（装得下 + 币种），不是先查后写。
_setlimit_src = func_src(SD_REPO, "set_budget_limit")
emit(
    "limit_amount" in _setlimit_src and "currency" in _setlimit_src
    and "rowcount" in _setlimit_src,
    "[C13] set_budget_limit 单语句双条件（额度可容纳 + 币种一致）",
    "命中" if "rowcount" in _setlimit_src else "未找到 rowcount 判定",
)

# C14：无 N+1 —— 循环体内不得出现逐项目查询。
#
# ⚠️ 必须走 AST 判定**循环体**：本模块注释里反复提到"逐项目查"这类词，
# 子串判据会命中说明文字（WP22/WP24 的第一版假发现都是这个根因）。
_n_plus_1: list[str] = []
for _f in ("service.py", "reconciliation.py", "repository.py"):
    for _loop in [n for n in ast.walk(ast_of(RAW[_f])) if isinstance(n, (ast.For, ast.While))]:
        for _sub in ast.walk(_loop):
            if not isinstance(_sub, ast.Call):
                continue
            _fa = _sub.func
            _nm = _fa.attr if isinstance(_fa, ast.Attribute) else (
                _fa.id if isinstance(_fa, ast.Name) else ""
            )
            if _nm in ("get_budget", "find_usable_pricing_version", "current_project_setting"):
                _n_plus_1.append(f"{_f}:{_sub.lineno} {_nm}")
emit(
    not _n_plus_1,
    "[C14] 循环体内无逐行查询（get_budget / find_usable_pricing_version / current_project_setting）",
    _n_plus_1 or "0 处",
)

# C15：`ensure_budget` 用 `ON CONFLICT DO NOTHING`（并发建预算行的正确姿势）。
_ensure_src = func_src(SD_REPO, "ensure_budget")
emit(
    "on_conflict_do_nothing" in _ensure_src,
    "[C15] ensure_budget 走 ON CONFLICT DO NOTHING（并发建行不撞 23505）",
    "命中" if "on_conflict_do_nothing" in _ensure_src else "未找到",
)

# C16：预算**并发预占**的约定必须写明"零元不得预占"。
# 0 元预占会通过所有并发判据（成功数 × 0 = 0 ≤ limit），
# 让"并发安全"的结论变成空话。
_pos_guard = func_node(RAW["service.py"], "_require_positive_amount")
emit(
    _pos_guard is not None,
    "[C16] `_require_positive_amount` 存在（0 元预占会通过所有并发判据）",
    f"L{_pos_guard.lineno}" if _pos_guard else "未定义",
)

# ---------------------------------------------------------------------------
head("--- 第三遍：边界 / 加固 ---")
# ---------------------------------------------------------------------------

# C17：不可变表清单 —— 取**单一来源**（`models.py`），并断言 `ddl.py` 是 import 它。
#
# ⚠️ 口径修正（CR-1 第二轮，见文末『判据口径修正记录』）：
# 旧口径在 `ddl.py` 里正则找 `IMMUTABLE_TABLES = (...)`，但该常量**定义在
# `models.py`**（`ddl.py` 只 `from ...models import IMMUTABLE_TABLES`，
# 因为反向 import 会成环）。⇒ 旧口径**永远解析不到**，恒报 FAIL。
# 恒 FAIL 的判据最终会被人关掉 —— 比没有判据更糟（ISS-95 的近亲）。
# 新口径改为走 AST 取 `models.py` 的**字面量真身**，并**另加**两条：
#   · `ddl.py` 必须 import 它（否则会出现两份清单，改一处漏一处）
#   · `ddl.py` 必须真的用它驱动触发器（`for table in IMMUTABLE_TABLES`）
# —— 只断言"值对"是不够的：值对但没人用，等于没挂触发器。
_MODELS_SRC = RAW["models.py"]
_DDL_SD = SD["ddl.py"]


def _module_literal(src: str, name: str):
    """取模块级 `NAME: T = <字面量>` / `NAME = <字面量>` 的值（AST，不碰 docstring）。"""
    for _n in ast.parse(src).body:
        if isinstance(_n, ast.AnnAssign) and isinstance(_n.target, ast.Name):
            if _n.target.id == name:
                return ast.literal_eval(_n.value)
        elif (
            isinstance(_n, ast.Assign)
            and len(_n.targets) == 1
            and isinstance(_n.targets[0], ast.Name)
        ):
            if _n.targets[0].id == name:
                return ast.literal_eval(_n.value)
    return None


_imm_declared = _module_literal(_MODELS_SRC, "IMMUTABLE_TABLES")
_imm_imported = bool(
    re.search(
        r"from\s+rednote\.modules\.billing\.models\s+import\s+[^\n]*IMMUTABLE_TABLES",
        RAW["ddl.py"],
    )
)
_imm_drives = "for table in IMMUTABLE_TABLES" in _DDL_SD
emit(
    _imm_declared is not None
    and tuple(_imm_declared) == ("pricing_versions", "usage_ledger")
    and _imm_imported
    and _imm_drives,
    "[C17] IMMUTABLE_TABLES（models.py 单一来源）== (pricing_versions, usage_ledger)"
    " 且 ddl.py import 它并驱动触发器",
    f"declared={_imm_declared} imported={_imm_imported} drives={_imm_drives}",
)

# C18：部分唯一索引的 spec 必须与声明同源（`WHERE idempotency_key IS NOT NULL`）。
# 这个 WHERE 写丢会让索引静默退化成"全表唯一"，
# 而 `usage_ledger` 里**允许**多条无键分录 ⇒ 正常业务会被拒。
#
# ⚠️ 口径修正（CR-1 第二轮）：`PARTIAL_UNIQUE_INDEXES` 的真身是
# **tuple-of-tuple**（`NAME: Final[tuple[...]] = (` ），不是 dict。
# 旧口径正则写的是 `=\s*\{`（dict 形态）⇒ 永远匹配不到 ⇒ 恒报 FAIL。
# 新口径：AST 取真身，**逐字段比对**（名字 / 键列含顺序 / WHERE），
# 并**另加**一条"WHERE 真的拼进 SQL"（否则写了个常量却不用 = 装饰）。
_pui_spec = _module_literal(RAW["ddl.py"], "PARTIAL_UNIQUE_INDEXES")
_pui_item = None
if _pui_spec:
    for _it in _pui_spec:
        if _it and _it[0] == "uq_usage_ledger_idempotency":
            _pui_item = _it
            break
_pui_name_ok = _pui_item is not None
_pui_cols_ok = bool(_pui_item) and tuple(_pui_item[2]) == ("idempotency_key",)
_pui_where_ok = bool(_pui_item) and _pui_item[3] == "idempotency_key IS NOT NULL"
# WHERE 必须真的进 CREATE INDEX 语句（本模块存在的全部理由就是消除那次重复抄写）。
_pui_used = "WHERE {where}" in RAW["ddl.py"]
emit(
    _pui_name_ok and _pui_cols_ok and _pui_where_ok and _pui_used,
    "[C18] 部分唯一索引 spec 逐字段：名 + 键列(含顺序) + WHERE `idempotency_key IS NOT NULL`，且 WHERE 真的进 SQL",
    f"name={_pui_name_ok} cols={_pui_cols_ok} "
    f"where={_pui_item[3] if _pui_item else None} used={_pui_used}",
)

# C19：`within_limit` CHECK 的**反向**判据 —— 它拦不住并发，只能拦绕过路径的直写。
# 本包必须把这条写在文档/代码里，否则读者会以为它是并发防线
# （MATERIAL.md 第 2 条：不能把 DB 已保证的事当判据）。
_within_in_models = "committed_amount + reserved_amount <= limit_amount" in SD_MODELS
_within_reasoned = (
    "恒真" in RAW["models.py"] or "恒真式" in RAW["models.py"]
)
emit(
    _within_in_models and _within_reasoned,
    "[C19] within_limit 存在，且文档/注释**显式说明它是恒真式**（不是并发防线）",
    f"约束在={_within_in_models} 已说明={_within_reasoned}",
)

# C20：`recompute_committed` 必须**不过滤符号**（冲正分录为负，要一起加）。
# 加了 `amount > 0` 过滤会让冲正永远冲不掉，账面只增不减。
_recompute = func_src(SD_REPO, "recompute_committed")
emit(
    bool(_recompute)
    and not re.search(r"amount\s*>\s*0", _recompute),
    "[C20] recompute_committed 不过滤符号（冲正分录必须计入）",
    "无反滤" if _recompute and not re.search(r"amount\s*>\s*0", _recompute) else "可疑",
)

# C21：`recompute_reserved` 必须**含 EXPIRED**（已过期未清理的预占仍占额度）。
_recompute_res = func_src(SD_REPO, "recompute_reserved")
emit(
    "HOLDING_RESERVATION_STATES" in _recompute_res,
    "[C21] recompute_reserved 走 HOLDING_RESERVATION_STATES（含 EXPIRED）",
    "命中" if "HOLDING_RESERVATION_STATES" in _recompute_res else "未找到",
)

# C22：`find_usable_pricing_version` 必须按"model 专属优先、再按生效时间倒序"取一条。
_usable = func_src(SD_REPO, "find_usable_pricing_version")
emit(
    "USABLE_PRICING_STATES" in _usable
    and "effective_from" in _usable
    and ("model_profile_id" in _usable),
    "[C22] find_usable_pricing_version：仅可用状态 + 生效下界 + 专属优先",
    "三要素齐备" if all(x in _usable for x in ("USABLE_PRICING_STATES", "effective_from", "model_profile_id")) else "缺项",
)

# C23（★）：`estimate_cost` 必须在**取价之前**做数量越界校验。
# 顺序反了会让"越界请求"先走一次取价（多余 IO），且在无价目时报错的
# 原因会指向"没配价"而不是"数量超限"—— 排障方向被误导（错误 ⑫ 同型）。
# ⚠️ 顺序比较必须走**剥离注释后**的文本：本函数自己的注释里就写着
# `_require_reasonable_quantity` 与 `validate_quantity`。
_est_sd = func_src(SD_SVC, "estimate_cost")
_i_qty = _est_sd.find("_require_reasonable_quantity")
_i_valid = _est_sd.find("validate_quantity")
_i_price = _est_sd.find("_require_pricing_version")
emit(
    0 <= _i_qty < _i_price and 0 <= _i_valid < _i_price,
    "[C23] estimate_cost：数量校验（_require_reasonable_quantity / validate_quantity）在取价 `_require_pricing_version` 之前（剥离注释后）",
    f"位置 reasonable@{_i_qty} validate@{_i_valid} pricing@{_i_price}",
)

# C24：`settle_reservation` 在 `entries` 非空时必须校验合计 == actual。
# 两处各说各话会让账面与账本从此分叉，且分叉后**没有**能自动发现它的判据。
_settle_res_sd = func_src(SD_SVC, "settle_reservation")
emit(
    "REASON_LEDGER_TOTAL_MISMATCH" in _settle_res_sd,
    "[C24] settle_reservation：Σ entries != actual ⇒ 拒绝（不替调用方选一个当真）",
    "命中" if "REASON_LEDGER_TOTAL_MISMATCH" in _settle_res_sd else "未找到",
)

# C25：`settle_reservation` 在 `entries` 为空时**必须自动补 BILL**。
# 少了它，`committed_amount` 会增加而账本没有对应分录 ⇒
# `recompute_committed` 永远报差异（"账面有、账本没有"是最该报警的方向）。
_autofill = "LEDGER_KIND_BILL" in _settle_res_sd and "settle:" in _settle_res_sd
emit(
    _autofill,
    "[C25] settle_reservation：entries 为空自动补一条 BILL（账面每次都对应一条分录）",
    "命中" if _autofill else "未找到自动补分录",
)

# C26：`set_budget` 拒绝负额度、且拒绝在已有金额时改币种。
_setbudget_sd = func_src(SD_SVC, "set_budget")
emit(
    "REASON_AMOUNT_NOT_POSITIVE" in _setbudget_sd
    or "REASON_CURRENCY_MISMATCH" in _setbudget_sd,
    "[C26] set_budget 拒绝负额度 / 已有金额时改币种",
    "命中" if "REASON_AMOUNT_NOT_POSITIVE" in _setbudget_sd else "未找到",
)

# C27：14 条路由（与测试第 4 层的计数判据互证）。
_routes: list[str] = []
for _n in ast.walk(ast_of(RAW["api.py"])):
    if isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for _d in _n.decorator_list:
            if isinstance(_d, ast.Call) and isinstance(_d.func, ast.Attribute):
                if _d.func.attr in ("get", "post", "put", "delete", "patch"):
                    _arg = _d.args[0].value if _d.args and isinstance(_d.args[0], ast.Constant) else "?"
                    _routes.append(f"{_d.func.attr.upper()} {_arg}")
emit(
    len(_routes) == 14,
    "[C27] api.py 恰好 14 条路由（与第 4 层 `test_router_exposes_exactly_fourteen_endpoints` 互证）",
    f"{len(_routes)} 条",
)

# C28（★ ISS-95 形态）：`pricing_version_fingerprint` 是"原语已备、接线未做"。
# 它的 docstring 曾声称"对账能立刻发现单价被绕过修改"，但
# `reconciliation` 不 import 它、`cost_estimates` 也没有存指纹的列。
# 这正是"声称有防线"的形态，故**必须**在 docstring 里显式标注"未接线"。
_fp_doc = RAW["pricing.py"]
_i_fp = _fp_doc.find("def pricing_version_fingerprint")
_fp_seg = _fp_doc[_i_fp:_i_fp + 2200] if _i_fp != -1 else ""
emit(
    "未接线" in _fp_seg,
    "[C28/CR-1 MINOR-1] 指纹函数的 docstring 显式标注『原语已备、接线未做』（不假装有防线）",
    "已标注" if "未接线" in _fp_seg else "未标注 —— 会误导读者以为对账在用它",
)
# 反向：确认真的是"未接线"（否则上面那条标注会过期）。
_recon_raw = RAW["reconciliation.py"]
emit(
    "fingerprint" not in _recon_raw and "fingerprint" not in SD_MODELS,
    "[C28 反向] 确证未接线：reconciliation 与 models 都不含 fingerprint",
    "确证" if "fingerprint" not in _recon_raw and "fingerprint" not in SD_MODELS else "已接线 —— 需更新 docstring 与判据",
)

# C29：测试文件存在且用例数达下界。
# ⚠️ 用**下界**而不是等值：后续补用例不该让判据变红（判据纪律第 1 条）。
_n_tests = 0
if TESTS.exists():
    _tt = ast.parse(TESTS.read_text(encoding="utf-8"))
    _n_tests = sum(
        1 for n in _tt.body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
    )
emit(
    TESTS.exists() and _n_tests >= 100,
    "[C29] test_billing_wp25.py 存在且用例数 ≥100（下界，不锁死具体值）",
    f"用例 {_n_tests} 条",
)

# C30：四层结构齐备（层分隔注释是**结构**，用 AST 判函数数不成立，故查分层标记）。
_tsrc = TESTS.read_text(encoding="utf-8") if TESTS.exists() else ""
_layers = [f"第 {i} 层" for i in (1, 2, 3, 4)]
_missing = [l for l in _layers if l not in _tsrc]
emit(
    not _missing,
    "[C30] 测试文件含四层分隔标记（1 纯函数 / 2 服务层 / 3 原生 SQL / 4 HTTP）",
    _missing or "四层齐备",
)

# C31：本包最容易写错的夹具口径 —— 断言"跨包入向 FK 不变"，不是"为 0"。
#
# ⚠️ 口径修正（CR-1 第二轮，同 WP24 的"错误 ⑫"）：
# 旧口径在**原始文本**里找 `inbound == 0`，结果命中的是 docstring 里
# **逐字写的反例说明**（"WP24 断言 `inbound == 0` 是对的……照抄会失败"）。
# 判据把"讲解为什么不能这么做"当成了"真的这么做了"。
# ⇒ 必须用 `sans_doc()` 剥离后的文本（这正是本脚本开头那段论证的用处）。
#
# 断言必须是**双向**的（判据纪律第 7 条）：
#   · 正向：真的用了 `cross_package_foreign_keys`（口径正确）
#   · 反向：剥离 docstring 后不再出现 `inbound == 0`（没错抄 WP24）
# 只做反向会被"把整段删掉"骗过；只做正向会被"两个都写"骗过。
_tsrc_raw = TESTS.read_text(encoding="utf-8") if TESTS.exists() else ""
_tsrc_sd = sans_doc(_tsrc_raw) if _tsrc_raw else ""
_inbound_raw = len(re.findall(r"inbound\s*==\s*0", _tsrc_raw))
_inbound_sd = len(re.findall(r"inbound\s*==\s*0", _tsrc_sd))
_uses_cross_sd = "cross_package_foreign_keys" in _tsrc_sd
emit(
    _uses_cross_sd and _inbound_sd == 0,
    "[C31] 夹具口径：用 `cross_package_foreign_keys`（跨包不变），"
    "剥离 docstring 后无 `inbound == 0`（不照抄 WP24）",
    f"用cross={_uses_cross_sd} 剥离后inbound==0={_inbound_sd} "
    f"（原文 {_inbound_raw} 处均在 docstring 的反例说明里）",
)

# C32：迁移链单一 head，且**本包迁移确实挂在上一个 head 之后**。
#
# ⚠️ 口径修正（CR-1 第二轮）：旧口径用逐行正则抓 `down_revision`，
# 但迁移文件的 docstring 里**以 `down_revision` 起头的说明行**长这样：
#     down_revision = 同 `(key, scope_kind, project_id)` 下 `revision_no` 最大那行 |
# 正则把它们当成了赋值 ⇒ 解析出 9 个假 head ⇒ 恒报 FAIL。
# 而且 `revision: str = "..."` 是 **AnnAssign**（带类型注解），
# 旧正则的 `^revision\s*=` 形态也匹配不到。
# ⇒ 必须走 AST，只认模块顶层的 `Assign` / `AnnAssign`。
#
# 另加两条**真不变量**（不是"恰好等于某个 revision"）：
#
# ⚠️ **口径第二次修正（避免时间炸弹 / ISS-44 形态）**：
# 初版新口径写的是 `heads == ["daea2b27212c"]` —— 它在**当前**是对的，
# 但下一个包（WP26）合法地追加一个迁移后，head 必然推进到 WP26 的 revision，
# 于是这条判据在**别人没有做错任何事**的情况下变红。
# 这就是 ISS-44/64/71/83/87 那个已现身 5 次的形态：
# **用等式断言"链上的位置"，下一个包必然失效。**
# 而且它落在 `collect_*code_review.py` 里 ⇒ 会被收口第 11 步复跑 ⇒ 每次都红。
# "永远红的判据等于没有判据"（ISS-143），最后会被人加进白名单/关掉。
#
# 改用两条**拓扑**断言（对任何后续包都恒成立）：
#   ① `len(heads) == 1` —— alembic 的硬约束（多个 head 会 `Multiple head` 直接崩）；
#      本包迁移的 `down_revision` 若被摘成 `None`，heads 立刻变 2 ⇒ 红。
#   ② `daea2b27212c` 是 head 的**祖先**（沿 down_revision 回溯可达）——
#      "我建的还在链上"。这条比 `_revision_is_at_least()` 更精确：
#      后者是"长度/字典序"近似（ISS-115 的恒真式），拓扑可达性是**事实**。
#      注意 `daea2b27212c` 自己就是 head 时也算可达（trivially）。
_ALL_MIG_REV: dict[str, object] = {}


def _mig_literal(_n):
    if isinstance(_n, ast.Constant):
        return _n.value
    try:
        return ast.literal_eval(_n)
    except Exception:
        return "<dyn>"


def _as_parents(_down) -> tuple[str, ...]:
    """把 `down_revision` 的取值归一成"父 revision 元组"（支持 tuple / list / None）。"""
    if _down is None or _down == "<dyn>":
        return ()
    if isinstance(_down, (list, tuple)):
        return tuple(str(x) for x in _down)
    return (str(_down),)


for _p in sorted((REPO_ROOT / "migrations" / "versions").glob("*.py")):
    _t = _p.read_text(encoding="utf-8")
    _rev = _down = None
    for _node in ast.parse(_t).body:
        _tgt = None
        if isinstance(_node, ast.AnnAssign) and isinstance(_node.target, ast.Name):
            _tgt, _val = _node.target.id, _node.value
        elif (
            isinstance(_node, ast.Assign)
            and len(_node.targets) == 1
            and isinstance(_node.targets[0], ast.Name)
        ):
            _tgt, _val = _node.targets[0].id, _node.value
        else:
            continue
        if _tgt == "revision":
            _rev = _mig_literal(_val)
        elif _tgt == "down_revision":
            _down = _mig_literal(_val)
    if _rev is not None:
        _ALL_MIG_REV[_rev] = _down

_referenced: set = set()
for _down in _ALL_MIG_REV.values():
    _referenced.update(_as_parents(_down))
_heads = sorted(r for r in _ALL_MIG_REV if r not in _referenced)

# ② 拓扑可达性：从每个 head 回溯，看 `daea2b27212c` 是否在祖先链上。
_ancestors_of_heads: set = set()
for _h in _heads:
    _cur: str | None = _h
    while _cur and _cur in _ALL_MIG_REV:
        _ancestors_of_heads.add(_cur)
        _ps = _as_parents(_ALL_MIG_REV[_cur])
        _cur = _ps[0] if _ps else None
_this_rev = "daea2b27212c"
_this_reachable = _this_rev in _ancestors_of_heads

emit(
    len(_heads) == 1 and _this_reachable,
    f"[C32] 迁移链单一 head（alembic 硬约束）且本包 {_this_rev} 仍在 head 的祖先链上"
    "（**拓扑判据**，不用等式断言链上位置 —— 否则下一包必然假红，ISS-44）",
    f"heads={_heads} 本包可达={_this_reachable}"
    f"（共 {len(_ALL_MIG_REV)} 个 revision，链长 {len(_ancestors_of_heads)}）",
)

# C35（第三遍 · 可维护性）：账本分录入库的 kwargs 构造在 `service.py` 里**有两处**
# —— `_insert_ledger_entry_idempotent`（单条 + SAVEPOINT，供 `append_ledger_entry`）
# 与 `_insert_ledger_entries`（批量，供 `settle_reservation`）。二者 **21 行逐字相同**。
#
# 阶段 7 的机械扫描（AST 归一化 + `SequenceMatcher`，语料 = billing 12 文件）
# 当场抓到这一处，并判定**不合并**（分析见 `evidence/m3/wp25-simplify.log` 第四节）：
#
#   · 合并需要引入一个新的"分录行结构"（把 `entry` / `currency` / `source_ref`
#     / `occurred_at` 打成一个 dataclass），收益只有 21 行 —— 而调用方
#     `append_ledger_entry` 手里本来就是**单条**，硬包一层多一次构造；
#   · **更关键**：抽成 helper 会改变两处字段校验的**求值顺序**。现在是
#     `source_ref` 先于 `description`（kwargs 书写顺序），而测试
#     `test_append_ledger_entry_source_ref_too_long_is_422` 断言的正是
#     `fieldErrors[0]["field"] == "sourceRef"` —— 两个字段**同时**非法时
#     才看得出差别，因此顺序变化在正常使用下**完全看不见**（静默错值形态）。
#
# ⚠️ 但"判定不合并"**不等于"不需要检查"**：既然承认这是复制，
# 就必须把"两份保持同步"变成**可机械检查**的事 —— 否则下一次只改一处
# 不会报红，读者会以为两处等价（ISS-95：声称有防线）。
# 本判据断言两处 insert 调用的**关键字序列完全一致**。
# 反向验证：在任一处增删/重排一个 kwarg ⇒ 本条报红（见 `evidence/m3/wp25-simplify.log` 第五节）。
def _insert_kwargs(_func_name: str) -> list[str] | None:
    _node = func_node(SD_SVC, _func_name)
    if _node is None:
        return None
    for _n in ast.walk(_node):
        if (
            isinstance(_n, ast.Call)
            and isinstance(_n.func, ast.Attribute)
            and _n.func.attr == "insert_ledger_entry"
        ):
            return [_k.arg for _k in _n.keywords]
    return None


_kw_idem = _insert_kwargs("_insert_ledger_entry_idempotent")
_kw_batch = _insert_kwargs("_insert_ledger_entries")
emit(
    _kw_idem is not None
    and _kw_idem == _kw_batch
    and len(_kw_idem) >= 8,
    "[C35] 账本分录的两处 kwargs 构造保持同步（复制是已知形态，"
    "此处把『同步』变成判据，而不是靠人记得）",
    f"idempotent={len(_kw_idem) if _kw_idem else '(未找到)'} 键 / "
    f"batch={len(_kw_batch) if _kw_batch else '(未找到)'} 键 / "
    f"一致={_kw_idem == _kw_batch}",
)

# ---------------------------------------------------------------------------
head("--- 反向验证记录（本包 CR-1 MAJOR 的实证） ---")
# ---------------------------------------------------------------------------
lines.append("本包与 WP24 最大的不同：**这条 MAJOR 不是文档声称，是实测抓到的**，")
lines.append("且做了完整的 apply → red → revert → green 四步验证。")
lines.append("")
lines.append("发现路径（静态判据没报红，是**读实现时发现 docstring 与代码不一致**）：")
lines.append("  `repository.find_ledger_entry_by_idempotency` 的 docstring 写明")
lines.append("  『直接 INSERT → 撞 23505 → 回查。**而不是**先查再插』，")
lines.append("  而 `service.append_ledger_entry` 的实现正是『先查再插』且不捕获 IntegrityError。")
lines.append("  ⇒ 这是 ISS-95 形态：**声称自己有防线**。")
lines.append("")
lines.append("实证 ① 探针（8 线程同键并发追加）：")
lines.append("  修复前：返回成功 3 ids=['d04daf03-...']（只有同一个 id）/ 抛异常 5 ['IntegrityError/23505']")
lines.append("  修复后：8/8 收敛到同一条，0 抛异常，库里恰好 1 行")
lines.append("")
lines.append("实证 ② 判据的反向验证（在**退化形态**下跑测试，必须变红）：")
lines.append("  把 `append_ledger_entry` 的调用点换回裸 `_insert_ledger_entries`（= 修复前形态）")
lines.append("  跑 `pytest -k concurrent_same_key` ⇒")
lines.append("    AssertionError: 并发同键追加有 3/8 抛异常（['23505']）")
lines.append("    `23505` 必须被当成正常并发结果回查，不能冒成 500。")
lines.append("  恢复修复后 ⇒ 105 passed / 0 failed（含该判据）。")
lines.append("")
lines.append("⚠️ 这一步不可省（判据纪律第 5 条『没跑』与『跑失败』同形）：")
lines.append("  若不退回缺陷实现，就无法证明这条判据**真的打在缺陷路径上** ——")
lines.append("  而原来的串行用例 `test_append_ledger_entry_is_idempotent_on_key`")
lines.append("  在缺陷实现下**照样绿**（它只走快路径），正是『判据没打在缺陷所在路径』的形态。")

# ---------------------------------------------------------------------------
head("--- 本轮新增的两条防线（CR 过程中产出） ---")
# ---------------------------------------------------------------------------
lines.append("① MAJOR：并发同键幂等（见上节）—— 实现修复 + 新判据 + 反向验证。")
lines.append("② MINOR：`_insert_ledger_entry_idempotent` 里的键缺失守卫原本写成 `assert`，")
lines.append("   已改为显式 `if ... is None: raise RuntimeError`。")
lines.append("   【登记】全仓另有 24 处同型 assert（audit/baseline/brief/files/gateway/")
lines.append("   jobs/knowledge/parsing/planning），其中 planning/allocation.py:364 被 WP24")
lines.append("   当作正当防线。当前部署未用 `-O` / PYTHONOPTIMIZE（全仓实测），故**当前不失效**；")
lines.append("   本包先做到零，并把『全仓 assert 防线在 -O 下失效』登记为跨包议题，")
lines.append("   不在本包单独发明新写法后沉默（否则全仓会出现两种真相）。")

# ---------------------------------------------------------------------------
head("--- 判据口径修正记录（CR-1 第二轮：4 条判据恒 FAIL 的根因） ---")
# ---------------------------------------------------------------------------
lines.append("首轮运行时本脚本报 31 PASS / 4 FAIL。逐条定位后：**4 条全部是判据口径错，")
lines.append("不是实现错**。这与 WP24 的『错误 ⑫』同族 —— 判据本身在两种实现下都报同一个结果。")
lines.append("")
lines.append("【为什么必须记下来】判据纪律：**恒 FAIL 的判据与恒 PASS 的判据一样无用**。")
lines.append("  恒 FAIL ⇒ 人会去改脚本或干脆关掉它 ⇒ 真正的回归从此无人看着（ISS-95 的近亲）。")
lines.append("  故每一条都附『根因 + 修正 + 反向验证』，而不是默默把正则改一行了事。")
lines.append("")
lines.append("① [C17] 恒 FAIL 根因：在 `ddl.py` 里正则找 `IMMUTABLE_TABLES = (...)`，")
lines.append("   但该常量**定义在 `models.py`**（ddl.py 只 import —— 反向 import 会成环）。")
lines.append("   修正：AST 取 `models.py` 的**字面量真身**，另加两条 ——")
lines.append("         ddl.py 必须 import 它（否者两份清单会各自漂移），")
lines.append("         且必须真的用它驱动触发器（值对但没人用 = 没挂触发器）。")
lines.append("   反向验证：把 models.py 改成只留 `(\"pricing_versions\",)` ⇒ C17 变红 ✓")
lines.append("")
lines.append("② [C18] 恒 FAIL 根因：`PARTIAL_UNIQUE_INDEXES` 真身是 **tuple-of-tuple**")
lines.append("   （`NAME: Final[tuple[tuple[...], ...]] = (` ），旧正则写的是 `= \\s*\\{`（dict 形态）。")
lines.append("   修正：AST 取真身 + **逐字段**比对（名 / 键列含顺序 / WHERE），")
lines.append("         另加『WHERE 真的拼进 CREATE INDEX 语句』。")
lines.append("   反向验证：把 WHERE 写成 `None` ⇒ C18 变红 ✓")
lines.append("")
lines.append("③ [C31] 恒 FAIL 根因：在**原始文本**里找 `inbound == 0`，命中的是 docstring 里")
lines.append("   **逐字写的反例说明**（『WP24 断言 inbound == 0 是对的……照抄会失败』）。")
lines.append("   判据把『讲解为什么不能这么做』当成了『真的这么做了』⇒ 必须 `sans_doc()`。")
lines.append("   修正：正向断言用 `cross_package_foreign_keys`，反向断言在**剥离 docstring 后**")
lines.append("         无 `inbound == 0`（双向：只做反向会被『整段删掉』骗过）。")
lines.append("   反向验证：注入一段真实的 `assert inbound == 0`（非 docstring）⇒ C31 变红 ✓")
lines.append("        （实测：剥离后命中 0 → 1）")
lines.append("")
lines.append("④ [C32] 恒 FAIL 根因有两层：")
lines.append("   (a) 逐行正则抓 `down_revision`，但迁移 docstring 里存在**以 `down_revision`")
lines.append("       起头的说明行**（如 `down_revision = 同 (key, ...) 最大那行 |`），被当成赋值")
lines.append("       ⇒ 解析出 9 个假 head；且 `revision: str = \"...\"` 是 **AnnAssign**，")
lines.append("       旧正则 `^revision\\s*=` 也匹配不到。")
lines.append("   (b) 修 (a) 时若改用**等式** `heads == [本包 revision]`，就会埋下时间炸弹 ——")
lines.append("       下一个包追加迁移后 head 必然推进，判据在**没人做错事**时变红")
lines.append("       （ISS-44/64/71/83/87 已现身 5 次的形态，且本文件会被收口第 11 步复跑）。")
lines.append("   修正：AST 取 revision/down_revision + **拓扑**断言（对后续任何包恒成立）：")
lines.append("         ① head 唯一（alembic 硬约束，多 head 直接 `Multiple head` 崩）；")
lines.append("         ② 本包 revision 在 head 的**祖先链**上可达（\"我建的还在\"）。")
lines.append("   反向验证：把 wp25 迁移的 down_revision 改成 None ⇒ C32 变红 ✓")
lines.append("        （实测：heads 从 1 个变 2 个 → ['022ba9804d66', 'daea2b27212c']）")
lines.append("")
lines.append("【四条口径修正的反向验证证据】/tmp/wp25_cr_caliber_reverse.log（本机留存）")
lines.append("  阶段 1 正常态 4/4 绿 · 阶段 2 四条退化各自变红且**无连带误报** ·")
lines.append("  阶段 3 恢复后回到 4/4 绿 · 阶段 4 git 零残留。")
lines.append("  ⚠️ 这一步不可省（纪律第 7 条）：改判据若不做反向验证，")
lines.append("     只是把『假红』换成『假绿』—— 那是更坏的形态，因为它看起来在保护你。")

# C33（★ CR-2 TOOLING-1）：本脚本自己**必须有**显式退出码。
#
# 这是"判据自身的判据"（元判据）。它存在的理由：本脚本在修复前**恒返回 0**，
# 于是 4 条判据报红对下游（收口第 11 步 / CI）**完全不可见** ——
# ISS-143 的变体（『判据报红』与『判据全绿』在机器可读层面同形）。
# 单独一条 README 说明"它会返回非 0"是不够的：**声称有防线 ≠ 有防线**（ISS-95），
# 故把它变成一条能当场指认的判据。
#
# ⚠️ **必须走 AST，不能用文本正则**（本脚本自己在写这条判据时踩到了同型的坑）：
# 本文件的正文里**逐字写着** `raise SystemExit(0 if verdict else 1)`（用作说明），
# 正则会把那段**说明文字**当成真实退出码 ⇒ 删掉真代码后判据**照样绿**。
# 这与 C31（docstring 里的 `inbound == 0` 反例）**是同一个坑**，
# 只是这次坑在"判据自己的文档"里 —— 所以它尤其值得记一笔：
# **写判据的人也会犯他要防的那个错。**
# AST 只认 `ast.Raise` 节点，说明文字永远不是节点。
_self_src = Path(__file__).read_text(encoding="utf-8")
_self_exit_args: list[str] = []
for _node in ast.walk(ast.parse(_self_src)):
    if not isinstance(_node, ast.Raise):
        continue
    _exc = _node.exc
    if _exc is None:
        continue
    # `raise SystemExit(...)` 与 `raise SystemExit`（无实参）都要能识别。
    _is_sys_exit = (
        isinstance(_exc, ast.Call)
        and isinstance(_exc.func, ast.Name)
        and _exc.func.id == "SystemExit"
    ) or (isinstance(_exc, ast.Name) and _exc.id == "SystemExit")
    if not _is_sys_exit:
        continue
    if isinstance(_exc, ast.Call) and _exc.args:
        _self_exit_args.append(ast.unparse(_exc.args[0]))
    else:
        _self_exit_args.append("(无实参)")
# 只认**非裸 0** 的退出码：裸 `SystemExit(0)` 与"没有退出码"行为等价。
_self_has_exit = any(a.strip() not in ("0", "(无实参)") for a in _self_exit_args)
emit(
    _self_has_exit,
    "[C33/CR-2 TOOLING-1] 累积 `checks` 的脚本必须有显式退出码"
    "（本脚本自查：`raise SystemExit(0 if _verdict else 1)`；AST 判定）",
    f"退出码实参={_self_exit_args or '（AST 未找到任何 SystemExit）'}",
)

# ---------------------------------------------------------------------------
head("--- 第三遍补充：环境相关的用例缺陷（CR-3） ---")
# ---------------------------------------------------------------------------

lines.append("本节记录一条**不是实现缺陷、而是用例缺陷**的发现。")
lines.append("它在前两轮里都没暴露，暴露路径是『换一个**由迁移建出**的库』。")
lines.append("")
lines.append("【现象】`test_estimate_cost_without_limits_configured_is_422` 在")
lines.append("  `rednote_test`（夹具建表）上绿，在迁移建出的库上 **DID NOT RAISE**。")
lines.append("")
lines.append("【根因】该用例用『另一个项目不写项目级配置』冒充『未配置上限』。")
lines.append("  但 `GLOBAL batch.limits` 是 **WP24 迁移 `022ba9804d66` 主动种下的**")
lines.append("  （其 docstring §五：调 `bootstrap_defaults` 写 ADR-13 初始行）。")
lines.append("  `get_effective_limits` 对『全局有 + 项目无』返回 `source='GLOBAL'`")
lines.append("  ⇒ 项目**继承**默认值 ⇒ `as_limits()` 成功 ⇒ 不抛 422。")
lines.append("")
lines.append("  ⇒ 通过条件是『**这个库没跑过 WP24+ 的迁移**』——")
lines.append("     而『测试库应由迁移建出』恰恰是本项目的标准做法。")
lines.append("     **判决：用例把库的历史当成了前提，实现（继承全局默认值）是 ADR-13 要的。**")
lines.append("")
lines.append("【修法】不删用例、不换库，而是**把前提显式化**：")
lines.append("  `monkeypatch.setattr(settings_service, 'get_effective_limits', …)`")
lines.append("  返回 `configured=False` 的 `EffectiveSetting`，直接构造出")
lines.append("  『全局、项目都没有』的语义 ⇒ 与库怎么建出的**无关**。")
lines.append("")
lines.append("⚠️ `service.estimate_cost` 对该函数用的是**函数内局部 import**")
lines.append("  （避开 billing ← planning 的模块级依赖）。patch 模块属性照样生效，")
lines.append("  但这一点必须**实测**而不是推断 —— 故下面两条判据 + 反向验证一起给。")

# C34：修法确实落地（打桩），而不是"换个项目"的老写法。
_self_tests_src = TESTS.read_text(encoding="utf-8")
_test_tree = ast.parse(_self_tests_src)
_limits_fn = None
for _node in _test_tree.body:
    if (
        isinstance(_node, ast.FunctionDef)
        and _node.name == "test_estimate_cost_without_limits_configured_is_422"
    ):
        _limits_fn = _node
        break
_fn_src = ast.get_source_segment(_self_tests_src, _limits_fn) if _limits_fn else ""
_patched = "get_effective_limits" in (_fn_src or "") and "monkeypatch" in (_fn_src or "")
_unconfigured = "configured=False" in (_fn_src or "") or "configured=False" in (
    _fn_src or ""
).replace(" ", "")
emit(
    _limits_fn is not None and _patched and _unconfigured,
    "[C34/CR-3] `without_limits_configured` 用例**打桩**制造『未配置』"
    "（不再用『另一个项目』冒充 —— 那在迁移建出的库上会被全局默认值兜住）",
    f"函数存在={_limits_fn is not None} 含 monkeypatch={_patched} "
    f"构造 configured=False={_unconfigured}",
)

# C34b：迁移**确实**种了全局行（坐实上面的根因，不靠文档声称）。
#
# ⚠️ 注意：种下 `GLOBAL batch.limits` 的是 **WP24** 的迁移（`022ba9804d66`），
# 不是本包的 `daea2b27212c`。本判据第一版就写错成了 `MIGRATION`
# （那是 WP25 的迁移）⇒ 恒 FAIL。**这正是"判据自己也会犯它要防的错"**：
# 断言"迁移里有 X"时，必须确保引用的是**那个**迁移。
_WP24_MIG = sorted(
    (REPO_ROOT / "migrations" / "versions").glob("*022ba9804d66*.py")
)
_global_seeded = False
_global_how = ""
if _WP24_MIG:
    _mig_src = _WP24_MIG[0].read_text(encoding="utf-8")
    _global_seeded = "bootstrap_defaults" in _mig_src
    _global_how = _WP24_MIG[0].name
emit(
    _global_seeded,
    "[C34b/CR-3] 根因坐实：**WP24** 迁移 `022ba9804d66` 调 `bootstrap_defaults`"
    " 种下 `GLOBAL batch.limits`（在**那个**迁移源码里指认）",
    f"迁移={_global_how or '未找到'} 含 bootstrap_defaults={_global_seeded}"
    f"（本包迁移={MIGRATION.name[:12] if MIGRATION else '?'}，**不含**）",
)

# C34c：修后必须**仍有区分力** —— 断言用例体内存在"会被兜底击穿"的断言。
# 只证明"打了桩"不够：打桩若把用例变成恒绿，比原来的环境依赖更坏。
_has_reason_assert = 'reason"] == "LIMITS_NOT_CONFIGURED"' in (_fn_src or "") or (
    "LIMITS_NOT_CONFIGURED" in (_fn_src or "")
)
emit(
    _has_reason_assert,
    "[C34c/CR-3] 修后仍断言**具体原因**（`LIMITS_NOT_CONFIGURED`），"
    "不是改成『抛任何错就算过』（区分力由 §反向验证 的 apply→red→revert→green 证明）",
    f"含 LIMITS_NOT_CONFIGURED 断言={_has_reason_assert}",
)

# ---------------------------------------------------------------------------
head("--- 判据红了之后会发生什么（CR-2：本脚本自身的防御缺陷） ---")
# ---------------------------------------------------------------------------
lines.append("首轮运行时本脚本报 `31 PASS / 4 FAIL`，而 `echo $?` 是 **0**。")
lines.append("")
lines.append("这不是笔误，是本脚本**曾经**的真实形态：")
lines.append("")
lines.append("  · 结尾只有 `write_text(...)` + `print(...)` —— **没有任何退出码语句**；")
lines.append("  · 也没有 `EXPECTED_CHECKS`，也没有 `RESULT=` 行。")
lines.append("")
lines.append("⇒ 它在**任何**输入下都返回 0。后果链：")
lines.append("")
lines.append("  ① `tools/m1/rerun_all_evidence.py`（收口第 11 步的载体）对每个脚本只断言两件事：")
lines.append("     `rc == 0` 与『证据文件 mtime 前进』—— 两条**都会 PASS**；")
lines.append("  ② 它虽然会把 `RESULT` 列进表格，但那列**只打印、不参与任何断言**；")
lines.append("  ③ 于是：**判据红了，没有任何机械后果。**")
lines.append("")
lines.append("这正是 ISS-143 的变体（『没跑』与『跑失败』同形）：")
lines.append("**『判据报红』与『判据全绿』在机器可读层面同形。**")
lines.append("它比 4 条口径错更危险 —— 口径错只是局部失真，")
lines.append("而这里整份证据的『红/绿』对下游完全不可见（ISS-95：声称有防线）。")
lines.append("")
lines.append("【修正】补三件：`EXPECTED_CHECKS` + `RESULT=` 行（写出期望条数与条数核对）")
lines.append("         + `raise SystemExit(0 if verdict else 1)`。")
lines.append("【防复发】`tools/m1/check_evidence_scripts.py` 新增第五条检查")
lines.append("         `check_returns_exit_code` —— 凡累积 `checks` 的脚本必须显式给退出码，")
lines.append("         且**不得**是裸 `SystemExit(0)`。它的反向验证见")
lines.append("         `evidence/m3/wp25-code-review-reverse.log` 的 C33 用例。")
lines.append("")
lines.append("⚠️ 同款缺陷也在 `tools/m3/collect_wp24_code_review.py`（已随 `afdd91f` 提交）——")
lines.append("   它只因为『恰好 0 FAIL』而从未暴露。两处一并修；")
lines.append("   『全仓还有几个这样的脚本』由上面那条门禁**机械回答**，不靠人回忆。")

# ---------------------------------------------------------------------------
head("--- 结论 ---")
# ---------------------------------------------------------------------------
lines.append("本遍审查：CRITICAL 0 / MAJOR 1 / MINOR 1 / 判据口径修正 4 / 工具链缺陷 1 / 用例缺陷 1"
             " / 阶段 7 保留 1（附同步判据）")
lines.append("  MAJOR-1：并发同键幂等缺陷（实测抓到，已修 + 已加判据 + 已反向验证）")
lines.append("  MINOR-1：指纹函数 docstring 声称的『第二道判据』实际未接线（已如实标注）")
lines.append("  CALIBER-4：CR 收集器自身 4 条判据恒 FAIL（C17/C18/C31/C32），已修正并反向验证")
lines.append("  TOOLING-1：CR 收集器恒返回退出码 0 ⇒ 判据报红无机械后果（本文件即其证据载体）")
lines.append("  TESTS-1（CR-3）：一条用例把『库没跑过 WP24+ 迁移』当成了通过前提 ——")
lines.append("      在迁移建出的库上必红。已改为**打桩制造未配置**，并做了区分力反向验证。")
lines.append("  DUPL-1（第三遍）：两处 21 行逐字相同的 kwargs 构造（idempotent / batch）——")
lines.append("      判定**保留不合并**（合并会改变 `sourceRef` 与 `description` 的校验先后），")
lines.append("      但把『两份保持同步』升级成机械判据 [C35]（反向验证见 `evidence/m3/wp25-simplify.log` 第五节）。")
lines.append("")
lines.append("三遍法各自的落点：")
lines.append("  第一遍 正确性/安全：裸 except / assert / 可变默认参 / 事务泄漏 / SQL 插值 / 量化")
lines.append("  第二遍 并发/事务：幂等原语三要素 / 唯一冲突判定 / 三条条件 UPDATE / 无 N+1 / ON CONFLICT")
lines.append("  第三遍 边界/加固：不可变表清单 / 部分索引 WHERE / 恒真式的反向说明 / 冲正不过滤 / 分层")
lines.append("  第三遍补充（CR-3）：**换一个更接近生产的库**跑同一份测试 ——")
lines.append("      环境差异暴露出的用例缺陷，静态审查与单库运行都看不见。")
lines.append("")
lines.append("测试侧：services/backend/tests/test_billing_wp25.py（四层范式，用例数见 C29）")
lines.append("证据侧：evidence/m3/wp25-code-review.log（本文件）")

lines.append("")
_passed = sum(1 for c in checks if c)
_failed = sum(1 for c in checks if not c)
lines.append(f"机械判据：{_passed} PASS / {_failed} FAIL / {len(checks)} checks")
lines.append(f"MIGRATION={MIGRATION.name if MIGRATION else '(未找到)'}")
lines.append(f"TESTS_EXISTS={TESTS.exists()}")
lines.append(f"TESTS_CASES={_n_tests}")

# ---------------------------------------------------------------------------
# 条数核对 + 退出码（CR-2 TOOLING-1：此前这三样**都没有**）
#
# 为什么必须有 `EXPECTED_CHECKS`：只统计"失败了几条"时，
# "脚本崩溃、本该产生的 CHECK 一条都没产生"与"全部通过"在计数上完全一样
# （ISS-43）。故必须**另有一个与实测无关的期望值**来对账。
#
# 为什么必须 `>=` 之外再加 `==`（ISS-153）：`>=` 拦不住"判据变多了而常量没跟着涨"，
# 于是新加的判据可以**悄悄不生效**。相等判据逼着人回来改常量。
# ---------------------------------------------------------------------------
EXPECTED_CHECKS = 40
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
out = EVIDENCE_DIR / "wp25-code-review.log"
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
print(f"\n写入 {out}")

# ⚠️ **这一行是 CR-2 TOOLING-1 的修复本体**：没有它，上面所有 CHECK 的红/绿
# 对下游（收口第 11 步 / CI）**完全不可见** —— 脚本恒返回 0。
# 由 `tools/m1/check_evidence_scripts.py::check_returns_exit_code` 守着不复发。
raise SystemExit(0 if _verdict else 1)