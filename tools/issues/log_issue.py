#!/usr/bin/env python3
"""开发过程问题记录工具（待定项与问题登记册 §4 的配套工具）。

数据源：docs/issues/issue-log.jsonl（每行一条 JSON，追加安全、可 diff、可校验）
人读视图：docs/待定项与问题登记册.md §4.3 的渲染区块（由本脚本 --render 生成）

用法：
    # 追加一条问题记录（自动分配编号，写完后自动同步登记册渲染区）
    python log_issue.py --new \\
        --scope WP10 --title "一句话现象" \\
        --symptom "观察到的现象与报错" \\
        --root-cause "根因（不是现象复述）" \\
        --resolution "实际处理" \\
        --lesson "下次能照做的动作" \\
        --severity MAJOR --status CLOSED \\
        --evidence evidence/m1/wp10.log --refs OP-69,OP-70

    python log_issue.py --check      # 校验数据源合法性（CI 门禁，0=通过）
    python log_issue.py --render     # 只刷新登记册渲染区
    python log_issue.py --stats      # 打印统计
    python log_issue.py --selftest   # 内置自测
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import json
import re
import sys
from contextlib import contextmanager
from pathlib import Path

# ---------------------------------------------------------------- 常量

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG = REPO_ROOT / "docs" / "issues" / "issue-log.jsonl"
DEFAULT_DOC = REPO_ROOT / "docs" / "待定项与问题登记册.md"

BLOCK_BEGIN = "<!-- ISSUES:RENDER:BEGIN -->"
BLOCK_END = "<!-- ISSUES:RENDER:END -->"

#: §4.3 标题里的编号区间，形如 `### 4.3 已发生问题登记（ISS-01 ~ ISS-141）`。
#:
#: **为什么要在工具里管它**（ISS-151）：`replace_block()` 只替换 BEGIN/END 之间的
#: 文本，而标题在标记**之外** —— 于是标题里的区间永远不会被 `--render` 更新，只能靠
#: 人记得改。实测它已静默漂移 3 个号（写着 `~ ISS-141`，渲染区实为 144 条），
#: 而同节的统计行（`共 **144** 条`）给出的是正确值：**同一个文件里「有标记的区块」
#: 与「标记外的元信息」命运不同**，后者无人看管。
#:
#: 这正是铁律第 14 条（覆盖范围由人写 → 必须改由磁盘决定）的又一次复发，
#: 这次的"覆盖范围"是**编号区间**。两处一起管：`sync_title()` 负责写对，
#: `_check_title_range()` 负责在 `--check` 里对账（写错也能被 CI 拦住）。
_TITLE_HEADING_RE = re.compile(r"^###\s*4\.3[^\n]*$", re.M)
_TITLE_RANGE_RE = re.compile(r"ISS-\d{2,}\s*~\s*ISS-\d{2,}")

REQUIRED_FIELDS = (
    "id", "date", "scope", "title", "symptom",
    "root_cause", "resolution", "lesson", "severity", "status",
)
OPTIONAL_FIELDS = ("evidence", "refs")

VALID_SEVERITY = ("CRITICAL", "MAJOR", "MINOR")
VALID_STATUS = ("OPEN", "MITIGATED", "CLOSED")

# 编号必须两位起（ISS-01），三位自然延展（ISS-100）——
# 放宽到 \d+ 会放行 ISS-1，与 ISS-01 数值相同却字符串不同，破坏连续性判定
ID_RE = re.compile(r"^ISS-(\d{2,})$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 渲染表格的列顺序（与文档 §4.3 保持一致）
RENDER_COLUMNS = (
    "编号", "日期", "范围", "标题", "严重度", "状态",
    "根因", "处理", "教训", "关联", "证据",
)


# ---------------------------------------------------------------- 基础


def issue(level: str, code: str, message: str) -> dict:
    """构造一条校验问题（level: error | warn）。"""
    return {"level": level, "code": code, "message": message}


def summarise(text: str, limit: int = 140) -> str:
    """压缩空白并截断，用于日志输出。"""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def escape_cell(value) -> str:
    """转义 Markdown 表格单元格：竖线转义、换行转 <br>。"""
    if value is None:
        return "—"
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value)
    text = str(value).strip()
    if not text:
        return "—"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    text = text.replace("\n", "<br>")
    return text


# ---------------------------------------------------------------- 读取与校验


def load_records(text: str) -> tuple[list[dict], list[dict]]:
    """解析 JSONL 文本，返回 (records, issues)。行号从 1 开始。"""
    records: list[dict] = []
    issues: list[dict] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            issues.append(issue("error", "json_invalid",
                                "第 {} 行 JSON 解析失败：{}".format(line_no, exc.msg)))
            continue
        if not isinstance(obj, dict):
            issues.append(issue("error", "not_object",
                                "第 {} 行不是 JSON 对象".format(line_no)))
            continue
        records.append(obj)
    return records, issues


def validate_records(records: list[dict]) -> list[dict]:
    """校验必填字段、取值域、ID 连续性与唯一性。"""
    issues: list[dict] = []
    seen: dict[str, int] = {}

    for idx, rec in enumerate(records, start=1):
        rid = str(rec.get("id", "")).strip()
        where = "第 {} 条记录{}".format(idx, "（{}）".format(rid) if rid else "")

        for field in REQUIRED_FIELDS:
            value = rec.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                issues.append(issue("error", "field_missing",
                                    "{} 缺少必填字段 `{}`".format(where, field)))

        for field in rec:
            if field not in REQUIRED_FIELDS and field not in OPTIONAL_FIELDS:
                issues.append(issue("warn", "field_unknown",
                                    "{} 出现未定义字段 `{}`".format(where, field)))

        if rid:
            if not ID_RE.match(rid):
                issues.append(issue("error", "id_format",
                                    "{} 编号格式非法（应为 ISS-01 形式）".format(where)))
            elif rid in seen:
                issues.append(issue("error", "id_duplicate",
                                    "编号 {} 重复（第 {} 条与第 {} 条）".format(
                                        rid, seen[rid], idx)))
            else:
                seen[rid] = idx

        date = str(rec.get("date", "")).strip()
        if date and not DATE_RE.match(date):
            issues.append(issue("error", "date_format",
                                "{} 日期格式非法（应为 YYYY-MM-DD）：{}".format(where, date)))

        severity = str(rec.get("severity", "")).strip()
        if severity and severity not in VALID_SEVERITY:
            issues.append(issue("error", "severity_invalid",
                                "{} severity 非法：{}（合法值 {}）".format(
                                    where, severity, "/".join(VALID_SEVERITY))))

        status = str(rec.get("status", "")).strip()
        if status and status not in VALID_STATUS:
            issues.append(issue("error", "status_invalid",
                                "{} status 非法：{}（合法值 {}）".format(
                                    where, status, "/".join(VALID_STATUS))))

        symptom = str(rec.get("symptom", "")).strip()
        root_cause = str(rec.get("root_cause", "")).strip()
        if symptom and root_cause and symptom == root_cause:
            issues.append(issue("error", "root_cause_equals_symptom",
                                "{} 的 root_cause 与 symptom 完全相同，视为未做根因分析".format(where)))

        refs = rec.get("refs")
        if refs is not None and not isinstance(refs, list):
            issues.append(issue("error", "refs_not_list",
                                "{} 的 refs 必须是数组".format(where)))
        elif rid and isinstance(refs, list) and rid in refs:
            # ★ **自引用**（2026-10-07 WP26 收口实测，见 ISS-185 的连带发现）：
            # 一条记录把**自己的编号**写进 refs —— 实测全库 2 处（ISS-179 / ISS-185），
            # 两处都是登记时顺手把"本编号"也列进去。它没有任何信息量（"我与我相关"
            # 恒真），却让"refs 是指向别处的引用"这条语义悄悄失效；而 refs 是本项目
            # 唯一的"跨条目关联"字段，一旦允许自引用，将来若有判据统计"被引用次数"，
            # 每条自引用的记录都会给自己虚增一次（ISS-117 / ISS-169 同族：**弱引用
            # 无判据**）。此前 `validate_records` 只校验 refs 是数组，不校验指向谁。
            issues.append(issue("error", "refs_self_reference",
                                "{} 的 refs 引用了自己（{}），自引用无信息量，请删除".format(
                                    where, rid)))

    issues.extend(_check_id_continuity(records))
    return issues


def _check_id_continuity(records: list[dict]) -> list[dict]:
    """编号必须从 ISS-01 起连续无缺号。"""
    numbers = []
    for rec in records:
        match = ID_RE.match(str(rec.get("id", "")).strip())
        if match:
            numbers.append(int(match.group(1)))
    if not numbers:
        return []
    numbers.sort()
    if len(set(numbers)) != len(numbers):
        return []  # 重复已由 id_duplicate 单独报告
    expected = list(range(1, len(numbers) + 1))
    if numbers != expected:
        missing = sorted(set(expected) - set(numbers))
        return [issue("error", "id_gap",
                      "编号不连续，缺号：{}".format(
                          ", ".join("ISS-{:02d}".format(n) for n in missing)))]
    return []


def next_id(records: list[dict]) -> str:
    """分配下一个编号。"""
    highest = 0
    for rec in records:
        match = ID_RE.match(str(rec.get("id", "")).strip())
        if match:
            highest = max(highest, int(match.group(1)))
    return "ISS-{:02d}".format(highest + 1)


def sort_records(records: list[dict]) -> list[dict]:
    """按编号数值升序排序。"""

    def key(rec: dict):
        match = ID_RE.match(str(rec.get("id", "")).strip())
        return int(match.group(1)) if match else 10 ** 9

    return sorted(records, key=key)


# ---------------------------------------------------------------- 渲染


def render_markdown(records: list[dict]) -> str:
    """把记录渲染成 Markdown 表格（含统计行）。"""
    ordered = sort_records(records)
    lines: list[str] = []

    if not ordered:
        return "_暂无问题记录。_"

    counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for rec in ordered:
        sev = str(rec.get("severity", "")).strip() or "UNKNOWN"
        status = str(rec.get("status", "")).strip() or "UNKNOWN"
        counts[sev] = counts.get(sev, 0) + 1
        status_counts[status] = status_counts.get(status, 0) + 1

    sev_part = " / ".join(
        "{} {}".format(sev, counts.get(sev, 0)) for sev in VALID_SEVERITY
    )
    status_part = " / ".join(
        "{} {}".format(status, status_counts.get(status, 0)) for status in VALID_STATUS
    )
    lines.append("> 共 **{}** 条：{}；状态 {}。".format(len(ordered), sev_part, status_part))
    lines.append("")
    lines.append("| " + " | ".join(RENDER_COLUMNS) + " |")
    lines.append("|" + "---|" * len(RENDER_COLUMNS))

    for rec in ordered:
        row = (
            "`{}`".format(rec.get("id", "")),
            rec.get("date", ""),
            rec.get("scope", ""),
            rec.get("title", ""),
            "`{}`".format(rec.get("severity", "")),
            "`{}`".format(rec.get("status", "")),
            rec.get("root_cause", ""),
            rec.get("resolution", ""),
            rec.get("lesson", ""),
            rec.get("refs", ""),
            rec.get("evidence", ""),
        )
        lines.append("| " + " | ".join(escape_cell(cell) for cell in row) + " |")

    return "\n".join(lines)


def replace_block(doc_text: str, block_body: str) -> tuple[str, str | None]:
    """替换登记册中的渲染区块。返回 (新文本, 错误信息)。"""
    begin = doc_text.find(BLOCK_BEGIN)
    end = doc_text.find(BLOCK_END)
    if begin < 0 or end < 0:
        return doc_text, "未找到渲染区块标记 {} / {}".format(BLOCK_BEGIN, BLOCK_END)
    if end < begin:
        return doc_text, "渲染区块标记顺序颠倒（END 出现在 BEGIN 之前）"
    head = doc_text[: begin + len(BLOCK_BEGIN)]
    tail = doc_text[end:]
    return head + "\n" + block_body.strip() + "\n" + tail, None


def id_range(records: list[dict]) -> tuple[str, str] | None:
    """数据源里的编号区间 `(最小号, 最大号)`；没有合法编号时返回 None。"""
    numbers = [
        int(match.group(1))
        for rec in records
        if (match := ID_RE.match(str(rec.get("id", "")).strip()))
    ]
    if not numbers:
        return None
    return "ISS-{:02d}".format(min(numbers)), "ISS-{:02d}".format(max(numbers))


def sync_title(doc_text: str, records: list[dict]) -> tuple[str, str | None]:
    """把 §4.3 标题里的编号区间改写成数据源实测的 min ~ max。

    只改**标题行内的区间**，不动标题其余文字，也不动标记内的渲染区。
    找不到标题、或标题里本来就没有区间形态时**原样返回**（不报错）——
    这个函数是「顺手改对」，对账由 `check_title_range()` 负责，
    两者分开才能避免「改了但改错了没人发现」。
    """
    span = id_range(records)
    if span is None:
        return doc_text, None
    heading = _TITLE_HEADING_RE.search(doc_text)
    if not heading:
        return doc_text, None
    line = heading.group(0)
    if not _TITLE_RANGE_RE.search(line):
        return doc_text, None
    new_line = _TITLE_RANGE_RE.sub("{} ~ {}".format(*span), line, count=1)
    if new_line == line:
        return doc_text, None
    return doc_text[: heading.start()] + new_line + doc_text[heading.end():], None


def check_title_range(doc_text: str, records: list[dict]) -> list[dict]:
    """标题里的编号区间必须等于数据源实测区间（有区间形态时才检查）。

    这是 `sync_title()` 的**对账判据**：两者独立实现，一个负责写、一个负责查。
    只有「写」而没有「查」的话，`sync_title()` 某天被绕过或改坏，
    漂移会再次静默发生 —— 与它要修的那个病一模一样。
    """
    span = id_range(records)
    heading = _TITLE_HEADING_RE.search(doc_text)
    if not heading or span is None:
        return []
    found = _TITLE_RANGE_RE.search(heading.group(0))
    if not found:
        return []  # 标题没写区间 ⇒ 不强制（形态自由，只在写了的时候要求写对）
    expected = "{} ~ {}".format(*span)
    normalised = re.sub(r"\s+", " ", found.group(0)).strip()
    if normalised == expected:
        return []
    return [issue("error", "title_range_drift",
                  "§4.3 标题的编号区间 `{}` 与数据源实测 `{}` 不一致"
                  "（人写的覆盖范围会静默漂移，请跑 --render 或 --new 自动改写）".format(
                      normalised, expected))]


# ---------------------------------------------------------------- IO


def read_text(path: Path) -> tuple[str, str | None]:
    try:
        return path.read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return "", "文件不存在：{}".format(path)
    except OSError as exc:
        return "", "读取失败 {}：{}".format(path, exc)


def append_record(path: Path, record: dict) -> str | None:
    """以追加方式写入一条记录（不重写历史）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        if "\n" in line:
            return "序列化结果含换行，拒绝写入"
        existing, read_err = read_text(path)
        if read_err and "不存在" not in read_err:
            return read_err
        needs_leading_newline = bool(existing) and not existing.endswith("\n")
        with path.open("a", encoding="utf-8") as handle:
            if needs_leading_newline:
                handle.write("\n")
            handle.write(line + "\n")
        return None
    except OSError as exc:
        return "写入失败 {}：{}".format(path, exc)


def sync_doc(doc_path: Path, records: list[dict]) -> str | None:
    """把渲染结果同步进登记册文档（渲染区 + §4.3 标题里的编号区间）。"""
    doc_text, err = read_text(doc_path)
    if err:
        return err
    # 先改标题区间，再替换渲染区 —— 两步的顺序不影响结果（作用范围不相交），
    # 但先改标题可以让"标题写对了、渲染区写失败"这种半成品状态更容易被看见。
    new_text, err = sync_title(doc_text, records)
    if err:
        return err
    new_text, err = replace_block(new_text, render_markdown(records))
    if err:
        return err
    try:
        doc_path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        return "写回文档失败 {}：{}".format(doc_path, exc)
    return None


# ---------------------------------------------------------------- 命令


def cmd_new(args: argparse.Namespace) -> int:
    log_path = Path(args.log)
    doc_path = Path(args.doc)

    text, err = read_text(log_path)
    if err and "不存在" not in err:
        print("[ERROR] {}".format(err), file=sys.stderr)
        return 2

    supplied = {
        "scope": args.scope,
        "title": args.title,
        "symptom": args.symptom,
        "root_cause": args.root_cause,
        "resolution": args.resolution,
        "lesson": args.lesson,
        "severity": args.severity,
        "status": args.status,
    }
    missing = [key for key, value in supplied.items() if not value]
    if missing:
        print("[ERROR] 缺少必填参数：{}".format(", ".join("--" + m.replace("_", "-") for m in missing)),
              file=sys.stderr)
        return 2

    records, parse_issues = load_records(text)
    hard = [item for item in parse_issues if item["level"] == "error"]
    if hard:
        for item in hard:
            print("[ERROR] {}".format(item["message"]), file=sys.stderr)
        return 2

    record = {
        "id": next_id(records),
        "date": args.date or _dt.date.today().isoformat(),
        "scope": args.scope.strip(),
        "title": args.title.strip(),
        "symptom": args.symptom.strip(),
        "root_cause": args.root_cause.strip(),
        "resolution": args.resolution.strip(),
        "lesson": args.lesson.strip(),
        "severity": args.severity.strip(),
        "status": args.status.strip(),
    }
    if args.evidence:
        record["evidence"] = args.evidence.strip()
    if args.refs:
        record["refs"] = [item.strip() for item in args.refs.split(",") if item.strip()]

    issues = validate_records(records + [record])
    hard = [item for item in issues if item["level"] == "error"]
    if hard:
        for item in hard:
            print("[ERROR] {}".format(item["message"]), file=sys.stderr)
        return 2

    err = append_record(log_path, record)
    if err:
        print("[ERROR] {}".format(err), file=sys.stderr)
        return 2

    print("已追加 {} -> {}".format(record["id"], log_path))
    print("  标题：{}".format(summarise(record["title"], 90)))
    print("  严重度/状态：{} / {}".format(record["severity"], record["status"]))

    if not args.no_sync_doc:
        err = sync_doc(doc_path, records + [record])
        if err:
            print("[ERROR] 数据源已写入，但同步登记册失败：{}".format(err), file=sys.stderr)
            return 2
        print("已同步登记册渲染区 -> {}".format(doc_path))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    text, err = read_text(Path(args.log))
    if err:
        print("[ERROR] {}".format(err), file=sys.stderr)
        return 2

    records, issues = load_records(text)
    issues.extend(validate_records(records))

    # 跨文件对账（ISS-151）：标题里的编号区间是**标记之外**的人写文本，
    # 渲染工具管不到它；只有把它与数据源对上，漂移才不会静默。
    doc_text, doc_err = read_text(Path(args.doc))
    if doc_err and "不存在" not in doc_err:
        issues.append(issue("error", "doc_unreadable",
                            "登记册读不到，无法对账 §4.3 标题区间：{}".format(doc_err)))
    elif doc_text:
        issues.extend(check_title_range(doc_text, records))

    errors = [item for item in issues if item["level"] == "error"]
    warns = [item for item in issues if item["level"] == "warn"]

    if not args.quiet:
        for item in errors:
            print("[ERROR] {}".format(item["message"]), file=sys.stderr)
        for item in warns:
            print("[WARN] {}".format(item["message"]))

    print("")
    print("问题记录校验：{} 条记录 / {} error / {} warn".format(len(records), len(errors), len(warns)))
    print("CHECK {}（{} PASS / {} FAIL）".format(
        "PASS" if not errors else "FAIL", len(records), len(errors)))
    return 1 if errors else 0


def cmd_render(args: argparse.Namespace) -> int:
    text, err = read_text(Path(args.log))
    if err:
        print("[ERROR] {}".format(err), file=sys.stderr)
        return 2
    records, issues = load_records(text)
    hard = [item for item in issues if item["level"] == "error"] + \
           [item for item in validate_records(records) if item["level"] == "error"]
    if hard:
        for item in hard:
            print("[ERROR] {}".format(item["message"]), file=sys.stderr)
        print("[ERROR] 数据源不合法，拒绝渲染", file=sys.stderr)
        return 2

    err = sync_doc(Path(args.doc), records)
    if err:
        print("[ERROR] {}".format(err), file=sys.stderr)
        return 2
    print("已渲染 {} 条记录 -> {}".format(len(records), args.doc))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    text, err = read_text(Path(args.log))
    if err:
        print("[ERROR] {}".format(err), file=sys.stderr)
        return 2
    records, _ = load_records(text)
    print("问题记录总计：{} 条".format(len(records)))
    for field, domain in (("severity", VALID_SEVERITY), ("status", VALID_STATUS)):
        parts = []
        for value in domain:
            count = sum(1 for rec in records if rec.get(field) == value)
            parts.append("{} {}".format(value, count))
        print("  {}：{}".format(field, " / ".join(parts)))
    scopes: dict[str, int] = {}
    for rec in records:
        key = str(rec.get("scope", "—"))
        scopes[key] = scopes.get(key, 0) + 1
    print("  范围分布：{}".format(
        " / ".join("{} {}".format(k, v) for k, v in sorted(scopes.items())) or "—"))
    return 0


# ---------------------------------------------------------------- 自测


def run_selftest() -> int:
    passed = 0
    failed = 0

    def check(name: str, condition: bool) -> None:
        nonlocal passed, failed
        if condition:
            passed += 1
            print("  PASS  {}".format(name))
        else:
            failed += 1
            print("  FAIL  {}".format(name))

    print("== 问题记录工具自测 ==")

    def make(**overrides) -> dict:
        base = {
            "id": "ISS-01",
            "date": "2026-09-21",
            "scope": "WP10",
            "title": "标题",
            "symptom": "现象描述",
            "root_cause": "根因描述",
            "resolution": "处理方式",
            "lesson": "下次动作",
            "severity": "MAJOR",
            "status": "CLOSED",
        }
        base.update(overrides)
        return base

    # --- 读取
    records, issues = load_records('{"id":"ISS-01"}\n\n{"id":"ISS-02"}\n')
    check("空行被忽略且记录数正确", len(records) == 2 and not issues)

    records, issues = load_records("{bad json}\n")
    check("非法 JSON 被报错", not records and issues and issues[0]["code"] == "json_invalid")

    records, issues = load_records("[1,2,3]\n")
    check("非对象行被报错", not records and issues and issues[0]["code"] == "not_object")

    # --- 校验：通过
    check("合法记录校验通过", not validate_records([make()]))

    # --- 校验：必填
    rec = make()
    del rec["lesson"]
    codes = {item["code"] for item in validate_records([rec])}
    check("缺必填字段被报错", "field_missing" in codes)

    rec = make(lesson="   ")
    codes = {item["code"] for item in validate_records([rec])}
    check("空白字符串等同缺失", "field_missing" in codes)

    # --- 校验：编号
    codes = {item["code"] for item in validate_records([make(), make()])}
    check("编号重复被报错", "id_duplicate" in codes)

    codes = {item["code"] for item in validate_records(
        [make(id="ISS-01"), make(id="ISS-03")])}
    check("编号缺号被报错", "id_gap" in codes)

    codes = {item["code"] for item in validate_records(
        [make(id="ISS-01"), make(id="ISS-02")])}
    check("编号连续不报缺号", "id_gap" not in codes)

    codes = {item["code"] for item in validate_records([make(id="ISS-1")])}
    check("编号格式非法被报错（缺前导零）", "id_format" in codes)

    codes = {item["code"] for item in validate_records([make(id="BUG-01")])}
    check("编号前缀非法被报错", "id_format" in codes)

    # --- 校验：取值域
    codes = {item["code"] for item in validate_records([make(severity="BLOCKER")])}
    check("severity 非法值被报错", "severity_invalid" in codes)

    codes = {item["code"] for item in validate_records([make(status="DONE")])}
    check("status 非法值被报错", "status_invalid" in codes)

    codes = {item["code"] for item in validate_records([make(date="2026/09/21")])}
    check("日期格式非法被报错", "date_format" in codes)

    # --- 校验：根因分析质量
    codes = {item["code"] for item in validate_records(
        [make(symptom="一样的话", root_cause="一样的话")])}
    check("根因与现象雷同被报错", "root_cause_equals_symptom" in codes)

    # --- 校验：未知字段（warn 不阻断）
    issues = validate_records([make(owner="rednote")])
    check("未知字段只告警不报错",
          any(i["code"] == "field_unknown" and i["level"] == "warn" for i in issues)
          and not any(i["level"] == "error" for i in issues))

    # --- refs 类型
    issues = validate_records([make(refs="OP-69")])
    check("refs 非数组被报错", any(i["code"] == "refs_not_list" for i in issues))

    # --- refs 自引用（ISS-185 连带发现：全库实测 2 处，登记时顺手写进了本编号）
    #     成对：引用了**自己**必须报错；引用了**别人**必须不报错
    #     （只测前者的话，一个"凡 refs 非空就报错"的退化实现也能通过 —— ISS-115 同族）。
    issues = validate_records([make(id="ISS-07", refs=["ISS-07"])])
    check("refs 自引用被报错", any(i["code"] == "refs_self_reference" for i in issues))
    issues = validate_records([make(id="ISS-07", refs=["ISS-03", "OP-69"])])
    check("refs 引用他人不报错",
          not any(i["code"] == "refs_self_reference" for i in issues))

    # --- 编号分配
    check("空列表分配 ISS-01", next_id([]) == "ISS-01")
    check("已有 ISS-01 则分配 ISS-02", next_id([make()]) == "ISS-02")
    check("跳过缺号按最大号续排",
          next_id([make(id="ISS-05"), make(id="ISS-02")]) == "ISS-06")
    check("不合法编号不影响分配", next_id([make(id="BAD")]) == "ISS-01")

    # --- 渲染
    md = render_markdown([make()])
    check("渲染包含统计行", "共 **1** 条" in md)
    check("渲染包含表头", "| 编号 | 日期 |" in md)
    check("渲染包含编号", "`ISS-01`" in md)
    check("空记录渲染占位文案", render_markdown([]) == "_暂无问题记录。_")

    md = render_markdown([make(title="含|竖线")])
    check("竖线被转义", "含\\|竖线" in md)

    md = render_markdown([make(title="第一行\n第二行")])
    check("换行转 <br>", "第一行<br>第二行" in md)

    md = render_markdown([make(evidence="", refs=[])])
    check("空证据与空关联显示为破折号", md.count("| — |") >= 1)

    md = render_markdown([make(id="ISS-02"), make(id="ISS-01")])
    check("渲染按编号升序", md.index("`ISS-01`") < md.index("`ISS-02`"))

    # --- 区块替换
    doc = "前文\n{}\n旧内容\n{}\n后文".format(BLOCK_BEGIN, BLOCK_END)
    new_doc, err = replace_block(doc, "新内容")
    check("区块替换成功", err is None and "新内容" in new_doc
          and "旧内容" not in new_doc and new_doc.startswith("前文")
          and new_doc.endswith("后文"))

    new_doc, err = replace_block("没有标记", "x")
    check("缺标记时报错", err is not None and new_doc == "没有标记")

    new_doc, err = replace_block(
        "{}\n{}\n".format(BLOCK_END, BLOCK_BEGIN), "x")
    check("标记顺序颠倒时报错", err is not None)

    # --- §4.3 标题编号区间（ISS-151）
    check("区间取数据源 min/max",
          id_range([make(id="ISS-01"), make(id="ISS-144"), make(id="ISS-77")])
          == ("ISS-01", "ISS-144"))
    check("空数据源无区间", id_range([]) is None)
    check("全非法编号时无区间", id_range([make(id="BAD")]) is None)

    doc = "### 4.3 已发生问题登记（ISS-01 ~ ISS-141）\n\n正文\n"
    new_doc, err = sync_title(doc, [make(id="ISS-01"), make(id="ISS-144")])
    check("标题区间被改写成实测上界",
          err is None and "ISS-01 ~ ISS-144" in new_doc and "ISS-141" not in new_doc)
    check("改写只动区间不动标题其余文字",
          new_doc.startswith("### 4.3 已发生问题登记（") and new_doc.endswith("\n\n正文\n"))

    new_doc, err = sync_title(doc, [make(id="ISS-01"), make(id="ISS-144")])
    new_doc2, err = sync_title(new_doc, [make(id="ISS-01"), make(id="ISS-144")])
    check("区间已正确时幂等", err is None and new_doc2 == new_doc)

    new_doc, err = sync_title("### 4.3 已发生问题登记\n", [make()])
    check("标题无区间形态时保持原样", err is None and new_doc == "### 4.3 已发生问题登记\n")

    new_doc, err = sync_title("没有这个标题\n", [make()])
    check("找不到标题时不报错", err is None and new_doc == "没有这个标题\n")

    drift = check_title_range(doc, [make(id="ISS-01"), make(id="ISS-144")])
    check("标题区间漂移被报 error",
          any(i["code"] == "title_range_drift" and i["level"] == "error" for i in drift))

    ok_doc = "### 4.3 已发生问题登记（ISS-01 ~ ISS-144）\n"
    check("标题区间正确时不报错",
          not check_title_range(ok_doc, [make(id="ISS-01"), make(id="ISS-144")]))

    check("标题无区间形态时不强制",
          not check_title_range("### 4.3 已发生问题登记\n", [make(id="ISS-144")]))

    print("")
    print("SELFTEST {} PASS / {} FAIL".format(passed, failed))
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="开发过程问题记录工具（数据源 + 渲染 + 校验）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="问题记录 JSONL 路径")
    parser.add_argument("--doc", default=str(DEFAULT_DOC), help="登记册文档路径")
    parser.add_argument("--quiet", action="store_true", help="不打印明细")

    parser.add_argument("--new", action="store_true", help="追加一条问题记录")
    parser.add_argument("--scope", help="所属工作包/模块，如 WP10")
    parser.add_argument("--title", help="一句话现象")
    parser.add_argument("--symptom", help="观察到的现象")
    parser.add_argument("--root-cause", dest="root_cause", help="根因")
    parser.add_argument("--resolution", help="处理方式")
    parser.add_argument("--lesson", help="沉淀出的动作")
    parser.add_argument("--severity", choices=list(VALID_SEVERITY), help="严重度")
    parser.add_argument("--status", choices=list(VALID_STATUS), help="状态")
    parser.add_argument("--evidence", help="证据文件路径")
    parser.add_argument("--refs", help="关联编号，逗号分隔")
    parser.add_argument("--date", help="日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--no-sync-doc", dest="no_sync_doc", action="store_true",
                        help="追加后不自动刷新登记册渲染区")

    parser.add_argument("--check", action="store_true", help="校验数据源")
    parser.add_argument("--render", action="store_true", help="刷新登记册渲染区")
    parser.add_argument("--stats", action="store_true", help="打印统计")
    parser.add_argument("--selftest", action="store_true", help="内置自测")
    return parser


@contextmanager
def _exclusive_lock(anchor: Path):
    """在被保护文件旁边加一把独占锁，把"读 → 改 → 写"整段串行化。

    **为什么必须做进工具里**（ISS-33）：本工具维护数据源与登记册的方式是
    "读全量 → 追加/插入 → 写回"，全程没有互斥。一次并发调用（三条 `--new`
    同时发出）的后果是：

      · **编号重复** —— 三者各自读到同一个 `max(ISS-27)`，于是都分配
        ISS-28（与 WP10/WP11 踩过的"先查再插"是同一类缺陷，
        只不过这次发生在工具自身）；
      · **文档被写坏** —— 三个进程交替写回，登记册末尾内容被重复拼接，
        其中一个字符写坏成非法 UTF-8，此后**任何读取该文件的操作用都会抛
        `UnicodeDecodeError`**，连 `--check` 都跑不起来。

    第二条是破坏性的、且比第一条难恢复得多。因此不能指望使用侧自律
    （"下次记得串行"）—— 互斥必须做在工具里。

    锁文件与被保护文件**分开**：若直接锁被保护文件，就等于要求它必须存在，
    而 `--new` 的一个合法用途恰恰是首次创建数据源。
    """
    lock_path = anchor.with_suffix(anchor.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 自测不碰任何真实文件，不需要锁。
    if args.selftest:
        return run_selftest()

    # 其余命令全部在读一致快照的前提下执行（`--check` / `--stats` 也算：
    # 它们读到"另一个进程正在写"的中间状态时会报出假错误）。
    with _exclusive_lock(Path(args.log)):
        if args.new:
            return cmd_new(args)
        if args.check:
            return cmd_check(args)
        if args.render:
            return cmd_render(args)
        if args.stats:
            return cmd_stats(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
