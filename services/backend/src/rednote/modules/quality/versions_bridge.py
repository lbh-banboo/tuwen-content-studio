"""`quality` → `versions` 的接缝（WP33）：REPORT 版本节点与依赖边 E11（接缝冻结 §5.2 / §5.16）。

**`revision_no` 必须显式传**（OP-91：不传则 `max+1` 不幂等）。边写入失败整体回滚、异常上抛、不吞；
可空外键调用方先判空（`link_dependency` 对不存在的端点抛 422）。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from rednote.modules.versions.models import ENTITY_TYPE_REPORT
from rednote.modules.versions.repository import VersionRepository
from rednote.modules.versions.service import (
    RegisteredVersion,
    link_dependency,
    register_version,
)


def register_report_version(
    session: Any,
    *,
    report_id: uuid.UUID,
    project_id: uuid.UUID,
    payload: Mapping[str, Any],
    created_by: uuid.UUID | None,
    revision_no: int,
) -> RegisteredVersion:
    return register_version(
        VersionRepository(session),
        entity_type=ENTITY_TYPE_REPORT,
        entity_id=report_id,
        payload=payload,
        project_id=project_id,
        created_by=created_by,
        revision_no=revision_no,
    )


def link(
    session: Any,
    *,
    parent_version_id: uuid.UUID,
    dependency_version_id: uuid.UUID,
    purpose: str,
) -> None:
    link_dependency(
        VersionRepository(session),
        parent_version_id=parent_version_id,
        dependency_version_id=dependency_version_id,
        purpose=purpose,
    )


__all__ = ["ENTITY_TYPE_REPORT", "link", "register_report_version"]
