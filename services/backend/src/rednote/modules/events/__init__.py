"""领域事件与 SSE 分发（WP14）。

**对外接口**：
  · `service.publish_event()` —— 各业务包用它产生事件（**写库 + NOTIFY 同事务**）；
  · `service.backfill_events()` / `filter_visible()` —— SSE 端点内部用；
  · `router` —— 协议 §11 的 `GET /api/v1/events`。

**三层机制**（ADR-14）：
  · 真相层 —— `domain_events` 落库（`models.py`）；
  · 分发送达层 —— PG `LISTEN/NOTIFY`（`notifier.py`），payload 只带序号；
  · 补漏层 —— `Last-Event-ID` + 序号水位线（`service.py`）。

**关键性质**：`NOTIFY` 是 at-most-once，所以**通知全失效时系统仍然正确**
（靠心跳周期的补漏兜底），只是慢。这条性质让"内网 PG 行为与假设不符"
从"故障"降级为"性能问题" —— 它是本包结构上最重要的一个决定。

显式 import 模型，使 `migrations/env.py` 的 autogenerate 能看见这张表。
"""

from rednote.modules.events import models  # noqa: F401
from rednote.modules.events.api import router  # noqa: F401

__all__: list[str] = ["router"]