"""`batches` → `content` 的接缝（WP32）：物化内容、生成请求、任务常量、provider 解析。

只转发、不写业务（沿用 `review/*_bridge.py` 的做法）。涉及：

* `start_generation`（`content/service.py`）：物化 N 条身份行；`scene_key` / `form_key`
  经接缝冻结 §5.15 的 `GenerationRequest` 可选字段传入（**当前恒为 None**：场景 x 形式联合展开规则
  未定义，OP-227，本包不编造）；
* `GenerationRequest` / `task_kinds_for_scope` / 任务常量；
* `resolve_provider_id`（`content/service.py`，全包唯一解析点）。

`ContentGenerationError`（`ValueError` 族，带 `reason`）原样冒出，由 `service.py` 翻成 `ApiError`。
"""

from __future__ import annotations

import uuid
from typing import Any

from rednote.modules.content.service import (
    ContentGenerationError,
    resolve_provider_id,
    start_generation,
)
from rednote.modules.content.types import (
    IDEMPOTENCY_SCOPE_CONTENT_GENERATE,
    KIND_CONTENT_GENERATE,
    QUEUE_CONTENT,
    GenerationRequest,
    task_kinds_for_scope,
)
from rednote.modules.content.repository import ContentRepository
from rednote.modules.gateway.repository import GatewayRepository


def build_generation_request(
    *,
    scope: str,
    batch_kind: str,
    model_profile_id: uuid.UUID,
    scene_key: str | None = None,
    form_key: str | None = None,
) -> GenerationRequest:
    """构造生成请求（构造即归一 + 校验；`ValueError` 由调用方翻译）。"""
    return GenerationRequest(
        scope=scope,
        batch_kind=batch_kind,
        model_profile_id=model_profile_id,
        scene_key=scene_key,
        form_key=form_key,
    )


def materialize_contents(
    session: Any,
    *,
    project_id: uuid.UUID,
    request: GenerationRequest,
    content_count: int,
    idempotency_key: str,
    created_by: uuid.UUID | None,
) -> tuple[Any, ...]:
    """物化 `content_count` 条内容身份行，返回 `ContentItem` 元组（按创建顺序）。"""
    rows, _created = start_generation(
        ContentRepository(session),
        project_id=project_id,
        request=request,
        content_count=content_count,
        idempotency_key=idempotency_key,
        created_by=created_by,
    )
    return rows


def provider_id_of(session: Any, *, model_profile_id: uuid.UUID) -> uuid.UUID:
    """由模型档案解出 `provider_id`（content 的唯一解析点）。"""
    return resolve_provider_id(
        GatewayRepository(session), model_profile_id=model_profile_id
    )


__all__ = [
    "ContentGenerationError",
    "IDEMPOTENCY_SCOPE_CONTENT_GENERATE",
    "KIND_CONTENT_GENERATE",
    "QUEUE_CONTENT",
    "build_generation_request",
    "materialize_contents",
    "provider_id_of",
    "task_kinds_for_scope",
]
