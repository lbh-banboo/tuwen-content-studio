"""WP29 反馈定位与审核决定的服务层：7 个用例 + 两道并发闸。

## 七个用例与两条锁

| 用例 | 函数 | 保护的是哪把锁 |
|---|---|---|
| 预览聚合 | `build_preview` | 只读，不参与并发串行化 |
| 编辑追加版 | `edit_content` | `content_items.lock_version` —— **判断在 `content.service` 里**，本层只转发 |
| 反馈列表 | `list_feedback` | 只读；锚点新鲜度**读时**判定 |
| 新建反馈 | `create_feedback` | 无行锁；用"当前正文"校验锚点是可读的 4xx |
| 推进反馈状态 | `advance_feedback` | `content_feedback_items.lock_version` —— 本层自己的 412 |
| 单条审核 | `decide_review` | 阻塞事实（BLOCKING）前置校验 |
| 批量审核 | `decide_reviews_batch` | 逐条独立子事务 |

三码的分工（协议 §1.2）：

· **412 `VERSION_CONFLICT`** —— `If-Match` 与库里的 `lock_version` 不一致。
  `edit_content` 的 412 由 `content.service.append_edited_version` 抛；
  `advance_feedback` 的 412 由本层抛（两把锁、两个判断点，**不共享**代码：
  共享会让"内容的锁"与"反馈的锁"在边界上互相污染）。
· **409 `INVALID_TRANSITION`** —— 业务状态不允许（有未解决的 BLOCKING 反馈
  时不允许通过）。这是"当前状态不允许这个操作"，客户端刷新也**不该**盲目重试。
· **428 `PRECONDITION_REQUIRED`** —— 缺少 `If-Match`。由端点层的
  `platform.concurrency.require_if_match` 统一判，本层不接触请求对象。

## 为什么事件表与状态列必须同一事务

`content_feedback_events` 是**历史**，`content_feedback_items.state` 是
**投影**（当前值）。两者在同一事务里写，失败就一起回滚；分两次提交会出现
"状态前进了但没有事件"（溯源断档）或"有事件但状态没变"（历史说谎）。
本层全程只 `flush()`，从不 `commit()` —— 提交边界归调用方（端点 / 用例编排）。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from rednote.modules.content.types import ContentVersionSummary
from rednote.modules.feedback import content_bridge, media_bridge
from rednote.modules.feedback.models import ContentFeedbackItem, ContentReview
from rednote.modules.feedback.repository import FeedbackRepository
from rednote.modules.feedback.types import (
    ACTOR_KINDS,
    ANCHOR_KIND_MEDIA,
    ANCHOR_KIND_PARAGRAPH,
    ANCHOR_KIND_WHOLE,
    ANCHOR_KINDS,
    FEEDBACK_CATEGORIES,
    FEEDBACK_SEVERITIES,
    FEEDBACK_SEVERITY_BLOCKING,
    FEEDBACK_SCOPES,
    FEEDBACK_STATE_OPEN,
    FEEDBACK_STATE_REJECTED,
    FEEDBACK_STATE_RESOLVED,
    FEEDBACK_STATES,
    MAX_FEEDBACK_BODY_CHARS,
    MAX_REJECT_REASON_CHARS,
    MAX_SELECTED_TEXT_CHARS,
    REASON_FEEDBACK_LOCK_VERSION_MISMATCH,
    REVIEW_DECISIONS,
    REVIEW_DECISION_APPROVED,
)
from rednote.platform.concurrency import version_conflict
from rednote.platform.errors import ApiError, ErrorCode
from rednote.platform.pagination import (
    DEFAULT_LIMIT,
    CursorPosition,
    CursorSpec,
    PageInfo,
    PageParams,
    decode_cursor,
    encode_cursor,
)

# ---------------------------------------------------------------------------
# 可读 4xx 的 reason（放进 `details.reason`，供前端与断言机器可判）
# ---------------------------------------------------------------------------
REASON_ANCHOR_KIND_INVALID: Final[str] = "ANCHOR_KIND_INVALID"
REASON_ANCHOR_PARAGRAPH_REF_REQUIRED: Final[str] = "ANCHOR_PARAGRAPH_REF_REQUIRED"
REASON_ANCHOR_PARAGRAPH_MEDIA_CONFLICT: Final[str] = (
    "ANCHOR_PARAGRAPH_MEDIA_CONFLICT"
)
REASON_ANCHOR_MEDIA_REQUIRED: Final[str] = "ANCHOR_MEDIA_REQUIRED"
REASON_ANCHOR_MEDIA_PARAGRAPH_CONFLICT: Final[str] = (
    "ANCHOR_MEDIA_PARAGRAPH_CONFLICT"
)
REASON_ANCHOR_WHOLE_NOT_EMPTY: Final[str] = "ANCHOR_WHOLE_NOT_EMPTY"
REASON_MEDIA_ITEM_NOT_FOUND: Final[str] = "MEDIA_ITEM_NOT_FOUND"
REASON_MEDIA_ITEM_CONTENT_MISMATCH: Final[str] = "MEDIA_ITEM_CONTENT_MISMATCH"
REASON_CATEGORY_INVALID: Final[str] = "FEEDBACK_CATEGORY_INVALID"
REASON_SEVERITY_INVALID: Final[str] = "FEEDBACK_SEVERITY_INVALID"
REASON_SCOPE_INVALID: Final[str] = "FEEDBACK_SCOPE_INVALID"
REASON_AUTHOR_KIND_INVALID: Final[str] = "FEEDBACK_AUTHOR_KIND_INVALID"
REASON_ACTOR_KIND_INVALID: Final[str] = "FEEDBACK_ACTOR_KIND_INVALID"
REASON_BODY_INVALID: Final[str] = "FEEDBACK_BODY_INVALID"
REASON_SELECTED_TEXT_TOO_LONG: Final[str] = "FEEDBACK_SELECTED_TEXT_TOO_LONG"
REASON_REJECT_REASON_REQUIRED: Final[str] = "FEEDBACK_REJECT_REASON_REQUIRED"
REASON_REJECT_REASON_INVALID: Final[str] = "FEEDBACK_REJECT_REASON_INVALID"
REASON_STATE_INVALID: Final[str] = "FEEDBACK_STATE_INVALID"
REASON_DECISION_INVALID: Final[str] = "REVIEW_DECISION_INVALID"
REASON_REVIEW_REASON_INVALID: Final[str] = "REVIEW_REASON_INVALID"
REASON_UNRESOLVED_BLOCKING_FEEDBACK: Final[str] = (
    "UNRESOLVED_BLOCKING_FEEDBACK"
)

#: 锚点新鲜度（读时判定，不落库）。"STALE" 只表示锚点失效，
#: **不**表示这条反馈可以丢弃 —— 列表照常返回它（R5 的硬要求）。
ANCHOR_STATE_FRESH: Final[str] = "FRESH"
ANCHOR_STATE_STALE: Final[str] = "STALE"


# ---------------------------------------------------------------------------
# 返回值结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PreviewPayload:
    """预览页的聚合快照：当前版本 + 段落 + 图片位 + 标题候选 + 反馈计数。

    `etag` 与 `lock_version` 来自同一次 `read_content_edit_target`，
    保证"用户看到的正文"与"提交时要带的 If-Match"属于同一版。
    """

    content_id: uuid.UUID
    project_id: uuid.UUID
    current_version_id: uuid.UUID
    version_no: int
    lock_version: int
    etag: str
    title: str
    body: str
    tags: tuple[str, ...]
    bindings: tuple[dict[str, Any], ...]
    paragraphs: tuple[dict[str, Any], ...]
    media_items: tuple[dict[str, Any], ...]
    title_candidates: tuple[dict[str, Any], ...]
    feedback_count: int
    #: 自检摘要的**占位**：自检能力属于 WP30（`modules/review`），本波未开工。
    #: `build_preview` 不编造任何自检数据 —— 宁可为空，也不让前端展示
    #: 一份看起来"通过"的假摘要。
    self_check_summary: None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "contentId": str(self.content_id),
            "projectId": str(self.project_id),
            "currentVersionId": str(self.current_version_id),
            "versionNo": self.version_no,
            "lockVersion": self.lock_version,
            "etag": self.etag,
            "title": self.title,
            "body": self.body,
            "tags": list(self.tags),
            "bindings": list(self.bindings),
            "paragraphs": list(self.paragraphs),
            "mediaItems": list(self.media_items),
            "titleCandidates": list(self.title_candidates),
            "feedbackCount": self.feedback_count,
            "selfCheckSummary": self.self_check_summary,
        }


@dataclass(frozen=True, slots=True)
class FeedbackPage:
    """反馈列表的一页。`items` 里每条都带**读时判定**的 `anchorState`。"""

    items: tuple[dict[str, Any], ...]
    page: PageInfo


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------
def _invalid(
    reason: str, message: str, **details: Any
) -> ApiError:
    """构造一个可读的 422（字段/锚点校验失败）。

    统一走 `VALIDATION_FAILED`：这些错误都是"请求里的值与当前数据对不上"，
    与 409（状态机不允许）和 412（版本冲突）是不同处置 —— 客户端应改请求，
    而不是刷新重试。
    """
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        message,
        details={"reason": reason, **details},
    )


def _required_choice(
    value: str, *, allowed: frozenset[str], reason: str, field: str
) -> None:
    if value not in allowed:
        raise _invalid(
            reason,
            f"{field} 取值非法：{value!r}，允许 {sorted(allowed)}。",
            field=field,
        )


def _current_known_paragraph_refs(feedback_repo: FeedbackRepository,
                                  content_id: uuid.UUID) -> frozenset[str]:
    """当前正文的段落 ref 集合（切段交给 media_bridge，本包零切段实现）。

    `current_content_body` 是投影优先的读法（与 content 的编辑目标同源）。
    正文不存在或切不出段 ⇒ 空集合，调用方的 `validate_paragraph_ref`
    会一律拒绝 —— "正文为空"是真事故，不能当成"跳过校验"。
    """
    body = feedback_repo.current_content_body(content_id)
    if body is None:
        return frozenset()
    return frozenset(
        ref for _index, _text, ref in media_bridge.split_body_into_paragraphs(body)
    )


def _feedback_item_dict(
    item: ContentFeedbackItem, *, known_refs: frozenset[str]
) -> dict[str, Any]:
    """一行反馈 → 响应字典；段落锚点在这里**实时**判 FRESH / STALE。

    ## 为什么 STALE 不过滤、不落库

    正文改过之后，旧段落 ref 指不到任何当前段落。此时这条反馈仍然有历史
    价值（"当时有人提过这个意见"），所以**条数不能因判定而减少**——只打标记。
    也不在编辑时批量更新一批行：一次编辑扫全表反馈是昂贵的写放大，
    而且漏触发时标记会永久过期（静默错误）。读时判定的代价只是切一次段。
    """
    anchor_state = ANCHOR_STATE_FRESH
    if (
        item.anchor_kind == ANCHOR_KIND_PARAGRAPH
        and item.paragraph_ref not in known_refs
    ):
        anchor_state = ANCHOR_STATE_STALE
    return {
        "id": str(item.id),
        "contentItemId": str(item.content_item_id),
        "projectId": str(item.project_id),
        "observedVersionId": str(item.observed_version_id),
        "anchorKind": item.anchor_kind,
        "anchorState": anchor_state,
        "paragraphRef": item.paragraph_ref,
        "selectedText": item.selected_text,
        "mediaItemId": (
            None if item.media_item_id is None else str(item.media_item_id)
        ),
        "category": item.category,
        "suggestedCategory": item.suggested_category,
        "severity": item.severity,
        "scope": item.scope,
        "state": item.state,
        "authorKind": item.author_kind,
        "body": item.body,
        "rejectReason": item.reject_reason,
        "resolvedVersionId": (
            None
            if item.resolved_version_id is None
            else str(item.resolved_version_id)
        ),
        "createdBy": None if item.created_by is None else str(item.created_by),
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
        "lockVersion": int(item.lock_version),
    }


def _review_dict(review: ContentReview) -> dict[str, Any]:
    return {
        "id": str(review.id),
        "projectId": str(review.project_id),
        "contentItemId": str(review.content_item_id),
        "subjectVersionId": str(review.subject_version_id),
        "decision": review.decision,
        "reason": review.reason,
        "decidedBy": None if review.decided_by is None else str(review.decided_by),
        "decidedAt": (
            None if review.decided_at is None else review.decided_at.isoformat()
        ),
        "supersedesId": (
            None if review.supersedes_id is None else str(review.supersedes_id)
        ),
    }


# ---------------------------------------------------------------------------
# ① 预览聚合
# ---------------------------------------------------------------------------
def build_preview(
    feedback_repo: FeedbackRepository,
    content_repo: Any,
    version_repo: Any,
    baseline_repo: Any,
    *,
    content_id: uuid.UUID,
) -> PreviewPayload:
    """预览页所需的全部聚合数据，一次装配返回。

    ## 为什么预览要"一次装配"而不是端点查五次

    预览页的段落、图片位、标题候选、反馈计数必须对应**同一版正文**。
    若端点分五次查，中间可以插进一次编辑：段落来自旧版、ETag 来自新版，
    用户看到的内容与提交时的基线不一致。这里先取一次 `EditTarget`
    （身份侧 + 版本侧同一快照），其余数据都以它的 `current_version_id`
    与 `body` 为锚点展开。

    ## 自检摘要为空是刻意的

    自检能力属于 WP30（`modules/review`），本波尚未开工。这里只留字段位
    （`selfCheckSummary: None`），**不编造**任何"通过/风险"数据 ——
    一份看起来通过的假摘要比空字段危险得多。

    `version_repo` / `baseline_repo` 保留在签名里是为了与 `edit_content`
    共用同一套依赖装配（端点不必为两个用例准备不同的 repo 组合）；
    本函数只读，不触碰写入原语。
    """
    _ = (version_repo, baseline_repo)  # 只读用例，不需要它们；保留以统一装配签名。

    target = content_bridge.read_content_edit_target(
        content_repo, content_id=content_id
    )
    paragraphs = media_bridge.split_body_into_paragraphs(target.body)
    media_rows = feedback_repo.list_media_items_for_content(content_id)
    candidates = feedback_repo.list_title_candidates_for_version(
        target.current_version_id
    )
    feedback_count = feedback_repo.count_feedback_items(
        content_item_id=content_id
    )

    return PreviewPayload(
        content_id=target.content_id,
        project_id=target.project_id,
        current_version_id=target.current_version_id,
        version_no=target.version_no,
        lock_version=target.lock_version,
        # `EditTarget.etag` 就是 `content_edit_etag(item)` 的产物（同一函数），
        # 这里直接取快照里的值，避免为了重算 ETag 再读一次内容行。
        etag=target.etag,
        title=target.title,
        body=target.body,
        tags=target.tags,
        bindings=target.bindings,
        paragraphs=tuple(
            {"index": index, "text": text, "ref": ref}
            for index, text, ref in paragraphs
        ),
        media_items=tuple(
            {
                "mediaItemId": str(media_item_id),
                "paragraphRef": paragraph_ref,
                "sortOrder": sort_order,
                "state": state,
            }
            for media_item_id, paragraph_ref, sort_order, state in media_rows
        ),
        title_candidates=tuple(
            {
                "titleCandidateId": str(candidate_id),
                "title": title,
                "isAdopted": is_adopted,
            }
            for candidate_id, title, is_adopted in candidates
        ),
        feedback_count=feedback_count,
        self_check_summary=None,
    )


# ---------------------------------------------------------------------------
# ② 编辑追加版（纯转发）
# ---------------------------------------------------------------------------
def edit_content(
    content_repo: Any,
    *,
    version_repo: Any,
    baseline_repo: Any,
    content_id: uuid.UUID,
    expected_lock_version: int,
    title: str | None = None,
    body: str | None = None,
    tags: Sequence[str] | None = None,
    bindings: Sequence[Mapping[str, Any]] | None = None,
    created_by: uuid.UUID | None = None,
) -> ContentVersionSummary:
    """人工编辑 ⇒ 追加一个内容版本；**只转发，不重复任何并发判断**。

    ## 为什么这里没有 412 / 409 判断

    编辑保护的是 `content_items.lock_version`。权威判断在
    `content.service.append_edited_version`：它在**行锁内**读当前锁版本、
    在**任何写入之前**比对，并用 `details.currentEtag` 回带当前值。
    本层若再判一次，拿到的是锁外读数 —— 既挡不住并发，又可能与权威点
    在边界上给出不同的结论。转发是唯一的正确做法。

    ## 为什么编辑成功后不刷新反馈

    编辑与反馈是两张独立的表。段落反馈的"锚点是否还有效"由
    `list_feedback` 在**读时**判定（`anchorState`），不在编辑时刻批量更新：
    一次编辑扫全表反馈是昂贵的写放大，而"漏触发更新"会留下永久过期的
    标记（静默错误）。读时判定的成本只是切一次段。
    """
    return content_bridge.apply_content_edit(
        content_repo,
        version_repo=version_repo,
        baseline_repo=baseline_repo,
        content_id=content_id,
        expected_lock_version=expected_lock_version,
        title=title,
        body=body,
        tags=tags,
        bindings=bindings,
        created_by=created_by,
    )


# ---------------------------------------------------------------------------
# ③ 反馈列表
# ---------------------------------------------------------------------------
def list_feedback(
    feedback_repo: FeedbackRepository,
    *,
    content_id: uuid.UUID,
    page_params: PageParams | None = None,
    state: str | None = None,
    category: str | None = None,
    anchor_kind: str | None = None,
) -> FeedbackPage:
    """一条内容的反馈列表（新的在前），每条附 `anchorState`。

    ## R5：STALE 只打标记，不过滤

    正文改过之后，旧段落 ref 不再对应任何当前段落。这条反馈仍然要出现在
    列表里（"当时有人提过"是历史事实），只是标记为 `STALE` 让前端提示
    "锚点已随编辑失效"。因此这里的 `items` 条数与"锚点新鲜度"**无关**。

    ## 为什么用 keyset 而不是 offset

    `created_at` 是持续写入的排序键。`OFFSET` 的前提是"前 n 条没变"，
    而反馈会不断新增 —— 翻页时漏项或重复。游标绑定排序与筛选条件
    （`CursorSpec.filters`）：把"按 OPEN 筛选"的游标用到"按全部筛选"的
    查询上会被 400 拒绝，而不是静默返回错位的一页。

    ## 锚点基准是"当前正文"

    判 FRESH/STALE 用的是 `current_content_body`（投影优先，与 content 的
    编辑目标同源），**不是** `observed_version_id` —— 后者记录"提意见时看到
    哪一版"，用于展示与影响面分析，不用于判锚点现在是否还有效。
    """
    params = page_params or PageParams(limit=DEFAULT_LIMIT)
    filters = {
        "contentItemId": str(content_id),
        "state": state,
        "category": category,
        "anchorKind": anchor_kind,
    }
    spec = CursorSpec(
        sort_fields=("createdAt", "id"), direction="desc", filters=filters
    )

    before: tuple[datetime, uuid.UUID] | None = None
    if params.cursor:
        position = decode_cursor(params.cursor, spec)
        # 游标里的排序值是 JSON 字符串（`encode_cursor` 用 `default=str`），
        # 回到查询前恢复成 datetime / UUID。任何解析失败都视为非法游标（400）。
        try:
            before = (
                datetime.fromisoformat(str(position.sort_values[0])),
                uuid.UUID(position.item_id),
            )
        except (ValueError, TypeError, IndexError) as exc:
            raise ApiError(
                ErrorCode.MALFORMED_REQUEST,
                details={"reason": "cursor_malformed", "field": "cursor"},
            ) from exc

    rows = feedback_repo.list_feedback_items(
        content_item_id=content_id,
        state=state,
        category=category,
        anchor_kind=anchor_kind,
        before=before,
        limit=params.limit + 1,
    )
    has_more = len(rows) > params.limit
    page_rows = rows[: params.limit]

    next_cursor: str | None = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(
            spec,
            CursorPosition(
                sort_values=(last.created_at.isoformat(), str(last.id)),
                item_id=str(last.id),
            ),
        )

    known_refs = _current_known_paragraph_refs(feedback_repo, content_id)
    return FeedbackPage(
        items=tuple(
            _feedback_item_dict(item, known_refs=known_refs) for item in page_rows
        ),
        page=PageInfo(next_cursor=next_cursor, has_more=has_more),
    )


# ---------------------------------------------------------------------------
# ④ 新建反馈
# ---------------------------------------------------------------------------
def create_feedback(
    feedback_repo: FeedbackRepository,
    *,
    project_id: uuid.UUID,
    content_id: uuid.UUID,
    observed_version_id: uuid.UUID,
    anchor_kind: str,
    category: str,
    severity: str,
    scope: str,
    author_kind: str,
    body: str,
    paragraph_ref: str | None = None,
    selected_text: str | None = None,
    media_item_id: uuid.UUID | None = None,
    suggested_category: str | None = None,
    created_by: uuid.UUID | None = None,
) -> ContentFeedbackItem:
    """新建一条反馈，并在**同一事务**内追加它的首行事件。

    ## 锚点一致性为什么要在服务层先判一次

    `models.py` 的 CHECK 与 `ddl.py` 的触发器是最后一道（数据无论从哪条
    路径进来都逃不掉），但它们只在提交时以 23514 / 触发器错误冒出来：
    对用户是"500 或不可读的约束错误"，无法定位到字段。服务层这一道给出
    可读的 4xx（`details.reason` 机器可判）。**两层都要有**：
    · 服务层：可读、定位到字段；
    · DDL：兜住绕过本函数的写路径（脚本、数据修复）。

    ## 段落 ref 对着"当前正文"校验

    用 `current_content_body`（投影优先）而不是 `observed_version_id`：
    用户提意见时看到的就是当前这一版；拿旧版校验会让"刚改完正文就提意见"
    被误拒。校验本身交给 `media_bridge.resolve_paragraph_ref` —— 本包
    **零切段、零 ref 形状判断**（R2）。

    ## R6：首行事件与行本身同一事务

    `content_feedback_items.state` 是投影，`content_feedback_events` 是历史。
    首行事件 `from_state=None`（"从没有状态来到 OPEN"）。两者都在调用方的
    同一个事务里，本函数只 `flush()` 不 `commit()`：分开提交会造出
    "有反馈行但没有创建事件"的溯源断档。
    """
    _required_choice(
        anchor_kind, allowed=ANCHOR_KINDS, reason=REASON_ANCHOR_KIND_INVALID,
        field="anchorKind",
    )
    _required_choice(
        category, allowed=FEEDBACK_CATEGORIES, reason=REASON_CATEGORY_INVALID,
        field="category",
    )
    if suggested_category is not None:
        _required_choice(
            suggested_category,
            allowed=FEEDBACK_CATEGORIES,
            reason=REASON_CATEGORY_INVALID,
            field="suggestedCategory",
        )
    _required_choice(
        severity, allowed=FEEDBACK_SEVERITIES, reason=REASON_SEVERITY_INVALID,
        field="severity",
    )
    _required_choice(
        scope, allowed=FEEDBACK_SCOPES, reason=REASON_SCOPE_INVALID, field="scope"
    )
    _required_choice(
        author_kind, allowed=ACTOR_KINDS, reason=REASON_AUTHOR_KIND_INVALID,
        field="authorKind",
    )

    if not isinstance(body, str) or not body.strip():
        raise _invalid(
            REASON_BODY_INVALID, "反馈正文不能为空。", field="body"
        )
    if len(body) > MAX_FEEDBACK_BODY_CHARS:
        raise _invalid(
            REASON_BODY_INVALID,
            f"反馈正文超长：{len(body)} > {MAX_FEEDBACK_BODY_CHARS}。",
            field="body",
        )
    if selected_text is not None and len(selected_text) > MAX_SELECTED_TEXT_CHARS:
        raise _invalid(
            REASON_SELECTED_TEXT_TOO_LONG,
            f"选中原文超长：{len(selected_text)} > {MAX_SELECTED_TEXT_CHARS}。",
            field="selectedText",
        )

    # --- 锚点三选一：列占用必须与 anchor_kind 对得上 -----------------------
    if anchor_kind == ANCHOR_KIND_PARAGRAPH:
        if not paragraph_ref:
            raise _invalid(
                REASON_ANCHOR_PARAGRAPH_REF_REQUIRED,
                "段落锚点必须给出 paragraphRef。",
                field="paragraphRef",
            )
        if media_item_id is not None:
            raise _invalid(
                REASON_ANCHOR_PARAGRAPH_MEDIA_CONFLICT,
                "段落锚点不能同时给 mediaItemId（锚点只能落在三处之一）。",
                field="mediaItemId",
            )
        known_refs = _current_known_paragraph_refs(feedback_repo, content_id)
        # 校验不过抛 media 的 SchemaViolation（PARAGRAPH_REF_UNKNOWN），
        # 由端点层按 reason 映射 4xx —— 桥接层不改写异常类型。
        media_bridge.resolve_paragraph_ref(
            paragraph_ref, known_paragraph_refs=known_refs
        )
    elif anchor_kind == ANCHOR_KIND_MEDIA:
        if media_item_id is None:
            raise _invalid(
                REASON_ANCHOR_MEDIA_REQUIRED,
                "媒体锚点必须给出 mediaItemId。",
                field="mediaItemId",
            )
        if paragraph_ref is not None:
            raise _invalid(
                REASON_ANCHOR_MEDIA_PARAGRAPH_CONFLICT,
                "媒体锚点不能同时给 paragraphRef（锚点只能落在三处之一）。",
                field="paragraphRef",
            )
        owner = feedback_repo.media_item_content_id(media_item_id)
        if owner is None:
            raise _invalid(
                REASON_MEDIA_ITEM_NOT_FOUND,
                f"媒体位 {media_item_id} 不存在。",
                field="mediaItemId",
            )
        if owner != content_id:
            raise _invalid(
                REASON_MEDIA_ITEM_CONTENT_MISMATCH,
                (
                    f"媒体位 {media_item_id} 属于内容 {owner}，"
                    f"不属于 {content_id}。媒体锚点只能指向同一条内容里的图。"
                ),
                field="mediaItemId",
            )
    else:  # ANCHOR_KIND_WHOLE
        if paragraph_ref is not None or media_item_id is not None:
            raise _invalid(
                REASON_ANCHOR_WHOLE_NOT_EMPTY,
                "整篇锚点不能给 paragraphRef 或 mediaItemId。",
            )

    # 主键在 Python 侧先定（与 `brief/service.py`、`exports/service.py` 同款）：
    # 这样"行 + 首行事件"可以在同一次 flush 里写入，不需要为了拿
    # `gen_random_uuid()` 的返回值先 flush 一次（两次 flush 虽仍同事务，
    # 但会让"两条写入是一次原子落库"这件事变得不明显）。
    feedback_item_id = uuid.uuid4()
    item = feedback_repo.insert_feedback_item(
        id=feedback_item_id,
        project_id=project_id,
        content_item_id=content_id,
        observed_version_id=observed_version_id,
        anchor_kind=anchor_kind,
        paragraph_ref=paragraph_ref,
        selected_text=selected_text,
        media_item_id=media_item_id,
        category=category,
        suggested_category=suggested_category,
        severity=severity,
        scope=scope,
        state=FEEDBACK_STATE_OPEN,
        author_kind=author_kind,
        body=body,
        reject_reason=None,
        resolved_version_id=None,
        created_by=created_by,
    )

    feedback_repo.insert_feedback_event(
        feedback_item_id=feedback_item_id,
        from_state=None,
        to_state=FEEDBACK_STATE_OPEN,
        # 首行事件的执行者就是作者本人（人提的 / AI 提的）。
        actor_kind=author_kind,
        actor_id=created_by,
        note=None,
        resulting_version_id=None,
    )
    # 一次 flush：反馈行与首行事件同时落库（同一事务，提交边界归调用方）。
    feedback_repo.flush()
    return item


# ---------------------------------------------------------------------------
# ⑤ 推进反馈状态
# ---------------------------------------------------------------------------
def advance_feedback(
    feedback_repo: FeedbackRepository,
    *,
    feedback_id: uuid.UUID,
    expected_lock_version: int,
    to_state: str,
    actor_kind: str,
    note: str | None = None,
    reject_reason: str | None = None,
    resulting_version_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
) -> ContentFeedbackItem:
    """推进一条反馈的状态，并在**同一事务**内追加迁移事件。

    ## 本层自己的 412（与 `edit_content` 的那把锁不同）

    这里保护的是 `content_feedback_items.lock_version`。读行之后**立即**
    比对 `expected_lock_version`，不等到条件 UPDATE 才发现 —— 与
    `content.service.append_edited_version` 的"锁内读、写前判"同款。
    这样 412 的 `currentEtag` 用的是库里读到的真值，而不是客户端传来的旧值。

    条件 UPDATE 返回 0 行时再兜一道：理论上读锁之后写之前不该被抢先
    （读已发生、同一事务），但那意味着投影被别的路径绕过本函数改过；
    仍按 412 处置（客户端需要"重新读一次"，而不是"当前不允许"）。

    ## REJECTED 必须带理由（服务层先挡，别等 DB CHECK）

    `models.py` 有双向 CHECK：`(state='REJECTED') = (reject_reason IS NOT NULL)`。
    依赖它报错只能得到一条 23514，对用户不可读、也无法定位字段。
    因此这里先给出 422；非 REJECTED 时把 `reject_reason` 归一为 `None`
    （列是"当前值"，从上一次拒绝迁移到别的状态时必须清空理由）。

    ## R6：事件与状态同一事务

    `advance_feedback_state` 的条件 UPDATE 与 `insert_feedback_event` 之间
    没有提交边界；本函数只 `flush()`。任一步失败，状态与历史一起回滚。
    """
    _required_choice(
        to_state, allowed=FEEDBACK_STATES, reason=REASON_STATE_INVALID,
        field="state",
    )
    _required_choice(
        actor_kind, allowed=ACTOR_KINDS, reason=REASON_ACTOR_KIND_INVALID,
        field="actorKind",
    )

    if to_state == FEEDBACK_STATE_REJECTED:
        if not reject_reason or not reject_reason.strip():
            raise _invalid(
                REASON_REJECT_REASON_REQUIRED,
                "拒绝反馈时必须给出理由。",
                field="rejectReason",
            )
        if len(reject_reason) > MAX_REJECT_REASON_CHARS:
            raise _invalid(
                REASON_REJECT_REASON_INVALID,
                (
                    f"拒绝理由超长：{len(reject_reason)} > "
                    f"{MAX_REJECT_REASON_CHARS}。"
                ),
                field="rejectReason",
            )
    else:
        # CHECK 要求"不是 REJECTED 就没有理由"。这里归一而不是拒绝：
        # 客户端从"拒绝"改判到"进行中"时，旧理由不该挡住迁移。
        reject_reason = None

    current = feedback_repo.get_feedback_item(feedback_id)
    if current is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"反馈 {feedback_id} 不存在。",
            details={"resource": "feedbackItem", "resourceId": str(feedback_id)},
        )

    current_lock_version = int(current.lock_version)
    if expected_lock_version != current_lock_version:
        raise version_conflict(
            resource_id=feedback_id,
            current_version=current_lock_version,
            reason=REASON_FEEDBACK_LOCK_VERSION_MISMATCH,
            message="该意见已被更新，请重新读取最新版本后再提交。",
        )

    from_state = current.state
    affected = feedback_repo.advance_feedback_state(
        feedback_item_id=feedback_id,
        expected_lock_version=expected_lock_version,
        to_state=to_state,
        reject_reason=reject_reason,
        resolved_version_id=resulting_version_id,
    )
    if affected == 0:
        # 兜底（见 docstring）：读与写之间被并发抢先，仍按协议回 412，
        # `currentEtag` 用重新读到的真值。
        reloaded = feedback_repo.reload_feedback_item(feedback_id)
        current_version = (
            int(reloaded.lock_version)
            if reloaded is not None
            else current_lock_version
        )
        raise version_conflict(
            resource_id=feedback_id,
            current_version=current_version,
            reason=REASON_FEEDBACK_LOCK_VERSION_MISMATCH,
            message="该意见已被更新，请重新读取最新版本后再提交。",
        )

    feedback_repo.insert_feedback_event(
        feedback_item_id=feedback_id,
        from_state=from_state,
        to_state=to_state,
        actor_kind=actor_kind,
        actor_id=actor_id,
        note=note,
        resulting_version_id=resulting_version_id,
    )
    feedback_repo.flush()

    # 批量 UPDATE 用了 `synchronize_session=False`，session 里的旧对象仍带旧状态；
    # 必须显式重读，否则返回值会让调用方以为状态没变。
    reloaded = feedback_repo.reload_feedback_item(feedback_id)
    if reloaded is None:  # pragma: no cover - 同一事务内刚更新过的行不可能消失
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"反馈 {feedback_id} 不存在。",
            details={"resource": "feedbackItem", "resourceId": str(feedback_id)},
        )
    return reloaded


# ---------------------------------------------------------------------------
# ⑥ 单条审核
# ---------------------------------------------------------------------------
def decide_review(
    feedback_repo: FeedbackRepository,
    *,
    project_id: uuid.UUID,
    content_item_id: uuid.UUID,
    subject_version_id: uuid.UUID,
    decision: str,
    reason: str,
    decided_by: uuid.UUID | None = None,
    supersedes_id: uuid.UUID | None = None,
) -> ContentReview:
    """记录一次审核决定（通过 / 毙掉）。

    ## APPROVED 的前置校验：未解决的 BLOCKING 反馈

    "存在未解决的阻断事实问题"时不允许标记通过。判据下推到 SQL
    （`severity=BLOCKING` 且 `state NOT IN (RESOLVED, REJECTED)`），
    而不是先拉全量再在 Python 里过滤：窗口一旦被已解决项占满，
    真正的阻断项会被漏判 —— 那是"审核通过了但其实有硬错"的静默事故。

    报 **409 `INVALID_TRANSITION`**（不是 422/412）：这不是请求格式错，
    也不是版本过期，而是"当前数据状态不允许这个决定"。先处理阻断项再来。

    ## 为什么审核行不可变、改判靠 `supersedes_id`

    与 `content_versions` 同一条纪律：审核历史要能溯源。改判是追加一行
    并指回被取代的决定，不是原地改 `decision`。这里可选地转发
    `supersedes_id`，但新旧两行都由调用方在**同一事务**里安排。
    """
    _required_choice(
        decision, allowed=REVIEW_DECISIONS, reason=REASON_DECISION_INVALID,
        field="decision",
    )
    if not isinstance(reason, str) or not reason.strip():
        raise _invalid(
            REASON_REVIEW_REASON_INVALID, "审核理由不能为空。", field="reason"
        )

    if decision == REVIEW_DECISION_APPROVED:
        blocking = feedback_repo.list_feedback_items(
            content_item_id=content_item_id,
            severity=FEEDBACK_SEVERITY_BLOCKING,
            exclude_states=(FEEDBACK_STATE_RESOLVED, FEEDBACK_STATE_REJECTED),
            limit=1,
        )
        if blocking:
            raise ApiError(
                ErrorCode.INVALID_TRANSITION,
                "存在未解决的阻断事实问题，无法标记已通过。",
                details={
                    "reason": REASON_UNRESOLVED_BLOCKING_FEEDBACK,
                    "contentItemId": str(content_item_id),
                    "blockingFeedbackIds": [str(row.id) for row in blocking],
                },
            )

    review = feedback_repo.insert_review(
        project_id=project_id,
        content_item_id=content_item_id,
        subject_version_id=subject_version_id,
        decision=decision,
        reason=reason,
        decided_by=decided_by,
        supersedes_id=supersedes_id,
    )
    feedback_repo.flush()
    return review


# ---------------------------------------------------------------------------
# ⑦ 批量审核
# ---------------------------------------------------------------------------
def decide_reviews_batch(
    feedback_repo: FeedbackRepository,
    *,
    project_id: uuid.UUID,
    content_item_ids: Sequence[uuid.UUID],
    decision: str,
    reason: str,
    decided_by: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """批量审核：**逐条独立判定、不整批失败**。

    ## 为什么每条一个 SAVEPOINT

    若整批共用一个事务且不带 savepoint，第 3 条因"有 BOOTCKING 反馈"抛错时，
    调用方为了继续处理第 4 条必须吞掉异常 —— 但 PostgreSQL 在语句报错后
    会把整个事务标记为 aborted，后续任何语句都失败。`begin_nested()`
    把每条包进子事务：失败只回滚那一条，前两条的写入与后两条的处理都不受影响。

    ## 为什么返回逐条结果而不是抛第一个错

    批量操作的价值是"一次处理多条"；若因一条不合规就整批回滚，运营要
    逐条重试并自己找出哪条坏了。逐条结果让前端直接展示"哪几条成功、
    哪几条为什么失败"，且**不整批失败**是 WP29 的硬性要求。

    ## 事务边界

    成功的条目留在**外层事务**里（savepoint 释放即并入外层），最终由
    调用方统一提交。本函数不 commit。
    """
    results: list[dict[str, Any]] = []
    for content_item_id in content_item_ids:
        try:
            # 子事务：本条失败只回滚本条，不污染外层事务。
            with feedback_repo.session.begin_nested():
                # 审核结论必须绑定版本。批量请求只给内容 id，版本取"当前版本"
                # （投影优先，见 repository.current_content_version_id 的说明）。
                subject_version_id = feedback_repo.current_content_version_id(
                    content_item_id
                )
                if subject_version_id is None:
                    raise ApiError(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"内容 {content_item_id} 还没有任何版本，无法审核。",
                        details={
                            "resource": "contentVersion",
                            "contentItemId": str(content_item_id),
                        },
                    )
                review = decide_review(
                    feedback_repo,
                    project_id=project_id,
                    content_item_id=content_item_id,
                    subject_version_id=subject_version_id,
                    decision=decision,
                    reason=reason,
                    decided_by=decided_by,
                )
            results.append(
                {
                    "contentItemId": str(content_item_id),
                    "ok": True,
                    "review": _review_dict(review),
                }
            )
        except Exception as exc:  # noqa: BLE001 - 逐条独立判定是硬性要求
            results.append(
                {
                    "contentItemId": str(content_item_id),
                    "ok": False,
                    "error": str(exc),
                }
            )
    return results


__all__ = [
    "ANCHOR_STATE_FRESH",
    "ANCHOR_STATE_STALE",
    "FeedbackPage",
    "PreviewPayload",
    "advance_feedback",
    "build_preview",
    "create_feedback",
    "decide_review",
    "decide_reviews_batch",
    "edit_content",
    "list_feedback",
]
