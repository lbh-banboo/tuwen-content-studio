"""模型网关的存储访问（WP16）。

**本层最要紧的三件事**：

1. **`provider_calls` 的写入必须"先持久化再发出去"**。
   数据结构 §13.4 原句："供应商调用前持久化 `request_key`，恢复先检查已有 `provider_call`"。
   因此 `insert_call_if_absent` 用 `ON CONFLICT DO NOTHING` + 回查，
   并且**不加 commit**（由外层事务统一提交，与 WP12/WP13 同一模式）。

2. **状态迁移必须用条件 UPDATE**（`WHERE state IN (...)` + `rowcount` 判定），
   而不是"读出来 → 改属性 → flush"。理由是并发：两个 Worker
   可能同时看到同一行（一个在被回收、一个刚接手），
   "读-改-写"会让后写的覆盖先写的，而**两者的意图完全不同**
   （一个要标 SUCCEEDED、一个要标 UNKNOWN）。
   与 WP15 的 `resolve_work_item` 同一手法。

3. **CALL 的 `state` 迁移必须由调用方显式给出 `from_states`**。
   本层不提供"无条件的 set_state"——那等于允许"从 SUCCEEDED 改回 POLLING"
   这类不可能迁移悄悄发生。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from rednote.modules.gateway.models import (
    CapabilityPolicy,
    GatewayEgress,
    ModelProfile,
    Provider,
    ProviderCall,
)


class GatewayRepository:
    """网关的读写入口。事务边界由调用方（service / 上游处理器）控制。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def flush(self) -> None:
        """把待写变更立刻发给数据库。

        **为什么要显式 flush**：让约束错误在**预期位置**抛出，
        而不是等到 session_scope 退出时提交才炸 —— 那里已经离开
        能给出可读 4xx 的代码位置，只会以 500 冒出去（WP12/WP13 的教训）。
        """
        self._session.flush()

    # ------------------------------------------------------------------
    # 供应商与模型档案
    # ------------------------------------------------------------------

    def insert_provider(
        self,
        *,
        name: str,
        capability_types: list[str],
        network_mode: str,
        endpoint: str,
        secret_ref: str | None = None,
        policy_version: str = "v1",
    ) -> Provider:
        provider = Provider(
            name=name,
            capability_types=list(capability_types),
            network_mode=network_mode,
            endpoint=endpoint,
            secret_ref=secret_ref,
            policy_version=policy_version,
        )
        self._session.add(provider)
        return provider

    def get_provider(self, provider_id: uuid.UUID) -> Provider | None:
        return self._session.get(Provider, provider_id)

    def get_provider_by_name(self, name: str) -> Provider | None:
        stmt = select(Provider).where(Provider.name == name)
        return self._session.execute(stmt).scalar_one_or_none()

    def insert_model_profile(
        self,
        *,
        provider_id: uuid.UUID,
        model_name: str,
        capabilities: list[str],
        revision: str | None = None,
        parameters_schema: dict[str, Any] | None = None,
        pricing_version_id: uuid.UUID | None = None,
        local_weights_hash: str | None = None,
    ) -> ModelProfile:
        profile = ModelProfile(
            provider_id=provider_id,
            model_name=model_name,
            revision=revision,
            capabilities=list(capabilities),
            parameters_schema=parameters_schema,
            pricing_version_id=pricing_version_id,
            local_weights_hash=local_weights_hash,
        )
        self._session.add(profile)
        return profile

    def get_model_profile(self, profile_id: uuid.UUID) -> ModelProfile | None:
        return self._session.get(ModelProfile, profile_id)

    # ------------------------------------------------------------------
    # 出网策略
    # ------------------------------------------------------------------

    def insert_policy(
        self,
        *,
        project_id: uuid.UUID | None,
        policy_version: str,
        external_policy: str,
        allowed_provider_ids: list[uuid.UUID] | None = None,
        allowed_data_classes: list[str] | None = None,
        concurrency_limits: dict[str, Any] | None = None,
    ) -> CapabilityPolicy:
        policy = CapabilityPolicy(
            project_id=project_id,
            policy_version=policy_version,
            external_policy=external_policy,
            allowed_provider_ids=list(allowed_provider_ids or []),
            allowed_data_classes=list(allowed_data_classes or []),
            concurrency_limits=dict(concurrency_limits or {}),
        )
        self._session.add(policy)
        return policy

    def get_policy(self, project_id: uuid.UUID) -> CapabilityPolicy | None:
        """取项目当前生效的策略 = 该项目**最近创建**的那一条。

        按 `created_at DESC, id DESC` 定序（而不是按 `policy_version` 字符串排序）：
        `policy_version` 是字符串，字典序与时间序不保证一致
        （`"v9" > "v10"`）。用创建时间才是"最新"的可靠口径；
        `id` 作为平局键，避免同一时刻两条记录的顺序不确定。
        """
        stmt = (
            select(CapabilityPolicy)
            .where(CapabilityPolicy.project_id == project_id)
            .order_by(CapabilityPolicy.created_at.desc(), CapabilityPolicy.id.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    # ------------------------------------------------------------------
    # 外部调用
    # ------------------------------------------------------------------

    def get_call(self, call_id: uuid.UUID) -> ProviderCall | None:
        return self._session.get(ProviderCall, call_id)

    def find_call_by_request_key(
        self, *, provider_id: uuid.UUID, request_key: str
    ) -> ProviderCall | None:
        """按幂等键查已有调用 —— **派发前必须先调它**。

        这是验收第二半的入口：查到了（且非终态）就绝不能再发一次。
        """
        stmt = select(ProviderCall).where(
            ProviderCall.provider_id == provider_id,
            ProviderCall.request_key == request_key,
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def insert_call_if_absent(
        self,
        *,
        job_attempt_id: uuid.UUID,
        provider_id: uuid.UUID,
        request_key: str,
        request_evidence_ref: str,
    ) -> tuple[ProviderCall, bool]:
        """插入一条 `PREPARED` 调用；已存在则返回既有行（`created=False`）。

        **用 `ON CONFLICT DO NOTHING` + 回查，而不是"先查再插"**：
        并发下两个 Worker 同时领到同一件事时，"先查再插"会让两个都插进去，
        而唯一约束会把其中一个打成 `IntegrityError` —— 那时事务已 aborted，
        要再回查就得先回滚到 SAVEPOINT（WP10/ISS-22 踩过）。
        `ON CONFLICT` 让"并发重复"变成一次干净的 no-op。
        """
        stmt = (
            pg_insert(ProviderCall)
            .values(
                job_attempt_id=job_attempt_id,
                provider_id=provider_id,
                request_key=request_key,
                request_evidence_ref=request_evidence_ref,
                state="PREPARED",
            )
            .on_conflict_do_nothing(
                index_elements=["provider_id", "request_key"]
            )
            .returning(ProviderCall.id)
        )
        created_id = self._session.execute(stmt).scalar_one_or_none()
        if created_id is not None:
            created = self._session.get(ProviderCall, created_id)
            assert created is not None
            return created, True

        existing = self.find_call_by_request_key(
            provider_id=provider_id, request_key=request_key
        )
        assert existing is not None  # ON CONFLICT 未插入说明已存在
        return existing, False

    def transition_call(
        self,
        *,
        call_id: uuid.UUID,
        from_states: list[str],
        to_state: str,
        external_request_id: str | None = None,
        submitted_at: datetime | None = None,
        next_poll_at: datetime | None = None,
        status_unknown_reason: str | None = None,
        last_error_code: str | None = None,
        response_evidence_ref: str | None = None,
    ) -> bool:
        """条件状态迁移。返回是否真的改了（`rowcount == 1`）。

        **只更新显式给出的字段**：`None` 表示"这次迁移不涉及它"，
        而不是"把它清空"。若把 `None` 当成清空，一次
        `POLLING → SUBMITTED` 的迁移会顺手抹掉 `external_request_id`——
        而那个 id 恰恰是下次轮询的唯一凭据。
        """
        values: dict[str, Any] = {
            "state": to_state,
            "updated_at": func.now(),
        }
        if external_request_id is not None:
            values["external_request_id"] = external_request_id
        if submitted_at is not None:
            values["submitted_at"] = submitted_at
        if next_poll_at is not None:
            values["next_poll_at"] = next_poll_at
        if status_unknown_reason is not None:
            values["status_unknown_reason"] = status_unknown_reason
        if last_error_code is not None:
            values["last_error_code"] = last_error_code
        if response_evidence_ref is not None:
            values["response_evidence_ref"] = response_evidence_ref

        result = self._session.execute(
            update(ProviderCall)
            .where(
                ProviderCall.id == call_id,
                ProviderCall.state.in_(list(from_states)),
            )
            .values(**values)
        )
        return result.rowcount == 1

    def list_due_polls(
        self, *, now: datetime, limit: int
    ) -> list[ProviderCall]:
        """到点该轮询的调用（走 `ix_provider_calls_poll_due` 部分索引）。"""
        stmt = (
            select(ProviderCall)
            .where(
                ProviderCall.state.in_(["SUBMITTED", "POLLING"]),
                ProviderCall.next_poll_at.is_not(None),
                ProviderCall.next_poll_at <= now,
            )
            .order_by(ProviderCall.next_poll_at.asc())
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # 出网留痕（追加且不可篡改；本层只写不读改）
    # ------------------------------------------------------------------

    def insert_egress(
        self,
        *,
        project_id: uuid.UUID,
        provider_id: uuid.UUID,
        capability: str,
        data_classes: list[str],
        decision: str,
        policy_version: str,
        trace_id: str,
        provider_call_id: uuid.UUID | None = None,
        blocked_reason: str | None = None,
        prompt_version: str | None = None,
        payload_digest: str | None = None,
    ) -> GatewayEgress:
        """写一条出网留痕。**不加 commit**（与调用同事务）。"""
        row = GatewayEgress(
            project_id=project_id,
            provider_id=provider_id,
            provider_call_id=provider_call_id,
            capability=capability,
            data_classes=list(data_classes),
            decision=decision,
            blocked_reason=blocked_reason,
            policy_version=policy_version,
            prompt_version=prompt_version,
            trace_id=trace_id,
            payload_digest=payload_digest,
        )
        self._session.add(row)
        return row

    def list_egress(
        self, *, project_id: uuid.UUID, limit: int = 100
    ) -> list[GatewayEgress]:
        stmt = (
            select(GatewayEgress)
            .where(GatewayEgress.project_id == project_id)
            .order_by(GatewayEgress.created_at.asc())
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars().all())

    def count_allowed_egress(self, *, project_id: uuid.UUID) -> int:
        """该项目**真的出过网**的次数（`decision='ALLOWED'`）。

        这是 ADR-10 违规判定口径的查询形态：
        "任何一条 `LOCAL_ONLY` 项目的出网记录 = 该次调用违规" ——
        因此对 `LOCAL_ONLY` 项目调它，期望值恒为 `0`。
        **被拒的（DENIED）不计入** —— 那一次什么都没出去。
        """
        stmt = select(func.count()).select_from(GatewayEgress).where(
            GatewayEgress.project_id == project_id,
            GatewayEgress.decision == "ALLOWED",
        )
        return int(self._session.execute(stmt).scalar_one())


__all__ = ["GatewayRepository"]
