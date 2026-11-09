"""WP33 重复检查 / 质量报告（`modules/quality`）。

把「这一批稿子彼此像不像、图和字对不对得上」变成一次可重放、可溯源、逐维度有结论的机器计算：
算了哪几个维度、每个维度比了几篇、命中哪几对、哪几个维度本轮根本没算——并且「没算」与「算了得 0」
在数据结构上无法混淆；整份报告作为一个 `REPORT` 版本落进 WP11 版本注册表。

**阈值是占位（待 OP-228，用户尚未给数值）**，每份报告落库快照；`ALERT` 不是阻断条件。
**只检查字面相似**（规范化 → 字符 2-gram → Jaccard，整数千分比，零新依赖），不是语义查重。

## 分层（单向，不可回指）

```
types ← similarity ← dimensions（纯函数，无 IO）
  ↑
  └── models / ddl / repository ──→ service ──→ api
        content_bridge / media_bridge / versions_bridge / files_bridge     dependencies / schemas
```

* `types.py` **不 import 任何 `rednote.modules.*`**；`similarity.py` / `dimensions.py` 只 import 本包 `types`
  / `similarity` 与标准库；
* 跨模块**调用**走各 `*_bridge.py`（只转发）；跨模块**取数**（`media_items` / `batches` / `batch_items` /
  `content_items`）走 `repository.py` 裸 SQL（只读，无 FK）；
* **切段只经 `media_bridge`**，本包零切段实现（`check_paragraph_split_parity.py::_IMPL_FILES` 仍应为 3）。

## 装配（主线收口动作，本包不做）

* 表注册：本文件 import `models`；`migrations/env.py` 的 import 由主线补；
* 端点注册：`main.py` 用 `from rednote.modules.quality import router`；
* 迁移：本包不建，DDL 规格见 `docs/m3/WP33-ddl-草案.py`（接在 `c3f34c0de003` 之后）；
* WP34 的 REPORT 解析器是「条件注册」（`quality_reports` 表存在才解析），本包落地后自动生效。
"""

from __future__ import annotations

from rednote.modules.quality import models  # noqa: F401  （建表注册，必须 import）
from rednote.modules.quality.api import router  # noqa: F401  （端点注册）

__all__: list[str] = ["router"]
