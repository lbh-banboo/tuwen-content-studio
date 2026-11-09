#!/usr/bin/env python
"""WP47 ① 证据采集脚本。

本包**零表、零迁移**（可观测面不落库），因此不需要私有测试库 ——
全部证据来自**进程内可复现的运行时事实**：起一个真 app、打真请求、
读真响应、抓真 registry。

产出的文件（落在 `evidence/m5/`）：

| 文件 | 内容 |
|---|---|
| `wp47-probes.log` | 三条新路径的状态码与响应头 + `/metrics` 输出结构 + 契约不含新路径 + 依赖声明读数 |

## 为什么这些是"证据"而不是"测试的复制品"

测试（`tests/test_observability.py`）与证据脚本的**职责不同**：

- 测试回答"行为对不对"，跑在开发者机器上，失败即阻塞合并；
- 证据回答"**交付那一刻**系统长什么样"，落盘存证，供事后核对与审计。

因此本脚本**不做断言判定**（那不是它的职责），它把观察到的原始事实**原样落盘**
（状态码、Content-Type、`/metrics` 的前若干行、路径清单、依赖计数），
并给出 PASS/FAIL 计数让 `rerun_all_evidence.py` 能识别。

## 七处核对（对应 WP47 ① 的六项 MVP + 一条纪律）

| # | 核对 | 为什么它是证据 |
|---|---|---|
| 1 | `/healthz` 在**库不可达**时仍 200 | R1：liveness 不碰库。这是本包唯一可能造成生产事故的点 |
| 2 | `/readyz` 在库不可达时 503 | 第 1 条的**对照组**（否则"整条链路都坏了"也能让第 1 条绿） |
| 3 | 别名与既有路径语义一致（除 requestId） | R10：证明"复用 handler"真的生效 |
| 4 | `/metrics` 的 Content-Type 与指标名 | R3/R5：exposition 契约 + 声明与输出一致 |
| 5 | 三条新路径**不在** `app.openapi()` 里 | D3：运维面不入契约 |
| 6 | 连续多次 `create_app()` 不抛异常 | R6：幂等注册（测试夹具反复装配） |
| 7 | `pyproject` 声明数 + 磁盘导入数 | 依赖声明的**现场读数**（判据在 `check_dependency_declarations.py`） |

## 探针自身的一处坑（写在这里，避免重复踩）

**`/metrics` 的指标名在 macOS 与 Linux 上不同**：本机（macOS）实测输出
仅 `python_info` + 3 个 GC、`ProcessCollector` 产出 **0 行**（无 `/proc`），
Linux CI 上会多出 `process_*`。因此本脚本**只核对本包自己声明的指标名**，
对"自动采集器的指标清单"只核对**结构合法性**（每行是合法 exposition 语法），
不硬编码清单 —— 硬编码会让本机绿、CI 红（§1.6 R5）。

## 为什么不用 `prometheus_client.__version__`

该库**不提供** `__version__` 属性（本机实测 `AttributeError`）。版本只能由
`importlib.metadata.version("prometheus_client")` 取。本脚本把读到的版本落盘，
让"证据生成时用的是哪个版本"成为可查事实（R4）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "services" / "backend"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m5"

# 期望条数（与内联脚本里的 EXPECTED_CHECKS 保持同值）。
# 两处都要：内联脚本用它写进 RESULT 行，外层用它判定成功与否（ISS-43：
# 崩溃会伪装成"没东西可查"，因此条数不足也算失败）。
EXPECTED_CHECKS: int = 21

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

INSPECT_SCRIPT = r"""
# 内联脚本：只允许单行注释，且不能出现三引号（三引号会提前终止外层包裹，见 ISS-45）。

import logging
from importlib.metadata import version

from fastapi.testclient import TestClient

from rednote.main import create_app
from rednote.platform.configuration import Settings
from rednote.platform.observability import (
    METRICS_PATH,
    REDNOTE_METRIC_NAMES,
    REQUEST_ID_LOG_FIELD,
)

EXPECTED_CHECKS = 21

# 一个**必然无响应**的 DSN（保留端口，必拒连）。
UNREACHABLE_DSN = "postgresql+psycopg://probe:probe@127.0.0.1:1/nonexistent"


def emit(ok, label, detail):
    print(f"CHECK {'OK  ' if ok else 'FAIL'} {label}: {detail}")


print("### 环境读数")
try:
    pc_version = version("prometheus_client")
except Exception as exc:
    pc_version = f"<读取失败 {type(exc).__name__}: {exc}>"
print(f"prometheus_client 版本 = {pc_version}（注意：该库无 __version__ 属性，R4）")
print(f"METRICS_PATH = {METRICS_PATH}")
print(f"REDNOTE_METRIC_NAMES = {REDNOTE_METRIC_NAMES}")
print(f"REQUEST_ID_LOG_FIELD = {REQUEST_ID_LOG_FIELD}")

# ---------------------------------------------------------------------------
# 1) 库不可达场景：三条路径的真行为
# ---------------------------------------------------------------------------
print()
print("### 核对 1/2：库不可达时的探针语义（R1 + 对照组）")
unreachable = create_app(Settings(database_url=UNREACHABLE_DSN, environment="test"))
with TestClient(unreachable) as client:
    liveness = client.get("/healthz")
    readiness = client.get("/readyz")
    existing_liveness = client.get("/api/v1/health")
    existing_readiness = client.get("/api/v1/health/db")

emit(
    liveness.status_code == 200,
    "/healthz 库不可达仍 200（liveness 不碰库）",
    f"status={liveness.status_code}",
)
emit(
    readiness.status_code == 503,
    "/readyz 库不可达返 503（readiness）",
    f"status={readiness.status_code} code={readiness.json().get('error', {}).get('code')}",
)
emit(
    existing_liveness.status_code == 200,
    "既有 /api/v1/health 库不可达仍 200（回归）",
    f"status={existing_liveness.status_code}",
)
emit(
    existing_readiness.status_code == 503,
    "既有 /api/v1/health/db 库不可达返 503（回归）",
    f"status={existing_readiness.status_code}",
)

# ---------------------------------------------------------------------------
# 3) 别名与既有路径语义一致（除 meta.requestId）
# ---------------------------------------------------------------------------
print()
print("### 核对 3：别名与既有路径语义一致（R10）")
alias_body = client.get("/healthz").json()
existing_body = client.get("/api/v1/health").json()
alias_body["meta"].pop("requestId", None)
existing_body["meta"].pop("requestId", None)
emit(
    alias_body == existing_body,
    "/healthz 与 /api/v1/health 语义一致（除 requestId）",
    "equal" if alias_body == existing_body else f"diff: {alias_body} vs {existing_body}",
)

# ⚠️ **必须两边都查，不能只查 liveness 一侧。**
# 只对 `/healthz` 做等价断言时，"`/readyz` 其实指向了 `get_health`"这个错误
# 完全不可见 —— 而那正是最危险的一种错接：readiness 变成永真的 liveness，
# 编排平台再也摘不掉一个连不上库的实例。两条一起查才封住这个排列。
#
# ⚠️ **比较前必须剥离"易变测量值"**（首版在这里报了假红）：
# readiness 的 error.details 里带 `latencyMs`，两次请求实测分别读到 1 与 0 ——
# 它是**真测量**，不是身份标识。逐字比较会把"语义一致"误判成不一致，
# 而失败信息里 1 vs 0 的差异极不显眼，很容易被当成 flaky 忽略掉。
# 因此这里只比较**身份性内容**（code / message / dependency / reason /
# retryable / timeouts 配置），把测量值摘掉。
_VOLATILE_KEYS = {"latencyMs"}


def _strip_volatile(node):
    if isinstance(node, dict):
        return {k: _strip_volatile(v) for k, v in node.items() if k not in _VOLATILE_KEYS}
    if isinstance(node, list):
        return [_strip_volatile(v) for v in node]
    return node


ready_alias = _strip_volatile(client.get("/readyz").json())
ready_existing = _strip_volatile(client.get("/api/v1/health/db").json())
ready_alias["meta"].pop("requestId", None)
ready_existing["meta"].pop("requestId", None)
emit(
    ready_alias == ready_existing,
    "/readyz 与 /api/v1/health/db 语义一致（除 requestId 与 latencyMs）",
    "equal" if ready_alias == ready_existing else f"diff: {ready_alias} vs {ready_existing}",
)

# ---------------------------------------------------------------------------
# 4) /metrics 形态
# ---------------------------------------------------------------------------
print()
print("### 核对 4：/metrics 形态与指标名（R3 / R5）")
metrics = client.get(METRICS_PATH)
emit(
    metrics.status_code == 200,
    "/metrics 返回 200",
    f"status={metrics.status_code}",
)
emit(
    metrics.headers.get("content-type") == "text/plain; version=0.0.4; charset=utf-8",
    "/metrics Content-Type 是 Prometheus exposition",
    metrics.headers.get("content-type", ""),
)

text = metrics.text
emitted_names = set()
for line in text.splitlines():
    if not line or line.startswith("#"):
        continue
    head = line.split("{", 1)[0].split(" ", 1)[0].strip()
    if head:
        emitted_names.add(head)

declared_present = [n for n in REDNOTE_METRIC_NAMES if n in emitted_names]
emit(
    len(declared_present) == len(REDNOTE_METRIC_NAMES),
    "声明的指标名全部出现在输出里",
    f"{declared_present} / {list(REDNOTE_METRIC_NAMES)}",
)

rednote_emitted = {
    n for n in emitted_names if n.startswith("rednote_") and not n.endswith("_created")
}
emit(
    rednote_emitted == set(REDNOTE_METRIC_NAMES),
    "rednote_ 指标名与声明集合一致（反向）",
    f"输出={sorted(rednote_emitted)} 声明={sorted(REDNOTE_METRIC_NAMES)}",
)

# 结构合法性：不硬编码自动采集器的指标清单（R5：随平台变化）。
struct_ok = True
struct_detail = []
for line in [ln for ln in text.splitlines() if ln.strip()]:
    if line.startswith("#"):
        if not line.startswith(("# HELP ", "# TYPE ")):
            struct_ok = False
            struct_detail.append(f"非法注释 {line!r}")
    else:
        try:
            float(line.rsplit(" ", 1)[-1])
        except ValueError:
            struct_ok = False
            struct_detail.append(f"非法值 {line!r}")
emit(
    struct_ok,
    "每行都是合法 exposition 语法（不硬编码指标清单，R5）",
    "; ".join(struct_detail) or f"{len(text.splitlines())} 行全部合法",
)

emit(
    "python_info" in text,
    "python_info 在场（跨平台稳定，作为自动采集器的锚点）",
    "present" if "python_info" in text else "absent",
)

print()
print("### /metrics 输出样本（前 8 行，原样落盘供事后核对）")
for line in text.splitlines()[:8]:
    print(f"    {line}")

# ---------------------------------------------------------------------------
# 5) 契约不含新路径
# ---------------------------------------------------------------------------
print()
print("### 核对 5：三条新路径不入契约（D3）")
schema_paths = set(create_app(Settings(database_url=None, environment="test")).openapi()["paths"])
# ⚠️ **口径修正（2026-10-10，WP27 收口）**：原判据写死 `== 71`。
# 本条要断言的**不是**"契约有 71 条路径"（那是别的包的数字，会随每个新包增长），
# 而是"**本包新增的三条探针路径不进契约**"。写死的 71 让本判据在 WP27
# 把契约推到 77 时**假红**（红的是尺子、不是缺陷）—— 即 ISS-171 教训
# 「判据的参照值必须从被验对象派生」的第 N 次现身。
# 改为：基线从 `contracts/openapi.json`（契约产物的**单一事实来源**）现场派生，
# 断言仍等价于"契约路径数 == 契约产物路径数"（本包零新增）+ 三条路径 absent。
import json as _json  # 仅本判据使用（内嵌脚本头部未导入 json）
_CONTRACT = "../../contracts/openapi.json"  # 子进程 cwd = services/backend
with open(_CONTRACT, encoding="utf-8") as _fh:
    _contract_paths = set(_json.load(_fh)["paths"])
emit(
    schema_paths == _contract_paths,
    "契约 paths 数与本包新增路径无关（基线由 contracts/openapi.json 现场派生）",
    f"openapi()={len(schema_paths)} / 契约产物={len(_contract_paths)}"
    + ("" if schema_paths == _contract_paths else f" 差集={sorted(schema_paths ^ _contract_paths)}"),
)
for path in ("/healthz", "/readyz", METRICS_PATH):
    emit(path not in schema_paths, f"{path} 不在 openapi 里", "absent")

# ---------------------------------------------------------------------------
# 6) 重复装配不炸
# ---------------------------------------------------------------------------
print()
print("### 核对 6：重复 create_app 不炸（R6）")
raised = None
for _ in range(5):
    try:
        app = create_app(Settings(database_url=None, environment="test"))
        with TestClient(app) as c:
            c.get(METRICS_PATH)
    except Exception as exc:
        raised = f"{type(exc).__name__}: {exc}"
emit(raised is None, "连续 5 次 create_app + 抓指标不抛异常", raised or "ok")

# ⚠️ **"不抛异常"与"没重复装 handler"是两个独立事实。**
# `configure_logging` 若写成裸 `root.addHandler(...)`，连续装配会**静默**
# 堆出 N 个 handler ⇒ 不抛任何异常，但每条日志被打印 N 遍。R6 只查前者，
# 漏掉后者。这里直接数 handler 个数（幂等性的正面对照）。
handler_counts = []
for _ in range(3):
    create_app(Settings(database_url=None, environment="dev"))
    handler_counts.append(
        sum(
            1
            for h in logging.getLogger().handlers
            if getattr(h, "_is_rednote_observability_handler", False)
        )
    )
emit(
    handler_counts == [1, 1, 1],
    "重复装配 3 次只保留 1 个本模块 handler（幂等）",
    f"每次计数={handler_counts}",
)

# ---------------------------------------------------------------------------
# 7) 日志：requestId 真的进日志
# ---------------------------------------------------------------------------
print()
print("### 核对 7：日志带 requestId（计划 §4.2 任务 ① 的唯一端到端判据）")
import io

root = logging.getLogger()
dev_app = create_app(Settings(database_url=None, environment="dev"))
ours = [h for h in root.handlers if getattr(h, "_is_rednote_observability_handler", False)]
emit(len(ours) == 1, "dev 环境装了 1 个本模块 handler", f"{len(ours)}")

if ours:
    import time
    from rednote.platform.errors import reset_request_id, set_request_id

    buffer = io.StringIO()
    ours[0].setStream(buffer)
    token = set_request_id("REQ-EVIDENCE-0001")
    try:
        logging.getLogger("rednote.evidence").warning("证据脚本请求期日志")
    finally:
        reset_request_id(token)
    time.sleep(0.05)
    captured = buffer.getvalue()
    ours[0].setStream(io.StringIO())
    emit(
        "REQ-EVIDENCE-0001" in captured,
        "请求期日志带上了真实 requestId",
        repr(captured.strip()[:140]),
    )
    emit(
        f"{REQUEST_ID_LOG_FIELD}=" in (captured or "x"),
        "日志行含 requestId 字段占位",
        "present" if f"{REQUEST_ID_LOG_FIELD}=" in captured else "absent",
    )
    # 清理由本脚本装的 handler，避免影响后续核对。
    for h in list(root.handlers):
        if getattr(h, "_is_rednote_observability_handler", False):
            root.removeHandler(h)

print()
print(f"RESULT_EMITTED (期望 {EXPECTED_CHECKS} 条)")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def _dependency_readings() -> str:
    """落盘依赖声明的现场读数（判据在 tools/quality/check_dependency_declarations.py）。

    ⚠️ **不要用字符串切片数依赖**（首版踩过）：`text.split("dependencies = [")[1]
    .split("]")[0]` 会在第一个 `]` 处停下，而 `uvicorn[standard]==0.30.6` 自带的
    `]` **在列表开头就出现** ⇒ 整个清单被截到只剩 `fastapi`，
    读数报成"直依赖名 1 个、含 prometheus_client: False" —— 与事实相反，
    而证据文件看起来完全正常。这类"读数自己算错"是最坏的一种证据缺陷
    （它不报错，只是把错的数字存了档）。改用 `tomllib` 解析真结构。
    """
    import tomllib

    pyproject = BACKEND_DIR / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    # 归一化比较：`prometheus_client` / `prometheus-client` / 带 extra 都要命中。
    def _name(spec: str) -> str:
        base = spec.split(";", 1)[0].split("==")[0].split(">=")[0]
        base = base.split("[", 1)[0]
        return base.strip().lower().replace("-", "_")

    has_prometheus = any(_name(d) == "prometheus_client" for d in deps)
    return (
        f"pyproject 直依赖 {len(deps)} 个（tomllib 解析）\n"
        f"含 prometheus_client: {has_prometheus}\n"
        f"清单: {', '.join(deps)}"
    )


def collect_wp47() -> tuple[Path, bool]:
    env = os.environ.copy()
    env.update(
        {
            "NO_PROXY": "*",
            "no_proxy": "*",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "PYTHONPATH": str(BACKEND_DIR / "src"),
        }
    )
    completed = subprocess.run(
        [PYTHON, "-c", INSPECT_SCRIPT],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    checks = [
        line for line in output.splitlines() if line.startswith(("CHECK", "SKIP"))
    ]
    failures = [line for line in checks if line.startswith("CHECK FAIL")]

    lines = [
        "# WP47 ① 探针根路径别名 + /metrics + 日志 requestId：七处核对",
        f"# 生成时间: {_now()}",
        "# 目标库: 无需数据库（本包零表、零迁移，全部为进程内运行时事实）",
        "# 命令: cd services/backend && python -c '<真 app + 真请求 + 真 registry>'",
        "",
        output.rstrip(),
        "",
        "### 依赖声明读数（旁证，判据见 tools/quality/check_dependency_declarations.py）",
        _dependency_readings(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp47-probes.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # **条数不足也算失败**（ISS-43：崩溃伪装成"全过"）。
    complete = len(checks) >= EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete)


def main() -> int:
    path, ok = collect_wp47()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())