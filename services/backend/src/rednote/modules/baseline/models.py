"""信息审核与基线的四张表（WP22）。

| 表 | 性质 | 可变性 |
|---|---|---|
| `information_drafts` | 审核工作区（事实 / 冲突 / 场景 / 边界 / 研究选用） | **可变**（`lock_version`，ETag 载体） |
| `production_baselines` | 基线（**不可变**，`id` 同时 FK `version_nodes.id`） | 不可变 |
| `baseline_items` | 基线内的条目（**不可变**，含未纳入项与原因） | 不可变 |
| `step_approvals` | 跨步骤批准（**不可变**，步骤 1/2/3 通用，闭合 OP-151） | 不可变 |

---

## 一、`information_drafts` 与 `brief_drafts` 的分工（R6，最容易被混淆的一处）

两个名字极像、作用完全不同：

| | `brief_drafts`（WP18） | `information_drafts`（本包） |
|---|---|---|
| 阶段 | Brief **解构前**的工作区 | 步骤 3 的**审核**工作区 |
| 运营在做什么 | 改字段、改结论、处置解析疑问 | 逐条确认事实、**处置冲突**、选用场景与研究资料 |
| 与版本的关系 | 固化出 `brief_versions` | 基线**只读到那一版为止**，不固化草稿 |
| 唯一性 | 一项目一份 | **一项目一份** |

**为什么必须独立成表**：若共用一张表或一个状态机，会出现
"审核时改了字段 → 解构结果被覆盖"的双向踩踏，而**两边读起来都正常工作**
（没有报错，只是数据在错误的地方被改了）。因此：独立表 + 独立服务
+ `project_id` 唯一（与 `briefs.project_id` 同构）。

## 二、`production_baselines` 为什么**没有** `state` 列（本包的刻意取舍）

设计文档 §2.1 第 1 层写的是「部分唯一索引 —— 每项目一个 ACTIVE 基线」，
而 §2.3 决策 5 与 AC-08 要求「基线**不可原地修改**」。两者**不能同时成立**：

> 一个 `state` 从 `ACTIVE` 变成 `SUPERSEDED` 的动作，就是一次
> 「原地改写旧行」—— 而 AC-08 说修改必须创建新版本。

本实现取**忠实于 AC-08 的那一侧**，并记录该冲突（收口时登记）：

| 层 | 设计文档原方案 | 本实现 | 为什么仍然成立 |
|---|---|---|---|
| 第 1 层 | 部分唯一索引 `WHERE state='ACTIVE'` | 部分唯一索引 `WHERE supersedes_id IS NULL`（**链根唯一**） | 见下 |
| "当前生效的是哪条" | 读 `state='ACTIVE'` 的那一行 | 读 `projects.active_baseline_id`（**投影列**） | 一个列**物理上装不下两个值** → "每项目一个生效基线"由结构保证，比索引更强 |

「每项目一个 ACTIVE 基线」这条不变量在本实现里由
**`projects.active_baseline_id` 是单值列**保证 —— 这比部分唯一索引更硬
（索引拦的是"插入第二行"，列拦的是"同时存在两个值"这件事本身）。

而部分唯一索引并未取消，它守的是另一条**真实且可插入期校验**的不变量：
**每个项目只能有一条链根**（`supersedes_id IS NULL` 的行至多一条）——
替代关系是链，两条互不相干的链会让"当前生效"有第二个候选。

## 三、"谁替代了 BL-v2"由反向查询得到（决策 5）

新基线记 `supersedes_id` 指向被它替代的那条；**旧行上不存 `superseded_by_id`**
—— 那一列意味着"改写旧行"，而旧行不可变。

这与 WP20 的「状态由事件流投影算出，不是一列」是同一条原则。
`projects.active_baseline_id` 是**可重建的**（`service.rebuild_active_baseline`），
测试会验证"增量维护与全量重算一致"（与 WP20 `rebuild_active_version` 同款）。

## 四、`baseline_items` 三处刻意的列设计

1. **`ref` 与 `snapshot` 双存**（决策 1）：`knowledge_asset_id` +
   `knowledge_version_id` 是 `ref`（钉死版本），`snapshot` 是当时的值。
   只存 `ref` → 知识更新后基线被**静默污染**（R1）；只存 `snapshot` → 断溯源（AC-24）。
2. **`inclusion` + `exclusion_reason`**：AC-24 要求「保存实际选用条目、版本、
   片段与**未纳入原因**」。排除项**也落行** —— 若"没纳入"只是"没有那一行"，
   "为什么没纳入"就永远查不到（说明文档 §1.2 的"容易忽略的用户"）。
3. **`snapshot_text` 与 `snapshot` 并存**：`snapshot` 是结构化负载（决策 3，
   给程序消费与溯源），`snapshot_text` 是**当时人读的原文**（给半年后查事故的人）。
   两者都不由本包重新解释 —— 只是把当时的版本内容复制一份。

## 五、`step_approvals` 的形状是本包定义的（闭合 OP-151）

OP-151 原文（登记册 L486）说协议**未定义**这个形状。
本包必须现在定，因为第 4 层防线的触发器要校验"基线在 COMMIT 时有对应批准"
—— **没有形状就写不出校验**。

批准的**对象是「步骤 + 版本」的组合**，理由是：
Brief 出了 v2 之后，步骤 1 的"已批准"指的是 **v2 已批准**，
而不是"步骤 1 曾经被批过"。只记步骤会让"这一步批的是哪一版"
在数据里**没有答案**，而八步闭环的验收（证据 = 「固定版本」）恰恰要回答它。

`subject_version_id` 指向 `version_nodes.id`（不是领域表），
因此它**通用**：步骤 1 存 brief 版本节点、步骤 3 存基线版本节点，
同一张表不需要为每步加一列。

## 六、`version_nodes.entity_id` 对 `BASELINE` 取什么值

`version_nodes.entity_id` 的语义是"逻辑实体的身份，跨版本不变"。
`BASELINE` 的取值是 **`project_id`**，理由：

本包没有"基线身份表"——`production_baselines` **自己就是版本化实体**
（`id PK/FK version_nodes`，设计文档 §0.2 事实 #5）。因此一个项目的
基线的"逻辑实体"就是**该项目的那条基线链**，`entity_id = project_id`
恰好使 `UQ(entity_type, entity_id, revision_no)` 的语义变成
"每个项目的基线有自己的修订号序列"——正是想要的。

第 6 层防线的判据 3（节点的 `entity_id` 必须等于子表的归属）因此写作
`entity_id = NEW.project_id`，它能抓到"给 A 项目注册了一个声称属于 B 项目
的基线节点"这类矛盾。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.baseline.baseline_types import (
    APPROVAL_DECISION_LIST_SQL,
    BASELINE_SCHEMA_VERSION,
    INCLUSION_LIST_SQL,
    ITEM_KIND_LIST_SQL,
    MAX_CHANGE_REASON_CHARS,
    MAX_DRAFT_ITEMS,
    MAX_REF_LABEL_CHARS,
    MAX_REASON_CHARS,
    MAX_RULESET_LABEL_CHARS,
    STEP_MAX,
    STEP_MIN,
    SUBJECT_KIND_LIST_SQL,
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
# **为什么 `step_approvals` 也在其中**：一份批准是**一次业务判断的证据**。
# 允许原地改写它，"这一步当时是被批准还是被拒绝"就取决于有没有人改过 ——
# 而没有任何人能审计这件事（与 `brief_versions` / `knowledge_versions` 同一论证）。
# 撤销走的也是"新增"：新批准记 `supersedes_id`（决策 5 同款）。
#
# **为什么 `information_drafts` 不在其中**：它是**工作区**，运营在里面
# 反复改是它的正常用法（与 `brief_drafts` 同款判断）。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = (
    "production_baselines",
    "baseline_items",
    "step_approvals",
)

# `snapshot` / `claims` 等 JSONB 的形状版本。
BASELINE_PAYLOAD_SCHEMA_VERSION: Final[int] = BASELINE_SCHEMA_VERSION


class InformationDraft(Base):
    """步骤 3 的审核工作区。**可变**，一项目一份（`project_id` 唯一）。

    各列都是 JSONB 数组/对象，因为它们是**运营正在编辑的内容**，
    不是已固化的历史：形状由 `readiness.py` 与 `api.py` 的请求模型约束。

    **为什么没有 `*_count` 列**（决策 7）：原型 L42~61 的四个指标
    （审核项 42 / 已处置 41 / 待处置 1 / 可生产场景 5）全部**从这些数组算出来**。
    落一列 count 就多了一个真值来源 —— 它与数组脱节时**不报错**，
    只会让 AC-09 的"存在阻断项"判断失效，而那意味着**基线可以被非法推进**。
    """

    __tablename__ = "information_drafts"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 本工作区正在审核的 Brief 版本（钉住，与 `brief_drafts.base_version_id` 同义）。
    brief_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "version_nodes.id",
            ondelete="RESTRICT",
            # 延迟：草稿与 Brief 版本的写入顺序不由本包控制。
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )

    # --- 五块审核内容 ---------------------------------------------------
    # 事实清单（原型 L78~101）：每行 {claimId, statement, source, state, kind?}
    claims: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # 冲突与其处置：每行 {conflictId, kind, blocking, state, action?, reason?}
    conflicts: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # 人群场景（原型 L149~205）：每行 {sceneId, name, audience, factRefs, state}
    scenes: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # 表达边界（原型 L228~270）：{mustMention[], mustNotMention[], bannedPhrases[], risks[]}
    boundaries: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # 研究资料选用（原型 L93~102）：每行 {fileId, title, included, exclusionReason?}
    research_selection: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # PRD §7.3 条件 ③「运营明确点击确认」的载体。
    # **它是一个显式的布尔，而不是从"草稿有内容"推断**：条件 ③ 的语义是
    # "人工点过确认"，系统代判会让它失效（`readiness.py` 的说明）。
    operator_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(BASELINE_PAYLOAD_SCHEMA_VERSION))
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    lock_version: Mapped[int] = lock_version_column()

    __table_args__ = (
        # 一项目一份（R6）。
        UniqueConstraint("project_id", name="uq_information_drafts_project_id"),
        # --- 形状 CHECK：数组列必须是数组，对象列必须是对象 ----------------
        # **不判非空**：一份刚创建的草稿本来就什么都没审核（`[]` 是合法初值），
        # 而 WP20 的 `structured_payload_not_empty` 判非空是因为那里
        # `structured_payload` 承载"事实字段"，空对象等于没写事实。
        # 两处判据不同是因为**语义不同**（刻意的，不是不一致）。
        CheckConstraint(
            "jsonb_typeof(claims) = 'array'", name="claims_is_array"
        ),
        CheckConstraint(
            "jsonb_typeof(conflicts) = 'array'", name="conflicts_is_array"
        ),
        CheckConstraint("jsonb_typeof(scenes) = 'array'", name="scenes_is_array"
        ),
        CheckConstraint(
            "jsonb_typeof(research_selection) = 'array'",
            name="research_selection_is_array",
        ),
        CheckConstraint(
            "jsonb_typeof(boundaries) = 'object'", name="boundaries_is_object"
        ),
        # 上界：防止某次导入把整本书塞进 `claims`。
        CheckConstraint(
            f"jsonb_array_length(claims) <= {MAX_DRAFT_ITEMS}",
            name="claims_within_bound",
        ),
        CheckConstraint(
            f"jsonb_array_length(conflicts) <= {MAX_DRAFT_ITEMS}",
            name="conflicts_within_bound",
        ),
        CheckConstraint(
            f"jsonb_array_length(scenes) <= {MAX_DRAFT_ITEMS}",
            name="scenes_within_bound",
        ),
        CheckConstraint(
            f"jsonb_array_length(research_selection) <= {MAX_DRAFT_ITEMS}",
            name="research_within_bound",
        ),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        Index("ix_information_drafts_project_id", "project_id"),
        Index("ix_information_drafts_updated_at", "updated_at"),
    )


class ProductionBaseline(Base):
    """一条基线。**不可变**（`id` 同时 FK `version_nodes.id`）。

    见模块 docstring 第二、三节：没有 `state` 列，"当前生效的是哪条"
    由 `projects.active_baseline_id` 回答；替代关系用 `supersedes_id` 单向表达。

    `snapshot` 是**基线级**快照（与 `baseline_items` 的**条目级**快照区分）：
    原型 L56~64 的 9 项清单里，属于"整条基线"的那几项落在这里
    （批准人 / 批准时间 / 规则集读数 / 已确认限制 / 事实清单摘要），
    属于"某一项"的落在 `baseline_items`。
    """

    __tablename__ = "production_baselines"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "version_nodes.id",
            ondelete="RESTRICT",
            # 延迟：注册表与具体表同事务插入，顺序由 WP11 的取号重试逻辑决定
            # （`register_version` 内部有 SAVEPOINT + 重试，不暴露节点插入时机）。
            deferrable=True,
            initially="DEFERRED",
        ),
        primary_key=True,
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 钉住"哪一版 Brief"（原型 L60「BL-v3 绑定 Brief v2」）。
    brief_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "version_nodes.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )

    # 审核规则集的版本标签（原型 L61「审核规则 v5（9 阻断 · 14 提醒）」）。
    # **只被快照，不被执行** —— 规则的判定归 WP30（说明文档 §1.1 的反面）。
    ruleset_version: Mapped[str] = mapped_column(
        String(MAX_RULESET_LABEL_CHARS), nullable=False
    )

    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # 被本条替代的那条基线（`production_baselines.id` = 版本节点 id）。
    # **单向**：旧行上不存 `superseded_by_id`（模块 docstring 第三节）。
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "production_baselines.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )

    change_reason: Mapped[str] = mapped_column(
        String(MAX_CHANGE_REASON_CHARS), nullable=False
    )

    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(BASELINE_PAYLOAD_SCHEMA_VERSION))
    )

    created_at: Mapped[datetime] = created_at_column()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "char_length(ruleset_version) > 0", name="ruleset_version_not_empty"
        ),
        CheckConstraint(
            "char_length(change_reason) > 0", name="change_reason_not_empty"
        ),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'", name="snapshot_is_object"
        ),
        # 自引用必须排除自身 —— 否则"我替代了我自己"这条无意义的关系合法。
        CheckConstraint(
            "supersedes_id IS NULL OR supersedes_id <> id",
            name="supersedes_not_self",
        ),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),

        # --- 第 1 层防线：部分唯一索引（**`WHERE` 必须逐条核对**） -----------
        # 每个项目至多一条链根。漏写 `WHERE` 会让它退化成"每项目只能有一条基线"
        # —— 而那条约束与 AC-08 直接冲突，且症状迟一步才出现
        # （第二次创建基线时），本机实测 3 已证这是**静默退化**。
        Index(
            "uq_production_baselines_project_root",
            "project_id",
            unique=True,
            postgresql_where=text("supersedes_id IS NULL"),
        ),
        Index("ix_production_baselines_project_id", "project_id", "created_at"),
        Index("ix_production_baselines_supersedes_id", "supersedes_id"),
        Index("ix_production_baselines_brief_version_id", "brief_version_id"),
    )


class BaselineItem(Base):
    """基线内的一条条目。**不可变**。

    见模块 docstring 第四节的三处列设计（`ref`+`snapshot` 双存、
    `inclusion`+`exclusion_reason`、`snapshot_text` 与 `snapshot` 并存）。

    `context_order` 的默认值由 `context_order.order_context_items()` 派生后写入，
    但**列上有值**（不是每次读时再算）：它是"批准那一刻的顺序"，
    属于快照的一部分。重算会随 `CONTEXT_PRIORITY_ORDER` 的演进变化，
    而基线必须固定（AC-08）。
    """

    __tablename__ = "baseline_items"

    id: Mapped[uuid.UUID] = uuid_pk()

    baseline_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("production_baselines.id", ondelete="RESTRICT"),
        nullable=False,
    )

    item_no: Mapped[int] = mapped_column(Integer, nullable=False)
    item_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    inclusion: Mapped[str] = mapped_column(String(20), nullable=False)

    # 人读的引用标签（原型「事实 v7」「R-014」这种短标识）。
    ref_label: Mapped[str] = mapped_column(
        String(MAX_REF_LABEL_CHARS), nullable=False
    )

    # --- `ref`：钉死"哪条知识的哪一版" -----------------------------------
    knowledge_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_assets.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # **钉死版本**，而不是让读方去追 `knowledge_assets.active_version_id`
    # —— 那正是 R1（基线被知识更新悄悄污染）。本机实测与设计文档 §2.2
    # 的次场景（9-15 批准 62%，9-20 知识更正为 60%）验证的就是这一列。
    knowledge_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # --- `snapshot`：当时批准的值 ----------------------------------------
    # 结构化负载（决策 3）：值 + 单位 + 限定条件 + 来源片段 id，
    # 与 WP20 的 `knowledge_versions.structured_payload` 同构 ——
    # 不重新解释，只复制一份，因此它**不引入新的真值来源**。
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # 当时人读的原文（可由 WP26 直接进上下文，也是半年后查事故时的第一手材料）。
    snapshot_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- 未纳入原因（AC-24 原文） ----------------------------------------
    exclusion_reason: Mapped[str | None] = mapped_column(
        String(MAX_REASON_CHARS), nullable=True
    )

    # 动态上下文顺序（由 `context_order.py` 派生，落列见类 docstring）。
    context_order: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint(
            "baseline_id", "item_no", name="uq_baseline_items_baseline_item_no"
        ),
        # **同一条知识不能既纳入又排除**：两条行同时存在时，
        # "这条被采纳了吗"有两个矛盾答案，而下游（WP26）只会取先读到的那条。
        # 排除项的 `knowledge_asset_id` 可为 NULL（研究资料不一定是知识资产），
        # NULL 不参与唯一性 —— 这是想要的（多条无资产引用的研究资料可以并存）。
        UniqueConstraint(
            "baseline_id",
            "knowledge_asset_id",
            name="uq_baseline_items_baseline_asset",
        ),
        CheckConstraint("item_no >= 1", name="item_no_positive"),
        CheckConstraint("context_order >= 1", name="context_order_positive"),
        CheckConstraint(f"item_kind IN ({ITEM_KIND_LIST_SQL})", name="item_kind_valid"),
        CheckConstraint(f"inclusion IN ({INCLUSION_LIST_SQL})", name="inclusion_valid"),
        CheckConstraint("char_length(ref_label) > 0", name="ref_label_not_empty"),
        CheckConstraint("jsonb_typeof(snapshot) = 'object'", name="snapshot_is_object"),
        # 空快照 = 只有引用没有值 —— 那就是"只存 ref"，正是 R1 的形状。
        CheckConstraint("snapshot <> '{}'::jsonb", name="snapshot_not_empty"),
        # 排除必须说为什么（纳入不必 —— 纳入的理由在 `ref` 与 `snapshot` 里）。
        CheckConstraint(
            "inclusion <> 'EXCLUDED' "
            "OR (exclusion_reason IS NOT NULL AND char_length(exclusion_reason) > 0)",
            name="excluded_needs_reason",
        ),
        # 纳入的项目不该带排除原因（带了说明状态与原因不一致）。
        CheckConstraint(
            "inclusion <> 'INCLUDED' OR exclusion_reason IS NULL",
            name="included_has_no_reason",
        ),
        # 有版本引用就必须有资产引用 —— 否则版本指向谁无从判断。
        CheckConstraint(
            "knowledge_version_id IS NULL OR knowledge_asset_id IS NOT NULL",
            name="version_ref_needs_asset_ref",
        ),
        # item_no 是 int 列；上界显式写出，避免溢出时报出"整数超出范围"
        # （那种错误读起来像程序 bug，其实是数据到了极限）。
        CheckConstraint("item_no <= 2147483647", name="item_no_in_range"),
        Index("ix_baseline_items_baseline_id", "baseline_id", "item_no"),
        Index("ix_baseline_items_knowledge_asset_id", "knowledge_asset_id"),
        Index("ix_baseline_items_knowledge_version_id", "knowledge_version_id"),
    )


class StepApproval(Base):
    """一次跨步骤批准。**不可变**（闭合 OP-151，见模块 docstring 第五节）。

    `subject_version_id` 指向 `version_nodes.id` —— 因此同一张表服务所有步骤：
    步骤 1 存 Brief 版本节点、步骤 3 存基线版本节点，不需要为每步加一列。

    **`decided_at` 是显式的列而不是靠 `created_at`**：两者在本表里通常相同，
    但"决策时刻"与"落库时刻"是两件事（补录历史批准时它们不同），
    而 S2 的 D4 要求"谁、何时、为什么"三个答案都显式。
    """

    __tablename__ = "step_approvals"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    step_no: Mapped[int] = mapped_column(Integer, nullable=False)

    subject_kind: Mapped[str] = mapped_column(String(30), nullable=False)

    # 被批准的**具体那一版**。指向共用注册表（与 `version_nodes.id` 同构）。
    subject_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "version_nodes.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=False,
    )

    decision: Mapped[str] = mapped_column(String(20), nullable=False)

    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    reason: Mapped[str] = mapped_column(String(MAX_REASON_CHARS), nullable=False)

    # 被本条替代的上一条批准（撤销靠新增，不靠改写 —— 与决策 5 同款）。
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "step_approvals.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            f"step_no >= {STEP_MIN} AND step_no <= {STEP_MAX}", name="step_no_in_range"
        ),
        CheckConstraint(
            f"subject_kind IN ({SUBJECT_KIND_LIST_SQL})", name="subject_kind_valid"
        ),
        CheckConstraint(
            f"decision IN ({APPROVAL_DECISION_LIST_SQL})", name="decision_valid"
        ),
        CheckConstraint("char_length(reason) > 0", name="reason_not_empty"),
        CheckConstraint(
            "supersedes_id IS NULL OR supersedes_id <> id", name="supersedes_not_self"
        ),
        # 每个 (项目, 步骤, 对象类型, 版本) 至多一条**链根**批准
        # （与基线的链根唯一性同一手法；撤销靠新增一条带 `supersedes_id` 的）。
        # 同一条不变量：`WHERE` 是判据的一部分，漏写会退化成
        # "同一版只能有一条批准"——那会让"先拒后批"变成不可能，
        # 而它同样是**静默**的（症状迟到：第二次批准同一版时）。
        Index(
            "uq_step_approvals_root",
            "project_id",
            "step_no",
            "subject_kind",
            "subject_version_id",
            unique=True,
            postgresql_where=text("supersedes_id IS NULL"),
        ),
        Index("ix_step_approvals_project_step", "project_id", "step_no", "decided_at"),
        Index("ix_step_approvals_subject_version_id", "subject_version_id"),
        Index("ix_step_approvals_supersedes_id", "supersedes_id"),
    )


# 不可变保护：ORM 事件（本调用）+ 数据库触发器（`ddl.py`，迁移中建立）双重守卫。
# 理由见 `platform/immutability.py` 与各表的 docstring。
guard_immutable(ProductionBaseline, BaselineItem, StepApproval)


__all__ = [
    "BASELINE_PAYLOAD_SCHEMA_VERSION",
    "IMMUTABLE_TABLES",
    "BaselineItem",
    "InformationDraft",
    "ProductionBaseline",
    "StepApproval",
]
