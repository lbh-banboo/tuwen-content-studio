"""`batches` → `jobs` 的接缝（WP32）：入队、取消、通用幂等记录、状态集合。

`jobs/*.py` 禁改；本包只调公开函数，并**经本桥引用**任务状态集合（不抄一份）。
任务状态的权威在 `jobs.models`，进度折算（`progress.py`）只读这里导出的常量。
"""

from __future__ import annotations

import uuid
from typing import Any

from rednote.modules.jobs.models import (
    JOB_STATES,
    JOB_TERMINAL_STATES,
    STATE_CANCEL_REQUESTED,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_PARTIAL,
    STATE_QUEUED,
    STATE_RUNNING,
    STATE_SUCCEEDED,
    STATE_WAITING_PROVIDER,
    STATE_WAITING_RECONCILIATION,
)
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.jobs.service import (
    begin_idempotent_request,
    complete_idempotent_request,
    enqueue_job,
    request_cancel,
)
from rednote.modules.jobs.types import JobView


def enqueue_batch_job(
    session: Any,
    *,
    kind: str,
    queue: str,
    idempotency_scope: str,
    idempotency_key: str,
    input_hash: str,
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    content_id: uuid.UUID,
) -> JobView:
    """入队一个带 `batch_id` / `content_id` 的任务（同事务写 outbox 事件）。"""
    return enqueue_job(
        JobRepository(session),
        kind=kind,
        queue=queue,
        idempotency_scope=idempotency_scope,
        idempotency_key=idempotency_key,
        input_hash=input_hash,
        project_id=project_id,
        batch_id=batch_id,
        content_id=content_id,
    )


def cancel_job(session: Any, *, job_id: uuid.UUID) -> bool:
    """登记取消意图；返回 `False` = 任务已是终态。"""
    return request_cancel(JobRepository(session), job_id=job_id)


def begin_request(session: Any, *, scope: str, key: str, request_hash: str) -> Any:
    """通用幂等记录（`permanent=True`：生产任务类请求不得因过期而再计费）。"""
    return begin_idempotent_request(
        JobRepository(session),
        scope=scope,
        key=key,
        request_hash=request_hash,
        permanent=True,
    )


def complete_request(
    session: Any,
    *,
    scope: str,
    key: str,
    response_status: int,
    response_json: dict[str, Any],
    resource_id: uuid.UUID,
) -> bool:
    return complete_idempotent_request(
        JobRepository(session),
        scope=scope,
        key=key,
        response_status=response_status,
        response_json=response_json,
        resource_id=resource_id,
    )


__all__ = [
    "JOB_STATES",
    "JOB_TERMINAL_STATES",
    "STATE_CANCELLED",
    "STATE_CANCEL_REQUESTED",
    "STATE_FAILED",
    "STATE_PARTIAL",
    "STATE_QUEUED",
    "STATE_RUNNING",
    "STATE_SUCCEEDED",
    "STATE_WAITING_PROVIDER",
    "STATE_WAITING_RECONCILIATION",
    "begin_request",
    "cancel_job",
    "complete_request",
    "enqueue_batch_job",
]
