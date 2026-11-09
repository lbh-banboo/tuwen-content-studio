"""WP24 的不可变值类型与受控取值域。

**为什么单独一个模块**（与 WP11/WP16/WP20/WP23 同一手法）：这些取值同时被
`models.py`（CHECK 约束）、`permille.py` / `allocation.py` / `limits.py`（纯函数）、
`repository.py`（存储）与 `service.py`（编排）使用。定义在 service 里会让
其它几个反向导入 service —— 而本模块的分层刻意是单向的：

    types ← permille
    types ← allocation
    types ← limits
    types ← models
    types ← repository
    types ← settings_service
    types ← service

**跨层传递的值用 frozen dataclass 而不是 ORM 对象或裸 dict**（ORM 实例绑在
session 上，session 一关就 `DetachedInstanceError`；裸 dict 则没有取值域，
写错键名不报错）。

---

## 本模块承载的三条硬性质（验收 AC-10~12 的配置侧）

| 性质 | 本模块的落点 |
|---|---|
| **比例是离散份额** | `PERMILLE_TOTAL = 1000` —— 占比一律用**千分位整数**，全链路不得出现 `float` 百分比 |
| **取值必须受控** | `SETTING_KEYS` / `BATCH_KINDS` / `SETTING_SCOPES` / `SETTING_ORIGINS` —— 每个键有独立的校验模型，未知键**拒绝**而不是放行 |
| **分配必须可复现** | `AllocationItem` 的三元组 `(weight_permille, capacity, sort_order)` + `key` 构成全序输入，平局次序显式写进 `allocation.py` 的 docstring |

## 为什么占比用千分位而不是浮点（历史收敛决议 C19）

占比是**一组离散的份额**，本就不该用浮点表示。改千分位后：

- 「合计必须等于 1000」是一次**整数比较**，没有 epsilon 可谈；
- `30%` / `30.0%` / `29.999…%` 不再是三个不同值；
- 最大余数法的余数比较（`total × permille mod 1000`）也是整数运算，
  与 `float` 的舍入误差无关。

**粒度为什么选千分位而不是万分位**：历史 SOP 的粒度是 5%（20 个刻度），
千分位留了两位余量；再细就是给「28.7%」这种没人能解释的数字开门 ——
而这个数字最终要变成整数篇数，精度超过"1 篇"没有意义。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Final

# ---------------------------------------------------------------------------
# 比例：千分位整数
# ---------------------------------------------------------------------------

#: 比例合计的恒等值。所有维度的启用项之和必须**严格等于**它。
#:
#: **为什么是 1000 而不是 100**：见模块 docstring。SOP 的粒度是 5% = 50‰，
#: 千分位让「5% 的 1/10」也能表示，且不必引入小数。
PERMILLE_TOTAL: Final[int] = 1000

#: 单个比例的合法区间。0 允许（运营可以先把一个场景调到 0 而暂不停用），
#: 1000 允许（只有一个场景时它独占全部）。
MIN_PERMILLE: Final[int] = 0
MAX_PERMILLE: Final[int] = PERMILLE_TOTAL


# ---------------------------------------------------------------------------
# 配置键：受控取值域
# ---------------------------------------------------------------------------
# **为什么必须是闭集**（与 WP23 的 `TASK_KINDS`、WP11 拒绝"万能表"同一理由）：
# `runtime_settings` 是天生的"万能杂货表"形态 —— 有 `key`/`value` 两个自由列。
# 若不定义取值域，`key='batch.limt'`（拼错）会**静默**成为一个新配置项，
# 而读取方读的是 `batch.limits`，于是"改了不生效"且不报错。
# 这与 WP23 那个"同一任务类型出现两种拼写"是同一形态的缺陷。
SETTING_KEY_BATCH_LIMITS: Final[str] = "batch.limits"
SETTING_KEY_RESEARCH_MAX_FILES: Final[str] = "research.maxEffectiveFiles"
SETTING_KEY_PLATFORM_MEDIA_LIMITS: Final[str] = "platform.mediaLimits"

SETTING_KEYS: Final[frozenset[str]] = frozenset(
    {
        SETTING_KEY_BATCH_LIMITS,
        SETTING_KEY_RESEARCH_MAX_FILES,
        SETTING_KEY_PLATFORM_MEDIA_LIMITS,
    }
)

# 与 CHECK 约束保持一致的 SQL 片段（生成用，单一来源）。
SETTING_KEY_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(SETTING_KEYS)
)

#: 尚未落值的键（机制已建、实测值未到 ⇒ ADR-13 的 OP-64）。
#:
#: **为什么显式列出来**：这些键的存在会让"迁移里为什么没有默认值"变得可解释 ——
#: 它们是**占位**，不是遗漏。读取它们的一方必须能处理"无值"（回落"未配置"）。
PLACEHOLDER_SETTING_KEYS: Final[frozenset[str]] = frozenset(
    {SETTING_KEY_PLATFORM_MEDIA_LIMITS}
)


# ---------------------------------------------------------------------------
# 分批类型：TRIAL / SCALE / BULK（ADR-13 的三档）
# ---------------------------------------------------------------------------

BATCH_KIND_TRIAL: Final[str] = "TRIAL"
BATCH_KIND_SCALE: Final[str] = "SCALE"
BATCH_KIND_BULK: Final[str] = "BULK"

BATCH_KINDS: Final[frozenset[str]] = frozenset(
    {BATCH_KIND_TRIAL, BATCH_KIND_SCALE, BATCH_KIND_BULK}
)

# 这里**没有** `BATCH_KIND_LIST_SQL`（其余 `*_LIST_SQL` 都有，唯独它没有）。
# 因为 batch 档位在库里**没有列**：`batch.limits` 是 `runtime_settings.value`
# 这个 JSONB 里的一个键，没有 CHECK 可以挂在它上面。同族的
# `DRAFT_STATE_LIST_SQL` / `SETTING_KEY_LIST_SQL` / `SETTING_SCOPE_LIST_SQL` /
# `SETTING_ORIGIN_LIST_SQL` / `ITEM_STATE_LIST_SQL` 都有 `models.py` 的
# CheckConstraint 消费 → 它们必须有。凭空造一个没有消费者的 SQL 片段常量
# 只会让下一个人以为"库里有个 batch_kind 列"。

#: 每档的**默认上限**（ADR-13 决策表的 `TRIAL=10 / SCALE≤30 / BULK≤50`）。
#:
#: **这是迁移写入 `GLOBAL` 行的初始值**，不是 Python 侧的兜底默认 ——
#: 读取路径一律走数据库（见 `settings_service.get_effective_limits`）。
#: 把它留在这里是为了让"迁移里写的值"与"测试断言的值"同源，
#: 避免两处各写一遍 10/30/50 而某一处改了另一处不改（**不报错**）。
DEFAULT_BATCH_MAX: Final[dict[str, int]] = {
    BATCH_KIND_TRIAL: 10,
    BATCH_KIND_SCALE: 30,
    BATCH_KIND_BULK: 50,
}

#: 数量的**下界**。ADR-13 决策表：DDL 只保留下界（`quantity > 0`），
#: 上界一律由 `runtime_settings` 提供。
MIN_QUANTITY: Final[int] = 1

#: `research.maxEffectiveFiles` 的默认值（研发执行计划 §6.1：默认 2000/项目）。
DEFAULT_RESEARCH_MAX_EFFECTIVE_FILES: Final[int] = 2000

#: 数值配置的通用上界。**存在的理由与比例上界不同**：比例越界是逻辑错误，
#: 而这个上界防的是"某次变更把一个 int 写成了 10 的 12 次方"，
#: 那会让每次分配算出一个天文数字的篇数（而下游会拿它乘单价）。
#: 10 万足够任何真实批次，且明显超出时是数据录入错误的强信号。
MAX_QUANTITY_BOUND: Final[int] = 100_000


# ---------------------------------------------------------------------------
# 配置的作用域（scope）与来源（origin）
# ---------------------------------------------------------------------------

#: `GLOBAL` = 平台级真值；`PROJECT` = 项目级行（副本或覆盖）。
SETTING_SCOPE_GLOBAL: Final[str] = "GLOBAL"
SETTING_SCOPE_PROJECT: Final[str] = "PROJECT"

SETTING_SCOPES: Final[frozenset[str]] = frozenset(
    {SETTING_SCOPE_GLOBAL, SETTING_SCOPE_PROJECT}
)

SETTING_SCOPE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(SETTING_SCOPES)
)

#: 行的**来源**。这是 OP-60（老大拍板"支持且必须有项目级副本"）的落点：
#: 副本与覆盖不是两张表，而是同一张表的两种 `origin`。
#:
#: | origin | 语义 | 是否跟随全局变更 |
#: |---|---|---|
#: | `GLOBAL` | 平台级真值 | — |
#: | `INHERITED` | 项目级**副本**（全局值的快照） | ✅ 是 |
#: | `MANUAL` | 项目级**覆盖**（运营显式改过） | ❌ 否 |
#:
#: **为什么用一列而不是两张表**："是否跟随"是**一行的属性**，不是**一类实体的属性**；
#: 同一项目从"跟随"变成"覆盖"时只是插入一行 `MANUAL` 的新版本，
#: 追加式表天然承载这次语义迁移，**不需要搬家**。
SETTING_ORIGIN_GLOBAL: Final[str] = "GLOBAL"
SETTING_ORIGIN_INHERITED: Final[str] = "INHERITED"
SETTING_ORIGIN_MANUAL: Final[str] = "MANUAL"

SETTING_ORIGINS: Final[frozenset[str]] = frozenset(
    {SETTING_ORIGIN_GLOBAL, SETTING_ORIGIN_INHERITED, SETTING_ORIGIN_MANUAL}
)

SETTING_ORIGIN_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(SETTING_ORIGINS)
)

# ---------------------------------------------------------------------------
# 草稿状态
# ---------------------------------------------------------------------------

DRAFT_STATE_EDITING: Final[str] = "EDITING"
DRAFT_STATE_SUBMITTED: Final[str] = "SUBMITTED"

DRAFT_STATES: Final[frozenset[str]] = frozenset(
    {DRAFT_STATE_EDITING, DRAFT_STATE_SUBMITTED}
)

DRAFT_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(DRAFT_STATES)
)


# ---------------------------------------------------------------------------
# 业务错误原因码
# ---------------------------------------------------------------------------
# **为什么要有独立的原因码**：错误响应里的 `message` 是给人读的文案，
# 会变；而 `details.reason` 是机器可判的。测试与前端要断言的是后者。
# 与 WP22/WP23 的 `REASON_*` 同一手法。
REASON_QUANTITY_BELOW_MIN: Final[str] = "QUANTITY_BELOW_MIN"
REASON_QUANTITY_ABOVE_MAX: Final[str] = "QUANTITY_ABOVE_MAX"
REASON_BATCH_KIND_UNKNOWN: Final[str] = "BATCH_KIND_UNKNOWN"
REASON_SETTING_KEY_UNKNOWN: Final[str] = "SETTING_KEY_UNKNOWN"
REASON_SETTING_VALUE_INVALID: Final[str] = "SETTING_VALUE_INVALID"
REASON_PERMILLE_NOT_TOTAL: Final[str] = "PERMILLE_NOT_TOTAL"
REASON_DUPLICATE_ITEM_KEY: Final[str] = "DUPLICATE_ITEM_KEY"
# 这里**没有** `REASON_ALLOCATION_OVER_CAPACITY`。它在阶段 7 被删除，理由不是"没人用"，
# 而是**它与 C14 的设计相反**：跑满容量时剩余篇数计入 `unallocated`，那是
# **合法结果**（`allocation.py` 模块 docstring 第四节），从来不存在"因容量超限而失败"
# 这个场景。留一个永不触发的错误码，会让下一个人误以为"容量超限应当报错"，
# 进而去补一条会把合法结果变成 500 的校验。
REASON_DRAFT_NOT_FOUND: Final[str] = "DRAFT_NOT_FOUND"
REASON_PROJECT_NOT_FOUND: Final[str] = "PROJECT_NOT_FOUND"
REASON_CHANGE_REASON_REQUIRED: Final[str] = "CHANGE_REASON_REQUIRED"
REASON_ACTOR_NOT_FOUND: Final[str] = "ACTOR_NOT_FOUND"
REASON_SETTING_NOT_CONFIGURED: Final[str] = "SETTING_NOT_CONFIGURED"
REASON_BASELINE_MISMATCH: Final[str] = "BASELINE_PROJECT_MISMATCH"
REASON_DRAFT_ALREADY_SUBMITTED: Final[str] = "DRAFT_ALREADY_SUBMITTED"
REASON_DIMENSION_LIMIT_EXCEEDED: Final[str] = "DIMENSION_LIMIT_EXCEEDED"
REASON_TOTAL_QUANTITY_INVALID: Final[str] = "TOTAL_QUANTITY_INVALID"


# ---------------------------------------------------------------------------
# 长度上界
# ---------------------------------------------------------------------------
# 变更理由的长度上界。**必须显式写出来**：理由经 `POST /settings/{key}` 进入
# 审计行，允许任意长度等于让一个 1MB 的字符串撑进审计表（ADR-13 要求审计行
# 是"与费用账本同级"的长期数据）。
MAX_CHANGE_REASON_CHARS: Final[int] = 500

# 场景名 / 形式名的长度上界（来自步骤 3 的场景名，原型里最长的约 12 个汉字）。
MAX_ITEM_NAME_CHARS: Final[int] = 120

# 维度内最多允许的条目数。**上界存在的理由**：分配是穷举验证过的，
# 而穷举的规模随条目数增长；同时"7 个场景"是原型实测的量级，
# 给一个宽松但有界的天花板能拦住"误把整个文件内容当条目导入"。
MAX_DIMENSION_ITEMS: Final[int] = 64

# 停用原因的长度上界（与 `MAX_CHANGE_REASON_CHARS` 同因：进 JSONB、
# 前端要展示、且不该被一个巨大字符串撑爆草稿行）。
MAX_DISABLED_REASON_CHARS: Final[int] = 200


# ---------------------------------------------------------------------------
# 维度条目的启用状态
# ---------------------------------------------------------------------------
# **为什么要有常量源**（本条为实现一致性收口）：
# `DRAFT_STATES` / `SETTING_ORIGINS` 都有常量 + `*_LIST_SQL` 供 DDL 与错误
# 消息共用，只有 `state` 是散落在服务层与 `_to_allocation_items` 里的**字面量**。
# 散落的直接风险：`_to_allocation_items` 判 `== "DISABLED"`、校验判
# `in ("ENABLED","DISABLED")` ——两处各写一遍，改一处就静默分叉
# （漏改的那处会把停用项当成启用项去分内容，且不报错）。

ITEM_STATE_ENABLED: Final[str] = "ENABLED"
ITEM_STATE_DISABLED: Final[str] = "DISABLED"

ITEM_STATES: Final[frozenset[str]] = frozenset(
    {ITEM_STATE_ENABLED, ITEM_STATE_DISABLED}
)

ITEM_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(ITEM_STATES)
)


# ---------------------------------------------------------------------------
# 值类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatchLimits:
    """三档批次的数量上限（`runtime_settings['batch.limits']` 的解析结果）。

    **为什么是三个具名字段而不是 `dict[str, int]`**：
    `limits.max_for(batch_kind)` 的入参可能来自请求；若内部是 dict，
    一次拼写错误（`'TRIAL '` 带空格）会得到 `KeyError` 或静默的
    `.get(kind, 0)` —— 后者会让"上限 0"变成"什么都提交不了"。
    具名字段让取值域在类型层就闭合，而 `max_for` 仍接受字符串键
    （它先做 `BATCH_KINDS` membership 校验，非法键抛业务错误）。
    """

    trial: int
    scale: int
    bulk: int

    def max_for(self, batch_kind: str) -> int:
        """取某一档的上限。非法档位抛 `ValueError`（调用方应先校验）。"""
        if batch_kind == BATCH_KIND_TRIAL:
            return self.trial
        if batch_kind == BATCH_KIND_SCALE:
            return self.scale
        if batch_kind == BATCH_KIND_BULK:
            return self.bulk
        raise ValueError(f"未知的批次档位：{batch_kind!r}")

    def as_dict(self) -> dict[str, int]:
        """回写 `runtime_settings.value` 用的形状（与 `DEFAULT_BATCH_MAX` 同形）。"""
        return {
            BATCH_KIND_TRIAL: self.trial,
            BATCH_KIND_SCALE: self.scale,
            BATCH_KIND_BULK: self.bulk,
        }


@dataclass(frozen=True, slots=True)
class EffectiveSetting:
    """**生效值**及其来源 —— OP-60 的"分叉必须显式暴露"落点（§3.6.4）。

    | 字段 | 含义 |
    |---|---|
    | `configured` | 是否存在**任何**生效值（全局与项目都没有时为 `False`） |
    | `value` | 本项目**实际生效**值（`configured=False` 时为 `None`） |
    | `source` | `'GLOBAL'` / `'INHERITED'` / `'MANUAL'`（生效值从哪来） |
    | `global_value` | 当前全局值 —— **始终返回**，不因被覆盖而为 `None` |
    | `diverged` | `value != global_value` |

    **为什么四条必须一起返回**（这是本包最要紧的一处设计）：
    只给 `value` 就是"悄悄分叉"（助手在提问时明确指出的风险形态）；
    给了 `diverged` 才有**可断言的对象**；给了 `global_value` 才能让人
    **看见差在哪**（前端据此渲染「本项目已覆盖 · 当前 5 条 · 全局 10 条」）。
    缺任何一条，这条防线就只能靠人记得去看。

    ## `configured` 为什么是必需字段，而不是"用 `value is None` 代替"

    `runtime_settings` 里有**占位键**（`PLACEHOLDER_SETTING_KEYS`，
    典型是 `platform.mediaLimits`）：机制已建、实测值未到（OP-64）。
    它的合法状态就是"**没有值**"。若用 `value is None` 表达这件事，
    那么 `{"maxEffectiveFiles": null}`（一个**有值但值为空**的形状）
    与"没有值"就无法区分了 —— 而前者应当被校验层拒绝、后者应当被放行。

    因此 `configured` 是**独立的一维**，不以 `value` 的取值形态兼任。

    `revision_no` 是**生效行的**版本号（不是全局行的）；无生效行时为 `None`。
    """

    key: str
    configured: bool
    value: dict[str, Any] | None
    source: str | None
    global_value: dict[str, Any] | None
    diverged: bool
    revision_no: int | None
    project_id: uuid.UUID | None

    def as_limits(self) -> BatchLimits:
        """按 `batch.limits` 的形状解析（仅在该 key 上合法）。

        **未配置时抛错而不是返回默认值**：返回默认值会让"读不到配置"
        表现为"用了 10/30/50"且不报错，而"我改的值为什么没生效"
        就无从查起（与 `limits.default_limits()` 的立场一致）。
        """
        if not self.configured or self.value is None:
            raise ValueError(f"{self.key} 尚未配置任何值，无法解析为批次数上限。")
        return parse_batch_limits(self.value)


@dataclass(frozen=True, slots=True)
class AllocationItem:
    """分配的一个条目 —— **纯数据，不含任何 ORM / session 引用**。

    | 字段 | 作用 |
    |---|---|
    | `key` | 稳定标识。**平局次序的第三级**（前两级是余数、`sort_order`） |
    | `weight_permille` | 权重（千分位整数）。可为 0（停用场景比例 0‰） |
    | `capacity` | 容量上限（`None` = 无上限）。C14 的"削峰"依据 |
    | `sort_order` | 显式次序。**平局次序的第二级** |
    | `name` | 仅供错误消息与证据日志可读，**不参与任何比较** |

    **`name` 为什么不参与比较**：它是可改的展示字段。若分配次序依赖它，
    运营改一个场景名就会让"同一份计划算出不同数字"（C19 的可复现性要求）。
    """

    key: str
    weight_permille: int
    capacity: int | None = None
    sort_order: int = 0
    name: str = ""


@dataclass(frozen=True, slots=True)
class AllocationLine:
    """分配结果的一行（带**可解释性**字段）。

    `floor_part` / `remainder` / `tie_rank` 不是必需的输出，但它们是
    原型「最大余数法换算过程」表的直接数据源，也是证据日志里
    "为什么这一类多一篇"的唯一解释。**没有它们，争议只能靠复算**。
    """

    key: str
    allocated: int
    weight_permille: int
    capacity: int | None
    sort_order: int
    floor_part: int
    remainder: int
    tie_rank: int
    capacity_shaved: bool = False


@dataclass(frozen=True, slots=True)
class AllocationResult:
    """一次分配的结果。

    ## ⚠️ 一条等式，两种命运（必读，否则会误以为这里已被守住）

        sum(line.allocated for line in lines) + unallocated == total

    **这条等式不能用来自证正确性**：`allocated_total` 是下面这个
    `@property`（就是同一条求和），而 `unallocated` 在生产侧的定义
    （`allocation.py`）正是 `total - sum(同一个 dict)` ——
    两者相加在**代数上恒等**，与输入无关，**永远为真**。
    实测：400 组离谱输入下一次都没破（含权重合计 8000‰、容量乱填）。

    它**仍然有用**，但只有一种用法：当 `lines` 与 `unallocated` 来自
    **两个独立数据源**时（例如前端把两处分别算出来、或测试侧独立实现），
    它能抓"两边打架"。**同源自证 = 恒真式，必须改写成互证。**

    真正能 FAIL 的判据形式见 `allocation.py` 模块 docstring 第一节之二。
    """

    total: int
    lines: tuple[AllocationLine, ...]
    unallocated: int
    rounds: int = 1
    notes: tuple[str, ...] = field(default_factory=tuple)

    def allocated_by_key(self) -> dict[str, int]:
        return {line.key: line.allocated for line in self.lines}

    @property
    def allocated_total(self) -> int:
        return sum(line.allocated for line in self.lines)


# ---------------------------------------------------------------------------
# 解析 / 校验辅助（纯函数，供各层复用）
# ---------------------------------------------------------------------------


def parse_batch_limits(value: dict[str, Any]) -> BatchLimits:
    """把 `runtime_settings['batch.limits'].value` 解析成 `BatchLimits`。

    **为什么解析要在这里而不是在读取路径里手写**：这个 dict 也可能来自
    运维手工写的 SQL（ADR-13 已识别该代价）。解析函数只有一个，
    它同时被"读取数据库"与"测试构造"两条路径使用 ⇒ 两处口径必然一致。
    缺键时**抛错而不是取默认**：缺失说明写了一个半成品的值，
    静默补默认会让"少写了一档"表现为"那一档用 10 条"且不报错。
    """
    try:
        return BatchLimits(
            trial=int(value[BATCH_KIND_TRIAL]),
            scale=int(value[BATCH_KIND_SCALE]),
            bulk=int(value[BATCH_KIND_BULK]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"batch.limits 的形状不正确：{value!r}（{exc}）") from exc


__all__ = [
    "BATCH_KINDS",
    "BATCH_KIND_BULK",
    "BATCH_KIND_SCALE",
    "BATCH_KIND_TRIAL",
    "DEFAULT_BATCH_MAX",
    "DEFAULT_RESEARCH_MAX_EFFECTIVE_FILES",
    "DRAFT_STATES",
    "DRAFT_STATE_EDITING",
    "DRAFT_STATE_LIST_SQL",
    "DRAFT_STATE_SUBMITTED",
    "ITEM_STATES",
    "ITEM_STATE_DISABLED",
    "ITEM_STATE_ENABLED",
    "ITEM_STATE_LIST_SQL",
    "MAX_CHANGE_REASON_CHARS",
    "MAX_DIMENSION_ITEMS",
    "MAX_DISABLED_REASON_CHARS",
    "MAX_ITEM_NAME_CHARS",
    "MAX_PERMILLE",
    "MAX_QUANTITY_BOUND",
    "MIN_PERMILLE",
    "MIN_QUANTITY",
    "PERMILLE_TOTAL",
    "PLACEHOLDER_SETTING_KEYS",
    "REASON_BATCH_KIND_UNKNOWN",
    "REASON_BASELINE_MISMATCH",
    "REASON_CHANGE_REASON_REQUIRED",
    "REASON_DIMENSION_LIMIT_EXCEEDED",
    "REASON_DRAFT_ALREADY_SUBMITTED",
    "REASON_DRAFT_NOT_FOUND",
    "REASON_ACTOR_NOT_FOUND",
    "REASON_DUPLICATE_ITEM_KEY",
    "REASON_PERMILLE_NOT_TOTAL",
    "REASON_PROJECT_NOT_FOUND",
    "REASON_QUANTITY_ABOVE_MAX",
    "REASON_QUANTITY_BELOW_MIN",
    "REASON_TOTAL_QUANTITY_INVALID",
    "REASON_SETTING_KEY_UNKNOWN",
    "REASON_SETTING_NOT_CONFIGURED",
    "REASON_SETTING_VALUE_INVALID",
    "SETTING_KEY_BATCH_LIMITS",
    "SETTING_KEY_LIST_SQL",
    "SETTING_KEY_PLATFORM_MEDIA_LIMITS",
    "SETTING_KEY_RESEARCH_MAX_FILES",
    "SETTING_KEYS",
    "SETTING_ORIGINS",
    "SETTING_ORIGIN_GLOBAL",
    "SETTING_ORIGIN_INHERITED",
    "SETTING_ORIGIN_LIST_SQL",
    "SETTING_ORIGIN_MANUAL",
    "SETTING_SCOPES",
    "SETTING_SCOPE_GLOBAL",
    "SETTING_SCOPE_LIST_SQL",
    "SETTING_SCOPE_PROJECT",
    "AllocationItem",
    "AllocationLine",
    "AllocationResult",
    "BatchLimits",
    "EffectiveSetting",
    "parse_batch_limits",
]
