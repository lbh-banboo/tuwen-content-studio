"""步骤 3 信息审核与生产基线（WP22）。

**对外接口**：

| 用途 | 位置 |
|---|---|
| 端点 | `api.router`（7 个端点：1 个协议 + 6 个本包补充） |
| 业务规则 | `service`（**唯一对外入口**） |
| 完成条件（纯函数） | `readiness.step3_readiness` |
| 组装顺序（纯函数） | `context_order.order_context_items` |
| 数据库级保证 | `ddl.all_trigger_ddl` / `ddl.trigger_names` |

**四张表**（数据结构 02 §6）：

| 表 | 性质 |
|---|---|
| `information_drafts` | 审核工作区，**可变**（一个项目一份，`project_id` 唯一） |
| `production_baselines` | 基线，**不可变**（`id` 同时 FK `version_nodes.id`） |
| `baseline_items` | 基线条目，**不可变**（含「未纳入项」与原因） |
| `step_approvals` | 步骤批准，**不可变**（步骤 1/2/3 通用，闭合 OP-151） |

**分层**（单向）：

    baseline_types ← readiness
    baseline_types ← context_order
    baseline_types ← models → platform.immutability
    baseline_types ← repository → models
    baseline_types ← service → repository / readiness / context_order / versions（WP11 接缝）
    api → service / dependencies

显式 import 模型与 router，使 `migrations/env.py` 的 autogenerate
与 `main.py` 的装配都能看见它们。

---

## 本包的四条硬约束（违反即返工）

1. **"信息草案"不是 Brief 的草稿** —— `brief_drafts`（WP18）是**解构前的
   工作区**，`information_drafts` 是**审核工作区**。名字极像、作用完全不同：
   混用一张表会让"审核时改了字段、解构结果被覆盖"双向踩踏，而两边都
   **读起来正常工作**（`information_drafts.project_id` 唯一，与
   `briefs.project_id` 同构，是结构性的隔离而不只是命名约定）。

2. **`ref` 与 `snapshot` 必须同时存在**（决策 1）——
   只存 `ref`：知识更新后**被污染**且静默（R1，本包最危险的一条）；
   只存 `snapshot`：无法回答"这条来自哪一版"（AC-24 断链）。
   两个字段答的是两个不同问题：`ref` 答"当时批准的是哪一版"，
   `snapshot` 答"当时批准的值是多少"。

3. **不落任何 `*_count` 聚合列**（决策 7）——
   本项目已踩 10 次（ISS-78/84/85/86 同族）。"存在阻断项"必须写成
   对明细行的否定式（`NOT EXISTS (... state <> 'SETTLED')`）。
   一旦落列，它与明细脱节时**不报错**，只会让 AC-09 失效 ——
   而 AC-09 失效意味着**基线可以被非法推进**。

4. **OP-88 的 `BASELINE` 那一半在本包闭合** ——
   `ddl.py` 的正向（子表 → 节点）与反向（节点 → 子表）两个延迟触发器，
   反向那个挂在**共用表 `version_nodes`** 上并用
   `WHEN (entity_type = 'BASELINE')` 收窄（与 WP18 的 `BRIEF`、
   WP20 的 `KNOWLEDGE`、WP23 的 `PROMPT` 三个并存，互不干扰）。

## 接缝调用不得改签名

`versions.service.register_version(entity_type="BASELINE", entity_id=project_id, …)`、
`versions.repository.VersionRepository`、
`projects.dependencies.ProjectAccessDep` —— 全部**原样调用**。

⚠️ **`entity_id` 必须是 `project_id`**：`BASELINE` 的"逻辑实体"是
**该项目的那条基线链**，不是"某一条基线" —— 一条基线是链上的一个修订。
传成 `baseline_id` 会让 `trg_production_baselines_node_consistency` 判据 3
直接拒绝，也会让"同一项目的第二条基线"拿到修订号 1 而不是 2。
"""

from rednote.modules.baseline import models  # noqa: F401
from rednote.modules.baseline.api import router  # noqa: F401

__all__: list[str] = ["router"]
