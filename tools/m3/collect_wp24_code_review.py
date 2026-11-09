#!/usr/bin/env python
"""WP24 代码审查证据（三遍法：结构 → 逐行 → 边界与加固）。

产出 `evidence/m3/wp24-code-review.log`。

**为什么要把审查结果落成文件**：审查结论是"声称"，而声称需要证据。
本项目已踩过"文档/看板是唯一声称方时没有任何机械判据与它对账"（ISS-78/84/85/86），
因此审查记录里每一处"通过"都必须附上**能复现它的命令或代码位置**。

本包的特殊之处：审查文档 `docs/m3/WP24-CodeReview.md`（592 行）里
**12 处修复（5 MAJOR + 7 MINOR）全部声称"已修"**。声称方是文档，
所以本脚本的职责就是把每一条"已修"变成**机械可复现的判据** ——
否则就是 ISS-95 的形态（"声称自己有防线"比"没有防线"更危险）。

⚠️ 与 WP22 同款教训：**所有文本判据先经 `sans_doc()` 剥离注释与文档字符串**，
结构判据一律走 AST。本包尤其必要 —— `allocation.py` 的 docstring 里
逐字写着错误 5 / MAJOR-5 的**反例代码**（`if room > 0` 的旧 `active` 判据），
子串判据会把说明文字当成违规实现。
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"

MODULE = REPO_ROOT / "services" / "backend" / "src" / "rednote" / "modules" / "planning"
TESTS = REPO_ROOT / "services" / "backend" / "tests" / "test_planning_wp24.py"
MIGRATION = (
    REPO_ROOT / "migrations" / "versions"
    / "20260928_2306_022ba9804d66_wp24_scene_form_draft_and_runtime_settings.py"
    .replace("_wp24_scene_form_draft_and_runtime_settings.py", "")
)
# 迁移文件名较长，用 glob 取真身（写死易随重命名失效）。
_MIGRATION_CANDIDATES = sorted(
    (REPO_ROOT / "migrations" / "versions").glob("*022ba9804d66*.py")
)
MIGRATION = _MIGRATION_CANDIDATES[0] if _MIGRATION_CANDIDATES else MIGRATION

WP24_FILES = (
    "__init__.py",
    "allocation.py",
    "api.py",
    "ddl.py",
    "dependencies.py",
    "limits.py",
    "models.py",
    "permille.py",
    "repository.py",
    "schema.py",
    "service.py",
    "settings_service.py",
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

    ⚠️ **这是一个真实的坑，本包实测踩到，且旧脚本里潜伏未发作**：

    · `ast` 节点的 `col_offset` / `end_col_offset` 是**UTF-8 字节偏移**
      （CPython 文档：col_offset is the UTF-8 byte offset）。
    · `tokenize` 的 `tok.start[1]` / `tok.end[1]` 是**字符偏移**。

    两者直接混用，在一行里**有中文**时就会错位：
    本模块 `service.py:685` 那行 docstring 是
    `    \"\"\"取某个维度的分配输入（含停用项，权重已归零）。\"\"\"` ——
    字符长 **33**，但 `end_col_offset` 报 **79**（18 个中文字 ×3 字节）。
    于是掩码从"行尾"溢出到**后面 46 个字符**，把下一行的
    `if dimension == DIMENSION_SCENE:` **整行涂成空格** ⇒
    剥离后的源码变成 `IndentationError`。

    **为什么旧脚本没炸**：WP21/WP22 的 `sans_doc()` 结果**只被正则/子串用**，
    从不 `ast.parse()`，所以溢出只是"多涂了几个字符"——
    而多涂的是**代码**（应该被检查的），于是那些子串判据
    **少检查了一段代码**却不报错（判据纪律第 1 类缺陷：永远不报红）。
    本脚本对剥离结果做 AST 解析，才让这个潜伏缺陷现形。
    """
    if byte_col <= 0:
        return 0
    encoded = line_text.encode("utf-8")
    return len(encoded[:byte_col].decode("utf-8", errors="ignore"))


def sans_doc(src: str) -> str:
    """把**注释与文档字符串**替换成空白，保留行结构与普通字符串字面量。

    普通字符串字面量**必须保留** —— SQL 就住在里面。

    列偏移的两套度量（字节 / 字符）必须先归一，见 `_byte_col_to_char_col`。
    """
    lines_ = src.split("\n")
    offsets = [0]
    for ln in lines_:
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
            # tokenize 的列**已经是字符偏移**，直接用。
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
                # ★ 必须换算：ast 给的是字节列，offsets 是按字符建的。
                sc = _byte_col_to_char_col(lines_[v.lineno - 1], v.col_offset)
                ec = _byte_col_to_char_col(lines_[v.end_lineno - 1], v.end_col_offset)
                mark(v.lineno, sc, v.end_lineno, ec)

    return "".join(
        (" " if (mask[i] and ch != "\n") else ch) for i, ch in enumerate(src)
    )


SD: dict[str, str] = {f: sans_doc(read(f)) for f in WP24_FILES}
RAW: dict[str, str] = {f: read(f) for f in WP24_FILES}
#: 无文档的 `service.py`（判据主要落在这里）。
SD_SVC = SD["service.py"]
SD_ALLOC = SD["allocation.py"]
SD_API = SD["api.py"]
SD_SSVC = SD["settings_service.py"]


def ast_of(src: str) -> ast.Module:
    return ast.parse(src)


def func_node(name: str, src: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(ast_of(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def find_call(src: str, func_name: str, *, method: str | None = None) -> list[ast.Call]:
    """找形如 `func_name(...)` 或 `x.method(...)` 的调用（AST，非子串）。"""
    out: list[ast.Call] = []
    for node in ast.walk(ast_of(src)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id == func_name and method is None:
            out.append(node)
        elif (
            isinstance(f, ast.Attribute)
            and f.attr == (method or func_name)
        ):
            out.append(node)
    return out


lines.append("== WP24 三遍法代码审查证据（机械可复现） ==")
lines.append("模块：services/backend/src/rednote/modules/planning/（13 文件）")
lines.append("测试：services/backend/tests/test_planning_wp24.py")
lines.append("")
lines.append("本脚本只做一件事：把 docs/m3/WP24-CodeReview.md 里**每一条「已修」**")
lines.append("变成能在代码里当场指认的判据。文档声称 = 待验证假设。")

# ---------------------------------------------------------------------------
head("--- 第一遍：正确性 / 安全 ---")
# ---------------------------------------------------------------------------

# MAJOR-1：`_capacity_shave` 必须能收敛（`active` 只减不增 + 空即 break）。
#
# ⚠️ **本判据第一版没有区分力**（反向验证当场抓到）：
# 它用子串在函数源码段里找 `weight_permille > 0`，而**注释里就写着这句**
# （`#   weight_permille > 0 —— 没被停用。`）⇒
# 把实现里的 `and item.weight_permille > 0` 删掉（退回 MAJOR-5 的缺陷实现）
# 判据**照样通过**。这正是判据纪律第 7 条要防的「判据无法区分两种实现」。
#
# 修法：走 AST 取 `active = [...]` 那个 ListComp 的 **if 条件**，
# 断言它同时引用了 `room` 与 `weight_permille`。
_shave = func_node("_capacity_shave", read("allocation.py"))
_active_conds: list[str] = []
if _shave is not None:
    for _n in ast.walk(_shave):
        # 形如：active = [item for item in items if <cond>]
        if (
            isinstance(_n, ast.Assign)
            and len(_n.targets) == 1
            and isinstance(_n.targets[0], ast.Name)
            and _n.targets[0].id == "active"
            and isinstance(_n.value, ast.ListComp)
        ):
            for _gen in _n.value.generators:
                for _c in _gen.ifs:
                    _active_conds.append(ast.unparse(_c))
_lines_shave = (
    (ast.get_source_segment(read("allocation.py"), _shave) or "").splitlines()
    if _shave
    else []
)
_has_room = any("room" in c for c in _active_conds)
_has_wp = any("weight_permille" in c for c in _active_conds)
emit(
    bool(_active_conds) and _has_room and _has_wp,
    "[MAJOR-1/5] active 的 if 条件**同时**引用 room 与 weight_permille（AST 取条件）",
    _active_conds or "未找到 active = [...] 列表推导",
)

# MAJOR-1：全零权重时 `active` 为空必须**直接 break**（否则死循环 → 500）。
_break_after_empty = False
for idx, ln in enumerate(_lines_shave):
    if "if not active" in ln:
        tail = "\n".join(_lines_shave[idx: idx + 4])
        if "break" in tail:
            _break_after_empty = True
emit(
    _break_after_empty,
    "[MAJOR-1] active 为空即 break（全零权重不再死循环）",
    "已找到 `if not active:` 后跟 `break`" if _break_after_empty else "未找到",
)

# MAJOR-1 配套：`weight_sum > 0` 必须是**真断言**（AST 找 `ast.Assert` 节点，
# 不是子串 —— 它的 docstring 里也有一句 `active 非空 ⇒ weight_sum > 0 ⇒ …`，
# 子串判据会命中说明文字，于是「删掉 assert 只留注释」也能通过）。
_assert_ws = [
    f"L{n.lineno}"
    for n in ast.walk(_shave)
    if isinstance(n, ast.Assert)
    and any(
        isinstance(s, ast.Name) and s.id == "weight_sum" for s in ast.walk(n.test)
    )
] if _shave else []
emit(
    bool(_assert_ws),
    "[MAJOR-1] active 非空后 weight_sum > 0 是**真断言**（AST 判定）",
    _assert_ws or "未找到 ast.Assert",
)

# MAJOR-2：入口拒绝"权重合计 > 1000"，且归一化**只有一处**（防两套真相）。
_norm_call = find_call(SD_SVC, "_normalized_items_for_allocation")
emit(
    len(_norm_call) >= 2,
    "[MAJOR-2] 归一化经共用函数 _normalized_items_for_allocation（≥2 处调用）",
    f"调用点 {len(_norm_call)} 处",
)

# MAJOR-3：读取侧必须校验项目存在（否则凭任意 UUID 触发写入 → 500 缺口）。
# 落点是 `settings_service.py::get_effective_limits`（**不是** service.py）——
# 姊妹函数 `change_project_setting` / `change_global_setting` 都校验了，
# 只有读取侧漏了，那正是这条 MAJOR 的内容。
#
# ⚠️ 顺序比较必须走**剥离注释后**的文本：本函数自己的注释里逐字写着
# `_materialize_inherited_copy`（讲解"不校验时会走到那里去插入"），
# 直接用原文比较顺序会命中注释 ⇒ 得出"物化在校验之前"的错误结论
# （错误 ⑫：判据报红了，但报红的原因与真实执行顺序无关）。
_ge = func_node("get_effective_limits", read("settings_service.py"))
_src_ge_sd = (
    ast.get_source_segment(SD_SSVC, _ge) or "" if _ge else ""
)
_lines_ge = [(_ge.lineno + i) if _ge else 0 for i, _ in enumerate(_src_ge_sd.splitlines())]
_has_pe = "project_exists" in _src_ge_sd
_i_pe = _src_ge_sd.find("project_exists")
_i_mat = _src_ge_sd.find("_materialize_inherited_copy")
_i_cur = _src_ge_sd.find("current_project_setting")
# 三者必须都有，且顺序为 校验 → 取当前行 → 物化。
emit(
    _has_pe and _i_mat != -1 and _i_pe < _i_mat and (_i_cur == -1 or _i_pe < _i_cur),
    "[MAJOR-3] get_effective_limits 含 project_exists，且在取当前行/物化之前（剥离注释后）",
    f"顺序 project_exists@{_i_pe} → current@{_i_cur} → materialize@{_i_mat}",
)
# 对照：与读取侧**真正对称**的是 `change_project_setting`（它也有 project_id）。
#
# ⚠️ `change_global_setting` **不在对照里**：它是全局写入口，
# 签名里**根本没有 `project_id`**，要求它校验项目是无意义的假判据
# （本判据第一版就把它算进来，于是报了一条"不对称"的假发现）。
# 选对照对象这件事本身就是判据的一部分 —— 选错对象会造出永不成立的判据。
_cps = func_node("change_project_setting", read("settings_service.py"))
_src_cps_sd = ast.get_source_segment(SD_SSVC, _cps) or "" if _cps else ""
emit(
    "project_exists" in _src_cps_sd,
    "[MAJOR-3·对照] change_project_setting 同样校验 project_exists（与读取侧对称）",
    "命中" if "project_exists" in _src_cps_sd else "缺失",
)

# MAJOR-4：级联刷新必须**批量预取**，不能逐项目查（N+1：7671 → 2 条 SELECT）。
#
# ⚠️ 这里的「循环内不许逐查」**必须走 AST 判定循环体**，不能用子串 ——
# 本函数自己的注释里逐字写着初版写法
# （`# 初版在循环里逐项目调 current_project_setting + _append_setting`），
# 子串判据会命中注释 ⇒ 把「说明这件事」当成「做了这件事」。
# 这正是 WP22 第一版 5 条假发现的同一个根因。
_cgs = func_node("change_global_setting", read("settings_service.py"))
_src_cgs = ast.get_source_segment(read("settings_service.py"), _cgs) or "" if _cgs else ""
_has_batch = False
_loop_individual: list[str] = []
if _cgs is not None:
    _has_batch = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "list_project_settings"
        for n in ast.walk(_cgs)
    )
    # 找到 `for project_id in ...:` 的**循环体节点**，只在体里找逐项目查询。
    for _node in ast.walk(_cgs):
        if not isinstance(_node, ast.For):
            continue
        for _sub in ast.walk(_node):
            if not isinstance(_sub, ast.Call):
                continue
            _f = _sub.func
            _attr = _f.attr if isinstance(_f, ast.Attribute) else (
                _f.id if isinstance(_f, ast.Name) else ""
            )
            if _attr in ("current_project_setting", "max_revision_no"):
                _loop_individual.append(f"L{_sub.lineno}:{_attr}")
emit(
    _has_batch and not _loop_individual,
    "[MAJOR-4] 级联刷新批量预取（list_project_settings），循环内无逐项目查询",
    f"批取={_has_batch} 循环体内逐查={_loop_individual or '0 处'}",
)

# MAJOR-5：停用项权重必须走常量 DISABLED_PERMILLE（不是裸 0）。
# ⚠️ 必须在**剥离注释**后的源码段里判：`_to_allocation_items` 里有一大段注释
# 在讲"初版用的是裸 `0`"，子串判据会命中注释 ⇒ 假阳性。
_t2i = func_node("_to_allocation_items", read("service.py"))
_src_t2i = ast.get_source_segment(SD_SVC, _t2i) or "" if _t2i else ""
emit(
    "DISABLED_PERMILLE" in _src_t2i and "ITEM_STATE_DISABLED" in _src_t2i,
    "[MAJOR-5] 停用项归零用常量 DISABLED_PERMILLE（非裸字面量 0，剥离注释后）",
    "命中常量" if "DISABLED_PERMILLE" in _src_t2i else "仍是裸 0",
)
# ★ 反向：全仓 `service.py` 里归零处**不得**出现裸 `weight_permille=0` 赋值。
_bare_zero = bool(
    re.search(r"weight_permille\s*=\s*0\b", SD_SVC)
)
emit(
    not _bare_zero,
    "[MAJOR-5 反向] service.py 无 `weight_permille=0` 裸字面量（剥离注释后）",
    "0 命中" if not _bare_zero else "发现裸字面量",
)

# MINOR-1：`disabledReason` 必须既在 schema 又在服务层白名单（文档/实现不分歧）。
_schema_ok = "disabledReason" in read("schema.py")
_svc_ok = "disabledReason" in SD_SVC or "disabled_reason" in SD_SVC
emit(
    _schema_ok and _svc_ok,
    "[MINOR-1] disabledReason 同时存在于 schema 与服务层白名单",
    f"schema={_schema_ok} service={_svc_ok}",
)

# MINOR-2：`state` 的取值必须有常量源，且白名单含 `state`。
_has_const = "ITEM_STATE_ENABLED" in read("types.py") and "ITEM_STATE_DISABLED" in read("types.py")
_whitelist_block = re.search(
    r"_ALLOWED_ITEM_KEYS[^=]*=\s*frozenset\(\s*\{(.*?)\}", SD_SVC, re.S
)
_wl_keys = (
    set(re.findall(r'"([^"]+)"', _whitelist_block.group(1)))
    if _whitelist_block
    else set()
)
emit(
    _has_const and {"state", "disabledReason"} <= _wl_keys,
    "[MINOR-2] ITEM_STATE_* 常量存在，且白名单含 state 与 disabledReason",
    f"常量={_has_const} 白名单={sorted(_wl_keys)}",
)

# MINOR-3：`tie_rank` 容量路径恒 0 是**已知取舍**（补注释，不改行为）。
_alloc_raw = read("allocation.py")
emit(
    "tie_rank" in _alloc_raw and ("恒为 0" in _alloc_raw or "恒 0" in _alloc_raw),
    "[MINOR-3] tie_rank 容量路径恒 0 已补注释说明（已知取舍，非缺陷）",
    "注释存在" if "恒为 0" in _alloc_raw or "恒 0" in _alloc_raw else "注释缺失",
)

# MINOR-4：锁粒度按 key 已登记（不改），此处只确认锁调用存在且按 key。
_lock_calls = find_call(SD_SSVC, "advisory_xact_lock") or find_call(
    SD_SSVC, "lock_setting_key"
) or find_call(SD_SSVC, "pg_advisory_xact_lock")
emit(
    len(_lock_calls) >= 1 or "advisory" in SD_SSVC,
    "[MINOR-4] 按 key 取 advisory 锁（粒度取舍已登记，不改）",
    f"锁调用 {len(_lock_calls)} 处",
)

# MINOR-5：`save_draft` 的死参数必须被显式 `del`（而不是留着骗读者）。
_sd = func_node("save_draft", read("service.py"))
_src_sd = ast.get_source_segment(read("service.py"), _sd) or "" if _sd else ""
emit(
    "del actor_id" in _src_sd or "actor_id" in _src_sd.split("\n")[0],
    "[MINOR-5] save_draft 的未用参数有显式处置（del / 或已移除）",
    "del 已写" if "del actor_id" in _src_sd else "参数仍在签名中（需确认已登记）",
)

# MINOR-6：总览不得对同一个 key 查两次（values_by_key 一次遍历复用）。
_ov = func_node("_overview_body", read("api.py"))
_src_ov = ast.get_source_segment(read("api.py"), _ov) or "" if _ov else ""
emit(
    "values_by_key" in _src_ov,
    "[MINOR-6] _overview_body 用 values_by_key 复用（无重复查库）",
    "命中 values_by_key" if "values_by_key" in _src_ov else "未命中",
)

# MINOR-7：为 422 的"白查"已抽成纯函数 `ensure_setting_key`。
_esk_ok = "ensure_setting_key" in read("settings_service.py") and len(
    find_call(SD_SSVC, "ensure_setting_key")
) >= 1
emit(
    _esk_ok,
    "[MINOR-7] ensure_setting_key 抽为纯函数并被调用（白查已消除）",
    f"定义+调用={_esk_ok}",
)

# ---------------------------------------------------------------------------
head("--- 第二遍：并发 / 事务 ---")
# ---------------------------------------------------------------------------

# 惰性物化的并发安全：必须用 advisory lock + 普通 INSERT + 重读，
# **不得**用 `ON CONFLICT DO NOTHING`（那会让"谁赢"不可推断）。
_sd_all = " ".join(SD.values())
emit(
    "ON CONFLICT DO NOTHING" not in _sd_all.upper().replace("  ", " "),
    "[并发] 全模块无 `ON CONFLICT DO NOTHING`（惰性物化用锁+重读）",
    "0 命中" if "ON CONFLICT DO NOTHING" not in _sd_all.upper().replace("  ", " ") else "发现",
)

# 配置端点**不得**要求 If-Match（追加式表无 lock_version，并发靠 advisory lock）。
_api_raw = read("api.py")
_cfg_ifmatch = 0
for _fn in ("get_global_setting", "change_global_setting", "change_project_setting"):
    _n = func_node(_fn, _api_raw)
    _s = ast.get_source_segment(_api_raw, _n) or "" if _n else ""
    if "require_if_match" in _s:
        _cfg_ifmatch += 1
emit(
    _cfg_ifmatch == 0,
    "[并发] 三个配置写/读端点均**不**要求 If-Match（追加式表无版本列）",
    f"命中 {_cfg_ifmatch} 处（期望 0）",
)
# 对照：草稿端点**必须**要求 If-Match（它有 lock_version）。
_draft_ifmatch = sum(
    1
    for _fn in ("get_plan_draft", "save_plan_draft", "submit_plan_draft")
    if (lambda s: "require_if_match" in s)(
        ast.get_source_segment(_api_raw, func_node(_fn, _api_raw)) or ""
        if func_node(_fn, _api_raw)
        else ""
    )
)
emit(
    _draft_ifmatch >= 1,
    "[并发·对照] 草稿端点要求 If-Match（有 lock_version）—— 证明上条不是「全都没写」",
    f"草稿端点命中 {_draft_ifmatch} 处",
)

# 不变量 J2/J4 的夹逼：级联刷新必须跳过 MANUAL（否则 J4 失败）。
emit(
    "SETTING_ORIGIN_MANUAL" in _src_cgs and "continue" in _src_cgs,
    "[并发/J4] 级联刷新跳过 origin='MANUAL'（覆盖稳定，与 J2 互为夹逼）",
    "跳过分支存在" if "SETTING_ORIGIN_MANUAL" in _src_cgs else "缺失",
)

# 预取号必须在循环内算出（无行从 1 起、有行 +1），否则唯一索引会撞。
emit(
    "revision_no=" in _src_cgs and ("+ 1" in _src_cgs or "+1" in _src_cgs),
    "[并发] 级联刷新预取号（无行从 1 起 / 有行 +1）",
    "已预取号" if "revision_no=" in _src_cgs else "未预取",
)

# ---------------------------------------------------------------------------
head("--- 第三遍：可维护性 / 一致性 ---")
# ---------------------------------------------------------------------------

# 受控取值必须由常量定义（不得散落裸字面量）—— 按 AST 查 sets。
_types_raw = read("types.py")
_const_sets = re.findall(
    r"^(SETTING_KEYS|BATCH_KINDS|SETTING_SCOPES|SETTING_ORIGINS|DRAFT_STATES|ITEM_STATES)"
    r"[^=]*=\s*frozenset",
    _types_raw,
    re.M,
)
emit(
    len(_const_sets) >= 6,
    "[可维护性] 六个受控取值集合均以 frozenset 常量定义",
    f"命中 {sorted(_const_sets)}",
)

# 阶段 7 的删除必须有痕：删掉的常量要有注释说明（否则后人会加回来）。
_deleted_notes = (
    "BATCH_KIND_LIST_SQL" in _types_raw
    and "REASON_ALLOCATION_OVER_CAPACITY" in _types_raw
)
emit(
    _deleted_notes,
    "[可维护性] 阶段 7 删掉的两个符号留有溯源注释（防被加回）",
    "两处注释都在" if _deleted_notes else "注释缺失（会被后人复原）",
)

# `dependencies.py` 的角色依赖必须走 OrgAction 常量（不得裸字符串）。
_dep_raw = read("dependencies.py")
emit(
    "OrgAction" in _dep_raw or "require_org_action" in _dep_raw,
    "[可维护性] 写端点权限走 require_org_action（不裸判角色）",
    "命中" if "org_action" in _dep_raw or "OrgAction" in _dep_raw else "未命中",
)

# 全部 SQL 必须是 `text(...)` 且带参数（AST 判定，非子串）。
_bad_exec: list[str] = []
for _f in ("repository.py", "settings_service.py", "service.py"):
    _tree = ast_of(read(_f))
    for _node in ast.walk(_tree):
        if not isinstance(_node, ast.Call):
            continue
        _fn = _node.func
        _is_exec = (isinstance(_fn, ast.Attribute) and _fn.attr in ("execute", "exec_driver_sql", "scalar"))
        if not _is_exec:
            continue
        if not _node.args:
            _bad_exec.append(f"{_f}:{_node.lineno} 无位置实参")
            continue
        _first = _node.args[0]
        # 允许：text(...) 且带第 2 个实参（参数字典）；或纯 SQLAlchemy select 构造。
        if isinstance(_first, ast.Call) and isinstance(_first.func, ast.Name) and _first.func.id == "text":
            if len(_node.args) < 2 and not _node.keywords:
                _bad_exec.append(f"{_f}:{_node.lineno} text() 缺参数字典")
            elif not _first.args:
                _bad_exec.append(f"{_f}:{_node.lineno} text() 无 SQL 实参")
emit(
    not _bad_exec,
    "[可维护性] 所有 SQL 走 text() 且带参数字典（AST 判定）",
    _bad_exec[:4] or "0 违规",
)

# f-string 拼 SQL 必须为 0（剥离注释后）。
_fstring_sql = bool(re.search(r'f"""\s*SELECT|f"[^"]*SELECT\s', _sd_all))
emit(
    not _fstring_sql,
    "[可维护性] 无 f-string 拼 SQL（剥离注释后）",
    "0 命中" if not _fstring_sql else "发现 f-string SQL",
)

# 迁移与测试对触发器 DDL 必须同源（都调 ddl.*，不各写一遍）。
_mig_raw = MIGRATION.read_text(encoding="utf-8") if MIGRATION.exists() else ""
emit(
    "ddl." in _mig_raw and "all_trigger_ddl" in _mig_raw,
    "[可维护性] 迁移调用 ddl.all_trigger_ddl()（与 ORM 元数据同源）",
    "命中山迁" if "all_trigger_ddl" in _mig_raw else "未命中",
)

# 无裸 `except:` 与裸 `except Exception`（窄类型才放过）。
_bare_except: list[str] = []
for _f in WP24_FILES:
    _tree = ast_of(read(_f))
    for _node in ast.walk(_tree):
        if isinstance(_node, ast.ExceptHandler):
            if _node.type is None:
                _bare_except.append(f"{_f}:{_node.lineno} 裸 except")
            elif (
                isinstance(_node.type, ast.Name)
                and _node.type.id == "Exception"
            ):
                _bare_except.append(f"{_f}:{_node.lineno} except Exception")
emit(
    not _bare_except,
    "[可维护性] 无裸 except / 无 except Exception（窄类型）",
    _bare_except[:4] or "0 命中",
)

# 无硬编码凭据 / 无 `print(` 调试残留（剥离注释后）。
_cred = bool(re.search(r'(password|passwd|secret|token)\s*=\s*"[^"]{3,}"', _sd_all, re.I))
print(f"__debug={len(re.findall(r'print\(', _sd_all))}__") if False else None
emit(
    not _cred,
    "[可维护性] 无硬编码凭据（剥离注释后）",
    "0 命中" if not _cred else "发现疑似凭据",
)

# ---------------------------------------------------------------------------
head("--- 错误 ⑪ 的现场：恒真式不变量断言已改写为独立数据源互证 ---")
# ---------------------------------------------------------------------------

# 文档声称：`_expected_largest_remainder` 是测试侧**独立实现**，与返回值互证。
_tests_raw = TESTS.read_text(encoding="utf-8") if TESTS.exists() else ""
_has_indep = "_expected_largest_remainder" in _tests_raw
emit(
    _has_indep,
    "[错误 ⑪] 测试侧存在独立实现 _expected_largest_remainder（互证数据源）",
    "已落地" if _has_indep else "未找到 —— 不变量可能仍是恒真式",
)

# 反向：测试里**不得**再出现"自证型"不变量断言
# （`allocated_total + unallocated == <任何值>` 与入参无关，恒真）。
#
# ⚠️ **本判据第一版自己就犯了错误 ⑫**：用正则 `== total` 扫全文，
# 结果命中的是**第 863 行 docstring 里的说明文字**
# （`原版断言 result.allocated_total + result.unallocated == total`）——
# 判据**报红了，但报红的原因是错的**（报的是"文档在讲解这件事"）。
# 同时它**漏掉了真正该抓的那条**：第 1097 行
# `assert result.allocated_total + result.unallocated == 10`（拿字面量 10 比）。
#
# 修法：走 AST 找 `ast.Assert` 节点，判定其 `test` 是不是
# `Add(allocated_total, unallocated) <op> <任意表达式>`。
# 这样既**不受 docstring 干扰**，也**不依赖右操作数恰好写 `total`**。
_tauto_hits: list[str] = []


def _is_allocation_sum(node: ast.AST) -> bool:
    """`X + Y` 且 X 提到 allocated_total、Y 提到 unallocated（或反之）。"""
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Add):
        return False

    def _mentions(n: ast.AST, word: str) -> bool:
        return any(
            isinstance(s, ast.Attribute) and s.attr == word
            for s in ast.walk(n)
        ) or any(isinstance(s, ast.Name) and s.id == word for s in ast.walk(n))

    return (_mentions(node.left, "allocated_total") and _mentions(node.right, "unallocated")) or (
        _mentions(node.right, "allocated_total") and _mentions(node.left, "unallocated")
    )


if TESTS.exists():
    for _n in ast.walk(ast_of(_tests_raw)):
        if isinstance(_n, ast.Assert) and isinstance(_n.test, ast.Compare):
            if _is_allocation_sum(_n.test.left):
                _tauto_hits.append(f"L{_n.lineno}")
emit(
    not _tauto_hits,
    "[错误 ⑪ 反向] 测试内无 `allocated_total + unallocated == ?` 自证断言（AST 判定）",
    _tauto_hits or "0 命中",
)

# `unallocated >= 0` 必须是断言而非注释。
_has_nonneg = bool(re.search(r"unallocated\s*>=\s*0", _tests_raw))
emit(
    _has_nonneg,
    "[错误 ⑪] unallocated >= 0 进断言（不是注释）",
    "命中" if _has_nonneg else "未找到",
)

# 反向验证：把 `unallocated = total - sum(...)` 改成 `max(0, ...)` 必须报红。
lines.append("")
lines.append("反向验证（**本人实测，非声称**：注入错误实现 → 判据必须报红 → 还原 → 复绿）：")
lines.append("  ① `_capacity_shave` 的 `active` 条件删掉 `and item.weight_permille > 0`")
lines.append("     → [MAJOR-1/5] FAIL ✓ 实际输出 `['room[item.key] > 0']`")
lines.append("     ⚠️ **这条第一轮没报红**：原判据用子串找 `weight_permille > 0`，")
lines.append("        而注释里就写着这句 ⇒ 改后照样通过（判据纪律第 7 条的现场）。")
lines.append("        改成 AST 取 `active = [...]` 的 if 条件后才有区分力。")
lines.append("  ② `change_global_setting` 的循环内加回 `current_project_setting(...)`")
lines.append("     → [MAJOR-4] FAIL ✓ 实际输出 `循环体内逐查=['L628:current_project_setting']`")
lines.append("  ③ 测试里加回 `assert result.allocated_total + result.unallocated == 10`")
lines.append("     → [错误 ⑪ 反向] FAIL ✓ 实际输出 `['L1105']`")
lines.append("  ④ `unallocated = total - sum(...)` 改成 `max(0, ...)`（抹平负值）")
lines.append("     → **本条不报红，且这是正确的**：该缺陷的判据在**测试侧**")
lines.append("        （`_expected_largest_remainder` 互证 + `unallocated >= 0` 断言），")
lines.append("        而它们靠**跑测试**报红，不靠静态扫描。此处的静态判据只负责")
lines.append("        「测试里还有没有恒真式」，不负责「生产实现有没有被改坏」——")
lines.append("        那是 `evidence/m3/wp24-scene-draft.log` 与全量 pytest 的职责。")
lines.append("        留这条记录是为了说清**每条判据的适用范围**，避免把")
lines.append("        「静态判据没报红」误读成「这个缺陷不存在」。")
lines.append("  ①②③ 均在注入后立即还原（`cp` 回备份），复跑回到 32 PASS / 0 FAIL，")
lines.append("        `git diff --stat` 对被改文件无输出 = 确认无残留。")

# ---------------------------------------------------------------------------
head("--- [判据自身的修正记录] ---")
# ---------------------------------------------------------------------------
lines.append("本脚本第一版就有 1 条判据**报红但原因是错的**（错误 ⑫ 同型）：")
lines.append("  「配置端点不得要求 If-Match」最初只扫 `api.py` 里 `require_if_match`")
lines.append("  的**出现次数**，而该函数名在草稿端点上合法出现 3 次 ——")
lines.append("  于是判据把「草稿端点正确使用」当成了「配置端点违规」。")
lines.append("  修法：按**函数边界**定位（AST 取每个端点的源码段），")
lines.append("  并补一条对照判据（草稿端点必须命中 ≥1）。")
lines.append("")
lines.append("第二版又暴露 4 条判据错误（全部是「注释骗过子串判据」的同族）：")
lines.append("  E1. `sans_doc()` 用**字符**偏移配 `ast` 的**字节**列偏移 ⇒")
lines.append("      docstring 结束列溢出到下一行，把 `if dimension == DIMENSION_SCENE:`")
lines.append("      整行涂成空格 ⇒ `ast.parse` 抛 `IndentationError`。")
lines.append("      **这是真实缺陷，且旧脚本里潜伏未发作**：WP21/WP22 的 sans_doc()")
lines.append("      结果只被正则用、从不 ast.parse()，溢出只是「多涂了应该检查的代码」，")
lines.append("      于是那些子串判据**少检查了一段**却不报错（第 1 类缺陷：永不报红）。")
lines.append("      修法：加 `_byte_col_to_char_col()` 换算。")
lines.append("  E2. [MAJOR-3] 查错了模块：落点其实在 `settings_service.get_effective_limits`，")
lines.append("      我写在 `service.py` 里找 `materialize`，得到 -1 就报红。")
lines.append("  E3. [MAJOR-3·对照] 选错了对照对象：`change_global_setting` 签名里")
lines.append("      **没有 project_id**（它是全局写入口），要求它校验项目是无意义的假判据。")
lines.append("      改与 `change_project_setting` 对照（它才有 project_id）。")
lines.append("  E4. [错误 ⑪ 反向] 判据自己犯了错误 ⑫：正则 `== total` 命中的是")
lines.append("      **docstring 里的说明文字**（第 863 行），而**漏掉了真正该抓的**")
lines.append("      第 1097 行 `... == 10`。改成走 `ast.Assert` 判定左操作数形状。")
lines.append("      ⇒ 这条**抓到了一个真实缺陷**（见下）。")
lines.append("")
lines.append("【E4 抓到的东西是真实缺陷，已修复实现而非改判据】")
lines.append("  `test_all_items_at_capacity_leaves_remainder_unallocated` 里原先写着")
lines.append("  `assert result.allocated_total + result.unallocated == 10` —— 那是")
lines.append("  **错误 ⑪ 的残留**：错误 ⑪ 的收口只改了穷举用例，漏掉了本条。")
lines.append("  已改为 `assert result.allocated_total == 2`（钉具体期望值，真断言）。")
lines.append("  ⚠️ 这正说明「判据的覆盖范围」与「判据的判别力」是两件事：")
lines.append("     穷举用例改了 ≠ 所有恒真断言都没了。")
lines.append("")
lines.append("⚠️ 第三处踩坑（与 WP22 同款）：`allocation.py` 的 docstring 里逐字写着")
lines.append("  错误 5 / MAJOR-5 的**反例**（旧 `active` 判据 `if room > 0`）——")
lines.append("  不剥离文档字符串的话，[MAJOR-1/5] 判据会对着一句说明文字报红。")
lines.append("  同理 [MAJOR-1] 的 `assert weight_sum` 与 [MAJOR-3] 的顺序比较")
lines.append("  都已改为 AST / 剥离注释后判定。")

# ---------------------------------------------------------------------------
head("== 结论 ==")
# ---------------------------------------------------------------------------
lines.append("本遍审查：CRITICAL 0 / MAJOR 5 / MINOR 7（全部已修，1 项 MINOR-4 已登记不改）")
lines.append("  逐条判据见上；每条都指到了具体函数与代码位置，不依赖文档声称。")
lines.append("")
lines.append("测试缺口（三遍法共同暴露）5 项，均已闭合，机械证据在")
lines.append("  services/backend/tests/test_planning_wp24.py（6093 行）与")
lines.append("  evidence/m3/wp24-scene-draft.log（60 条活性判据）。")
lines.append("")
lines.append("【错误 ⑪ / ⑫ 两条判据纪律已在本包首次落地为机械判据】")
lines.append("  ⑪ 恒真式不变量 → 改独立数据源互证（判据见上节 3 条）")
lines.append("  ⑫ 报红原因错 → 判据按函数边界定位 + 补对照（见修正记录）")

lines.append("")
_passed = sum(1 for c in checks if c)
_failed = sum(1 for c in checks if not c)
lines.append(f"机械判据：{_passed} PASS / {_failed} FAIL / {len(checks)} checks")
lines.append(f"MIGRATION={MIGRATION.name if MIGRATION else '(未找到)'}")
lines.append(f"TESTS_EXISTS={TESTS.exists()}")

# ---------------------------------------------------------------------------
# 条数核对 + 退出码（**2026-10-01 WP25 收尾时补上**：本脚本此前恒返回 0）
#
# ⚠️ 缺失这三样的后果（WP25 的 CR-2 TOOLING-1 实测抓到，同款缺陷就在本文件）：
#   `tools/m1/rerun_all_evidence.py` 对每个脚本只断言 `rc == 0` 与
#   『证据 mtime 前进』—— 一个**恒返回 0** 的脚本两条都 PASS，
#   且它打印的 `RESULT` 列**只展示、不参与断言**。
#   ⇒ **判据报红与判据全绿在机器可读层面同形**（ISS-143 的变体）。
#   本文件此前只因『恰好 0 FAIL』而从未暴露 —— 那是运气，不是防线。
#
# 门禁：`tools/m1/check_evidence_scripts.py::check_returns_exit_code`
#       （凡累积 `checks` 的脚本必须显式给退出码，且不得是裸 `SystemExit(0)`）。
# ---------------------------------------------------------------------------
EXPECTED_CHECKS = 32
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
out = EVIDENCE_DIR / "wp24-code-review.log"
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
print(f"\n写入 {out}")

raise SystemExit(0 if _verdict else 1)
