"""WP27 阶段 6：`media_items` 加任务寻址列与重做理由暂存列。

revision: c4d9f2a17e35
down_revision: b7c1e4a9d203

**本迁移做三件事：加两列 + 一个唯一索引 + 一条 CHECK。**

| 加什么 | 为什么 |
|---|---|
| `media_items.job_id`（FK → `jobs.id`，`RESTRICT`，可空） | 任务**寻址**（见 §一） |
| `uq_media_items_job_id`（唯一） | 寻址的**判据**（见 §二） |
| `media_items.pending_regeneration_reason`（可空，`VARCHAR(500)`） | F7 重做理由的**输入暂存位**（见 §三） |
| `ck_media_items_pending_regeneration_reason_length` | 长度上界（见 §四） |

---

## 一、★ 为什么必须加 `job_id`（不是"顺手加的"）

`KIND_MEDIA_GENERATE` 的任务要产出"**某一张图**的新版本"，因此处理器
必须同时知道 `media_item_id` 与 `content_id`（后者用于读正文切段、算 R4）。

而任务框架给处理器的东西只有 `ClaimedJob`
（`job_id / kind / attempt_id / fence_token / attempt_no / worker_id /
input_version_ids`）—— **没有自由参数位**（`jobs/types.py`）。
`jobs` 表也没有 actor / payload 列，唯一能装关联对象的只有
`jobs.content_id`（裸 UUID，**刻意无 FK**：`jobs/models.py:195` 写明
"`batches` / `contents` 表属 WP24 / WP29，现在建外键会指向不存在的表"）。

三条先例，**形态相反**：

| 包 | 任务目标 vs 入队对象 | 处置 |
|---|---|---|
| WP26 | 同一（都是内容） | 用 `jobs.content_id` 装 `content_items.id` |
| WP17/parsing | **不同**（目标是产出 `parse_run`） | 加 `parse_runs.job_id`（FK → `jobs.id`）+ 索引，处理器 `claimed.job_id` **反查** |
| WP27 | 目标是"单张图的新版本"，且**还要** `content_id` | ⇒ **取 WP17 同构** |

**为什么不能借 `jobs.content_id`**：它只有一个位。装 `media_item_id`
⇒ 处理器拿不到 `content_id`；装 `content_id` ⇒ 处理器拿不到"是哪一张图"，
只会重做第一格。**两者都静默做错**（不报错），因此都不是选项。

**为什么不改 `jobs` 表**：`content/types.py:597` 已把"本包不修改 `jobs`"
定为接缝冻结铁律（WP13 的资产）。加列会同时影响 WP17 / WP26 两条已在跑
的任务链，而收益只是省一次反查。

**为什么落在 `media_items` 而不是新建一张映射表**："这一格当前在途的是哪个
任务"是**一格的属性**，不是独立实体；建第 6 张表会让"清理/对账"多一处
与 `media_items` 同生命周期却不同步的行。

## 二、★ 为什么是**唯一**索引（与 WP17 的普通索引刻意不同）

WP17 的 `ix_parse_runs_job_id` 是**普通**索引。本包用**唯一**：

1. **语义上就该唯一** —— 一个 `MEDIA_GENERATE` 任务恰好产出一格图的一个新版本
   （`types.calls_for(单图) = 1`；任务按**媒体行**逐条入队，
   `MAX_IMAGES_PER_REQUEST` 是**请求**级上界而不是任务级）；
2. **唯一约束才是判据** —— 普通索引下，"一个 job 指向两格图"这个**装配故障**
   没有任何东西会拦；处理器反查会随 `ORDER BY` 的实现细节拿到其中一格，
   **而后台不报错**。WP17 用普通索引是因为那里"一个 job 一个 run"没有
   DB 层可表达的约束；本包不同 —— 本列只是"当前在途指针"，
   版本历史在 `media_versions`，所以唯一性成立**且可判**；
3. **不会挡住重做** —— 重做是**新 job 覆盖同一行的本列**（UPDATE，不是
   INSERT 新行）⇒ 唯一约束不影响它。被它挡住的只有"两条媒体行挂同一个 job"，
   那是 bug。

⚠️ PostgreSQL 的 UNIQUE 索引**允许多个 NULL**（`NULLS DISTINCT` 是默认）
⇒ "尚未入队的媒体行"（可能上千行）全部合法共存。
**不要**改成 `NULLS NOT DISTINCT` —— 那会让第二行 `NULL` 就违反约束，
直接建不出表。本迁移用的是默认行为，**不加任何 `postgresql_nulls_not_distinct`**。

## 三、★ 为什么必须加 `pending_regeneration_reason`

与 `pending_reference_bindings`（`b7c1e4a9d203`）是**同一个接缝的第二个缺口**，
理由逐条同构：端点在**发起**事务里拿到运营填的理由，任务在**另一次事务**里
才写版本行 —— 中间那一列就是本条。

**不补这一列会怎样**（补列前的实测）：`service.item_pending_regeneration_reason`
没有可读的暂存位，**只能恒返回 `None`** ⇒ 每一次重做产生的版本行
`regeneration_reason` 都是 NULL。而那一列存在的唯一理由就是回答
"第 2 版与第 1 版为什么不一样"（`models.py` docstring 十）——
**列在、值恒空、不报错** ⇒ 这是"看起来有留痕、实际没有留痕"，
比没有这一列更坏（ISS-95「声称有防线」同族）。

**为什么不并进 `pending_reference_bindings` 的 JSON**：那一列的 CHECK 是
`jsonb_array_length BETWEEN 1 AND 8`（约束**数组元素**），而理由是**标量**。
混进去要么让数组多出一个非绑定元素（`MediaReferenceBinding.from_payload`
当场抛 `ValueError` ⇒ 把"重做"报成 `REFERENCE_BINDING_MALFORMED`），
要么把 `jsonb_typeof` 从 `'array'` 改成 `'object'` 而作废那两条 CHECK。

**可空，且 NULL 是常态**：首次生成**没有**理由可言
（与 `media_versions.regeneration_reason` 同一取值口径）。

## 四、CHECK 的口径：只拦长度，**不**强制非空

`ck_media_items_pending_regeneration_reason_length` 沿用版本列那条
（`ck_media_versions_regeneration_reason_length`）的**同一 SQL 形状**：

    pending_regeneration_reason IS NULL
      OR char_length(pending_regeneration_reason) <= 500

**为什么不落"重做时必非空"CHECK** —— 与 `models.py` docstring 十
对版本列的结论**逐字同一条理由**："这一行是不是重做产生的"**无法从列判断**
（重做与首次生成在列上同形），那种 CHECK 恒真或恒假（ISS-115 族）。
这条不变量归服务层：`POST /media/{id}/regenerations` 必然写它。

**空串在这里合法，端点必须拒它**：`char_length('') = 0` ⇒ `''` 通过。
这是**刻意的口径对齐**（与版本列一致）：SQL 层拦"超长"，
"理由不能是空白"是**输入校验**，`schemas.py` 在端点层拒并报业务码，
而不是让用户看见一个 CHECK violation 的 500。

**上界与版本列同源**（都在 `types.MAX_REGENERATION_REASON_CHARS`）：
两列各写一个上界 ⇒ "端点按 A 校验通过、DB 按 B 拒掉"，
而报出来的是 SQLSTATE 而不是一个业务 reason 码。

### ★ 实测：这一列上其实有**两道**防线，而**列宽先说话**

`String(500)` 与 `char_length(...) <= 500` 是**同一个量的两条 DB 防线**。
本包在私有库上实测（放宽列宽到 2000 后重试同一插入）：

| 情形 | 谁拒的 | SQLSTATE |
|---|---|---|
| 正常列宽（500）+ 501 字符 | **`varchar(500)`** | `22001 StringDataRightTruncation` |
| 列宽放宽到 2000 + 501 字符 | **`ck_media_items_pending_regeneration_reason_length`** | `23514 CheckViolation` |

⇒ 两条**都不是恒真的**（深层探针坐实 CHECK 活着），但**正常情况下 CHECK 永远轮不到**。
这与 `media_versions.regeneration_reason`（同样 `String(500)` + 同样形状的 CHECK）
**逐字同形** ⇒ 它是本仓既有形态，不是本迁移引入的。

**这个观察对端点层的硬要求**：用户不该看见任何一个 SQLSTATE。
`api.py` **必须**在写库前按 `MAX_REGENERATION_REASON_CHARS` 校验超长
并翻成业务 reason 码 —— 否则一个 501 字的理由会变成 **500 + SQLSTATE 22001**，
而排障的人会去查"DB 怎么了"而不是"输入超了"。
（与 `models.py` docstring 十对版本列的结论同一处置。）

## 五、`downgrade()` 必须完整实现（G2）

删本迁移创建的**四样**（检查约束 / 列 / 唯一索引 / 列 / CHECK），
**不碰** `df6bb2f41f84` 建的 8 个不可变触发器、不碰
`rednote_reject_mutation()`、不碰 `b7c1e4a9d203` 的两条 CHECK。

**删除顺序有硬要求**：必须先 `drop_constraint` / `drop_index`，
再 `drop_column`。反过来会被 PG 以 `cannot drop column ... because
other objects depend on it` 拒（CHECK 与索引都依赖该列）。

## 六、`upgrade()` 的执行顺序与 NOT VALID 的判断

本迁移**无数据回填**（新列全 NULL，无历史行会被判失败），
且 `ADD CONSTRAINT` 在 PG 17 上同样会扫全表 —— 但**空表加列后扫描代价为零**，
因此**不需要** `NOT VALID` + `VALIDATE CONSTRAINT` 两步。
（与 `b7c1e4a9d203` §五同一判断。）

`create_unique_index` **可以在有数据的表上直接建**：本仓 `media_items`
当前在生产/测试库里都是空表或近乎空表；即便有数据，
唯一索引建立时若有重复的**非 NULL** `job_id`，会**当场失败** ——
那是我们想看到的行为（不静默用 `CREATE INDEX CONCURRENTLY` 藏问题）。

## 七、判据覆盖与已知盲区

| 判据 | 覆盖什么 |
|---|---|
| `check_migration_model_parity.py` | 新列 / 新索引 / 新 CHECK 的**存在与形态**（两库同时从零建后逐对象比） |
| `test_schema_parity.py` | deferrable FK 子集（本列的 FK **不**延迟 ⇒ 不在其中） |
| `tests/test_media_wp27.py` | 唯一索引的**行为**（两条行挂同一 job ⇒ 拒）；CHECK 的长度行为 |

⚠️ **盲区（登记为已知缺口，不假装覆盖）**：`check_migration_model_parity.py`
不比较 CHECK 的**表达式文本**（只比名字与表的成员关系）⇒ **本文件这侧**
已冻结成字面量（见文件头注释），但 `models.py` 那侧仍引用活常量
`types.PENDING_REGENERATION_REASON_SQL`；若两边取值分叉（仍能建库、
仍能通过列对账），**没有判据会红**。本包登记的 `check_migration_value_
domain_no_live_import.py` 只保证**迁移侧是化石**，不保证**模型侧与化石
一致** —— 后者需要"parity 比 CHECK 表达式文本"，属独立动作。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ⚠️ **刻意不 import 任何 `rednote.*` 名字**（见 §八第 3 条）：
# 迁移文本必须是"建库那一刻的化石"。本迁移首版从 `media.types` 引入了
# `PENDING_REGENERATION_REASON_SQL` / `MAX_REGENERATION_REASON_CHARS`，
# 被 `tools/quality/check_migration_value_domain_no_live_import.py` 的 G1
# 判红后**已改回字面量（2026-10-10）**。理由：`ALTER TABLE ... ADD
# CONSTRAINT` 与 `ADD COLUMN` 都只在迁移执行那一刻取值一次，之后
# `types.py` 怎么改，老库都不动 ⇒ 委托会造成"新库能过、老库过不了"
# 且**无判据报红**；字面量则把分叉变成"看得见的红"。
# 本文件的口径：长度上界 **500**、CHECK 文本为
# `pending_regeneration_reason IS NULL OR char_length(...) <= 500`
# （均 = 2026-10-08 当时的取值）。

revision: str = "c4d9f2a17e35"
down_revision: str | None = "b7c1e4a9d203"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """加两列 + 一个唯一索引 + 一条 CHECK。**不复用** `df6bb2f41f84` 的 `create_table`。"""
    # ---- 1. 任务寻址列 ------------------------------------------------
    # `FK jobs.id` —— 与 WP17 的 `parse_runs.job_id` **逐字同构**：
    #   同一目标表、同一 `ondelete="RESTRICT"`、同一可空性。
    #   ⚠️ **显式写 `name=`**：按命名约定会生成
    #   `fk_media_items_job_id_jobs`（25 字节，未超限），
    #   但本仓 `df6bb2f41f84` 已因 63 字节问题吃过一次亏
    #   （见 `models.py` 的 `generation_context_id` 注释）⇒
    #   本处**显式命名**，让"元数据名 == 库里名"在任何环境下都成立，
    #   而不是依赖或然成立的字节数。
    op.add_column(
        "media_items",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_media_items_job_id_jobs"),
        "media_items",
        "jobs",
        ["job_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # ---- 2. 重做理由暂存列 --------------------------------------------
    # ⚠️ 长度 **500** 是字面量而非 `MAX_REGENERATION_REASON_CHARS`：
    #   列宽同样只在建表/加列那一刻进库，委托给活常量会让老库的列宽
    #   永久停在旧值而新库拿到新值（与 CHECK 文本同因，见文件头注释）。
    #   `models.py` 那侧仍引用该常量 —— 两侧取值若分叉，
    #   `check_migration_model_parity.py` 会**在列类型上**报红（它比列宽），
    #   故此处不需要额外判据。
    op.add_column(
        "media_items",
        sa.Column(
            "pending_regeneration_reason",
            sa.String(length=500),
            nullable=True,
        ),
    )

    # ---- 3. 唯一索引（§二）--------------------------------------------
    # `unique=True` 对应 `models.py` 的 `Index("uq_media_items_job_id", "job_id", unique=True)`。
    # ⚠️ 名字前缀是 `uq_` 而**不是** `ix_` —— 与 `models.py` 逐字一致。
    #   迁移与模型任何一处前缀写错，`check_migration_model_parity.py`
    #   都会在**索引名**上红（它比名字），这里刻意让两处同名同前缀。
    op.create_index(
        "uq_media_items_job_id",
        "media_items",
        ["job_id"],
        unique=True,
    )

    # ---- 4. 长度 CHECK（§四）------------------------------------------
    # ⚠️ 用 `op.create_check_constraint` 而非 `batch_alter_table`：本仓 PG 17，
    #   `ALTER TABLE ... ADD CONSTRAINT` 对 NOT VALID 的 CHECK 同样会扫全表，
    #   而本列**新增即全 NULL**（无历史行会被判失败）⇒ 直接 ADD 即可。
    # ⚠️ **SQL 文本在本文件写字面量**，**不从 `media.types` import**
    #   （见文件头注释与 §八第 3 条）—— 委托给活常量会让老库的 CHECK
    #   永久停在旧值，而 `check_migration_model_parity.py` **比不到
    #   CHECK 文本**（§七盲区）⇒ 漂移不可见。
    op.create_check_constraint(
        op.f("ck_media_items_pending_regeneration_reason_length"),
        "media_items",
        "pending_regeneration_reason IS NULL"
        " OR char_length(pending_regeneration_reason) <= 500",
    )


def downgrade() -> None:
    """只删本迁移创建的四样，**不碰** `b7c1e4a9d203` 与 `df6bb2f41f84` 的任何对象。"""
    # ⚠️ 顺序：先删依赖列的对象（CHECK / 索引 / FK），再删列。
    #    反过来会被 PG 以 "cannot drop column ... because other objects
    #    depend on it" 拒 —— 而 `check_migration_roundtrip.py` 会跑到这一步。
    op.drop_constraint(
        op.f("ck_media_items_pending_regeneration_reason_length"),
        "media_items",
        type_="check",
    )
    op.drop_index("uq_media_items_job_id", table_name="media_items")
    op.drop_constraint(
        op.f("fk_media_items_job_id_jobs"), "media_items", type_="foreignkey"
    )
    op.drop_column("media_items", "pending_regeneration_reason")
    op.drop_column("media_items", "job_id")
