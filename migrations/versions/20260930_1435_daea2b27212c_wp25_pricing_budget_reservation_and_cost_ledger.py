"""WP25：供应商计价 / 预算并发预占 / 费用账本（5 张表 + 2 个触发器 + 1 条部分唯一索引）。

revision: daea2b27212c
down_revision: 022ba9804d66

**本迁移包含 5 张表**（`modules/billing/models.py` 的 `OWN_TABLES` 同值）：

| 表 | 性质 | 说明 |
|---|---|---|
| `pricing_versions` | **不可变** | 单价一旦发布不可改；改价 = 插新版本 |
| `cost_estimates` | 可变（`lock_version`） | 预估（估算 / 账单 / 重试核对 = AC-26 的"估算"一侧） |
| `project_budgets` | 可变（**无 `lock_version`**） | 一项目一份**账面**；更新全是单条原子条件 UPDATE |
| `budget_reservations` | 可变（状态机条件 UPDATE） | **一次**预占意图一行 |
| `usage_ledger` | **不可变** | 追加式；冲正靠新分录而不是改旧行 |

---

## 一、G2 铁律的执行记录（本文件是手工写全的）

与 WP18 / WP20 / WP21 / WP22 / WP23 / WP24 同一做法：**只取 `revision` /
`down_revision` 两个值，其余手工写全**。理由见
`docs/m2/WP22-ddl-草案.py` §一 —— autogenerate 在本项目反复产出
"删探针表 / 丢部分索引 WHERE / 不含触发器"的产物，逐条核对的手工成本
高于直接写全。

三件事已逐条处理（G2 铁律）：

1. **删掉 autogenerate 提议的 M0 探针表 `op.drop_table`** ——
   见 `downgrade()` 末尾的注释块（列举了被刻意剔除的语句，便于评审复核）；
2. **`downgrade` 不重建任何本包不拥有的对象** —— 尤其**不删
   `rednote_reject_mutation()`**（它属于 `platform`，被全项目共用；
   删了会让 WP11/WP15/WP16/WP17/WP18/WP20/WP22/WP23/WP24 的保护一起失效）；
3. **`from sqlalchemy.dialects import postgresql` 已补** —— 本迁移用了
   `postgresql.JSONB`（`pricing_versions.unit_prices` 与
   `cost_estimates.lines` 两列）。autogenerate 用了它却**不会自动加这行
   import**，结果是迁移文件语法正确、diff 看着正常，**执行时 `NameError`**。
   本坑在本项目已连续触发 9 次（登记册 G2 / ISS-02）。

### ★ 取值域字面量**刻意不从 `types.py` import**（与 WP23 的取向相反）

WP23 的迁移用 `_task_kind_sql()` 之类的委托函数从 `prompts.types` 取
`LIST_SQL`。**本迁移不这么做**，理由是"迁移是**历史快照**"：

| 取法 | 改取值域（如 `LEDGER_KINDS` 加一类）之后 |
|---|---|
| import 委托 | 老迁移**自动跟着变** ⇒ 新库建出 `IN (…新值…)`，而生产库上这条迁移**早已执行**、CHECK 仍是老的 ⇒ **新库能过、老库过不了**，且没有任何判据会报红 |
| 字面量（本迁移） | `tools/quality/check_migration_model_parity.py` 在**两个空库**上分别跑 `alembic upgrade head` 与 `create_all` ⇒ 两侧 CHECK 文本不等 ⇒ **判据报红**，逼人写新迁移 |

**字面量让漂移可见，委托让漂移静默** —— 这正是本仓判据纪律的取向。
因此本文件里出现的每一个取值域都写成**出处可查的**字面量，并在紧邻的
注释里点名它的单一来源（`modules/billing/types.py` 的哪个常量）。
WP24（`022ba9804d66`）是同一取向。

## 二、★ **一条**部分唯一索引 —— 本迁移最容易被 autogenerate 悄悄毁掉的东西

| 索引 | 键 | `WHERE` | 漏写 `WHERE` 的后果 |
|---|---|---|---|
| `uq_usage_ledger_idempotency` | `(idempotency_key)` | `idempotency_key IS NOT NULL` | 退化成"每行都必须唯一"⇒ **第二条人工分录（`ADJUSTMENT`，幂等键为 NULL）被拒**，而人工调整是账本唯一的纠错手段 |

**本包只有这一条**（对比 WP24 有两条）—— `models.py` 里只有
`usage_ledger` 的 `uq_usage_ledger_idempotency` 是 `unique=True` 且带
`WHERE`；其余 9 条都是**普通**索引（查询路径用，`op.create_index` 建）。
`ddl.PARTIAL_UNIQUE_INDEXES` 与 `ddl.model_index_specs()` 逐字段对账
（键列含顺序 + `WHERE` 的规范化文本），因此"少建一条"不会静默。

### 为什么是**部分**索引而不是普通 `UNIQUE`

同一笔用量被记两次（worker 重试、worker 崩溃后重投、人工补录）在
**金额上完全看不出异常** —— 账面多扣一倍，而每一笔分录本身都合法。
唯一能拦的位置是入账前，而"是不是同一笔"只能靠调用方给的幂等键。

普通 `UNIQUE(idempotency_key)` 对多行 NULL 是允许的（PG 认为 `NULL != NULL`），
语义上"能用"，但两处不好：

1. 人工分录（`ADJUSTMENT`）**没有天然幂等键**，只能留 NULL。
   普通 UNIQUE 让人误以为"全部行都被约束住了"，而实际只覆盖了一部分；
2. 一旦将来有人加了 `NOT NULL DEFAULT gen_random_uuid()` 来"让唯一约束生效"，
   幂等就**彻底失效**且不报错 —— 每行的键都不同，重复入账畅通无阻。

部分索引把"只有非空值受约束"**写进 DDL 文本**，这个意图无法被静默改掉。

⚠️ **部分唯一索引在 `pg_constraint` 里根本不出现** —— 只查约束表的核对
**看不到**这条防线。核对必须查 `pg_indexes.indexdef`（WP22 已登记此坑）。

⚠️ **`WHERE` 写错 = 索引静默失效**（PG 不报错，索引照建、永远不命中）。
因此证据脚本做**双向**活性验证：造一个**不匹配** `WHERE` 的重复行断言它
**能插进去**（证明索引确实在按 `WHERE` 过滤），再造一个**匹配**的重复行
断言被拒（`23505`）。只做后者会漏掉"`WHERE` 永远命不中"这一整类失效。

**本迁移不重写这条索引的 SQL**，而是调
`modules/billing/ddl.py` 的 `partial_unique_index_ddl()` ——
与测试、证据脚本**同源**。

## 三、2 个触发器（DDL 不写在本文件里）与"本包没有跨表一致性触发器"

触发器 DDL 来自 `modules/billing/ddl.py` 的 `all_trigger_ddl()` ——
与测试完全同源。**2 项**，由 `models.IMMUTABLE_TABLES` 驱动：

| # | 触发器 | 挂在哪张表 | 类型 |
|---|---|---|---|
| 1 | `trg_pricing_versions_immutable` | `pricing_versions` | BEFORE UPDATE/DELETE（复用共用函数 `rednote_reject_mutation`） |
| 2 | `trg_usage_ledger_immutable` | `usage_ledger` | 同上 |

**为什么 `pricing_versions` 要挂**（R8）：改一个单价 ⇒ **历史所有预估与
账单全部失真**，且改的人无感（他以为只影响未来）。单价的"变更" = 插新版本。

**为什么 `usage_ledger` 要挂**（R5）：ADR-13 原话要求审计"不可被后续修改
或删除，与**费用账本**、版本快照同级对待"。账本可改 = 整个审计体系可改。

**另 3 张表刻意不挂**：`cost_estimates`（结算要回填实际值，必须可变）、
`project_budgets` / `budget_reservations`（活的账面，每次预占都要改）。
它们靠 CHECK + 单条原子条件 UPDATE + 明细对账（R10）保证正确性。

### ★ `usage_ledger` 没有 `state` 列 —— 这不是省略，是判据的前提

文档 §1.6 R4 曾设想"每笔带 `state`（PENDING / CONFIRMED / REVERSED）"，
而 R5 要求账本不可变。**两者不可兼得**："状态迁移"是就地 UPDATE，
而"不可篡改"禁止 UPDATE。只能放弃一个 —— 放弃 `state` 列之后：

| 原设计 | 改后 | 好处 |
|---|---|---|
| `state = PENDING` | 没有对应分录 | "未结"是**查询的结论**（`NOT EXISTS` 反连接） |
| `state = CONFIRMED` | 追加一条 `BILL` 分录 | 与"只追加"一致 |
| `state = REVERSED` | 追加一条负数 `REVERSAL` 分录 | 被冲正的原分录**不动** |

**这直接决定了本包最有价值的一条判据能不能写出来**：UPDATE / DELETE
各断言被 `23001` 拒绝。若真有 `state` 列，"确认"必须就地 UPDATE，
那条判据根本写不出来，只能退化为"断言状态列存在"这种恒真式
（判据纪律第 11 条）。

**本包没有跨表一致性触发器**（`ddl.consistency_items()` 返回空列表）——
这是**有意的显式形状**而不是"忘了写"：本包 5 张表全部是**本包私有**的，
`providers` / `model_profiles` / `projects` 上的 FK 是**出向**的，
不构成跨包一致性不变量。WP18/WP20/WP22/WP23 的 OP-88 那一半在各自包里
闭合 —— 那批触发器要守的是"注册表（`version_nodes`）+ 领域表必须配对"，
而本包**没有任何表挂到 `version_nodes` 上**：计价版本是运营维护的价目，
不是内容产物，没有版本节点。

**`CREATE TRIGGER` 不是幂等的**（重复执行报 `DuplicateObject`，本机实测
复现于 WP22）。本迁移的 `upgrade` 走 `CREATE OR REPLACE FUNCTION` +
`CREATE TRIGGER`，在**空库 / 该迁移首次执行**的前提下次次成功。

## 四、建表顺序（依赖序，表级无环）

    pricing_versions     （引用 providers / model_profiles，均已存在）
        ↓
    cost_estimates       （引用 projects / pricing_versions / users）
        ↓
    project_budgets      （引用 projects / users）
        ↓
    budget_reservations  （引用 projects / cost_estimates / users）
        ↓
    usage_ledger         （引用 projects / users / **自身**）

五张表的全部被引用对象都由**更早的迁移**建好（`projects`/`users`/`providers`/
`model_profiles` 属 M1）。因此**不需要 `ALTER TABLE ... ADD CONSTRAINT`
分两步**，也不需要 `DEFERRABLE`（本包没有"注册表行与子表行由不同代码路径
写入"那种时序问题 —— 对照 WP22 的 6 条 `DEFERRABLE` FK）。

`usage_ledger.reverses_ledger_id → usage_ledger.id` 是**自引用**，在同一条
`create_table` 里声明即可：PG 的 FK 检查发生在**行插入 / 更新时**，
不是建表时，因此"表引用自己"没有时序问题。

## 五、★ 数据回填：本包**没有**回填 —— 这是决定，不是遗漏

与 WP24（回填 ADR-13 初始配置 + 存量项目副本）**不同**，本迁移末尾
**不写任何 DML**。两条理由，各有出处：

1. **`pricing_versions` 没有初始数据**。文档 §1.5 把"真实供应商单价数据"
   列入**不做的范围**：本机无供应商合同、无报价单（OP-04/WP04 未结论），
   "用哪个供应商、单价多少"是 WP04 的结论，现在填等于**用猜测约束未来**。
   因此本迁移只建表与机制，初始价目走运营录入（`POST` 路径尚未开放时
   由运维脚本插入 —— 那是 WP04 结论之后的事）。
2. **`project_budgets` 也刻意不回填**。存量项目"从未设过预算"与
   "设过 0 元"必须能区分：
   * 无行 ⇒ `service.get_budget_view` 返回 `configured=False` 的零值视图
     （页面能渲染），`reserve_budget` 会先 `ensure_budget` 惰性建行；
   * 有 `limit = 0` 的行 ⇒ 所有预占一律 `BUDGET_EXCEEDED`。

   若在此处为存量项目批量插零限额行，就等于把"**从未设过**"静默改写成
   "**设过且为零**" —— 而这两者的运维处置完全不同（前者是"去设一个额度"，
   后者是"你的额度是 0，去查为什么"）。`ensure_budget` 的
   `ON CONFLICT DO NOTHING` 已经让惰性建行在并发下正确，回填没有收益。

> 判据侧：`downgrade()` 因此**没有**"回灌数据"这一步的必要性说明 ——
> 本迁移不含数据，6 节的代价只涉及结构。

## 六、`downgrade()` 的代价（必须显式登记）

`downgrade()` 删表 ⇒ **账本与预占明细一并丢失**。ADR-13 要求账本
"不可被后续修改或删除，与费用账本、版本快照同级对待"，
而 `DROP TABLE` 比"改一行"更彻底 —— 这是本包**最重的一处回退代价**。

**这不构成"因此不准 downgrade"** —— 本仓的恢复演练（WP47 的 RPO/RTO）
需要 `downgrade` 可用（留 `NotImplementedError` 会让任何回滚尝试直接失败）。
正确处置是**把代价写明白**：回退前须导出 `usage_ledger` /
`budget_reservations` / `project_budgets` / `cost_estimates` /
`pricing_versions` 五张表全量（`COPY ... TO`），恢复时用 `downgrade`
之后的 `upgrade` + 回灌。

⚠️ **`usage_ledger` 的不可变触发器会拦住 `DELETE`，但挡不住 `DROP TABLE`** ——
这是"不可变"的边界，必须知道：不可变保护防的是**应用与运维的正常写入路径**
（含 `psql` 手工 UPDATE、排障脚本），不防 DDL 级操作（那需要另一套权限
与审计，属 WP47 的范围）。

## 七、落地实测记录（2026-09-30）

见本文件末尾的 `# 落地实测` 注释块（由收口脚本
`tools/m3/collect_wp25_evidence.py` 核对，不在这里重复计数）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# G2 铁律第三件：`postgresql` 导入必须补（本迁移用了 JSONB）。
# autogenerate 不会自动加这行 —— 结果是语法正确但执行时 NameError。
# 本坑在本项目已连续触发 9 次（登记册 G2 / ISS-02）。
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "daea2b27212c"
down_revision: str | Sequence[str] | None = "022ba9804d66"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """正向迁移。建表顺序见模块 docstring 第四节（依赖序，表级无环）。"""
    # ### commands auto generated by Alembic - please adjust! ###
    # （本文件手工写全，理由见模块 docstring 第一节）

    # ------------------------------------------------------------------
    # ---- 第 1 张：pricing_versions（**不可变**，计价版本）------------
    # 只引用 providers / model_profiles（均属 M1），因此排在最前。
    #
    # ⚠️ **本表没有 UPDATE 路径**（`repository.py` 只有
    # `insert_pricing_version` / `get_pricing_version` /
    # `find_usable_pricing_version` 读三个写一个）。这不是"还没写"，
    # 是设计：改价 = 插新版本（R8），触发器 `trg_pricing_versions_immutable`
    # 把这件事钉在数据库层。
    # ------------------------------------------------------------------
    op.create_table(
        "pricing_versions",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # 供应商。`ON DELETE RESTRICT`：删供应商时不允许静默丢掉它的价目
        # （历史预估/账单的复核全靠这些行）。
        sa.Column("provider_id", sa.UUID(), nullable=False),
        # 模型档案。**可空**：一份"供应商级"计价（该供应商所有模型同一价目）
        # 是合法形态，用 NULL 表达"不分模型"。
        sa.Column("model_profile_id", sa.UUID(), nullable=True),
        # 取值域 `('DRAFT', 'PUBLISHED', 'RETIRED')` 的单一来源 =
        # `modules/billing/types.py` 的 `PRICING_STATES` / `PRICING_STATE_LIST_SQL`。
        # （字面量而不 import，理由见模块 docstring 第一节末表。）
        sa.Column(
            "state",
            sa.String(length=20),
            server_default=sa.text("'DRAFT'"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        # 人读的版本名（如"2026Q3 官方价目"）。非空且非空白。
        sa.Column("label", sa.String(length=200), nullable=False),
        # 单价表：`{"TOKEN": "0.00012000", "IMAGE": "0.05000000"}`。
        # JSONB 而不是五列：单位集合会增长（新供应商、新模式），
        # 五列方案每加一个单位要一次迁移，且"没填的那一列"与
        # "这个版本不支持该单位"无法区分（都是 NULL 或 0）。
        #
        # **值是字符串**：`Numeric` 的 JSONB 承载物在 PG 里是 jsonb number，
        # 而 jsonb number 是 `numeric` 的**无标度**形态，读取时位数会丢
        # ⇒ 与全仓"金额一律字符串"一致地存字符串。
        sa.Column(
            "unit_prices",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        # 生效起点（供应商报价单的生效日）。**含时间**：同一供应商可能
        # 一天内调价两次（促销），只到日期就无法表达先后。
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('DRAFT', 'PUBLISHED', 'RETIRED')",
            name=op.f("ck_pricing_versions_state_valid"),
        ),
        sa.CheckConstraint(
            "currency IN ('CNY', 'USD')",
            name=op.f("ck_pricing_versions_currency_valid"),
        ),
        # 单价表必须是 JSON **object**（标量/数组会让 `prices[unit]` 抛
        # `TypeError` 而不是可读错误）。
        sa.CheckConstraint(
            "jsonb_typeof(unit_prices) = 'object'",
            name=op.f("ck_pricing_versions_unit_prices_is_object"),
        ),
        sa.CheckConstraint(
            "char_length(label) > 0",
            name=op.f("ck_pricing_versions_label_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name=op.f("fk_pricing_versions_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_profile_id"],
            ["model_profiles.id"],
            name=op.f("fk_pricing_versions_model_profile_id_model_profiles"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pricing_versions")),
        # ★ **一份版本不能有两种 (provider, model, currency, effective_from)**
        # —— 否则"这个时点该用哪份价目"没有确定答案，而预占与结算
        # 可能各自选中不同的一份，**两边都不报错**。
        #
        # NULLS NOT DISTINCT：PG 默认认为两个 NULL **互不相同**，于是
        # (provider, NULL, CNY, t) 可以插很多行 —— 而语义上它们是同一份
        # "供应商级"价目。本项目锁定 PG 17（NULLS NOT DISTINCT 自 PG 15
        # 起支持），因此能用它把"model_profile 为空"也纳入唯一性
        # （与 WP16 的 `uq_model_profiles_provider_model_revision` 同手法）。
        sa.UniqueConstraint(
            "provider_id",
            "model_profile_id",
            "currency",
            "effective_from",
            name="uq_pricing_versions_provider_model_currency_from",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_pricing_versions_provider_state",
        "pricing_versions",
        ["provider_id", "state"],
        unique=False,
    )
    op.create_index(
        "ix_pricing_versions_model",
        "pricing_versions",
        ["model_profile_id"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # ---- 第 2 张：cost_estimates（**可变**，预估）--------------------
    # 引用 projects / pricing_versions / users，三者均已存在
    # （pricing_versions 由**本迁移第 1 步**建）。
    # ------------------------------------------------------------------
    op.create_table(
        "cost_estimates",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        # 批次类型（`TRIAL` / `SCALE` / `BULK`，取值域由 **WP24** 的
        # `planning.types.BATCH_KINDS` 持有）。
        # ★ **本列刻意不落 CHECK** —— 在这里再写一份
        # `IN ('TRIAL','SCALE','BULK')` 就是第二处副本，而 WP24 若调整档位，
        # 这里会**静默拒绝新的合法值**。这是本迁移里唯一一处"故意没有约束"
        # 的列，写出来是为了让评审看得见它不是漏了。
        sa.Column("batch_kind", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        # 冻结引用当时的计价版本。**可空**：预估可以先建、价目后绑
        # （与 `budget_reservations.estimate_id` 的可空是两件事，见那张表）。
        sa.Column("pricing_version_id", sa.UUID(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        # 取值域 `('CANCELLED', 'DRAFT', 'RESERVED', 'SETTLED')`，
        # 单一来源 = `types.ESTIMATE_STATES`。
        sa.Column(
            "state",
            sa.String(length=20),
            server_default=sa.text("'DRAFT'"),
            nullable=False,
        ),
        # --- 金额三列：估 / 实 / 差（AC-26 的"估算/账单/重试核对"）--------
        # 全部 `NUMERIC(20, 8)` —— 与 `models.AMOUNT_TYPE` 同源
        # （`money.AMOUNT_PRECISION` / `AMOUNT_SCALE`）。
        # **不写成 `Numeric(20, 6)`**：PG 的 NUMERIC 是四舍五入、不报错，
        # 写错就**静默截断**，而对账永远差一分钱。
        sa.Column("estimated_amount", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("actual_amount", sa.Numeric(precision=20, scale=8), nullable=True),
        # `variance_amount` = `actual - estimated`，**单独存一列**而不是算出来：
        # 它是"要不要报警"的判据列，而"算出来的列"无法被索引、也无法被 CHECK。
        # 由服务层在回填 `actual_amount` 的**同一条 UPDATE** 里写出 ——
        # 分两条写会留下"实际值有了、差异还是 NULL"的中间态。
        sa.Column("variance_amount", sa.Numeric(precision=20, scale=8), nullable=True),
        # 预估的明细行：`[{"unit": "TOKEN", "quantity": "…", "unitPrice": "…",
        # "amount": "…"}, …]`。**全部字符串**（与 `unit_prices` 同一理由）。
        # 存下来而不是从 `estimated_amount` 反推：AC-26 的「估算」核对要求
        # "能复核这个数字怎么来的" —— 只留总数等于要求人重新猜单价。
        sa.Column(
            "lines",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        # 产生这份预估的人（可空 —— 系统自动算的没有具体人）。
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # 乐观锁：结算回填是"读出来 → 改几个字段 → 写回去"（要读原预估
        # 才能算差异），因此有读-改-写窗口。
        # **与 `project_budgets` 形成对照** —— 那张表全是条件 UPDATE，故不需要。
        sa.Column(
            "lock_version",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('CANCELLED', 'DRAFT', 'RESERVED', 'SETTLED')",
            name=op.f("ck_cost_estimates_state_valid"),
        ),
        sa.CheckConstraint(
            "currency IN ('CNY', 'USD')",
            name=op.f("ck_cost_estimates_currency_valid"),
        ),
        sa.CheckConstraint(
            "quantity >= 1",
            name=op.f("ck_cost_estimates_quantity_positive"),
        ),
        sa.CheckConstraint(
            "estimated_amount >= 0",
            name=op.f("ck_cost_estimates_estimated_amount_non_negative"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(lines) = 'array'",
            name=op.f("ck_cost_estimates_lines_is_array"),
        ),
        # ★ 三列的一致性：**"已结算"与"有实际值"必须同时成立**。
        #
        # 少了这条，一行 `(state='SETTLED', actual_amount=NULL)` 是合法数据，
        # 而它会被读成"结清了但不知道花了多少" —— 对账把它算作已结、汇总
        # 把它当 0 加，**差额无声消失**。
        #
        # 逐类列出（不用"其余都行"的兜底）：新增状态时必须显式回答
        # "它有没有实际值"。
        sa.CheckConstraint(
            "(state = 'SETTLED' AND actual_amount IS NOT NULL"
            " AND variance_amount IS NOT NULL)"
            " OR (state <> 'SETTLED' AND actual_amount IS NULL"
            " AND variance_amount IS NULL)",
            name=op.f("ck_cost_estimates_settled_requires_actual"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_cost_estimates_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pricing_version_id"],
            ["pricing_versions.id"],
            name=op.f("fk_cost_estimates_pricing_version_id_pricing_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_cost_estimates_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cost_estimates")),
    )
    op.create_index(
        "ix_cost_estimates_project_created",
        "cost_estimates",
        ["project_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_cost_estimates_state",
        "cost_estimates",
        ["state"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # ---- 第 3 张：project_budgets（**本包的核心表**）-----------------
    # 引用 projects / users。**主键就是 project_id** —— 一项目一份账面，
    # 用"唯一约束 + surrogate PK"会让"一项目一份"变成一条可以被
    # 单独 DROP 的约束，而主键不是。
    # ------------------------------------------------------------------
    op.create_table(
        "project_budgets",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        # 承诺额（管理者设定的上限）。
        sa.Column("limit_amount", sa.Numeric(precision=20, scale=8), nullable=False),
        # 已结算（对账确认后的累计）。
        sa.Column(
            "committed_amount",
            sa.Numeric(precision=20, scale=8),
            server_default=sa.text("0"),
            nullable=False,
        ),
        # 已预占（在途，会随释放/结算变动）。
        sa.Column(
            "reserved_amount",
            sa.Numeric(precision=20, scale=8),
            server_default=sa.text("0"),
            nullable=False,
        ),
        # 谁设的上限（可空 —— 惰性建的行、迁移回填的行没有人）。
        sa.Column("set_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "currency IN ('CNY', 'USD')",
            name=op.f("ck_project_budgets_currency_valid"),
        ),
        sa.CheckConstraint(
            "limit_amount >= 0",
            name=op.f("ck_project_budgets_limit_non_negative"),
        ),
        sa.CheckConstraint(
            "committed_amount >= 0",
            name=op.f("ck_project_budgets_committed_non_negative"),
        ),
        # 负的 `reserved_amount` 是一个**从来不应存在**的状态（少还了会让它
        # 变负，而 `release_budget` 的语句带 `WHERE reserved_amount >= :delta`
        # 防"还多了"）。让它被 CHECK 挡住变成显式错误，比"静默记着一笔负数"
        # 好排障。
        sa.CheckConstraint(
            "reserved_amount >= 0",
            name=op.f("ck_project_budgets_reserved_non_negative"),
        ),
        # ★★ **本包最关键的一条约束**：账面不变量。
        #
        # 数据库层兜底 —— 应用层的条件 UPDATE（`reserve_budget` 把
        # `committed + reserved + delta <= limit` 放进 `WHERE`）是**主防线**，
        # 但数据回填、运维脚本、将来的批量对账都可能绕过它。
        #
        # ⚠️ **判据纪律第 2 条**：这条 CHECK 在"条件 UPDATE 正确"的前提下
        # **恒真** —— 因此测试要断言的是"**并发预占之后**这条等式仍成立"
        # （即 N 个并发请求里成功数 × 单笔 <= 限额），而不是断言
        # `available >= 0` 这种恒真式。判据不能建立在 DB 已经保证的事上。
        sa.CheckConstraint(
            "committed_amount + reserved_amount <= limit_amount",
            name=op.f("ck_project_budgets_within_limit"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_budgets_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["set_by"],
            ["users.id"],
            name=op.f("fk_project_budgets_set_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("project_id", name=op.f("pk_project_budgets")),
        # **本表没有普通索引**：全部查询都按 `project_id` 走主键；
        # 对账要的 `list_budgeted_project_ids()` 是全表扫描（项目数量级为
        # 数百，且它跑在运维路径上）。
        # **也没有 `lock_version`**：更新全是单条原子条件 UPDATE，
        # 没有读-改-写窗口（见模块 docstring 第三节与 `quota_accounts` 的同一论证）。
    )

    # ------------------------------------------------------------------
    # ---- 第 4 张：budget_reservations（**可变**，预占明细 + 状态机）--
    # 引用 projects / cost_estimates / users。
    # ------------------------------------------------------------------
    op.create_table(
        "budget_reservations",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        # 可选地挂在某份预估上。**可空是刻意的**：预估是"打算花多少"
        # （批量、粗粒度），预占是"先扣住多少"（单次、细粒度），
        # 两者可以不同粒度 —— 强制挂靠会让"没有预估的直接预占"无法表达。
        sa.Column("estimate_id", sa.UUID(), nullable=True),
        # 状态机取值域 `('EXPIRED', 'HELD', 'RELEASED', 'SETTLED')`，
        # 单一来源 = `types.RESERVATION_STATES`。
        #
        # ⚠️ **`EXPIRED` 仍在持有集合里**（`types.HOLDING_RESERVATION_STATES`
        # = `{EXPIRED, HELD}`）：它的钱还占着账面，只是标记了"该回收"。
        # 若把它排除，`project_budgets.reserved_amount` 会与明细对不上，
        # 而 `recompute_reserved` 的对账判据立刻报差异。
        sa.Column(
            "state",
            sa.String(length=20),
            server_default=sa.text("'HELD'"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("settled_amount", sa.Numeric(precision=20, scale=8), nullable=True),
        # 业务引用（哪次任务 / 哪个批次要的钱）。自由文本 —— 引用对象跨包，
        # 建 FK 会让本迁移依赖那些包的落地顺序（WP16 对
        # `model_profiles.pricing_version_id` 的同一处置）。
        sa.Column("ref", sa.String(length=200), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('EXPIRED', 'HELD', 'RELEASED', 'SETTLED')",
            name=op.f("ck_budget_reservations_state_valid"),
        ),
        sa.CheckConstraint(
            "currency IN ('CNY', 'USD')",
            name=op.f("ck_budget_reservations_currency_valid"),
        ),
        sa.CheckConstraint(
            "amount > 0",
            name=op.f("ck_budget_reservations_amount_positive"),
        ),
        # 已结算时必须有 `settled_amount`，未结算时必须为 NULL。
        # **不能只写 `settled_amount IS NULL OR state = 'SETTLED'`** ——
        # 那允许"已结算但金额是 NULL"，而对账读它会当成 0。
        sa.CheckConstraint(
            "(state = 'SETTLED' AND settled_amount IS NOT NULL)"
            " OR (state <> 'SETTLED' AND settled_amount IS NULL)",
            name=op.f("ck_budget_reservations_settled_amount_matches_state"),
        ),
        # 结算额非负（可以是 0 —— "跑完了但供应商免费"是合法情形，
        # 与"没跑"（RELEASED）是两件事）。
        sa.CheckConstraint(
            "settled_amount IS NULL OR settled_amount >= 0",
            name=op.f("ck_budget_reservations_settled_amount_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_budget_reservations_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["estimate_id"],
            ["cost_estimates.id"],
            name=op.f("fk_budget_reservations_estimate_id_cost_estimates"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_budget_reservations_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_reservations")),
    )
    # 持有中的预占查找（释放 / 结算路径按它定位；`EXPIRED` 也在集合里）。
    op.create_index(
        "ix_budget_reservations_holding",
        "budget_reservations",
        ["project_id", "state", "created_at"],
        unique=False,
    )
    # "挂在这份预估上的预占有哪些"（预估取消时要先释放它们）。
    op.create_index(
        "ix_budget_reservations_estimate",
        "budget_reservations",
        ["estimate_id"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # ---- 第 5 张：usage_ledger（**不可变**，追加式账本）--------------
    # 引用 projects / users / **自身**。建在最后（`reverses_ledger_id`
    # 指向自己，见模块 docstring 第四节）。
    # ------------------------------------------------------------------
    op.create_table(
        "usage_ledger",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        # 分录种类 `('ADJUSTMENT', 'BILL', 'REVERSAL', 'USAGE')`，
        # 单一来源 = `types.LEDGER_KINDS`。
        sa.Column("entry_kind", sa.String(length=20), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        # 金额。**可为负**（只有 `REVERSAL` 允许，见下方 CHECK）。
        sa.Column("amount", sa.Numeric(precision=20, scale=8), nullable=False),
        # 用量。**`NUMERIC(30, 10)`** —— 与 `models.QUANTITY_TYPE` 同源
        # （`money.QUANTITY_PRECISION` / `QUANTITY_SCALE`）。
        # 精度比金额宽：token 数以亿计、单张图以秒计，10 位小数是给
        # "秒"这种单位留的（0.0000000001 秒）。
        sa.Column("quantity", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("unit", sa.String(length=20), nullable=True),
        # ★ 冲正指向的原分录。**自引用 FK**。
        #
        # 没有这一列时，一条负数分录可以是任意东西 —— "冲掉 A 那笔"与
        # "凭空减一笔"在数据上长得一样，于是**账面差额永远解释不清**，
        # 而"没法解释的差额"是最贵的一种缺陷（要靠人去数过去的每一笔）。
        sa.Column("reverses_ledger_id", sa.UUID(), nullable=True),
        # 产生这笔分录的来源引用：
        #   · `attempt:<job_attempts.id>` —— 来自一次任务尝试（OP-108 的关联）
        #   · `bill:<供应商账单号>` —— 来自账单导入
        #   · `manual:<操作者>` —— 人工调整
        sa.Column("source_ref", sa.String(length=200), nullable=True),
        # 幂等键：同一次尝试的同一类分录只能有一条。
        # **可空**（人工分录没有天然幂等键），唯一性用**部分索引**只约束非空值
        # —— 那条索引**不在这里**建，见下方 `op.execute` 段。
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=True),
        # 费用**发生**时间（供应商侧事件时间），与 `created_at`（入账时间）
        # 是两件事，**无 `server_default`**：
        #   · 按 `created_at` 做月度汇总 ⇒ 一笔跨月补录的账会被算进错误的月份；
        #   · 按 `occurred_at` 做汇总 ⇒ 需要它是**调用方给的**。
        # 默认值会让"忘了传"表现为"就是现在"，而那个错误在月度汇总上才显形。
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "entry_kind IN ('ADJUSTMENT', 'BILL', 'REVERSAL', 'USAGE')",
            name=op.f("ck_usage_ledger_kind_valid"),
        ),
        sa.CheckConstraint(
            "currency IN ('CNY', 'USD')",
            name=op.f("ck_usage_ledger_currency_valid"),
        ),
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
        sa.CheckConstraint(
            "(entry_kind IN ('BILL', 'USAGE') AND amount > 0)"
            " OR (entry_kind IN ('REVERSAL') AND amount < 0)"
            " OR (entry_kind = 'ADJUSTMENT' AND amount <> 0)",
            name=op.f("ck_usage_ledger_amount_sign_matches_kind"),
        ),
        # ★ 冲正必须指向被冲正的分录，且**只有冲正能指向**。
        sa.CheckConstraint(
            "(entry_kind = 'REVERSAL' AND reverses_ledger_id IS NOT NULL)"
            " OR (entry_kind <> 'REVERSAL' AND reverses_ledger_id IS NULL)",
            name=op.f("ck_usage_ledger_reversal_target_matches_kind"),
        ),
        # 冲正不能指向自己（自指会让"顺着链找到源头"永远走不到头）。
        sa.CheckConstraint(
            "reverses_ledger_id IS NULL OR reverses_ledger_id <> id",
            name=op.f("ck_usage_ledger_reversal_not_self"),
        ),
        # 用量与单位**成对出现**：少了这条，一行 `(unit='TOKEN', quantity=NULL)`
        # 是合法数据，而它会被按"不知道多少 token"处理 —— 而单价 × NULL
        # 在 SQL 里等于 NULL，于是它**静默地不参与任何汇总**。
        sa.CheckConstraint(
            "(quantity IS NULL) = (unit IS NULL)",
            name=op.f("ck_usage_ledger_quantity_unit_paired"),
        ),
        # 按用量入账**必须**有用量与单位（`BILL` 只有金额，无用量）。
        # 逐类列出而不是"其余都行"：新增分录种类时"它要不要用量"
        # 必须显式回答。
        sa.CheckConstraint(
            "entry_kind <> 'USAGE' OR quantity IS NOT NULL",
            name=op.f("ck_usage_ledger_usage_requires_quantity"),
        ),
        # 单位取值域 `('CALL', 'IMAGE', 'SECOND', 'TOKEN')`，
        # 单一来源 = `types.PRICING_UNITS`。
        # （只有 `USAGE` 有单位，因此这条天然只管到它。）
        sa.CheckConstraint(
            "unit IS NULL OR unit IN ('CALL', 'IMAGE', 'SECOND', 'TOKEN')",
            name=op.f("ck_usage_ledger_unit_valid"),
        ),
        sa.CheckConstraint(
            "quantity IS NULL OR quantity >= 0",
            name=op.f("ck_usage_ledger_quantity_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_usage_ledger_project_id_projects"),
            ondelete="RESTRICT",
        ),
        # **自引用**：被冲正的分录用 `ON DELETE RESTRICT` —— 但本表挂的是
        # 不可变触发器（拒绝一切 DELETE），因此这一条实际上永远走不到；
        # 留着是为了让"账本外部引用链"的意图在 DDL 里完整（且它是
        # `models.py` 的声明，两侧必须一致 —— parity 判据会核对）。
        sa.ForeignKeyConstraint(
            ["reverses_ledger_id"],
            ["usage_ledger.id"],
            name=op.f("fk_usage_ledger_reverses_ledger_id_usage_ledger"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_ledger")),
        # ⚠️ `uq_usage_ledger_idempotency` **不在这里** —— 它是部分唯一索引，
        # 由下方 `op.execute(ddl)` 段从 `modules/billing/ddl.py` 取值建出
        # （名字与 `WHERE` 只有一个来源，见模块 docstring 第二节）。
    )
    # 按项目 + 发生时间汇总（月度对账的主查询路径）。
    op.create_index(
        "ix_usage_ledger_project_occurred",
        "usage_ledger",
        ["project_id", "occurred_at"],
        unique=False,
    )
    # 按来源找（"这次尝试记了什么"）。
    op.create_index(
        "ix_usage_ledger_source",
        "usage_ledger",
        ["source_ref"],
        unique=False,
    )
    # 冲正链（"这笔被冲正了吗"）。
    op.create_index(
        "ix_usage_ledger_reverses",
        "usage_ledger",
        ["reverses_ledger_id"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # ⚠️⚠️ **一条部分唯一索引。`WHERE` 是判据的一部分。**
    #
    # SQL **不写在这里** —— 来自 `modules/billing/ddl.py` 的
    # `partial_unique_index_ddl()`，与测试、证据脚本同源。
    # 本包有三处（models / 迁移 / ddl.py）会写出索引规格，
    # 而"只比对名字"是不够的（名字相同、WHERE 不同是最可能的漂移形态），
    # 因此 `ddl.index_spec_drift()` 逐字段对账 **键列含顺序** + **WHERE 的
    # 规范化文本**。
    #
    # ⚠️ 它**不会出现在 `pg_constraint` 里** —— 核对必须查
    # `pg_indexes.indexdef`，只查约束表会完全看不到这条防线。
    # ------------------------------------------------------------------
    from rednote.modules.billing import ddl as billing_ddl

    for _index_name, _index_ddl in billing_ddl.partial_unique_index_ddl():
        op.execute(_index_ddl)

    # ------------------------------------------------------------------
    # 触发器：autogenerate **不生成触发器**，必须手工补。
    # DDL 来源与测试完全同源（`modules/billing/ddl.py` 的
    # `all_trigger_ddl`），因此这里**不重写 SQL**，而是直接调用它。
    #
    # **2 个**（`pricing_versions` + `usage_ledger` 的不可变保护，复用跨包
    # 共用的 `rednote_reject_mutation()`）—— **只挂触发器，不重建函数**。
    #
    # ⚠️ `CREATE TRIGGER` 在 PG 里**不做依赖检查** —— plpgsql 函数体在首次
    # 调用时才解析，因此"触发器先于函数存在"其实是安全的；但本循环仍按
    # (函数, 触发器) 顺序执行，读日志时更直观。
    #
    # ⚠️ 顺序必须是"先全部建表、再建触发器"：`all_trigger_ddl()` 里每项都带
    # `DROP FUNCTION IF EXISTS rednote_reject_mutation()` 之外的函数 DDL，
    # 而函数被多张表共用 —— 放在建表之间会让第 1 张表建完时函数已存在、
    # 第 2 张表建完时再 `CREATE OR REPLACE` 一次（幂等但日志难读）。
    # ------------------------------------------------------------------
    for _table, _function_ddl, _trigger_ddl, _drop_trigger, _drop_function in (
        billing_ddl.all_trigger_ddl()
    ):
        op.execute(_function_ddl)
        op.execute(_trigger_ddl)

    # ------------------------------------------------------------------
    # ★ **本迁移没有数据回填** —— 这是决定，不是遗漏。
    # 两条理由见模块 docstring 第五节（`pricing_versions` 的初始数据属
    # WP04 结论、`project_budgets` 的"从未设过"必须能与"设过 0 元"区分）。
    #
    # 因此这里**不出现 `Session(bind=op.get_bind())`**（对照 WP24 的第五节）。
    # 若将来要回填（例如 WP04 定了供应商之后补一份基准价目），
    # 那是**新迁移**的事 —— 老迁移不追加 DML，否则"这条迁移建出的历史
    # schema"会随应用代码演进而漂移。
    # ------------------------------------------------------------------

    # ### end Alembic commands ###


def downgrade() -> None:
    """反向迁移。

    必须补全，不能留 `NotImplementedError` —— 那会让任何回滚尝试直接失败，
    与 WP47 的恢复演练（RPO/RTO）目标冲突。

    **只删本迁移自己建出来的对象。** 判据不是"它在哪张表上"，
    而是"**这个对象是不是我建的**"：

      · `trg_pricing_versions_immutable` / `trg_usage_ledger_immutable`
        → 我建的，删。
      · `rednote_reject_mutation()` → 别人建的共用函数，**不删**。
        删它会撞依赖错误，并让 WP11/WP15/WP16/WP17/WP18/WP20/WP22/WP23/WP24
        的保护**一起失效**（WP16 差点踩、WP18/WP20/WP22/WP23/WP24 明确防了）。
        因此第 2 步用 `ddl.consistency_items()`（本包为空集），
        **不得**改用 `all_trigger_ddl()` —— 它的 `drop_function` 指向共用函数。
      · 5 张表 → 我建的，删（**反序**：先删引用别人的）。
      · 1 条部分唯一索引 → 随表删除，但**显式写出来**（见下）。
      · 数据行 → 随表一并删除（见模块 docstring 第六节的代价登记）。

    ⚠️ **本 `downgrade` 有真实且严重的数据损失**：`usage_ledger` 是费用账本，
    ADR-13 要求它"与费用账本、版本快照同级对待"。回退前须导出全量。
    """
    # ### commands auto generated by Alembic - please adjust! ###
    from rednote.modules.billing import ddl as billing_ddl

    # ---- 1. 先删触发器（触发器 DROP 必须先于函数 DROP）----
    # 顺序不能反：函数仍被触发器引用时，PG 会拒绝删除
    # （"cannot drop function ... because other objects depend on it"）。
    for _table, _function_ddl, _trigger_ddl, _drop_trigger, _drop_function in (
        billing_ddl.all_trigger_ddl()
    ):
        op.execute(_drop_trigger)

    # ---- 2. 只删本模块自己的函数（**本包为空集，即什么都不删**）----
    # `consistency_items()` 返回空列表是**有意的显式形状**：
    # 本包没有跨表一致性触发器（见模块 docstring 第三节），
    # 而"删本模块的跨表对象"退化为"什么都不删"恰好是**正确**的行为 ——
    # 共用函数 `rednote_reject_mutation` 不属于本模块。
    for _table, _function_ddl, _trigger_ddl, _drop_trigger, _drop_function in (
        billing_ddl.consistency_items()
    ):
        op.execute(_drop_function)

    # ---- 3. 再删表（**反序**：先删引用别人的，后删被引用的）----
    #
    # 反序的依据（建表正序见 `upgrade()` 的注释）：
    #   usage_ledger → budget_reservations → project_budgets
    #   → cost_estimates → pricing_versions
    #
    # `budget_reservations` 引用 `cost_estimates`，`cost_estimates` 引用
    # `pricing_versions` —— 正序删会撞 FK 依赖。`project_budgets` 与其余
    # 四张表无引用关系，位置自由（放在中间只为与正序对称）。

    # -- 第 5 张（usage_ledger）：先删 3 条普通索引 + 1 条部分唯一索引 --
    op.drop_index("ix_usage_ledger_reverses", table_name="usage_ledger")
    op.drop_index("ix_usage_ledger_source", table_name="usage_ledger")
    op.drop_index("ix_usage_ledger_project_occurred", table_name="usage_ledger")
    # ⚠️ 部分唯一索引在 drop_table 时也会被一并删掉；显式写出来是为了让
    # "我删了这条防线"在迁移日志里**可见**（与 WP22/WP24 同一做法）。
    # 名字从 `ddl.partial_unique_index_names()` 取，不手写 —— 手写会让
    # "改名后 downgrade 静默失效"（`DROP INDEX IF EXISTS` 不报错）。
    for _index_name in billing_ddl.partial_unique_index_names():
        op.drop_index(_index_name, table_name="usage_ledger")
    op.drop_table("usage_ledger")

    # -- 第 4 张（budget_reservations）--
    op.drop_index(
        "ix_budget_reservations_estimate", table_name="budget_reservations"
    )
    op.drop_index(
        "ix_budget_reservations_holding", table_name="budget_reservations"
    )
    op.drop_table("budget_reservations")

    # -- 第 3 张（project_budgets）：无索引 --
    op.drop_table("project_budgets")

    # -- 第 2 张（cost_estimates）--
    op.drop_index("ix_cost_estimates_state", table_name="cost_estimates")
    op.drop_index("ix_cost_estimates_project_created", table_name="cost_estimates")
    op.drop_table("cost_estimates")

    # -- 第 1 张（pricing_versions）--
    op.drop_index("ix_pricing_versions_model", table_name="pricing_versions")
    op.drop_index("ix_pricing_versions_provider_state", table_name="pricing_versions")
    op.drop_table("pricing_versions")

    # ------------------------------------------------------------------
    # ⚠️ G2 铁律第一件：**删掉 autogenerate 提议的 M0 探针表 drop**。
    #
    # autogenerate 在测试库上跑时，会把 M0 阶段留下的探针表当成"多余对象"
    # 并提议删掉它们。若把那些语句留在本迁移里，**在别的库上它们会删掉
    # 真实存在的表**（本坑已连续 9 次触发，见登记册 G2 与 ISS-02）。
    #
    # 被刻意**没有**写进本文件的内容（列举出来便于评审复核）：
    #
    #     op.drop_index(op.f('wp05_vec_probe_embedding_idx'), table_name='wp05_vec_probe', postgresql_using='hnsw')
    #     op.drop_table('wp05_vec_probe')
    #     op.drop_index(op.f('smoke_meta_created_at_idx'), table_name='smoke_meta')
    #     op.drop_table('smoke_meta')
    #
    # 本迁移只碰 pricing_versions / cost_estimates / project_budgets /
    # budget_reservations / usage_ledger 五张表 + 2 个触发器
    # + 1 条部分唯一索引。
    # ------------------------------------------------------------------
    # ### end Alembic commands ###
