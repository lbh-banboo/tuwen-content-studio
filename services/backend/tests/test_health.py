"""健康检查竖切片测试（WP06）。

覆盖三件事：
1. `/health` 不碰数据库 —— 用一个"未配置数据库"的 app 也能 200，
   这就是它作为 liveness 探针的定义性行为。
2. `/health/db` 真连本机 PG 并报告 pgvector 版本。
3. 连库失败时返回 503 DEPENDENCY_UNAVAILABLE（协议 §1.2），不是 500。

第 2 项不做 mock：竖切片的全部意义就是证明这条链路真的通。
连不上就让它红 —— 那是必须被看到的信息，不是要绕过的障碍。
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient

from rednote.platform.errors import REQUEST_ID_HEADER

# 冒烟集：进程存活 + 真库连通 + pgvector 可用 —— 这三条不通，其余测试没有意义。
pytestmark = pytest.mark.smoke

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# 进程存活腿：不查库
# ---------------------------------------------------------------------------


def test_health_returns_ok_without_database_configured(
    client_without_database: TestClient,
) -> None:
    """未配置数据库时 /health 仍必须 200。

    这是本端点存在的理由：数据库挂了不该让编排平台把应用实例重启掉。
    如果这个测试红了，说明有人把连库逻辑塞进了 liveness 探针。
    """
    response = client_without_database.get("/api/v1/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["status"] == "ok"
    assert payload["data"]["service"]
    assert payload["data"]["version"]
    assert payload["data"]["environment"] == "test"


def test_health_response_matches_protocol_envelope(
    client_without_database: TestClient,
) -> None:
    """协议 §1：单资源响应必须是 {"data": {...}, "meta": {"requestId": "..."}}。"""
    payload = client_without_database.get("/api/v1/health").json()

    assert set(payload.keys()) == {"data", "meta"}
    assert set(payload["meta"].keys()) == {"requestId"}
    assert UUID_RE.match(payload["meta"]["requestId"])


def test_health_echoes_valid_client_request_id(
    client_without_database: TestClient,
) -> None:
    """协议 §1.1：客户端提供的合法 UUID 应被透传（保持客户端追踪链路）。"""
    supplied = str(uuid.uuid4())

    response = client_without_database.get(
        "/api/v1/health", headers={REQUEST_ID_HEADER: supplied}
    )

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == supplied
    assert response.json()["meta"]["requestId"] == supplied


def test_health_generates_new_request_id_when_client_value_is_invalid(
    client_without_database: TestClient,
) -> None:
    """非法 requestId 不拒绝请求，而是生成新的。

    理由：这是可观测性辅助字段，不是安全字段。为它返回 400 会让一个
    排障工具变成可用性风险。
    """
    response = client_without_database.get(
        "/api/v1/health", headers={REQUEST_ID_HEADER: "not-a-uuid"}
    )

    assert response.status_code == 200
    generated = response.json()["meta"]["requestId"]
    assert generated != "not-a-uuid"
    assert UUID_RE.match(generated)
    # 响应头与响应体必须一致：两者都读同一个 ContextVar，不能有两处真相。
    assert response.headers[REQUEST_ID_HEADER] == generated


def test_request_id_does_not_leak_across_requests(
    client_without_database: TestClient,
) -> None:
    """同一 client 连续两次请求必须拿到不同的 requestId。

    这条测试守的是 ContextVar 的复位。若中间件不在 finally 里 reset，
    复用同一 asyncio 任务的第二个请求会继承第一个的 requestId，
    表现为日志串号 —— 排障时会把故障定位到错误的请求上。
    """
    first = client_without_database.get("/api/v1/health").json()["meta"]["requestId"]
    second = client_without_database.get("/api/v1/health").json()["meta"]["requestId"]

    assert first != second


def test_health_endpoint_is_not_under_bare_path(
    client_without_database: TestClient,
) -> None:
    """路径必须在 /api/v1 前缀下（协议 §1：同域入口 /api/v1）。"""
    assert client_without_database.get("/health").status_code == 404


# ---------------------------------------------------------------------------
# 连库腿：真连 PG
# ---------------------------------------------------------------------------


def test_health_db_connects_to_real_postgres(client_with_database: TestClient) -> None:
    """真连本机 PostgreSQL 执行 select 1 并返回成功。

    这是 WP06/WP07 竖切片的核心验收：证明
    HTTP -> 路由 -> 平台层 -> psycopg3 -> 真实 PG 整条链路是通的。
    """
    response = client_with_database.get("/api/v1/health/db")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "ok"
    assert data["reachable"] is True
    # PG 17 主线（ADR-11 C1）。
    assert data["server_version"].startswith("17"), data["server_version"]
    assert data["latency_ms"] >= 0


def test_health_db_reports_pgvector_version(client_with_database: TestClient) -> None:
    """报告 pgvector 扩展版本（ADR-11 C1：pgvector 是数据库根能力）。

    注意：这里断言的是"如果装了扩展就报告版本"，而不是"必须装了"。
    未装扩展时 pgvector_version 为 null 且仍返回 200 —— 因为"扩展没装"是
    一个需要被看到的事实，不是健康检查失败。把它做成 503 会让未装 pgvector
    的合法部署（例如只跑不涉及向量的模块）无法就绪。
    """
    response = client_with_database.get("/api/v1/health/db")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert "pgvector_version" in data
    if data["pgvector_version"] is not None:
        # 版本形如 "0.8.6"。
        assert re.match(r"^\d+\.\d+", data["pgvector_version"]), data["pgvector_version"]


def test_health_db_reports_effective_timeouts(client_with_database: TestClient) -> None:
    """响应里必须能看到实际生效的超时数值（C4 可验证性）。

    为什么把超时配置放进响应：超时是本切片的核心交付项之一，
    如果只在代码里配了、运行时看不到，就无法从外部验证"配置真的被注入了"。
    这些值不含任何凭据或网络位置，可以安全外泄。
    """
    timeouts = client_with_database.get("/api/v1/health/db").json()["data"]["timeouts"]

    # 所有超时都必须是正数，禁止 None（C4：显式超时数值）。
    assert timeouts["connectTimeoutSeconds"] == 3
    assert timeouts["statementTimeoutMs"] == 2000
    assert timeouts["poolTimeoutSeconds"] > 0
    assert timeouts["healthTotalTimeoutSeconds"] > 0
    assert timeouts["driver"] == "psycopg3"
    assert timeouts["configured"] is True


def test_health_db_returns_503_when_database_not_configured(
    client_without_database: TestClient,
) -> None:
    """未配置 DSN 时返回 503 DEPENDENCY_UNAVAILABLE，不是 500，也不是 200。

    503 与 500 的区别是有意义的：503 带 retryable=true，编排平台与前端会按
    "临时故障、可重试"处理；500 表示服务端缺陷，会触发告警升级。
    把一个"配置缺失"报成 500 会让运维去查代码，而实际上要看配置。
    """
    response = client_without_database.get("/api/v1/health/db")

    assert response.status_code == 503, response.text
    payload = response.json()
    assert payload["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert payload["error"]["retryable"] is True
    assert payload["error"]["details"]["dependency"] == "postgresql"
    assert payload["error"]["details"]["reason"] == "database_url_not_configured"
    # 错误响应也必须带 requestId（协议 §1.2）。
    assert UUID_RE.match(payload["meta"]["requestId"])


def test_health_db_returns_503_when_database_unreachable(
    client_without_database: TestClient,
) -> None:
    """DSN 指向不可达地址时返回 503 而非 500。

    用 127.0.0.1 上一个确定没有监听的端口（1）。连接会被立即拒绝，
    因此不会真的等满 connect_timeout —— 测试保持快速且确定性。
    这里断言的是"失败被正确归类为依赖不可用"，而不是"网络确实断了"。
    """
    from fastapi.testclient import TestClient as _TestClient

    from rednote.main import create_app
    from rednote.platform.configuration import Settings

    settings = Settings(
        database_url="postgresql+psycopg://rednote@127.0.0.1:1/postgres",
        environment="test",
    )
    with _TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health/db")

    assert response.status_code == 503, response.text
    payload = response.json()
    assert payload["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert payload["error"]["retryable"] is True
    # reason 是异常类名，不含异常正文 —— C6：不泄露内部细节。
    reason = payload["error"]["details"]["reason"]
    assert isinstance(reason, str) and reason
    assert "postgresql+psycopg://" not in response.text


# ---------------------------------------------------------------------------
# 契约形状
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/v1/health", "/api/v1/health/db"])
def test_health_endpoints_reject_unexpected_query_params_gracefully(
    client_with_database: TestClient, path: str
) -> None:
    """未声明的查询参数被忽略而不是 422。

    FastAPI 对未声明的 query 参数默认忽略。这里显式钉住该行为：
    健康检查会被各种探针/负载均衡器附加参数（如 ?probe=1），
    因为一个多余参数让探针失败是不可接受的。
    """
    assert client_with_database.get(path, params={"probe": "1"}).status_code in (
        200,
        503,
    )
