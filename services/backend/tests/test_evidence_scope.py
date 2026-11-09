"""判据 D（证据脚本覆盖范围三方对账）**自己的判据**（ISS-162）。

## 为什么判据自己需要判据

`tools/quality/check_evidence_scope.py` 声称"能发现证据脚本被静默漏覆盖"。
但**声称**不等于**能**（ISS-95：「声称自己有防线」比「没有防线」更危险）。
本文件用三种方式独立验证它：

  ① **反推定义的阳性/阴性对照**（`writes_evidence` 在最简样本上的行为）；
  ② **豁免白名单不腐烂**（理由仍成立，且判据会核对）；
  ③ **判据自己的活性**（A1/A2/B 三个集合都非空、且与磁盘一致）。

⚠️ 本文件属于 `services/backend/tests/` ⇒ 由 `check_test_db_isolation.py`
按测试口径扫描。因此这里**不写任何 DSN 字面量**（本文件根本不需要连库）。

## ⚠️ 2026-10-01 三遍法自纠（判据自己的判据也有自己的缺陷）

三遍法审这一族判据时，"第一遍发现的第一条缺陷"是**判据自己对文档的断言**：

  · 判据 D 的 docstring 曾手写覆盖范围数字（26/27/27），而现场是 27/28/28；
  · 写成"断言 docstring 与现场一致"**治不了** —— 本包自己新增一个脚本后它立刻又漂移。
  · 正解是**禁止手写**：数字一律以现场输出为准，并另有一条判据
    （`collect_discipline_code_review.py` 的 `[逐行-2]`）断言 docstring 里
    **不存在**手写数字行。

教训与本文件要讲的是同一件事：**判据的说明书也要有判据**，
否则"声称覆盖 27 个"这句话本身就是靠记性维护的。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCOPE_TOOL = REPO_ROOT / "tools" / "quality" / "check_evidence_scope.py"


def _load_scope_tool():
    """按路径加载判据模块（不走包导入 —— tools/ 不是包）。"""
    spec = importlib.util.spec_from_file_location("_check_evidence_scope", SCOPE_TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load_scope_tool()


# --------------------------------------------------------------------------- #
# ① 反推定义的阳性/阴性对照
# --------------------------------------------------------------------------- #

#: (源码片段, 是否应被判为"写 evidence", 说明)
_CASES: list[tuple[str, bool, str]] = [
    (
        'from pathlib import Path\n'
        'OUT = Path("evidence/m1/x.log")\n'
        'OUT.write_text("a")\n',
        True,
        "直接路径常量 + write_text",
    ),
    (
        'from pathlib import Path\n'
        'ROOT = Path(__file__).resolve().parents[2]\n'
        'OUT = ROOT / "evidence" / "m1" / "x.log"\n'
        'OUT.write_text("a")\n',
        True,
        "二元拼接路径（本仓主流形态：REPO_ROOT / 'evidence' / …）",
    ),
    (
        'from pathlib import Path\n'
        'OUT = Path("evidence/m1/x.log")\n'
        'OUT.parent.mkdir(parents=True, exist_ok=True)\n',
        True,
        "mkdir 也算写证据",
    ),
    (
        'with open("evidence/m1/x.log", "w") as fh:\n'
        '    fh.write("a")\n',
        True,
        "open(..., 'w') 形态",
    ),
    (
        # ⚠️ 这一条是本判据第一版的**真实缺陷**：它曾把这个判为"写证据"，
        # 因为实现按"左半段含 evidence 就算 real"判定。
        'from pathlib import Path\n'
        'TMP = Path(__file__).resolve().parents[3] / "evidence" / "m1" / "tmp"\n'
        'TMP.mkdir(parents=True, exist_ok=True)\n',
        False,
        "evidence/**/tmp 是临时文件，不算证据（本判据实测踩过的假阳性）",
    ),
    (
        'from pathlib import Path\n'
        'OUT = Path("build/report.log")\n'
        'OUT.write_text("a")\n',
        False,
        "不指向 evidence 的写入",
    ),
    (
        'value = "evidence-ish string"\n'
        'print(value)\n',
        False,
        "只是提到 evidence，没有写入",
    ),
]


@pytest.mark.parametrize("src,expected,why", _CASES, ids=[c[2] for c in _CASES])
def test_writes_evidence_positive_and_negative(
    tool, tmp_path: Path, src: str, expected: bool, why: str
) -> None:
    """反推判定的阳性/阴性对照 —— 两向都要对。"""
    sample = tmp_path / "sample.py"
    sample.write_text(src, encoding="utf-8")
    ok, _reason = tool.writes_evidence(sample)
    assert ok is expected, f"{why}：期望 {expected}，实际 {ok}"


def test_evidence_kind_treats_tmp_as_not_evidence(tool) -> None:
    """`_evidence_kind` 的三分类边界（按路径**段**判，不按子串判）。

    ⚠️ 下面两条断言都是**本判据实测踩过的真缺陷**留下的回归钉：

      · 第 5 条（`/tmp/evidence-marker.txt`）：第一版用 `"evidence" in low`
        判定，把它误判成 `real` ⇒ 一个与证据无关的文件被当成证据脚本。
      · 第 4 条（`…/evidence/m1/tmp/…`）：第一版按"左半含 evidence 就 real"
        判定，把 `test_contract.py` 的临时目录当成漏覆盖的证据脚本，
        D2 一次报 27 个假阳性。
    """
    assert tool._evidence_kind("evidence/m1/x.log") == "real"
    assert tool._evidence_kind("…/evidence/tmp") == "tmp"
    assert tool._evidence_kind("build/x.log") == "none"
    # ① evidence 是**独立路径段**且其后有独立 tmp 段 ⇒ 临时，不算证据。
    assert tool._evidence_kind("…/evidence/m1/tmp/x.json") == "tmp"
    # ② `evidence` 只是**文件名的一部分**，没有独立的 evidence 段 ⇒ 与证据无关。
    #    （第一版误判成 real —— 假阳性）
    assert tool._evidence_kind("/tmp/evidence-marker.txt") == "none"
    # ③ 反向边界：`tmp` 出现在 evidence **之前** ⇒ 不影响判定，仍是证据。
    #    （防实现退化成"含 tmp 就算临时"）
    assert tool._evidence_kind("/tmp/work/evidence/m1/x.log") == "real"


def test_syntax_error_is_not_silently_treated_as_clean(tool, tmp_path: Path) -> None:
    """语法错的文件不能被当成"不写证据"而**静默放过**。"""
    bad = tmp_path / "broken.py"
    bad.write_text("def (: pass\n", encoding="utf-8")
    ok, reason = tool.writes_evidence(bad)
    assert ok is False
    assert "语法错" in reason, "语法错必须在依据里显式说出来（不静默）"


# --------------------------------------------------------------------------- #
# ② 豁免白名单不腐烂
# --------------------------------------------------------------------------- #


def test_exemptions_have_nonempty_reasons(tool) -> None:
    """每条豁免都必须写明理由（不能只写"跳过"）。"""
    for rel, reason in tool.EXEMPT_FROM_A1.items():
        assert reason.strip(), f"{rel} 的豁免理由为空"
        assert len(reason) > 20, f"{rel} 的豁免理由太短，不足以核对"


def test_every_exemption_reason_still_holds(tool) -> None:
    """豁免理由必须**仍然成立**（判据 D2b 的同一逻辑，这里独立再判一次）。

    两条：① 该文件确实写 evidence；② 该文件确实不在 A1 里。
    """
    a1 = tool.discover_a1()
    b, _reasons = tool.discover_b()
    for rel in tool.EXEMPT_FROM_A1:
        assert rel in b, f"{rel} 已不再写 evidence ⇒ 豁免理由失效，应删除该条目"
        assert rel not in a1, f"{rel} 已进入 A1 ⇒ 豁免多余，应删除该条目"


# --------------------------------------------------------------------------- #
# ③ 判据自己的活性
# --------------------------------------------------------------------------- #


def test_all_three_sets_are_nonempty(tool) -> None:
    """A1 / A2 / B 都不能为空 —— 空集会让差集判据**恒真**（ISS-143 同族）。"""
    a1 = tool.discover_a1()
    a2 = tool.discover_a2()
    b, _ = tool.discover_b()
    assert a1, "A1 为空 ⇒ 差集判据恒真"
    assert a2, "A2 为空 ⇒ 差集判据恒真"
    assert b, "B 为空 ⇒ 差集判据恒真"


def test_scope_matches_disk(tool) -> None:
    """A1 / A2 / B 必须与磁盘一致（不能停在某个历史快照上）。"""
    b, reasons = tool.discover_b()
    # B 的每个成员都必须在磁盘上真实存在。
    for rel in b:
        assert (REPO_ROOT / rel).is_file(), f"{rel} 在 B 里但磁盘上没有"
        assert rel in reasons, f"{rel} 缺少反推依据"
    # A1 的每个成员都必须在磁盘上真实存在。
    for rel in tool.discover_a1():
        assert (REPO_ROOT / rel).is_file(), f"{rel} 在 A1 里但磁盘上没有"


def test_run_checks_all_pass(tool) -> None:
    """判据 D 本体在当前树上必须全绿（这是"当前状态"的基线判据）。"""
    checks, _detail, _notes = tool.run_checks()
    failed = [name for name, ok, _ in checks if not ok]
    assert not failed, f"判据 D 未通过：{failed}"


def test_tool_is_not_itself_a_evidence_script(tool) -> None:
    """判据 D 自己**不是**证据脚本（它不写 evidence）。

    这是必要的自指检查：若哪天它开始写 evidence，A1 是否覆盖它就成了新问题。
    """
    ok, _reason = tool.writes_evidence(SCOPE_TOOL)
    assert ok is False, "判据 D 自己不该写 evidence（否则又引入一处覆盖范围问题）"
