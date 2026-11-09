"""模型网关、出网策略与统一调用状态（WP16）。

**对外接口**：

| 用途 | 位置 |
|---|---|
| 派发 / 轮询 / 核对 | `service.dispatch_call` / `poll_call` / `reconcile_call` |
| 出网策略判定（纯函数） | `policy.evaluate_egress` |
| 能力适配器接口与注册表 | `adapters.CapabilityAdapter` / `AdapterRegistry` |
| 与任务框架的接缝 | `handlers.outcome_for_result` |

**本模块不提供任何 HTTP 端点**：协议 §12 明写这些是
"内部服务接口，不开放给前端直接调用"。管理端点（`/admin/providers` 等）
在研发计划里没有归属包，已登记 OP-123。

**出网留痕落在 WP15 的 `audit_events`**（不新建表），理由见
`docs/m1/WP16-模型网关说明.md` §2.6 约束 1：那张表已经有"模型层不可存正文"
与"不可篡改"两条硬保证，新建表只会得到更弱的复制品。

显式 import 模型，使 `migrations/env.py` 的 autogenerate 能看见这 4 张表。
"""

from rednote.modules.gateway import models  # noqa: F401

__all__: list[str] = []
