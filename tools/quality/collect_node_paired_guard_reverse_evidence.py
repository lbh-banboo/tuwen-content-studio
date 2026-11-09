#!/usr/bin/env python
"""`check_node_paired_guard.py` 的**反向验证**：把错误实现放回去，判据必须报红。

产出：`evidence/m3/node-paired-guard-reverse.log`

## 为什么需要这个文件

`tools/quality/check_node_paired_guard.py` 此刻打印 `[PASS]`，但那**只证明
"当前工作区看起来自洽"**，不能证明"它被违反时会报红"。两者之差就是
判据纪律第 9 条：

> **「声称自己有防线」比「没有防线」更危险。**

一个恒真判据（ISS-115）、一个覆盖范围被悄悄收窄的判据（ISS-139/143）、
一个只有警告没有报红的判据 —— 三者都打印 `[PASS]`。

## 本判据的特殊之处：底座**不能**用"整个文件"

`check_node_paired_guard.py` 的覆盖范围是**全仓 309 个 .py**，
它不接路径参数 —— 这是刻意的（铁律 14：范围由磁盘决定，不由调用者指定）。
因此变异**不能**像 `collect_all_exports_reverse_evidence.py` 那样
"拷一份底座再喂给判据"。

本脚本的做法：**把变异写进正在被扫的真实文件**，跑完立刻还原。

⚠️ **风险与对策**（这是本脚本最要紧的一节）：

  · 变异写的是**运行中的工作树** ⇒ 若脚本崩在还原之前，工作树会留下变异。
    对策 ①：`try/finally` 包住每一次变异，`finally` 里无条件还原；
    对策 ②：还原后再**读回来核对字节**（`assert`），不是"我以为还原了"；
    对策 ③：每次变异前后都记 `sha256[:12]`，log 里逐条打印，
            任何人都能看出"变异发生了 / 还原了"。

  · 它**不碰** `tools/regression/collect_regression_evidence.py`（收尾纪律）。

## 变异清单

| # | 节 | 变异 | 期望 | 它证明的失效面 |
|---|---|---|---|---|
| 0 | `[0]` | 未变异 | `[PASS]` | 判据在未变异时是绿的 |
| 1 | `[1]` | 摘掉 media 的**放行守卫** | `[FAIL]` + `G1` | **ISS-175 本体**：判据抓得住"漏第 1 道" |
| 2 | `[2]` | 把 media 的**两道判断写反** | `[FAIL]` + `G2` | 守卫不可达（等价于漏掉） |
| 3 | `[3]` | 在 `[1]` 基础上**摘掉 G1 判定** | 假绿 | 成对证明：`[1]` 的红**确实是 G1 造成的** |
| 4 | `[4]` | media 的配对函数**改名**（`*_node_paired_ddl` → 别的） | `[FAIL]` + `G5` | **铁律 14**：覆盖范围不许由名字单方面决定 |
| 5 | `[5]` | `MIN_PAIRED` 抬到 999 | `[FAIL]` + `G7` | 活性下界真的在判（不是装饰） |
| 6 | `[6]` | 把 media 的守卫表名改成**不存在的表** | `[FAIL]` + `G6` | 守卫表名拼错 ⇒ 运行期才炸 |

> ⚠️ `[3]` 这条**成对证明**是本仓固定手法：只证明"变异 → 报红"还不足以
> 说明**是这条判据**报的红（可能是别的判据恰好也红了）。把判定摘掉后
> 必须转为假绿，才证明因果关系。
>
> `[3]` 的隔离要**穷举连带面**（本仓在 `all-exports` 那次连踩三次）：
> 摘掉 media 的守卫不仅触发 G1，还会连带触发 **G2**（没有守卫时
> "守卫在配对判定之前"无从谈起 —— 本判据的 G2 实现是
> `if f.guard_table and f.has_pair and not f.guard_before_pair`，
> 守卫为 None 时 G2 **自然通过**）。实测 G2 不连带 ⇒ 只需摘 G1。
> 这个"实测"而不是"推理"很重要 —— 推理容易漏（见同族脚本的坑 2）。

跑法：`python tools/quality/collect_node_paired_guard_reverse_evidence.py [--fast]`
退出码 0 = 全部变异都被抓住（且成对证明成立）。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "tools" / "quality" / "check_node_paired_guard.py"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"
LOG_PATH = EVIDENCE_DIR / "node-paired-guard-reverse.log"

#: 变异底座：**真实被扫的文件**。选 media（本包刚落的，结构最新、最典型）
#: 而不是拷一份 —— 因为判据接不了路径参数（见模块 docstring）。
BASE_REL = "services/backend/src/rednote/modules/media/ddl.py"

#: 守卫段 / 配对段的**定位标记**（源码里的一行，逐字）。
#:
#: ⚠️ **不许手抄整段源码**（初版就是这么错的，2026-10-07 实测）：
#: `ddl.py` 里的守卫是 **Python 字符串字面量**（`"    IF NOT EXISTS (\n"`），
#: 不是真实换行的 SQL。手抄时会自然写成换行形态 ⇒ 锚点永远不匹配
#: ⇒ 全部变异静默失效。那一刻脚本**正确地崩了**（"宁可崩，不可假绿"），
#: 但这说明手抄锚点本身就是一处"覆盖范围由人写"。
#:
#: 正解：只标记**一行**（含 SQL 片段，字面量里是唯一的），
#: 由 `_locate_block()` 在**磁盘上的真实源码**里按行推出整块的范围。
#: 段边界也由**结构**决定（前一行是 `IF NOT EXISTS (`，后一行是 `END IF;`），
#: 不是由我数行数决定。
_GUARD_MARKER = "SELECT 1 FROM media_items WHERE id = NEW.entity_id"
_PAIR_MARKER = "SELECT 1 FROM media_versions WHERE id = NEW.id"

#: 块尾扫描的**兜底上限**（行）。只用于"跑飞了要报错"，不是块长度的假设 ——
#: 块长度由结构（下一个 `END IF;`）决定，不由这个数决定。
#: 2026-10-07 实测：守卫段 5 行、配对段 9 行（含 3 行 `_raise_block` message）。
_BLOCK_SCAN_LIMIT = 40


def _locate_block(src: str, marker: str, *, need_return_null: bool) -> tuple[int, int]:
    """按标记行推出它所在 IF 块的**行号区间** `[start, end)`。

    结构（本仓 6 个包一致，格式由 `_raise_block` 与手写约定固化）：

        "    IF NOT EXISTS (\\n"            ← start
        "        SELECT 1 FROM <表> WHERE ..."  ← marker 所在行
        "    ) THEN\\n"
        "        RETURN NULL;\\n"           ← 守卫段才有
        "    END IF;\\n"                    ← end - 1

    取不到就**抛**（不是跳过）：段边界变了必须让人知道，否则变异静默失效。
    """
    lines = src.splitlines(keepends=True)
    hit = [i for i, ln in enumerate(lines) if marker in ln]
    if len(hit) != 1:
        raise AssertionError(f"标记 {marker!r} 在底座里出现 {len(hit)} 次（应恰好 1 次）")
    i = hit[0]
    if "IF NOT EXISTS (" not in lines[i - 1]:
        raise AssertionError(f"标记行 i-1 不是 `IF NOT EXISTS (`：{lines[i - 1]!r}")
    start = i - 1
    # 往后找本块的 `END IF;`。
    #
    # ⚠️ **窗口不能用固定行数**（初版写死 6 行，实测配对段就超出）：
    #    配对的 `RAISE` 走的是多行 `_raise_block(...)` 调用
    #    （`+ _raise_block(` · 3 行 message · `)`），于是 `END IF;` 落在
    #    标记行之后第 7 行 ⇒ 写死 6 会误报"没找到"。那**不是**结构变了，
    #    是判据自己的窗口假设错了 —— 又一次"按长相判而非按事实判"。
    #    正解：按**结构**收束 —— 从 `IF NOT EXISTS (` 起，遇到块内的
    #    `END IF;` 才停；上限 `_BLOCK_SCAN_LIMIT` 只用于兜底（防跑飞）。
    end = None
    for k in range(i, min(i + _BLOCK_SCAN_LIMIT, len(lines))):
        if "END IF;" in lines[k]:
            end = k + 1
            break
    if end is None:
        raise AssertionError(
            f"从 {start} 起 {_BLOCK_SCAN_LIMIT} 行内没找到 `END IF;` —— 块结构变了"
        )
    # 反向核对：块内不许**再**出现 `IF NOT EXISTS (`（那说明块边界划错了，
    # 会把两个块并成一个 ⇒ 变异删掉的东西多于预期）。
    inner = "".join(lines[start + 1 : end - 1])
    if "IF NOT EXISTS (" in inner:
        raise AssertionError(
            f"块 [{start}, {end}) 内还有第二个 `IF NOT EXISTS (` —— 块边界划错了"
        )
    body = "".join(lines[start:end])
    if need_return_null and "RETURN NULL;" not in body:
        raise AssertionError("守卫段里没有 `RETURN NULL;` —— 结构契约变了")
    return start, end


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sha12(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _run(checker: Path) -> tuple[int, str]:
    """跑判据。**判据不接路径参数**（范围由磁盘决定）。"""
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
    emit(f"# `check_node_paired_guard.py` 反向验证 —— {_now()}")
    emit(f"# 被测判据：tools/quality/check_node_paired_guard.py  sha256[:12]={checker_sha}")
    emit(f"# 模式：{'--fast' if args.fast else '完整'}")
    emit()

    base_path = REPO_ROOT / BASE_REL
    if not base_path.is_file():
        emit(f"RESULT=FAIL 底座不存在：{BASE_REL}")
        LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 1
    base_text = base_path.read_text(encoding="utf-8")
    mut = Mutator(base_path)
    emit(f"# 底座：{BASE_REL}  sha256[:12]={mut.original_sha}（**真实被扫文件，变异后立即还原**）")

    # 段边界由**结构**推出（不是手抄、不是数行数）。取不到就崩 —— 宁可崩不可假绿。
    try:
        g_lo, g_hi = _locate_block(base_text, _GUARD_MARKER, need_return_null=True)
        p_lo, p_hi = _locate_block(base_text, _PAIR_MARKER, need_return_null=False)
    except AssertionError as exc:
        emit(f"RESULT=FAIL 定位变异锚点失败：{exc}")
        emit("# ⇒ 变异必然失效（ISS-143 假绿）。宁可崩，不可假绿。")
        LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 1

    base_lines = base_text.splitlines(keepends=True)
    guard_block = "".join(base_lines[g_lo:g_hi])
    pair_block = "".join(base_lines[p_lo:p_hi])
    emit(f"# 守卫段：行 {g_lo + 1}~{g_hi}（{len(guard_block)} 字符，含 RETURN NULL）")
    emit(f"# 配对段：行 {p_lo + 1}~{p_hi}（{len(pair_block)} 字符）")
    if not (g_lo < p_lo):
        emit("RESULT=FAIL 守卫段不在配对段之前 —— 底座已不合预期（判据与底座的前提冲突）")
        LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 1
    emit()

    checks: list[tuple[str, bool]] = []

    def add(name: str, ok: bool) -> None:
        checks.append((name, ok))

    # ---- [0] 基线：未变异必须 PASS
    rc0, out0 = _run(CHECKER)
    ok0 = rc0 == 0 and "[PASS]" in out0
    emit(f"[0] 基线（未变异）              rc={rc0}  {'PASS' if ok0 else 'FAIL'}")
    add("基线绿", ok0)

    # ---- [1] ISS-175 本体：摘掉 media 的放行守卫
    try:
        s1 = mut.apply(base_text.replace(guard_block, "", 1))
        rc1, out1 = _run(CHECKER)
        ok1 = rc1 != 0 and "G1" in out1 and "media" in out1
        emit(f"[1] 摘掉放行守卫（ISS-175）     rc={rc1}  {'PASS' if ok1 else 'FAIL'}  sha={s1}")
        emit(_indent(out1))
        add("G1 抓得住（ISS-175 本体）", ok1)
    finally:
        mut.restore()

    # ---- [2] 把两道判断写反（守卫段搬到配对段之后）
    try:
        swapped = base_text.replace(guard_block, "", 1).replace(pair_block, pair_block + guard_block, 1)
        s2 = mut.apply(swapped)
        rc2, out2 = _run(CHECKER)
        ok2 = rc2 != 0 and "G2" in out2
        emit(f"[2] 两道判断写反                rc={rc2}  {'PASS' if ok2 else 'FAIL'}  sha={s2}")
        emit(_indent(out2))
        add("G2 抓得住（顺序）", ok2)
    finally:
        mut.restore()

    # ---- [3] 成对证明：在 [1] 基础上摘掉 G1 判定 ⇒ 必须转假绿
    # ⚠️ 隔离要穷举连带面。实测：摘掉守卫后 G2 的实现
    #    （`if f.guard_table and f.has_pair and not f.guard_before_pair`）
    #    因 guard_table 为 None 而**自然通过** ⇒ 无需同时摘 G2。
    #    这里**实测**而不是推理（同族脚本曾在推理上连踩三次）。
    try:
        c3 = EVIDENCE_DIR / "_tmp_checker_no_g1.py"
        c3_src = CHECKER.read_text(encoding="utf-8")
        c3_text = c3_src.replace(
            "    g1_bad = [f for f in funcs if not f.guard_table]\n",
            "    g1_bad = []\n",
            1,
        )
        assert c3_text != c3_src, "摘除 G1 判定未生效（形状变了）"
        c3.write_text(c3_text, encoding="utf-8")
        s3 = mut.apply(base_text.replace(guard_block, "", 1))
        rc3, out3 = _run(c3)
        ok3 = rc3 == 0 and "[FAIL]" not in out3
        emit(f"[3] 摘掉 G1 判定（成对）        rc={rc3}  {'PASS' if ok3 else 'FAIL'}  sha={s3}")
        add("[1] 的红确实是 G1 造成的", ok3)
        c3.unlink(missing_ok=True)
    finally:
        mut.restore()

    # ---- [4] 改名（铁律 14：覆盖范围不许由名字单方面决定）
    try:
        s4 = mut.apply(base_text.replace("media_node_paired_ddl", "media_nodes_guard_ddl"))
        rc4, out4 = _run(CHECKER)
        ok4 = rc4 != 0 and "G5" in out4 and "media" in out4
        emit(f"[4] 配对函数改名                rc={rc4}  {'PASS' if ok4 else 'FAIL'}  sha={s4}")
        emit(_indent(out4))
        add("G5 抓得住（改名后两来源分叉）", ok4)
    finally:
        mut.restore()

    # ---- [5] 活性下界抬到 999
    try:
        c5 = EVIDENCE_DIR / "_tmp_checker_floor.py"
        c5_src = CHECKER.read_text(encoding="utf-8")
        c5_text = re.sub(r"^MIN_PAIRED = \d+", "MIN_PAIRED = 999", c5_src, flags=re.M)
        assert c5_text != c5_src, "抬高下界未生效"
        c5.write_text(c5_text, encoding="utf-8")
        rc5, out5 = _run(c5)
        ok5 = rc5 != 0 and "G7" in out5
        emit(f"[5] G7 下界抬到 999             rc={rc5}  {'PASS' if ok5 else 'FAIL'}")
        add("G7 活性下界在判", ok5)
        c5.unlink(missing_ok=True)
    finally:
        mut.restore()

    # ---- [6] 守卫表名改成不存在的表
    try:
        s6 = mut.apply(
            base_text.replace(
                "SELECT 1 FROM media_items WHERE id = NEW.entity_id",
                "SELECT 1 FROM media_items_TYPO WHERE id = NEW.entity_id",
                1,
            )
        )
        rc6, out6 = _run(CHECKER)
        ok6 = rc6 != 0 and "G6" in out6
        emit(f"[6] 守卫表名拼错                rc={rc6}  {'PASS' if ok6 else 'FAIL'}  sha={s6}")
        emit(_indent(out6))
        add("G6 抓得住（表名对照）", ok6)
    finally:
        mut.restore()

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
    verdict = passed == len(checks)
    emit(f"RESULT={'PASS' if verdict else 'FAIL'} checks={passed}/{len(checks)}")
    LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写出 {LOG_PATH.relative_to(REPO_ROOT)}")
    return 0 if verdict else 1


def _indent(text: str) -> str:
    keep = [
        ln
        for ln in text.splitlines()
        if ln.strip().startswith(("[FAIL]", "★", "-", "  -", "         ★"))
    ]
    return "\n".join("      " + ln.strip() for ln in keep[:6])


if __name__ == "__main__":
    sys.exit(main())