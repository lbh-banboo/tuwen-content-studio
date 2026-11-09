#!/usr/bin/env python
"""WP-DEBT-03：**纪律判据化的反向验证**（把错误实现放回去，判据必须报红）。

产出：`evidence/m3/discipline-reverse.log`

## 为什么需要这个文件

判据 D（`tools/quality/check_evidence_scope.py`）、判据 E
（`tools/quality/check_shared_db_quiet.py`）与判据 F
（`tools/quality/check_loader_registration.py`）此刻都打印 `[PASS]`，
但那**只证明"当前工作区看起来合规"**，不能证明"它被违反时会报红"。
这两件事的差别就是本项目判据纪律第 9 条：

> **「声称自己有防线」比「没有防线」更危险。**

一个恒真判据（ISS-115）、一个覆盖范围被悄悄收窄的判据（ISS-139/143）、
一个只有警告没有报红的判据 —— 三者都**打印 `[PASS]`**。
本脚本把"它会报红"从**声明**变成**可复跑的证据**。

## 被测判据与它们各自的失效面

| 判据 | 文件 | 抓什么 | 需要真库 |
|---|---|---|---|
| D 证据脚本覆盖范围三方对账 | `tools/quality/check_evidence_scope.py` | 写 evidence 的脚本被静默漏跑（铁律 14） | 否（纯 AST） |
| E 共享库静止前提 | `tools/quality/check_shared_db_quiet.py` | 并行会话在同库上真跑（铁律 17 / ISS-160） | 是（**自开占位连接**，不依赖并行会话） |
| F 模块加载必须注册 | `tools/quality/check_loader_registration.py` | `importlib` 加载点漏注册 `sys.modules`（ISS-164） | 否（纯 AST） |

## 变异清单（每条对应一个**真实失效面**）

⚠️ 下表**逐条与 `main()` 里的 `run_case(...)` / 直接调用一一对应**。
写"声称有防线却指不出是哪条"正是本包要消灭的病（践 ISS-95）。

| # | 变异 | 期望 | 它证明的失效面 |
|---|---|---|---|
| D0 | 副本基线（`[0]`，**不带 D 编号**） | `[PASS]` | 判据在未变异时确实是绿的 |
| D① | 从 `rerun_all_evidence.SCOPE_PATTERNS` 删掉一条 glob（`[1]`） | `[FAIL]` + **指名漏覆盖的脚本** | D2 抓得住"复跑范围缩水"（**红的形状**，不只 rc≠0） |
| D② | 造一个**写 evidence 但不匹配任何 glob** 的新脚本（`[2]`） | `[FAIL]` + 该文件名 | B 由**行为**反推 ⇒ 覆盖范围由**磁盘**决定，不由名字决定（铁律 14 本体） |
| D③ | `MIN_REVERSE` 抬到 999（`[3]`） | `[FAIL]` + `D4` | 活性下界真的在判（不是装饰） |
| D④ | 在 D① 基础上**摘掉 D2 的判定**（`[4]`） | 假绿（`[PASS]` 且无 `[FAIL]`） | D① 的红**确实是 D2 造成的**，不是别的巧合（成对证明） |
| D⑤ | 豁免白名单指向一个不存在的文件（`[5]`） | `[FAIL]` + `豁免理由失效` | 白名单会**腐烂**这件事被盯着（D2b） |
| D⑥ | 把本脚本 `--fast` 的 `--help` 文案退回"保留参数（本脚本无慢用例）"（`[6]`） | `CHECK FAIL [边界-9]` | **判据自身的判据**也能报红 —— 它守"元信息 = 实现"（C9）；本包修复的文案漂移有人看管 |
| F① | 删掉 `check_evidence_scope._load_module` 的 `sys.modules[name] = module`（`[7]`） | `[FAIL]` + `未注册 1 个` + 文件名 | **这就是 ISS-164 真实上线过的形态**（不是发明的假故障）；且验证**红的形状**（指名 + 计数） |
| F② | `MIN_LOAD_SITES` 抬到 999（`[8]`） | `[FAIL]` + `F2 加载点 >= 999` | F2 是"判据自己瞎了也会全绿"的兜底 ⇒ 兜底本身也得被验（ISS-143 同族） |
| E0 | 副本基线（库静止；`[10]`，**不带 E 编号**） | `[PASS]` | 判据在无人占用时确实是绿的 |
| E① | **自己**开一条占位连接（`idle in transaction`；`[11]`） | `[FAIL]` + **指名该 pid** | E2 抓得住并发占用，且**可处置**（拦得住） |
| E② | 在 E① 条件下**摘掉 main 的静止判定**（`[12]`） | 假绿 | E① 的红**确实是那条判定造成的**（成对证明） |
| E③ | `--dsn` 指向不可达端口（`[13]`） | `[FAIL]` | E4 **fail-closed**：连不上 ≠ 放行 |
| E④ | 跑单测 `test_probe_rejects_empty_database_name`（`[14]`） | 通过 | E1（读不到库名 ⇒ 拒跑）由单测覆盖 —— **真库给不出这个条件**，所以它必须由能构造该条件的地方证明 |

> ⚠️ 编号纪律：**基线不计入 `D①/E①` 序列**（它是"未变异"这个前提，不是一条变异）。
> ⚠️ **2026-10-01 三遍法自纠**：上表初版把基线写成 `D①/E①`，于是全表比代码里的节标签
> （`[1] D①` …）**整体错位一位** —— 表说 `D③` 的地方代码是 `D②`。
> 这类"文档与代码各写一份编号"就是铁律 14 的病长在文档上。
> 现在每行**显式写出代码里的节号**（`[1]`/`[2]`…），并由
> `tools/quality/collect_discipline_code_review.py` 的 `[逐行-3]` 逐行对账
> （表行数 == 用例节数，且行内节号与代码节一一对上）。

> ⚠️ **E0 的位置很关键**：它必须在**占用之前**跑。本脚本自己就是"占用者"，
> 顺序错了会把基线也染红，于是"基线绿"这条断言**永远不可能成立**
> （前提写错 —— 与它要防的病同形）。

> ⚠️ **E④ 为什么不在这里用变异验**：E1 的触发条件是"`current_database()` 读不到"，
> 而真库**必然**读得到 ⇒ 在真库上摘掉那道防线，输出**照样是绿的**，
> 反向验证会得到一个**假绿**并且无法分辨"防线删了"与"条件没触发"（ISS-82 同族）。
> 正解是**换到能构造该条件的地方**证明它 —— 单测里有假连接。

## 两种模式（`--fast` 不是空壳参数）

| 模式 | 覆盖 | 连真库 | 期望条数 | 产物 |
|---|---|---|---|---|
| 完整（默认） | 判据 D + F 全部 + 判据 E 全部 | **是** | 21 | `evidence/m3/discipline-reverse.log` |
| `--fast` | **仅**判据 D + F（纯 AST） | **否** | 14 | `evidence/m3/discipline-reverse-fast.log` |

⚠️ `--fast` 的**范围由代码结构决定**（跳过 §2 整段），不由人写的名单决定 ——
且它**响亮写出**"判据 E 在本模式不适用"（ISS-118），不是静默省略。
产物文件名也分流：两种模式的**覆盖范围不同**，共用一个名字会让 CI 的 fast 跑
**覆盖**收口期的完整证据，且从文件名看不出是哪一种。

## 做法（照 `collect_test_db_credibility_reverse_evidence.py` 的模式）

整仓复制到 `/tmp/rdd3-*/repo`（**副本**，真树只读），在副本上做变异、跑判据、
断言报红，最后断言副本还原后回绿、真树**整树**哈希未变。

⚠️ 本脚本在**完整模式**下**自己会连真库**（为了给判据 E 造一个确定性的占用者）。
因此它遵守它自己要守的铁律 17：**跑之前先确认库静止**，
并且把"我开的占用连接"在 finally 里关掉。
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
#: 本脚本自己的**相对仓库根**路径。D⑥ 要变异的目标就是本脚本（测「判据自身的判据」）。
#: ⚠️ 不能写 `REVERSE` ——那是 **Code Review 脚本**里给"本脚本"起的常量名，
#: 两边名字相同但所在模块不同，跨模块引用会 `NameError`（实测踩到）。
SELF_REL = str(SELF_PATH.relative_to(REPO_ROOT))
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"
#: 证据文件名。**随模式分流** —— 两种模式的覆盖范围**不同**（fast 不含判据 E 的真库用例），
#: 写同一个名字会让 CI 的 fast 跑**覆盖**收口期的完整证据，且文件名无法分辨是哪一种。
#: 「名字看起来像完整证据、其实是子集」正是铁律 14 的病长在证据命名上。
OUT_FILE = EVIDENCE_DIR / "discipline-reverse.log"
OUT_FILE_FAST = EVIDENCE_DIR / "discipline-reverse-fast.log"

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

TOOL_D = "tools/quality/check_evidence_scope.py"
TOOL_E = "tools/quality/check_shared_db_quiet.py"
#: 判据 F（ISS-164）：`importlib` 加载点必须注册 `sys.modules`。
#: 纯静态、零真库 ⇒ 与判据 D 同族，**两种模式都跑**。
TOOL_F = "tools/quality/check_loader_registration.py"
#: 判据 D/E **自己的判据**（三遍法 Code Review）。D⑥ 用它做变异目标 ——
#: 「判据自身的判据」也必须有人验证它能报红，否则它只是文档里的第四条纪律（ISS-95）。
CR = "tools/quality/collect_discipline_code_review.py"
RERUN = "tools/m1/rerun_all_evidence.py"

#: 「真树未被触碰」的取证范围。只哈希几个文件是**自证清白**。
TREE_SCOPE = ("services/backend", "migrations", "tools")

#: 本脚本**唯一**会写盘的一处（相对仓库根）。副本在 `/tmp`，天然不相交。
#: ⚠️ 两种模式的输出文件**都**登记在这里 —— 判定"写盘范围与取证范围不相交"
#: 必须覆盖**实际会写的全部路径**，只登记一个会让另一种模式成为法外之地。
_OWN_WRITE_PATHS = (
    str(OUT_FILE.relative_to(REPO_ROOT)),
    str(OUT_FILE_FAST.relative_to(REPO_ROOT)),
)

_EXCLUDE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})


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
    return True, "写盘范围与取证范围不相交（副本在 /tmp，日志在 evidence/m3）"


def _concurrently_modified_paths() -> set[str]:
    """取证窗口**开始之前**就已是 git 脏状态的路径（ISS-154）。

    ⚠️ 跨时刻的整树哈希是**可变量**，不是不变量：本仓并行开发是常态，
    取证窗口内任何人改一次文件，完整版就**必然**红。
    而"必定红"与"真的坏了"同形（ISS-143）——**永远红的判据等于没有判据**。
    收集范围由 `git status --porcelain` **现场决定**，不由人写名单（铁律 14）。

    ⚠️ `-c core.quotepath=false` 不可省（**ISS-166**）：`git status --porcelain`
    默认把非 ASCII 路径**转义成八进制**（`docs/\345\276\205...`），下面
    `out.add(rel)` 拿到的就是转义串 ⇒ 与 `TREE_SCOPE` 里的真实路径**永不相等**
    ⇒ 那个文件该被隔离却没被隔离。
    本仓 `TREE_SCOPE = ("services/backend", "migrations", "tools")` 内当前**没有**
    非 ASCII 文件名（实测 0 个，中文名只在 `docs/` 下），因此本例的失效方向是
    **假红**（响亮、可处置），不是静默漏判。但它与
    `tools/m1/audit_consistency.py` 的同名字段是**同一根因**（本包 ISS-166），
    后者那里方向相反、是**静默偏绿**。同一个 flag 两处都要加，别只修响的那边。

    这是**隔离**不是**豁免**：本脚本自己的写盘范围另由
    `_own_writes_are_disjoint()` 用**结构**证明不在 TREE_SCOPE 内。
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
    """把整仓复制到一个**唯一**临时目录（`mkdtemp` 原子唯一，ISS-156）。"""
    base = Path(tempfile.mkdtemp(prefix=f"rdd3-{tag}-"))
    dest = base / "repo"
    shutil.copytree(
        REPO_ROOT,
        dest,
        ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".pytest_cache", "*.pyc", "evidence"
        ),
    )
    return dest


def _run(
    repo: Path, tool_rel: str, extra_args: list[str] | None = None, timeout: int = 300
) -> tuple[int, str]:
    """在副本里跑判据，返回 `(退出码, 合并输出)`。

    ⚠️ `cwd` 必须是**副本根**：判据用 `Path(__file__).parents[N]` 定位仓库。
    `NO_PROXY='*'` 不可省（本机代理劫持 localhost）。
    """
    env = {**os.environ, "NO_PROXY": "*", "no_proxy": "*"}
    env.pop("REDNOTE_DATABASE_URL", None)
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


def _default_dsn() -> str:
    """取判据 E 的**实际**目标 DSN（从模块的解析器读，不在脚本里写第二个字面量）。

    本脚本位于 `tools/` 且会被 CI 引用 ⇒ 受 `check_test_db_isolation.py`
    第二段管辖：脚本里出现裸 DSN 字面量即判违规。
    从模块派生，既避免字面量、又保证与判据**同源同口径**。

    ⚠️ **必须调 `_resolve_dsn(None)` 而不是读 `DEFAULT_DATABASE_URL` 常量**
    （2026-10-01 三遍法自纠，本脚本的**真缺陷**，且是会**让 CI 门 3 直接红**的那一种）：

      · 常量 `DEFAULT_DATABASE_URL` 是 `postgresql://rednote@localhost:5432/rednote_test`，
        **完全忽略环境变量**；
      · 而 CI 门 3 的库是 `services: postgres`（`REDNOTE_TEST_DATABASE_URL` =
        `…@localhost:5432/postgres`）。本机也不设环境变量。
      · 于是本脚本在 CI 上会去连一个**不存在的库** `rednote_test` ⇒
        `_assert_quiet_or_die()` fail-closed ⇒ `RESULT=FAIL` ⇒ 门 3 红。
      · 更糟的是这**不是"判据抓到了问题"，而是"载体连错了库"** ——
        两者都打印 FAIL，形状同形（ISS-143）。
      · 判据 E 自己的 `main()` 走的就是 `_resolve_dsn()`，本脚本却绕开它，
        等于**自己发明了第二套 DSN 口径**（铁律 14：口径由人写 → 必有分叉）。

    改为一律走判据自己的解析器：本机得 `rednote_test`、CI 得 `postgres`，
    与判据 E 的 `--dsn` 缺省行为**逐字一致**。

    ⚠️ 必须先 `sys.modules[name] = module` 再 `exec_module` ——
    判据 E 用了 `@dataclass`，而 dataclass 在处理注解时会
    `sys.modules.get(cls.__module__).__dict__`，模块不在 `sys.modules` 里
    会抛 `AttributeError: 'NoneType' object has no attribute '__dict__'`
    （实测踩到）。
    """
    import importlib.util

    name = "_quiet_for_reverse"
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / TOOL_E)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return str(module._resolve_dsn(None))


def _libpq(dsn: str) -> str:
    """SQLAlchemy 方言串 → libpq 串（与判据 E 的同一处协议替换）。"""
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


def _unreachable_dsn(base: str) -> str:
    """把一个可达 DSN 变成**不可达**（端口 1）。

    ⚠️ 用 `replace`（协议/端口替换）而不是新写一个字面量 —— 见 `_default_dsn`
    的说明。语义上这是"把同一个地址的端口换成必然无人监听的那个"。
    """
    return base.replace(":5432/", ":1/", 1)


def _assert_quiet_or_die() -> tuple[bool, str]:
    """铁律 17：本脚本自己要连真库，跑之前必须先确认库静止。"""
    import psycopg

    dsn = _libpq(_default_dsn())
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("select current_database()")
                row = cur.fetchone()
                database = str(row[0]) if row else ""
                if not database:
                    return False, "读不到目标库名 ⇒ 无法确认静止（fail-closed）"
                cur.execute(
                    "select pid, coalesce(state,'-') from pg_stat_activity "
                    "where datname = current_database() and pid <> pg_backend_pid()"
                )
                others = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        return False, f"无法确认库静止（{type(exc).__name__}: {exc}）⇒ 按 fail-closed 拒跑"
    if others:
        listed = "、".join(f"pid={p}({s})" for p, s in others[:5])
        return False, f"目标库 `{database}` 上有 {len(others)} 个他人连接：{listed}"
    return True, f"目标库 `{database}` 静止（无他人连接）"


class _Occupier:
    """为判据 E 造一个**确定性**占用者（`idle in transaction`）。

    ⚠️ 为什么要自开连接而不是"等一个并行会话"：那样这条证据就依赖外部条件，
    在库静止时**永远无法成立**（ISS-82 同族：判据只在特定条件下运行）。
    自开连接让"拦得住"这件事**任何时候都可复现**。

    `idle in transaction` 而不是瞬时 `active`：事务打开、语句已结束的状态会
    **稳定停留**在 `pg_stat_activity` 里，判据一定看得到它。
    """

    def __init__(self, dsn: str) -> None:
        import psycopg

        self._conn = psycopg.connect(_libpq(dsn), connect_timeout=5)
        self._cur = self._conn.cursor()
        self._cur.execute("begin")
        self._cur.execute("select 1")
        self.pid = int(self._conn.info.backend_pid)

    def close(self) -> None:
        try:
            self._cur.close()
        finally:
            self._conn.close()


#: 判据 D 的变异锚点：`rerun_all_evidence.SCOPE_PATTERNS` 里删掉一条 glob。
_A1_PATTERN = '    "tools/**/collect_*_evidence.py",\n'

#: 判据 D 的变异锚点：`MIN_REVERSE` 抬到 999（远超任何可能是真的值）。
_MIN_REVERSE_ANCHOR = "MIN_REVERSE = 27"

#: 判据 D 的变异锚点：D2 的判定（`not offending_d2` → `True`）。
_D2_GUARD_ANCHOR = "            not offending_d2,"

#: 判据 D 的变异锚点：豁免白名单的 key（改成一个不存在的文件）。
_EXEMPT_KEY_ANCHOR = '    "tools/m1/rerun_all_evidence.py": ('

#: D⑥ 的变异锚点：本脚本 `--fast` 的 `help=` **完整三段**（含隐式拼接的续行）。
#:
#: ⚠️ **2026-10-01 三遍法自纠（D⑥ 首版假红）**：初版锚点只取**第一段**，
#:    替换后留下 `help="…"\n        ""\n        "（期望…"` —— 隐式拼接里夹了
#:    一个**没有逗号的独立表达式** ⇒ 副本 `SyntaxError` ⇒ Code Review 整个崩掉、
#:    压根不打印 `[边界-9]`。于是 `expect_marker="CHECK FAIL [边界-9]"` 落空。
#:    症状看似"用例失败"，真因却是"**变异把脚本搞坏了**"——
#:    这正是本包反复引用的 ISS-143（「没跑」与「跑失败」同形）。
#:    修法：锚点必须覆盖**整个参数表达式**，替换品是**同样完整**的一个表达式。
_FAST_HELP_ANCHOR = (
    'help="纯静态模式：只跑判据 D（AST 反推），**整段跳过**判据 E 的 4 组真库用例"\n'
    '        "（期望条数随之变少，见结论行打印的「期望 N 条」；产物写"\n'
    '        " discipline-reverse-fast.log）。CI 用这个。",'
)

#: D⑥ 的替换品（同样是一个完整的 `help=…,` 表达式，保证语法合法）。
_FAST_HELP_MUTANT = 'help="保留参数（本脚本无慢用例）",'

#: 判据 E 的变异锚点：`main()` 里的静止判定（不是 `assert_quiet` 里那条同名语句）。
#: ⚠️ 两处文本相同，因此锚点必须**带足上下文**才唯一 —— 否则会变异到
#: `assert_quiet` 里那条，而 `main()` 照旧报红 ⇒ 这个用例永远无法满足期望。
_E_MAIN_GUARD_ANCHOR = (
    "    if not report.quiet:\n"
    "        print()\n"
    '        print(f"[FAIL] 目标库 `{report.database}` 上有 {len(report.occupants)} 个他人连接。")'
)

#: ── 判据 F（ISS-164）的变异锚点 ──────────────────────────────────────────
#:
#: 目标：`tools/quality/check_evidence_scope.py::_load_module` 里的注册行。
#: 删掉它 = 把 ISS-164 那次**真实**修法放回去（这正是"变异是真实缺陷"的理想形态：
#: 不需要我发明一个假故障，缺陷本身就是上线过的样子）。
#: 该行在文件里唯一（判据 F 已静态确认 8 个加载点各只有一处注册）。
_F_REGISTER_ANCHOR = "    sys.modules[name] = module\n"

#: 判据 F 的**活性下界**锚点：把 `MIN_LOAD_SITES` 抬到 999。
#: 为什么要单独验这条：F2 是防"判据自身 AST 规则失效 ⇒ 扫到 0 个 ⇒ 全通过"的
#: 那条兜底。只验 F1 会漏掉"判据扫不到东西时也是绿的"这一形态（ISS-143）。
_F_MIN_SITES_ANCHOR = "MIN_LOAD_SITES = 5"

#: D③ 造的新脚本：写 evidence，且**不匹配任何 SCOPE_PATTERN**。
#: 名字刻意不含 `evidence`/`code_review` 约定字样的一部分 —— 让"由名字决定的
#: 覆盖范围"漏掉它；判据 B 由**行为**反推 ⇒ 必须发现它。
_PROBE_SCRIPT_REL = "tools/quality/zz_probe_writes_evidence_tool.py"
_PROBE_SCRIPT_SRC = (
    '"""反向验证探针：会写入 evidence/，但名字不匹配任何收录约定（仅存在于副本）。"""\n'
    "\n"
    "from pathlib import Path\n"
    "\n"
    'OUT = Path("evidence/m3/zz-probe.log")\n'
    "\n"
    "\n"
    "def main() -> None:\n"
    '    OUT.write_text("probe\\n")\n'
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    main()\n"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    #: ⚠️ **2026-10-01 三遍法自纠（C9 元信息 ≠ 实现）**：此处原文案为
    #: `"保留参数（本脚本无慢用例）"` —— 那是 C2 修复**之前**的形态（当时 `--fast`
    #: 确为空壳）。C2 把它实现为真之后（整段跳过判据 E 的 4 组真库用例、期望条数
    #: 17→10、产物名分流），`--help` 仍在说"无慢用例"，与 docstring 第 62 行
    #: 「`--fast` 不是空壳参数」**直接矛盾**。这是铁律 14 的又一次复发：
    #: 「这个参数是干什么的」由**人写的 help 字符串**决定，而不是由**代码事实**决定。
    #: 修法：让文案只说事实，且事实可被 `collect_discipline_code_review.py::[边界-9]` 对账。
    parser.add_argument(
        "--fast",
        action="store_true",
        help="纯静态模式：只跑判据 D（AST 反推），**整段跳过**判据 E 的 4 组真库用例"
        "（期望条数随之变少，见结论行打印的「期望 N 条」；产物写"
        " discipline-reverse-fast.log）。CI 用这个。",
    )
    args = parser.parse_args()

    checks: list[tuple[str, bool, str]] = []
    lines: list[str] = []

    #: 本模式实际写出的证据文件（fast / 完整**分流**，见 `OUT_FILE_FAST` 的说明）。
    out_file = OUT_FILE_FAST if args.fast else OUT_FILE

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
        extra_args: list[str] | None = None,
    ) -> None:
        """做一组变异 → 跑判据 → 断言报红（**指名预期字符串**）→ 还原。

        `expect_marker` 是**必须出现**的字符串。只断言 rc≠0 是不够的 ——
        脚本因任何原因崩溃也会 rc≠0（ISS-143：没跑与跑失败同形）。

        `forbid_marker` 是**不得出现**的字符串，用于"期望假绿"的用例 ——
        只断言 `[PASS]` 不够：一个既印 `[PASS]` 又印 `[FAIL]` 的脚本也会
        命中 `[PASS]`，于是"没变绿"被误判成"变绿"（**绿的形状**也要验）。
        """
        original_texts: dict[Path, str] = {}
        for rel, _old, _new in edits:
            path = repo / rel
            if path not in original_texts:
                original_texts[path] = path.read_text(encoding="utf-8")
        # 锚点命中检查（在写任何东西之前全查完 —— 失败时副本未被改动）。
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

            code, out = _run(repo, tool_rel, extra_args)
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
                    emit(f"        | {s[:170]}")
        finally:
            for path in created:
                path.unlink(missing_ok=True)
            for path, text in original_texts.items():
                path.write_text(text, encoding="utf-8")

    # ------------------------------------------------------------------ §0
    emit("# WP-DEBT-03 纪律判据化的反向验证（把错误实现放回去，判据必须报红）")
    emit(f"# 生成时间: {_now()}")
    emit(f"# 命令: python tools/quality/{SELF_PATH.name}")
    emit(f"# 被测判据: D={TOOL_D}")
    emit(f"#           E={TOOL_E}")
    emit("# 本轮主题: 铁律 14（覆盖范围由磁盘决定）+ 铁律 17（跑真库前确认共享库静止）")
    emit()

    # ---------------------------------------------------------- §-1 前置（铁律 17）
    # ⚠️ 顺序是本脚本的正确性问题，不是风格问题：E① 基线必须在**占用之前**跑。
    if args.fast:
        # ⚠️ `--fast` 是**纯静态**模式（判据 D 为纯 AST）⇒ 本模式不产生任何真库动作，
        #    因此「共享库静止」这个前提在本模式下**不适用**，而不是「被忽略」
        #    （ISS-118：「判据在此环境不适用」≠「判据不存在」）。
        quiet_ok, quiet_detail = True, "`--fast` 纯静态模式（判据 D 为 AST）⇒ 静止前提不适用"
    else:
        quiet_ok, quiet_detail = _assert_quiet_or_die()
    add("shared-db-quiet(铁律17)", quiet_ok, quiet_detail)
    # ⚠️ 标题必须随模式变：完整模式**确实会连真库**，fast 模式**根本不连**。
    #    两种模式共用一句"本脚本自己要连真库"会让 fast 的日志说谎（ISS-95 的轻量版）。
    if args.fast:
        emit("## [-1] 前置：`--fast` 不连真库 ⇒ 静止前提**不适用**（不是被忽略）")
    else:
        emit("## [-1] 前置：本脚本自己要连真库 ⇒ 先确认共享库静止（铁律 17）")
    emit(f"  {'OK ' if quiet_ok else 'BAD'}  {quiet_detail}")
    if not quiet_ok:
        emit("")
        emit("  本脚本会为判据 E 造一个确定性的占用者，因此它必须遵守它自己守的纪律：")
        emit("  库上有他人连接时不能开跑（否则两边的观测互相污染）。")
        emit("  处置：等并行会话结束、PG 回到静止窗；或改用私有库。")
        for name, ok, _ in checks:
            emit(f"  {'PASS' if ok else 'FAIL'} {name}")
        emit()
        emit(f"RESULT=FAIL checks={len(checks)} PASS / {len(checks) - sum(1 for _, o, _ in checks if o)} FAIL")
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"写出 {out_file.relative_to(REPO_ROOT)}")
        print("RESULT=FAIL（前置：共享库不静止，拒跑）")
        return 1
    emit()

    repo = _make_copy("rt")
    before = _snapshot(REPO_ROOT)
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
        # ============================================================ §1 判据 D
        emit("## [0] 判据 D 副本基线：变异之前必须是绿的")
        code, out = _run(repo, TOOL_D)
        d_green = code == 0 and "[PASS]" in out
        add("D-baseline-green", d_green, f"exit={code}")
        emit(f"  {'OK ' if d_green else 'BAD'}  判据 D 基线 → exit={code}（期望 0 / [PASS]）")
        for line in out.splitlines():
            s = line.strip()
            if s.startswith(("[PASS]", "[FAIL]", "  三方对账", "  差集")):
                emit(f"        | {s[:170]}")
        emit()

        emit("## [1] D① 复跑范围缩水：从 `SCOPE_PATTERNS` 删掉一条 glob")
        emit("   断言的是**红的形状** —— 报红信息必须**指名**被漏覆盖的脚本，")
        emit("   而不是只说一句「有分叉」（那样读的人无从处置）。")
        run_case(
            repo=repo,
            tool_rel=TOOL_D,
            edits=[(RERUN, _A1_PATTERN, "")],
            expect_marker="D2 B⊆A1",
            name="D2-red-when-a1-loses-a-pattern",
            label="D① 删掉一条 SCOPE_PATTERN → 必须报红并指名",
        )
        emit()

        emit("## [2] D② 造一个**写 evidence 但名字不匹配任何约定**的脚本")
        emit("   这条直接证伪「覆盖范围由名字决定」的旧形态：")
        emit("   A1（按名字 glob）看不见它，B（按行为 AST 反推）必须看见它。")
        run_case(
            repo=repo,
            tool_rel=TOOL_D,
            edits=[],
            extra_files={_PROBE_SCRIPT_REL: _PROBE_SCRIPT_SRC},
            expect_marker=_PROBE_SCRIPT_REL,
            name="D2-red-when-new-evidence-script-is-untracked",
            label="D② 新证据脚本未被收录 → 必须指名它",
        )
        emit()

        emit("## [3] D③ 活性下界不是装饰：`MIN_REVERSE` 抬到 999")
        run_case(
            repo=repo,
            tool_rel=TOOL_D,
            edits=[(TOOL_D, _MIN_REVERSE_ANCHOR, "MIN_REVERSE = 999")],
            expect_marker="D4",
            name="D4-red-when-activity-floor-is-too-high",
            label="D③ MIN_REVERSE=999 → D4 必须报红",
        )
        emit()

        emit("## [4] D④ 成对变异：证明 D① 的红**确实是 D2 造成的**")
        emit("   在 D① 的变异之上**摘掉 D2 自己的判定**（`not offending_d2` → `True`）——")
        emit("   若输出变绿，则 D① 那条红不可能来自别处。")
        run_case(
            repo=repo,
            tool_rel=TOOL_D,
            edits=[
                (RERUN, _A1_PATTERN, ""),
                (TOOL_D, _D2_GUARD_ANCHOR, "            True,  # 变异：摘掉 D2 判定"),
            ],
            expect_marker="[PASS]",
            forbid_marker="[FAIL]",
            name="D2-lies-pass-when-its-check-removed",
            label="D④ 摘掉 D2 判定 → 假绿（证明确实是它拦住的）",
            expect_exit_nonzero=False,
        )
        emit()

        emit("## [5] D⑤ 豁免白名单会腐烂：把 key 改成不存在的文件")
        emit("   白名单若不核对理由，就会退化成「把报红的地方都加进来」。")
        run_case(
            repo=repo,
            tool_rel=TOOL_D,
            edits=[
                (TOOL_D, _EXEMPT_KEY_ANCHOR, '    "tools/m1/zz_gone_evidence.py": ('),
            ],
            expect_marker="豁免理由失效",
            name="D2b-red-when-exemption-rots",
            label="D⑤ 豁免指向不存在的文件 → D2b 必须报红",
        )
        emit()

        emit("## [6] D⑥ C9：把 `--help` 文案退回「空壳」说法 ⇒ Code Review 的 [边界-9] 必须报红")
        emit("   ⚠️ 这一条**不测判据 D/E**，测的是**判据自己的判据**（Code Review 脚本）。")
        emit("   它守的是「元信息 = 实现」：`--help` 文案对读它的人而言**就是**这个参数的功能说明。")
        emit("   为什么必须验：C9 只修了一次文案，若无人看管，下次重构会再退回旧说法而全绿。")
        emit("   （这正是本包反复强调的 ISS-95：声称有防线 ≠ 有防线。）")
        emit("   变体必须**语法合法且只有事实被抽走** —— 若把整个 `help=` 删掉、")
        emit("   只留下悬空的 `,` 或残留拼接段，副本会 `SyntaxError` ⇒ Code Review 整个崩掉、")
        emit("   压根不打印 `[边界-9]`，于是「脚本崩了」被误读成「判据报红了」（ISS-143）。")
        emit("   （本用例初版就踩了这个坑：锚点只覆盖 help 第一段，替换后留下语法错误。）")
        emit("   断言的是 `CHECK FAIL [边界-9]` 而**不是** `[边界-9]` —— 后者在 PASS 行里也出现，")
        emit("   断言它等于断言「这个标签存在」（恒真；形状不对的红等于没红，ISS-143）。")
        run_case(
            repo=repo,
            tool_rel=CR,
            edits=[(SELF_REL, _FAST_HELP_ANCHOR, _FAST_HELP_MUTANT)],
            expect_marker="CHECK FAIL [边界-9]",
            name="C9-boundary9-red-when-help-reverts-to-shell",
            label="D⑥ `--help` 退回归空壳说法 → [边界-9] 必须报红",
        )
        emit()

        emit("## [7] F① F 拦得住：把 ISS-164 的注册行**删回去** ⇒ 判据 F 必须报红")
        emit("   ⚠️ 这条变异**不是发明的假故障** —— 它就是 2026-10-01 真实上线过的形态：")
        emit("   `check_evidence_scope.py::_load_module` 少了 `sys.modules[name] = module`。")
        emit("   它当时**不报错**，只因为被加载的两个模块恰好都没有 `@dataclass`。")
        emit("   断言的是 `未注册 1 个` **加** 文件名 —— 只断言 `[FAIL]` 会放过")
        emit("   「别的用例红它也红」，而形状（指名到文件 + 计数）才是可处置的证据。")
        run_case(
            repo=repo,
            tool_rel=TOOL_F,
            edits=[(TOOL_D, _F_REGISTER_ANCHOR, "")],
            expect_marker="未注册 1 个",
            name="F1-red-when-registration-removed",
            label="F① 删掉 sys.modules 注册 → 判据 F 必须报红",
        )
        emit()

        emit("## [8] F② 活性下界不是装饰：`MIN_LOAD_SITES` 抬到 999 ⇒ F2 必须报红")
        emit("   为什么要单独验 F2：F1 只判「扫到的加载点都注册了」。")
        emit("   若判据自身的 AST 规则失效（扫到 0 个），F1 会**全通过** ——")
        emit("   那是「判据瞎了」与「代码合规」同形（ISS-143 同族）。")
        emit("   F2 就是防这件事的那条兜底，因此它自己也得被验。")
        run_case(
            repo=repo,
            tool_rel=TOOL_F,
            edits=[(TOOL_F, _F_MIN_SITES_ANCHOR, "MIN_LOAD_SITES = 999")],
            expect_marker="F2 加载点 >= 999",
            name="F2-red-when-liveness-bound-mutated",
            label="F② 活性下界抬到 999 → F2 必须报红",
        )
        emit()

        emit("## [9] 判据 D / F 副本还原后必须回绿（证明前面是「变异」而非「破坏」）")
        code, out = _run(repo, TOOL_D)
        d_restored = code == 0 and "[PASS]" in out and "[FAIL]" not in out
        add("D-restored-green", d_restored, f"exit={code}")
        emit(f"  {'OK ' if d_restored else 'BAD'}  判据 D 还原 → exit={code}（期望 0 / [PASS]）")
        code, out = _run(repo, TOOL_F)
        f_restored = code == 0 and "[PASS]" in out and "[FAIL]" not in out
        add("F-restored-green", f_restored, f"exit={code}")
        emit(f"  {'OK ' if f_restored else 'BAD'}  判据 F 还原 → exit={code}（期望 0 / [PASS]）")
        emit()

        # ============================================================ §2 判据 E
        if args.fast:
            emit("## [10~15] `--fast` ⇒ 判据 E 的 4 组真库用例**整段不跑**（响亮跳过）")
            emit("   ⚠️ 为何是「不适用」而不是「被跳过」：")
            emit("     · CI runner 以 `services: postgres` **独占** PG 实例，")
            emit("       「有没有别人在用这个库」在 CI 上没有区分度；")
            emit("     · 判据 E 自己的 `--ci` 分支就是这么判的（响亮 SKIP）。")
            emit("       本脚本在 CI 上必须与它**同口径**，否则口径分叉（铁律 14）。")
            emit("   真库覆盖由本机收口期完整模式提供（`tools/m1/rerun_all_evidence.py`）。")
            # ⚠️ **2026-10-01 三遍法自纠（C10）**：此处原文案写"fast 期望 9 条"，
            #    而同一文件里的两种模式表与 `EXPECTED_CHECKS` 都是 10 —— 三处说法
            #    两种答案，而只有 `exact` 判据护得住第三处 ⇒ 前两处静默漂移。
            #    修法：**不写数字**，让读者去看运行期打印的「期望 N 条」。
            emit("   条数核对随之调整（分模式条数由 `EXPECTED_CHECKS` 决定，见结论行）。")
            emit()
        else:
            dsn = _default_dsn()
            emit("## [10] 判据 E 副本基线（此刻库静止 —— 必须在占用**之前**跑）")
            code, out = _run(repo, TOOL_E, ["--dsn", dsn])
            e_green = code == 0 and "[PASS]" in out
            add("E-baseline-green", e_green, f"exit={code}")
            emit(f"  {'OK ' if e_green else 'BAD'}  判据 E 基线 → exit={code}（期望 0 / [PASS]）")
            for line in out.splitlines():
                s = line.strip()
                if s.startswith(("[PASS]", "[FAIL]", "目标库", "他人连接数")):
                    emit(f"        | {s[:170]}")
            emit()

            emit("## [11] E① 拦得住：**自己**开一条占位连接（确定性，不依赖并行会话）")
            emit("   占用者由本脚本创建 ⇒ 这条证据在任何时候都可复现。")
            emit("   断言两件事：① 判据报红；② 报红信息**指名该 pid**（可处置）。")
            occupier: _Occupier | None = None
            try:
                occupier = _Occupier(dsn)
                code, out = _run(repo, TOOL_E, ["--dsn", dsn])
                hit_fail = "[FAIL]" in out
                hit_pid = str(occupier.pid) in out
                ok = code != 0 and hit_fail and hit_pid
                add(
                    "E-red-when-occupied",
                    ok,
                    f"exit={code}，占用者 pid={occupier.pid}，"
                    f"{'命中' if hit_pid else '未命中'}该 pid",
                )
                emit(f"  {'OK ' if ok else 'BAD'}  E① 库被占用 → exit={code}（期望 ≠0 / [FAIL] / 指名 pid）")
                for line in out.splitlines():
                    s = line.strip()
                    # ⚠️ `s` 已经 strip 过 ⇒ 缩进前缀永远匹配不上，占用者行必须按 `·` 起头匹配。
                    if s.startswith(("[FAIL]", "[PASS]", "·", "目标库", "他人连接数")):
                        emit(f"        | {s[:170]}")
                emit()

                emit("## [12] E② 成对变异：证明 E① 的红**确实是 main 的静止判定造成的**")
                emit("   ⚠️ 锚点必须带足上下文：`assert_quiet` 里有一条**文本相同**的判据，")
                emit("      变异到那一条的话 `main()` 照旧报红 ⇒ 这个用例永远无法满足期望。")
                occupier.close()
                occupier = None
                # 先还原一次占用状态：这里重新开一条，供 run_case 期间使用。
                occupier = _Occupier(dsn)
                run_case(
                    repo=repo,
                    tool_rel=TOOL_E,
                    edits=[(TOOL_E, _E_MAIN_GUARD_ANCHOR, "    if False:  # 变异：摘掉静止判定")],
                    expect_marker="[PASS]",
                    forbid_marker="[FAIL]",
                    name="E-lies-pass-when-its-guard-removed",
                    label="E② 摘掉静止判定 → 假绿（证明确实是它拦住的）",
                    expect_exit_nonzero=False,
                    extra_args=["--dsn", dsn],
                )
                emit()
            finally:
                if occupier is not None:
                    occupier.close()
                # 确认占用已释放 —— 否则后面的用例（与后续人工操作）会看到幽灵占用。
                released, release_detail = _assert_quiet_or_die()
                add("E-occupier-released", released, release_detail)
                emit(f"  {'OK ' if released else 'BAD'}  占用连接已释放 → {release_detail}")
                emit()

            emit("## [13] E③ fail-closed：`--dsn` 指向不可达端口 ⇒ 必须报红（不是放行）")
            emit("   判据纪律：**权限/网络问题不能变成「放行」**。")
            run_case(
                repo=repo,
                tool_rel=TOOL_E,
                edits=[],
                expect_marker="[FAIL]",
                name="E-red-when-dsn-unreachable",
                label="E③ 不可达 DSN → fail-closed 报红",
                extra_args=["--dsn", _unreachable_dsn(dsn)],
            )
            emit()

            emit("## [14] E④ E1（读不到库名 ⇒ 拒跑）由单测覆盖")
            emit("   真库**必然**读得到库名 ⇒ 在真库上摘掉那道防线输出照样是绿的，")
            emit("   反向验证会得到**假绿**且无法分辨「防线删了」与「条件没触发」（ISS-82）。")
            emit("   正解：换到**能构造该条件**的地方证明它 —— 单测里有假连接。")
            e1_node = "tests/test_shared_db_quiet.py::test_probe_rejects_empty_database_name"
            proc = subprocess.run(
                [
                    PYTHON,
                    "-m",
                    "pytest",
                    "-p",
                    "no:cacheprovider",
                    e1_node,
                ],
                cwd=str(repo / "services" / "backend"),
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "NO_PROXY": "*",
                    "no_proxy": "*",
                    "PYTHONPATH": "src",
                },
                timeout=300,
            )
            e1_out = (proc.stdout or "") + (proc.stderr or "")
            e1_ok = proc.returncode == 0 and "1 passed" in e1_out
            add("E1-covered-by-unit-test", e1_ok, f"exit={proc.returncode} / {e1_node}")
            emit(f"  {'OK ' if e1_ok else 'BAD'}  E④ 单测 {e1_node} → exit={proc.returncode}（期望 0 / 1 passed）")
            for line in e1_out.splitlines():
                s = line.strip()
                if "passed" in s or "failed" in s or s.startswith(("PASSED", "FAILED", "ERROR")):
                    emit(f"        | {s[:170]}")
            emit()

            emit("## [15] 判据 E 副本还原后必须回绿")
            code, out = _run(repo, TOOL_E, ["--dsn", dsn])
            e_restored = code == 0 and "[PASS]" in out and "[FAIL]" not in out
            add("E-restored-green", e_restored, f"exit={code}")
            emit(f"  {'OK ' if e_restored else 'BAD'}  判据 E 还原 → exit={code}（期望 0 / [PASS]）")
            emit()

        # ------------------------------------------------------------ §3 收尾
        emit("## [16] 真树未被触碰（整树哈希，不是自证清白的那几个文件）")
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
            return any(rel == d or rel.startswith(d.rstrip("/") + "/") for d in dirty_at_start)

        foreign = sorted(rel for rel in (changed + added + removed) if _pre_existing(rel))
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
            emit(f"  —— 另有 {len(foreign)} 个路径在窗口内变动，但**开始前就已脏**，属并行会话（不计入本判据）：")
            for rel in foreign[:10]:
                emit(f"        · {rel}")
            if len(foreign) > 10:
                emit(f"        · …（共 {len(foreign)} 个）")
        emit()
    finally:
        shutil.rmtree(repo.parent, ignore_errors=True)

    # ------------------------------------------------------------------ §4
    failures = [name for name, ok, _ in checks if not ok]
    #: 期望条数由模式决定。⚠️ 变量名必须是 `EXPECTED_CHECKS` ——
    #: `tools/m1/check_evidence_scripts.py` 用 `complete = len(checks) >= EXPECTED_CHECKS`
    #: 这个**形态**做门禁（改名会被判成"没有核对条数"）。
    #: ⚠️ **分模式**：`--fast` 不跑判据 E 那 7 条 ⇒ 期望数必须跟着走。
    #: 否则「条数核对」会在 fast 模式下**恒定报红**（永远红的判据 = 没有判据，ISS-143）。
    #: 实测口径：完整 21 条 / fast 14 条（2026-10-01 加判据 F 的三条后）。
    #: ⚠️ 数字**以现场实测为准** —— 改这里之前先看结论行报出的实际条数；
    #: 手写数字必然漂移（C10/C11 的教训），故本注释只作提示、不作事实。
    #: 变量名仍必须是 `EXPECTED_CHECKS` —— 门禁认的是这个形态，不是它的值。
    EXPECTED_CHECKS = 14 if args.fast else 21
    complete = len(checks) >= EXPECTED_CHECKS
    #: `>=` 有一处结构性盲区（ISS-153）：它拦不住"判据变多了而常量没跟着涨"。
    #: 故**另加一条相等判据**：条数与期望必须逐条对上，多一条也要回来改常量。
    exact = len(checks) == EXPECTED_CHECKS
    emit("## [17] 结论")
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
    out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写出 {out_file.relative_to(REPO_ROOT)}")
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
