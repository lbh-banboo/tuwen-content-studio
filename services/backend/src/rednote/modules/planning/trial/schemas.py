"""WP31 请求 / 响应体（协议 §1 的 camelCase 契约）。

本文件用 `alias_generator=to_camel` 一次性生成 camelCase 别名（本包字段全部是
snake_case <-> camelCase 的机械对应，没有例外键）；请求体 `extra="forbid"`（OP-156）。
取值域校验全在 `service.py`（失败给 422 + `details.reason`），这里只做类型约束。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _Request(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True, extra="forbid", alias_generator=to_camel
    )


class _Response(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


class CreateRoundRequestBody(_Request):
    kind: str
    plan_version_id: uuid.UUID
    quantity: int | None = None
    source_round_id: uuid.UUID | None = None
    scene_keys: list[str] | None = None


class TransitionRequestBody(_Request):
    to_state: str
    reason: str | None = None


class BindContentRequestBody(_Request):
    content_item_id: uuid.UUID


class GateDecisionRequestBody(_Request):
    approved_scene_keys: list[str]
    reason: str


class EntryConditionBody(_Response):
    code: str
    #: None = 本次未评估（不是 False）。
    satisfied: bool | None
    detail: dict[str, Any]


class EntryLimitsBody(_Response):
    trial: int | None
    source: str | None


class EntryCheckBody(_Response):
    ok: bool
    conditions: list[EntryConditionBody]
    limits: EntryLimitsBody


class RatioBody(_Response):
    numerator: int
    denominator: int
    permille: int | None
    rounded: bool


class StatisticsBody(_Response):
    live_slots: int
    replaced_slots: int
    generated: int
    failed: int
    approved: int
    eliminated: int
    pending: int
    unbound: int
    approved_on_first_version: int
    not_self_checked: int
    approval_rate: RatioBody
    by_scene: dict[str, dict[str, int]]


class SlotBody(_Response):
    id: uuid.UUID
    slot_no: int
    scene_key: str
    form_key: str | None
    content_item_id: uuid.UUID | None
    bound_at: datetime | None
    replaces_slot_id: uuid.UUID | None
    replaced_by_slot_id: uuid.UUID | None
    status: str
    review_stale: bool
    content_state: str | None
    current_version_id: uuid.UUID | None
    review_id: uuid.UUID | None
    open_blocking_feedback: bool | None
    self_checked: bool | None


class GateDecisionBody(_Response):
    id: uuid.UUID
    round_id: uuid.UUID
    approved_scene_keys: list[str]
    condition_results: dict[str, Any]
    statistics: dict[str, Any]
    basis: list[dict[str, Any]]
    reason: str
    decided_by: uuid.UUID | None
    decided_at: datetime
    stale: bool
    stale_reasons: list[str]


class RoundBody(_Response):
    id: uuid.UUID
    project_id: uuid.UUID
    round_no: int
    kind: str
    state: str
    plan_version_id: uuid.UUID
    baseline_id: uuid.UUID
    source_round_id: uuid.UUID | None
    requested_quantity: int
    planned_quantity: int
    unallocated_quantity: int
    limit_applied: int
    limit_source: str
    cancel_reason: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    etag: str
    statistics: StatisticsBody | None = Field(default=None)


class RoundDetailBody(RoundBody):
    plan_snapshot: dict[str, Any]
    slots: list[SlotBody]
    decision: GateDecisionBody | None


class GateBody(_Response):
    round_id: uuid.UUID
    state: str
    etag: str
    conditions: dict[str, Any]
    directions: list[dict[str, Any]]
    satisfiable: bool
    statistics: StatisticsBody
    decision: GateDecisionBody | None


class GateDecisionResultBody(_Response):
    round: RoundBody
    decision: GateDecisionBody


class ReplacementResultBody(_Response):
    slot: SlotBody
    round: RoundBody
