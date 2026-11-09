"""Brief 结构化解构的五张表（数据结构 02 §6）。

| 表 | 性质 | 可变性 |
|---|---|---|
| `briefs` | 一项目一个逻辑 Brief（版本与草稿的挂载点） | **可变**（`active_version_id` 会移动） |
| `brief_drafts` | 审核工作区（字段 / 结论 / 疑问处置） | **可变**（`lock_version`，ETag 载体） |
| `brief_versions` | 固化版本（**不可变**，`id` 同时 FK `version_nodes.id`） | 不可变 |
| `brief_claims` | 版本内的结论（**不可变**，随版本冻结） | 不可变 |
| `brief_issues` | 版本内的疑问（**不可变**，随版本冻结） | 不可变 |

---

## 为什么 claims / issues 与 versions 一起不可变

数据结构 02 §6 的末尾只有一句约束性说明：

> `brief_claims.resolution_status` 是版本内快照字段；对已固化版本的确认结果
> 通过新版本或审核事件表达，**不直接 UPDATE 历史 claim**。

这句话的判据不是"claims 特殊"，而是**版本内容不可变**（02 §5）：
"这一版当时说哪些结论成立、哪些还不确定"本身就是版本内容的一部分。
若允许原地把 `PENDING` 改成 `CONFIRMED`，那么"这一版被确认过吗"
的答案就取决于"有没有人改过它"，而**没有人能审计这件事** ——
与 WP11 的 `version_nodes`、WP17 的 `source_segments` 是同一个论证
（`platform/immutability.py` 的模块 docstring 写了完整版）。

`brief_issues` 同理：它是"这一版带着哪些疑问固化下来的"的记录。
疑问的**处置**发生在草稿里（`brief_drafts.issue_resolutions`），
固化时把处置结果一并冻结 —— 于是"处置"这件事本身是可追溯的，
而不是散落在若干次 UPDATE 里。

需求 §227 也支持这条：**"新上传 Brief 形成新版本，不覆盖旧版"** ——
要改结论就出新版本，不存在"改一版已经固化的 Brief"。

## 为什么 `brief_versions` 的两个外键是 `DEFERRABLE INITIALLY DEFERRED`

`brief_versions.id` 指向 `version_nodes.id`（数据结构 02 §5：
"每个具体版本表的 `id` 同时 FK `version_nodes.id`；注册表、具体表及引用
**同一事务插入**"）。同事务插入意味着两行的插入顺序由应用决定，
而 WP11 的 `register_version` 内部用 SAVEPOINT + flush 重试取修订号 ——
**它不会让调用方控制节点行的插入时机**。延迟到事务提交时再校验，
让"谁先谁后"不再是一个必须猜对的问题。

`briefs.active_version_id` 同理：它在固化事务里被更新成新版本，
而那个版本行在同一事务中插入。

## 本包对 WP17 `state='PARTIAL'` 的落库处理（硬约束）

`brief_drafts.source_state` 与 `brief_versions.source_state` 是**显式列**，
取值含 `PARTIAL` 且**独立于 `SUCCEEDED`**。三层保证：

1. **列与取值域**（本文件）：`source_state` 是受控枚举，`PARTIAL` 是一个独立值；
2. **草稿上的 CHECK**（本文件）：`source_state='PARTIAL'` 时
   `issue_resolutions` **必须非空** —— "缺了页"这个事实必须变成一条疑问，
   否则它会静静地躺在库里，没有任何下游看得到；
3. **版本上的延迟触发器**（`ddl.py`）：`source_state='PARTIAL'` 的版本
   必须有至少一条 `brief_issues` 行 —— 固化**不得把残缺洗白**。
   这一条必须放在触发器里，因为 `brief_issues` 是另一张表。

**为什么不做成"PARTIAL 就禁止固化"**：需求 §5.1 明确"缺失字段必须显示
'待补充'，不得由 AI 填成确定事实"，而 §5.3 的完成条件允许"原材料不足时
如实记录"。禁止固化会把一个**可以诚实交付**的状态变成死路，
而下游真正需要知道的是"这一版缺了什么"，不是"这一版不存在"。
因此本包的口径是：**允许固化，但残缺必须被记录且可查**。
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
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.brief.types import (
    CATEGORY_LIST_SQL,
    CLAIM_HASH_LENGTH,
    EVIDENCE_CATEGORY_LIST_SQL,
    ISSUE_STATE_LIST_SQL,
    MAX_CLAIM_CHARS,
    MAX_ISSUE_DESCRIPTION_CHARS,
    RESOLUTION_LIST_SQL,
    SEVERITY_LIST_SQL,
    SOURCE_STATE_LIST_SQL,
)
from rednote.platform.base import Base, created_at_column, updated_at_column, uuid_pk
from rednote.platform.immutability import guard_immutable

# 本模块需要不可变保护的表。`ddl.py` 里有同值副本（迁移要在不加载 ORM
# 元数据的前提下拿到表名），两处必须一致 —— 测试会交叉核对。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = (
    "brief_versions",
    "brief_claims",
    "brief_issues",
)

# `structured_fields` / `claims` / `issue_resolutions` 的形状版本。
# 与 WP11 的 `schema_version`、WP17 的 `position_schema_version` 同一手法：
# 写入方带上它，消费方据此选择解析方式（结构将来会扩展）。
BRIEF_SCHEMA_VERSION: Final[int] = 1

# 摘要字段（summary）的长度上限。它是给列表与审核人看的**摘要**，
# 不是正文容器 —— 不设上限会让某个实现顺手把整份 structured_fields
# 序列化进来，而列表接口的响应体会随之失控。
MAX_SUMMARY_CHARS: Final[int] = 2_000

# 产品名 / 型号的长度。与 WP10 的字典命名口径一致（200）。
MAX_PRODUCT_NAME_CHARS: Final[int] = 200
MAX_MODEL_CODE_CHARS: Final[int] = 100

# 字段路径的长度上限。`field_path` 是 `a.b.c` 形式的受控字符串，
# 200 远超最长路径（`product.supporting_materials` = 28）。
MAX_FIELD_PATH_CHARS: Final[int] = 200


class Brief(Base):
    """一项目一个逻辑 Brief。**可变**。

    `project_id` 唯一 —— 需求 §639 的表："一项目一个逻辑 Brief，多版本"。
    "逻辑 Brief" 跨版本不变，因此 `version_nodes.entity_id` 指向的就是本表的 `id`
    （见 WP11 的 `VersionNode` docstring："`entity_id` 是逻辑实体，`id` 才是版本"）。

    **为什么 `draft_id` 与 `active_version_id` 都可空**：
    项目刚创建时两者都不存在（`briefs` 行由"项目创建 / 上游结果"事务一并建出，
    但草稿要等第一次访问才需要）。可空让"还没开始"成为一个可表达的状态，
    而不是靠一个假 UUID 占位。

    **为什么没有 `lock_version`**：本表没有"客户端读出来、改几个字段、写回去"
    的路径 —— 它的两个可变列都由**服务端**在固化事务里设置。
    乐观锁用于携带旧值的场景，这里没有旧值要携带
    （与 WP12 的 `quota_accounts` 同一判断：更新是单条原子 UPDATE）。
    """

    __tablename__ = "briefs"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 当前生效版本。**延迟外键**：固化事务里先插版本行、再更新本列，
    # 也可能反过来 —— 延迟到提交时校验让顺序不再重要。
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "version_nodes.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )

    # 当前草稿。**与 `brief_drafts.brief_id` 构成循环外键**（两者都延迟）。
    # 保留这一列而不是"靠 UQ(brief_drafts.brief_id) 反查"，是因为
    # `GET /projects/{id}/brief` 的响应要一次给出草稿 id 与 ETag ——
    # 反查要多发一条查询，而这条查询恰好每次请求都要发。
    draft_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "brief_drafts.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        # 一项目一个逻辑 Brief（数据结构 02 §6）。
        # 用唯一约束而不是"应用层先查再插"：并发创建两个项目请求不会撞，
        # 但同一项目被两次初始化（事件重投）会 —— 那时唯一约束是唯一防线。
        UniqueConstraint("project_id", name="uq_briefs_project_id"),
        Index("ix_briefs_active_version_id", "active_version_id"),
    )


class BriefDraft(Base):
    """Brief 审核工作区。**可变**，`lock_version` 是 ETag 载体。

    `base_version_id` 是"这次编辑从哪一版开始"（WP11 的 `promote_draft`
    入参口径）—— **必须由编辑方声明**，服务端不猜：
    猜"最新版"会把一个用户在旧版上做的修改记成"基于最新版"，
    溯源链从此失真，而且没有任何报错。

    **为什么 `source_state` / `source_errors` 在草稿上**：
    草稿是"这一轮解构的产物"，而解构的输入完整度是它的属性。
    把它们放在草稿上，`PUT /brief-draft` 之后前端还能看到
    "这一轮的输入缺了第 7、12 页" —— 若只放在版本上，
    运营一旦开始编辑就再也看不到输入是残缺的。

    **为什么加厚建议也在草稿上**（`suggestions` / `suggestions_version_id`）：
    建议的消费者是**正在编辑的运营**，而不是"某一版固化下来的内容"。
    它必须与"当前草稿"同生命周期：固化一次之后，上一轮建议就不再适用
    （基准版本已经前移），因此 `solidify` 会把它清空。
    若把建议做成独立表，就要为它单独维护一套"什么时候过期"的规则 ——
    而"跟着草稿走、固化即清"这条规则本身已经足够。
    """

    __tablename__ = "brief_drafts"

    id: Mapped[uuid.UUID] = uuid_pk()

    brief_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("briefs.id", ondelete="RESTRICT"),
        nullable=False,
    )

    base_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("version_nodes.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 结构化字段。**每个字段的值与其状态在同一个对象里**
    # （`{"status": "FILLED", "value": ...}`），理由见 `schema.py`。
    structured_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # 结论列表（`ClaimDraft.to_json()` 的数组）。
    claims: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # 疑问列表（`IssueDraft.to_json()` 的数组）。
    #
    # 列名沿用数据结构 02 §6 的 `issue_resolutions`，但存的是**完整疑问对象**
    # （含 severity / description / state / resolution）而不是"id → 处置"的映射：
    # 映射形式会让"这条疑问是什么"只存在于别处，而草稿阶段那些疑问还没有
    # `brief_issues` 行可指（它们要到固化时才落库）。
    issue_resolutions: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # 加厚建议（协议 §5 第 5 行："返回分类建议，**不自动生效**"）。
    #
    # **为什么建议不进 `claims`**：`claims` 是"这份文档说了什么"的产物，
    # 而建议是"模型认为还应该补什么"。混在一起之后，运营再也分不清
    # "这条是原文里的"还是"这条是模型想出来的" —— 而那正是
    # 需求 §5.2 要求把外部证据与原文事实分开的原因。
    #
    # 运营采纳某条建议的路径是显式的：在 `PUT /brief-draft` 里
    # 把它加进 `claims`，那一步会走 `ClaimDraft` 的校验（事实类必须有来源）。
    suggestions: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # 建议是基于**哪一版**提出的。**可空**：还没有加厚过时为空。
    # 有了它，前端才能显示"这些建议基于 v3，而当前草稿的基准是 v4" ——
    # 没有它，一份过期的建议与一份新鲜的建议看起来完全一样。
    suggestions_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("version_nodes.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # **待处理的加厚请求**（`{"briefVersionId": …, "allowedSourceIds": [...]}`）。
    #
    # 为什么参数落在这里而不是 job 上：`jobs.result_ref` 是**结果**引用
    # （用输入占住它，任务终态时就放不下真正的产出）；`jobs.input_version_ids`
    # 只装 `version_nodes` 的 id，装不下 `allowedSourceIds`（那是
    # `source_segments` 的 id）。而新建一张"加厚请求表"会自创一张
    # 数据结构 02 §6 里没有的表。
    #
    # 它与 `suggestions` 是同一件事的两半（"请求了什么" / "产出了什么"），
    # 放在同一行上才不会出现"请求在、产物没了"的中间态。
    # **处理器读完即清空**：它是一次性的，留着只会让下一轮误读旧参数。
    #
    # **`none_as_null=True` 不是可选项**：默认情况下 SQLAlchemy 把 Python
    # `None` 序列化成 JSON 的 `null`（一个**非空**的 JSON 值），
    # 于是清空时写进去的是 `'null'::jsonb` 而不是 SQL NULL，
    # `ck_brief_drafts_enrichment_request_is_object` 会以
    # `jsonb_typeof('null'::jsonb) = 'null' ≠ 'object'` 拒绝 —— 表现为
    # "清空加厚请求"这个动作本身失败。加上它之后 `None` 才真的落成 NULL。
    enrichment_request: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )

    # 本轮解构的输入来源。可空：全新草稿还没有解析过。
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("file_objects.id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_parse_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("parse_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # 输入完整度。**`PARTIAL` 是独立取值**（见模块 docstring）。
    source_state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'NONE'")
    )
    # 解析的错误明细（WP17 的 `parse_runs.errors` 原样带上）。
    # 只存 `{unit, code, message(截断)}` —— 不放正文（WP17 §1.4 第 6 条）。
    source_errors: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(BRIEF_SCHEMA_VERSION))
    )

    # 乐观锁 / ETag 载体（协议 §1.1：可变资源修改要求其当前 ETag）。
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        # 一个逻辑 Brief 一个草稿（多轮编辑复用同一个草稿，见协议 §5 末段
        # "正式提交后保留新一轮草稿入口"）。
        UniqueConstraint("brief_id", name="uq_brief_drafts_brief_id"),
        CheckConstraint(
            f"source_state IN ({SOURCE_STATE_LIST_SQL})", name="source_state_valid"
        ),
        CheckConstraint("lock_version >= 1", name="lock_version_positive"),
        CheckConstraint(
            "schema_version >= 1", name="schema_version_positive"
        ),
        # jsonb 的形状必须在数据库层也是确定的 —— 否则一个写入方把
        # `structured_fields` 写成数组，下游每次读都要先判类型。
        CheckConstraint(
            "jsonb_typeof(structured_fields) = 'object'",
            name="structured_fields_is_object",
        ),
        CheckConstraint(
            "jsonb_typeof(claims) = 'array'", name="claims_is_array"
        ),
        CheckConstraint(
            "jsonb_typeof(issue_resolutions) = 'array'",
            name="issue_resolutions_is_array",
        ),
        CheckConstraint(
            "jsonb_typeof(suggestions) = 'array'", name="suggestions_is_array"
        ),
        # 待处理的加厚请求必须是对象（它有两个具名字段），不是数组。
        CheckConstraint(
            "enrichment_request IS NULL"
            " OR jsonb_typeof(enrichment_request) = 'object'",
            name="enrichment_request_is_object",
        ),
        # 建议必须能说清"基于哪一版"：有建议却没有版本 = 一批无从判断
        # 新鲜度的建议。反方向（有版本没建议）是正常的 ——
        # 固化后建议被清空但版本保留，用来显示"上一轮加厚基于 v3"。
        CheckConstraint(
            "jsonb_array_length(suggestions) = 0"
            " OR suggestions_version_id IS NOT NULL",
            name="suggestions_require_version",
        ),
        CheckConstraint(
            "jsonb_typeof(source_errors) = 'array'", name="source_errors_is_array"
        ),
        # **本包对 PARTIAL 的第二层保证**（见模块 docstring）：
        # 输入是"部分成功"时，草稿里**必须**至少有一条疑问。
        # 不加这一条，PARTIAL 会静静地躺在库里 —— 字段缺了、疑问没有，
        # 而"必填字段已填写或明确不适用"这条完成条件会在一堆空洞上通过。
        CheckConstraint(
            "source_state <> 'PARTIAL'"
            " OR jsonb_array_length(issue_resolutions) > 0",
            name="partial_requires_issue",
        ),
        # "上一版"必须与本次解构来源一致才能算作基准 —— 这条无法用 CHECK
        # 表达（跨表），由 service 层校验。这里只索引查询路径。
        Index("ix_brief_drafts_base_version_id", "base_version_id"),
        Index("ix_brief_drafts_source_parse_run_id", "source_parse_run_id"),
        Index("ix_brief_drafts_suggestions_version_id", "suggestions_version_id"),
    )


class BriefVersion(Base):
    """Brief 的固化版本。**不可变**。

    `id` 同时 FK `version_nodes.id`（数据结构 02 §5 原句）。
    `product_name` / `model_code` 是 `structured_fields` 的**投影列** ——
    冗余是有意的：版本列表与"这个产品做过几版 Brief"的查询
    不该为了拿一个产品名而解析 20 个字段的 jsonb。
    """

    __tablename__ = "brief_versions"

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

    brief_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("briefs.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 来源文件。**非空**：需求 §5.1 原文"从上传主 Brief 开始，保留原文件、
    # 文件摘要、上传人、时间、解析版本" —— 一个无法回溯到原始文档的
    # Brief 版本，其所有"原文事实"都没有依据。
    source_file_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("file_objects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 产出版本的解析运行。**可空**（历史数据/手工补录），但只要有 claim
    # 引用了片段，它就必然非空 —— 那条不变量由 `ddl.py` 的延迟触发器保证。
    source_parse_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("parse_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # 输入完整度。**从草稿复制，不在固化时重算** —— 重算会让
    # "固化把残缺洗白"成为可能（重新解析一次就变 SUCCEEDED 了）。
    source_state: Mapped[str] = mapped_column(String(20), nullable=False)

    product_name: Mapped[str] = mapped_column(
        String(MAX_PRODUCT_NAME_CHARS), nullable=False
    )
    model_code: Mapped[str | None] = mapped_column(
        String(MAX_MODEL_CODE_CHARS), nullable=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    structured_fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(BRIEF_SCHEMA_VERSION))
    )

    created_at: Mapped[datetime] = created_at_column()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            f"source_state IN ({SOURCE_STATE_LIST_SQL})", name="source_state_valid"
        ),
        CheckConstraint(
            "char_length(product_name) > 0", name="product_name_not_empty"
        ),
        CheckConstraint("char_length(summary) > 0", name="summary_not_empty"),
        CheckConstraint(
            f"char_length(summary) <= {MAX_SUMMARY_CHARS}", name="summary_length"
        ),
        CheckConstraint(
            "jsonb_typeof(structured_fields) = 'object'",
            name="structured_fields_is_object",
        ),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        # 版本列表的查询路径（按 brief 取全部版本，最新在前）。
        Index("ix_brief_versions_brief_id", "brief_id"),
        # 来源文件的溯源路径："这个文件产出过哪些 Brief 版本"。
        Index("ix_brief_versions_source_file_id", "source_file_id"),
        Index("ix_brief_versions_source_parse_run_id", "source_parse_run_id"),
    )


class BriefClaim(Base):
    """版本内的一条结论。**不可变**（随版本冻结）。

    验收原文之一是「**分类来源**」。它拆成两件必须同时成立的事：

    | 问题 | 落点 |
    |---|---|
    | 这条结论属于哪一类？ | `category` 非空 + 受控枚举 |
    | 它凭什么成立？ | `category` 是事实类时**必须有证据**（本表的 CHECK） |
    | 证据是真的吗？ | 引用的片段必须来自本版本解析的那份文档（`ddl.py` 的延迟触发器） |

    第三行是本包最要紧的一条：**只要求"有 evidence_segment_id"是不够的** ——
    一个指向别的项目、别的文件的片段 id 同样能满足它，而那是**假溯源**，
    比"没有来源"更危险（它看起来可追溯）。因此那条不变量必须跨表校验。
    """

    __tablename__ = "brief_claims"

    id: Mapped[uuid.UUID] = uuid_pk()

    brief_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("brief_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 指向 `BriefSchema` 的字段路径（受控，见 `schema.py` 的 `field_spec`）。
    field_path: Mapped[str] = mapped_column(
        String(MAX_FIELD_PATH_CHARS), nullable=False
    )

    # 结论文本。列名按数据结构 02 §6（`claim_text`）；
    # 协议 §5.1 叫 `text`，映射在 api 层。
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)

    category: Mapped[str] = mapped_column(String(30), nullable=False)

    # 主证据：原文片段（WP17 的 `source_segments.id`）。
    evidence_segment_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("source_segments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # 主证据：外部链接（`EXTERNAL_EVIDENCE` 的来源通常是这个）。
    evidence_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # 适用范围：`{"productIds": [...], "modelCodes": [...]}`。
    # 空对象 = 不限定（适用于本 Brief 的全部产品/型号）。
    applicability: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    resolution_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'PENDING'")
    )
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 结论摘要（SHA-256）。用途与 WP17 的 `source_segments.content_hash` 一致：
    #   1. 参与唯一约束（同字段同文本 = 重复结论，挡住跑偏的解构）；
    #   2. 让下游能按摘要比对，而不必把正文拉进内存比较。
    claim_hash: Mapped[str] = mapped_column(
        String(CLAIM_HASH_LENGTH), nullable=False
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        # 同版本、同字段、同文本的结论只能有一条。
        # **不是**"同文本全局唯一"：不同字段出现同一句话是合法的
        # （例如"温和不刺激"既可能出现在卖点也可能出现在功效里）。
        UniqueConstraint(
            "brief_version_id",
            "field_path",
            "claim_hash",
            name="uq_brief_claims_version_field_hash",
        ),
        CheckConstraint(
            f"category IN ({CATEGORY_LIST_SQL})", name="category_valid"
        ),
        # **"分类来源"的第一条结构性保证**：事实类必须有证据。
        # 与 `ClaimDraft.__post_init__` 同一判据，两层都要 ——
        # 构造器给可读错误，本约束管住绕过构造器的写入（数据回填、手工 SQL）。
        CheckConstraint(
            f"category NOT IN ({EVIDENCE_CATEGORY_LIST_SQL})"
            " OR evidence_segment_id IS NOT NULL"
            " OR (evidence_url IS NOT NULL AND char_length(evidence_url) > 0)",
            name="factual_category_requires_evidence",
        ),
        CheckConstraint(
            f"resolution_status IN ({RESOLUTION_LIST_SQL})",
            name="resolution_status_valid",
        ),
        # 处置必须留理由 —— 与 `IssueDraft` 的同一判据：
        # 没有理由的"不适用"在下一次审查时无法复核。
        CheckConstraint(
            "resolution_status <> 'NOT_APPLICABLE'"
            " OR (resolution_reason IS NOT NULL"
            " AND char_length(resolution_reason) > 0)",
            name="not_applicable_requires_reason",
        ),
        # "确认"必须有人 —— 一条无人确认的 CONFIRMED 与 PENDING 在语义上
        # 没有区别，只是前者看起来更可信。
        CheckConstraint(
            "resolution_status <> 'CONFIRMED' OR confirmed_by IS NOT NULL",
            name="confirmed_requires_actor",
        ),
        CheckConstraint(
            "char_length(claim_text) > 0", name="claim_text_not_empty"
        ),
        CheckConstraint(
            f"char_length(claim_text) <= {MAX_CLAIM_CHARS}", name="claim_text_length"
        ),
        CheckConstraint(
            "char_length(field_path) > 0", name="field_path_not_empty"
        ),
        CheckConstraint(
            f"char_length(claim_hash) = {CLAIM_HASH_LENGTH}", name="claim_hash_length"
        ),
        CheckConstraint(
            "jsonb_typeof(applicability) = 'object'", name="applicability_is_object"
        ),
        # 按字段取结论（前端按字段分组展示"这个字段的依据是什么"）。
        Index("ix_brief_claims_version_field", "brief_version_id", "field_path"),
        # **部分索引**：本版还有哪些待确认结论。
        # 已确认/不适用的结论会随时间累积，而"待确认"始终是小集合 ——
        # 全量索引会让这个最高频的查询跟着表一起变大。
        Index(
            "ix_brief_claims_pending",
            "brief_version_id",
            postgresql_where=text("resolution_status = 'PENDING'"),
        ),
        # 溯源反查："这条片段被哪些结论引用"（WP34 影响分析的入口）。
        Index("ix_brief_claims_evidence_segment_id", "evidence_segment_id"),
    )


class BriefIssue(Base):
    """版本内的一条疑问。**不可变**（随版本冻结）。

    验收原文之三是「**确认版本闭环**」。闭环的四个环节：

    1. **产生** —— 解构（或 PARTIAL 检测）产出疑问，落在 `brief_drafts`
       的 `issue_resolutions` 里；
    2. **处置** —— 运营在 `PUT /brief-draft` 里回答（`state` + `resolution`）；
    3. **阻断** —— 未处置的 `BLOCK` 疑问**拒绝固化版本**（service 层报 409）；
    4. **留痕** —— 固化时疑问随版本落库并冻结，`GET /brief` 可查
       "这一版当时带着哪些提醒"。

    第 3 步在数据库层也有表述（`blocking_issue_cannot_be_open`），
    它防的是**服务层回归**：某天有人改了固化校验而没改这里，
    一条"带着未答复核心疑问的版本"就会写进去 —— 而那正是本包存在的理由。

    **为什么 `project_id` 在版本表上重复存在**（数据结构 02 §6 列了它）：
    它让"本项目还有哪些未处置疑问"不必 join 三张表。
    代价是它必须与版本所属项目一致 —— 由 `ddl.py` 的延迟触发器保证
    （跨行不变量无法用 CHECK 表达）。
    """

    __tablename__ = "brief_issues"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    brief_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("brief_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )

    field_path: Mapped[str] = mapped_column(
        String(MAX_FIELD_PATH_CHARS), nullable=False
    )
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            f"severity IN ({SEVERITY_LIST_SQL})", name="severity_valid"
        ),
        CheckConstraint(
            f"state IN ({ISSUE_STATE_LIST_SQL})", name="state_valid"
        ),
        # **"核心疑问未解决阻断步骤 1" 的数据库级表述**
        # （数据结构 02 §6 原句 + 需求 §5.3）。
        CheckConstraint(
            "severity <> 'BLOCK' OR state <> 'OPEN'",
            name="blocking_issue_cannot_be_open",
        ),
        # 未处置的疑问不该有处置理由；已处置的必须有。
        # 两个方向都要：只写一个方向会让"已解决但没说为什么"进来，
        # 而那条记录在下一次审查时无法复核。
        CheckConstraint(
            "state <> 'OPEN'"
            " OR (resolution IS NULL AND resolved_by IS NULL)",
            name="open_issue_has_no_resolution",
        ),
        CheckConstraint(
            "state = 'OPEN'"
            " OR (resolution IS NOT NULL AND char_length(resolution) > 0"
            " AND resolved_by IS NOT NULL)",
            name="settled_issue_has_resolution",
        ),
        CheckConstraint(
            "char_length(description) > 0", name="description_not_empty"
        ),
        CheckConstraint(
            f"char_length(description) <= {MAX_ISSUE_DESCRIPTION_CHARS}",
            name="description_length",
        ),
        CheckConstraint(
            "char_length(field_path) > 0", name="field_path_not_empty"
        ),
        # 按版本读疑问（GET 一个版本的全部疑问）。
        Index("ix_brief_issues_version_severity", "brief_version_id", "severity"),
        # **部分索引**：本项目还有哪些未处置疑问。
        # 已处置的疑问会随版本累积，而"未处置"是小集合 —— 同上。
        Index(
            "ix_brief_issues_open",
            "project_id",
            "severity",
            postgresql_where=text("state = 'OPEN'"),
        ),
    )


# **模块级**注册不可变守卫（与 WP11/WP15/WP16/WP17 同一手法）：
# 只要 import 了模型，ORM 层守卫就已生效，不依赖初始化顺序。
guard_immutable(BriefVersion, BriefClaim, BriefIssue)


__all__ = [
    "BRIEF_SCHEMA_VERSION",
    "IMMUTABLE_TABLES",
    "MAX_FIELD_PATH_CHARS",
    "MAX_MODEL_CODE_CHARS",
    "MAX_PRODUCT_NAME_CHARS",
    "MAX_SUMMARY_CHARS",
    "Brief",
    "BriefClaim",
    "BriefDraft",
    "BriefIssue",
    "BriefVersion",
]
