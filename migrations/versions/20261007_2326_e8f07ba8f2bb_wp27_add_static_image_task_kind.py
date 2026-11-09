"""WP27：把 `STATIC_IMAGE` 补进 `prompt_templates.task_kind` 的取值域（9 → 10）。

revision: e8f07ba8f2bb
down_revision: 1c1c814b248e

**本迁移不改表结构、不建表、不建索引、不建触发器** —— 它只做一件事：
`ALTER TABLE prompt_templates DROP CONSTRAINT ck_prompt_templates_task_kind_valid`
然后以 10 个值重建同名 CHECK。

---

## 一、这是本仓第一次「改已有表的 CHECK」

全仓检索（`grep -rn "create_check_constraint" migrations/versions/`）**零命中**，
`drop_constraint` 的 3 处命中全是 `type_="foreignkey"`（拆环用，见
WP23 `:500` 与 WP26 `:1203/:1208/:1213`）。

既有 20 条迁移的 CHECK 一律是**建表时**用 `sa.CheckConstraint` 写进
`CREATE TABLE` —— 因为此前每一包改取值域的方式都是**新表**，从不需要
改动已交付的表。

本迁移是**第一个例外**，原因见下一节。它是**结构性**的例外，不是风格选择：
`prompt_templates` 表由 WP23（`3cb74d02edc5`）交付、已被 WP24/25/26 消费，
**不可能重建成新表**。

## 二、★ 为什么必须有这条迁移 —— 一条判据盲区（本文件存在的主要理由）

现场（2026-10-07 WP27 落库期实测）：
`modules/prompts/types.py` 的 `TASK_KINDS` 原本**恰好 9 个值**，
**不含 `STATIC_IMAGE`**。而 WP27 的 `resolve_active_version(task_kind="STATIC_IMAGE")`
第一道门就是 `task_kind not in TASK_KINDS ⇒ 抛 REASON_TASK_KIND_UNKNOWN`。
⇒ R5 的 fail-closed「查不到激活版本就不出图」**退化成永久 fail**：
不是"暂时没有激活版本"，而是**这个 task_kind 永远不可能有激活版本** ——
连往 `prompt_templates` 里插一行静态图模板都会被 CHECK 拒掉。
主路径正例**建不起来**，WP27 的端点无法端到端走通。

### ⚠️ 关键：既有的 `check_migration_model_parity.py` **抓不到这个缺陷**

该脚本的机制是「**两个空库**同时从零点建」：
库 A 跑 `alembic upgrade head`（走迁移链），库 B 跑 `create_all`（走模型），
再逐对象比 `pg_get_constraintdef(con.oid)` 的**全文**。

问题在于：**WP23 迁移的 CHECK 是委托取值的** ——

```python
# migrations/versions/20260924_1359_3cb74d02edc5_...wp23...py:139~142
def _task_kind_sql() -> str:
    from rednote.modules.prompts.types import TASK_KIND_LIST_SQL
    return TASK_KIND_LIST_SQL
```

⇒ 在**空库**上，WP23 建出的 CHECK 跟着 `types.py` 走，**自动变成 10 值**；
库 B 的 `create_all` 也是 10 值 ⇒ **两侧相等 ⇒ 判据全绿**。

而**已执行过 WP23 的生产库**里，那条迁移**早已跑完**，CHECK 仍是 9 值 ——
`upgrade head` 在这一侧**不会重跑 WP23**，于是：

| 库 | 该 CHECK 的值域 | parity 是否覆盖 |
|---|---|---|
| 空库 A（`upgrade head`） | 10 值（WP23 委托 + 本迁移） | ✅ 覆盖 |
| 空库 B（`create_all`） | 10 值 | ✅ 覆盖 |
| **已执行过 WP23 的库** | **仍是 9 值** | ❌ **完全盲区** |

**这正是 WP26 迁移 docstring 那句「字面量让漂移可见，委托让漂移静默」的实证**
（`20261006_1700_1c1c814b248e_...:43~55`）—— 委托把"改上游取值域"这件事
的后果**静默切成了两半**：新库自动对新值，老库留在旧值，而**没有任何既有判据
会发现这个分叉**。

⇒ **本迁移就是把「老库那一半」补上。** 它对新库是幂等的（drop 10 值 → create 10 值），
对老库是修复性的（drop 9 值 → create 10 值）。两种路径终态相同。

## 三、取值域写成**字面量**（与 WP26 同向、与 WP23 相反）

| 取法 | 后果 |
|---|---|
| 委托 `TASK_KIND_LIST_SQL`（WP23 的取法） | 本迁移会在**下一次**改取值域时**又一次静默分叉** —— 老库永远追不上 |
| **字面量（本迁移）** | 取值域在迁移里是**冻结的历史事实**；未来再加第 11 个值时，`check_migration_model_parity.py` 会在两个空库上比出「模型侧 11 值 / 最新迁移（即本迁移）10 值」的差异 ⇒ **判据报红，逼人写新迁移** |

**判据报红这件事本身就是本迁移的价值所在**：它把"需要一条新迁移"从
「靠人记得」变成「机器报红」。WP25（`daea2b27212c`）与 WP26（`1c1c814b248e`）
是同一取向，本迁移跟随。

**10 个字面量必须与 `TASK_KIND_LIST_SQL` 逐字相同**（`sorted()` 后的顺序）：

```
'BRIEF_DECOMPOSE', 'BRIEF_ENRICH', 'CONTENT_COPY', 'CONTENT_STRUCTURE',
'EVIDENCE_COLLECTION', 'FACT_VERIFICATION', 'KNOWLEDGE_SEARCH',
'QUALITY_CHECK', 'REPORT_GENERATE', 'STATIC_IMAGE'
```

单一来源 = `modules/prompts/types.py` 的 `TASK_KIND_LIST_SQL`
（`= ", ".join(f"'{n}'" for n in sorted(TASK_KINDS))`）。
如果本文件的字面量与它不等，`check_migration_model_parity.py` 在两个空库上
比 `pg_get_constraintdef` 就会当场报红 —— 所以**不需要**另建判据来对账它，
PG 自己就是判据。

## 四、G2 铁律的执行记录

与 WP18/WP20/WP21/WP22/WP23/WP24/WP25/WP26 同一做法（本文件是手工写的，
未走 autogenerate）。三件事逐条处理：

1. **删掉 autogenerate 提议的 M0 探针表 `op.drop_table`** ——
   本迁移**不是** autogenerate 产物（它不含任何 `drop_table`），
   此条对本文件**不适用**，写在这里是为了让"三件事"的清单保持完整、
   便于评审确认「不是漏了」；
2. **`downgrade` 不重建、不删除任何本包不拥有的对象** ——
   本迁移只碰 `prompt_templates` 上的**一条 CHECK**，它由 WP23 拥有。
   `downgrade` 的唯一动作是把这条 CHECK **写回 WP23 交付时的 9 值形态**
   （= 恢复原状，不是"删掉"）。**不得**在此文件里 `drop_table("prompt_templates")`
   —— 那会连带毁掉 WP24/25/26 的既有数据，且 `upgrade` 侧也重建不回来
   （原表结构属 WP23，不在本文件里）；
3. **`from sqlalchemy.dialects import postgresql` 是否要补** ——
   本迁移**没用** `postgresql.JSONB`（无新列），因此该 import **不适用**。
   但保留 `from collections.abc import Sequence` 与 `import sqlalchemy as sa`：
   前者是 `down_revision` 类型注解所需，后者是本仓迁移文件的标准头
   （WP23 `:113~119` 同款）。**留一个空 import 比少一个 import 好** ——
   本项目实测过 `postgresql` 漏 import 导致「语法正确、diff 正常、
   **执行时 NameError**」已连续触发 9 次（登记册 G2 / ISS-02）。

## 五、⚠️ `downgrade` 的已知风险（不是缺陷，是必须写明的边界）

若库中**已有** `task_kind='STATIC_IMAGE'` 的行（即 WP27 已真实落过静态图模板），
`downgrade()` 的 `CREATE CHECK (9 值)` 会因**存量行违反约束**而失败。
这是**正确行为**（数据回退不可自动化），不是 bug。
处置口径与 WP11/WP15 的「不可变表不给 downgrade 删数据」一致：
**报错让人看见，比静默删行好**。实操上若要回退，须先人工处置这些行。

## 六、验证（三处，缺一不可）

1. **干净库**：`alembic upgrade head` ⇒ 建库成功，且
   `pg_get_constraintdef` 读出 10 值；
2. **已有表库**（已执行过 WP23、CHECK 为 9 值）：再跑 `upgrade` ⇒
   本迁移修复为 10 值。**这一处是 parity 脚本覆盖不到的，必须手工验一次**
   （§二的盲区）；
3. **`tools/quality/check_migration_model_parity.py`** ⇒ 两个空库逐对象比对全绿。

另：`downgrade` 往返（`upgrade head` → `downgrade -1` → `upgrade head`）
须在三处都跑一次 —— ISS-79 的教训是「往返修复」这种事只有真跑了才知道。

---

> **本迁移的归属**：它由 WP27 落库期发现并补登，但改的是 **WP23 的既有契约**。
> 因此它与 `modules/prompts/types.py` 的取值域改动、`tests/test_prompts_wp23.py`
> 的断言改动构成**一个原子变更**（跨包扩取值域），应作为**单独一次提交**，
> 不与 WP27 阶段 4 的 service/api 混提。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e8f07ba8f2bb"
down_revision: str | Sequence[str] | None = "1c1c814b248e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# ★ 取值域字面量（**刻意不从 types.py import** —— 理由见本文件 docstring §三）
#
# 单一来源：modules/prompts/types.py 的 `TASK_KIND_LIST_SQL`
#           = ", ".join(f"'{name}'" for name in sorted(TASK_KINDS))
#
# 两边逐字相同由 `tools/quality/check_migration_model_parity.py` 在两个空库上
# 比 `pg_get_constraintdef` 保证 —— PG 自己就是判据，不需要另建脚本对账。
# ---------------------------------------------------------------------------

# 本迁移之后（10 个值，含 WP27 的 STATIC_IMAGE）
_TASK_KIND_SQL_WITH_STATIC_IMAGE: str = (
    "task_kind IN ("
    "'BRIEF_DECOMPOSE', "
    "'BRIEF_ENRICH', "
    "'CONTENT_COPY', "
    "'CONTENT_STRUCTURE', "
    "'EVIDENCE_COLLECTION', "
    "'FACT_VERIFICATION', "
    "'KNOWLEDGE_SEARCH', "
    "'QUALITY_CHECK', "
    "'REPORT_GENERATE', "
    "'STATIC_IMAGE'"
    ")"
)

# WP23 交付时的原状（9 个值，**不含** STATIC_IMAGE）—— 仅供 downgrade 复原。
# ⚠️ 它是 WP23 那一刻的历史事实，**不得**随着 `TASK_KINDS` 的后续变化而改动。
_TASK_KIND_SQL_WP23_ORIGINAL: str = (
    "task_kind IN ("
    "'BRIEF_DECOMPOSE', "
    "'BRIEF_ENRICH', "
    "'CONTENT_COPY', "
    "'CONTENT_STRUCTURE', "
    "'EVIDENCE_COLLECTION', "
    "'FACT_VERIFICATION', "
    "'KNOWLEDGE_SEARCH', "
    "'QUALITY_CHECK', "
    "'REPORT_GENERATE'"
    ")"
)

_CHECK_NAME: str = "ck_prompt_templates_task_kind_valid"
_TABLE_NAME: str = "prompt_templates"


def upgrade() -> None:
    """把 `STATIC_IMAGE` 补进 `prompt_templates.task_kind` 的取值域。

    步骤是「先摘后挂」，**不能**用 `op.alter_column` 之类的原地改法 ——
    CHECK 约束在 PG 里没有"修改条件"的语法，只有 DROP + ADD。

    ⚠️ DROP 与 ADD 之间存在一个**极短的窗口**，此刻表上该列**无约束**。
    本迁移**不**包在显式事务外（alembic 默认整条迁移一个事务，
    PG 的 DDL 是事务性的）⇒ 并发写入落到窗口里的概率为零：
    要么整条迁移可见，要么整条不可见。因此**不加** `CONCURRENTLY`
    （CHECK 也不支持 `CONCURRENTLY`，那是 `CREATE INDEX` 的能力）。
    """
    op.drop_constraint(
        op.f(_CHECK_NAME),
        _TABLE_NAME,
        type_="check",
    )
    op.create_check_constraint(
        op.f(_CHECK_NAME),
        _TABLE_NAME,
        _TASK_KIND_SQL_WITH_STATIC_IMAGE,
    )


def downgrade() -> None:
    """写回 WP23 交付时的 9 值形态。

    ⚠️ **若库中已存在 `task_kind='STATIC_IMAGE'` 的行，本函数会失败**
    （存量行违反 9 值 CHECK）。这是**正确行为**：数据回退不可自动化。
    处置口径见 docstring §五 —— 报错让人看见，比静默删行好。
    """
    op.drop_constraint(
        op.f(_CHECK_NAME),
        _TABLE_NAME,
        type_="check",
    )
    op.create_check_constraint(
        op.f(_CHECK_NAME),
        _TABLE_NAME,
        _TASK_KIND_SQL_WP23_ORIGINAL,
    )