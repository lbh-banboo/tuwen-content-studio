"""WP25 五张表的受控取值域与传值类型。

## 取值域为什么必须闭集

与 WP24 的 `types.py` 同一立场：**拼错的常量会静默成为新的合法取值**，
而读取方按正确拼写去查 ⇒ "改了不生效"且不报错。
本包的取值域尤其不能开：`entry_kind` 若可自由取值，
"按用量记"与"按账单记"的混淆**在账面表现为金额相同** —— 对账永远发现不了。

每个取值域配一个 `*_LIST_SQL`（`sorted()` 派生），供 `models.py` 的
`CheckConstraint` 消费。**`*_LIST_SQL` 只在有 DDL 消费者时才定义** ——
凭空造一个没有消费者的 SQL 片段会让下一个人以为库里有那一列
（与 WP24 `types.py` §112 的同一判断）。

## 金额与用量：本模块不定义数值口径

数值口径全部在 :mod:`rednote.modules.billing.money`。本模块只定义
取值域与传值结构 —— 这样"金额精度"这件事全仓只有一个地方回答。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

from rednote.modules.billing import money

# ---------------------------------------------------------------------------
# 一、计价版本：计量单位（§2.4 第 4 项 —— 本包自行定义）
# ---------------------------------------------------------------------------

#: 按 token 计（文本 / 结构化输出）。
PRICING_UNIT_TOKEN: Final[str] = "TOKEN"
#: 按张计（静态图）。
PRICING_UNIT_IMAGE: Final[str] = "IMAGE"
#: 按秒计（视频 / Live 动态）。
PRICING_UNIT_SECOND: Final[str] = "SECOND"
#: 按次计（审核、重排这类一次调用一个价的）。
PRICING_UNIT_CALL: Final[str] = "CALL"

PRICING_UNITS: Final[frozenset[str]] = frozenset(
    {
        PRICING_UNIT_TOKEN,
        PRICING_UNIT_IMAGE,
        PRICING_UNIT_SECOND,
        PRICING_UNIT_CALL,
    }
)

PRICING_UNIT_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(PRICING_UNITS)
)

#: 币种取值域。**只做存储校验，不做换算**。
#:
#: 自动汇率需要外部汇率源 ⇒ **那是一条新的出网通道**，而 `COST_LEDGER`
#: 明确在 `FORBIDDEN_EGRESS_DATA_CLASSES` 里（`gateway/types.py:105`，
#: ADR-10 E-5）。因此本包只支持多币种**并存**，跨币种汇总属 WP43 报表。
CURRENCY_CNY: Final[str] = "CNY"
CURRENCY_USD: Final[str] = "USD"

CURRENCIES: Final[frozenset[str]] = frozenset({CURRENCY_CNY, CURRENCY_USD})

CURRENCY_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(CURRENCIES)
)

#: 计价版本状态的取值域存在的原因：**"草拟中"的价格不该被预估引用**。
#:
#: 若没有状态，运营录入一半的单价会在下一个请求里立刻生效，
#: 而预估会用一个只填了 `TOKEN` 单价、`IMAGE` 单价还是 0 的版本。
PRICING_STATE_DRAFT: Final[str] = "DRAFT"
PRICING_STATE_PUBLISHED: Final[str] = "PUBLISHED"
PRICING_STATE_RETIRED: Final[str] = "RETIRED"

PRICING_STATES: Final[frozenset[str]] = frozenset(
    {PRICING_STATE_DRAFT, PRICING_STATE_PUBLISHED, PRICING_STATE_RETIRED}
)

PRICING_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(PRICING_STATES)
)

#: 只有 `PUBLISHED` 可被预估引用。
USABLE_PRICING_STATES: Final[frozenset[str]] = frozenset({PRICING_STATE_PUBLISHED})

MAX_UNIT_PRICE_CHARS: Final[int] = 40
MAX_PRICING_NOTE_CHARS: Final[int] = 500
MAX_PRICING_LABEL_CHARS: Final[int] = 200


# ---------------------------------------------------------------------------
# 二、预占状态机（本包命门，R3 的落点）
# ---------------------------------------------------------------------------


#: 持有预占的三个状态。**"还回去"只允许从这三个里发生一次**。
RESERVATION_STATE_HELD: Final[str] = "HELD"
#: 已结算（预占转已结算，可能金额与预占不等）。
RESERVATION_STATE_SETTLED: Final[str] = "SETTLED"
#: 已释放（任务取消 / 失败，钱还回可用额度）。
RESERVATION_STATE_RELEASED: Final[str] = "RELEASED"
#: 已过期待回收（预占方失联，由对账回收）。**它仍然持有预占** ——
#: 这是与 `RELEASED` 的关键区别：`EXPIRED` 表示"系统认为该还但还没还"，
#: 需要人工或定时任务推进到 `RELEASED`。
RESERVATION_STATE_EXPIRED: Final[str] = "EXPIRED"

RESERVATION_STATES: Final[frozenset[str]] = frozenset(
    {
        RESERVATION_STATE_HELD,
        RESERVATION_STATE_SETTLED,
        RESERVATION_STATE_RELEASED,
        RESERVATION_STATE_EXPIRED,
    }
)

RESERVATION_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(RESERVATION_STATES)
)

#: **持有预占的状态集合**。
#:
#: `HELD` 与 `EXPIRED` 都在其中 —— `EXPIRED` 的钱**还在账面里占着**，
#: 只是标记了"该回收"。若把它排除，`project_budgets.reserved_amount`
#: 就会与明细对不上，而 `recompute_reserved` 的对账判据（R10）会立刻报差异。
#:
#: 终态（`SETTLED` / `RELEASED`）**不在其中**：它们代表"账已经结清，
#: 不占额度了"。所有释放路径的 `WHERE state IN (...)` 用的就是这一个集合，
#: 因此"重复释放"第二次会 `rowcount = 0`（`upload_sessions` 的同款保证）。
HOLDING_RESERVATION_STATES: Final[frozenset[str]] = frozenset(
    {RESERVATION_STATE_HELD, RESERVATION_STATE_EXPIRED}
)

HOLDING_RESERVATION_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(HOLDING_RESERVATION_STATES)
)

#: 终态集合。它存在的理由与 `HOLDING_*` 对称：**"不占额度"这件事也要被点名**，
#: 否则"哪些状态是终态"只存在于人的记忆里。
TERMINAL_RESERVATION_STATES: Final[frozenset[str]] = frozenset(
    {RESERVATION_STATE_SETTLED, RESERVATION_STATE_RELEASED}
)

TERMINAL_RESERVATION_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(TERMINAL_RESERVATION_STATES)
)

MAX_RESERVATION_REF_CHARS: Final[int] = 200
MAX_RESERVATION_NOTE_CHARS: Final[int] = 500


# ---------------------------------------------------------------------------
# 三、账本分录（§2.4 第 5 项 —— 本包自行定义）
# ---------------------------------------------------------------------------


#: 按用量记（来自 `CapabilityResult.usage`，单价由 `pricing_versions` 解释）。
LEDGER_KIND_USAGE: Final[str] = "USAGE"
#: 按账单记（来自供应商账单 / 人工录入的权威金额）。
LEDGER_KIND_BILL: Final[str] = "BILL"
#: 冲正。**必须是新增的一条负数分录**，不是改旧行 —— 见 `models.py`。
LEDGER_KIND_REVERSAL: Final[str] = "REVERSAL"
#: 人工调整（对账差异的最终处置）。
LEDGER_KIND_ADJUSTMENT: Final[str] = "ADJUSTMENT"

LEDGER_KINDS: Final[frozenset[str]] = frozenset(
    {
        LEDGER_KIND_USAGE,
        LEDGER_KIND_BILL,
        LEDGER_KIND_REVERSAL,
        LEDGER_KIND_ADJUSTMENT,
    }
)

LEDGER_KIND_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(LEDGER_KINDS)
)

#: 负金额的分录种类。**只有冲正允许负数**。
#:
#: 为什么 `ADJUSTMENT` 不允许：调整可以往两边调，但"往回调"的语义
#: 应当在账本上读成"一条负数调整"。把它放开会让"这个数为什么是负的"
#: 无从判断是冲正还是调整 —— 而这两者的可审计性要求不同。
#: `BILL` / `USAGE` 为负则是数据缺陷（供应商不会给你倒付钱）。
NEGATIVE_LEDGER_KINDS: Final[frozenset[str]] = frozenset({LEDGER_KIND_REVERSAL})

NEGATIVE_LEDGER_KIND_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(NEGATIVE_LEDGER_KINDS)
)

#: 正向分录（金额必须 > 0）。
POSITIVE_LEDGER_KINDS: Final[frozenset[str]] = frozenset(
    {LEDGER_KIND_USAGE, LEDGER_KIND_BILL}
)

POSITIVE_LEDGER_KIND_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(POSITIVE_LEDGER_KINDS)
)

MAX_LEDGER_DESCRIPTION_CHARS: Final[int] = 500
MAX_LEDGER_SOURCE_REF_CHARS: Final[int] = 200


# ---------------------------------------------------------------------------
# 四、预估状态
# ---------------------------------------------------------------------------


ESTIMATE_STATE_DRAFT: Final[str] = "DRAFT"
#: 已预占（预估已转成一条 `budget_reservations`）。
ESTIMATE_STATE_RESERVED: Final[str] = "RESERVED"
#: 已结算（实际金额已回填）。
ESTIMATE_STATE_SETTLED: Final[str] = "SETTLED"
#: 已作废（不再需要这些钱）。
ESTIMATE_STATE_CANCELLED: Final[str] = "CANCELLED"

ESTIMATE_STATES: Final[frozenset[str]] = frozenset(
    {
        ESTIMATE_STATE_DRAFT,
        ESTIMATE_STATE_RESERVED,
        ESTIMATE_STATE_SETTLED,
        ESTIMATE_STATE_CANCELLED,
    }
)

ESTIMATE_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(ESTIMATE_STATES)
)


# ---------------------------------------------------------------------------
# 五、业务错误原因码
# ---------------------------------------------------------------------------
# `message` 是给人读的文案、会变；`details.reason` 是机器可判的。
# 测试与前端断言的是后者（与 WP22/WP23/WP24 同一手法）。

REASON_BUDGET_NOT_FOUND: Final[str] = "BUDGET_NOT_FOUND"
REASON_BUDGET_EXCEEDED: Final[str] = "BUDGET_EXCEEDED"
REASON_RESERVATION_NOT_FOUND: Final[str] = "RESERVATION_NOT_FOUND"
REASON_RESERVATION_NOT_HOLDING: Final[str] = "RESERVATION_NOT_HOLDING"
REASON_RESERVATION_AMOUNT_INVALID: Final[str] = "RESERVATION_AMOUNT_INVALID"
REASON_AMOUNT_NOT_POSITIVE: Final[str] = "AMOUNT_NOT_POSITIVE"
REASON_AMOUNT_PRECISION: Final[str] = "AMOUNT_PRECISION"
REASON_CURRENCY_MISMATCH: Final[str] = "CURRENCY_MISMATCH"
REASON_PRICING_VERSION_NOT_FOUND: Final[str] = "PRICING_VERSION_NOT_FOUND"
REASON_PRICING_VERSION_NOT_USABLE: Final[str] = "PRICING_VERSION_NOT_USABLE"
REASON_QUANTITY_BELOW_MIN: Final[str] = "QUANTITY_BELOW_MIN"
REASON_QUANTITY_ABOVE_MAX: Final[str] = "QUANTITY_ABOVE_MAX"
REASON_BATCH_KIND_UNKNOWN: Final[str] = "BATCH_KIND_UNKNOWN"
REASON_LEDGER_KIND_INVALID: Final[str] = "LEDGER_KIND_INVALID"
REASON_REVERSAL_TARGET_REQUIRED: Final[str] = "REVERSAL_TARGET_REQUIRED"
REASON_ESTIMATE_NOT_FOUND: Final[str] = "ESTIMATE_NOT_FOUND"
REASON_ESTIMATE_NOT_RESERVED: Final[str] = "ESTIMATE_NOT_RESERVED"
REASON_PRICING_INCOMPLETE: Final[str] = "PRICING_INCOMPLETE"
REASON_BUDGET_NOT_CONFIGURED: Final[str] = "BUDGET_NOT_CONFIGURED"
REASON_LEDGER_TOTAL_MISMATCH: Final[str] = "LEDGER_TOTAL_MISMATCH"
#: 与 `planning.types.REASON_PROJECT_NOT_FOUND` **同值**。
#: 本包刻意自带一个常量而不是 import 上游那个：`planning` 是 `billing` 的
#: 上游（本包读它的配置），跨包 import 一个错误码常量会让"改一个字符串"
#: 变成跨包改动。两边同值是**约定**，由契约测试对账。
REASON_PROJECT_NOT_FOUND: Final[str] = "PROJECT_NOT_FOUND"
#: 数量上限尚未配置（`runtime_settings` 里 `batch.limits` 为空）。
#: 这是**配置缺失**不是"数量越界"：前者要人去配，后者要人改输入。
REASON_LIMITS_NOT_CONFIGURED: Final[str] = "LIMITS_NOT_CONFIGURED"


# ---------------------------------------------------------------------------
# 六、传值结构（frozen dataclass，与 WP24 `types.py` 同风格）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitPrice:
    """一条计价：`(unit, unit_price)`。

    `unit` 是 :data:`PRICING_UNITS` 之一；`unit_price` 是**每一计量单位**的
    单价（例如"每 token 0.00012 元"），不是"每千 token"——
    换算留给录入方，因为"每千 token"这个前缀在跨供应商时名字不同
    （1K / 1M），把它写进库里就必须再定一个"千"的口径。
    """

    unit: str
    unit_price: Decimal

    def __post_init__(self) -> None:
        if self.unit not in PRICING_UNITS:
            raise money.MoneyError(f"未知计量单位：{self.unit!r}")
        if money.is_negative(self.unit_price):
            raise money.MoneyError(f"单价不得为负：{self.unit_price}")


@dataclass(frozen=True)
class UsageItem:
    """一条用量：`(metric, quantity)`。

    `metric` 是**供应商给的计量名**（`input_tokens` / `output_tokens` /
    `images` / `seconds` …）—— 本包**不把它约束成闭集**，理由：
    它是供应商侧的名字，而供应商有多个（OP-04 未结论）。
    把它做成闭集等于"用今天的两个供应商约束未来所有的"。

    但**从 `metric` 到 `unit` 的映射必须是显式的**（见 :data:`METRIC_UNITS`）。
    映射表是闭集 —— 一个没有映射的 metric **无法计价**，
    这件事必须报错而不是算成 0。
    """

    metric: str
    quantity: Decimal

    def __post_init__(self) -> None:
        if not self.metric.strip():
            raise money.MoneyError("用量名不得为空")
        if money.is_negative(self.quantity):
            raise money.MoneyError(f"用量不得为负：{self.quantity}")


#: 供应商计量名 → 本包计量单位。**闭集，且必须显式**。
#:
#: 没有映射的 metric 会让 `compute_amount` 无法得到单价 ⇒ 报错。
#: 若"查不到就跳过"，预估会**静默少算**（一笔只算了 input 没算 output），
#: 而账面看起来完全正常。
#:
#: 代价已登记：新供应商带来新 metric 时必须改这里。这是刻意的 ——
#: "这个新指标按什么单位算钱"**必须有人回答一次**。
METRIC_UNITS: Final[dict[str, str]] = {
    "input_tokens": PRICING_UNIT_TOKEN,
    "output_tokens": PRICING_UNIT_TOKEN,
    "total_tokens": PRICING_UNIT_TOKEN,
    "tokens": PRICING_UNIT_TOKEN,
    "images": PRICING_UNIT_IMAGE,
    "image_count": PRICING_UNIT_IMAGE,
    "seconds": PRICING_UNIT_SECOND,
    "duration_seconds": PRICING_UNIT_SECOND,
    "calls": PRICING_UNIT_CALL,
    "requests": PRICING_UNIT_CALL,
}


@dataclass(frozen=True)
class BudgetView:
    """预算账面视图。**四个值必须一起返回**。

    只给 `available` 就是"悄悄算错也没人知道"：调用方拿不到 `limit` 与
    `reserved` 就无法发现"available 是不是真的等于 limit - committed - reserved"。
    这与 WP24 的 `EffectiveSetting` 必须同时返回 `value` / `global_value` /
    `diverged` 是**同一条理由**（给足可断言的对象）。

    ## 不变量

        available == limit - committed - reserved

    这条由 `project_budgets` 的 CHECK 把 `committed + reserved <= limit`
    兜住，因此 `available >= 0` 恒成立。**但它是"数据库已保证的事"，
    不能当判据**（判据纪律第 2 条：CHECK 保证恒真的条件只能当解释）。
    测试要断言的是"并发预占后这条等式仍成立"，而不是"available >= 0"。
    """

    project_id: uuid.UUID
    currency: str
    limit: Decimal
    committed: Decimal
    reserved: Decimal
    available: Decimal
    #: 账面是否已存在（`False` = 该项目从未设过预算）。
    configured: bool = True

    def as_dict(self) -> dict[str, str]:
        """回给前端的形状。**金额一律字符串**（§2.4 第 7 项）。"""
        return {
            "projectId": str(self.project_id),
            "currency": self.currency,
            "limit": money.to_wire(self.limit),
            "committed": money.to_wire(self.committed),
            "reserved": money.to_wire(self.reserved),
            "available": money.to_wire(self.available),
            "configured": self.configured,
        }


@dataclass(frozen=True)
class ReservationView:
    """一条预占的对外视图。"""

    id: uuid.UUID
    project_id: uuid.UUID
    state: str
    currency: str
    amount: Decimal
    settled_amount: Decimal | None
    ref: str | None
    note: str | None
    estimate_id: uuid.UUID | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "projectId": str(self.project_id),
            "state": self.state,
            "currency": self.currency,
            "amount": money.to_wire(self.amount),
            "settledAmount": money.to_wire_optional(self.settled_amount),
            "ref": self.ref,
            "note": self.note,
            "estimateId": None if self.estimate_id is None else str(self.estimate_id),
        }


@dataclass(frozen=True)
class EstimateLine:
    """预估的一行：`数量 × 单价 = 金额`。**三项一起留**。

    只留 `amount` 会让"这个数字怎么来的"无从复核 —— 而 AC-26 的
    「估算」核对要求的正是"能复核"。单价在预估时必须**冻结**
    （`pricing_versions` 不可变，改价 = 插新版本），因此预估不会被
    后来的改价影响，回算永远得出同一个数。
    """

    unit: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "quantity": money.quantity_to_wire(self.quantity),
            "unitPrice": money.to_wire(self.unit_price),
            "amount": money.to_wire(self.amount),
        }


@dataclass(frozen=True)
class CostEstimateView:
    """一份预估的对外视图。"""

    id: uuid.UUID
    project_id: uuid.UUID
    batch_kind: str
    quantity: int
    currency: str
    state: str
    total_amount: Decimal
    actual_amount: Decimal | None
    variance_amount: Decimal | None
    pricing_version_id: uuid.UUID | None
    lines: tuple[EstimateLine, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "projectId": str(self.project_id),
            "batchKind": self.batch_kind,
            "quantity": self.quantity,
            "currency": self.currency,
            "state": self.state,
            "totalAmount": money.to_wire(self.total_amount),
            "actualAmount": money.to_wire_optional(self.actual_amount),
            "varianceAmount": money.to_wire_optional(self.variance_amount),
            "pricingVersionId": (
                None if self.pricing_version_id is None
                else str(self.pricing_version_id)
            ),
            "lines": [line.as_dict() for line in self.lines],
        }


@dataclass(frozen=True)
class LedgerEntryView:
    """一条账本分录的对外视图。"""

    id: uuid.UUID
    project_id: uuid.UUID
    entry_kind: str
    currency: str
    amount: Decimal
    quantity: Decimal | None
    unit: str | None
    description: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "projectId": str(self.project_id),
            "entryKind": self.entry_kind,
            "currency": self.currency,
            "amount": money.to_wire(self.amount),
            "quantity": (
                None if self.quantity is None
                else money.quantity_to_wire(self.quantity)
            ),
            "unit": self.unit,
            "description": self.description,
        }


@dataclass(frozen=True)
class PendingUsage:
    """一条**未结用量**（对账的输入）。

    ## 这是本包对 OP-108 的回答，也是本包最重要的一个"没有状态"的决定

    "未结"**不是存储的状态**，而是**查询的结论**：

        job_attempts.usage_pending = true
        AND NOT EXISTS (usage_ledger WHERE job_attempt_id = 该 attempt)

    即"上游说这里有一笔未结的用量，而账本里还没有它的分录"。

    ## 为什么不在 `usage_ledger` 上放一个 `state` 列

    文档 §1.6 R4 曾设想"`usage_ledger` 每笔带 `state`（PENDING / CONFIRMED /
    REVERSED）"。**实现时发现它与 R5 直接冲突**：R5 要求账本是**不可变**的
    （ADR-13 原话"与费用账本、版本快照同级对待"），而"状态迁移"是就地
    `UPDATE` —— 两者不可能同时成立。

    三者不可兼得，只能放弃一个：放弃"状态列"。

    改后的形态更简单，且**消除了一个真实风险**：
      · "未结"是派生的 ⇒ 不存在"状态写错了"这件事（没有状态可写错）；
      · "已确认"由**追加一条 `BILL` 分录**表达 ⇒ 与"账本只追加"一致；
      · "冲正"是新分录 ⇒ 同上。

    唯一的代价是"未结清单"每次要现算 —— 那是 `LIMIT` 内的一个
    `NOT EXISTS` 反连接，不是问题。

    ## 金额为什么叫 `estimated_amount` 而不是 `amount`

    因为它**不确定**。上游此刻只给了 `usage_pending = true` 这个布尔信号，
    `CapabilityResult.usage` / `cost_amount` **没有任何落库位置**
    （本包勘察实测：`provider_calls` 表无 usage / cost 列，
    `job_attempts` 只有 `usage_pending`）。因此：

      · `available` = `True` 表示"账本里已有对应分录，金额取自那里"；
      · `available` = `False` 表示"账本里还没有 —— 金额待人工核对或账单导入"。

    这个字段存在的意义就是**让"不知道金额"与"知道金额是 0"可区分**。
    把它默认成 0 会让账面把"未结"读成"没花钱"。
    """

    attempt_id: uuid.UUID
    job_id: uuid.UUID
    project_id: uuid.UUID | None
    attempt_no: int
    state: str
    #: 账本里是否已有对应分录。
    available: bool
    estimated_amount: Decimal | None = None
    ledger_entry_id: uuid.UUID | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "attemptId": str(self.attempt_id),
            "jobId": str(self.job_id),
            "projectId": None if self.project_id is None else str(self.project_id),
            "attemptNo": self.attempt_no,
            "state": self.state,
            "available": self.available,
            "estimatedAmount": money.to_wire_optional(self.estimated_amount),
            "ledgerEntryId": (
                None if self.ledger_entry_id is None else str(self.ledger_entry_id)
            ),
            "note": self.note,
        }


@dataclass(frozen=True)
class DriftReport:
    """一张表的 `账面 vs 明细` 对账结果（R10）。

    `drift == 0` 是唯一的好结果。**本结构刻意不做"容差"**：
    金额是定点数，两边用同一个 `money.sum_amounts` 算出来，
    不一致就是真的不一致。引入容差等于把"确定性缺陷"降级成"概率性告警"。
    """

    project_id: uuid.UUID
    table: str
    account_value: Decimal
    recomputed_value: Decimal

    @property
    def drift(self) -> Decimal:
        return money.sub(self.account_value, self.recomputed_value)

    @property
    def healthy(self) -> bool:
        return money.is_zero(self.drift)

    def as_dict(self) -> dict[str, Any]:
        return {
            "projectId": str(self.project_id),
            "table": self.table,
            "accountValue": money.to_wire(self.account_value),
            "recomputedValue": money.to_wire(self.recomputed_value),
            "drift": money.to_wire(self.drift),
            "healthy": self.healthy,
        }


@dataclass(frozen=True)
class ReconciliationReport:
    """一次对账跑批的结果。

    ## 四个字段分别回答四个不同的问题

    | 字段 | 回答 |
    |---|---|
    | `scanned` | 这次看了多少条 `usage_pending` 的 attempt |
    | `pending` | 其中有多少条**仍然未结**（要么没分录，要么分录未确认） |
    | `resolved` | 有多少条**已经在账本里有对应分录**（不需要人工介入） |
    | `drifts` | 账面与明细的不一致（R10，独立的第二条判据） |

    **`scanned` 与 `resolved + pending` 必须相等** —— 这是个可断言的恒等式
    （两个数来自同一批扫描，count 与列表长度都是现场算的）。
    缺了 `scanned`，一个"扫到 0 条"的错误实现会报告"全部已结清"。

    ## `projects_checked` 为什么必须存在

    `drifts` 只装**不一致**的条目（`drift == 0` 的健康项目不进列表，
    否则 `healthy` 会恒为 `False`）。于是"查了 30 个项目、全部一致"与
    "一个项目都没查"产出的 `drifts` **都是空元组** —— 前者是好消息，
    后者是**漏查**，而 `healthy` 对两者都说 True。
    这与 `scanned` 要解决的问题同源（判据纪律："没跑"与"跑绿"不能同形），
    所以把"实际检查过几个项目"也记下来：
    `projects_checked == len(repo.list_budgeted_project_ids())`（全平台时）
    是一条能与明细列对账的判据。

    ## `truncated` 的偏向

    `scanned >= limit` 即判 True（哪怕恰好取满）。**宁多报不少报**：
    多报一次 "还有没扫完的" 只是让人再跑一次，少报一次则等于声称
    "全平台已结清"而实际还有尾巴。
    """

    scanned: int
    resolved: int
    pending: tuple[PendingUsage, ...] = ()
    drifts: tuple[DriftReport, ...] = ()
    #: 本次扫描覆盖的项目（`None` = 全平台）。
    project_id: uuid.UUID | None = None
    truncated: bool = False
    #: `drift` 那半边**实际检查过**的项目数（全平台时 = 全部需对账项目数）。
    projects_checked: int = 0

    @property
    def pending_count(self) -> int:
        return len(self.pending)

    @property
    def healthy(self) -> bool:
        return not self.pending and not self.drifts and not self.truncated

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "resolved": self.resolved,
            "pending": [item.as_dict() for item in self.pending],
            "pendingCount": self.pending_count,
            "drifts": [item.as_dict() for item in self.drifts],
            "projectId": None if self.project_id is None else str(self.project_id),
            "truncated": self.truncated,
            "projectsChecked": self.projects_checked,
            "healthy": self.healthy,
        }


@dataclass(frozen=True)
class LedgerEntryInput:
    """结算时要写进账本的一条分录（**尚未落库的意图**）。

    ## 为什么它不是 :class:`LedgerEntryView`

    `LedgerEntryView` 描述**已经写进去的**行（有 `id`、有 `project_id`）。
    本结构描述**打算写的**一行 —— 它没有 `id`，也**不带 `project_id`**
    （那由结算的目标预占决定），更没有 `occurred_at`。

    ## `occurred_at` 与 `source_ref` 刻意不在本结构里

    两者都由 `service.settle_reservation` **统一给**：

      · `occurred_at` —— 同一笔结算的几条分录必须落在同一时刻。
        若让调用方逐条传，他们会各写一个 `datetime.now()`，
        于是"同一笔账"在账本上分成了两个时间点，按时间聚合的报表
        会把它算成两笔。给的是**数据库时间**（`repo.now()`），
        不是 Python 时间 —— 两个 API 实例的机器时钟可以差几秒。
      · `source_ref` —— 它标识"这笔分录来自哪次尝试"，由预占/结算的
        目标派生（`attempt:<id>`），不是调用方自由发挥的字段。
        让调用方填就等于允许写错前缀，而写错的后果是
        `NOT EXISTS` 永远为真、这笔账被**反复判为未结**且不报错。

    `idempotency_key` 在人工分录（`ADJUSTMENT`）时可以不给 ——
    那类分录没有天然幂等键；自动路径（`USAGE` / `BILL` / `REVERSAL`）
    必须给，否则重跑一次对账就会多记一笔。
    """

    entry_kind: str
    amount: Decimal
    quantity: Decimal | None = None
    unit: str | None = None
    reverses_ledger_id: uuid.UUID | None = None
    idempotency_key: str | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        if self.entry_kind not in LEDGER_KINDS:
            raise money.MoneyError(
                f"未知分录类型：{self.entry_kind!r}（取值域 {sorted(LEDGER_KINDS)}）"
            )
        # 符号与类型的对应关系由 `usage_ledger.amount_sign_matches_kind`
        # CHECK 兜底，这里做一次同口径的早失败 —— 让"传了个正数的冲正"
        # 在 Python 层就报错，而不是等到 flush 时才炸成 500。
        if self.entry_kind in NEGATIVE_LEDGER_KINDS:
            if not money.is_negative(self.amount):
                raise money.MoneyError(
                    f"{self.entry_kind} 是冲正分录，金额必须为负：{self.amount}"
                )
            if self.reverses_ledger_id is None:
                raise money.MoneyError(
                    f"{self.entry_kind} 必须引用被冲正的那条分录"
                    "（reverses_ledger_id）—— 否则'冲的是哪一笔'没有答案"
                )
        elif not money.is_positive(self.amount):
            raise money.MoneyError(
                f"{self.entry_kind} 的金额必须为正：{self.amount}"
                "（要用负数请用 REVERSAL，并引用被冲正的分录）"
            )
        if self.reverses_ledger_id is not None and self.entry_kind not in NEGATIVE_LEDGER_KINDS:
            raise money.MoneyError(
                f"只有 {sorted(NEGATIVE_LEDGER_KINDS)} 可以引用被冲正的分录，"
                f"收到 {self.entry_kind}"
            )
        if (self.quantity is None) != (self.unit is None):
            raise money.MoneyError(
                "用量与计量单位必须同时给或同时不给"
                "（`usage_ledger.quantity_unit_paired` CHECK 的同口径早失败）"
            )


@dataclass(frozen=True)
class PricingLine:
    """一份计价版本里的一行。"""

    unit: str
    unit_price: Decimal


@dataclass(frozen=True)
class PricingVersionView:
    """一份计价版本的对外视图。"""

    id: uuid.UUID
    provider_id: uuid.UUID
    model_profile_id: uuid.UUID | None
    state: str
    currency: str
    label: str
    effective_from: object
    lines: tuple[PricingLine, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "providerId": str(self.provider_id),
            "modelProfileId": (
                None if self.model_profile_id is None else str(self.model_profile_id)
            ),
            "state": self.state,
            "currency": self.currency,
            "label": self.label,
            "effectiveFrom": self.effective_from.isoformat()  # type: ignore[union-attr]
            if hasattr(self.effective_from, "isoformat")
            else str(self.effective_from),
            "lines": [
                {
                    "unit": line.unit,
                    "unitPrice": money.to_wire(line.unit_price),
                }
                for line in self.lines
            ],
        }


__all__ = [
    "CURRENCIES",
    "CURRENCY_CNY",
    "CURRENCY_LIST_SQL",
    "CURRENCY_USD",
    "ESTIMATE_STATES",
    "ESTIMATE_STATE_CANCELLED",
    "ESTIMATE_STATE_DRAFT",
    "ESTIMATE_STATE_LIST_SQL",
    "ESTIMATE_STATE_RESERVED",
    "ESTIMATE_STATE_SETTLED",
    "HOLDING_RESERVATION_STATES",
    "HOLDING_RESERVATION_STATE_LIST_SQL",
    "LEDGER_KINDS",
    "LEDGER_KIND_ADJUSTMENT",
    "LEDGER_KIND_BILL",
    "LEDGER_KIND_LIST_SQL",
    "LEDGER_KIND_REVERSAL",
    "LEDGER_KIND_USAGE",
    "MAX_LEDGER_DESCRIPTION_CHARS",
    "MAX_LEDGER_SOURCE_REF_CHARS",
    "MAX_PRICING_LABEL_CHARS",
    "MAX_PRICING_NOTE_CHARS",
    "MAX_RESERVATION_NOTE_CHARS",
    "MAX_RESERVATION_REF_CHARS",
    "MAX_UNIT_PRICE_CHARS",
    "METRIC_UNITS",
    "NEGATIVE_LEDGER_KINDS",
    "NEGATIVE_LEDGER_KIND_LIST_SQL",
    "POSITIVE_LEDGER_KINDS",
    "POSITIVE_LEDGER_KIND_LIST_SQL",
    "PRICING_STATES",
    "PRICING_STATE_DRAFT",
    "PRICING_STATE_LIST_SQL",
    "PRICING_STATE_PUBLISHED",
    "PRICING_STATE_RETIRED",
    "PRICING_UNITS",
    "PRICING_UNIT_CALL",
    "PRICING_UNIT_IMAGE",
    "PRICING_UNIT_LIST_SQL",
    "PRICING_UNIT_SECOND",
    "PRICING_UNIT_TOKEN",
    "REASON_AMOUNT_NOT_POSITIVE",
    "REASON_AMOUNT_PRECISION",
    "REASON_BATCH_KIND_UNKNOWN",
    "REASON_BUDGET_EXCEEDED",
    "REASON_BUDGET_NOT_CONFIGURED",
    "REASON_BUDGET_NOT_FOUND",
    "REASON_CURRENCY_MISMATCH",
    "REASON_ESTIMATE_NOT_FOUND",
    "REASON_ESTIMATE_NOT_RESERVED",
    "REASON_LEDGER_KIND_INVALID",
    "REASON_LEDGER_TOTAL_MISMATCH",
    "REASON_LIMITS_NOT_CONFIGURED",
    "REASON_PRICING_INCOMPLETE",
    "REASON_PRICING_VERSION_NOT_FOUND",
    "REASON_PRICING_VERSION_NOT_USABLE",
    "REASON_PROJECT_NOT_FOUND",
    "REASON_QUANTITY_ABOVE_MAX",
    "REASON_QUANTITY_BELOW_MIN",
    "REASON_RESERVATION_AMOUNT_INVALID",
    "REASON_RESERVATION_NOT_FOUND",
    "REASON_RESERVATION_NOT_HOLDING",
    "REASON_REVERSAL_TARGET_REQUIRED",
    "RESERVATION_STATES",
    "RESERVATION_STATE_EXPIRED",
    "RESERVATION_STATE_HELD",
    "RESERVATION_STATE_LIST_SQL",
    "RESERVATION_STATE_RELEASED",
    "RESERVATION_STATE_SETTLED",
    "TERMINAL_RESERVATION_STATES",
    "TERMINAL_RESERVATION_STATE_LIST_SQL",
    "USABLE_PRICING_STATES",
    "BudgetView",
    "CostEstimateView",
    "DriftReport",
    "EstimateLine",
    "LedgerEntryInput",
    "LedgerEntryView",
    "PendingUsage",
    "PricingLine",
    "PricingVersionView",
    "ReconciliationReport",
    "ReservationView",
    "UnitPrice",
    "UsageItem",
]
