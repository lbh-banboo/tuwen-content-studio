"""预算账面、预占明细与费用账本的存储访问（WP25 的存储原语层）。

**本层最要紧的一件事**：预算账面的每一次变动都是**单条条件 UPDATE**，
不允许出现"先 SELECT 查余量、再 UPDATE 扣减"。

原因（W10 验收标题"预算并发预占测试"的全部含金量都在这）：
两个并发请求各自读到"还剩 100 元"，各自判断"我要预占的 80 元放得下"，
然后各自扣减 —— 结果账面 160 元进了 100 元的余量，**超限且没有任何一方察觉**。
把判断条件放进 `WHERE` 之后，两条语句在数据库层串行化：
后执行的那条会因为条件不成立而 `rowcount = 0`，调用方据此报 `BUDGET_EXCEEDED`。

与 WP12 的 `FileRepository.reserve_quota` **逐字同构** —— 那是本仓已跑通
并测过的形态。三条理由照抄并确认仍成立：

* **为什么不用 `SELECT ... FOR UPDATE`**：它同样正确，但会把"读"与"写"
  分成两条语句、并在读时就持有行锁。条件 UPDATE 是一条语句拿到锁，
  锁持有时间更短，且只有一次往返。两者选一是实现偏好，但
  **"条件写进 WHERE"这个性质不能少**。
* **为什么释放预占也要走条件 UPDATE**：若用
  `SET reserved = reserved - :delta` 无条件下发，重复的释放请求会把额度
  **多还一次**（甚至还成负数被 `reserved_non_negative` 挡住 → 500）。
  因此所有释放路径都先经过 :meth:`transition_reservation` 改状态，
  **只有 `rowcount = 1`（即状态确实迁移了）才去动账面**。
* **为什么 `project_budgets` 没有 `lock_version`**：更新全是单条原子条件
  UPDATE，没有读-改-写窗口。乐观锁用于"读出来、改几个字段、写回去"
  这种需要携带旧值的场景；在这里加它只会引入一个多余的失败分支。
  （`cost_estimates` 则相反 —— 结算回填要读原预估才能算差异，故它有
  `lock_version`。两张表的差别是刻意的，见 `models.py` 的对照表。）

## 账本只有 INSERT

`usage_ledger` 是 **不可变表**（`platform/immutability.py` 的触发器拦
UPDATE/DELETE）。本类因此**刻意不提供**任何 `update_ledger_*` /
`delete_ledger_*` 方法 —— 不是忘了写，而是那些方法就算写了也会在运行时
被触发器以 SQLSTATE `23001` 拒绝，而"存在一个永远抛异常的方法"会让下一个人
以为那条路走得通。要改账本 = 追加一条新的分录（冲正 / 调整）。

这与 `planning/repository.py` docstring 第 5 条是同一处置。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    String,
    and_,
    func,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from rednote.modules.billing.models import (
    BudgetReservation,
    CostEstimate,
    PricingVersion,
    ProjectBudget,
    UsageLedger,
)
from rednote.modules.billing.types import (
    ESTIMATE_STATE_SETTLED,
    HOLDING_RESERVATION_STATES,
    RESERVATION_STATE_HELD,
    USABLE_PRICING_STATES,
)

#: 账本 `source_ref` 里"来自一次任务尝试"的前缀，形如
#: `attempt:<job_attempts.id>`（`UsageLedger.source_ref` 的 docstring 定义了
#: 三种形态：`attempt:` / `bill:` / `manual:`）。
#:
#: **为什么这个常量在本模块而不是 `types.py`**：它是**存储层对本列形状的
#: 解释**，且只有本包读写它（`jobs` 侧只写 `usage_pending` 布尔位，
#: 不认识 `source_ref`）。`types.py` 放的是"取值域与传值结构"，
#: 把一个只被本层使用的字符串拼装约定放进去会让那份取值域表虚胖。
#: 将来若有第二个包要按它反查，再上移 —— 到那时它才真的成了跨包契约。
ATTEMPT_SOURCE_REF_PREFIX: str = "attempt:"

#: 一次性拉取"待对账"记录的默认上限。**必须有限**：`NOT EXISTS` 反连接
#: 在零分录的历史数据上会全表扫，无上限的跑批会在第一次上线时拖垮实例。
DEFAULT_PENDING_SCAN_LIMIT: int = 200


def attempt_source_ref(attempt_id: uuid.UUID) -> str:
    """`job_attempts.id` → 账本 `source_ref`。**Python 侧唯一构造入口**。

    不把 `f"attempt:{id}"` 散在各处：一旦有人在某处写成 `"attempts:"`，
    `NOT EXISTS` 就永远为真 —— 那笔账会被**反复判为未结**，
    而对账报告看起来只是"有一条待人工处理"，不会报错。
    """
    return f"{ATTEMPT_SOURCE_REF_PREFIX}{attempt_id}"


def attempt_source_ref_expr(attempt_id_column: Any) -> Any:
    """SQL 侧的同一个字符串：`'attempt:' || <id>::text`。

    与 :func:`attempt_source_ref` **必须拼出完全相同的字符串** ——
    前者给 Python 侧、后者给 SQL 侧。两处分开是**不得已**：
    在 SQL 里调 Python 函数做不到，在 Python 里用 SQL 表达式也做不到。
    一致性由测试保证（同一个 id 两边拼出的字符串逐字相等）——
    这也是本包唯一一处"同一事实的两种写法"，因此必须有一条判据盯着。

    不用 `concat()`：`||` 是 PG 原生操作符。这里用 `+` 让 SQLAlchemy
    编译成 `||`（两侧都是 `String` 类型，编译期即可确定）。

    `cast(..., String)` 显式写出而不是依赖 `uuid::text` 的默认形态：
    默认形态当前确实带连字符、与 `str(uuid.UUID)` 一致，
    但让"两种写法相等"依赖一个未写明的默认行为不够稳。
    显式 cast 到无长度 `String` 时 PG 仍产出规范形态（`uuid_out`），
    这一点由反向验证覆盖。
    """
    return literal(ATTEMPT_SOURCE_REF_PREFIX) + attempt_id_column.cast(String)


class BillingRepository:
    """计价 / 预算 / 预占 / 账本的读写入口。事务边界由调用方控制。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        """本仓库持有的 session。

        **为什么公开它**（与 `FileRepository.session` 同一条理由）：
        `service.py` 要在**同一个事务**里调 `record_audit`
        （它需要自己的 `AuditRepository(repo.session)`），也要在
        `schema.py` 组装的响应里读同一次快照。若为此新开 session，
        "预算已改"与"审计已记"会来自两个不同事务（可能跨越一次提交），
        排障时无法解释"为什么审计里没有这笔"。
        """
        return self._session

    def flush(self) -> None:
        """把待写入的变更立即发给数据库。

        用途与 `FileRepository.flush` 一致：让约束错误在**预期位置**抛出，
        而不是等到事务提交时才炸 —— 提交发生在 `session_scope` 的退出处，
        那里已经离开了能给出可读 4xx 的代码位置，错误只会以 500 冒出去。
        """
        self._session.flush()

    def now(self) -> datetime:
        """**数据库**当前时间。

        不用 `datetime.now()`：应用与数据库可能不同时区（本仓 `timestamptz`
        列由 PG 归一，但"现在"这个判断若在应用侧做，事务期间的时钟漂移
        会让"生效时间 <= 现在"的筛选结果在同一请求内前后不一致）。
        `planning/repository.py::now` 是同一实现。
        """
        return self._session.execute(select(func.now())).scalar_one()

    # ------------------------------------------------------------------
    # 预算账面：三条条件 UPDATE（本包的命门）
    # ------------------------------------------------------------------

    def reserve_budget(self, *, project_id: uuid.UUID, delta: Decimal) -> bool:
        """原子预占 `delta` 金额。放得下返回 True，否则 False。

        条件 `committed_amount + reserved_amount + :delta <= limit_amount`
        **必须在本条语句的 WHERE 里**（见模块 docstring）。
        调用方拿到 False 即报 `BUDGET_EXCEEDED`，**不需要也不应该再查一次
        余量** —— 再查一次读到的是"别人已经改过"的账面，反而误导
        （`files/repository.py:100` 原文）。

        账面行不存在时也是 `rowcount = 0`（返回 False）。这不区分
        "超限"与"没设预算"是**由 service 层负责的**：它在调本方法之前
        先 `ensure_budget`，因此能确定行一定存在；此后 False 只可能是超限。
        把两个原因分开报（`BUDGET_NOT_FOUND` / `BUDGET_EXCEEDED`）
        对前端是必要的 —— 前者要引导去设置预算，后者要引导去调额度。
        """
        result = self._session.execute(
            update(ProjectBudget)
            .where(
                ProjectBudget.project_id == project_id,
                ProjectBudget.committed_amount
                + ProjectBudget.reserved_amount
                + delta
                <= ProjectBudget.limit_amount,
            )
            .values(
                reserved_amount=ProjectBudget.reserved_amount + delta,
                updated_at=func.now(),
            )
        )
        return result.rowcount == 1

    def release_budget(self, *, project_id: uuid.UUID, delta: Decimal) -> bool:
        """归还预占。**只应由持有预占的状态迁移触发一次**。

        `WHERE reserved_amount >= :delta` 是防"还多了"的最后一道：
        正常流程下不会触发（因为 `transition_reservation` 已经保证只释放一次），
        但若某条路径被重复调用，这里会让 UPDATE 不生效（返回 False）
        而不是把账面扣成负数。负值会被 `reserved_non_negative` 挡住并变成
        500 —— 一个"静默多还"变成"显式失败"，对排障更友好，但仍不如
        直接不发生。

        返回 `bool`（而不是像 `files` 那样返回 `None`）：本包把"归还没生效"
        当作**可观测的异常信号**交给 service 层决定要不要报警 ——
        正常情况下它必须为 True，为 False 说明状态机有洞。

        `delta <= 0` 早返回 True（无事可做不算失败）。
        """
        if delta <= 0:
            return True
        result = self._session.execute(
            update(ProjectBudget)
            .where(
                ProjectBudget.project_id == project_id,
                ProjectBudget.reserved_amount >= delta,
            )
            .values(
                reserved_amount=ProjectBudget.reserved_amount - delta,
                updated_at=func.now(),
            )
        )
        return result.rowcount == 1

    def settle_budget(
        self,
        *,
        project_id: uuid.UUID,
        reserved_release: Decimal,
        committed_add: Decimal,
    ) -> bool:
        """把预占转成已结算，允许两者不相等（按实际金额修正）。

        `reserved_release` 是"当初预占了多少"，`committed_add` 是"实际花了多少"。
        两者相等是常态；不相等时本条语句顺带把差额还回可用额度 ——
        这正是 `models.BudgetReservation` docstring 里"为什么 `settled_amount`
        可以与 `amount` 不等"的落地（预估 50 篇、实际 48 篇）。

        条件里同时校验归还量足够、且转换后仍不超限：
        实际远超预占且超出余量时返回 False，调用方报 `BUDGET_EXCEEDED`
        （此时**不**改状态 —— 先 `settle_budget` 成功再 `transition`，
        顺序见 service 层）。

        两次检查仍是一条语句，不存在中间窗口。与
        `files/repository.py::finalize_reservation` 逐字同构。
        """
        result = self._session.execute(
            update(ProjectBudget)
            .where(
                ProjectBudget.project_id == project_id,
                ProjectBudget.reserved_amount >= reserved_release,
                ProjectBudget.committed_amount
                + ProjectBudget.reserved_amount
                - reserved_release
                + committed_add
                <= ProjectBudget.limit_amount,
            )
            .values(
                reserved_amount=ProjectBudget.reserved_amount - reserved_release,
                committed_amount=ProjectBudget.committed_amount + committed_add,
                updated_at=func.now(),
            )
        )
        return result.rowcount == 1

    def get_budget(self, project_id: uuid.UUID) -> ProjectBudget | None:
        """取账面行。**不加行锁** —— 所有写路径都是条件 UPDATE，读不需要锁。"""
        return self._session.get(ProjectBudget, project_id)

    def ensure_budget(
        self,
        *,
        project_id: uuid.UUID,
        currency: str,
        limit_amount: Decimal,
        set_by: uuid.UUID | None = None,
    ) -> None:
        """保证项目有预算账面（缺则建一个）。**不覆盖已有行**。

        用 `ON CONFLICT DO NOTHING` 而不是"先查再插"：并发下两个请求同时
        发现"没有账面"，先查再插会让其中一个撞主键报 500（`project_id`
        就是主键）。这条语句是幂等的，重复调用无副作用。
        （与 `FileRepository.ensure_quota_account` 同一手法。）

        传入的 `limit_amount` 只在**确实要新建**时生效；
        已存在的账面不受影响 —— 否则一次普通的预估动作会把管理者设的
        额度悄悄改成默认值。
        """
        self._session.execute(
            pg_insert(ProjectBudget)
            .values(
                project_id=project_id,
                currency=currency,
                limit_amount=limit_amount,
                set_by=set_by,
            )
            .on_conflict_do_nothing(index_elements=["project_id"])
        )

    def set_budget_limit(
        self,
        *,
        project_id: uuid.UUID,
        currency: str,
        limit_amount: Decimal,
        set_by: uuid.UUID | None,
    ) -> bool:
        """改预算上限。返回 False 表示"新上限装不下已发生的占用"。

        两个条件都在 WHERE 里：

        1. `committed + reserved <= :limit` —— 调低上限时若装不下，
           直接改会被 `within_limit` CHECK 拒成 500。放进 WHERE 让它
           变成可解释的 4xx（"请先释放占用或取消在途任务"）。
           **不加这一条会怎样**：CHECK 确实兜住了数据，但错误以
           `IntegrityError` 冒到 API 层，用户看到"服务器内部错误"，
           而真实原因是"你要设的额度比已经花掉的还少" —— 一句人话的事。
        2. 币种一致性：已有金额时不许换币种。
           `OR (committed = 0 AND reserved = 0)` 的例外是给"设错了要改"
           留的门 —— 没有金额时换币种是安全的（没有数字被重新解释）。

        调用方应先 `ensure_budget`，这样 False 只可能是上面两条之一，
        而不是"行不存在"。
        """
        result = self._session.execute(
            update(ProjectBudget)
            .where(
                ProjectBudget.project_id == project_id,
                ProjectBudget.committed_amount
                + ProjectBudget.reserved_amount
                <= limit_amount,
                or_(
                    ProjectBudget.currency == currency,
                    and_(
                        ProjectBudget.committed_amount == 0,
                        ProjectBudget.reserved_amount == 0,
                    ),
                ),
            )
            .values(
                currency=currency,
                limit_amount=limit_amount,
                set_by=set_by,
                updated_at=func.now(),
            )
        )
        return result.rowcount == 1

    # ------------------------------------------------------------------
    # 预算对账（R10）：从明细重算账面
    # ------------------------------------------------------------------

    def recompute_reserved(self, project_id: uuid.UUID) -> Decimal:
        """按**预占明细**重算已预占金额（对账用，**不参与写入路径**）。

        口径必须与 `reserve_budget` / `release_budget` / `settle_budget`
        累加的口径一致，否则对账会永远报差异：这里是
        `Σ amount WHERE state IN HOLDING_RESERVATION_STATES`。

        **`EXPIRED` 必须算在内** —— 它的钱还占着账面，只是标记了"该回收"。
        若把它排除，"有一笔过期待回收"会被当成账面多记了钱，
        而对账报的差异方向正好相反（会让人去查一个不存在的问题）。
        """
        stmt = select(func.coalesce(func.sum(BudgetReservation.amount), 0)).where(
            BudgetReservation.project_id == project_id,
            BudgetReservation.state.in_(sorted(HOLDING_RESERVATION_STATES)),
        )
        return Decimal(self._session.execute(stmt).scalar_one())

    def recompute_committed(self, project_id: uuid.UUID) -> Decimal:
        """按**账本**重算已结算金额（对账用，**不参与写入路径**）。

        `committed_amount` 的语义是"对账确认后的累计费用"，而账本
        （`usage_ledger`）正是"费用发生过"的追加式记录 —— 两者若不等，
        说明有一条改账面的路径绕过了账本，或有一条入账路径没被累加。

        求和**不过滤符号**：`REVERSAL` 是负数分录，冲正就该让合计变小。
        这正是 Append-only 账本的好处 —— 不需要 `WHERE` 就能得到正确的净额。
        """
        stmt = select(func.coalesce(func.sum(UsageLedger.amount), 0)).where(
            UsageLedger.project_id == project_id
        )
        return Decimal(self._session.execute(stmt).scalar_one())

    def list_budgeted_project_ids(self) -> list[uuid.UUID]:
        """全部**需要对账**的项目：有账面行的 ∪ 有账本明细的。

        **为什么要并集**：一个项目可能有账本分录但没有账面行（历史数据、
        或账面行被误删）。只看 `project_budgets` 会漏掉它 ——
        而"账本里有费用、账面上查不到这个项目"恰恰是最该报警的一种分叉。

        返回值的顺序不稳定（并集去重后由 PG 决定），调用方不应依赖顺序。
        """
        budgeted = select(ProjectBudget.project_id)
        ledgered = select(UsageLedger.project_id).distinct()
        stmt = budgeted.union(ledgered).order_by(None)
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # 预占明细：状态机条件 UPDATE
    # ------------------------------------------------------------------

    def insert_reservation(
        self,
        *,
        project_id: uuid.UUID,
        amount: Decimal,
        currency: str,
        state: str = RESERVATION_STATE_HELD,
        estimate_id: uuid.UUID | None = None,
        ref: str | None = None,
        note: str | None = None,
        created_by: uuid.UUID | None = None,
    ) -> BudgetReservation:
        """插入一条预占明细。

        与 `reserve_budget` 的**顺序**（先改账面、后插明细）由 service 层
        保证，见其 docstring：先改账面才能在并发下拿到"我是否抢到了额度"
        这个确定的答案；先插明细再改账面的话，抢额度失败时那条明细
        已经写进去了，只能再删/再改状态 —— 而它将是一行**从未真正持有过
        预占**的记录，把"每次预占一行"的语义污染掉。
        """
        row = BudgetReservation(
            project_id=project_id,
            amount=amount,
            currency=currency,
            state=state,
            estimate_id=estimate_id,
            ref=ref,
            note=note,
            created_by=created_by,
        )
        self._session.add(row)
        return row

    def get_reservation(self, reservation_id: uuid.UUID) -> BudgetReservation | None:
        return self._session.get(BudgetReservation, reservation_id)

    def transition_reservation(
        self,
        *,
        reservation_id: uuid.UUID,
        from_states: Sequence[str],
        to_state: str,
        settled_amount: Decimal | None = None,
        note: str | None = None,
    ) -> bool:
        """条件状态迁移。**返回是否真的迁移了**。

        这是"每次释放只允许一次"的实现点（`files/repository.py:260`
        的 `transition_session` 同款）：所有会动账面的路径都先调它，
        `rowcount == 1` 才继续。重复请求（第二次释放、重复结算）
        拿到 False，直接返回已有结果，不再触碰账面。

        `from_states` 的取值由**调用方按语义选**：
        释放走 `HOLDING_RESERVATION_STATES`（HELD + EXPIRED），
        结算同理。不在这里硬编码 —— 硬编码会让"从 EXPIRED 释放"
        这条真实存在的路径（对账回收）变成写不出来。

        `settled_amount` 用 `None` 表示"本次不改这一列"。
        **注意**：结算时它必须是具体值（可以是 `Decimal(0)`），
        因为 `settled_amount_matches_state` CHECK 要求
        `state = 'SETTLED'` 时该列非空。传 `Decimal(0)` 与不传
        在这里是两件事 —— Python 的 `if settled_amount is not None`
        正确区分了它们（`Decimal(0) is not None` 为 True）。
        """
        values: dict[str, Any] = {"state": to_state, "updated_at": func.now()}
        if settled_amount is not None:
            values["settled_amount"] = settled_amount
        if note is not None:
            values["note"] = note

        result = self._session.execute(
            update(BudgetReservation)
            .where(
                BudgetReservation.id == reservation_id,
                BudgetReservation.state.in_(list(from_states)),
            )
            .values(**values)
        )
        return result.rowcount == 1

    def list_reservations(
        self,
        *,
        project_id: uuid.UUID,
        states: Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[BudgetReservation]:
        """列项目的预占明细（新→旧的顺序）。

        `states=None` 表示全部状态（管理视图要看到已释放的历史），
        传具体集合则是"只看在途"（走 `ix_budget_reservations_holding`）。
        """
        stmt = select(BudgetReservation).where(
            BudgetReservation.project_id == project_id
        )
        if states is not None:
            stmt = stmt.where(BudgetReservation.state.in_(list(states)))
        stmt = stmt.order_by(
            BudgetReservation.created_at.desc(), BudgetReservation.id
        ).limit(limit)
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # 费用账本：**只有 INSERT**
    # ------------------------------------------------------------------

    def insert_ledger_entry(
        self,
        *,
        project_id: uuid.UUID,
        entry_kind: str,
        currency: str,
        amount: Decimal,
        occurred_at: datetime,
        quantity: Decimal | None = None,
        unit: str | None = None,
        reverses_ledger_id: uuid.UUID | None = None,
        source_ref: str | None = None,
        idempotency_key: str | None = None,
        description: str | None = None,
    ) -> UsageLedger:
        """追加一条分录。**本类是账本唯一的写入口**。

        `occurred_at` 是**必填位置参数**（在签名里没有默认值）：
        `models.py` 模块 docstring 第四节要求"它由调用方给的，不能默认
        `now()`" —— 而"忘了传"在数据库层表现为 NOT NULL 违约（好事，
        会炸），在应用层若给了 `now()` 默认值则表现为"就是现在"，
        那个错误要到月度汇总才发现。

        `idempotency_key` 可空：人工分录（`ADJUSTMENT`）没有天然幂等键。
        非空时由部分唯一索引 `uq_usage_ledger_idempotency` 保证唯一 ——
        调用方传同一个键第二次会拿到 `23505`（`UNIQUE_VIOLATION_SQLSTATE`），
        那是**正常的并发结果**（"这笔已经记过了"），
        与 `23001`（不可变被违反）是完全不同的两件事，处理方式也不同。
        """
        row = UsageLedger(
            project_id=project_id,
            entry_kind=entry_kind,
            currency=currency,
            amount=amount,
            occurred_at=occurred_at,
            quantity=quantity,
            unit=unit,
            reverses_ledger_id=reverses_ledger_id,
            source_ref=source_ref,
            idempotency_key=idempotency_key,
            description=description,
        )
        self._session.add(row)
        return row

    def get_ledger_entry(self, ledger_id: uuid.UUID) -> UsageLedger | None:
        return self._session.get(UsageLedger, ledger_id)

    def find_ledger_entry_by_idempotency(
        self, idempotency_key: str
    ) -> UsageLedger | None:
        """按幂等键回查已有分录（`23505` 之后用来取回那一条）。

        并发下正确流程是：直接 INSERT → 撞 `23505` → 用本方法取回。
        而不是"先查再插" —— 后者在两个请求同时发现"不存在"时，
        两边都去插，其中一边仍会撞唯一约束（把 500 冒出去），
        而这件事在语义上完全正常（同一笔账单重复导入）。

        **本方法的调用方是 `service._insert_ledger_entry_idempotent`**：
        它在 SAVEPOINT 里 INSERT，`except IntegrityError` 且 sqlstate 为
        `23505` 时调本方法。`service.append_ledger_entry` 里那次
        "先查"只是快路径，**不承担并发正确性**。
        （CR-1：修复前 service 侧真的只做了"先查再插"，
        实测 8 线程同键 ⇒ 5 个抛 `23505` 到调用方。）
        """
        stmt = select(UsageLedger).where(
            UsageLedger.idempotency_key == idempotency_key
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def list_ledger_entries(
        self,
        *,
        project_id: uuid.UUID,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[UsageLedger]:
        """列项目的账本分录（新→旧）。`since` 是 `occurred_at` 的下界。

        按 `occurred_at` 而不是 `created_at` 过滤/排序 —— 见 `models.py`
        模块 docstring 第四节：一笔跨月补录的账按 `created_at` 会落进
        错误的月份。走 `ix_usage_ledger_project_occurred`。
        """
        stmt = select(UsageLedger).where(UsageLedger.project_id == project_id)
        if since is not None:
            stmt = stmt.where(UsageLedger.occurred_at >= since)
        stmt = stmt.order_by(
            UsageLedger.occurred_at.desc(), UsageLedger.created_at.desc()
        ).limit(limit)
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # 对账输入：未结用量（`PendingUsage` 的数据来源）
    # ------------------------------------------------------------------

    def find_ledger_entry_for_attempt(
        self, attempt_id: uuid.UUID
    ) -> UsageLedger | None:
        """这次尝试在账本里有没有分录。**`PendingUsage.available` 的判据**。

        与 `gateway/handlers.py::_usage_pending` **同源**：那边定义
        "上游说这笔没用完"（`usage_pending = true`），这边回答
        "账本里有没有它的分录"。两者相与才是"未结"。

        走 `source_ref = 'attempt:<id>'`。**`scalar_one_or_none` 会在
        出现两条时报错** —— 这是刻意的：同一次尝试理论上只能有一类分录
        （`idempotency_key` 保证），真出现两条说明有人绕过了幂等键，
        这时报错比"随便取一条"好。
        """
        stmt = select(UsageLedger).where(
            UsageLedger.source_ref == attempt_source_ref(attempt_id)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def find_ledger_entries_for_attempts(
        self, attempt_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, UsageLedger]:
        """批量版 :meth:`find_ledger_entry_for_attempt`，返回 `{attempt_id: 行}`。

        **为什么必须有批量版**：对账一次扫 `limit` 条（默认 200），
        逐条查会让"扫 200 条"变成 201 次往返。批量只有一次。

        用 `scalar_one_or_none` 的循环语义会丢掉"重复"的报错能力，
        因此这里**显式检测重复**并在发现时抛 `ValueError` ——
        与单条版同样的立场（两条意味着幂等键被绕过，那是缺陷不是数据）。
        """
        if not attempt_ids:
            return {}
        refs = {attempt_source_ref(attempt_id) for attempt_id in attempt_ids}
        stmt = select(UsageLedger).where(UsageLedger.source_ref.in_(sorted(refs)))
        found = list(self._session.execute(stmt).scalars().all())

        by_attempt: dict[uuid.UUID, UsageLedger] = {}
        for row in found:
            if row.source_ref is None or not row.source_ref.startswith(
                ATTEMPT_SOURCE_REF_PREFIX
            ):
                # 理论上不可能（WHERE 已经限定），但 `startswith` 的
                # 前置判断让类型收窄成立，也顺手把"前缀被改过"这种
                # 未来改动暴露成可读错误而不是 `ValueError: badly formed
                # hexadecimal UUID string`。
                continue
            raw = row.source_ref[len(ATTEMPT_SOURCE_REF_PREFIX) :]
            try:
                attempt_id = uuid.UUID(raw)
            except ValueError as exc:  # pragma: no cover - 数据缺陷
                raise ValueError(
                    f"账本 source_ref 不是合法尝试引用：{row.source_ref!r}"
                ) from exc
            if attempt_id in by_attempt:
                raise ValueError(
                    f"同一次尝试有两条账本分录：{attempt_id}"
                    "（幂等键被绕过，或 source_ref 构造未走 attempt_source_ref）"
                )
            by_attempt[attempt_id] = row
        return by_attempt

    def scan_pending_attempts(
        self,
        *,
        limit: int = DEFAULT_PENDING_SCAN_LIMIT,
        project_id: uuid.UUID | None = None,
    ) -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID | None, int, str, bool]]:
        """扫出**上游声明未结**的尝试，附"账本里有没有分录"。

        返回行的形状：
        `(attempt_id, job_id, project_id, attempt_no, state, has_ledger_entry)`
        —— 刻意返回元组而不是 ORM 对象：调用方（`reconciliation.py`）
        要的是 `PendingUsage`，把 ORM 行漏出去会让上层有机会
        顺手改一个**不可变表的实体**，而那条路是走不通的（触发器拒绝），
        只会让缺陷延后到 flush 时才炸。

        `NOT EXISTS` 反连接在这里**不是优化而是语义**：
        `has_ledger_entry` 为 False 就是"未结"。用 `LEFT JOIN` +
        `IS NULL` 是等价写法，但 `NOT EXISTS` 在有 `ix_usage_ledger_source`
        时计划更稳（`LIMIT` + 反连接，PG 不会把整个账本物化）。

        `project_id` 可空的两层含义必须分开：本方法的参数为 `None` 是
        "不过滤（全平台）"；返回元组里的 `project_id` 为 `None` 是
        "这个 job 本身没有项目"（`Job.project_id` 可空）。混为一谈会让
        "按项目对账"查不出孤儿，而孤儿正是最该被看到的那一类。
        """
        # 局部 import 防循环：`jobs.models` 反过来不需要 `billing`，
        # 但模块级的相互 import 会在将来某次重构时变成环。本仓的
        # `files/repository.py::project_exists` 是同一手法。
        from rednote.modules.jobs.models import Job, JobAttempt

        ledger_ref = (
            select(UsageLedger.id)
            .where(UsageLedger.source_ref == attempt_source_ref_expr(JobAttempt.id))
            .exists()
        )

        stmt = (
            select(
                JobAttempt.id,
                JobAttempt.job_id,
                Job.project_id,
                JobAttempt.attempt_no,
                JobAttempt.state,
                ledger_ref.label("has_ledger_entry"),
            )
            .join(Job, Job.id == JobAttempt.job_id)
            .where(JobAttempt.usage_pending.is_(True))
            .order_by(JobAttempt.started_at, JobAttempt.id)
            .limit(limit)
        )
        if project_id is not None:
            stmt = stmt.where(Job.project_id == project_id)

        rows = self._session.execute(stmt).all()
        return [
            (row[0], row[1], row[2], row[3], row[4], bool(row[5])) for row in rows
        ]

    # ------------------------------------------------------------------
    # 费用预估
    # ------------------------------------------------------------------

    def insert_estimate(
        self,
        *,
        project_id: uuid.UUID,
        batch_kind: str,
        quantity: int,
        currency: str,
        estimated_amount: Decimal,
        lines: list[dict[str, Any]],
        pricing_version_id: uuid.UUID | None,
        created_by: uuid.UUID | None,
        state: str | None = None,
    ) -> CostEstimate:
        """插入一份预估。`state` 用 `None` 表示"走列默认值 `DRAFT`"。

        不把 `DRAFT` 写成 Python 的默认参数：默认值写在两处（这里与
        `models.py`）以后，改一处就会分叉。传 `None` 让数据库的
        `server_default` 成为唯一真值来源。
        """
        row = CostEstimate(
            project_id=project_id,
            batch_kind=batch_kind,
            quantity=quantity,
            currency=currency,
            estimated_amount=estimated_amount,
            lines=lines,
            pricing_version_id=pricing_version_id,
            created_by=created_by,
        )
        if state is not None:
            row.state = state
        self._session.add(row)
        return row

    def get_estimate(self, estimate_id: uuid.UUID) -> CostEstimate | None:
        return self._session.get(CostEstimate, estimate_id)

    def get_estimate_for_update(self, estimate_id: uuid.UUID) -> CostEstimate | None:
        """取预估并**加行锁**。

        只有"读出来 → 改几个字段 → 写回去"的路径需要它（结算回填）。
        非结算路径一律用 :meth:`get_estimate` —— `SELECT ... FOR UPDATE`
        在有并发时会排成队列，滥用会让一次普通查询阻塞一个结算。
        """
        return self._session.get(CostEstimate, estimate_id, with_for_update=True)

    def transition_estimate_state(
        self,
        *,
        estimate_id: uuid.UUID,
        from_states: Sequence[str],
        to_state: str,
        expected_lock_version: int,
    ) -> int:
        """预估的状态迁移（乐观锁）。**返回 `rowcount`**。

        `lock_version` 条件与状态条件是**并列的两道**，缺一不可：

        * 只有状态条件 ⇒ 两个并发结算会各自把 `actual_amount` 写成
          不同的值，后者覆盖前者且**双方都以为成功**（正是 WP22
          `update_draft` 的教训形态）；
        * 只有版本条件 ⇒ 从 `CANCELLED` 也能迁到 `RESERVED`，
          作废了的东西会复活。

        返回值是 `int` 而不是 `bool`：调用方（service 层）需要区分
        "状态不对"与"版本不对"来给出不同文案（`VERSION_CONFLICT` 412
        vs 业务 409）。这与 `planning/repository.py::update_draft`
        返回 `int` 是同一条理由。

        `.execution_options(synchronize_session=False)` 必须加：
        session 里可能已经有这份预估的 ORM 实例（上面刚读过），
        ORM 默认会尝试同步它 —— 而对 `lock_version = lock_version + 1`
        这种**基于列自身**的表达式，同步需要回读，在语句已发之后
        才决定要不要回读会让行为依赖 session 状态。
        """
        result = self._session.execute(
            update(CostEstimate)
            .where(
                CostEstimate.id == estimate_id,
                CostEstimate.state.in_(list(from_states)),
                CostEstimate.lock_version == expected_lock_version,
            )
            .values(
                state=to_state,
                lock_version=CostEstimate.lock_version + 1,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    def settle_estimate(
        self,
        *,
        estimate_id: uuid.UUID,
        expected_lock_version: int,
        actual_amount: Decimal,
        variance_amount: Decimal,
    ) -> int:
        """回填实际金额与差异，并把状态推到 `SETTLED`。**返回 `rowcount`**。

        三列**同一条 UPDATE 写出**（`models.CostEstimate` docstring 原话：
        分两条写会留下"实际值有了、差异还是 NULL"的中间态）——
        那个中间态会被 `settled_requires_actual` CHECK 拒掉，
        但拒在 flush 时、位置已经不在能给出可读 4xx 的地方。

        状态条件写 `state = 'RESERVED'` 而不是 `IN (...)`：
        只有"已预占"的预估才谈得上结算。`DRAFT`（还没占额度）与
        `CANCELLED`（已作废）都不该走到这里，而若走到了，
        说明调用方漏了一次状态判断 —— 让 UPDATE 不生效比让它成功更好。
        """
        result = self._session.execute(
            update(CostEstimate)
            .where(
                CostEstimate.id == estimate_id,
                CostEstimate.state == "RESERVED",
                CostEstimate.lock_version == expected_lock_version,
            )
            .values(
                state=ESTIMATE_STATE_SETTLED,
                actual_amount=actual_amount,
                variance_amount=variance_amount,
                lock_version=CostEstimate.lock_version + 1,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    def reload_estimate(self, estimate_id: uuid.UUID) -> CostEstimate | None:
        """重新读一份预估，**丢弃 session 里的缓存实例**。

        `update()` 带了 `synchronize_session=False`，因此 session 里那个
        旧实例的字段**还是改前的值**。直接把它序列化返回给前端，
        调用方会看到"改了但接口回的是旧值" —— 而数据库里其实已经改好了。
        `populate_existing=True` 强制用库里的值覆盖会话实例
        （与 `planning/repository.py::reload_draft` 同款）。
        """
        stmt = (
            select(CostEstimate)
            .where(CostEstimate.id == estimate_id)
            .execution_options(populate_existing=True)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def list_estimates(
        self,
        *,
        project_id: uuid.UUID,
        states: Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[CostEstimate]:
        stmt = select(CostEstimate).where(CostEstimate.project_id == project_id)
        if states is not None:
            stmt = stmt.where(CostEstimate.state.in_(list(states)))
        stmt = stmt.order_by(CostEstimate.created_at.desc(), CostEstimate.id).limit(
            limit
        )
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # 计价版本（不可变：只有 INSERT 与 SELECT）
    # ------------------------------------------------------------------

    def insert_pricing_version(
        self,
        *,
        provider_id: uuid.UUID,
        model_profile_id: uuid.UUID | None,
        currency: str,
        label: str,
        unit_prices: dict[str, str],
        effective_from: datetime,
        state: str,
        note: str | None = None,
    ) -> PricingVersion:
        """插入一份计价版本。

        `unit_prices` 的**值必须是字符串**（`models.PricingVersion` docstring：
        jsonb number 是 numeric 的无标度形态，读取时位数会丢）。
        类型注解写成 `dict[str, str]` 是让"传了 float"在静态检查期
        就被看见，而不是等到读取时发现少了两位小数。
        """
        row = PricingVersion(
            provider_id=provider_id,
            model_profile_id=model_profile_id,
            currency=currency,
            label=label,
            unit_prices=unit_prices,
            effective_from=effective_from,
            state=state,
            note=note,
        )
        self._session.add(row)
        return row

    def get_pricing_version(self, version_id: uuid.UUID) -> PricingVersion | None:
        return self._session.get(PricingVersion, version_id)

    def find_usable_pricing_version(
        self,
        *,
        provider_id: uuid.UUID,
        currency: str,
        at: datetime,
        model_profile_id: uuid.UUID | None = None,
    ) -> PricingVersion | None:
        """找出该时点该供应商（该模型）该币种的**可用**价目。

        筛选三条：`state IN USABLE_PRICING_STATES`（只有 `PUBLISHED`
        可被引用 —— `DRAFT` 是"录了一半"）、`effective_from <= at`
        （未来生效的不能用）、币种匹配（跨币种不换算，见 `types.CURRENCIES`
        的说明）。

        优先级：**模型级优先于供应商级**。
        `model_profile_id` 为空的行是"该供应商所有模型同一价目"，
        它只在没有更具体的模型级价目时生效。

        实现上**把选择交给数据库**（`ORDER BY` + `LIMIT 1`）而不是
        "取回一批在 Python 里挑"：后者在有效版本很多时要拉回整批，
        且"哪一条优先"这件事会变成 Python 的逻辑，与索引无关。
        排序键 `model_profile_id IS NULL`（False 排前）+
        `effective_from DESC`（同一时点不会有两份 —— 唯一约束保证）。

        `model_profile_id=None` 表示"调用方没有模型信息"，
        此时只看供应商级（`model_profile_id IS NULL`）——
        **不能**退化成"随便取一条模型级的"，那会用错单价。
        """
        stmt = select(PricingVersion).where(
            PricingVersion.provider_id == provider_id,
            PricingVersion.currency == currency,
            PricingVersion.state.in_(sorted(USABLE_PRICING_STATES)),
            PricingVersion.effective_from <= at,
        )
        if model_profile_id is None:
            stmt = stmt.where(PricingVersion.model_profile_id.is_(None))
        else:
            stmt = stmt.where(
                or_(
                    PricingVersion.model_profile_id == model_profile_id,
                    PricingVersion.model_profile_id.is_(None),
                )
            )
        stmt = stmt.order_by(
            PricingVersion.model_profile_id.is_(None),
            PricingVersion.effective_from.desc(),
        ).limit(1)
        return self._session.execute(stmt).scalar_one_or_none()

    def list_pricing_versions(
        self,
        *,
        provider_id: uuid.UUID | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[PricingVersion]:
        stmt = select(PricingVersion)
        if provider_id is not None:
            stmt = stmt.where(PricingVersion.provider_id == provider_id)
        if state is not None:
            stmt = stmt.where(PricingVersion.state == state)
        stmt = stmt.order_by(
            PricingVersion.effective_from.desc(), PricingVersion.id
        ).limit(limit)
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # 权限辅助
    # ------------------------------------------------------------------

    def project_exists(self, project_id: uuid.UUID) -> bool:
        """项目是否存在。

        **局部 import**：`projects.models` 是本包的上游，模块级 import 会
        让 `projects` 的任何重构都可能撞上环（`files/repository.py:510`
        与 `planning/repository.py` 是同一手法）。

        用 `select(Project.id).limit(1)` 而不是 `session.get(Project, ...)`：
        后者会把整个实体（含所有列）加载进 identity map，
        而我们只要一个布尔 —— 那些列永远用不上，却会跟着会话活到结束。
        """
        from rednote.modules.projects.models import Project

        return (
            self._session.execute(
                select(Project.id).where(Project.id == project_id).limit(1)
            ).first()
            is not None
        )


__all__ = [
    "ATTEMPT_SOURCE_REF_PREFIX",
    "DEFAULT_PENDING_SCAN_LIMIT",
    "BillingRepository",
    "attempt_source_ref",
    "attempt_source_ref_expr",
]
