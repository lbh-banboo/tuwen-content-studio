"""WP26：文案结构生成与标题候选（5 张表 + 7 个触发器 + 3 条环 FK 后置补建）。

revision: 1c1c814b248e
down_revision: daea2b27212c

**本迁移包含 5 张表**（`modules/content/models.py` 的 `OWN_TABLES` 同值）：

| 表 | 性质 | 说明 |
|---|---|---|
| `content_items` | 可变（`lock_version`） | 内容的**身份**行；承载会前进的 `current_version_id` 投影 |
| `content_versions` | **不可变** | 内容的一版；文案三件 + 段落级绑定 + 采纳落点 |
| `narrative_blueprints` | **不可变** | 叙事蓝图 9 字段（16 条字段级 CHECK + 2 条通用） |
| `title_candidates` | **不可变** | 标题候选 + 6 维评分（JSONB） |
| `generation_contexts` | **不可变** | 一次生成的上下文快照（OP-160 的答案） |

---

## 一、G2 铁律的执行记录（本文件是手工写全的）

与 WP18 / WP20 / WP21 / WP22 / WP23 / WP24 / WP25 同一做法：**只取
`revision` / `down_revision` 两个值，其余手工写全**。理由见
`docs/m2/WP22-ddl-草案.py` §一 —— autogenerate 在本项目反复产出
"删探针表 / 丢部分索引 WHERE / 不含触发器"的产物，逐条核对的手工成本
高于直接写全。

三件事已逐条处理（G2 铁律）：

1. **删掉 autogenerate 提议的 M0 探针表 `op.drop_table`** ——
   见 `downgrade()` 末尾的注释块（列举了被刻意剔除的语句，便于评审复核）；
2. **`downgrade` 不重建任何本包不拥有的对象** —— 尤其**不删
   `rednote_reject_mutation()`**（它属于 `platform`，被全项目共用；
   删了会让 WP11/WP15/WP16/WP17/WP18/WP20/WP22/WP23/WP24/WP25
   的保护一起失效）。因此第 2 步只能用 `ddl.consistency_items()`
   （**本包非空，3 项**），**不得**用 `all_trigger_ddl()`；
3. **`from sqlalchemy.dialects import postgresql` 已补** —— 本迁移用了
   `postgresql.JSONB`（`content_items.generation_request`、
   `content_versions.tags` / `bindings` / `payload`、
   `title_candidates.scores` / `keywords`）共 6 列。
   autogenerate 用了它却**不会自动加这行 import**，结果是迁移文件语法
   正确、diff 看着正常，**执行时 `NameError`**。
   本坑在本项目已连续触发 9 次（登记册 G2 / ISS-02）。

### ★ 取值域字面量**刻意不从 `types.py` import**（与 WP23 的取向相反）

迁移是**历史快照**，不是当前代码的视图：

| 取法 | 改取值域（如 `CONTENT_STATES` 加一类）之后 |
|---|---|
| import 委托 | 老迁移**自动跟着变** ⇒ 新库建出 `IN (…新值…)`，而生产库上这条迁移**早已执行**、CHECK 仍是老的 ⇒ **新库能过、老库过不了**，且没有任何判据会报红 |
| 字面量（本迁移） | `tools/quality/check_migration_model_parity.py` 在**两个空库**上分别跑 `alembic upgrade head` 与 `create_all` ⇒ 两侧 CHECK 文本不等 ⇒ **判据报红**，逼人写新迁移 |

**字面量让漂移可见，委托让漂移静默**。因此本文件里出现的每一个取值域
都写成**出处可查的**字面量，并在紧邻注释里点名它的单一来源
（`modules/content/types.py` 的哪个常量）。WP25（`daea2b27212c`）是同一取向，
本迁移跟随。

### 触发器名与索引名**反过来必须取自模块**（不手写）

触发器 DDL 与名字调 `ddl.all_trigger_ddl()`（**7 项**），与验证脚本、
测试、证据脚本**同源** —— 手写一遍必然漂移，而漂移的后果是"找回对象"
的检查**假失败或假通过**。

索引名在 `upgrade`/`downgrade` 里以字面量出现（`op.create_index` /
`op.drop_index` 的字符串参数，迁移期无法从模块取），与
`ddl.index_names()` 的**对账不在本文件里做** —— 由收口期的对账脚本在
**真库**上比对 `pg_indexes`（见 §五）。

---

## 二、★ 环外键：本迁移唯一需要"拆两步"的地方

本包有**两个环**，涉及 **5 条**外键：

```
①  content_items.current_version_id  ──▶  content_versions.id
    content_versions.content_item_id  ──▶  content_items.id

②  content_versions.generation_context_id  ──▶  generation_contexts.id
    generation_contexts.content_version_id  ──▶  content_versions.id

③  content_versions.adopted_title_candidate_id  ──▶  title_candidates.id
    title_candidates.content_version_id         ──▶  content_versions.id
```

### 处置规则（一句话）："被引用的表还没建 ⇒ 后置补；已经建 ⇒ 表内写"

⚠️ **判断依据只有一条：`CREATE TABLE` 的外键引用是建表期检查的**
（`deferrable` / `initially` 都**救不了**这一条 —— 它们管的是**行级**
检查时机，不是**表存在性**）。因此：

| # | FK | 处置 | 依据 |
|---|---|---|---|
| 1 | `fk_content_items_current_version_id_content_versions` | **后置**（第 6 步） | 建 `content_items` 时 `content_versions` 还不存在。**先例：WP23 的 `prompt_templates.active_version_id`**，逐字同款 |
| 2 | `fk_content_versions_content_item_id_content_items` | 表内 | `content_items` 第 1 步已建 |
| 3 | `fk_content_versions_generation_context_id_generation_contexts` | **后置**（第 6 步） | ⚠️ **环②的另一侧**：`generation_contexts` 虽在第 2 步建出，但它是**表存在**了，而 `content_versions` 此刻尚未建 —— 不对，本方向是 CV 引 GC，GC 第 2 步已存在 ⇒ **表内可写**。**但**GC 反向引 CV 的那条必须后置，见 #5 |
| 4 | `fk_generation_contexts_content_version_id_content_versions` | **后置**（第 6 步） | 建 `generation_contexts`（第 2 步）时 `content_versions` 还不存在 |
| 5 | `fk_content_versions_adopted_title_candidate_id_title_candidates` | **后置**（第 6 步） | 环③：`title_candidates.content_version_id` 引 CV，而 CV 的 `adopted_title_candidate_id` 引 TC —— **两者不可能都表内**（先建谁，另一个就缺表）。故选 **CV 先建、TC 后建**，则 CV 侧的这一条后置 |
| 6 | `fk_title_candidates_content_version_id_content_versions` | 表内 | TC 建在 CV 之后（第 5 步），CV 已存在 |
| 7 | `fk_content_versions_blueprint_id_narrative_blueprints` | 表内 | `narrative_blueprints` 第 3 步已建 |
| 8 | `fk_content_versions_id_version_nodes` | 表内 | `version_nodes` 是 WP11 的既存表 |

**⇒ 后置补 3 条**（#1 / #4 / #5），其余 5 条表内写。

### ★ 建表顺序（与 `models.OWN_TABLES` 的声明顺序**不同**，这是刻意的）

```
content_items → generation_contexts → narrative_blueprints
→ content_versions → title_candidates → 后置 3 条环 FK
→ 17 条索引 → 7 个触发器
```

`models.OWN_TABLES` 的顺序是**模型阅读顺序**（身份 → 版本 → 蓝图 →
候选 → 上下文）；本迁移的顺序是**FK 可满足顺序**。两者由本注释对账，
不由脚本对账（脚本对账的是**内容**，不是顺序 —— 顺序错了会 `UndefinedTable`
当场炸，判据是 PG 自己）。

### ★ 四条 `DEFERRABLE INITIALLY DEFERRED` FK（**逐条点名**）

`models.py` docstring 的第五条把"哪一条延迟"论证过；迁移侧必须**逐条
写出 `deferrable=True, initially="DEFERRED"`** —— 因为**范本注释不可信**
（本包实测：`briefs` 模块的两条 FK 中，注释声称两条都延迟，而
`pg_constraint` 实测 `fk_brief_drafts_brief_id_briefs` 是
`deferrable=false`）。本包**从 ORM 元数据逐条派生**，实测结果如下：

| # | FK | 为什么必须延迟 |
|---|---|---|
| 1 | `fk_content_items_current_version_id_content_versions` | 环①的一半：身份行与首版行同事务写入，顺序不可猜 |
| 2 | `fk_content_versions_id_version_nodes` | 注册表与领域表同事务写入，顺序由 WP11 的取号重试决定（与 WP18/WP20/WP22/WP23 同款） |
| 3 | `fk_content_versions_generation_context_id_generation_contexts` | 环②的一半：上下文行与本版行同事务写入 |
| 4 | `fk_generation_contexts_content_version_id_content_versions` | 环②的另一半 |

**恰好 4 条**（`test_content_wp26.py` 直接查 `pg_constraint` 断言
`condeferrable = true` 的行数与其名字集合）。

⚠️ **`fk_content_versions_content_item_id_content_items` 刻意
`deferrable=false`**（`models.py:460` 的 `⛔` 注释）：本列非空，且
"版本必须属于已存在的内容"没有任何合法流程需要"事后才补身份"。
延迟它只会额外放行**孤儿版本行** —— 那是削弱防线。
`test_content_wp26.py` 有一条**负向**用例钉住这一点。

⚠️ **`fk_title_candidates_content_version_id_content_versions` 刻意
`deferrable=false`**：`models.py` 里就没有给它 `deferrable=True`。
"候选属于已存在的一版"是硬前提，没有合法流程需要事后补。
**它虽然参与环③，但不靠延迟解环** —— 环③靠的是**建表顺序**
（CV 先、TC 后），不是延迟。把这两件事分清很重要：延迟解的是
"**行**什么时候插入"，顺序解的是"**表**什么时候存在"。

---

## 三、7 个触发器（DDL 不写在本文件里）

触发器 DDL 来自 `modules/content/ddl.py` 的 `all_trigger_ddl()` ——
与测试完全同源。**7 项**，分两组：

| # | 触发器 | 挂在哪张表 | 类型 |
|---|---|---|---|
| 1 | `trg_content_versions_immutable` | `content_versions` | BEFORE UPDATE/DELETE（复用共用函数 `rednote_reject_mutation`） |
| 2 | `trg_narrative_blueprints_immutable` | `narrative_blueprints` | 同上 |
| 3 | `trg_title_candidates_immutable` | `title_candidates` | 同上 |
| 4 | `trg_generation_contexts_immutable` | `generation_contexts` | 同上 |
| 5 | `trg_content_versions_node_consistency` | `content_versions` | CONSTRAINT TRIGGER（OP-88 `CONTENT` 正向） |
| 6 | `trg_version_nodes_content_paired` | `version_nodes` | CONSTRAINT TRIGGER（OP-88 `CONTENT` 反向） |
| 7 | `trg_generation_contexts_content_consistency` | `generation_contexts` | CONSTRAINT TRIGGER（上下文三 id 与内容一致） |

**为什么 4 张不可变表都要挂**：它们是版本快照 / 结构快照 / 候选快照 /
上下文快照 —— 改了任何一张，"上一版长什么样"就永久丢失，而
"仅标题重做"的核心价值恰恰是**能对比新旧标题**。

**为什么 `content_items` 刻意不挂**：它是身份行，承载会前进的
`current_version_id` 投影与 `lock_version`（ETag 载体），必须可变。
与 WP24 的 `plan_drafts`（工作区，可变）同一取舍。

### ★ `trg_version_nodes_content_paired` 挂在本包之外的 `version_nodes` 上

这是**有意**的（`ddl.py` docstring 第二节）：OP-88 的"禁止孤立节点"
要求**两个方向**都判 —— `BRIEF` / `KNOWLEDGE` / `BASELINE` / `PROMPT`
已由 WP18/WP20/WP22/WP23 闭合，**本包闭合第五半 `CONTENT`**。
本包不修改 `versions` 模块的任何代码或表结构，只是在共用表上**新增
一个触发器对象**，且被 `WHEN (NEW.entity_type = 'CONTENT')` 收窄到
本包负责的实体类型 ⇒ 与另四个 `*_paired` 触发器在同一张表上**并存、
互不影响**。

⚠️ **反向判据收窄**（与其余四个包**同一处理**）：只对"真实存在的内容身份"
强制 —— `content_items` 里有 `id = entity_id` 的行时才要求配对。
`version_nodes` 是**跨包共用**的注册表，`ENTITY_TYPE_CONTENT` 是它
`ck_version_nodes_entity_type_valid` 允收的 8 类实体类型之一，
**不是本包专有**；WP11 的通用用例（`tests/test_versions_wp11.py`）
拿它当代表性样本造"无领域行的节点"。

> ⚠️ **本段原有"刻意不收窄"的论证已被推翻**（2026-10-06，DB 层实测）：
> 原论证据"`ENTITY_TYPE_CONTENT` **业务代码**零消费"推出"不收窄不会误拦
> **任何既有路径**"。错在推论跨度：作者其实看见了 WP11 的消费
> （`docs/m3/WP26-…说明.md:104` 如实写了"全仓仅 7 处引用，除定义外
> 全部是 WP11 自己的测试"），但把它读成"只是测试、不算业务代码"，
> 于是"业务代码零消费"被偷换成"无既有消费方"。而 WP18/WP20 收窄的
> 理由从来不是"怕拦业务代码"，正是"**WP11 夹具会造无领域行节点**"。
> 不收窄的实测后果是**破坏 WP11 既有 2 例**
> （`test_cycle_detection_terminates_on_cyclic_graph` /
> `test_promote_draft_keeps_entity_type_and_id`），因果已用
> `DISABLE/ENABLE TRIGGER` 钉死。详见 `ddl.py` docstring 二的修正记录。
>
> **"收窄会让判据变成恒真式"的顾虑不成立**：本包写入顺序是
> 先落 `content_items` 身份行、再 `register_version`（造节点，
> `service.py:840-847`），故生产路径上守卫永不误放。判据真正要拦的
> 「内容存在但缺配对版本行」（静默分叉）**完整保留**，且另有对照判据锁定。

⚠️ **正向判据实测被 FK 遮蔽**（2026-10-06 私有库 `rednote_wp26` 实测）：
反例「版本行没有对应节点」报 **`23503`（FK）**，不是触发器的 `23514` ——
`content_versions.id → version_nodes.id` 的延迟 FK 与 `CONSTRAINT TRIGGER`
都是 `DEFERRABLE INITIALLY DEFERRED`，在同一结算点被检查，**FK 先出手**。
这不构成缺陷（判据 1 的价值本就是"同款错误消息"），但证据脚本对这条
**只能断言 `23503`** —— 谎称是触发器拦下的就是"声称自己有防线"（ISS-95）。
判据 2/3 **不受遮蔽**（管的是 FK 管不住的方向：类型对不对、指向谁），
实测都报 `23514`。

---

## 四、`downgrade` 的顺序与数据损失

五步（**不可换序**）：

1. 删 **7 个触发器**（用 `all_trigger_ddl()` 的 `_drop_trigger`）；
2. 删**本模块自己的 3 个函数**（用 `consistency_items()` 的 `_drop_function`）
   —— **不得**用 `all_trigger_ddl()`：它的 `drop_function` 还包含
   4 项指向**跨包共用**的 `rednote_reject_mutation()`，删了会撞依赖错误
   并让另外 8 个包的保护一起失效（ISS-79 的教训）；
3. 删 **17 条索引**（与 `upgrade()` 第 7 步**严格反序**）；
4. 删 **3 条后置环 FK**（必须先于删表：`content_items` 引用
   `content_versions`，不先删 FK 会让 `DROP TABLE content_versions` 被拒）；
5. 反序删 5 张表。

⚠️ **本 `downgrade` 有真实且严重的数据损失**：`content_versions` 是内容
版本快照（含段落级证据绑定 `bindings`），`generation_contexts` 是
**OP-160 的唯一答案来源**。回退前须导出全量。

⚠️ **`version_nodes` 不在删除集合里**（WP11 的资产）：
`content_versions.id → version_nodes.id` 这条 FK 属于**子表**
`content_versions`，随它删除时一并消失；`version_nodes` 的表结构与行
**不受影响**。

## 五、本迁移与 `ddl.index_names()` 的对账（不在本文件里做）

`upgrade()` 建 **17 条索引**（`ddl.index_names()` 返回的正是这 17 个名字，
从 `models.__table__.indexes` 派生，铁律 14）。
**两处相等这条不由本文件断言** —— 由收口期的对账脚本在**真库**上比对
`pg_indexes`，理由：本文件里的索引是 `op.create_index` 调用的字面量，
在 import 期看不到（只有跑过才知道建没建成）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "1c1c814b248e"
down_revision: str | Sequence[str] | None = "daea2b27212c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """正向迁移。

    **建表顺序见模块 docstring §二** —— 与 `models.OWN_TABLES` 不同：

        content_items → generation_contexts → narrative_blueprints
        → content_versions → title_candidates → 后置 3 条环 FK
        → 17 条索引 → 7 个触发器

    **唯一约束**：`CREATE TABLE` 的外键引用是**建表期**检查的，
    `deferrable` / `initially` 都救不了"被引用的表还没建"。
    因此凡"引用后建表"的 FK 一律后置（第 6 步补）。
    """
    # ### commands auto generated by Alembic - please adjust! ###

    # ==================================================================
    # 第 1 张：content_items（2 条表内 FK；环①的一半后置）
    # ==================================================================
    op.create_table(
        "content_items",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        # ⚠️ 环①的一半：这里**只写列，不写 FK**（见本函数第 6 步）。
        #    此刻 `content_versions` 还不存在，表内写会 `UndefinedTable`。
        sa.Column("current_version_id", sa.UUID(), nullable=True),
        # `current_version_id` 的**冗余投影**：有它列表页不必 JOIN。
        # **没有版本时为 0 而不是 NULL** —— NULL 参与比较是 UNKNOWN，
        # 会让"只改过标题的那些"这类筛选**静默返回空集**。
        sa.Column(
            "current_version_no",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        # 取值域 `('DRAFT', 'FAILED', 'GENERATED', 'STRUCTURED')` 的单一来源
        # = `modules/content/types.py` 的 `CONTENT_STATES` /
        # `CONTENT_STATE_LIST_SQL`。（字面量而不 import，理由见 docstring §一。）
        sa.Column(
            "state",
            sa.String(length=20),
            server_default=sa.text("'DRAFT'"),
            nullable=False,
        ),
        # 当前版本的标题投影。**可空**：还没生成文案时确实没有标题，
        # 而 `''` 会与"标题为空"这个非法状态混在一起（后者被 CHECK 拒）。
        sa.Column("title", sa.String(length=200), nullable=True),
        # 一次生成 = 一行的**幂等域**（R5）：网关侧的幂等由
        # `CapabilityRequest.idempotency_key` 保证，这一列让**预占**
        # （发生在派发之前）也能对得上。
        sa.Column("last_idempotency_key", sa.String(length=160), nullable=True),
        # ★ 第 4 处接缝缺口的落点：这一轮为这条内容请求了什么
        #   （`types.GenerationRequest` 的落库形状）。
        #   处置借用 WP18 的同款先例（`brief_drafts.enrichment_request`）：
        #   在**自己模块的表**上加一次性请求列，不碰 `jobs`（WP13 的资产）。
        sa.Column(
            "generation_request",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
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
        sa.Column(
            "lock_version",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('DRAFT', 'FAILED', 'GENERATED', 'STRUCTURED')",
            name=op.f("ck_content_items_state_valid"),
        ),
        sa.CheckConstraint(
            "current_version_no >= 0",
            name=op.f("ck_content_items_version_no_non_negative"),
        ),
        # 与 `content_versions.title_not_empty` 分工不同：身份行的 title
        # **可空**（还没生成），但一旦有值就不许是空白串。
        sa.CheckConstraint(
            "title IS NULL OR char_length(title) > 0",
            name=op.f("ck_content_items_title_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_content_items_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_content_items_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_items")),
    )

    # ==================================================================
    # 第 2 张：generation_contexts（8 条表内 FK；环②的一半后置）
    #   ⚠️ **提前建**（`models.OWN_TABLES` 里它在最后）：因为
    #      `content_versions.generation_context_id` 要在表内写 FK，
    #      而该引用是**建表期**检查的。
    # ==================================================================
    op.create_table(
        "generation_contexts",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("content_id", sa.UUID(), nullable=False),
        # ⚠️ 环②的一半：只写列，FK 在第 6 步补（`content_versions` 尚未建）。
        #    **可空**：`CONTENT_STRUCTURE` 调用发生时内容版本还没有
        #    （它产出的是蓝图，而蓝图是文案的输入）。若非空，结构调用就
        #    无法留下上下文行 ⇒ "这批内容的蓝图是哪版提示词生成的"永远查不到。
        sa.Column("content_version_id", sa.UUID(), nullable=True),
        # 取值域 `('CONTENT_COPY', 'CONTENT_STRUCTURE')` 的单一来源 =
        # `modules/content/types.py` 的 `GENERATION_CONTEXT_LIST_SQL`。
        sa.Column("task_kind", sa.String(length=40), nullable=False),
        # --- OP-160 的四个 id（本表就是 OP-160 的答案）----------------
        sa.Column("prompt_template_id", sa.UUID(), nullable=True),
        # **可空但"应当有"**：正常路径上必然有值（fail-closed 要求
        # 查不到激活版本就不生成）。可空是为容纳"激活版本后来被删" ——
        # 而 `ON DELETE RESTRICT` 让它删不掉，于是这个 NULL 只出现在
        # 历史数据导入场景。留空比塞一个假 UUID 好。
        sa.Column("prompt_version_id", sa.UUID(), nullable=True),
        sa.Column("baseline_id", sa.UUID(), nullable=True),
        sa.Column("model_profile_id", sa.UUID(), nullable=True),
        sa.Column("provider_call_id", sa.UUID(), nullable=True),
        # 幂等键（与 `CapabilityRequest.idempotency_key` 同值）。
        # **存下来的理由**：重试时要用**同一个**键去问网关"这个键跑过没有"，
        # 而键由内容版本号 + task_kind 派生 —— 派生规则若只在服务层，
        # 重启后（版本号已变）就派生不出同一个键，幂等失效且不报错。
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "task_kind IN ('CONTENT_COPY', 'CONTENT_STRUCTURE')",
            name=op.f("ck_generation_contexts_task_kind_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_generation_contexts_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["content_id"],
            ["content_items.id"],
            name=op.f("fk_generation_contexts_content_id_content_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_template_id"],
            ["prompt_templates.id"],
            name=op.f("fk_generation_contexts_prompt_template_id_prompt_templates"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version_id"],
            ["prompt_versions.id"],
            name=op.f("fk_generation_contexts_prompt_version_id_prompt_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_id"],
            ["production_baselines.id"],
            name=op.f("fk_generation_contexts_baseline_id_production_baselines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_profile_id"],
            ["model_profiles.id"],
            name=op.f("fk_generation_contexts_model_profile_id_model_profiles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_call_id"],
            ["provider_calls.id"],
            name=op.f("fk_generation_contexts_provider_call_id_provider_calls"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_generation_contexts_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_contexts")),
    )

    # ==================================================================
    # 第 3 张：narrative_blueprints（2 条 FK + 18 条 CHECK）
    #   ⚠️ **提前建**：`content_versions.blueprint_id` 要在表内写 FK。
    # ==================================================================
    op.create_table(
        "narrative_blueprints",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("content_item_id", sa.UUID(), nullable=False),
        # --- 9 个字段（顺序与说明文档 §2.4 第 2 项一致）----------------
        sa.Column("opening_function", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("decision_process", sa.Text(), nullable=False),
        sa.Column("usage_scene", sa.Text(), nullable=False),
        sa.Column("evidence_beats", sa.Text(), nullable=False),
        # `limitation`：**本条最要紧的必填项** —— `plan-detail.html` 的硬约束
        # "含待补信息的行，生成时可退化为产品说明结构，但不允许把空白补成
        # 使用经历"。这一栏就是那句话的落点（R10）。
        sa.Column("limitation", sa.Text(), nullable=False),
        # 取值域 `('CONSERVATIVE', 'DIRECT', 'HEDGED')` 的单一来源 =
        # `modules/content/types.py` 的 `CONCLUSION_STRENGTH_LIST_SQL`。
        sa.Column("conclusion_strength", sa.String(length=20), nullable=False),
        sa.Column("interaction_question", sa.Text(), nullable=False),
        sa.Column("paragraph_plan", sa.Text(), nullable=False),
        sa.Column(
            "schema_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        # ---- 8 个文本字段各 2 条：非空 + 长度上界 ----
        # 上界字面量 4000 = `types.MAX_BLUEPRINT_FIELD_CHARS`。
        #
        # "非空"**落 DDL 而不只在服务层判**：蓝图少一个字段是**结构性缺陷**
        # （下游 `schemas.py` 的 `REASON_FIELD_MISSING` 与正文分段都要按
        # 9 个字段齐全来算），不是"这段话写得好不好"那种语义判断。
        # DDL 判得动的就应当落在这里 —— 服务层可能是**旁路**（数据导入脚本、
        # 将来的迁移），而 CHECK 是**绕不过去的**。
        #
        # ⚠️ CHECK **判不了**这 8 个字段的取值是不是"9 个字段"之一 ——
        #    那是 `schemas.validate_blueprint` 的职责（拒多余字段）。
        #    DDL 管"列自身合法"，纯函数管"整份 payload 的形状"。
        sa.CheckConstraint(
            "char_length(opening_function) > 0",
            name=op.f("ck_narrative_blueprints_opening_function_not_empty"),
        ),
        sa.CheckConstraint(
            "char_length(opening_function) <= 4000",
            name=op.f("ck_narrative_blueprints_opening_function_length"),
        ),
        sa.CheckConstraint(
            "char_length(context) > 0",
            name=op.f("ck_narrative_blueprints_context_not_empty"),
        ),
        sa.CheckConstraint(
            "char_length(context) <= 4000",
            name=op.f("ck_narrative_blueprints_context_length"),
        ),
        sa.CheckConstraint(
            "char_length(decision_process) > 0",
            name=op.f("ck_narrative_blueprints_decision_process_not_empty"),
        ),
        sa.CheckConstraint(
            "char_length(decision_process) <= 4000",
            name=op.f("ck_narrative_blueprints_decision_process_length"),
        ),
        sa.CheckConstraint(
            "char_length(usage_scene) > 0",
            name=op.f("ck_narrative_blueprints_usage_scene_not_empty"),
        ),
        sa.CheckConstraint(
            "char_length(usage_scene) <= 4000",
            name=op.f("ck_narrative_blueprints_usage_scene_length"),
        ),
        sa.CheckConstraint(
            "char_length(evidence_beats) > 0",
            name=op.f("ck_narrative_blueprints_evidence_beats_not_empty"),
        ),
        sa.CheckConstraint(
            "char_length(evidence_beats) <= 4000",
            name=op.f("ck_narrative_blueprints_evidence_beats_length"),
        ),
        sa.CheckConstraint(
            "char_length(limitation) > 0",
            name=op.f("ck_narrative_blueprints_limitation_not_empty"),
        ),
        sa.CheckConstraint(
            "char_length(limitation) <= 4000",
            name=op.f("ck_narrative_blueprints_limitation_length"),
        ),
        sa.CheckConstraint(
            "char_length(interaction_question) > 0",
            name=op.f("ck_narrative_blueprints_interaction_question_not_empty"),
        ),
        sa.CheckConstraint(
            "char_length(interaction_question) <= 4000",
            name=op.f("ck_narrative_blueprints_interaction_question_length"),
        ),
        sa.CheckConstraint(
            "char_length(paragraph_plan) > 0",
            name=op.f("ck_narrative_blueprints_paragraph_plan_not_empty"),
        ),
        sa.CheckConstraint(
            "char_length(paragraph_plan) <= 4000",
            name=op.f("ck_narrative_blueprints_paragraph_plan_length"),
        ),
        # ---- 受控枚举 1 条（只落取值 CHECK，不落非空/长度）----
        sa.CheckConstraint(
            "conclusion_strength IN ('CONSERVATIVE', 'DIRECT', 'HEDGED')",
            name=op.f("ck_narrative_blueprints_conclusion_strength_valid"),
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name=op.f("ck_narrative_blueprints_schema_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["content_items.id"],
            name=op.f("fk_narrative_blueprints_content_item_id_content_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_narrative_blueprints_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_narrative_blueprints")),
    )

    # ==================================================================
    # 第 4 张：content_versions（4 条表内 FK + 9 条 CHECK；2 条环 FK 后置）
    #   ⚠️ **必须建在 title_candidates 之前**（环③）：若先建 TC，则
    #      本表的 `adopted_title_candidate_id` 无处可引。选"CV 先、TC 后"
    #      的代价是**本表这一条 FK 后置**，而 TC 那条可以表内写。
    #      两个方向选其一，依据是"哪一边的 FK 更少"（CV 侧本就已有一条
    #      `current_version_id` 要后置，合并到同一步更省）。
    # ==================================================================
    op.create_table(
        "content_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("content_item_id", sa.UUID(), nullable=False),
        # 本包内容的修订号（**与 `version_nodes.revision_no` 分开**）：
        # 前者是**业务**意义上的"第几版"，后者是**注册表**序号。
        # 两者当前同值但语义不同 —— 注册表将来若支持"同一版本挂多条
        # 修订记录"，注册表序号会前进而业务版本号不该动。
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("blueprint_id", sa.UUID(), nullable=True),
        # ⚠️ 环③的一半：只写列，FK 在第 6 步补（`title_candidates` 尚未建）。
        #    ★ "采纳"唯一的落点。**只有采纳端点产生的版本行会写这一列**，
        #      生成产生的版本行恒为 NULL。采纳**不是** UPDATE 已有版本行
        #      （那会撞不可变触发器），而是**追加一版**。
        sa.Column("adopted_title_candidate_id", sa.UUID(), nullable=True),
        # 文案三件。`body` 是纯文本（含换行），**不是 markdown**
        # （§1.5 砍掉清单）。
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # **段落级证据绑定**（R4 的存储落点）。形状见 `types.py` 的四键常量。
        # 用 JSONB 而不是子表：绑定**只在整版一起读**，从不按 `claim_id`
        # 反查；子表会引入"绑定数与正文段数一致"这条跨表不变量，而它在
        # 服务层已由 `schemas.validate_bindings` 逐段判过 —— 再落一条
        # 跨表约束是第二真值来源。（与 WP24 `plan_drafts.scene_items` 同取舍。）
        sa.Column(
            "bindings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("generation_context_id", sa.UUID(), nullable=True),
        # 新版本的**追加式**快照（供下游按 schema_version 演化）。
        # 三件产出在专属列里，但整个版本行作为一个版本节点注册到
        # `version_nodes`，其 `payload_hash` 由 `canonical_payload_hash` 算出
        # —— 摘要不能从列重算（列会演进），必须存下来。
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "schema_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "version_no >= 1",
            name=op.f("ck_content_versions_version_no_positive"),
        ),
        sa.CheckConstraint(
            "char_length(title) > 0",
            name=op.f("ck_content_versions_title_not_empty"),
        ),
        sa.CheckConstraint(
            "char_length(title) <= 200",
            name=op.f("ck_content_versions_title_length"),
        ),
        sa.CheckConstraint(
            "char_length(body) > 0",
            name=op.f("ck_content_versions_body_not_empty"),
        ),
        # 上界 20000 = `types.MAX_BODY_CHARS`。
        sa.CheckConstraint(
            "char_length(body) <= 20000",
            name=op.f("ck_content_versions_body_length"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(tags) = 'array'",
            name=op.f("ck_content_versions_tags_is_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(bindings) = 'array'",
            name=op.f("ck_content_versions_bindings_is_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name=op.f("ck_content_versions_payload_is_object"),
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name=op.f("ck_content_versions_schema_version_positive"),
        ),
        # ★ 延迟 FK #2：注册表与领域表同事务插入，顺序由 WP11 的
        #   取号重试决定（与 WP18/WP20/WP22/WP23 同款）。
        sa.ForeignKeyConstraint(
            ["id"],
            ["version_nodes.id"],
            name=op.f("fk_content_versions_id_version_nodes"),
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        # ⛔ **刻意不延迟**（`models.py:460` 的 `⛔` 注释）：本列非空，
        #   且"版本必须属于已存在的内容"没有任何合法流程需要"事后才补
        #   身份"。延迟它只会额外放行**孤儿版本行** —— 那是削弱防线。
        #   `test_content_wp26.py` 有一条负向用例直接查 `pg_constraint`
        #   钉住 `condeferrable = false`。
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["content_items.id"],
            name=op.f("fk_content_versions_content_item_id_content_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["blueprint_id"],
            ["narrative_blueprints.id"],
            name=op.f("fk_content_versions_blueprint_id_narrative_blueprints"),
            ondelete="RESTRICT",
        ),
        # ★ 延迟 FK #3：上下文行与本版行同事务写入（环②的一半）。
        sa.ForeignKeyConstraint(
            ["generation_context_id"],
            ["generation_contexts.id"],
            name=op.f(
                "fk_content_versions_generation_context_id_generation_contexts"
            ),
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_content_versions_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_versions")),
    )

    # ==================================================================
    # 第 5 张：title_candidates（1 条表内 FK + 7 条 CHECK）
    #   ⚠️ **必须在 content_versions 之后**（环③的另一侧）。
    # ==================================================================
    op.create_table(
        "title_candidates",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # 指向 `content_versions`（已在上一步建出，故此处**表内写 FK**）
        # 而不是 `content_items`：候选是**一次生成**的产物，而一次生成 =
        # 一版。指向身份会让"这条内容的候选"混起所有历史版本 ——
        # 那时"这个候选是哪一版给的"就没有答案了。
        sa.Column("content_version_id", sa.UUID(), nullable=False),
        # 同一版本内的候选序号（0 基）。**有它才能"按输入顺序展示"** ——
        # 而输入顺序是模型给的优先级信号（小红书里第一个标题权重最高）。
        sa.Column("candidate_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        # 6 维评分。**JSONB 而不是 6 个列**：量纲由产品定（说明文档附录 A4），
        # 而具名列会隐含"6 个都是同一量纲"这个尚未确定的假设。
        # 代价（JSONB 内部无法落 CHECK）由 `schemas.py` 逐维判承担。
        sa.Column("scores", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "keywords",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("highlight", sa.String(length=300), nullable=True),
        # `risk_note`：这个标题有什么风险（夸大、与正文不符、蹭敏感热点）。
        # **可空**：多数候选没有风险可讲；而"必须有风险说明"会逼模型
        # **编一个不存在的风险**出来（与 `bindings.reason` 同一取向 ——
        # 允许的例外必须留痕，但不强迫留痕）。
        sa.Column("risk_note", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "candidate_index >= 0",
            name=op.f("ck_title_candidates_candidate_index_non_negative"),
        ),
        sa.CheckConstraint(
            "char_length(title) > 0",
            name=op.f("ck_title_candidates_title_not_empty"),
        ),
        sa.CheckConstraint(
            "char_length(title) <= 200",
            name=op.f("ck_title_candidates_title_length"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(scores) = 'object'",
            name=op.f("ck_title_candidates_scores_is_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(keywords) = 'array'",
            name=op.f("ck_title_candidates_keywords_is_array"),
        ),
        sa.CheckConstraint(
            "highlight IS NULL OR char_length(highlight) <= 300",
            name=op.f("ck_title_candidates_highlight_length"),
        ),
        sa.CheckConstraint(
            "risk_note IS NULL OR char_length(risk_note) <= 500",
            name=op.f("ck_title_candidates_risk_note_length"),
        ),
        # ⛔ **刻意不延迟**（见 docstring §二末段）：本列非空，
        #   "候选属于已存在的一版"没有合法流程需要事后补。
        #   环③由**建表顺序**解开，不靠延迟。
        sa.ForeignKeyConstraint(
            ["content_version_id"],
            ["content_versions.id"],
            name=op.f("fk_title_candidates_content_version_id_content_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_title_candidates")),
    )

    # ==================================================================
    # 第 6 步：后置补 **3 条**环 FK
    #
    #   ① fk_content_items_current_version_id_content_versions
    #   ② fk_generation_contexts_content_version_id_content_versions
    #   ③ fk_content_versions_adopted_title_candidate_id_title_candidates
    #
    #   **判据是"表存在性"而不是"行存在性"**：此刻 5 张表都已建出，
    #   因此这 3 条 FK 可以被立即校验（`validated`）。
    #   `DEFERRABLE INITIALLY DEFERRED` 管的是**行级插入顺序** ——
    #   身份行与首版行、上下文行与版本行都同事务写入，顺序不可猜。
    #
    #   先例：WP23 的 `prompt_templates.active_version_id`（`3cb74d02edc5`），
    #   逐字同款处置。
    # ==================================================================
    op.create_foreign_key(
        op.f("fk_content_items_current_version_id_content_versions"),
        "content_items",
        "content_versions",
        ["current_version_id"],
        ["id"],
        ondelete="RESTRICT",
        initially="DEFERRED",
        deferrable=True,
    )
    op.create_foreign_key(
        op.f("fk_generation_contexts_content_version_id_content_versions"),
        "generation_contexts",
        "content_versions",
        ["content_version_id"],
        ["id"],
        ondelete="RESTRICT",
        initially="DEFERRED",
        deferrable=True,
    )
    # ⚠️ 这一条**不延迟**（环③，同 `fk_title_candidates_content_version_id_*`
    #    的取向）：`adopted_title_candidate_id` 可空，且采纳流程是
    #    "先有候选行、再有采纳版本行"——两个表的行**不是同事务插的**
    #    （候选由生成产生，采纳是后来的另一次请求）。因此没有
    #    "顺序不可猜"的问题，延迟它只会放行"指向不存在候选的采纳"。
    op.create_foreign_key(
        op.f(
            "fk_content_versions_adopted_title_candidate_id_title_candidates"
        ),
        "content_versions",
        "title_candidates",
        ["adopted_title_candidate_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # ==================================================================
    # 第 7 步：17 条索引（`op.create_index`，与 `ddl.index_names()` 同集合）
    #
    # ⚠️ 其中 3 条是 `unique=True`（`uq_*`），但它们**不是**
    #    `UniqueConstraint` —— 在 `models.py` 里写成 `Index(..., unique=True)`，
    #    因此建法也是 `op.create_index`（不是表内 `sa.UniqueConstraint`）。
    #    写成约束会让 `check_migration_model_parity` 在两边看到不同的
    #    `pg_constraint` 内容。
    # ==================================================================

    # --- content_items（4 条）---
    # 列表页的排序与筛选（`GET /projects/{id}/contents`）。
    op.create_index(
        "ix_content_items_project_updated",
        "content_items",
        ["project_id", "updated_at"],
        unique=False,
    )
    # "这个项目还有哪些没生成完"（状态筛选）。
    op.create_index(
        "ix_content_items_project_state",
        "content_items",
        ["project_id", "state"],
        unique=False,
    )
    op.create_index(
        "ix_content_items_current_version",
        "content_items",
        ["current_version_id"],
        unique=False,
    )
    # 幂等域查询：按 (内容, 幂等键) 找"这次生成是不是已经跑过"。
    op.create_index(
        "ix_content_items_idempotency",
        "content_items",
        ["id", "last_idempotency_key"],
        unique=False,
    )

    # --- generation_contexts（6 条，含 1 条唯一）---
    # ★ OP-160 的**能力前提**：按提示词版本反查历史生成。
    #   没有这条索引，OP-160 的查询会退化成全表扫 —— 而它正是本包
    #   关闭 OP-160 的验收条件（见说明文档 §3.6）。
    op.create_index(
        "ix_generation_contexts_prompt_version",
        "generation_contexts",
        ["prompt_version_id"],
        unique=False,
    )
    # OP-160 的粗粒度问法："改了这个模板影响谁"。
    op.create_index(
        "ix_generation_contexts_prompt_template",
        "generation_contexts",
        ["prompt_template_id"],
        unique=False,
    )
    # "这批内容的生成为什么变差了" → 按基线归组。
    op.create_index(
        "ix_generation_contexts_baseline",
        "generation_contexts",
        ["baseline_id"],
        unique=False,
    )
    op.create_index(
        "ix_generation_contexts_model_profile",
        "generation_contexts",
        ["model_profile_id"],
        unique=False,
    )
    # 详情端点按内容取上下文；按 (内容, kind) 归组结构/文案两次调用。
    op.create_index(
        "ix_generation_contexts_content_kind",
        "generation_contexts",
        ["content_id", "task_kind"],
        unique=False,
    )
    # 幂等：同一个键在同一内容上只该有**一行**上下文。
    # 为什么不是全局唯一：不同内容可以有相同键前缀，而键里含内容版本号
    # —— 同内容同版本同 kind 才是"同一次生成"。
    op.create_index(
        "uq_generation_contexts_idempotency",
        "generation_contexts",
        ["content_id", "idempotency_key"],
        unique=True,
    )

    # --- narrative_blueprints（1 条）---
    # `plan-detail.html` 那一屏要"这 10 条的蓝图分别是什么"。
    op.create_index(
        "ix_narrative_blueprints_item_created",
        "narrative_blueprints",
        ["content_item_id", "created_at"],
        unique=False,
    )

    # --- title_candidates（1 条，唯一）---
    # 同一版本内候选序号唯一 —— 没有它，"第 2 个候选"有两个答案。
    op.create_index(
        "uq_title_candidates_version_index",
        "title_candidates",
        ["content_version_id", "candidate_index"],
        unique=True,
    )

    # --- content_versions（5 条，含 1 条唯一）---
    # **一内容一版本号**（业务版本号不允许重复）。这条同时是
    # "取号并发冲突"的捕获点：两个请求同时重做时，后到的撞这条
    # 唯一约束，服务层捕获后重新取号（与 WP11 `register_version` 同手法）。
    op.create_index(
        "uq_content_versions_item_version_no",
        "content_versions",
        ["content_item_id", "version_no"],
        unique=True,
    )
    # 版本历史列表（`GET /contents/{id}/versions`）。
    op.create_index(
        "ix_content_versions_item_created",
        "content_versions",
        ["content_item_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_content_versions_blueprint",
        "content_versions",
        ["blueprint_id"],
        unique=False,
    )
    op.create_index(
        "ix_content_versions_adopted_title",
        "content_versions",
        ["adopted_title_candidate_id"],
        unique=False,
    )
    op.create_index(
        "ix_content_versions_generation_context",
        "content_versions",
        ["generation_context_id"],
        unique=False,
    )

    # ==================================================================
    # 第 8 步：触发器 —— autogenerate **不生成触发器**，必须手工补。
    #   DDL 来源与测试、证据脚本完全同源（`modules/content/ddl.py` 的
    #   `all_trigger_ddl`），因此这里**不重写 SQL**，而是直接调用它。
    #
    #   **7 个**（4 条不可变保护 + 3 条跨表一致性），其中
    #   `trg_version_nodes_content_paired` 挂在**共用注册表**
    #   `version_nodes` 上（OP-88 第五半的反向判据，见 docstring §三）。
    #
    #   ⚠️ 不可变保护那 4 项的 `function_ddl` 是
    #   `CREATE OR REPLACE FUNCTION rednote_reject_mutation()` ——
    #   它是**跨包共用**的（WP11 建），这里重放一遍是**幂等**且**必要**的：
    #   `all_trigger_ddl()` 的契约就是"函数 DDL + 触发器 DDL"成对给出，
    #   跳过它会破坏与测试的同源性。（`downgrade` 则**只删本模块的
    #   3 个函数**，见 docstring §四。）
    #
    #   ⚠️ 顺序必须是"先全部建表、再建触发器"：`CREATE CONSTRAINT TRIGGER`
    #   在 PG 里**不做依赖检查**（plpgsql 函数体在首次调用时才解析），
    #   但把触发器放在建表循环里会让读日志的人以为顺序有约束。
    # ==================================================================
    from rednote.modules.content import ddl as content_ddl

    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in content_ddl.all_trigger_ddl():
        op.execute(_function_ddl)
        op.execute(_trigger_ddl)

    # ------------------------------------------------------------------
    # ★ **本迁移没有数据回填** —— 这是决定，不是遗漏。
    #   `narrative_blueprints` / `title_candidates` / `generation_contexts`
    #   的行**只能由生成流程产生**（要真调模型），任何"初始数据"都是假的；
    #   而假数据会污染 OP-160 的答案（"哪个提示词版本被消费过"）。
    #
    #   因此这里**不出现 `Session(bind=op.get_bind())`**（对照 WP24 的第五节）。
    # ------------------------------------------------------------------

    # ### end Alembic commands ###


def downgrade() -> None:
    """反向迁移。

    必须补全，不能留 `NotImplementedError` —— 那会让任何回滚尝试直接失败，
    与 WP47 的恢复演练（RPO/RTO）目标冲突。

    **只删本迁移自己建出来的对象。** 判据不是"它在哪张表上"，
    而是"**这个对象是不是我建的**"：

      · 7 个触发器（4 条不可变 + 3 条一致性）→ 我建的，删。
        **含挂在 `version_nodes` 上的 `trg_version_nodes_content_paired`** ——
        它虽在别人的表上，但是**本迁移建的对象**，不删会残留。
      · `rednote_reject_mutation()` → 别人建的共用函数，**不删**。
        删它会撞依赖错误，并让 WP11/WP15/WP16/WP17/WP18/WP20/WP22/WP23/
        WP24/WP25 的保护**一起失效**（WP16 差点踩、
        WP18/WP20/WP22/WP23/WP24/WP25 明确防了）。
        因此第 2 步用 `ddl.consistency_items()`（本包**非空，3 项**），
        **不得**改用 `all_trigger_ddl()` —— 它的 `drop_function` 指向共用函数。
      · 3 个一致性函数 → 我建的，删。
      · 17 条索引 → 我建的，删（**先删索引再删表**）。
      · 3 条后置环 FK → 我建的，删（**必须先于删表**，见第 4 步）。
      · 5 张表 → 我建的，删（**反序**）。
      · 数据行 → 随表一并删除（见下）。

    ⚠️ **本 `downgrade` 有真实且严重的数据损失**：`content_versions` 是
    内容版本快照（含段落级证据绑定 `bindings`），`generation_contexts`
    是 **OP-160 的唯一答案来源**（"哪个提示词版本被哪些生成消费过"）。
    回退前须导出全量。
    """
    # ### commands auto generated by Alembic - please adjust! ###
    from rednote.modules.content import ddl as content_ddl

    # ---- 1. 先删触发器（触发器 DROP 必须先于函数 DROP）----
    # 顺序不能反：函数仍被触发器引用时，PG 会拒绝删除
    # （"cannot drop function ... because other objects depend on it"）。
    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in content_ddl.all_trigger_ddl():
        op.execute(_drop_trigger)

    # ---- 2. 只删本模块自己的 3 个函数 ----
    # **不得**用 `all_trigger_ddl()` —— 它的 `drop_function` 还包含
    # 4 项指向跨包共用的 `rednote_reject_mutation()`（见 docstring §四）。
    # `consistency_items()` 返回的 3 项，其 `_drop_function` 分别指向
    # `rednote_check_content_version_consistency` /
    # `rednote_check_content_node_paired` /
    # `rednote_check_generation_context_consistency` —— 都是本模块建的。
    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in content_ddl.consistency_items():
        op.execute(_drop_function)

    # ---- 3. 删 17 条索引（与第 7 步严格反序）----
    # ⚠️ 索引随表删除时也会一并消失；**显式写出来**是为了让
    # "我删了这些防线"在迁移日志里**可见**（与 WP22/WP24/WP25 同一做法）。

    # -- content_versions（5 条）--
    op.drop_index(
        "ix_content_versions_generation_context", table_name="content_versions"
    )
    op.drop_index(
        "ix_content_versions_adopted_title", table_name="content_versions"
    )
    op.drop_index("ix_content_versions_blueprint", table_name="content_versions")
    op.drop_index(
        "ix_content_versions_item_created", table_name="content_versions"
    )
    op.drop_index(
        "uq_content_versions_item_version_no", table_name="content_versions"
    )

    # -- title_candidates（1 条）--
    op.drop_index(
        "uq_title_candidates_version_index", table_name="title_candidates"
    )

    # -- narrative_blueprints（1 条）--
    op.drop_index(
        "ix_narrative_blueprints_item_created", table_name="narrative_blueprints"
    )

    # -- generation_contexts（6 条）--
    op.drop_index(
        "uq_generation_contexts_idempotency", table_name="generation_contexts"
    )
    op.drop_index(
        "ix_generation_contexts_content_kind", table_name="generation_contexts"
    )
    op.drop_index(
        "ix_generation_contexts_model_profile", table_name="generation_contexts"
    )
    op.drop_index(
        "ix_generation_contexts_baseline", table_name="generation_contexts"
    )
    op.drop_index(
        "ix_generation_contexts_prompt_template", table_name="generation_contexts"
    )
    op.drop_index(
        "ix_generation_contexts_prompt_version", table_name="generation_contexts"
    )

    # -- content_items（4 条）--
    op.drop_index("ix_content_items_idempotency", table_name="content_items")
    op.drop_index(
        "ix_content_items_current_version", table_name="content_items"
    )
    op.drop_index("ix_content_items_project_state", table_name="content_items")
    op.drop_index("ix_content_items_project_updated", table_name="content_items")

    # ---- 4. 删 3 条后置环 FK（**必须先于删表**）----
    # 不先删会怎样：`content_items.current_version_id` 引用
    # `content_versions`，`DROP TABLE content_versions` 会被 PG 以
    # "cannot drop table content_versions because other objects depend on it"
    # 拒绝。删 FK 的顺序与第 6 步**严格反序**。
    op.drop_constraint(
        op.f("fk_content_versions_adopted_title_candidate_id_title_candidates"),
        "content_versions",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_generation_contexts_content_version_id_content_versions"),
        "generation_contexts",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_content_items_current_version_id_content_versions"),
        "content_items",
        type_="foreignkey",
    )

    # ---- 5. 最后删表（**反序**：先删引用别人的，后删被引用的）----
    #
    # 反序的依据（建表正序见 `upgrade()` 的注释）：
    #   title_candidates → content_versions → narrative_blueprints
    #   → generation_contexts → content_items
    #
    # `title_candidates` 必须先删（它引用 `content_versions`，且
    # `content_versions.adopted_title_candidate_id` 的 FK 已在第 4 步删掉）。
    #
    # ⚠️ `version_nodes` **不在**删除集合里（WP11 的资产）：
    #    `content_versions.id → version_nodes.id` 这条 FK 属于**子表**
    #    `content_versions`，随它删除时一并消失；`version_nodes` 的
    #    表结构与行**不受影响**。
    op.drop_table("title_candidates")
    op.drop_table("content_versions")
    op.drop_table("narrative_blueprints")
    op.drop_table("generation_contexts")
    op.drop_table("content_items")

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
    # 本迁移只碰 content_items / content_versions / narrative_blueprints /
    # title_candidates / generation_contexts 五张表 + 7 个触发器
    # + 17 条索引 + 3 条后置环 FK。
    # ------------------------------------------------------------------
    # ### end Alembic commands ###