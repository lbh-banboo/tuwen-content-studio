"""请求体大小上限（协议 §1 通用约定的落地位置）。

协议 §1 原文：

> 编码：UTF-8，JSON 请求／响应。**普通请求体上限默认 2 MB，文件走分片协议，
> 不能用 Base64 塞进 JSON。**

这句话在 M1 骨架期一直没落地（`docs/m1/M1后端骨架说明.md` §10-9 记为
"骨架期无接收请求体的端点，故未加；WP12 文件上传时须落地"）。
本模块就是那个落地位置，也是待定项 OP-67 的闭合点。

**"普通请求体"这个词是全部关键**。若把 2 MB 当成全局上限，分片端点
（`PUT /uploads/{id}/parts/{n}`，单片默认 8 MiB）会被拦死，上传功能
100% 不可用 —— 而错误信息只会说"请求体过大"，排障的人要花很久才能
把"8 MiB 的分片"与"2 MB 的全局上限"联系起来。

因此实现写成：**默认 2 MB，路径匹配分片端点时整段豁免**。

**已知局限（显式记录，不假装没有）**：本中间件只读 `Content-Length` 头。

  · 有该头（浏览器与所有主流 HTTP 客户端在发送已知长度 body 时都会带）
    → 超限立即 413，且**不读 body**，代价为零；
  · 缺该头（手工构造的 chunked 请求）→ 本层放行，由端点自身的流式
    计数兜底（分片端点必须实现早停，见 `files/api.py` 的 `_spool_body`）。

没有把"包装 receive 逐块计数"做进中间件，是因为那种实现一旦中途判定
超限，很难干净地中止下游已经开始的解析 —— 常见的做法是抛异常，
而那会变成 500 而不是 413。**用一个会产生错错误码的强校验，
不如用一个正确但覆盖不全的弱校验 + 端点级兜底**，并把边界写在这里。
"""

from __future__ import annotations

import re
from typing import Final

from starlette.types import ASGIApp, Receive, Scope, Send

from rednote.platform.errors import ErrorCode, error_response

# 协议 §1 的默认上限。用 2 * 1024 * 1024（MiB）而不是 2_000_000：
# 协议写的是 "2 MB"，而 JSON 请求体的实际约束来自内存与网关配置，
# MiB 是工程上更自然的单位。这个选择不影响本包验收（上传走分片协议），
# 已登记在模块 docstring 的上游说明里。
DEFAULT_BODY_LIMIT_BYTES: Final[int] = 2 * 1024 * 1024

# 分片上传端点：`.../uploads/<uuid>/parts/<数字>`。
# 用正则而不是前缀匹配：`/uploads/<id>/completions` 也是 POST JSON，
# 它**不该**被豁免（1 MB 的 orderedParts 数组本身就是异常输入）。
PART_UPLOAD_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"^/api/v1/uploads/[^/]+/parts/\d+$"
)


class RequestBodyLimitMiddleware:
    """按 `Content-Length` 拒绝过大的"普通请求体"。

    写成纯 ASGI 中间件而不是 `BaseHTTPMiddleware`，理由与 `main.py` 里的
    `RequestIdMiddleware` 相同：`BaseHTTPMiddleware` 会在独立任务里运行
    下游 app，并且对流式请求体有额外缓冲 —— 而本模块要判定的恰恰是
    "body 有多大"，用一层会改变 body 行为的包装去观察 body 是自相矛盾的。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int = DEFAULT_BODY_LIMIT_BYTES,
        exempt_path_pattern: re.Pattern[str] = PART_UPLOAD_PATH_RE,
    ) -> None:
        self.app = app
        self._max_bytes = max_bytes
        self._exempt = exempt_path_pattern

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if self._exempt.match(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        declared = self._content_length(scope)
        if declared is not None and declared > self._max_bytes:
            response = error_response(
                ErrorCode.UPLOAD_TOO_LARGE,
                "请求体超过允许大小。",
                details={
                    "reason": "request_body_too_large",
                    "limitBytes": str(self._max_bytes),
                    "declaredBytes": str(declared),
                    # 明确指向正确做法：协议要求大文件走分片协议。
                    "hint": "大文件请使用分片上传接口。",
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        """从原始头里取 Content-Length。取不到或非法返回 None。"""
        for name, value in scope.get("headers") or []:
            if name.lower() != b"content-length":
                continue
            try:
                parsed = int(value.decode("ascii").strip())
            except (UnicodeDecodeError, ValueError):
                # 非法值不猜测意图：当作"未声明"，交给端点兜底。
                # 在这里报 400 会让一个畸形的可选头把一个合法请求打掉。
                return None
            return parsed if parsed >= 0 else None
        return None


__all__ = [
    "DEFAULT_BODY_LIMIT_BYTES",
    "PART_UPLOAD_PATH_RE",
    "RequestBodyLimitMiddleware",
]
