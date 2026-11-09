"""WP25 五张表的存储模型。

按数据结构文档 02 §11「任务、外部服务与费用」建立（**§11 本仓无副本**，
表名与用途由三处独立交叉引用确认：`gateway/models.py` 的"刻意不建的表"块、
`jobs/models.py` 的模块 docstring、WP13/WP16 两份迁移的同类块 —— 见
`docs/m3/WP25-*.md` §1.4 第 7 条）。

| 表 | 角色 | 可变性 |
|---|---|---|
| `pricing_versions` | 供应商计价版本（单价一旦发布不可改） | **不可变**（触发器拦 UPDATE/DELETE） |
| `cost_estimates` | 费用预估（项目 × 批次类型） | 可变（`lock_version`） |
| `project_budgets` | 项目预算**账面**（限额 / 已结算 / 已预占）—— 一项目一份 | **高频可变**（单条原子条件 UPDATE，**无 `lock_version`**） |
| `budget_reservations` | 预占**明细**（每次预占一行 + 状态机） | 可变（状态机条件 UPDATE） |
| `usage_ledger` | 费用账本（追加式） | **不可变**（触发器拦 UPDATE/DELETE） |

---

## 一、账面与明细为什么分成两张表（本包最关键的一处设计）

这不是规范化洁癖 —— 两者的**更新模式完全不同**：

| | `project_budgets` | `budget_reservations` |
|---|---|---|
| 一行代表 | 一个项目的**当前账面** | **一次**预占意图 |
| 更新 | 高频单条条件 UPDATE（每次预占改一行） | 每次预占**插一行**，随状态迁移改自己那行 |
| 不变量 | `committed + reserved <= limit` | 每行状态自身合法 |
| 无 `lock_version` | 更新全是单条原子条件 UPDATE，**没有读-改-写窗口** | 同上 |

与 WP12 的 `quota_accounts` / `upload_sessions` **完全同构** ——
那是本仓已跑通并测过的形态。`QuotaAccount` 的 docstring 已论证过
"为什么没有 `lock_version`"：乐观锁用于"读出来、改几个字段、写回去"
这种需要携带旧值的场景，而条件 UPDATE 把"检查"和"写入"压在同一条语句里，
加乐观锁反而引入一个多余的失败分支。

## 二、★ `usage_ledger` 为什么**没有** `state` 列

文档 §1.6 R4 曾设想"每笔带 `state`（PENDING / CONFIRMED / REVERSED）"，
R5 同时要求账本**不可变**（ADR-13 原话："不可被后续修改或删除，
**与费用账本、版本快照同级对待**"）。**实现时发现两者不可兼得**：

    "状态迁移"是就地 UPDATE，而"不可篡改"禁止 UPDATE。

只能放弃一个。放弃"状态列"，改后形态更简单：

| 原设计 | 改后 | 好处 |
|---|---|---|
| `state = PENDING` | 没有对应分录 | "未结"是**查询的结论**，不存在"状态写错" |
| `state = CONFIRMED` | 追加一条 `BILL` 分录 | 与"只追加"一致 |
| `state = REVERSED` | 追加一条 `REVERSAL` 负数分录 | 同上；被冲正的原分录**不动** |

唯一的代价是"未结清单"每次现算（一个 `NOT EXISTS` 反连接）。
判据也更强了：**UPDATE / DELETE 各断言被 `23001` 拒绝** ——
若真有 `state` 列，这条判据根本写不出来。

## 三、★ 冲正为什么必须引用被冲正的分录

`REVERSAL` 分录带 `reverses_ledger_id`，且 CHECK 要求
"`entry_kind = 'REVERSAL'` ⟺ `reverses_ledger_id IS NOT NULL`"。

没有这条约束时，一条负数分录可以是任意东西 ——
"冲掉 A 那笔"与"凭空减一笔"在数据上长得一样，
于是**账面差额永远解释不清**，而"没法解释的差额"是最贵的一种缺陷
（要靠人去数过去的每一笔）。

## 四、★ 时间列：`occurred_at` 与 `created_at` 是两件事

`usage_ledger.occurred_at` 是**费用发生的时间**（供应商侧事件时间，
可能远早于入账），`created_at` 是**入账时间**。两者分开的理由：

* 按 `created_at` 做月度汇总 ⇒ 一笔跨月补录的账会被算进错误的月份；
* 按 `occurred_at` 做汇总 ⇒ 需要它是**调用方给的**，不能默认 `now()`。

因此 `occurred_at` 是必填（由调用方传入），且**没有 `server_default`** ——
默认值会让"忘了传"表现为"就是现在"，而那个错误在月度汇总上才显形。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.billing import money
from rednote.modules.billing.types import (
    CURRENCY_LIST_SQL,
    ESTIMATE_STATE_DRAFT,
    ESTIMATE_STATE_LIST_SQL,
    LEDGER_KIND_LIST_SQL,
    MAX_LEDGER_DESCRIPTION_CHARS,
    MAX_LEDGER_SOURCE_REF_CHARS,
    MAX_PRICING_LABEL_CHARS,
    MAX_PRICING_NOTE_CHARS,
    MAX_RESERVATION_NOTE_CHARS,
    MAX_RESERVATION_REF_CHARS,
    NEGATIVE_LEDGER_KIND_LIST_SQL,
    POSITIVE_LEDGER_KIND_LIST_SQL,
    PRICING_STATE_DRAFT,
    PRICING_STATE_LIST_SQL,
    PRICING_UNIT_LIST_SQL,
    RESERVATION_STATE_HELD,
    RESERVATION_STATE_LIST_SQL,
)
from rednote.platform.base import (
    Base,
    created_at_column,
    lock_version_column,
    updated_at_column,
    uuid_pk,
)
from rednote.platform.immutability import guard_immutable

# 本模块需要不可变保护的表。`ddl.py` 里有同值副本（迁移要在不加载 ORM
# 元数据的前提下拿到表名），两处必须一致 —— 测试会交叉核对。
#
# **为什么 `pricing_versions` 在其中**（R8）：改一个单价 ⇒
# **历史所有预估与账单全部失真**，且改的人无感（他以为只影响未来）。
# 单价的"变更" = 插新版本。
#
# **为什么 `usage_ledger` 在其中**（R5）：ADR-13 原话要求审计
# "不可被后续修改或删除，与**费用账本**、版本快照同级对待"。
# 账本可改 = 整个审计体系可改。
#
# **为什么 `project_budgets` / `budget_reservations` 不在其中**：
# 它们是**活的账面**，每次预占都要改（条件 UPDATE）。它们靠
# CHECK + 单条原子语句 + 明细对账（R10）保证正确性，而不是靠不可变。
#
# **为什么 `cost_estimates` 不在其中**：预估是**打算花多少**，
# 结算时要回填实际值 —— 它必须可变（`lock_version` 是其 ETag 载体）。
IMMUTABLE_TABLES: tuple[str, ...] = ("pricing_versions", "usage_ledger")

#: 金额列类型：`NUMERIC(20, 8)`。与 `money.AMOUNT_PRECISION/SCALE` **同源**。
#:
#: 写成模块级常量而不是各列各写一遍：`Numeric(20, 8)` 出现五次，
#: 若某一次写成 `Numeric(20, 6)`，那个列会**静默截断**到 6 位
#: （PG 的 NUMERIC 是四舍五入，不报错），而对账永远差一分钱。
AMOUNT_TYPE: Final[Numeric] = Numeric(money.AMOUNT_PRECISION, money.AMOUNT_SCALE)
QUANTITY_TYPE: Final[Numeric] = Numeric(
    money.QUANTITY_PRECISION, money.QUANTITY_SCALE
)

#: 零的 SQL 字面量（`server_default` 用）。
_ZERO: Final[str] = "0"


class PricingVersion(Base):
    """供应商计价版本。**不可变**。

    一行 = (供应商, 模型档案可选, 币种, 一组单价, 生效时间)。
    **单价一旦发布不可改** —— 改价就是插一个新版本，因此
    "历史所有预估与账单"永远能用当时的单价复核（R8）。

    ## 为什么单价放 JSONB 而不是五列

    单价按 `unit` 分（token / 张 / 秒 / 次），而**单位集合会增长**
    （新供应商、新模式）。五列方案每加一个单位要一次迁移，
    且"没填的那一列"与"这个版本不支持该单位"无法区分
    （都是 NULL 或 0）。

    JSONB + 应用层校验（`pricing.build_price_map`）的表达力对得上需求：
    值形状是 `{"TOKEN": "0.00012000", "IMAGE": "0.05000000"}` ——
    **值也是字符串**，与全仓"金额一律字符串"一致（`Numeric` 的 JSONB
    承载物在 PG 里是 `jsonb` number，而 jsonb number 是 `numeric` 的
    无标度形态，读取时位数会丢，因此存字符串）。
    """

    __tablename__ = "pricing_versions"

    id: Mapped[uuid.UUID] = uuid_pk()

    provider_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("providers.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # **可空**：一份供应商级计价（该供应商所有模型同一价目）是合法形态。
    model_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("model_profiles.id", ondelete="RESTRICT"),
        nullable=True,
    )

    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=PRICING_STATE_DRAFT
    )

    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    label: Mapped[str] = mapped_column(String(MAX_PRICING_LABEL_CHARS), nullable=False)

    # 单价表：`{"TOKEN": "0.00012000", …}`。**字符串值**，见类 docstring。
    unit_prices: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # 生效起点（供应商报价单的生效日）。**含时间**：同一供应商可能
    # 一天内调价两次（促销），只到日期就无法表达先后。
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    note: Mapped[str | None] = mapped_column(
        String(MAX_PRICING_NOTE_CHARS), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            f"state IN ({PRICING_STATE_LIST_SQL})", name="state_valid"
        ),
        CheckConstraint(
            f"currency IN ({CURRENCY_LIST_SQL})", name="currency_valid"
        ),
        # 单价表必须是 JSON **object**（标量/数组会让 `prices[unit]` 抛
        # `TypeError` 而不是可读错误）。
        CheckConstraint(
            "jsonb_typeof(unit_prices) = 'object'", name="unit_prices_is_object"
        ),
        CheckConstraint(
            "char_length(label) > 0", name="label_not_blank"
        ),
        # **一份版本不能有两种 (provider, model, currency, effective_from)**
        # —— 否则"这个时点该用哪份价目"没有确定答案，而预占与结算
        # 可能各自选中不同的一份，**两边都不报错**。
        #
        # `postgresql_nulls_not_distinct=True`：PG 默认认为两个 NULL
        # **互不相同**，于是 (provider, NULL, CNY, t) 可以插很多行 ——
        # 而语义上它们是同一份"供应商级"价目。本项目锁定 PG 17
        # （NULLS NOT DISTINCT 自 PG 15 起支持），因此能用它把
        # "model_profile 为空"也纳入唯一性（与 `model_profiles` 同手法）。
        UniqueConstraint(
            "provider_id",
            "model_profile_id",
            "currency",
            "effective_from",
            name="uq_pricing_versions_provider_model_currency_from",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_pricing_versions_provider_state", "provider_id", "state"),
        Index("ix_pricing_versions_model", "model_profile_id"),
    )


class CostEstimate(Base):
    """费用预估（项目 × 批次类型）。**可变**（结算时回填实际值）。

    ## `estimated_amount` 与 `actual_amount` 必须同时在，且差异显式

    R9 的处置：预估 100 元、实际 300 元，若不报警则项目一直按 100 元的
    节奏跑。因此三列一起：

    | 列 | 含义 |
    |---|---|
    | `estimated_amount` | 预估（冻结引用当时的 `pricing_version_id`） |
    | `actual_amount` | 实际（结算时回填；未结算为 NULL） |
    | `variance_amount` | `actual - estimated`（未结算为 NULL） |

    `variance_amount` **单独存一列**而不是算出来：它是"要不要报警"的判据列，
    而"算出来的列"无法被索引、也无法被 CHECK 约束。
    它由服务层在回填 `actual_amount` 的**同一条 UPDATE** 里写出 ——
    分两条写会留下"实际值有了、差异还是 NULL"的中间态。

    ## `lock_version` 为什么这里要

    与 `plan_drafts` 同理：结算回填是"读出来 → 改几个字段 → 写回去"
    （要读原预估才能算差异），因此有读-改-写窗口，需要乐观锁。
    与 `project_budgets` 形成对照 —— 那张表全是条件 UPDATE，故不需要。
    """

    __tablename__ = "cost_estimates"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 批次类型（`TRIAL` / `SCALE` / `BULK`，取值域由 WP24 持有）。
    # **本表刻意不落 CHECK** —— 取值域属于 `planning.types.BATCH_KINDS`，
    # 在这里再写一份 `IN ('TRIAL','SCALE','BULK')` 就是第两处副本，
    # 而 WP24 若调整档位，这里会**静默拒绝新的合法值**。
    batch_kind: Mapped[str] = mapped_column(String(20), nullable=False)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    pricing_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("pricing_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )

    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=ESTIMATE_STATE_DRAFT
    )

    estimated_amount: Mapped[Decimal] = mapped_column(AMOUNT_TYPE, nullable=False)

    actual_amount: Mapped[Decimal | None] = mapped_column(
        AMOUNT_TYPE, nullable=True
    )
    variance_amount: Mapped[Decimal | None] = mapped_column(
        AMOUNT_TYPE, nullable=True
    )

    # 预估的明细行：`[{"unit": "TOKEN", "quantity": "…", "unitPrice": "…",
    # "amount": "…"}, …]`。**全部字符串**（与 `unit_prices` 同一理由）。
    #
    # 为什么存下来而不是从 `estimated_amount` 反推：AC-26 的「估算」核对
    # 要求"能复核这个数字怎么来的" —— 只留总数等于要求人重新猜单价。
    lines: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # 产生这份预估的人（可空 —— 系统自动算的没有具体人）。
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    lock_version: Mapped[int] = lock_version_column()

    __table_args__ = (
        CheckConstraint(f"state IN ({ESTIMATE_STATE_LIST_SQL})", name="state_valid"),
        CheckConstraint(f"currency IN ({CURRENCY_LIST_SQL})", name="currency_valid"),
        CheckConstraint("quantity >= 1", name="quantity_positive"),
        CheckConstraint(
            "estimated_amount >= 0", name="estimated_amount_non_negative"
        ),
        CheckConstraint(
            "jsonb_typeof(lines) = 'array'", name="lines_is_array"
        ),
        # ★ 三列的一致性：**"已结算"与"有实际值"必须同时成立**。
        #
        # 少了这条，一行 `(state='SETTLED', actual_amount=NULL)` 是合法数据，
        # 而它会被读成"结清了但不知道花了多少" —— 对账把它算作已结、汇总
        # 把它当 0 加，**差额无声消失**。
        CheckConstraint(
            "(state = 'SETTLED' AND actual_amount IS NOT NULL"
            " AND variance_amount IS NOT NULL)"
            " OR (state <> 'SETTLED' AND actual_amount IS NULL"
            " AND variance_amount IS NULL)",
            name="settled_requires_actual",
        ),
        Index("ix_cost_estimates_project_created", "project_id", "created_at"),
        Index("ix_cost_estimates_state", "state"),
    )


class ProjectBudget(Base):
    """每项目一行预算**账面**。**本包的核心表**。

    "预算并发不超承诺"（W10 的验收标题）的全部正确性都落在这一张表的
    **更新方式**上：条件 `committed + reserved + delta <= limit`
    必须在**单条 UPDATE 的 WHERE 里**，不能是 Python 的 `if`。

    为什么（与 `quota_accounts` 同一论证）：两个并发请求各自读到
    "还剩 100 元"，各自判断"我要预占的 80 元放得下"，然后各自扣减 ——
    结果账面 160 元进了 100 元的余量，**超限且没有任何一方察觉**。
    把判断条件放进 `WHERE` 之后，两条语句在数据库层串行化：
    后执行的那条会因为条件不成立而 `rowcount = 0`。

    **`ON DELETE RESTRICT`**：项目被删时不允许静默丢掉预算账面。

    ## `reserved_amount` 允许为负吗？不允许

    三列都有 `>= 0` 的 CHECK。负的 `reserved_amount` 是一个**从来不应
    存在**的状态（少还了会让它变负，而 `release` 语句带
    `WHERE reserved_amount >= :delta` 防"还多了"）。
    让它被 CHECK 挡住变成显式错误，比"静默记着一笔负数"好排障。

    ## `revision_no` 为什么不要

    与 `runtime_settings` 不同：这里的更新是**就地原子更新**，
    没有"历史版本"的概念。要历史请看账本（`usage_ledger`）——
    那才是费用的追加式记录。给账面加版本号会造出第二个真值来源。
    """

    __tablename__ = "project_budgets"

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        primary_key=True,
    )

    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    # 承诺额（管理者设定）。
    limit_amount: Mapped[Decimal] = mapped_column(AMOUNT_TYPE, nullable=False)
    # 已结算（对账确认后的累计）。
    committed_amount: Mapped[Decimal] = mapped_column(
        AMOUNT_TYPE, nullable=False, server_default=text(_ZERO)
    )
    # 已预占（在途，会随释放/结算变动）。
    reserved_amount: Mapped[Decimal] = mapped_column(
        AMOUNT_TYPE, nullable=False, server_default=text(_ZERO)
    )

    # 谁设的上限（可空 —— 迁移回填的行没有人）。
    set_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint(f"currency IN ({CURRENCY_LIST_SQL})", name="currency_valid"),
        CheckConstraint("limit_amount >= 0", name="limit_non_negative"),
        CheckConstraint("committed_amount >= 0", name="committed_non_negative"),
        CheckConstraint("reserved_amount >= 0", name="reserved_non_negative"),
        # ★ 账面不变量。数据库层兜底 —— 应用层的条件 UPDATE 是主防线，
        # 但数据回填、运维脚本、将来的批量对账都可能绕过它
        # （`quota_accounts` 的同一条理由）。
        #
        # ⚠️ 判据纪律第 2 条：**这条 CHECK 保证恒真的条件不能当判据**。
        # 测试要断言的是"并发预占后这条等式仍成立"（即并发路径确实
        # 把判断放进了 WHERE），而不是"available >= 0"。
        CheckConstraint(
            "committed_amount + reserved_amount <= limit_amount",
            name="within_limit",
        ),
    )


class BudgetReservation(Base):
    """一次预占意图 + 状态机。**明细表**。

    `budget_reservations` 与 `upload_sessions` **同构** —— 后者的 docstring
    原话"**预占与释放必须成对**"，靠状态机 + 条件 UPDATE 保证
    "每次释放只允许一次"：同一请求到两次，第二次 `rowcount` 为 0。

    ## 状态机

        HELD ──settle──> SETTLED      （终态，占额度 → 已结算）
          │
          ├──release──> RELEASED      （终态，占额度 → 归还）
          │
          └──expire───> EXPIRED       （**仍持有预占**，等人回收）
                          │
                          └──release──> RELEASED

    `EXPIRED` **仍在持有集合里**（`types.HOLDING_RESERVATION_STATES`）：
    它的钱还占着账面，只是标记了"该回收"。若把它排除，
    `project_budgets.reserved_amount` 会与明细对不上，
    而 `recompute_reserved` 的对账判据立刻报差异。

    ## 为什么 `settled_amount` 可以与 `amount` 不等

    预占是"先扣住多少"（估计），结算是"实际花了多少"。
    两者不等是常态（生成了 48 篇而不是 50 篇）。差额在结算时
    就地在 `project_budgets` 上修正 —— 这正是 `files` 的
    `finalize_reservation(reserved_release, effective_add)` 的同一形态。
    """

    __tablename__ = "budget_reservations"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 可选地挂在某份预估上。**可空是刻意的**（§2.4 第 6 项）：
    # 预估是"打算花多少"（批量、粗粒度），预占是"先扣住多少"
    # （单次、细粒度），两者可以不同粒度。
    estimate_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("cost_estimates.id", ondelete="RESTRICT"),
        nullable=True,
    )

    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=RESERVATION_STATE_HELD
    )

    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(AMOUNT_TYPE, nullable=False)
    settled_amount: Mapped[Decimal | None] = mapped_column(
        AMOUNT_TYPE, nullable=True
    )

    # 业务引用（哪次任务 / 哪个批次要的钱）。自由文本 —— 引用对象跨包，
    # 建 FK 会让本迁移依赖那些包的落地顺序（WP16 对
    # `model_profiles.pricing_version_id` 的同一处置）。
    ref: Mapped[str | None] = mapped_column(
        String(MAX_RESERVATION_REF_CHARS), nullable=True
    )
    note: Mapped[str | None] = mapped_column(
        String(MAX_RESERVATION_NOTE_CHARS), nullable=True
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint(
            f"state IN ({RESERVATION_STATE_LIST_SQL})", name="state_valid"
        ),
        CheckConstraint(f"currency IN ({CURRENCY_LIST_SQL})", name="currency_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        # 已结算时必须有 `settled_amount`，未结算时必须为 NULL。
        # **不能只写 `settled_amount IS NULL OR state = 'SETTLED'`** ——
        # 那允许"已结算但金额是 NULL"，而对账读它会当成 0。
        CheckConstraint(
            "(state = 'SETTLED' AND settled_amount IS NOT NULL)"
            " OR (state <> 'SETTLED' AND settled_amount IS NULL)",
            name="settled_amount_matches_state",
        ),
        # 结算额非负（可以是 0 —— "跑完了但供应商免费"是合法情形，
        # 与"没跑"（RELEASED）是两件事）。
        CheckConstraint(
            "settled_amount IS NULL OR settled_amount >= 0",
            name="settled_amount_non_negative",
        ),
        # 持有中的预占查找（释放/结算路径按它定位）。
        Index(
            "ix_budget_reservations_holding",
            "project_id",
            "state",
            "created_at",
        ),
        Index("ix_budget_reservations_estimate", "estimate_id"),
    )


class UsageLedger(Base):
    """费用账本（追加式）。**不可变**。

    ADR-13 原话把审计与"**费用账本**、版本快照"同级对待 ——
    因此本表与 `audit_events` / `version_nodes` / `gateway_egress`
    同级，**复用同一套不可变机制**（`platform/immutability.py` 的
    `guard_immutable` + `immutable_trigger_ddl`），不另造一套。

    ## 为什么没有 `state` 列 —— 见模块 docstring 第二节

    一句话：R4 想要的"状态"与 R5 要求的"不可变"不可兼得。
    "未结"是查询的结论（`NOT EXISTS` 反连接），"已确认"是追加一条
    `BILL` 分录，"冲正"是追加一条负数 `REVERSAL` 分录。

    ## `quantity` / `unit` 为什么可空

    账本要承载两类事实：
      · 按用量入账（`entry_kind='USAGE'`）⇒ 有 `quantity` + `unit`；
      · 按账单入账（`entry_kind='BILL'`）⇒ 只有金额，无用量。

    用 NULL 而不是 0 表达"没有用量"：`quantity = 0` 是一个**可疑**状态
    （零用量的账单），而 NULL 是"这件事上没有这一维"。
    两者的 CHECK 是"USAGE 必须有、BILL 必须无"——一律写成
    `(entry_kind = 'USAGE' AND quantity IS NOT NULL) OR (…)`
    要求**逐类列出**，因为默认"其余都可以"会让新增分录种类时
    忘记回答"它要不要用量"。

    ## `occurred_at` 是必填且无 `server_default` —— 见模块 docstring 第四节
    """

    __tablename__ = "usage_ledger"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 可空：供应商级账单没有具体项目分摊。
    entry_kind: Mapped[str] = mapped_column(String(20), nullable=False)

    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    # 金额。**可为负**（只有 REVERSAL 允许，见下方的 CHECK）。
    amount: Mapped[Decimal] = mapped_column(AMOUNT_TYPE, nullable=False)

    quantity: Mapped[Decimal | None] = mapped_column(QUANTITY_TYPE, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # 计量单位与用量的**互相一致性**：有单位必须有用量，反之亦然。
    # 少了这条，一行 `(unit='TOKEN', quantity=NULL)` 是合法数据，
    # 而它会被按"不知道多少 token"处理 —— 而单价 × NULL 在 SQL 里
    # 等于 NULL，于是它**静默地不参与任何汇总**。
    #
    # ★ 冲正指向的原分录。见模块 docstring 第三节。
    reverses_ledger_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("usage_ledger.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 产生这笔分录的来源引用：
    #   · `attempt:<job_attempts.id>` —— 来自一次任务尝试（OP-108 的关联）
    #   · `bill:<供应商账单号>` —— 来自账单导入
    #   · `manual:<操作者>` —— 人工调整
    source_ref: Mapped[str | None] = mapped_column(
        String(MAX_LEDGER_SOURCE_REF_CHARS), nullable=True
    )

    # 幂等键：同一次尝试的同一类分录只能有一条。
    # **可空**（人工分录没有天然幂等键），唯一性用部分索引只约束非空值。
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    description: Mapped[str | None] = mapped_column(
        String(MAX_LEDGER_DESCRIPTION_CHARS), nullable=True
    )

    # 费用**发生**时间（见模块 docstring 第四节）。**无 server_default**。
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(f"entry_kind IN ({LEDGER_KIND_LIST_SQL})", name="kind_valid"),
        CheckConstraint(f"currency IN ({CURRENCY_LIST_SQL})", name="currency_valid"),
        # ★ 金额符号与分录种类的**互相一致性**。
        #
        # 必须逐类列出（不用"其余都行"的兜底），理由：新增分录种类时
        # "它的金额是正是负"是一个**必须显式回答**的问题。兜底写法会让
        # 新种类默认允许任意符号，而"一笔负的 USAGE"会让月度汇总变负
        # 且不报错。
        #
        # 注意 `amount <> 0`：零金额分录是**无意义**的。
        # 允许它会让"这笔账寄出去了吗"多一个无信息的答案，
        # 而账本的价值正在于每一条都对应一次真实的资金变动。
        CheckConstraint(
            "(entry_kind IN (" + POSITIVE_LEDGER_KIND_LIST_SQL + ") AND amount > 0)"
            " OR (entry_kind IN (" + NEGATIVE_LEDGER_KIND_LIST_SQL + ") AND amount < 0)"
            " OR (entry_kind = 'ADJUSTMENT' AND amount <> 0)",
            name="amount_sign_matches_kind",
        ),
        # ★ 冲正必须指向被冲正的分录，且**只有冲正能指向**。
        CheckConstraint(
            "(entry_kind = 'REVERSAL' AND reverses_ledger_id IS NOT NULL)"
            " OR (entry_kind <> 'REVERSAL' AND reverses_ledger_id IS NULL)",
            name="reversal_target_matches_kind",
        ),
        # 冲正不能指向自己（自指会让"顺着链找到源头"永远走不到头）。
        CheckConstraint(
            "reverses_ledger_id IS NULL OR reverses_ledger_id <> id",
            name="reversal_not_self",
        ),
        # 用量与单位成对出现。
        CheckConstraint(
            "(quantity IS NULL) = (unit IS NULL)",
            name="quantity_unit_paired",
        ),
        # 按用量入账**必须**有用量与单位（见类 docstring）。
        CheckConstraint(
            "entry_kind <> 'USAGE' OR quantity IS NOT NULL",
            name="usage_requires_quantity",
        ),
        # 单位取值域（只有 USAGE 有单位，因此这条天然只管到它）。
        CheckConstraint(
            "unit IS NULL OR unit IN (" + PRICING_UNIT_LIST_SQL + ")",
            name="unit_valid",
        ),
        CheckConstraint(
            "quantity IS NULL OR quantity >= 0", name="quantity_non_negative"
        ),
        # 幂等键的唯一性：**只约束非空值**（人工分录没有天然幂等键）。
        # 部分唯一索引而不是普通 UNIQUE —— 普通 UNIQUE 下多行 NULL
        # 是允许的（PG 认为 NULL != NULL），但它同时会让
        # `UNIQUE` 的语义看起来"已覆盖全部行"，而实际只覆盖了一部分。
        # 用部分索引把"只有非空值受约束"这件事写在 DDL 里。
        Index(
            "uq_usage_ledger_idempotency",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        # 按项目 + 发生时间汇总（月度对账的主查询路径）。
        Index("ix_usage_ledger_project_occurred", "project_id", "occurred_at"),
        # 按来源找（"这次尝试记了什么"）。
        Index("ix_usage_ledger_source", "source_ref"),
        # 冲正链（"这笔被冲正了吗"）。
        Index("ix_usage_ledger_reverses", "reverses_ledger_id"),
    )


# ---------------------------------------------------------------------------
# 参考日期的类型别名（供 schema.py 复用，避免 import date 两次）
# ---------------------------------------------------------------------------

#: 金额与用量的**标度**在本模块只出现一次（`AMOUNT_TYPE` / `QUANTITY_TYPE`），
#: 其余地方一律用它们。理由见上方注释 —— 五处各写一遍 `Numeric(20, 8)`
#: 就有五次写错的机会，而写错是静默截断。

guard_immutable(PricingVersion, UsageLedger)


__all__ = [
    "AMOUNT_TYPE",
    "IMMUTABLE_TABLES",
    "QUANTITY_TYPE",
    "BudgetReservation",
    "CostEstimate",
    "PricingVersion",
    "ProjectBudget",
    "UsageLedger",
]
