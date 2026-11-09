#!/usr/bin/env python3
"""WP-DEBT-04 判据自身的反向验证：证明 `check_cross_session_shared_files` 真的会报红。

## 为什么必须这个脚本

判据全绿**不能证明判据有效**（ISS-95「声称有防线比没有防线更危险」/
ISS-143「没跑与跑失败同形」）。本判据守的是一条**此前完全靠自觉**的纪律
（V2.1 §7.1「共享文件由主 agent 独占写」），所以"它到底会不会拦人"
必须由变异实测回答，而不是由它自己在正常仓库上打印 PASS 回答。

## 变异设计（每条都对应本判据开发期**真实踩到**的失效形态）

| 编号 | 变异 | 期望 | 验证的是 |
|---|---|---|---|
| R0 | 不变异，跑基线 | 全绿 | 判据在当前仓库上是绿的 |
| R① | 提交集里放一个共享文件（README） | **G2 报红** | 主判据本身有效 |
| R② | 提交集里放 `evidence/**/*.log` | **G2 报红** | **glob 形态**的模式能命中（不是只认字面路径） |
| R③ | 把 §7.1 小节标题改名 | **G1 报红** | 派生源失效时必须**报警**，不得静默当成"没有共享文件" |
| R④ | 删掉清单里一行（README） | **G1 报红** | ★ **本判据开发期实测抓出的真缺陷**：只读表时删条会被静默放行（清单 8→7、G2 照旧 PASS）⇒ 由标题的「N 处」条数声称兜住 |
| R⑤ | 提交集为空（`--paths` 后无参） | **G3 报红** | 活性下界：空集时"未触碰共享文件"是恒真（ISS-143 族） |
| R⑥ | 提交集里放一个不存在的路径 | **G3 报红** | 路径写错/展开失败时的假绿 |
| R⑦ | 全部变异后磁盘逐字未变 | 断言成立 | 判据只读、不污染共享工作树（ISS-207/217 教训：别把共享树当自己的） |
| R⑧ | `--main-agent` 在**无共享文件**时声明 | **G4 报红** | 开关不得空转：恒 ON 的豁免等于没有判据（且它会掩盖"共享清单被删空"） |
| R⑨ | `--main-agent` 在**有共享文件**时声明，且**输出含逐条放行字面** | 放行且出声 | 与 R⑧ 成对：光有报红分支不够，放行分支必须**真的打印**（否则开关=静默后门，ISS-95 的开关形态） |

⚠️ **R⑧/R⑨ 是"成对判据"的示范**：一条只说"空转要报红"的判据，会把实现推向
"开关只在有共享文件时生效"，而**放行时到底有没有出声**没人管 ——
一个**静默放行**的开关与后门同形。故 R⑨ 断言的是**输出里出现了放行路径字面**
（`README.md` 与 `命中`），而不是"退出码为 0"。

⚠️ **R⑦ 的特别说明**：本仓的工作树是**共享**的（并行会话），
一个"跑完顺手把变异体写回磁盘"的反向验证脚本，会在第一次跑时
就把污染留给别人 —— 所以还原必须用快照、且**不用 `assert`**
（崩掉会把工作树留在变异态，见判据纪律第 14 条）。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Final

REPO: Final[Path] = Path(__file__).resolve().parents[2]
TARGET: Final[Path] = REPO / "tools" / "quality" / "check_cross_session_shared_files.py"
SEAM_DOC: Final[Path] = REPO / "docs" / "m4" / "M4并行开发接缝冻结.md"
EVIDENCE: Final[Path] = REPO / "evidence" / "m3" / "cross-session-shared-files-reverse.log"


def _now() -> str:
    import datetime

    return datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def _load() -> object:
    spec = importlib.util.spec_from_file_location(
        "check_cross_session_shared_files", TARGET
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # ⚠️ **必须先注册进 `sys.modules` 再 `exec_module`**（ISS-164 / 判据 F）。
    # 不注册的表现是"现在不炸、将来炸"。
    sys.modules["check_cross_session_shared_files"] = mod
    spec.loader.exec_module(mod)
    return mod


def _snapshot() -> dict[Path, str]:
    """把会被变异的文件读进内存（只读，不写回）。"""
    return {SEAM_DOC: SEAM_DOC.read_text(encoding="utf-8")}


def _restore(snap: dict[Path, str]) -> None:
    """还原。**不用 `assert`**（崩掉会把共享工作树留在变异态）。"""
    for path, text in snap.items():
        try:
            if path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
        except OSError:  # pragma: no cover
            pass


def _run(mod: object, paths: list[str]) -> tuple[int, str]:
    """在**同进程**里跑判据的 `classify` / `load_shared_patterns`，返回 (rc, 摘要)。

    ⚠️ 不 fork 子进程：子进程要重新解析参数、还要处理中文路径引号，
    会把"判据逻辑对不对"与"CLI 引号对不对"两件事混在一起。
    这里只调库函数，CLI 形态另由 R⑥ 的 `--paths` 显式集合覆盖。
    """
    try:
        patterns = mod.load_shared_patterns()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 —— G1 就该在这里拦
        return 1, f"G1 报红：{type(exc).__name__}"
    if not paths:
        return 1, "G3 报红：提交集为空"
    missing = [p for p in paths if not (REPO / p).exists()]
    if missing:
        return 1, f"G3 报红：路径不存在 {missing}"
    hits = mod.classify(paths)  # type: ignore[attr-defined]
    if hits:
        return 1, f"G2 报红：命中 {[h[0] for h in hits]}"
    return 0, f"全绿（清单 {len(patterns)} 处）"


def _run_cli(*args: str) -> tuple[int, str]:
    """真跑**命令行**形态（R⑧/R⑨ 必须走 CLI —— 开关是 CLI 面的东西）。

    ⚠️ 与 `_run()` 的分工：`_run()` 调库函数、测判据逻辑；本函数起子进程、
    测 **CLI 解析 + 输出文案**。R⑨ 断言的是"输出里出现了放行路径字面"，
    那件事只能从**真的 stdout** 上读 —— 从库函数读不到
    （"放行了"与"放行了且说出来了"是两件事，后者才是防线）。
    """
    proc = subprocess.run(
        [sys.executable, str(TARGET), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "NO_PROXY": "*"},
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def run_cases() -> list[tuple[str, bool, str]]:
    mod = _load()
    results: list[tuple[str, bool, str]] = []
    snap = _snapshot()

    try:
        # R0 基线：正常情况下不报红
        rc, detail = _run(mod, ["tools/quality/check_cross_session_shared_files.py"])
        results.append(("R0  基线（非共享路径）全绿", rc == 0, detail))

        # R① 提交集含共享文件 ⇒ G2 报红
        rc, detail = _run(mod, ["README.md"])
        results.append(("R①  提交集含 README ⇒ G2 报红", rc == 1 and "G2" in detail, detail))

        # R② glob 形态 ⇒ G2 报红
        rc, detail = _run(mod, ["evidence/m1/backend-pytest.log"])
        results.append(
            ("R②  提交集含 evidence/*.log（glob 形态）⇒ G2 报红",
             rc == 1 and "G2" in detail, detail)
        )

        # R③ 派生源标题改名 ⇒ G1 报红
        text = snap[SEAM_DOC]
        SEAM_DOC.write_text(
            text.replace("### 7.1 共享文件归属", "### 7.1 共享目录说明", 1),
            encoding="utf-8",
        )
        try:
            rc, detail = _run(mod, ["tools/quality/check_cross_session_shared_files.py"])
            results.append(("R③  §7.1 标题改名（派生源失效）⇒ G1 报红",
                            rc == 1 and "G1" in detail, detail))
        finally:
            _restore(snap)

        # R④ 删掉清单一行 ⇒ 由标题条数声称兜住（G1 报红）
        SEAM_DOC.write_text(
            text.replace("| 6 | `README.md` | 禁改 |\n", "", 1), encoding="utf-8"
        )
        try:
            rc, detail = _run(mod, ["tools/quality/check_cross_session_shared_files.py"])
            results.append(("R④  清单删一行（8→7）⇒ G1 报红（标题条数对账兜住）",
                            rc == 1 and "G1" in detail, detail))
        finally:
            _restore(snap)

        # R⑤ 空提交集 ⇒ G3 报红
        rc, detail = _run(mod, [])
        results.append(("R⑤  提交集为空 ⇒ G3 报红（活性下界）", rc == 1 and "G3" in detail, detail))

        # R⑥ 不存在的路径 ⇒ G3 报红
        rc, detail = _run(mod, ["this/path/does/not/exist.py"])
        results.append(("R⑥  提交集含不存在路径 ⇒ G3 报红", rc == 1 and "G3" in detail, detail))

        # R⑦ 磁盘逐字还原
        after = _snapshot()
        unchanged = after == snap
        rc, detail = _run(mod, ["tools/quality/check_cross_session_shared_files.py"])
        results.append(
            ("R⑦  全部变异仅在内存/临时上进行，磁盘逐字还原且复跑仍全绿",
             unchanged and rc == 0, f"磁盘一致={unchanged}；复跑 {detail}")
        )

        # R⑧ `--main-agent` 空转 ⇒ G4 报红（走 CLI：开关是 CLI 面的东西）
        rc, out = _run_cli(
            "--paths", "tools/quality/check_cross_session_shared_files.py", "--main-agent"
        )
        results.append(
            ("R⑧  声明主 agent 却无共享文件 ⇒ G4 报红（开关不得空转）",
             rc == 1 and "G4" in out, f"rc={rc}；{'G4 报红' if 'G4' in out else out[-160:]}")
        )

        # R⑨ 放行分支必须**出声**（打印了逐条放行路径），而不只是退出码 0
        rc, out = _run_cli("--paths", "README.md", "--main-agent")
        spoke = ("README.md" in out) and ("命中" in out) and ("主 agent" in out)
        results.append(
            ("R⑨  放行分支逐条出声（输出含路径字面 + 『主 agent』），不是静默放行",
             rc == 0 and spoke,
             f"rc={rc}；出声={spoke}（退出码 0 与『说出来了』是两件事）")
        )

        # R⑩ 收尾文案不得自相矛盾：放行时不许还写"未触碰共享文件"
        contradict = "未触碰共享文件" in out
        results.append(
            ("R⑩  收尾文案与放行分支口径一致（不得一边放行一边宣称『未触碰』）",
             not contradict, f"含『未触碰共享文件』={contradict}")
        )

    finally:
        _restore(snap)

    return results


def main() -> int:
    results = run_cases()
    lines: list[str] = []
    for i, (tag, ok, detail) in enumerate(results, 1):
        lines.append(f"{'PASS' if ok else 'FAIL'}  [{i}] {tag}")
        lines.append(f"          {detail}")
    passed = sum(1 for _, ok, _ in results if ok)
    lines.append("")
    lines.append(f"{passed}/{len(results)} 条变异验证通过")
    lines.append("RESULT=" + ("PASS" if passed == len(results) else "FAIL"))

    out = [
        "# WP-DEBT-04（后半）判据的反向验证证据",
        f"# 生成时间: {_now()}",
        "# 判据: tools/quality/check_cross_session_shared_files.py",
        "# 本脚本: tools/quality/collect_cross_session_shared_files_reverse_evidence.py",
        "",
    ] + lines + [""]
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text("\n".join(out), encoding="utf-8")

    print("\n".join(lines))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
