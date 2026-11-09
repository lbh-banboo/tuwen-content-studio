"""应用配置（pydantic-settings）。

**凭据口径（C6）**：`database_url` 没有默认值，必须由环境变量注入。
未注入时服务照常启动（`/health` 可用），但 `/health/db` 返回 503 ——
"没配数据库"是一个必须被看到的事实，不应该让整个进程起不来，
也不应该被一个假的默认 DSN 掩盖成"连上了"。

**为什么 app 工厂与 handler 共享同一份 Settings**：
`create_app(settings)` 允许注入配置。如果 handler 里再调 `get_settings()`
（进程级 lru_cache 单例），就会出现两套真相 —— app 按注入值装配路由，
handler 按环境变量读超时。这个错误不会报错，只会让接口返回值不对，
极难发现（本机实测踩到过，见 docs/m1 说明 §7.2）。
因此 handler 一律从 `request.app.state.settings` 取配置。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 环境变量前缀。用前缀避免与容器/系统里已有的 DATABASE_URL 之类变量撞名，
# 也让"这个变量属于 rednote"一眼可辨。
_ENV_PREFIX = "REDNOTE_"


class Settings(BaseSettings):
    """运行配置。字段名自动映射为 `REDNOTE_<FIELD_NAME 大写>`。"""

    model_config = SettingsConfigDict(
        env_prefix=_ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = Field(default="rednote-backend")
    app_version: str = Field(default="0.1.0")
    environment: str = Field(default="dev")
    api_prefix: str = Field(default="/api/v1")
    log_level: str = Field(default="INFO")

    # ------------------------------------------------------------------
    # 数据库
    # ------------------------------------------------------------------
    # 无默认值：缺失即 None，由健康检查报 503。
    # 格式：postgresql+psycopg://<user>:<password>@<host>:<port>/<db>
    database_url: str | None = Field(default=None)

    # ------------------------------------------------------------------
    # 超时（C4：所有外部调用必须有显式超时数值，禁止 None）
    #
    # 分四层，每层解决不同的挂死场景，任何一层都不能省：
    #   1) connect_timeout      —— TCP 握手/认证阶段，防网络黑洞
    #   2) statement_timeout_ms —— 服务端语句执行，防慢查询
    #                              （只在客户端超时无法中断服务端执行）
    #   3) pool_timeout         —— 连接池取连接，池满时默认无限等待
    #   4) health_total_timeout —— Python 侧兜底，防驱动极端情况不响应
    # ------------------------------------------------------------------
    db_connect_timeout_seconds: int = Field(default=3, gt=0)
    db_statement_timeout_ms: int = Field(default=2000, gt=0)
    db_pool_size: int = Field(default=5, gt=0)
    db_max_overflow: int = Field(default=5, ge=0)
    db_pool_timeout_seconds: float = Field(default=5.0, gt=0)
    db_health_total_timeout_seconds: float = Field(default=3.0, gt=0)

    # 驱动标识。放进配置是为了让它出现在健康检查响应里 ——
    # 运维能一眼看出"这个实例用的是哪个驱动"，排障时少一次猜测。
    db_driver: str = Field(default="psycopg3")

    # ------------------------------------------------------------------
    # 会话与安全（WP08，研发计划 01 §10.1）
    # ------------------------------------------------------------------
    # 会话 Cookie 是否加 Secure 属性。
    # 生产必须为 true。开发环境默认 false 是因为本机是 http://127.0.0.1，
    # 浏览器不会发送 Secure Cookie，开发环境无法登录。
    # **这不是降低安全，而是避免开发者为了能登录而全局关掉 Secure。**
    session_cookie_secure: bool = Field(default=False)

    # OIDC 令牌加密密钥。
    # 必须是 urlsafe base64 编码的 32 字节（Fernet 格式）。
    # 无默认值：缺失时引用加密功能的接口会明确失败，而不是静默用弱密钥。
    # 生产由密钥管理系统注入（C6）。
    oidc_token_encryption_key: str | None = Field(default=None)

    # ------------------------------------------------------------------
    # OIDC / IdP 接入（WP08）
    # ------------------------------------------------------------------
    # issuer —— 令牌签发方标识。写入 users.issuer，用于身份唯一性判定。
    oidc_issuer: str = Field(default="http://localhost:8080/realms/rednote")
    oidc_client_id: str = Field(default="rednote-backend")
    # client_secret 只对 confidential client 需要；PKCE 公开客户端不需要。
    # 无默认值，缺失时若 IdP 要求则会明确报错。
    oidc_client_secret: str | None = Field(default=None)
    oidc_authorization_endpoint: str = Field(
        default="http://localhost:8080/realms/rednote/protocol/openid-connect/auth"
    )
    oidc_token_endpoint: str = Field(
        default="http://localhost:8080/realms/rednote/protocol/openid-connect/token"
    )
    oidc_userinfo_endpoint: str = Field(
        default="http://localhost:8080/realms/rednote/protocol/openid-connect/userinfo"
    )
    # 回调地址。必须与 IdP 侧注册的一致，否则 IdP 拒绝。
    oidc_redirect_uri: str = Field(default="http://127.0.0.1:8899/api/v1/auth/callback")

    # ------------------------------------------------------------------
    # 对象存储（WP12）
    # ------------------------------------------------------------------
    # 取值域 `LOCAL` / `MINIO`（与 `modules/files/models.py` 的
    # `STORAGE_BACKEND_*` 一致）。**不在这里 import 那些常量**：
    # `platform` 层不能依赖 `modules` 层 —— 依赖方向反了，
    # 而且 `configuration` 会被迁移脚本 import，应尽量轻。
    # 取值合法性由 `storage.build_storage_backend()` 在构造时校验并报可读错误。
    storage_backend: str = Field(default="LOCAL")

    # LOCAL 后端的根目录。**无默认值**：静默落到某个相对路径会让
    # "忘了配"表现为"文件存在但不知道在哪"，而对象存储的位置
    # 是运维必须显式知道的东西。
    storage_local_root: str | None = Field(default=None)

    # 登录放行后的前端落地页。仅允许站内路径（协议 §2：returnTo 仅本站路径）。
    frontend_landing_path: str = Field(default="/")

    # ------------------------------------------------------------------
    # 前端静态资源托管（G1 演示）
    # ------------------------------------------------------------------
    # 静态资源目录。**默认 None = 不挂载**。
    #
    # 为什么做成配置项而不是固定路径：
    #   1) CI 与生产环境的 `create_app()` 行为必须不变 ——
    #      默认 None 意味着不传这个变量时，路由表里不会多出任何条目，
    #      既有端点的匹配与响应完全不受影响（挂载/不挂载的行为矩阵
    #      已实测比对，唯一差异见 `main.py` 里 Mount 段落末尾的记录）。
    #   2) 前端资产（`outputs/rednote-demo/` 的 33 页）在**另一个目录树**，
    #      硬编码相对路径会让"换机器就崩"。
    #
    # **为什么前端必须由后端托管（而不是起一个 5173 开发服务器 + CORS）**：
    # `platform/csrf.py` 的 `verify_csrf` 要求 `Origin` 严格等于
    # 后端自身的 `scheme://netloc`。前端跑在另一个端口时，每个写请求
    # 都会被判为跨源而 403 `origin_mismatch`。
    # 要绕过只有两条路：削弱 CSRF 校验（不可接受）或**让两者同源**（本项）。
    frontend_static_dir: str | None = Field(default=None)

    # IdP HTTP 调用超时（C4）。OIDC 端点属于外部依赖，必须有显式超时。
    oidc_http_timeout_seconds: float = Field(default=10.0, gt=0)
    # 令牌交换的重试次数。OIDC 令牌交换**不可重试**（code 是一次性的，
    # 重试会因 code 已消费而失败），因此这里固定 0 次 ——
    # 显式写出来是为了让"为什么没有重试"成为一个被记录的决定。
    oidc_token_exchange_max_retries: int = Field(default=0, ge=0)

    # ------------------------------------------------------------------
    # 会话与 CSRF 时限
    # ------------------------------------------------------------------
    # 会话有效期。8 小时覆盖工作日。
    session_ttl_seconds: int = Field(default=8 * 60 * 60, gt=0)
    # 登录流程（state/nonce/PKCE）暂存时限。短 —— 授权跳转应在几分钟内完成。
    login_flow_ttl_seconds: int = Field(default=10 * 60, gt=0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级配置单例。

    仅用于**没有 app 上下文**的场景（例如 Alembic env.py、独立脚本）。
    请求处理路径上一律用 `request.app.state.settings`，理由见模块 docstring。
    """
    return Settings()


# 别名。历史原因：平台层内部一度叫 AppSettings，业务模块沿用至今。
# 保留一个名字而不是全仓库改名，避免引入一次纯重命名的大 diff ——
# 那正是 export_openapi 的 operationId 设计要避免的同类噪声。
AppSettings = Settings


def get_app_settings(app: object) -> Settings:
    """从 app.state 读生效配置。

    单独抽一个函数是为了让"配置只有一处真相"这条规则可被集中引用与测试。
    """
    settings = getattr(getattr(app, "state", None), "settings", None)
    if not isinstance(settings, Settings):
        # 早失败：装配漏了 app.state.settings 就应该立刻炸，
        # 而不是静默退回环境变量、埋下两套真相的隐患。
        raise RuntimeError(
            "app.state.settings 未设置。请通过 create_app(settings) 装配应用。"
        )
    return settings


def app_settings_dependency(request: Request) -> Settings:
    """FastAPI 依赖：从当前请求的 app.state 取配置。

    必须走依赖而不是把 `Settings` 直接写成端点参数类型 ——
    后者会让 FastAPI 把它当成一个请求体/查询参数模型去解析，
    结果是端点返回 422 而不是 200（本机实测踩到）。

    加 `Depends` 后 FastAPI 才会调用本函数、把返回值注入端点。

    用 Request 而非 app 单例：`app.state` 在 app 工厂里被赋值，
    所以依赖必须从请求上取到当前 app，才能拿到注入的那份配置。
    """
    return get_app_settings(request.app)


# 端点签名里用的依赖别名。写成 Annotated 别名是为了让端点声明保持简洁，
# 且"配置从 app.state 来"这一约定只有一个落点。
SettingsDep = Annotated[Settings, Depends(app_settings_dependency)]