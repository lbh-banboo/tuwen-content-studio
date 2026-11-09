"""WP30 AI 自检的存储模型：三张**全部不可变**的表。

| 表 | 角色 | 可变性 |
|---|---|---|
| `self_check_runs` | 一轮自检（判的是哪一版、用的哪版规则、聚合结论与五个计数） | **不可变**（重跑 = 追加一行） |
| `self_check_rule_results` | 一轮 × 一规则 = 一行（`PASSED`/`FAILED`/`SKIPPED`/`ERRORED`） | **不可变** |
| `self_check_findings` | 一条命中（锚点三选一 + 机器可判 evidence + 回指 AI 反馈） | **不可变** |

## 为什么三张表全不可变（与 WP29 的 `content_feedback_items` 恰好相反）

自检结论是**观测**，不是工作区。"这一刻的规则对这一版稿子给出了什么结论"
是一个历史事实；需要新结论就再跑一轮、再追加一行。原地改会让
"上周判通过、今天判阻断"无从解释（也无从区分"稿子变了"与"规则变了"）。
运营要推进的状态机落在 WP29 的反馈行上 —— 本包只**建**反馈、不**改**反馈。

因此本包**零 `If-Match`**：三张表没有"原地改"的路径 ⇒ 没有乐观锁载体，
也没有"读-写窗口"要挡。`POST /contents/{id}/self-checks` 并发两次的结果是
**两轮**自检记录，这是正确行为而不是冲突。唯一要防的是"同一阻断项被两轮
各建一条反馈"，那靠服务层的去重 + `(run_id, rule_code)` 唯一约束，不靠 ETag。

## `self_check_runs.outcome` 的四条**双向等价式** CHECK

```
(outcome = 'BLOCKED') = (blocking_count > 0)
(outcome = 'ERRORED') = (blocking_count = 0 AND errored_count > 0)
(outcome = 'WARNED')  = (blocking_count = 0 AND errored_count = 0 AND warning_count > 0)
(outcome = 'PASSED')  = (blocking_count = 0 AND errored_count = 0 AND warning_count = 0)
```

`outcome` 由服务层算、五个计数也由服务层写 ⇒ 两处都可能漏改，而表现是
"`outcome='PASSED'` 但 `blocking_count=1`"——读端点通常**只看 outcome**，
于是阻断内容显示为通过。把等价式下推到数据库，这个漂移就不可能落库。

**定序 `BLOCKED > ERRORED > WARNED > PASSED` 的理由**：
`BLOCKED` 已是"不能通过"的最强结论，降级成 `ERRORED` 会让前端丢掉
"为什么不能过"；而 `ERRORED`（无阻断）必须压过 `WARNED`/`PASSED` ——
"规则没跑成"绝不能被读成"没问题"（fail-closed）。

**`skipped_count` 刻意不参与 outcome**：未判既不是通过也不是不通过。
它单列，并在响应体里单独呈现。本包有三类恒 SKIPPED 的规则，若把 skipped
折算成 pass，「5 类判了 + 3 类没判」就会被渲染成「8 类全通过」——
而原型的批量通过按钮正是以"已完成自检、无阻断问题"为前提。

**`info_count` 也不参与 outcome**：`INFO` 是登记事实，不干预审核结论。

## `rule_code` 与 `skip_reason` 为什么**刻意不落取值域 CHECK**

两者都是**会增长的开集**，权威在 `rules.py` 的 `RULES` 元组与
`types.SKIP_REASONS`。若落 CHECK，按 ISS-191 它必须写成**字面量化石**：

1. 每加一条规则就要一次迁移；
2. 化石与注册表的分叉**不会报错** —— 库里能插的码变少，新规则的结果
   静默写不进去（一条规则"跑了但没留痕"）。

而 `category` / `rule_kind` / `status` / `severity` / `outcome` /
`trigger_kind` / `anchor_kind` 是**闭集**（统计口径与审核闸的消费对象），
必须落 CHECK。

代偿：服务层写入前对 `rules.by_code` / `types.SKIP_REASONS` 做 membership
校验（未知值抛 422，不落库），另有测试断言"注册表里每个 code 都 ≤ 60 字节
且无重复"。**这一条必须写进迁移的 docstring**，否则下一个人会"顺手补上"
而把开集冻成化石。

## `self_check_findings` 为什么**不带** `run_id` / `rule_code` / `category` / `severity`

本仓的纪律是"每个投影列都要配一条触发器钉住，否则是第二处声称"。
这四列省下的只是一次 join（`findings → rule_results → runs`），
买不起四条触发器。WP29 让 `content_feedback_items.state` 做投影之所以成立，
是因为它有真实的读路径收益（工作台按 state 筛）且配了判据。

## 跨表归属为什么不在模型层表达

- `subject_version_id` 是否属于 `content_item_id`、`content_item_id` 是否
  属于 `project_id`；
- finding 的 `media_item_id` / `feedback_item_id` 是否属于**本轮的**那条内容。

四条都**不是 FK 能表达的**（FK 只保证被引用行存在），且后两条跨两跳 join。
它们由 `ddl.py` 的延迟约束触发器在提交时检查。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.review.types import (
    MAX_ERROR_NOTE_CHARS,
    MAX_FINDING_MESSAGE_CHARS,
    MAX_PARAGRAPH_REF_CHARS,
    MAX_RULE_CODE_CHARS,
    MAX_SKIP_REASON_CHARS,
    REVIEW_ANCHOR_KIND_LIST_SQL,
    REVIEW_ANCHOR_KIND_MEDIA,
    REVIEW_ANCHOR_KIND_PARAGRAPH,
    REVIEW_ANCHOR_KIND_WHOLE,
    REVIEW_CATEGORY_LIST_SQL,
    REVIEW_SEVERITY_LIST_SQL,
    RULE_KIND_LIST_SQL,
    RULE_STATUS_ERRORED,
    RULE_STATUS_FAILED,
    RULE_STATUS_LIST_SQL,
    RULE_STATUS_SKIPPED,
    RUN_OUTCOME_BLOCKED,
    RUN_OUTCOME_ERRORED,
    RUN_OUTCOME_LIST_SQL,
    RUN_OUTCOME_PASSED,
    RUN_OUTCOME_WARNED,
    TRIGGER_KIND_LIST_SQL,
)
from rednote.platform.base import Base, created_at_column, uuid_pk
from rednote.platform.immutability import guard_immutable

# 本模块需要不可变保护的表。`ddl.py` 从同一常量派生触发器（名字只有一处来源）。
# **顺序即建表顺序**（引用方在后）。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = (
    "self_check_runs",
    "self_check_rule_results",
    "self_check_findings",
)

#: 本模块拥有的全部表。本包三张表**全部**不可变，因此与 `IMMUTABLE_TABLES`
#: 相等 —— 不写成 `(*IMMUTABLE_TABLES,)` 的别名，而是各列一次并在文件末尾
#: 断言两者相等：将来加一张**可变**表时，断言会提醒"你得决定它进不进守卫"
#: （WP29 的 `content_feedback_items` 恰恰必须可变，循环会把它也锁死）。
OWN_TABLES: Final[tuple[str, ...]] = (
    "self_check_runs",
    "self_check_rule_results",
    "self_check_findings",
)


class SelfCheckRun(Base):
    """一轮自检。**不可变**（重跑 = 追加一行）。"""

    __tablename__ = "self_check_runs"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    content_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_items.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: **这一轮判的是哪一版**。必填 —— 没有它，"上一版通过"会被读成
    #: "当前版通过"（原型列表页的 `AI 自检` 列不带版本号）。
    subject_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: `types.RULE_SET_VERSION` 的落库快照（规则改版后历史仍可解释）。
    rule_set_version: Mapped[int] = mapped_column(Integer, nullable=False)

    #: `MANUAL` / `BATCH`（**无 `AUTO`**：自动触发本包不做）。
    trigger_kind: Mapped[str] = mapped_column(String(16), nullable=False)

    #: 聚合结论。与下面五个计数之间有四条**双向等价式** CHECK。
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)

    #: 命中数按严重度分列（原型卡片的「阻断 · N 项 / 提示 · N 项」）。
    blocking_count: Mapped[int] = mapped_column(Integer, nullable=False)
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False)
    info_count: Mapped[int] = mapped_column(Integer, nullable=False)

    #: 规则数按结论分列。`evaluated_count` = 给出了 PASSED/FAILED 的规则数。
    evaluated_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: **未判数单列**，且不参与 outcome（见模块 docstring）。
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False)
    errored_count: Mapped[int] = mapped_column(Integer, nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("rule_set_version >= 1", name="rule_set_version_positive"),
        CheckConstraint(
            f"trigger_kind IN ({TRIGGER_KIND_LIST_SQL})", name="trigger_kind_valid"
        ),
        CheckConstraint(f"outcome IN ({RUN_OUTCOME_LIST_SQL})", name="outcome_valid"),
        CheckConstraint("blocking_count >= 0", name="blocking_count_non_negative"),
        CheckConstraint("warning_count >= 0", name="warning_count_non_negative"),
        CheckConstraint("info_count >= 0", name="info_count_non_negative"),
        # 一轮至少跑成一条规则，否则是**空转轮次**（"自检过了"但什么都没判）。
        CheckConstraint("evaluated_count >= 1", name="evaluated_count_positive"),
        CheckConstraint("skipped_count >= 0", name="skipped_count_non_negative"),
        CheckConstraint("errored_count >= 0", name="errored_count_non_negative"),
        CheckConstraint(
            "finished_at >= started_at", name="finished_at_not_before_started_at"
        ),
        # --- outcome 的四条双向等价式（本表的核心，见模块 docstring）--------
        CheckConstraint(
            f"(outcome = '{RUN_OUTCOME_BLOCKED}') = (blocking_count > 0)",
            name="outcome_blocked_equivalence",
        ),
        CheckConstraint(
            f"(outcome = '{RUN_OUTCOME_ERRORED}')"
            " = (blocking_count = 0 AND errored_count > 0)",
            name="outcome_errored_equivalence",
        ),
        CheckConstraint(
            f"(outcome = '{RUN_OUTCOME_WARNED}')"
            " = (blocking_count = 0 AND errored_count = 0 AND warning_count > 0)",
            name="outcome_warned_equivalence",
        ),
        CheckConstraint(
            f"(outcome = '{RUN_OUTCOME_PASSED}')"
            " = (blocking_count = 0 AND errored_count = 0 AND warning_count = 0)",
            name="outcome_passed_equivalence",
        ),
        # 看板与历史的读路径（倒序由查询给，索引方向对 btree 无影响）。
        Index("ix_self_check_runs_content_created", "content_item_id", "created_at"),
        Index("ix_self_check_runs_project_created", "project_id", "created_at"),
        # "这一版被自检过哪些轮"（STALE 判定与影响面）。
        Index("ix_self_check_runs_subject_version", "subject_version_id"),
        # **刻意不加** (content_item_id, rule_set_version) 唯一约束：
        # 同一版、同一规则集允许重跑 —— 重跑是诊断手段，不是重复数据。
    )


class SelfCheckRuleResult(Base):
    """一轮里一条规则的结论。**不可变**。

    四个状态各有自己的必填项，全部写成**双向等值** CHECK：

    | status | severity | skip_reason | error_note | finding_count |
    |---|---|---|---|---|
    | `PASSED` | NULL | NULL | NULL | 0 |
    | `FAILED` | 非空 | NULL | NULL | **> 0** |
    | `SKIPPED` | NULL | **非空** | NULL | 0 |
    | `ERRORED` | NULL | NULL | **非空** | 0 |

    写成等值而不是两条单向条件，是因为反向也是坏数据：
    "没 FAILED 却带了严重度"会让统计把它算进命中，
    "FAILED 却零 finding"会让前端显示"有问题"但点不开任何一处。
    """

    __tablename__ = "self_check_rule_results"

    id: Mapped[uuid.UUID] = uuid_pk()

    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("self_check_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: 规则码。**刻意无取值域 CHECK**（开集，见模块 docstring）。
    rule_code: Mapped[str] = mapped_column(
        String(MAX_RULE_CODE_CHARS), nullable=False
    )

    #: 8 类之一（闭集，字面量化石）。
    category: Mapped[str] = mapped_column(String(24), nullable=False)

    #: `HARD` / `SOFT` —— 回答"这个结论可复现吗"，与严重度是两维。
    rule_kind: Mapped[str] = mapped_column(String(8), nullable=False)

    status: Mapped[str] = mapped_column(String(12), nullable=False)

    #: 仅 `FAILED` 时非空（双向 CHECK）。
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)

    #: 仅 `SKIPPED` 时非空。**取值域不落 CHECK**（开集，同 `rule_code`）。
    skip_reason: Mapped[str | None] = mapped_column(
        String(MAX_SKIP_REASON_CHARS), nullable=True
    )

    #: 仅 `ERRORED` 时非空 —— 规则崩了的诊断串（单条规则异常不中断整轮）。
    error_note: Mapped[str | None] = mapped_column(
        String(MAX_ERROR_NOTE_CHARS), nullable=True
    )

    finding_count: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("char_length(rule_code) > 0", name="rule_code_not_blank"),
        CheckConstraint(
            f"category IN ({REVIEW_CATEGORY_LIST_SQL})", name="category_valid"
        ),
        CheckConstraint(f"rule_kind IN ({RULE_KIND_LIST_SQL})", name="rule_kind_valid"),
        CheckConstraint(f"status IN ({RULE_STATUS_LIST_SQL})", name="status_valid"),
        CheckConstraint(
            f"severity IS NULL OR severity IN ({REVIEW_SEVERITY_LIST_SQL})",
            name="severity_valid",
        ),
        CheckConstraint(
            f"(status = '{RULE_STATUS_FAILED}') = (severity IS NOT NULL)",
            name="severity_paired_with_failed",
        ),
        CheckConstraint(
            f"(status = '{RULE_STATUS_SKIPPED}') = (skip_reason IS NOT NULL)",
            name="skip_reason_paired_with_skipped",
        ),
        CheckConstraint(
            f"(status = '{RULE_STATUS_ERRORED}') = (error_note IS NOT NULL)",
            name="error_note_paired_with_errored",
        ),
        CheckConstraint("finding_count >= 0", name="finding_count_non_negative"),
        CheckConstraint(
            f"(status = '{RULE_STATUS_FAILED}') = (finding_count > 0)",
            name="finding_count_paired_with_failed",
        ),
        # 一轮里一条规则只能有一个结论。唯一约束自带索引，不另建同列普通索引。
        UniqueConstraint("run_id", "rule_code", name="uq_self_check_rule_results_run_code"),
        Index("ix_self_check_rule_results_run_category", "run_id", "category"),
        Index("ix_self_check_rule_results_run_status", "run_id", "status"),
    )


class SelfCheckFinding(Base):
    """一条命中。**不可变**。

    锚点三选一（与 WP29 同值、三条 CHECK 同形）：

    | `anchor_kind` | `paragraph_ref` | `media_item_id` |
    |---|---|---|
    | `WHOLE` | NULL | NULL |
    | `PARAGRAPH` | 非空 | NULL |
    | `MEDIA` | NULL | 非空 |

    `paragraph_ref` **刻意不加 FK**：段落不是表，它是正文切段后的产物
    （`p<序号>-<8位哈希>`），只对本轮 `subject_version_id` 那一版正文有意义。
    """

    __tablename__ = "self_check_findings"

    id: Mapped[uuid.UUID] = uuid_pk()

    rule_result_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("self_check_rule_results.id", ondelete="RESTRICT"),
        nullable=False,
    )

    anchor_kind: Mapped[str] = mapped_column(String(20), nullable=False)

    paragraph_ref: Mapped[str | None] = mapped_column(
        String(MAX_PARAGRAPH_REF_CHARS), nullable=True
    )

    media_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("media_items.id", ondelete="RESTRICT"),
        nullable=True,
    )

    #: 人读的一句话。
    message: Mapped[str] = mapped_column(
        String(MAX_FINDING_MESSAGE_CHARS), nullable=False
    )

    #: **机器可判的事实**（实测值 / 期望值 / 引用 id），不是 `message` 的重复。
    #: 键名 camelCase（与 `content_versions.bindings` 同惯例）。
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    #: 这条发现落成了哪条 AI 反馈（**仅阻断项落**）。
    #: ⚠️ **跨包入向 FK**：它会让 `conftest.drop_own_tables` 把
    #: `content_feedback_items` 从"删"变成"留"（见说明文档 §3 的 R8）。
    feedback_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_feedback_items.id", ondelete="RESTRICT"),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            f"anchor_kind IN ({REVIEW_ANCHOR_KIND_LIST_SQL})",
            name="anchor_kind_valid",
        ),
        # 每条写成"不是这种锚点，或者列占用正确"，三条件合起来互斥且完备。
        CheckConstraint(
            f"(anchor_kind <> '{REVIEW_ANCHOR_KIND_PARAGRAPH}')"
            " OR (paragraph_ref IS NOT NULL AND media_item_id IS NULL)",
            name="anchor_paragraph_shape",
        ),
        CheckConstraint(
            f"(anchor_kind <> '{REVIEW_ANCHOR_KIND_MEDIA}')"
            " OR (media_item_id IS NOT NULL AND paragraph_ref IS NULL)",
            name="anchor_media_shape",
        ),
        CheckConstraint(
            f"(anchor_kind <> '{REVIEW_ANCHOR_KIND_WHOLE}')"
            " OR (paragraph_ref IS NULL AND media_item_id IS NULL)",
            name="anchor_whole_shape",
        ),
        CheckConstraint("char_length(message) > 0", name="message_not_blank"),
        # evidence 必须是 JSON **对象**：数组或标量会让"读 evidence.xxx"
        # 这件事在不同 finding 上形状不同（前端只能逐条猜）。
        CheckConstraint("jsonb_typeof(evidence) = 'object'", name="evidence_is_object"),
        Index("ix_self_check_findings_rule_result", "rule_result_id"),
        Index("ix_self_check_findings_feedback_item", "feedback_item_id"),
        Index("ix_self_check_findings_media_item", "media_item_id"),
    )


# 不可变守卫：三张表各自登记（不是循环 —— 见 `OWN_TABLES` 的说明）。
guard_immutable(SelfCheckRun, SelfCheckRuleResult, SelfCheckFinding)

assert {
    SelfCheckRun.__tablename__,
    SelfCheckRuleResult.__tablename__,
    SelfCheckFinding.__tablename__,
} == set(IMMUTABLE_TABLES)
assert set(OWN_TABLES) == set(IMMUTABLE_TABLES), (
    "本包三张表目前全部不可变。将来加可变表时，必须显式决定它进不进"
    "`IMMUTABLE_TABLES`（照 WP29 的 `content_feedback_items`：它恰恰必须可变）。"
)

__all__ = [
    "IMMUTABLE_TABLES",
    "OWN_TABLES",
    "SelfCheckFinding",
    "SelfCheckRuleResult",
    "SelfCheckRun",
]
