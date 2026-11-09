"""WP27 阶段 4：静态图适配与产品参考 —— 5 张表 + 8 个触发器 + 2 条环 FK 后置补建。

revision: df6bb2f41f84
down_revision: e8f07ba8f2bb

**本迁移只做一件事：把 `modules/media` 的 5 张表落库。**
WP27 阶段 1~3（DDL / 模型 / 仓储 / 契约 + 3 条新判据）已由 `4e10d21` 交付；
阶段 4 的 service / api / 装配因**等待真实产品参考素材与候选模型**而显式挂起 ——
但"迁移落库"与取材无关，可以且应当先做（理由见 §一）。

**本迁移包含 5 张表**（`modules/media/models.py` 的 `OWN_TABLES` 同值）：

| 表 | 性质 | 说明 |
|---|---|---|
| `media_items` | 可变（`lock_version`） | 媒体位的**身份**行；承载会前进的 `current_version_id` 投影 |
| `media_versions` | **不可变** | 媒体的一版：图 + 提示词 + 参考绑定 + 生成上下文 |
| `reference_assets` | **不可变** | 产品参考素材（型号 / 颜色 / 部位） |
| `media_renditions` | **不可变** | 一版的转码产物（预览 / 交付） |
| `media_generation_contexts` | **不可变** | 一次静态图生成的上下文快照（OP-160 的答案来源） |

---

## 一、★ 本迁移存在的主要理由：两条判据在 HEAD 上是红的

`4e10d21` 只交付了模型，**没有配套迁移**。于是 HEAD 上：

| 判据 | 红在哪 | 机制 |
|---|---|---|
| `tools/quality/check_migration_model_parity.py`（**CI 门 3**） | 在**跑任何库之前**的"三处清单对账"就 FAIL | 磁盘 19 个模块 ⟷ `migrations/env.py` 的 import；差集 `['media']` |
| `services/backend/tests/test_schema_parity.py` | `missing` 断言 | 模型声明的 **23 条** media FK 在库里**不存在** |

⇒ 本迁移 + `migrations/env.py` 补 `media import` 两条，**一次消掉两条红**。

⚠️ **注意这两条红各自覆盖不同的盲区，缺一不可**：
`alembic check` 在本仓实测 `EXIT=0`（因为它是"模型 ⟷ 迁移脚本"的比对，
而 `media` 模型**根本没进 `env.py`**，双方都看不见它 —— 见 ISS-110）。
清单对账堵的正是这个盲区。

## 二、G2 铁律的执行记录（本文件从 autogenerate 产物手工改全）

`alembic revision --autogenerate` 产出了骨架（253 行），**逐条改过**：

1. **删掉 autogenerate 提议的 M0 探针表 drop** —— ✅ 本迁移的
   `op.drop_table` / `op.drop_index`（**27 处**）**全部**指向本迁移自己建的
   5 张表及其索引，**未出现** `smoke_meta` / `wp05_vec_probe`
   （复核方式见 §六第 4 条）。故 G2 第一条**无对象可删**，
   写在这里是为了让"三件事"的清单保持完整、便于评审确认「不是漏了」；
2. **`downgrade` 不重建、不删除本包不拥有的对象** —— 尤其**不删
   `rednote_reject_mutation()`**（它属于 `platform`，被全项目共用；
   删了会让 WP11/WP15/WP16/WP17/WP18/WP20/WP22/WP23/WP24/WP25/WP26
   的保护**一起失效**）。因此第 2 步只能用 `ddl.consistency_items()`
   （本包**非空，4 项**），**不得**用 `all_trigger_ddl()`；
3. **`from sqlalchemy.dialects import postgresql` 必须补** ——
   autogenerate 用了 `postgresql.JSONB` 却**不会自动加这行 import**：
   `media_items.generation_request`、`media_versions.reference_bindings` /
   `payload` 共 **3 列**。缺它的表现是**迁移文件语法正确、diff 看着正常、
   执行时 `NameError`**。本坑在本项目已连续触发 9 次（登记册 G2 / ISS-02）
   —— 本次是本仓**第 10 次**实证（autogenerate 产物确实没这行）。

**第 4、5 条（拆环 / 触发器）是 autogenerate 完全无能为力的两处**，见 §三、§四。

### ★ 补记：2 条命名约定"放不下"的 FK（PostgreSQL 63 字节上限）

本迁移有 **2 条 FK** 的**约定产物超过 63 字节**，模型侧与迁移侧都必须**显式短名**：

| 约定产物（未截断） | 字节 | 库里实际（被截断） | 显式短名 | 字节 |
|---|---|---|---|---|
| `fk_media_versions_generation_context_id_media_generation_contexts` | **65** | `..._media_generatio_4e11`（60） | `fk_media_versions_generation_context_media_generation_contexts` | 62 |
| `fk_media_generation_contexts_prompt_template_id_prompt_templates` | **64** | `..._prompt__d58c`（60） | `fk_media_generation_contexts_prompt_template_prompt_templates` | 61 |

⚠️ **为什么这不是"风格问题"**：PostgreSQL 标识符上限 `NAMEDATALEN-1` = **63**。
SQLAlchemy 的命名约定在**两条路径都**做"截断 + 4 位摘要"，
但 **`ForeignKeyConstraint.name`（ORM 元数据属性）仍是未截断的原名**。于是：

1. `tests/test_schema_parity.py:174` 从**模型元数据**取 `constraint.name`
   （65 / 64 字节）去比 `pg_constraint.conname`（**被 PG 截断**后 60 字节）
   ⇒ **恒报 2 条 `missing`**（本次修它就是为了消这个假红）；
2. 任何"按元数据名写 `op.drop_constraint(...)`"的迁移/脚本会在**运行期**
   `UndefinedObject` 失败 —— 而元数据侧看起来完全正常、没有半点异常。

⚠️ **CI 门 3 抓不到这一条**：它是"两个空库互比"（alembic 建 ⟷ `create_all` 建），
**两侧都会截断**，截断后的名字一模一样 ⇒ 它照报 **PASS**。
（⇒ 又一条"两条判据覆盖不同盲区、缺一不可"的实例，与 §一 的表格同构。）

⚠️ **超限是"这条 FK 的结构"决定的、不是偶然**：`fk_` + `media_versions` +
`generation_context_id` + `media_generation_contexts` 天然就是 65。
处置：**去掉列名末尾的 `_id`**（FK 名里 `_id` 是冗余的 —— 其后已跟着目标表名），
得 62 / 61 字节，**完整保住 `fk_<table>_<column>_<reftable>` 结构与语义**。

**只对这 2 条显式命名**：本包其余 21 条 FK 仍由命名约定生成
（同表紧邻的 `prompt_version_id` = 61、`model_profile_id` = 60，均未超限，
**不作无谓手写**）—— 显式命名的适用条件就是"约定的产物放不下"。
模型侧的两处 `name=` 在 `models.py` 里有同样口径的注释。

## 三、★ 环外键：本迁移唯一需要"拆两步"的地方

本包有**一个 3 表 SCC**（强连通分量）—— 与 `ISS-110` 记载的残差一致：

```
①  media_items.current_version_id          ──▶  media_versions.id
    media_versions.media_item_id            ──▶  media_items.id

②  media_versions.generation_context_id     ──▶  media_generation_contexts.id
    media_generation_contexts.media_version_id ──▶  media_versions.id
```

### 处置规则（一句话）："被引用的表还没建 ⇒ 后置补；已经建 ⇒ 表内写"

⚠️ **判断依据只有一条：`CREATE TABLE` 的外键引用是建表期检查的**
（`deferrable` / `initially` 都**救不了**这一条 —— 它们管的是**行级**
检查时机，不是**表存在性**）；同理 `DEFERRABLE INITIALLY DEFERRED`
**不能**替代拆环。

**autogenerate 按字母序建表**（`media_generation_contexts` → `media_items`
→ `media_versions` → …），两张引用 `media_versions` 的表都排在它前面 ⇒
**必然 `UndefinedTable`**。这正是 G2 第 4 条预言的形态。

### ★ 建表顺序（与 `models.OWN_TABLES` 的声明顺序**不同**，这是刻意的）

```
media_items → media_generation_contexts → media_versions
→ media_renditions → reference_assets → 后置 2 条环 FK
→ 21 条索引 → 8 个触发器
```

`models.OWN_TABLES` 的顺序是**模型阅读顺序**（身份 → 版本 → …）；
本迁移的顺序是**FK 可满足顺序**。两者由本注释对账，不由脚本对账
（脚本对账的是**内容**，不是顺序 —— 顺序错了会 `UndefinedTable` 当场炸，
判据就是 PG 自己）。

### 逐条点名：哪条表内写、哪条后置

| # | FK | 处置 | 依据 |
|---|---|---|---|
| 1 | `fk_media_versions_media_item_id_media_items` | 表内 | `media_items` 第 1 步已建 |
| 2 | `fk_media_versions_generation_context_media_generation_contexts`（**显式短名**，见 §二补记） | 表内 | `media_generation_contexts` 第 2 步已建 |
| 3 | `fk_media_items_current_version_id_media_versions` | **后置**（第 6 步） | 建 `media_items`（第 1 步）时 `media_versions` 还不存在 |
| 4 | `fk_media_generation_contexts_media_version_id_media_versions` | **后置**（第 6 步） | 建 `media_generation_contexts`（第 2 步）时 `media_versions` 还不存在 |

**⇒ 后置补 2 条**（#3 / #4），其余 2 条（#1 / #2）表内写。
（对比 WP26 的 3 条后置：那里是因为读顺序把 `content_versions` 排在了
环的另一半之前；本迁移把 `media_versions` 排在**环的最后**，
于是它的两条出边全部表内可写，后置只剩 2 条。）

### 四条 `DEFERRABLE INITIALLY DEFERRED` FK（**逐条点名**）

⚠️ **范本注释不可信**（WP26 实测：`briefs` 模块注释声称两条都延迟，
而 `pg_constraint` 实测其中一条是 `deferrable=false`）。本包**从 ORM
元数据逐条派生**，实测本包**共 4 条**延迟 FK：

| FK | 为什么延迟 |
|---|---|
| `fk_media_items_current_version_id_media_versions` | 环①的一半：媒体身份行与首版行**同事务**写入，顺序不可猜 |
| `fk_media_generation_contexts_media_version_id_media_versions` | 环②的一半：上下文行与版本行同事务写入 |
| `fk_media_versions_generation_context_media_generation_contexts`（**显式短名**） | 环②的另一半 |
| `fk_media_versions_id_version_nodes` | 注册表与领域表同事务写入，顺序由 WP11 的取号重试决定（与 WP18/WP20/WP22/WP23/WP26 同款） |

（上表**4 条全部计入**；`models.py` 末尾的断言按
`table.column` 键对账这 4 条 —— **不含约束名**，故 §二补记的显式改名
不会影响它。迁移侧必须**逐条**写出 `deferrable=True, initially="DEFERRED"`。）

> `check_migration_model_parity.py` 比的是 `pg_get_constraintdef` **全文**，
> 因此延迟与否**不由人记**：漏一处，两个空库当场比出差异。

## 四、★ 触发器：autogenerate **不生成触发器**，必须手工补

DDL 来源与测试、证据脚本**完全同源**（`modules/media/ddl.py` 的
`all_trigger_ddl`），因此这里**不重写 SQL**，而是直接调用它 —— 手写第二份
必然漂移，漂移的后果是"找回对象"的检查**假失败或假通过**。

**8 个**（4 条不可变保护 + 4 条跨表一致性）：

| 类别 | 对象 | 挂在哪 |
|---|---|---|
| 不可变 | `trg_media_versions_immutable` 等 **4 个** | 各自的不可变表（见 `models.IMMUTABLE_TABLES`） |
| 一致性（OP-88 正向） | `trg_media_versions_node_consistency` | `media_versions` |
| 一致性（OP-88 反向） | `trg_version_nodes_media_paired` | **共用注册表 `version_nodes`** |
| 一致性（第 3 层 3.1+3.2） | `trg_media_versions_binding_consistency` | `media_versions` |
| 一致性（第 3 层 3.3） | `trg_media_generation_contexts_consistency` | `media_generation_contexts` |

⚠️ **`trg_version_nodes_media_paired` 挂在 WP11 的表上，但它是本迁移建的对象**
—— 不建它，OP-88 第五半只闭合了一半（正向判据有、反向判据没有）；
不删它，`downgrade` 会残留对象挡住后续迁移重建。
它是**第 6 个** `*_node_paired` 触发器（`versions/models.py` 的 8 个取值中，
6 个已有生产者）。

⚠️ **不可变保护那 4 项的 `function_ddl` 是
`CREATE OR REPLACE FUNCTION rednote_reject_mutation()`** —— 它是**跨包共用**的
（WP11 建），这里重放一遍是**幂等**且**必要**的：`all_trigger_ddl()` 的契约
就是"函数 DDL + 触发器 DDL"成对给出，跳过它会破坏与测试的同源性。
（`downgrade` 则**只删本模块的 4 个函数**，见 §五。）

⚠️ **顺序必须是"先全部建表、再建触发器"**：`CREATE CONSTRAINT TRIGGER` 在 PG 里
**不做依赖检查**（plpgsql 函数体在首次调用时才解析），但把触发器放进建表循环
会让读日志的人以为顺序有约束。

## 五、⚠️ `downgrade` 的已知风险与归属判据

`downgrade` 的归属判据不是"它在哪张表上"，而是"**这个对象是不是我建的**"：

| 对象 | 处置 |
|---|---|
| 8 个触发器（含挂在 `version_nodes` 上的那个） | **我建的，删** |
| `rednote_reject_mutation()` | 别人建的**共用**函数，**不删**（删了撞依赖错误并让 11 个包的保护一起失效） |
| 4 个一致性函数（`rednote_check_media_*`） | **我建的，删**（故第 2 步只能用 `consistency_items()`） |
| 21 条索引 | 我建的，删（**先删索引再删表**） |
| 2 条后置环 FK | 我建的，删（**必须先于删表**） |
| 5 张表 | 我建的，删（**反序**） |

⚠️ **本 `downgrade` 有真实且严重的数据损失**：`media_versions` 是图的版本快照
（含段落级参考绑定），`media_generation_contexts` 是 **OP-160 的唯一答案来源**
（"哪个提示词版本被哪些生成消费过"）。回退前须导出全量。
（与 WP11/WP15 的「不可变表不给 downgrade 静默删数据」口径一致：
**报错让人看见，比静默删行好**。）

## 六、验证（四处，缺一不可）

1. **干净库**：`alembic upgrade head` ⇒ 5 表 + 8 触发器 + 21 索引建出；
2. **往返**：`upgrade head` → `downgrade -1` → `upgrade head` 双绿
   （ISS-79 的教训是「往返修复」只有真跑了才知道）；
3. **`tools/quality/check_migration_model_parity.py`** ⇒ 两个空库逐对象
   （`pg_constraint` / `pg_indexes` 全文）比对全绿；
4. **探针表未被触碰** —— 判据是**可执行语句的实参集合**，不是文本 grep：
   本文件中 `op.drop_table(...)` / `op.drop_index(..., table_name=...)` 的
   实参**只含**本迁移自己建的 5 张表与 21 条索引；
   `smoke_meta` / `wp05_vec_probe` 这两个名字**只出现在 `downgrade()` 末尾的
   注释块里**（列举被刻意剔除的语句，便于评审复核）。
   ⚠️ **不能**用 `grep -c 'smoke_meta' <本文件> = 0` 当判据 —— 那个断言
   会被注释块当场证伪（"声称一个不存在的防线"正是 ISS-95 的形态）。
   机械核对方式：解析 AST 取所有 `op.drop_table` 的字面量实参，比对
   `{media_items, media_generation_contexts, media_versions,
   media_renditions, reference_assets}`。
5. **FK 名两侧逐字对账 + 长度上界**（本次踩到 63 字节截断后新增）——
   解析本文件的 AST，取所有 `sa.ForeignKeyConstraint(name=op.f("…"))` 与
   `op.create_foreign_key(op.f("…"), …)` 的**字面量实参**，必须与模型侧
   `media` 5 表的 `ForeignKeyConstraint.name` 集合**逐字相等**
   （**不是"个数相等"** —— 两个名字互换位置、个数不变，只有集合相等才抓得到），
   且**每个名 ≤ 63 字节**。实测：迁移 23 条 ⟷ 模型 23 条，差集为空，无超限。
   ⚠️ 这一条**不是**锦上添花，它堵的是一个只有它看得见的盲区：
   | 判据 | 对"元数据名 ≠ 库名"（§二补记）的反应 |
   |---|---|
   | CI 门 3（两个空库互比） | **PASS** —— 两侧同样截断，名字看起来一样 |
   | `test_schema_parity` | **报 missing**，但只说"库里找不到这条 FK"，**不说根因是长度** |
   | 本条（AST 名字集合 + 长度上界） | **直接把超长的那个名字点出来** |
6. **两条显式短名必须真的落库**（§二补记的执行结果）——
   `pg_constraint.conname` 应为
   `fk_media_versions_generation_context_media_generation_contexts`（62）
   与 `fk_media_generation_contexts_prompt_template_prompt_templates`（61），
   **不再是** `…_media_generatio_4e11` / `…_prompt__d58c` 那种截断尾巴。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# ⚠️ G2 第三条：autogenerate 用了 `postgresql.JSONB` 却**不自动加这行 import**。
# 缺它的表现是"迁移文件语法正确、diff 看着正常、**执行时 NameError**"
# （本项目已连续触发 9 次，登记册 G2 / ISS-02）。本次 autogenerate 产物
# 实测同样缺 —— 本行是手工补的。
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
# 用 repr() 而非 "Ellipsis"：字符串插值会把 Python 的 None 渲染成字符串 "None"，
# 导致 Alembic 解析 down_revision 时 KeyError: 'None'（本机实测踩到过）。
# repr() 输出的是 Python 字面量，None 就是 None，'abc123' 就是 'abc123'。
revision: str = "df6bb2f41f84"
down_revision: str | Sequence[str] | None = "e8f07ba8f2bb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# WP28 才建的两张表。`media_ddl.all_trigger_ddl()` 是活的，已含它们的触发器；
# 本迁移发生时这两张表还不存在，必须按表名排除（见 upgrade 第 8 步注释）。
# 字面量而非 import：与 `c4d9f2a17e35` 的"化石"口径一致，不随模块演进漂移。
_WP28_LATER_TABLES = frozenset({"media_components", "live_profiles"})


def upgrade() -> None:
    """正向迁移：建 5 张表 → 后置 2 条环 FK → 21 条索引 → 8 个触发器。

    建表顺序见 docstring §三（**不是** `models.OWN_TABLES` 的声明顺序）。
    """
    # ### commands auto generated by Alembic - please adjust! ###

    # ==================================================================
    # 第 1 步：media_items（身份行，可变）
    #
    # ⚠️ `current_version_id` 的 FK **刻意不在表内**（引用的
    #    `media_versions` 此刻还不存在）—— 见第 6 步后置补建。
    #    其余 3 条 FK 指向 WP 既存表（content_items / users / projects）。
    #
    # 7 条 CHECK：
    #   · `paragraph_ref_shape`（`^p[0-9]+-[0-9a-f]{8}$`）—— 段落锚点的
    #     形状契约，它让"这张图属于哪一段"可机械解析（不是自由文本）；
    #   · `projection_coherent`（`(current_version_id IS NULL) =
    #     (current_version_no = 0)`）—— 该投影是**一格双列**，
    #     两列分叉时"这张图现在指哪一版"有两个答案（铁律 26）。
    # ==================================================================
    op.create_table(
        "media_items",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("content_item_id", sa.UUID(), nullable=False),
        sa.Column("paragraph_ref", sa.String(length=200), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=20),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("current_version_id", sa.UUID(), nullable=True),
        sa.Column(
            "current_version_no",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_idempotency_key", sa.String(length=160), nullable=True),
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
            "paragraph_ref ~ '^p[0-9]+-[0-9a-f]{8}$'",
            name=op.f("ck_media_items_paragraph_ref_shape"),
        ),
        sa.CheckConstraint(
            "state IN ('FAILED', 'PENDING', 'READY')",
            name=op.f("ck_media_items_state_valid"),
        ),
        sa.CheckConstraint(
            "(current_version_id IS NULL) = (current_version_no = 0)",
            name=op.f("ck_media_items_projection_coherent"),
        ),
        sa.CheckConstraint(
            "char_length(paragraph_ref) <= 200",
            name=op.f("ck_media_items_paragraph_ref_length"),
        ),
        sa.CheckConstraint(
            "char_length(paragraph_ref) > 0",
            name=op.f("ck_media_items_paragraph_ref_not_empty"),
        ),
        sa.CheckConstraint(
            "current_version_no >= 0",
            name=op.f("ck_media_items_version_no_non_negative"),
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name=op.f("ck_media_items_sort_order_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["content_items.id"],
            name=op.f("fk_media_items_content_item_id_content_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_media_items_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_media_items_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_items")),
    )

    # ==================================================================
    # 第 2 步：media_generation_contexts（不可变）
    #
    # ⚠️ `media_version_id` 的 FK **刻意不在表内**（引用的 `media_versions`
    #    此刻还不存在）—— 见第 6 步后置补建。
    #
    # `task_kind IN ('STATIC_IMAGE')` 是**单值域**：本表是"一次**静态图**生成"
    # 的快照；扩容取值域必须开新迁移（同 WP27 的 `e8f07ba8f2bb` 把
    # `STATIC_IMAGE` 补进 `prompt_templates.task_kind` 的取法 —— 字面量让漂移可见）。
    # ==================================================================
    op.create_table(
        "media_generation_contexts",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("content_item_id", sa.UUID(), nullable=False),
        sa.Column("media_version_id", sa.UUID(), nullable=True),
        sa.Column("task_kind", sa.String(length=40), nullable=False),
        sa.Column("prompt_template_id", sa.UUID(), nullable=True),
        sa.Column("prompt_version_id", sa.UUID(), nullable=True),
        sa.Column("model_profile_id", sa.UUID(), nullable=True),
        sa.Column("provider_call_id", sa.UUID(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "task_kind IN ('STATIC_IMAGE')",
            name=op.f("ck_media_generation_contexts_task_kind_valid"),
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) > 0",
            name=op.f("ck_media_generation_contexts_idempotency_key_not_empty"),
        ),
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["content_items.id"],
            name=op.f("fk_media_generation_contexts_content_item_id_content_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_media_generation_contexts_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_profile_id"],
            ["model_profiles.id"],
            name=op.f("fk_media_generation_contexts_model_profile_id_model_profiles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_media_generation_contexts_project_id_projects"),
            ondelete="RESTRICT",
        ),
        # ⚠️ 显式短名（docstring §二之四）：命名约定的产物是 64 字节 > 63 ⇒ 必须手写。
        sa.ForeignKeyConstraint(
            ["prompt_template_id"],
            ["prompt_templates.id"],
            name=op.f(
                "fk_media_generation_contexts_prompt_template_prompt_templates"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version_id"],
            ["prompt_versions.id"],
            name=op.f(
                "fk_media_generation_contexts_prompt_version_id_prompt_versions"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_call_id"],
            ["provider_calls.id"],
            name=op.f("fk_media_generation_contexts_provider_call_id_provider_calls"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_generation_contexts")),
    )

    # ==================================================================
    # 第 3 步：media_versions（不可变）—— 环的**最后一块**
    #
    # 此刻 `media_items` / `media_generation_contexts` 都已存在，
    # 因此本表的两条出边（`media_item_id` / `generation_context_id`）
    # **全部表内可写** —— 这就是后置只剩 2 条的原因（docstring §三）。
    #
    # ⚠️ `id` 既是主键**又是**到 `version_nodes` 的外键（OP-88 的注册表配对）。
    #    `version_nodes` 是 WP11 的既存表。
    #
    # ⚠️ `id` **无 `server_default`**（与另外 4 表的 `gen_random_uuid()` 不同）：
    #    它的值由 WP11 的 `register_version` 取号产生，不由库里生成。
    # ==================================================================
    op.create_table(
        "media_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("media_item_id", sa.UUID(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("paragraph_ref", sa.String(length=200), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column(
            "reference_bindings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("image_file_id", sa.UUID(), nullable=False),
        sa.Column("prompt", sa.String(length=20000), nullable=False),
        sa.Column("regeneration_reason", sa.String(length=500), nullable=True),
        sa.Column("generation_context_id", sa.UUID(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
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
            "jsonb_typeof(payload) = 'object'",
            name=op.f("ck_media_versions_payload_is_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reference_bindings) = 'array'",
            name=op.f("ck_media_versions_bindings_is_array"),
        ),
        sa.CheckConstraint(
            "paragraph_ref ~ '^p[0-9]+-[0-9a-f]{8}$'",
            name=op.f("ck_media_versions_paragraph_ref_shape"),
        ),
        sa.CheckConstraint(
            "char_length(paragraph_ref) <= 200",
            name=op.f("ck_media_versions_paragraph_ref_length"),
        ),
        sa.CheckConstraint(
            "char_length(paragraph_ref) > 0",
            name=op.f("ck_media_versions_paragraph_ref_not_empty"),
        ),
        sa.CheckConstraint(
            "char_length(prompt) <= 20000",
            name=op.f("ck_media_versions_prompt_length"),
        ),
        sa.CheckConstraint(
            "char_length(prompt) > 0",
            name=op.f("ck_media_versions_prompt_not_empty"),
        ),
        sa.CheckConstraint(
            "jsonb_array_length(reference_bindings) BETWEEN 1 AND 8",
            name=op.f("ck_media_versions_bindings_size"),
        ),
        sa.CheckConstraint(
            "regeneration_reason IS NULL OR char_length(regeneration_reason) <= 500",
            name=op.f("ck_media_versions_regeneration_reason_length"),
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name=op.f("ck_media_versions_schema_version_positive"),
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name=op.f("ck_media_versions_sort_order_non_negative"),
        ),
        sa.CheckConstraint(
            "version_no >= 1",
            name=op.f("ck_media_versions_version_no_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_media_versions_created_by_users"),
            ondelete="RESTRICT",
        ),
        # ⚠️ 延迟（docstring §三）：环②的一半。
        # ⚠️ 显式短名（docstring §二之四）：命名约定的产物是 65 字节 > 63 ⇒ 必须手写。
        sa.ForeignKeyConstraint(
            ["generation_context_id"],
            ["media_generation_contexts.id"],
            name=op.f(
                "fk_media_versions_generation_context_media_generation_contexts"
            ),
            ondelete="RESTRICT",
            initially="DEFERRED",
            deferrable=True,
        ),
        # ⚠️ 延迟：注册表与领域表同事务写入。
        sa.ForeignKeyConstraint(
            ["id"],
            ["version_nodes.id"],
            name=op.f("fk_media_versions_id_version_nodes"),
            ondelete="RESTRICT",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["image_file_id"],
            ["file_objects.id"],
            name=op.f("fk_media_versions_image_file_id_file_objects"),
            ondelete="RESTRICT",
        ),
        # ⚠️ **刻意不延迟**：本列非空，"版本属于已存在的媒体位"没有合法流程
        #    需要事后补 —— 环①由**建表顺序**解开（`media_items` 先建），
        #    不靠延迟。延迟它只会放行"版本指向不存在的媒体位"。
        sa.ForeignKeyConstraint(
            ["media_item_id"],
            ["media_items.id"],
            name=op.f("fk_media_versions_media_item_id_media_items"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_versions")),
    )

    # ==================================================================
    # 第 4 步：media_renditions（不可变）
    #
    # `ready_requires_artifact` 是**跨列整体性** CHECK：`state='READY'` 时
    # 五列（file_object_id / width / height / byte_size / image_format）
    # 必须同时在场 —— 防止"标记为交付就绪但产物指针为空"。
    # ==================================================================
    op.create_table(
        "media_renditions",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("media_version_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=20),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("file_object_id", sa.UUID(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("image_format", sa.String(length=20), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('DELIVERY', 'PREVIEW')",
            name=op.f("ck_media_renditions_kind_valid"),
        ),
        sa.CheckConstraint(
            "state <> 'READY' OR (file_object_id IS NOT NULL AND width IS NOT NULL "
            "AND height IS NOT NULL AND byte_size IS NOT NULL "
            "AND image_format IS NOT NULL)",
            name=op.f("ck_media_renditions_ready_requires_artifact"),
        ),
        sa.CheckConstraint(
            "state IN ('FAILED', 'PENDING', 'READY')",
            name=op.f("ck_media_renditions_state_valid"),
        ),
        sa.CheckConstraint(
            "(width IS NULL OR width > 0) AND (height IS NULL OR height > 0)",
            name=op.f("ck_media_renditions_dimensions_positive"),
        ),
        sa.CheckConstraint(
            "attempt_no >= 1",
            name=op.f("ck_media_renditions_attempt_no_positive"),
        ),
        sa.CheckConstraint(
            "byte_size IS NULL OR byte_size > 0",
            name=op.f("ck_media_renditions_byte_size_positive"),
        ),
        sa.CheckConstraint(
            "image_format IS NULL OR char_length(image_format) <= 20",
            name=op.f("ck_media_renditions_image_format_length"),
        ),
        sa.CheckConstraint(
            "note IS NULL OR char_length(note) <= 500",
            name=op.f("ck_media_renditions_note_length"),
        ),
        sa.ForeignKeyConstraint(
            ["file_object_id"],
            ["file_objects.id"],
            name=op.f("fk_media_renditions_file_object_id_file_objects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["media_version_id"],
            ["media_versions.id"],
            name=op.f("fk_media_renditions_media_version_id_media_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_renditions")),
    )

    # ==================================================================
    # 第 5 步：reference_assets（不可变）
    #
    # ⚠️ `supersedes_asset_id` 是**自引用** FK（同表内），表内可写。
    #    `retired_needs_supersedes` 让"退役"必须**指名替代者** ——
    #    否则"这个素材为什么不可用了"没有答案（AQ-03 的追溯线断掉）。
    # ==================================================================
    op.create_table(
        "reference_assets",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("file_object_id", sa.UUID(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=20),
            server_default=sa.text("'ACTIVE'"),
            nullable=False,
        ),
        sa.Column("product_model", sa.String(length=200), nullable=False),
        sa.Column("product_color", sa.String(length=100), nullable=True),
        sa.Column("product_part", sa.String(length=200), nullable=True),
        sa.Column("source", sa.String(length=500), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("supersedes_asset_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "state <> 'RETIRED' OR supersedes_asset_id IS NOT NULL",
            name=op.f("ck_reference_assets_retired_needs_supersedes"),
        ),
        sa.CheckConstraint(
            "state IN ('ACTIVE', 'RETIRED')",
            name=op.f("ck_reference_assets_state_valid"),
        ),
        sa.CheckConstraint(
            "char_length(product_model) <= 200",
            name=op.f("ck_reference_assets_product_model_length"),
        ),
        sa.CheckConstraint(
            "char_length(product_model) > 0",
            name=op.f("ck_reference_assets_product_model_not_empty"),
        ),
        sa.CheckConstraint(
            "note IS NULL OR char_length(note) <= 500",
            name=op.f("ck_reference_assets_note_length"),
        ),
        sa.CheckConstraint(
            "product_color IS NULL OR char_length(product_color) <= 100",
            name=op.f("ck_reference_assets_product_color_length"),
        ),
        sa.CheckConstraint(
            "product_part IS NULL OR char_length(product_part) <= 200",
            name=op.f("ck_reference_assets_product_part_length"),
        ),
        sa.CheckConstraint(
            "source IS NULL OR char_length(source) <= 500",
            name=op.f("ck_reference_assets_source_length"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_reference_assets_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["file_object_id"],
            ["file_objects.id"],
            name=op.f("fk_reference_assets_file_object_id_file_objects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_reference_assets_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_asset_id"],
            ["reference_assets.id"],
            name=op.f("fk_reference_assets_supersedes_asset_id_reference_assets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reference_assets")),
    )

    # ==================================================================
    # 第 6 步：后置补 **2 条**环 FK
    #
    #   ① fk_media_items_current_version_id_media_versions
    #   ② fk_media_generation_contexts_media_version_id_media_versions
    #
    #   **判据是"表存在性"而不是"行存在性"**：此刻 5 张表都已建出，
    #   因此这 2 条 FK 可以被立即校验（`validated`）。
    #   `DEFERRABLE INITIALLY DEFERRED` 管的是**行级插入顺序** ——
    #   媒体身份行与首版行、上下文行与版本行都同事务写入，顺序不可猜。
    #
    #   先例：WP23 的 `prompt_templates.active_version_id`（`3cb74d02edc5`）、
    #   WP26 的 3 条（`1c1c814b248e`），逐字同款处置。
    # ==================================================================
    op.create_foreign_key(
        op.f("fk_media_items_current_version_id_media_versions"),
        "media_items",
        "media_versions",
        ["current_version_id"],
        ["id"],
        ondelete="RESTRICT",
        initially="DEFERRED",
        deferrable=True,
    )
    op.create_foreign_key(
        op.f("fk_media_generation_contexts_media_version_id_media_versions"),
        "media_generation_contexts",
        "media_versions",
        ["media_version_id"],
        ["id"],
        ondelete="RESTRICT",
        initially="DEFERRED",
        deferrable=True,
    )

    # ==================================================================
    # 第 7 步：21 条索引（`op.create_index`，与 `ddl.index_names()` 同集合）
    #
    # ⚠️ 其中 5 条是 `unique=True`（`uq_*`），但它们**不是**
    #    `UniqueConstraint` —— 在 `models.py` 里写成 `Index(..., unique=True)`，
    #    因此建法也是 `op.create_index`（不是表内 `sa.UniqueConstraint`）。
    #    写成约束会让 `check_migration_model_parity` 在两边看到不同的
    #    `pg_constraint` 内容。
    #
    # ⚠️ 索引名在 `upgrade`/`downgrade` 里以**字面量**出现（迁移期无法从模块取），
    #    与 `ddl.index_names()` 的对账**不在本文件里做** —— 由
    #    `check_migration_model_parity.py` 在真库上比 `pg_indexes`。
    #    本包**没有**部分索引（`WHERE` 子句），故没有"WHERE 写错导致索引
    #    静默退化"那一类风险。
    # ==================================================================

    # --- media_items（5 条，含 1 条唯一）---
    # 列表页按内容 + 段落锚点取图。
    op.create_index(
        "ix_media_items_content_paragraph",
        "media_items",
        ["content_item_id", "paragraph_ref"],
        unique=False,
    )
    # "当前生效版本"的反查。
    op.create_index(
        "ix_media_items_current_version",
        "media_items",
        ["current_version_id"],
        unique=False,
    )
    # 幂等重放：`(id, last_idempotency_key)`。
    op.create_index(
        "ix_media_items_idempotency",
        "media_items",
        ["id", "last_idempotency_key"],
        unique=False,
    )
    # 项目视图按状态筛。
    op.create_index(
        "ix_media_items_project_state",
        "media_items",
        ["project_id", "state"],
        unique=False,
    )
    # **一内容一段落一图位**（排序位不允许重复）。这条同时是
    # "并发插入同一段落"的捕获点；服务层撞到后走幂等回放路径。
    op.create_index(
        "uq_media_items_content_sort",
        "media_items",
        ["content_item_id", "sort_order"],
        unique=True,
    )

    # --- media_versions（5 条，含 1 条唯一）---
    op.create_index(
        "ix_media_versions_generation_context",
        "media_versions",
        ["generation_context_id"],
        unique=False,
    )
    op.create_index(
        "ix_media_versions_image_file",
        "media_versions",
        ["image_file_id"],
        unique=False,
    )
    # 版本历史列表。
    op.create_index(
        "ix_media_versions_item_created",
        "media_versions",
        ["media_item_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_media_versions_item_paragraph",
        "media_versions",
        ["media_item_id", "paragraph_ref"],
        unique=False,
    )
    # **一图位一版本号**（业务版本号不允许重复）。与 WP11 的
    # `register_version` 取号重试同款：撞这条即重新取号。
    op.create_index(
        "uq_media_versions_item_version_no",
        "media_versions",
        ["media_item_id", "version_no"],
        unique=True,
    )

    # --- reference_assets（3 条）---
    op.create_index(
        "ix_reference_assets_file_object",
        "reference_assets",
        ["file_object_id"],
        unique=False,
    )
    op.create_index(
        "ix_reference_assets_project_state",
        "reference_assets",
        ["project_id", "state"],
        unique=False,
    )
    op.create_index(
        "ix_reference_assets_supersedes",
        "reference_assets",
        ["supersedes_asset_id"],
        unique=False,
    )

    # --- media_renditions（3 条，含 1 条唯一）---
    op.create_index(
        "ix_media_renditions_state",
        "media_renditions",
        ["state"],
        unique=False,
    )
    op.create_index(
        "ix_media_renditions_version_kind",
        "media_renditions",
        ["media_version_id", "kind", "attempt_no"],
        unique=False,
    )
    # **一版本一类型一尝试号**（同一次尝试不允许重复落行）。
    op.create_index(
        "uq_media_renditions_version_kind_attempt",
        "media_renditions",
        ["media_version_id", "kind", "attempt_no"],
        unique=True,
    )

    # --- media_generation_contexts（5 条，含 1 条唯一）---
    op.create_index(
        "ix_media_generation_contexts_content_kind",
        "media_generation_contexts",
        ["content_item_id", "task_kind"],
        unique=False,
    )
    op.create_index(
        "ix_media_generation_contexts_model_profile",
        "media_generation_contexts",
        ["model_profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_media_generation_contexts_prompt_template",
        "media_generation_contexts",
        ["prompt_template_id"],
        unique=False,
    )
    op.create_index(
        "ix_media_generation_contexts_prompt_version",
        "media_generation_contexts",
        ["prompt_version_id"],
        unique=False,
    )
    # **一内容一幂等键**（同一幂等键不允许重复生成）。
    op.create_index(
        "uq_media_generation_contexts_idempotency",
        "media_generation_contexts",
        ["content_item_id", "idempotency_key"],
        unique=True,
    )

    # ==================================================================
    # 第 8 步：触发器 —— autogenerate **不生成触发器**，必须手工补。
    #   DDL 来源与测试、证据脚本完全同源（`modules/media/ddl.py` 的
    #   `all_trigger_ddl`），因此这里**不重写 SQL**，而是直接调用它。
    #
    #   **8 个**（4 条不可变保护 + 4 条跨表一致性），其中
    #   `trg_version_nodes_media_paired` 挂在**共用注册表** `version_nodes`
    #   上（OP-88 第五半的反向判据，见 docstring §四）。
    #
    #   ⚠️ 不可变保护那 4 项的 `function_ddl` 是
    #   `CREATE OR REPLACE FUNCTION rednote_reject_mutation()` ——
    #   它是**跨包共用**的（WP11 建），这里重放一遍是**幂等**且**必要**的。
    #   （`downgrade` 则**只删本模块的 4 个函数**，见 docstring §五。）
    #
    #   ⚠️ 顺序必须是"先全部建表、再建触发器"（见 docstring §四末段）。
    # ==================================================================
    from rednote.modules.media import ddl as media_ddl

    # ★ 冻结为本迁移当时的 8 项（WP28 4.3 补）。
    #   `media_ddl.all_trigger_ddl()` 是**活的**：WP28 往里加了
    #   `media_components` / `live_profiles` 两张表的触发器，而这两张表要到
    #   后继迁移（`wp28_live_components`）才建。不排除它们，全新库从零迁移到
    #   本修订时会在 `CREATE TRIGGER trg_media_components_immutable` 处报
    #   `relation "media_components" does not exist`（已实测复现）。
    #   这两张表的触发器由 WP28 迁移自己建；这里按**表名**排除，不改 SQL 来源。
    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in media_ddl.all_trigger_ddl():
        if _table in _WP28_LATER_TABLES:
            continue
        op.execute(_function_ddl)
        op.execute(_trigger_ddl)

    # ------------------------------------------------------------------
    # ★ **本迁移没有数据回填** —— 这是决定，不是遗漏。
    #   `media_versions` / `reference_assets` / `media_renditions` /
    #   `media_generation_contexts` 的行**只能由生成流程产生**（要真调模型、
    #   要真上传素材），任何"初始数据"都是假的；而假数据会污染 OP-160 的
    #   答案（"哪个提示词版本被消费过"）。
    #
    #   因此这里**不出现 `Session(bind=op.get_bind())`**（对照 WP24 的第五节）。
    # ------------------------------------------------------------------

    # ### end Alembic commands ###


def downgrade() -> None:
    """反向迁移。

    必须补全，不能留 `NotImplementedError` —— 那会让任何回滚尝试直接失败，
    与 WP47 的恢复演练（RPO/RTO）目标冲突。

    **只删本迁移自己建出来的对象。** 判据不是"它在哪张表上"，
    而是"**这个对象是不是我建的**"（逐项见 docstring §五）：

      · 8 个触发器 → 我建的，删。**含挂在 `version_nodes` 上的
        `trg_version_nodes_media_paired`** —— 它虽在别人的表上，
        但是**本迁移建的对象**，不删会残留。
      · `rednote_reject_mutation()` → 别人建的共用函数，**不删**。
        因此第 2 步用 `ddl.consistency_items()`（本包**非空，4 项**），
        **不得**改用 `all_trigger_ddl()` —— 它的 `drop_function` 指向共用函数。
      · 4 个一致性函数 → 我建的，删。
      · 21 条索引 → 我建的，删（**先删索引再删表**）。
      · 2 条后置环 FK → 我建的，删（**必须先于删表**，见第 4 步）。
      · 5 张表 → 我建的，删（**反序**）。

    ⚠️ **本 `downgrade` 有真实且严重的数据损失**：
    `media_versions` 是图的版本快照（含段落级参考绑定 `reference_bindings`），
    `media_generation_contexts` 是 **OP-160 的唯一答案来源**
    （"哪个提示词版本被哪些生成消费过"）。回退前须导出全量。
    """
    # ### commands auto generated by Alembic - please adjust! ###
    from rednote.modules.media import ddl as media_ddl

    # ---- 1. 先删触发器（触发器 DROP 必须先于函数 DROP）----
    # 顺序不能反：函数仍被触发器引用时，PG 会拒绝删除
    # （"cannot drop function ... because other objects depend on it"）。
    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in media_ddl.all_trigger_ddl():
        if _table in _WP28_LATER_TABLES:
            continue  # WP28 迁移建的，由它的 downgrade 删（见第 8 步注释）
        op.execute(_drop_trigger)

    # ---- 2. 只删本模块自己的 4 个函数 ----
    # **不得**用 `all_trigger_ddl()` —— 它的 `drop_function` 还包含
    # 4 项指向跨包共用的 `rednote_reject_mutation()`（见 docstring §五）。
    # `consistency_items()` 返回的 4 项，其 `_drop_function` 分别指向
    # `rednote_check_media_version_consistency` /
    # `rednote_check_media_node_paired` /
    # `rednote_check_media_binding_consistency` /
    # `rednote_check_media_context_consistency` —— 都是本模块建的。
    for (
        _table,
        _function_ddl,
        _trigger_ddl,
        _drop_trigger,
        _drop_function,
    ) in media_ddl.consistency_items():
        if _table in _WP28_LATER_TABLES:
            continue
        op.execute(_drop_function)

    # ---- 3. 删 21 条索引（与第 7 步严格反序）----
    # ⚠️ 索引随表删除时也会一并消失；**显式写出来**是为了让
    # "我删了这些防线"在迁移日志里**可见**（与 WP22/WP24/WP25/WP26 同一做法）。

    # -- media_generation_contexts（5 条）--
    op.drop_index(
        "uq_media_generation_contexts_idempotency",
        table_name="media_generation_contexts",
    )
    op.drop_index(
        "ix_media_generation_contexts_prompt_version",
        table_name="media_generation_contexts",
    )
    op.drop_index(
        "ix_media_generation_contexts_prompt_template",
        table_name="media_generation_contexts",
    )
    op.drop_index(
        "ix_media_generation_contexts_model_profile",
        table_name="media_generation_contexts",
    )
    op.drop_index(
        "ix_media_generation_contexts_content_kind",
        table_name="media_generation_contexts",
    )

    # -- media_renditions（3 条）--
    op.drop_index(
        "uq_media_renditions_version_kind_attempt",
        table_name="media_renditions",
    )
    op.drop_index(
        "ix_media_renditions_version_kind", table_name="media_renditions"
    )
    op.drop_index("ix_media_renditions_state", table_name="media_renditions")

    # -- reference_assets（3 条）--
    op.drop_index("ix_reference_assets_supersedes", table_name="reference_assets")
    op.drop_index(
        "ix_reference_assets_project_state", table_name="reference_assets"
    )
    op.drop_index("ix_reference_assets_file_object", table_name="reference_assets")

    # -- media_versions（5 条）--
    op.drop_index(
        "uq_media_versions_item_version_no", table_name="media_versions"
    )
    op.drop_index(
        "ix_media_versions_item_paragraph", table_name="media_versions"
    )
    op.drop_index("ix_media_versions_item_created", table_name="media_versions")
    op.drop_index("ix_media_versions_image_file", table_name="media_versions")
    op.drop_index(
        "ix_media_versions_generation_context", table_name="media_versions"
    )

    # -- media_items（5 条）--
    op.drop_index("uq_media_items_content_sort", table_name="media_items")
    op.drop_index("ix_media_items_project_state", table_name="media_items")
    op.drop_index("ix_media_items_idempotency", table_name="media_items")
    op.drop_index("ix_media_items_current_version", table_name="media_items")
    op.drop_index("ix_media_items_content_paragraph", table_name="media_items")

    # ---- 4. 删 2 条后置环 FK（**必须先于删表**）----
    # 不先删会怎样：`media_items.current_version_id` 与
    # `media_generation_contexts.media_version_id` 都引用 `media_versions`，
    # 而这两张表在删表序里排在 `media_versions` **之后** ——
    # `DROP TABLE media_versions` 会被 PG 以
    # "cannot drop table media_versions because other objects depend on it"
    # 拒绝。删 FK 的顺序与第 6 步**严格反序**。
    op.drop_constraint(
        op.f("fk_media_generation_contexts_media_version_id_media_versions"),
        "media_generation_contexts",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_media_items_current_version_id_media_versions"),
        "media_items",
        type_="foreignkey",
    )

    # ---- 5. 最后删表（**反序**：先删引用别人的，后删被引用的）----
    #
    # 反序的依据（建表正序见 `upgrade()` 的注释）：
    #   reference_assets → media_renditions → media_versions
    #   → media_generation_contexts → media_items
    #
    # `media_versions` 可以排在 `media_items` / `media_generation_contexts`
    # 之前删：它的两条出边（`fk_media_versions_media_item_id_media_items` /
    # `fk_media_versions_generation_context_media_generation_contexts`）
    # 属于**子表**自身，随它删除时一并消失；而被别人引用的那两条（第 4 步）
    # 已先删掉。
    #
    # ⚠️ `version_nodes` **不在**删除集合里（WP11 的资产）：
    #    `media_versions.id → version_nodes.id` 这条 FK 属于**子表**
    #    `media_versions`，随它删除时一并消失；`version_nodes` 的
    #    表结构与行**不受影响**。
    op.drop_table("reference_assets")
    op.drop_table("media_renditions")
    op.drop_table("media_versions")
    op.drop_table("media_generation_contexts")
    op.drop_table("media_items")

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
    # 本迁移只碰 media_items / media_generation_contexts / media_versions /
    # media_renditions / reference_assets 五张表 + 8 个触发器
    # + 21 条索引 + 2 条后置环 FK。
    # ------------------------------------------------------------------
