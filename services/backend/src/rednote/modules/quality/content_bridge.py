"""`quality` → `content` 的接缝（WP33，只读）：取「当前版本」及其可追溯上下文。

**当前版本口径（契约 §2）：`current_version_id` 投影优先；投影为 NULL 才回落末版。禁以
`ORDER BY version_no DESC` 作主路径。** 注意 `content.service.read_edit_target`（接缝冻结 §5.12）
的实现是 `latest_version`（按 `version_no` 降序），**并不看投影**——在「投影指向 v1、最大版本号是 v2」
的回滚态下它会取到 v2。所以本桥：

1. 先用 content 自己的仓储读投影：`item.current_version_id` 非空 ⇒ `get_version(...)`；
2. 投影为空才走 `read_edit_target`（其回落即「末版」口径；零版本抛 `REASON_CONTENT_VERSION_NOT_FOUND`）。

不自写第二份「当前版本」SQL；全部经 `ContentRepository`。零版本返回 `None`（⇒ `EXCLUDED(NO_VERSION)`，
不是抛错，也不是按 0 分计）。`baseline_id` 取自被钉死版本的 `generation_context_id`（NULL = 无上下文）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from rednote.modules.content.repository import ContentRepository
from rednote.modules.content.service import ContentGenerationError, read_edit_target
from rednote.modules.content.types import REASON_CONTENT_VERSION_NOT_FOUND


@dataclass(frozen=True)
class CurrentSubject:
    """某篇内容被钉死的当前版本（同一次读取）。"""

    content_version_id: uuid.UUID
    title: str
    body: str
    generation_context_id: uuid.UUID | None
    baseline_id: uuid.UUID | None


def read_current_subject(session: Any, *, content_id: uuid.UUID) -> CurrentSubject | None:
    repo = ContentRepository(session)
    item = repo.get_content_item(content_id)
    if item is None:
        return None
    if item.current_version_id is not None:
        version = repo.get_version(item.current_version_id)
        if version is None:  # 不可达（FK 保证），不静默回落
            raise RuntimeError(f"内容 {content_id} 的投影指向不存在的版本")
        version_id, title, body, context_id = (
            version.id,
            version.title,
            version.body,
            version.generation_context_id,
        )
    else:
        try:
            target = read_edit_target(repo, content_id=content_id)
        except ContentGenerationError as exc:
            if exc.reason == REASON_CONTENT_VERSION_NOT_FOUND:
                return None
            raise
        version_id, title, body, context_id = (
            target.current_version_id,
            target.title,
            target.body,
            target.generation_context_id,
        )
    baseline_id: uuid.UUID | None = None
    if context_id is not None:
        context = repo.get_generation_context(context_id)
        baseline_id = None if context is None else context.baseline_id
    return CurrentSubject(version_id, title, body, context_id, baseline_id)


def current_version_id_of(session: Any, *, content_id: uuid.UUID) -> uuid.UUID | None:
    """STALE 判定用：同口径取当前版本 id（无版本 ⇒ `None`）。"""
    subject = read_current_subject(session, content_id=content_id)
    return None if subject is None else subject.content_version_id


__all__ = ["CurrentSubject", "current_version_id_of", "read_current_subject"]
