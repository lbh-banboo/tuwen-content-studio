"""模型网关的存储模型（WP16）—— 4 张表。

按数据结构文档 02 §11「任务、外部服务与费用」建立：

| 表 | 语义 | 本包建它的理由 |
|---|---|---|
| `providers` | 供应商与**网络模式** | `network_mode` 是出网策略判定的输入 |
| `model_profiles` | 模型档案 | 网关的派发目标；与任务绑定准确 profile |
| `capability_policies` | 项目级**出网策略** | 验收第一半的落点（LOCAL_ONLY / 缺省拒绝） |
| `provider_calls` | 外部调用记录 | 验收第二半的落点（未知状态不得直接另发计费请求） |

**§11 里同属这一节、但本包刻意不建的表**（边界按"有没有设计空间"划）：

| 表 | 归属 | 理由 |
|---|---|---|
| `pricing_versions` / `cost_estimates` / `project_budgets` / `budget_reservations` / `usage_ledger` | **WP25**（供应商计价版本；预估；预算预占；账本基础） | 费用体系是**一整套**策略（预估/预占/结算/退款/汇率），不是一个列。本包只在 `CapabilityResult` 里**透传** `usage` / `cost` |
| `job_dependencies` | WP32 | 见 OP-106（建了不用就是死表） |
| `runtime_settings` | 平台层（尚无包认领） | 见 OP-104（`jobs.kind` 取值域未定） |

**本包不给 `projects` 加 `is_local_only`**：出网是"策略"问题不是"项目"问题 ——
落在 `capability_policies` 上，将来模式 B 上线只需改策略行、不动项目表。登记 OP-127。

---

## 为什么出网留痕有**自己的表**，而不是复用 WP15 的 `audit_events`

一开始的设计是复用审计表（"它已经有'不可存正文'与'不可篡改'两条硬保证"）。
**实测撞墙了**：`audit_events` 有一条 `hash_pair_consistent` CHECK ——
`before_hash` 与 `after_hash` 必须**同时有或同时无**。那条约束为
"**变更**记录"而设（"从哪改到哪是一对的"），而出网留痕是**事件**：
它没有"before"。硬塞进去要么造一个假的 before（污染语义），
要么把事件拆成两条（更荒谬）。

**结论：复用错误的抽象比多一张表更糟。** ADR-10 本来就把"网关出网记录"
列为一个**有名字的备份对象**，它的留痕要求（摘要 + 提示词版本 + 字段类别 + traceId）
也正好一一对应到列。因此建这张表，并**复用同一套不可变机制**
（`platform/immutability.py` 的 `guard_immutable` + 触发器生成器），
保持"凭据不可篡改"这条性质与审计一致。见 ISS-47。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.gateway.types import (
    CALL_PREPARED,
    CALL_STATES,
    CAPABILITIES,
    DATA_CLASSES,
    EGRESS_DECISIONS,
    EXTERNAL_POLICIES,
    EXTERNAL_POLICY_LOCAL_ONLY,
    NETWORK_MODES,
    PROVIDER_ACTIVE,
    PROVIDER_STATUSES,
)
from rednote.platform.base import Base, created_at_column, updated_at_column, uuid_pk
from rednote.platform.immutability import guard_immutable

# ---------------------------------------------------------------------------
# jsonb 数组取值域的 CHECK 用的 SQL 字面量
#
# **为什么要做 CHECK**：`capability_types` / `allowed_data_classes` 的取值域
# **已经在协议 §12.1 与 ADR-10 里定死了**（不像 `jobs.kind` 那样未定），
# 因此可以也应当用 CHECK 兜住 —— 一旦允许任意字符串，
# 同一类能力会出现三种拼写（`TEXT_STRUCTURED` / `text_structured` / `textStructured`），
# 而出网策略按类别比对时会**静默漏放行或漏拦**。
#
# 用 `<@`（jsonb 的"被包含于"）而不是逐元素展开：一次比较覆盖全部元素，
# 且先由 `jsonb_typeof(...) = 'array'` 保证形状（否则标量也能"被包含于"）。
# ---------------------------------------------------------------------------

_CAPABILITY_LIST_SQL: Final[str] = json.dumps(sorted(CAPABILITIES))
_DATA_CLASS_LIST_SQL: Final[str] = json.dumps(sorted(DATA_CLASSES))


def _list_in_list(table_column: str, allowed_sql: str) -> str:
    return (
        f"jsonb_typeof({table_column}) = 'array' "
        f"AND {table_column} <@ '{allowed_sql}'::jsonb"
    )


class Provider(Base):
    """一个模型供应商。

    **`secret_ref` 只存引用，不存密钥正文**（§11 原句"不存密钥正文"）。
    真实密钥来自部署层的 Secret 挂载 —— 落库会让备份、导出、
    日志采集链路都把它带走，而 ADR-10 把备份列为"一律不出网"的 E-5 类数据，
    也就是说密钥一旦入库，它的泄露面就被绑在了备份链路上。
    """

    __tablename__ = "providers"

    id: Mapped[uuid.UUID] = uuid_pk()

    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # 能力集合（jsonb 数组）。取值域由 CHECK 兜住，见上方说明。
    capability_types: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # LOCAL = 内网推理（不出网）；EXTERNAL = 出网调用。
    # **它是出网策略判定的输入**，不是展示字段。
    network_mode: Mapped[str] = mapped_column(String(20), nullable=False)

    endpoint: Mapped[str] = mapped_column(String(300), nullable=False)

    # 密钥引用（如 Secret 名 / Vault 路径），**不是密钥本身**。
    secret_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=PROVIDER_ACTIVE
    )

    # 该 supplier 对应的策略版本（供应商侧的口径版本，与项目的 policy_version 不是同一个。
    # §11 有该列；它记录"这个供应商的接入口径版本"，变更时可追溯到当时的口径）。
    policy_version: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="v1"
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        UniqueConstraint("name", name="uq_providers_name"),
        CheckConstraint(
            "network_mode IN (" + ", ".join(f"'{m}'" for m in NETWORK_MODES) + ")",
            name="network_mode_valid",
        ),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in PROVIDER_STATUSES) + ")",
            name="status_valid",
        ),
        CheckConstraint(
            _list_in_list("capability_types", _CAPABILITY_LIST_SQL),
            name="capability_types_valid",
        ),
    )


class ModelProfile(Base):
    """一个模型档案（供应商 + 模型名 + 修订）。"""

    __tablename__ = "model_profiles"

    id: Mapped[uuid.UUID] = uuid_pk()

    provider_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("providers.id", ondelete="RESTRICT"),
        nullable=False,
    )

    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # 该档案支持的能力（与供应商能力取交集才是有效能力）。
    capabilities: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # 参数 schema（温度、最大 token 等）。形状由供应商决定，故不做 CHECK。
    parameters_schema: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )

    # 计价版本（**WP25 的表**）。此处刻意**不建外键** ——
    # 目标表还不存在；建一个指向未来表的 FK 会让本迁移依赖 WP25 的落地顺序。
    # 本包只存引用，WP25 落地时按需补 FK。
    pricing_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    # 本地权重的哈希（模式 B 用；外部模型为 NULL）。
    local_weights_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        # `postgresql_nulls_not_distinct=True`：PG 默认认为两个 NULL **互不相同**，
        # 于是 (provider, model, NULL) 可以插很多行 —— 而语义上它们是同一个档案。
        # 本项目锁定 PG 17（NULLS NOT DISTINCT 自 PG 15 起支持），所以能用它把
        # "revision 未填"也纳入唯一性。"同一模型建了两份档案"的后果是
        # 两次调用指向不同的 profile，而二者的计费口径可能不同。
        UniqueConstraint(
            "provider_id",
            "model_name",
            "revision",
            name="uq_model_profiles_provider_model_revision",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            _list_in_list("capabilities", _CAPABILITY_LIST_SQL),
            name="capabilities_valid",
        ),
    )


class CapabilityPolicy(Base):
    """项目级出网策略。**缺省拒绝**。

    **`external_policy` 的服务端默认值是 `LOCAL_ONLY`**（不是 `ALLOW_EXTERNAL`）：
    "缺省拒绝"要落在**两个**地方 ——
    ① 列默认值（直插 SQL 也不放行）；
    ② 判定函数里"查不到策略即拒绝"（`policy.evaluate_egress` 第 4 步）。
    只做其中一个都会留下缺口：只有①时，"没建策略行"就绕过了；
    只有②时，直接 `INSERT ... DEFAULT` 会得到允许语义的默认值。
    """

    __tablename__ = "capability_policies"

    id: Mapped[uuid.UUID] = uuid_pk()

    # **可空**：NULL 表示平台级策略。本包不做"项目级缺失则回落平台级"（见 policy 模块说明）。
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=True,
    )

    policy_version: Mapped[str] = mapped_column(String(50), nullable=False)

    external_policy: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=EXTERNAL_POLICY_LOCAL_ONLY
    )

    # 允许出网的供应商（**空白名单 = 一个都不允许**，见 policy 模块说明）。
    allowed_provider_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)),
        nullable=False,
        server_default=text("'{}'::uuid[]"),
    )

    # 允许出网的数据类别（ADR-10 出网矩阵的左侧列）。
    allowed_data_classes: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # 并发限额（形状待定：按供应商 / 按能力 / 全局各说各话，属 OP-131）。
    concurrency_limits: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "policy_version",
            name="uq_capability_policies_project_policy_version",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "external_policy IN ("
            + ", ".join(f"'{p}'" for p in EXTERNAL_POLICIES)
            + ")",
            name="external_policy_valid",
        ),
        CheckConstraint(
            _list_in_list("allowed_data_classes", _DATA_CLASS_LIST_SQL),
            name="allowed_data_classes_valid",
        ),
        Index("ix_capability_policies_project", "project_id", "created_at"),
    )


class ProviderCall(Base):
    """一次外部调用的记录。**验收第二半的落点**。

    **`UQ(provider_id, request_key)` 是整条防线**：派发前先按它查一次，
    查到且状态在途（`CALL_IN_FLIGHT_STATES`）就**直接返回既有调用**，不再发出去。
    数据结构 §13.4 的原句是"供应商调用前持久化 `request_key`，
    恢复先检查已有 `provider_call`" —— 顺序不能反：
    先发再记的话，进程在"发出去"与"记下来"之间崩溃，就永远丢了这次调用的凭据。

    **`PREPARED` 是"已持久化凭证但还没发出去"**。它必须是一个落库状态
    （不能只存在内存里），否则崩溃恢复时无法区分
    "还没发"（可以发）与"不知道发没发"（不能发）。
    """

    __tablename__ = "provider_calls"

    id: Mapped[uuid.UUID] = uuid_pk()

    # §11 该列没有 `?`（必填）。把调用绑到 attempt 上是刻意的：
    # 租约、fence_token、重试次数都挂在 attempt 上，
    # 而"这次调用算哪一次尝试的"决定了它能不能被旧的（已失联的）Worker 写回。
    job_attempt_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("job_attempts.id", ondelete="RESTRICT"),
        nullable=False,
    )

    provider_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("providers.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 幂等键。构造只有一个来源（`service.build_request_key`），见 OP-129。
    request_key: Mapped[str] = mapped_column(String(200), nullable=False)

    external_request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=CALL_PREPARED
    )

    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_poll_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 请求/响应的证据引用（§11：请求必有、响应可空）。
    # 出网调用 → `audit:<audit_events.id>`（WP15 的留痕记录）；
    # 本地调用 → `local:<request_key>`（没有出网，也就没有出网留痕）。
    request_evidence_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    response_evidence_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # 状态不明的原因（只在 UNKNOWN 时有值）。§11 的 `status_unknown_reason?`。
    status_unknown_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # 最近一次失败的错误码（供应商侧或传输层的）。
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        # 幂等键的唯一性 —— **验收第二半的地基**。
        UniqueConstraint(
            "provider_id", "request_key", name="uq_provider_calls_provider_request"
        ),
        CheckConstraint(
            "state IN (" + ", ".join(f"'{s}'" for s in CALL_STATES) + ")",
            name="state_valid",
        ),
        # 状态不明原因的作用域：只在"曾经不明过"的行上有值。
        #
        # **不能写成 `status_unknown_reason IS NULL OR state = 'UNKNOWN'`** ——
        # `reconcile_call` 从 UNKNOWN 迁到终态时**刻意保留**这个原因
        # （它是"曾经不明过"的历史，排障要看得见那次不明的原因）。
        # 写成"只有 UNKNOWN 允许有值"会让核对动作**必然撞 CHECK**，
        # 而那个失败发生在人工核对的那一刻 —— 也就是最不能出错的时候。
        # 本包在渲染 DDL 时发现（见 ISS-46）。
        CheckConstraint(
            "status_unknown_reason IS NULL "
            "OR state IN ('UNKNOWN', 'SUCCEEDED', 'FAILED')",
            name="status_unknown_reason_scope",
        ),
        # 轮询路径的部分索引：只有"在途且排定了下次轮询时间"的调用需要它。
        # 已终态的行（占绝大多数）不进索引 —— 与 WP15 的未读回执索引同一判断。
        Index(
            "ix_provider_calls_poll_due",
            "state",
            "next_poll_at",
            postgresql_where=text("state IN ('SUBMITTED', 'POLLING')"),
        ),
        # "这个供应商的调用历史"（排障与健康度）。
        Index("ix_provider_calls_provider_created", "provider_id", "created_at"),
    )


class GatewayEgress(Base):
    """一条出网留痕。**追加，且不可篡改**（与 `audit_events` 同性质）。

    ADR-10 决策 3 的留痕要求逐项对应到列：

    | ADR-10 要求 | 列 |
    |---|---|
    | 摘要 | `payload_digest`（`char(64)`，规范化哈希） |
    | 提示词版本 | `prompt_version` |
    | 字段类别 | `data_classes`（jsonb 数组，取值域受 CHECK 约束） |
    | traceId | `trace_id` |

    **没有可存正文的列**（与 WP15 审计同一手法）：最长的文本列是
    `blocked_reason`（50）与 `trace_id`（100），都是**受控格式**，
    放不下业务正文。想存也存不进去。

    **`decision` 只有两个取值**，且它承载 ADR-10 的违规判定口径：

    > 任何一条 `LOCAL_ONLY` 项目的**出网记录** = 该次调用违规。

    因此"被拒"也要留痕（`decision=DENIED`），但被拒**不算出网记录** ——
    判据是"`decision=ALLOWED` 且项目是 LOCAL_ONLY"。
    这两件事必须能分开数，所以`decision` 是一个显式列，而不是靠"有没有行"。

    **本地调用不留痕**：本地供应商不出网，记一条"出网记录"会让上述口径
    退化成"本地调用也算违规"，模式 B 下系统将寸步难行。
    """

    __tablename__ = "gateway_egress"

    id: Mapped[uuid.UUID] = uuid_pk()

    # 关联的调用。**被拒时为空** —— 那时确实没有调用可指（策略判定在派发之前）。
    provider_call_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("provider_calls.id", ondelete="RESTRICT"),
        nullable=True,
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    provider_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("providers.id", ondelete="RESTRICT"),
        nullable=False,
    )

    capability: Mapped[str] = mapped_column(String(50), nullable=False)

    # 本次出网涉及的数据类别（ADR-10 出网矩阵的左侧列）。
    data_classes: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # ALLOWED / DENIED。**它是违规判定口径的判据列**。
    decision: Mapped[str] = mapped_column(String(20), nullable=False)

    # 否决原因（只在 DENIED 时有值）。
    blocked_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)

    policy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(100), nullable=True)

    trace_id: Mapped[str] = mapped_column(String(100), nullable=False)

    # 出网内容的规范化摘要（**不是正文**）。没有上下文时可空。
    payload_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            "decision IN (" + ", ".join(f"'{d}'" for d in EGRESS_DECISIONS) + ")",
            name="decision_valid",
        ),
        # ALLOWED ⟺ 有 blocked_reason 之外的对应调用。
        # 让"允许却没留下调用记录"在结构上不可能 —— 那意味着凭据丢了。
        CheckConstraint(
            "decision = 'DENIED' OR provider_call_id IS NOT NULL",
            name="allowed_requires_call",
        ),
        # 否决必须说明原因；允许不该有原因。
        CheckConstraint(
            "(decision = 'DENIED') = (blocked_reason IS NOT NULL)",
            name="blocked_reason_consistent",
        ),
        CheckConstraint(
            _list_in_list("data_classes", _DATA_CLASS_LIST_SQL),
            name="data_classes_valid",
        ),
        # "这个项目出过哪些网"（合规核查的主路径）。
        Index("ix_gateway_egress_project_created", "project_id", "created_at"),
        # "这次执行出了什么网"（顺着 traceId 排障）。
        Index("ix_gateway_egress_trace", "trace_id"),
    )


# 本模块需要不可变保护的表。
#
# **只有 `gateway_egress`**：它是**凭据**（出网留痕，写完不改）。
# 其余 4 张表全部是可变状态（`provider_calls.state` 会迁移、
# `capability_policies` 会被改、`providers` 会停用）。
#
# 与 WP15 的 `audit_events` 并列：两者都是"凭据"，共用同一套不可变机制
# （`platform/immutability.py`），保证"审计被篡改"与"出网记录被篡改"
# 能用**同一个判据**识别（sqlstate 23001）。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = ("gateway_egress",)

# **两层保护缺一不可**（WP11 的核心设计，WP15 踩过只做一层）：
#
# | 层 | 管住什么 | 给出什么错误 |
# |---|---|---|
# | ORM 事件（本行） | 应用的正常写入路径 | **可读的领域错误**（`ImmutableRecordError`） |
# | 数据库触发器（`ddl.py`） | 所有写入（手工 SQL / 回填 / 运维脚本） | SQLSTATE `23001` |
guard_immutable(GatewayEgress)


__all__ = [
    "EGRESS_DECISIONS",
    "IMMUTABLE_TABLES",
    "CapabilityPolicy",
    "GatewayEgress",
    "ModelProfile",
    "Provider",
    "ProviderCall",
]
