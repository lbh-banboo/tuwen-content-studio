#!/usr/bin/env python
"""回归护栏的**反向验证**：把错误实现放回去，护栏必须报红。

产出的文件：`evidence/m1/regression-guardrails-reverse.log`

**为什么需要这个**（本仓 ISS-82：「判据必须能区分两种实现」）：
`collect_regression_evidence.py` 的全部 CHECK 全绿，只能证明
"此刻工作区看起来合规"；**不能**证明这些判据在被违反时会报红。
一个只会 `PASS` 的判据与没有判据等价 —— 而它的危害更大：
它会让所有人以为这里有防线（ISS-95）。

⚠️ **刻意不写条数**（初版写的是"23 条 CHECK"，2026-10-08 已是 52 条）：
那个数字住在采集脚本的 `EXPECTED_CHECKS` 里，此处再抄一遍就是
「同一个数字要在多处同步」—— 见下方 `_failures()` 上方的同款说明。

做法：把仓库整份 `cp` 到临时目录，在副本上做 6 种"错误实现"，
每次跑一遍采集脚本并断言出现预期的 FAIL，最后断言副本还原后回到全绿。

**为什么在副本里做**：第一版直接变异真树 + `finally` 还原，
结果外层进程被强杀（exit 137）—— `finally` 根本没机会跑，
`ci.yml` 里被注入的 `-q` 就留在了工作区。**"靠 finally 保证还原"
在进程被强杀时是假保证**；在副本上做则物理上不可能污染真树，
另外还额外验证了"脚本能在符号链接路径下跑"（见下）。

**顺带抓到的真实缺陷**：本工具第一次跑就全线报 BAD，
根因是 `collect_regression_evidence.py` 用 `Path(__file__).relative_to(REPO_ROOT)`
打印日志 —— macOS 上 `/var` 是 `/private/var` 的符号链接，
`REPO_ROOT` 走了 `resolve()`、`__file__` 没走，两边不同源 → `ValueError`
→ **脚本整体崩溃、一条 CHECK 都不产生**，与"全部通过"在计数上同形。
这个缺陷在 CI（`/home/example/...`，无符号链接）下永远不出现，
**只有靠"在临时副本里跑一遍"才能发现**。

跑法：
    python tools/regression/collect_regression_reverse_evidence.py
退出码 0 = 6 种变异都被抓住、且真树未被触碰。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SELF_PATH = Path(__file__).resolve()
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m1"
OUT_FILE = EVIDENCE_DIR / "regression-guardrails-reverse.log"
COLLECTOR = "tools/regression/collect_regression_evidence.py"

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

#: 变异只碰这几个文件；真树前后哈希必须一致。
GUARDED_FILES = (
    "services/backend/pyproject.toml",
    ".github/workflows/ci.yml",
    "services/backend/tests/test_health.py",
)

#: 全部变异撤掉后，采集脚本必须回到**变异前的基线**。
#:
#: **刻意不比对条数**（例如 `checks=23`）：条数是采集脚本自己的 `EXPECTED_CHECKS`，
#: 由它自己的 `complete` 判定 + `check_evidence_scripts.py` 双重把守。
#: 在这里再写死一遍，就等于同一个数字要在三个地方同步 ——
#: 一旦采集脚本加一条 CHECK，这里会**静默地**变成"期望 23 实则 24"而失败，
#: 排查成本全花在改数字上（ISS-72：阈值型防线的阈值本身也要维护）。
#:
#: **口径（ISS-167 修正后）**：不再绝对断言"RESULT=PASS 且 failures=0"，
#: 而是"与无变异副本的基线**逐条一致**"（`_failures(out) == baseline`
#: 且 `code == base_code`）。原因见 `_failures()` 的文档字符串。
def _failures(out: str) -> set[str]:
    """从采集脚本输出里抽出 FAIL 名集合（只取结尾的汇总行，不碰 `CHECK FAIL a :: b`）。

    **ISS-167（本包收口第 11 步首次真正跑通时暴露）**：本脚本把**整仓**
    `cp` 到副本，副本因而会带进**并行会话在途、尚未登记**的文件。例如
    并行会话新写的带 `regression` 标记、登记册里还没有对应行的函数，
    会让副本基线天生带一条 `register-check-*` 红。
    此时若仍断言"还原后 `failures=0`"，就把**别人的在途状态**判成了
    本工具的失败 —— 判据对"环境是干净的"做了**隐含假设**。
    正确口径是**相对基线**：变异前先跑一次拿基线集合，之后只断言
    "没有出现**基线之外的新** FAIL"。环境干净时该断言与 `failures==0` 等价。
    """
    return {
        line.strip().split("FAIL ", 1)[1].strip()
        for line in out.splitlines()
        if line.strip().startswith("FAIL ")
    }


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def _repo_rel(path: Path) -> str:
    """仓库内展示路径；不在仓库内时退回绝对路径（不抛异常）。"""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _snapshot(repo: Path) -> dict[str, str]:
    return {rel: _sha(repo / rel) for rel in GUARDED_FILES}


def _make_copy() -> Path:
    """整仓副本（排除 .git / 缓存）。真树只读。

    **副本必须建在 `/tmp`，不能用 `tempfile.mkdtemp()`**：
    macOS 的 `TMPDIR` 是 `/var/folders/...`，在本机的沙箱下
    "复制 14MB + 跑 pytest 收集"会被拖到 **200 秒/次**（实测），
    6 个用例就是 20 分钟；换到 `/tmp` 后是秒级。
    这里同时也就是那个符号链接缺陷的现场（`/var` → `/private/var`）。
    """
    base = Path("/tmp") / "rednote-guardrail-reverse"
    shutil.rmtree(base, ignore_errors=True)
    dest = base / "repo"
    shutil.copytree(
        REPO_ROOT,
        dest,
        ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".pytest_cache", "*.pyc"
        ),
    )
    return dest


def _run_collector(repo: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [PYTHON, str(repo / COLLECTOR)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "NO_PROXY": "*"},
    )
    return proc.returncode, proc.stdout + proc.stderr


def _run_check_register(repo: Path) -> tuple[int, str]:
    """直接跑登记册对账脚本，拿它的**明细**（`R3 孤儿：…` 逐行）。

    **为什么不能只看采集脚本的 stdout**（ISS-167）：采集脚本的汇总 stdout
    只打印 `FAIL <检查名>`（如 `register-check-PASS`），而 `R3 孤儿：file::func`
    这行只写进**证据文件**、不进 stdout。于是"造一个孤儿"与"基线本来就
    有别人的孤儿"在采集脚本 stdout 上**完全同形**（都是 `register-check-*`）——
    这正是 ISS-82「判据必须能区分两种实现」的同族问题。
    直接跑对账脚本、按**孤儿全名**断言，才真正有判别力。
    """
    proc = subprocess.run(
        [PYTHON, str(repo / "tools" / "regression" / "check_register.py")],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "NO_PROXY": "*"},
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    checks: list[tuple[str, bool, str]] = []
    lines: list[str] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    def emit(text: str = "") -> None:
        lines.append(text)

    def run_case(
        *,
        rel: str,
        old: str,
        new: str,
        expect_fail_marker: str,
        name: str,
        label: str,
        repo: Path,
    ) -> None:
        """在副本里做一次变异 → 跑采集 → 断言出现预期 FAIL → 还原。"""
        path = repo / rel
        original = path.read_text(encoding="utf-8")
        if old not in original:
            add(name, False, f"变异锚点未找到：{old!r}")
            emit(f"  BAD  {label} —— 变异锚点未找到")
            return
        try:
            path.write_text(original.replace(old, new, 1), encoding="utf-8")
            code, out = _run_collector(repo)
            ok = code != 0 and f"FAIL {expect_fail_marker}" in out
            add(
                name,
                ok,
                f"exit={code}，{'命中' if ok else '未命中'} FAIL {expect_fail_marker}",
            )
            emit(f"  {'OK ' if ok else 'BAD'}  {label} → exit={code}")
            for line in out.splitlines():
                if "FAIL " in line or line.startswith("RESULT"):
                    emit(f"        | {line}")
        finally:
            path.write_text(original, encoding="utf-8")

    # ------------------------------------------------------------------ §0
    emit("# 回归护栏反向验证（把错误实现放回去，判据必须报红）")
    emit(f"# 生成时间: {_now()}")
    emit(f"# 命令: python {_repo_rel(SELF_PATH)}")

    before = _snapshot(REPO_ROOT)
    repo = _make_copy()
    emit(f"# 变异副本: {repo}")
    emit()

    try:
        # -------------------------------------------------------------- §0b
        # 先跑一次副本基线（**不做任何变异**）。ISS-167：副本是整仓复制，
        # 可能带进并行会话在途、尚未登记的文件 ⇒ 基线未必"全绿"。
        # 后面凡是"期望 PASS"的断言一律相对这个基线，而非绝对 failures=0。
        base_code, base_out = _run_collector(repo)
        baseline = _failures(base_out)
        emit(f"# 副本基线（无变异）: exit={base_code} 既有 FAIL {len(baseline)} 条")
        for name in sorted(baseline):
            emit(f"#   | 基线既有（不计入本次变异结论）: {name}")
        emit()

        # -------------------------------------------------------------- §1
        emit("## [1] 六种「错误实现」逐个放回")
        run_case(
            repo=repo,
            rel="services/backend/pyproject.toml",
            old='    "smoke: ',
            new='    "smokex: ',
            expect_fail_marker="marker-registered:smoke",
            name="rev-marker-unregistered",
            label="① pyproject 去掉 smoke 标记注册",
        )
        run_case(
            repo=repo,
            rel=".github/workflows/ci.yml",
            old="run: python -m pytest -p no:cacheprovider -m smoke",
            new="run: python -m pytest -q -p no:cacheprovider -m smoke",
            expect_fail_marker="ci-no-extra-q",
            name="rev-ci-extra-q",
            label="② CI 的 pytest 加 -q",
        )
        run_case(
            repo=repo,
            rel=".github/workflows/ci.yml",
            old="\n  guard:\n",
            new="\n  guardrail:\n",
            expect_fail_marker="ci-job:guard",
            name="rev-ci-job-renamed",
            label="③ CI 门 guard 改名",
        )
        run_case(
            repo=repo,
            rel=".github/workflows/ci.yml",
            old="        run: python tools/regression/check_register.py",
            new="        run: echo skipped",
            expect_fail_marker="ci-step-declared:tools/regression/check_register.py",
            name="rev-ci-step-deleted",
            label="④ CI 删掉登记册对账步骤",
        )
        # 直接跑一次（不进 run_case，因为它"期望 PASS"而不是"期望 FAIL"）
        ci = repo / ".github/workflows/ci.yml"
        original = ci.read_text(encoding="utf-8")
        ci.write_text(
            original.replace(
                "name: backend-ci", "# 说明：pytest -q 是禁止的\nname: backend-ci", 1
            ),
            encoding="utf-8",
        )
        code, out = _run_collector(repo)
        # ISS-167：注释是**非命令**，采集脚本的结论必须与基线**逐条一致**
        # （既不多报也不漏报）。绝对口径 `failures==0` 会把基线既有红算进来。
        ok = code == base_code and _failures(out) == baseline
        add(
            "rev-comment-not-command",
            ok,
            f"注释里的 -q 不得改变采集结论（基线 {len(baseline)} 条既有 FAIL）",
        )
        emit(
            f"  {'OK ' if ok else 'BAD'}  ⑤ 只在注释里写 pytest -q → exit={code}"
            f"（基线 exit={base_code}，期望结论逐条一致）"
        )
        for name in sorted(_failures(out) ^ baseline):
            emit(f"        | 与基线不一致：{name}")
        ci.write_text(original, encoding="utf-8")
        emit()

        # -------------------------------------------------------------- §2
        emit("## [2] 造一个带标记的孤儿测试（登记册不再是自由文本）")
        health = repo / "services/backend/tests/test_health.py"
        original = health.read_text(encoding="utf-8")
        try:
            health.write_text(
                original
                + "\n\n@pytest.mark.regression\n"
                "def test_zz_guardrail_reverse_orphan() -> None:\n"
                "    assert True\n",
                encoding="utf-8",
            )
            code, out = _run_check_register(repo)
            # ISS-167：必须点名**我这一个**孤儿（`file::func` 全名），
            # 而不是"存在某个 register-check 红" —— 后者在基线已带别人孤儿时恒真。
            orphan = "test_health.py::test_zz_guardrail_reverse_orphan"
            ok = code != 0 and f"R3 孤儿：{orphan}" in out
            add("rev-orphan-marked-test", ok, f"exit={code}")
            emit(
                f"  {'OK ' if ok else 'BAD'}  ⑥ 造孤儿带标记函数 → exit={code}"
                f"（期望登记册点 name：{orphan}）"
            )
            for line in out.splitlines():
                if "FAIL" in line or line.startswith("RESULT"):
                    emit(f"        | {line}")
        finally:
            health.write_text(original, encoding="utf-8")
        emit()

        # -------------------------------------------------------------- §3
        emit("## [3] 还原后必须回到基线（证明前面是「变异」而非「破坏」）")
        code, out = _run_collector(repo)
        # ISS-167：与基线逐条比对，而不是绝对断言 failures==0 ——
        # 副本是整仓复制，可能带进并行会话在途的既有红。
        ok = code == base_code and _failures(out) == baseline
        add("rev-restore-baseline", ok, f"exit={code}，基线 {len(baseline)} 条既有 FAIL")
        emit(
            f"  {'OK ' if ok else 'BAD'}  ⑦ 副本全部还原 → exit={code}"
            f"（期望与基线逐条一致：exit={base_code}，既有 FAIL {len(baseline)} 条）"
        )
        for name in sorted(_failures(out) ^ baseline):
            emit(f"        | 与基线不一致：{name}")
        emit()

        # -------------------------------------------------------------- §4
        emit("## [4] 真树未被触碰")
        after = _snapshot(REPO_ROOT)
        same = before == after
        add("real-tree-untouched", same, f"{len(GUARDED_FILES)} 个受保护文件哈希一致")
        emit(f"  {'OK ' if same else 'BAD'}  ⑧ 真树哈希前后一致：{'是' if same else '否'}")
        for rel in GUARDED_FILES:
            if before[rel] != after[rel]:
                emit(f"        | 被改动：{rel} —— 这是本工具最严重的失败")
        emit()
    finally:
        shutil.rmtree(repo.parent, ignore_errors=True)

    # ------------------------------------------------------------------ §5
    failures = [name for name, ok, _ in checks if not ok]
    complete = len(checks) >= EXPECTED_CHECKS
    emit("## [5] 结论")
    for name, ok, detail in checks:
        suffix = f"（{detail}）" if detail else ""
        emit(f"  {'PASS' if ok else 'FAIL'} {name}{suffix}")
    emit()
    emit(
        f"RESULT={'PASS' if (not failures and complete) else 'FAIL'} "
        f"checks={len(checks)}/{EXPECTED_CHECKS} failures={len(failures)}"
    )

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"写出 {_repo_rel(OUT_FILE)}")
    print(
        f"RESULT={'PASS' if (not failures and complete) else 'FAIL'} "
        f"checks={len(checks)} 期望 {EXPECTED_CHECKS} 条 "
        f"failures={len(failures)}"
    )
    for name in failures:
        print(f"  FAIL {name}")
    return 0 if (not failures and complete) else 1


#: 本脚本应当产生的 CHECK 条数下界（6 种变异 + 还原 + 真树未动 = 8）。
#: 与采集脚本同理：没有它，"脚本崩了"与"全部通过"在计数上同形（ISS-43）。
EXPECTED_CHECKS = 8


if __name__ == "__main__":
    raise SystemExit(main())
