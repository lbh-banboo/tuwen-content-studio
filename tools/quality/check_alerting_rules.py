#!/usr/bin/env python3
"""WP47② 告警规则判据：规则引用的指标名必须**真实可抓**。

## 这条判据补的是什么洞

计划 §4.2 的验收原文是「告警规则 `promtool check` 通过」。本机实测（2026-10-08）：

    $ promtool check rules deploy/monitoring/rules/rednote-api.yml   # 指标名拼错
    Checking deploy/monitoring/rules/rednote-api.yml
      SUCCESS: 6 rules found
    EXIT=0

⇒ **`promtool check` 不校验指标是否存在**。它只做 YAML 语法 + 表达式解析。
而 Prometheus 对"引用不存在的指标"**同样不报错**（表达式静默返回空向量），
告警永不触发、没有任何日志。

⇒ 计划写的那句验收，在**单独使用**时会给出一条假安全感：
"YAML 合法"被当成"规则生效"。本判据就是补这一层。

## 三个判据

| 判据 | 内容 | 为什么必须 |
|---|---|---|
| D1 | `promtool check rules` exit 0（语法层） | 计划硬验收项，不可省 |
| D2 | 规则 `expr` 里引用的**每个**指标名都在真实 `/metrics` 输出里 | 补 D1 的洞；指标名是契约（metrics.py 明文承诺"只能新增不能重命名"） |
| D3 | `promtool check config` exit 0 且能解析到规则文件 | scrape 配置错了，规则根本不会被加载 |

## 为什么 D2 必须"从真实输出取集合"，不能写死名单

写死 `{"up", "python_gc_*", "rednote_build_info"}` 的话，
新增埋点时忘记改这份名单 ⇒ 判据反而把**合法的**新规则判红，或者反过来
新增指标后规则引了它、判据没跟上而静默放过。
⇒ 集合一律**从运行时的 exposition 输出派生**（`_exposition_metric_names`）。

⚠️ **`collect()` 的名字与 exposition 里的名字不一致**（metrics.py docstring 已记录）：
Counter 在 `collect()` 里是 `python_gc_collections`，exposition 里是
`python_gc_collections_total`。本判据比对的是 **exposition 名**
（与 Prometheus 抓到的、与规则里写的同一口径），因此取 `# TYPE` 行的名字。

## 已知盲区（不假装覆盖）

- **不校验 PromQL 表达式语义**：`rate(foo[5m])` 里 foo 存在但类型不对
  （拿 Gauge 当 Counter）promtool 也不报，本判据也不报。
  抓这类错误的正确工具是 `promtool check rules` 在**真 Prometheus** 里加载，
  或 `promtool test rules`（本轮未建测试用例，见文档"待补清单"）。
- **不校验 `for` 子句的时长是否合理**：那是运维判断，不是机械事实。
- **抓不到真实输出时不静默跳过**：本判据在无依赖时**报错**而不是跳过
  —— 一个"跑不了所以绿"的判据等于没有判据（同 ISS-143）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

REPO: Final[Path] = Path(__file__).resolve().parents[2]
RULES_DIR: Final[Path] = REPO / "deploy" / "monitoring" / "rules"
PROM_CONFIG: Final[Path] = REPO / "deploy" / "monitoring" / "prometheus.yml"

#: 表达式里可能出现的**内建/函数/标签**不是指标名，不能拿去对账。
#:
#: ⚠️ 这份"不计入"名单同样是**人写的覆盖范围**（铁律 14）。
#: 它必须尽量小 —— 宁可漏一个真指标名进对账（那样会报红、可解释），
#: 也不要放过一个真悬空名（那样是假绿）。
#: 判据 §D4 会断言这份名单的每个条目**确实不出现在任何规则里** ——
#: 若某个条目其实是个真指标名（被误列进忽略名单），D4 立刻报红。
_IGNORE_TOKENS: Final[frozenset[str]] = frozenset(
    {
        # PromQL 聚合函数与算子
        "sum", "count", "min", "max", "avg", "stddev", "stdvar",
        "rate", "irate", "increase", "absent", "absent_over_time",
        "by", "without", "on", "ignoring", "group_left", "group_right",
        "offset", "bool",
        # 比较与逻辑算子
        "==", "!=", ">", "<", ">=", "<=", "and", "or", "unless",
    }
)

#: **Prometheus 自带指标**：由 Prometheus 自身产生，不出现在应用的 `/metrics` 里。
#:
#: ⚠️ 它们之所以必须单列，是因为本判据的 D2 拿"应用 exposition 输出"当基准，
#: 而 `up` / `scrape_*` 是服务端指标 —— 引用它们是合法的（告警规则大量使用），
#: 但用应用输出去对账会**必然误报**。曾实测踩中：`up{job="rednote-api"}` 被判成悬空。
#:
#: ⚠️ 这份名单的准确性由 D4b 兜底：**它列的每个名字都必须真的是
#: Prometheus 官方内置指标**，否则有人拿它给悬空指标开后门。
_SERVER_METRICS: Final[frozenset[str]] = frozenset(
    {
        "up",
        "scrape_duration_seconds",
        "scrape_samples_scraped",
        "scrape_samples_post_metric_relabeling",
        "scrape_series_added",
        "scrape_up",
        "prometheus_build_info",
    }
)

#: 规则文件里允许出现的、非指标名的 YAML 键（含**组级**键）。
#:
#: ⚠️ 第一版漏登记了 `groups` / `name` / `interval` / `rules`（合法的组级结构）
#: ⇒ D5 报出4 个"陌生键"假红。**判据自己的白名单不完整，也是一种假绿来源**：
#: 它会让人习惯性忽略 D5 的输出，真陌生键混进来也就没人看了。
_ALLOWED_NON_METRIC_KEYS: Final[frozenset[str]] = frozenset(
    {"groups", "name", "interval", "rules",
     "alert", "expr", "for", "labels", "annotations", "summary",
     "description", "runbook", "severity", "service"}
)

#: `# TYPE <name> <type>` 行
_TYPE_LINE: Final[re.Pattern[str]] = re.compile(r"^#\s*TYPE\s+(\S+)\s+\S+", re.MULTILINE)
#: `job="x"` 这样的标签匹配器 —— 其**值**不是指标名（曾误判出 `api` / `rednote`）。
_LABEL_MATCHER: Final[re.Pattern[str]] = re.compile(
    r"""\{[^}]*\}|\b(job|instance|service|version|environment|quantile|le)\s*=\s*("[^"]*"|'[^']*')"""
)
#: `[5m]` / `[1h30m]` 这类 range 选择器 —— 其中的 `m`/`h`/`s` 不是指标名
#: （实测踩中：`rate(...[5m])` 被切出碎片 `m`）。
_RANGE_SELECTOR: Final[re.Pattern[str]] = re.compile(r"\[[0-9smhdwy]+(?:\s*\])?")
#: `by (version)` / `without (job)` 括号里的**无引号分组标签名** ——
#: 它既不是指标名也不是标签取值（实测踩中第三形态：
#: `count(count by (version) (rednote_build_info))` 被切出 `version`）。
#: ⚠️ 只能抹掉紧跟在 `by` / `without` 之后的括号组，**不能**抹掉所有 `(...)`
#: —— 那会把真的子查询表达式一起吃掉。
_GROUP_LABEL: Final[re.Pattern[str]] = re.compile(
    r"\b(?:by|without|on|ignoring)\s*\(([^)]*)\)"
)

EXPECTED_CHECKS: Final[int] = 10


def _exposition_metric_names() -> tuple[set[str], str]:
    """真实跑一次 `/metrics` 渲染，返回 exposition 里的指标名集合。

    **必须真跑，不能读代码常量**：`REDNOTE_METRIC_NAMES` 是
    `metrics.py` 的**自述承诺**，而 Prometheus 看到的是**实际输出**。
    两者不一致时（承诺了但没注册 / 注册了但没进承诺集），
    规则可能引用到"承诺里有、实际抓不到"的名字 —— 那正是要抓的洞。
    """
    backend = REPO / "services" / "backend"
    src = backend / "src"
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(src)!r})\n"
        "from prometheus_client import REGISTRY, generate_latest\n"
        "from rednote.platform.configuration import Settings\n"
        "from rednote.platform.observability.metrics import ensure_metrics_installed\n"
        "ensure_metrics_installed(Settings(environment='alertrule-check', app_version='0.0.0'))\n"
        "print(generate_latest(REGISTRY).decode())\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=backend,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"渲染 /metrics 失败（exit {proc.returncode}）：\n{proc.stderr.strip()}"
        )
    names = set(_TYPE_LINE.findall(proc.stdout))
    if not names:
        raise RuntimeError("exposition 输出里没有任何 # TYPE 行，判据不能静默通过")
    return names, proc.stdout


def _referenced_metric_names(expr: str) -> set[str]:
    """从一个 PromQL 表达式里取出候选指标名。

    做法：先剥掉 range 选择器与标签匹配器，再抽标识符 token，
    最后减去函数/算子/标签名。
    这不是完整的 PromQL 解析器（不该自己写），而是**候选集** ——
    宁可多取（多取会报红、可解释），不可漏取（漏取 = 假绿）。

    ⚠️ **剥离顺序是刻意的**（实测踩坑，共三种形态）：
    ① 先剥 `[5m]`（range 时长）—— 否则 `rate(x[5m])` 切出碎片 `m`；
    ② 再剥 `{...}` 与 `k="v"`（标签匹配器**及其值**）—— 否则切出 `api` / `rednote`；
    ③ 再剥 `by (...)` / `without (...)`（**无引号**分组标签名）—— 否则切出 `version`。
    三者任一漏掉都会把"非指标名"误报成悬空指标，制造假红。
    """
    cleaned = _RANGE_SELECTOR.sub(" ", expr)
    cleaned = _LABEL_MATCHER.sub(" ", cleaned)
    cleaned = _GROUP_LABEL.sub(" ", cleaned)
    tokens = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_:]*", cleaned))
    return {t for t in tokens if t not in _IGNORE_TOKENS}


def _all_exprs(rules_texts: dict[Path, str]) -> list[tuple[Path, str, str]]:
    """抽出每个文件的每个 `alert:` 块的 expr。

    **按 `alert:` 切片而不是全文扫 expr**：规则文件里 `expr` 可能写成
    多行 block scalar（`|），只按行匹配会漏掉后续行。
    """
    out: list[tuple[Path, str, str]] = []
    for path, text in rules_texts.items():
        blocks = re.split(r"^\s*-\s*alert:\s*", text, flags=re.MULTILINE)
        for block in blocks[1:]:
            name = block.split("\n", 1)[0].strip()
            m = re.search(r"^\s*expr:\s*(.*?)(?=^\s*(?:for|labels|annotations|-\s*alert:)\s*|\Z)",
                          block, flags=re.MULTILINE | re.DOTALL)
            if m:
                out.append((path, name, m.group(1).strip()))
    return out


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(tag: str, ok: bool, detail: str) -> None:
        results.append((tag, ok, detail))

    if not RULES_DIR.is_dir():
        print(f"FATAL 规则目录不存在: {RULES_DIR}", file=sys.stderr)
        return 1

    rule_files = sorted(RULES_DIR.glob("*.yml")) + sorted(RULES_DIR.glob("*.yaml"))
    texts = {p: p.read_text(encoding="utf-8") for p in rule_files}
    exprs = _all_exprs(texts)

    # D0 规则文件必须存在且非空 —— **必须放在最前面**。
    #
    # ⚠️ 这是反向验证 R④ 抓出来的真缺陷（2026-10-08）：规则文件整个被删时，
    # D2「规则引用的指标名都真实可抓」**仍报绿** —— 因为文件没了就没有
    # 告警可扫，D2 的循环体一次都没执行，空集合“无悬空”。
    # ⇒ 「没东西可查」被当成「查过了没问题」，这是最典型的假绿形态
    # （与 ISS-143「没跑与跑失败同形」同族）。
    #
    # 所以：缺失必须在**任何扫描之前**就报红，而不是让下游判据自己发现。
    if not rule_files:
        check("D0 规则文件存在且非空", False,
              f"{RULES_DIR} 下没有任何 *.yml/*.yaml 规则文件 —— "
              f"告警规则被整体删除或路径漂移，D1~D8 会因'无内容可查'而全部空转")
        _report(results)
        return 1
    check(f"D0 规则文件存在且非空（{len(rule_files)} 个）", True,
          "、".join(p.name for p in rule_files))

    # D1 promtool check rules —— 计划 §4.2 的硬验收项
    promtool = shutil.which("promtool")
    if promtool is None:
        check("D1 promtool check rules 存在", False,
              "promtool 未安装 —— 计划 §4.2 的硬验收工具缺失（brew install prometheus）")
        _report(results)
        return 1
    r_fail = []
    for p in rule_files:
        rr = subprocess.run([promtool, "check", "rules", str(p)],
                            capture_output=True, text=True)
        if rr.returncode != 0:
            r_fail.append(f"{p.name}: exit={rr.returncode} {(rr.stdout + rr.stderr).strip()[:160]}")
    check(f"D1 promtool check rules 逐文件 exit 0（{len(rule_files)} 个文件）",
          not r_fail,
          f"全部通过" if not r_fail else f"{len(r_fail)} 个文件失败：" + "; ".join(r_fail))

    # D2 悬空指标名对账 —— 补 D1 的洞
    try:
        app_names, raw = _exposition_metric_names()
    except RuntimeError as exc:
        check("D2 真实 exposition 可渲染", False, str(exc))
        app_names, raw = set(), ""

    if app_names:
        # 基准 = 应用真暴露的指标 ∪ Prometheus 服务端内置指标
        baseline = app_names | _SERVER_METRICS
        dangling: dict[str, list[str]] = {}
        for path, alert_name, expr in exprs:
            for cand in _referenced_metric_names(expr):
                if cand not in baseline:
                    dangling.setdefault(cand, []).append(f"{path.name}:{alert_name}")
        #⚠️ `exprs` 为空时**不能**判绿：那是"无告警可查"而非"无悬空指标"。
        # 反向验证 R④ 实测到此形态 —— 规则文件被删 ⇒ D2 循环体零次执行 ⇒ 假绿。
        if not exprs:
            check("D2 规则引用的指标名都真实可抓", False,
                  "规则里一条 alert 都没有 ⇒ 无法核对指标名（不是'无悬空'）")
        else:
            check(
                f"D2 规则引用的指标名都真实可抓（应用面 {len(app_names)} 个 / "
                f"服务端内置 {len(_SERVER_METRICS)} 个 / 规则 {len(exprs)} 条）",
                not dangling,
                "无悬空指标名" if not dangling
                else f"悬空 {len(dangling)} 个：" + "; ".join(
                    f"{k} <- {v}" for k, v in sorted(dangling.items())),
            )

    # D3 promtool check config —— scrape 配置错了规则不会被加载
    if PROM_CONFIG.is_file():
        r3 = subprocess.run([promtool, "check", "config", str(PROM_CONFIG)],
                            capture_output=True, text=True)
        check("D3 promtool check config exit 0", r3.returncode == 0,
              f"exit={r3.returncode}; {(r3.stdout + r3.stderr).strip()[:300]}")
    else:
        check("D3 prometheus.yml 存在", False, f"缺 {PROM_CONFIG}")

    # D4 白名单不腐烂：忽略名单 / 服务端名单的条目**不得与真实指标名相交**。
    #
    # ⚠️ 本条的第一版**方向是反的**（曾实测报错 5 处）：原写法是
    # "忽略名单里的词不得出现在规则里"，但 `sum` / `rate` / `by` 这些
    # **本来就该出现在** PromQL 里 ⇒ 该检查的前提就不成立。
    # 真正要防的是"有人把一个真实存在的指标名塞进白名单，从此它的
    # 悬空不再被 D2 发现" ⇒ 判据是**集合相交**，不是"文本出现"。
    collisions = (set(_IGNORE_TOKENS) | _SERVER_METRICS) & app_names if app_names else set()
    check(f"D4 白名单不腐烂（忽略 {len(_IGNORE_TOKENS)} + 服务端 {len(_SERVER_METRICS)}）",
          not collisions,
          "无条目与应用指标名相交" if not collisions
          else f"相交 {len(collisions)} 个（有人给真指标开后门）：{sorted(collisions)}")

    # D4b 服务端名单必须真的是 Prometheus 内置指标名
    #（否则它就是个"任意名字的放行通道"，D2 会被静默架空）
    known_server = {"up", "scrape_duration_seconds", "scrape_samples_scraped",
                    "scrape_samples_post_metric_relabeling", "scrape_series_added",
                    "scrape_up", "prometheus_build_info"}
    unknown_server = _SERVER_METRICS - known_server
    check("D4b 服务端名单是 Prometheus 官方内置指标子集",
          not unknown_server,
          "全部为官方内置名" if not unknown_server else f"疑似杜撰：{sorted(unknown_server)}")

    # D5 非指标名的 YAML 键不得被误当指标（结构完整性）
    unknown_keys = set()
    for path, text in texts.items():
        for k in re.findall(r"^\s{4,}(\w+):", text, flags=re.MULTILINE):
            if k not in _ALLOWED_NON_METRIC_KEYS:
                unknown_keys.add(k)
    check("D5 规则文件无非指标名的陌生键", not unknown_keys,
          "键集合正常" if not unknown_keys else f"陌生键：{sorted(unknown_keys)}")

    # D6 告警条数下界：防止"规则文件被清空但判据仍绿"
    check(f"D6 告警条数 >= 6（本轮实测 6）", len(exprs) >= 6,
          f"当前 {len(exprs)} 条")

    # D7 每条告警必须有 severity 与 summary —— 缺了等于无法分派
    missing_fields = []
    for path, text in texts.items():
        for block in re.split(r"^\s*-\s*alert:\s*", text, flags=re.MULTILINE)[1:]:
            name = block.split("\n", 1)[0].strip()
            nxt = re.split(r"^\s*-\s*alert:\s*", block, flags=re.MULTILINE)
            body = nxt[0]
            for field in ("severity", "summary", "description"):
                if not re.search(rf"^\s*{field}:", body, flags=re.MULTILINE):
                    missing_fields.append(f"{path.name}:{name} 缺 {field}")
    check("D7 每条告警都带 severity + summary + description",
          not missing_fields,
          "字段齐全" if not missing_fields else f"缺失 {len(missing_fields)}：{missing_fields[:6]}")

    # D8 规则文件的 metrics_path 与代码常量一致（防两侧漂移）
    code_metrics_path = subprocess.run(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0, 'src');"
         "from rednote.platform.observability.metrics import METRICS_PATH;"
         "print(METRICS_PATH)"],
        capture_output=True, text=True, cwd=REPO / "services" / "backend",
    )
    if code_metrics_path.returncode != 0:
        check("D8 规则 metrics_path == 代码常量", False,
              f"读代码常量失败: {code_metrics_path.stderr.strip()[:200]}")
    else:
        code_path = code_metrics_path.stdout.strip().splitlines()[-1] if code_metrics_path.stdout.strip() else ""
        cfg = PROM_CONFIG.read_text(encoding="utf-8") if PROM_CONFIG.is_file() else ""
        m = re.search(r"metrics_path:\s*(\S+)", cfg)
        cfg_path = m.group(1) if m else ""
        check("D8 规则 metrics_path == 代码常量 METRICS_PATH",
              bool(code_path) and cfg_path == code_path,
              f"配置={cfg_path or '(未取到)'} / 代码={code_path or '(未取到)'}")

    return _finish(results)


def _report(results: list[tuple[str, bool, str]]) -> None:
    passed = sum(1 for _, ok, _ in results if ok)
    for i, (tag, ok, detail) in enumerate(results, 1):
        print(f"{'PASS' if ok else 'FAIL'}  [{i}] {tag}")
        print(f"          {detail}")
    print()
    print(f"{passed}/{len(results)} 条检查通过")
    print("RESULT=" + ("PASS" if passed == len(results) == EXPECTED_CHECKS else "FAIL"))


def _finish(results: list[tuple[str, bool, str]]) -> int:
    _report(results)
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
