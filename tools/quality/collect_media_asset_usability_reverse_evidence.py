#!/usr/bin/env python
"""`check_media_asset_usability_single_source.py` 的**反向验证**：把错误实现放回去，判据必须报红。

产出：`evidence/m3/media-asset-usability-reverse.log`

## 为什么需要这个文件

`tools/quality/check_media_asset_usability_single_source.py` 此刻打印 `[PASS]`，
但那**只证明"当前工作区看起来自洽"**，不能证明"它被违反时会报红"。两者之差
就是判据纪律第 9 条：

> **「声称自己有防线」比「没有防线」更危险。**

而本判据的存在理由恰恰是 ISS-177（`media/repository.py` 的 docstring 点名了一个
**当时并不存在**的判据脚本 —— 声称有防线）。所以对这条判据，"它真的会报红"
必须被**单独取证**，不能靠"它现在绿"来推。

## 本判据的底座**不能**是"拷一份再喂给它"

`check_media_asset_usability_single_source.py` 的覆盖范围是**全仓 312 个 .py**
（`_iter_py_files()` 用 `rglob` 由磁盘决定，铁律 14），它**不接路径参数**。
因此变异**不能**像 `collect_all_exports_reverse_evidence.py` 那样"拷一份底座
再喂给判据" —— 判据根本不看你拷的那份。

本脚本的做法（与 `collect_node_paired_guard_reverse_evidence.py` 同款）：
**把变异写进正在被扫的真实文件**（`media/repository.py`），跑完立刻还原。

⚠️ **风险与对策**（这是本脚本最要紧的一节）：

  · 变异写的是**运行中的工作树** ⇒ 若脚本崩在还原之前，工作树会留下变异。
    对策 ①：`try/finally` 包住每一次变异，`finally` 里**无条件**还原；
    对策 ②：还原后**读回来核对字节**（`assert`），不是"我以为还原了"；
    对策 ③：每次变异前后都记 `sha256[:12]`，log 里逐条打印，
            任何人都能看出"变异发生了 / 还原了"。

  · 它**不碰** `tools/regression/collect_regression_evidence.py`（收尾纪律）。

## ⚠️ 顶大的一个坑：本脚本**自己**会被判据扫到（"自伤"）

判据扫全仓 `*.py`，**包括本文件**。本脚本的变异载荷里逐字写着
`ASSET_STATE_RETIRED` / `supersedes_asset_id` / `select(` / `exists(` ——
若它们出现在**函数体内**，`_collect()` 就会把某个函数判成"持两符号 + 构造查询"，
**G1 从 1 变 2，判据被本脚本自己判红**（实测确认过这条路径会命中）。

对策（**这是本脚本全部载荷常量都放在模块级的唯一理由**）：

  · 全部载荷以**模块级字符串常量**存在 ⇒ 它们落在 `_collect_non_functional()`
    的"模块级"容器里（只影响 G1b，而 G1b 是 `>=` 下界，多一处无害），
    **永远不落在任何函数体里** ⇒ G1 不受影响。
  · 含符号的字面量**只在模块级出现一次**；函数体内只用**名字**
    （`_RETIRED_COMPARE` 之类），AST 看不见名字背后的字符串。

这不是洁癖 —— 它与判据源码里那条"关键词全部放模块级常量"的自透明设计
（`check_media_asset_usability_single_source.py` 的 §自透明）**互为镜像**：
**判据必须对自己也成立，反向验证也必须对自己成立。**

## ⚠️ 第二个坑：**临时判据副本**必须落在 `evidence/` 下

变异 `[2] / [12(G10)] / [13(G1b)]` 改的是**判据自己**（摘掉某条判定、
抬高某个下界）。改真判据是不行的（跑完要还原，且会污染工作树），
于是写一份副本再跑。副本放在 `evidence/m3/` 下，理由有两条：

  · `evidence` 在判据的 `SKIP_DIRS` 里 ⇒ **副本不会被判据自己扫到**
    （否则副本里的 `MIN_FILES = 99999` 之类会以奇怪的方式互相干扰）；
  · `evidence/m3/x.py` 的 `Path(__file__).resolve().parents[2]` **正好**是
    仓库根，与 `tools/quality/x.py` 的 `parents[2]` 一致 ⇒ 副本里的
    `REPO_ROOT` 指向同一个地方，判据行为与真判据逐字相同。

（这是从 `collect_node_paired_guard_reverse_evidence.py` 抄来的正确做法。
 抄的是**做法**，不是数字 —— 本判据的域更大，副本的落点理由也重新核对过。）

## 变异清单

| # | 变异 | 期望 | 它证明的失效面 |
|---|---|---|---|
| 0 | 未变异 | `[PASS]` | 判据在未变异时是绿的 |
| 1 | `repository.py` 里**加第二个拼点** | `[FAIL]` + `G1` | **本判据存在的理由**：两处判断必然分叉 |
| 2 | 在 `[1]` 基础上**摘掉 G1 判定** | 转假绿 | 成对证明：`[1]` 的红**确实是 G1 造成的** |
| 3 | 拼点**改名** | `[FAIL]` + `G2` | 改名后 G3~G5 的锚点即失效 |
| 4 | 把 `~exists(...)` 写成 `exists(...)` | `[FAIL]` + `G3` | 可用性**整体反向**，而运行期不报错 |
| 5 | `state` 比较从 `RETIRED` 换成 `ACTIVE` | `[FAIL]` + `G4` | 退化成"只判 state"（漏掉"是否被接替"） |
| 6 | 删掉一个调用点 | `[FAIL]` + `G5` | 私有入口被绕开（调用点下界） |
| 7 | 把拼点写进 `__all__` | `[FAIL]` + `G6` | 把"条件"交出去 = 允许第二处拼点 |
| 8 | `only_usable` 默认改 `False` | `[FAIL]` + `G7` | 默认多列一个已作废的素材 |
| 9 | 加个"只判 ACTIVE"的过滤函数 | `[FAIL]` + `G8` | 有人补 `state == ACTIVE` 当可用性判据 |
| 10 | docstring 点名一个**不存在**的脚本 | `[FAIL]` + `G9` | **ISS-177 本体** |
| 11 | 引用豁免表加一条多余项 | `[FAIL]` + `G9b` | 白名单腐烂（空占位 = 没有白名单） |
| 12 | `MIN_FILES` 抬到 99999 | `[FAIL]` + `G10` | 活性下界真的在判（不是装饰） |
| 13 | `MIN_NON_FUNC_PAIRS` 抬到 99999 | `[FAIL]` + `G1b` | 结构排除的前提真的在判 |
| 14 | —— | —— | **覆盖对账**：上表必须盖住判据源码里出现的**全部** G 编号 |

> ⚠️ `[2]` 这条**成对证明**是本仓固定手法：只证明"变异 → 报红"还不足以
> 说明**是这条判据**报的红（可能是别的判据恰好也红了）。把判定摘掉后
> 必须转为假绿，才证明因果关系。
>
> `[2]` 的隔离要**穷举连带面**（本仓在 `all-exports` 那次连踩三次）：
> 摘掉 G1 判定后，`query` 变成 2 个，而 **G2 的判定右端恰好引用了
> `query[0].rel if len(query) == 1 else ""`** ⇒ G2 会连带报红。
> 因此 `[2]` 必须**同时**摘掉 G1 与 G2 两条判定。这个连带面是**实测**
> 出来的（首版只摘 G1 ⇒ `rc=1` 且 G2 仍红 ⇒ 当场发现），不是推理出来的
> —— 推理容易漏（同族脚本在推理上连踩三次）。

## 覆盖对账（`[14]`）为什么必要

上表的"哪条变异证明哪条判据"**是我写的**。凡"覆盖范围由人写"就必须
另有一条判据对账它（判据纪律第 14 条）。`[14]` 的做法：

  · 从**判据源码**（磁盘）正则提取全部 `G数字[b]` 编号 ⇒ 集合 A；
  · 取本脚本 `MUTATION_COVERAGE` 的键 ⇒ 集合 B；
  · 断言 **A == B**（双向）。

判据将来新增一条 `G11` 而忘了在 `MUTATION_COVERAGE` 登记 ⇒ `[14]` 当场报红。

跑法：`python tools/quality/collect_media_asset_usability_reverse_evidence.py [--fast]`
退出码 0 = 全部变异都被抓住（且成对证明成立、覆盖对账通过）。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "tools" / "quality" / "check_media_asset_usability_single_source.py"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"
LOG_PATH = EVIDENCE_DIR / "media-asset-usability-reverse.log"

#: 变异底座：**真实被扫的文件**（判据接不了路径参数，见模块 docstring）。
#:
#: 选 `repository.py` 而不是别的：它是本判据**唯一**的正面事实来源
#: （唯一拼点在这里），所以"往这里加第二个拼点"就是本判据要防的那件事本身。
BASE_REL = "services/backend/src/rednote/modules/media/repository.py"

#: 本脚本产出的 checks 条数（`[0]`~`[14]` = 15 条 + 收尾还原 1 条）。
#: 写成常量并由 `complete = len(checks) >= EXPECTED_CHECKS` 核对 ——
#: 少跑几条会被 `tools/m1/check_evidence_scripts.py` 判红（条数不足会伪装成全通过）。
EXPECTED_CHECKS = 16

# ---------------------------------------------------------------------------
# 变异载荷：**全部放模块级**（理由见模块 docstring 的"自伤"一节）
# ---------------------------------------------------------------------------
#
# ⚠️ 下面这些常量里**逐字**含 `ASSET_STATE_RETIRED` / `supersedes_asset_id`，
# 以及 `select` / `exists` 这些查询符号。它们**必须**待在模块级：
# 一旦落进某个函数体，`_collect()` 就会把那个函数判成"持两符号 + 构造查询"
# ⇒ G1 从 1 变 2 ⇒ **本脚本把判据判红**。
#
# 因此：含符号的字面量只在模块级出现；函数体内只用**名字**引用它们。

#: 插入点：`_usable_asset_condition` 之后、`usable_asset_ids` 之前。
#: 选这个位置是因为它就在唯一拼点隔壁 —— 变异与本体**在同一区域**，
#: 排除了"因为隔着太远所以没被比较到"这种解释。
_ANCHOR_METHOD_HEAD = "    def usable_asset_ids(\n"

#: 载荷 `[1]`：第二个拼点（函数体内持两符号 + 构造查询）。
#: 它**语法正确、语义看起来也正确** —— 正是"两处判断必然分叉"的那个形态：
#: 两次都是"可用 ⇔ 不存在 RETIRED 且接了它"，但只要将来有人只改其中一处
#: （比如给其中一处加个 `state` 过滤），两条路径就会对同一个素材给出不同答案，
#: 而**没有任何报错**。
_PAYLOAD_SECOND_PIVOT = (
    "\n"
    "    def _probe_second_pivot(self, asset_id_column: Any) -> Any:\n"
    "        probe = aliased(ReferenceAsset)\n"
    "        return ~exists(\n"
    "            select(1)\n"
    "            .select_from(probe)\n"
    "            .where(\n"
    "                probe.state == ASSET_STATE_RETIRED,\n"
    "                probe.supersedes_asset_id == asset_id_column,\n"
    "            )\n"
    "        )\n"
    "\n"
)

#: 载荷 `[9]`：一个"只判 state"的过滤函数（**没有**取反、**没有**列符号）。
#: 它与 `[1]` 刻意不同：它**不持** `supersedes_asset_id` 的**赋值/调用**形态吗？
#: 不 —— 它持（`.supersedes_asset_id`），所以它命中 G8 而非 G1：
#: `has_state` 为 False（没有 `ASSET_STATE_RETIRED`，用的是 `ASSET_STATE_ACTIVE`）
#: ⇒ 不进 `query` ⇒ G1 不变；而 G8 恰好要找"持列符号 + 与 ACTIVE 比较"。
#: **两个变异的差异只在这一个字面量上**，因此它们互为对照。
_PAYLOAD_ACTIVE_ONLY = (
    "\n"
    "    def _probe_active_only(self, asset_id_column: Any) -> Any:\n"
    "        probe = aliased(ReferenceAsset)\n"
    "        return exists(\n"
    "            select(1)\n"
    "            .select_from(probe)\n"
    "            .where(\n"
    "                probe.state == ASSET_STATE_ACTIVE,\n"
    "                probe.supersedes_asset_id == asset_id_column,\n"
    "            )\n"
    "        )\n"
    "\n"
)

#: `[4]` 的两个锚点（取反 → 裸存在）。出现次数各断言为 1。
_NEG_EXISTS_FROM = "        return ~exists(\n"
_NEG_EXISTS_TO = "        return exists(\n"

#: `[5]` 的两个锚点（常量 → 另一个常量 / 字面量）。各 1 次。
_RETIRED_COMPARE_FROM = "                retired.state == ASSET_STATE_RETIRED,\n"
_ACTIVE_COMPARE_TO = "                retired.state == ASSET_STATE_ACTIVE,\n"

#: `[6]` 的锚点：某条调用点（出现 2 次，只替换第 1 处 ⇒ 调用点 4 → 3）。
#: ⚠️ 替换成 `stmt = stmt`（而非删掉整行）：那行是 `if only_usable:` 的**唯一**
#: 语句，删掉会变成空块 ⇒ **语法错误** ⇒ 判据报的是"无法解析"而不是 G5
#: （红色的**原因**会被打印错，本项目错误 ⑫）。变异必须保持语法正确。
_CALL_SITE_FROM = (
    "            stmt = stmt.where(self._usable_asset_condition(ReferenceAsset.id))\n"
)
_CALL_SITE_TO = "            stmt = stmt\n"

#: `[7]` 的锚点：`__all__`（1 次）。
#: ⚠️ **不带尾换行**：`repository.py` 的 `__all__` 是文件**最后一行**，
#: 而该文件末尾**没有**换行符（实测 `repr` 确认）。首版锚点写成
#: `'__all__ = ["MediaRepository"]\n'` ⇒ 出现 0 次 ⇒ 锚点自检当场崩。
#: 这正是"宁可崩，不可假绿"在起作用：若锚点失败被静默跳过，`[7]` 就成了恒绿装饰。
_ALL_FROM = '__all__ = ["MediaRepository"]'
_ALL_TO = '__all__ = ["MediaRepository", "_usable_asset_condition"]'

#: `[8]` 的锚点：`list_reference_assets` 的形参默认值（1 次）。
_ONLY_USABLE_FROM = "        only_usable: bool = True,\n"
_ONLY_USABLE_TO = "        only_usable: bool = False,\n"

#: `[3]` 的锚点：唯一拼点的**定义行**（1 次）。
_COND_DEF_FROM = "    def _usable_asset_condition(asset_id_column: Any) -> Any:\n"
_COND_DEF_TO = "    def _usable_asset_condition_v2(asset_id_column: Any) -> Any:\n"

#: `[10]` 的锚点：模块 docstring 的首行（1 次）。
#: 变异在其中插一行**点名一个不存在的脚本** —— ISS-177 的形态：
#: 声称有防线，而那个脚本根本不在磁盘上。
#: ⚠️ 脚本名必须全小写（判据的锚点正则只认 `[a-z0-9_]+`）—— 这是**实测**出来的：
#: 首版写成 `check_mediaAsset_ghost.py`，G9 因为大小写而**根本没看见**它。
_DOC_ANCHOR = '"""WP27 五张表的存储访问。\n'
_DOC_INSERT = (
    "\n"
    "见 `tools/quality/check_media_asset_ghost_pivot.py`"
    "（★ 反向验证 `[10]` 变异用：该脚本**不存在**）。\n"
)

#: `[11]`：给判据副本的 `EXEMPT_REFERENCES` 插一条**多余的**豁免
#: （该 target 没有任何 docstring 引用它 ⇒ G9b 必须报"豁免多余"）。
#: ⚠️ 字典条目必须带**尾逗号**结束 —— 首版漏了，插进去后副本直接语法错误
#: （离线预检当场抓到：`invalid syntax. Perhaps you forgot a comma?`）。
#: 若没预检，这一条会在跑完 10 分钟后才以"副本崩了"的形式暴露，
#: 且很容易被误读成"G9b 不报红"。
_EXEMPT_ANCHOR = "EXEMPT_REFERENCES: Final[dict[str, str]] = {\n"
_EXEMPT_INSERT = (
    '    "tools/quality/check_never_referenced_anywhere.py": (\n'
    '        "★ 反向验证 [11] 变异用：这条豁免是多余的 —— 没有任何 docstring "\n'
    '        "引用它，因此 G9b 必须报它。理由写得足够长以免撞上长度下界。"\n'
    '    ),\n'
)

#: `[12]` / `[13]`：改**判据副本**里的下界常量（真判据不动）。
_MIN_FILES_FROM = "MIN_FILES = 200\n"
_MIN_FILES_TO = "MIN_FILES = 99999\n"
_MIN_NON_FUNC_FROM = "MIN_NON_FUNC_PAIRS = 2\n"
_MIN_NON_FUNC_TO = "MIN_NON_FUNC_PAIRS = 99999\n"

#: `[2]`：成对证明要摘掉的两条判定（在判据副本里）。
#: ⚠️ G2 必须一起摘 —— 它的右端引用了 `query`，摘掉 G1 后 `query` 变 2 个
#: ⇒ G2 连带报红。这是**实测**的连带面，不是推理（见模块 docstring）。
_G1_DECISION_FROM = "    g1_bad = len(query) != 1\n"
_G1_DECISION_TO = "    g1_bad = False\n"
_G2_DECISION_FROM = (
    '    g2_ok = len(cond_nodes) == 1 and cond_nodes[0][0] == (query[0].rel if len(query) == 1 else "")\n'
)
_G2_DECISION_TO = "    g2_ok = True\n"

#: 覆盖对账：判据编号 → 证明它的变异编号（**人写**，由 `[14]` 与磁盘对账）。
MUTATION_COVERAGE: Final[dict[str, str]] = {
    "G1": "[1]（+ [2] 成对证明）",
    "G1b": "[13]",
    "G2": "[3]",
    "G3": "[4]",
    "G4": "[5]",
    "G5": "[6]",
    "G6": "[7]",
    "G7": "[8]",
    "G8": "[9]",
    "G9": "[10]",
    "G9b": "[11]",
    "G10": "[12]",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sha12(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _anchor(text: str, needle: str, times: int, label: str) -> None:
    """断言锚点在底座里出现 `times` 次。

    ⚠️ **取不到就抛**（不是跳过、不是"找不到就算了"）：锚点抄错了 ⇒ 变异
    静默失效 ⇒ 判据照样绿 ⇒ 本脚本变成**恒绿装饰**（ISS-143 假绿）。
    同族脚本吃过这个亏（手抄多行 SQL 锚点永远不匹配）。宁可崩，不可假绿。
    """
    got = text.count(needle)
    if got != times:
        raise AssertionError(f"锚点 {label} 出现 {got} 次（应 {times} 次）：{needle!r}")


def _run(checker: Path) -> tuple[int, str]:
    """跑某个判据（真判据或副本）。**判据不接路径参数**（范围由磁盘决定）。"""
    cmd = [sys.executable, str(checker)]
    env = {"NO_PROXY": "*", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
    return proc.returncode, proc.stdout + proc.stderr


class Mutator:
    """在真实文件上做变异，并在 `finally` 里无条件还原 + 核对字节。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.original = path.read_text(encoding="utf-8")
        self.original_sha = _sha12(path)

    def apply(self, new_text: str) -> str:
        """写入变异，返回变异后的 sha12。断言变异**确实生效**。"""
        if new_text == self.original:
            raise AssertionError("变异与原文逐字节相同 ⇒ 变异未生效（ISS-143 假绿）")
        self.path.write_text(new_text, encoding="utf-8")
        return _sha12(self.path)

    def restore(self) -> None:
        """无条件还原，并**读回来核对字节**（不是"我以为还原了"）。"""
        self.path.write_text(self.original, encoding="utf-8")
        back = self.path.read_text(encoding="utf-8")
        if back != self.original:
            raise AssertionError(f"还原失败！{self.path} 与原文不一致 —— 请手工 git checkout")
        if _sha12(self.path) != self.original_sha:
            raise AssertionError(f"还原后 sha 变了：{self.path}（应为 {self.original_sha}）")


def _write_variant(name: str, src: str) -> Path:
    """把判据副本写到 `evidence/m3/` 下（该目录在判据的 SKIP_DIRS 里，见 docstring）。"""
    path = EVIDENCE_DIR / name
    path.write_text(src, encoding="utf-8")
    return path


def _indent(text: str, limit: int = 8) -> str:
    """从判据输出里**只挑出值得看的行**（FAIL / ★ / 问题条目），减少 log 噪声。"""
    keep = [
        ln
        for ln in text.splitlines()
        if ln.strip().startswith(("[FAIL]", "★", "-", "  -"))
    ]
    return "\n".join("      " + ln.strip() for ln in keep[:limit])


def _gs_in_checker(src: str) -> set[str]:
    """从判据源码里提取全部 `G数字[b]` 编号（**磁盘决定**，不由我列举）。"""
    return set(re.findall(r"\bG\d+b?\b", src))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fast",
        action="store_true",
        help="（保留参数：本脚本无慢用例，与同族脚本口径一致）",
    )
    args = parser.parse_args()

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def emit(text: str = "") -> None:
        lines.append(text)
        print(text)

    checker_sha = _sha12(CHECKER)
    emit(f"# `check_media_asset_usability_single_source.py` 反向验证 —— {_now()}")
    emit(f"# 被测判据：tools/quality/check_media_asset_usability_single_source.py  sha256[:12]={checker_sha}")
    emit(f"# 模式：{'--fast' if args.fast else '完整'}")
    emit(f"# EXPECTED_CHECKS={EXPECTED_CHECKS}")
    emit()

    base_path = REPO_ROOT / BASE_REL
    if not base_path.is_file():
        emit(f"RESULT=FAIL 底座不存在：{BASE_REL}（期望 {EXPECTED_CHECKS} 条全绿）")
        LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 1
    base_text = base_path.read_text(encoding="utf-8")
    mut = Mutator(base_path)
    emit(f"# 底座：{BASE_REL}  sha256[:12]={mut.original_sha}（**真实被扫文件，变异后立即还原**）")

    # 全部锚点先一次性自检：任何一个对不上 ⇒ 直接崩（宁可崩，不可假绿）。
    try:
        _anchor(base_text, _ANCHOR_METHOD_HEAD, 1, "插入点 usable_asset_ids")
        _anchor(base_text, _NEG_EXISTS_FROM, 1, "[4] ~exists")
        _anchor(base_text, _RETIRED_COMPARE_FROM, 1, "[5] RETIRED 比较")
        _anchor(base_text, _CALL_SITE_FROM, 2, "[6] 调用点（应 2 处，替换第 1 处）")
        _anchor(base_text, _ALL_FROM, 1, "[7] __all__")
        _anchor(base_text, _ONLY_USABLE_FROM, 1, "[8] only_usable 默认")
        _anchor(base_text, _COND_DEF_FROM, 1, "[3] 拼点定义行")
        _anchor(base_text, _DOC_ANCHOR, 1, "[10] 模块 docstring 首行")
    except AssertionError as exc:
        emit(f"RESULT=FAIL 锚点自检失败：{exc}")
        emit("# ⇒ 变异必然失效（ISS-143 假绿）。宁可崩，不可假绿。")
        LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 1

    checker_src = CHECKER.read_text(encoding="utf-8")
    try:
        _anchor(checker_src, _EXEMPT_ANCHOR, 1, "[11] EXEMPT_REFERENCES")
        _anchor(checker_src, _MIN_FILES_FROM, 1, "[12] MIN_FILES")
        _anchor(checker_src, _MIN_NON_FUNC_FROM, 1, "[13] MIN_NON_FUNC_PAIRS")
        _anchor(checker_src, _G1_DECISION_FROM, 1, "[2] G1 判定")
        _anchor(checker_src, _G2_DECISION_FROM, 1, "[2] G2 判定（成对时须一起摘）")
    except AssertionError as exc:
        emit(f"RESULT=FAIL 判据侧锚点自检失败：{exc}")
        emit("# ⇒ 判据实现已变（判定/常量的写法与预期不符）⇒ 变异会静默失效。")
        LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 1
    emit()

    checks: list[tuple[str, bool]] = []

    def add(name: str, ok: bool) -> None:
        checks.append((name, ok))

    # ---- [0] 基线：未变异必须 PASS
    rc0, out0 = _run(CHECKER)
    ok0 = rc0 == 0 and "[PASS]" in out0 and "[FAIL]" not in out0
    emit(f"[0]  基线（未变异）            rc={rc0}  {'PASS' if ok0 else 'FAIL'}")
    add("基线绿", ok0)

    # ---- [1] 第二个拼点 ⇒ G1 必须红
    try:
        s1 = mut.apply(
            base_text.replace(_ANCHOR_METHOD_HEAD, _PAYLOAD_SECOND_PIVOT + _ANCHOR_METHOD_HEAD, 1)
        )
        rc1, out1 = _run(CHECKER)
        ok1 = rc1 != 0 and "G1 " in out1 and "[FAIL] G1" in out1
        emit(f"[1]  加第二个拼点              rc={rc1}  {'PASS' if ok1 else 'FAIL'}  sha={s1}")
        emit(_indent(out1))
        add("G1 抓得住（本判据存在理由）", ok1)
    finally:
        mut.restore()

    # ---- [2] 成对证明：摘掉 G1 判定（**并**摘 G2，实测的连带面）⇒ 必须转假绿
    try:
        cond_src = checker_src.replace(_G1_DECISION_FROM, _G1_DECISION_TO, 1).replace(
            _G2_DECISION_FROM, _G2_DECISION_TO, 1
        )
        if cond_src == checker_src:
            raise AssertionError("摘除 G1/G2 判定未生效（形状变了）")
        cond = _write_variant("_tmp_maus_checker_no_g1g2.py", cond_src)
        s2 = mut.apply(
            base_text.replace(_ANCHOR_METHOD_HEAD, _PAYLOAD_SECOND_PIVOT + _ANCHOR_METHOD_HEAD, 1)
        )
        rc2, out2 = _run(cond)
        ok2 = rc2 == 0 and "[FAIL]" not in out2
        emit(f"[2]  摘掉 G1+G2 判定（成对）  rc={rc2}  {'PASS' if ok2 else 'FAIL'}  sha={s2}")
        if not ok2:
            emit(_indent(out2))
        add("[1] 的红确实是 G1 造成的（成对证明）", ok2)
        cond.unlink(missing_ok=True)
    finally:
        mut.restore()

    # ---- [3] 拼点改名 ⇒ G2 红
    try:
        s3 = mut.apply(base_text.replace(_COND_DEF_FROM, _COND_DEF_TO, 1))
        rc3, out3 = _run(CHECKER)
        ok3 = rc3 != 0 and "[FAIL] G2" in out3
        emit(f"[3]  拼点改名                  rc={rc3}  {'PASS' if ok3 else 'FAIL'}  sha={s3}")
        emit(_indent(out3))
        add("G2 抓得住（改名即锚点失效）", ok3)
    finally:
        mut.restore()

    # ---- [4] ~exists → exists ⇒ G3 红
    try:
        s4 = mut.apply(base_text.replace(_NEG_EXISTS_FROM, _NEG_EXISTS_TO, 1))
        rc4, out4 = _run(CHECKER)
        ok4 = rc4 != 0 and "[FAIL] G3" in out4
        emit(f"[4]  ~exists → exists          rc={rc4}  {'PASS' if ok4 else 'FAIL'}  sha={s4}")
        emit(_indent(out4))
        add("G3 抓得住（可用性整体反向）", ok4)
    finally:
        mut.restore()

    # ---- [5] RETIRED → ACTIVE ⇒ G4 红
    try:
        s5 = mut.apply(base_text.replace(_RETIRED_COMPARE_FROM, _ACTIVE_COMPARE_TO, 1))
        rc5, out5 = _run(CHECKER)
        ok5 = rc5 != 0 and "[FAIL] G4" in out5
        emit(f"[5]  state 比较换成 ACTIVE     rc={rc5}  {'PASS' if ok5 else 'FAIL'}  sha={s5}")
        emit(_indent(out5))
        add("G4 抓得住（退化成只判 state）", ok5)
    finally:
        mut.restore()

    # ---- [6] 删掉一个调用点 ⇒ G5 红
    try:
        s6 = mut.apply(base_text.replace(_CALL_SITE_FROM, _CALL_SITE_TO, 1))
        rc6, out6 = _run(CHECKER)
        ok6 = rc6 != 0 and "[FAIL] G5" in out6
        emit(f"[6]  删掉一个调用点            rc={rc6}  {'PASS' if ok6 else 'FAIL'}  sha={s6}")
        emit(_indent(out6))
        add("G5 抓得住（调用点下界）", ok6)
    finally:
        mut.restore()

    # ---- [7] 把拼点写进 __all__ ⇒ G6 红
    try:
        s7 = mut.apply(base_text.replace(_ALL_FROM, _ALL_TO, 1))
        rc7, out7 = _run(CHECKER)
        ok7 = rc7 != 0 and "[FAIL] G6" in out7
        emit(f"[7]  拼点写进 __all__          rc={rc7}  {'PASS' if ok7 else 'FAIL'}  sha={s7}")
        emit(_indent(out7))
        add("G6 抓得住（私有性）", ok7)
    finally:
        mut.restore()

    # ---- [8] only_usable 默认改 False ⇒ G7 红
    try:
        s8 = mut.apply(base_text.replace(_ONLY_USABLE_FROM, _ONLY_USABLE_TO, 1))
        rc8, out8 = _run(CHECKER)
        ok8 = rc8 != 0 and "[FAIL] G7" in out8
        emit(f"[8]  only_usable 默认 False    rc={rc8}  {'PASS' if ok8 else 'FAIL'}  sha={s8}")
        emit(_indent(out8))
        add("G7 抓得住（默认最小档）", ok8)
    finally:
        mut.restore()

    # ---- [9] 加"只判 ACTIVE"的过滤函数 ⇒ G8 红
    try:
        s9 = mut.apply(
            base_text.replace(_ANCHOR_METHOD_HEAD, _PAYLOAD_ACTIVE_ONLY + _ANCHOR_METHOD_HEAD, 1)
        )
        rc9, out9 = _run(CHECKER)
        ok9 = rc9 != 0 and "[FAIL] G8" in out9
        emit(f"[9]  只判 ACTIVE 的过滤函数    rc={rc9}  {'PASS' if ok9 else 'FAIL'}  sha={s9}")
        emit(_indent(out9))
        add("G8 抓得住（漏掉是否被接替）", ok9)
    finally:
        mut.restore()

    # ---- [10] docstring 点名不存在的脚本 ⇒ G9 红（ISS-177 本体）
    try:
        s10 = mut.apply(base_text.replace(_DOC_ANCHOR, _DOC_ANCHOR + _DOC_INSERT, 1))
        rc10, out10 = _run(CHECKER)
        ok10 = rc10 != 0 and "[FAIL] G9 " in out10
        emit(f"[10] docstring 点名幽灵脚本    rc={rc10}  {'PASS' if ok10 else 'FAIL'}  sha={s10}")
        emit(_indent(out10))
        add("G9 抓得住（ISS-177 本体）", ok10)
    finally:
        mut.restore()

    # ---- [11] 豁免表加一条多余项 ⇒ G9b 红（改判据副本，底座不动）
    try:
        ex_src = checker_src.replace(_EXEMPT_ANCHOR, _EXEMPT_ANCHOR + _EXEMPT_INSERT, 1)
        if ex_src == checker_src:
            raise AssertionError("插入多余豁免未生效（形状变了）")
        ex_chk = _write_variant("_tmp_maus_checker_stale_exempt.py", ex_src)
        rc11, out11 = _run(ex_chk)
        ok11 = rc11 != 0 and "[FAIL] G9b" in out11
        emit(f"[11] 豁免表加多余项            rc={rc11}  {'PASS' if ok11 else 'FAIL'}")
        emit(_indent(out11))
        add("G9b 抓得住（白名单腐烂）", ok11)
        ex_chk.unlink(missing_ok=True)
    finally:
        mut.restore()

    # ---- [12] MIN_FILES 抬到 99999 ⇒ G10 红（改判据副本）
    try:
        big_src = checker_src.replace(_MIN_FILES_FROM, _MIN_FILES_TO, 1)
        if big_src == checker_src:
            raise AssertionError("抬 MIN_FILES 未生效（形状变了）")
        big_chk = _write_variant("_tmp_maus_checker_big_min_files.py", big_src)
        rc12, out12 = _run(big_chk)
        ok12 = rc12 != 0 and "[FAIL] G10" in out12
        emit(f"[12] MIN_FILES 抬到 99999      rc={rc12}  {'PASS' if ok12 else 'FAIL'}")
        add("G10 活性下界真的在判", ok12)
        big_chk.unlink(missing_ok=True)
    finally:
        mut.restore()

    # ---- [13] MIN_NON_FUNC_PAIRS 抬到 99999 ⇒ G1b 红（改判据副本）
    try:
        nf_src = checker_src.replace(_MIN_NON_FUNC_FROM, _MIN_NON_FUNC_TO, 1)
        if nf_src == checker_src:
            raise AssertionError("抬 MIN_NON_FUNC_PAIRS 未生效（形状变了）")
        nf_chk = _write_variant("_tmp_maus_checker_big_min_non_func.py", nf_src)
        rc13, out13 = _run(nf_chk)
        ok13 = rc13 != 0 and "[FAIL] G1b" in out13
        emit(f"[13] MIN_NON_FUNC_PAIRS 抬高   rc={rc13}  {'PASS' if ok13 else 'FAIL'}")
        add("G1b 结构排除前提真的在判", ok13)
        nf_chk.unlink(missing_ok=True)
    finally:
        mut.restore()

    # ---- [14] 覆盖对账：人写的覆盖表必须等于判据源码里的全部 G 编号
    gs_on_disk = _gs_in_checker(checker_src)
    gs_in_table = set(MUTATION_COVERAGE)
    missing = sorted(gs_on_disk - gs_in_table)
    extra = sorted(gs_in_table - gs_on_disk)
    ok14 = not missing and not extra
    emit(
        f"[14] 覆盖对账（磁盘 {len(gs_on_disk)} 条 vs 覆盖表 {len(gs_in_table)} 条）"
        f"  {'PASS' if ok14 else 'FAIL'}"
    )
    if not ok14:
        if missing:
            emit(f"      ★ 判据里有、覆盖表未登记：{'、'.join(missing)}")
        if extra:
            emit(f"      ★ 覆盖表里有、判据里没有：{'、'.join(extra)}")
    emit(f"      · 磁盘：{'、'.join(sorted(gs_on_disk))}")
    add("覆盖表与判据源码双向对账", ok14)

    # 收尾自检：底座回到原样（这是一条**独立的判据**，不是"我以为"）。
    final_sha = _sha12(base_path)
    intact = final_sha == mut.original_sha
    emit()
    emit(f"[收尾] 底座已还原：sha256[:12]={final_sha}（原 {mut.original_sha}）—— {'OK' if intact else '★未还原★'}")
    add("底座完整还原", intact)

    passed = sum(1 for _, ok in checks if ok)
    emit()
    for name, ok in checks:
        emit(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    complete = len(checks) >= EXPECTED_CHECKS
    verdict = passed == len(checks) and complete
    emit(
        f"RESULT={'PASS' if verdict else 'FAIL'} checks={passed}/{len(checks)} (期望 {EXPECTED_CHECKS})"
    )
    LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写出 {LOG_PATH.relative_to(REPO_ROOT)}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(0 if main() == 0 else 1)