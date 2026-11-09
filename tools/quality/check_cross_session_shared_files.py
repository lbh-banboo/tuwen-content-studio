#!/usr/bin/env python
"""跨会话共享文件的**写权边界**判据（WP-DEBT-04 后半 / V2.1 §7.1）。

## 这条判据在守什么

`outputs/Rednote研发执行计划_V2.1_2026-10-08.md` **§7.1** 写了四条"并发开发线纪律"，
其中一条是**共享文件归属**（`docs/m4/M4并行开发接缝冻结.md` §7.1 把它落成 8 处清单）：

> **跨会话共享的文件由主 agent 独占写**；分支只写自己模块。

V2.1 §10.3 把这条的判据化立为 **WP-DEBT-04**，并写明：
> §7.1 的四条纪律**目前全是自觉，没有一条判据** —— 这等价于
> **ISS-95 形态**（声称有防线）。**在 M4 并行线开工之前把判据做出来**。

> ⚠️ **引文的一处改写（2026-10-11，如实登记）**：V2.1 原文此处引用的那个「纪律编号」
> 写的是一个**仓库内并无定义**的号（见 `docs/判据纪律-编号口径.md` §2.3）——
> 本脚本初版照抄了它，**当场被 `audit_consistency.py` §8 的双向对账判据报红**
> （"被引用 1 处但既无定义、也未登记进台账"）。
> 按该文件 §4 的处置纪律（**严禁靠推断改编号**：没有权威序能证明它应当等价于哪一条），
> 此处按 §5 关闭条件推荐的**语义锚点**形态改写 —— 不写数字，直接指 ISS-95 这条根因。
>
> ★ **这处改写过程本身就是一条证据，且它自证了两次**：
> ① `audit_consistency.py` §8（ISS-173 的产物）在**新代码落地的当下**就拦住了作者的旧习惯，
> 而不是等到某次收口复核 —— 判据有活性，不是文档里的一句声明；
> ② 连**这段"解释那个编号没有定义"的注记本身**也会被 §8 计入一次引用
> （初稿在这里又写了一遍那个数字，复跑当场再次报红）—— **引用即计数，解释不豁免**。

（另一半"同名库有活跃连接则拒跑"= `tools/quality/check_shared_db_quiet.py`，已交付。）

## ⚠️ 本判据**刻意不做**的两件事（都是从 ISS-207 / ISS-217 的实证教训来的）

### ① 不做"工作树静止"断言

ISS-217（`collect_schema_parity_reverse_evidence.py` 的 `real-tree-untouched`）实测：
共享工作树里**整树前后比对**测的不是"我没写坏"，而是"这段时间全仓没人写任何东西"。
并行会话一改盘就报红，而红出来的形状与本工具真写坏了**完全同形**（最严重文案）。
⇒ 本判据**不比对整树**、**不要求静止**。

### ② 不把"谁改的"当断言

判据对"这个改动是谁写的"**没有判据力** —— 共享工作树里两个会话的改动在
`git status` 上完全同形（同一个 `M`、同一个 mtime 区间）。
⇒ 本判据把"归属信息"作为**输出**（供人判断），而不是 FAIL 的理由。
这正是 ISS-217 已定的候选 C（"点名变更文件清单 + 各文件最后提交时刻"）。

## 判据 G 的做法

对"**本次要提交的路径集合**"（入参：路径参数，或 `--staged` 取暂存区）
做四项判定：

| # | 判据 | 含义 |
|---|---|---|
| G1 | 共享清单**从文档派生**且非空（+G1b 标题条数与表行数双向对账） | 铁律 14：覆盖范围不能由人手写在脚本里；文档缺表/删条时报红 |
| G2 | 提交集**未触碰**任何共享文件 | 主判据。触碰即 FAIL，并点名是哪一条（8 处里的第几处） |
| G3 | 提交集**非空**且路径都真实存在 | 活性下界：空集时"没触碰共享文件"是恒真（ISS-143 同族） |
| G4 | `--main-agent` **不得空转** | 声明了主 agent 却一处都没放行 ⇒ 该开关在掩盖"覆盖范围已变化" |

> G1 的"从文档派生"是**必须**的：若把 8 处清单抄进本脚本，就等于制造
> "同一份事实被两处声称而只有一处被对账"（ISS-85 族）—— 文档改了脚本不知道。
> 派生失败（表被删 / 格式改了）必须报红，不能静默当成"没有共享文件"。
>
> G2 的"主 agent 放行"（`--main-agent`）也是**必须**的：§7.1 表里的
> `禁改` / `禁建` 是**对分支说的**，而 §7.2 的收口顺序里共享文件本就由主 agent
> 串行改。一个把主 agent 的合规提交判成违规的判据 = 假红，而假红会逼人削弱判据
> （判据纪律第 8 条）。但放行必须**逐条出声**，否则开关 = 后门（ISS-95 的开关形态）。

跑法：

    # 检查将要提交的一批路径（显式列出，与 `git commit -- <paths>` 同形）
    python tools/quality/check_cross_session_shared_files.py \\
        --paths README.md docs/待定项与问题登记册.md

    # 分支/非主 agent 的提交：触碰共享文件即报红（默认，无需开关）
    python tools/quality/check_cross_session_shared_files.py --staged

    # 主 agent 串行收口：显式声明身份，共享文件逐条出声放行
    python tools/quality/check_cross_session_shared_files.py --staged --main-agent

    # 只打印共享清单（供人核对文档表）
    python tools/quality/check_cross_session_shared_files.py --list

    # CI：无入参 ⇒ 显式 SKIP（本判据的实际载体是收口期的提交前步骤）
    python tools/quality/check_cross_session_shared_files.py --ci

作为库使用：

    from check_cross_session_shared_files import shared_files, classify
    hits = classify(["README.md"])          # → 命中的共享条目
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 共享清单的**权威出处**（派生源，不在此处抄第二份）。
#: ⚠️ 这是"唯一声称方"⇒ 若该表被删/改名，本判据必须报红（见 G1）。
SEAM_DOC = "docs/m4/M4并行开发接缝冻结.md"

#: 表头所在的小节标题（用于定位，避免扫到别的 8 行表）。
_SEAM_HEADING = "共享文件归属"

#: 标题里的**条数声称**：`…（8 处，全归主 agent）` ⇒ 抓出那个数字。
#: ⚠️ 这是本判据的**第二处声称**（第一处是表的实际行数）。
#: 来历：变异实测（2026-10-11）发现——**只读表**时，把表里一行删掉
#: 不会报红（清单从 8 静默变 7，G2 照旧 PASS）⇒ 判据对"清单被删条"
#: 完全失明。这正是"清单由人手写"的经典失效形态（铁律 14）。
#: 补法：标题里的「N 处」与表的实际行数**双向对账** ⇒ 删行后
#: 7 ≠ 标题的 8 ⇒ 报红（前提是改表的人没同时改标题；两处一起改是
#: 本判据如实登记的剩余边界，与 §7.1 表的性质一致）。
_SEAM_COUNT_RE = re.compile(r"（\s*(\d+)\s*处")

#: 表格行：`| 3 | `path/glob` | 权限 |`
_SEAM_ROW_RE = re.compile(r"^\|\s*\d+\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|\s*$")

#: G3 活性下界：一次提交至少要有个数（空集会让 G2 恒真）。
MIN_PATHS = 1

#: 主 agent 收口提交的**显式声明**开关。
#:
#: ⚠️ **为什么必须有它**（而不是让判据一律拒绝共享文件）：
#:   §7.1 表的权限列（`禁改` / `禁建`）是**对分支说的**；而 V2.1 §7.2 的收口顺序里，
#:   共享文件（`main.py` / `openapi.json` / 登记册 / `README.md` / `evidence/**`）
#:   **本来就由主 agent 串行改**。一个把"主 agent 的合规提交"判成违规的判据
#:   = **假红**，而假红会逼人削弱判据（判据纪律第 8 条）—— 那比没有判据更坏。
#:
#: ⚠️ **但豁免不许静默**：生效时逐条打印被放行的共享文件，且输出里必须出现
#:   「主 agent」字样。一个**恒被跳过**的开关等于没有开关（ISS-95 的开关形态）——
#:   所以"出声"本身由反向验证的 R⑧ 钉住（它断言的是"打印了"，不是"没报错"）。
#:
#: ⚠️ **谁在用这个开关，判据无法机械判定** —— 共享工作树里两个会话的改动在
#:   `git status` 上完全同形（ISS-207 / ISS-217 已定：**不把"谁改的"当断言**）。
#:   这是本判据**如实登记的剩余边界**：它守的是"是否**声明**了主 agent 身份"，
#:   不是"这个声明是否为真"。把这个边界写成"有防线"才是 ISS-95。
MAIN_AGENT_FLAG = "--main-agent"


class SharedFileTouched(RuntimeError):
    """提交集触碰了共享文件 —— 按 §7.1 应由主 agent 独占写。"""


def load_shared_patterns() -> list[tuple[str, str]]:
    """G1：从接缝冻结文档派生出共享文件清单 `[(glob, 权限), ...]`。

    ⚠️ 派生必须**定位到正确小节**再读表：整份文档里有多张表，
    只用行正则扫会连"只读接缝表""分区边界表"一起收进来（实测会多收 10 行）。

    失败（文件不存在 / 找不到小节 / 表为空）⇒ 抛，**不返回空列表**
    —— 返回空会让 G2 变成"什么都没触碰"的恒真式（ISS-143 家族）。
    """
    doc = REPO_ROOT / SEAM_DOC
    if not doc.is_file():
        raise SharedFileTouched(
            f"共享清单的权威文档不存在：{SEAM_DOC} —— 判据无法派生清单；"
            f"按铁律 14，覆盖范围必须由磁盘决定，缺源即报红（不得静默放行）"
        )
    lines = doc.read_text(encoding="utf-8").splitlines()
    start = None
    heading_line = ""
    for idx, line in enumerate(lines):
        if _SEAM_HEADING in line and line.lstrip().startswith("#"):
            start = idx
            heading_line = line
            break
    if start is None:
        raise SharedFileTouched(
            f"{SEAM_DOC} 里找不到含「{_SEAM_HEADING}」的标题 —— "
            f"清单派生失败；文档改了结构而脚本不知道，正是 ISS-85 族"
        )
    out: list[tuple[str, str]] = []
    for line in lines[start + 1 :]:
        # 表格连续段结束（遇到新的标题或空行后的非表行）就停
        if line.lstrip().startswith("#") and _SEAM_HEADING not in line:
            break
        m = _SEAM_ROW_RE.match(line)
        if m:
            out.append((m.group(1).strip(), m.group(2).strip()))
    if not out:
        raise SharedFileTouched(
            f"{SEAM_DOC} 的「{_SEAM_HEADING}」小节里一行表都没解析到 —— "
            f"要么表被删了、要么行格式改了；解析不到时『无命中』是假绿"
        )

    # ---- G1b：标题的「N 处」与表的实际行数**双向对账** ------------------
    # ⚠️ 变异实测（2026-10-11）：**只读表**时删掉一行不报红
    #    （清单 8→7 静默放行，G2 照旧 PASS）⇒ 对"清单被删条"完全失明。
    #    标题自带「（8 处，…）」这**第二处声称**；两者不等即报红。
    claimed = _SEAM_COUNT_RE.search(heading_line)
    if claimed:
        want = int(claimed.group(1))
        if want != len(out):
            raise SharedFileTouched(
                f"{SEAM_DOC} 的标题声称「{want} 处共享文件」，而表里实际有 "
                f"{len(out)} 行 —— 清单被删条/加条而标题没跟上（或反之）。"
                f"本判据只读表时这是**静默放行**的（实测：删一行后清单 8→7 "
                f"而 G2 仍 PASS）⇒ 必须由标题的条数声称兜住"
            )
    return out


def _matches(path: str, pattern: str) -> bool:
    """`path` 是否命中 glob `pattern`。

    `**` 用 `fnmatch` 不跨 `/` ⇒ 对 `evidence/**/*.log` 这类模式要
    同时试"整串 fnmatch"与"按前缀段逐个试"。这里用**逐段前缀**判定，
    保证 `evidence/m1/x.log` 命中 `evidence/**/*.log`（`**` 视作任意层数）。
    """
    if pattern.endswith("/**"):
        base = pattern[: -len("/**")]
        return path == base or path.startswith(base + "/")
    if "/**/" in pattern:
        prefix, _, suffix = pattern.partition("/**/")
        if not (path == prefix or path.startswith(prefix + "/")):
            return False
        rest = path[len(prefix) + 1 :]
        return fnmatch.fnmatch(rest, suffix) or fnmatch.fnmatch(rest, "*" + suffix)
    return fnmatch.fnmatch(path, pattern)


def _norm(path: str) -> str:
    """把路径归一成**仓库相对形态**，供 glob 匹配与存在性判定共用。

    ⚠️ **不许用 `str.lstrip("./")`** —— `lstrip` 的参数是**字符集合**，
    它会把 `.github/ci.yml` 的前导 `.` 一并剥成 `github/ci.yml`
    （也把 `./a` → `a`、但那是巧合，不是语义）。实测症状：主 agent 收口提交
    含 `.github/workflows/ci.yml` 时，G3 报「路径在磁盘上不存在」
    —— 一个**每条路径都真实存在**的提交被自己的工具判红（假红，且形状
    像"shell 展开失败"，会把读的人引向错误方向）。
    正确做法：只剥**前缀** `./`（`removeprefix` 是前缀语义，不是字符集）。
    """
    norm = path.strip()
    while norm.startswith("./"):
        norm = norm[len("./"):]
    return norm


def classify(paths: list[str]) -> list[tuple[str, str, str]]:
    """返回 `[(提交路径, 命中的共享模式, 该模式的权限说明), ...]`。"""
    patterns = load_shared_patterns()
    hits: list[tuple[str, str, str]] = []
    for raw in paths:
        norm = _norm(raw)
        if not norm:
            continue
        for pattern, perm in patterns:
            if _matches(norm, pattern):
                hits.append((norm, pattern, perm))
                break
    return hits


def _staged_paths() -> list[str]:
    """暂存区路径（`git diff --cached --name-only`）。

    ⚠️ 用 `-c core.quotepath=false`：中文路径默认会被 git 转义成
    `"\\346\\226\\207..."`，那样 `_matches` 永远匹配不上（静默漏判）。
    """
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", "diff", "--cached", "--name-only"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]


def main() -> int:
    argv = sys.argv[1:]

    if "--ci" in argv:
        # 本判据的实际载体是收口期的"提交前检查"（见 tools/m1/rerun_all_evidence.py 的收口清单）。
        # CI 上无暂存集 ⇒ 没有区分度 ⇒ 响亮 SKIP，而不是静默通过。
        print("跨会话共享文件写权边界（WP-DEBT-04 / V2.1 §7.1）")
        print("  [SKIP] 本判据在 CI 上不适用：CI 无『本次要提交的路径集合』这一输入 ⇒")
        print("         无区分度 ⇒ 显式跳过，而不是静默通过。")
        print("         实际载体 = 收口期提交前的本机步骤（见本脚本 docstring 的跑法）。")
        return 0

    print("跨会话共享文件写权边界（WP-DEBT-04 / V2.1 §7.1）")

    # ---- G1：清单从文档派生且非空 --------------------------------------
    try:
        patterns = load_shared_patterns()
    except SharedFileTouched as exc:
        print(f"  [FAIL] G1 共享清单派生 —— {exc}")
        print()
        print("[FAIL] RESULT=FAIL 共 1 处问题：")
        print("  - 共享清单无法从文档派生（覆盖范围失去来源）")
        return 1
    print(f"  [PASS] G1 共享清单派生 —— {len(patterns)} 处（源：{SEAM_DOC}）")

    if "--list" in argv:
        for i, (pattern, perm) in enumerate(patterns, 1):
            print(f"    {i}. {pattern:<45} {perm}")
        return 0

    # ---- 取提交集 --------------------------------------------------------
    explicit: list[str] = []
    if "--paths" in argv:
        idx = argv.index("--paths")
        explicit = [a for a in argv[idx + 1 :] if not a.startswith("--")]
    if "--staged" in argv:
        paths = _staged_paths()
        source = "暂存区（git diff --cached）"
    elif explicit:
        paths = explicit
        source = "--paths（显式列出）"
    else:
        print("  [SKIP] 未提供提交集（用 `--paths <p...>` 或 `--staged`）；")
        print("         本判据没有『要检查什么』的默认值 —— 无入参时**不假装检查过**。")
        return 0

    # ---- G3：非空 + 路径存在（活性下界）--------------------------------
    problems: list[str] = []
    if len(paths) < MIN_PATHS:
        problems.append(
            f"提交集为空（{len(paths)} < {MIN_PATHS}）—— 空集时『未触碰共享文件』"
            f"恒真（ISS-143 同族：扫不到东西时的『全通过』）"
        )
    missing = [p for p in paths if not (REPO_ROOT / _norm(p)).exists()]
    if missing:
        problems.append(
            f"提交集里有路径在磁盘上不存在：{missing} —— 可能是 shell 展开失败 /"
            f" 路径写错；对不存在的路径判『未触碰共享文件』是假绿"
        )
    if len(paths) >= MIN_PATHS and not missing:
        print(f"  [PASS] G3 提交集活性下界 —— {len(paths)} 个路径（源：{source}）")
    else:
        print(f"  [FAIL] G3 提交集活性下界 —— {len(paths)} 个路径，缺失 {len(missing)} 个")

    # ---- G2：未触碰共享文件（主判据）------------------------------------
    main_agent = MAIN_AGENT_FLAG in argv
    hits = classify(paths)
    if hits and main_agent:
        # 显式声明"我是主 agent 收口" ⇒ 放行，但**逐条出声**（不许静默）。
        # V2.1 §7.2 的收口顺序里共享文件本就由主 agent 串行改；
        # 静默放行才会退化成后门（ISS-95 的开关形态）。
        print(f"  [PASS] G2 共享文件写权 —— 显式声明『主 agent 收口』，放行 {len(hits)} 处：")
        for path, pattern, perm in hits:
            print(
                f"         · {path}  命中 `{pattern}`"
                f"（表内权限：{perm}）—— 按 §7.2 由主 agent 串行改"
            )
        print(
            "         ⚠️ 本判据只核对『是否**声明**了主 agent』，"
            "**不核对这个声明是否为真** ——"
        )
        print(
            "            共享工作树里两个会话的改动在 `git status` 上完全同形"
            "（ISS-207/217 已定：不把『谁改的』当断言）。"
        )
    elif hits:
        print(f"  [FAIL] G2 未触碰共享文件 —— 命中 {len(hits)} 个")
        for path, pattern, perm in hits:
            print(f"         ★ {path}  命中共享条目 `{pattern}`（{perm}）")
        problems.append(
            "提交集触碰了 §7.1 的共享文件："
            + "、".join(f"{p}（`{pat}`）" for p, pat, _ in hits)
            + "。若本次确属主 agent 串行收口，请显式加 `--main-agent`"
            + "（会逐条出声放行）；否则应把共享文件从本次提交中拆出。"
        )
    else:
        print(f"  [PASS] G2 未触碰共享文件 —— {len(paths)} 个路径全部在共享清单之外")

    # ---- G4：`--main-agent` 不得是"恒 ON 的空转开关" --------------------
    # ⚠️ 缺了这条，开关会退化成**掩盖覆盖范围变化**的东西：
    #    它一旦被无条件带上，G2 就永远不再有区分力，而输出照旧全绿
    #    （与 `check_evidence_scope.py` 的 `EXEMPT_FROM_A1` 必须核对
    #    "豁免是否多余"是同一条纪律）。
    #    故：声明了主 agent，却**一个共享文件都没放行** ⇒ 报红。
    if main_agent and not hits:
        print(
            "  [FAIL] G4 `--main-agent` 空转 —— 声明了主 agent 收口，"
            f"但 {len(paths)} 个路径里一个共享文件都没有"
        )
        problems.append(
            "`--main-agent` 是空转的：本次提交集不含任何共享文件 ⇒ 该开关"
            "掩盖了『覆盖范围已变化』（例如共享清单从文档里被删空了）。"
            "请在不需要它时去掉 —— 恒 ON 的豁免等于没有判据。"
        )
    elif main_agent:
        print(f"  [PASS] G4 `--main-agent` 非空转 —— 确实放行了 {len(hits)} 处共享文件")

    print()
    if not problems:
        # ⚠️ 收尾文案必须**区分两种通过**，否则会出现"判据放行了 1 处共享文件、
        #    收尾却说『未触碰共享文件』"的**自我矛盾的输出** ——
        #    读证据的人只会看到最后一行（本仓已多次踩到"末行被读成结论"，
        #    见 `collect_wp22_code_review.py` 反向抓最后含 passed 的行）。
        verdict = (
            f"遵守了 §7.1 写权边界：**以主 agent 身份显式放行 {len(hits)} 处共享文件**"
            if main_agent
            else f"未触碰共享文件，符合 §7.1 的写权边界"
        )
        print(
            f"[PASS] 提交集（{len(paths)} 个路径）{verdict}"
            f"（清单 {len(patterns)} 处，由 {SEAM_DOC} 派生）"
        )
        return 0
    print(f"[FAIL] RESULT=FAIL 共 {len(problems)} 处问题：")
    for p in problems:
        print(f"  - {p}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
