"""统一错误模型与错误码体系测试（WP07）。

覆盖：
1. 错误码表与协议 §1.2 逐条对齐（状态码映射、retryable 语义）。
2. 错误响应包装形状 {"error": {...}, "meta": {...}}。
3. details 脱敏（C6：不泄露凭据、SQL、堆栈）。
4. 框架自身异常（404 路由不存在、422 校验失败）被折算成协议错误码，
   而不是漏出 FastAPI 默认的 {"detail": ...}。
5. 游标分页工具（同属 WP07 的"统一分页"）。

测试通过一个临时挂载的路由触发各类错误。为什么不在测试里真的去调
业务接口：骨架期没有业务接口，而错误处理器的行为与业务无关 ——
用一个最小路由触发即可，且不引入对后续 WP 的依赖。
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from rednote.platform.errors import (
    ErrorCode,
    _RETRYABLE_BY_CODE,
    _STATUS_BY_CODE,
    ApiError,
    sanitize_details,
)
from rednote.platform.pagination import (
    CursorPosition,
    CursorSpec,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    PageParams,
    PageParamsDep,
    decode_cursor,
    encode_cursor,
    list_response,
    paginate_items,
)

# 协议 §1.2 表格的权威副本。写成字面量而不是从代码 import ——
# 若从代码 import，代码改错了测试会跟着一起错，测试就失去意义。
# 这张表是"协议说了什么"的独立记录。
PROTOCOL_STATUS_TABLE: dict[str, int] = {
    "MALFORMED_REQUEST": 400,
    "SESSION_EXPIRED": 401,
    "PERMISSION_DENIED": 403,
    "EXTERNAL_POLICY_DENIED": 403,
    "RESOURCE_NOT_FOUND": 404,
    "INVALID_TRANSITION": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "DUPLICATE_PUBLICATION": 409,
    "EVENT_HISTORY_EXPIRED": 410,
    "VERSION_CONFLICT": 412,
    "UPLOAD_TOO_LARGE": 413,
    "VALIDATION_FAILED": 422,
    "BUDGET_EXCEEDED": 422,
    "DIRECTION_UNVERIFIED": 422,
    "PRECONDITION_REQUIRED": 428,
    "RATE_LIMITED": 429,
    "DEPENDENCY_UNAVAILABLE": 503,
}

# 协议 §1.2 "前端行为"列直接推出的可重试集合。
# 只有这两类：429（尊重 Retry-After）与 503（保持原幂等键核对）。
PROTOCOL_RETRYABLE: frozenset[str] = frozenset({"RATE_LIMITED", "DEPENDENCY_UNAVAILABLE"})


class Payload(BaseModel):
    """测试用请求体。

    定义在模块级而不是 fixture 内部：FastAPI 解析端点签名时用的是函数的
    `__globals__`，局部定义的类无法被解析，会被当成未解析的 ForwardRef
    并错误地按 query 参数处理（本机实测报 AttributeError:
    __pydantic_core_schema__）。这不是风格问题，是必须放在模块级。
    """

    count: int


def _build_error_app() -> FastAPI:
    """最小 app：挂几个必然报错的端点，用来触发各异常处理器。

    路由函数同样定义在模块级函数体里但**不引用局部类** —— 所有注解类型
    都来自模块级或已导入的名字。
    """
    from rednote.main import create_app
    from rednote.platform.configuration import Settings

    app = create_app(Settings(database_url=None, environment="test"))

    @app.get("/api/v1/_test/raise/{code}")
    async def _raise(code: str) -> None:
        # 用于逐码验证状态码映射。
        try:
            resolved = ErrorCode(code)
        except ValueError:
            raise ApiError(ErrorCode.MALFORMED_REQUEST, details={"unknownCode": code})
        raise ApiError(resolved)

    @app.get("/api/v1/_test/raise-with-details")
    async def _raise_with_details() -> None:
        raise ApiError(
            ErrorCode.VERSION_CONFLICT,
            details={
                "resourceId": "content-01",
                "currentEtag": '"content-01:18"',
                "fieldErrors": [],
                # 下面几项用于验证脱敏：都不应原样出现在响应里。
                "databaseUrl": "postgresql+psycopg://rednote:secretpw@db:5432/rednote",
                "password": "hunter2",
                "apiKey": "sk-live-abcdef",
                "note": "dsn=postgresql://u:p@h/db",
                "nested": {"token": "nested-token-value", "safe": "kept"},
            },
        )

    @app.get("/api/v1/_test/raise-unexpected")
    async def _raise_unexpected() -> None:
        raise RuntimeError("boom: postgresql+psycopg://rednote@localhost/db")

    @app.get("/api/v1/_test/rate-limited")
    async def _rate_limited() -> None:
        raise ApiError(
            ErrorCode.RATE_LIMITED,
            headers={"Retry-After": "30"},
        )

    @app.post("/api/v1/_test/validate")
    async def _validate(payload: Payload) -> dict[str, int]:
        return {"count": payload.count}

    @app.get("/api/v1/_test/list")
    async def _list(params: PageParamsDep) -> dict:
        # 用真实的分页工具与真实的分页依赖产出列表响应，验证包装形状与游标可用性。
        spec = CursorSpec(sort_fields=("score",), direction="desc")
        items = [{"id": f"item-{index:02d}", "score": 100 - index} for index in range(5)]
        page_items, page = paginate_items(
            items,
            params=params,
            spec=spec,
            sort_key=lambda item: (item["score"],),
            id_of=lambda item: item["id"],
        )
        return list_response(page_items, page)

    @app.get("/api/v1/_test/paged")
    async def _paged(params: PageParamsDep) -> dict[str, int]:
        return {"limit": params.limit}

    return app


@pytest.fixture
def error_app() -> FastAPI:
    return _build_error_app()


@pytest.fixture
def error_client(error_app: FastAPI) -> TestClient:
    return TestClient(error_app)


# ---------------------------------------------------------------------------
# 错误码表与协议一致性
# ---------------------------------------------------------------------------


def test_error_code_set_matches_protocol_exactly() -> None:
    """代码里的错误码集合必须与协议 §1.2 完全一致，不多不少。

    多一个 = 自创错误码（协议明确不允许）；少一个 = 漏实现。
    这条测试是防止"顺手加个新 code"的唯一防线。
    """
    assert {code.value for code in ErrorCode} == set(PROTOCOL_STATUS_TABLE)


@pytest.mark.parametrize("code_value,expected_status", sorted(PROTOCOL_STATUS_TABLE.items()))
def test_status_code_mapping_matches_protocol(
    code_value: str, expected_status: int
) -> None:
    """每个错误码映射到协议规定的 HTTP 状态码。"""
    assert _STATUS_BY_CODE[ErrorCode(code_value)] == expected_status


@pytest.mark.parametrize("code_value", sorted(PROTOCOL_STATUS_TABLE))
def test_retryable_semantics_match_protocol(code_value: str) -> None:
    """retryable 取值必须与协议 §1.2 "前端行为"列一致。"""
    expected = code_value in PROTOCOL_RETRYABLE
    assert _RETRYABLE_BY_CODE[ErrorCode(code_value)] is expected


@pytest.mark.parametrize("code_value,expected_status", sorted(PROTOCOL_STATUS_TABLE.items()))
def test_each_error_code_returns_correct_http_status(
    error_client: TestClient, code_value: str, expected_status: int
) -> None:
    """端到端验证：抛某错误码，HTTP 状态码与错误体都正确。"""
    response = error_client.get(f"/api/v1/_test/raise/{code_value}")

    assert response.status_code == expected_status, response.text
    payload = response.json()
    assert payload["error"]["code"] == code_value
    assert payload["error"]["retryable"] is (code_value in PROTOCOL_RETRYABLE)
    assert payload["error"]["message"]


def test_error_envelope_matches_protocol_shape(error_client: TestClient) -> None:
    """协议 §1.2：{"error": {code, message, details, retryable}, "meta": {requestId}}"""
    payload = error_client.get("/api/v1/_test/raise/VERSION_CONFLICT").json()

    assert set(payload.keys()) == {"error", "meta"}
    assert set(payload["error"].keys()) == {"code", "message", "details", "retryable"}
    assert set(payload["meta"].keys()) == {"requestId"}
    assert isinstance(payload["error"]["details"], dict)
    assert isinstance(payload["error"]["retryable"], bool)


def test_rate_limited_preserves_retry_after_header(error_client: TestClient) -> None:
    """协议 §1.2：429 要求"尊重 Retry-After"，因此响应头必须能透出该值。"""
    response = error_client.get("/api/v1/_test/rate-limited")

    assert response.status_code == 429
    assert response.headers.get("Retry-After") == "30"


def test_unknown_error_code_falls_back_to_malformed_request(
    error_client: TestClient,
) -> None:
    """传入不存在的错误码时不崩溃，按 400 处理。"""
    response = error_client.get("/api/v1/_test/raise/NOT_A_REAL_CODE")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


# ---------------------------------------------------------------------------
# C6 安全：details 脱敏
# ---------------------------------------------------------------------------


def test_details_redacts_credentials_and_dsn(error_client: TestClient) -> None:
    """C6 + 协议 §1.2：禁止返回供应商密钥、数据库 SQL/DSN。

    这是安全断言，不是格式断言 —— 一旦红了就是泄露事故。
    """
    body = error_client.get("/api/v1/_test/raise-with-details").text

    # 明文凭据绝不允许出现。
    assert "secretpw" not in body
    assert "hunter2" not in body
    assert "sk-live-abcdef" not in body
    assert "postgresql+psycopg://" not in body
    assert "postgresql://" not in body


def test_details_preserves_non_sensitive_fields(error_client: TestClient) -> None:
    """脱敏不能过度：正常业务字段必须原样保留，否则前端拿不到冲突详情。"""
    details = error_client.get("/api/v1/_test/raise-with-details").json()["error"][
        "details"
    ]

    assert details["resourceId"] == "content-01"
    assert details["currentEtag"] == '"content-01:18"'
    assert details["fieldErrors"] == []
    assert details["databaseUrl"] == "***REDACTED***"
    assert details["password"] == "***REDACTED***"
    # 嵌套结构里的敏感键同样要打码，无害键保留。
    assert details["nested"]["token"] == "***REDACTED***"
    assert details["nested"]["safe"] == "kept"


def test_details_redacts_nested_credentials(error_client: TestClient) -> None:
    """嵌套结构里的凭据不得外泄。

    这条测试针对一个已修复的真实缺陷：初版只在顶层做键名检查，
    `{"nested": {"token": "..."}}` 会原样输出。
    """
    body = error_client.get("/api/v1/_test/raise-with-details").text

    assert "nested-token-value" not in body


def test_sanitize_details_handles_nested_structures() -> None:
    """嵌套 dict/list 也要被递归脱敏，不能只查顶层键。"""
    sanitized = sanitize_details(
        {
            "outer": {"token": "abc", "safe": "ok"},
            "items": [{"secret": "x"}, "plain"],
        }
    )

    assert sanitized["outer"]["token"] == "***REDACTED***"
    assert sanitized["outer"]["safe"] == "ok"
    assert sanitized["items"][0]["secret"] == "***REDACTED***"
    assert sanitized["items"][1] == "plain"


def test_sanitize_details_truncates_oversized_strings() -> None:
    """超长 details 值被截断，避免响应体膨胀。"""
    sanitized = sanitize_details({"blob": "x" * 10_000})

    assert len(sanitized["blob"]) < 1_000
    assert sanitized["blob"].endswith("<truncated>")


def test_sanitize_details_redacts_dsn_inside_harmless_key() -> None:
    """键名无害但值是 DSN 时也要打码（值级兜底）。"""
    sanitized = sanitize_details({"host": "postgresql://u:p@h:5432/db"})

    assert sanitized["host"] == "***REDACTED***"


def test_sanitize_details_returns_empty_dict_for_none() -> None:
    assert sanitize_details(None) == {}


# ---------------------------------------------------------------------------
# 框架异常折算
# ---------------------------------------------------------------------------


def test_unknown_route_returns_protocol_error_not_fastapi_default(
    error_client: TestClient,
) -> None:
    """路由不存在时返回协议错误体，而不是 FastAPI 默认的 {"detail": "Not Found"}。

    这是 WP07 验收项"404/422 可定位"的直接落点：前端必须能按 code 分支，
    而 FastAPI 默认响应里没有 code 字段。
    """
    response = error_client.get("/api/v1/_test/does-not-exist")

    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert payload["error"]["retryable"] is False
    assert "detail" not in payload


def test_validation_error_returns_protocol_error_with_field_errors(
    error_client: TestClient,
) -> None:
    """请求体校验失败 -> 422 VALIDATION_FAILED，并给出可定位的 fieldErrors。"""
    response = error_client.post("/api/v1/_test/validate", json={"count": "not-an-int"})

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "VALIDATION_FAILED"
    field_errors = payload["error"]["details"]["fieldErrors"]
    assert len(field_errors) >= 1
    assert field_errors[0]["field"] == "body.count"
    assert field_errors[0]["type"]
    assert field_errors[0]["message"]


def test_validation_error_does_not_echo_input_value(error_client: TestClient) -> None:
    """422 详情不回显原始输入值（可能含敏感内容）。"""
    secret_value = "supersecretvalue123"
    response = error_client.post("/api/v1/_test/validate", json={"count": secret_value})

    assert response.status_code == 422
    assert secret_value not in response.text


def test_unexpected_exception_returns_structured_500_without_stacktrace(
    error_client: TestClient,
) -> None:
    """未捕获异常返回结构化 500，且不泄露堆栈或异常正文。

    注意 code 是 INTERNAL_ERROR 而非协议 §1.2 表内任何值 —— 它是明确的
    "这是缺陷、不是业务错误"信号，前端看到就应报障。异常正文只进服务端日志。
    """
    # TestClient 默认 raise_server_exceptions=True 会重抛异常，
    # 这里需要拿到真实响应体，因此临时关闭该行为。
    client = TestClient(error_client.app, raise_server_exceptions=False)
    response = client.get("/api/v1/_test/raise-unexpected")

    assert response.status_code == 500
    payload = response.json()
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["retryable"] is False
    assert "boom" not in response.text
    assert "postgresql+psycopg://" not in response.text
    assert "Traceback" not in response.text
    assert payload["meta"]["requestId"]


def test_error_responses_include_request_id_header(error_client: TestClient) -> None:
    """错误响应的 header 与 body 里 requestId 必须一致。"""
    response = error_client.get("/api/v1/_test/raise/VERSION_CONFLICT")

    assert response.headers["X-Request-Id"] == response.json()["meta"]["requestId"]


# ---------------------------------------------------------------------------
# 游标分页工具（WP07 统一分页）
# ---------------------------------------------------------------------------


def _items(count: int) -> list[dict[str, object]]:
    """构造测试数据：score 递减，id 稳定。"""
    return [
        {"id": f"item-{index:02d}", "score": 1000 - index}
        for index in range(count)
    ]


def _spec(**overrides: object) -> CursorSpec:
    params: dict[str, object] = {"sort_fields": ("score",), "direction": "desc"}
    params.update(overrides)
    return CursorSpec(**params)  # type: ignore[arg-type]


def test_default_and_max_limit_match_protocol() -> None:
    """协议 §1：默认 limit=20，最大 100。"""
    assert DEFAULT_LIMIT == 20
    assert MAX_LIMIT == 100


def test_first_page_returns_limit_items_and_cursor() -> None:
    page_items, page = paginate_items(
        _items(5),
        params=PageParams(limit=2),
        spec=_spec(),
        sort_key=lambda item: (item["score"],),
        id_of=lambda item: str(item["id"]),
    )

    assert [item["id"] for item in page_items] == ["item-00", "item-01"]
    assert page.has_more is True
    assert page.next_cursor is not None


def test_last_page_has_null_cursor() -> None:
    """协议 §1：末页 nextCursor 为 null，hasMore 为 false。"""
    page_items, page = paginate_items(
        _items(3),
        params=PageParams(limit=10),
        spec=_spec(),
        sort_key=lambda item: (item["score"],),
        id_of=lambda item: str(item["id"]),
    )

    assert len(page_items) == 3
    assert page.has_more is False
    assert page.next_cursor is None


def test_walking_all_pages_covers_every_item_exactly_once() -> None:
    """逐页翻完必须不重不漏 —— 这是分页工具唯一真正重要的性质。"""
    all_items = _items(7)
    seen: list[str] = []
    cursor: str | None = None

    for _ in range(20):  # 上界防止实现错误导致死循环
        page_items, page = paginate_items(
            all_items,
            params=PageParams(limit=3, cursor=cursor),
            spec=_spec(),
            sort_key=lambda item: (item["score"],),
            id_of=lambda item: str(item["id"]),
        )
        seen.extend(str(item["id"]) for item in page_items)
        if not page.has_more:
            break
        cursor = page.next_cursor

    assert seen == [str(item["id"]) for item in all_items]
    assert len(seen) == len(set(seen))


def test_cursor_is_opaque_base64_not_plaintext() -> None:
    """协议 §1：游标不透明，前端不能自行拼接。

    验证方式是"能解码出结构但看不出明文"——这里断言游标里不含排序值的
    十进制明文，且整体是 base64url + 校验和的形式。
    """
    _, page = paginate_items(
        _items(5),
        params=PageParams(limit=1),
        spec=_spec(),
        sort_key=lambda item: (item["score"],),
        id_of=lambda item: str(item["id"]),
    )
    cursor = page.next_cursor
    assert cursor is not None

    encoded, _, checksum = cursor.rpartition(".")
    assert len(checksum) == 64  # sha256 hexdigest
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    # 载荷里只有版本、指纹、排序值、id —— 没有可读的查询语义。
    assert set(payload.keys()) == {"v", "f", "k", "i"}


def test_cursor_binds_to_sort_fields() -> None:
    """协议 §1：游标绑定排序与筛选条件。

    用"按 score 排序"签发的游标，拿去"按 name 排序"的查询里必须被拒绝，
    而不是静默返回错位的下一页。
    """
    _, page = paginate_items(
        _items(5),
        params=PageParams(limit=1),
        spec=_spec(),
        sort_key=lambda item: (item["score"],),
        id_of=lambda item: str(item["id"]),
    )
    assert page.next_cursor is not None

    with pytest.raises(ApiError) as excinfo:
        decode_cursor(page.next_cursor, _spec(sort_fields=("name",)))

    assert excinfo.value.code is ErrorCode.MALFORMED_REQUEST
    assert excinfo.value.details["reason"] == "cursor_binding_mismatch"


def test_cursor_binds_to_direction_and_filters() -> None:
    """方向与筛选条件同样参与绑定。"""
    _, page = paginate_items(
        _items(5),
        params=PageParams(limit=1),
        spec=_spec(),
        sort_key=lambda item: (item["score"],),
        id_of=lambda item: str(item["id"]),
    )
    assert page.next_cursor is not None

    with pytest.raises(ApiError):
        decode_cursor(page.next_cursor, _spec(direction="asc"))
    with pytest.raises(ApiError):
        decode_cursor(page.next_cursor, _spec(filters={"phase": "PRODUCTION"}))


def test_tampered_cursor_is_rejected() -> None:
    """篡改载荷会被校验和发现，返回 400 而不是静默接受。"""
    spec = _spec()
    cursor = encode_cursor(spec, CursorPosition(sort_values=(5,), item_id="item-05"))
    encoded, _, checksum = cursor.rpartition(".")
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    payload["i"] = "item-999"  # 篡改 id
    tampered = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")

    with pytest.raises(ApiError) as excinfo:
        decode_cursor(f"{tampered}.{checksum}", spec)

    assert excinfo.value.details["reason"] == "cursor_checksum_mismatch"


@pytest.mark.parametrize(
    "bad_cursor,expected_reason",
    [
        ("no-dot-separator", "cursor_malformed"),
        ("!!!not-base64!!!.deadbeef", "cursor_not_base64"),
    ],
)
def test_malformed_cursors_return_malformed_request(
    bad_cursor: str, expected_reason: str
) -> None:
    """畸形游标返回 400 MALFORMED_REQUEST，不抛未捕获异常。"""
    with pytest.raises(ApiError) as excinfo:
        decode_cursor(bad_cursor, _spec())

    assert excinfo.value.code is ErrorCode.MALFORMED_REQUEST
    assert excinfo.value.details["reason"] == expected_reason


def test_cursor_from_other_version_is_rejected() -> None:
    """版本不匹配的游标明确失效，不与新格式静默混用。"""
    spec = _spec()
    body = json.dumps({"v": 999, "f": spec.fingerprint, "k": [1], "i": "x"}).encode()
    encoded = base64.urlsafe_b64encode(body).decode().rstrip("=")
    import hashlib

    cursor = f"{encoded}.{hashlib.sha256(body).hexdigest()}"

    with pytest.raises(ApiError) as excinfo:
        decode_cursor(cursor, spec)

    assert excinfo.value.details["reason"] == "cursor_version_unsupported"


def test_stable_ordering_appends_id_as_tiebreaker() -> None:
    """协议 §1：稳定排序追加 id。

    两条记录 score 相同时，必须按 id 有序，否则游标锚点不唯一，
    翻页会漏项或重复。
    """
    tied = [
        {"id": "b", "score": 10},
        {"id": "a", "score": 10},
        {"id": "c", "score": 10},
    ]
    page_items, _ = paginate_items(
        tied,
        params=PageParams(limit=3),
        spec=_spec(),
        sort_key=lambda item: (item["score"],),
        id_of=lambda item: str(item["id"]),
    )

    # desc 方向下 id 也是 desc。
    assert [item["id"] for item in page_items] == ["c", "b", "a"]


def test_ascending_direction_is_supported() -> None:
    page_items, page = paginate_items(
        _items(4),
        params=PageParams(limit=2),
        spec=_spec(direction="asc"),
        sort_key=lambda item: (item["score"],),
        id_of=lambda item: str(item["id"]),
    )

    # score 递增即 item-03, item-02（score 997, 998）。
    assert [item["id"] for item in page_items] == ["item-03", "item-02"]
    assert page.has_more is True


def test_list_response_matches_protocol_envelope(error_client: TestClient) -> None:
    """协议 §1：列表响应 {"data": [...], "page": {nextCursor, hasMore}, "meta": {...}}"""
    payload = error_client.get("/api/v1/_test/list", params={"limit": 2}).json()

    assert set(payload.keys()) == {"data", "page", "meta"}
    assert set(payload["page"].keys()) == {"nextCursor", "hasMore"}
    assert isinstance(payload["data"], list)
    assert len(payload["data"]) == 2
    assert payload["page"]["hasMore"] is True
    assert payload["page"]["nextCursor"] is not None
    assert payload["meta"]["requestId"]


def test_list_endpoint_default_limit_is_protocol_default(
    error_client: TestClient,
) -> None:
    """不传 limit 时按协议默认 20 —— 数据只有 5 条，因此一次拿完且无下一页。"""
    payload = error_client.get("/api/v1/_test/list").json()

    assert len(payload["data"]) == 5
    assert payload["page"]["hasMore"] is False
    assert payload["page"]["nextCursor"] is None


def test_list_endpoint_second_page_via_cursor(error_client: TestClient) -> None:
    """用返回的游标请求第二页，能拿到后续数据（端到端验证游标可用）。"""
    first = error_client.get("/api/v1/_test/list", params={"limit": 2}).json()
    second = error_client.get(
        "/api/v1/_test/list",
        params={"cursor": first["page"]["nextCursor"], "limit": 2},
    ).json()

    assert first["data"] == [
        {"id": "item-00", "score": 100},
        {"id": "item-01", "score": 99},
    ]
    assert second["data"] == [
        {"id": "item-02", "score": 98},
        {"id": "item-03", "score": 97},
    ]
    assert second["page"]["hasMore"] is True


def test_list_endpoint_rejects_cursor_from_different_sort(
    error_client: TestClient,
) -> None:
    """游标绑定校验在端到端路径上同样生效，返回 400 而非错位数据。"""
    # 手工构造一个"排序字段不匹配"的游标：用 spec 指纹不同的位置编码。
    foreign = encode_cursor(
        CursorSpec(sort_fields=("createdAt",), direction="desc"),
        CursorPosition(sort_values=("2026-01-01T00:00:00Z",), item_id="item-99"),
    )
    response = error_client.get("/api/v1/_test/list", params={"cursor": foreign})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"
    assert response.json()["error"]["details"]["reason"] == "cursor_binding_mismatch"


def test_limit_below_minimum_is_rejected_by_query_validation(
    error_client: TestClient,
) -> None:
    """limit=0 与 limit=101 由 FastAPI 的 Query(ge=1, le=100) 拦成 422。

    这条测试守的是"边界校验发生在框架层"这个设计选择：
    若改成在函数体里手写校验，就会有接口忘记校验。
    """
    assert error_client.get("/api/v1/_test/paged", params={"limit": 0}).status_code == 422
    assert (
        error_client.get("/api/v1/_test/paged", params={"limit": 101}).status_code == 422
    )
    assert (
        error_client.get("/api/v1/_test/paged", params={"limit": 100}).status_code == 200
    )
    # 默认值走协议规定的 20。
    assert error_client.get("/api/v1/_test/paged").json()["limit"] == DEFAULT_LIMIT