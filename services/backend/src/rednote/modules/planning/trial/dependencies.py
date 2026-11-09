"""WP31 依赖注入：端点与 service 共用同一个 session（只 flush，不 commit）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.identity.dependencies import DBSession
from rednote.modules.planning.repository import PlanningRepository
from rednote.modules.planning.trial.repository import TrialRepository


def get_trial_repository(db: DBSession) -> TrialRepository:
    return TrialRepository(db)


TrialRepoDep = Annotated[TrialRepository, Depends(get_trial_repository)]


def get_planning_repository_for_trial(db: DBSession) -> PlanningRepository:
    """配置 / 草稿仓储（与 `TrialRepoDep` 同一 session）。"""
    return PlanningRepository(db)


TrialPlanningRepoDep = Annotated[
    PlanningRepository, Depends(get_planning_repository_for_trial)
]

__all__ = [
    "TrialPlanningRepoDep",
    "TrialRepoDep",
    "get_planning_repository_for_trial",
    "get_trial_repository",
]
