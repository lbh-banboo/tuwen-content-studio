"""批次进度的读时折算（**纯函数**，零 IO）。

批次行只存“命令”（`SUBMITTED` / `CANCEL_REQUESTED`），执行进度永远从 `jobs` 折算，不落库。

输入：批次 `state` + 每条成员**最新一轮**（`round_no` 最大）的 job 状态（成员没有任何 run
或 job 行缺失时为 `None`）。输出 `Progress`：派生 8 值 `state` + `counts`。

## 折算表（自上而下，第一条命中即返回；说明文档 §2.2.3）

| 条件 | `state` |
|---|---|
| 成员数 = 0，或任一成员没有 run，或 `counts` 之和 != 成员数 | `UNKNOWN`（**不折成 0 / FAILED**） |
| 批次 `CANCEL_REQUESTED` 且所有最新 job 终态 | `CANCELLED` |
| 批次 `CANCEL_REQUESTED` 且仍有非终态 | `CANCELLING` |
| 所有最新 job `QUEUED` | `QUEUED` |
| 存在非终态 job | `RUNNING` |
| 全终态且全 `SUCCEEDED` | `SUCCEEDED` |
| 全终态，无 `SUCCEEDED` 且无 `PARTIAL`（全是 `FAILED` / `CANCELLED`） | `FAILED` |
| 其余全终态组合 | `PARTIAL` |

`WAITING_RECONCILIATION` 是**非终态**：存在它时结论只可能是 `RUNNING` / `CANCELLING`，
永远不会被折成 `SUCCEEDED` / `FAILED` / `PARTIAL`；它的条数还单列为 `needsReconciliation`。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from rednote.modules.batches.jobs_bridge import (
    JOB_STATES,
    JOB_TERMINAL_STATES,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_PARTIAL,
    STATE_QUEUED,
    STATE_SUCCEEDED,
    STATE_WAITING_RECONCILIATION,
)
from rednote.modules.batches.types import (
    BATCH_STATE_CANCEL_REQUESTED,
    PROGRESS_CANCELLED,
    PROGRESS_CANCELLING,
    PROGRESS_FAILED,
    PROGRESS_PARTIAL,
    PROGRESS_QUEUED,
    PROGRESS_RUNNING,
    PROGRESS_SUCCEEDED,
    PROGRESS_UNKNOWN,
)


@dataclass(frozen=True)
class Progress:
    """折算结果。`counts` 按 job 状态分列（9 个键恒在，确为 0 才是 0）。"""

    state: str
    total: int
    counts: dict[str, int] = field(default_factory=dict)
    #: 没有 run / job 的成员数（>0 时 `state` 必为 `UNKNOWN`）。
    missing: int = 0

    @property
    def needs_reconciliation(self) -> int:
        """`WAITING_RECONCILIATION` 的条数 —— 单列，不被任何汇总吞掉。"""
        return self.counts.get(STATE_WAITING_RECONCILIATION, 0)


def rollup(batch_state: str, latest_job_states: Sequence[str | None]) -> Progress:
    """见模块 docstring 的折算表。"""
    total = len(latest_job_states)
    counts: dict[str, int] = {name: 0 for name in sorted(JOB_STATES)}
    missing = 0
    unknown_value = 0
    for value in latest_job_states:
        if value is None:
            missing += 1
        elif value in counts:
            counts[value] += 1
        else:
            unknown_value += 1

    # 计数自洽：各状态之和 + 缺失 + 未知值 == 成员数（自洽判据由结构保证；
    # 这里再核一次，是为了让“有人改了上面的计数逻辑”在这里当场变成 UNKNOWN 而不是静默错数）。
    if (
        total == 0
        or missing > 0
        or unknown_value > 0
        or sum(counts.values()) != total
    ):
        return Progress(PROGRESS_UNKNOWN, total, counts, missing)

    all_terminal = all(
        counts[name] == 0 for name in JOB_STATES if name not in JOB_TERMINAL_STATES
    )

    if batch_state == BATCH_STATE_CANCEL_REQUESTED:
        state = PROGRESS_CANCELLED if all_terminal else PROGRESS_CANCELLING
    elif counts[STATE_QUEUED] == total:
        state = PROGRESS_QUEUED
    elif not all_terminal:
        state = PROGRESS_RUNNING
    elif counts[STATE_SUCCEEDED] == total:
        state = PROGRESS_SUCCEEDED
    elif counts[STATE_SUCCEEDED] == 0 and counts[STATE_PARTIAL] == 0:
        # 此时全部终态只剩 FAILED / CANCELLED。
        assert counts[STATE_FAILED] + counts[STATE_CANCELLED] == total
        state = PROGRESS_FAILED
    else:
        state = PROGRESS_PARTIAL
    return Progress(state, total, counts, missing)


__all__ = ["Progress", "rollup"]
