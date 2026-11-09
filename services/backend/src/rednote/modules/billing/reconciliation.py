"""费用对账 —— **只读**，不写库，不加锁。

## 两条**互相独立**的判据

| 判据 | 问的问题 | 数据来源 |
|---|---|---|
| 未结用量（R4） | "上游说有没用完的用量，账本收下了吗？" | `job_attempts.usage_pending` × `usage_ledger.source_ref` |
| 账面分叉（R10） | "账面数字与明细重算出来的一样吗？" | `project_budgets` × `budget_reservations` / `usage_ledger` |

**为什么必须两条**：它们回答的是两种不同的事故。

  · 只有未结用量：一笔**从未入账**的费用查不出来（账面与"它自己认可
    的明细"完全一致，因为两边都没有这笔）。
  · 只有分叉：一笔**入账了但状态没转**的钱查不出来 —— 账本与账面吻合，
    可预占明细里它还占着额度。

两条都不报才是真的健康（`ReconciliationReport.healthy` 要求
`pending` 与 `drifts` **同时**为空）。

## 本模块刻意不做的事

1. **不自动结算、不自动挂账。** 文档 §1.6 R4 的"CHK-4"要求
   "`UNKNOWN` 一律挂账等人核对，**不自动落终态**" —— 自动落终态的
   后果是"猜错了也没人知道"，而这里恰恰是全系统唯一能发现
   "上游说没用完、账本没有"的地方。本模块只**报告**，处置由运维
   走 `service.append_ledger_entry`（人工分录）或
   `service.settle_reservation`（如果那笔确实对应一笔预占）。
2. **不引入容差。** 金额是定点数，两边用同一个 `money.sum_amounts`
   语义算出来，不等就是真的不等（`DriftReport` docstring 原文）。
3. **不持锁。** 对账是读，读到的是某一瞬间的快照；它**不保证**
   那一瞬间之后不再变。要求"对账期间冻结写入"会让这个功能
   永远不敢在生产上跑。判据的正确用法是：对账跑绿 =
   "在那个时刻，两处一致"，而不是"从此永远一致"。

## 判据与上游同源（R4 的处置）

"这条 attempt 是否未结"的口径**不在本模块重新定义**：

  · 上半边（"上游说未结"）= `job_attempts.usage_pending = true`，
    由 `gateway/handlers.py::_usage_pending` 写入
    （"有过用量，且计费未确认"）。
  · 下半边（"账本收下了吗"）= `usage_ledger` 里有没有**确认性**分录。

本模块只消费这两个既成事实，不另写一个判断。文档 OP-108 的原话：
"判据**与 `gateway/handlers._usage_pending` 同源**，本包不另写一个判断"。
"""

from __future__ import annotations

import uuid
from typing import Final

from rednote.modules.billing import money
from rednote.modules.billing.models import UsageLedger
from rednote.modules.billing.repository import (
    DEFAULT_PENDING_SCAN_LIMIT,
    BillingRepository,
)
from rednote.modules.billing.types import (
    LEDGER_KIND_BILL,
    DriftReport,
    PendingUsage,
    ReconciliationReport,
)

#: **"这条 attempt 已结"的判据**：账本里有一条 `BILL` 分录。
#:
#: ## 为什么不是"有任意一条分录"
#:
#: 三个类型定义必须同时成立，只有这一个读法做得到：
#:
#: 1. `ReconciliationReport.pending` 的定义是"仍然未结
#:    （**要么没分录，要么分录未确认**）" —— 后半句明确承认
#:    "有分录但仍未结"是一种真实形态。
#: 2. `PendingUsage.available` 的定义是"账本里已有对应分录，
#:    金额取自那里"；若"有分录即已结"，`pending` 里的元素就永远
#:    `available=False`，这个字段连同 `ledger_entry_id` /
#:    `estimated_amount` 三个字段全部永无用武之地 —— 而它们
#:    是为"让'不知道金额'与'知道金额是 0'可区分"而存在的。
#: 3. `gateway/handlers.py::_usage_pending` 的语义是
#:    "计费未确认"，而"确认"的那一步正是供应商给出确定账单 ⇒
#:    写 `BILL`。`USAGE` 是网关按计价表估出来的**未确认**金额。
#:
#: 所以：`USAGE` ⇒ 钱已经在账本上看得见（`available=True`），
#: 但最终金额仍待供应商账单确认 ⇒ **仍算未结**；
#: `BILL` ⇒ 确认到账 ⇒ 已结。
#:
#: 用 frozenset 而不是字面量判断，是为了让"哪些 kind 算确认"
#: 成为一处可被搜索、可被测试枚举的声明 —— 将来若要承认
#: "人工调整也算确认"，改这里一处即可。
CONFIRMING_LEDGER_KINDS: Final[frozenset[str]] = frozenset({LEDGER_KIND_BILL})

#: 对账报告里 `table` 列的取值。**用稳定字符串而不是表名**：
#: 表名会随迁移改名，而对账报告是要被人工抄进工单的。
TABLE_BUDGET_RESERVED: Final[str] = "budget_reservations"
TABLE_BUDGET_COMMITTED: Final[str] = "usage_ledger"


# ---------------------------------------------------------------------------
# 一、未结用量（R4）
# ---------------------------------------------------------------------------


def _as_pending(
    *,
    attempt_id: uuid.UUID,
    job_id: uuid.UUID,
    project_id: uuid.UUID | None,
    attempt_no: int,
    state: str,
    entry: UsageLedger | None,
) -> PendingUsage:
    """把一条扫描结果组装成 :class:`PendingUsage`。

    `entry is None` ⇒ `available=False`，`estimated_amount` 保持 `None`。
    **不要把它补成 0**：`available` 存在的全部意义就是让"不知道金额"
    与"知道金额是 0"可区分（`PendingUsage` docstring 末段）。
    """
    if entry is None:
        return PendingUsage(
            attempt_id=attempt_id,
            job_id=job_id,
            project_id=project_id,
            attempt_no=attempt_no,
            state=state,
            available=False,
        )
    return PendingUsage(
        attempt_id=attempt_id,
        job_id=job_id,
        project_id=project_id,
        attempt_no=attempt_no,
        state=state,
        available=True,
        estimated_amount=entry.amount,
        ledger_entry_id=entry.id,
        note=entry.description,
    )


def _cross_check_ledger_presence(
    *,
    rows: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID | None, int, str, bool]],
    ledger_by_attempt: dict[uuid.UUID, UsageLedger],
) -> None:
    """两条**独立**查询对"有没有分录"必须给出同一答案。

    · `scan_pending_attempts` 走 `NOT EXISTS` 反连接（按 `source_ref` 关联）；
    · `find_ledger_entries_for_attempts` 走 `source_ref IN (...)` 正向查询。

    两者算的是同一件事，路径不同。若不一致，说明
    `attempt_source_ref_expr` 与 `attempt_source_ref` 的拼法分叉了 ——
    那是**静默的**：反连接会永远为真，于是每一笔账都被反复判为未结，
    而"未结"看起来只是一个业务状态，没人会怀疑是拼串写错了。
    宁可在这里炸成一个可定位的 `RuntimeError`。

    这是本模块唯一一处 `raise` —— 它断言的**不是业务不变量**，
    而是"两个实现必须是同一个函数"。
    """
    for attempt_id, _job_id, _project_id, _no, _state, has_ledger_entry in rows:
        present = attempt_id in ledger_by_attempt
        if present != has_ledger_entry:
            raise RuntimeError(
                f"对账自检失败：attempt {attempt_id} 的 '有无账本分录' 两条"
                f"路径结论不一致（反连接说 {has_ledger_entry}，正向查询说"
                f"{present}）。通常是 attempt_source_ref() 与"
                " attempt_source_ref_expr() 的拼法分叉 —— 请检查"
                " repository.py 里这对函数。"
            )


def reconcile_pending(
    repo: BillingRepository,
    *,
    limit: int = DEFAULT_PENDING_SCAN_LIMIT,
    project_id: uuid.UUID | None = None,
    include_drifts: bool = True,
) -> ReconciliationReport:
    """扫一遍 `usage_pending = true` 的尝试，报告哪些还没结清。

    ## 恒等式

        report.scanned == report.resolved + len(report.pending)

    `scanned` 是这次**实际取回**的行数（不是全局总数：`LIMIT` 之内），
    `resolved` 与 `pending` 是遍历同一批行现场分出来的。三者同源，
    所以等式必须成立；一条一条数不出来的实现会立刻暴露。

    ## `truncated`

    `len(rows) >= limit` ⇒ `truncated=True`（**取满也算**）。宁多报：
    "还有没扫完的"只会让人再跑一次；少报等于声称"全平台已结清"
    而实际还有尾巴。调用方应据此调大 `limit` 或按项目分片。

    ## `include_drifts`

    默认同时跑 R10 的账面分叉判据（`reconcile_project`）。
    传 `False` 只做未结扫描 —— 用于"只想看未结"的轻量调用，
    此时 `drifts=()` 且 `projects_checked=0`，**两者都是"没查"
    而不是"查了没问题"**，调用方不能把这份报告当成健康证明
    （这正是 `projects_checked` 存在的理由）。
    """
    if limit <= 0:
        raise ValueError(f"limit 必须为正数，收到 {limit}")

    rows = repo.scan_pending_attempts(limit=limit, project_id=project_id)
    attempt_ids = [row[0] for row in rows]
    # 批量查：把"扫 200 条"从 201 次往返变成 2 次。
    ledger_by_attempt = repo.find_ledger_entries_for_attempts(attempt_ids)
    _cross_check_ledger_presence(rows=rows, ledger_by_attempt=ledger_by_attempt)

    pending: list[PendingUsage] = []
    resolved = 0
    for attempt_id, job_id, row_project_id, attempt_no, state, _has in rows:
        entry = ledger_by_attempt.get(attempt_id)
        if entry is not None and entry.entry_kind in CONFIRMING_LEDGER_KINDS:
            resolved += 1
            continue
        pending.append(
            _as_pending(
                attempt_id=attempt_id,
                job_id=job_id,
                project_id=row_project_id,
                attempt_no=attempt_no,
                state=state,
                entry=entry,
            )
        )

    if include_drifts:
        if project_id is None:
            drifts = reconcile_all_projects(repo)
            checked = len(repo.list_budgeted_project_ids())
        else:
            drifts = reconcile_project(repo, project_id)
            checked = 1
    else:
        drifts = ()
        checked = 0

    return ReconciliationReport(
        scanned=len(rows),
        resolved=resolved,
        pending=tuple(pending),
        drifts=drifts,
        project_id=project_id,
        truncated=len(rows) >= limit,
        projects_checked=checked,
    )


# ---------------------------------------------------------------------------
# 二、账面与明细的分叉（R10）
# ---------------------------------------------------------------------------


def reconcile_project(
    repo: BillingRepository, project_id: uuid.UUID
) -> tuple[DriftReport, ...]:
    """重算一个项目的账面，返回**两张表**的对比结果。

    返回两条而不是一条，因为它们是**两次相遇**：

    | 表 | 账面值 | 重算值 | 分叉意味着 |
    |---|---|---|---|
    | `budget_reservations` | `project_budgets.reserved_amount` | `Σ amount WHERE state ∈ HOLDING` | 预占与释放没成对（改了明细没改账面，或反之） |
    | `usage_ledger` | `project_budgets.committed_amount` | `Σ usage_ledger.amount` | 有一条改账面的路径绕过了账本，或入账没被累加 |

    两条**永远都返回**（哪怕 `drift == 0`）—— 调用方自己筛。
    这样"我只查了预留那一半"与"两半都查了且都健康"在返回值上
    可区分；若这里只返回不健康的条目，两者都是空元组。

    ## 账面行不存在的处理

    `project_budgets` 没有这个项目的行时，**账面值取 0 而不是跳过**。
    "账本里有费用、账面上查不到这个项目"是最该被看到的一类分叉
    （`repository.list_budgeted_project_ids` docstring 原文），
    跳过它等于把最坏的情况排除在判据之外。
    """
    budget = repo.get_budget(project_id)
    zero = money.ZERO
    # 账面行不存在时取 0 而不是跳过 —— 理由见 docstring。
    # 用 `money.ZERO` 而不是字面量 `0`：金额在本包只有一种构造方式，
    # 破例写一个字面量会让 `DriftReport.drift` 的 `money.sub` 在
    # 这条分支上收到 `int` —— 而这条分支只在"账面行恰好不存在"时
    # 走到，正是最难在测试里撞到的那条路径。
    account_reserved = zero if budget is None else budget.reserved_amount
    account_committed = zero if budget is None else budget.committed_amount

    return (
        DriftReport(
            project_id=project_id,
            table=TABLE_BUDGET_RESERVED,
            account_value=account_reserved,
            recomputed_value=repo.recompute_reserved(project_id),
        ),
        DriftReport(
            project_id=project_id,
            table=TABLE_BUDGET_COMMITTED,
            account_value=account_committed,
            recomputed_value=repo.recompute_committed(project_id),
        ),
    )


def reconcile_all_projects(repo: BillingRepository) -> tuple[DriftReport, ...]:
    """对**全部需要对账的项目**跑 :func:`reconcile_project`。

    项目集合取自 `repository.list_budgeted_project_ids()`（有账面行的
    ∪ 有账本明细的），**不在这里自己写一个 `SELECT`**：
    两处各写一遍查询，迟早会分叉成"对账漏掉某类项目"。

    返回的条目数是 `2 × 项目数`（每个项目两条）。顺序不稳定 ——
    并集去重后由 PG 决定，调用方不应依赖。
    """
    drifts: list[DriftReport] = []
    for project_id in repo.list_budgeted_project_ids():
        drifts.extend(reconcile_project(repo, project_id))
    return tuple(drifts)


__all__ = [
    "CONFIRMING_LEDGER_KINDS",
    "TABLE_BUDGET_COMMITTED",
    "TABLE_BUDGET_RESERVED",
    "reconcile_all_projects",
    "reconcile_pending",
    "reconcile_project",
]
