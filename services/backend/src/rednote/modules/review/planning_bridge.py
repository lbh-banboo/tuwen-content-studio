"""`review` → `planning` 的唯一接缝（WP30）：平台媒体限制的**只读**取数。

## 为什么必须 `materialize=False`（R9，本包最容易踩的一处）

`get_effective_limits` 的 `materialize` 默认 **`True`**，而惰性物化
**是读路径上的写副作用**（`settings_service.py:296-301` 原文）。
用默认值的后果：

| 场景 | 后果 |
|---|---|
| `GET /self-checks/{runId}`（`VIEW` 权限） | 一个纯读请求往 `runtime_settings` 插一行继承副本 |
| `POST /projects/{id}/self-checks`（批量 N 条） | 放大成 N 次写（同一项目同一 key，第一次之后靠部分唯一索引撞车） |

因此本函数**把 `materialize=False` 写死**，不做成参数：做成参数就意味着
"某个调用点可以传 True"，而那个调用点迟早会出现。要物化请走 planning
自己的管理端点。

## 本包为什么不新增 `runtime_settings` 的 key

新增要改 `planning/types.py` 的 `SETTING_KEYS`，而 `modules/planning/**`
在本批是 **WP31 的写面**（接缝冻结 §5.6：WP31 是"本模块内唯一有写权的分支"）
⇒ 跨包争用。本包只**只读**调一个**已存在的占位键**
（`platform.mediaLimits`，OP-64 仍 OPEN）。

## 为什么不内置 1440×1920 / 2MB 的字面量

那等于在本包造**第二处**平台限制真值，而两处真值的分叉是静默的
（配置改了、规则还按旧值判，没有任何报错）。未配置时规则走
`SKIPPED(PLATFORM_MEDIA_LIMITS_NOT_CONFIGURED)` —— 前端显示"未判"，
不冒充通过。

## 为什么不 catch

`get_effective_limits` 在"项目不存在"时抛 404（`REASON_PROJECT_NOT_FOUND`）。
本包的调用点已经过两步骤鉴权（项目一定存在），所以这条路径不可达；
若真发生了，它应该原样冒出去而不是被吞成"未配置" —— 后者会让一个
真事故（项目被删）长得像一个合法状态（占位键没值）。
"""

from __future__ import annotations

import uuid
from typing import Any

from rednote.modules.planning.settings_service import get_effective_limits
from rednote.modules.planning.types import (
    SETTING_KEY_PLATFORM_MEDIA_LIMITS,
    EffectiveSetting,
)


def read_platform_media_limits(
    planning_repo: Any, *, project_id: uuid.UUID
) -> EffectiveSetting:
    """读本项目生效的平台媒体限制，**只读**（`materialize=False`，见 docstring）。

    返回 `EffectiveSetting`，调用方只用其中两维：
    `configured`（是否存在任何生效值）与 `value`（生效值本身）。
    两维刻意分开 —— 占位键的合法状态就是"没有值"，用 `value is None`
    兼任会与"有值但为空字典"混淆。
    """
    return get_effective_limits(
        planning_repo,
        SETTING_KEY_PLATFORM_MEDIA_LIMITS,
        project_id,
        # ★ 写死 False。做成参数就意味着某个调用点可以传 True（R9）。
        materialize=False,
    )


__all__ = ["read_platform_media_limits"]
