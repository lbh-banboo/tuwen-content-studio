"""WP24 两张表的存储模型。

| 表 | 角色 | 可变性 |
|---|---|---|
| `runtime_settings` | 配置的**历史**（追加式）。当前生效值 = 同一 `(key, scope_kind, project_id)` 下 `revision_no` 最大那行 | **不可变**（触发器拦 UPDATE/DELETE） |
| `plan_drafts` | 步骤 4 的工作区（场景比例 + 形式比例 + 配对）。一项目一份 | **可变**（`lock_version`，ETag 载体） |

---

## 一、为什么 `runtime_settings` 是追加式而不是"就地 UPDATE + 审计表"

| 维度 | 追加式（本实现） | 就地 UPDATE + 审计表 |
|---|---|---|
| "变更前值 / 变更后值完整快照" | 前值 = **上一行**，天然可读（不是哈希） | 需要审计表**自己带快照**，且快照与真值**可能分叉而不报错** |
| 不可篡改 | 表级不可变，**一个触发器守全部** | 要守两张表，且"当前值"那张**仍可被改** |
| 查历史 | `ORDER BY revision_no` 即可 | 要 JOIN |
| 代价 | 表随变更增长 | — |

代价可忽略（ADR-13 代价 #4 原文："量级极小，可忽略"）。

**ADR-13 的关键句**：审计记录"不可被后续修改或删除，**与费用账本、版本快照
同级对待**"。本仓的费用账本（`usage_ledger`）与版本快照（`brief_versions` /
`production_baselines`）**都是追加式** —— 就地 UPDATE 与这个级别不同。

## 二、`revision_no` 而不是 `created_at` 排序（R9）

`now()` 在 PostgreSQL 里是**事务开始时刻**，同一事务内插入的多行时间戳
完全相同，按它排序得到的是**不确定的顺序**（WP17 的 ISS-52 实证）。
因此"当前生效值"的排序键必须是**显式递增的 `revision_no`**，
配两条部分唯一索引保证全序。

## 三、★ OP-60：`origin` 列（老大拍板"支持且必须有项目级副本"）

三态模型（详见 `docs/m3/WP24-*.md` §3.6.1）：

| `origin` | 语义 | 是否跟随全局变更 |
|---|---|---|
| `GLOBAL` | 平台级真值 | — |
| `INHERITED` | 项目级**副本**（全局值的快照） | ✅ 是（全局变更同事务级联刷新） |
| `MANUAL` | 项目级**覆盖**（运营显式改过） | ❌ 否 |

**为什么用一列而不是两张表**："是否跟随"是**一行的属性**，不是**一类实体的属性**。
同一项目从"跟随"变成"覆盖"时只是插入一行 `MANUAL` 的新版本 ——
追加式表天然承载这次语义迁移，**不需要搬家**。这也让"当前生效值"的
读取路径始终只有一条（`revision_no` 最大那行），不因 `origin` 而分叉。

## 四、⚠️ `NULL` 在唯一约束里不等于 `NULL`（本包最易写错处）

`UNIQUE(key, scope_kind, project_id, revision_no)` **对全局行完全不生效** ——
因为 `project_id IS NULL`，而 PG 认为 `NULL != NULL`，
于是同一个 `(key, 'GLOBAL', NULL, 1)` 可以**无限重复插入**，且**不报错**。

**处置是两条部分唯一索引**（在 `__table_args__` 里）：

    CREATE UNIQUE INDEX uq_runtime_settings_global
      ON runtime_settings (key, revision_no) WHERE scope_kind = 'GLOBAL';
    CREATE UNIQUE INDEX uq_runtime_settings_project
      ON runtime_settings (key, project_id, revision_no) WHERE scope_kind = 'PROJECT';

**不用 `COALESCE(project_id, sentinel)` 的做法**：哨兵值会污染数据，
且"哨兵"这个约定本身**没有任何约束保证**。

**部分索引的已知风险**（本项目已有前车之鉴）：`WHERE` 子句写错会让索引
**静默退化**为不生效 —— 没有任何报错，只是约束不再起作用。
因此证据脚本必须做**活性验证**：写一个与 `WHERE` 不匹配的重复行，
断言它**能被插进去**（证明索引确实在按 `WHERE` 过滤），
再写一个匹配的重复行，断言它**被拒**（`23505`）。只做后者会漏掉
"`WHERE` 写成了永远不会命中的条件"这种情形。

## 五、`plan_drafts` 为什么是独立表而不是挂在 `information_drafts` 上

`information_drafts`（WP22）是**步骤 3 的事实草稿**，它的生命周期在
基线固化后结束；而步骤 4 的场景/形式配置是**生产计划的输入**，
两者变更原因、变更时机、可变性都不同（前者固化后只读，后者反复调整）。
挂在一起会让"改一个场景比例"变成"改事实草稿"的版本冲突。

**场景的来源**是 `information_drafts.scenes`（每行 `{sceneId, name, audience,
factRefs, state}`）—— 但本表**存快照而不是外键**：场景的 `state` 可以在
步骤 3 里被停用，而本表的草稿是"当时基于哪些场景配的"的**历史事实**。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

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

from rednote.modules.planning.types import (
    DRAFT_STATE_LIST_SQL,
    MAX_CHANGE_REASON_CHARS,
    MAX_QUANTITY_BOUND,
    MIN_QUANTITY,
    SETTING_KEY_LIST_SQL,
    SETTING_ORIGIN_LIST_SQL,
    SETTING_SCOPE_GLOBAL,
    SETTING_SCOPE_LIST_SQL,
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
# **为什么 `runtime_settings` 在其中**：它是配置的**历史**。
# ADR-13 要求审计"不可被后续修改或删除，与费用账本、版本快照同级"。
# 改一行 = 篡改"这个上限曾经是多少"的答案，而排障与追责全靠它。
#
# **为什么 `plan_drafts` 不在其中**：它是**工作区**，运营反复调整，
# 必须可变（`lock_version` 是其 ETag 载体）。
IMMUTABLE_TABLES: tuple[str, ...] = ("runtime_settings",)


class RuntimeSetting(Base):
    """一行配置（**追加式**，每个变更插一行）。**不可变**。

    `value` 用 JSONB 而不是 `text`：三种键的形状不同
    （`batch.limits` 是 `{TRIAL,SCALE,BULK}`、`research.maxEffectiveFiles`
    是一个数、`platform.mediaLimits` 是一个嵌套 map），
    用 JSONB 才能让读取方按 key 分派到各自的 Pydantic 模型。
    **代价**：数据库层无法对 `value` 的内容做 CHECK（形状由应用层保证）。
    这是 R8（"万能杂货表"）的处置：**每个键有独立的校验模型，
    未知键拒绝而不是放行**（见 `settings_service.py`）。
    """

    __tablename__ = "runtime_settings"

    id: Mapped[uuid.UUID] = uuid_pk()

    # 配置键。取值域是**闭集**（`SETTING_KEY_LIST_SQL`）。
    # 闭集的理由见 `types.py`：`key='batch.limt'`（拼错）会静默成为新配置项，
    # 而读取方读 `batch.limits` ⇒ "改了不生效"且不报错。
    key: Mapped[str] = mapped_column(String(80), nullable=False)

    scope_kind: Mapped[str] = mapped_column(String(20), nullable=False)

    # 项目级配置指向的项目；`GLOBAL` 时**必须为 NULL**。
    #
    # `ON DELETE RESTRICT`：项目被删时不允许静默丢掉它的配置历史
    # （而项目是否可删由 WP07 决定，不在本包）。
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=True,
    )

    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # 同一 `(key, scope_kind, project_id)` 下的**显式递增序号**。
    # 当前生效值 = 该组合下 `revision_no` 最大的那行。
    # 不用 `created_at` 排序的理由见模块 docstring 第二节。
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)

    # 行的来源（见模块 docstring 第三节）。
    origin: Mapped[str] = mapped_column(String(20), nullable=False)

    # --- 审计字段（ADR-13 要求：前后值 / 人 / 时间 / 理由 / 生效范围 / requestId）---
    # "变更前值"**不需要一列** —— 它就是同一组合下 `revision_no` 的上一行。
    # 这正是追加式相对"就地 UPDATE + 审计表"的优势：**没有第二个真值来源**。
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(
        String(MAX_CHANGE_REASON_CHARS), nullable=True
    )
    request_id: Mapped[str | None] = mapped_column(String(80), nullable=True)

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            f"key IN ({SETTING_KEY_LIST_SQL})", name="key_valid"
        ),
        CheckConstraint(
            f"scope_kind IN ({SETTING_SCOPE_LIST_SQL})", name="scope_kind_valid"
        ),
        CheckConstraint(
            f"origin IN ({SETTING_ORIGIN_LIST_SQL})", name="origin_valid"
        ),
        CheckConstraint("revision_no >= 1", name="revision_no_positive"),
        # 作用域与项目的**互相一致性**：
        #   · GLOBAL ⇒ project_id IS NULL（"全局"不该指向某个项目）
        #   · PROJECT ⇒ project_id IS NOT NULL（"项目级"必须说清是哪个项目）
        # 少了这条，一行 `(scope_kind='PROJECT', project_id=NULL)` 是合法数据，
        # 而它被读成"某个没有项目的项目级配置"——**不报错**。
        CheckConstraint(
            "(scope_kind = 'GLOBAL' AND project_id IS NULL)"
            " OR (scope_kind = 'PROJECT' AND project_id IS NOT NULL)",
            name="scope_matches_project",
        ),
        # `origin` 与作用域的一致性：
        #   · GLOBAL 行只能是 origin='GLOBAL'（"全局真值"没有别的来源）
        #   · PROJECT 行只能是 INHERITED / MANUAL（副本或覆盖）
        # 少了这条，一行 `(scope_kind='GLOBAL', origin='MANUAL')` 会让
        # 级联刷新的判断逻辑（读 `origin`）得出错误结论。
        CheckConstraint(
            "(scope_kind = 'GLOBAL' AND origin = 'GLOBAL')"
            " OR (scope_kind = 'PROJECT' AND origin IN ('INHERITED','MANUAL'))",
            name="origin_matches_scope",
        ),
        # `value` 必须是 JSON **object**（不是数组/标量）：三种键的形状
        # 都是对象，而标量会让 `value['TRIAL']` 抛 `TypeError` 而不是可读错误。
        CheckConstraint(
            "jsonb_typeof(value) = 'object'", name="value_is_object"
        ),
        # 审计理由：**PROJECT 级变更必须带理由**（那条要求出自 ADR-13 的审计表）。
        # GLOBAL 也要求 —— 见 `settings_service.py` 的显式校验；
        # 这里只兜"两者都不能是空串"这一层。
        CheckConstraint(
            "reason IS NULL OR char_length(reason) > 0", name="reason_not_blank"
        ),
        # --- 四条查询路径的索引 -------------------------------------------
        # ① 当前生效值：按 (key, scope_kind, project_id) 取 revision_no 最大
        Index(
            "ix_runtime_settings_lookup",
            "key",
            "scope_kind",
            "project_id",
            "revision_no",
        ),
        # ② 全局变更时**枚举所有项目**的副本（级联刷新）：
        #    按 origin 定位"需要跟随"的行
        Index("ix_runtime_settings_origin", "key", "origin"),
        # ③ `GET /settings/projects/diverged`：找出与全局不同的项目
        Index("ix_runtime_settings_project_id", "project_id"),
        # ④ 按 key 列历史（管理侧"这个上限被谁改过"）
        Index("ix_runtime_settings_key_created", "key", "created_at"),
        # --- ★ 两条部分唯一索引（模块 docstring 第四节）-------------------
        # 这两条是"当前生效值"这个概念的**结构基础**，不是性能优化：
        #   · 没有它们，同一个 (key, revision_no) 可以无限重复插入；
        #   · 重复插入后，`ORDER BY revision_no DESC LIMIT 1` 返回哪一行
        #     是**不确定的** ⇒ 两个实例可能读到不同的"当前值"，且不报错。
        #
        # 为什么必须带 `WHERE`：普通 `UNIQUE(key, scope_kind, project_id,
        # revision_no)` 对 GLOBAL 行**完全失效**（`project_id IS NULL`，
        # 而 PG 认为 `NULL != NULL`）。带 `WHERE scope_kind = 'GLOBAL'`
        # 的部分索引把 `project_id` 从键里去掉，NULL 参与比较的问题就
        # 不存在了；PROJECT 行则由另一条索引守（此时 `project_id` 非空）。
        #
        # ⚠️ 部分索引的 `WHERE` 写错会**静默失效**（不报错，只是不再约束）。
        # 因此证据脚本必须做**双向**活性验证：写一个**不匹配** `WHERE` 的
        # 重复行，断言它**能插进去**（证明索引确实在按 `WHERE` 过滤）；
        # 再写一个**匹配**的重复行，断言被拒（`23505`）。只做后者会漏掉
        # "`WHERE` 写成了永远命不中的条件"这种情形。
        Index(
            "uq_runtime_settings_global",
            "key",
            "revision_no",
            unique=True,
            postgresql_where=text("scope_kind = 'GLOBAL'"),
        ),
        Index(
            "uq_runtime_settings_project",
            "key",
            "project_id",
            "revision_no",
            unique=True,
            postgresql_where=text("scope_kind = 'PROJECT'"),
        ),
    )


class PlanDraft(Base):
    """步骤 4 的工作区（一项目一份）。**可变**。

    `scene_items` / `form_items` 是 JSONB 数组，每行形状：

        {
          "key": "scene-<uuid>",          # 稳定标识（分配次序的第三级）
          "name": "早八通勤急救",          # 展示名，不参与任何比较
          "weightPermille": 280,          # 比例（千分位整数）
          "sortOrder": 0,                 # 显式次序（分配次序的第二级）
          "capacity": null,               # 容量上限（None = 无上限）
          "state": "ENABLED",             # ENABLED / DISABLED
          "disabledReason": null                  # 停用原因（原型有这一列）
        }

    **为什么用 JSONB 而不是两张子表**：
    这两个数组是**一份草稿内部的结构**，没有独立的生命周期 ——
    它们不跨草稿被引用、不被外键指向、不需要独立查询
    （"哪些草稿用了某个场景"不是任何需求）。
    建两张子表会带来"子表与父表的行数必须一致"这类**没有需求支撑的**不变量。

    **代价已登记**：JSONB 内部无法落 CHECK（`weightPermille` 的和等于 1000
    这一条只能由服务层保证）。这与 `runtime_settings.value` 同一取舍 ——
    处置也一样：**服务层是唯一写入口**，且纯函数层（`permille` /
    `allocation`）让"非法比例"在任何路径上都算不出结果。

    ## `lock_version` 与"最后一写获胜"

    原型页面是"拖数字 → 自动保存"的交互，意味着**并发编辑是真实场景**
    （两个运营同时调比例）。`lock_version` 是乐观锁：客户端提交时带
    `If-Match`，版本不符返回 412 而不是静默覆盖。
    **不用悲观锁**：拖数字的交互会产生大量短暂写入，行锁会让人互相等。
    """

    __tablename__ = "plan_drafts"

    id: Mapped[uuid.UUID] = uuid_pk()

    # **一个项目一份草稿**（`unique`）—— 与 `quota_accounts` 的
    # `project_id` 做主键是同一语义（"一项目一份"）。
    # 本表用 surrogate PK + 唯一约束，理由是它还有 `id` 被引用的可能
    # （将来 PLAN 版本可能要指向草稿），而 surrogate 主键让这件事不必迁移。
    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )

    # 本草稿对应的基线（原型「基线 BL-v3」）。
    # **可空**：项目可能还没固化基线就开始配置计划（原型里基线已存在，
    # 但那不是必然）。可空 + 显式记录，比"偷偷用当前基线"好 ——
    # 后者会在基线切换后让草稿的语义悄悄改变。
    baseline_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("production_baselines.id", ondelete="RESTRICT"),
        nullable=True,
    )

    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'EDITING'")
    )

    # 本轮总量（原型「试产固定生成 10 条」）。**可空**：
    # 运营可能先配比例、后定总量（原型的交互顺序正是如此）。
    # 上界不在这里落 CHECK（ADR-13 决策把上界移到 runtime_settings），
    # 但**下界留着**（`quantity > 0`），理由见 `limits.py` 第二节。
    total_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 场景维度（来源：`information_drafts.scenes` 的快照）。
    scene_items: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # 内容形式维度（原型「第一版四种形式」）。
    form_items: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # 场景 × 形式的配对（原型「系统建议配对关系，运营可逐行改配」）。
    pairings: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
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
        CheckConstraint(f"state IN ({DRAFT_STATE_LIST_SQL})", name="state_valid"),
        # 下界（上界由 runtime_settings 管，见 ADR-13）。允许 NULL
        # （"还没定总量"是合法中间态），但一旦有值就必须 >= 1。
        CheckConstraint(
            f"total_quantity IS NULL OR total_quantity >= {MIN_QUANTITY}",
            name="total_quantity_positive",
        ),
        # `total_quantity` 的**防御性上界**：`runtime_settings` 是权威，
        # 但一行被裸 SQL 写进 10 亿条时，所有下游的成本计算都会爆炸。
        # 这条 CHECK 是"绕过应用层也拦得住"的那一层（与 ADR-13 决策
        # 把下界留在 DDL 同一手法：**结构性的不可能**留在 DDL）。
        CheckConstraint(
            f"total_quantity IS NULL OR total_quantity <= {MAX_QUANTITY_BOUND}",
            name="total_quantity_within_bound",
        ),
        CheckConstraint(
            "jsonb_typeof(scene_items) = 'array'", name="scene_items_is_array"
        ),
        CheckConstraint(
            "jsonb_typeof(form_items) = 'array'", name="form_items_is_array"
        ),
        CheckConstraint(
            "jsonb_typeof(pairings) = 'array'", name="pairings_is_array"
        ),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        Index("ix_plan_drafts_updated_at", "updated_at"),
    )


guard_immutable(RuntimeSetting)


__all__ = [
    "IMMUTABLE_TABLES",
    "PlanDraft",
    "RuntimeSetting",
]
