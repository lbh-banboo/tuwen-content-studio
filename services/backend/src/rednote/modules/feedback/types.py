"""WP29 反馈定位与审核决定的取值域、边界（新建模块的活常量）。

本文件**只放常量**，不 import 任何 `rednote.modules.*`
（平级模块不可互 import；跨模块取数走 `repository.py` 的裸 SQL）。
分层是单向的：`types ← models / repository / schemas / service`。

## 为什么取值域是这些

| 常量族 | 取值 | 依据 |
|---|---|---|
| `ANCHOR_KIND_*` | `WHOLE` / `PARAGRAPH` / `MEDIA` | 反馈锚点只能落在"整篇 / 某段 / 某张图"三处；三者的列占用互斥由 CHECK 表达 |
| `FEEDBACK_CATEGORY_*` | 8 类 | 反馈分类是**受控取值**：未知分类拒绝而不是落库，否则统计口径会随调用方漂移 |
| `FEEDBACK_SEVERITY_*` | `BLOCKING` / `WARNING` / `INFO` | 审核阻塞级的唯一判据 |
| `FEEDBACK_SCOPE_*` | `CURRENT_ONLY` / `FUTURE_GENERATIONS` | 这条意见只改当前稿，还是要沉淀到后续生成 |
| `FEEDBACK_STATE_*` | `OPEN` / `IN_PROGRESS` / `RESOLVED` / `REJECTED` | 反馈项的状态机；迁移历史落在 `content_feedback_events` |
| `ACTOR_KIND_*` | `USER` / `AI` | 谁提的/谁改的/谁审的 —— 人与 AI 都要能记 |
| `REVIEW_DECISION_*` | `APPROVED` / `ELIMINATED` | 审核只有"过"与"毙"两态；`supersedes_id` 表达改判 |

## 边界常量的来源

`MAX_PARAGRAPH_REF_CHARS` 与 `media/types.py` **同值、各写一份**
（平级不可互 import）。它决定 `paragraph_ref` 的列宽；两处漂移会让
"media 生成的 ref 插不进 feedback 列"，一致性由测试对账。
`MAX_SELECTED_TEXT_CHARS` / `MAX_FEEDBACK_BODY_CHARS` 是 CHECK 的边界，
`MAX_REJECT_REASON_CHARS` 是 REJECTED 必填理由的列宽。
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# 锚点粒度（三选一，列占用互斥）
# ---------------------------------------------------------------------------
ANCHOR_KIND_WHOLE: Final[str] = "WHOLE"
ANCHOR_KIND_PARAGRAPH: Final[str] = "PARAGRAPH"
ANCHOR_KIND_MEDIA: Final[str] = "MEDIA"

ANCHOR_KINDS: Final[frozenset[str]] = frozenset(
    {ANCHOR_KIND_WHOLE, ANCHOR_KIND_PARAGRAPH, ANCHOR_KIND_MEDIA}
)
ANCHOR_KIND_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(ANCHOR_KINDS)
)

# ---------------------------------------------------------------------------
# 反馈分类（8 类）
# ---------------------------------------------------------------------------
FEEDBACK_CATEGORY_FACT: Final[str] = "FACT"
FEEDBACK_CATEGORY_TONE: Final[str] = "TONE"
FEEDBACK_CATEGORY_STRUCTURE: Final[str] = "STRUCTURE"
FEEDBACK_CATEGORY_SCENE: Final[str] = "SCENE"
FEEDBACK_CATEGORY_DUPLICATE: Final[str] = "DUPLICATE"
FEEDBACK_CATEGORY_MEDIA_CONSISTENCY: Final[str] = "MEDIA_CONSISTENCY"
FEEDBACK_CATEGORY_TECH_FORMAT: Final[str] = "TECH_FORMAT"
FEEDBACK_CATEGORY_OTHER: Final[str] = "OTHER"

FEEDBACK_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        FEEDBACK_CATEGORY_FACT,
        FEEDBACK_CATEGORY_TONE,
        FEEDBACK_CATEGORY_STRUCTURE,
        FEEDBACK_CATEGORY_SCENE,
        FEEDBACK_CATEGORY_DUPLICATE,
        FEEDBACK_CATEGORY_MEDIA_CONSISTENCY,
        FEEDBACK_CATEGORY_TECH_FORMAT,
        FEEDBACK_CATEGORY_OTHER,
    }
)
FEEDBACK_CATEGORY_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(FEEDBACK_CATEGORIES)
)

# ---------------------------------------------------------------------------
# 严重度
# ---------------------------------------------------------------------------
FEEDBACK_SEVERITY_BLOCKING: Final[str] = "BLOCKING"
FEEDBACK_SEVERITY_WARNING: Final[str] = "WARNING"
FEEDBACK_SEVERITY_INFO: Final[str] = "INFO"

FEEDBACK_SEVERITIES: Final[frozenset[str]] = frozenset(
    {
        FEEDBACK_SEVERITY_BLOCKING,
        FEEDBACK_SEVERITY_WARNING,
        FEEDBACK_SEVERITY_INFO,
    }
)
FEEDBACK_SEVERITY_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(FEEDBACK_SEVERITIES)
)

# ---------------------------------------------------------------------------
# 作用域
# ---------------------------------------------------------------------------
FEEDBACK_SCOPE_CURRENT_ONLY: Final[str] = "CURRENT_ONLY"
FEEDBACK_SCOPE_FUTURE_GENERATIONS: Final[str] = "FUTURE_GENERATIONS"

FEEDBACK_SCOPES: Final[frozenset[str]] = frozenset(
    {FEEDBACK_SCOPE_CURRENT_ONLY, FEEDBACK_SCOPE_FUTURE_GENERATIONS}
)
FEEDBACK_SCOPE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(FEEDBACK_SCOPES)
)

# ---------------------------------------------------------------------------
# 反馈项状态机
# ---------------------------------------------------------------------------
FEEDBACK_STATE_OPEN: Final[str] = "OPEN"
FEEDBACK_STATE_IN_PROGRESS: Final[str] = "IN_PROGRESS"
FEEDBACK_STATE_RESOLVED: Final[str] = "RESOLVED"
FEEDBACK_STATE_REJECTED: Final[str] = "REJECTED"

FEEDBACK_STATES: Final[frozenset[str]] = frozenset(
    {
        FEEDBACK_STATE_OPEN,
        FEEDBACK_STATE_IN_PROGRESS,
        FEEDBACK_STATE_RESOLVED,
        FEEDBACK_STATE_REJECTED,
    }
)
FEEDBACK_STATE_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(FEEDBACK_STATES)
)
#: 新建反馈项的初始状态（表的 `server_default`）。
FEEDBACK_STATE_INITIAL: Final[str] = FEEDBACK_STATE_OPEN

# ---------------------------------------------------------------------------
# 行为主体（反馈作者 / 状态迁移执行者 / 审核人）
# ---------------------------------------------------------------------------
ACTOR_KIND_USER: Final[str] = "USER"
ACTOR_KIND_AI: Final[str] = "AI"

ACTOR_KINDS: Final[frozenset[str]] = frozenset({ACTOR_KIND_USER, ACTOR_KIND_AI})
ACTOR_KIND_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(ACTOR_KINDS)
)

# ---------------------------------------------------------------------------
# 审核决定
# ---------------------------------------------------------------------------
REVIEW_DECISION_APPROVED: Final[str] = "APPROVED"
REVIEW_DECISION_ELIMINATED: Final[str] = "ELIMINATED"

REVIEW_DECISIONS: Final[frozenset[str]] = frozenset(
    {REVIEW_DECISION_APPROVED, REVIEW_DECISION_ELIMINATED}
)
REVIEW_DECISION_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(REVIEW_DECISIONS)
)

# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------
#: 与 `media/types.MAX_PARAGRAPH_REF_CHARS` 同值（各写一份，见模块 docstring）。
MAX_PARAGRAPH_REF_CHARS: Final[int] = 200
MAX_SELECTED_TEXT_CHARS: Final[int] = 4000
MAX_FEEDBACK_BODY_CHARS: Final[int] = 2000
MAX_REJECT_REASON_CHARS: Final[int] = 500

# ---------------------------------------------------------------------------
# 并发冲突的 reason（412 的 `details.reason`）
# ---------------------------------------------------------------------------
#: 反馈行的乐观锁不匹配。**为什么单独一个常量**：412 的 `reason` 是前端
#: 机器可判的契约（"内容锁过期"与"反馈锁过期"要能分开），而 `details.reason`
#: 一旦写成自由字符串，拼写漂移不会被任何判据抓到。
REASON_FEEDBACK_LOCK_VERSION_MISMATCH: Final[str] = "FEEDBACK_LOCK_VERSION_MISMATCH"

__all__ = [
    "ACTOR_KINDS",
    "ACTOR_KIND_AI",
    "ACTOR_KIND_LIST_SQL",
    "ACTOR_KIND_USER",
    "ANCHOR_KINDS",
    "ANCHOR_KIND_LIST_SQL",
    "ANCHOR_KIND_MEDIA",
    "ANCHOR_KIND_PARAGRAPH",
    "ANCHOR_KIND_WHOLE",
    "FEEDBACK_CATEGORIES",
    "FEEDBACK_CATEGORY_DUPLICATE",
    "FEEDBACK_CATEGORY_FACT",
    "FEEDBACK_CATEGORY_LIST_SQL",
    "FEEDBACK_CATEGORY_MEDIA_CONSISTENCY",
    "FEEDBACK_CATEGORY_OTHER",
    "FEEDBACK_CATEGORY_SCENE",
    "FEEDBACK_CATEGORY_STRUCTURE",
    "FEEDBACK_CATEGORY_TECH_FORMAT",
    "FEEDBACK_CATEGORY_TONE",
    "FEEDBACK_SCOPES",
    "FEEDBACK_SCOPE_CURRENT_ONLY",
    "FEEDBACK_SCOPE_FUTURE_GENERATIONS",
    "FEEDBACK_SCOPE_LIST_SQL",
    "FEEDBACK_SEVERITIES",
    "FEEDBACK_SEVERITY_BLOCKING",
    "FEEDBACK_SEVERITY_INFO",
    "FEEDBACK_SEVERITY_LIST_SQL",
    "FEEDBACK_SEVERITY_WARNING",
    "FEEDBACK_STATES",
    "FEEDBACK_STATE_INITIAL",
    "FEEDBACK_STATE_IN_PROGRESS",
    "FEEDBACK_STATE_LIST_SQL",
    "FEEDBACK_STATE_OPEN",
    "FEEDBACK_STATE_REJECTED",
    "FEEDBACK_STATE_RESOLVED",
    "MAX_FEEDBACK_BODY_CHARS",
    "MAX_PARAGRAPH_REF_CHARS",
    "MAX_REJECT_REASON_CHARS",
    "MAX_SELECTED_TEXT_CHARS",
    "REASON_FEEDBACK_LOCK_VERSION_MISMATCH",
    "REVIEW_DECISIONS",
    "REVIEW_DECISION_APPROVED",
    "REVIEW_DECISION_ELIMINATED",
    "REVIEW_DECISION_LIST_SQL",
]
