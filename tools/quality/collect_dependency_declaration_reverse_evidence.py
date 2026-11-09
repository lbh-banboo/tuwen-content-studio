#!/usr/bin/env python
"""WP-DEBT-01：**依赖声明判据的反向验证**（把错误实现放回去，判据必须报红）。

产出：`evidence/m3/dependency-declaration-reverse.log`

## 为什么需要这个文件

`tools/quality/check_dependency_declarations.py` 此刻打印 `[PASS]`，
但那只证明"**当前工作区看起来合规**"，**不能证明"它被违反时会报红"**。

这两件事的差别就是本项目判据纪律第 9 条：
> **"声称自己有防线"比"没有防线"更危险。**

一个恒真判据（ISS-115）、一个覆盖范围被悄悄收窄的判据（ISS-139/143）、
一个只有警告没有报红的判据 —— 三者都**打印 `[PASS]`**。
本脚本把"它会报红"从**声明**变成**可复跑的证据**。

## 七个变异（每个都对应一条真实失效面）

| # | 变异 | 期望报红 | 它证明的失效面 |
|---|---|---|---|
| ① | 删 `cryptography` 声明（**顶层**硬导入） | `缺少发行包 **cryptography**` | 反向覆盖判据的基本盘 |
| ② | 删 `python-docx` 声明（**函数体内**延迟导入） | `缺少发行包 **python-docx**` | `ast.walk` 真扫了函数体（只扫 `tree.body` 会**假绿**） |
| ③ | 映射表 `{"docx": "docx"}`（别名写回自己） | `发行名被写成自己` | 映射表自检 —— 防"看起来有、实际指错包" |
| ④ | 源码新增未登记的 `import absent_third_party_pkg` | `未登记映射` | 新依赖写错名字**不静默跳过** |
| ⑤ | `cryptography` 从 `dependencies` 挪进 `[dev]` | `不许只放进任何 extra 组` | 作用域分桶（**抓的就是 httpx 那个真实缺陷**） |
| ⑥ | `_scan_imports` 把 `ast.walk(tree)` 收窄成 `tree.body` | `<UNUSED-DECLARATION:python-docx>` | **判据的覆盖范围本身有守门人** |
| ⑦ | `_scan_imports` 把 `rglob` 改成 `glob`（非递归） | `扫描范围缩水` | 铁律 14 第 5 次复发候选 |

### 用例 ② 是**最关键**的一条

`src` 下 `python-docx` / `openpyxl` / `pypdf` / `Pillow` 四个包**全部**只出现在
**函数体内**（`parsers/*.py` 的延迟导入注释写着"避免 import 模块即拉起重依赖"）。
若判据只扫模块顶层，这 4 个包会被判成"代码没用"，
而主线的"代码 import 的都在声明里"**照样全绿** —— 整整一类依赖无人看管。
用例 ② 通过"删掉它们的声明必须报红"来反证扫描确实深入了函数体。

### 用例 ⑥ / ⑦ 是**判据自身**的守门人（本仓第 5 次复发形态）

铁律 14 序列：ISS-104（写死目录）→ ISS-111（只换藏身处）→ ISS-139
（glob 只扫 `tools/m1/`）→ ISS-143（复跑载体把跨包脚本路径写死）。
本工具的覆盖范围由 `ast.walk` 与 `rglob` 决定 —— **都是"一个字写错就静默"**：

* ⑥ 收窄成 `tree.body`：**文件数不变**，`_count_py_files_independently` 对账
  **抓不到** ⇒ 唯一表征是"4 个声明看起来没人用"（即互补判据那条）。
* ⑦ 改成非递归：`src` 从 174 个 `.py` 掉到 **0** 个，
  而 `found` 仍返回一个"看起来正常"的映射 ⇒ 只有**独立实现对账**能抓。

两个用例分别验证这两条守门人**各自能报红**。若它们不能报红，
那么用例 ①~⑤ 的"报红"也可能只是因为碰巧 —— 判据的覆盖范围本身没有守门人。

## 做法（照 `tools/m1/collect_schema_parity_reverse_evidence.py` 的模式）

整仓复制到 `/tmp/rdbdep/repo`（**副本**，真树只读），在副本上做变异、
跑判据、断言报红，最后断言副本还原后回绿、真树**整树**哈希未变。

**为什么必须在副本里做**（ISS-103 的教训）：第一版反向验证直接变异真树 +
`finally` 还原，结果外层进程被强杀（exit 137），`finally` 没机会跑，
被注入的改动留在了工作区。**"靠 finally 保证还原"在进程被强杀时是假保证。**

## 不需要数据库

被测判据是**纯静态**（AST + TOML），无任何 DB 依赖 ⇒ 本脚本不与
WP25 等并行会话抢测试库，可在任意时刻跑（本包选它做"零冲突"的核心理由）。

跑法：

    python tools/quality/collect_dependency_declaration_reverse_evidence.py

退出码 0 = 7 种变异都被抓住、副本还原后回绿、真树未被触碰。
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
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"
OUT_FILE = EVIDENCE_DIR / "dependency-declaration-reverse.log"

#: 被测判据（相对仓库根的路径；副本里会解析到副本的根）。
TOOL_REL = "tools/quality/check_dependency_declarations.py"

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

#: 「真树未被触碰」的取证范围：**整树**，不是"我打算改的那几个文件"。
#:
#: 只哈希几个文件是**自证清白** —— 若变异逻辑写错位置（路径拼错、误改了别处），
#: 那份清单上的哈希依然一致，于是"真树未被触碰"在**真的被触碰过**的情况下
#: 照样 PASS（与本包 `collect_schema_parity_reverse_evidence.py` 的
#: `TREE_SCOPE` 说明同款）。
TREE_SCOPE = (
    "services/backend",
    "migrations",
    "tools",
)

#: 哈希快照要排除的目录名（缓存/构建产物，不是源码）。
_EXCLUDE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def _repo_rel(path: Path) -> str:
    """仓库内展示路径；不在仓库内时退回绝对路径（不抛异常）。

    ⚠️ 必须容错：macOS 上 `/var` 是 `/private/var` 的符号链接，副本路径
    走 `resolve()` 而 `REPO_ROOT` 已 resolve，两边不同源会 `ValueError`。
    这个坑在 `collect_regression_evidence.py` 上真实发生过并导致**整脚本崩溃、
    一条 CHECK 都不产生**（ISS-102）—— 崩溃与全过在计数上同形。
    """
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _snapshot(repo: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in TREE_SCOPE:
        base = repo / rel
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if any(part in _EXCLUDE_DIRS for part in path.parts):
                continue
            if path.suffix == ".pyc":
                continue
            out[str(path.relative_to(repo))] = _sha(path)
    return out


def _make_copy() -> Path:
    """整仓副本（排除 .git / 缓存）。真树只读。

    **副本建在 `/tmp`**（不是 `tempfile.mkdtemp()`）：macOS 的 `TMPDIR` 是
    `/var/folders/...`，本机沙箱下"复制 + 跑子进程"会被拖慢数量级；
    换到 `/tmp` 后是秒级。
    """
    base = Path("/tmp") / "rdbdep"
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


def _run_tool(repo: Path) -> tuple[int, str]:
    """在副本里跑被测判据，返回 `(退出码, 合并输出)`。"""
    proc = subprocess.run(
        [PYTHON, str(repo / TOOL_REL)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"},
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    checks: list[tuple[str, bool, str]] = []
    lines: list[str] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    def emit(text: str = "") -> None:
        lines.append(text)

    def run_case(
        *,
        repo: Path,
        edits: list[tuple[str, str, str]],
        expect_marker: str,
        name: str,
        label: str,
    ) -> None:
        """在副本里做一组变异 → 跑判据 → 断言报红 → 还原。

        `edits` 是 `[(相对仓库根的文件, 原文, 替换文)]`。**多文件多锚点**都要传，
        每个锚点都必须命中 —— 锚点未命中说明脚本与代码已脱节，直接判 BAD
        （否则"变异没生效"会伪装成"判据没报红"，两种结论完全相反）。

        ## ⚠️ 还原必须回**变异前**的文本（本脚本第一版在这里栽过）

        第一版把"当前内容"写回了 `originals[path]`：

            path.write_text(originals[path].replace(old, new, 1), ...)
            originals[path] = (repo / rel).read_text(...)   # ← 致命：覆盖了原件

        于是 `finally` 里"还原"写回的是**变异后**的内容 —— 副本**越跑越脏**，
        且失败链条极隐蔽：① 删掉的 `cryptography` 从未归还
        → ⑤ 的锚点找不到（报"脚本与代码已脱节"，指向完全无关的方向）
        → ② 删的 `python-docx` 也遗留 → ⑥ 产不出 `<UNUSED-DECLARATION:python-docx>`
        → ⑧ "还原回绿"必然失败。**一个还原缺陷伪装成了三个互不相关的判据失败。**

        现在拆成两本账：`original_texts`（**全程只读**，finally 的唯一来源）
        与 `current`（变异态，仅供本次跑判据用）。
        """
        original_texts: dict[Path, str] = {}
        for rel, _old, _new in edits:
            path = repo / rel
            if path not in original_texts:
                original_texts[path] = path.read_text(encoding="utf-8")
        # 锚点命中检查（在写入任何东西之前全查完 —— 保证失败时副本未被改动）。
        for rel, old, _new in edits:
            if old not in original_texts[repo / rel]:
                add(name, False, f"变异锚点未找到：{rel} :: {old[:60]!r}")
                emit(f"  BAD  {label} —— 变异锚点未找到（脚本与代码已脱节）")
                return

        try:
            # 在"变异态"上**链式**应用编辑（同一文件的多个锚点按顺序替换）。
            current: dict[Path, str] = dict(original_texts)
            for rel, old, new in edits:
                path = repo / rel
                current[path] = current[path].replace(old, new, 1)
            for path, text in current.items():
                path.write_text(text, encoding="utf-8")

            code, out = _run_tool(repo)
            hit = expect_marker in out
            ok = code != 0 and hit
            add(
                name,
                ok,
                f"exit={code}，{'命中' if hit else '未命中'} {expect_marker!r}",
            )
            emit(f"  {'OK ' if ok else 'BAD'}  {label} → exit={code}")
            # 只摘录与断言有关的关键行，避免证据文件被整段输出淹没。
            for line in out.splitlines():
                s = line.strip()
                if not s:
                    continue
                if expect_marker in line or s.startswith("[FAIL]") or s.startswith("[PASS]"):
                    emit(f"        | {s[:160]}")
                elif s.startswith("  [") and "——" in s:
                    emit(f"        | {s[:160]}")
        finally:
            # 还原**唯一**来源 = `original_texts`（全程未被变异污染）。
            for path, text in original_texts.items():
                path.write_text(text, encoding="utf-8")

    # ------------------------------------------------------------------ §0
    emit("# WP-DEBT-01 依赖声明判据的反向验证（把错误实现放回去，判据必须报红）")
    emit(f"# 生成时间: {_now()}")
    emit(f"# 命令: python {_repo_rel(SELF_PATH)}")
    emit(f"# 被测判据: {TOOL_REL}")
    emit("# 说明: 纯静态判据（AST + TOML），无需数据库 ⇒ 与并行会话零资源冲突")

    before = _snapshot(REPO_ROOT)
    repo = _make_copy()
    emit(f"# 变异副本: {repo}（真树只读）")
    emit()

    try:
        # -------------------------------------------------------------- §1
        emit("## [0] 副本基线：变异之前必须是绿的（否则后面全是噪音）")
        code, out = _run_tool(repo)
        base_green = code == 0 and "[PASS]" in out
        add("baseline-green-in-copy", base_green, f"exit={code}，[PASS]={'是' if '[PASS]' in out else '否'}")
        emit(f"  {'OK ' if base_green else 'BAD'}  副本基线 → exit={code}（期望 0 / [PASS]）")
        if not base_green:
            for line in out.splitlines()[-12:]:
                emit(f"        | {line.strip()[:160]}")
        emit()

        # -------------------------------------------------------------- §2
        emit("## [1] 删顶层硬导入的声明（cryptography，platform/tokens.py 顶层）")
        run_case(
            repo=repo,
            edits=[
                (
                    "services/backend/pyproject.toml",
                    '    "cryptography==50.0.0",\n',
                    "",
                )
            ],
            expect_marker="缺少发行包 **cryptography**",
            name="rev-missing-top-level-decl",
            label="① 删 cryptography 声明",
        )
        emit()

        # -------------------------------------------------------------- §3
        emit("## [2] 删**函数体内**延迟导入的声明（python-docx，parsers/office.py:60）")
        emit("   （这条反证 AST 扫描确实深入函数体：只扫 tree.body 会让它假绿）")
        run_case(
            repo=repo,
            edits=[
                (
                    "services/backend/pyproject.toml",
                    '    "python-docx==1.1.2",\n',
                    "",
                )
            ],
            expect_marker="缺少发行包 **python-docx**",
            name="rev-missing-deferred-import-decl",
            label="② 删 python-docx 声明（延迟导入）",
        )
        emit()

        # -------------------------------------------------------------- §4
        emit("## [3] 映射表别名写回自己（docx → docx，指向 PyPI 上的废弃同名包）")
        run_case(
            repo=repo,
            edits=[(TOOL_REL, '    "docx": "python-docx",', '    "docx": "docx",')],
            expect_marker="发行名被写成自己",
            name="rev-mapping-alias-self",
            label="③ 映射表 docx→docx",
        )
        emit()

        # -------------------------------------------------------------- §5
        emit("## [4] 源码新增未登记的导入名（新依赖写错名字不得静默跳过）")
        run_case(
            repo=repo,
            edits=[
                (
                    "services/backend/src/rednote/platform/tokens.py",
                    "from cryptography.fernet import Fernet, InvalidToken",
                    "from cryptography.fernet import Fernet, InvalidToken\n"
                    "import absent_third_party_pkg  # 反向验证用：未在映射表登记",
                )
            ],
            expect_marker="未登记映射",
            name="rev-unregistered-import-name",
            label="④ 新增未登记导入名",
        )
        emit()

        # -------------------------------------------------------------- §6
        emit("## [5] 运行时依赖挪进 extra 组（抓的就是 httpx 那个真实缺陷）")
        run_case(
            repo=repo,
            edits=[
                (
                    "services/backend/pyproject.toml",
                    '    "cryptography==50.0.0",\n',
                    "",
                ),
                (
                    "services/backend/pyproject.toml",
                    'dev = [\n    "pytest==8.3.3",\n]',
                    'dev = [\n    "pytest==8.3.3",\n    "cryptography==50.0.0",\n]',
                ),
            ],
            expect_marker="不许只放进任何 extra 组",
            name="rev-runtime-dep-in-extra",
            label="⑤ cryptography 从 dependencies 挪进 [dev]",
        )
        emit()

        # -------------------------------------------------------------- §7
        emit("## [6] 判据自身的守门人 A：扫描收窄到只扫模块顶层")
        emit("   （`all_names` 从 `ast.walk(tree)` 收窄成 `tree.body`：**文件数不变**，")
        emit("     `_count_py_files_independently` 对账抓不到；")
        emit("     唯一表征 = 四个延迟导入的声明看起来'没人用'）")
        run_case(
            repo=repo,
            edits=[
                (
                    TOOL_REL,
                    "        all_names = _top_names(list(ast.walk(tree)))",
                    "        all_names = _top_names(tree.body)",
                )
            ],
            expect_marker="<UNUSED-DECLARATION:python-docx>",
            name="rev-scan-narrowed-to-body",
            label="⑥ ast.walk → tree.body（收窄扫描）",
        )
        emit()

        # -------------------------------------------------------------- §8
        emit("## [7] 判据自身的守门人 B：扫描退化成非递归（铁律 14 第 5 次复发候选）")
        emit("   （`rglob` → `glob`：src 从 174 个 .py 掉到 **0** 个，")
        emit("     而 `found` 仍返回'看起来正常'的映射 —— 只有独立实现对账能抓）")
        run_case(
            repo=repo,
            edits=[
                (
                    TOOL_REL,
                    '    for path in sorted(base.rglob("*.py")):',
                    '    for path in sorted(base.glob("*.py")):',
                )
            ],
            expect_marker="扫描范围缩水",
            name="rev-scan-non-recursive",
            label="⑦ rglob → glob（非递归）",
        )
        emit()

        # -------------------------------------------------------------- §9
        emit("## [8] 还原后必须回到基线（证明前面是「变异」而非「破坏」）")
        code, out = _run_tool(repo)
        green = code == 0 and "[PASS]" in out and "[FAIL]" not in out
        add("restored-to-baseline", green, f"exit={code}，[PASS]={'是' if '[PASS]' in out else '否'}")
        emit(f"  {'OK ' if green else 'BAD'}  副本全部还原 → exit={code}（期望 0 / [PASS]）")
        emit()

        # -------------------------------------------------------------- §10
        emit("## [9] 真树未被触碰（整树哈希，不是自证清白的那几个文件）")
        after = _snapshot(REPO_ROOT)
        changed = sorted(rel for rel in before if before.get(rel) != after.get(rel))
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        same = not changed and not added and not removed
        add("real-tree-untouched", same, f"{len(before)} 个文件哈希一致")
        emit(
            f"  {'OK ' if same else 'BAD'}  真树 {len(before)} 个文件哈希前后一致："
            f"{'是' if same else '否'}"
        )
        for rel in changed:
            emit(f"        | 内容被改：{rel} —— 这是本工具最严重的失败")
        for rel in added:
            emit(f"        | 新增文件：{rel}")
        for rel in removed:
            emit(f"        | 文件丢失：{rel}")
        emit()
    finally:
        shutil.rmtree(repo.parent, ignore_errors=True)

    # ------------------------------------------------------------------ §11
    failures = [name for name, ok, _ in checks if not ok]
    complete = len(checks) >= EXPECTED_CHECKS
    emit("## [10] 结论")
    for name, ok, detail in checks:
        suffix = f"（{detail}）" if detail else ""
        emit(f"  {'PASS' if ok else 'FAIL'} {name}{suffix}")
    emit()
    emit(
        f"RESULT={'PASS' if (not failures and complete) else 'FAIL'} "
        f"checks={len(checks)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）"
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


#: 本脚本应产生的 CHECK 条数下界
#: （副本基线 1 + 7 种变异 + 还原回绿 1 + 真树未动 1 = 10）。
#: 没有它，"脚本崩了"与"全部通过"在计数上同形（ISS-43）。
EXPECTED_CHECKS = 10


if __name__ == "__main__":
    raise SystemExit(main())
