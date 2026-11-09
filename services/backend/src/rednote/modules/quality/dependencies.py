"""WP33 的依赖注入：端点与 `service` 共用**同一个 session**（`session_scope` 统一提交 / 回滚）。

报告版本、子表行、`version_nodes`、依赖边必须同一事务（延迟 FK / 延迟一致性触发器在提交时才核对）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.identity.dependencies import DBSession
from rednote.modules.quality.repository import QualityRepository


def get_quality_repository(db: DBSession) -> QualityRepository:
    return QualityRepository(db)


QualityRepoDep = Annotated[QualityRepository, Depends(get_quality_repository)]

__all__ = ["QualityRepoDep", "get_quality_repository"]
