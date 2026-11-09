#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WP03 指标导入模板校验工具（Rednote 内容生产系统 M0）。

职责：
  1. 读入按 templates/metric_import.template.csv 表头整理的 CSV 文件；
  2. 做两类检查：
     - 字段级错误（ERROR）：格式、必填、枚举、类型、时间、主键；
     - 口径违规告警（WARNING）：缺失填 0、约数标精确、曝光混入阅读量、
       窗口缺失、自动来源无证据、重复行等；
  3. 输出行数、逐条问题（含行号与字段名）、按代码汇总；
  4. 支持 --selftest：用内置合成数据覆盖全部错误分支，输出断言计数。

依赖：仅 Python 标准库（argparse / csv / datetime / os / re / sys / tempfile）。

口径依据：docs/m0/WP03-指标口径与导入模板.md（需求说明 §12.1 / §12.3 / §12.4）
字段说明：templates/metric_import_field_guide.md

退出码：
  --file    : 0 = 无 ERROR（可有 WARNING）；1 = 存在 ERROR；2 = 用法/文件不可读
  --selftest: 0 = 全部断言通过；1 = 存在失败断言
"""

import argparse
import csv
import datetime
import io
import os
import re
import sys
import tempfile

TOOL_VERSION = "1.0"
SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# 表结构
# ---------------------------------------------------------------------------

COLUMNS = (
    "publication_key",
    "metric_code",
    "metric_scope",
    "observed_at",
    "value",
    "raw_value",
    "precision",
    "availability",
    "source_type",
    "evidence_ref",
    "note",
)

# 逐行必填（非空）的列
ROW_REQUIRED_COLUMNS = (
    "publication_key",
    "metric_code",
    "metric_scope",
    "observed_at",
    "availability",
    "source_type",
)

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------

COUNT_METRICS = frozenset((
    "VIEW_COUNT",
    "IMPRESSION_COUNT",
    "LIKE_COUNT",
    "FAVORITE_COUNT",
    "COMMENT_COUNT",
    "LINK_CLICK_PV",
    "LINK_CLICK_UV",
))

TEXT_METRICS = frozenset((
    "COMMENT_TEXT",
    "NOTE_URL",
    "NOTE_ID",
    "PUBLISHED_AT",
    "PROMOTION_INFO",
    "PUBLISH_STATUS",
))

FLAG_METRICS = frozenset((
    "IS_PROMOTED",
))

NUMERIC_METRICS = COUNT_METRICS | FLAG_METRICS
ALL_METRICS = NUMERIC_METRICS | TEXT_METRICS

METRIC_SCOPES = (
    "CUMULATIVE",
    "LAST_24H",
    "LAST_7D",
    "LAST_30D",
    "LAST_90D",
    "NOT_APPLICABLE",
    "UNKNOWN",
)

# 需求 §12.3 的六种字段状态
AVAILABILITIES = (
    "ACQUIRED",        # 已获取
    "NOT_PROVIDED",    # 未提供
    "NO_PERMISSION",   # 无权限
    "NOT_APPLICABLE",  # 不适用
    "FETCH_FAILED",    # 获取失败
    "PENDING",         # 待确认
)

# 不参与汇总的缺失状态（PENDING 单独处理）
MISSING_AVAILABILITIES = (
    "NOT_PROVIDED",
    "NO_PERMISSION",
    "NOT_APPLICABLE",
    "FETCH_FAILED",
)

SOURCE_TYPES = (
    "MANUAL",
    "CSV_IMPORT",
    "XLSX_IMPORT",
    "SCREENSHOT_OCR",
    "AUTO_API",
)

PRECISIONS = ("EXACT", "APPROXIMATE")

# 约数标记词：出现即说明 raw_value 不是精确值
APPROX_MARKERS = ("万", "千", "k", "K", "约", "余", "左右", "+")

# 点击类指标必须写明统计窗口（需求 §12.3）
CLICK_METRICS = frozenset(("LINK_CLICK_PV", "LINK_CLICK_UV"))

# 主键/引用允许的字符
KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# 中文引号（CSV 中禁止使用，会破坏解析语义）
CJK_QUOTES = ("\u201c", "\u201d", "\u2018", "\u2019")

# 观测时间过旧的告警阈值（天）
STALE_DAYS = 365


# ---------------------------------------------------------------------------
# 问题记录
# ---------------------------------------------------------------------------

class Issue(object):
    """一条校验问题。severity 为 ERROR 或 WARNING。"""

    __slots__ = ("severity", "code", "line_no", "column", "message")

    def __init__(self, severity, code, line_no, column, message):
        self.severity = severity
        self.code = code
        self.line_no = line_no
        self.column = column
        self.message = message

    def render(self):
        location = "行 {}".format(self.line_no) if self.line_no else "文件"
        column = self.column if self.column else "-"
        return "[{}] {} 字段 {} | {} | {}".format(
            self.severity, location, column, self.code, self.message
        )

    def to_dict(self):
        return {
            "severity": self.severity,
            "code": self.code,
            "line_no": self.line_no,
            "column": self.column,
            "message": self.message,
        }


def error(code, line_no, column, message):
    return Issue("ERROR", code, line_no, column, message)


def warning(code, line_no, column, message):
    return Issue("WARNING", code, line_no, column, message)


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def read_rows(path):
    """读取 CSV，返回 (header, rows, issues)。

    rows 为 [(line_no, {column: value})] 列表。
    注释行（首字符 #）、空行、BOM 均被容忍。
    """
    issues = []
    if not os.path.isfile(path):
        issues.append(error("FILE_MISSING", 0, None,
                            "文件不存在或不是普通文件：{}".format(path)))
        return None, [], issues

    try:
        with io.open(path, "r", encoding="utf-8-sig", newline="") as handle:
            raw_lines = handle.readlines()
    except (IOError, OSError, UnicodeDecodeError) as exc:
        issues.append(error("FILE_UNREADABLE", 0, None,
                            "文件无法读取：{}".format(exc)))
        return None, [], issues

    header = None
    rows = []
    skipped_comments = 0
    skipped_blanks = 0

    for index, raw_line in enumerate(raw_lines, start=1):
        stripped = raw_line.strip()
        if not stripped:
            skipped_blanks += 1
            continue
        if stripped.startswith("#"):
            skipped_comments += 1
            continue

        parsed = _parse_csv_line(raw_line, index, issues)
        if parsed is None:
            continue

        if header is None:
            header = parsed
            continue

        rows.append((index, parsed))

    if header is None:
        issues.append(error("HEADER_MISSING", 0, None, "未找到表头行（文件为空或全为注释）"))
        return None, [], issues

    header_issues = check_header(header)
    issues.extend(header_issues)

    normalized = []
    for line_no, values in rows:
        row, row_issues = normalize_row(line_no, values)
        issues.extend(row_issues)
        normalized.append((line_no, row))

    read_meta = {
        "skipped_comments": skipped_comments,
        "skipped_blanks": skipped_blanks,
        "header": header,
    }
    return read_meta, normalized, issues


def _parse_csv_line(raw_line, line_no, issues):
    """解析单行 CSV。解析失败返回 None 并记录问题。"""
    try:
        parsed = next(csv.reader([raw_line]))
    except (csv.Error, StopIteration) as exc:
        issues.append(error("ROW_PARSE_FAILED", line_no, None,
                            "CSV 行解析失败：{}".format(exc)))
        return None
    return [cell.strip() for cell in parsed]


def check_header(header):
    """校验表头列名与顺序。"""
    issues = []
    expected = list(COLUMNS)
    actual = list(header)

    if actual == expected:
        return issues

    missing = [name for name in expected if name not in actual]
    unknown = [name for name in actual if name not in expected]

    if missing:
        issues.append(error(
            "HEADER_MISSING_COLUMN", 0, None,
            "表头缺少列：{}".format(", ".join(missing)),
        ))
    if unknown:
        issues.append(error(
            "HEADER_UNKNOWN_COLUMN", 0, None,
            "表头存在未知列：{}".format(", ".join(unknown)),
        ))
    if not missing and not unknown:
        issues.append(error(
            "HEADER_ORDER", 0, None,
            "表头列名齐全但顺序与模板不一致（期望 {}）".format(", ".join(expected)),
        ))
    return issues


def normalize_row(line_no, values):
    """把一行对齐到 COLUMNS，返回 (row_dict, issues)。"""
    issues = []
    expected_count = len(COLUMNS)
    actual_count = len(values)

    if actual_count != expected_count:
        # 容忍尾部空列（多余逗号）与缺失尾部空列
        if actual_count > expected_count:
            if all(cell == "" for cell in values[expected_count:]):
                values = values[:expected_count]
            else:
                issues.append(error(
                    "COLUMN_COUNT", line_no, None,
                    "列数 {} 多于表头 {} 列（存在未转义逗号？）".format(
                        actual_count, expected_count),
                ))
                values = values[:expected_count]
        else:
            issues.append(error(
                "COLUMN_COUNT", line_no, None,
                "列数 {} 少于表头 {} 列".format(actual_count, expected_count),
            ))
            values = values + [""] * (expected_count - actual_count)

    return dict(zip(COLUMNS, values)), issues


# ---------------------------------------------------------------------------
# 单值解析辅助
# ---------------------------------------------------------------------------

def parse_int(text):
    """把文本解析为非负整数。失败返回 None。"""
    candidate = text.strip()
    if not candidate:
        return None
    if not re.match(r"^[0-9]+$", candidate):
        return None
    try:
        return int(candidate)
    except ValueError:
        return None


def parse_timestamp(text):
    """解析 ISO 8601 时间戳。要求带时区。返回 (datetime, reason)。

    reason: None 表示成功；否则为 'format' / 'no_tz'。
    """
    candidate = text.strip()
    if not candidate:
        return None, "format"
    if candidate.endswith("Z") or candidate.endswith("z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(candidate)
    except ValueError:
        return None, "format"
    if parsed.tzinfo is None:
        return None, "no_tz"
    return parsed, None


def contains_approx_marker(text):
    """判断 raw_value 是否含约数标记。"""
    for marker in APPROX_MARKERS:
        if marker in text:
            return marker
    return None


def has_cjk_quote(text):
    """判断文本是否含中文引号。"""
    for quote in CJK_QUOTES:
        if quote in text:
            return quote
    return None


def magnitude_bounds(raw_value):
    """按 raw_value 的单位给出 value 的合理量级区间。无单位返回 None。"""
    if "万" in raw_value:
        return 10000, 99999999, "万"
    if "千" in raw_value or "k" in raw_value or "K" in raw_value:
        return 1000, 99999, "千/k"
    return None


# ---------------------------------------------------------------------------
# 行级校验
# ---------------------------------------------------------------------------

def validate_row(line_no, row):
    """校验一行，返回 Issue 列表。"""
    issues = []

    # -- 0) 中文引号（任何字段） --------------------------------------------
    for name in COLUMNS:
        quote = has_cjk_quote(row.get(name, ""))
        if quote:
            issues.append(error(
                "CJK_QUOTE", line_no, name,
                "含中文引号（{}），CSV 中必须使用 ASCII 直引号".format(quote),
            ))

    # -- 1) 必填 ------------------------------------------------------------
    for name in ROW_REQUIRED_COLUMNS:
        if not row.get(name, "").strip():
            issues.append(error(
                "REQUIRED_EMPTY", line_no, name, "必填列为空",
            ))

    # -- 2) publication_key -------------------------------------------------
    key = row.get("publication_key", "").strip()
    if key:
        if not KEY_PATTERN.match(key):
            issues.append(error(
                "KEY_FORMAT", line_no, "publication_key",
                "主键格式非法（仅允许字母/数字/._-，且不以符号开头）：{}".format(key),
            ))
        if key.upper() in ("0", "NULL", "NONE", "NA", "N/A"):
            issues.append(error(
                "KEY_PLACEHOLDER", line_no, "publication_key",
                "主键为占位值，不可作为发布记录标识：{}".format(key),
            ))

    # -- 3) metric_code -----------------------------------------------------
    metric_code = row.get("metric_code", "").strip()
    metric_known = metric_code in ALL_METRICS
    if metric_code and not metric_known:
        issues.append(error(
            "METRIC_UNKNOWN", line_no, "metric_code",
            "指标代码不在词表内：{}（合法值：{}）".format(
                metric_code, ", ".join(sorted(ALL_METRICS))),
        ))

    # -- 4) metric_scope ----------------------------------------------------
    scope = row.get("metric_scope", "").strip()
    if scope and scope not in METRIC_SCOPES:
        issues.append(error(
            "SCOPE_UNKNOWN_VALUE", line_no, "metric_scope",
            "统计窗口不在词表内：{}".format(scope),
        ))
    elif scope == "UNKNOWN":
        issues.append(warning(
            "SCOPE_UNKNOWN", line_no, "metric_scope",
            "统计窗口未知，需运营确认后才可用于汇总",
        ))

    if metric_known and metric_code in CLICK_METRICS and scope in ("UNKNOWN", "NOT_APPLICABLE"):
        issues.append(warning(
            "CLICK_SCOPE_MISSING", line_no, "metric_scope",
            "点击类指标必须写明统计窗口，当前为 {}（需求 §12.3）".format(scope or "空"),
        ))
    if metric_known and metric_code in COUNT_METRICS and scope == "NOT_APPLICABLE":
        issues.append(warning(
            "COUNT_SCOPE_NOT_APPLICABLE", line_no, "metric_scope",
            "计数类指标不适用 NOT_APPLICABLE，应填 CUMULATIVE 或具体窗口",
        ))
    if metric_known and metric_code in TEXT_METRICS and scope not in ("NOT_APPLICABLE", ""):
        issues.append(warning(
            "TEXT_METRIC_SCOPE", line_no, "metric_scope",
            "文本类指标应为 NOT_APPLICABLE，当前为 {}".format(scope),
        ))

    # -- 5) observed_at -----------------------------------------------------
    observed_raw = row.get("observed_at", "").strip()
    observed_dt = None
    if observed_raw:
        observed_dt, reason = parse_timestamp(observed_raw)
        if reason == "no_tz":
            issues.append(error(
                "OBSERVED_AT_NO_TZ", line_no, "observed_at",
                "时间戳缺时区：{}（要求 ISO 8601 带偏移，如 2026-01-01T10:00:00+08:00）".format(
                    observed_raw),
            ))
        elif reason == "format":
            issues.append(error(
                "OBSERVED_AT_FORMAT", line_no, "observed_at",
                "时间戳格式非法：{}".format(observed_raw),
            ))
        else:
            now = datetime.datetime.now(datetime.timezone.utc)
            if observed_dt > now + datetime.timedelta(minutes=5):
                issues.append(error(
                    "OBSERVED_AT_FUTURE", line_no, "observed_at",
                    "观测时间在未来：{}".format(observed_raw),
                ))
            else:
                age = now - observed_dt
                if age.days > STALE_DAYS:
                    issues.append(warning(
                        "OBSERVED_AT_STALE", line_no, "observed_at",
                        "观测时间距今 {} 天（>{} 天），请确认是否为陈旧数据".format(
                            age.days, STALE_DAYS),
                    ))

    # -- 6) availability ----------------------------------------------------
    availability = row.get("availability", "").strip()
    availability_known = availability in AVAILABILITIES
    if availability and not availability_known:
        hint = ""
        if availability == "UNAVAILABLE":
            hint = "（UNAVAILABLE 是实测协议的可得性等级，不是字段状态）"
        issues.append(error(
            "AVAILABILITY_UNKNOWN", line_no, "availability",
            "字段状态不在六态词表内：{}{}".format(availability, hint),
        ))

    # -- 7) source_type -----------------------------------------------------
    source_type = row.get("source_type", "").strip()
    if source_type and source_type not in SOURCE_TYPES:
        issues.append(error(
            "SOURCE_UNKNOWN", line_no, "source_type",
            "来源类型不在词表内：{}".format(source_type),
        ))

    # -- 8) precision -------------------------------------------------------
    precision = row.get("precision", "").strip()
    if precision and precision not in PRECISIONS:
        issues.append(error(
            "PRECISION_UNKNOWN", line_no, "precision",
            "精度只能是 EXACT 或 APPROXIMATE，当前为：{}".format(precision),
        ))

    # -- 9) value / raw_value 与状态的一致性 ---------------------------------
    value_raw = row.get("value", "").strip()
    raw_value = row.get("raw_value", "").strip()
    has_value = value_raw != ""
    parsed_value = None

    if has_value:
        parsed_value = parse_int(value_raw)
        if parsed_value is None:
            if re.match(r"^-", value_raw):
                issues.append(error(
                    "VALUE_NEGATIVE", line_no, "value",
                    "值为负数：{}".format(value_raw),
                ))
            else:
                issues.append(error(
                    "VALUE_NOT_INTEGER", line_no, "value",
                    "值必须为非负整数（不带千分位/单位）：{}".format(value_raw),
                ))

    if metric_known and metric_code in TEXT_METRICS and has_value:
        issues.append(error(
            "TEXT_METRIC_HAS_VALUE", line_no, "value",
            "文本类指标 {} 的 value 必须留空，内容写入 raw_value".format(metric_code),
        ))

    if metric_known and metric_code in FLAG_METRICS and parsed_value is not None:
        if parsed_value not in (0, 1):
            issues.append(error(
                "IS_PROMOTED_RANGE", line_no, "value",
                "IS_PROMOTED 只能是 0（否）或 1（是），当前为：{}".format(value_raw),
            ))

    if availability_known and availability in MISSING_AVAILABILITIES:
        if has_value:
            if parsed_value == 0:
                issues.append(warning(
                    "VALUE_ZERO_ON_MISSING", line_no, "value",
                    "缺失不填 0：availability={} 时 value 必须留空（0 是合法的真实观测值，"
                    "与「拿不到」不可混用）".format(availability),
                ))
            else:
                issues.append(error(
                    "VALUE_ON_MISSING", line_no, "value",
                    "availability={} 时不得带值（缺失状态不参与汇总）".format(availability),
                ))

    if availability_known and availability == "ACQUIRED":
        is_numeric_metric = metric_known and metric_code in NUMERIC_METRICS
        if is_numeric_metric and not has_value:
            issues.append(error(
                "NUMERIC_METRIC_NO_VALUE", line_no, "value",
                "数值类指标 {} 在 ACQUIRED 状态下必须填 value".format(metric_code),
            ))
        # 精度只对数值类指标有意义；文本类指标（URL/标识/时间/状态）不适用
        if is_numeric_metric and not precision:
            issues.append(error(
                "PRECISION_MISSING", line_no, "precision",
                "ACQUIRED 状态下 precision 必填（EXACT / APPROXIMATE）",
            ))
        if not raw_value:
            issues.append(error(
                "RAW_VALUE_MISSING", line_no, "raw_value",
                "ACQUIRED 状态下 raw_value 必填（必须保留平台原样显示值）",
            ))
        if not row.get("evidence_ref", "").strip():
            issues.append(warning(
                "ACQUIRED_WITHOUT_EVIDENCE", line_no, "evidence_ref",
                "已获取的值建议给出证据引用（截图/导出文件/记录表）",
            ))

    if availability_known and availability == "PENDING":
        if not raw_value:
            issues.append(error(
                "PENDING_RAW_MISSING", line_no, "raw_value",
                "PENDING 状态下 raw_value 必填（待确认的值必须保留原始显示值）",
            ))

    # -- 10) 约数与精确度的矛盾 ---------------------------------------------
    marker = contains_approx_marker(raw_value) if raw_value else None
    if raw_value and precision == "EXACT" and marker:
        issues.append(warning(
            "APPROX_MARKED_EXACT", line_no, "precision",
            "raw_value 含约数标记「{}」却标 EXACT：约数不得伪装成精确计数，"
            "应标 APPROXIMATE".format(marker),
        ))
    if precision == "EXACT" and not raw_value and availability_known and availability == "ACQUIRED":
        issues.append(warning(
            "EXACT_MISSING_RAW", line_no, "raw_value",
            "标 EXACT 但 raw_value 为空，无法追溯平台原值",
        ))

    # -- 11) 量级一致性 -----------------------------------------------------
    if raw_value and parsed_value is not None:
        bounds = magnitude_bounds(raw_value)
        if bounds:
            low, high, unit = bounds
            if not (low <= parsed_value <= high):
                issues.append(error(
                    "RAW_MAGNITUDE_MISMATCH", line_no, "value",
                    "raw_value「{}」含单位「{}」，value {} 不在合理量级 [{}, {}]".format(
                        raw_value, unit, parsed_value, low, high),
                ))

    # -- 12) 曝光与阅读量的口径隔离 -----------------------------------------
    note = row.get("note", "")
    if metric_code == "VIEW_COUNT":
        if "曝光" in raw_value or "曝光" in note:
            issues.append(warning(
                "IMPRESSION_IN_VIEW", line_no, "metric_code",
                "VIEW_COUNT 的 raw_value/note 出现「曝光」：阅读量与曝光量不得混加"
                "（需求 §12.3），请改用 IMPRESSION_COUNT 或注明口径未拆分",
            ))

    # -- 13) 自动来源必须有实测证据 -----------------------------------------
    if source_type == "AUTO_API":
        if not row.get("evidence_ref", "").strip():
            issues.append(warning(
                "AUTO_WITHOUT_EVIDENCE", line_no, "evidence_ref",
                "source_type=AUTO_API 但无证据引用：未实测不得标自动（G0-11）",
            ))

    # -- 14) 证据引用格式 ---------------------------------------------------
    evidence_ref = row.get("evidence_ref", "").strip()
    if evidence_ref:
        if not REF_PATTERN.match(evidence_ref):
            issues.append(warning(
                "EVIDENCE_REF_FORMAT", line_no, "evidence_ref",
                "证据引用建议使用相对路径标识（字母/数字/._-/），"
                "避免中文、绝对路径与空格：{}".format(evidence_ref),
            ))
        if evidence_ref.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", evidence_ref):
            issues.append(warning(
                "EVIDENCE_REF_ABS_PATH", line_no, "evidence_ref",
                "证据引用为绝对路径，归档后可能失效：{}".format(evidence_ref),
            ))

    return issues


def validate_duplicates(rows):
    """检测重复行（同 key + 同 metric_code + 同 observed_at）。"""
    issues = []
    seen = {}
    for line_no, row in rows:
        key = row.get("publication_key", "").strip()
        metric_code = row.get("metric_code", "").strip()
        observed = row.get("observed_at", "").strip()
        if not key or not metric_code or not observed:
            continue
        fingerprint = (key, metric_code, observed)
        if fingerprint in seen:
            issues.append(error(
                "DUPLICATE_ROW", line_no, "publication_key",
                "与行 {} 重复（同 publication_key + metric_code + observed_at）："
                "重复写入会导致汇总重复计数".format(seen[fingerprint]),
            ))
        else:
            seen[fingerprint] = line_no
    return issues


# ---------------------------------------------------------------------------
# 顶层校验
# ---------------------------------------------------------------------------

def validate_file(path):
    """校验文件，返回结果字典。"""
    meta, rows, issues = read_rows(path)

    if meta is None:
        return {
            "path": path,
            "row_count": 0,
            "column_errors": len([i for i in issues if i.severity == "ERROR"]),
            "warning_count": 0,
            "issues": issues,
            "meta": None,
            "readable": False,
        }

    for line_no, row in rows:
        issues.extend(validate_row(line_no, row))
    issues.extend(validate_duplicates(rows))

    errors = [i for i in issues if i.severity == "ERROR"]
    warnings = [i for i in issues if i.severity == "WARNING"]

    return {
        "path": path,
        "row_count": len(rows),
        "readable": True,
        "meta": meta,
        "issues": issues,
        "error_count": len(errors),
        "warning_count": len(warnings),
    }


def count_by_code(issues):
    """按代码汇总计数，返回 [(code, severity, count)] 列表（按代码排序）。"""
    counter = {}
    for issue in issues:
        key = (issue.code, issue.severity)
        counter[key] = counter.get(key, 0) + 1
    return [(code, severity, counter[(code, severity)])
            for code, severity in sorted(counter.keys())]


def render_report(result):
    """渲染人类可读报告。"""
    lines = []
    path = result["path"]
    lines.append("=" * 68)
    lines.append("WP03 指标导入模板校验报告")
    lines.append("=" * 68)
    lines.append("工具版本    : {}（schema {}）".format(TOOL_VERSION, SCHEMA_VERSION))
    lines.append("校验文件    : {}".format(path))
    lines.append("生成时间    : {}".format(now_iso()))

    if not result["readable"]:
        lines.append("")
        lines.append("文件不可读，校验终止。")
        lines.append("")
        for issue in result["issues"]:
            lines.append(issue.render())
        return "\n".join(lines)

    meta = result["meta"]
    lines.append("表头        : {} 列".format(len(meta["header"])))
    lines.append("注释行跳过  : {}".format(meta["skipped_comments"]))
    lines.append("空行跳过    : {}".format(meta["skipped_blanks"]))
    lines.append("数据行数    : {}".format(result["row_count"]))
    lines.append("")

    codes = count_by_code(result["issues"])
    lines.append("问题合计    : ERROR {} 条 / WARNING {} 条".format(
        result.get("error_count", 0), result.get("warning_count", 0)))
    lines.append("-" * 68)

    if not result["issues"]:
        lines.append("（无问题）")
    else:
        lines.append("逐条问题：")
        for issue in result["issues"]:
            lines.append("  " + issue.render())
        lines.append("")
        lines.append("按代码汇总：")
        for code, severity, count in codes:
            lines.append("  {}  {:<28} {} 条".format(
                "ERROR  " if severity == "ERROR" else "WARNING", code, count))

    lines.append("-" * 68)
    if result.get("error_count", 0) > 0:
        lines.append("结论：不通过 —— 存在 {} 条字段级错误，必须修正后重新导入。".format(
            result["error_count"]))
    elif result.get("warning_count", 0) > 0:
        lines.append("结论：可导入但有告警 —— {} 条口径违规告警需逐条确认或修正"
                     "（需求 §12.3 / §12.4）。".format(result["warning_count"]))
    else:
        lines.append("结论：通过 —— 无错误、无告警。")
    lines.append("")
    lines.append("提示：只有 availability=ACQUIRED 的行参与汇总；其余五种状态一律排除，")
    lines.append("      且界面必须显示排除原因（需求 §12.3 / §12.4）。")
    return "\n".join(lines)


def now_iso():
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

class Checker(object):
    """断言计数器。"""

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.lines = []
        self._section = None

    def section(self, title):
        self._section = title
        self.lines.append(title)

    def ok(self, condition, message):
        if condition:
            self.passed += 1
            self.lines.append("  ok   {}".format(message))
        else:
            self.failed += 1
            self.lines.append("  FAIL {}".format(message))
        return bool(condition)

    def has_code(self, issues, code, message):
        found = any(issue.code == code for issue in issues)
        return self.ok(found, message)

    def not_has_code(self, issues, code, message):
        found = any(issue.code == code for issue in issues)
        return self.ok(not found, message)


def codes_of(issues):
    return sorted(set(issue.code for issue in issues))


def valid_row(**overrides):
    """构造一行合法数据，可覆盖任意列。"""
    row = {
        "publication_key": "PUB-2026-0001",
        "metric_code": "LIKE_COUNT",
        "metric_scope": "CUMULATIVE",
        "observed_at": "2026-01-01T10:00:00+08:00",
        "value": "120",
        "raw_value": "120",
        "precision": "EXACT",
        "availability": "ACQUIRED",
        "source_type": "MANUAL",
        "evidence_ref": "WP03-shots/F02-like.png",
        "note": "",
    }
    row.update(overrides)
    return row


def write_csv(path, header, rows):
    with io.open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


def row_to_list(row):
    return [row.get(name, "") for name in COLUMNS]


def run_selftest():
    """执行自检，返回 (passed, failed, lines)。"""
    checker = Checker()
    checker.lines.append("WP03 validate_import.py 自检开始：{}".format(now_iso()))
    tmpdir = tempfile.mkdtemp(prefix="wp03_selftest_")

    try:
        selftest_body(checker, tmpdir)
    finally:
        cleanup_dir(tmpdir)

    return checker.passed, checker.failed, checker.lines


def cleanup_dir(path):
    """尽力清理临时目录。"""
    try:
        for name in os.listdir(path):
            try:
                os.remove(os.path.join(path, name))
            except OSError:
                pass
        os.rmdir(path)
    except OSError:
        pass


def selftest_body(checker, tmpdir):
    """自检主体：覆盖解析、字段级错误、口径告警、重复、报告渲染。"""

    # ----------------------------------------------------------------- [1]
    checker.section("[1] 词表与辅助函数")
    checker.ok("万" in APPROX_MARKERS, "约数标记含「万」")
    checker.ok(len(AVAILABILITIES) == 6, "字段状态为六态")
    checker.ok("ACQUIRED" in AVAILABILITIES and "PENDING" in AVAILABILITIES,
               "六态含 ACQUIRED 与 PENDING")
    checker.ok("UNAVAILABLE" not in AVAILABILITIES,
               "UNAVAILABLE 不在字段状态词表中（它是实测协议等级）")
    checker.ok(len(COLUMNS) == 11, "模板共 11 列")
    checker.ok(parse_int("120") == 120, "parse_int 识别 120")
    checker.ok(parse_int("12,000") is None, "parse_int 拒绝千分位")
    checker.ok(parse_int("1.2万") is None, "parse_int 拒绝带单位")
    checker.ok(parse_int("") is None, "parse_int 拒绝空串")
    checker.ok(parse_int("-5") is None, "parse_int 拒绝负数")
    dt, reason = parse_timestamp("2026-01-01T10:00:00+08:00")
    checker.ok(dt is not None and reason is None, "parse_timestamp 接受带时区时间戳")
    dt, reason = parse_timestamp("2026-01-01T10:00:00")
    checker.ok(dt is None and reason == "no_tz", "parse_timestamp 识别缺时区")
    dt, reason = parse_timestamp("2026/01/01 10:00")
    checker.ok(dt is None and reason == "format", "parse_timestamp 识别格式错误")
    dt, reason = parse_timestamp("2026-01-01T10:00:00Z")
    checker.ok(dt is not None, "parse_timestamp 接受 Z 后缀")
    checker.ok(contains_approx_marker("1.2 万") == "万", "识别「万」约数标记")
    checker.ok(contains_approx_marker("100+") == "+", "识别「+」约数标记")
    checker.ok(contains_approx_marker("120") is None, "精确值无约数标记")
    checker.ok(magnitude_bounds("1.2 万")[0] == 10000, "「万」量级下界 10000")
    checker.ok(magnitude_bounds("1.5k")[0] == 1000, "「k」量级下界 1000")
    checker.ok(magnitude_bounds("120") is None, "无单位不检查量级")
    checker.ok(has_cjk_quote("\u201c12000\u201d") is not None, "识别中文引号")

    # ----------------------------------------------------------------- [2]
    checker.section("[2] 合法行不产生任何问题")
    issues = validate_row(2, valid_row())
    checker.ok(issues == [], "完全合法的行无 ERROR 也无 WARNING")
    checker.ok(codes_of(issues) == [], "合法行问题代码集为空")
    approx_ok = valid_row(value="12000", raw_value="1.2 万", precision="APPROXIMATE")
    checker.ok(validate_row(2, approx_ok) == [], "合法约数行（APPROXIMATE）无问题")
    text_ok = valid_row(metric_code="NOTE_URL", metric_scope="NOT_APPLICABLE",
                        value="", raw_value="https://example.invalid/a/b",
                        precision="", evidence_ref="WP03-shots/F09-url.png")
    checker.ok(validate_row(2, text_ok) == [], "合法文本类指标行无问题")
    flag_ok = valid_row(metric_code="IS_PROMOTED", metric_scope="NOT_APPLICABLE",
                        value="1", raw_value="是")
    checker.ok(validate_row(2, flag_ok) == [], "合法 IS_PROMOTED 行无问题")
    pending_ok = valid_row(availability="PENDING", value="12000",
                           raw_value="1.2 万", precision="APPROXIMATE")
    checker.ok(validate_row(2, pending_ok) == [], "合法 PENDING 行无问题")

    # ----------------------------------------------------------------- [3]
    checker.section("[3] 必填与枚举错误")
    issues = validate_row(2, valid_row(publication_key=""))
    checker.has_code(issues, "REQUIRED_EMPTY", "空主键报 REQUIRED_EMPTY")

    issues = validate_row(2, valid_row(publication_key="PUB,2026"))
    checker.has_code(issues, "KEY_FORMAT", "含逗号主键报 KEY_FORMAT")

    issues = validate_row(2, valid_row(publication_key="-BAD"))
    checker.has_code(issues, "KEY_FORMAT", "以符号开头主键报 KEY_FORMAT")

    issues = validate_row(2, valid_row(publication_key="N/A"))
    checker.has_code(issues, "KEY_PLACEHOLDER", "NA 占位主键报 KEY_PLACEHOLDER")

    issues = validate_row(2, valid_row(metric_code="READ_COUNT"))
    checker.has_code(issues, "METRIC_UNKNOWN", "自造指标代码报 METRIC_UNKNOWN")

    issues = validate_row(2, valid_row(metric_scope=""))
    checker.has_code(issues, "REQUIRED_EMPTY", "空统计窗口报 REQUIRED_EMPTY")

    issues = validate_row(2, valid_row(metric_scope="LAST_7D_A"))
    checker.has_code(issues, "SCOPE_UNKNOWN_VALUE", "非法窗口值报 SCOPE_UNKNOWN_VALUE")

    issues = validate_row(2, valid_row(availability=""))
    checker.has_code(issues, "REQUIRED_EMPTY", "空状态报 REQUIRED_EMPTY")

    issues = validate_row(2, valid_row(availability="UNAVAILABLE"))
    checker.has_code(issues, "AVAILABILITY_UNKNOWN", "UNAVAILABLE 报 AVAILABILITY_UNKNOWN")

    issues = validate_row(2, valid_row(availability="MISSING"))
    found = [i for i in issues if i.code == "AVAILABILITY_UNKNOWN"]
    checker.ok(found and "UNAVAILABLE" not in found[0].message,
               "普通非法状态不显示 UNAVAILABLE 提示")

    issues = validate_row(2, valid_row(source_type="CRAWLER"))
    checker.has_code(issues, "SOURCE_UNKNOWN", "非法来源类型报 SOURCE_UNKNOWN")

    issues = validate_row(2, valid_row(precision="APPROX"))
    checker.has_code(issues, "PRECISION_UNKNOWN", "非法精度值报 PRECISION_UNKNOWN")

    # ----------------------------------------------------------------- [4]
    checker.section("[4] 时间戳校验")
    issues = validate_row(2, valid_row(observed_at=""))
    checker.has_code(issues, "REQUIRED_EMPTY", "空观测时间报 REQUIRED_EMPTY")

    issues = validate_row(2, valid_row(observed_at="2026-01-01 10:00:00"))
    checker.has_code(issues, "OBSERVED_AT_NO_TZ", "缺时区报 OBSERVED_AT_NO_TZ")

    issues = validate_row(2, valid_row(observed_at="2026/01/01"))
    checker.has_code(issues, "OBSERVED_AT_FORMAT", "格式错误报 OBSERVED_AT_FORMAT")

    issues = validate_row(2, valid_row(observed_at="2999-01-01T00:00:00+08:00"))
    checker.has_code(issues, "OBSERVED_AT_FUTURE", "未来时间报 OBSERVED_AT_FUTURE")

    issues = validate_row(2, valid_row(observed_at="2000-01-01T00:00:00+08:00"))
    checker.has_code(issues, "OBSERVED_AT_STALE", "过旧时间报 OBSERVED_AT_STALE")

    # ----------------------------------------------------------------- [5]
    checker.section("[5] value / raw_value 类型与一致性")
    issues = validate_row(2, valid_row(value="12,000", raw_value="12,000"))
    checker.has_code(issues, "VALUE_NOT_INTEGER", "千分位报 VALUE_NOT_INTEGER")

    issues = validate_row(2, valid_row(value="1.2万", raw_value="1.2万"))
    checker.has_code(issues, "VALUE_NOT_INTEGER", "带单位报 VALUE_NOT_INTEGER")

    issues = validate_row(2, valid_row(value="-5", raw_value="-5"))
    checker.has_code(issues, "VALUE_NEGATIVE", "负数报 VALUE_NEGATIVE")

    issues = validate_row(2, valid_row(metric_code="NOTE_URL",
                                       metric_scope="NOT_APPLICABLE",
                                       value="https://example.invalid/a",
                                       raw_value="https://example.invalid/a"))
    checker.has_code(issues, "TEXT_METRIC_HAS_VALUE",
                     "文本类指标填 value 报 TEXT_METRIC_HAS_VALUE")

    issues = validate_row(2, valid_row(metric_code="LIKE_COUNT",
                                       metric_scope="CUMULATIVE",
                                       value="", raw_value="",
                                       precision=""))
    checker.has_code(issues, "NUMERIC_METRIC_NO_VALUE",
                     "ACQUIRED 数值类无 value 报 NUMERIC_METRIC_NO_VALUE")

    issues = validate_row(2, valid_row(value="", raw_value="", precision=""))
    checker.has_code(issues, "PRECISION_MISSING", "ACQUIRED 无精度报 PRECISION_MISSING")
    checker.has_code(issues, "RAW_VALUE_MISSING", "ACQUIRED 无原值报 RAW_VALUE_MISSING")

    text_no_precision = valid_row(metric_code="NOTE_URL",
                                  metric_scope="NOT_APPLICABLE",
                                  value="", raw_value="https://example.invalid/a/b",
                                  precision="")
    checker.not_has_code(validate_row(2, text_no_precision), "PRECISION_MISSING",
                         "文本类指标不要求 precision（精度仅对数值有意义）")

    issues = validate_row(2, valid_row(evidence_ref=""))
    checker.has_code(issues, "ACQUIRED_WITHOUT_EVIDENCE",
                     "ACQUIRED 无证据引用报 ACQUIRED_WITHOUT_EVIDENCE")

    issues = validate_row(2, valid_row(metric_code="IS_PROMOTED",
                                       metric_scope="NOT_APPLICABLE",
                                       value="2", raw_value="2"))
    checker.has_code(issues, "IS_PROMOTED_RANGE", "IS_PROMOTED=2 报 IS_PROMOTED_RANGE")

    # ----------------------------------------------------------------- [6]
    checker.section("[6] 口径违规告警（核心）")
    issues = validate_row(2, valid_row(metric_code="COMMENT_COUNT",
                                       value="0", raw_value="",
                                       precision="EXACT",
                                       availability="NO_PERMISSION"))
    checker.has_code(issues, "VALUE_ZERO_ON_MISSING",
                     "value=0 且 NO_PERMISSION 报 VALUE_ZERO_ON_MISSING")
    checker.ok(any(i.severity == "WARNING" for i in issues if i.code == "VALUE_ZERO_ON_MISSING"),
               "VALUE_ZERO_ON_MISSING 是告警而非错误")
    checker.not_has_code(issues, "VALUE_ON_MISSING",
                         "value=0 时不再重复报 VALUE_ON_MISSING")

    for state in ("NOT_PROVIDED", "NOT_APPLICABLE", "FETCH_FAILED"):
        issues = validate_row(2, valid_row(metric_code="LIKE_COUNT",
                                           value="0", raw_value="",
                                           precision="EXACT", availability=state))
        checker.has_code(issues, "VALUE_ZERO_ON_MISSING",
                         "{} 下 value=0 报 VALUE_ZERO_ON_MISSING".format(state))

    issues = validate_row(2, valid_row(value="120", availability="NO_PERMISSION"))
    checker.has_code(issues, "VALUE_ON_MISSING", "缺失状态下带非零值报 VALUE_ON_MISSING")

    issues = validate_row(2, valid_row(value="12000", raw_value="1.2 万",
                                       precision="EXACT"))
    checker.has_code(issues, "APPROX_MARKED_EXACT",
                     "约数标 EXACT 报 APPROX_MARKED_EXACT")

    for raw in ("1.2 万", "约 300", "1.5k", "100+", "3 千", "50 余"):
        issues = validate_row(2, valid_row(value="12000", raw_value=raw,
                                           precision="EXACT"))
        checker.has_code(issues, "APPROX_MARKED_EXACT",
                         "raw_value「{}」标 EXACT 被抓出".format(raw))

    issues = validate_row(2, valid_row(metric_code="VIEW_COUNT",
                                       raw_value="曝光 12000", value="12000"))
    checker.has_code(issues, "IMPRESSION_IN_VIEW",
                     "VIEW_COUNT 原值含「曝光」报 IMPRESSION_IN_VIEW")

    issues = validate_row(2, valid_row(metric_code="VIEW_COUNT",
                                       note="口径含曝光，未拆分"))
    checker.has_code(issues, "IMPRESSION_IN_VIEW",
                     "VIEW_COUNT 备注含「曝光」报 IMPRESSION_IN_VIEW")

    issues = validate_row(2, valid_row(metric_code="IMPRESSION_COUNT",
                                       note="口径含曝光，未拆分"))
    checker.not_has_code(issues, "IMPRESSION_IN_VIEW",
                         "IMPRESSION_COUNT 本身不报 IMPRESSION_IN_VIEW")

    issues = validate_row(2, valid_row(metric_scope="UNKNOWN"))
    checker.has_code(issues, "SCOPE_UNKNOWN", "窗口未知报 SCOPE_UNKNOWN")

    issues = validate_row(2, valid_row(metric_code="LINK_CLICK_PV",
                                       metric_scope="UNKNOWN", value="30",
                                       raw_value="30"))
    checker.has_code(issues, "CLICK_SCOPE_MISSING",
                     "点击类窗口未知报 CLICK_SCOPE_MISSING")

    issues = validate_row(2, valid_row(metric_code="LINK_CLICK_UV",
                                       metric_scope="NOT_APPLICABLE", value="20",
                                       raw_value="20"))
    checker.has_code(issues, "CLICK_SCOPE_MISSING",
                     "点击类窗口不适用报 CLICK_SCOPE_MISSING")

    issues = validate_row(2, valid_row(metric_code="LIKE_COUNT",
                                       metric_scope="NOT_APPLICABLE"))
    checker.has_code(issues, "COUNT_SCOPE_NOT_APPLICABLE",
                     "计数类填 NOT_APPLICABLE 报 COUNT_SCOPE_NOT_APPLICABLE")

    issues = validate_row(2, valid_row(metric_code="NOTE_ID",
                                       metric_scope="CUMULATIVE",
                                       value="", raw_value="abc123",
                                       precision=""))
    checker.has_code(issues, "TEXT_METRIC_SCOPE",
                     "文本类填真实窗口报 TEXT_METRIC_SCOPE")

    issues = validate_row(2, valid_row(value="12000", raw_value="1.2 万",
                                       precision="APPROXIMATE",
                                       source_type="AUTO_API", evidence_ref=""))
    checker.has_code(issues, "AUTO_WITHOUT_EVIDENCE",
                     "AUTO_API 无证据报 AUTO_WITHOUT_EVIDENCE")

    issues = validate_row(2, valid_row(source_type="AUTO_API"))
    checker.not_has_code(issues, "AUTO_WITHOUT_EVIDENCE",
                         "AUTO_API 有证据时不报 AUTO_WITHOUT_EVIDENCE")

    issues = validate_row(2, valid_row(availability="PENDING", value="12000",
                                       raw_value=""))
    checker.has_code(issues, "PENDING_RAW_MISSING",
                     "PENDING 无原值报 PENDING_RAW_MISSING")

    issues = validate_row(2, valid_row(value="120", raw_value="1.2 万"))
    checker.has_code(issues, "RAW_MAGNITUDE_MISMATCH",
                     "「万」与量级不符报 RAW_MAGNITUDE_MISMATCH")

    issues = validate_row(2, valid_row(value="120", raw_value="1.5k"))
    checker.has_code(issues, "RAW_MAGNITUDE_MISMATCH",
                     "「k」与量级不符报 RAW_MAGNITUDE_MISMATCH")

    issues = validate_row(2, valid_row(value="12000", raw_value="1.2 万",
                                       precision="APPROXIMATE"))
    checker.not_has_code(issues, "RAW_MAGNITUDE_MISMATCH",
                         "「万」与量级相符时不报错")

    issues = validate_row(2, valid_row(note="\u201c示例\u201d"))
    checker.has_code(issues, "CJK_QUOTE", "中文引号报 CJK_QUOTE")

    issues = validate_row(2, valid_row(evidence_ref="/Users/x/shot.png"))
    checker.has_code(issues, "EVIDENCE_REF_ABS_PATH",
                     "绝对路径证据引用报 EVIDENCE_REF_ABS_PATH")

    issues = validate_row(2, valid_row(evidence_ref="见截图"))
    checker.has_code(issues, "EVIDENCE_REF_FORMAT",
                     "无可定位信息证据引用报 EVIDENCE_REF_FORMAT")

    # ----------------------------------------------------------------- [7]
    checker.section("[7] 文件读取：注释 / 空行 / BOM / 列数 / 表头")
    path = os.path.join(tmpdir, "tolerant.csv")
    with io.open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write("# 注释行一\n")
        handle.write("\n")
        handle.write("# 注释行二\n")
        handle.write(",".join(COLUMNS) + "\n")
        handle.write("\n")
        handle.write(",".join(row_to_list(valid_row())) + "\n")
        handle.write("# 尾注释\n")
    result = validate_file(path)
    checker.ok(result["readable"], "带 BOM/注释/空行的文件可读")
    checker.ok(result["row_count"] == 1, "数据行数为 1（注释与空行被跳过）")
    checker.ok(result["meta"]["skipped_comments"] == 3, "跳过 3 行注释")
    checker.ok(result["meta"]["skipped_blanks"] == 2, "跳过 2 行空行")
    checker.ok(result["error_count"] == 0, "容错文件无 ERROR")
    checker.ok(result["warning_count"] == 0, "容错文件无 WARNING")

    path_header = os.path.join(tmpdir, "bad_header.csv")
    bad_header = list(COLUMNS)
    bad_header[4] = "amount"
    write_csv(path_header, bad_header + ["extra_col"],
              [row_to_list(valid_row())])
    result = validate_file(path_header)
    checker.has_code(result["issues"], "HEADER_MISSING_COLUMN",
                     "表头缺列报 HEADER_MISSING_COLUMN")
    checker.has_code(result["issues"], "HEADER_UNKNOWN_COLUMN",
                     "表头多列报 HEADER_UNKNOWN_COLUMN")

    path_order = os.path.join(tmpdir, "order.csv")
    write_csv(path_order, list(reversed(COLUMNS)), [list(reversed(row_to_list(valid_row())))])
    result = validate_file(path_order)
    checker.has_code(result["issues"], "HEADER_ORDER",
                     "列名齐全但顺序错报 HEADER_ORDER")

    path_count = os.path.join(tmpdir, "count.csv")
    short_row = row_to_list(valid_row())[:5]
    write_csv(path_count, list(COLUMNS), [short_row])
    result = validate_file(path_count)
    checker.has_code(result["issues"], "COLUMN_COUNT", "列数不足报 COLUMN_COUNT")

    path_long = os.path.join(tmpdir, "long.csv")
    long_row = row_to_list(valid_row()) + ["多余"]
    write_csv(path_long, list(COLUMNS), [long_row])
    result = validate_file(path_long)
    checker.has_code(result["issues"], "COLUMN_COUNT",
                     "列数过多（非尾部空列）报 COLUMN_COUNT")

    path_trailing = os.path.join(tmpdir, "trailing.csv")
    trailing_row = row_to_list(valid_row()) + ["", ""]
    write_csv(path_trailing, list(COLUMNS), [trailing_row])
    result = validate_file(path_trailing)
    checker.not_has_code(result["issues"], "COLUMN_COUNT",
                         "尾部空列被容忍，不报 COLUMN_COUNT")
    checker.ok(result["error_count"] == 0, "尾部空列文件无 ERROR")

    path_empty = os.path.join(tmpdir, "empty.csv")
    with io.open(path_empty, "w", encoding="utf-8") as handle:
        handle.write("# 只有注释\n\n")
    result = validate_file(path_empty)
    checker.has_code(result["issues"], "HEADER_MISSING",
                     "全注释文件报 HEADER_MISSING")

    result = validate_file(os.path.join(tmpdir, "not_exist.csv"))
    checker.ok(not result["readable"], "文件不存在时标记不可读")
    checker.has_code(result["issues"], "FILE_MISSING",
                     "文件不存在记 FILE_MISSING 而不抛异常")
    report = render_report(result)
    checker.ok("文件不可读" in report, "不可读文件报告含提示")

    # ----------------------------------------------------------------- [8]
    checker.section("[8] 重复行检测")
    rows = [
        (2, valid_row()),
        (3, valid_row()),
    ]
    issues = validate_duplicates(rows)
    checker.has_code(issues, "DUPLICATE_ROW", "同 key+指标+时刻报 DUPLICATE_ROW")
    checker.ok(len(issues) == 1, "重复检测只报 1 条")

    rows = [
        (2, valid_row()),
        (3, valid_row(observed_at="2026-01-01T18:00:00+08:00")),
    ]
    checker.ok(validate_duplicates(rows) == [],
               "不同观测时刻不算重复（合法快照）")

    rows = [
        (2, valid_row()),
        (3, valid_row(metric_code="FAVORITE_COUNT", value="10", raw_value="10")),
    ]
    checker.ok(validate_duplicates(rows) == [],
               "同笔记不同指标不算重复")

    rows = [
        (2, valid_row(publication_key="")),
        (3, valid_row(publication_key="")),
    ]
    checker.ok(validate_duplicates(rows) == [],
               "主键为空的行不参与重复检测（已由 REQUIRED_EMPTY 报错）")

    # ----------------------------------------------------------------- [9]
    checker.section("[9] 文件级聚合与报告渲染")
    path_mixed = os.path.join(tmpdir, "mixed.csv")
    mixed_rows = [
        row_to_list(valid_row()),
        row_to_list(valid_row(publication_key="PUB-2026-0002",
                              metric_code="COMMENT_COUNT", value="0",
                              raw_value="", precision="EXACT",
                              availability="NO_PERMISSION")),
        row_to_list(valid_row(publication_key="PUB-2026-0003",
                              metric_code="FAVORITE_COUNT",
                              value="12,000", raw_value="12,000")),
        row_to_list(valid_row(publication_key="PUB-2026-0004",
                              metric_code="VIEW_COUNT",
                              value="12000", raw_value="1.2 万",
                              precision="EXACT")),
        # 与第 1 行完全同 key + 同指标 + 同时刻 -> 第 2 条 ERROR
        row_to_list(valid_row()),
    ]
    write_csv(path_mixed, list(COLUMNS), mixed_rows)
    result = validate_file(path_mixed)
    checker.ok(result["row_count"] == 5, "混合文件数据行数为 5")
    checker.ok(result["error_count"] >= 2, "混合文件存在 >=2 条 ERROR")
    checker.ok(result["warning_count"] >= 2, "混合文件存在 >=2 条 WARNING")
    checker.has_code(result["issues"], "VALUE_ZERO_ON_MISSING",
                     "混合文件抓到缺失填 0")
    checker.has_code(result["issues"], "VALUE_NOT_INTEGER",
                     "混合文件抓到千分位")
    checker.has_code(result["issues"], "APPROX_MARKED_EXACT",
                     "混合文件抓到约数标精确")
    checker.has_code(result["issues"], "DUPLICATE_ROW",
                     "混合文件抓到重复行")

    codes = count_by_code(result["issues"])
    checker.ok(len(codes) >= 3, "按代码汇总至少 3 类")
    checker.ok(all(len(item) == 3 for item in codes), "汇总条目为三元组")
    total = sum(item[2] for item in codes)
    checker.ok(total == len(result["issues"]), "汇总计数之和等于问题总数")

    report = render_report(result)
    checker.ok("WP03 指标导入模板校验报告" in report, "报告含标题")
    checker.ok("不通过" in report, "报告反映 ERROR 结论为不通过")
    checker.ok("数据行数    : 5" in report, "报告显示数据行数")
    checker.ok("按代码汇总" in report, "报告含按代码汇总段")

    path_clean = os.path.join(tmpdir, "clean.csv")
    write_csv(path_clean, list(COLUMNS), [row_to_list(valid_row())])
    result = validate_file(path_clean)
    checker.ok(result["error_count"] == 0 and result["warning_count"] == 0,
               "干净文件无错误无告警")
    report = render_report(result)
    checker.ok("结论：通过" in report, "干净文件报告结论为通过")

    path_warn = os.path.join(tmpdir, "warn_only.csv")
    write_csv(path_warn, list(COLUMNS), [
        row_to_list(valid_row(metric_code="COMMENT_COUNT", value="0",
                              raw_value="", precision="EXACT",
                              availability="NO_PERMISSION")),
    ])
    result = validate_file(path_warn)
    checker.ok(result["error_count"] == 0, "纯告警文件无 ERROR")
    checker.ok(result["warning_count"] >= 1, "纯告警文件有 WARNING")
    report = render_report(result)
    checker.ok("可导入但有告警" in report, "纯告警文件报告结论为可导入但有告警")

    # ----------------------------------------------------------------- [10]
    checker.section("[10] 模板文件本身应只产生预期告警")
    template_path = locate_template()
    if template_path is None:
        checker.ok(False, "未找到 templates/metric_import.template.csv")
    else:
        result = validate_file(template_path)
        checker.ok(result["readable"], "模板文件可读")
        checker.ok(result["row_count"] == 2, "模板含 2 行示例数据")
        checker.ok(result["error_count"] == 0, "模板示例行无 ERROR")
        checker.has_code(result["issues"], "VALUE_ZERO_ON_MISSING",
                         "模板第 2 行演示出 VALUE_ZERO_ON_MISSING 告警")
        checker.ok(result["warning_count"] == 1,
                   "模板告警数恰为 1（仅第 2 行的故意违规）")

    # ----------------------------------------------------------------- [11]
    checker.section("[11] 幂等与可重复性")
    if template_path is not None:
        first = validate_file(template_path)
        second = validate_file(template_path)
        checker.ok(len(first["issues"]) == len(second["issues"]),
                   "两次校验问题数一致")
        checker.ok(codes_of(first["issues"]) == codes_of(second["issues"]),
                   "两次校验问题代码集一致")
        checker.ok(first["row_count"] == second["row_count"],
                   "两次校验行数一致")


def locate_template():
    """相对脚本位置定位模板文件。"""
    here = os.path.dirname(os.path.abspath(__file__))
    # tools/m0/wp03_data/ -> 项目根
    root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    candidate = os.path.join(root, "templates", "metric_import.template.csv")
    if os.path.isfile(candidate):
        return candidate
    return None


def run_selftest_command():
    """执行自检并输出结果，返回退出码。"""
    passed, failed, lines = run_selftest()
    for line in lines:
        print(line)
    print("")
    print("SELFTEST {} PASS / {} FAIL".format(passed, failed))
    print("自检结束：{}".format(now_iso()))
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="validate_import.py",
        description="WP03 指标导入模板校验工具（纯标准库）",
    )
    parser.add_argument("--file", dest="file_path",
                        help="待校验的 CSV 文件路径")
    parser.add_argument("--selftest", action="store_true",
                        help="运行内置自检（覆盖全部错误分支）")
    parser.add_argument("--quiet", action="store_true",
                        help="仅输出结论摘要")
    parser.add_argument("--json", dest="json_out",
                        help="可选：把结构化结果写入指定 JSON 文件")
    parser.add_argument("--version", action="version",
                        version="validate_import.py {} (schema {})".format(
                            TOOL_VERSION, SCHEMA_VERSION))
    return parser


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest_command()

    if not args.file_path:
        parser.error("必须提供 --file <csv>，或使用 --selftest")

    result = validate_file(args.file_path)

    if args.quiet:
        if not result["readable"]:
            print("文件不可读：{}".format(args.file_path))
            return 2
        print("行数 {} | ERROR {} | WARNING {}".format(
            result["row_count"], result["error_count"], result["warning_count"]))
    else:
        print(render_report(result))

    if args.json_out:
        write_json_report(args.json_out, result)

    if not result["readable"]:
        return 2
    return 1 if result.get("error_count", 0) > 0 else 0


def write_json_report(path, result):
    """写出结构化 JSON 报告（不引入第三方依赖，手写序列化）。"""
    import json
    payload = {
        "tool_version": TOOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso(),
        "path": result["path"],
        "readable": result["readable"],
        "row_count": result["row_count"],
        "error_count": result.get("error_count", 0),
        "warning_count": result.get("warning_count", 0),
        "issues": [issue.to_dict() for issue in result["issues"]],
    }
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        handle.write("\n")


if __name__ == "__main__":
    sys.exit(main())