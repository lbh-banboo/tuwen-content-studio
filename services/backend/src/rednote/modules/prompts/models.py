"""Prompt 三张表的存储模型（WP23）。

规格源 02 §8 的三张表：

| 表 | 规格源原文 |
|---|---|
| `prompt_templates` | id, task_kind, name, active_version_id? —— **管理模板身份** |
| `prompt_versions` | id PK/FK version_nodes, template_id FK, system_template, input_schema jsonb, output_schema jsonb, allowed_tools jsonb, change_reason —— **模板版本与模型版本分别记录** |
| （本包新增）`prompt_publications` | 激活事件 —— 协议 03 §9 第 13 行「激活，**记录影响范围**」的落点 |

---

## 四处需要解释的设计

### 1. 为什么"模板身份"与"版本"必须是两张表

规格源 02 §160 只写了"管理模板身份"四个字，但它与 §161 的
`prompt_versions`（`id PK/FK version_nodes`）**并列**，已经说明了分层。
若合成一张表，**模板改名会产生一个"新版本"** —— 而改名不是内容变化，
却会让 `version_nodes` 里多一个节点、让"这次改动影响了什么"变成"什么都没影响"。
这与 WP20 的 `knowledge_assets` / `knowledge_versions` 完全同构。

### 2. `prompt_versions.id` 同时是主键与外键（指向 `version_nodes`）

规格源原文：`id PK/FK version_nodes`。与 `brief_versions` / `knowledge_versions`
完全同构，理由见 WP11 §5：注册表、具体表及引用**同一事务插入**。

**延迟外键**（`DEFERRABLE INITIALLY DEFERRED`）：同事务插入意味着两行的顺序
由应用决定，而 `register_version` 内部用 SAVEPOINT + 重试取号，
不暴露"节点行何时落库"的控制权（WP18 已实测：延迟触发器在 INSERT 时静默通过、
在 COMMIT 时抛 `sqlstate=23514`）。

### 3. `prompt_publications` 是本包**新增**的表

规格源 02 §8 里**没有**这张表 —— 但协议 03 §9 第 13 行要求
「`POST /prompts/{id}/publications`：激活，**记录影响范围**」。
"记录"必须有落点。两个选择：

| 方案 | 问题 |
|---|---|
| 在 `prompt_templates.active_version_id` 上就地改 | 只有"当前是谁"，**没有历史** —— 而验收原文要求「变更可追溯」 |
| 新增一张事件表（本包采用） | 与 WP20 的 `knowledge_publications` 同构；"影响范围"有地方放 |

**为什么与 `knowledge_publications` 的动作集不同**（`ACTIVATE`/`SUPERSEDE`
而不是四种）：知识有 `ARCHIVE`/`RESTORE`（整条资产退出/恢复可用），
而模板"停用"的语义协议没定义（见说明文档 §1.4 疏漏 #8）。
**照抄知识的动作集等于凭空发明一个语义** —— 那是越界。

### 4. `allowed_tools` 用 `text[]` 而不是 `jsonb`（**这是本包最要紧的一处**）

规格源 02 §161 写的是 `allowed_tools jsonb`。**本包改用 `text[]`**，
理由是**验收原文的落点取决于它**：

> 验收原文：「**不扩大工具权限**」。协议 §424 要求 `allowed_tools`
> 「由平台配置管理」，普通模板编辑不能扩大。

用 `jsonb` 时，数据库层**无法**用 CHECK 表达"是本平台授予集合的子集" ——
需要 `jsonb` 数组元素逐个比对，PostgreSQL 没有直接语法（要写函数 + 触发器，
而 jsonb 的元素类型还不受约束：`["a", 1, {"b":2}]` 是合法 jsonb）。

用 `text[]` 时，一行 CHECK 就够：

    allowed_tools <@ ARRAY['KNOWLEDGE_READ', ...]::text[]

**`<@` 是"被包含于"（is contained by）**，正好是子集关系。

代价（诚实记录）：与规格源 §161 的列类型不一致，已在迁移与说明文档里标注。
**取舍判据**：验收原文的"不扩大"必须有**绕过 service 也拦得住**的防线，
而"与规格源的列类型一致"是形式要求。**能落成列是设计约束，不是风格偏好** ——
这一点在说明文档 §3 的 V3 对照里详细论证过。
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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.prompts.types import (
    MAX_CHANGE_REASON_CHARS,
    MAX_NAME_CHARS,
    MAX_REASON_CHARS,
    MAX_TEMPLATE_CHARS,
    MIN_PUBLICATION_SEQUENCE,
    PROMPT_SCHEMA_VERSION,
    PUBLICATION_ACTION_LIST_SQL,
    TASK_KIND_LIST_SQL,
    TOOL_CEILING_ARRAY_SQL,
)
from rednote.platform.base import Base, created_at_column, updated_at_column, uuid_pk
from rednote.platform.immutability import guard_immutable

# 本模块需要不可变保护的表。`ddl.py` 里有同值副本（迁移要在不加载 ORM
# 元数据的前提下拿到表名），两处必须一致 —— 测试会交叉核对。
#
# **为什么 `prompt_publications` 也在其中**：它是**激活事件**表。
# 事件一旦写上就是历史事实。改一条事件 = 篡改"这版提示词什么时候生效过"的答案，
# 而那正是验收原文「模板变更可追溯」所依赖的东西。
#
# **为什么 `prompt_templates` 不在其中**：它有 `active_version_id`
# 这一列**投影**（见下），投影必须能被更新。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = (
    "prompt_versions",
    "prompt_publications",
)

# `input_schema` / `output_schema` 的形状版本。
PROMPT_PAYLOAD_SCHEMA_VERSION: Final[int] = PROMPT_SCHEMA_VERSION


class PromptTemplate(Base):
    """一个提示词模板的**身份**。**可变**（`active_version_id` 是被投影的列）。

    **`active_version_id` 是一列投影，不是权威**（这是本表最要紧的一句话）：
    权威是 `prompt_publications` 的事件流。本列的作用是让
    "现在用的是哪版"不用扫事件表 —— 与 WP20 的
    `knowledge_assets.active_version_id` 同一手法。

    **它是可重建的**：`service.rebuild_active_version` 能从事件流重算它
    （测试会验证"重算结果与增量维护的结果一致"）。

    **`task_kind` 落 CHECK**：取值域在 `types.py` 单一来源定义，
    不落 CHECK 的话 `?taskKind=随便写` 会静默返回空列表 —— 用户以为
    "没有这个模板"，实际是"拼错了"。
    """

    __tablename__ = "prompt_templates"

    id: Mapped[uuid.UUID] = uuid_pk()

    task_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(MAX_NAME_CHARS), nullable=False)

    # 当前激活版本。`ON DELETE RESTRICT`：版本不可变（触发器已保证），
    # 因此这个 RESTRICT 永远不触发，是纯防御。
    #
    # **必须 `DEFERRABLE INITIALLY DEFERRED`（ISS-107）**：激活事务里
    # 版本行与模板行由**同一事务、两条不同代码路径**写入
    # （`versions.service.register_version` 内部有 SAVEPOINT + 重试取号，
    # 不暴露"节点行何时落库"）—— 立即校验会逼应用猜对插入顺序。
    # 迁移 `3cb74d02edc5` 一直是延迟的，但模型这里**曾漏写**：
    # 夹具用 `create_all` 按模型重建表，会把迁移的延迟 FK 静默"治愈"成
    # 非延迟，于是同一个库"迁移建出"与"夹具建出"的 FK 形态不一致。
    # 与 `briefs.active_version_id` / `knowledge_assets.active_version_id`
    # 同构（那两处模型里都写了 deferrable）。
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "prompt_versions.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint(
            f"task_kind IN ({TASK_KIND_LIST_SQL})", name="task_kind_valid"
        ),
        CheckConstraint("char_length(name) > 0", name="name_not_empty"),
        # 同一 task_kind 下模板名唯一。
        # **为什么不是全局唯一**：`CONTENT_COPY` 与 `REPORT_GENERATE` 各有
        # 一个叫"标准版"的模板是完全正常的，而全局唯一会拒绝它。
        UniqueConstraint("task_kind", "name", name="uq_prompt_templates_kind_name"),
        # 列表端点的查询路径：按 task_kind 筛选 + 按创建时间倒序翻页。
        Index("ix_prompt_templates_task_kind", "task_kind", "created_at"),
        Index("ix_prompt_templates_created_at", "created_at"),
    )


class PromptVersion(Base):
    """模板的一个版本。**不可变**（`id` 同时 FK `version_nodes.id`）。

    `system_template` 是发给模型的系统提示词文本，
    `input_schema` 声明模板能用哪些字段（`render.py` 拿它做"可渲染"校验），
    `output_schema` 声明期望的输出结构（下游解析用），
    `allowed_tools` 是**这一版**允许使用的工具（必须 ⊆ 平台上界）。

    **`allowed_tools` 用 `text[]` + CHECK 兜底**（见模块 docstring 第 4 点）。
    **`change_reason` 非空**：版本不可变，这份理由是一经写入就无法补充的
    唯一解释 —— 允许为空会让"为什么又发了一版"永远没有答案。
    """

    __tablename__ = "prompt_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "version_nodes.id",
            ondelete="RESTRICT",
            # 延迟：注册表与具体表同事务插入，顺序由 WP11 的取号重试逻辑决定。
            deferrable=True,
            initially="DEFERRED",
        ),
        primary_key=True,
    )

    template_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("prompt_templates.id", ondelete="RESTRICT"),
        nullable=False,
    )

    system_template: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # 见模块 docstring 第 4 点：`text[]` 而不是 `jsonb`，为的是 CHECK 能表达子集。
    allowed_tools: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )

    change_reason: Mapped[str] = mapped_column(
        String(MAX_CHANGE_REASON_CHARS), nullable=False
    )

    schema_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text(str(PROMPT_PAYLOAD_SCHEMA_VERSION)),
    )

    created_at: Mapped[datetime] = created_at_column()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "char_length(system_template) > 0", name="system_template_not_empty"
        ),
        CheckConstraint(
            f"char_length(system_template) <= {MAX_TEMPLATE_CHARS}",
            name="system_template_length",
        ),
        CheckConstraint(
            "char_length(change_reason) > 0", name="change_reason_not_empty"
        ),
        CheckConstraint(
            f"char_length(change_reason) <= {MAX_CHANGE_REASON_CHARS}",
            name="change_reason_length",
        ),
        # --- 两个 schema 必须是 object -------------------------------------
        # 与 WP20 的 `structured_payload_is_object` 同一手法：
        # 数组/标量都不是"结构声明"。**不判非空** —— 一个不需要输入字段的
        # 模板（例如纯格式转换）写 `{}` 是合法的，而 WP20 判非空是因为
        # 那里 `structured_payload` 承载的是"事实字段"，空对象等于没写事实。
        # 两处的判据不同，因为**语义不同**（这是刻意的，不是不一致）。
        CheckConstraint(
            "jsonb_typeof(input_schema) = 'object'", name="input_schema_is_object"
        ),
        CheckConstraint(
            "jsonb_typeof(output_schema) = 'object'", name="output_schema_is_object"
        ),
        # --- 验收原文「不扩大工具权限」的**数据库层兜底** --------------------
        # `<@` 是 PostgreSQL 的"被包含于"（is contained by），正好是子集关系。
        #
        # **为什么这条必须在数据库层**：service 层的校验给的是可读错误，
        # 但绕过 service 的写入路径（数据回填、导入脚本、psql 排障）
        # 它管不住。而"扩权"的失败形态是**静默的** ——
        # 没有任何异常，只是某次生成多了一个它能调的工具。
        #
        # **`COALESCE` 不必要**：列是 NOT NULL + 默认空数组，
        # 空数组 `<@` 任何数组恒为真（空集是任何集合的子集）—— 正确。
        CheckConstraint(
            f"allowed_tools <@ {TOOL_CEILING_ARRAY_SQL}",
            name="allowed_tools_within_ceiling",
        ),
        # 取某模板的全部版本（按创建时间倒序翻页）。
        Index("ix_prompt_versions_template_id", "template_id", "created_at"),
    )


class PromptPublication(Base):
    """一次激活事件。**不可变**（协议 §9 第 13 行的"激活"）。

    | action | 含义 |
    |---|---|
    | `ACTIVATE` | 某版本开始生效（同时切换 `active_version_id`） |
    | `SUPERSEDE` | 一个版本被取代（由 ACTIVATE 自动产生） |

    **`sequence` 列是本包新增的**（与 WP20 的 `knowledge_publications.sequence`
    同一理由）：`now()` 在 PostgreSQL 里是**事务开始时刻**，
    同一事务内插入的 `ACTIVATE(v2)` 与 `SUPERSEDE(v1)` 时间戳完全相同，
    按 `created_at` 排序得到的是**不确定的顺序**（WP17 ISS-52 踩过）。

    **`affected_task_kinds` 是本包新增的**：协议原文「激活，**记录影响范围**」。
    只记 `version_id` 的话，"影响范围"是空的 —— 出事时查不出
    "这次激活影响了哪些任务"。它冗余存一份 `task_kind` 快照而不是 JOIN 回
    `prompt_templates`，理由：**事件是历史事实**，而 `task_kind` 将来若被改，
    JOIN 出来的会是"现在的值"而不是"当时的值"。
    """

    __tablename__ = "prompt_publications"

    id: Mapped[uuid.UUID] = uuid_pk()

    template_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("prompt_templates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("prompt_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False)

    # 事件在**本模板内**的序号（从 1 开始）。投影的排序键。
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    # 被这次激活取代的那一版（`ACTIVATE` 时填，`SUPERSEDE` 时为 NULL）。
    superseded_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("prompt_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 「记录影响范围」的落点（见类 docstring）。
    affected_task_kinds: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(
        String(MAX_REASON_CHARS), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            f"action IN ({PUBLICATION_ACTION_LIST_SQL})", name="action_valid"
        ),
        CheckConstraint(
            f"sequence >= {MIN_PUBLICATION_SEQUENCE}",
            name="sequence_positive",
        ),
        # 同一模板内序号唯一 —— 这是并发激活时"两人同时取到同一个号"
        # 的拦截面（先查再插之间有窗口，唯一约束是唯一防线，同 WP11/WP20）。
        UniqueConstraint(
            "template_id", "sequence", name="uq_prompt_publications_template_sequence"
        ),
        # 取某模板的事件流（投影的输入）：按序号升序。
        Index(
            "ix_prompt_publications_template_sequence", "template_id", "sequence"
        ),
        # 反查"哪些激活用过这个版本"（排障）。
        Index("ix_prompt_publications_version_id", "version_id"),
    )


guard_immutable(PromptVersion, PromptPublication)


__all__ = [
    "IMMUTABLE_TABLES",
    "PROMPT_PAYLOAD_SCHEMA_VERSION",
    "PromptPublication",
    "PromptTemplate",
    "PromptVersion",
]
