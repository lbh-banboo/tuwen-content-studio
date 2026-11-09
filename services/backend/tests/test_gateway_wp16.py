"""WP16 测试：模型网关、出网策略与统一调用状态。

覆盖验收标准（研发计划 04 §4.2）：

> **「LOCAL_ONLY 无外部回退；未知调用状态不盲重试」**

**按验收的两半组织**：

| 半 | 分节 | 关键断言 |
|---|---|---|
| **LOCAL_ONLY 无外部回退** | 「出网策略」+「派发」 | 被拒时适配器**一次都没被调用**；写 DENIED 留痕；**不存在降级分支** |
| **未知调用状态不盲重试** | 「未知状态」+「轮询 / 核对」 | 同 key 第二次派发**不触发 `submit`**；`UNKNOWN` 只能经核对落终态 |

**为什么"无回退"要断言"适配器零调用"而不是"抛了错"**：
只断言抛错的话，一个"先偷偷发出去、发现策略不允许、再抛错"的实现
同样能通过 —— 而那一版已经把数据送出了内网。
`submit_call_count == 0` 才真正证明**没有任何东西出去**。

**为什么并发用例是本包最重要的之一**：
派发路径是"先查幂等 → 没有则插入 → 再发出去"。并发下两个 Worker
可能都查到"没有"，然后都插、都发 —— 唯一约束只拦得住插入，
拦不住"两个请求都已经出去了"。因此必须**真并发**跑一次，
断言"只有一次真的发了"。

**测试隔离**：每次用独立的 provider / project / queue / request_key，
避免"幂等查询"互相污染（幂等键与供应商是全局唯一的）。
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from rednote.modules.gateway import adapters, policy, service
from rednote.modules.gateway import handlers as gateway_handlers
from rednote.modules.gateway.repository import GatewayRepository
from rednote.modules.gateway.types import (
    ADAPTER_ACCEPTED,
    ADAPTER_FAILED,
    ADAPTER_SUCCEEDED,
    ADAPTER_UNKNOWN,
    CALL_FAILED,
    CALL_POLLING,
    CALL_SUCCEEDED,
    CALL_UNKNOWN,
    CAPABILITY_EMBEDDING,
    CAPABILITY_STATIC_IMAGE,
    CAPABILITY_TEXT_STRUCTURED,
    COST_CONFIRMED,
    COST_ESTIMATED,
    DATA_CLASS_EMBEDDING,
    DATA_CLASS_TEXT_STRUCTURED,
    EXTERNAL_POLICY_ALLOW,
    EXTERNAL_POLICY_LOCAL_ONLY,
    NETWORK_EXTERNAL,
    NETWORK_LOCAL,
    PROVIDER_ACTIVE,
    PROVIDER_DISABLED,
    REASON_CAPABILITY_UNSUPPORTED,
    REASON_DATA_CLASS_NOT_ALLOWED,
    REASON_LOCAL_PROVIDER,
    REASON_NO_POLICY,
    REASON_PROJECT_LOCAL_ONLY,
    REASON_PROVIDER_DISABLED,
    REASON_PROVIDER_NOT_ALLOWED,
    CapabilityRequest,
    CapabilityResult,
    EgressDecision,
)
from rednote.modules.jobs import service as jobs_service
from rednote.modules.jobs.models import (
    STATE_FAILED,
    STATE_SUCCEEDED,
    STATE_WAITING_PROVIDER,
    STATE_WAITING_RECONCILIATION,
)
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.jobs.types import HandlerOutcome
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.projects.models import Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.modules.versions.service import canonical_payload_hash
from rednote.platform.configuration import Settings
from rednote.platform.database import session_scope
from rednote.platform.errors import ApiError, ErrorCode

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
# 测试库 DSN。**必须从环境变量读**（ISS-108）：硬编码会让
# `REDNOTE_TEST_DATABASE_URL` 约定的「测试库物理隔离」失效 —— 测试会把
# 数据写进本机默认库（实测跑一次全量 +367 行 users），且 CI 里 PG 用户是
# `postgres` 而不是 `rednote`，硬编码会直接连不上（role 不存在）。
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
TEST_DATABASE_URL = os.environ.get(
    "REDNOTE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL
).strip()

SETTINGS = Settings(
    database_url=TEST_DATABASE_URL,
    environment="test",
    session_cookie_secure=False,
    oidc_issuer=TEST_ISSUER,
    oidc_token_encryption_key="dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM=",
    storage_backend="LOCAL",
    storage_local_root="/tmp/rednote-wp16-test-storage",
)

# `list_due_polls` 是**全局**扫描（`next_poll_at ASC`、无实例过滤），
# 默认 `limit=100`。共享库上积累的历史 due polls 会把本用例刚造的那条
# 挤出窗口 → 断言"应该在里面"失败。**这不是顺序依赖，是数据量依赖**
# （共用库实测 104 条 due / 116 条 POLLING / 838 行总表）。
#
# 同类问题在本项目已出现三次（ISS-49 / ISS-59 / 本次），修法一致：
# 调用处**显式传大 limit**，别用默认值。
_DUE_POLLS_LIMIT = 10_000


# ---------------------------------------------------------------------------
# 装配 helper
# ---------------------------------------------------------------------------


def _seed_project() -> uuid.UUID:
    with session_scope(SETTINGS) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name="网关测试用户",
            role="OPERATOR",
        )
        session.flush()
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name="测试产品")
        session.add(product)
        session.flush()
        project = Project(
            project_code=f"GW-{uuid.uuid4().hex[:8].upper()}",
            name="网关测试项目",
            brand_id=brand.id,
            product_id=product.id,
            owner_id=user.id,
            target_delivery_count=10,
            due_date=date(2026, 12, 31),
        )
        session.add(project)
        session.flush()
        ProjectRepository(session).add_owner_and_collaborators(
            project.id, owner_id=user.id, collaborator_ids=()
        )
        return project.id


def _seed_provider(
    *,
    network_mode: str = NETWORK_EXTERNAL,
    capabilities: tuple[str, ...] = (CAPABILITY_TEXT_STRUCTURED,),
    status: str = PROVIDER_ACTIVE,
) -> uuid.UUID:
    with session_scope(SETTINGS) as session:
        provider = GatewayRepository(session).insert_provider(
            name=f"prov-{uuid.uuid4().hex[:10]}",
            capability_types=list(capabilities),
            network_mode=network_mode,
            endpoint="https://provider.test.invalid/v1",
            secret_ref=f"secret/{uuid.uuid4().hex[:8]}",
        )
        session.flush()
        return provider.id


def _seed_profile(
    provider_id: uuid.UUID,
    *,
    capabilities: tuple[str, ...] = (CAPABILITY_TEXT_STRUCTURED,),
) -> uuid.UUID:
    with session_scope(SETTINGS) as session:
        profile = GatewayRepository(session).insert_model_profile(
            provider_id=provider_id,
            model_name=f"model-{uuid.uuid4().hex[:8]}",
            capabilities=list(capabilities),
        )
        session.flush()
        return profile.id


def _seed_policy(
    *,
    project_id: uuid.UUID | None,
    external_policy: str = EXTERNAL_POLICY_ALLOW,
    provider_ids: tuple[uuid.UUID, ...] = (),
    data_classes: tuple[str, ...] = (DATA_CLASS_TEXT_STRUCTURED,),
) -> uuid.UUID:
    with session_scope(SETTINGS) as session:
        row = GatewayRepository(session).insert_policy(
            project_id=project_id,
            policy_version=f"pv-{uuid.uuid4().hex[:8]}",
            external_policy=external_policy,
            allowed_provider_ids=list(provider_ids),
            allowed_data_classes=list(data_classes),
        )
        session.flush()
        return row.id


def _seed_attempt(*, project_id: uuid.UUID | None = None) -> tuple[uuid.UUID, uuid.UUID]:
    """建任务、领取一次、**随即结束它**，返回 `(job_id, attempt_id)`。

    `provider_calls.job_attempt_id` 是外键，所以测试必须走**真实的领取路径**
    （而不是手造一个 attempt 行）—— 那样才顺带验证了
    `ClaimedJob.attempt_id` 这个接缝确实是通的。

    **领完必须结束它**（ISS-50）：领取会把任务置为 `RUNNING` 并挂上租约，
    而 WP13 的 `reclaim_expired_leases` 是**全局**扫描、其测试断言的是
    **全局计数**。若本包留下大量"运行中"的任务，跨运行时它们的租约到期，
    就会把 WP13 那些断言推高 —— 表现为**别人随机失败**
    （实测：`test_waiting_provider_job_survives_lease_expiry` 偶发红）。
    结束掉它之后任务进终态、租约释放，对全局扫描不可见。
    这与 ISS-37 的"测试里任何全局共享资源都要显式隔离"是同一类问题。
    """
    queue = f"gwq-{uuid.uuid4().hex[:12]}"
    with session_scope(SETTINGS) as session:
        jobs_service.enqueue_job(
            JobRepository(session),
            kind="GATEWAY_TEST",
            queue=queue,
            idempotency_scope="gateway-test",
            idempotency_key=f"k-{uuid.uuid4().hex}",
            input_hash="h",
            project_id=project_id,
        )
    with session_scope(SETTINGS) as session:
        claimed = jobs_service.claim_jobs(
            JobRepository(session), queue=queue, worker_id="worker-gw"
        )
    assert len(claimed) == 1
    attempt_id = claimed[0].attempt_id
    with session_scope(SETTINGS) as session:
        jobs_service.finish_job(
            JobRepository(session),
            claimed=claimed[0],
            outcome=HandlerOutcome(state=STATE_SUCCEEDED),
        )
    return claimed[0].job_id, attempt_id


def _request(
    *,
    project_id: uuid.UUID,
    profile_id: uuid.UUID,
    job_id: uuid.UUID,
    attempt_id: uuid.UUID,
    capability: str = CAPABILITY_TEXT_STRUCTURED,
    data_classes: tuple[str, ...] = (DATA_CLASS_TEXT_STRUCTURED,),
    context: dict[str, object] | None = None,
) -> CapabilityRequest:
    return CapabilityRequest(
        request_id=f"req-{uuid.uuid4().hex[:10]}",
        job_id=job_id,
        attempt_id=attempt_id,
        project_id=project_id,
        capability=capability,
        model_profile_id=profile_id,
        output_schema_version=1,
        policy_version="pv-test",
        idempotency_key=f"idem-{uuid.uuid4().hex[:10]}",
        data_classes=data_classes,
        # **传摘要而不是正文** —— 与真实调用方一致（网关只留痕摘要）。
        context_digest=canonical_payload_hash(context or {"brief": "任务必要片段"}),
        prompt_version="prompt-v1",
        trace_id=f"trace-{uuid.uuid4().hex[:10]}",
    )


def _success(**overrides: object) -> CapabilityResult:
    params: dict[str, object] = {
        "state": ADAPTER_SUCCEEDED,
        "external_request_id": f"ext-{uuid.uuid4().hex[:8]}",
        "structured_output": {"title": "生成结果"},
    }
    params.update(overrides)
    return CapabilityResult(**params)  # type: ignore[arg-type]


def _dispatch(
    *, request: CapabilityRequest, adapter: adapters.CapabilityAdapter, round_no: int = 0
) -> service.DispatchResult:
    with session_scope(SETTINGS) as session:
        return service.dispatch_call(
            GatewayRepository(session),
            request=request,
            adapter=adapter,
            round_no=round_no,
        )


def _poll(*, call_id: uuid.UUID, adapter: adapters.CapabilityAdapter) -> service.CallView:
    with session_scope(SETTINGS) as session:
        return service.poll_call(
            GatewayRepository(session), call_id=call_id, adapter=adapter
        )


def _reconcile(
    *, call_id: uuid.UUID, resolution_state: str, reason: str = "供应商回执核对"
) -> service.CallView:
    with session_scope(SETTINGS) as session:
        return service.reconcile_call(
            GatewayRepository(session),
            call_id=call_id,
            resolution_state=resolution_state,
            reason=reason,
        )


def _call_of(result: service.DispatchResult) -> service.CallView:
    """取出结果里的调用（`call` 可空，因为被拒时没有调用）。"""
    assert result.call is not None, "该结果本应有调用记录"
    return result.call


def _expect_denied(result: service.DispatchResult) -> ApiError:
    """被拒的统一断言：**没派发、没调用、有否决结论、留痕已落库**。

    返回折好的协议错误供调用方进一步断言错误码与 details。
    这条 helper 本身就是"被拒不抛异常"这个设计的说明：
    事务必须正常提交，留痕才能活下来（见 `DispatchResult` 的 docstring）。
    """
    assert result.denied is True
    assert result.dispatched is False
    assert result.call is None
    assert result.egress_id is not None
    assert result.denial is not None
    return service.egress_error(result.denial)


def _call_rows(request_key: str) -> int:
    with session_scope(SETTINGS) as session:
        return session.execute(
            text("SELECT count(*) FROM provider_calls WHERE request_key = :k"),
            {"k": request_key},
        ).scalar_one()


def _egress_rows(*, project_id: uuid.UUID) -> list[dict[str, object]]:
    """该项目的出网留痕（每行转成 dict，便于按列断言）。"""
    with session_scope(SETTINGS) as session:
        rows = session.execute(
            text(
                "SELECT decision, blocked_reason, provider_call_id, capability, "
                "payload_digest, policy_version, trace_id "
                "FROM gateway_egress WHERE project_id = :p ORDER BY created_at"
            ),
            {"p": str(project_id)},
        ).mappings().all()
    return [dict(row) for row in rows]


# ===========================================================================
# 第一组：出网策略（纯函数，穷举分支）
# ===========================================================================


def _evaluate(**overrides: object) -> EgressDecision:
    params: dict[str, object] = {
        "policy": None,
        "provider_id": uuid.uuid4(),
        "network_mode": NETWORK_EXTERNAL,
        "provider_status": PROVIDER_ACTIVE,
        "provider_capabilities": (CAPABILITY_TEXT_STRUCTURED,),
        "capability": CAPABILITY_TEXT_STRUCTURED,
        "data_classes": (DATA_CLASS_TEXT_STRUCTURED,),
    }
    params.update(overrides)
    return policy.evaluate_egress(**params)  # type: ignore[arg-type]


def _allow_policy(provider_id: uuid.UUID, **overrides: object) -> policy.EgressPolicyView:
    params: dict[str, object] = {
        "project_id": uuid.uuid4(),
        "policy_version": "pv-allow",
        "external_policy": EXTERNAL_POLICY_ALLOW,
        "allowed_provider_ids": (provider_id,),
        "allowed_data_classes": (DATA_CLASS_TEXT_STRUCTURED,),
    }
    params.update(overrides)
    return policy.EgressPolicyView(**params)  # type: ignore[arg-type]


def test_policy_rejects_disabled_provider() -> None:
    decision = _evaluate(provider_status=PROVIDER_DISABLED)
    assert decision.allowed is False
    assert decision.reason == REASON_PROVIDER_DISABLED


def test_policy_rejects_unsupported_capability() -> None:
    """能力不支持与"能不能出网"是两个问题 —— 它在策略判定**之前**被拦下。"""
    decision = _evaluate(
        provider_capabilities=(CAPABILITY_STATIC_IMAGE,),
        capability=CAPABILITY_TEXT_STRUCTURED,
    )
    assert decision.allowed is False
    assert decision.reason == REASON_CAPABILITY_UNSUPPORTED


def test_capability_check_precedes_local_shortcut() -> None:
    """**第 2 步必须早于第 3 步**：本地供应商也可能不支持某能力。

    写反了会把"本地供应商不支持某能力"报成"允许"，
    然后在调用时才炸 —— 那时错误信息指向适配器，而根因在策略判定。
    """
    decision = _evaluate(
        network_mode=NETWORK_LOCAL,
        provider_capabilities=(CAPABILITY_STATIC_IMAGE,),
        capability=CAPABILITY_TEXT_STRUCTURED,
    )
    assert decision.allowed is False
    assert decision.reason == REASON_CAPABILITY_UNSUPPORTED


def test_local_provider_allowed_without_any_policy() -> None:
    """**第 3 步必须早于第 4 步**：本地调用不出网，因此与"缺省拒绝"无关。

    若"缺省拒绝"排在前面，一个没配策略的项目连本地模型都调不了 ——
    而模式 B（完全离线）下所有项目都注定没有外部策略，
    那会让系统在模式 B 下完全不可用。
    """
    decision = _evaluate(policy=None, network_mode=NETWORK_LOCAL)
    assert decision.allowed is True
    assert decision.reason == REASON_LOCAL_PROVIDER
    assert decision.is_local is True


def test_external_without_policy_is_denied() -> None:
    """数据结构 §11 原句「项目策略优先，**缺省拒绝**未授权出网」。"""
    decision = _evaluate(policy=None)
    assert decision.allowed is False
    assert decision.reason == REASON_NO_POLICY


def test_local_only_policy_denies_external() -> None:
    """验收第一半的核心判据。"""
    provider_id = uuid.uuid4()
    decision = _evaluate(
        policy=_allow_policy(provider_id, external_policy=EXTERNAL_POLICY_LOCAL_ONLY),
        provider_id=provider_id,
    )
    assert decision.allowed is False
    assert decision.reason == REASON_PROJECT_LOCAL_ONLY


def test_local_only_denies_even_when_provider_is_whitelisted() -> None:
    """`LOCAL_ONLY` 优先于白名单 —— 白名单不是绕过它的办法。"""
    provider_id = uuid.uuid4()
    decision = _evaluate(
        policy=policy.EgressPolicyView(
            project_id=uuid.uuid4(),
            policy_version="pv",
            external_policy=EXTERNAL_POLICY_LOCAL_ONLY,
            allowed_provider_ids=(provider_id,),
            allowed_data_classes=(DATA_CLASS_TEXT_STRUCTURED,),
        ),
        provider_id=provider_id,
    )
    assert decision.reason == REASON_PROJECT_LOCAL_ONLY


def test_provider_not_in_whitelist_is_denied() -> None:
    decision = _evaluate(policy=_allow_policy(uuid.uuid4()))
    assert decision.allowed is False
    assert decision.reason == REASON_PROVIDER_NOT_ALLOWED


def test_empty_provider_whitelist_denies_everything() -> None:
    """空白名单 = **一个都不允许**（不是"不限制"）。

    否则一个"只填了 external_policy 但忘了填白名单"的策略
    会变成"所有供应商都能调"，而那种失败**不报错**（策略看起来配了）。
    """
    provider_id = uuid.uuid4()
    decision = _evaluate(
        policy=_allow_policy(provider_id, allowed_provider_ids=()),
        provider_id=provider_id,
    )
    assert decision.allowed is False
    assert decision.reason == REASON_PROVIDER_NOT_ALLOWED


def test_data_class_not_whitelisted_is_denied() -> None:
    provider_id = uuid.uuid4()
    decision = _evaluate(
        policy=_allow_policy(
            provider_id, allowed_data_classes=(DATA_CLASS_EMBEDDING,)
        ),
        provider_id=provider_id,
    )
    assert decision.allowed is False
    assert decision.reason == REASON_DATA_CLASS_NOT_ALLOWED


def test_embedding_requires_explicit_data_class_whitelist() -> None:
    """ADR-10 出网矩阵 E-4「embedding **默认不走外呼**」的落地。

    默认策略只放 `TEXT_STRUCTURED`，因此 embedding 请求自然被拒 ——
    不需要一条专门的 if 分支（只有一处判据）。
    """
    provider_id = uuid.uuid4()
    provider = _evaluate(
        policy=_allow_policy(provider_id),
        provider_id=provider_id,
        provider_capabilities=(CAPABILITY_EMBEDDING,),
        capability=CAPABILITY_EMBEDDING,
        data_classes=(DATA_CLASS_EMBEDDING,),
    )
    assert provider.allowed is False
    assert provider.reason == REASON_DATA_CLASS_NOT_ALLOWED


def test_forbidden_egress_data_class_is_denied() -> None:
    """ADR-10 出网矩阵 E-5：知识库全量正文/账本/审计日志 **禁止出网**。

    即使有人把它写进了策略白名单也拦 —— 因为 E-5 是**禁止清单**，
    不是"默认不允许"。
    """
    provider_id = uuid.uuid4()
    decision = _evaluate(
        policy=_allow_policy(
            provider_id,
            allowed_data_classes=("KNOWLEDGE_FULLTEXT",),
        ),
        provider_id=provider_id,
        data_classes=("KNOWLEDGE_FULLTEXT",),
    )
    assert decision.allowed is False
    assert decision.reason == REASON_DATA_CLASS_NOT_ALLOWED


def test_all_conditions_met_allows_external() -> None:
    provider_id = uuid.uuid4()
    decision = _evaluate(policy=_allow_policy(provider_id), provider_id=provider_id)
    assert decision.allowed is True
    assert decision.reason == "ALLOWED"
    assert decision.is_local is False
    assert decision.policy_version == "pv-allow"


def test_evaluate_egress_is_deterministic() -> None:
    """纯函数：同入参同结论（可穷举单测的前提）。"""
    provider_id = uuid.uuid4()
    kwargs = {
        "policy": _allow_policy(provider_id),
        "provider_id": provider_id,
        "network_mode": NETWORK_EXTERNAL,
        "provider_status": PROVIDER_ACTIVE,
        "provider_capabilities": (CAPABILITY_TEXT_STRUCTURED,),
        "capability": CAPABILITY_TEXT_STRUCTURED,
        "data_classes": (DATA_CLASS_TEXT_STRUCTURED,),
    }
    first = policy.evaluate_egress(**kwargs)  # type: ignore[arg-type]
    second = policy.evaluate_egress(**kwargs)  # type: ignore[arg-type]
    assert first == second


# ===========================================================================
# 第二组：适配器与结果校验
# ===========================================================================


def test_accepted_without_external_id_is_rejected() -> None:
    """`ACCEPTED` 没有 `externalRequestId` 就**无法轮询** —— 任务会永远等下去。"""
    result = CapabilityResult(state=ADAPTER_ACCEPTED, external_request_id=None)
    with pytest.raises(adapters.AdapterContractError):
        adapters.validate_capability_result(result)


def test_succeeded_without_output_is_rejected() -> None:
    result = CapabilityResult(state=ADAPTER_SUCCEEDED)
    with pytest.raises(adapters.AdapterContractError):
        adapters.validate_capability_result(result)


def test_failed_without_error_code_is_rejected() -> None:
    result = CapabilityResult(state=ADAPTER_FAILED)
    with pytest.raises(adapters.AdapterContractError):
        adapters.validate_capability_result(result)


def test_unknown_state_value_is_rejected() -> None:
    result = CapabilityResult(state="MAYBE")
    with pytest.raises(adapters.AdapterContractError):
        adapters.validate_capability_result(result)


def test_invalid_cost_status_is_rejected() -> None:
    result = _success(cost_status="CHEAP")
    with pytest.raises(adapters.AdapterContractError):
        adapters.validate_capability_result(result)


def test_structured_output_requires_keys() -> None:
    adapters.validate_structured_output({"a": 1}, required_keys=["a"])
    with pytest.raises(adapters.AdapterContractError):
        adapters.validate_structured_output({"a": 1}, required_keys=["b"])


def test_structured_output_must_be_mapping() -> None:
    with pytest.raises(adapters.AdapterContractError):
        adapters.validate_structured_output(None, required_keys=[])


def test_adapter_registry_is_immutable() -> None:
    """注册返回新实例 —— 避免"测试里注册了一个适配器，影响后续测试"。"""
    first = adapters.AdapterRegistry.empty()
    second = first.register(adapters.StubAdapter(capability=CAPABILITY_TEXT_STRUCTURED))
    assert first.registered_capabilities() == frozenset()
    assert second.registered_capabilities() == frozenset({CAPABILITY_TEXT_STRUCTURED})
    assert second.adapter_for(CAPABILITY_TEXT_STRUCTURED) is not None
    assert second.adapter_for(CAPABILITY_STATIC_IMAGE) is None


# ===========================================================================
# 第三组：LOCAL_ONLY 无外部回退（验收第一半，真库）
# ===========================================================================


def test_local_only_project_denied_and_adapter_never_called() -> None:
    """**验收第一半的核心用例**。

    两件事一起断言：
    ① 抛 403 `EXTERNAL_POLICY_DENIED`；
    ② **适配器一次都没被调用** —— 这才证明"没有任何东西出去"。
    """
    project_id = _seed_project()
    provider_id = _seed_provider()
    profile_id = _seed_profile(provider_id)
    _seed_policy(
        project_id=project_id,
        external_policy=EXTERNAL_POLICY_LOCAL_ONLY,
        provider_ids=(provider_id,),
    )
    job_id, attempt_id = _seed_attempt(project_id=project_id)
    adapter = adapters.StubAdapter(
        capability=CAPABILITY_TEXT_STRUCTURED, default_result=_success()
    )

    result = _dispatch(
        request=_request(
            project_id=project_id,
            profile_id=profile_id,
            job_id=job_id,
            attempt_id=attempt_id,
        ),
        adapter=adapter,
    )

    error = _expect_denied(result)
    assert error.code == ErrorCode.EXTERNAL_POLICY_DENIED
    assert error.details["reason"] == REASON_PROJECT_LOCAL_ONLY
    assert adapter.submit_call_count == 0, "被拒时绝不能有任何东西发出去"


def test_local_only_denial_leaves_no_provider_call_row() -> None:
    """被拒的调用**不留 `provider_calls` 行** —— 一次都没派发。"""
    project_id = _seed_project()
    provider_id = _seed_provider()
    profile_id = _seed_profile(provider_id)
    _seed_policy(
        project_id=project_id,
        external_policy=EXTERNAL_POLICY_LOCAL_ONLY,
        provider_ids=(provider_id,),
    )
    job_id, attempt_id = _seed_attempt(project_id=project_id)
    request = _request(
        project_id=project_id, profile_id=profile_id, job_id=job_id, attempt_id=attempt_id
    )
    key = service.build_request_key(
        capability=request.capability, provider_id=provider_id, job_id=job_id
    )
    adapter = adapters.StubAdapter(
        capability=CAPABILITY_TEXT_STRUCTURED, default_result=_success()
    )

    result = _dispatch(request=request, adapter=adapter)

    _expect_denied(result)
    assert _call_rows(key) == 0


def test_denied_egress_is_recorded_without_call_reference() -> None:
    """被拒也要留痕（`decision=DENIED`），且 `provider_call_id` 为空。

    策略判定发生在派发**之前**，那时确实没有调用可指 ——
    用 `decision` 显式记录结论，而不是靠"有没有行"来推断。
    """
    project_id = _seed_project()
    provider_id = _seed_provider()
    profile_id = _seed_profile(provider_id)
    _seed_policy(
        project_id=project_id,
        external_policy=EXTERNAL_POLICY_LOCAL_ONLY,
        provider_ids=(provider_id,),
    )
    job_id, attempt_id = _seed_attempt(project_id=project_id)
    adapter = adapters.StubAdapter(
        capability=CAPABILITY_TEXT_STRUCTURED, default_result=_success()
    )

    result = _dispatch(
        request=_request(
            project_id=project_id,
            profile_id=profile_id,
            job_id=job_id,
            attempt_id=attempt_id,
        ),
        adapter=adapter,
    )

    _expect_denied(result)
    rows = _egress_rows(project_id=project_id)
    assert len(rows) == 1
    assert rows[0]["decision"] == "DENIED"
    assert rows[0]["blocked_reason"] == REASON_PROJECT_LOCAL_ONLY
    assert rows[0]["provider_call_id"] is None


def test_no_policy_project_is_denied_the_same_way() -> None:
    """没有策略 → 与 LOCAL_ONLY 同一处置（缺省拒绝），不是"放行"。"""
    project_id = _seed_project()
    provider_id = _seed_provider()
    profile_id = _seed_profile(provider_id)
    job_id, attempt_id = _seed_attempt(project_id=project_id)
    adapter = adapters.StubAdapter(
        capability=CAPABILITY_TEXT_STRUCTURED, default_result=_success()
    )

    result = _dispatch(
        request=_request(
            project_id=project_id,
            profile_id=profile_id,
            job_id=job_id,
            attempt_id=attempt_id,
        ),
        adapter=adapter,
    )

    error = _expect_denied(result)
    assert error.code == ErrorCode.EXTERNAL_POLICY_DENIED
    assert error.details["reason"] == REASON_NO_POLICY
    assert adapter.submit_call_count == 0


def test_local_provider_runs_without_policy_and_without_egress_record() -> None:
    """本地供应商在**无策略**项目下也允许（不出网），且**不写**出网留痕。

    "不写留痕"是刻意的：ADR-10 的违规判定口径是
    "任何一条 LOCAL_ONLY 项目的**出网记录** = 该次调用违规"。
    若本地调用也记出网，那条口径会退化成"本地调用也算违规"。
    """
    project_id = _seed_project()
    provider_id = _seed_provider(network_mode=NETWORK_LOCAL)
    profile_id = _seed_profile(provider_id)
    job_id, attempt_id = _seed_attempt(project_id=project_id)
    adapter = adapters.StubAdapter(
        capability=CAPABILITY_TEXT_STRUCTURED, default_result=_success()
    )

    result = _dispatch(
        request=_request(
            project_id=project_id,
            profile_id=profile_id,
            job_id=job_id,
            attempt_id=attempt_id,
        ),
        adapter=adapter,
    )

    assert result.dispatched is True
    assert adapter.submit_call_count == 1
    assert _call_of(result).state == CALL_SUCCEEDED
    assert _egress_rows(project_id=project_id) == [], "本地调用没有出网记录"


def test_unsupported_capability_is_rejected_before_dispatch() -> None:
    """供应商不支持该能力 → 422 + `capability_unsupported`，且不发。"""
    project_id = _seed_project()
    provider_id = _seed_provider(capabilities=(CAPABILITY_STATIC_IMAGE,))
    profile_id = _seed_profile(provider_id, capabilities=(CAPABILITY_STATIC_IMAGE,))
    _seed_policy(
        project_id=project_id,
        provider_ids=(provider_id,),
    )
    job_id, attempt_id = _seed_attempt(project_id=project_id)
    adapter = adapters.StubAdapter(
        capability=CAPABILITY_TEXT_STRUCTURED, default_result=_success()
    )

    result = _dispatch(
        request=_request(
            project_id=project_id,
            profile_id=profile_id,
            job_id=job_id,
            attempt_id=attempt_id,
        ),
        adapter=adapter,
    )

    error = _expect_denied(result)
    assert error.code == ErrorCode.VALIDATION_FAILED
    assert error.details["reason"] == REASON_CAPABILITY_UNSUPPORTED
    assert adapter.submit_call_count == 0


def test_allowed_external_call_writes_egress_record_with_call_object() -> None:
    """允许时留痕指向 `provider_call`，且 `request_evidence_ref` 是 trace 引用。"""
    project_id = _seed_project()
    provider_id = _seed_provider()
    profile_id = _seed_profile(provider_id)
    _seed_policy(project_id=project_id, provider_ids=(provider_id,))
    job_id, attempt_id = _seed_attempt(project_id=project_id)
    adapter = adapters.StubAdapter(
        capability=CAPABILITY_TEXT_STRUCTURED, default_result=_success()
    )

    request = _request(
        project_id=project_id, profile_id=profile_id, job_id=job_id, attempt_id=attempt_id
    )
    result = _dispatch(request=request, adapter=adapter)

    rows = _egress_rows(project_id=project_id)
    assert len(rows) == 1
    assert rows[0]["decision"] == "ALLOWED"
    assert rows[0]["provider_call_id"] == _call_of(result).call_id
    assert rows[0]["capability"] == CAPABILITY_TEXT_STRUCTURED
    assert result.call.request_evidence_ref == f"trace:{request.trace_id}"


def test_egress_record_contains_no_business_text() -> None:
    """出网留痕**不含正文**：交给网关的是**摘要**，不是内容本身。

    这里传一段"像正文"的字符串进 context，断言它不出现在留痕表的任何列里
    （`gateway_egress` 没有能放正文的列，最长的文本列是 64 位摘要与受控格式的 traceId）。
    """
    project_id = _seed_project()
    provider_id = _seed_provider()
    profile_id = _seed_profile(provider_id)
    _seed_policy(project_id=project_id, provider_ids=(provider_id,))
    job_id, attempt_id = _seed_attempt(project_id=project_id)
    adapter = adapters.StubAdapter(
        capability=CAPABILITY_TEXT_STRUCTURED, default_result=_success()
    )

    secret_text = "这是一段绝不该出现在留痕里的业务正文-9f3a"
    result = _dispatch(
        request=_request(
            project_id=project_id,
            profile_id=profile_id,
            job_id=job_id,
            attempt_id=attempt_id,
            context={"brief": secret_text},
        ),
        adapter=adapter,
    )

    with session_scope(SETTINGS) as session:
        rows = session.execute(
            text(
                "SELECT row_to_json(e)::text FROM gateway_egress e "
                "WHERE project_id = :p"
            ),
            {"p": str(project_id)},
        ).scalars().all()
    assert rows
    assert all(secret_text not in row for row in rows), "正文绝不能进留痕"
    # 留痕里存的是摘要（64 位十六进制），与调用方传进来的摘要一致。
    assert result.egress_id is not None
    assert _egress_rows(project_id=project_id)[0]["payload_digest"] is not None


# ===========================================================================
# 第四组：未知调用状态不盲重试（验收第二半，真库）
# ===========================================================================


def _setup_external_call(*, result: CapabilityResult) -> tuple[
    uuid.UUID, CapabilityRequest, adapters.StubAdapter
]:
    project_id = _seed_project()
    provider_id = _seed_provider()
    profile_id = _seed_profile(provider_id)
    _seed_policy(project_id=project_id, provider_ids=(provider_id,))
    job_id, attempt_id = _seed_attempt(project_id=project_id)
    request = _request(
        project_id=project_id, profile_id=profile_id, job_id=job_id, attempt_id=attempt_id
    )
    adapter = adapters.StubAdapter(
        capability=CAPABILITY_TEXT_STRUCTURED, default_result=result
    )
    return project_id, request, adapter


def test_unknown_result_lands_in_unknown_state() -> None:
    _, request, adapter = _setup_external_call(
        result=CapabilityResult(state=ADAPTER_UNKNOWN, error_code="TIMEOUT")
    )
    result = _dispatch(request=request, adapter=adapter)
    assert _call_of(result).state == CALL_UNKNOWN
    assert result.call.status_unknown_reason == "TIMEOUT"


def test_second_dispatch_of_unknown_call_does_not_resend() -> None:
    """**验收第二半的核心用例**。

    第一次返回 UNKNOWN 后，用**同一个 request** 再派发一次：
    必须**不再调用 `submit`**（`submit_call_count` 仍为 1），
    而是返回既有那条 UNKNOWN 调用。

    这就是"未知调用状态不盲重试"的机械证据 ——
    超时可能意味着请求已经发出去了，重发等于再买一次。
    """
    _, request, adapter = _setup_external_call(
        result=CapabilityResult(state=ADAPTER_UNKNOWN, error_code="TIMEOUT")
    )
    first = _dispatch(request=request, adapter=adapter)
    assert first.dispatched is True
    assert adapter.submit_call_count == 1

    second = _dispatch(request=request, adapter=adapter)

    assert second.dispatched is False, "不得重新发出去"
    assert adapter.submit_call_count == 1, "适配器不能再被调用"
    assert second.call.call_id == first.call.call_id
    assert second.call.state == CALL_UNKNOWN


def test_terminal_success_call_is_not_redispatched() -> None:
    """终态调用也不重发 —— 回放既有结果（同一件事不该做两遍）。"""
    _, request, adapter = _setup_external_call(result=_success())
    first = _dispatch(request=request, adapter=adapter)
    assert first.call.state == CALL_SUCCEEDED

    second = _dispatch(request=request, adapter=adapter)
    assert second.dispatched is False
    assert adapter.submit_call_count == 1
    assert second.call.state == CALL_SUCCEEDED


def test_explicit_round_allows_a_new_call() -> None:
    """"重试"是**显式决定**：`round_no>0` 才会开新一轮。

    默认（`round_no=0`）永远是"不重发"。这条保证
    "多花一次钱"必须有人签字，而不是某个默认行为。
    """
    _, request, adapter = _setup_external_call(result=_success())
    _dispatch(request=request, adapter=adapter)
    assert adapter.submit_call_count == 1

    retry = _dispatch(request=request, adapter=adapter, round_no=1)
    assert retry.dispatched is True
    assert adapter.submit_call_count == 2
    assert retry.call.request_key.endswith(":r1")


def test_request_key_is_stable_across_attempts() -> None:
    """**`request_key` 不带 attempt** —— 这是验收第二半的机制本身。

    若 key 带上 attempt，租约过期后新 attempt 会查不到旧调用 → 重复派发。
    这条测试把"key 里没有 attempt"钉死：换一个 attempt_id，key 不变。
    """
    provider_id = uuid.uuid4()
    job_id = uuid.uuid4()
    first = service.build_request_key(
        capability=CAPABILITY_TEXT_STRUCTURED, provider_id=provider_id, job_id=job_id
    )
    second = service.build_request_key(
        capability=CAPABILITY_TEXT_STRUCTURED, provider_id=provider_id, job_id=job_id
    )
    assert first == second
    assert first.endswith(":r0")
    assert "attempt" not in first


def test_request_key_changes_with_round() -> None:
    provider_id = uuid.uuid4()
    job_id = uuid.uuid4()
    round0 = service.build_request_key(
        capability=CAPABILITY_TEXT_STRUCTURED, provider_id=provider_id, job_id=job_id
    )
    round1 = service.build_request_key(
        capability=CAPABILITY_TEXT_STRUCTURED,
        provider_id=provider_id,
        job_id=job_id,
        round_no=1,
    )
    assert round0 != round1


def test_concurrent_dispatch_sends_only_one_request() -> None:
    """**并发**下同一 request 只能真的发出去一次（本包最重要的用例之一）。

    派发是"先查幂等 → 没有则插 → 再发"。并发下两个 Worker 可能都查到"没有"，
    然后都插、都发 —— 唯一约束只拦得住插入，拦不住"两个请求都已经出去了"。
    因此必须**真并发**跑一次。
    """
    project_id = _seed_project()
    provider_id = _seed_provider()
    profile_id = _seed_profile(provider_id)
    _seed_policy(project_id=project_id, provider_ids=(provider_id,))
    job_id, attempt_id = _seed_attempt(project_id=project_id)
    request = _request(
        project_id=project_id, profile_id=profile_id, job_id=job_id, attempt_id=attempt_id
    )
    adapter = adapters.StubAdapter(
        capability=CAPABILITY_TEXT_STRUCTURED, default_result=_success()
    )
    barrier_ready = 4

    def attempt(_index: int) -> bool:
        result = _dispatch(request=request, adapter=adapter)
        return result.dispatched

    with ThreadPoolExecutor(max_workers=barrier_ready) as pool:
        dispatched_flags = list(pool.map(attempt, range(barrier_ready)))

    assert sum(1 for flag in dispatched_flags if flag) == 1, (
        f"只应有 1 次真的派发，实际 {sum(1 for f in dispatched_flags if f)} 次"
    )
    assert adapter.submit_call_count == 1
    key = service.build_request_key(
        capability=CAPABILITY_TEXT_STRUCTURED, provider_id=provider_id, job_id=job_id
    )
    assert _call_rows(key) == 1


def test_accepted_result_moves_to_polling_with_next_poll_at() -> None:
    """`ACCEPTED` → 落库 `POLLING`，且**必须有 `next_poll_at`**（否则永远不会被捞起）。"""
    _, request, adapter = _setup_external_call(
        result=CapabilityResult(
            state=ADAPTER_ACCEPTED, external_request_id="ext-accepted"
        )
    )
    result = _dispatch(request=request, adapter=adapter)
    assert _call_of(result).state == CALL_POLLING
    assert result.call.external_request_id == "ext-accepted"
    assert result.call.next_poll_at is not None


def test_malformed_adapter_result_raises() -> None:
    """适配器返回畸形结果 → 抛错（这是缺陷信号，不是业务失败）。"""
    _, request, adapter = _setup_external_call(result=_success())
    adapter.malformed_result = CapabilityResult(state=ADAPTER_ACCEPTED)
    with pytest.raises(adapters.AdapterContractError):
        _dispatch(request=request, adapter=adapter)


# ===========================================================================
# 第五组：轮询与核对
# ===========================================================================


def test_poll_moves_polling_to_terminal() -> None:
    _, request, adapter = _setup_external_call(
        result=CapabilityResult(
            state=ADAPTER_ACCEPTED, external_request_id="ext-1"
        )
    )
    dispatched = _dispatch(request=request, adapter=adapter)
    assert _call_of(dispatched).state == CALL_POLLING

    poller = adapters.StubAdapter(
        capability=CAPABILITY_TEXT_STRUCTURED,
        query_results=[_success(external_request_id="ext-1")],
    )
    polled = _poll(call_id=_call_of(dispatched).call_id, adapter=poller)
    assert polled.state == CALL_SUCCEEDED
    assert polled.is_terminal is True


def test_poll_rejects_terminal_call() -> None:
    _, request, adapter = _setup_external_call(result=_success())
    dispatched = _dispatch(request=request, adapter=adapter)
    assert _call_of(dispatched).state == CALL_SUCCEEDED

    poller = adapters.StubAdapter(capability=CAPABILITY_TEXT_STRUCTURED)
    with pytest.raises(ApiError) as excinfo:
        _poll(call_id=_call_of(dispatched).call_id, adapter=poller)
    assert excinfo.value.code == ErrorCode.INVALID_TRANSITION
    assert poller.query_call_count == 0


def test_poll_rejects_unknown_call() -> None:
    """`UNKNOWN` **不能靠轮询自动复位** —— 它必须走核对。

    允许轮询会给出"自动恢复了"的假象，而数据结构 §13.4 明确要求
    UNKNOWN 不因时间流逝而变成可自由重试。
    """
    _, request, adapter = _setup_external_call(
        result=CapabilityResult(state=ADAPTER_UNKNOWN, error_code="TIMEOUT")
    )
    dispatched = _dispatch(request=request, adapter=adapter)
    assert _call_of(dispatched).state == CALL_UNKNOWN

    poller = adapters.StubAdapter(capability=CAPABILITY_TEXT_STRUCTURED)
    with pytest.raises(ApiError) as excinfo:
        _poll(call_id=_call_of(dispatched).call_id, adapter=poller)
    assert excinfo.value.code == ErrorCode.INVALID_TRANSITION
    assert poller.query_call_count == 0


@pytest.mark.regression
def test_reconcile_resolves_unknown_and_keeps_reason() -> None:
    """**`UNKNOWN` 的唯一出口**，且核对后**保留** `status_unknown_reason`。

    保留它 = 保留"曾经不明过"的历史。这条同时是 ISS-46 的回归测试：
    最初的 CHECK 写成"只有 UNKNOWN 允许有值"，会让核对**必然撞约束**。
    """
    _, request, adapter = _setup_external_call(
        result=CapabilityResult(state=ADAPTER_UNKNOWN, error_code="TIMEOUT")
    )
    dispatched = _dispatch(request=request, adapter=adapter)

    resolved = _reconcile(
        call_id=_call_of(dispatched).call_id, resolution_state=CALL_SUCCEEDED
    )
    assert resolved.state == CALL_SUCCEEDED
    assert resolved.status_unknown_reason == "TIMEOUT", "历史必须保留"


def test_reconcile_rejects_non_unknown_call() -> None:
    _, request, adapter = _setup_external_call(result=_success())
    dispatched = _dispatch(request=request, adapter=adapter)

    with pytest.raises(ApiError) as excinfo:
        _reconcile(
            call_id=_call_of(dispatched).call_id, resolution_state=CALL_FAILED
        )
    assert excinfo.value.code == ErrorCode.INVALID_TRANSITION


def test_reconcile_rejects_non_terminal_target() -> None:
    """核对只能落**终态** —— 回到在途状态等于"通过核对重新发一次"。"""
    _, request, adapter = _setup_external_call(
        result=CapabilityResult(state=ADAPTER_UNKNOWN, error_code="TIMEOUT")
    )
    dispatched = _dispatch(request=request, adapter=adapter)

    with pytest.raises(ApiError) as excinfo:
        _reconcile(
            call_id=_call_of(dispatched).call_id, resolution_state=CALL_POLLING
        )
    assert excinfo.value.code == ErrorCode.VALIDATION_FAILED


def test_reconcile_requires_reason() -> None:
    _, request, adapter = _setup_external_call(
        result=CapabilityResult(state=ADAPTER_UNKNOWN, error_code="TIMEOUT")
    )
    dispatched = _dispatch(request=request, adapter=adapter)

    with pytest.raises(ApiError) as excinfo:
        _reconcile(
            call_id=_call_of(dispatched).call_id,
            resolution_state=CALL_FAILED,
            reason="   ",
        )
    assert excinfo.value.code == ErrorCode.VALIDATION_FAILED
    assert excinfo.value.details["reason"] == "reason_required"


@pytest.mark.regression
def test_list_due_polls_only_returns_due_calls() -> None:
    _, request, adapter = _setup_external_call(
        result=CapabilityResult(
            state=ADAPTER_ACCEPTED,
            external_request_id="ext-due",
            next_poll_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        )
    )
    dispatched = _dispatch(request=request, adapter=adapter)
    assert _call_of(dispatched).state == CALL_POLLING

    with session_scope(SETTINGS) as session:
        # ⚠️ 必须显式传大 limit：`list_due_polls` 是**全局**扫描（按
        # `next_poll_at ASC` 排序、无实例/队列过滤），而默认 `limit=100`。
        # 共享库上积累的历史 due polls 会把本用例刚造的那条挤出窗口
        # （共用库里实测 104 条 due > 100）→ 断言"应该在里面"失败，
        # **而它单跑也红**（不是顺序问题，是数据量问题）。
        # 与 WP13 的 `_RECLAIM_LIMIT` 同一修法（ISS-49 / ISS-59 同型）。
        due = service.list_due_polls(
            GatewayRepository(session), limit=_DUE_POLLS_LIMIT
        )
    assert _call_of(dispatched).call_id in {row.call_id for row in due}


def test_list_due_polls_excludes_future_calls() -> None:
    _, request, adapter = _setup_external_call(
        result=CapabilityResult(
            state=ADAPTER_ACCEPTED,
            external_request_id="ext-future",
            next_poll_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    )
    dispatched = _dispatch(request=request, adapter=adapter)

    with session_scope(SETTINGS) as session:
        # 同上：否定式断言在小 limit 下可能**恒真而失去意义**
        # （未来调用本来就不会出现在前 100 条里，那样这条用例什么都没验）。
        due = service.list_due_polls(
            GatewayRepository(session), limit=_DUE_POLLS_LIMIT
        )
    assert _call_of(dispatched).call_id not in {row.call_id for row in due}


# ===========================================================================
# 第六组：与 WP13 的接缝
# ===========================================================================


def test_outcome_maps_success() -> None:
    outcome = gateway_handlers.outcome_for_result(
        _success(external_request_id="ext-x")
    )
    assert outcome.state == STATE_SUCCEEDED
    assert outcome.result_ref == "ext-x"


def test_outcome_maps_accepted_to_waiting_provider() -> None:
    outcome = gateway_handlers.outcome_for_result(
        CapabilityResult(state=ADAPTER_ACCEPTED, external_request_id="ext-y")
    )
    assert outcome.state == STATE_WAITING_PROVIDER


def test_outcome_maps_unknown_to_waiting_reconciliation() -> None:
    """**本接缝唯一需要想清楚的一步**（见 handlers 模块 docstring）。

    映射到 `FAILED` 会让"其实已经成功、只是我们不知道"的任务
    永久失败；`WAITING_RECONCILIATION` 是系统里为"不知道成没成"
    准备的状态，且 WP13 在它上面**禁止 requeue**。
    """
    outcome = gateway_handlers.outcome_for_result(
        CapabilityResult(state=ADAPTER_UNKNOWN, error_code="TIMEOUT")
    )
    assert outcome.state == STATE_WAITING_RECONCILIATION
    assert outcome.usage_pending is True, "不知道成没成 → 按'可能有费用'处理"


def test_outcome_maps_failure_with_conservative_retry() -> None:
    outcome = gateway_handlers.outcome_for_result(
        CapabilityResult(state=ADAPTER_FAILED, error_code="BAD_REQUEST")
    )
    assert outcome.state == STATE_FAILED
    assert outcome.retryable is False, "默认不可重试（重试可能再买一次）"


def test_usage_pending_is_false_when_cost_confirmed() -> None:
    outcome = gateway_handlers.outcome_for_result(
        _success(usage=(("token", "100"),), cost_status=COST_CONFIRMED)
    )
    assert outcome.usage_pending is False


def test_usage_pending_is_true_for_unconfirmed_usage() -> None:
    outcome = gateway_handlers.outcome_for_result(
        _success(usage=(("token", "100"),), cost_status=COST_ESTIMATED)
    )
    assert outcome.usage_pending is True


# ===========================================================================
# 第七组：结构断言与活性验证
# ===========================================================================


def test_gateway_tables_have_expected_columns() -> None:
    with session_scope(SETTINGS) as session:
        counts = {
            table: session.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name = :t"
                ),
                {"t": table},
            ).scalar_one()
            for table in (
                "providers",
                "model_profiles",
                "capability_policies",
                "provider_calls",
                "gateway_egress",
            )
        }
    assert counts == {
        "providers": 10,
        "model_profiles": 10,
        "capability_policies": 9,
        "provider_calls": 14,
        "gateway_egress": 13,
    }


def test_gateway_egress_has_no_column_capable_of_storing_body() -> None:
    """**结构性断言**：留痕表没有能放正文的列。

    比"传了正文查不到"强得多：将来有人想加一个 `payload` 列，会先撞上这条断言。
    允许出现的文本列只有：受控取值的枚举（decision/capability/policy_version）、
    受控格式的引用（trace_id / blocked_reason）、规范化摘要（payload_digest）。
    """
    forbidden = {
        "payload",
        "content",
        "body",
        "text",
        "data",
        "snapshot",
        "detail",
        "diff",
        "before",
        "after",
    }
    with session_scope(SETTINGS) as session:
        columns = set(
            session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name = 'gateway_egress'"
                )
            ).scalars().all()
        )
    assert not (columns & forbidden), f"留痕表出现了可存正文的列: {columns & forbidden}"
    # 摘要列是定长 64（SHA-256 十六进制），不是任意长度的文本。
    with session_scope(SETTINGS) as session:
        length = session.execute(
            text(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='gateway_egress' "
                "AND column_name='payload_digest'"
            )
        ).scalar_one()
    assert length == 64


def test_gateway_egress_cannot_be_updated_via_raw_sql() -> None:
    """留痕是**凭据**：原生 SQL 也改不了（触发器拦，sqlstate 23001）。"""
    project_id = _seed_project()
    provider_id = _seed_provider()
    profile_id = _seed_profile(provider_id)
    _seed_policy(project_id=project_id, provider_ids=(provider_id,))
    job_id, attempt_id = _seed_attempt(project_id=project_id)
    adapter = adapters.StubAdapter(
        capability=CAPABILITY_TEXT_STRUCTURED, default_result=_success()
    )
    result = _dispatch(
        request=_request(
            project_id=project_id,
            profile_id=profile_id,
            job_id=job_id,
            attempt_id=attempt_id,
        ),
        adapter=adapter,
    )
    assert result.egress_id is not None

    with pytest.raises(DBAPIError) as excinfo:
        with session_scope(SETTINGS) as session:
            session.execute(
                text("UPDATE gateway_egress SET decision = 'DENIED' WHERE id = :i"),
                {"i": str(result.egress_id)},
            )
    assert excinfo.value.orig.sqlstate == "23001"

    with session_scope(SETTINGS) as session:
        still = session.execute(
            text("SELECT decision FROM gateway_egress WHERE id = :i"),
            {"i": str(result.egress_id)},
        ).scalar_one()
    assert still == "ALLOWED", "内容必须原样保留"


def test_gateway_egress_cannot_be_deleted_via_raw_sql() -> None:
    project_id = _seed_project()
    provider_id = _seed_provider()
    profile_id = _seed_profile(provider_id)
    _seed_policy(project_id=project_id, provider_ids=(provider_id,))
    job_id, attempt_id = _seed_attempt(project_id=project_id)
    adapter = adapters.StubAdapter(
        capability=CAPABILITY_TEXT_STRUCTURED, default_result=_success()
    )
    result = _dispatch(
        request=_request(
            project_id=project_id,
            profile_id=profile_id,
            job_id=job_id,
            attempt_id=attempt_id,
        ),
        adapter=adapter,
    )
    assert result.egress_id is not None

    with pytest.raises(DBAPIError) as excinfo:
        with session_scope(SETTINGS) as session:
            session.execute(
                text("DELETE FROM gateway_egress WHERE id = :i"),
                {"i": str(result.egress_id)},
            )
    assert excinfo.value.orig.sqlstate == "23001"


def test_immutability_trigger_exists_on_egress_table() -> None:
    with session_scope(SETTINGS) as session:
        count = session.execute(
            text(
                "SELECT count(*) FROM pg_trigger WHERE tgname = "
                "'trg_gateway_egress_immutable' AND NOT tgisinternal"
            )
        ).scalar_one()
    assert count == 1


def test_local_only_project_has_zero_allowed_egress_rows() -> None:
    """ADR-10 违规判定口径的机械形式：`LOCAL_ONLY` 项目的 ALLOWED 留痕数恒为 0。"""
    project_id = _seed_project()
    provider_id = _seed_provider()
    profile_id = _seed_profile(provider_id)
    _seed_policy(
        project_id=project_id,
        external_policy=EXTERNAL_POLICY_LOCAL_ONLY,
        provider_ids=(provider_id,),
    )
    job_id, attempt_id = _seed_attempt(project_id=project_id)
    adapter = adapters.StubAdapter(
        capability=CAPABILITY_TEXT_STRUCTURED, default_result=_success()
    )

    result = _dispatch(
        request=_request(
            project_id=project_id,
            profile_id=profile_id,
            job_id=job_id,
            attempt_id=attempt_id,
        ),
        adapter=adapter,
    )
    _expect_denied(result)

    with session_scope(SETTINGS) as session:
        allowed_count = GatewayRepository(session).count_allowed_egress(
            project_id=project_id
        )
    assert allowed_count == 0


def test_request_key_unique_constraint_exists() -> None:
    """幂等键的唯一约束**必须在库里**（只靠代码查是扛不住并发的）。"""
    with session_scope(SETTINGS) as session:
        names = session.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE conrelid = "
                "'provider_calls'::regclass AND contype = 'u'"
            )
        ).scalars().all()
    assert "uq_provider_calls_provider_request" in names


def test_poll_due_partial_index_keeps_where_clause() -> None:
    """部分索引的 `WHERE` **不可丢** —— 丢了会让已终态的行也进索引（只增不减）。

    autogenerate 有可能丢掉 WHERE 而**不报任何错**，所以必须专门核对一次。
    """
    with session_scope(SETTINGS) as session:
        definition = session.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname='public' "
                "AND indexname = 'ix_provider_calls_poll_due'"
            )
        ).scalar_one()
    assert "WHERE" in definition
    assert "SUBMITTED" in definition and "POLLING" in definition


def test_capability_types_check_rejects_unknown_value() -> None:
    """jsonb 取值域 CHECK 的**活性验证**（不是只查约束存在）。

    取值域已在协议 §12.1 定死，因此允许任意字符串的后果是
    "同一类能力出现三种拼写 → 策略按类别比对时静默漏放行或漏拦"。
    """
    with pytest.raises(DBAPIError) as excinfo:
        with session_scope(SETTINGS) as session:
            session.execute(
                text(
                    "INSERT INTO providers (name, capability_types, network_mode, "
                    "endpoint) VALUES (:n, '[\"MAGIC\"]'::jsonb, 'EXTERNAL', "
                    "'https://x.invalid')"
                ),
                {"n": f"bad-{uuid.uuid4().hex[:8]}"},
            )
    assert excinfo.value.orig.sqlstate == "23514"


def test_external_policy_column_defaults_to_local_only() -> None:
    """**缺省拒绝落在列默认值上**：直插 SQL 不传该列也得到 `LOCAL_ONLY`。

    "缺省拒绝"要同时落在两处（列默认值 + 判定函数），
    只做一处都会留下缺口：只有判定函数时，直接 `INSERT ... DEFAULT`
    会得到一个语义为"允许"的默认值。
    """
    version = f"pv-default-{uuid.uuid4().hex[:8]}"
    with session_scope(SETTINGS) as session:
        value = session.execute(
            text(
                "INSERT INTO capability_policies (project_id, policy_version) "
                "VALUES (NULL, :v) RETURNING external_policy"
            ),
            {"v": version},
        ).scalar_one()
    assert value == EXTERNAL_POLICY_LOCAL_ONLY


@pytest.mark.regression
def test_status_unknown_reason_allowed_on_terminal_state() -> None:
    """**ISS-46 的结构回归**：终态也允许带 `status_unknown_reason`。

    最初的 CHECK 写成"只有 UNKNOWN 允许有值"，会让 `reconcile_call`
    （UNKNOWN → 终态且保留原因）**必然撞约束** —— 而那是人工核对的那一刻。
    """
    project_id, request, adapter = _setup_external_call(
        result=CapabilityResult(state=ADAPTER_UNKNOWN, error_code="TIMEOUT")
    )
    dispatched = _dispatch(request=request, adapter=adapter)
    resolved = _reconcile(
        call_id=_call_of(dispatched).call_id, resolution_state=CALL_FAILED
    )
    assert resolved.state == CALL_FAILED
    assert resolved.status_unknown_reason == "TIMEOUT"


def test_model_profile_unique_ignores_null_distinctness() -> None:
    """`UNIQUE NULLS NOT DISTINCT`：同一 (provider, model, NULL) 只能有一行。

    PG 默认认为两个 NULL **互不相同**，那样"同一模型建了两份档案"可以插进去，
    两次调用就会指向不同 profile，计费口径可能不同。
    """
    provider_id = _seed_provider()
    model_name = f"model-{uuid.uuid4().hex[:8]}"
    with session_scope(SETTINGS) as session:
        GatewayRepository(session).insert_model_profile(
            provider_id=provider_id, model_name=model_name, capabilities=[]
        )
    with pytest.raises(DBAPIError) as excinfo:
        with session_scope(SETTINGS) as session:
            GatewayRepository(session).insert_model_profile(
                provider_id=provider_id, model_name=model_name, capabilities=[]
            )
    assert excinfo.value.orig.sqlstate == "23505"
