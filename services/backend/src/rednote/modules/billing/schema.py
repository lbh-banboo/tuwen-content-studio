"""WP25 的请求/响应 DTO。

**本模块的全部意义是一件事**：把 `types.*View` / `reconciliation.*Report`
这些跨层值对象翻成协议形状，并保证**金额在 HTTP 上的表示唯一**。

---

## 一、★ 金额一律字符串（§2.4 第 7 项）

`Numeric(20,8)` 在 Python 侧是 `Decimal`，而 JSON **没有定点数**：
`0.1` 在 JSON 里就是 IEEE 754 双精度，`0.1 + 0.2 != 0.3`。
若把 `Decimal` 直接交给 FastAPI 的默认编码器，它会被序列化成 float ——
从此"账面上的 100.00000000"与"前端眼里的 100"是两件事，
而**两边都不报错**。

因此本模块**没有任何 `Decimal` 字段**：

| 层 | 表示 |
|---|---|
| 数据库 | `Numeric(20,8)` / `Numeric(20,10)`（任意精度定点） |
| Python 服务层 | `Decimal`，唯一入口 `money.py` |
| **HTTP** | **定长小数字符串**（`money.to_wire`，8 位 / 10 位） |

反面写法（本模块刻意不采用）：`amount: float`、`amount: Decimal`（会被
pydantic 渲成 `number`，前端拿到的就是双精度）、`amount: str` 但**不校验**
格式（`"1e5"` / `"1,000"` / `" 12 "` 都能进服务层，由 `money` 抛
`MoneyError` → 500，而它本该是 422）。

## 二、入参侧：先过 pydantic，再过 `money`

请求体里的金额是 `str`，本模块**只做"非空 + 长度"这一级的粗筛**，
真正的解析口径在 `money.parse_wire_*`（唯一入口）。
为什么不在 DTO 用 `constr(pattern=...)`：正则与 `Decimal` 的容错边界
必然会分叉（`money.parse_wire_optional` 的容错表是**契约本体**，
见其 docstring 的六行表格），两处各写一遍就等于有了两个真值来源。
DTO 放行、服务层判定 ⇒ 错误统一是 `VALIDATION_FAILED` + `fieldErrors`，
前端拿到的形状一致。

## 三、响应侧：`Field(serialization_alias=...)`

与 `planning/schema.py` 同一手法（`_DTO_CONFIG`）：
内部用 snake_case 构造，对外是 camelCase。
**`extra="forbid"`** 是必须的 —— 不做的话前端把 `actualAmount` 拼成
`actualamount` 会得到"预占成功但金额没生效"。

## 四、`of()` 类方法全部**显式逐字段赋值**

与 `planning/schema.py` 的第四节同一条理由：`model_validate(obj,
from_attributes=True)` 按**属性名**取值，而本模块字段名是 snake_case、
响应键是 camelCase 别名 —— 一旦某个字段改名，`from_attributes` 会
**静默取到默认值**（`None`），响应里那个字段就消失了，且没有任何错误。
显式赋值让这种不一致在**导入时**就报 `AttributeError`。
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from rednote.modules.billing import money
from rednote.modules.billing.reconciliation import (
    CONFIRMING_LEDGER_KINDS,
    TABLE_BUDGET_COMMITTED,
    TABLE_BUDGET_RESERVED,
)
from rednote.modules.billing.repository import BillingRepository
from rednote.modules.billing.types import (
    CURRENCIES,
    LEDGER_KINDS,
    MAX_RESERVATION_REF_CHARS,
    DriftReport,
    LedgerEntryView,
    PendingUsage,
    PricingVersionView,
    ReconciliationReport,
    ReservationView,
)

# 所有 DTO 的公共配置（照抄 `planning/schema.py` 的 `_DTO_CONFIG`）：
#   · `populate_by_name` —— 内部可用 snake_case 构造，输出仍是 camelCase
#   · `extra="forbid"` —— 多传字段直接 422，而不是静默忽略
_DTO_CONFIG = ConfigDict(populate_by_name=True, extra="forbid")

#: 金额字符串的**粗筛**上限。`Numeric(20,8)` 的 20 位总精度 ⇒ 整数部分
#: 最多 12 位，加上小数点与 8 位小数、负号，24 是够的；
#: 给到 40 是为了让"明显离谱的输入"在 DTO 层就被拦下，
#: 而**不**试图在这里精确判精度（那是 `money` 的职责，见模块 docstring 第二节）。
_AMOUNT_MAX_CHARS = 40

#: `description` 的上限与 `types.MAX_LEDGER_DESCRIPTION_CHARS` 同值。
#: 刻意**再写一遍**而不是 import：DTO 层是"协议形状"，服务层是"业务规则"，
#: 两者可以不同（若某天协议把 description 放宽到 1000，服务层仍守 500 ——
#: 那时 DTO 放行、服务层 422，是正确的层次关系）。
_DESCRIPTION_MAX_CHARS = 500


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class SetBudgetBody(BaseModel):
    """`PUT /projects/{projectId}/budget` 的请求体。"""

    model_config = _DTO_CONFIG

    limit_amount: str = Field(
        min_length=1,
        max_length=_AMOUNT_MAX_CHARS,
        alias="limitAmount",
        description="预算上限（定长 8 位小数的字符串，如 \"1000.00000000\"）。",
    )
    currency: str = Field(
        default="CNY",
        description=f"币种，取值域 {sorted(CURRENCIES)}。已有金额时不允许更换。",
    )


class ReserveBudgetBody(BaseModel):
    """`POST /projects/{projectId}/budget-reservations` 的请求体。"""

    model_config = _DTO_CONFIG

    amount: str = Field(
        min_length=1,
        max_length=_AMOUNT_MAX_CHARS,
        description="预占金额（必须为正）。",
    )
    currency: str = Field(default="CNY", description=f"币种，取值域 {sorted(CURRENCIES)}。")
    ref: str | None = Field(
        default=None,
        max_length=MAX_RESERVATION_REF_CHARS,
        description="业务引用（如批次号），便于人肉追溯这笔钱属于谁。",
    )
    note: str | None = Field(default=None, max_length=500, description="备注。")
    estimate_id: uuid.UUID | None = Field(
        default=None,
        alias="estimateId",
        description=(
            "可选：这笔预占对应哪份预估。**不是必填** —— 预估是"
            "「打算花多少」，预占是「先扣住多少」，两者可以不同粒度（§2.4 第 6 项）。"
        ),
    )


class ReleaseReservationBody(BaseModel):
    """`POST /budget-reservations/{reservationId}/releases` 的请求体。

    **可以完全为空**：释放是补偿动作，原因可留空。
    body 做成可选（见 `api.py`）而不是必填，是为了让"超时回收自动释放"
    这类调用方不必编一个假原因。
    """

    model_config = _DTO_CONFIG

    reason: str | None = Field(default=None, max_length=500, description="释放原因。")


class LedgerEntryBody(BaseModel):
    """结算请求里的一条账本分录（`LedgerEntryInput` 的协议形态）。"""

    model_config = _DTO_CONFIG

    entry_kind: str = Field(
        alias="entryKind",
        description=(
            f"分录类型，取值域 {sorted(LEDGER_KINDS)}。"
            "`REVERSAL` 必须为负且必须给出 reversesLedgerId。"
        ),
    )
    amount: str = Field(min_length=1, max_length=_AMOUNT_MAX_CHARS, description="金额。")
    quantity: str | None = Field(
        default=None, max_length=_AMOUNT_MAX_CHARS, description="用量（可选）。"
    )
    unit: str | None = Field(default=None, max_length=20, description="计量单位（可选）。")
    reverses_ledger_id: uuid.UUID | None = Field(
        default=None,
        alias="reversesLedgerId",
        description="冲正的目标分录 id（仅 `REVERSAL` 可给）。",
    )


class SettleReservationBody(BaseModel):
    """`POST /budget-reservations/{reservationId}/settlements` 的请求体。"""

    model_config = _DTO_CONFIG

    actual_amount: str = Field(
        min_length=1,
        max_length=_AMOUNT_MAX_CHARS,
        alias="actualAmount",
        description="实际发生金额（可以与预占金额不等）。",
    )
    entries: list[LedgerEntryBody] = Field(
        default_factory=list,
        description=(
            "本次结算要写入账本的分录。**为空时自动补一条 `BILL`**"
            "（金额 = actualAmount，幂等键 `settle:<reservationId>`）。"
            "非空时**合计必须等于 actualAmount**，否则 422 `LEDGER_TOTAL_MISMATCH`。"
        ),
    )
    note: str | None = Field(default=None, max_length=500, description="备注。")


class AppendLedgerEntryBody(BaseModel):
    """`POST /projects/{projectId}/ledger-entries` 的请求体（人工分录）。

    只允许 `ADJUSTMENT` / `REVERSAL` 两种 —— 自动路径（`USAGE` / `BILL`）
    由 `settle_reservation` 写，不接受人工伪造。
    取值域在这里**收窄**（`Literal`）而不是靠服务层拒：
    这一步是"协议形状"，让 OpenAPI 里直接显示两个合法值。
    """

    model_config = _DTO_CONFIG

    entry_kind: Literal["ADJUSTMENT", "REVERSAL"] = Field(
        alias="entryKind", description="分录类型：ADJUSTMENT 或 REVERSAL。"
    )
    amount: str = Field(min_length=1, max_length=_AMOUNT_MAX_CHARS, description="金额。")
    quantity: str | None = Field(default=None, max_length=_AMOUNT_MAX_CHARS)
    unit: str | None = Field(default=None, max_length=20)
    reverses_ledger_id: uuid.UUID | None = Field(default=None, alias="reversesLedgerId")
    description: str | None = Field(
        default=None, max_length=_DESCRIPTION_MAX_CHARS, description="这条分录的说明。"
    )
    source_ref: str | None = Field(
        default=None,
        alias="sourceRef",
        max_length=200,
        description=(
            "来源引用。**通常不该由客户端给** —— 自动路径用的是"
            " `attempt:<attemptId>`，写错前缀会让对账的反连接永远为真"
            "（每笔账被反复判为未结且不报错）。人工分录留空即可。"
        ),
    )
    idempotency_key: str | None = Field(
        default=None,
        alias="idempotencyKey",
        max_length=200,
        description=(
            "幂等键。**强烈建议给**：重复提交同一笔人工分录时，"
            "有它会被识别成「已经记过了」并返回已有那条（200），"
            "没有它就会真的多记一笔。"
        ),
    )


class EstimateCostBody(BaseModel):
    """`POST /projects/{projectId}/cost-estimates` 的请求体。"""

    # protected_namespaces=()：Pydantic v2 把 `model_` 前缀视为受保护
    # 命名空间（与 model_dump / model_config 冲突），而本类字段
    # `model_profile_id` 是**业务术语**（模型档案，上游 `gateway` 同名字段），
    # 不是框架概念。与 `projects/dictionaries.py` 的 `ProductBody.model_code`
    # 同款处置：业务术语优先，显式关闭保护，**不**在同一处放宽全局。
    model_config = ConfigDict(
        populate_by_name=True, extra="forbid", protected_namespaces=()
    )

    batch_kind: str = Field(
        alias="batchKind", description="批次类型（TRIAL / SCALE / BULK），上限读 WP24 的生效配置。"
    )
    quantity: int = Field(description="数量。越界返回 422，`details.reason` 说明是哪一侧越界。")
    provider_id: uuid.UUID = Field(
        alias="providerId",
        description=(
            "按哪个供应商的价目估算。**必填** —— `cost_estimates` 表没有"
            " provider 列，本包也不设「默认供应商」（默认值会在多供应商时"
            "静默用错单价，而账面完全正常）。"
        ),
    )
    model_profile_id: uuid.UUID | None = Field(
        default=None,
        alias="modelProfileId",
        description="可选：模型档案。给了就优先用模型级价目，没有则回落到供应商级。",
    )
    currency: str = Field(default="CNY", description=f"币种，取值域 {sorted(CURRENCIES)}。")
    metrics: dict[str, str] | None = Field(
        default=None,
        description=(
            "各计量单位的预计用量（如 {\"input_tokens\": \"120000\"}）。"
            "不给时只能按次（`CALL`）计价；价目里没有 `CALL` 单价时 422 "
            "`PRICING_INCOMPLETE`（**不按 0 算**）。"
        ),
    )


# ---------------------------------------------------------------------------
# 响应体
# ---------------------------------------------------------------------------


class BudgetBody(BaseModel):
    """`BudgetView` 的协议形态。**四个金额一起返回**。

    只给 `available` 就是"悄悄算错也没人知道" —— 调用方拿不到 `limit`
    与 `reserved` 就无法发现 `available` 是不是真的等于
    `limit - committed - reserved`（`BudgetView` docstring 的第一条理由）。
    """

    model_config = _DTO_CONFIG

    project_id: uuid.UUID = Field(alias="projectId")
    currency: str
    limit: str
    committed: str
    reserved: str
    available: str
    configured: bool = Field(
        description="账面是否已存在。`False` 表示该项目从未设过预算（金额全为 0）。"
    )

    @classmethod
    def of(cls, view: Any) -> BudgetBody:
        """直接取 `BudgetView.as_dict()` —— **转换点只有一处**。

        `BudgetView.as_dict()` 已经做了 `money.to_wire`（三个 `types` 视图
        都自带 `as_dict`），本模块**不重写一遍金额格式化**：
        重写会让"HTTP 上的金额形态"出现第二个真值来源，而它必然与
        `money.to_wire` 分叉（比如忘了补零到 8 位）。
        """
        return cls(**view.as_dict())


class ReservationBody(BaseModel):
    """`ReservationView` 的协议形态。"""

    model_config = _DTO_CONFIG

    id: uuid.UUID
    project_id: uuid.UUID = Field(alias="projectId")
    state: str
    currency: str
    amount: str
    settled_amount: str | None = Field(default=None, alias="settledAmount")
    ref: str | None = None
    note: str | None = None
    estimate_id: uuid.UUID | None = Field(default=None, alias="estimateId")

    @classmethod
    def of(cls, view: ReservationView) -> ReservationBody:
        return cls(**view.as_dict())


class LedgerEntryResponseBody(BaseModel):
    """`LedgerEntryView` 的协议形态。"""

    model_config = _DTO_CONFIG

    id: uuid.UUID
    project_id: uuid.UUID = Field(alias="projectId")
    entry_kind: str = Field(alias="entryKind")
    currency: str
    amount: str
    quantity: str | None = None
    unit: str | None = None
    description: str | None = None

    @classmethod
    def of(cls, view: LedgerEntryView) -> LedgerEntryResponseBody:
        return cls(**view.as_dict())


class EstimateLineBody(BaseModel):
    """预估的一行。**数量、单价、金额三项一起给**（可复核）。"""

    model_config = _DTO_CONFIG

    unit: str
    quantity: str
    unit_price: str = Field(alias="unitPrice")
    amount: str

    @classmethod
    def of(cls, view: Any) -> EstimateLineBody:
        return cls(**view.as_dict())


class CostEstimateBody(BaseModel):
    """`CostEstimateView` 的协议形态。"""

    model_config = _DTO_CONFIG

    id: uuid.UUID
    project_id: uuid.UUID = Field(alias="projectId")
    batch_kind: str = Field(alias="batchKind")
    quantity: int
    currency: str
    state: str
    total_amount: str = Field(alias="totalAmount")
    actual_amount: str | None = Field(default=None, alias="actualAmount")
    variance_amount: str | None = Field(default=None, alias="varianceAmount")
    pricing_version_id: uuid.UUID | None = Field(default=None, alias="pricingVersionId")
    lines: list[EstimateLineBody] = Field(default_factory=list)

    @classmethod
    def of(cls, view: Any) -> CostEstimateBody:
        return cls(**view.as_dict())


class PricingVersionBody(BaseModel):
    """价目版本的协议形态。**只读** —— 写路径属运维脚本（§1.5 砍掉清单）。"""

    # 同上：`model_profile_id` 是业务字段名，关掉 `model_` 保护。
    model_config = ConfigDict(
        populate_by_name=True, extra="forbid", protected_namespaces=()
    )

    id: uuid.UUID
    provider_id: uuid.UUID = Field(alias="providerId")
    model_profile_id: uuid.UUID | None = Field(default=None, alias="modelProfileId")
    state: str
    currency: str
    label: str
    effective_from: str = Field(alias="effectiveFrom")
    lines: list[dict[str, str]] = Field(default_factory=list)

    @classmethod
    def of(cls, view: PricingVersionView) -> PricingVersionBody:
        return cls(**view.as_dict())


class PendingUsageBody(BaseModel):
    """一条未结用量（`PendingUsage` 的协议形态）。

    `available=False` 时 `estimatedAmount` 为 `null` ——
    **刻意不补 0**：`null` 是"还不知道金额"，`"0.00000000"` 是"金额是 0"，
    两者混起来会让账面把"未结"读成"没花钱"。
    """

    model_config = _DTO_CONFIG

    attempt_id: uuid.UUID = Field(alias="attemptId")
    job_id: uuid.UUID = Field(alias="jobId")
    project_id: uuid.UUID | None = Field(default=None, alias="projectId")
    attempt_no: int = Field(alias="attemptNo")
    state: str
    available: bool
    estimated_amount: str | None = Field(default=None, alias="estimatedAmount")
    ledger_entry_id: uuid.UUID | None = Field(default=None, alias="ledgerEntryId")
    note: str | None = None

    @classmethod
    def of(cls, item: PendingUsage) -> PendingUsageBody:
        return cls(**item.as_dict())


class DriftBody(BaseModel):
    """一次"账面 vs 重算"的对比（R10）。"""

    model_config = _DTO_CONFIG

    project_id: uuid.UUID = Field(alias="projectId")
    table: str
    account_value: str = Field(alias="accountValue")
    recomputed_value: str = Field(alias="recomputedValue")
    drift: str
    healthy: bool = Field(
        description=(
            "`drift == 0`。**没有容差**：两边用同一个 `money.sum_amounts` "
            "语义算出来，不等就是真的不等。"
        )
    )

    @classmethod
    def of(cls, item: DriftReport) -> DriftBody:
        return cls(**item.as_dict())


class ReconciliationReportBody(BaseModel):
    """对账报告（`ReconciliationReport` 的协议形态）。"""

    model_config = _DTO_CONFIG

    scanned: int = Field(
        description="本次实际取回的 `usage_pending` attempt 条数。"
    )
    resolved: int = Field(description="其中已结清（账本里有确认性分录）的条数。")
    pending: list[PendingUsageBody] = Field(default_factory=list)
    pending_count: int = Field(alias="pendingCount")
    drifts: list[DriftBody] = Field(default_factory=list)
    project_id: uuid.UUID | None = Field(default=None, alias="projectId")
    truncated: bool = Field(
        description=(
            "是否因 `limit` 被截断（取满也算）。`true` 时 **不能**把本报告"
            "当成「全平台已结清」——请调大 limit 或按项目分片重跑。"
        )
    )
    projects_checked: int = Field(
        alias="projectsChecked",
        description=(
            "`drift` 那半边实际检查过的项目数。`drifts` 只装不一致的条目，"
            "所以「查了 30 个项目全一致」与「一个都没查」在 `drifts` 上同形 —— "
            "这个字段让两者可区分。"
        ),
    )
    healthy: bool

    @classmethod
    def of(cls, report: ReconciliationReport) -> ReconciliationReportBody:
        return cls(**report.as_dict())


class ReconciliationIdentityBody(BaseModel):
    """★ **恒等式的显式回执**，供前端与运维脚本直接断言。

    `scanned == resolved + pendingCount` 是 `reconcile_pending` 的实现
    必须满足的性质（三数同源）。把它**显式回给客户端**的理由是：
    运维脚本要在"扫描结果被截断、或者判据实现被改坏"时立刻发现，
    而自己去算这个等式需要它自己知道 `pendingCount` 是怎么数出来的。

    **它是回执而不是判据本体** —— 判据本体在
    `tests/test_billing_wp25.py` 的对账用例里（对同一份报告断言等式），
    本字段只是让调用方不必重新实现一遍减法。
    """

    model_config = _DTO_CONFIG

    scanned: int
    resolved: int
    pending_count: int = Field(alias="pendingCount")
    holds: bool = Field(description="`scanned == resolved + pendingCount` 是否成立。")

    @classmethod
    def of(cls, report: ReconciliationReport) -> ReconciliationIdentityBody:
        return cls(
            scanned=report.scanned,
            resolved=report.resolved,
            pending_count=report.pending_count,
            holds=report.scanned == report.resolved + report.pending_count,
        )


class LedgerSummaryBody(BaseModel):
    """一个项目的账本汇总（按分录类型分组的小计）。

    **为什么要有它**：账本明细可能几百条，而管理者要回答的第一个问题是
    "这个项目花了多少、都是什么性质"。分组小计 + 净额一行即可回答，
    不必把明细全拉下来。

    `net` **不过滤符号**：冲正是负数分录，净额就该变小 ——
    这正是 append-only 账本的好处（`repository.recompute_committed` 原文）。
    """

    model_config = _DTO_CONFIG

    project_id: uuid.UUID = Field(alias="projectId")
    by_kind: dict[str, str] = Field(
        alias="byKind",
        description=f"各分录类型的小计（键取值域 {sorted(LEDGER_KINDS)}，缺失的种类不出现）。",
    )
    net: str = Field(description="净额 = 全部分录金额之和（含负数冲正）。")
    entry_count: int = Field(alias="entryCount", description="参与统计的分录条数。")
    truncated: bool = Field(
        description="明细是否因 limit 被截断。`true` 时 `net` **不是**全量净额。"
    )


# ---------------------------------------------------------------------------
# 组装帮助（从仓储读 → 汇总视图）
# ---------------------------------------------------------------------------


def ledger_summary_body(
    repo: BillingRepository, *, project_id: uuid.UUID, limit: int
) -> LedgerSummaryBody:
    """按分录类型汇总账本。**只读一次明细**（`list_ledger_entries`）。

    `truncated` 的口径与 `reconcile_pending` 一致（`len(rows) >= limit`
    即判截断，取满也算）：宁多报不少报 —— 少报会让"这个项目的净额"
    看起来是个完整数字，而它其实只是前 100 条的小计。
    """
    rows = repo.list_ledger_entries(project_id=project_id, limit=limit)
    totals: dict[str, Any] = {kind: money.ZERO for kind in sorted(LEDGER_KINDS)}
    amounts = []
    for row in rows:
        amount = money.quantize(row.amount)
        amounts.append(amount)
        # `entry_kind` 的取值域由 CHECK 保证，这里的 `if` 只为防御
        # "将来加了新种类但忘了在本表登记"——那种情况下宁可漏计一类，
        # 也不要让它静默进到别的桶里。
        if row.entry_kind in totals:
            totals[row.entry_kind] = money.add(totals[row.entry_kind], amount)
    return LedgerSummaryBody(
        project_id=project_id,
        by_kind={
            kind: money.to_wire(total)
            for kind, total in totals.items()
            if not money.is_zero(total)
        },
        net=money.to_wire(money.sum_amounts(amounts) if amounts else money.ZERO),
        entry_count=len(rows),
        truncated=len(rows) >= limit,
    )


#: 供 `api.py` 的对账端点复用（避免在端点里 import reconciliation 的常量）。
RECONCILIATION_TABLE_BUDGET_RESERVED = TABLE_BUDGET_RESERVED
RECONCILIATION_TABLE_BUDGET_COMMITTED = TABLE_BUDGET_COMMITTED
RECONCILIATION_CONFIRMING_KINDS = CONFIRMING_LEDGER_KINDS
#: 供契约测试与运维脚本断言"两条判据都跑了"。
RECONCILIATION_TABLES: tuple[str, ...] = (
    TABLE_BUDGET_RESERVED,
    TABLE_BUDGET_COMMITTED,
)


__all__ = [
    "AppendLedgerEntryBody",
    "BudgetBody",
    "CostEstimateBody",
    "DriftBody",
    "EstimateCostBody",
    "EstimateLineBody",
    "LedgerEntryBody",
    "LedgerEntryResponseBody",
    "LedgerSummaryBody",
    "PendingUsageBody",
    "PricingVersionBody",
    "RECONCILIATION_CONFIRMING_KINDS",
    "RECONCILIATION_TABLES",
    "RECONCILIATION_TABLE_BUDGET_COMMITTED",
    "RECONCILIATION_TABLE_BUDGET_RESERVED",
    "ReconciliationIdentityBody",
    "ReconciliationReportBody",
    "ReleaseReservationBody",
    "ReservationBody",
    "ReserveBudgetBody",
    "SetBudgetBody",
    "SettleReservationBody",
    "ledger_summary_body",
]
