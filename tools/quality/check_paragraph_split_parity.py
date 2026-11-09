#!/usr/bin/env python
"""正文**切段口径三处一致**判据（阻塞 A 的后半：ISS-137 同族的落点）。

## 这条判据在守什么

「一段」的定义决定 `media_versions.paragraph_ref` 的**坐标系**。
正文切段在本仓有**三处**实现：

| # | 位置 | 形态 |
|---|---|---|
| 1 | `content/schemas.py::split_paragraphs` | 供`paragraph_count` / 绑定覆盖校验用 |
| 2 | `knowledge/chunking.py::_split_paragraphs` | 供检索切块用 |
| 3 | `media/schemas.py::paragraph_refs_for_body` | 供生成图的段落坐标用 |

⚠️ **`media` 那一处在 OP-197 收口期从 `service.py`搬到了 `schemas.py`**：
`service.py` 长期是**未入库**的在途产物，判据绑在它上面时**共享工作树绿、
干净检出红**（`ModuleNotFoundError`）。`media/service.py` 仍转出同名函数
（调用方不用改），但**不保留第二份实现**。见 G7。

⚠️ **三处不可收敛成一处**：模块平级、彼此不可import（本仓铁律，
与 `media/repository.py` 取`content` 数据的同一纪律）。所以"唯一实现"
这个手段在这里**不可用**，只能退一步用**判据钉住一致性**。

这不是"洁癖"问题，是**真实故障链**：

    `content` 侧按 N 段判"绑定是否覆盖全部段落"
    `media`侧按 M 段（M ≠ N）生成图
    ⇒ 两套段号坐标系，`paragraph_ref` 与段落绑定**根本对不上**
    ⇒ 刚生成的图立刻被判`PARAGRAPH_REF_UNKNOWN`，而真因是切段口径分叉。

## 实测过的分叉（2026-10-08，判据写下来之前的现场）

`content/schemas.py` 当时写的是 `body.splitlines()`（**段内软换行也切**），
另两处是 `re.split(r"\\n\\s*\\n")`（**只按空行切**）。8 个场景里**4 个**分叉：

| 正文 | `content`（旧） | 另两处 |
|---|---|---|
| `A\\nB` | 2 段 | 1 段 |
| `A1\\nA2\\n\\nB` | 3 段 | 2 段 |
| `A\\n  B` | 2 段 | 1 段 |
| `A\\U0001F600\\nB\\U0001F602` | 2 段 | 1 段 |

后两行是**小红书正文常态**（emoji + 段内换行）。
⚠️ 那个 `content` 版本的docstring 自己就写着「**只按空行分段**」
⇒ 它是**代码没兑现文档**，不是有意的口径分歧。这也是为什么修法是
"改 `content` 去对齐"而不是"三处投票"。

## 判据（G1~G8）

| # | 判据 | 抓什么 |
|---|---|---|
| G1 | **三处真代码**在同一批正文上切段结果**逐段全等** | 口径分叉（本体） |
| G2 | 每处的**段数**都与本判据的场景期望表全等 | 三处一起错到同一个值 ⇒ G1 恒真 |
| G3 | 段内软换行**不切段**（小红书常态） | 退回`splitlines()` 写法 |
| G4 | `media` 侧序号是**过滤后连续下标**且从 0 起 | 空白段参与编号 ⇒ 删一个空段让全体序号平移 |
| G5 | 活性下界：三处实现**都真的被导入并调用过** | 某个 import 静默失败时"全通过"（ISS-143 同族） |
| G6 | docstring 里点名的 `tools/quality/check_*.py` **必须真实存在** | 点名一个不存在的脚本 = 声称有防线（ISS-95） |
| G7 | 三处实现所在文件**必须已入库**（`git ls-files`） | 判据绑在未入库产物上 ⇒ 共享树绿、**干净检出红**（ISS-95/143 同族） |
| G8 | 权威口径文档 `docs/数据结构与接口契约.md` **存在且已入库**，且它的 §1.4 场景期望表与本判据 `_EXPECTED` **双向对账** | OP-197 要的"第二处文档副本"缺位；或文档与判据**各自漂移**（汇总列无对账 ⇒ ISS-85 族） |

## ⚠️ 期望值不得来自被测对象（第 6 条纪律）

`_EXPECTED` 里每一条都是**手写的场景语义**，不是"跑一遍实现把输出抄下来"。
否则三处一起错到同一个值时 G2 会一起"通过"，判据变成同义反复。
G1 与 G2 是**成对**的：G1 管"三处相等"，G2 管"相等的那份对不对"。

## 为什么必须**导入真代码**而不是扫源码文本

扫 `"\\n\\s*\\n"` 这样的字面量只能证明"文本里有这个字符串"，
证不出"切出来的段一样"。本判据的做法是 `import` 三个真函数、
喂同一批正文、比对返回值 —— **这是唯一能让G1 说出真话的形态**。
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "services" / "backend" / "src"

# G5：活性下界。三处实现各至少被调用这么多次（真实场景数远大于此）。
MIN_CASES = 11
MIN_FILES = 30

# G2的期望值：**手写**的场景语义，不是从实现输出抄的。
# 键是场景名，值是 (正文, 期望段数, 期望首段文本或None 表示空)。
_EXPECTED: dict[str, tuple[str, int, str | None]] = {
    "空行分段": ("A\n\nB", 2, "A"),
    "单换行是段内软换行": ("A\nB", 1, "A\nB"),
    "连续多空行仍算一段": ("A\n\n\n\nB", 2, "A"),
    "段内换行加空行": ("A1\nA2\n\nB", 2, "A1\nA2"),
    "缩进不切段": ("A\n  B", 1, "A\n  B"),
    "空正文切出零段": ("", 0, None),
    "emoji加段内换行": ("A\U0001F600\nB\U0001F602", 1, "A\U0001F600\nB\U0001F602"),
    "尾随空段被丢弃": ("A\n\n\n", 1, "A"),
    "前导空段被丢弃": ("\n\nA\n\nB", 2, "A"),
    "全空白切出零段": ("  \n\n \n", 0, None),
    "CRLF视作空行": ("A\r\n\r\nB", 2, "A"),
}

# G3：这三个场景是"段内软换行不切段"的锚点。
_MUST_NOT_SPLIT = ("单换行是段内软换行", "缩进不切段", "emoji加段内换行")

_THIS_SCRIPT_REFS = (
    "check_paragraph_split_parity.py",
)


# 三处实现各自住在哪个**文件** —— 用于 G7 的入库状态门。
# ⚠️ 这里登记的是**已入库**文件。`media` 侧原先登记 `service.py`，
# 而它长期未入库⇒ 本判据在干净检出上必然 FAIL（实测 OP-197 收口期撞到，
# `ModuleNotFoundError: media.service`）。判据被绑在还没进版本库的实现上
# = 「有防线但门打不开」（ISS-95 族）。实现已搬进 `media/schemas.py`。
_IMPL_FILES: dict[str, str] = {
    "content": "services/backend/src/rednote/modules/content/schemas.py",
    "knowledge": "services/backend/src/rednote/modules/knowledge/chunking.py",
    "media": "services/backend/src/rednote/modules/media/schemas.py",
}

# G8：权威口径的**第二处文档副本**（OP-197 明确要求）。
# ⚠️ 这条判据的来历：OP-197 原话是「§2 的『权威定义』必须有第二处文档副本
#    （G6 只查脚本存在性，不查内容一致）」。此前该文档**全仓不存在**
#    （`git log --all` 零历史）⇒ 两条 OP（OP-197 / OP-199）共同点名一个
#    不存在的落点，属"幽灵落点"（ISS-95 族）。
# 本判据做两件事，缺一不可：
#   ① 文档**存在且已入库**（否则共享树有、干净检出没有 ⇒ 铁律 18）；
#   ② 文档 §1.4 的表与本判据 `_EXPECTED` **双向对账**（场景名 ↔ 期望段数）。
#      只查存在性 = G6 的老毛病（"点名一个路径就算有防线"）。
CONTRACT_DOC = "docs/数据结构与接口契约.md"

# 文档 §1.4 表格行的形态：`| `场景名` | 期望段数 | ... |`
# ⚠️ 场景名用反引号包住（与 `_EXPECTED` 的键一一对应）；期望段数是纯数字。
_DOC_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(\d+)\s*\|")


def _check_contract_doc() -> list[str]:
    """G8：权威口径文档存在 + 已入库 + §1.4 场景表与 `_EXPECTED` 双向对账。

    ## 为什么"双向"

    单向下（只查"文档里的场景都在 `_EXPECTED` 里"）会漏掉
    "`_EXPECTED` 新增了场景、文档没跟上" —— 那正是"第二处副本腐烂"的形态。
    单向上（只查"`_EXPECTED` 的场景都在文档里"）则漏掉"文档写了判据没有的场景"。
    两个方向都要，才能让**任一侧单独改动**都报红。

    ## 为什么这条属于"内容一致"而不是"存在性"（OP-197 的原话）

    G6 只查"脚本存不存在"、不查内容；若 G8 也只查"文档存不存在"，
    那它就与 G6 同病 —— 点名一个路径就算有防线（ISS-95）。
    ⇒ 本函数**必须**读到表、比到值。
    """
    problems: list[str] = []
    doc_path = REPO_ROOT / CONTRACT_DOC

    if not doc_path.exists():
        problems.append(
            f"权威口径文档**不存在**：{CONTRACT_DOC} —— OP-197 明确要求"
            f"「权威定义必须有第二处文档副本」，而该文件全仓不存在"
            f"（两条 OP 共同点名一个不存在的落点 = 幽灵落点，ISS-95 族）"
        )
        return problems

    # ---- 已入库门（与 G7 同理：共享树有、干净检出没有 = 铁律 18）--------
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=false", "ls-files", "--", CONTRACT_DOC],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        tracked = {ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()}
    except OSError as exc:  # pragma: no cover - git 不可用
        problems.append(f"  调用 git ls-files 失败：{exc}")
        return problems
    if CONTRACT_DOC not in tracked:
        problems.append(
            f"权威口径文档**未入库**：{CONTRACT_DOC} —— 干净检出上 G8 会红，"
            f"形状与「口径不一致」同形（铁律 18 / ISS-95 族）"
        )

    # ---- §1.4 场景表双向对账 -------------------------------------------
    text = doc_path.read_text(encoding="utf-8")
    doc_rows: dict[str, int] = {}
    for line in text.splitlines():
        m = _DOC_ROW_RE.match(line)
        if m:
            # 同名场景出现两次 = 文档内部自相矛盾，末次写入会被覆盖 ⇒ 显式报红
            if m.group(1) in doc_rows:
                problems.append(
                    f"文档 §1.4 表里场景「{m.group(1)}」出现多次 —— "
                    f"同名两行会让对账取到哪一行变成偶然"
                )
            doc_rows[m.group(1)] = int(m.group(2))

    expected_rows = {name: want_n for name, (_b, want_n, _f) in _EXPECTED.items()}

    only_in_code = sorted(set(expected_rows) - set(doc_rows))
    only_in_doc = sorted(set(doc_rows) - set(expected_rows))
    mismatch = sorted(
        name
        for name in set(expected_rows) & set(doc_rows)
        if expected_rows[name] != doc_rows[name]
    )

    if only_in_code:
        problems.append(
            f"这些场景在判据 `_EXPECTED` 里有、文档 §1.4 表里没有：{only_in_code}"
            f" —— 判据新增场景时未同步文档，第二处副本已开始腐烂（ISS-85 族）"
        )
    if only_in_doc:
        problems.append(
            f"这些场景在文档 §1.4 表里有、判据 `_EXPECTED` 里没有：{only_in_doc}"
            f" —— 文档声称的场景没有任何实现被比对"
        )
    if mismatch:
        detail = "、".join(
            f"{n}（文档 {doc_rows[n]} ≠ 判据 {expected_rows[n]}）" for n in mismatch
        )
        problems.append(f"场景期望段数不一致：{detail}")

    if not doc_rows:
        problems.append(
            "文档 §1.4 表**一行都没解析到** —— 要么表被删了、要么行格式改了"
            "（本判据靠 `` | `场景名` | 数字 | `` 形态解析）；"
            "解析不到时『无差异』是假绿（ISS-143 同族的『扫不到东西』）"
        )
    return problems


def _check_impl_tracked() -> list[str]:
    """G7：三处实现所在文件必须**已入库**（`git ls-files` 能查到）。

    ## 为什么需要这条（G7 的来历 —— 真实踩坑）

    本判据第一版把 `media`侧指向 `media/service.py`，而那个文件当时是
    **未跟踪的在途产物**。于是在**干净检出**（`git worktree add` + HEAD）
    上跑本判据必然 FAIL：`ModuleNotFoundError: rednote.modules.media.service`。

    形状上它与"判据写错了"完全一样，而更糟的是：**共享工作树里它是绿的**
    （那个文件在磁盘上），只有干净检出才红 —— 而干净检出才是 CI 与
    别人的检出口径。判据在自己跑的时候绿、在别人跑的时候红，且不报错，
    正是 ISS-143「没跑」与「跑失败」同形的族。

    ⇒ 判据必须**自己**确认输入已入库，而不是指望"文件迟早会入库"。
    """
    problems: list[str] = []
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=false", "ls-files", "--", *sorted(_IMPL_FILES.values())],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - git 不可用
        problems.append(f"  调用 git ls-files 失败：{exc}")
        return problems

    tracked = {
        line.strip()
        for line in (proc.stdout or "").splitlines()
        if line.strip()
    }
    for name, rel in sorted(_IMPL_FILES.items()):
        if rel not in tracked:
            problems.append(
                f"  `{name}` 侧切段实现所在文件**未入库**：{rel}"
                f" —— 干净检出上本判据无法导入它"
                f"（会把「门打不开」印成与「口径不一致」同形的失败）"
            )
    return problems


def _load_impls() -> dict[str, object]:
    """导入三处真实现。返回 {标签: 可调用}。

    ⚠️ **导入失败必须抛**，不能返回空 dict 蒙过去 —— 那样G1/G2 会
    因为"没有实现可比"而空转通过。
    """
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))

    from rednote.modules.content.schemas import split_paragraphs
    from rednote.modules.knowledge.chunking import _split_paragraphs
    from rednote.modules.media.schemas import paragraph_refs_for_body

    return {
        "content": split_paragraphs,
        "knowledge": lambda body: tuple(_split_paragraphs(body)),
        "media": lambda body: tuple(
            item[1] for item in paragraph_refs_for_body(body)
        ),
    }


def _load_media_indexes() -> object:
    """单独导入`media` 的三元组序列，供 G4 查序号连续性。"""
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    from rednote.modules.media.schemas import paragraph_refs_for_body

    return paragraph_refs_for_body


def _iter_py_files() -> list[Path]:
    return sorted(p for p in SRC_ROOT.rglob("*.py"))


def _check_docstring_refs() -> list[str]:
    """G6：`src/` 与 `migrations/` 里docstring 点名的判据脚本必须真实存在。"""
    problems: list[str] = []
    roots = [
        SRC_ROOT,
        REPO_ROOT / "services" / "backend" / "tests",
        REPO_ROOT / "migrations",
    ]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for ref in _THIS_SCRIPT_REFS:
                if ref in text and not (REPO_ROOT / "tools" / "quality" / ref).exists():
                    problems.append(
                        f"{path.relative_to(REPO_ROOT)} 点名了 {ref}，但该脚本不存在"
                        "（声称有防线比没有防线危险）"
                    )
    return problems


def main() -> int:
    print("判据：正文切段口径三处必须一致（阻塞 A / ISS-137 同族）")
    print()

    problems: list[str] = []

    # ---- G5 前置：活性下界 ----------------------------------------------
    files = _iter_py_files()
    print(f"  扫描 {len(files)} 个 .py 文件")
    if len(files) < MIN_FILES:
        problems.append(
            f"活性下界未达（文件 {len(files)}/{MIN_FILES}）—— "
            "收集规则可能已失效，扫不到东西时会是『全通过』（ISS-143 同族）"
        )
        print(f"  [FAIL] G5 活性下界—— 文件 {len(files)} < {MIN_FILES}")
    else:
        print(f"  [PASS] G5 活性下界（文件 {len(files)} >= {MIN_FILES}）")

    if len(_EXPECTED) < MIN_CASES:
        problems.append(
            f"场景表只有 {len(_EXPECTED)} 个 < {MIN_CASES} —— "
            "场景太少时口径分叉可能落在表外（G1 会假绿）"
        )

    # ---- 导入三处真实现（G5：导入失败必须抛）---------------------------
    try:
        impls = _load_impls()
        media_triples = _load_media_indexes()
    except Exception as exc:  # noqa: BLE001 —— 这里就是要拦住任何导入失败
        print(f"  [FAIL] G5 三处实现可导入 —— {type(exc).__name__}: {exc}")
        print()
        print(f"[FAIL] RESULT=FAIL 共 1 处问题：")
        print("  - 三处切段实现有导入失败，判据无法比对（静默通过 = 无防线）")
        return 1
    print(f"  [PASS] G5 三处实现可导入并调用 —— {sorted(impls)}")

    # ---- G1 / G2：逐场景比对三处 + 比对手写期望 ------------------------
    diff_cases: list[str] = []
    spec_bad: list[str] = []
    rows: list[str] = []
    for name, (body, want_n, want_first) in _EXPECTED.items():
        results: dict[str, object] = {}
        errored: dict[str, str] = {}
        for label, fn in impls.items():
            try:
                results[label] = tuple(fn(body))
            except Exception as exc:  # noqa: BLE001
                errored[label] = f"{type(exc).__name__}: {exc}"
        if errored:
            problems.append(f"场景「{name}」下有实现抛错：{errored}")
            continue

        values = {label: tuple(val) for label, val in results.items()}
        distinct = set(values.values())
        same = len(distinct) == 1
        if not same:
            detail = " ≠ ".join(f"{k}={v!r}" for k, v in sorted(values.items()))
            diff_cases.append(f"场景「{name}」三处结果不等：{detail}")

        got = values["content"]
        spec_ok = len(got) == want_n and (want_first is None or (got and got[0] == want_first))
        if not spec_ok:
            spec_bad.append(
                f"场景「{name}」与期望不符：得 {got!r}，"
                f"期望 {want_n} 段且首段={want_first!r}"
            )

        rows.append(
            f"    {name:<16} content={len(values['content'])} "
            f"knowledge={len(values['knowledge'])} media={len(values['media'])} "
            f"期望={want_n}  {'OK' if same and spec_ok else '★FAIL'}"
        )

    print("  场景逐条：")
    for row in rows:
        print(row)
    print()

    print(
        f"  [{'PASS' if not diff_cases else 'FAIL'}] G1 三处逐段全等"
        f" —— {len(_EXPECTED) - len(diff_cases)}/{len(_EXPECTED)} 个场景一致"
    )
    for item in diff_cases:
        print(f"         ★ {item}")
    problems.extend(diff_cases)

    print(
        f"  [{'PASS' if not spec_bad else 'FAIL'}] G2 段数与首段合手写期望"
        f" —— {len(_EXPECTED) - len(spec_bad)}/{len(_EXPECTED)} 个场景符合"
    )
    for item in spec_bad:
        print(f"         ★ {item}")
    problems.extend(spec_bad)

    # ---- G3：段内软换行不切段 ------------------------------------------
    g3_bad: list[str] = []
    for name in _MUST_NOT_SPLIT:
        body, want_n, _ = _EXPECTED[name]
        for label, fn in impls.items():
            try:
                got = tuple(fn(body))
            except Exception as exc:  # noqa: BLE001
                g3_bad.append(f"场景「{name}」{label} 抛错：{exc}")
                continue
            if len(got) != want_n:
                g3_bad.append(
                    f"场景「{name}」{label} 切出 {len(got)} 段（应{want_n}）—— "
                    "段内软换行被当成了分段（小红书正文常态）"
                )
    print(
        f"  [{'PASS' if not g3_bad else 'FAIL'}] G3 段内软换行不切段"
        f" —— {len(_MUST_NOT_SPLIT)} 个锚点场景"
    )
    for item in g3_bad:
        print(f"         ★ {item}")
    problems.extend(g3_bad)

    # ---- G4：media 序号是过滤后连续下标 --------------------------------
    g4_bad: list[str] = []
    for name, (body, want_n, _first) in _EXPECTED.items():
        try:
            triples = tuple(media_triples(body))
        except Exception as exc:  # noqa: BLE001
            g4_bad.append(f"场景「{name}」抛错：{exc}")
            continue
        indexes = [int(t[0]) for t in triples]
        if indexes != list(range(len(triples))):
            g4_bad.append(
                f"场景「{name}」序号不是从0 起的连续下标：{indexes} —— "
                "空白段参与了编号，删一个空段会让后面所有段序号平移"
            )
        if len(triples) != want_n:
            g4_bad.append(f"场景「{name}」三元组数 {len(triples)} != 期望 {want_n}")
    print(
        f"  [{'PASS' if not g4_bad else 'FAIL'}] G4 media 序号连续且段数正确"
        f" —— {len(_EXPECTED)} 个场景"
    )
    for item in g4_bad:
        print(f"         ★ {item}")
    problems.extend(g4_bad)

    # ---- G6：docstring 点名的判据脚本必须存在 --------------------------
    ref_problems = _check_docstring_refs()
    print(
        f"  [{'PASS' if not ref_problems else 'FAIL'}] G6 被点名的判据脚本真实存在"
        f" —— 查 {len(_THIS_SCRIPT_REFS)} 个"
    )
    for item in ref_problems:
        print(f"         ★ {item}")
    problems.extend(ref_problems)

    # ---- G7：三处实现所在文件必须已入库（干净检出口径） -----------------
    tracked_problems = _check_impl_tracked()
    print(
        f"  [{'PASS' if not tracked_problems else 'FAIL'}] G7 三处实现所在文件已入库"
        f" —— 查 {len(_IMPL_FILES)} 个"
    )
    for item in tracked_problems:
        print(f"         ★ {item.strip()}")
    problems.extend(tracked_problems)

    # ---- G8：权威口径文档存在 + 已入库 + 场景表双向对账（OP-197）---------
    doc_problems = _check_contract_doc()
    print(
        f"  [{'PASS' if not doc_problems else 'FAIL'}] G8 权威口径文档与场景表对账"
        f" —— {CONTRACT_DOC}"
    )
    for item in doc_problems:
        print(f"         ★ {item.strip()}")
    problems.extend(doc_problems)

    print()
    if not problems:
        print(
            f"[PASS] 三处切段口径一致且合手写期望"
            f"（{len(_EXPECTED)} 个场景；序号连续；被点名脚本存在；实现均已入库；"
            f"权威口径文档已入库且场景表双向对账）"
        )
        return 0
    print(f"[FAIL] RESULT=FAIL 共 {len(problems)} 处问题：")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    raise SystemExit(0 if main() == 0 else 1)
