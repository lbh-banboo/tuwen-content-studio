"""供应商计价 / 预算预占 / 费用账本的依赖注入（WP25）。

**本模块存在的理由**：端点与 `service` 必须共用同一个 session。
本包对这件事的敏感度比 WP24 **更高**，原因是本包的写路径对
"两表必须在同一事务里改"的依赖是**结构性的**（不是"偶尔借用"）：

| 场景 | 若端点与 `service` 用两个 session | 症状 |
|---|---|---|
| `reserve_budget`（改 `project_budgets` + 插 `budget_reservations`） | 账面已扣、明细未落 | **钱扣了却说不出被谁扣的** —— 正是 §3 方案 V2 的否决理由 |
| `settle_reservation`（改 `budget_reservations` + 改 `project_budgets` + 插 `usage_ledger`） | 三者各自提交 | 账面与账本分叉（R10），且**不报错** |
| `estimate_cost`（`get_effective_limits` 会**惰性物化**项目副本） | 物化落在一个 session、预估落在另一个 | 无法回滚 ⇒ 预估失败却留下一条副本 |
| `PUT .../budget`（先 `ensure_budget` 再 `set_budget_limit`） | 先插了一行零限额的账面 | 后续所有预占**全部** `BUDGET_EXCEEDED`，且看不出为什么 |

最后一行是**本包最危险的一个**：`project_budgets` 是"一项目一份"的账面，
一行限额为 0 的存根会让这个项目**彻底无法提交任务**，
而错误信息看起来完全正常（就是"预算不足"）。

| 依赖 | 提供 | 谁用 |
|---|---|---|
| `BillingRepoDep` | `BillingRepository` | 全部 14 个端点 |

## 为什么只提供**一个**依赖（对比 WP24 提供了两个）

WP24 需要 `VersionRepoDep`，因为 `submit_draft` 要调 `versions` 模块的
`register_version`（跨模块接缝）。本包**没有任何跨模块仓储需求**：

- `estimate_cost` 读 WP24 的生效上限走的是 `planning.settings_service.
  get_effective_limits`，而它要的是**一个 `PlanningRepository`** ——
  这一点在 `service.estimate_cost` 内部就地构造
  （`PlanningRepository(repo.session)`）。
  在端点层一并注入 `PlanningRepoDep` 会让"本包依赖 planning 的**配置读取**"
  升级成"端点依赖 planning 的仓储"，把一条精确的缝变成一条宽缝。

**就地构造的代价是零**：`PlanningRepository` 是无状态薄封装（只持有 session），
而 `repo.session` 与端点 session 是**同一个对象** —— 这正是本模块要保证的事。

## 刻意不提供"Billing 专用权限依赖"

与 WP22/WP23/WP24 同一条取向：**不新建 `ProjectAction` / `OrgAction`**。
本包的权限需求全部落在**既有**动作上，且两个都是"此前零业务消费者"的：

| 动作用途 | 动作 | 已确认的取值域事实（逐行读 `permissions.py` 定案） |
|---|---|---|
| 读预算 / 预占 / 账本（`GET`） | `ProjectAction.VIEW_COST` | `_PROJECT_MATRIX`（第 135 / 145 行）下 **OWNER 与 COLLABORATOR 都是 True** |
| 写预算限额（`PUT .../budget`） | `OrgAction.MANAGE_BUDGET` | `_ORG_MATRIX` 下 **ADMIN=True（第 157 行）/ OPERATOR=False（第 166 行）** |

`MANAGE_BUDGET` 是组织级动作（**无项目上下文**），因此 `PUT .../budget`
不能走 `ProjectAccessDep.require()` —— 那只能查项目级动作。
它走 `require_org_action(OrgAction.MANAGE_BUDGET)`（与 WP24 的
`GLOBAL_SETTING_WRITE` 同款）。

## ★ `OPERATOR=False` 是本包**故意保留**的语义，不是待修的缺口

`_ORG_MATRIX` 第 164 行的原始注释是"运营不能创建项目、**不能碰预算**、
不能重开项目、不能管用户"。文档 §1.2 把它列为**零消费者权限**并明写：

> ⇒ **本包是 `MANAGE_BUDGET` 的第一个消费者**，且**这是"运营不能碰预算"
> 这句话第一次有代码落点**。

因此本包**不**照抄 WP24 的绕行手法（WP24 因 `MANAGE_DICTIONARY` 对
OPERATOR 为 `False`，改选了 `KNOWLEDGE_MANAGE` 以放行运营）。

两者的区别是**需求层的**，不是权限层的：

| | WP24 的批次上限 | 本包的预算限额 |
|---|---|---|
| 需求取向 | ADR-13 明确要求"运营能自助确认并调整当前生效值" ⇒ **必须放行运营** | 需求 §2.2"设置预算"行管理者=是、运营=否 ⇒ **必须挡住运营** |
| 结论 | 换一个运营开放的动作 | **就用 `MANAGE_BUDGET`，不放行** |

⚠️ 上一版本文档在本节写过"实测两种角色都是 `True`" —— 那是**错误断言**，
与 `permissions.py:166` 的磁盘实况相反，已更正。**教训**：矩阵取值必须
逐行读源码定案，不能凭"文档说它零消费者、所以大概两者都放行"推演；
推演出来的"事实"一旦写进 docstring，就成了下一个人眼里的权威
（判据纪律第 9 条："声称自己有防线比没有防线更危险"的同族形态）。

## 端点层的**实际影响**（写在这里以免 `api.py` 再推演一遍）

`PUT /projects/{projectId}/budget` 是**唯一**过一个组织级动作的端点，
因此它的调用者是**管理者**。但"读"是项目级的 —— 于是同一个预算有两个入口口径：

- `GET /projects/{projectId}/budget`：项目成员（OWNER / COLLABORATOR）即可读；
- `PUT /projects/{projectId}/budget`：仅管理员可写。

这不是缺陷。管理员**本来就对所有项目有全权**（`_PROJECT_MATRIX` 不计
管理者，`ProjectPermissions.allows` 第一句就是 `if self.is_admin: return True`），
因此"管理员能改任意项目预算"与"项目负责人只能看不能改"两者不冲突。
若将来需求放行"负责人可调本项目预算"，那是**需求变更**，
处置是把动作换成项目级的新动作，而不是在这里放宽 `MANAGE_BUDGET`。

多一个动作就多一处矩阵定义，而两处一旦不一致，
**放宽的那一处会生效**（因为它是先被调用的那层）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.billing.repository import BillingRepository
from rednote.modules.identity.dependencies import DBSession


def get_billing_repository(db: DBSession) -> BillingRepository:
    """请求级仓储实例（与端点 session 同源）。"""
    return BillingRepository(db)


#: 全部 14 个端点共用。
BillingRepoDep = Annotated[BillingRepository, Depends(get_billing_repository)]


__all__ = [
    "BillingRepoDep",
    "get_billing_repository",
]
