"""FastAPI app 工厂与请求中间件（协议 §1.1）。

**为什么用纯 ASGI 中间件而不是 `BaseHTTPMiddleware`：**

`requestId` 通过 ContextVar 传递给下游（成功响应的 `meta` 由各 handler 用
`build_meta()` 读取该 ContextVar 拼出）。而 `BaseHTTPMiddleware` 会在独立
的任务里运行下游 app，ContextVar 的写入时机与可见性依赖 Starlette 版本实现
细节 —— 拿一个契约字段的正确性去赌框架内部实现，不值得。纯 ASGI 中间件与
下游 app 在同一个上下文里顺序执行，ContextVar 的可见性是确定的。

顺带也避免了 `BaseHTTPMiddleware` 在流式响应下的额外缓冲层（协议 §11 的
SSE 端点对缓冲敏感）。

**requestId 的取舍：**

协议 §1.1 原文是"可由客户端提供 UUID；服务端验证或生成，用于追踪"。
本实现：客户端给了合法 UUID 就原样透传（保持客户端侧的追踪链路完整）；
给了非法值就**生成新的**，而不是返回 400。理由是这是追踪字段，不是安全
字段 —— 为一个格式错误拒绝整个业务请求，会让一个"可观测性辅助字段"变成
可用性风险。代价是客户端的关联 ID 会断，所以这种情况记 DEBUG 日志。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rednote.platform.configuration import Settings, get_settings
from rednote.platform.errors import (
    REQUEST_ID_HEADER,
    get_request_id,
    register_exception_handlers,
    reset_request_id,
    set_request_id,
)
from rednote.modules.devtools.api import DEV_LOGIN_ENVIRONMENTS
from rednote.modules.events.notifier import SubscriberRegistry
from rednote.platform.observability import (
    METRICS_PATH,
    configure_logging,
    ensure_metrics_installed,
    metrics_response,
)
from rednote.platform.request_limits import RequestBodyLimitMiddleware

logger = logging.getLogger(__name__)

# 协议 §1："API 文档仅内部可访问，不携带生产数据示例或密钥。"
# 因此非 prod 环境才挂载 /docs 与 /openapi.json 的交互式入口；
# prod 下由反向代理在运维面单独暴露（ADR-11 C8）。
_DOCS_ENABLED_ENVIRONMENTS: Final[frozenset[str]] = frozenset(
    {"dev", "test", "staging"}
)


def _is_valid_uuid(value: str) -> bool:
    """严格 UUID 校验。

    用 `uuid.UUID(value)` 而非正则：它会拒绝长度不符、含非法分组的值，
    同时接受带/不带连字符两种合法写法。只接受 ASCII 可打印短串，
    避免把超长或含换行的输入写进日志与响应头（header 注入面）。
    """
    if not value or len(value) > 64:
        return False
    if not value.isascii() or not value.isprintable():
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


class RequestIdMiddleware:
    """生成或透传 X-Request-Id，并写入响应头。

    只做三件事：确定 requestId、写 ContextVar、把值回写到响应头。
    错误/成功响应体里的 `meta.requestId` 由 handler 读 ContextVar 拼出 ——
    这样"响应体里的 requestId"只有一处真相（ContextVar），不会出现
    中间件写头、handler 写体、两者不一致的情况。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # lifespan / websocket 等非 HTTP scope 直接放行：WebSocket 不走
        # HTTP 头语义，lifespan 没有请求上下文。
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._resolve_request_id(scope)
        token = set_request_id(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                header_name = REQUEST_ID_HEADER.lower().encode("ascii")
                # 先移除同名头，再追加，避免上游中间件已写过导致出现两个值。
                headers = [
                    (name, value)
                    for name, value in headers
                    if name.lower() != header_name
                ]
                headers.append((header_name, request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # 复位 ContextVar。同一 asyncio 任务可能被复用来处理后续请求，
            # 不复位会让"上一个请求的 requestId"泄漏给下一个请求，
            # 表现为日志串号、排障时定位到错误的请求。
            reset_request_id(token)

    @staticmethod
    def _resolve_request_id(scope: Scope) -> str:
        raw: str | None = None
        for name, value in scope.get("headers") or []:
            if name.lower() == REQUEST_ID_HEADER.lower().encode("ascii"):
                try:
                    raw = value.decode("ascii")
                except UnicodeDecodeError:
                    raw = None
                break

        if raw is not None and _is_valid_uuid(raw):
            return raw

        generated = str(uuid.uuid4())
        if raw is not None:
            # 只记长度，不记原值 —— 原值是外部输入，可能含控制字符或
            # 试图注入日志的内容（C6：日志不打印完整敏感内容）。
            logger.debug(
                "invalid %s from client, generated a new one",
                REQUEST_ID_HEADER,
                extra={"invalid_header_length": len(raw), "request_id": generated},
            )
        return generated


def _operation_id(route: APIRoute) -> str:
    """确定性 operationId，例如 `get_api_v1_health_db`。

    为什么不用 FastAPI 默认的 `<函数名>_<路径>_<方法>`：
    默认实现把 Python 函数名编进契约。重命名一个函数（纯重构，对外行为不变）
    会在 CI 的契约 diff 里表现为操作 ID 变更，被误判成破坏性变更（协议 §13.2）。
    operationId 只应依赖 HTTP 契约本身 —— 方法与路径。

    同一路径上有多个方法时（例如未来的 GET+POST），方法名参与组成，
    保证 operationId 全局唯一 —— OpenAPI 要求这一点。
    """
    methods = sorted(route.methods or [])
    verb = methods[0].lower() if methods else "any"
    path = route.path_format.strip("/").replace("{", "").replace("}", "")
    path = path.replace("/", "_")
    return f"{verb}_{path}"


def create_app(settings: Settings | None = None) -> FastAPI:
    """app 工厂。

    用工厂而不是模块级 `app = FastAPI()`：测试需要注入不同配置（例如
    未配置 database_url 的场景）并拿到干净实例，模块级单例做不到这一点，
    只能靠 monkeypatch 全局状态。
    """
    resolved = settings or get_settings()

    # 集中日志配置（WP47 ①）。放在最前面 —— 后面每一步（含 `include_router`、
    # 静态目录校验失败抛 RuntimeError）都可能写日志，配置晚于它们会丢掉早期日志。
    #
    # **幂等**：`create_app()` 被测试夹具反复调用，配置函数自身保证不重复装
    # handler、不重复包装 record factory（见 observability/logging_config.py）。
    #
    # **返回值这里刻意不接收**：调用方（tests / uvicorn）不关心装没装。函数内部
    # 返回值是给**测试**断言用的（纪律第 5 条："没跑"与"跑失败"同形）。
    configure_logging(resolved)

    app = FastAPI(
        title="Rednote 内容生产系统 API",
        version=resolved.app_version,
        description=(
            "M1 WP06/WP07 骨架竖切片。当前只含健康检查；"
            "业务接口按工作包逐个加入，契约由 tools/contracts/export_openapi.py 导出。"
        ),
        openapi_version="3.1.0",
        docs_url="/docs" if resolved.environment in _DOCS_ENABLED_ENVIRONMENTS else None,
        redoc_url=None,
        openapi_url=(
            "/openapi.json"
            if resolved.environment in _DOCS_ENABLED_ENVIRONMENTS
            else None
        ),
        generate_unique_id_function=_operation_id,
    )

    # 中间件顺序有讲究：Starlette 的 `add_middleware` 是 **后加的在外层**。
    # 因此先加请求体上限、后加 requestId —— 这样 requestId 先生成，
    # 上限中间件拒绝请求时，错误体里的 `meta.requestId` 才是真实值
    # 而不是占位的 "unknown"（否则排障时无法把这次拒绝关联到具体请求）。
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)

    # 把生效配置挂到 app.state。这是 handler 与 app 装配共享同一份配置的
    # 唯一通道 —— 见 configuration.get_app_settings 的说明。
    app.state.settings = resolved

    from rednote.modules.audit import router as audit_router
    from rednote.modules.baseline import router as baseline_router
    from rednote.modules.billing import router as billing_router
    from rednote.modules.brief import router as brief_router
    from rednote.modules.content import router as content_router
    from rednote.modules.delivery import router as delivery_router
    from rednote.modules.batches import router as batches_router
    from rednote.modules.planning.trial import router as trial_router
    from rednote.modules.versions.impact import router as impact_router
    from rednote.modules.quality import router as quality_router
    from rednote.modules.events import router as events_router
    from rednote.modules.exports import router as exports_router
    from rednote.modules.feedback import router as feedback_router
    from rednote.modules.files import router as files_router
    from rednote.modules.health import router as health_router
    from rednote.modules.health.api import get_health, get_health_db
    from rednote.modules.identity import router as identity_router
    from rednote.modules.knowledge import router as knowledge_router
    from rednote.modules.knowledge import search_router as knowledge_search_router
    from rednote.modules.media import router as media_router
    from rednote.modules.parsing import router as parsing_router
    from rednote.modules.planning import router as planning_router
    from rednote.modules.projects import dictionaries_router, router as projects_router
    from rednote.modules.prompts import router as prompts_router
    from rednote.modules.research import router as research_router
    from rednote.modules.review import router as review_router

    app.include_router(health_router, prefix=resolved.api_prefix)
    app.include_router(identity_router, prefix=resolved.api_prefix)
    app.include_router(projects_router, prefix=resolved.api_prefix)
    # 字典是共享主数据，路径不带项目前缀，因此单独注册一个 router。
    app.include_router(dictionaries_router, prefix=resolved.api_prefix)
    # WP12 文件上传：三组路径（/projects/{projectId}/uploads、/uploads/*、/files/*），
    # 与项目路由路径不重叠，因此单独一个 router。
    app.include_router(files_router, prefix=resolved.api_prefix)
    # WP14 SSE：/events（协议 §11）。SSE 是长连接，不是普通请求 ——
    # 因此这个 router 只含一个端点，且单独注册便于将来对它做超时豁免。
    app.include_router(events_router, prefix=resolved.api_prefix)
    # WP15 审计与待办：/work-items、/work-items/{id}/resolutions、/audit-events
    # （协议 §10）。三组路径与既有路由不重叠。
    app.include_router(audit_router, prefix=resolved.api_prefix)
    # WP17 解析：/files/{fileId}/parse-jobs（协议 §4.2 第 7 行）。
    # 与 WP12 的 /files/{fileId} 是**不同方法 + 不同路径段**，不冲突：
    # 本 router 的路径是 /files/{fileId}/parse-jobs，WP12 的是 /files/{fileId}。
    app.include_router(parsing_router, prefix=resolved.api_prefix)
    # WP18 Brief 结构化解构：/projects/{projectId}/brief*（协议 §5）。
    # 路径与既有路由不重叠（projects 路由没有 /brief 段）。
    app.include_router(brief_router, prefix=resolved.api_prefix)

    # WP20 知识 CRUD 与版本：/knowledge/assets*、/knowledge/proposals*
    # （协议 §9 的 10 行中的 9 行。第 8 行 `POST /knowledge/searches` 留给
    # WP21 —— 语义检索需要 embedding，本包只提供精确读取路径）。
    # 路径与既有路由不重叠（没有别的模块用 /knowledge 段）。
    app.include_router(knowledge_router, prefix=resolved.api_prefix)

    # WP21 切块 + embedding + 检索：`POST /knowledge/searches`
    # （协议 §9 的 10 行中的第 8 行，WP20 刻意留给本包的那一条）。
    #
    # **为什么不并入 knowledge_router**：WP20 的 router 文件是本包的
    # "别人家的文件"（接缝冻结文档：分支只写自己模块内的新文件）。
    # 单独一个 router 模块让"检索端点是谁加的"从代码结构上就看得见，
    # 也让将来检索要单独做超时/限流/观测时不必动 WP20 的文件。
    #
    # 路径与 WP20 的 `/knowledge/assets*`、`/knowledge/proposals*` 不重叠。
    app.include_router(knowledge_search_router, prefix=resolved.api_prefix)

    # WP19 研究关键词与提示词、资料列表、覆盖缺口确认：
    # /projects/{projectId}/research-prompts 与 /research-files
    # （协议 §5 的第 6、7 行）。**两个都是 GET，本包无写端点** ——
    # 上传走 WP12、解析走 WP17（协议里没有研究专用的写端点）。
    # 路径与既有路由不重叠（projects 路由没有 /research-* 段）。
    #
    # **本包零迁移、零表**：它读的全是别人的表（Brief / 文件 / 解析 / 配额），
    # 因此不需要在 `migrations/env.py` 里加 import（那里是给 autogenerate
    # 发现新表用的，本包没有新表可发现）。
    app.include_router(research_router, prefix=resolved.api_prefix)

    # WP23 Prompt 版本与工具权限：/prompts*（协议 §9 的第 11~13 行）
    # + /knowledge/proposals 的**产生方**（POST，闭合 WP20 的 OP-150 一半）。
    #
    # 与 WP20 的 `/knowledge/proposals`（GET）是**同路径不同方法**：
    # FastAPI 按 `(path, method)` 匹配，两个 router 的注册顺序不影响结果。
    # 本 router 只注册 `POST /knowledge/proposals`，不碰 GET 那一条。
    #
    # 三个协议端点 + 三个本包补充端点（建模板 / 平台侧授予工具 / 产生候选）——
    # 补充的三个各自对应协议里的一处空缺，见 `prompts/api.py` 的 docstring。
    app.include_router(prompts_router, prefix=resolved.api_prefix)

    # WP22 信息审核与生产基线：/projects/{projectId}/information-draft、
    # /baselines*、/step-approvals*（协议 §6 的第 1、2 行 + 本包补充的 5 条）。
    #
    # 路径前缀 `/projects/{projectId}/...` 与 projects_router 同段，但
    # **没有一条子路径与它重叠**（projects 路由只有 /projects、
    # /projects/{projectId}、/projects/{projectId}/dictionaries 等，没有
    # /baselines、/information-draft、/step-approvals 段）——
    # FastAPI 按 `(path, method)` 匹配，同段不同尾不影响结果。
    # 与 brief_router 的 `/projects/{projectId}/brief*` 同理并立。
    #
    # **本包是 `ENTITY_TYPE_BASELINE` 的第一个生产者**：4 张新表 +
    # 挂在共用 `version_nodes` 上的一个反向一致性触发器，因此
    # `migrations/env.py` 必须同步加 import（已加）。
    #
    # 挂载顺序放在最后：本包不覆盖任何既有端点的行为，
    # 放在末尾可保证既有路由的解析优先级完全不变。
    app.include_router(baseline_router, prefix=resolved.api_prefix)

    # WP24 场景形式草稿与数量配置：/projects/{projectId}/plan-draft*（3 个）
    # 与 /settings/**（5 个）。
    #
    # 路径前缀 `/projects/{projectId}/plan-draft` 与 projects_router 同段，
    # 但**没有一条子路径与既有路由重叠**（projects 路由没有 /plan-draft 段，
    # brief 有 /brief*、baseline 有 /baselines*、/information-draft、
    # /step-approvals——都不叫 plan-draft）——FastAPI 按 `(path, method)`
    # 匹配，同段不同尾不影响结果。
    #
    # `/settings/**` 是**全仓第一个非 /projects 前缀的业务路由段**。
    # 它不与任何既有段冲突（health/identity/dictionaries/files/events/audit/
    # parsing/knowledge/research/prompts 都不含 settings 二字），但这一条
    # 值得写下来：将来若有第二个"平台级配置"模块（如 WP36~45 的数据与管理），
    # 应当**复用 `/settings` 前缀而不是另起一段**，否则同一个"配置"概念
    # 会散在多个前缀下，而前端要维护多套 base path（已登记收口事项）。
    #
    # **本包是 `ENTITY_TYPE_PLAN` 的第一个生产者**：2 张新表 +
    # `runtime_settings` 上的一张不可变触发器（复用共用函数
    # `rednote_reject_mutation`），因此 `migrations/env.py` 必须同步加
    # import（已加）。`runtime_settings` 也是**本仓第二张不可变表**
    # （第一张是 WP11 的 `version_nodes`）。
    #
    # 挂载顺序放在最后：本包不覆盖任何既有端点的行为，
    # 放在末尾可保证既有路由的解析优先级完全不变。
    app.include_router(planning_router, prefix=resolved.api_prefix)

    # WP25 供应商计价 / 预算并发预占 / 费用账本：14 个端点
    # （/projects/{projectId}/budget*、/budget-reservations/*、
    #   /projects/{projectId}/ledger-*、/projects/{projectId}/cost-estimates*、
    #   /cost-estimates/{estimateId}、/pricing-versions、/billing/reconciliations）。
    #
    # **协议 §1.4 第 2 条实测：55 paths 里没有任何 cost / budget / ledger /
    # pricing / usage 前缀的路径** —— 即"费用"这一整块在协议里没有一条
    # 端点定义。因此这 14 条的**形状全部由本包定义**（路径命名沿用
    # 其余模块的既有惯例：项目级子资源放 /projects/{projectId}/ 下，
    # 跨项目的共享资源放顶层段），并已登记为**协议侧待回填项**。
    #
    # 路径段与既有路由**逐条对过**，无重叠：
    #   · /projects/{projectId}/budget*      —— projects 无 /budget 段
    #   · /projects/{projectId}/ledger-*     —— 无 /ledger 段
    #   · /projects/{projectId}/cost-estimates —— 无 /cost-estimates 段
    #   · /budget-reservations/{id}/…        —— **顶层段**，全仓首次出现
    #   · /cost-estimates/{id}               —— 顶层段，全仓首次出现
    #   · /pricing-versions                  —— 顶层段，全仓首次出现
    #   · /billing/reconciliations           —— 顶层段，全仓首次出现
    # FastAPI 按 (path, method) 匹配，同段不同尾不影响结果。
    #
    # **为什么子资源端点（/budget-reservations/{id}/…、/cost-estimates/{id}）
    # 不放在 /projects/{projectId}/ 下**：预占与预估的 id 本身就是全局唯一
    # 的 UUID，调用方（worker、结算路径）手上只有它、不一定知道 projectId。
    # 强行要求 projectId 会逼调用方先反查一次 —— 而那次反查在并发下
    # 可能读到与 id 不属于同一项目的 projectId（本包 `api.py` 的
    # `_reservation_project_id` 就是干这件事的，鉴权分两步且顺序不可换）。
    #
    # **本包是 `OrgAction.MANAGE_BUDGET` 的第一个消费者**
    # （`permissions.py:157/166`：ADMIN=True / OPERATOR=False），
    # 也是 `ProjectAction.VIEW_COST` 的第一个消费者。
    #
    # **本包是 5 张新表 + 2 个不可变触发器（pricing_versions / usage_ledger）
    # + 1 条部分唯一索引的建立者**，因此 `migrations/env.py` 必须同步加
    # import（已加）。`usage_ledger` 与 `pricing_versions` 是继
    # `version_nodes`(WP11) / `gateway_egress`（WP16）/ `audit_events`(WP15) /
    # `runtime_settings`(WP24) 之后的**不可变表** —— 复用同一套
    # `rednote_reject_mutation()` 机制，不另造一套。
    #
    # 挂载顺序放在最后：本包不覆盖任何既有端点的行为，
    # 放在末尾可保证既有路由的解析优先级完全不变。
    app.include_router(billing_router, prefix=resolved.api_prefix)

    # ------------------------------------------------------------------
    # WP26 内容生成与标题候选（6 端点，路径空间**跨两类**）
    #
    # 与前面所有包的形态差别在于：本包的路径**一半带项目前缀、一半不带**
    # （协议 §1.7 六端点表）：
    #
    #   · `/projects/{projectId}/content-generations`  —— 带项目段
    #   · `/projects/{projectId}/contents`             —— 带项目段
    #   · `/contents/{contentId}`                      —— 顶层段
    #   · `/contents/{contentId}/versions`             —— 顶层段
    #   · `/contents/{contentId}/regenerations`        —— 顶层段
    #   · `/contents/{contentId}/title-adoptions`      —— 顶层段
    #
    # **`/contents` 是新出现的顶层段** —— 全仓检索确认此前无任何包占用
    # （`/baselines` 是 WP22、`/cost-estimates` 是 WP25，均不同）。
    # 因此本 router 只有一个，不需要拆两个（不像 projects 与 dictionaries
    # 那样因"字典不带项目前缀"而拆）。
    #
    # **路径参数名是 `contentId` / `projectId`（camelCase）**：与协议 §1.7
    # 逐字一致，由 `Path(alias=...)` 实现。⚠️ 与 `/files/{file_id}`、
    # `/uploads/{upload_id}` 的 snake_case 口径**不同** —— 那是 WP12 的
    # 历史选择，本包不跟随也不改动它（改它会让既有契约静默变化）。
    #
    # **`migrations/env.py` 已 import content 模型**（本包是 5 张新表
    # + 7 个触发器 + 17 条索引的建立者，`1c1c814b248e`）。
    # 其中 4 张表是继 `version_nodes`(WP11) / `gateway_egress`(WP16) /
    # `audit_events`(WP15) / `runtime_settings`(WP24) / `pricing_versions` /
    # `usage_ledger`(WP25) 之后的**不可变表** —— 复用同一套
    # `rednote_reject_mutation()` 机制，不另造一套。
    #
    # 挂载顺序放在 billing 之后（即既有路由之后）：本包不覆盖任何既有
    # 端点的行为，放在末尾保证既有路由的解析优先级完全不变。
    #
    # ⚠️ **`/contents/{contentId}` 与既有路径的潜在歧义**：全仓现有顶层段
    # 里没有任何一个以 `/contents` 开头的路径，因此不存在"同名参数吃掉
    # 既有路由"的风险。但若将来有包新增 `/contents/xxx` 形式的**字面量**
    # 段，Starlette 按**声明顺序**匹配，字面量段会排在 `{contentId}` 之前
    # —— 那时的正确做法是**把字面量路由的包排在本包之前**，而不是改本包
    # 的路径参数名（后者会破坏契约）。
    # ------------------------------------------------------------------
    app.include_router(content_router, prefix=resolved.api_prefix)

    # ------------------------------------------------------------------
    # WP27 静态图适配与产品参考素材（7 端点，路径空间**跨三类**）
    #
    # 与既有各包的形态差别：本包的路径**三段分散**（说明文档 §1.7 七端点表）：
    #
    #   · `/projects/{projectId}/reference-assets`      —— 带项目段
    #   · `/contents/{contentId}/media-generations`     —— 内容段（WP26 已占）
    #   · `/contents/{contentId}/media`                 —— 内容段
    #   · `/media/{mediaId}`                            —— **新顶层段**
    #   · `/media/{mediaId}/regenerations`              —— 新顶层段
    #   · `/media/{mediaId}/renditions`                 —— 新顶层段
    #
    # **`/media` 是新出现的顶层段**（实测：装配本包前全仓 96 条路由里
    # 含 `/media` 的**零条**）。`/reference-assets` 与 `/media-generations`
    # 亦均为新尾段（实测含它们的路由各零条）。因此本 router 只有一个，
    # 不需要像 projects/dictionaries 那样因"一半不带项目前缀"而拆。
    #
    # **`/contents/{contentId}/media*` 与 WP26 的同段并存**：WP26 已注册
    # `/contents/{contentId}`、`/contents/{contentId}/versions`、
    # `/contents/{contentId}/regenerations`、`/contents/{contentId}/title-adoptions`。
    # 本包新增的两条是 `/media-generations` 与 `/media`（不同尾段）。
    # ⚠️ 一条**需要写下来**的相邻关系：本包的 `/contents/{contentId}/media`
    # 与 WP26 的 `/contents/{contentId}`（GET）**不会**互相吃掉 ——
    # 前者多一个路径段，Starlette 按段匹配，不是前缀匹配。
    # 但若将来有包新增 `/contents/{contentId}/media/xxx` 形式的字面量段，
    # 按 WP26 那段注释的同一条处置（把字面量路由的包排在本包之前）。
    #
    # **`/media/{mediaId}/regenerations` 与 WP26 的
    # `/contents/{contentId}/regenerations`**：两条都叫 regenerations，
    # 但**不是同一个资源层级** —— 前者重做一张图，后者重做一条内容的某一段。
    # 路径段不同（`/media/` vs `/contents/`），FastAPI 按 (path, method)
    # 匹配，不冲突。刻意不统一成一条路径：两者的请求体、幂等键、
    # 入队 kind 都不同（详见 `media/api.py` 第八节的那张对比表）。
    #
    # **`migrations/env.py` 已 import media 模型**（断点前已就位；本包是
    # 5 张新表 + 若干触发器 + 索引的建立者，见 `df6bb2f41f84` /
    # `b7c1e4a9d203` / `c4d9f2a17e35`）。其中 4 张是继 `version_nodes`(WP11) /
    # `gateway_egress`(WP16) / `audit_events`(WP15) / `runtime_settings`(WP24) /
    # `pricing_versions` / `usage_ledger`(WP25) / content 的 4 张(WP26)
    # 之后的**不可变表** —— 复用同一套 `rednote_reject_mutation()` 机制。
    #
    # ⚠️ **本包的任务处理器（`build_task_registry`）刻意不在这里合并**：
    # 全仓实测，`run_job` / `claim_jobs` / `build_default_registry`
    # **只在测试里被消费**，生产侧**没有 worker 入口**（这是 WP13 的
    # 既有状态，不是本包引入的）。因此在这里构造一个注册表并挂到
    # `app.state` 会是一个**零消费者的悬空符号** —— 那正是本仓多次记录的
    # ISS-95 形态（"声称有防线、入口打不开"）。本包只交付
    # `media/handlers.build_task_registry()` 这个**可被装配的入口**，
    # 由将来的 worker 装配点与 `build_default_registry()` 合并
    # （合并口径已在 `media/handlers.py` 的 docstring 写明）。
    #
    # 挂载顺序放在 content 之后（即既有路由之后）：本包不覆盖任何既有
    # 端点的行为，放在末尾保证既有路由的解析优先级完全不变。
    # ------------------------------------------------------------------
    app.include_router(media_router, prefix=resolved.api_prefix)

    # WP35 固定版本 ZIP 导出：4 条路径，全部在 `/projects/{projectId}/exports` 下。
    # 实测装配前全仓含 `/exports` 的路由为零条，与既有路径不重叠；
    # 放在 media 之后（末尾），不改变既有路由的解析优先级。
    app.include_router(exports_router, prefix=resolved.api_prefix)

    # WP29 预览/编辑/反馈定位：7 条路径，含 `/contents/{contentId}/preview`
    # 等新端点与 `/feedback/{feedbackId}`。实测装配前全仓不含这些路径，
    # 放在 exports 之后（末尾），不改变既有路由的解析优先级。
    app.include_router(feedback_router, prefix=resolved.api_prefix)

    # WP30 AI 自检/硬软规则：6 条路径，含 `/contents/{contentId}/self-checks`
    # 等新端点与顶层 `/self-check-rules`/`/self-checks/{runId}`。实测装配前
    # 全仓不含 `self-check` 字样的路径，放在 feedback 之后（末尾），
    # 不改变既有路由的解析优先级。
    app.include_router(review_router, prefix=resolved.api_prefix)

    # WP36 KOC 记录/交付与替换/发布登记：11 条路径，含
    # `/projects/{projectId}/{koc-records,deliveries,publications}`、
    # `/deliveries/{deliveryId}/{confirmations,cancellations,replacements}`、
    # `/projects/{projectId}/unmarked-exports`。实测装配前全仓不含这些路径，
    # 放在 review 之后（末尾），不改变既有路由的解析优先级。
    app.include_router(delivery_router, prefix=resolved.api_prefix)

    # WP31 试产关口 / 淘汰补产：9 个端点，含 `/projects/{projectId}/trial-rounds` 与
    # 关口批准 / 补产。放在 delivery 之后（末尾），不改变既有路由的解析优先级。
    app.include_router(trial_router, prefix=resolved.api_prefix)

    # WP32 扩产 / 批产：批次创建（Idempotency-Key）、详情、取消、重做、预览。
    # 创建前经 `trial_bridge` 读 WP31 的关口结论（fail-closed 的默认实现已替换）。
    app.include_router(batches_router, prefix=resolved.api_prefix)

    # WP34 回退影响图 / 导出失效：5 个端点（影响图只读预览、导出失效扫描与查询）。
    # 放在 batches 之后（末尾），不改变既有路由的解析优先级。
    app.include_router(impact_router, prefix=resolved.api_prefix)

    # WP33 重复检查 / 质量报告：7 个端点（报告生成与读取、维度清单、导出）。
    # 放在 impact 之后（末尾），不改变既有路由的解析优先级。
    app.include_router(quality_router, prefix=resolved.api_prefix)

    # WP14 的跨实例分发器与进程内订阅表挂在 app.state 上
    # （不是全局单例 —— 理由见 events/api.py 的 get_notifier）。
    #
    # **不在这里 start()** —— 建立 LISTEN 连接需要一个可用的 Engine，
    # 而 Engine 是懒建的（`database.get_engine` 在首次连接时才建）。
    # 在工厂里强行连库会让"未配置数据库的 app"直接崩掉
    # （`create_app(Settings(database_url=None))` 是合法用法，
    # conftest 与多处测试都依赖它）。因此订阅器**只装配、不连接**，
    # 由 lifespan/部署层决定何时 start。
    app.state.event_registry = SubscriberRegistry()
    app.state.event_notifier = None

    @app.get("/", include_in_schema=False)
    async def _root() -> dict[str, str]:
        """非契约入口。只为人工排障时给出"服务在这、前缀是它"的提示，
        因此 `include_in_schema=False` —— 协议 §1 的路径空间里没有它。"""
        return {
            "service": resolved.app_name,
            "apiPrefix": resolved.api_prefix,
            "health": f"{resolved.api_prefix}/health",
        }

    # ------------------------------------------------------------------
    # WP47 ① 探针根路径别名 + 指标出口（ADR-11 C6「仅运维面」）
    #
    # 三个路径**全部根路径、全部 `include_in_schema=False`**（决策 D3）：
    # 契约描述**产品接口面**，而这三个是运维面入口 —— 给前端生成一个
    # `metrics()` SDK 方法是荒谬的。先例：上面的 `/` 与
    # `/api/v1/dev/auth/login` 同样不入契约。因此契约仍为 71 paths。
    #
    # **为什么是「别名」而不是「改名」**（§1.5 砍掉项 C6）：
    # 既有的 `/api/v1/health` 与 `/api/v1/health/db` 已被 12 条测试、
    # `docs/m1` 文档、`contracts/openapi.json` 引用。改名 = 破坏性变更
    # （协议 §13.2）。根路径别名与它们**路径空间不重叠**，零破坏。
    #
    # **别名直接复用既有 handler 函数对象**（决策 D1）—— 这是本段唯一
    # 重要的技术选择，理由是一条**生产级风险**（R1）：
    #
    #   k8s / 编排平台的 liveness 探针**失败即重启实例**。若 `/healthz`
    #   的实现碰了数据库，那么 PG 抖动（连接池耗尽、慢查询）会让探针失败
    #   ⇒ 编排平台**重启全部实例** ⇒ 故障从"数据库不可用"升级为"应用全挂"
    #   （ADR-11 C8 是双实例，会全灭）。
    #
    # 复用 `get_health`（不碰库）而不是重写一遍，让这件事**结构上不可能出错**：
    # 没有第二份实现，就不存在"改了一处忘了另一处"。若写成 `_healthz()` 再
    # 复制 body，则"liveness 的实现被某个后来者顺手加上一次 DB ping"这个
    # 错误在 review 时**没有任何视觉信号**。
    #
    # handler 的依赖（`settings: SettingsDep`）由 FastAPI 在请求期注入，
    # 与是否复用无关 —— 见 `/api/v1/health` 的同一函数。
    # ------------------------------------------------------------------
    app.add_api_route(
        "/healthz",
        get_health,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/readyz",
        get_health_db,
        methods=["GET"],
        include_in_schema=False,
    )

    # `/metrics`：Prometheus 抓取端点（决策 D2 放在 platform 层）。
    #
    # **为什么用 `to_thread`**（风险 R9）：`generate_latest` 是同步 CPU 操作。
    # 当前数据量小，但它与 `/health/db` 的 `probe_database` 同属"同步阻塞
    # 调用放进 async 端点"，用同款手法保持一致；且 WP47 ② 加业务指标后
    # 数据量会显著增长，那时提前改比事后排查"抓指标时接口变慢"容易。
    #
    # **幂等注册必须在请求期之前**：`ensure_metrics_installed` 在请求处理里
    # 调用（而不是在装配期），是为了让**指标反映当前装配的 Settings** ——
    # 测试会用不同 environment 反复 `create_app()`，装配期写死会让
    # `/metrics` 报出与实际运行实例不符的 version。重复调用安全（决策 D5）。
    #
    # ⚠️ **本端点无内置访问控制**（风险 R2 / OP-C）：ADR-11 C6 把"仅运维面"
    # 定为**网关职责**。部署上必须由入口网关把它限制到运维网段。
    # 代码层刻意不加"看起来有鉴权"的半吊子实现 —— 那会造出假安全感
    # （ISS-95：声称有防线比没有防线更危险）。
    @app.get(METRICS_PATH, include_in_schema=False)
    async def _metrics() -> Response:
        """Prometheus exposition。不进契约（运维面入口）。"""
        registry = await asyncio.to_thread(ensure_metrics_installed, resolved)
        return await asyncio.to_thread(metrics_response, registry)

    # ------------------------------------------------------------------
    # 开发期登录旁路：**装配期**按环境决定是否注册（G1 演示）
    #
    # 关键在"装配期"三个字：非 dev/test 环境下 `include_router` 根本
    # 不会被调用，因此 `/api/v1/dev/auth/login` 在生产的路由表里**不存在**，
    # 返回 404。这比"端点存在但内部返回 403"强一个量级 ——
    # 后者仍可被探测到（403 等于承认端点存在），攻击面更大。
    #
    # 第二道门在 `devtools/api.py::_assert_dev_environment`（端点内部再判一次）。
    # 两道门是刻意的纵深防御：一次重构或一个 merge 冲突解决失误
    # 就可能让本 include 逃出 if，那时第二道是唯一拦截点。
    #
    # **挂载顺序放最后**：本 router 的路径是 `/dev/**`，
    # 与既有路径空间不重叠，放末尾保证既有路由的解析优先级完全不变。
    #
    # **本端点 `include_in_schema=False`，不进 `contracts/openapi.json`**：
    # 契约描述产品的接口面，而这是开发期脚手架，生产路由表里没有它。
    # 因此 dev 环境下 `app.openapi()` 是 49 paths、契约是 48 ——
    # 这个差值**是预期的**，不是漏导出。理由详见 `devtools/api.py`
    # 里 `dev_login` 装饰器上的注释。
    # ------------------------------------------------------------------
    if resolved.environment in DEV_LOGIN_ENVIRONMENTS:
        from rednote.modules.devtools import router as devtools_router

        app.include_router(devtools_router, prefix=resolved.api_prefix)
        logger.info(
            "已装配开发期登录旁路",
            extra={
                "environment": resolved.environment,
                "path": f"{resolved.api_prefix}/dev/auth/login",
            },
        )

    # ------------------------------------------------------------------
    # 前端静态资源（G1 演示）：**配置驱动，默认不挂载**
    #
    # 不设 `REDNOTE_FRONTEND_STATIC_DIR` 时本段完全不执行，
    # 路由表里不会多出任何条目（已实测：`env=dev` 下挂载前后
    # 既有端点、`GET /`、未注册 GET 路径的行为矩阵完全一致）。
    #
    # **为什么用 Mount 而不是通配路由**：Starlette 的 Mount 匹配优先级
    # 低于显式路由，因此 `/api/v1/**` 与上面那个 `/` 都不会被静态处理器
    # 抢走。若改用 `@app.get("/{path:path}")` 通配路由，它会吃掉
    # `/api/v1/...`（取决于注册顺序），是典型的"API 突然返回 HTML"故障。
    #
    # **注意 `/` 的行为**：`_root` 路由在本 Mount 之前定义，因此
    # 挂载静态目录后 `GET /` 仍返回那段 JSON 提示，而不是 `index.html`。
    # 页面入口是 `/index.html`。这是刻意保留的，不是遗漏 ——
    # 演示时从前端登录页 `/login.html` 进入。
    #
    # **挂载后唯一的行为变更（已实测，不要以为"零影响"）**：
    #   未注册路径 + 写方法（POST/PUT/DELETE）：
    #       不挂载 → 404 RESOURCE_NOT_FOUND
    #       已挂载 → 400 MALFORMED_REQUEST
    #   原因：Mount("/") 会兜住这些请求，StaticFiles 对非 GET 抛 405，
    #   而 `errors._CODE_BY_STATUS` 没有 405 条目，于是被折成 400。
    #   影响面：仅"打错路径且用了写方法"的场景，状态码同为 4xx、
    #   同为结构化错误体，不涉及任何既有端点的正常路径 ——
    #   但**确实不是零影响**，故在此显式记录，而非声称"逐字节一致"。
    #   不引入 405 映射来"修"它：那会改变未挂载时的既有契约行为，
    #   代价大于收益。
    # ------------------------------------------------------------------
    if resolved.frontend_static_dir:
        static_root = Path(resolved.frontend_static_dir).expanduser().resolve()
        if not static_root.is_dir():
            # 配了但目录不存在 → 明确失败，不静默跳过。
            # 静默跳过的表现是"前端页面 404 但 API 正常"，排查时会先怀疑
            # 前端代码，而真正的原因（路径写错）要兜一大圈才发现。
            raise RuntimeError(
                "REDNOTE_FRONTEND_STATIC_DIR 指向的目录不存在："
                f"{static_root}"
            )
        app.mount(
            "/",
            StaticFiles(directory=static_root, html=True),
            name="frontend",
        )
        logger.info("已挂载前端静态资源", extra={"directory": str(static_root)})

    return app


def get_request_id_for_logging() -> str:
    """供日志配置使用的 requestId 读取器。

    ⚠️ **这个函数在 WP47 ① 之前是「悬空」的**：它有 docstring、进了 `__all__`，
    却**零调用**，而它声称服务的"日志配置"当时**根本不存在**（本仓第一份集中
    日志配置是 WP47 ① 的 `platform/observability/logging_config.py`）。
    这是 ISS-95 同族缺陷的一个实例 —— "声称自己有防线"比"没有防线"更危险，
    因为读者会据此认为日志里已经有 requestId。

    现在它是一条**转发**：真正的实现在
    `platform/observability/logging_config.py`（那里被 `configure_logging`
    当作默认取值器**真的调用**）。

    **为什么不直接删掉它、只留 observability 里的那份**：
    它进过 `__all__`，是本模块的公开面；但更实在的理由是 —— 删掉会让
    "这个悬空符号曾经存在"从代码里彻底消失。保留 + 写明它是转发，
    让下一个人读到这里时能看见"曾经声称有防线、后来被接上"这段历史。
    删掉是隐藏问题，接上才是解决问题。
    """
    return get_request_id()


__all__: list[str] = ["RequestIdMiddleware", "create_app", "get_request_id_for_logging"]