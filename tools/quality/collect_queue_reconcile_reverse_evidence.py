#!/usr/bin/env python
"""队列对账判据的反向验证（WP47④，异常矩阵第 ⑭ 项）。

## 这份脚本要证明什么

`check_backup_restore_drill.py` 新增了 6 条 `queue:*` 判据。它们的共同形态是
**"声称自己有防线"**（ISS-95）：每一条都会在全绿时报出"队列逐列对账通过"。

而队列对账这一块**此前有过一次真实的假绿**：2026-10-07 首跑的演练只造了
`users` 与 `version_nodes` 两张表，67 张表逐表比对里那四张队列表全0 行 ——
`0 == 0` 恒真，备份把队列内容全丢了照样报"行数相等"。
**判据在自己的队列表上犯了它 docstring 里写下的 ISS-115。**

⇒光"跑一次绿了"不能证明这 6 条在干活。本脚本**逐个注入破坏**，
要求每一种破坏都让判据报红，然后**逐字还原**。

## 为什么必须做成常驻脚本而不是跑一次就记进日志

第 5 条纪律（ISS-143）：**「没跑」与「跑失败」同形**。
终端里跑过一次变异、下次没人再跑，几周后判据退化成恒真而**无人知晓**
—— 那与"当初就没写"完全同形。
⇒ 落盘 + 挂 CI 门 3（与 WP47② 的 `collect_alerting_rules_reverse_evidence.py` 同形）。

## 变异的 5 种失效形态（每种对应一种具体的"退化成恒真"路径）

| 编号 | 注入 | 对应的失效形态 |
|---|---|---|
| R1 | 种子摘掉一个 job 状态 | 该状态从未被对账 ⇒ 半个恒真式 |
| R2 | 种子行数缩到不够走完状态 | 取模循环造不出末几个状态 |
| R3 | `QUEUE_TABLES` 与 `QUEUE_DIGEST_COLUMNS` 脱钩 | 少一张表**既不报错也不比对** |
| R4 | 列项从二元组退化成字符串 | 解包取首字母 ⇒ 摘要退化成"行数+首字母" |
| R5 | 反向验证被改名 | 灵敏度自证消失而条数不变 |
| R6 | 磁盘逐字还原 | 证明上面 5 次变异没有留下污染 |

## 用法

    python tools/quality/collect_queue_reconcile_reverse_evidence.py
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

#: pytest 汇总行的样子："14 passed in 0.03s" / "1 failed, 13 passed in ..."
_PYTEST_SUMMARY_RE = re.compile(r"\b\d+ (?:passed|failed|error|errors|skipped)\b")
#: 进度行："....F....[ 46%]"
_PYTEST_PROGRESS_RE = re.compile(r"^([.FEsx]+)\s+\[")

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
BACKEND: Final[Path] = REPO_ROOT / "services" / "backend"
DRILL_DB: Final[Path] = REPO_ROOT / "tools" / "ops" / "drill_db.py"
DRILL_CHECK: Final[Path] = REPO_ROOT / "tools" / "quality" / "check_backup_restore_drill.py"
TEST_FILE: Final[str] = "tests/test_backup_restore_drill.py"

#: 变异清单。**每项必须三样齐全**：锚点唯一（`src.count(old) == 1`）、
#: 锚点确实存在（否则跳过）、改完真的会让判据红（否则本脚本自己报红）。
#:
#: ⚠️ 为什么锚点不用正则：正则写错了会"静默不匹配"，而 `count != 1` 这个
#: fail-fast 会当场报出来—— 它把"变异根本没生效"与"判据没报红"区分开了。
#: 不做这个区分的话，R1~R5 全绿可能只是因为**一个都没改上**。
MUTATIONS: Final[tuple[tuple[str, Path, str, str, str], ...]] = (
    (
        "R1 种子摘掉一个 job 状态（CANCEL_REQUESTED）",
        DRILL_DB,
        '    "SUCCEEDED", "PARTIAL", "FAILED", "CANCEL_REQUESTED", "CANCELLED",',
        '    "SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED",',
        "该状态从未被逐列对账验过 ⇒ 判据是半个恒真式",
    ),
    (
        "R2 种子行数缩到不够走完状态",
        DRILL_DB,
        "SEED_JOBS = 9",
        "SEED_JOBS = 4",
        "取模循环造不出末几个状态，而判据仍会全绿",
    ),
    (
        "R3 表清单脱钩（摘掉一张队列表）",
        DRILL_DB,
        '    "outbox_events", "jobs", "job_attempts", "idempotency_records",\n)',
        '    "outbox_events", "jobs", "job_attempts",\n)\n'
        '_UNUSED_QUEUE_TABLE = "idempotency_records"',
        "少那张表既不报错也不参与对账",
    ),
    (
        "R4 列项从二元组退化成字符串",
        DRILL_DB,
        '        ("id", "id"), ("event_type", "event_type"),',
        '        "id", "event_type",',
        "解包后 SQL 只取首字母 ⇒ 摘要退化成'行数+首字母'",
    ),
    (
        "R5 反向验证被改名（灵敏度自证消失）",
        DRILL_CHECK,
        '            "reverse:queue-digest-detects-content-change", q_reverse_ok,',
        '            "reverse:queue-digest-detects-content-change-removed", q_reverse_ok,',
        "条数不变而灵敏度自证消失 ⇒ 判据无法自证在干活",
    ),
)

EVIDENCE: Final[Path] = REPO_ROOT / "evidence" / "m5" / "wp47-queue-reconcile-reverse.log"

#: 变异脚本数 + 基线 + 还原 = 7 条。
EXPECTED_CASES: Final[int] = len(MUTATIONS) + 2


def _run_pytest() -> tuple[int, str]:
    """跑判据自身的 pytest。返回 (退出码, 人可读结果行)。

    ## ⚠️ 为什么 CLI 上**不能再传 `-q`**（2026-10-08 实测，本脚本首版踩到）

    `services/backend/pyproject.toml` 的 `addopts` 里已经有 `-q`。CLI 再传一个
    会叠加成 `-qq`，**连汇总行 `14 passed in 0.03s` 一起压掉** —— 最后一行只剩
    进度条 `..............[100%]`，而退出码仍是 0。

    首版正是这样写的，于是 R0 的证据块**打出来是空的**（靠"过滤含 passed 的行"
    收集读数 ⇒ 收集到空字符串 ⇒ 仍判PASS）。这与本包纪律第11 条同形：
    **"没东西可查"被当成"查过了没问题"。** 本仓`tools/m1/collect_wp10_evidence.py`
    的 `_pytest_result()` 早就写过同一个坑的解法，此处同形处理。

    ⇒ 三重防护：① 不传 `-q`；② 优先抓汇总行；③ **抓不到就 fail-fast**。
    """
    env = dict(os.environ, NO_PROXY="*", PYTHONPATH="src")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_FILE, "-p", "no:cacheprovider",
         "--no-header"],
        cwd=str(BACKEND), capture_output=True, text=True, env=env,
    )
    out = proc.stdout + proc.stderr
    return proc.returncode, _pytest_summary(out)


def _pytest_summary(output: str) -> str:
    """从 pytest 输出提取一行人可读结果。抓不到汇总行就退化为数进度标记。

    与 `tools/m1/collect_wp10_evidence.py::_pytest_result` 同形。
    返回 `NO SUMMARY ...` 时调用方**必须报红**，不得当正常读数。
    """
    for line in reversed(output.splitlines()):
        if _PYTEST_SUMMARY_RE.search(line):
            return line.strip()

    passed = failed = 0
    for line in output.splitlines():
        match = _PYTEST_PROGRESS_RE.match(line)
        if match:
            marks = match.group(1)
            passed += marks.count(".")
            failed += marks.count("F") + marks.count("E")
    if passed or failed:
        return f"{passed} 个通过标记 / {failed} 个失败标记（无汇总行，进度标记统计）"
    return "NO SUMMARY（既无汇总行也无进度标记）"


def _mutate(path: Path, old: str, new: str) -> None:
    """原地替换。**锚点必须恰好命中 1 次**，否则抛异常。

    「恰好 1」而不是「至少 1」：命中 2 次以上意味着锚点不唯一，
    替换会改掉多处、变异内容与预期不符 ⇒ 报错内容会误导读证据的人。
    """
    src = io.open(path, encoding="utf-8").read()
    hits = src.count(old)
    if hits != 1:
        raise RuntimeError(f"锚点在 {path.name} 命中 {hits} 次（要求恰好 1）")
    io.open(path, "w", encoding="utf-8").write(src.replace(old, new))


def _restore(path: Path, old: str, new: str) -> str | None:
    """把 `new` 逐字换回 `old`。成功返回 None，失败返回一句人可读的原因。

    ## 为什么不写成 `assert`（2026-10-08 Code Review 改）

    首版是 `assert new in current`，看起来更直白，但它有两个问题：

    1. **崩掉整个脚本** ⇒ 后面 R6 不跑、证据不落盘、读数丢失；
    2. 更糟的是 **磁盘停留在变异状态** —— 而这是共享工作树，
       `check_backup_restore_drill.py` 并行会话也在动。若"变异 → pytest"窗口内
       别人改了那个文件，`new` 就可能不在里面了。

    ⇒ 失败必须**返回**而不是抛出，由调用方显式报红并中止后续变异。
    """
    try:
        current = io.open(path, encoding="utf-8").read()
        if new not in current:
            return f"{path.name} 里找不到变异内容（可能被并行会话改过）"
        # 先读回再写：不用内存里的旧副本覆盖别人的并发改动
        io.open(path, "w", encoding="utf-8").write(current.replace(new, old, 1))
        # 二次确认：写回去之后 `new` 必须已经不在
        if new in io.open(path, encoding="utf-8").read():
            return f"{path.name} 还原后变异内容仍在"
        return None
    except OSError as exc:
        return f"{path.name} 还原时 IO 失败：{exc}"


def main() -> int:
    lines: list[str] = [
        "## WP47④ 队列对账判据 —— 反向验证",
        "",
        "证明 `check_backup_restore_drill.py` 的 6 条 `queue:*` 判据真会报红，",
        "而不是在队列表退化成空表时仍然全绿（2026-10-07 首跑的真实假绿形态）。",
        "",
        f"生成时间：{datetime.now(timezone.utc).isoformat()}",
        f"pytest 目标：{TEST_FILE}",
        "",
    ]
    results: list[tuple[str, bool, str]] = []

    # ---------- R0 基线 ----------
    rc, summary = _run_pytest()
    # fail-fast：读不到 pytest 读数时，下面的「报红/全绿」都不可信。
    # 这不是多余的防御 —— 首版就是因为汇总行被 `-qq` 压掉而读到空字符串。
    readable = not summary.startswith("NO SUMMARY")
    baseline_ok = rc == 0 and readable
    lines += ["### R0 基线（未变异）", "", "```", summary, "```", ""]
    if baseline_ok:
        lines += ["⇒ 变异前判据自测全绿，且读到了 pytest 汇总行。", ""]
    elif rc == 0:
        lines += [
            "⇒ ⚠️ **exit=0 但读不到 pytest 汇总行** —— 汇总行被 `-qq` 或重定向吞掉。",
            "这属于「没跑」与「跑失败」同形（纪律第 5 条）：下面的'报红'不可信。",
            "",
        ]
    else:
        lines += ["⇒ ⚠️ 基线就不绿，下面的'报红'不可信。", ""]
    results.append(("R0 基线（未变异）全绿且可读", baseline_ok,
                    summary if readable else f"读不到读数：{summary}"))

    # ---------- R1~R5逐个变异 ----------
    for name, path, old, new, why in MUTATIONS:
        rel = path.relative_to(REPO_ROOT)
        try:
            _mutate(path, old, new)
        except RuntimeError as exc:
            # 锚点不存在 / 不唯一 ⇒ 变异没生效，后面全部读数不可信（fail-fast）
            results.append((name, False, f"变异未生效：{exc}"))
            lines += [
                f"### {name}",
                "",
                f"❌ **变异未生效**：{exc}",
                "",
                "⇒ 磁盘处于**未变异**状态，但已不可信（脚本与代码已脱钩）⇒ 中止后续变异。",
                "",
            ]
            break

        try:
            rc, summary = _run_pytest()
            red = rc != 0
            detail = summary
        finally:
            # ---- 还原：必须在任何情况下把磁盘放回去 ----
            # ⚠️ 共享工作树风险（2026-10-08 评审发现）：被改的文件
            #    `check_backup_restore_drill.py` 并行会话也在动。若它在
            #    "变异 → pytest"窗口内被别人改过，`new` 可能已不在文件里
            #    ⇒ 首版写的是 `assert new in current`，异常会直接崩掉脚本，
            #    **而磁盘停留在变异状态**。必须吞掉异常并显式报红。
            restore_note = _restore(path, old, new)
            if restore_note is not None:
                results.append((name, False, f"还原失败：{restore_note}"))
                lines += [
                    f"### {name}",
                    "",
                    f"- 注入：`{old.strip()[:64]}` → `{new.strip()[:64]}`",
                    f"- 结果：⚠️ **{restore_note}**",
                    "",
                    "⇒ **磁盘可能残留变异内容**，后续所有读数不可信，已中止。",
                    "   请人工检查该文件后重跑本脚本。",
                    "",
                ]
                break

        results.append((name, red, f"{rel} ⇒ {detail}"))
        mark = "✅ 报红" if red else "❌ 未报红"
        lines += [
            f"### {name}",
            "",
            f"- 注入：`{old.strip()[:64]}` → `{new.strip()[:64]}`",
            f"- 为什么这是缺陷：{why}",
            f"- 结果：**{mark}**（pytest exit={rc}）",
            "",
            "```",
            detail,
            "```",
            "",
        ]

    # ---------- R6 还原 ----------
    rc, summary = _run_pytest()
    restored_ok = rc == 0 and not summary.startswith("NO SUMMARY")
    results.append(("R6 磁盘逐字还原后全绿且可读", restored_ok, summary))
    lines += [
        "### R6 磁盘逐字还原",
        "",
        "```",
        summary,
        "```",
        "",
        f"- 结果：**{'✅ 回绿' if restored_ok else '❌ 仍红'}**（pytest exit={rc}）",
        f"- 意义：上面 {len(MUTATIONS)} 次变异没有在磁盘上留残留 —— 否则后续所有读数都不可信。",
        "",
    ]

    failed = [n for n, ok, _ in results if not ok]
    total = len(results)
    lines += [
        "---",
        "",
        "## 读数",
        "",
    ]
    for name, ok, detail in results:
        lines.append(f"- {'PASS' if ok else 'FAIL'} {name} —— {detail}")
    lines += [
        "",
        f"RESULT={'PASS' if not failed else 'FAIL'} "
        f"cases={total}/{EXPECTED_CASES} failures={len(failed)}",
    ]
    if total != EXPECTED_CASES:
        lines.append(
            f"  ⚠️ 读数 {total} ≠ 期望 {EXPECTED_CASES} —— "
            f"MUTATIONS 条数与 EXPECTED_CASES 脱钩了"
        )
    lines.append("")

    # 自落盘（铁律 14：证据不许只靠人手 `| tee`）
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"（证据已落盘：{EVIDENCE.relative_to(REPO_ROOT)}）")
    return 1 if (failed or total != EXPECTED_CASES) else 0


if __name__ == "__main__":
    sys.exit(main())