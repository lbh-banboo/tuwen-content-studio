"""WP30 AI 自检与硬软规则引擎（`modules/review`）。

数据层 + 规则层 + 服务层 + 接口层已落盘：
`types` / `rules` / `evaluators` / `models` / `ddl` / `repository` /
`content_bridge` / `media_bridge` / `feedback_bridge` / `planning_bridge` /
`service` / `dependencies` / `schemas` / `api`。

## 分层（单向，不可回指）

```
types ← rules ← evaluators
  ↑               ↑
  └── models / ddl / repository ──→ service ──→ api
                                      ↑
          content_bridge / media_bridge / feedback_bridge / planning_bridge
```

- `types.py` **不 import 任何 `rednote.modules.*`**（平级模块不可互 import）；
- `evaluators.py` 是**纯函数**（零 IO、零 session、零 `datetime.now()`）——
  这是"每条规则都有阳性 + 阴性对照用例"这条判据的全部载体；
- 跨模块取数走 `repository.py` 的裸 SQL，跨模块**调用**走四个 bridge。

## 装配

- 表注册：本文件 import `models`（`migrations/env.py` 的接线由主 agent 收口时补）
- 端点注册：`main.py` 用 `from rednote.modules.review import router`
- 迁移：本波**不建**迁移文件，DDL 草案见 `docs/m3/WP30-ddl-草案.py`

## 一句话

把"这一篇稿子合不合规"从一句人话变成一次可重放、可溯源、逐规则有结论的
机器判定：谁判的、判的是哪一版、哪条规则命中、命中在哪一段/哪张图、
以及 —— **哪几条规则本轮根本没判**。最后那半句是本包最难的部分：
"没判"与"通过"在数据结构上必须无法混淆。

**本包不新建第二道审核闸**：阻断项经 `feedback_bridge` 落成 AI 反馈，
由 WP29 既有的 `decide_review` 拒绝"通过"。
"""

from __future__ import annotations

from rednote.modules.review import models  # noqa: F401  （建表注册，必须 import）
from rednote.modules.review.api import router  # noqa: F401  （端点注册）

__all__: list[str] = ["router"]
