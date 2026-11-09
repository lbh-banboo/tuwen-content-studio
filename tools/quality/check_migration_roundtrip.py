#!/usr/bin/env python
"""迁移往返判据：`upgrade head → downgrade base → upgrade head` 必须真的能走通（ISS-79）。

## 为什么需要这个

`downgrade` 这条路径在本仓**从来没有被任何判据走过** —— CI 只跑
`alembic upgrade head`（`ci.yml` 门 1/门 2/门 3 三处都只有 upgrade）。
于是它坏了整整两个大包而无人发现：

    $ alembic downgrade base
    ERROR: cannot drop function rednote_reject_mutation() because
           other objects depend on it
    DETAIL: trigger trg_version_nodes_immutable on table version_nodes
            depends on function rednote_reject_mutation()
    [SQL: DROP FUNCTION IF EXISTS rednote_reject_mutation()]

根因（ISS-79）：WP11 / WP15 的 `downgrade()` 第 2 步遍历 `all_trigger_ddl()`
去取元组的**第 5 项**（`drop_function`）执行 `DROP FUNCTION`。而第 5 项里
混着两类归属完全不同的函数：

  · `rednote_reject_mutation()` —— **跨包共用**（WP11 建，WP15/16/17/18/20/22/23/25
    都挂着它的触发器）⇒ 谁都不许删；
  · `rednote_check_*()` —— **本模块自己的** ⇒ 必须删。

两类函数在 `all_trigger_ddl()` 里长得一模一样，只有 `consistency_items()`
把"归属"显式表达出来。修法就是让第 2 步只迭代 `consistency_items()`。

**为什么必须有判据**：`downgrade` 不可用 ⇒ 回滚路径不存在 ⇒
ADR-11 的 RPO ≤ 24h / RTO ≤ 8h 无法演练与验收 ⇒ 阻塞 WP47。
而"回滚路径存在"这件事，在修好之前**没有任何机械判据会说话**（ISS-95：
声称有防线比没有防线更危险）。

## 判据设计：两段

### 第 1 段（静态，零数据库，秒级）

扫**全部**迁移的 `downgrade` 函数体，禁止"遍历 `all_trigger_ddl()` 去
`DROP FUNCTION`"这一形态（两种写法：下标 `[4]` 推导式 / 五元组解包后
把最后一个名字交给 `op.execute`）。

⚠️ **必须先剥掉 docstring 与注释再匹配**。实测：WP11 / WP15 的 docstring
里**引用**了旧写法做教训记录（"原写法（已废止，不要改回去）"），
不剥就会把这两段引文当成真违规 —— 判据自己制造假红。本脚本自带
阳性/阴性对照（见 `_selfcheck_matcher`）证明剥离确实生效。

### 第 2 段（动态，需要真库）

在**私有临时库**（`rednote_roundtrip_<pid>`，跑完即删）上走完整往返：

    upgrade head → downgrade base → upgrade head

断言四件事：

1. `downgrade base` **成功**（rc=0）—— 这是 ISS-79 的直接判据；
2. base 上**共用函数存活**（`rednote_reject_mutation`）；
3. base 上**模块自己的函数全部消失**，且**触发器归零**；
4. 再 `upgrade head` 后 head 状态**逐项还原**（表 / 函数 / 触发器）。

第 2、3 条的"哪些算共用"**由磁盘决定**（铁律 14）：读
`rednote.platform.immutability.TRIGGER_FUNCTION`，不写函数名字面量。

## 已知边界（不做假保证）

* **不比较函数体内容**，只比较**存在性**（`pg_proc.proname` 集合）。
  函数体正确性由各包证据脚本按行为验（与 `check_migration_model_parity`
  对触发器的处置同款）。
* **不覆盖数据迁移的正确性**：本仓迁移全是 DDL，无数据搬运。
* **base 态残留 `alembic_version` 表**属 alembic 记账表，正常（不参与判定）。

跑法：
    python tools/quality/check_migration_roundtrip.py
退出码 0 = 往返走得通且状态可还原。
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 与 `conftest.py` / `check_migration_model_parity.py` 同口径的默认基准 DSN
#: （**库名会被本脚本替换为一次性临时库**）。
#: ⚠️ 库名不得是 `postgres`（ISS-145）—— 本脚本只借它的 host/user/port。
DEFAULT_BASE_DSN = "postgresql+psycopg://rednote@localhost:5432/rednote_test"

MIGRATIONS_DIR = REPO_ROOT / "migrations" / "versions"

#: `rednote_*` 函数名的取样前缀（本仓全部业务函数都以此为前缀）。
FUNCTION_PREFIX = "rednote"

#: head 态**模块自有函数**数量的下界。
#:
#: 它让"函数查询失效 / 迁移没跑完"与"一切都对"不再同形：
#: 若 `pg_proc` 查询写错、或 `upgrade head` 静默没跑全，自有函数集合会缩水，
#: 而对 base 的"全消失"断言**照样会通过**（空集当然消失）。实测 head 上
#: `rednote_*` 函数 21 个、减去共用 1 个 ⇒ 自有 20 个，下界取 10 留足余量。
MIN_MODULE_OWNED_FUNCTIONS = 10


def _base_dsn() -> str:
    """基准 DSN：环境变量优先（CI 口径），逐级回退到本机口径。"""
    return os.environ.get(
        "REDNOTE_DATABASE_URL",
        os.environ.get("REDNOTE_TEST_DATABASE_URL", DEFAULT_BASE_DSN),
    )


def _dsn_for(dbname: str) -> str:
    """把基准 DSN 的库名换成 `dbname`，并统一驱动为 `postgresql+psycopg`。"""
    from sqlalchemy.engine import make_url

    url = make_url(_base_dsn())
    return url.set(drivername="postgresql+psycopg", database=dbname).render_as_string(
        hide_password=False
    )


def _psycopg_dsn(sqlalchemy_dsn: str) -> str:
    """把 SQLAlchemy DSN 转成 psycopg 能吃的形态（去掉 `+psycopg` 后缀）。"""
    return sqlalchemy_dsn.replace("postgresql+psycopg://", "postgresql://", 1)


def _shared_trigger_function() -> str:
    """读**磁盘上**的共用触发器函数名（铁律 14：不写名字字面量）。

    唯一来源是 `rednote/platform/immutability.py::TRIGGER_FUNCTION` ——
    它是全项目共用只读函数名的**单一事实来源**。本脚本刻意不从
    `modules/*/ddl.py` 里人写的 `immutable_function_names()` 取，
    因为那些返回值本身就是"人写的名单"，取它等于拿结论当依据。
    """
    sys.path.insert(0, str(REPO_ROOT / "services" / "backend" / "src"))
    from rednote.platform.immutability import TRIGGER_FUNCTION

    return TRIGGER_FUNCTION


# ===========================================================================
# 第 1 段：静态扫描全部迁移的 downgrade
# ===========================================================================


def _iterates_all_trigger_ddl(node: ast.AST) -> bool:
    """`node` 是不是 `all_trigger_ddl()` 这个调用（用作 for / 推导式的 iter）。"""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return isinstance(func, ast.Name) and func.id == "all_trigger_ddl"


def _contains_fifth_subscript(node: ast.AST, element_names: set[str]) -> bool:
    """`node` 内部是否有 `item[4]`（item 是 `all_trigger_ddl()` 的元素名）。"""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Subscript):
            continue
        sl = sub.slice
        if not (isinstance(sl, ast.Constant) and sl.value == 4):
            continue
        if isinstance(sub.value, ast.Name) and sub.value.id in element_names:
            return True
    return False


def _is_fifth_comp(node: ast.AST) -> bool:
    """`node` 是不是「`all_trigger_ddl()` 元素的 [4]」这种推导式。

    形态：`{item[4] for item in all_trigger_ddl()}`（SetComp，本仓原始缺陷形态）。
    """
    if not isinstance(node, (ast.SetComp, ast.ListComp, ast.GeneratorExp)):
        return False
    if not any(_iterates_all_trigger_ddl(g.iter) for g in node.generators):
        return False
    return _contains_fifth_subscript(node.elt, _element_names(node))


def _expr_carries_fifth(node: ast.AST, tainted: set[str]) -> bool:
    """`node` 表达式是否"产出第 5 项"（用于赋值 / 迭代来源的传播判定）。"""
    for sub in ast.walk(node):
        # ① 直接是 `{item[4] for item in all_trigger_ddl()}`
        if _is_fifth_comp(sub):
            return True
        # ② 引用了已经确认承载第 5 项的名字
        if isinstance(sub, ast.Name) and sub.id in tainted:
            return True
        # ③ 由这些名字组成的字面容器 `{drop_function}` / `[a, b]`
        if isinstance(sub, (ast.Set, ast.List, ast.Tuple)):
            if any(
                isinstance(e, ast.Name) and e.id in tainted for e in sub.elts
            ):
                return True
    return False


def _fifth_element_names(downgrade: ast.FunctionDef) -> set[str]:
    """找出**值为「`all_trigger_ddl()` 元素的第 5 项」**的所有名字（不动点）。

    覆盖四类真实/可能的形态（少一类就是"覆盖范围由人写"，铁律 14；
    每一类都有 `_selfcheck_matcher` 里的阳性对照钉住）：

      A 推导式直接迭代：`for f in {item[4] for item in all_trigger_ddl()}:`
        —— 本仓 WP11/WP15 的**原始缺陷形态**；
      B 五元组解包：`for _a, _b, _c, _d, drop_function in all_trigger_ddl():`
        —— 同一语义的另一种写法；
      C 先赋值再迭代：`fns = {item[4] for item in all_trigger_ddl()}` …
        `for f in fns:` —— 一行重构即可绕过 A，必须一并覆盖；
      D 名字传递：`fns = {drop_function}` / `g = fns` / `for x in fns:`
        —— 沿赋值与迭代传播（迭代到不动点）。

    ## 为什么这段**不需要**剥离 docstring / 注释

    本函数是**纯 AST 匹配**：`ast.parse` 之后，docstring 只是一个
    `ast.Constant` 字符串节点，`ast.walk` **不会下钻字符串内容**。
    因此"把旧写法写进 docstring 做教训记录"（本仓 WP11/WP15 正是此形态）
    在 AST 层面天然不可见 —— 不构成假红。

    ⚠️ 我最初把这步写成"先剥离文本、再 `ast.unparse`、再正则匹配"，那是
    **方向性错误**：`unparse` 会把字符串常量的内容重新吐回源码面，引文于是被
    当成真代码（实测 WP11/WP15 双双假红）。本仓已因"用正则判代码"吃过两次亏
    （`check_evidence_scripts` 用正则被引文骗过）。**判据的形态（AST vs 文本）
    决定了它会不会被自己引用的文档骗到。**
    """
    tainted: set[str] = set()

    # --- 种子：A / B ---
    for node in ast.walk(downgrade):
        pairs: list[tuple[ast.AST, ast.AST]] = []
        if isinstance(node, ast.For):
            pairs.append((node.iter, node.target))
        elif isinstance(node, ast.comprehension):
            pairs.append((node.iter, node.target))
        for it, tgt in pairs:
            # A：`for f in {item[4] for item in all_trigger_ddl()}`
            if _is_fifth_comp(it) and isinstance(tgt, ast.Name):
                tainted.add(tgt.id)
            # B：`for _a,_b,_c,_d,drop_function in all_trigger_ddl()`
            if _iterates_all_trigger_ddl(it) and isinstance(tgt, ast.Tuple):
                if len(tgt.elts) == 5 and isinstance(tgt.elts[4], ast.Name):
                    tainted.add(tgt.elts[4].id)

    # --- 传播：C / D（不动点）---
    changed = True
    while changed:
        changed = False
        for node in ast.walk(downgrade):
            targets: list[ast.AST] = []
            source: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets, source = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, source = [node.target], node.value
            elif isinstance(node, ast.For):
                # `for x in fns:` —— 迭代一个承载第 5 项的名字
                if _expr_carries_fifth(node.iter, tainted):
                    targets = [node.target]
            if source is not None and not _expr_carries_fifth(source, tainted):
                continue
            for tgt in targets:
                for sub in ast.walk(tgt):
                    if isinstance(sub, ast.Name) and sub.id not in tainted:
                        tainted.add(sub.id)
                        changed = True
    return tainted


def _element_names(comp: ast.AST) -> set[str]:
    """一个推导式里"迭代 `all_trigger_ddl()` 得到元素"所绑定的名字。"""
    names: set[str] = set()
    for gen in getattr(comp, "generators", []):
        if _iterates_all_trigger_ddl(gen.iter):
            for sub in ast.walk(gen.target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
    return names


def _element_names_of(node: ast.AST) -> set[str]:
    """一个表达式里所有推导式的元素名并集（供 `_contains_fifth_subscript` 用）。"""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, (ast.SetComp, ast.ListComp, ast.GeneratorExp)):
            names |= _element_names(sub)
    return names


def forbidden_sites_in_source(src: str) -> list[str]:
    """返回源码里"遍历 `all_trigger_ddl()` 去删函数"的命中位置。

    **纯 AST 匹配，不做文本剥离** —— 理由见 `_fifth_element_names` 的长注释：
    docstring 在 AST 里只是字符串常量，天然不会被误命中。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:  # 语法错由别的判据管；这里不静默通过
        return [f"(无法解析：{exc.msg} @ line {exc.lineno})"]

    hits: list[str] = []
    for node in tree.body:
        if not (isinstance(node, ast.FunctionDef) and node.name == "downgrade"):
            continue
        element_names = {
            n
            for sub in ast.walk(node)
            if isinstance(sub, (ast.SetComp, ast.ListComp, ast.GeneratorExp))
            for n in _element_names(sub)
        }
        fifth_names = _fifth_element_names(node)

        # 写法 A：`op.execute(sa.text(<item>[4] 的推导式结果 …))` ——
        # 直接下标形态（iterable 本身出现在 execute 实参里）。
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)):
                continue
            if sub.func.attr != "execute":
                continue
            for arg in sub.args:
                if _contains_fifth_subscript(arg, element_names):
                    hits.append(
                        f"第 {sub.lineno} 行：`{ast.unparse(sub)}`"
                        f"（把 all_trigger_ddl 元素的 [4] 当 DROP FUNCTION 执行）"
                    )

        # 写法 B/C/D：第 5 项经名字传播后交给 op.execute
        for sub in ast.walk(node):
            if not (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "execute"
            ):
                continue
            for arg in sub.args:
                for inner in ast.walk(arg):
                    if isinstance(inner, ast.Name) and inner.id in fifth_names:
                        hits.append(
                            f"第 {sub.lineno} 行：`{ast.unparse(sub)}`"
                            f"（`{inner.id}` 承载 all_trigger_ddl 的第 5 项 —— 共用函数，不可删）"
                        )
    return hits


def _selfcheck_matcher() -> list[str]:
    """判据自身的阳性/阴性对照。

    五格（缺任何一格都可能让判据变成恒真或恒假的摆设）：

      A 阴性：真实迁移（docstring 引用旧写法、代码已修）→ **0 命中**；
      B 阳性：旧写法（推导式 `[4]`）放回**代码** → 必须命中；
      C 关键格：旧写法**只**出现在 **docstring** → 必须 **0 命中**
        （否则判据把"教训记录"当违规，假红淹没真红）；
      D 阳性：五元组解包形态 → 必须命中（判据不能只认推导式）；
      E 阳性：先赋值再迭代（`fns = {…}` → `for f in fns`）→ 必须命中
        （否则一行重构就能绕过判据）。
    """
    problems: list[str] = []

    # A：真实迁移（已修）
    for name in (
        "20260921_0345_cedfe85c1bf9_wp11_version_registry_and_dependencies.py",
        "20260921_1123_005e53eaed23_wp15_audit_and_notifications.py",
    ):
        path = MIGRATIONS_DIR / name
        if not path.exists():
            problems.append(f"对照 A 缺文件：{name}")
            continue
        if forbidden_sites_in_source(path.read_text(encoding="utf-8")):
            problems.append(f"对照 A 失败：已修的 {name} 仍被报违规（假红）")

    # B：推导式 [4] 在**代码**里
    buggy_code = (
        "def downgrade():\n"
        "    for drop_function in {item[4] for item in all_trigger_ddl()}:\n"
        "        op.execute(sa.text(drop_function))\n"
    )
    if not forbidden_sites_in_source(buggy_code):
        problems.append("对照 B 失败：推导式旧写法放回代码却**没**报红 —— 判据恒真")

    # C：旧写法只在 docstring 里（本仓 WP11/WP15 的真实形态）
    quoted = (
        "def downgrade():\n"
        '    """原写法（已废止，不要改回去）：\n'
        "\n"
        "        for drop_function in {item[4] for item in all_trigger_ddl()}:\n"
        "            op.execute(sa.text(drop_function))\n"
        '    """\n'
        "    for _t, _f, _td, _dt, drop_function in consistency_items():\n"
        "        op.execute(sa.text(drop_function))\n"
    )
    if forbidden_sites_in_source(quoted):
        problems.append(
            "对照 C 失败：旧写法**只**在 docstring 里也被报违规 ——"
            " 判据把教训记录当违规（假红淹没真红）"
        )

    # D：五元组解包
    buggy_unpack = (
        "def downgrade():\n"
        "    for _t, _f, _td, _dt, drop_function in all_trigger_ddl():\n"
        "        op.execute(sa.text(drop_function))\n"
    )
    if not forbidden_sites_in_source(buggy_unpack):
        problems.append("对照 D 失败：五元组解包形态没被报红 —— 覆盖不全")

    # E：先赋值再迭代
    buggy_alias = (
        "def downgrade():\n"
        "    fns = {item[4] for item in all_trigger_ddl()}\n"
        "    for f in fns:\n"
        "        op.execute(sa.text(f))\n"
    )
    if not forbidden_sites_in_source(buggy_alias):
        problems.append("对照 E 失败：先赋值再迭代的形态没被报红 —— 判据可被一行重构绕过")

    # F 阴性：合规写法（迭代 consistency_items）不许命中
    good = (
        "def downgrade():\n"
        "    for _t, _f, _td, _dt, drop_function in consistency_items():\n"
        "        op.execute(sa.text(drop_function))\n"
        "    for _t, _f, _td, drop_trigger, _df in all_trigger_ddl():\n"
        "        op.execute(sa.text(drop_trigger))\n"
    )
    if forbidden_sites_in_source(good):
        problems.append(
            "对照 F 失败：合规写法（第 1 步用 all_trigger_ddl 取 drop_trigger、"
            "第 2 步用 consistency_items）被误报 —— 判据会拦住正确实现"
        )

    return problems


def scan_migrations() -> tuple[list[str], int]:
    """扫 `migrations/versions/*.py`，返回 `(命中列表, 扫描的迁移数)`。

    范围由**磁盘**决定（`glob("*.py")`，排除 `__init__.py`）—— 铁律 14：
    新迁移自动进入覆盖，不需要有人记得回来加名字。
    """
    hits: list[str] = []
    count = 0
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        if path.name.startswith("__"):
            continue
        count += 1
        for hit in forbidden_sites_in_source(path.read_text(encoding="utf-8")):
            hits.append(f"{path.name}: {hit}")
    return hits, count


#: 注释里用来"点名守该不变式的测试文件"的形态：`` `test_xxx.py` ``，
#: 允许带目录前缀（`src/` 里 5 处写成 `` `tests/test_baseline_wp22.py` ``）。
#: ⚠️ **只认反引号包裹的整文件名**，不认裸词 —— 裸词里既有真文件名，也有
#: `*_evidence.py` 这类 glob 片段，收进来就是高假阳性（ISS-169 已判定：
#: "断言注释主题与被引用条目相符"的通用判据只能造成假红或恒真，是 ISS-95 形态）。
_GUARDRAIL_TEST_RE = re.compile(r"`(?:[A-Za-z0-9_./]*/)?(test_[A-Za-z0-9_]+\.py)`")

#: 守这条不变式的测试文件由**磁盘**决定（铁律 14），不写名字。
TESTS_DIR = REPO_ROOT / "services" / "backend" / "tests"

#: 接受本判据约束的**源码根**（`(根名, 路径)`；路径取自磁盘，不含文件名）。
#:
#: · `migrations/` —— 初版口径（ISS-169 第 3 次复发：WP11/WP15 的 `downgrade`
#:   docstring 逐字承诺"由 `test_ddl_downgrade_ownership.py` 静态守"，
#:   而那个文件**全仓不存在**）。
#: · `src/` —— 第 4 次复发后扩的口径。实测 20 处引用、**4 处悬空**，
#:   全部是真幽灵（`media/models.py` 1 / `media/schemas.py` 2 /
#:   `devtools/__init__.py` 1），**零假阳性**。
#:
#: ⚠️ **刻意不含 `tools/` 与 `tests/`**（两者都会自己咬自己）：
#:   · `tools/quality/check_migration_roundtrip.py` 的 docstring **必须**举
#:     `test_ddl_downgrade_ownership.py` 这类历史幽灵当反例 —— 它正是本判据的
#:     实现，收进来等于判据判自己。
#:   · `services/backend/tests/test_migration_roundtrip.py` 同样逐字保留该幽灵名
#:     作说明，**并自带阳性对照**（故意拿一个不存在的名字喂正则）—— 覆盖
#:     `tests/` 会把对照 fixture 判红，自相矛盾。
GUARDRAIL_SCAN_ROOTS: tuple[tuple[str, Path], ...] = (
    ("migrations", MIGRATIONS_DIR),
    ("src", REPO_ROOT / "services" / "backend" / "src"),
)


def scan_guardrail_test_references() -> tuple[list[str], dict[str, int]]:
    """注释里点名的护栏测试文件必须**真实存在**。

    返回 `(悬空引用列表, {根名: 该根命中的引用数})`。返回**逐根计数**而非一个
    合计数，是因为调用方要按根判非空（测试里的 ISS-143 锐化）：只报合计时，
    "某一根源码根静默失效"与"一切正常"同形。

    **为什么需要**：修 ISS-79 时人工发现 WP11/WP15 的 `downgrade` 文档逐字
    承诺"该不变式由 `test_ddl_downgrade_ownership.py` 静态守 + 真库往返动态
    守"，而那个文件**全仓不存在**；`versions/ddl.py` 也承诺了一个不存在的
    `test_versions_ddl_ownership.py`。这正是 ISS-169（弱引用）第三次复发：
    **文档声称有防线，而防线的入口打不开** —— 比没写更危险（ISS-95）。
    人工发现 ⇒ 说明此处无判据 ⇒ 补一条**窄口径**判据把它机械钉住。
    ISS-169 第 4 次复发（`src/` 侧 4 处）后，口径由 `migrations/` 扩到
    `GUARDRAIL_SCAN_ROOTS`。

    ⚠️ 口径仍然收窄，两点：
      · 通用"注释引用存在性"判据实测假阳性极高（`*_evidence.py` 是 glob 片段、
        `x.py` 是举例、`file.py::func` 要跨模块解析）—— 本仓实测 10 处精确引用
        里 2 处疑似假阳性，故**不做**通用版（ISS-169 的结论）。
      · 实际口径 = "反引号包裹的整文件名" ∩ "两个磁盘根"。它是**实测**出来的
        零假阳性边界，不是审美选择。

    ⚠️ **受判根内说明"历史幽灵"时不得加反引号**：写 `` `test_xxx.py` `` 就是
    重新声称一次引用。要在注释里提一个不存在的文件名，写**裸词**。
    这不是绕过判据的窍门，而是"反引号 = 引用"这个语义的必然结论。
    2026-10-07 实测：更正 `src/` 里的 4 处幽灵引用时，第一次正因为把幽灵名
    连同反引号抄进了新注释，被判据**正确判红** 3 处。
    """
    dangling: list[str] = []
    per_root: dict[str, int] = {}
    for root_name, root in GUARDRAIL_SCAN_ROOTS:
        count = 0
        for path in sorted(root.rglob("*.py")):
            # `migrations/versions/` 下的 `__init__.py` 不参与 —— 与
            # `scan_migrations()` 同款。⚠️ 只对 migrations 根生效：
            # `src/**/__init__.py` 是**正常文档位**，`devtools/__init__.py`
            # 的悬空引用正是靠不排除 `__init__.py` 才被抓到。
            if root_name == "migrations" and path.name.startswith("__"):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover
                continue
            # 只收 `Constant` 字符串（docstring / 字符串字面量）——`#` 行注释
            # 根本不进 AST，故行内注释里的引文不会被误收。
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Constant) and isinstance(node.value, str)
                ):
                    continue
                for m in _GUARDRAIL_TEST_RE.finditer(node.value):
                    count += 1
                    name = m.group(1)
                    if not (TESTS_DIR / name).is_file():
                        rel = path.relative_to(REPO_ROOT)
                        dangling.append(f"{rel}: `{name}` 不存在")
        per_root[root_name] = count
    return dangling, per_root


# ===========================================================================
# 第 2 段：真库往返
# ===========================================================================


def _recreate_database(admin_dsn: str, dbname: str) -> None:
    """DROP + CREATE 一个空库（先踢残留连接）。"""
    import psycopg

    with psycopg.connect(_psycopg_dsn(admin_dsn), autocommit=True) as conn:
        conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity"
            " where datname = %s and pid <> pg_backend_pid()",
            (dbname,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        conn.execute(f'CREATE DATABASE "{dbname}"')


def _enable_vector(dsn: str) -> None:
    """在目标库启用 pgvector（WP21 的迁移会断言它已启用）。"""
    import psycopg

    with psycopg.connect(_psycopg_dsn(dsn), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")


def _run_alembic(dsn: str, *args: str) -> tuple[int, str]:
    """在目标库跑一条 alembic 子命令。返回 `(rc, 输出尾部)`。"""
    import subprocess

    env = {**os.environ, "NO_PROXY": "*", "REDNOTE_DATABASE_URL": dsn}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "migrations/alembic.ini",
            *args,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-8:])
    return proc.returncode, tail


def _snapshot(dsn: str) -> dict:
    """读库里的函数 / 触发器 / 表（**只按存在性**取样，不比内容）。"""
    from sqlalchemy import create_engine, text

    engine = create_engine(dsn)
    try:
        with engine.begin() as conn:
            funcs = {
                r[0]
                for r in conn.execute(
                    text(
                        """
                        select p.proname
                          from pg_proc p
                          join pg_namespace n on n.oid = p.pronamespace
                         where n.nspname = 'public'
                           and p.proname like :pfx
                        """
                    ),
                    {"pfx": f"{FUNCTION_PREFIX}%"},
                ).fetchall()
            }
            triggers = {
                r[0]
                for r in conn.execute(
                    text(
                        """
                        select t.tgname
                          from pg_trigger t
                          join pg_class c on c.oid = t.tgrelid
                          join pg_namespace n on n.oid = c.relnamespace
                         where n.nspname = 'public'
                           and not t.tgisinternal
                        """
                    )
                ).fetchall()
            }
            tables = {
                r[0]
                for r in conn.execute(
                    text("select tablename from pg_tables where schemaname = 'public'")
                ).fetchall()
            }
    finally:
        engine.dispose()
    return {"functions": funcs, "triggers": triggers, "tables": tables}


def main() -> int:
    print("迁移往返判据：upgrade head → downgrade base → upgrade head（ISS-79）")
    print(f"  基准 DSN：{_base_dsn()}（库名会被替换为一次性临时库）")
    print()

    checks: list[tuple[str, bool, str]] = []
    notes: list[str] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    # ------------------------------------------------------------------ 第 1 段
    print("## [1] 静态：全部迁移的 downgrade 禁止'遍历 all_trigger_ddl 删函数'")
    selfcheck = _selfcheck_matcher()
    add(
        "static-matcher-selfcheck",
        not selfcheck,
        "；".join(selfcheck) if selfcheck else "阳性/阴性/引文六格全过（AST 匹配不受 docstring 引文影响）",
    )
    hits, mig_count = scan_migrations()
    add("static-scan-nonempty", mig_count > 0, f"扫描 {mig_count} 个迁移")
    add(
        "static-no-forbidden-downgrade",
        not hits,
        "；".join(hits) if hits else f"{mig_count} 个迁移的 downgrade 均未命中",
    )
    # 注释点名的护栏测试文件必须真实存在（ISS-169 弱引用，第 3 / 4 次复发）。
    dangling, refs_by_root = scan_guardrail_test_references()
    ref_total = sum(refs_by_root.values())
    add(
        "static-guardrail-test-refs-resolve",
        not dangling,
        "；".join(dangling)
        if dangling
        else f"{ref_total} 处护栏测试引用全部落在真实文件上",
    )
    print(f"  扫描 {mig_count} 个迁移；命中 {len(hits)} 处")
    print(
        "  护栏测试引用逐根："
        + " / ".join(f"{k}={v}" for k, v in refs_by_root.items())
        + f"（合计 {ref_total} 处）；悬空 {len(dangling)} 处"
    )
    if selfcheck:
        for p in selfcheck:
            print(f"  ⚠️ {p}")
    for h in hits:
        print(f"  ✗ {h}")
    for d in dangling:
        print(f"  ✗ {d}")
    print()

    # ------------------------------------------------------------------ 第 2 段
    print("## [2] 动态：私有临时库上的完整往返")
    from sqlalchemy.engine import make_url

    base = _base_dsn()
    url = make_url(base)
    if url.database is None:  # pragma: no cover
        print("[FAIL] 基准 DSN 里没有库名，无法定位管理连接")
        return 1

    dbname = f"rednote_roundtrip_{os.getpid()}"
    dsn = _dsn_for(dbname)
    admin = base  # 管理连接连到一个已存在的库（基准库本身）

    try:
        print(f"  [1/6] 重建空库 {dbname}")
        _recreate_database(admin, dbname)
        _enable_vector(dsn)

        print("  [2/6] upgrade head（第一次）")
        rc, tail = _run_alembic(dsn, "upgrade", "head")
        add("upgrade-head-1-ok", rc == 0, f"rc={rc}" if rc else "ok")
        if rc != 0:
            notes.append(f"upgrade head 失败：\n{tail}")
            raise _RoundtripAbort()
        head1 = _snapshot(dsn)
        notes.append(
            f"  head 第一次：函数 {len(head1['functions'])} / "
            f"触发器 {len(head1['triggers'])} / 表 {len(head1['tables'])}"
        )

        print("  [3/6] downgrade base（ISS-79 的直接判据）")
        rc, tail = _run_alembic(dsn, "downgrade", "base")
        add(
            "downgrade-base-ok",
            rc == 0,
            f"rc={rc}；{tail}" if rc else "ok",
        )
        if rc != 0:
            notes.append(f"downgrade base 失败：\n{tail}")
            raise _RoundtripAbort()
        base_state = _snapshot(dsn)
        notes.append(
            f"  base：函数 {sorted(base_state['functions'])} / "
            f"触发器 {len(base_state['triggers'])} / 表 {len(base_state['tables'])}"
        )

        shared = {_shared_trigger_function()}
        module_owned_at_head = head1["functions"] - shared

        # 非空性守卫：若函数取样失效，下面"全消失"会退化成恒真（ISS-143 族）。
        add(
            "head-has-module-owned-functions>=",
            len(module_owned_at_head) >= MIN_MODULE_OWNED_FUNCTIONS,
            f"head 自有函数 {len(module_owned_at_head)}（下界 {MIN_MODULE_OWNED_FUNCTIONS}）",
        )
        add(
            "shared-function-survives-base",
            shared <= base_state["functions"],
            f"base 上存活 {sorted(base_state['functions'])}；共用应为 {sorted(shared)}",
        )
        leaked = base_state["functions"] - shared
        add(
            "module-owned-functions-gone-at-base",
            not leaked,
            f"残留 {sorted(leaked)}" if leaked else f"{len(module_owned_at_head)} 个自有函数已全部删除",
        )
        add(
            "no-triggers-at-base",
            not base_state["triggers"],
            f"残留 {sorted(base_state['triggers'])}"
            if base_state["triggers"]
            else "触发器归零",
        )
        # 反向守卫：downgrade 必须真的改变了状态（否则"没跑"与"跑通了"同形）。
        add(
            "downgrade-actually-changed-state",
            base_state["tables"] != head1["tables"]
            and base_state["triggers"] != head1["triggers"],
            f"表 {len(head1['tables'])}→{len(base_state['tables'])}，"
            f"触发器 {len(head1['triggers'])}→{len(base_state['triggers'])}",
        )

        print("  [4/6] upgrade head（第二次，验证可还原）")
        rc, tail = _run_alembic(dsn, "upgrade", "head")
        add("upgrade-head-2-ok", rc == 0, f"rc={rc}；{tail}" if rc else "ok")
        if rc != 0:
            notes.append(f"第二次 upgrade head 失败：\n{tail}")
            raise _RoundtripAbort()
        head2 = _snapshot(dsn)
        notes.append(
            f"  head 第二次：函数 {len(head2['functions'])} / "
            f"触发器 {len(head2['triggers'])} / 表 {len(head2['tables'])}"
        )
        add(
            "head-state-restored",
            head2["functions"] == head1["functions"]
            and head2["triggers"] == head1["triggers"]
            and head2["tables"] == head1["tables"],
            "逐项还原"
            if head2 == head1
            else f"函数差 {head2['functions'] ^ head1['functions']}；"
            f"触发器差 {head2['triggers'] ^ head1['triggers']}；"
            f"表差 {head2['tables'] ^ head1['tables']}",
        )

    except _RoundtripAbort:
        pass
    finally:
        import psycopg

        print(f"  [6/6] 清理 {dbname}")
        try:
            with psycopg.connect(_psycopg_dsn(admin), autocommit=True) as conn:
                conn.execute(
                    "select pg_terminate_backend(pid) from pg_stat_activity"
                    " where datname = %s and pid <> pg_backend_pid()",
                    (dbname,),
                )
                conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        except Exception as exc:  # noqa: BLE001
            notes.append(f"  ⚠️ 清理 {dbname} 失败（{type(exc).__name__}）—— 需手工删")

    print()
    for n in notes:
        print(n)
    print()

    failures = [name for name, ok, _ in checks if not ok]
    #: 条数构成（防"崩溃伪装成通过"，ISS-43）：
    #: 静态 4 + 动态 9 = 13。
    expected = EXPECTED_CHECKS
    complete = len(checks) >= expected
    print("## [3] 结论")
    for name, ok, detail in checks:
        suffix = f"（{detail}）" if detail else ""
        print(f"  {'PASS' if ok else 'FAIL'} {name}{suffix}")
    print()
    print(
        f"RESULT={'PASS' if (not failures and complete) else 'FAIL'} "
        f"checks={len(checks)}/{expected} failures={len(failures)}"
    )
    return 0 if (not failures and complete) else 1


#: 本脚本应产出的 CHECK 条数下界（`>=` 语义）。
#: 静态段 4（自检 / 扫描非空 / 无命中 / **护栏测试引用可解析**）+ 动态段 9
#: （升级1 / 降级 / head 自有无下界 / 共用存活 / 自有清零 / 触发器归零 /
#:   状态真变 / 升级2 / 逐项还原）= **13**。
#: 它是 `>=` 语义：中途 abort 会让条数变少 ⇒ 与"全部通过"不再同形（ISS-43）。
#: ⚠️ 与 `collect_regression_evidence.py::EXPECTED_CHECKS` 同族的 ISS-153 陷阱：
#:    加静态判据时若忘了把 12 改成 13，`>=` 判据不会报红、静默通过。
EXPECTED_CHECKS = 13


class _RoundtripAbort(Exception):
    """往返中途不可继续（已记录成因，跳到清理）。"""


if __name__ == "__main__":
    sys.exit(main())