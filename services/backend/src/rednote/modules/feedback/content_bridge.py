"""`feedback` → `content` 的唯一接缝（WP29）。

## 为什么必须有一个桥接文件

`content` 与 `feedback` 是**平级**业务模块，本仓的分层纪律是
"平级模块不可互 import 对方的 ORM 模型 / 写入原语"。反馈功能需要
"读当前编辑快照"和"人工编辑落成新版本"，这两件事的权威实现都在
`content.service` 里；若反馈模块自己拼 SQL 或自己调版本原语，
就会长出**第二份**并发判断与版本注册逻辑 —— 两份迟早分叉，
而分叉的表现是"一条路径 412、另一条静默覆盖"。

因此本文件只做三件事：**转发参数、转发返回值、不写业务逻辑**。
所有 412 / 409 / 状态一致性判断都留在 `content.service` 里，
`api.py` 层统一接住它们。

## 为什么不在这里 catch `ApiError`

`append_edited_version` 在锁过期时抛 `platform.concurrency.version_conflict`
构造的 412 `ApiError`。桥接层若把它包成别的异常，`api.py` 的
`details.currentEtag` 就会丢（前端无法"不重新 GET 就重试"）。
同理，桥接层也不应把 `ContentGenerationError` 改写成另一种错误 ——
"内容不存在 / 无版本"的语义映射只有一个地方（端点层）。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from rednote.modules.content.service import (
    append_edited_version,
    content_edit_etag,
    read_edit_target,
)
from rednote.modules.content.types import ContentVersionSummary, EditTarget


def read_content_edit_target(
    content_repo: Any, *, content_id: uuid.UUID
) -> EditTarget:
    """读编辑目标快照（当前版本 + ETag），**逐字转发**给 `content.service`。

    为什么不让反馈端点自己查两次：`EditTarget` 的价值在于"身份侧与版本侧
    同一次读取"，自己拼装会在两次读之间插进别人的写入，导致展示的正文
    与 ETag 不属于同一版。装配点只能有一个。
    """
    return read_edit_target(content_repo, content_id=content_id)


def build_content_etag(item: Any) -> str:
    """由内容行构造 ETag，**逐字转发** `content.service.content_edit_etag`。

    为什么不在反馈层手拼 `f'"{id}:{version}"'`：那是 `build_etag` 的一份
    会分叉的副本，而分叉的症状是"服务端签发的 ETag 自己校验不过"。
    """
    return content_edit_etag(item)


def apply_content_edit(
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
    """人工编辑落成**新版本**，**逐字转发** `content.service.append_edited_version`。

    ## 为什么反馈层不重复并发判断

    编辑的乐观锁保护的是 `content_items.lock_version`，判断点必须在
    "行锁内读 → 立即比 → 条件 UPDATE"这三步所在的函数里。反馈层再写一遍
    `expected != current` 只会拿到一个**锁外**的读数：既挡不住并发，
    又让两处判断在边界上出现分歧。这里只转发，让 412 从唯一权威处抛出。

    ## 为什么编辑成功后不批量刷新反馈状态

    反馈行的 `state` 与内容版本是两张独立的表。编辑后"某条段落反馈的锚点
    还在不在"是**读时**判定的（`service.list_feedback` 的 `anchorState`），
    不需要在这里扫一遍 `content_feedback_items`。读时判定避免了
    "一次编辑扫全表反馈"的昂贵写放大，也不会因为漏触发而留下过期标记。
    """
    return append_edited_version(
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


__all__ = [
    "apply_content_edit",
    "build_content_etag",
    "read_content_edit_target",
]
