#!/usr/bin/env python
"""测试库目标自检：**默认 DSN 不许指向 PostgreSQL 系统维护库**（ISS-145）。

## 为什么需要这个

本仓所有测试与工具的默认 DSN 长期写成：

    postgresql+psycopg://rednote@localhost:5432/postgres

`postgres` 是 **PostgreSQL 的系统维护库**（`CREATE DATABASE` 的默认模板目标、
`psql` 不指定 `-d` 时的落点），**不是**项目库。实测代价（2026-09-29 现场）：

| 库 | 表数 | `projects` | `users` |
|---|---|---|---|
| `postgres`（系统维护库） | 54 | **74,145** | **119,720** |
| `rednote_dev`（真正的开发库） | 50 | 8 | — |

系统库里堆了 **665,951 行**测试垃圾，跨 9 天累积，横跨 54 张业务表
（`brands` 76,222 / `products` 72,667 / `version_nodes` 46,629 …）。
直接后果：全量 pytest 从秒级退化到 13~20 分钟（WP24 在干净库上只需 9s，
差约 **130 倍**），且它还被当成"集成环境"来做诊断 —— **而它其实是垃圾场**。

## 为什么既有判据看不见它

`tools/quality/check_test_db_isolation.py`（ISS-108）判的是
**「DSN 是否流向 `REDNOTE_TEST_DATABASE_URL`」**。而那些字面量**确实流向了**
环境变量（形态是 `os.environ.get(ENV, "…/postgres")`）—— 它判的是
**"能不能被覆盖"**，不是**"默认值指向哪里"**。

两者是**两条独立事实**：

* ISS-108：硬编码 → 覆盖不掉 → CI 上 `role "rednote" does not exist`（**可诊断性**）
* ISS-145：默认值指向系统库 → 本机开发者不设环境变量就污染系统库（**默认值安全性**）

一条合规不蕴含另一条合规。本判据补的是后者。

## 判据方向：**反向覆盖**（与 ISS-144 同纪律）

遍历「**代码里实际写着的** DSN 字面量」，要求「**它的库名不是保留系统库**」。
⚠️ 不能反过来遍历「一份保留名单再去找它出现在哪」—— 漏掉的 DSN
（新写的、换个变量名的）根本不参与比较，那是 **ISS-115 的形态**：
判据对"漏掉"恒真。

## 覆盖范围由磁盘决定（铁律 14）

* **第一段**：`services/backend/tests/**/*.py` —— `rglob` 递归，**不写目录名单**。
* **第二段**：从 `.github/workflows/ci.yml` 机械提取 `python <路径>.py` 引用的
  仓库内 `tools/**` 脚本 —— 与 `check_test_db_isolation.py` **同一口径**，
  理由同：CI 会跑的脚本，其默认 DSN 同样会被 CI 使用。
* **豁免是机械的**：`tools/m1/collect_wp*_evidence.py` 等**未被 CI 引用**的
  本机采集器不在范围内（口径同 ISS-108 判据：豁免依据是"CI 有没有引用"，
  不是名单）。
* 另有 `_count_py_files_independently` 用 `os.walk` **独立实现**数一遍，
  与 `rglob` 实扫数逐范围对账 —— 防铁律 14 第 5 次复发
  （`rglob` 写成 `glob` 只差一个字、写错静默，174 个源文件的导入全部不参与比较
  而判据照样打印 PASS）。

## 保留库名从哪来

保留集合 = `{postgres, template0, template1}`（见 `STATIC_RESERVED_DATABASES`）。
这三个是 **PostgreSQL 自身固定的名字**，不是"本项目的名单"：

* `postgres` —— 系统维护库（`CREATE DATABASE` 的默认连接目标、`psql` 的默认落点）；
* `template0` / `template1` —— 模板库，`datistemplate = true`。

**本判据刻意不连数据库**：它是纯静态检查，要能在 CI 门 1（"便宜且早"）
或不装任何依赖的环境里跑。但"不连库"带来一个风险 ——
**这份静态名单会不会与真实 PG 的模板库集合漂移？**

因此**另有一条判据对账它**（铁律 14："覆盖范围由人写就必须另有一条判据对账"）：

    services/backend/tests/test_test_database_target.py
      test_static_reserved_databases_covers_all_pg_templates()

它连真库读 `pg_database.datistemplate`，断言
**每一个模板库名都出现在 `STATIC_RESERVED_DATABASES` 里**。
静态名单过期（PG 新增模板库、或有人删掉一个名字）时会报红，
而不是静默漏判。

## ⚠️ 已知边界（不做假保证）

* 只判**字面量**。用变量拼出来的 DSN（`f"{host}/{dbname}"`）扫不到。
  本仓实测该形态为零；引入时需扩充。
* 只判**库名**。指向保留库但换端口的写法（`@localhost:5433/postgres`）
  仍会被判出（库名相同），这是**有意的**：库名才是"是不是系统库"的判据。
* **白名单**（`ALLOW_FILES`，逐条给理由）：两个文件里的保留库 DSN 是
  **被测数据**而非配置 —— 见 `ALLOW_FILES` 的注释。豁免按文件，但**每条都写明理由**，
  否则白名单会退化成"把报红的地方都加进来"（与 `check_test_db_isolation` 同纪律）。

跑法：
    python tools/quality/check_test_database_target.py
退出码 0 = 无违规。
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "services" / "backend" / "tests"
CI_FILE = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: 保留库名 —— **静态那一半**。动态那一半从 `pg_database` 现场读写（见模块 docstring）。
#:
#: * `postgres`：PostgreSQL 的系统维护库，`CREATE DATABASE` 与 `psql` 的默认落点。
#: * `template0` / `template1`：模板库，`datistemplate=true`，正常也不该被连。
#:
#: 这三个是 PostgreSQL 自身固定的名字，属于语言/产品事实，不是"本项目的名单"。
STATIC_RESERVED_DATABASES: frozenset[str] = frozenset(
    {"postgres", "template0", "template1"}
)

#: 从 ci.yml 里提取 `python <路径>.py` 的脚本路径（机械取范围，与 ISS-108 判据同口径）。
_CI_SCRIPT_RE = re.compile(r"\bpython\s+([\w./\-]+\.py)")

#: 独立实现的对账下界：测试目录下 `.py` 文件数不得少于这个值。
#: 若低于它，说明目录结构变了而本判据的遍历方式没跟上 —— **报红并指名**，
#: 而不是静默地少扫几个文件还打印 PASS。
MIN_TEST_FILES = 20

#: 独立实现的对账下界：**实际判定**的 DSN 字面量数不得少于这个值。
#:
#: 为什么需要它：本判据有两处"合法的减免"——`ALLOW_FILES` 白名单、
#: `parametrize` 位置豁免。若哪天它们过度膨胀，判据会**扫得到、判得少**，
#: 仍然打印漂亮的 `[PASS]`。这个下界把"空转"变成可见的失败。
#: 实测当前值 21，留 1 的余量。
MIN_JUDGED_LITERALS = 20

#: 逐文件白名单：文件名 → 理由。**新增条目必须写清理由**，
#: 否则这个白名单会退化成"把报红的地方都加进来"。
#:
#: 两个文件里的保留库 DSN 是**被测数据**，不是配置：
#:   · `test_health.py` —— DSN 指向 `127.0.0.1:1`（不可达端口），
#:     测的是"连不上时健康检查如何报错"。库名是 `postgres` 只因它是随手写的地址，
#:     改成 `rednote_test` 反而会让这条用例的意图变得不清晰（它压根没打算连上）。
#:   · `test_errors.py` —— DSN 是**被测数据**，测"错误详情里的连接串会被打码"。
ALLOW_FILES: dict[str, str] = {
    "test_health.py": "DSN 指向不可达端口，测错误处理路径（库名无实际含义）",
    "test_errors.py": "DSN 是被测数据 —— 测「错误详情里的连接串会被打码」",
}


def _is_dsn_literal(node: ast.AST) -> str | None:
    """节点是 `postgresql[...]://…` 字面量则返回它，否则 None。

    ⚠️ 必须要求 `://`（与 ISS-108 判据同一条教训）：早期版本只判前缀 `postgresql+`，
    会把**驱动名** `"postgresql+psycopg"` 当成 DSN。
    """
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return None
    v = node.value
    if "://" not in v:
        return None
    if v.startswith("postgresql://") or v.startswith("postgresql+"):
        return v
    if v.startswith("postgres://"):
        return v
    return None


def database_name(dsn: str) -> str | None:
    """从 DSN 取**库名**。取不到返回 None（调用方当成"判据不适用"并计入覆盖）。

    用 `urlparse` 而不是正则：`+` 在 scheme 里合法（`postgresql+psycopg`），
    `urlparse` 能正确切出 `netloc` / `path`。库名是 `path` 去掉前导 `/`。

    ⚠️ 不要 `import` sqlalchemy 来做这件事 —— 本判据要能在**没装依赖**
    的静态检查场景下跑（CI 门 1 的定位就是"便宜且早"）。
    """
    try:
        parsed = urlparse(dsn)
    except ValueError:
        return None
    path = parsed.path or ""
    name = path.lstrip("/").split("/")[0].split("?")[0]
    return name or None


def _count_py_files_independently(base: Path) -> int:
    """用 **`os.walk`** 独立实现数一遍 `.py` 文件。

    ⚠️ **必须是独立实现**。写成 `len(list(base.rglob("*.py")))` 与判据内部的
    `for path in base.rglob("*.py")` 是**同一个 API 的两种写法**，会**一起缩水**、
    对账恒等、永不报红 —— 那正是 ISS-137 的形态
    （`sum(x) + (total - sum(x)) == total` 对任何实现都成立）。
    `os.walk` 基于 `os.scandir`，是另一套遍历机制，才构成真对账。
    """
    count = 0
    for _dirpath, _dirnames, filenames in os.walk(base):
        count += sum(1 for name in filenames if name.endswith(".py"))
    return count


def _ci_referenced_scripts() -> tuple[list[Path], list[str]]:
    """从 `ci.yml` 机械提取被 `python <路径>.py` 引用的仓库内 `tools/` 脚本。

    **范围现场派生**，不维护名单 —— 新脚本一进 `ci.yml` 就自动被覆盖。
    口径与 `check_test_db_isolation.py::_ci_referenced_scripts` 一致
    （豁免依据是"CI 有没有引用"，不是文件名）。
    """
    notes: list[str] = []
    if not CI_FILE.exists():
        return [], [f"  ⚠️ 找不到 {CI_FILE}，第二段未执行"]

    text = CI_FILE.read_text(encoding="utf-8")
    found: dict[Path, bool] = {}
    missing: list[str] = []
    for match in _CI_SCRIPT_RE.finditer(text):
        rel = match.group(1)
        path = (REPO_ROOT / rel).resolve()
        try:
            inside = path.relative_to(REPO_ROOT)
        except ValueError:
            continue
        if inside.parts[:1] != ("tools",):
            continue
        if path.is_file():
            found[path] = True
        else:
            missing.append(rel)
    if missing:
        notes.append(
            "  ⚠️ ci.yml 引用了不存在的脚本："
            + "，".join(sorted(set(missing)))
            + " —— 这些脚本的目标判据**无法执行**（不是通过）"
        )
    return sorted(found), notes


def _parametrize_literal_ids(tree: ast.Module) -> set[int]:
    """收集**作为 `pytest.mark.parametrize` 实参**出现的 DSN 字面量的 `id()`。

    这些字面量是**被测数据**（判据自己的单元测试要验证 `postgres` 这种库名
    能被正确解析），不是可执行的默认配置。**按位置豁免，不按文件名豁免** ——
    白名单式豁免会让判据自己的测试文件成为"法外之地"，
    而它恰恰是最该被判的文件之一（与 `check_test_db_isolation._PATTERN_METHODS`
    同一条纪律）。

    ⚠️ 用 `id()` 而不是行号：同一行可能有多个字面量，
    行号法会把同一行上的真硬编码一起豁免掉（"顺手放过"的经典形态）。
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # 形态：`pytest.mark.parametrize(...)` → Attribute(attr="parametrize")
        if not (isinstance(func, ast.Attribute) and func.attr == "parametrize"):
            continue
        for arg in node.args:
            for sub in ast.walk(arg):
                if _is_dsn_literal(sub) is not None:
                    out.add(id(sub))
    return out


class Finding:
    __slots__ = ("path", "lineno", "line", "database", "dsn")

    def __init__(self, path: Path, lineno: int, line: str, database: str, dsn: str) -> None:
        self.path, self.lineno = path, lineno
        self.line, self.database, self.dsn = line, database, dsn

    def __str__(self) -> str:
        """打印一行日志**不许**成为崩溃原因（ISS-102 同族）。

        仓外路径（`/tmp/...` 的单元自测样本）调 `relative_to` 会抛 `ValueError`
        → **判据整体崩溃、一条结论都不产生，与"全部通过"在计数上同形**。
        """
        try:
            rel = self.path.relative_to(REPO_ROOT)
        except ValueError:
            rel = self.path
        return (
            f"{rel}:{self.lineno}: 默认 DSN 指向保留库 '{self.database}' → {self.dsn}\n"
            f"      {self.line.strip()}"
        )


def _scan_file(path: Path, findings: list[Finding]) -> tuple[int, int, int]:
    """扫一个文件。返回 `(DSN 字面量数, 豁免数, 实际判定数)`。

    三个数必须分开统计：**"扫到了 N 个字面量但一个都没判"**
    与 **"扫到了且全部合规"** 在 findings 为空时**同形** ——
    那正是 ISS-143 的"没跑 vs 跑失败同形"。分开回报才能让
    "豁免把范围吃光了"这件事**可见**。
    """
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        findings.append(
            Finding(path, exc.lineno or 0, "", "(语法错)", f"无法解析：{exc.msg}")
        )
        return 0, 0, 0

    total = 0
    exempt = 0
    judged = 0
    exempt_ids = _parametrize_literal_ids(tree)
    for node in ast.walk(tree):
        dsn = _is_dsn_literal(node)
        if dsn is None:
            continue
        total += 1
        # 位置豁免：作 parametrize 实参 → 是被测数据，不是默认配置。
        if id(node) in exempt_ids:
            exempt += 1
            continue
        db = database_name(dsn)
        if db is None:
            continue
        judged += 1
        if db.lower() not in STATIC_RESERVED_DATABASES:
            continue
        lineno = node.lineno
        line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        # 注释行里出现 DSN 是说明文字，不是可执行配置。
        if line.lstrip().startswith("#"):
            continue
        findings.append(Finding(path, lineno, line, db, dsn))
    return total, exempt, judged


def scan() -> tuple[list[Finding], int, int, int, int, int, list[str]]:
    """返回 `(findings, 测试文件数, 独立计数, 字面量数, 豁免数, 判定数, notes)`。"""
    findings: list[Finding] = []
    notes: list[str] = []
    literal_total = 0
    exempt_total = 0
    judged_total = 0

    # ---------------------------------------------------------------- 第一段
    test_files = sorted(TESTS_DIR.rglob("*.py"))
    independent = _count_py_files_independently(TESTS_DIR)
    if len(test_files) != independent:
        notes.append(
            f"  ❌ 覆盖范围对账失败：rglob 扫到 {len(test_files)} 个 .py，"
            f"os.walk 独立数出 {independent} 个 —— 遍历方式与磁盘不一致"
            f"（铁律 14 第 5 次复发形态：`rglob` 写成 `glob` 只差一个字、写错静默）"
        )
        findings.append(
            Finding(TESTS_DIR, 0, "", "(覆盖范围)", f"rglob={len(test_files)} vs os.walk={independent}")
        )
    if len(test_files) < MIN_TEST_FILES:
        notes.append(
            f"  ❌ 测试目录只有 {len(test_files)} 个 .py（下界 {MIN_TEST_FILES}）"
            f" —— 目录结构变了而本判据没跟上，少扫是静默的"
        )
        findings.append(
            Finding(TESTS_DIR, 0, "", "(覆盖范围)", f"{len(test_files)} < {MIN_TEST_FILES}")
        )

    for path in test_files:
        if path.name in ALLOW_FILES:
            notes.append(f"  白名单跳过 {path.name} —— {ALLOW_FILES[path.name]}")
            continue
        total, exempt, judged = _scan_file(path, findings)
        literal_total += total
        exempt_total += exempt
        judged_total += judged

    # ---------------------------------------------------------------- 第二段
    scripts, script_notes = _ci_referenced_scripts()
    notes.extend(script_notes)
    for path in scripts:
        rel = path.relative_to(REPO_ROOT)
        notes.append(f"  CI 引用 {rel} —— 按工具脚本口径判定")
        total, exempt, judged = _scan_file(path, findings)
        literal_total += total
        exempt_total += exempt
        judged_total += judged

    # 覆盖范围本身也要有下界：把"判定数少到没意义"变成可见的失败，
    # 而不是一个漂亮的 [PASS]（ISS-137 形态：判据对自己的空转恒真）。
    if judged_total < MIN_JUDGED_LITERALS:
        notes.append(
            f"  ❌ 实际判定的 DSN 字面量只有 {judged_total} 个"
            f"（下界 {MIN_JUDGED_LITERALS}）—— 覆盖被豁免/解析失败吃掉了"
        )
        findings.append(
            Finding(
                REPO_ROOT,
                0,
                "",
                "(覆盖范围)",
                f"judged={judged_total} < {MIN_JUDGED_LITERALS}",
            )
        )

    return (
        findings,
        len(test_files),
        independent,
        literal_total,
        exempt_total,
        judged_total,
        notes,
    )


def main() -> int:
    findings, test_count, independent, literals, exempt, judged, notes = scan()

    print("测试库目标自检（ISS-145：默认 DSN 不许指向系统维护库）")
    print(f"  第一段：{test_count} 个测试文件（rglob）/ {independent} 个（os.walk 独立对账）")
    print("  第二段：CI 引用的 tools 脚本（范围现场读 ci.yml 派生）")
    print(
        f"  DSN 字面量：{literals} 个 = 豁免 {exempt}（parametrize 实参，被测数据）"
        f" + 判定 {judged}（解析不出库名的按「不适用」跳过）"
    )
    for note in notes:
        print(note)
    print()

    if findings:
        print(f"[FAIL] 发现 {len(findings)} 处违规：\n")
        for f in findings:
            print(f"  {f}")
        print(
            "\n合规形态：默认 DSN 的**库名**不得是保留库\n"
            f"  · 保留库（静态）：{' / '.join(sorted(STATIC_RESERVED_DATABASES))}\n"
            "    ⚠️ 另有判据对账它：test_test_database_target.py 的\n"
            "       test_static_reserved_databases_covers_all_pg_templates\n"
            "\n修法：把默认值改成专用测试库，例如\n"
            '  DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"\n'
            "\n理由：`postgres` 是 PostgreSQL 的系统维护库。"
            "把它当测试库的默认值，会让**不设环境变量**的开发者直接污染系统库 ——"
            "实测已累积 665,951 行测试垃圾、54 张业务表，"
            "并让全量 pytest 从 9s 退化到 13~20 分钟。"
        )
        return 1

    print(
        f"[PASS] 无违规：{test_count} 个测试文件 + {len(_ci_referenced_scripts()[0])} 个"
        f" CI 引用脚本的默认 DSN 都不指向保留库"
        f"（{literals} 个字面量 = 豁免 {exempt} + 判定 {judged}）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
