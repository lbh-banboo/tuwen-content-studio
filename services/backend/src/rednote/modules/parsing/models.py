"""解析运行与来源片段两张表（数据结构 02 §4）。

| 表 | 性质 | 可变性 |
|---|---|---|
| `parse_runs` | **过程记录**（一次解析的状态、计数、错误） | 可变（状态与计数会推进） |
| `source_segments` | **证据**（原文片段 + 定位） | **不可变**（触发器 + ORM 守卫双层保护） |

---

## 为什么 `source_segments` 必须不可变

它是"这条结论出自哪里"的**唯一凭据**。若片段可以被原地改写，
那么"溯源"这件事的答案就取决于"有没有人改过片段"，
而**没有人能审计'谁改过片段'** —— 与 WP15 审计表、WP16 出网留痕
是同一个论证（`platform/immutability.py` 的模块 docstring 写了完整版）。

重解析不修改旧片段，而是产生**新的 `parse_runs` 行**（旧 run 与它的
片段一并留档）—— 与数据结构 02 §5「历史版本不允许原地改写」一致。

## 为什么 `state` 的约束写成三条而不是一条

`derive_state` 已经把规则写清楚了，但**约束必须在数据库层也成立** ——
否则有人绕过 service 直接写 SQL 就能造出"`SUCCEEDED` 但只解出 3/20"。
三条 CHECK 逐条对应 `derive_state` 的三个分支：

| CHECK | 拦住什么 |
|---|---|
| `parsed_units <= total_units` | 计数自相矛盾 |
| `state='SUCCEEDED' → total_units IS NOT NULL AND parsed_units = total_units` | **虚报成功**（最核心的一条） |
| `state='PARTIAL' → total_units IS NOT NULL AND parsed_units > 0 AND parsed_units < total_units` | 把"一个都没解出来"标成部分成功 |

第三条同时挡住了 `parsed_units = 0` 的 PARTIAL —— 那是 FAILED。
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.parsing.types import (
    EXTRACTION_KIND_LIST_SQL,
    PARSE_STATE_LIST_SQL,
    PARSER_VERSION,
)
from rednote.platform.base import Base, created_at_column, updated_at_column, uuid_pk
from rednote.platform.immutability import guard_immutable

# 本模块需要不可变保护的表（`ddl.py` 里另有一份同值副本，
# 理由见 `ddl.py` 的说明：迁移脚本要在不加载 ORM 元数据的前提下拿到表名）。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = ("source_segments",)

# `errors` 的 schema 版本。写入方带上它，消费方据此选择解析方式。
PARSE_ERRORS_SCHEMA_VERSION: Final[int] = 1

# `position` 的 schema 版本。定位结构将来可能扩展（例如加"表名"），
# 老数据要能被正确解读 —— 与 WP11 的 `payload_schema_version` 同一手法。
POSITION_SCHEMA_VERSION: Final[int] = 1


class ParseRun(Base):
    """一次解析运行。**可变**（状态与计数会推进）。

    `file_id` 指向 WP12 的 `file_objects`。**同文件多次解析 = 多行** ——
    重解析不覆盖旧结果（旧片段是证据）。因此**没有** `UQ(file_id)`。
    "同一解析器版本结果可复用"由 service 层的查询实现（按
    `(file_id, parser_version)` 找既有的终态 run），不是唯一约束 ——
    因为"可复用"是策略，策略变更不该要迁移。
    """

    __tablename__ = "parse_runs"

    id: Mapped[uuid.UUID] = uuid_pk()

    file_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("file_objects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 产出这次解析的任务（WP13 的 `jobs`）。**可空**：
    # 同步调用（测试、运维补解析）没有任务行。
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="RESTRICT"),
        nullable=True,
    )

    parser_version: Mapped[str] = mapped_column(
        String(60), nullable=False, server_default=text(f"'{PARSER_VERSION}'")
    )

    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'QUEUED'")
    )

    # **可空**：`None` 表示"数不出总量"（结构读不出）→ 必然 FAILED。
    # 这个可空性是刻意的：它让"我不知道总量"与"总量是 0"成为两件事，
    # 而两者都不允许报成功。
    total_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parsed_units: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    errors_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(PARSE_ERRORS_SCHEMA_VERSION))
    )

    # 解析结果导出文件。**本包建列不用**：消费者是 WP35（导出）。
    # 与 WP13 的 `batch_id` / `content_id` 同一做法 ——
    # 不建的话将来要加列 + 回填；建了不用只是一个 NULL 列。
    output_file_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("file_objects.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 实际使用的 OCR 引擎名（`none` = 未用）。落库是为了让
    # "这批文本是谁识别的"可查 —— OCR 结果的可信度与引擎绑定。
    ocr_engine: Mapped[str] = mapped_column(
        String(60), nullable=False, server_default=text("'none'")
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint(
            f"state IN ({PARSE_STATE_LIST_SQL})", name="state_valid"
        ),
        CheckConstraint("parsed_units >= 0", name="parsed_units_non_negative"),
        # 三条逐条对应 `derive_state` 的分支，见模块 docstring。
        CheckConstraint(
            "total_units IS NULL OR parsed_units <= total_units",
            name="parsed_within_total",
        ),
        CheckConstraint(
            "state <> 'SUCCEEDED'"
            " OR (total_units IS NOT NULL AND parsed_units = total_units"
            " AND parsed_units > 0)",
            name="succeeded_means_all_parsed",
        ),
        CheckConstraint(
            "state <> 'PARTIAL'"
            " OR (total_units IS NOT NULL AND parsed_units > 0"
            " AND parsed_units < total_units)",
            name="partial_means_some_parsed",
        ),
        # 终态必须有 finished_at：否则"跑了多久"不可回答，
        # 而"卡住的解析"与"跑完的解析"在库里长得一样。
        CheckConstraint(
            f"state NOT IN ('SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED')"
            " OR finished_at IS NOT NULL",
            name="terminal_has_finished_at",
        ),
        # 复用查询路径：`WHERE file_id = ? AND parser_version = ? ORDER BY created_at DESC`。
        Index("ix_parse_runs_file_created", "file_id", "created_at"),
        Index("ix_parse_runs_file_parser", "file_id", "parser_version"),
        Index("ix_parse_runs_job_id", "job_id"),
        # 运维扫描路径："有哪些 run 还在跑"。
        Index(
            "ix_parse_runs_active",
            "state",
            postgresql_where=text("state IN ('QUEUED', 'RUNNING')"),
        ),
    )


class SourceSegment(Base):
    """来源片段。**不可变证据**。

    唯一约束 `UQ(parse_run_id, content_hash, position)`（数据结构 02 §4 原句）。
    三重键而不是只按 position：同一位置出现两段不同文本是**合法**的
    （例如 PDF 的一页里既有正文又有页眉，OCR 行框不同）——
    只按 position 唯一会把这些误判成重复。

    `position` 用 JSONB 参与唯一约束：PG 17 的 `jsonb_ops` 支持 btree
    （本机已实测 `UNIQUE (text, jsonb)` 建得出来）。JSONB 的等值比较是
    **结构比较**（键序无关），因此 `{"kind":"PAGE","page":7}` 与
    `{"page":7,"kind":"PAGE"}` 视为同一值 —— 这正是我们要的。
    """

    __tablename__ = "source_segments"

    id: Mapped[uuid.UUID] = uuid_pk()

    parse_run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("parse_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # **批内序号**（从 1 开始）。为什么必须有这一列：
    #
    # `created_at` 的默认值是 `now()` —— 那是 **事务**时间戳，
    # 同一个事务里插入的所有行**值完全相同**。于是"按 created_at 排序"
    # 会退化成按主键（随机 UUID）排序 —— 本机实测：三行 `["甲","乙","丙"]`
    # 读回来是 `["乙","丙","甲"]`。
    #
    # 下游（WP18 拼 Brief 原文、WP21 切块）必须能**按原文顺序**拿到片段，
    # 所以顺序必须是一个**显式落库的事实**，不能靠排序某个恰好存在的列。
    segment_no: Mapped[int] = mapped_column(Integer, nullable=False)

    position: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    position_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(POSITION_SCHEMA_VERSION))
    )

    text_content: Mapped[str] = mapped_column(Text, nullable=False)
    # 内容摘要（SHA-256 十六进制）。用途有二：
    #   · 参与唯一约束（同位置同内容 = 重复片段）；
    #   · 让下游能按摘要去重，而不必把正文拉进内存比较。
    # **与 WP11 的 `canonical_payload_hash` 不同算法**：那个是
    # "规范化 JSON 的摘要"，这个是"UTF-8 文本字节的摘要" ——
    # 两者的输入空间不同，强行统一会让"片段摘要"多一层 JSON 包装。
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    extraction_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # 0~1。只有 OCR 会填（`TEXT`/`TABLE` 是确定性提取，填一个 1.0
    # 会让"哪些是识别出来的"变得不可分辨）。
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)

    # 来源链接（网页剪藏等场景）。**可空**，因为绝大多数片段来自上传文件。
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint(
            "parse_run_id",
            "content_hash",
            "position",
            name="uq_source_segments_run_hash_position",
        ),
        # 同一 run 内序号唯一 —— 保证"按序号排序"是一个**全序**，
        # 不存在两行并列导致的顺序不确定。
        UniqueConstraint(
            "parse_run_id", "segment_no", name="uq_source_segments_run_segment_no"
        ),
        CheckConstraint("segment_no >= 1", name="segment_no_positive"),
        CheckConstraint(
            f"extraction_kind IN ({EXTRACTION_KIND_LIST_SQL})",
            name="extraction_kind_valid",
        ),
        CheckConstraint(
            f"char_length(content_hash) = 64", name="content_hash_length"
        ),
        # 空片段一律拒绝：它对下游没有信息量，且会让"解出几个 unit"
        # 这个计数变成可以靠空串刷上去的数字。
        CheckConstraint("char_length(text_content) > 0", name="text_not_empty"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_in_range",
        ),
        # **至少一个定位锚点**（数据结构 02 §4 的同类要求，用于 source_provenance；
        # 这里把它落到 position 上）：`{}` 不是合法定位。
        CheckConstraint(
            "position ? 'kind'", name="position_has_kind"
        ),
        # 定位的查询路径：按页/段找片段（"跳到原文第 7 页"）。
        Index("ix_source_segments_run_id", "parse_run_id"),
        # 顺序读取路径：按原文顺序拿片段（WP18/WP21 都要）。
        Index("ix_source_segments_run_no", "parse_run_id", "segment_no"),
    )


# **模块级**注册不可变守卫（与 WP15/WP16 同一手法）：
# 只要 import 了模型，ORM 层守卫就已生效，不依赖初始化顺序。
guard_immutable(SourceSegment)


__all__ = [
    "IMMUTABLE_TABLES",
    "PARSE_ERRORS_SCHEMA_VERSION",
    "POSITION_SCHEMA_VERSION",
    "ParseRun",
    "SourceSegment",
]
