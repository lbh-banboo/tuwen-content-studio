"""WP32 的依赖注入。

端点与 `service` 必须共用**同一个 session**（结构性要求，不是偶尔借用）：

| 用例 | 若分属两个 session | 症状 |
|---|---|---|
| 创建批次 | 批次 / 成员 / 内容 / 任务 / 版本节点分属多个事务 | “批次有、内容没有”的半成品；延迟一致性触发器在提交时跨事务找不到行 |
| 重做 | CAS 与入队不在一个事务 | 轮次占了位、任务没入队（或反过来） |

`session_scope` 在请求正常结束时统一提交、异常时回滚，所以端点与服务层都只 flush、不 commit。

## 关口检查依赖（`GateCheckerDep`）

`get_gate_checker` 返回一个闭包 `(project_id) -> GateVerdict`（签名见 `types.py`）。
**默认实现 `default_gate_checker` 未接线时 fail-closed**：抛 503 `DEPENDENCY_UNAVAILABLE`，
`details.reason = trial_gate_not_wired`。主线在 WP31 完成后，只需把 `get_gate_checker`
的函数体换成调用 WP31 的只读函数（如 `check_gate(session, project_id)`）并把结果转成 `GateVerdict`，
`service` 与测试都不用改（测试用 FastAPI 的 `dependency_overrides` / 直接传 stub 闭包）。

不读 baseline 的 `step_approvals` 做关口（八步编号冲突，OP-226）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends

from rednote.modules.batches.repository import BatchRepository
from rednote.modules.batches.trial_bridge import read_gate_verdict
from rednote.modules.batches.types import REASON_GATE_NOT_WIRED, GateChecker, GateVerdict
from rednote.modules.identity.dependencies import DBSession
from rednote.platform.errors import ApiError, ErrorCode


def get_batch_repository(db: DBSession) -> BatchRepository:
    """本包的仓储（全部端点共用）。"""
    return BatchRepository(db)


BatchRepoDep = Annotated[BatchRepository, Depends(get_batch_repository)]


def default_gate_checker(project_id: uuid.UUID) -> GateVerdict:
    """默认关口实现：**未接线，fail-closed**。

    宁可建不了批，也不能在没有关口结论时放行（放行 = 没试产就开扩产 / 批产，花真钱）。
    503 虽被协议标为可重试，但此时尚未写入任何东西，重试无副作用。
    """
    raise ApiError(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        "试产关口尚未接线，无法确认该项目已通过试产关口，拒绝建批。",
        details={"reason": REASON_GATE_NOT_WIRED, "projectId": str(project_id)},
    )


def get_gate_checker(db: DBSession) -> GateChecker:
    """关口检查依赖：已接 WP31 的只读接缝 `trial_gate_status`（经 `trial_bridge`）。

    `default_gate_checker`（未接线 fail-closed）保留为未接线形态的参照与测试对象，
    运行时不再使用。
    """

    def _checker(project_id: uuid.UUID) -> GateVerdict:
        return read_gate_verdict(db, project_id)

    return _checker


GateCheckerDep = Annotated[GateChecker, Depends(get_gate_checker)]


__all__ = [
    "BatchRepoDep",
    "GateCheckerDep",
    "default_gate_checker",
    "get_batch_repository",
    "get_gate_checker",
]
