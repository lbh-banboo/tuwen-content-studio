"""WP26 的第二条接缝：把内容生成接进 WP25 的计价 / 预算 / 账本。

**为什么需要这个文件**（说明文档 §1.4 第 7 条 / §2.6 R2）：`billing/service.py`
的 `reserve_budget` docstring **点名了本包**（"WP26/WP27/WP28/WP31 都走这里"），
但"预占多少"这件事**上游没有依据** —— `estimate_cost` 要求调用方给
`provider_id` 与 `quantity`，而 `quantity` 的算法只在本包手里。

**处置：本模块把上游四个签名包成稳定入口，并把唯一的数量口径钉在里面。**
若将来 WP25 改了签名，改这一处即可；`service.py` 不直接 import `billing`。

## 三条本模块特有的约定

1. **数量口径只有一处，且就在本模块的调用点**（§2.6 R3）。
   `quantity` 一律由 `content/types.py` 的 `calls_for(task_kind, content_count)`
   算出，**调用方给的是"内容条数"而不是"调用次数"**。
   这样做的理由：若把 `quantity` 做成入参，调用方就能绕过 `calls_for` 自己乘一次
   —— 而"少乘一次"或"多乘一次"都不会报错，只会在对账时表现为"账面永远差一笔"。
   把入参定成 `content_count`，就让"算错次数"变成**不可能**，而不是"不推荐"。

2. **预占与预估绑死**。`reserve_generation_budget` 只接受
   `estimate: CostEstimateView` 而不是裸金额 —— 于是
   `estimate_id` 不可能漏填、预占金额不可能与预估金额不一致。
   上游允许两者不等（`amount` 与 `estimate_id` 是两个独立入参），
   但**本包没有"要预占一个预估之外的金额"这个需求**：不需要的能力不加参数。

3. **上游的 `ApiError` 原样透传，本模块不包装。**
   `billing/service.py` 的错误（`BUDGET_EXCEEDED` / `VALIDATION_FAILED` /
   `RESOURCE_NOT_FOUND` / `INVALID_TRANSITION`）都是 `ApiError`，
   带 `details.reason`，由 API 层翻成 4xx。**本模块不把它们翻成本模块的错误码**
   —— 那会把两个模块的错误码体系接在一起，而 WP25 的 `reason`
   （`currency_mismatch` / `pricing_incomplete` / …）本来就已经是给前端用的。

## 与 R2（先生成后记账）的关系

本模块只提供**原语**，"顺序"由 `service.py` 定死：
`estimate_generation_cost` → `reserve_generation_budget` → `dispatch` → `settle`。
R2 的判据（额度为 0 的项目上 `dispatch_call` 一次都没被调用）打在
`service.py` 的编排上，不打在本模块 —— 本模块只保证"每一次预占都真的持有过额度"
（`reserve_budget` 的三步语义）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from rednote.modules.billing.service import (
    DEFAULT_CURRENCY,
    estimate_cost,
    release_reservation,
    reserve_budget,
    settle_reservation,
)
from rednote.modules.billing.types import (
    CostEstimateView,
    LedgerEntryInput,
    ReservationView,
)
from rednote.modules.billing.repository import BillingRepository
from rednote.modules.content.types import calls_for


#: 预算引用前缀。形状 `content:<content_id>`。
#:
#: **为什么带前缀而不是裸 UUID**：同一个项目上将来会有别的包预占
#: （WP27 的静态图、WP28 的多媒体），前缀让账面明细能按来源分堆 ——
#: 与 `usage_ledger.source_ref` 的 `attempt:` / `bill:` / `manual:`
#: 同一种手法（那是 WP25 已有的惯例，本包沿用而不另创一套）。
RESERVATION_REF_PREFIX: str = "content:"


def _reservation_ref(content_id: uuid.UUID) -> str:
    """`content:<content_id>`。**唯一**的 `ref` 构造点。

    抽成函数而不是在各处拼字符串：`ref` 是运维从账面行找回业务的
    唯一线索，两处拼接分叉的后果是"有一半的预占按 `ref` 查不到"。
    """
    return f"{RESERVATION_REF_PREFIX}{content_id}"


def estimate_generation_cost(
    billing_repo: BillingRepository,
    *,
    project_id: uuid.UUID,
    task_kind: str,
    content_count: int,
    batch_kind: str,
    provider_id: uuid.UUID,
    currency: str = DEFAULT_CURRENCY,
    model_profile_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> CostEstimateView:
    """预估"`task_kind` 跑 `content_count` 条内容"要花多少钱。**不预占**。

    三个入参的来历，各自都不在本模块发明：

    | 入参 | 谁定的 | 本模块做什么 |
    |---|---|---|
    | `task_kind` | 本包两个取值（`CONTENT_STRUCTURE` / `CONTENT_COPY`） | 交给 `calls_for` 换算次数 |
    | `content_count` | 调用方（一次生成请求里的内容条数） | **不直接当 `quantity`**，先过 `calls_for` |
    | `batch_kind` | WP24 的批次档位（`TRIAL` / `SCALE` / `BULK`） | 原样透传，**不由本模块从 `content_count` 反推** |

    **为什么 `batch_kind` 必须由调用方给、不能从 `content_count` 反推**：
    `estimate_cost` 会拿它去 `planning.validate_quantity` 校验数量上界
    （G3 的 10/30/50 硬约束）。若本模块"按条数猜档位"，实际效果是
    **猜出来的档位必然容纳这个条数** ⇒ 那次校验恒过，G3 的硬约束在本包这条路径上
    **被静默关掉**。档位是**请求的属性**（用户选试产/扩产/批产），不是条数的函数。

    **`quantity` 为什么是"每个 task_kind 单独一次预占"**：`calls_for` 是
    **按 kind** 的（`TASK_CONTENT_STRUCTURE` = n 次、`TASK_CONTENT_COPY` = n 次）。
    一次 `FULL` 重做会**调两次本函数**（两个 kind 各一次），于是
    两个 kind 各自预估、各自预占 —— 与说明文档 §1.7 前端消费面
    「两个 `task_kind` 的独立预估与预占」一致。**不合成一次**：
    合成会让"只重做标题"也按整篇预占，而这正是 §1.3 否决 V1 的理由之一。

    `estimate_cost` **必须在写事务里调**（它按需物化项目配置副本）——
    事务边界由调用方控制，本模块不开事务。
    """
    quantity = calls_for(task_kind, content_count)
    return estimate_cost(
        billing_repo,
        project_id=project_id,
        batch_kind=batch_kind,
        quantity=quantity,
        provider_id=provider_id,
        currency=currency,
        model_profile_id=model_profile_id,
        # `metrics=None` ⇒ 走"按次计价"（`CALL` 单价）。
        # **本包只按次预估，不自己数 token**（§1.5 砍掉清单：自己数是第二真值来源，
        # 与供应商账单必然分叉）。计价版本没有 `CALL` 单价时上游会报错而不是按 0 算
        # —— 那个行为正是本包要的。
        metrics=None,
        actor_id=actor_id,
        request_id=request_id,
    )


def reserve_generation_budget(
    billing_repo: BillingRepository,
    *,
    project_id: uuid.UUID,
    estimate: CostEstimateView,
    content_id: uuid.UUID | None = None,
    currency: str | None = None,
    ref: str | None = None,
    note: str | None = None,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> ReservationView:
    """按 `estimate` 预占额度。**必须在上游调用发出之前调用**（R2）。

    `currency` 缺省跟随 `estimate.currency`：预估与预占用同一个币种，
    而"两处各写一个币种"正是 `reserve_budget` 会报 `CURRENCY_MISMATCH`
    的那类不一致（本包不做汇率换算）。

    **跨项目拼接是一个编程错误，不是用户错误**：`estimate` 属于 B 项目、
    而 `project_id` 传了 A 项目时，预占行会落在 A 项目、却引用 B 项目的预估
    （`cost_estimates` 没有 FK 到项目预算，DB 拦不住）。本模块显式拒掉，
    而不是让它静默落库 —— 那种行在报表上表现为"某项目的预估凭空出现在另一个项目"。

    ## `ref` 由 `content_id` 派生，**不留给调用方拼**

    `budget_reservations.ref` 是自由文本（`billing/models.py` 明写"引用对象跨包，
    建 FK 会让本迁移依赖那些包的落地顺序"），因此 DB 拦不住"拼错前缀"。
    若让各调用点自己拼，就会有两种写法 —— 而运维从账面行找回内容条目
    靠的**只有**这一个字段，拼错的那一半会永久查不回（不报错）。
    故本函数把 `ref` 的构造压成 `_reservation_ref()` 一处：
    调用方给 `content_id`，`ref` 由本函数生成；确实要覆盖时才显式传 `ref`。
    """
    if estimate.project_id != project_id:
        raise ValueError(
            "预占的项目与预估的项目不一致："
            f"project_id={project_id}，estimate.project_id={estimate.project_id}"
            f"（estimate={estimate.id}）。这是调用方的编程错误，不是用户输入问题。"
        )

    resolved_ref = ref
    if resolved_ref is None and content_id is not None:
        resolved_ref = _reservation_ref(content_id)

    return reserve_budget(
        billing_repo,
        project_id=project_id,
        amount=estimate.total_amount,
        currency=currency if currency is not None else estimate.currency,
        ref=resolved_ref,
        note=note,
        estimate_id=estimate.id,
        actor_id=actor_id,
        request_id=request_id,
    )


def settle_generation_budget(
    billing_repo: BillingRepository,
    *,
    reservation_id: uuid.UUID,
    actual_amount: Decimal | str | int,
    entries: Sequence[LedgerEntryInput] = (),
    source_ref: str | None = None,
    note: str | None = None,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> ReservationView:
    """按**实际花费**结算（预占与实际不等是常态 —— 预占时不知道真实 token 数）。

    **`actual_amount` 与 `entries` 的关系由上游定死**：`entries` 为空时上游
    自动补一条 `BILL` 分录（`idempotency_key = f"settle:{reservation_id}"`）；
    `entries` 非空时 `Σ entries` 必须等于 `actual_amount`，否则上游报
    `LEDGER_TOTAL_MISMATCH`。本模块**不替调用方选一个当真** —— 两个数各说各话时
    报错比"猜一个"安全（猜错的那个会让账面与账本从此分叉）。

    ## ⚠️ `actual_amount` 在本机**没有可信来源**（实测，不是推测）

    本函数**不提供**"自己去网关拿实际金额"的能力，因为那个来源不存在：

    | 可能的位置 | 实测结论 |
    |---|---|
    | `CapabilityResult.cost_amount` / `usage` | **没有任何落库位置** —— `billing/types.py` 的 `PendingUsage` docstring 原话 |
    | `provider_calls` 表 | 无 cost / usage 列（本会话逐列核对） |
    | `CallView`（`dispatch_call` 的返回） | 无 cost 字段 |
    | `dispatch_call` 的 `DispatchResult` | 无 cost 字段 |

    因此本包在**成功**路径上按 `estimate.total_amount` 结算，
    并把这个口径写进 `settle` 的 `note`。这是"如实报数"，
    与 WP25 拒绝"缺失即算 0"同一取向：宁可账面显示预估值，
    也不要编一个看起来精确的真实金额。

    **将来补上 cost 落库位置时**，本函数是唯一要改的地方（它已经收下了
    `actual_amount` 这个入参，改的是**调用方从哪里取它**）。
    在补上之前，任何声称"本包按真实花费结算"的说法都是
    ISS-95 形态的"声称有防线"—— 本 docstring 显式否掉它。
    """

    return settle_reservation(
        billing_repo,
        reservation_id=reservation_id,
        actual_amount=actual_amount,
        entries=entries,
        source_ref=source_ref,
        note=note,
        actor_id=actor_id,
        request_id=request_id,
    )


def release_generation_budget(
    billing_repo: BillingRepository,
    *,
    reservation_id: uuid.UUID,
    reason: str | None = None,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> ReservationView:
    """释放一笔预占（失败 / 取消 / 出网被拒的补偿路径）。

    **上游语义：重复调用不生效且不报错**（补偿动作天然会被重试多次，
    做成 409 会让补偿路径产生新的失败，而补偿失败意味着钱被永久占住）。
    已结算（`SETTLED`）的预占**不能**被释放 —— 那会双重归还。

    本包在两条路径上用它：① `denied`（出网被策略拒，一次调用都没发出，**全额释放**）；
    ② 派发失败 / `UNKNOWN`（本次没有可信的实际金额，**先释放**，
    真实花费由 WP16 的对账路径处理）。**`UNKNOWN` 不是终态**（R8），
    不能当失败而重发 —— 释放额度与"重发"是两件事，本模块只做前者。
    """

    return release_reservation(
        billing_repo,
        reservation_id=reservation_id,
        reason=reason,
        actor_id=actor_id,
        request_id=request_id,
    )


__all__ = [
    "RESERVATION_REF_PREFIX",
    "estimate_generation_cost",
    "release_generation_budget",
    "reserve_generation_budget",
    "settle_generation_budget",
]
