"""`batches` → `audit` 的接缝（WP32）：与业务变更同事务写审计（open-set 动作串 `batch.*`）。

只在 `actor_id` 与 `request_id` 都齐时才写（照 `billing/service.py::_audit` 的判据）。
`before` / `after` 必须成对：“从无到有”用 `{}`。
"""

from __future__ import annotations

import uuid
from typing import Any

from rednote.modules.audit.repository import AuditRepository
from rednote.modules.audit.service import record_audit


def audit(
    session: Any,
    *,
    action: str,
    object_type: str,
    object_id: uuid.UUID,
    project_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    request_id: str | None,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    if actor_id is None or request_id is None:
        return
    record_audit(
        AuditRepository(session),
        action=action,
        object_type=object_type,
        object_id=object_id,
        request_id=request_id,
        actor_id=actor_id,
        project_id=project_id,
        before=before,
        after=after,
    )


__all__ = ["audit"]
