"""WP31 试产关口 / 淘汰补产（`modules/planning/trial`）。

八件套：`types` / `models` / `ddl` / `repository` / `service` / `schemas` /
`dependencies` / `api`，外加只读接缝 `seam`（WP32 经 `trial_gate_status` 检查关口）。

装配（主线收口）：`env.py` import `planning.trial.models`；`main.py` 注册本包 `router`；
迁移由主线串行落库（DDL 见 `docs/m3/WP31-ddl-草案.py`，触发器见 `ddl.all_trigger_ddl()`）。
"""

from __future__ import annotations

from rednote.modules.planning.trial import models  # noqa: F401  （建表注册）
from rednote.modules.planning.trial.api import router  # noqa: F401  （端点注册）
from rednote.modules.planning.trial.seam import (  # noqa: F401
    TrialGateDecisionInfo,
    TrialGateStatus,
    trial_gate_status,
)

__all__: list[str] = [
    "TrialGateDecisionInfo",
    "TrialGateStatus",
    "router",
    "trial_gate_status",
]
