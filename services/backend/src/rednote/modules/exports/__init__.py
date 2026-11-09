"""固定版本 ZIP 导出（WP35）—— M3「交付」的收口包。

把"一组内容"在**此刻**的正式版本冻结成一个确定性 ZIP，落盘留痕，鉴权下载。

## 表（1 张）

| 表 | 可变性 | 说明 |
|---|---|---|
| `export_items` | **不可变** | 形态 A'：一行 = 一个 `(导出组, 内容, 冻结版本)`；同组行共享 `export_group_id`，不建组表。组级属性在组内重复，由延迟约束触发器在提交时守一致 |

## 分层（单向）

```
api.py          →  4 个端点（协议原件不在本机，形状由本包定义）
  ↓
service.py      →  七步编排（校验 → 幂等 → 归属 → 冻结 → 流式打包 → 流式落盘 → 写行）
  ↓
schemas.py      →  纯函数：入参校验 / 成员命名 / 确定性流式打包
  ↓
repository.py   →  本表 + 跨模块只读裸 SQL
  ↓
models.py / ddl.py / types.py
```

## 装配

- `migrations/env.py` 须 import 本包 `models`（建表注册）。
- `main.py` 注册本包 `router`。
- 跨包接缝：`files.service.store_generated_asset_from_path`（流式落盘，WP35 新增）。
"""

from __future__ import annotations

from rednote.modules.exports import models  # noqa: F401  （建表注册，必须 import）
from rednote.modules.exports.api import router  # noqa: F401  （端点注册）
from rednote.modules.exports.service import current_eligibility_hash  # noqa: F401  （WP36 只读接缝）

__all__: list[str] = ["router", "current_eligibility_hash"]
