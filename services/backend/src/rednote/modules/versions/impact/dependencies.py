"""WP34 的依赖注入：端点与 `service` 共用**同一个 session**（`session_scope` 统一提交 / 回滚）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.identity.dependencies import DBSession
from rednote.modules.versions.impact.repository import ImpactRepository


def get_impact_repository(db: DBSession) -> ImpactRepository:
    return ImpactRepository(db)


ImpactRepoDep = Annotated[ImpactRepository, Depends(get_impact_repository)]

__all__ = ["ImpactRepoDep", "get_impact_repository"]
