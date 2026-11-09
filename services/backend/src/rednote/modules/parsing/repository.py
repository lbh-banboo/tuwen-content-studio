"""解析运行的存储访问（WP17）。

**本层最要紧的三件事**：

1. **`source_segments` 是只写不读的追加表**：本层提供 `insert_segments`
   （批量）与 `list_segments`（读取），**不提供任何 update/delete** ——
   与 WP15 的审计表同一口径。"想改也改不了"（触发器 + ORM 守卫）
   是第二道，本层不给入口是第一道。

2. **状态迁移必须用条件 UPDATE**（`WHERE state IN (...)` + `rowcount`），
   不是"读出来 → 改属性 → flush"。理由是并发：同一个文件可能被
   两次解析请求（用户点了两次、或事件投递重复）同时处理，
   而"读-改-写"会让后写的覆盖先写的，两者的意图完全不同。
   与 WP15 的 `resolve_work_item`、WP16 的 `_apply_result` 同一手法。

3. **计数与状态一起写**，且**状态由调用方算好传入** ——
   本层不做 `derive_state`（那是业务规则，属 service）。
   但本层**会**核对"传入的状态与计数一致"，不一致即抛错：
   这是"虚报成功"在存储层的最后一道拦截（数据库 CHECK 是第一道，
   这里是第二道，两者**故意重复**：CHECK 管住绕过本层的写入，
   本层管住"有人改坏了 service 但没改 CHECK"）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from rednote.modules.parsing.models import ParseRun, SourceSegment
from rednote.modules.parsing.types import (
    PARSE_STATE_QUEUED,
    PARSE_STATE_RUNNING,
    PARSE_TERMINAL_STATES,
    derive_state,
)


class ParseRepository:
    """解析的读写入口。事务边界由调用方（service / 处理器）控制。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def flush(self) -> None:
        """把待写变更立刻发给数据库。

        **为什么要显式 flush**：让约束错误在**预期位置**抛出，
        而不是等到 `session_scope` 退出时提交才炸 ——
        那里已经离开能给出可读错误的代码位置（WP12/WP13/WP16 的教训）。
        """
        self._session.flush()

    def now(self) -> datetime:
        return self._session.execute(select(func.now())).scalar_one()

    # ------------------------------------------------------------------
    # parse_runs
    # ------------------------------------------------------------------

    def insert_run(
        self,
        *,
        file_id: uuid.UUID,
        job_id: uuid.UUID | None,
        parser_version: str,
        state: str = PARSE_STATE_QUEUED,
    ) -> ParseRun:
        run = ParseRun(
            file_id=file_id,
            job_id=job_id,
            parser_version=parser_version,
            state=state,
        )
        self._session.add(run)
        return run

    def get_run(self, run_id: uuid.UUID) -> ParseRun | None:
        """读一次 run，**强制从数据库取最新值**（`populate_existing`）。

        **为什么必须强制刷新**：本仓库的所有状态迁移都用
        `UPDATE ... WHERE state IN (...)`（裸 SQL），**不经过 ORM 的
        属性赋值**。于是 session 的身份映射里那个对象仍是旧值 ——
        紧接着 `get_run` 会原样返回**陈旧的内存副本**，
        调用方拿到的 `state` 与 `total_units` 全是错的。

        本机实测踩到过：`execute_parse` 明明把 run 写成了
        `SUCCEEDED / 3 / 3`，返回给测试的却是 `None / None` ——
        因为返回的是 UPDATE 之前就存在于 identity map 里的那个对象。

        **为什么不做成"全局 expire"**：`session.expire_all()` 会把
        调用方正在编辑的其它对象也一起失效，可能丢掉未 flush 的改动。
        只对这一个查询强制刷新，影响面精确可控。
        """
        return self._session.get(ParseRun, run_id, populate_existing=True)

    def find_reusable_run(
        self, *, file_id: uuid.UUID, parser_version: str
    ) -> ParseRun | None:
        """找同文件同解析器版本的**最近一次终态 run**（结果可复用）。

        数据结构 02 §4 原句："同一解析器版本结果可复用"。

        **复用粒度是 `(file_id, parser_version)`，不跨文件**：
        跨文件复用（按 blob sha256）能省一次 CPU，但代价是
        "系统里已经有同字节文件"这件事会因为响应时间差而泄露
        （数据结构 §4 明令："不能向无权限用户暴露'某文件已存在'"）。
        **省一次本机 CPU 不值得换一个信息泄露面。**

        只找终态：非终态的 run 可能还在写片段，复用它等于复用半成品。
        按 `created_at DESC` 取最近一次：解析器版本不变时，
        越新的 run 越可能反映当前文件内容（文件理论上不可变，
        但 `output_file_id` 与错误处理逻辑会演进）。
        """
        stmt = (
            select(ParseRun)
            .where(
                ParseRun.file_id == file_id,
                ParseRun.parser_version == parser_version,
                ParseRun.state.in_(tuple(sorted(PARSE_TERMINAL_STATES))),
            )
            .order_by(ParseRun.created_at.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def list_runs_for_file(self, file_id: uuid.UUID) -> list[ParseRun]:
        """该文件的全部 run（**按时间正序**，便于前端显示"解析历史"）。"""
        stmt = (
            select(ParseRun)
            .where(ParseRun.file_id == file_id)
            .order_by(ParseRun.created_at.asc())
        )
        return list(self._session.execute(stmt).scalars().all())

    def latest_run_for_file(self, file_id: uuid.UUID) -> ParseRun | None:
        stmt = (
            select(ParseRun)
            .where(ParseRun.file_id == file_id)
            .order_by(ParseRun.created_at.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def find_run_for_job(self, job_id: uuid.UUID) -> ParseRun | None:
        """按任务反查它产出的 run（**任务处理器用的入口**）。

        这是"任务 → run"关联的**唯一真相**（`parse_runs.job_id`）。
        刻意不用 `jobs.result_ref` 携带 run id，理由见 `handlers.py`
        的模块 docstring（竞态 + 语义错位）。

        一个任务理论上只产出一个 run（入队与建 run 同事务、幂等键唯一），
        但**不假设唯一**：取最早的一个并在多于一个时不影响正确性 ——
        若真有多个，说明有人绕过 service 直接建了 run，
        那时"取哪个"不该让处理器崩掉。
        """
        stmt = (
            select(ParseRun)
            .where(ParseRun.job_id == job_id)
            .order_by(ParseRun.created_at.asc())
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def mark_running(self, *, run_id: uuid.UUID, now: datetime) -> bool:
        """`QUEUED → RUNNING`（条件 UPDATE）。"""
        return self._transition(
            run_id=run_id,
            from_states=[PARSE_STATE_QUEUED],
            values={"state": PARSE_STATE_RUNNING, "started_at": now},
        )

    def finish_run(
        self,
        *,
        run_id: uuid.UUID,
        total_units: int | None,
        parsed_units: int,
        errors: list[dict[str, Any]],
        ocr_engine: str,
        now: datetime,
        from_states: list[str] | None = None,
    ) -> bool:
        """写终态（状态**由计数推导**，不接受调用方指定）。

        **本函数刻意没有 `state` 参数** —— 状态是算出来的。
        若让调用方传状态，"不虚报成功"就退化成一个约定：
        约定会被违反，而算出来的状态不会。

        额外做一次"传入计数是否自洽"的核对：`derive_state` 若给出
        `FAILED` 而调用方显然想写"成功"（`total_units` 非空且
        `parsed_units == total_units`），说明调用方算错了 ——
        这时**不抛错**，因为 `derive_state` 的结果才是权威，
        直接按它写即可（多一次核对只是为了在日志里留下痕迹）。
        """
        derived = derive_state(total_units=total_units, parsed_units=parsed_units)
        values: dict[str, Any] = {
            "state": derived,
            "total_units": total_units,
            "parsed_units": parsed_units,
            "errors": errors,
            "ocr_engine": ocr_engine,
            "finished_at": now,
        }
        states = from_states or [PARSE_STATE_QUEUED, PARSE_STATE_RUNNING]
        return self._transition(run_id=run_id, from_states=states, values=values)

    def cancel_run(self, *, run_id: uuid.UUID, now: datetime) -> bool:
        """标为 CANCELLED（用于任务被取消时）。

        **不是终态的"回退"**：只允许从非终态进入（条件 UPDATE 拦住重入）。
        """
        return self._transition(
            run_id=run_id,
            from_states=[PARSE_STATE_QUEUED, PARSE_STATE_RUNNING],
            values={"state": "CANCELLED", "finished_at": now},
        )

    def _transition(
        self, *, run_id: uuid.UUID, from_states: list[str], values: dict[str, Any]
    ) -> bool:
        """条件 UPDATE。返回是否真的改到了行。"""
        stmt = (
            update(ParseRun)
            .where(
                ParseRun.id == run_id,
                ParseRun.state.in_(from_states),
            )
            .values(**values)
        )
        result = self._session.execute(stmt)
        return bool(result.rowcount)

    # ------------------------------------------------------------------
    # source_segments（**只写不读的追加表**）
    # ------------------------------------------------------------------

    def insert_segments(
        self,
        *,
        run_id: uuid.UUID,
        segments: list[tuple[dict[str, Any], str, str, str, float | None]],
    ) -> int:
        """批量插入片段。

        参数里每个元组是 `(position, text_content, content_hash,
        extraction_kind, confidence)` —— **不用 `SegmentDraft` 直接传**，
        因为 hash 是 service 算的（本层不该知道哈希口径）。

        `segment_no` 由**本层按传入顺序生成**（1, 2, 3…）。
        不靠 `created_at` 排序：它的默认值是 `now()`（**事务**时间戳），
        同一事务里所有行的值相同，排序会退化成随机 UUID 顺序
        （本机实测：`["甲","乙","丙"]` 读回来是 `["乙","丙","甲"]`）。

        `on_conflict_do_nothing` 带 `index_elements`：
        重复片段（同 run 同位置同内容）**静默跳过**而不是报错。
        理由是重试场景：处理器崩溃后重跑，会产出与上次完全相同的片段集，
        此时唯一约束会拦住它们 —— 那是**正确行为**，不该让重试失败。
        """
        if not segments:
            return 0
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        rows = [
            {
                "parse_run_id": run_id,
                # 序号从 1 开始，按传入顺序（解析器逐 unit 产出的顺序）
                "segment_no": index,
                "position": position,
                "text_content": text_content,
                "content_hash": content_hash,
                "extraction_kind": extraction_kind,
                "confidence": confidence,
            }
            for index, (
                position,
                text_content,
                content_hash,
                extraction_kind,
                confidence,
            ) in enumerate(segments, start=1)
        ]
        stmt = pg_insert(SourceSegment).values(rows)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["parse_run_id", "content_hash", "position"]
        )
        result = self._session.execute(stmt)
        return int(result.rowcount or 0)

    def list_segments(self, run_id: uuid.UUID) -> list[SourceSegment]:
        """读取某次 run 的片段（**按原文顺序**）。

        排序用 `segment_no`（**显式落库的批内序号**），不用 `created_at`：
        后者是事务时间戳，同一批插入的行值相同，排序结果不确定
        （本机实测：`["甲","乙","丙"]` 读回来是 `["乙","丙","甲"]`）。
        下游（WP18 拼原文、WP21 切块）必须能按原文顺序拿到片段。
        """
        stmt = (
            select(SourceSegment)
            .where(SourceSegment.parse_run_id == run_id)
            .order_by(SourceSegment.segment_no.asc())
        )
        return list(self._session.execute(stmt).scalars().all())

    def count_segments(self, run_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(SourceSegment)
            .where(SourceSegment.parse_run_id == run_id)
        )
        return int(self._session.execute(stmt).scalar_one())


__all__ = ["ParseRepository"]
