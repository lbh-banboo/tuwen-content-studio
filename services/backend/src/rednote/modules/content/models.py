"""WP26 五张表的存储模型。

| 表 | 角色 | 可变性 |
|---|---|---|
| `content_items` | 内容条目**身份**（一项目一条内容，跨版本不变） | **可变**（`lock_version` + 当前有效版本投影） |
| `content_versions` | 内容**版本**（标题 / 正文 / 标签 / 采纳的标题候选 / 证据绑定） | **不可变** |
| `narrative_blueprints` | **叙事蓝图**（9 字段结构），生成正文的输入 | **不可变** |
| `title_candidates` | 标题候选（每批 N 个 + 6 维评分 + 关键词/吸引点/风险说明） | **不可变** |
| `generation_contexts` | 生成上下文（**OP-160 落点**） | **不可变** |

---

## 一、为什么"采纳标题"记在 `content_versions` 上

说明文档 §1.7 已给过三条理由（表可变性 / 一处可查 / 采纳两次不冲突）。
这里补一条**结构性的**：给 `title_candidates` 加 `adopted` 列会让它变成
**可变表**，而它的不可变触发器就得开一个例外（"只许改 adopted 列"）。
"只许改某一列"的触发器不好写（要在 BEFORE UPDATE 里逐列比对），
而写错的后果是**候选历史可以被整行改写**——那是一张
"看起来不可变、实际有一扇后门"的表。

与 WP25 的"账面 / 明细分表"是**同一种判断**：
把"历史事实"与"当前选择"分开，历史表不可变，选择只出现在新的版本行上。

## 二、`content_versions.id` 与 `version_nodes.id` 同值；`entity_id` 是**内容身份**

本仓有个容易混的地方：`version_nodes` 有两个 UUID 列——

- `version_nodes.id` —— **这次版本**的节点 id；
- `version_nodes.entity_id` —— **逻辑实体**的身份 id（跨版本不变）。

对 `CONTENT`：

    version_nodes.id        = content_versions.id      （本行就是那个版本）
    version_nodes.entity_id = content_versions.content_item_id  （内容身份）

**这两个值不同**，因此反向触发器能区分"节点 vs 领域行"，
不存在"两边是同一个 UUID、分不清谁对谁"的问题
（`BASELINE` 是个特例：它的 `entity_id` 取 `project_id`，因为一项目一条基线链）。

## 三、★ 蓝图**不挂** `version_nodes`（对说明文档 §1.7 的一处偏离）

说明文档 §1.7 的表格写 `narrative_blueprints` 的 "`id` 同时 FK `version_nodes.id`"。
**本实现不这么做**，理由如下（已登记为待回填项）：

`ENTITY_TYPE_CONTENT` 只有一个取值。若蓝图也挂 `version_nodes`，
则同一个 `(entity_type='CONTENT', entity_id=<内容身份>)` 下会有
**两条互相干扰的 `revision_no` 序列**（内容版本一条、蓝图一条），
而 `UQ(entity_type, entity_id, revision_no)` 会让它们必须共享同一个序列 ——
于是"内容的第 3 版"这句话**有两个答案**（是内容版本 v3，还是蓝图 v3？）。
这正是本仓反复现身的"同一事实的两种写法"（ISS-137 的判据纪律第 4 条）。

**正确的落法是**：蓝图是内容版本的**组成部分**，由
`content_versions.blueprint_id` 指向（FK + 蓝图的不可变触发器）。
它的"版本语义"由**所属的内容版本**承载 —— 一次 `FULL` 重做同时
产生一条新蓝图与一条新内容版本，两者在同一条版本链上。

**保住了什么**：蓝图仍然**不可变**、仍然可单独查
（`plan-detail.html` 那一屏要的"这 10 条的蓝图分别是什么"照样能查）、
9 个字段仍然各有列级 CHECK。
**放弃了什么**：蓝图不再是一个**独立可寻址的版本节点**。
若将来确有"只回滚蓝图不回滚正文"的需求，那需要给 `entity_type` 加第 9 个取值
（那是**扩大受控取值域**，须走协议与版本的显式变更，不在本包）。

## 四、★ OP-160：`generation_contexts.prompt_version_id` 必须有索引

OP-160 要的能力是"**改了提示词之后，哪些历史生成受影响**"。
那是一次按 `prompt_version_id` 的反查 —— 没有索引它就要全表扫，
而这张表是**每次生成一行**、只增不减的。

因此本表的索引不是性能优化，而是**那个能力能不能成立的前提**
（说明文档 §3 V2 方案被否决的第 3 条理由就是"JSONB 列里的
`prompt_version_id` 无法建索引 ⇒ OP-160 落不了"）。

同理建了 `prompt_template_id` 的索引：OP-160 的粗粒度问法是
"改了**这个模板**"，而模板下的版本有多个。

## 五、`content_items.current_version_id` 是**投影**，且它这一侧必须延迟

`content_items` 与 `content_versions` 互相 FK（内容指向当前版本、
版本指向所属内容）。**但两侧的可空性不对称，因此只延迟一侧**：

| 列 | 可空 | 处置 |
|---|---|---|
| `content_items.current_version_id` | 是 | ✅ `deferrable=True, initially="DEFERRED"` |
| `content_versions.content_item_id` | **否** | ⛔ 保持 IMMEDIATE |

理由见第七节「为什么环 1 的 `content_item_id` **必须**留 IMMEDIATE」——
一句话：延迟可空的那一侧是为了允许"先建身份、后建版本"，
而延迟非空的那一侧只会**额外放行孤儿版本行**，没有任何合法流程需要它。

⚠️ **不要照抄 `brief/models.py` 的注释**：它在 `briefs.draft_id` 那行写着
"两者都延迟"，但实测 `fk_brief_drafts_brief_id_briefs` 是
`deferrable=false`（2026-10-06 查 `rednote_wp26` 的 `pg_constraint`）——
**范本注释写错了**，代码是对的。本包按实测口径写，不按注释口径。

**为什么保留这个投影列而不是每次 `ORDER BY version_no DESC LIMIT 1`**：
列表页（`GET /projects/{id}/contents`）每一行都要显示当前版本号 ——
不保留投影，这个端点会变成 N+1 次查询，而列表页是最常打开的页面。
代价是"投影可能漂移"，处置与 WP20 的 `_project_asset` 同一手法：
**投影只由服务层在追加版本时改写**，且 `test_content_wp26.py` 里有一条
"追加版本后投影必须前进"的用例。

## 六、★「采纳标题」= **追加一版**，不是 UPDATE

这一条不写下来，写 `service.py` 的人一定会去写 `UPDATE content_versions
SET adopted_title_candidate_id = …` —— 然后撞上不可变触发器，且**报错发生在
运行时**（DDL 层看不出来）。

### 为什么不能就地 UPDATE

采纳是一个**独立端点**（`POST /contents/{id}/title-adoptions`，说明文档 §1.7），
它发生在生成之后、运营看过候选之后。那一刻被采纳候选所属的那一行
`content_versions` **早已存在**，而 `content_versions` 是**不可变**的。

### 三条出路，为什么选第三条

| 出路 | 代价 |
|---|---|
| ① 就地 UPDATE（破不可变） | 一次生成的产物可被事后改写 ⇒ AQ-01 盲审与 WP30 自检的事实依据不再可靠 |
| ② 给触发器开"只许改 `adopted_title_candidate_id` 这一列"的例外 | 与本文档开篇**同一条理由被自己推翻**：加了 adopted 列就要开这个例外，我们正是因此否决了给候选表加列 |
| ③ **采纳 = 追加一版**（新版本行带着 `adopted_title_candidate_id`） | 版本链里多出"正文没变、只换了采纳标题"的版本 —— 但它与「仅标题重做」**同形**（都是一版），列表页照样显示，没有新增形态 |

选 ③。依据是说明文档 §1.7 表格里的**两句话**：

> 「**一处** —— 内容版本的 payload 里直接有 `adopted_title_candidate_id`」
> 「采纳两次 → **不同版本各记各的**，天然不会冲突」

后一句已经明说了采纳是**按版本记**的，而版本行不可变 ⇒ 只能是新版本。

### 由此得到的落库规则（`service.py` 必须照做）

- **生成产生的版本行**：`adopted_title_candidate_id` **恒为 NULL**；
- **采纳端点产生的版本行**：`title` 换成被采纳候选的标题，
  `body` / `tags` / `bindings` / `blueprint_id` / `generation_context_id`
  **原样复用上一版**（没有模型调用，因此**没有**新的 `generation_contexts` 行
  —— 这也解释了 `content_versions.generation_context_id` 为什么可空）；
- 两个动作都让 `content_items.current_version_id` 前进（第五节）。

### 由此产生一条跨表不变量（`ddl.py` 承担）

被采纳的候选，其所属版本必须与**本行**属于**同一条内容**。
`adopted_title_candidate_id → title_candidates.id` 这条 FK **管不住**它 ——
FK 只保证"这个候选存在"，不保证"它属于我这条内容"。
不同内容之间互相采纳是**语义错误**（采纳了别人家的标题，而 AQ-01
要按内容算事实正确率）。该约束是跨表的，DDL 层的 FK 表达不了，
落在 `ddl.py` 的触发器里。

### 与「仅标题重做」（`REGENERATION_SCOPES` 的 `TITLE`）的区别

两者**都 +1 版**，区别在于**要不要调用模型**：
`TITLE` 重做要派发一次 `CONTENT_COPY` 产出**一批新候选**；
采纳是在**已有候选**里挑一个，不派发、不花钱。
因此采纳端点的计费路径是**空**的（`billing_bridge` 不参与），
这一点在 `api.py` 的 docstring 里也要写。

## 七、★ 本模块有**两个**循环外键，延迟其中 **3 条**（不是 4 条）

本模块出现了两处"两张表互相 FK"：

| # | 环 | 两条 FK | 各自处置 |
|---|---|---|---|
| 1 | `content_items` ↔ `content_versions` | `content_items.current_version_id` | ✅ **延迟** |
|   |   | `content_versions.content_item_id` | ⛔ **保持 IMMEDIATE**（见下） |
| 2 | `content_versions` ↔ `generation_contexts` | `content_versions.generation_context_id` | ✅ **延迟** |
|   |   | `generation_contexts.content_version_id` | ✅ **延迟** |

⇒ **延迟的是 3 条，不是 4 条。**（2026-10-06 私有库 `rednote_wp26` 查 `pg_constraint` 实测，
逐条见 `/tmp/wp26_cycle_probe_v3.py` 的输出段 `[0]`。）

### ★ 为什么环 1 的 `content_item_id` **必须**留 IMMEDIATE

这里有个陷阱：看到"两处环，处置相同"就想四条一起加延迟 —— **那会削弱防线**。

`content_versions.content_item_id` 是**非空**的（每条版本必须属于某条内容），
而 `content_items.current_version_id` **可空**。两者的可空性不对称，
能解开的环也就只有一半：

* `content_items.current_version_id` 延迟 ⇒ 允许"**先建身份、后建版本**"
  （身份行先带着一个尚不存在的版本 id，COMMIT 期版本行补上）；
* `content_versions.content_item_id` 若也延迟 ⇒ 就**额外**允许
  "先插一条孤儿版本行、再补身份"。而"版本必须属于已存在的内容"
  是本模块最基本的引用完整性，**没有任何合法流程需要这个顺序**
  （`service.py` 一律先有内容才有版本）⇒ 延迟它只是把一条真实错误
  从"当场拒绝"推迟到"COMMIT 时才拒"，**没有任何收益，只有损失**。

实测（v3 探针 B2 用例）：保持 IMMEDIATE 时，"版本先于身份"在 INSERT
那一刻就报 `23503` —— 这是**期望行为**，是"我们没有过度延迟"的**正向证据**。
（v2 探针把 B2 当正例，于是把正确行为读成了缺陷，那段弯路记在此处以免重走。）

### 第 2 个环是本包编码期实测发现的（原设计漏了）

第一版的 `content_versions.generation_context_id` 与
`generation_contexts.content_version_id` 都是**默认的 IMMEDIATE**，
而两张表**都不可变**（改不了、删不掉）⇒ `TASK_CONTENT_COPY` 的
**主路径根本走不通**。实测两种插入顺序都失败：

| 顺序 | 结果 |
|---|---|
| 先插 `generation_contexts`（`content_version_id` 指向尚未存在的版本） | `23503` —— IMMEDIATE FK 在 INSERT 那一刻就要求被引用行存在 |
| 先插 `content_versions`，再插 `generation_contexts`，然后回填 `generation_context_id` | 回填 = `UPDATE content_versions` ⇒ 撞**不可变触发器**（`23001`） |

⇒ 没有第三条路：**只能让 FK 延迟**。

### 为什么"不可变的循环引用"不是死结

延迟之后插入顺序不再重要，在 **COMMIT 期**两侧 FK 一起结算：

    ① INSERT content_versions (generation_context_id = <ctx>)   ← FK 挂起
    ② INSERT generation_contexts (content_version_id = <ver>)   ← FK 挂起
    ③ COMMIT → 两侧同时满足 ✅

**关键前提是"两侧都不许事后改"** —— 而两张表都不可变，正好满足。
所以"不可变 + 延迟 FK"这一对组合是**自洽的**：
环形引用借"同事务一起成立"落地，不可变性保证它落地后再也不会被破坏。

（环 1 只有一侧延迟，是因为它的另一侧指向的行**不需要**"事后才出现"：
身份行必然先于版本行存在。延迟只用于化解**互相等待**，
不是"凡环必延迟" —— 这个区别是本包最容易写错的一处。）

### 与 `title_candidates` 那个环的区别（那个**不需要**延迟）

`content_versions.adopted_title_candidate_id` ↔ `title_candidates.content_version_id`
**也是**一个环，两条 FK 也都是 IMMEDIATE，但它**不需要延迟** ——
因为采纳走"**追加一版**"（docstring 六），插入顺序天然是：

    ① 版本行（adopted_* = NULL）→ ② 候选行（指向①）→ ③ **新**版本行（指向②）

**每一步引用的行都已存在**，IMMEDIATE 完全够用。
（这也是 docstring 六末段"它让'两个不可变表互相 FK'这件事不构成插入死结"
那句话的完整含义 —— 严格说，是"**追加一版**"而不是循环 FK 本身免除了死结。）

### 判据

`test_content_wp26.py` 必须有 **3 条**用例（`/tmp/wp26_cycle_probe_v3.py` 是原型）：

1. **环 2 · 版本在前**：同一事务里 `INSERT content_versions`（带 `generation_context_id`）
   → `INSERT generation_contexts`（带 `content_version_id`）⇒ 通过；
2. **环 2 · 上下文在前**：反序再测一次 ⇒ 也通过。
   1、2 两条同时是"**两侧都延迟了**"的判据 —— 漏改任何一侧，该组必红；
3. **环 1 · 负向**：`INSERT content_versions`（`content_item_id` 指向不存在的身份行）
   ⇒ **必须报 `23503`**。这条钉住"我们没有过度延迟"——
   若有人顺手给 `content_item_id` 也加上 `deferrable=True`，该用例必红。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.content.types import (
    BLUEPRINT_FIELDS,
    CONCLUSION_STRENGTH_LIST_SQL,
    CONTENT_SCHEMA_VERSION,
    CONTENT_STATE_DRAFT,
    CONTENT_STATE_LIST_SQL,
    GENERATION_CONTEXT_LIST_SQL,
    MAX_BLUEPRINT_FIELD_CHARS,
    MAX_BODY_CHARS,
    MAX_HIGHLIGHT_CHARS,
    MAX_RISK_NOTE_CHARS,
    MAX_SCENE_FORM_KEY_CHARS,
    MAX_TITLE_CHARS,
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
# 元数据的前提下拿到表名），两处必须一致 —— 测试会交叉核对（与 WP24 同款）。
#
# **为什么这四张都在其中**：
#   · `content_versions` —— 一次生成的产物。改写它 = 篡改"当时生成了什么"，
#     而 AQ-01 的盲审与 WP30 的自检都要拿它当**事实依据**；
#   · `narrative_blueprints` —— 蓝图是**文案的输入**。改一条蓝图，
#     "这篇内容为什么这么写"的答案就变了，而内容版本还是旧的；
#   · `title_candidates` —— 候选历史。采纳哪一个记在内容版本上（见模块
#     docstring 一），因此候选行本身**没有任何需要改的理由**；
#   · `generation_contexts` —— OP-160 的影响面分析靠它。
#     允许改它 = 允许把"这次生成用了哪版提示词"改成别的值，
#     而那正是追溯要回答的问题。
#
# **为什么 `content_items` 不在其中**：它是**身份**行，承载
# `current_version_id` 这个会前进的投影与 `lock_version`（ETag 载体），
# 必须可变。与 WP24 的 `plan_drafts`（工作区，可变）同一取舍。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = (
    "content_versions",
    "narrative_blueprints",
    "title_candidates",
    "generation_contexts",
)

# 本模块拥有的全部表（迁移与本模块对账用；与 `__all__` 分开，
# 因为 `__all__` 里有值类型，而这里要的是"库里有哪些表"）。
OWN_TABLES: Final[tuple[str, ...]] = (
    "content_items",
    *IMMUTABLE_TABLES,
)


class ContentItem(Base):
    """一条内容的**身份**。**可变**（`lock_version` 是 ETag 载体）。

    ## 为什么身份与版本分成两张表

    同一项目会反复重做同一条内容（"仅标题"重做、"整篇"重做），
    而运营看到的始终是"同一条内容"（列表里的同一行）。
    若只有一张表，"重做"就要就地改行 —— 于是"上一版长什么样"
    永久丢失，而"仅标题重做"的核心价值恰恰是**能对比新旧标题**。

    身份的"一项目一条内容"保证：`content_items` 数量 = 计划里的内容条数。
    版本无上限（每次重做 +1），且**只增不改**。
    """

    __tablename__ = "content_items"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 当前生效版本。**延迟外键** —— 与 `content_versions.content_item_id`
    # 构成循环，两侧都延迟后插入顺序不再重要（模块 docstring 五）。
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "content_versions.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )

    # `current_version_id` 的**冗余投影**（模块 docstring 五）。
    # 有它，列表页不必 JOIN 就能显示"当前第几版"。
    #
    # **没有版本时为 0**（而不是 NULL）：0 是一个可比较的值，
    # `ORDER BY` 与范围查询都能用；而 NULL 参与比较的结果是 UNKNOWN，
    # 会让"只改过标题的那些"这类筛选**静默返回空集**。
    current_version_no: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text(f"'{CONTENT_STATE_DRAFT}'")
    )

    # 当前版本的标题投影（列表页要显示）。
    # **可空**：还没生成文案时确实没有标题，而 `''` 会与"标题为空"
    # 这个非法状态混在一起（后者应当被 `schemas.py` 拒）。
    title: Mapped[str | None] = mapped_column(
        String(MAX_TITLE_CHARS), nullable=True
    )

    # 一次生成 = 一行的**幂等域**：同一 `(content_item_id, task_kind)`
    # 的同一"轮次"重复请求不许重复计费（R5）。
    # 网关侧的幂等由 `CapabilityRequest.idempotency_key` 保证，
    # 这一列的作用是让**预占**也能对得上（预占发生在派发之前）。
    last_idempotency_key: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )

    # ★ 这一轮为这条内容请求了什么（`types.GenerationRequest` 的落库形状）。
    #
    # **为什么必须有这一列** —— 见 `types.py` 里那段完整的接缝缺口实测：
    # "生成参数"从**发起**（端点事务）到**执行**（Worker + `handlers.py`，
    # 另一次事务）之间没有任何既有传输通道（`ClaimedJob` 冻结、
    # `jobs`/`generation_contexts` 无参数列、`projects` 无默认模型档案、
    # 处理器注入是进程级而非每请求）。没有它，处理器算不出一次预占。
    #
    # **处置借用 WP18 的同款先例**：`BRIEF_ENRICH` 的 `allowedSourceIds`
    # 当年也没有落点，WP18 在**自己模块的表**上加了一次性请求列
    # （`brief_drafts.enrichment_request`）。本列与它同款：
    # 不碰 `jobs`（WP13 的资产），不新建"只为传一次参数"的表。
    #
    # 可空：内容行在**入队之前**就已存在（那一步只需要身份），
    # 而"还没请求生成"是合法状态（与 `title` 可空同一口径）。
    # 处理器读到 `None` ⇒ 装配故障 ⇒ 不可重试的任务失败，
    # **不给默认值**（理由见 `GenerationRequest.from_payload` 的 docstring
    # 那张表：每个默认值都会把装配故障变成静默的错误行为）。
    generation_request: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )

    # ★ 接缝 §5.15：这条内容挂在哪个场景 / 形式（WP31/WP32 经
    # `GenerationRequest.scene_key/form_key` 带入）。可空，NULL = 未指派
    # （不是空串）；两列互相独立（不要求同有同无）；无受控取值域，只判格式。
    scene_key: Mapped[str | None] = mapped_column(
        String(MAX_SCENE_FORM_KEY_CHARS), nullable=True
    )
    form_key: Mapped[str | None] = mapped_column(
        String(MAX_SCENE_FORM_KEY_CHARS), nullable=True
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    lock_version: Mapped[int] = lock_version_column()

    __table_args__ = (
        CheckConstraint(
            "scene_key IS NULL OR char_length(btrim(scene_key)) > 0",
            name="scene_key_not_blank",
        ),
        CheckConstraint(
            "form_key IS NULL OR char_length(btrim(form_key)) > 0",
            name="form_key_not_blank",
        ),
        CheckConstraint(f"state IN ({CONTENT_STATE_LIST_SQL})", name="state_valid"),
        CheckConstraint("current_version_no >= 0", name="version_no_non_negative"),
        CheckConstraint(
            "title IS NULL OR char_length(title) > 0", name="title_not_blank"
        ),
        # 列表页的排序与筛选（`GET /projects/{id}/contents`）。
        Index("ix_content_items_project_updated", "project_id", "updated_at"),
        # "这个项目还有哪些没生成完"（状态筛选）。
        Index("ix_content_items_project_state", "project_id", "state"),
        Index("ix_content_items_current_version", "current_version_id"),
        # 幂等域查询：按 (内容, 幂等键) 找"这次生成是不是已经跑过"。
        Index(
            "ix_content_items_idempotency",
            "id",
            "last_idempotency_key",
        ),
    )


class ContentVersion(Base):
    """一条内容的**版本**。**不可变**（`id` 同时 FK `version_nodes.id`）。

    这是本包最核心的一张表：它同时承载三样东西 ——
    文案三件（标题/正文/标签）、采纳的标题候选、**段落级证据绑定**。
    """

    __tablename__ = "content_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "version_nodes.id",
            ondelete="RESTRICT",
            # 延迟：注册表与领域表同事务插入，顺序由 WP11 的取号重试决定。
            deferrable=True,
            initially="DEFERRED",
        ),
        primary_key=True,
    )

    content_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        # ⛔ **刻意不延迟**（模块 docstring 七）：本列非空，且"版本必须属于
        #   已存在的内容"没有任何合法流程需要"事后才补身份"。延迟它只会
        #   额外放行孤儿版本行 —— 那是削弱防线，不是化解死结。
        #   `test_content_wp26.py` 有一条负向用例钉住这一点。
        ForeignKey("content_items.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 本包内容的修订号（**与 `version_nodes.revision_no` 分开**）。
    # 分开的理由：`version_nodes` 的 revision_no 是**注册表**的序号，
    # 而本列是**业务**意义上的"第几版"，两者当前同值但语义不同 ——
    # 注册表将来若支持"同一版本挂多条修订记录"（例如补录元数据），
    # 注册表序号会前进而业务版本号不该动。
    # 取值由 `repository.next_version_no()` 在写事务内取。
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)

    # 本版所依据的蓝图（第三条 versioned 输入）。
    # **可空**：只重做标题时复用上一版的蓝图 —— 但它仍然指向**某一条**
    # 蓝图行；为空只出现在"手工补录"场景。
    blueprint_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("narrative_blueprints.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 本版采纳的标题候选。**这就是"采纳"唯一的落点**（模块 docstring 一）。
    #
    # ★ 写入规则见模块 docstring 六：**只有采纳端点产生的版本行会写这一列**，
    #   生成产生的版本行恒为 NULL。采纳**不是** UPDATE 已有版本行
    #   （那会撞不可变触发器），而是**追加一版**。
    #
    # `title_candidates` 本身是**不可变**的（见下面同名列的说明），
    # 所以不存在"先插版本行、再插候选行、再回填本列"这种顺序要求 ——
    # 本列只在插入时赋值一次，之后永不改动。这是刻意的：
    # 它让"两个不可变表互相 FK"这件事**不构成插入死结**。
    adopted_title_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("title_candidates.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 文案三件。`body` 是纯文本（含换行），**不是 markdown**（§1.5 砍掉清单）。
    title: Mapped[str] = mapped_column(String(MAX_TITLE_CHARS), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)

    # **段落级证据绑定**（R4 的存储落点）。
    #
    # 形状是 `[{"paragraphIndex": 0, "claimId": "<uuid>"|null,
    #          "reason": "...", "text": "..."}, …]`
    #
    # **为什么用 JSONB 而不是一张 `content_claim_bindings` 子表**：
    #   · 绑定**只在整版一起读**（详情端点一次取全、WP30 自检一次取全），
    #     从不按 `claim_id` 反查"哪些段落引用了它"（那是 WP34 影响图的事，
    #     而它走 `version_dependencies` 的边）；
    #   · 子表会引入"绑定数与正文段数必须一致"这条**跨表**不变量，
    #     而它在服务层已经由 `schemas.validate_bindings` 逐段判过 ——
    #     再落一条跨表约束是第二真值来源。
    #
    # **与 `plan_drafts.scene_items` 同一取舍**（WP24）：
    # 是一份版本行**内部的结构**，没有独立生命周期。
    # 代价（JSONB 内部无法落 CHECK）由 `schemas.py` 的纯函数承担，
    # 而那个函数是**唯一写入口**（旁路它就没有别的路能写这一列）。
    bindings: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # 生成这一版时用的哪次文案生成上下文（可追溯到提示词/基线/模型）。
    # 指向 `generation_contexts`，与 OP-160 的"按提示词版本反查"联动。
    #
    # ★ **必须延迟** —— 与 `content_items.current_version_id` ↔
    #   `content_versions.content_item_id`（第五节）**完全同形**，处置也相同。
    #   见模块 docstring 七。
    generation_context_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "generation_contexts.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )

    # 新版本的**追加式**快照（供下游按 schema_version 演化）。
    # 三件产出在专属列里，但整个版本行作为一个版本节点注册到
    # `version_nodes`，其 `payload_hash` 由 `canonical_payload_hash` 算出 ——
    # 摘要不能从列重算（列会演进），必须存下来。
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(CONTENT_SCHEMA_VERSION))
    )

    created_at: Mapped[datetime] = created_at_column()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        # **一内容一版本号**（业务版本号不允许重复）。
        # 这条同时是"取号并发冲突"的捕获点：两个请求同时重做时，
        # 后到的撞这条唯一约束，服务层捕获后重新取号
        # （与 WP11 的 `register_version` 同一手法）。
        Index(
            "uq_content_versions_item_version_no",
            "content_item_id",
            "version_no",
            unique=True,
        ),
        CheckConstraint("version_no >= 1", name="version_no_positive"),
        CheckConstraint("char_length(title) > 0", name="title_not_empty"),
        CheckConstraint(
            f"char_length(title) <= {MAX_TITLE_CHARS}", name="title_length"
        ),
        CheckConstraint("char_length(body) > 0", name="body_not_empty"),
        CheckConstraint(
            f"char_length(body) <= {MAX_BODY_CHARS}", name="body_length"
        ),
        CheckConstraint("jsonb_typeof(tags) = 'array'", name="tags_is_array"),
        CheckConstraint("jsonb_typeof(bindings) = 'array'", name="bindings_is_array"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_is_object"),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        # 版本历史列表（`GET /contents/{id}/versions`）。
        Index("ix_content_versions_item_created", "content_item_id", "created_at"),
        Index("ix_content_versions_blueprint", "blueprint_id"),
        Index("ix_content_versions_adopted_title", "adopted_title_candidate_id"),
        Index("ix_content_versions_generation_context", "generation_context_id"),
    )


class NarrativeBlueprint(Base):
    """叙事蓝图（9 字段结构）。**不可变**。

    9 个字段是**9 个独立列**而不是一个 JSONB —— 理由见说明文档 §1.7
    的对照表（JSONB 无 CHECK 能力，字段漂移无人发现；且
    `versions/models.py` 的 docstring 明写"注册表不是万能 JSON 仓库"）。

    **8 个文本字段各两条 CHECK（非空 + 长度上界），`conclusion_strength`
    一条（受控取值）** —— 共 17 条字段级 CHECK。

    "非空"**落 DDL 而不只在服务层判**，理由：蓝图少一个字段是**结构性缺陷**
    （下游 `schemas.py` 的 `REASON_FIELD_MISSING` 与正文分段都要按 9 个字段
    齐全来算），不是"这段话写得好不好"那种语义判断。DDL 层的
    `char_length(x) > 0` 判得动，就应当落在这里 —— 服务层可能是**旁路**
    （数据导入脚本、将来的迁移），而 CHECK 是**绕不过去的**。

    ⚠️ 但 CHECK **判不了**这 8 个字段的取值是不是"9 个字段"之一 ——
    那是 `schemas.py` 的 `validate_blueprint` 的职责（拒多余字段）。
    DDL 与纯函数的分工：DDL 管"列自身合法"，纯函数管"整份 payload 的形状"。
    """

    __tablename__ = "narrative_blueprints"

    # 8 个**文本**字段的列名，**唯一来源** —— 下面的 CHECK 从这里派生。
    # ⚠️ 不含 `conclusion_strength`：它是受控枚举，只落取值 CHECK、
    #    不落"非空"与"长度"（长度由 `String(20)` 类型自带）。
    #    它与本元组的**和**才是完整的 9 个字段 —— 这条由模块末尾的
    #    对称差断言守住（漏一个字段 = 导入即报错）。
    _TEXT_FIELD_NAMES: Final[tuple[str, ...]] = (
        "opening_function",
        "context",
        "decision_process",
        "usage_scene",
        "evidence_beats",
        "limitation",
        "interaction_question",
        "paragraph_plan",
    )

    id: Mapped[uuid.UUID] = uuid_pk()

    content_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_items.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # --- 9 个字段（顺序与说明文档 §2.4 第 2 项一致）------------------------
    # `opening_function`：开头承担什么功能（钩子 / 场景切入 / 直接给结论）。
    opening_function: Mapped[str] = mapped_column(Text, nullable=False)
    # `context`：这条内容在什么背景下说（谁、什么时候、为什么看）。
    context: Mapped[str] = mapped_column(Text, nullable=False)
    # `decision_process`：读者做决策时经历什么（`plan-detail.html` 的
    # "结构：容量 → 单次用量推算 → 结论"就是这一栏的来源）。
    decision_process: Mapped[str] = mapped_column(Text, nullable=False)
    # `usage_scene`：挂在哪个使用场景（来自 WP24 的 `scene_items`，
    # **本包不重新生成角度**，见 §1.5 砍掉清单第 4 条）。
    usage_scene: Mapped[str] = mapped_column(Text, nullable=False)
    # `evidence_beats`：每一段里要出现的事实点（供 `bindings` 对齐）。
    evidence_beats: Mapped[str] = mapped_column(Text, nullable=False)
    # `limitation`：**本条最要紧的必填项** —— `plan-detail.html` 的硬约束
    # "含待补信息的行，生成时可退化为产品说明结构，但不允许把空白补成
    # 使用经历"。这一栏就是那句话的落点（R10）。
    limitation: Mapped[str] = mapped_column(Text, nullable=False)
    # `conclusion_strength`：**唯一的受控枚举**（见 types.py 的说明）。
    conclusion_strength: Mapped[str] = mapped_column(String(20), nullable=False)
    # `interaction_question`：结尾的互动问题（小红书形态）。
    interaction_question: Mapped[str] = mapped_column(Text, nullable=False)
    # `paragraph_plan`：段落规划（正文分段的依据；与 `bindings` 的
    # 段号一一对应）。
    paragraph_plan: Mapped[str] = mapped_column(Text, nullable=False)

    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(CONTENT_SCHEMA_VERSION))
    )
    created_at: Mapped[datetime] = created_at_column()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            f"conclusion_strength IN ({CONCLUSION_STRENGTH_LIST_SQL})",
            name="conclusion_strength_valid",
        ),
        # 8 个文本字段各自"非空 + 在界内"。
        #
        # **用生成器表达式而不是手敲 16 条**：手敲会漏（而漏了**无人报错**，
        # 只是 CHECK 少一条）。改成从 `_TEXT_FIELD_NAMES` 派生后，
        # 下面那两条 `assert` 把"漏一个字段"变成**导入即失败**。
        #
        # 与 `planning/models.py` 的做法区别：那边字段少且各字段约束不同，
        # 手写更清楚；这边 8 个字段的约束**完全同构**，派生才是唯一来源。
        *(
            CheckConstraint(
                f"char_length({name}) > 0", name=f"{name}_not_empty"
            )
            for name in _TEXT_FIELD_NAMES
        ),
        *(
            CheckConstraint(
                f"char_length({name}) <= {MAX_BLUEPRINT_FIELD_CHARS}",
                name=f"{name}_length",
            )
            for name in _TEXT_FIELD_NAMES
        ),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        # `plan-detail.html` 那一屏要"这 10 条的蓝图分别是什么"。
        Index("ix_narrative_blueprints_item_created", "content_item_id", "created_at"),
    )


# ---------------------------------------------------------------------------
# 蓝图字段的三条对账断言（**导入期**执行，不是测试期）
#
# 为什么放在**模块级**而不是测试里：这些不变量一旦破了，
# `CREATE TABLE` 出来的表就**少几条 CHECK**，而少 CHECK 是**静默**的 ——
# 迁移能跑通、测试能跑过（除非专门去数约束）。放到导入期才拦得住。
#
# ⚠️ 写这三条时踩过一次：第一版只写了"对称差为空"，
#    而当时 `_TEXT_FIELD_NAMES`（那时叫 `_FIELD_NAMES`）**忘了含
#    `conclusion_strength`** —— 断言当场报错，拦下来了。
#    这就是它存在的理由：手写字段清单一定会漏，而"漏"必须**响**。
# ---------------------------------------------------------------------------

_BLUEPRINT_ENUM_FIELDS: Final[frozenset[str]] = frozenset({"conclusion_strength"})

# ① "8 个文本字段" ∪ "枚举字段" 与 `types.BLUEPRINT_FIELDS`（唯一来源）
#    **恰相等**。写成集合相等而不是"是子集"：字段增删改任一侧都要人来确认。
assert (set(NarrativeBlueprint._TEXT_FIELD_NAMES) | _BLUEPRINT_ENUM_FIELDS) == set(
    BLUEPRINT_FIELDS
), (
    "narrative_blueprints 的字段名与 types.BLUEPRINT_FIELDS 不一致："
    f"模型侧独有 {sorted((set(NarrativeBlueprint._TEXT_FIELD_NAMES) | _BLUEPRINT_ENUM_FIELDS) - set(BLUEPRINT_FIELDS))}；"
    f"类型侧独有 {sorted(set(BLUEPRINT_FIELDS) - (set(NarrativeBlueprint._TEXT_FIELD_NAMES) | _BLUEPRINT_ENUM_FIELDS))}"
)

# ② 两类字段**不重叠**（枚举字段不能同时被当成文本字段去要"非空/长度"）。
assert not (set(NarrativeBlueprint._TEXT_FIELD_NAMES) & _BLUEPRINT_ENUM_FIELDS), (
    "_TEXT_FIELD_NAMES 与枚举字段重叠："
    f"{sorted(set(NarrativeBlueprint._TEXT_FIELD_NAMES) & _BLUEPRINT_ENUM_FIELDS)}"
)

# ③ 蓝图表的列数与预期**相等**（不是 `>=`）：
#    8 个文本 + 1 个枚举 + 通用列（id / content_item_id / schema_version /
#    created_at / created_by）。
_BLUEPRINT_COMMON_COLS: Final[int] = 5
_BLUEPRINT_EXPECTED_COLS: Final[int] = (
    len(NarrativeBlueprint._TEXT_FIELD_NAMES)
    + len(_BLUEPRINT_ENUM_FIELDS)
    + _BLUEPRINT_COMMON_COLS
)
assert len(NarrativeBlueprint.__table__.columns) == _BLUEPRINT_EXPECTED_COLS, (
    "narrative_blueprints 列数与预期不符："
    f"实际 {len(NarrativeBlueprint.__table__.columns)}，"
    f"预期 {_BLUEPRINT_EXPECTED_COLS}"
)

# ④ 8 个文本字段各 2 条 CHECK（非空 + 长度）+ 枚举 1 条 + schema_version 1 条。
#    **等式**：少一条说明生成器表达式或字段清单出了问题。
_BLUEPRINT_EXPECTED_CHECKS: Final[int] = (
    len(NarrativeBlueprint._TEXT_FIELD_NAMES) * 2 + 2
)
assert (
    len(
        [
            c
            for c in NarrativeBlueprint.__table__.constraints
            if c.__class__.__name__ == "CheckConstraint"
        ]
    )
    == _BLUEPRINT_EXPECTED_CHECKS
), (
    "narrative_blueprints 的 CHECK 条数与预期不符："
    f"实际 {len([c for c in NarrativeBlueprint.__table__.constraints if c.__class__.__name__ == 'CheckConstraint'])}，"
    f"预期 {_BLUEPRINT_EXPECTED_CHECKS}"
)


class TitleCandidate(Base):
    """一个标题候选（+ 6 维评分 + 关键词/吸引点/风险说明）。**不可变**。

    **采纳不在这里**（模块 docstring 一）：本表没有任何可变的列，
    因此"哪一版用了哪个标题"只有一个答案来源。
    """

    __tablename__ = "title_candidates"

    id: Mapped[uuid.UUID] = uuid_pk()

    # 本候选属于哪一次生成。指向 `content_versions` 而不是
    # `content_items`：候选是**一次生成**的产物，而一次生成 = 一版。
    # 指向身份会让"这条内容的候选"混起所有历史版本 ——
    # 那时"这个候选是哪一版给的"就没有答案了。
    #
    # ★ 与 `content_versions.adopted_title_candidate_id` 构成**跨表循环引用**
    #   （版本指向被采纳的候选，候选又指向所属版本），且两表都**不可变**。
    #   这**不是死结**，因为采纳走的是"追加一版"而不是 UPDATE
    #   （模块 docstring 六）—— 插入顺序是：
    #     ① 生成 → 插入 `content_versions` 行（`adopted_*` = NULL）
    #     ② 生成 → 插入本表候选行（`content_version_id` = ① 的 id）
    #     ③ 采纳 → 插入**新** `content_versions` 行（`adopted_*` = ② 中某个 id）
    #   推荐值只在本列插入时写入，改不了 ①的 `adopted_*`，也不需要改。
    content_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 同一版本内的候选序号（0 基）。**有它才能"按输入顺序展示"** ——
    # 而输入顺序是模型给的优先级信号（小红书里第一个标题权重最高）。
    candidate_index: Mapped[int] = mapped_column(Integer, nullable=False)

    title: Mapped[str] = mapped_column(String(MAX_TITLE_CHARS), nullable=False)

    # 6 维评分。**JSONB 而不是 6 个列**：量纲由产品定（说明文档附录 A4），
    # 而具名列会隐含"6 个都是同一量纲"这个尚未确定的假设。
    # 代价（JSONB 内部无法落 CHECK）由 `schemas.py` 逐维判承担，
    # 且"6 维齐全、不多不少"是显式判据（不是 `is not None` 那种恒真式）。
    scores: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # 关键词 / 吸引点 / 风险说明（历史文档的硬规则，且可判）。
    keywords: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    highlight: Mapped[str | None] = mapped_column(
        String(MAX_HIGHLIGHT_CHARS), nullable=True
    )
    # `risk_note`：这个标题有什么风险（夸大、与正文不符、蹭敏感热点）。
    # **可空**：多数候选没有风险可讲；而"必须有风险说明"会逼模型
    # 编一个不存在的风险出来（与 `bindings.reason` 同一取向 ——
    # 允许的例外必须留痕，但不强迫留痕）。
    risk_note: Mapped[str | None] = mapped_column(
        String(MAX_RISK_NOTE_CHARS), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("candidate_index >= 0", name="candidate_index_non_negative"),
        CheckConstraint("char_length(title) > 0", name="title_not_empty"),
        CheckConstraint(
            f"char_length(title) <= {MAX_TITLE_CHARS}", name="title_length"
        ),
        CheckConstraint("jsonb_typeof(scores) = 'object'", name="scores_is_object"),
        CheckConstraint(
            "jsonb_typeof(keywords) = 'array'", name="keywords_is_array"
        ),
        CheckConstraint(
            f"highlight IS NULL OR char_length(highlight) <= {MAX_HIGHLIGHT_CHARS}",
            name="highlight_length",
        ),
        CheckConstraint(
            f"risk_note IS NULL OR char_length(risk_note) <= {MAX_RISK_NOTE_CHARS}",
            name="risk_note_length",
        ),
        # 同一版本内候选序号唯一 —— 没有它，"第 2 个候选"有两个答案。
        Index(
            "uq_title_candidates_version_index",
            "content_version_id",
            "candidate_index",
            unique=True,
        ),
    )


class GenerationContext(Base):
    """一次生成用的**上下文快照**：提示词版本 / 模板 / 基线 / 模型档案。**不可变**。

    ## ★ 这张表就是 OP-160 的答案

    登记册 OP-160 原文："**哪个提示词版本被哪些生成消费**答不了"。
    有了本表，那个问题变成一条按 `prompt_version_id` 的索引查询 ——
    触发条件是 WP23 交付 + 本包落点。

    ## 为什么要 6 个 id，而不是只记提示词版本

    "改了提示词之后质量变了"与"换了模型之后质量变了"是**两个不同的结论**，
    而它们在一个只记 `prompt_version_id` 的模型里**看起来一样**。
    记录 `model_profile_id` 与 `baseline_id` 才让这两件事分得开。

    ## `content_version_id` 为什么可空

    `CONTENT_STRUCTURE` 那一次调用发生时，内容版本**还没有**（它产出的是
    蓝图，而蓝图是文案的输入）。若该列非空，结构调用就无法留下上下文行 ——
    于是"这批内容的蓝图是哪版提示词生成的"永远查不到，
    而蓝图同样影响内容质量。

    处置：结构调用的上下文行 `content_version_id = NULL`，
    文案调用的上下文行指向它产出的那一版；两者靠
    `(content_id, task_kind)` 归组。
    """

    __tablename__ = "generation_contexts"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    content_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_items.id", ondelete="RESTRICT"),
        nullable=False,
    )

    content_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "content_versions.id",
            ondelete="RESTRICT",
            # ★ **必须延迟**（模块 docstring 七）：与本行所指版本的
            #   `generation_context_id` 构成循环外键，两侧都延迟才能落地。
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )

    task_kind: Mapped[str] = mapped_column(String(40), nullable=False)

    # --- OP-160 的四个 id -------------------------------------------------
    prompt_template_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("prompt_templates.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # **可空但"应当有"**：正常路径上它必然有值（因为 fail-closed 要求
    # 查不到激活版本就不生成，见 R6）。可空是为了容纳"激活版本后来被删"——
    # 而 PG 的 `ON DELETE RESTRICT` 让它删不掉，于是这个 NULL 只出现在
    # 历史数据导入场景。留空比塞一个假 UUID 好。
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("prompt_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    baseline_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("production_baselines.id", ondelete="RESTRICT"),
        nullable=True,
    )
    model_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("model_profiles.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 这次调用在网关侧的实际调用 id（`provider_calls.id`）。
    # 有了它，"这次生成花了多少钱"能从 WP25 的账本上串起来。
    provider_call_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("provider_calls.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 幂等键（与 `CapabilityRequest.idempotency_key` 同值）。
    # **存下来的理由**：重试时要用**同一个**键去问网关"这个键跑过没有"，
    # 而键是由内容版本号 + task_kind 派生的 —— 派生规则若在服务层，
    # 重启后（内容版本号已变）就派生不出同一个键，幂等失效且不报错。
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)

    created_at: Mapped[datetime] = created_at_column()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            f"task_kind IN ({GENERATION_CONTEXT_LIST_SQL})", name="task_kind_valid"
        ),
        # ★ OP-160 的能力前提（模块 docstring 四）：按提示词版本反查历史生成。
        Index("ix_generation_contexts_prompt_version", "prompt_version_id"),
        # OP-160 的粗粒度问法："改了这个模板影响谁"。
        Index("ix_generation_contexts_prompt_template", "prompt_template_id"),
        # "这批内容的生成为什么变差了" → 按基线归组。
        Index("ix_generation_contexts_baseline", "baseline_id"),
        Index("ix_generation_contexts_model_profile", "model_profile_id"),
        # 详情端点按内容取上下文；按 (内容, kind) 归组结构/文案两次调用。
        Index("ix_generation_contexts_content_kind", "content_id", "task_kind"),
        # 幂等：同一个键在同一内容上只该有**一行**上下文。
        # 为什么不是全局唯一：不同内容可以有相同的键前缀，而键里含
        # 内容版本号 —— 同内容同版本同 kind 才是"同一次生成"。
        Index(
            "uq_generation_contexts_idempotency",
            "content_id",
            "idempotency_key",
            unique=True,
        ),
    )


guard_immutable(ContentVersion)
guard_immutable(NarrativeBlueprint)
guard_immutable(TitleCandidate)
guard_immutable(GenerationContext)


__all__ = [
    "IMMUTABLE_TABLES",
    "OWN_TABLES",
    "ContentItem",
    "ContentVersion",
    "GenerationContext",
    "NarrativeBlueprint",
    "TitleCandidate",
]