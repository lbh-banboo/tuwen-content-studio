"""WP27 阶段 5：给 `media_items` 加 `pending_reference_bindings`（R1 的输入暂存位）。

revision: b7c1e4a9d203
down_revision: df6bb2f41f84

**本迁移只做一件事：加一列 + 两条 CHECK。**

## 一、★ 为什么必须加这一列（不是"顺手加的"）

R1 要求"每张图绑**具体的**参考素材"，绑定的落点是
`media_versions.reference_bindings`（`df6bb2f41f84` 已建，DDL 触发器
`media_binding_consistency_ddl` 已把它固化为 1~8 条 + 素材存在 + 同项目）。

但生成是**异步**的：`POST /contents/{contentId}/media-generations`
在端点事务里入队后**立刻返回**，任务在**另一次事务**里才跑
（`KIND_MEDIA_GENERATE` 走 `jobs` 队列）。于是存在一个空窗：

| 时刻 | `media_versions` 有绑定吗 |
|---|---|
| 端点返回（入队完成） | **还没有** —— 版本行是任务跑完才插的 |
| 任务开始跑 | 还没有 |

若绑定只在 `media_versions` 上，处理器**读不到本次请求的绑定** ⇒
R1 退化成"没有绑定"，而更糟的是它会**拿到空集合而不报错**
（`_claim_and_validate` 的 `raw_bindings` 若是 `None` 会被形状校验拒，
但若下游放宽就会静默通过）——这正是 ISS-95「声称有防线」族的形态。

## 二、为什么落在 `media_items` 而不是别的三处

| 候选 | 裁决 |
|---|---|
| 并入 `media_generation_contexts` | ❌ 那张表是**事后留痕**（每次生成一行、不可变、`provider_call_id` 要派发后才知道），本需求是**事前意图** |
| 塞进 `generation_request` | ❌ 那一列是 `MediaGenerationRequest` 的**三键白名单**（`taskKind`/`modelProfileId`/`batchKind`），语义是"**怎么**生成"；绑定是"**绑什么**"。`schemas.validate_generation_request_payload` 是 **fail-closed 拒多余键**的，扩它要改三处契约，还让"请求形状"与"内容输入"在同一 JSON 里分叉 |
| 先插 `media_versions` 占位行 | ❌ 给**不可变**表开"未完成版本"先例，直接撞 `df6bb2f41f84` 立的不可变触发器与 `bindings_size` 约束 |
| **`media_items` 加一列** | ✅ **选定**。该表本就是**可变投影**（`lock_version` + `set_generation_request` 都在它上面），"下一次生成要绑什么"与它现有角色**完全同构** |

## 三、★ 它不是第二个真值来源

同一时刻只有一处有值：

```
入队： media_items.pending_reference_bindings = [...]   （有值）
      media_versions.reference_bindings        = NULL   （还没有行）
生成成功：
      media_versions.reference_bindings        = [...]  （落版本）
      media_items.pending_reference_bindings   = NULL    （投影清空）
```

生成成功时投影列被置 `NULL`，由 `service` 在**同一事务**内完成
⇒ 任务重放不会读到上一轮的绑定。判据：`tests/test_media_wp27.py`
的"投影清空"用例（成对断言：版本行有绑定**且**投影列已 NULL）。

## 四、两条 CHECK 都必须容许 `NULL`

`NULL` = "没有待生成的绑定"（合法态）；`[]` = 空数组，**非法**
（被 `pending_bindings_size` 拒掉）。

⚠️ 这两种是**两种不同的事实**：混起来会让"忘了传绑定"伪装成
"明确传了空绑定"，而后者有明确的产品含义（R1 说"可为空但必须
显式标 null + 理由"——显式标记正是本列的职责）。

## 五、G2 铁律执行记录

1. **不删任何非本迁移创建的对象** —— 本迁移**只加**一列两条 CHECK，
   `downgrade` 只删这三条，**不碰** `rednote_reject_mutation()` 与
   `df6bb2f41f84` 建的 8 个触发器（G2 第二条）。
2. **`op.f()` 包 CHECK 名** —— 与 `df6bb2f41f84` 同款：裸名会被
   Alembic 的 naming convention 再加一次前缀。
3. **CHECK 的 SQL 文本在本文件写「化石字面量」**，**不从 `media.types`
   import**。⚠️ 本迁移首版取的是**相反**取向（把文本委托给活常量
   `PENDING_BINDINGS_IS_ARRAY_SQL` / `PENDING_BINDINGS_SIZE_SQL`），
   被 `tools/quality/check_migration_value_domain_no_live_import.py`
   的 G1 判红后**已改回字面量（2026-10-10）**。理由（该判据 §三 的完整
   论证，此处只摘结论）：

   · 委托的表现是**新库拿新值、老库留旧值**。`ALTER TABLE ... ADD
     CONSTRAINT` 只在迁移执行那一刻生效一次，之后 `types.py` 怎么改，
     老库那两条 CHECK 都**不动**。于是"新库能过、老库过不了"，而
     **没有任何判据会红** —— 这是"看不见的错"。
   · 手写字面量的表现是"改一处忘另一处"，结果是**看得见的红**。
     两个风险里前者更坏。
   · 字面量取自 `types.py` 的**同一取值**（`'array'` /
     `BETWEEN 1 AND 8`）：`1` / `8` 是**写迁移那一刻**的值，
     此后 `types` 怎么改都不再影响本文件。

## 六、判据覆盖与已知盲区

| 判据 | 覆盖什么 |
|---|---|
| `test_schema_parity.py` | 新列的**存在与类型** |
| `check_migration_model_parity.py` | 模型 ⟷ 迁移的**列清单**对账 |
| `check_migration_value_domain_no_live_import.py` G1 | 本文件的 CHECK 文本**不含活引用**（即本节 §五第 3 条） |
| `test_media_wp27.py::test_pending_bindings_*`（3 条） | 两条 CHECK 的**行为**：`is_array` 拒 `object`/标量；`size` 拒 `[]` 与 9 条；对照容许 `NULL`/1 条/8 条 |

⚠️ **盲区（登记为已知缺口，不假装覆盖）**：`check_migration_model_parity.py`
不比较 CHECK 的**表达式文本**（只比名字与成员关系）⇒ **本文件这侧**
虽然已冻结成字面量（不再可能被 `types` 带着漂移），但
`models.py` 那侧仍引用活常量；若两边取值分叉（仍能建库、仍能通过
列对账），**没有判据会红**。这条盲区**不是**本迁移引入的，本判据
（`check_migration_value_domain_no_live_import.py`）只保证**迁移侧是化石**，
不保证**模型侧与化石一致** —— 后者需要"parity 比 CHECK 表达式文本"，
属独立动作，登记为已知缺口（见 `docs/m3/WP27-静态图适配与产品参考说明.md`）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ⚠️ **刻意不 import 任何 `rednote.*` 名字**（见 §五第 3 条）：
# 迁移文本必须是"建库那一刻的化石"。连 `MIN_REFERENCE_ASSETS` /
# `MAX_REFERENCE_ASSETS` 这类数值常量都不 import —— 用 f-string 拼进来
# 与直接 import SQL 片段是**同一个病**（文本仍跟着活常量走），
# `check_migration_value_domain_no_live_import.py` 的污染闭包照样判红。
# 本文件对这两条下界的口径是 `BETWEEN 1 AND 8`（= 2026-10-08 当时的取值）。

revision: str = "b7c1e4a9d203"
down_revision: str | None = "df6bb2f41f84"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """加一列 + 两条 CHECK。**不复用** `df6bb2f41f84` 的 `create_table`。"""
    op.add_column(
        "media_items",
        sa.Column(
            "pending_reference_bindings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    # ⚠️ 用 `op.create_check_constraint` 而非 `batch_alter_table`：
    # 本仓PG 17，`ALTER TABLE ... ADD CONSTRAINT` 对 NOT VALID 的
    # CHECK 同样会扫全表，而本列**新增即全 NULL**（无历史行会被判失败）
    # ⇒ 直接 ADD 即可，不需要 NOT VALID 后再 VALIDATE。
    # ⚠️ 字面量与 `models.py` 的 f-string **展开后逐字一致**
    #   （模型侧写 `f"... OR ({PENDING_BINDINGS_IS_ARRAY_SQL})"`，故此处
    #   也保留那层括号 —— 让"迁移文本 == 模型文本"在文本层就成立，
    #   而不是靠 PG 的表达式规范化来抹平差异）。
    op.create_check_constraint(
        op.f("ck_media_items_pending_bindings_is_array"),
        "media_items",
        "pending_reference_bindings IS NULL"
        " OR (jsonb_typeof(pending_reference_bindings) = 'array')",
    )
    op.create_check_constraint(
        op.f("ck_media_items_pending_bindings_size"),
        "media_items",
        "pending_reference_bindings IS NULL"
        " OR (jsonb_array_length(pending_reference_bindings) BETWEEN 1 AND 8)",
    )


def downgrade() -> None:
    """只删本迁移创建的三样，**不碰** `df6bb2f41f84` 的任何对象。"""
    op.drop_constraint(
        op.f("ck_media_items_pending_bindings_size"), "media_items", type_="check"
    )
    op.drop_constraint(
        op.f("ck_media_items_pending_bindings_is_array"), "media_items", type_="check"
    )
    op.drop_column("media_items", "pending_reference_bindings")
