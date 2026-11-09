"""WP31 对外的**只读接缝**：WP32（扩产 / 批产）经它检查"试产关口"。

## 对外承诺

```python
trial_gate_status(session, *, project_id, plan_version_id=None, baseline_id=None)
    -> TrialGateStatus
```

* **纯读**：只发 `SELECT`，不改状态、不加锁、不写审计；
* 入参 `session` 是调用方（WP32）自己的 SQLAlchemy `Session`（与其业务写入同一事务，
  这样"检查通过"与"随后落库"看到同一份快照）；
* **不抛业务异常**：项目不存在 / 没有决定 ⇒ `passed=False, latest=None`；
* `passed` ⇔ 存在至少一条 `GATE_PASSED` 轮的决定，**且**该决定**未过期**；
* 可选过滤：`plan_version_id`（只看钉住该 PLAN 版本的轮）、`baseline_id`
  （只看钉住该基线的轮）。不传即不过滤；
* `latest` 是过滤后**最近一条**决定（无论是否过期），WP32 用它展示"为什么过期"；
* `verified_scene_keys` = 全部**未过期**决定批准的方向并集（WP32 的"方向已验证矩阵"）。

## "过期"（stale）的定义

决定本身不可变；"过期"是**读时判定**（与 WP29 anchorState / WP30 STALE 同款手法）。任一成立即过期：

| 原因码 | 判据 |
|---|---|
| `BASELINE_CHANGED` | 轮钉住的 `baseline_id` ≠ `projects.active_baseline_id`（投影） |
| `CONTENT_REVISED` | 决定 `basis` 中**状态为 APPROVED** 的某条内容，其当前版本（契约 §2 口径）≠ 决定时的 `subjectVersionId` |
| `REVIEW_CHANGED` | 同上某条内容在当前版本上的有效审核 ≠ 决定时的 `reviewId`（被改判 / 被取代） |

只看 APPROVED 的依据：它们是"放行凭什么"；被淘汰内容之后被改稿不应让整轮关口作废。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.orm import Session

from rednote.modules.planning.trial.repository import (
    CURRENT_VERSION_SQL,
    EFFECTIVE_REVIEW_LATERAL_SQL,
)
from rednote.modules.planning.trial.types import (
    REVIEW_DECISION_APPROVED,
    ROUND_STATE_GATE_PASSED,
    STALE_BASELINE_CHANGED,
    STALE_CONTENT_REVISED,
    STALE_REVIEW_CHANGED,
)

_DECISIONS_SQL: Final[str] = (
    "SELECT d.id, d.round_id, d.approved_scene_keys, d.basis, d.decided_at,"
    "       d.decided_by, r.round_no, r.kind, r.plan_version_id, r.baseline_id"
    "  FROM trial_gate_decisions d"
    "  JOIN trial_rounds r ON r.id = d.round_id"
    " WHERE r.project_id = :p AND r.state = :passed"
    "   AND (CAST(:plan AS uuid) IS NULL OR r.plan_version_id = CAST(:plan AS uuid))"
    "   AND (CAST(:base AS uuid) IS NULL OR r.baseline_id = CAST(:base AS uuid))"
    " ORDER BY d.decided_at DESC, d.id DESC"
)

#: 基于 basis 逐条重判：当前版本 / 有效审核与决定时是否一致（只看 APPROVED 条目）。
_BASIS_DRIFT_SQL: Final[str] = (
    "SELECT DISTINCT CASE"
    "   WHEN cur.version_id IS DISTINCT FROM (e.entry->>'subjectVersionId')::uuid"
    "        THEN :revised"
    "   WHEN rv.id IS DISTINCT FROM (e.entry->>'reviewId')::uuid"
    "        THEN :changed END"
    "  FROM (SELECT jsonb_array_elements(CAST(:basis AS jsonb)) AS entry) e"
    "  LEFT JOIN content_items ci ON ci.id = (e.entry->>'contentItemId')::uuid"
    "  LEFT JOIN LATERAL (SELECT "
    + CURRENT_VERSION_SQL.format(ci="ci")
    + " AS version_id) cur ON true"
    "  LEFT JOIN LATERAL ("
    + EFFECTIVE_REVIEW_LATERAL_SQL.format(
        content="ci.id", version="cur.version_id"
    )
    + ") rv ON true"
    " WHERE e.entry->>'status' = :approved"
)


@dataclass(frozen=True, slots=True)
class TrialGateDecisionInfo:
    """一条关口决定的最小信息（只读接缝返回值）。"""

    decision_id: uuid.UUID
    round_id: uuid.UUID
    round_no: int
    round_kind: str
    plan_version_id: uuid.UUID
    baseline_id: uuid.UUID
    approved_scene_keys: tuple[str, ...]
    decided_at: datetime
    decided_by: uuid.UUID | None
    stale: bool
    stale_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrialGateStatus:
    """`passed`：存在未过期的关口通过决定。`latest`：过滤后最近一条（可能已过期）。"""

    passed: bool
    latest: TrialGateDecisionInfo | None
    verified_scene_keys: tuple[str, ...]


def decision_stale_reasons(
    session: Session,
    *,
    project_id: uuid.UUID,
    round_baseline_id: uuid.UUID,
    basis: list[dict[str, Any]],
) -> tuple[str, ...]:
    """某条决定的过期原因（空元组 = 未过期）。纯读。"""
    reasons: list[str] = []
    active = session.execute(
        text("SELECT active_baseline_id FROM projects WHERE id = :p"),
        {"p": project_id},
    ).first()
    if active is None or active[0] != round_baseline_id:
        reasons.append(STALE_BASELINE_CHANGED)

    import json

    rows = session.execute(
        text(_BASIS_DRIFT_SQL),
        {
            "basis": json.dumps(basis),
            "revised": STALE_CONTENT_REVISED,
            "changed": STALE_REVIEW_CHANGED,
            "approved": REVIEW_DECISION_APPROVED,
        },
    ).fetchall()
    found = {row[0] for row in rows if row[0] is not None}
    for code in (STALE_CONTENT_REVISED, STALE_REVIEW_CHANGED):
        if code in found:
            reasons.append(code)
    return tuple(reasons)


def trial_gate_status(
    session: Session,
    *,
    project_id: uuid.UUID,
    plan_version_id: uuid.UUID | None = None,
    baseline_id: uuid.UUID | None = None,
) -> TrialGateStatus:
    """项目是否存在已通过（`GATE_PASSED`）且未过期的试产关口决定。**纯读，不改状态。**

    见模块 docstring。典型用法（WP32）::

        status = trial_gate_status(repo.session, project_id=pid)
        if not status.passed:
            raise ...  # 409：试产关口未通过 / 已过期（看 status.latest.stale_reasons）
        verified = set(status.verified_scene_keys)
    """
    rows = session.execute(
        text(_DECISIONS_SQL),
        {
            "p": project_id,
            "passed": ROUND_STATE_GATE_PASSED,
            "plan": plan_version_id,
            "base": baseline_id,
        },
    ).fetchall()

    infos: list[TrialGateDecisionInfo] = []
    for row in rows:
        reasons = decision_stale_reasons(
            session,
            project_id=project_id,
            round_baseline_id=row[9],
            basis=list(row[3] or []),
        )
        infos.append(
            TrialGateDecisionInfo(
                decision_id=row[0],
                round_id=row[1],
                round_no=int(row[6]),
                round_kind=row[7],
                plan_version_id=row[8],
                baseline_id=row[9],
                approved_scene_keys=tuple(str(k) for k in (row[2] or [])),
                decided_at=row[4],
                decided_by=row[5],
                stale=bool(reasons),
                stale_reasons=reasons,
            )
        )

    fresh = [info for info in infos if not info.stale]
    verified: list[str] = []
    for info in fresh:
        for key in info.approved_scene_keys:
            if key not in verified:
                verified.append(key)
    return TrialGateStatus(
        passed=bool(fresh),
        latest=infos[0] if infos else None,
        verified_scene_keys=tuple(verified),
    )


__all__ = [
    "TrialGateDecisionInfo",
    "TrialGateStatus",
    "decision_stale_reasons",
    "trial_gate_status",
]
