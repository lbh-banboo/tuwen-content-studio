"""可观测面（运维面）平台能力。

**为什么在 `platform/` 而不是 `modules/`：**

`modules/` 在本仓的语义是**业务竖切片** —— 每个模块有自己的表、服务、端点
（`projects` / `knowledge` / `content` …）。而本目录承载的东西**没有业务实体、
没有表、没有鉴权主体**，它是被所有模块共享的基础能力，与 `pagination.py` /
`errors.py` / `configuration.py` 同级。

**为什么单独一个子包而不是平铺三个文件：**

三个文件（指标 / 日志 / 探针装配）共享一个前提 —— 「**这些能力只对运维面负责，
不对业务面负责**」（ADR-11 C6 原文：「Prometheus + Grafana + OpenTelemetry …
**仅运维面**」）。把它们放在一个子包里，让"运维面"这件事在**目录结构**上可见，
而不是藏在三个平铺文件的名字里。

**本包（WP47 ①）的边界**：只做"接线"—— 让 Prometheus 能抓到本进程的指标、
让每条日志带上 requestId。**业务指标**（队列积压 / 任务失败率 / 预算异常 /
磁盘 / 备份失败）属 WP47 ②，它们各自需要业务侧埋点，不在这里。

**为什么 `get_request_id_for_logging` 同时出现在这里和 `rednote.main`：**

`main.py` 的同名函数是 WP47 之前的遗留 —— 它进了 `__all__`、有 docstring 说
"供日志配置使用"，却**零调用**，而那份"日志配置"当时根本不存在（本仓第一份
集中日志配置就是本包的 `logging_config.py`）。它是一个"声称有防线"的实例
（ISS-95 同族），处置见 `docs/m5/WP47-探针与指标说明.md` §0.2。

本模块提供**真正被调用**的那个（`configure_logging` 的默认取值器）。
`main.py` 的那个保留为**薄转发**，理由不是"为了兼容"——它是零调用的公开面，
没有兼容压力 —— 而是：**删掉它会让"这个悬空符号曾经存在"这件事从代码里消失**，
而它正是 §0.2 那条缺陷的证据。保留 + 在 docstring 里写明它现在是转发，比删掉
更好：删掉是"把问题藏起来"，保留并接上才是"把问题解决掉"。
"""

from rednote.platform.observability.logging_config import (
    APP_LOGGER_NAME,
    LOG_SETUP_EXEMPT_ENVIRONMENTS,
    REQUEST_ID_LOG_FIELD,
    RequestIdLogRecordFactory,
    configure_logging,
    get_request_id_for_logging,
    install_request_id_factory,
)
from rednote.platform.observability.metrics import (
    METRICS_PATH,
    REDNOTE_METRIC_NAMES,
    ensure_metrics_installed,
    metrics_response,
    render_metrics,
)

__all__ = [
    "APP_LOGGER_NAME",
    "LOG_SETUP_EXEMPT_ENVIRONMENTS",
    "METRICS_PATH",
    "REDNOTE_METRIC_NAMES",
    "REQUEST_ID_LOG_FIELD",
    "RequestIdLogRecordFactory",
    "configure_logging",
    "ensure_metrics_installed",
    "get_request_id_for_logging",
    "install_request_id_factory",
    "metrics_response",
    "render_metrics",
]