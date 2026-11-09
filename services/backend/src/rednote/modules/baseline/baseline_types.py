"""WP22 的不可变值类型与受控取值域。

**为什么单独一个模块**（与 WP11 / WP18 / WP20 / WP23 同一手法）：这些取值同时被
`models.py`（CHECK 约束）、`context_order.py`（顺序规则）、`readiness.py`
（完成条件）、`repository.py`（存储）、`service.py`（编排）与 `api.py` 使用。
定义在 service 里会让其余几个反向导入 service —— 本模块的分层刻意单向：

    baseline_types ← context_order
    baseline_types ← readiness
    baseline_types ← models
    baseline_types ← repository
    baseline_types ← service
    baseline_types ← api

**跨层传递的值用 frozen dataclass 而不是 ORM 对象**（ORM 实例绑在 session 上，
session 一关就 `DetachedInstanceError`）。

---

## 本包承载的两条不变量，取值层怎么落地

验收原文（原型 L33 转述 AC-08）：「运营确认后创建**不可原地修改**的基线快照」；
G2 出口：「**跨品牌事实不串**；**基线固定不被污染**」。

| 不变量 | 本模块的落点 |
|---|---|
| **快照而非指针** | `ItemDraft` 强制 `ref` 与 `snapshot` **同时**给（见该类的 docstring）—— 只有 `ref` 会被知识更新污染（R1），只有 `snapshot` 会断溯源（AC-24） |
| **不跨品牌** | `ControlledScope` 把「基线所属品牌/产品」与「知识的 scope 四值」之间的许可关系写成一张**显式表**，触发器与测试共用同一份判据 |
| **未纳入原因必须留痕** | `INCLUSION_EXCLUDED` + `ItemDraft.exclusion_reason`（AC-24 原文「未纳入原因」） |

## 为什么"审核项状态"与"冲突状态"是两套取值，不合并

原型 L78~101 的审核项状态是 `已确认 / 冲突 / 待确认`；
原型 L42 的指标卡是 `审核项 42 · 已处置 41 · 待处置 1`。
而**冲突**（L84 的"氨基酸表活占比 62% vs ≥60%"）是审核项的一个**子集**。

两者合并会造出一个**无法区分"没有冲突"与"冲突已处置"**的取值域 ——
而这两件事在下游的天差地别：前者可以创建基线，后者在有阻断项时不行。
因此 `CLAIM_STATES`（行状态）与 `CONFLICT_STATES`（处置进度）分开，
判据写成"**存在** state≠SETTLED 的**阻断**冲突"（对明细行的否定式，D7）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

# ---------------------------------------------------------------------------
# 基线条目的种类
# ---------------------------------------------------------------------------
# 一条基线钉住的东西不止"事实" —— 原型 L56~64 的 9 项快照清单里同时有
# 研究资料、知识版本、人群场景、事实清单、审核规则、已确认限制。
# 若不分类，"这条基线项是什么"就只能靠读 `snapshot` 的散文猜。
ITEM_KIND_FACT: Final[str] = "FACT"
ITEM_KIND_SCENE: Final[str] = "SCENE"
ITEM_KIND_RULE: Final[str] = "RULE"
ITEM_KIND_STYLE: Final[str] = "STYLE"
ITEM_KIND_RESEARCH: Final[str] = "RESEARCH"

ITEM_KINDS: Final[tuple[str, ...]] = (
    ITEM_KIND_FACT,
    ITEM_KIND_SCENE,
    ITEM_KIND_RULE,
    ITEM_KIND_STYLE,
    ITEM_KIND_RESEARCH,
)
ITEM_KIND_LIST_SQL: Final[str] = ", ".join(f"'{name}'" for name in sorted(ITEM_KINDS))

# ---------------------------------------------------------------------------
# 纳入 / 未纳入（AC-24 原文「实际选用条目、版本、片段与**未纳入原因**」）
# ---------------------------------------------------------------------------
# **排除项也必须落行**（这是本模块最容易被做漏的一处）：
# 若"没纳入"只是"没有那一行"，那么"为什么没纳入"就永远查不到 ——
# 而它正是半年后复盘时唯一想知道的事（说明文档 §1.2 的"容易忽略的用户"）。
INCLUSION_INCLUDED: Final[str] = "INCLUDED"
INCLUSION_EXCLUDED: Final[str] = "EXCLUDED"

INCLUSIONS: Final[tuple[str, ...]] = (INCLUSION_INCLUDED, INCLUSION_EXCLUDED)
INCLUSION_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(INCLUSIONS)
)

# ---------------------------------------------------------------------------
# 冲突类型（说明文档 §1.4 疏漏 #2：原型只给了一个样例，真实至少有四类）
# ---------------------------------------------------------------------------
CONFLICT_KIND_SOURCE: Final[str] = "SOURCE"
CONFLICT_KIND_TIMELINESS: Final[str] = "TIMELINESS"
CONFLICT_KIND_CROSS_BRAND: Final[str] = "CROSS_BRAND"
CONFLICT_KIND_FACT_VS_LIMIT: Final[str] = "FACT_VS_LIMIT"

CONFLICT_KINDS: Final[tuple[str, ...]] = (
    CONFLICT_KIND_SOURCE,
    CONFLICT_KIND_TIMELINESS,
    CONFLICT_KIND_CROSS_BRAND,
    CONFLICT_KIND_FACT_VS_LIMIT,
)
CONFLICT_KIND_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(CONFLICT_KINDS)
)

# ---------------------------------------------------------------------------
# 冲突处置动作（原型 L~"动作：采纳区间表述「≥60%」+ 记录依据"）
# ---------------------------------------------------------------------------
CONFLICT_ACTION_ADOPT_ONE: Final[str] = "ADOPT_ONE"
CONFLICT_ACTION_RANGE_STATEMENT: Final[str] = "RANGE_STATEMENT"
CONFLICT_ACTION_ESCALATE_BLOCK: Final[str] = "ESCALATE_BLOCK"
CONFLICT_ACTION_DISABLE_SCENE: Final[str] = "DISABLE_SCENE"

CONFLICT_ACTIONS: Final[tuple[str, ...]] = (
    CONFLICT_ACTION_ADOPT_ONE,
    CONFLICT_ACTION_RANGE_STATEMENT,
    CONFLICT_ACTION_ESCALATE_BLOCK,
    CONFLICT_ACTION_DISABLE_SCENE,
)
CONFLICT_ACTION_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(CONFLICT_ACTIONS)
)

# ---------------------------------------------------------------------------
# 冲突处置进度
# ---------------------------------------------------------------------------
# **两值而不是三值**：原型只有"已处置 / 待处置"。加一个 `DISMISSED`
# 会让"忽略它"与"处理它"在数据里长得一样 —— 而 AC-09 要求前者不能放行。
CONFLICT_STATE_OPEN: Final[str] = "OPEN"
CONFLICT_STATE_SETTLED: Final[str] = "SETTLED"

CONFLICT_STATES: Final[tuple[str, ...]] = (CONFLICT_STATE_OPEN, CONFLICT_STATE_SETTLED)
CONFLICT_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(CONFLICT_STATES)
)

# ---------------------------------------------------------------------------
# 审核项（事实清单一行）的状态 —— 原型 L78~101 的「已确认 · 冲突 · 待确认」
# ---------------------------------------------------------------------------
CLAIM_STATE_CONFIRMED: Final[str] = "CONFIRMED"
CLAIM_STATE_CONFLICT: Final[str] = "CONFLICT"
CLAIM_STATE_PENDING: Final[str] = "PENDING"

CLAIM_STATES: Final[tuple[str, ...]] = (
    CLAIM_STATE_CONFIRMED,
    CLAIM_STATE_CONFLICT,
    CLAIM_STATE_PENDING,
)
CLAIM_STATE_LIST_SQL: Final[str] = ", ".join(f"'{name}'" for name in sorted(CLAIM_STATES))

# ---------------------------------------------------------------------------
# 人群场景状态 —— 原型 L149~205 的「可生产 · 需标注限制 · 已停用」
# ---------------------------------------------------------------------------
SCENE_STATE_PRODUCIBLE: Final[str] = "PRODUCIBLE"
SCENE_STATE_NEEDS_LIMIT: Final[str] = "NEEDS_LIMIT"
SCENE_STATE_DISABLED: Final[str] = "DISABLED"

SCENE_STATES: Final[tuple[str, ...]] = (
    SCENE_STATE_PRODUCIBLE,
    SCENE_STATE_NEEDS_LIMIT,
    SCENE_STATE_DISABLED,
)
SCENE_STATE_LIST_SQL: Final[str] = ", ".join(f"'{name}'" for name in sorted(SCENE_STATES))

# ---------------------------------------------------------------------------
# 八步（PRD §7 的步骤 1~8）
# ---------------------------------------------------------------------------
# `step_approvals.step_no` 的取值域。**显式给上下界**而不是靠 int：
# 写 `9` 或 `0` 的批准单在库里看起来完全正常，而它对应不到任何一个步骤。
STEP_MIN: Final[int] = 1
STEP_MAX: Final[int] = 8
STEP_NUMBERS: Final[tuple[int, ...]] = tuple(range(STEP_MIN, STEP_MAX + 1))

# 步骤名字（错误消息与审计可读性用）。索引 0 = 步骤 1。
STEP_NAMES: Final[dict[int, str]] = {
    1: "Brief 解析",
    2: "研究补充",
    3: "信息审核与基线",
    4: "内容规划",
    5: "文案生成",
    6: "自检与审核",
    7: "试产",
    8: "批产",
}

# ---------------------------------------------------------------------------
# 批准对象的类型（D4 的关键列：只记"批了"无法回答"批的是哪一版"）
# ---------------------------------------------------------------------------
# 取值**不是** `version_nodes.entity_type` 的拷贝：这里表达的是
# "这一步的产物是什么"。两者现在恰好大部分重合，但语义不同 ——
# 步骤 2 的产物是研究补充结果（`RESEARCH`），而 `entity_type` 里没有它。
SUBJECT_KIND_BRIEF: Final[str] = "BRIEF"
SUBJECT_KIND_RESEARCH: Final[str] = "RESEARCH"
SUBJECT_KIND_BASELINE: Final[str] = "BASELINE"

SUBJECT_KINDS: Final[tuple[str, ...]] = (
    SUBJECT_KIND_BRIEF,
    SUBJECT_KIND_RESEARCH,
    SUBJECT_KIND_BASELINE,
)
SUBJECT_KIND_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(SUBJECT_KINDS)
)

# ---------------------------------------------------------------------------
# 批准结论
# ---------------------------------------------------------------------------
# **拒绝也留痕**：OP-151 原文说步骤 1 与步骤 2 "通过它完成"，
# 而"曾经被拒过"是审计信息（说明文档 §2.3 D4）。
APPROVAL_DECISION_APPROVED: Final[str] = "APPROVED"
APPROVAL_DECISION_REJECTED: Final[str] = "REJECTED"

APPROVAL_DECISIONS: Final[tuple[str, ...]] = (
    APPROVAL_DECISION_APPROVED,
    APPROVAL_DECISION_REJECTED,
)
APPROVAL_DECISION_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(APPROVAL_DECISIONS)
)

# ---------------------------------------------------------------------------
# 跨品牌判据（AC-24 的落点，**基线侧与知识侧的唯一权威声明**）
# ---------------------------------------------------------------------------
# 基线本身不存 scope —— 它总是项目级的（`production_baselines.project_id`）。
# 因此"允许引用哪些知识"由**知识自己的 scope**决定，条件是"它与本项目的
# 品牌/产品是否一致"。这张表就是那条规则：
#
# 它同时是 `ddl.py` 的触发器函数体、`service.py` 的预检、以及测试的
# 反向验证所依据的**同一份声明** —— 三处各写一遍 `if scope == 'BRAND'`
# 必然漂移，而漂移的方向是"其中一处放行了跨品牌引用"（静默）。
KNOWLEDGE_SCOPE_GLOBAL: Final[str] = "GLOBAL"
KNOWLEDGE_SCOPE_BRAND: Final[str] = "BRAND"
KNOWLEDGE_SCOPE_PRODUCT: Final[str] = "PRODUCT"
KNOWLEDGE_SCOPE_PROJECT: Final[str] = "PROJECT"

#: 知识 scope → 允许被引用的匹配条件。
#:  · `always`       —— 全局知识对任何项目都可用（**不是**放水：`FACT` 类别
#:    被 WP20 的 `ck_knowledge_assets_fact_requires_concrete_scope` 禁止为
#:    GLOBAL，所以放行 GLOBAL 不会让任何一条**事实**跨界）
#:  · `same_brand`   —— 项目的品牌必须与知识声明的品牌相同
#:  · `same_product` —— 项目的产品必须与知识声明的产品相同
#:  · `same_project` —— 必须是本项目（项目私有知识，G2 的"项目私有数据边界"）
SCOPE_MATCH_ALWAYS: Final[str] = "always"
SCOPE_MATCH_SAME_BRAND: Final[str] = "same_brand"
SCOPE_MATCH_SAME_PRODUCT: Final[str] = "same_product"
SCOPE_MATCH_SAME_PROJECT: Final[str] = "same_project"

KNOWLEDGE_SCOPE_MATCH_RULE: Final[dict[str, str]] = {
    KNOWLEDGE_SCOPE_GLOBAL: SCOPE_MATCH_ALWAYS,
    KNOWLEDGE_SCOPE_BRAND: SCOPE_MATCH_SAME_BRAND,
    KNOWLEDGE_SCOPE_PRODUCT: SCOPE_MATCH_SAME_PRODUCT,
    KNOWLEDGE_SCOPE_PROJECT: SCOPE_MATCH_SAME_PROJECT,
}

# ---------------------------------------------------------------------------
# 长度上界
# ---------------------------------------------------------------------------
# 显式写出而不是靠 text 的隐式无限：基线表与批准表长期保留，
# 一个 1MB 的排除原因会让列表接口的响应体失控。
MAX_TITLE_CHARS: Final[int] = 200
MAX_STATEMENT_CHARS: Final[int] = 2_000
MAX_SOURCE_LABEL_CHARS: Final[int] = 200
MAX_REASON_CHARS: Final[int] = 1_000
MAX_CHANGE_REASON_CHARS: Final[int] = 1_000
MAX_REF_LABEL_CHARS: Final[int] = 100
MAX_RULESET_LABEL_CHARS: Final[int] = 200

# `snapshot` / `conflicts` / `claims` 等 JSONB 的形状版本。与 WP11 的
# `schema_version`、WP17 的 `position_schema_version`、WP18 的
# `BRIEF_SCHEMA_VERSION`、WP20 的 `KNOWLEDGE_SCHEMA_VERSION` 同一手法。
BASELINE_SCHEMA_VERSION: Final[int] = 1

# 一个草稿里各类数组的上界。**不是"够不够用"的判断**，而是防止
# 某次导入把整本书塞进 `claims`（与 WP20 的 `MAX_BODY_CHARS` 同一取向）。
MAX_DRAFT_ITEMS: Final[int] = 2_000

# ---------------------------------------------------------------------------
# 错误码（契约的一部分，前端按它分支，不散写成字符串字面量）
# ---------------------------------------------------------------------------
REASON_PROJECT_NOT_FOUND: Final[str] = "baseline_project_not_found"
REASON_DRAFT_NOT_FOUND: Final[str] = "information_draft_not_found"
REASON_BASELINE_NOT_FOUND: Final[str] = "baseline_not_found"
#: `briefVersionId` 指向的版本节点不是 `BRIEF`（或根本不存在）。
#: 与 `REASON_BASELINE_NOT_FOUND` 分开：一个讲"这条基线没有"，一个讲
#: "你锚定 Brief 的那一版不是 Brief"。合成一个会让前端无法区分。
REASON_BRIEF_VERSION_NOT_FOUND: Final[str] = "brief_version_not_found"
REASON_STEP3_NOT_READY: Final[str] = "step3_not_ready"
REASON_CROSS_BRAND_REFERENCE: Final[str] = "cross_brand_reference"
REASON_APPROVAL_REQUIRED: Final[str] = "step_approval_required"
REASON_CLAIM_STATE_INVALID: Final[str] = "claim_state_invalid"
REASON_CONFLICT_KIND_INVALID: Final[str] = "conflict_kind_invalid"
REASON_CONFLICT_ACTION_INVALID: Final[str] = "conflict_action_invalid"
REASON_SCENE_STATE_INVALID: Final[str] = "scene_state_invalid"
REASON_EXCLUSION_REASON_REQUIRED: Final[str] = "exclusion_reason_required"
#: `changeReason` 为空/超长。**与 `REASON_EXCLUSION_REASON_REQUIRED` 分开**：
#: 前者说的是"你漏了为什么改这条基线"，后者说的是"你漏了为什么排除这个条目"
#: —— 前端/运营要改的字段完全不同。复用会让排查被引到 `selected[]` 上去
#: （与 `_require_decision` 不复用 `REASON_SUBJECT_KIND_INVALID` 同一条理由）。
REASON_CHANGE_REASON_REQUIRED: Final[str] = "baseline_change_reason_required"
#: 批准单的 `reason` 为空/超长。同上：它讲的是"为什么批"，与条目排除无关。
REASON_APPROVAL_REASON_REQUIRED: Final[str] = "step_approval_reason_required"
REASON_SNAPSHOT_REQUIRED: Final[str] = "baseline_item_snapshot_required"
REASON_STEP_NO_INVALID: Final[str] = "step_no_invalid"
REASON_SUBJECT_KIND_INVALID: Final[str] = "subject_kind_invalid"
REASON_DECISION_INVALID: Final[str] = "approval_decision_invalid"
REASON_SUBJECT_VERSION_NOT_FOUND: Final[str] = "subject_version_not_found"
REASON_ALREADY_APPROVED: Final[str] = "step_already_approved"
REASON_BASELINE_SUPERSEDED: Final[str] = "baseline_already_superseded"
REASON_SELECTION_EMPTY: Final[str] = "baseline_selection_empty"

# ---------------------------------------------------------------------------
# 值类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemDraft:
    """一条待写入的基线项（`production_baselines` 的**子行**）。

    **`ref` 与 `snapshot` 必须同时给** —— 这是决策 1 在类型层的落点：

    | 只存 `ref` | 只存 `snapshot` |
    |---|---|
    | 知识更新后**被污染**（读到新值），且**静默**（R1） | 无法回答"这条来自哪一版"（AC-24 断链） |

    因此两个字段都是必填（`ref_label` + `knowledge_version_id` 是 `ref`，
    `snapshot` + `snapshot_text` 是 `snapshot`）。这**不是**"多存一份"的冗余，
    而是两个不同问题的答案：`ref` 答"当时批准的是哪一版"，
    `snapshot` 答"当时批准的值是多少"。

    `snapshot` 是**结构化负载**（决策 3）而不是美化后的文本：
    若它是我们自己拼的一句话，它**本身就成了一个新的真值来源** ——
    半年后无法判断"这个值是从哪来的"。结构化的形态与 WP20 的
    `knowledge_versions.structured_payload` 同构：
    `{"value": "≥60%", "unit": "percent", "qualifier": "区间", "sourceSegmentId": "..."}`。

    **未纳入项也要落行**（`inclusion=EXCLUDED`）：AC-24 要求
    「保存实际选用条目、版本、片段与**未纳入原因**」，只存"纳入了什么"
    会让"为什么没纳入"永远查不到。
    """

    item_kind: str
    ref_label: str
    snapshot: dict[str, Any]
    inclusion: str = INCLUSION_INCLUDED
    knowledge_asset_id: uuid.UUID | None = None
    knowledge_version_id: uuid.UUID | None = None
    snapshot_text: str | None = None
    exclusion_reason: str | None = None
    #: 该条目在动态上下文里的相对顺序。`None` = 由 `context_order` 派生。
    context_order: int | None = None


@dataclass(frozen=True, slots=True)
class BaselineDraft:
    """创建一条基线的输入（`service.create_baseline` 的入参）。

    `brief_version_id` 钉住"哪一版 Brief"、`selected` 是本次纳入/未纳入的
    全部条目（**含排除项**）、`ruleset_version` 是审核规则集的版本标签
    —— 原型 L61 的「审核规则 v5（9 阻断 · 14 提醒）」在本包**只被快照，不被执行**
    （规则的判定归 WP30）。

    **为什么这里没有 `state`**：基线完全不可变（AC-08 + 决策 5），
    "当前生效的是哪一条"由 `projects.active_baseline_id` 回答。
    见 `models.py` 的模块 docstring 第三节。
    """

    project_id: uuid.UUID
    brief_version_id: uuid.UUID
    selected: tuple[ItemDraft, ...]
    ruleset_version: str
    change_reason: str
    #: 被本条基线替代的上一条基线（`production_baselines.id`，即版本节点 id）。
    supersedes_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ConflictDecision:
    """一次冲突处置（`information_drafts.conflicts` 里一行的写入形状）。

    冲突**不落独立表** —— 它是草稿内容的一部分（与 WP18 的 issues 同款判断）：
    处置过程发生在"审核工作区"里，而工作区本身是可变的；
    一旦进了基线，快照就是不可变的。

    `blocking=True` 时，**未处置（`state != SETTLED`）会让基线创建被拒**
    —— AC-09 的落点。这个判据不读任何 count 列（D7），
    而是在创建事务里对明细行做否定式查询（第 5 层防线）。
    """

    conflict_id: str
    kind: str
    blocking: bool
    state: str = CONFLICT_STATE_OPEN
    action: str | None = None
    reason: str | None = None
    resolution: str | None = None


@dataclass(frozen=True, slots=True)
class StepApprovalDraft:
    """一条步骤批准（`step_approvals` 的写入形状，**本包定义，闭合 OP-151**）。

    OP-151 原文说协议**没给形状**。本包必须现在定，理由：
    第 4 层防线的触发器要校验"基线在 COMMIT 时有对应批准" ——
    **没有形状就写不出校验**。

    **为什么批准的对象是「步骤 + 版本」而不是只有"步骤"**：
    Brief 出了 v2 之后，步骤 1 的"已批准"指的是 **v2 已批准**，
    而不是"步骤 1 曾经被批过"。只记步骤会让"这一步批的是哪一版"
    在数据里**没有答案**，而八步闭环的验收（AC-08~09 证据 = 「固定版本」）
    恰恰要回答它。

    ⚠️ **与设计文档 §2.3 决策 4 表格的一处措辞分歧（已记录）**：
    该表列了 `superseded_by_id`（"被哪条新批准替代"），而同一格的备注
    写着「不可变表的'撤销'靠新增，不靠改写（决策 5 同款）」。
    两者不能同时成立 —— 在不可变行上写"我被谁替代了"需要 UPDATE 旧行。
    本实现取**备注与决策 5 的方向**：新批准记 `supersedes_id`，
    不建 `superseded_by_id`（与 `production_baselines` 完全同款）。
    收口时登记进 OP-151 的回填清单。
    """

    project_id: uuid.UUID
    step_no: int
    subject_kind: str
    subject_version_id: uuid.UUID
    decision: str
    reason: str
    decided_by: uuid.UUID | None = None
    supersedes_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ApprovalView:
    """一次批准写入的结果（跨层传递用，不传 ORM 对象）。"""

    approval_id: uuid.UUID
    project_id: uuid.UUID
    step_no: int
    subject_kind: str
    subject_version_id: uuid.UUID
    decision: str
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class DraftView:
    """审核工作区的一份快照视图（`GET .../information-draft` 的 `data`）。

    ⚠️ **`operator_confirmed` 与 `draft_id` 曾经缺失（本包自审发现）**：
    本类的说明写着"就是该端点的 `data`"，但少了这两个字段 →
    端点只能回去戳 ORM 实例的 `draft.id` / `draft.operator_confirmed`。
    那会破坏本类存在的全部理由（"转成不可变值对象后，响应组装不依赖
    session 生命周期"）—— 因为戳 ORM 就重新依赖了 session。

    两张表都有陷阱，各自都会**静默**出错：

    | 字段 | 缺失时的失效形态 |
    |---|---|
    | `draft_id` | 端点只能拿 `project_id` 当 id 用（本项目 `information_drafts.project_id` 唯一，两个 id **看起来**一样能用）→ 一旦将来允许多份草稿，ETag 就会串到别的草稿上 |
    | `operator_confirmed` | 端点的 `bool(draft.operator_confirmed)` 属于"缺省即 False"；漏了它就会把"已确认"报成"未确认"，而完成条件 ③ 的读数**只在 Create 时才被校验** → 前端看到"条件未满足"但后端创建成功 |
    """

    draft_id: uuid.UUID
    project_id: uuid.UUID
    brief_version_id: uuid.UUID | None
    claims: tuple[dict[str, Any], ...]
    conflicts: tuple[dict[str, Any], ...]
    scenes: tuple[dict[str, Any], ...]
    boundaries: dict[str, Any]
    research_selection: tuple[dict[str, Any], ...]
    operator_confirmed: bool
    lock_version: int
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class BaselineItemView:
    """基线里的一行（列表与详情共用）。"""

    item_no: int
    item_kind: str
    inclusion: str
    ref_label: str
    snapshot: dict[str, Any]
    snapshot_text: str | None
    exclusion_reason: str | None
    context_order: int
    knowledge_asset_id: uuid.UUID | None
    knowledge_version_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class BaselineSummary:
    """基线列表的一行。"""

    baseline_id: uuid.UUID
    project_id: uuid.UUID
    revision_no: int
    brief_version_id: uuid.UUID | None
    ruleset_version: str
    supersedes_id: uuid.UUID | None
    item_count: int
    excluded_count: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BaselineDetail:
    """基线详情（含全部条目与完成条件读数）。"""

    baseline_id: uuid.UUID
    project_id: uuid.UUID
    revision_no: int
    brief_version_id: uuid.UUID | None
    ruleset_version: str
    snapshot: dict[str, Any]
    supersedes_id: uuid.UUID | None
    created_at: datetime
    created_by: uuid.UUID | None
    items: tuple[BaselineItemView, ...]


@dataclass(frozen=True, slots=True)
class CreatedBaseline:
    """创建基线的结果。"""

    baseline_id: uuid.UUID
    revision_no: int
    item_count: int
    excluded_count: int
    approval_id: uuid.UUID
    superseded_baseline_id: uuid.UUID | None
    created: bool


@dataclass(frozen=True, slots=True)
class ContextItem:
    """动态上下文的一条候选（`context_order.order_context_items` 的输入）。

    `source` 是**受控取值**（`CONTEXT_PRIORITY_ORDER` 的键），
    `payload` 原样带回 —— 排序是纯函数，它不解释内容，只重排。
    """

    source: str
    item_id: str
    payload: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "APPROVAL_DECISIONS",
    "APPROVAL_DECISION_APPROVED",
    "APPROVAL_DECISION_LIST_SQL",
    "APPROVAL_DECISION_REJECTED",
    "BASELINE_SCHEMA_VERSION",
    "CLAIM_STATES",
    "CLAIM_STATE_CONFIRMED",
    "CLAIM_STATE_CONFLICT",
    "CLAIM_STATE_LIST_SQL",
    "CLAIM_STATE_PENDING",
    "CONFLICT_ACTIONS",
    "CONFLICT_ACTION_ADOPT_ONE",
    "CONFLICT_ACTION_DISABLE_SCENE",
    "CONFLICT_ACTION_ESCALATE_BLOCK",
    "CONFLICT_ACTION_LIST_SQL",
    "CONFLICT_ACTION_RANGE_STATEMENT",
    "CONFLICT_KINDS",
    "CONFLICT_KIND_CROSS_BRAND",
    "CONFLICT_KIND_FACT_VS_LIMIT",
    "CONFLICT_KIND_LIST_SQL",
    "CONFLICT_KIND_SOURCE",
    "CONFLICT_KIND_TIMELINESS",
    "CONFLICT_STATES",
    "CONFLICT_STATE_LIST_SQL",
    "CONFLICT_STATE_OPEN",
    "CONFLICT_STATE_SETTLED",
    "INCLUSIONS",
    "INCLUSION_EXCLUDED",
    "INCLUSION_INCLUDED",
    "INCLUSION_LIST_SQL",
    "ITEM_KINDS",
    "ITEM_KIND_FACT",
    "ITEM_KIND_LIST_SQL",
    "ITEM_KIND_RESEARCH",
    "ITEM_KIND_RULE",
    "ITEM_KIND_SCENE",
    "ITEM_KIND_STYLE",
    "KNOWLEDGE_SCOPE_BRAND",
    "KNOWLEDGE_SCOPE_GLOBAL",
    "KNOWLEDGE_SCOPE_MATCH_RULE",
    "KNOWLEDGE_SCOPE_PRODUCT",
    "KNOWLEDGE_SCOPE_PROJECT",
    "MAX_CHANGE_REASON_CHARS",
    "MAX_DRAFT_ITEMS",
    "MAX_REF_LABEL_CHARS",
    "MAX_REASON_CHARS",
    "MAX_RULESET_LABEL_CHARS",
    "MAX_SOURCE_LABEL_CHARS",
    "MAX_STATEMENT_CHARS",
    "MAX_TITLE_CHARS",
    "REASON_ALREADY_APPROVED",
    "REASON_APPROVAL_REASON_REQUIRED",
    "REASON_APPROVAL_REQUIRED",
    "REASON_BASELINE_NOT_FOUND",
    "REASON_BASELINE_SUPERSEDED",
    "REASON_BRIEF_VERSION_NOT_FOUND",
    "REASON_CHANGE_REASON_REQUIRED",
    "REASON_CLAIM_STATE_INVALID",
    "REASON_CONFLICT_ACTION_INVALID",
    "REASON_CONFLICT_KIND_INVALID",
    "REASON_CROSS_BRAND_REFERENCE",
    "REASON_DECISION_INVALID",
    "REASON_DRAFT_NOT_FOUND",
    "REASON_EXCLUSION_REASON_REQUIRED",
    "REASON_PROJECT_NOT_FOUND",
    "REASON_SCENE_STATE_INVALID",
    "REASON_SELECTION_EMPTY",
    "REASON_SNAPSHOT_REQUIRED",
    "REASON_STEP3_NOT_READY",
    "REASON_STEP_NO_INVALID",
    "REASON_SUBJECT_KIND_INVALID",
    "REASON_SUBJECT_VERSION_NOT_FOUND",
    "SCENE_STATES",
    "SCENE_STATE_DISABLED",
    "SCENE_STATE_LIST_SQL",
    "SCENE_STATE_NEEDS_LIMIT",
    "SCENE_STATE_PRODUCIBLE",
    "SCOPE_MATCH_ALWAYS",
    "SCOPE_MATCH_SAME_BRAND",
    "SCOPE_MATCH_SAME_PRODUCT",
    "SCOPE_MATCH_SAME_PROJECT",
    "STEP_MAX",
    "STEP_MIN",
    "STEP_NAMES",
    "STEP_NUMBERS",
    "SUBJECT_KINDS",
    "SUBJECT_KIND_BASELINE",
    "SUBJECT_KIND_BRIEF",
    "SUBJECT_KIND_LIST_SQL",
    "SUBJECT_KIND_RESEARCH",
    "ApprovalView",
    "BaselineDetail",
    "BaselineDraft",
    "BaselineItemView",
    "BaselineSummary",
    "ConflictDecision",
    "ContextItem",
    "CreatedBaseline",
    "DraftView",
    "ItemDraft",
    "StepApprovalDraft",
]
