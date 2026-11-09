"""WP32 `batches` 包的验收判据 —— 纯函数层 / 取值域层 / 数据库行为层 / 服务层 / 静态接缝。

## 夹具纪律（照抄 `test_review_wp30.py`，并加一道**私有库自检**）

· 一律跑**私有库**（`REDNOTE_TEST_DATABASE_URL`）；缺省也指向 WP32 私有库 `rednote_test_wp32`；
· **私有库自检**：模块级夹具第一件事断言 DSN 里的库名含 `wp32`，否则 `pytest.fail` ——
  环境变量漏带会静默落到共享库（此前有 agent 踩过），宁可当场红；
· 只重建本包 3 张表 + 6 个触发器，其余表按迁移已就绪（**不**复用 content 的 autouse 建表夹具：
  那个夹具的缺省库是共享库，且会在本夹具自检之前先执行 DDL）；
· 每个用例新造 user / project / 基线 / 方案 / 计价 / 预算，**按项目隔离**，不数全库行；
· 数据库探针断言 **sqlstate**，不只断言“抛了异常”。

## 本文件守什么

| 组 | 判据 | 风险 |
|---|---|---|
| 纯函数 | 进度折算 8 值 + 未知态不被吞 + 缺失不填 0；请求摘要；分配快照确定性；展示码 | 派生值漂移是静默的 |
| 取值域 | 3 个落库取值域与 planning / jobs 的同值性；DDL 无 10/30/50 上界 | ISS-191 / ADR-13 |
| 数据库 | 唯一约束、CHECK、不可变（23001）、4 个延迟一致性触发器（23514） | 数量与成员行数两处声称 |
| 服务层 | 创建 13 步、幂等重放 / 异体 / 并发、关口、预算、分配、原子性、取消、重做 | 重复建批 / 重复花钱 / 把未知当失败 |
| 静态 | 批次层不预占、无 10/30/50 字面量、`register_version` 显式 `revision_no` | G3 双占 / OP-91 |
"""

from __future__ import annotations

import ast
import itertools
import os
import re
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from conftest import drop_own_tables, truncate_targets

# FK 目标表必须在 `create_all` 之前 import（否则 NoReferencedTableError）。
import rednote.modules.audit.models  # noqa: F401
import rednote.modules.baseline.models  # noqa: F401
import rednote.modules.billing.models  # noqa: F401
import rednote.modules.content.models  # noqa: F401
import rednote.modules.identity.models  # noqa: F401
import rednote.modules.jobs.models  # noqa: F401
import rednote.modules.planning.models  # noqa: F401
import rednote.modules.projects.models  # noqa: F401
import rednote.modules.versions.models  # noqa: F401
import rednote.modules.batches.models  # noqa: F401

from rednote.modules.batches import (
    jobs_bridge,
    planning_bridge,
    versions_bridge,
)
from rednote.modules.batches import ddl as b_ddl
from rednote.modules.batches import service as b_service
from rednote.modules.batches import types as b_types
from rednote.modules.batches.dependencies import default_gate_checker
from rednote.modules.batches.models import (
    IMMUTABLE_TABLES,
    OWN_TABLES,
    Batch,
)
from rednote.modules.batches.progress import rollup
from rednote.modules.batches.repository import BatchRepository
from rednote.modules.batches.types import CreateBatchCommand, GateVerdict
from rednote.modules.billing.repository import BillingRepository
from rednote.modules.billing.service import set_budget
from rednote.modules.billing.types import CURRENCY_CNY, PRICING_STATE_PUBLISHED
from rednote.modules.jobs import models as jobs_models
from rednote.modules.jobs import service as jobs_service
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.planning.repository import PlanningRepository
from rednote.modules.planning.service import submit_draft
from rednote.modules.planning.settings_service import change_project_setting
from rednote.modules.planning.types import (
    BATCH_KINDS as PLANNING_BATCH_KINDS,
    SETTING_KEY_BATCH_LIMITS,
    SETTING_ORIGINS,
)
from rednote.modules.versions.repository import VersionRepository
from rednote.modules.versions.service import canonical_payload_hash, register_version
from rednote.platform.base import Base
from rednote.platform.configuration import Settings
from rednote.platform.database import get_engine, reset_engines, session_scope
from rednote.platform.errors import ApiError, ErrorCode
from rednote.platform.immutability import IMMUTABLE_RECORD_SQLSTATE

# 复用 content 包的造数助手（只读复用，不往 `test_content_wp26*.py` 里写任何东西；
# **不**导入它们的 autouse 建表夹具，理由见模块 docstring）。
from test_content_wp26 import (
    _create_user,
    _seed_project,
    _seed_provider,
    _sqlstate,
)
from test_content_wp26_exec import _seed_baseline

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
#: WP32 私有测试库（**不是**共享的 `rednote_test`）。
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test_wp32"
TEST_DATABASE_URL_ENV = "REDNOTE_TEST_DATABASE_URL"

#: DROP / TRUNCATE 顺序：引用方在前。
DROP_ORDER = ("batch_item_runs", "batch_items", "batches")
_TABLE_NAMES = tuple(OWN_TABLES)

UNIQUE_VIOLATION = "23505"
CHECK_VIOLATION = "23514"

SRC = Path(__file__).resolve().parents[1] / "src/rednote/modules/batches"


def _test_database_url() -> str:
    return os.environ.get(TEST_DATABASE_URL_ENV, DEFAULT_TEST_DATABASE_URL).strip()


def _make_settings() -> Settings:
    return Settings(
        database_url=_test_database_url(),
        environment="test",
        session_cookie_secure=False,
        oidc_issuer=TEST_ISSUER,
        oidc_token_encryption_key=TEST_ENCRYPTION_KEY,
    )


# ===========================================================================
# 建表夹具（module 级）
# ===========================================================================


@pytest.fixture(scope="module", autouse=True)
def _ensure_own_tables() -> Iterator[None]:
    """私有库自检 → 只重建本包 3 张表 + 6 个触发器。"""
    reset_engines()
    engine = get_engine(_make_settings())
    dropped, kept = drop_own_tables(engine, DROP_ORDER)
    Base.metadata.create_all(
        engine, tables=[Base.metadata.tables[name] for name in OWN_TABLES]
    )
    # `jobs.batch_id → batches.id`（迁移 b2f32c0de002）是跨包入向 FK：在已迁到 head 的库里
    # `batches` 会被保留（`drop_own_tables` 不删被引用的表），交给 create_all 保证存在性、
    # 交给迁移保证结构；私有库若停在该迁移之前则三张表全可删。除 `batches` 外都应可删。
    assert set(kept) <= {"batches"}, f"除 batches 外应全部可删：dropped={dropped} kept={kept}"
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(truncate_targets(engine, DROP_ORDER))))
        for statement in b_ddl.install_statements():
            conn.execute(text(statement))
    yield


@pytest.fixture(autouse=True)
def _clean_own_rows() -> Iterator[None]:
    """每个用例前清空本包 3 张表（不可变表只能 TRUNCATE）。"""
    reset_engines()
    engine = get_engine(_make_settings())
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(truncate_targets(engine, DROP_ORDER))))
    yield


# ===========================================================================
# 造数
# ===========================================================================

PRICE_CALL = "12.50000000"


@dataclass(frozen=True)
class BatchEnv:
    settings: Settings
    owner_id: uuid.UUID
    project_id: uuid.UUID
    provider_id: uuid.UUID
    model_profile_id: uuid.UUID
    baseline_id: uuid.UUID
    plan_version_id: uuid.UUID


def _scene_items(*weights_caps: tuple[int, int | None]) -> list[dict[str, Any]]:
    return [
        {
            "key": f"scene-{index}",
            "name": f"场景{index}",
            "weightPermille": weight,
            "sortOrder": index,
            "capacity": cap,
            "state": "ENABLED",
            "disabledReason": None,
        }
        for index, (weight, cap) in enumerate(weights_caps)
    ]


def _form_items(*weights_caps: tuple[int, int | None]) -> list[dict[str, Any]]:
    return [
        {
            "key": f"form-{index}",
            "name": f"形式{index}",
            "weightPermille": weight,
            "sortOrder": index,
            "capacity": cap,
            "state": "ENABLED",
            "disabledReason": None,
        }
        for index, (weight, cap) in enumerate(weights_caps)
    ]


def _seed_project_with_limits(settings: Settings, *, owner_id: uuid.UUID) -> uuid.UUID:
    """`_seed_project` + 保证该项目有生效的 `batch.limits`（项目级，**不碰 GLOBAL 行**）。

    迁移种下了 GLOBAL `batch.limits`（ADR-13 初始行）：在迁移到 head 的库里（CI 的形态），
    新项目会继承它，本函数什么都不写。只靠各包夹具 `create_all` 建出来的共享库没有 GLOBAL 行，
    项目就没有生效值（`SETTING_NOT_CONFIGURED`）⇒ 补一条**项目级** MANUAL 覆盖，值同 ADR-13。
    不写 GLOBAL 行：那会污染 WP24 里依赖「全局未配置」的用例。
    """
    from rednote.modules.planning.repository import PlanningRepository
    from rednote.modules.planning.settings_service import (
        change_project_setting,
        get_effective_limits,
    )
    from rednote.modules.planning.types import SETTING_KEY_BATCH_LIMITS

    project_id = _seed_project(settings, owner_id=owner_id)
    with session_scope(settings) as session:
        repo = PlanningRepository(session)
        effective = get_effective_limits(
            repo, SETTING_KEY_BATCH_LIMITS, project_id, materialize=False
        )
        if not effective.configured:
            change_project_setting(
                repo,
                key=SETTING_KEY_BATCH_LIMITS,
                project_id=project_id,
                value={"TRIAL": 10, "SCALE": 30, "BULK": 50},
                reason="测试夹具：库内无 GLOBAL batch.limits，补项目级覆盖（值同 ADR-13）",
                actor_id=owner_id,
            )
    return project_id


def make_env(
    *,
    scenes: list[dict[str, Any]] | None = None,
    forms: list[dict[str, Any]] | None = None,
    total_quantity: int = 100,
    budget: str | None = "100000",
    priced: bool = True,
) -> BatchEnv:
    """一个用户 + 项目 + 模型档案 + 计价 + 预算 + 生效基线 + 已提交的 PLAN。"""
    reset_engines()
    settings = _make_settings()
    owner_id = _create_user(settings)
    project_id = _seed_project_with_limits(settings, owner_id=owner_id)
    provider_id, profile_id = _seed_provider(settings)
    env0 = BatchEnv(
        settings=settings,
        owner_id=owner_id,
        project_id=project_id,
        provider_id=provider_id,
        model_profile_id=profile_id,
        baseline_id=uuid.UUID(int=0),
        plan_version_id=uuid.UUID(int=0),
    )
    if priced:
        with session_scope(settings) as session:
            BillingRepository(session).insert_pricing_version(
                provider_id=provider_id,
                model_profile_id=None,
                currency=CURRENCY_CNY,
                label="WP32 测试价目",
                unit_prices={"CALL": PRICE_CALL},
                effective_from=datetime.now(timezone.utc) - timedelta(days=1),
                state=PRICING_STATE_PUBLISHED,
            )
    if budget is not None:
        with session_scope(settings) as session:
            set_budget(
                BillingRepository(session), project_id=project_id, limit_amount=budget
            )
    baseline_id = _seed_baseline(env0, brief_version_id=None)  # type: ignore[arg-type]
    with session_scope(settings) as session:
        repo = PlanningRepository(session)
        draft = repo.insert_draft(
            project_id=project_id,
            baseline_id=baseline_id,
            created_by=owner_id,
            total_quantity=total_quantity,
            scene_items=scenes or _scene_items((500, None), (300, None), (200, None)),
            form_items=forms or _form_items((600, None), (400, None)),
        )
        session.flush()
        registered = submit_draft(
            repo,
            VersionRepository(session),
            project_id=project_id,
            expected_lock_version=draft.lock_version,
            actor_id=owner_id,
        )
        plan_version_id = registered.node_id
    return BatchEnv(
        settings=settings,
        owner_id=owner_id,
        project_id=project_id,
        provider_id=provider_id,
        model_profile_id=profile_id,
        baseline_id=baseline_id,
        plan_version_id=plan_version_id,
    )


@pytest.fixture
def env() -> BatchEnv:
    return make_env()


GATE_OK = lambda _pid: GateVerdict(passed=True)  # noqa: E731


def command_of(
    env: BatchEnv,
    *,
    kind: str = "SCALE",
    quantity: int = 5,
    scope: str = "FULL",
    baseline_id: uuid.UUID | None = None,
    plan_version_id: uuid.UUID | None = None,
) -> CreateBatchCommand:
    return CreateBatchCommand(
        batch_kind=kind,
        quantity=quantity,
        plan_version_id=plan_version_id or env.plan_version_id,
        baseline_id=baseline_id,
        model_profile_id=env.model_profile_id,
        scope=scope,
    )


def do_create(
    env: BatchEnv,
    *,
    key: str | None = None,
    gate: Any = GATE_OK,
    command: CreateBatchCommand | None = None,
    **command_kwargs: Any,
) -> b_service.CreateResult:
    """在独立事务里建一个批次（退出即提交；抛 ApiError 即回滚）。"""
    with session_scope(env.settings) as session:
        return b_service.create_batch(
            BatchRepository(session),
            project_id=env.project_id,
            command=command or command_of(env, **command_kwargs),
            idempotency_key=key if key is not None else f"key-{uuid.uuid4().hex[:10]}",
            gate_checker=gate,
            actor_id=env.owner_id,
            request_id=f"req-{uuid.uuid4().hex[:8]}",
        )


def sql(env: BatchEnv, statement: str, params: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
    with session_scope(env.settings) as session:
        result = session.execute(text(statement), params or {})
        return [tuple(row) for row in result.fetchall()] if result.returns_rows else []


def scalar(env: BatchEnv, statement: str, params: dict[str, Any] | None = None) -> Any:
    rows = sql(env, statement, params)
    return rows[0][0] if rows else None


def counts(env: BatchEnv) -> dict[str, int]:
    """本项目范围内各相关表行数（按项目隔离，不数全库）。"""
    p = {"p": env.project_id}
    return {
        "batches": scalar(env, "SELECT count(*) FROM batches WHERE project_id=:p", p),
        "items": scalar(
            env,
            "SELECT count(*) FROM batch_items bi JOIN batches b ON b.id=bi.batch_id"
            " WHERE b.project_id=:p",
            p,
        ),
        "runs": scalar(
            env,
            "SELECT count(*) FROM batch_item_runs r JOIN batch_items bi ON bi.id=r.batch_item_id"
            " JOIN batches b ON b.id=bi.batch_id WHERE b.project_id=:p",
            p,
        ),
        "contents": scalar(env, "SELECT count(*) FROM content_items WHERE project_id=:p", p),
        "jobs": scalar(env, "SELECT count(*) FROM jobs WHERE project_id=:p", p),
        "nodes": scalar(env, "SELECT count(*) FROM version_nodes WHERE project_id=:p", p),
        "estimates": scalar(env, "SELECT count(*) FROM cost_estimates WHERE project_id=:p", p),
        "reservations": scalar(
            env, "SELECT count(*) FROM budget_reservations WHERE project_id=:p", p
        ),
        "settings": scalar(env, "SELECT count(*) FROM runtime_settings WHERE project_id=:p", p),
    }


def set_job_state(env: BatchEnv, job_id: uuid.UUID, state: str) -> None:
    sql(env, "UPDATE jobs SET state=:s WHERE id=:j", {"s": state, "j": job_id})


def job_ids_of(env: BatchEnv, batch_id: uuid.UUID) -> list[uuid.UUID]:
    rows = sql(
        env,
        "SELECT r.job_id FROM batch_item_runs r JOIN batch_items bi ON bi.id=r.batch_item_id"
        " WHERE bi.batch_id=:b ORDER BY bi.ordinal, r.round_no",
        {"b": batch_id},
    )
    return [row[0] for row in rows]


def detail_of(env: BatchEnv, batch_id: uuid.UUID, *, can_view_cost: bool = True) -> dict[str, Any]:
    with session_scope(env.settings) as session:
        repo = BatchRepository(session)
        return b_service.get_batch_detail(
            repo, b_service.require_batch(repo, batch_id), can_view_cost=can_view_cost
        )


def assert_api_error(exc: pytest.ExceptionInfo[ApiError], code: ErrorCode, reason: str | None) -> None:
    assert exc.value.code == code, (exc.value.code, exc.value.details)
    if reason is not None:
        assert exc.value.details.get("reason") == reason, exc.value.details


# ===========================================================================
# 1. 纯函数层
# ===========================================================================

_ALL_JOB_STATES = sorted(jobs_models.JOB_STATES)


def test_progress_states_are_eight_and_exact() -> None:
    assert b_types.PROGRESS_STATES == {
        "QUEUED",
        "RUNNING",
        "CANCELLING",
        "SUCCEEDED",
        "PARTIAL",
        "FAILED",
        "CANCELLED",
        "UNKNOWN",
    }
    assert len(b_types.PROGRESS_STATES) == 8


@pytest.mark.parametrize(
    ("batch_state", "states", "expected"),
    [
        # --- UNKNOWN：空 / 缺失 / 未知值，不折成 0 或 FAILED ----------------
        ("SUBMITTED", [], "UNKNOWN"),
        ("SUBMITTED", [None], "UNKNOWN"),
        ("SUBMITTED", ["SUCCEEDED", None], "UNKNOWN"),
        ("CANCEL_REQUESTED", [None, "CANCELLED"], "UNKNOWN"),
        ("SUBMITTED", ["SUCCEEDED", "NOT_A_STATE"], "UNKNOWN"),
        # --- 取消 -----------------------------------------------------------
        ("CANCEL_REQUESTED", ["CANCELLED", "CANCELLED"], "CANCELLED"),
        ("CANCEL_REQUESTED", ["CANCELLED", "SUCCEEDED"], "CANCELLED"),
        ("CANCEL_REQUESTED", ["CANCEL_REQUESTED", "CANCELLED"], "CANCELLING"),
        ("CANCEL_REQUESTED", ["RUNNING", "SUCCEEDED"], "CANCELLING"),
        ("CANCEL_REQUESTED", ["WAITING_RECONCILIATION", "CANCELLED"], "CANCELLING"),
        # --- 进行中 ----------------------------------------------------------
        ("SUBMITTED", ["QUEUED", "QUEUED"], "QUEUED"),
        ("SUBMITTED", ["QUEUED", "RUNNING"], "RUNNING"),
        ("SUBMITTED", ["WAITING_PROVIDER", "SUCCEEDED"], "RUNNING"),
        ("SUBMITTED", ["WAITING_RECONCILIATION", "SUCCEEDED"], "RUNNING"),
        ("SUBMITTED", ["WAITING_RECONCILIATION", "FAILED"], "RUNNING"),
        ("SUBMITTED", ["WAITING_RECONCILIATION"], "RUNNING"),
        # --- 全终态 ----------------------------------------------------------
        ("SUBMITTED", ["SUCCEEDED", "SUCCEEDED"], "SUCCEEDED"),
        ("SUBMITTED", ["FAILED", "FAILED"], "FAILED"),
        ("SUBMITTED", ["FAILED", "CANCELLED"], "FAILED"),
        ("SUBMITTED", ["SUCCEEDED", "FAILED"], "PARTIAL"),
        ("SUBMITTED", ["PARTIAL", "PARTIAL"], "PARTIAL"),
        ("SUBMITTED", ["PARTIAL", "FAILED"], "PARTIAL"),
        ("SUBMITTED", ["SUCCEEDED", "CANCELLED"], "PARTIAL"),
    ],
)
def test_rollup_table(batch_state: str, states: list[str | None], expected: str) -> None:
    assert rollup(batch_state, states).state == expected


def test_rollup_exhaustive_invariants() -> None:
    """穷举 (批次状态 2) x (3 个成员的 job 状态 ∈ 9 + 缺失)：三条不变量逐行成立。"""
    domain: list[str | None] = [*_ALL_JOB_STATES, None]
    for batch_state in sorted(b_types.BATCH_STATES):
        for combo in itertools.product(domain, repeat=3):
            progress = rollup(batch_state, list(combo))
            assert progress.state in b_types.PROGRESS_STATES
            # 缺失不填 0：任一成员没有 run ⇒ UNKNOWN。
            if any(item is None for item in combo):
                assert progress.state == "UNKNOWN", combo
                continue
            # 计数自洽：各状态之和 == 成员数；needsReconciliation 单列且等于 WAITING_RECONCILIATION 条数。
            assert sum(progress.counts.values()) == 3
            assert progress.needs_reconciliation == combo.count("WAITING_RECONCILIATION")
            # 未知态不被吞：存在 WAITING_RECONCILIATION 时结论只可能是 RUNNING / CANCELLING。
            if "WAITING_RECONCILIATION" in combo:
                assert progress.state in {"RUNNING", "CANCELLING"}, (batch_state, combo)


def test_rollup_counts_have_nine_keys_even_when_zero() -> None:
    progress = rollup("SUBMITTED", ["SUCCEEDED"])
    assert set(progress.counts) == set(jobs_models.JOB_STATES)
    assert progress.counts["FAILED"] == 0  # 确为 0 才是 0


def test_request_hash_is_key_order_independent_and_sensitive() -> None:
    plan, profile = uuid.uuid4(), uuid.uuid4()
    base = CreateBatchCommand("SCALE", 5, plan, None, profile, "FULL")
    same = CreateBatchCommand(
        batch_kind="SCALE", quantity=5, plan_version_id=plan, baseline_id=None,
        model_profile_id=profile, scope="FULL",
    )
    assert b_service.build_request_hash(base, scope="FULL") == b_service.build_request_hash(
        same, scope="FULL"
    )
    for changed in (
        CreateBatchCommand("BULK", 5, plan, None, profile, "FULL"),
        CreateBatchCommand("SCALE", 6, plan, None, profile, "FULL"),
        CreateBatchCommand("SCALE", 5, uuid.uuid4(), None, profile, "FULL"),
        CreateBatchCommand("SCALE", 5, plan, uuid.uuid4(), profile, "FULL"),
        CreateBatchCommand("SCALE", 5, plan, None, uuid.uuid4(), "FULL"),
    ):
        assert b_service.build_request_hash(changed, scope="FULL") != b_service.build_request_hash(
            base, scope="FULL"
        )
    assert b_service.build_request_hash(base, scope="COPY") != b_service.build_request_hash(
        base, scope="FULL"
    )


def test_display_code() -> None:
    assert b_service.display_code("SCALE", 1) == "BATCH-S01"
    assert b_service.display_code("BULK", 12) == "BATCH-B12"
    assert b_service.display_code("TRIAL", 2) == "BATCH-T02"
    assert b_service.display_code("BULK", 100) == "BATCH-B100"


def test_job_and_content_idempotency_keys_embed_round() -> None:
    content, batch = uuid.uuid4(), uuid.uuid4()
    assert b_types.job_idempotency_key(content, batch, 1) != b_types.job_idempotency_key(
        content, batch, 2
    )
    assert b_types.job_idempotency_key(content, batch, 2) == f"{content}:batch:{batch}:r2"
    assert b_types.content_idempotency_key(batch) == f"batch:{batch}"


def test_render_batch_hides_money_without_view_cost() -> None:
    """无权看金额：金额与币种为 null，**不写 0**；有权则原样（含 0 的预估要写 0）。"""
    now = datetime.now(timezone.utc)
    batch = Batch(
        id=uuid.uuid4(), project_id=uuid.uuid4(), batch_kind="SCALE", seq_no=1, quantity=3,
        state="SUBMITTED", plan_version_id=uuid.uuid4(), baseline_id=uuid.uuid4(),
        allocation={}, limit_applied=30, limit_source="GLOBAL",
        estimated_amount=Decimal("0"), estimated_currency="CNY", estimate_refs=[],
        idempotency_key="k", request_hash="h", created_at=now, updated_at=now, lock_version=1,
    )
    progress = rollup("SUBMITTED", ["QUEUED"])
    hidden = b_service.render_batch(batch, progress, baseline_state=None, can_view_cost=False)
    shown = b_service.render_batch(batch, progress, baseline_state=None, can_view_cost=True)
    assert hidden["estimate"]["amount"] is None and hidden["estimate"]["currency"] is None
    assert shown["estimate"]["amount"] == "0.00000000" and shown["estimate"]["currency"] == "CNY"
    assert shown["estimate"]["isEstimate"] is True
    batch.estimated_amount = None
    batch.estimated_currency = None
    unknown = b_service.render_batch(batch, progress, baseline_state=None, can_view_cost=True)
    assert unknown["estimate"]["amount"] is None  # 未预估 ≠ 0


# ===========================================================================
# 2. 取值域 / 静态接缝
# ===========================================================================


def test_value_domains_match_upstream() -> None:
    assert b_types.BATCH_KINDS == set(PLANNING_BATCH_KINDS)
    assert b_types.LIMIT_SOURCES == set(SETTING_ORIGINS)
    assert b_types.BATCH_STATES == {"SUBMITTED", "CANCEL_REQUESTED"}


def test_creatable_kinds_exclude_trial() -> None:
    assert b_types.CREATABLE_BATCH_KINDS == {"SCALE", "BULK"}


def test_constraint_and_index_names_fit_63_bytes_and_are_unique() -> None:
    names: list[str] = []
    for table in (Base.metadata.tables[name] for name in OWN_TABLES):
        for constraint in table.constraints:
            if constraint.name:
                names.append(str(constraint.name))
        names.extend(str(index.name) for index in table.indexes)
    assert names
    assert len(names) == len(set(names)), "约束 / 索引名重复"
    assert all(len(name.encode()) <= 63 for name in names)


def test_quantity_has_only_a_lower_bound_adr13() -> None:
    """ADR-13：`quantity` 的 CHECK 只有 `quantity > 0`；DDL 里没有 10/30/50 作为上界。"""
    table = Base.metadata.tables["batches"]
    quantity_checks = [
        str(c.sqltext)
        for c in table.constraints
        if getattr(c, "sqltext", None) is not None and "quantity" in str(c.sqltext)
    ]
    assert quantity_checks == ["quantity > 0"]
    for table_name in OWN_TABLES:
        for constraint in Base.metadata.tables[table_name].constraints:
            sqltext = str(getattr(constraint, "sqltext", ""))
            assert not re.search(r"<=?\s*(10|30|50)\b", sqltext), sqltext


def test_ddl_trigger_inventory() -> None:
    assert len(b_ddl.trigger_names()) == 6
    assert set(b_ddl.consistency_function_names()) == {
        "rednote_check_batch_item_count",
        "rednote_check_batch_item_ownership",
        "rednote_check_batch_item_run_ownership",
    }
    assert "rednote_reject_mutation" not in b_ddl.consistency_function_names()
    assert b_ddl.CONSISTENCY_SQLSTATE == "23514"
    assert all(len(name.encode()) <= 63 for name in b_ddl.trigger_names())
    assert len(b_ddl.all_trigger_ddl()) == 6
    assert set(IMMUTABLE_TABLES) == {"batch_items", "batch_item_runs"}


def _batches_sources() -> dict[str, ast.Module]:
    return {
        path.name: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(SRC.glob("*.py"))
    }


def _call_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_static_batch_layer_never_reserves_settles_or_releases() -> None:
    """方案 B：批次层不调用预占 / 结算 / 释放（否则与逐条预占叠加，G3 双占）。"""
    forbidden = {
        "reserve_budget",
        "settle_budget",
        "release_budget",
        "reserve_generation_budget",
        "settle_generation_budget",
        "release_generation_budget",
        "reserve_budget_for_content",
    }
    for filename, tree in _batches_sources().items():
        assert not (_call_names(tree) & forbidden), filename


def test_static_no_literal_10_30_50_in_source() -> None:
    """T-REG-03：本包源码不含 10 / 30 / 50 作为数量上界的数字字面量（ADR-13 无硬编码残留）。"""
    for filename, tree in _batches_sources().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and type(node.value) is int:
                assert node.value not in {10, 30, 50}, (filename, node.lineno)


def test_static_update_statements_touch_only_command_columns() -> None:
    """T-WRITE-SURFACE：仓储层的全部 UPDATE 都只改命令位列。"""
    source = (SRC / "repository.py").read_text(encoding="utf-8")
    allowed = {
        "state",
        "cancel_requested_at",
        "cancel_requested_by",
        "lock_version",
        "updated_at",
    }
    updates = re.findall(r"UPDATE\s+(\w+)\s+SET\s+(.*?)\s+WHERE", source, flags=re.S)
    assert updates, "仓储层应当有 UPDATE（取消 / 重做占位）"
    for table, assignments in updates:
        assert table == "batches"
        columns = set(re.findall(r"(\w+)\s*=", assignments))
        assert columns <= allowed, columns
    # 其它文件不得拼 UPDATE batches。
    for path in SRC.glob("*.py"):
        if path.name in {"repository.py", "ddl.py"}:
            continue
        assert "UPDATE batches" not in path.read_text(encoding="utf-8"), path.name


def test_static_bridges_are_the_only_cross_module_imports() -> None:
    """平级模块不可互 import：除 bridge / dependencies / api 外，不 import 其它业务模块。"""
    allowed_files = {
        "api.py", "dependencies.py", "audit_bridge.py", "baseline_bridge.py",
        "billing_bridge.py", "content_bridge.py", "jobs_bridge.py",
        "planning_bridge.py", "trial_bridge.py", "versions_bridge.py",
    }
    for filename, tree in _batches_sources().items():
        if filename in allowed_files:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("rednote.modules.") and not node.module.startswith(
                    "rednote.modules.batches"
                ):
                    # service.py 用 `from rednote.modules.batches import …bridge` 的形态；
                    # 其余业务模块一律不许直接 import。
                    pytest.fail(f"{filename}:{node.lineno} 直接 import 了 {node.module}")


def test_default_gate_checker_is_fail_closed() -> None:
    with pytest.raises(ApiError) as exc:
        default_gate_checker(uuid.uuid4())
    assert_api_error(exc, ErrorCode.DEPENDENCY_UNAVAILABLE, "trial_gate_not_wired")


# --- trial_bridge：把 WP31 的只读接缝翻译成 GateVerdict（接线后的真实形态） -------------


def _gate_info(*, stale: bool, reasons: tuple[str, ...] = ()) -> Any:
    from datetime import datetime, timezone

    from rednote.modules.planning.trial.seam import TrialGateDecisionInfo

    return TrialGateDecisionInfo(
        decision_id=uuid.uuid4(),
        round_id=uuid.uuid4(),
        round_no=1,
        round_kind="TRIAL",
        plan_version_id=uuid.uuid4(),
        baseline_id=uuid.uuid4(),
        approved_scene_keys=("s1",),
        decided_at=datetime.now(timezone.utc),
        decided_by=None,
        stale=stale,
        stale_reasons=reasons,
    )


def _patch_gate(monkeypatch: pytest.MonkeyPatch, *, passed: bool, latest: Any) -> None:
    from rednote.modules.batches import trial_bridge
    from rednote.modules.planning.trial.seam import TrialGateStatus

    monkeypatch.setattr(
        trial_bridge,
        "trial_gate_status",
        lambda _session, *, project_id: TrialGateStatus(
            passed=passed, latest=latest, verified_scene_keys=()
        ),
    )


def test_trial_bridge_passed_carries_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    from rednote.modules.batches.trial_bridge import read_gate_verdict

    info = _gate_info(stale=False)
    _patch_gate(monkeypatch, passed=True, latest=info)
    verdict = read_gate_verdict(object(), uuid.uuid4())
    assert verdict.passed is True
    assert verdict.decision_id == info.decision_id and verdict.reason is None


def test_trial_bridge_no_decision_is_not_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    from rednote.modules.batches.trial_bridge import read_gate_verdict

    _patch_gate(monkeypatch, passed=False, latest=None)
    verdict = read_gate_verdict(object(), uuid.uuid4())
    assert verdict.passed is False and verdict.reason == "no_gate_decision"
    assert verdict.decision_id is None


def test_trial_bridge_stale_decision_reports_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    from rednote.modules.batches.trial_bridge import read_gate_verdict

    info = _gate_info(stale=True, reasons=("BASELINE_CHANGED", "REVIEW_CHANGED"))
    _patch_gate(monkeypatch, passed=False, latest=info)
    verdict = read_gate_verdict(object(), uuid.uuid4())
    assert verdict.passed is False
    assert verdict.reason == "stale:BASELINE_CHANGED,REVIEW_CHANGED"
    assert verdict.decision_id == info.decision_id


# ===========================================================================
# 3. 数据库行为层（原生 SQL 绕过 service）
# ===========================================================================


def _raw_batch_params(env: BatchEnv, **override: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "id": uuid.uuid4(),
        "p": env.project_id,
        "k": "SCALE",
        "seq": 1,
        "q": 2,
        "state": "SUBMITTED",
        "pv": env.plan_version_id,
        "bl": env.baseline_id,
        "alloc": "{}",
        "la": 30,
        "ls": "GLOBAL",
        "amt": None,
        "cur": None,
        "refs": "[]",
        "idem": f"raw-{uuid.uuid4().hex[:8]}",
        "hash": "h",
        "cat": None,
    }
    params.update(override)
    return params


_INSERT_BATCH = (
    "INSERT INTO batches (id, project_id, batch_kind, seq_no, quantity, state,"
    " plan_version_id, baseline_id, allocation, limit_applied, limit_source,"
    " estimated_amount, estimated_currency, estimate_refs, idempotency_key,"
    " request_hash, cancel_requested_at)"
    " VALUES (:id, :p, :k, :seq, :q, :state, :pv, :bl, cast(:alloc as jsonb), :la, :ls,"
    " :amt, :cur, cast(:refs as jsonb), :idem, :hash, :cat)"
)


def _expect_constraint(env: BatchEnv, params: dict[str, Any], sqlstate: str, name: str) -> None:
    """单事务 + SAVEPOINT：违规语句失败只回滚这一小段；断言 sqlstate 与约束名。"""
    with session_scope(env.settings) as session:
        nested = session.begin_nested()
        with pytest.raises(DBAPIError) as exc:
            session.execute(text(_INSERT_BATCH), params)
        assert _sqlstate(exc.value) == sqlstate
        assert exc.value.orig.diag.constraint_name == name  # type: ignore[union-attr]
        nested.rollback()


def test_db_quantity_has_no_upper_bound(env: BatchEnv) -> None:
    """T-QTY-05：库里没有上界（51 / 10**6 的 INSERT 本身成功）；0 / -1 以 23514 + 约束名失败。"""
    with session_scope(env.settings) as session:
        for quantity in (51, 10**6):
            nested = session.begin_nested()
            session.execute(text(_INSERT_BATCH), _raw_batch_params(env, q=quantity))
            nested.rollback()  # 不触发延迟的数量一致性触发器
    for bad in (0, -1):
        _expect_constraint(
            env, _raw_batch_params(env, q=bad), CHECK_VIOLATION, "ck_batches_quantity_positive"
        )


@pytest.mark.parametrize(
    ("override", "name"),
    [
        ({"k": "WEEKLY"}, "ck_batches_batch_kind_valid"),
        ({"state": "DONE"}, "ck_batches_state_valid"),
        ({"ls": "ELSEWHERE"}, "ck_batches_limit_source_valid"),
        ({"seq": 0}, "ck_batches_seq_no_positive"),
        ({"la": 0}, "ck_batches_limit_applied_positive"),
        ({"amt": Decimal("1"), "cur": None}, "ck_batches_estimate_pair_consistent"),
        ({"amt": None, "cur": "CNY"}, "ck_batches_estimate_pair_consistent"),
        ({"amt": Decimal("-1"), "cur": "CNY"}, "ck_batches_estimated_amount_non_negative"),
        ({"refs": "{}"}, "ck_batches_estimate_refs_is_array"),
        ({"alloc": "[]"}, "ck_batches_allocation_is_object"),
        ({"idem": ""}, "ck_batches_idempotency_key_not_blank"),
        ({"hash": ""}, "ck_batches_request_hash_not_blank"),
        # 取消：state 与取消时刻双向等值。
        ({"state": "CANCEL_REQUESTED", "cat": None}, "ck_batches_cancel_pair_consistent"),
        (
            {"state": "SUBMITTED", "cat": datetime.now(timezone.utc)},
            "ck_batches_cancel_pair_consistent",
        ),
    ],
)
def test_db_check_constraints(env: BatchEnv, override: dict[str, Any], name: str) -> None:
    _expect_constraint(env, _raw_batch_params(env, **override), CHECK_VIOLATION, name)


def test_db_estimate_zero_is_legal_and_distinct_from_null(env: BatchEnv) -> None:
    with session_scope(env.settings) as session:
        nested = session.begin_nested()
        session.execute(
            text(_INSERT_BATCH), _raw_batch_params(env, amt=Decimal("0"), cur="CNY")
        )
        nested.rollback()


def test_db_unique_idempotency_key_and_seq(env: BatchEnv) -> None:
    """T-DB-01：同项目同幂等键 / 同档位同序号 → 23505 + 约束名。"""
    first = _raw_batch_params(env, idem="dup-key", seq=7)
    with session_scope(env.settings) as session:
        session.execute(text(_INSERT_BATCH), first)
        for override, name in (
            ({"idem": "dup-key", "seq": 8}, "uq_batches_project_idempotency_key"),
            ({"idem": "other", "seq": 7}, "uq_batches_project_kind_seq"),
        ):
            nested = session.begin_nested()
            with pytest.raises(DBAPIError) as exc:
                session.execute(text(_INSERT_BATCH), _raw_batch_params(env, **override))
            assert _sqlstate(exc.value) == UNIQUE_VIOLATION
            assert exc.value.orig.diag.constraint_name == name  # type: ignore[union-attr]
            nested.rollback()
        session.rollback()


def test_db_same_key_in_two_projects_is_fine(env: BatchEnv) -> None:
    other = make_env()
    do_create(env, key="shared-key")
    do_create(other, key="shared-key")
    assert counts(env)["batches"] == 1 and counts(other)["batches"] == 1


def _seed_content_row(env: BatchEnv, project_id: uuid.UUID | None = None) -> uuid.UUID:
    content_id = uuid.uuid4()
    sql(
        env,
        "INSERT INTO content_items (id, project_id, state, current_version_no, lock_version)"
        " VALUES (:c, :p, 'DRAFT', 0, 0)",
        {"c": content_id, "p": project_id or env.project_id},
    )
    return content_id


def _enqueue_raw_job(
    env: BatchEnv, *, batch_id: uuid.UUID | None, content_id: uuid.UUID | None
) -> uuid.UUID:
    with session_scope(env.settings) as session:
        view = jobs_service.enqueue_job(
            JobRepository(session),
            kind="CONTENT_GENERATE",
            queue="content",
            idempotency_scope="wp32-probe",
            idempotency_key=f"probe-{uuid.uuid4().hex}",
            input_hash="h",
            project_id=env.project_id,
            batch_id=batch_id,
            content_id=content_id,
        )
        return view.job_id


def _commit_expecting(env: BatchEnv, work: Any, sqlstate: str) -> None:
    """在一个事务里执行 `work(session)`，随后 `SET CONSTRAINTS ALL IMMEDIATE` 强制检查延迟约束。"""
    with session_scope(env.settings) as session:
        nested = session.begin_nested()
        with pytest.raises(DBAPIError) as exc:
            work(session)
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert _sqlstate(exc.value) == sqlstate
        nested.rollback()


def test_db_item_count_trigger_both_directions(env: BatchEnv) -> None:
    """T-ATOM-03：quantity 与成员行数不一致（少 / 多）→ 延迟触发器 23514。"""
    content_a, content_b, content_c = (_seed_content_row(env) for _ in range(3))

    def fewer(session: Any) -> None:
        params = _raw_batch_params(env, q=2)
        session.execute(text(_INSERT_BATCH), params)
        session.execute(
            text("INSERT INTO batch_items (batch_id, ordinal, content_item_id) VALUES (:b,1,:c)"),
            {"b": params["id"], "c": content_a},
        )

    def more(session: Any) -> None:
        params = _raw_batch_params(env, q=1)
        session.execute(text(_INSERT_BATCH), params)
        for ordinal, content in ((1, content_b), (2, content_c)):
            session.execute(
                text(
                    "INSERT INTO batch_items (batch_id, ordinal, content_item_id)"
                    " VALUES (:b,:o,:c)"
                ),
                {"b": params["id"], "o": ordinal, "c": content},
            )

    _commit_expecting(env, fewer, CHECK_VIOLATION)
    _commit_expecting(env, more, CHECK_VIOLATION)


def test_db_item_count_trigger_blocks_late_insert_after_commit(env: BatchEnv) -> None:
    """批次提交后再往里塞成员（INSERT 是允许的）→ 行数超过 quantity → 23514。"""
    created = do_create(env, quantity=2)
    extra = _seed_content_row(env)

    def late(session: Any) -> None:
        session.execute(
            text("INSERT INTO batch_items (batch_id, ordinal, content_item_id) VALUES (:b,3,:c)"),
            {"b": created.batch.id, "c": extra},
        )

    _commit_expecting(env, late, CHECK_VIOLATION)


def test_db_item_ownership_trigger(env: BatchEnv) -> None:
    """T-ATOM-04：成员内容属于别的项目 → 23514。"""
    foreign = make_env()
    foreign_content = _seed_content_row(foreign)

    def cross(session: Any) -> None:
        params = _raw_batch_params(env, q=1)
        session.execute(text(_INSERT_BATCH), params)
        session.execute(
            text("INSERT INTO batch_items (batch_id, ordinal, content_item_id) VALUES (:b,1,:c)"),
            {"b": params["id"], "c": foreign_content},
        )

    _commit_expecting(env, cross, CHECK_VIOLATION)


def test_db_run_ownership_trigger(env: BatchEnv) -> None:
    """T-ATOM-05：执行轮次的 job 其 batch_id 为空 / 指向别的批次 / content_id 不符 → 23514。"""
    created = do_create(env, quantity=1)
    item_id = scalar(env, "SELECT id FROM batch_items WHERE batch_id=:b", {"b": created.batch.id})
    content_id = scalar(
        env, "SELECT content_item_id FROM batch_items WHERE id=:i", {"i": item_id}
    )
    # `jobs.batch_id` 已有外键（迁移 b2f32c0de002）⇒ "别的批次"必须是一个真实存在的批次，
    # 不能再用随手 uuid4()（那会在 INSERT jobs 时先撞外键，测不到本触发器）。
    other_batch = do_create(env, quantity=1).batch.id
    jobs_for = {
        "null_batch": _enqueue_raw_job(env, batch_id=None, content_id=content_id),
        "other_batch": _enqueue_raw_job(env, batch_id=other_batch, content_id=content_id),
        "other_content": _enqueue_raw_job(
            env, batch_id=created.batch.id, content_id=uuid.uuid4()
        ),
    }
    for job_id in jobs_for.values():

        def run_with(session: Any, job_id: uuid.UUID = job_id) -> None:
            session.execute(
                text(
                    "INSERT INTO batch_item_runs (batch_item_id, round_no, job_id)"
                    " VALUES (:i, 9, :j)"
                ),
                {"i": item_id, "j": job_id},
            )

        _commit_expecting(env, run_with, CHECK_VIOLATION)


def test_db_content_belongs_to_one_batch_only(env: BatchEnv) -> None:
    """T-DB-02：一条内容至多属于一个批次（扩产内容不含试产内容的库级落点）。"""
    created = do_create(env, quantity=1)
    content_id = scalar(
        env, "SELECT content_item_id FROM batch_items WHERE batch_id=:b", {"b": created.batch.id}
    )
    with session_scope(env.settings) as session:
        params = _raw_batch_params(env, q=1, seq=9)
        session.execute(text(_INSERT_BATCH), params)
        nested = session.begin_nested()
        with pytest.raises(DBAPIError) as exc:
            session.execute(
                text(
                    "INSERT INTO batch_items (batch_id, ordinal, content_item_id)"
                    " VALUES (:b,1,:c)"
                ),
                {"b": params["id"], "c": content_id},
            )
        assert _sqlstate(exc.value) == UNIQUE_VIOLATION
        assert exc.value.orig.diag.constraint_name == "uq_batch_items_content_item_id"  # type: ignore[union-attr]
        nested.rollback()
        session.rollback()


def test_db_items_and_runs_are_immutable(env: BatchEnv) -> None:
    """T-DB-08：成员 / 轮次的 UPDATE / DELETE → 23001。"""
    created = do_create(env, quantity=1)
    for statement in (
        "UPDATE batch_items SET ordinal = 5 WHERE batch_id = :b",
        "DELETE FROM batch_items WHERE batch_id = :b",
        "UPDATE batch_item_runs SET round_no = 5 WHERE batch_item_id IN"
        " (SELECT id FROM batch_items WHERE batch_id = :b)",
        "DELETE FROM batch_item_runs WHERE batch_item_id IN"
        " (SELECT id FROM batch_items WHERE batch_id = :b)",
    ):
        with session_scope(env.settings) as session:
            nested = session.begin_nested()
            with pytest.raises(DBAPIError) as exc:
                session.execute(text(statement), {"b": created.batch.id})
            assert _sqlstate(exc.value) == IMMUTABLE_RECORD_SQLSTATE
            nested.rollback()


def test_db_item_scene_form_pair_and_blank_checks(env: BatchEnv) -> None:
    """T-DB-07：scene_key / form_key 一有一无失败；空串失败。"""
    created = do_create(env, quantity=1)
    for scene, form, name in (
        ("s", None, "ck_batch_items_assignment_paired"),
        (None, "f", "ck_batch_items_assignment_paired"),
        ("", "f", "ck_batch_items_scene_key_not_blank"),
        ("s", "", "ck_batch_items_form_key_not_blank"),
    ):
        content = _seed_content_row(env)
        with session_scope(env.settings) as session:
            nested = session.begin_nested()
            with pytest.raises(DBAPIError) as exc:
                session.execute(
                    text(
                        "INSERT INTO batch_items (batch_id, ordinal, content_item_id,"
                        " scene_key, form_key) VALUES (:b, 99, :c, :s, :f)"
                    ),
                    {"b": created.batch.id, "c": content, "s": scene, "f": form},
                )
            assert _sqlstate(exc.value) == CHECK_VIOLATION
            assert exc.value.orig.diag.constraint_name == name  # type: ignore[union-attr]
            nested.rollback()


def test_db_foreign_keys_restrict_delete(env: BatchEnv) -> None:
    """T-DB-09：FK 全部 ON DELETE RESTRICT。"""
    rows = sql(
        env,
        "SELECT conrelid::regclass::text, confdeltype FROM pg_constraint"
        " WHERE contype='f' AND conrelid::regclass::text IN ('batches','batch_items','batch_item_runs')",
    )
    assert rows
    assert {row[1] for row in rows} == {"r"}, rows


def test_db_has_six_triggers_and_three_functions(env: BatchEnv) -> None:
    triggers = {
        row[0]
        for row in sql(
            env,
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgrelid::regclass::text"
            " IN ('batches','batch_items','batch_item_runs')",
        )
    }
    assert triggers == set(b_ddl.trigger_names())
    deferred = sql(
        env,
        "SELECT tgname, tgdeferrable, tginitdeferred FROM pg_trigger WHERE tgname = ANY(:n)",
        {"n": list(b_ddl.trigger_names()[2:])},
    )
    assert len(deferred) == 4 and all(row[1] and row[2] for row in deferred)


# ===========================================================================
# 4. 服务层：创建
# ===========================================================================


def test_create_batch_happy_path_writes_everything_consistently(env: BatchEnv) -> None:
    before = counts(env)
    result = do_create(env, quantity=5, key="happy")
    assert result.replayed is False
    after = counts(env)
    assert after["batches"] == before["batches"] + 1
    assert after["items"] == 5 and after["runs"] == 5
    assert after["contents"] == 5 and after["jobs"] == 5
    # 版本节点：PLAN 项目链（提交方案时 1 个）+ 基线 + 本批快照节点 1 个。
    assert after["nodes"] == before["nodes"] + 1

    batch_id = result.batch.id
    detail = detail_of(env, batch_id)
    assert detail["state"] == "SUBMITTED"
    assert detail["displayCode"] == "BATCH-S01"
    assert detail["quantity"] == 5
    assert detail["limit"]["applied"] == 30
    assert detail["limit"]["source"] in {"GLOBAL", "INHERITED", "MANUAL"}
    assert detail["baselineState"] == "CURRENT"
    assert detail["baselineId"] == str(env.baseline_id)
    assert detail["planVersionId"] == str(env.plan_version_id)
    # 进度：5 条都 QUEUED。
    assert detail["progress"]["state"] == "QUEUED"
    assert detail["progress"]["total"] == 5
    assert detail["progress"]["counts"]["QUEUED"] == 5
    assert detail["progress"]["needsReconciliation"] == 0
    assert detail["reviewSummary"] is None

    # 成员 / 内容 / 任务三处是同一件事。
    rows = sql(
        env,
        "SELECT bi.ordinal, bi.content_item_id, bi.scene_key, bi.form_key,"
        "       ci.scene_key, ci.form_key, j.batch_id, j.content_id, j.kind, j.queue, r.round_no"
        "  FROM batch_items bi"
        "  JOIN content_items ci ON ci.id = bi.content_item_id"
        "  JOIN batch_item_runs r ON r.batch_item_id = bi.id"
        "  JOIN jobs j ON j.id = r.job_id"
        " WHERE bi.batch_id = :b ORDER BY bi.ordinal",
        {"b": batch_id},
    )
    assert [row[0] for row in rows] == [1, 2, 3, 4, 5]
    for row in rows:
        assert row[2] == row[4] and row[3] == row[5]  # 与 content_items 一致
        assert row[2] is None and row[3] is None  # OP-227：联合展开规则未定义，不编造
        assert row[6] == batch_id and row[7] == row[1]
        assert (row[8], row[9], row[10]) == ("CONTENT_GENERATE", "content", 1)


def test_create_writes_plan_snapshot_node_with_explicit_revision_and_edges(env: BatchEnv) -> None:
    """T-VER：快照节点 (PLAN, batch_id, rev 1)；payload 摘要 == allocation 列的摘要；两条依赖边。"""
    result = do_create(env, quantity=4)
    batch = result.batch
    node = sql(
        env,
        "SELECT id, revision_no, payload_hash, project_id FROM version_nodes"
        " WHERE entity_type='PLAN' AND entity_id=:b",
        {"b": batch.id},
    )
    assert len(node) == 1
    node_id, revision_no, payload_hash, project_id = node[0]
    assert revision_no == 1 and project_id == env.project_id
    assert payload_hash == canonical_payload_hash(batch.allocation)  # T-VER-04
    purposes = {
        row[0]: row[1]
        for row in sql(
            env,
            "SELECT purpose, dependency_version_id FROM version_dependencies"
            " WHERE parent_version_id=:n",
            {"n": node_id},
        )
    }
    assert purposes == {
        "BATCH_PLAN": env.plan_version_id,
        "BATCH_BASELINE": env.baseline_id,
    }
    # 分配快照：场景 / 形式各自合计 == quantity，且无未分配。
    allocation = batch.allocation
    assert sum(line["allocated"] for line in allocation["scenes"]) == 4
    assert sum(line["allocated"] for line in allocation["forms"]) == 4
    assert allocation["unallocated"] == {"scene": 0, "form": 0}
    assert "createdAt" not in allocation and "createdBy" not in allocation


def test_register_snapshot_passes_explicit_revision_no(
    env: BatchEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-VER-01：创建批次调用 `register_version` 时 `revision_no` 显式为 1（spy 断言非 None）。"""
    seen: list[dict[str, Any]] = []
    real = versions_bridge.register_version

    def spy(repo: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(repo, **kwargs)

    monkeypatch.setattr(versions_bridge, "register_version", spy)
    do_create(env, quantity=2)
    assert len(seen) == 1
    assert seen[0]["revision_no"] == 1 and seen[0]["entity_type"] == "PLAN"


def test_snapshot_revision_is_idempotent_but_auto_numbering_is_not(env: BatchEnv) -> None:
    """T-VER-03：显式 revision_no=1 的重放 → created=False；自动取号 → 出现第 2 版（OP-91 活体复现）。"""
    result = do_create(env, quantity=2)
    batch = result.batch
    with session_scope(env.settings) as session:
        again = versions_bridge.register_snapshot(
            session,
            batch_id=batch.id,
            project_id=env.project_id,
            payload=batch.allocation,
            created_by=env.owner_id,
            revision_no=1,
        )
        assert again.created is False
        auto = register_version(
            VersionRepository(session),
            entity_type="PLAN",
            entity_id=batch.id,
            payload=batch.allocation,
            project_id=env.project_id,
            created_by=env.owner_id,
        )
        assert auto.created is True and auto.revision_no == 2


def test_create_estimates_per_task_kind_and_never_reserves(env: BatchEnv) -> None:
    """T-COST-01 / 04：FULL = 2 个 task_kind；金额 = 2 x 5 x 单价；批次层不预占。"""
    before = counts(env)
    budget_before = sql(
        env, "SELECT reserved_amount, committed_amount FROM project_budgets WHERE project_id=:p",
        {"p": env.project_id},
    )
    result = do_create(env, quantity=5)
    batch = result.batch
    assert len(batch.estimate_refs) == 2
    assert {ref["taskKind"] for ref in batch.estimate_refs} == {
        "CONTENT_STRUCTURE",
        "CONTENT_COPY",
    }
    assert batch.estimated_currency == "CNY"
    assert batch.estimated_amount == Decimal(PRICE_CALL) * 2 * 5
    after = counts(env)
    assert after["estimates"] == before["estimates"] + 2
    assert after["reservations"] == before["reservations"]  # 不预占
    assert (
        sql(
            env,
            "SELECT reserved_amount, committed_amount FROM project_budgets WHERE project_id=:p",
            {"p": env.project_id},
        )
        == budget_before
    )
    # 只算文案：1 个 task_kind。
    copy_only = do_create(env, quantity=5, scope="COPY")
    assert len(copy_only.batch.estimate_refs) == 1
    assert copy_only.batch.estimated_amount == Decimal(PRICE_CALL) * 5


def test_create_writes_audit(env: BatchEnv) -> None:
    result = do_create(env, quantity=1)
    assert (
        scalar(
            env,
            "SELECT count(*) FROM audit_events WHERE action='batch.create' AND object_id=:b",
            {"b": result.batch.id},
        )
        == 1
    )


# --- 4.1 幂等 ---------------------------------------------------------------


def test_idempotent_replay_writes_nothing_and_returns_same_batch(env: BatchEnv) -> None:
    """T-IDEM-01：同键同体重放 → 各表行数全部不变，返回同一批次。"""
    first = do_create(env, quantity=3, key="replay-me")
    snapshot = counts(env)
    second = do_create(env, quantity=3, key="replay-me")
    assert second.replayed is True and second.batch.id == first.batch.id
    assert counts(env) == snapshot


def test_idempotent_replay_still_works_after_all_jobs_finished(env: BatchEnv) -> None:
    """T-IDEM-07：job 全部完成之后重放，仍返回原批次，不新建任务、不重复预估。"""
    first = do_create(env, quantity=2, key="after-done")
    for job_id in job_ids_of(env, first.batch.id):
        set_job_state(env, job_id, "SUCCEEDED")
    snapshot = counts(env)
    again = do_create(env, quantity=2, key="after-done")
    assert again.replayed is True and again.batch.id == first.batch.id
    assert counts(env) == snapshot


@pytest.mark.parametrize(
    "change",
    [
        {"quantity": 4},
        {"kind": "BULK"},
        {"scope": "COPY"},
        {"baseline_id": uuid.uuid4()},
    ],
)
def test_same_key_different_body_is_409(env: BatchEnv, change: dict[str, Any]) -> None:
    """T-IDEM-02：同键异体 → 409 IDEMPOTENCY_CONFLICT，无新增行。"""
    do_create(env, quantity=3, key="same-key")
    snapshot = counts(env)
    kwargs: dict[str, Any] = {"quantity": 3, **change}
    with pytest.raises(ApiError) as exc:
        do_create(env, key="same-key", **kwargs)
    assert_api_error(exc, ErrorCode.IDEMPOTENCY_CONFLICT, "idempotency_key_reused")
    assert "existingBatchId" in exc.value.details
    assert counts(env) == snapshot


def test_same_key_different_model_profile_or_plan_is_409(env: BatchEnv) -> None:
    do_create(env, quantity=3, key="k2")
    other_profile = CreateBatchCommand(
        "SCALE", 3, env.plan_version_id, None, uuid.uuid4(), "FULL"
    )
    with pytest.raises(ApiError) as exc:
        do_create(env, key="k2", command=other_profile)
    assert_api_error(exc, ErrorCode.IDEMPOTENCY_CONFLICT, "idempotency_key_reused")
    other_plan = CreateBatchCommand(
        "SCALE", 3, uuid.uuid4(), None, env.model_profile_id, "FULL"
    )
    with pytest.raises(ApiError) as exc2:
        do_create(env, key="k2", command=other_plan)
    assert_api_error(exc2, ErrorCode.IDEMPOTENCY_CONFLICT, "idempotency_key_reused")


@pytest.mark.parametrize("key", [None, "", "   "])
def test_idempotency_key_is_required(env: BatchEnv, key: str | None) -> None:
    """T-IDEM-06：缺失 / 空白 → 422 idempotency_key_required。"""
    with session_scope(env.settings) as session:
        with pytest.raises(ApiError) as exc:
            b_service.create_batch(
                BatchRepository(session),
                project_id=env.project_id,
                command=command_of(env),
                idempotency_key=key,
                gate_checker=GATE_OK,
                actor_id=env.owner_id,
                request_id="r",
            )
    assert_api_error(exc, ErrorCode.VALIDATION_FAILED, "idempotency_key_required")


def test_concurrent_same_key_creates_exactly_one_batch(env: BatchEnv) -> None:
    """T-IDEM-03：N 线程同键同体并发 → 恰好 1 个批次；恰 1 个首次、其余重放；无异常。"""
    threads_n = 8
    barrier = threading.Barrier(threads_n)
    results: list[Any] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=20)
            results.append(do_create(env, quantity=2, key="race-key"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not errors, errors
    assert len(results) == threads_n
    assert len({r.batch.id for r in results}) == 1
    assert sum(1 for r in results if not r.replayed) == 1
    snapshot = counts(env)
    assert snapshot["batches"] == 1 and snapshot["items"] == 2 and snapshot["jobs"] == 2


def test_insert_batch_conflict_is_caught_inside_savepoint(env: BatchEnv) -> None:
    """T-IDEM-04：撞幂等键的输家在 SAVEPOINT 内处理，之后同一事务里仍能继续执行语句。"""
    from rednote.modules.batches.repository import BatchInsertConflict

    first = do_create(env, quantity=1, key="sp-key")
    with session_scope(env.settings) as session:
        repo = BatchRepository(session)
        with pytest.raises(BatchInsertConflict) as exc:
            repo.insert_batch(
                project_id=env.project_id,
                batch_kind="SCALE",
                seq_no=repo.next_seq_no(project_id=env.project_id, batch_kind="SCALE"),
                quantity=1,
                plan_version_id=env.plan_version_id,
                baseline_id=env.baseline_id,
                allocation={},
                limit_applied=30,
                limit_source="GLOBAL",
                estimate_refs=[],
                idempotency_key="sp-key",
                request_hash="x",
            )
        assert exc.value.constraint == "uq_batches_project_idempotency_key"
        # 事务没有 aborted：还能回查赢家。
        winner = repo.find_by_idempotency(project_id=env.project_id, idempotency_key="sp-key")
        assert winner is not None and winner.id == first.batch.id


def test_seq_numbers_increase_per_kind(env: BatchEnv) -> None:
    a = do_create(env, quantity=1)
    b = do_create(env, quantity=1)
    c = do_create(env, quantity=1, kind="BULK")
    assert (a.batch.seq_no, b.batch.seq_no, c.batch.seq_no) == (1, 2, 1)


# --- 4.2 数量（ADR-13 / AC-10~12） ------------------------------------------


def _effective_limits(env: BatchEnv) -> Any:
    with session_scope(env.settings) as session:
        setting = planning_bridge.read_batch_limits(
            session, project_id=env.project_id, materialize=True
        )
        return setting.as_limits(), setting.source


@pytest.mark.parametrize("kind", ["SCALE", "BULK"])
def test_quantity_out_of_range_is_422_with_limit_and_source(env: BatchEnv, kind: str) -> None:
    """T-QTY-01：上界读生效配置构造（不写死）；越界 422 且带 reason + limit + source，无任何写入。"""
    limits, source = _effective_limits(env)
    upper = limits.max_for(kind)
    snapshot = counts(env)
    for quantity, reason in ((0, "quantity_below_min"), (upper + 1, "quantity_above_max")):
        with pytest.raises(ApiError) as exc:
            do_create(env, kind=kind, quantity=quantity)
        assert_api_error(exc, ErrorCode.VALIDATION_FAILED, reason)
        if reason == "quantity_above_max":
            assert exc.value.details["limit"] == upper
            assert exc.value.details["source"] == source
    assert counts(env)["batches"] == snapshot["batches"] == 0
    assert counts(env)["jobs"] == snapshot["jobs"] == 0


@pytest.mark.parametrize("kind", ["SCALE", "BULK"])
def test_quantity_at_upper_bound_succeeds(kind: str) -> None:
    """T-QTY-01：边界内成功，`limit_applied` = 创建时刻生效上界。"""
    env = make_env(total_quantity=200)
    limits, _source = _effective_limits(env)
    upper = limits.max_for(kind)
    result = do_create(env, kind=kind, quantity=upper)
    assert result.batch.quantity == upper and result.batch.limit_applied == upper
    assert counts(env)["items"] == upper


def test_limits_change_applies_immediately_and_snapshots_manual_source(env: BatchEnv) -> None:
    """T-QTY-02 / 03：改 batch.limits 后不改代码立即生效；项目级覆盖时 source=MANUAL 且快照一致。"""
    with session_scope(env.settings) as session:
        change_project_setting(
            PlanningRepository(session),
            key=SETTING_KEY_BATCH_LIMITS,
            project_id=env.project_id,
            value={"TRIAL": 10, "SCALE": 3, "BULK": 50},
            reason="WP32 测试：项目级覆盖",
            actor_id=env.owner_id,
        )
    with pytest.raises(ApiError) as exc:
        do_create(env, quantity=4)
    assert_api_error(exc, ErrorCode.VALIDATION_FAILED, "quantity_above_max")
    assert exc.value.details["limit"] == 3 and exc.value.details["source"] == "MANUAL"
    ok = do_create(env, quantity=3)
    assert ok.batch.limit_applied == 3 and ok.batch.limit_source == "MANUAL"


@pytest.mark.parametrize("kind", ["TRIAL", "WEEKLY", ""])
def test_batch_kind_other_than_scale_bulk_is_422(env: BatchEnv, kind: str) -> None:
    """T-QTY-07：TRIAL 由 WP31 自建，本包不创建；未知档位同拒。"""
    with pytest.raises(ApiError) as exc:
        do_create(env, kind=kind, quantity=2)
    assert_api_error(exc, ErrorCode.VALIDATION_FAILED, "batch_kind_unknown")
    assert counts(env)["batches"] == 0


def test_unknown_scope_is_422_without_writes(env: BatchEnv) -> None:
    with pytest.raises(ApiError) as exc:
        do_create(env, scope="EVERYTHING")
    assert_api_error(exc, ErrorCode.VALIDATION_FAILED, "generation_request_invalid")
    assert counts(env)["batches"] == 0


def test_limits_not_configured_is_422_not_default(env: BatchEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """T-QTY-06：未配置不兜底默认值。"""
    from rednote.modules.planning.types import EffectiveSetting

    unconfigured = EffectiveSetting(
        key=SETTING_KEY_BATCH_LIMITS, configured=False, value=None, source=None,
        global_value=None, diverged=False, revision_no=None, project_id=env.project_id,
    )
    monkeypatch.setattr(planning_bridge, "read_batch_limits", lambda *a, **k: unconfigured)
    with pytest.raises(ApiError) as exc:
        do_create(env, quantity=1)
    assert_api_error(exc, ErrorCode.VALIDATION_FAILED, "limits_not_configured")


# --- 4.3 关口（gate_checker） -------------------------------------------------


def test_gate_not_passed_blocks_creation_with_latest_decision(env: BatchEnv) -> None:
    decision_id = uuid.uuid4()
    decided_at = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def not_passed(_pid: uuid.UUID) -> GateVerdict:
        return GateVerdict(passed=False, decision_id=decision_id, decided_at=decided_at, reason="REJECTED")

    with pytest.raises(ApiError) as exc:
        do_create(env, quantity=2, gate=not_passed)
    assert_api_error(exc, ErrorCode.INVALID_TRANSITION, "gate_not_satisfied")
    assert exc.value.details["decisionId"] == str(decision_id)
    assert exc.value.details["gateReason"] == "REJECTED"
    snapshot = counts(env)
    assert snapshot["batches"] == 0 and snapshot["jobs"] == 0 and snapshot["estimates"] == 0


def test_gate_not_wired_is_fail_closed_503(env: BatchEnv) -> None:
    with pytest.raises(ApiError) as exc:
        do_create(env, quantity=2, gate=default_gate_checker)
    assert_api_error(exc, ErrorCode.DEPENDENCY_UNAVAILABLE, "trial_gate_not_wired")
    assert counts(env)["batches"] == 0


def test_gate_passed_allows_creation_and_checker_receives_project_id(env: BatchEnv) -> None:
    seen: list[uuid.UUID] = []

    def passed(pid: uuid.UUID) -> GateVerdict:
        seen.append(pid)
        return GateVerdict(passed=True, decision_id=uuid.uuid4())

    do_create(env, quantity=1, gate=passed)
    assert seen == [env.project_id]


def test_replay_does_not_consult_gate(env: BatchEnv) -> None:
    """重放走既有批次，不重判关口（关口之后变化不应让已建批次的重放变成 409 / 503）。"""
    do_create(env, quantity=1, key="gate-replay")
    again = do_create(env, quantity=1, key="gate-replay", gate=default_gate_checker)
    assert again.replayed is True


# --- 4.4 前提校验 ---------------------------------------------------------------


def test_plan_version_must_be_this_projects_plan_node(env: BatchEnv) -> None:
    other = make_env()
    for bad in (uuid.uuid4(), other.plan_version_id, env.baseline_id):
        with pytest.raises(ApiError) as exc:
            do_create(env, plan_version_id=bad, quantity=1)
        assert_api_error(exc, ErrorCode.VALIDATION_FAILED, "plan_version_invalid")
    assert counts(env)["batches"] == 0


def test_non_head_plan_version_is_409_plan_changed(env: BatchEnv) -> None:
    """方案被重新提交（链头前进）后，旧方案版本不能再建批。"""
    with session_scope(env.settings) as session:
        register_version(
            VersionRepository(session),
            entity_type="PLAN",
            entity_id=env.project_id,
            payload={"probe": "newer plan"},
            project_id=env.project_id,
            created_by=env.owner_id,
        )
    with pytest.raises(ApiError) as exc:
        do_create(env, quantity=1)
    assert_api_error(exc, ErrorCode.INVALID_TRANSITION, "plan_changed")


def test_draft_edited_after_submit_is_409_plan_changed(env: BatchEnv) -> None:
    """计划草稿在提交后被改：重算摘要与钉住的节点不一致（“不能先通过后补查”）。"""
    sql(env, "UPDATE plan_drafts SET total_quantity = total_quantity + 1 WHERE project_id=:p",
        {"p": env.project_id})
    with pytest.raises(ApiError) as exc:
        do_create(env, quantity=1)
    assert_api_error(exc, ErrorCode.INVALID_TRANSITION, "plan_changed")
    assert counts(env)["batches"] == 0


def test_baseline_mismatch_and_missing(env: BatchEnv) -> None:
    with pytest.raises(ApiError) as exc:
        do_create(env, quantity=1, baseline_id=uuid.uuid4())
    assert_api_error(exc, ErrorCode.INVALID_TRANSITION, "baseline_changed")
    # 显式等于当前基线 → 通过。
    ok = do_create(env, quantity=1, baseline_id=env.baseline_id)
    assert ok.batch.baseline_id == env.baseline_id
    # 无生效基线 → baseline_not_ready。
    sql(env, "UPDATE projects SET active_baseline_id = NULL WHERE id=:p", {"p": env.project_id})
    with pytest.raises(ApiError) as exc2:
        do_create(env, quantity=1)
    assert_api_error(exc2, ErrorCode.INVALID_TRANSITION, "baseline_not_ready")


def _seed_superseding_baseline(env: BatchEnv) -> uuid.UUID:
    """造一条取代当前基线的新基线并把 `active_baseline_id` 指向它。"""
    from rednote.modules.baseline.repository import BaselineRepository

    with session_scope(env.settings) as session:
        new_id = register_version(
            VersionRepository(session),
            entity_type="BASELINE",
            entity_id=env.project_id,
            payload={"probe": "baseline v2"},
            project_id=env.project_id,
            created_by=env.owner_id,
        ).node_id
        repo = BaselineRepository(session)
        repo.insert_baseline(
            baseline_id=new_id,
            project_id=env.project_id,
            brief_version_id=None,
            ruleset_version="v1",
            snapshot={"probe": True},
            supersedes_id=env.baseline_id,
            change_reason="WP32 测试：新基线取代旧基线",
            created_by=env.owner_id,
        )
        repo.insert_approval(
            project_id=env.project_id,
            step_no=3,
            subject_kind="BASELINE",
            subject_version_id=new_id,
            decision="APPROVED",
            reason="WP32 测试：批准新基线",
            decided_by=env.owner_id,
            supersedes_id=None,
        )
        repo.set_active_baseline(project_id=env.project_id, baseline_id=new_id)
        return new_id


def test_baseline_state_is_superseded_after_baseline_moves(env: BatchEnv) -> None:
    """T-PRE-02：基线在创建后变化 → baselineState=SUPERSEDED，批次本身不变；无生效基线 → UNKNOWN。"""
    created = do_create(env, quantity=1)
    new_baseline = _seed_superseding_baseline(env)
    detail = detail_of(env, created.batch.id)
    assert detail["baselineState"] == "SUPERSEDED"
    assert detail["baselineId"] == str(env.baseline_id) != str(new_baseline)
    sql(env, "UPDATE projects SET active_baseline_id = NULL WHERE id=:p", {"p": env.project_id})
    assert detail_of(env, created.batch.id)["baselineState"] == "UNKNOWN"


# --- 4.5 分配 -------------------------------------------------------------------


def test_allocation_accumulates_across_batches_and_rejects_incomplete() -> None:
    """T-ALLOC-01 / 02：延续配比（allocated_so_far 来自历史批次 allocation）；分不完 → 422 且无写入。"""
    env = make_env(
        scenes=_scene_items((500, 3), (500, 3)),
        forms=_form_items((1000, None)),
        total_quantity=6,
    )
    first = do_create(env, quantity=4)
    second = do_create(env, quantity=2)
    per_scene = {
        line["key"]: line["allocated"]
        for batch in (first.batch, second.batch)
        for line in batch.allocation["scenes"]
    }
    totals: dict[str, int] = {}
    for batch in (first.batch, second.batch):
        for line in batch.allocation["scenes"]:
            totals[line["key"]] = totals.get(line["key"], 0) + line["allocated"]
    assert totals == {"scene-0": 3, "scene-1": 3}, per_scene  # 累计收敛到容量
    snapshot = counts(env)
    with pytest.raises(ApiError) as exc:
        do_create(env, quantity=1)
    assert_api_error(exc, ErrorCode.VALIDATION_FAILED, "allocation_incomplete")
    assert exc.value.details["unallocated"] == 1
    assert counts(env) == snapshot


def test_cancelled_batch_releases_its_allocation() -> None:
    """Q18 默认：CANCEL_REQUESTED 不计入 allocated_so_far（视为释放）。"""
    env = make_env(
        scenes=_scene_items((500, 2), (500, 2)),
        forms=_form_items((1000, None)),
        total_quantity=4,
    )
    first = do_create(env, quantity=4)
    with pytest.raises(ApiError):
        do_create(env, quantity=1)
    with session_scope(env.settings) as session:
        repo = BatchRepository(session)
        b_service.cancel_batch(
            repo,
            batch=b_service.require_batch(repo, first.batch.id),
            expected_lock_version=first.batch.lock_version,
            actor_id=env.owner_id,
            request_id="r",
        )
    assert do_create(env, quantity=2).batch.quantity == 2


def test_allocation_snapshot_is_deterministic_pure_function() -> None:
    from rednote.modules.planning.allocation import allocate_by_weight
    from rednote.modules.planning.types import AllocationItem

    items = [AllocationItem("a", 600, None, 0), AllocationItem("b", 400, None, 1)]
    pv, bl = uuid.uuid4(), uuid.uuid4()
    one = b_service.build_allocation_snapshot(
        batch_kind="BULK", quantity=7, plan_version_id=pv, baseline_id=bl,
        scene_result=allocate_by_weight(items, 7), form_result=allocate_by_weight(items, 7),
    )
    two = b_service.build_allocation_snapshot(
        batch_kind="BULK", quantity=7, plan_version_id=pv, baseline_id=bl,
        scene_result=allocate_by_weight(items, 7), form_result=allocate_by_weight(items, 7),
    )
    assert one == two and canonical_payload_hash(one) == canonical_payload_hash(two)
    assert [line["allocated"] for line in one["scenes"]] == [4, 3]


# --- 4.6 费用预检 ---------------------------------------------------------------


def test_estimate_exceeding_available_budget_is_rejected_without_writes() -> None:
    """T-COST-02：额度不足 → 422 BUDGET_EXCEEDED(batch_estimate_exceeds_available)，无任何写入。"""
    env = make_env(budget="100")  # 预估 = 2 x 5 x 12.5 = 125 > 100
    with pytest.raises(ApiError) as exc:
        do_create(env, quantity=5)
    assert_api_error(exc, ErrorCode.BUDGET_EXCEEDED, "batch_estimate_exceeds_available")
    assert exc.value.details["available"] == "100.00000000"
    snapshot = counts(env)
    assert snapshot["batches"] == 0 and snapshot["jobs"] == 0 and snapshot["estimates"] == 0
    # 恰好等于可用额度 → 通过（不是 >=）。
    env_ok = make_env(budget="125")
    assert do_create(env_ok, quantity=5).batch.estimated_amount == Decimal("125")


def test_budget_not_configured_is_reported_not_zero_available() -> None:
    """T-COST-03：未配置预算报“未配置”，不报 available=0。"""
    env = make_env(budget=None)
    with pytest.raises(ApiError) as exc:
        do_create(env, quantity=2)
    assert_api_error(exc, ErrorCode.BUDGET_EXCEEDED, "budget_not_configured")
    assert exc.value.details["budgetConfigured"] is False
    assert "available" not in exc.value.details


def test_missing_pricing_is_billing_error_passthrough() -> None:
    """没有可用计价版本：billing 的 `ApiError` 原样透传（本包不包装），且无任何写入。"""
    from rednote.modules.billing.types import REASON_PRICING_VERSION_NOT_FOUND

    env = make_env(priced=False)
    with pytest.raises(ApiError) as exc:
        do_create(env, quantity=2)
    assert_api_error(exc, ErrorCode.RESOURCE_NOT_FOUND, REASON_PRICING_VERSION_NOT_FOUND)
    assert counts(env)["batches"] == 0 and counts(env)["jobs"] == 0


def test_unknown_model_profile_is_422_field() -> None:
    env = make_env()
    with pytest.raises(ApiError) as exc:
        do_create(
            env,
            command=CreateBatchCommand("SCALE", 2, env.plan_version_id, None, uuid.uuid4(), "FULL"),
        )
    assert exc.value.code == ErrorCode.VALIDATION_FAILED
    assert exc.value.details["fieldErrors"][0]["field"] == "modelProfileId"


# --- 4.7 原子性 -------------------------------------------------------------------


@pytest.mark.parametrize("fail_at", [1, 3])
def test_failure_midway_leaves_no_residue(
    env: BatchEnv, monkeypatch: pytest.MonkeyPatch, fail_at: int
) -> None:
    """T-ATOM-01 / 02：内容物化后、第 k 个任务入队时失败 → 零残留。"""
    calls = {"n": 0}
    real = jobs_bridge.enqueue_batch_job

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == fail_at:
            raise RuntimeError("注入故障")
        return real(*args, **kwargs)

    monkeypatch.setattr(jobs_bridge, "enqueue_batch_job", flaky)
    before = counts(env)
    with pytest.raises(RuntimeError):
        do_create(env, quantity=4)
    after = counts(env)
    assert after == before
    assert (
        scalar(
            env,
            "SELECT count(*) FROM version_nodes WHERE project_id=:p AND entity_type='PLAN'"
            " AND entity_id <> :p",
            {"p": env.project_id},
        )
        == 0
    )
    assert (
        scalar(
            env,
            "SELECT count(*) FROM audit_events WHERE project_id=:p AND action='batch.create'",
            {"p": env.project_id},
        )
        == 0
    )


# ===========================================================================
# 5. 服务层：进度（真 jobs）/ 取消 / 重做 / 读 / 预览
# ===========================================================================


def _set_all(env: BatchEnv, batch_id: uuid.UUID, states: list[str]) -> list[uuid.UUID]:
    job_ids = job_ids_of(env, batch_id)
    assert len(job_ids) == len(states)
    for job_id, state in zip(job_ids, states, strict=True):
        set_job_state(env, job_id, state)
    return job_ids


def test_progress_reads_latest_jobs_and_keeps_unknown_separate(env: BatchEnv) -> None:
    """T-ROLL：进度读时折算；WAITING_RECONCILIATION 单列，不被折成失败 / 成功。"""
    created = do_create(env, quantity=4)
    _set_all(env, created.batch.id, ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED", "WAITING_RECONCILIATION"])
    progress = detail_of(env, created.batch.id)["progress"]
    assert progress["state"] == "RUNNING"
    assert progress["needsReconciliation"] == 1
    assert progress["counts"]["SUCCEEDED"] == 3 and progress["counts"]["FAILED"] == 0
    assert progress["missing"] == 0

    _set_all(env, created.batch.id, ["SUCCEEDED", "SUCCEEDED", "FAILED", "FAILED"])
    assert detail_of(env, created.batch.id)["progress"]["state"] == "PARTIAL"
    _set_all(env, created.batch.id, ["SUCCEEDED"] * 4)
    assert detail_of(env, created.batch.id)["progress"]["state"] == "SUCCEEDED"
    _set_all(env, created.batch.id, ["FAILED"] * 4)
    assert detail_of(env, created.batch.id)["progress"]["state"] == "FAILED"


def test_progress_member_without_run_is_unknown_not_zero(env: BatchEnv) -> None:
    """T-ROLL-03：造一个无 run 的成员 → UNKNOWN（用 service 之外的路径造：成员行有、轮次行无）。"""
    created = do_create(env, quantity=2)
    # 轮次表不可变，只能 TRUNCATE 摘掉一轮来模拟“成员没有任何 run”。
    sql(env, "TRUNCATE batch_item_runs")
    progress = detail_of(env, created.batch.id)["progress"]
    assert progress["state"] == "UNKNOWN"
    assert progress["missing"] == 2 and progress["total"] == 2


def test_latest_round_is_by_round_no_not_by_timestamp(env: BatchEnv) -> None:
    """T-ROLL-04：同事务插入的两轮（created_at 相同）仍取 round_no 大者。"""
    created = do_create(env, quantity=1)
    item_id, content_id = sql(
        env, "SELECT id, content_item_id FROM batch_items WHERE batch_id=:b", {"b": created.batch.id}
    )[0]
    first_job = job_ids_of(env, created.batch.id)[0]
    set_job_state(env, first_job, "FAILED")
    with session_scope(env.settings) as session:
        repo = BatchRepository(session)
        job = jobs_bridge.enqueue_batch_job(
            session,
            kind="CONTENT_GENERATE",
            queue="content",
            idempotency_scope="content-generate",
            idempotency_key=f"{content_id}:manual:r2",
            input_hash="h",
            project_id=env.project_id,
            batch_id=created.batch.id,
            content_id=content_id,
        )
        repo.insert_run(batch_item_id=item_id, round_no=2, job_id=job.job_id)
    members = BatchRepositoryProbe.members(env, created.batch.id)
    assert members[0]["latest_round"] == 2 and members[0]["job_state"] == "QUEUED"
    assert detail_of(env, created.batch.id)["progress"]["state"] == "QUEUED"


class BatchRepositoryProbe:
    @staticmethod
    def members(env: BatchEnv, batch_id: uuid.UUID) -> list[dict[str, Any]]:
        with session_scope(env.settings) as session:
            return BatchRepository(session).list_members(batch_ids=[batch_id])


def _cancel(env: BatchEnv, batch_id: uuid.UUID) -> tuple[Batch, bool]:
    with session_scope(env.settings) as session:
        repo = BatchRepository(session)
        batch = b_service.require_batch(repo, batch_id)
        return b_service.cancel_batch(
            repo,
            batch=batch,
            expected_lock_version=batch.lock_version,
            actor_id=env.owner_id,
            request_id="req-cancel",
        )


def test_cancel_marks_batch_and_requests_cancel_on_nonterminal_jobs_only(env: BatchEnv) -> None:
    """T-SM-01 / 02：SUBMITTED → CANCEL_REQUESTED；非终态 job 登记取消，终态 job 不动。"""
    created = do_create(env, quantity=3)
    job_ids = _set_all(env, created.batch.id, ["SUCCEEDED", "RUNNING", "QUEUED"])
    before_version = created.batch.lock_version
    fresh, changed = _cancel(env, created.batch.id)
    assert changed is True and fresh.state == "CANCEL_REQUESTED"
    assert fresh.lock_version == before_version + 1
    assert fresh.cancel_requested_at is not None and fresh.cancel_requested_by == env.owner_id
    states = [
        scalar(env, "SELECT state FROM jobs WHERE id=:j", {"j": job_id}) for job_id in job_ids
    ]
    assert states == ["SUCCEEDED", "CANCEL_REQUESTED", "CANCEL_REQUESTED"]
    assert detail_of(env, created.batch.id)["progress"]["state"] == "CANCELLING"
    # 全部终态后折算为 CANCELLED。
    _set_all(env, created.batch.id, ["SUCCEEDED", "CANCELLED", "CANCELLED"])
    assert detail_of(env, created.batch.id)["progress"]["state"] == "CANCELLED"
    # 审计。
    assert (
        scalar(
            env,
            "SELECT count(*) FROM audit_events WHERE action='batch.cancel' AND object_id=:b",
            {"b": created.batch.id},
        )
        == 1
    )


def test_repeated_cancel_is_idempotent_and_irreversible(env: BatchEnv) -> None:
    created = do_create(env, quantity=1)
    first, changed1 = _cancel(env, created.batch.id)
    again, changed2 = _cancel(env, created.batch.id)
    assert (changed1, changed2) == (True, False)
    assert again.lock_version == first.lock_version  # 不再增长
    assert again.state == "CANCEL_REQUESTED"
    # 不可恢复：没有任何把它改回 SUBMITTED 的写路径（DB 的双向等值也拒绝）。
    with session_scope(env.settings) as session:
        nested = session.begin_nested()
        with pytest.raises(DBAPIError) as exc:
            session.execute(
                text("UPDATE batches SET state='SUBMITTED' WHERE id=:b"), {"b": created.batch.id}
            )
        assert _sqlstate(exc.value) == CHECK_VIOLATION
        nested.rollback()


def test_cancel_with_stale_lock_version_is_412_with_current_etag(env: BatchEnv) -> None:
    created = do_create(env, quantity=1)
    with session_scope(env.settings) as session:
        repo = BatchRepository(session)
        batch = b_service.require_batch(repo, created.batch.id)
        with pytest.raises(ApiError) as exc:
            b_service.cancel_batch(
                repo,
                batch=batch,
                expected_lock_version=batch.lock_version + 5,
                actor_id=env.owner_id,
                request_id="r",
            )
    assert_api_error(exc, ErrorCode.VERSION_CONFLICT, "lock_version_mismatch")
    # currentEtag 取自重读后的当前版本，不是传入的旧值。
    assert exc.value.details["currentEtag"].strip('"').endswith(f":{created.batch.lock_version}")
    assert detail_of(env, created.batch.id)["state"] == "SUBMITTED"


def _retry(
    env: BatchEnv,
    batch_id: uuid.UUID,
    *,
    key: str | None = "retry-1",
    expected_lock_version: int | None = None,
) -> b_service.RetryResult:
    from rednote.platform.concurrency import version_conflict

    with session_scope(env.settings) as session:
        repo = BatchRepository(session)
        batch = b_service.require_batch(repo, batch_id)

        def check(lock_version: int) -> None:
            if expected_lock_version is not None and expected_lock_version != lock_version:
                raise version_conflict(
                    resource_id=batch_id,
                    current_version=lock_version,
                    reason="version_mismatch",
                    message="stale",
                )

        return b_service.retry_failed(
            repo,
            batch=batch,
            idempotency_key=key,
            check_if_match=check,
            actor_id=env.owner_id,
            request_id="req-retry",
        )


def test_retry_only_redoes_latest_failed_and_never_touches_unknown(env: BatchEnv) -> None:
    """T-RETRY-01 / 07：只重做 FAILED；SUCCEEDED / CANCELLED / WAITING_* / PARTIAL 不产生新 run。"""
    created = do_create(env, quantity=6)
    _set_all(
        env,
        created.batch.id,
        ["FAILED", "SUCCEEDED", "WAITING_RECONCILIATION", "WAITING_PROVIDER", "PARTIAL", "FAILED"],
    )
    before = counts(env)
    result = _retry(env, created.batch.id)
    assert result.replayed is False and result.body["retriedCount"] == 2
    assert {item["roundNo"] for item in result.body["retried"]} == {2}
    after = counts(env)
    assert after["runs"] == before["runs"] + 2 and after["jobs"] == before["jobs"] + 2
    # 批次 / 成员 / 分配不变（T-RETRY-07）。
    assert after["items"] == before["items"] and after["batches"] == before["batches"]
    assert detail_of(env, created.batch.id)["quantity"] == 6
    # 新 job 带 batch_id / content_id，键含轮次。
    rows = sql(
        env,
        "SELECT j.batch_id, j.content_id, j.idempotency_key, j.state FROM batch_item_runs r"
        " JOIN jobs j ON j.id = r.job_id WHERE r.round_no = 2",
    )
    assert len(rows) == 2
    for batch_id, content_id, key, state in rows:
        assert batch_id == created.batch.id and key == f"{content_id}:batch:{batch_id}:r2"
        assert state == "QUEUED"
    # 锁版本推进，审计一条。
    assert detail_of(env, created.batch.id)["etag"].endswith(f":{created.batch.lock_version + 1}\"")
    assert (
        scalar(
            env,
            "SELECT count(*) FROM audit_events WHERE action='batch.retry' AND object_id=:b",
            {"b": created.batch.id},
        )
        == 1
    )
    # 进度仍有未知态：RUNNING，且 needsReconciliation=1。
    progress = detail_of(env, created.batch.id)["progress"]
    assert progress["needsReconciliation"] == 1 and progress["state"] == "RUNNING"


def test_retry_replay_returns_original_body_and_writes_nothing(env: BatchEnv) -> None:
    """T-RETRY-02：同 Idempotency-Key 重放 → 回放原响应，不新增 run / job。"""
    created = do_create(env, quantity=2)
    _set_all(env, created.batch.id, ["FAILED", "FAILED"])
    first = _retry(env, created.batch.id, key="same")
    snapshot = counts(env)
    again = _retry(env, created.batch.id, key="same")
    assert again.replayed is True and again.body == first.body
    assert counts(env) == snapshot


def test_retry_nothing_to_retry_is_409_and_leaves_no_idempotency_record(env: BatchEnv) -> None:
    """T-RETRY-05。"""
    created = do_create(env, quantity=2)
    _set_all(env, created.batch.id, ["SUCCEEDED", "WAITING_RECONCILIATION"])
    with pytest.raises(ApiError) as exc:
        _retry(env, created.batch.id, key="nothing")
    assert_api_error(exc, ErrorCode.INVALID_TRANSITION, "nothing_to_retry")
    assert (
        scalar(
            env,
            "SELECT count(*) FROM idempotency_records WHERE scope=:s",
            {"s": f"batch-retry:{created.batch.id}"},
        )
        == 0
    )


def test_retry_on_cancelled_batch_is_409(env: BatchEnv) -> None:
    created = do_create(env, quantity=1)
    _set_all(env, created.batch.id, ["FAILED"])
    _cancel(env, created.batch.id)
    with pytest.raises(ApiError) as exc:
        _retry(env, created.batch.id, key="after-cancel")
    assert_api_error(exc, ErrorCode.INVALID_TRANSITION, "batch_cancelled")


def test_retry_same_key_other_batch_is_independent_and_requires_key(env: BatchEnv) -> None:
    created = do_create(env, quantity=1)
    _set_all(env, created.batch.id, ["FAILED"])
    with pytest.raises(ApiError) as exc:
        _retry(env, created.batch.id, key=None)
    assert_api_error(exc, ErrorCode.VALIDATION_FAILED, "idempotency_key_required")


def test_retry_second_round_failure_can_be_retried_again(env: BatchEnv) -> None:
    """重做的轮次持续递增（r2 → r3），旧轮不被改。"""
    created = do_create(env, quantity=1)
    _set_all(env, created.batch.id, ["FAILED"])
    _retry(env, created.batch.id, key="r2")
    round2_job = job_ids_of(env, created.batch.id)[-1]
    set_job_state(env, round2_job, "FAILED")
    third = _retry(env, created.batch.id, key="r3")
    assert third.body["retried"][0]["roundNo"] == 3
    assert scalar(
        env,
        "SELECT max(r.round_no) FROM batch_item_runs r JOIN batch_items bi ON bi.id=r.batch_item_id"
        " WHERE bi.batch_id=:b",
        {"b": created.batch.id},
    ) == 3


def test_concurrent_retries_one_wins_and_round_numbers_are_unique(env: BatchEnv) -> None:
    """T-RETRY-03：N 线程并发 retries（不同键、同期望锁版本）→ 恰 1 个成功，其余 412；无重复 round_no。"""
    created = do_create(env, quantity=3)
    _set_all(env, created.batch.id, ["FAILED", "FAILED", "FAILED"])
    expected = created.batch.lock_version
    threads_n = 4
    barrier = threading.Barrier(threads_n)
    outcomes: list[Any] = []

    def worker(index: int) -> None:
        barrier.wait(timeout=20)
        try:
            outcomes.append(
                _retry(env, created.batch.id, key=f"conc-{index}", expected_lock_version=expected)
            )
        except ApiError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert len(outcomes) == threads_n
    successes = [o for o in outcomes if isinstance(o, b_service.RetryResult)]
    failures = [o for o in outcomes if isinstance(o, ApiError)]
    assert len(successes) == 1, outcomes
    assert all(f.code == ErrorCode.VERSION_CONFLICT for f in failures)
    dup = sql(
        env,
        "SELECT batch_item_id, round_no, count(*) FROM batch_item_runs GROUP BY 1,2 HAVING count(*) > 1",
    )
    assert dup == []


def test_retry_and_cancel_are_mutually_exclusive(env: BatchEnv) -> None:
    """T-RETRY-04：用同一个期望锁版本，先取消后重做 → 重做 412 / 409；先重做后取消 → 取消 412。"""
    created = do_create(env, quantity=1)
    _set_all(env, created.batch.id, ["FAILED"])
    expected = created.batch.lock_version
    _retry(env, created.batch.id, key="first", expected_lock_version=expected)
    with session_scope(env.settings) as session:
        repo = BatchRepository(session)
        batch = b_service.require_batch(repo, created.batch.id)
        with pytest.raises(ApiError) as exc:
            b_service.cancel_batch(
                repo, batch=batch, expected_lock_version=expected, actor_id=env.owner_id, request_id="r"
            )
    assert exc.value.code == ErrorCode.VERSION_CONFLICT


# --- 读 ----------------------------------------------------------------------


def test_reads_are_pure_no_rows_written(env: BatchEnv) -> None:
    """T-READ-01：详情 / 列表 / 成员 三个读函数前后各数一遍相关表（含 runtime_settings）：零差异。"""
    created = do_create(env, quantity=3)
    from rednote.platform.pagination import PageParams

    before = counts(env)
    detail_of(env, created.batch.id)
    with session_scope(env.settings) as session:
        repo = BatchRepository(session)
        b_service.list_batches(
            repo, project_id=env.project_id, batch_kind=None, state=None,
            page_params=PageParams(limit=20), can_view_cost=True,
        )
        b_service.list_items(repo, batch_id=created.batch.id, page_params=PageParams(limit=20))
    assert counts(env) == before


def test_list_batches_pagination_filter_and_isolation(env: BatchEnv) -> None:
    """T-READ-02：游标分页稳定、过滤、按项目隔离。"""
    from rednote.platform.pagination import PageParams

    ids = [do_create(env, quantity=1).batch.id for _ in range(3)]
    bulk = do_create(env, quantity=1, kind="BULK").batch.id
    other = make_env()
    other_batch = do_create(other, quantity=1).batch.id

    def page(**kwargs: Any) -> b_service.Page:
        with session_scope(env.settings) as session:
            return b_service.list_batches(
                BatchRepository(session), project_id=env.project_id,
                batch_kind=kwargs.get("kind"), state=kwargs.get("state"),
                page_params=PageParams(limit=kwargs.get("limit", 20), cursor=kwargs.get("cursor")),
                can_view_cost=True,
            )

    everything = page()
    assert {item["id"] for item in everything.items} == {str(x) for x in (*ids, bulk)}
    assert str(other_batch) not in {item["id"] for item in everything.items}
    assert [item["batchKind"] for item in page(kind="BULK").items] == ["BULK"]
    assert page(state="CANCEL_REQUESTED").items == ()

    first = page(limit=2)
    assert first.page.has_more is True and first.page.next_cursor
    second = page(limit=2, cursor=first.page.next_cursor)
    seen = [item["id"] for item in (*first.items, *second.items)]
    assert len(seen) == len(set(seen)) == 4
    # 游标绑定过滤条件：换个过滤用同一个游标 → 400。
    with pytest.raises(ApiError) as exc:
        page(kind="BULK", limit=2, cursor=first.page.next_cursor)
    assert exc.value.code == ErrorCode.MALFORMED_REQUEST


def test_items_page_is_ordered_and_null_when_no_run(env: BatchEnv) -> None:
    from rednote.platform.pagination import PageParams

    created = do_create(env, quantity=5)
    with session_scope(env.settings) as session:
        first = b_service.list_items(
            BatchRepository(session), batch_id=created.batch.id, page_params=PageParams(limit=2)
        )
        second = b_service.list_items(
            BatchRepository(session), batch_id=created.batch.id,
            page_params=PageParams(limit=10, cursor=first.page.next_cursor),
        )
    assert [i["ordinal"] for i in first.items] == [1, 2] and first.page.has_more
    assert [i["ordinal"] for i in second.items] == [3, 4, 5] and not second.page.has_more
    assert all(i["latestRound"] == 1 and i["jobState"] == "QUEUED" for i in first.items)
    assert all(i["sceneKey"] is None and i["formKey"] is None for i in first.items)
    sql(env, "TRUNCATE batch_item_runs")
    with session_scope(env.settings) as session:
        page = b_service.list_items(
            BatchRepository(session), batch_id=created.batch.id, page_params=PageParams(limit=1)
        )
    assert page.items[0]["latestRound"] is None and page.items[0]["jobState"] is None


def test_batch_not_found_is_404(env: BatchEnv) -> None:
    with session_scope(env.settings) as session:
        with pytest.raises(ApiError) as exc:
            b_service.require_batch(BatchRepository(session), uuid.uuid4())
    assert_api_error(exc, ErrorCode.RESOURCE_NOT_FOUND, "BATCH_NOT_FOUND")


# --- 预览 ----------------------------------------------------------------------


def _preview(env: BatchEnv, *, gate: Any = GATE_OK, can_view_cost: bool = True, **kwargs: Any) -> dict[str, Any]:
    with session_scope(env.settings) as session:
        return b_service.preview_batch(
            BatchRepository(session),
            project_id=env.project_id,
            command=command_of(env, **kwargs),
            gate_checker=gate,
            actor_id=env.owner_id,
            request_id="req-preview",
            can_view_cost=can_view_cost,
        )


def test_preview_creates_no_batch_but_writes_estimates_and_reports_gate(env: BatchEnv) -> None:
    """T-COST-05：预览不落批次、不预占，但会写 cost_estimates（文档化的副作用）。"""
    before = counts(env)
    preview = _preview(env, quantity=5)
    after = counts(env)
    assert after["batches"] == before["batches"] == 0
    assert after["jobs"] == before["jobs"] and after["reservations"] == before["reservations"]
    assert after["estimates"] == before["estimates"] + 2
    assert preview["limit"]["applied"] == 30
    assert preview["estimate"]["amount"] == "125.00000000" and preview["estimate"]["isEstimate"] is True
    assert preview["budget"]["configured"] is True and preview["budget"]["available"] == "100000.00000000"
    assert preview["gate"]["passed"] is True
    assert sum(line["allocated"] for line in preview["allocation"]["scenes"]) == 5


def test_preview_gate_states_not_passed_and_unwired(env: BatchEnv) -> None:
    no = _preview(env, gate=lambda _p: GateVerdict(passed=False, reason="X"))
    assert no["gate"]["passed"] is False and no["gate"]["reason"] == "X"
    unwired = _preview(env, gate=default_gate_checker)
    assert unwired["gate"]["passed"] is None  # 缺失不填 False
    assert unwired["gate"]["reason"] == "trial_gate_not_wired"


def test_preview_hides_money_without_view_cost(env: BatchEnv) -> None:
    hidden = _preview(env, can_view_cost=False)
    assert hidden["estimate"]["amount"] is None and hidden["estimate"]["lines"] == []
    assert hidden["budget"]["available"] is None and hidden["budget"]["configured"] is True


def test_preview_budget_unconfigured_reports_configured_false() -> None:
    env = make_env(budget=None)
    preview = _preview(env)
    assert preview["budget"] == {"configured": False, "currency": None, "available": None}


# ===========================================================================
# 6. 场景 / 形式接缝（接缝冻结 §5.15）
# ===========================================================================


def test_batch_items_mirror_content_items_scene_and_form_keys(
    env: BatchEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`batch_items.scene_key/form_key` 恒取自 `content_items` 行（同一次读，不另算）。

    本包当前不传（OP-227：联合展开规则未定义，不编造），所以默认全 NULL；
    这里用桥接层注入一对值，证明“只要 content 接缝带入了，批次成员就与内容行一致”，
    且 `ck_batch_items_assignment_paired` 在两者同有时成立。
    """
    real = content_bridge_build()

    def with_keys(**kwargs: Any) -> Any:
        return real(**{**kwargs, "scene_key": "早八通勤", "form_key": "图文"})

    monkeypatch.setattr(b_service.content_bridge, "build_generation_request", with_keys)
    created = do_create(env, quantity=2)
    rows = sql(
        env,
        "SELECT bi.scene_key, bi.form_key, ci.scene_key, ci.form_key FROM batch_items bi"
        " JOIN content_items ci ON ci.id = bi.content_item_id WHERE bi.batch_id=:b",
        {"b": created.batch.id},
    )
    assert rows == [("早八通勤", "图文", "早八通勤", "图文")] * 2


def content_bridge_build() -> Any:
    from rednote.modules.batches import content_bridge

    return content_bridge.build_generation_request
