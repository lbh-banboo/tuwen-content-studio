"""供应商计价 / 预算并发预占 / 费用账本（WP25）—— M3「生产」的第二个包。

## 本模块解决什么

把"这一批任务能不能花钱、花了多少、账单对不对得上"从
**调用后再统计的报表**变成**提交前就成立的硬约束**。

| 拼图 | 落点 | 表 |
|---|---|---|
| 单价冻结（改价 = 插新版本） | `pricing.py`（纯函数） | `pricing_versions`（**不可变**） |
| **预算并发不超承诺** | 单条条件 UPDATE（`repository`） | `project_budgets` + `budget_reservations` |
| 费用可核对（估 / 实 / 差） | `service.estimate_cost` / `settle_reservation` | `cost_estimates` |
| 账本不可篡改（ADR-13 同级） | `ddl.py` 触发器 + 冲正分录 | `usage_ledger`（**不可变**） |
| 对账（未结用量 + 账面分叉） | `reconciliation.py`（两条**独立**判据） | — |

## 表清单（5 张）

| 表 | 可变性 | 说明 |
|---|---|---|
| `pricing_versions` | **不可变** | 单价发布后不可改；改价 = 插新版本 |
| `project_budgets` | 可变（**无 `lock_version`**） | 一项目一份**账面**；更新全是单条原子条件 UPDATE |
| `budget_reservations` | 可变（状态机条件 UPDATE） | **一次**预占意图一行 |
| `cost_estimates` | 可变（`lock_version`） | 结算时回填实际值 |
| `usage_ledger` | **不可变**（只有 INSERT） | 追加式；冲正靠新分录（`REVERSAL`）而不是改旧行 |

**账面与明细为什么分两张表**：更新模式完全不同 —— 账面是"高频单条条件
UPDATE 改一行"，明细是"每次预占插一行、随状态迁移改自己那行"。
与 WP12 的 `quota_accounts`（账面）/ `upload_sessions`（明细）**同构**，
那是本仓已跑通并测过的形态。

## ★ 本包最核心的一条：正确性放在**单条 SQL 语句**里

```sql
UPDATE project_budgets
   SET reserved_amount = reserved_amount + :delta
 WHERE project_id = :project_id
   AND committed_amount + reserved_amount + :delta <= limit_amount
```

三个要点缺一不可（§2.6 R1）：

1. **条件在 `WHERE` 里**，不在 Python 的 `if` 里 —— 否则有读-改-写窗口；
2. **`rowcount == 1` 才是成功**，`0` 即预算不足（**不再查一次余量** ——
   读到的是别人刚改过的账面，只会误导）；
3. **`CHECK (committed + reserved <= limit)` 兜底** —— 应用层是主防线，
   但数据回填 / 运维脚本 / 将来的批量对账都可能绕过它。

**没有 `lock_version`**：更新全是单条原子条件 UPDATE，**没有读-改-写窗口**。
给账面加版本号只会引入"谁该赢"的问题，而它已经由 `WHERE` 解决了。
（`quota_accounts` docstring 的原文论证）

## 分层（单向，不反向依赖）

```
api.py             →  端点（14 个；协议零费用端点 ⇒ 全部由本包定义）
  ↓
schema.py          →  请求/响应 DTO（**金额一律字符串**）
  ↓
service.py         →  唯一写入口（预估 / 预占 / 释放 / 结算 / 记账，同事务改多表）
reconciliation.py  →  ★ 对账（只读，不写库，不加锁）
  ↓
repository.py      →  存储原语（★ 条件 UPDATE 预占 / 释放 / 结算）
  ↓
money.py / pricing.py  →  纯函数（零 float；不读库、不写库，可穷举验证）
  ↓
models.py / ddl.py / types.py
```

## ★ 金额的三层表示（R2 的唯一正确形态）

| 层 | 表示 | 出口 |
|---|---|---|
| 存储 | `Numeric(20,8)` —— PostgreSQL 任意精度**定点**，不是浮点 | — |
| Python | `Decimal`，**唯一入口 `money.py`** | — |
| HTTP | **定长小数字符串**（8 位小数 / 数量 10 位） | `money.to_wire` |

全仓零 `Decimal` 混用（§2.4 第 1 条实测），HTTP 侧一律字符串
（§2.4 第 7 条）—— `0.1` 在 JSON number 里**没有精确定义**。

## 对账为什么要**两条独立**判据

| 判据 | 只做它的后果 |
|---|---|
| 未结用量（扫 `usage_pending`，闭合 OP-108） | 查不出"账本有费用、账面状态没转"的钱 |
| 账面分叉（账本 Σ vs `recompute_committed`，R10） | 查不出"从未入账"的费用 |

**两者都要有才叫健康** —— 单条判据全绿不代表账面没问题。
"已结"的判据是"账本里有一条 `BILL` 分录"（**不是**"有任意分录"），
理由见 `reconciliation.py` 的模块 docstring。

## 装配

- `migrations/env.py` 补一行 `from rednote.modules.billing import models`
- `main.py` 末尾区注册本模块 `router`
- 迁移 `down_revision = "022ba9804d66"`（WP24 是当前文件链头）

## 已知取舍（均登记收口事项）

| 取舍 | 理由 |
|---|---|
| 端点是**本项目补充**（协议零费用端点） | §1.4 第 2 条；已登记 OP-176 待协议确认 |
| `PUT .../budget` 不带 `If-Match` | 无条件 UPDATE，无读-改-写窗口（与 WP24 配置端点同类取舍） |
| `GET .../budget` **不写库** | 与 WP24 `GET .../plan-draft` 的懒创建不同 —— 账面缺失返回"未配置"而不是凭空造一行零限额存根（见 `dependencies.py` 第四节：零限额存根会让项目**彻底无法提交任务**） |
| 文档 §2.2 / §2.6 R4 提到的 `usage_ledger.state = PENDING` **不存在** | `usage_ledger` **无状态列**；待办状态是 `PendingUsage.available` 现场派生的。R4 与 R5 冲突，实现时放弃状态列 —— **收口必须回填文档** |
| 文档 §2.2 / R9 引用 `WP15 ensure_work_item` | `work_items/` 下**没有** `service.py` / `types.py`（只有 `__init__` / `models` / `repository`），引用待核实/回填 |
"""

from __future__ import annotations

from rednote.modules.billing import models  # noqa: F401  （建表注册，必须 import）
from rednote.modules.billing.api import router  # noqa: F401  （端点注册）

__all__: list[str] = ["router"]
