"""`batches` → `baseline` 的接缝（WP32，**只读**）：基线的“当前”= `projects.active_baseline_id` 投影。"""

from __future__ import annotations

import uuid
from typing import Any

from rednote.modules.baseline.repository import BaselineRepository
from rednote.modules.baseline.service import active_baseline_id


def read_active_baseline_id(session: Any, *, project_id: uuid.UUID) -> uuid.UUID | None:
    """当前生效基线 id；无生效基线为 `None`。"""
    return active_baseline_id(BaselineRepository(session), project_id=project_id)


__all__ = ["read_active_baseline_id"]
