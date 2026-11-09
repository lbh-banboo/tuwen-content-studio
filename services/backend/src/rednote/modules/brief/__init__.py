"""Brief 结构化解构（WP18）。

**对外接口**：

| 用途 | 位置 |
|---|---|
| 端点 | `api.router`（五个，协议 03 §5 前五行） |
| 业务规则 | `service`（**唯一对外入口**） |
| 与任务框架的接缝 | `handlers.build_task_registry` |
| 数据库级保证 | `ddl.all_trigger_ddl` / `ddl.trigger_names` |
| 字段 Schema | `schema.BRIEF_FIELDS` / `schema.field_spec` |
| 解构通道 | `decomposer`（**默认不可用**，生产装配走 WP16 网关） |

**五张表**（数据结构 02 §6）：

| 表 | 性质 |
|---|---|
| `briefs` | 一项目一个逻辑 Brief（版本与草稿的挂载点），可变 |
| `brief_drafts` | 审核工作区，可变（`lock_version` 是 ETag 载体） |
| `brief_versions` | 固化版本，**不可变**（`id` 同时 FK `version_nodes.id`） |
| `brief_claims` | 版本内的结论，**不可变** |
| `brief_issues` | 版本内的疑问，**不可变** |

**分层**（单向）：

    types ← schema
    types ← models → platform.immutability
    types ← repository → models
    types ← decomposer → gateway（**惰性 import**）
    types ← service → repository / schema / decomposer
    handlers → service / repository / parsing / files / jobs
    api → service / dependencies

显式 import 模型与 router，使 `migrations/env.py` 的 autogenerate
与 `main.py` 的装配都能看见它们。

---

## 本包的三条硬约束（违反即返工）

1. **`state='PARTIAL'` 必须显式处理** —— 它是独立状态，不是 `SUCCEEDED`
   的变体。三层落地：受控枚举 / 草稿 CHECK / 版本延迟触发器。
   见 `service.py` 的模块 docstring 第一节。
2. **OP-88 的闭合点在本包** —— "延迟约束检查 `entity_type`/`entity_id`
   与子表一致"，含反向的"禁止孤立节点"。见 `ddl.py` 的模块 docstring 第二节。
3. **接缝调用不得改签名** —— `register_version(entity_type=ENTITY_TYPE_BRIEF, …)`
   与 `ParseRepository.list_segments` / `get_run` 原样调用。
"""

from rednote.modules.brief import models  # noqa: F401
from rednote.modules.brief.api import router  # noqa: F401

__all__: list[str] = ["router"]
