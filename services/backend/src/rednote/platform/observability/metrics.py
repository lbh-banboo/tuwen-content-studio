"""Prometheus 指标出口（WP47 ①，ADR-11 C6）。

## 这个模块负责什么

只负责**接线**：把本进程的指标以合法的 Prometheus exposition 格式暴露在
`GET /metrics` 上。业务指标（队列积压 / 任务失败率 / 预算异常 / 磁盘 / 备份失败）
属 WP47 ②，各自需要业务侧埋点，**不在本模块**。

## 指标名是契约，不是随意命名

WP47 ② 的告警规则会**按名字引用**这里的指标。Prometheus 对"引用了不存在的指标"
**不报错**——表达式静默返回空结果，告警永不触发。因此：

> **本模块登记的指标名，WP47 ② 只能新增，不能重命名。**

这就是本模块把 `REDNOTE_METRIC_NAMES` 显式列出来的理由 —— 让"我们对外承诺了哪些
名字"是一个**可被读取的事实**，而不是散落在各处的字符串字面量。
`tools/quality/collect_wp47_evidence.py` 会逐条核对它们真的出现在输出里。

## 为什么用默认 `REGISTRY` 而不是自建 `CollectorRegistry`

`prometheus_client` 在**导入时**就往默认 `REGISTRY` 注册了三个自动采集器
（`ProcessCollector` / `PlatformCollector` / `GCCollector`）。自建 registry 会让
这三样**全部丢失** —— 表现为 `/metrics` 里连内存和 GC 都没有，运维第一眼就会
以为"指标没接上"。复用默认 `REGISTRY` 是**继承**这些能力，不是偷懒。

代价是"同一进程内重复注册会炸"（`Duplicated timeseries in CollectorRegistry`），
而本仓的测试夹具会**反复调用 `create_app()`**（`conftest.py` 有 4 个 app fixture）。
因此下面所有注册动作**必须幂等** —— 见 `ensure_metrics_installed`。

## 已知盲区（不做假保证）

- **多进程（`--workers > 1`）下指标不聚合**：`prometheus_client` 的多进程模式
  需要 `PROMETHEUS_MULTIPROC_DIR` + `MultiProcessCollector`，且要求进程退出时
  落盘。本仓当前部署（ADR-11 C8：双 API 实例 = 两台机器各一进程）**不是**
  "单机多 worker"，因此不启用该模式。**若将来改成单机多 worker，这里必须改**
  —— 否则 Prometheus 每抓一次只会看到一个 worker 的数字，且是随机的那个。
- **`/metrics` 无内建访问控制**：ADR-11 C6 把"仅运维面"定为**网关职责**。
  本模块**刻意不提供**任何内建鉴权 —— 加一个"看起来有鉴权"的半吊子实现，
  比明确依赖网关更危险（本项目纪律：声称有防线比没有防线更危险）。
"""

from __future__ import annotations

import logging
import threading
from typing import Final

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest
from prometheus_client import REGISTRY as _DEFAULT_REGISTRY
from starlette.responses import Response

from rednote.platform.configuration import Settings

logger = logging.getLogger(__name__)

#: 保护"先查后建"整段的互斥锁（CR-2）。
#:
#: **为什么必须有**：`main.py` 的 `/metrics` 端点用
#: `await asyncio.to_thread(ensure_metrics_installed, resolved)` —— 调用跑在
#: **线程池**里，而事件循环可并发处理多个请求（Prometheus 双实例抓同一实例的
#: HA 配置很常见）。此时"读 `collect()`"与"建 `Gauge`"之间可被另一个线程插入。
#:
#: ⚠️ **GIL 不提供这个保证**：`request_id` 那类单字节码读写是原子的，但
#: "查 + 建"是**跨多个字节码的复合操作**。本机用可控替身把两个线程对齐在
#: "查完、还没建"这一刻，实测稳定复现：
#:
#:     created = ['t1']
#:     errors  = ["t0: ValueError: Duplicated timeseries in CollectorRegistry: {'rednote_build_info'}"]
#:
#: 后果是 `/metrics` **偶发 500** ⇒ Prometheus 侧 `up=0`，且不可复现（依赖时序）。
#: 这与 WP12/WP13 的"并发不能突破 X"同族，是本项目已认定的命门。
#:
#: ⚠️ **不要试图用"两线程 + Barrier 撞一次"当这条缺陷的判据** —— 实测那样做
#: **反向验证不成立**（去掉锁仍绿），且把 Barrier 挪进竞态窗口又会让判据在
#: **有锁时自己死锁**（对齐要求两线程同时进临界区，而锁的定义就是禁止这件事）。
#: 正确形态是**判定式**：断言"查/建/set 都发生在 `lock.locked()` 为真期间"，
#: 另配一条断言"锁真的互斥"。见 `tests/test_observability.py` 的 CR-2 段。
#:
#: **为什么不是 try/except `ValueError`**：见 `ensure_metrics_installed` 的 docstring
#: —— 那会把"我重复注册了"与"别的包注册了同名指标"变成同一种静默结果。
#: 加锁是**消除竞态**，不是**掩盖异常**；冲突仍会以可诊断的事实暴露。
_REGISTRATION_LOCK: Final[threading.Lock] = threading.Lock()

#: 指标出口路径。**根路径**，不带 `/api/v1` 前缀 ——
#: 理由见 `docs/m5/WP47-探针与指标说明.md` §2.2 决策 D3：
#: 它是运维面入口（ADR-11 C6），不是产品契约的一部分；Prometheus 的 scrape
#: config 直接写 `metrics_path`，不需要业务前缀。
METRICS_PATH: Final[str] = "/metrics"

#: 本模块对外承诺的指标名（WP47 ② 的告警规则按这些名字引用）。
#:
#: ⚠️ **只能新增，不能重命名** —— 重命名会让 WP47 ② 的告警规则静默失效
#: （Prometheus 引用不存在的指标不报错，表达式返回空、告警永不触发）。
REDNOTE_METRIC_NAMES: Final[tuple[str, ...]] = ("rednote_build_info",)

#: 构建信息指标名。
_BUILD_INFO_METRIC: Final[str] = "rednote_build_info"

#: 构建信息的标签。取值全部来自 `Settings`，**不含**任何主机名 / DSN / 凭据
#: （C6：日志与指标不泄露内部细节）。
_BUILD_INFO_LABELS: Final[tuple[str, ...]] = ("service", "version", "environment")


def _registered_metric_names(registry: CollectorRegistry) -> set[str]:
    """列出 registry 里已注册的指标族名。

    **为什么用公开的 `collect()` 而不是私有的 `_names_to_collectors`：**
    后者是 `prometheus_client` 的内部实现，跨版本可能变；`collect()` 是公开
    协议方法。

    ⚠️ **`collect()` 的名字与 exposition 里的名字在 Counter/Info 上不一致**：
    本机实测 —— `Counter("c_x_total")` 在 `collect()` 里是 `c_x`（剥掉 `_total`），
    `Info("i_x")` 是 `i_x`（exposition 里是 `i_x_info`）。本模块只登记 **Gauge**
    （实测 `collect()` 名字与注册名**逐字相同**），因此这一差异不影响本模块；
    但若将来加了 Counter/Info，**判据不能拿 `collect()` 的名字去比对 exposition**。
    """
    return {metric.name for metric in registry.collect()}


def ensure_metrics_installed(
    settings: Settings, registry: CollectorRegistry | None = None
) -> CollectorRegistry:
    """幂等登记本模块的指标，返回用于渲染的 registry。

    **幂等的必要性**：`create_app()` 在测试里被反复调用（`conftest.py` 的 4 个
    app fixture + 各测试内自建），而 `prometheus_client` 的默认 `REGISTRY` 是
    **进程级单例** —— 第二次注册同名指标会抛
    `ValueError: Duplicated timeseries in CollectorRegistry`（本机已实测该异常）。

    **为什么不用 try/except 兜住这个 `ValueError`**：那会把"我重复注册了"和
    "别的包注册了同名指标"变成同一种静默结果，掩盖真正的名字冲突。
    先查后建（read-then-write）把冲突**暴露成一个可诊断的事实**。

    **为什么每次调用都重设 build info 的值**：`Settings` 可能在不同 `create_app`
    调用间不同（测试会注入不同 environment）。重设让指标反映**最后一次装配**
    的配置，而不是第一次的值 —— 后者会让 `/metrics` 报出一个与实际运行的
    实例不符的 version。

    **为什么整段加锁（CR-2）**：本函数被 `/metrics` 经 `asyncio.to_thread` 调用，
    并发抓取时"查 + 建"不是原子操作 —— 实测可复现 `Duplicated timeseries`。
    锁的范围**必须覆盖"查 → 建/取 → set"整段**：只锁后面一半没用（两个线程会
    同时通过"查"这一关）。`Gauge.labels(...).set(1)` 也在锁内 —— 建与取的对象
    必须是同一批，否则后到的线程会 set 到一个即将被丢弃的对象上。
    """
    target = registry if registry is not None else _DEFAULT_REGISTRY

    with _REGISTRATION_LOCK:
        if _BUILD_INFO_METRIC not in _registered_metric_names(target):
            gauge = Gauge(
                _BUILD_INFO_METRIC,
                "构建信息。值恒为 1，实际信息在标签里（Prometheus 的 info 惯例）。",
                list(_BUILD_INFO_LABELS),
                registry=target,
            )
        else:
            # 已存在 ⇒ 取回同一个对象。`collect()` 只给出名字，取对象要走
            # 注册表的公开入口：`Gauge` 构造在同名时会抛异常，因此这里用
            # `_names_to_collectors` —— 它是取回**已有实例**的唯一途径。
            # 该属性自 prometheus_client 0.4 起稳定存在，且本仓已锁死补丁版本
            # （pyproject.toml），升级时会被 check_dependency_declarations 提醒。
            gauge = target._names_to_collectors[_BUILD_INFO_METRIC]  # noqa: SLF001

        gauge.labels(
            service=settings.app_name,
            version=settings.app_version,
            environment=settings.environment,
        ).set(1)
        return target



def render_metrics(registry: CollectorRegistry) -> bytes:
    """把 registry 渲染成 Prometheus exposition 文本（bytes）。"""
    return generate_latest(registry)


def metrics_response(registry: CollectorRegistry) -> Response:
    """构造 `/metrics` 的响应。

    **Content-Type 必须原样用 `CONTENT_TYPE_LATEST`**（实测值
    `text/plain; version=0.0.4; charset=utf-8`）。Prometheus 按这个头判断
    exposition 版本；写成 `application/json` 或漏掉 `version=` 会被判为
    不合法的抓取目标 —— 表现为 Prometheus 侧 `up=0`，且报错信息指向
    "unsupported media type"，排查时会先怀疑网络。
    """
    return Response(
        content=render_metrics(registry),
        media_type=CONTENT_TYPE_LATEST,
    )


__all__ = [
    "METRICS_PATH",
    "REDNOTE_METRIC_NAMES",
    "ensure_metrics_installed",
    "metrics_response",
    "render_metrics",
]