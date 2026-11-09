"""WP22（信息审核与基线）的四层测试。

对应 `superpower-workflow` 阶段 6。与 `test_prompts_wp23.py` /
`test_brief_wp18.py` / `test_knowledge_wp20.py` 同一范式。

## 四层各测什么（以及为什么不能合并）

| 层 | 测什么 | 手段 | 为什么单独一层 |
|---|---|---|---|
| 1 | 纯函数：受控取值、`readiness` 判据、DDL 文本自洽 | 直接调用，**不碰库** | 快；且 DDL 的正确性不该依赖"库能不能连上" |
| 2 | 服务层编排：七步链路、部分更新语义、批准三情形 | 真库 + `service_*` 函数 | 排序/事务语义只有在真事务里才成立 |
| 3 | **数据库约束自己会不会拦** | 原生 SQL（绕过 ORM 与 service） | 检查"绕过本层的写入"；若测试也只用 ORM，测试与实现共享同一个盲区 |
| 4 | HTTP 接线：路径、鉴权、ETag、状态码 | `TestClient` | 端点组装是独立的一层，服务层全绿也可能挂错前缀 |

## 与 `ddl.py` 判据的对应关系

第 1 层的 DDL 文本判据**与迁移同源**：迁移与测试都调
`ddl.all_trigger_ddl()`，不各写一遍。这样"触发器 DDL 改了但迁移没跟"
会在测试里直接暴露（本包的一条既有纪律）。

## 五处 Code Review MAJOR 的回归（"先红后绿"）

`evidence/m1/wp22-code-review.log` 的 MAJOR-1/2/3 在本文件里各有一条
**期望修复后行为**的判据，第 2 层实测又发现了两条（MAJOR-4/5）：

* MAJOR-1：`briefVersionId` 不校验版本类型 → 期望 **422**
  （`test_create_baseline_rejects_non_brief_version_node`）
* MAJOR-2：同 payload 幂等重投 → 期望 `created=False`，不是裸 23505
  （`test_create_baseline_is_idempotent_for_same_payload`）
* MAJOR-3：部分更新把 `briefVersionId` 静默清空 → 期望 **保留**
  （`test_partial_update_preserves_brief_version_id`）
* MAJOR-4：`newest_baseline_of` 的 `NOT EXISTS` 子查询被自动关联成
  同行比较 → 恒返回链根，`rebuild_active_baseline` 与增量路径不一致
  （`test_rebuild_active_baseline_matches_incremental_path`）
* MAJOR-5：三处 `record_audit` 只传 `after` 不传 `before` → 撞
  `ck_audit_events_hash_pair_consistent`，**三个写端点全 500**
  （`test_write_endpoints_survive_request_id`）

这五条判据在修复前会 **FAIL**（这正是它们存在的意义：钉住修复）。
**MAJOR-5 是本包最严重的一条** —— 它不是"少写一条审计"，而是
"写端点直接不可用"，且因为第 2 层此前一律走 `request_id=None`
（不写审计），它从未被任何测试覆盖到。

## 关于延迟约束触发器的取判定方式

本包三条不可变表**物理删不掉**（`rednote_reject_mutation()` 拦 DELETE），
因此第 3 层统一用 `SET CONSTRAINTS ALL IMMEDIATE` 把判定提前到事务内，
拿到判定后无条件 rollback（详见 `_assert_deferred_blocked`）。
"""

from __future__ import annotations

import ast
import dataclasses
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from conftest import cross_package_foreign_keys, drop_own_tables

from rednote.modules.baseline import baseline_types as bt
from rednote.modules.baseline import context_order as co
from rednote.modules.baseline import ddl as bs_ddl
from rednote.modules.baseline import readiness as rd
from rednote.modules.baseline import service as bs
from rednote.modules.baseline.baseline_types import (
    APPROVAL_DECISION_APPROVED,
    APPROVAL_DECISION_REJECTED,
    BASELINE_SCHEMA_VERSION,
    CLAIM_STATE_CONFIRMED,
    CLAIM_STATE_PENDING,
    CONFLICT_ACTION_ADOPT_ONE,
    CONFLICT_KIND_SOURCE,
    CONFLICT_STATE_OPEN,
    CONFLICT_STATE_SETTLED,
    INCLUSION_EXCLUDED,
    INCLUSION_INCLUDED,
    ITEM_KIND_FACT,
    ITEM_KIND_RESEARCH,
    ITEM_KIND_SCENE,
    ITEM_KINDS,
    MAX_CHANGE_REASON_CHARS,
    MAX_DRAFT_ITEMS,
    MAX_REASON_CHARS,
    MAX_REF_LABEL_CHARS,
    MAX_RULESET_LABEL_CHARS,
    MAX_STATEMENT_CHARS,
    REASON_ALREADY_APPROVED,
    REASON_APPROVAL_REASON_REQUIRED,
    REASON_APPROVAL_REQUIRED,
    REASON_BASELINE_NOT_FOUND,
    REASON_BASELINE_SUPERSEDED,
    REASON_CHANGE_REASON_REQUIRED,
    REASON_CLAIM_STATE_INVALID,
    REASON_CONFLICT_ACTION_INVALID,
    REASON_CONFLICT_KIND_INVALID,
    REASON_CROSS_BRAND_REFERENCE,
    REASON_DECISION_INVALID,
    REASON_DRAFT_NOT_FOUND,
    REASON_EXCLUSION_REASON_REQUIRED,
    REASON_PROJECT_NOT_FOUND,
    REASON_SCENE_STATE_INVALID,
    REASON_SELECTION_EMPTY,
    REASON_SNAPSHOT_REQUIRED,
    REASON_STEP3_NOT_READY,
    REASON_STEP_NO_INVALID,
    REASON_SUBJECT_KIND_INVALID,
    REASON_SUBJECT_VERSION_NOT_FOUND,
    SCENE_STATE_DISABLED,
    SCENE_STATE_PRODUCIBLE,
    STEP_MAX,
    STEP_MIN,
    SUBJECT_KIND_BASELINE,
    SUBJECT_KIND_BRIEF,
    SUBJECT_KIND_RESEARCH,
    SUBJECT_KINDS,
    BaselineDraft,
    ItemDraft,
    StepApprovalDraft,
)
from rednote.modules.baseline.models import (
    IMMUTABLE_TABLES,
    BaselineItem,
    InformationDraft,
    ProductionBaseline,
    StepApproval,
)
from rednote.modules.baseline.repository import BaselineRepository
from rednote.modules.identity.models import ROLE_ADMIN, ROLE_OPERATOR
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.knowledge import service as kn_service
from rednote.modules.knowledge.models import KnowledgeAsset
from rednote.modules.knowledge.repository import KnowledgeRepository
from rednote.modules.knowledge.types import (
    CATEGORY_FACT,
    SCOPE_BRAND,
    SCOPE_GLOBAL,
    SCOPE_PRODUCT,
    SCOPE_PROJECT,
)
from rednote.modules.projects.models import Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.modules.versions.models import (
    ENTITY_TYPE_BASELINE,
    ENTITY_TYPE_BRIEF,
    ENTITY_TYPE_KNOWLEDGE,
    ENTITY_TYPE_PLAN,
)
from rednote.modules.versions.repository import VersionRepository
from rednote.platform.configuration import Settings
from rednote.platform.database import get_engine, reset_engines, session_scope
from rednote.platform.errors import ApiError, ErrorCode
from rednote.platform.immutability import IMMUTABLE_RECORD_SQLSTATE

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
CSRF_SECRET_PLAINTEXT = "test-csrf-secret-plaintext"
TEST_ORIGIN = "http://testserver"

#: 本包自己的四张表（`version_nodes` **不在其中** —— 它是共用表）。
OWN_TABLES: tuple[Any, ...] = (
    InformationDraft.__table__,
    ProductionBaseline.__table__,
    BaselineItem.__table__,
    StepApproval.__table__,
)

#: DROP 顺序：**引用方在前**（先删引用别人的表，否则撞 FK）。
#: `production_baselines` 有自引用 FK 与 `step_approvals` 的自引用 FK，
#: 都是 `DEFERRABLE INITIALLY DEFERRED` —— 顺序无法完全解决，
#: 因此用 `CASCADE`。测试夹具要**可重复**，迁移要**可核对**，两者判据不同。
#: 已实测确认：本仓库**没有任何外部表**引用这四张表，CASCADE 不会波及其它模块。
DROP_ORDER: tuple[str, ...] = (
    "step_approvals",
    "baseline_items",
    "production_baselines",
    "information_drafts",
)


def _test_database_url() -> str:
    """DSN 从环境变量取（分支用 `rednote_test_wp22`），缺省本机 postgres。"""
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
    """只建本包的四张表 + 九个触发器。

    **先 DROP 自己的四张表**：测试库里可能有早先的探针残留，而
    `create_all` 只对"表不存在"是幂等的 —— 表已存在但结构是旧的时候
    它会**静默跳过**，于是测试跑在一个结构不对的表上。

    **触发器必须 DROP → CREATE**：`CREATE TRIGGER` 不是幂等的
    （本机实测报 `DuplicateObject: trigger "trg_..." already exists`）。

    `trg_version_nodes_baseline_paired` 挂在共用表 `version_nodes` 上，
    本夹具删它再建它，**但不删 `version_nodes` 本身**（那是 WP11 的表）。

    `reset_engines()` 先跑：conftest 的 `_isolate_settings_cache` 是
    autouse 的 function 级 fixture，module 级 fixture 拿到的 engine
    可能已被上一轮清掉 —— 重取一次最稳。
    """
    reset_engines()
    settings = _make_settings()
    engine = get_engine(settings)

    # 夹具前后核对「跨包入向外键」集合必须不变（ISS-106 的判据）。
    fks_before = cross_package_foreign_keys(engine, DROP_ORDER)

    # 只删「没有跨包入向外键」的表；被别的包引用的表必须保留。
    # 本包当前无入向跨包外键，因此四张表都可删 —— 但流程统一，
    # 否则下一个包把 FK 指向本包表时，这里会静默摘掉它。
    dropped, kept = drop_own_tables(engine, DROP_ORDER)

    InformationDraft.metadata.create_all(engine, tables=list(OWN_TABLES))

    fks_after = cross_package_foreign_keys(engine, DROP_ORDER)
    assert fks_after == fks_before, (
        f"建表夹具改变了跨包外键集合：before={sorted(fks_before)} "
        f"after={sorted(fks_after)}（dropped={dropped} kept={kept}）"
    )

    with engine.begin() as conn:
        for (
            _table,
            func_ddl,
            trigger_ddl,
            drop_trigger,
            _drop_func,
        ) in bs_ddl.all_trigger_ddl():
            conn.execute(text(drop_trigger))  # 幂等关键
            conn.execute(text(func_ddl))
            conn.execute(text(trigger_ddl))
    yield


# ===========================================================================
# 数据夹具（自包含，不依赖 conftest 的隐式状态）
# ===========================================================================


@dataclass
class BaselineEnv:
    settings: Settings
    project_id: uuid.UUID
    brand_id: uuid.UUID
    product_id: uuid.UUID
    owner_id: uuid.UUID
    admin_id: uuid.UUID
    #: 另一套品牌/产品/项目，用于跨品牌反例（AC-24）
    other_brand_id: uuid.UUID
    other_product_id: uuid.UUID
    other_project_id: uuid.UUID


def _create_user(
    settings: Settings, *, role: str = ROLE_OPERATOR, name: str = "Baseline 测试用户"
) -> uuid.UUID:
    with session_scope(settings) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name=name,
            role=role,
        )
        return user.id


def _seed_project(settings: Settings, *, owner_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """造 Brand → Product → Project，返回 `(brand_id, product_id, project_id)`。"""
    with session_scope(settings) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name=f"产品-{uuid.uuid4().hex[:6]}")
        session.add(product)
        session.flush()
        project = Project(
            project_code=f"BSL-{uuid.uuid4().hex[:8].upper()}",
            name="基线测试项目",
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
        return brand.id, product.id, project.id


@pytest.fixture
def env() -> BaselineEnv:
    reset_engines()
    settings = _make_settings()
    owner = _create_user(settings)
    admin = _create_user(settings, role=ROLE_ADMIN, name="Baseline 测试管理员")
    brand_id, product_id, project_id = _seed_project(settings, owner_id=owner)
    other_brand, other_product, other_project = _seed_project(settings, owner_id=owner)
    return BaselineEnv(
        settings=settings,
        project_id=project_id,
        brand_id=brand_id,
        product_id=product_id,
        owner_id=owner,
        admin_id=admin,
        other_brand_id=other_brand,
        other_product_id=other_product,
        other_project_id=other_project,
    )


# --- 2.0 基础设施 ----------------------------------------------------------


def _raw(env: BaselineEnv, sql: str, params: dict[str, Any] | None = None) -> Any:
    """在**独立事务**里执行一条原生 SQL 并提交。返回结果行（若有）。"""
    with session_scope(env.settings) as session:
        result = session.execute(text(sql), params or {})
        return result.fetchall() if result.returns_rows else None


def _scalar(env: BaselineEnv, sql: str, params: dict[str, Any] | None = None) -> Any:
    with session_scope(env.settings) as session:
        return session.execute(text(sql), params or {}).scalar_one_or_none()


def _raw_expect_error(
    env: BaselineEnv, sql: str, params: dict[str, Any] | None = None
) -> DBAPIError:
    """执行一条**应当失败**的原生 SQL，返回被捕获的异常供断言 sqlstate。"""
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            session.execute(text(sql), params or {})
    return caught.value


def _assert_deferred_blocked(
    env: BaselineEnv, statements: list[tuple[str, dict[str, Any]]]
) -> tuple[str, str, str]:
    """在一个事务里跑多条语句，**用 `SET CONSTRAINTS ALL IMMEDIATE` 取出判定**。

    返回 `(sqlstate, message_primary, detail)`。

    **为什么不用 `commit()` 取判定**：本包的三张不可变表**物理删不掉** ——
    `DELETE FROM production_baselines` 会被 `rednote_reject_mutation()` 拦下，
    连自己的探针残留都清不掉（WP18 实测，ISS-63）。延迟约束的判定时机可以
    用 `SET CONSTRAINTS ALL IMMEDIATE` **提前**到事务内的任意点：事务仍未提交，
    判定已经拿到，之后无条件 rollback，一行不留。

    ## 为什么返回消息而不只是 sqlstate

    三条延迟触发器**同挂 `production_baselines`**，按名字典序执行：
    `approval_required` < `no_blocking` < `node_consistency`。
    只断言 `sqlstate=23514` 无法区分"是哪一个判据拦下的" ——
    而"被正确地拦下"与"被另一个判据顺带拦下"是两件事（ISS-95 同族）。
    因此断言**专属措辞**（`"不是 BASELINE"` / `"不一致"` 等）。

    ## 为什么把 `detail` 也带出来

    `message_primary` 是**主消息**，唯一约束违约时它只说
    `duplicate key value violates unique constraint "…"` ——
    **"是哪个项目"在 `DETAIL` 里**。第一版把断言写成
    "`str(project_id) in primary`"，于是永远失败（primary 里本就没有它），
    且失败原因看起来像"索引认错了项目"。需要它的用例显式要 `detail`。
    """
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            for sql, params in statements:
                session.execute(text(sql), params)
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    orig = caught.value.orig  # type: ignore[attr-defined]
    diag = getattr(orig, "diag", None)
    return (
        getattr(orig, "sqlstate", ""),
        getattr(diag, "message_primary", "") or "",
        getattr(diag, "message_detail", "") or "",
    )


def _insert_node(
    conn: Any,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    revision_no: int = 1,
    node_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """在**给定连接**里插一个版本节点，返回 node id。

    **必须复用调用方的连接**（而不是自己开事务）：本包的反向触发器是
    **延迟**的，它在 `COMMIT` 时才校验"`BASELINE` 节点必须有配对的
    `production_baselines` 行"。若节点与基线行在两个事务里插入，
    第一个事务提交时节点就是**孤立节点** → 被拒（WP18/WP20 已踩过）。
    """
    nid = node_id or uuid.uuid4()
    conn.execute(
        text(
            "insert into version_nodes (id, entity_type, entity_id, revision_no,"
            " payload_hash, schema_version, project_id)"
            " values (:id, :etype, :eid, :rev, :hash, 1, :pid)"
        ),
        {
            "id": nid,
            "etype": entity_type,
            "eid": entity_id,
            "rev": revision_no,
            "hash": "0" * 64,
            "pid": project_id,
        },
    )
    return nid


def _make_brief_node(
    env: BaselineEnv, *, project_id: uuid.UUID | None = None
) -> uuid.UUID:
    """造一个合法的 `BRIEF` 版本节点（`briefVersionId` 的合法取值）。

    ⚠️ **每个节点的 `entity_id` 都不同**。`version_nodes` 的唯一约束是
    `(entity_type, entity_id, revision_no)`，而本包的判据只关心
    "这是一个合法的 BRIEF 节点"，不关心它挂在哪个 Brief 实体下。
    把 `entity_id` 固定成 `project_id`（配 `revision_no=1`）会让**第二次
    调用**直接撞唯一约束 —— 实测踩过，且报错点落在 helper 里，
    很容易被误读成"被测代码有缺陷"。
    """
    with session_scope(env.settings) as session:
        return _insert_node(
            session,
            entity_type=ENTITY_TYPE_BRIEF,
            entity_id=uuid.uuid4(),
            project_id=env.project_id if project_id is None else project_id,
        )


def _make_knowledge(
    env: BaselineEnv,
    *,
    scope: str = SCOPE_PRODUCT,
    brand_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """用**服务层**建一条知识，返回 `(asset_id, version_id)`。

    本包只读知识的 `scope` / `brand` / `product` / `project` 四个投影列，
    因此用真服务层建（而不是裸 SQL）可以让第 2 层的跨品牌判据
    测试的是**真实的**知识行，而非手搓的形状。
    """
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        created = kn_service.create_asset(
            repo,
            category=CATEGORY_FACT,
            title=f"基线用事实-{uuid.uuid4().hex[:6]}",
            scope=scope,
            brand_id=brand_id,
            product_id=product_id,
            project_id=project_id,
            body="续航 18 小时",
            structured_payload={"fact.statement": {"status": "FILLED", "value": "续航 18 小时"}},
            change_reason="基线测试",
            actor_id=env.owner_id,
        )
        return created.asset.id, created.version.id


def _own_knowledge(
    env: BaselineEnv,
    *,
    scope: str = SCOPE_PRODUCT,
    brand_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """本项目产品范围内的知识（第 2 层正例用）。

    ⚠️ **必须同时给 `brand_id` 与 `product_id`**：`scope='PRODUCT'` 的
    *知识侧* CHECK 只要求 `product_id`（`scope_product_needs_product_id`），
    但*基线侧*的 `_require_item_scope` 对 `PRODUCT` 的判据是
    `k_brand == project_brand and k_product == project_product`
    （`service.py` 的 `SCOPE_MATCH_SAME_PRODUCT` 分支）。
    只给 `product_id` 会让 `k_brand=None` ≠ `project_brand` → 被判成跨品牌。
    两层的判据**不是同一条**，因此这里要按更严的那条构造。
    """
    return _make_knowledge(
        env,
        scope=scope,
        brand_id=env.brand_id if brand_id is None else brand_id,
        product_id=env.product_id if product_id is None else product_id,
    )


def _write_draft(
    env: BaselineEnv,
    *,
    project_id: uuid.UUID | None = None,
    claims: Any = None,
    conflicts: Any = None,
    scenes: Any = None,
    boundaries: Any = None,
    research_selection: Any = None,
    operator_confirmed: bool | None = None,
    brief_version_id: uuid.UUID | None = None,
) -> Any:
    """保存工作区（部分更新），返回 `(draft, readiness)`。

    `project_id` 缺省为 `env.project_id`；显式传入可以给**另一个项目**
    备料（`_require_supersedes_target` 的"目标属于别的项目"那一半
    必须有一个真实的别项目基线才能测到，见 `_ready_and_baseline`
    的 `project_id` 参数）。
    """
    pid = env.project_id if project_id is None else project_id
    with session_scope(env.settings) as session:
        repo = BaselineRepository(session)
        draft = bs.require_draft(repo, project_id=pid)
        return bs.update_draft(
            repo,
            project_id=pid,
            expected_lock_version=draft.lock_version,
            claims=claims,
            conflicts=conflicts,
            scenes=scenes,
            boundaries=boundaries,
            research_selection=research_selection,
            operator_confirmed=operator_confirmed,
            brief_version_id=brief_version_id,
        )


#: readiness 全绿的草稿内容（第 2 层"可创建"的前提）。
READY_CLAIMS: list[dict[str, Any]] = [
    {"claimId": "c1", "statement": "续航 18 小时", "state": CLAIM_STATE_CONFIRMED}
]
READY_CONFLICTS: list[dict[str, Any]] = [
    {
        "conflictId": "k1",
        "kind": "SOURCE",
        "blocking": True,
        "state": CONFLICT_STATE_SETTLED,
        "action": "ADOPT_ONE",
    }
]
READY_SCENES: list[dict[str, Any]] = [
    {"sceneId": "s1", "state": SCENE_STATE_PRODUCIBLE, "factRefs": ["c1"]}
]


def _make_ready(
    env: BaselineEnv, *, brief_version_id: uuid.UUID, project_id: uuid.UUID | None = None
) -> None:
    """把工作区写成 readiness 全绿（默认本项目，可指定别的项目）。"""
    _draft, status = _write_draft(
        env,
        project_id=project_id,
        claims=READY_CLAIMS,
        conflicts=READY_CONFLICTS,
        scenes=READY_SCENES,
        boundaries={"tone": "克制"},
        operator_confirmed=True,
        brief_version_id=brief_version_id,
    )
    assert status.ready, [c.reason for c in status.unsatisfied()]


def _draft_of(env: BaselineEnv) -> Any:
    with session_scope(env.settings) as session:
        return bs.require_draft(BaselineRepository(session), project_id=env.project_id)


def _baseline_draft(
    env: BaselineEnv,
    *,
    brief_version_id: uuid.UUID,
    selected: tuple[ItemDraft, ...],
    project_id: uuid.UUID | None = None,
    supersedes_id: uuid.UUID | None = None,
    ruleset_version: str = "ruleset-v5",
    change_reason: str = "首版基线",
) -> BaselineDraft:
    return BaselineDraft(
        project_id=env.project_id if project_id is None else project_id,
        brief_version_id=brief_version_id,
        selected=selected,
        ruleset_version=ruleset_version,
        change_reason=change_reason,
        supersedes_id=supersedes_id,
    )


def _create_baseline(env: BaselineEnv, draft: BaselineDraft, **kwargs: Any) -> Any:
    with session_scope(env.settings) as session:
        return bs.create_baseline(
            BaselineRepository(session),
            VersionRepository(session),
            draft=draft,
            actor_id=kwargs.pop("actor_id", env.owner_id),
            **kwargs,
        )


#: 审计的 action 取值（与 `service.py` 的 `_ACTION_*` 同名同值）。
#: **不 import 私有常量**：它们是模块私有（前导下划线），而测试断言的是
#: **落库的字符串**。直接写字面量让"哪天有人改了 action 名"在测试里失败。
AUDIT_DRAFT_UPDATE = "information_draft.update"
AUDIT_BASELINE_CREATE = "baseline.create"
AUDIT_BASELINE_SUPERSEDE = "baseline.supersede"
AUDIT_STEP_APPROVE = "step.approve"


def _approve_step(
    env: BaselineEnv,
    *,
    step_no: int | None = None,
    subject_kind: str = SUBJECT_KIND_BASELINE,
    subject_version_id: uuid.UUID | None = None,
    decision: str = APPROVAL_DECISION_APPROVED,
    reason: str | None = "步骤批准",
    supersedes_id: uuid.UUID | None = None,
    decided_by: uuid.UUID | None = None,
    request_id: str | None = None,
) -> Any:
    """调 `service.approve_step`（不传 `request_id` ⇒ 不写审计）。

    `subject_version_id` 缺省取**该项目的当前生效基线** ——
    `effectiveBaselineId` 是"步骤 7 已批准哪一版"的自然对象。
    """
    if subject_version_id is None:
        active = _active_baseline(env)
        assert active is not None, "调用方应当先建一条基线，或显式传 subject_version_id"
        subject_version_id = active
    draft = StepApprovalDraft(
        project_id=env.project_id,
        step_no=bs_ddl.BASELINE_STEP_NO if step_no is None else step_no,
        subject_kind=subject_kind,
        subject_version_id=subject_version_id,
        decision=decision,
        reason=reason,
        decided_by=env.owner_id if decided_by is None else decided_by,
        supersedes_id=supersedes_id,
    )
    with session_scope(env.settings) as session:
        kwargs: dict[str, Any] = {}
        if request_id is not None:
            kwargs["request_id"] = request_id
        return bs.approve_step(BaselineRepository(session), draft=draft, **kwargs)


def _expect_api_error(callable_: Any, *, code: ErrorCode | None = None, reason: str | None = None) -> ApiError:
    """断言调用抛 `ApiError`，可选校验 `code` 与 `details.reason`。"""
    with pytest.raises(ApiError) as caught:
        callable_()
    error = caught.value
    if code is not None:
        assert error.code == code, f"期望 {code}，实际 {error.code}：{error.message}"
    if reason is not None:
        assert (error.details or {}).get("reason") == reason, (
            f"期望 reason={reason!r}，实际 {(error.details or {}).get('reason')!r}"
        )
    return error


# ===========================================================================
# 第 1 层 —— 纯函数（不碰库）
# ===========================================================================
#
# 这一层的存在理由：`ddl.py` 的文本正确性、`readiness`/`context_order` 的
# 判据正确性，都不该依赖"库能不能连上"。它们红了就说明**代码**错了，
# 而不是环境错了 —— 这个区分在排障时省掉大量时间。


# --- 1.1 DDL 文本自洽（与迁移同源） ----------------------------------------


def test_trigger_names_are_unique_and_nine() -> None:
    """九个触发器名唯一，且 = 三个不可变 + 六个一致性。

    ⚠️ 这里断言的是 **`ddl.py` 自己声明的**九个都在且唯一 ——
    不是"全库恰好有九个触发器"（那是第 3 层的事，且要用下界）。
    """
    names = bs_ddl.trigger_names()
    assert isinstance(names, tuple)
    assert len(names) == len(set(names)), f"触发器名有重复：{names}"
    assert len(names) == 9, f"本包声明九个触发器，实际 {len(names)}：{names}"
    assert set(names) == set(bs_ddl.immutable_trigger_names()) | set(
        bs_ddl.consistency_trigger_names()
    ), "九个应恰好等于不可变三条 + 一致性六条，且两族不重叠"
    assert not (
        set(bs_ddl.immutable_trigger_names())
        & set(bs_ddl.consistency_trigger_names())
    ), "一个触发器不能既不可变又一致性"


def test_immutable_trigger_names_verbatim() -> None:
    """三条不可变触发器：逐字钉住名与所属表。

    名字里带表名 —— 因此"触发器挂错表"会让这条红。
    """
    assert bs_ddl.immutable_trigger_names() == (
        "trg_production_baselines_immutable",
        "trg_baseline_items_immutable",
        "trg_step_approvals_immutable",
    )


def test_consistency_trigger_names_fixed_order() -> None:
    """六条一致性触发器：**固定顺序**钉住。

    顺序要紧：三条延迟触发器同挂 `production_baselines`，
    按名字典序执行（`approval_required` < `no_blocking` < `node_consistency`）。
    这里的元组顺序就是 `all_trigger_ddl()` 的装配顺序。
    """
    assert bs_ddl.consistency_trigger_names() == (
        "trg_production_baselines_node_consistency",
        "trg_version_nodes_baseline_paired",
        "trg_baseline_items_cross_brand",
        "trg_baseline_items_asset_scope",
        "trg_production_baselines_approval_required",
        "trg_production_baselines_no_blocking",
    )


def test_consistency_function_names_unique_and_six() -> None:
    """六个一致性函数名唯一；与六个触发器**一一对应**（数量相等）。

    `trigger` 与 `function` 是两层：一个函数可被多个触发器用（三个不可变
    触发器共用一个 `rednote_reject_mutation`），但六条一致性函数各自专属 ——
    因此"函数数 == 触发器数"对一致性族成立，对不可变族**不成立**（3:1）。
    这条等式是本包的**设计约束**，不是巧合。
    """
    funcs = bs_ddl.consistency_function_names()
    assert isinstance(funcs, tuple)
    assert len(funcs) == 6
    assert len(set(funcs)) == 6, f"一致性函数名有重复：{funcs}"
    assert len(funcs) == len(bs_ddl.consistency_trigger_names()), (
        "一致性族：每个触发器一个专属函数（与不可变族的 3:1 共享相反）"
    )
    assert not any(
        "reject_mutation" in name for name in funcs
    ), "rednote_reject_mutation 是三条不可变触发器共享的，不属于一致性族"


def test_all_trigger_ddl_shape_is_five_tuple_of_str() -> None:
    """`all_trigger_ddl()` 的九项都是 **(表名, func, trigger, drop_trg, drop_fn)** 五元组。

    ⚠️ **与单 builder 的形状不同**：`baseline_node_paired_ddl()` 等单 builder
    返回**四元组**（无表名）—— 表名由聚合器 prepend。
    这个差异曾让本包的测试探针 `IndexError`，所以在这里显式钉住。
    """
    items = bs_ddl.all_trigger_ddl()
    assert len(items) == 9
    for item in items:
        assert isinstance(item, tuple), f"应是元组：{type(item)}"
        assert len(item) == 5, f"聚合器应是五元组（表名在首位），实际 {len(item)}：{item[0]}"
        assert all(isinstance(part, str) for part in item), f"五项都应是 str：{item[0]}"
    # 单 builder 是四元组 —— 反向确认这个差异存在（否则上面的说明就是假的）
    single = bs_ddl.baseline_node_paired_ddl()
    assert len(single) == 4, (
        "单 builder 返回四元组（无表名）。若这里变成五元组，"
        "本文件的解包与迁移的调用都需同步改"
    )
    assert single[0].startswith("CREATE OR REPLACE FUNCTION"), (
        "单 builder 的第 0 项是**函数** DDL（聚合器的 [0] 是表名，两者错位一位）"
    )
    assert single[1].startswith("CREATE CONSTRAINT TRIGGER"), (
        "第 1 项是触发器 DDL"
    )
    assert single[2].startswith("DROP TRIGGER"), "第 2 项是 DROP TRIGGER"
    assert single[3].startswith("DROP FUNCTION"), "第 3 项是 DROP FUNCTION"


def test_all_trigger_ddl_covers_both_families_in_order() -> None:
    """前三条是不可变、后六条是一致性，顺序与 `trigger_names()` 一致。

    这条保证"装触发器的循环"的顺序 = "声明名的元组"的顺序 ——
    两者漂移时，`temp_install` 可能装了一半就停（顺序错 → 依赖错）。
    """
    items = bs_ddl.all_trigger_ddl()
    tables = [item[0] for item in items]
    assert tables[:3] == [
        "production_baselines",
        "baseline_items",
        "step_approvals",
    ], f"前三条不可变触发器应挂这三张表，实际 {tables[:3]}"
    assert tables[3:] == [
        "production_baselines",
        "version_nodes",
        "baseline_items",
        "baseline_items",
        "production_baselines",
        "production_baselines",
    ], f"后六条一致性的所属表，实际 {tables[3:]}"
    # 名字顺序也要对得上（而不是只有表名对得上）
    for item, name in zip(items, bs_ddl.trigger_names()):
        assert f" {name}" in item[2], (
            f"装配顺序与 trigger_names() 漂移：第 {items.index(item)} 项 "
            f"的 DDL 里没有 {name}"
        )


def test_consistency_items_order_and_tables() -> None:
    """`consistency_items()` 六项、顺序固定、所属表固定。

    与 `all_trigger_ddl()[3:]` 应当是**同一批**（同一来源，不各写一遍）。
    """
    items = bs_ddl.consistency_items()
    assert len(items) == 6
    assert [item[0] for item in items] == [
        "production_baselines",
        "version_nodes",
        "baseline_items",
        "baseline_items",
        "production_baselines",
        "production_baselines",
    ]
    assert items == bs_ddl.all_trigger_ddl()[3:], (
        "`consistency_items()` 与 `all_trigger_ddl()` 的后六项必须同源，"
        "否则迁移与测试会各装一套"
    )


def test_immutable_ddl_drops_the_shared_reject_mutation_function() -> None:
    """三条不可变触发器的 `drop_func` 指向**同一个**共享函数。

    若某条写成 DROP 自己的私有名，`temp_uninstall` 会在第一条之后就
    把函数删掉，第二条的 `CREATE OR REPLACE` 又把它建回来 ——
    **看起来能跑**，但"卸载"根本没卸干净。
    """
    for item in bs_ddl.all_trigger_ddl()[:3]:
        table, _fn, _trg, drop_trg, drop_func = item
        assert "rednote_reject_mutation" in drop_func, (
            f"{table} 的 drop_func 应指向共享的 rednote_reject_mutation：{drop_func}"
        )
        assert item[3].endswith(table) or table in item[3], (
            f"DROP TRIGGER 语句应带 ON {table}：{item[3]}"
        )
    funcs = {item[4] for item in bs_ddl.all_trigger_ddl()[:3]}
    assert len(funcs) == 1, f"三条共享一个函数（3:1），实际 {funcs}"


def test_consistency_ddl_drops_its_own_function_not_the_shared_one() -> None:
    """六条一致性触发器各 DROP 自己的专属函数，**不动**共享的 reject_mutation。

    这是上一条的镜像：两族共享策略不同。若一致性族也去 DROP
    `rednote_reject_mutation`，卸载一致性触发器会**顺手拆掉不可变防线** ——
    防线的失效不会报错，只会让基线可以被改。
    """
    for item in bs_ddl.consistency_items():
        table, _fn, _trg, _drop_trg, drop_func = item
        assert "rednote_reject_mutation" not in drop_func, (
            f"{table} 的一致性触发器不得 DROP 不可变族的共享函数：{drop_func}"
        )
    names = bs_ddl.consistency_function_names()
    dropped = {item[4] for item in bs_ddl.consistency_items()}
    for name in names:
        assert any(name in stmt for stmt in dropped), (
            f"函数 {name} 没有任何一条 drop_func 负责卸载它（装了卸不掉）"
        )


def test_consistency_functions_raise_with_controlled_sqlstate() -> None:
    """六条一致性函数都用 `RAISE EXCEPTION ... ERRCODE = '23514'`。

    且 **不得含 `%s`**：plpgsql 的 `RAISE` 用 `%` 占位，
    在 Python 侧拼 SQL 时 `%` 会与 DBAPI 的参数风格冲突
    （本项目已踩过同类形态）。函数体里只用 `||` 拼接。
    """
    for item in bs_ddl.consistency_items():
        func_ddl = item[1]
        assert "RAISE EXCEPTION" in func_ddl, f"{item[0]} 的函数应会 RAISE"
        assert str(bs_ddl.CONSISTENCY_SQLSTATE) in func_ddl, (
            f"{item[0]} 的函数应以 {bs_ddl.CONSISTENCY_SQLSTATE} 报错"
        )
        assert "%s" not in func_ddl, (
            f"{item[0]} 的函数体出现 %s —— 与 DBAPI 参数风格冲突，须用 || 拼接"
        )
        assert "$$ LANGUAGE plpgsql" in func_ddl, (
            f"{item[0]} 的函数没有正确收尾（缺 LANGUAGE plpgsql）"
        )


def test_consistency_triggers_are_deferred_but_immutable_are_not() -> None:
    """六条一致性用 `CREATE CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED`；
    三条不可变是普通 `CREATE TRIGGER`（即时）。

    **两族的时机不同是本包的核心设计**：不可变必须**即时**拦（否则一次
    UPDATE 在事务里改完再改回来，中途的非法状态无人看见）；一致性必须
    **延迟**到 COMMIT（因为"基线 + 它的批准 + 它的版本节点"是在同一事务里
    分多步插入的，中途必然短暂不一致）。
    这条一旦搞反，正例会全红或防线会失效。
    """
    for item in bs_ddl.consistency_items():
        assert "CREATE CONSTRAINT TRIGGER" in item[2], (
            f"{item[0]} 的一致性触发器应是 CONSTRAINT TRIGGER：{item[2][:60]}"
        )
        assert "DEFERRABLE INITIALLY DEFERRED" in item[2], (
            f"{item[0]} 的一致性触发器应延迟到 COMMIT：{item[2][:60]}"
        )
    for item in bs_ddl.all_trigger_ddl()[:3]:
        assert "CONSTRAINT TRIGGER" not in item[2], (
            f"{item[0]} 的不可变触发器不得延迟 —— 必须即时拦：{item[2][:60]}"
        )
        assert "DEFERRABLE" not in item[2]
        assert "BEFORE UPDATE OR DELETE" in item[2], (
            f"{item[0]} 的不可变触发器应是 BEFORE UPDATE OR DELETE：{item[2][:60]}"
        )


@pytest.mark.cross_wp
def test_node_paired_trigger_only_fires_for_baseline_nodes() -> None:
    """挂在共用表 `version_nodes` 上的反向触发器带 WHEN 过滤。

    **没有 WHEN 的话**：每一次 `BRIEF`/`KNOWLEDGE` 节点插入都会去
    `production_baselines` 里找配对行 —— 找不到就 RAISE，
    于是**所有**版本节点都建不了。这是一个"漏了 WHEN 就全盘停摆"的点，
    因此必须显式钉住。

    这里按**四元组**索引（单 builder 形状，见上一条）。
    """
    func_ddl, trigger_ddl, drop_trigger, drop_func = bs_ddl.baseline_node_paired_ddl()
    assert "ON version_nodes" in trigger_ddl, trigger_ddl
    assert "AFTER INSERT" in trigger_ddl, trigger_ddl
    assert "WHEN (NEW.entity_type = 'BASELINE')" in trigger_ddl, (
        f"缺 WHEN 过滤会让所有实体类型的节点都被要求配对基线：{trigger_ddl}"
    )
    assert "ON version_nodes" in drop_trigger, drop_trigger
    assert drop_func.startswith("DROP FUNCTION IF EXISTS rednote_check_baseline_node_paired"), (
        drop_func
    )
    # 这次**不**用 WHEN 过滤 —— 函数体里还必须自己查 projects 兜一层
    assert "version_nodes_baseline_paired" not in func_ddl, (
        "函数体不该引用触发器名（它只看 NEW）"
    )


def test_baseline_step_no_is_three_and_consistent_across_modules() -> None:
    """`BASELINE_STEP_NO == 3`，且 `ddl` 与 `service` 两处**同值**。

    两处各写一个 3 是本项目的经典漂移形态（函数体里硬编码了 `sa.step_no = 3`，
    而服务层用常量）。这条钉住"函数体的 3 来自同一个定义"。
    """
    assert bs_ddl.BASELINE_STEP_NO == bs.BASELINE_STEP_NO == 3
    assert bs.BASELINE_STEP_NO == bt.STEP_MIN + 2, "步骤号从 1 起算"
    assert bt.STEP_MIN <= bs.BASELINE_STEP_NO <= bt.STEP_MAX
    assert bs.BASELINE_STEP_NO in bs.STEP_NAMES, "步骤号必须有名字（看板要显示）"
    assert bs.STEP_NAMES[bs.BASELINE_STEP_NO] == "信息审核与基线"
    # 函数体里的硬编码 3 也必须等于它 —— 这是"同源"的实质检查
    approval_ddl = bs_ddl.approval_required_ddl()
    assert "sa.step_no = 3" in approval_ddl[0], (
        "一致性函数体的步骤号与 BASELINE_STEP_NO 必须一致"
    )


def test_each_consistency_function_owns_its_own_domain_wording() -> None:
    """六条函数各自的专属措辞 —— 钉住"这个函数管这件事"。

    **为什么逐条断言而不是只数个数**：六个函数若互相错配
    （例如 `no_blocking` 的函数被装到 `node_consistency` 的触发器上），
    数量、deferrable、RAISE、23514 **全部仍然通过** ——
    只有专属措辞能发现。而错配的后果是"报错说的事与真实原因不符"，
    排障时把人引向完全错误的方向。
    """
    texts = {item[1]: item[0] for item in bs_ddl.consistency_items()}

    def body_of(trigger_name: str) -> str:
        for func_ddl, _trg, _dt, _df in (
            bs_ddl.baseline_version_consistency_ddl(),
            bs_ddl.baseline_node_paired_ddl(),
            bs_ddl.cross_brand_reference_ddl(),
            bs_ddl.asset_version_scope_ddl(),
            bs_ddl.approval_required_ddl(),
            bs_ddl.no_blocking_conflict_ddl(),
        ):
            if trigger_name in _trg:
                return func_ddl
        raise AssertionError(f"没有 builder 产出触发器 {trigger_name}")

    # ① 节点类型一致性
    text = body_of("trg_production_baselines_node_consistency")
    assert "不是 BASELINE" in text, "node_consistency 应校验 entity_type"
    assert "不一致" in text, "node_consistency 应校验 entity_id 与 project_id"
    assert "没有对应的 version_nodes 行" in text, "应校验基线有配对节点"
    # ② 反向：孤立节点
    text = body_of("trg_version_nodes_baseline_paired")
    assert "孤立节点" in text, "node_paired 应拦孤立 BASELINE 节点"
    assert "production_baselines" in text
    # ③ 跨品牌
    text = body_of("trg_baseline_items_cross_brand")
    assert "AC-24" in text, "cross_brand 的报错应点名 AC-24"
    assert "跨品牌事实不得迁移" in text
    assert "不存在" in text, "应区分'资产不存在'与'跨品牌'两种失败"
    # ④ 版本归属
    text = body_of("trg_baseline_items_asset_scope")
    assert "条目须保存实际选用的版本" in text, (
        "asset_scope 应钉住'版本属于本行声明的资产'"
    )
    # ⑤ 批准
    text = body_of("trg_production_baselines_approval_required")
    assert "OP-151" in text, "approval 的报错应点名 OP-151（同事务生成批准）"
    assert "同事务的批准" in text
    # ⑥ 阻断问题
    text = body_of("trg_production_baselines_no_blocking")
    assert "AC-09" in text, "no_blocking 的报错应点名 AC-09"
    assert "未处置的阻断问题" in text
    assert "不得创建基线" in text
    # 六段文本互不相同 —— 否则上面那些断言有两条在测同一段
    assert len(set(texts)) == 6, "六个函数的文本应两两不同"


# --- 1.2 `readiness` 纯判据 -------------------------------------------------


def test_condition_keys_order_matches_prd() -> None:
    """三条完成条件的键与顺序（PRD §7.3）—— 受控取值，前端按 key 取。"""
    assert rd.CONDITION_KEYS == (
        rd.CONDITION_NO_BLOCKING_OPEN,
        rd.CONDITION_PRODUCIBLE_SCENE,
        rd.CONDITION_OPERATOR_CONFIRMED,
    )
    assert rd.CONDITION_KEYS == (
        "blockingResolved",
        "hasProducibleScene",
        "operatorConfirmed",
    ), "键名是 API 契约的一部分（改了就前端静默失效）"


def test_readiness_ready_is_conjunction_of_three() -> None:
    """`ready` 是三条的**合取**，且 `unsatisfied()` 精确列出未满足的项。

    逐条翻转 —— 不是只测"全绿"与"全红"两个端点：
    只测端点的话，一个把 `all(...)` 写成 `any(...)` 的实现会**通过**。
    """
    claims = ({"claimId": "c1", "state": CLAIM_STATE_CONFIRMED},)
    conflicts_open = ({"conflictId": "k1", "blocking": True, "state": CONFLICT_STATE_OPEN},)
    conflicts_settled = (
        {"conflictId": "k1", "blocking": True, "state": CONFLICT_STATE_SETTLED},
    )
    scenes_ok = ({"sceneId": "s1", "state": SCENE_STATE_PRODUCIBLE, "factRefs": ["c1"]},)
    scenes_none = ({"sceneId": "s1", "state": "DISABLED", "factRefs": ["c1"]},)

    all_ok = rd.step3_readiness(
        claims=claims, conflicts=conflicts_settled, scenes=scenes_ok, operator_confirmed=True
    )
    assert all_ok.ready is True
    assert all_ok.unsatisfied() == ()
    assert [c.key for c in all_ok.conditions] == list(rd.CONDITION_KEYS)

    # 只把①翻红
    only_blocking = rd.step3_readiness(
        claims=claims, conflicts=conflicts_open, scenes=scenes_ok, operator_confirmed=True
    )
    assert only_blocking.ready is False
    assert [c.key for c in only_blocking.unsatisfied()] == [rd.CONDITION_NO_BLOCKING_OPEN]

    # 只把②翻红
    only_scene = rd.step3_readiness(
        claims=claims, conflicts=conflicts_settled, scenes=scenes_none, operator_confirmed=True
    )
    assert only_scene.ready is False
    assert [c.key for c in only_scene.unsatisfied()] == [rd.CONDITION_PRODUCIBLE_SCENE]

    # 只把③翻红
    only_confirm = rd.step3_readiness(
        claims=claims, conflicts=conflicts_settled, scenes=scenes_ok, operator_confirmed=False
    )
    assert only_confirm.ready is False
    assert [c.key for c in only_confirm.unsatisfied()] == [rd.CONDITION_OPERATOR_CONFIRMED]

    # 全红：三条都上榜，顺序与 CONDITION_KEYS 一致
    none_ok = rd.step3_readiness(
        claims=(),
        conflicts=conflicts_open,
        scenes=scenes_none,
        operator_confirmed=False,
    )
    assert none_ok.ready is False
    assert [c.key for c in none_ok.unsatisfied()] == list(rd.CONDITION_KEYS)


def test_readiness_reason_texts_are_verbatim() -> None:
    """六条 reason 文案**逐字**钉住。

    这些文字同时出现在"API 预检响应"与"创建拒绝的错误体"里
    （`_require_readiness_for_creation` 直接拼 `condition.reason`）。
    逐字断言 = 保证两处说的是同一句话的前置条件。
    """
    claims = ({"claimId": "c1", "state": CLAIM_STATE_CONFIRMED},)
    scenes_ok = ({"sceneId": "s1", "state": SCENE_STATE_PRODUCIBLE, "factRefs": ["c1"]},)

    ok = rd.step3_readiness(
        claims=claims,
        conflicts=(),
        scenes=scenes_ok,
        operator_confirmed=True,
    )
    assert ok.by_key(rd.CONDITION_NO_BLOCKING_OPEN).reason == "阻断问题已全部处置。"
    assert ok.by_key(rd.CONDITION_PRODUCIBLE_SCENE).reason == "1 个场景可生产。"
    assert (
        ok.by_key(rd.CONDITION_OPERATOR_CONFIRMED).reason == "运营已明确点击确认。"
    )

    bad = rd.step3_readiness(
        claims=claims,
        conflicts=(
            {"conflictId": "k1", "blocking": True, "state": CONFLICT_STATE_OPEN},
            {"conflictId": "k2", "blocking": True, "state": CONFLICT_STATE_OPEN},
        ),
        scenes=(),
        operator_confirmed=False,
    )
    assert bad.by_key(rd.CONDITION_NO_BLOCKING_OPEN).reason == (
        "2 项阻断问题待处置：k1、k2"
    ), "应列出**具体**的冲突 id，而不是只给个数"
    assert bad.by_key(rd.CONDITION_PRODUCIBLE_SCENE).reason == (
        "没有任何场景可生产（可生产 = 场景状态为 PRODUCIBLE 且至少引用一项已确认事实）。"
    ), "空场景的文案必须解释'可生产'的定义（否则运营不知道怎样才能绿）"
    assert bad.by_key(rd.CONDITION_OPERATOR_CONFIRMED).reason == (
        "运营尚未明确点击确认（PRD §7.3 条件③ 要求人工确认，不可由系统代判）。"
    ), "必须说明'不可由系统代判' —— 否则未来会有人想自动置真"


def test_readiness_by_key_raises_on_unknown_key() -> None:
    """未知 key **抛 `KeyError`**（不返回 `None`）。

    返回 `None` 会让调用方写 `if cond and cond.satisfied` ——
    一个拼错的 key 就此恒为"未满足"，表现为"永远创建不了基线"
    且没有任何人知道原因。
    """
    status = rd.step3_readiness(claims=(), conflicts=(), scenes=(), operator_confirmed=False)
    with pytest.raises(KeyError) as caught:
        status.by_key("NOPE")
    assert "blockingResolved" in str(caught.value), "报错应提示合法取值"


def test_pick_field_tolerates_both_spellings() -> None:
    """`pick_field(row, keys)` 取第一个存在的键；都不存在返回 `None`。

    容忍两种拼写**不是"随便"**：草稿由 API 写入（camelCase），
    也可能由数据回填或测试夹具直写（snake_case）。精确到只认一种，
    会让另一条路径静默地"没有阻断项"（最坏的失效方向）。
    """
    assert rd.pick_field({"factRefs": [1]}, rd.FACT_REF_KEYS) == [1]
    assert rd.pick_field({"fact_refs": [2]}, rd.FACT_REF_KEYS) == [2]
    assert rd.pick_field({"factRefs": [1], "fact_refs": [2]}, rd.FACT_REF_KEYS) == [1], (
        "两种都在时以 FACT_REF_KEYS 的顺序为准（camelCase 在前）"
    )
    assert rd.pick_field({}, rd.FACT_REF_KEYS) is None
    assert rd.pick_field({"other": 1}, rd.FACT_REF_KEYS) is None
    # keys 是**位置参数元组**（曾误传成两个参数）
    assert rd.pick_field({"id": "x"}, rd.CLAIM_ID_KEYS) == "x"


def test_confirmed_claim_ids_only_confirmed() -> None:
    """`confirmed_claim_ids` **只收 `CONFIRMED`**。

    `CONFLICT`/`PENDING` 不算 —— 一个"带着冲突的事实"支撑的场景不是
    可生产场景，它是下一个待处置的阻断项。这条判据若放宽，
    基线里会纳入未确认的事实，而**没有任何地方会报错**。
    """
    claims = (
        {"claimId": "c1", "state": CLAIM_STATE_CONFIRMED},
        {"claimId": "c2", "state": "PENDING"},
        {"claimId": "c3", "state": "CONFLICT"},
        {"claimId": "c4", "state": CLAIM_STATE_CONFIRMED},
        {"statement": "没有 id 的行不进集合"},
        {"claimId": "c6"},  # 状态缺失
    )
    assert rd.confirmed_claim_ids(claims) == frozenset({"c1", "c4"})
    assert rd.confirmed_claim_ids(()) == frozenset()


def test_producible_scene_ids_requires_state_and_confirmed_fact() -> None:
    """可生产 = 状态 `PRODUCIBLE` **且** 至少引用一项已确认事实。

    四种组合逐一对上 —— 这是"可生产"定义的**唯一**实现，
    服务层的"事实纳入"也读同一个 `confirmed_claim_ids`。
    """
    claims = (
        {"claimId": "c1", "state": CLAIM_STATE_CONFIRMED},
        {"claimId": "c2", "state": "PENDING"},
    )
    scenes = (
        # 满足：PRODUCIBLE + 引用已确认事实
        {"sceneId": "s_ok", "state": SCENE_STATE_PRODUCIBLE, "factRefs": ["c1"]},
        # 状态不对
        {"sceneId": "s_bad_state", "state": "DISABLED", "factRefs": ["c1"]},
        # 只引用未确认的事实
        {"sceneId": "s_unconfirmed", "state": SCENE_STATE_PRODUCIBLE, "factRefs": ["c2"]},
        # 没有引用
        {"sceneId": "s_no_ref", "state": SCENE_STATE_PRODUCIBLE},
        # snake_case 也要认
        {"scene_id": "s_snake", "state": SCENE_STATE_PRODUCIBLE, "fact_refs": ["c1"]},
    )
    got = rd.producible_scene_ids(scenes, claims)
    assert got == ("s_ok", "s_snake"), f"实际 {got}"
    # 没有任何已确认事实 → 短路为空（显式语义）
    assert rd.producible_scene_ids(scenes, ()) == ()


def test_blocking_open_conflicts_only_blocking_and_unsettled() -> None:
    """未处置的**阻断**冲突：`blocking=True` 且 `state != SETTLED`。

    非阻断冲突（例如"时效存疑"）不阻止创建基线 —— 否则"提示"与"阻断"
    就没有区别了，而 AC-09 说的是**阻断项**。
    """
    conflicts = (
        {"conflictId": "k_open", "blocking": True, "state": CONFLICT_STATE_OPEN},
        {"conflictId": "k_settled", "blocking": True, "state": CONFLICT_STATE_SETTLED},
        {"conflictId": "k_nonblocking", "blocking": False, "state": CONFLICT_STATE_OPEN},
        {"conflictId": "k_no_state", "blocking": True},  # 状态缺失 ⇒ 视为未处置
    )
    assert rd.blocking_open_conflicts(conflicts) == ("k_open", "k_no_state")
    assert rd.blocking_open_conflicts(()) == ()


def test_step3_readiness_of_reads_camel_and_snake() -> None:
    """`step3_readiness_of(draft)` 从草稿 dict 取字段并转调。

    `operatorConfirmed` / `operator_confirmed` 两种写法都认
    —— 与 `pick_field` 同一条取向。它**只取字段**，不含任何判定
    （判定在 `step3_readiness()`，一处）。
    """
    draft = {
        "claims": [{"claimId": "c1", "state": CLAIM_STATE_CONFIRMED}],
        "conflicts": [],
        "scenes": [{"sceneId": "s1", "state": SCENE_STATE_PRODUCIBLE, "factRefs": ["c1"]}],
        "operatorConfirmed": True,
    }
    assert rd.step3_readiness_of(draft).ready is True
    assert rd.step3_readiness_of({"operator_confirmed": True}).by_key(
        rd.CONDITION_OPERATOR_CONFIRMED
    ).satisfied is True
    assert rd.step3_readiness_of({}).ready is False
    # 两种拼写同时存在时，camelCase 优先（与 API 写入的形状一致）
    assert rd.step3_readiness_of(
        {"operatorConfirmed": True, "operator_confirmed": False}
    ).by_key(rd.CONDITION_OPERATOR_CONFIRMED).satisfied is True


# --- 1.3 `context_order` 纯判据 ---------------------------------------------


def test_priority_order_matches_prd_five_items() -> None:
    """PRD §15.4 五项顺序，唯一权威定义。

    语法上是 dict，语义上是**有序清单** —— 因此断言的是 `list(...)` 的顺序，
    不是 `set`。顺序就是"裁剪时谁先被保留"。
    """
    assert list(co.CONTEXT_PRIORITY_ORDER) == [
        co.CONTEXT_SOURCE_PROJECT_FACTS,
        co.CONTEXT_SOURCE_PRODUCT_ASSETS,
        co.CONTEXT_SOURCE_SCENES_AND_RULES,
        co.CONTEXT_SOURCE_STYLES,
        co.CONTEXT_SOURCE_CROSS_PROJECT_METHODS,
    ]
    assert list(co.CONTEXT_PRIORITY_ORDER) == [
        "PROJECT_FACTS",
        "PRODUCT_ASSETS",
        "SCENES_AND_RULES",
        "STYLES",
        "CROSS_PROJECT_METHODS",
    ], "键名是受控取值（WP26 会读）"
    assert list(co.CONTEXT_PRIORITY_ORDER.values()) == [1, 2, 3, 4, 5]
    assert co.TOP_PRIORITY == 1
    assert co.is_top_priority(co.CONTEXT_SOURCE_PROJECT_FACTS) is True
    assert co.is_top_priority(co.CONTEXT_SOURCE_STYLES) is False


def test_item_kind_to_source_mapping_verbatim() -> None:
    """`item_kind` → 动态上下文档位，四条逐字。

    表列 `baseline_items.context_order` 是 NOT NULL，而
    `ItemDraft.context_order` 可空 —— 空的时候填什么由这张表唯一决定。
    """
    assert co.ITEM_KIND_CONTEXT_SOURCE == {
        "FACT": "PROJECT_FACTS",
        "SCENE": "SCENES_AND_RULES",
        "RULE": "SCENES_AND_RULES",
        "STYLE": "STYLES",
    }
    assert co.context_source_of("FACT") == "PROJECT_FACTS"
    assert co.context_source_of("RULE") == "SCENES_AND_RULES"
    assert co.order_for_item_kind("FACT") == 1
    assert co.order_for_item_kind("RULE") == 3
    assert co.order_for_item_kind("SCENE") == 3
    assert co.order_for_item_kind("STYLE") == 4


def test_two_deliberate_gaps_are_exact_sets() -> None:
    """两处缺口各由**显式集合**钉住（不是"其余全部"的兜底桶）。

    兜底桶**永远不会告警**：一个新的来源被加进来时会静默排到末尾，
    而没有任何人知道 PRD §15.4 的意思是"它该排第几"。
    这里做**等式**断言 —— 新增一个 `ITEM_KIND` 或一档 `source` 时，
    不回答"它排第几 / 它从哪来"就会红。
    """
    assert co.UNMAPPED_ITEM_KINDS == frozenset({"RESEARCH"}), (
        "研究资料在原型里属于另一张表（快照清单），不在 §15.4 五项内"
    )
    assert co.UNSOURCED_CONTEXT_SOURCES == frozenset(
        {"CROSS_PROJECT_METHODS", "PRODUCT_ASSETS"}
    ), "这两档讲的是别的东西（知识资产本身 / 其他项目的方法）"
    # 两处缺口都不是"空集占位" —— 空集的等式断言是恒真的假判据
    assert co.UNMAPPED_ITEM_KINDS, "缺口集合不能为空，否则这条断言什么也没测"
    assert co.UNSOURCED_CONTEXT_SOURCES


def test_mapping_plus_gaps_exactly_covers_all_item_kinds() -> None:
    """`已映射 + 刻意未映射` **恰好**等于全部 `ITEM_KINDS`，且两者不交。

    这是本模块的两条缺口"是事实而非遗漏"的机械保证：
    新增一个 `ITEM_KIND` 若不表态落在哪一边，这条就红。
    """
    mapped = set(co.ITEM_KIND_CONTEXT_SOURCE)
    unmapped = set(co.UNMAPPED_ITEM_KINDS)
    assert not (mapped & unmapped), f"既映射又未映射：{mapped & unmapped}"
    assert mapped | unmapped == set(bt.ITEM_KINDS), (
        f"并集应等于 ITEM_KINDS={set(bt.ITEM_KINDS)}，"
        f"差集={set(bt.ITEM_KINDS) - (mapped | unmapped)}"
    )
    assert len(mapped) + len(unmapped) == len(bt.ITEM_KINDS)
    # 同样是"两个方向都闭嘴"的反向：unmapped 必须真的从 ITEM_KINDS 里来
    assert unmapped <= set(bt.ITEM_KINDS)


def test_order_for_item_kind_raises_on_unmapped_kind() -> None:
    """`RESEARCH` 没有档位 —— **抛错**，不静默排末尾。

    这个错误会在**创建基线那一刻**暴露（服务层写 `context_order` 时调它），
    而不是在 WP26 组装上下文时变成一个静默的末尾项。
    """
    for bad in ("RESEARCH", "NOPE"):
        with pytest.raises(ValueError) as caught:
            co.order_for_item_kind(bad)
        assert "没有对应的动态上下文档位" in str(caught.value), str(caught.value)
    assert issubclass(co.UnknownItemKindError, ValueError), (
        "刻意不用 ApiError：这是编程错误，不是用户输入错误"
    )
    # 已知的种类不该抛
    for kind in co.ITEM_KIND_CONTEXT_SOURCE:
        assert isinstance(co.order_for_item_kind(kind), int)


def test_priority_of_and_context_source_of_raise_on_unknown() -> None:
    """未知档位/未知种类都**抛错**（白名单而非兜底桶）。"""
    with pytest.raises(co.UnknownContextSourceError) as caught:
        co.priority_of("NOPE")
    assert "未知的动态上下文来源" in str(caught.value)
    assert "PROJECT_FACTS" in str(caught.value), "报错应列出合法取值"
    assert issubclass(co.UnknownContextSourceError, ValueError)
    with pytest.raises(co.UnknownItemKindError):
        co.context_source_of("NOPE")


def test_unknown_sources_reports_only_illegal_sorted() -> None:
    """`unknown_sources(iterable)` 只报不在受控域里的项，且**排序**（可复现）。"""
    assert co.unknown_sources([]) == ()
    assert co.unknown_sources(["PROJECT_FACTS", "STYLES"]) == ()
    assert co.unknown_sources(["ZZZ", "PROJECT_FACTS", "AAA"]) == ("AAA", "ZZZ"), (
        "去重并排序 —— 报错文本要稳定，否则测试与日志都不可比对"
    )
    assert co.unknown_sources(["AAPL", "AAPL"]) == ("AAPL",), "应去重"


def test_order_context_items_priority_then_id_and_order_independent() -> None:
    """排序键是 `(priority, item_id)` 两元组。

    **只按 priority 排是不够的**：同一档内多条（例如 12 项事实）的相对顺序
    会取决于输入顺序 —— 而输入顺序取决于调用方怎么拼这个 tuple，
    于是同一个基线在两个读取路径下会得到两种上下文顺序。
    """
    items = (
        co.ContextItem(source="STYLES", item_id="z"),
        co.ContextItem(source="PROJECT_FACTS", item_id="b"),
        co.ContextItem(source="SCENES_AND_RULES", item_id="m"),
        co.ContextItem(source="PROJECT_FACTS", item_id="a"),
        co.ContextItem(source="CROSS_PROJECT_METHODS", item_id="q"),
    )
    ordered = co.order_context_items(items)
    assert [i.item_id for i in ordered] == ["a", "b", "m", "z", "q"], (
        "先按 priority 分档，档内按 item_id 字符串序"
    )
    # 幂等
    assert co.order_context_items(ordered) == ordered
    # 与输入顺序无关：把输入倒过来，结果相同
    assert co.order_context_items(tuple(reversed(items))) == ordered
    # 不改输入
    assert [i.item_id for i in items] == ["z", "b", "m", "a", "q"]
    assert co.order_context_items(()) == ()


def test_order_context_items_rejects_illegal_even_when_it_would_be_sliced_out() -> None:
    """非法来源**先整体检查**再排序，不靠排序过程中的抛错。

    若只靠 `sorted()` 里的比较抛错：输入为空、或非法项恰好被短路，
    非法值就漏过去了（ISS-78 同族：判据被数据形状绕过）。
    用**排在最后**的非法项钉住这一点。
    """
    with pytest.raises(co.UnknownContextSourceError) as caught:
        co.order_context_items(
            (
                co.ContextItem(source="PROJECT_FACTS", item_id="a"),
                co.ContextItem(source="ZZZ_LAST", item_id="zzz"),
            )
        )
    assert "ZZZ_LAST" in str(caught.value), f"应点名非法来源：{caught.value}"


# --- 1.4 `baseline_types` / `models` 不变量 ----------------------------------


def test_immutable_tables_excludes_information_drafts() -> None:
    """`IMMUTABLE_TABLES` 恰好三张，**不含** `information_drafts`。

    草稿是**可反复修改**的工作区（`PUT` 每次覆盖并 +1 `lock_version`）；
    基线/基线项/批准是**不可变记录**。把草稿误列入不可变，
    会让"保存草稿"整个功能失效 —— 而它会是 500 而不是某个明确的拒绝。
    """
    assert IMMUTABLE_TABLES == (
        "production_baselines",
        "baseline_items",
        "step_approvals",
    )
    assert "information_drafts" not in IMMUTABLE_TABLES
    # 与不可变触发器的所属表**一致** —— 两处名单漂移 = 某张表实际没人守
    trigger_tables = tuple(item[0] for item in bs_ddl.all_trigger_ddl()[:3])
    assert trigger_tables == IMMUTABLE_TABLES, (
        f"不可变表名单与不可变触发器的所属表必须一致："
        f"{IMMUTABLE_TABLES} vs {trigger_tables}"
    )


def test_own_tables_exist_in_metadata_and_match_immutability() -> None:
    """本包四张表都在 metadata 里；三张不可变表在 `IMMUTABLE_TABLES` 名单内。"""
    names = {t.name for t in OWN_TABLES}
    assert names == {
        "information_drafts",
        "production_baselines",
        "baseline_items",
        "step_approvals",
    }
    assert names - {"information_drafts"} == set(IMMUTABLE_TABLES)
    # 表名与 DDL 里引用的表名一致（拼错表名时 DDL 会装到不存在的表上）
    ddl_tables = {item[0] for item in bs_ddl.all_trigger_ddl()}
    assert ddl_tables <= names | {"version_nodes"}, (
        f"DDL 引用了本包四表之外的表：{ddl_tables - names - {'version_nodes'}}"
    )
    assert "version_nodes" in ddl_tables, "反向触发器必须挂在共用表上"


def test_subject_kind_entity_type_covers_all_subject_kinds() -> None:
    """`_SUBJECT_KIND_ENTITY_TYPE` 的键集合 == `SUBJECT_KINDS`。

    这是本包最易漂移的一处：新增一个 `SUBJECT_KIND` 而不表态
    "它对应哪个 `entity_type`"，会让 `approve_step` 拿到一个
    `None` 版本类型并**静默**跳过类型校验（`RESEARCH` 就是 None）。
    """
    assert set(bs._SUBJECT_KIND_ENTITY_TYPE) == set(SUBJECT_KINDS)
    assert bs.SUBJECT_KINDS == (
        SUBJECT_KIND_BRIEF,
        SUBJECT_KIND_RESEARCH,
        SUBJECT_KIND_BASELINE,
    )
    assert bs._SUBJECT_KIND_ENTITY_TYPE == {
        "BRIEF": ENTITY_TYPE_BRIEF,
        "BASELINE": ENTITY_TYPE_BASELINE,
        "RESEARCH": None,
    }, (
        "RESEARCH 没有对应的版本实体类型（研究资料不是版本化实体）—— "
        "这个 None 是事实，不是遗漏；但它意味着 RESEARCH 的版本 id 不被校验"
    )
    # None 的语义要显式说明：值为 None 的种类不该出现在"需校验"的集合里
    untyped = {k for k, v in bs._SUBJECT_KIND_ENTITY_TYPE.items() if v is None}
    assert untyped == {"RESEARCH"}, (
        "若有第二个种类变成 None，'哪些种类不做类型校验'就变了而无人知道"
    )


def test_controlled_vocabularies_are_unique_and_nonempty() -> None:
    """受控取值域无重复、非空。

    重复的取值不会报错，只会让 `IN (...)` 多一项 ——
    而"哪个是权威"变成看谁排前面。
    """
    for name, values, expected_len in (
        ("ITEM_KINDS", ITEM_KINDS, 5),
        ("SUBJECT_KINDS", SUBJECT_KINDS, 3),
        ("STEP_NAMES", tuple(bs.STEP_NAMES.values()), bs.STEP_MAX - bs.STEP_MIN + 1),
    ):
        assert len(values) == len(set(values)), f"{name} 有重复：{values}"
        assert len(values) == expected_len, f"{name} 应有 {expected_len} 项：{values}"
    assert set(bs.STEP_NAMES) == set(bs.STEP_NUMBERS)
    assert bs.STEP_NUMBERS == tuple(range(bs.STEP_MIN, bs.STEP_MAX + 1))
    assert bs.CONFLICT_ACTIONS and len(set(bs.CONFLICT_ACTIONS)) == len(
        bs.CONFLICT_ACTIONS
    )


def test_public_drafts_are_frozen_and_slotted() -> None:
    """对外契约的 dataclass 全部 `frozen=True, slots=True`。

    这是本项目的一条通用纪律：派生的视图/草稿对象一旦可变，
    调用方就可能就地改它，而**改动会跨层泄漏**（服务层返回的对象
    被 API 层改写，服务层的假设随之失效）。
    """
    for name in (
        "ItemDraft",
        "BaselineDraft",
        "StepApprovalDraft",
        "DraftView",
        "BaselineSummary",
        "BaselineDetail",
        "BaselineItemView",
        "ApprovalView",
        "CreatedBaseline",
        "ConflictDecision",
        "ContextItem",
    ):
        cls = getattr(bt, name, None)
        assert cls is not None, f"baseline_types 缺少 {name}"
        assert dataclasses.is_dataclass(cls), f"{name} 应是 dataclass"
        params = cls.__dataclass_params__  # type: ignore[attr-defined]
        assert params.frozen is True, f"{name} 应当 frozen"
        assert "__slots__" in vars(cls), f"{name} 应当 slots"
    assert dataclasses.is_dataclass(co.ContextItem)
    assert co.ContextItem.__dataclass_params__.frozen is True  # type: ignore[attr-defined]


def test_creation_and_preview_share_one_readiness_function(monkeypatch: Any) -> None:
    """API 的预检与创建时的拒绝**调的是同一个函数**（D6 单一来源）。

    **为什么不是"结果相等"的断言**：两份恰好写得一样的拷贝会骗过它
    （WP19 §6 `test_gap_categories_come_from_wp18_constant` 同款）。
    这里用替身记录调用 —— 任何一处自己算都会让计数不对。
    """
    calls: list[dict[str, Any]] = []
    real = rd.step3_readiness

    def spy(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(rd, "step3_readiness", spy)

    class _FakeDraft:
        claims = ({"claimId": "c1", "state": CLAIM_STATE_CONFIRMED},)
        conflicts = ()
        scenes = ()
        operator_confirmed = True

    # ① API 预检路径（`step3_status_of_draft`）
    status = bs.step3_status_of_draft(_FakeDraft())
    assert len(calls) == 1, "预检路径应当只调一次 readiness.step3_readiness"
    assert status.ready is False, "scenes 为空 ⇒ 条件②不满足"

    # ② 创建路径的拒绝判据（`_require_readiness_for_creation`）
    _expect_api_error(
        lambda: bs._require_readiness_for_creation(_FakeDraft()),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_STEP3_NOT_READY,
    )
    assert len(calls) == 2, (
        "创建路径也必须经同一个 readiness.step3_readiness —— "
        "若它自己写了一遍 all(...)，计数就不会涨"
    )


def test_draft_row_validators_share_one_preamble() -> None:
    """`claims` / `conflicts` / `scenes` 的"必须是数组 + 条数上限"**只有一处实现**。

    这三段此前逐字各写一遍（同一事实三处实现）。危害不在于啰嗦：
    三条消息里的 `field` 与 `reason` 各写一遍，就有三次写错的机会，
    而写错的后果是**方向错误的提示**（报成别的字段，或复用别的 reason 码）
    —— 与 `_require_decision` 特意不复用 `REASON_SUBJECT_KIND_INVALID`
    是同一条理由，也与本包 CR 里"错误码必须指向真正出错的字段"一脉相承。

    判据用 AST 检查：三个函数体内**不得**再出现 `isinstance(..., list)`
    与 `> MAX_DRAFT_ITEMS` 这种前导判断 —— 它们必须经 `_require_rows`。
    """
    source = Path(bs.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    targets = {"_require_claims", "_require_conflicts", "_require_scenes"}
    found = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in targets:
            continue
        found += 1
        # 函数体（去 docstring）里不得有 list 类型判断或条数上限判断。
        # 只排除"单行必须是对象"那种（`isinstance(x, dict)`），
        # 它是**每条明细行**的校验，与公共前导是两件事。
        body = [
            stmt
            for stmt in node.body
            if not (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            )
        ]
        offending: list[str] = []
        for stmt in body:
            for sub in ast.walk(stmt):
                if not isinstance(sub, ast.If):
                    continue
                text = ast.unparse(sub.test)
                if "MAX_DRAFT_ITEMS" in text:
                    offending.append(f"条数上限：{text}")
                elif "isinstance" in text and ", list)" in text:
                    offending.append(f"数组类型：{text}")
        assert "_require_rows" in ast.unparse(node), (
            f"{node.name} 没有走 _require_rows —— 前导校验又变成两处实现了"
        )
        assert not offending, (
            f"{node.name} 里仍有自己的前导判断（应与 _require_rows 同源）：\n"
            + "\n".join(f"  {x}" for x in offending)
        )

    assert found == 3, (
        f"只找到 {found} 个目标函数（期望 3）—— 扫描范围可能已缩水"
    )


def test_draft_key_tuples_come_from_readiness_constants() -> None:
    """草稿字段名元组**只有一处定义**（`readiness`），`service.py` 必须引用它。

    ## 为什么这条判据必须有，且必须用 AST 而不是文本

    同一组候选键（`conflictId` / `conflict_id` / `id`）此前**出现在两处**：
    `readiness.blocking_open_conflicts` 与 `service._require_conflicts`。
    两处各写一遍元组时，漂移**完全不报错**：若 `service.py` 只认
    `conflictId` 而 `readiness` 认三种，同一条草稿会"保存时通过、
    算完成条件时 id 取不到而静默丢弃" —— 症状是条件①永远为绿，
    而"存在阻断项"这个判断失效（AC-09 的防线开了口子）。

    ## 判据为什么是"第二参数不是字面量元组"

    文本判据（如"源码里不得出现 `conflict_id`"）**对漂移毫无区分力**：
    把 `service.py` 的引用改回现写元组，只要那次改写恰好没漏掉某个拼写，
    文本判据就 PASS —— 但它已经与 `readiness` 常量脱钩了（改常量不再联动）。
    真正要钉住的是**引用关系**，不是字面量集合。

    因此这里用 AST 检查：凡调用 `readiness.pick_field(...)` 的地方，
    第二个实参必须是 `readiness.<CONST>` 形式的属性访问。
    任何现写的 `("a", "b")` 元组都会被抓到 —— 因为元组没有单一来源，
    它改不动、也不会跟着常量变。
    """
    source = Path(bs.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    literal_tuples: list[tuple[int, str]] = []
    attribute_refs: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # 只看 `readiness.pick_field(...)`
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "pick_field"
            and isinstance(func.value, ast.Name)
            and func.value.id == "readiness"
        ):
            continue
        assert len(node.args) >= 2, (
            f"service.py:{node.lineno} 的 pick_field 调用缺第二个参数"
        )
        second = node.args[1]
        if isinstance(second, ast.Attribute) and isinstance(second.value, ast.Name):
            assert second.value.id == "readiness", (
                f"service.py:{node.lineno} 的键元组取自 {second.value.id}，"
                "应当取自 readiness（唯一来源）"
            )
            attribute_refs.append(f"{second.value.id}.{second.attr}")
        elif isinstance(second, (ast.Tuple, ast.List)):
            literal_tuples.append(
                (node.lineno, ast.unparse(second))  # type: ignore[attr-defined]
            )
        else:
            raise AssertionError(
                f"service.py:{node.lineno} 的键元组形态未知："
                f"{ast.dump(second)[:80]} —— 判据需要跟着更新"
            )

    assert not literal_tuples, (
        "service.py 里仍有**现写的**键元组（应与 readiness 常量同源）：\n"
        + "\n".join(f"  第 {ln} 行：{src}" for ln, src in literal_tuples)
    )
    # 下界断言：本包当前至少引用两处（conflicts / scenes）。
    # 没有它，把调用整段删掉也能让这条 PASS（扫描范围静默缩水）。
    assert len(attribute_refs) >= 2, (
        f"引用常量处的数量 {len(attribute_refs)} 少于下界 2 —— "
        "扫描范围可能已缩水（ISS-107 同款：范围缩水不报错）"
    )


def test_draft_key_constants_cover_both_spellings() -> None:
    """两组键常量都认 camelCase 与 snake_case（容忍两种来源）。

    容忍两种写法**不是**"随便"：草稿 JSONB 由 API 写入（camelCase），
    也可能由数据回填或测试夹具直接写库（snake_case）。
    只认一种会让另一条路径**静默地"没有阻断项"**（最坏的失效方向）。

    这条与上一条是**一对**：上一条钉住"来源唯一"，
    这一条钉住"来源本身的内容对" —— 只钉前者的话，
    一个恰好写成空元组的常量也能通过。
    """
    for name, keys in (
        ("CONFLICT_ID_KEYS", rd.CONFLICT_ID_KEYS),
        ("SCENE_ID_KEYS", rd.SCENE_ID_KEYS),
    ):
        assert len(keys) >= 2, f"{name} 至少要认两种拼写，实际 {keys!r}"
        assert any("_" in k for k in keys), (
            f"{name}={keys!r} 不含 snake_case —— 直接写库的路径会取不到 id"
        )
        assert any("_" not in k for k in keys), f"{name}={keys!r} 不含 camelCase"
        assert "id" in keys, (
            f"{name}={keys!r} 不含裸 'id' 兜底键（原型用的是裸 id）"
        )

    # 钉住具体内容：这两组是「服务层保存」与「readiness 算条件」共用的契约，
    # 任何一侧改拼写都必须同时改到另一端（它们现在是同一个对象）。
    assert rd.CONFLICT_ID_KEYS == ("conflictId", "conflict_id", "id")
    assert rd.SCENE_ID_KEYS == ("sceneId", "scene_id", "id")


# ===========================================================================
# 第 2 层 —— 服务层编排（真库 + 真事务）
# ===========================================================================
#
# 这一层测的是**只有真事务里才成立**的东西：部分更新的语义、乐观锁的
# 两层判定、七步链路的顺序、延迟约束在 COMMIT 时才跑。
#
# 判据纪律：本层断言的是**服务层的拒绝形态**（可读 4xx + `reason`），
# 不是"数据库会不会拦" —— 后者是第 3 层的事。两层分开是因为
# "有可读错误"与"有防线"是两件事：只有数据库那层能拦住绕过 ORM 的写入，
# 而只有服务层那层能给出前端可分支的错误码。


# --- 2.0 基础设施（第 2 层专用） -------------------------------------------


def _update_draft_raw(
    env: BaselineEnv,
    *,
    expected_lock_version: int,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
    **blocks: Any,
) -> Any:
    """直接调 `service.update_draft` 并**显式指定期望版本**。

    `_write_draft` 用的是"读到什么就传什么"，因此它永远走成功路径 ——
    而乐观锁的判据（`expected_lock_version` 不符 → 412）需要故意传一个
    **过期的**版本号，所以这里必须能把版本号从调用方传进来。

    `actor_id` / `request_id` **默认都不传**：服务层对二者的要求是
    `and`（两者都非空才写审计），而本包写审计的三处都没有配对 `before`
    （MAJOR-5），传进去会撞 CHECK。需要测审计的用例显式传这两个。
    """
    with session_scope(env.settings) as session:
        kwargs: dict[str, Any] = {}
        if actor_id is not None:
            kwargs["actor_id"] = actor_id
        if request_id is not None:
            kwargs["request_id"] = request_id
        return bs.update_draft(
            BaselineRepository(session),
            project_id=env.project_id,
            expected_lock_version=expected_lock_version,
            **blocks,
            **kwargs,
        )


def _draft_row(env: BaselineEnv, *, project_id: uuid.UUID | None = None) -> dict[str, Any]:
    """读工作区的**原始列**（绕过 ORM 的身份映射，避免读到过期实例）。"""
    rows = _raw(
        env,
        "select lock_version, brief_version_id, claims, scenes,"
        " operator_confirmed from information_drafts where project_id = :pid",
        {"pid": env.project_id if project_id is None else project_id},
    )
    assert rows, "工作区应当已存在"
    row = rows[0]
    return {
        "lock_version": row[0],
        "brief_version_id": row[1],
        "claims": row[2],
        "scenes": row[3],
        "operator_confirmed": row[4],
    }


def _baseline_count(env: BaselineEnv, *, project_id: uuid.UUID | None = None) -> int:
    return int(
        _scalar(
            env,
            "select count(*) from production_baselines where project_id = :pid",
            {"pid": env.project_id if project_id is None else project_id},
        )
        or 0
    )


def _active_baseline(
    env: BaselineEnv, *, project_id: uuid.UUID | None = None
) -> uuid.UUID | None:
    """读投影列（原生 SQL，不经过 service）。"""
    value = _scalar(
        env,
        "select active_baseline_id from projects where id = :pid",
        {"pid": env.project_id if project_id is None else project_id},
    )
    return None if value is None else uuid.UUID(str(value))


def _rebuild_active(
    env: BaselineEnv, *, project_id: uuid.UUID | None = None
) -> uuid.UUID | None:
    with session_scope(env.settings) as session:
        return bs.rebuild_active_baseline(
            BaselineRepository(session),
            project_id=env.project_id if project_id is None else project_id,
        )


def _node_entity_type_of(env: BaselineEnv, node_id: uuid.UUID) -> str | None:
    value = _scalar(
        env,
        "select entity_type from version_nodes where id = :nid",
        {"nid": node_id},
    )
    return None if value is None else str(value)


def _node_entity_id_of(env: BaselineEnv, node_id: uuid.UUID) -> uuid.UUID | None:
    value = _scalar(
        env,
        "select entity_id from version_nodes where id = :nid",
        {"nid": node_id},
    )
    return None if value is None else uuid.UUID(str(value))


def _audit_count(env: BaselineEnv, *, action: str, object_id: uuid.UUID) -> int:
    return int(
        _scalar(
            env,
            "select count(*) from audit_events"
            " where action = :action and object_id = :oid",
            {"action": action, "oid": object_id},
        )
        or 0
    )


#: 一个 FACT 条目（`context_order` 由 `context_order.py` 派生 = 1）。
def _fact_item(**overrides: Any) -> ItemDraft:
    fields: dict[str, Any] = {
        "item_kind": ITEM_KIND_FACT,
        "ref_label": "事实 v1",
        "snapshot": {"statement": "续航 18 小时", "status": "FILLED"},
    }
    fields.update(overrides)
    return ItemDraft(**fields)


#: 一个 SCENE 条目（派生 `context_order` = 3）。
def _scene_item(**overrides: Any) -> ItemDraft:
    fields: dict[str, Any] = {
        "item_kind": ITEM_KIND_SCENE,
        "ref_label": "场景 v1",
        "snapshot": {"audience": "通勤族", "state": SCENE_STATE_PRODUCIBLE},
    }
    fields.update(overrides)
    return ItemDraft(**fields)


def _ready_and_baseline(
    env: BaselineEnv,
    *,
    project_id: uuid.UUID | None = None,
    selected: tuple[ItemDraft, ...] | None = None,
    supersedes_id: uuid.UUID | None = None,
    change_reason: str = "首版基线",
    request_id: str | None = None,
) -> Any:
    """把工作区写成 ready，并创建一条基线（第 2 层最常用的两步）。

    **每次都新建 Brief 节点**：`brief_version_id` 只是"钉住哪一版"，
    重复用同一个节点会让"两次创建是否同版"这件事不可区分。

    `project_id` 缺省为 `env.project_id`；显式传入可以在**另一个项目**
    下建一条基线 —— 那是"`supersedesId` 指向别的项目"这条判据的
    必要前提（服务层会先查目标存在、再比 `project_id`，因此
    一个凭空生成的 uuid 只会走到"不存在"那一支，测不到"不属于本项目"）。
    """
    pid = env.project_id if project_id is None else project_id
    brief_node = _make_brief_node(env, project_id=pid)
    _make_ready(env, brief_version_id=brief_node)
    if pid != env.project_id:
        _write_draft(
            env,
            project_id=pid,
            claims=READY_CLAIMS,
            conflicts=READY_CONFLICTS,
            scenes=READY_SCENES,
            boundaries={"tone": "克制"},
            operator_confirmed=True,
            brief_version_id=brief_node,
        )
    draft = _baseline_draft(
        env,
        project_id=pid,
        brief_version_id=brief_node,
        selected=selected if selected is not None else (_fact_item(),),
        supersedes_id=supersedes_id,
        change_reason=change_reason,
    )
    kwargs: dict[str, Any] = {}
    if request_id is not None:
        kwargs["request_id"] = request_id
    return _create_baseline(env, draft, **kwargs)


# --- 2.1 审核工作区（懒创建 / 部分更新 / 乐观锁） ---------------------------


def test_require_draft_lazily_creates_once_per_project(env: BaselineEnv) -> None:
    """`require_draft` 是**幂等懒创建**：同一个项目永远只有一份工作区。

    这条判据同时钉住一个容易被忽略的事实：`GET .../information-draft`
    **会写库**（它调本函数）。若某天有人把它改成"不存在就 404"，
    运营第一次打开步骤 3 会拿不到任何数据来源，而 `If-Match` 的 ETag
    也无从获得 —— 于是保存必然 428，且**没有任何一处报错**。
    """
    assert _scalar(
        env,
        "select count(*) from information_drafts where project_id = :pid",
        {"pid": env.project_id},
    ) == 0, "夹具不应当预先建工作区 —— 否则这条测试测不到懒创建"

    first = _draft_of(env)
    second = _draft_of(env)

    assert first.id == second.id, "两次 require_draft 必须拿到同一份工作区"
    assert first.lock_version == 1, (
        "新建的工作区版本号从 1 起（`platform.base.lock_version_column` 的 "
        "`server_default=1`）—— 与 `build_etag` 的 `<id>:<version>` 形状同源"
    )
    assert first.operator_confirmed is False, "条件③默认未确认（不可由系统代判）"
    assert _scalar(
        env,
        "select count(*) from information_drafts where project_id = :pid",
        {"pid": env.project_id},
    ) == 1, "懒创建必须幂等（uq_information_drafts_project_id 兜底）"


def test_require_draft_rejects_unknown_project(env: BaselineEnv) -> None:
    """项目不存在 → 422 `baseline_project_not_found`（不是 404）。

    刻意不是 404：404 在本包里专指"工作区/基线不存在"，
    而"项目不存在"是**输入字段**的问题（`projectId`），与 WP18/WP20 同款。
    """
    with session_scope(env.settings) as session:
        _expect_api_error(
            lambda: bs.require_draft(
                BaselineRepository(session), project_id=uuid.uuid4()
            ),
            code=ErrorCode.VALIDATION_FAILED,
            reason=REASON_PROJECT_NOT_FOUND,
        )


def test_update_draft_partial_update_keeps_untouched_blocks(env: BaselineEnv) -> None:
    """部分更新：`None` = **本次不改这一块**（不是"改成空"）。

    这是本包最贵的一条语义 —— 把它写成"改成空"会让"只想处置一个冲突"
    的请求把运营几小时的 claims / scenes 全部**静默清空**
    （HTTP 200，响应里那几块也空着，看起来像保存成功）。
    """
    brief_node = _make_brief_node(env)
    _make_ready(env, brief_version_id=brief_node)

    before = _draft_row(env)
    assert len(before["claims"]) == 1 and len(before["scenes"]) == 1

    # 只改 scenes（其余全不传）
    after, status = _write_draft(
        env, scenes=[{"sceneId": "s2", "state": SCENE_STATE_DISABLED}]
    )

    row = _draft_row(env)
    assert len(row["scenes"]) == 1, "scenes 应当被这次请求替换掉"
    assert row["scenes"][0]["sceneId"] == "s2"
    assert len(row["claims"]) == 1, "claims 没被提到，必须原样保留"
    assert row["claims"][0]["claimId"] == "c1"
    assert row["operator_confirmed"] is True, "operatorConfirmed 没被提到，必须保留"
    assert row["lock_version"] == before["lock_version"] + 1
    assert status.ready is False, "scenes 换成 DISABLED 后条件②不再满足"


def test_update_draft_operator_confirmed_false_takes_effect(env: BaselineEnv) -> None:
    """`operatorConfirmed=false` 是**合法的显式值**（取消确认），不是"没提"。

    若实现里写成 `if operator_confirmed:`，这个动作会**静默无效** ——
    而条件③的语义正是"运营明确点过确认"，它永远为真意味着
    **创建基线的判据被绕过**（PRD §7.3 条件③）。
    """
    brief_node = _make_brief_node(env)
    _make_ready(env, brief_version_id=brief_node)
    assert _draft_row(env)["operator_confirmed"] is True

    _draft, status = _write_draft(env, operator_confirmed=False)

    assert _draft_row(env)["operator_confirmed"] is False, (
        "False 必须真的落库 —— 写成真值判断会让它静默无效"
    )
    condition = status.by_key(rd.CONDITION_OPERATOR_CONFIRMED)
    assert condition.satisfied is False, "取消确认后条件③必须变红"


def test_update_draft_empty_request_does_not_bump_lock_version(env: BaselineEnv) -> None:
    """空请求不递增 `lock_version`（一个字段都没改，凭什么让 ETag 失效）。

    若递增，客户端下一步提交会被 412 挡下 —— 而它其实什么都没做错。
    """
    _make_ready(env, brief_version_id=_make_brief_node(env))
    before = _draft_row(env)

    draft, status = _write_draft(env)  # 七块全 None

    assert _draft_row(env)["lock_version"] == before["lock_version"], (
        "空请求不得递增版本号"
    )
    assert draft.lock_version == before["lock_version"]
    assert status.ready is True


def test_update_draft_stale_version_raises_412_with_current_etag(
    env: BaselineEnv,
) -> None:
    """乐观锁的第二层：`expected_lock_version` 不符 → 412 并回带 `currentEtag`。

    **为什么这层不能省**：端点的 `require_if_match` 只保证"客户端拿的不是
    过期值"，它挡不住两个**同时通过**前置校验的请求 —— 那正是"乐观锁"
    这个词的全部含义。判定落在 `WHERE lock_version = ?` 的条件 UPDATE 上。
    """
    from rednote.platform.concurrency import build_etag

    _make_ready(env, brief_version_id=_make_brief_node(env))
    current = _draft_row(env)["lock_version"]
    draft_id = _draft_of(env).id

    # 先用一个正确的版本号写一次，把库里的版本推到 current + 1
    _write_draft(env, boundaries={"tone": "更克制"})

    # 再用**过期**的版本号写
    error = _expect_api_error(
        lambda: _update_draft_raw(
            env, expected_lock_version=current, boundaries={"tone": "过期写入"}
        ),
        code=ErrorCode.VERSION_CONFLICT,
    )
    assert error.details["reason"] == "lock_version_mismatch"
    assert error.details["resourceId"] == str(draft_id)
    assert error.details["currentEtag"] == build_etag(draft_id, current + 1), (
        "回带的必须是**当前** ETag —— 前端靠它不发第二次 GET 就能重试"
    )


@pytest.mark.regression
def test_partial_update_preserves_brief_version_id(env: BaselineEnv) -> None:
    """**Code Review MAJOR-3 的回归判据（先红后绿）**。

    ## 缺陷

    `repository.update_draft` 的 `values` 初始化里有

        values["brief_version_id"] = brief_version_id

    **不在** `if brief_version_id is not None:` 块内 —— 于是"只改 scenes"
    的请求会把 `brief_version_id` 无条件写成 `None`。

    ## 为什么它危险

    `brief_version_id` 是"这条基线钉住哪一版 Brief"的答案
    （原型 L60「BL-v3 绑定 Brief v2」）。它被静默清成 NULL 之后：

      · 保存仍然 HTTP 200，响应里 `briefVersionId` 是 `null`；
      · 运营屏幕上不会出现任何提示（它刚保存过，不会回头检查这一格）；
      · 而 `readiness` 的三条条件**都不看它** —— 于是基线照样能建出来，
        只是建出来的基线**不绑定任何 Brief 版本**，AC-24 的溯源链断在这里。

    典型的"静默失效"：写入成功、没有错误、数据少了一半。

    ## 期望

    只改 `scenes` 时 `brief_version_id` 必须**原样保留**。
    本判据在修复前会 FAIL。
    """
    brief_node = _make_brief_node(env)
    _make_ready(env, brief_version_id=brief_node)
    assert _draft_row(env)["brief_version_id"] == brief_node

    # 只改 scenes —— 一个字节都不碰 briefVersionId
    _write_draft(env, scenes=[{"sceneId": "s1", "state": SCENE_STATE_DISABLED}])

    assert _draft_row(env)["brief_version_id"] == brief_node, (
        "只改 scenes 不得把 briefVersionId 清成 NULL —— "
        "`repository.update_draft` 的无条件写入必须收进 `is not None` 块内"
    )


# --- 2.2 创建基线：七步链路的正例 -------------------------------------------


def test_create_baseline_registers_node_with_project_id_as_entity_id(
    env: BaselineEnv,
) -> None:
    """**`entity_id` 必须是 `project_id`**（本包最容易写错的一处）。

    `BASELINE` 的"逻辑实体"是**该项目的那条基线链**，不是"某一条基线" ——
    一条基线就是链上的一个修订。写成自己生成的 uuid 会让：

      · `trg_production_baselines_node_consistency` 判据 3 直接拒绝；
      · "同一项目的第二条基线"拿到修订号 1 而不是 2。

    这条判据**直接查 `version_nodes` 的两列**，而不是看返回值 ——
    返回值里 `baseline_id` 与 `revision_no` 都对不上"注册表的哪一行"。
    """
    created = _ready_and_baseline(env)

    assert created.created is True, "首次创建必须是新建（不是幂等命中）"
    assert created.revision_no == 1, "该项目的第 1 条基线 = 第 1 版"
    assert _node_entity_type_of(env, created.baseline_id) == ENTITY_TYPE_BASELINE
    assert _node_entity_id_of(env, created.baseline_id) == env.project_id, (
        "entity_id 必须是 projectId —— 传 baseline_id 会让第二条基线拿到修订号 1"
    )


def test_create_baseline_revision_no_increments_per_project(env: BaselineEnv) -> None:
    """修订号在**同一个 project 的基线链**上递增（1 → 2 → 3）。

    这条同时是 MAJOR-4 的**背景判据**：若 `newest_baseline_of` 恒返回链根，
    第三版会被 `_require_supersedes_target` 误拒（见 `_require_supersedes_target`
    的判据 2）—— 那时这条测试会以"第三版建不出来"的形式变红。
    """
    first = _ready_and_baseline(env)
    second = _ready_and_baseline(env, supersedes_id=first.baseline_id)

    assert (first.revision_no, second.revision_no) == (1, 2)
    assert second.superseded_baseline_id == first.baseline_id
    assert _baseline_count(env) == 2


def test_create_baseline_writes_seven_step_chain(env: BaselineEnv) -> None:
    """七步链路的**副作用清单**逐条核对（这是本包的主路径）。

    | 步骤 | 可核对的落点 |
    |---|---|
    | 6 注册版本节点 | `version_nodes` 一行（`entity_type=BASELINE`） |
    | 7 写基线行 | `production_baselines` 一行（`id` == node id） |
    | 7 写条目行 | `baseline_items` 两行（`item_no` 1/2） |
    | 7 同事务批准 | `step_approvals` 一条链根 `APPROVED`（OP-151） |
    | 7 切投影列 | `projects.active_baseline_id` |

    ⚠️ **审计不在这条测试里核对**：本包写审计的三处都**没有配对的
    `before`**，因此在 `request_id` 非空时整条链路会撞
    `ck_audit_events_hash_pair_consistent`（见
    `test_write_endpoints_survive_request_id`，MAJOR-5）。
    把那件事混进来会让这条"七步落点"判据变成"审计缺陷判据"，
    两者红的原因完全不同 —— 分开才诊断得动。
    审计的核对集中在 `test_audit_events_hash_pair_is_consistent`。
    """
    created = _ready_and_baseline(
        env, selected=(_fact_item(), _scene_item())
    )

    # 基线行与节点行**同 id**（"注册表与具体表"的配对方式，数据结构 02 §6）
    assert _scalar(
        env,
        "select count(*) from production_baselines"
        " where id = :bid and project_id = :pid",
        {"bid": created.baseline_id, "pid": env.project_id},
    ) == 1

    # 条目行：`item_no` 由给定顺序派生（1 起）
    items = _raw(
        env,
        "select item_no, item_kind, context_order, inclusion from baseline_items"
        " where baseline_id = :bid order by item_no",
        {"bid": created.baseline_id},
    )
    assert [(r[0], r[1], r[2], r[3]) for r in items] == [
        (1, ITEM_KIND_FACT, co.CONTEXT_PRIORITY_ORDER[co.CONTEXT_SOURCE_PROJECT_FACTS],
         INCLUSION_INCLUDED),
        (2, ITEM_KIND_SCENE,
         co.CONTEXT_PRIORITY_ORDER[co.CONTEXT_SOURCE_SCENES_AND_RULES],
         INCLUSION_INCLUDED),
    ], "context_order 必须由 order_for_item_kind 派生（FACT→1 / SCENE→3）"

    # 同事务批准（OP-151）：链根、APPROVED、步骤号 = BASELINE_STEP_NO
    approvals = _raw(
        env,
        "select step_no, subject_kind, subject_version_id, decision, supersedes_id"
        " from step_approvals where project_id = :pid",
        {"pid": env.project_id},
    )
    assert len(approvals) == 1, "创建基线必须同事务写一条批准"
    assert approvals[0][0] == bs_ddl.BASELINE_STEP_NO
    assert approvals[0][1] == SUBJECT_KIND_BASELINE
    assert uuid.UUID(str(approvals[0][2])) == created.baseline_id
    assert approvals[0][3] == APPROVAL_DECISION_APPROVED
    assert approvals[0][4] is None, "首次批准是链根（supersedes_id 为 NULL）"

    # 投影列
    assert _active_baseline(env) == created.baseline_id

    # 审计：见 docstring —— 本包写审计时未配对 `before`，
    # 传 `request_id` 会撞 `ck_audit_events_hash_pair_consistent`（MAJOR-5）。
    # 这里**不传 request_id**，因此**不应当**有审计行。
    assert _audit_count(
        env, action=AUDIT_BASELINE_CREATE, object_id=created.baseline_id
    ) == 0, (
        "服务层对 `actor_id is not None and request_id is not None` 是"
        "**同时**要求的 —— 不传 request_id 时不得写审计（否则审计里"
        "会出现没有请求号的行，回溯时无法定位是哪次调用）"
    )


def test_create_baseline_snapshot_has_exactly_seven_keys(env: BaselineEnv) -> None:
    """基线级快照的**七个键**（`_baseline_snapshot`）。

    刻意**不含**批准人/批准时间（由 `step_approvals` 回答 —— 再存一份
    就是第二个真值来源），也**不含** `activeBaselineId`
    （那会变；一条不可变基线里存一个会变的判断，建成那一刻就过期）。

    `readiness` 是 `{条件key: satisfied}` 的读数、`counts` 是各块规模 ——
    它们让半年后能回答"这条基线是在什么状态下建出来的"。
    """
    created = _ready_and_baseline(env)

    snapshot = _scalar(
        env,
        "select snapshot from production_baselines where id = :bid",
        {"bid": created.baseline_id},
    )
    assert set(snapshot) == {
        "schemaVersion",
        "rulesetVersion",
        "changeReason",
        "briefVersionId",
        "readiness",
        "counts",
        "boundaries",
    }, "快照键集合必须恰好是这七个"
    assert snapshot["schemaVersion"] == BASELINE_SCHEMA_VERSION
    assert snapshot["readiness"] == {
        key: True for key in rd.CONDITION_KEYS
    }, "创建成功意味着三条条件全绿（与 readiness 的判据同源）"
    assert snapshot["counts"] == {"claims": 1, "conflicts": 1, "scenes": 1}
    assert snapshot["boundaries"] == {"tone": "克制"}, "表达边界必须进快照"
    assert "approvedBy" not in snapshot and "approvedAt" not in snapshot, (
        "批准人/批准时间必须由 step_approvals 回答，不得在快照里存第二份"
    )
    assert "activeBaselineId" not in snapshot, (
        "不得在不可变快照里存一个会变的判断"
    )


def test_create_baseline_counts_included_and_excluded(env: BaselineEnv) -> None:
    """`item_count` / `excluded_count` 分别数纳入项与未纳入项。

    未纳入项**也必须入库**（AC-24：未纳入原因要留痕）——
    只把它们从计数里排除、连行都不写，会让"为什么没纳入"在半年后
    无处可查，而那时**补不出来**。
    """
    created = _ready_and_baseline(
        env,
        selected=(
            _fact_item(),
            _scene_item(
                inclusion=INCLUSION_EXCLUDED,
                exclusion_reason="该场景需要产品未声明的续航前提",
            ),
        ),
    )

    assert created.item_count == 1
    assert created.excluded_count == 1
    rows = _raw(
        env,
        "select inclusion, exclusion_reason from baseline_items"
        " where baseline_id = :bid order by item_no",
        {"bid": created.baseline_id},
    )
    assert [(r[0], r[1]) for r in rows] == [
        (INCLUSION_INCLUDED, None),
        (INCLUSION_EXCLUDED, "该场景需要产品未声明的续航前提"),
    ], "未纳入项必须落库且带原因"


@pytest.mark.regression
def test_write_endpoints_survive_request_id(env: BaselineEnv) -> None:
    """**Code Review MAJOR-5 的回归判据（先红后绿）**。

    ## 缺陷

    本包三处写审计的调用（`update_draft` L648、`create_baseline` L1348 与
    L1371、`approve_step` L1639）都只传了 `after=`，**没有配对的 `before=`**：

        record_audit(..., after={"revisionNo": ...}, reason=change_reason)

    而 `audit_events` 有一条 CHECK：

        (before_hash IS NULL) = (after_hash IS NULL)   -- ck_..._hash_pair_consistent

    于是 `before=None` + `after={...}` ⇒ `before_hash IS NULL` 为真、
    `after_hash IS NULL` 为假 ⇒ **CHECK 失败**（23514）。

    ## 为什么它是 Blocker 而不是"少写一条审计"

    审计写入被包在 `if actor_id is not None and request_id is not None:` 里，
    而 HTTP 端点的 `request_id=get_request_id()` **必然非空**（`api.py` 的
    三个写端点都传它）。因此：

      · `POST /baselines`（创建基线）→ 500
      · `PUT /projects/{id}/information-draft`（保存工作区）→ 500
      · `POST /step-approvals`（步骤批准）→ 500

    **整个 WP22 的三个写端点全部不可用**，而第 2 层此前一直是
    `request_id=None`（不写审计）在跑，所以这个问题在本判据出现前
    **从未被任何一条测试覆盖到** —— 典型的"共享盲区"：
    测试与实现都走同一条捷径（不传 request_id），谁也没碰到审计。

    ## 为什么第 4 层（HTTP）会红但它不够

    HTTP 层能测出 500，但测不出**是哪一步**撞的 —— 而"审计 CHECK"与
    "某个业务约束"的修法完全不同。本判据在**服务层**直接断言
    "带 request_id 的调用能跑完"，把根因钉在最近的那一层。

    ## 期望

    三处写审计的调用都必须给出配对的 `before`。语义上：

      · `update_draft`：before = 更新前的草稿负载（`_draft_audit_payload(old)`）
      · `create_baseline`：before = `{}`（创建前没有这条基线）
      · `baseline.supersede`：before = `{"supersededBy": None}`
      · `approve_step`：before = `{"decision": 被取代那条的结论}`（取代分支）
        或 `{}`（首次分支）—— **两条分支都在下面被实际调到**，
        否则"条件里写 `None`"这种只废一半的实现会被放过（ISS-82）

    本判据在修复前会 FAIL（裸 `IntegrityError`）。
    """
    brief_node = _make_brief_node(env)
    _make_ready(env, brief_version_id=brief_node)

    # ① 保存工作区（`update_draft`）带 request_id
    _update_draft_raw(
        env,
        expected_lock_version=_draft_row(env)["lock_version"],
        boundaries={"tone": "带请求号"},
        actor_id=env.owner_id,
        request_id="req-wp22-audit-draft",
    )
    assert _audit_count(
        env, action=AUDIT_DRAFT_UPDATE, object_id=_draft_of(env).id
    ) == 1, "带 request_id 的保存必须真的写下一条审计（而不是 500）"

    # ② 创建基线（`create_baseline`）带 request_id
    draft = _baseline_draft(
        env, brief_version_id=brief_node, selected=(_fact_item(),)
    )
    created = _create_baseline(env, draft, request_id="req-wp22-audit-create")

    assert _audit_count(
        env, action=AUDIT_BASELINE_CREATE, object_id=created.baseline_id
    ) == 1

    # ③ 步骤批准（`approve_step`）带 request_id —— **两个分支都要覆盖**。
    #
    # ⚠️ 这里**不能**"再批一次 APPROVED"：步骤 3 的那条批准由
    #    `create_baseline` 同事务写成（OP-151），再批同一结论会被
    #    `approve_step` **合法地**拦成 409（MAJOR-6 的判据），
    #    于是 `approve_step` 的审计分支根本走不到 ——
    #    判据会"看起来在测、实际没测"（ISS-82 的同款陷阱）。
    #    所以取两种"能真正写成"的批准各来一次，把两条条件分支都钉住：
    #
    #      ③a 取代分支（`before` 非空）：步骤 3 由 APPROVED 改成 REJECTED
    #      ③b 首次分支（`before` 为空负载）：给步骤 1 写一条 BRIEF 批准
    #
    #    两处都传 `request_id`，因此都必然走审计写入。

    # ③a 取代：这一版步骤 3 的生效结论是 APPROVED → 新的一行会取代它
    superseding = _approve_step(
        env,
        decision=APPROVAL_DECISION_REJECTED,
        reason="基线不合规，退回重做",
        request_id="req-wp22-audit-approve-supersede",
    )
    assert _audit_count(
        env, action=AUDIT_STEP_APPROVE, object_id=superseding.approval_id
    ) == 1, "取代分支的审计必须写成（`before` 非空且与 `after` 配对）"

    # ③b 首次：步骤 1 的产物是 Brief，这一版此前没有任何批准 → `before` 是空负载
    first = _approve_step(
        env,
        step_no=1,
        subject_kind=SUBJECT_KIND_BRIEF,
        subject_version_id=brief_node,
        reason="Brief 解析通过",
        request_id="req-wp22-audit-approve-first",
    )
    assert _audit_count(
        env, action=AUDIT_STEP_APPROVE, object_id=first.approval_id
    ) == 1, "首次分支的审计必须写成（`before` 是 `{}` 而不是 `None`）"

    # 两条 `approve_step` 审计的 `before`/`after` 必须**成对非空**。
    # 这条断言才是判据的核心：`before=None` + `after={...}` 会撞
    # `ck_audit_events_hash_pair_consistent`，而那**不是在服务层能读到的错**
    # （它发生在 COMMIT 期）—— 因此这里直接查库，把"配对"钉成可观测量。
    for expected_id in (superseding.approval_id, first.approval_id):
        paired = _scalar(
            env,
            "select count(*) from audit_events"
            " where action = :action and object_id = :oid"
            "   and before_hash is not null and after_hash is not null",
            {"action": AUDIT_STEP_APPROVE, "oid": expected_id},
        )
        assert paired == 1, (
            "`approve_step` 的审计必须 before/after 同时有哈希 ——"
            " 这就是 MAJOR-5 的根因所在"
        )

    # 全仓层面：每一条审计都必须成对（before/after 同时有或同时无）
    unbalanced = _scalar(
        env,
        "select count(*) from audit_events"
        " where (before_hash is null) <> (after_hash is null)",
    )
    assert unbalanced == 0, (
        "审计表的 before/after 必须成对 —— 这条 CHECK 是数据库层的判据，"
        "但它只有在真的写审计时才会被触发（第 3 层会单独验证它）"
    )


def test_create_baseline_is_idempotent_for_same_payload(env: BaselineEnv) -> None:
    """同 payload 重投 → `created=False`（**幂等命中**，不是错误）。

    网络超时后的重试必须成功 —— 把重投当错误处理会迫使每个调用方
    自己写去重（`register_version` 的 docstring 已论证这一点）。

    ## ⚠️ 本判据在 MAJOR-2 修复前会 FAIL（已实测）

    实测报错点**不在幂等命中路径上**，而是：

        sqlalchemy.exc.IntegrityError: (psycopg.errors.UniqueViolation)
        duplicate key value violates unique constraint "uq_production_baselines_project_root"
        DETAIL: Key (project_id)=(...) already exists.
        [SQL: INSERT INTO production_baselines (..., supersedes_id, ...) VALUES (..., None, ...)]

    即 `INSERT INTO production_baselines (supersedes_id = NULL)` 撞了
    **部分唯一索引** `uq_production_baselines_project_root`
    （`(project_id) WHERE supersedes_id IS NULL`）。

    ## 机制（与"幂等命中"相关的那个缺口）

    `create_baseline` 调 `register_version` **不传 `revision_no`**，
    因此它走"追加到实体末尾"分支：

      · 第 1 次：`max_revision_no(BASELINE, project_id) = 0` → 修订号 1 → 插入 → 返回 `created=True`
      · 第 2 次：`max_revision_no(...) = 1` → 修订号 **2** → 节点**能**插进去
        → `registered.created=True` → 走 `repo.insert_baseline(supersedes_id=None)`
        → **撞链根唯一索引**（本项目的链根已经是第 1 条）→ 裸 23505

    也就是说：**"同 payload 重投"从来没有走到过幂等命中分支** ——
    服务层把 `created` 的判定权完全交给了 `register_version`，而它按
    "修订号"判幂等，基线却按"链根唯一"判幂等。判据不一致 → 重投必炸。

    本判据断言的是**可观察行为**：重投必须返回 `created=False`
    且复用同一条基线（`_baseline_count == 1`、`step_approvals` 计数 == 1）。
    它**不指定修法**（可以是"先查链根再复用"，也可以是别的）——
    修法由缺陷修复阶段决定。
    """
    brief_node = _make_brief_node(env)
    _make_ready(env, brief_version_id=brief_node)
    selected = (_fact_item(),)
    draft = _baseline_draft(env, brief_version_id=brief_node, selected=selected)

    first = _create_baseline(env, draft)
    second = _create_baseline(env, draft)

    assert first.created is True
    assert second.created is False, "同内容同修订号必须是幂等命中"
    assert second.baseline_id == first.baseline_id
    assert second.revision_no == first.revision_no == 1
    assert _baseline_count(env) == 1, "幂等命中不得凭空多出一条基线"
    assert _scalar(
        env,
        "select count(*) from step_approvals where project_id = :pid",
        {"pid": env.project_id},
    ) == 1, (
        "批准必须被**复用**而不是每次重投都插一条 —— "
        "重复记录等于伪造审计（`_ensure_baseline_approval` 的三种情形表）"
    )


# --- 2.3 创建基线：拒绝分支（每条防线各有一条判据） -------------------------
#
# 这一段的组织原则：**先把 readiness 推到全绿**，再让被测的那一条防线
# 单独变红。否则每条用例测到的都是"条件未满足"，而"被正确地拦下"与
# "被另一条防线顺带拦下"是两件事（ISS-95 同族）。
#
# 全部用例都**不传 `request_id`**（不写审计）：审计路径有独立的
# MAJOR-5 判据，混进来会让"这条防线红了"与"审计撞 CHECK"分不开。


def _ready_for(env: BaselineEnv) -> uuid.UUID:
    """把工作区写成 readiness 全绿，返回它绑定的 Brief 版本节点 id。

    与 `_ready_and_baseline` 的区别：**不创建基线** —— 拒绝分支的用例
    要测的是"创建这一步被拦下"，前提当然是"还没建过"。
    """
    node = _make_brief_node(env)
    _make_ready(env, brief_version_id=node)
    return node


def _create_expect_error(
    env: BaselineEnv,
    *,
    brief_version_id: uuid.UUID,
    selected: tuple[ItemDraft, ...],
    code: ErrorCode,
    reason: str,
    **draft_overrides: Any,
) -> ApiError:
    """调 `create_baseline` 并**要求它抛 `ApiError`**，返回该错误。

    `draft_overrides` 直接透传给 `_baseline_draft`（`ruleset_version` /
    `change_reason` / `supersedes_id` / `project_id`）—— 这样"哪一条输入
    非法"在调用点一眼可见，不必先构造 draft 再调 `_expect_api_error`。
    """
    draft = _baseline_draft(
        env, brief_version_id=brief_version_id, selected=selected, **draft_overrides
    )
    return _expect_api_error(
        lambda: _create_baseline(env, draft), code=code, reason=reason
    )


# --- 2.3.1 第 7 层：三条完成条件 --------------------------------------------


def test_create_baseline_requires_three_readiness_conditions(env: BaselineEnv) -> None:
    """三条完成条件未满足 → 422 `step3_not_ready`，且**逐条**回带读数。

    ## 为什么必须逐条回带（而不是一句话）

    前端要能标出"哪一条没满足、差多少"。只给一句
    "条件尚未全部满足"会让它在三条之间猜 —— 而 `readiness.ConditionResult`
    的 `reason` 与 API 预检文案是**同一段文字**，本判据顺手钉住这一点。

    ## `unsatisfied[]` 的四个键

    | 键 | 作用 |
    |---|---|
    | `key` | 前端按它分支（不是按文案） |
    | `reason` | 与人看的说明同源 |
    | `blockingOpen` | 条件① 的明细（哪几个阻断冲突还开着） |
    | `producibleScenes` | 条件② 的明细（哪几个场景可生产） |

    **四个键必须都在**：只回 `key` + `reason` 会让"差多少"无从回答，
    而"补一条事实"与"处置三个冲突"是两个完全不同的工作量。
    """
    node = _make_brief_node(env)
    # 只钉 Brief 版本，五块内容一个不填 —— 条件②③ 都不满足。
    _write_draft(env, brief_version_id=node)

    expected_status = bs.step3_status_of_draft(_draft_of(env))
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_STEP3_NOT_READY,
    )

    unsatisfied = error.details["unsatisfied"]
    assert [row["key"] for row in unsatisfied] == [
        condition.key for condition in expected_status.unsatisfied()
    ], "回带的顺序必须与 `readiness` 的判定顺序一致（前端按顺序渲染）"
    assert [row["key"] for row in unsatisfied] == [
        rd.CONDITION_PRODUCIBLE_SCENE,
        rd.CONDITION_OPERATOR_CONFIRMED,
    ], "空草稿下只有条件②③ 不满足（没有阻断冲突 ⇒ 条件① 天然成立）"

    for row in unsatisfied:
        assert set(row) == {"key", "reason", "blockingOpen", "producibleScenes"}, (
            "四个键必须都在 —— 少一个都会让前端在某条条件下拿不到明细"
        )
        assert isinstance(row["blockingOpen"], list)
        assert isinstance(row["producibleScenes"], list)

    assert [row["reason"] for row in unsatisfied] == [
        condition.reason for condition in expected_status.unsatisfied()
    ], "服务层的拒绝文案必须与 readiness 的读数同源（否则两处会漂移）"

    by_key = {row["key"]: row for row in unsatisfied}
    assert by_key[rd.CONDITION_PRODUCIBLE_SCENE]["producibleScenes"] == [], (
        "没有任何场景 ⇒ 明细为空列表（不是缺键、不是 null）"
    )


def test_create_baseline_blocks_on_operator_confirmation_alone(
    env: BaselineEnv,
) -> None:
    """**只差条件③**（运营没点确认）也必须拒绝。

    PRD §7.3 条件③ 的语义是"运营**明确**点击确认"，系统不可代判。
    若实现里把它从三条合取里漏掉（或写成"草稿有内容就算确认"），
    基线会在运营还没看过的时候就被建出来 —— 而它是 AC-08 的验收对象。

    **这条与上一条不是重复**：上一条的三条里红了两条，因此即使条件③
    被漏判它也会红（另外两条挡着）。只有把条件①② 都推到全绿，
    才测得到"条件③ 单独拦不拦得住"。
    """
    node = _make_brief_node(env)
    _write_draft(
        env,
        claims=READY_CLAIMS,
        conflicts=READY_CONFLICTS,
        scenes=READY_SCENES,
        boundaries={"tone": "克制"},
        operator_confirmed=False,
        brief_version_id=node,
    )
    assert bs.step3_status_of_draft(_draft_of(env)).ready is False

    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_STEP3_NOT_READY,
    )
    assert [row["key"] for row in error.details["unsatisfied"]] == [
        rd.CONDITION_OPERATOR_CONFIRMED
    ], "条件①② 全绿时，唯一没满足的必须是条件③"


def test_create_baseline_blocks_on_open_blocking_conflict(env: BaselineEnv) -> None:
    """**只差条件①**：存在未处置的阻断冲突 → 拒绝，并回带它的 id。

    `blockingOpen` 里的 id 就是运营下一步要处置的对象 —— 空列表会让
    前端只能显示"有阻断问题"，而不知道点哪里。
    """
    node = _make_brief_node(env)
    _write_draft(
        env,
        claims=READY_CLAIMS,
        conflicts=[
            {
                "conflictId": "k9",
                "kind": CONFLICT_KIND_SOURCE,
                "blocking": True,
                "state": CONFLICT_STATE_OPEN,
            }
        ],
        scenes=READY_SCENES,
        operator_confirmed=True,
        brief_version_id=node,
    )

    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_STEP3_NOT_READY,
    )
    rows = error.details["unsatisfied"]
    assert [row["key"] for row in rows] == [rd.CONDITION_NO_BLOCKING_OPEN]
    assert rows[0]["blockingOpen"] == ["k9"], (
        "必须指名是哪几个阻断冲突 —— 否则前端无从指引运营去处置"
    )


# --- 2.3.2 第 1~2 步：项目与工作区 ------------------------------------------


def test_create_baseline_rejects_unknown_project(env: BaselineEnv) -> None:
    """项目不存在 → 422（`projectId`），不是 404。

    404 在本包里专指"工作区/基线不存在"；"项目不存在"是**输入字段**的问题。
    两者混用会让前端无法区分"换个 id 重试"与"提示用户先建项目"。
    """
    node = _make_brief_node(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_PROJECT_NOT_FOUND,
        project_id=uuid.uuid4(),
    )
    assert error.details["field"] == "projectId"


def test_create_baseline_requires_existing_draft(env: BaselineEnv) -> None:
    """工作区不存在 → 404 `information_draft_not_found`。

    **没有工作区就没有"审核过什么"这个前提**：此时 `readiness` 无从计算，
    快照里的 `readiness` / `counts` / `boundaries` 也没有来源。
    让它走到"条件未满足"会给出一个指向错误方向的提示
    （运营会去补内容，而实际要做的是先打开步骤 3 让工作区被懒创建）。
    """
    assert _scalar(
        env,
        "select count(*) from information_drafts where project_id = :pid",
        {"pid": env.project_id},
    ) == 0, "夹具不应当预先建工作区 —— 否则这条测试测不到 404"

    node = _make_brief_node(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(),),
        code=ErrorCode.RESOURCE_NOT_FOUND,
        reason=REASON_DRAFT_NOT_FOUND,
    )
    assert "field" not in (error.details or {}), (
        "404 是资源级错误，不是字段级 —— 带 `field` 会把前端引到输入上去"
    )


# --- 2.3.3 第 4 步：selected / rulesetVersion / changeReason ----------------


def test_create_baseline_rejects_empty_selection(env: BaselineEnv) -> None:
    """`selected` 为空 → 422 `baseline_selection_empty`（且不带 `maxItems`）。

    "一条不含任何条目的基线"在下游看来与"什么都没审核"无法区分 ——
    而它的快照里 `counts` 会是 0/0，读起来像"审过了，结果为空"。
    """
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SELECTION_EMPTY,
    )
    assert error.details["field"] == "selected"
    assert "maxItems" not in error.details, (
        "空集合不是超限 —— 带 `maxItems` 会让前端提示「最多 2000 项」，"
        "而实际问题是「至少 1 项」"
    )


def test_create_baseline_rejects_too_many_items(env: BaselineEnv) -> None:
    """条目数超上限 → 同样用 `baseline_selection_empty`，但**额外带 `maxItems`**。

    两个分支共用同一个 `reason` 是刻意的（都是"这个集合不可用"），
    靠 `maxItems` 的有无区分方向 —— 前端因此只需要一个 `if`。

    ⚠️ 超限判定排在逐条形状校验**之前**（`service.create_baseline` 的顺序）：
    因此这里可以安全地传 2001 条**内容合法**的条目，不会先撞形状校验。
    若哪天有人把顺序调换，"超限"这个判据会以"某条形状不对"的形式变红 ——
    那也是这条测试的价值之一。
    """
    node = _ready_for(env)
    selected = tuple(
        _fact_item(ref_label=f"事实 v{i}") for i in range(MAX_DRAFT_ITEMS + 1)
    )
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=selected,
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SELECTION_EMPTY,
    )
    assert error.details["maxItems"] == MAX_DRAFT_ITEMS


def test_create_baseline_requires_ruleset_version(env: BaselineEnv) -> None:
    """`rulesetVersion` 空白（含纯空格）→ 422 `baseline_item_snapshot_required`。

    **纯空格也算空**：它是审核规则集的版本标签，会被写进不可变快照 ——
    一个空格进去之后**永远改不掉**，而它对"这条基线按哪版规则审的"
    这个问题给不出任何答案。
    """
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(),),
        ruleset_version="   ",
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SNAPSHOT_REQUIRED,
    )
    assert error.details["field"] == "rulesetVersion"
    assert "maxLength" not in error.details


def test_create_baseline_rejects_overlong_ruleset_version(env: BaselineEnv) -> None:
    """`rulesetVersion` 过长 → 同一 `reason` + `maxLength`。"""
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(),),
        ruleset_version="R" * (MAX_RULESET_LABEL_CHARS + 1),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SNAPSHOT_REQUIRED,
    )
    assert error.details["field"] == "rulesetVersion"
    assert error.details["maxLength"] == MAX_RULESET_LABEL_CHARS


def test_create_baseline_requires_change_reason(env: BaselineEnv) -> None:
    """`changeReason` 为空 → 422，`reason` 是**专用**码（不再复用条目排除那个）。

    ⚠️ 收口修复（ISS-109）：此前 `_require_reason_text` 对三处调用方
    （`changeReason` / `exclusionReason` / 批准的 `reason`）发同一个
    `exclusion_reason_required`，而 `_validation_error` 的 docstring 明写
    "前端按 `reason` 分支" —— 于是前端拿到"排除原因必填"去分支，
    会把排查引到 `selected[]` 上去，而真正出错的是基线的 `changeReason`。
    这与本文件 `_require_decision` 明确立的规矩正面对撞
    （"一个指向错误方向的错误码比没有错误码更糟"）。
    本判据现在按**修复后**的行为断言：字段名 + 专用码两者都对才通过。
    """
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(),),
        change_reason="  ",
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_CHANGE_REASON_REQUIRED,
    )
    assert error.details["field"] == "changeReason"
    assert error.details["reason"] != REASON_EXCLUSION_REASON_REQUIRED, (
        "不得退回复用条目排除那个码 —— 那是本次修复要消除的方向错误的提示"
    )
    assert "maxLength" not in error.details


def test_create_baseline_rejects_overlong_change_reason(env: BaselineEnv) -> None:
    """`changeReason` 过长 → 带 `maxLength`（上限与 `exclusionReason` 不同）。

    ⚠️ `MAX_CHANGE_REASON_CHARS` 与 `MAX_REASON_CHARS` 在本包**恰好相等**，
    但它们是两个独立的常量。判据断言的是 `MAX_CHANGE_REASON_CHARS` ——
    若哪天有人只改了一个，这条会红（用另一个常量断言则不会）。
    """
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(),),
        change_reason="x" * (MAX_CHANGE_REASON_CHARS + 1),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_CHANGE_REASON_REQUIRED,
    )
    assert error.details["field"] == "changeReason"
    assert error.details["maxLength"] == MAX_CHANGE_REASON_CHARS


# --- 2.3.4 第 4 步：单条条目的形状（`_require_item`） -----------------------
#
# 这一组与数据库 CHECK **逐条对应**（`_require_item` 的 docstring 里那张表）：
# 服务层这一层给可读 422，数据库那层管住绕过本层的写入（第 3 层验证）。
# 两层都要有，因此这里逐条断言 `field` 与 `reason`。
#
# `where` 前缀是 `selected[<0 基下标>]` —— 断言它而不是"某处出错"，
# 是因为前端要用它把错误标在**具体哪一行**上。


def test_create_baseline_rejects_unknown_item_kind(env: BaselineEnv) -> None:
    """`itemKind` 不在受控取值域 → 422，指向 `selected[0].itemKind`。"""
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(item_kind="NOT_A_KIND"),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SNAPSHOT_REQUIRED,
    )
    assert error.details["field"] == "selected[0].itemKind"


def test_create_baseline_rejects_unknown_inclusion(env: BaselineEnv) -> None:
    """`inclusion` 不在受控取值域 → 422，指向 `selected[0].inclusion`。

    `ItemDraft` 的默认值是 `INCLUDED`，因此走到这条只可能是显式传了非法值 ——
    这正是"受控取值必须显式判"的理由：JSONB 里任何字符串都存得下。
    """
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(inclusion="MAYBE"),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SNAPSHOT_REQUIRED,
    )
    assert error.details["field"] == "selected[0].inclusion"


def test_create_baseline_rejects_blank_ref_label(env: BaselineEnv) -> None:
    """`refLabel` 空白（含纯空格）→ 422。

    **纯空格也算空**：它是给人读的引用标签（「事实 v7」），一个空格进去
    会让详情页显示一个空白格子，而运营无从知道这一项引的是什么。
    """
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(ref_label="   "),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SNAPSHOT_REQUIRED,
    )
    assert error.details["field"] == "selected[0].refLabel"
    assert "maxLength" not in error.details


def test_create_baseline_rejects_overlong_ref_label(env: BaselineEnv) -> None:
    """`refLabel` 过长 → 带 `maxLength=MAX_REF_LABEL_CHARS`。

    数据库那侧是 `String(100)`（截断会报 22001，而不是 23514），
    因此服务层这条判据是**唯一**能给出 `field` 的地方。
    """
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(ref_label="标" * (MAX_REF_LABEL_CHARS + 1)),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SNAPSHOT_REQUIRED,
    )
    assert error.details["field"] == "selected[0].refLabel"
    assert error.details["maxLength"] == MAX_REF_LABEL_CHARS


def test_create_baseline_rejects_empty_item_snapshot(env: BaselineEnv) -> None:
    """`snapshot` 是空对象 → 422（**R1 的入口**）。

    "只有引用没有值的条目"就是「基线存指针」：知识更新后它读到新值，
    且**静默**。它的失效当天完全正常，只在知识更新后才显现 ——
    因此必须在写入前拦住，不能等下游发现。
    """
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(snapshot={}),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SNAPSHOT_REQUIRED,
    )
    assert error.details["field"] == "selected[0].snapshot"


def test_create_baseline_requires_exclusion_reason_for_excluded(
    env: BaselineEnv,
) -> None:
    """`EXCLUDED` 项缺 `exclusionReason` → 422 `exclusion_reason_required`。

    **未纳入原因只在复盘时才被读，而那时补不出来**（AC-24）——
    因此它是硬性必填，不是"建议填"。
    """
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_scene_item(inclusion=INCLUSION_EXCLUDED),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_EXCLUSION_REASON_REQUIRED,
    )
    assert error.details["field"] == "selected[0].exclusionReason"


def test_create_baseline_rejects_overlong_exclusion_reason(env: BaselineEnv) -> None:
    """`exclusionReason` 过长 → 带 `maxLength=MAX_REASON_CHARS`。

    ⚠️ 断言 `MAX_REASON_CHARS` 而不是 `MAX_CHANGE_REASON_CHARS` ——
    两者当前恰好相等，但它们是两个独立常量（判据要能区分）。
    """
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(
            _scene_item(
                inclusion=INCLUSION_EXCLUDED,
                exclusion_reason="x" * (MAX_REASON_CHARS + 1),
            ),
        ),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_EXCLUSION_REASON_REQUIRED,
    )
    assert error.details["field"] == "selected[0].exclusionReason"
    assert error.details["maxLength"] == MAX_REASON_CHARS


def test_create_baseline_rejects_reason_on_included_item(env: BaselineEnv) -> None:
    """`INCLUDED` 项**带** `exclusionReason` → 422。

    这是"状态与原因不一致"：一条已纳入的条目说自己为什么没被纳入。
    若放行，下游读到时会有一个自相矛盾的答案 ——
    而两个方向都可能被信（有人看 `inclusion`，有人看 `exclusionReason`）。
    """
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(exclusion_reason="其实我纳入了"),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_EXCLUSION_REASON_REQUIRED,
    )
    assert error.details["field"] == "selected[0].exclusionReason"


def test_create_baseline_requires_asset_when_version_given(env: BaselineEnv) -> None:
    """有 `knowledgeVersionId` 但没有 `knowledgeAssetId` → 422。

    **"版本指向谁无从判断"**：`baseline_items` 的两条 FK 各自只保证
    "这行版本存在"，而"这个版本属于哪个资产"要靠本行的
    `knowledge_asset_id` 来声明（第 3 层再校验这个声明对不对）。
    只给版本等于放弃了这半条溯源链。
    """
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(knowledge_version_id=uuid.uuid4()),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SNAPSHOT_REQUIRED,
    )
    assert error.details["field"] == "selected[0].knowledgeAssetId"


# --- 2.3.5 第 4 步：条目的 scope（`_require_item_scope`） -------------------
#
# 第 3 层防线的服务层一侧。判据读的是 `KNOWLEDGE_SCOPE_MATCH_RULE` ——
# `ddl.py` 编译的 plpgsql 分支与这里读的是**同一张声明表**，
# 这是"两层不漂移"的全部机制。


def test_create_baseline_accepts_own_product_knowledge(env: BaselineEnv) -> None:
    """**正例**：本项目产品范围内的知识必须被接受。

    没有这条正例，下面五条反例即使全部通过也说明不了什么 ——
    一个"一律拒绝"的实现会让它们全绿。
    （这正是 ISS-82 的教训：判据必须能区分两种实现。）
    """
    node = _ready_for(env)
    asset_id, version_id = _own_knowledge(env)

    created = _create_baseline(
        env,
        _baseline_draft(
            env,
            brief_version_id=node,
            selected=(
                _fact_item(
                    knowledge_asset_id=asset_id, knowledge_version_id=version_id
                ),
            ),
        ),
    )
    assert created.created is True

    row = _raw(
        env,
        "select knowledge_asset_id, knowledge_version_id from baseline_items"
        " where baseline_id = :bid",
        {"bid": created.baseline_id},
    )
    assert [(r[0], r[1]) for r in row] == [(asset_id, version_id)], (
        "资产与版本必须**一对**落库（决策 1：ref 是 asset + version）"
    )


def test_create_baseline_accepts_own_project_knowledge(env: BaselineEnv) -> None:
    """**正例之二**：`scope=PROJECT` 且属于本项目 → 接受。

    与上一条走的是**不同的分支**（`SCOPE_MATCH_SAME_PROJECT` 比的是
    `k_project == project_id`，不比品牌/产品）。只测 PRODUCT 分支的话，
    把 PROJECT 分支写错（比如也去比品牌）不会被任何判据发现。
    """
    node = _ready_for(env)
    with session_scope(env.settings) as session:
        asset_id, version_id = _make_knowledge(
            env, scope=SCOPE_PROJECT, project_id=env.project_id
        )

    created = _create_baseline(
        env,
        _baseline_draft(
            env,
            brief_version_id=node,
            selected=(_fact_item(knowledge_asset_id=asset_id),),
        ),
    )
    assert created.created is True


def test_create_baseline_rejects_missing_knowledge_asset(env: BaselineEnv) -> None:
    """引用的知识资产不存在 → 422 `cross_brand_reference`。

    ⚠️ **`reason` 复用 `cross_brand_reference` 是既有实现的取向**：
    "这个资产我查不到"与"它属于别人"在服务层被归为同一类
    （`_require_item_scope` 的第一支）。本判据按现状断言；
    若收口时拆成两个 reason，这条会红并提醒同步前端分支。已登记。
    """
    node = _ready_for(env)
    missing = uuid.uuid4()
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(knowledge_asset_id=missing),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_CROSS_BRAND_REFERENCE,
    )
    assert error.details["field"] == "selected[0].knowledgeAssetId"
    assert "scope" not in error.details, (
        "资产不存在时没有 scope 可回带 —— 带一个 `None` 会让前端以为"
        "「它的 scope 是空」，而实际是「查不到这条资产」"
    )


def test_create_baseline_rejects_other_brand_knowledge(env: BaselineEnv) -> None:
    """`scope=BRAND` 且属于**另一个品牌** → 422，并回带 `scope` 与资产 id。

    AC-24 原文：「跨品牌事实不得迁移」。这条判据是它的服务层一侧
    （数据库那一侧在第 3 层，用同一张 `KNOWLEDGE_SCOPE_MATCH_RULE`）。

    ⚠️ 反例必须用**真实的**别品牌知识（而不是手搓一个 scope 字符串）：
    `_require_item_scope` 读的是 `knowledge_assets` 的投影列，
    一个凭空生成的 uuid 只会走到"资产不存在"那一支（上一条已覆盖）。
    """
    node = _ready_for(env)
    asset_id, _version = _make_knowledge(
        env, scope=SCOPE_BRAND, brand_id=env.other_brand_id
    )
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(knowledge_asset_id=asset_id),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_CROSS_BRAND_REFERENCE,
    )
    assert error.details["field"] == "selected[0].knowledgeAssetId"
    assert error.details["scope"] == SCOPE_BRAND
    assert error.details["knowledgeAssetId"] == str(asset_id), (
        "必须回带**资产 id**：运营要据此找到那条知识并决定怎么处置"
    )


def test_create_baseline_rejects_other_product_knowledge(env: BaselineEnv) -> None:
    """`scope=PRODUCT`：品牌相同但**产品不同** → 拒绝。

    这条单独存在的理由是：`SCOPE_MATCH_SAME_PRODUCT` 是**两个条件的合取**
    （`k_brand == project_brand and k_product == project_product`）。
    只测"品牌不同"的话，把实现写成"只比品牌"也能通过 ——
    而"同品牌不同产品"正是产品线并行时最常见的误引用形态。
    """
    node = _ready_for(env)
    asset_id, _version = _make_knowledge(
        env,
        scope=SCOPE_PRODUCT,
        brand_id=env.brand_id,          # 同品牌
        product_id=env.other_product_id,  # 不同产品
    )
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(knowledge_asset_id=asset_id),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_CROSS_BRAND_REFERENCE,
    )
    assert error.details["scope"] == SCOPE_PRODUCT


def test_create_baseline_rejects_null_brand_knowledge(env: BaselineEnv) -> None:
    """**`k_brand IS NULL` 而项目有品牌 → 必须拒绝**（NULL 安全比较）。

    这是本包最容易写错的一条比较：若实现写成
    `if k_brand and k_brand != proj_brand`，那个 `and` 会把
    "知识没有品牌"这条路径**静默放行** —— 而它正是跨品牌引用
    最常见的形态（GLOBAL / 未声明的知识被任何项目引用）。
    `ddl.py` 那侧必须用 `IS DISTINCT FROM`，服务层这侧必须用
    `==` 对 `None` 也成立 —— 两个语言侧的同一条纪律。

    构造：`scope=BRAND` 但 `brand_id=None`（知识侧的 CHECK 只要求
    `scope='BRAND'` 时有 brand —— 这里显式给 `None` 会被知识侧拦下，
    因此改用 `scope=PRODUCT` + 无 brand 的形态：知识侧只要求 product）。
    """
    node = _ready_for(env)
    # 知识侧：`scope=PRODUCT` 只要求 `product_id`（`scope_product_needs_product_id`），
    # 因此 `brand_id=None` 能建出来 —— 而基线侧要求它等于项目品牌。
    asset_id, _version = _make_knowledge(
        env, scope=SCOPE_PRODUCT, brand_id=None, product_id=env.product_id
    )
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(knowledge_asset_id=asset_id),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_CROSS_BRAND_REFERENCE,
    )
    assert error.details["scope"] == SCOPE_PRODUCT


def test_create_baseline_rejects_missing_knowledge_version(env: BaselineEnv) -> None:
    """`knowledgeVersionId` 指向不存在的版本 → 422，回带 `versionId`。"""
    node = _ready_for(env)
    asset_id, _version = _own_knowledge(env)
    ghost = uuid.uuid4()
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(
            _fact_item(knowledge_asset_id=asset_id, knowledge_version_id=ghost),
        ),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SNAPSHOT_REQUIRED,
    )
    assert error.details["field"] == "selected[0].knowledgeVersionId"
    assert error.details["versionId"] == str(ghost)


def test_create_baseline_rejects_version_of_another_asset(env: BaselineEnv) -> None:
    """**版本属于另一个资产** → 422，回带三个 id（版本 / 真实资产 / 声称资产）。

    ## 为什么这条判据必须存在

    两条 FK 各自只保证"这行资产存在""这行版本存在"——
    **没有任何东西保证"这个版本属于这个资产"**。挂错资产时 FK 全绿，
    基线能建出来、能读回、快照也在，**只有溯源链是错的**。

    三个 id 都要回带：运维拿到"版本 5 其实属于资产 B，而你说它属于资产 A"
    才知道该改哪一边。只回 `versionId` 会让它去查两遍。
    """
    node = _ready_for(env)
    claimed_asset, _v1 = _own_knowledge(env)
    real_asset, real_version = _own_knowledge(env)

    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(
            _fact_item(
                knowledge_asset_id=claimed_asset, knowledge_version_id=real_version
            ),
        ),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SNAPSHOT_REQUIRED,
    )
    assert error.details["field"] == "selected[0].knowledgeVersionId"
    assert error.details["versionId"] == str(real_version)
    assert error.details["versionAssetId"] == str(real_asset)
    assert error.details["claimedAssetId"] == str(claimed_asset)


def test_item_index_in_error_field_is_zero_based(env: BaselineEnv) -> None:
    """`where` 前缀的下标是 **0 基**（`selected[<index>]`）。

    这条看着像吹毛求疵，但它决定了前端能不能把错误标在正确的行上：
    1 基会让"第 2 行错了"被标到第 3 行，而运营会去改一行本来正确的条目。

    ⚠️ `_item_rows` 里的 `contextOrder` 分支用的是**另一个**下标写法
    （`selected[{index - 1}]`，见下一条判据）—— 两处形状不同是既有实现的
    现状，已登记；本判据钉住的是 `_require_item` / `_require_item_scope` 这一组。
    """
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(), _fact_item(ref_label="")),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SNAPSHOT_REQUIRED,
    )
    assert error.details["field"] == "selected[1].refLabel", (
        "第二条（下标 1）的 refLabel 为空 —— 报成 selected[2] 就是 1 基"
    )


def test_research_item_requires_explicit_context_order(env: BaselineEnv) -> None:
    """`RESEARCH` 项**没有默认 `contextOrder`**，必须显式给出。

    `context_order.UNMAPPED_ITEM_KINDS` 明确把 `RESEARCH` 排除出
    PRD §15.4 的五项，因此"研究资料排第几"这个问题**必须由调用方回答**。
    把它转成 422 而不是让 `UnknownItemKindError` 冒到 500，
    是因为触发它的输入（一条 RESEARCH 项）是**用户数据**，不是编程错误。

    ⚠️ 这里的 `field` 用的是 `selected[0].contextOrder`（0 基）——
    与 `_item_rows` 里 `index - 1` 的写法一致（那一处的 `index` 从 1 起）。
    """
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(
            ItemDraft(
                item_kind=ITEM_KIND_RESEARCH,
                ref_label="研究资料 v1",
                snapshot={"title": "竞品续航横评"},
            ),
        ),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SNAPSHOT_REQUIRED,
    )
    assert error.details["field"] == "selected[0].contextOrder"
    assert error.details["itemKind"] == ITEM_KIND_RESEARCH, (
        "必须回带 `itemKind`：调用方要知道是**哪一类**条目需要显式排序"
    )


def test_research_item_with_explicit_context_order_is_accepted(
    env: BaselineEnv,
) -> None:
    """**正例**：显式给了 `contextOrder` 的 RESEARCH 项必须被接受。

    与上一条配对：一个"一律拒绝 RESEARCH"的实现会让上一条绿、这一条红。
    """
    node = _ready_for(env)
    created = _create_baseline(
        env,
        _baseline_draft(
            env,
            brief_version_id=node,
            selected=(
                ItemDraft(
                    item_kind=ITEM_KIND_RESEARCH,
                    ref_label="研究资料 v1",
                    snapshot={"title": "竞品续航横评"},
                    context_order=5,
                ),
            ),
        ),
    )
    assert created.created is True
    assert _scalar(
        env,
        "select context_order from baseline_items where baseline_id = :bid",
        {"bid": created.baseline_id},
    ) == 5, "显式给的 contextOrder 必须原样落库（不被派生覆盖）"


def test_create_baseline_rejects_nonpositive_context_order(env: BaselineEnv) -> None:
    """`contextOrder < 1` → 422。

    数据库那侧有 `ck_baseline_items_context_order_positive`，
    但它的报错是裸 23514，指不出是第几条 —— 因此服务层这一层要给出 `field`。
    """
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(context_order=0),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SNAPSHOT_REQUIRED,
    )
    assert error.details["field"] == "selected[0].contextOrder"


# --- 2.3.6 第 5 步：被替代的那条（`_require_supersedes_target`） -------------


def test_create_baseline_rejects_supersedes_from_other_project(
    env: BaselineEnv,
) -> None:
    """`supersedesId` 指向**别的项目**的基线 → 422 `baseline_not_found`。

    ⚠️ **反例必须是真实的别项目基线**。服务层先查目标存在、再比 `project_id`
    （`_require_supersedes_target` 的判据 1），因此一个凭空生成的 uuid
    只会走到"不存在"那一支 —— 那样测到的就不是"不属于本项目"。
    `_ready_and_baseline(project_id=...)` 就是为这条判据准备的。

    为什么必须拦：让两个项目的链交叉之后，"当前生效的是哪条"
    （`projects.active_baseline_id`）会在两个项目之间互相污染，
    而**没有任何一处会报错**。
    """
    foreign = _ready_and_baseline(env, project_id=env.other_project_id)
    assert foreign.created is True
    node = _make_brief_node(env)
    _make_ready(env, brief_version_id=node)

    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_BASELINE_NOT_FOUND,
        supersedes_id=foreign.baseline_id,
    )
    assert error.details["field"] == "supersedesId"
    assert _baseline_count(env) == 0, "被拒之后本项目不得留下任何基线"


def test_create_baseline_rejects_unknown_supersedes_target(env: BaselineEnv) -> None:
    """`supersedesId` 指向不存在的基线 → 同一 `reason`（目标不存在与不属于本项目合一）。

    两者用同一个 `reason` 是刻意的：对调用方来说，**这两种情况的动作相同**
    （检查你传的 id），而区分它们需要服务端回带"这个 id 属于哪个项目" ——
    那是一条枚举别人项目信息的旁路。
    """
    node = _ready_for(env)
    error = _create_expect_error(
        env,
        brief_version_id=node,
        selected=(_fact_item(),),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_BASELINE_NOT_FOUND,
        supersedes_id=uuid.uuid4(),
    )
    assert error.details["field"] == "supersedesId"


def test_supersede_must_target_chain_tail(env: BaselineEnv) -> None:
    """**不得替代一条已被替代过的基线** → 409 `baseline_already_superseded`。

    替代一条"已经被替代过"的基线意味着**链分叉**：两条基线同时声称
    取代了同一条旧基线，而 `projects.active_baseline_id` 只能指向其中一条。
    症状是"当前生效的基线"与"链尾"不一致，且**不报错**。

    ## ⚠️ 本判据与 `test_rebuild_active_baseline_matches_incremental_path`
    ## **共享同一个根因**（MAJOR-4）

    `_require_supersedes_target` 的判据 2 用 `repo.newest_baseline_of()`
    回答"链尾是谁"，而这个方法的 `NOT EXISTS` 子查询被写成了
    `supersedes_id == id`（**同行自比，恒假**）→ 子查询恒空 →
    `NOT EXISTS` 恒真 → 过滤条件恒真 → `LIMIT 1` 无 `ORDER BY`
    → 返回**任意一行**（实测为最先插入的链根）。

    因此修复前这里会**取不到 409**（链根恰好就是被替代的那条时，
    判据 2 会误判为"它就是链尾"）→ 本判据变红。
    两条判据红的原因完全相同，**修一处即可同时转绿**。
    """
    first = _ready_and_baseline(env)
    second = _ready_and_baseline(env, supersedes_id=first.baseline_id)
    node = _make_brief_node(env)
    _make_ready(env, brief_version_id=node)

    error = _expect_api_error(
        lambda: _create_baseline(
            env,
            _baseline_draft(
                env,
                brief_version_id=node,
                selected=(_fact_item(),),
                supersedes_id=first.baseline_id,  # ← 已被 second 替代
            ),
        ),
        code=ErrorCode.INVALID_TRANSITION,
        reason=REASON_BASELINE_SUPERSEDED,
    )
    assert error.details["supersedesId"] == str(first.baseline_id)
    assert error.details["currentTailId"] == str(second.baseline_id), (
        "必须回带**真正的链尾** —— 前端据此提示「请改为替代 BL-v2」"
    )
    assert _baseline_count(env) == 2, "被拒之后不得多出第三条基线"


# --- 2.3.7 Code Review MAJOR-1 的回归判据 ----------------------------------


@pytest.mark.regression
def test_create_baseline_rejects_non_brief_version_node(env: BaselineEnv) -> None:
    """**Code Review MAJOR-1 的回归判据（先红后绿）**。

    ## 缺陷

    `service.create_baseline` 从第 1 步到第 7 步**从不校验
    `draft.brief_version_id` 的类型**。它被原样写进
    `production_baselines.brief_version_id`，而那条 FK 只要求
    "`version_nodes` 里有这一行"—— **不管那一行是什么类型**。

    于是把任意一个版本节点 id 当成 `briefVersionId` 传进来，
    基线照样建得出来：

        created = create_baseline(draft=BaselineDraft(
            brief_version_id=<一个 PLAN 节点的 id>, ...))
        # → created.created is True，HTTP 201

    ## 为什么它危险

    `briefVersionId` 是"这条基线钉住哪一版 Brief"的答案
    （原型 L60「BL-v3 绑定 Brief v2」），也是 AC-24 溯源链的锚点。
    钉到一个**计划**节点上之后：

      · 基线建得出来、读得回、快照里的 `briefVersionId` 也有值；
      · 前端拿这个 id 去 `GET /versions/{id}` 会拿到一个 `PLAN` 节点 ——
        它自己会按类型渲染，**不会报错**；
      · 而"这条基线基于哪一版 Brief"从此没有答案，且**事后补不出来**
        （基线不可变）。

    与 MAJOR-3（静默清空）是**同一处字段的两个方向**：一个写没了，
    一个写错了。两条都不报错。

    ## 期望

    传入一个非 `BRIEF` 的版本节点 → **422**。

    ⚠️ **本判据刻意不指定修法，也不钉死 `reason`**：修法可以是
    "在服务层校验节点类型"，也可以是"在 `register_version` 之前
    调一次 `version_repo`"，还可以是别的。因此只断言
    "抛出 `VALIDATION_FAILED` 且 `field` 指向 `briefVersionId`" ——
    若收口时给了一个专门的 `reason`，把这里补上即可（不影响"先红后绿"）。

    本判据在修复前会 FAIL（`create_baseline` 会正常返回）。
    """
    node = _make_brief_node(env)
    _make_ready(env, brief_version_id=node)

    # 一个**合法存在**但类型是 PLAN 的版本节点。
    # ⚠️ `PLAN` 在 `version_nodes` 上没有配对的延迟触发器（只有
    # BRIEF / KNOWLEDGE / PROMPT / BASELINE 四个有），因此单独插它是安全的。
    with session_scope(env.settings) as session:
        plan_node = _insert_node(
            session,
            entity_type=ENTITY_TYPE_PLAN,
            entity_id=uuid.uuid4(),
            project_id=env.project_id,
        )

    assert _node_entity_type_of(env, plan_node) == ENTITY_TYPE_PLAN, (
        "前提：这个节点确实存在且类型是 PLAN（不是凭空 uuid —— 那只会走到 FK）"
    )

    error = _expect_api_error(
        lambda: _create_baseline(
            env,
            _baseline_draft(
                env, brief_version_id=plan_node, selected=(_fact_item(),)
            ),
        ),
        code=ErrorCode.VALIDATION_FAILED,
    )
    assert "briefVersionId" in str((error.details or {}).get("field", "")), (
        f"错误必须指向 briefVersionId 字段，实际 field="
        f"{(error.details or {}).get('field')!r}"
    )
    assert _baseline_count(env) == 0, (
        "被拒之后不得留下基线 —— 注册表里也不该多出节点"
    )
    assert _scalar(
        env,
        "select count(*) from version_nodes"
        " where entity_type = :etype and entity_id = :eid",
        {"etype": ENTITY_TYPE_BASELINE, "eid": env.project_id},
    ) == 0, (
        "**预检必须排在 `register_version` 之前**（`create_baseline` 的 "
        "docstring 已论证）：否则被拒的请求会白占一个修订号"
    )


# --- 2.3.8 Code Review MAJOR-4 的回归判据 ----------------------------------


@pytest.mark.regression
def test_rebuild_active_baseline_matches_incremental_path(env: BaselineEnv) -> None:
    """**Code Review MAJOR-4 的回归判据（先红后绿）**。

    ## 缺陷

    `repository.newest_baseline_of`（L440~469）的过滤条件里有一个
    **被写坏的相关子查询**：

        ~select(ProductionBaseline.id)
         .where(ProductionBaseline.supersedes_id == ProductionBaseline.id)
         .exists()

    `supersedes_id == id` 是**同一行的两列相比**，而
    `ck_production_baselines_supersedes_not_self` 保证这两列永远不相等 ——
    于是这个子查询**恒为空**，`NOT EXISTS` **恒为真**，
    整个过滤条件退化成"只按 `project_id` 过滤"，再 `LIMIT 1`
    （**没有 `ORDER BY`**）→ 返回**任意一行**（实测为最先插入的链根）。

    期望的语义是"没有任何其它行 `supersedes` 它的那条"，即
    `NOT EXISTS (SELECT 1 FROM production_baselines s WHERE s.supersedes_id = b.id)`
    —— 子查询必须引用**外层**的行。

    ## 两个症状

    1. `rebuild_active_baseline`（全量重算）返回**链根**，
       而增量路径（`set_active_baseline`）在每次创建时都指向**新创建的那条**
       → 两者不一致，而"当前生效的是哪条"因此有两个答案；
    2. `_require_supersedes_target` 的判据 2（"必须是链尾"）失效
       → 链可以分叉（由 `test_supersede_must_target_chain_tail` 覆盖）。

    **症状 1 是恢复数据时才爆的那一类**：重建投影列是事故恢复路径，
    那时既没有增量历史也没有对照 —— 而它会静默地把"当前生效"
    指回**第一版**基线。下游按它取上下文，于是拿到的是最旧的那份事实。

    ## 判据

    链上走三步，然后断言：

      · 增量路径的读数 == 链尾（**独立查询**给出的定义）；
      · 全量重算的读数 == 同一个值。

    **"链尾"用测试自己写的一条 SQL 独立求**（而不是复用被测方法）——
    否则判据与实现共享同一个盲区（本项目的第 2 条判据纪律）。

    ⚠️ 本判据在修复前会 FAIL —— 而且**红在第 3 步**：因为症状 2 会让
    第三步的 `supersedesId` 校验误判（详见
    `test_supersede_must_target_chain_tail` 的说明）。因此下面给第三步
    包了一句说明，避免把"链建不起来"误读成"投影列算错"。
    """
    first = _ready_and_baseline(env)
    second = _ready_and_baseline(env, supersedes_id=first.baseline_id)
    # ⚠️ MAJOR-4 修复前：这一行会抛 409 `baseline_already_superseded` ——
    # 因为 `newest_baseline_of` 恒返回链根，于是"链尾是不是它"被误判。
    # 这是**同一个根因的另一个症状**，不是"链本身有问题"。
    third = _ready_and_baseline(env, supersedes_id=second.baseline_id)

    assert (first.revision_no, second.revision_no, third.revision_no) == (1, 2, 3)
    assert _baseline_count(env) == 3

    # --- 链尾的**独立定义**（不复用被测方法） ---------------------------
    tail_rows = _raw(
        env,
        "select b.id from production_baselines b"
        " where b.project_id = :pid"
        "   and not exists ("
        "       select 1 from production_baselines s where s.supersedes_id = b.id"
        "   )",
        {"pid": env.project_id},
    )
    assert len(tail_rows) == 1, (
        f"链尾必须唯一，实际 {len(tail_rows)} 条 —— 链已分叉"
    )
    tail = uuid.UUID(str(tail_rows[0][0]))
    assert tail == third.baseline_id

    # --- 判据 1：增量路径 == 链尾 ---------------------------------------
    incremental = _active_baseline(env)
    assert incremental == tail, (
        "增量路径（`set_active_baseline`）必须在每次创建时指向新基线"
    )

    # --- 判据 2：全量重算 == 链尾 ---------------------------------------
    rebuilt = _rebuild_active(env)
    assert rebuilt == tail, (
        "全量重算必须得到与增量路径**相同**的值 —— "
        "`newest_baseline_of` 的 NOT EXISTS 子查询要引用外层行"
        "（`s.supersedes_id = b.id`），而不是同行自比（恒假）"
    )
    assert _active_baseline(env) == tail, "重算必须把结果写回投影列"

    # --- 判据 3：仓库方法本身 -------------------------------------------
    with session_scope(env.settings) as session:
        newest = BaselineRepository(session).newest_baseline_of(env.project_id)
    assert newest is not None and newest.id == tail, (
        "`newest_baseline_of` 的语义是「没有任何其它行 supersedes 它的那条」，"
        "不是「随便取一行」"
    )


# --- 2.4 步骤批准：三情形 + 交叉核对 ---------------------------------------


def _step1_brief_node(env: BaselineEnv) -> uuid.UUID:
    """给**步骤 1（BRIEF）**用的版本节点。

    步骤 1 与步骤 3 的差别在于：步骤 3 的批准由 `create_baseline`
    **同事务自动生成**（OP-151），因此"无链根"这一情形在步骤 3 上
    **天然不可达** —— 要测它必须换一个没有自动批准的步骤。
    步骤 1 是最干净的选择：它同样走 `_SUBJECT_KIND_ENTITY_TYPE` 的
    `BRIEF → BRIEF` 核对，不是"随便挑一个能过的步骤"。
    """
    return _make_brief_node(env)


def test_approve_step_inserts_root_when_none(env: BaselineEnv) -> None:
    """情形 1：**没有链根** → 插一条链根（`supersedes_id IS NULL`）。

    ⚠️ 这条**不能用步骤 3 测**：`create_baseline` 已经同事务写了一条
    `APPROVED` 链根，因此步骤 3 上"无链根"这一支永远走不到。
    用步骤 1（`BRIEF`）—— 它没有自动批准。
    """
    node = _step1_brief_node(env)
    approval = _approve_step(
        env,
        step_no=1,
        subject_kind=SUBJECT_KIND_BRIEF,
        subject_version_id=node,
        decision=APPROVAL_DECISION_APPROVED,
        reason="Brief v1 批准",
    )

    rows = _raw(
        env,
        "select id, step_no, subject_kind, subject_version_id, decision,"
        " supersedes_id, decided_by, reason from step_approvals"
        " where project_id = :pid",
        {"pid": env.project_id},
    )
    assert len(rows) == 1, "第一次批准必须只落一行"
    row = rows[0]
    assert row[1] == 1 and row[2] == SUBJECT_KIND_BRIEF
    assert uuid.UUID(str(row[3])) == node, "批准的对象必须是传入的那个版本节点"
    assert row[4] == APPROVAL_DECISION_APPROVED
    assert row[5] is None, "链根：supersedes_id 必须为 NULL（uq_step_approvals_root）"
    assert row[6] == env.owner_id, "`decided_by` 必须落库（它是「谁批的」的唯一答案）"
    assert row[7] == "Brief v1 批准", "reason 去空白后原样落库"
    assert approval.approval_id == uuid.UUID(str(row[0])), (
        "返回值必须指向**刚落库的那一行** —— 断言取值而不是只断言类型"
        "（ISS-77：`ANY(...)` 那种恒真判据也能通过）"
    )
    assert (approval.step_no, approval.subject_kind, approval.decision) == (
        1,
        SUBJECT_KIND_BRIEF,
        APPROVAL_DECISION_APPROVED,
    )
    assert approval.subject_version_id == node
    assert approval.decided_at is not None, "decided_at 由 `server_default=now()` 给"


def test_approve_step_rejects_duplicate_same_decision(env: BaselineEnv) -> None:
    """情形 2：已有 `APPROVED` 链根、**结论相同** → **409**（不是幂等返回）。

    ## 为什么不是幂等

    批准是**人为动作**：重投（网络超时重试）应当返回同一条，但
    "又批了一次"会在数据里留下第二条记录，让「这一步被批过几次」
    虚假增长 —— 而调用方无法区分这两者。因此宁可让它看见 409。
    （与本包 `create_baseline` 的重投路径刻意不同：那条必须成功，
    否则超时重试永远失败。）

    这条用**步骤 3**（`create_baseline` 已同事务写了 `APPROVED` 链根），
    因此不需要额外造前置状态 —— 它同时钉住了"创建基线时的自动批准
    确实算一条链根"这件事。
    """
    created = _ready_and_baseline(env)

    error = _expect_api_error(
        lambda: _approve_step(
            env,
            subject_version_id=created.baseline_id,
            decision=APPROVAL_DECISION_APPROVED,
        ),
        code=ErrorCode.INVALID_TRANSITION,
        reason=REASON_ALREADY_APPROVED,
    )
    assert error.details.get("approvalId") == str(created.approval_id), (
        "409 必须回带**已有那条**的 id —— 否则调用方只能重读列表去猜"
    )
    assert error.details.get("decision") == APPROVAL_DECISION_APPROVED

    assert _scalar(
        env,
        "select count(*) from step_approvals where project_id = :pid",
        {"pid": env.project_id},
    ) == 1, "被拒之后不得新增批准行"


def test_approve_step_supersedes_rejected_root(env: BaselineEnv) -> None:
    """情形 3：已有 `REJECTED` 链根 → 插一条**取代它**的新批准（「先拒后批」）。

    `step_approvals` 是不可变表，因此"撤销"靠**新增**而不是改写。
    旧的那条被保留（审计要看得到"曾经被拒过"），但不再构成依据
    （DDL 触发器的判据收窄到"未被取代"）。

    这条用步骤 1：步骤 3 上这条路径在修复前**不可达**（见
    `test_baseline_rejected_then_reapproved_becomes_effective`）。
    """
    node = _step1_brief_node(env)
    rejected = _approve_step(
        env,
        step_no=1,
        subject_kind=SUBJECT_KIND_BRIEF,
        subject_version_id=node,
        decision=APPROVAL_DECISION_REJECTED,
        reason="版本号与正文对不上",
    )

    approved = _approve_step(
        env,
        step_no=1,
        subject_kind=SUBJECT_KIND_BRIEF,
        subject_version_id=node,
        decision=APPROVAL_DECISION_APPROVED,
        reason="已修正，重批",
    )

    assert approved.approval_id != rejected.approval_id
    rows = _raw(
        env,
        "select id, decision, supersedes_id from step_approvals"
        " where project_id = :pid order by decided_at, id",
        {"pid": env.project_id},
    )
    assert len(rows) == 2, "旧的那条必须保留（不可变表的「撤销」靠新增）"
    assert uuid.UUID(str(rows[0][0])) == rejected.approval_id
    assert rows[0][1] == APPROVAL_DECISION_REJECTED
    assert rows[0][2] is None, "被拒的那条仍是链根（链根不会因为被取代而改变身份）"
    assert uuid.UUID(str(rows[1][0])) == approved.approval_id
    assert rows[1][1] == APPROVAL_DECISION_APPROVED
    assert uuid.UUID(str(rows[1][2])) == rejected.approval_id, (
        "新批准必须 `supersedes` 旧的链根 —— 否则会撞 uq_step_approvals_root"
    )


def test_approve_step_rejects_missing_version_node(env: BaselineEnv) -> None:
    """`subjectVersionId` 必须指向**存在**的版本节点（422，可读）。

    FK 也会拦（`sqlstate=23503`），但那给出的是"外键约束失败"，
    调用方不知道是哪个字段 —— 服务层这一层给出可读的 422。
    """
    error = _expect_api_error(
        lambda: _approve_step(env, subject_version_id=uuid.uuid4()),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SUBJECT_VERSION_NOT_FOUND,
    )
    assert error.details.get("field") == "subjectVersionId"


def test_approve_step_rejects_kind_entity_type_mismatch(env: BaselineEnv) -> None:
    """`subjectKind` 与节点类型**交叉核对**（矛盾在写入前就该抓到）。

    `step_approvals.subject_kind` 是"这一步的产物是什么"（受控取值），
    `version_nodes.entity_type` 是"注册表里的类型标签" —— 两者语义不同，
    但**一个声称 `BASELINE` 的批准指向一个 `BRIEF` 节点**显然是矛盾。

    断言 `expectedEntityType` / `actualEntityType` 两个键：只报"不对"
    会让调用方不知道自己错在哪一侧（是 kind 写错了，还是 id 传错了）。
    """
    brief_node = _make_brief_node(env)

    error = _expect_api_error(
        lambda: _approve_step(
            env,
            subject_kind=SUBJECT_KIND_BASELINE,
            subject_version_id=brief_node,
        ),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SUBJECT_VERSION_NOT_FOUND,
    )
    assert error.details.get("expectedEntityType") == ENTITY_TYPE_BASELINE
    assert error.details.get("actualEntityType") == ENTITY_TYPE_BRIEF
    assert _scalar(
        env,
        "select count(*) from step_approvals where project_id = :pid",
        {"pid": env.project_id},
    ) == 0, "核对失败不得留下批准行"


def test_approve_step_skips_cross_check_for_research(env: BaselineEnv) -> None:
    """`RESEARCH` **跳过**交叉核对（「无法核对」≠「核对失败」）。

    `_SUBJECT_KIND_ENTITY_TYPE[RESEARCH]` 显式映射到 `None`，因为注册表
    里没有"研究"这个 `entity_type`。若谁把 `.get()` 的 `None` 当成
    "核对失败"，步骤 2 的批准将**永远批不了** —— 而那是八步闭环的一环。

    这条同时是 `_SUBJECT_KIND_ENTITY_TYPE` 与 `SUBJECT_KINDS` 键集合
    相等（第 1 层 `test_subject_kind_entity_type_covers_all_subject_kinds`）
    这条机械判据的**行为侧证据**。
    """
    # 刻意指向一个 BRIEF 节点：若核对没被跳过，这里必然失败。
    brief_node = _make_brief_node(env)

    approval = _approve_step(
        env,
        step_no=2,
        subject_kind=SUBJECT_KIND_RESEARCH,
        subject_version_id=brief_node,
        decision=APPROVAL_DECISION_APPROVED,
        reason="研究补充已核对",
    )

    assert _scalar(
        env,
        "select count(*) from step_approvals where id = :aid",
        {"aid": approval.approval_id},
    ) == 1, "RESEARCH 的批准必须真的落库（而不是被核对误拦）"


def test_approve_step_rejects_unknown_project(env: BaselineEnv) -> None:
    """项目不存在 → 422（不是让 FK 抛 23503）。"""
    node = _step1_brief_node(env)
    draft = StepApprovalDraft(
        project_id=uuid.uuid4(),
        step_no=1,
        subject_kind=SUBJECT_KIND_BRIEF,
        subject_version_id=node,
        decision=APPROVAL_DECISION_APPROVED,
        reason="不存在的项目",
        decided_by=env.owner_id,
    )
    with session_scope(env.settings) as session:
        error = _expect_api_error(
            lambda: bs.approve_step(BaselineRepository(session), draft=draft),
            code=ErrorCode.VALIDATION_FAILED,
            reason=REASON_PROJECT_NOT_FOUND,
        )
    assert error.details.get("field") == "projectId"


def test_approve_step_rejects_step_no_out_of_range(env: BaselineEnv) -> None:
    """步骤号必须在 1~8 内。

    **必须显式判**：写 `9` 的批准单在库里看起来完全正常
    （`step_no` 只是个 int 列），而它对应不到任何一步 ——
    症状是"这一步永远没有批准"，且**不报错**。
    """
    node = _step1_brief_node(env)
    error = _expect_api_error(
        lambda: _approve_step(
            env,
            step_no=STEP_MAX + 1,
            subject_kind=SUBJECT_KIND_BRIEF,
            subject_version_id=node,
        ),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_STEP_NO_INVALID,
    )
    assert error.details.get("field") == "stepNo"

    _expect_api_error(
        lambda: _approve_step(
            env,
            step_no=STEP_MIN - 1,
            subject_kind=SUBJECT_KIND_BRIEF,
            subject_version_id=node,
        ),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_STEP_NO_INVALID,
    )


def test_approve_step_rejects_unknown_subject_kind(env: BaselineEnv) -> None:
    """`subjectKind` 受控取值校验（422，field=subjectKind）。"""
    node = _step1_brief_node(env)
    error = _expect_api_error(
        lambda: _approve_step(env, subject_kind="CONTENT", subject_version_id=node),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_SUBJECT_KIND_INVALID,
    )
    assert error.details.get("field") == "subjectKind"


def test_approve_step_rejects_unknown_decision(env: BaselineEnv) -> None:
    """`decision` 受控取值校验 —— **不复用 `subject_kind_invalid`**。

    错误码指错方向比没有错误码更糟：它会把人引到 `subjectKind` 上去查。
    """
    node = _step1_brief_node(env)
    error = _expect_api_error(
        lambda: _approve_step(env, subject_version_id=node, decision="MAYBE"),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_DECISION_INVALID,
    )
    assert error.details.get("field") == "decision"


def test_approve_step_requires_reason(env: BaselineEnv) -> None:
    """`reason` 不能为空（纯空格也算空），且报**专用**码。

    ⚠️ 收口修复（ISS-109）：此前这里复用 `exclusion_reason_required` ——
    前端拿到"排除原因必填"却是在批准上出的错，会把排查引到条目上去。
    修法与 `_require_decision` 不复用 `REASON_SUBJECT_KIND_INVALID` 同一条：
    错误码必须指向真正出错的字段。本判据同时钉住 `field` 与专用 `reason`。
    """
    node = _step1_brief_node(env)
    error = _expect_api_error(
        lambda: _approve_step(env, subject_version_id=node, reason="   "),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_APPROVAL_REASON_REQUIRED,
    )
    assert error.details.get("field") == "reason", (
        "**字段名必须正确** —— field 是调用方唯一能用来定位的线索"
    )
    assert error.details.get("reason") != REASON_EXCLUSION_REASON_REQUIRED, (
        "不得退回复用条目排除那个码"
    )


def test_approve_step_rejects_overlong_reason(env: BaselineEnv) -> None:
    """`reason` 限长（`MAX_REASON_CHARS`），回带 `maxLength` 供前端预校验。

    不限长会让 `reason` 变成"顺便把内容也塞进来"的通道 ——
    而那是审计泄露正文最常见的入口。
    """
    node = _step1_brief_node(env)
    error = _expect_api_error(
        lambda: _approve_step(env, subject_version_id=node, reason="长" * (MAX_REASON_CHARS + 1)),
        code=ErrorCode.VALIDATION_FAILED,
        reason=REASON_APPROVAL_REASON_REQUIRED,
    )
    assert error.details.get("field") == "reason"
    assert error.details.get("maxLength") == MAX_REASON_CHARS


# --- 2.4.1 Code Review MAJOR-6 的回归判据 ----------------------------------


@pytest.mark.regression
def test_approve_step_rejects_duplicate_after_supersede(env: BaselineEnv) -> None:
    """**第 2 层实测发现的 MAJOR-6（先红后绿）**：重复批同一结论会**静默累积**。

    ## 缺陷

    `service.approve_step` 用 `repo.find_root_approval(...)` 取"现状"，
    而它取的是 **`supersedes_id IS NULL` 的那条（链根）**：

        root = repo.find_root_approval(project_id=..., step_no=..., ...)
        if root is not None and root.decision == draft.decision:
            raise _conflict(...)          # 409
        ...
        supersedes_id=root.id if root is not None else draft.supersedes_id

    但 DDL 触发器（`approval_required_ddl`，本包第 4 层防线）判定"有生效
    批准"的口径是**另一条**：`decision='APPROVED'` **且未被任何行取代**。
    两者不是同一个集合 —— 链根会**永久停留在第一次的那条**上：

    | 动作 | 落库 | 链根 | 409 判据看的 |
    |---|---|---|---|
    | ① `REJECTED` | 1 行 REJECTED | ① | ① |
    | ② `APPROVED`（先拒后批） | +1 行 APPROVED，取代① | ①（**没变**） | ①（REJECTED） |
    | ③ `APPROVED`（再批一次） | +1 行 APPROVED，取代① | ① | ①（REJECTED）≠APPROVED → **放行** |
    | ④ `APPROVED`（第三次） | +1 行 … | ① | 同上 → **放行** |

    实测（`/tmp` 探针）：③④ 都成功插入，`supersedes_id` 全部指向①，
    **"这一步被批过几次"虚假增长且不报错** —— 这正是
    `approve_step` docstring 明确要防的那件事（"重复记录等于伪造审计"），
    而它只在**没有先拒过**的情形下防住了。

    ## 为什么它在步骤 3 上表现为"另一副面孔"

    步骤 3 的链根由 `create_baseline` 同事务写成 `APPROVED`。于是：

      · ②（先拒后批）时 `root.decision == APPROVED != REJECTED` → 不拦；
      · 但插入的那条 `REJECTED` 的 `supersedes_id = root.id`（=那条 APPROVED），
        把**唯一生效的批准**取代掉了；
      · 于是 DDL 触发器判定"本基线**没有**生效批准"，该基线进入
        "已批准但无生效依据"的矛盾状态，而 `require_baseline_approval`
        （它同样用 `find_root_approval`）**看不出来**，仍然放行。

    症状 1 见 `test_baseline_rejected_then_reapproved_becomes_effective`；
    这条判据钉症状 2（静默累积）。

    ## 期望

    "现状"必须取**生效的那条**（未被取代的），而不是链根。修法可以是
    `find_root_approval` 改口径、也可以新增 `find_effective_approval` ——
    因此这里只断言行为：**先拒后批之后再批同一结论 → 409**。

    ⚠️ 本判据在修复前会 FAIL（返回成功，库里多出一行）。
    """
    node = _step1_brief_node(env)

    _approve_step(
        env,
        step_no=1,
        subject_kind=SUBJECT_KIND_BRIEF,
        subject_version_id=node,
        decision=APPROVAL_DECISION_REJECTED,
        reason="第一次拒绝",
    )
    _approve_step(
        env,
        step_no=1,
        subject_kind=SUBJECT_KIND_BRIEF,
        subject_version_id=node,
        decision=APPROVAL_DECISION_APPROVED,
        reason="修正后重批",
    )

    count_before = _scalar(
        env,
        "select count(*) from step_approvals"
        " where project_id = :pid and step_no = 1",
        {"pid": env.project_id},
    )
    assert count_before == 2, "前提：先拒后批已落两行"

    error = _expect_api_error(
        lambda: _approve_step(
            env,
            step_no=1,
            subject_kind=SUBJECT_KIND_BRIEF,
            subject_version_id=node,
            decision=APPROVAL_DECISION_APPROVED,
            reason="又批了一次",
        ),
        code=ErrorCode.INVALID_TRANSITION,
        reason=REASON_ALREADY_APPROVED,
    )
    assert error.details.get("decision") == APPROVAL_DECISION_APPROVED

    assert _scalar(
        env,
        "select count(*) from step_approvals"
        " where project_id = :pid and step_no = 1",
        {"pid": env.project_id},
    ) == count_before, (
        "被拒之后**不得新增行** —— 否则「这一步被批过几次」会虚假增长，"
        "而每一次「批准」都是人为动作（`approve_step` docstring 的原始诉求）"
    )


def test_baseline_rejected_then_reapproved_becomes_effective(
    env: BaselineEnv,
) -> None:
    """**MAJOR-6 的症状 1**：拒绝一次之后，基线再也**回不到「有生效批准」**。

    ## 缺陷链条（与上一条同一根因，症状不同）

    步骤 3 的链根 = `create_baseline` 写的 `APPROVED`。当调用方对同一条
    基线投一次 `REJECTED`：

      · `find_root_approval` 返回那条 `APPROVED`，与 `REJECTED` 不同 → 放行；
      · 插入的 `REJECTED` 记 `supersedes_id = root.id`（取代那条 `APPROVED`）；
      · 于是**没有任何**"未被取代的 `APPROVED`"了 —— DDL 触发器
        （`trg_production_baselines_approval_required`）的口径下，
        这条基线**没有生效批准**；
      · 再投一次 `APPROVED` 想撤销这个拒绝时，`find_root_approval`
        仍返回那条老的 `APPROVED` → `decision == APPROVED` → **409
        `step_already_approved`**，把"重批"永久堵死。

    实测（`/tmp` 探针）：拒绝之后库里"生效的批准"= **0 条**，
    而 `require_baseline_approval` 仍然放行（它用同一个 `find_root_approval`）。

    ## 为什么这条比"多一行审计"严重

    它让**不可逆的误操作**成为可能：一次误点"拒绝"，这条基线的批准状态
    就永久停在"无生效批准"，且：

      · 服务层读不出来（`require_baseline_approval` 放行）；
      · 唯一会拦的地方是**数据库延迟触发器**，而它只在**插入新基线时**才跑 ——
        对**已存在的**这条基线永远不会再校验一次；
      · 于是这条"已批准"的基线会一直生效下去，而它的批准依据已经没了。

    ## 判据

    拒绝一次、再批一次（`APPROVED`）之后，库里必须存在**恰好一条**
    未被取代的 `APPROVED` 批准 —— 与 DDL 触发器**同一个口径**
    （这条 SQL 是照着 `approval_required_ddl` 的判据抄的，
    而不是另写一个"看起来差不多"的查询）。

    ⚠️ 本判据在修复前会 FAIL（重批那一步直接 409）。
    """
    created = _ready_and_baseline(env)
    baseline_id = created.baseline_id

    _approve_step(
        env,
        subject_version_id=baseline_id,
        decision=APPROVAL_DECISION_REJECTED,
        reason="先撤回",
    )

    _approve_step(
        env,
        subject_version_id=baseline_id,
        decision=APPROVAL_DECISION_APPROVED,
        reason="确认无误，重批",
    )

    # 与 `approval_required_ddl()` 的判据**逐条对齐**（同一个口径）。
    effective = _scalar(
        env,
        "select count(*) from step_approvals sa"
        " where sa.project_id = :pid"
        f"   and sa.step_no = {bs_ddl.BASELINE_STEP_NO}"
        "   and sa.subject_kind = :kind"
        "   and sa.subject_version_id = :bid"
        "   and sa.decision = :approved"
        "   and not exists ("
        "       select 1 from step_approvals s2 where s2.supersedes_id = sa.id"
        "   )",
        {
            "pid": env.project_id,
            "kind": SUBJECT_KIND_BASELINE,
            "bid": baseline_id,
            "approved": APPROVAL_DECISION_APPROVED,
        },
    )
    assert effective == 1, (
        "先拒后批之后必须**恰好一条**未被取代的 APPROVED —— "
        "与 `trg_production_baselines_approval_required` 同一个口径。"
        f"实际 {effective} 条。修复前这里是 0（拒绝把唯一那条 APPROVED 取代了），"
        "而服务层的 `require_baseline_approval` 看不出来。"
    )

    # 服务层也应当能读到"有生效批准"（读出来与触发器一致）。
    with session_scope(env.settings) as session:
        bs.require_baseline_approval(
            BaselineRepository(session),
            project_id=env.project_id,
            baseline_id=baseline_id,
        )


def test_require_baseline_approval_rejects_when_only_rejected_effective(
    env: BaselineEnv,
) -> None:
    """**MAJOR-6 的判据侧证据**：只有一条生效的 `REJECTED` 时必须 409。

    `require_baseline_approval` 的 docstring 说得很清楚：它是
    **第 4 层防线的服务层翻译**，"判据本身在触发器里"。因此它必须与
    触发器**同一个口径** —— 触发器要求"未被取代的 `APPROVED`"，
    那么"未被取代的 `REJECTED`"就不能算数。

    而它当前用的是 `find_root_approval`（链根），于是：

      · 拒绝一次（取代了创建时的 `APPROVED`）之后，
        库里唯一生效的是 `REJECTED`；
      · `find_root_approval` 却仍返回那条**老 `APPROVED`** → 放行。

    这条把「服务层读出来的结论」与「触发器会给出的结论」直接对照 ——
    两者不一致时，能读到的调用方（例如"试建基线但不提交"的诊断路径）
    会拿到与真实约束**相反**的答案。

    ⚠️ 本判据在修复前会 FAIL（`require_baseline_approval` 不抛错）。
    """
    created = _ready_and_baseline(env)
    baseline_id = created.baseline_id

    _approve_step(
        env,
        subject_version_id=baseline_id,
        decision=APPROVAL_DECISION_REJECTED,
        reason="撤回这条基线",
    )

    # 前提核对：库里**确实**没有未被取代的 APPROVED 了。
    assert _scalar(
        env,
        "select count(*) from step_approvals sa"
        " where sa.subject_version_id = :bid and sa.decision = :approved"
        "   and not exists ("
        "       select 1 from step_approvals s2 where s2.supersedes_id = sa.id"
        "   )",
        {"bid": baseline_id, "approved": APPROVAL_DECISION_APPROVED},
    ) == 0, "前提：拒绝已经把唯一那条 APPROVED 取代掉了"

    with session_scope(env.settings) as session:
        error = _expect_api_error(
            lambda: bs.require_baseline_approval(
                BaselineRepository(session),
                project_id=env.project_id,
                baseline_id=baseline_id,
            ),
            code=ErrorCode.INVALID_TRANSITION,
            reason=REASON_APPROVAL_REQUIRED,
        )
    assert error.details.get("baselineId") == str(baseline_id)


# --- 2.5 读路径与投影列 -----------------------------------------------------


def test_list_baselines_revision_no_comes_from_version_nodes(
    env: BaselineEnv,
) -> None:
    """列表里的"BL-v3"那个 **3 必须与 `version_nodes` 同源**。

    `production_baselines` **不存 `revision_no`**（决策 5：不造第二个真值来源），
    它只在注册表里。因此列表端点必须向 `repo.revision_numbers()` 批量取 ——
    若谁改成 `row.created_at` 排序后自己数序号，第一版数据看起来完全正常，
    而在数据被回填（补录一条历史基线）时列表与详情会给出**不同的数字**。

    判据用**两个独立来源互相钉住**：列表给的 vs 直接查注册表给的；
    再与详情端点给的第三个读数对照。

    ⚠️ **只用两条修订**：第三条会被 MAJOR-4（`newest_baseline_of` 恒返回
    链根 → 链尾判据误拒）挡住，而那条缺陷由
    `test_rebuild_active_baseline_matches_incremental_path` 覆盖。
    本判据要测的是"修订号的来源"，让它因为另一个缺陷变红会掩盖
    "这条判据自己写对了没有"。
    """
    first = _ready_and_baseline(env)
    second = _ready_and_baseline(env, supersedes_id=first.baseline_id)

    with session_scope(env.settings) as session:
        repo = BaselineRepository(session)
        summaries, total = bs.list_baselines(
            repo, project_id=env.project_id, limit=20, offset=0
        )
        expected = repo.revision_numbers([first.baseline_id, second.baseline_id])
        detail = bs.get_baseline_detail(
            repo, project_id=env.project_id, baseline_id=second.baseline_id
        )

    assert total == 2, "总数必须含全部修订（不分页窗口）"
    assert len(summaries) == 2

    # 列表按 `created_at DESC, id` 排序 → 最新的在前
    assert [s.baseline_id for s in summaries] == [
        second.baseline_id,
        first.baseline_id,
    ], "列表必须按 `created_at DESC, id`（最新在前），否则游标翻页会跳行"

    by_id = {s.baseline_id: s for s in summaries}
    assert by_id[second.baseline_id].revision_no == expected[second.baseline_id] == 2
    assert by_id[first.baseline_id].revision_no == expected[first.baseline_id] == 1

    assert detail.revision_no == by_id[second.baseline_id].revision_no, (
        "列表与详情给出的修订号必须是**同一个**（否则前端两处会显示不同的 "
        "BL-vN，而两个都「没错」）"
    )


def test_list_baselines_pagination_is_stable(env: BaselineEnv) -> None:
    """`limit`/`offset` 分页：两页不重不漏，且总数不随窗口变小。

    **总数是"本项目的全部修订"而不是"本页条数"** —— 否则前端无法
    渲染"共 N 条"，而游标翻到底也不知道停在哪。

    ⚠️ 用 **4 个条目的 `limit=2` 窗口**需要链上 4 步，而 MAJOR-4 会让
    第 3 步建不出来 —— 因此这里**不串链**：`supersedes_id` 全部为
    `None` 是做不到的（`uq_production_baselines_project_root` 只允许一条链根），
    所以改用**两个项目各 2 条链**的做法也不必要：直接测 `limit=1`
    分两页即可，同样覆盖"不重不漏 + 总数不随窗口变"。
    """
    first = _ready_and_baseline(env)
    second = _ready_and_baseline(env, supersedes_id=first.baseline_id)

    with session_scope(env.settings) as session:
        repo = BaselineRepository(session)
        page1, total1 = bs.list_baselines(
            repo, project_id=env.project_id, limit=1, offset=0
        )
        page2, total2 = bs.list_baselines(
            repo, project_id=env.project_id, limit=1, offset=1
        )

    assert total1 == total2 == 2, "总数与窗口大小无关"
    assert len(page1) == len(page2) == 1
    ids = [s.baseline_id for s in page1] + [s.baseline_id for s in page2]
    assert len(set(ids)) == 2, "两页不得重叠（否则翻页会看到重复条目）"
    assert set(ids) == {first.baseline_id, second.baseline_id}, "两页合起来必须不遗漏"
    assert ids == [second.baseline_id, first.baseline_id], (
        "排序在分页里必须稳定：第一页给最新的那条（否则 `created_at` 同微秒时"
        "两页可能给出同一条、漏掉另一条）"
    )


def test_list_baselines_rejects_unknown_project(env: BaselineEnv) -> None:
    """读路径同样要判项目存在（否则返回空列表，与"项目有 0 条基线"不可区分）。"""
    with session_scope(env.settings) as session:
        error = _expect_api_error(
            lambda: bs.list_baselines(
                BaselineRepository(session), project_id=uuid.uuid4(), limit=10, offset=0
            ),
            code=ErrorCode.VALIDATION_FAILED,
            reason=REASON_PROJECT_NOT_FOUND,
        )
    assert error.details.get("field") == "projectId"


def test_get_baseline_detail_includes_excluded_items(env: BaselineEnv) -> None:
    """详情**必须含 `EXCLUDED` 条目**（AC-24 原文）。

    过滤掉未纳入项会让"为什么没纳入"在详情接口里无处可查 ——
    而它正是半年后复盘时唯一想知道的事。

    同时钉住另一个容易写错的点：**未纳入项不计入 `item_count`**，
    但它必须**在 `items` 里**。两个读数的口径不同，混起来会让
    "这条基线纳入了几项"有两种答案。
    """
    created = _ready_and_baseline(
        env,
        selected=(
            _fact_item(),
            _fact_item(
                ref_label="不采纳的旧事实",
                inclusion=INCLUSION_EXCLUDED,
                exclusion_reason="已被 v2 事实取代",
            ),
        ),
    )

    with session_scope(env.settings) as session:
        repo = BaselineRepository(session)
        detail = bs.get_baseline_detail(
            repo, project_id=env.project_id, baseline_id=created.baseline_id
        )
        summaries, _total = bs.list_baselines(
            repo, project_id=env.project_id, limit=10, offset=0
        )
        counts = repo.count_items(created.baseline_id)

    assert len(detail.items) == 2, "详情必须含未纳入项（EXCLUDED 也在其中）"
    inclusions = [item.inclusion for item in detail.items]
    assert inclusions == [INCLUSION_INCLUDED, INCLUSION_EXCLUDED], (
        "条目按 `item_no` 排（给定顺序派生，1 起），未纳入项不排到末尾"
    )

    excluded = detail.items[1]
    assert excluded.exclusion_reason == "已被 v2 事实取代", (
        "未纳入项必须带原因 —— `ck_baseline_items_excluded_needs_reason` 也会拦，"
        "但它拦不住「原因写在了别的条目上」"
    )
    assert excluded.snapshot, "未纳入项同样要留快照（否则事后无法核对当时排除了什么）"

    assert counts == (1, 1), "`count_items` 返回 (included, excluded)"
    summary = next(s for s in summaries if s.baseline_id == created.baseline_id)
    assert summary.item_count == 1, "`item_count` 只算纳入项"
    assert summary.excluded_count == 1, "未纳入项走 `excluded_count`"


def test_get_baseline_detail_rejects_foreign_baseline(env: BaselineEnv) -> None:
    """归属不符 → **404**（而不是 403/200）。

    用**真实存在的别项目基线**（不是凭空 uuid）：凭空 uuid 只会走到
    "不存在"那一支，测不到"存在但不属于本项目"。两者给同一个 404 是
    刻意的 —— 不能通过错误码区分"存在但不是你的"与"不存在"，
    否则它就是一个跨项目探测通道。
    """
    foreign = _ready_and_baseline(env, project_id=env.other_project_id)

    with session_scope(env.settings) as session:
        error = _expect_api_error(
            lambda: bs.get_baseline_detail(
                BaselineRepository(session),
                project_id=env.project_id,
                baseline_id=foreign.baseline_id,
            ),
            code=ErrorCode.RESOURCE_NOT_FOUND,
            reason=REASON_BASELINE_NOT_FOUND,
        )
    assert error.code == ErrorCode.RESOURCE_NOT_FOUND
    assert (error.details or {}).get("reason") == REASON_BASELINE_NOT_FOUND

    # 同一个 id 用自己的项目读得到 —— 证明上面 404 的原因是"归属不符"
    with session_scope(env.settings) as session:
        detail = bs.get_baseline_detail(
            BaselineRepository(session),
            project_id=env.other_project_id,
            baseline_id=foreign.baseline_id,
        )
    assert detail.baseline_id == foreign.baseline_id


def test_revision_no_falls_back_to_zero_without_node(env: BaselineEnv) -> None:
    """`_item_view` / 列表：**注册表里没有节点时给 0，不抛错**。

    ⚠️ **正常路径下这个 fallback 不可达** —— `production_baselines.id` 有
    FK 指向 `version_nodes.id`，所以"基线行存在但节点行不存在"是数据库
    保证不会发生的事。按本项目第 2 条判据纪律（判据不能建立在数据库
    已保证的事上），这里测的是**降级函数本身**的行为，而不是
    "数据库会不会让它发生"：

        repo.revision_no(<不存在的 id>) is None
        → 列表给 0（不整页 500）

    刻意用 `repo.revision_no` / `repo.revision_numbers` 直测 ——
    它们接受任意 id，不要求有 FK。
    """
    with session_scope(env.settings) as session:
        repo = BaselineRepository(session)
        assert repo.revision_no(uuid.uuid4()) is None
        assert repo.revision_numbers([uuid.uuid4()]) == {}
        assert repo.revision_numbers([]) == {}, "空入参必须早返回，不产生 `IN ()` 空列表 SQL"

    # 列表在"节点缺失"下的读数：把查得到与查不到的两条一起喂进同一个函数
    created = _ready_and_baseline(env)
    with session_scope(env.settings) as session:
        repo = BaselineRepository(session)
        absent = uuid.uuid4()
        numbers = repo.revision_numbers([created.baseline_id, absent])
    assert numbers.get(created.baseline_id) == 1
    assert absent not in numbers, "查不到的 id 必须缺席（由调用方按 `.get(id, 0)` 给 0）"
    assert len(numbers) == 1, "只返回查得到的那条"


def test_list_approvals_includes_superseded_and_filters_by_step(
    env: BaselineEnv,
) -> None:
    """批准历史：**不筛掉被取代的那些**，且能按步骤过滤。

    `supersedes_id` 链是"先拒后批"的痕迹，而"曾经被拒过"是审计信息
    （决策 4）。筛掉会让"这一步被批了几次、中间有没有反复"查不到。

    同时钉住排序：`step_no, decided_at, id` —— 按 `decided_at` 单排
    在同一微秒内的两条上不确定（测试库上真的会发生）。
    """
    node = _step1_brief_node(env)
    rejected = _approve_step(
        env,
        step_no=1,
        subject_kind=SUBJECT_KIND_BRIEF,
        subject_version_id=node,
        decision=APPROVAL_DECISION_REJECTED,
        reason="第一次拒绝",
    )
    approved = _approve_step(
        env,
        step_no=1,
        subject_kind=SUBJECT_KIND_BRIEF,
        subject_version_id=node,
        decision=APPROVAL_DECISION_APPROVED,
        reason="修正后重批",
    )
    created = _ready_and_baseline(env)

    with session_scope(env.settings) as session:
        repo = BaselineRepository(session)
        all_rows = bs.list_approvals(repo, project_id=env.project_id)
        step1 = bs.list_approvals(repo, project_id=env.project_id, step_no=1)
        step3 = bs.list_approvals(
            repo, project_id=env.project_id, step_no=bs_ddl.BASELINE_STEP_NO
        )

    assert len(all_rows) == 3, "全部批准（含被取代的那条）"
    assert {r.approval_id for r in all_rows} == {
        rejected.approval_id,
        approved.approval_id,
        created.approval_id,
    }, "被取代的那条**不能**被筛掉"

    assert [r.approval_id for r in step1] == [
        rejected.approval_id,
        approved.approval_id,
    ], "步骤 1 的两条按 decided_at 升序（这是我第一次拒、第二次批的顺序）"
    assert [r.approval_id for r in step3] == [created.approval_id], (
        "步骤 3 只有创建基线时同事务写的那一条"
    )


def test_list_approvals_rejects_unknown_step_no(env: BaselineEnv) -> None:
    """按步骤过滤时同样要判步骤号合法（否则返回空列表，与"没有批准"不可区分）。"""
    with session_scope(env.settings) as session:
        error = _expect_api_error(
            lambda: bs.list_approvals(
                BaselineRepository(session), project_id=env.project_id, step_no=STEP_MAX + 1
            ),
            code=ErrorCode.VALIDATION_FAILED,
            reason=REASON_STEP_NO_INVALID,
        )
    assert error.details.get("field") == "stepNo"


def test_active_baseline_id_reads_projection_column(env: BaselineEnv) -> None:
    """`active_baseline_id` 读的就是投影列（**不是**自己再算一遍链尾）。

    这条刻意与 `rebuild_active_baseline` 对立：那个是**重算并写回**，
    这个是**只读**。若有人把读路径也改成"顺便重算"，那么每次读都会
    产生一次写 —— 而"读端点不写库"是本项目的接缝约定，且这个写
    在只读副本上会直接失败。
    """
    created = _ready_and_baseline(env)

    with session_scope(env.settings) as session:
        repo = BaselineRepository(session)
        value = bs.active_baseline_id(repo, project_id=env.project_id)
        # 读路径**不应当**改投影列：把值手工改掉，再读一次
        repo.set_active_baseline(project_id=env.project_id, baseline_id=None)

    assert value == created.baseline_id, "正常读到的就是创建时写下的那条"
    assert _active_baseline(env) is None, (
        "读路径不得写库 —— 手工改掉投影列之后它必须保持被改后的值"
    )

    with session_scope(env.settings) as session:
        assert bs.active_baseline_id(
            BaselineRepository(session), project_id=env.project_id
        ) is None, "再来一次读：仍然是被改后的值（证明上一次读没有偷偷写回）"


def test_active_baseline_id_rejects_unknown_project(env: BaselineEnv) -> None:
    with session_scope(env.settings) as session:
        error = _expect_api_error(
            lambda: bs.active_baseline_id(
                BaselineRepository(session), project_id=uuid.uuid4()
            ),
            code=ErrorCode.VALIDATION_FAILED,
            reason=REASON_PROJECT_NOT_FOUND,
        )
    assert error.details.get("field") == "projectId"


def test_rebuild_active_baseline_is_idempotent(env: BaselineEnv) -> None:
    """全量重算**幂等**：读两次得到同一个值（第二次不改动任何行）。

    `set_active_baseline` 用 `IS DISTINCT FROM` 把"检查是否已经是目标值"
    与"写入"压在同一条语句里，因此第二次调用 `rowcount = 0` ——
    这是"重算路径可以安全地反复跑"的依据（恢复脚本会跑很多次）。
    """
    created = _ready_and_baseline(env)

    assert _rebuild_active(env) == created.baseline_id
    assert _rebuild_active(env) == created.baseline_id
    assert _active_baseline(env) == created.baseline_id

    with session_scope(env.settings) as session:
        changed = BaselineRepository(session).set_active_baseline(
            project_id=env.project_id, baseline_id=created.baseline_id
        )
    assert changed == 0, (
        "已经是目标值时必须 `rowcount = 0`（`IS DISTINCT FROM` 的语义）—— "
        "调用方据此知道「这次没有真正改变」"
    )


def test_rebuild_active_baseline_without_baselines_clears_projection(
    env: BaselineEnv,
) -> None:
    """没有任何基线时重算 → 投影列为 `NULL`（而不是抛错或留着旧值）。

    这是"链被清空"（回滚、恢复）后的路径：留着旧值会让
    `active_baseline_id` 指向一条**已经不生效**的基线，而下游按它取
    上下文的动作不会报错。
    """
    with session_scope(env.settings) as session:
        repo = BaselineRepository(session)
        # 先手工把投影列写成某个值（模拟"曾经有基线"）
        repo.set_active_baseline(project_id=env.project_id, baseline_id=None)
    assert _active_baseline(env) is None

    assert _rebuild_active(env) is None, "没有基线时重算必须给出 None"
    assert _active_baseline(env) is None


# ===========================================================================
# 第 3 层 —— 数据库层兜底（原生 SQL，绕过 ORM 与 service）
# ===========================================================================
#
# ## 本层测什么
#
# 第 2 层走的是 `service.*`，因此**服务层与 DDL 共享的盲区**在那一层测不出来。
# 本层全部用**原生 SQL**（既不建 ORM 对象、也不调 service），
# 直接问数据库："这条不变量你自己会不会拦？"
#
# ## 为什么断言"专属措辞"而不只断言 sqlstate
#
# 三张表上挂了**九条**触发器，其中三条延迟触发器同挂 `production_baselines`
# （按名字典序 `approval_required` < `no_blocking` < `node_consistency`），
# 两条同挂 `baseline_items`，另一条在 `version_nodes` 上。
# 只断言 `23514` 无法区分"是哪一条拦下的" —— 而"被**正确的**判据拦下"
# 与"被另一条顺带拦下"是两件事（ISS-95 同族）。
# 因此每条用例都断言**该判据独有的那句话**（逐字从 `ddl.py` 取出，
# 再用探针在真库上核过；见 `evidence/m1/wp22-l3-probe.log`）。
#
# ## 可达性分组（**不是"每个 builder 铺一条"**）
#
# 实测发现三条判据在库层面**不可达**（被更强的约束压过）：
#
# | 判据 | 不可达的原因 | 本层的处理 |
# |---|---|---|
# | `node_consistency` 第 1 支（"没有对应的 version_nodes 行"） | `fk_production_baselines_id_version_nodes` 是 DEFERRABLE 且更强（`23503`） | 写成**能 FAIL 的反向判据**（见 `test_..._first_branch_is_unreachable_behind_fk`） |
# | `cross_brand` 的"基线不存在"支 | `fk_baseline_items_baseline_id_production_baselines` 更强 | 同上，合并在 `test_item_side_reference_branches_are_unreachable_behind_fk` |
# | `cross_brand` 的"知识资产不存在"支 | `fk_baseline_items_knowledge_asset_id_knowledge_assets` 更强 | 同上 |
#
# 把它们写成"一句注释"会是 ISS-95 那个坑（文档声称有防线、实际没有）：
# 反向判据断言"此刻是 FK 在兜底"，因此**若某天 FK 被去掉**，
# 它会红，提示这段说明要重写。
#
# ## 取判定：`SET CONSTRAINTS ALL IMMEDIATE`
#
# 本包三张不可变表**物理删不掉**（`rednote_reject_mutation()` 拦 DELETE），
# 连探针残留都清不掉。因此延迟约束统一用 `SET CONSTRAINTS ALL IMMEDIATE`
# 把判定提前到事务内，拿到判定后无条件 rollback（见 `_assert_deferred_blocked`）。
# 非延迟的（CHECK / UQ / FK / 不可变）在语句处即抛，走的是同一条捕获路径。


# --- 3.0 原生 SQL 与取判定的小工具 ------------------------------------------

#: `cast(:x as jsonb)` 而**不是** `:x::jsonb`：SQLAlchemy 的 `text()` 会把
#: `::` 的第二个冒号当成绑定参数前缀，实测报 `syntax error at or near ":"`
#: （本项目已踩过的坑）。`cast()` 没有这个问题。
_L3_INSERT_NODE = (
    "insert into version_nodes (id, entity_type, entity_id, revision_no,"
    " payload_hash, schema_version, project_id)"
    " values (:node_id, :entity_type, :entity_id, :revision_no,"
    " :payload_hash, 1, :project_id)"
)

#: `schema_version` 不写：它有 `server_default`，显式给值只会让
#: "默认值改了测试没跟"这件事失去覆盖。
_L3_INSERT_BASELINE = (
    "insert into production_baselines (id, project_id, brief_version_id,"
    " ruleset_version, change_reason, snapshot, created_by, supersedes_id)"
    " values (:baseline_id, :project_id, :brief_version_id, :ruleset_version,"
    " :change_reason, cast(:snapshot as jsonb), :created_by, :supersedes_id)"
)

#: 与上一条**只差一个显式列**：`schema_version` 有 `server_default`，
#: 基础 INSERT 刻意不给它，好让"默认值改了测试没跟"仍被覆盖；
#: 只有"测 `schema_version` 必须为正"那一条用例需要显式写非法值。
#: 写成独立的常量而不是对上面那条做字符串替换 —— 替换一旦失效是**静默**的
#: （列没被加进去，于是测的还是默认值，判据照样"通过"）。
_L3_INSERT_BASELINE_WITH_SCHEMA = (
    "insert into production_baselines (id, project_id, brief_version_id,"
    " ruleset_version, change_reason, snapshot, created_by, supersedes_id,"
    " schema_version)"
    " values (:baseline_id, :project_id, :brief_version_id, :ruleset_version,"
    " :change_reason, cast(:snapshot as jsonb), :created_by, :supersedes_id,"
    " :schema_version)"
)

_L3_INSERT_APPROVAL = (
    "insert into step_approvals (id, project_id, step_no, subject_kind,"
    " subject_version_id, decision, reason, decided_by, supersedes_id)"
    " values (:approval_id, :project_id, :step_no, :subject_kind,"
    " :subject_version_id, :decision, :reason, :decided_by, :supersedes_id)"
)

_L3_INSERT_ITEM = (
    "insert into baseline_items (baseline_id, item_no, item_kind, inclusion,"
    " ref_label, snapshot, snapshot_text, exclusion_reason, context_order,"
    " knowledge_asset_id, knowledge_version_id)"
    " values (:baseline_id, :item_no, :item_kind, :inclusion, :ref_label,"
    " cast(:snapshot as jsonb), :snapshot_text, :exclusion_reason,"
    " :context_order, :knowledge_asset_id, :knowledge_version_id)"
)


def _l3_unique_positive_int() -> int:
    """一个几乎不会撞的正整数。

    `version_nodes` 的唯一键是 `(entity_type, entity_id, revision_no)`，
    而本层多数用例刻意把 `BASELINE` 节点的 `entity_id` 设成**同一个项目**
    （那正是"孤立节点"与"entity_id 不一致"两条判据的前提）。
    写死 `revision_no=1` 会让**第二次运行**撞唯一约束 ——
    而报错点落在"插节点"那一句上，极易被误读成"触发器写错了"。
    `baseline_items.item_no` 同理（它也有 `(baseline_id, item_no)` 唯一键）。
    """
    return uuid.uuid4().int % (2**31 - 2) + 1


def _l3_node(env: BaselineEnv, **overrides: Any) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {
        "node_id": uuid.uuid4(),
        "entity_type": ENTITY_TYPE_BASELINE,
        "entity_id": env.project_id,
        "revision_no": _l3_unique_positive_int(),
        "payload_hash": uuid.uuid4().hex * 2,
        "project_id": env.project_id,
    }
    params.update(overrides)
    return _L3_INSERT_NODE, params


def _l3_baseline(
    node_id: uuid.UUID, **overrides: Any
) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {
        "baseline_id": node_id,
        "project_id": None,  # 调用方必须显式给（见 `_l3_legal_baseline`）
        "brief_version_id": None,
        "ruleset_version": "ruleset-v5",
        "change_reason": "第 3 层原生写入",
        "snapshot": "{}",
        "created_by": None,
        "supersedes_id": None,
    }
    params.update(overrides)
    return _L3_INSERT_BASELINE, params


def _l3_approval(
    subject_version_id: uuid.UUID, **overrides: Any
) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {
        "approval_id": uuid.uuid4(),
        "project_id": None,
        "step_no": bs_ddl.BASELINE_STEP_NO,
        "subject_kind": SUBJECT_KIND_BASELINE,
        "subject_version_id": subject_version_id,
        "decision": APPROVAL_DECISION_APPROVED,
        "reason": "第 3 层原生批准",
        "decided_by": None,
        "supersedes_id": None,
    }
    params.update(overrides)
    return _L3_INSERT_APPROVAL, params


def _l3_item(
    baseline_id: uuid.UUID, **overrides: Any
) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {
        "baseline_id": baseline_id,
        "item_no": _l3_unique_positive_int(),
        "item_kind": ITEM_KIND_FACT,
        "inclusion": INCLUSION_INCLUDED,
        "ref_label": "第 3 层条目",
        "snapshot": '{"statement": "续航 18 小时"}',
        "snapshot_text": None,
        "exclusion_reason": None,
        "context_order": 1,
        "knowledge_asset_id": None,
        "knowledge_version_id": None,
    }
    params.update(overrides)
    return _L3_INSERT_ITEM, params


def _l3_legal_baseline(
    env: BaselineEnv, *, project_id: uuid.UUID | None = None
) -> tuple[uuid.UUID, list[tuple[str, dict[str, Any]]]]:
    """一组**合法**的原生写入：节点 + 基线行 + 未被取代的 `APPROVED` 批准。

    返回 `(node_id, statements)`。多数用例只需在返回值里**替换掉其中一句**
    就能把某一条判据单独打红 —— 这正是"先让其它判据都绿"的做法：
    否则被测用例红的可能是**另一条**判据（ISS-95 同族）。

    ⚠️ **必须一次给全三句**：三条延迟触发器里有两条只在"已经有一份合规批准"
    时才轮到 `node_consistency` 执行（字典序 `approval_required` 在前）。
    少了批准那一句，想测 `entity_type` / `entity_id` 的用例会**先**被
    `approval_required` 拦下 —— 而那条消息与目标判据完全不同。
    """
    pid = env.project_id if project_id is None else project_id
    node_sql, node_params = _l3_node(env, entity_id=pid, project_id=pid)
    node_id: uuid.UUID = node_params["node_id"]
    statements = [
        (node_sql, node_params),
        _l3_baseline(
            node_id, project_id=pid, created_by=env.owner_id
        ),
        _l3_approval(node_id, project_id=pid, decided_by=env.owner_id),
    ]
    return node_id, statements


def _l3_expect_violation(
    env: BaselineEnv,
    statements: list[tuple[str, dict[str, Any]]],
    *,
    sqlstate: str,
    fragment: str,
) -> str:
    """断言"这组原生写入被拦下"，且**拦下它的正是这一条判据**。

    `fragment` 必填（不给默认值）：只断言 sqlstate 会让"被另一条判据
    顺带拦下"看起来是通过 —— 本层因此**不给省略的机会**。
    """
    actual_state, message, _detail = _assert_deferred_blocked(env, statements)
    assert actual_state == sqlstate, (
        f"期望 sqlstate={sqlstate}，实际 {actual_state}；消息：{message}"
    )
    assert fragment in message, f"期望消息含 {fragment!r}，实际：{message}"
    return message


def _l3_expect_violation_anywhere(
    env: BaselineEnv,
    statements: list[tuple[str, dict[str, Any]]],
    *,
    sqlstate: str,
    fragment: str,
) -> str:
    """同 `_l3_expect_violation`，但 `fragment` 可命中 `DETAIL` 而非主消息。

    **只在"判据本身就在 `DETAIL` 里"时用它**：唯一约束违约的主消息只有
    `duplicate key value violates unique constraint "…"`，
    "哪一行重复"（项目 id / 列值）在 `DETAIL`。若这里也用 primary 版，
    断言会永远失败并误指向"索引认错了对象"。
    两把尺子分开，读的时候一眼能看出被测的判据在哪一段。
    """
    actual_state, message, detail = _assert_deferred_blocked(env, statements)
    assert actual_state == sqlstate, (
        f"期望 sqlstate={sqlstate}，实际 {actual_state}；消息：{message}"
    )
    combined = f"{message} {detail}"
    assert fragment in combined, (
        f"期望消息（含 DETAIL）含 {fragment!r}，实际：主消息 {message!r}；"
        f"DETAIL {detail!r}"
    )
    return combined


def _l3_expect_allowed(
    env: BaselineEnv, statements: list[tuple[str, dict[str, Any]]]
) -> None:
    """**成对的反向验证**：这组原生写入必须能成功提交。

    只测"非法被拦"会漏掉"合法也被拦"（例如某条判据写成恒真、
    或某条触发器把收窄条件写反了）。本层每条"拦下"的判据都配一条
    "放行"的对照，且对照与反例**只差被测的那一处**。
    """
    with session_scope(env.settings) as session:
        for sql, params in statements:
            session.execute(text(sql), params)
        # 与 `_assert_deferred_blocked` 同一个提前取判定的手法，
        # 只是这里期望**不抛**：延迟判据在提交前就已全部通过。
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def _l3_sqlstate(error: DBAPIError) -> str:
    return getattr(error.orig, "sqlstate", "")


def _l3_message(error: DBAPIError) -> str:
    """`message_primary`（不含 `DETAIL` / `CONTEXT` / `CONTEXT` 的附加行）。

    用 primary 而不是 `str(error)`：后者会把 SQL 语句与参数一起带进来，
    而 `in` 断言只要命中参数里的某个子串就会**假通过**。
    """
    diag = getattr(error.orig, "diag", None)
    return getattr(diag, "message_primary", "") or ""


# --- 3.1 不可变保护（三张表 × 两个操作） ------------------------------------


def test_three_immutable_tables_reject_update_and_delete(
    env: BaselineEnv,
) -> None:
    """三张不可变表 × {UPDATE, DELETE} **全部**被 `23001` 拦下。

    这里刻意**逐表逐操作**跑六次，而不是"抽一张表代表"：
    触发器是 `FOR EACH ROW` 挂在**每一张表**上的，抽检只能证明被测的那一张，
    而 `immutable_trigger_names()` 是按 `IMMUTABLE_TABLES` 生成名字的 ——
    "名字生成对了"与"六条触发器都建出来了"是两件事。

    `23001` 是 `restrict_violation`，与 CHECK 的 `23514`、UQ 的 `23505`
    都不同：调用方据此把"试图改写历史"与"数据不合法"分开
    （见 `platform/immutability.py` 的说明）。

    消息里必须带**表名与操作名** —— 它们是 `RAISE` 用 `TG_TABLE_NAME`
    与 `TG_OP` 现拼的，若某天改成写死字符串，排障时就说不出
    "是哪张表的哪个操作"。
    """
    created = _ready_and_baseline(env)
    item_id = _scalar(
        env,
        "select id from baseline_items where baseline_id = :bid limit 1",
        {"bid": created.baseline_id},
    )
    assert item_id is not None, "前提：合法基线必须至少有一条条目"
    approval_id = created.approval_id

    cases: tuple[tuple[str, str, dict[str, Any]], ...] = (
        (
            "production_baselines",
            "update production_baselines set change_reason = '改写' where id = :row",
            {"row": created.baseline_id},
        ),
        (
            "production_baselines",
            "delete from production_baselines where id = :row",
            {"row": created.baseline_id},
        ),
        (
            "baseline_items",
            "update baseline_items set ref_label = '改写' where id = :row",
            {"row": item_id},
        ),
        (
            "baseline_items",
            "delete from baseline_items where id = :row",
            {"row": item_id},
        ),
        (
            "step_approvals",
            "update step_approvals set reason = '改写' where id = :row",
            {"row": approval_id},
        ),
        (
            "step_approvals",
            "delete from step_approvals where id = :row",
            {"row": approval_id},
        ),
    )

    for table, sql, params in cases:
        operation = sql.split()[0].upper()
        error = _raw_expect_error(env, sql, params)
        assert _l3_sqlstate(error) == IMMUTABLE_RECORD_SQLSTATE, (
            f"{table} 的 {operation} 期望 sqlstate={IMMUTABLE_RECORD_SQLSTATE}，"
            f"实际 {_l3_sqlstate(error)}：{_l3_message(error)}"
        )
        message = _l3_message(error)
        assert table in message, f"消息必须带表名 {table}：{message}"
        assert operation in message, f"消息必须带操作名 {operation}：{message}"


def test_information_draft_is_mutable(env: BaselineEnv) -> None:
    """**成对的反向验证**：`information_drafts` 必须**可以**改。

    它是四张表里唯一可变的（模块 docstring：只有已固化的历史才不可变，
    而工作区是"运营正在编辑的内容"）。若某天有人把它也加进
    `IMMUTABLE_TABLES`，第 2 层的写路径会全线崩掉，但"六条触发器都在"
    那条判据**照样通过** —— 因为它只测被保护的表。

    `operator_confirmed` 是 writing 的目标列：它是 PRD §7.3 条件 ③ 的载体，
    运营点确认时改的正是它。
    """
    _ready_and_baseline(env)
    _raw(
        env,
        "update information_drafts set operator_confirmed = true"
        " where project_id = :pid",
        {"pid": env.project_id},
    )
    assert _draft_row(env)["operator_confirmed"] is True


# --- 3.2 OP-88 的正向与反向判据 ---------------------------------------------


def test_isolated_baseline_node_is_rejected(env: BaselineEnv) -> None:
    """`BASELINE` 节点**没有**配对的基线行 → "孤立节点"（OP-88 反向判据）。

    这条判据只插节点、**不插基线行** —— 于是"节点先落地、基线行忘了写"
    这类半成品事务在提交时被拦下。没有它，注册表里会积累一批
    "说自己是一条基线、却没有任何内容"的节点，而下游按 id 去取详情时
    拿到 404（一个**看起来像前端 bug** 的症状）。
    """
    node_sql, node_params = _l3_node(env, entity_id=env.project_id)
    _l3_expect_violation(
        env,
        [(node_sql, node_params)],
        sqlstate=bs_ddl.CONSISTENCY_SQLSTATE,
        fragment="但没有任何 production_baselines 行（孤立节点）",
    )


@pytest.mark.cross_wp
def test_other_entity_types_are_not_affected_by_baseline_pairing(
    env: BaselineEnv,
) -> None:
    """**成对的反向验证**：同样形状的 `BRIEF` / `PLAN` 节点必须**放行**。

    反向触发器带 `WHEN (NEW.entity_type = 'BASELINE')`，与 WP18/WP20/WP23
    的三个同款触发器在**同一张表**上并存、各管各的实体类型。
    若某天有人漏写或写错这个 `WHEN`，本包会替**别的模块**的节点
    强制"必须有配对基线行"—— 而症状是别包的写入莫名其妙失败。

    ⚠️ `PLAN` 刻意也在名单里：它**没有任何**配对触发器，
    因此它是"`WHEN` 收窄彻底生效"的最干净读数（`BRIEF` 会被 WP18 的
    触发器检查，虽然本用例的构造也让它可以放行，但那是巧合而非隔离）。
    """
    statements: list[tuple[str, dict[str, Any]]] = []
    for entity_type in (ENTITY_TYPE_BRIEF, ENTITY_TYPE_PLAN):
        node_sql, node_params = _l3_node(
            env, entity_type=entity_type, entity_id=env.project_id
        )
        statements.append((node_sql, node_params))

    _l3_expect_allowed(env, statements)

    for _sql, params in statements:
        assert _scalar(
            env,
            "select count(*) from version_nodes where id = :nid",
            {"nid": params["node_id"]},
        ) == 1, "前提：两个节点确实落库了（放行不等于没写进去）"


def test_node_consistency_rejects_non_baseline_entity_type(
    env: BaselineEnv,
) -> None:
    """基线行指向一个 `BRIEF` 节点 → "不是 BASELINE"（OP-88 判据 2）。

    ⚠️ **本用例的节点 id 与基线行 id 是同一个**（`_l3_legal_baseline` 保证）。
    不这样构造就测不到这条判据：若节点 id 另取一个值，会先撞
    "孤立节点"（那条节点的基线行不存在）—— 而"触发顺序与多触发器并发"
    正是本层最容易写错的地方（实测踩过）。

    本条防线挡的是"注册一个别的类型的版本，却按基线登记"：
    它的失效是静默的 —— 基线能读回，只有"这条基线是个什么东西"
    这个问题没有答案。
    """
    node_sql, node_params = _l3_node(
        env, entity_type=ENTITY_TYPE_BRIEF, entity_id=env.project_id
    )
    node_id: uuid.UUID = node_params["node_id"]
    statements = [
        (node_sql, node_params),
        _l3_baseline(
            node_id, project_id=env.project_id, created_by=env.owner_id
        ),
        _l3_approval(
            node_id, project_id=env.project_id, decided_by=env.owner_id
        ),
    ]
    _l3_expect_violation(
        env,
        statements,
        sqlstate=bs_ddl.CONSISTENCY_SQLSTATE,
        fragment="，不是 BASELINE",
    )


def test_node_consistency_rejects_entity_id_mismatch(env: BaselineEnv) -> None:
    """节点声明的 `entity_id` ≠ 基线行的 `project_id` → "不一致"（OP-88 判据 3）。

    `BASELINE` 的 `entity_id` 必须是**项目 id**（一条基线是该项目链上的
    一个修订，见 `service.create_baseline` 的说明）。两个值不一致意味着
    "这条基线的修订号是从别的项目的序号里取的" —— 于是同一项目的
    第二条基线可能拿到修订号 1，而列表页展示的 `BL-vN` 与详情页
    给出的数字会对不上，**两个都不报错**。

    ⚠️ 节点的 `entity_id` 用**另一个真实项目**：若随便给一个不存在的 uuid，
    `baseline_node_paired` 的收窄会直接放行（它先查 `projects` 里有没有
    这一行），而我们要测的是 `node_consistency` 那一条，不是收窄那条。
    """
    node_sql, node_params = _l3_node(
        env, entity_type=ENTITY_TYPE_BASELINE, entity_id=env.other_project_id
    )
    node_id: uuid.UUID = node_params["node_id"]
    statements = [
        (node_sql, node_params),
        _l3_baseline(
            node_id, project_id=env.project_id, created_by=env.owner_id
        ),
        _l3_approval(
            node_id, project_id=env.project_id, decided_by=env.owner_id
        ),
    ]
    _l3_expected = "不一致"
    message = _l3_expect_violation(
        env,
        statements,
        sqlstate=bs_ddl.CONSISTENCY_SQLSTATE,
        fragment=_l3_expected,
    )
    assert str(env.other_project_id) in message, (
        "消息必须回带节点声明的 entity_id —— 排障时据此知道「它以为属于谁」"
    )


def test_node_consistency_first_branch_is_unreachable_behind_fk(
    env: BaselineEnv,
) -> None:
    """`node_consistency` 第 1 支（"没有对应的 version_nodes 行"）**被 FK 压过**。

    ## 这是一条"能 FAIL 的说明"，不是一句注释

    `ddl.baseline_version_consistency_ddl` 里第一支写的是
    "`production_baselines.id` 没有对应的 `version_nodes` 行"。实测：
    它**永远跑不到** —— `fk_production_baselines_id_version_nodes`
    （`DEFERRABLE INITIALLY DEFERRED`）比它更强，先给出 `23503`。

    本判据因此断言的是**"此刻兜底的是 FK"**：
    若某天有人去掉或收窄那条 FK，这里会红，提示
    "`node_consistency` 的第一支现在可达了，第 3 层需要补一条活性判据"。
    把它写成注释就没有这个提示（ISS-95：文档声称有防线比没有防线更糟）。
    """
    error = _raw_expect_error(
        env,
        "insert into production_baselines (id, project_id, brief_version_id,"
        " ruleset_version, change_reason, snapshot, created_by)"
        " values (:bid, :pid, null, 'ruleset-v5', '孤儿基线',"
        " cast('{}' as jsonb), null)",
        {"bid": uuid.uuid4(), "pid": env.project_id},
    )
    assert _l3_sqlstate(error) == "23503", (
        "期望外键违约（23503）—— 若变成 23514，说明第一支已可达，"
        f"需把本条改写成活性判据。实际：{_l3_sqlstate(error)}"
        f"：{_l3_message(error)}"
    )
    assert "fk_production_baselines_id_version_nodes" in _l3_message(error), (
        "必须确认兜底的是**那一条** FK，而不是别的外键："
        f"{_l3_message(error)}"
    )


# --- 3.3 第 4 层防线：同事务批准（OP-151） ----------------------------------


def test_baseline_without_same_transaction_approval_is_rejected(
    env: BaselineEnv,
) -> None:
    """有基线、无批准 → "没有同事务的批准"（OP-151 的硬约束化）。

    ⚠️ **节点必须与基线行、批准都在同一个事务里**：`_assert_deferred_blocked`
    正是这样构造的。若分成三个事务，第一个提交时节点就是孤立节点，
    被拦的是那条判据而不是这一条（WP18/WP20 已踩过）。

    这条防线是"基线不可被非法推进"的最后一道：服务层会**自动**生成批准
    （`create_baseline` 的第 7 步），所以本判据测的是"绕过服务层时还拦不拦"。
    """
    node_sql, node_params = _l3_node(env, entity_id=env.project_id)
    node_id: uuid.UUID = node_params["node_id"]
    statements = [
        (node_sql, node_params),
        _l3_baseline(
            node_id, project_id=env.project_id, created_by=env.owner_id
        ),
    ]
    _l3_expect_violation(
        env,
        statements,
        sqlstate=bs_ddl.CONSISTENCY_SQLSTATE,
        fragment="在提交时没有同事务的批准",
    )


def test_rejected_only_approval_does_not_count(env: BaselineEnv) -> None:
    """只有一条 `REJECTED` 批准 → **不构成依据**（拒绝不是放行）。

    判据原文是 `sa.decision = 'APPROVED'`。若写成"存在任意一条批准"，
    "先拒后批"会退化成"拒了就自动通过" —— 而那是把**否决**读成了**同意**。

    消息与"完全没有批准"**同一条**（`ddl.py` 把两种情形合成一句）：
    因此本判据额外断言"库里确实有一条批准"，证明红的不是"没有批准"。
    """
    node_sql, node_params = _l3_node(env, entity_id=env.project_id)
    node_id: uuid.UUID = node_params["node_id"]
    statements = [
        (node_sql, node_params),
        _l3_baseline(
            node_id, project_id=env.project_id, created_by=env.owner_id
        ),
        _l3_approval(
            node_id,
            project_id=env.project_id,
            decision=APPROVAL_DECISION_REJECTED,
            decided_by=env.owner_id,
        ),
    ]
    _l3_expect_violation(
        env,
        statements,
        sqlstate=bs_ddl.CONSISTENCY_SQLSTATE,
        fragment="在提交时没有同事务的批准",
    )
    assert _scalar(
        env,
        "select count(*) from step_approvals where subject_version_id = :sid",
        {"sid": node_id},
    ) == 0, "整事务回滚 —— 那条 REJECTED 也不该留下（否则它会污染后续用例）"


def test_superseded_approval_does_not_count(env: BaselineEnv) -> None:
    """`APPROVED` 但**已被取代** → 不构成依据（"先批后撤"必须真的撤掉）。

    判据里的 `NOT EXISTS (select 1 from step_approvals s2
    where s2.supersedes_id = sa.id)` 是这一条的落点。缺了它，
    "先批准、再撤回"会变成"批准仍然有效" —— 而撤回动作看起来成功了
    （确实多了一条记录），于是**没人会去查**。
    """
    node_sql, node_params = _l3_node(env, entity_id=env.project_id)
    node_id: uuid.UUID = node_params["node_id"]
    granted = uuid.uuid4()
    statements = [
        (node_sql, node_params),
        _l3_baseline(
            node_id, project_id=env.project_id, created_by=env.owner_id
        ),
        _l3_approval(
            node_id,
            project_id=env.project_id,
            approval_id=granted,
            decided_by=env.owner_id,
        ),
        _l3_approval(
            node_id,
            project_id=env.project_id,
            decision=APPROVAL_DECISION_REJECTED,
            supersedes_id=granted,
            decided_by=env.owner_id,
        ),
    ]
    _l3_expect_violation(
        env,
        statements,
        sqlstate=bs_ddl.CONSISTENCY_SQLSTATE,
        fragment="或该批准已被取代",
    )


def test_baseline_with_effective_approval_is_allowed(env: BaselineEnv) -> None:
    """**成对的反向验证**：节点 + 基线行 + 未被取代的 `APPROVED` → 放行。

    与 `test_superseded_approval_does_not_count` **只差** `supersedes_id`
    那一处。若没有这条对照，把判据写成恒假（例如漏写 `APPROVED` 那个条件、
    或把 `NOT EXISTS` 写反）会让上面三条全部"通过"，
    而合法基线**一条都建不出来** —— 一个只在生产上才发现的故障。
    """
    _node_id, statements = _l3_legal_baseline(env)
    _l3_expect_allowed(env, statements)


# --- 3.4 第 5 层防线：阻断项（AC-09） ---------------------------------------


def test_open_blocking_conflict_blocks_baseline_at_db_level(
    env: BaselineEnv,
) -> None:
    """草稿里有 `blocking=true` 且 `state<>SETTLED` 的冲突 → 不得创建基线。

    消息里必须回带**冲突 id 的样本**（`string_agg` 拼的）：
    只说"有 1 项未处置"会让运营无从下手 —— 而 AC-09 的原文要求
    "阻断项必须逐条处置"。回带 id 让报错本身可操作。

    判据刻意写成**对明细行的否定式查询**（`NOT EXISTS` 风格），
    **不读任何 count 列** —— 本包没有 count 列（决策 7），
    而"由别处维护的计数与明细脱节时不报错"是本项目已踩 10 次的同族问题。
    """
    _ready_and_baseline(env)  # 先建一条合法的，制造"项目已存在草稿"
    _write_draft(
        env,
        conflicts=[
            {
                "conflictId": "k-l3-blocking",
                "kind": CONFLICT_KIND_SOURCE,
                "blocking": True,
                "state": CONFLICT_STATE_OPEN,
                "action": CONFLICT_ACTION_ADOPT_ONE,
            }
        ],
    )
    _node_id, statements = _l3_legal_baseline(env)
    # ⚠️ 这是**第二条**基线（第一条已在上面的 `_ready_and_baseline` 里建过），
    # 因此它会撞链根唯一索引 —— 但那不是本判据要测的。
    # 为避免混淆，显式让它成为链上的一条替代基线。
    tail = _active_baseline(env)
    assert tail is not None
    baseline_sql, baseline_params = statements[1]
    baseline_params["supersedes_id"] = tail
    # 链尾唯一性由部分唯一索引保证：链根已被第一条占用，
    # 因此这条必须带 `supersedes_id` 才轮得到 `no_blocking` 来拦。
    statements[1] = (baseline_sql, baseline_params)

    message = _l3_expect_violation(
        env,
        statements,
        sqlstate=bs_ddl.CONSISTENCY_SQLSTATE,
        fragment="项未处置的阻断问题",
    )
    assert "k-l3-blocking" in message, (
        f"必须回带未处置冲突的 id（运营据此逐条处置）：{message}"
    )
    assert "不得创建基线（AC-09）" in message, (
        f"消息必须引用验收原文编号，便于与需求文档对账：{message}"
    )


def test_settled_blocking_conflict_does_not_block(env: BaselineEnv) -> None:
    """**成对的反向验证**：同样是 `blocking=true`，但 `state=SETTLED` → 放行。

    与上一条**只差** `state` 一个取值。若判据漏写 `state` 的比对，
    "已处置的阻断项"仍会挡住创建 —— 而症状是"处置完了还是建不了基线"，
    运营只能猜是哪里没做。
    """
    _ready_and_baseline(env)
    _write_draft(
        env,
        conflicts=[
            {
                "conflictId": "k-l3-settled",
                "kind": CONFLICT_KIND_SOURCE,
                "blocking": True,
                "state": CONFLICT_STATE_SETTLED,
                "action": CONFLICT_ACTION_ADOPT_ONE,
            }
        ],
    )
    tail = _active_baseline(env)
    assert tail is not None
    _node_id, statements = _l3_legal_baseline(env)
    statements[1][1]["supersedes_id"] = tail
    _l3_expect_allowed(env, statements)


def test_no_draft_at_all_does_not_block(env: BaselineEnv) -> None:
    """**收窄的反向验证**：项目**没有草稿**时放行（判据正确地放行，而非漏判）。

    `information_drafts` 一项目一份，但"还没开始审核"是合法的中间态。
    此时没有明细行 → "不存在未处置的阻断冲突"这个否定式**天然成立**。
    若某天有人改成"必须先有草稿"，本项目所有"先建基线再补审核"的
    历史数据都会失效。

    用 `other_project`（`env` fixture 里另一个项目，从未写过草稿）。
    """
    assert _scalar(
        env,
        "select count(*) from information_drafts where project_id = :pid",
        {"pid": env.other_project_id},
    ) == 0, "前提：这个项目确实没有草稿"

    _node_id, statements = _l3_legal_baseline(env, project_id=env.other_project_id)
    _l3_expect_allowed(env, statements)


# --- 3.5 第 3 层防线：基线项引用的范围 --------------------------------------


def test_item_cross_brand_reference_is_rejected(env: BaselineEnv) -> None:
    """条目引用**别品牌**的知识 → AC-24 原文拒绝（跨品牌事实不得迁移）。

    用服务层建的**真知识行**（不是手搓形状）：这条判据读的是
    `knowledge_assets` 的 `scope`/`brand_id`/`product_id` 三个投影列，
    手搓行会让"服务层写进去的 scope 到底长什么样"这件事失去覆盖。
    """
    created = _ready_and_baseline(env)
    foreign_asset, foreign_version = _make_knowledge(
        env, scope=SCOPE_BRAND, brand_id=env.other_brand_id
    )
    item_sql, item_params = _l3_item(
        created.baseline_id,
        knowledge_asset_id=foreign_asset,
        knowledge_version_id=foreign_version,
    )
    message = _l3_expect_violation(
        env,
        [(item_sql, item_params)],
        sqlstate=bs_ddl.CONSISTENCY_SQLSTATE,
        fragment="基线项引用的知识属于品牌",
    )
    assert str(env.other_brand_id) in message, (
        f"必须回带实际品牌 id（排障时据此知道「它以为属于谁」）：{message}"
    )
    assert "（AC-24：跨品牌事实不得迁移）" in message, (
        f"消息必须引用验收原文编号：{message}"
    )


def test_item_cross_product_reference_is_rejected(env: BaselineEnv) -> None:
    """条目引用**同品牌别产品**的知识 → 拒绝。

    与跨品牌走**同一个 `RAISE` 块**（`SCOPE_MATCH_SAME_PRODUCT` 分支），
    因此断言的是"属于产品 … 而本项目属于产品"。这条判据的存在理由：
    产品的知识范围比品牌窄，若只挡跨品牌，同品牌下的产品串味会静默通过。
    """
    created = _ready_and_baseline(env)
    foreign_asset, foreign_version = _make_knowledge(
        env,
        scope=SCOPE_PRODUCT,
        brand_id=env.brand_id,
        product_id=env.other_product_id,
    )
    item_sql, item_params = _l3_item(
        created.baseline_id,
        knowledge_asset_id=foreign_asset,
        knowledge_version_id=foreign_version,
    )
    message = _l3_expect_violation(
        env,
        [(item_sql, item_params)],
        sqlstate=bs_ddl.CONSISTENCY_SQLSTATE,
        fragment="基线项引用的知识属于产品",
    )
    assert str(env.other_product_id) in message


def test_item_cross_project_reference_is_rejected(env: BaselineEnv) -> None:
    """条目引用**别项目**的 `PROJECT` 范围知识 → 拒绝（用另一句措辞）。

    三个分支的措辞**刻意不同**（品牌/产品/项目各一句）：合并成一句会让
    "这次失败是哪一层范围越界"读不出来，而三者的修法不同。
    """
    created = _ready_and_baseline(env)
    foreign_asset, foreign_version = _make_knowledge(
        env,
        scope=SCOPE_PROJECT,
        brand_id=env.brand_id,
        product_id=env.product_id,
        project_id=env.other_project_id,
    )
    item_sql, item_params = _l3_item(
        created.baseline_id,
        knowledge_asset_id=foreign_asset,
        knowledge_version_id=foreign_version,
    )
    message = _l3_expect_violation(
        env,
        [(item_sql, item_params)],
        sqlstate=bs_ddl.CONSISTENCY_SQLSTATE,
        fragment="基线项引用的知识属于项目",
    )
    assert "，不是本项目 " in message, (
        "项目分支的措辞与品牌/产品分支不同（后两者用「而本项目属于」）—— "
        f"若三处被合并成一句，这里会红：{message}"
    )


def test_item_version_of_another_asset_is_rejected(env: BaselineEnv) -> None:
    """`knowledge_version_id` 属于**别的资产** → 拒绝（AC-24 溯源链）。

    两条 FK 各自只保证"这行资产存在""这行版本存在"，
    **没有任何东西保证"这个版本属于这个资产"** —— 那正是本触发器补的洞。
    它的失效是静默的：基线能建、能读回、快照也在，只有溯源链是错的
    （"这条事实来自哪一版"的答案是错的）。
    """
    created = _ready_and_baseline(env)
    own_asset, _own_version = _own_knowledge(env)
    _other_asset, other_version = _make_knowledge(
        env,
        scope=SCOPE_PRODUCT,
        brand_id=env.brand_id,
        product_id=env.product_id,
    )
    item_sql, item_params = _l3_item(
        created.baseline_id,
        knowledge_asset_id=own_asset,
        knowledge_version_id=other_version,
    )
    message = _l3_expect_violation(
        env,
        [(item_sql, item_params)],
        sqlstate=bs_ddl.CONSISTENCY_SQLSTATE,
        fragment="（AC-24：条目须保存实际选用的版本）",
    )
    assert str(own_asset) in message and str(other_version) in message, (
        f"消息必须同时回带「声明的资产」与「实际的版本」两个 id：{message}"
    )


def test_item_with_own_knowledge_is_allowed(env: BaselineEnv) -> None:
    """**成对的反向验证**：本产品自己的知识 + 本资产的版本 → 放行。

    与上面四条"拒绝"只差引用对象。没有这条对照，
    把 `IS DISTINCT FROM` 写成 `=`、或把 `RETURN NULL` 写错位置，
    会让四条拒绝判据全部通过而**合法条目一条也写不进去**。
    """
    created = _ready_and_baseline(env)
    own_asset, own_version = _own_knowledge(env)
    item_sql, item_params = _l3_item(
        created.baseline_id,
        knowledge_asset_id=own_asset,
        knowledge_version_id=own_version,
    )
    _l3_expect_allowed(env, [(item_sql, item_params)])


def test_item_without_knowledge_asset_is_allowed(env: BaselineEnv) -> None:
    """**收窄的反向验证**：不引用知识资产的条目（研究资料）→ 放行。

    两条触发器都以 `knowledge_asset_id IS NULL` / `knowledge_version_id IS NULL`
    开头直接 `RETURN NULL`。理由：原型 L93~102 的"21 份选用研究资料"
    不是知识资产，对它们强制范围检查会让**合法的基线创建失败**。

    这里用 `ITEM_KIND_RESEARCH` 并显式给 `context_order`：
    研究类的顺序不由 `context_order.py` 派生，必须显式指定
    （那是服务层的判据，第 3 层只关心它写不进去）。
    """
    created = _ready_and_baseline(env)
    item_sql, item_params = _l3_item(
        created.baseline_id,
        item_kind=ITEM_KIND_RESEARCH,
        ref_label="研究资料 R-014",
        snapshot='{"title": "通勤场景调研"}',
        context_order=2,
    )
    _l3_expect_allowed(env, [(item_sql, item_params)])


def test_item_with_asset_but_no_version_is_allowed(env: BaselineEnv) -> None:
    """**收窄的反向验证**：给了资产、不给版本 → 放行。

    `asset_version_scope` 与 `cross_brand_reference` 的收窄条件**不同**
    （前者看 `knowledge_version_id`，后者看 `knowledge_asset_id`）。
    只给资产是合法的（"引用了这条知识，但不钉死某一版"在导入场景里存在），
    而若有人把两个收窄条件写成同一个，这条会红。
    """
    created = _ready_and_baseline(env)
    own_asset, _own_version = _own_knowledge(env)
    item_sql, item_params = _l3_item(
        created.baseline_id, knowledge_asset_id=own_asset
    )
    _l3_expect_allowed(env, [(item_sql, item_params)])


def test_item_reference_branches_are_unreachable_behind_fk(
    env: BaselineEnv,
) -> None:
    """两条"引用不存在"的 `RAISE` 支**被 FK 压过**（能 FAIL 的说明）。

    ## 被压过的是哪两句

    1. `cross_brand_reference` 的"基线项 … 的基线 … 不存在"；
    2. `cross_brand_reference` 的"基线项引用的知识资产 … 不存在"。

    实测两条都跑不到：对应的 FK（`fk_baseline_items_baseline_id_production_baselines`
    与 `fk_baseline_items_knowledge_asset_id_knowledge_assets`）先给出 `23503`。

    `ddl.py` 的注释已说明它们是**防御性分支**（"FK 已保证存在，
    走到这里只有一种可能：有人在同一事务里删了那行"），
    而本判据把这个说法变成**可执行的**：它断言"此刻是那两条 FK 在兜底"。
    若某天 FK 被去掉或改成不可延迟（那会让合法的同事务写入顺序失效），
    这里会红，提示第 3 层需要补活性判据。
    """
    created = _ready_and_baseline(env)

    ghost_baseline = _raw_expect_error(
        env,
        _L3_INSERT_ITEM,
        _l3_item(uuid.uuid4())[1],
    )
    assert _l3_sqlstate(ghost_baseline) == "23503", (
        f"期望 FK 违约（23503），实际 {_l3_sqlstate(ghost_baseline)}："
        f"{_l3_message(ghost_baseline)}"
    )
    assert "fk_baseline_items_baseline_id_production_baselines" in _l3_message(
        ghost_baseline
    ), f"必须确认兜底的是那一条 FK：{_l3_message(ghost_baseline)}"

    ghost_asset = _raw_expect_error(
        env,
        _L3_INSERT_ITEM,
        _l3_item(created.baseline_id, knowledge_asset_id=uuid.uuid4())[1],
    )
    assert _l3_sqlstate(ghost_asset) == "23503", (
        f"期望 FK 违约（23503），实际 {_l3_sqlstate(ghost_asset)}："
        f"{_l3_message(ghost_asset)}"
    )
    assert "fk_baseline_items_knowledge_asset_id_knowledge_assets" in _l3_message(
        ghost_asset
    ), f"必须确认兜底的是那一条 FK：{_l3_message(ghost_asset)}"


# --- 3.6 第 1 层防线：CHECK 与部分唯一索引 ----------------------------------


def test_step_approvals_check_constraints_reject_bad_values(
    env: BaselineEnv,
) -> None:
    """`step_approvals` 的五条 CHECK 逐条实测（主动给出**约束名**）。

    断言约束名而不只断言 `23514`：这张表上同时有 CHECK、唯一索引与
    `23001` 的不可变触发器，只断言码会让"是哪条规则拦下"读不出来。

    ⚠️ 约束名是**实测值**而不是模型里的字面量：`platform/base.py` 的
    `NAMING_CONVENTION` 把 `CheckConstraint(name="decision_valid")`
    落成 `ck_step_approvals_decision_valid`（加了表名前缀）。
    写模型里的短名会让本判据**永远失败**，而失败原因看起来像"约束没建"。
    """
    node_id = _make_brief_node(env)
    cases: tuple[tuple[str, str, dict[str, Any]], ...] = (
        (
            "ck_step_approvals_decision_valid",
            "decision",
            {"decision": "MAYBE"},
        ),
        (
            "ck_step_approvals_subject_kind_valid",
            "subject_kind",
            {"subject_kind": "SCENE"},
        ),
        (
            "ck_step_approvals_step_no_in_range",
            "step_no",
            {"step_no": STEP_MAX + 1},
        ),
        (
            "ck_step_approvals_reason_not_empty",
            "reason",
            {"reason": ""},
        ),
        (
            "ck_step_approvals_supersedes_not_self",
            "supersedes_id",
            {},  # 自比由 `_l3_approval_self_reference` 处理（需要 id 已知）
        ),
    )

    for constraint, field, overrides in cases:
        if field == "supersedes_id":
            # 自比必须让 `supersedes_id == id`，而 id 在构造时才生成。
            approval_id = uuid.uuid4()
            sql, params = _l3_approval(
                node_id,
                project_id=env.project_id,
                approval_id=approval_id,
                supersedes_id=approval_id,
            )
        else:
            sql, params = _l3_approval(node_id, project_id=env.project_id, **overrides)
        error = _raw_expect_error(env, sql, params)
        assert _l3_sqlstate(error) == bs_ddl.CONSISTENCY_SQLSTATE, (
            f"{constraint} 期望 23514，实际 {_l3_sqlstate(error)}："
            f"{_l3_message(error)}"
        )
        assert constraint in _l3_message(error), (
            f"期望被 {constraint} 拦下，实际：{_l3_message(error)}"
        )


def test_production_baselines_check_constraints_reject_bad_values(
    env: BaselineEnv,
) -> None:
    """`production_baselines` 的**五条** CHECK 逐条实测（主动给出约束名）。

    ⚠️ 这些 CHECK 是**非延迟**的，因此它们**先于**三条延迟触发器判定 ——
    本用例不插版本节点也能拿到 CHECK 的报错（`id` 上的那条 FK 是
    DEFERRABLE，判定在提交时；CHECK 在语句处即抛）。
    这一点值得写下来：若有人把校验挪进触发器，本组判据的报错时机会变。

    ⚠️ 约束名是**实测值**：`NAMING_CONVENTION` 的
    `ck_%(table_name)s_%(constraint_name)s` 把 `CheckConstraint(name="snapshot_is_object")`
    落成 `ck_production_baselines_snapshot_is_object`。**并且** SQLAlchemy 在
    `Table.constraints` 里存的就是渲染后的全名（实测），所以本层既不能写短名、
    也不能再手工加一次前缀。
    """
    cases: tuple[tuple[str, dict[str, Any]], ...] = (
        (
            "ck_production_baselines_ruleset_version_not_empty",
            {"ruleset_version": ""},
        ),
        (
            "ck_production_baselines_change_reason_not_empty",
            {"change_reason": ""},
        ),
        (
            "ck_production_baselines_schema_version_positive",
            {"schema_version": 0},
        ),
        (
            "ck_production_baselines_snapshot_is_object",
            {"snapshot": "[]"},
        ),
        (
            "ck_production_baselines_supersedes_not_self",
            {},  # 自比要求 `supersedes_id == id`，id 在构造时才生成
        ),
    )

    for constraint, overrides in cases:
        params: dict[str, Any] = {
            "baseline_id": uuid.uuid4(),
            "project_id": env.project_id,
            "brief_version_id": None,
            "ruleset_version": "ruleset-v5",
            "change_reason": "第 3 层原生写入",
            "snapshot": "{}",
            "created_by": None,
            "supersedes_id": None,
        }
        params.update(overrides)
        if constraint == "ck_production_baselines_supersedes_not_self":
            # 自比必须让 `supersedes_id == id`（`baseline_id` 与 `supersedes_id`
            # 是同一列族，构造时才定下值）。
            params["supersedes_id"] = params["baseline_id"]
            sql = _L3_INSERT_BASELINE
        elif constraint == "ck_production_baselines_schema_version_positive":
            # 该列有 `server_default`，基础 INSERT 刻意不写它；
            # 只有这条用例需要显式给一个非法值，用专门的变体而不是字符串替换。
            sql = _L3_INSERT_BASELINE_WITH_SCHEMA
        else:
            sql = _L3_INSERT_BASELINE
        error = _raw_expect_error(env, sql, params)
        assert _l3_sqlstate(error) == bs_ddl.CONSISTENCY_SQLSTATE, (
            f"{constraint} 期望 23514，实际 {_l3_sqlstate(error)}："
            f"{_l3_message(error)}"
        )
        assert constraint in _l3_message(error), (
            f"期望被 {constraint} 拦下，实际：{_l3_message(error)}"
        )


def test_baseline_items_check_constraints_reject_bad_values(
    env: BaselineEnv,
) -> None:
    """`baseline_items` 的六条 CHECK 逐条实测。

    其中三条是**跨列条件**（`excluded_needs_reason` / `included_has_no_reason`
    / `version_ref_needs_asset_ref`）—— 它们挡的是"状态与原因不一致"，
    而那种不一致不会让任何读路径报错，只会让下游拿到矛盾的两条信息。
    """
    created = _ready_and_baseline(env)
    cases: tuple[tuple[str, str, dict[str, Any]], ...] = (
        ("ck_baseline_items_snapshot_not_empty", {"snapshot": "{}"}),
        (
            "ck_baseline_items_excluded_needs_reason",
            {"inclusion": INCLUSION_EXCLUDED, "exclusion_reason": None},
        ),
        (
            "ck_baseline_items_included_has_no_reason",
            {"inclusion": INCLUSION_INCLUDED, "exclusion_reason": "为什么"},
        ),
        (
            "ck_baseline_items_version_ref_needs_asset_ref",
            {"knowledge_asset_id": None, "knowledge_version_id": uuid.uuid4()},
        ),
        ("ck_baseline_items_context_order_positive", {"context_order": 0}),
        ("ck_baseline_items_item_no_positive", {"item_no": 0}),
    )

    for constraint, overrides in cases:
        sql, params = _l3_item(created.baseline_id, **overrides)
        error = _raw_expect_error(env, sql, params)
        assert _l3_sqlstate(error) == bs_ddl.CONSISTENCY_SQLSTATE, (
            f"{constraint} 期望 23514，实际 {_l3_sqlstate(error)}："
            f"{_l3_message(error)}"
        )
        assert constraint in _l3_message(error), (
            f"期望被 {constraint} 拦下，实际：{_l3_message(error)}"
        )


def test_baseline_items_unique_constraints_reject_duplicates(
    env: BaselineEnv,
) -> None:
    """两条 UQ 逐条实测：`(baseline_id, item_no)` 与 `(baseline_id, knowledge_asset_id)`。

    ⚠️ **UQ 的正确性只能靠"插第二行"来测** —— 单行插入无法区分
    "唯一约束生效"与"约束根本不存在"。两条用例都插两行。

    第二条的含义是"同一条知识不能既纳入又排除"：两条行同时存在时
    "这条被采纳了吗"有两个矛盾答案，而下游只会取先读到的那条。
    `knowledge_asset_id` 为 NULL 的行**不参与**这个唯一性
    （NULL 之间不相等），那是刻意的：多条无资产引用的研究资料可以并存。
    """
    created = _ready_and_baseline(env)
    own_asset, own_version = _own_knowledge(env)

    same_no = _l3_unique_positive_int()
    first = _l3_item(created.baseline_id, item_no=same_no)
    second = _l3_item(created.baseline_id, item_no=same_no)
    _l3_expect_allowed(env, [first])

    error = _raw_expect_error(env, second[0], second[1])
    assert _l3_sqlstate(error) == "23505", (
        f"期望唯一违约（23505），实际 {_l3_sqlstate(error)}：{_l3_message(error)}"
    )
    assert "uq_baseline_items_baseline_item_no" in _l3_message(error), _l3_message(error)

    first_asset = _l3_item(
        created.baseline_id,
        knowledge_asset_id=own_asset,
        knowledge_version_id=own_version,
    )
    second_asset = _l3_item(
        created.baseline_id,
        knowledge_asset_id=own_asset,
        knowledge_version_id=own_version,
    )
    _l3_expect_allowed(env, [first_asset])

    error = _raw_expect_error(env, second_asset[0], second_asset[1])
    assert _l3_sqlstate(error) == "23505"
    assert "uq_baseline_items_baseline_asset" in _l3_message(error), _l3_message(error)


def test_null_asset_items_do_not_collide(env: BaselineEnv) -> None:
    """**收窄的反向验证**：两条 `knowledge_asset_id IS NULL` 的条目可以并存。

    这是 `uq_baseline_items_baseline_asset` 的滑动部分。若有人把它改成
    非空默认值（例如用 `COALESCE` 或 `NULLS NOT DISTINCT`），
    "21 份选用研究资料"这种合法场景会**第二份就写不进去** ——
    而一处 UQ 报错看起来像"数据重复"，不像"约束写错了"。
    """
    created = _ready_and_baseline(env)
    first = _l3_item(created.baseline_id, knowledge_asset_id=None)
    second = _l3_item(created.baseline_id, knowledge_asset_id=None)
    _l3_expect_allowed(env, [first, second])


def test_single_chain_root_per_project_is_enforced_by_index(
    env: BaselineEnv,
) -> None:
    """每个项目至多一条**链根**（`supersedes_id IS NULL` 的部分唯一索引）。

    用**原生 SQL 插第二条链根**（绕过服务层）从数据库侧确认这条不变量。
    服务层走得通并不意味着库层拦得住 —— 而这一条正是"基线链不分叉"的
    第一层防线：分叉之后 `projects.active_baseline_id` 只能指向其中一条，
    症状是"当前生效的基线"与"链尾"不一致，且**不报错**。

    ⚠️ 第二条基线必须**另取一个版本节点 id**（不能复用第一条的）：
    否则先撞 `version_nodes` 的唯一键，测到的是另一条约束。
    """
    first = _ready_and_baseline(env)
    assert first.created is True

    node_sql, node_params = _l3_node(env, entity_id=env.project_id)
    node_id: uuid.UUID = node_params["node_id"]
    second = [
        (node_sql, node_params),
        _l3_baseline(
            node_id, project_id=env.project_id, created_by=env.owner_id
        ),
        _l3_approval(
            node_id, project_id=env.project_id, decided_by=env.owner_id
        ),
    ]
    # 链根已被第一条占用 → 撞部分唯一索引。
    # 注意报错发生在**插基线行**那一句（唯一索引是非延迟的）。
    message = _l3_expect_violation_anywhere(
        env,
        second,
        sqlstate="23505",
        fragment="uq_production_baselines_project_root",
    )
    # 再确认"重复的正是本项目"：这句在 `DETAIL` 里（primary 只有约束名），
    # 所以上面用 `_anywhere` 版；这里直接断言 detail 部分被带回来了。
    assert str(env.project_id) in message, (
        f"`DETAIL` 里应带冲突键（本项目 id）：{message}"
    )


def test_information_drafts_is_one_per_project(env: BaselineEnv) -> None:
    """`information_drafts` 一项目一份（R6）。

    用原生 SQL 插第二份 → 撞 `uq_information_drafts_project_id`。
    服务层用 `require_draft`（懒创建 + 幂等）保证这件事，而库层的
    UQ 是"两个请求同时首次打开步骤 3"时的兜底 ——
    没有它，并发下会留下**两份**工作区，之后 `GET` 拿到哪一份取决于
    查询顺序（一个"数据偶尔不对"的经典来源）。
    """
    _ready_and_baseline(env)
    error = _raw_expect_error(
        env,
        "insert into information_drafts (project_id) values (:pid)",
        {"pid": env.project_id},
    )
    assert _l3_sqlstate(error) == "23505", (
        f"期望唯一违约（23505），实际 {_l3_sqlstate(error)}：{_l3_message(error)}"
    )
    assert "uq_information_drafts_project_id" in _l3_message(error), _l3_message(error)


def test_layer_three_constraint_names_exist_in_the_database(
    env: BaselineEnv,
) -> None:
    """**本层断言的约束名必须真的在库里**（否则前面那些断言是空气）。

    ## 为什么需要这一条

    本层是**原生 SQL**，拿不到 ORM 对象，只能把约束名写成字符串。
    若只写字符串而没有任何对账，某天有人把
    `CheckConstraint(name="snapshot_not_empty")` 改名后，
    第 3 层会**照旧通过** —— 因为它断言的是"那条 SQL 会失败"，
    而失败原因会悄悄变成别的约束（或 FK、或触发器）。
    这是本项目已现身 10 次的同族坑（ISS-78/84/85/86/94/95）。

    ## 判据的两半

    1. 模型里**声明的每个** CHECK，其库名必须是 `ck_{表}_{短名}`
       （`platform/base.py` 的 `NAMING_CONVENTION` 是唯一来源）；
    2. 库里这四张表的 CHECK **数量**必须等于声明数量 ——
       前半句只能发现"改名了"，发现不了"有人只加在数据库里"。

    ⚠️ **实测发现的一处反直觉**：SQLAlchemy 在 `Table.constraints` 里存的
    `constraint.name` **已经是渲染后的全名**（`ck_information_drafts_claims_is_array`），
    不是模型源码里写的短名。因此这里**不能**再手工拼一次 `ck_{表}_` 前缀 ——
    第一版正是那么写的，报错形如"缺少
    `ck_information_drafts_ck_information_drafts_claims_is_array`"，
    而库里明明有它。这个坑值得留在这里：它**看起来像约束没建**，
    实际是判据自己拼错了名字。
    """
    for table in OWN_TABLES:
        declared = {
            constraint.name
            for constraint in table.constraints
            if constraint.__class__.__name__ == "CheckConstraint"
        }
        assert declared, f"{table.name} 应当声明了 CHECK 约束"
        # 命名约定是唯一来源：库名必须以 `ck_{表}_` 开头，
        # 这条断言防止"模型里换成了别的命名风格"而下面仍然通过。
        for name in declared:
            assert name.startswith(f"ck_{table.name}_"), (
                f"{table.name} 的 CHECK 名 {name!r} 不符合 "
                f"`NAMING_CONVENTION` 的 `ck_%(table_name)s_%(constraint_name)s`"
            )

        actual = {
            row[0]
            for row in (
                _raw(
                    env,
                    "select conname from pg_constraint"
                    " where conrelid = to_regclass(:t) and contype = 'c'",
                    {"t": table.name},
                )
                or []
            )
        }
        assert declared <= actual, (
            f"{table.name} 缺少这些 CHECK：{sorted(declared - actual)}；"
            f"库里的全部：{sorted(actual)}。"
            "库名由 `NAMING_CONVENTION` 的 `ck_%(table_name)s_"
            "%(constraint_name)s` 生成 —— 改名后本层那些字符串必须跟着改"
        )
        assert actual == declared, (
            f"{table.name} 的 CHECK 数量不一致：库里有而模型没声明 "
            f"{sorted(actual - declared)}。多出来的约束没有对应的模型声明，"
            "因此没有第二条判据与它对账"
        )


def test_partial_unique_indexes_keep_their_where_clause(env: BaselineEnv) -> None:
    """两条部分唯一索引的 **`WHERE` 子句**必须在库里可见（`indexdef` 含它）。

    "链根唯一"与"批准链根唯一"这两条不变量的**全部含义都在 `WHERE` 里**：
    去掉 `WHERE supersedes_id IS NULL` 它们会退化成
    "每项目只能有一条基线""同一版只能有一条批准" ——
    而后者会让"先拒后批"变成不可能（`test_approve_step_rejects_duplicate_after_supersede`
    覆盖的服务层症状），且同样**静默**（症状迟到：第二次写入时）。

    本判据读 `pg_indexes.indexdef`：它把 `WHERE` 一起渲染出来，
    因此是"这一条索引确实带条件"的直接读数 ——
    只查 `indexname` 存在的话，退化成普通唯一索引也照样通过。
    """
    cases = (
        ("production_baselines", "uq_production_baselines_project_root"),
        ("step_approvals", "uq_step_approvals_root"),
    )
    for table, index_name in cases:
        definitions = _raw(
            env,
            "select indexdef from pg_indexes"
            " where tablename = :t and indexname = :i",
            {"t": table, "i": index_name},
        )
        assert definitions, f"{table} 上找不到索引 {index_name}"
        definition = definitions[0][0]
        assert "UNIQUE" in definition, f"{index_name} 必须是唯一索引：{definition}"
        assert "supersedes_id IS NULL" in definition, (
            f"{index_name} 的 `WHERE` 子句丢了 —— 它会退化成"
            f"「整体唯一」，而那会让合法写入被静默拒绝：{definition}"
        )
