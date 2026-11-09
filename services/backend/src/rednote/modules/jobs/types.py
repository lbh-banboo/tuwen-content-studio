"""WP13 的不可变值类型。

**为什么单独一个模块**：这些类型同时被 `service.py`（生产者）与
`handlers.py`（消费者）使用。若定义在 service 里、handlers 再导入，
就形成 `service → handlers → service` 的循环 —— Python 能靠延迟导入绕过，
但那样"谁依赖谁"就说不清了，而本模块的分层刻意是单向的：

    types ← handlers ← service
    types ← service

与 WP11/WP12 的做法一致：**跨模块传递的值用 frozen dataclass 而不是 ORM 对象**
（ORM 实例绑在 session 上，session 一关就 `DetachedInstanceError`）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JobView:
    """任务的不可变快照。"""

    job_id: uuid.UUID
    kind: str
    queue: str
    state: str
    project_id: uuid.UUID | None
    fence_token: int
    result_ref: str | None
    error_code: str | None
    created: bool


@dataclass(frozen=True)
class ClaimedJob:
    """已领取的任务：Worker 拿它去干活，回来时带着 `fence_token` 写结果。

    `fence_token` 是这张凭据的**唯一性**所在 ——
    同样的 job_id 被两个 Worker 先后领取时，只有后者的 token 是当前的。

    **`attempt_id` 是这次执行的数据库 id**（不是编号）。WP16 需要它：
    `provider_calls.job_attempt_id` 是外键，指向"这次调用算哪一次尝试的"。
    不给它的话，调用方只有 `attempt_no`，还得反查一次 ——
    而 `insert_attempt` 本来就已经把那行建出来了，id 是现成的。
    """

    job_id: uuid.UUID
    kind: str
    attempt_id: uuid.UUID
    fence_token: int
    attempt_no: int
    worker_id: str
    input_version_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class HandlerOutcome:
    """处理器对一次执行的申报。

    用**返回值**而不是异常来表达"业务结果"：`PARTIAL`（部分成功）与
    `WAITING_PROVIDER`（等外部）都不是错误 ——
    用异常表达会迫使调用方区分"我抛的这个算失败还是算等待"。
    异常只留给**真正的意外**（未捕获的 bug），由 `service._invoke_handler`
    折算成可重试的 FAILED。
    """

    state: str
    result_ref: str | None = None
    error_code: str | None = None
    # 仅对 FAILED 有意义：是否值得再试一次。
    retryable: bool = False
    # 这次执行是否产生了未结的外部用量（WP25 费用对账的输入）。
    usage_pending: bool = False


@dataclass(frozen=True)
class RunReport:
    """一次 `run_job` 的结果。"""

    job_id: uuid.UUID | None = None
    state: str | None = None
    # 结果是否被采纳。False = fence 已过期（有人接手了）——
    # **这不是错误**，正是本包要正确处理的情形。
    accepted: bool = True
    error_code: str | None = None

    @property
    def idle(self) -> bool:
        """本次没领到任务（无活可干）。"""
        return self.job_id is None


@dataclass(frozen=True)
class ReclaimReport:
    """一次过期租约回收的结果。"""

    cancelled: int = 0
    awaiting_reconciliation: int = 0
    abandoned_attempts: int = 0


@dataclass(frozen=True)
class IdempotencyDecision:
    """一次幂等判定的结论（协议 §1.3）。"""

    # NEW（首次，可执行）/ REPLAY（同 key 同内容，回放）/ CONFLICT（同 key 不同内容）
    outcome: str
    record: Any = None

    @property
    def is_new(self) -> bool:
        return self.outcome == "NEW"


__all__ = [
    "ClaimedJob",
    "HandlerOutcome",
    "IdempotencyDecision",
    "JobView",
    "ReclaimReport",
    "RunReport",
]
