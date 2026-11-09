"""WP29 反馈定位与审核决定（`modules/feedback`）。

数据层 + 服务层 + 接口层已落盘：`models` / `ddl` / `repository` / `types` /
`content_bridge` / `media_bridge` / `service` / `dependencies` / `schemas` / `api`。

## 装配

- 表注册：本文件 import `models`（`migrations/env.py` 的接线由主 agent 收口时补）
- 端点注册：`main.py` 用 `from rednote.modules.feedback import router`
- 迁移：本波**不建**迁移文件，DDL 草案见 `docs/m3/WP29-ddl-草案.py`

## 一句话

意见落在"整篇 / 某段 / 某张图"三处之一；人工编辑追加版本并用
`If-Match` 防覆盖；审核结论不可变、改判靠 `supersedesId`。
"""

from __future__ import annotations

from rednote.modules.feedback import models  # noqa: F401  （建表注册，必须 import）
from rednote.modules.feedback.api import router  # noqa: F401  （端点注册）

__all__: list[str] = ["router"]
