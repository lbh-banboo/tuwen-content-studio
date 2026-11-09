#!/usr/bin/env python
"""WP-DEBT-02：**测试库可信度三条判据的反向验证**（把错误实现放回去，判据必须报红）。

产出：`evidence/m3/test-db-credibility-reverse.log`

## 为什么需要这个文件

三条判据此刻都打印 `[PASS]`，但那**只证明"当前工作区看起来合规"**，
不能证明"它被违反时会报红"。这两件事的差别就是本项目判据纪律第 9 条：

> **「声称自己有防线」比「没有防线」更危险。**

一个恒真判据（ISS-115）、一个覆盖范围被悄悄收窄的判据（ISS-139/143）、
一个只有警告没有报红的判据 —— 三者都**打印 `[PASS]`**。
本脚本把"它会报红"从**声明**变成**可复跑的证据**。

## 三条被测判据

| 判据 | 文件 | 抓什么 | 需要真库 |
|---|---|---|---|
| A 默认 DSN 目标 | `tools/quality/check_test_database_target.py` | 默认测试 DSN 指向 `postgres` 等保留库 | 否（纯静态） |
| B 测试残留 | `tools/quality/check_test_residue.py` | 跑测试前后库里多出持久对象 | 是（**跑 pytest，约 2 分钟**） |
| C 迁移↔模型对账 | `tools/quality/check_migration_model_parity.py` | 两条建表路径产物不一致 | 是（建两个临时库） |

## 变异清单（每条对应一个**真实失效面**）

⚠️ 下表**逐条与 `main()` 里的 `run_case(...)` 一一对应**。
2026-09-30 自纠：本表初版有 4 条与实现不符（②③④⑥写的不是实际做法），
"声称有防线却指不出是哪条" —— 判据纪律第 9 条的反面。
已加机械判据：`collect_test_db_credibility_code_review.py` 的
「反向验证的变异表条数 = `run_case` 调用数」。

| # | 变异（§编号） | 期望报红 | 它证明的失效面 |
|---|---|---|---|
| ① | §1 `test_devtools.py` 默认 DSN 改回 `postgres` | `[FAIL]` + 文件名 + `postgres` | 判据 A 的基本盘（**红的形状**，不只 rc≠0） |
| ② | §2 **新增**一个指向系统库的测试文件 | `[FAIL]` + 该文件名 | 判据扫**磁盘现状**，不是写死的文件名单 |
| ③ | §3 `TESTS_DIR.rglob` → `glob` **并造一个子目录** | `覆盖范围对账失败` | 铁律 14 第 5 次复发形态（`os.walk` 独立对账抓遍历机制） |
| ④ | §5 把 pytest 参数弄坏（断言**报红**） | `[FAIL]` + 一条都没跑 | **没跑 vs 跑失败同形**（ISS-143） |
| ⑤ | §5 在④基础上**摘掉两道**「测试真跑过」防线（断言**假绿**） | `[PASS]` 且无 `[FAIL]` | 成对证明那两道防线是**必要**的（ISS-143 的另一半） |
| ⑥ | §6 删掉 WP21 夹具的索引清理段 | `[FAIL]` | 判据 B 能抓到**真**残留（不是恒真） |
| ⑦ | §8 模型侧加一列（断言**报红**） | `只在**模型**里有` | 判据 C 真在比「列」 |
| ⑧ | §8 在⑦基础上删掉「列」比较那一行（断言**假绿**） | `[PASS]` 且无 `[FAIL]` | 成对证明抓它的是列比较，不是别的巧合 |
| ⑨ | §9 排除集换成不存在的表名 | `排除集自检` | 排除集不许吃掉真差异 |

> ⚠️ **③ 与 ⑧ 的 marker 曾是错的**（2026-09-30 完整版实测发现）：原先写
> `expect_marker="RESULT=PASS"`，但**判据 B/C 从不输出 `RESULT=` 行**
> （只有判据 A/B/C 之外的那类"逐条 CHECK"脚本才印 `RESULT=`）。
> 于是这两个"期望假绿"的用例断言了一个**永远不存在**的字符串 ⇒
> **永远报 BAD**。反向验证自己踩了它要防的病：断言了不存在于输出的标记，
> 且"永远红"与"真的没变绿"同形（ISS-143 的镜像）。现已改为断言
> 判据**实际**印出的绿形态（`[PASS]` 且**不得**含 `[FAIL]`）——
> 后者也补上了"绿的形状"这一维（ISS-140 只要求校验红的形状）。
>
> ⚠️ **⑤ 的前提也曾是错的**：原变异只摘掉 `collected == 0` 一道守卫，
> 而 `main()` 还有第二道 `proc.returncode != 0` —— 两道互相独立，
> 只摘一道**必然仍报红**（实测：`[FAIL] pytest 自身未通过`）。
> "摘掉一道防线"不等于"防线不存在"，因此那个用例不可能满足它自己的期望。
> 现改为**同时**摘掉两道，才真正回到"没有这道防线的世界"。

> ⚠️ **§11「真树未被触碰」的失败文案也曾误诊**（2026-09-30 完整版实测）：
> 原文案把所有哈希不一致都写成"这是本工具最严重的失败"，而本工具**只写副本与
> 本日志**（都在取证范围之外）⇒ "本工具改了真树"**结构上不可能**。实测那次
> 漂移来自**并行的另一个会话在取证窗口内编辑了仓库**。两个完全不同成因印出
> 同一条断言 ⇒ 读的人被断言本身带偏，去修一个不存在的缺陷（与 ISS-143 同族）。
> 现改为显式列出两个成因并给出可执行处置（冻结编辑后重跑）。
>
> ⚠️ **但只改文案不够 —— 判据本身要改**（ISS-154，同日第二轮）：
> §11 原先把「跨时刻的整树哈希」当成**不变量**，而本仓**并行开发是常态**
> （实测另一会话正在写 `services/backend/src/rednote/modules/billing/`）——
> 取证窗口 10~13 分钟里任何人改一次文件，完整版就**必然**红。
> "必定红"与"真的坏了"同形（ISS-143）：**永远红的判据等于没有判据**，
> 它只会训练人忽略这条输出。这与本包刚修的 ISS-148（J2 计数 TOCTOU）是同一族。
> 修法：`_concurrently_modified_paths()` 在窗口**开始前**冻结「本就不是 HEAD
> 状态」的路径集（`git status --porcelain` 现场决定，不写名单），窗口内的变动
> 落在其中即**隔离**并在输出里单独列出。
> ⚠️ 这是**隔离**不是**豁免**：本脚本自己的写盘范围另由 `own-writes-disjoint`
> 用**结构**证明不在 `TREE_SCOPE` 内。只有两者合起来才够说"本工具没写坏真树"。

`--fast` 只跑 ①②③（判据 A，**纯静态**）；④~⑨ 需建临时库/跑 pytest，
留给完整版。**CI 门 3 用 `--fast`**（只验判据 A 的活性）；
完整版在本机收口时跑一次，证据同提交入库。

## 做法（照 `collect_dependency_declaration_reverse_evidence.py` 的模式）

整仓复制到 `/tmp/rdbtdc/repo`（**副本**，真树只读），在副本上做变异、跑判据、
断言报红，最后断言副本还原后回绿、真树**整树**哈希未变。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SELF_PATH = Path(__file__).resolve()
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"
OUT_FILE = EVIDENCE_DIR / "test-db-credibility-reverse.log"

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

TOOL_A = "tools/quality/check_test_database_target.py"
TOOL_B = "tools/quality/check_test_residue.py"
TOOL_C = "tools/quality/check_migration_model_parity.py"

#: 「真树未被触碰」的取证范围：**整树**。只哈希几个文件是**自证清白**（见
#: `collect_dependency_declaration_reverse_evidence.TREE_SCOPE` 的同款说明）。
TREE_SCOPE = ("services/backend", "migrations", "tools")

#: 本脚本**唯一**会写盘的两处（相对仓库根，或绝对路径下的副本）。
#:
#: **为什么要把这件事写成数据而不是注释**（ISS-152）：「真树未被触碰」失败时，
#: 有两个互斥成因 —— 本工具写坏了真树 / 外部进程在取证窗口内写了盘。
#: 原文案把前者写成默认解释，而本脚本只在 `/tmp/rdbtdc-*/repo` 与下面这个日志
#: 里写盘 ⇒ 前者由**结构**保证不可能。把这个"不可能"变成一条可执行判据
#: （`_own_writes_are_disjoint`），失败文案才有资格说"默认是外部编辑"，
#: 而不是靠作者自觉。
_OWN_WRITE_PATHS = (str(OUT_FILE.relative_to(REPO_ROOT)),)


def _own_writes_are_disjoint() -> tuple[bool, str]:
    """本脚本的写盘范围必须与取证范围 `TREE_SCOPE` **不相交**。

    相交的话「真树未被触碰」这条判据就永远无法自证 —— 因为工具本来就在写它，
    此时失败文案里"结构上不可能"那句话就是假的（正是 ISS-95：声称有防线）。
    副本目录（`/tmp/rdbtdc-*`）在仓库外，天然不相交，无需参与比较。
    """
    clashes = [
        rel for rel in _OWN_WRITE_PATHS
        if any(rel == scope or rel.startswith(scope + "/") for scope in TREE_SCOPE)
    ]
    if clashes:
        return False, f"本脚本会写 {clashes}，它落在取证范围 {TREE_SCOPE} 内"
    return True, "写盘范围与取证范围不相交（副本在 /tmp，日志在 evidence/）"

def _concurrently_modified_paths() -> set[str]:
    """取证窗口**开始之前**就已经处于 git 脏状态的路径（相对仓库根）。

    ⚠️ 为什么必须有这个集合（**ISS-154**）：本判据原先把「整树哈希不变」当成
    **不变量**，而它是**跨时刻可变量** —— 与 ISS-148（J2 计数判据的 TOCTOU）
    **同一族**。本仓并行开发是常态（实测另一会话正在写
    `services/backend/src/rednote/modules/billing/`），而取证窗口要 10~13 分钟，
    足够任何人改一次文件 ⇒ 完整版**必然**报 `real-tree-untouched` 红。
    而"必定红"与"真的坏了"同形（ISS-143）——**永远红的判据等于没有判据**。

    收集范围由 `git status --porcelain` **现场决定**，不由人写名单（铁律 14）。

    ⚠️ 它只做**隔离**，不做**豁免**：本脚本自己的写盘范围由
    `_own_writes_are_disjoint()` 用**结构**证明不在 `TREE_SCOPE` 内
    （副本在 /tmp、日志在 evidence/），两者合起来才够说「本工具没写坏真树」。
    单独一条"脏文件不算"会被一个真写坏真树的 bug 直接吃掉 —— 那个 bug 也会
    让文件变脏。**结构证明 + 时刻隔离**缺一不可。
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=all"],
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
        if " -> " in rel:  # 重命名：取新路径
            rel = rel.split(" -> ", 1)[1].strip().strip('"')
        out.add(rel)
    return out


_EXCLUDE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


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


def _make_copy(tag: str) -> Path:
    """把整仓复制到一个**唯一**临时目录，返回副本根。

    ⚠️ 为什么必须**唯一**（**ISS-156**）：原实现用写死的常量目录
    `/tmp/rdbtdc-{tag}`（tag ∈ {"fast","full"}），于是**两次并发的同模式运行
    共用同一个目录**：后启动者的 `shutil.rmtree(base)` 会把先启动者正在用的副本
    删掉，或 `copytree` 把它换成"另一个中间态"。实测后果：一次完整版运行在
    `C①-a` 写 `projects/models.py` 时抛 `FileNotFoundError`（副本已消失），
    `rc=1`、13 分半白跑；更隐蔽的是副本可能**停在别人的中间态**，让判据 B/C 报
    出与本次变异无关的红。

    这与 ISS-154 是**同一根因的另一个面**：把"这个路径是本实例独占的"当**不变量**，
    而它是**跨实例可变量**。修法不是加重试，而是让目录名由**系统分配**（`mkdtemp`
    保证原子唯一）——覆盖范围（此处是"谁拥有这个目录"）**由机制决定，不由人写**（铁律 14）。
    """
    base = Path(tempfile.mkdtemp(prefix=f"rdbtdc-{tag}-"))
    dest = base / "repo"
    shutil.copytree(
        REPO_ROOT,
        dest,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc", "evidence"),
    )
    return dest


#: `_copy_is_alive` 用的探针（**相对仓库根**，不是相对 `tools/quality`）。
#: ⚠️ 写成常量是为了让它能被 `_probe_is_sound()` 在**真树**上验一遍 ——
#: 见 ISS-157：初版把 `TOOL_A`（已含 `tools/quality/` 前缀）又拼了一次
#: `repo/"tools"/"quality"/TOOL_A` ⇒ 探针路径恒不存在 ⇒ 判据**永远报红**。
_COPY_PROBES = (
    "services/backend/src/rednote/modules",
    "tools/quality/check_test_database_target.py",
)


def _probe_is_sound() -> bool:
    """在**真树**上验一遍：`_COPY_PROBES` 指向的东西确实存在。

    ⚠️ 为什么必须有（**ISS-157**，本包第二次被"恒真/恒假断言"咬）：一个用于
    "判断副本是否还在"的探针，如果它自己的路径是错的，就会**恒返回 False**
    ⇒ `run_case` 每条都报"副本已消失" ⇒ 判据**永远红**。而"永远红"与"真的
    坏了"在输出上同形（ISS-143）⇒ 这条判据退化成噪声。

    修法不是"我这次写对了"，而是**让探针自己也要被验**：真树里这些路径必然
    存在（否则仓库本身就坏了），所以拿真树做基准，探针必须为真 —— 不是则说明
    探针写错（而不是副本有问题），当场报红。这与 ISS-149「绿的形状也要验」
    是同一条纪律：**任何断言，包括"路径存在"这种最朴素的断言，都要有一个
    已知为真的基准去校它。**
    """
    return all((REPO_ROOT / rel).exists() for rel in _COPY_PROBES)


def _copy_is_alive(repo: Path) -> bool:
    """副本根**及其关键文件**仍在（并发实例不该能删掉它 —— 见 `_make_copy`/ISS-156）。

    这是**前置检查**，不是兜底：它把"副本在取证中途消失了"报成一条**可读的判据红**
    （指名缺失路径），而不是让 `run_case` 深处抛裸 `FileNotFoundError` 崩栈 ——
    崩栈会丢掉"这是一个环境/并发问题"的信息（ISS-143：崩了与跑失败同形）。
    """
    probes = (repo, *(repo / rel for rel in _COPY_PROBES))
    return all(p.exists() for p in probes)


#: 判据 B 真跑全量 pytest 实测 **2239s**（2330 passed，2026-10-16 复跑；
#: 2026-10-14 时为 1478s / 2170 passed —— 套件已增长 160 条）。
#: 原 900s 短于它的真实耗时，导致基线与 B① 类变异**因超时而非判据语义**报红（exit=124）。
#: 上限取 3600s（约 1.6 倍余量），仍是有界等待 —— 不是无上限的 `while True`（C5）。
#: ⚠️ 2026-10-16 再次触顶：2400s 对 2239s 只剩 1.07 倍余量，加上副本/启动开销
#:    ⇒ `B-baseline-green` exit=124、`restored-B-green` 连锁红（2/18 FAIL）。
#:    代价须知：判据 B 要跑两遍全量 pytest，单遍上限 3600s ⇒ 本脚本单轮最坏约 2 小时。
_RUN_TIMEOUT_S = 3600


def _run(repo: Path, tool_rel: str, timeout: int = _RUN_TIMEOUT_S) -> tuple[int, str]:
    """在副本里跑判据，返回 `(退出码, 合并输出)`。

    ⚠️ `cwd` 必须是**副本根**：判据内部用 `Path(__file__).parents[N]` 定位仓库，
    所以它会解析到副本。`NO_PROXY='*'` 不可省（本机代理劫持 localhost）。
    """
    env = {**os.environ, "NO_PROXY": "*", "no_proxy": "*"}
    # 让被测判据用**本机测试库**；CI 上由外部环境变量覆盖。
    env.pop("REDNOTE_DATABASE_URL", None)
    try:
        proc = subprocess.run(
            [PYTHON, str(repo / tool_rel)],
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


MODEL_ANCHOR = (
    "class Project(Base):\n"
    '    """项目。权限隔离的基本单位。"""\n'
    "\n"
    '    __tablename__ = "projects"\n'
)
MODEL_MUTATED = (
    "class Project(Base):\n"
    '    """项目。权限隔离的基本单位。"""\n'
    "\n"
    '    __tablename__ = "projects"\n'
    "\n"
    "    probe_extra_column: Mapped[str | None] = mapped_column(String(8), nullable=True)\n"
)


def _model_anchor_edits() -> list[tuple[str, str, str]]:
    """往 `projects/models.py` 的 `Project` 模型加一列（迁移侧不会有）。

    只在**变异副本**里做。这一列会让"模型路径"比"迁移路径"多一列 ⇒ 判据 C 必须报红。
    """
    return [
        (
            "services/backend/src/rednote/modules/projects/models.py",
            MODEL_ANCHOR,
            MODEL_MUTATED,
        )
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fast",
        action="store_true",
        help="只跑判据 A 的纯静态变异（跳过要建库/跑 pytest 的 B、C）",
    )
    args = parser.parse_args()

    checks: list[tuple[str, bool, str]] = []
    lines: list[str] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    def emit(text: str = "") -> None:
        lines.append(text)

    def run_case(
        *,
        repo: Path,
        tool_rel: str,
        edits: list[tuple[str, str, str]],
        expect_marker: str,
        name: str,
        label: str,
        expect_exit_nonzero: bool = True,
        extra_files: dict[str, str] | None = None,
        forbid_marker: str | None = None,
    ) -> None:
        """做一组变异 → 跑判据 → 断言报红（**指名预期字符串**）→ 还原。

        `expect_marker` 是**必须出现的字符串**。只断言 rc≠0 是不够的 ——
        脚本因任何原因崩溃也会 rc≠0（ISS-143：没跑与跑失败同形）。

        `forbid_marker` 是**不得出现**的字符串，用于"期望假绿"的用例 ——
        只断言 `[PASS]` 不够：一个既印 `[PASS]` 又印 `[FAIL]` 的脚本
        （判据 A/B/C 的多问题汇总形态）也会命中 `[PASS]`，于是"没变绿"
        被误判成"变绿"（ISS-140 的镜像：不只红的形状，**绿的形状**也要验）。

        `extra_files`：变异时新建的文件（相对路径 → 内容），跑完删除。
        覆盖范围类判据的变异常常需要**造一个新文件**才能触发
        （例如"tests/ 下出现子目录时，非递归扫描会漏"）。
        """
        if not _copy_is_alive(repo):
            add(name, False, "副本已消失（并发实例删掉了它？见 ISS-156）")
            emit(f"  BAD  {label} —— 变异副本在取证中途消失：{repo}")
            return

        original_texts: dict[Path, str] = {}
        for rel, _old, _new in edits:
            path = repo / rel
            if path not in original_texts:
                original_texts[path] = path.read_text(encoding="utf-8")
        # 锚点命中检查（在写入任何东西之前全查完 —— 失败时副本未被改动）。
        for rel, old, _new in edits:
            if old not in original_texts[repo / rel]:
                add(name, False, f"变异锚点未找到：{rel} :: {old[:60]!r}")
                emit(f"  BAD  {label} —— 变异锚点未找到（脚本与代码已脱节）")
                return

        created: list[Path] = []
        try:
            current: dict[Path, str] = dict(original_texts)
            for rel, old, new in edits:
                path = repo / rel
                current[path] = current[path].replace(old, new, 1)
            for path, text in current.items():
                path.write_text(text, encoding="utf-8")

            for rel, content in (extra_files or {}).items():
                path = repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                created.append(path)

            code, out = _run(repo, tool_rel)
            hit = expect_marker in out
            exit_ok = (code != 0) if expect_exit_nonzero else (code == 0)
            vetoed = bool(forbid_marker) and forbid_marker in out
            ok = exit_ok and hit and not vetoed
            detail = f"exit={code}，{'命中' if hit else '未命中'} {expect_marker!r}"
            if forbid_marker:
                detail += f"，{'出现了' if vetoed else '未出现'} {forbid_marker!r}"
            add(name, ok, detail)
            emit(f"  {'OK ' if ok else 'BAD'}  {label} → exit={code}")
            for line in out.splitlines():
                s = line.strip()
                if not s:
                    continue
                if (
                    expect_marker in line
                    or (forbid_marker and forbid_marker in line)
                    or s.startswith(("[FAIL]", "[PASS]", "RESULT="))
                ):
                    emit(f"        | {s[:160]}")
        finally:
            for path in created:
                path.unlink(missing_ok=True)
            for path, text in original_texts.items():
                path.write_text(text, encoding="utf-8")

    # ------------------------------------------------------------------ §0
    emit("# WP-DEBT-02 测试库可信度判据的反向验证（把错误实现放回去，判据必须报红）")
    emit(f"# 生成时间: {_now()}")
    emit(f"# 命令: python tools/quality/{SELF_PATH.name}{'  --fast' if args.fast else ''}")
    emit(f"# 被测判据: A={TOOL_A}")
    emit(f"#           B={TOOL_B}")
    emit(f"#           C={TOOL_C}")
    if args.fast:
        emit("# 模式: --fast（只跑判据 A 的纯静态变异；B/C 要建库/跑 pytest）")
    emit()

    repo = _make_copy("fast" if args.fast else "full")
    before = _snapshot(REPO_ROOT)
    #: 取证窗口**开始前**就已是 git 脏状态的路径。见 `_concurrently_modified_paths`（ISS-154）。
    dirty_at_start = _concurrently_modified_paths()
    emit(f"# 变异副本: {repo}（真树只读）")
    if dirty_at_start:
        emit(
            f"# 窗口开始前已脏（{len(dirty_at_start)} 个，计「隔离」不计「豁免」）: "
            + "、".join(sorted(dirty_at_start)[:8])
            + ("…" if len(dirty_at_start) > 8 else "")
        )
    emit()

    try:
        # ====================================================== §-1 探针自校验
        # ⚠️ ISS-157：`_copy_is_alive` 的探针若自己写错路径，会**恒报「副本消失」**
        # ⇒ 后续每条变异都红 ⇒ 判据永远红（与 ISS-143 同形）。所以在用它之前，
        # 先拿**真树**（这些路径必然存在）校一次探针本身。它绿，"副本消失"这句话
        # 才有诊断力；它红，那是**探针写错了**，与副本无关 —— 两者必须能区分。
        emit("## [-1] 探针自校验：`_copy_is_alive` 用的路径在真树上必须存在")
        probe_ok = _probe_is_sound()
        add("probe-sound", probe_ok, f"{len(_COPY_PROBES)} 个探针路径在真树上均存在")
        emit(f"  {'OK ' if probe_ok else 'BAD'}  探针自校验 → {'真树里路径齐全' if probe_ok else '探针路径写错了（与副本无关！）'}")
        for rel in _COPY_PROBES:
            exists = (REPO_ROOT / rel).exists()
            emit(f"        | {'存在' if exists else '缺失'} {rel}")
        emit()

        # ============================================================== §1 判据 A
        emit("## [0] 判据 A 副本基线：变异之前必须是绿的")
        code, out = _run(repo, TOOL_A)
        base_green = code == 0 and "[PASS]" in out
        add("A-baseline-green", base_green, f"exit={code}")
        emit(f"  {'OK ' if base_green else 'BAD'}  判据 A 基线 → exit={code}（期望 0 / [PASS]）")
        emit()

        emit("## [1] A① 把一处默认 DSN 改回系统库 `postgres`")
        emit("   （断言的是**红的形状**：文件名 + 库名，不只是 rc≠0）")
        run_case(
            repo=repo,
            tool_rel=TOOL_A,
            edits=[
                (
                    "services/backend/tests/test_devtools.py",
                    "localhost:5432/rednote_test",
                    "localhost:5432/postgres",
                )
            ],
            expect_marker="postgres",
            name="A-red-when-default-dsn-points-at-system-db",
            label="A① test_devtools 默认 DSN → postgres",
        )
        emit()

        emit("## [2] A② 新增一个指向系统库的测试文件（判据必须抓到**新出现**的违规）")
        emit("   这条同时证明：判据扫的是**磁盘现状**，不是一份写死的文件名单。")
        run_case(
            repo=repo,
            tool_rel=TOOL_A,
            edits=[],
            extra_files={
                "services/backend/tests/test_zz_probe_reserved_db.py": (
                    '"""反向验证探针：默认 DSN 指向系统库（**仅存在于变异副本**）。"""\n'
                    "\n"
                    'DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/postgres"\n'
                )
            },
            expect_marker="test_zz_probe_reserved_db.py",
            name="A-red-when-a-new-violating-file-appears",
            label="A② 新增违规文件",
        )
        emit()

        emit("## [3] A③ 覆盖范围对账：`rglob` → `glob` + 造一个子目录")
        emit("   本仓 `tests/` 是**扁平的**（25 个 .py 全在顶层），所以单改 `glob`")
        emit("   **不会**缩水 —— 必须同时造一个子目录，缩水才显现。")
        emit("   这正是那条 `os.walk` 独立对账存在的意义：它抓的是**遍历机制**，")
        emit("   而不是「当前恰好没有子目录」这个偶然事实。")
        run_case(
            repo=repo,
            tool_rel=TOOL_A,
            edits=[(TOOL_A, 'TESTS_DIR.rglob("*.py")', 'TESTS_DIR.glob("*.py")')],
            extra_files={
                "services/backend/tests/subpkg/__init__.py": "",
                "services/backend/tests/subpkg/test_nested.py": "# 嵌套测试（变异副本专用）\n",
            },
            expect_marker="覆盖范围对账失败",
            name="A-red-when-scan-scope-shrinks",
            label="A③ rglob → glob + 子目录",
        )
        emit()

        if not args.fast:
            # ========================================================== §2 判据 B
            emit("## [4] 判据 B 副本基线（**会真跑一遍 pytest**，约 2 分钟）")
            code, out = _run(repo, TOOL_B)
            b_green = code == 0 and "[PASS]" in out
            add("B-baseline-green", b_green, f"exit={code}")
            emit(f"  {'OK ' if b_green else 'BAD'}  判据 B 基线 → exit={code}（期望 0 / [PASS]）")
            if not b_green:
                for line in out.splitlines()[-12:]:
                    emit(f"        | {line.strip()[:160]}")
            emit()

            emit("## [5] B① 绕过「测试真跑过」这条前置条件（ISS-143：没跑 vs 跑失败同形）")
            emit("   成对变异，证明那条前置条件是**必要防线**（不是装饰）：")
            emit("   ①-a 只把 pytest 参数弄坏（测试一条没跑）→ 期望判据**报红拦住**；")
            emit("   ①-b 再叠加「谎报跑了 1 条」→ 期望判据**错误地 PASS**。")
            emit("   两边对照才能证明：拦住它的是那条前置条件，不是别的东西。")
            emit()
            emit("   ①-a 单独把 pytest 参数弄坏：")
            run_case(
                repo=repo,
                tool_rel=TOOL_B,
                edits=[
                    (
                        TOOL_B,
                        '"REDNOTE_TEST_RESIDUE_PYTEST_ARGS", ""',
                        '"REDNOTE_TEST_RESIDUE_PYTEST_ARGS", "no_such_test_path_xyz"',
                    )
                ],
                expect_marker="[FAIL]",
                name="B-red-when-no-test-ran",
                label="B①-a 测试一条没跑 → 必须报红",
            )
            emit()
            emit("   ①-b 在①-a 基础上**同时摘掉两道防线**（回到「没有这道防线」的世界）：")
            emit("       防线一 `collected == 0`：谎报「跑了 1 条」（解析函数返回 max(...,1)）；")
            emit("       防线二 `proc.returncode != 0`：置为恒假（它独立地也会拦住）。")
            emit("       ⚠️ 只摘一道**必然仍报红** —— 实测过：只摘防线一时输出")
            emit("          `[FAIL] pytest 自身未通过（退出码 4，passed=1）`。")
            emit("          「摘掉一道防线」不等于「防线不存在」，那样这个用例")
            emit("          永远无法满足它自己的期望（前提写错，与它要防的病同形）。")
            run_case(
                repo=repo,
                tool_rel=TOOL_B,
                edits=[
                    (
                        TOOL_B,
                        '"REDNOTE_TEST_RESIDUE_PYTEST_ARGS", ""',
                        '"REDNOTE_TEST_RESIDUE_PYTEST_ARGS", "no_such_test_path_xyz"',
                    ),
                    (
                        TOOL_B,
                        "    return passed, total\n\n\ndef _classify",
                        "    return max(passed, 1), max(total, 1)\n\n\ndef _classify",
                    ),
                    (
                        TOOL_B,
                        "    if proc.returncode != 0:",
                        "    if False:  # 变异：摘掉第二道防线",
                    ),
                ],
                expect_marker="[PASS]",
                forbid_marker="[FAIL]",
                name="B-lies-pass-when-guard-removed",
                label="B①-b 摘掉两道防线 → 假绿（证明确实是它们拦住的）",
                expect_exit_nonzero=False,
            )
            emit()

            emit("## [6] B② 删掉 WP21 的索引清理段（判据必须抓到真残留）")
            run_case(
                repo=repo,
                tool_rel=TOOL_B,
                edits=[
                    (
                        "services/backend/tests/test_knowledge_wp21.py",
                        "    residue = _ann_index_names(engine) - ann_before",
                        "    residue = set()  # 变异：假装没有残留",
                    )
                ],
                expect_marker="[FAIL]",
                name="B-red-when-wp21-leaves-index-residue",
                label="B② WP21 清理段删除",
            )
            emit()

            # ========================================================== §3 判据 C
            emit("## [7] 判据 C 副本基线（**建两个临时库**跑迁移与 create_all）")
            code, out = _run(repo, TOOL_C)
            c_green = code == 0 and "[PASS]" in out
            add("C-baseline-green", c_green, f"exit={code}")
            emit(f"  {'OK ' if c_green else 'BAD'}  判据 C 基线 → exit={code}（期望 0 / [PASS]）")
            if not c_green:
                for line in out.splitlines()[-12:]:
                    emit(f"        | {line.strip()[:160]}")
            emit()

            emit("## [8] C① 判据真在比「列」：成对变异")
            emit("   ①-a 往模型里加一列（模型侧多一列）→ 期望**报红**，且指名该列；")
            emit("   ①-b 在①-a 基础上删掉「列」比较那一行 → 期望**变绿**。")
            emit("   两边对照证明：报红的原因是「列比较在工作」，不是别的巧合。")
            emit()
            emit("   ①-a 模型侧加一列（迁移侧不会有）：")
            run_case(
                repo=repo,
                tool_rel=TOOL_C,
                edits=_model_anchor_edits(),
                expect_marker="只在**模型**里有",
                name="C-red-when-model-has-extra-column",
                label="C①-a 模型多一列 → 必须报红",
            )
            emit()
            emit("   ①-b 同样变异 + 删掉「列」比较那一行（证明是列比较在抓它）：")
            run_case(
                repo=repo,
                tool_rel=TOOL_C,
                edits=_model_anchor_edits()
                + [
                    (
                        TOOL_C,
                        '        problems += _diff("列", snap_mig["column"], snap_mod["column"], notes)\n',
                        "",
                    )
                ],
                expect_marker="[PASS]",
                forbid_marker="[FAIL]",
                name="C-passes-when-column-diff-removed",
                label="C①-b 删掉列比较 → 变绿（证明抓它的是列比较）",
                expect_exit_nonzero=False,
            )
            emit()

            emit("## [9] C② 排除集自检必须能报红（记账表断言换成不存在的名字）")
            run_case(
                repo=repo,
                tool_rel=TOOL_C,
                edits=[
                    (
                        TOOL_C,
                        "    bookkeeping = _migration_bookkeeping_tables()",
                        "    bookkeeping = frozenset({'no_such_bookkeeping_table'})",
                    )
                ],
                expect_marker="排除集自检",
                name="C-red-when-exclusion-set-is-wrong",
                label="C② 排除集指向不存在的表",
            )
            emit()

        # -------------------------------------------------------------- §4 收尾
        emit("## [10] 副本全部还原后必须回绿（证明前面是「变异」而非「破坏」）")
        for tool_rel, tag in ((TOOL_A, "A"),) + (() if args.fast else ((TOOL_B, "B"), (TOOL_C, "C"))):
            code, out = _run(repo, tool_rel)
            green = code == 0 and "[PASS]" in out and "[FAIL]" not in out
            add(f"restored-{tag}-green", green, f"exit={code}")
            emit(f"  {'OK ' if green else 'BAD'}  判据 {tag} 还原 → exit={code}（期望 0 / [PASS]）")
        emit()

        emit("## [11] 真树未被触碰（整树哈希，不是自证清白的那几个文件）")
        writes_ok, writes_detail = _own_writes_are_disjoint()
        add("own-writes-disjoint", writes_ok, writes_detail)
        emit(f"  {'OK ' if writes_ok else 'BAD'}  本工具写盘范围 vs 取证范围：{writes_detail}")
        after = _snapshot(REPO_ROOT)
        changed = sorted(rel for rel in before if before.get(rel) != after.get(rel))
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        #: ⚠️ 三类变更里，**窗口开始前就已经脏/不存在**的那些不算本工具触碰真树 ——
        #: 它们是**并行会话**在活的（ISS-154）。判据原先把跨时刻的整树哈希当**不变量**，
        #: 与本包刚修的 ISS-148（TOCTOU）同族：并行写盘是常态，完整版因此**必然**红，
        #: 而"必定红"与"真的坏了"同形（ISS-143）。
        #: ⚠️ 这不是豁免：本脚本自己的写盘范围另由 `own-writes-disjoint` 用**结构**证明
        #: 不在 TREE_SCOPE 内。**结构证明 + 时刻隔离**合起来才够。
        def _pre_existing(rel: str) -> bool:
            if rel in dirty_at_start:
                return True
            # 目录级 git 报告（如 `?? dir/`）要能覆盖其下的具体文件。
            return any(rel == d or rel.startswith(d.rstrip("/") + "/") for d in dirty_at_start)

        foreign = sorted(
            rel for rel in (changed + added + removed) if _pre_existing(rel)
        )
        own_changed = [rel for rel in changed if rel not in foreign]
        own_added = [rel for rel in added if rel not in foreign]
        own_removed = [rel for rel in removed if rel not in foreign]
        same = not own_changed and not own_added and not own_removed
        add("real-tree-untouched", same, f"{len(before)} 个文件哈希一致")
        emit(f"  {'OK ' if same else 'BAD'}  真树 {len(before)} 个文件哈希前后一致：{'是' if same else '否'}")
        for rel in own_changed:
            emit(f"        | 内容被改：{rel}")
        for rel in own_added:
            emit(f"        | 新增文件：{rel}")
        for rel in own_removed:
            emit(f"        | 文件丢失：{rel}")
        if foreign:
            emit(
                f"  —— 另有 {len(foreign)} 个路径在取证窗口内变动，但**窗口开始前就已脏**，"
                "属并行会话（不计入本判据）："
            )
            for rel in foreign[:10]:
                emit(f"        · {rel}")
            if len(foreign) > 10:
                emit(f"        · …（共 {len(foreign)} 个）")
        if not same:
            # ⚠️ 2026-09-30 两轮自纠（ISS-152 → ISS-154）：
            #
            # 第一轮（ISS-152）：原文案把所有不一致都写成「这是本工具最严重的失败」，
            # 实测**误诊**了一次 —— 那次哈希漂移是**并行的另一个会话/人**在后台任务
            # 运行的十分钟里编辑了 `tools/m1/rerun_all_evidence.py`（mtime 先后可查），
            # 而本工具从头到尾**只写两处**：副本 `/tmp/rdbtdc-*/repo`（上面 `_make_copy`
            # 建的）与本日志 `evidence/m3/test-db-credibility-reverse.log` ——
            # 两者都不在 `TREE_SCOPE` 里（`evidence/` 根本不在取证范围）。
            # 所以「本工具改了真树」在**结构上不可能**，把它当成默认解释会让报告人
            # 去修一个不存在的缺陷，而真正的动作（"别再同时编辑仓库"）被淹没。
            # 这与 ISS-143 同族：**两个不同的成因印出同一条断言**，
            # 读的人无法区分，于是诊断被这句断言本身带偏。
            #
            # 第二轮（ISS-154）：光改文案**不够**。判据本身把「跨时刻的整树哈希」
            # 当成了**不变量**，而本仓并行开发是常态（实测另一会话正在写
            # `modules/billing/`，mtime 距运行时刻 1 秒）⇒ 完整版**必然**红。
            # "必定红"与"真的坏了"同形（ISS-143）——**永远红的判据等于没有判据**，
            # 它只会训练人忽略这条输出。与本包刚修的 ISS-148（TOCTOU）是同一族。
            # 修法：用 `_concurrently_modified_paths()` 在窗口**开始前**冻结一份
            # 「本就不是 HEAD 状态」的路径集，窗口内的变动落在其中 ⇒ 隔离。
            # 注意这是**隔离**不是**豁免**，见 `_concurrently_modified_paths` 的说明。
            #
            # 上面那条 `own-writes-disjoint` 判据把「结构上不可能」从**声明**变成
            # **当场可查**：只要它绿，下面"默认是成因乙"这句话就是有依据的。
            emit("")
            emit(f"  本工具写盘范围自检：{'OK' if writes_ok else 'BAD'}  {writes_detail}")
            emit("  诊断（两个成因必须区分开，别默认成后者）：")
            emit(
                "    · 成因甲「本工具改了真树」——"
                + ("**结构上不可能**（上方自检为 OK：本脚本只写副本与本日志，"
                   "二者均在取证范围之外）" if writes_ok
                   else "**可能**（上方自检为 BAD：本脚本的写盘范围与取证范围相交，"
                        "先修 _OWN_WRITE_PATHS 与 TREE_SCOPE 的关系）")
            )
            emit(
                "    · 成因乙「外部有人正在编辑真树」——"
                + ("**默认解释**：取证窗口内（典型 10 分钟）任何其它进程/会话/编辑器"
                   "写盘都会命中这里。" if writes_ok
                   else "（在成因甲排除之前，不能默认是它）")
            )
            emit("  处置：")
            emit("    1. 按上面列表逐个 `git diff <文件>`（未被 git 跟踪的看 mtime），")
            emit("       确认改动**不是**本包引入的；")
            emit("    2. 冻结编辑（本包收口期间不碰 services/backend、migrations、tools），")
            emit("       然后重跑完整版；")
            emit("    3. 若重跑仍不稳定，用 `git stash` 前后各跑一次对比哈希集合，")
            emit("       定位是哪个进程在写。")
        emit()
    finally:
        shutil.rmtree(repo.parent, ignore_errors=True)

    # ------------------------------------------------------------------ §12
    failures = [name for name, ok, _ in checks if not ok]
    #: 期望条数由模式决定。⚠️ 变量名必须是 `EXPECTED_CHECKS` ——
    #: `tools/m1/check_evidence_scripts.py` 用
    #: `complete = len(checks) >= EXPECTED_CHECKS` 这个**形态**做门禁
    #: （命名带后缀 `_FAST`/`_FULL` 会被判成"没有核对条数"，
    #:  实测被门 3 抓到过 —— 那正是"给变量改个名就能绕过判据"的形态，ISS-82 族）。
    EXPECTED_CHECKS = EXPECTED_CHECKS_FAST if args.fast else EXPECTED_CHECKS_FULL
    complete = len(checks) >= EXPECTED_CHECKS
    #: ⚠️ `>=` 有一处**结构性盲区**（ISS-153）：它只拦"条数变少"，
    #: 拦不住"判据变多了而常量没跟着涨"。本包实测就发生过 ——
    #: 完整版实际 17 条、`EXPECTED_CHECKS_FULL` 仍写 15，`>=` 静默通过，
    #: 而注释里那句"A 基线 1 + … = 15"因此成了一句**没人核对的算术**。
    #: 这与 ISS-143 同族（常量与实际脱钩后，输出照旧印"期望 N 条"，
    #: 读证据的人会以为已核对过）。门 3 的形态不能改，故**另加一条相等判据**：
    #: 条数与期望必须逐条对上，多一条也要回来改常量。
    exact = len(checks) == EXPECTED_CHECKS
    emit("## [12] 结论")
    for name, ok, detail in checks:
        suffix = f"（{detail}）" if detail else ""
        emit(f"  {'PASS' if ok else 'FAIL'} {name}{suffix}")
    emit()
    verdict = not failures and complete and exact
    emit(
        f"RESULT={'PASS' if verdict else 'FAIL'} "
        f"checks={len(checks)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条，"
        f"条数核对 {'一致' if exact else '不一致'}）"
    )

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写出 {OUT_FILE.relative_to(REPO_ROOT)}")
    print(
        f"RESULT={'PASS' if verdict else 'FAIL'} "
        f"checks={len(checks)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条，"
        f"条数核对 {'一致' if exact else '不一致'}）"
    )
    for name in failures:
        print(f"  FAIL {name}")
    if not exact:
        print(f"  FAIL 条数核对：实际 {len(checks)} 条 ≠ 期望 {EXPECTED_CHECKS} 条")
    return 0 if verdict else 1


#: `--fast` 的 CHECK 下界：探针自校验 1 + A 基线 1 + A 三个变异 3 + 还原 1 + 真树 1 + 写盘范围自检 1 = 8
EXPECTED_CHECKS_FAST = 8
#: 完整版：探针自校验 1 + A 基线 1 + A 变异 3 + B 基线 1 + B 变异 3 + C 基线 1 + C 变异 2
#:          + 还原 3 + 写盘范围自检 1 + 真树 1 = 18
#: ⚠️ 这里曾写 15（漏算 B 的第二个变异），且因 `>=` 判据静默通过 —— 见 ISS-153。
#: 后又曾写 17，加「探针自校验」后应为 18 —— 见 ISS-157。
#: **改动判据条数后必须回来重数这个常量**，脚本会在不一致时直接判 FAIL。
EXPECTED_CHECKS_FULL = 18


if __name__ == "__main__":
    raise SystemExit(main())
