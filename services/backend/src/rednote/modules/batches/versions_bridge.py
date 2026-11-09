"""`batches` → `versions` 的接缝（WP32）：PLAN 快照节点与依赖边（接缝冻结 §5.2 / §5.16）。

**`revision_no` 显式传 1**（OP-91：不传则 `max+1` 不幂等；WP32 天生要重试）。
取值是确定的（批次提交后不可改，每个批次恰一个快照节点），不依赖此刻的 max。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from rednote.modules.versions.models import ENTITY_TYPE_PLAN
from rednote.modules.versions.repository import VersionRepository
from rednote.modules.versions.service import (
    RegisteredVersion,
    canonical_payload_hash,
    link_dependency,
    register_version,
)


def register_snapshot(
    session: Any,
    *,
    batch_id: uuid.UUID,
    project_id: uuid.UUID,
    payload: Mapping[str, Any],
    created_by: uuid.UUID | None,
    revision_no: int,
) -> RegisteredVersion:
    return register_version(
        VersionRepository(session),
        entity_type=ENTITY_TYPE_PLAN,
        entity_id=batch_id,
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


def get_node(session: Any, node_id: uuid.UUID) -> Any | None:
    return VersionRepository(session).get_node(node_id)


def plan_chain_head_revision(session: Any, *, project_id: uuid.UUID) -> int:
    """该项目 PLAN 链（`entity_id=project_id`）的最大修订号；无则 0。"""
    return VersionRepository(session).max_revision_no(
        entity_type=ENTITY_TYPE_PLAN, entity_id=project_id
    )


__all__ = [
    "ENTITY_TYPE_PLAN",
    "canonical_payload_hash",
    "get_node",
    "link",
    "plan_chain_head_revision",
    "register_snapshot",
]
