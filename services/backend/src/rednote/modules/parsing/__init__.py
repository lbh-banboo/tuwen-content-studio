"""文档解析、OCR 与来源定位（WP17）。

**对外接口**：

| 用途 | 位置 |
|---|---|
| 端点 | `api.router`（`POST /files/{fileId}/parse-jobs`） |
| 发起解析（服务层） | `service.request_parse` |
| 执行解析（处理器层） | `service.execute_parse` |
| 与任务框架的接缝 | `handlers.build_task_registry` / `handlers.build_event_registry` |
| 解析器注册表 | `parsers.parse` / `parsers.supported_formats` |
| OCR 引擎 | `ocr.build_ocr_engine`（**默认不可用**） |

**两张表**：`parse_runs`（可变的过程记录）、`source_segments`
（**不可变证据**，触发器 + ORM 守卫双层保护）。

**分层**（单向，见 `types.py` 的说明）：

    types ← ocr
    types ← parsers → ocr
    types ← repository → models
    types ← service → repository / parsers / ocr
    api → service / dependencies

显式 import 模型，使 `migrations/env.py` 的 autogenerate 能看见这 2 张表。
"""

from rednote.modules.parsing import models  # noqa: F401
from rednote.modules.parsing.api import router  # noqa: F401

__all__: list[str] = ["router"]
