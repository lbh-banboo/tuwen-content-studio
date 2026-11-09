#!/usr/bin/env python
"""测试库隔离自检：**任何测试文件都不许硬编码数据库 DSN**（ISS-108）。

## 为什么需要这个

本仓有一条明确约定（`docs/m2/M2并行开发接缝冻结.md`）：

> 测试库物理隔离：`REDNOTE_TEST_DATABASE_URL` 覆盖，无需改 conftest。

这条约定**此前没有任何机械判据**，于是它被破坏了 10 次而无人发现：

  · **本机**：全量 pytest 把数据写进默认库 `postgres`。实测跑一次
    `users` 从 70527 → 70894（+367 行）；该库因此累积到 **7 万行**
    测试垃圾，还被当成"集成环境"来诊断 —— 而它其实是垃圾场。
    （判据纪律第 8 条：文档是唯一声称方时，没有任何机械判据与它对账。）
  · **CI**：`ci.yml` 的 `PG_DSN` 用户是 `postgres`
    （`postgresql+psycopg://postgres:postgres@localhost:5432/postgres`），
    而硬编码的是 **`rednote`**。GitHub runner 上不存在 `rednote` 角色 →
    `OperationalError: role "rednote" does not exist`。
    也就是说 **CI 一旦真正接上 remote 就会在门 2 直接红**，
    10 个文件、数百条测试全部报错。

## 判据：DSN 字面量必须"流向"隔离变量

合规形态（本仓 21 个文件统一口径）有两种，都做 **AST 数据流**判定，
而不是"文件里出现过 environ.get 就算合规"（后者会让"一个文件写死两处
DSN、只隔离一处"静默通过）：

  ① 直接作默认值：
         url = os.environ.get("REDNOTE_TEST_DATABASE_URL", "postgresql://…")
  ② 先赋给命名常量，再作默认值（**两跳**，本仓实际用法）：
         DEFAULT_TEST_DATABASE_URL = "postgresql://…"
         TEST_DATABASE_URL = os.environ.get(
             "REDNOTE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL
         ).strip()

出现 DSN 字面量、但**不满足①②** → 报违规。

白名单（**逐条给理由**，与 `check_undefined_globals._IMPLICIT_GLOBALS` 同纪律）：
  · `test_health.py`——含故意不可达的 DSN（`127.0.0.1:1`），测错误处理路径；
  · `test_errors.py`——DSN 是**被测数据**，测的就是"错误详情里的连接串会被打码"。

跑法：
    python tools/quality/check_test_db_isolation.py
退出码 0 = 无违规。

## 第二段扫描范围：**CI 会跑的 tools 脚本**（后补，同一个病）

上面这条判据第一版只扫 `services/backend/tests/`。于是同一个病在
`tools/` 下**原样复发而无人发现**：`tools/` 全域有 21 处
`rednote@localhost:5432`，其中
`tools/m1/collect_schema_parity_reverse_evidence.py` 正要被补进
CI 门 3 —— CI 的角色是 `postgres`，它硬编码的却是 `rednote`，
一旦补进去就是**门 3 在 CI 上直接红**，且红的原因是
`role "rednote" does not exist`，与"判据被违反"毫无关系。

因此判据增加一段：**从 `.github/workflows/ci.yml` 机械提取被引用的
`tools/**/*.py`，逐个做同样的 AST 数据流判定**。

  · 范围**必须现场读 ci.yml**，不能让人维护一份"该查哪些脚本"的名单 ——
    新脚本进门 3 时没人会记得回来加（本仓"覆盖范围靠记性"已栽过多次）。
  · 允许的环境变量放宽为 `REDNOTE_DATABASE_URL` / `REDNOTE_TEST_DATABASE_URL`
    两者之一：工具脚本用前者、测试夹具用后者，都是"可被环境覆盖"。
  · `tools/m1/collect_wp*_evidence.py` 里那些 `rednote@localhost` **不在范围内** ——
    它们是本机手动跑的证据采集器，口径就是本机默认库，且 CI 引不到它们。
    这个豁免是**机械的**（"CI 有没有引用"），不是名单式的。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "services" / "backend" / "tests"
CI_FILE = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: 测试文件必须流向的环境变量（**只此一个** —— 测试库隔离靠它）。
ISOLATION_ENV = "REDNOTE_TEST_DATABASE_URL"

#: 工具脚本可接受的环境变量：两者皆可（db 口径 / 测试库口径）。
TOOLS_ENVS: frozenset[str] = frozenset(
    {"REDNOTE_DATABASE_URL", "REDNOTE_TEST_DATABASE_URL"}
)

#: 从 ci.yml 里提取 `python <路径>.py` 的脚本路径（机械取范围）。
_CI_SCRIPT_RE = re.compile(r"\bpython\s+([\w./\-]+\.py)")

#: 逐文件白名单：文件名 → 理由。**新增条目必须写清理由**，
#: 否则这个白名单会退化成"把报红的地方都加进来"。
ALLOW_FILES: dict[str, str] = {
    "test_health.py": "含故意不可达的 DSN（127.0.0.1:1），用于测错误处理路径",
    "test_errors.py": "DSN 是被测数据 —— 测「错误详情里的连接串会被打码」",
    # ⚠️ 本条是 2026-10-10 补的（该处**在 HEAD 上，门 3 自 8918fec 起就红**）：
    "test_observability.py": (
        "与 test_health.py **逐字同族**：`Settings(database_url=\"…127.0.0.1:1/nonexistent\")`"
        " 是 R1 的真判据所需的**故意不可达探针**（保留端口、必拒连），"
        "用于区分 liveness 与 readiness。它按定义**不能**连得上，"
        "也就不是「忘了隔离」；该文件只有这一处 DSN 字面量。"
    ),
}


#: 字符串方法：其实参是**模式**，不是**连接地址**。
#:
#: 判据第一版只认字面量的「值」，不认它出现的「位置」，于是
#: `x.startswith("postgresql://")` 这种**前缀判定**被当成硬编码 DSN ——
#: **判据抓到它自己**（`check_test_db_isolation.py` 里就有这两行）。
#: 这个假阳性在判据只扫 `tests/` 时从不出现（那里没人写前缀判定），
#: 一旦扫描范围扩到 `tools/`（本文件自己进了 CI）就立刻自报违规。
#:
#: 语义区分是清楚的：**连接地址会被赋值/传参去连库；
#: 模式只会被拿去比较字符串**。因此按「位置」豁免，不按「文件名」豁免 ——
#: 白名单式豁免会让本文件成为"法外之地"，而它恰恰是最该被判的文件之一。
#:
#: ⚠️ 已知边界（**不做**豁免的形态）：`assert "postgresql://" not in resp.text`
#: 这类 `in` / `not in` 比较**不豁免**。原因是那两种情况里，
#: 被比较的字面量既可能是模式、也可能是具体地址，机械区分不了；
#: 当前用到它的两个文件（`test_health.py` / `test_errors.py`）本就在白名单里。
#: 宁可对将来新增的这类写法报假阳性，也不放过一个真硬编码。
_PATTERN_METHODS: frozenset[str] = frozenset(
    {
        "startswith",
        "endswith",
        "removeprefix",
        "removesuffix",
        "split",
        "rsplit",
        "partition",
        "rpartition",
        "find",
        "rfind",
        "index",
        "rindex",
        # ⚠️ `replace` 是 2026-09-30 补的（ISS-158），补充理由必须写清：
        # 它的**两个**实参分别是「被替换的模式」与「替换成的模式」，
        # 都不是"拿去连库的地址"。本仓 6 处清一色是
        #     dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        # —— SQLAlchemy 方言 → libpq 方言的**协议替换**，两个字符串
        # 都不含 host/user/db，结构上不可能是一个连接地址。
        # 与 `startswith` 同族：字面量只被拿去**变换字符串**，不被赋值去连库。
        # ⚠️ 已知边界：`x.replace("postgresql://rednote@h/db", "postgresql://u@h/db")`
        # 这种把**完整地址**当模式/替换物的写法也会被豁免。它虽然看着像地址，
        # 但语义仍是字符串变换（不构成"赋值/传参去连库"的数据流）。
        # 这个边界由 `test_test_db_isolation.py` 的阳性对照显式覆盖：
        # 那里断言**真硬编码赋值仍报红**，证明豁免没有宽到放走真缺陷。
        "replace",
    }
)


def _is_dsn_literal(node: ast.AST) -> str | None:
    """节点是 `postgresql[...]://…` 字面量则返回它，否则 None。

    ⚠️ 必须要求 `://`：早期版本只判前缀 `postgresql+`，
    于是 **`"postgresql+psycopg"`（一个驱动名，不是连接串）也被当成 DSN**。
    这条假阳性在判据只扫 `tests/` 时从未暴露（那里没人写驱动名），
    一旦把扫描范围扩到 `tools/`（脚本里要 `set(drivername=...)`）就立刻误报。
    判据的"能命中"必须与约定的"什么算 DSN"同形。
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


def _is_environ_get(node: ast.AST) -> ast.Call | None:
    """节点是 `os.environ.get(...)` 则返回它，否则 None。

    ⚠️ 形态是**三层**：`Call(func=Attribute(attr="get",
    value=Attribute(attr="environ", value=Name("os"))))`。
    第一版只判 `func.value` 是 `Name("os")` —— 永远不会成立，
    于是"合规"集合恒为空，21 个文件全被误报（判据自己的假阳性）。
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "get"):
        return None
    owner = func.value
    if not (isinstance(owner, ast.Attribute) and owner.attr == "environ"):
        return None
    if not (isinstance(owner.value, ast.Name) and owner.value.id == "os"):
        return None
    return node


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """模块级 `NAME = "<字符串>"` → `NAME → 值`（用于解析环境变量名的间接引用）。"""
    out: dict[str, str] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for t in targets:
            if isinstance(t, ast.Name):
                out[t.id] = value.value
    return out


def _isolation_default_names(
    tree: ast.Module,
    str_consts: dict[str, str],
    env_names: frozenset[str] = frozenset({ISOLATION_ENV}),
) -> tuple[set[str], set[str]]:
    """扫出 `os.environ.get(<env_names 之一>, X)`。

    第一个实参可以是字面量，也可以是**指向字面量的模块级常量**
    （`conftest.py` 就是后者：`TEST_DATABASE_URL_ENV = "REDNOTE_…"`）。

    返回 `(直接内联的 DSN 字面量集合, 作为默认值引用的变量名集合)`。
    """
    inline: set[str] = set()
    names: set[str] = set()

    for node in ast.walk(tree):
        call = _is_environ_get(node)
        if call is None or not call.args:
            continue
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            env_name = first.value
        elif isinstance(first, ast.Name):
            env_name = str_consts.get(first.id)
        else:
            env_name = None
        if env_name not in env_names:
            continue

        for arg in call.args[1:]:
            for sub in ast.walk(arg):
                lit = _is_dsn_literal(sub)
                if lit:
                    inline.add(lit)
                elif isinstance(sub, ast.Name):
                    names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    names.add(sub.attr)
    return inline, names


def _module_level_dsn_constants(tree: ast.Module) -> dict[str, tuple[str, int]]:
    """模块级 `NAME = <DSN 字面量>` / `NAME: T = <DSN>` → `NAME → (值, 行号)`。"""
    out: dict[str, tuple[str, int]] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None:
            continue
        lit = _is_dsn_literal(value)
        if lit is None:
            continue
        for t in targets:
            if isinstance(t, ast.Name):
                out[t.id] = (lit, node.lineno)
    return out


def _pattern_literal_ids(tree: ast.Module) -> set[int]:
    """收集**被用作字符串方法实参**的 DSN 字面量的 `id()`。

    这些字面量是**模式**（`x.startswith("postgresql://")`），不是连接地址，
    按位置豁免（理由见 `_PATTERN_METHODS` 的注释）。

    用 `id()` 而不是行号：同一行可能有多个字面量，
    行号法会把同一行上的真硬编码一起豁免掉（"顺手放过"的经典形态）。
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in _PATTERN_METHODS):
            continue
        # 只豁免**字符串方法**调用（`"x".startswith` / `s.startswith` 均可）。
        for arg in node.args:
            if _is_dsn_literal(arg) is not None:
                out.add(id(arg))
    return out


def _environ_name_of(
    node: ast.AST,
    str_consts: dict[str, str],
    alias_consts: dict[str, dict[str, str]] | None = None,
) -> str | None:
    """把一个「环境变量名」表达式解析成字符串：支持字面量、模块级常量、
    以及**跨文件**的 `alias.CONST_NAME`（`alias_consts` 由 `_import_aliases` +
    `_resolve_imported_consts` 在调用方装配，见 `_scan_file`）。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return str_consts.get(node.id)
    if (
        alias_consts
        and isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
    ):
        return alias_consts.get(node.value.id, {}).get(node.attr)
    return None


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    """收集 `import X as alias` → `alias → X` 的映射（仅顶层 `import`，非 `from`）。

    本仓测试文件习惯 `import reset_shared_test_db as tool`，随后用
    `tool.TEST_DATABASE_URL_ENV` 引用被测模块自己导出的隔离变量名常量 ——
    这与同文件内 `_string_constants` 处理的 `NAME = "..."` 是**同一件事**，
    只是常量定义在**另一个文件**里。要认出它就必须先知道 `tool` 指向哪个模块。
    """
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                out[alias.asname or alias.name] = alias.name
    return out


def _resolve_imported_consts(module_name: str) -> dict[str, str]:
    """解析被导入模块的模块级字符串常量（跨文件版 `_string_constants`）。

    搜索范围限定在 `tools/**` 与 `services/backend/tests/`——本仓测试/工具
    脚本互相 `import` 的实际范围（如 `sys.path` 注入 `tools/dev` 后
    `import reset_shared_test_db`）。多个同名文件时取第一个匹配；
    找不到就返回空字典，调用方据此判定"无法解析"而**不豁免**
    （宁可漏豁免误报一次，也不让解析不到的名字悄悄放行真硬编码）。
    """
    candidates = [*REPO_ROOT.glob(f"tools/**/{module_name}.py"), TESTS_DIR / f"{module_name}.py"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            file_tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        return _string_constants(file_tree)
    return {}


#: 本仓确立的"刻意不可达探针"惯例（`test_health.py` / `test_observability.py` /
#: `test_test_database_target.py` / `tools/m5/collect_wp47_evidence.py` 的
#: `UNREACHABLE_DSN` 均用此形态）：**端口恰为 `1`**。端口 1 是保留端口，
#: 永远不可能是真实的 PostgreSQL 部署目标，因此该字面量**结构上不可能是
#: "忘了隔离"**，与 host/db 名无关——不按文件名、不按"unreachable"这个
#: 词出现在字符串里判定（后者可被绕过），只认端口号这一个硬事实。
_UNREACHABLE_PORT_RE = re.compile(r"@[^/@]+:1/")


def _is_deliberately_unreachable_probe(dsn: str) -> bool:
    return bool(_UNREACHABLE_PORT_RE.search(dsn))


def _or_chain_is_isolated(
    node: ast.BoolOp, env_names: frozenset[str], str_consts: dict[str, str]
) -> bool:
    """`A or B or "postgresql://…"` 里只要有 `os.environ.get(<隔离变量>)` 即算隔离。

    这是本仓实际用法（`conftest.py` 之外的一种**两级环境变量 + 兜底**写法）：

        dsn = (
            os.environ.get("REDNOTE_DATABASE_URL")
            or os.environ.get("REDNOTE_TEST_DATABASE_URL")
            or "postgresql://rednote@localhost:5432/rednote_test"
        ).strip()

    语义与形态①②相同 —— 最后那个字面量**是默认值**，可被环境变量整体覆盖。
    判据第一版只认 `os.environ.get(env, X)` 的**第二实参**，
    于是这种"把默认值放在 `or` 链末尾"的同义写法被判成硬编码（ISS-158）。
    按语义豁免：**字面量作为 `or` 链的兜底**，与作 `get` 的第二实参**是一回事**。
    """
    for operand in node.values:
        for sub in ast.walk(operand):
            call = _is_environ_get(sub)
            if call is None or not call.args:
                continue
            if _environ_name_of(call.args[0], str_consts) in env_names:
                return True
    return False


def _data_position_literal_ids(
    tree: ast.Module,
    env_names: frozenset[str],
    str_consts: dict[str, str],
    alias_consts: dict[str, dict[str, str]] | None = None,
) -> set[int]:
    """收集**被当作「被测数据 / 期望值」**的 DSN 字面量的 `id()`（ISS-158）。

    判据抓的是"DSN 字面量被**赋值/传参去连库**"。但测试文件里有另一类字面量：
    它们是**被测数据本身** —— 期望值、参数表、要解析的样本串。
    这些字面量**从不流向任何连接**，结构上不可能是硬编码。

    判据第一版只按「值」匹配，不看「位置」，于是本包新写的三个测试文件
    一共报了 21 处假阳性（其中 13 处是这一类）。

    六种**位置**（都按结构判定，不建文件名白名单 ——
    白名单会让整个文件变成"法外之地"，而本包这三个文件恰恰最该被判）：

      ⓐ `@pytest.mark.parametrize(..., [ ("postgresql://…", "expected"), … ])`
         的参数表 —— 按定义就是"输入 → 期望输出"。
      ⓑ `assert <比较式>` 里的字面量 —— 期望值。
         （限定在 `ast.Compare` 内：`assert kind('"postgresql://"') == "postgresql://"`
          与 `assert tool.database_name("postgresql://") is None` 都是这一形态。）
      ⓒ `A or B or "postgresql://…"` 的兜底（见 `_or_chain_is_isolated`）。
      ⓓ `os.environ["REDNOTE_TEST_DATABASE_URL"] = "postgresql://…"` ——
         **测试夹具在主动写隔离变量**。这个字面量不是"忘了隔离"，
         恰恰相反，它是**隔离机制的入口**：`test_test_residue.py` 用它验证
         "设了环境变量就能换目标库"。把它判成硬编码，等于判据在惩罚
         唯一真正在测隔离的代码。
      ⓕ（2026-10-18，WP29 收口核查时发现）DSN 字面量**端口恰为 `1`**ーー
         本仓既定的"刻意不可达探针"惯例（见 `_is_deliberately_unreachable_probe`
         的注释），出现在**任意位置**都豁免：端口 1 结构上不可能是真实部署，
         所以它不可能是"忘了隔离"。`tools/quality/collect_test_data_volume_reverse_evidence.py`
         与 `services/backend/tests/test_test_data_volume.py` 各有一处这样的探针
         直接传参给被测函数（不在 assert/environ-写入内），因此需要独立于
         ⓐ~ⓔ 的位置判定之外单列一条。

    ⚠️ 边界（**故意不豁免**的形态，与 `_PATTERN_METHODS` 同纪律）：
    `assert` 之外、`or` 链之外的**裸赋值/裸传参**一律照报
    （`url = "postgresql://rednote@localhost/postgres"`）。
    该边界由 `services/backend/tests/test_test_db_isolation.py` 的
    「阳性对照」显式覆盖：真硬编码必须仍报红。
    """
    out: set[int] = set()

    # ⓐ parametrize 的参数表
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "parametrize":
                for arg in node.args[1:]:
                    for sub in ast.walk(arg):
                        if _is_dsn_literal(sub) is not None:
                            out.add(id(sub))

    # ⓑ assert 的比较式（字面量可在比较式任意深度 —— 例如作为被调函数的实参）
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        for sub in ast.walk(node.test):
            if isinstance(sub, ast.Compare):
                for inner in ast.walk(sub):
                    if _is_dsn_literal(inner) is not None:
                        out.add(id(inner))

    # ⓒ or 链的兜底
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)):
            continue
        if not _or_chain_is_isolated(node, env_names, str_consts):
            continue
        for operand in node.values:
            for sub in ast.walk(operand):
                if _is_dsn_literal(sub) is not None:
                    out.add(id(sub))

    # ⓓ `os.environ[<隔离变量>] = <DSN>`（测试夹具主动设置隔离变量）
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Subscript):
                continue
            owner = target.value
            if not (isinstance(owner, ast.Attribute) and owner.attr == "environ"):
                continue
            if not (isinstance(owner.value, ast.Name) and owner.value.id == "os"):
                continue
            slice_node = target.slice
            if _environ_name_of(slice_node, str_consts, alias_consts) not in env_names:
                continue
            for sub in ast.walk(node.value):
                if _is_dsn_literal(sub) is not None:
                    out.add(id(sub))

    # ⓔ `monkeypatch.setenv(<隔离变量>, <DSN>)` —— 与 ⓓ **同义**（ISS-216）。
    #
    # ⚠️ ⓓ 与 ⓔ 是同一件事的两种写法：都是「测试夹具在主动把目标库指向
    #    隔离变量」。ⓓ 用 `os.environ[...] = ...` 直写，ⓔ 用 pytest 的
    #    `monkeypatch.setenv`（可自动还原，更规范）。
    #
    #    2026-10-10 实测：`services/backend/tests/test_rollback_drill.py`
    #    的 `test_base_dsn_follows_environment` 用 ⓔ 写下两条断言——
    #    「基准 DSN 必须只换库名、不许重拼本机角色」。它的字面量是
    #    **被测输入**（`drill_db.dsn()` 的期望值来源），从不直接连库；
    #    而判据报 2 处红 ⇒ `collect_schema_parity_reverse_evidence.py`
    #    的 `rev-restore-isolation` 跟着红。这是**假阳性**，而假红的代价
    #    是逼人削弱判据（判据纪律第 8 条）。
    #
    #    只认 ⓓ 不认 ⓔ = 「同一个语义，因 API 形态不同而一个放行一个报红」
    #    —— 铁律 14 在**豁免侧**的形态（覆盖范围由写法决定）。
    #
    #    边界（与 ⓓ 完全一致）：只豁免「第一个实参是**本仓的库配置变量名**」
    #    的形态（`REDNOTE_DATABASE_URL` / `REDNOTE_TEST_DATABASE_URL` 皆可 ——
    #    两者都是"可被环境覆盖"的入口，`drill_db` 这类工具模块两个都读）。
    #    `monkeypatch.setenv("OTHER_VAR", "<DSN>")` 照旧报红；裸赋值
    #    `url = "<DSN>"` 照旧报红（由 `test_test_db_isolation.py` 的
    #    阳性对照显式覆盖）。
    #
    #    为什么 ⓔ 比 ⓓ 多接受一个变量名：`os.environ[...] = ...` 与
    #    `monkeypatch.setenv(...)` 都**只能**改运行环境、不可能充当"默认值"，
    #    故其语义必定是"夹具在设置库入口"；而形如 `os.environ.get(NAME, X)`
    #    的读取写法里，`X` 才可能是"没流到隔离变量"的硬编码。
    #
    #    2026-10-18：`monkeypatch.setenv(tool.TEST_DATABASE_URL_ENV, "<DSN>")`
    #    —— 第一实参是**跨模块属性引用**而非字面量/本文件常量，
    #    `_environ_name_of` 现经 `alias_consts` 解析 `tool` → 真实模块 →
    #    其 `TEST_DATABASE_URL_ENV` 常量值，语义与直接写字面量完全等价。
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "setenv"):
            continue
        if len(node.args) < 2:
            continue
        if _environ_name_of(node.args[0], str_consts, alias_consts) not in set(env_names) | set(
            TOOLS_ENVS
        ):
            continue
        for sub in ast.walk(node.args[1]):
            if _is_dsn_literal(sub) is not None:
                out.add(id(sub))

    # ⓖ 字典字面量把 DSN 写给隔离变量名键 —— `env_overrides={"REDNOTE_TEST_DATABASE_URL": "<DSN>"}`
    # 一类工具脚本把环境覆盖表达成 dict（传给 subprocess 的 env kwarg），
    # 与 ⓓ/ⓔ 同一语义（主动把目标库指向隔离变量），只是语法容器换成了字典。
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if key is None:
                continue
            if _environ_name_of(key, str_consts, alias_consts) not in set(env_names) | set(
                TOOLS_ENVS
            ):
                continue
            for sub in ast.walk(value):
                if _is_dsn_literal(sub) is not None:
                    out.add(id(sub))

    # ⓕ 端口恰为 1 的刻意不可达探针（见函数 docstring ⓕ），不限位置。
    for node in ast.walk(tree):
        lit = _is_dsn_literal(node)
        if lit is not None and _is_deliberately_unreachable_probe(lit):
            out.add(id(node))

    return out


class Finding:
    __slots__ = ("path", "lineno", "line", "literal", "why")

    def __init__(
        self, path: Path, lineno: int, line: str, literal: str, why: str
    ) -> None:
        self.path, self.lineno = path, lineno
        self.line, self.literal, self.why = line, literal, why

    def __str__(self) -> str:
        """打印一行日志**不许**成为崩溃原因（ISS-102 同族，本文件已复发）。

        ⚠️ 原实现直接 `self.path.relative_to(REPO_ROOT)`：仓外路径（例如
        单元自测用 `/tmp/...` 造样本，或将来扫描范围扩到仓外）会抛
        `ValueError: ... is not in the subpath of ...` ——
        **判据整体崩溃、一条结论都不产生，与"全部通过"在计数上同形**。
        容错退回绝对路径即可。
        """
        try:
            rel = self.path.relative_to(REPO_ROOT)
        except ValueError:
            rel = self.path
        return (
            f"{rel}:{self.lineno}: {self.why} → {self.literal}\n"
            f"      {self.line.strip()}"
        )


def _scan_file(
    path: Path, env_names: frozenset[str], findings: list[Finding]
) -> bool:
    """对单个文件做一次 AST 数据流判定。返回是否可解析（False = 语法错）。"""
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:  # 语法错由别的判据管，这里不静默
        findings.append(
            Finding(path, exc.lineno or 0, "", "(语法错)", f"无法解析：{exc.msg}")
        )
        return False

    inline, names = _isolation_default_names(tree, _string_constants(tree), env_names)
    consts = _module_level_dsn_constants(tree)
    pattern_ids = _pattern_literal_ids(tree)
    alias_consts = {
        alias: _resolve_imported_consts(module)
        for alias, module in _import_aliases(tree).items()
    }
    data_ids = _data_position_literal_ids(tree, env_names, _string_constants(tree), alias_consts)
    ok_literals = set(inline)
    ok_lines: set[int] = set()
    for name in names:
        if name in consts:
            val, lineno = consts[name]
            ok_literals.add(val)
            ok_lines.add(lineno)

    for node in ast.walk(tree):
        lit = _is_dsn_literal(node)
        if lit is None:
            continue
        # 位置豁免 ①：用作字符串方法实参 → 是模式，不是连接地址。
        if id(node) in pattern_ids:
            continue
        # 位置豁免 ②：被测数据 / 期望值 / or 兜底 / 夹具写隔离变量（ISS-158）。
        if id(node) in data_ids:
            continue
        if lit in ok_literals:
            continue
        lineno = node.lineno
        line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        if line.lstrip().startswith("#"):
            continue
        # 模块级常量若已确认流向默认值，其赋值行也合规（上面 ok_lines）。
        if lineno in ok_lines:
            continue
        findings.append(
            Finding(
                path,
                lineno,
                line,
                lit,
                f"硬编码 DSN（未流向 {' / '.join(sorted(env_names))}）",
            )
        )
    return True


def _ci_referenced_scripts() -> tuple[list[Path], list[str]]:
    """从 `ci.yml` 机械提取被 `python <路径>.py` 引用的仓库内脚本。

    返回 `(存在的脚本列表, 说明行)`。**范围现场派生**，不维护名单：
    新脚本一进 `ci.yml` 就自动被本判据覆盖 —— 否则又是"覆盖范围靠记性"。
    """
    notes: list[str] = []
    if not CI_FILE.exists():
        return [], [f"  ⚠️ 找不到 {CI_FILE}，tools 段未执行"]

    text = CI_FILE.read_text(encoding="utf-8")
    found: dict[Path, bool] = {}
    for match in _CI_SCRIPT_RE.finditer(text):
        rel = match.group(1)
        path = (REPO_ROOT / rel).resolve()
        if path.suffix != ".py" or not path.is_file():
            continue
        # 只判仓库内、且真的在 tools/ 下的脚本（services/ 由别的判据管）。
        try:
            inside = path.relative_to(REPO_ROOT)
        except ValueError:
            continue
        if inside.parts[:1] != ("tools",):
            continue
        found[path] = True

    missing: list[str] = []
    for match in _CI_SCRIPT_RE.finditer(text):
        rel = match.group(1)
        path = (REPO_ROOT / rel).resolve()
        try:
            inside = path.relative_to(REPO_ROOT)
        except ValueError:
            continue
        if inside.parts[:1] == ("tools",) and not path.is_file():
            missing.append(rel)
    if missing:
        notes.append(
            "  ⚠️ ci.yml 引用了不存在的脚本："
            + "，".join(sorted(set(missing)))
            + " —— 这些脚本的隔离判据**无法执行**（不是通过）"
        )
    return sorted(found), notes


def scan() -> tuple[list[Finding], int, int, list[str]]:
    """返回 `(findings, 测试文件数, tools 脚本数, 说明行)`。"""
    findings: list[Finding] = []
    notes: list[str] = []
    test_count = 0

    # ---------------------------------------------------------------- 第一段
    for path in sorted(TESTS_DIR.rglob("*.py")):
        test_count += 1
        if path.name in ALLOW_FILES:
            notes.append(f"  白名单跳过 {path.name} —— {ALLOW_FILES[path.name]}")
            continue
        _scan_file(path, frozenset({ISOLATION_ENV}), findings)

    # ---------------------------------------------------------------- 第二段
    scripts, script_notes = _ci_referenced_scripts()
    notes.extend(script_notes)
    for path in scripts:
        rel = path.relative_to(REPO_ROOT)
        notes.append(f"  CI 引用 {rel} —— 按工具脚本口径判定")
        _scan_file(path, TOOLS_ENVS, findings)

    return findings, test_count, len(scripts), notes


def main() -> int:
    findings, test_count, script_count, notes = scan()

    print("测试库隔离自检（ISS-108）")
    print(f"  第一段：扫描 {test_count} 个测试文件（{TESTS_DIR.relative_to(REPO_ROOT)}）")
    print(f"  第二段：扫描 {script_count} 个 CI 引用的 tools 脚本")
    for note in notes:
        print(note)
    print()

    if findings:
        print(f"[FAIL] 发现 {len(findings)} 处违规：\n")
        for f in findings:
            print(f"  {f}")
        print(
            f"\n合规形态（二选一）：\n"
            f"  ① url = os.environ.get(\"{ISOLATION_ENV}\", \"postgresql://…\")\n"
            f"  ② DEFAULT_… = \"postgresql://…\"\n"
            f"     url = os.environ.get(\"{ISOLATION_ENV}\", DEFAULT_…).strip()\n"
            f"\n工具脚本可流向 REDNOTE_DATABASE_URL 或 {ISOLATION_ENV}。"
            f"\n理由：硬编码会让测试库物理隔离失效，且 CI 的 PG 用户是 "
            "`postgres` 而非 `rednote`，会直接 `role does not exist`。"
        )
        return 1

    print(
        f"[PASS] 无违规：{test_count} 个测试文件 + {script_count} 个 CI 引用脚本"
        f"的库地址都能被环境变量覆盖。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
