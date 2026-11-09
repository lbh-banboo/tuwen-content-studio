"""步骤 3 的业务编排（WP22）—— **本层是唯一对外入口**。

端点只做参数解析与响应组装，全部判定在这里。判定所需的数据由
`repository` 在**同一事务内**读出 —— 若调用方先读一次、再传参进来，
两次读之间就有窗口，并发时能构造出"校验通过但写入时前提已变"。

---

## 一、创建基线是一个**四写一读**的事务

```
① register_version(BASELINE)   → version_nodes 一行（id = 基线 id）
② insert_baseline              → production_baselines 一行（id 同上）
③ insert_items                 → baseline_items 若干行（含未纳入项）
④ insert_approval              → step_approvals 一行（步骤 3 / BASELINE / APPROVED）
⑤ set_active_baseline          → projects.active_baseline_id 切到新基线
        ↑ 这五件事必须在同一个事务里，否则
          · 延迟约束触发器（第 4 层）会在 COMMIT 时拒绝「没有同事务批准」；
          · `active_baseline_id` 会指向一条尚未提交的基线。
```

**⑤ 不是可有可无的**：`projects.active_baseline_id` 是"当前生效的是哪条"
这个问题的**唯一**答案（`production_baselines` 没有 `state` 列，见 `models.py`）。
不写它，基线建出来了但下游读到的仍是旧值 —— 而这是**静默**的
（基线在列表里可见，只有"当前生效"这一处不对）。

## 二、两条防线，不是两处实现（这是本包最容易做错的地方）

| | 服务层（本文件） | 数据库（`ddl.py`） |
|---|---|---|
| 拦的是 | 正常请求路径 | 绕过本层的写入（裸 SQL / 数据回填 / 别的模块） |
| 给出 | 可读的 4xx，带 `field` 与 `reason` | `sqlstate=23514` → 422 |
| 判据来源 | `baseline_types` 的受控取值与 `KNOWLEDGE_SCOPE_MATCH_RULE` | 同一份声明编译出的 plpgsql |

**关键在于"判据来源同一"而不是"代码不重复"**：两处各写一遍
`if scope == 'BRAND'` 必然漂移，而漂移的方向是"其中一处放行了跨品牌引用"
（静默）。因此本文件的跨品牌预检**读的是 `KNOWLEDGE_SCOPE_MATCH_RULE`**，
与 `ddl.py::_scope_match_case()` 编译的是同一张表。

## 三、为什么 `readiness` 的预检与触发器的判定要分开写

第 5 层防线（`trg_production_baselines_no_blocking`）与第 7 层
（`readiness.step3_readiness()`）**不是同一个判据的两处实现，是两层**：

- 第 7 层拦正常路径，能给"哪一项阻断没处置"这样可操作的错误；
- 第 5 层拦绕过路径，只能给 `sqlstate=23514`。

设计文档 §2.1 明确把它们列为第 5 层与第 7 层。**"两处重复"的指控不成立
是因为两者拦的对象不同** —— 而"判据漂移"的风险由对账测试管住
（构造同一批冲突数据，断言两处同时为真/为假）。

## 四、与 `prompts/service.py` 的一处刻意不同

`PromptRepository` 用 `get_*_for_update()` 行锁串行化同模板的并发激活；
本包**不加行锁**：基线的唯一性靠
`uq_production_baselines_project_root`（部分唯一索引）+ 幂等命中处理，
而不是靠悲观锁。理由是本包的写入**不基于读到的旧值计算新值**
（`register_version` 自己取号重试），行锁只会把一个不会发生冲突的操作串行化。
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from rednote.modules.audit.models import ACTOR_KIND_USER
from rednote.modules.audit.repository import AuditRepository
from rednote.modules.audit.service import record_audit
from rednote.modules.baseline import readiness
from rednote.modules.baseline.baseline_types import (
    APPROVAL_DECISIONS,
    BASELINE_SCHEMA_VERSION,
    CLAIM_STATES,
    CONFLICT_ACTIONS,
    CONFLICT_KINDS,
    CONFLICT_STATE_SETTLED,
    CONFLICT_STATES,
    INCLUSION_EXCLUDED,
    INCLUSION_INCLUDED,
    INCLUSIONS,
    ITEM_KINDS,
    KNOWLEDGE_SCOPE_MATCH_RULE,
    MAX_CHANGE_REASON_CHARS,
    MAX_DRAFT_ITEMS,
    MAX_REF_LABEL_CHARS,
    MAX_REASON_CHARS,
    MAX_RULESET_LABEL_CHARS,
    MAX_STATEMENT_CHARS,
    REASON_ALREADY_APPROVED,
    REASON_APPROVAL_REASON_REQUIRED,
    REASON_APPROVAL_REQUIRED,
    REASON_BASELINE_NOT_FOUND,
    REASON_BASELINE_SUPERSEDED,
    REASON_BRIEF_VERSION_NOT_FOUND,
    REASON_CHANGE_REASON_REQUIRED,
    REASON_CLAIM_STATE_INVALID,
    REASON_CONFLICT_ACTION_INVALID,
    REASON_CONFLICT_KIND_INVALID,
    REASON_CROSS_BRAND_REFERENCE,
    REASON_DECISION_INVALID,
    REASON_DRAFT_NOT_FOUND,
    REASON_EXCLUSION_REASON_REQUIRED,
    REASON_PROJECT_NOT_FOUND,
    REASON_SCENE_STATE_INVALID,
    REASON_SELECTION_EMPTY,
    REASON_SNAPSHOT_REQUIRED,
    REASON_STEP3_NOT_READY,
    REASON_STEP_NO_INVALID,
    REASON_SUBJECT_KIND_INVALID,
    REASON_SUBJECT_VERSION_NOT_FOUND,
    SCENE_STATES,
    SCOPE_MATCH_ALWAYS,
    SCOPE_MATCH_SAME_BRAND,
    SCOPE_MATCH_SAME_PRODUCT,
    SCOPE_MATCH_SAME_PROJECT,
    STEP_MAX,
    STEP_MIN,
    STEP_NAMES,
    STEP_NUMBERS,
    SUBJECT_KINDS,
    SUBJECT_KIND_BASELINE,
    SUBJECT_KIND_BRIEF,
    SUBJECT_KIND_RESEARCH,
    ApprovalView,
    BaselineDetail,
    BaselineDraft,
    BaselineItemView,
    BaselineSummary,
    CreatedBaseline,
    DraftView,
    ItemDraft,
    StepApprovalDraft,
)
from rednote.modules.baseline.context_order import (
    UnknownItemKindError,
    order_for_item_kind,
)
from rednote.modules.baseline.ddl import BASELINE_STEP_NO
from rednote.modules.baseline.models import BaselineItem, ProductionBaseline
from rednote.modules.baseline.repository import BaselineRepository
from rednote.modules.versions.models import ENTITY_TYPE_BASELINE, ENTITY_TYPE_BRIEF
from rednote.modules.versions.repository import VersionRepository
from rednote.modules.versions.service import link_dependency, register_version
from rednote.platform.concurrency import build_etag, version_conflict
from rednote.platform.errors import ApiError, ErrorCode

# `subject_kind` → 该类型的版本节点在注册表里的 `entity_type`。
#
# **`RESEARCH` 显式映射到 `None`，而不是"不写这一项"** —— 这两种写法
# 在运行时的行为完全相同（`.get()` 都返回 `None`），但在**可维护性**上
# 是两个世界：
#
#   不写 → "RESEARCH 呢？" 要靠读上面那段注释才知道是刻意的；
#         将来给 `SUBJECT_KINDS` 加第 4 个类型时漏加在这里，
#         症状是**该类型的交叉核对被静默跳过**（不报错）。
#   写 `None` → 本表与 `SUBJECT_KINDS` 的键集合**相等**，
#         于是可以有一条机械判据（测试断言 `set(本表) == set(SUBJECT_KINDS)`）
#         把"漏加"从约定变成会被拦住的事。
#
# 这正是本项目反复踩的形态（ISS-78/84/85/86）：**唯一的声称方式只有注释时，
# 没有任何东西与它对账**。
#
# 为什么 `RESEARCH` 没有对应的 `entity_type`：`version_nodes.entity_type`
# 的 8 类里没有 RESEARCH（`baseline_types.SUBJECT_KINDS` 的 docstring
# 已说明两者语义不同 —— 前者是"注册表里的类型标签"，后者是"这一步的产物是什么"）。
# "没有对应项"因此意味着**无法交叉核对**，而不是"核对失败"。
_SUBJECT_KIND_ENTITY_TYPE: Final[dict[str, str | None]] = {
    SUBJECT_KIND_BRIEF: "BRIEF",
    SUBJECT_KIND_BASELINE: ENTITY_TYPE_BASELINE,
    SUBJECT_KIND_RESEARCH: None,
}

# 审计动作名。与本项目既有口径一致（`"work_item.resolve"` 那种 `对象.动作`）。
_ACTION_DRAFT_UPDATE: Final[str] = "information_draft.update"
_ACTION_BASELINE_CREATE: Final[str] = "baseline.create"
_ACTION_BASELINE_SUPERSEDE: Final[str] = "baseline.supersede"
_ACTION_STEP_APPROVE: Final[str] = "step.approve"

_OBJECT_DRAFT: Final[str] = "information_draft"
_OBJECT_BASELINE: Final[str] = "baseline"
_OBJECT_APPROVAL: Final[str] = "step_approval"


# ---------------------------------------------------------------------------
# 错误构造（与 WP11/WP18/WP20/WP23 同一形状）
# ---------------------------------------------------------------------------


DEPENDENCY_PURPOSE_BASELINE_BRIEF: Final = "BASELINE_BRIEF"
DEPENDENCY_PURPOSE_BASELINE_KNOWLEDGE: Final = "BASELINE_KNOWLEDGE"


def _validation_error(
    field: str, message: str, *, reason: str, **extra: Any
) -> ApiError:
    """构造 422 校验错误。

    `details` 里带 `field` 与 `reason`：前端按 `reason` 分支（而不是匹配文案），
    文案会随本地化变化。
    """
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        message,
        details={"field": field, "reason": reason, **extra},
    )


def _not_found(message: str, *, reason: str) -> ApiError:
    return ApiError(
        ErrorCode.RESOURCE_NOT_FOUND,
        message,
        details={"reason": reason},
    )


def _conflict(message: str, *, reason: str, **extra: Any) -> ApiError:
    """构造 409 状态冲突。

    与 `brief/service.py::_conflict` 同款：`INVALID_TRANSITION` 表示
    "当前状态不允许这个动作"，与"输入格式不对"（422）分开 ——
    两者的修法完全不同（前者要改状态，后者要改输入）。
    """
    return ApiError(
        ErrorCode.INVALID_TRANSITION,
        message,
        details={"reason": reason, **extra},
    )


# ---------------------------------------------------------------------------
# 受控取值校验
# ---------------------------------------------------------------------------


def _require_project(repo: BaselineRepository, project_id: uuid.UUID) -> None:
    """项目必须存在。

    **为什么在服务层判一次**（数据库的 FK 也会拦）：FK 给出的是
    `sqlstate=23503`，用户看到的是"外键约束失败"而不知道是哪个字段。
    服务层这一层给出可读的 422 并指出 `projectId`。
    """
    if not repo.project_exists(project_id):
        raise _validation_error(
            "projectId",
            "项目不存在。",
            reason=REASON_PROJECT_NOT_FOUND,
        )


def _require_step_no(step_no: int) -> None:
    """步骤号必须在 1~8 内。

    **必须显式判**：写 `9` 或 `0` 的批准单在库里看起来完全正常
    （`step_no` 只是个 int 列），而它对应不到任何一个步骤 ——
    症状是"这一步永远没有批准"，且不报错。
    """
    if step_no not in STEP_NUMBERS:
        raise _validation_error(
            "stepNo",
            f"步骤号必须在 {STEP_MIN}~{STEP_MAX} 之间。"
            f"合法取值：{', '.join(str(n) for n in STEP_NUMBERS)}。",
            reason=REASON_STEP_NO_INVALID,
        )


def _require_subject_kind(subject_kind: str) -> None:
    if subject_kind not in SUBJECT_KINDS:
        raise _validation_error(
            "subjectKind",
            f"subjectKind 不在受控取值域内。合法取值：{', '.join(sorted(SUBJECT_KINDS))}",
            reason=REASON_SUBJECT_KIND_INVALID,
        )


def _require_decision(decision: str) -> None:
    """批准结论必须在受控取值域内（`APPROVED` / `REJECTED`）。

    ⚠️ **不能复用 `REASON_SUBJECT_KIND_INVALID`**：那会让前端拿到的
    `reason` 说"对象类型不对"，而实际错的是结论 —— 一个指向错误方向的
    错误码比没有错误码更糟（它会把排查引到 `subjectKind` 上去）。
    """
    if decision not in APPROVAL_DECISIONS:
        raise _validation_error(
            "decision",
            f"decision 不在受控取值域内。合法取值：{', '.join(sorted(APPROVAL_DECISIONS))}",
            reason=REASON_DECISION_INVALID,
        )


def _require_reason_text(reason: str | None, *, field: str, max_chars: int, reason_code: str) -> str:
    """`reason` 类字段的通用校验：非空、限长。

    与 `audit.service.normalize_reason` 同一条取向（那边限 512）：`reason`
    是给人看的"为什么"，不是内容存储。不限长会让它被当成
    "顺便把内容也塞进来"的通道 —— 而那是审计泄露正文最常见的入口。

    ⚠️ **`reason_code` 必须由调用方给，不能在这里写死**（收口修复）。
    此前这个函数对三处调用方（`changeReason` / `exclusionReason` /
    批准的 `reason`）发**同一个** `exclusion_reason_required`，与
    `_require_decision` 立下的规矩（"不复用 `REASON_SUBJECT_KIND_INVALID`：
    一个指向错误方向的错误码比没有错误码更糟"）正面对撞 ——
    前端拿 `exclusion_reason_required` 去分支，会把排查引到 `selected[]`
    上去，而真正出错的是基线的 `changeReason` 或批准单的 `reason`。
    参数化之后，三处各自报自己的码，而"非空 + 限长"这条逻辑仍然只有一处。
    """
    text = (reason or "").strip()
    if not text:
        raise _validation_error(
            field, f"{field} 不能为空。", reason=reason_code
        )
    if len(text) > max_chars:
        raise _validation_error(
            field,
            f"{field} 过长（上限 {max_chars} 字符）。",
            reason=reason_code,
            maxLength=max_chars,
        )
    return text


# ---------------------------------------------------------------------------
# 审核工作区（**本包唯一的可变表**）
# ---------------------------------------------------------------------------


def draft_etag(draft: Any) -> str:
    """草稿的**可变** ETag（`build_etag(draft_id, lock_version)`）。

    与 WP18 `brief/service.py::draft_etag` 完全同款：草稿阶段资源可变、
    ETag 每次保存递增；固化之后换成不可变 ETag。这个切换点是本包与 WP11
    的接缝，也是"确认版本闭环"的可观察证据。
    """
    return build_etag(draft.id, draft.lock_version)


def require_draft(repo: BaselineRepository, *, project_id: uuid.UUID):
    """取本项目的工作区；不存在即建（**幂等的懒创建**）。

    **为什么懒创建而不是要求上游先建**：与 WP18 `ensure_brief` 同一条理由 ——
    协议把"创建初始草稿"归给项目创建事务，而项目创建是 WP10 的代码，
    本包不能改它。因此做成"第一次被调用时建出来"，此后每次都是读。

    **代价（已登记收口事项）**：`GET` 变成会写库的请求。它幂等
    （`uq_information_drafts_project_id` 兜底），但违反了"GET 是安全方法"
    的 HTTP 约定。收口方向是让主 agent 在项目创建事务里补一次调用 ——
    那时本函数退化成纯读。
    """
    _require_project(repo, project_id)

    draft = repo.get_draft(project_id)
    if draft is not None:
        return draft

    # 本工作区的 `brief_version_id` 建的时候为空：它由第一次保存草稿时
    # 客户端显式给出（`update_draft` 的 `brief_version_id` 参数）。
    # **刻意不去自动抓"当前生效的 Brief 版本"** —— 那会让"这份审核基于哪一版
    # Brief"变成一个本包替用户做的决定，而它是 AC-08 基线快照的一部分。
    from sqlalchemy.exc import IntegrityError

    try:
        draft = repo.insert_draft(project_id=project_id, brief_version_id=None)
    except IntegrityError:
        # 并发：另一个请求刚建出来（`uq_information_drafts_project_id` 拦下）。
        # **重新读一次**而不是报错 —— 两个请求的意图完全相同。
        repo.session.rollback()
        existing = repo.get_draft(project_id)
        if existing is None:  # pragma: no cover - 唯一约束已保证有
            raise
        return existing
    return draft


def draft_view(draft: Any) -> DraftView:
    """把 ORM 草稿转成跨层值对象。

    **转成 frozen dataclass 而不是把 ORM 对象交给上层**：ORM 实例绑在
    session 上，session 一关就 `DetachedInstanceError` —— 而端点返回它
    发生在 session 关闭之后。转成不可变值对象后，响应组装不依赖
    session 生命周期（与 `brief/service.py::draft_view` 同款）。
    """
    return DraftView(
        draft_id=draft.id,
        project_id=draft.project_id,
        brief_version_id=draft.brief_version_id,
        claims=tuple(draft.claims or ()),
        conflicts=tuple(draft.conflicts or ()),
        scenes=tuple(draft.scenes or ()),
        boundaries=dict(draft.boundaries or {}),
        research_selection=tuple(draft.research_selection or ()),
        operator_confirmed=bool(draft.operator_confirmed),
        lock_version=draft.lock_version,
        updated_at=draft.updated_at,
    )


def _require_rows(value: Any, *, field: str, reason: str) -> list[Any]:
    """草稿里"一组明细行"的公共前导校验：必须是数组、条数不超上限。

    三处调用（`claims` / `conflicts` / `scenes`）此前**逐字各写一遍**
    这两条判断。为什么不只是"啰嗦"：三条消息里的 `field` 与 `reason`
    各写一遍就各有一次写错的机会，而写错的后果是**方向错误的提示**
    （报成另一个字段，或复用了别的 reason 码）—— 与 `_require_decision`
    特意不复用 `REASON_SUBJECT_KIND_INVALID` 是同一条理由。

    返回原列表，方便调用方直接 `for ... in _require_rows(...)`，
    不必再重复 `isinstance` 判断（重复判断正是漂移的入口）。
    """
    if not isinstance(value, list):
        raise _validation_error(field, f"{field} 必须是数组。", reason=reason)
    if len(value) > MAX_DRAFT_ITEMS:
        raise _validation_error(
            field,
            f"{field} 条数 {len(value)} 超过上限 {MAX_DRAFT_ITEMS}。",
            reason=reason,
            maxItems=MAX_DRAFT_ITEMS,
        )
    return value


def _require_claims(claims: Any) -> None:
    """事实清单的形状与受控取值。

    每行 `{claimId, statement, source, state}`。**不校验 `source` 的存在性**：
    "这条事实来自哪个片段"由 WP18 的 `brief_claims` 提供，本包的草稿是
    运营在步骤 3 里重新组织的视图 —— 要求它与 WP18 逐字对应会让
    "运营补一条新事实"变成不可能。
    """
    for index, claim in enumerate(
        _require_rows(claims, field="claims", reason=REASON_CLAIM_STATE_INVALID)
    ):
        where = f"claims[{index}]"
        if not isinstance(claim, dict):
            raise _validation_error(
                where, f"{where} 必须是对象。", reason=REASON_CLAIM_STATE_INVALID
            )
        if not str(readiness.pick_field(claim, readiness.CLAIM_ID_KEYS) or "").strip():
            raise _validation_error(
                where,
                f"{where} 缺少 claimId。",
                reason=REASON_CLAIM_STATE_INVALID,
            )
        statement = claim.get("statement")
        if statement is not None and len(str(statement)) > MAX_STATEMENT_CHARS:
            raise _validation_error(
                where,
                f"{where}.statement 过长（上限 {MAX_STATEMENT_CHARS} 字符）。",
                reason=REASON_CLAIM_STATE_INVALID,
                maxLength=MAX_STATEMENT_CHARS,
            )
        state = str(readiness.pick_field(claim, readiness.CLAIM_STATE_KEYS) or "")
        if state not in CLAIM_STATES:
            raise _validation_error(
                where,
                f"{where}.state 不在受控取值域内。"
                f"合法取值：{', '.join(sorted(CLAIM_STATES))}",
                reason=REASON_CLAIM_STATE_INVALID,
            )


def _require_conflicts(conflicts: Any) -> None:
    """冲突与其处置的形状与受控取值。

    **`state=SETTLED` 时 `action` 必填**：一条"已处置"却没有说怎么处置的冲突，
    在下游看来与"被无视"无法区分 —— 而 AC-09 的区别正是"处置了"与"忽略了"。
    这条判据在本层（给人看的 422），数据库那层管不住它
    （`conflicts` 是 JSONB，CHECK 写不了子查询 —— 说明文档 §0.3 实测 1）。
    """
    for index, conflict in enumerate(
        _require_rows(conflicts, field="conflicts", reason=REASON_CONFLICT_KIND_INVALID)
    ):
        where = f"conflicts[{index}]"
        if not isinstance(conflict, dict):
            raise _validation_error(
                where, f"{where} 必须是对象。", reason=REASON_CONFLICT_KIND_INVALID
            )
        if not str(readiness.pick_field(conflict, readiness.CONFLICT_ID_KEYS) or "").strip():
            raise _validation_error(
                where, f"{where} 缺少 conflictId。", reason=REASON_CONFLICT_KIND_INVALID
            )
        kind = str(conflict.get("kind") or "")
        if kind not in CONFLICT_KINDS:
            raise _validation_error(
                where,
                f"{where}.kind 不在受控取值域内。"
                f"合法取值：{', '.join(sorted(CONFLICT_KINDS))}",
                reason=REASON_CONFLICT_KIND_INVALID,
            )
        state = str(conflict.get("state") or "")
        if state not in CONFLICT_STATES:
            raise _validation_error(
                where,
                f"{where}.state 不在受控取值域内。"
                f"合法取值：{', '.join(sorted(CONFLICT_STATES))}",
                reason=REASON_CONFLICT_KIND_INVALID,
            )
        action = conflict.get("action")
        if action is not None and str(action) not in CONFLICT_ACTIONS:
            raise _validation_error(
                where,
                f"{where}.action 不在受控取值域内。"
                f"合法取值：{', '.join(sorted(CONFLICT_ACTIONS))}",
                reason=REASON_CONFLICT_ACTION_INVALID,
            )
        if state == CONFLICT_STATE_SETTLED and action is None:
            raise _validation_error(
                where,
                f"{where} 标记为已处置（SETTLED）但没有给出 action ——"
                "「处置了」与「被无视」在下游必须可区分。",
                reason=REASON_CONFLICT_ACTION_INVALID,
            )


def _require_scenes(scenes: Any) -> None:
    for index, scene in enumerate(
        _require_rows(scenes, field="scenes", reason=REASON_SCENE_STATE_INVALID)
    ):
        where = f"scenes[{index}]"
        if not isinstance(scene, dict):
            raise _validation_error(
                where, f"{where} 必须是对象。", reason=REASON_SCENE_STATE_INVALID
            )
        if not str(readiness.pick_field(scene, readiness.SCENE_ID_KEYS) or "").strip():
            raise _validation_error(
                where, f"{where} 缺少 sceneId。", reason=REASON_SCENE_STATE_INVALID
            )
        state = str(scene.get("state") or "")
        if state not in SCENE_STATES:
            raise _validation_error(
                where,
                f"{where}.state 不在受控取值域内。"
                f"合法取值：{', '.join(sorted(SCENE_STATES))}",
                reason=REASON_SCENE_STATE_INVALID,
            )


def _require_boundaries(boundaries: Any) -> None:
    """表达边界必须是对象（原型 L228~270 的四类）。"""
    if not isinstance(boundaries, dict):
        raise _validation_error(
            "boundaries", "boundaries 必须是对象。", reason=REASON_SCENE_STATE_INVALID
        )


def update_draft(
    repo: BaselineRepository,
    *,
    project_id: uuid.UUID,
    expected_lock_version: int,
    claims: Any = None,
    conflicts: Any = None,
    scenes: Any = None,
    boundaries: Any = None,
    research_selection: Any = None,
    operator_confirmed: bool | None = None,
    brief_version_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
):
    """保存审核工作区（部分更新），返回 `(draft, readiness)`。

    ## 部分更新语义：`None` = **本次不改这一块**

    与 WP18 `PUT /brief-draft` 同款。**不能把 `None` 当成"改成空"** ——
    "只想改 `scenes`"的请求会把没提到的四块全部清空，而那是运营
    几小时工作的**静默丢失**（HTTP 200，响应里那几块也是空的，
    看起来像"保存成功"）。

    ## `operator_confirmed` 用 `is not None` 判，不用真值判

    它是 `bool | None`，而 `False` 是**合法的显式值**（运营可以取消确认）。
    写 `if operator_confirmed:` 会让"取消确认"这个动作**静默无效**，
    而那意味着 `readiness` 的条件 ③ 永远为真 —— 能创建基线的判据被绕过。
    这一条在仓储层也写了一遍（`repository.update_draft` 的 docstring），
    因为两处都能独立地把这个 bug 引进来。

    ## 乐观锁：两层，缺一不可

    前置 `require_if_match`（端点在调本函数**之前**做）只保证
    "客户端拿的不是过期值" —— 它挡不住两个**同时通过**前置校验的请求。
    那正是"乐观锁"这个词的全部含义，因此 `WHERE lock_version = ?`
    条件 UPDATE 是必须的：返回 0 表示"读到之后有人改过"，本函数报 412
    并回带当前 ETag（协议 §1.1），前端可直接用它重试。

    ## 空请求不递增 `lock_version`

    与 WP18 同款：所有块都是 `None` 时一个字段都没改，
    递增版本号会让客户端的 ETag 无谓失效（它下一步提交会被 412 挡下，
    而它其实什么都没做）。

    ## 为什么返回 `readiness`

    端点要在保存的**同一次往返**里回带三条完成条件的读数（原型 L139~165
    的界面就是"保存后立刻看条件是否变绿"）。让它再发一次 GET 会引入
    "两次读之间状态已变"的窗口 —— 而前端会拿着过期的条件读数去决定
    按钮是否可点。**同一事务内算出来的读数才是与这次保存一致的读数**。
    """
    draft = require_draft(repo, project_id=project_id)

    # --- 形状与受控取值（给人看的 422） ---------------------------------
    if claims is not None:
        _require_claims(claims)
    if conflicts is not None:
        _require_conflicts(conflicts)
    if scenes is not None:
        _require_scenes(scenes)
    if boundaries is not None:
        _require_boundaries(boundaries)
    if research_selection is not None and not isinstance(research_selection, list):
        raise _validation_error(
            "researchSelection",
            "researchSelection 必须是数组。",
            reason=REASON_CLAIM_STATE_INVALID,
        )

    touches_something = any(
        block is not None
        for block in (
            claims,
            conflicts,
            scenes,
            boundaries,
            research_selection,
            operator_confirmed,
            brief_version_id,
        )
    )

    if not touches_something:
        repo.flush()
        return draft, step3_status_of_draft(draft)

    # ⚠️ **必须在 UPDATE 之前取 before 负载**（MAJOR-5）。
    # 条件 UPDATE 用 `synchronize_session=False` 绕过了 ORM 身份映射，
    # 而 `reload_draft()` 会不会就地刷新同一个身份的实例是实现细节 ——
    # 依赖它等于让审计的"改前值"变成碰运气。在改动发生前取，就没有这个问题。
    before_payload = _draft_audit_payload(draft)

    updated = repo.update_draft(
        draft_id=draft.id,
        expected_lock_version=expected_lock_version,
        brief_version_id=brief_version_id,
        claims=claims,
        conflicts=conflicts,
        scenes=scenes,
        boundaries=boundaries,
        research_selection=research_selection,
        operator_confirmed=operator_confirmed,
    )

    if updated == 0:
        # 读到之后有人改过。回带**当前**值 —— 前端据此重试或展示差异。
        current = repo.get_draft(project_id)
        current_version = 0 if current is None else current.lock_version
        raise version_conflict(
            resource_id=draft.id,
            current_version=current_version,
            reason="lock_version_mismatch",
            message="审核工作区已被更新，请重新读取最新版本后再提交。",
        )

    # 条件 UPDATE 绕过了 ORM 的身份映射，读回来的是**过期的实例**
    # （`synchronize_session=False`）。必须显式刷新，否则下面算出的
    # `readiness` 用的是保存**之前**的内容 —— 而它正是响应里回带的条件读数。
    refreshed = repo.reload_draft(draft.id)
    assert refreshed is not None  # 刚更新过，必然存在

    if actor_id is not None and request_id is not None:
        record_audit(
            AuditRepository(repo.session),
            action=_ACTION_DRAFT_UPDATE,
            object_type=_OBJECT_DRAFT,
            object_id=refreshed.id,
            request_id=request_id,
            actor_kind=ACTOR_KIND_USER,
            actor_id=actor_id,
            project_id=project_id,
            after=_draft_audit_payload(refreshed),
            before=before_payload,
            reason=None,
        )

    repo.flush()
    return refreshed, step3_status_of_draft(refreshed)


def _draft_audit_payload(draft: Any) -> dict[str, Any]:
    """草稿的审计负载（**只记形状不记正文**）。

    ⚠️ **这里刻意不把 `claims` / `scenes` 的内容放进审计**：
    审计是长期保留的凭据，把审核内容整体复制进去会让它变成
    第二个内容存储 —— 而"审计里能看到所有正文"正是
    `audit/service.py` 反复防的那件事（它连 `reason` 都限长 512）。

    记 `lock_version` 与各块的长度，足以回答"这次保存改了什么规模"；
    内容本身在 `information_drafts` 里，有它自己的 ETag。
    """
    return {
        "lockVersion": draft.lock_version,
        "briefVersionId": None
        if draft.brief_version_id is None
        else str(draft.brief_version_id),
        "operatorConfirmed": bool(draft.operator_confirmed),
        "claimCount": len(draft.claims or ()),
        "conflictCount": len(draft.conflicts or ()),
        "sceneCount": len(draft.scenes or ()),
        "researchCount": len(draft.research_selection or ()),
    }


def step3_status(repo: BaselineRepository, *, project_id: uuid.UUID):
    """三条完成条件的读数（API 预检端点用）。

    **它就是 `readiness.step3_readiness()` 的薄封装**，因此与
    `create_baseline` 的拒绝判据**是同一个函数**（D6 单一来源）。
    测试断言的是"两处调用同一个函数"，而不只是"结果相等" ——
    后者会被两份恰好写得一样的拷贝骗过。
    """
    draft = require_draft(repo, project_id=project_id)
    return step3_status_of_draft(draft)


def step3_status_of_draft(draft: Any):
    """从 ORM 草稿算完成条件。"""
    return readiness.step3_readiness(
        claims=tuple(draft.claims or ()),
        conflicts=tuple(draft.conflicts or ()),
        scenes=tuple(draft.scenes or ()),
        operator_confirmed=bool(draft.operator_confirmed),
    )


# ---------------------------------------------------------------------------
# 创建基线
# ---------------------------------------------------------------------------


def _baseline_payload(draft: BaselineDraft) -> dict[str, Any]:
    """构造交给 `register_version` 的 payload（它据此算 `payload_hash`）。

    **这是一份内容指纹，字段顺序与内容都必须稳定**：`payload_hash` 用于
    "同一修订号重复注册时内容是否一致"的幂等判定 —— 若同一个版本两次
    注册算出不同的 hash，幂等会失效并凭空多出一个版本。

    **单独成函数**是为了让"什么算这一版基线的内容"只有一处定义
    （与 `prompts/service.py::_version_payload` 同一手法）。

    ⚠️ **条目按给定的顺序序列化，不排序**：顺序是内容的一部分
    （`item_no` 就是从这个顺序派生的），排序会让"把两条目换个位置"
    这个真实变更被判为幂等命中。

    `selected` 里的条目**含未纳入项**：AC-24 要求「保存实际选用条目、版本、
    片段与**未纳入原因**」—— 只把纳入项放进指纹，等于"把一项从纳入改成排除"
    这个变更不改变指纹，于是幂等命中一个**内容已经不同**的旧版本。
    """
    return {
        "projectId": str(draft.project_id),
        "briefVersionId": (
            None if draft.brief_version_id is None else str(draft.brief_version_id)
        ),
        "rulesetVersion": draft.ruleset_version,
        "changeReason": draft.change_reason,
        "supersedesId": (
            None if draft.supersedes_id is None else str(draft.supersedes_id)
        ),
        "items": [
            {
                "itemKind": item.item_kind,
                "refLabel": item.ref_label,
                "snapshot": item.snapshot,
                "inclusion": item.inclusion,
                "knowledgeAssetId": (
                    None
                    if item.knowledge_asset_id is None
                    else str(item.knowledge_asset_id)
                ),
                "knowledgeVersionId": (
                    None
                    if item.knowledge_version_id is None
                    else str(item.knowledge_version_id)
                ),
                "exclusionReason": item.exclusion_reason,
            }
            for item in draft.selected
        ],
    }


def _require_item(item: ItemDraft, *, index: int) -> None:
    """单条基线项的形状校验。

    逐条对应的数据库约束（**两层都要**：这一层给可读错误，数据库那层
    管住绕过本层的写入）：

    | 本函数 | 数据库 |
    |---|---|
    | `item_kind ∈ ITEM_KINDS` | `ck_baseline_items_item_kind_valid` |
    | `inclusion ∈ INCLUSIONS` | `ck_baseline_items_inclusion_valid` |
    | `ref_label` 非空且限长 | `ck_baseline_items_ref_label_not_empty` + `String(100)` |
    | `snapshot` 是非空对象 | `snapshot_is_object` + `snapshot_not_empty` |
    | 排除项必须有原因 | `excluded_needs_reason` |
    | 纳入项不得带原因 | `included_has_no_reason` |
    | 有版本引用必须有资产引用 | `version_ref_needs_asset_ref` |

    **`snapshot` 非空这条是 R1 的入口**：空快照 = 只有引用没有值，
    那正是"基线存指针"的形状（知识更新后读到新值，且静默）。
    它的失效**当天完全正常**，只在知识更新后才显现 —— 因此必须在写入前拦住。
    """
    where = f"selected[{index}]"

    if item.item_kind not in ITEM_KINDS:
        raise _validation_error(
            f"{where}.itemKind",
            f"{where}.itemKind 不在受控取值域内。"
            f"合法取值：{', '.join(sorted(ITEM_KINDS))}",
            reason=REASON_SNAPSHOT_REQUIRED,
        )

    if item.inclusion not in INCLUSIONS:
        raise _validation_error(
            f"{where}.inclusion",
            f"{where}.inclusion 不在受控取值域内。"
            f"合法取值：{', '.join(sorted(INCLUSIONS))}",
            reason=REASON_SNAPSHOT_REQUIRED,
        )

    label = (item.ref_label or "").strip()
    if not label:
        raise _validation_error(
            f"{where}.refLabel",
            f"{where}.refLabel 不能为空（它是人读的引用标签，如「事实 v7」）。",
            reason=REASON_SNAPSHOT_REQUIRED,
        )
    if len(label) > MAX_REF_LABEL_CHARS:
        raise _validation_error(
            f"{where}.refLabel",
            f"{where}.refLabel 过长（上限 {MAX_REF_LABEL_CHARS} 字符）。",
            reason=REASON_SNAPSHOT_REQUIRED,
            maxLength=MAX_REF_LABEL_CHARS,
        )

    if not isinstance(item.snapshot, dict) or not item.snapshot:
        raise _validation_error(
            f"{where}.snapshot",
            f"{where}.snapshot 必须是非空对象 —— 只有引用没有值的条目就是"
            "「基线存指针」，知识更新后会静默读到新值（AC-08 要求快照）。",
            reason=REASON_SNAPSHOT_REQUIRED,
        )

    if item.inclusion == INCLUSION_EXCLUDED:
        if not (item.exclusion_reason or "").strip():
            raise _validation_error(
                f"{where}.exclusionReason",
                f"{where} 是未纳入项，必须给出 exclusionReason"
                "（AC-24：未纳入原因要留痕，否则半年后复盘时查不到）。",
                reason=REASON_EXCLUSION_REASON_REQUIRED,
            )
        if len(item.exclusion_reason or "") > MAX_REASON_CHARS:
            raise _validation_error(
                f"{where}.exclusionReason",
                f"{where}.exclusionReason 过长（上限 {MAX_REASON_CHARS} 字符）。",
                reason=REASON_EXCLUSION_REASON_REQUIRED,
                maxLength=MAX_REASON_CHARS,
            )
    elif item.inclusion == INCLUSION_INCLUDED and item.exclusion_reason:
        raise _validation_error(
            f"{where}.exclusionReason",
            f"{where} 是纳入项，不应带 exclusionReason"
            "（带了说明状态与原因不一致）。",
            reason=REASON_EXCLUSION_REASON_REQUIRED,
        )

    if item.knowledge_version_id is not None and item.knowledge_asset_id is None:
        raise _validation_error(
            f"{where}.knowledgeAssetId",
            f"{where} 引用了版本但没有引用资产 —— 版本指向谁无从判断。",
            reason=REASON_SNAPSHOT_REQUIRED,
        )


def _require_item_scope(
    repo: BaselineRepository,
    *,
    item: ItemDraft,
    index: int,
    project_brand: uuid.UUID | None,
    project_product: uuid.UUID | None,
    project_id: uuid.UUID,
) -> None:
    """第 3 层防线的服务层一侧（跨品牌 + 版本属于资产）。

    **判据读的是 `KNOWLEDGE_SCOPE_MATCH_RULE`** —— `ddl.py` 编译的
    plpgsql 分支与这里读的是**同一张声明表**。这是"两层不漂移"的全部机制：
    两处各写一遍 `if scope == 'BRAND'` 必然漂移，而漂移的方向是
    其中一处放行了跨品牌引用（静默）。

    ⚠️ **比较用"NULL 视为一个普通值"的语义**（`!=` 对 `None` 也要成立）：

        knowledge 的 brand 是 None（GLOBAL/未声明）而项目有品牌
        → 不匹配 → 拒绝

    这与 `ddl.py` 必须用 `IS DISTINCT FROM` 是同一条纪律的两个语言侧。
    若这里写成 `if k_brand and k_brand != proj_brand`，那个 `and` 就把
    "知识没有品牌"这条路径**静默放行**了 —— 而它正是跨品牌引用最常见的形态。
    """
    if item.knowledge_asset_id is None:
        return

    where = f"selected[{index}]"
    scope_row = repo.knowledge_scope(item.knowledge_asset_id)
    if scope_row is None:
        raise _validation_error(
            f"{where}.knowledgeAssetId",
            f"{where} 引用的知识资产不存在。",
            reason=REASON_CROSS_BRAND_REFERENCE,
        )
    scope, k_brand, k_product, k_project = scope_row

    rule = KNOWLEDGE_SCOPE_MATCH_RULE.get(str(scope))
    if rule is None:
        # `scope` 是受限取值（WP20 的 CHECK 保证），走到这里说明
        # `KNOWLEDGE_SCOPE_MATCH_RULE` 与 `KNOWLEDGE_SCOPES` 不一致 ——
        # 那是**代码缺陷**，不是用户输入问题。
        #
        # 这里**不伪装成 422**：`ErrorCode` 里没有 `INTERNAL_ERROR`
        # （那个码只出现在 500 的错误体里，见 `platform/errors.py` 的
        # `InternalErrorBody` 说明），因此只能选一个"最接近真实原因"的码。
        # 选 `DEPENDENCY_UNAVAILABLE`（503）而不是 `VALIDATION_FAILED`（422）：
        # 后者会把一次部署不一致报成"你传错了"，而调用方按 422 处理
        # 只会去改请求体 —— 改一辈子也不会好。
        raise ApiError(  # pragma: no cover - 受控取值域已穷尽
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            f"知识的 scope 取值 {scope!r} 没有对应的匹配规则（部署不一致）。",
            details={"reason": "scope_rule_missing", "scope": str(scope)},
        )

    matched = True
    if rule == SCOPE_MATCH_ALWAYS:
        matched = True
    elif rule == SCOPE_MATCH_SAME_BRAND:
        matched = k_brand == project_brand
    elif rule == SCOPE_MATCH_SAME_PRODUCT:
        matched = k_brand == project_brand and k_product == project_product
    elif rule == SCOPE_MATCH_SAME_PROJECT:
        matched = k_project == project_id

    if not matched:
        raise _validation_error(
            f"{where}.knowledgeAssetId",
            f"{where} 引用的知识属于另一个品牌/产品（scope={scope}）。"
            "AC-24：跨品牌事实不得迁移。",
            reason=REASON_CROSS_BRAND_REFERENCE,
            scope=str(scope),
            knowledgeAssetId=str(item.knowledge_asset_id),
        )

    # --- 第 3 层的另一半：版本必须属于该资产 -----------------------------
    if item.knowledge_version_id is not None:
        version_asset = repo.knowledge_version_asset(item.knowledge_version_id)
        if version_asset is None:
            raise _validation_error(
                f"{where}.knowledgeVersionId",
                f"{where} 引用的知识版本不存在。",
                reason=REASON_SNAPSHOT_REQUIRED,
                versionId=str(item.knowledge_version_id),
            )
        if version_asset != item.knowledge_asset_id:
            raise _validation_error(
                f"{where}.knowledgeVersionId",
                f"{where} 引用的版本不属于它声明的知识资产"
                "（AC-24：条目须保存实际选用的版本，挂错资产会让溯源链指错地方）。",
                reason=REASON_SNAPSHOT_REQUIRED,
                versionId=str(item.knowledge_version_id),
                versionAssetId=str(version_asset),
                claimedAssetId=str(item.knowledge_asset_id),
            )


def _item_rows(
    draft: BaselineDraft, *, project_id: uuid.UUID
) -> list[dict[str, Any]]:
    """把 `ItemDraft` 逐条变成 `baseline_items` 的列值。

    **`item_no` 由给定顺序派生**（1 起）—— 它是"入库时的编号"，
    稳定且唯一；`context_order` 是"上下文顺序"，可跨档混排。
    两者分开是因为它们回答两个不同的问题（`repository.list_items` 的说明）。

    **`context_order` 的派生点是 `order_for_item_kind()`**：
    调用方给了显式值就用它，否则派生。

    ⚠️ **`RESEARCH` 没有映射，因此 `ItemDraft.context_order=None` 的
    研究资料项会在这里失败** —— 这是刻意的：`context_order.py` 的
    `UNMAPPED_ITEM_KINDS` 明确把 `RESEARCH` 排除出 PRD §15.4 的五项，
    因此"研究资料排第几"这个问题**必须由调用方回答**（显式给 `contextOrder`）。
    在这里把它转成 422 而不是让 `UnknownItemKindError` 冒到 500，
    是因为触发它的输入（一条 RESEARCH 项）是**用户数据**，不是编程错误。
    """
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(draft.selected, start=1):
        if item.context_order is not None:
            order = item.context_order
        else:
            try:
                order = order_for_item_kind(item.item_kind)
            except UnknownItemKindError as exc:
                raise _validation_error(
                    f"selected[{index - 1}].contextOrder",
                    f"{exc} 该项必须显式给出 contextOrder。",
                    reason=REASON_SNAPSHOT_REQUIRED,
                    itemKind=item.item_kind,
                ) from None
        if order < 1:
            raise _validation_error(
                f"selected[{index - 1}].contextOrder",
                "contextOrder 必须 ≥ 1。",
                reason=REASON_SNAPSHOT_REQUIRED,
            )
        rows.append(
            {
                "baseline_id": None,  # 由调用方在拿到基线 id 后填（见下）
                "item_no": index,
                "item_kind": item.item_kind,
                "inclusion": item.inclusion,
                "ref_label": item.ref_label,
                "knowledge_asset_id": item.knowledge_asset_id,
                "knowledge_version_id": item.knowledge_version_id,
                "snapshot": item.snapshot,
                "snapshot_text": item.snapshot_text,
                "exclusion_reason": item.exclusion_reason,
                "context_order": order,
            }
        )
    return rows


def _require_readiness_for_creation(draft: Any):
    """创建前的三条完成条件预检（第 7 层）。

    **`unsatisfied()` 的结果整体回带**：前端要能标出"哪一条没满足"，
    而只回一句话会让它在四条不同的条件之间猜。这也是 `ConditionResult.reason`
    存在的意义 —— 服务层的拒绝文案与 API 的预检文案是**同一段文字**。
    """
    status = step3_status_of_draft(draft)
    if not status.ready:
        unsatisfied = status.unsatisfied()
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "步骤 3 的三条完成条件尚未全部满足，不能创建基线："
            + "；".join(condition.reason for condition in unsatisfied),
            details={
                "reason": REASON_STEP3_NOT_READY,
                "unsatisfied": [
                    {
                        "key": condition.key,
                        "reason": condition.reason,
                        "blockingOpen": list(condition.blocking_open),
                        "producibleScenes": list(condition.producible_scenes),
                    }
                    for condition in unsatisfied
                ],
            },
        )
    return status


def _require_brief_version_node(
    repo: BaselineRepository, brief_version_id: uuid.UUID
) -> None:
    """`briefVersionId` 必须指向一个 **`BRIEF`** 版本节点（MAJOR-1）。

    ## 为什么外键拦不住

    `production_baselines.brief_version_id` 的外键只要求"`version_nodes`
    里有这一行"，**不看那一行是什么类型**。于是把一个 `PLAN` 节点的 id
    传进来，基线照样建得出来、读得回、快照里的 `briefVersionId` 也有值；
    而"这条基线基于哪一版 Brief"从此**没有答案，且事后补不出来**
    （基线不可变）。

    ## 为什么必须排在 `register_version` 之前

    它占修订号。被拒的请求若已经占过号，错误消息会变成"修订号已存在"，
    指向一个错误的方向（本函数 docstring「为什么先预检再注册」）。

    ## `node_entity_type` 返回 `None` 的情形

    那是"这个 id 根本不是版本节点"—— 与"类型不对"是两件事，但**同一个
    字段的同一个错**（锚点无效），因此共用一条错误码，只在**措辞**上分开。
    """

    actual = repo.node_entity_type(brief_version_id)
    if actual == ENTITY_TYPE_BRIEF:
        return
    raise _validation_error(
        "briefVersionId",
        f"briefVersionId 必须指向一个 {ENTITY_TYPE_BRIEF} 版本节点，"
        f"而该节点{'不存在' if actual is None else f'是 {actual}'}。",
        reason=REASON_BRIEF_VERSION_NOT_FOUND,
        expectedEntityType=ENTITY_TYPE_BRIEF,
        actualEntityType=actual,
    )


#: 链根的修订号。链根至多一条（`uq_production_baselines_project_root`），
#: 因此它**恒为 1**，不是"取 max"。
_FIRST_BASELINE_REVISION: Final[int] = 1


def _target_baseline_revision(
    repo: BaselineRepository, *, supersedes_id: uuid.UUID | None
) -> int:
    """这条基线应当落在**哪个修订号**上（`create_baseline` 第 6 步）。

    ## 为什么必须显式给号（MAJOR-2 的根因）

    `register_version` 的幂等判据是「**同一修订号** + 同一内容摘要 →
    复用」。而 `revision_no=None` 会走"追加到末尾"分支（自动取 `max + 1`）——
    于是**每一次重投都占一个新号**，幂等命中分支永远走不到：

      · 第 1 次：号 = 1 → 新建基线；
      · 第 2 次（同 payload 重投）：号 = 2 → 节点建得出来 →
        `insert_baseline(supersedes_id=None)` 撞
        `uq_production_baselines_project_root` → 客户端拿到裸 23505。

    也就是说服务层把"是不是重投"的判定权交给了修订号，而自己又从来
    不给号 —— 判据不一致 → 网络超时后的重试必炸。

    ## 号在链上有唯一答案

      · 链根（`supersedes_id is None`）→ 第 1 版；
      · 取代 → **被取代那条**的修订号 + 1。

    于是"同一 payload 重投"必然落到同一个号上，被内容摘要识别为幂等命中
    （`created=False`）—— 这正是重投该有的行为。

    修订号取自注册表（`repo.revision_no`），不另立真值来源
    （决策 5：`production_baselines` 自己不存 `revision_no`）。
    """
    if supersedes_id is None:
        return _FIRST_BASELINE_REVISION

    current = repo.revision_no(supersedes_id)
    if current is None:
        # `_require_supersedes_target` 已保证该节点存在且可替代。
        # 走到这里只可能是"两次查询之间节点没了"—— 当前实现不会发生，
        # 因此按**程序缺陷**抛出，而不是猜一个号继续往下走。
        raise RuntimeError(
            "被替代的基线版本节点查不到修订号（预检与取号之间被改动）"
        )
    return current + 1


def _require_supersedes_target(
    repo: BaselineRepository,
    *,
    project_id: uuid.UUID,
    supersedes_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """校验"被替代的那条基线"是可替代的。

    两条判据：

      1. **必须属于本项目** —— 一个基线的版本节点 id 撞到别的项目的机会
         极小，但"我就是从别的项目抄来的 id"是真实存在的误用，
         而它会让两个项目的链交叉；
      2. **必须是链尾**（没有任何其它行 `supersedes` 它）——
         替代一条"已经被替代过"的基线意味着**链分叉**：两条基线同时声称
         自己取代了同一条旧基线，而 `projects.active_baseline_id`
         只能指向其中一条。症状是"当前生效的基线"与"链尾"不一致，
         且**不报错**。

    判据 2 用 `repo.newest_baseline_of()` 而不是"这一条自己有没有被替代过"
    （后者要写一次反向查询，两处实现会漂移）：链尾是唯一的，
    因此"目标 == 链尾"就等价于"目标还没有被替代"。
    """
    if supersedes_id is None:
        return None

    target = repo.get_baseline(supersedes_id)
    if target is None or target.project_id != project_id:
        raise _validation_error(
            "supersedesId",
            "被替代的基线不存在或不属于本项目。",
            reason=REASON_BASELINE_NOT_FOUND,
        )

    tail = repo.newest_baseline_of(project_id)
    if tail is None or tail.id != supersedes_id:
        raise _conflict(
            "被替代的基线已被其它基线替代（链尾已不是它），不能再次替代 ——"
            "否则基线链会分叉，而「当前生效的是哪条」只能指向其中一条。",
            reason=REASON_BASELINE_SUPERSEDED,
            supersedesId=str(supersedes_id),
            currentTailId=None if tail is None else str(tail.id),
        )
    return supersedes_id


def _require_existing_baseline_belongs(
    repo: BaselineRepository, *, baseline_id: uuid.UUID, project_id: uuid.UUID
) -> ProductionBaseline:
    """幂等命中时，核对"已存在的那一条"确实属于本项目。

    **为什么必须核对**：`register_version` 判定为幂等命中时，
    领域行**已存在且不可变**，本层不重写它。但调用方拿到的是一个
    `baselineId`，它会以为"这是我刚创建的基线" —— 若那一条其实属于
    别的项目（修订号在不同实体间理论上不该撞，但注册表的幂等键是
    `(entity_type, entity_id, revision_no)`，而 `entity_id` 是 `project_id`，
    因此同一项目的不同内容才可能撞），调用方后续用它做激活就会失败在
    一个离这里很远的地方。

    **报出来比静默返回一个"不属于你的 baselineId"好**（与
    `prompts/service.py::_require_existing_version_belongs` 同款）。
    """
    existing = repo.get_baseline(baseline_id)
    if existing is None or existing.project_id != project_id:
        raise _conflict(
            "该修订号已被其它项目的基线占用。",
            reason=REASON_BASELINE_SUPERSEDED,
            baselineId=str(baseline_id),
        )
    return existing


def _ensure_baseline_approval(
    repo: BaselineRepository,
    *,
    project_id: uuid.UUID,
    baseline_id: uuid.UUID,
    decided_by: uuid.UUID | None,
    reason: str,
    requester: str,
) -> Any:
    """保证「这条基线 + 步骤 3 + BASELINE + APPROVED」有一条**生效的**批准。

    第 4 层防线的触发器在 COMMIT 时要求它存在，因此它是创建基线事务的
    一个必要组成部分（OP-151：协议原文的「同事务生成 step approval」）。

    ## 三种情形

    判据是「**当前生效**的那条批准」（`find_effective_approval`，
    与触发器同口径），不是链根：

    | 现状（生效的那条） | 动作 |
    |---|---|
    | 一条都没有 | 插一条链根（`supersedes_id = NULL`） |
    | 结论是 `APPROVED` | **复用**（不新增行） |
    | 结论是 `REJECTED` | 插一条**取代它**的新批准（"先拒后批"） |

    ⚠️ **这里必须看链尾而不是链根**（MAJOR-6）：链根会永久停在第一次
    写入的那条上。若链根是 `REJECTED` 而链尾是 `APPROVED`（重批过），
    按链根判会**再插一条**取代链根 → 同一条被取代两次（**链分叉**），
    且"被批准过几次"这个数字虚假增长。

    第三种是"拒绝也留痕"的落点：撤销靠新增而不是改写
    （`step_approvals` 不可变），因此旧的那条被保留且不再构成依据
    （「未被取代」的收窄）。

    ## 为什么复用而不是"每次都插一条新的"

    基线创建可能被重投（网络超时后的重试）。每次插一条新批准会让
    "这条基线被批准过几次"这个历史数字虚假增长，而每一次"批准"都是一个
    人为动作 —— 重复记录等于伪造审计。
    `uq_step_approvals_root` 也会在第二次插入时拦下它（同一版只能有一条链根），
    但那时错误来自一个唯一约束，客户端拿不到"这其实是你自己的重试"这个解释。
    """
    effective = repo.find_effective_approval(
        project_id=project_id,
        step_no=BASELINE_STEP_NO,
        subject_kind=SUBJECT_KIND_BASELINE,
        subject_version_id=baseline_id,
    )

    if effective is not None and effective.decision == "APPROVED":
        return effective

    return repo.insert_approval(
        project_id=project_id,
        step_no=BASELINE_STEP_NO,
        subject_kind=SUBJECT_KIND_BASELINE,
        subject_version_id=baseline_id,
        decision="APPROVED",
        reason=reason,
        decided_by=decided_by,
        # 走到这里"生效的那条"只可能是 REJECTED（或一条都没有）——
        # 取代它，而不是再插一条链根（后者会撞唯一索引）。
        supersedes_id=None if effective is None else effective.id,
    )


def _link_baseline_dependencies(
    version_repo: VersionRepository,
    *,
    baseline_id: uuid.UUID,
    brief_version_id: uuid.UUID | None,
    knowledge_version_ids: list[uuid.UUID | None],
) -> None:
    """写 E6（BASELINE→BRIEF）/ E7（BASELINE→KNOWLEDGE）依赖边，供 WP34 回退影响图遍历。

    只在"本次新建基线"分支里调用，与基线注册同一事务；`link_dependency`
    出错（422）直接上抛让整个事务回滚，不吞。可空外键判空跳过。
    """
    if brief_version_id is not None:
        link_dependency(
            version_repo,
            parent_version_id=baseline_id,
            dependency_version_id=brief_version_id,
            purpose=DEPENDENCY_PURPOSE_BASELINE_BRIEF,
        )
    for knowledge_version_id in dict.fromkeys(
        v for v in knowledge_version_ids if v is not None
    ):
        link_dependency(
            version_repo,
            parent_version_id=baseline_id,
            dependency_version_id=knowledge_version_id,
            purpose=DEPENDENCY_PURPOSE_BASELINE_KNOWLEDGE,
        )


def create_baseline(
    repo: BaselineRepository,
    version_repo: VersionRepository,
    *,
    draft: BaselineDraft,
    approval_reason: str | None = None,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> CreatedBaseline:
    """创建一条不可变基线（**本包的主路径**）。

    步骤编号即 `ddl.py` 的第 1~7 步（模块 docstring §一、§二）：

    1. 项目存在（422 可读）
    2. 工作区存在（404）—— 没有工作区就没有"审核过什么"这个前提
    3. **第 7 层预检**：三条完成条件（与 API 预检同一个函数）
    4. 条目形状与受控取值 + 第 3 层预检（跨品牌、版本属于资产）
       —— 以及 `briefVersionId` **必须指向 `BRIEF` 版本节点**（MAJOR-1）
    5. `supersedesId` 可替代性（链尾）
    6. `register_version`（`entity_id = project_id`，**显式给修订号**）→ `node_id`
    7. 写基线行 / 条目行 / 同事务批准 / 切 `active_baseline_id` / 写审计

    ## `entity_id` 必须是 `project_id`（本包最容易写错的一处）

    `BASELINE` 的"逻辑实体"是**该项目的那条基线链**，不是"某一条基线" ——
    一条基线就是链上的一个修订。因此 `entity_id = project_id`，
    而 `register_version` 返回的 `node_id` 才是基线的 id。
    把它传成 `baseline_id`（自己先生成一个 uuid）会让
    `ddl.py::trg_production_baselines_node_consistency` 判据 3 直接拒绝，
    也会让"同一项目的第二条基线"拿到修订号 1 而不是 2。

    ## 为什么先预检再注册

    `register_version` 会**占用一个修订号**。若在它之后才发现输入非法，
    那个号已经被占（注册表不可回滚到"没发生过"，只能回滚整个事务）——
    而调用方拿到的错误消息会变成"修订号已存在"，指向一个错误的方向。
    因此全部预检都排在注册之前（与 `prompts/service.py::add_version`
    的校验顺序同一条理由）。

    ## `approval_reason`

    步骤 3 的批准理由。缺省时用 `change_reason` —— 为创建基线而生的
    批准与"为什么会有这条基线"是同一个答案；要求调用方写两遍会让
    两次的措辞漂移，而批准记录是要给人看的。
    """
    _require_project(repo, draft.project_id)

    info_draft = repo.get_draft(draft.project_id)
    if info_draft is None:
        raise _not_found(
            "本项目的审核工作区不存在，无法创建基线。",
            reason=REASON_DRAFT_NOT_FOUND,
        )

    # --- 第 3 步：三条完成条件（第 7 层防线） ---------------------------
    _require_readiness_for_creation(info_draft)

    # --- 第 4 步：入参与条目 --------------------------------------------
    #
    # ⚠️ `briefVersionId` 的类型核对**排在最前**（MAJOR-1）：它是"这条基线
    #    钉住哪一版 Brief"的答案，也是 AC-24 溯源链的锚点。外键只保证
    #    "节点存在"，于是把一个 PLAN 节点钉进来能一路建成 —— 而基线不可变，
    #    "基于哪一版 Brief"事后补不出来。详见 `_require_brief_version_node`。
    _require_brief_version_node(repo, draft.brief_version_id)

    if not draft.selected:
        raise _validation_error(
            "selected",
            "基线必须包含至少一个条目 —— 一条不含任何条目的基线"
            "在下游看来与「什么都没审核」无法区分。",
            reason=REASON_SELECTION_EMPTY,
        )
    if len(draft.selected) > MAX_DRAFT_ITEMS:
        raise _validation_error(
            "selected",
            f"条目数 {len(draft.selected)} 超过上限 {MAX_DRAFT_ITEMS}。",
            reason=REASON_SELECTION_EMPTY,
            maxItems=MAX_DRAFT_ITEMS,
        )

    ruleset_version = (draft.ruleset_version or "").strip()
    if not ruleset_version:
        raise _validation_error(
            "rulesetVersion",
            "rulesetVersion 不能为空（它是审核规则集的版本标签，只被快照不被执行）。",
            reason=REASON_SNAPSHOT_REQUIRED,
        )
    if len(ruleset_version) > MAX_RULESET_LABEL_CHARS:
        raise _validation_error(
            "rulesetVersion",
            f"rulesetVersion 过长（上限 {MAX_RULESET_LABEL_CHARS} 字符）。",
            reason=REASON_SNAPSHOT_REQUIRED,
            maxLength=MAX_RULESET_LABEL_CHARS,
        )

    change_reason = _require_reason_text(
        draft.change_reason,
        field="changeReason",
        max_chars=MAX_CHANGE_REASON_CHARS,
        reason_code=REASON_CHANGE_REASON_REQUIRED,
    )

    scope = repo.project_scope(draft.project_id)
    assert scope is not None  # `_require_project` 已保证
    project_brand, project_product = scope

    for index, item in enumerate(draft.selected):
        _require_item(item, index=index)
        _require_item_scope(
            repo,
            item=item,
            index=index,
            project_brand=project_brand,
            project_product=project_product,
            project_id=draft.project_id,
        )

    # --- 第 5 步：被替代的那条 ------------------------------------------
    supersedes_id = _require_supersedes_target(
        repo, project_id=draft.project_id, supersedes_id=draft.supersedes_id
    )

    # --- 第 6 步：注册版本节点 ------------------------------------------
    #
    # ⚠️ `entity_id = draft.project_id`（不是自己生成的 uuid），
    #    且 `payload` 里带 `supersedesId` —— 后者让"替代"这个动作本身
    #    进入内容指纹：两次替代若指纹相同，说明第二次什么都没改。
    #
    # ⚠️ **必须显式给修订号**（MAJOR-2）：`revision_no=None` 会走"追加到
    #    末尾"，于是重投必占新号 → 幂等命中分支永远走不到 → 随后撞链根
    #    唯一索引变成裸 500。号在链上有唯一答案，见 `_target_baseline_revision`。
    target_revision = _target_baseline_revision(
        repo, supersedes_id=supersedes_id
    )
    registered = register_version(
        version_repo,
        entity_type=ENTITY_TYPE_BASELINE,
        entity_id=draft.project_id,
        payload=_baseline_payload(draft),
        project_id=draft.project_id,
        created_by=actor_id,
        schema_version=BASELINE_SCHEMA_VERSION,
        revision_no=target_revision,
    )

    baseline_id = registered.node_id

    if registered.created:
        repo.insert_baseline(
            baseline_id=baseline_id,
            project_id=draft.project_id,
            brief_version_id=draft.brief_version_id,
            ruleset_version=ruleset_version,
            snapshot=_baseline_snapshot(
                draft=draft, info_draft=info_draft, change_reason=change_reason
            ),
            supersedes_id=supersedes_id,
            change_reason=change_reason,
            created_by=actor_id,
        )
    else:
        _require_existing_baseline_belongs(
            repo, baseline_id=baseline_id, project_id=draft.project_id
        )

    # --- 第 7 步：条目 / 批准 / 投影列 / 审计 ---------------------------
    #
    # ⚠️ **只有在真的新建了这条基线时才写条目**（MAJOR-2 的另一半）。
    #    幂等命中时（同一 payload 重投）这条基线的条目**已经在库里**了，
    #    再插一遍会静默重复 —— `baseline_items` 的唯一性只按
    #    `(baseline_id, ref_label)` 判，而重投的 ref_label 完全相同，
    #    于是第二遍必然插不进去……但它**不会报错**地走到"部分成功"，
    #    而是让"重投"这个无害动作依赖另一条约束去兜底。
    #    做法上直接跟着 `registered.created` 走：它已经是"这次到底建没建"的
    #    唯一真值来源（第 6 步刚算出来），不必再查一次库。
    if registered.created:
        rows = _item_rows(draft, project_id=draft.project_id)
        for row in rows:
            row["baseline_id"] = baseline_id
        repo.insert_items(rows)
        _link_baseline_dependencies(
            version_repo,
            baseline_id=baseline_id,
            brief_version_id=draft.brief_version_id,
            knowledge_version_ids=[row["knowledge_version_id"] for row in rows],
        )
    # **必须显式 flush**：条目与批准都是延迟约束触发器的检查对象，
    # 它们在 COMMIT 期才跑。若在本函数返回前没有把语句真正发出去，
    # 一次约束失败会发生在 `session_scope` 退出时 —— 那时已经离开
    # 能给出可读 4xx 的代码位置（WP12/WP13/WP16/WP18/WP20/WP23 的教训）。
    repo.flush()

    approval = _ensure_baseline_approval(
        repo,
        project_id=draft.project_id,
        baseline_id=baseline_id,
        decided_by=actor_id,
        reason=approval_reason or change_reason,
        requester="create_baseline",
    )

    # 投影列：`projects.active_baseline_id`（"当前生效的是哪条"的唯一答案）
    repo.set_active_baseline(
        project_id=draft.project_id, baseline_id=baseline_id
    )

    item_count, excluded_count = repo.count_items(baseline_id)

    if actor_id is not None and request_id is not None:
        record_audit(
            AuditRepository(repo.session),
            action=_ACTION_BASELINE_CREATE,
            object_type=_OBJECT_BASELINE,
            object_id=baseline_id,
            request_id=request_id,
            actor_kind=ACTOR_KIND_USER,
            actor_id=actor_id,
            project_id=draft.project_id,
            version_id=baseline_id,
            after={
                "revisionNo": registered.revision_no,
                "itemCount": item_count,
                "excludedCount": excluded_count,
                "rulesetVersion": ruleset_version,
                "supersedesId": None if supersedes_id is None else str(supersedes_id),
            },
            reason=change_reason,
            # ⚠️ `before` 必须与 `after` 配对（MAJOR-5）：
            # `audit_events` 的 `ck_audit_events_hash_pair_consistent` 要求
            # 「两者同时为空或同时非空」。创建前**没有**这条基线，
            # 所以"改前"是空负载 —— 用 `{}` 而不是 `None`，
            # 因为 `None` 会被理解成"这次不记改前"，而这里恰恰记的就是"从无到有"。
            before={},
        )
        if supersedes_id is not None:
            # 替代是**独立的一个动作**，单独记一条：只记 create 会让
            # "旧基线是哪天被替换的"在审计里没有答案（旧行不可变，
            # 它自己不会留下任何痕迹）。
            record_audit(
                AuditRepository(repo.session),
                action=_ACTION_BASELINE_SUPERSEDE,
                object_type=_OBJECT_BASELINE,
                object_id=supersedes_id,
                request_id=request_id,
                actor_kind=ACTOR_KIND_USER,
                actor_id=actor_id,
                project_id=draft.project_id,
                version_id=supersedes_id,
                after={"supersededBy": str(baseline_id)},
                before={"supersededBy": None},
                reason=change_reason,
            )

    repo.flush()

    return CreatedBaseline(
        baseline_id=baseline_id,
        revision_no=registered.revision_no,
        item_count=item_count,
        excluded_count=excluded_count,
        approval_id=approval.id,
        superseded_baseline_id=supersedes_id,
        created=registered.created,
    )


def _baseline_snapshot(
    *, draft: BaselineDraft, info_draft: Any, change_reason: str
) -> dict[str, Any]:
    """基线级的快照负载（原型 L56~64 的 9 项清单里"属于整条基线"的那几项）。

    **它与条目级快照是两个层级**（`models.py` 的说明）：属于某一项的事实值
    落在 `baseline_items.snapshot`；属于整条基线的读数（规则集、完成条件读数、
    各块规模）落在这里。

    ⚠️ **这里刻意不落"批准人 / 批准时间"**：它们由 `step_approvals` 回答
    （`decided_by` / `decided_at`），在这里再存一份就是**第二个真值来源** ——
    而它与批准表脱节时不报错，只会让"这条基线是谁批的"有两个可能矛盾的答案
    （ISS-75 同族）。快照里只放**没有别处回答**的东西。

    ⚠️ **也不落 `activeBaselineId` / `isCurrent` 之类的状态**：那由
    `projects.active_baseline_id` 回答（决策 5），而它**会变** ——
    一条不可变基线里存一个会变的判断，等于让 A-08 的快照在建成那一刻就过期。
    """
    status = step3_status_of_draft(info_draft)
    return {
        "schemaVersion": BASELINE_SCHEMA_VERSION,
        "rulesetVersion": draft.ruleset_version,
        "changeReason": change_reason,
        "briefVersionId": (
            None if draft.brief_version_id is None else str(draft.brief_version_id)
        ),
        # 完成条件读数（原型 L139~165）。它是**建成那一刻的读数** ——
        # 属于快照的本质：半年后回看"当时三条条件都绿吗"应该看到当时的答案，
        # 而不是用今天的草稿重算出来的答案。
        "readiness": {
            condition.key: condition.satisfied for condition in status.conditions
        },
        # 各块的规模。**不是 count 列**（决策 7）：它们只在这里留痕一次，
        # 不参与任何判定 —— 没有任何判据会读它，因此它脱节也不会让防线失效。
        "counts": {
            "claims": len(info_draft.claims or ()),
            "conflicts": len(info_draft.conflicts or ()),
            "scenes": len(info_draft.scenes or ()),
        },
        "boundaries": dict(info_draft.boundaries or {}),
    }


# ---------------------------------------------------------------------------
# 读路径
# ---------------------------------------------------------------------------


def list_baselines(
    repo: BaselineRepository, *, project_id: uuid.UUID, limit: int, offset: int
) -> tuple[list[BaselineSummary], int]:
    """基线列表 + 总数。

    **`revision_no` 从 `version_nodes` 批量取**（`repo.revision_numbers`）：
    `production_baselines` 自己不存它（决策 5：不造第二个真值来源），
    因此列表里那个"BL-v3"的 3 必须与 `GET /versions/{id}` 同源。
    """
    _require_project(repo, project_id)

    rows = repo.list_baselines(project_id=project_id, limit=limit, offset=offset)
    total = repo.count_baselines(project_id=project_id)
    revisions = repo.revision_numbers([row.id for row in rows])
    counts = {row.id: repo.count_items(row.id) for row in rows}

    summaries = [
        BaselineSummary(
            baseline_id=row.id,
            project_id=row.project_id,
            # 缺修订号意味着注册表没有对应节点 —— 那正是 OP-88 判据 1 拦的事。
            # 真出现（只可能来自绕过触发器的回填）时给 0 而不是抛错：
            # 列表端点不该因为一条脏数据整页 500（详情端点会把它暴露出来）。
            revision_no=revisions.get(row.id, 0),
            brief_version_id=row.brief_version_id,
            ruleset_version=row.ruleset_version,
            supersedes_id=row.supersedes_id,
            item_count=counts[row.id][0],
            excluded_count=counts[row.id][1],
            created_at=row.created_at,
        )
        for row in rows
    ]
    return summaries, total


def get_baseline_detail(
    repo: BaselineRepository, *, project_id: uuid.UUID, baseline_id: uuid.UUID
) -> BaselineDetail:
    """基线详情（含全部条目，**包含未纳入项**）。

    **含 `EXCLUDED`**（AC-24 原文）：过滤掉未纳入项会让"为什么没纳入"
    在详情接口里无处可查 —— 而它正是半年后复盘时唯一想知道的事。
    """
    baseline = repo.get_baseline(baseline_id)
    if baseline is None or baseline.project_id != project_id:
        raise _not_found(
            "基线不存在。", reason=REASON_BASELINE_NOT_FOUND
        )

    revision_no = repo.revision_no(baseline_id) or 0
    items = repo.list_items(baseline_id)
    return BaselineDetail(
        baseline_id=baseline.id,
        project_id=baseline.project_id,
        revision_no=revision_no,
        brief_version_id=baseline.brief_version_id,
        ruleset_version=baseline.ruleset_version,
        snapshot=dict(baseline.snapshot or {}),
        supersedes_id=baseline.supersedes_id,
        created_at=baseline.created_at,
        created_by=baseline.created_by,
        items=tuple(_item_view(item) for item in items),
    )


def _item_view(item: BaselineItem) -> BaselineItemView:
    return BaselineItemView(
        item_no=item.item_no,
        item_kind=item.item_kind,
        inclusion=item.inclusion,
        ref_label=item.ref_label,
        snapshot=dict(item.snapshot or {}),
        snapshot_text=item.snapshot_text,
        exclusion_reason=item.exclusion_reason,
        context_order=item.context_order,
        knowledge_asset_id=item.knowledge_asset_id,
        knowledge_version_id=item.knowledge_version_id,
    )


# ---------------------------------------------------------------------------
# 步骤批准（闭合 OP-151 的写入路径）
# ---------------------------------------------------------------------------


def approve_step(
    repo: BaselineRepository,
    *,
    draft: StepApprovalDraft,
    request_id: str | None = None,
) -> ApprovalView:
    """记录一次步骤批准（**不可变**）。

    ## 批准的对象是「步骤 + 版本」的组合（决策 4）

    Brief 出了 v2 之后，步骤 1 的"已批准"指的是 **v2 已批准**，而不是
    "步骤 1 曾经被批过"。只记步骤会让这个问题在数据里**没有答案** ——
    而八步闭环的验收（AC-08~09 的证据 = 「固定版本」）恰恰要回答它。

    ## 重复批准 vs 先拒后批

    | 现状 | 动作 |
    |---|---|
    | 无链根批准 | 插链根 |
    | 已有 `APPROVED` 链根 | **409** `step_already_approved` |
    | 已有 `REJECTED` 链根 | 插一条取代它的新批准 |

    **"重复批同一结论"报 409 而不是幂等返回**：批准是一个**人为动作**，
    重投应当返回同一条（那是幂等）；但"又批了一次"这个动作在数据里
    留下第二条记录，会让"这条被批过几次"虚假增长。两者的区别在于
    调用方能否区分 —— 它不能，因此宁可让它看见 409 并决定要不要重读。
    （与本包 `create_baseline` 的重投路径刻意不同：那条路径必须成功，
    否则"超时后重试"永远失败，见 `_ensure_baseline_approval`。）

    ## 交叉核对 `subject_kind` 与节点类型

    `step_approvals.subject_kind` 是"这一步的产物是什么"，
    `version_nodes.entity_type` 是"注册表里的类型标签" —— 两者语义不同
    （`baseline_types` 的说明），但**一个声称 `BASELINE` 的批准指向一个
    `BRIEF` 节点**显然是矛盾。这类矛盾能在写入前抓到，就不必等下游
    读到时才发现。`RESEARCH` 没有对应的 `entity_type`，因此跳过核对 ——
    "无法核对"与"核对失败"是两件事。

    **`_SUBJECT_KIND_ENTITY_TYPE.get()` 返回 `None` 的两种含义已被消解**：
    字面上 `None` 可以是"这个 kind 不在表里"或"这个 kind 刻意映射到 `None`"。
    本函数上面已经调过 `_require_subject_kind(draft.subject_kind)`，它保证
    `subject_kind ∈ SUBJECT_KINDS`；而 `_SUBJECT_KIND_ENTITY_TYPE` 的键集合
    与之**相等**（`tests/test_baseline_wp22.py` 有一条机械判据钉住这件事），
    因此走到 `expected_entity_type = ...get(...)` 这一行时，
    返回 `None` **只可能**来自 `SUBJECT_KIND_RESEARCH` 的显式映射。
    若谁将来给 `SUBJECT_KINDS` 加了新成员却忘了同步这张表，`.get()` 会返回
    `None` → 静默跳过核对 —— 那条相等性判据就是为了让这种"漏加"在测试里
    失败，而不是在这里静默放过。
    """
    _require_project(repo, draft.project_id)
    _require_step_no(draft.step_no)
    _require_subject_kind(draft.subject_kind)
    _require_decision(draft.decision)
    reason = _require_reason_text(
        draft.reason,
        field="reason",
        max_chars=MAX_REASON_CHARS,
        reason_code=REASON_APPROVAL_REASON_REQUIRED,
    )

    if not repo.node_exists(draft.subject_version_id):
        raise _validation_error(
            "subjectVersionId",
            "被批准的版本节点不存在。",
            reason=REASON_SUBJECT_VERSION_NOT_FOUND,
        )

    expected_entity_type = _SUBJECT_KIND_ENTITY_TYPE.get(draft.subject_kind)
    if expected_entity_type is not None:
        actual = repo.node_entity_type(draft.subject_version_id)
        if actual != expected_entity_type:
            raise _validation_error(
                "subjectVersionId",
                f"subjectKind={draft.subject_kind} 的批准必须指向 "
                f"{expected_entity_type} 版本节点，而该节点是 {actual}。",
                reason=REASON_SUBJECT_VERSION_NOT_FOUND,
                expectedEntityType=expected_entity_type,
                actualEntityType=actual,
            )

    # ⚠️ 取"当前生效的那条"（链尾），不是链根（MAJOR-6）。
    # 重复结论判据要落在这条上，否则重批过一轮之后
    # 旧链根与新链尾会同时被视作依据，"已经批过"判定失效。
    effective = repo.find_effective_approval(
        project_id=draft.project_id,
        step_no=draft.step_no,
        subject_kind=draft.subject_kind,
        subject_version_id=draft.subject_version_id,
    )

    if effective is not None and effective.decision == draft.decision:
        raise _conflict(
            f"步骤 {draft.step_no}（{STEP_NAMES.get(draft.step_no, '未知')}）"
            f"对这一版的结论已经是 {draft.decision}，无需重复记录。",
            reason=REASON_ALREADY_APPROVED,
            approvalId=str(effective.id),
            decision=draft.decision,
        )

    approval = repo.insert_approval(
        project_id=draft.project_id,
        step_no=draft.step_no,
        subject_kind=draft.subject_kind,
        subject_version_id=draft.subject_version_id,
        decision=draft.decision,
        reason=reason,
        decided_by=draft.decided_by,
        # 先拒后批：新增一条取代旧的（**不可变表的"撤销"靠新增，不靠改写**）。
        supersedes_id=effective.id if effective is not None else draft.supersedes_id,
    )
    repo.flush()

    if draft.decided_by is not None and request_id is not None:
        record_audit(
            AuditRepository(repo.session),
            action=_ACTION_STEP_APPROVE,
            object_type=_OBJECT_APPROVAL,
            object_id=approval.id,
            request_id=request_id,
            actor_kind=ACTOR_KIND_USER,
            actor_id=draft.decided_by,
            project_id=draft.project_id,
            version_id=draft.subject_version_id,
            after={
                "stepNo": draft.step_no,
                "subjectKind": draft.subject_kind,
                "decision": draft.decision,
                "supersedesId": None if effective is None else str(effective.id),
            },
            # ⚠️ `before` 必须与 `after` 配对（MAJOR-5），语义是"本次取代掉的
            # 那条批准当时的结论"。没有被取代的（首次批准）就是空负载。
            before={} if effective is None else {"decision": effective.decision},
            reason=reason,
        )
        repo.flush()

    return ApprovalView(
        approval_id=approval.id,
        project_id=approval.project_id,
        step_no=approval.step_no,
        subject_kind=approval.subject_kind,
        subject_version_id=approval.subject_version_id,
        decision=approval.decision,
        decided_at=approval.decided_at,
    )


def require_baseline_approval(
    repo: BaselineRepository,
    *,
    project_id: uuid.UUID,
    baseline_id: uuid.UUID,
) -> None:
    """要求某条基线有一条生效的批准（第 4 层防线的服务层一侧）。

    ⚠️ **它的存在不是为了"也判一遍"** —— 数据库那个延迟约束触发器是
    权威。它存在是为了给**能读到的调用方**（例如"试建基线但不提交"的
    诊断路径）一个可读的 409，而不是让它在 COMMIT 时变成一个
    `sqlstate=23514`。**判据本身在触发器里**，这里只是把它翻译成人话。
    """
    # 与触发器同口径：判"当前生效的那条"（链尾），不是链根（MAJOR-6）。
    # 用链根判会漏掉"重批成 APPROVED"的情形（链根停在 REJECTED）→ 错误放行。
    effective = repo.find_effective_approval(
        project_id=project_id,
        step_no=BASELINE_STEP_NO,
        subject_kind=SUBJECT_KIND_BASELINE,
        subject_version_id=baseline_id,
    )
    if effective is None or effective.decision != "APPROVED":
        raise _conflict(
            "该基线没有生效的批准（OP-151：步骤 3 创建基线时同事务生成批准）。",
            reason=REASON_APPROVAL_REQUIRED,
            baselineId=str(baseline_id),
        )


def list_approvals(
    repo: BaselineRepository, *, project_id: uuid.UUID, step_no: int | None = None
) -> list[ApprovalView]:
    """本项目（可按步骤过滤）的批准历史。

    **不筛掉被取代的那些**：`supersedes_id` 链是"先拒后批"的痕迹，
    而"曾经被拒过"是审计信息（决策 4）。筛掉会让"这一步被批了几次、
    中间有没有反复"查不到。
    """
    _require_project(repo, project_id)
    if step_no is not None:
        _require_step_no(step_no)
    return [
        ApprovalView(
            approval_id=row.id,
            project_id=row.project_id,
            step_no=row.step_no,
            subject_kind=row.subject_kind,
            subject_version_id=row.subject_version_id,
            decision=row.decision,
            decided_at=row.decided_at,
        )
        for row in repo.list_approvals(project_id=project_id, step_no=step_no)
    ]


# ---------------------------------------------------------------------------
# 投影列（`projects.active_baseline_id`）
# ---------------------------------------------------------------------------


def active_baseline_id(
    repo: BaselineRepository, *, project_id: uuid.UUID
) -> uuid.UUID | None:
    """当前生效的基线 id（`projects.active_baseline_id`）。"""
    _require_project(repo, project_id)
    return repo.active_baseline_id(project_id)


def rebuild_active_baseline(
    repo: BaselineRepository, *, project_id: uuid.UUID
) -> uuid.UUID | None:
    """从基线链**全量重算** `active_baseline_id`（恢复/对账用）。

    它是"增量维护（创建基线时 `set_active_baseline`）与投影定义一致"的判据 ——
    测试断言"用增量路径走 N 步"与"直接重算"得到同一个值
    （与 WP20 `rebuild_active_version` 同款）。

    **它与增量路径必须调同一个 `newest_baseline_of()`**：若重算逻辑自己
    写一份"取最新"，两处一旦漂移，问题会在恢复数据时才暴露
    （那时既没有增量历史也没有对照）。
    """
    _require_project(repo, project_id)
    return repo.rebuild_active_baseline(project_id)


__all__ = [
    "active_baseline_id",
    "approve_step",
    "create_baseline",
    "draft_etag",
    "draft_view",
    "get_baseline_detail",
    "list_approvals",
    "list_baselines",
    "rebuild_active_baseline",
    "require_baseline_approval",
    "require_draft",
    "step3_status",
    "step3_status_of_draft",
    "update_draft",
]
