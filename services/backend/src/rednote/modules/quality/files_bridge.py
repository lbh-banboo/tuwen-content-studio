"""`quality` → `files` 的接缝（WP33）：把报告导出字节落成 `file_objects`（`PURPOSE_GENERATED`，不占研究配额）。

`store_generated_asset` **每次调用都新建 `file_objects` 行，自身不幂等**——幂等判据在
`quality_report_artifacts` 的 `UNIQUE(report_version_id, format)`，由服务层先查再建。
"""

from __future__ import annotations

import uuid
from typing import Any

from rednote.modules.files.models import PURPOSE_GENERATED
from rednote.modules.files.repository import FileRepository
from rednote.modules.files.service import store_generated_asset


def store_report_bytes(
    session: Any,
    storage: Any,
    *,
    project_id: uuid.UUID,
    original_name: str,
    content: bytes,
    mime_type: str,
    uploaded_by: uuid.UUID,
) -> Any:
    return store_generated_asset(
        FileRepository(session),
        storage,
        project_id=project_id,
        original_name=original_name,
        content=content,
        mime_type=mime_type,
        uploaded_by=uploaded_by,
        purpose=PURPOSE_GENERATED,
    )


__all__ = ["PURPOSE_GENERATED", "store_report_bytes"]
