"""费用服务层 —— **本包唯一的写入口**。

## 为什么写入口只有一个

`project_budgets`（账面）与 `budget_reservations`（明细）必须**同事务**改，
否则会出现"账面扣了但明细没记"（钱凭空少）或"明细记了但账面没扣"
（钱凭空多）—— R10 分叉的两种方向**都不会报错**。

把这两个动作分散到多个模块，就等于让"它们是否成对"取决于调用者的自觉。
本模块是唯一入口后，"成对"是**代码结构**保证的，不是约定保证的。

## 三条顺序不可调换

| 动作 | 顺序 | 为什么不能反 |
|---|---|---|
| 预占 | ① `ensure_budget` → ② `reserve_budget`（条件 UPDATE）→ ③ `insert_reservation` | 反过来的话，抢额度失败时那条明细**已经写进去了**，只能再删/再改状态 —— 它将是一行"从未真正持有过预占"的记录，把"每次预占一行"的语义污染掉（`repository.insert_reservation` 原文） |
| 释放 | ① `transition_reservation`（条件 UPDATE）→ ② `release_budget` | **状态迁移成功才算还了钱**。反过来会让一个重复的释放请求把账面多还一次 —— 那正是 R3 的故障形态，而且是静默的 |
| 结算 | ① `settle_budget`（一条语句双检查）→ ② `transition_reservation` | 同预占：先动账面才能拿到"我是否抢到了额度"这个确定答案 |

## 权限不在这里判定

`ProjectAction.VIEW_COST` / `OrgAction.MANAGE_BUDGET` 的判定在 `api.py`
的依赖里。服务层只做业务校验。理由与 `planning/service.py` 同：
把权限判定混进服务层，会让"同一条规则在 API 与 Worker 两侧各写一遍"，
而两边必然会分叉。

## 金额的进出

所有对外函数的金额参数**只收 `Decimal`**（`str` / `int` 也接受，
`float` 一律抛 `MoneyError`）；返回值里的金额一律经 `money.to_wire`
转成字符串（§2.4 第 7 项）。本模块**不 import `float` 相关的任何东西**。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Sequence

from sqlalchemy.exc import IntegrityError

from rednote.modules.audit.models import ACTOR_KIND_USER
from rednote.modules.audit.repository import AuditRepository
from rednote.modules.audit.service import record_audit
from rednote.modules.billing import ddl, money, pricing
from rednote.modules.billing.models import (
    BudgetReservation,
    CostEstimate,
    PricingVersion,
    ProjectBudget,
    UsageLedger,
)
from rednote.modules.billing.repository import BillingRepository
from rednote.modules.billing.types import (
    CURRENCIES,
    CURRENCY_CNY,
    HOLDING_RESERVATION_STATES,
    LEDGER_KIND_BILL,
    MAX_LEDGER_DESCRIPTION_CHARS,
    MAX_LEDGER_SOURCE_REF_CHARS,
    MAX_RESERVATION_NOTE_CHARS,
    MAX_RESERVATION_REF_CHARS,
    REASON_AMOUNT_NOT_POSITIVE,
    REASON_AMOUNT_PRECISION,
    REASON_BUDGET_EXCEEDED,
    REASON_CURRENCY_MISMATCH,
    REASON_ESTIMATE_NOT_FOUND,
    REASON_LEDGER_TOTAL_MISMATCH,
    REASON_LIMITS_NOT_CONFIGURED,
    REASON_PRICING_INCOMPLETE,
    REASON_PRICING_VERSION_NOT_FOUND,
    REASON_PROJECT_NOT_FOUND,
    REASON_QUANTITY_BELOW_MIN,
    REASON_RESERVATION_NOT_FOUND,
    REASON_RESERVATION_NOT_HOLDING,
    RESERVATION_STATE_RELEASED,
    RESERVATION_STATE_SETTLED,
    BudgetView,
    CostEstimateView,
    EstimateLine,
    LedgerEntryInput,
    LedgerEntryView,
    PricingLine,
    PricingVersionView,
    ReservationView,
)
from rednote.platform.errors import ApiError, ErrorCode

logger = logging.getLogger(__name__)

#: 新建项目账面时的**默认额度**。取 0 而不是某个"看起来合理"的数字：
#: 一个非零默认值会让从未设过预算的项目**凭空拥有额度**，
#: 而"这个项目有没有预算"恰恰是管理层要看的第一个事实
#: （`BudgetView.configured` 就是它）。0 元额度让"没设预算"表现为
#: "一预占就超限"，那是可解释的 422，不是静默放行。
DEFAULT_BUDGET_LIMIT: Final[Decimal] = money.ZERO

#: 默认币种。协议与本仓既有数据都用 CNY（`types.CURRENCY_CNY`）。
DEFAULT_CURRENCY: Final[str] = CURRENCY_CNY

# ---- 审计动作与对象（命名照抄 `baseline/service.py` 的 `_ACTION_*` / `_OBJECT_*`） ----

_ACTION_BUDGET_SET: Final[str] = "budget.set"
_ACTION_RESERVATION_CREATE: Final[str] = "budget_reservation.create"
_ACTION_RESERVATION_RELEASE: Final[str] = "budget_reservation.release"
_ACTION_RESERVATION_SETTLE: Final[str] = "budget_reservation.settle"
_ACTION_ESTIMATE_CREATE: Final[str] = "cost_estimate.create"
_ACTION_LEDGER_APPEND: Final[str] = "usage_ledger.append"

_OBJECT_BUDGET: Final[str] = "project_budget"
_OBJECT_RESERVATION: Final[str] = "budget_reservation"
_OBJECT_ESTIMATE: Final[str] = "cost_estimate"
_OBJECT_LEDGER: Final[str] = "usage_ledger"


# ---------------------------------------------------------------------------
# 一、错误构造（与 `planning/service.py:151~169` 同形）
# ---------------------------------------------------------------------------


def _validation_error(
    field: str, message: str, *, reason: str, **extra: Any
) -> ApiError:
    """构造 422 `VALIDATION_FAILED`，带可定位的 `fieldErrors`。

    前端按 `field` 高亮输入框；`details.reason` 给测试与前端做机器可判的断言
    （`message` 是给人读的文案，会变）。
    """
    details: dict[str, Any] = {
        "fieldErrors": [{"field": field, "type": reason, "message": message}],
        "reason": reason,
    }
    details.update(extra)
    return ApiError(ErrorCode.VALIDATION_FAILED, message, details=details)


def _not_found(message: str, *, reason: str, **extra: Any) -> ApiError:
    """构造 404 `RESOURCE_NOT_FOUND`。"""
    details: dict[str, Any] = {"reason": reason}
    details.update(extra)
    return ApiError(ErrorCode.RESOURCE_NOT_FOUND, message, details=details)


def _conflict(message: str, *, reason: str, **extra: Any) -> ApiError:
    """构造 409 `INVALID_TRANSITION`（状态机不允许的迁移）。"""
    details: dict[str, Any] = {"reason": reason}
    details.update(extra)
    return ApiError(ErrorCode.INVALID_TRANSITION, message, details=details)


def _budget_exceeded(message: str, *, reason: str = REASON_BUDGET_EXCEEDED, **extra: Any) -> ApiError:
    """构造 422 `BUDGET_EXCEEDED` —— **本包是它的第一个业务消费者**。

    `errors.py:66` 定义了它、`:87` 映射到 422、`:117` 给了文案
    "项目预算不足，无法提交该任务。"，而全仓此前**零业务消费者**。

    **它必须可重试吗** —— 不。`_RETRYABLE_BY_CODE` 里只有
    `RATE_LIMITED` 与 `DEPENDENCY_UNAVAILABLE`，计费类写操作**不可重试**：
    "预算不足"是业务结论，不是瞬时故障（与 V4 否决"预占重试队列"同一理由）。
    """
    details: dict[str, Any] = {"reason": reason}
    details.update(extra)
    return ApiError(ErrorCode.BUDGET_EXCEEDED, message, details=details)


# ---------------------------------------------------------------------------
# 二、入参守卫
# ---------------------------------------------------------------------------


def _as_amount(value: Decimal | str | int, *, label: str = "金额") -> Decimal:
    """金额入参归一。`float` 与 `bool` 在这里被拒（`money._reject_float`）。

    `MoneyError` 是 `ValueError` 的子类，**刻意不继承 `ApiError`**
    （纯函数层不认识 HTTP）。服务层负责把它翻成 422 —— 翻的位置就在本函数，
    这样"金额非法"这条路径不会散落到每个对外函数的开头。
    """
    try:
        amount = money.quantize(value)
    except money.MoneyError as exc:
        raise _validation_error("amount", str(exc), reason=REASON_AMOUNT_PRECISION) from exc
    return amount


def _require_positive_amount(
    value: Decimal | str | int, *, field: str = "amount"
) -> Decimal:
    """预占金额必须为正。

    **0 元的预占被明确拒绝**：它会通过所有并发判据
    （`committed + reserved + 0 <= limit` 恒真），于是"预算并发预占测试"
    全绿而系统根本没在管钱。这条拒绝就是这个判据的入口处守卫。
    （与 `pricing.total_of` 对空预估行的拒绝同一理由。）
    """
    amount = _as_amount(value, label="预占金额")
    if not money.is_positive(amount):
        raise _validation_error(
            field,
            f"预占金额必须为正数：{money.to_wire(amount)}。"
            "0 元预占会通过所有并发判据，等于没在管钱",
            reason=REASON_AMOUNT_NOT_POSITIVE,
        )
    return amount


def _require_currency(currency: str) -> str:
    if currency not in CURRENCIES:
        raise _validation_error(
            "currency",
            f"未知币种 {currency!r}（取值域 {sorted(CURRENCIES)}）。",
            reason=REASON_CURRENCY_MISMATCH,
        )
    return currency


def _require_text(value: str | None, *, field: str, limit: int) -> str | None:
    """可选短文本的长度守卫（`ref` / `note`）。超长在 DB 层是 `22001`（500），
    在这里是 422 —— 同一件事，前者让用户看到"服务器内部错误"。"""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > limit:
        raise _validation_error(
            field, f"{field} 最长 {limit} 字符，收到 {len(text)}", reason="TEXT_TOO_LONG"
        )
    return text


# ---------------------------------------------------------------------------
# 三、计价版本解析（预估与预占共用）
# ---------------------------------------------------------------------------


def _to_pricing_lines(version: PricingVersion) -> tuple[PricingLine, ...]:
    """把 `pricing_versions.unit_prices`（jsonb）转成计价行。

    **值必须是字符串**（`models.PricingVersion` docstring：jsonb number 是
    numeric 的无标度形态，读取时位数会丢）。这里对"读回来的值不是 str"
    显式报错，而不是 `Decimal(str(value))` 硬转 —— 硬转会把一个
    已经丢过精度的 float 静默救活，那个错误要到对账才发现。
    """
    raw = version.unit_prices or {}
    lines: list[PricingLine] = []
    for unit, price in raw.items():
        if not isinstance(price, str):
            raise _validation_error(
                "unitPrices",
                f"计价版本 {version.id} 的 {unit} 单价不是字符串"
                f"（{type(price).__name__}）—— jsonb number 会丢位数，"
                "请以字符串录入",
                reason=REASON_PRICING_INCOMPLETE,
                pricingVersionId=str(version.id),
                unit=unit,
            )
        try:
            lines.append(PricingLine(unit=unit, unit_price=money.parse_wire_strict(price, label=f"{unit} 单价")))
        except (money.MoneyError, pricing.PricingError) as exc:
            raise _validation_error(
                "unitPrices",
                f"计价版本 {version.id} 的 {unit} 单价无法解析：{exc}",
                reason=REASON_PRICING_INCOMPLETE,
                pricingVersionId=str(version.id),
                unit=unit,
            ) from exc
    return tuple(lines)


def _require_pricing_version(
    repo: BillingRepository,
    *,
    provider_id: uuid.UUID,
    currency: str,
    model_profile_id: uuid.UUID | None,
    at: datetime,
) -> PricingVersion:
    """取该时点可用的价目。**取不到就报错，不退化成默认单价**。

    退化成"0 单价"的后果在 `pricing.price_for` 的 docstring 里写过了：
    整批免费，且账面看不出异常。
    """
    version = repo.find_usable_pricing_version(
        provider_id=provider_id,
        currency=currency,
        at=at,
        model_profile_id=model_profile_id,
    )
    if version is None:
        raise _not_found(
            f"供应商 {provider_id} 在 {at.isoformat()} 没有可用的 "
            f"{currency} 计价版本"
            + (f"（模型档案 {model_profile_id}）" if model_profile_id else ""),
            reason=REASON_PRICING_VERSION_NOT_FOUND,
            providerId=str(provider_id),
            currency=currency,
            modelProfileId=None if model_profile_id is None else str(model_profile_id),
        )
    return version


# ---------------------------------------------------------------------------
# 四、视图组装
# ---------------------------------------------------------------------------


def _budget_view(project_id: uuid.UUID, row: ProjectBudget | None) -> BudgetView:
    """账面行 → 视图。**四值一起返回**（`BudgetView` docstring 的第一条理由）。

    行不存在时返回 `configured=False` 的零值视图，**不抛 404** ——
    "这个项目还没设预算"是 GET 的正常答案（前端要显示"未设置"），
    不是错误。写路径（预占）才需要区分它，那里走 `_require_budget_currency`。
    """
    if row is None:
        return BudgetView(
            project_id=project_id,
            currency=DEFAULT_CURRENCY,
            limit=money.ZERO,
            committed=money.ZERO,
            reserved=money.ZERO,
            available=money.ZERO,
            configured=False,
        )
    limit = money.quantize(row.limit_amount)
    committed = money.quantize(row.committed_amount)
    reserved = money.quantize(row.reserved_amount)
    return BudgetView(
        project_id=project_id,
        currency=row.currency,
        limit=limit,
        committed=committed,
        reserved=reserved,
        available=money.sub(money.sub(limit, committed), reserved),
        configured=True,
    )


def _reservation_view(row: BudgetReservation) -> ReservationView:
    return ReservationView(
        id=row.id,
        project_id=row.project_id,
        state=row.state,
        currency=row.currency,
        amount=money.quantize(row.amount),
        settled_amount=(
            None if row.settled_amount is None else money.quantize(row.settled_amount)
        ),
        ref=row.ref,
        note=row.note,
        estimate_id=row.estimate_id,
    )


def _ledger_view(row: UsageLedger) -> LedgerEntryView:
    return LedgerEntryView(
        id=row.id,
        project_id=row.project_id,
        entry_kind=row.entry_kind,
        currency=row.currency,
        amount=money.quantize(row.amount),
        quantity=None if row.quantity is None else money.quantize_quantity(row.quantity),
        unit=row.unit,
        description=row.description,
    )


def _estimate_lines(raw: Any) -> tuple[EstimateLine, ...]:
    """`cost_estimates.lines`（jsonb 数组）→ 视图行。

    **解析失败不抛错、只跳过**：这是唯一的"容错读取"点，理由是该列
    描述的是**已经发生过的预估**（历史），把一条读不出来的历史行
    变成 500 会让整个预估视图不可用 —— 而它本该只是少显示一行明细。
    跳过的那条已在写入时校验过（见 `estimate_cost`），所以走到这里
    说明是数据被外部改过，记 warning。
    """
    if not isinstance(raw, list):
        logger.warning("cost_estimates.lines 不是数组：%r", type(raw).__name__)
        return ()
    lines: list[EstimateLine] = []
    for item in raw:
        try:
            quantity = money.parse_quantity_wire(item["quantity"])
            unit_price = money.parse_wire_optional(item["unitPrice"], label="unitPrice")
            amount = money.parse_wire_optional(item["amount"], label="amount")
            lines.append(
                EstimateLine(
                    unit=str(item["unit"]),
                    # 显式判 None：`Decimal("0")` 是 falsy，写成 `x or ZERO`
                    # 会让"真的解析出 0"与"解析不出"走同一条路 —— 数值
                    # 恰好相同，但判据（以及后来读这段代码的人）就没法区分了。
                    quantity=money.ZERO if quantity is None else quantity,
                    unit_price=money.ZERO if unit_price is None else unit_price,
                    amount=money.ZERO if amount is None else amount,
                )
            )
        except (KeyError, TypeError, money.MoneyError) as exc:
            logger.warning("跳过一行无法解析的预估明细：%r（%s）", item, exc)
    return tuple(lines)


def _estimate_view(row: CostEstimate) -> CostEstimateView:
    return CostEstimateView(
        id=row.id,
        project_id=row.project_id,
        batch_kind=row.batch_kind,
        quantity=row.quantity,
        currency=row.currency,
        state=row.state,
        total_amount=money.quantize(row.estimated_amount),
        actual_amount=(
            None if row.actual_amount is None else money.quantize(row.actual_amount)
        ),
        variance_amount=(
            None if row.variance_amount is None else money.quantize(row.variance_amount)
        ),
        pricing_version_id=row.pricing_version_id,
        lines=_estimate_lines(row.lines),
    )


def _pricing_version_view(row: PricingVersion) -> PricingVersionView:
    return PricingVersionView(
        id=row.id,
        provider_id=row.provider_id,
        model_profile_id=row.model_profile_id,
        state=row.state,
        currency=row.currency,
        label=row.label,
        effective_from=row.effective_from,
        lines=tuple(
            PricingLine(unit=line.unit, unit_price=money.quantize(line.unit_price))
            for line in _to_pricing_lines(row)
        ),
    )


# ---------------------------------------------------------------------------
# 五、审计（与业务同事务）
# ---------------------------------------------------------------------------


def _audit(
    repo: BillingRepository,
    *,
    action: str,
    object_type: str,
    object_id: uuid.UUID,
    project_id: uuid.UUID | None,
    actor_id: uuid.UUID | None,
    request_id: str | None,
    before: dict[str, Any],
    after: dict[str, Any],
    reason: str | None = None,
) -> None:
    """写一条审计。**只在两端都齐时才写**（照抄 `baseline/service.py` 的判据）。

    `before` / `after` **必须成对**：`audit_events` 有
    `ck_audit_events_hash_pair_consistent`，要求"同时为空或同时非空"。
    "从无到有"用 `{}` 而不是 `None`（`None` 被理解为"这次不记改前"，
    而创建恰恰记的就是"从无到有"）。

    金额进审计负载时用 `money.to_wire` 的字符串 —— 审计摘要虽然只存哈希，
    但 `before` / `after` 会参与哈希；传 `Decimal` 与传字符串会算出不同的
    哈希，而"同一次变更的审计与版本指纹对不对得上"依赖它稳定。
    """
    if actor_id is None or request_id is None:
        return
    record_audit(
        AuditRepository(repo.session),
        action=action,
        object_type=object_type,
        object_id=object_id,
        request_id=request_id,
        actor_kind=ACTOR_KIND_USER,
        actor_id=actor_id,
        project_id=project_id,
        before=before,
        after=after,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# 六、项目存在性
# ---------------------------------------------------------------------------


def _require_project(repo: BillingRepository, project_id: uuid.UUID) -> None:
    """项目必须存在。

    `get_effective_limits` 自己也会校验并抛 404（`settings_service.py:333`），
    但那只覆盖 `estimate_cost` 一条路径 —— 预占、结算、设预算都不经过它。
    统一在这里校验，让"不存在的项目"在所有入口都是同一个 404。
    """
    if not repo.project_exists(project_id):
        raise _not_found(
            f"项目 {project_id} 不存在。",
            reason=REASON_PROJECT_NOT_FOUND,
            projectId=str(project_id),
        )


# ---------------------------------------------------------------------------
# 七、预算账面：读与设
# ---------------------------------------------------------------------------


def get_budget_view(repo: BillingRepository, *, project_id: uuid.UUID) -> BudgetView:
    """读项目预算视图（四值一起）。

    **不校验项目存在性**：这是只读视图，一个"查不到项目"的调用方
    应该得到 404 还是"未配置的零值"？本函数选后者并在 `BudgetView`
    里用 `configured=False` 表达 —— 因为页面要先能渲染，再谈 404。
    调用方若要严格的 404 语义，自己调 `_require_project`
    （`api.py` 的 `ProjectAccessDep` 已经做了这件事）。
    """
    return _budget_view(project_id, repo.get_budget(project_id))


def set_budget(
    repo: BillingRepository,
    *,
    project_id: uuid.UUID,
    limit_amount: Decimal | str | int,
    currency: str = DEFAULT_CURRENCY,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> BudgetView:
    """设/改项目预算额度。**权限是 `OrgAction.MANAGE_BUDGET`**（在 `api.py`）。

    两个拒绝分支分开报，不合并成一句"设不了"：

    | 情形 | reason | 为什么单列 |
    |---|---|---|
    | 已有金额且换币种 | `CURRENCY_MISMATCH` | 换币种会**重新解释已发生的数字**（100 元变 100 美元），是语义变更不是数值变更 |
    | 新额度装不下既有占用 | `BUDGET_EXCEEDED` | 用户的实际意图是"调低上限"，而调低前得先释放占用 —— 这是可操作的指引 |

    这两个条件在 `repository.set_budget_limit` 里都写在 UPDATE 的 WHERE 中，
    所以并发下也正确（两个管理者同时调低，只有一个能成功）。
    """
    _require_project(repo, project_id)
    target_currency = _require_currency(currency)
    limit = _as_amount(limit_amount, label="预算额度")
    if money.is_negative(limit):
        raise _validation_error(
            "limitAmount",
            f"预算额度不得为负：{money.to_wire(limit)}",
            reason=REASON_AMOUNT_NOT_POSITIVE,
        )

    before_row = repo.get_budget(project_id)
    if (
        before_row is not None
        and before_row.currency != target_currency
        and not (
            money.is_zero(before_row.committed_amount)
            and money.is_zero(before_row.reserved_amount)
        )
    ):
        raise _validation_error(
            "currency",
            f"该项目账面已有 {before_row.currency} 的金额，不能改为 {target_currency}"
            "（换币种会重新解释已发生的数字）。请先在零占用时改，或新建项目。",
            reason=REASON_CURRENCY_MISMATCH,
            projectId=str(project_id),
            existingCurrency=before_row.currency,
            requestedCurrency=target_currency,
        )

    repo.ensure_budget(
        project_id=project_id,
        currency=target_currency,
        limit_amount=limit,
        set_by=actor_id,
    )
    if not repo.set_budget_limit(
        project_id=project_id,
        currency=target_currency,
        limit_amount=limit,
        set_by=actor_id,
    ):
        raise _budget_exceeded(
            f"新额度 {money.to_wire(limit)} {target_currency} 装不下该项目已发生的占用"
            "（已结算 + 已预占）。请先释放预占或等待在途任务结束。",
            projectId=str(project_id),
            requestedLimit=money.to_wire(limit),
            currency=target_currency,
        )

    repo.flush()
    _audit(
        repo,
        action=_ACTION_BUDGET_SET,
        object_type=_OBJECT_BUDGET,
        object_id=project_id,
        project_id=project_id,
        actor_id=actor_id,
        request_id=request_id,
        before={} if before_row is None else _budget_audit_payload(before_row),
        after={
            "limitAmount": money.to_wire(limit),
            "currency": target_currency,
        },
        reason=None,
    )
    repo.flush()
    return _budget_view(project_id, repo.get_budget(project_id))


def _budget_audit_payload(row: ProjectBudget) -> dict[str, Any]:
    """账面的审计负载。**只记金额与币种**（不记 `set_by` 之类会自指的字段）。"""
    return {
        "limitAmount": money.to_wire(row.limit_amount),
        "currency": row.currency,
        "committedAmount": money.to_wire(row.committed_amount),
        "reservedAmount": money.to_wire(row.reserved_amount),
    }


# ---------------------------------------------------------------------------
# 八、预占：本包的命门（W10「预算并发预占测试」）
# ---------------------------------------------------------------------------


def reserve_budget(
    repo: BillingRepository,
    *,
    project_id: uuid.UUID,
    amount: Decimal | str | int,
    currency: str = DEFAULT_CURRENCY,
    ref: str | None = None,
    note: str | None = None,
    estimate_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> ReservationView:
    """**唯一预占入口**（WP26/WP27/WP28/WP31 都走这里）。

    ## 三步，顺序不可换

    1. `ensure_budget` —— 保证账面行存在。**必须先做**，
       否则下面那句 `reserve_budget` 的 `rowcount == 0` 有两种成因
       （"行不存在"与"额度不足"），而它们该报不同的错。
    2. `repo.reserve_budget`（单条条件 UPDATE）—— `False` **即**预算不足。
       **不再查一次余量**：再查读到的是"别人已经改过"的账面，反而误导
       （`files/repository.py:100` 原文）。
    3. `insert_reservation` —— 只有拿到额度才落明细，
       于是"每次预占一行"里的每一行都**真的持有过预占**。

    ## 并发正确性为什么成立

    本函数**没有任何进程内状态**参与判断：唯一决定"能不能预占"的是
    第 2 步那条 UPDATE 的 WHERE（`committed + reserved + :delta <= limit`）
    与 `rowcount`。因此它在 API 双实例（ADR-11 C8）下天然正确 ——
    两个实例的本地缓存、读到的余量、之前的任何查询都不影响结论。

    这是 W10「预算并发预占测试」要测的那条性质：
    **N 个并发请求，成功数 × 单笔 <= 限额**。

    ## 额度为 0 的项目

    从未设过预算的项目，`ensure_budget` 会建一个 `limit = 0` 的账面
    （`DEFAULT_BUDGET_LIMIT`），于是这里必然抛 `BUDGET_EXCEEDED`。
    这是**刻意**的：一个非零默认值会让项目凭空拥有额度。
    """
    _require_project(repo, project_id)
    target_currency = _require_currency(currency)
    delta = _require_positive_amount(amount, field="amount")

    before_row = repo.get_budget(project_id)
    if before_row is not None and before_row.currency != target_currency:
        raise _validation_error(
            "currency",
            f"该项目账面的币种是 {before_row.currency}，与本次预占的 "
            f"{target_currency} 不一致（本包不做汇率换算，见 ADR-10 E-5）。",
            reason=REASON_CURRENCY_MISMATCH,
            projectId=str(project_id),
            existingCurrency=before_row.currency,
            requestedCurrency=target_currency,
        )

    repo.ensure_budget(
        project_id=project_id,
        currency=target_currency,
        limit_amount=DEFAULT_BUDGET_LIMIT,
        set_by=None,
    )

    if not repo.reserve_budget(project_id=project_id, delta=delta):
        view = _budget_view(project_id, repo.get_budget(project_id))
        raise _budget_exceeded(
            f"项目预算不足，无法预占 {money.to_wire(delta)} {target_currency}。",
            projectId=str(project_id),
            requestedAmount=money.to_wire(delta),
            currency=target_currency,
            limit=money.to_wire(view.limit),
            committed=money.to_wire(view.committed),
            reserved=money.to_wire(view.reserved),
            available=money.to_wire(view.available),
        )

    row = repo.insert_reservation(
        project_id=project_id,
        amount=delta,
        currency=target_currency,
        estimate_id=estimate_id,
        ref=_require_text(ref, field="ref", limit=MAX_RESERVATION_REF_CHARS),
        note=_require_text(note, field="note", limit=MAX_RESERVATION_NOTE_CHARS),
        created_by=actor_id,
    )
    repo.flush()

    _audit(
        repo,
        action=_ACTION_RESERVATION_CREATE,
        object_type=_OBJECT_RESERVATION,
        object_id=row.id,
        project_id=project_id,
        actor_id=actor_id,
        request_id=request_id,
        before={},
        after={
            "amount": money.to_wire(delta),
            "currency": target_currency,
            "state": row.state,
            "estimateId": None if estimate_id is None else str(estimate_id),
        },
        reason=None,
    )
    repo.flush()
    return _reservation_view(row)


def release_reservation(
    repo: BillingRepository,
    *,
    reservation_id: uuid.UUID,
    reason: str | None = None,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> ReservationView:
    """释放一笔预占（失败 / 取消路径）。**重复调用不生效，且不报错**。

    ## 为什么"重复调用"必须是幂等而不是冲突

    释放是**补偿动作**，它天然会在重试、超时重投、以及"失败路径与超时回收
    同时触发"这三种情形下被多次请求。把它做成 409 会让补偿路径产生
    新的失败 —— 而补偿动作失败意味着钱被永久占住（R3 的后半）。

    但**已结算（`SETTLED`）的预占不能被释放**：那会双重归还
    （结算已经把 `reserved` 转成 `committed` 了）。这一支仍然报 409。

    ## 顺序：先迁移状态，后还钱

    `transition_reservation` 返回 `True` 才继续。反过来会让重复请求
    把账面多还一次，而 `release_budget` 的 `WHERE reserved_amount >= :delta`
    只能挡住"还超余额"，挡不住"同一笔还两次而余额恰好够" —— 那笔钱
    会静默地变多。
    """
    row = repo.get_reservation(reservation_id)
    if row is None:
        raise _not_found(
            f"预占 {reservation_id} 不存在。",
            reason=REASON_RESERVATION_NOT_FOUND,
            reservationId=str(reservation_id),
        )

    previous_state = row.state
    moved = repo.transition_reservation(
        reservation_id=reservation_id,
        from_states=sorted(HOLDING_RESERVATION_STATES),
        to_state=RESERVATION_STATE_RELEASED,
        note=_require_text(reason, field="reason", limit=MAX_RESERVATION_NOTE_CHARS),
    )
    if not moved:
        # 迁移失败 ⇒ 这笔已经不在"持有预占"的状态里了。重读判断是哪一种。
        # （`refresh` 是把"另一个事务刚改过"的当前值读回来；本事务若已
        #  改过它，下面这条分支就走不到了。）
        repo.session.refresh(row)
        if row.state == RESERVATION_STATE_RELEASED:
            # 幂等命中：已经释放过了，直接返回当前状态（不改账面）。
            return _reservation_view(row)
        raise _conflict(
            f"预占 {reservation_id} 当前状态是 {row.state}，不能释放"
            "（已结算的预占释放会造成双重归还）。",
            reason=REASON_RESERVATION_NOT_HOLDING,
            reservationId=str(reservation_id),
            currentState=row.state,
        )

    amount = money.quantize(row.amount)
    if not repo.release_budget(project_id=row.project_id, delta=amount):
        # 明细说"这笔预占持有 {amount}"，而账面说"没这么多可还" ——
        # 这是账面与明细分叉（R10），是**数据缺陷**不是业务结论。
        # 抛 RuntimeError 让事务整体回滚（包括上面那次状态迁移），
        # 于是不会留下"明细说释放了、账面没还"的中间态。
        raise RuntimeError(
            f"账面与预占明细分叉：释放预占 {reservation_id}"
            f"（项目 {row.project_id}，金额 {amount}）时账面不足。"
            "这不该发生 —— 预占与释放是同事务成对的。"
            "请先跑对账（reconciliation.reconcile_project）确认分叉范围。"
        )

    repo.flush()

    _audit(
        repo,
        action=_ACTION_RESERVATION_RELEASE,
        object_type=_OBJECT_RESERVATION,
        object_id=reservation_id,
        project_id=row.project_id,
        actor_id=actor_id,
        request_id=request_id,
        # 用迁移**前**捕获的状态，不用 `row.state`：`transition_reservation`
        # 走的是 core `update()`，它会把身份映射上的这个对象同步成**新**状态，
        # 于是迁移后读 `row.state` 拿到的是 RELEASED —— 审计的"改前"就变成了
        # "改后"，两边一模一样，这条审计从此不再说明任何事（且不会报错）。
        before={"state": previous_state},
        after={"state": RESERVATION_STATE_RELEASED, "amount": money.to_wire(amount)},
        reason=reason,
    )
    repo.flush()
    repo.session.refresh(row)
    return _reservation_view(row)


# ---------------------------------------------------------------------------
# 九、结算：预占 → 实际
# ---------------------------------------------------------------------------


def settle_reservation(
    repo: BillingRepository,
    *,
    reservation_id: uuid.UUID,
    actual_amount: Decimal | str | int,
    entries: Sequence[LedgerEntryInput] = (),
    source_ref: str | None = None,
    note: str | None = None,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> ReservationView:
    """结算一笔预占：`reserved` 转 `committed`，并把实际金额入账。

    ## 允许与实际不等

    预占是"先扣住多少"，结算才是"实际花了多少"（§2.4 第 6 项同一判断）。
    两者不等是**常态**：预占时不知道真实 token 数。

    ## `entries` 为空时自动补一条 `BILL`

    这是本包**最重要的一条自洽约束**：`committed_amount` 的每一次增加，
    账本里都必须有一条对应分录。否则 `recompute_committed`
    （R10 的对账判据）会永远报差异 —— 而"账面有、账本没有"正是
    最该报警的分叉方向（那条钱无凭据可查）。

    ## `entries` 非空时，合计必须等于 `actual_amount`

    两个数是**两条独立路径**算出来的：`actual_amount` 是调用方给的
    业务结论，`Σ entries` 是这一笔花销的构成。它们不等说明
    "结算金额"与"入账明细"各说各话 —— 本包拒绝替调用方选一个当真
    （选错的那个会让账本与账面从此分叉），直接报 422。

    ## 顺序：先 `settle_budget`（一条语句双检查），再迁移状态

    双检查是"归还量够 + 转换后不超限"。若过渡到 `committed` 后会超限，
    `settle_budget` 返回 `False` ⇒ 抛 `BUDGET_EXCEEDED`。
    事务回滚保证此时账面与明细都未变。
    """
    row = repo.get_reservation(reservation_id)
    if row is None:
        raise _not_found(
            f"预占 {reservation_id} 不存在。",
            reason=REASON_RESERVATION_NOT_FOUND,
            reservationId=str(reservation_id),
        )
    if row.state not in HOLDING_RESERVATION_STATES:
        raise _conflict(
            f"预占 {reservation_id} 当前状态是 {row.state}，不能结算。",
            reason=REASON_RESERVATION_NOT_HOLDING,
            reservationId=str(reservation_id),
            currentState=row.state,
        )

    actual = _as_amount(actual_amount, label="实际金额")
    if money.is_negative(actual):
        raise _validation_error(
            "actualAmount",
            f"实际金额不得为负：{money.to_wire(actual)}"
            "（退款走冲正分录，不改历史行的金额）",
            reason=REASON_AMOUNT_NOT_POSITIVE,
        )

    if entries:
        total = money.sum_amounts(entry.amount for entry in entries)
        if total != actual:
            raise _validation_error(
                "entries",
                f"账本分录合计 {money.to_wire(total)} 与实际金额 "
                f"{money.to_wire(actual)} 不一致 —— "
                "两处各说各话会让账面与账本从此分叉，本包不替调用方选一个当真。",
                reason=REASON_LEDGER_TOTAL_MISMATCH,
                reservationId=str(reservation_id),
                ledgerTotal=money.to_wire(total),
                actualAmount=money.to_wire(actual),
            )

    released = money.quantize(row.amount)
    previous_state = row.state
    if not repo.settle_budget(
        project_id=row.project_id, reserved_release=released, committed_add=actual
    ):
        view = _budget_view(row.project_id, repo.get_budget(row.project_id))
        raise _budget_exceeded(
            f"结算 {money.to_wire(actual)} {row.currency} 会让项目超过预算上限。",
            projectId=str(row.project_id),
            reservationId=str(reservation_id),
            actualAmount=money.to_wire(actual),
            limit=money.to_wire(view.limit),
            committed=money.to_wire(view.committed),
            reserved=money.to_wire(view.reserved),
            available=money.to_wire(view.available),
        )

    if not repo.transition_reservation(
        reservation_id=reservation_id,
        from_states=sorted(HOLDING_RESERVATION_STATES),
        to_state=RESERVATION_STATE_SETTLED,
        settled_amount=actual,
        note=_require_text(note, field="note", limit=MAX_RESERVATION_NOTE_CHARS),
    ):
        # settle_budget 已经改了账面，但事务会随这个异常整体回滚 ——
        # 所以不会留下"钱转了但状态没转"的中间态。
        raise _conflict(
            f"预占 {reservation_id} 在结算过程中被并发改动，请重读后重试。",
            reason=REASON_RESERVATION_NOT_HOLDING,
            reservationId=str(reservation_id),
        )

    occurred_at = repo.now()
    to_write = list(entries)
    if not to_write:
        to_write = [
            LedgerEntryInput(
                entry_kind=LEDGER_KIND_BILL,
                amount=actual,
                description=f"预占 {reservation_id} 结算",
                idempotency_key=f"settle:{reservation_id}",
            )
        ]
    written = _insert_ledger_entries(
        repo,
        project_id=row.project_id,
        currency=row.currency,
        entries=to_write,
        source_ref=source_ref,
        occurred_at=occurred_at,
    )

    repo.flush()
    if actor_id is not None and request_id is not None:
        for entry_row in written:
            _audit(
                repo,
                action=_ACTION_LEDGER_APPEND,
                object_type=_OBJECT_LEDGER,
                object_id=entry_row.id,
                project_id=row.project_id,
                actor_id=actor_id,
                request_id=request_id,
                before={},
                after={
                    "entryKind": entry_row.entry_kind,
                    "amount": money.to_wire(entry_row.amount),
                    "currency": entry_row.currency,
                },
                reason=None,
            )
        _audit(
            repo,
            action=_ACTION_RESERVATION_SETTLE,
            object_type=_OBJECT_RESERVATION,
            object_id=reservation_id,
            project_id=row.project_id,
            actor_id=actor_id,
            request_id=request_id,
            # 同 `release_reservation`：用迁移前捕获的状态，
            # 否则 `transition_reservation` 的 core UPDATE 会把 `row.state`
            # 同步成 SETTLED，"改前/改后"写成同一个值。
            before={"state": previous_state, "amount": money.to_wire(released)},
            after={
                "state": RESERVATION_STATE_SETTLED,
                "settledAmount": money.to_wire(actual),
                "ledgerEntryIds": [str(entry_row.id) for entry_row in written],
            },
            reason=None,
        )
    repo.flush()
    repo.session.refresh(row)
    return _reservation_view(row)


def _with_description(
    entry: LedgerEntryInput, description: str | None
) -> LedgerEntryInput:
    """把入参 `description` 并进传值结构（**构造前**完成，不写库后改属性）。

    `LedgerEntryInput` 是 frozen 的，所以这里用 `dataclasses.replace`
    造一个新的 —— 顺带把"入参优先"这条口径写死在代码里：
    `description` 为空时保留结构里原有的业务描述，非空时以入参为准。
    """
    if description is None:
        return entry
    return replace(entry, description=description)


def _is_unique_violation(exc: IntegrityError) -> bool:
    """`IntegrityError` 是不是**唯一约束冲突**（`23505`）。

    只看 sqlstate，不看 message 文本：文本在不同 PG 版本/语言下会变，
    而 `23505` 是 SQL 标准里"唯一约束被违反"的稳定标识
    （`ddl.UNIQUE_VIOLATION_SQLSTATE` 是它的唯一真值来源）。
    """
    return (
        getattr(getattr(exc, "orig", None), "sqlstate", None)
        == ddl.UNIQUE_VIOLATION_SQLSTATE
    )


def _insert_ledger_entry_idempotent(
    repo: BillingRepository,
    *,
    project_id: uuid.UUID,
    currency: str,
    entry: LedgerEntryInput,
    source_ref: str | None,
    occurred_at: datetime,
) -> UsageLedger:
    """写一条带幂等键的分录：**直接 INSERT，撞 `23505` 则回查返回已有那条**。

    ## 为什么必须这样，而不是"先查再插"（CR-1，实测修复）

    本函数的存在是因为曾经的实现是：

        existing = repo.find_ledger_entry_by_idempotency(key)   # ← 先查
        if existing is not None: return existing
        ...INSERT...                                            # ← 再插

    这在**并发**下是错的。两个请求同时查到"不存在"（窗口就在这两行之间），
    于是都去 INSERT，其中一边撞 `uq_usage_ledger_idempotency`，
    `IntegrityError` 被 `session_scope` 原样抛出 ⇒ 调用方看到 **HTTP 500**。

    实测（`/tmp/wp25_probe_idem.py`，8 线程同键）：

        返回成功 : 3   ids=['d04daf03-...']   （只有同一个 id）
        抛异常   : 5   ['IntegrityError/23505']

    而"同一笔账单重复导入"在业务上**完全正常**，报 500 是错的。
    `repository.find_ledger_entry_by_idempotency` 的 docstring 早就写明了
    正确流程（"直接 INSERT → 撞 23505 → 回查。**而不是**先查再插"），
    但 `service` 侧没照它做 —— 这是 ISS-95 的形态：
    **"声称自己有防线"比"没有防线"更危险**，因为看代码的人会以为它已处理。

    ## 为什么先查一次仍然保留（当下的调用点）

    先查一次是**快路径**（重复导入的常见形态是"早就记过了"，此时省一次
    必然失败的 INSERT 与一次 SAVEPOINT 回滚）。但它**不能替代**
    下面的 `except` —— 只有 `except` 才是并发正确性的来源。
    两者并存不是冗余：快路径处理"串行重复"，慢路径处理"并发重复"。

    ## 为什么用 SAVEPOINT（`begin_nested`）

    `23505` 会让**整个事务**进入 aborted 状态（PG 语义：事务内任一语句
    失败后，后续语句一律报 `25P02`）。若直接 `except` 再查，那条 SELECT
    会立刻失败。`begin_nested()` 给这次 INSERT 套一层 SAVEPOINT，
    失败只回滚到 SAVEPOINT，外层事务仍然可用 —— 于是回查能成功。

    这也是"不能简单 `try/except` 后重试"的原因：必须显式划出可回滚的子边界。

    ## 回查仍为空的情形

    理论上可能（该行在 `23505` 之后被别的事务删除），但本表**不可变**
    （DELETE 被 `trg_usage_ledger_immutable` 拒成 `23001`），所以不可能。
    真出现就说明防线出了问题，此处**显式报错**而不是返回 None
    —— 静默返回 None 会让调用方拿到一个"成功但什么都没有"的视图。
    """
    try:
        with repo.session.begin_nested():
            written = repo.insert_ledger_entry(
                project_id=project_id,
                entry_kind=entry.entry_kind,
                currency=currency,
                amount=money.quantize(entry.amount),
                occurred_at=occurred_at,
                quantity=(
                    None
                    if entry.quantity is None
                    else money.quantize_quantity(entry.quantity)
                ),
                unit=entry.unit,
                reverses_ledger_id=entry.reverses_ledger_id,
                source_ref=_require_text(
                    source_ref, field="sourceRef", limit=MAX_LEDGER_SOURCE_REF_CHARS
                ),
                idempotency_key=entry.idempotency_key,
                description=_require_text(
                    entry.description,
                    field="description",
                    limit=MAX_LEDGER_DESCRIPTION_CHARS,
                ),
            )
            repo.flush()
    except IntegrityError as exc:
        if not _is_unique_violation(exc):
            raise  # 别的完整性错误（FK / CHECK）是真问题，原样冒出去
        # ⚠️ 这里**不能写 `assert`**（CR-1 MINOR-2）：`python -O` 会剥离 assert，
        # 防线凭空消失；此后 `find_ledger_entry_by_idempotency(None)` 会渲染成
        # `WHERE idempotency_key IS NULL` ⇒ 返回**任意一条无键分录**，
        # 调用方拿到一个"成功但对象是别人的"视图 —— 静默错值，最难查的一类。
        # 用显式抛错：无键不可能撞部分唯一索引（`WHERE idempotency_key IS NOT NULL`），
        # 真走到这里说明键的传递链断了。
        if entry.idempotency_key is None:
            raise RuntimeError(
                "无幂等键却撞了唯一约束 —— 部分唯一索引的 WHERE 是"
                " `idempotency_key IS NOT NULL`，无键不可能触发。"
                "说明键在传递链上被丢了，请排查调用方。"
            ) from exc
        existing = repo.find_ledger_entry_by_idempotency(entry.idempotency_key)
        if existing is None:
            raise RuntimeError(
                f"幂等键 {entry.idempotency_key!r} 撞了唯一约束，但回查不到那一行"
                "—— 本表不可变（DELETE 被触发器拒），这不该发生；"
                "若出现说明不可变防线失效，请立即排查。"
            ) from exc
        return existing
    return written


def _insert_ledger_entries(
    repo: BillingRepository,
    *,
    project_id: uuid.UUID,
    currency: str,
    entries: Sequence[LedgerEntryInput],
    source_ref: str | None,
    occurred_at: datetime,
) -> list[UsageLedger]:
    """把若干条分录写进账本。**`occurred_at` 与 `source_ref` 由调用方统一给**。

    同一笔结算的几条分录必须落在同一时刻 —— 否则按时间聚合的报表
    会把它算成两笔（`LedgerEntryInput` docstring 的第一条理由）。

    文本长度守卫在这里做，不在调用方：`ref` / `description` 超长在 DB 层
    是 `22001`（500 "服务器内部错误"），在这里是 422（可指到具体字段）。
    两个 `MAX_LEDGER_*_CHARS` 常量只有这里有消费者 —— 放在别处就会有
    某条调用路径漏掉（本包有 `settle_reservation` 与 `append_ledger_entry`
    两个入口，`settle_reservation` 的 `source_ref` 来自上游 Worker）。
    """
    return [
        repo.insert_ledger_entry(
            project_id=project_id,
            entry_kind=entry.entry_kind,
            currency=currency,
            amount=money.quantize(entry.amount),
            occurred_at=occurred_at,
            quantity=(
                None if entry.quantity is None else money.quantize_quantity(entry.quantity)
            ),
            unit=entry.unit,
            reverses_ledger_id=entry.reverses_ledger_id,
            source_ref=_require_text(
                source_ref, field="sourceRef", limit=MAX_LEDGER_SOURCE_REF_CHARS
            ),
            idempotency_key=entry.idempotency_key,
            description=_require_text(
                entry.description,
                field="description",
                limit=MAX_LEDGER_DESCRIPTION_CHARS,
            ),
        )
        for entry in entries
    ]


# ---------------------------------------------------------------------------
# 十、账本：人工分录（调整 / 冲正）
# ---------------------------------------------------------------------------


def append_ledger_entry(
    repo: BillingRepository,
    *,
    project_id: uuid.UUID,
    entry: LedgerEntryInput,
    source_ref: str | None = None,
    description: str | None = None,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> LedgerEntryView:
    """追加一条人工分录（`ADJUSTMENT` / `REVERSAL`）。

    **冲正用新分录，不改旧行**（R5）：`usage_ledger` 是不可变表，
    `UPDATE` / `DELETE` 会被触发器拒成 `23001`。"这笔钱记错了"的表达是
    "再记一条负数分录并引用它"，于是**错的那笔在账本里永远看得见** ——
    这正是审计要求"与费用账本同等级对待"的含义。

    幂等：`idempotency_key` 非空时，重复追加会撞
    `uq_usage_ledger_idempotency`（`23505`）。这里把它当成
    **正常的并发结果**（"这笔已经记过了"），回查并返回已有那条 ——
    与 `23001`（防线被违反）是完全不同的两件事。

    `description` 是这条分录的**文本**（区别于传入结构里 `entry.description`
    的业务描述）：二者想表达同一件事，故 `entry.description` 为空时
    用入参补上，非空时以入参为准 —— 拼接发生在**构造之前**，
    不写库后再改 ORM 属性（那样 `flush` 与改属性之间存在一个
    "库里已有值、内存已改过"的窗口，对本仓不可变表尤其危险）。
    """
    _require_project(repo, project_id)
    # 快路径：串行重复（常见形态）直接返回已有那条，省一次必然失败的 INSERT。
    # **并发正确性不来自这里** —— 它来自 `_insert_ledger_entry_idempotent`
    # 里的 `23505` 捕获。CR-1 实测：只靠这两行会让 8 线程中的 5 个冒 500。
    if entry.idempotency_key is not None:
        existing = repo.find_ledger_entry_by_idempotency(entry.idempotency_key)
        if existing is not None:
            return _ledger_view(existing)

    budget = repo.get_budget(project_id)
    currency = DEFAULT_CURRENCY if budget is None else budget.currency

    # 并发正确性**来自这里**（`23505` → SAVEPOINT 回滚 → 回查已有那条）。
    # 上面那两行快路径只是省一次必然失败的 INSERT，不能替代本调用。
    written = _insert_ledger_entry_idempotent(
        repo,
        project_id=project_id,
        currency=currency,
        entry=_with_description(entry, description),
        source_ref=source_ref,
        occurred_at=repo.now(),
    )

    _audit(
        repo,
        action=_ACTION_LEDGER_APPEND,
        object_type=_OBJECT_LEDGER,
        object_id=written.id,
        project_id=project_id,
        actor_id=actor_id,
        request_id=request_id,
        before={},
        after={
            "entryKind": written.entry_kind,
            "amount": money.to_wire(written.amount),
            "currency": written.currency,
        },
        reason=None,
    )
    repo.flush()
    return _ledger_view(written)


# ---------------------------------------------------------------------------
# 十一、费用预估
# ---------------------------------------------------------------------------


def estimate_cost(
    repo: BillingRepository,
    *,
    project_id: uuid.UUID,
    batch_kind: str,
    quantity: int,
    provider_id: uuid.UUID,
    currency: str = DEFAULT_CURRENCY,
    model_profile_id: uuid.UUID | None = None,
    metrics: dict[str, Decimal | str | int] | None = None,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> CostEstimateView:
    """预估一次批次要花多少钱。**不预占**（预占是 `reserve_budget`）。

    ## 数量越界读 WP24 的生效配置

    走 `planning.settings_service.get_effective_limits` —— 它是**全仓唯一的
    配置读取路径**（会按需物化项目副本，因此本函数必须在写事务里调）。
    越界抛 `QuantityOutOfRange`，按其 `reason` 翻成 422。
    本包**不重写一份数量校验**：那会让"10/30/50"这条 G3 硬约束
    出现第二个真值来源，而两个来源必然分叉。

    ## `provider_id` 为什么必须由调用方给

    `cost_estimates` 表**没有 provider 列**（数据结构 02 §11 的列清单里没有），
    所以"用哪个供应商的价"这件事只能从入参来。本包不设"默认供应商"：
    一个默认值会在多供应商时静默用错单价，而账面完全正常
    （与 `pricing.price_for` 拒绝"缺失即算 0"同一理由）。

    ## 没有 `metrics` 时只能按次计价

    本包**不自己数 token**（§1.5 砍掉清单：自己数是第二真值来源，
    与供应商账单必然分叉）。所以"50 篇要花多少"这件事必须由调用方
    给出用量组成（`{"input_tokens": …, "output_tokens": …}`）。
    若调用方只说"50 篇"，唯一能算的口径是"每篇一个价"（`CALL` 单位）——
    计价版本里没有 `CALL` 单价时**报错**，而不是按 0 算。
    """
    _require_project(repo, project_id)
    target_currency = _require_currency(currency)
    _require_reasonable_quantity(quantity)

    # --- 数量上限：读 WP24 的生效配置（唯一的数量口径来源） ---
    from rednote.modules.planning.limits import QuantityOutOfRange, validate_quantity
    from rednote.modules.planning.repository import PlanningRepository
    from rednote.modules.planning.settings_service import get_effective_limits
    from rednote.modules.planning.types import SETTING_KEY_BATCH_LIMITS

    setting = get_effective_limits(
        PlanningRepository(repo.session), SETTING_KEY_BATCH_LIMITS, project_id
    )
    try:
        batch_limits = setting.as_limits()
    except ValueError as exc:
        raise _validation_error(
            "quantity",
            f"批量数量上限尚未配置（配置键 {SETTING_KEY_BATCH_LIMITS} 为空）：{exc}",
            reason=REASON_LIMITS_NOT_CONFIGURED,
            projectId=str(project_id),
            source=setting.source,
        ) from exc

    try:
        validate_quantity(batch_limits, batch_kind, quantity, source=setting.source)
    except QuantityOutOfRange as exc:
        # 越界的两条原因（低于下界 / 高于上限）来自同一个 `reason` 字段，
        # 由 `planning` 提供 —— 本包不重写一份判断，只做翻译。
        raise _validation_error(
            "quantity",
            str(exc),
            reason=exc.reason,
            projectId=str(project_id),
            batchKind=exc.batch_kind,
            quantity=exc.quantity,
            limit=exc.limit,
            source=exc.source,
        ) from exc

    # --- 单价：该时点该供应商的可用版本 ---
    at = repo.now()
    version = _require_pricing_version(
        repo,
        provider_id=provider_id,
        currency=target_currency,
        model_profile_id=model_profile_id,
        at=at,
    )
    price_map = pricing.build_price_map(_to_pricing_lines(version))

    # --- 用量组成 → 金额 ---
    if metrics is None:
        unit_price = pricing.call_unit_price(price_map)
        if unit_price is None:
            raise _validation_error(
                "metrics",
                f"计价版本 {version.id} 没有 CALL（按次）单价，"
                "因此无法仅凭数量估算。请提供 metrics（各计量单位的预计用量）——"
                "本包不自己数 token，自己数会与供应商账单分叉。",
                reason=REASON_PRICING_INCOMPLETE,
                pricingVersionId=str(version.id),
            )
        resolved_metrics: dict[str, Decimal] = {"calls": Decimal(quantity)}
    else:
        resolved_metrics = {key: money.as_quantity(value) for key, value in metrics.items()}

    try:
        lines = pricing.estimate_for_metrics(prices=price_map, metrics=resolved_metrics)
        total = pricing.total_of(lines)
    except (pricing.PricingError, money.MoneyError) as exc:
        raise _validation_error(
            "metrics",
            f"无法按计价版本 {version.id} 算出金额：{exc}",
            reason=REASON_PRICING_INCOMPLETE,
            pricingVersionId=str(version.id),
        ) from exc

    row = repo.insert_estimate(
        project_id=project_id,
        batch_kind=batch_kind,
        quantity=quantity,
        currency=target_currency,
        estimated_amount=total,
        lines=[
            {
                "unit": line.unit,
                "quantity": money.quantity_to_wire(line.quantity),
                "unitPrice": money.to_wire(line.unit_price),
                "amount": money.to_wire(line.amount),
            }
            for line in lines
        ],
        pricing_version_id=version.id,
        created_by=actor_id,
        state=None,  # 走列默认值 DRAFT（默认值只写在 models.py 一处）
    )
    repo.flush()

    _audit(
        repo,
        action=_ACTION_ESTIMATE_CREATE,
        object_type=_OBJECT_ESTIMATE,
        object_id=row.id,
        project_id=project_id,
        actor_id=actor_id,
        request_id=request_id,
        before={},
        after={
            "batchKind": batch_kind,
            "quantity": quantity,
            "estimatedAmount": money.to_wire(total),
            "currency": target_currency,
            "pricingVersionId": str(version.id),
        },
        reason=None,
    )
    repo.flush()
    return _estimate_view(row)


def _require_reasonable_quantity(quantity: int) -> None:
    """数量必须是正整数。**上限不在这里判** —— 它走 WP24 的配置。

    这里只挡住"类型不对"与"非正"（那两种情形走到
    `validate_quantity` 会得到 README 里那条
    `REASON_QUANTITY_BELOW_MIN`，但错误信息会指向"配置的最小值"，
    而真实原因是调用方传了 0 或字符串 —— 分开报更准确）。
    """
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise _validation_error(
            "quantity",
            f"数量必须是整数，收到 {type(quantity).__name__}",
            reason=REASON_QUANTITY_BELOW_MIN,
        )
    if quantity < 1:
        raise _validation_error(
            "quantity",
            f"数量必须为正整数，收到 {quantity}",
            reason=REASON_QUANTITY_BELOW_MIN,
        )


# ---------------------------------------------------------------------------
# 十二、只读查询
# ---------------------------------------------------------------------------


def list_project_reservations(
    repo: BillingRepository,
    *,
    project_id: uuid.UUID,
    states: Sequence[str] | None = None,
    limit: int = 100,
) -> list[ReservationView]:
    """列项目预占明细（新→旧）。`states=None` 表示全部状态。"""
    return [
        _reservation_view(row)
        for row in repo.list_reservations(project_id=project_id, states=states, limit=limit)
    ]


def list_project_ledger(
    repo: BillingRepository,
    *,
    project_id: uuid.UUID,
    since: datetime | None = None,
    limit: int = 100,
) -> list[LedgerEntryView]:
    """列项目账本分录（按 `occurred_at` 排序）。"""
    return [
        _ledger_view(row)
        for row in repo.list_ledger_entries(project_id=project_id, since=since, limit=limit)
    ]


def get_estimate(repo: BillingRepository, *, estimate_id: uuid.UUID) -> CostEstimateView:
    """读一份预估。不存在抛 404。"""
    row = repo.get_estimate(estimate_id)
    if row is None:
        raise _not_found(
            f"预估 {estimate_id} 不存在。",
            reason=REASON_ESTIMATE_NOT_FOUND,
            estimateId=str(estimate_id),
        )
    return _estimate_view(row)


def list_project_estimates(
    repo: BillingRepository,
    *,
    project_id: uuid.UUID,
    states: Sequence[str] | None = None,
    limit: int = 100,
) -> list[CostEstimateView]:
    """列项目预估（新→旧）。"""
    return [
        _estimate_view(row)
        for row in repo.list_estimates(project_id=project_id, states=states, limit=limit)
    ]


def list_pricing_versions(
    repo: BillingRepository,
    *,
    provider_id: uuid.UUID | None = None,
    state: str | None = None,
    limit: int = 100,
) -> list[PricingVersionView]:
    """列计价版本（新→旧）。**`unit_prices` 里的单价在这里被解析成 `Decimal`**，
    解析失败的行会被跳过（历史数据），见 `_to_pricing_lines` 的调用点。"""
    out: list[PricingVersionView] = []
    for row in repo.list_pricing_versions(
        provider_id=provider_id, state=state, limit=limit
    ):
        try:
            out.append(_pricing_version_view(row))
        except ApiError:
            logger.warning("跳过一份无法解析的计价版本：%s", row.id)
    return out


__all__ = [
    "DEFAULT_BUDGET_LIMIT",
    "DEFAULT_CURRENCY",
    "append_ledger_entry",
    "estimate_cost",
    "get_budget_view",
    "get_estimate",
    "list_pricing_versions",
    "list_project_estimates",
    "list_project_ledger",
    "list_project_reservations",
    "release_reservation",
    "reserve_budget",
    "set_budget",
    "settle_reservation",
]

