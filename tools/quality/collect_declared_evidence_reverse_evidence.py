#!/usr/bin/env python
"""`tools/m1/audit_consistency.py` §4 的**反向验证**：把失效形态放回去，判据必须报红。

产出：`evidence/m5/declared-evidence-reverse.log`

## 为什么需要这个文件

`audit_consistency.py` §4（`check_declared_evidence_exists`）此刻打印
「文档声明的证据文件 97 处 / 不存在 0 处」，但那**只证明"当前工作区看起来自洽"**，
不能证明"它被违反时会报红"。两者之差正是判据纪律第 9 条：

> **「声称自己有防线」比「没有防线」更危险。**

## 被测判据的失效面（本脚本要证的，逐条可指认）

§4 的**覆盖范围由后缀白名单（`_DECLARED_EVIDENCE_SUFFIXES`）单独决定**（铁律 14）。
原版只写 `\\.log` ⇒ 全仓 `evidence/**/*.json` 声明**零覆盖**，而输出照旧打印
「声明 N 处 / 不存在 0 处」，**与"判过"完全同形**（ISS-95/115/124 族）。
实测：`docs/m5/WP47-演练记录.md` 引用 8 个 `.json`，扩展前一处都不在扫描面内。

更隐蔽的一层：**"判据的判据"自己也会得同一种病**。本扩展的首版自测是
"遍历白名单断言语义命中"—— 白名单一缩，"自测"跟着缩。实测变异
（白名单退回 `("log",)`）下**自测全程静默**、`.json` 臂消失而无人报警。
故自测 (A) 的期望集合改由**文档实测后缀**派生，(D) 另对账白名单完备性。
⚠️ 本脚本的 `[4] [5]` 两条用例，正是为了证明"分写解析的两条不变式"也**有牙齿** ——
它们历史上配出错路径的那行语料**已被修好**，只靠工作树**观测不到差异**
（实测：把不变式放宽，审计照旧 PASS）。

## 变异清单（每条对应一个**真实失效面**）

⚠️ 下表**逐条与 `main()` 里的节号 `[N]` 一一对应**。
写"声称有防线却指不出是哪条"正是本包要消灭的病（践 ISS-95）。

| # | 变异 | 期望 | 它证明的失效面 |
|---|---|---|---|
| `[0]` | 沙箱基线（未变异） | 绿：0 自测红 / 0 缺失 / 三臂都命中 | 判据在未变异时确实是绿的（且 `.log`/`.json`/`.jsonl` 三臂在真文档上都活着） |
| `[1]` | 白名单退回 `("log",)` | 红 + 含「连写声明未被命中」+ 含「既不在扫描白名单」 | 铁律 14 本体：覆盖范围缩窄必须响亮（活性 A + 完备性 D 两路报红） |
| `[2]` | 白名单少一个后缀（去掉 `jsonl`） | 红 + 含「`.jsonl`」 | 细粒度缩窄也抓（不是只有"退回 log only"才响） |
| `[3]` | 连写正则去掉结尾边界 | 红 + 含「不该被连写形态命中」 | `x.jsonl` 会被读成 `x.json` ⇒ 凭空造出不存在的路径（假红），首版预检实测踩过 |
| `[4]` | 分写不变式 ② 放宽（`names < 1`） | 红 + 含「负例 ②」 | "恰好 1 目录 / 1 名字"被改坏 ⇒ 会臆造路径；**工作树观测不到**，故须合成样本 |
| `[5]` | 分写不变式 ① 去掉 mask | 红 + 含「负例 ①」 | 同行多个连写声明会与别处反引号名**交叉配对**（本判据首版踩过，配出过不存在的路径） |
| `[6]` | 副本 docs 注入一处不存在的 `.json` 声明 | 缺失 1 处 + **指名该路径** | 判据对 `.json` 真的在**判**（不只是计数）；且红的形状要指名到路径 |
| `[7]` | 在 `[6]` 基础上撤掉注入行（**成对证明**） | 回绿 | `[6]` 的红**确实是那条注入**造成的（否则可能是别的巧合） |
| `[8]` | 白名单三档（`log` → `+json` → `+jsonl`） | 每档 0 缺失 + 声明数**严格递增** + 分布里出现该档新增后缀 | 扩展是**真的扩大了覆盖**，不是把数字挪了个位置 |
| `[9]` | 白名单**多**一个无关注后缀（`exe`） | 仍绿 | 多纳入不假红 ⇒ 不会为"过判据"而不敢扩展 |
| `[10]` | 真树未被触碰（整树哈希）+ 写盘范围与取证范围不相交 | 一致 / 不相交 | 本脚本**只读真树**（副本在 `/tmp`，判据也是副本） |

> ⚠️ 编号纪律：**基线不算一条变异**（它是"未变异"这个前提）。每行都显式写出代码节号。

## 沙箱策略：**真树全程只读**（比"改真树再还原"更强的形态）

§4 **不接路径参数**（它自己 `REPO_ROOT.glob("docs/**/*.md")`，铁律 14：范围由磁盘决定），
所以不能像 `collect_all_exports_reverse_evidence.py` 那样"拷个底座再喂给判据"。
同族的 `collect_node_paired_guard_reverse_evidence.py` 的做法是**把变异写进真实工作树**、
跑完在 `finally` 里还原 —— 它扛的残留风险靠"还原后读回核对字节"来限制。

本脚本用**更低风险的形态**（本判据只需 `docs/` + `README.md` + `evidence/`）：

```
/tmp/rde-<tag>-XXXX/repo/
├── README.md          ← 真树副本（`[6]` 的注入打进这里）
├── docs/              ← 真树副本（`shutil.copytree`）
├── evidence/          ← **符号链接**到真树 evidence（零复制；`is_file()` 会跟随）
└── tools/m1/audit_consistency.py   ← 判据副本（变异只改这一份）
```

于是：① 判据副本的 `Path(__file__).parents[2]` 正好是沙箱根 ⇒ 它扫的是沙箱；
② 变异改的是**副本里的判据文本**，真树 `tools/m1/audit_consistency.py` 一个字节都不动；
③ 副本里的 `README.md`/`docs/` 可随意写；④ `evidence/` 是软链 ⇒ 只读。
**连"还原"都不需要**（没有东西需要还原），所以不存在"崩在还原之前"的窗口。

⚠️ 软链写盘的危险方向是"变异写穿软链改到真树" —— 本脚本**从不写** `repo/evidence` 下任何路径，
`_own_writes_are_disjoint()` 与 `[10]` 的整树哈希各证一次。

跑法：`NO_PROXY='*' python tools/quality/collect_declared_evidence_reverse_evidence.py`
退出码 0 = 全部变异都被抓住（且成对证明成立）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_REL = "tools/m1/audit_consistency.py"
AUDIT = REPO_ROOT / AUDIT_REL
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m5"
OUT_FILE = EVIDENCE_DIR / "declared-evidence-reverse.log"

#: 「真树未被触碰」的取证范围。只哈希几个文件是**自证清白**。
#: ⚠️ 含 `docs` / `README.md` —— 因为本判据的**输入就是它们**，
#: "变异有没有写穿到真树"必须先在这一层被否掉。
TREE_SCOPE = ("tools", "docs", "README.md")

#: 本脚本**唯一**会写盘的一处（相对仓库根）。副本在 `/tmp`，天然不相交。
_OWN_WRITE_PATHS = (str(OUT_FILE.relative_to(REPO_ROOT)),)

#: §4 声明数的**下界**（铁律 1：判据只能断言"我建的还在"，不能断言"恰好 N"）。
#: 实测基线 97 处（`.log` 85 / `.json` 11 / `.jsonl` 1），取 90 留出文档增删余量。
_MIN_DECLARED = 90

TIMEZONE = timezone(timedelta(hours=8))

# --------------------------------------------------------------------------- #
# 变异锚点：**逐字**取自 `audit_consistency.py`。找不到就抛（锚点漂移 ⇒ 变异会静默失效）
# --------------------------------------------------------------------------- #
_WL_LINE = '_DECLARED_EVIDENCE_SUFFIXES: tuple[str, ...] = ("log", "json", "jsonl")'
_END_LINE = '_EVIDENCE_SUFFIX_END = r"(?![A-Za-z0-9_.\\-])"'
_MASK_LINE = (
    '    masked = _DECLARED_EVIDENCE_RE.sub(lambda m: " " * len(m.group(0)), line)'
)
_INV2_LINE = "    if len(dirs) != 1 or len(names) != 1:\n        return None"

#: §4 输出首行的解析（判据自己在 `check_declared_evidence_exists` 里打印）。
_HEAD_RE = re.compile(r"文档声明的证据文件 (\d+) 处 / 不存在 (\d+) 处")
_DIST_RE = re.compile(r"\.([A-Za-z0-9]+) (\d+)")

#: 判据源码的**开始快照**（真树只读一次；本脚本全程用这一份做变异基线）。
ORIG_AUDIT_SRC = AUDIT.read_text(encoding="utf-8")

_EXCLUDE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})

checks: list[tuple[str, bool, str]] = []
lines: list[str] = []


def add(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))


def emit(text: str = "") -> None:
    lines.append(text)


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _own_writes_are_disjoint() -> tuple[bool, str]:
    """本脚本的写盘范围必须与取证范围 `TREE_SCOPE` **不相交**。

    相交的话「真树未被触碰」这条判据就永远无法自证（判据纪律第 9 条：
    声称有防线）。副本目录在 `/tmp`，不需要参与比较。
    """
    clashes = [
        rel
        for rel in _OWN_WRITE_PATHS
        if any(rel == scope or rel.startswith(scope + "/") for scope in TREE_SCOPE)
    ]
    if clashes:
        return False, f"本脚本会写 {clashes}，落在取证范围 {TREE_SCOPE} 内"
    return True, "写盘范围与取证范围不相交（副本在 /tmp，日志在 evidence/m5）"


def _concurrently_modified_paths() -> set[str]:
    """取证窗口**开始之前**就已是 git 脏状态的路径（ISS-154）。

    ⚠️ 跨时刻的整树哈希是**可变量**，不是不变量：本仓并行开发是常态，
    窗口内任何人改一次文件就**必然**红，而"必定红"与"真的坏了"同形（ISS-143）。
    收集范围由 `git status --porcelain` **现场决定**，不由人写名单（铁律 14）。

    ⚠️ `-c core.quotepath=false` 不可省（ISS-166）：默认会把非 ASCII 路径
    转义成八进制，于是与 `TREE_SCOPE` 里的真实路径**永不相等** ⇒ 该隔离的没隔离。
    本仓 `TREE_SCOPE` 含 `docs/`（**有**中文文件名）⇒ 缺这个 flag 时会假红。
    """
    import subprocess

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


def _snapshot(repo: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in TREE_SCOPE:
        base = repo / rel
        if not base.exists():
            continue
        targets = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in targets:
            if not path.is_file() or path.suffix == ".pyc":
                continue
            if any(part in _EXCLUDE_DIRS for part in path.parts):
                continue
            out[str(path.relative_to(repo))] = _sha(path)
    return out


# --------------------------------------------------------------------------- #
# 沙箱：真树只读（详见模块 docstring 的目录图）
# --------------------------------------------------------------------------- #
def _make_sandbox(tag: str) -> Path:
    """建一个**足够 §4 跑**的微型仓库，`evidence/` 用软链（零复制）。

    ⚠️ **不能照抄 `_make_copy()` 的 `ignore_patterns(..., "evidence")`**：
    那个模板服务于不读 evidence 的判据；§4 的全部意义就是"声明必须在
    evidence 里真的存在"，缺了 evidence 目录 ⇒ 基线必然满屏缺失（前提写错）。
    """
    base = Path(tempfile.mkdtemp(prefix=f"rde-{tag}-"))
    repo = base / "repo"
    repo.mkdir()
    shutil.copy2(REPO_ROOT / "README.md", repo / "README.md")
    shutil.copytree(
        REPO_ROOT / "docs", repo / "docs", ignore=shutil.ignore_patterns(*_EXCLUDE_DIRS)
    )
    os.symlink(REPO_ROOT / "evidence", repo / "evidence")
    (repo / "tools" / "m1").mkdir(parents=True)
    shutil.copy2(AUDIT, repo / AUDIT_REL)
    return repo


def _write_audit(repo: Path, text: str) -> None:
    (repo / AUDIT_REL).write_text(text, encoding="utf-8")


def _mutate(repo: Path, old: str, new: str) -> None:
    """把正确写法换成失效形态。锚点找不到就**抛**（宁可崩，不可假绿）。"""
    if old not in ORIG_AUDIT_SRC:
        raise AssertionError(
            f"变异锚点在 {AUDIT_REL} 里找不到：{old!r}"
            f" —— 锚点漂移会让变异**静默失效**，那样本脚本会变成「声称有防线」"
        )
    _write_audit(repo, ORIG_AUDIT_SRC.replace(old, new, 1))


def _restore(repo: Path) -> None:
    _write_audit(repo, ORIG_AUDIT_SRC)


def _load_probe(repo: Path, seq: int) -> object:
    """按文件路径加载**副本里的**判据模块（不执行 `__main__` 块）。

    ⚠️ **必须先 `sys.modules[name] = module` 再 `exec_module`**（ISS-164）：
    CPython 处理注解时会 `sys.modules.get(cls.__module__).__dict__`，
    模块不在 `sys.modules` 里时它是 `None` ⇒ `AttributeError`。
    本判据此刻没有 `@dataclass`，所以缺这一行**暂时不报错** ——
    那正是最坏情况：等哪天判据引入 dataclass 才炸，而那时炸的是**别的包**。
    全仓的注册纪律由 `tools/quality/check_loader_registration.py` 静态守。

    每个用例用**唯一模块名**，避免同名模块被 `sys.modules` 缓存成上一个变体。
    """
    path = repo / AUDIT_REL
    name = f"_rde_probe_{seq}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _eval(repo: Path, seq: int) -> dict[str, object]:
    """跑一遍 §4，返回**结构化**结果（不解析 stdout —— 直接调函数拿返回值）。

    ⚠️ 直接调 `check_declared_evidence_exists()` 而不跑整个 `audit_consistency.py`：
    ① 后者的 §3 门禁要真跑命令、§5 要比 mtime，那些在沙箱里都是噪声；
    ② 本脚本要断言的只有 §4。范围收窄到"被测判据本体"，不受无关段落干扰。
    """
    module = _load_probe(repo, seq)
    section_lines, problems = module.check_declared_evidence_exists()  # type: ignore[attr-defined]
    head = next((ln for ln in section_lines if "文档声明的证据文件" in ln), "")
    match = _HEAD_RE.search(head)
    declared = int(match.group(1)) if match else -1
    missing = int(match.group(2)) if match else -1
    dist: dict[str, int] = {}
    if match:
        for suffix, count in _DIST_RE.findall(head):
            dist[suffix] = int(count)
    return {
        "declared": declared,
        "missing": missing,
        "dist": dist,
        "missing_paths": [ln.strip() for ln in section_lines if "★缺失" in ln],
        "problems": list(problems),
        "head": head,
    }


def _has(problems: list[str], needle: str) -> bool:
    return any(needle in p for p in problems)


def main() -> int:
    dirty_at_start = _concurrently_modified_paths()
    before = _snapshot(REPO_ROOT)

    emit("# audit_consistency.py §4（文档声明的证据文件必须存在）反向验证")
    emit(f"# 生成时间: {_now()}")
    emit(
        "# 命令: NO_PROXY='*' python"
        " tools/quality/collect_declared_evidence_reverse_evidence.py"
    )
    emit("# 覆盖: tools/m1/audit_consistency.py §4 的失效形态 —— 白名单缩窄（2 种粒度）")
    emit("#       / 缺结尾边界 / 分写解析两条不变式 / 存在性判定本体 + 成对证明")
    emit("#       断言的是**红的形状**（指名到位），不是只断言 rc≠0（ISS-143）")
    emit("# 沙箱: /tmp/rde-*/repo（真树只读：docs+README 副本 / evidence 软链 / 判据副本）")
    emit()

    sandbox = _make_sandbox("sec4")
    try:
        # ------------------------------------------------------------ §0 基线
        emit("## [0] 沙箱基线（未变异）—— §4 必须是绿的，且三臂都活着")
        base = _eval(sandbox, 0)
        emit(f"  输出：{base['head']}")
        arms = [s for s in ("log", "json", "jsonl") if base["dist"].get(s, 0) > 0]
        base_ok = (
            base["missing"] == 0
            and not base["problems"]
            and base["declared"] >= _MIN_DECLARED
            and len(arms) == 3
        )
        add(
            "sec4-baseline-green",
            base_ok,
            f"声明 {base['declared']} 处 / 缺失 {base['missing']} / 自测红 "
            f"{len(base['problems'])} / 活着的臂 {arms}",
        )
        emit(
            f"  {'OK ' if base_ok else 'BAD'}  声明 {base['declared']} 处（下界 {_MIN_DECLARED}）"
            f" / 缺失 {base['missing']} / 自测红 {len(base['problems'])}"
        )
        # 活性：`.json` 与 `.jsonl` 臂**在真文档上**确实吃到了东西
        # （合成自测证明"设计上能命中"，这一条证明"本仓此刻真的命中" —— 两者互补）。
        json_arm = base["dist"].get("json", 0) > 0
        jsonl_arm = base["dist"].get("jsonl", 0) > 0
        add(
            "json-arm-alive-on-real-docs",
            json_arm and jsonl_arm,
            f".json={base['dist'].get('json', 0)} / .jsonl={base['dist'].get('jsonl', 0)}",
        )
        emit(
            f"  {'OK ' if json_arm and jsonl_arm else 'BAD'}  扩展后的两臂在真文档上命中："
            f".json {base['dist'].get('json', 0)} 处 / .jsonl {base['dist'].get('jsonl', 0)} 处"
        )
        emit()

        # ------------------------------------------------------------ §1 白名单退回
        emit("## [1] 白名单退回 `(\"log\",)` —— 铁律 14 本体（覆盖范围缩窄）")
        _mutate(sandbox, _WL_LINE, '_DECLARED_EVIDENCE_SUFFIXES: tuple[str, ...] = ("log",)')
        r1 = _eval(sandbox, 1)
        emit(f"  输出：{r1['head']}")
        ok1 = (
            bool(r1["problems"])
            and _has(r1["problems"], "的连写声明未被命中")
            and _has(r1["problems"], "既不在扫描白名单")
        )
        add("whitelist-narrowed-to-log", ok1, f"红 {len(r1['problems'])} 条")
        emit(f"  {'OK ' if ok1 else 'BAD'}  报红 {len(r1['problems'])} 条（期望：活性 + 完备性 两路）")
        for p in r1["problems"][:4]:
            emit(f"        | {p[:150]}")
        emit()

        # ------------------------------------------------------------ §2 少一个后缀
        emit("## [2] 白名单少一个后缀（去掉 `jsonl`）—— 细粒度缩窄也要响")
        _mutate(
            sandbox, _WL_LINE, '_DECLARED_EVIDENCE_SUFFIXES: tuple[str, ...] = ("log", "json")'
        )
        r2 = _eval(sandbox, 2)
        emit(f"  输出：{r2['head']}")
        ok2 = bool(r2["problems"]) and _has(r2["problems"], "`.jsonl`")
        add("whitelist-missing-one-suffix", ok2, f"红 {len(r2['problems'])} 条")
        emit(f"  {'OK ' if ok2 else 'BAD'}  报红 {len(r2['problems'])} 条，且指名了 `.jsonl`")
        for p in r2["problems"][:3]:
            emit(f"        | {p[:150]}")
        emit()

        # ------------------------------------------------------------ §3 结尾边界
        emit("## [3] 去掉连写正则的结尾边界 —— `x.jsonl` 会被读成 `x.json`（假红）")
        _mutate(sandbox, _END_LINE, "_EVIDENCE_SUFFIX_END = \"\"")
        r3 = _eval(sandbox, 3)
        emit(f"  输出：{r3['head']}")
        if r3["missing_paths"]:
            for mp in r3["missing_paths"][:3]:
                emit(f"        | {mp[:160]}")
        ok3 = _has(r3["problems"], "不该被连写形态命中")
        add("truncation-detected", ok3, f"截断类红 {sum('不该被连写形态命中' in p for p in r3['problems'])} 条")
        emit(
            f"  {'OK ' if ok3 else 'BAD'}  截断被自测抓到"
            f"（同时产生 {r3['missing']} 处**假红缺失**，正是要防的形态）"
        )
        emit()

        # ------------------------------------------------------------ §4 不变式②
        emit("## [4] 分写不变式 ② 放宽成 `names < 1` —— 会臆造路径")
        emit("  ⚠️ 该不变式在**当前工作树观测不到差异**（历史踩坑的那行语料已修好），")
        emit("     故断言必须打在下方的**合成负例**上，而不是真文档上。")
        _mutate(sandbox, _INV2_LINE, "    if len(dirs) != 1 or len(names) < 1:\n        return None")
        r4 = _eval(sandbox, 4)
        ok4 = _has(r4["problems"], "负例 ②")
        add("mixed-invariant-2-detected", ok4, f"红 {len(r4['problems'])} 条")
        emit(f"  {'OK ' if ok4 else 'BAD'}  负例 ② 报红（真文档仍无法观测 ⇒ 合成样本是唯一入口）")
        for p in r4["problems"]:
            if "负例 ②" in p:
                emit(f"        | {p[:170]}")
        emit()

        # ------------------------------------------------------------ §5 不变式①
        emit("## [5] 分写不变式 ① 去掉 mask —— 同行多个连写声明会交叉配对")
        _mutate(sandbox, _MASK_LINE, "    masked = line")
        r5 = _eval(sandbox, 5)
        ok5 = _has(r5["problems"], "负例 ①")
        add("mixed-invariant-1-detected", ok5, f"红 {len(r5['problems'])} 条")
        emit(f"  {'OK ' if ok5 else 'BAD'}  负例 ① 报红")
        for p in r5["problems"]:
            if "负例 ①" in p:
                emit(f"        | {p[:170]}")
        emit()

        # ------------------------------------------------------------ §6 存在性判定
        _restore(sandbox)
        emit("## [6] 副本 docs 注入一处**不存在的** `.json` 声明 —— 必须指名到路径")
        readme_copy = sandbox / "README.md"
        orig_readme = readme_copy.read_text(encoding="utf-8")
        injected = "evidence/m5/this-file-does-not-exist-20261008.json"
        readme_copy.write_text(
            orig_readme + f"\n\n<!-- 反向验证注入 -->见 {injected}\n", encoding="utf-8"
        )
        r6 = _eval(sandbox, 6)
        emit(f"  输出：{r6['head']}")
        for mp in r6["missing_paths"]:
            emit(f"        | {mp[:170]}")
        ok6 = r6["missing"] == 1 and any(injected in mp for mp in r6["missing_paths"])
        add(
            "injected-missing-json-named",
            ok6,
            f"缺失 {r6['missing']} 处 / 指名 {injected in ' '.join(r6['missing_paths'])}",
        )
        emit(f"  {'OK ' if ok6 else 'BAD'}  缺失 {r6['missing']} 处，且**指名**该路径")
        emit()

        # ------------------------------------------------------------ §7 成对证明
        emit("## [7] 成对证明：撤掉 [6] 的注入行 ⇒ 必须回绿")
        emit("  ⚠️ 只证明「注入 → 报红」还不够 —— 可能是别的巧合也红了。")
        emit("     撤掉注入后转为绿，才证明 [6] 的红**确实是那条注入**造成的。")
        readme_copy.write_text(orig_readme, encoding="utf-8")
        r7 = _eval(sandbox, 7)
        emit(f"  输出：{r7['head']}")
        ok7 = r7["missing"] == 0 and not r7["problems"]
        add("paired-proof-back-to-green", ok7, f"缺失 {r7['missing']} / 自测红 {len(r7['problems'])}")
        emit(f"  {'OK ' if ok7 else 'BAD'}  回绿：缺失 {r7['missing']} / 自测红 {len(r7['problems'])}")
        emit()

        # ------------------------------------------------------------ §8 三档单调
        emit("## [8] 白名单三档（log → +json → +jsonl）—— 扩展必须真的扩大覆盖")
        tiers = (
            ('("log",)', ("json", "jsonl"), "只认 .log"),
            ('("log", "json")', ("jsonl",), "加 .json"),
            ('("log", "json", "jsonl")', (), "加 .jsonl"),
        )
        tier_ok = True
        tier_detail: list[str] = []
        declared_series: list[int] = []
        for idx, (literal, absent, label) in enumerate(tiers):
            _mutate(
                sandbox,
                _WL_LINE,
                f"_DECLARED_EVIDENCE_SUFFIXES: tuple[str, ...] = {literal}",
            )
            res = _eval(sandbox, 100 + idx)
            declared_series.append(int(res["declared"]))  # type: ignore[arg-type]
            absent_ok = all(res["dist"].get(s, 0) == 0 for s in absent)  # type: ignore[union-attr]
            tier_ok = tier_ok and res["missing"] == 0 and absent_ok
            tier_detail.append(
                f"{label}: 声明 {res['declared']} 处 / 缺失 {res['missing']} / 分布 {res['dist']}"
            )
            emit(f"  {'OK ' if res['missing'] == 0 and absent_ok else 'BAD'}  {tier_detail[-1]}")
        monotonic = all(
            declared_series[i] < declared_series[i + 1]
            for i in range(len(declared_series) - 1)
        )
        add(
            "three-tier-monotonic",
            tier_ok and monotonic,
            f"声明数 {declared_series}（须严格递增）+ 每档 0 缺失",
        )
        emit(
            f"  {'OK ' if tier_ok and monotonic else 'BAD'}  声明数 {declared_series}"
            f"（严格递增：{monotonic}）；每档 0 缺失：{tier_ok}"
        )
        emit()

        # ------------------------------------------------------------ §9 多纳入无害
        emit("## [9] 白名单**多**一个无关注后缀（`exe`）—— 必须仍绿")
        emit("  ⚠️ 这一条反过来防「不敢扩展」：若多纳入会假红，人就会宁可留着盲区。")
        _mutate(
            sandbox,
            _WL_LINE,
            '_DECLARED_EVIDENCE_SUFFIXES: tuple[str, ...] = ("log", "json", "jsonl", "exe")',
        )
        r9 = _eval(sandbox, 9)
        emit(f"  输出：{r9['head']}")
        ok9 = r9["missing"] == 0 and not r9["problems"]
        add("extra-suffix-harmless", ok9, f"缺失 {r9['missing']} / 自测红 {len(r9['problems'])}")
        emit(f"  {'OK ' if ok9 else 'BAD'}  仍绿（缺失 {r9['missing']} / 自测红 {len(r9['problems'])}）")
        emit()

        # ------------------------------------------------------------ §10 真树
        _restore(sandbox)
        emit("## [10] 真树未被触碰（整树哈希，不是自证清白的那几个文件）")
        writes_ok, writes_detail = _own_writes_are_disjoint()
        add("own-writes-disjoint", writes_ok, writes_detail)
        emit(f"  {'OK ' if writes_ok else 'BAD'}  本工具写盘范围 vs 取证范围：{writes_detail}")
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
        own_changed = [rel for rel in changed if rel not in foreign]
        own_added = [rel for rel in added if rel not in foreign]
        own_removed = [rel for rel in removed if rel not in foreign]
        same = not own_changed and not own_added and not own_removed
        add("real-tree-untouched", same, f"{len(before)} 个文件哈希一致")
        emit(
            f"  {'OK ' if same else 'BAD'}  真树 {len(before)} 个文件哈希前后一致："
            f"{'是' if same else '否'}"
        )
        for rel in own_changed:
            emit(f"        | 内容被改：{rel}")
        for rel in own_added:
            emit(f"        | 新增文件：{rel}")
        for rel in own_removed:
            emit(f"        | 文件丢失：{rel}")
        if foreign:
            emit(
                f"  —— 另有 {len(foreign)} 个路径在窗口内变动，但**开始前就已脏**"
                f"（属并行会话，不计入本判据）："
            )
            for rel in foreign[:10]:
                emit(f"        · {rel}")
            if len(foreign) > 10:
                emit(f"        · …（共 {len(foreign)} 个）")
        emit()
    finally:
        shutil.rmtree(sandbox.parent, ignore_errors=True)

    # ------------------------------------------------------------------ 结论
    failures = [name for name, ok, _ in checks if not ok]
    #: 期望条数。⚠️ 变量名必须是 `EXPECTED_CHECKS` —— `tools/m1/check_evidence_scripts.py`
    #: 认的是 `complete = len(checks) >= EXPECTED_CHECKS` 这个**形态**（改名会被判成
    #: "没有核对条数"）。本脚本无慢用例（不连真库、不跑 pytest）⇒ 无 `--fast` 分流。
    #: 实测 13 条 = [0] 两条（基线绿 + 两臂活着）+ [1]~[9] 九条 + [10] 两条
    #: （写盘不相交 + 整树哈希）。⚠️ 首版误写 11 条，被下面那条 `exact` 当场抓住
    #: —— 正是 ISS-153 那个盲区（"用例变多了而常量没跟着涨"）的活标本。
    EXPECTED_CHECKS = 13
    complete = len(checks) >= EXPECTED_CHECKS
    #: `>=` 有一处结构性盲区（ISS-153）：拦不住"用例变多了而常量没跟着涨"。
    #: 故另加一条相等判据：条数与期望必须逐条对上，多一条也要回来改常量。
    exact = len(checks) == EXPECTED_CHECKS
    emit("## [11] 结论")
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


if __name__ == "__main__":
    raise SystemExit(main())
