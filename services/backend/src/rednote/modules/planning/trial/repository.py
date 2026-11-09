"""`trial_rounds` / `trial_round_slots` / `trial_gate_decisions` 的仓储。

跨模块取数走**裸 SQL**（其他模块的表不 import 其 ORM 模型），列名即对方契约：
写错列名当场 `UndefinedColumn`，不会静默。**本仓储对他表只读**（A-01 判据：
本包不含对他表的 INSERT/UPDATE/DELETE，唯一例外是 `SELECT ... FOR SHARE` 加锁）。

## "当前版本"的口径（契约 §2）

**当前版本** = `content_items.current_version_id` 投影；仅当其为 NULL 才回落
`content_versions ... ORDER BY version_no DESC LIMIT 1`。回落分支**只出现在**
`CURRENT_VERSION_SQL` 这一处（`COALESCE` 内），其余查询一律引用它。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from rednote.modules.planning.trial.models import (
    TrialGateDecision,
    TrialRound,
    TrialRoundSlot,
)
from rednote.modules.planning.trial.types import (
    ROUND_OPEN_STATES,
    ROUND_STATE_CANCELLED,
    SlotFact,
)

#: "当前版本"表达式模板。`{ci}` 是 content_items 的别名。投影优先，仅 NULL 回落末版。
CURRENT_VERSION_SQL: Final[str] = (
    "COALESCE({ci}.current_version_id,"
    " (SELECT cv.id FROM content_versions cv"
    "   WHERE cv.content_item_id = {ci}.id"
    "   ORDER BY cv.version_no DESC LIMIT 1))"
)

#: 某内容在其"当前版本"上、未被取代的有效审核（最新一条）。
#: `NOT EXISTS` 子查询用**另一行**的 `supersedes_id`（自比恒假，MAJOR-4）。
EFFECTIVE_REVIEW_LATERAL_SQL: Final[str] = (
    "SELECT r.id, r.decision, r.subject_version_id FROM content_reviews r"
    " WHERE r.content_item_id = {content}"
    "   AND r.subject_version_id = {version}"
    "   AND NOT EXISTS (SELECT 1 FROM content_reviews c WHERE c.supersedes_id = r.id)"
    " ORDER BY r.decided_at DESC, r.id DESC LIMIT 1"
)

_SLOT_FACTS_SQL: Final[str] = (
    "SELECT s.id, s.round_id, s.slot_no, s.scene_key, s.form_key,"
    "       s.content_item_id, s.replaces_slot_id,"
    "       EXISTS (SELECT 1 FROM trial_round_slots x WHERE x.replaces_slot_id = s.id),"
    "       ci.state, cur.version_id, rv.id, rv.decision,"
    "       EXISTS (SELECT 1 FROM content_reviews ar"
    "                WHERE ar.content_item_id = s.content_item_id),"
    "       COALESCE("
    "         (SELECT cv.version_no FROM content_versions cv"
    "           WHERE cv.id = rv.subject_version_id)"
    "         = (SELECT min(v2.version_no) FROM content_versions v2"
    "             WHERE v2.content_item_id = s.content_item_id), false),"
    "       EXISTS (SELECT 1 FROM content_feedback_items f"
    "                WHERE f.content_item_id = s.content_item_id"
    "                  AND f.severity = 'BLOCKING'"
    "                  AND f.state NOT IN ('RESOLVED', 'REJECTED')),"
    "       COALESCE((SELECT sc.subject_version_id = cur.version_id"
    "                   FROM self_check_runs sc"
    "                  WHERE sc.content_item_id = s.content_item_id"
    "                  ORDER BY sc.created_at DESC, sc.id DESC LIMIT 1), false)"
    "  FROM trial_round_slots s"
    "  LEFT JOIN content_items ci ON ci.id = s.content_item_id"
    "  LEFT JOIN LATERAL (SELECT "
    + CURRENT_VERSION_SQL.format(ci="ci")
    + " AS version_id) cur ON true"
    "  LEFT JOIN LATERAL ("
    + EFFECTIVE_REVIEW_LATERAL_SQL.format(
        content="s.content_item_id", version="cur.version_id"
    )
    + ") rv ON true"
    " WHERE s.round_id = :rid ORDER BY s.slot_no"
)


class TrialRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def flush(self) -> None:
        self._session.flush()

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        with self._session.begin_nested():
            yield

    # ------------------------------------------------------------------
    # 项目与取号
    # ------------------------------------------------------------------

    def project_exists(self, project_id: uuid.UUID) -> bool:
        return (
            self._session.execute(
                text("SELECT 1 FROM projects WHERE id = :p"), {"p": project_id}
            ).first()
            is not None
        )

    def lock_project_rounds(self, project_id: uuid.UUID) -> None:
        """项目级事务 advisory lock：串行化"取号 + 判重 + 插入"。"""
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('trial-round:' || :p))"),
            {"p": str(project_id)},
        )

    def next_round_no(self, project_id: uuid.UUID) -> int:
        value = self._session.execute(
            text("SELECT COALESCE(max(round_no), 0) + 1 FROM trial_rounds WHERE project_id = :p"),
            {"p": project_id},
        ).scalar_one()
        return int(value)

    # ------------------------------------------------------------------
    # 轮
    # ------------------------------------------------------------------

    def get_round(self, round_id: uuid.UUID) -> TrialRound | None:
        return self._session.execute(
            select(TrialRound)
            .where(TrialRound.id == round_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def get_round_for_update(self, round_id: uuid.UUID) -> TrialRound | None:
        return self._session.execute(
            select(TrialRound)
            .where(TrialRound.id == round_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def find_open_round(self, project_id: uuid.UUID) -> TrialRound | None:
        return self._session.execute(
            select(TrialRound)
            .where(
                TrialRound.project_id == project_id,
                TrialRound.state.in_(sorted(ROUND_OPEN_STATES)),
            )
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def find_round_by_idempotency(
        self, project_id: uuid.UUID, key: str
    ) -> TrialRound | None:
        return self._session.execute(
            select(TrialRound)
            .where(
                TrialRound.project_id == project_id,
                TrialRound.idempotency_key == key,
            )
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def insert_round(self, **values: Any) -> TrialRound:
        row = TrialRound(**values)
        self._session.add(row)
        self._session.flush()
        self._session.refresh(row)
        return row

    def list_rounds(
        self,
        project_id: uuid.UUID,
        *,
        before_round_no: int | None,
        limit: int,
    ) -> list[TrialRound]:
        stmt = select(TrialRound).where(TrialRound.project_id == project_id)
        if before_round_no is not None:
            stmt = stmt.where(TrialRound.round_no < before_round_no)
        stmt = stmt.order_by(TrialRound.round_no.desc()).limit(limit)
        return list(
            self._session.execute(
                stmt.execution_options(populate_existing=True)
            ).scalars()
        )

    def update_round_state(
        self,
        *,
        round_id: uuid.UUID,
        expected_lock_version: int,
        state: str,
        cancel_reason: str | None = None,
    ) -> int:
        """条件 UPDATE（`WHERE lock_version = :v`）。返回受影响行数（0 = 版本不匹配）。"""
        values: dict[str, Any] = {
            "state": state,
            "updated_at": text("now()"),
            "lock_version": TrialRound.lock_version + 1,
        }
        if state == ROUND_STATE_CANCELLED:
            values["cancel_reason"] = cancel_reason
        result = self._session.execute(
            update(TrialRound)
            .where(
                TrialRound.id == round_id,
                TrialRound.lock_version == expected_lock_version,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    def touch_round(self, *, round_id: uuid.UUID, expected_lock_version: int) -> int:
        """只推进 `updated_at` / `lock_version`（名额集合变了，ETag 要跟着变）。"""
        result = self._session.execute(
            update(TrialRound)
            .where(
                TrialRound.id == round_id,
                TrialRound.lock_version == expected_lock_version,
            )
            .values(updated_at=text("now()"), lock_version=TrialRound.lock_version + 1)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------
    # 名额
    # ------------------------------------------------------------------

    def insert_slot(self, **values: Any) -> TrialRoundSlot:
        row = TrialRoundSlot(**values)
        self._session.add(row)
        self._session.flush()
        self._session.refresh(row)
        return row

    def get_slot(self, slot_id: uuid.UUID) -> TrialRoundSlot | None:
        return self._session.execute(
            select(TrialRoundSlot)
            .where(TrialRoundSlot.id == slot_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def list_slots(self, round_id: uuid.UUID) -> list[TrialRoundSlot]:
        return list(
            self._session.execute(
                select(TrialRoundSlot)
                .where(TrialRoundSlot.round_id == round_id)
                .order_by(TrialRoundSlot.slot_no)
                .execution_options(populate_existing=True)
            ).scalars()
        )

    def max_slot_no(self, round_id: uuid.UUID) -> int:
        return int(
            self._session.execute(
                text(
                    "SELECT COALESCE(max(slot_no), 0) FROM trial_round_slots"
                    " WHERE round_id = :r"
                ),
                {"r": round_id},
            ).scalar_one()
        )

    def count_replacement_slots(self, round_id: uuid.UUID) -> int:
        return int(
            self._session.execute(
                text(
                    "SELECT count(*) FROM trial_round_slots"
                    " WHERE round_id = :r AND replaces_slot_id IS NOT NULL"
                ),
                {"r": round_id},
            ).scalar_one()
        )

    def slot_replaced_by(self, slot_id: uuid.UUID) -> uuid.UUID | None:
        row = self._session.execute(
            text("SELECT id FROM trial_round_slots WHERE replaces_slot_id = :s"),
            {"s": slot_id},
        ).first()
        return None if row is None else row[0]

    def slot_holding_content(self, content_item_id: uuid.UUID) -> uuid.UUID | None:
        row = self._session.execute(
            text("SELECT id FROM trial_round_slots WHERE content_item_id = :c"),
            {"c": content_item_id},
        ).first()
        return None if row is None else row[0]

    def bind_slot_content(
        self, *, slot_id: uuid.UUID, content_item_id: uuid.UUID
    ) -> int:
        """write-once 绑定：`WHERE content_item_id IS NULL`。返回受影响行数。"""
        result = self._session.execute(
            update(TrialRoundSlot)
            .where(
                TrialRoundSlot.id == slot_id,
                TrialRoundSlot.content_item_id.is_(None),
            )
            .values(
                content_item_id=content_item_id,
                bound_at=text("now()"),
                updated_at=text("now()"),
            )
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    def live_scene_counts(self, project_id: uuid.UUID) -> dict[str, int]:
        """本项目全部在役名额（非 REPLACED、所属轮非 CANCELLED）按方向计数。"""
        rows = self._session.execute(
            text(
                "SELECT s.scene_key, count(*)"
                "  FROM trial_round_slots s"
                "  JOIN trial_rounds r ON r.id = s.round_id"
                " WHERE r.project_id = :p AND r.state <> 'CANCELLED'"
                "   AND NOT EXISTS (SELECT 1 FROM trial_round_slots x"
                "                    WHERE x.replaces_slot_id = s.id)"
                " GROUP BY s.scene_key"
            ),
            {"p": project_id},
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def slot_facts(self, round_id: uuid.UUID) -> list[SlotFact]:
        """本轮全部名额 + 其内容在"当前版本"上的事实（一次查询，同一快照）。"""
        rows = self._session.execute(text(_SLOT_FACTS_SQL), {"rid": round_id}).fetchall()
        return [
            SlotFact(
                slot_id=r[0],
                round_id=r[1],
                slot_no=r[2],
                scene_key=r[3],
                form_key=r[4],
                content_item_id=r[5],
                replaces_slot_id=r[6],
                replaced=bool(r[7]),
                content_state=r[8],
                current_version_id=r[9],
                review_id=r[10],
                review_decision=r[11],
                any_review=bool(r[12]),
                review_on_first_version=bool(r[13]),
                open_blocking=bool(r[14]),
                self_checked=bool(r[15]),
            )
            for r in rows
        ]

    def lock_contents_shared(self, content_ids: Sequence[uuid.UUID]) -> None:
        """对在役内容 `FOR SHARE`：挡住并发改稿推进 `current_version_id`（R13 TOCTOU）。"""
        if not content_ids:
            return
        self._session.execute(
            text(
                "SELECT id FROM content_items WHERE id = ANY(CAST(:ids AS uuid[]))"
                " ORDER BY id FOR SHARE"
            ),
            {"ids": list(content_ids)},
        ).fetchall()

    # ------------------------------------------------------------------
    # 决定
    # ------------------------------------------------------------------

    def insert_decision(self, **values: Any) -> TrialGateDecision:
        row = TrialGateDecision(**values)
        self._session.add(row)
        self._session.flush()
        self._session.refresh(row)
        return row

    def get_decision_by_round(self, round_id: uuid.UUID) -> TrialGateDecision | None:
        return self._session.execute(
            select(TrialGateDecision)
            .where(TrialGateDecision.round_id == round_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    # ------------------------------------------------------------------
    # 跨模块只读（裸 SQL）
    # ------------------------------------------------------------------

    def active_baseline_id(self, project_id: uuid.UUID) -> uuid.UUID | None:
        """`projects.active_baseline_id` 投影；NULL 即未满足（不回落"最新基线"）。"""
        row = self._session.execute(
            text("SELECT active_baseline_id FROM projects WHERE id = :p"),
            {"p": project_id},
        ).first()
        return None if row is None else row[0]

    def baseline_brief_version_id(self, baseline_id: uuid.UUID) -> uuid.UUID | None:
        row = self._session.execute(
            text("SELECT brief_version_id FROM production_baselines WHERE id = :b"),
            {"b": baseline_id},
        ).first()
        return None if row is None else row[0]

    def has_effective_approval(
        self,
        *,
        project_id: uuid.UUID,
        step_no: int,
        subject_kind: str,
        subject_version_id: uuid.UUID | None,
    ) -> bool:
        """是否存在**有效**（链尾，不是链根）且 `APPROVED` 的步骤批准。

        `subject_version_id=None` 只用于 RESEARCH（无"当前版本"钉点，偏宽，Q3）。
        口径必须与 `BaselineRepository.find_effective_approval` 一致（测试里对拍）。
        """
        sql = (
            "SELECT 1 FROM step_approvals a"
            " WHERE a.project_id = :p AND a.step_no = :n AND a.subject_kind = :k"
            "   AND a.decision = 'APPROVED'"
            "   AND NOT EXISTS (SELECT 1 FROM step_approvals c WHERE c.supersedes_id = a.id)"
        )
        params: dict[str, Any] = {"p": project_id, "n": step_no, "k": subject_kind}
        if subject_version_id is not None:
            sql += " AND a.subject_version_id = :v"
            params["v"] = subject_version_id
        return self._session.execute(text(sql + " LIMIT 1"), params).first() is not None

    def version_node(self, node_id: uuid.UUID) -> dict[str, Any] | None:
        row = self._session.execute(
            text(
                "SELECT entity_type, entity_id, project_id, payload_hash"
                "  FROM version_nodes WHERE id = :n"
            ),
            {"n": node_id},
        ).first()
        if row is None:
            return None
        return {
            "entity_type": row[0],
            "entity_id": row[1],
            "project_id": row[2],
            "payload_hash": row[3],
        }

    def content_row(self, content_id: uuid.UUID) -> dict[str, Any] | None:
        row = self._session.execute(
            text(
                "SELECT project_id, state, scene_key, form_key, generation_request"
                "  FROM content_items WHERE id = :c"
            ),
            {"c": content_id},
        ).first()
        if row is None:
            return None
        return {
            "project_id": row[0],
            "state": row[1],
            "scene_key": row[2],
            "form_key": row[3],
            "generation_request": row[4],
        }

    def now(self) -> datetime:
        return self._session.execute(text("SELECT now()")).scalar_one()


__all__ = [
    "CURRENT_VERSION_SQL",
    "EFFECTIVE_REVIEW_LATERAL_SQL",
    "TrialRepository",
]
