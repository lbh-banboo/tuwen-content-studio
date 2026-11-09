"""健康检查竖切片（WP06/WP07 的"最窄可跑通链路"）。

两个端点的分工是刻意的，不要合并：

- `GET /health`     —— **只答"进程活着"，绝不碰数据库**。
  理由：这个端点会被反向代理/容器编排当作存活探针（liveness）。如果它依赖
  数据库，那么数据库短暂不可用会导致编排平台判定"应用已死"并重启所有实例 ——
  重启解决不了数据库问题，反而让故障扩大成"应用全挂"。存活与就绪必须是两个
  探针，这是 ADR-11 C8（双 API 实例 + 反向代理）下的硬要求。

- `GET /health/db`  —— **真连 PG 执行 select 1，并报告 pgvector 版本**。
  这是就绪探针（readiness）语义：依赖不可用时返回 503 DEPENDENCY_UNAVAILABLE
  （协议 §1.2），不是 500。区别很重要：503 带 retryable=true，前端与编排平台
  会按"临时故障"处理；500 表示服务端缺陷，会触发告警升级。

超时是显式的、分层的（C4）：
- libpq connect_timeout、服务端 statement_timeout 在 database.py 里配好；
- 这里额外用 `asyncio.wait_for` 施加"整体硬上限"（默认 3 秒）。
  第三层的必要性：前两层都依赖驱动正确响应，而健康检查最不能出现的情况是
  "探针自己挂住不返回"——那会让编排平台在超时后判定实例失联。硬上限保证无论
  底层怎么卡，这个端点总能在 3 秒内给出结构化答案。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from rednote.platform.configuration import SettingsDep
from rednote.platform.database import connection_facts, probe_database
from rednote.platform.errors import (
    DataEnvelope,
    ErrorCode,
    ErrorEnvelopeForOpenApi,
    InternalErrorEnvelope,
    error_response,
)
from rednote.platform.pagination import single_response

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

# 错误响应的 OpenAPI 声明。抽成常量复用，避免每个端点的 responses 里
# 各写一份导致文档漂移。
#
# 为什么必须显式声明：错误响应由 exception_handler 直接构造 JSONResponse 返回，
# 不经过 response_model 校验。不声明的话契约里 4xx/5xx 就没有 body 结构，
# 前端无法据此生成错误 DTO（协议 §13.3 要求生成 TS 类型与 SDK）。
#
# 400/422/500 是所有端点都可能返回的（框架级校验失败、路由层异常），
# 因此作为"公共错误响应"挂在每个端点上；503 是本端点特有的。
_UNIVERSAL_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_400_BAD_REQUEST: {
        "model": ErrorEnvelopeForOpenApi,
        "description": "请求格式错误（MALFORMED_REQUEST）。",
    },
    status.HTTP_422_UNPROCESSABLE_ENTITY: {
        "model": ErrorEnvelopeForOpenApi,
        "description": "字段校验未通过（VALIDATION_FAILED）。",
    },
    status.HTTP_500_INTERNAL_SERVER_ERROR: {
        "model": InternalErrorEnvelope,
        "description": "服务内部错误（INTERNAL_ERROR，不在协议 §1.2 表内）。",
    },
}

_DEPENDENCY_UNAVAILABLE_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorEnvelopeForOpenApi,
        "description": "依赖不可用（DEPENDENCY_UNAVAILABLE，retryable=true）。",
    }
}


class LivenessBody(BaseModel):
    """进程存活信息。不含任何依赖状态 —— 见模块 docstring。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    status: str = Field(..., description="固定为 ok；只要进程能响应即为 ok。")
    service: str = Field(..., description="服务名。")
    version: str = Field(..., description="服务版本。")
    environment: str = Field(..., description="运行环境标识。")


class DependencyBody(BaseModel):
    """连库探测结果。字段刻意不含主机、用户、DSN。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    status: str = Field(..., description="ok 或 unavailable。")
    reachable: bool
    server_version: str | None = Field(
        default=None, description="PostgreSQL server_version，未连通时为 null。"
    )
    pgvector_version: str | None = Field(
        default=None,
        description="pgvector 扩展版本；未安装扩展时为 null（不等于不可用）。",
    )
    latency_ms: int = Field(..., description="探测耗时毫秒。")
    timeouts: dict[str, Any] = Field(
        ...,
        description=(
            "本次探测实际生效的超时配置，便于排障时确认配置是否被注入。"
            "值类型混合（秒为数字、驱动为字符串），因此用 Any 而非联合标量类型 —— "
            "后者会让 pydantic 在联合成员间做智能猜测（本机实测："
            "dict[str, float|int|bool] 会尝试把 'psycopg3' 解析成 bool 并失败）。"
        ),
    )


@router.get(
    "/health",
    summary="进程存活探针",
    description=(
        "只报告进程能否响应，**不访问数据库**。"
        "用作 liveness 探针：数据库故障不应导致实例被重启。"
    ),
    # 显式声明 response_model，让契约里带出 data 的具体结构而不是裸 object。
    response_model=DataEnvelope[LivenessBody],
    responses=_UNIVERSAL_ERROR_RESPONSES,
)
async def get_health(settings: SettingsDep) -> dict[str, Any]:
    body = LivenessBody(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
    )
    # 协议 §1：单资源包装 {"data": {...}, "meta": {"requestId": "..."}}
    return single_response(body.model_dump(by_alias=True, mode="json"))


@router.get(
    "/health/db",
    summary="数据库就绪探针",
    description=(
        "真连 PostgreSQL 执行 select 1，并报告 pgvector 扩展版本。"
        "不可用时返回 503 DEPENDENCY_UNAVAILABLE（retryable=true），不返回 500。"
    ),
    response_model=DataEnvelope[DependencyBody],
    responses={
        **_UNIVERSAL_ERROR_RESPONSES,
        **_DEPENDENCY_UNAVAILABLE_RESPONSE,
    },
)
async def get_health_db(settings: SettingsDep) -> JSONResponse:
    timeouts = connection_facts(settings)

    # 第三层兜底超时：驱动层之外的硬上限。probe_database 是同步阻塞调用，
    # 用 to_thread 丢到线程池，否则会阻塞整个事件循环（健康检查在双实例
    # 反代下会被频繁调用，阻塞事件循环会拖慢所有业务请求）。
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(probe_database, settings),
            timeout=settings.db_health_total_timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "database health probe exceeded total timeout",
            extra={"timeout_seconds": settings.db_health_total_timeout_seconds},
        )
        return error_response(
            code=ErrorCode.DEPENDENCY_UNAVAILABLE,
            details={
                "dependency": "postgresql",
                "reason": "total_timeout_exceeded",
                "timeoutSeconds": settings.db_health_total_timeout_seconds,
                "timeouts": timeouts,
            },
        )

    if not result.reachable:
        # error_kind 是异常类名（已在 probe_database 里做过收窄），
        # 不含异常正文、主机名或 DSN。details 还会再过一遍 sanitize_details。
        return error_response(
            code=ErrorCode.DEPENDENCY_UNAVAILABLE,
            details={
                "dependency": "postgresql",
                "reason": result.error_kind or "unknown",
                "latencyMs": result.latency_ms,
                "timeouts": timeouts,
            },
        )

    body = DependencyBody(
        status="ok",
        reachable=True,
        server_version=result.server_version,
        pgvector_version=result.pgvector_version,
        latency_ms=result.latency_ms,
        timeouts=timeouts,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=single_response(body.model_dump(by_alias=True, mode="json")),
    )