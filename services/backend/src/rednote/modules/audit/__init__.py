"""审计事件、站内待办与通知回执（WP15）。

**对外接口**：`service.py` 里的函数。

| 用途 | 函数 |
|---|---|
| 写审计 | `record_audit`（**与业务同事务**） |
| 建待办（去重） | `ensure_work_item` + `build_dedupe_key` |
| 查待办 / 查审计 | `list_work_items` / `list_audit_events` |
| 完成待办 | `resolve_work_item`（**不自动通过业务对象**） |

**本模块的 `work_items` 表由 WP10 建**（它的验收含"待办和历史正确"），
本包只消费，不重建。本包新建的是 `audit_events` 与 `notification_receipts`。

**不可变保护**：`audit_events` 用触发器拦 UPDATE/DELETE
（`ddl.py` 复用 WP11 已验证的机制）。`notification_receipts` 是**可变**的
（`read_at` / `dismissed_at` 会被写）—— 两者的约束正好相反，
这也是数据结构 §11 把它们列成两张表的原因。

显式 import 模型，使 `migrations/env.py` 的 autogenerate 能看见这两张表。
"""

from rednote.modules.audit import models  # noqa: F401
from rednote.modules.audit.api import router  # noqa: F401

__all__: list[str] = ["router"]
