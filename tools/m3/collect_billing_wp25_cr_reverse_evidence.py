#!/usr/bin/env python
"""WP25 CR 收集器 **4 条判据口径修正**的反向验证（把缺陷实现放回去，判据必须报红）。

产出：`evidence/m3/wp25-code-review-reverse.log`

## 为什么需要这个文件

`tools/m3/collect_wp25_code_review.py` 首轮运行报 `31 PASS / 4 FAIL`。
逐条定位后，**4 条全部是判据口径错，不是实现错** —— 也就是说，
本包的真实实现是好的，**判据本身在两种实现下都报同一个结果**。
这与 WP24 的「错误 ⑫」同族。

修完之后，那 4 条判据打印 `CHECK OK`。但"打印 OK"**只证明当前工作区
看起来合规**，不能证明"实现退化了它会报红"。这两件事的差别正是本项目
判据纪律第 7 条与第 9 条：

> · **判据必须能区分"正确实现"与"缺陷实现"**（第 7 条）
> · **「声称自己有防线」比「没有防线」更危险**（ISS-95）

⚠️ 尤其要注意：**改判据若不做反向验证，只是把"假红"换成"假绿"** ——
那是更坏的形态，因为它看起来在保护你。因此本脚本存在的唯一理由是：
把那 4 条新口径的**灵敏度**从声明变成可复跑的证据。

## 被测的 4 条判据与它们各自的失效面

| 判据 | 旧口径为什么恒 FAIL | 变异（退化形态） | 它证明的失效面 |
|---|---|---|---|
| C17 不可变表清单 | 在 `ddl.py` 找 `IMMUTABLE_TABLES` 赋值，但常量**定义在 `models.py`**（ddl 只 import，反向 import 会成环）⇒ 永远解析不到 | `models.py` 少写一张表 | 不可变触发器**漏挂一整张表**（`usage_ledger` 可改 = 审计体系可改） |
| C18 部分唯一索引 WHERE | 正则写的是 `= {`（dict 形态），真身是 **tuple-of-tuple** `= (` ⇒ 永远匹配不到 | `WHERE` 写成 `None` | 索引**静默退化**成全表唯一 ⇒ 第二条人工分录（无键）被拒 |
| C31 夹具口径 | 在**原始文本**找 `inbound == 0`，命中的是 docstring 里**逐字写的反例说明** | 注入真实的 `assert inbound == 0`（非 docstring） | 照抄 WP24 口径 ⇒ 夹具必然失败，且失败信息指向错误方向 |
| C32 迁移链单一 head | 逐行正则把 docstring 里**以 `down_revision` 起头的说明行**当成赋值 ⇒ 9 个假 head | wp25 迁移的 `down_revision` 改成 `None` | 双 head ⇒ alembic `Multiple head` 直接崩 |

## 为什么 C32 的新口径是**拓扑**而不是等式

初版新口径写成 `heads == ["daea2b27212c"]` —— 当前正确，但**是时间炸弹**：
下一个包（WP26）合法追加迁移后 head 必然推进，判据在**没人做错事**时变红。
这正是 ISS-44/64/71/83/87 已现身 5 次的形态，而"永远红的判据等于没有判据"
（ISS-143）—— 最后会被人加进白名单或直接关掉。

故改用两条**对任何后续包恒成立**的拓扑断言：

  ① `len(heads) == 1`（alembic 硬约束；本包 `down_revision` 被摘成 `None` ⇒ heads 变 2 ⇒ 红）
  ② `daea2b27212c` 在 head 的**祖先链**上可达（"我建的还在链上"）

这条 ② 比 `_revision_is_at_least()`（长度/字典序近似，ISS-115 的恒真式）
**精确**：可达性是事实，不是近似。本脚本的 D④ 用例专门证明这一点 ——
它把 head 换成"长度更大但与本包无血缘"的 revision，断言拓扑判据**仍然报红**
（而长度近似判据会假绿）。

## 做法：整仓副本 + 内存备份，真树只读

所有变异都在 `mkdtemp` 出来的**整仓副本**上做，真树一个字节都不动
（末尾用整树哈希前后比对证明 —— 不是自证清白，见 `[17]`）。
副本里跑的是**收集器本体**（`tools/m3/collect_wp25_code_review.py`），
断言它**报红且指名预期的 CHECK FAIL 行**。

⚠️ 只断言 `rc != 0` 是不够的：脚本因任何原因崩溃也会 `rc != 0`
（ISS-143：「没跑」与「跑失败」同形）。故每条用例都断言**具体的 CHECK 行**。

跑法：
    NO_PROXY='*' python tools/m3/collect_billing_wp25_cr_reverse_evidence.py
退出码 0 = 全部通过。

## ★ 文件名为什么是 `collect_billing_wp25_cr_reverse_evidence.py`

本文件初名 `collect_wp25_code_review_reverse.py`，收口期被
`tools/quality/check_evidence_scope.py` 的 **D2**（`B ⊆ A1`：凡写 evidence
的脚本都必须被复跑载体覆盖）**当场报红** —— 它不被任何 `SCOPE_PATTERNS`
命中（三条 glob 都要求以 `_evidence.py` 或 `code_review.py` 结尾）。
这正是铁律 14 要防的"覆盖范围由名字决定、漏掉是静默的"，而**判据抓住了它**。

改名时又踩到第二层：`collect_wp25_code_review_reverse_evidence.py` 会被
`collect_wp*_evidence.py` 这条**更窄**的 glob 命中，而该 glob 的语义是
"两层结构（外层 + 内联 `r\"\"\"` 段）⇒ **必须**能找到内联段"——
本脚本是单层结构，于是 `check_evidence_scripts.py` 报"未找到内联脚本"。

⇒ 两个 glob 的交集是个陷阱。既有 5 个反向脚本**全部**避开了 `wp*_` 段
（`collect_schema_parity_reverse_evidence.py` / `collect_regression_reverse_evidence.py`
/ `collect_discipline_reverse_evidence.py` / `collect_test_db_credibility_reverse_evidence.py`
/ `collect_dependency_declaration_reverse_evidence.py`）。
本文件名因此对齐这个既有约定：**`collect_<主题>_reverse_evidence.py`，
主题里不带 `wp<NN>_`**。

（命名由"磁盘上的 glob 与既有同族文件"决定，不是我随手起的 ——
判据 D2 与 `check_inline_compiles` 共同把这条约束**机械地**加在了文件名上。）
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"
OUT_LOG = EVIDENCE_DIR / "wp25-code-review-reverse.log"

PYTHON = sys.executable

#: 被测对象：CR 收集器本体（**不是**被打分的实现代码）。
TOOL_REL = "tools/m3/collect_wp25_code_review.py"

#: 第二个被测对象：**门禁本体** `check_evidence_scripts.py`。
#:
#: ⚠️ 它必须被验，理由同 ISS-143：一条"只加不改"的门禁与没有门禁同形。
#: 新加的 `check_returns_exit_code` 若自己恒绿，就又是一处"声称有防线"（ISS-95）。
GATE_REL = "tools/m1/check_evidence_scripts.py"

#: 会被变异的文件（相对仓库根）。取整树哈希时排除临时目录/字节码。
MUTABLE_RELS = {
    "models": "services/backend/src/rednote/modules/billing/models.py",
    "ddl": "services/backend/src/rednote/modules/billing/ddl.py",
    "tests": "services/backend/tests/test_billing_wp25.py",
}
_MIG_GLOB = "*daea2b27212c*.py"

#: 整树哈希的取证范围。与 `collect_discipline_reverse_evidence.py` 同口径：
#: 只覆盖本包会碰的目录，避免并行会话在 `docs/` 等处的写入造成**假红**。
TREE_SCOPE = (
    "services/backend/src/rednote",
    "services/backend/tests",
    "migrations",
    "tools/m3",
)
_EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot(repo: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in TREE_SCOPE:
        base = repo / rel
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix == ".pyc":
                continue
            if any(part in _EXCLUDE_DIRS for part in path.parts):
                continue
            out[str(path.relative_to(repo))] = _sha(path)
    return out


def _dirty_at_start() -> set[str]:
    """取证窗口**开始之前**就已 git 脏的路径（并行会话正在改的东西）。

    ⚠️ 跨时刻的整树哈希是**可变量**，不是不变量：本仓并行开发是常态，
    窗口内别人改一次文件，完整版就**必然**红 —— 而"必定红"与"真的坏了"同形
    （ISS-143）。隔离范围由 `git status` **现场决定**，不由人写名单（铁律 14）。

    ⚠️ `-c core.quotepath=false` 不可省（ISS-166）：默认会把非 ASCII 路径转义成
    八进制，与 `TREE_SCOPE` 里的真实路径永不相等 ⇒ 该隔离的没隔离。
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "-C",
                str(REPO_ROOT),
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode != 0:
        return set()
    out: set[str] = set()
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        rel = line[3:].strip().strip('"')
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1].strip().strip('"')
        out.add(rel)
    return out


def _make_copy(tag: str) -> Path:
    """把整仓复制到**唯一**临时目录（`mkdtemp` 原子唯一，避免同名互踩）。"""
    base = Path(tempfile.mkdtemp(prefix=f"wp25cr-{tag}-"))
    dest = base / "repo"
    shutil.copytree(
        REPO_ROOT,
        dest,
        ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.pyc", ".pytest_cache",
            ".mypy_cache", ".ruff_cache", ".venv", "node_modules",
        ),
    )
    return dest


def _run(repo: Path, extra_args: list[str] | None = None, timeout: int = 300) -> tuple[int, str]:
    """在副本里跑收集器，返回 `(退出码, 合并输出)`。

    ⚠️ `cwd` 必须是**副本根**：收集器用 `Path(__file__).parents[2]` 定位仓库。
    `NO_PROXY='*'` 不可省（本机代理劫持 localhost）。
    """
    return _run_rel(repo, TOOL_REL, extra_args, timeout)


def _expected_checks_from_tool() -> int:
    """从被收集器**自己的源码**派生 `EXPECTED_CHECKS`（铁律 14：别写死）。

    ## 为什么必须有这个函数（ISS-171）

    本脚本最初把基线的判定写成**绝对断言**：

        and "36 PASS / 0 FAIL / 36 checks" in out

    那是"当时快照"。收集器随后从 36 涨到 39（CR-3 用例缺陷判据化）再到 40
    （C35 重复度不变量），而这两行**从未同步** ⇒ 第 `[1]` 节基线 `BAD`、
    第 `[11]` 节恢复 `BAD`，两条恒 FAIL。它在 36 时代生成过一次证据就再没重跑，
    于是 `evidence/m3/wp25-code-review-reverse.log` **从此是陈旧的**
    （文件在、格式对、退出码 1，只有结论早已不是事实）——
    与 ISS-78（元信息写死）、ISS-72（阈值长期小于实测值）同族。

    更值得记的是**修法方向**：ISS-167 当时把这个脚本自己的病灶
    （"整仓副本的基线必然携带别人的在途状态"）判为"绝对断言 → 相对基线"。
    本条是**同一个修法**的第二次应用 —— 基线该问"被验对象说什么"，
    不该由验的人**提前写死**。

    这里读 `EXPECTED_CHECKS = <int>`（顶层赋值），并**要求它只出现一次**：
    出现两处说明有人在文件里留了第二份真相，那正是本函数要防的形态。
    """
    src = (REPO_ROOT / TOOL_REL).read_text(encoding="utf-8")
    values = re.findall(r"^EXPECTED_CHECKS\s*=\s*(\d+)\s*$", src, re.M)
    if len(values) != 1:
        raise SystemExit(
            f"派生失败：{TOOL_REL} 里顶层 `EXPECTED_CHECKS = <int>` 出现 {len(values)} 次"
            "（期望恰好 1 次）—— 参照物本身不唯一，基线无法派生"
        )
    return int(values[0])


def _baseline_lines(expected: int) -> str:
    """收集器基线的**唯一**判据行（由派生值拼出，不写死）。"""
    return f"{expected} PASS / 0 FAIL / {expected} checks"


def _run_rel(
    repo: Path, tool_rel: str, extra_args: list[str] | None = None, timeout: int = 300
) -> tuple[int, str]:
    """在副本里跑任意工具脚本（用于分别跑收集器与门禁）。"""
    env = {**os.environ, "NO_PROXY": "*", "no_proxy": "*"}
    try:
        proc = subprocess.run(
            [PYTHON, str(repo / tool_rel), *(extra_args or [])],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"[TIMEOUT] {tool_rel} 超过 {timeout}s 未返回"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# --------------------------------------------------------------------------- #
# 变异注入器：每条对应一个**真实失效面**（不是随手删字符）
# --------------------------------------------------------------------------- #
IMMUT_ANCHOR = 'IMMUTABLE_TABLES: tuple[str, ...] = ("pricing_versions", "usage_ledger")'
IMMUT_DEGRADED = 'IMMUTABLE_TABLES: tuple[str, ...] = ("pricing_versions",)'
PUI_ANCHOR = '"idempotency_key IS NOT NULL",'
PUI_DEGRADED = 'None,  # 退化注入：WHERE 写丢 ⇒ 索引静默退化成全表唯一'

#: C31 的注入块。**必须是真实代码**（不是 docstring）—— 这正是旧口径失效的形态，
#: 若把它写进 docstring 就变成"旧口径本来就该忽略的东西"，验证不到点子上。
C31_INJECT = (
    "\n\n# ---- 退化注入（反向验证用，非真实代码）----\n"
    "def _wp25_reverse_probe(fixture, inbound=0):\n"
    "    assert inbound == 0\n"
    "    return fixture\n"
)


def _patch(path: Path, old: str, new: str) -> None:
    """文本级注入（锚点必须**唯一**）。

    ⚠️ **`count` 必须是 1**，否则拒绝注入。加这道断言是本包踩出来的：
    退出码锚点 `raise SystemExit(0 if _verdict else 1)` 在收集器里出现 **2 次** ——
    L922 是 `emit(...)` 里**用引号引起来的说明文字**，L1016 才是真代码。
    原先 `replace(old, new, 1)` 只会换掉**第一处**，于是变异注到了引文上、
    真退出码原封不动 ⇒ 脚本仍然返回 0 ⇒ 用例断言 `code == 1` 永不可满足。

    这与 C31（docstring 里的 `inbound == 0`）**是同一个坑**：
    **"作者在文字里提到了这个模式" ≠ "代码里用了这个模式"**。
    凡锚点可能出现在说明文字里的，一律改用 `_patch_stmt()`（AST 语句定位）。
    """
    txt = path.read_text(encoding="utf-8")
    n = txt.count(old)
    if n == 0:
        raise SystemExit(f"注入失败：{path.name} 中找不到锚点 {old[:70]!r}")
    if n > 1:
        raise SystemExit(
            f"注入锚点不唯一：{path.name} 中出现 {n} 次 {old[:70]!r}"
            " —— 文本注入会注错位置（很可能注到说明文字而不是代码）。"
            "请改用 `_patch_stmt()`（AST 语句级定位）。"
        )
    path.write_text(txt.replace(old, new, 1), encoding="utf-8")


def _patch_stmt(path: Path, stmt_src: str, new: str, *, expect: int = 1) -> None:
    """**语句级**注入：只认 `ast` 语句节点，说明文字里的同名串永远不匹配。

    这是 `_patch()` 在"锚点会出现在字符串字面量里"时的替代方案，
    也是 C33 那条元判据（同样必须走 AST）在**注入侧**的对称做法：
    既然"读退出码"必须走 AST，那"删退出码"也必须走 AST —— 否则两侧口径不一致，
    注入会打在引文上、判据却读真代码，用例变成**永假的断言**。

    实现：`ast.parse` → 逐语句节点按行区间取原文 → `textwrap.dedent` 归一 →
    与 `stmt_src` 比较。命中数必须是 `expect`。
    """
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    target = textwrap.dedent(stmt_src).strip()

    hits: list[tuple[int, int]] = []  # (lineno, end_lineno)，1-based 闭区间
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.stmt):
            continue
        end = getattr(node, "end_lineno", None)
        if end is None or not hasattr(node, "lineno"):
            continue
        seg = textwrap.dedent("".join(lines[node.lineno - 1 : end])).strip()
        if seg == target:
            hits.append((node.lineno, end))

    if len(hits) != expect:
        raise SystemExit(
            f"注入失败：{path.name} 中匹配语句 {target[:70]!r} 的 "
            f"AST 节点数 = {len(hits)}，期望 {expect}"
        )
    # 自底向上替换，避免行号位移。
    for lineno, end in sorted(hits, reverse=True):
        indent = lines[lineno - 1][: len(lines[lineno - 1]) - len(lines[lineno - 1].lstrip())]
        body = "".join(
            (indent + ln if ln.strip() else ln) for ln in (new + "\n").splitlines(keepends=True)
        )
        lines[lineno - 1 : end] = [body]
    path.write_text("".join(lines), encoding="utf-8")


def degrade_c17(repo: Path) -> None:
    """退化①：`models.py` 少写一张不可变表 ⇒ `usage_ledger` 的触发器漏挂。"""
    _patch(repo / MUTABLE_RELS["models"], IMMUT_ANCHOR, IMMUT_DEGRADED)


def degrade_c18(repo: Path) -> None:
    """退化②：部分唯一索引的 `WHERE` 写丢 ⇒ 索引静默退化成全表唯一。"""
    _patch(repo / MUTABLE_RELS["ddl"], PUI_ANCHOR, PUI_DEGRADED)


def degrade_c31(repo: Path) -> None:
    """退化③：照抄 WP24 的 `inbound == 0` 口径（**真实断言**，不进 docstring）。"""
    p = repo / MUTABLE_RELS["tests"]
    p.write_text(p.read_text(encoding="utf-8") + C31_INJECT, encoding="utf-8")


def degrade_c32(repo: Path) -> None:
    """退化④：本包迁移的 `down_revision` 摘成 `None` ⇒ 双 head（alembic 直接崩）。"""
    mig = next((repo / "migrations" / "versions").glob(_MIG_GLOB))
    txt = mig.read_text(encoding="utf-8")
    new = re.sub(
        r"(down_revision[^=]*=\s*)['\"]022ba9804d66['\"]", r"\1None", txt, count=1
    )
    if new == txt:
        raise SystemExit("注入失败：找不到 wp25 迁移的 down_revision 赋值")
    mig.write_text(new, encoding="utf-8")


def degrade_c32_fakehead(repo: Path) -> None:
    """退化④b（**专门验证拓扑口径**）：造一个"长度更大但与本包无血缘"的 head。

    这是对 C32 新口径的**元验证**：若判据用的是"链上位置近似"
    （如 `_revision_is_at_least()` 那种长度/字典序比较，ISS-115 的恒真式），
    本用例会**假绿** —— 因为新 head 的 revision 更长、看起来"更新"。
    而拓扑判据（祖先链可达）会当场报红。

    做法：把本包迁移的 `down_revision` 改成 `None`，**同时**注入一个
    名字更长的孤立 head（模拟"另一个分支的迁移"）。
    """
    degrade_c32(repo)
    orphan = repo / "migrations" / "versions" / "20261001_0000_ffffffffffff_orphan.py"
    orphan.write_text(
        '"""反向验证用孤立迁移（非真实代码）：与本包无血缘的第二个 head。"""\n'
        "from __future__ import annotations\n"
        "\n"
        'revision: str = "ffffffffffff"\n'
        "down_revision: str | None = None\n"
        "branch_labels = None\n"
        "depends_on = None\n"
        "\n"
        "\n"
        "def upgrade() -> None:\n"
        '    """空。"""\n'
        "\n"
        "\n"
        "def downgrade() -> None:\n"
        '    """空。"""\n',
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="WP25 CR 判据口径的反向验证")
    parser.parse_args()

    checks: list[tuple[str, bool, str]] = []
    lines: list[str] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    def emit(text: str = "") -> None:
        lines.append(text)

    emit("# WP25 CR 收集器判据口径修正 + CR-2 退出码缺陷 —— 反向验证")
    emit("# 被测对象: " + TOOL_REL)
    emit("# 第二被测对象: " + GATE_REL + "（门禁本身，防「只加不改」= ISS-95）")
    emit("# 结论: 4 条口径修正 + 退出码防线 + 门禁，各自在退化形态下全部报红（灵敏），")
    emit("#        恢复后回绿（可复现），真树未被触碰")
    emit()

    dirty_at_start = _dirty_at_start()
    before = _snapshot(REPO_ROOT)
    emit(f"## [0] 取证范围 {len(before)} 个文件（{', '.join(TREE_SCOPE)}）")
    emit(f"  · 窗口开始前已脏的路径 {len(dirty_at_start)} 个（属并行会话，不计入本判据）")
    emit()

    #: ★ 基线参照物**从被验脚本自己的源码派生**（铁律 14 / ISS-171）。
    #: 此前这里写死 `"36 PASS / 0 FAIL / 36 checks"`，收集器涨到 40 后
    #: 本判据恒 FAIL —— 而文件里只会显示一行"条数行未命中"，看起来像
    #: "收集器坏了"，实际是"尺子没跟着涨"。
    expected = _expected_checks_from_tool()
    baseline_line = _baseline_lines(expected)

    repo = _make_copy("a")
    try:
        # ---------------------------------------------------------- 基线
        emit(f"## [1] 基线：未变异时必须全绿（期望 {expected} PASS / 0 FAIL / {expected} checks）")
        emit("  · 参照值派生自被测脚本自身的 `EXPECTED_CHECKS`，非本脚本写死（ISS-171）")
        code, out = _run(repo)
        baseline_ok = (
            code == 0
            and out.count("CHECK FAIL") == 0
            and baseline_line in out
        )
        add("baseline-all-pass-0-fail", baseline_ok, f"exit={code}")
        emit(f"  {'OK ' if baseline_ok else 'BAD'}  exit={code}，"
             f"CHECK FAIL={out.count('CHECK FAIL')}，"
             f"{baseline_line if baseline_line in out else '条数行未命中（期望 ' + baseline_line + '）'}")
        emit("  ⚠️ 基线必须**先**跑：若基线就红，后面所有变异都无意义")
        emit()

        # ------------------------------------------- 逐条变异（各自独立副本）
        cases: list[tuple[str, str, str, str, str]] = [
            (
                "C17-models-drops-immutable-table",
                "C17",
                "models.py 少写一张不可变表（usage_ledger 触发器漏挂）",
                "[C17] IMMUTABLE_TABLES",
                degrade_c17,
            ),
            (
                "C18-pui-where-dropped",
                "C18",
                "部分唯一索引 WHERE 写丢（静默退化成全表唯一）",
                "[C18] 部分唯一索引 spec 逐字段",
                degrade_c18,
            ),
            (
                "C31-inbound-zero-copied",
                "C31",
                "照抄 WP24 的 inbound == 0（真实代码断言，非 docstring）",
                "[C31] 夹具口径",
                degrade_c31,
            ),
            (
                "C32-down-revision-none",
                "C32",
                "本包迁移 down_revision 摘成 None（双 head）",
                "[C32] 迁移链单一 head",
                degrade_c32,
            ),
            (
                "C32-orphan-head-no-lineage",
                "C32",
                "孤立更长的 head（专验拓扑口径：长度近似判据会假绿）",
                "[C32] 迁移链单一 head",
                degrade_c32_fakehead,
            ),
        ]

        for idx, (name, crit, label, marker, fn) in enumerate(cases, start=1):
            sub = _make_copy(f"c{idx}")
            try:
                fn(sub)
                code, out = _run(sub)
                # ⚠️ 断言**具体的 CHECK FAIL 行**，不只 rc≠0（ISS-143：
                # "没跑"与"跑失败"同形；脚本崩溃也会 rc≠0）。
                fail_lines = [
                    ln.strip() for ln in out.splitlines()
                    if ln.startswith("CHECK FAIL") and marker in ln
                ]
                hit = bool(fail_lines)
                ok = code == 1 and hit
                add(name, ok, f"exit={code}，{'命中' if hit else '未命中'} {marker!r}")
                emit(f"## [{idx + 1}] {crit} 退化 → {'变红 ✓' if ok else 'BAD ✗'}")
                emit(f"  变异：{label}")
                emit(f"  exit={code}（期望 1）")
                for fl in fail_lines:
                    emit(f"  {fl[:200]}")
                if not fail_lines:
                    emit(f"  **未命中预期 CHECK FAIL 行 {marker!r}**")
                    for ln in out.splitlines():
                        if ln.startswith("CHECK FAIL"):
                            emit(f"      实际: {ln[:200]}")
                emit()
            finally:
                shutil.rmtree(sub.parent, ignore_errors=True)

        # ------------------------------------ CR-2 TOOLING-1：退出码 / 门禁
        #
        # ⚠️ 这一组是**本包最危险那条缺陷**的验证。前 5 条变异只证明"判据会报红"，
        # 这一组证明"**报红之后有机械后果**"——
        # 而修复前的事实是：收集器恒返回 0，报红的 4 条对下游完全不可见。
        #
        # ⚠️⚠️ **本组的第一版自己踩了两个坑** —— 两个都不是"没测"，而是"测了但测错了"：
        #
        #   坑 A（注入侧）：锚点 `raise SystemExit(0 if _verdict else 1)` 在收集器里
        #       出现 **2 次** —— 一次在 `emit(...)` 的**说明文字**（字符串字面量）里，
        #       一次才是真代码。原先 `replace(old, new, 1)` 只换第一处 ⇒ 变异注到了
        #       **引文**上，真退出码原封不动 ⇒ 脚本仍返回 0 ⇒ `code == 1` 永不可满足。
        #       ⇒ 改用 `_patch_stmt()`（AST 语句定位）。**与 C31 是同一个坑**：
        #          "作者在文字里提到这个模式" ≠ "代码里用了这个模式"。
        #
        #   坑 B（断言侧）：`C33-no-exit-code-detected` 断言 `code == 1`，
        #       但**删掉退出码之后脚本必然返回 0** —— 这是**逻辑自反**的断言，
        #       无论实现怎么写都不可满足；而 `C33-bare-exit-zero-detected` 拿去匹配的
        #       marker `退出码恒为 0` 是**门禁**的措辞，却对着**收集器**的输出去找，
        #       同样永不命中。⇒ 断言被拆成两层，各管各的。
        #
        # 现在的四层分工（"谁的红"分清楚，不把两种机制混成一条 = 本项目错误 ⑫）：
        #
        #   ① 收集器自检 C33 能看见「退出码缺失 / 裸 0」    → 断言 CHECK FAIL 指名
        #   ② 收集器在此形态下 exit=0                      → **这才是缺陷本体**（红不可见）
        #   ③ 中央门禁把缺失退出码判红（rc=1 + 指名文件）   → 断言非零 + marker
        #   ④ 中央门禁不因"有 SystemExit 调用"就放行裸 0    → 断言非零 + marker
        #
        # ① 与 ② 必须**同时**成立：只断言 ① 会漏掉"红不可见"这个要害；
        # 只断言 ② 则任何崩溃都能满足（ISS-143：「没跑」与「跑失败」同形）。
        emit("## [7] CR-2 TOOLING-1：退出码真的会红（此前恒 0，报红无机械后果）")

        C33_MARKER = "[C33/CR-2 TOOLING-1] 累积 `checks` 的脚本必须有显式退出码"

        def _fail_lines(out: str, marker: str) -> list[str]:
            return [
                ln for ln in out.splitlines()
                if ln.startswith("CHECK FAIL") and marker in ln
            ]

        EXIT_STMT = "raise SystemExit(0 if _verdict else 1)"

        # 7a：删掉收集器的退出码 ⇒ ① 自检 C33 报红 ② 脚本 exit=0（红对下游不可见）
        sub = _make_copy("x1")
        try:
            _patch_stmt(
                sub / TOOL_REL,
                EXIT_STMT,
                "pass  # 退化注入：删掉退出码（= 修复前的形态）",
            )
            code, out = _run(sub)
            hit = bool(_fail_lines(out, C33_MARKER))
            # 断言 `code == 0` **不是退让，它就是本条要证明的缺陷形态** ——
            # 判据报红而退出码为 0，故下游（rerun_all_evidence 只判 rc==0）看不见。
            ok = hit and code == 0
            add("C33-detects-missing-exit-code", ok, f"exit={code}，{'命中' if hit else '未命中'}")
            emit(f"  {'OK ' if ok else 'BAD'}  删掉收集器的 SystemExit ⇒ exit={code}，"
                 f"自检 C33 {'命中' if hit else '未命中'}")
            for ln in _fail_lines(out, C33_MARKER):
                emit(f"  {ln.strip()[:200]}")
            emit("  ⇒ ① 判据**看得见**（C33 报红并指名）② 但 exit=0 ⇒ **下游看不见**")
            emit("     两句必须同时成立，才是 ISS-168 的完整形态（红且无机械后果）")
            emit()
        finally:
            shutil.rmtree(sub.parent, ignore_errors=True)

        # 7b：换成裸 `SystemExit(0)` ⇒ 自检 C33 也必须报红（与"没有退出码"同判）
        sub = _make_copy("x2")
        try:
            _patch_stmt(
                sub / TOOL_REL,
                EXIT_STMT,
                "raise SystemExit(0)  # 退化注入：恒 0",
            )
            code, out = _run(sub)
            hit = bool(_fail_lines(out, C33_MARKER))
            ok = hit and code == 0
            add("C33-detects-bare-exit-zero", ok, f"exit={code}，{'命中' if hit else '未命中'}")
            emit(f"  {'OK ' if ok else 'BAD'}  换成裸 SystemExit(0) ⇒ exit={code}，"
                 f"自检 C33 {'命中' if hit else '未命中'}")
            for ln in _fail_lines(out, C33_MARKER):
                emit(f"  {ln.strip()[:200]}")
            emit("  ⇒ 证明『裸 0』与『没有退出码』被判成同一件事（按行为定，不按写法定）")
            emit()
        finally:
            shutil.rmtree(sub.parent, ignore_errors=True)

        # 7c：门禁的**元验证** —— 把真退出码删掉，直接跑 `check_evidence_scripts.py`，
        #     断言它报红且**指名这个文件**。这一步是"门禁真的接上了"的证据
        #     （不是只加了函数、没进 main 的调用列表）。
        sub = _make_copy("x3")
        try:
            _patch_stmt(
                sub / TOOL_REL,
                EXIT_STMT,
                "pass  # 退化注入：删掉退出码，看门禁抓不抓",
            )
            code, out = _run_rel(sub, GATE_REL)
            marker = "累积 `checks` 却没有显式退出码"
            hit = marker in out and "FAIL collect_wp25_code_review.py" in out
            ok = code == 1 and hit
            add("gate-detects-missing-exit-code", ok, f"exit={code}，{'命中' if hit else '未命中'}")
            emit(f"  {'OK ' if ok else 'BAD'}  门禁 check_evidence_scripts.py ⇒ exit={code}（期望 1）")
            for ln in out.splitlines():
                if "FAIL collect_wp25" in ln or marker in ln:
                    emit(f"  {ln.strip()[:200]}")
            emit("  ⇒ 门禁**接上了**（不是只加了函数没进 main 的调用列表）")
            emit()
        finally:
            shutil.rmtree(sub.parent, ignore_errors=True)

        # 7d：门禁不因"文件里出现了 SystemExit 调用"就放行**裸 0**。
        #     这条同时是**反假绿**：一个"只检查有没有 SystemExit"的浅门禁会在这里假绿 ——
        #     而那正是本检查第一版的真实形态（用引文当退出码 ⇒ 永远命中 ⇒ 恒绿）。
        sub = _make_copy("x4")
        try:
            _patch_stmt(
                sub / TOOL_REL,
                EXIT_STMT,
                "raise SystemExit(0)  # 退化注入：恒 0",
            )
            code, out = _run_rel(sub, GATE_REL)
            marker = "退出码恒为 `0`"
            hit = marker in out and "FAIL collect_wp25_code_review.py" in out
            ok = code == 1 and hit
            add("gate-rejects-bare-exit-zero", ok, f"exit={code}，{'命中' if hit else '未命中'}")
            emit(f"  {'OK ' if ok else 'BAD'}  门禁面对裸 SystemExit(0) ⇒ exit={code}（期望 1）")
            for ln in out.splitlines():
                if "FAIL collect_wp25" in ln or marker in ln:
                    emit(f"  {ln.strip()[:200]}")
            emit("  ⇒ 有 `SystemExit` **调用** ≠ 有可区分的退出码；门禁按**行为**判，不按**写法**判")
            emit("     若门禁退回文本正则（引文也算命中），本条会当场假绿")
            emit()
        finally:
            shutil.rmtree(sub.parent, ignore_errors=True)

        # 7e：**证明"改用 AST"这个决定是必要的** —— 把门禁口径退回第一版的文本正则，
        #     重放 7c 的形态，断言门禁**假绿（rc=0）**。
        #
        # ⚠️ 这一条是纪律 7 的落地："改判据也必须做反向验证，否则只是把假红换成假绿"。
        # 没有它，7c/7d 只能证明"现在的门禁能报红"，**不能证明**"AST 化是必需的"——
        # 万一正则在真代码被删后恰好也不命中，那 AST 就是多余的装饰，
        # 而我会在文档里写下一句无法当场指认的"我们用了 AST 更稳"（ISS-95）。
        sub = _make_copy("x5")
        try:
            _patch_stmt(
                sub / GATE_REL,
                "exit_args = _ast_exit_args(text)",
                'exit_args = re.findall(r"(?:raise\\s+SystemExit|sys\\.exit)'
                '\\s*\\(\\s*([^)]*)\\)", _strip_comments(text))',
            )
            _patch_stmt(
                sub / TOOL_REL,
                EXIT_STMT,
                "pass  # 退化注入：删掉退出码（重放 7c 的形态）",
            )
            code, out = _run_rel(sub, GATE_REL)
            marker = "累积 `checks` 却没有显式退出码"
            regressed = code == 0 and "FAIL collect_wp25_code_review.py" not in out
            add(
                "gate-regex-caliber-would-be-blind",
                regressed,
                f"退回正则后 exit={code}（期望 0 = 假绿复现）",
            )
            emit("  OK   门禁退回**文本正则**后重放 7c ⇒ "
                 f"exit={code}，{'未报红（假绿复现 ✓）' if regressed else '仍然报红（本条失去意义）'}")
            emit(f"  ⇒ 正则把**引文**当成了退出码，于是真代码被删也不报 —— 报红数 = "
                 f"{out.count('CHECK FAIL')}")
            emit("     故 **AST 化不是装饰**：它是 7c/7d 能成立的前提")
            emit()
        finally:
            shutil.rmtree(sub.parent, ignore_errors=True)

        # 7f：反向脚本**自己的**注入器也必须有一条防线被验证（ISS-95 递归）。
        #     `_patch()` 新增了"锚点必须唯一"的断言 —— 它的存在理由就是坑 A。
        #     若这条断言本身是装饰（比如写成了 count >= 1），坑 A 会原样复活。
        tmp_dir = Path(tempfile.mkdtemp(prefix="wp25cr-dup-"))
        try:
            dup = tmp_dir / "dup.py"
            dup.write_text('x = "DUP 锚点"\n# DUP 锚点\n', encoding="utf-8")
            rejected = False
            msg = ""
            try:
                _patch(dup, "DUP 锚点", "变异")
            except SystemExit as exc:
                rejected = "注入锚点不唯一" in str(exc)
                msg = str(exc)
            add("patch-rejects-ambiguous-anchor", rejected, "重复锚点被拒" if rejected else "未拒绝")
            emit(f"  {'OK ' if rejected else 'BAD'}  用重复锚点调 `_patch()` ⇒ "
                 f"{'被拒绝 ✓' if rejected else '未拒绝 ✗'}")
            emit(f"     消息：{msg[:150]}")
            emit("  ⇒ 坑 A（变异注到引文上）不会复发；注入器的防线**被验证过**，不是声称有")
            emit()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        # ------------------------------------------------- 恢复（重跑基线）
        emit("## [11] 恢复：重新取一份干净副本，4 条口径与退出码必须回到全绿（可复现性）")
        again = _make_copy("z")
        try:
            code, out = _run(again)
            restored = (
                code == 0
                and out.count("CHECK FAIL") == 0
                and baseline_line in out
            )
            add("restored-green", restored, f"exit={code}")
            emit(f"  {'OK ' if restored else 'BAD'}  干净副本 → exit={code}，"
                 f"CHECK FAIL={out.count('CHECK FAIL')}")
            emit("  ⚠️ 这一步证明上面 9 次报红**确实是变异造成的**，不是随机/环境噪声")
            emit("     9 次 = 5 条口径变异（C17/C18/C31/C32×2）+ 收集器自检 2 次（7a/7b）"
                 "+ 门禁 2 次（7c/7d）")
            emit()
        finally:
            shutil.rmtree(again.parent, ignore_errors=True)
    finally:
        shutil.rmtree(repo.parent, ignore_errors=True)

    # ---------------------------------------------------------- 真树未触碰
    emit("## [8] 真树未被触碰（整树哈希，不是自证清白的那几个文件）")
    after = _snapshot(REPO_ROOT)
    changed = sorted(rel for rel in before if before.get(rel) != after.get(rel))
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))

    def _pre_existing(rel: str) -> bool:
        if rel in dirty_at_start:
            return True
        return any(
            rel == d or rel.startswith(d.rstrip("/") + "/") for d in dirty_at_start
        )

    foreign = sorted(rel for rel in (changed + added + removed) if _pre_existing(rel))
    own = [rel for rel in (changed + added + removed) if rel not in foreign]
    same = not own
    add("real-tree-untouched", same, f"{len(before)} 个文件哈希一致")
    emit(f"  {'OK ' if same else 'BAD'}  真树 {len(before)} 个文件哈希前后一致："
         f"{'是' if same else '否'}")
    for rel in own:
        emit(f"        | 被动过：{rel}")
    if foreign:
        emit(f"  —— 另有 {len(foreign)} 个路径在窗口内变动，但**开始前就已脏**，"
             f"属并行会话（不计入本判据）：")
        for rel in foreign[:10]:
            emit(f"        · {rel}")
    emit()

    # ---------------------------------------------------------- 结论
    failures = [name for name, ok, _ in checks if not ok]
    EXPECTED_CHECKS = 14
    complete = len(checks) >= EXPECTED_CHECKS
    exact = len(checks) == EXPECTED_CHECKS
    emit("## [9] 结论")
    for name, ok, detail in checks:
        suffix = f"（{detail}）" if detail else ""
        emit(f"  {'PASS' if ok else 'FAIL'} {name}{suffix}")
    emit()
    verdict = not failures and complete and exact
    summary = (
        f"RESULT={'PASS' if verdict else 'FAIL'} "
        f"checks={len(checks)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条，"
        f"条数核对 {'一致' if exact else '不一致'}）"
    )
    emit(summary)

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写出 {OUT_LOG.relative_to(REPO_ROOT)}")
    print(summary)
    for name in failures:
        print(f"  FAIL {name}")
    if not exact:
        print(f"  FAIL 条数核对：实际 {len(checks)} 条 ≠ 期望 {EXPECTED_CHECKS} 条")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())