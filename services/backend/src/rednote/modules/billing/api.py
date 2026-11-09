"""供应商计价 / 预算并发预占 / 费用账本的端点（WP25）。

| 方法 | 路径 | 成功码 | 权限 | 依据 |
|---|---|---|---|---|
| GET | `/projects/{projectId}/budget` | 200 | `VIEW_COST` | §1.2（"看这个项目还剩多少预算"） |
| PUT | `/projects/{projectId}/budget` | 200 | `MANAGE_BUDGET` | §1.2（**本包是它的第一个消费者**） |
| GET | `/projects/{projectId}/budget-reservations` | 200 | `VIEW_COST` | §1.2（"这笔钱被谁扣住了"） |
| POST | `/projects/{projectId}/budget-reservations` | 201 | `EDIT` | §1.7（**唯一预占入口**，WP26/WP27/WP28/WP31 都走这里） |
| POST | `/budget-reservations/{reservationId}/releases` | 200 | `EDIT` | §1.7（失败 / 取消路径） |
| POST | `/budget-reservations/{reservationId}/settlements` | 200 | `EDIT` | §1.7（对账 / 完成路径） |
| GET | `/projects/{projectId}/ledger-entries` | 200 | `VIEW_COST` | §1.2（"实际花了多少"） |
| GET | `/projects/{projectId}/ledger-summary` | 200 | `VIEW_COST` | 本包补充（分组小计，省一次全量拉取） |
| POST | `/projects/{projectId}/ledger-entries` | 201 | `EDIT` | §1.5（人工冲正：退款自动执行被砍，但**冲正分录要有人能写**） |
| GET | `/projects/{projectId}/cost-estimates` | 200 | `VIEW_COST` | §1.3 F3（AC-26「估算」） |
| POST | `/projects/{projectId}/cost-estimates` | 201 | `EDIT` | §1.3 F3（"这次要花多少"） |
| GET | `/cost-estimates/{estimateId}` | 200 | `VIEW_COST` | §1.3 F3 |
| GET | `/pricing-versions` | 200 | 登录 | 本包补充（价格透明） |
| POST | `/billing/reconciliations` | 200 | 登录 | §1.3 F6（AC-26「重试核对」；闭合 OP-108 / OP-132） |

**协议零费用端点**（§1.4 第 2 条实测：55 paths 里无 cost/budget/ledger/pricing/usage 前缀），
因此十四条端点的形状**全部由本包定义**，路径按既有命名风格
（`{projectId}` / 连字符 / 复数资源名）。已登记待协议确认（与 WP24 §1.4 第 11 条同款）。

---

## 一、权限：一个 `MANAGE_BUDGET`，其余全是项目级

| 端点类别 | 权限 | 判定依赖 |
|---|---|---|
| `PUT /projects/{projectId}/budget` | `OrgAction.MANAGE_BUDGET` | `BUDGET_WRITE`（**组织级，无项目上下文**） |
| 其余 `{projectId}` 下的端点 | `ProjectAction.VIEW_COST` / `EDIT` | `ProjectAccessDep.require(...)` |

`MANAGE_BUDGET` 是组织级动作，**不能**走 `ProjectAccessDep.require()` ——
那只能查项目级动作（`ProjectAccess.require` 的类型签名就是 `ProjectAction`）。
因此 `PUT .../budget` 走 `require_org_action`，与 WP24 的 `GLOBAL_SETTING_WRITE` 同款。

**`OPERATOR=False` 是故意保留的**（`billing/dependencies.py` 第 52~74 行已论证）：
需求 §2.2 的"设置预算"行是**管理者=是、运营=否**，而 `_ORG_MATRIX`
第 164 行的原注释就是"运营不能创建项目、**不能碰预算**"。
本包**不**照抄 WP24 的绕行手法（WP24 的 `MANAGE_DICTIONARY` 对运营为 `False`，
故改选 `KNOWLEDGE_MANAGE` 以放行运营 —— 那是 ADR-13 的明确要求）。

⇒ 同一个预算有**两个入口口径**，且这是正确的：

- `GET .../budget`：项目成员（OWNER / COLLABORATOR，两者 `VIEW_COST` 都是 `True`）可读；
- `PUT .../budget`：仅管理者可写。

管理员本来就对所有项目有全权（`ProjectPermissions.allows` 第一句是
`if self.is_admin: return True`），两者不冲突。

## 二、★ 子资源端点：`{reservationId}` / `{estimateId}` 不在项目路径下

`POST /budget-reservations/{reservationId}/releases` 与
`GET /cost-estimates/{estimateId}` 按**资源自身的 id** 寻址，
因此没有 `Path(alias="projectId")` 可给 `ProjectAccessDep` 用。
照 `modules/files/dependencies.py` 的同款手法**分两步**：

1. 读资源（**不存在 → 404**，含"不存在"与"无权知道它存在"不可区分）；
2. 由资源反查 `project_id`，再 `resolve_project_access` 判权限。

**顺序不可调换**：先判权限就得先知道资源属于哪个项目，而那个信息只能从资源来。
反过来（先取资源再判权限）是安全的 —— 此时对象已知，权限判定的结果不影响
"它是否存在"这件事是否泄露（协议 §1.2 的防枚举只在**未授权**时要求 404，
而已授权用户看到自己的资源本就是正常的）。

`resolve_project_access` 会**再查一次项目**（第一次查询在 `project_exists` / 资源行里），
这是已知的重复 —— 但 `files/dependencies.py` 已就此写过：
"两处各判一次权限"才是危险的那种重复，
而这里只有**一处**权限判定，重复的只是一次按主键的查询。

## 三、★ `budget_reservations` 的权限为什么是 `EDIT` 而不是 `VIEW_COST`

`VIEW_COST` 的语义是"**查看**本项目费用"（`permissions.py:91`），而预占/释放/结算
都是**写**：它们真的会改 `project_budgets.reserved_amount`。

若用 `VIEW_COST` 做写端点的权限，协作者就能拿走别人项目的预算 ——
而 `VIEW_COST` 对协作者是 `True`（第 145 行）。这不是"权限偏紧/偏松"的取舍，
而是把只读权限当写权限用，属**权限混淆**。

`EDIT` 的注释是"编辑 Brief、方案、内容" —— 预占是"**为生成内容而先扣住额度**"，
属内容生产的原料准备，与 `EDIT` 同级。两者（OWNER / COLLABORATOR）都为 `True`，
因此"协作者能预占"是**有意放行**的：协作者本来就要参与生产。

## 四、分页：一律 `DataEnvelope[list[T]]`，**不做游标分页**

四个列表端点（预占明细 / 账本 / 预估 / 价目版本）的仓储原语
**全部是 `LIMIT` 语义，没有 keyset 游标**（`repository.py` 第 484 / 573 / 867 / 971 行）。

- 若声明 `PaginatedEnvelope[T]`，`page.nextCursor` 就**没有真值来源**：
  要么填 `null` （等于谎称"没有下一页"），要么现造一个假游标 ——
  后者是**判据纪律第 9 条**的形态（文档声称有分页能力而实现没有）。
- `paginate_items` 是**内存分页**（`pagination.py` 第 308 行），
  要用它就得先把**全量**取回来 —— 那就把 `LIMIT` 的意义抹掉了，
  而账本是 append-only 表，会持续增长。

因此照 `planning/api.py::list_diverged_projects` 的先例用
`DataEnvelope[list[T]]`，并**把 `limit` 与 `truncated` 的语义显式暴露**：

| 端点 | 截断的表达 |
|---|---|
| 预占 / 预估 | `limit` 直传仓储；无 `truncated` 字段 ⇒ 调用方按条数自己判 |
| 账本 | `GET .../ledger-summary` 回带 `truncated`（取满也算，见 `schema.ledger_summary_body`） |

⚠️ 已登记的收口方向：**当单项目账本条数破千**（`limit` 上限 100 时
"最近 100 条"这个视图会明显不够用），改为 keyset 游标分页 ——
那需要给四个 `list_*` 原语加 `after` 参数，是**仓储层改造**，不在本包。
判据：本包**不**声明 `page` 字段，因此"有没有分页"在 OpenAPI 里
不需要靠人读文档判断（声明了却没实现才是危险的那种）。

## 五、金额的进出：入参过 `money`，出参过 `as_dict()`

**入参**：请求体里的金额是 `str`（`schema.py` 只做"非空 + 长度"粗筛），
解析**全部**交给 `money.parse_wire_*`（本包唯一的解析入口）—— 不手写
`Decimal(str)`，也不借服务层的私有函数。

| 入口 | 解析函数 | 解析失败时 |
|---|---|---|
| `service.*` 的 `amount` / `limit_amount` / `actual_amount` | `money.quantize`（经 `service._as_amount`） | 服务层翻 422 |
| `_ledger_input` 的 `entry.amount` | `money.parse_wire_optional` | **本文件就地翻 422** |
| `_ledger_input` 的 `entry.quantity` | `money.parse_quantity_wire` | 同上 |

两处口径**同源**（`_as_amount` 内部就是 `money.quantize`，而
`quantize` 收 `str` 时走同一个 `_parse_text`），因此不构成第二真值来源。

⚠️ 本文件**必须**自己翻 `MoneyError`：它不继承 `ApiError`，
若裸抛会被 `platform/errors.py:476` 的兜底处理器吞成 **500**
（`schema.py` 第 26~27 行已就这个坑写过）。

**出参**：全部走 `XxxBody.of(view)` → `view.as_dict()` →
`money.to_wire`。本文件**没有任何金额格式化代码**（`schema.py` 第 315~324 行
"转换点只有一处"的同一条纪律）。

## 五之二、本文件的错误工厂是**自己的**，不借 `service._*`

全仓检索：**没有任何 `api.py` 从 `service` 借私有函数**
（唯一同形先例是 `projects/api.py:86` 的**本地** `_validation_error`）。
借私有名不会立刻报错，但它把两层的边界抹掉了 ——
服务层改 `fieldErrors` 的键名，端点层会**静默**跟着变。
因此 `_validation_error` / `_not_found` 在本文件各有一份。

**`reason` 的取值则有明确的分工**：契约里登记过的（`types.py` 的 24 个
`REASON_*`）用 `service.REASON_*` 常量引用；**纯端点层的形状错**
（`_REASON_LEDGER_ENTRY_INVALID`，只可能由"请求体里的分录不合法"触发）
用本文件私有的常量，**不**去扩 `types.py` 的契约取值域。

## 六、不带 `If-Match`（与 WP24 的写端点**相反**）

`project_budgets` **没有 `lock_version`**（§1.6 R1 明确要求不带：
"乐观锁会引入多余的失败分支"）。它的并发正确性由**单条条件 UPDATE** 保证：

```sql
UPDATE project_budgets
   SET reserved_amount = reserved_amount + :delta
 WHERE project_id = :project_id
   AND committed_amount + reserved_amount + :delta <= limit_amount
```

`rowcount == 1` 才是成功（`repository.reserve_budget`）。这条路比 ETag **更强**：
ETag 只能让一个客户端失败重试，条件 UPDATE 让两个预占**不可能交错**。

同理 `budget_reservations` 的释放/结算走**状态机条件 UPDATE**
（`WHERE state IN <持有态>`），重复调用 `rowcount` 为 0 —— 也不需要 ETag。

⚠️ `cost_estimates` **有** `lock_version`，但本包**不提供修改预估的端点**
（预估只增不改：改的是 `state`，由 WP31 试产关口消费，不在本包范围）⇒
本文件不需要 `require_if_match`，也**不 import 它**。

## 七、`POST /billing/reconciliations` 是**纯读**（不写库）

`reconciliation.reconcile_pending` 只做两件事：扫 `usage_pending=true` 的
attempt、查它们有没有确认性账本分录。它**不结算、不挂账** ——
"把未结的自动结掉"是 WP43 定时任务的事（§1.7 的消费者列）。

因此它用 `POST`（查询体里带 `limit` / `projectId` 这类筛选，
且与 GET 的幂等语义不同）但权限只要求**登录**：
它返回的是**跨项目**的运维视图，没有单一 `projectId` 可用于项目级判定。
这与 WP24 `list_diverged_projects` 用 `CurrentUserDep` 是同一条取向，
但更强的理由是：它**不含任何金额**（只有 attempt 的元信息与"有没有账本行"），
所以放宽到"登录即可"不泄露费用。

⚠️ 若将来它改成"顺手把未结的结掉"，那就是**写端点**，
必须同时补 `EDIT`-类的权限判定 —— 这条注释就是那个改造的入口提示。

## 八、响应组装只读服务层返回值，不戳 ORM 实例

`planning/api.py` 第六节的同款纪律：ORM 实例绑在 session 上，
而响应组装发生在 session 关闭之后（`DetachedInstanceError`）。
本文件里所有 `XxxBody.of(...)` 的入参都是 frozen 值对象
（`types.*View` / `ReconciliationReport`），唯一的例外是
`ledger_summary_body(repo, ...)` —— 它自己读库、自己组装，在函数内完成。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Path, Query, status

from rednote.modules.billing import money, reconciliation, service
from rednote.modules.billing.dependencies import BillingRepoDep
from rednote.modules.billing.schema import (
    AppendLedgerEntryBody,
    BudgetBody,
    CostEstimateBody,
    EstimateCostBody,
    LedgerEntryBody,
    LedgerEntryResponseBody,
    LedgerSummaryBody,
    PricingVersionBody,
    ReconciliationReportBody,
    ReleaseReservationBody,
    ReservationBody,
    ReserveBudgetBody,
    SetBudgetBody,
    SettleReservationBody,
    ledger_summary_body,
)
from rednote.modules.billing.types import (
    ESTIMATE_STATES,
    PRICING_STATES,
    RESERVATION_STATES,
    LedgerEntryInput,
)
from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.projects.dependencies import (
    ProjectAccessDep,
    ProjectRepoDep,
    require_org_action,
    resolve_project_access,
)
from rednote.modules.projects.permissions import OrgAction, ProjectAction
from rednote.platform.api_responses import (
    READ_RESPONSES,
    WRITE_RESPONSES,
)
from rednote.platform.errors import (
    ApiError,
    DataEnvelope,
    ErrorCode,
    ErrorEnvelopeForOpenApi,
    get_request_id,
)
from rednote.platform.pagination import single_response

router = APIRouter(tags=["billing"])

# 与 `planning/api.py` 同款别名：端点声明里用短名，读起来不被前缀淹没。
BILLING_READ_RESPONSES = READ_RESPONSES

#: `422 BUDGET_EXCEEDED` 的响应声明。
#:
#: ⚠️ **必须单独声明**：`UNIVERSAL_ERROR_RESPONSES` 里已有 422，
#: 但它的描述是"字段校验未通过（VALIDATION_FAILED）"——
#: 而本包的预占失败给的是**另一个码**（`BUDGET_EXCEEDED`），
#: 且 `details` 的形状不同（带 `reason` 与四个金额，不带 `fieldErrors`）。
#: 若只声明通用的那个，前端读 OpenAPI 会以为 422 一定是字段格式错，
#: 于是把"预算不足"渲染成"请检查输入格式"。
#:
#: 写法照抄 `UPLOAD_ERROR_RESPONSES`（`platform/api_responses.py` 第 86 行）：
#: 同状态码在**本模块内**覆盖描述，不动 platform 层的通用集合
#: （在 platform 里改会让所有模块的 422 描述都变成预算语义）。
BUDGET_EXCEEDED_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_ENTITY: {
        "model": ErrorEnvelopeForOpenApi,
        "description": (
            "项目预算不足（BUDGET_EXCEEDED）。details 带 reason / limit / "
            "committed / reserved / available（均为十进制字符串）。"
            "**不可重试** —— 它是业务结论，不是瞬时故障"
            "（`_RETRYABLE_BY_CODE` 里只有 RATE_LIMITED 与 DEPENDENCY_UNAVAILABLE）。"
        ),
    },
}

BILLING_WRITE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **WRITE_RESPONSES,
    **BUDGET_EXCEEDED_RESPONSES,
}

# 组织级权限：写预算限额。
#
# ⚠️ **必须包在 `Depends(...)` 里**：`require_org_action` 返回的是一个
# **依赖函数**，直接放进 `dependencies=[...]` 会让 FastAPI 尝试读它的
# `.dependency` 属性并抛 `AttributeError`（`planning/api.py` 第 176~188 行
# 已就本坑写过注释）。
#
# 动作取 `MANAGE_BUDGET` 而**不**照抄 WP24 的 `KNOWLEDGE_MANAGE`：
# 需求 §2.2 的"设置预算"行是管理者=是、运营=否，且 `_ORG_MATRIX`
# 第 164 行原注释"运营不能碰预算"。本包是这句话的第一个代码落点。
BUDGET_WRITE = [Depends(require_org_action(OrgAction.MANAGE_BUDGET))]

#: 对账扫描的默认与上限条数。
#:
#: **为什么默认是 200**：与 `repository.DEFAULT_PENDING_SCAN_LIMIT` 同值 ——
#: 它是"一次往返能处理多大一批"的经验值（`reconcile_pending` 内部
#: 把逐条查询折成批量查询，200 条只需 2 次往返）。
#:
#: **为什么上限是 1000 而不是 100**：`platform.pagination.MAX_LIMIT` 的 100
#: 是**面向客户端的列表页**口径，而对账是**运维批量扫描** ——
#: 它的 `truncated=true` 意味着"全平台还没扫完"，而把上限压到 100
#: 会让"扫完一个中等规模平台"需要十几次调用。两个 `limit` 的语义不同，
#: 因此这里显式定义**本模块自己的**上限，不复用 pagination 的。
DEFAULT_RECONCILIATION_LIMIT: int = 200
MAX_RECONCILIATION_LIMIT: int = 1000


# ---------------------------------------------------------------------------
# 零、本文件自己的错误工厂（**不跨模块借服务层的私有函数**）
# ---------------------------------------------------------------------------


def _validation_error(field: str, message: str, *, reason: str) -> ApiError:
    """构造 422 `VALIDATION_FAILED`（`fieldErrors` 形状与 `service` 同口径）。

    ## 为什么本文件要自带一份，而不是调 `service._validation_error`

    全仓检索：**没有任何 `api.py` 从 `service` 借私有名**
    （`from ...service import _xxx` 命中 0 处）；唯一的同形先例是
    `projects/api.py:86` 的**本地** `_validation_error`。
    借私有名的代价不是"能不能跑"，而是它把两层的边界抹掉了 ——
    服务层改一个 `fieldErrors` 的键名，本文件会**静默**跟着变
    （同一个名字，没有编译器或判据会提示这是跨层耦合）。

    端点层需要"把协议形状的错误说清楚"时，自己构造即可：
    `ApiError` 是公开类型，`ErrorCode.VALIDATION_FAILED` 是公开枚举。
    """
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        message,
        details={"fieldErrors": [{"field": field, "type": reason, "message": message}], "reason": reason},
    )


def _not_found(message: str, *, reason: str, **extra: Any) -> ApiError:
    """构造 404 `RESOURCE_NOT_FOUND`（同 `_validation_error` 的落地理由）。"""
    details: dict[str, Any] = {"reason": reason}
    details.update(extra)
    return ApiError(ErrorCode.RESOURCE_NOT_FOUND, message, details=details)


#: 分录结构非法（`LedgerEntryInput.__post_init__` 的六项校验之一不满足）。
#:
#: **为何不自建 `REASON_*` 常量到 `types.py`**：那六项校验各对应一种情形
#: （未知类型 / 符号不符 / 冲正缺目标 / 用量与单位不成对），要如实分列
#: 就得新增四五个 reason —— 而 `types.py` 的 `REASON_*` 取值域是
#: 跨包契约的一部分（`docs/m3/WP25-*.md` §2.2 的表逐行引用它），
#: 为一个纯协议层的形状错去扩契约取值域不划算。
#: 因此这里用**本文件私有的** reason，且**只用于端点入口的形状校验**
#: —— 服务层的 six 项校验仍在（`settle_reservation` 内部构造时同样生效）。
_REASON_LEDGER_ENTRY_INVALID: str = "LEDGER_ENTRY_INVALID"


# ---------------------------------------------------------------------------
# 一、子资源鉴权（`{reservationId}` / `{estimateId}`）
# ---------------------------------------------------------------------------


def _reservation_project_id(repo: Any, reservation_id: uuid.UUID) -> uuid.UUID:
    """由预占 id 反查项目 id。**不存在直接 404**。"""
    row = repo.get_reservation(reservation_id)
    if row is None:
        raise _not_found(
            f"预占 {reservation_id} 不存在。",
            reason=service.REASON_RESERVATION_NOT_FOUND,
            reservationId=str(reservation_id),
        )
    return row.project_id


def _estimate_project_id(repo: Any, estimate_id: uuid.UUID) -> uuid.UUID:
    """由预估 id 反查项目 id。**不存在直接 404**。"""
    row = repo.get_estimate(estimate_id)
    if row is None:
        raise _not_found(
            f"预估 {estimate_id} 不存在。",
            reason=service.REASON_ESTIMATE_NOT_FOUND,
            estimateId=str(estimate_id),
        )
    return row.project_id


# ---------------------------------------------------------------------------
# 二、预算账面：读 / 写
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/budget",
    response_model=DataEnvelope[BudgetBody],
    responses=BILLING_READ_RESPONSES,
    summary="读取项目预算账面（限额 / 已结算 / 已预占 / 可用，四值一起）",
)
def get_budget(
    access: ProjectAccessDep,
    repo: BillingRepoDep,
) -> dict[str, Any]:
    """四值一起返回，**不只回 `available`**。

    只给 `available` 就是"悄悄算错也没人知道"：调用方拿不到 `limit` 与
    `reserved` 就无法发现 `available` 是不是真的等于
    `limit - committed - reserved`（`BudgetView` docstring 的第一条理由）。

    ## 为什么不校验项目存在性（服务层也不校验）

    `service.get_budget_view` docstring 明写"不校验项目存在性"，
    并指出"调用方若要严格的 404 语义，自己调 `_require_project`
    （`api.py` 的 `ProjectAccessDep` 已经做了这件事）"——
    **本行 `access: ProjectAccessDep` 就是那件事的落点**，
    因此这里不再调 `_require_project`（会白查一次库）。

    ## `view.configured=False` 是正常结果，不是错误

    从未设过预算的项目返回全 0 的账面 + `configured=false`。
    若这里报 404，页面就没法渲染"这个项目还没设预算"这个事实 ——
    而它恰恰是管理层要看的第一个事实（`DEFAULT_BUDGET_LIMIT` 的注释）。

    ## ★ 为什么**必须**显式 `access.require(VIEW_COST)`

    `ProjectAccessDep` 只做**成员级**判定（项目存在 + 是成员/管理员），
    它**不含任何动作级判定**（`resolve_project_access` 的四步里没有
    `require`）。也就是说，少了下面这一行，**任何协作者都能读到
    本项目的费用账面** —— 而 `VIEW_COST` 在矩阵里对
    OWNER / COLLABORATOR **都是 `True`**（`permissions.py:135 / :145`），
    所以这一行**不会**误拒任何人；它拦的是"矩阵以后把某项改成 False"
    那一天（届时少这一行的端点会成为唯一的例外，且不报错）。

    这是**判据纪律第 9 条**的形态："声称自己有防线"比"没有防线"更危险：
    模块 docstring 的表格写了 `GET .../budget → VIEW_COST`，
    若代码里没有 `require`，那张表就是**无人对账的声称**。
    """
    access.require(ProjectAction.VIEW_COST)
    return single_response(
        BudgetBody.of(service.get_budget_view(repo, project_id=access.project_id))
    )


@router.put(
    "/projects/{projectId}/budget",
    response_model=DataEnvelope[BudgetBody],
    responses=BILLING_WRITE_RESPONSES,
    dependencies=BUDGET_WRITE,
    summary="设置项目预算限额（仅管理员；调低时若装不下既有占用会被拒）",
)
def put_budget(
    user: CurrentUserDep,
    repo: BillingRepoDep,
    body: SetBudgetBody,
    project_id: Annotated[uuid.UUID, Path(alias="projectId")],
) -> dict[str, Any]:
    """**唯一预算写入口**（§2.6 实现约束：写 `project_budgets` 的路径只有它）。

    ## 为什么这个端点没有 `access: ProjectAccessDep`

    它走**组织级**权限（`BUDGET_WRITE` ⇒ `MANAGE_BUDGET`），
    而组织级动作**没有项目上下文** —— `ProjectAccessDep` 提供的是
    项目级判定，两者不是同一层。若同时注入，读者会以为"两个都要过"，
    而实际只有 `dependencies=BUDGET_WRITE` 那一层生效
    （`ProjectAccessDep` 只会在 handler 里被 `del` 掉）。

    这也解释了为什么 `projectId` 要**显式声明为路径参数**：
    `ProjectAccessDep` 不在签名里，就没有任何依赖会去解析 `{projectId}`。

    ## 两个拒绝分支（`service.set_budget` 分列，不合并成"设不了"）

    | 情形 | reason | HTTP |
    |---|---|---|
    | 已有金额且换币种 | `CURRENCY_MISMATCH` | 422 `VALIDATION_FAILED` |
    | 新额度装不下既有占用 | `BUDGET_EXCEEDED` | 422 `BUDGET_EXCEEDED` |

    换币种会**重新解释已发生的数字**（100 元变 100 美元），语义变更不是数值变更；
    调低上限装不下占用则要给"先释放占用"这条可操作的指引。

    ## 不带 `If-Match`（见模块 docstring 第六节）

    `project_budgets` 无 `lock_version`，并发正确性由条件 UPDATE 保证。
    """
    view = service.set_budget(
        repo,
        project_id=project_id,
        limit_amount=body.limit_amount,
        currency=body.currency,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    return single_response(BudgetBody.of(view))


# ---------------------------------------------------------------------------
# 三、预占：列 / 建
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/budget-reservations",
    response_model=DataEnvelope[list[ReservationBody]],
    responses=BILLING_READ_RESPONSES,
    summary="列项目预占明细（新→旧，可按状态筛选）",
)
def list_reservations(
    access: ProjectAccessDep,
    repo: BillingRepoDep,
    states: Annotated[
        list[str] | None,
        Query(description=f"状态筛选，可重复传。取值域 {sorted(RESERVATION_STATES)}"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="最多返回条数。")] = 100,
) -> dict[str, Any]:
    """回答"这笔钱被谁扣住了"（§3 方案 V2 的否决理由就是答不了这个问题）。

    ## `states` 为什么不做白名单校验

    非法状态值由 SQL 层直接给出空结果（`state IN (...)` 匹配不到任何行），
    而不是 422。理由：这是**筛选条件**，筛不到就是空列表 ——
    与 `keyword` 搜不到同性质。若报 422，前端在"按状态筛选"的
    下拉里多传一个废弃状态就会整页报错，而它本该只是显示空。

    ## 不做游标分页（见模块 docstring 第四节）

    `list_reservations` 是 `LIMIT` 语义（`repository.py` 第 484 行），
    没有 keyset 游标可给。用 `DataEnvelope[list[T]]` 并**不声明 `page` 字段**。
    调用方按返回条数 == `limit` 判断是否被截断。
    """
    access.require(ProjectAction.VIEW_COST)
    rows = service.list_project_reservations(
        repo, project_id=access.project_id, states=states, limit=limit
    )
    return single_response([ReservationBody.of(row) for row in rows])


@router.post(
    "/projects/{projectId}/budget-reservations",
    response_model=DataEnvelope[ReservationBody],
    status_code=status.HTTP_201_CREATED,
    responses=BILLING_WRITE_RESPONSES,
    summary="为项目预占预算（唯一预占入口；不足返回 422 BUDGET_EXCEEDED）",
)
def create_reservation(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: BillingRepoDep,
    body: ReserveBudgetBody,
) -> dict[str, Any]:
    """**本包命门**（§1.3 F4 = W10「预算并发预占测试」的落点）。

    ## 权限是 `EDIT` 而不是 `VIEW_COST`（见模块 docstring 第三节）

    预占真的会改 `project_budgets.reserved_amount`，是**写**操作。
    `VIEW_COST` 对协作者也为 `True`，用它做写端点的权限
    等于"能看就能扣钱"，属权限混淆。

    ## 201 而不是 200

    它**创建**了一个资源（`budget_reservations` 的一行），
    并回带该资源的 `id` —— 后续的释放/结算都按这个 id 寻址。
    与 `files/api.py::create_upload_session`（同为 201）一致。

    ## 并发正确性不靠本文件（见模块 docstring 第六节）

    服务层走单条条件 UPDATE（`WHERE committed + reserved + delta <= limit`），
    `rowcount == 1` 才算成功。若只是 0，就抛 `BUDGET_EXCEEDED`
    —— **不再查一次余量**（再查读到的是别人改过的账面，反而误导）。

    ## `estimateId` 是可选的

    §2.4 第 6 项已定：预估是"打算花多少"、预占是"先扣住多少"，
    两者可以不同粒度 ⇒ **不强制**预占挂在预估上。
    """
    access.require(ProjectAction.EDIT)
    view = service.reserve_budget(
        repo,
        project_id=access.project_id,
        amount=body.amount,
        currency=body.currency,
        ref=body.ref,
        note=body.note,
        estimate_id=body.estimate_id,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    return single_response(ReservationBody.of(view))


# ---------------------------------------------------------------------------
# 四、预占：释放 / 结算（子资源路径）
# ---------------------------------------------------------------------------


@router.post(
    "/budget-reservations/{reservationId}/releases",
    response_model=DataEnvelope[ReservationBody],
    responses=BILLING_WRITE_RESPONSES,
    summary="释放一笔预占（重复调用不生效，也不报错）",
)
def release_reservation(
    user: CurrentUserDep,
    repo: BillingRepoDep,
    project_repo: ProjectRepoDep,
    reservation_id: Annotated[uuid.UUID, Path(alias="reservationId")],
    body: Annotated[
        ReleaseReservationBody,
        Body(description="可以为空对象 —— 释放是补偿动作，原因可留空。"),
    ] = ReleaseReservationBody(),
) -> dict[str, Any]:
    """**重复调用不生效，且不报错**（§1.6 R3 的处置要求）。

    实现靠状态机条件 UPDATE（`WHERE state IN <持有态>`）：
    第二次调用时 `rowcount` 为 0 ⇒ 服务层返回当前的终态视图，
    **而不是抛 409**。"释放"是幂等的补偿动作，
    而调用方（超时回收、失败路径）会重试它 ——
    报错会让一个正常的补偿流程看起来像故障。

    ## `body` **可选**：用默认值 `ReleaseReservationBody()`

    客户端可以完全不发 body。⚠️ 若这里写成必填，`POST .../releases`
    不带 body 会是 422 —— 那会让"超时回收自动释放"这类调用方
    **不得不编一个假原因**。

    ## 参数顺序：`reservation_id` 在 `body` 之前

    看起来可以反过来（让必填的路径参数排在最后），但那样**必须**给路径参数
    补一个 `= ...`，否则有默认值的 `body` 会排在无默认值的参数之前 ——
    Python 语法不允许。

    **`= ...` 正是必须避免的**：`Annotated[uuid.UUID, Path(alias=...)]`
    已经完整表达了"路径参数、必填"，再补 `= ...` 会让 FastAPI 把
    `Ellipsis` 当成一个显式的字段默认值塞进 `FieldInfo`，
    pydantic 2.9 随即尝试为它生成 JSON schema，失败后打出一条
    `PydanticJsonSchemaWarning: Default value Ellipsis is not JSON serializable`。
    契约仍能生成，但**每次导出都多一条噪声警告**（实测：本端点独占 2 条，
    其余 13 个端点 0 条）—— 与"证据日志必须干净"的取向不符。
    ⇒ 正确写法是路径参数**不写默认值**，靠调整参数顺序满足语法。

    ## 鉴权分两步（见模块 docstring 第二节）

    先由 `reservationId` 反查项目（**不存在 → 404**），再判 `EDIT`。
    顺序不可调换：权限判定需要资源的 `project_id`，而它只能从资源来。
    """
    project_id = _reservation_project_id(repo, reservation_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.EDIT)
    view = service.release_reservation(
        repo,
        reservation_id=reservation_id,
        reason=body.reason,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    return single_response(ReservationBody.of(view))


@router.post(
    "/budget-reservations/{reservationId}/settlements",
    response_model=DataEnvelope[ReservationBody],
    responses=BILLING_WRITE_RESPONSES,
    summary="结算一笔预占（预占转已结算，实际金额可以不等）",
)
def settle_reservation(
    user: CurrentUserDep,
    repo: BillingRepoDep,
    project_repo: ProjectRepoDep,
    body: SettleReservationBody,
    reservation_id: Annotated[uuid.UUID, Path(alias="reservationId")],
) -> dict[str, Any]:
    """预占 → 已结算，并把实际金额入账（`usage_ledger` 追加若干条）。

    ## 三个失败分支，码各不相同

    | 情形 | reason | HTTP |
    |---|---|---|
    | 预占不存在 | `RESERVATION_NOT_FOUND` | 404 |
    | 预占不在持有态（已释放/已结算） | `RESERVATION_NOT_HOLDING` | 409 |
    | 分录合计 ≠ `actualAmount` | `LEDGER_TOTAL_MISMATCH` | 422 |

    409 而不是 422：状态机不允许的迁移是**冲突**，不是格式错 ——
    前端该显示"这笔已经结过了"，并**不要重试**。

    ## `entries` 为空时自动补一条 `BILL`

    金额 = `actualAmount`，幂等键 `settle:<reservationId>`。
    这让"按账单结算"（只知道一个总数）不必编出一条明细。
    非空时**合计必须等于 `actualAmount`**（差一分都拒）——
    否则账面与账本会静默分叉（§1.6 R10）。

    ## 为什么**不需要** `If-Match`

    状态机条件 UPDATE 已经保证"同一次结算只生效一次"：
    第二次调用时 `state` 已不在持有态 ⇒ `rowcount` 为 0 ⇒ 409。
    ETag 只会多一个客户端可见的失败分支（§1.6 R1 的同一条理由）。
    """
    project_id = _reservation_project_id(repo, reservation_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.EDIT)
    view = service.settle_reservation(
        repo,
        reservation_id=reservation_id,
        actual_amount=body.actual_amount,
        entries=[_ledger_input(item) for item in body.entries],
        note=body.note,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    return single_response(ReservationBody.of(view))


# ---------------------------------------------------------------------------
# 五、账本：列 / 汇总 / 追加
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/ledger-entries",
    response_model=DataEnvelope[list[LedgerEntryResponseBody]],
    responses=BILLING_READ_RESPONSES,
    summary="列项目账本分录（按发生时间新→旧，可按起始时间筛选）",
)
def list_ledger_entries(
    access: ProjectAccessDep,
    repo: BillingRepoDep,
    since: Annotated[
        datetime | None,
        Query(description="只返回该时刻之后发生的分录（ISO 8601）。"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="最多返回条数。")] = 100,
) -> dict[str, Any]:
    """账本是 append-only 的（§1.6 R5）：这里只有读，没有改与删。

    ## `since` 用 `datetime` 类型而不是 `str`

    非法时间串由 FastAPI 拦成 422 `VALIDATION_FAILED`，
    而不是进到服务层再被 `parse` 抛异常（那会是 500）。
    与 `audit/api.py` 的 `moment` 用同一取向。

    ## 看不到 `truncated` 字段时怎么判断截断

    返回条数 == `limit` ⇒ 可能被截断。要看**带 `truncated` 的版本**
    用 `GET .../ledger-summary`（它回带该字段，且口径是"取满也算"）。
    """
    access.require(ProjectAction.VIEW_COST)
    rows = service.list_project_ledger(
        repo, project_id=access.project_id, since=since, limit=limit
    )
    return single_response([LedgerEntryResponseBody.of(row) for row in rows])


@router.get(
    "/projects/{projectId}/ledger-summary",
    response_model=DataEnvelope[LedgerSummaryBody],
    responses=BILLING_READ_RESPONSES,
    summary="项目账本汇总（按分录类型分组的小计 + 净额，带 truncated）",
)
def get_ledger_summary(
    access: ProjectAccessDep,
    repo: BillingRepoDep,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=100,
            description="参与统计的最大分录条数。`truncated=true` 时 net 不是全量净额。",
        ),
    ] = 100,
) -> dict[str, Any]:
    """**它存在的理由**：账本明细可能几百条，而管理者要回答的第一个问题是
    "这个项目花了多少、都是什么性质" —— 分组小计 + 净额一行即可。

    ## `net` **不过滤符号**

    冲正是负数分录（`NEGATIVE_LEDGER_KINDS`），净额就该变小。
    这正是 append-only 账本的好处（`repository.recompute_committed` 原文）：
    错的那笔永远看得见，而它对总额的影响是**减法**。

    ## `truncated` 的口径：**取满也算**

    与 `reconcile_pending` 一致（`len(rows) >= limit`）。
    宁多报不少报 —— 少报会让 `net` 看起来是个完整数字，
    而它其实只是前 100 条的小计。

    ## 只读一次明细

    `schema.ledger_summary_body` 内部调一次 `list_ledger_entries`，
    本端点**不**再读第二遍（两遍读之间账本可能被追加，
    于是 `entryCount` 与 `byKind` 来自不同时刻，而两者都不报错）。
    """
    access.require(ProjectAction.VIEW_COST)
    return single_response(
        ledger_summary_body(repo, project_id=access.project_id, limit=limit)
    )


@router.post(
    "/projects/{projectId}/ledger-entries",
    response_model=DataEnvelope[LedgerEntryResponseBody],
    status_code=status.HTTP_201_CREATED,
    responses=BILLING_WRITE_RESPONSES,
    summary="追加一条人工分录（仅 ADJUSTMENT / REVERSAL）",
)
def append_ledger_entry(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: BillingRepoDep,
    body: AppendLedgerEntryBody,
) -> dict[str, Any]:
    """人工分录：**调整**（ADJUSTMENT）与**冲正**（REVERSAL）。

    ## 为什么只有这两种（取值域在 `schema.py` 用 `Literal` 收窄）

    `USAGE` / `BILL` 由 `settle_reservation` 写 —— 它们绑定"某次 attempt 的实际用量"，
    而那个人工给不出可信的值。允许人工写 `USAGE` 等于允许伪造用量，
    而账面完全正常（金额自洽、幂等键自洽）。
    ⇒ 这不是"少实现一个功能"，而是**把伪造路径从协议形状上删掉**。

    ## 冲正用**新分录**，不改旧行（§1.6 R5）

    `usage_ledger` 是不可变表，`UPDATE` / `DELETE` 会被触发器拒成 `23001`。
    "这笔钱记错了"的表达是"再记一条负数分录并引用它"，
    于是错的那笔在账本里**永远看得见** —— 这正是 ADR-13
    "与费用账本同等级对待"的含义。

    ## 幂等靠 `idempotencyKey`（强烈建议给）

    给了就撞 `uq_usage_ledger_idempotency`，服务层把它当**正常的并发结果**
    （"这笔已经记过了"）并回查返回已有那条（**200 语义下的 201**：
    本端点固定回 201，因为"首次创建"与"已存在"对调用方是同一件事 ——
    都代表"这笔分录在账本里了"）。
    不给就真的会多记一笔。

    ## `sourceRef` **通常不该由客户端给**

    自动路径用 `attempt:<attemptId>`。写错前缀会让对账的反连接永远为真
    （每笔账被反复判为未结且不报错）。人工分录留空即可 ——
    `schema.py` 第 232~236 行已就此写过警告。
    """
    access.require(ProjectAction.EDIT)
    view = service.append_ledger_entry(
        repo,
        project_id=access.project_id,
        entry=_ledger_input(body),
        source_ref=body.source_ref,
        description=body.description,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    return single_response(LedgerEntryResponseBody.of(view))


# ---------------------------------------------------------------------------
# 六、预估：列 / 建
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/cost-estimates",
    response_model=DataEnvelope[list[CostEstimateBody]],
    responses=BILLING_READ_RESPONSES,
    summary="列项目费用预估（新→旧，可按状态筛选）",
)
def list_estimates(
    access: ProjectAccessDep,
    repo: BillingRepoDep,
    states: Annotated[
        list[str] | None,
        Query(description=f"状态筛选，可重复传。取值域 {sorted(ESTIMATE_STATES)}"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="最多返回条数。")] = 100,
) -> dict[str, Any]:
    """AC-26「估算」的读取出口（§1.3 F3）。

    `states` 与预占端点同样**不做白名单校验**（筛不到就是空列表）。

    ⚠️ 预估的 `state` 由 WP31 试产关口推进（`transition_estimate_state`），
    本包**不提供改状态的端点** —— 那是"试产关口"的业务动作，
    而本包只负责它要读的那个数（§1.7 "预估粒度是项目 × 批次类型"）。
    """
    access.require(ProjectAction.VIEW_COST)
    rows = service.list_project_estimates(
        repo, project_id=access.project_id, states=states, limit=limit
    )
    return single_response([CostEstimateBody.of(row) for row in rows])


@router.post(
    "/projects/{projectId}/cost-estimates",
    response_model=DataEnvelope[CostEstimateBody],
    status_code=status.HTTP_201_CREATED,
    responses=BILLING_WRITE_RESPONSES,
    summary="按供应商价目预估一次批次的费用（不预占）",
)
def create_estimate(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: BillingRepoDep,
    body: EstimateCostBody,
) -> dict[str, Any]:
    """**不预占**："打算花多少"与"先扣住多少"是两件事（§2.4 第 6 项）。

    ## 三个 422 分支（`service.estimate_cost` 分列）

    | 情形 | reason |
    |---|---|
    | WP24 的批量上限未配置 | `LIMITS_NOT_CONFIGURED` |
    | 数量越界（低于下界 / 高于上限） | `QUANTITY_BELOW_MIN` / `QUANTITY_ABOVE_MAX`（**由 planning 提供**） |
    | 价目缺该计量单位 / 无 `CALL` 单价 | `PRICING_INCOMPLETE` |

    数量口径**只有 WP24 一个真值来源** —— 本包不重写一份数量校验
    （那会让"10/30/50"这条 G3 硬约束出现第二个来源，而两个来源必然分叉）。

    ## 为什么这个 `POST` 会**写库**（已知代价）

    它插一行 `cost_estimates`，因此不是"安全的查询"。
    但它也**不是幂等的**：同一请求发两次会得到两份预估
    （各自有独立的 `id`）。这是有意的 —— 预估是历史记录，
    "什么时候、按什么价、谁估的"本身是信息（审计 `cost_estimate.create`）。
    ⇒ 本端点**不要求** `idempotencyKey`（与人工分录相反）。

    ## 必须在写事务里（`dependencies.py` 第 11 行）

    `get_effective_limits` 会**惰性物化项目副本**。若预估失败却不回滚，
    库里会留下一条孤立的副本 —— 本包的 `BillingRepoDep` 与端点 session
    同源，正是为了让两者在同一个事务里。
    """
    access.require(ProjectAction.EDIT)
    view = service.estimate_cost(
        repo,
        project_id=access.project_id,
        batch_kind=body.batch_kind,
        quantity=body.quantity,
        provider_id=body.provider_id,
        currency=body.currency,
        model_profile_id=body.model_profile_id,
        metrics=body.metrics,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    return single_response(CostEstimateBody.of(view))


@router.get(
    "/cost-estimates/{estimateId}",
    response_model=DataEnvelope[CostEstimateBody],
    responses=BILLING_READ_RESPONSES,
    summary="读一份费用预估（不存在返回 404）",
)
def get_estimate(
    user: CurrentUserDep,
    repo: BillingRepoDep,
    project_repo: ProjectRepoDep,
    estimate_id: Annotated[uuid.UUID, Path(alias="estimateId")],
) -> dict[str, Any]:
    """按**资源自身 id** 寻址，因此鉴权分两步（见模块 docstring 第二节）。

    ## 404 的两种来源**刻意同形**

    ① 预估不存在（服务层 `_not_found` + `ESTIMATE_NOT_FOUND`）；
    ② 预估存在但当前用户无权访问其项目（`resolve_project_access` 的 404）。

    两者都是 404 `RESOURCE_NOT_FOUND`，因此外部无法通过这个端点判断
    "某个 id 的预估是否存在"（协议 §1.2 防枚举）。
    ⚠️ 但 `details.reason` **不同**：① 带 `reason=ESTIMATE_NOT_FOUND`，
    ② 只带 `projectId`。这是**有意的**：reason 只在已通过鉴权后
    才有意义（②时用户已证明自己对该项目无权限，项目 id 本就是他给的）。

    ## 状态筛选过的行也能读

    即使预估已被 `CANCELLED`，这个端点也能读到它（不做状态过滤）——
    审计需要"看到被取消的那份当时估了多少"，而不是 404。
    """
    project_id = _estimate_project_id(repo, estimate_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.VIEW_COST)
    return single_response(CostEstimateBody.of(service.get_estimate(repo, estimate_id=estimate_id)))


# ---------------------------------------------------------------------------
# 七、价目版本：只读
# ---------------------------------------------------------------------------


@router.get(
    "/pricing-versions",
    response_model=DataEnvelope[list[PricingVersionBody]],
    responses=BILLING_READ_RESPONSES,
    summary="列供应商价目版本（新→旧，可按供应商与状态筛选）",
)
def list_pricing_versions(
    user: CurrentUserDep,
    repo: BillingRepoDep,
    provider_id: Annotated[
        uuid.UUID | None, Query(description="按供应商筛选。")
    ] = None,
    state: Annotated[
        str | None, Query(description=f"状态筛选。取值域 {sorted(PRICING_STATES)}")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="最多返回条数。")] = 100,
) -> dict[str, Any]:
    """**只读** —— 写路径属运维脚本（§1.5 砍掉清单：供应商管理端点被砍）。

    ## 为什么权限只要"登录"（不挂项目）

    价目版本是**全局**的（一个供应商一个价，不属于任何项目）。
    它挂在 `/pricing-versions` 而不是 `/projects/{projectId}/...` 下，
    正是这个原因 —— 挂到项目下会暗示"这是那个项目的价目"。

    价格透明是**有意的**取向：运营要能自己算出"这次大约花多少"，
    而藏起来会让他只能盲报。⚠️ 它**不含任何项目金额**
    （只有单价，没有用量），所以不违反 §1.4 第 5 条的
    `COST_LEDGER` 禁止出网约束（那条约束的是**账本数据**）。

    ## 解析失败的行会被跳过（服务层，`logger.warning`）

    `service.list_pricing_versions` 对 `unit_prices` 里无法解析的
    历史数据 `except ApiError` 跳过。因此返回条数可能**小于** `limit`
    而并非被截断 —— 这是**有意的**：一份坏数据不该让整个列表 500。
    已登记：坏数据应在运维脚本里修，不在这里兜。
    """
    del user
    rows = service.list_pricing_versions(
        repo, provider_id=provider_id, state=state, limit=limit
    )
    return single_response([PricingVersionBody.of(row) for row in rows])


# ---------------------------------------------------------------------------
# 八、对账：纯读报告
# ---------------------------------------------------------------------------


@router.post(
    "/billing/reconciliations",
    response_model=DataEnvelope[ReconciliationReportBody],
    responses=BILLING_READ_RESPONSES,
    summary="扫未结用量与账面分叉，返回对账报告（纯读，不写库）",
)
def run_reconciliation(
    user: CurrentUserDep,
    repo: BillingRepoDep,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_RECONCILIATION_LIMIT,
            description="扫描的最大 attempt 条数。取满即判 truncated。",
        ),
    ] = DEFAULT_RECONCILIATION_LIMIT,
    project_id: Annotated[
        uuid.UUID | None,
        Query(description="只扫某个项目。不传则扫全平台。"),
    ] = None,
    include_drifts: Annotated[
        bool,
        Query(description="是否同时跑账面分叉判据（R10）。false 时 drifts 为空且 projectsChecked=0。"),
    ] = True,
) -> dict[str, Any]:
    """AC-26「重试核对」的落点（§1.3 F6），闭合 OP-108 / OP-132。

    ## 它是**纯读**：不结算、不挂账

    只做两件事：扫 `usage_pending=true` 的 attempt、
    查它们有没有确认性账本分录（`CONFIRMING_LEDGER_KINDS`）。
    "把未结的自动结掉"是 **WP43 定时任务**的事（§1.7 的消费者列）。

    因此它用 `POST`（筛选条件多、且语义不是"取一个资源"）
    但**只要求登录** —— 与用户是否属于某项目无关（它是跨项目的运维视图）。
    ⚠️ 若将来改成"顺手结掉"，那就要补 `EDIT` 类权限判定 ——
    这条注释就是那个改造的入口。

    ## 对账判据与上游**同源**（§1.4 第 3 条）

    `usage_pending` 有四个置位点，全在上游（`jobs/repository.py:288`、
    `jobs/service.py:385 / :476 / :505`、`gateway/handlers.py:90`）。
    本包**一行都不改它们**，判据的权威定义是
    `gateway/handlers.py::_usage_pending`。
    本端点消费的是 `job_attempts.usage_pending` **列本身**
    （上游已按那个判据置好的值），因此不构成第二真值来源。

    ## `includeDrifts=false` 的语义（不能当健康证明）

    传 `false` 时 `drifts=()` 且 `projectsChecked=0` ——
    **两者都是"没查"而不是"查了没问题"**。
    `projectsChecked` 字段存在的理由就是让这两种情况可区分
    （`reconciliation.reconcile_pending` docstring 原文）。

    ## 恒等式 `scanned == resolved + pendingCount`

    三个数同源（遍历同一批行现场分出），因此必须成立。
    `ReconciliationIdentityBody` 是它的**显式回执** ——
    这里**不**返回它（本端点的响应已经是完整报告），
    它供"只想断言等式"的调用方用（见 `schema.py` 第 506~517 行的说明）。
    """
    del user
    report = reconciliation.reconcile_pending(
        repo, limit=limit, project_id=project_id, include_drifts=include_drifts
    )
    return single_response(ReconciliationReportBody.of(report))


# ---------------------------------------------------------------------------
# 内部帮助
# ---------------------------------------------------------------------------


def _ledger_input(body: LedgerEntryBody | AppendLedgerEntryBody) -> LedgerEntryInput:
    """请求体 → `LedgerEntryInput`（**端点层的形状翻译**）。

    ## 为什么解析必须发生，且只能发生在 `money`

    `LedgerEntryInput.amount` 的类型是 `Decimal`，而请求体给的是 `str`
    （`schema.py` 第 16~27 行：金额在 HTTP 上**一律字符串**）。
    `LedgerEntryInput.__post_init__` 会调 `money.is_positive` /
    `is_negative`，而它们只接受 `Decimal`（内部先过 `money.quantize`）
    —— 传 `str` 其实也能过，但那样**每个字段的解析口径就散了**：
    有人传 `"1e5"` 会在 `is_positive` 里才炸，且炸出来的位置
    与 `service._as_amount` 不是同一处。

    因此这里显式走 `money.parse_wire_*`（**全仓唯一的解析入口**，
    `money.py` 第 261 / 308 行）。口径与 `service._as_amount` 完全同源
    （`_as_amount` 内部就是 `money.quantize`，而 `quantize` 收 `str`
    时会走同一个 `_parse_text`），所以**不构成第二真值来源**。

    ## `MoneyError` 必须就地翻成 422（否则是 500）

    `money.MoneyError` 是 `ValueError` 的子类，**刻意不继承 `ApiError`**
    （纯函数层不认识 HTTP）。`platform/errors.py` 的兜底处理器
    （第 476 行 `@app.exception_handler(Exception)`）会把它变成
    **500 INTERNAL_ERROR** —— 而"金额写法非法"本该是 422。
    这个坑 `schema.py` 第 26~27 行已经写明，本函数是它的落点。

    ## `quantity` 与 `unit` 成对

    `parse_quantity_wire` 对空串返回 `None`（与 `parse_wire_optional` 同口径）。
    `LedgerEntryInput.__post_init__` 会拒"只给一个"的情况
    （`usage_ledger.quantity_unit_paired` CHECK 的同口径早失败）。
    """
    try:
        amount = money.parse_wire_optional(body.amount, label="分录金额")
        quantity = (
            None if body.quantity is None else money.parse_quantity_wire(body.quantity)
        )
        return LedgerEntryInput(
            entry_kind=body.entry_kind,
            # `parse_wire_optional` 对空白串返回 None，而 schema 的 min_length=1
            # 只挡得住空串（`"   "` 是 3 个字符，能过 schema）。
            # 不能把 None 塞进 amount（类型是 Decimal）⇒ 显式报 422。
            amount=_require_amount(amount),
            quantity=quantity,
            unit=body.unit,
            reverses_ledger_id=body.reverses_ledger_id,
            idempotency_key=getattr(body, "idempotency_key", None),
        )
    except money.MoneyError as exc:
        # 解析失败（非法形态 / 超长）与结构校验失败（符号不符、冲正缺目标）
        # 都收敛成同一个 422：它们是"这条分录不合法"这一件事的不同说法，
        # 对客户端而言处理方式相同（改请求体重发）。
        raise _validation_error(
            "entries", str(exc), reason=_REASON_LEDGER_ENTRY_INVALID
        ) from exc


def _require_amount(amount: Any) -> Any:
    """`parse_wire_optional` 可能回 `None`（空白串），而 `amount` 不接受它。"""
    if amount is None:
        raise _validation_error(
            "amount",
            "分录金额不能是空白字符串 —— 空值请省略该字段。",
            reason=_REASON_LEDGER_ENTRY_INVALID,
        )
    return amount


__all__ = [
    "BILLING_READ_RESPONSES",
    "BILLING_WRITE_RESPONSES",
    "BUDGET_EXCEEDED_RESPONSES",
    "BUDGET_WRITE",
    "DEFAULT_RECONCILIATION_LIMIT",
    "MAX_RECONCILIATION_LIMIT",
    "router",
]
