"""OpenAPI 错误响应声明的共享集合（协议 §1.2）。

**为什么这一层在 platform**：到 WP12 为止已有三个模块（项目、字典、文件上传）
要声明同一套错误响应。每个模块各留一份，迟早会漂移 ——
而 OpenAPI 契约是**对外承诺**，漂移意味着文档与实际不一致，
前端按错的文档写，直到联调才发现。

本文件合并了原 `modules/projects/api_responses.py` 的内容
（它的 docstring 里已经预告了这次合并），并补上 WP12 引入的 413。

**为什么每个端点都必须显式声明 `responses`**（这条是踩出来的）：
FastAPI 只检查 `responses` 里**有没有** 422 —— 没有就自动挂上自带的
`HTTPValidationError`，其形状与协议 §1.2 的错误体完全不同。
带查询参数的 GET（`limit` / `cursor` / `keyword`）同样会触发这个默认注入，
**不会因为"它是 GET"而豁免**。WP10 实测：只声明访问类错误，
契约里就多出了 `HTTPValidationError`，被契约测试拦下。
"""

from __future__ import annotations

from typing import Any

from fastapi import status

from rednote.platform.errors import ErrorEnvelopeForOpenApi, InternalErrorEnvelope

# 所有端点共有的三种"非业务"错误。
UNIVERSAL_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
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

# 项目级访问控制相关的三种错误。
ACCESS_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorEnvelopeForOpenApi,
        "description": "会话无效或已过期（SESSION_EXPIRED）。",
    },
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorEnvelopeForOpenApi,
        "description": "已可见但无该操作权限（PERMISSION_DENIED）。",
    },
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorEnvelopeForOpenApi,
        "description": (
            "对象不存在**或当前用户无权知道它存在**（RESOURCE_NOT_FOUND）。"
            "两者刻意不区分，防止通过枚举 UUID 判断对象是否存在。"
        ),
    },
}

# 并发与状态冲突。
CONCURRENCY_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorEnvelopeForOpenApi,
        "description": "业务状态冲突（INVALID_TRANSITION），不盲目重试。",
    },
    status.HTTP_412_PRECONDITION_FAILED: {
        "model": ErrorEnvelopeForOpenApi,
        "description": "版本冲突（VERSION_CONFLICT），响应 details 带 currentEtag。",
    },
    status.HTTP_428_PRECONDITION_REQUIRED: {
        "model": ErrorEnvelopeForOpenApi,
        "description": "缺少 If-Match 前置条件（PRECONDITION_REQUIRED）。",
    },
}

# WP12 新增：容量相关。
#
# 413 在本系统里有三个触发点，语义统一为"你要传的东西超出了允许大小"：
#   1. 普通请求体超过 2 MB（platform/request_limits.py）；
#   2. 预占会突破项目 300,000,000 字节研究容量（创建会话时，AC-06）；
#   3. 实际字节数超出剩余余量（完成上传时按实际修正，OP-93 的口径）。
# 前端只需要一种提示："太大了，请拆分或清理"。这也是选 413 而不是 422 的理由：
# 422 会让前端以为某个字段格式不对，提示不到点子上。
UPLOAD_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: {
        "model": ErrorEnvelopeForOpenApi,
        "description": (
            "超出容量限制（UPLOAD_TOO_LARGE）。details 带 "
            "limitBytes / usedBytes / remainingQuotaBytes（均为十进制字符串）。"
        ),
    },
}

# 503：存储后端不可用（例如内网 MinIO 未就绪、本地根目录未配置）。
STORAGE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorEnvelopeForOpenApi,
        "description": "依赖服务暂时不可用（DEPENDENCY_UNAVAILABLE）。",
    },
}

# 只读端点的完整集合。
READ_RESPONSES: dict[int | str, dict[str, Any]] = {
    **UNIVERSAL_ERROR_RESPONSES,
    **ACCESS_ERROR_RESPONSES,
}

# 写端点的完整集合。
WRITE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **UNIVERSAL_ERROR_RESPONSES,
    **ACCESS_ERROR_RESPONSES,
    **CONCURRENCY_ERROR_RESPONSES,
}

# 文件上传端点的完整集合（在写端点基础上补容量与存储依赖错误）。
UPLOAD_RESPONSES: dict[int | str, dict[str, Any]] = {
    **WRITE_RESPONSES,
    **UPLOAD_ERROR_RESPONSES,
    **STORAGE_ERROR_RESPONSES,
}

# 单资源读取（文件元数据、上传会话）。
UPLOAD_READ_RESPONSES: dict[int | str, dict[str, Any]] = {
    **READ_RESPONSES,
    **STORAGE_ERROR_RESPONSES,
}

__all__ = [
    "ACCESS_ERROR_RESPONSES",
    "CONCURRENCY_ERROR_RESPONSES",
    "READ_RESPONSES",
    "STORAGE_ERROR_RESPONSES",
    "UNIVERSAL_ERROR_RESPONSES",
    "UPLOAD_ERROR_RESPONSES",
    "UPLOAD_READ_RESPONSES",
    "UPLOAD_RESPONSES",
    "WRITE_RESPONSES",
]
