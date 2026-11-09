"""错误码体系、统一响应包装、脱敏与异常处理器（协议 §1.2 / §1）。

**为什么错误码要做成枚举而不是一堆字符串常量**：
`ErrorBody.code` 用 `ErrorCode` 枚举后，错误码会作为 enum 进入 OpenAPI 契约，
前端能据此生成 TS 联合类型并写穷尽分支（协议 §13.3）。若用裸 str，
前端只能手抄一份错误码清单 —— 正是协议 §13.2 要禁止的"手工维护两套字段"。

**为什么 `INTERNAL_ERROR` 刻意不进 `ErrorCode`**：
协议 §1.2 的表只列"可预期错误"，没有 500 —— 因为未捕获异常是缺陷，不是业务状态。
若把它加进 `ErrorCode`，它会自动获得 HTTP 状态映射并进入契约里 `ErrorBody.code`
的 enum，于是一个"代码 bug"就变成契约里合法的业务错误码，前端会以为可以按它写业务分支。
因此用 `Literal["INTERNAL_ERROR"]` 单值约束：它以单值 enum 出现在 500 的契约声明里
（前端能生成类型），但不在 `ErrorCode` 枚举中。

**为什么 details 在返回前统一脱敏**：
约定靠自觉，脱敏靠代码。`details` 会经过多个模块的手（健康检查、文件上传、
模型网关），任何一处顺手把连接信息塞进去都会直接对外。做一次收口，
代价是偶尔多打码一个无害字段（可接受），收益是"凭据不可能经 details 外泄"。
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from enum import StrEnum
from typing import Any, Final, Generic, Literal, TypeVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

# 请求追踪头名（协议 §1.1）。
REQUEST_ID_HEADER: Final[str] = "X-Request-Id"

# 脱敏后的统一占位符。用固定常量便于前端与排障时识别。
REDACTED: Final[str] = "***REDACTED***"

# details 中单个字符串值的截断长度。取 512 是因为 details 是"给人看的上下文"，
# 不是数据传输通道；超长值（例如误把整个文档塞进来）只会让响应体膨胀。
_MAX_DETAIL_STRING_LENGTH: Final[int] = 512

_DEFAULT_REQUEST_ID: Final[str] = "unknown"


class ErrorCode(StrEnum):
    """协议 §1.2 的错误码，逐条照抄，不多不少。

    多一个 = 自创错误码（协议不允许）；少一个 = 漏实现。
    测试 `test_error_code_set_matches_protocol_exactly` 会独立核对这张表。
    """

    MALFORMED_REQUEST = "MALFORMED_REQUEST"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    EXTERNAL_POLICY_DENIED = "EXTERNAL_POLICY_DENIED"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    DUPLICATE_PUBLICATION = "DUPLICATE_PUBLICATION"
    EVENT_HISTORY_EXPIRED = "EVENT_HISTORY_EXPIRED"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    UPLOAD_TOO_LARGE = "UPLOAD_TOO_LARGE"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    DIRECTION_UNVERIFIED = "DIRECTION_UNVERIFIED"
    PRECONDITION_REQUIRED = "PRECONDITION_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"


# 协议 §1.2 状态码列。
_STATUS_BY_CODE: Final[dict[ErrorCode, int]] = {
    ErrorCode.MALFORMED_REQUEST: 400,
    ErrorCode.SESSION_EXPIRED: 401,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.EXTERNAL_POLICY_DENIED: 403,
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.INVALID_TRANSITION: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.DUPLICATE_PUBLICATION: 409,
    ErrorCode.EVENT_HISTORY_EXPIRED: 410,
    ErrorCode.VERSION_CONFLICT: 412,
    ErrorCode.UPLOAD_TOO_LARGE: 413,
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.BUDGET_EXCEEDED: 422,
    ErrorCode.DIRECTION_UNVERIFIED: 422,
    ErrorCode.PRECONDITION_REQUIRED: 428,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
}

# 协议 §1.2 "前端行为"列直接推出的可重试集合。
# 只有这两类：429（尊重 Retry-After）与 503（保持原幂等键核对）。
# 其余一律不可重试 —— 尤其计费类写操作，盲目重试会造成重复扣费。
_RETRYABLE_BY_CODE: Final[dict[ErrorCode, bool]] = {
    code: code in {ErrorCode.RATE_LIMITED, ErrorCode.DEPENDENCY_UNAVAILABLE}
    for code in ErrorCode
}

# 默认中文提示。message 可被调用方覆盖；这些只是兜底，
# 保证任何错误响应都有非空 message（协议 §1.2 要求该字段存在）。
_MESSAGE_BY_CODE: Final[dict[ErrorCode, str]] = {
    ErrorCode.MALFORMED_REQUEST: "请求格式不正确。",
    ErrorCode.SESSION_EXPIRED: "会话已过期，请重新登录。",
    ErrorCode.PERMISSION_DENIED: "没有执行该操作的权限。",
    ErrorCode.EXTERNAL_POLICY_DENIED: "当前项目策略不允许访问外部模型。",
    ErrorCode.RESOURCE_NOT_FOUND: "资源不存在或无权访问。",
    ErrorCode.INVALID_TRANSITION: "当前状态不允许该操作。",
    ErrorCode.IDEMPOTENCY_CONFLICT: "相同幂等键对应了不同的请求内容。",
    ErrorCode.DUPLICATE_PUBLICATION: "该笔记已登记过，请勿重复计入。",
    ErrorCode.EVENT_HISTORY_EXPIRED: "事件历史已过期，请全量同步。",
    ErrorCode.VERSION_CONFLICT: "内容已被更新，请比较新版本后再提交。",
    ErrorCode.UPLOAD_TOO_LARGE: "上传内容超出允许大小。",
    ErrorCode.VALIDATION_FAILED: "字段校验未通过。",
    ErrorCode.BUDGET_EXCEEDED: "项目预算不足，无法提交该任务。",
    ErrorCode.DIRECTION_UNVERIFIED: "该方向缺少已验证的通过样例。",
    ErrorCode.PRECONDITION_REQUIRED: "缺少 If-Match 等前置条件。",
    ErrorCode.RATE_LIMITED: "请求过于频繁，请稍后重试。",
    ErrorCode.DEPENDENCY_UNAVAILABLE: "依赖服务暂时不可用。",
}

_INTERNAL_ERROR_MESSAGE: Final[str] = "服务内部错误。"

# HTTP 状态 -> 业务错误码。用于把框架自身抛的裸 HTTPException
# （例如路由不存在、Starlette 内部错误）折算成协议错误体，
# 避免漏出 FastAPI 默认的 {"detail": ...}。
_CODE_BY_STATUS: Final[dict[int, ErrorCode]] = {
    400: ErrorCode.MALFORMED_REQUEST,
    401: ErrorCode.SESSION_EXPIRED,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.RESOURCE_NOT_FOUND,
    409: ErrorCode.INVALID_TRANSITION,
    410: ErrorCode.EVENT_HISTORY_EXPIRED,
    412: ErrorCode.VERSION_CONFLICT,
    413: ErrorCode.UPLOAD_TOO_LARGE,
    422: ErrorCode.VALIDATION_FAILED,
    428: ErrorCode.PRECONDITION_REQUIRED,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.DEPENDENCY_UNAVAILABLE,
}

# ---------------------------------------------------------------------------
# requestId 传递（ContextVar）
#
# 为什么用 ContextVar 而不是把 request 传进每个函数：
# requestId 需要出现在「成功响应的 meta」「错误响应的 meta」「响应头」三处。
# 若靠参数传递，异常处理器（由框架调用、拿不到业务函数的参数）就无法读到它。
# ContextVar 让这三处都从同一个来源取值，避免出现"头里一个值、体里另一个值"。
# ---------------------------------------------------------------------------

_request_id_var: ContextVar[str] = ContextVar("rednote_request_id", default=_DEFAULT_REQUEST_ID)


def set_request_id(value: str) -> Token[str]:
    """写入当前请求的 requestId，返回用于复位的 token。"""
    return _request_id_var.set(value)


def get_request_id() -> str:
    """读取当前请求的 requestId。无请求上下文时返回占位值。"""
    return _request_id_var.get()


def reset_request_id(token: Token[str]) -> None:
    """复位 ContextVar。

    必须复位：同一 asyncio 任务会被复用来处理后续请求，不复位会让
    "上一个请求的 requestId"泄漏给下一个请求，表现为日志串号。
    """
    _request_id_var.reset(token)


def build_meta() -> dict[str, str]:
    """构造协议 §1 的 meta 块。"""
    return {"requestId": get_request_id()}


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------

# 键名命中即整个值打码。统一小写比较，覆盖常见的多种写法。
_SENSITIVE_KEY_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "token",
        "secret",
        "dsn",
        "sql",
        "stack",
        "traceback",
        "apikey",
        "credential",
        "credentials",
        "authorization",
        "databaseurl",
        "dburl",
        "connectionstring",
        "privatekey",
    }
)

# 值级兜底：即使键名无害（如 {"host": "postgresql://..."}），值是连接串也要打码。
_DSN_VALUE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb|redis|amqp|rabbitmq)"
    r"(?:\+\w+)?://"
)
# password=hunter2 / pwd: hunter2 形式。
_KEY_VALUE_SECRET_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(?:password|passwd|pwd|token|secret|apikey|api_key)\s*[:=]\s*\S+"
)


def _is_sensitive_key(key: str) -> bool:
    """键名是否命中敏感标记。

    先把键名归一化（小写 + 去掉下划线与连字符），这样 api_key / apiKey /
    api-key 都会归一成 apikey，只用维护一份标记表。
    """
    normalized = key.lower().replace("_", "").replace("-", "")
    return any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS)


def _scrub_string(value: str) -> str:
    """值级脱敏 + 截断。"""
    if _DSN_VALUE_RE.search(value) or _KEY_VALUE_SECRET_RE.search(value):
        return REDACTED
    if len(value) > _MAX_DETAIL_STRING_LENGTH:
        return value[:_MAX_DETAIL_STRING_LENGTH] + "<truncated>"
    return value


def sanitize_details(details: Any) -> dict[str, Any]:
    """递归脱敏 details。

    递归是必须的：初版只在顶层做键名检查，`{"nested": {"token": "..."}}`
    会原样外泄（本机实测确认，已加回归用例 test_details_redacts_nested_credentials）。
    """
    if details is None:
        return {}
    if not isinstance(details, dict):
        # 非 dict 输入不猜测意图，直接归空 —— 调用方传错类型不该产出一个畸形响应。
        return {}

    sanitized: dict[str, Any] = {}
    for key, value in details.items():
        if not isinstance(key, str):
            # JSON 对象键必须是字符串；非字符串键无法安全序列化，直接丢弃。
            continue
        if _is_sensitive_key(key):
            sanitized[key] = REDACTED
            continue
        sanitized[key] = _sanitize_value(value)
    return sanitized


def _sanitize_value(value: Any) -> Any:
    """对单个值递归处理：dict 逐键、list 逐项、str 打码截断、其余原样。"""
    if isinstance(value, dict):
        return sanitize_details(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        return _scrub_string(value)
    return value


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ApiError(Exception):
    """业务异常。

    `details` 会在返回前统一脱敏；`headers` 用于需要附加响应头的场景
    （协议 §1.2：429 要求"尊重 Retry-After"）。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message or _MESSAGE_BY_CODE[code])
        self.code = code
        self.message = message or _MESSAGE_BY_CODE[code]
        self.details = details or {}
        self.headers = headers


# ---------------------------------------------------------------------------
# 响应模型（同时用于运行时构造与 OpenAPI 契约）
# ---------------------------------------------------------------------------

T = TypeVar("T")


class Meta(BaseModel):
    """协议 §1 的 meta 块。"""

    model_config = ConfigDict(populate_by_name=True)

    request_id: str = Field(alias="requestId", serialization_alias="requestId")


class DataEnvelope(BaseModel, Generic[T]):
    """协议 §1：单资源响应 `{"data": {...}, "meta": {...}}`。

    **为什么必须是泛型**：契约测试要求 `data` 在 OpenAPI 里带出具体结构
    （`envelope["properties"]["data"]["$ref"]` 指向 LivenessBody），
    而不是裸 object —— 否则前端 SDK 会退化成 any，契约失去约束力（协议 §13.3）。
    泛型参数化后 pydantic 会为每个具体类型生成独立 schema。

    `data` 接受任意可 JSON 化的内容：调用方传 dict 或 pydantic 模型都行，
    这样 handler 不必为包装形状再写一遍转换。
    """

    model_config = ConfigDict(populate_by_name=True)

    data: T
    meta: Meta


class ErrorBody(BaseModel):
    """协议 §1.2 的 error 块。

    `code` 用 `ErrorCode` 枚举，让错误码作为 enum 进入契约（前端可生成联合类型）。
    """

    model_config = ConfigDict(populate_by_name=True)

    code: ErrorCode
    message: str
    details: dict[str, Any]
    retryable: bool


class InternalErrorBody(BaseModel):
    """500 专用 error 块。

    `code` 用单值 Literal 而非 `ErrorCode`：`INTERNAL_ERROR` 是"代码缺陷"信号，
    不能混进业务错误码 enum（见模块 docstring）。
    但它仍需以单值 enum 出现在契约里，前端才能生成类型。
    """

    model_config = ConfigDict(populate_by_name=True)

    code: Literal["INTERNAL_ERROR"]
    message: str
    details: dict[str, Any]
    retryable: bool


class ErrorEnvelopeForOpenApi(BaseModel):
    """协议 §1.2 的错误响应包装，供端点 `responses=` 声明使用。

    为什么必须显式声明：错误响应由 exception_handler 直接构造 JSONResponse 返回，
    不经过 response_model 校验。不声明的话契约里 4xx/5xx 就没有 body 结构，
    前端无法据此生成错误 DTO（协议 §13.3）。
    """

    model_config = ConfigDict(populate_by_name=True)

    error: ErrorBody
    meta: Meta


class InternalErrorEnvelope(BaseModel):
    """500 的错误响应包装。走独立模型，避免兜底处理器因枚举外的值而自身崩溃。"""

    model_config = ConfigDict(populate_by_name=True)

    error: InternalErrorBody
    meta: Meta


def error_response(
    code: ErrorCode,
    message: str | None = None,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """构造协议 §1.2 的错误响应（含脱敏）。"""
    body = {
        "error": {
            "code": code.value,
            "message": message or _MESSAGE_BY_CODE[code],
            "details": sanitize_details(details),
            "retryable": _RETRYABLE_BY_CODE[code],
        },
        "meta": build_meta(),
    }
    return JSONResponse(
        status_code=_STATUS_BY_CODE[code],
        content=body,
        headers=headers,
    )


def _internal_error_response() -> JSONResponse:
    """构造 500 响应。不带堆栈、不带异常正文（C6）。"""
    body = {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": _INTERNAL_ERROR_MESSAGE,
            "details": {},
            "retryable": False,
        },
        "meta": build_meta(),
    }
    return JSONResponse(status_code=500, content=body)


# ---------------------------------------------------------------------------
# 异常处理器注册
# ---------------------------------------------------------------------------


def register_exception_handlers(app: FastAPI) -> None:
    """把所有异常统一收敛成协议错误体。

    注册顺序无关紧要（FastAPI 按异常类型精确匹配），但四类必须齐全：
    业务异常、请求校验失败、框架 HTTP 异常、未捕获兜底。
    """

    @app.exception_handler(ApiError)
    async def _handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return error_response(
            code=exc.code,
            message=exc.message,
            details=exc.details,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 转成可定位的 fieldErrors。刻意**不回显原始输入值** ——
        # 输入可能含敏感内容，且测试 test_validation_error_does_not_echo_input_value
        # 明确要求其不出现在响应里。
        field_errors = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ())),
                "type": str(err.get("type", "value_error")),
                "message": str(err.get("msg", "字段值不合法。")),
            }
            for err in exc.errors()
        ]
        return error_response(
            code=ErrorCode.VALIDATION_FAILED,
            details={"fieldErrors": field_errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # 框架自身的 HTTPException（含路由不存在）折算成协议错误码，
        # 使响应体里一定有 code，而不是 FastAPI 默认的 {"detail": ...}。
        code = _CODE_BY_STATUS.get(exc.status_code)
        if code is None:
            # 未映射的 5xx 走 INTERNAL_ERROR；4xx 统一按 MALFORMED_REQUEST。
            if exc.status_code >= 500:
                return _internal_error_response()
            code = ErrorCode.MALFORMED_REQUEST
        return error_response(code=code)

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, _exc: Exception) -> JSONResponse:
        # 兜底：任何未捕获异常都返回结构化 500，且不带堆栈。
        # 真实原因由服务端日志按 requestId 记录，供排障用。
        return _internal_error_response()


__all__ = [
    "REDACTED",
    "REQUEST_ID_HEADER",
    "ApiError",
    "DataEnvelope",
    "ErrorBody",
    "ErrorCode",
    "ErrorEnvelopeForOpenApi",
    "InternalErrorBody",
    "InternalErrorEnvelope",
    "Meta",
    "build_meta",
    "error_response",
    "get_request_id",
    "register_exception_handlers",
    "reset_request_id",
    "sanitize_details",
    "set_request_id",
]