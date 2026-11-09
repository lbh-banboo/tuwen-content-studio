"""项目模块的 OpenAPI 错误响应声明。

**本文件已改为从 `platform/api_responses.py` 重导出**（WP12 时期合并）。

历史：这些集合原本定义在本文件里，服务于 WP10 的项目端点与字典端点。
WP12 引入文件上传端点后出现第三个消费方 —— 三份拷贝正是最容易漂移的
情形，而 OpenAPI 契约是对外承诺，漂移会让前端按错的文档实现。
因此集合上移到平台层，本文件保留为**向后兼容的重导出**：
既有的 `from rednote.modules.projects.api_responses import WRITE_RESPONSES`
全部无需改动，也不会出现"改了一处忘了另一处"。

（`identity/api.py` 里仍有自己的一份同类声明，那是 WP08 的产物。
它没有第三个消费方，本轮不动它 —— 避免让 WP12 的改动波及已验收的工作包。
真正的合并应发生在它也需要扩张的时候。）
"""

from __future__ import annotations

from rednote.platform.api_responses import (
    ACCESS_ERROR_RESPONSES,
    CONCURRENCY_ERROR_RESPONSES,
    READ_RESPONSES,
    UNIVERSAL_ERROR_RESPONSES,
    WRITE_RESPONSES,
)

__all__ = [
    "ACCESS_ERROR_RESPONSES",
    "CONCURRENCY_ERROR_RESPONSES",
    "READ_RESPONSES",
    "UNIVERSAL_ERROR_RESPONSES",
    "WRITE_RESPONSES",
]
