"""WP25（供应商计价 + 费用账本 + 预算预占）的四层测试。

对应 `superpower-workflow` 阶段 6。与 `test_planning_wp24.py` 同一范式
（1 纯函数 → 2 服务层真库 → 3 原生 SQL 绕过 ORM → 4 HTTP `TestClient`）。

## 四层各测什么（以及为什么不能合并）

| 层 | 测什么 | 手段 | 为什么单独一层 |
|---|---|---|---|
| 1 | 纯函数：金额/用量解析边界、单价×用量、DDL 文本自洽、`LedgerEntryInput` 构造校验 | 直接调用，**不碰库** | 快；且"金额解析的容错边界"必须能被**穷举**证明 —— 一旦解析路径能读库，穷举就要一个数据库 |
| 2 | 服务层编排：预算四条条件 UPDATE、预占/释放/结算状态机、幂等键回查、数量越界翻 422 | 真库 + `service_*` | "并发预占只放行 N 笔"与"账面/账本成对"是**事务语义**，只有在真事务里才成立 |
| 3 | **数据库约束自己会不会拦** | 原生 SQL（绕过 ORM 与 service） | 检查"绕过本层的写入"；若测试也只用 ORM，测试与实现共享同一个盲区 |
| 4 | HTTP 接线：14 条路径、鉴权（+`MANAGE_BUDGET` 的组织级动作）、201/200/403/404 | `TestClient` | 端点组装是独立的一层，服务层全绿也可能挂错前缀或漏了 `access.require` |

## 本包的四条验收线，各自打在哪一层

1. **W10 预算并发预占**（§数据结构 02 §11「预算并发预占测试」）—— 第 2 层
   （`ThreadPoolExecutor` 并发 N 笔，断言"成功数 × 单笔 ≤ 限额"）+ 第 3 层
   （`within_limit` 恒真式的**反向**用法：断言并发后等式仍成立，**不是**
   断言它拦得住 —— 它拦不住，因为它恒真）。
2. **不可变账本**（R5：冲正用新分录，不改旧行）—— 第 3 层
   （`usage_ledger` / `pricing_versions` 的 UPDATE/DELETE 各断言 `23001`）
   + 第 2 层（`append_ledger_entry` 的 `REVERSAL` 路径 + 幂等键回查）。
3. **账面与账本成对**（R10）—— 第 2 层（`entries` 为空自动补 `BILL`、
   `Σ entries != actual` → 422）+ 第 3 层（`amount_sign_matches_kind` 等
   九条 CHECK）+ `reconciliation`（分叉判据自己）。
4. **`MANAGE_BUDGET` 的组织级边界** —— 第 4 层（`PUT .../budget`：
   ADMIN 200 / OPERATOR **403**）。

## ⚠️ 本包最易写错处：`within_limit` 是恒真式，不能当判据

`ck_project_budgets_within_limit`（`committed + reserved <= limit`）看起来像
"预算防线"。**它不是** —— 在 CHECK 成立的任何时刻它都成立：

* 想插一行 `committed=100, reserved=0, limit=50` ⇒ CHECK 拒绝（`23514`）；
* 但**并发**下两条 UPDATE 各自都满足 WHERE，`committed+reserved` 恰好
  超限的中间态**根本不存在**（每条 UPDATE 是原子的，且 WHERE 里已含该条件）。
  换言之：**唯一能造出"超限"的方式是绕过条件 UPDATE 直接写**，
  而那时 CHECK 会拒 —— 于是这个 CHECK 只在"程序有缺陷"时才会响。

因此本文件对它的判据是：**断言并发后等式仍成立**（"账面没超"），
以及第 3 层单独一条"原生 SQL 直写超限值 ⇒ 撞 `23514`"证明它确实在位。
把"防线在位"与"防线被触发意味着什么"分开写 —— 否则会得到一条
**恒过**的判据（MATERIAL.md 第 2 条纪律：判据不能建立在 DB 已保证的事上）。

## ⚠️ `usage_ledger` / `pricing_versions` 没有 `note` 列（本机实测）

`usage_ledger` 的列是 `id / project_id / entry_kind / currency / amount /
quantity / unit / reverses_ledger_id / source_ref / idempotency_key /
description / occurred_at / created_at` —— **没有 `note`**。
`pricing_versions` 有 `note`。写"UPDATE 应当被拒"的探针时若用
`set note = note`，在 `usage_ledger` 上会先撞 `42703`（列不存在），
于是**没走到触发器**却返回了一个"非 23001 的 sqlstate" ——
判据会以"防线失效"的形式报红，而真实原因是列名写错。
本文件因此对每张不可变表都用**真实存在的列**构造 UPDATE。

## 为什么 `pricing_versions` / `usage_ledger` 的残留只能用 TRUNCATE 清

两张都是**不可变表**（`trg_*_immutable` 拦 UPDATE/DELETE，SQLSTATE `23001`）。
因此第 2/3 层的写入会**累积**在测试库里 ——
需要"表里只有本次的那几行"的判据一律先 `TRUNCATE`（夹具已做），
而不是写 `DELETE`。
（行级 BEFORE 触发器不拦 `TRUNCATE` —— 同 `test_planning_wp24.py` 的说明。）

## 夹具为什么断言"**跨包**入向 FK 不变"而不是"入向 FK 为 0"

本包 5 张表**有 3 条入向 FK，但全部是包内 FK**（本机实测）：

    budget_reservations.fk_..._estimate_id_cost_estimates   -> cost_estimates
    cost_estimates.fk_..._pricing_version_id_pricing_versions -> pricing_versions
    usage_ledger.fk_..._reverses_ledger_id_usage_ledger      -> usage_ledger（自引用）

WP24 的 `test_planning_wp24.py` 断言 `inbound == 0` 是对的（它那两张表确实
没有入向 FK）。**照抄会失败**。本包的正确口径是
`cross_package_foreign_keys(engine, DROP_ORDER)` 前后相等 ——
它已排除"引用方也在 `DROP_ORDER` 里"的包内 FK。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from conftest import cross_package_foreign_keys, drop_own_tables

# FK 目标表：**必须在 `create_all` 之前 import**，否则 SQLAlchemy 解析
# `ForeignKey("model_profiles.id")` 时找不到表 ⇒ `NoReferencedTableError`。
# 而且 `test_schema_parity.py` 有一条判据断言"模型声明的 FK 库里必须存在"，
# 漏 import 会让**那个文件**失败（不是本文件）—— 排障方向会被误导。
import rednote.modules.gateway.models  # noqa: F401
import rednote.modules.identity.models  # noqa: F401
import rednote.modules.jobs.models  # noqa: F401
import rednote.modules.projects.models  # noqa: F401
from rednote.modules.billing import ddl as b_ddl
from rednote.modules.billing import money
from rednote.modules.billing import pricing as b_pricing
from rednote.modules.billing import reconciliation as b_recon
from rednote.modules.billing import service as b_service
from rednote.modules.billing.models import (
    BudgetReservation,
    CostEstimate,
    PricingVersion,
    ProjectBudget,
    UsageLedger,
)
from rednote.modules.billing.repository import BillingRepository
from rednote.modules.billing.schema import (
    RECONCILIATION_CONFIRMING_KINDS,
    RECONCILIATION_TABLES,
    ledger_summary_body,
)
from rednote.modules.billing.types import (
    CURRENCY_CNY,
    CURRENCY_USD,
    ESTIMATE_STATE_DRAFT,
    ESTIMATE_STATES,
    HOLDING_RESERVATION_STATES,
    LEDGER_KIND_ADJUSTMENT,
    LEDGER_KIND_BILL,
    LEDGER_KIND_REVERSAL,
    LEDGER_KIND_USAGE,
    LEDGER_KINDS,
    MAX_LEDGER_SOURCE_REF_CHARS,
    MAX_RESERVATION_REF_CHARS,
    NEGATIVE_LEDGER_KINDS,
    POSITIVE_LEDGER_KINDS,
    PRICING_STATE_DRAFT,
    PRICING_STATE_PUBLISHED,
    REASON_AMOUNT_NOT_POSITIVE,
    REASON_AMOUNT_PRECISION,
    REASON_BUDGET_EXCEEDED,
    REASON_CURRENCY_MISMATCH,
    REASON_LEDGER_TOTAL_MISMATCH,
    REASON_PROJECT_NOT_FOUND,
    REASON_QUANTITY_ABOVE_MAX,
    REASON_RESERVATION_NOT_FOUND,
    REASON_RESERVATION_NOT_HOLDING,
    RESERVATION_STATE_HELD,
    RESERVATION_STATE_RELEASED,
    RESERVATION_STATE_SETTLED,
    RESERVATION_STATES,
    USABLE_PRICING_STATES,
    LedgerEntryInput,
    UnitPrice,
)
from rednote.modules.gateway.repository import GatewayRepository
from rednote.modules.gateway.types import (
    CAPABILITY_TEXT_STRUCTURED,
    NETWORK_EXTERNAL,
)
from rednote.modules.identity.models import ROLE_ADMIN, ROLE_OPERATOR
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.planning.repository import PlanningRepository
from rednote.modules.planning import settings_service as planning_settings_service
from rednote.modules.planning.settings_service import change_project_setting
from rednote.modules.planning.types import SETTING_KEY_BATCH_LIMITS, EffectiveSetting
from rednote.modules.projects.models import Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.platform.configuration import Settings
from rednote.platform.database import get_engine, reset_engines, session_scope
from rednote.platform.errors import ApiError, ErrorCode
from rednote.platform.immutability import IMMUTABLE_RECORD_SQLSTATE

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
CSRF_SECRET_PLAINTEXT = "test-csrf-secret-plaintext"
TEST_ORIGIN = "http://testserver"

#: 仓库内 `services/backend/` 的绝对路径 —— 给"读源码自查"型判据用。
#: 从本文件（`services/backend/tests/`）上溯两级，**不写死绝对路径**
#: （写死会让整仓挪位置时判据静默指向不存在的目录）。
SERVICES_BACKEND = Path(__file__).resolve().parents[1]

#: 本包自己的 5 张表。`projects` / `users` / `providers` / `model_profiles`
#: **不在其中** —— 它们是别的包的表。
OWN_TABLES: tuple[Any, ...] = (
    PricingVersion.__table__,
    CostEstimate.__table__,
    ProjectBudget.__table__,
    BudgetReservation.__table__,
    UsageLedger.__table__,
)

#: DROP 顺序：**引用方在前**（BEFORE 触发器之外的第二重保险）。
#: `budget_reservations → cost_estimates → pricing_versions` 是一条链，
#: `usage_ledger` 自引用。`project_budgets` 不被本包其它表引用。
DROP_ORDER: tuple[str, ...] = (
    "budget_reservations",
    "usage_ledger",
    "cost_estimates",
    "project_budgets",
    "pricing_versions",
)

#: 本包对"跨包"的定义域 —— 给 `cross_package_foreign_keys` 用。
_TABLE_NAMES: tuple[str, ...] = DROP_ORDER


def _test_database_url() -> str:
    """DSN 从环境变量取（分支用 `rednote_wp25` 私有库），缺省本机 postgres。

    形态必须与 `tools/quality/check_test_db_isolation.py` 的两跳判定一致：
    **先命名常量 `DEFAULT_TEST_DATABASE_URL`，再 `os.environ.get(...)` 取它
    并 `.strip()`**。写成别的等价形态（例如内联字面量、或 `or` 兜底）
    会被那条判据判为"硬编码测试 DSN"。
    """
    return os.environ.get(
        "REDNOTE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL
    ).strip()


def _make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": _test_database_url(),
        "environment": "test",
        "session_cookie_secure": False,
        "oidc_issuer": TEST_ISSUER,
        "oidc_token_encryption_key": TEST_ENCRYPTION_KEY,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ===========================================================================
# 建表夹具（module 级）
# ===========================================================================


@pytest.fixture(scope="module", autouse=True)
def _ensure_own_tables() -> Iterator[None]:
    """只建本包的 5 张表 + 两个不可变触发器 + 一条部分唯一索引。

    ## 为什么 `create_all` 之前必须 import FK 目标表

    `Base.metadata.create_all(tables=[...])` **只注册传入的那几张表**。
    本包 5 张表引用了 `projects` / `users` / `providers` / `model_profiles`
    —— 那些表不在 `tables=` 里，SQLAlchemy 解析 `ForeignKey` 时会抛
    `NoReferencedTableError`。因此文件顶部 import 了
    `gateway/identity/jobs/projects` 四个 `models` 模块
    （副作用是它们把各自表注册进同一个 `Base.metadata`）。

    ## ★ 入向 FK 断言的口径（**不要照抄 WP24**）

    WP24 断言 `inbound == 0`。本包**有 3 条入向 FK，全为包内**：

        budget_reservations -> cost_estimates
        cost_estimates      -> pricing_versions
        usage_ledger        -> usage_ledger（自引用）

    所以正确口径是"**跨包**入向 FK 集合不变"——
    `cross_package_foreign_keys` 已排除引用方也在 `DROP_ORDER` 里的包内 FK。
    照抄 `inbound == 0` 会**必然失败**，且失败信息看起来像"本包表被别的模块
    引用了"，而真实原因是判据抄错了。

    ## 为什么 TRUNCATE 不加 CASCADE

    加了 CASCADE 会在"将来真有跨包入向 FK"时**静默清空引用方表**
    （ISS-106 实测过的破坏形态）。不写 CASCADE 让那种情况变成
    **当场报错**。`drop_own_tables` 也已按"没有跨包入向 FK"筛过一遍，
    两条判据互为保险。
    """
    reset_engines()
    engine = get_engine(_make_settings())

    fks_before = cross_package_foreign_keys(engine, _TABLE_NAMES)
    dropped, kept = drop_own_tables(engine, DROP_ORDER)

    PricingVersion.metadata.create_all(engine, tables=list(OWN_TABLES))

    fks_after = cross_package_foreign_keys(engine, _TABLE_NAMES)
    assert fks_after == fks_before, (
        f"建表夹具改变了跨包外键集合：before={sorted(fks_before)} "
        f"after={sorted(fks_after)}（dropped={dropped} kept={kept}）"
    )

    with engine.begin() as conn:
        # 顺序在触发器之前：此刻还没挂不可变触发器；即便挂着也没关系
        # —— TRUNCATE 不触发行级 BEFORE 触发器。
        conn.execute(
            text(
                "TRUNCATE budget_reservations, usage_ledger, cost_estimates,"
                " project_budgets, pricing_versions"
            )
        )

        for (
            _table,
            func_ddl,
            trigger_ddl,
            drop_trigger,
            _drop_func,
        ) in b_ddl.all_trigger_ddl():
            conn.execute(text(drop_trigger))  # 幂等关键：CREATE TRIGGER 不幂等
            conn.execute(text(func_ddl))
            conn.execute(text(trigger_ddl))
    yield


@pytest.fixture(autouse=True)
def _clean_own_rows() -> Iterator[None]:
    """**每个用例前**清空本包 5 张表（autouse ⇒ 先于 `env` 执行）。

    ## 为什么 module 级那一次 TRUNCATE 不够

    本文件里有若干判据**看全表**（例：`list_pricing_versions()` 传
    `provider_id=None` 看全部；`reconcile_all_projects` 看全部有账面的项目）。
    不清的话它们会看到**兄弟用例留下的行**：

    | 判据 | 被污染后的症状 |
    |---|---|
    | `list_pricing_versions()` 计数 | 报"多出 N 条" —— 全是上一轮用例的价目 |
    | 对账全项目 | 报出与本次无关的 drift，且方向随机 |

    这两类症状与"实现真的错了"**在输出上完全同形**。
    正确处置是**让起点干净**，而不是收窄断言范围（那会丢掉判据的价值）。

    ## 为什么用 TRUNCATE 而不是 DELETE

    `pricing_versions` / `usage_ledger` 是不可变表（行级触发器拦
    UPDATE/DELETE，返回 `23001`）。只有 TRUNCATE 能绕过行级触发器。

    清空顺序按引用关系倒序，避免撞 FK。
    """
    reset_engines()
    engine = get_engine(_make_settings())
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE budget_reservations, usage_ledger, cost_estimates,"
                " project_budgets, pricing_versions"
            )
        )
    yield


# ===========================================================================
# 数据夹具（自包含，不依赖 conftest 的隐式状态）
# ===========================================================================


@dataclass
class BillingEnv:
    settings: Settings
    project_id: uuid.UUID
    owner_id: uuid.UUID
    admin_id: uuid.UUID
    #: 供应商 + 模型档案（计价版本要挂在供应商上）
    provider_id: uuid.UUID
    model_profile_id: uuid.UUID
    #: 另一套项目，用于"跨项目隔离"反例
    other_project_id: uuid.UUID
    #: **不是任何项目成员**的运营 —— 用于验证 404（防项目存在性枚举）
    outsider_id: uuid.UUID


def _create_user(
    settings: Settings, *, role: str = ROLE_OPERATOR, name: str = "Billing 测试用户"
) -> uuid.UUID:
    with session_scope(settings) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name=name,
            role=role,
        )
        return user.id


def _seed_project(settings: Settings, *, owner_id: uuid.UUID) -> uuid.UUID:
    """造 Brand → Product → Project（含 owner 成员关系），返回 `project_id`。"""
    with session_scope(settings) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name=f"产品-{uuid.uuid4().hex[:6]}")
        session.add(product)
        session.flush()
        project = Project(
            project_code=f"BIL-{uuid.uuid4().hex[:8].upper()}",
            name="计费测试项目",
            brand_id=brand.id,
            product_id=product.id,
            owner_id=owner_id,
            target_delivery_count=10,
            due_date=date(2030, 12, 31),
        )
        session.add(project)
        session.flush()
        ProjectRepository(session).add_owner_and_collaborators(
            project.id, owner_id=owner_id, collaborator_ids=()
        )
        return project.id


def _seed_provider(
    settings: Settings,
) -> tuple[uuid.UUID, uuid.UUID]:
    """造 Provider → ModelProfile，返回 `(provider_id, model_profile_id)`。"""
    with session_scope(settings) as session:
        repo = GatewayRepository(session)
        provider = repo.insert_provider(
            name=f"供应商-{uuid.uuid4().hex[:8]}",
            capability_types=[CAPABILITY_TEXT_STRUCTURED],
            network_mode=NETWORK_EXTERNAL,
            endpoint="https://api.test.invalid/v1",
        )
        session.flush()
        profile = repo.insert_model_profile(
            provider_id=provider.id,
            model_name=f"model-{uuid.uuid4().hex[:6]}",
            capabilities=[CAPABILITY_TEXT_STRUCTURED],
        )
        session.flush()
        return provider.id, profile.id


@pytest.fixture
def env() -> BillingEnv:
    reset_engines()
    settings = _make_settings()
    owner = _create_user(settings)
    admin = _create_user(settings, role=ROLE_ADMIN, name="Billing 测试管理员")
    outsider = _create_user(settings, name="Billing 非成员")
    project_id = _seed_project(settings, owner_id=owner)
    other_project_id = _seed_project(settings, owner_id=owner)
    provider_id, profile_id = _seed_provider(settings)
    return BillingEnv(
        settings=settings,
        project_id=project_id,
        owner_id=owner,
        admin_id=admin,
        provider_id=provider_id,
        model_profile_id=profile_id,
        other_project_id=other_project_id,
        outsider_id=outsider,
    )


# --- 2.0 基础设施 ----------------------------------------------------------

#: 计价版本用的价目（**值必须是字符串** —— jsonb number 会丢位数）。
_PRICE_CALL = "12.50000000"
_PRICE_TOKEN = "0.00010000"
_PRICE_IMAGE = "0.50000000"
_PRICE_SECOND = "0.01000000"


def _raw(env: BillingEnv, sql: str, params: dict[str, Any] | None = None) -> Any:
    """在**独立事务**里执行一条原生 SQL 并提交。返回结果行（若有）。"""
    with session_scope(env.settings) as session:
        result = session.execute(text(sql), params or {})
        return result.fetchall() if result.returns_rows else None


def _scalar(env: BillingEnv, sql: str, params: dict[str, Any] | None = None) -> Any:
    with session_scope(env.settings) as session:
        return session.execute(text(sql), params or {}).scalar_one_or_none()


def _raw_expect_error(
    env: BillingEnv, sql: str, params: dict[str, Any] | None = None
) -> DBAPIError:
    """执行一条**应当失败**的原生 SQL，返回被捕获的异常供断言 sqlstate。"""
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            session.execute(text(sql), params or {})
    return caught.value


def _sqlstate(exc: DBAPIError) -> str:
    return getattr(getattr(exc, "orig", None), "sqlstate", "") or ""


def _constraint_name(exc: DBAPIError) -> str:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", "") or ""


@contextmanager
def _repo(env: BillingEnv) -> Iterator[BillingRepository]:
    """一次事务里的 `BillingRepository`（退出即提交）。"""
    with session_scope(env.settings) as session:
        yield BillingRepository(session)


def _grant_batch_limits(env: BillingEnv, *, project_id: uuid.UUID | None = None) -> None:
    """写**项目级 MANUAL** 的 `batch.limits` —— `estimate_cost` 的唯一数量口径来源。

    ## 为什么写项目级而不是全局

    `change_global_setting` 会**枚举全部项目**做级联刷新。本机私有库里
    `projects` 有几万行（M1/M2 测试的累积残留），一次全局变更会插几万行
    `runtime_settings` —— 慢，且给别人的判据留下"某项目有 MANUAL 副本"
    的污染（WP24 的 J3/J4 正是这么被污染的）。

    写项目级只落一行，且 `estimate_cost` 走
    `get_effective_limits(repo, key, project_id)`，项目级 `MANUAL` 优先，
    完全满足它的读取需求。

    `TRIAL=10 / SCALE=30 / BULK=50` 与 `DEFAULT_BATCH_MAX` 同值，
    便于用例直接断言"第 11 篇越界"。
    """
    with session_scope(env.settings) as session:
        change_project_setting(
            PlanningRepository(session),
            key=SETTING_KEY_BATCH_LIMITS,
            project_id=project_id or env.project_id,
            value={"TRIAL": 10, "SCALE": 30, "BULK": 50},
            reason="WP25 测试：项目级批次上限",
            actor_id=env.owner_id,
        )


def _seed_pricing_version(
    env: BillingEnv,
    *,
    state: str = PRICING_STATE_PUBLISHED,
    currency: str = CURRENCY_CNY,
    provider_id: uuid.UUID | None = None,
    model_profile_id: uuid.UUID | None = None,
    unit_prices: dict[str, str] | None = None,
    effective_from: datetime | None = None,
    label: str = "测试价目",
) -> uuid.UUID:
    """插一个计价版本，返回其 id。"""
    prices = unit_prices or {
        "CALL": _PRICE_CALL,
        "TOKEN": _PRICE_TOKEN,
        "IMAGE": _PRICE_IMAGE,
        "SECOND": _PRICE_SECOND,
    }
    with session_scope(env.settings) as session:
        row = BillingRepository(session).insert_pricing_version(
            provider_id=provider_id or env.provider_id,
            model_profile_id=model_profile_id,
            currency=currency,
            label=label,
            unit_prices=prices,
            effective_from=effective_from or datetime.now(timezone.utc) - timedelta(days=1),
            state=state,
        )
        session.flush()
        return row.id


def _seed_pending_attempts(
    env: BillingEnv,
    *,
    count: int,
    project_id: uuid.UUID | None = None,
    with_ledger_entry: bool = False,
) -> list[uuid.UUID]:
    """在**本项目**下造 `count` 条 `usage_pending=true` 的尝试，返回 `attempt_id` 列表。

    ## 为什么要限定项目（`billing` 不建 `jobs` 删表，但必须限定查询）

    本机测试库的 `job_attempts` 有**历史残留**（实测 575 条 pending，
    M1/M2 累积）。`reconcile_pending` 支持 `project_id` 过滤，因此
    "本项目有 2 条"这类判据必须传 `project_id`，否则读到的是
    "这台机器上所有未结用量"，判据会变成对历史数据的断言。

    ## 为什么 `with_ledger_entry` 要显式给

    对账的"已结"口径是"账本里有一条 `BILL` 分录，且其 `source_ref`
    等于 `attempt:<attempt_id>`"。默认 `False`（未结）——
    这条辅助函数只负责造"上游说未结"，**不替调用方决定账本侧**。
    """
    ids: list[uuid.UUID] = []
    with session_scope(env.settings) as session:
        from rednote.modules.jobs.models import Job, JobAttempt

        for index in range(count):
            job = Job(
                project_id=project_id or env.project_id,
                kind="CONTENT_GENERATE",
                queue="default",
                state="SUCCEEDED",
                idempotency_scope=f"wp25-{uuid.uuid4().hex[:8]}",
                idempotency_key=f"key-{uuid.uuid4().hex[:12]}",
                input_hash="0" * 64,
            )
            session.add(job)
            session.flush()
            attempt = JobAttempt(
                job_id=job.id,
                attempt_no=index + 1,
                state="SUCCEEDED",
                worker_id="wp25-test-worker",
                fence_token=index + 1,
                usage_pending=True,
            )
            session.add(attempt)
            session.flush()
            ids.append(attempt.id)
    if with_ledger_entry:
        for attempt_id in ids:
            _raw(
                env,
                "insert into usage_ledger (project_id, entry_kind, currency, amount,"
                " source_ref, occurred_at) values (:p, 'BILL', 'CNY', 5, :ref, now())",
                {"p": str(project_id or env.project_id), "ref": f"attempt:{attempt_id}"},
            )
    return ids


def _budget_snapshot(env: BillingEnv, *, project_id: uuid.UUID | None = None) -> dict[str, Any]:
    """从库直接读账面四值（不经过 service，避免断言依赖被测对象）。

    ⚠️ 必须用 `_raw` 而不是 `_scalar`：后者基于 `scalar_one_or_none()`，
    **只取第一列**，于是 `row[0]` 会抛 `TypeError: Decimal is not subscriptable`
    （一个"看起来像实现坏了、其实是取数方式错"的症状）。
    """
    rows = _raw(
        env,
        "select limit_amount, committed_amount, reserved_amount, currency"
        " from project_budgets where project_id = :p",
        {"p": str(project_id or env.project_id)},
    )
    if not rows:
        return {"configured": False}
    row = rows[0]
    return {
        "configured": True,
        "limit": row[0],
        "committed": row[1],
        "reserved": row[2],
        "currency": row[3],
    }


# ===========================================================================
# 第 1 层：纯函数（不碰库）
# ===========================================================================


def test_money_quantize_is_idempotent_and_scales_to_eight() -> None:
    """`quantize` 收敛到 8 位小数且幂等（`AMOUNT_SCALE` 的唯一真值来源）。"""
    once = money.quantize(Decimal("12.345678901"))
    twice = money.quantize(once)
    assert once == Decimal("12.34567890")
    assert twice == once, "量化必须是幂等的 —— 否则每次读写都会漂一点"


def test_money_parse_wire_optional_boundary_table() -> None:
    """`parse_wire_optional` 的容错边界**逐行**钉死。

    这张表是本包"金额从 HTTP 进来"的唯一入口处的行为契约。
    逐行断言（而不是抽查两三个）是刻意的：漏掉一行就意味着
    某个畸形输入的行为**没有被任何人声明过**。
    """
    # 空形态 ⇒ None（不是 0 —— "没给"与"给了 0"是不同的业务事实）
    assert money.parse_wire_optional(None) is None
    assert money.parse_wire_optional("") is None
    assert money.parse_wire_optional("   ") is None

    # 正常形态
    assert money.parse_wire_optional("0") == Decimal("0E-8")
    assert money.parse_wire_optional("12.5") == Decimal("12.50000000")

    # **负数允许通过解析**（"不得为负"是业务层的判断，不是解析层的）
    assert money.parse_wire_optional("-3") == Decimal("-3.00000000")

    # 拒绝的形态：科学计数 / 千分位 / 非数字
    for bad in ("1e5", "1,000", "abc", "12.5.6"):
        with pytest.raises(money.MoneyError):
            money.parse_wire_optional(bad)

    # **float 被显式拒绝**（`_reject_float`）—— 二进制浮点不是金额
    with pytest.raises(money.MoneyError):
        money.parse_wire_optional(1.5)  # type: ignore[arg-type]

    # 超长（`MAX_WIRE_CHARS = 64`）
    with pytest.raises(money.MoneyError):
        money.parse_wire_optional("1" * 70)


def test_money_parse_wire_optional_silently_quantizes_but_strict_refuses() -> None:
    """**有损量化**：宽口径静默截断，严口径当场报错。

    这是本包一处刻意的双口径：
      · `parse_wire_optional`（HTTP 入参）—— 用户写 9 位小数可接受，
        截断到 8 位；
      · `parse_wire_strict`（价目表）—— 价目是**配置**，
        有损意味着录错了，必须报错（否则每次计算都少一点钱，且不报错）。
    """
    assert money.parse_wire_optional("0.000000001") == Decimal("0E-8")
    with pytest.raises(money.MoneyPrecisionError):
        money.parse_wire_strict("0.000000001")


def test_money_parse_quantity_wire_rejects_float_with_attribute_error() -> None:
    """`parse_quantity_wire(float)` 抛的是 `AttributeError`（**不是 `MoneyError`**）。

    ## 为什么这条要单独钉住（本机首次实测就踩到）

    `parse_wire_optional` 走 `_reject_float`，对 float 抛 `MoneyError`；
    而 `parse_quantity_wire` 直接调 `str.strip()`，于是传 float 会抛出
    `AttributeError: 'float' object has no attribute 'strip'`。

    一种"更整齐"的做法是把这个 `AttributeError` 也翻译成 `MoneyError`。
    本文件**不断言它更整齐**，只断言**它现在的真实行为** ——
    因为上层（`api.py::_ledger_input`）捕获的是 `money.MoneyError`，
    若哪天这里改成抛 `MoneyError`，那条 except 才会开始生效；
    在它生效之前，一个 float 用量会变成 **500**。写成断言，
    这个差异就不会被静默地"顺手改掉"。
    """
    assert money.parse_quantity_wire(None) is None
    assert money.parse_quantity_wire("") is None
    assert money.parse_quantity_wire("12.5") == Decimal("12.5000000000")
    with pytest.raises(money.MoneyError):
        money.parse_quantity_wire("1e5")
    with pytest.raises(AttributeError):
        money.parse_quantity_wire(1.5)  # type: ignore[arg-type]


def test_money_mul_amount_refuses_overflow_of_numeric_20_8() -> None:
    """`mul_amount` 超 `NUMERIC(20,8)` 抛 `MoneyError`（不是静默变 `inf`）。"""
    huge = Decimal("999999999999.00000000")
    with pytest.raises(money.MoneyError):
        money.mul_amount(huge, huge)
    assert money.mul_amount(Decimal("2"), Decimal("3")) == Decimal("6.00000000")


def test_pricing_line_from_quantity_multiplies_unit_price() -> None:
    """`line_from_quantity` 按"单价 × 数量"出金额。"""
    line = b_pricing.line_from_quantity(
        unit="TOKEN", quantity=Decimal("1000"), unit_price=Decimal("0.00010000")
    )
    assert line.unit == "TOKEN"
    assert line.quantity == Decimal("1000.0000000000")
    assert line.amount == Decimal("0.10000000")


def test_pricing_estimate_for_metrics_maps_metrics_to_units() -> None:
    """`estimate_for_metrics` 把 `_METRIC_UNITS` 映射成计量单位并逐项计价。"""
    prices = {
        "TOKEN": Decimal("0.00010000"),
        "IMAGE": Decimal("0.50000000"),
        "SECOND": Decimal("0.01000000"),
        "CALL": Decimal("12.50000000"),
    }
    lines = b_pricing.estimate_for_metrics(
        prices=prices, metrics={"input_tokens": Decimal("1000"), "images": Decimal("2")}
    )
    units = {line.unit for line in lines}
    assert units == {"TOKEN", "IMAGE"}
    assert b_pricing.total_of(lines) == Decimal("0.10000000") + Decimal("1.00000000")


def test_pricing_total_of_refuses_empty_lines() -> None:
    """空行集合抛 `PricingError` —— "没有任何预估行"不是"0 元"。

    若返回 0，一个"忘了传 metrics、价目表又恰好缺 CALL"的调用方会得到
    "这次批次免费"的结论，而账面完全正常。
    """
    with pytest.raises(b_pricing.PricingError):
        b_pricing.total_of([])


def test_pricing_price_for_refuses_missing_unit() -> None:
    """`price_for` 缺单位 ⇒ 抛（**不返回 0**）。"""
    prices = b_pricing.build_price_map([UnitPrice(unit="TOKEN", unit_price=Decimal("1"))])
    assert prices == {"TOKEN": Decimal("1")}
    with pytest.raises(b_pricing.PricingError):
        b_pricing.price_for(prices, "CALL")


def test_pricing_build_price_map_refuses_duplicate_unit() -> None:
    """重复计量单位抛 `PricingError` —— "两个价格哪个算"不该由实现替用户猜。"""
    with pytest.raises(b_pricing.PricingError):
        b_pricing.build_price_map(
            [
                UnitPrice(unit="TOKEN", unit_price=Decimal("1")),
                UnitPrice(unit="TOKEN", unit_price=Decimal("2")),
            ]
        )


def test_pricing_unit_price_validates_at_construction() -> None:
    """`UnitPrice` 的两项校验在**构造时**就发生（不是等到用）。

    异常类型是 `MoneyError`（`money` 模块的），**不是** `PricingError` ——
    因为"单位名合法"与"单价非负"两条都由 `money` 侧的常量与判据定义。
    """
    with pytest.raises(money.MoneyError):
        UnitPrice(unit="BOGUS", unit_price=Decimal("1"))
    with pytest.raises(money.MoneyError):
        UnitPrice(unit="TOKEN", unit_price=Decimal("-1"))
    # 0 单价合法（免费额度）
    assert UnitPrice(unit="TOKEN", unit_price=Decimal("0")).unit_price == Decimal("0")


def test_pricing_assert_single_currency_refuses_mixed_and_empty() -> None:
    """`assert_single_currency`：混币种与空集合都抛。

    "混币种"是本包的一条硬边界（ADR-10 E-5：**不做汇率换算**）——
    它必须在这里被拦，而不是在金额相加时悄悄把 100 元当 100 美元。
    """
    assert b_pricing.assert_single_currency([CURRENCY_CNY]) == CURRENCY_CNY
    with pytest.raises(b_pricing.PricingError):
        b_pricing.assert_single_currency([CURRENCY_CNY, CURRENCY_USD])
    with pytest.raises(b_pricing.PricingError):
        b_pricing.assert_single_currency([])


def test_pricing_required_units_excludes_call() -> None:
    """`REQUIRED_UNITS` 是 `(TOKEN, IMAGE, SECOND)` —— **不含 `CALL`**。

    `CALL` 只在"调用方只给了数量、没给 metrics"时才用得上，
    因此它是可选的。这条断言的价值在于：若哪天把 `CALL` 加进必需集合，
    所有"只按 token 计价"的供应商版本会被 `validate_pricing_lines` 拒掉
    —— 而那是一次**静默的功能收窄**（旧价目突然不可用）。
    """
    assert b_pricing.REQUIRED_UNITS == ("TOKEN", "IMAGE", "SECOND")
    assert "CALL" not in b_pricing.REQUIRED_UNITS
    assert b_pricing.PRICING_UNITS == frozenset({"CALL", "TOKEN", "IMAGE", "SECOND"})


def test_pricing_validate_pricing_lines_requires_all_required_units() -> None:
    """缺必需单位 ⇒ 抛，且文案点出**缺了哪几个**。"""
    complete = [
        UnitPrice(unit="TOKEN", unit_price=Decimal("0.0001")),
        UnitPrice(unit="IMAGE", unit_price=Decimal("0.5")),
        UnitPrice(unit="SECOND", unit_price=Decimal("0.01")),
    ]
    assert set(b_pricing.validate_pricing_lines(complete)) == {"TOKEN", "IMAGE", "SECOND"}
    with pytest.raises(b_pricing.PricingError) as caught:
        b_pricing.validate_pricing_lines(complete[:1])
    assert "IMAGE" in str(caught.value) and "SECOND" in str(caught.value)


def test_ledger_entry_input_construction_guards() -> None:
    """`LedgerEntryInput.__post_init__` 的六项校验各一例。

    这是"账本分录在进库之前就已经合法"的**最早**一道面 ——
    它比九条 CHECK 更早（构造期），也比 service 的守卫更早。
    三层都要在，因为绕过路径不同：ORM / 原生 SQL / 直接构造。
    """
    target = uuid.uuid4()
    with pytest.raises(money.MoneyError):
        LedgerEntryInput(entry_kind="NOPE", amount=Decimal("1"))
    with pytest.raises(money.MoneyError):
        LedgerEntryInput(entry_kind=LEDGER_KIND_BILL, amount=Decimal("-1"))
    with pytest.raises(money.MoneyError):
        LedgerEntryInput(entry_kind=LEDGER_KIND_BILL, amount=Decimal("0"))
    with pytest.raises(money.MoneyError):
        LedgerEntryInput(
            entry_kind=LEDGER_KIND_REVERSAL, amount=Decimal("1"), reverses_ledger_id=target
        )
    with pytest.raises(money.MoneyError):
        LedgerEntryInput(entry_kind=LEDGER_KIND_REVERSAL, amount=Decimal("-1"))
    with pytest.raises(money.MoneyError):
        LedgerEntryInput(
            entry_kind=LEDGER_KIND_BILL, amount=Decimal("1"), reverses_ledger_id=target
        )
    with pytest.raises(money.MoneyError):
        LedgerEntryInput(entry_kind=LEDGER_KIND_BILL, amount=Decimal("1"), quantity=Decimal("1"))
    with pytest.raises(money.MoneyError):
        LedgerEntryInput(entry_kind=LEDGER_KIND_BILL, amount=Decimal("1"), unit="TOKEN")

    # 合法形态
    ok = LedgerEntryInput(entry_kind=LEDGER_KIND_BILL, amount=Decimal("1"))
    assert ok.quantity is None and ok.unit is None
    with_unit = LedgerEntryInput(
        entry_kind=LEDGER_KIND_USAGE, amount=Decimal("1"), quantity=Decimal("1"), unit="TOKEN"
    )
    assert with_unit.unit == "TOKEN"


def test_types_ledger_kind_sign_partition_covers_all_kinds() -> None:
    """`POSITIVE ∪ NEGATIVE == LEDGER_KINDS`，且两者**不相交**。

    这不是同义反复：`ADJUSTMENT` 被刻意排除在两侧之外（它的符号由业务定，
    正负都合法），所以 `∪` 恰好等于全集的**补**是一次真实的结构声明。
    写成"`{BILL,USAGE} ∪ {REVERSAL} == LEDGER_KINDS`"会立刻失败 ——
    而这正是要防的：把 ADJUSTMENT 误当成"必须是正的"。
    """
    assert POSITIVE_LEDGER_KINDS == frozenset({LEDGER_KIND_BILL, LEDGER_KIND_USAGE})
    assert NEGATIVE_LEDGER_KINDS == frozenset({LEDGER_KIND_REVERSAL})
    assert not (POSITIVE_LEDGER_KINDS & NEGATIVE_LEDGER_KINDS)
    assert LEDGER_KIND_ADJUSTMENT not in POSITIVE_LEDGER_KINDS
    assert LEDGER_KIND_ADJUSTMENT not in NEGATIVE_LEDGER_KINDS
    assert POSITIVE_LEDGER_KINDS | NEGATIVE_LEDGER_KINDS | {LEDGER_KIND_ADJUSTMENT} == LEDGER_KINDS


def test_types_reservation_state_partitions() -> None:
    """预占状态的三分：持有 / 终态，且互不相交、并集为全集。"""
    assert HOLDING_RESERVATION_STATES == frozenset(
        {RESERVATION_STATE_HELD, "EXPIRED"}
    )
    assert HOLDING_RESERVATION_STATES | {RESERVATION_STATE_RELEASED, RESERVATION_STATE_SETTLED} == (
        RESERVATION_STATES
    )
    assert not (HOLDING_RESERVATION_STATES & {RESERVATION_STATE_RELEASED, RESERVATION_STATE_SETTLED})
    assert RESERVATION_STATE_SETTLED in RESERVATION_STATES


def test_reconciliation_confirms_only_with_bill_entries() -> None:
    """"已结"的定义是**有 `BILL` 分录** —— `USAGE` 仍算未结。

    这条口径是 `reconciliation` 与 `service` 共用的单一来源
    （`reconciliation.CONFIRMING_LEDGER_KINDS` 与
    `schema.RECONCILIATION_CONFIRMING_KINDS` 必须一致）。
    若某天有人把 `USAGE` 也算成"已结"，那么"账本里有用量、
    但从未结算"的项目会被对账**放过**，而那条钱正是最该被追问的。
    """
    assert b_recon.CONFIRMING_LEDGER_KINDS == frozenset({LEDGER_KIND_BILL})
    assert RECONCILIATION_CONFIRMING_KINDS == b_recon.CONFIRMING_LEDGER_KINDS
    assert LEDGER_KIND_USAGE not in b_recon.CONFIRMING_LEDGER_KINDS
    assert LEDGER_KIND_ADJUSTMENT not in b_recon.CONFIRMING_LEDGER_KINDS
    assert LEDGER_KIND_REVERSAL not in b_recon.CONFIRMING_LEDGER_KINDS
    # 表清单在 test_reconciliation_tables_match_module_constants 里单独钉 ——
    # 两条判据各管一件事，不重复断言同一个事实。


def test_reconciliation_tables_match_module_constants() -> None:
    """`schema.RECONCILIATION_TABLES` 与 `reconciliation` 两张表常量同源。"""
    assert RECONCILIATION_TABLES == (
        b_recon.TABLE_BUDGET_RESERVED,
        b_recon.TABLE_BUDGET_COMMITTED,
    )
    assert b_recon.TABLE_BUDGET_RESERVED == "budget_reservations"
    assert b_recon.TABLE_BUDGET_COMMITTED == "usage_ledger"


# --- 1.x DDL 文本自洽（与迁移同源） ---------------------------------------


def test_ddl_immutable_tables_are_exactly_the_declared_pair() -> None:
    """不可变表恰好两张，且与 `models.IMMUTABLE_TABLES` 一致。"""
    assert b_ddl.IMMUTABLE_TABLES == ("pricing_versions", "usage_ledger")
    assert b_ddl.OWN_TABLES == (
        "pricing_versions",
        "cost_estimates",
        "project_budgets",
        "budget_reservations",
        "usage_ledger",
    )
    assert set(b_ddl.IMMUTABLE_TABLES) <= set(b_ddl.OWN_TABLES)


def test_ddl_trigger_names_are_the_two_immutability_triggers() -> None:
    """`trigger_names()` 恰好两项；`consistency_trigger_names()` 显式为**空**。

    第二项是"我确认本包没有一致性触发器"的**声明**，
    不是"我忘了写"。空元组与"函数返回了空"在测试里同形，
    因此这里显式断言空 —— 将来若加了一致性触发器，这条会亮，
    提醒去补对应的第 3 层判据。
    """
    assert b_ddl.trigger_names() == (
        "trg_pricing_versions_immutable",
        "trg_usage_ledger_immutable",
    )
    assert b_ddl.immutable_trigger_names() == b_ddl.trigger_names()
    assert b_ddl.consistency_trigger_names() == ()
    assert b_ddl.consistency_items() == []
    assert b_ddl.immutable_function_names() == ("rednote_reject_mutation",)


def test_ddl_all_trigger_ddl_produces_two_complete_five_tuples() -> None:
    """`all_trigger_ddl()` 给迁移用的 DDL 恰好两条，且每条五个字段都非空。

    迁移与测试**调同一个函数**（不各写一遍 DDL 文本）—— 于是
    "索引/触发器改了但迁移没跟"会在第 1 层直接暴露。
    """
    items = b_ddl.all_trigger_ddl()
    assert len(items) == 2
    for table, func_ddl, trigger_ddl, drop_trigger, drop_func in items:
        assert table in b_ddl.IMMUTABLE_TABLES
        assert func_ddl.strip() and trigger_ddl.strip()
        assert drop_trigger.strip() and drop_func.strip()
        assert table in trigger_ddl


def test_ddl_partial_unique_index_spec_matches_declaration() -> None:
    """部分唯一索引的**声明**（`PARTIAL_UNIQUE_INDEXES`）与**实测**（`partial_index_specs`）逐字段一致。

    `PARTIAL_UNIQUE_INDEXES` 的元组是 5 元素（末位是 `why` 文案），
    而 `partial_index_specs()` 给的是 4 键 dict —— 两者形状不同但必须对得上。
    这条判据同时也是"`why` 文案没被删掉"的守卫：
    那条文案解释的是"为什么不能退化成普通 UNIQUE"，
    删了它下一个读代码的人就会想"去掉 WHERE 不是更简单吗"。
    """
    specs = b_ddl.partial_index_specs()
    assert set(specs) == {"uq_usage_ledger_idempotency"}
    spec = specs["uq_usage_ledger_idempotency"]
    assert spec == {
        "columns": ("idempotency_key",),
        "unique": True,
        "where": "idempotency_key IS NOT NULL",
        "table": "usage_ledger",
    }

    declarations = {
        name: (table, columns, where) for name, table, columns, where, _why in b_ddl.PARTIAL_UNIQUE_INDEXES
    }
    assert set(declarations) == set(specs)
    for name, (table, columns, where) in declarations.items():
        assert table == specs[name]["table"]
        assert columns == specs[name]["columns"]
        assert where == specs[name]["where"]
        assert b_ddl.PARTIAL_UNIQUE_INDEXES[0][-1].strip(), "`why` 文案不得为空"

    ddl_items = b_ddl.partial_unique_index_ddl()
    assert len(ddl_items) == 1
    name, ddl = ddl_items[0]
    assert name == "uq_usage_ledger_idempotency"
    assert "UNIQUE INDEX" in ddl.upper()
    assert "WHERE idempotency_key IS NOT NULL" in ddl


def test_ddl_index_spec_drift_is_empty() -> None:
    """`index_spec_drift()` 为空 ⇒ 模型声明的索引与 `ddl` 的清单一致。

    空列表**就是**"一致"的表达（不是"没跑"）—— 因此这里同时断言
    它确实返回了列表类型，避免"函数不存在导致异常被吞"的假绿。
    """
    drift = b_ddl.index_spec_drift()
    assert isinstance(drift, list)
    assert drift == [], f"模型与 ddl 的索引清单漂移：{drift}"


def test_ddl_unique_violation_sqlstate_constant() -> None:
    """`UNIQUE_VIOLATION_SQLSTATE` 与 `immutability` 的常量口径。"""
    assert b_ddl.UNIQUE_VIOLATION_SQLSTATE == "23505"
    assert IMMUTABLE_RECORD_SQLSTATE == "23001"


def test_pricing_fingerprint_is_stable_and_discriminating() -> None:
    """指纹对同一份价目稳定、对不同价目不同、未知币种报错。

    这里**只测函数自身行为**，不声称"对账会用它" —— 见下条。
    """
    prices = {"TOKEN": Decimal("0.0001"), "CALL": Decimal("12.5")}
    a = b_pricing.pricing_version_fingerprint(
        version_id=uuid.uuid4(), currency=CURRENCY_CNY, prices=prices
    )
    same = b_pricing.pricing_version_fingerprint(
        version_id=uuid.uuid4(), currency=CURRENCY_CNY, prices=dict(prices)
    )
    # 同一个体（id 不同）⇒ 同一个指纹：id 刻意不进输出。
    assert a == same, "指纹不该随 version_id 变化（id 不参与输出）"
    # 单位顺序无关：入参字典顺序不同不该改指纹。
    shuffled = {"CALL": Decimal("12.5"), "TOKEN": Decimal("0.0001")}
    assert (
        b_pricing.pricing_version_fingerprint(
            version_id=uuid.uuid4(), currency=CURRENCY_CNY, prices=shuffled
        )
        == a
    ), "指纹必须按单位名排序，与入参顺序无关"
    # 单价变了 ⇒ 指纹必须变（这是它能当"第二道判据"的前提）。
    changed = dict(prices)
    changed["CALL"] = Decimal("13.5")
    assert (
        b_pricing.pricing_version_fingerprint(
            version_id=uuid.uuid4(), currency=CURRENCY_CNY, prices=changed
        )
        != a
    ), "单价被改而指纹不变 ⇒ 它当不了任何判据"
    # 币种变了也必须变。
    assert (
        b_pricing.pricing_version_fingerprint(
            version_id=uuid.uuid4(), currency=CURRENCY_USD, prices=prices
        )
        != a
    )
    with pytest.raises(b_pricing.PricingError):
        b_pricing.pricing_version_fingerprint(
            version_id=uuid.uuid4(), currency="XYZ", prices=prices
        )


def test_pricing_fingerprint_is_not_wired_into_reconciliation() -> None:
    """★ **钉住"未接线"这个事实本身**（CR-1 MINOR-1）。

    `pricing_version_fingerprint` 的 docstring 曾声称"对账能立刻发现
    单价被绕过修改"，但 `reconciliation.py` 不 import 它、
    `cost_estimates` 也没有存指纹的列 —— 即**这条第二道判据当时不存在**。
    这正是 ISS-95 的形态。

    本用例把当前真实状态钉下来，作用是**反向的**：
    · 若将来有人真的把指纹接进对账，这条用例会**变红** ——
      那是好事，说明该回来把 docstring 里的"未接线"说明删掉，
      并把本用例改成"已接线"的正面判据。
    · 在没人接线之前，它挡住"docstring 又写回设计意图"的回退。

    ⚠️ 判据纪律：断言"某处没有某物"的用例，必须同时断言**该物在别处存在**
    （否则文件路径写错、import 写错都会让它永远绿）。故同时断言
    `pricing.VERSION` 前缀与函数可调用 —— 证明我找对了地方。
    """
    recon_src = (
        SERVICES_BACKEND
        / "src" / "rednote" / "modules" / "billing" / "reconciliation.py"
    ).read_text(encoding="utf-8")
    models_src = (
        SERVICES_BACKEND
        / "src" / "rednote" / "modules" / "billing" / "models.py"
    ).read_text(encoding="utf-8")
    # 正向锚：文件确实是我要找的那两个（防止路径写错 ⇒ 判据恒绿）。
    assert "def reconcile_pending" in recon_src, "reconciliation.py 找错了"
    assert "class CostEstimate" in models_src, "models.py 找错了"
    # 反向断言：当前状态 = 未接线。
    assert "fingerprint" not in recon_src, (
        "reconciliation 已经开始用指纹了 —— 请更新 "
        "pricing.pricing_version_fingerprint 的 docstring（删掉『未接线』"
        "一节）并把本用例改成『已接线』的正面判据。"
    )
    assert "fingerprint" not in models_src, (
        "cost_estimates 已经存了指纹 —— 同上传导处理。"
    )
    # 而且它确实还能用（不是"被删了一半"）。
    assert callable(b_pricing.pricing_version_fingerprint)


# ===========================================================================
# 第 2 层：服务层编排（真库）
# ===========================================================================


def test_get_budget_view_returns_unconfigured_zero_for_unknown_project(
    env: BillingEnv,
) -> None:
    """没设过预算的项目拿到**四值全零 + `configured=False`**（不是 404）。

    这是刻意选择：只读视图要让页面**先能渲染**，再谈 404。
    用 `configured` 这一维表达"没配过"，而不是用"四值都是 0"——
    后者与"额度确实是 0 元"同形。
    """
    with _repo(env) as repo:
        view = b_service.get_budget_view(repo, project_id=env.other_project_id)
    assert view.configured is False
    assert view.limit == money.ZERO
    assert view.committed == money.ZERO
    assert view.reserved == money.ZERO
    assert view.currency == CURRENCY_CNY


def test_set_budget_then_read_back_four_values(env: BillingEnv) -> None:
    """设额度后四值自洽：`available == limit - committed - reserved`。"""
    with _repo(env) as repo:
        view = b_service.set_budget(repo, project_id=env.project_id, limit_amount="1000")
    assert view.configured is True
    assert view.limit == Decimal("1000.00000000")
    assert view.available == Decimal("1000.00000000")
    assert view.committed == money.ZERO and view.reserved == money.ZERO


def test_set_budget_refuses_negative_limit(env: BillingEnv) -> None:
    """负额度 ⇒ 422 `AMOUNT_NOT_POSITIVE`。"""
    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="-1")
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == REASON_AMOUNT_NOT_POSITIVE


def test_set_budget_refuses_currency_change_while_amounts_exist(
    env: BillingEnv,
) -> None:
    """**已有金额时不许换币种** ⇒ 422 `CURRENCY_MISMATCH`。

    换币种会**重新解释已发生的数字**（100 元变 100 美元）——
    这是语义变更而不是数值变更，所以单列一个 reason。
    零占用时允许换（"设错了要改"的门）。
    """
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="100")
        b_service.reserve_budget(repo, project_id=env.project_id, amount="10")

    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.set_budget(
            repo, project_id=env.project_id, limit_amount="100", currency=CURRENCY_USD
        )
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == REASON_CURRENCY_MISMATCH


def test_set_budget_allows_currency_change_when_no_amounts(env: BillingEnv) -> None:
    """零占用时**允许**换币种（`OR (committed = 0 AND reserved = 0)` 的那扇门）。"""
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="0")
        view = b_service.set_budget(
            repo, project_id=env.project_id, limit_amount="500", currency=CURRENCY_USD
        )
    assert view.currency == CURRENCY_USD
    assert view.limit == Decimal("500.00000000")


def test_set_budget_refuses_limit_below_existing_occupancy(env: BillingEnv) -> None:
    """调低到装不下既有占用 ⇒ 422 `BUDGET_EXCEEDED`（由 UPDATE 的 WHERE 判，不是 CHECK）。"""
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="100")
        b_service.reserve_budget(repo, project_id=env.project_id, amount="80")

    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="50")
    assert caught.value.code is ErrorCode.BUDGET_EXCEEDED
    assert caught.value.details["reason"] == REASON_BUDGET_EXCEEDED

    # 账面未被改动
    snap = _budget_snapshot(env)
    assert snap["limit"] == Decimal("100.00000000")
    assert snap["reserved"] == Decimal("80.00000000")


def test_set_budget_on_unknown_project_is_404(env: BillingEnv) -> None:
    """不存在的项目 ⇒ 404 `PROJECT_NOT_FOUND`（写路径必须校验）。"""
    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.set_budget(repo, project_id=uuid.uuid4(), limit_amount="10")
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert caught.value.details["reason"] == REASON_PROJECT_NOT_FOUND


def test_reserve_budget_on_project_without_budget_is_exceeded(
    env: BillingEnv,
) -> None:
    """从未设过预算的项目 ⇒ `ensure_budget` 建 `limit=0` ⇒ **必然** `BUDGET_EXCEEDED`。

    这是**刻意**的：一个非零默认值会让项目凭空拥有额度。
    """
    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.reserve_budget(repo, project_id=env.project_id, amount="1")
    assert caught.value.code is ErrorCode.BUDGET_EXCEEDED
    # 账面被建出来了（ensure_budget 的副作用），且 limit == 0
    snap = _budget_snapshot(env)
    assert snap["configured"] is True
    assert snap["limit"] == money.ZERO


def test_reserve_budget_exceeded_details_carry_full_snapshot(env: BillingEnv) -> None:
    """超限的 `details` 带齐六个数 —— 前端据此渲染"还差多少"。"""
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="100")
        b_service.reserve_budget(repo, project_id=env.project_id, amount="90")
        with pytest.raises(ApiError) as caught:
            b_service.reserve_budget(repo, project_id=env.project_id, amount="20")
    details = caught.value.details
    assert details["reason"] == REASON_BUDGET_EXCEEDED
    for key in ("projectId", "requestedAmount", "currency", "limit", "committed", "reserved", "available"):
        assert key in details, f"超限 details 缺 {key}"
    assert details["limit"] == "100.00000000"
    assert details["reserved"] == "90.00000000"
    assert details["available"] == "10.00000000"


def test_reserve_budget_refuses_zero_and_negative(env: BillingEnv) -> None:
    """**0 元预占被明确拒绝** —— 它会通过所有并发判据（恒真），等于没在管钱。"""
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="100")
        for bad in ("0", "0.00000000", "-1"):
            with pytest.raises(ApiError) as caught:
                b_service.reserve_budget(repo, project_id=env.project_id, amount=bad)
            assert caught.value.code is ErrorCode.VALIDATION_FAILED
            assert caught.value.details["reason"] == REASON_AMOUNT_NOT_POSITIVE


def test_reserve_budget_is_atomic_under_concurrency(env: BillingEnv) -> None:
    """★ **W10 预算并发预占测试**：N 线程竞争，成功数 × 单笔 ≤ 限额。

    ## 判据的形状（以及为什么它是可 FAIL 的）

    `limit = 300`、单笔 `100`、5 个线程并发 ⇒ 成功的必须有且只有 3 笔。
    断言三件事，缺一不可：

      1. `sum(granted) * 100 <= 300` —— **上限**（并发没把额度用超）；
      2. `sum(granted) == 3` —— **下界**（不会因为过度的悲观锁把它们
         互相饿死；"只成功 1 笔"也是缺陷，只是方向相反）；
      3. `reserved == 300` —— 账面与成功笔数**对得上**
         （防止"UPDATE 成功但明细没落"或反之）。

    只做第 1 条会漏掉"实现变成串行化但正确"的退化；
    只做第 2 条会漏掉"额度被用超"。两条互相夹逼。

    ## 为什么不用 `ps` 判断有没有并行干扰

    本用例自己造并发。判据只看**本项目的账面**（`env.project_id`
    是本次新建的），因此别的会话在测试库里跑什么都不会影响结论 ——
    这一点比"等一个静止窗"可靠得多。
    """
    attempts = 5
    chunk = Decimal("100.00000000")
    limit = Decimal("300.00000000")

    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="300")

    def attempt(index: int) -> bool:
        try:
            with session_scope(env.settings) as session:
                b_service.reserve_budget(
                    BillingRepository(session),
                    project_id=env.project_id,
                    amount=chunk,
                    ref=f"conc-{index}",
                )
            return True
        except ApiError as exc:
            assert exc.code is ErrorCode.BUDGET_EXCEEDED, (
                f"并发失败的原因必须是额度不足，实际 {exc.code}: {exc}"
            )
            return False

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        granted = list(pool.map(attempt, range(attempts)))

    succeeded = sum(1 for ok in granted if ok)
    assert succeeded * chunk <= limit, "并发预占把额度用超了"
    assert succeeded == 3, f"期望恰好 3 笔成功，实际 {succeeded}"
    assert _budget_snapshot(env)["reserved"] == limit
    assert _scalar(
        env,
        "select count(*) from budget_reservations where project_id = :p"
        " and state in ('HELD','EXPIRED')",
        {"p": str(env.project_id)},
    ) == 3


def test_concurrent_reservations_exactly_at_limit_all_succeed(env: BillingEnv) -> None:
    """★ 反向用例：合计**恰等于**限额时，**每一笔都必须成功**。

    上一条防"用超"，这一条防"用不满"—— 后者同样是缺陷，
    但它的症状是"业务偶发失败"，比超限更难被发现（没有数据异常）。

    本机实测：`chunk = limit // 4`、4 线程 ⇒ 4 笔全过，`reserved == limit`。
    """
    attempts = 4
    chunk = Decimal("25.00000000")
    limit = Decimal("100.00000000")

    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="100")

    def attempt(index: int) -> bool:
        try:
            with session_scope(env.settings) as session:
                b_service.reserve_budget(
                    BillingRepository(session),
                    project_id=env.project_id,
                    amount=chunk,
                    ref=f"exact-{index}",
                )
            return True
        except ApiError:
            return False

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        granted = list(pool.map(attempt, range(attempts)))

    assert all(granted), f"合计等于限额时不该有失败，实际 {granted}"
    assert _budget_snapshot(env)["reserved"] == limit


def test_release_reservation_is_idempotent(env: BillingEnv) -> None:
    """★ 释放是**补偿动作**：重复调用返回同一结果、**不报错**、账面只还一次。

    做成 409 会让补偿路径产生新的失败，而补偿失败意味着钱被永久占住。
    因此这里断言：第二次返回的状态仍是 `RELEASED`，且 `reserved` 没被再减一次。
    """
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="100")
        view = b_service.reserve_budget(repo, project_id=env.project_id, amount="40")
        reservation_id = view.id

    with _repo(env) as repo:
        first = b_service.release_reservation(repo, reservation_id=reservation_id)
    assert first.state == RESERVATION_STATE_RELEASED
    assert _budget_snapshot(env)["reserved"] == money.ZERO

    with _repo(env) as repo:
        second = b_service.release_reservation(repo, reservation_id=reservation_id)
    assert second.state == RESERVATION_STATE_RELEASED
    assert second.id == first.id
    assert _budget_snapshot(env)["reserved"] == money.ZERO, "重复释放不得多还一次"


def test_release_reservation_on_settled_is_409(env: BillingEnv) -> None:
    """**已结算的预占不能被释放** ⇒ 409（那会造成双重归还）。"""
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="100")
        view = b_service.reserve_budget(repo, project_id=env.project_id, amount="40")
        reservation_id = view.id
        b_service.settle_reservation(repo, reservation_id=reservation_id, actual_amount="40")

    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.release_reservation(repo, reservation_id=reservation_id)
    assert caught.value.code is ErrorCode.INVALID_TRANSITION
    assert caught.value.details["reason"] == REASON_RESERVATION_NOT_HOLDING


def test_release_unknown_reservation_is_404(env: BillingEnv) -> None:
    """不存在的预占 ⇒ 404 `RESERVATION_NOT_FOUND`。"""
    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.release_reservation(repo, reservation_id=uuid.uuid4())
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert caught.value.details["reason"] == REASON_RESERVATION_NOT_FOUND


def test_settle_reservation_autofills_bill_entry(env: BillingEnv) -> None:
    """★ `entries` 为空时**自动补一条 `BILL`**，且幂等键形如 `settle:<id>`。

    这是本包最重要的一条自洽约束：`committed_amount` 的每一次增加，
    账本里都必须有对应分录 —— 否则 `recompute_committed`（R10 的对账判据）
    会永远报差异，而"账面有、账本没有"正是最该报警的方向。
    """
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="100")
        view = b_service.reserve_budget(repo, project_id=env.project_id, amount="60")
        reservation_id = view.id

    with _repo(env) as repo:
        settled = b_service.settle_reservation(
            repo, reservation_id=reservation_id, actual_amount="55"
        )
    assert settled.state == RESERVATION_STATE_SETTLED
    assert settled.settled_amount == Decimal("55.00000000")

    rows = _raw(
        env,
        "select entry_kind, amount, idempotency_key, description from usage_ledger"
        " where project_id = :p",
        {"p": str(env.project_id)},
    )
    assert len(rows) == 1
    kind, amount, idem, desc = rows[0]
    assert kind == LEDGER_KIND_BILL
    assert amount == Decimal("55.00000000")
    assert idem == f"settle:{reservation_id}"
    assert str(reservation_id) in desc

    snap = _budget_snapshot(env)
    assert snap["committed"] == Decimal("55.00000000")
    assert snap["reserved"] == money.ZERO, "结算必须把预占**全部**释放（差额回可用）"


def test_settle_reservation_allows_actual_different_from_reserved(
    env: BillingEnv,
) -> None:
    """实际可以**大于**预占（只要不超限）—— 差额由可用额度补。

    本机实测：限 300 / 预占 100 / 实际 250 ⇒ committed=250、reserved=0、available=50。
    """
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="300")
        view = b_service.reserve_budget(repo, project_id=env.project_id, amount="100")
        reservation_id = view.id

    with _repo(env) as repo:
        settled = b_service.settle_reservation(
            repo, reservation_id=reservation_id, actual_amount="250"
        )
    assert settled.settled_amount == Decimal("250.00000000")
    snap = _budget_snapshot(env)
    assert snap["committed"] == Decimal("250.00000000")
    assert snap["reserved"] == money.ZERO
    assert snap["limit"] - snap["committed"] == Decimal("50.00000000")


def test_settle_reservation_refuses_mismatched_entries_total(env: BillingEnv) -> None:
    """★ `Σ entries != actual_amount` ⇒ 422 `LEDGER_TOTAL_MISMATCH`。

    两个数是**两条独立路径**算出来的（业务结论 vs 花销构成）。
    不等说明"结算金额"与"入账明细"各说各话 —— 本包拒绝替调用方选一个当真。
    """
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="100")
        view = b_service.reserve_budget(repo, project_id=env.project_id, amount="60")
        reservation_id = view.id

    entries = [LedgerEntryInput(entry_kind=LEDGER_KIND_BILL, amount=Decimal("10"))]
    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.settle_reservation(
            repo, reservation_id=reservation_id, actual_amount="55", entries=entries
        )
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == REASON_LEDGER_TOTAL_MISMATCH
    # 事务整体回滚：账面与明细都没动
    snap = _budget_snapshot(env)
    assert snap["reserved"] == Decimal("60.00000000")
    assert snap["committed"] == money.ZERO


def test_settle_reservation_refuses_negative_actual(env: BillingEnv) -> None:
    """负实际金额 ⇒ 422（退款走冲正分录，不改历史行金额）。"""
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="100")
        view = b_service.reserve_budget(repo, project_id=env.project_id, amount="60")
        reservation_id = view.id

    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.settle_reservation(repo, reservation_id=reservation_id, actual_amount="-1")
    assert caught.value.details["reason"] == REASON_AMOUNT_NOT_POSITIVE


def test_settle_reservation_exceeding_limit_is_budget_exceeded(env: BillingEnv) -> None:
    """实际金额让 `committed` 超限 ⇒ 422 `BUDGET_EXCEEDED`，且**状态未迁移**。"""
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="100")
        view = b_service.reserve_budget(repo, project_id=env.project_id, amount="60")
        reservation_id = view.id

    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.settle_reservation(repo, reservation_id=reservation_id, actual_amount="150")
    assert caught.value.code is ErrorCode.BUDGET_EXCEEDED
    assert _scalar(
        env,
        "select state from budget_reservations where id = :r",
        {"r": str(reservation_id)},
    ) == RESERVATION_STATE_HELD, "失败时必须回滚到 HELD"


def test_settle_reservation_twice_is_409(env: BillingEnv) -> None:
    """重复结算 ⇒ 409 `RESERVATION_NOT_HOLDING`。"""
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="100")
        view = b_service.reserve_budget(repo, project_id=env.project_id, amount="40")
        reservation_id = view.id
        b_service.settle_reservation(repo, reservation_id=reservation_id, actual_amount="40")

    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.settle_reservation(repo, reservation_id=reservation_id, actual_amount="40")
    assert caught.value.code is ErrorCode.INVALID_TRANSITION
    assert caught.value.details["reason"] == REASON_RESERVATION_NOT_HOLDING


def test_append_ledger_entry_is_idempotent_on_key(env: BillingEnv) -> None:
    """★ 幂等键非空时**重复追加返回已有那条**，且不报错。

    `append_ledger_entry` 把 `23505` 当**正常的并发结果**（"这笔已经记过了"）
    —— 与 `23001`（防线被违反）是完全不同的两件事。
    """
    entry = LedgerEntryInput(
        entry_kind=LEDGER_KIND_ADJUSTMENT,
        amount=Decimal("7.50000000"),
        description="人工调整",
        idempotency_key=f"adj-{uuid.uuid4().hex[:10]}",
    )
    with _repo(env) as repo:
        first = b_service.append_ledger_entry(repo, project_id=env.project_id, entry=entry)
    with _repo(env) as repo:
        second = b_service.append_ledger_entry(repo, project_id=env.project_id, entry=entry)

    assert first.id == second.id, "幂等键命中必须返回同一条"
    assert _scalar(
        env, "select count(*) from usage_ledger where project_id = :p", {"p": str(env.project_id)}
    ) == 1


def test_append_ledger_entry_concurrent_same_key_never_raises(
    env: BillingEnv,
) -> None:
    """★ **并发**同键追加：全部返回同一条，**没有任何线程抛异常**。

    ## 为什么上面那条串行用例测不出这个缺陷（CR-1）

    `test_append_ledger_entry_is_idempotent_on_key` 是**串行**调用：
    第二次调用时第一行已经提交，`find_ledger_entry_by_idempotency`
    会命中快路径直接返回 —— 它证明的是"快路径有效"，
    **完全没有走到** `except IntegrityError` 那段。

    修复前的实现只有快路径（"先查再插"），因此上面那条用例**绿着**，
    而并发同键时 8 个线程里有 5 个把 `23505` 冒成 HTTP 500（实测）。
    这正是"判据没打在缺陷所在的那条路径上"的形态。

    ## 怎么才能稳定复现

    必须让 N 个线程在**同一个窗口**内都查到"不存在"。用
    `threading.Barrier` 让它们等齐后同时出发 —— 否则线程是串行到的，
    又退化成快路径。修复后实测 8/8 收敛、库里恰好 1 行。
    """
    import threading

    workers = 8
    barrier = threading.Barrier(workers)
    key = f"cr-idem-{uuid.uuid4().hex[:10]}"
    entry = LedgerEntryInput(
        entry_kind=LEDGER_KIND_ADJUSTMENT,
        amount=Decimal("7"),
        idempotency_key=key,
    )

    def append(_: int) -> tuple[str, str]:
        barrier.wait()
        try:
            with _repo(env) as repo:
                view = b_service.append_ledger_entry(
                    repo, project_id=env.project_id, entry=entry
                )
            return ("OK", str(view.id))
        except Exception as exc:  # noqa: BLE001 —— 这条判据要看的就是"有没有异常"
            sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", "") or type(
                exc
            ).__name__
            return ("RAISE", sqlstate)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(append, range(workers)))

    raised = [r for r in results if r[0] == "RAISE"]
    assert not raised, (
        f"并发同键追加有 {len(raised)}/{workers} 抛异常（{sorted({r[1] for r in raised})}）"
        " —— `23505` 必须被当成正常并发结果回查，不能冒成 500。"
    )
    ids = {r[1] for r in results if r[0] == "OK"}
    assert len(ids) == 1, f"并发同键必须收敛到同一条，实际拿到 {len(ids)} 个不同 id"
    assert _scalar(
        env,
        "select count(*) from usage_ledger where idempotency_key = :k",
        {"k": key},
    ) == 1, "幂等键对应的库里恰好 1 行"


def test_append_ledger_entry_without_key_creates_distinct_rows(env: BillingEnv) -> None:
    """**没有幂等键**的人工分录可以有多条（部分唯一索引的 `WHERE` 就是为此）。

    这两条与下面第 3 层的"双向活性"合起来才是完整判据：
      · 这里证明"NULL 不受唯一约束"（业务上必需 —— 人工调整没有天然幂等键）；
      · 第 3 层证明"非 NULL 重复确实会撞 `23505`"（防线真的在）。
    只做前者会得到"索引好像没生效"的错觉；只做后者则不知道 NULL 是否被误伤。
    """
    with _repo(env) as repo:
        for _ in range(2):
            b_service.append_ledger_entry(
                repo,
                project_id=env.project_id,
                entry=LedgerEntryInput(entry_kind=LEDGER_KIND_ADJUSTMENT, amount=Decimal("1")),
            )
    assert _scalar(
        env, "select count(*) from usage_ledger where project_id = :p", {"p": str(env.project_id)}
    ) == 2


def test_append_ledger_entry_reversal_requires_target(env: BillingEnv) -> None:
    """冲正必须引用被冲正的分录；且**不改旧行**（新分录表达"这笔记错了"）。

    这是 R5 的落地形态：错的在账本里**永远看得见**。
    """
    with _repo(env) as repo:
        original = b_service.append_ledger_entry(
            repo,
            project_id=env.project_id,
            entry=LedgerEntryInput(entry_kind=LEDGER_KIND_BILL, amount=Decimal("20")),
        )

    with _repo(env) as repo:
        reversal = b_service.append_ledger_entry(
            repo,
            project_id=env.project_id,
            entry=LedgerEntryInput(
                entry_kind=LEDGER_KIND_REVERSAL,
                amount=Decimal("-20"),
                reverses_ledger_id=original.id,
            ),
        )
    assert reversal.amount == Decimal("-20.00000000")

    rows = _raw(
        env,
        "select entry_kind, amount from usage_ledger where project_id = :p order by created_at, id",
        {"p": str(env.project_id)},
    )
    assert len(rows) == 2, "旧行必须保留"
    assert rows[0][1] == Decimal("20.00000000"), "旧行的金额不得被改"
    # 净额归零 —— append-only 账本不需要 WHERE 就能算对
    assert sum(amount for _kind, amount in rows) == money.ZERO


def test_append_ledger_entry_source_ref_too_long_is_422(env: BillingEnv) -> None:
    """超长 `source_ref` ⇒ 422（DB 层是 `22001`/500，这里提前拦成人话）。

    ## 为什么这里测 `source_ref` 而不是预占的 `ref`

    `reserve_budget` 的 `ref` 闸门（`_require_text(ref, ...)`）排在
    **拿到额度之后** —— 未设预算的项目会先抛 `BUDGET_EXCEEDED`，
    于是"超长"这条根本走不到。那是刻意的顺序（先确认有钱，再校验附注），
    但也意味着**用 `reserve_budget` 测超长会测到一个别的错误**。

    `append_ledger_entry` 的 `source_ref` 闸门在插入之前，是本包
    "账本侧来源串"的长度闸门（`MAX_LEDGER_SOURCE_REF_CHARS`，
    与预占的 `MAX_RESERVATION_REF_CHARS` 是两个独立常量）。
    """
    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.append_ledger_entry(
            repo,
            project_id=env.project_id,
            entry=LedgerEntryInput(
                entry_kind=LEDGER_KIND_ADJUSTMENT, amount=Decimal("1")
            ),
            source_ref="x" * (MAX_LEDGER_SOURCE_REF_CHARS + 1),
        )
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == "TEXT_TOO_LONG"
    # 定位信息在 `fieldErrors[0].field`（`details` 没有顶层 `field` 键）
    assert caught.value.details["fieldErrors"][0]["field"] == "sourceRef"
    # 没有落任何分录（校验在插入之前）
    assert _scalar(
        env,
        "select count(*) from usage_ledger where project_id = :p",
        {"p": str(env.project_id)},
    ) == 0


def test_estimate_cost_uses_configured_batch_limits(env: BillingEnv) -> None:
    """★ 数量上限读 **WP24 的生效配置**（唯一真值来源），不是本包自己写死。

    本包**不重写一份数量校验** —— 那会让 10/30/50 这条 G3 硬约束
    出现第二个真值来源，而两个来源必然分叉。
    """
    _grant_batch_limits(env)
    _seed_pricing_version(env)

    with _repo(env) as repo:
        view = b_service.estimate_cost(
            repo,
            project_id=env.project_id,
            batch_kind="TRIAL",
            quantity=10,  # 恰好在上限
            provider_id=env.provider_id,
            metrics={"calls": "10"},
        )
    assert view.state == ESTIMATE_STATE_DRAFT
    assert view.batch_kind == "TRIAL"
    assert view.quantity == 10
    assert view.total_amount == Decimal("125.00000000")

    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.estimate_cost(
            repo,
            project_id=env.project_id,
            batch_kind="TRIAL",
            quantity=11,  # 越界
            provider_id=env.provider_id,
            metrics={"calls": "11"},
        )
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == REASON_QUANTITY_ABOVE_MAX


def test_estimate_cost_unknown_batch_kind_is_422(env: BillingEnv) -> None:
    """未知档位 ⇒ 422，reason 来自 planning 的 `REASON_BATCH_KIND_UNKNOWN`（翻译而非重写）。"""
    _grant_batch_limits(env)
    _seed_pricing_version(env)

    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.estimate_cost(
            repo,
            project_id=env.project_id,
            batch_kind="NOPE",
            quantity=1,
            provider_id=env.provider_id,
            metrics={"calls": "1"},
        )
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == "BATCH_KIND_UNKNOWN"


def test_estimate_cost_without_limits_configured_is_422(
    env: BillingEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**未配置**上限时明确报错（`LIMITS_NOT_CONFIGURED`），**不用默认值兜底**。

    兜底会让"读不到配置"表现为"用了 10/30/50"且不报错 ——
    于是"我改的值为什么没生效"就无从查起。

    ## ★ 为什么必须**打桩**制造"未配置"，而不能只"不写项目级配置"

    `upgrade head` 建出的库里，`GLOBAL batch.limits` **是被种下的**
    —— WP24 迁移 `022ba9804d66` 末尾调 `bootstrap_defaults`，按 ADR-13
    写入 `{TRIAL:10, SCALE:30, BULK:50}`（见该迁移 docstring §五：
    "数据回填：调 `bootstrap_defaults`，不写裸 SQL"）。
    而 `get_effective_limits` 对"全局有行、项目无行"的组合会返回
    `source='GLOBAL'` ⇒ 项目**继承**了默认值 ⇒ `as_limits()` 成功、
    不抛错。

    ⇒ 于是"另一个项目不写项目级配置"这个前提，**只在没跑过 WP24 迁移的库上
    才等于"未配置"**。本包在 `rednote_test`（夹具建表，全局行被
    `DROP+create_all` 清掉）上写这条用例是对的，但换到"由迁移建出的库"
    上必然失败 —— 而"测试库应由迁移建出"正是本项目的标准做法。

    **实测**（两种库对照，同一份代码）：

    | 库 | `GLOBAL batch.limits` | 本用例 |
    |---|---|---|
    | `rednote_test`（夹具建表） | 不存在 | ✅ 绿 |
    | 迁移建出的库 | `{BULK:50, SCALE:30, TRIAL:10}` | 🔴 `DID NOT RAISE` |

    ⇒ 判决：**是"用例把库的历史当成了前提"，不是实现错**。
    实现的行为（继承全局默认值）是 ADR-13 §3.6.3 要的，
    真正要测的是"**全局也没有**的时候不准兜底"。

    修法是**把前提显式化**：patch `settings_service.get_effective_limits`
    返回一个 `configured=False` 的 `EffectiveSetting`，直接构造出
    "全局、项目都没有"的语义。这样用例测的是它真正想测的那件事，
    且与库怎么建出来的**无关**。

    > `service.estimate_cost` 里对该函数用的是**函数内局部 import**
    > （避免 billing ← planning 的模块级依赖），patch 模块属性照样生效
    > —— 局部 `from ... import name` 每次执行都去模块取属性。
    > 这一点本用例在两种库上都实测过（不再依赖读者的信念）。
    """
    # ★ 显式制造"未配置"：全局与项目都没有 `batch.limits`。
    #   不用"换个项目"来冒充 —— 那在迁移建出的库上会被全局默认值兜住。
    unconfigured = EffectiveSetting(
        key=SETTING_KEY_BATCH_LIMITS,
        configured=False,
        value=None,
        source=None,
        global_value=None,
        diverged=False,
        revision_no=None,
        project_id=env.other_project_id,
    )
    monkeypatch.setattr(
        planning_settings_service, "get_effective_limits", lambda *a, **k: unconfigured
    )

    _seed_pricing_version(env)
    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.estimate_cost(
            repo,
            project_id=env.other_project_id,  # 未写 batch.limits 的项目
            batch_kind="TRIAL",
            quantity=1,
            provider_id=env.provider_id,
            metrics={"calls": "1"},
        )
    assert caught.value.details["reason"] == "LIMITS_NOT_CONFIGURED"


def test_estimate_cost_without_metrics_needs_call_price(env: BillingEnv) -> None:
    """没给 `metrics` 时**只能按次计价**；价目缺 `CALL` ⇒ 422 `PRICING_INCOMPLETE`。

    本包**不自己数 token**（那会与供应商账单分叉），所以"50 篇要花多少"
    必须由调用方给出用量组成；只说"50 篇"时唯一能算的口径是 CALL。
    """
    _grant_batch_limits(env)
    _seed_pricing_version(
        env, unit_prices={"TOKEN": _PRICE_TOKEN, "IMAGE": _PRICE_IMAGE, "SECOND": _PRICE_SECOND}
    )
    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.estimate_cost(
            repo,
            project_id=env.project_id,
            batch_kind="TRIAL",
            quantity=3,
            provider_id=env.provider_id,
        )
    assert caught.value.details["reason"] == "PRICING_INCOMPLETE"

    # 有 CALL 时按 CALL × 数量
    _seed_pricing_version(
        env,
        label="含 CALL 的价目",
        unit_prices={
            "CALL": _PRICE_CALL,
            "TOKEN": _PRICE_TOKEN,
            "IMAGE": _PRICE_IMAGE,
            "SECOND": _PRICE_SECOND,
        },
    )
    with _repo(env) as repo:
        view = b_service.estimate_cost(
            repo, project_id=env.project_id, batch_kind="TRIAL", quantity=3, provider_id=env.provider_id
        )
    assert view.total_amount == Decimal("37.50000000")


def test_estimate_cost_ignores_draft_and_future_pricing(env: BillingEnv) -> None:
    """只有 `PUBLISHED` 且**已生效**的价目可用；`DRAFT` / 未来生效 ⇒ 404。

    `DRAFT` 是"录了一半"；未来生效的不能用 —— 两者都不该被静默选中。
    """
    _grant_batch_limits(env)
    assert USABLE_PRICING_STATES == frozenset({PRICING_STATE_PUBLISHED})

    _seed_pricing_version(env, state=PRICING_STATE_DRAFT)
    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.estimate_cost(
            repo, project_id=env.project_id, batch_kind="TRIAL", quantity=1,
            provider_id=env.provider_id, metrics={"calls": "1"},
        )
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert caught.value.details["reason"] == "PRICING_VERSION_NOT_FOUND"

    # 未来生效的也不行
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="0")
    _clear_pricing(env)
    _seed_pricing_version(
        env, effective_from=datetime.now(timezone.utc) + timedelta(days=30)
    )
    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.estimate_cost(
            repo, project_id=env.project_id, batch_kind="TRIAL", quantity=1,
            provider_id=env.provider_id, metrics={"calls": "1"},
        )
    assert caught.value.details["reason"] == "PRICING_VERSION_NOT_FOUND"


def _clear_pricing(env: BillingEnv) -> None:
    """清空计价版本**及其包内引用者**（不可变表 ⇒ 终点只能 TRUNCATE）。

    ## ⚠️ 名字只说了 1 张表，实际清 3 张（本机实测）

    `pricing_versions` 被 `cost_estimates` 引用，`cost_estimates` 又被
    `budget_reservations` 引用 —— 实测报错：

        ERROR: cannot truncate a table referenced in a foreign key constraint
        DETAIL: Table "budget_reservations" references "cost_estimates".

    PG 要求 `TRUNCATE` 把**整条引用链**一起列出，只写其中两张仍不够。
    所以这里一次列三张。

    ## 为什么这么写而不是 `TRUNCATE ... CASCADE`

    `CASCADE` 会静默多清"没被提到但恰好有依赖"的表 —— 在本包正好就是
    这三张，但将来多一条跨包引用时它会扩大范围且不报错。
    显式列出三张，将来引用链变了会**当场报 FK 错误**。

    ## 为什么不用 DELETE

    终点 `pricing_versions` 是不可变表（`trg_pricing_versions_immutable`，
    UPDATE/DELETE ⇒ `23001`），只有 TRUNCATE 能绕行级触发器。
    （`cost_estimates` / `budget_reservations` 本身可变，但既已在同一条
    TRUNCATE 语句里，就不必分开写了。）
    """
    _raw(
        env,
        "TRUNCATE budget_reservations, cost_estimates, pricing_versions",
    )


def test_estimate_cost_model_specific_beats_provider_wide(env: BillingEnv) -> None:
    """**模型级价目优先于供应商级**（同一时点两者都在时）。

    `find_usable_pricing_version` 的排序键是
    `model_profile_id IS NULL`（False 排前）+ `effective_from DESC`。
    """
    _grant_batch_limits(env)
    _seed_pricing_version(env, unit_prices={"CALL": "1.00000000", "TOKEN": _PRICE_TOKEN,
                                           "IMAGE": _PRICE_IMAGE, "SECOND": _PRICE_SECOND},
                          label="供应商级")
    _seed_pricing_version(
        env,
        model_profile_id=env.model_profile_id,
        unit_prices={"CALL": "9.00000000", "TOKEN": _PRICE_TOKEN,
                     "IMAGE": _PRICE_IMAGE, "SECOND": _PRICE_SECOND},
        label="模型级",
    )
    with _repo(env) as repo:
        view = b_service.estimate_cost(
            repo, project_id=env.project_id, batch_kind="TRIAL", quantity=1,
            provider_id=env.provider_id, model_profile_id=env.model_profile_id,
            metrics={"calls": "1"},
        )
    assert view.total_amount == Decimal("9.00000000"), "模型级价目必须胜出"


def test_list_project_reservations_and_estimates_are_project_scoped(
    env: BillingEnv,
) -> None:
    """两个 list 函数**只看本项目**（不串项目）。"""
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="100")
        b_service.set_budget(repo, project_id=env.other_project_id, limit_amount="100")
        b_service.reserve_budget(repo, project_id=env.project_id, amount="10")
        b_service.reserve_budget(repo, project_id=env.other_project_id, amount="20")

    with _repo(env) as repo:
        mine = b_service.list_project_reservations(repo, project_id=env.project_id)
        theirs = b_service.list_project_reservations(repo, project_id=env.other_project_id)
    assert len(mine) == 1 and mine[0].amount == Decimal("10.00000000")
    assert len(theirs) == 1 and theirs[0].amount == Decimal("20.00000000")


def test_list_project_reservations_filters_by_state(env: BillingEnv) -> None:
    """`states` 筛选走持有集合（`HELD` + `EXPIRED`）。"""
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="100")
        keep = b_service.reserve_budget(repo, project_id=env.project_id, amount="10")
        drop = b_service.reserve_budget(repo, project_id=env.project_id, amount="20")
        b_service.release_reservation(repo, reservation_id=drop.id)

    with _repo(env) as repo:
        holding = b_service.list_project_reservations(
            repo, project_id=env.project_id, states=sorted(HOLDING_RESERVATION_STATES)
        )
        all_rows = b_service.list_project_reservations(repo, project_id=env.project_id)
    assert [row.id for row in holding] == [keep.id]
    assert len(all_rows) == 2, "不传 states 要看到已释放的历史"


def test_list_project_ledger_orders_newest_first_and_filters_since(
    env: BillingEnv,
) -> None:
    """账本按发生时间**新→旧**，且 `since` 生效。"""
    with _repo(env) as repo:
        for i in range(3):
            b_service.append_ledger_entry(
                repo,
                project_id=env.project_id,
                entry=LedgerEntryInput(
                    entry_kind=LEDGER_KIND_ADJUSTMENT, amount=Decimal(str(i + 1))
                ),
            )
    with _repo(env) as repo:
        rows = b_service.list_project_ledger(repo, project_id=env.project_id)
    assert len(rows) == 3

    with _repo(env) as repo:
        filtered = b_service.list_project_ledger(
            repo, project_id=env.project_id, since=datetime.now(timezone.utc) + timedelta(minutes=5)
        )
    assert filtered == [], "`since` 在未来 ⇒ 空"


def test_get_estimate_and_list_estimates_round_trip(env: BillingEnv) -> None:
    """预估的读路径：`get_estimate` 按 id、`list_project_estimates` 按项目 + 状态。"""
    _grant_batch_limits(env)
    _seed_pricing_version(env)
    with _repo(env) as repo:
        created = b_service.estimate_cost(
            repo, project_id=env.project_id, batch_kind="TRIAL", quantity=2,
            provider_id=env.provider_id, metrics={"calls": "2"},
        )
    with _repo(env) as repo:
        fetched = b_service.get_estimate(repo, estimate_id=created.id)
        listed = b_service.list_project_estimates(
            repo, project_id=env.project_id, states=[ESTIMATE_STATE_DRAFT]
        )
    assert fetched.id == created.id
    assert fetched.total_amount == created.total_amount
    assert [row.id for row in listed] == [created.id]
    assert ESTIMATE_STATE_DRAFT in ESTIMATE_STATES

    with _repo(env) as repo, pytest.raises(ApiError) as caught:
        b_service.get_estimate(repo, estimate_id=uuid.uuid4())
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert caught.value.details["reason"] == "ESTIMATE_NOT_FOUND"


def test_list_pricing_versions_survives_corrupt_row(env: BillingEnv) -> None:
    """★ 一行坏数据**不能**让整个列表 500 —— 跳过 + `logger.warning`。

    `list_pricing_versions` 对每行调 `_to_pricing_lines`，而后者在
    "单价不是字符串"或"超出小数位"时抛 `ApiError`。实现里捕获它、
    记警告、跳过该行。

    ## 为什么这条判据重要（而不是"容错过度"）

    `pricing_versions` 是**不可变表** —— 一行坏数据删不掉、改不了。
    若列表因它整体 500，那个供应商的**所有**价目都对前端不可见，
    而唯一的修复手段是写一条新版本（旧行永远留在那里继续作恶）。
    跳过让"坏的那一份"可见地缺失，而不是让"全部"消失。
    """
    good = _seed_pricing_version(env, label="好的价目")
    # 原生 SQL 直插一行坏数据（`unit_prices` 的值是数字而不是字符串）
    _raw(
        env,
        "insert into pricing_versions (provider_id, state, currency, label,"
        " unit_prices, effective_from) values (:p, 'PUBLISHED', 'CNY', '坏价目',"
        " '{\"CALL\": 12.5}'::jsonb, now())",
        {"p": str(env.provider_id)},
    )

    with _repo(env) as repo:
        rows = b_service.list_pricing_versions(repo, provider_id=env.provider_id)
    assert [row.id for row in rows] == [good], "坏行必须被跳过，好的必须还在"


def test_reconciliation_detects_committed_ledger_drift(env: BillingEnv) -> None:
    """★ R10 对账判据：**账面 committed 与账本合计不等** ⇒ 报出 drift。

    ## 为什么要**人为造**一个分叉

    正常路径下两者永远相等（`settle_budget` 与 `_insert_ledger_entries`
    在同一事务里）。若只测"正常时 drift == 0"，这条判据**永远绿** ——
    它证明不了"对账真的会报警"，只能证明"没跑出异常"。
    因此这里用原生 SQL 直改账面（绕过 service），造出真实分叉，
    再断言对账**指出了它、且方向正确**。
    """
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="1000")
        view = b_service.reserve_budget(repo, project_id=env.project_id, amount="100")
        b_service.settle_reservation(repo, reservation_id=view.id, actual_amount="100")

    with _repo(env) as repo:
        clean = b_recon.reconcile_project(repo, env.project_id)
    assert len(clean) == 2, "永远返回两条（哪怕健康）"
    by_table = {report.table: report for report in clean}
    assert by_table[b_recon.TABLE_BUDGET_COMMITTED].drift == money.ZERO
    assert by_table[b_recon.TABLE_BUDGET_RESERVED].drift == money.ZERO

    # 绕过 service 把账面 committed 改大 50（`project_budgets` 是可变表）
    _raw(
        env,
        "update project_budgets set committed_amount = committed_amount + 50"
        " where project_id = :p",
        {"p": str(env.project_id)},
    )

    with _repo(env) as repo:
        dirty = b_recon.reconcile_project(repo, env.project_id)
    dirty_by_table = {report.table: report for report in dirty}
    committed = dirty_by_table[b_recon.TABLE_BUDGET_COMMITTED]
    assert committed.drift != money.ZERO, "对账必须报出这条分叉"
    assert committed.account_value == Decimal("150.00000000")
    assert committed.recomputed_value == Decimal("100.00000000")


def test_reconcile_all_projects_covers_ledger_only_projects(env: BillingEnv) -> None:
    """`list_budgeted_project_ids` 取"有账面行 ∪ 有账本明细"的并集。

    "账本里有费用、账面上查不到这个项目"是最该报警的一类分叉 ——
    只看 `project_budgets` 会把这类项目整个排除在判据之外。
    """
    # 只写账本，**不建账面行**
    _raw(
        env,
        "insert into usage_ledger (project_id, entry_kind, currency, amount, occurred_at)"
        " values (:p, 'BILL', 'CNY', 10, now())",
        {"p": str(env.other_project_id)},
    )
    with _repo(env) as repo:
        reports = b_recon.reconcile_all_projects(repo)
    targets = {report.project_id for report in reports}
    assert env.other_project_id in targets

    committed = [
        report
        for report in reports
        if report.project_id == env.other_project_id
        and report.table == b_recon.TABLE_BUDGET_COMMITTED
    ]
    assert len(committed) == 1
    assert committed[0].account_value == money.ZERO, "账面行不存在时取 0 而不是跳过"
    assert committed[0].recomputed_value == Decimal("10.00000000")


def test_reconciliation_report_identity_holds(env: BillingEnv) -> None:
    """报告自身的不变量：`scanned == resolved + len(pending)`，且 `truncated` 与 limit 一致。

    ## ⚠️ 必须按项目限定，不能扫全平台

    本机测试库里 `job_attempts` 有**别人的残留行**（M1/M2 累积，实测
    575 条 `usage_pending=true`）。不限定 `project_id` 的话
    `reconcile_pending(limit=10)` 会被它们填满，`truncated` 恒为 True ——
    这条判据就变成了在断言"这台机器的历史数据"，与实现无关。

    限定到本用例的项目后，本用例自己造 2 条 pending，`limit=10` 取不满
    ⇒ `truncated=False` 是**确定的**。
    """
    _seed_pending_attempts(env, count=2)
    with _repo(env) as repo:
        report = b_recon.reconcile_pending(
            repo, limit=10, project_id=env.project_id
        )
    assert report.scanned == 2, "本用例只造了 2 条 pending"
    assert report.scanned == report.resolved + len(report.pending)
    assert report.truncated is False, "2 < limit=10 ⇒ 不该判截断"

    # limit 小于实际条数 ⇒ truncated=True（"取满也算"的口径）
    with _repo(env) as repo:
        tight = b_recon.reconcile_pending(
            repo, limit=1, project_id=env.project_id
        )
    assert tight.scanned == 1
    assert tight.truncated is True, "取满 limit 即判截断（宁多报不少报）"


def test_ledger_summary_by_kind_and_net(env: BillingEnv) -> None:
    """`ledger_summary_body` 的分组与净额：`net` **不过滤符号**（冲正让净额变小）。"""
    with _repo(env) as repo:
        bill = b_service.append_ledger_entry(
            repo, project_id=env.project_id,
            entry=LedgerEntryInput(entry_kind=LEDGER_KIND_BILL, amount=Decimal("100")),
        )
        b_service.append_ledger_entry(
            repo, project_id=env.project_id,
            entry=LedgerEntryInput(entry_kind=LEDGER_KIND_USAGE, amount=Decimal("30"),
                                   quantity=Decimal("1000"), unit="TOKEN"),
        )
        b_service.append_ledger_entry(
            repo, project_id=env.project_id,
            entry=LedgerEntryInput(entry_kind=LEDGER_KIND_REVERSAL, amount=Decimal("-40"),
                                   reverses_ledger_id=bill.id),
        )

    with _repo(env) as repo:
        body = ledger_summary_body(repo, project_id=env.project_id, limit=100)
    assert body.entry_count == 3
    # ⚠️ `by_kind` / `net` 是 **wire 字符串**（`money.to_wire`），不是 Decimal。
    # 断言成 `Decimal(...)` 会报 `'100.00000000' == Decimal('100.00000000')`
    # —— pytest 的 repr 看起来两边一样，但类型不同，这条会红得让人困惑。
    assert body.by_kind[LEDGER_KIND_BILL] == "100.00000000"
    assert body.by_kind[LEDGER_KIND_USAGE] == "30.00000000"
    assert body.by_kind[LEDGER_KIND_REVERSAL] == "-40.00000000"
    assert body.net == "90.00000000"
    assert body.truncated is False
    # 数值口径也钉一下：字符串解析回来必须等于定点数（不是浮点近似）
    assert money.parse_wire_strict(body.net) == Decimal("90.00000000")


def test_ledger_summary_marks_truncated_when_limit_reached(env: BillingEnv) -> None:
    """`truncated` 在"取满 limit"时为 True —— 此时 `net` **不是全量净额**。"""
    with _repo(env) as repo:
        for i in range(3):
            b_service.append_ledger_entry(
                repo, project_id=env.project_id,
                entry=LedgerEntryInput(entry_kind=LEDGER_KIND_ADJUSTMENT, amount=Decimal(str(i + 1))),
            )
    with _repo(env) as repo:
        body = ledger_summary_body(repo, project_id=env.project_id, limit=3)
    assert body.truncated is True
    assert body.entry_count == 3


def test_repo_reserve_budget_returns_false_when_row_missing(env: BillingEnv) -> None:
    """`reserve_budget` 在**账面行不存在**时返回 False（不抛）。

    service 之所以必须先 `ensure_budget`：`False` 有两种成因
    （"行不存在"与"额度不足"），而它们该报不同的错。
    这条判据把"repository 层不区分"这件事钉住 ——
    若哪天有人在这里加一个"行不存在就建"，service 的 `ensure_budget` 步骤
    就变成了重复动作，而那条"必须先做"的注释会变成谎言。
    """
    with _repo(env) as repo:
        assert repo.reserve_budget(project_id=env.project_id, delta=Decimal("1")) is False


def test_repo_settle_budget_requires_reserved_release_available(env: BillingEnv) -> None:
    """`settle_budget` 的双检查：归还量不足 ⇒ False（不会把 `reserved` 扣成负数）。"""
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="100")
    with _repo(env) as repo:
        assert repo.settle_budget(
            project_id=env.project_id,
            reserved_release=Decimal("50"),
            committed_add=Decimal("10"),
        ) is False
        assert _budget_snapshot(env)["reserved"] == money.ZERO


def test_repo_release_budget_early_returns_true_for_non_positive_delta(
    env: BillingEnv,
) -> None:
    """`delta <= 0` 早返回 True（无事可做不算失败）。"""
    with _repo(env) as repo:
        assert repo.release_budget(project_id=env.project_id, delta=Decimal("0")) is True
        assert repo.release_budget(project_id=env.project_id, delta=Decimal("-1")) is True


def test_repo_transition_reservation_is_conditional(env: BillingEnv) -> None:
    """状态迁移是**条件 UPDATE**：`from_states` 不含当前状态 ⇒ False。"""
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount="100")
        view = b_service.reserve_budget(repo, project_id=env.project_id, amount="10")

    with _repo(env) as repo:
        # 当前是 HELD，却要求"从 RELEASED 出发" ⇒ False
        assert repo.transition_reservation(
            reservation_id=view.id, from_states=[RESERVATION_STATE_RELEASED],
            to_state=RESERVATION_STATE_SETTLED,
        ) is False
        assert repo.transition_reservation(
            reservation_id=view.id, from_states=sorted(HOLDING_RESERVATION_STATES),
            to_state=RESERVATION_STATE_RELEASED,
        ) is True


def test_repo_find_ledger_entry_by_idempotency_round_trip(env: BillingEnv) -> None:
    """幂等键回查能取到刚写入的那条。"""
    key = f"probe-{uuid.uuid4().hex[:12]}"
    with _repo(env) as repo:
        written = b_service.append_ledger_entry(
            repo, project_id=env.project_id,
            entry=LedgerEntryInput(entry_kind=LEDGER_KIND_ADJUSTMENT, amount=Decimal("5"),
                                   idempotency_key=key),
        )
    with _repo(env) as repo:
        found = repo.find_ledger_entry_by_idempotency(key)
    assert found is not None and found.id == written.id


# ===========================================================================
# 第 3 层：原生 SQL（绕过 ORM 与 service）
# ===========================================================================


def test_native_insert_bypassing_service_still_meets_within_limit(
    env: BillingEnv,
) -> None:
    """★ `within_limit` 是**恒真式**：它拦的是"绕过条件 UPDATE 的直写"。

    ## 这条判据的形态（MATERIAL.md 第 2 条纪律）

    `ck_project_budgets_within_limit`（`committed + reserved <= limit`）
    **不能**当作"预占防线"来测 —— 在它成立的前提下它总是成立。
    能证明它真的在位的只有一种做法：**直写一个超限的行**，看它是否拒。

    同时测"合法直写能过"，否则"全部直写都被拒"（例如列被写成只读）
    也会让上面那条绿。
    """
    # 合法直写（超限值恰好不触发）
    _raw(
        env,
        "insert into project_budgets (project_id, currency, limit_amount,"
        " committed_amount, reserved_amount) values (:p, 'CNY', 100, 60, 40)",
        {"p": str(env.project_id)},
    )
    assert _budget_snapshot(env)["reserved"] == Decimal("40.00000000")

    # 超限直写 ⇒ 撞 CHECK（`23514`），约束名点出是哪一条
    exc = _raw_expect_error(
        env,
        "update project_budgets set reserved_amount = 41 where project_id = :p",
        {"p": str(env.project_id)},
    )
    assert _sqlstate(exc) == "23514"
    assert _constraint_name(exc) == "ck_project_budgets_within_limit"
    # 账面未被改动（语句失败 ⇒ 整个事务回滚）
    assert _budget_snapshot(env)["reserved"] == Decimal("40.00000000")


def test_native_negative_amounts_rejected_by_check(env: BillingEnv) -> None:
    """`limit / committed / reserved` 三个**非负** CHECK 各拒一次。

    断言到**约束名**而不是只看 sqlstate：sqlstate 只说"某个 CHECK 拒了"，
    而这张表有五个 CHECK，报错指向哪一条才是有用信息。
    """
    # 先落一行"干净"的账面，随后逐列直写负值
    _raw(
        env,
        "insert into project_budgets (project_id, currency, limit_amount,"
        " committed_amount, reserved_amount) values (:p, 'CNY', 100, 0, 0)",
        {"p": str(env.project_id)},
    )
    for column, constraint in (
        ("limit_amount", "ck_project_budgets_limit_non_negative"),
        ("committed_amount", "ck_project_budgets_committed_non_negative"),
        ("reserved_amount", "ck_project_budgets_reserved_non_negative"),
    ):
        exc = _raw_expect_error(
            env,
            f"update project_budgets set {column} = -1 where project_id = :p",
            {"p": str(env.project_id)},
        )
        assert _sqlstate(exc) == "23514", f"{column} 的负值未被拒"
        assert _constraint_name(exc) == constraint, (
            f"{column} 报的是 {_constraint_name(exc)}，期望 {constraint}"
        )
        # 每轮失败都整体回滚 ⇒ 下一轮仍是干净账面（100/0/0）
        assert _budget_snapshot(env)["limit"] == Decimal("100.00000000")


def test_native_usage_ledger_amount_sign_must_match_kind(env: BillingEnv) -> None:
    """`ck_usage_ledger_amount_sign_matches_kind`：三类符号约束各拒一次。

    * `BILL` / `USAGE` 必须为正；
    * `REVERSAL` 必须为负；
    * `ADJUSTMENT` 只要求非零（正负都可以）。

    最后那条是本包一处刻意的**留白**：人工调整的方向由业务决定，
    数据库不替它表态 —— 但要挡住"金额为 0 的分录"（它不改变任何账）。
    """
    def insert(kind: str, amount: str, extra: str = "", params_extra: dict[str, Any] | None = None):
        return _raw_expect_error(
            env,
            "insert into usage_ledger (project_id, entry_kind, currency, amount,"
            f" occurred_at{extra}) values (:p, '{kind}', 'CNY', {amount}, now()"
            + (", :r" if "reverses_ledger_id" in extra else "")
            + ")",
            {"p": str(env.project_id), **(params_extra or {})},
        )

    assert _constraint_name(insert(LEDGER_KIND_BILL, "-1")) == (
        "ck_usage_ledger_amount_sign_matches_kind"
    )
    target = uuid.uuid4()
    _raw(
        env,
        "insert into usage_ledger (project_id, entry_kind, currency, amount,"
        " occurred_at, idempotency_key) values (:p, 'ADJUSTMENT', 'CNY', 1, now(), :k)",
        {"p": str(env.project_id), "k": f"target-{target.hex[:8]}"},
    )
    # ADJUSTMENT 的 0 元被拒（"非零"要求）
    exc = _raw_expect_error(
        env,
        "insert into usage_ledger (project_id, entry_kind, currency, amount, occurred_at)"
        " values (:p, 'ADJUSTMENT', 'CNY', 0, now())",
        {"p": str(env.project_id)},
    )
    assert _constraint_name(exc) == "ck_usage_ledger_amount_sign_matches_kind"


def test_native_usage_ledger_reversal_requires_target_and_not_self(
    env: BillingEnv,
) -> None:
    """`reversal_target_matches_kind` 与 `reversal_not_self` 两条 CHECK。"""
    # REVERSAL 没有 reverses_ledger_id
    exc = _raw_expect_error(
        env,
        "insert into usage_ledger (project_id, entry_kind, currency, amount, occurred_at)"
        " values (:p, 'REVERSAL', 'CNY', -1, now())",
        {"p": str(env.project_id)},
    )
    assert _constraint_name(exc) == "ck_usage_ledger_reversal_target_matches_kind"

    # 自引用（id == reverses_ledger_id）—— 预生成同一个 id
    row_id = uuid.uuid4()
    exc = _raw_expect_error(
        env,
        "insert into usage_ledger (id, project_id, entry_kind, currency, amount,"
        " occurred_at, reverses_ledger_id) values (:rid, :p, 'REVERSAL', 'CNY',"
        " -1, now(), :rid)",
        {"rid": str(row_id), "p": str(env.project_id)},
    )
    assert _constraint_name(exc) == "ck_usage_ledger_reversal_not_self"


def test_native_usage_ledger_usage_requires_quantity_and_pairs_unit(
    env: BillingEnv,
) -> None:
    """`usage_requires_quantity` 与 `quantity_unit_paired` 两条 CHECK。"""
    exc = _raw_expect_error(
        env,
        "insert into usage_ledger (project_id, entry_kind, currency, amount, occurred_at)"
        " values (:p, 'USAGE', 'CNY', 1, now())",
        {"p": str(env.project_id)},
    )
    assert _constraint_name(exc) == "ck_usage_ledger_usage_requires_quantity"

    # 只有 quantity 没有 unit
    exc = _raw_expect_error(
        env,
        "insert into usage_ledger (project_id, entry_kind, currency, amount, occurred_at,"
        " quantity) values (:p, 'BILL', 'CNY', 1, now(), 5)",
        {"p": str(env.project_id)},
    )
    assert _constraint_name(exc) == "ck_usage_ledger_quantity_unit_paired"

    # 只有 unit 没有 quantity
    exc = _raw_expect_error(
        env,
        "insert into usage_ledger (project_id, entry_kind, currency, amount, occurred_at,"
        " unit) values (:p, 'BILL', 'CNY', 1, now(), 'TOKEN')",
        {"p": str(env.project_id)},
    )
    assert _constraint_name(exc) == "ck_usage_ledger_quantity_unit_paired"

    # 合法：两者都给
    _raw(
        env,
        "insert into usage_ledger (project_id, entry_kind, currency, amount, occurred_at,"
        " quantity, unit) values (:p, 'USAGE', 'CNY', 1, now(), 5, 'TOKEN')",
        {"p": str(env.project_id)},
    )
    assert _scalar(
        env, "select count(*) from usage_ledger where project_id = :p", {"p": str(env.project_id)}
    ) == 1


def test_native_reservation_settled_amount_matches_state(env: BillingEnv) -> None:
    """`settled_amount_matches_state`：`SETTLED` 必须有金额，非 `SETTLED` 必须是 NULL。"""
    exc = _raw_expect_error(
        env,
        "insert into budget_reservations (project_id, state, currency, amount)"
        " values (:p, 'SETTLED', 'CNY', 10)",
        {"p": str(env.project_id)},
    )
    assert _constraint_name(exc) == "ck_budget_reservations_settled_amount_matches_state"

    exc = _raw_expect_error(
        env,
        "insert into budget_reservations (project_id, state, currency, amount, settled_amount)"
        " values (:p, 'HELD', 'CNY', 10, 5)",
        {"p": str(env.project_id)},
    )
    assert _constraint_name(exc) == "ck_budget_reservations_settled_amount_matches_state"

    # **金额 0 的已结算行必须能插**（检查是 `IS NOT NULL` 而不是 `> 0`）
    _raw(
        env,
        "insert into budget_reservations (project_id, state, currency, amount, settled_amount)"
        " values (:p, 'SETTLED', 'CNY', 10, 0)",
        {"p": str(env.project_id)},
    )
    assert _scalar(
        env,
        "select count(*) from budget_reservations where project_id = :p",
        {"p": str(env.project_id)},
    ) == 1


def test_native_reservation_amount_must_be_positive(env: BillingEnv) -> None:
    """`amount_positive`：0 与负数都被拒。"""
    for bad in ("0", "-1"):
        exc = _raw_expect_error(
            env,
            f"insert into budget_reservations (project_id, state, currency, amount)"
            f" values (:p, 'HELD', 'CNY', {bad})",
            {"p": str(env.project_id)},
        )
        assert _constraint_name(exc) == "ck_budget_reservations_amount_positive"


def test_native_cost_estimate_settled_requires_actual(env: BillingEnv) -> None:
    """`ck_cost_estimates_settled_requires_actual` 是**双向**约束。

    `SETTLED` ⇒ 两个金额都非空；非 `SETTLED` ⇒ 两个都为空。
    第二句比第一句更常被忽略：它挡住"记了实际金额但状态还是 DRAFT"
    ——那时对账会拿一个"尚未确认"的数字去比账本。
    """
    exc = _raw_expect_error(
        env,
        "insert into cost_estimates (project_id, batch_kind, quantity, currency,"
        " state, estimated_amount, lines) values (:p, 'TRIAL', 1, 'CNY', 'SETTLED',"
        " 10, '[]'::jsonb)",
        {"p": str(env.project_id)},
    )
    assert _constraint_name(exc) == "ck_cost_estimates_settled_requires_actual"

    exc = _raw_expect_error(
        env,
        "insert into cost_estimates (project_id, batch_kind, quantity, currency,"
        " state, estimated_amount, actual_amount, variance_amount, lines)"
        " values (:p, 'TRIAL', 1, 'CNY', 'DRAFT', 10, 8, -2, '[]'::jsonb)",
        {"p": str(env.project_id)},
    )
    assert _constraint_name(exc) == "ck_cost_estimates_settled_requires_actual"


def test_native_cost_estimate_quantity_must_be_positive(env: BillingEnv) -> None:
    """`ck_cost_estimates_quantity_positive`：0 篇的预算是无意义的。"""
    exc = _raw_expect_error(
        env,
        "insert into cost_estimates (project_id, batch_kind, quantity, currency,"
        " estimated_amount, lines) values (:p, 'TRIAL', 0, 'CNY', 0, '[]'::jsonb)",
        {"p": str(env.project_id)},
    )
    assert _constraint_name(exc) == "ck_cost_estimates_quantity_positive"


def test_native_usage_ledger_is_immutable(env: BillingEnv) -> None:
    """★ `usage_ledger` 的 UPDATE / DELETE 各撞 `23001`（R5 的结构基础）。

    ## 列名必须**真实存在**（本机实测踩到）

    `usage_ledger` **没有 `note` 列**（它的列是 `id / project_id / entry_kind /
    currency / amount / quantity / unit / reverses_ledger_id / source_ref /
    idempotency_key / description / occurred_at / created_at`）。
    若探针写成 `update usage_ledger set note = note`，会先撞 `42703`
    （列不存在）—— 触发器**根本没被走到**，却返回了一个非 `23001` 的值。
    那样判据会以"防线失效"的形式报红，而真实原因是列名写错。
    因此这里 UPDATE 的是真实存在的 `description`。
    """
    _raw(
        env,
        "insert into usage_ledger (project_id, entry_kind, currency, amount, occurred_at)"
        " values (:p, 'ADJUSTMENT', 'CNY', 1, now())",
        {"p": str(env.project_id)},
    )
    for sql in (
        "update usage_ledger set description = '改了' where project_id = :p",
        "delete from usage_ledger where project_id = :p",
    ):
        exc = _raw_expect_error(env, sql, {"p": str(env.project_id)})
        assert _sqlstate(exc) == IMMUTABLE_RECORD_SQLSTATE, (
            f"{sql!r} 未被不可变触发器拦截，sqlstate={_sqlstate(exc)}"
        )
    # 行还在（事务回滚）
    assert _scalar(
        env, "select count(*) from usage_ledger where project_id = :p", {"p": str(env.project_id)}
    ) == 1


def test_native_pricing_versions_is_immutable(env: BillingEnv) -> None:
    """★ `pricing_versions` 的 UPDATE / DELETE 各撞 `23001`。

    它**有** `note` 列，所以这里就用 `note`（与上一条形成对照：
    两张不可变表、列清单不同、断言同一 SQLSTATE）。
    """
    _raw(
        env,
        "insert into pricing_versions (provider_id, state, currency, label,"
        " unit_prices, effective_from) values (:pv, 'DRAFT', 'CNY', '待改',"
        " '{\"CALL\": \"1\"}'::jsonb, now())",
        {"pv": str(env.provider_id)},
    )
    for sql in (
        "update pricing_versions set note = '改了' where provider_id = :pv",
        "delete from pricing_versions where provider_id = :pv",
    ):
        exc = _raw_expect_error(env, sql, {"pv": str(env.provider_id)})
        assert _sqlstate(exc) == IMMUTABLE_RECORD_SQLSTATE, (
            f"{sql!r} 未被不可变触发器拦截，sqlstate={_sqlstate(exc)}"
        )
    assert _scalar(
        env,
        "select count(*) from pricing_versions where provider_id = :pv",
        {"pv": str(env.provider_id)},
    ) == 1


def test_native_partial_unique_index_is_live_both_ways(env: BillingEnv) -> None:
    """★ 部分唯一索引**双向活性**：NULL 可重复 + 非 NULL 重复撞 `23505`。

    索引 `uq_usage_ledger_idempotency` 的 `WHERE idempotency_key IS NOT NULL`。
    两个方向都必须测：

      * **正向**（NULL 可重复）—— 证明索引**没有**误伤人工分录
        （人工调整没有天然幂等键，勉强生成一个会掩盖真正的重复）；
      * **反向**（非 NULL 重复撞 `23505`）—— 证明索引**确实会报**。
        若它静默跳过（例如变成 `ON CONFLICT DO NOTHING` 的语义），
        `append_ledger_entry` 的幂等回查就失去了结构基础，
        而重试会写出两笔重复的钱。

    只做正向会留下"索引根本没生效"的可能；只做反向则不知道 NULL 是否被误伤。

    ## 为什么必须查 `pg_indexes` 而不是 `pg_constraint`

    部分唯一索引**不在** `pg_constraint` 里（它不是约束，只是索引）。
    用 `pg_constraint` 找它会得到空集，于是"没找到"与"没有索引"同形。
    """
    # 结构断言：确认它真的是一条**部分**唯一索引（`WHERE` 子句在 indexdef 里）
    indexdef = _scalar(
        env,
        "select indexdef from pg_indexes where schemaname = 'public'"
        " and indexname = 'uq_usage_ledger_idempotency'",
    )
    assert indexdef is not None, "部分唯一索引不存在 —— 幂等回查失去结构基础"
    assert "UNIQUE INDEX" in indexdef.upper()
    assert "WHERE (idempotency_key IS NOT NULL)" in indexdef, indexdef

    # 正向：两条 NULL 能插
    _raw(
        env,
        "insert into usage_ledger (project_id, entry_kind, currency, amount, occurred_at)"
        " values (:p, 'ADJUSTMENT', 'CNY', 1, now()), (:p, 'ADJUSTMENT', 'CNY', 2, now())",
        {"p": str(env.project_id)},
    )
    assert _scalar(
        env, "select count(*) from usage_ledger where project_id = :p", {"p": str(env.project_id)}
    ) == 2

    # 反向：重复的非 NULL 幂等键必须撞 23505
    key = f"dup-{uuid.uuid4().hex[:10]}"
    _raw(
        env,
        "insert into usage_ledger (project_id, entry_kind, currency, amount, occurred_at,"
        " idempotency_key) values (:p, 'ADJUSTMENT', 'CNY', 3, now(), :k)",
        {"p": str(env.project_id), "k": key},
    )
    exc = _raw_expect_error(
        env,
        "insert into usage_ledger (project_id, entry_kind, currency, amount, occurred_at,"
        " idempotency_key) values (:p, 'ADJUSTMENT', 'CNY', 4, now(), :k)",
        {"p": str(env.project_id), "k": key},
    )
    assert _sqlstate(exc) == b_ddl.UNIQUE_VIOLATION_SQLSTATE
    assert _constraint_name(exc) == "uq_usage_ledger_idempotency"


def test_native_fk_prevents_dangling_project_reference(env: BillingEnv) -> None:
    """绕过 service 直插**不存在的项目** ⇒ 撞 FK（`23503`）。

    service 层有 `_require_project`（→404），但那是"人话层"。
    结构层的防线是 FK —— 它必须真的在位，否则一个 bug 能造出
    "有费用、但不属于任何项目"的账本行。
    """
    exc = _raw_expect_error(
        env,
        "insert into project_budgets (project_id, currency, limit_amount,"
        " committed_amount, reserved_amount) values (:p, 'CNY', 100, 0, 0)",
        {"p": str(uuid.uuid4())},
    )
    assert _sqlstate(exc) == "23503"


# ===========================================================================
# 第 4 层：HTTP 接线（TestClient）
# ===========================================================================
#
# 本层守的是**端点组装**：路径、方法、状态码、鉴权依赖链。
# 一个"端点漏了 `access.require`"的缺陷会让前三层全部通过 ——
# 而那是一个**越权**缺陷。
#
# ## 本层的三个实测约束
#
# 1. **`PUT .../budget` 对 OPERATOR 返回 403**：
#    `_ORG_MATRIX[ROLE_OPERATOR]["MANAGE_BUDGET"] is False`（逐行实测）。
#    该端点**不带** `ProjectAccessDep`，只带 `BUDGET_WRITE`
#    （`require_org_action(OrgAction.MANAGE_BUDGET)`）—— 于是运营即使
#    是项目 owner 也会被挡。这是**刻意**的：额度是组织级决策。
# 2. **非成员访问项目端点返回 404 而不是 403**：
#    `resolve_project_access` 刻意这么设计（防项目存在性枚举）。
# 3. **`GET /pricing-versions` 与 `POST /billing/reconciliations` 只要登录**：
#    它们没有项目上下文（价目是供应商级的），因此没有项目级鉴权。

API_PREFIX = "/api/v1"


@pytest.fixture
def app(env: BillingEnv) -> FastAPI:
    """按注入配置装配 app，并**在 `main.py` 尚未接线时**补挂本模块 router。

    **挂载必须幂等**：`main.py` 第 364 行已 `include_router(billing_router, ...)`，
    此时再挂一次会让每条路由**重复注册**（WP20 收口时实测：路由数翻倍）。

    探测依据用**路径前缀**而不是 router 对象身份：`create_app` 内部 import 的
    是同一个模块级 `router`，但 `include_router` 会把它**复制成新对象**，
    身份比较不可靠。
    """
    from rednote.main import create_app

    assert env.settings.api_prefix == API_PREFIX, (
        f"本文件写死了 {API_PREFIX!r}，注入配置给的是 "
        f"{env.settings.api_prefix!r} —— 两者必须一致"
    )

    application = create_app(env.settings)

    mounted_path = f"{API_PREFIX}/projects/{{projectId}}/budget"
    already_mounted = any(
        getattr(route, "path", "") == mounted_path for route in application.routes
    )
    if not already_mounted:
        from rednote.modules.billing.api import router as billing_router

        application.include_router(billing_router, prefix=env.settings.api_prefix)
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, env: BillingEnv, *, who: str = "owner") -> None:
    """造一条会话并写 Cookie（含 CSRF 令牌）。

    ## 为什么 CSRF 必须一起处理（不能绕过）

    `current_user` 依赖对**写方法**强制 `verify_csrf`，且它是**结构上无法
    绕过**的。绕过它会让本层不再验证真实的鉴权链 —— 那时"401/403 断言"
    就成了对一段假鉴权的断言。

    ## `who` 的三档

    | `who` | 用户 | 本层用途 |
    |---|---|---|
    | `"owner"` | `ROLE_OPERATOR`，项目成员 | 正常路径 200/201 |
    | `"admin"` | `ROLE_ADMIN`，**非成员** | `MANAGE_BUDGET` 的唯一持有者 |
    | `"outsider"` | `ROLE_OPERATOR`，**非成员** | 404（防枚举） |
    """
    from rednote.platform.csrf import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
    from rednote.platform.tokens import generate_session_token, hash_token

    user_id = {
        "owner": env.owner_id,
        "admin": env.admin_id,
        "outsider": env.outsider_id,
    }[who]
    raw_token = generate_session_token()
    with session_scope(env.settings) as session:
        IdentityRepository(session).create_session(
            user_id=user_id,
            token_hash=hash_token(raw_token),
            csrf_secret_hash=hash_token(CSRF_SECRET_PLAINTEXT),
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    client.cookies.set(CSRF_COOKIE_NAME, CSRF_SECRET_PLAINTEXT)


def _write_headers(**extra: str) -> dict[str, str]:
    """写请求的公共头：CSRF 令牌 + Origin。

    两者都带上，本层的 403 断言才只可能来自**权限**判定
    （否则 403 可能来自 CSRF，而那是环境问题，不是权限问题）。
    """
    headers = {"X-CSRF-Token": CSRF_SECRET_PLAINTEXT, "Origin": TEST_ORIGIN}
    headers.update(extra)
    return headers


def _budget_url(env: BillingEnv, *, project_id: uuid.UUID | None = None) -> str:
    return f"{API_PREFIX}/projects/{project_id or env.project_id}/budget"


def _reservations_url(env: BillingEnv, *, project_id: uuid.UUID | None = None) -> str:
    return f"{API_PREFIX}/projects/{project_id or env.project_id}/budget-reservations"


def _ledger_url(env: BillingEnv, *, project_id: uuid.UUID | None = None) -> str:
    return f"{API_PREFIX}/projects/{project_id or env.project_id}/ledger-entries"


def _estimates_url(env: BillingEnv, *, project_id: uuid.UUID | None = None) -> str:
    return f"{API_PREFIX}/projects/{project_id or env.project_id}/cost-estimates"


def _seed_http_budget(env: BillingEnv, *, amount: str = "1000") -> None:
    """给 HTTP 用例铺一个预算（走 service，不是 HTTP —— 减少用例间的耦）."""
    with _repo(env) as repo:
        b_service.set_budget(repo, project_id=env.project_id, limit_amount=amount)


@pytest.mark.regression
def test_router_exposes_exactly_fourteen_endpoints(app: FastAPI) -> None:
    """本模块**恰好注册 14 个端点**。

    多一个少一个都会让 `api.py` docstring 里那张 14 行表失去可核对性 ——
    而那张表是本包对协议的唯一形状声明。

    ## 判据范围：只数**本包 router 自己**声明的操作

    刻意**不数 `app.routes` 里所有相关路径**。两个理由：

      * `/projects/{projectId}/...` 是**共用前缀**（几乎所有业务包都有），
        按 `app.routes` 计数会在别的包合法加端点时变红；
      * `POST /billing/reconciliations` 的 `/billing` 段也可能被别人用。

    因此分两句各自成立：

      1. **本包 router 自己**恰好 14 个操作（本包的事实，稳定）；
      2. 14 个全都真的挂在 `app` 上（接线正确，允许别人加端点）。

    第 1 句用 `router.routes` 而不是 `app.routes`，这是本项目
    ISS-44/64/71/83/87 的重复教训：**判据只能断言"我建的还在"，
    不能断言"全项目恰好有 N 个"**。
    """
    from rednote.modules.billing.api import router as billing_router

    own_operations = {
        (route.path, method)
        for route in billing_router.routes
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }
    expected = {
        ("/projects/{projectId}/budget", "GET"),
        ("/projects/{projectId}/budget", "PUT"),
        ("/projects/{projectId}/budget-reservations", "GET"),
        ("/projects/{projectId}/budget-reservations", "POST"),
        ("/budget-reservations/{reservationId}/releases", "POST"),
        ("/budget-reservations/{reservationId}/settlements", "POST"),
        ("/projects/{projectId}/ledger-entries", "GET"),
        ("/projects/{projectId}/ledger-summary", "GET"),
        ("/projects/{projectId}/ledger-entries", "POST"),
        ("/projects/{projectId}/cost-estimates", "GET"),
        ("/projects/{projectId}/cost-estimates", "POST"),
        ("/cost-estimates/{estimateId}", "GET"),
        ("/pricing-versions", "GET"),
        ("/billing/reconciliations", "POST"),
    }
    assert own_operations == expected, (
        f"多出：{sorted(own_operations - expected)}；"
        f"缺少：{sorted(expected - own_operations)}"
    )
    assert len(expected) == 14

    mounted = {
        (route.path, method)
        for route in app.routes
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }
    missing = {(f"{API_PREFIX}{path}", method) for path, method in expected} - mounted
    assert not missing, f"本包的端点在 app 里找不到：{sorted(missing)}"


def test_create_reservation_returns_201_and_persists(
    client: TestClient, env: BillingEnv
) -> None:
    """★ `POST .../budget-reservations` 的成功码是 **201**（不是 200）。

    三处 201（`create_reservation` / `append_ledger_entry` / `create_estimate`）
    与其余 11 处 200 的分界是"本次是否**创建了一个新资源**"。
    这条区分对前端有意义：201 的响应体是"刚创建的那个东西"，
    前端可以直接把它塞进列表而不必再查一次。
    """
    _login(client, env, who="owner")
    _seed_http_budget(env)

    response = client.post(
        _reservations_url(env),
        json={"amount": "100.00000000", "ref": "http-1"},
        headers=_write_headers(),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"data", "meta"}
    data = body["data"]
    assert data["state"] == RESERVATION_STATE_HELD
    assert data["amount"] == "100.00000000"
    assert data["projectId"] == str(env.project_id)
    assert data["ref"] == "http-1"
    assert "requestId" in body["meta"]


def test_get_budget_returns_four_values(client: TestClient, env: BillingEnv) -> None:
    """`GET .../budget` 的四值与别名（camelCase）都对。"""
    _login(client, env, who="owner")
    _seed_http_budget(env, amount="500")

    response = client.get(_budget_url(env))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["projectId"] == str(env.project_id)
    assert data["limit"] == "500.00000000"
    assert data["committed"] == "0.00000000"
    assert data["reserved"] == "0.00000000"
    assert data["available"] == "500.00000000"
    assert data["configured"] is True


def test_put_budget_allows_admin(client: TestClient, env: BillingEnv) -> None:
    """★ `PUT .../budget`：`ROLE_ADMIN` ⇒ 200（`MANAGE_BUDGET` 的唯一持有者）。"""
    _login(client, env, who="admin")
    response = client.put(
        _budget_url(env),
        json={"limitAmount": "800.00000000"},
        headers=_write_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["limit"] == "800.00000000"


def test_put_budget_rejects_operator_with_403(client: TestClient, env: BillingEnv) -> None:
    """★ `PUT .../budget`：`ROLE_OPERATOR` ⇒ **403**。

    `_ORG_MATRIX[ROLE_OPERATOR]["MANAGE_BUDGET"] is False`（逐行读源码实测）。
    该端点**不带** `ProjectAccessDep` —— 只带
    `require_org_action(OrgAction.MANAGE_BUDGET)`。于是**项目 owner
    也拿不到这个权限**，这是刻意的：额度是组织级决策。

    ## 这条判据防的是什么

    若哪天有人"顺手"把 `BUDGET_WRITE` 换成 `ProjectAccessDep`（看起来更
    一致），运营就能给自己所在项目设额度了 —— 而这是一个**越权**缺陷，
    且它的表现是"功能正常"（200 而不是 403），没有任何报错。
    """
    _login(client, env, who="owner")  # owner 恰好也是 OPERATOR
    response = client.put(
        _budget_url(env),
        json={"limitAmount": "800.00000000"},
        headers=_write_headers(),
    )
    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["code"] == ErrorCode.PERMISSION_DENIED.value
    assert error["details"]["action"] == "MANAGE_BUDGET"
    assert error["details"]["currentRole"] == ROLE_OPERATOR


def test_put_budget_requires_login(client: TestClient, env: BillingEnv) -> None:
    """未登录 ⇒ 401（`CurrentUserDep` 的那一层）。"""
    response = client.put(
        _budget_url(env),
        json={"limitAmount": "1"},
        headers=_write_headers(),
    )
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == ErrorCode.SESSION_EXPIRED.value


def test_release_and_settle_endpoints_round_trip(
    client: TestClient, env: BillingEnv
) -> None:
    """两条子资源端点（`releases` / `settlements`）的成功路径。

    它们**先读资源再判权限**（要从 `reservation_id` 反查 `project_id`）
    —— 这条顺序对"防枚举"很关键：若先判权限，一个不存在的 id
    会得到 404，而"存在但无权"会得到 403，两者可被区分。
    """
    _login(client, env, who="owner")
    _seed_http_budget(env)

    created = client.post(
        _reservations_url(env), json={"amount": "50.00000000"}, headers=_write_headers()
    )
    assert created.status_code == 201, created.text
    reservation_id = created.json()["data"]["id"]

    settled = client.post(
        f"{API_PREFIX}/budget-reservations/{reservation_id}/settlements",
        json={"actualAmount": "42.00000000"},
        headers=_write_headers(),
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["data"]["state"] == RESERVATION_STATE_SETTLED
    assert settled.json()["data"]["settledAmount"] == "42.00000000"

    # 再释放一次 ⇒ 409（已结算）
    released = client.post(
        f"{API_PREFIX}/budget-reservations/{reservation_id}/releases",
        json={},
        headers=_write_headers(),
    )
    assert released.status_code == 409, released.text
    assert released.json()["error"]["code"] == ErrorCode.INVALID_TRANSITION.value


def test_release_endpoint_accepts_empty_body(
    client: TestClient, env: BillingEnv
) -> None:
    """`releases` 的 body 是**可选**的（释放是补偿动作，原因可留空）。

    这条守的是"默认值 `ReleaseReservationBody()` 还能不能生效" ——
    若哪天把 body 改成必填，补偿路径会在**最需要它的时候**（失败重试）
    因为缺一个原因字段而 422。
    """
    _login(client, env, who="owner")
    _seed_http_budget(env)
    created = client.post(
        _reservations_url(env), json={"amount": "20.00000000"}, headers=_write_headers()
    )
    reservation_id = created.json()["data"]["id"]

    # 完全不带 body
    response = client.post(
        f"{API_PREFIX}/budget-reservations/{reservation_id}/releases",
        headers=_write_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["state"] == RESERVATION_STATE_RELEASED


def test_append_ledger_entry_returns_201_and_validates_kind(
    client: TestClient, env: BillingEnv
) -> None:
    """`POST .../ledger-entries` ⇒ 201；`entryKind` 只接受 `ADJUSTMENT`/`REVERSAL`。

    `BILL` / `USAGE` 不能从这里写 —— 它们必须由"结算"这种**有业务语义的
    路径**产生。允许手写一条 `BILL` 等于让人可以凭空增加已结算金额，
    而账本里那条分录没有任何事件与之对应。
    """
    _login(client, env, who="owner")
    response = client.post(
        _ledger_url(env),
        json={"entryKind": "ADJUSTMENT", "amount": "12.00000000", "description": "手工调整"},
        headers=_write_headers(),
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["entryKind"] == LEDGER_KIND_ADJUSTMENT
    assert data["amount"] == "12.00000000"
    assert data["description"] == "手工调整"

    # BILL 被 schema 层挡（Literal 只允许两个值）
    rejected = client.post(
        _ledger_url(env),
        json={"entryKind": "BILL", "amount": "1.00000000"},
        headers=_write_headers(),
    )
    assert rejected.status_code == 422, rejected.text


def test_ledger_summary_endpoint(client: TestClient, env: BillingEnv) -> None:
    """`GET .../ledger-summary` 的分组与净额。"""
    _login(client, env, who="owner")
    client.post(
        _ledger_url(env),
        json={"entryKind": "ADJUSTMENT", "amount": "10.00000000"},
        headers=_write_headers(),
    )
    response = client.get(f"{API_PREFIX}/projects/{env.project_id}/ledger-summary")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["entryCount"] == 1
    assert data["net"] == "10.00000000"
    assert data["truncated"] is False


def test_create_estimate_returns_201(client: TestClient, env: BillingEnv) -> None:
    """`POST .../cost-estimates` ⇒ 201，且响应体带 `totalAmount` 与 `lines`。"""
    _login(client, env, who="owner")
    _grant_batch_limits(env)
    _seed_pricing_version(env)

    response = client.post(
        _estimates_url(env),
        json={
            "batchKind": "TRIAL",
            "quantity": 5,
            "providerId": str(env.provider_id),
            "metrics": {"calls": "5"},
        },
        headers=_write_headers(),
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["batchKind"] == "TRIAL"
    assert data["quantity"] == 5
    assert data["totalAmount"] == "62.50000000"
    assert data["pricingVersionId"] is not None
    assert len(data["lines"]) == 1
    assert data["lines"][0]["unit"] == "CALL"


def test_estimate_budget_exceeded_is_422_not_retryable(
    client: TestClient, env: BillingEnv
) -> None:
    """超限 ⇒ **422** `BUDGET_EXCEEDED`，且 `retryable == False`。

    `retryable` 是给前端重试策略用的。把它写成 `true` 会让客户端
    无限重试一个**永远不会成功**的请求（额度不会因为重试而变大）。
    `_RETRYABLE_BY_CODE` 只对 429/503 置真。
    """
    _login(client, env, who="owner")
    _seed_http_budget(env, amount="10")
    response = client.post(
        _reservations_url(env), json={"amount": "50.00000000"}, headers=_write_headers()
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == ErrorCode.BUDGET_EXCEEDED.value
    assert error["retryable"] is False
    assert error["details"]["reason"] == REASON_BUDGET_EXCEEDED


def test_unknown_project_returns_404_via_http(client: TestClient, env: BillingEnv) -> None:
    """不存在的项目 ⇒ 404（`ProjectAccessDep` 的 `resolve_project_access`）。"""
    _login(client, env, who="admin")
    response = client.get(f"{API_PREFIX}/projects/{uuid.uuid4()}/budget")
    assert response.status_code == 404, response.text


def test_outsider_gets_404_not_403_on_project_read(
    client: TestClient, env: BillingEnv
) -> None:
    """★ 非成员读项目预算 ⇒ **404**（不是 403）。

    这是 `resolve_project_access` 的**刻意**设计：403 会告诉攻击者
    "这个项目存在，只是你无权" —— 于是可以用它枚举项目 id。
    404 让"无权访问"与"项目不存在"**不可区分**。

    因此本用例的断言里 404 是**正确行为**，不是"权限没生效"。
    """
    _login(client, env, who="outsider")
    response = client.get(_budget_url(env))
    assert response.status_code == 404, response.text


def test_admin_non_member_can_read_project_budget(
    client: TestClient, env: BillingEnv
) -> None:
    """★ 与上一条**成对**：`ADMIN` 也不是项目成员，但**能**读。

    两条合起来才测出"管理者全权"这条规则。若把两者合成
    "非成员应被拒"，这条规则就消失了 —— 而它是本仓的权限模型的核心。
    """
    _login(client, env, who="admin")
    response = client.get(_budget_url(env))
    assert response.status_code == 200, response.text


def test_read_endpoints_require_login(client: TestClient, env: BillingEnv) -> None:
    """★ 未登录读各端点 ⇒ 401（守"依赖链有没有装"）。

    若某天有人把 `access: ProjectAccessDep` 换成直接读路径参数，
    未登录就会走到"项目不存在 ⇒ 404"，而 404 与 401 的
    **客户端行为完全不同**（401 让前端跳登录，404 让前端显示"项目不见了"）。
    """
    for url in (
        _budget_url(env),
        _reservations_url(env),
        _ledger_url(env),
        f"{API_PREFIX}/projects/{env.project_id}/ledger-summary",
        _estimates_url(env),
    ):
        response = client.get(url)
        assert response.status_code == 401, f"{url} 未要求登录：{response.status_code}"
        assert response.json()["error"]["code"] == ErrorCode.SESSION_EXPIRED.value


def test_pricing_versions_endpoint_needs_login_only(
    client: TestClient, env: BillingEnv
) -> None:
    """`GET /pricing-versions` 只要登录 —— 价目是**供应商级**，没有项目上下文。

    它用 `del user` 显式标注"登录了但不看项目"。
    两个断言：未登录 401；登录后 200（且能看到刚造的版本）。
    """
    unauth = client.get(f"{API_PREFIX}/pricing-versions")
    assert unauth.status_code == 401, unauth.text

    _login(client, env, who="outsider")  # 非成员也能看价目
    _seed_pricing_version(env, label="HTTP 价目")
    response = client.get(f"{API_PREFIX}/pricing-versions", params={"providerId": str(env.provider_id)})
    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    assert len(rows) == 1
    assert rows[0]["providerId"] == str(env.provider_id)
    assert rows[0]["label"] == "HTTP 价目"


def test_reconciliation_endpoint_needs_login_only(
    client: TestClient, env: BillingEnv
) -> None:
    """`POST /billing/reconciliations` 只要登录；报告结构完整。

    ## `POST` 必须带 CSRF 头与 `Origin`（本包唯一一条"无 body 的写方法"）

    `POST` 走 CSRF 校验（`platform/csrf.py`），缺 `Origin`/`Referer`
    会抛 `PERMISSION_DENIED: missing_origin_and_referer`（403）。
    这条 403 与"权限不足"的 403 **同一个状态码**，因此这里必须把
    CSRF 头带全，否则 403 断言的含义会漂成"CSRF 没配好"。
    """
    unauth = client.post(f"{API_PREFIX}/billing/reconciliations")
    assert unauth.status_code == 401, unauth.text

    _login(client, env, who="outsider")
    response = client.post(
        f"{API_PREFIX}/billing/reconciliations", headers=_write_headers()
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    for key in ("scanned", "resolved", "pending", "pendingCount", "drifts", "truncated", "healthy"):
        assert key in data, f"对账报告缺 {key}"
    assert data["scanned"] == data["resolved"] + data["pendingCount"]

    # 少了 Origin ⇒ 403，且 reason 是 CSRF 而不是权限
    _login(client, env, who="outsider")
    no_origin = client.post(
        f"{API_PREFIX}/billing/reconciliations",
        headers={"X-CSRF-Token": CSRF_SECRET_PLAINTEXT},
    )
    assert no_origin.status_code == 403, no_origin.text
    assert (
        no_origin.json()["error"]["details"]["reason"] == "missing_origin_and_referer"
    ), "这条 403 必须来自 CSRF，而不是权限判定"


def test_http_list_endpoints_return_data_and_meta(
    client: TestClient, env: BillingEnv
) -> None:
    """四个 list 端点都返回 `{"data": [...], "meta": {...}}`（协议 §1 的形状）。"""
    _login(client, env, who="owner")
    _seed_http_budget(env)
    client.post(_reservations_url(env), json={"amount": "5.00000000"}, headers=_write_headers())

    for url in (
        _reservations_url(env),
        _ledger_url(env),
        _estimates_url(env),
        f"{API_PREFIX}/pricing-versions",
    ):
        response = client.get(url)
        assert response.status_code == 200, f"{url}: {response.text}"
        body = response.json()
        assert set(body) == {"data", "meta"}, f"{url} 的形状不对：{sorted(body)}"
        assert isinstance(body["data"], list)
        assert "requestId" in body["meta"]


def test_http_query_limits_are_bounded(client: TestClient, env: BillingEnv) -> None:
    """`limit` 的边界由 `Query(ge=1, le=100)` 在**框架层**校验。"""
    _login(client, env, who="owner")
    for bad in (0, 101, -1):
        response = client.get(_reservations_url(env), params={"limit": bad})
        assert response.status_code == 422, f"limit={bad} 未被拒：{response.status_code}"

    # 合法边界
    assert client.get(_reservations_url(env), params={"limit": 1}).status_code == 200
    assert client.get(_reservations_url(env), params={"limit": 100}).status_code == 200
