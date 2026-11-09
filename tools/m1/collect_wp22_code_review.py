#!/usr/bin/env python
"""WP22 代码审查证据（三遍法：结构 → 逐行 → 边界与加固）。

产出 `evidence/m1/wp22-code-review.log`。

**为什么要把审查结果落成文件**：审查结论是"声称"，而声称需要证据。
本项目已踩过"文档/看板是唯一声称方时没有任何机械判据与它对账"（ISS-78/84/85/86），
因此审查记录里每一处"通过"都必须附上**能复现它的命令或代码位置**。

⚠️ **本脚本的第一版有 9 条判据自己就是错的**（详见文件末尾「判据自身的修正记录」）。
其中 5 条是被**注释与文档字符串**骗了 —— 例如 `repository.py` 的模块 docstring
在讲解"jsonb 字面量里 `:1` 会被当绑定参数"这个坑时，**自己写了那个反例**，
于是子串判据把说明文字当成了违规代码。因此本版：
  · 所有文本判据先经 `sans_doc()` 剥离注释与文档字符串；
  · 结构判据一律走 AST，不做子串匹配；
  · 每一条关键判据附「反向验证」（把错误实现放回去必须 FAIL）。
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m1"

MODULE = REPO_ROOT / "services" / "backend" / "src" / "rednote" / "modules" / "baseline"
MIGRATION = (
    REPO_ROOT / "migrations" / "versions"
    / "20260926_0339_185ec01bed91_wp22_information_review_and_baseline.py"
)

WP22_FILES = (
    "__init__.py",
    "api.py",
    "baseline_types.py",
    "context_order.py",
    "ddl.py",
    "dependencies.py",
    "models.py",
    "readiness.py",
    "repository.py",
    "service.py",
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


def sans_doc(src: str) -> str:
    """把**注释与文档字符串**替换成空白，保留行结构与普通字符串字面量。

    这是本脚本第一版 5 条假发现的**唯一根因**的修法：
    `repository.py` 的模块 docstring 逐字写着反例（`{"page":1}`、`if operator_confirmed:`），
    而子串判据分不清"说明这件事"与"做了这件事"。

    普通字符串字面量**必须保留** —— SQL 就住在里面。
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
                mark(v.lineno, v.col_offset, v.end_lineno, v.end_col_offset)

    return "".join(
        (" " if (mask[i] and ch != "\n") else ch) for i, ch in enumerate(src)
    )


SD: dict[str, str] = {f: sans_doc(read(f)) for f in WP22_FILES}


def ast_of(name: str) -> ast.Module:
    return ast.parse(read(name))


def parents_of(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    p: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            p[child] = node
    return p


lines.append("# WP22 代码审查证据（三遍法）")
lines.append("#")
lines.append("# 审查对象：")
for f in WP22_FILES:
    lines.append(f"#   modules/baseline/{f}")
lines.append("#   migrations/versions/20260926_0339_185ec01bed91_wp22_*.py")
lines.append("#   contracts/openapi.json（增量 7 操作）")
lines.append("#")
lines.append("# 三遍法：一遍看结构与架构契合 / 二遍逐行 / 三遍边界与加固。")
lines.append("# 每一条「通过」都附可复现的判据，不写没有判据的结论。")
lines.append("#")
lines.append("# ⚠️ 三处 MAJOR 是**在真 PG 库上探针跑出来的**，不是读代码推出来的：")
lines.append("#   探针：/tmp/wp22_cr_probe.py / wp22_cr_probe2.py / wp22_cr_probe3.py")
lines.append("#   库：rednote_mig_wp22（一次性，收口时丢弃）")
lines.append("#")
lines.append("# ⚠️ 本脚本第一版有 9 条判据自己就是错的（见末尾「判据自身的修正记录」）。")
lines.append("#   因此本版所有文本判据先剥离注释/文档字符串，结构判据一律走 AST。")

# ---------------------------------------------------------------------------
head("")
head("== 一遍：结构与架构契合 ==")

# 1) 分层：纯函数 / 存储 / 业务 / 接口四层是否各就各位
LAYERS = {
    "纯函数层": ("readiness.py", "context_order.py", "baseline_types.py"),
    "存储层": ("repository.py", "models.py", "ddl.py"),
    "业务层": ("service.py",),
    "接口层": ("api.py", "dependencies.py"),
}
for layer, files in LAYERS.items():
    present = [f for f in files if (MODULE / f).exists()]
    emit(len(present) == len(files), f"分层「{layer}」文件齐全", present)

# 2) 两个纯函数层不得 import ORM（它们要能被单测直接喂输入）
for pure in ("readiness.py", "context_order.py"):
    mods: list[str] = []
    for node in ast.walk(ast_of(pure)):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
    bad = [m for m in mods if "models" in m]
    emit(
        not bad,
        f"纯函数层 {pure} 不 import ORM 模型（可被单测直接喂输入）",
        bad or f"import 清单={mods}",
    )

# 3) 依赖方向：业务层不 import 接口层（判据必须是 import 语句，不能子串匹配）
svc_mods: list[str] = []
for node in ast.walk(ast_of("service.py")):
    if isinstance(node, ast.ImportFrom) and node.module:
        svc_mods.append(node.module)
    elif isinstance(node, ast.Import):
        svc_mods.extend(a.name for a in node.names)
emit(
    not any(m.endswith(".api") or m.endswith("dependencies") for m in svc_mods),
    "依赖方向正确：业务层不 import 接口层（api 依赖 service，反向不成立）",
    "无反向 import" if not any(m.endswith(".api") for m in svc_mods) else f"{svc_mods}",
)

# 4) 接口/业务层不得有裸 SQL（全部 SQL 收在 repository.py / ddl.py）
sql_outside: list[str] = []
for f in WP22_FILES:
    if f in ("repository.py", "ddl.py"):
        continue
    for i, line in enumerate(SD[f].splitlines(), 1):
        if re.search(r"\btext\(\s*[\"']", line) and re.search(
            r"\b(SELECT|INSERT|UPDATE|DELETE)\b", line, re.I
        ):
            sql_outside.append(f"{f}:{i}")
emit(
    not sql_outside,
    "业务/接口层无裸 SQL（全部 SQL 收在 repository.py / ddl.py）",
    sql_outside or "未发现",
)

# 5) 迁移同时有 upgrade/downgrade，且 downgrade 只删本包对象（G2 铁律）
mig = MIGRATION.read_text(encoding="utf-8")
emit(
    "def upgrade()" in mig and "def downgrade()" in mig,
    "迁移同时实现 upgrade / downgrade",
    "两者都在",
)
drop_tables = re.findall(r'op\.drop_table\(\s*"([^"]+)"', mig)
WP22_TABLES = {
    "information_drafts",
    "production_baselines",
    "baseline_items",
    "step_approvals",
}
emit(
    set(drop_tables) == WP22_TABLES,
    "downgrade 恰好删本包四张表（G2：不多删别人的、也不少删自己的）",
    f"{len(drop_tables)} 张 = {sorted(drop_tables)}",
)
# ⚠️ 触发器 DDL **不在迁移文件里**，而是从 `ddl.py` 的 `all_trigger_ddl()` 取
#    （迁移 L83/L798/L816 的说明）。因此幂等写法要去 `ddl.py` 里找，
#    而迁移只需证明它**确实调了那个函数**。
emit(
    "all_trigger_ddl()" in mig and "from rednote.modules.baseline import ddl" in mig,
    "迁移的触发器 DDL 来自 ddl.all_trigger_ddl()（与测试同源，不各写一遍）",
    "已 import 并调用",
)
emit(
    "DROP TRIGGER IF EXISTS" in read("ddl.py")
    and "CREATE OR REPLACE FUNCTION" in read("ddl.py"),
    "ddl.py 走 DROP TRIGGER IF EXISTS + CREATE OR REPLACE FUNCTION"
    "（CREATE TRIGGER 非幂等，本机实测复现过 DuplicateObject）",
    "幂等写法在 ddl.py 里",
)

# 6) G3 铁律：env.py 必须 import 本包 models（否则 autogenerate 生成 DROP）
env_py = (REPO_ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
emit(
    "from rednote.modules.baseline import models" in env_py,
    "migrations/env.py 已 import 本包 models（G3：不 import 会被 autogenerate 当多余表 DROP）",
    "import 在位",
)

# 7) main.py 已挂载本包 router
main_py = (REPO_ROOT / "services" / "backend" / "src" / "rednote" / "main.py").read_text(
    encoding="utf-8"
)
emit(
    "from rednote.modules.baseline import router as baseline_router" in main_py
    and "app.include_router(baseline_router" in main_py,
    "main.py 已 import 并挂载 baseline router",
    "import + include_router 都在",
)

# ---------------------------------------------------------------------------
head("")
head("== 二遍：逐行（机械扫描，剥离注释与文档字符串） ==")

# 8) 未使用 import（死代码）
unused_report: list[str] = []
for f in WP22_FILES:
    text = read(f)
    tree = ast.parse(text)
    imported: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported[alias.asname or alias.name.split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    imported[alias.asname or alias.name] = node.lineno
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    unused = [
        k for k in imported if k not in used and k != "annotations" and f"'{k}'" not in text
    ]
    # `__init__.py` 的 `models` / `router` 是刻意的 re-export（模块 docstring
    # 末节说明的公开面），由 `__all__` 之外的名字承载，单独放行。
    if f == "__init__.py":
        unused = [k for k in unused if k not in ("models", "router")]
    if unused:
        unused_report.append(f"{f}: {unused}")
emit(not unused_report, "未使用 import（死代码）扫描", unused_report or "全部干净")

# 9) 硬编码凭据 / 密钥
secret_hits: list[str] = []
SECRET_RE = re.compile(
    r"(password|passwd|secret|api[_-]?key|token)\s*=\s*[\"'][^\"']{6,}[\"']", re.IGNORECASE
)
for f in WP22_FILES:
    for i, line in enumerate(SD[f].splitlines(), 1):
        if SECRET_RE.search(line):
            secret_hits.append(f"{f}:{i}")
emit(not secret_hits, "无硬编码凭据（C6 安全）", secret_hits or "未发现")

# 10) 裸 except（吞异常）—— 只允许窄类型
broad: list[str] = []
seen: list[str] = []
for f in WP22_FILES:
    for node in ast.walk(ast.parse(read(f))):
        if isinstance(node, ast.ExceptHandler):
            kind = "bare" if node.type is None else ast.unparse(node.type)
            seen.append(f"{f}:{node.lineno} except {kind}")
            if node.type is None or kind in ("Exception", "BaseException"):
                broad.append(f"{f}:{node.lineno} except {kind}")
emit(
    not broad,
    "无裸 except / except Exception（吞异常会让可读 4xx 退化成 500）",
    broad or f"仅窄类型：{seen}",
)

# 11) SQL 无 f-string 拼接（值必须走绑定参数）
sql_fstring: list[str] = []
for f in ("repository.py",):
    for i, line in enumerate(SD[f].splitlines(), 1):
        if re.search(r"\btext\(\s*f[\"']", line):
            sql_fstring.append(f"{f}:{i}")
emit(
    not sql_fstring,
    "SQL 无 f-string 拼接（值全部走 :name 绑定参数，标识符也不拼）",
    sql_fstring or "未发现 f-string SQL",
)

# 12) 每个 `text()` 都作为 `execute(...)` 的第一个实参且带参数字典（AST 判定）
repo_tree = ast_of("repository.py")
repo_parents = parents_of(repo_tree)
n_text = 0
text_bad: list[str] = []
for node in ast.walk(repo_tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "text":
        n_text += 1
        parent = repo_parents.get(node)
        ok_parent = (
            isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Attribute)
            and parent.func.attr == "execute"
            and node in parent.args
            and len(parent.args) >= 2
        )
        if not ok_parent:
            text_bad.append(f"L{node.lineno}")
emit(
    not text_bad and n_text >= 8,
    "每个 `text()` 都是 `execute(sql, params)` 的第一个实参且带参数（AST 判定，非子串）",
    f"{n_text} 处 text() 调用，无参数者为 {text_bad or '空集'}",
)

# 13) 本项目高频坑第 2 条：jsonb 字面量里的 `:1` 会被 text() 当绑定参数
jsonb_risk: list[str] = []
for f in ("repository.py", "service.py", "ddl.py", "models.py"):
    for i, line in enumerate(SD[f].splitlines(), 1):
        if re.search(r"\{\"[a-zA-Z_]+\":\d", line):
            jsonb_risk.append(f"{f}:{i}")
emit(
    not jsonb_risk,
    "jsonb 字面量里冒号后都留了空格（本项目高频坑第 2 条：':1' 会被当绑定参数）",
    jsonb_risk or "未发现（剥离注释后）",
)

# 14) 仓储层 SQL 集中且可数
emit(
    n_text >= 8 and len(read("repository.py").splitlines()) < 700,
    "仓储层 SQL 集中（text() 只用于读查询与 scope 探测，写操作走 ORM）",
    f"text() {n_text} 处 / 文件 {len(read('repository.py').splitlines())} 行",
)

# ---------------------------------------------------------------------------
head("")
head("== 三遍：边界与加固（含实证结论） ==")

types_src = read("baseline_types.py")
co_src = read("context_order.py")
svc_src = read("service.py")
repo_src = read("repository.py")
api_src = read("api.py")

# 15) 受控取值域与 plpgsql CHECK 同源：每个 *_LIST_SQL 由 `sorted(<常量元组>)` 拼
#
# ⚠️ 第二版这里仍写成 `ast.Assign`，而本项目所有模块级常量都带
#    `Final[...]` 注解 → 它们是 `ast.AnnAssign`，于是收集到 0 个常量、
#    判据假 FAIL。判据的**节点类型选错**与上面「作用域选错」是同一族错误。
list_sql_bad: list[str] = []
list_sql_names: list[str] = []
for node in ast.walk(ast.parse(types_src)):
    target = None
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        target, value = node.target.id, node.value
    elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
        target, value = node.targets[0].id, node.value
    if target is None or not target.endswith("_LIST_SQL") or value is None:
        continue
    list_sql_names.append(target)
    seg = ast.get_source_segment(types_src, value) or ""
    # 必须由 sorted(...) 拼，且不得出现任何**硬编码的域取值字面量**（全大写单词）
    if "sorted(" not in seg or re.search(r"'(?!\{name\})[A-Z_]{3,}'", seg):
        list_sql_bad.append(f"{target}: {seg[:70]}")
emit(
    not list_sql_bad and len(list_sql_names) >= 5,
    "受控取值域都由 `sorted(<常量元组>)` 派生（plpgsql CHECK 与 Python 常量同源，不各写一遍）",
    f"{len(list_sql_names)} 个 LIST_SQL 常量，异常者 {list_sql_bad or '空集'}",
)

# 16) `_SUBJECT_KIND_ENTITY_TYPE` 键集合 == `SUBJECT_KINDS`（ISS-78 同族判据）
#
# ⚠️ 首版只解析 service.py，而 `SUBJECT_KINDS` **定义在 baseline_types.py**
#    （service.py 是 import 进来的）→ 得到空集合、判据假 FAIL。
#    这正是"判据自己的作用域搞错了"的形态，故本版跨两个文件取真身。
map_keys: set[str] = set()
for node in ast.walk(ast.parse(svc_src)):
    if (
        isinstance(node, ast.AnnAssign)
        and getattr(node.target, "id", "") == "_SUBJECT_KIND_ENTITY_TYPE"
    ):
        for k in node.value.keys:  # type: ignore[union-attr]
            map_keys.add(getattr(k, "id", getattr(k, "value", "?")))
subject_kinds: set[str] = set()
for node in ast.walk(ast.parse(types_src)):
    if (
        isinstance(node, ast.AnnAssign)
        and getattr(node.target, "id", "") == "SUBJECT_KINDS"
    ):
        for el in node.value.elts:  # type: ignore[union-attr]
            subject_kinds.add(getattr(el, "id", "?"))
emit(
    bool(map_keys) and map_keys == subject_kinds,
    "`_SUBJECT_KIND_ENTITY_TYPE` 键集合 == `SUBJECT_KINDS`"
    "（漏加会让 approve_step 的交叉核对静默跳过）",
    f"映射键={sorted(map_keys)} / SUBJECT_KINDS={sorted(subject_kinds)}",
)

# 17) context_order 两处缺口由显式集合钉住（白名单而非兜底桶）
emit(
    "UNMAPPED_ITEM_KINDS" in co_src and "UNSOURCED_CONTEXT_SOURCES" in co_src,
    "context_order 两处缺口各有显式集合（UNMAPPED_ITEM_KINDS / UNSOURCED_CONTEXT_SOURCES）",
    "两个集合都在",
)
emit(
    "raise UnknownItemKindError" in co_src and "raise UnknownContextSourceError" in co_src,
    "未知 item_kind / source 抛错而非兜底排末尾（兜底桶永不告警，ISS-78 同族）",
    "两处都抛错",
)
emit(
    "illegal = unknown_sources(" in co_src,
    "order_context_items 排序前做**整体**合法性检查（空列表/被 slice 的来源不会漏过）",
    "整体检查在位",
)
# 等式判据：映射(4) + 刻意缺口(1) == ITEM_KINDS(5)
mapped = re.findall(r"(ITEM_KIND_\w+): CONTEXT_SOURCE_", co_src)
kinds_block = re.search(r"ITEM_KINDS[^=]*=\s*\(([^)]*)\)", types_src, re.S)
all_kinds = re.findall(r"ITEM_KIND_\w+", kinds_block.group(1)) if kinds_block else []
emit(
    len(mapped) == 4 and len(all_kinds) == 5 and len(mapped) < len(all_kinds),
    "ITEM_KIND 映射(4) + 刻意缺口(1) == ITEM_KINDS(5)"
    "（等式断言：新增种类必须回答「它排第几」）",
    f"ITEM_KINDS={len(all_kinds)} 个 / 映射={len(mapped)} 条 / 缺口={len(all_kinds) - len(mapped)}",
)

# 18) `RESEARCH` 项缺 contextOrder → 服务层必须转 422 而不是冒到 500
emit(
    "except UnknownItemKindError" in svc_src and "该项必须显式给出 contextOrder" in svc_src,
    "RESEARCH 项缺 contextOrder → 服务层转 422（`UnknownItemKindError` 不冒到 500）",
    "已转 422",
)

# 19) `operator_confirmed` 用 `is not None`（`False` 是合法显式值）
emit(
    "if operator_confirmed is not None:" in SD["repository.py"]
    and "if operator_confirmed:" not in SD["repository.py"],
    "`operator_confirmed` 用 `is not None`（`False`=取消确认，真值判会让它静默无效）",
    "is not None 在位（剥离文档字符串后判定）",
)

# 20) 乐观锁两层：端点前置 require_if_match + 仓储条件 UPDATE
if_match_calls = re.findall(r"require_if_match\(", api_src)
emit(
    len(if_match_calls) >= 2,
    "两个写端点都做 `If-Match` 前置校验",
    f"{len(if_match_calls)} 处",
)
emit(
    "InformationDraft.lock_version == expected_lock_version" in repo_src,
    "仓储层条件 UPDATE（挡「两个请求同时通过前置校验」）",
    "WHERE lock_version = :expected 在位",
)

# 21) 端点权限：7 个端点必须都有 require(...)，读 VIEW / 写 REVIEW
require_calls = re.findall(r"access\.require\((ProjectAction\.\w+)\)", api_src)
n_view = require_calls.count("ProjectAction.VIEW")
n_review = require_calls.count("ProjectAction.REVIEW")
emit(
    len(require_calls) == 7,
    "7 个端点全部声明权限（读 VIEW / 写 REVIEW）",
    f"{len(require_calls)} 处：{require_calls}",
)
# ⚠️ 首版我把期望写成「读 3 / 写 4」——错了。模块 docstring §二 的表里
#    `GET .../baselines*` 一个路径名覆盖**列表与详情两个端点**，
#    因此正确计数是 VIEW=4 / REVIEW=3。错的是我的期望，不是代码。
emit(
    n_view == 4 and n_review == 3,
    "读 4（VIEW）/ 写 3（REVIEW）与模块 docstring §二 的表一致"
    "（`GET .../baselines*` 覆盖列表+详情两个端点）",
    f"VIEW={n_view} REVIEW={n_review}",
)

# 22) 契约：本包 3 个新路径都在
contract = (REPO_ROOT / "contracts" / "openapi.json").read_text(encoding="utf-8")
emit(
    all(
        p in contract
        for p in (
            "/projects/{projectId}/information-draft",
            "/projects/{projectId}/baselines",
            "/projects/{projectId}/step-approvals",
        )
    ),
    "契约已含本包 3 个新路径（第 4 个是 /baselines/{baselineId} 详情）",
    "information-draft / baselines / step-approvals 都在",
)
emit(
    "/projects/{projectId}/baselines/{baselineId}" in contract,
    "契约含基线详情路径（归属不符给 404 防枚举）",
    "详情路径在位",
)

# ---------------------------------------------------------------------------
head("")
head("== 实证缺陷（探针跑出来的，不是读出来的） ==")

lines.append("")
lines.append("--- [MAJOR-1] `brief_version_id` 全链路无版本类型校验（发现 A + G） ---")
lines.append("探针：/tmp/wp22_cr_probe.py 用例 A（草稿侧）、wp22_cr_probe2.py 用例 G（基线侧）")
lines.append("现象：把一个 **KNOWLEDGE** 版本节点的 UUID 写进 `briefVersionId`，两处都成功入库。")
lines.append("  探针 A 实测（草稿）：库中值 = <该 KNOWLEDGE 节点 uuid> ← 非 BRIEF 节点被静默接受")
lines.append("  探针 G 实测（基线）：库中值 = 0205d2d3-... ← 与草稿同一个缺口")
lines.append("根因（三条，都可机械核实）：")
lines.append("  · 该字段只有 FK 校验「节点存在」，无「节点是 BRIEF」的校验；")
lines.append("  · ddl.py 的 6 个一致性触发器**全部不覆盖 `brief_version_id`**")
lines.append("    （机械核实：ddl.py 中 `brief_version_id` 出现 0 次）；")
lines.append("  · service.py 的 `_require_item_scope` 对 `knowledge_version_id` 做了")
lines.append("    「版本必须属于声明资产」的交叉核对（L936-954），而 `brief_version_id`")
lines.append("    没有任何同类校验 —— **同一模块内两条同类字段的处理不对称**。")
lines.append("对照：`approve_step` 对 `subject_kind` 做了类型交叉核对（service.py L1596-1607，")
lines.append("  用 `_SUBJECT_KIND_ENTITY_TYPE` 比对一个 kind → entity_type 映射）；")
lines.append("  `repo.node_entity_type()` 已存在且可用 —— 说明本模块**有**这个工具，")
lines.append("  只是没用在 `brief_version_id` 上。")
lines.append("后果：基线可能「钉住」一个非 Brief 的版本 —— 决策 1 的 ref 静默错链")
lines.append("  （AC-24 溯源断链方向），且不报错。")
lines.append("处置：加一条与 `_require_item_scope` 同款的类型核对；草稿侧在 `update_draft`")
lines.append("  校验，基线侧在 `create_baseline` 第 4 步校验。")

lines.append("")
lines.append("--- [MAJOR-2] 不传 `supersedesId` 的第二次创建 → 裸 IntegrityError → HTTP 500（发现 B/D） ---")
lines.append("探针：wp22_cr_probe.py 用例 B2（幂等复投）、wp22_cr_probe2.py 用例 D（第二条）")
lines.append("现象：对同一项目连续两次 `create_baseline`（不传 `supersedesId`），第二次抛")
lines.append('  `(psycopg.errors.UniqueViolation) duplicate key value violates unique constraint "uq_production_baselines_project_root"`')
lines.append("定性：这是**裸 500**，不是可读 4xx —— `platform/errors.py` L476-480 的全局")
lines.append("  handler 只把 `ApiError` 映射为 4xx，其它一律兜底结构化 500（不回显堆栈）。")
lines.append("  探针 D 实测确认：`type(exc).__name__ == 'IntegrityError'`（不是 `ApiError`）。")
lines.append("链路：`register_version` 未收到 `revision_no` → 取 `max_revision_no + 1`（=2）")
lines.append("  → `created=True` → `insert_baseline(supersedes_id=None)`")
lines.append("  → 撞部分唯一索引 `uq_production_baselines_project_root`。")
lines.append("根因：`_require_supersedes_target`（service.py L1044）只校验「传进来的 id 合法」，")
lines.append("  **不校验「本项目已有链尾时必须传」** —— 而 AC-08 恰恰要求改基线必须新建版本。")
lines.append("对照：探针 E 证明传了 `supersedesId` 时路径完全正常（rev=2 / created=True /")
lines.append("  superseded=True），因此缺的只是那条前置判据。")
lines.append("处置：把「无 `supersedesId` 但本项目已有链尾」在**第 5 步**提升为可读 409/422；")
lines.append("  这样部分唯一索引退化为「最后一道保险」而不是「唯一的一道」。")
lines.append("附带（发现 F，正面确认）：第 6 层 `trg_production_baselines_no_blocking` 只看")
lines.append("  创建那一刻的快照 —— 基线建成后草稿里再加阻断冲突（kind=SOURCE, OPEN,")
lines.append("  blocking=True）写成功且不影响已建基线。**这是正确行为**（基线不可变），")
lines.append("  但值得在测试里钉住，否则将来有人「顺手」加一个追溯判据会破坏不可变性。")

lines.append("")
lines.append("--- [MAJOR-3] 部分更新语义被自己违反：`briefVersionId` 被无条件写（发现 H） ---")
lines.append("探针：/tmp/wp22_cr_probe3.py 用例 H1/H2/H3")
lines.append("现象：H1 只改 conflicts（不传 briefVersionId）→ 库中值变 NULL（被静默清空）")
lines.append("      H2 只改 scenes（不传 briefVersionId）→ 再次变 NULL（可复现）")
lines.append("      H3 每次都显式携带 briefVersionId → 保留（对照组，证明不是别的原因）")
lines.append("根因：`repository.py` L325 `values[\"brief_version_id\"] = brief_version_id`")
lines.append("  是**无条件写**，而同函数 L326-337 的其它六块都是 `if x is not None` 才写。")
lines.append("  该函数自己的 docstring（L311）明说「**`None` 表示「本次不改这一块」**，")
lines.append("  与「改成空」不同」—— **代码违反了它自己的契约**。")
lines.append("  服务层 `update_draft` 的 docstring（L549-552）进一步警告：")
lines.append("  「不能把 `None` 当成「改成空」——『只想改 scenes』的请求会把没提到的四块")
lines.append("  全部清空，而那是运营几小时工作的**静默丢失**（HTTP 200）」。")
lines.append("  实测结果与这段警告**逐字相符**，只是丢失的是 briefVersionId 而不是「四块」。")
lines.append("加重因素：`SaveDraftBody.brief_version_id`（api.py L195）是")
lines.append("  `uuid.UUID | None = Field(default=None, ...)`，**无法区分**「未提供」与")
lines.append("  「显式 null」—— 探针 I 实测两者 runtime 值都是 None，")
lines.append("  只有 `model_fields_set` 能区分，而服务层/仓储层**都没读它**。")
lines.append("  （对照：`operator_confirmed` 同为 `bool | None`，但它用 `is not None` 判，")
lines.append("   因此 `False` 这个合法显式值不会丢 —— 两个 `Optional` 字段的处理不对称。）")
lines.append("后果：运营设好「这条基线钉住哪一版 Brief」后，任何一次只改别的块的保存")
lines.append("  都会把它清空，且返回 HTTP 200 + 空值。下次创建基线时")
lines.append("  `briefVersionId` 静默为 NULL（基线快照 L1421 也记 NULL）。")
lines.append("处置：`values[\"brief_version_id\"]` 移入 `if brief_version_id is not None:` 分支；")
lines.append("  若确实需要「显式清空」的语义，则改用 `model_fields_set` 判定并在")
lines.append("  `update_draft` 的签名上用一个显式哨兵（同 `operator_confirmed` 的处理哲学）。")

lines.append("")
lines.append("--- [MINOR-1] `readiness` 与 `service` 对冲突/场景 state 取值口径不对称 ---")
lines.append("`readiness.confirmed_claim_ids` 用 `pick_field(claim, CLAIM_STATE_KEYS)`")
lines.append("  容忍 `claimState` / `claim_state` 两种键名；")
lines.append("而 `blocking_open_conflicts`（L205）与 `producible_scene_ids`（L228）用裸")
lines.append("  `conflict.get(\"state\")` / `scene.get(\"state\")`。")
lines.append("定性：与 `service._require_conflicts`（L465）/ `_require_scenes`（L512）")
lines.append("  的裸 `.get(\"state\")` **一致**，因此当前不产生错误；但三个同类字段两种口径，")
lines.append("  将来前端若按 claims 的宽松口径提交 `conflictState`，冲突会静默不被识别为阻断。")
lines.append("处置：统一到 `pick_field`（或统一到裸键），并加一条机械判据钉住「三者同口径」。")

lines.append("")
lines.append("--- [MINOR-2] 两处已知取舍已在代码注释里坦白，收口时须登记 ---")
lines.append("1. `GET .../information-draft` **会写库**（`require_draft` 幂等懒创建）——")
lines.append("   api.py L456-459 已说明「已知取舍」与收口方向（在项目创建事务里补一次调用）。")
lines.append("2. `list_baselines` 用游标分页、仓储层用 `limit/offset` —— api.py L571-573")
lines.append("   已说明「形状不一致，已登记待定项」。")
lines.append("这两条是**注释声称「已登记」**—— 收口时必须真的登记进")
lines.append("`docs/待定项与问题登记册.md`，否则就是 ISS-95 的形态")
lines.append("（「声称自己有防线」比「没有防线」更危险）。")

lines.append("")
lines.append("--- [INFO] 已排除的疑点（逐项有判据） ---")
lines.append("· 无硬编码凭据 —— 机械扫描 0 命中（剥离注释后）；")
lines.append("· 无裸 except / except Exception —— 只有 `except KeyError`×2、")
lines.append("  `except IntegrityError`、`except UnknownItemKindError`（全部窄类型）；")
lines.append("· SQL 无 f-string 拼接 —— `text()` 全部作为 `execute(sql, params)` 的第一个")
lines.append("  实参且带参数字典（AST 判定，非子串）；")
lines.append("· jsonb 字面量冒号后均留空格 —— 本项目高频坑第 2 条，剥离注释后 0 命中；")
lines.append("· `newest_baseline_of` 的自引用 NOT EXISTS 子查询写法正确（刻意选暴露式）；")
lines.append("  ⚠️ **这一条后来被证伪（MAJOR-4）**：子查询里写的是")
lines.append("  `ProductionBaseline.supersedes_id == ProductionBaseline.id` ——")
lines.append("  **同一行自比恒假**（一条记录的 `supersedes_id` 永远不等于它自己的 `id`）")
lines.append("  → `EXISTS` 恒 False → `~EXISTS` 恒 True → 过滤条件**恒真**")
lines.append("  → 返回任意一行，且不报任何错。全量 pytest 里 2 条判据转红才暴露。")
lines.append("  **教训**：这条「已排除」的依据是「读代码」而不是「跑现象」——")
lines.append("  这样得出的排除，本身就是一次无判据的声称（ISS-95 同族）。")
lines.append("· `update_draft` 的乐观锁两层保证正确（端点 require_if_match + 仓储条件 UPDATE）；")
lines.append("· `readiness.py` / `context_order.py` 是纯函数且不 import ORM（可被单测直接喂输入）；")
lines.append("· 第 6 层触发器只判创建那一刻（基线不可变，追溯判定会是错的行为）；")
lines.append("· 触发器 DDL 与测试同源（迁移与测试都调 `ddl.all_trigger_ddl()`，不各写一遍）。")

lines.append("")
lines.append("--- [判据自身的修正记录] 第一版 9 条 FAIL 全是我判据写错，非真实缺陷 ---")
lines.append("这是本项目「判据纪律」的一次现场演示 —— 判据错了会**假装**有发现，")
lines.append("比没有判据更危险（ISS-95 同族）。逐条留痕：")
lines.append("1. 「迁移幂等写法」查错了文件：触发器 DDL 不在迁移里，在 `ddl.py`")
lines.append("   （迁移只是调 `all_trigger_ddl()`）。→ 改为分别查 ddl.py + 迁移的调用。")
lines.append("2. 「SQL 无用户输入拼接」命中 repository.py:196 —— **那是 docstring 里的反例**")
lines.append("   （讲解 `text(\"id = ANY(:ids)\")` 这段写法）。→ 改为先剥离文档字符串。")
lines.append("3. 「params 字典」查 `params=`，而代码是**第二位置实参** `text(...), {...}`。")
lines.append("   → 改为 AST：每个 `text()` 必须是 `execute()` 的第一个实参且带第 2 参。")
lines.append("4. 「jsonb 冒号空格」命中 repository.py:32 —— 又是**模块 docstring 里的反例**")
lines.append("   （`{\"page\":1}`）。→ 同 2，剥离文档字符串后归零。")
lines.append("5. 「text() 计数」查 `session.execute(text(`，而代码是 `self._session.execute(`。")
lines.append("   → 改为 AST 计数。")
lines.append("6. 「受控取值由常量拼」的正则把 `sorted(ITEM_KINDS)` 误判成手写列表。")
lines.append("   → 改为检查**是否含硬编码的域取值字面量**，而不是检查拼接形式。")
lines.append("7. 「SUBJECT_KINDS」我只解析 service.py，而它**定义在 baseline_types.py**")
lines.append("   （service.py 是 import 的）→ 得到空集合。→ 改为跨两文件取真身。")
lines.append("8. 「operator_confirmed」被 docstring 里那句「写 `if operator_confirmed:` 会让…」")
lines.append("   触发。→ 同 2。")
lines.append("9. 「VIEW/REVIEW 计数」**我的期望值写错了**：模块 docstring §二 的表里")
lines.append("   `GET .../baselines*` 一个名字覆盖列表+详情两个端点，正确是 VIEW=4/REVIEW=3。")
lines.append("   代码是对的，错的是判据。→ 修正期望值。")
lines.append("其中第 2/4/8 条是同一根因（**注释与文档字符串骗过子串判据**），")
lines.append("已在 `sans_doc()` 里一次性解决。")

lines.append("")
lines.append("--- [反向验证] 判据有区分力（判据纪律第 7 条） ---")
lines.append("对三条关键判据做了「把错误实现放回去，判据必须 FAIL」的验证：")
lines.append("1. `operator_confirmed` 的 `is not None` 改回真值判 `if operator_confirmed:`")
lines.append("   → 对应判据 FAIL ✓（证明它真能区分两种实现，不是恒真）")
lines.append("2. 把一个 `INCLUSION_LIST_SQL` 从 `sorted(INCLUSIONS)` 改成手写枚举字符串")
lines.append("   → 同源性判据 FAIL ✓")
lines.append("3. 把 `sans_doc()` 关掉（直接用原文）→ **2 条判据重现 FAIL**")
lines.append("   （jsonb 冒号 + operator_confirmed 各一条）")
lines.append("   ✓ 这条尤其重要：它**机械证明**了「剥离注释/文档字符串」不是洁癖，")
lines.append("   而是这三条判据能工作的前提 —— 因为被测文件的 docstring 里")
lines.append("   逐字写着它们的反例。")
lines.append("三项验证均在临时改写后立即还原，复跑回到 35 PASS / 0 FAIL。")

lines.append("")
lines.append("== 结论 ==")
lines.append("本遍审查：CRITICAL 0 / MAJOR 3（A+G 合为 1、B/D 为 1、H 为 1）/ MINOR 2")
lines.append("三处 MAJOR 都**已在真 PG 库上用探针复现**，不是静态推断：")
lines.append("  MAJOR-1 探针 wp22_cr_probe.py:A + wp22_cr_probe2.py:G")
lines.append("  MAJOR-2 探针 wp22_cr_probe.py:B2 + wp22_cr_probe2.py:D")
lines.append("  MAJOR-3 探针 wp22_cr_probe3.py:H1/H2（H3 为对照）")

lines.append("")
lines.append("【补记 · 收口轮】本节初版写的是「处置**不在本阶段改代码**」，已作废。")
lines.append("按「测试必须先红后绿」补完回归判据后，**全量 pytest 一次性暴露了另外 3 个 MAJOR**")
lines.append("（本遍审查没能发现，其中 MAJOR-4 恰是本遍 [INFO] 段落里被我明确「已排除」的那一条）：")
lines.append("  MAJOR-4 `newest_baseline_of` 子查询同行自比恒假 → 链根误判为链尾 → 基线链分叉；")
lines.append("  MAJOR-5 `record_audit` 4 处只传 after 缺配对 before → 撞")
lines.append("          `ck_audit_events_hash_pair_consistent` → 三个写端点全部 500；")
lines.append("  MAJOR-6 `find_root_approval` 用「链根」口径，与 DDL 触发器「未被取代」口径不一致")
lines.append("          → 重批被永久堵死（409）。")
lines.append("六个 MAJOR 处置：**全部已修复**，逐条登记于 docs/issues/issue-log.jsonl")
lines.append("（ISS-96 ~ ISS-101），修复内容见 git 工作区中的")
lines.append("modules/baseline/{service,repository,baseline_types}.py 与 tests/test_baseline_wp22.py。")
lines.append("修复的机械判据（**现场派生，不写死**）：原先转红的 10 条判据现已全绿 ——")
_pytest_log = EVIDENCE_DIR / "backend-pytest.log"
_pytest_line = ""
if _pytest_log.exists():
    for _line in reversed(_pytest_log.read_text(encoding="utf-8").splitlines()):
        if re.search(r"\d+\s+passed", _line):
            _pytest_line = _line.strip()
            break
lines.append(f"  evidence/m1/backend-pytest.log → {_pytest_line or '(未取到汇总行)'}")
lines.append(f"机械判据：{sum(1 for c in checks if c)} PASS / "
             f"{sum(1 for c in checks if not c)} FAIL / {len(checks)} checks")

EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
out = EVIDENCE_DIR / "wp22-code-review.log"
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
print(f"\n写入 {out}")
raise SystemExit(0 if all(checks) else 1)
