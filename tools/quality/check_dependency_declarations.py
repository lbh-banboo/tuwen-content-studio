#!/usr/bin/env python
"""依赖声明一致性自检：**代码 import 的第三方发行包，必须在 `pyproject.toml` 里有声明**。

## 为什么需要这个

`services/backend/pyproject.toml` 长期只声明 **7** 个直依赖（其中 `uvicorn` 并不被 import ——
它是 `python -m uvicorn rednote.main:app` 的启动入口），而 `src/` 真正需要声明的第三方发行名
远多于此（顶层硬导入 + 函数体内延迟导入两类）。**覆盖范围的计数一律由本工具在运行输出里
现场派生**（见「src 导入分布」一行），**不写死在本注释里** —— 写死的计数会随后续开发漂移，
而**漂移了不会报错**（判据纪律第 6 条 / ISS-78：说明与证据里的元信息必须现场派生）。

> **本注释本身踩过这个坑（2026-09-29 自查）**：初版此处写的是「`src/` 实际顶层硬导入
> **12** 个第三方包」，而现场实测是 **9** 个。这个错误的数字**没有任何判据会去对账**
> —— 它安静地活在一段"讲为什么需要判据"的说明里，极具讽刺。修法就是本节的做法：
> 计数**不写死**，改由运行输出派生。
>
> 修之前，`src` 需要声明而未声明（或放错作用域）的共 **8** 个发行名 —— 剩下的靠
> `sync-kit/02-落地/install.sh` 里一段注释为「★ 关键补丁」的循环兜底补装。
> 这个形态有三个独立的失效面：

  · **干净环境按声明装完仍跑不起来**：缺 `cryptography` 时
    `alembic upgrade head` 直接 `ModuleNotFoundError` 崩掉（sync-kit 沙盒演练实测）。
    也就是"照着文档做"的路径在**第一步就断**。
  · **CI 靠运气**：`.github/workflows/ci.yml` 跑的是
    `pip install -e "services/backend[dev]"`，它**从不读 install.sh**。
    它今天能绿，是因为 GitHub runner 恰好装了那些包 —— 不是因为有声明。
  · **注释在说谎**：`modules/parsing/parsers/{office,pdf,image}.py` 共 **4 处**写着
    `except ImportError as exc:  # pragma: no cover - 依赖已在 pyproject 锁定`，
    而 `python-docx` / `openpyxl` / `pypdf` / `Pillow` 在 pyproject 里**一个都没有**。
    这正是本项目判据纪律第 9 条：「声称自己有防线，比没有防线更危险」——
    注释让读者以为依赖有保障，于是没人再复核。

## 判据方向：**必须反向**（这是本工具唯一的重点）

错误方向（**恒真式，与 ISS-115 同族**）：

    for dist in declared:            # ← 只遍历"已声明的"
        assert installed(dist)       # 漏声明的那 7 个**根本不参与比较** ⇒ 永远 PASS

该方向对"漏声明"恒真："通过"与"根本没判"在输出上同形，所以永不失败、永不报警。

正确方向（**代码需要什么 → 声明里必须有**）：

    for imp, files in scan_imports(src):     # ← 遍历"代码真正 import 的"
        assert MAPPING.get(imp) in declared  # 漏一个即 FAIL，并指出包名与出处文件

`src/` 是**独立的一侧**，不因 pyproject 写没写而改变 —— 这就是它能抓住漏声明的原因。

## 三段判定（按"作用域"分桶，不合并）

| 段 | 扫描范围 | 必须声明在 |
|---|---|---|
| ① | `services/backend/src/**` 的导入 | `dependencies`（**不许**只在 `[dev]`） |
| ② | `services/backend/tests/**` 的导入 | `dependencies` **或** `[dev]` |
| ③ | `tools/**` + `migrations/**` 的导入 | 同 ②（迁移由部署方执行，随运行时依赖安装） |

**第 ① 段的分桶不是形式主义**，它抓到过一个真实缺陷：`httpx` 曾只声明在 `[dev]` 里，
而 `modules/identity/oidc.py:44` 是**生产代码顶层** `import httpx`。
⇒ `pip install .`（不带 `[dev]`）装完，身份模块一 import 即崩。
而本机与 CI **都装 `[dev]`**，所以两边都测不出来 —— 只有"按作用域分桶"能发现它。

## 已知盲区（**显式记录，不做假保证**）

本工具是 **AST 静态扫描**，因此**扫不到动态导入**：
`importlib.import_module("x")` / `__import__("x")` / `exec("import x")`。

本仓实测：`services/backend/src` 与 `migrations` 下**零** `importlib.import_module`
调用（`grep -rn "import_module\\|__import__"` 见证据）。
⇒ 当前盲区不影响结论。**但这不等于"100% 覆盖"** ——
将来若引入插件式加载，必须同步扩充本工具（或在加载点登记白名单）。
**不声称自己覆盖了动态导入**，这是本工具的能力边界。

## 覆盖范围的对账（**铁律第 14 条的第 5 次复发，必须堵**）

"覆盖范围由人写"在本仓已现身 5 次：ISS-104（写死目录）→ ISS-111（只换藏身处）
→ ISS-139（glob 只扫 `tools/m1/`）→ ISS-143（复跑载体把跨包脚本路径写死）
→ ISS-155（`LOCAL_IMPORT_NAMES` 又是一份人写名单）。
本工具**本身就是第 5 个候选**：`_scan_imports` 的覆盖范围由 `rglob` 决定 ——
而"递归 vs 非递归"只是**一个字**（`rglob` / `glob`），**写错是静默的**。

**实测后果**（本机 `find` 取证）：本仓若把 `rglob` 写成 `glob`：

| 范围 | 递归 | 非递归 | 缩水 |
|---|---|---|---|
| `services/backend/src` | 174 | **0** | 全部（代码都在 `rednote/` 子目录下） |
| `tools` | 36 | **0** | 全部 |
| `migrations` | 18 | 1 | 17 |

⇒ 174 个源文件里的**全部导入都不参与比较**，判据**照样打印 `[PASS]`**。
"没扫到"与"扫到了且合规"在输出上同形 —— 与 ISS-143 的"没跑 vs 跑失败同形"同族。

因此本工具对**每个扫描范围**都做一次**独立实现对账**（纪律 11：期望值必须由
**独立路径**算出，不能是同一条语句的两种写法）：`os.walk` 数一遍 `.py`，
再与 `Path.rglob` 实际扫过的文件数逐范围比较，**不等即报红并指名是哪个范围**。

## 运行

    python tools/quality/check_dependency_declarations.py      # 期望 exit 0
"""

from __future__ import annotations

import ast
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
#: `tools/quality/<this>.py` → 仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "services" / "backend"
PYPROJECT_PATH = BACKEND_DIR / "pyproject.toml"

SRC_DIR = BACKEND_DIR / "src"
TESTS_DIR = BACKEND_DIR / "tests"
TOOLS_DIR = REPO_ROOT / "tools"
MIGRATIONS_DIR = REPO_ROOT / "migrations"

# --------------------------------------------------------------------------
# 导入名 → 发行名（PyPI 上的规范名；比较前统一小写并把 `_` / `.` 折成 `-`）
#
# ⚠️ **不在这张表里的导入名一律 FAIL**，不会静默跳过。
#    这是刻意的：静默跳过会让"新依赖写错名字"变成"看起来通过"。
#    要放行新的第三方导入名，必须在此登记（这本身就是一次有意识的声明）。
#
# ⚠️ 别名是**实测**得到的（importlib.metadata.packages_distributions()），
#    不是凭记忆写的：`import docx` 的发行名是 `python-docx`、
#    `import PIL` 是 `Pillow`。写成 `{"docx": "docx"}` 会指向 PyPI 上一个
#    同名废弃包 —— 那是"看起来有、实际指错包"。
# --------------------------------------------------------------------------
IMPORT_TO_DISTRIBUTION: dict[str, str] = {
    # --- 运行时：顶层硬导入 ---
    "fastapi": "fastapi",
    "starlette": "starlette",
    "pydantic": "pydantic",
    "pydantic_settings": "pydantic-settings",
    "sqlalchemy": "SQLAlchemy",
    "psycopg": "psycopg",
    "alembic": "alembic",
    "cryptography": "cryptography",
    "pgvector": "pgvector",
    "httpx": "httpx",
    "uvicorn": "uvicorn",
    "multipart": "python-multipart",
    # prometheus_client：WP47 ① 的 `/metrics` 出口（observability/metrics.py 顶层导入）。
    #   导入名与发行名逐字相同（实测 `packages_distributions()["prometheus_client"]
    #   == ["prometheus_client"]`），**不是** `docx`/`PIL` 那类需改名映射的情形 ——
    #   登记它是为了满足"每个第三方导入名都必须在表里有一条"的完备性要求，
    #   而不是为了纠别名。PyPI 规范名（`prometheus-client`）由 `_norm()` 折平比较。
    "prometheus_client": "prometheus_client",
    # --- 运行时：解析器函数内延迟导入 ---
    "docx": "python-docx",
    "openpyxl": "openpyxl",
    "pypdf": "pypdf",
    "PIL": "Pillow",
    # --- 仅测试 ---
    "pytest": "pytest",
    # --- 仅 M0 探针（tools/m0/wp05_smoke/smoke.py，函数内 import）---
    "boto3": "boto3",
    "botocore": "botocore",
}

#: 本项目自己的顶层包名 —— 出现在 import 里不算第三方依赖。
LOCAL_TOP_LEVEL: frozenset[str] = frozenset({"rednote"})

#: 本地导入名的**派生根**：这三处下的 `.py` / 包目录，名字都可能以 import 形式出现
#: （`tests/conftest.py` 的 `sys.path` 注入把 `tools/quality` 也放进了解析路径）。
#: `src/**` 的本地包以 `rednote.` 前缀出现，由 `LOCAL_TOP_LEVEL` 单独处理。
_LOCAL_IMPORT_SCAN_ROOTS: tuple[Path, ...] = (TOOLS_DIR, MIGRATIONS_DIR, TESTS_DIR)

#: 显式豁免的本地导入名 → 理由（**逐条给理由**）。
#: 当前为空 —— 磁盘派生已覆盖全部本地名。保留这个口子是为了应对
#: "名字以 import 形式出现、但磁盘上没有对应文件"的特例（如运行时注入的模块）。
#: ⚠️ 只登记**当前确实需要**的项；写一条永不生效的登记就是装饰（ISS-72 同族）。
LOCAL_IMPORT_EXEMPT: dict[str, str] = {}


def _discover_local_import_names() -> frozenset[str]:
    """扫出"以 import 形式出现、其实是本仓自己的文件/包"的名字（**由磁盘决定**）。

    ## 为什么必须派生，不能写名单（铁律 14 的第 6 次复发）

    本字段初版是一份**人写的四个名字**的名单（`conftest` +
    三个 `check_*`）。它当时是对的，但**每新增一个"测试 import 本地工具脚本"
    就要手工补一行**，漏了就报红 —— 而报红的形态是"某本地模块缺发行包"，
    指向完全无关的方向。实测已复发两次：

      · 2026-09-30 修 ISS-158 新增 `tests/test_test_db_isolation.py`，
        它 `import check_test_db_isolation` ⇒ 判据报 `未登记映射`；
      · 更早的 ISS-155 是同一根因族（本包三个新测试文件触发 CI 门 3 报 3 处红）。

    "覆盖范围由人写"在本仓已现身 6 次：ISS-104 → ISS-111 → ISS-139 → ISS-143
    → ISS-155 → 本条。修法一律相同：**改由磁盘决定，并另设判据对账它**
    （对账见 `scan()` 里的 `<LOCAL-NAMES-MISMATCH>` / `<LOCAL-NAMES-SHADOW-DEP>`）。

    ## 派生规则（用 `os.walk`，与 `_scan_imports` 的 `rglob` 解耦）

    * 三个根下的 `<stem>.py` → `stem`（`__init__.py` 除外）；
    * 含 `__init__.py` 的**包目录** → 目录名。

    刻意**不复用** `_scan_imports` 的遍历：若共用一条 `rglob`，将来
    "扫描退化"会把派生集合一起打塌，对账就失去独立来源（纪律 11）。
    """
    names: set[str] = set()
    for base in _LOCAL_IMPORT_SCAN_ROOTS:
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for filename in filenames:
                if filename.endswith(".py") and filename != "__init__.py":
                    names.add(filename[: -len(".py")])
            if "__init__.py" in filenames:
                names.add(Path(dirpath).name)
    names |= set(LOCAL_IMPORT_EXEMPT)
    return frozenset(names)


#: 非第三方发行包、但会以 import 形式出现的名字 —— **由磁盘派生**（见上）。
LOCAL_IMPORT_NAMES: frozenset[str] = _discover_local_import_names()

#: 白名单：出现过但**不判定**的导入名 → 理由。
#: 当前为空 —— 全部导入名都能映到发行名或已知本地模块。
#: （纪律：白名单必须逐条给理由，与 `check_undefined_globals._IMPLICIT_GLOBALS` 同款。）
IMPORT_ALLOWLIST: dict[str, str] = {}

#: 已声明在 `[project.dependencies]`、但代码里**没有** import 的包 → 理由。
#: 这不是错误（见 `scan()` 里的说明），登记理由只是为了防止"多余声明"被无视。
#:
#: ⚠️ 只登记**当前确实未触发**的项 —— 写一条永不生效的登记就是装饰
#:    （与"阈值长期小于实测值"（ISS-72）同族）。新增前先跑一次确认它真会打印。
DECLARED_NOT_IMPORTED_ALLOWLIST: dict[str, str] = {
    # 实测：本机跑一次会打印这一条。uvicorn 由 `python -m uvicorn` 启动，**不经 import**，
    # 但它是部署入口的一部分（README / sync-kit 备忘 的启动命令），
    # 删掉会让"照文档启动"失败 —— 与"代码 import 才声明"同样重要。
    "uvicorn": "以 `python -m uvicorn rednote.main:app` 启动，不经 import；是部署入口依赖",
}

#: **本工具必须产出的 CHECK 条数下界**（防"崩溃伪装成通过"，ISS-43：
#: 脚本中途抛异常时"一条 CHECK 都没产生"与"全过"的失败计数都是 0）。
#:
#: ⚠️ 必须是**与依赖数量无关的固定值**。反例（本包实测踩到）：
#:    初版把"每个已声明依赖"也各计一条 CHECK，于是删掉一个声明会让总数 -1，
#:    **"条数不足"守卫抢先报警，打出"脚本可能中途未跑完"，把真正的诊断
#:    「cryptography 未声明」整段盖掉** —— 判据自己成了噪音源。
#:    这是 ISS-72（阈值长期小于实测值 ⇒ 防线变装饰）的镜像变体：
#:    **阈值与一个会变动的量绑定 ⇒ 防线变成误报源**。
#:
#: 固定构成：pyproject 可解析 1 + 映射表自检 1 + 四段扫描各 1（4）
#:         + 本地名派生对账 1 + 反向覆盖判据 1 + 互补判据（声明方向）1 = **9**。
#: 它**不随 `dependencies` 条数变化**，因此任何变动都只由语义判据负责报红。
#:
#: 覆盖范围对账（`_count_py_files_independently`）**不单独计条数** ——
#: 它属于"四段扫描"那 4 条里的附带断言，但它产生的 Finding 会走同一条报红路径。
#: 刻意不单独计数：若给它单独计一条，"扫描缩水"就会同时改条数，
#: 又回到"条数守卫抢先报警"的老坑（见上一段）。
EXPECTED_CHECKS: int = 9

# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """一处违规。`scope` 用来说明它属于哪一段，便于定位。"""

    scope: str
    import_name: str
    detail: str
    files: tuple[str, ...]

    def render(self) -> str:
        where = "，".join(self.files[:3])
        more = f" 等 {len(self.files)} 处" if len(self.files) > 3 else ""
        # 合成标记（`<PARSE-ERROR:…>` / `<SCAN-COMPLETENESS:…>`）不是导入名，
        # 不加 `import ` 前缀 —— 否则读证据的人会以为代码里真有这么一行 import。
        if self.import_name.startswith("<"):
            return f"[{self.scope}] {self.import_name} —— {self.detail}（出处：{where}{more}）"
        return f"[{self.scope}] `import {self.import_name}` —— {self.detail}（出处：{where}{more}）"


def _norm(name: str) -> str:
    """PEP 503 规范化：小写 + `_` / `.` 折成 `-`（用于发行名比较）。"""
    return name.strip().lower().replace("_", "-").replace(".", "-")


def _strip_specifier(entry: str) -> str:
    """`"uvicorn[standard]==0.30.6"` → `"uvicorn"`。只取名字，不解析版本。"""
    head = entry.split("[")[0]
    for sep in ("===", "==", "!=", "~=", ">=", "<=", ">", "<", ";"):
        head = head.split(sep)[0]
    return head.strip()


def _load_declarations() -> tuple[set[str], dict[str, set[str]]]:
    """读取 pyproject，返回 `(运行时发行名集合, {extra 名: 发行名集合})`（均已规范化）。

    **所有** extra 组都会被读进来（不只 `dev`）—— 本仓有 `dev`（测试链）与
    `m0`（M0 探针的 boto3/botocore）。硬编码只认 `dev` 会让 `m0` 组被判"未声明"，
    于是逼人去加白名单绕开 —— 那是把判据当障碍，而不是当依据。
    """
    with PYPROJECT_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    project = data["project"]

    def names(entries: list[str]) -> set[str]:
        return {_norm(_strip_specifier(e)) for e in entries if _strip_specifier(e)}

    runtime = names(list(project.get("dependencies", [])))
    extras: dict[str, set[str]] = {
        extra: names(list(entries))
        for extra, entries in project.get("optional-dependencies", {}).items()
    }
    return runtime, extras


def _scan_imports(
    base: Path,
) -> tuple[dict[str, list[str]], int, dict[str, list[str]], dict[str, list[str]]]:
    """AST 扫 `base` 下全部 `.py` 的导入。

    返回 `(全部导入, 实扫文件数, 顶层硬导入, 仅函数内延迟导入)`。

    ⚠️ 第二项（实扫文件数）是给**覆盖范围对账**用的
    （见 `_count_py_files_independently`）：只返回导入映射的话，
    "扫了 0 个文件"与"扫了 174 个文件且全合规"产出的 `found` 都是 `{}`
    或同一个集合 —— 无法区分。文件数让两者可分。

    ⚠️ 第三、四项只用于**报告派生**（`scan()` 里那行"src 导入分布"），
    不参与判定。**为什么必须拆分**：说明与证据里的覆盖范围计数必须
    **现场派生**（ISS-78）—— 写死一个数字，后续开发让它变大或变小都
    **不会报错**。本工具初版 docstring 里就写死过一个错数字
    （"顶层硬导入 12 个"，现场实测是 9 个），见模块 docstring 的自查记录。

    刻意**遍历全部 `ast.Import` / `ast.ImportFrom` 节点**（含函数体内），
    而不是只看 `tree.body`：

      · `parsers/*.py` 的 `import docx` 写在函数里（延迟加载，避免 import 模块
        即拉起重依赖）。若只扫顶层，这 4 个包会被判"代码没用"⇒ 判据漏掉整整一类。
      · 判据是"代码用到了"，不是"模块加载时用到了"。延迟导入**同样需要声明** ——
        不声明则该格式的解析用例必然失败。

    方向上是"更严"（更多东西被要求声明），因此安全。
    """
    found: dict[str, list[str]] = {}
    top_only: dict[str, list[str]] = {}
    lazy_only: dict[str, list[str]] = {}
    scanned = 0
    if not base.exists():
        return found, scanned, top_only, lazy_only

    def _top_names(nodes) -> set[str]:
        """从一组 AST 节点里取"第三方顶层包名"（排除本项目顶层包 `rednote`）。"""
        out: set[str] = set()
        for node in nodes:
            if isinstance(node, ast.Import):
                out |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                # 相对导入（level>0）不是第三方包
                if node.level or not node.module:
                    continue
                out.add(node.module.split(".")[0])
        return {n for n in out if n and n not in LOCAL_TOP_LEVEL}

    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        scanned += 1
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            # 解析不了必须响亮报出 —— 静默跳过 = 判据对那个文件失效。
            found.setdefault(f"<PARSE-ERROR:{type(exc).__name__}>", []).append(rel)
            continue

        top_names = _top_names(tree.body)
        all_names = _top_names(list(ast.walk(tree)))
        for raw in all_names:
            bucket = found.setdefault(raw, [])
            if rel not in bucket:
                bucket.append(rel)
        for raw in top_names:
            entry = top_only.setdefault(raw, [])
            if rel not in entry:
                entry.append(rel)
        for raw in all_names - top_names:
            entry = lazy_only.setdefault(raw, [])
            if rel not in entry:
                entry.append(rel)

    # 互斥化：`lazy_only` 收敛为「**从未**在任何文件顶层出现过」的包名。
    # 否则一个包（如 `SQLAlchemy`）在 A 文件顶层 import、在 B 文件函数内 import，
    # 会同时出现在两张表里，让"延迟导入 N 个"这个计数失去意义。
    for name in [n for n in lazy_only if n in top_only]:
        del lazy_only[name]
    return found, scanned, top_only, lazy_only


def _count_py_files_independently(base: Path) -> int:
    """用 `os.walk` 数 `base` 下 `.py` 文件数 —— 与 `rglob` **互为独立实现**。

    **为什么必须是独立实现**（纪律 11：期望值要有独立来源）：
    写成 `len(list(base.rglob("*.py")))` 与 `_scan_imports` 内部的
    `for path in base.rglob("*.py")` 是**同一个 API 的两种写法** ——
    若 `rglob` 被误写成 `glob`（覆盖范围只到顶层），两处会**一起缩水**，
    对账恒等、永不报红。那正是 ISS-137 的形态：
    `sum(x) + (total - sum(x)) == total` 对任何实现都成立，跑一千次什么都不判。

    `os.walk` 是另一套遍历机制（基于 `os.scandir`），才构成真正的对账。
    """
    if not base.exists():
        return 0
    total = 0
    for _dirpath, dirnames, filenames in os.walk(base):
        # 与 `_scan_imports` 的排除规则保持一致（否则对账会假报不平）。
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        total += sum(1 for name in filenames if name.endswith(".py"))
    return total



def _check_mapping_table() -> list[tuple[str, str]]:
    """映射表自检：发行名非空，且别名不得写回自己。

    返回 `[(导入名, 问题描述)]` —— 带上导入名是为了让 `Finding.render()`
    能把它渲染成合成标记（`<MAPPING:docx>`）而不是把整段问题描述当导入名
    （那会渲染出 `` `import 映射表：docx 的发行名被写成自己…` `` 这种误导性输出）。

    防的是"映射表被写成 `{"docx": "docx"}`"这类**看起来有、实际指错包**的情形
    （PyPI 上确实存在一个废弃的同名 `docx` 包）。
    """
    problems: list[tuple[str, str]] = []
    for imp, dist in IMPORT_TO_DISTRIBUTION.items():
        if not dist.strip():
            problems.append((imp, f"`{imp}` 的发行名为空"))
        if imp == dist and imp in {"docx", "PIL"}:
            problems.append(
                (
                    imp,
                    f"`{imp}` 的发行名被写成自己（`{dist}`）—— "
                    f"实测其发行名不是这个，PyPI 上的同名包是另一个废弃项目",
                )
            )
    return problems


def scan() -> tuple[list[Finding], int, list[str]]:
    """返回 `(findings, 已产出 CHECK 条数, 说明行)`。"""
    findings: list[Finding] = []
    notes: list[str] = []
    checks = 0

    runtime_declared, extras_declared = _load_declarations()
    allowed = runtime_declared | set().union(*extras_declared.values()) if extras_declared else runtime_declared
    checks += 1
    extras_desc = " / ".join(f"{k} {len(v)} 个" for k, v in sorted(extras_declared.items()))
    notes.append(
        f"  pyproject 可解析 —— 运行时声明 {len(runtime_declared)} 个；"
        f"extra 组：{extras_desc or '（无）'}"
    )

    checks += 1
    for imp, problem in _check_mapping_table():
        findings.append(Finding("映射表自检", f"<MAPPING:{imp}>", problem, (__file__,)))
    notes.append(
        f"  映射表 {len(IMPORT_TO_DISTRIBUTION)} 条已自检"
        f"（别名不得写回自己：docx→python-docx / PIL→Pillow）"
    )

    # ------------------------------------------------------------------
    # 先扫全部四个范围，把"代码 import 的发行名"收成一个集合。
    # 这个集合是**独立的一侧** —— 它不因 pyproject 写没写而改变。
    # ------------------------------------------------------------------
    scopes = (
        ("src", SRC_DIR, True),  # 运行时必需
        ("tests", TESTS_DIR, False),
        ("tools", TOOLS_DIR, False),
        ("migrations", MIGRATIONS_DIR, False),
    )

    imported_distributions: set[str] = set()
    per_scope: list[tuple[str, Path, bool, dict[str, list[str]]]] = []
    src_split: tuple[dict[str, list[str]], dict[str, list[str]]] | None = None
    for scope, base_note_path, requires_runtime in scopes:
        imports, scanned, top_only, lazy_only = _scan_imports(base_note_path)
        per_scope.append((scope, base_note_path, requires_runtime, imports))
        if scope == "src":
            src_split = (top_only, lazy_only)
        checks += 1
        # ---- 覆盖范围对账（铁律 14 第 5 次复发候选，见 docstring） ----
        # 独立实现（os.walk）数一遍，与 rglob 实际扫过的文件数比较。
        # 若有人把 `rglob` 误写成 `glob`，src 会从 174 掉到 0，
        # 而 `found` 仍是一个"看起来正常"的映射 —— 只有这条能报红。
        independent = _count_py_files_independently(base_note_path)
        rel_base = base_note_path.relative_to(REPO_ROOT).as_posix()
        if scanned != independent:
            findings.append(
                Finding(
                    scope,
                    f"<SCAN-COMPLETENESS:{rel_base}>",
                    f"扫描范围缩水：rglob 实扫 {scanned} 个 .py，"
                    f"os.walk 独立数得 {independent} 个 —— "
                    f"检查 `_scan_imports` 是否被改成了非递归（glob）",
                    (f"{rel_base}/**/*.py",),
                )
            )
        notes.append(
            f"  扫描 {rel_base} —— "
            f"导入名 {len([k for k in imports if not k.startswith('<')])} 个"
            f"（实扫 {scanned} 文件 / 独立核得 {independent}）"
        )

    # ------------------------------------------------------------------
    # 本地导入名集合的**对账判据**（铁律 14 第 6 次复发的配套守门人）
    #
    # `LOCAL_IMPORT_NAMES` 改成由磁盘派生之后，它仍有两处能悄悄坏掉：
    #
    #   ① **派生范围塌缩**：`_discover_local_import_names` 的 `os.walk` 若被
    #      写成非递归，集合会缩水 ⇒ 本地名被当第三方 ⇒ 假红（响亮，尚可）；
    #      但若 `_LOCAL_IMPORT_SCAN_ROOTS` 少一个目录，就是**静默漏**。
    #   ② **遮蔽真实依赖**（真正危险的一侧）：派生根里有 `tools/**`，若将来
    #      有人新建 `tools/httpx.py`，那么任何 `import httpx` 都会被当成
    #      "本地名"而**豁免** —— 真实依赖的漏声明被永久吞掉，且**不报错**。
    #
    # 因此这里做两件独立的事（不能用同一条遍历自证清白，纪律 11）：
    #   · 用 `rglob` 独立派生一遍，与 `os.walk` 的结果**对账**（抓 ①）；
    #   · 把派生名与**映射表里的第三方导入名**求交（抓 ②）。
    #
    # ⚠️ 刻意**不**写"派生名必须存在于磁盘" —— 名字既然从磁盘派生，
    #    那条断言对任何实现都恒真（ISS-137 同族：恒真式判据等于没有判据）。
    # ------------------------------------------------------------------
    checks += 1
    # --- ① 独立实现对账：rglob 派生 vs os.walk 派生（两条不同路径）
    via_rglob: set[str] = set()
    for base in _LOCAL_IMPORT_SCAN_ROOTS:
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if path.name == "__init__.py":
                via_rglob.add(path.parent.name)
            else:
                via_rglob.add(path.stem)
    via_rglob |= set(LOCAL_IMPORT_EXEMPT)
    walk_only = sorted(LOCAL_IMPORT_NAMES - via_rglob)
    glob_only = sorted(via_rglob - set(LOCAL_IMPORT_NAMES))
    if walk_only or glob_only:
        findings.append(
            Finding(
                "本地名派生",
                "<LOCAL-NAMES-MISMATCH>",
                f"两种独立实现（os.walk / rglob）派生的本地名不一致："
                f"仅 walk 有 {len(walk_only)} 个（{', '.join(walk_only[:3])}…）、"
                f"仅 rglob 有 {len(glob_only)} 个（{', '.join(glob_only[:3])}…）—— "
                f"检查 `_discover_local_import_names` 是否漏扫了某个根目录",
                (__file__,),
            )
        )
    # --- ② 遮蔽检查：本地名不得与**第三方导入名**同名
    shadowed = sorted(
        n for n in LOCAL_IMPORT_NAMES if n in IMPORT_TO_DISTRIBUTION
    )
    if shadowed:
        findings.append(
            Finding(
                "本地名派生",
                "<LOCAL-NAMES-SHADOW-DEP>",
                f"派生出 {len(shadowed)} 个与第三方导入名**同名**的本地名"
                f"（{', '.join(shadowed[:5])}）—— 它们会把这些真实依赖的"
                f"漏声明**永久豁免**掉，且不报错。请重命名该文件/目录",
                (__file__,),
            )
        )
    notes.append(
        f"  本地导入名 {len(LOCAL_IMPORT_NAMES)} 个（os.walk 派生，"
        f"rglob 独立核得 {len(via_rglob)} 个；与第三方同名 {len(shadowed)} 个）"
    )

    # ------------------------------------------------------------------
    # 反向覆盖判据：代码 import 的，声明里必须有
    # ------------------------------------------------------------------
    for scope, base_note_path, requires_runtime, imports in per_scope:
        for import_name, files in sorted(imports.items()):
            if import_name.startswith("<PARSE-ERROR"):
                findings.append(
                    Finding(scope, import_name, "源码解析失败，判据对它失效", tuple(files))
                )
                continue
            if import_name in sys.stdlib_module_names:
                continue
            if import_name in LOCAL_IMPORT_NAMES or import_name in IMPORT_ALLOWLIST:
                continue

            distribution = IMPORT_TO_DISTRIBUTION.get(import_name)
            if distribution is None:
                # 未登记的导入名：**必须报红**，不静默跳过。
                findings.append(
                    Finding(
                        scope,
                        import_name,
                        "未登记映射，请在 IMPORT_TO_DISTRIBUTION 补该导入名 → 发行包",
                        tuple(files),
                    )
                )
                continue

            normalized = _norm(distribution)
            imported_distributions.add(normalized)

            if requires_runtime:
                ok = normalized in runtime_declared
                detail = "需在 [project.dependencies] 声明（不许只放进任何 extra 组）"
            else:
                ok = normalized in allowed
                detail = "需在 [dependencies] 或某个 extra 组（如 [dev] / [m0]）声明"
            if not ok:
                findings.append(Finding(scope, import_name, f"缺少发行包 **{distribution}** —— {detail}", tuple(files)))

    checks += 1
    notes.append(
        f"  反向覆盖判据已执行 —— 遍历'代码 import 的'（{len(imported_distributions)} 个发行包），"
        f"而非'已声明的'（后者对漏声明恒真，见 ISS-115）"
    )

    # ------------------------------------------------------------------
    # 覆盖范围派生：`src` 的导入分布（**现场算，不写死在注释里**，ISS-78）
    #
    # 模块 docstring 讲"为什么需要本判据"时引用了一组计数。那组计数**绝不能写死**
    # —— 本工具初版就写死过一个错数字（"顶层硬导入 12 个"，实为 9 个），
    # 而写死的数字漂移了不会有任何东西报错。因此这里把计数做成一行运行输出。
    #
    # 拆"顶层硬导入"与"仅函数内延迟导入"的依据：两者对"缺包"的暴露时机不同
    # （前者 import 模块即崩，后者只在该格式被解析时崩），故计数理应分开呈现。
    # ------------------------------------------------------------------
    if src_split is not None:
        top_only, lazy_only = src_split

        def _dists(mapping: dict[str, list[str]]) -> list[str]:
            """把导入名映成发行名；**只保留第三方**（过滤标准库 / 已知本地模块）。"""
            return sorted(
                IMPORT_TO_DISTRIBUTION.get(name, name)
                for name in mapping
                if not name.startswith("<")
                and name not in sys.stdlib_module_names
                and name not in LOCAL_IMPORT_NAMES
            )

        top_dists = _dists(top_only)
        lazy_dists = _dists(lazy_only)
        notes.append(
            f"  src 导入分布（现场派生）—— 顶层硬导入 {len(top_dists)} 个发行名"
            f"（{'/'.join(top_dists)}）；仅函数内延迟导入 {len(lazy_dists)} 个"
            f"（{'/'.join(lazy_dists)}）"
        )

    # ------------------------------------------------------------------
    # 互补判据：从声明往代码看（抓"多余声明"与"扫描被收窄"）
    #
    # ⚠️ 不能写成 `assert dist in runtime_declared` —— 那是恒真式
    #    （ISS-137 同族：`sum(x) + (total - sum(x)) == total` 对任何实现都成立）。
    #    真正的对账对象是 imported_distributions，即"这个声明在代码里有没有消费者"。
    #
    # ## 为什么未登记的"多余声明"必须**判红**，而不是打印警告
    #
    # 第一版只打印警告（`⚠️ …`），理由是"合法的声明了但不 import 确实存在
    # （uvicorn / psycopg），一刀切报红会逼人写假注释"。**这个理由现在不成立**：
    # uvicorn 已登记进 `DECLARED_NOT_IMPORTED_ALLOWLIST`，psycopg 实际被 import。
    # 而"只警告不报红"落进了本项目最忌讳的那一类 ——
    # **警告不是判据**：CI 只看退出码，"有警告"与"没问题"同形
    # （与 `check_test_db_isolation` 的 `[WARN]` 同族）。
    #
    # ## 它同时是"扫描被收窄"的**唯一可观测信号**（这才是关键）
    #
    # `src` 下 `python-docx` / `openpyxl` / `pypdf` / `Pillow` 四个包**全部**
    # 只出现在**函数体内**（`parsers/*.py` 的延迟导入）。若有人把
    # `_scan_imports` 的 `ast.walk(tree)` 收窄成 `tree.body`（只扫模块顶层），
    # **文件数不变**（`_count_py_files_independently` 那条对账抓不到），
    # 但 `imported_distributions` 会静默少掉这 4 个发行名 ——
    # 于是"代码 import 的都在声明里"这条主线判据**照样全绿**。
    # 收窄后唯一的表征就是"这 4 个声明看起来没人用"。
    # 因此这条必须能报红，否则收窄是**完全静默**的。
    # ------------------------------------------------------------------
    # 互补判据（声明方向）：整段只计 **1 条** CHECK，不按依赖逐个计 ——
    # 见 EXPECTED_CHECKS 的说明（按依赖计数会让"删声明"变成"条数不足"误报）。
    checks += 1
    pyproject_rel = PYPROJECT_PATH.relative_to(REPO_ROOT).as_posix()
    for dist in sorted(runtime_declared):
        if dist in imported_distributions:
            continue
        reason = DECLARED_NOT_IMPORTED_ALLOWLIST.get(dist)
        if reason:
            notes.append(f"  已声明但代码不 import（已登记理由，不判红）：{dist} —— {reason}")
            continue
        findings.append(
            Finding(
                "声明方向",
                f"<UNUSED-DECLARATION:{dist}>",
                "已声明但四个扫描范围内**均无** import —— "
                "若它确实被 import（如 `parsers/*.py` 的函数体内延迟导入），"
                "则说明 `_scan_imports` 被收窄到只扫模块顶层（`tree.body`）；"
                "否则请删除该声明，或在 `DECLARED_NOT_IMPORTED_ALLOWLIST` 登记理由",
                (pyproject_rel,),
            )
        )

    return findings, checks, notes


def main() -> int:
    try:
        findings, checks, notes = scan()
    except Exception as exc:  # noqa: BLE001 - 必须响亮失败，不能伪装成通过
        print("依赖声明一致性自检（ISS-144）")
        print(f"[FAIL] 自检器自身崩溃：{type(exc).__name__}: {exc}")
        print("  ⇒ 崩溃 ≠ 通过。请先修自检器本体。")
        return 2

    print("依赖声明一致性自检（ISS-144）")
    print("  判据方向：代码 import → pyproject 必须有声明（**反向覆盖**）")
    print(f"  权威声明处：{PYPROJECT_PATH.relative_to(REPO_ROOT).as_posix()}")
    for note in notes:
        print(note)
    print()

    # ⚠️ 顺序有讲究：**先输出语义诊断，再判条数**。反过来的话，
    #    一旦条数不足就会盖掉真正的诊断（本包实测踩过，见 EXPECTED_CHECKS 说明）。
    if findings:
        print(f"[FAIL] 发现 {len(findings)} 处依赖声明不一致：\n")
        for f in findings:
            print(f"  {f.render()}")
        if checks < EXPECTED_CHECKS:
            print(
                f"\n  （附带：只产出 {checks} 条 CHECK，期望 >= {EXPECTED_CHECKS} 条 —— "
                f"上面这 {len(findings)} 条可能不全，见 ISS-43）"
            )
        print(
            "\n处置：\n"
            "  · 运行时依赖（`src/**`，含函数内延迟导入）→ 加进 `[project.dependencies]`，"
            "锁到补丁号（研发计划 §5）\n"
            "  · 仅测试/工具需要 → 可放某个 extra 组（`[dev]` / `[m0]`）\n"
            "  · 确认不是第三方包（本地模块）→ 它应在 `tools/**` / `tests/**` 下，"
            "由 `_discover_local_import_names` **自动派生**；若确无文件，"
            "才在 `LOCAL_IMPORT_EXEMPT` 登记理由\n"
            "  · 新的第三方导入名 → 先登记 `IMPORT_TO_DISTRIBUTION`（导入名 ≠ 发行名时）\n"
            "\n⚠️ 不要为了让它变绿而把运行时依赖挪进 extra 组 —— "
            "`pip install .`（不带 extra）下会 ModuleNotFoundError。"
        )
        return 1

    if checks < EXPECTED_CHECKS:
        print(
            f"[FAIL] 未发现声明问题，但只产出 {checks} 条 CHECK，期望 >= {EXPECTED_CHECKS} 条 —— "
            f"脚本很可能中途未跑完（ISS-43：崩溃会伪装成通过）"
        )
        return 2

    print(
        f"[PASS] 共 {checks} 条 CHECK：`src` / `tests` / `tools` / `migrations` "
        f"四范围代码 import 的第三方包，均已在 pyproject 声明。"
    )
    print("  （已知盲区：动态导入 importlib.import_module / __import__ 扫不到，见文件 docstring）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
