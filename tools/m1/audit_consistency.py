#!/usr/bin/env python
"""跨文档一致性对账：把"散落在多处、必须互相一致"的计数与版本号一次核对。

**为什么需要它**（WP19 收口时踩出来的）：

本项目的计数同时出现在至少五处 —— `README.md`、`docs/待定项与问题登记册.md` 的
§1 看板/§1.2 状态分布/§3 统计/§6 变更日志、`contracts/openapi.json`、
`evidence/m1/*.log`、各 WP 说明文档。收口时要手工把它们对齐，而**手工对齐必然漏**：
WP19 收口时实测漏了三处，且三处**都不报错**：

1. 登记册文件头「当前版本」停在 **v1.7**（已到 v1.12）；
2. §1.2 状态分布写 `OPEN 144`（实际 148）；
3. §3.1/§3.2 合计写 `150`（实际 154）。

更隐蔽的是**版本号重复**：`v1.11` 被用了两次（WP20 收口 09-23 与 WP19 09-24 各写了一条），
`grep` 看得见但没人会去数。

**判据**：凡"同一事实在 ≥2 处出现"，就必须有一处机械核对 ——
否则一致性靠记性维护，而记性不是资源。

跑法：

```bash
python tools/m1/audit_consistency.py
```

退出码 0 = 全部对账通过。**不产出证据文件**（它是门禁，不是证据）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTER = REPO_ROOT / "docs" / "待定项与问题登记册.md"
ISSUE_LOG = REPO_ROOT / "docs" / "issues" / "issue-log.jsonl"
CONTRACT = REPO_ROOT / "contracts" / "openapi.json"
README = REPO_ROOT / "README.md"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m1"
PYTEST_LOG = EVIDENCE_DIR / "backend-pytest.log"

_METHODS = {"get", "post", "put", "patch", "delete"}
_STATUSES = ("OPEN", "IN_PROGRESS", "RESOLVED", "WONT_FIX", "SUPERSEDED")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# pytest 证据的汇总行有两种形态：
#   绿的：`RESULT=1539 passed in 104.24s`
#   红的：`RESULT=1 failed, 1538 passed in 97.95s`
# 两种都要能解析出 passed 数。**失败运行里的 passed 数不是基线**，
# 但它仍是一个真实的读数 —— 是否可用由 §5 单独判断，不在这里混淆。
_PYTEST_RESULT_RE = re.compile(
    r"RESULT=(?:(?P<failed>\d+)\s+failed[,\s]+)?(?P<passed>\d+)\s+passed"
)
_PYTEST_EXIT_RE = re.compile(r"^EXIT=(-?\d+)\s*$", re.M)


def _pytest_facts() -> tuple[int | None, int | None]:
    """从 pytest 证据日志读出 (passed 数, 退出码)，读不到给 None。

    **为什么不用 `-1` 这类哨兵值**：哨兵会把「日志格式变了」与「值不一致」
    压成同一个信号，报出来是「声明 [1539] 不含实测值 -1」—— 读的人既不知道
    是格式问题还是证据那次跑挂了，也无从下手。两件事必须能分开（本项目铁律：
    判据必须能区分两种实现）。
    """
    text = _read(PYTEST_LOG)
    match = _PYTEST_RESULT_RE.search(text)
    exit_match = _PYTEST_EXIT_RE.search(text)
    passed = int(match.group("passed")) if match else None
    exit_code = int(exit_match.group(1)) if exit_match else None
    return passed, exit_code


def _collect_facts() -> dict[str, int | None]:
    """从各自的唯一事实来源现场读出全部计数。读不到用 None，不用哨兵值。"""
    contract = json.loads(_read(CONTRACT))
    issues = [
        json.loads(line) for line in _read(ISSUE_LOG).splitlines() if line.strip()
    ]
    register = _read(REGISTER)
    pytest_passed, _pytest_exit = _pytest_facts()
    return {
        "契约 paths": len(contract["paths"]),
        "契约 operations": sum(
            1
            for item in contract["paths"].values()
            for method in item
            if method in _METHODS
        ),
        "契约 schemas": len(contract.get("components", {}).get("schemas", {})),
        "OP 条数": len(re.findall(r"^\|\s*OP-\d+\s*\|", register, re.M)),
        "ISS 条数": len(issues),
        "pytest 通过数": pytest_passed,
    }


def check_readme(facts: dict[str, int | None]) -> tuple[list[str], list[str]]:
    """README 必须声明每个计数值（它是人看的第一入口）。"""
    readme = _read(README)
    problems: list[str] = []
    lines: list[str] = []
    for name, value in facts.items():
        if value is None:
            problems.append(f"「{name}」未能从事实来源读出（证据脚本格式变了？）")
            lines.append(f"  {name:16s} 实测    ?   未取到")
            continue
        present = str(value) in readme
        verdict = "OK" if present else "MISSING（README 未声明该计数）"
        if not present:
            problems.append(f"README 未声明「{name}」= {value}")
        lines.append(f"  {name:16s} 实测 {value:>6d}   {verdict}")
    return lines, problems


# README 里同一个计数会有**多处**声明，且形态不统一（开头表格的叙述句、
# 末尾进度看板的加粗行）。每个计数对应一组正则，全部抓出来比对。
#
# ★ **为什么每条正则都必须有"来源锚点"**（2026-10-07 WP26 收口实测，记 ISS-185）：
#
# 初版三条里有两条是**无锚点的自由匹配**，实测各有一种失效：
#
#   ① `OP 条数` 的第二条 `全部\s*\*\*(\d+)\s*条\*\*` —— **恒不命中**（0 次）。
#      它写的时候（`b0f9ae6`/WP22）想匹配的是"开头表格 `全部 **154** 条`"这
#      种写法，但 README 从头到尾都写成 `全部 188 条待定项`（**不带星号**）
#      ⇒ 这条正则**一次都没匹配过**。它是"死正则"：写着像防线，实际不参与判据
#      （ISS-95 / ISS-115 同族 —— 判据的一个入口从来是空的，而输出与"判过"同形）。
#
#   ② `OP 条数` 的第一条 `(\d+)\s*条待定项` —— **误伤局部叙述**。
#      它本意是抓"全量声明"（`全部 188 条待定项`），但 WP26 收口把附录 A 的
#      8 条写进看板行后，同一段里出现了"来自本包**附录 A** 的 8 条待定项"
#      ⇒ 抓到 `{8, 188}` 两个值 ⇒ 判据按"同类声明必须只有一个取值"报**假红**。
#      假红的危险与假绿同级：它让人去"修"一个**根本没坏**的东西
#      （WP26 文档 §8.4 刚记过同一句话）。
#
#   ③ 同一条正则 —— **误伤"关于声明的引文"**（2026-10-08 WP47① 实测，ISS-206）。
#      为修 ② 而加的锚 `条待定项 +` 只区分了"全量 vs 局部"，**区分不了"声明 vs 引文"**：
#      登记 ISS-185 时把修复过程写进了 README 的「问题记录」行，里面引了旧句式
#      「全部 188 条待定项」⇒ 取值集合 `{188, 193}` ⇒ 又一次假红。
#      **引文是历史记录，改它等于篡改史实**（同"删证据文件"性质），
#      所以唯一出路是把锚再收一级：锚到**行首 + 登记表文件名**。
#      ⇒ 三次失效是同一个递进：**裸词 → 加句式 → 加行首**。
#      每一级都能排除一批干扰，但**只有最后一级能排除"引文"** ——
#      因为引文与声明的差异不在词形，在**位置**。
#
# ⇒ **判据**：凡"同一事实多处声明"的抓取，正则必须锚定到**它该看的那一行/那个
#   句式**，不能只锚"数字 + 名词"。这样三件事同时成立：
#     · 判据的覆盖范围**可指名**（我改的是哪一行、漏了哪一行）；
#     · 新增叙述句时**不会**污染计数（`8 条待定项` 落在句中被锚点排除）；
#     · 真正的看板行写错时**当场报红**（已做变异验证：`188` → `189` 立即 FAIL）。
#
# ⚠️ 一句话总结：**"数字 + 名词"是人写的自由文本，不是字段。**
#    把它当字段用之前，先问"它的句式是谁定的"，再问"**它出现在哪一行**"。
_README_DECL_PATTERNS: dict[str, tuple[str, ...]] = {
    # 开头表格概述句：`全部 188 条待定项 + 197 条开发过程问题记录 + 7 条铁律`
    # —— 锚 `条待定项 +` 防止正文里"新增 8 条开发过程问题记录"这类局部叙述
    # 被算成全量声明。
    # ⚠️ **初版锚错了句式**（ISS-198）：原文写 `全部\s*(\d+)\s*条开发过程问题记录`，
    #    读作"全部 <N> 条开发过程问题记录"，而 README 实际是
    #    "全部 <N> **条待定项** + <M> 条开发过程问题记录" —— `<N>`（188）后面
    #    紧跟的是"条待定项"，不是"条开发过程问题记录" ⇒ 该正则**恒不命中**，
    #    于是"多处声明对账"静默退化为只剩末尾看板行**一个来源**（ISS-95 / ISS-115
    #    族：判据的一个入口从来是空的，而输出与"判过"同形）。
    #    教训：**锚点只负责收窄范围、不负责命中** —— 若锚的句式取自想象而非真实
    #    文本，收窄的代价就是零命中。凡"多条来源"的判据，每条来源都要能自证活着
    #    （见 `check_readme_internal_consistency` 里的活性自检）。
    "ISS 条数": (
        # ⚠️ **第三次收窄**（2026-10-08 WP47① 实测，记 ISS-206）：锚点必须锚
        #    **行首 + 该行所属的表**，不能只锚句式。真实声明是 README L17 那行
        #    文档目录表的一整行：
        #      `| `docs/待定项与问题登记册.md` | **单一事实来源**：全部 193 条待定项 + 205 ...`
        #    而 WP26 收口把 ISS-185 的**修复过程写进了 L252 的「问题记录」行单元格里**，
        #    那里有一句**引文**：`README 从头到尾写「全部 188 条待定项」`。
        #    ⇒ 上一版锚 `条待定项 +` 只区分了"全量声明 vs 局部叙述"，
        #      **区分不了"声明 vs 关于声明的引文"** —— 同一句式第二次出现在正文里。
        #    两次失效的差别：① 局部叙述是**别的表**里的计数；② 引文是**本表内的散文**。
        #      只有"行首 + 文件名"能同时排除两者。
        r"^\|\s*`docs/待定项与问题登记册\.md`\s*\|[^\n]*?全部\s*\d+\s*条待定项\s*\+\s*(\d+)\s*条开发过程问题记录",
        # 末尾看板行：`| 问题记录 | **184 条 / 0 error**（...`
        r"^\|\s*问题记录\s*\|\s*\*\*(\d+)\s*条\s*/\s*0\s*error\*\*",
    ),
    "OP 条数": (
        # 开头文档目录表的那一行（**不带星号** —— 初版那条带 `**` 的正则因此恒不命中，见上）。
        # 锚点构成：`^` 行首（`re.M`）+ 登记表文件名 + 该行内出现「全部 N 条待定项」。
        # 前两级缺一不可：只锚行首会连「问题记录」行的说明文字一起吃进来（ISS-206）。
        r"^\|\s*`docs/待定项与问题登记册\.md`\s*\|[^\n]*?全部\s*(\d+)\s*条待定项",
        # 末尾看板行：`| 待定项 | **188 条**：OPEN 181 / ...`
        r"^\|\s*待定项\s*\|\s*\*\*(\d+)\s*条\*\*",
    ),
    "pytest 通过数": (
        # 末尾看板行：`| 测试 | **2032 passed / 0 skipped**（...`
        r"^\|\s*测试\s*\|\s*\*\*(\d+)\s*passed",
    ),
}


def check_readme_internal_consistency(
    facts: dict[str, int | None],
) -> tuple[list[str], list[str]]:
    """README 对同一计数的**多处**声明必须彼此一致，且含实测值。

    **为什么需要**（ISS-118 同族的判据缺口）：上面那个 `check_readme` 只验证
    「该数字在 README 里**出现过**」—— 于是 README 里同时写着「110 条开发过程
    问题记录」与「119 条 / 0 error」时，**两处都能让判据通过**（119 出现过即可）。
    它只证明"至少有一处写了 X"，**不证明"没有互相打架的声明"**。

    实测踩到：README 开头表格写 110、末尾看板写 119，`audit_consistency.py`
    当时的输出是 `RESULT=PASS 全部对账通过`。人读 README 时看到两个数，
    而门禁不响 —— 与 ISS-118 同一条道理：判据只证明"读得到"，不证明"还成立"。

    判据三条：① 同一计数的所有声明取值**集合大小必须为 1**；
    ② 该取值必须等于现场实测值（防止"多处一致但一起错"）；
    ③ **每条来源正则都必须至少命中一次**（活性自检，ISS-198）—— 否则
    "多处来源对账"只剩一个来源在工作，而输出与"判过"同形。

    ⚠️ **`re.M` 不可省**：`_README_DECL_PATTERNS` 里的看板行正则用 `^|...`
    锚定行首（这是"来源锚点"的实现方式，见该常量处的说明）。去掉 `re.M`
    会让 `^` 只匹配**整个串**的开头 ⇒ 那些正则**全部静默失配**，
    而判据照旧打印 `声明 [...] / 实测 ...`（**与"判过"同形**，ISS-115 族）。
    """
    readme = _read(README)
    problems: list[str] = []
    lines: list[str] = []
    dead: list[str] = []
    for name, patterns in _README_DECL_PATTERNS.items():
        # `re.M`：看板行正则以 `^|` 锚行首（"来源锚点"，见常量处说明）。
        hits = [list(re.finditer(pat, readme, re.M)) for pat in patterns]
        found = sorted({int(m.group(1)) for group in hits for m in group})
        expected = facts.get(name)
        if not found:
            lines.append(f"  {name:16s} 未找到声明（跳过，由 §1 的存在性判据负责）")
            continue
        # 活性自检（ISS-198）：该计数**在 README 里确实被声明了**（found 非空）时，
        # **每一条**来源正则都必须至少命中一次 —— 恒不命中的"死正则"会让
        # "多处来源对账"静默退化成单来源，而输出与"判过"完全同形（ISS-95/ISS-115）。
        # ⚠️ 只在 `found` 非空时判：若整条计数在 README 里都没有，那是 §1
        #    `check_readme`（存在性）的职责，此处不重复报红（避免两处报同一件事）。
        for idx, (pat, group) in enumerate(zip(patterns, hits), 1):
            if not group:
                dead.append(f"「{name}」第 {idx} 条来源正则恒不命中（0 次）：{pat}")
        if len(found) > 1:
            problems.append(
                f"README 对「{name}」有互相矛盾的声明：{found}（同类声明必须只有一个取值）"
            )
        if expected is not None and expected not in found:
            problems.append(
                f"README 对「{name}」声明 {found}，不含实测值 {expected}"
            )
        shown = "?" if expected is None else expected
        lines.append(f"  {name:16s} 声明 {found} / 实测 {shown}")
    for item in dead:
        lines.append(f"  ⚠️  {item}")
        problems.append(f"{item} —— 死正则不参与判据，而输出与「判过」同形（ISS-198）")
    return lines, problems


def check_register_internals() -> tuple[list[str], list[str]]:
    """登记册自身的内部一致性：版本号唯一 + 头部版本存在 + 状态分布自洽。"""
    register = _read(REGISTER)
    problems: list[str] = []
    lines: list[str] = []

    header = re.search(r"当前版本\s*\|\s*([^|]+?)\s*\|", register)
    header_ver = header.group(1) if header else "?"
    version_rows = re.findall(
        r"^\|\s*\*{0,2}(v1\.\d+(?:\.\d+)?)\*{0,2}\s*\|", register, re.M
    )
    dupes = sorted({v for v in version_rows if version_rows.count(v) > 1})
    lines.append(f"  头部版本 {header_ver}；变更日志 {len(version_rows)} 行")
    if dupes:
        problems.append(f"变更日志版本号重复：{dupes}（同一版本号只能有一条）")
    if version_rows and header_ver not in version_rows:
        problems.append(f"头部版本 {header_ver} 在变更日志中找不到对应行")

    declared_rows = re.findall(
        r"^\|\s*`?(" + "|".join(_STATUSES) + r")`?\s*\|\s*\*{0,2}(\d+)\*{0,2}\s*\|",
        register,
        re.M,
    )
    declared = {k: int(v) for k, v in declared_rows if int(v)}
    actual: dict[str, int] = {}
    for _oid, rest in re.findall(r"^\|\s*(OP-\d+)\s*\|(.+)$", register, re.M):
        for cell in (c.strip().strip("`* ").upper() for c in rest.split("|")):
            if cell in _STATUSES:
                actual[cell] = actual.get(cell, 0) + 1
                break
    lines.append(f"  §1.2 声明 {declared} / 实际 {actual}")
    if declared and declared != actual:
        problems.append(f"§1.2 状态分布与表格行不一致：声明 {declared} vs 实际 {actual}")
    return lines, problems


def check_gates() -> tuple[list[str], list[str]]:
    """两个可执行门禁：契约一致 + 问题记录合法。"""
    problems: list[str] = []
    lines: list[str] = []
    for label, args in (
        ("契约 --check", ["tools/contracts/export_openapi.py", "--check"]),
        ("issue --check", ["tools/issues/log_issue.py", "--check"]),
    ):
        proc = subprocess.run(
            [sys.executable, *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        lines.append(f"  {label}: exit {proc.returncode}")
        if proc.returncode != 0:
            problems.append(f"{label} 退出码 {proc.returncode}（应 0）")
    return lines, problems


# ---------------------------------------------------------------------------
# 文档声明的证据文件必须存在（ISS-124）
# ---------------------------------------------------------------------------
#
# **为什么需要**：本仓 §4.3 / 各 WP 说明文档的"关联"节都会列出证据文件路径。
# 那些路径是**下游读者的唯一入口** —— 但此前没有任何判据核对它们是否真实存在。
#
# 实测（WP24 收口时全仓扫）：69 处声明里 **6 处指向不存在的文件**，
# 且全部是"文档声称有防线"的形态：
#
#   · WP24 说明文档 §4.3 列了 3 个从未产出过的日志（`wp24-config.log` /
#     `wp24-inherit.log` / `wp24-allocation.log`）—— 它们的**内容**其实都在
#     `wp24-scene-draft.log` 里（60 条判据），但读者按文档去 `cat` 会得到
#     "No such file"；三个文件名在**全仓任何脚本里都搜不到**，即从未被产出；
#   · `README.md` 写 `wp23-prompts.log`（真名 `wp23-prompt.log`）；
#   · WP23 说明文档写 `wp23-evidence.log`（真名也是 `wp23-prompt.log`）；
#   · ISS-106 的 evidence 写 `wp20-cross-package-fk.log`（真名
#     `wp22-schema-parity-reverse.log`）。
#
# 这正是铁律第 9 条（ISS-95 同族）：**"声称自己有防线"比"没有防线"更危险** ——
# 没有防线时人会谨慎；声称有防线而入口打不开时，所有人以为有人看着。
#
# **判据**：扫 `docs/**/*.md` 与 `README.md` 里形如 `evidence/<层>/<名>.<后缀>`
# 的路径（后缀由 `_DECLARED_EVIDENCE_SUFFIXES` 决定），逐个 `is_file()`。
# **不检查 .py 源码里的路径** —— 那些多是注释举例，
# 且 `tools/m3/collect_wp24_evidence.py` 的 docstring 会**声明自己将要产出的文件**
# （写的时候还不存在），把它们算进来会造成"先有鸡还是先有蛋"的假红。
#
# ★ **为什么后缀必须点名**（2026-10-08 扩展，ISS-200）：
# 本判据的覆盖范围**由后缀白名单单独决定**（铁律第 14 条）—— 原版只写 `\.log`，
# 于是所有 `evidence/**/*.json` 声明**零覆盖**，而输出照旧打印
# 「声明 N 处 / 不存在 0 处」，**与"判过"完全同形**（ISS-95 / ISS-115 族：
# 声称有防线而入口打不开，且判据自己不知道）。这正是 ISS-124 的原始动机
# （"文档声明的路径必须打得开"）在同一判据内部**又开了一个后门**。
# 实测：`docs/m5/WP47-演练记录.md` 引用 8 个 `.json`（备份恢复 + 回滚演练产物），
# 在扩展前**一处都不在扫描面内**；全仓另有 WP04/WP05 说明的 3 个 `.json`。
# 扩展实测：`.log|json|jsonl` ⇒ 声明 97 处，**"不存在"仍为 0**
# （`.log` only 时代 85 处 / 0；加 `.json` 96 处 / 0；再加 `.jsonl` 97 处 / 0）
# —— 即扩展只是补上盲区，不会引入假红。
_DECLARED_EVIDENCE_SUFFIXES: tuple[str, ...] = ("log", "json", "jsonl")
#
# ⚠️ **机械豁免：实测存在、但按本判据口径「刻意不纳入」的后缀**。
# 豁免必须写上理由，且**由 `_self_test_evidence_declaration_patterns` 对账**：
# 文档里出现过的每个后缀都必须落在「白名单 ∪ 豁免」里，否则报红。
# 这样"没纳入某个后缀"永远是一次**显式表态**，而不是静默漏检（铁律 14）。
_EVIDENCE_SUFFIX_EXEMPT: dict[str, str] = {
    "md": (
        "实测 3 处（`WP01-whitelist-request.md` / `WP02-tool-decision.md` / "
        "`WP02-import-guide.md`），均在 M0 WP01/WP02 文档的**交付物清单**表里，"
        "属「将要产出」而非「已有证据索引」，与 `.py` docstring 同理 ⇒ 纳入即假红；"
        "三个文件实测**确实不存在** —— 若 M0 方确认它们已产出/已废弃，"
        "应改成「已产出索引」或从清单移除，再决定是否纳入本判据"
    ),
    "png": (
        "实测 1 处且为通配形态 `` `evidence/m0/WP01-conn-*.png` ``"
        "（「截图/记录」，同属交付物清单）；`*` 不是文件名的一部分，"
        "对通配形态做 `is_file()` 必然假红（与「范围记法 `~`」同族）"
    ),
}
# 单一来源：下面两个正则都从这里拼后缀，避免"白名单在两处各写一遍"再次走样（铁律 14）。
# ⚠️ 按**长度降序**排列：`jsonl` 必须排在 `json` 前面，否则 `x.jsonl` 会先撞上
# `json` 分支 —— 虽然结尾边界能兜住（回溯后会再试 `jsonl`），但排序让意图直白。
_EVIDENCE_SUFFIX_ALT = "|".join(
    sorted(_DECLARED_EVIDENCE_SUFFIXES, key=len, reverse=True)
)
# ⚠️ **结尾边界不可省**：`[A-Za-z0-9_.\-]+\.(?:log|json)` 会把 `x.jsonl` 截成
# `x.json`、把 `x.log.gz` 截成 `x.log` —— **凭空造出不存在的路径**（假红）。
# 首版预检即踩：`evidence/m0/WP04-placeholder-results.jsonl` 被读成 `.json`
# 后误报"缺失"（真名 `.jsonl`）。假红与假绿同病：判据说了它不该说的话。
_EVIDENCE_SUFFIX_END = r"(?![A-Za-z0-9_.\-])"
_DECLARED_EVIDENCE_RE = re.compile(
    rf"evidence/[a-z0-9]+/[A-Za-z0-9_.\-]+"
    rf"\.(?:{_EVIDENCE_SUFFIX_ALT}){_EVIDENCE_SUFFIX_END}"
)
# **后缀探针**（不做存在性判断，只用来统计"文档里出现过哪些后缀"）：
# 刻意写得**比生产正则宽**（允许 `*`、允许任意后缀），这样白名单之外的形态
# 才会被看见并逼人表态 —— 用生产正则去探白名单完备性等于让白名单自查，
# 正是变异 A 实测到的失效形态（白名单一缩，"自测"跟着缩）。
_DECLARED_SUFFIX_PROBE_RE = re.compile(
    r"evidence/[a-z0-9]+/[A-Za-z0-9_.\-*]*\.([A-Za-z0-9]+)"
)

# ★ **分写形态**（2026-10-07 WP26 收口实测发现，记 ISS-186）：
#
#     | 证据日志 | `evidence/m3/`（文件名 `wp26-content-generation.log`） |
#
# 上面那条连写正则只吃「目录与文件名连在一起」的形态；这一种**目录与文件名
# 被文字隔开**的写法**完全不被命中**。后果与本判据要消灭的形态一模一样：
#
#   · WP26 文档声明了 2 个从未产出过的证据文件（`wp26-content-generation.log`
#     / `wp26-simplify.log`），而 `check_declared_evidence_exists`
#     **全程静默** —— 「声称自己有防线」而入口打不开（铁律第 9 条 / ISS-95）；
#   · 附带地，README 里同一段落了另外三处事实错（不可变表 1→4 / 部分索引
#     1→0 / 延迟 FK 3→4），因为**没有任何判据把"文档摘要"与"库结构"对账**。
#
# 为什么必须一并覆盖：本判据的职责就是"文档里声明的路径必须打得开"，
# 覆盖范围**由正则长什么样决定**（铁律第 14 条）—— 换一种排版就漏，
# 等于判据的防线形状由写法决定，而漏掉的形态是静默的。
_DECLARED_DIR_RE = re.compile(r"evidence/([a-z0-9]+)/")
# 反引号形态同样点名后缀（`x.log` 与 `x.json` 都要吃）。结尾本来由反引号锚定，
# 仍显式复用 `_EVIDENCE_SUFFIX_END`：否则一旦有人把结尾反引号改成可选，
# 这里就会静默开始把 `x.jsonl` 读成 `x.json`（与上面同一个假红）。
_DECLARED_BACKTICK_EVIDENCE_RE = re.compile(
    rf"`([A-Za-z0-9_.\-]+\.(?:{_EVIDENCE_SUFFIX_ALT}){_EVIDENCE_SUFFIX_END})`"
)
#
# ⚠️ **机械排除：含 `~` 的名字是「范围记法」而不是文件名**。
# `docs/待定项与问题登记册.md` 写 `evidence/m1/wp10~wp16-code-review.log`，
# 实指 `wp10-code-review.log` … `wp16-code-review.log` **7 个独立文件**
# （逐个实测均存在）。把它当成一个文件名去 `is_file()` 必然假红 ——
# 而"假红"和"假绿"是同一种病（判据说了它不该说的话）。
# 排除条件写成**机械规则**（名字里带 `~`）而不是人写白名单，
# 否则又是一处"覆盖范围由人记"。
_SCOPE_NOTATION_CHAR = "~"


def _declared_from_mixed_line(line: str) -> str | None:
    """从「目录与文件名分写」的一行里拼出一条声明；不适用时返回 `None`。

    ★ 两条必须同时满足，否则会造出**假红**：
      ① 先把**连写命中**从行里抠掉（用等长空格替换，保持其余字符原位）；
         否则 `evidence/m1/a.log,evidence/m3/b.log` 这类同行多命中的行
         会把 `m3` 和别处的反引号名交叉配对（本判据首版即踩，
         配出并不存在的 `evidence/m3/backend-pytest.log`）。
      ② 抠掉之后**必须恰好剩 1 个目录、1 个文件名** —— 数量不等说明
         这行不是"目录与文件名分写"的排版，宁可漏也不臆造路径。
    （假红与假绿同病：判据说了它不该说的话。）

    抽成独立函数**不是为了复用**，是为了让 `_self_test_evidence_declaration_patterns`
    能对**生产路径本身**做合成断言（而不是另写一份仿制品去自证）。
    """
    masked = _DECLARED_EVIDENCE_RE.sub(lambda m: " " * len(m.group(0)), line)
    dirs = set(_DECLARED_DIR_RE.findall(masked))
    names = set(_DECLARED_BACKTICK_EVIDENCE_RE.findall(masked))
    if len(dirs) != 1 or len(names) != 1:
        return None
    layer = next(iter(dirs))
    name = next(iter(names))
    if _SCOPE_NOTATION_CHAR in name:
        return None  # 范围记法（见常量处说明），不是文件名
    return f"evidence/{layer}/{name}"


def _self_test_evidence_declaration_patterns(
    observed: dict[str, set[str]] | None = None,
) -> list[str]:
    """§4 三个正则的**自测**（判据的判据）。

    参数 `observed`：文档里实测出现过的「后缀 → 出现处文档」，由调用方用
    `_DECLARED_SUFFIX_PROBE_RE`（刻意比生产正则**宽**）扫出来。

    **为什么必须有**：§4 的覆盖范围**由后缀白名单单独决定**（铁律第 14 条）。
    白名单写窄（原版只 `.log`）时漏掉 `.json` 是**静默的** —— 判据照样打印
    「声明 N 处 / 不存在 0 处」，与"判过"完全同形（ISS-95 / ISS-115 / ISS-124）。
    实测：WP47 演练记录引用的 8 个 `.json` 在扩展前**零覆盖**，而审计零告警。

    ⚠️ **期望后缀集合必须由文档派生，不能从白名单读**：本函数首版就是遍历
    `_DECLARED_EVIDENCE_SUFFIXES` 断言的 —— 变异实测（把白名单退回 `("log",)`）
    下**自测全程静默**：白名单一缩，"自测"跟着缩，`.json` 臂消失而无人报警。
    那是把铁律 14 的病原样搬进了自测里（"判据的判据"自己也有同一个盲区）。
    故 (A) 的期望集合 = 白名单 ∪ **文档实测后缀**，(D) 另行断言文档实测后缀
    必须全部被「白名单 ∪ 豁免」覆盖。

    四类断言：
      (A) **活性** —— 期望集合里每种后缀都必须被两个生产正则命中；
      (B) **后缀边界** —— `x.jsonl` / `x.log.gz` 一律不得被截断成白名单后缀
          （截断 = 凭空造出不存在的路径 = 假红，首版预检已实测踩过）；
      (C) **分写形态活性** —— 目录与文件名分写（含 `.json`）也必须能拼出；
      (D) **白名单完备性** —— 文档里出现过的后缀必须 ∈ 白名单 ∪ 豁免（机械对账）。
    """
    problems: list[str] = []

    def _hits(pattern: re.Pattern[str], text: str) -> list[str]:
        return [m.group(0) for m in pattern.finditer(text)]

    # (A) 活性：期望后缀 = 白名单 ∪ 文档实测后缀（去掉刻意豁免的），
    #     所以白名单被改窄时，文档里真实存在的后缀仍会被拿去断言（不再跟着缩）。
    expected = set(_DECLARED_EVIDENCE_SUFFIXES)
    for suffix in set(observed or ()) - set(_EVIDENCE_SUFFIX_EXEMPT):
        expected.add(suffix)
    for suffix in sorted(expected):
        sample = f"见 evidence/m5/drill-x.{suffix} 与 evidence/m1/foo.{suffix}"
        got = _hits(_DECLARED_EVIDENCE_RE, sample)
        for want in (
            f"evidence/m5/drill-x.{suffix}",
            f"evidence/m1/foo.{suffix}",
        ):
            if want not in got:
                problems.append(
                    f"§4 自测：后缀 `.{suffix}` 的连写声明未被命中（{want}）"
                    f" —— 覆盖范围由后缀白名单决定，写窄即静默漏检（铁律 14）"
                )
        if _DECLARED_BACKTICK_EVIDENCE_RE.findall(f"`foo.{suffix}`") != [
            f"foo.{suffix}"
        ]:
            problems.append(
                f"§4 自测：后缀 `.{suffix}` 的反引号声明未被命中（`foo.{suffix}`）"
            )

    # (B) 后缀边界 —— 两种错各断言一次：
    #     · **截断**：`.jsonl` 被读成 `.json`、`.log.gz` 被读成 `.log`
    #       ⇒ 凭空造出不存在的路径（假红，首版预检实测踩过）；
    #     · **漏检**：白名单里的后缀没被整名命中（例如交替顺序写反 +
    #       边界断言写错时会只吃到短后缀）。
    #     期望 `None` = 该样本**不该命中任何东西**。
    linked_cases = (
        (
            "evidence/m0/WP04-placeholder-results.jsonl",
            "evidence/m0/WP04-placeholder-results.jsonl",
        ),
        ("evidence/m1/foo.log.gz", None),
        ("evidence/m1/foo.json.bak", None),
        # 后缀名"恰以白名单后缀开头"的形态 —— 结尾边界写错时最先在这里崩。
        ("evidence/m1/foo.logs", None),
        ("evidence/m1/foo.jsonnet", None),
    )
    for text, want in linked_cases:
        got = _hits(_DECLARED_EVIDENCE_RE, text)
        if want is None and got:
            problems.append(
                f"§4 自测：`{text}` 不该被连写形态命中，实测得到 {got}"
                f"（多半是把更长的后缀截断成了白名单后缀）"
            )
        elif want is not None and got != [want]:
            problems.append(
                f"§4 自测：`{text}` 应整名命中 `{want}`，实测得到 {got}"
                f" —— 截断/漏检都会让判据说它不该说的话"
            )
    backtick_cases = (
        ("`x.jsonl`", "x.jsonl"),
        ("`x.json`", "x.json"),
        ("`x.log.gz`", None),
        ("`x.json.bak`", None),
    )
    for text, want in backtick_cases:
        got = _DECLARED_BACKTICK_EVIDENCE_RE.findall(text)
        if want is None and got:
            problems.append(
                f"§4 自测：{text} 被反引号形态误命中 {got}（缺结尾边界）"
            )
        elif want is not None and got != [want]:
            problems.append(
                f"§4 自测：{text} 应整名命中 `{want}`，实测得到 {got}"
            )

    # (C) 分写形态：正例 + **两条不变式各自独立可观测的负例**。
    #     ⚠️ 为什么必须用合成样本：这两条不变式是为历史上踩过的坑写的，
    #     而那一行语料**已经被修好** —— 实测证明（变异 `names < 1` 放宽），
    #     仅靠当前工作树**完全观测不到差异**：改坏了审计照旧 PASS。
    #     没有合成负例，这两条不变式就是"声称有防线"（ISS-95）。
    mixed_cases: tuple[tuple[str, str | None, str], ...] = (
        # (样本行, 期望结果, 该样本在测什么)
        (
            "| 备份恢复演练 | `evidence/m5/`（文件名 `backup-restore-drill-x.json`） |",
            "evidence/m5/backup-restore-drill-x.json",
            "正例：目录与反引号 `.json` 文件名分写，应拼出声明",
        ),
        (
            # 不变式 ②：恰好 1 目录 + 1 名字。放宽成 `names < 1` 会在这里配出错路径。
            "| 证据 | `evidence/m1/`（`wp10-projects.log`、`wp11-versions.log`） |",
            None,
            "负例 ②：同一行 2 个反引号文件名 ⇒ 数量不等，宁可漏也不臆造路径",
        ),
        (
            # 不变式 ①：连写命中必须先被 mask 掉。不 mask 时 dirs 会多出 m2 ⇒ 返回 None。
            "| 证据 | `evidence/m1/`（另见 evidence/m2/x.log） | 文件名 `y.log` |",
            "evidence/m1/y.log",
            "负例 ①：连写命中须先抠掉（否则 dirs={m1,m2} 而漏掉这条真声明）",
        ),
    )
    for text, want, why in mixed_cases:
        got = _declared_from_mixed_line(text)
        if got != want:
            problems.append(
                f"§4 自测：{why} —— 样本 {text!r} 期望 {want!r}，实测 {got!r}"
            )

    # (D) **白名单完备性（机械对账，铁律 14 的落地形态）**：
    #     文档里出现过的每个后缀，必须落在「白名单 ∪ 豁免」里。
    #     期望集合从**文档**派生（`observed`），所以"把白名单改窄"必然报红 ——
    #     这正是变异 A 实测到的失效形态（首版自测对白名单变窄完全免疫）。
    #     大小写敏感：文档写 `.Log` 而生产正则只认 `.log`，那是**真漏**，必须报。
    if observed is not None:
        covered = set(_DECLARED_EVIDENCE_SUFFIXES) | set(_EVIDENCE_SUFFIX_EXEMPT)
        for suffix in sorted(set(observed) - covered):
            sources = "、".join(sorted(observed[suffix])[:3])
            problems.append(
                f"§4 自测：文档里出现了 `.{suffix}` 形态的证据路径（{sources}），"
                f"但它既不在扫描白名单 {_DECLARED_EVIDENCE_SUFFIXES} 中，"
                f"也不在豁免名单 {tuple(_EVIDENCE_SUFFIX_EXEMPT)} 中"
                f" —— 要么纳入白名单，要么写明豁免理由"
                f"（覆盖范围不得静默漏，铁律 14）"
            )
    return problems


def check_declared_evidence_exists() -> tuple[list[str], list[str]]:
    """文档里声明的每个 `evidence/**.{log,json,jsonl}` 都必须真实存在。

    ⚠️ **只扫 `docs/**/*.md` 与 `README.md`**：说明文档给人读、索引给人按图索骥，
    一个打不开的路径在那里就是"防线不存在"；而 `.py` 注释里的路径常是
    "本脚本会产出"的声明（写代码时文件尚未生成），纳入会无条件假红。

    后缀白名单见 `_DECLARED_EVIDENCE_SUFFIXES`（扩展理由见该常量处注释，ISS-200）；
    正则的设计不变式 + **白名单完备性**由 `_self_test_evidence_declaration_patterns`
    当场断言（后者需要先扫出文档实测后缀，故自测放在扫描之后）。
    """
    lines: list[str] = []
    docs = sorted(REPO_ROOT.glob("docs/**/*.md"))
    readme = REPO_ROOT / "README.md"
    if readme.is_file():
        docs.append(readme)

    declared: dict[str, set[str]] = {}
    observed: dict[str, set[str]] = {}
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        rel = str(doc.relative_to(REPO_ROOT))
        # **后缀探针**（刻意比生产正则宽）：统计"文档里到底出现过哪些后缀"。
        # 只喂给自测 (D) 做白名单完备性对账，**不参与存在性判断** ——
        # 否则宽探针会把通配/交付物清单也算成"声明的证据"（假红）。
        for probe in _DECLARED_SUFFIX_PROBE_RE.finditer(text):
            observed.setdefault(probe.group(1), set()).add(rel)
        # (a) 连写形态：`evidence/<层>/<名>.<后缀>` 在同一处。
        for match in _DECLARED_EVIDENCE_RE.finditer(text):
            declared.setdefault(match.group(0), set()).add(rel)
        # (b) 分写形态：同一行里出现 `evidence/<层>/` 与一处反引号包住的
        #     `<名>.<后缀>` ⇒ 拼成一条声明（细节见 `_declared_from_mixed_line`）。
        for line in text.splitlines():
            path = _declared_from_mixed_line(line)
            if path is not None:
                declared.setdefault(path, set()).add(rel)

    problems: list[str] = list(
        _self_test_evidence_declaration_patterns(observed)
    )
    missing = sorted(
        path for path in declared if not (REPO_ROOT / path).is_file()
    )
    # 后缀分布：给人读的**活性提示**，不作判据 —— 真活性由上面的自测负责。
    # （工作树决定的计数会随仓库内容涨落，拿去当判据就是"覆盖范围由仓库内容决定"
    #  的假红源头；自测 (A) 的期望集合由文档派生 + (D) 对账完备性，测的是
    #  "设计上一定能命中、且没有后缀被静默漏掉"。）
    by_suffix: dict[str, int] = {}
    for path in declared:
        suffix = path.rsplit(".", 1)[-1]
        by_suffix[suffix] = by_suffix.get(suffix, 0) + 1
    dist = " / ".join(
        f".{s} {by_suffix.get(s, 0)}" for s in _DECLARED_EVIDENCE_SUFFIXES
    )
    lines.append(
        f"  文档声明的证据文件 {len(declared)} 处 / 不存在 {len(missing)} 处"
        f"（后缀分布：{dist}）"
    )
    for path in missing:
        sources = "、".join(sorted(declared[path]))
        lines.append(f"    ★缺失 {path}  ←  {sources}")
        problems.append(
            f"文档声明了不存在的证据文件：{path}（{sources}）"
            f" —— 读者按图索骥会打不开；改指向真名，或补产出该文件的脚本"
        )
    return lines, problems


# ---------------------------------------------------------------------------
# 证据时效（ISS-118）
# ---------------------------------------------------------------------------
#
# **为什么需要**：`_collect_facts()` 从 `evidence/m1/*.log` 读计数，它只能证明
# 「文件里写着 X」，**不能证明「X 还成立」**。实测踩到：`contract-export.log`
# 与 `backend-pytest.log` 停在 10:15，而 `main.py` 在 10:53 又被改 ——
# 于是同一次 audit 里 `pytest 通过数 实测 1517 OK`（读旧日志）与
# `契约 --check: exit 1`（现场执行）**互相矛盾**，而 audit 自己不知道。
#
# 本仓证据文件开头都写了 `# 生成时间: ...`（有 `2026-09-28T11:41:57+08:00`
# 与 `2026-09-28 14:37:04 +0800` 两种形态），据此把它与**被证据代码的 mtime** 比。
_EVIDENCE_SOURCES: dict[str, tuple[str, ...]] = {
    # 证据文件 → 它证据的代码范围（相对仓库根的路径）
    "backend-pytest.log": ("services/backend",),
    "contract-export.log": ("services/backend", "contracts"),
    "op-register.log": ("docs/待定项与问题登记册.md",),
    "issue-log-stats.log": ("docs/issues/issue-log.jsonl",),
}

# 同秒写入 / 文件系统时间粒度可能造成 1~2 秒假红，留一点余量。
_EVIDENCE_TOLERANCE_SECONDS = 5

_TIME_PATTERNS = (
    re.compile(r"生成时间[:：]\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"),
    re.compile(r"生成时间[:：]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"),
)


def _evidence_generated_at(text: str) -> datetime | None:
    """从证据文件内容里读 `# 生成时间:`。读不到返回 None（由调用方判为缺口）。"""
    for pattern in _TIME_PATTERNS:
        match = pattern.search(text)
        if match:
            return datetime.fromisoformat(match.group(1).replace(" ", "T"))
    return None


def _newest_mtime(paths: tuple[str, ...]) -> float | None:
    """给定路径集合里最新的 mtime（目录则递归 .py/.md/.json）。

    ⚠️ **这只是降级取数**（见 `_tracked_last_change`）。它把「被摸过」与
    「被改过」混为一谈，因此只有在 git 事实取不到时才用它，且**必须出声说明**。
    """
    newest: float | None = None
    for rel in paths:
        target = REPO_ROOT / rel
        if target.is_file():
            candidates = [target]
        elif target.is_dir():
            candidates = [
                p
                for p in target.rglob("*")
                if p.is_file()
                and p.suffix in {".py", ".md", ".json"}
                and "__pycache__" not in p.parts
            ]
        else:
            continue
        for candidate in candidates:
            stamp = candidate.stat().st_mtime
            if newest is None or stamp > newest:
                newest = stamp
    return newest


def _tracked_last_change(paths: tuple[str, ...]) -> tuple[float | None, str]:
    """被证据路径**真正被改动**的最后时刻 + 取数方式说明（ISS-165）。

    ## 为什么不能直接取「目录内最新 mtime」

    测试可以用「写完再还原」的方式验证某条判据会报红 —— 例如
    `services/backend/tests/test_test_database_target.py` 为了验
    `check_test_database_target.py` 会报红，把真树上的 `test_devtools.py`
    `read_text → replace → write_text`，跑完再 `write_text` 还原。
    **内容还原了，mtime 没有还原**（`Path.write_text` 必然刷新 mtime）。

    后果：`services/backend` 的「最新 mtime」永远等于「最后一次跑全量 pytest
    的时刻」⇒ 本节判据**必然**报 `backend-pytest.log` 陈旧，
    即使业务代码一行没改（本包实测：差 56612 秒，且文件内容与 HEAD 分毫不差，
    `git diff --quiet` exit 0）。
    更糟的是它与「真改了代码忘了重跑证据」**在输出上完全同形**（ISS-143 同族）——
    读证据的人无法分辨「该重跑」与「判据在骗我」。

    ## 判据改为「按内容取数」

    * **有未提交改动的文件** → 用它自己的 mtime（那确实是一次真实编辑）；
    * **无改动的文件** → 用**最后一次提交**的时间（内容没变 ⇒ mtime 无意义）；
    * 两者**取最大**。

    `git status --porcelain` 只报**内容差异**，因此变异测试碰不到它
    （改完还原 ⇒ `git status` 为空）。这样「谁真的改了」与「谁只是被摸了一下」
    就被分开了 —— 而后者正是污染源。

    函数返回 `(时间戳, 说明)`，说明会原样打印在证据时效一节里：
    取数口径变了却不说，等于换了一把不知道刻度在哪的尺子。
    """
    if not paths:
        return None, ""

    def _git(*args: str) -> tuple[int, str]:
        # ⚠️ `-c core.quotepath=false` 不可省（**ISS-166**，本函数第一版就踩了）：
        # `git status --porcelain` 默认把非 ASCII 路径**转义成八进制**
        # （`docs/\345\276\205\345\256\232...`），于是下面 `REPO_ROOT / rel`
        # 找不到该文件 ⇒ `is_file()` 为假 ⇒ 那个文件的未提交改动被**静默跳过**。
        # 实测后果：`docs/待定项与问题登记册.md` 明明是 ` M ` 状态，
        # `op-register.log` 的取数却报「未提交改动 **0** 个文件」，
        # 于是"登记册改了而证据没重跑"这条**本该报红**的情况被判成同代。
        # 方向是**偏绿**（漏报陈旧），比假红更危险 —— 假红会被人看到，
        # 漏报只会让陈旧证据安静地留在库里（与 ISS-143 同族：
        # 两种成因印出同一条输出，读的人无从分辨）。
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode, (proc.stdout or "")

    rel_paths = [str(p) for p in paths]
    code, out = _git("log", "-1", "--format=%ct", "--", *rel_paths)
    if code != 0 or not out.strip().isdigit():
        # 取不到 git 事实 ⇒ 降级，并**明确说明这是降级**（静默降级 = 判据失明）。
        return (
            _newest_mtime(paths),
            "（降级：git 取数不可用，退回目录内最新 mtime —— 该口径会把"
            "「被摸过」当成「被改过」，见 ISS-165）",
        )
    committed_at = float(out.strip())

    dirty_newest: float | None = None
    dirty_count = 0
    code, out = _git("status", "--porcelain", "--", *rel_paths)
    if code == 0:
        for line in out.splitlines():
            rel = line[3:].strip().strip('"')
            if " -> " in rel:  # 重命名：取新名
                rel = rel.split(" -> ")[-1].strip().strip('"')
            target = REPO_ROOT / rel
            if not target.is_file():
                continue
            dirty_count += 1
            stamp = target.stat().st_mtime
            if dirty_newest is None or stamp > dirty_newest:
                dirty_newest = stamp

    effective = max(committed_at, dirty_newest or 0.0)
    stamp_txt = datetime.fromtimestamp(committed_at)
    note = (
        f"（按内容取数：最后提交 {stamp_txt:%m-%d %H:%M:%S}"
        f" + 未提交改动 {dirty_count} 个文件）"
    )
    return effective, note


def check_evidence_freshness() -> tuple[list[str], list[str]]:
    """证据必须与被证据的代码**同代**（证据生成时间不早于代码最后修改）。

    ⚠️ **本判据在 CI 里跳过，且跳过要出声**：CI 的 `git checkout` 把所有文件的
    mtime 重置为 checkout 时刻，于是证据内容里写的生成时间**必然**早于工作树
    mtime → 无条件假红。所以它守的是**本机收口流程**（证据正是在那里生成、
    陈旧证据也正是那里咬人），CI 里响亮跳过。

    跳过必须打印 —— 一个「永远被静默跳过」的判据与没有判据无从区分
    （ISS-110 / ISS-95 族）。
    """
    lines: list[str] = []
    problems: list[str] = []

    if os.environ.get("CI"):
        lines.append("  SKIP（CI：checkout 重置 mtime，mtime 型时效判据无意义）")
        lines.append("       —— 本判据只在**本机收口**时有效")
        return lines, problems

    for name, sources in _EVIDENCE_SOURCES.items():
        path = EVIDENCE_DIR / name
        if not path.is_file():
            problems.append(f"证据文件缺失：{name}（下面第 1 节会读到它）")
            continue
        generated = _evidence_generated_at(path.read_text(encoding="utf-8"))
        if generated is None:
            problems.append(f"{name} 里找不到「生成时间」（时效判据无从判断）")
            continue
        newest, how = _tracked_last_change(sources)
        if newest is None:
            lines.append(f"  {name:24s} 被证据路径不存在，跳过")
            continue
        latest = datetime.fromtimestamp(newest)
        delta = (latest - generated).total_seconds()
        if delta > _EVIDENCE_TOLERANCE_SECONDS:
            problems.append(
                f"{name} 陈旧：生成于 {generated:%m-%d %H:%M:%S}，"
                f"而被证据代码最后改于 {latest:%m-%d %H:%M:%S}（晚 {int(delta)} 秒）"
                f" —— 重跑对应证据脚本"
            )
            lines.append(f"  {name:24s} 陈旧（晚 {int(delta):>6d} 秒）{how}")
        else:
            lines.append(f"  {name:24s} 同代（差 {int(delta):>4d} 秒）{how}")
    return lines, problems


def check_evidence_self_report() -> tuple[list[str], list[str]]:
    """证据**自己那一次运行**必须是绿的（否则基线计数来自一次失败运行）。

    **为什么需要**（ISS-121 暴露的判据缺口）：上一轮 `backend-pytest.log` 被
    一次 flaky 失败覆盖成 `RESULT=1 failed, 1538 passed`。当时的处置是
    "重跑直到绿" —— 这等于**把偶发失败藏起来**：下一次它再红，只要再跑一次
    就好了，没有人会知道这个 flaky 存在。而 §2b 那边还会报出「声明 [1539]
    不含实测值 -1」这种把两件事混在一起的错。

    本判据把「证据那次跑挂了」单独拎出来说，且**不许重跑掩盖**：
    退出码非 0 就是 FAIL，并直接指出 flaky 用例名（从 FAILURES 段抓）。
    """
    lines: list[str] = []
    problems: list[str] = []
    text = _read(PYTEST_LOG)
    _passed, exit_code = _pytest_facts()

    if exit_code is None:
        lines.append("  未找到 EXIT= 行（证据脚本格式变了？）")
        return lines, problems

    if exit_code != 0:
        failed_cases = sorted(
            set(
                re.findall(
                    r"^FAILED\s+(\S+)", text, re.M
                )
            )
        )
        detail = "、".join(failed_cases) if failed_cases else "（FAILURES 段未列出用例名）"
        problems.append(
            f"backend-pytest.log 记录的是**失败运行**（EXIT={exit_code}）：{detail}"
            f" —— 基线计数不可用；这不是「重跑一次就好」，要查清 flaky 根因"
        )
        lines.append(f"  backend-pytest.log  EXIT={exit_code}  ← 证据那次跑挂了")
    else:
        lines.append(f"  backend-pytest.log  EXIT=0（证据那次运行是绿的）")
    return lines, problems


# ---------------------------------------------------------------------------
# 文档引用的 OP 编号必须真实存在（ISS-172 收口自查发现，ISS-95 同族第 4 次）
# ---------------------------------------------------------------------------
#
# **为什么需要**：WP25 收口时，说明文档 §10.1 与附录里写了两处 `OP-181` 引用，
# 而当时登记册的**最大编号仍是 OP-180** —— 于是读者按编号去登记册里查，
# 什么也查不到。这正是铁律「声称自己有防线」（ISS-95）的形态：
# 文档看起来"已登记为待定项"，实际上那是一张**不存在的欠条**。
# 前三次现身：OP-172 / OP-173 / OP-174（WP22 收口回查 `api.py` 注释捞出来的）。
#
# **判据**：扫 `docs/**/*.md` 与 `README.md` 里所有形如 `OP-\d{2,3}` 的引用，
# 逐个核对它是否出现在登记册的 **OP 表格行**（行首 `| OP-xx |`）里。
# 只认表格行，不认正文 —— 登记册正文里会写"将来追加 OP-xxx"之类的**计划性**表述，
# 把它们算作"已存在"会让判据失去意义（同 `_DECLARED_EVIDENCE_RE` 的口径）。
#
# ★ **编号位数不是常量**（2026-10-07 WP26 收口实测发现，记 ISS-187）：
#   首版把两条正则都写死成 `\d{3}`。而本项目 OP 编号**从 2 位起步**（OP-01…OP-99），
#   过百后才自然变成 3 位 —— 于是 99 条双位数编号在**定义侧与引用侧同时失明**：
#   实测登记册 3 位表格行 89 条 / 2 位表格行 99 条（合并唯一 188 条），
#   §7 只看得见后 89 条，却打印一句读起来完全正常的「已定义 OP 89 条；幽灵引用 0 处」。
#   同一份运行里 §1（用 `OP-\d+`）报「OP 条数 188」而 §7 报「89」—— **自相矛盾**。
#   后果是 ISS-115 的**对称形态**（那次是恒真式，这次是**恒不命中**）：46 处双位数
#   引用（占全库引用 37%）从未被核对过一次，而输出说「0 处」。
#   引用侧的负向前瞻 `(?!\d)` **不可省**：否则 4 位及以上编号会被截成前 3 位，
#   造出一个**假编号**（假红的另一种成因）。
_OP_REF_RE = re.compile(r"OP-\d{2,3}(?!\d)")
_OP_ROW_DEF_RE = re.compile(r"^\|\s*(OP-\d{2,3})\s*\|", re.M)

# ★ **非数字占位号形态**（ISS-210 ③，2026-10-10 补）。
#
# 本判据上面那两条正则只认 `OP-<数字>`。而本项目**实际用过一批非数字占位号**：
# `OP-新1~OP-新9`、`OP-新A/B/C`、`OP-新N` —— 它们是"还没分配编号"的记号。
# 后果（ISS-210 实测）：`OP-新7/8` 被**两批不同内容复用**（登记册/WP27 文档说
# Outbox/字节/mime，而代码 5 个文件 14 处说固定张数/顺序对位）—— 撞号，
# 因为**占位号没有命名空间**；而幽灵引用判据的正则扫不到这种形态 ⇒
# 它们**从不被核对**，占位号可以永久悬空。
#
# 处置有两个方向，本判据选**报红**而不是"把它也当编号去核对"：
#   · 占位号**按定义**不在登记册里有条目 ⇒ 拿它当编号查必然恒红（假红），
#     而假红会逼人削弱判据（判据纪律第 8 条）。
#   · 真正要禁的是「文档/代码里**留着**占位号」这个状态本身：它意味着
#     "这里欠一张欠条，且没人知道该还哪张"。
# 因此本形态的出现一律报红，报错文案要求**当场分配正式编号**。
#
# ⚠️ 已知边界（**刻意豁免**的形态）：**讲解占位号历史的元文本**必然要写出
# 这些记号本身（例如本文件这段注释、ISS-210/213 的记录、登记册里
# "已回填为 OP-200~OP-205"的变更注记、WP27 §2.4.2 的消歧块）。
# 把它们一并报红会让判据对"唯一记录了这段历史的文字"报红 —— 那等于
# 要求删掉历史。
#
# 豁免口径**按"文本块"**（不是按文件，不建文件名白名单），且判的是**块内任一行**
# 是否含下列关键词 —— ⚠️ **不能按单行判**：这类讲解天然是**多行 blockquote**
# （关键词在首行「⚠️ 占位编号消歧」，而占位号出现在第 3、5 行的续行里），
# 单行判定会漏豁免、造出假红（本判据首版实测就是这样：314/316 两行漏网）。
# 块的定义 = 连续的 `>` 引用行（markdown blockquote 的物理形态）。
_OP_PLACEHOLDER_RE = re.compile(r"OP-新[\w]*")
_OP_PLACEHOLDER_META_HINTS: tuple[str, ...] = (
    "占位",
    "撞号",
    "改名",
    "回填",
    "消歧",
    "形态",
    "已分配",
    "正式编号",
)


def _placeholder_meta_spans(text: str) -> list[tuple[int, int]]:
    """返回**元文本块**的行号区间 `[(起, 止), ...]`（1-based，闭区间）。

    两类块：
      ① **blockquote 块** —— 连续的 `>` 行；只要块内任一行含关键词，整块豁免。
      ② **单行元文本** —— 非引用行里含关键词的（例如表格行「已回填为 OP-…」）。
    """
    spans: list[tuple[int, int]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith(">"):
            start = i
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                i += 1
            block = lines[start:i]
            if any(any(h in ln for h in _OP_PLACEHOLDER_META_HINTS) for ln in block):
                spans.append((start + 1, i))
            continue
        i += 1
    for lineno, line in enumerate(lines, 1):
        if line.lstrip().startswith(">"):
            continue
        if any(h in line for h in _OP_PLACEHOLDER_META_HINTS):
            spans.append((lineno, lineno))
    return spans


def check_op_references_resolve() -> tuple[list[str], list[str]]:
    """文档里引用的每个 `OP-xxx` 都必须是登记册里真实存在的条目。

    ⚠️ **只扫 `docs/**/*.md` 与 `README.md`**，且**排除登记册自身** ——
    登记册正文（§1.1 看板、§3.1/§3.2 统计、§6 变更日志）会写编号区间
    （`OP-175~OP-180`）与**计划中**的编号，那是它的本职，不是幽灵引用。
    """
    lines: list[str] = []
    problems: list[str] = []
    register = _read(REGISTER)
    defined = set(_OP_ROW_DEF_RE.findall(register))
    # ★ 对账：本判据的「定义侧」取数**必须与 §1 同源**（ISS-187）。
    #   两条正则各写一份是允许的，但**不允许给出两个数**：一旦编号位数、排版、
    #   前缀写法任一处变动只改了一边，这里就会立刻报红，而不是让两个数字
    #   在同一份输出里各说各话（曾出现 §1 报 188 / §7 报 89）。
    defined_any = set(re.findall(r"^\|\s*(OP-\d+)\s*\|", register, re.M))
    if defined_any != defined:
        only_any = sorted(defined_any - defined)
        only_here = sorted(defined - defined_any)
        problems.append(
            f"§7 的 OP 定义侧取数与 §1 不同源：§1 口径 `OP-\\d+` 得 {len(defined_any)} 条 / "
            f"本节口径 `OP-\\d{{2,3}}` 得 {len(defined)} 条"
            f"（仅 §1 可见 {only_any[:5]}… / 仅本节可见 {only_here[:5]}…）"
            f" —— 两处必须读同一份事实，否则会输出互相矛盾的计数"
        )

    docs = sorted(REPO_ROOT.glob("docs/**/*.md"))
    readme = REPO_ROOT / "README.md"
    if readme.is_file():
        docs.append(readme)

    ghosts: dict[str, set[str]] = {}
    scanned = 0
    placeholder_hits: dict[str, set[str]] = {}
    for doc in docs:
        rel = str(doc.relative_to(REPO_ROOT))
        if rel == str(REGISTER.relative_to(REPO_ROOT)):
            continue  # 登记册是定义的出处，它的正文不参与引用核对
        scanned += 1
        text = doc.read_text(encoding="utf-8")
        refs = set(_OP_REF_RE.findall(text))
        for ghost in sorted(refs - defined):
            ghosts.setdefault(ghost, set()).add(rel)
        # ---- 非数字占位号（ISS-210 ③）：按**元文本块**豁免，块内任一行含
        #      关键词即整块放行（这类讲解天然是多行 blockquote，见上方注释）。
        meta_spans = _placeholder_meta_spans(text)
        for lineno, line in enumerate(text.splitlines(), 1):
            if not _OP_PLACEHOLDER_RE.search(line):
                continue
            if any(lo <= lineno <= hi for lo, hi in meta_spans):
                continue
            for token in set(_OP_PLACEHOLDER_RE.findall(line)):
                placeholder_hits.setdefault(token, set()).add(f"{rel}:{lineno}")

    lines.append(
        f"  登记册已定义 OP {len(defined)} 条；扫描 {scanned} 份文档，"
        f"幽灵引用 {len(ghosts)} 处，未回填的占位号 {len(placeholder_hits)} 种"
    )
    for ghost in sorted(ghosts):
        sources = "、".join(sorted(ghosts[ghost]))
        lines.append(f"    ★幽灵 {ghost}  ←  {sources}")
        problems.append(
            f"文档引用了登记册里不存在的 {ghost}（{sources}）"
            f" —— 要么把该条真正登记进登记册，要么改正编号；"
            f"引用一张不存在的欠条，比不引用更危险（ISS-95 同族）"
        )
    for token in sorted(placeholder_hits):
        sources = "、".join(sorted(placeholder_hits[token])[:5])
        total = len(placeholder_hits[token])
        lines.append(f"    ★占位号 {token}  未回填  ←  {sources}（共 {total} 处）")
        problems.append(
            f"文档里仍有未回填的占位号 {token}（{sources}，共 {total} 处）"
            f" —— 占位号在登记册里**没有条目**、也无命名空间（ISS-210 实测"
            f"`OP-新7/8` 被两批不同内容复用），留着它等于「欠一张不知该还哪张的欠条」。"
            f"处置：当场分配正式编号并回填；若该行是在**讲解占位号历史**，"
            f"请带上「占位/撞号/回填/消歧」等字样以标明它是元文本"
        )
    return lines, problems


# ---------------------------------------------------------------------------
# 「判据纪律第 N 条」的引用集合 ⇄ 口径文件台账 双向对账（ISS-173）
# ---------------------------------------------------------------------------
#
# **为什么需要**：全仓 107 处写着「判据纪律第 N 条 / 铁律第 N 条」，而**没有任何一处
# 给出这些编号的定义**（在 docs/**、README.md、交付技能 SKILL.md 里穷举搜索零命中）。
# 更硬的一条：`第 5 条` 与 `第 12 条` 被引作**同一语义**（『没跑』与『跑失败』同形）；
# 而 `docs/m3/WP24-...说明.md` 写「承接 §4.4 的既有 **11** 条」，可是 §4.4 在
# **全部 12 个 git 提交里历来只有 7 条（G1~G7）** —— 那句承接自写下即为假。
#
# 处置**刻意不含「顺手改编号」**：没有权威序能证明 5 在 12 之前是对的，
# 按语义推断改写等于用一次猜测替换另一次猜测，还会掩盖真正的问题（ISS-169 的教训）。
# 本轮做的是：**建口径文件 + 登记台账 + 建双向对账判据 + 留改写任务（OP-182）**。
#
# **判据双向**（与铁律 14 的对账口径一致）：
#   · 实测引用的编号里出现「台账外」的 ⇒ 报红（新写了一个编号，既没定义也没登记）；
#   · 台账里的编号在实测引用里全部消失 ⇒ 报错（账本在腐烂，要更新它）。
# 只查一个方向会漏掉另一半 —— 而"漏了"与"都合规"在输出上同形。
_DISCIPLINE_REF_RE = re.compile(
    r"(判据纪律|项目铁律|铁律|纪律)\s*(?:第\s*(\d+)\s*条|#\s*(\d+))"
)
# ★ 前缀归一化：`纪律` 与 `判据纪律`、`铁律` 与 `项目铁律` 分别是**同一体系的两种写法**，
# 不是两个不同的编号主体（ISS-137：先问「是两种独立事实相遇，还是同一事实的两种写法」）。
# 若给台账补 26 行前缀别名，等于把"写法的多样性"伪装成"编号的多样性"，台账立刻失去分辨力 ——
# 那样台账会永远"对得上"，也永远发现不了真有人写了新编号。
_REF_KIND_ALIASES: dict[str, str] = {
    "判据纪律": "判据纪律",
    "纪律": "判据纪律",
    "铁律": "铁律",
    "项目铁律": "铁律",
}
# 口径文件里台账表格的编号单元格：`| 判据纪律 | 第 1 条 | ... |`，
# 也须容忍合并行 `| 项目铁律 | 第 1 / 2 / 6 / 7 / 8 条 | ... |`。
_LEDGER_ROW_RE = re.compile(
    r"^\|\s*(判据纪律|项目铁律|铁律|纪律)\s*\|\s*(第[^|]*?条)\s*\|", re.M
)
# 编号单元格可能是一串：`第 1 条`、`第 1 / 2 / 6 / 7 / 8 条` —— 取出其中**每一个**数字。
# （若只写 `第\s*(\d+)\s*条`，多编号行会静默只命中最后一个编号，
#   台账于是"少了几条"却看不出来 —— 与 ISS-143「没跑与跑失败同形」同族的静默失配。）
_LEDGER_NUM_RE = re.compile(r"第\s*([\d\s/、,]+?)\s*条")
_LEDGER_DIGITS_RE = re.compile(r"\d+")
# 这些是**有权威定义**的编号（§2 权威定义表），不参与「未定义台账」对账。
_DEFINED_REFS: frozenset[tuple[str, int]] = frozenset(
    {("铁律", 14), ("铁律", 17), ("判据纪律", 14)}
)


def check_discipline_references() -> tuple[list[str], list[str]]:
    """「第 N 条」引用集合 ⇄ `docs/判据纪律-编号口径.md` §3 台账，双向对账。"""
    lines: list[str] = []
    problems: list[str] = []
    doc = REPO_ROOT / "docs" / "判据纪律-编号口径.md"
    if not doc.is_file():
        problems.append(
            "口径文件 docs/判据纪律-编号口径.md 不存在 —— "
            "107 处「判据纪律第 N 条」引用将再次失去唯一可查的定义入口"
        )
        return ["  口径文件缺失，无法对账"], problems

    ledger: set[tuple[str, int]] = set()
    for raw_kind, cell in _LEDGER_ROW_RE.findall(_read(doc)):
        kind = _REF_KIND_ALIASES[raw_kind]
        for chunk in _LEDGER_NUM_RE.findall(cell):
            for num in _LEDGER_DIGITS_RE.findall(chunk):
                ledger.add((kind, int(num)))

    sources = sorted(REPO_ROOT.glob("docs/**/*.md"))
    readme = REPO_ROOT / "README.md"
    if readme.is_file():
        sources.append(readme)
    sources += sorted(REPO_ROOT.glob("tools/**/*.py"))
    sources += sorted(REPO_ROOT.glob("services/**/*.py"))
    sources += sorted(REPO_ROOT.glob("migrations/**/*.py"))

    found: dict[tuple[str, int], dict[str, int]] = {}
    total = 0
    for path in sources:
        rel = str(path.relative_to(REPO_ROOT))
        if rel == str(doc.relative_to(REPO_ROOT)):
            continue  # 口径文件自己是台账的出处，它的正文不参与引用核对
        text = path.read_text(encoding="utf-8")
        for match in _DISCIPLINE_REF_RE.finditer(text):
            num = int(match.group(2) or match.group(3))
            key = (_REF_KIND_ALIASES[match.group(1)], num)
            total += 1
            per_file = found.setdefault(key, {})
            per_file[rel] = per_file.get(rel, 0) + 1

    defined = _DEFINED_REFS
    undefined_found = {
        key: files for key, files in found.items() if key not in defined
    }
    unlisted = sorted(k for k in undefined_found if k not in ledger)
    vanished = sorted(k for k in ledger if k not in found)

    lines.append(
        f"  「第 N 条」引用 {total} 处 / 形态 {len(found)} 种；"
        f"台账 {len(ledger)} 条（另有 {len(defined)} 种已定义）"
    )
    for kind, num in unlisted:
        files = undefined_found[(kind, num)]
        hits = sum(files.values())
        preview = [
            f"{rel}×{cnt}" for rel, cnt in sorted(files.items(), key=lambda kv: -kv[1])
        ][:3]
        lines.append(f"    ★台账外 {kind} 第 {num} 条  ←  {hits} 处：{preview}")
        problems.append(
            f"「{kind} 第 {num} 条」被引用 {hits} 处但既无定义、也未登记进 "
            f"docs/判据纪律-编号口径.md §3 台账"
            f"（见于 {'、'.join(rel for rel in sorted(files))}）"
            f" —— 要么在 §2 给出定义，要么登记台账"
        )
    for kind, num in vanished:
        lines.append(f"    ★已消失 {kind} 第 {num} 条（台账有、引用里找不到）")
        problems.append(
            f"台账里的「{kind} 第 {num} 条」在全仓引用里已消失 —— "
            f"台账在腐烂，请更新 docs/判据纪律-编号口径.md §3"
        )
    return lines, problems


def main() -> int:
    facts = _collect_facts()

    print("## 1. 计数：实测 vs README 声明")
    readme_lines, readme_problems = check_readme(facts)
    print("\n".join(readme_lines))

    print("## 2. 登记册内部一致性")
    reg_lines, reg_problems = check_register_internals()
    print("\n".join(reg_lines))

    print("\n## 2b. README 内部一致性（同一计数多处声明不得打架）")
    readme_consistency_lines, readme_consistency_problems = (
        check_readme_internal_consistency(facts)
    )
    print("\n".join(readme_consistency_lines))

    print("\n## 3. 可执行门禁")
    gate_lines, gate_problems = check_gates()
    print("\n".join(gate_lines))

    print("\n## 4. 文档声明的证据文件必须存在")
    declared_lines, declared_problems = check_declared_evidence_exists()
    print("\n".join(declared_lines))

    print("\n## 5. 证据时效（证据不得早于被证据的代码）")
    fresh_lines, fresh_problems = check_evidence_freshness()
    print("\n".join(fresh_lines))

    print("\n## 6. 证据自身结论（那次运行必须是绿的）")
    self_lines, self_problems = check_evidence_self_report()
    print("\n".join(self_lines))

    print("\n## 7. 文档引用的 OP 编号必须真实存在于登记册")
    op_lines, op_problems = check_op_references_resolve()
    print("\n".join(op_lines))

    print("\n## 8. 「判据纪律第 N 条」引用 ⇄ 编号口径台账（双向）")
    disc_lines, disc_problems = check_discipline_references()
    print("\n".join(disc_lines))

    problems = (
        readme_problems
        + reg_problems
        + readme_consistency_problems
        + gate_problems
        + declared_problems
        + fresh_problems
        + self_problems
        + op_problems
        + disc_problems
    )
    print()
    if problems:
        print(f"RESULT=FAIL  共 {len(problems)} 处不一致：")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("RESULT=PASS  全部对账通过（计数 / 版本号 / 状态分布 / 门禁 / OP 引用 / 纪律编号）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
