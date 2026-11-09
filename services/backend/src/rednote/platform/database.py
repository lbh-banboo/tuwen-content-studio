"""数据库连接与探针（C4：所有外部调用必须有显式超时）。

**为什么用 psycopg3 同步驱动而不是 asyncpg**：
研发计划 01 §4 锁定 SQLAlchemy 2 + psycopg 3。同步驱动 + FastAPI 的线程池
已能覆盖本项目 30 RPS 的负载，而异步 ORM 会引入 session 生命周期与事务边界的
额外复杂度 —— 本项目的核心难点是事务正确性（版本、配额、费用），不是并发吞吐，
所以选更可控的那条路。

**engine 按 DSN 缓存**：
测试会注入不同 DSN（含指向不可达地址的），若全局只存一个 engine，
第二个测试会复用第一个的连接池，测出来的是上一个测试的库。
按 DSN 做键缓存，`reset_engines()` 供测试清理。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from rednote.platform.configuration import Settings, get_settings

# 探针用的固定查询。写成常量而不是散在代码里，便于契约测试断言。
_PROBE_SERVER_VERSION_SQL: Final[str] = "select current_setting('server_version')"
_PROBE_PGVECTOR_SQL: Final[str] = (
    "select extversion from pg_extension where extname = 'vector'"
)

# engine 缓存：DSN -> Engine。测试通过 reset_engines() 清空。
_ENGINES: dict[str, Engine] = {}


def reset_engines() -> None:
    """清空 engine 缓存并释放连接池。仅测试与配置热加载使用。"""
    for engine in _ENGINES.values():
        engine.dispose()
    _ENGINES.clear()


def connection_facts(settings: Settings) -> dict[str, Any]:
    """回显**实际生效**的超时配置。

    为什么把它放进健康检查响应：超时是本切片的核心交付项之一，
    如果只在代码里配了、运行时看不到，就无法从外部验证"配置真的被注入了"。
    这些值不含任何凭据或网络位置，可以安全外泄。
    """
    return {
        "connectTimeoutSeconds": settings.db_connect_timeout_seconds,
        "statementTimeoutMs": settings.db_statement_timeout_ms,
        "poolTimeoutSeconds": settings.db_pool_timeout_seconds,
        "healthTotalTimeoutSeconds": settings.db_health_total_timeout_seconds,
        "driver": settings.db_driver,
        # 显式标志，让"配置确实生效"可被一眼断言，而不用逐字段比较。
        "configured": True,
    }


def _make_engine(settings: Settings) -> Engine:
    """按配置建 engine。三层超时在连接参数里一次性注入。"""
    if not settings.database_url:
        raise RuntimeError("database_url 未配置")

    if settings.database_url in _ENGINES:
        return _ENGINES[settings.database_url]

    engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # 池满时默认无限等待（None），是高负载下的隐性雪崩点，必须显式设值。
        pool_timeout=settings.db_pool_timeout_seconds,
        # 内网长连接常被防火墙/负载均衡静默断开，取连接前先探活，
        # 避免把"连接已被对端关闭"当成业务错误。
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": settings.db_connect_timeout_seconds,
            # statement_timeout 下发给服务端：只在客户端超时无法中断服务端执行，
            # 慢查询会继续占着 PG 的连接与 CPU。
            "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
        },
    )
    _ENGINES[settings.database_url] = engine
    return engine


@dataclass(frozen=True)
class ProbeResult:
    """探针结果。

    `error_kind` 只放异常**类名**，不含异常正文 —— C6：不泄露内部细节
    （异常正文常带 DSN、主机名、驱动内部路径）。
    """

    reachable: bool
    server_version: str | None = None
    pgvector_version: str | None = None
    latency_ms: int = 0
    error_kind: str | None = None


def probe_database(settings: Settings) -> ProbeResult:
    """真连数据库执行探针查询。

    **不吞异常，而是分类**：连接失败与查询失败都归为 reachable=False，
    但原因（异常类名）会带到响应里，便于排障时区分"网络不通"与"SQL 写错"。

    **pgvector 未安装不算失败**：返回 `pgvector_version=None` 但 `reachable=True`。
    理由见 test_health_db_reports_pgvector_version 的说明 ——
    把"扩展没装"做成 503 会让合法的非向量部署无法就绪。
    """
    started = time.monotonic()

    if not settings.database_url:
        return ProbeResult(
            reachable=False,
            latency_ms=0,
            error_kind="database_url_not_configured",
        )

    try:
        engine = _make_engine(settings)
        with engine.connect() as conn:
            server_version = conn.execute(text(_PROBE_SERVER_VERSION_SQL)).scalar_one()
            vector_version = conn.execute(text(_PROBE_PGVECTOR_SQL)).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 - 探针必须吞下一切异常并分类，否则健康检查自己会 500
        # 只保留类名。异常正文可能含 DSN、主机名、内部路径（C6）。
        return ProbeResult(
            reachable=False,
            latency_ms=int((time.monotonic() - started) * 1000),
            error_kind=type(exc).__name__,
        )

    return ProbeResult(
        reachable=True,
        server_version=str(server_version) if server_version is not None else None,
        pgvector_version=str(vector_version) if vector_version else None,
        latency_ms=int((time.monotonic() - started) * 1000),
    )


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """请求级事务上下文（WP08 起业务模块使用）。

    **为什么接受可选 settings 参数**：
    配置有两个来源 —— 进程级单例（Alambic/脚本用）与 app.state（Web 请求用）。
    若只读全局单例，就会出现"app 用注入配置装配、session 却按环境变量连库"
    的两套真相。本机实测踩到过：测试注入 `Settings(database_url=...)`，
    但 session_scope 读全局单例拿到 None，报"database_url 未配置"。
    显式传入 settings 让调用方能锁定自己那一份配置。

    不传 settings 时退回全局单例，供无 app 上下文的脚本（迁移、运维工具）使用。

    **为什么是上下文管理器而不是 sessionmaker 直接给业务用**：
    本项目的写操作（阶段推进、配额预占、费用结算）都要求"要么全成、要么全不成"。
    若业务代码自己 `session.commit()`，事务边界会散落在各模块，
    无法推理"这个操作到底在哪个事务里"。这里强制调用方声明边界。

    **为什么 commit 放在正常路径、rollback 放在异常路径**：
    这是唯一能保证"异常时不留半成品写入"的写法。
    注意异常时会 rollback 后**重新抛出** —— 吞掉异常会让上层以为操作成功了。

    **关于超时**：单条语句的超时由 engine 的 `statement_timeout` 统一注入
    （见 `_make_engine`），这里不重复配置，避免两处设置打架。
    """
    resolved = settings or get_settings()
    engine = _make_engine(resolved) if resolved.database_url else _raise_no_dsn()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _raise_no_dsn() -> Engine:
    """未配置 DSN 时明确失败。

    让请求报 503 而不是静默返回空数据 ——"没配数据库"必须被看到。
    """
    raise RuntimeError("database_url 未配置，无法建立数据库会话。")


def get_engine(settings: Settings) -> Engine:
    """取（或首次创建）按配置缓存到 engine。

    **为什么需要它**：本模块此前只对外提供 `session_scope`（一个 Session 上下文）。
    但 WP14 的 `LISTEN/NOTIFY` 订阅器需要的是 **Engine 本身** ——
    它要开一条**独立于业务连接池**的长连接（ADR-14 明确要求），
    而 `session_scope` 给不了这个。

    公开它而不是让调用方去碰 `_make_engine`：后者是私有实现，
    而"engine 按 DSN 哈希缓存、同一 DSN 复用同一个池"这件事**是必须被遵守的约定**
    （否则每次调用都新建池，连接数会失控）。把约定放在一个公开函数里，
    调用方就不必知道缓存机制。
    """
    return _make_engine(settings)


__all__ = [
    "ProbeResult",
    "connection_facts",
    "get_engine",
    "probe_database",
    "reset_engines",
    "session_scope",
]