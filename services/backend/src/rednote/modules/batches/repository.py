"""`batches` / `batch_items` / `batch_item_runs` 的仓储。

跨模块取数（`jobs`）走**裸 SQL**（平级模块不可互 import ORM），列名即对方契约：写错列名当场
`UndefinedColumn`，不会静默。

## `batches` 的写入面只有两个

INSERT（`insert_batch`）与条件 UPDATE（`cas_cancel` / `cas_bump`）。后两者都带
`WHERE id AND lock_version AND state='SUBMITTED'`，`rowcount == 0` 由调用方重读当前
`lock_version` 后抛 412（不得用旧值）。没有别的 UPDATE：说明文档 T-WRITE-SURFACE 靠
`test_batches_wp32.py` 枚举本文件的 UPDATE 语句断言列集合。

## SAVEPOINT

PG 里任何失败语句都会让整个事务 aborted（ISS-22）。`insert_batch` 在 `begin_nested()` 内
捕获唯一冲突，按**约束名**分流（幂等键 / 序号），事务保持可用。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rednote.modules.batches.models import Batch, BatchItem, BatchItemRun
from rednote.modules.batches.types import (
    BATCH_KIND_BULK,
    BATCH_KIND_SCALE,
    BATCH_STATE_CANCEL_REQUESTED,
    BATCH_STATE_SUBMITTED,
)

CONSTRAINT_IDEMPOTENCY: str = "uq_batches_project_idempotency_key"
CONSTRAINT_SEQ: str = "uq_batches_project_kind_seq"


class BatchInsertConflict(Exception):
    """`insert_batch` 撞唯一约束（已在 SAVEPOINT 内回滚，事务仍可用）。"""

    def __init__(self, constraint: str) -> None:
        self.constraint = constraint
        super().__init__(constraint)


def _constraint_name(exc: IntegrityError) -> str:
    orig = exc.orig
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else ""


class BatchRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def flush(self) -> None:
        self._session.flush()

    # ------------------------------------------------------------------
    # 锁与序号
    # ------------------------------------------------------------------

    def advisory_lock_project(self, project_id: uuid.UUID) -> None:
        """事务级 advisory lock：串行化同项目创建（保护 `seq_no` 与 `allocated_so_far`，R6）。"""
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('batch:' || :p))"),
            {"p": str(project_id)},
        )

    def next_seq_no(self, *, project_id: uuid.UUID, batch_kind: str) -> int:
        row = self._session.execute(
            text(
                "SELECT coalesce(max(seq_no), 0) + 1 FROM batches"
                " WHERE project_id = :p AND batch_kind = :k"
            ),
            {"p": project_id, "k": batch_kind},
        ).scalar_one()
        return int(row)

    # ------------------------------------------------------------------
    # batches：读
    # ------------------------------------------------------------------

    def get_batch(self, batch_id: uuid.UUID) -> Batch | None:
        return self._session.get(Batch, batch_id, populate_existing=True)

    def find_by_idempotency(
        self, *, project_id: uuid.UUID, idempotency_key: str
    ) -> Batch | None:
        return self._session.execute(
            select(Batch)
            .where(
                Batch.project_id == project_id,
                Batch.idempotency_key == idempotency_key,
            )
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def list_batches(
        self,
        *,
        project_id: uuid.UUID,
        batch_kind: str | None,
        state: str | None,
        before: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Batch]:
        """项目下的批次，新的在前（keyset：`created_at DESC, id DESC`）。"""
        stmt = select(Batch).where(Batch.project_id == project_id)
        if batch_kind is not None:
            stmt = stmt.where(Batch.batch_kind == batch_kind)
        if state is not None:
            stmt = stmt.where(Batch.state == state)
        if before is not None:
            b_created, b_id = before
            stmt = stmt.where(
                or_(
                    Batch.created_at < b_created,
                    and_(Batch.created_at == b_created, Batch.id < b_id),
                )
            )
        stmt = stmt.order_by(Batch.created_at.desc(), Batch.id.desc()).limit(limit)
        return list(self._session.execute(stmt).scalars().all())

    def allocated_so_far(
        self, *, project_id: uuid.UUID, plan_version_id: uuid.UUID, dimension: str
    ) -> dict[str, int]:
        """历史批次在某维度（`scenes` / `forms`）上已分配的累计量。

        口径（OP-Q18 暂用默认）：同项目、同 `plan_version_id`、`state='SUBMITTED'` 的
        SCALE + BULK 批次；`CANCEL_REQUESTED` 视为释放，TRIAL 不计入。
        读的是批次自己的 `allocation` 快照（不重算）。
        """
        if dimension not in {"scenes", "forms"}:
            raise ValueError(f"未知维度：{dimension!r}")
        rows = self._session.execute(
            text(
                "SELECT e->>'key', sum((e->>'allocated')::int)"
                "  FROM batches b, jsonb_array_elements(b.allocation -> :dim) AS e"
                " WHERE b.project_id = :p AND b.plan_version_id = :v"
                "   AND b.state = :submitted AND b.batch_kind IN (:scale, :bulk)"
                " GROUP BY 1"
            ),
            {
                "dim": dimension,
                "p": project_id,
                "v": plan_version_id,
                "submitted": BATCH_STATE_SUBMITTED,
                "scale": BATCH_KIND_SCALE,
                "bulk": BATCH_KIND_BULK,
            },
        ).all()
        return {str(key): int(total) for key, total in rows}

    # ------------------------------------------------------------------
    # batches：写（两个写路径之一：INSERT）
    # ------------------------------------------------------------------

    def insert_batch(self, **columns: Any) -> Batch:
        """INSERT 一行；唯一冲突在 SAVEPOINT 内捕获并按约束名抛 `BatchInsertConflict`。"""
        batch = Batch(**columns)
        try:
            with self._session.begin_nested():
                self._session.add(batch)
                self._session.flush()
        except IntegrityError as exc:
            name = _constraint_name(exc)
            if name in {CONSTRAINT_IDEMPOTENCY, CONSTRAINT_SEQ}:
                raise BatchInsertConflict(name) from exc
            raise
        return batch

    # ------------------------------------------------------------------
    # batches：写（两个写路径之二：条件 UPDATE；`rowcount == 0` 由调用方抛 412）
    # ------------------------------------------------------------------

    def cas_cancel(
        self,
        *,
        batch_id: uuid.UUID,
        expected_lock_version: int,
        actor_id: uuid.UUID | None,
    ) -> int:
        """`SUBMITTED → CANCEL_REQUESTED`。只改 state / cancel_requested_* / lock_version / updated_at。"""
        result = self._session.execute(
            text(
                "UPDATE batches SET state = :cancelled, cancel_requested_at = now(),"
                " cancel_requested_by = :actor, lock_version = lock_version + 1,"
                " updated_at = now()"
                " WHERE id = :id AND lock_version = :v AND state = :submitted"
            ),
            {
                "cancelled": BATCH_STATE_CANCEL_REQUESTED,
                "submitted": BATCH_STATE_SUBMITTED,
                "actor": actor_id,
                "id": batch_id,
                "v": expected_lock_version,
            },
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    def cas_bump(self, *, batch_id: uuid.UUID, expected_lock_version: int) -> int:
        """仅推进 `lock_version`（失败项重做占位：与取消 / 其它重做互斥）。"""
        result = self._session.execute(
            text(
                "UPDATE batches SET lock_version = lock_version + 1, updated_at = now()"
                " WHERE id = :id AND lock_version = :v AND state = :submitted"
            ),
            {
                "submitted": BATCH_STATE_SUBMITTED,
                "id": batch_id,
                "v": expected_lock_version,
            },
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # 成员与轮次（不可变：只有 INSERT）
    # ------------------------------------------------------------------

    def insert_item(
        self,
        *,
        batch_id: uuid.UUID,
        ordinal: int,
        content_item_id: uuid.UUID,
        scene_key: str | None,
        form_key: str | None,
    ) -> BatchItem:
        item = BatchItem(
            batch_id=batch_id,
            ordinal=ordinal,
            content_item_id=content_item_id,
            scene_key=scene_key,
            form_key=form_key,
        )
        self._session.add(item)
        self._session.flush()
        return item

    def insert_run(
        self, *, batch_item_id: uuid.UUID, round_no: int, job_id: uuid.UUID
    ) -> BatchItemRun:
        run = BatchItemRun(batch_item_id=batch_item_id, round_no=round_no, job_id=job_id)
        self._session.add(run)
        self._session.flush()
        return run

    # ------------------------------------------------------------------
    # 读：成员 + 最新一轮 job 状态（裸 SQL 读 jobs）
    # ------------------------------------------------------------------

    def list_members(
        self,
        *,
        batch_ids: Sequence[uuid.UUID],
        after_ordinal: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """成员 + **最新轮**（`round_no` 最大，不按时间戳）的 job 状态。

        没有 run / job 行缺失时 `latest_round` / `job_state` 为 `None`（折算为 UNKNOWN，不折成 0）。
        单个批次时可带 `after_ordinal` / `limit` 做分页。
        """
        if not batch_ids:
            return []
        sql = (
            "SELECT bi.id, bi.batch_id, bi.ordinal, bi.content_item_id,"
            "       bi.scene_key, bi.form_key, r.round_no, r.job_id, j.state"
            "  FROM batch_items bi"
            "  LEFT JOIN LATERAL ("
            "       SELECT round_no, job_id FROM batch_item_runs"
            "        WHERE batch_item_id = bi.id ORDER BY round_no DESC LIMIT 1"
            "  ) r ON true"
            "  LEFT JOIN jobs j ON j.id = r.job_id"
            " WHERE bi.batch_id = ANY(:ids)"
        )
        params: dict[str, Any] = {"ids": list(batch_ids)}
        if after_ordinal is not None:
            sql += " AND bi.ordinal > :after"
            params["after"] = after_ordinal
        sql += " ORDER BY bi.batch_id, bi.ordinal"
        if limit is not None:
            sql += " LIMIT :lim"
            params["lim"] = limit
        rows = self._session.execute(text(sql), params).all()
        return [
            {
                "item_id": row[0],
                "batch_id": row[1],
                "ordinal": int(row[2]),
                "content_item_id": row[3],
                "scene_key": row[4],
                "form_key": row[5],
                "latest_round": None if row[6] is None else int(row[6]),
                "job_id": row[7],
                "job_state": row[8],
            }
            for row in rows
        ]

    def count_members(self, batch_id: uuid.UUID) -> int:
        return int(
            self._session.execute(
                text("SELECT count(*) FROM batch_items WHERE batch_id = :b"),
                {"b": batch_id},
            ).scalar_one()
        )


__all__ = [
    "BatchInsertConflict",
    "BatchRepository",
    "CONSTRAINT_IDEMPOTENCY",
    "CONSTRAINT_SEQ",
]
