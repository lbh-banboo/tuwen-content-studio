"""`review` → `content` 的唯一接缝（WP30）。

本包需要的内容侧事实只有一件：**被判的那一版是什么样**（身份侧 +
版本侧同一快照）。权威装配点是 `content.service.read_edit_target`
（接缝冻结 §5.12 已登记、已入库、签名已冻结）。

## 为什么不让 `repository.py` 自己拼这次查询

`EditTarget` 的价值在于"身份侧（`project_id` / `lock_version`）与版本侧
（`title` / `body` / `tags` / `bindings` / `blueprint_id`）来自**同一次读取**"。
自己拼装会在两次读之间插进别人的写入 ⇒ 这一轮自检的段落坐标来自旧版、
`subject_version_id` 来自新版，于是 finding 全部指向错误的段。
跨快照拼装的禁令见 `M3并行开发接缝冻结.md:607-613`（WP29 的 R7 同款）。

## 为什么不在这里 catch

`read_edit_target` 在"内容不存在 / 一版都没有"时抛
`content.service.ContentGenerationError`（`ValueError` 族，带 `reason`）。
桥接层把它包成别的异常，`api.py` 就无法按 `reason` 分流 404 / 409 ——
那个映射表只应该有一处（端点层）。

本包**只读** content：没有 `apply_*` 之类的转发函数。自检不改稿
（原型 `quality-view.html:481` 原文「生效动作保留显式确认」—— 机器改了稿
而没人点过头是被明令禁止的写路径）。
"""

from __future__ import annotations

import uuid
from typing import Any

from rednote.modules.content.service import read_edit_target
from rednote.modules.content.types import EditTarget


def read_content_edit_target(
    content_repo: Any, *, content_id: uuid.UUID
) -> EditTarget:
    """读被判对象的快照（当前版本 + 正文 + 绑定），**逐字转发**给 `content.service`。"""
    return read_edit_target(content_repo, content_id=content_id)


__all__ = ["read_content_edit_target"]
