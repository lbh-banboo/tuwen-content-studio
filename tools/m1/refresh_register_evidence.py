#!/usr/bin/env python
"""刷新「登记册」类证据日志（`op-register.log` / `issue-log-stats.log`）。

**为什么要有这个脚本**：这两份日志原先**没有生成器**，是 WP09 时代手工拼的，
于是它们的元信息一路停在当时的状态 —— `op-register.log` 里写着「OP 条目数 78 /
编号范围 OP-01 ~ OP-78 / 登记册行数 401」，而登记册已经到 154 条、715 行。
读证据的人看到的是**一个早就不是事实的结论**，而且没有任何机制会提醒他。

这与 `collect_wp10_evidence.py` 里 `backend-pytest.log` 标题硬编码「WP06~WP10」
是同一个病：**证据文件里的元信息一旦写死，就会过期；过期比缺失更伤信任**。
因此本脚本的全部计数都**从数据源现场读出**，一个都不写死。

数据源（都是唯一事实来源，不另存一份）：
  - 待定项：`docs/待定项与问题登记册.md` 里的 `OP-xx` 表格行
  - 问题记录：`docs/issues/issue-log.jsonl`

产出（落在 `evidence/m1/`）：

| 文件 | 内容 |
|---|---|
| `op-register.log` | 登记册结构：OP 条目数 / 编号连续性 / 状态分布 / 分类看板核对 / 责任方 / 来源统计 |
| `issue-log-stats.log` | `log_issue.py --stats` 原样输出 + `--check` 门禁结果 |

跑法：

```bash
python tools/m1/refresh_register_evidence.py
```

退出码 0 = 全部通过（含"看板声明数与实际行数一致"这类交叉核对）。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m1"
REGISTER = REPO_ROOT / "docs" / "待定项与问题登记册.md"
ISSUE_LOG = REPO_ROOT / "docs" / "issues" / "issue-log.jsonl"
LOG_ISSUE = REPO_ROOT / "tools" / "issues" / "log_issue.py"

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

# OP 表格行的样子：`| OP-151 | ... |`
_OP_ROW_RE = re.compile(r"^\|\s*(OP-\d+)\s*\|(.+)$", re.M)
# 看板行：`| G 技术设计待定类 | OP-65 ~ OP-73、OP-79 ~ OP-81、... | 83 | 0 | 15 | 45 | 23 |`
_BOARD_ROW_RE = re.compile(
    r"^\|\s*([A-H][^|]*?类)\s*\|([^|]*)\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|",
    re.M,
)
_BOARD_TOTAL_RE = re.compile(
    r"^\|\s*\*\*合计\*\*\s*\|([^|]*)\|\s*\*\*(\d+)\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|"
    r"\s*\*\*(\d+)\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|",
    re.M,
)
# 「目标时机」列尾部括号补充：`M1 内（S1 收口批）` → `M1 内`
_TIME_TAIL_PAREN_RE = re.compile(r"[（(][^（()）]*[)）]\s*$")
_STATUSES = ("OPEN", "IN_PROGRESS", "RESOLVED", "WONT_FIX", "SUPERSEDED")
_LEVELS = ("\U0001f534", "\U0001f7e0", "\U0001f7e1", "\U0001f7e2")  # 🔴 🟠 🟡 🟢
_LEVEL_RE = re.compile("|".join(map(re.escape, _LEVELS)))
_HEADER_VERSION_RE = re.compile(r"^\|\s*当前版本\s*\|\s*([^|]+?)\s*\|", re.M)


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def _write(name: str, lines: list[str]) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _run(args: list[str], *, cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, env={"NO_PROXY": "*", **__import__("os").environ}
    )
    return proc.returncode, (proc.stdout + proc.stderr).rstrip()


def _parse_ops() -> list[tuple[str, list[str]]]:
    """返回 [(OP 编号, 该行按 `|` 切开的单元格)]。"""
    text = REGISTER.read_text(encoding="utf-8")
    rows: list[tuple[str, list[str]]] = []
    for oid, rest in _OP_ROW_RE.findall(text):
        rows.append((oid, [cell.strip() for cell in rest.split("|")]))
    return rows


def _row_status(cells: list[str]) -> str:
    """从行内单元格里找出状态 token（状态列是裸 token，不加反引号）。"""
    for cell in cells:
        token = cell.strip("`* ").upper()
        if token in _STATUSES:
            return token
    return "?"


def _row_level(cells: list[str]) -> str:
    """从行内单元格里找出**阻塞级别**（🔴/🟠/🟡/🟢）。

    按**单元格出现顺序**取第一个命中的 emoji —— 不能按 `_LEVELS` 的固定顺序扫，
    否则同一行有多个 emoji 时会取错（ISS-85 的现场教训：口径一错，
    对账结果就变成"数字错了"的假告警，而真正错的是提取方式）。
    """
    for cell in cells:
        match = _LEVEL_RE.search(cell)
        if match:
            return match.group(0)
    return "?"


# ---------------------------------------------------------------- §3.2 目标时机分桶
# 「目标时机」列是自由文本（实测 60 种取值），因此**必须**定死一套归一规则，
# 否则「分项之和 == 合计」永远只是人工点数（ISS-84/86 同族：声称方没有判据）。
#
# 规则 = 归一（去 `**`、去尾部括号补充、压空白）+ **按语义优先级**匹配：
# 先判跨阶段交叉项与硬时点，再判里程碑，最后才是具体工作包；都不中 → 兜底桶。
# 用 `in` 而不是 `startswith`：`WP08 / 内网首日` 这类**复合值**必须归到硬时点
# （内网首日），而不是被 WP 前缀抢走 —— 口径定在哪一边会直接改变分桶结果。
_TIME_BUCKET_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 顺序即优先级。前三条是"硬时点"，必须早于具体工作包。
    ("W1~W2", ("W1~W2",)),
    ("内网首日", ("内网首日",)),
    ("W1（内网首日之前）", ("W1 当周", "W1 起持续", "W1")),
    ("W2", ("W2",)),
    ("M1 内 / 收口前", ("M1",)),
    ("M2 / M3 / M4", ("M2", "M3", "M4")),
    ("具体工作包（WPxx）", ("WP",)),
)
# 兜底桶：**明确确认"没有可排期的时点"**的写法（语义上是"触发式"的同义表述）。
# 白名单之外的取值一律进 `_TIME_UNCLASSIFIED` 并告警 —— 若把兜底桶写成
# "其余全部"，就永远不会有告警，新增写法会静默消失（ISS-86 的教训）。
_TIME_FALLBACK = "触发式 / 无明确时机"
_TIME_FALLBACK_HINTS = (
    "触发式", "实测当次", "实测之后", "G0-17", "下次协议修订", "下次文档修订",
    "CI 建设", "试点期", "待产品定模型", "不影响内网", "有真实负载时", "M0 出口后",
    "与 OP-166 一起", "首次切块器变更时",
)
# 真正归不进去的（应为 0；非 0 说明新增了写法，必须显式处理而不是静默消失）
_TIME_UNCLASSIFIED = "未归类"


def _norm_time(raw: str) -> str:
    """归一「目标时机」原文：去强调符、去尾部括号补充、压空白。"""
    text = raw.replace("*", "").strip()
    text = _TIME_TAIL_PAREN_RE.sub("", text).strip()
    return re.sub(r"\s+", " ", text)


def _time_bucket(normed: str) -> str:
    for bucket, keys in _TIME_BUCKET_RULES:
        if any(key in normed for key in keys):
            return bucket
    if any(hint in normed for hint in _TIME_FALLBACK_HINTS):
        return _TIME_FALLBACK
    return _TIME_UNCLASSIFIED


# ---------------------------------------------------------------- §3.1 责任方编号列
# 「编号」列是**自由书写**的混合格式，同一行里混着四种写法 + 中文说明：
#   `OP-01~05, 09, 10, ...`（范围打头，后续是**裸数字**）
#   `OP-135~OP-140`（范围带 OP- 前缀）/ `OP-80, OP-84~OP-86`（单个与范围混排）
#   `WP19 新发现 **OP-151**（关键词命中率）`（编号前有说明、后有括号注）
#
# ⚠️ 展开错了会得到"数字错了"的**假告警**，这是本判据最大的风险点。两个坑：
#   ① 一个无差别的 `\d+` 会把 `（200 万量级校准）` 里的 `200` 当成 OP-200；
#   ② 反过来，若要求 token 必须是"纯编号"，`**OP-135~OP-140**（WP17 新发现）`
#      这类带说明的写法又会**整条漏掉**（实测漏 39 条 → 假报"孤儿"）。
# 因此用**带状态的扫描器**：
#   · 范围与 `OP-xx` 自带标识，无条件接受（说明文字可以在它们前面）；
#   · **裸数字**只在"上一个是范围/编号，且与它之间只隔分隔符"时才接受 ——
#     这条让续列能被完整吃下，同时把 `（200 ...）` 挡在外面（`（` 不是分隔符）。
_SCAN_RE = re.compile(
    r"(?:OP-)?(\d+)\s*~\s*(?:OP-)?(\d+)"  # ① 范围
    r"|OP-(\d+)"                          # ② 单编号
    r"|(?<![\w-])(\d{1,3})(?![\w-])"      # ③ 裸数字（需链）
)
_GAP_OK_RE = re.compile(r"[\s,、+]*")


def _expand_id_cell(raw: str) -> set[int]:
    """把「编号」列的自由文本展开成编号集合（见上方口径说明）。"""
    text = raw.replace("**", "")
    out: set[int] = set()
    pos = 0
    chain = False
    for match in _SCAN_RE.finditer(text):
        gap = text[pos:match.start()]
        pos = match.end()
        if match.group(1) is not None:  # ① 范围
            out |= set(range(int(match.group(1)), int(match.group(2)) + 1))
            chain = True
        elif match.group(3) is not None:  # ② 单编号
            out.add(int(match.group(3)))
            chain = True
        else:  # ③ 裸数字：仅在"编号链"内且紧邻分隔符时有效
            if chain and _GAP_OK_RE.fullmatch(gap):
                out.add(int(match.group(4)))
            else:
                chain = False
    return out


def _parse_owner_table(text: str) -> list[tuple[str, int, set[int]]]:
    """解析 §3.1「按责任方」表：[(行名, 声明条数, 编号集合)]。"""
    match = re.search(r"### 3\.1 按责任方(.*?)\n### 3\.2", text, re.S)
    if match is None:
        return []
    rows = re.findall(
        r"^\|\s*(\*\*[^*]+\*\*)\s*\|\s*(\d+)\s*\|([^|]*)\|", match.group(1), re.M
    )
    return [(name.strip(), int(cnt), _expand_id_cell(ids)) for name, cnt, ids in rows]


def _parse_time_table(text: str) -> list[tuple[str, int]]:
    """解析 §3.2「按目标时机」表：[(时机桶名, 声明条数)]（跳过「合计」行）。

    段落到下一个 `##` 标题或 `---` 分隔线为止（**不能用 `\\n>` 截断** ——
    本节的「口径」引注块紧跟在表后，用 `>` 定位会把整张表切掉）。
    桶名可能是 `**加粗**` 也可能不是，故先剥强调符再比对。
    """
    match = re.search(r"### 3\.2 按目标时机(.*?)(?=\n---|\n## )", text, re.S)
    if match is None:
        return []
    rows = re.findall(r"^\|\s*(.+?)\s*\|\s*\*{0,2}(\d+)\*{0,2}\s*\|", match.group(1), re.M)
    out: list[tuple[str, int]] = []
    for name, cnt in rows:
        name = name.replace("*", "").strip()
        if name and name not in ("合计", "时机"):
            out.append((name, int(cnt)))
    return out



def collect_op_register() -> tuple[Path, bool]:
    text = REGISTER.read_text(encoding="utf-8")
    ops = _parse_ops()
    nums = sorted(int(oid.split("-")[1]) for oid, _ in ops)
    uniq = sorted(set(nums))
    missing = [n for n in range(1, max(uniq) + 1) if n not in set(uniq)]
    dupes = [n for n, c in Counter(nums).items() if c > 1]

    status_counts = Counter(_row_status(cells) for _, cells in ops)

    # 分类看板：声明条数 vs 实际行数（交叉核对，防"看板数字忘了改"）
    board_rows = _BOARD_ROW_RE.findall(text)
    board_total = _BOARD_TOTAL_RE.search(text)
    declared_by_class: dict[str, int] = {}
    for name, _ids, cnt, *_ in board_rows:
        declared_by_class[name.strip()] = int(cnt)

    # 实际按编号区间归属（用看板自己声明的编号段反查，避免另立一套规则）
    actual_by_class: dict[str, int] = {}
    for name, ids_cell, *_ in board_rows:
        wanted: set[int] = set()
        for a, b in re.findall(r"OP-(\d+)\s*~\s*OP-(\d+)", ids_cell):
            wanted |= set(range(int(a), int(b) + 1))
        for m in re.finditer(r"OP-(\d+)", ids_cell):
            wanted.add(int(m.group(1)))
        actual_by_class[name.strip()] = len(wanted & set(uniq))

    board_mismatch = [
        (name, declared_by_class[name], actual_by_class.get(name, -1))
        for name in declared_by_class
        if declared_by_class[name] != actual_by_class.get(name, -1)
    ]
    declared_total = int(board_total.group(2)) if board_total else -1

    # 优先级四列核对（ISS-85：这四列此前从未被任何脚本读过，静默腐烂到 7/8 类错）
    level_by_op: dict[str, str] = {oid: _row_level(cells) for oid, cells in ops}
    level_dist = Counter(level_by_op.values())
    level_mismatch: list[tuple[str, tuple[int, ...], tuple[int, ...]]] = []
    for name, ids_cell, cnt, p0, p1, p2, p3 in board_rows:
        wanted: set[int] = set()
        for a, b in re.findall(r"OP-(\d+)\s*~\s*OP-(\d+)", ids_cell):
            wanted |= set(range(int(a), int(b) + 1))
        for m in re.finditer(r"OP-(\d+)", ids_cell):
            wanted.add(int(m.group(1)))
        hit = {f"OP-{n:02d}" for n in wanted if n in set(uniq)}
        act = tuple(
            sum(1 for oid in hit if level_by_op.get(oid) == lv) for lv in _LEVELS
        )
        decl = (int(p0), int(p1), int(p2), int(p3))
        if decl != act:
            level_mismatch.append((name.strip(), decl, act))
    if board_total is not None:
        decl_total = tuple(int(x) for x in board_total.groups()[2:])
        act_total = tuple(level_dist.get(lv, 0) for lv in _LEVELS)
        if decl_total != act_total:
            level_mismatch.append(("合计", decl_total, act_total))

    issues = [
        json.loads(line)
        for line in ISSUE_LOG.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    issue_sev = Counter(item["severity"] for item in issues)
    issue_status = Counter(item["status"] for item in issues)

    header_version = (_HEADER_VERSION_RE.search(text) or [None, "?"])[1]

    # ---- §3.1 责任方表核对（此前从未被任何判据读过）----
    # 口径：分项**不互斥**（一条 OP 可有多责任方），因此判据不是"之和 == 合计"，
    # 而是三条各自独立的机械判据：
    #   ① 行内声明条数 == 编号列实际展开出的条数（逐行）；
    #   ② 编号列里不得出现不存在的 OP（幽灵编号）；
    #   ③ 不得有 OP 在任何责任方行里都没被提到（孤儿）。
    owner_rows = _parse_owner_table(text)
    owner_seen: Counter[int] = Counter()
    owner_line_bad: list[tuple[str, int, int]] = []
    owner_ghosts: dict[str, list[int]] = {}
    for name, declared, ids in owner_rows:
        hit = ids & set(uniq)
        ghost = sorted(ids - set(uniq))
        if ghost:
            owner_ghosts[name] = ghost
        if declared != len(hit):
            owner_line_bad.append((name, declared, len(hit)))
        for num in hit:
            owner_seen[num] += 1
    owner_orphans = sorted(n for n in uniq if n not in owner_seen)
    owner_sum = sum(len(ids & set(uniq)) for _, _, ids in owner_rows)

    # ---- §3.2 目标时机分桶核对（此前从未被任何判据读过）----
    # 判据：**归一后的实际分桶**必须与文档声明的桶名与条数逐桶一致，
    # 且「分项之和 == 合计」。兜底桶/未归类桶非空都要显式打出（不报错最危险）。
    time_actual: Counter[str] = Counter()
    time_raw_by_bucket: dict[str, Counter[str]] = defaultdict(Counter)
    for oid, cells in ops:
        raw = cells[5] if len(cells) > 5 else "?"
        normed = _norm_time(raw)
        bucket = _time_bucket(normed)
        time_actual[bucket] += 1
        time_raw_by_bucket[bucket][normed] += 1
    time_declared = dict(_parse_time_table(text))
    time_declared_total = int(board_total.group(2)) if board_total else -1
    time_unclassified = time_actual.get(_TIME_UNCLASSIFIED, 0)
    time_bucket_mismatch = [
        (name, time_declared.get(name, -1), time_actual.get(name, 0))
        for name in set(time_declared) | set(time_actual)
        if name != _TIME_UNCLASSIFIED
        and time_declared.get(name, -1) != time_actual.get(name, 0)
    ]
    time_sum_ok = sum(time_actual.values()) == len(uniq)

    ok = (
        not missing
        and not dupes
        and declared_total == len(uniq)
        and not board_mismatch
        and not level_mismatch
        and status_counts.get("?", 0) == 0
        and not owner_line_bad
        and not owner_ghosts
        and not owner_orphans
        and not time_bucket_mismatch
        and time_unclassified == 0
        and time_sum_ok
    )

    lines = [
        "=== 待定项与问题登记册 建立与刷新证据 ===",
        f"生成时间：{_now()}",
        "生成方式：python tools/m1/refresh_register_evidence.py（计数全部现场派生，不写死）",
        f"数据源：{REGISTER.relative_to(REPO_ROOT)}",
        f"        {ISSUE_LOG.relative_to(REPO_ROOT)}",
        f"登记册版本（文件头声明）：{header_version}",
        "",
        "## 1. 待定项（OP）结构",
        f"OP 条目数：{len(ops)}",
        f"编号唯一数：{len(uniq)}",
        f"编号范围：OP-{min(uniq):02d} ~ OP-{max(uniq):02d}",
        f"编号连续性：{'PASS 无缺号' if not missing else f'FAIL 缺号 {missing}'}",
        f"编号唯一性：{'PASS 无重复' if not dupes else f'FAIL 重复 {dupes}'}",
        f"登记册总行数：{len(text.splitlines())}",
        "",
        "## 2. 状态分布（从表格行现场统计）",
        *[f"  {state:14s} {status_counts.get(state, 0):>4d}" for state in _STATUSES],
        f"  {'未识别':14s} {status_counts.get('?', 0):>4d}"
        f"{'  <- 应为 0；非 0 说明有行缺状态列' if status_counts.get('?', 0) else ''}",
        f"  合计           {sum(status_counts.values()):>4d}",
        "",
        "## 3. 分类看板核对（声明条数 vs 编号段实际覆盖）",
        f"  {'类别':28s} {'声明':>5s} {'实际':>5s}  判定",
    ]
    for name in declared_by_class:
        decl = declared_by_class[name]
        act = actual_by_class.get(name, -1)
        verdict = "OK" if decl == act else f"MISMATCH（差 {decl - act}）"
        lines.append(f"  {name:28s} {decl:>5d} {act:>5d}  {verdict}")
    lines += [
        f"  {'合计':28s} {declared_total:>5d} {len(uniq):>5d}  "
        f"{'OK' if declared_total == len(uniq) else 'MISMATCH'}",
        "",
        "## 3b. 分类看板优先级分布核对（声明四列 vs 表格行实测）",
        f"  {'类别':28s} {'声明(P0/P1/P2/P3)':>20s} {'实际(P0/P1/P2/P3)':>20s}  判定",
    ]
    for name, ids_cell, *_ in board_rows:
        wanted: set[int] = set()
        for a, b in re.findall(r"OP-(\d+)\s*~\s*OP-(\d+)", ids_cell):
            wanted |= set(range(int(a), int(b) + 1))
        for m in re.finditer(r"OP-(\d+)", ids_cell):
            wanted.add(int(m.group(1)))
        hit = {f"OP-{n:02d}" for n in wanted if n in set(uniq)}
        act = tuple(
            sum(1 for oid in hit if level_by_op.get(oid) == lv) for lv in _LEVELS
        )
        decl = next(
            (t[1] for t in level_mismatch if t[0] == name.strip()),
            None,
        )
        shown = decl if decl is not None else act
        lines.append(
            f"  {name.strip():28s} {str(shown):>20s} {str(act):>20s}  "
            f"{'MISMATCH' if decl is not None else 'OK'}"
        )
    if board_total is not None:
        decl_total = tuple(int(x) for x in board_total.groups()[2:])
        act_total = tuple(level_dist.get(lv, 0) for lv in _LEVELS)
        lines.append(
            f"  {'合计':28s} {str(decl_total):>20s} {str(act_total):>20s}  "
            f"{'OK' if decl_total == act_total else 'MISMATCH'}"
        )
    lines += [
        f"  优先级分布实测：🔴 {level_dist.get(_LEVELS[0], 0)} / "
        f"🟠 {level_dist.get(_LEVELS[1], 0)} / "
        f"🟡 {level_dist.get(_LEVELS[2], 0)} / "
        f"🟢 {level_dist.get(_LEVELS[3], 0)} / 未识别 {level_dist.get('?', 0)}",
        "",
        "## 4. 问题记录（ISS）",
        f"数据源条目数：{len(issues)}",
        "  severity：" + " / ".join(f"{k} {issue_sev.get(k, 0)}" for k in ("CRITICAL", "MAJOR", "MINOR")),
        "  status：" + " / ".join(f"{k} {issue_status.get(k, 0)}" for k in ("OPEN", "MITIGATED", "CLOSED")),
        "",
        "## 5. §3.1 按责任方 核对（声明条数 vs 编号列展开 / 幽灵 / 孤儿）",
        f"  {'责任方':44s} {'声明':>4s} {'展开':>4s}  判定",
    ]
    for name, declared, ids in owner_rows:
        hit = len(ids & set(uniq))
        verdict = "OK" if declared == hit else f"MISMATCH（差 {declared - hit}）"
        lines.append(f"  {name:44s} {declared:>4d} {hit:>4d}  {verdict}")
    lines += [
        f"  {'分项之和（不互斥，允许 > 合计）':44s} {'':>4s} {owner_sum:>4d}  "
        f"{'OK' if not owner_line_bad else '见逐行'}",
        f"  幽灵编号（编号列写了但表里没有）："
        f"{'无' if not owner_ghosts else owner_ghosts}",
        f"  孤儿 OP（任何责任方行都没提到）："
        f"{'无' if not owner_orphans else [f'OP-{n:02d}' for n in owner_orphans]}",
        f"  逐行声明一致的判定："
        f"{'PASS 全部一致' if not owner_line_bad else f'FAIL {len(owner_line_bad)} 行'}"
        f"{'' if not owner_line_bad else ' ' + str(owner_line_bad)}",
        "",
        "## 6. §3.2 按目标时机 分桶核对（自由文本 → 固定桶，声明 vs 实测）",
        f"  {'时机桶':32s} {'声明':>5s} {'实测':>5s}  判定",
    ]
    for bucket in [b for b, _ in _TIME_BUCKET_RULES] + [_TIME_FALLBACK, _TIME_UNCLASSIFIED]:
        decl = time_declared.get(bucket, -1)
        act = time_actual.get(bucket, 0)
        if bucket == _TIME_UNCLASSIFIED:
            verdict = "OK 应为 0" if act == 0 else f"FAIL 有 {act} 条无法归类"
        elif decl < 0:
            verdict = f"FAIL 文档缺此桶（实测 {act} 条）"
        else:
            verdict = "OK" if decl == act else f"MISMATCH（差 {decl - act}）"
        lines.append(f"  {bucket:32s} {decl if decl >= 0 else '-':>5} {act:>5d}  {verdict}")
    lines += [
        f"  {'分项之和 == 合计':32s} {'':>5s} {sum(time_actual.values()):>5d}  "
        f"{'OK' if time_sum_ok else f'FAIL（合计 {len(uniq)}）'}",
        f"  文档声明的合计（§1.1 看板）：{time_declared_total}"
        f"{'  OK' if time_declared_total == len(uniq) else '  FAIL 与实测 OP 数不符'}",
        f"  归一后不同取值数：{len({_norm_time(c[5]) for _, c in ops if len(c) > 5})}"
        f"（归一前 {len({c[5] for _, c in ops if len(c) > 5})} 种）",
        "",
        "## 7. 结论",
        f"RESULT={'PASS' if ok else 'FAIL'}  "
        f"（OP {len(uniq)} 条 / ISS {len(issues)} 条 / "
        f"看板条数一致={'是' if not board_mismatch and declared_total == len(uniq) else '否'} / "
        f"看板优先级一致={'是' if not level_mismatch else '否'} / "
        f"责任方表一致={'是' if not (owner_line_bad or owner_ghosts or owner_orphans) else '否'} / "
        f"目标时机分桶一致={'是' if not time_bucket_mismatch and time_unclassified == 0 and time_sum_ok else '否'}）",
    ]
    return _write("op-register.log", lines), ok


def collect_issue_stats() -> tuple[Path, bool]:
    stats_code, stats_out = _run([PYTHON, str(LOG_ISSUE), "--stats"], cwd=REPO_ROOT)
    check_code, check_out = _run([PYTHON, str(LOG_ISSUE), "--check"], cwd=REPO_ROOT)

    issues = [
        json.loads(line)
        for line in ISSUE_LOG.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    lines = [
        "# 问题记录（issue-log.jsonl）统计与门禁证据",
        f"# 生成时间: {_now()}",
        "# 命令: python tools/issues/log_issue.py --stats / --check",
        "",
        "## [1] --stats",
        f"$ python tools/issues/log_issue.py --stats  ->  exit {stats_code}",
        stats_out,
        "",
        "## [2] --check 门禁（合法应退出 0，且 error=0）",
        f"$ python tools/issues/log_issue.py --check  ->  exit {check_code}",
        check_out,
        "",
        "## [3] 条目数交叉核对",
        f"jsonl 实际行数：{len(issues)}",
        f"编号范围：{issues[0]['id']} ~ {issues[-1]['id']}（按追加顺序）",
        "",
        "## [4] 结论",
        f"RESULT={'PASS' if check_code == 0 else 'FAIL'}  "
        f"（{len(issues)} 条，check exit={check_code}）",
    ]
    return _write("issue-log-stats.log", lines), check_code == 0


def main() -> int:
    results: list[tuple[str, bool]] = []
    for name, func in (
        ("op-register.log", collect_op_register),
        ("issue-log-stats.log", collect_issue_stats),
    ):
        path, ok = func()
        results.append((name, ok))
        print(f"{'OK  ' if ok else 'FAIL'} {path.relative_to(REPO_ROOT)}")

    failed = [name for name, ok in results if not ok]
    print(f"\nRESULT={'PASS' if not failed else 'FAIL'}  产出 {len(results)} 份，失败 {len(failed)} 份")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
