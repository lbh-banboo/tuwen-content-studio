"""WP34 回退影响图 / 导出失效（`modules/versions/impact`）。

只读影响图遍历（沿 `version_dependencies` 反向分层 BFS）+ 导出资格失效的**追加式事件表**
`export_validity_events`（不可变；`export_items` 零改动）。不执行回退、不自动触发扫描。

## 分层（单向）

```
types ← traversal（纯函数）
  ↑
  └── models / ddl / repository ──→ service ──→ api
        export_bridge / export_items_bridge / currentness_bridge     dependencies / schemas
```

* `types.py` / `traversal.py` **不 import 任何 `rednote.modules.*`**；
* `export_bridge.py` 是**唯一** import `rednote.modules.exports` 的文件（S2 只读接缝）；
* 跨模块取数（`export_items` / `content_*` / `media_*` / `quality_*`）走两个 `*_bridge.py` 的裸 SQL。

## 装配（主线收口动作，本包不做）

* 表注册：本文件 import `models`；`migrations/env.py` 的 import 由主线补；
* 端点注册：`main.py` 用 `from rednote.modules.versions.impact import router`；
  `versions/__init__.py`（根层）**不得** import `impact`（避免导入环）；
* 迁移：本包不建，DDL 规格见 `docs/m3/WP34-ddl-草案.py`（接在 `b2f32c0de002` 之后）。
"""

from __future__ import annotations

from rednote.modules.versions.impact import models  # noqa: F401  （建表注册，必须 import）
from rednote.modules.versions.impact.api import router  # noqa: F401  （端点注册）

__all__: list[str] = ["router"]
