"""`batches` → `planning.trial` 的接缝（WP32，**只读**）：试产关口结论。

主线把 WP32 的关口检查（`dependencies.get_gate_checker`）接到 WP31 导出的只读函数
`trial_gate_status`（`planning/trial/seam.py`）。WP31 的函数纯读、不抛业务异常：
项目不存在 / 没有决定 ⇒ `passed=False`。

`reason`（仅在未通过时回显，不参与判定）：
* 从未有过关口决定 ⇒ `no_gate_decision`；
* 有决定但已过期 ⇒ `stale:` + 过期原因（`BASELINE_CHANGED` / `CONTENT_REVISED` / `REVIEW_CHANGED`）。
"""

from __future__ import annotations

import uuid
from typing import Any

from rednote.modules.batches.types import GateVerdict
from rednote.modules.planning.trial import trial_gate_status

REASON_NO_GATE_DECISION = "no_gate_decision"
REASON_STALE_PREFIX = "stale:"


def read_gate_verdict(session: Any, project_id: uuid.UUID) -> GateVerdict:
    """读该项目的试产关口结论（只发 SELECT）。"""
    status = trial_gate_status(session, project_id=project_id)
    latest = status.latest
    if status.passed:
        return GateVerdict(
            passed=True,
            decision_id=latest.decision_id if latest is not None else None,
            decided_at=latest.decided_at if latest is not None else None,
        )
    if latest is None:
        return GateVerdict(passed=False, reason=REASON_NO_GATE_DECISION)
    return GateVerdict(
        passed=False,
        decision_id=latest.decision_id,
        decided_at=latest.decided_at,
        reason=REASON_STALE_PREFIX + ",".join(latest.stale_reasons)
        if latest.stale
        else None,
    )
