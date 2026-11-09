"""`check_test_db_isolation.py` 自身的判据（ISS-158）。

## 为什么判据也要有判据

`tools/quality/check_test_db_isolation.py` 是**声称自己有防线**的一方。
按判据纪律第 9 条（ISS-95）：「声称自己有防线」比「没有防线」更危险 ——
所有人以为有人看着，就没人再手工复核。

本文件专盯它**最容易被悄悄削弱**的一处：**位置豁免**。

判据原本只有一种豁免（`x.startswith("postgresql://")` 这类**模式**）。
2026-09-30 本包为修 ISS-158 又把豁免扩到四类位置：
`replace` 实参 / `or` 链兜底 / `assert` 期望值 / `parametrize` 参数表 /
`os.environ[隔离变量] = <DSN>`。

**豁免是最危险的东西**：它天生只会把红变绿，不会把绿变红。
因此每扩一次，都必须**成对**证明两件事：

  ① **够用**（阳性对照）：那几类真实合规写法确实被判为合规；
  ② **没放宽**（阴性对照）：真硬编码**仍然**报红。

只验①会让"豁免写成 `return True`"也通过；只验②会让"豁免完全失效、
21 处假阳性照旧"也通过。**两者缺一，这条判据就退化成仪式。**

## ⚠️ 本文件的样本为什么必须用 `+` 拼接构造

`check_test_db_isolation.py` 的第一段扫描 `services/backend/tests/**/*.py` ——
**本文件就在范围内**。若把样本 DSN 直接写成字面量：

    _BAD = 'URL = "postgresql://rednote@localhost:5432/postgres"'   # ← 危险

那么判据（和它自己）会把本文件判红。更糟的是：本文件的**意图**就是
"里面全是故意的硬编码样本"，一旦有人图省事把它加进 `ALLOW_FILES` 白名单，
整个文件就成了**法外之地** —— 而那正是它最该被判的理由。

因此所有样本都写成 `_PG + "…"` 的拼接：`_PG = "postgres" + "ql://"`。
拼接表达式在 AST 里是 `BinOp(Constant, Constant)`，**不是** `ast.Constant`，
判据扫不到它；而 `_scan()` 把样本写进临时文件后**重新解析**，
那里才出现真正的 DSN 常量节点 —— 样本与判据各得其所。

最后一条测试 `test_this_file_itself_is_clean` 钉住这个约束本身：
将来有人往里加真字面量，它会红。

## 反向验证

无独立反向脚本 —— 本文件**本身**就是判据的面（ISS-108）的活性证据，
与 `tools/m1/collect_schema_parity_reverse_evidence.py` 的用例 ③ 互为两遍：
那里验"判据在真实副本上报红"，这里验"判据的函数被直接调用时判得对"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL = REPO_ROOT / "tools" / "quality" / "check_test_db_isolation.py"

#: 判据所在目录必须能 import（测试跑在 `services/backend` 下，工具在仓根 `tools/`）。
sys.path.insert(0, str(TOOL.parent))

import check_test_db_isolation as checker  # noqa: E402

#: 测试文件口径：只认 `REDNOTE_TEST_DATABASE_URL`。
ISOLATION_ENVS = frozenset({checker.ISOLATION_ENV})
#: 工具脚本口径：两个环境变量都认（见判据 docstring）。
TOOLS_ENVS = checker.TOOLS_ENVS


# ===========================================================================
# 样本构造：**一律拼接**，理由见模块 docstring
# ===========================================================================

#: `"postgres" + "ql://"` —— 拼接点刻意落在 `://` **之前**：
#: `_is_dsn_literal` 要求 `v.startswith("postgresql://")`，
#: 因此本文件里任何常量都不会被认成 DSN（双保险）。
_PG = "postgres" + "ql://"
_HOST = "rednote@localhost:5432/postgres"
_HOST_SHORT = "rednote@h/db"


def _scan(tmp_path: Path, src: str, env_names: frozenset[str] = ISOLATION_ENVS) -> list:
    """把样本源码写进临时文件，用判据的 `_scan_file` 直接判定。

    写文件（而不是传字符串）是刻意的：`_scan_file` 的真实入参就是磁盘上的文件，
    这样测的是**判据实际会走的那条路径**，而不是一个只在测试里存在的接口。
    """
    sample = tmp_path / "sample_under_test.py"
    sample.write_text(src, encoding="utf-8")
    findings: list = []
    checker._scan_file(sample, env_names, findings)
    return findings


# --- 真硬编码（**必须报红**）--- 每个样本对应一个独立失效面 ---------------
_BAD_MODULE_CONST = 'URL = "' + _PG + _HOST + '"\n'
_BAD_IN_FUNCTION = 'def connect():\n    return "' + _PG + _HOST + '"\n'
_BAD_PASSED_TO_CONNECT = 'conn = psycopg.connect("' + _PG + _HOST + '")\n'
_BAD_WRONG_ENV = (
    'URL = os.environ.get("SOME_OTHER_VAR", "' + _PG + _HOST + '")\n'
)
_BAD_OR_WITHOUT_ENV = 'dsn = os.environ.get("PATH") or "' + _PG + _HOST + '"\n'
# 关键边界：字面量是 `.replace(...)` 的**调用者**（不是实参）——
# 它不是"被拿来变换的模式"，而是一个地址，**必须报红**。
_BAD_IS_THE_CALLEE = 'raw = "' + _PG + _HOST + '".replace("a", "b")\n'

# --- 合规写法（**必须通过**）----------------------------------------------
_OK_INLINE_DEFAULT = (
    'URL = os.environ.get("REDNOTE_TEST_DATABASE_URL", "' + _PG + _HOST + '")\n'
)
_OK_TWO_HOP = (
    'DEFAULT_TEST_DATABASE_URL = "' + _PG + _HOST + '"\n'
    'TEST_DATABASE_URL = os.environ.get(\n'
    '    "REDNOTE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL\n'
    ').strip()\n'
)
_OK_OR_CHAIN = (
    "dsn = (\n"
    '    os.environ.get("REDNOTE_DATABASE_URL")\n'
    '    or os.environ.get("REDNOTE_TEST_DATABASE_URL")\n'
    '    or "' + _PG + _HOST + '"\n'
    ").strip()\n"
)
_OK_ENVIRON_WRITE = (
    'os.environ["REDNOTE_TEST_DATABASE_URL"] = "' + _PG + _HOST + '"\n'
)
# ⚠️ `monkeypatch.setenv` 与上面那条**同义**（2026-10-10 补的豁免形态 ⓔ）：
#    两种都是「夹具主动把库入口指向隔离变量」，只是一个直写 os.environ、
#    一个走 pytest 的自动还原 API。`REDNOTE_DATABASE_URL` 也认 ——
#    `drill_db` 这类工具模块两个变量名都读。
_OK_SETENV_ISOLATION = (
    'monkeypatch.setenv("REDNOTE_TEST_DATABASE_URL", "' + _PG + _HOST + '")\n'
    'monkeypatch.setenv("REDNOTE_DATABASE_URL", "' + _PG + _HOST_SHORT + '")\n'
)
# ⚠️ 阴性：`setenv` 的第一个实参**不是**本仓的库配置变量名 ⇒ 字面量流向了
#    别处，仍属"忘了隔离"，必须报红（不得因函数名是 setenv 就整句放行）。
_BAD_SETENV_OTHER_VAR = (
    'monkeypatch.setenv("SOMETHING_ELSE", "' + _PG + _HOST + '")\n'
)
_OK_REPLACE_ARGS = (
    'raw = dsn.replace("' + _PG + 'psycopg://", "' + _PG + '")\n'
)
_OK_ASSERT_EXPECTED = (
    'assert kind(\'"' + _PG + _HOST_SHORT + '"\') == "' + _PG + _HOST_SHORT + '"\n'
)
_OK_PARAMETRIZE = (
    '@pytest.mark.parametrize(("dsn", "expected"), [\n'
    '    ("' + _PG + _HOST + '", "expected_db"),\n'
    "])\n"
    "def test_x(dsn, expected):\n"
    "    assert database_name(dsn) == expected\n"
)

_BAD_CASES = (
    ("模块级裸赋值", _BAD_MODULE_CONST),
    ("函数内裸赋值", _BAD_IN_FUNCTION),
    ("作实参传给连接函数", _BAD_PASSED_TO_CONNECT),
    ("流向非隔离变量", _BAD_WRONG_ENV),
    ("or 链里没有任何 environ.get", _BAD_OR_WITHOUT_ENV),
    ("字面量是 replace 的调用者（不是实参）", _BAD_IS_THE_CALLEE),
    ("setenv 到非库变量名", _BAD_SETENV_OTHER_VAR),
)

_OK_CASES = (
    ("内联默认值", _OK_INLINE_DEFAULT),
    ("两跳命名常量", _OK_TWO_HOP),
    ("or 链兜底", _OK_OR_CHAIN),
    ("夹具写隔离变量", _OK_ENVIRON_WRITE),
    ("夹具用 monkeypatch.setenv 写隔离变量", _OK_SETENV_ISOLATION),
    ("replace 的两个实参是模式", _OK_REPLACE_ARGS),
    ("assert 期望值", _OK_ASSERT_EXPECTED),
    ("parametrize 参数表", _OK_PARAMETRIZE),
)


# ===========================================================================
# 判据 1：阴性对照 —— 真硬编码必须报红（证明豁免没放宽）
# ===========================================================================


@pytest.mark.regression
@pytest.mark.parametrize(("label", "src"), _BAD_CASES, ids=[c[0] for c in _BAD_CASES])
def test_hardcoded_dsn_still_reports_red(tmp_path: Path, label: str, src: str) -> None:
    """**豁免扩了四类位置之后，真硬编码必须仍然报红。**

    这条是 ISS-158 修复的护栏：豁免天生只会把红变绿。
    若某次改动把豁免写宽了（例如"文件里出现过 `os.environ.get` 就整文件放行"、
    或"按行号豁免"），本条会立刻红 —— 而它红的方式正是
    `findings == []`（"该报的没报"），与"改对了"在**计数上**不同形。
    """
    findings = _scan(tmp_path, src)
    assert len(findings) == 1, (
        f"「{label}」必须报红且只报 1 处，实际 {len(findings)} 处：\n"
        + "\n".join(str(f) for f in findings)
    )
    assert findings[0].literal in src, "报的必须是样本里那个字面量"


# ===========================================================================
# 判据 2：阳性对照 —— 真实合规写法必须通过（证明豁免够用）
# ===========================================================================


@pytest.mark.regression
@pytest.mark.parametrize(("label", "src"), _OK_CASES, ids=[c[0] for c in _OK_CASES])
def test_compliant_forms_are_accepted(tmp_path: Path, label: str, src: str) -> None:
    """**真实合规写法必须 0 处报红。**

    21 处假阳性（ISS-158）就是这么来的：判据只认①②两种形态，
    同义的第三、四种写法被判成硬编码 ⇒ 判据开始惩罚唯一在测隔离的代码。
    本条逐形态钉住"它被正确识别"。
    """
    findings = _scan(tmp_path, src)
    assert findings == [], (
        f"「{label}」被判成硬编码（假阳性）：\n"
        + "\n".join(str(f) for f in findings)
    )


# ===========================================================================
# 判据 3：豁免必须精确到节点，不许顺带放过同一行/同一文件的真硬编码
# ===========================================================================


@pytest.mark.regression
def test_exemption_is_by_node_identity_not_by_line(tmp_path: Path) -> None:
    """同一文件里「豁免形态」与「真硬编码」并存 → 只报后者，且恰 1 处。

    这是判据用 `id()` 而非行号的关键理由（见 `_pattern_literal_ids` 注释）。
    若改成"按行豁免"，本样本第 2 行的真硬编码会被第 1 行的豁免顺带放过 ——
    这正是"顺手放过"的经典形态。
    """
    src = _OK_REPLACE_ARGS + _BAD_MODULE_CONST
    findings = _scan(tmp_path, src)
    assert len(findings) == 1, (
        f"应只报第 2 行的裸赋值，实际 {len(findings)} 处：\n"
        + "\n".join(str(f) for f in findings)
    )
    assert findings[0].lineno == 2, f"报的行号应为 2，实际 {findings[0].lineno}"


# ===========================================================================
# 判据 4：环境变量集合确实参与了判定（不是摆设）
# ===========================================================================


@pytest.mark.regression
def test_env_name_set_actually_drives_the_verdict(tmp_path: Path) -> None:
    """同一份源码：测试口径（只认 TEST 变量）报红，工具口径（两个都认）通过。

    若 `_or_chain_is_isolated` 的 `env_names` 参数被忽略（例如写成常量），
    两个口径会给出**同一个**结论，本条当场红 —— 它把"参数被用到了"
    从阅读代码变成可复跑的判据。
    """
    src = 'dsn = os.environ.get("REDNOTE_DATABASE_URL") or "' + _PG + _HOST + '"\n'
    as_test = _scan(tmp_path, src, ISOLATION_ENVS)
    as_tool = _scan(tmp_path, src, TOOLS_ENVS)
    assert len(as_test) == 1, "测试口径只认 REDNOTE_TEST_DATABASE_URL，应报红"
    assert as_tool == [], "工具口径认 REDNOTE_DATABASE_URL，应通过"


# ===========================================================================
# 判据 5：本文件自己必须是"干净"的（钉住"样本用拼接"这条约束）
# ===========================================================================


@pytest.mark.regression
def test_this_file_itself_is_clean() -> None:
    """本测试文件必须 0 处违规 —— 它就在判据第一段的扫描范围内。

    这条钉住"样本一律用 `_PG + …` 拼接"这个约束：将来有人往里直接写
    一个 DSN 字面量（最省事的做法），本测试立刻红，而不是让整个文件
    被迫进 `ALLOW_FILES` 白名单（那会让它成为法外之地）。
    """
    findings: list = []
    checker._scan_file(Path(__file__), ISOLATION_ENVS, findings)
    assert findings == [], (
        "本文件含可被静态识别的 DSN 字面量 —— 样本必须用 `_PG + …` 拼接：\n"
        + "\n".join(str(f) for f in findings)
    )


# ===========================================================================
# 判据 6：第二段（tools 脚本）的覆盖范围由 ci.yml 派生，且非空（铁律 14）
# ===========================================================================


@pytest.mark.regression
def test_ci_referenced_scripts_scope_is_derived_and_nonempty() -> None:
    """第二段范围必须现场从 `ci.yml` 派生，且至少覆盖到已知的几条判据。

    **只写下界**（本仓铁律）：后续会继续往 CI 加脚本，"恰好 N 个"必失效。
    若 `_ci_referenced_scripts()` 因 ci.yml 改格式而静默返回空集，
    第二段就变成"扫 0 个脚本" —— **与"全部通过"在计数上同形**（ISS-143）。
    这里的 `>=` 下界让那种静默失效当场报红。

    ⚠️ 分两段断言，**不经过"文件是否存在"这一层**：
    `_ci_referenced_scripts()` 会过滤掉磁盘上不存在的引用，
    因此在"只复制了部分文件"的临时副本里，它的返回集天然偏小 ——
    若只断言那个集合，本测试会在与判据无关的环境差异上报红
    （"环境导致的红"与"判据坏了导致的红"同形，ISS-143 的镜像）。
    故：覆盖范围的名词（ci.yml 里引用了什么）从**文本**判，
    只有"判据自己被覆盖到"这一条才依赖真树。
    """
    ci_text = checker.CI_FILE.read_text(encoding="utf-8")
    referenced = set(checker._CI_SCRIPT_RE.findall(ci_text))
    referenced = {r for r in referenced if r.startswith("tools/")}
    assert len(referenced) >= 10, (
        f"ci.yml 里 `python tools/**` 引用异常偏少（{len(referenced)} 条）："
        f"{sorted(referenced)} —— 正则或 ci.yml 格式变了，第二段会静默失效"
    )
    rels = {str(p.relative_to(REPO_ROOT)) for p in checker._ci_referenced_scripts()[0]}
    assert "tools/quality/check_test_db_isolation.py" in referenced, (
        "判据自己必须被 ci.yml 引用到（否则第二段永远扫不到它自己）"
    )
    # 真树才有这些文件；临时副本里该集合可能为空，故只在下界成立时才比。
    if len(rels) >= 10:
        assert "tools/quality/check_test_db_isolation.py" in rels
