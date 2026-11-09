"""WP27 的触发器 DDL + 索引清单来源。

**为什么 DDL 生成放在模块里而不是迁移文件里**（与 WP11/WP18/WP20/WP22/WP23/WP24/WP25/WP26 同一理由）：
验证脚本要按名字回查 `pg_trigger` / `pg_indexes`。若名字由迁移文件手写，
验证脚本也得手写一遍同样的字符串，两处一旦不一致，"找回触发器"的检查
就会**假失败**或**假通过**。名字只有一个来源才靠得住。

---

## 一、不可变保护（4 张表）

复用跨包共用的 `rednote_reject_mutation()` 函数（WP11 建）。
**本包只挂触发器，不重建函数**，且 `downgrade` **不删它** ——
删了会撞依赖错误并让 WP11/WP15/WP16/WP17/WP18/WP20/WP22/WP23/WP24/WP25/WP26
的保护一起失效（WP16 差点踩，其后各包明确防了，WP26 在
`consistency_items()` 的 docstring 里把这条写成了一条**约束**）。

挂哪四张：见 `models.IMMUTABLE_TABLES`（`media_versions` /
`reference_assets` / `media_renditions` / `media_generation_contexts`）。
`media_items` **刻意不挂** —— 它是身份行，承载会前进的四个投影与
`lock_version`（ETag 载体），必须可变。

## 二、OP-88：`MEDIA` 的节点一致性（正向 + 反向）

数据结构 02 §5 的原句是：

> 每个具体版本表的 `id` 同时 FK `version_nodes.id`；注册表、具体表及引用
> 同一事务插入。**延迟约束检查 entity_type、entity_id 与实际子表一致，
> 禁止孤立节点。**

WP18 闭合 `BRIEF`、WP20 闭合 `KNOWLEDGE`、WP22 闭合 `BASELINE`、
WP23 闭合 `PROMPT`、WP26 闭合 `CONTENT`。**本包闭合 `MEDIA`** ——
这是**第 6 个** `*_node_paired` 触发器。闭合之后，`versions/models.py` 里
已定义的 8 个取值中，**6 个有生产者**（`PLAN` / `REPORT` 仍无 ——
它们分别属 WP24 未采用与 WP38 未开工，与本包无关）。

### ★★ 本包必须带上 WP26 勘误的那道守卫（ISS-175）—— 这是本文件最要紧的一处

`version_nodes` 是**跨包共用**的注册表。**WP26 的第一版**漏了"先放行
不归我管的"那一道，实测**直接破坏 WP11 既有 2 例测试**（造 19 个 `CONTENT`
节点、断言 `entity_type` 不被重置），因果已用 `DISABLE/ENABLE TRIGGER` 钉死。
⇒ **凡新增 `*_node_paired`，两道判断的顺序不能换**：

1. **先判"归不归我管"** —— `media_items` 里有没有 `id = NEW.entity_id` 的行？
   没有就 `RETURN NULL` **放行**。这一道是**跨包契约**：WP11 的通用用例
   （`tests/test_versions_wp11.py`）会拿 `MEDIA` 当代表性样本造"无领域行的节点"
   （其 `test_versions_wp11.py:292` 实测就把它当样例用）。
2. **再判"配对是否存在"** —— `media_versions` 里有没有 `id = NEW.id` 的行？
   没有就 `RAISE`。

⚠️ **反过来说：漏掉第 1 道的表现是"一个跨包共用的表被本包的单包判据卡住"**，
而它**只在别的包的测试里炸** —— 本包的测试**全绿**。这正是
"声称有防线比没有防线更危险"（ISS-95）在触发器上的形态。

#### 守卫会让判据变成恒真式吗（WP26 被问过同一问题，答案同样是不）

守卫挡掉的是"`entity_id` 指向不存在的媒体身份"这一类，而
**本包永远不产生这种节点** —— 写入顺序是**先落 `media_items` 身份行、
再 `register_version`（造节点）**，所以生产路径上 `media_items` 行
**必然已存在**，守卫不会误放。
因此本判据真正要拦的形态 ——「**媒体位真实存在、但节点没有配对版本行**」
（`register_version` 成功而 `insert_media_version` 失败所致的静默分叉）——
**被完整保留**。测试里有一条对照用例专门锁这个行为。

**这条收窄的代价已登记**（与 WP18/20/22/23/26 同款）：`entity_id` 指向
不存在的 `media_items.id` 的"悬空节点"不会被拦下。危害等级低 ——
悬空节点不被任何东西引用（`media_versions` 里没有它），溯源也走不到它。

### 正向判据（挂 `media_versions`，三条）

| # | 判据 | 为什么必须存在 |
|---|---|---|
| 1 | `media_versions.id` 必须有一个 `version_nodes` 行 | FK 在 `models.py` 里已声明，但 FK 是**独立**保证；本触发器让"节点缺失"在**同一处**给出与前五个包同款的错误消息（证据脚本按 `sqlstate` 断言，不按消息文本） |
| 2 | 那个节点的 `entity_type` 必须是 `'MEDIA'` | FK 管不住方向：它只保证被引用的行存在，不保证那行的 `entity_type` 对 |
| 3 | 那个节点的 `entity_id` 必须等于本行的 `media_item_id` | `MEDIA` 的"逻辑实体"是**媒体身份**（`models.py` docstring 一/七）。不一致意味着"给媒体位 A 注册了一个声称属于媒体位 B 的版本" |

> ⚠️ **判据 1 会被 FK 遮蔽**（与 WP26 实测同现象）：
> 反例「版本行没有对应节点」报的是 **`23503`（FK）**，不是本触发器的 `23514`。
> 两条都是 `DEFERRABLE INITIALLY DEFERRED`，在同一个结算点被检查，
> 而 **FK 先出手**。这不构成缺陷（判据 1 的价值本就是"同款错误消息"），
> 但**必须写下来**：证据脚本对这条只能断言 `23503`。
> 谎称是触发器拦下的就是"声称自己有防线"（ISS-95 形态）。
> 判据 2/3 **不受遮蔽** —— 它们管的是 FK 管不住的方向。

## 三、第 3 层：三条**跨表链接**不变量（WP26 同构的"同一类形态"）

说明文档 §1.7 只规定了不可变与 OP-88。本节的判据是**实现期发现**的 ——
与 WP26 的第三节**同一类发现**：**两条 FK 各自都绿，但合起来说的是错的**。

### 3.1 参考素材绑定的每条元素必须**真实存在**（`reference_bindings`）

`models.py` docstring 二已说明：绑定落成一列 JSONB，**JSONB 里的元素 id
没有引用完整性** —— DDL 的 FK 管不住数组元素。这一条补的就是那个缺口。

    reference_bindings = [{"assetId": "<不存在的 uuid>", ...}]
    FK 表态：没有 FK 可表态  → 全绿（而这条绑定指向空气）

后果：**AQ-03 的对照关系指向一个不存在的素材** ⇒ "这张图画的型号对不对"
**永远答不出来**，而系统显示一切正常。这正是 R1 的失效形态的**第二种**
（第一种"绑 0 条或整库"已被 `models.py` 的 `bindings_size` CHECK 拦下）。

### 3.2 绑定的素材必须与图属于**同一个项目**（`reference_bindings`）

    媒体位属于项目 A，绑的素材属于项目 B
    FK 表态：两列的 `id` 都存在 ✅  → 全绿

后果：**跨项目的素材被引用**。AQ-03 是**按项目**做的盲审
（每个项目的产品不同），跨项目引用会让"这批图里混进了别的项目的产品"
这件事**静默发生** —— 而"跨品牌事实混入 = 0"是验收的硬线。

`types.py` 里有专门的 `REASON_REFERENCE_BINDING_CROSS_PROJECT`，
说明这条在 Python 侧已有理由码（`schemas.py` 先判一道，本触发器是
**绕不过去的第二道** —— 数据导入脚本、将来的迁移都不走 `schemas.py`）。

### 3.3 生成上下文引用的版本必须属于**同一条媒体**（吊 `media_generation_contexts`）

    media_generation_contexts(media_version_id = V) 而 V.media_item_id ≠ 本行的参考媒体
    FK 表态：两列各自存在 ✅  → 全绿

后果：**OP-160 的影响面分析被算到别的图上**。OP-160 要回答"改了提示词之后
哪些历史生成受影响"，而答案按内容归组 —— 两列不一致时，"受影响的图"
这个集合**错得无声无息**。

⚠️ 本包的这一条**比 WP26 的同款更弱一层**：`media_generation_contexts`
上**没有** `media_item_id` 列（它只有 `content_item_id`，
见 `models.py` docstring 九的对照表）。因此本判据的形式是
"上下文行的 `content_item_id` 必须等于它引用的媒体版本所属的 **`media_item.content_item_id`**"
—— 即**跨两级**核对。这不是设计缺陷，而是"内容身份在上下文行上有、
媒体身份没有"这一事实的直接后果。

**为什么不索性给上下文行加一列 `media_item_id`**：那会让
"这一版属于哪个媒体位"有**两个来源**（`media_version_id` 可达一个、
新列直接给一个），而两者分叉时**没有答案** —— 违反铁律 26。
现形式只多一次 JOIN，不引入第二真值来源。

### 3.1 / 3.2 的 `IF NOT FOUND` 分支**不是**防御性的（与 WP26 的 3.1/3.2 相反）

WP26 的 3.1/3.2 那两条 FK 是 IMMEDIATE ⇒ "引用的行不存在"在到达触发器
之前就被 FK 以 `23503` 拒掉 ⇒ 它们的 `IF NOT FOUND` **实测不可达**，
保留只为"将来有人把 FK 改成 DEFERRED 时语义不变"。

**本包的 3.1/3.2 完全不同**：`reference_bindings` 是 JSONB 数组，
**没有任何 FK** ⇒ `IF NOT FOUND` 是**唯一防线**，是**可达的**
（测试里必须有一条反例走进去，且断言报 `23514`）。
这条区别是本节最容易被照抄错的地方 —— 照抄 WP26 的注释会写成
"防御性分支"，而那是**错的**（ISS-95 形态：把唯一防线说成冗余）。

### 为什么 3.1 与 3.2 合在一个函数里

它们**同属一列、同一次插入**（`media_versions` 的每一行都要同时满足），
且**修法是同一类**（改 `reference_bindings` 那一列）。
拆开只会让"这一行插入失败"出现两条同样的 `sqlstate=23514` 而无法区分。
与 WP26 把 3.1/3.2 合进 `content_version_consistency_ddl` 是同一取舍标准。

**为什么 3.3 单独一个函数**：挂的是另一张表（`media_generation_contexts`），
`NEW` 的列也不同。

**为什么全部用 `IS DISTINCT FROM` 而不是 `<>`**：本判据要比较的列
**全都可以为 NULL**（`media_version_id` 可空），写成 `<>` 时若任一操作数
为 NULL 会返回 NULL，`IF` 视为 false ⇒ **放行**。
`IS DISTINCT FROM` 是 NULL 安全的比较。这条与 `baseline/ddl.py` 第三节
"实现里最容易写错的一处"是**同一条纪律**（WP26 第三节末段同样写了）。

## 四、索引：本包**没有**部分唯一索引，但索引名仍须有唯一来源

`models.py` 里本模块声明了 **25** 条索引（17 条普通 + 5 条唯一，
逐表 `media_items` 6 / `reference_assets` 3 / `media_versions` 5 /
`media_renditions` 3 / `media_generation_contexts` 5；WP28 另加
`media_components` 2（含 1 条唯一）/ `live_profiles` 1（唯一））。
⚠️ 上面括号里的「17 + 5」是 WP27 的 22 条口径，**未随 WP28 的 3 条重算**，
以 `index_names()` 现场计数为准。
⚠️ 这两个数字**必须现场从 `index_names()` 数**，不能手抄：
2026-10-09 实测发现此处旧值写的是"21（16+5），逐表 5/5/3/3/5" ——
`media_items` 少算了一条（`uq_media_items_job_id`，由 WP27 的
`c4d9f2a17e35` 迁移新建）。docstring 里的计数**没有判据守着**
（判据核的是 `pg_indexes` 实际内容，不是这段注释的数字）⇒ 这类漂移只能靠
"改这个模块时跑一次 `index_names()` 对账"发现。与 WP24 不同，本包
**没有部分索引**（`WHERE` 子句），
因此不存在"部分索引的 `WHERE` 写错导致索引静默退化"那一类风险 ——
但这**不代表**索引可以两处各写一遍。

`index_names()` **从 ORM 元数据派生**（铁律 14：凡"覆盖范围"由人写，
就必须改由**磁盘**决定）。证据脚本按它点名核对 `pg_indexes`。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.media.models import IMMUTABLE_TABLES
from rednote.modules.media.types import (
    ASSET_STATE_ACTIVE,
    BINDING_KEY_ASSET_ID,
    COMPONENT_ROLE_STILL,
    MAX_REFERENCE_ASSETS,
    MEDIA_FORM_LIVE,
    MIN_REFERENCE_ASSETS,
)
from rednote.modules.versions.models import ENTITY_TYPE_MEDIA
from rednote.platform.immutability import immutable_trigger_ddl

# ---------------------------------------------------------------------------
# SQLSTATE 与名字
# ---------------------------------------------------------------------------

# PostgreSQL check_violation。与 WP11 的 `DEPENDENCY_SCOPE_SQLSTATE`、
# WP18/WP20/WP22/WP23/WP26 的 `CONSISTENCY_SQLSTATE` 同一个码：
# 语义上它都是"某条数据规则被违反了"，与本模块的 CHECK 同族，
# 都该映射到 422 VALIDATION_FAILED。
CONSISTENCY_SQLSTATE: Final[str] = "23514"

# --- OP-88：节点一致性（正向 + 反向） ---------------------------------------
MEDIA_VERSION_FUNCTION: Final[str] = "rednote_check_media_version_consistency"
MEDIA_VERSION_TRIGGER: Final[str] = "trg_media_versions_node_consistency"
MEDIA_NODE_FUNCTION: Final[str] = "rednote_check_media_node_paired"
MEDIA_NODE_TRIGGER: Final[str] = "trg_version_nodes_media_paired"

# --- 第 3 层：跨表链接 ------------------------------------------------------
BINDING_LINK_FUNCTION: Final[str] = "rednote_check_media_binding_consistency"
BINDING_LINK_TRIGGER: Final[str] = "trg_media_versions_binding_consistency"
CONTEXT_LINK_FUNCTION: Final[str] = "rednote_check_media_context_consistency"
CONTEXT_LINK_TRIGGER: Final[str] = "trg_media_generation_contexts_consistency"
# --- WP28（S2）：Live 组件与所属版本的一致性 ---------------------------------
COMPONENT_LINK_FUNCTION: Final[str] = "rednote_check_media_component_consistency"
COMPONENT_LINK_TRIGGER: Final[str] = "trg_media_components_consistency"


def _raise_block(message_expr: str) -> str:
    """生成一段 RAISE 语句（**字符串拼接，不用 `%`**，见 WP20 踩过的坑）。

    psycopg 的 paramstyle 会把 `%` 当参数占位符，未提供参数时直接报
    "unsupported format character"。字符串拼接没有这个坑。
    """
    return (
        "        RAISE EXCEPTION USING\n"
        f"            MESSAGE = {message_expr},\n"
        f"            ERRCODE = '{CONSISTENCY_SQLSTATE}';\n"
    )


def media_version_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    挂 `media_versions` AFTER INSERT，延迟到事务提交时执行。
    含 OP-88 的**正向**判据 1~3（模块 docstring 二）。

    **为什么正向与第 3 层拆成两个函数、而 WP26 合在一起**：
    WP26 把 OP-88 正向与两条跨表链接合进一个函数（它论证"五条同属一张表、
    修法是同一类"）。本包**拆开**，因为两类判据的**触发时机语义不同**：

    | | 判据 | 时机 |
    |---|---|---|
    | 正向 | 本行与 `version_nodes` 的配对 | `register_version` 之后、同一事务 |
    | 第 3 层 | 本行与 `media_items` / `reference_assets` 的**跨表**一致 | 同上，但依赖的是**别的表**的行 |

    拆开的收益是**失败信号可分辨**：插入一行失败时，两条触发器各报自己的
    消息，而 `RAISE` 里我们**刻意不依赖消息文本**（证据按 sqlstate 断言）——
    但排障的人读日志时能一眼看出是哪一类。
    代价是多一次函数调用 —— 可忽略。

    ⚠️ 这与 WP26"合在一起"并不矛盾：那里的五条**都是**"本行与别表一致"，
    没有"本行与注册表配对"这一类，故没有可分的维度。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {MEDIA_VERSION_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    node_entity_type text;\n"
        "    node_entity_id uuid;\n"
        "BEGIN\n"
        "    SELECT vn.entity_type, vn.entity_id\n"
        "      INTO node_entity_type, node_entity_id\n"
        "      FROM version_nodes vn WHERE vn.id = NEW.id;\n"
        "    IF NOT FOUND THEN\n"
        # FK 已保证存在（延迟 FK 在同一结算点先出手）⇒ 实测本条报 23503。
        # 保留只为"若将来有人把那条 FK 改成非延迟，本触发器语义不变"。
        + _raise_block(
            "'媒体版本 ' || NEW.id::text\n"
            "                      || ' 没有对应的 version_nodes 行（孤立版本）'"
        )
        + "    END IF;\n"
        "    IF node_entity_type IS DISTINCT FROM '"
        + ENTITY_TYPE_MEDIA
        + "' THEN\n"
        + _raise_block(
            "'媒体版本 ' || NEW.id::text || ' 对应的节点 entity_type 是 '\n"
            "                      || COALESCE(node_entity_type, '(NULL)')\n"
            f"                      || '，应为 ''{ENTITY_TYPE_MEDIA}'''"
        )
        + "    END IF;\n"
        "    IF node_entity_id IS DISTINCT FROM NEW.media_item_id THEN\n"
        + _raise_block(
            "'媒体版本 ' || NEW.id::text || ' 属于媒体位 '\n"
            "                      || NEW.media_item_id::text\n"
            "                      || '，但它注册的节点 entity_id 是 '\n"
            "                      || COALESCE(node_entity_id::text, '(NULL)')\n"
            "                      || '（溯源与影响图按 entity_id 聚合，错值会画到别的媒体位上）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {MEDIA_VERSION_TRIGGER}\n"
        "AFTER INSERT ON media_versions\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW EXECUTE FUNCTION {MEDIA_VERSION_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {MEDIA_VERSION_TRIGGER}"
        " ON media_versions"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {MEDIA_VERSION_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def media_node_paired_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    OP-88 的**反向**判据："一个 `MEDIA` 节点必须配对一条 `media_versions` 行"。
    **本包收窄**（与 WP18/WP20/WP22/WP23/WP26 同一处理，理由见模块 docstring 二）。

    **挂 `version_nodes` 是刻意的**：这个不变量是"注册表侧不许出现
    没有领域行的节点"，它的检查点只能是被插入的那个节点。
    挂在 `media_versions` 上做不到 —— 那时节点可能还没插入。

    **与 WP18/WP20/WP22/WP23/WP26 的 `*_paired` 在同一张表上并存**：
    六个触发器各自被 `WHEN` 收窄到自己的 `entity_type`，互不影响。
    这是"共用表上按实体类型分区加约束"的标准做法。

    ## ★★★ 两道判断的顺序不能换（ISS-175 —— 本文件最要紧的一处）

    1. **先判"归不归我管"**（`media_items` 里有 `id = entity_id` 的行）——
       没有就 `RETURN NULL` 放行。这一道是**跨包契约**：`version_nodes`
       是共用表，WP11 的通用用例（`test_versions_wp11.py:292` 实测把
       `MEDIA` 当样例用）会注册 `MEDIA` 节点而没有对应的媒体身份行。
    2. **再判"配对是否存在"**（`media_versions` 里有 `id = NEW.id` 的行）——
       没有就 `RAISE`。

    ⚠️ **WP26 的第一版漏了第 1 道**，实测直接破坏 WP11 既有 2 例
    （本包文档 §1.6 R8 把它列为"最容易漏的一条"）。漏掉的表现是
    **本包测试全绿、别的包的测试红** —— 而那时排障的人会先怀疑别的包。
    """

    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {MEDIA_NODE_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "BEGIN\n"
        # ★ 收窄：只对"真实存在的媒体身份"强制。这一道是跨包契约，
        # 不是可选的优化 —— `version_nodes` 是共用表，别的包
        # （WP11 通用用例 / 将来的包）会注册 MEDIA 节点而没有
        # 对应的 `media_items` 行。去掉它 = 直接破坏 WP11 既有用例（ISS-175）。
        # 顺序也不能换：必须先放行"不归我管"的，再判配对。
        "    IF NOT EXISTS (\n"
        "        SELECT 1 FROM media_items WHERE id = NEW.entity_id\n"
        "    ) THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "    IF NOT EXISTS (\n"
        "        SELECT 1 FROM media_versions WHERE id = NEW.id\n"
        "    ) THEN\n"
        + _raise_block(
            "'版本节点 ' || NEW.id::text\n"
            "                      || ' 声明属于媒体位 ' || NEW.entity_id::text\n"
            "                      || '，但没有任何 media_versions 行（孤立节点）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {MEDIA_NODE_TRIGGER}\n"
        "AFTER INSERT ON version_nodes\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW WHEN (NEW.entity_type = '{ENTITY_TYPE_MEDIA}')\n"
        f"EXECUTE FUNCTION {MEDIA_NODE_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {MEDIA_NODE_TRIGGER} ON version_nodes"
    drop_function = f"DROP FUNCTION IF EXISTS {MEDIA_NODE_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def media_binding_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    第 3 层防线的 3.1 + 3.2（模块 docstring 三）：
    `reference_bindings` 的**每个元素**必须指向一条**真实存在**
    且**属于同一项目**的 `reference_assets` 行。

    ## ★ 这两条是 JSONB 数组的**唯一防线**（与 WP26 的 3.1/3.2 相反）

    WP26 的 3.1/3.2 那两条 FK 是 IMMEDIATE ⇒ 它们的 `IF NOT FOUND`
    实测**不可达**，保留只为将来。**本包不同**：`reference_bindings`
    是 JSONB，**没有任何 FK** ⇒ 这里不是冗余，是**唯一能拦住的地方**。
    照抄 WP26 的注释会把本判据写成"防御性分支" —— 那是**错的**
    （ISS-95 的反向形态：把唯一防线说成冗余）。

    ## 为什么用 `jsonb_array_elements` 而不是在 Python 里逐条查

    走 Python 就要**每个元素一次往返**（1~8 条绑定 ⇒ 最多 8 次查询），
    且那只在你走服务层时有效。放这里：
      · 一次 JOIN 就拿全（`jsonb_array_elements` 是集合返回函数）；
      · 数据导入脚本 / 迁移回填 **也必须过这一关**。

    ## `MIN/MAX_REFERENCE_ASSETS` 在函数体里只做**防御性**核对

    形状与数量区间已由 `models.py` 的 CHECK 拦下（`bindings_size`）。
    这里仍然读一遍 `jsonb_array_length` 并拒掉区间外的值 ——
    因为**本函数至少要能独立回答"这列是不是有 1~8 条合法绑定"**，
    否则它就是一个"假设上游已经判过"的函数（而假设上游，正是
    "声称有防线"的典型形态）。**两个常量的唯一来源是 `types.py`**，
    这里 import 进来用，**不写第二份字面量**。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {BINDING_LINK_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    binding_count integer;\n"
        "    missing_asset uuid;\n"
        "    foreign_asset uuid;\n"
        "    own_project uuid;\n"
        "BEGIN\n"
        f"    binding_count := jsonb_array_length(NEW.reference_bindings);\n"
        f"    IF binding_count < {MIN_REFERENCE_ASSETS}"
        f" OR binding_count > {MAX_REFERENCE_ASSETS} THEN\n"
        + _raise_block(
            "'媒体版本 ' || NEW.id::text || ' 的参考素材绑定数 '"
            "\n                      || binding_count::text || ' 不在合法区间 "
            f"[{MIN_REFERENCE_ASSETS}, {MAX_REFERENCE_ASSETS}]"
            "'\n"
            "                      || '（0 条 = 看起来有绑定实际没绑；"
            "过多 = 整库引用，AQ-03 定位不到型号）'"
        )
        + "    END IF;\n"
        "\n"
        # 本媒体位属于哪个项目 —— 一次取，后面逐条比较复用。
        "    SELECT mi.project_id INTO own_project\n"
        "      FROM media_items mi WHERE mi.id = NEW.media_item_id;\n"
        "    IF NOT FOUND THEN\n"
        # 非空 FK（IMMEDIATE）已保证存在；防御性分支。
        + _raise_block(
            "'媒体版本 ' || NEW.id::text || ' 的媒体位 '\n"
            "                      || NEW.media_item_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        "\n"
        # ★ 3.1：每一条绑定的 assetId 必须指向一条真实存在的素材。
        #   用 LEFT JOIN + IS NULL 一次找出**第一条**缺失的（报一条即可，
        #   目的是给出"是哪个 id"这种可操作的信息，不是枚举全部）。
        #
        #   ⚠️ 键名必须由 `BINDING_KEY_ASSET_ID` 插值（不得手写字面量）：
        #   本函数住在 plpgsql 字符串里，import 不到常量，因此这里是
        #   **同一个键名的第三个消费点**（另两个在 `types.py` / `schemas.py`）。
        #   三处各写一遍的表现是静默的 —— 写侧 `asset_id`、本处 `assetId`
        #   时，`elem->>'assetId'` 恒为 NULL ⇒ `missing_asset IS NULL` ⇒
        #   **本触发器全绿**，而绑定实际指向空气（详见 `types.py` 的常量注释）。
        f"    SELECT (elem->>'{BINDING_KEY_ASSET_ID}')::uuid INTO missing_asset\n"
        "      FROM jsonb_array_elements(NEW.reference_bindings) AS elem\n"
        "      LEFT JOIN reference_assets ra\n"
        f"             ON ra.id = (elem->>'{BINDING_KEY_ASSET_ID}')::uuid\n"
        "     WHERE ra.id IS NULL\n"
        "     LIMIT 1;\n"
        "    IF missing_asset IS NOT NULL THEN\n"
        + _raise_block(
            "'媒体版本 ' || NEW.id::text || ' 绑定的参考素材 '\n"
            "                      || missing_asset::text\n"
            "                      || ' 不存在（JSONB 数组元素没有引用完整性，"
            "本触发器是唯一防线）'"
        )
        + "    END IF;\n"
        "\n"
        # ★ 3.2：绑定的素材必须与图属于**同一个项目**。
        "    SELECT ra.id INTO foreign_asset\n"
        "      FROM jsonb_array_elements(NEW.reference_bindings) AS elem\n"
        f"      JOIN reference_assets ra ON ra.id = (elem->>'{BINDING_KEY_ASSET_ID}')::uuid\n"
        "     WHERE ra.project_id IS DISTINCT FROM own_project\n"
        "     LIMIT 1;\n"
        "    IF foreign_asset IS NOT NULL THEN\n"
        + _raise_block(
            "'媒体版本 ' || NEW.id::text || ' 绑定的参考素材 '\n"
            "                      || foreign_asset::text\n"
            "                      || ' 属于别的项目（AQ-03 按项目盲审，"
            "跨项目引用会让别的项目的产品静默混入）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {BINDING_LINK_TRIGGER}\n"
        "AFTER INSERT ON media_versions\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW EXECUTE FUNCTION {BINDING_LINK_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {BINDING_LINK_TRIGGER} ON media_versions"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {BINDING_LINK_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def media_context_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    第 3 层防线的 3.3（模块 docstring 三）：`media_generation_contexts` 引用的
    媒体版本必须属于**本行的 `content_item_id`**。

    **跨两级核对**（`media_version_id` → `media_versions.media_item_id` →
    `media_items.content_item_id`）—— 因为上下文行上**没有** `media_item_id`
    列。为什么不加那一列的理由见模块 docstring 三末段（铁律 26）。

    `media_version_id IS NULL` 时直接放行：**首次生成**时，"这一版是哪次
    调用产出的"这条链接的方向是"版本 → 上下文"，而"上下文 → 版本"那一侧
    要等版本行落地（`models.py` docstring 九）。这是**合法的常态**，
    不是漏判 —— 若对这个 NULL 也要求配对，首次生成的上下文行就永远
    留不下来，于是"这张图是哪版提示词生成的"永远查不到，
    而那正是 OP-160 要回答的问题。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {CONTEXT_LINK_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    mv_item uuid;\n"
        "    mv_content uuid;\n"
        "BEGIN\n"
        "    IF NEW.media_version_id IS NULL THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        "\n"
        "    SELECT mv.media_item_id INTO mv_item\n"
        "      FROM media_versions mv WHERE mv.id = NEW.media_version_id;\n"
        "    IF NOT FOUND THEN\n"
        # FK 已保证存在；走到这里只有一种可能：有人在同一事务里删了那行 ——
        # 而 media_versions 不可变（删不掉）。防御性分支。
        + _raise_block(
            "'媒体生成上下文 ' || NEW.id::text || ' 引用的媒体版本 '\n"
            "                      || NEW.media_version_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        "\n"
        "    SELECT mi.content_item_id INTO mv_content\n"
        "      FROM media_items mi WHERE mi.id = mv_item;\n"
        "    IF NOT FOUND THEN\n"
        + _raise_block(
            "'媒体生成上下文 ' || NEW.id::text || ' 引用的媒体版本所属的媒体位 '\n"
            "                      || mv_item::text || ' 不存在'"
        )
        + "    END IF;\n"
        "\n"
        "    IF mv_content IS DISTINCT FROM NEW.content_item_id THEN\n"
        + _raise_block(
            "'媒体生成上下文 ' || NEW.id::text || ' 声明属于内容 '\n"
            "                      || NEW.content_item_id::text\n"
            "                      || '，但它引用的媒体版本属于内容 '\n"
            "                      || COALESCE(mv_content::text, '(未知)')\n"
            "                      || '（OP-160 的影响面按内容归组，错值会算到别的内容上）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {CONTEXT_LINK_TRIGGER}\n"
        "AFTER INSERT ON media_generation_contexts\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW EXECUTE FUNCTION {CONTEXT_LINK_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {CONTEXT_LINK_TRIGGER}"
        " ON media_generation_contexts"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {CONTEXT_LINK_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def media_component_consistency_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    WP28 的 S2：`media_components` 与它所属 `media_versions` 的两条跨表不变量 ——

    1. **只有 `LIVE` 版本才有组件**：`media_form <> 'LIVE'` 的版本挂组件 = 静态图
       冒充 Live（或反过来），"这一版是什么形态"会有两个答案。
    2. **`STILL` 组件的 `file_id` 必须等于版本的 `image_file_id`**：原图只有一个落点
       （老大 2026-10-14 Q2 去掉 `ORIGINAL` 的前提）。不兜住它，同一张静帧会存成
       两个文件，`image_file_id` 与 STILL 悄悄分叉。

    ## 为什么是延迟触发器、而不是 CHECK

    两条都要读**别的表的行**，CHECK 做不到；与本包其它三条一致性触发器同形
    （`AFTER INSERT … DEFERRABLE INITIALLY DEFERRED`）。

    ## 为什么 `IS DISTINCT FROM`

    NULL 安全：`<>` 遇 NULL 返回 NULL、`IF` 视为 false ⇒ **放行**，这正是
    "看起来有防线"的形态（`baseline/ddl.py` 第三节同款）。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {COMPONENT_LINK_FUNCTION}()"
        " RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    v_form text;\n"
        "    v_image uuid;\n"
        "BEGIN\n"
        "    SELECT mv.media_form, mv.image_file_id INTO v_form, v_image\n"
        "      FROM media_versions mv WHERE mv.id = NEW.media_version_id;\n"
        "    IF NOT FOUND THEN\n"
        # FK 非延迟，当场就报 23503；本分支不可达，保留只为将来有人改 FK。
        + _raise_block(
            "'媒体组件 ' || NEW.id::text || ' 引用的媒体版本 '\n"
            "                      || NEW.media_version_id::text || ' 不存在'"
        )
        + "    END IF;\n"
        "\n"
        f"    IF v_form IS DISTINCT FROM '{MEDIA_FORM_LIVE}' THEN\n"
        + _raise_block(
            "'媒体组件 ' || NEW.id::text || ' 挂在形态为 '\n"
            "                      || COALESCE(v_form, '(NULL)')\n"
            f"                      || ' 的版本上，只有 {MEDIA_FORM_LIVE} 版本才有组件'"
        )
        + "    END IF;\n"
        "\n"
        f"    IF NEW.role = '{COMPONENT_ROLE_STILL}'\n"
        "       AND NEW.file_id IS DISTINCT FROM v_image THEN\n"
        + _raise_block(
            "'媒体组件 ' || NEW.id::text || ' 是 STILL，但 file_id '\n"
            "                      || NEW.file_id::text\n"
            "                      || ' 不等于版本的 image_file_id '\n"
            "                      || COALESCE(v_image::text, '(NULL)')\n"
            "                      || '（原图只有一个落点）'"
        )
        + "    END IF;\n"
        "    RETURN NULL;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE CONSTRAINT TRIGGER {COMPONENT_LINK_TRIGGER}\n"
        "AFTER INSERT ON media_components\n"
        "DEFERRABLE INITIALLY DEFERRED\n"
        f"FOR EACH ROW EXECUTE FUNCTION {COMPONENT_LINK_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {COMPONENT_LINK_TRIGGER} ON media_components"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {COMPONENT_LINK_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块的**跨表一致性**触发器（OP-88 第五半 + 三条链接不变量）。

    **为什么公开（去掉下划线）**：迁移的 `downgrade` 需要**只删本模块自己的
    函数**（`rednote_check_media_*`），而 `all_trigger_ddl()` 里同时混着
    不可变保护那 4 项 —— 它们的 `drop_function` 指向**跨包共用**的
    `rednote_reject_mutation()`，删了会撞依赖错误并让
    WP11/WP15/WP16/WP17/WP18/WP20/WP22/WP23/WP24/WP25/WP26 的保护一起失效
    （ISS-79 的教训：`versions/ddl.py` 的 `all_trigger_ddl()` 第 5 项
    混着共用函数，`downgrade` 直接遍历就会误删）。

    因此 downgrade 必须能单独拿到"一致性那 4 项"。
    判据：**这个对象是不是我建的** —— 是就删，不是就不删。

    ⚠️ **这个列表的长度是判据的一部分**：它与 `consistency_trigger_names()` /
    `consistency_function_names()` 必须**等长**（各 4 项）。收口时由
    `check_evidence_scripts.py` 与测试双向核对 —— 数错一个的后果是
    `downgrade` 漏删一个函数（残留对象会挡住后续迁移重建它）。
    """
    items: list[tuple[str, str, str, str, str]] = []
    for table, builder in (
        ("media_versions", media_version_consistency_ddl),
        ("version_nodes", media_node_paired_ddl),
        ("media_versions", media_binding_consistency_ddl),
        ("media_generation_contexts", media_context_consistency_ddl),
        ("media_components", media_component_consistency_ddl),
    ):
        function_ddl, trigger_ddl, drop_trigger, drop_function = builder()
        items.append((table, function_ddl, trigger_ddl, drop_trigger, drop_function))
    return items


def all_trigger_ddl() -> list[tuple[str, str, str, str, str]]:
    """本模块需要建立的全部触发器。

    返回 `[(表名, 函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP), …]`。

    **顺序有意义**：函数 DDL 用 `CREATE OR REPLACE`，多张表共用同一个
    不可变函数，重复执行是安全的（幂等）；触发器 DROP 也必须先于函数 DROP，
    否则函数仍被触发器引用，PG 会拒绝删除。

    **注意 `version_nodes` 也在列表里**：`trg_version_nodes_media_paired`
    是本模块为了闭合 OP-88 第五半而挂到**共用注册表**上的触发器。
    它不修改 WP11 的任何代码或表结构 —— 它只是 `version_nodes` 上的
    一个新触发器对象，且被 `WHEN (NEW.entity_type = 'MEDIA')` 限定到
    本模块负责的实体类型。**落迁移时必须把它一并建出**，
    否则 OP-88 第五半只闭合了一半（正向判据有、反向判据没有）。

    **刻意把不可变保护排在前面**：让"不可变保护先建、业务不变量后建"
    在读日志时可见。（`CREATE CONSTRAINT TRIGGER` 在 PG 里不做依赖检查 ——
    plpgsql 函数体在首次调用时才解析，因此顺序其实是安全的。）
    """
    items: list[tuple[str, str, str, str, str]] = []
    for table in IMMUTABLE_TABLES:
        function_ddl, trigger_ddl, drop_trigger, drop_function = (
            immutable_trigger_ddl(table)
        )
        items.append((table, function_ddl, trigger_ddl, drop_trigger, drop_function))

    items.extend(consistency_items())
    return items


def immutable_trigger_names() -> tuple[str, ...]:
    """本模块的不可变触发器名（**不含**共用函数名）。"""
    return tuple(f"trg_{table}_immutable" for table in IMMUTABLE_TABLES)


def consistency_trigger_names() -> tuple[str, ...]:
    """本模块的跨表一致性触发器名。"""
    return (
        MEDIA_VERSION_TRIGGER,
        MEDIA_NODE_TRIGGER,
        BINDING_LINK_TRIGGER,
        CONTEXT_LINK_TRIGGER,
        COMPONENT_LINK_TRIGGER,
    )


def consistency_function_names() -> tuple[str, ...]:
    """本模块**自己的**函数名（downgrade 只删这些）。

    **不含** `rednote_reject_mutation` —— 它属于 `platform`，被全项目共用。
    """
    return (
        MEDIA_VERSION_FUNCTION,
        MEDIA_NODE_FUNCTION,
        BINDING_LINK_FUNCTION,
        CONTEXT_LINK_FUNCTION,
        COMPONENT_LINK_FUNCTION,
    )


def immutable_function_names() -> tuple[str, ...]:
    """本模块**建立**的函数名。只有共用那一个 —— 且它是**跨包共用**的，
    `downgrade` 不许删（删了会让其它 10 个包的保护一起失效）。
    """
    return ("rednote_reject_mutation",)


def trigger_names() -> tuple[str, ...]:
    """全部应存在的触发器名（验证脚本与测试用，**点名核对、不数个数**）。

    **为什么不数个数**：`version_nodes` 上同时挂着 WP18/WP20/WP22/WP23/WP26
    与本包的**六个** `*_paired` 触发器，数个数会把别包的算进来或把自己的漏掉。
    点名核对才能给出"缺失集合为空集"这种可断言的结论。
    """
    return immutable_trigger_names() + consistency_trigger_names()


def index_names() -> tuple[str, ...]:
    """本模块声明的全部索引名，**从 ORM 元数据派生**（铁律 14）。

    为什么派生而不是手写清单：手写清单会漏（漏了**不报错**，
    只是"索引存在性核对"少查一条，于是那条索引可以静默消失）。
    派生自 `__table__.indexes` 之后，`models.py` 是唯一来源。

    **不含主键索引**：`pk_*` 由主键约束隐含创建，不在
    `__table__.indexes` 里，也不由本模块显式声明。
    证据脚本核对的是**本模块显式声明的**那 22 条（2026-10-09 现场数出；
    旧注释写 21，见模块 docstring 第四节对这次漂移的说明）。

    ⚠️ **一个近似的老化风险**（与 WP26 的同一段注释逐字同款）：
    本函数返回的是"模型里现在有哪些索引"，而证据脚本会拿它与迁移建出的
    索引对账。若某天有人在 `models.py` 里删了一条索引但忘了改迁移，
    **两处都会少一条** —— 对账看到的是"双方一致"，看不到"少了一条"。
    挡住它的是 `check_migration_model_parity.py`（它比的是
    **迁移库 ⟷ 模型库**，不是本函数 ⟷ 迁移）。
    本函数的职责只是"给证据脚本一份不手写的名字清单"。
    """
    from rednote.modules.media.models import (
        LiveProfile,
        MediaComponent,
        MediaGenerationContext,
        MediaItem,
        MediaRendition,
        MediaVersion,
        ReferenceAsset,
    )

    names: set[str] = set()
    for model in (
        MediaItem,
        MediaVersion,
        ReferenceAsset,
        MediaRendition,
        MediaGenerationContext,
        MediaComponent,
        LiveProfile,
    ):
        for index in model.__table__.indexes:
            names.add(index.name)
    return tuple(sorted(names))


def active_asset_states() -> tuple[str, ...]:
    """本模块视为"素材当前可用"的状态集合。

    ⚠️ **注意这是"登记动作"的状态，不是"当前可用性"**（`models.py`
    docstring 五）：一个 `ACTIVE` 的行**可能已经被别的 `RETIRED` 行
    supersede 掉了**。因此真正的"某素材当前可用吗"**不能**只看本函数 ——
    它要**两列合起来**回答，而那件事的唯一落点是 `repository.py` 的
    单一函数（`REASON_REFERENCE_ASSET_NOT_ACTIVE`）。

    本函数存在只是为了给那个函数一个**不手写状态名**的入口 ——
    与 `index_names()` 同一动机。**它不是**"可用素材的判据"，
    任何调用方都不该拿它直接过滤。
    """
    return (ASSET_STATE_ACTIVE,)


__all__ = [
    "BINDING_LINK_FUNCTION",
    "BINDING_LINK_TRIGGER",
    "CONSISTENCY_SQLSTATE",
    "COMPONENT_LINK_FUNCTION",
    "COMPONENT_LINK_TRIGGER",
    "CONTEXT_LINK_FUNCTION",
    "CONTEXT_LINK_TRIGGER",
    "MEDIA_NODE_FUNCTION",
    "MEDIA_NODE_TRIGGER",
    "MEDIA_VERSION_FUNCTION",
    "MEDIA_VERSION_TRIGGER",
    "active_asset_states",
    "all_trigger_ddl",
    "consistency_function_names",
    "consistency_items",
    "consistency_trigger_names",
    "immutable_function_names",
    "immutable_trigger_names",
    "index_names",
    "media_binding_consistency_ddl",
    "media_component_consistency_ddl",
    "media_context_consistency_ddl",
    "media_node_paired_ddl",
    "media_version_consistency_ddl",
    "trigger_names",
]