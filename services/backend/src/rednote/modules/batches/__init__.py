"""WP32 扩产 / 批产（`modules/batches`）。

把“提交一个 1~30 条的扩产批次 / 1~50 条的批产批次”变成一次可重放、可部分失败后恢复、
可核对“按哪个上限、哪份方案、哪个基线提交的”的事务：同一个请求重发不会多出一个批次、
一条内容、一个任务，也不会多花一笔钱；一半失败时，成功的不重做、失败的能补做、
**未知的不乱重试**。

## 分层（单向，不可回指）

```
types ← progress（纯函数，读时折算）
  ↑
  └── models / ddl / repository ──→ service ──→ api
                                      ↑
  content_bridge / planning_bridge / billing_bridge / baseline_bridge /
  versions_bridge / jobs_bridge / audit_bridge        dependencies / schemas
```

* `types.py` **不 import 任何 `rednote.modules.*`**（平级模块不可互 import）；
* 跨模块**调用**走各 `*_bridge.py`（只转发、不写业务）；跨模块**取数**（`jobs`）走 `repository.py` 裸 SQL。

## 装配（主线收口动作，本包不做）

* 表注册：本文件 import `models`；`migrations/env.py` 的 import 由主线补；
* 端点注册：`main.py` 用 `from rednote.modules.batches import router`；
* 迁移：本包**不建**迁移文件，DDL 草案见 `docs/m3/WP32-ddl-草案.py`；
* `jobs.batch_id` 的 FK 与 `ix_jobs_batch_id` 是主线收口动作（草案第六节）；
* 关口：`dependencies.py::get_gate_checker` 默认 fail-closed，WP31 完成后由主线接线。
"""

from __future__ import annotations

from rednote.modules.batches import models  # noqa: F401  （建表注册，必须 import）
from rednote.modules.batches.api import router  # noqa: F401  （端点注册）

__all__: list[str] = ["router"]
