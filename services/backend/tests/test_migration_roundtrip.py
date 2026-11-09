"""`check_migration_roundtrip.py` 自身的判据（ISS-79）。

## 为什么判据也要有判据

`tools/quality/check_migration_roundtrip.py` 是**声称自己有防线**的一方
（ISS-95：「声称自己有防线」比「没有防线」更危险）。它守的是本仓
**长期无人守**的一条路径 —— `downgrade`。CI 三道门只跑 `upgrade head`，
于是 WP11/WP15 的 `downgrade` 坏了两个大包而无人发现（ISS-79）。

本文件专盯它**最容易被悄悄削弱**的四处：

1. **判据是不是恒真**（ISS-115）。静态匹配器若认不出真缺陷，它就会
   永远打印 `[PASS]` —— 而"没判"与"判了且通过"在输出上同形。
   实证：本案的旧写法**确实存在于历史提交里**（WP11/WP15 修复前），
   故阳性对照有真对象可依，不是编的。

2. **判据会不会被自己的文档骗到**（本仓两次前科）。WP11/WP15 的 docstring
   里**引用**了旧写法做教训记录。若静态匹配走"文本正则"路线，
   这两段引文会被当成真违规 —— 判据制造假红，淹没真红。
   本文件用"引文只在 docstring"这一格把它钉死。

3. **覆盖范围是不是由磁盘决定**（铁律 14）。扫描范围必须是
   `migrations/versions/*.py` 的 glob，而不是一份"该查哪些迁移"的名单。

4. **共用函数名是不是从磁盘读的**（铁律 14）。动态段断言
   `base` 上"共用函数存活"，这个"共用是谁"**不许写姓名** ——
   必须读 `rednote/platform/immutability.py::TRIGGER_FUNCTION`。

## 与反向验证的分工

本文件验"函数被直接调用时判得对"（单元级）；
`check_migration_roundtrip.py` 第 1 段自带**六格对照**（这是它的
"判据自身的判据"），并在 `main()` 里作为一条 CHECK 输出。
两遍互补：本文件保证对照本身没被删空；`main()` 保证对照真的在跑。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL = REPO_ROOT / "tools" / "quality" / "check_migration_roundtrip.py"

sys.path.insert(0, str(TOOL.parent))

import check_migration_roundtrip as tool  # noqa: E402


# ===========================================================================
# 判据 1：静态匹配器**有区分力**（阳性 / 阴性 / 引文三格）
# ===========================================================================


@pytest.mark.regression
def test_matcher_selfcheck_passes_on_the_real_repository() -> None:
    """工具自带的六格对照，在**真仓库**上必须全过。

    这是最外层的冒烟：六格里的 A 格直接读 WP11/WP15 两个真实迁移。
    若它们被改回旧写法（或 docstring 引文被当成代码），这一条立刻红。
    """
    problems = tool._selfcheck_matcher()
    assert not problems, "自检对照失败：" + "；".join(problems)


@pytest.mark.regression
def test_matcher_flags_the_original_buggy_form() -> None:
    """阳性对照：WP11/WP15 的**原始缺陷写法**必须被报违规。

    ⚠️ 这条是本文件的重点：判据的价值全在"能不能认出真缺陷"。
    认不出 ⇒ 恒真 ⇒ 连它自己的存在意义都没有（ISS-115）。
    写法定格自历史提交（`{item[4] for item in all_trigger_ddl()}`）。
    """
    buggy = (
        "def downgrade():\n"
        "    for drop_function in {item[4] for item in all_trigger_ddl()}:\n"
        "        op.execute(sa.text(drop_function))\n"
    )
    hits = tool.forbidden_sites_in_source(buggy)
    assert hits, "判据认不出历史缺陷形态 —— 它是恒真的摆设"


@pytest.mark.regression
def test_matcher_ignores_the_same_text_quoted_in_a_docstring() -> None:
    """关键格：旧写法**只**出现在 docstring 里 ⇒ **不许**报违规。

    这是本仓"判据被自己的文档骗到"的第三次防线。WP11/WP15 的 docstring
    逐字引用了旧写法（"原写法（已废止，不要改回去）"）；纯文本匹配会把
    引文当代码 ⇒ 假红。**假红会让真红被忽略**（狼来了）。
    """
    quoted = (
        "def downgrade():\n"
        '    """原写法（已废止）：\n'
        "\n"
        "        for drop_function in {item[4] for item in all_trigger_ddl()}:\n"
        "            op.execute(sa.text(drop_function))\n"
        '    """\n'
        "    for _t, _f, _td, _dt, drop_function in consistency_items():\n"
        "        op.execute(sa.text(drop_function))\n"
    )
    assert not tool.forbidden_sites_in_source(quoted), (
        "docstring 里的引文被当成真违规 —— 判据会被自己的文档骗到（假红）"
    )


@pytest.mark.regression
def test_matcher_covers_unpack_and_alias_forms() -> None:
    """五元组解包、先赋值再迭代两种等价写法也必须被认出。

    只认推导式的话，实现者把
        `for f in {item[4] for item in all_trigger_ddl()}:`
    重构成
        `for _t,_f,_td,_dt,drop in all_trigger_ddl():`
    就能绕过判据 —— 而语义**完全没变**（都删共用函数）。
    这类"换个写法就绕过"是覆盖范围缺口的经典形态（铁律 14）。
    """
    unpack = (
        "def downgrade():\n"
        "    for _t, _f, _td, _dt, drop_function in all_trigger_ddl():\n"
        "        op.execute(sa.text(drop_function))\n"
    )
    assert tool.forbidden_sites_in_source(unpack), "五元组解包形态没被认出"

    alias = (
        "def downgrade():\n"
        "    fns = {item[4] for item in all_trigger_ddl()}\n"
        "    for f in fns:\n"
        "        op.execute(sa.text(f))\n"
    )
    assert tool.forbidden_sites_in_source(alias), "先赋值再迭代形态没被认出"


@pytest.mark.regression
def test_matcher_allows_the_correct_form() -> None:
    """阴性对照：**正确实现**（第 2 步迭代 `consistency_items()`）不许被报红。

    判据不仅要抓坏、还要**不误伤好** —— 误伤会让实现者去"绕过判据"
    而不是去修代码（本仓假红前科：ISS-158 报了 21 处假阳性）。
    正确形态 = 第 1 步 `all_trigger_ddl()` 取 `drop_trigger`，
    第 2 步 `consistency_items()` 取 `drop_function`（WP11/WP15 现状）。
    """
    good = (
        "def downgrade():\n"
        "    for _t, _f, _td, drop_trigger, _df in all_trigger_ddl():\n"
        "        op.execute(sa.text(drop_trigger))\n"
        "    for _t, _f, _td, _dt, drop_function in consistency_items():\n"
        "        op.execute(sa.text(drop_function))\n"
    )
    assert not tool.forbidden_sites_in_source(good), (
        "正确实现被误报 —— 判据会拦住对的写法，逼人去绕它"
    )


# ===========================================================================
# 判据 2：共用函数名必须**从磁盘读**（铁律 14）
# ===========================================================================


@pytest.mark.regression
def test_shared_function_name_is_derived_not_hardcoded() -> None:
    """`_shared_trigger_function()` 在**源码层**必须来自平台模块，不许写字面量。

    动态段断言"base 上共用函数存活"。"共用是谁"若写成字符串字面量，
    将来改名会**静默失败**：判据还在找旧名字，而它已不存在 ⇒
    "共用函数存活"这条断言变成了**检查一个不存在的对象**，
    "没通过"与"对象根本不在"同形。
    """
    src = TOOL.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_shared_trigger_function"
    )
    body = ast.unparse(fn)
    assert "rednote.platform.immutability" in body, (
        "共用函数名必须从 `rednote.platform.immutability` 读（单一事实来源）"
    )
    assert "TRIGGER_FUNCTION" in body, "应引用 `TRIGGER_FUNCTION` 常量，而非自己拼名"
    # 函数体内不得出现"看起来是函数名"的字面量（除 import 路径字符串）。
    literals = {
        n.value
        for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    suspicious = {
        s for s in literals if re.fullmatch(r"rednote_[a-z_]+", s) and s != "rednote"
    }
    assert not suspicious, (
        f"`_shared_trigger_function` 里出现疑似写死的函数名 {sorted(suspicious)}"
        f" —— 必须从平台模块派生（铁律 14）"
    )


@pytest.mark.regression
def test_shared_function_is_a_real_function_in_the_database() -> None:
    """磁盘派生出的共用函数名，必须**真的存在于迁移建过的库**里。

    与 `test_migration_model_parity.py` 的"派生值 vs 磁盘真值"同款：
    派生逻辑写得再漂亮都不算数，只有 `pg_proc` 里有它才证明没脱节。

    连不上库就 skip（**写明理由**，不静默通过）—— 本条是"派生值 vs 真值"
    的对账，没有真库就没有可对账的对象。
    """
    import os

    import psycopg

    dsn = os.environ.get(
        "REDNOTE_TEST_DATABASE_URL",
        "postgresql+psycopg://rednote@localhost:5432/rednote_roundtrip_probe",
    ).replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        conn = psycopg.connect(dsn)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"测试库不可达（{type(exc).__name__}）—— 本条是「派生值 vs 磁盘真值」"
            f"的对账，没有真库就没有可对账的对象，跳过而非假装通过。"
        )
    assert dsn.startswith("postgresql://")  # 形态自查（见下一条）

    name = tool._shared_trigger_function()
    assert name, "共用函数名不得为空"
    with conn:
        row = conn.execute(
            "select count(*) from pg_proc p"
            " join pg_namespace n on n.oid = p.pronamespace"
            " where n.nspname = 'public' and p.proname = %s",
            (name,),
        ).fetchone()
    assert row and row[0] >= 1, (
        f"派生出的共用函数 {name!r} 在真库 `pg_proc` 里不存在 —— "
        f"动态段的『共用函数存活』断言在检查一个不存在的对象"
    )


# ===========================================================================
# 判据 3：覆盖范围由磁盘决定（铁律 14）
# ===========================================================================


@pytest.mark.regression
def test_scan_range_is_disk_derived_and_nonempty() -> None:
    """扫描范围必须现场从 `migrations/versions/` 派生，且数量不小于下界。

    "该查哪些迁移"若由人写名单维护，新迁移会**静默不在覆盖内**。
    `main()` 已断言 `static-scan-nonempty`，本条在单元层再钉一次 ——
    并给出**下界**而非精确值（迁移数会随开发增长，写死必失效，本仓铁律）。
    """
    hits, count = tool.scan_migrations()
    assert count >= 18, f"只扫到 {count} 个迁移（下界 18）—— glob 或基点可能失效"
    assert not hits, f"真树上有迁移命中禁止形态：{hits}"


@pytest.mark.regression
def test_expected_checks_constant_is_declared() -> None:
    """`EXPECTED_CHECKS` 必须存在且与实际产出条数一致（防 ISS-153）。

    `main()` 用 `len(checks) >= EXPECTED_CHECKS` 判完整性。它是 `>=` 语义，
    所以**只拦变少、不拦变多** —— 加判据时若忘了同步这个常量，
    不会报红（ISS-153 的原始形态）。本条把"常量与真实产出"钉在一起：
    静态段可离线复算，动态段的条数由 `main()` 运行期核对。
    """
    assert tool.EXPECTED_CHECKS >= 13, "常量不得小于静态 4 + 动态 9"
    # 静态段实际 4 条 CHECK，必须 ≤ 常量，否则常量写小了会拦住正常的运行。
    assert tool.EXPECTED_CHECKS >= 4


# ===========================================================================
# 判据 4：迁移承诺的护栏测试文件必须真实存在（ISS-169 弱引用，第 3 次复发）
# ===========================================================================


@pytest.mark.regression
def test_guardrail_test_references_resolve_to_real_files() -> None:
    """注释点名的 `test_*.py` 必须真实存在（本次修的就是这个）。

    修 ISS-79 时人工发现：WP11/WP15 的 `downgrade` 文档逐字承诺
    "该不变式由 `test_ddl_downgrade_ownership.py` 静态守 + 真库往返动态守"，
    而那个文件**全仓不存在** —— 文档声称有防线、入口却打不开（ISS-95/169）。
    人工发现即意味着此处无判据，故补此条机械护栏。

    ISS-169 **第 4 次复发**后口径由 `migrations/` 扩到 `tool.GUARDRAIL_SCAN_ROOTS`：
    `src/` 侧有 4 处点名了不存在的文件（`media/models.py`、`media/schemas.py`
    点名 `test_media_wp27.py`；`devtools/__init__.py` 点名
    `tests/test_devtools_wp_g1.py`），实测零假阳性。
    """
    dangling, refs_by_root = tool.scan_guardrail_test_references()
    assert not dangling, (
        "注释点名的护栏测试文件不存在 —— 防线入口打不开："
        + "；".join(dangling)
    )
    # 非空性**逐根**判（ISS-143 的锐化）：只断言合计数时，"某一根源码根失效"
    # （目录改名 / 基点写错 / 遍历机制退化）会让该根静默贡献 0 处引用，
    # 而合计仍 > 0 ⇒ "无悬空"在该根上**恒真** —— 那正是"没跑"与"跑失败"同形。
    empty_roots = sorted(r for r, n in refs_by_root.items() if n == 0)
    assert not empty_roots, (
        f"这些源码根一处护栏测试引用都没扫到：{empty_roots} —— 基点或遍历可能失效，"
        f"该根上的'无悬空'是恒真的（实测逐根：{refs_by_root}）"
    )


@pytest.mark.regression
def test_guardrail_scanner_flags_a_dangling_reference() -> None:
    """阳性对照：扫描器必须能认出**不存在的**测试文件名。

    只断言"真树无悬空"是恒真风险 —— 扫描器写错（正则永远不匹配）也能过。
    这里用一个必然不存在的名字，验证它有区分力。
    """
    import re

    # 复用工具的同一正则，喂一个不可能存在的文件名。
    pat = tool._GUARDRAIL_TEST_RE
    m = pat.search("见 `test_this_file_definitely_does_not_exist_zzz.py`")
    assert m, "正则认不出反引号包裹的 test_*.py 形态"
    name = m.group(1)
    assert not (tool.TESTS_DIR / name).is_file(), "该对照文件竟然存在，换一个名字"

    # 带目录前缀的写法也必须认，且**必须把它剥掉**只留文件名（`src/` 实测
    # 5 处是 `tests/test_baseline_wp22.py` 这种形态；若前缀被吞进组里，
    # 拼出来的路径永远不存在 ⇒ 判据会把真文件误报成悬空）。
    m2 = pat.search("见 `tests/test_baseline_wp22.py`")
    assert m2 and m2.group(1) == "test_baseline_wp22.py", (
        "带目录前缀的引用没被认出来，或前缀没被剥掉："
        f"{m2.group(1) if m2 else None}"
    )
    assert (tool.TESTS_DIR / m2.group(1)).is_file(), "剥前缀后应能解析到真实文件"

    # 反向：正确形态不被误伤。
    assert pat.search("见 `test_migration_roundtrip.py`"), (
        "正则把真实存在的文件名漏掉了（假阴性）"
    )
    assert not re.search(r"test_\w+\.py", ""), "空串不应命中"