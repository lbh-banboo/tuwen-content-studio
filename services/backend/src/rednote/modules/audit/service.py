"""审计写入、待办去重与完成、通知回执（WP15）。

**本模块承载验收的两半**：

| 半 | 函数 | 结构性保证 |
|---|---|---|
| **日志可追踪且不泄露正文** | `record_audit` | 只存规范化**摘要**（复用 WP11 的 `canonical_payload_hash`），不存正文；表本身由触发器保护不可篡改 |
| **待办去重** | `ensure_work_item` | `dedupe_key` 部分唯一索引（`WHERE state='OPEN'`）+ `ON CONFLICT DO NOTHING` |

**关于"不泄露正文"的三层防线**（缺一不可）：

1. **模型层**：`audit_events` 只有 `before_hash` / `after_hash`（String(64)），
   **没有存正文的列**。想存也存不进去 —— 这是最硬的一层。
2. **服务层**：`record_audit` 接受的是**已算好的哈希**，不是 payload。
   调用方必须自己算（用 `hash_payload`）—— 这让"传正文"在类型上就不可能。
3. **写入校验**：`reason` 是唯一可能承载长文本的字段（运营写的理由），
   它有 512 字符上限并由 CHECK 兜底。

**为什么"审计不存正文"值得这么用力**：审计是**日志**，
而日志的常见死法是"为了方便排障把内容也记进去"。一旦这么做：
  · 运维/开发看日志就等于看到了全部业务内容（**且没有任何痕迹**）；
  · 日志的保留期通常比业务数据长（§15："审计默认长期保留"）——
    意味着**已删除的业务内容仍留在日志里**；
  · 备份、导出、日志采集链路都会把它带走。
这三条都不需要恶意，只需要"当时为了排障方便一点"。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from rednote.modules.audit.models import (
    ACTOR_KINDS,
    ACTOR_KIND_USER,
    AuditEvent,
)
from rednote.modules.audit.repository import AuditRepository
from rednote.modules.identity.dependencies import CurrentUser
from rednote.modules.versions.service import canonical_payload_hash
from rednote.modules.work_items.models import (
    WORK_ITEM_OPEN,
    WORK_ITEM_STATES,
    WorkItem,
)
from rednote.platform.errors import ApiError, ErrorCode

# 一次分页查询的最大条数（与其它模块一致）。
MAX_PAGE_SIZE: Final[int] = 100
DEFAULT_PAGE_SIZE: Final[int] = 20

# `reason` 的长度上限，与 `audit_events.reason` 的列宽一致。
# 在一处定义、两处使用（列宽 + 校验），避免"列能存 512 但校验只让 255"。
MAX_REASON_LENGTH: Final[int] = 512


# ---------------------------------------------------------------------------
# 返回值
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditView:
    """一条审计事件的不可变快照。

    **注意它没有正文** —— 这不是遗漏，是设计（见模块 docstring）。
    想看内容的人应该顺着 `version_id` 去查那一版对象。
    """

    audit_id: uuid.UUID
    actor_kind: str
    actor_id: uuid.UUID | None
    action: str
    object_type: str
    object_id: uuid.UUID
    project_id: uuid.UUID | None
    version_id: uuid.UUID | None
    before_hash: str | None
    after_hash: str | None
    reason: str | None
    request_id: str
    created_at: datetime

    @property
    def changed(self) -> bool:
        """这次动作是否改变了对象内容。

        有哈希对（且不相等）= 内容变了；无哈希 = 这次动作不涉及内容
        （如"查看"、"登录"）；哈希相等 = 写入了相同内容（幂等命中）。
        三种情况对排障的含义完全不同，因此给出这个判断而不是让调用方自己比。
        """
        if self.before_hash is None or self.after_hash is None:
            return False
        return self.before_hash != self.after_hash


@dataclass(frozen=True)
class WorkItemView:
    """待办的不可变快照。"""

    work_item_id: uuid.UUID
    project_id: uuid.UUID | None
    assignee_id: uuid.UUID | None
    kind: str
    subject_type: str
    subject_id: uuid.UUID
    state: str
    due_at: datetime | None
    dedupe_key: str | None
    created_at: datetime


@dataclass(frozen=True)
class WorkItemPage:
    """一页待办（无 HTTP 端点之外的用途：供端点组装 page 块）。"""

    items: tuple[WorkItemView, ...]
    has_more: bool
    next_anchor: tuple[datetime, uuid.UUID] | None


@dataclass(frozen=True)
class AuditPage:
    """一页审计。"""

    items: tuple[AuditView, ...]
    has_more: bool
    next_anchor: tuple[datetime, uuid.UUID] | None


@dataclass(frozen=True)
class WorkItemUpsert:
    """一次待办创建/命中的结果。"""

    item: WorkItemView
    created: bool


@dataclass(frozen=True)
class ResolutionResult:
    """一次"完成待办"的结果。"""

    item: WorkItemView
    # 本次是否真的完成了（False = 幂等命中：它本来就是 DONE）。
    resolved: bool


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _validation_error(field: str, message: str, *, reason: str, **extra: Any) -> ApiError:
    details: dict[str, Any] = {
        "fieldErrors": [{"field": field, "type": reason, "message": message}],
        "reason": reason,
    }
    details.update(extra)
    return ApiError(ErrorCode.VALIDATION_FAILED, message, details=details)


def _require_non_empty(value: str, *, field: str) -> str:
    text = value.strip()
    if not text:
        raise _validation_error(field, f"{field} 不能为空。", reason="empty")
    return text


def _require_actor_kind(value: str) -> str:
    if value not in ACTOR_KINDS:
        raise _validation_error(
            "actorKind",
            "未知的行为主体类型。",
            reason="actor_kind_unknown",
            allowedActorKinds=sorted(ACTOR_KINDS),
        )
    return value


def _require_state(state: str) -> str:
    if state not in WORK_ITEM_STATES:
        raise _validation_error(
            "state",
            "未知的待办状态。",
            reason="work_item_state_unknown",
            allowedStates=list(WORK_ITEM_STATES),
        )
    return state


def normalize_reason(raw: str | None) -> str | None:
    """校验并归一化 `reason`。

    **它可以是"正文"吗**：这里说的"不泄露正文"指的是**业务对象的正文**
    （Brief 内容、生成文案、知识正文）。`reason` 是"为什么做这个动作"
    （运营填的理由），它**本来就该给人看** —— 审计的价值一半在这里。

    但仍然要限长（512）：`reason` 是审计字段，不是内容存储。
    允许无限长度会让它被当成"顺便把内容也塞进来"的通道 ——
    而那就是"审计泄露正文"最常见的入口。
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if len(text) > MAX_REASON_LENGTH:
        raise _validation_error(
            "reason",
            f"reason 过长（上限 {MAX_REASON_LENGTH} 字符）。",
            reason="reason_too_long",
            maxLength=MAX_REASON_LENGTH,
        )
    return text


# ---------------------------------------------------------------------------
# 审计写入
# ---------------------------------------------------------------------------


def hash_payload(payload: Mapping[str, Any] | None) -> str | None:
    """算一个规范化摘要；`None` 原样返回。

    **复用 WP11 的 `canonical_payload_hash`**（见 models 的论证）：
    两处用不同算法会让"审计里的 before_hash 和版本里的 payload_hash
    是不是同一份数据"永远没有答案，而"审计说从 X 变到 Y，这个 X 对应哪一版"
    恰恰是审计最常见的核对场景。

    `None` 原样返回而不是算一个"空对象的哈希"：**"没有内容"与
    "内容是空对象"是两件事** —— 前者表示这次动作不涉及内容
    （查看、登录），后者表示内容确实被清空了。算成同一个哈希就分不出来了。
    """
    if payload is None:
        return None
    return canonical_payload_hash(payload)


def record_audit(
    repo: AuditRepository,
    *,
    action: str,
    object_type: str,
    object_id: uuid.UUID,
    request_id: str,
    actor_kind: str = ACTOR_KIND_USER,
    actor_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    version_id: uuid.UUID | None = None,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    reason: str | None = None,
) -> AuditView:
    """写一条审计。**与业务变更同事务**（调用方不加 commit）。

    **入参是 `before` / `after` 的 payload，本函数负责算哈希** ——
    而不是让调用方传哈希。理由：算哈希是**容易漏的**（漏了就没有凭据），
    而"传 payload 由本函数算"让这件事不可能被忘掉。
    哈希本身仍然不会把 payload 写进库 —— 它只在函数内部存在。

    **`actor_kind` 与 `actor_id` 的一致性**：`USER` 必须有 `actor_id`
    （否则"谁做的"无从查起）；`SYSTEM` / `SERVICE` 的 `actor_id` 可空。
    这条在下面显式校验 —— 让"忘了传 actor_id 的 USER 审计"当场失败，
    而不是在几个月后回溯时才发现查不到人。
    """
    normalized_action = _require_non_empty(action, field="action")
    normalized_object_type = _require_non_empty(object_type, field="objectType")
    normalized_actor_kind = _require_actor_kind(actor_kind)
    normalized_request_id = _require_non_empty(request_id, field="requestId")

    if normalized_actor_kind == ACTOR_KIND_USER and actor_id is None:
        raise _validation_error(
            "actorId",
            "actorKind 为 USER 时必须提供 actorId。",
            reason="actor_id_required_for_user",
        )

    event = repo.insert_audit_event(
        actor_kind=normalized_actor_kind,
        actor_id=actor_id,
        action=normalized_action,
        object_type=normalized_object_type,
        object_id=object_id,
        request_id=normalized_request_id,
        project_id=project_id,
        version_id=version_id,
        before_hash=hash_payload(before),
        after_hash=hash_payload(after),
        reason=normalize_reason(reason),
    )
    repo.flush()
    return _to_audit_view(event)


def _to_audit_view(event: AuditEvent) -> AuditView:
    return AuditView(
        audit_id=event.id,
        actor_kind=event.actor_kind,
        actor_id=event.actor_id,
        action=event.action,
        object_type=event.object_type,
        object_id=event.object_id,
        project_id=event.project_id,
        version_id=event.version_id,
        before_hash=event.before_hash,
        after_hash=event.after_hash,
        reason=event.reason,
        request_id=event.request_id,
        created_at=event.created_at,
    )


# ---------------------------------------------------------------------------
# 审计查询
# ---------------------------------------------------------------------------


def list_audit_events(
    repo: AuditRepository,
    *,
    user: CurrentUser,
    member_project_ids: frozenset[uuid.UUID],
    project_id: uuid.UUID | None = None,
    object_type: str | None = None,
    object_id: uuid.UUID | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    after: tuple[datetime, uuid.UUID] | None = None,
) -> AuditPage:
    """查询审计（协议 §10：「项目成员仅见所属项目；**管理者全量**」）。

    **可见范围的判据**（延续 WP11/WP12/WP14 的口径）：

    | 情形 | 结果 |
    |---|---|
    | 管理者 | 全量（含平台级 `project_id IS NULL`） |
    | 指定 `projectId` 且我是该项目成员 | 该项目 |
    | 指定 `projectId` 但我不是成员 | **空页**（不是 403 —— 防存在性枚举） |
    | 未指定 `projectId` | 我所属**全部**项目的审计 |

    最后一行是协议原文"项目成员仅见所属项目"的直接落地：
    非成员未指定过滤时，**不能**返回全量（那是管理者才有的）。
    """
    normalized_limit = _clamp_limit(limit)
    normalized_state = None  # 审计没有 state 过滤（协议未定义）

    if project_id is not None:
        if not _may_view_project(
            user=user, project_id=project_id, member_project_ids=member_project_ids
        ):
            # 不可见 → 空页。**不报 403**：那会告诉攻击者"这个项目存在"。
            return AuditPage(items=(), has_more=False, next_anchor=None)
        rows = repo.list_audit_events(
            project_id=project_id,
            object_type=object_type,
            object_id=object_id,
            limit=normalized_limit,
            after=after,
        )
        return _to_audit_page(rows, limit=normalized_limit)

    if user.is_admin:
        rows = repo.list_audit_events(
            object_type=object_type,
            object_id=object_id,
            limit=normalized_limit,
            after=after,
        )
        return _to_audit_page(rows, limit=normalized_limit)

    # 非管理者且未指定项目：只能看自己所属项目的审计。
    # **不能**直接调 `list_audit_events()`（那是全量）。
    # 这里用 `project_id IN (...)` 做过滤 —— 复用 repository 的单项目查询
    # 会退化成"逐项目查一遍再合并"，而合并后的分页游标无法正确工作。
    rows = repo.list_audit_events_for_projects(
        project_ids=sorted(member_project_ids),
        object_type=object_type,
        object_id=object_id,
        limit=normalized_limit,
        after=after,
    )
    return _to_audit_page(rows, limit=normalized_limit)


def _to_audit_page(rows: Sequence[AuditEvent], *, limit: int) -> AuditPage:
    has_more = len(rows) > limit
    window = tuple(_to_audit_view(row) for row in rows[:limit])
    next_anchor: tuple[datetime, uuid.UUID] | None = None
    if has_more and window:
        last = window[-1]
        next_anchor = (last.created_at, last.audit_id)
    return AuditPage(items=window, has_more=has_more, next_anchor=next_anchor)


def _clamp_limit(limit: int) -> int:
    """把 limit 夹到 [1, MAX_PAGE_SIZE]。**不报错**（夹紧比拒绝更友好）。

    负数或 0 会被夹成 1；超过上限夹到上限。
    这与"limit 非法就 422"是两种取舍：这里选夹紧，因为 limit 是
    "我想看多少"的提示，不是业务约束；为一个提示性参数拒绝整页数据
    对用户没有帮助。
    """
    return max(1, min(limit, MAX_PAGE_SIZE))


def _may_view_project(
    *,
    user: CurrentUser,
    project_id: uuid.UUID,
    member_project_ids: frozenset[uuid.UUID],
) -> bool:
    if user.is_admin:
        return True
    return project_id in member_project_ids


# ---------------------------------------------------------------------------
# 待办
# ---------------------------------------------------------------------------


def ensure_work_item(
    repo: AuditRepository,
    *,
    dedupe_key: str,
    kind: str,
    subject_type: str,
    subject_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    due_at: datetime | None = None,
) -> WorkItemUpsert:
    """建待办（**同未结去重**）—— 验收第二半的落点。

    **为什么 dedupe 必须是数据库级的**：并发下两个进程同时发现
    "该提醒"，先查再插会让两个都插入 —— 同一对象被提醒两次。
    而这件事在语义上完全正常（重试、并发触发、消息重复投递）。
    唯一索引是**唯一防线**，本函数只是正确地用它（`ON CONFLICT DO NOTHING`）。

    **`dedupe_key` 的构造由调用方负责**，本函数不猜。
    但给出明确的构造约定（见 `build_dedupe_key`）：
    同一"对象 + 待办类型"用同一个 key —— 这样"同一内容版本只生成一条
    待审待办"就自然成立。

    **已结待办不参与去重**（部分唯一索引的 `WHERE state='OPEN'`）：
    同一对象反复产生待办是**正常**的（内容改了要重新审），
    只有"同时未结"才是冲突。若唯一性不限于未结，
    第二次提醒会被永远挡住 —— 而那正是"待办列表看起来正常、
    实际漏了提醒"的静默故障。
    """
    normalized_key = _require_non_empty(dedupe_key, field="dedupeKey")
    normalized_kind = _require_non_empty(kind, field="kind")
    normalized_subject_type = _require_non_empty(subject_type, field="subjectType")

    item, created = repo.upsert_work_item(
        dedupe_key=normalized_key,
        kind=normalized_kind,
        subject_type=normalized_subject_type,
        subject_id=subject_id,
        project_id=project_id,
        assignee_id=assignee_id,
        due_at=due_at,
    )
    repo.flush()
    return WorkItemUpsert(item=_to_work_item_view(item), created=created)


def build_dedupe_key(*, kind: str, subject_type: str, subject_id: uuid.UUID) -> str:
    """构造去重键：`{kind}:{subject_type}:{subject_id}`。

    **为什么做成函数而不是让调用方拼**：key 的格式是**契约**——
    两处拼法不同（例如一处带空格、一处不带）会让去重静默失效：
    两条"本该是同一条"的待办并存，而**不会报任何错**。
    统一走这个函数，格式只有一个来源。

    不含 `project_id`：`subject_id` 已经是全局唯一的 UUID，
    带上项目只会让 key 更长。也不含 `assignee_id`：
    **转交不应该产生新待办**（同一件事换了人做，还是那件事）——
    若 key 含 assignee，转交会让旧待办"未结但无人"、新待办"新人未结"，
    同一件事在列表里出现两次。
    """
    return f"{kind}:{subject_type}:{subject_id}"


def _to_work_item_view(item: WorkItem) -> WorkItemView:
    return WorkItemView(
        work_item_id=item.id,
        project_id=item.project_id,
        assignee_id=item.assignee_id,
        kind=item.kind,
        subject_type=item.subject_type,
        subject_id=item.subject_id,
        state=item.state,
        due_at=item.due_at,
        dedupe_key=item.dedupe_key,
        created_at=item.created_at,
    )


def list_work_items(
    repo: AuditRepository,
    *,
    user: CurrentUser,
    member_project_ids: frozenset[uuid.UUID],
    project_id: uuid.UUID | None = None,
    state: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    after: tuple[datetime, uuid.UUID] | None = None,
) -> WorkItemPage:
    """查询待办（协议 §10：「**当前成员**待办」）。

    **可见范围（§2 选型确认的口径）**：

    | 待办 | 谁能看到 |
    |---|---|
    | 指派给当前用户 | 该用户 |
    | **未认领**（`assignee_id IS NULL`）且属于我所属项目 | 该项目成员 |
    | 指派给别人 | **看不到**（不是"我的待办"） |
    | 管理者 | 全量 |

    **未认领为什么对项目成员可见**：需求 §2.3 明说有"未认领的项目待办"，
    而它的用途就是"让项目成员看到并认领/转交"。若只给管理者看，
    这些待办就没人认领 —— 它们会永远躺在库里。

    **指派给别人的为什么不给看**：协议原文是"**当前成员**待办"，
    语义是"我要做什么"，不是"团队在做什么"。
    要看团队全貌应走项目视图（后续包），不是待办列表。

    **状态过滤默认 `OPEN`**：用户打开待办列表想看到的是"还没做的"。
    协议把 `state` 列为入参（可选），因此显式传 `DONE` 也能查历史 ——
    但默认必须是 OPEN，否则列表里会混进已完成的，用户要自己筛。
    """
    normalized_limit = _clamp_limit(limit)
    normalized_state = _require_state(state) if state is not None else WORK_ITEM_OPEN

    if project_id is not None and not _may_view_project(
        user=user, project_id=project_id, member_project_ids=member_project_ids
    ):
        return WorkItemPage(items=(), has_more=False, next_anchor=None)

    rows = repo.list_work_items_visible(
        project_id=project_id,
        state=normalized_state,
        user_id=user.id,
        member_project_ids=sorted(member_project_ids),
        include_all=user.is_admin,
        limit=normalized_limit,
        after=after,
    )
    has_more = len(rows) > normalized_limit
    window = tuple(_to_work_item_view(row) for row in rows[:normalized_limit])
    next_anchor: tuple[datetime, uuid.UUID] | None = None
    if has_more and window:
        last = window[-1]
        next_anchor = (last.created_at, last.work_item_id)
    return WorkItemPage(items=window, has_more=has_more, next_anchor=next_anchor)


def resolve_work_item(
    repo: AuditRepository,
    *,
    work_item_id: uuid.UUID,
    reason: str | None,
    user: CurrentUser,
    request_id: str,
    member_project_ids: frozenset[uuid.UUID],
) -> ResolutionResult:
    """完成待办（协议 §10：`POST /work-items/{id}/resolutions`）。

    协议原句：**「完成待办不自动通过其业务对象」** —— 这句话是本函数的
    核心约束：它**只**把待办置为 DONE，**不**去改任何业务对象的状态。
    例如"内容待审"待办完成后，内容**仍然**是待审状态 ——
    审核要通过它自己的审核命令走。

    **为什么这条约束重要**：把"完成待办"当成"通过审核"的快捷方式
    会绕过审核链路上的全部校验（自审、版本绑定、影响标记）。
    待办是**提醒**，不是**决策**。

    **幂等**：已 DONE 的待办再次完成 → 返回 `resolved=False`，
    **不报错**。用户点两次"完成"是正常操作（网络重试、手滑），
    报错只会让他以为没完成。

    **同时写审计**：完成待办是"谁在什么时候把哪条待办结了、为什么"——
    这正是审计要记的。**同事务**写入（与状态变更一致）。
    """
    item = repo.get_work_item(work_item_id)
    if item is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "待办不存在。",
            details={"resourceId": str(work_item_id)},
        )

    # 权限：能看到才能完成。判据与列表**共用同一个函数**（避免两套漂移）。
    if not _may_resolve(
        user=user, item=item, member_project_ids=member_project_ids
    ):
        # 不可见 → 404（不是 403，防存在性枚举）。
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "待办不存在。",
            details={"resourceId": str(work_item_id)},
        )

    before_state = item.state
    changed = repo.resolve_work_item(work_item_id=work_item_id, reason=reason)
    repo.flush()

    # **重新读一次**拿到最新状态（`resolve_work_item` 走的是 UPDATE 语句，
    # 执行后 ORM 实例可能被 expire；直接读属性会触发惰性加载 ——
    # WP10/WP12/WP13 都踩过 PendingRollbackError）。
    refreshed = repo.get_work_item(work_item_id)
    assert refreshed is not None
    if changed:
        # 只有**真的改了**才写审计 —— 幂等命中不写。
        # 否则一次重试会留下两条"完成了待办"的审计，而其中一条是假的。
        record_audit(
            repo,
            action="work_item.resolve",
            object_type="work_item",
            object_id=work_item_id,
            request_id=request_id,
            actor_kind=ACTOR_KIND_USER,
            actor_id=user.id,
            project_id=refreshed.project_id,
            before={"state": before_state},
            after={"state": refreshed.state},
            reason=reason,
        )
        repo.flush()

    return ResolutionResult(
        item=_to_work_item_view(refreshed), resolved=changed
    )


def _may_resolve(
    *,
    user: CurrentUser,
    item: WorkItem,
    member_project_ids: frozenset[uuid.UUID],
) -> bool:
    """能否完成这条待办。**判据与列表的可见范围一致**。

    | 待办 | 谁能完成 |
    |---|---|
    | 指派给当前用户 | 该用户 |
    | 未认领且属于我所属项目 | 该项目成员（"认领并做完"的落地就是完成它） |
    | 指派给别人 | **不能**（不是他的事） |
    | 任何 | 管理者 |

    **为什么必须与 `list_work_items` 的可见范围一致**：
    两处各写一遍迟早漂移，而漂移的后果是"能看到的却完不成"（用户困惑）
    或"看不到的却能完成"（越权）。因此这里与列表共用
    `_may_see_item` —— 它接受**同一份** `member_project_ids`
    （由 API 层算一次、两处共用），所以两处的判据不可能不一致。

    **`member_project_ids` 由调用方传入而不是在这里查库**：
    判据本身与"怎么拿到成员集合"是两件事。分开之后，
    这个函数是**纯的**（可毫秒级单测），而查库那一半在 API 层
    （与 `load_project_access` 同一手法）。

    **直接委托给 `_may_see_item`**：两者判据完全相同（含管理者短路）。
    过去这里自己又写了一遍 `if user.is_admin`，那一段是死代码 ——
    `_may_see_item` 开头已短路管理者。留着一个名字是有用的
    （调用点读作"能不能完成它"比"能不能看见它"更贴切），
    但判据只允许有一个来源。
    """
    return _may_see_item(
        user=user, item=item, member_project_ids=member_project_ids
    )


def _may_see_item(
    *,
    user: CurrentUser,
    item: WorkItem,
    member_project_ids: frozenset[uuid.UUID],
) -> bool:
    """**可见范围的唯一判据**（列表与完成共用它）。

    单条版本 —— `list_work_items` 的 SQL 过滤是它的批量等价物。
    两者必须表达同一规则：SQL 那份在 `repository.list_work_items_visible`，
    本函数在 Python 侧；测试里有一条专门断言"能看到的就能完成"。
    """
    if user.is_admin:
        return True
    if item.assignee_id is not None:
        return item.assignee_id == user.id
    # 未认领：需要项目成员身份。组织级未认领待办（project_id 为空）
    # 只有管理者能碰（上面已放行）—— 它不属于任何项目，没有"成员"可判。
    if item.project_id is None:
        return False
    return item.project_id in member_project_ids


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "MAX_REASON_LENGTH",
    "AuditPage",
    "AuditView",
    "ResolutionResult",
    "WorkItemPage",
    "WorkItemUpsert",
    "WorkItemView",
    "build_dedupe_key",
    "ensure_work_item",
    "hash_payload",
    "list_audit_events",
    "list_work_items",
    "normalize_reason",
    "record_audit",
    "resolve_work_item",
]