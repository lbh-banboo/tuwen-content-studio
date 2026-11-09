"""迁移的 CHECK 约束文本不得「活引用」模块常量（parity 恒绿盲区 / WP27 判据化）。

# ---------------------------------------------------------------------------
# 一、它守的那个盲区（为什么必须有这条判据）
# ---------------------------------------------------------------------------
WP27 阶段 4 落 `prompts` 的 `STATIC_IMAGE` 取值域补登时，实测撞到一件事：

    `migrations/versions/20260924_1359_..._wp23_....py` 里的
    `ck_prompt_templates_task_kind_valid` 是**用 import 委托拼出来的** ——

        def _task_kind_sql() -> str:
            from rednote.modules.prompts.types import TASK_KIND_LIST_SQL
            return TASK_KIND_LIST_SQL

        sa.CheckConstraint(f"task_kind IN ({_task_kind_sql()})", ...)

后果是：**迁移文本跟着活常量走**。于是 `check_migration_model_parity.py`
的「建两个空库，一个跑迁移、一个建模型，逐项对账」设计**恒绿** ——
两侧取的是同一个活常量，它们**同时移动**，永远不可能分叉。

而真实世界里会分叉的是第三种库：**已经执行过 WP23 的老库**。它的 CHECK
在建库那一刻就被固化成当时的取值域；之后 `types.py` 怎么改，它都不动。
于是出现「新库能过、老库过不了」，而**没有任何判据会报红**。

本判据就是把这条盲区钉住：**CHECK 文本里不许出现来自活模块的任何东西**。

# ---------------------------------------------------------------------------
# 二、判据域（精确到"判什么、不判什么"）
# ---------------------------------------------------------------------------
判：**写入库里的 CHECK 约束文本**（`sa.CheckConstraint(第 1 个位置参数)`、
`op.create_check_constraint(第 3 个位置参数)`）里引用的名字。

不判（且必须说清为什么不判，否则就是 ISS-95 的空白）：
  · `from ... import ddl as x` / `all_trigger_ddl` / `consistency_items` /
    `mixing_guard_ddl` —— 本仓**刻意**让触发器与索引名取自模块
    （WP26 迁移 docstring：「触发器名与索引名**反过来必须取自模块**」）。
    它们是**逻辑**，不是**数据**：改了触发器，老库新库必须一起变。
    本判据不把它们列为违规，不是靠白名单，而是因为它们**不出现在 CHECK
    文本里**（见 G4 的阴性对照 —— 判据不是"见 import 就报红"）。
  · `PlanningRepository` / `bootstrap_defaults`（WP24 数据回填）—— 同上。
  · `server_default` —— 全仓实测只有 `sa.text("now()")` 与字面量，无取值域。
  · `sa.Enum` —— 全仓迁移**零使用**（2026-10-07 实测）。若将来引入，
    它的取值域同样是"历史数据"，应纳入本判据的第二种形态。**这是已知盲区，
    写在这里而不是假装不存在。**

# ---------------------------------------------------------------------------
# 三、必须正面回应 WP23 的既有理由（否则就是无视设计意图）
# ---------------------------------------------------------------------------
WP23 的 docstring 明写了它为什么选委托，理由本身是成立的：

    「迁移里手写一遍会让"上界"有两个定义，改一处忘另一处就是**静默的扩权**。」

这条理由**没有被反驳** —— 反驳的是它的**代价排序**：

  · 手写两遍的风险 = 「改一处忘另一处」⇒ 结果是 **parity 报红**（看得见）；
  · 委托给活常量的风险 = 「漂移不可见」⇒ 结果是 **老库升不上来且无人知道**。

两个风险里后者更坏：前者是"看得见的红"，后者是"看不见的错"。
**让漂移报红正是 parity 存在的意义**，委托恰好把它唯一的作用抵消掉了。
所以本判据选「字面量让漂移可见」，与 WP26 迁移的取向一致、与 WP23 相反。

# ---------------------------------------------------------------------------
# 四、为什么豁免不是"留个后门"
# ---------------------------------------------------------------------------
WP23 的三处是**现存**违规，判据一落地就会报红。本判据给它们登记豁免，
但豁免被三条判据夹住，不允许永久滞留：

  · G2a：**豁免必须仍然成立** —— 被豁免的名字必须真的出现在该文件的违规
    集合里。谁把 WP23 改成字面量，这条立刻红，逼你删豁免。
  · G2b：理由长度下界（防"TODO"式占位）。
  · G2c：豁免总数**上界**，只许减不许增。
  · G7：违规集合与豁免覆盖集合**双向对账** —— 多一个违规（新增的）红，
    多一条豁免（失效的）也红。

# ---------------------------------------------------------------------------
# 五、跑法
# ---------------------------------------------------------------------------
    python tools/quality/check_migration_value_domain_no_live_import.py

纯 AST、零数据库、秒级。退出码 0 = PASS，1 = FAIL（打印 `RESULT=FAIL`）。

反向验证（把违规放回去，判据必须报红）：
    python tools/quality/collect_migration_value_domain_reverse_evidence.py

覆盖范围的同步纪律：本脚本是 `check_*` 判据，不落进
`tools/m1/rerun_all_evidence.py` 的 `SCOPE_PATTERNS`（那三条只收
`collect_*_evidence.py` / `refresh_*_evidence.py` / `collect_*code_review.py`），
因此**不需要**改 `MIN_SCRIPTS`；它挂在 CI 门 3（`.github/workflows/ci.yml`）。
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations" / "versions"

#: 活引用的根前缀。迁移文件里凡从它 import 进来的名字，都是"活"的 ——
#: 它的值会随模块演化而变，而迁移文本必须是**建库那一刻的化石**。
LIVE_MODULE_PREFIX = "rednote"

#: 活性下界（铁律 1：一律写下界不写等式；扫不到东西时判据会假绿）。
#: 数值取自 2026-10-07 磁盘实测，只增不减。
MIN_MIGRATIONS = 20
MIN_CHECK_CONSTRAINTS = 260
MIN_LIVE_IMPORTS = 18
#: 「两种写法都被检查」的下界。`op.create_check_constraint` 目前只有
#: WP27 的 `e8f07ba8f2bb` 两处，故下界取 1（取 2 会在那两处被合并/回退时假红）。
MIN_OP_STYLE_CHECKS = 1
MIN_SA_STYLE_CHECKS = 100
#: 阴性对照 G4 的下界：既有活 import、又有 CHECK、且 CHECK 全部合规的迁移文件数。
#: 2026-10-07 实测 13 个。
MIN_CLEAN_FILES_WITH_LIVE = 10
#: 阴性对照 G4b 的下界：CHECK 文本里出现**非污染**名字的处数。
#: 证明判据不是"文本里出现任何 Name 就报红" —— 迁移内自己定义的局部常量
#: （如 WP16 的 `_CAPABILITY_JSON`）是合法且被放行的。2026-10-07 实测 4 处。
MIN_LOCAL_NAME_IN_CHECK = 2

#: 豁免总数**上界**（G2c）。只许减不许增 —— 修一处就减一条。
MAX_EXEMPT = 3
#: 豁免理由的最小长度（G2b）。防"待修"/"TODO"式占位。
MIN_REASON_LEN = 40

#: 现存违规豁免表。键 = `("<迁移文件相对路径>", "<被豁免的活引用名字>")`，
#: 值 = 理由。锚点刻意用**名字**而不是行号：行号会因上下插入注释而漂移，
#: 而"这个名字是否还在 CHECK 文本里被引用"才是豁免成立与否的真条件。
EXEMPT_REFERENCES: dict[tuple[str, str], str] = {
    (
        "migrations/versions/20260924_1359_3cb74d02edc5_wp23_prompt_versions_and_tool_ceiling.py",
        "_task_kind_sql",
    ): (
        "WP23 建迁移时刻意选了『import 委托』取向，其 docstring 有完整理由"
        "（见本文件 §三 的正面回应）。改它要动历史迁移并复验 parity，"
        "属独立动作，不在本判据落地的同一批改动里 —— 故先登记，不静默放过。"
    ),
    (
        "migrations/versions/20260924_1359_3cb74d02edc5_wp23_prompt_versions_and_tool_ceiling.py",
        "_tool_ceiling_sql",
    ): (
        "同上：WP23 的工具上界 CHECK 用同一套委托写法（函数内 import + return）。"
        "三条同因，一起登记、一起到期 —— 修 WP23 时须三条一并删除。"
    ),
    (
        "migrations/versions/20260924_1359_3cb74d02edc5_wp23_prompt_versions_and_tool_ceiling.py",
        "_publication_action_sql",
    ): (
        "同上：WP23 的发布动作取值域 CHECK，第三个同因条目。"
        "G7 的双向对账保证：三条里少修任何一条，判据都会红。"
    ),
}


@dataclass
class CheckSite:
    """一处写进库里的 CHECK 约束文本。"""

    rel: str
    lineno: int
    style: str  # "sa" = sa.CheckConstraint / "op" = op.create_check_constraint
    used_live: tuple[str, ...] = ()
    used_local: tuple[str, ...] = ()


@dataclass
class FileScan:
    """一个迁移文件的扫描结果。"""

    rel: str
    live: tuple[tuple[int, str], ...] = ()
    tainted: frozenset[str] = frozenset()
    checks: list[CheckSite] = field(default_factory=list)
    error: str = ""


def _iter_migrations() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.py"))


def _live_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """收集从活模块 import 进来的名字（`as` 别名即本地名）。

    刻意用 `ast.walk` 而不是只看 `tree.body` —— WP23 的三处是
    **函数内** import（在 `def _task_kind_sql():` 体内），只看模块级会漏。
    """
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(LIVE_MODULE_PREFIX):
            for alias in node.names:
                out.append((node.lineno, alias.asname or alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(LIVE_MODULE_PREFIX):
                    out.append((node.lineno, alias.asname or alias.name.split(".")[0]))
    return out


def _definitions(tree: ast.Module) -> dict[str, list[ast.AST]]:
    """模块级「名字 → 它的定义体」映射（含函数体，故能覆盖 WP23 的间接形态）。"""
    defs: dict[str, list[ast.AST]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            defs.setdefault(node.name, []).extend(ast.walk(node))
        elif isinstance(node, ast.ClassDef):
            defs.setdefault(node.name, []).extend(ast.walk(node))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defs.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None and isinstance(node.target, ast.Name):
            defs.setdefault(node.target.id, []).append(node.value)
    return defs


def _tainted_closure(tree: ast.Module, live: set[str]) -> frozenset[str]:
    """活引用的传递闭包。

    `LIVE` 本身是污染的；任何模块级名字，只要它的**定义体**引用了污染名字，
    它也被污染 —— 迭代到不动点。

    这一步是本判据能不能成立的关键：WP23 的 CHECK 文本里写的是
    `_task_kind_sql()`，**不是** `TASK_KIND_LIST_SQL`。不做闭包就抓不到，
    而"抓不到"的表现是**全绿**（和没这条判据一样）。
    """
    defs = _definitions(tree)
    tainted: set[str] = set(live)
    changed = True
    while changed:
        changed = False
        for name, bodies in defs.items():
            if name in tainted:
                continue
            for body in bodies:
                if any(isinstance(x, ast.Name) and x.id in tainted for x in ast.walk(body)):
                    tainted.add(name)
                    changed = True
                    break
    return frozenset(tainted)


def _check_text_sites(tree: ast.Module) -> list[tuple[int, str, ast.AST]]:
    """收集 CHECK 约束文本节点，返回 `(行号, 写法, 文本节点)`。

    两种写法都必须收（G6）：只认 `sa.CheckConstraint` 会让
    `op.create_check_constraint`（ALTER 已存在的表）这整条路静默漏 ——
    而**改已有表的 CHECK** 恰恰是最容易出本盲区的场景（老库就在那条路上）。
    """
    out: list[tuple[int, str, ast.AST]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else "")
        if name == "CheckConstraint" and node.args:
            out.append((node.lineno, "sa", node.args[0]))
        elif name == "create_check_constraint" and len(node.args) >= 3:
            out.append((node.lineno, "op", node.args[2]))
    return out


def _scan_file(path: Path) -> FileScan:
    rel = path.relative_to(REPO_ROOT).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return FileScan(rel=rel, error=f"{exc.__class__.__name__}: {exc}")

    live = _live_imports(tree)
    tainted = _tainted_closure(tree, {name for _, name in live})
    scan = FileScan(rel=rel, live=tuple(live), tainted=tainted)
    for lineno, style, text_node in _check_text_sites(tree):
        used = {x.id for x in ast.walk(text_node) if isinstance(x, ast.Name)}
        scan.checks.append(
            CheckSite(
                rel=rel,
                lineno=lineno,
                style=style,
                used_live=tuple(sorted(used & tainted)),
                used_local=tuple(sorted(used - tainted)),
            )
        )
    return scan


def main() -> int:
    print("判据：迁移的 CHECK 约束文本不得活引用模块常量（parity 恒绿盲区 / WP27）")
    print()

    files = _iter_migrations()
    scans = [_scan_file(p) for p in files]

    all_checks = [c for s in scans for c in s.checks]
    live_total = sum(len(s.live) for s in scans)
    violations = [c for c in all_checks if c.used_live]
    exempt_keys = set(EXEMPT_REFERENCES)
    violation_keys = {(c.rel, name) for c in violations for name in c.used_live}

    print(f"  扫描 {len(files)} 个迁移文件")
    print(f"  活引用名字：{live_total} 处")
    print(f"  CHECK 约束文本：{len(all_checks)} 处"
          f"（sa 式 {sum(1 for c in all_checks if c.style == 'sa')}"
          f" / op 式 {sum(1 for c in all_checks if c.style == 'op')}）")
    print(f"  污染闭包命中：{len(violations)} 处")
    print()

    print(f"  {'文件:行':<70} {'写法':<5} {'活引用名字'}")
    print("  " + "-" * 118)
    for site in violations:
        mark = "（已豁免）" if all((site.rel, n) in exempt_keys for n in site.used_live) else "  ★违规★"
        print(f"  {site.rel + ':' + str(site.lineno):<70} {site.style:<5} "
              f"{', '.join(site.used_live)}{mark}")
    if not violations:
        print("  （无）")
    print()

    problems: list[str] = []

    # ---- G1：本体 —— 未被豁免的污染即违规
    unexempt = [c for c in violations if any((c.rel, n) not in exempt_keys for n in c.used_live)]
    print(f"  [{'PASS' if not unexempt else 'FAIL'}] G1 CHECK 文本无未豁免的活引用 —— {len(unexempt)} 处")
    for site in unexempt:
        print(f"         ★ {site.rel}:{site.lineno} 用了 {', '.join(site.used_live)}")
    if unexempt:
        problems.append(
            f"{len(unexempt)} 处 CHECK 文本活引用了模块常量"
            " —— 迁移文本会跟着活常量走，`check_migration_model_parity.py` 的"
            "「两个空库」设计因此恒绿：新库能过、老库过不了，而没有任何判据报红"
        )

    # ---- G2a：豁免必须仍然成立（名字真的还在违规集合里）
    stale = sorted(exempt_keys - violation_keys)
    print(f"  [{'PASS' if not stale else 'FAIL'}] G2a 豁免仍然成立（被豁免的名字确实仍被引用） —— 失效 {len(stale)} 条")
    for key in stale:
        print(f"         ★ {key[0]} :: {key[1]} —— 已不在违规集合里，请删除这条豁免")
    if stale:
        problems.append(
            f"{len(stale)} 条豁免已失效（对应违规已不存在）"
            " —— 留着它会让判据对未来的同类违规静默放行（ISS-95 的留后门形态）"
        )

    # ---- G2b：理由不得是占位
    thin = sorted(k for k, reason in EXEMPT_REFERENCES.items() if len(reason.strip()) < MIN_REASON_LEN)
    print(f"  [{'PASS' if not thin else 'FAIL'}] G2b 豁免理由非占位（>= {MIN_REASON_LEN} 字） —— 过短 {len(thin)} 条")
    for key in thin:
        print(f"         ★ {key}")
    if thin:
        problems.append(f"{len(thin)} 条豁免理由过短 —— 没有理由的豁免等于没有判据")

    # ---- G2c：豁免总数只许减不许增
    g2c_ok = len(EXEMPT_REFERENCES) <= MAX_EXEMPT
    print(f"  [{'PASS' if g2c_ok else 'FAIL'}] G2c 豁免总数 <= {MAX_EXEMPT}（只许减不许增）—— {len(EXEMPT_REFERENCES)} 条")
    if not g2c_ok:
        problems.append(
            f"豁免数 {len(EXEMPT_REFERENCES)} 已超过上界 {MAX_EXEMPT}"
            " —— 每新增一条豁免都要先问「为什么不是修掉它」"
        )

    # ---- G3：活性下界（防收集器失效假绿）
    floors = (
        ("G3a 迁移文件数", len(files), MIN_MIGRATIONS),
        ("G3b CHECK 约束文本数", len(all_checks), MIN_CHECK_CONSTRAINTS),
        ("G3c 活引用名字数", live_total, MIN_LIVE_IMPORTS),
    )
    for label, actual, floor in floors:
        ok = actual >= floor
        print(f"  [{'PASS' if ok else 'FAIL'}] {label} >= {floor} —— {actual}")
        if not ok:
            problems.append(
                f"{label} 只有 {actual}（下界 {floor}）"
                " —— 判据的收集器可能已失效，它扫不到东西时会是『全通过』"
            )

    # ---- G4：阴性对照 —— 「有活 import」不等于「违规」
    clean_files = [
        s.rel for s in scans if s.live and s.checks and not any(c.used_live for c in s.checks)
    ]
    g4_ok = len(clean_files) >= MIN_CLEAN_FILES_WITH_LIVE
    print(f"  [{'PASS' if g4_ok else 'FAIL'}] G4 阴性对照：有活 import 的迁移文件仍被放行"
          f"（>= {MIN_CLEAN_FILES_WITH_LIVE}）—— {len(clean_files)} 个")
    for rel in clean_files[:5]:
        print(f"         · {rel}")
    if not g4_ok:
        problems.append(
            f"只有 {len(clean_files)} 个『有活 import 且 CHECK 合规』的文件（下界 {MIN_CLEAN_FILES_WITH_LIVE}）"
            " —— 判据过宽（见 import 就报红）。过宽的判据会被人以『加豁免』绕过，等于没有"
        )

    # ---- G4b：阴性对照 —— 「CHECK 文本里出现名字」也不等于「违规」
    local_hits = sum(len(c.used_local) for c in all_checks)
    g4b_ok = local_hits >= MIN_LOCAL_NAME_IN_CHECK
    print(f"  [{'PASS' if g4b_ok else 'FAIL'}] G4b 阴性对照：迁移内局部常量进 CHECK 仍被放行"
          f"（>= {MIN_LOCAL_NAME_IN_CHECK}）—— {local_hits} 处")
    for site in [c for c in all_checks if c.used_local][:5]:
        print(f"         · {site.rel}:{site.lineno} 用 {', '.join(site.used_local)}")
    if not g4b_ok:
        problems.append(
            f"CHECK 文本引用局部常量的处数只有 {local_hits}（下界 {MIN_LOCAL_NAME_IN_CHECK}）"
            " —— 判据把『文本里出现任何名字』都判成违规了。正确形态是只判污染闭包内的名字"
        )

    # ---- G5：解析完整性
    errors = [s for s in scans if s.error]
    print(f"  [{'PASS' if not errors else 'FAIL'}] G5 全部迁移文件可解析 —— {len(errors)} 个失败")
    for scan in errors:
        print(f"         ★ {scan.rel} —— {scan.error}")
    if errors:
        problems.extend(f"迁移文件解析失败：{s.rel} —— {s.error}" for s in errors)

    # ---- G6：两种写法都要被检查（铁律 14 —— 覆盖范围不许由单一形态决定）
    sa_n = sum(1 for c in all_checks if c.style == "sa")
    op_n = sum(1 for c in all_checks if c.style == "op")
    g6_ok = sa_n >= MIN_SA_STYLE_CHECKS and op_n >= MIN_OP_STYLE_CHECKS
    print(f"  [{'PASS' if g6_ok else 'FAIL'}] G6 两种写法都被覆盖"
          f"（sa>= {MIN_SA_STYLE_CHECKS} / op>= {MIN_OP_STYLE_CHECKS}）—— sa {sa_n} / op {op_n}")
    if not g6_ok:
        problems.append(
            f"两种 CHECK 写法未都被覆盖（sa {sa_n} / op {op_n}）"
            " —— `op.create_check_constraint`（ALTER 已存在的表）正是老库"
            "最容易踩本盲区的那条路，只认 `sa.CheckConstraint` 会让它静默漏"
        )

    # ---- G7：违规集合与豁免集合双向对账
    extra = sorted(violation_keys - exempt_keys)
    g7_ok = not extra
    print(f"  [{'PASS' if g7_ok else 'FAIL'}] G7 违规集合 ⊆ 豁免集合（双向对账）—— 未被覆盖 {len(extra)} 个")
    for key in extra:
        print(f"         ★ {key[0]} :: {key[1]}")
    if not g7_ok:
        problems.append(
            f"{len(extra)} 个违规未被任何豁免覆盖（与 G1 同源，此处作为对账面重列）"
        )

    print()
    if not problems:
        print(
            f"[PASS] {len(all_checks)} 处 CHECK 文本全部合规"
            f"（无未豁免的活引用；{len(EXEMPT_REFERENCES)} 条豁免仍成立；"
            f"{len(clean_files)} 个有活 import 的文件被正确放行）。"
        )
        return 0
    print(f"[FAIL] RESULT=FAIL 共 {len(problems)} 处问题：")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
