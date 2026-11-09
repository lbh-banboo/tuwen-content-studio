"""文件、配额与上传会话的存储模型（WP12）。

按数据结构文档 02 §4「文件、配额与解析」建立本包所需的 6 张表。

**为什么不是把 §4 的表全建了**：§4 还列了 `parse_runs` / `source_segments` /
`source_provenance` 三张解析表，它们都带 `job_id FK` —— 依赖 `jobs`（§11，WP13）。
本包只做「把文件安全收进来 + 配额不超不漏」，解析属 WP17。
按"边界按有没有设计空间划"的原则（见 WP12 说明文档 §3），这三张表在这里建
只会与 WP17 的设计打架。

**六张表的分工**：

| 表 | 角色 | 可变性 |
|---|---|---|
| `blobs` | 物理内容身份（sha256 + object_key + 字节数），**跨项目可共用** | 可变（scan_status / storage_state 会变） |
| `file_objects` | 逻辑文件（原名 + 可见性 + 上传人） | 半可变（`archived_at` 会写一次） |
| `project_file_links` | 项目 ↔ 文件 ↔ 用途；`effective` 决定是否计入配额 | 可变（`effective` 会翻） |
| `quota_accounts` | 每项目一行账面：limit / effective / reserved | **高频可变**（每次上传都动） |
| `upload_sessions` | 一次上传的意图与状态机 | 可变（状态迁移） |
| `upload_parts` | 已接收分片 | **只增不改**（同片重传一致则不动） |

**物理去重与配额是两件事**（文档 02 §4 原文）：
"物理 Blob 可做企业内部去重，但不能向无权限用户暴露'某文件已存在'或原文件名称。
配额去重按项目有效文件摘要处理，**不因跨项目共用 Blob 免掉该项目配额**。"
因此 `blobs` 按 `UQ(storage_backend, object_key)` 去重，而配额只看
`project_file_links` 的有效关联 —— 两者不能互相短路。

**关于时间戳列的补充**：文档 02 §4 只列了业务列，未列 `updated_at`。
但四张可变表（blobs / file_objects / quota_accounts / upload_sessions）都靠
状态迁移驱动，没有时间戳就无法回答"这个会话卡在 COMPLETING 多久了"这类
排障问题。因此补了 `updated_at`，并登记为待定项（见 WP12 说明文档 §9）。
`upload_parts` 刻意不加：它只增不改，重传一致时不触碰。

**关于 `upload_sessions.file_id`**：文档 §4 的列里没有它，但"完成上传"
必须能幂等返回同一个 `fileId` —— 重复调用 `completions` 时若重新固化，
会产生两份 blob 与两份配额。因此补一列指向固化结果，同为待定项。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.platform.base import Base, created_at_column, updated_at_column, uuid_pk

# ---------------------------------------------------------------------------
# 配额
# ---------------------------------------------------------------------------

# 需求说明 §6.2：「每项目有效研究原文件累计不超过 300 MB，按 300,000,000 字节计算」。
# **刻意用十进制字节数而不是 300 * 1024 * 1024**：需求原文写死了 300,000,000，
# 而 300 MiB = 314,572,800 —— 两者差 4.6%，用错会让验收（AC-06）在
# "刚好 300,000,000 附近"的用例上与需求不一致。常量名与数值都要能对上原文。
QUOTA_LIMIT_BYTES: Final[int] = 300_000_000

# 计入配额的用途。**只有 RESEARCH**（需求 §6.2 原文："有效研究原文件"）。
# 其余用途（Brief、生成媒体、导出等）明确不占研究配额 —— 原文：
# "不包含 Brief、生成媒体和系统提取出的内部索引。"
# 做成集合而不是散落的 if：新增用途时，"要不要计配额"是一个必须显式回答的问题。
PURPOSE_BRIEF: Final[str] = "BRIEF"
PURPOSE_RESEARCH: Final[str] = "RESEARCH"
PURPOSE_REFERENCE: Final[str] = "REFERENCE"
PURPOSE_PROOF: Final[str] = "PROOF"
PURPOSE_GENERATED: Final[str] = "GENERATED"
PURPOSE_EXPORT: Final[str] = "EXPORT"

FILE_PURPOSES: Final[frozenset[str]] = frozenset(
    {
        PURPOSE_BRIEF,
        PURPOSE_RESEARCH,
        PURPOSE_REFERENCE,
        PURPOSE_PROOF,
        PURPOSE_GENERATED,
        PURPOSE_EXPORT,
    }
)

QUOTA_PURPOSES: Final[frozenset[str]] = frozenset({PURPOSE_RESEARCH})

FILE_PURPOSE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(FILE_PURPOSES)
)

# ---------------------------------------------------------------------------
# 存储后端与 blob 状态
# ---------------------------------------------------------------------------

# 本机用 LOCAL（文件系统），内网用 MINIO。做成适配器是因为内网对象存储
# 尚未就位（M0 的 OP-75），而"上传链路"必须在本机能端到端跑通。
STORAGE_BACKEND_LOCAL: Final[str] = "LOCAL"
STORAGE_BACKEND_MINIO: Final[str] = "MINIO"
STORAGE_BACKENDS: Final[frozenset[str]] = frozenset(
    {STORAGE_BACKEND_LOCAL, STORAGE_BACKEND_MINIO}
)

# 病毒/内容扫描状态。本包只置 PENDING —— 真实引擎属安全评审范围（见待定项）。
# 保留完整取值域是为了让"扫描没做"与"扫描通过"在数据上可区分：
# 若把未扫描也写成 CLEAN，将来接入引擎后无法分辨存量数据到底扫过没有。
SCAN_STATUS_PENDING: Final[str] = "PENDING"
SCAN_STATUS_CLEAN: Final[str] = "CLEAN"
SCAN_STATUS_INFECTED: Final[str] = "INFECTED"
SCAN_STATUS_SKIPPED: Final[str] = "SKIPPED"
SCAN_STATUSES: Final[frozenset[str]] = frozenset(
    {
        SCAN_STATUS_PENDING,
        SCAN_STATUS_CLEAN,
        SCAN_STATUS_INFECTED,
        SCAN_STATUS_SKIPPED,
    }
)
SCAN_STATUS_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(SCAN_STATUSES)
)

# 对象生命周期。删除物理对象前先置 DELETED，让"配额已释放但对象还在"
# 与"对象已删"可区分（GC 与对账需要）。
STORAGE_STATE_ACTIVE: Final[str] = "ACTIVE"
STORAGE_STATE_PENDING_DELETE: Final[str] = "PENDING_DELETE"
STORAGE_STATE_DELETED: Final[str] = "DELETED"
STORAGE_STATES: Final[frozenset[str]] = frozenset(
    {
        STORAGE_STATE_ACTIVE,
        STORAGE_STATE_PENDING_DELETE,
        STORAGE_STATE_DELETED,
    }
)
STORAGE_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(STORAGE_STATES)
)

# 文件可见性。PROJECT 绑项目；KNOWLEDGE 绑知识库（全员可见的共享主数据）。
VISIBILITY_PROJECT: Final[str] = "PROJECT"
VISIBILITY_KNOWLEDGE: Final[str] = "KNOWLEDGE"
VISIBILITIES: Final[frozenset[str]] = frozenset(
    {VISIBILITY_PROJECT, VISIBILITY_KNOWLEDGE}
)
VISIBILITY_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(VISIBILITIES)
)

# ---------------------------------------------------------------------------
# 可解析类型（AC-07 的判据）
# ---------------------------------------------------------------------------

# 需求 §6.2：「第一版自动解析目标：PDF、DOCX、TXT、Markdown、CSV、XLSX、JPG、PNG、WebP；
# 扫描文档与图片通过 OCR」「其他格式原样归档并标记"未解析"」。
CLASSIFICATION_SUPPORTED: Final[str] = "SUPPORTED"
CLASSIFICATION_UNSUPPORTED: Final[str] = "UNSUPPORTED"
CLASSIFICATIONS: Final[frozenset[str]] = frozenset(
    {CLASSIFICATION_SUPPORTED, CLASSIFICATION_UNSUPPORTED}
)
CLASSIFICATION_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(CLASSIFICATIONS)
)

# 扩展名白名单。**与 MIME 白名单并列**，两者任一命中即视为支持：
# 客户端给的 MIME 不可信（浏览器对 .md 常报 text/plain，
# 对 .csv 在 macOS 上可能报 application/vnd.ms-excel），
# 只看 MIME 会把本来能解析的文件误判成 UNSUPPORTED；
# 只看扩展名又会被改名绕过。两者取并集，宽进严出 ——
# 真正的类型判定（magic bytes）属 WP17，本包只回答"是否在承诺清单内"。
SUPPORTED_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        "pdf",
        "docx",
        "txt",
        "md",
        "markdown",
        "csv",
        "xlsx",
        "jpg",
        "jpeg",
        "png",
        "webp",
    }
)

SUPPORTED_MIME_TYPES: Final[frozenset[str]] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/plain",
        "text/markdown",
        "text/csv",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)

# ---------------------------------------------------------------------------
# 上传会话状态机
# ---------------------------------------------------------------------------

# 迁移方向（单向，不可回退）：
#   UPLOADING ──→ COMPLETING ──→ COMPLETED
#       │              │
#       ├──→ CANCELLED │
#       └──→ EXPIRED   └──→ FAILED（校验不过，预占已释放）
UPLOAD_STATE_UPLOADING: Final[str] = "UPLOADING"
UPLOAD_STATE_COMPLETING: Final[str] = "COMPLETING"
UPLOAD_STATE_COMPLETED: Final[str] = "COMPLETED"
UPLOAD_STATE_CANCELLED: Final[str] = "CANCELLED"
UPLOAD_STATE_EXPIRED: Final[str] = "EXPIRED"
UPLOAD_STATE_FAILED: Final[str] = "FAILED"

UPLOAD_STATES: Final[frozenset[str]] = frozenset(
    {
        UPLOAD_STATE_UPLOADING,
        UPLOAD_STATE_COMPLETING,
        UPLOAD_STATE_COMPLETED,
        UPLOAD_STATE_CANCELLED,
        UPLOAD_STATE_EXPIRED,
        UPLOAD_STATE_FAILED,
    }
)
UPLOAD_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(UPLOAD_STATES)
)

# **预占还挂在账上的状态**。定义成集合而不是散写 `state == UPLOADING`：
# 释放预占的判定与"能否继续上传"的判定是两件事，写散必然漂移。
# COMPLETING 也算 —— 那一刻预占还没转有效，进程若在这里崩了，
# TTL 回收必须能把它捞回来。
UPLOAD_STATES_HOLDING_RESERVATION: Final[frozenset[str]] = frozenset(
    {UPLOAD_STATE_UPLOADING, UPLOAD_STATE_COMPLETING}
)

UPLOAD_STATES_HOLDING_RESERVATION_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(UPLOAD_STATES_HOLDING_RESERVATION)
)

# 分片大小。协议 §4.2：「partSizeBytes（默认 8 MiB）」。
DEFAULT_PART_SIZE_BYTES: Final[int] = 8 * 1024 * 1024

# 会话有效期。协议 §4.2 要求返回 expiresAt，但未规定时长。
# 取 24h：足够一次跨班次的上传（用户晚上传一半、第二天接着传），
# 又不至于让废弃会话长期占住预占额度。
UPLOAD_SESSION_TTL_SECONDS: Final[int] = 24 * 60 * 60

# sha256 十六进制摘要长度。
SHA256_HEX_LENGTH: Final[int] = 64


class Blob(Base):
    """物理内容身份。

    **`object_key` 由服务端生成，永远不含用户输入**（文档 02 §4：
    "不把文件名当路径"）。原文件名存在 `file_objects.original_name`，
    只用于展示 —— 两者分开是路径穿越防护的第一道。
    """

    __tablename__ = "blobs"

    id: Mapped[uuid.UUID] = uuid_pk()

    storage_backend: Mapped[str] = mapped_column(String(20), nullable=False)
    # LOCAL 后端无 bucket 概念，故可空。
    bucket: Mapped[str | None] = mapped_column(String(100), nullable=True)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)

    # **服务端重算的结果**（文档 02 §4："SHA 为服务端结果"）。
    # 客户端传的 sha256 只作提示，绝不写进这一列 —— 否则用户可以用
    # 一个假的摘要污染去重逻辑，让两个不同文件被当成同一个。
    sha256: Mapped[str] = mapped_column(String(SHA256_HEX_LENGTH), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)

    scan_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text(f"'{SCAN_STATUS_PENDING}'")
    )
    storage_state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text(f"'{STORAGE_STATE_ACTIVE}'")
    )

    retained_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    encryption_key_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        # 同一后端下 object_key 唯一。跨后端不约束 —— LOCAL 与 MINIO
        # 可能同时存在（迁移期），key 空间互不相干。
        UniqueConstraint(
            "storage_backend",
            "object_key",
            name="uq_blobs_storage_backend_object_key",
        ),
        CheckConstraint("byte_size >= 0", name="byte_size_non_negative"),
        CheckConstraint(
            f"char_length(sha256) = {SHA256_HEX_LENGTH}", name="sha256_length"
        ),
        CheckConstraint(
            f"storage_backend IN ('{STORAGE_BACKEND_LOCAL}', '{STORAGE_BACKEND_MINIO}')",
            name="storage_backend_valid",
        ),
        CheckConstraint(
            f"scan_status IN ({SCAN_STATUS_LIST_SQL})", name="scan_status_valid"
        ),
        CheckConstraint(
            f"storage_state IN ({STORAGE_STATE_LIST_SQL})", name="storage_state_valid"
        ),
        # 物理去重的查重路径：同后端同摘要 → 复用已有 blob。
        Index("ix_blobs_sha256", "storage_backend", "sha256"),
        Index("ix_blobs_storage_state", "storage_state"),
    )


class FileObject(Base):
    """逻辑文件：用户视角的"一个文件"。

    **`original_name` 与 `blobs.object_key` 必须分开**（文档 02 §4）。
    把原文件名当存储路径会同时引入三个问题：路径穿越（`../../etc/passwd`）、
    重名覆盖、以及"文件名本身泄露业务信息"（例如
    `2026Q3-未发布新品-定价表.xlsx` 出现在对象存储的 key 里，
    运维看一眼就知道项目内容）。
    """

    __tablename__ = "file_objects"

    id: Mapped[uuid.UUID] = uuid_pk()

    blob_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("blobs.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 原始文件名，仅用于展示。长度给 512：够长以容纳中文长名，
    # 又不至于让列表接口的响应体失控。
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)

    visibility: Mapped[str] = mapped_column(String(20), nullable=False)
    # KNOWLEDGE 可见性的文件不属于任何项目，故可空。
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 是否在"承诺可解析"的类型清单内（AC-07 的判据）。
    classification: Mapped[str] = mapped_column(String(20), nullable=False)

    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint(
            f"visibility IN ({VISIBILITY_LIST_SQL})", name="visibility_valid"
        ),
        CheckConstraint(
            f"classification IN ({CLASSIFICATION_LIST_SQL})",
            name="classification_valid",
        ),
        # PROJECT 可见性必须有项目归属；KNOWLEDGE 不允许绑项目。
        # 不加这条会出现"标称项目私有、实际不属于任何项目"的悬空文件。
        CheckConstraint(
            "(visibility = 'PROJECT' AND project_id IS NOT NULL)"
            " OR (visibility = 'KNOWLEDGE' AND project_id IS NULL)",
            name="visibility_matches_project",
        ),
        Index("ix_file_objects_project_id", "project_id"),
        Index("ix_file_objects_uploaded_by", "uploaded_by"),
    )


class ProjectFileLink(Base):
    """项目 ↔ 文件 ↔ 用途的关联。**研究配额按本表计算**（文档 02 §4）。

    **为什么配额不直接看 `file_objects.project_id`**：
    同一个文件可以在一个项目里既是 REFERENCE 又是 PROOF，也可以从项目解绑
    （`effective = false`）而文件本身仍在。配额要算的是"**当前有效的研究资料**"，
    这只有关联表能表达 —— `file_objects` 上只有一个 `project_id`，表达不了
    "同一文件在本项目里有几种用途""这个关联还有效吗"。
    """

    __tablename__ = "project_file_links"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("file_objects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)

    # 解绑用软删：历史关联要保留（审计"这个文件曾经属于本项目"），
    # 但不再计入配额。
    effective: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint(
            f"purpose IN ({FILE_PURPOSE_LIST_SQL})", name="purpose_valid"
        ),
        # "同项目同文件同用途**活跃**唯一"（文档 02 §4）。
        # **必须是部分唯一索引**：不加 WHERE 就只能有一条历史记录，
        # 解绑后重新关联会被唯一约束挡住 —— 而重新关联是完全正常的操作。
        Index(
            "uq_project_file_links_active",
            "project_id",
            "file_id",
            "purpose",
            unique=True,
            postgresql_where=text("effective"),
        ),
        # 配额求和路径：(project_id, purpose) WHERE effective。
        Index(
            "ix_project_file_links_quota",
            "project_id",
            "purpose",
            postgresql_where=text("effective"),
        ),
        Index("ix_project_file_links_file_id", "file_id"),
    )


class QuotaAccount(Base):
    """每项目一行配额账面。

    **本包的核心表**：AC-06（并发上传仍无法超过 300,000,000 字节）
    的全部正确性都落在这一张表的更新方式上。

    `effective + reserved <= limit` 写成 CHECK 而不是只靠应用层判断，
    是因为这条不变量一旦被破坏，**失败是静默的**：文件都传上去了、
    没有任何异常，只是账面超了。等用户发现"配额满了但只有一半文件"时，
    已经无从回溯是哪一次上传越的界。

    **为什么没有 `lock_version`**：乐观锁用于"读出来、改几个字段、写回去"
    这种需要携带旧值的场景。本表的更新全部是**单条原子条件 UPDATE**
    （`SET reserved = reserved + :delta WHERE effective + reserved + :delta <= limit`），
    它把"检查"和"写入"压在同一条语句里，根本没有"读-改-写"的窗口，
    加乐观锁反而会引入一个多余的失败分支。
    """

    __tablename__ = "quota_accounts"

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        primary_key=True,
    )

    limit_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text(str(QUOTA_LIMIT_BYTES))
    )
    effective_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    reserved_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint("limit_bytes >= 0", name="limit_non_negative"),
        CheckConstraint("effective_bytes >= 0", name="effective_non_negative"),
        CheckConstraint("reserved_bytes >= 0", name="reserved_non_negative"),
        # 账面不变量。数据库层兜底 —— 应用层的条件 UPDATE 是主防线，
        # 但数据回填、运维脚本、将来的批量对账都可能绕过它。
        CheckConstraint(
            "effective_bytes + reserved_bytes <= limit_bytes",
            name="within_limit",
        ),
    )


class UploadSession(Base):
    """一次上传的意图与状态机。

    **预占与释放必须成对**：`reserved_bytes` 是创建会话时预占的额度，
    在「完成」（转 effective）、「取消」、「过期」、「失败」四条路径上
    都必须还回去，且**每条路径只能还一次**（文档 02 §13.1：
    "每次释放只允许一次"）。

    "只允许一次"靠状态机 + 条件 UPDATE 实现：释放语句带
    `WHERE state IN (持有预占的状态)`，改成功才算还了。若同一个取消请求
    到达两次，第二次的 `rowcount` 为 0，于是不去重复扣减账面。
    """

    __tablename__ = "upload_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)

    # 原始文件名与分类在**创建会话时**就确定，完成时写入 file_objects。
    #
    # 文档 02 §4 的列清单里没有这两列，但**不加就跨实例不可用**：
    # 本项目的部署模式是"API 双实例"（研发执行计划 §5 的 D4 选定，
    # 架构图里 GW 后面是实例 A / 实例 B）。创建会话的请求可能落在 A，
    # 完成上传的请求落在 B —— 任何进程内缓存（模块级 dict、lru_cache）
    # 都会让"完成上传"随机失败，而失败率取决于负载均衡，
    # **这类缺陷在单机测试里永远不会出现**（本包开发时先写了进程内方案，
    # 随即自查发现，见待定项 OP-99）。
    #
    # 另一条理由与前一条同样重要：完成时的文件名必须与创建时一致。
    # 若让客户端在 `completions` 里重传文件名，就产生了一个"上传时报 A、
    # 完成时报 B"的替换面 —— 而协议 §4.2 的 completions body 里
    # 根本没有文件名字段，说明原设计也认为它不该由客户端二次提供。
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    classification: Mapped[str] = mapped_column(String(20), nullable=False)

    # 客户端声明的大小（协议 §4.2 的 byteSize）。它只是**估计** ——
    # 预占按它来，但完成时以服务端实测为准（见 service.complete_upload）。
    declared_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )

    state: Mapped[str] = mapped_column(String(20), nullable=False)

    # 客户端声明的摘要，仅作提示（文档 02 §4："摘要前端值仅作提示"）。
    expected_sha256: Mapped[str | None] = mapped_column(
        String(SHA256_HEX_LENGTH), nullable=True
    )
    # 服务端实测摘要 —— 写入 blobs 的权威值从这里取。
    actual_sha256: Mapped[str | None] = mapped_column(
        String(SHA256_HEX_LENGTH), nullable=True
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 分片在存储后端上的临时前缀。服务端生成，不含用户输入。
    temporary_key: Mapped[str] = mapped_column(String(512), nullable=False)

    # 固化结果。**文档 §4 未列此列**，但"重复调用 completions 必须返回
    # 同一个 fileId"离不开它：没有它，第二次调用只能重新固化，
    # 于是产生两份 blob 与两份配额占用。已登记为待定项。
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("file_objects.id", ondelete="RESTRICT"),
        nullable=True,
    )

    created_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint(
            f"purpose IN ({FILE_PURPOSE_LIST_SQL})", name="purpose_valid"
        ),
        CheckConstraint(
            f"state IN ({UPLOAD_STATE_LIST_SQL})", name="state_valid"
        ),
        CheckConstraint(
            f"classification IN ({CLASSIFICATION_LIST_SQL})",
            name="classification_valid",
        ),
        CheckConstraint("declared_size >= 0", name="declared_size_non_negative"),
        CheckConstraint("reserved_bytes >= 0", name="reserved_non_negative"),
        CheckConstraint("received_bytes >= 0", name="received_bytes_non_negative"),
        # 完成的会话必须有固化结果；未完成的不允许有。
        # 不写这条会出现"状态是 COMPLETED 但查不到文件"的悬空记录。
        CheckConstraint(
            "(state = 'COMPLETED') = (file_id IS NOT NULL)",
            name="completed_has_file",
        ),
        # TTL 回收的扫描路径。
        Index(
            "ix_upload_sessions_expires_at",
            "expires_at",
            postgresql_where=text(
                "state IN ('UPLOADING', 'COMPLETING')"
            ),
        ),
        Index("ix_upload_sessions_project_id", "project_id"),
        Index("ix_upload_sessions_created_by", "created_by"),
    )


class UploadPart(Base):
    """已接收的分片。

    `PK(upload_id, part_no)` 保证同片只能有一条记录；文档 02 §4 要求
    "重复分片内容不一致拒绝" —— 不一致的判定靠比对 `sha256`，
    而不是靠主键冲突（主键冲突只能告诉你"这片已存在"）。
    """

    __tablename__ = "upload_parts"

    upload_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("upload_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    part_no: Mapped[int] = mapped_column(Integer, primary_key=True)

    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column(
        String(SHA256_HEX_LENGTH), nullable=True
    )
    storage_etag: Mapped[str | None] = mapped_column(String(255), nullable=True)

    uploaded_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("part_no >= 1", name="part_no_positive"),
        CheckConstraint("byte_size >= 0", name="byte_size_non_negative"),
    )


__all__ = [
    "CLASSIFICATIONS",
    "CLASSIFICATION_LIST_SQL",
    "CLASSIFICATION_SUPPORTED",
    "CLASSIFICATION_UNSUPPORTED",
    "DEFAULT_PART_SIZE_BYTES",
    "FILE_PURPOSES",
    "FILE_PURPOSE_LIST_SQL",
    "QUOTA_LIMIT_BYTES",
    "QUOTA_PURPOSES",
    "SHA256_HEX_LENGTH",
    "STORAGE_BACKENDS",
    "STORAGE_BACKEND_LOCAL",
    "STORAGE_BACKEND_MINIO",
    "STORAGE_STATES",
    "STORAGE_STATE_LIST_SQL",
    "SCAN_STATUSES",
    "SCAN_STATUS_LIST_SQL",
    "SUPPORTED_EXTENSIONS",
    "SUPPORTED_MIME_TYPES",
    "UPLOAD_SESSION_TTL_SECONDS",
    "UPLOAD_STATES",
    "UPLOAD_STATES_HOLDING_RESERVATION",
    "UPLOAD_STATE_LIST_SQL",
    "VISIBILITIES",
    "VISIBILITY_LIST_SQL",
    "Blob",
    "FileObject",
    "ProjectFileLink",
    "QuotaAccount",
    "UploadPart",
    "UploadSession",
    "PURPOSE_BRIEF",
    "PURPOSE_EXPORT",
    "PURPOSE_GENERATED",
    "PURPOSE_PROOF",
    "PURPOSE_REFERENCE",
    "PURPOSE_RESEARCH",
    "SCAN_STATUS_CLEAN",
    "SCAN_STATUS_INFECTED",
    "SCAN_STATUS_PENDING",
    "SCAN_STATUS_SKIPPED",
    "STORAGE_STATE_ACTIVE",
    "STORAGE_STATE_DELETED",
    "STORAGE_STATE_PENDING_DELETE",
    "UPLOAD_STATE_CANCELLED",
    "UPLOAD_STATE_COMPLETED",
    "UPLOAD_STATE_COMPLETING",
    "UPLOAD_STATE_EXPIRED",
    "UPLOAD_STATE_FAILED",
    "UPLOAD_STATE_UPLOADING",
    "UPLOAD_STATES_HOLDING_RESERVATION_LIST_SQL",
    "VISIBILITY_KNOWLEDGE",
    "VISIBILITY_PROJECT",
]
