"""WP47 ① 可观测面测试：探针根路径别名 + `/metrics` + 日志 requestId。

## 这个文件为什么存在

`test_health.py` 守住的是**既有**两条路径的语义（WP06/WP07 交付）。本文件守住
WP47 ① 新加的**接线**：根路径别名、指标出口、日志 correlation id。

两者刻意分开：前者的 12 条用例是本包的**行为基线**（别名必须与它们语义一致），
后者的用例是本包的**新增面**。混在一个文件里会让"别名改动是否破坏了既有语义"
与"新功能是否达标"两件事的失败信号互相淹没。

## 四条高风险项各有对应用例（`docs/m5/WP47-探针与指标说明.md` §1.6）

- **R1** liveness 混进连库 → `test_healthz_does_not_touch_database_*` 两条**成对**断言
- **R3** 指标名漂移 → `test_metrics_contains_declared_metric_names`
- **R6** 重复注册 → `test_repeated_create_app_does_not_raise`
- **R8** 日志全局污染 → `test_configure_logging_preserves_existing_handlers`

## 一个刻意的取舍：不做假保证

`/metrics` 的**访问控制**不在本文件里测 —— 因为**代码层没有实现它**（ADR-11 C6
把"仅运维面"定为网关职责，本包刻意不加半吊子鉴权，见 §1.6 R2）。为它写一条
"应该被拒绝"的测试会造出**假防线**：测试绿了，读者以为有鉴权，实际没有。
"""

from __future__ import annotations

import io
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from rednote.main import create_app
from rednote.platform.configuration import Settings, get_settings
from rednote.platform.errors import reset_request_id, set_request_id
from rednote.platform.observability import (
    LOG_SETUP_EXEMPT_ENVIRONMENTS,
    METRICS_PATH,
    REDNOTE_METRIC_NAMES,
    REQUEST_ID_LOG_FIELD,
    RequestIdLogRecordFactory,
    configure_logging,
    get_request_id_for_logging,
    install_request_id_factory,
)

# 冒烟集：探针不可用 = 编排平台无法判活，属最关键路径。
pytestmark = pytest.mark.smoke


# ===========================================================================
# R1：liveness 别名**不碰数据库**（本包唯一可能造成生产事故的风险）
# ===========================================================================


def test_healthz_returns_ok_without_database_configured(
    client_without_database: TestClient,
) -> None:
    """未配置数据库时 `/healthz` 必须 200（别名继承了 liveness 语义）。"""
    response = client_without_database.get("/healthz")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ok"


def test_healthz_survives_unreachable_database() -> None:
    """**R1 的真判据**：注入一个必然无响应的 DSN，`/healthz` 仍必须 200。

    为什么上一条不足以证明 R1：`/healthz` 若被实现成"碰库但探测失败也返回 200"，
    上一条**照样通过**（因为那里根本没配库，走的是"未配置"短路路径）。
    只有"配了库、但库不可达"才真正区分 liveness 与 readiness。

    DSN 指向 `127.0.0.1:1`（保留端口、必拒连）。若实现里混进了 probe_database，
    要么慢到超时、要么返 503 —— 两种都会让本用例红。
    """
    settings = Settings(
        database_url="postgresql+psycopg://probe:probe@127.0.0.1:1/nonexistent",
        environment="test",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        liveness = client.get("/healthz")
        readiness = client.get("/readyz")

    assert liveness.status_code == 200, (
        "/healthz 在数据库不可达时返回了非 200 —— liveness 探针混进了连库逻辑，"
        "生产上会导致编排平台把全部实例重启掉（R1）"
    )
    # 成对断言：同一个 app 上，readiness **必须**反映依赖不可用。
    # 这条让上一条的"200"有意义 —— 证明 200 不是因为整条链路都坏了。
    assert readiness.status_code == 503


def test_readyz_reports_dependency_unavailable_without_database(
    client_without_database: TestClient,
) -> None:
    """`/readyz` 是 readiness：未配置库时必须 503 DEPENDENCY_UNAVAILABLE。"""
    response = client_without_database.get("/readyz")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


# ===========================================================================
# R10：别名与既有路径**语义一致**（证明"复用 handler"真的生效）
# ===========================================================================


def test_healthz_matches_existing_health_path(client_without_database: TestClient) -> None:
    """根路径别名与 `/api/v1/health` 必须返回**同一份语义**。

    比对时排除 `meta.requestId` —— 两次请求是两个不同请求，requestId 本就该不同。
    若把 requestId 也算进去，这条断言永远为假，只能被"看起来在测什么"骗过去。
    """
    alias = client_without_database.get("/healthz").json()
    existing = client_without_database.get("/api/v1/health").json()

    alias["meta"].pop("requestId")
    existing["meta"].pop("requestId")

    assert alias == existing


def test_health_still_not_under_bare_path(client_without_database: TestClient) -> None:
    """回归护栏：`/health`（无 z）**仍必须是 404**。

    本包加了 `/healthz`，最容易犯的错是顺手也加一个 `/health`。
    那会与 `test_health.py::test_health_endpoint_is_not_under_bare_path`
    的既有断言冲突 —— 这里从"别名"这一侧再钉一次。
    """
    assert client_without_database.get("/health").status_code == 404


# ===========================================================================
# `/metrics`：形态、内容、契约外
# ===========================================================================


def test_metrics_endpoint_returns_prometheus_exposition(
    client_without_database: TestClient,
) -> None:
    """/metrics 必须返回 Prometheus exposition 文本。

    Content-Type 逐字校验：Prometheus 按这个头判断 exposition 版本。
    写成 `application/json` 或漏掉 `version=` 会被判为不合法抓取目标
    （Prometheus 侧表现为 up=0，报错指向"unsupported media type"，
    排查时会先怀疑网络 —— 因此这里把字面值钉死）。
    """
    response = client_without_database.get(METRICS_PATH)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert "python_info" in response.text


def test_metrics_contains_declared_metric_names(
    client_without_database: TestClient,
) -> None:
    """**R3 的守门用例**：模块声明的每个指标名都必须真的出现在输出里。

    期望集合**由被测对象推导**（`REDNOTE_METRIC_NAMES`），这是刻意的：
    本判据要守的是"声明与输出一致"，不是"输出里有某个我背下来的名字"。
    若把期望写成硬编码的 `{"rednote_build_info"}`，那么重命名指标时
    两边一起改、测试照样绿 —— 而重命名正是 R3 要防的事
    （WP47 ② 的告警规则按名字引用，改名会让告警**静默失效**）。

    ⚠️ 单靠这一条不足以防 R3（期望来自被测对象 ⇒ 声明一变期望跟着变）。
    完备性由 `test_metrics_declares_no_undeclared_rednote_metrics` 反向兜底 ——
    两条合起来才闭合，这正是纪律第 6 条的形态。
    """
    text = client_without_database.get(METRICS_PATH).text

    for name in REDNOTE_METRIC_NAMES:
        assert f"# HELP {name} " in text or f"{name}{{" in text, (
            f"声明的指标 {name} 未出现在 /metrics 输出里"
        )


def test_metrics_declares_no_undeclared_rednote_metrics(
    client_without_database: TestClient,
) -> None:
    """R3 的**反向**判据：输出里的 `rednote_` 指标名必须全部在声明清单里。

    为什么必须有这一条：上一条的期望集合来自被测对象自身，因此"少声明一个
    指标"在那边**恒真通过**。这条从输出侧反向枚举：凡是 `rednote_` 前缀的
    指标名，都必须能在 `REDNOTE_METRIC_NAMES` 里找到。

    前缀只认 `rednote_` —— 自动采集器（GC / 进程 / python_info）的名字
    随平台变化（本机实测 macOS 无 `process_*`、Linux CI 有），**不能**纳入
    本断言，否则会变成"本机绿、CI 红"（§1.6 R5）。
    """
    text = client_without_database.get(METRICS_PATH).text

    # exposition 行形如 `name{labels} value` 或 `name value`；注释行以 # 开头。
    emitted: set[str] = set()
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        head = line.split("{", 1)[0].split(" ", 1)[0].strip()
        if head:
            emitted.add(head)

    rednote_names = {n for n in emitted if n.startswith("rednote_")}
    # `_created` 后缀由 prometheus_client 的 Gauge 自动生成（值 = 注册时刻），
    # 不是模块声明的指标名，按后缀剔除而不写进声明清单 —— 否则清单会被
    # "框架生成物"污染，将来换库版本时第一条判据会误红。
    rednote_names = {n for n in rednote_names if not n.endswith("_created")}

    assert rednote_names == set(REDNOTE_METRIC_NAMES), (
        f"输出里的 rednote_ 指标 {sorted(rednote_names)} 与声明 "
        f"{sorted(REDNOTE_METRIC_NAMES)} 不一致 —— 声明与实际漂移会让 "
        f"WP47 ② 的告警规则静默失效（R3）"
    )


def test_metrics_has_valid_exposition_syntax(
    client_without_database: TestClient,
) -> None:
    """每行都必须是合法 exposition 语法（注释 / 指标行两种形态之一）。

    兜 R5（指标集随平台变化）的替代手法：不去枚举"应该有哪些指标名"，
    而是校验**输出本身的结构合法性**。这样 Linux CI 多出 `process_*` 也不会红。
    """
    text = client_without_database.get(METRICS_PATH).text
    lines = [ln for ln in text.splitlines() if ln.strip()]

    assert lines, "/metrics 输出为空"
    for line in lines:
        if line.startswith("#"):
            assert line.startswith(("# HELP ", "# TYPE ")), f"非法注释行：{line!r}"
        else:
            # `name{...} value` 或 `name value`；值必须是数字（Prometheus 不接受 NaN 字面量）
            assert " " in line, f"非法指标行（缺少值）：{line!r}"
            value = line.rsplit(" ", 1)[-1]
            assert float(value) is not None, f"值不是数字：{line!r}"


def test_metrics_not_exposed_in_openapi_schema() -> None:
    """**D3 的判据**：三条新路径**全部不进契约**（运维面不对外承诺接口）。

    契约是给前端生成 SDK 的。给前端一个 `metrics()` 或 `healthz()` 方法是荒谬的
    （ADR-11 C6「仅运维面」）。这条测试守住 71 paths 不变。
    """
    paths = create_app(Settings(database_url=None, environment="test")).openapi()["paths"]

    for path in ("/healthz", "/readyz", METRICS_PATH):
        assert path not in paths, f"{path} 混进了 OpenAPI 契约（应 include_in_schema=False）"


# ===========================================================================
# R6：重复注册不炸
# ===========================================================================


def test_repeated_create_app_does_not_raise() -> None:
    """连续多次 `create_app()` 不得抛异常（R6）。

    真实来源：`conftest.py` 有 4 个 app fixture，每个都调一次 `create_app()`；
    整个测试会话里这个函数会被调用几十次。指标若在模块顶层裸建、
    或在装配期无脑重复注册，第二次就会
    `ValueError: Duplicated timeseries in CollectorRegistry`。

    这里刻意**不** mock 任何东西 —— 要验证的就是"真实的重复装配不炸"。
    """
    for _ in range(3):
        app = create_app(Settings(database_url=None, environment="test"))
        with TestClient(app) as client:
            assert client.get(METRICS_PATH).status_code == 200


def test_metrics_registration_is_idempotent(
    client_without_database: TestClient,
) -> None:
    """重复抓取 `/metrics` 不得让指标重复出现（幂等注册的表象判据）。"""
    first = client_without_database.get(METRICS_PATH).text
    second = client_without_database.get(METRICS_PATH).text

    build_info_lines_first = [ln for ln in first.splitlines() if ln.startswith("rednote_build_info{")]
    build_info_lines_second = [ln for ln in second.splitlines() if ln.startswith("rednote_build_info{")]

    assert len(build_info_lines_first) == 1, "rednote_build_info 出现了多行 —— 重复注册"
    assert len(build_info_lines_second) == 1


# ===========================================================================
# 日志：requestId 注入（阶段 4 的核心，实测结论落地）
# ===========================================================================


def test_configure_logging_preserves_existing_handlers() -> None:
    """**R8 的判据**：配置函数不得删除既有 handler。

    本仓的测试与 root logger 共享同一个进程。若配置函数用
    `dictConfig`（声明式替换 handler 列表）或 `root.handlers.clear()`，
    同进程里其他包的日志会被静默掐断 —— 表现为"别的包莫名其妙红"。

    这里放一个哨兵 handler，调用配置函数后断言它**仍在**。
    """
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    try:
        configure_logging(Settings(database_url=None, environment="dev"))
        assert sentinel in root.handlers, "configure_logging 删除了既有 handler（R8）"
    finally:
        root.removeHandler(sentinel)
        # 清理本模块装的 handler，避免污染同进程后续测试。
        for handler in list(root.handlers):
            if getattr(handler, "_is_rednote_observability_handler", False):
                root.removeHandler(handler)


def test_configure_logging_is_reentrant() -> None:
    """重复调用 `configure_logging` 不得累积 handler（幂等）。"""
    root = logging.getLogger()
    try:
        for _ in range(3):
            configure_logging(Settings(database_url=None, environment="dev"))
        ours = [
            h
            for h in root.handlers
            if getattr(h, "_is_rednote_observability_handler", False)
        ]
        assert len(ours) == 1, f"重复配置装了 {len(ours)} 个 handler（应为 1）"
    finally:
        for handler in list(root.handlers):
            if getattr(handler, "_is_rednote_observability_handler", False):
                root.removeHandler(handler)


def test_configure_logging_returns_false_in_exempt_environment() -> None:
    """测试环境豁免 root 接管；返回值让"配没配"成为可断言的事实。

    纪律第 5 条："没跑"与"跑失败"同形。若 `configure_logging` 只返回 None，
    这条差异只能靠读日志文件发现。返回值把它变成一次断言。
    """
    assert "test" in LOG_SETUP_EXEMPT_ENVIRONMENTS
    assert configure_logging(Settings(database_url=None, environment="test")) is False


def test_request_id_factory_records_value_at_construction_time() -> None:
    """**选 Record Factory 而非 Filter 的核心依据**（模块 docstring 的实测表）。

    断言两件事：
    1. 请求上下文中构造的记录带真实 requestId（不是 default）；
    2. 请求上下文**外**构造的记录带默认值，且**日志照常输出**
       （不会因缺字段抛 `ValueError` 而整条丢失 —— R7）。

    第 2 条是"没请求时日志也要能记"的判据。只测第 1 条的话，一个
    "无上下文即崩"的实现会留下一个只在生产空闲期才爆的雷。
    """
    install_request_id_factory()

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(
        logging.Formatter(f"%(levelname)s %(message)s {REQUEST_ID_LOG_FIELD}=%({REQUEST_ID_LOG_FIELD})s")
    )
    probe_logger = logging.getLogger("test.wp47.probe")
    probe_logger.handlers = [handler]
    probe_logger.setLevel(logging.INFO)
    probe_logger.propagate = False

    token = set_request_id("REQ-TEST-1234")
    try:
        probe_logger.info("during")
    finally:
        reset_request_id(token)
    probe_logger.info("after")

    lines = buffer.getvalue().splitlines()
    assert any("REQ-TEST-1234" in line for line in lines), (
        f"请求上下文内的日志未带上真实 requestId：{lines}"
    )
    assert len(lines) == 2, f"有日志被丢弃（只输出 {len(lines)} 条）：{lines}"


def test_request_id_factory_installation_is_idempotent() -> None:
    """工厂是**进程级全局状态**：重复安装只能包一层。

    若不幂等，经过 N 次 `create_app()` 后工厂链会有 N 层 —— 不报错，
    但每条日志都要穿过 N 个包装。这里直接数链深。
    """
    first = install_request_id_factory()
    second = install_request_id_factory()
    third = install_request_id_factory()

    assert first is second is third

    depth = 0
    current = logging.getLogRecordFactory()
    while isinstance(current, RequestIdLogRecordFactory):
        depth += 1
        current = current.previous
    assert depth == 1, f"工厂链深为 {depth}（应为 1）"


def test_request_id_for_logging_reader_is_wired() -> None:
    """`get_request_id_for_logging` 必须真的可用（不再是悬空函数）。

    背景：WP47 ① 之前 `main.py` 里有一个同名的零调用函数，docstring 声称
    "供日志配置使用"，而那份日志配置**根本不存在** —— ISS-95 同族的
    "声称有防线"。本包的处置是**接上它**，本用例是那个处置的判据。
    """
    token = set_request_id("REQ-WIRED")
    try:
        assert get_request_id_for_logging() == "REQ-WIRED"
    finally:
        reset_request_id(token)


def test_logged_requests_carry_request_id_in_configured_output() -> None:
    """端到端：配好日志后，**请求期的日志**必须带上该请求的 requestId。

    这是本包"日志只带 requestId"（计划 §4.2 任务 ①）的唯一端到端判据。
    前面几条测的是零件，这条测的是**装配后的行为**。

    手法：在 dev 环境装配（测试环境被豁免，不装 handler），把本模块的 handler
    的流换成内存 buffer，然后经真实 HTTP 请求触发一条已知日志
    （`main.py` 的"已装配开发期登录旁路"），断言其中带上了该请求的 requestId。
    """
    root = logging.getLogger()
    app = create_app(Settings(database_url=None, environment="dev"))

    ours = [
        h for h in root.handlers if getattr(h, "_is_rednote_observability_handler", False)
    ]
    assert len(ours) == 1, "dev 环境下应恰有一个本模块的 handler"

    buffer = io.StringIO()
    original_stream = ours[0].stream
    ours[0].setStream(buffer)
    try:
        with TestClient(app) as client:
            response = client.get("/healthz")
            request_id = response.headers["X-Request-Id"]

        # 触发一条请求期日志并确认它带上了正确 requestId。
        # 用 `logger.info` 走 root 链（handler 挂在 root），模拟业务日志。
        logging.getLogger("rednote.smoke").info("端到端 requestId 校验")
        output = buffer.getvalue()
    finally:
        ours[0].setStream(original_stream or io.StringIO())
        for handler in list(root.handlers):
            if getattr(handler, "_is_rednote_observability_handler", False):
                root.removeHandler(handler)
        logging.getLogger().setLevel(logging.WARNING)

    assert "端到端 requestId 校验" in output, f"日志未落盘：{output!r}"
    # 请求已结束，这条请求后的日志应带默认值；但格式字段必须存在。
    assert f"{REQUEST_ID_LOG_FIELD}=" in output
    assert request_id  # 响应头本身仍带 requestId（回归）


def test_log_level_falls_back_to_info_on_invalid_value() -> None:
    """非法日志级别回落 INFO 并**告警**，不抛异常。

    级别写错的正确后果是"记多了/记少了"，不该升级成"服务起不来"。
    但必须可见 —— 静默回落会让"设了 DEBUG 却没生效"需要读源码才能明白。
    """
    app = create_app(Settings(database_url=None, environment="dev", log_level="NOT-A-LEVEL"))
    assert isinstance(app, FastAPI)
    get_settings.cache_clear()


# ===========================================================================
# CR-1：默认取值器必须指向本模块的读取器（"转发层有人在用"的判据）
# ===========================================================================


def test_default_request_id_getter_is_this_module_reader() -> None:
    """`install_request_id_factory` 的**默认**取值器必须是本模块的转发函数（CR-1）。

    ## 为什么这条判据必须存在

    本模块首版写的是 `getter = request_id_getter or get_request_id` —— 用的是
    从 `errors` **直接导入**的那个同义函数，于是 `logging_config.get_request_id_for_logging`
    **零调用**，而它的 docstring 却写着"这个真的被调用"。

    这与本包要修的那条缺陷（`main.py` 的悬空同名函数）**同形**：不是"声称有防线"，
    是"声称自己不是摆设"。**删掉一个悬空符号的同时新造了一个** ⇒ 缺陷形态未消灭。

    ## 这条判据为什么能抓住它（而不是恒真）

    断言的是**对象身份**（`is`），不是行为等价 —— 两个函数**行为完全一样**
    （都是 `return get_request_id()`），因此任何"测行为"的用例都会同时通过。
    只有身份断言能把"转发层被绕过"变成可观测事实。

    ⚠️ **反向验证**：把 `install_request_id_factory` 的默认值改回
    `or get_request_id`（从 errors 直接导入的那个），本用例必须红。
    """
    from rednote.platform.observability import logging_config as module_under_test

    factory = install_request_id_factory()  # 不传取值器 ⇒ 走默认路径
    try:
        assert factory._request_id_getter is module_under_test.get_request_id_for_logging, (
            "默认取值器不是本模块的 get_request_id_for_logging —— 转发层被绕过，"
            "该函数的 docstring 声称'真的被调用'将成为一句假话（CR-1）"
        )
    finally:
        # 还原成标准 getter，避免影响后续用例。
        install_request_id_factory()


# ===========================================================================
# CR-2：并发抓取 `/metrics` 不得抛 `Duplicated timeseries`
# ===========================================================================


@pytest.fixture
def recording_registry() -> CollectorRegistry:
    """一条**独立** registry：默认 `REGISTRY` 是进程级单例，测试里可能已被注册过
    （那样两个线程/两次调用都会走"已存在"分支，竞态根本不会被触发 ⇒ 判据失效）。"""
    return CollectorRegistry()


@pytest.fixture
def spy_on_critical_section(
    monkeypatch: pytest.MonkeyPatch, recording_registry: CollectorRegistry
) -> list[tuple[str, bool]]:
    """把"查"与"建"替换成**只记账不干活**的探针，返回事件流水。

    每条事件是 `(动作, 此刻注册锁是否被持有)`。判据据此断言**不变量**：
    查与建**都**发生在锁内 —— 这比"两线程并发跑一遍不抛异常"强得多，理由见
    两条用例各自的 docstring。
    """
    from rednote.platform.observability import metrics as metrics_module

    events: list[tuple[str, bool]] = []
    real_lookup = metrics_module._registered_metric_names
    lock = metrics_module._REGISTRATION_LOCK

    def _spy_lookup(registry: CollectorRegistry) -> set[str]:
        events.append(("lookup", lock.locked()))
        return real_lookup(registry)

    class _SpyGauge:
        """只记"被构造了"，不进真 registry —— 本用例只关心**时序**，
        不关心注册结果（注册结果由同文件其它用例守）。"""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            events.append(("create", lock.locked()))

        def labels(self, **_kwargs: object) -> "_SpyGauge":
            return self

        def set(self, _value: object) -> None:
            events.append(("set", lock.locked()))

    monkeypatch.setattr(metrics_module, "_registered_metric_names", _spy_lookup)
    monkeypatch.setattr(metrics_module, "Gauge", _SpyGauge)
    return events


def test_lookup_and_create_both_happen_while_registration_lock_is_held(
    spy_on_critical_section: list[tuple[str, bool]],
    recording_registry: CollectorRegistry,
) -> None:
    """**CR-2 的主判据（判定式，不依赖时序）**：查与建都在注册锁内。

    ## 为什么不能靠"开两线程撞一次"来判

    首版判据是"两线程 + `Barrier` 对齐 ⇒ 断言零异常"，**实测反向验证失败**：
    去掉 `_REGISTRATION_LOCK` 后该用例**仍然绿**。原因是结构性的 ——
    `Barrier` 对齐点放在函数入口时，两线程随后各自的"查+建"仍可能**串行完成**
    （GIL + 窗口极短）⇒ 第二个线程进函数时第一个已建好 ⇒ 走"已存在"分支 ⇒ 零异常。

    而把 `Barrier` 挪进"查完、还没建"那一刻又制造了新的问题：
    **确定性复现竞态与"有锁时不阻塞"逻辑互斥** —— 对齐必然要求两线程同时进入
    临界区，而锁的定义就是禁止这件事。有锁时第二个线程会卡在 `acquire` 上，
    第一个线程则永远等不到搭档（`Barrier` 超时）⇒ **判据自己把实现 deadlock 死**。

    ⇒ 并发类缺陷的判据**不能是"复现它"**，得是**"断言那条不变量"**。
    本用例正是那条不变量：`lock.locked()` 是**可查询的事实**（`threading.Lock.locked`），
    不需要任何时序配合。

    ## 为什么这条一定能报红

    去掉锁（或改用别的锁）⇒ `create` / `set` 记账时 `lock.locked()` 为 `False`
    ⇒ 断言直接失败。**已实测**：变异后 RED，还原后 GREEN。

    ⚠️ 注意 `lock` 取的是**模块对象上的那把锁本身**（不是函数入参），
    所以"换了一把新锁"这种变异同样会被抓住 —— `lock.locked()` 只反映
    `_REGISTRATION_LOCK` 的真实状态。
    """
    from rednote.platform.observability import ensure_metrics_installed

    settings = Settings(database_url=None, environment="test")

    ensure_metrics_installed(settings, recording_registry)

    assert spy_on_critical_section == [
        ("lookup", True),
        ("create", True),
        ("set", True),
    ], (
        "「查 → 建 → set」三步没有全部发生在注册锁内 —— CR-2 的竞态会重现："
        f"记账={spy_on_critical_section}。"
        "锁的范围缺任何一段，两个并发抓取都能通过'查'这一关，"
        "表现为 /metrics 偶发 500、Prometheus up=0"
    )


def test_registration_lock_actually_blocks_a_second_caller(
    recording_registry: CollectorRegistry,
) -> None:
    """**CR-2 的第二条判据**：锁真的**互斥**（不是装饰性的摆设）。

    ## 为什么必须与主判据成对

    `test_lookup_and_create_both_happen_while_registration_lock_is_held` 断言的是
    "锁的**范围**对"。它**抓不到**另一种坏实现：把 `with _REGISTRATION_LOCK:`
    换成一个**永不阻塞**的自定义锁对象（`__enter__` 直接返回）——
    上面那条全绿（`lock.locked()` 确实为 `True`，因为 `acquire` 真被调用了），
    但互斥**完全不存在**，竞态照样重现。

    这就是"两种独立事实"：**范围对** ≠ **互斥真**。必须分别断言。

    ## 判据形态：主线程持锁 ⇒ 工作线程必须被挡住

    不 sleep 后"猜"它有没有卡住，而是等一个**事件**（`reached_call`）确认工作线程
    **已经进入 `ensure_metrics_installed`**（即：起线程延迟不算成"被挡住"），
    再断言结果列表仍为空。去掉锁或换成不阻塞的锁 ⇒ 它当场完成 ⇒ 断言失败。

    ⚠️ **为什么这样写不会假绿**：主线程在 `acquire()` 之后才 `wait(reached_call)`，
    而工作线程的 `set()` 一定发生在它调用 `ensure_metrics_installed` **之后** ——
    即一定在 `reached_call.set()` 之后。所以"结果列表为空"这个观察点严格晚于
    工作线程的起点，不可能把"线程还没启动"误读成"锁挡住了"。

    释放锁用 `finally` 兜底：断言失败时必须让工作线程能退出，否则 pytest 会挂。
    """
    import threading

    from rednote.platform.observability import ensure_metrics_installed
    from rednote.platform.observability.metrics import _REGISTRATION_LOCK

    settings = Settings(database_url=None, environment="test")
    reached_call = threading.Event()
    finished = threading.Event()
    errors: list[str] = []

    def _worker() -> None:
        reached_call.set()
        try:
            ensure_metrics_installed(settings, recording_registry)
        except Exception as exc:  # noqa: BLE001 —— 卡住/报错都要看得见
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            finished.set()

    _REGISTRATION_LOCK.acquire()
    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    try:
        assert reached_call.wait(timeout=10), "工作线程未进入判据函数（测试自身失效）"
        # 给一个宽松但有限的窗口：够短以免拖慢测试，够长以覆盖任何调度延迟。
        finished.wait(timeout=0.5)
        assert not finished.is_set(), (
            "注册锁没有互斥作用：锁已被本线程持有时，第二个调用者仍能完成 —— "
            "CR-2 的竞态未被消除（错误细节：%r）" % (errors,)
        )
    finally:
        _REGISTRATION_LOCK.release()

    assert finished.wait(timeout=10), "释放锁后工作线程仍未完成（疑似真死锁）"
    assert errors == [], f"释放锁后的调用抛出了异常：{errors}"
    # 只创建一次：名字集合里 rednote_build_info 恰好一条。
    names = [m.name for m in recording_registry.collect()]
    assert names.count("rednote_build_info") == 1


def test_ensure_metrics_installed_is_safe_under_concurrency(
    recording_registry: CollectorRegistry,
) -> None:
    """**CR-2 的补充观测（非判据）**：真实两线程并发不抛异常。

    ## ⚠️ 这条**不是**判据 —— 别把它当防线

    首版把它当判据，实测反向验证**失败**（去锁后仍绿）。已查明两个原因：

    1. `Barrier` 对齐在函数**入口**时，两线程随后各自的"查+建"仍可能**串行完成**
       （GIL + 窗口极短）⇒ 第二个线程进函数时第一个已建好 ⇒ 走"已存在"分支。
    2. 对齐挪进"查完、还没建"那一刻，又会与"有锁时不阻塞"互斥 ⇒ 有锁时
       `Barrier` 永远凑不齐第二人（判据把实现 deadlock 死）。

    保留它的理由是**它仍然测到了一件独立的事**：真实线程交错下没有异常、
    且结果只有一份。生产缺陷（`Duplicated timeseries`）确实由这条先发现 ——
    但它**无法保证复现**，所以判据责任交给上面两条判定式用例。

    判据的分工（本项目纪律第 6 条的推论："变异无红 ≠ 判据多余"）：
    **确定性判据负责报红**，这条负责**在真实交错下兜住未知形状**。
    """
    import threading

    from rednote.platform.observability import ensure_metrics_installed

    settings = Settings(database_url=None, environment="test")
    barrier = threading.Barrier(2)
    errors: list[str] = []
    results: list[CollectorRegistry] = []

    def _install() -> None:
        # Barrier 让两线程在"进入函数前"对齐 —— 之后它们在锁外竞争。
        barrier.wait()
        try:
            results.append(ensure_metrics_installed(settings, recording_registry))
        except Exception as exc:  # noqa: BLE001 —— 要看的就是"有没有异常"
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=_install) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == [], f"并发注册指标抛出了异常：{errors}"
    assert len(results) == 2, f"有线程未返回结果（可能死锁）：{len(results)}"
    # 只创建一次：名字集合里 rednote_build_info 恰好一条。
    names = [m.name for m in recording_registry.collect()]
    assert names.count("rednote_build_info") == 1


# ===========================================================================
# CR-3：日志级别不得打开第三方库的 INFO（SQL / 内网 URL / 访问日志）
# ===========================================================================


def test_configure_logging_does_not_enable_third_party_info() -> None:
    """**CR-3 的成对判据**：本 app 的 INFO 出来，第三方库的 INFO **不**出来。

    ## 为什么这条判据必须存在

    首版 `configure_logging` 写的是 `logging.getLogger().setLevel(level)` ——
    把 **root** 降到 INFO，于是**所有未被单独设级的第三方 logger 一起继承 INFO**。
    本机实测三个后果（默认 `log_level=INFO`，不需要任何人做错事）：

        INFO sqlalchemy.engine.Engine BEGIN (implicit) SELECT users.email, users.password_hash ...
        INFO uvicorn.access GET /x 200
        INFO httpx HTTP Request: POST https://internal.svc/token

    即 **SQL 语句与绑定参数（含个人数据）、内网 URL、全部访问日志** 一起打开 ——
    与 ADR-11 C6「仅运维面」和本模块"日志不泄露内部细节"的目标**直接冲突**。

    ## 为什么必须成对（只有"不出现"半边会骗过自己）

    只断言"第三方 INFO 不出现"，一个**整个 handler 都不工作**的实现照样通过。
    因此另一半断言 `rednote.*` 的 INFO **必须**出现 —— 两条一起才证明
    "级别加在了正确的 logger 上"，而不是"日志根本没接上"。

    ⚠️ **反向验证**：把 `logging.getLogger(APP_LOGGER_NAME).setLevel(level)`
    改回 `logging.getLogger().setLevel(level)`，本用例的第三方半边必须红。
    """
    from rednote.platform.observability import APP_LOGGER_NAME

    root = logging.getLogger()
    root_level_before = root.level
    app_logger = logging.getLogger(APP_LOGGER_NAME)
    app_level_before = app_logger.level

    # 前置：确认第三方与 app logger 都处于"未单独设级"的状态，
    # 否则本用例测的是上一次测试留下的痕迹。
    for name in ("sqlalchemy.engine.Engine", "httpx", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.NOTSET)

    app = create_app(Settings(database_url=None, environment="dev"))
    try:
        ours = [
            h for h in root.handlers if getattr(h, "_is_rednote_observability_handler", False)
        ]
        assert len(ours) == 1, "dev 环境下应恰有一个本模块的 handler"

        buffer = io.StringIO()
        original_stream = ours[0].stream
        ours[0].setStream(buffer)
        try:
            logging.getLogger("sqlalchemy.engine.Engine").info(
                "SELECT users.email, users.password_hash FROM users WHERE id = %s",
                ("user-42@example.com",),
            )
            logging.getLogger("httpx").info("HTTP Request: POST https://internal.svc/token")
            logging.getLogger("uvicorn.access").info("GET /x 200")
            # 另一半：本 app 的 INFO **必须**出来（否则"handler 不工作"也能骗过上面三条）。
            logging.getLogger(f"{APP_LOGGER_NAME}.cr3.probe").info("本 app 的 INFO 应落盘")
            output = buffer.getvalue()
        finally:
            ours[0].setStream(original_stream or io.StringIO())
            for handler in list(root.handlers):
                if getattr(handler, "_is_rednote_observability_handler", False):
                    root.removeHandler(handler)
    finally:
        root.setLevel(root_level_before)
        app_logger.setLevel(app_level_before)
        # 清理本用例可能给第三方 logger 留下的级别（我们不设，但要防止外部设置残留）。
        for name in ("sqlalchemy.engine.Engine", "httpx", "uvicorn.access"):
            logging.getLogger(name).setLevel(logging.NOTSET)

    assert app is not None  # app 装配成功

    # 半边 A：第三方 INFO **不得**落盘（CR-3 的核心断言）。
    leaked = [
        line
        for line in output.splitlines()
        if "password_hash" in line or "internal.svc" in line or "GET /x 200" in line
    ]
    assert leaked == [], (
        f"配置日志级别时打开了第三方库的 INFO，泄露了 SQL/内网 URL/访问日志（CR-3）：{leaked}"
    )

    # 半边 B：本 app 的 INFO **必须**落盘 —— 证明上面的"不泄露"不是因为日志没接上。
    assert "本 app 的 INFO 应落盘" in output, (
        f"本 app 的 INFO 未落盘 —— 级别没有加在 {APP_LOGGER_NAME} logger 上"
        f"（上面的'无泄露'即成假象）：{output!r}"
    )
