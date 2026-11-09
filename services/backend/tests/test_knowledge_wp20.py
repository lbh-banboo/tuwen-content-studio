"""WP20 测试：六类知识 Schema / CRUD 与版本 / 全员检索列表。

覆盖 WP20 验收原文（研发计划 04 §4.3）：
> **归档可追溯，生效版本与旧引用并存**

**测试策略（四层）**：

1. **纯函数层**（不碰数据库）：六类 Schema 的必填判据与归一化、范围自洽规则
   （"事实范围必须明确"）、版本状态投影的事件序列组合、`allowed_actions`。
   这些是"规则正确性"的权威测试 —— 投影是纯函数，因此 ARCHIVE/RESTORE
   交叉、多次激活、SUPERSEDE 的传递可以用**构造出来的事件序列**穷举，
   不必建库、不必造数据。
2. **服务层 + 真库**：创建（草稿）、加版本、发布事务（ACTIVATE/ARCHIVE/
   RESTORE 三动作 + 重复动作的 409）、列表筛选、引用可见性过滤、
   提案决策（ACCEPT 生成版本与生效事件 / REJECT 必须理由）。
   端点是薄层，因此**验收主要打在服务层**。
3. **数据库层兜底**：用**原生 SQL** 绕过 ORM 与 service，验证 CHECK、
   部分索引、不可变触发器、**OP-88 的延迟一致性触发器**、事件范围、
   `active_version_id` 归属。这一层是本包最重要的测试之一：
   **OP-88 的 `KNOWLEDGE` 那一半在本包闭合**，而"延迟约束"若只测 ORM 路径，
   等于没测它"在提交时才生效"这个特性。
4. **HTTP 层**：九个端点的状态码、响应形状、权限（写要 `KNOWLEDGE_MANAGE`）、
   `If-Match` 语义、错误体形状。端点是薄层，因此这一层只覆盖"接线是否正确"，
   不重复服务层的判据。

**为什么第 3 层必须存在**：不可变保护与跨表不变量的设计动机就是
"绕过 ORM 的写入路径（psql / 数据回填 / 运维脚本）没有代码评审"。
若测试也只用 ORM，那么测试与实现共享同一个盲区。

---

## 本文件的建表机制

**不生成迁移文件**（那会让另一分支的 `alembic upgrade head` 撞上
"Multiple head revisions are present"），改为 module 级 fixture
用 `Base.metadata.create_all(engine, tables=[...])` 建**自己那五张表**，
再用 `ddl.all_trigger_ddl()` 建六个触发器（**必须 DROP → CREATE**：
`CREATE TRIGGER` 不是幂等的，本机实测会报 `DuplicateObject`）。

**先 DROP 再 CREATE**：测试库里可能有本包早先的探针残留（WP18 实测过）。
但**只 DROP 本包自己的五张表**，绝不碰 `version_nodes`（共用表）——
它由 WP11 的迁移建出，删它会连带影响别的包。

## 为什么"零残留"用**基线差分**而不是"全表 0 行"

表里有别的包的合法遗留（WP18 实测：`brief_versions` 52 行 /
`brief_claims` 46 行 / `brief_issues` 6 行 `测试产品`），
而且不可变表**物理删不掉**（`DELETE FROM knowledge_versions` 会被
`rednote_reject_mutation()` 拦下 —— WP18 实测连自己的探针残留都删不掉）。
因此判据是"本次新增 = 0"，不是"表为空"。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from conftest import cross_package_foreign_keys, drop_own_tables

from rednote.modules.identity.models import ROLE_ADMIN, ROLE_OPERATOR, User
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.knowledge import ddl as kn_ddl
from rednote.modules.knowledge import models as kn_models
from rednote.modules.knowledge import projection as kn_projection
from rednote.modules.knowledge import schema as kn_schema
from rednote.modules.knowledge import service as kn_service
from rednote.modules.knowledge.models import (
    KnowledgeAsset,
    KnowledgeChunk,
    KnowledgeProposal,
    KnowledgePublication,
    KnowledgeVersion,
)
from rednote.modules.knowledge.repository import KnowledgeRepository
from rednote.modules.knowledge.types import (
    ACTION_ACTIVATE,
    ACTION_ARCHIVE,
    ACTION_RESTORE,
    ACTION_SUPERSEDE,
    ASSET_STATE_ACTIVE,
    ASSET_STATE_ARCHIVED,
    ASSET_STATE_DRAFT,
    ASSET_STATE_INDEXING,
    ASSET_STATE_SUPERSEDED,
    CATEGORIES,
    CATEGORY_FACT,
    CATEGORY_FEEDBACK_EXPERIMENT,
    CATEGORY_RESEARCH,
    CATEGORY_RULE,
    CATEGORY_SCENE,
    CATEGORY_STYLE,
    CATEGORIES_REQUIRING_CONCRETE_SCOPE,
    CLIENT_PUBLICATION_ACTIONS,
    DECISION_ACCEPT,
    DECISION_REJECT,
    MAX_TITLE_CHARS,
    PROPOSAL_STATE_ACCEPTED,
    PROPOSAL_STATE_PENDING,
    PROPOSAL_STATE_REJECTED,
    PUBLICATION_ACTIONS,
    SCOPE_BRAND,
    SCOPE_GLOBAL,
    SCOPE_PRODUCT,
    SCOPE_PROJECT,
    SCOPES,
    VERSION_STATES,
    VERSION_STATE_ACTIVE,
    VERSION_STATE_DRAFT,
    VERSION_STATE_SUPERSEDED,
    AssetProjection,
    PublicationEvent,
    VersionDraft,
)
from rednote.modules.projects.models import Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.modules.versions.models import ENTITY_TYPE_KNOWLEDGE, VersionNode
from rednote.modules.versions.repository import VersionRepository
from rednote.platform.configuration import Settings
from rednote.platform.database import get_engine, reset_engines, session_scope
from rednote.platform.errors import ApiError
from rednote.platform.immutability import IMMUTABLE_RECORD_SQLSTATE

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"

#: 本包自己的五张表（`version_nodes` **不在其中** —— 它是共用表）。
OWN_TABLES: tuple[Any, ...] = (
    KnowledgeAsset.__table__,
    KnowledgeVersion.__table__,
    KnowledgePublication.__table__,
    KnowledgeChunk.__table__,
    KnowledgeProposal.__table__,
)

#: DROP 顺序：**引用方在前**（先删引用别人的表，否则撞 FK）。
DROP_ORDER: tuple[str, ...] = (
    "knowledge_chunks",
    "knowledge_publications",
    "knowledge_versions",
    "knowledge_proposals",
    "knowledge_assets",
)


def _test_database_url() -> str:
    """DSN 从环境变量取（分支用 `rednote_test_wp20`），缺省本机 postgres。"""
    return os.environ.get("REDNOTE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL).strip()


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
    """只建本包的五张表 + 六个触发器。

    **先 DROP 自己的五张表**：测试库里可能有早先的探针残留，
    而 `create_all` 只对"表不存在"是幂等的 —— 表已存在但结构是旧的时候
    它会**静默跳过**，于是测试跑在一个结构不对的表上。
    （`version_nodes` 不删：它是 WP11 的迁移建的共用表。）

    **触发器必须 DROP → CREATE**：`CREATE TRIGGER` 不是幂等的
    （本机实测报 `DuplicateObject: trigger "trg_..." already exists`）。

    `reset_engines()` 先跑：conftest 的 `_isolate_settings_cache` 是
    autouse 的 function 级 fixture，module 级 fixture 拿到的 engine
    可能已被上一轮清掉 —— 重取一次最稳。
    """
    reset_engines()
    settings = _make_settings()
    engine = get_engine(settings)

    # 夹具前后核对「跨包入向外键」集合必须不变 —— 这条判据就是为 ISS-106 立的：
    # 缺了它，`DROP ... CASCADE` 会静默摘掉 WP22/WP21 建在本包表上的外键，
    # 全量 1511 条测试依然全绿（见 conftest.drop_own_tables 的说明）。
    fks_before = cross_package_foreign_keys(engine, DROP_ORDER)

    # 只删「没有跨包入向外键」的表；`knowledge_assets` / `knowledge_versions` /
    # `knowledge_chunks` 被 WP22 / WP21 引用，**必须保留**（删了就破坏别人）。
    dropped, kept = drop_own_tables(engine, DROP_ORDER)

    KnowledgeAsset.metadata.create_all(engine, tables=list(OWN_TABLES))

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
        ) in kn_ddl.all_trigger_ddl():
            conn.execute(text(drop_trigger))  # 幂等关键
            conn.execute(text(func_ddl))
            conn.execute(text(trigger_ddl))
    yield


# ===========================================================================
# 数据夹具（自包含，不依赖 conftest 的隐式状态）
# ===========================================================================


@dataclass
class KnowledgeEnv:
    settings: Settings
    project_id: uuid.UUID
    owner_id: uuid.UUID
    brand_id: uuid.UUID
    product_id: uuid.UUID
    admin_id: uuid.UUID


def _create_user(
    settings: Settings, *, role: str = ROLE_OPERATOR, name: str = "知识测试用户"
) -> uuid.UUID:
    with session_scope(settings) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name=name,
            role=role,
        )
        return user.id


def _seed_project(settings: Settings, *, owner_id: uuid.UUID) -> tuple[
    uuid.UUID, uuid.UUID, uuid.UUID
]:
    """造 brand / product / project，返回 `(brand_id, product_id, project_id)`。"""
    with session_scope(settings) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name=f"测试产品-{uuid.uuid4().hex[:6]}")
        session.add(product)
        session.flush()
        project = Project(
            project_code=f"KNW-{uuid.uuid4().hex[:8].upper()}",
            name="知识测试项目",
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
def env() -> KnowledgeEnv:
    reset_engines()
    settings = _make_settings()
    owner = _create_user(settings)
    admin = _create_user(settings, role=ROLE_ADMIN, name="知识测试管理员")
    brand_id, product_id, project_id = _seed_project(settings, owner_id=owner)
    return KnowledgeEnv(
        settings=settings,
        project_id=project_id,
        owner_id=owner,
        brand_id=brand_id,
        product_id=product_id,
        admin_id=admin,
    )


def _fact_payload(statement: str = "续航 18 小时") -> dict[str, Any]:
    """一条 `FACT` 的最小合法 `structured_payload`。"""
    return {
        "fact.statement": {"status": "FILLED", "value": statement},
    }


def _payload_for(category: str, value: str = "内容") -> dict[str, Any]:
    """按类别造一个最小合法 payload（用的是各类的**必填字段**）。"""
    required = kn_schema.REQUIRED_FIELDS_BY_CATEGORY[category]
    assert required, f"{category} 必须有至少一个必填字段（见 schema.py 的判据）"
    return {required[0]: {"status": "FILLED", "value": value}}


def _create_fact(
    env: KnowledgeEnv,
    *,
    statement: str = "续航 18 小时",
    scope: str = SCOPE_PRODUCT,
    title: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """建一条 `FACT`（`PRODUCT` 范围），返回 `(asset_id, version_id)`。"""
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        created = kn_service.create_asset(
            repo,
            category=CATEGORY_FACT,
            title=title or f"续航事实-{uuid.uuid4().hex[:6]}",
            scope=scope,
            brand_id=env.brand_id if scope == SCOPE_BRAND else None,
            product_id=env.product_id if scope == SCOPE_PRODUCT else None,
            project_id=env.project_id if scope == SCOPE_PROJECT else None,
            body=statement,
            structured_payload=_fact_payload(statement),
            change_reason="初始版本",
            actor_id=actor_id or env.owner_id,
        )
        return created.asset.id, created.version.id


def _publish(
    env: KnowledgeEnv,
    *,
    asset_id: uuid.UUID,
    action: str,
    version_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> Any:
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        asset = repo.get_asset(asset_id)
        assert asset is not None
        return kn_service.publish(
            repo,
            asset=asset,
            action=action,
            version_id=version_id,
            actor_id=env.owner_id,
            reason=reason,
        )


def _view(env: KnowledgeEnv, asset_id: uuid.UUID) -> Any:
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        asset = repo.get_asset(asset_id)
        assert asset is not None
        return kn_service.asset_view(repo, asset=asset)


def _events(*triples: tuple[int, str, Any]) -> list[PublicationEvent]:
    """构造 (sequence, action, version_id) 三元组的投影输入。

    **刻意用构造的事件而不是真库数据**：投影是纯函数，
    它的正确性（ARCHIVE/RESTORE 交叉、多次激活、SUPERSEDE 传递）
    与数据库无关。建库测它只会慢，且测不出更多东西。
    """
    return [
        PublicationEvent(sequence=seq, action=action, version_id=vid)
        for seq, action, vid in triples
    ]


# ===========================================================================
# 第 1 层：纯函数
# ===========================================================================


# --- 1.1 六类 Schema -------------------------------------------------------


def test_six_categories_match_spec() -> None:
    """六类取值必须是规格源 02 §8 的原文（且**恰好六个**）。"""
    assert set(CATEGORIES) == {
        "FACT",
        "RESEARCH",
        "SCENE",
        "STYLE",
        "RULE",
        "FEEDBACK_EXPERIMENT",
    }
    assert len(CATEGORIES) == 6


def test_every_category_has_at_least_one_required_field() -> None:
    """**每类都必须有至少一个必填结构字段**。

    这是规格源 02 §8 的备注「类别对应 Schema，**禁止无结构散文替代事实字段**」
    的结构性落点。某一类若没有必填字段，`missing_required_fields` 对它
    永远返回空元组 —— 于是 `structured_payload = {"source.note": ...}`
    这种"只有一点溯源说明"的载荷会通过校验，而它的**实质内容是空的**。
    那正是这条备注要禁止的东西。
    """
    for category in CATEGORIES:
        required = kn_schema.REQUIRED_FIELDS_BY_CATEGORY[category]
        assert required, f"{category} 没有必填字段 —— 该类的 payload 可以空着通过"


def test_required_fields_belong_to_their_category() -> None:
    """必填字段必须**属于它自己那一类**（不能是跨类通用字段）。

    若某一类的必填字段是 `source.note`（六类共用），那等于"所有类
    都要求填来源说明" —— 而来源说明是可选溯源，不是实质内容。
    """
    for category in CATEGORIES:
        own = {spec.path for spec in kn_schema.CATEGORY_FIELDS[category]}
        for path in kn_schema.REQUIRED_FIELDS_BY_CATEGORY[category]:
            assert path in own, (
                f"{category} 的必填字段 {path} 不在它自己的字段集里 —— "
                f"这会让别的类也背上同一条要求"
            )


def test_common_fields_apply_to_all_six() -> None:
    """通用溯源字段对六类都生效（"六类都必须能追溯"的结构性落点）。"""
    for category in CATEGORIES:
        paths = {spec.path for spec in kn_schema.fields_for(category)}
        for spec in kn_schema.COMMON_FIELDS:
            assert spec.path in paths, f"{category} 缺通用字段 {spec.path}"


def test_field_paths_are_globally_unique() -> None:
    """字段路径全局唯一 —— 否则 `field_spec(path)` 无法确定归属。

    **注意 `ALL_FIELDS` 里同一路径会出现多次**（`source.note` 是六类共用的，
    因此它在 `ALL_FIELDS` 里出现六遍）。唯一性由 `FIELD_BY_PATH`
    这个"路径 → 规格"的映射表达：它的键数必须等于去重后的路径数。
    """
    paths = [spec.path for spec in kn_schema.ALL_FIELDS]
    assert len(kn_schema.FIELD_BY_PATH) == len(set(paths))
    # 共用字段确实被重复展开了六次（不是漏加）：
    common = [spec.path for spec in kn_schema.COMMON_FIELDS]
    for path in common:
        assert paths.count(path) == len(CATEGORIES)
    # 每一类的字段集内部**不重复**（重复会让同一字段在两个位置显示）。
    for category in CATEGORIES:
        own = [spec.path for spec in kn_schema.fields_for(category)]
        assert len(own) == len(set(own)), f"{category} 的字段集里有重复路径"


def test_empty_field_values_covers_every_field() -> None:
    """新版本的初始 payload **预置全部字段且状态是 MISSING**。

    `MISSING` 是一个显式事实（"这个字段我们知道该有，但还没填"），
    与"这个字段不在 Schema 里"是两件事。前端据此渲染完整表单。
    """
    for category in CATEGORIES:
        values = kn_schema.empty_field_values(category)
        expected = {spec.path for spec in kn_schema.fields_for(category)}
        assert set(values) == expected
        assert all(item["status"] == "MISSING" for item in values.values())
        assert all(item["value"] is None for item in values.values())


def test_normalize_none_gives_full_missing_shape() -> None:
    """`raw=None` 给出合法起点，而不是空对象。

    空对象会被 `models.py` 的非空 CHECK 拦下（23514）——
    那是"没给 payload"这个**程序错误**的合理归宿，
    但走服务层的调用方不该撞到它。
    """
    payload = kn_schema.normalize_payload(CATEGORY_FACT, None)
    assert payload  # 非空
    assert set(payload) == {spec.path for spec in kn_schema.fields_for(CATEGORY_FACT)}
    assert kn_schema.missing_required_fields(CATEGORY_FACT, payload) == (
        "fact.statement",
    )


def test_normalize_accepts_bare_value() -> None:
    """裸值（前端直接传 `{"fact.statement": "续航 18 小时"}`）能归一化。"""
    payload = kn_schema.normalize_payload(
        CATEGORY_FACT, {"fact.statement": "续航 18 小时"}
    )
    assert payload["fact.statement"] == {
        "status": "FILLED",
        "value": "续航 18 小时",
    }


def test_normalize_rejects_unknown_path() -> None:
    """未知路径**显式报错**，不静默丢弃。

    静默丢弃会让"字段拼错了"表现为"这个字段没填" —— 前者是程序缺陷，
    后者是正常状态，两者的处理方式完全不同。
    """
    with pytest.raises(ValueError, match="未知的知识字段路径"):
        kn_schema.normalize_payload(CATEGORY_FACT, {"fact.not_exist": "x"})


def test_normalize_rejects_field_from_another_category() -> None:
    """**路径放错类别要报错**（这是最容易漏的一条校验）。

    若只查路径存在性，`style.guideline` 写进 `FACT` 会静默通过 ——
    而下游按 `FACT` 取 `fact.statement` 时永远取不到。
    """
    with pytest.raises(ValueError, match="不属于类别"):
        kn_schema.normalize_payload(
            CATEGORY_FACT, {"style.guideline": "首图要有人"}
        )


def test_normalize_rejects_filled_with_empty_value() -> None:
    """标 `FILLED` 但值为空 → 报错（状态与值必须一致）。

    **不"以某一方为准"**：那是替调用方猜意图，而猜测的方向决定了
    校验会不会放行 —— 猜"以 status 为准"就会放行一条空的事实。
    """
    with pytest.raises(ValueError, match="标为 FILLED 但值为空"):
        kn_schema.normalize_payload(
            CATEGORY_FACT, {"fact.statement": {"status": "FILLED", "value": ""}}
        )


def test_normalize_rejects_missing_with_nonempty_value() -> None:
    """标 `MISSING` 但值非空 → 报错（只清状态不清值会让下游以为可以用）。"""
    with pytest.raises(ValueError, match="标为 MISSING 但值非空"):
        kn_schema.normalize_payload(
            CATEGORY_FACT, {"fact.statement": {"status": "MISSING", "value": "x"}}
        )


def test_not_applicable_requires_reason() -> None:
    """`NOT_APPLICABLE` 必须填原因。

    一个没有理由的"不适用"无法复核 —— 下一个人只能重走一遍判断。
    理由放在 `value` 位置（与 WP18 同形）。
    """
    with pytest.raises(ValueError, match="必须填写原因"):
        kn_schema.normalize_payload(
            CATEGORY_FACT,
            {"fact.statement": {"status": "NOT_APPLICABLE", "value": ""}},
        )
    ok = kn_schema.normalize_payload(
        CATEGORY_FACT,
        {"fact.statement": {"status": "NOT_APPLICABLE", "value": "本产品不适用"}},
    )
    assert kn_schema.missing_required_fields(CATEGORY_FACT, ok) == ()


def test_multi_field_requires_list() -> None:
    """多值字段的值必须是数组（允许标量会让下游为两种形状写分支）。"""
    with pytest.raises(ValueError, match="是多值字段"):
        kn_schema.normalize_payload(
            CATEGORY_FACT,
            {
                "fact.statement": "续航 18 小时",
                "fact.effective_conditions": "常温",
            },
        )


def test_has_value_treats_false_and_zero_as_filled() -> None:
    """`False` / `0` 算"有内容"；`""` / `[]` / `{}` / `None` 算"没填"。"""
    assert kn_schema.has_value(False)
    assert kn_schema.has_value(0)
    assert not kn_schema.has_value("")
    assert not kn_schema.has_value("   ")
    assert not kn_schema.has_value([])
    assert not kn_schema.has_value({})
    assert not kn_schema.has_value(None)


# --- 1.2 范围自洽（"事实范围必须明确"）-------------------------------------


def test_fact_cannot_be_global() -> None:
    """**`FACT` + `scope='GLOBAL'` 必须被拒** —— 本包核心验收落点之一。

    规格源 02 §8 原文：「全员可管理；**事实范围必须明确**」。
    "全员可管理"意味着任何登录成员都能写知识，那么一条"适用于所有品牌"
    的事实断言会**横穿所有项目** —— 而 WP21 的验收原文里有一句
    直接相关的话：「近似品牌案例**不串事实**」。串事实的源头正是它。
    """
    reason = kn_schema.scope_violation(
        category=CATEGORY_FACT,
        scope=SCOPE_GLOBAL,
        brand_id=None,
        product_id=None,
        project_id=None,
    )
    assert reason is not None
    assert "范围必须明确" in reason


def test_only_fact_requires_concrete_scope() -> None:
    """**只有 `FACT` 受限**，其余五类允许 `GLOBAL`。

    "小红书图文首图要有人"这类风格约定本就不该绑定品牌；
    把六类一起限死只会逼运营编一个假的 `brand_id`。
    """
    assert CATEGORIES_REQUIRING_CONCRETE_SCOPE == frozenset({CATEGORY_FACT})
    for category in CATEGORIES:
        if category == CATEGORY_FACT:
            continue
        assert (
            kn_schema.scope_violation(
                category=category,
                scope=SCOPE_GLOBAL,
                brand_id=None,
                product_id=None,
                project_id=None,
            )
            is None
        ), f"{category} 不该被要求明确范围"


def test_scope_requires_matching_qualifier_column() -> None:
    """范围声明必须有对应的限定列（否则是一句无法执行的声明）。

    `scope='BRAND'` 而没有 `brand_id`，在检索时会被当成"适用于所有品牌" ——
    因为过滤条件 `brand_id = ?` 匹配不上任何值，而不过滤又不报错。
    """
    for scope, column, kwargs in (
        (SCOPE_BRAND, "brand_id", {"brand_id": None, "product_id": None, "project_id": None}),
        (SCOPE_PRODUCT, "product_id", {"brand_id": None, "product_id": None, "project_id": None}),
        (SCOPE_PROJECT, "project_id", {"brand_id": None, "product_id": None, "project_id": None}),
    ):
        reason = kn_schema.scope_violation(
            category=CATEGORY_STYLE, scope=scope, **kwargs
        )
        assert reason is not None and column in reason


def test_global_scope_rejects_any_qualifier() -> None:
    """**反向也必须成立**：`GLOBAL` 不该带任何限定列。

    "全组织知识（但只属于这个品牌）"自相矛盾，且两种解读会导致
    不同的检索结果。
    """
    reason = kn_schema.scope_violation(
        category=CATEGORY_STYLE,
        scope=SCOPE_GLOBAL,
        brand_id=uuid.uuid4(),
        product_id=None,
        project_id=None,
    )
    assert reason is not None and "brand_id" in reason


def test_scope_required_column_covers_every_scope() -> None:
    """`SCOPE_REQUIRED_COLUMN` 必须覆盖全部范围取值。

    漏一个的话，那个范围的校验会静默退化成"不需要任何限定列"——
    与 `models.py` 的 CHECK 逐条写的那一份就漂移了。
    """
    assert set(kn_schema.SCOPE_REQUIRED_COLUMN) == set(SCOPES)
    assert set(SCOPES) == {"GLOBAL", "BRAND", "PRODUCT", "PROJECT"}


# --- 1.3 版本状态投影（纯函数）---------------------------------------------


def test_projection_empty_events_yields_draft() -> None:
    """没有事件 → 全 DRAFT、无生效版本、asset 状态 DRAFT。"""
    v1 = uuid.uuid4()
    result = kn_projection.project_versions([], version_ids=(v1,))
    assert result.active_version_id is None
    assert result.archived is False
    assert result.state == ASSET_STATE_DRAFT
    assert result.versions[v1].state == VERSION_STATE_DRAFT


def test_projection_activate_sets_active_and_supersedes_previous() -> None:
    """**`ACTIVATE` 顺带 SUPERSEDE 旧版本**（规格源 §277 的"切换"蕴含此意）。

    若要求调用方额外发一条 SUPERSEDE，就存在"忘发"的窗口：
    那一刻会有两个版本同时 ACTIVE，而没有 `active_version_id`
    能同时指向它们 —— 下游取"哪个是当前版本"会随查询顺序变化。
    """
    v1, v2 = uuid.uuid4(), uuid.uuid4()
    result = kn_projection.project_versions(
        _events((1, ACTION_ACTIVATE, v1), (2, ACTION_ACTIVATE, v2)),
        version_ids=(v1, v2),
    )
    assert result.active_version_id == v2
    assert result.versions[v2].state == VERSION_STATE_ACTIVE
    assert result.versions[v1].state == VERSION_STATE_SUPERSEDED
    # 每个版本都知道"它是第几个生效的"（供展示与排序）。
    assert result.versions[v1].activation_no == 1
    assert result.versions[v2].activation_no == 2


def test_projection_archive_keeps_active_version() -> None:
    """**ARCHIVE 只置归档，不动 `active_version_id`** —— 本包关键取舍。

    验收原文「归档可追溯，**生效版本与旧引用并存**」：
    归档后旧引用仍要能解析到内容。若归档把 `active_version_id` 清空，
    版本虽不可变仍可按 id 直读，但"这条知识当前是什么"就没有答案了。
    """
    v1 = uuid.uuid4()
    result = kn_projection.project_versions(
        _events((1, ACTION_ACTIVATE, v1), (2, ACTION_ARCHIVE, v1)),
        version_ids=(v1,),
    )
    assert result.archived is True
    assert result.state == ASSET_STATE_ARCHIVED
    assert result.active_version_id == v1  # **仍在**
    assert result.versions[v1].state == VERSION_STATE_ACTIVE


def test_projection_restore_clears_archived_without_touching_history() -> None:
    """`RESTORE` **通过新事件**恢复，不改历史 —— "归档可追溯"的落点。

    事件流里完整保留"归档过又恢复"这条轨迹。
    """
    v1 = uuid.uuid4()
    result = kn_projection.project_versions(
        _events(
            (1, ACTION_ACTIVATE, v1),
            (2, ACTION_ARCHIVE, v1),
            (3, ACTION_RESTORE, v1),
        ),
        version_ids=(v1,),
    )
    assert result.archived is False
    assert result.state == ASSET_STATE_ACTIVE
    assert result.versions[v1].state == VERSION_STATE_ACTIVE


def test_projection_archive_restore_multiple_times() -> None:
    """多次归档/恢复交叉 —— 最终态只由**最后一个**事件决定。"""
    v1 = uuid.uuid4()
    result = kn_projection.project_versions(
        _events(
            (1, ACTION_ACTIVATE, v1),
            (2, ACTION_ARCHIVE, v1),
            (3, ACTION_RESTORE, v1),
            (4, ACTION_ARCHIVE, v1),
            (5, ACTION_RESTORE, v1),
        ),
        version_ids=(v1,),
    )
    assert result.archived is False
    assert result.state == ASSET_STATE_ACTIVE


def test_projection_activate_from_archived_restores() -> None:
    """**归档后直接 ACTIVATE 新版本也能恢复**（不必先 RESTORE）。

    规格源 §277 只描述了 ACTIVATE 这一条路径，因此它必须能让知识
    从归档态回到可用态 —— 否则"归档后再发新版"需要客户端记得先 RESTORE
    再 ACTIVATE，而漏掉任一步都会得到一条**永远显示为已归档**的新版本。
    """
    v1, v2 = uuid.uuid4(), uuid.uuid4()
    result = kn_projection.project_versions(
        _events(
            (1, ACTION_ACTIVATE, v1),
            (2, ACTION_ARCHIVE, v1),
            (3, ACTION_ACTIVATE, v2),
        ),
        version_ids=(v1, v2),
    )
    assert result.archived is False
    assert result.active_version_id == v2
    assert result.state == ASSET_STATE_ACTIVE


def test_projection_supersede_does_not_move_active_version() -> None:
    """`SUPERSEDE(v)` **不动 `active_version_id`** —— 哪怕 v 正是当前生效版本。

    动它会留下"当前有什么在生效"悬空。规格源 §277 的切换只能由 ACTIVATE 做。
    """
    v1 = uuid.uuid4()
    result = kn_projection.project_versions(
        _events((1, ACTION_ACTIVATE, v1), (2, ACTION_SUPERSEDE, v1)),
        version_ids=(v1,),
    )
    assert result.active_version_id == v1
    assert result.versions[v1].state == VERSION_STATE_SUPERSEDED


def test_projection_mark_is_idempotent() -> None:
    """重复把同一版本标成同一状态是**空操作** —— 不能抹掉 `activation_no`。

    `SUPERSEDE(old)` 可能在 `ACTIVATE(new)` 之后**再**来一次（数据回填、
    事件重投）。若覆盖 `activation_no`，"它是第几个生效的"这段历史就丢了。
    """
    v1, v2 = uuid.uuid4(), uuid.uuid4()
    result = kn_projection.project_versions(
        _events(
            (1, ACTION_ACTIVATE, v1),
            (2, ACTION_ACTIVATE, v2),
            (3, ACTION_SUPERSEDE, v1),
        ),
        version_ids=(v1, v2),
    )
    assert result.versions[v1].state == VERSION_STATE_SUPERSEDED
    assert result.versions[v1].activation_no == 1


def test_projection_rejects_unknown_action() -> None:
    """未知动作**显式报错**，不静默忽略。

    静默忽略会让一条拼错的事件消失在投影里 ——
    于是"事件写了但状态没变"永远无法被发现。
    """
    v1 = uuid.uuid4()
    with pytest.raises(ValueError, match="未知的发布动作"):
        kn_projection.project_versions(
            _events((1, "NOT_A_REAL_ACTION", v1)), version_ids=(v1,)
        )


def test_projection_indexing_state() -> None:
    """`index_ready=False` 且已生效 → `INDEXING`（规格源 §277 的"索引处理中"）。"""
    v1 = uuid.uuid4()
    result = kn_projection.project_versions(
        _events((1, ACTION_ACTIVATE, v1)),
        version_ids=(v1,),
        index_ready=False,
    )
    assert result.state == ASSET_STATE_INDEXING
    # 版本状态仍是 ACTIVE —— "谁是当前生效版本"有确定答案（精确读取可用）。
    assert result.versions[v1].state == VERSION_STATE_ACTIVE


def test_projection_archived_beats_indexing() -> None:
    """**状态优先级：归档 > 索引中 > 生效 > 草稿**。

    一条已归档的知识无论索引状态如何，对用户而言都是"不在了"；
    显示成"索引处理中"会让人以为它即将可用。
    """
    v1 = uuid.uuid4()
    result = kn_projection.project_versions(
        _events((1, ACTION_ACTIVATE, v1), (2, ACTION_ARCHIVE, v1)),
        version_ids=(v1,),
        index_ready=False,
    )
    assert result.state == ASSET_STATE_ARCHIVED


def test_projection_never_published_version_stays_draft() -> None:
    """从未被任何事件提到的版本是 `DRAFT`。

    这一点**无法从事件流自身推出来**（事件流里没有它）——
    因此 `version_ids` 必须由调用方传进来。
    """
    v1, v2 = uuid.uuid4(), uuid.uuid4()
    result = kn_projection.project_versions(
        _events((1, ACTION_ACTIVATE, v1)),
        version_ids=(v1, v2),
    )
    assert result.versions[v2].state == VERSION_STATE_DRAFT


def test_allowed_actions_reflects_archive_state() -> None:
    """`allowed_actions` 只给出**会改变东西**的动作。

    | 动作 | 何时无意义 |
    |---|---|
    | `ARCHIVE` | 已经归档 |
    | `RESTORE` | 没有归档 |
    """
    v1 = uuid.uuid4()
    live = kn_projection.project_versions(
        _events((1, ACTION_ACTIVATE, v1)), version_ids=(v1,)
    )
    assert ACTION_ARCHIVE in kn_projection.allowed_actions(live)
    assert ACTION_RESTORE not in kn_projection.allowed_actions(live)

    archived = kn_projection.project_versions(
        _events((1, ACTION_ACTIVATE, v1), (2, ACTION_ARCHIVE, v1)),
        version_ids=(v1,),
    )
    assert ACTION_ARCHIVE not in kn_projection.allowed_actions(archived)
    assert ACTION_RESTORE in kn_projection.allowed_actions(archived)


def test_client_publication_actions_exclude_supersede() -> None:
    """**`SUPERSEDE` 不是客户端可选动作**（协议 §9 只列三个）。

    让它可选就会出现"把当前生效版本标成被取代、但没有任何版本接过它"
    的空窗 —— 知识库看起来有内容，实际无生效版本。
    """
    assert CLIENT_PUBLICATION_ACTIONS == {
        ACTION_ACTIVATE,
        ACTION_ARCHIVE,
        ACTION_RESTORE,
    }
    assert ACTION_SUPERSEDE in PUBLICATION_ACTIONS
    assert ACTION_SUPERSEDE not in CLIENT_PUBLICATION_ACTIONS


def test_supersede_targets_are_only_superseded_versions() -> None:
    """`supersede_targets` 只列出已被取代的版本。"""
    v1, v2 = uuid.uuid4(), uuid.uuid4()
    result = kn_projection.project_versions(
        _events((1, ACTION_ACTIVATE, v1), (2, ACTION_ACTIVATE, v2)),
        version_ids=(v1, v2),
    )
    assert kn_projection.supersede_targets(result) == (v1,)


def test_projection_is_pure_function() -> None:
    """投影**不改输入**（纯函数）—— 同一输入重复调用结果相同。"""
    v1 = uuid.uuid4()
    events = _events((1, ACTION_ACTIVATE, v1), (2, ACTION_ARCHIVE, v1))
    first = kn_projection.project_versions(events, version_ids=(v1,))
    second = kn_projection.project_versions(events, version_ids=(v1,))
    assert first.state == second.state
    assert first.archived == second.archived
    assert first.active_version_id == second.active_version_id


# --- 1.4 值类型与取值域 ----------------------------------------------------


def test_version_states_exclude_pending() -> None:
    """版本状态链**不含 `PENDING`**（本包的一处有意取舍，已登记）。

    规格源把 PENDING 放在版本链上，但同一份规格源 §277 又说
    "索引尚未完成时**知识状态**显示'已生效／索引处理中'"（明写是 asset 级）。
    两者放一起会矛盾：一个版本若因索引未完成而处于 PENDING，
    那"谁是当前生效版本"就没有答案了，而 §277 要求精确读取必须能拿到它。
    因此 PENDING 归到 asset 级（`ASSET_STATE_INDEXING`）。
    """
    assert "PENDING" not in VERSION_STATES
    assert set(VERSION_STATES) == {
        "DRAFT",
        "ACTIVE",
        "SUPERSEDED",
        "ARCHIVED",
    }
    assert ASSET_STATE_INDEXING not in VERSION_STATES


def test_knowledge_schema_version_is_positive() -> None:
    """`structured_payload` 的形状版本必须是正数（消费方据此选解析方式）。"""
    assert kn_models.KNOWLEDGE_PAYLOAD_SCHEMA_VERSION >= 1


def test_only_two_tables_are_immutable() -> None:
    """**不可变表恰好两张**：`knowledge_versions` / `knowledge_publications`。

    `knowledge_chunks` **刻意不在其中** —— 它完全由 (版本内容, 切块器版本)
    决定，是**可重建的物化结果**。对可重建的派生数据加"改了就是篡改历史"
    的语义，会让 WP21 无法实现"模型换版不混向量"（换模型时重切、
    删旧 chunk 是正常运维）。
    """
    assert set(kn_models.IMMUTABLE_TABLES) == {
        "knowledge_versions",
        "knowledge_publications",
    }
    assert "knowledge_chunks" not in kn_models.IMMUTABLE_TABLES


def test_publication_events_value_type_is_frozen() -> None:
    """跨层传值用 frozen dataclass（ORM 实例绑 session，关了就 Detached）。"""
    event = PublicationEvent(sequence=1, version_id=uuid.uuid4(), action=ACTION_ACTIVATE)
    with pytest.raises(Exception):
        event.sequence = 2  # type: ignore[misc]


def test_client_publication_actions_protects_against_empty_live_state() -> None:
    """上一条判据的**后果验证**：客户端确实无法造出"无生效版本"的空窗。

    若能直接发 SUPERSEDE，`allowed_actions` 会放行它，
    而投影会给出 `active_version_id` 仍指向那个版本、
    版本状态却是 SUPERSEDED 的矛盾组合。
    """
    v1 = uuid.uuid4()
    projection = kn_projection.project_versions(
        _events((1, ACTION_ACTIVATE, v1), (2, ACTION_SUPERSEDE, v1)),
        version_ids=(v1,),
    )
    # 投影本身是自洽的（active 不动），但若客户端能做这件事，
    # 用户就会看到"当前生效版本已被取代"这种自相矛盾的展示。
    assert isinstance(projection, AssetProjection)
    assert projection.active_version_id == v1
    assert projection.versions[v1].state == VERSION_STATE_SUPERSEDED
    assert ACTION_SUPERSEDE not in CLIENT_PUBLICATION_ACTIONS

# ===========================================================================
# 第 2 层：服务层 + 真库
# ===========================================================================


# --- 2.1 创建（草稿身份与版本）---------------------------------------------


def test_create_asset_yields_draft_with_version(env: KnowledgeEnv) -> None:
    """`create_asset` 建出 asset + 第一版，**但不发布**（→ `DRAFT`）。

    协议 §9 原文：`POST /knowledge/assets` → 201 **草稿身份与版本**。
    "建了但还没确认"与"已生效"必须是两个显式状态，
    而不是靠"有没有调用过 publications"推断。
    """
    asset_id, version_id = _create_fact(env)
    view = _view(env, asset_id)
    assert view.state == ASSET_STATE_DRAFT
    assert view.active_version is None
    assert len(view.versions) == 1
    assert view.versions[0][0].id == version_id
    assert view.history == ()


def test_create_asset_registers_version_node(env: KnowledgeEnv) -> None:
    """版本行与注册表节点**同事务建出**（数据结构 02 §5 的要求）。

    若只写领域行不注册节点，`version_dependencies` 的溯源就断了 ——
    而那是"这条知识被谁引用"的唯一依据。
    """
    asset_id, version_id = _create_fact(env)
    with session_scope(env.settings) as session:
        node = session.get(VersionNode, version_id)
        assert node is not None
        assert node.entity_type == ENTITY_TYPE_KNOWLEDGE
        assert node.entity_id == asset_id
        assert node.revision_no == 1


def test_create_asset_rejects_empty_title(env: KnowledgeEnv) -> None:
    """标题不能为空（`models.py` 有 CHECK，服务层先给出可读 422）。"""
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        with pytest.raises(ApiError) as caught:
            kn_service.create_asset(
                repo,
                category=CATEGORY_FACT,
                title="   ",
                scope=SCOPE_PRODUCT,
                brand_id=None,
                product_id=env.product_id,
                project_id=None,
                body="x",
                structured_payload=_fact_payload(),
                actor_id=env.owner_id,
            )
    assert caught.value.code.value == "VALIDATION_FAILED"
    assert caught.value.message


def test_create_asset_rejects_unknown_category(env: KnowledgeEnv) -> None:
    """未知类别显式报错（静默接受会让一条无 Schema 的知识进库）。"""
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        with pytest.raises(ApiError) as caught:
            kn_service.create_asset(
                repo,
                category="NOT_A_CATEGORY",
                title="标题",
                scope=SCOPE_PRODUCT,
                brand_id=None,
                product_id=env.product_id,
                project_id=None,
                body="x",
                structured_payload=_fact_payload(),
                actor_id=env.owner_id,
            )
    assert caught.value.code.value == "VALIDATION_FAILED"


def test_create_asset_requires_missing_fields_report(env: KnowledgeEnv) -> None:
    """缺必填结构字段 → 422 且带 `missingFields` —— **"禁止无结构散文"的落点**。

    只给散文（`body`）而不给结构，`missingFields` 会把缺的字段列出来，
    客户端据此定位；而"字段不适用"可以显式标 `NOT_APPLICABLE` 并给原因。
    """
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        with pytest.raises(ApiError) as caught:
            kn_service.create_asset(
                repo,
                category=CATEGORY_FACT,
                title="只有散文的事实",
                scope=SCOPE_PRODUCT,
                brand_id=None,
                product_id=env.product_id,
                project_id=None,
                body="这个产品挺能打。",
                structured_payload=None,
                actor_id=env.owner_id,
            )
    errors = caught.value.details.get("fieldErrors") or []
    assert any(item.get("type") == "required_fields_missing" for item in errors)
    assert caught.value.details.get("missingFields") == ["fact.statement"]


def test_create_asset_rejects_nonexistent_dimension(env: KnowledgeEnv) -> None:
    """范围限定列指向的对象必须存在 —— 给出**哪个字段**不存在的可读 422。

    外键也会拦住，但它抛 `IntegrityError`（难读的 422 或 500）。
    两层都要：服务层给人看，外键管住绕过本函数的所有写入。
    """
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        with pytest.raises(ApiError) as caught:
            kn_service.create_asset(
                repo,
                category=CATEGORY_FACT,
                title="指向不存在的产品",
                scope=SCOPE_PRODUCT,
                brand_id=None,
                product_id=uuid.uuid4(),
                project_id=None,
                body="x",
                structured_payload=_fact_payload(),
                actor_id=env.owner_id,
            )
    errors = caught.value.details.get("fieldErrors") or []
    assert any(item.get("field") == "productId" for item in errors)


def test_create_asset_rejects_fact_global_scope(env: KnowledgeEnv) -> None:
    """服务层也拦 `FACT` + `GLOBAL`（数据库 CHECK 是同一判据的第二层）。"""
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        with pytest.raises(ApiError) as caught:
            kn_service.create_asset(
                repo,
                category=CATEGORY_FACT,
                title="全组织事实",
                scope=SCOPE_GLOBAL,
                brand_id=None,
                product_id=None,
                project_id=None,
                body="x",
                structured_payload=_fact_payload(),
                actor_id=env.owner_id,
            )
    assert "范围必须明确" in (caught.value.message or "")


def test_create_asset_allows_global_style(env: KnowledgeEnv) -> None:
    """**对照组**：`STYLE` + `GLOBAL` 必须**通过**（只有 `FACT` 受限）。

    没有这条对照，上一组"被拒"的测试在一个"拒绝一切 GLOBAL"的实现上也会全绿。
    """
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        created = kn_service.create_asset(
            repo,
            category=CATEGORY_STYLE,
            title="首图风格约定",
            scope=SCOPE_GLOBAL,
            brand_id=None,
            product_id=None,
            project_id=None,
            body="小红书图文首图要有人。",
            structured_payload=_payload_for(CATEGORY_STYLE, "首图要有人"),
            actor_id=env.owner_id,
        )
        assert created.asset.scope == SCOPE_GLOBAL
    assert _view(env, created.asset.id).state == ASSET_STATE_DRAFT


def test_every_category_can_be_created(env: KnowledgeEnv) -> None:
    """六类都能被建出来（用一个最小合法 payload）。

    **`FACT` 用 `PRODUCT` 范围**：它是唯一不允许 `GLOBAL` 的类别
    （"事实范围必须明确"）—— 用 `GLOBAL` 会撞上那条判据，
    于是这个"六类都能建"的用例会变成"五类能建 + 一类被拒"。
    这里按类别的实际约束选范围，测的才是"Schema 齐全"而非"范围规则"。
    """
    for category in CATEGORIES:
        scoped = category in CATEGORIES_REQUIRING_CONCRETE_SCOPE
        with session_scope(env.settings) as session:
            repo = KnowledgeRepository(session)
            created = kn_service.create_asset(
                repo,
                category=category,
                title=f"{category} 测试",
                scope=SCOPE_PRODUCT if scoped else SCOPE_GLOBAL,
                brand_id=None,
                product_id=env.product_id if scoped else None,
                project_id=None,
                body=f"{category} 的正文",
                structured_payload=_payload_for(category),
                actor_id=env.owner_id,
            )
        assert created.asset.category == category


# --- 2.2 加版本（不改原文）-------------------------------------------------


def test_add_version_creates_second_revision(env: KnowledgeEnv) -> None:
    """加版本 = 追加一行 + 注册第二个节点（revision_no 递增）。"""
    asset_id, v1_id = _create_fact(env)
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        asset = repo.get_asset(asset_id)
        assert asset is not None
        v2, revision_no, etag = kn_service.add_version(
            repo,
            asset=asset,
            body="续航 18 小时（补充测试口径）。",
            structured_payload=_fact_payload("续航 18 小时（补充测试口径）"),
            change_reason="补充测量口径",
            actor_id=env.owner_id,
        )
        v2_id, rev2, etag2 = v2.id, revision_no, etag
    assert rev2 == 2
    assert etag2
    view = _view(env, asset_id)
    assert len(view.versions) == 2
    # **旧版本仍在**（"不改原文"是结构性的：表级不可变 + 只插新行）
    assert {item.id for item, _state in view.versions} == {v1_id, v2_id}


def test_add_version_requires_change_reason(env: KnowledgeEnv) -> None:
    """`changeReason` 必填 —— 版本不可变，事后无法补写理由。

    它是**唯一**能解释"为什么又发了一版"的地方。
    """
    asset_id, _ = _create_fact(env)
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        asset = repo.get_asset(asset_id)
        assert asset is not None
        with pytest.raises(ApiError) as caught:
            kn_service.add_version(
                repo,
                asset=asset,
                body="新版正文",
                structured_payload=_fact_payload(),
                change_reason="   ",
                actor_id=env.owner_id,
            )
    assert caught.value.code.value == "VALIDATION_FAILED"


def test_add_version_rejects_archived_asset(env: KnowledgeEnv) -> None:
    """归档的知识**不能加版本**。

    那会让"已归档"与"有新内容"同时成立，而列表默认不显示归档知识 ——
    新版本等于写进了一个看不见的地方。
    """
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    _publish(env, asset_id=asset_id, action=ACTION_ARCHIVE)
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        asset = repo.get_asset(asset_id)
        assert asset is not None
        with pytest.raises(ApiError) as caught:
            kn_service.add_version(
                repo,
                asset=asset,
                body="归档后的新版",
                structured_payload=_fact_payload(),
                change_reason="试图写进看不见的地方",
                actor_id=env.owner_id,
            )
    assert caught.value.code.value == "INVALID_TRANSITION"


def test_add_version_validates_payload_against_asset_category(env: KnowledgeEnv) -> None:
    """加版本时按 **asset 的类别** 校验（`add_version` 不接收 category）。

    这防止"给一条 FACT 追加一个 STYLE payload"这类跨类写入 ——
    若按调用方传的类别校验，脏数据就能进来。
    """
    asset_id, _ = _create_fact(env)
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        asset = repo.get_asset(asset_id)
        assert asset is not None
        with pytest.raises(ApiError):
            kn_service.add_version(
                repo,
                asset=asset,
                body="风格内容",
                structured_payload=_payload_for(CATEGORY_STYLE, "首图要有人"),
                change_reason="跨类写入",
                actor_id=env.owner_id,
            )


# --- 2.3 发布事务 ----------------------------------------------------------


def test_publish_activate_sets_active_version(env: KnowledgeEnv) -> None:
    """`ACTIVATE` 切换 `active_version_id` 并写一条事件。"""
    asset_id, v1_id = _create_fact(env)
    result = _publish(
        env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id,
        reason="首次生效",
    )
    assert result.sequence == 1
    assert result.active_version_id == v1_id
    # **写入侧与读取侧状态必须一致**（冒烟阶段实测过它们曾不同：
    # 写入侧写死 index_ready=True 得到 ACTIVE，读回按 chunk 判定得到 INDEXING）。
    view = _view(env, asset_id)
    assert result.asset_state == view.state


def test_publish_state_is_indexing_without_chunks(env: KnowledgeEnv) -> None:
    """刚发布的知识显示 `INDEXING`（规格源 §277 的"已生效／索引处理中"）。

    本包不生产 chunk（那是 WP21），因此语义检索此刻召回不到它 ——
    而精确读取已经可用。这是**正确**的显示，不是错误状态。
    """
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    view = _view(env, asset_id)
    assert view.state == ASSET_STATE_INDEXING
    assert view.active_chunk_count == 0
    assert view.active_version is not None and view.active_version.id == v1_id


def test_publish_becomes_active_once_chunk_exists(env: KnowledgeEnv) -> None:
    """**对照组**：插入一条 chunk 后状态变成 `ACTIVE`（不再"索引处理中"）。

    没有这条对照，"显示 INDEXING"这个断言在一个"永远 INDEXING"的实现上
    也会通过 —— 那等于把一个中间态固化成了终态。
    """
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    _insert_chunk(env, version_id=v1_id, chunk_no=1, text_content="续航 18 小时")
    view = _view(env, asset_id)
    assert view.state == ASSET_STATE_ACTIVE
    assert view.active_chunk_count == 1


def test_publish_second_version_supersedes_first(env: KnowledgeEnv) -> None:
    """激活新版本 → 旧版本自动 `SUPERSEDED`（"生效版本与旧引用并存"）。"""
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        asset = repo.get_asset(asset_id)
        assert asset is not None
        v2, _rev, _etag = kn_service.add_version(
            repo,
            asset=asset,
            body="新版正文",
            structured_payload=_fact_payload("续航 19 小时"),
            change_reason="补测后修正",
            actor_id=env.owner_id,
        )
        v2_id = v2.id
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v2_id)

    view = _view(env, asset_id)
    states = {item.id: state for item, state in view.versions}
    assert states[v1_id] == VERSION_STATE_SUPERSEDED
    assert states[v2_id] == VERSION_STATE_ACTIVE
    # **旧版本仍可按 id 直读** —— 这是"旧引用并存"的读取路径。
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        old = repo.get_version(v1_id)
        assert old is not None and old.body


def test_publish_archive_keeps_history_and_active_version(env: KnowledgeEnv) -> None:
    """归档：置 `archived_at`、**不动 active_version**、事件流完整保留。

    "归档可追溯"要求"曾经归档过"留在事件流里，而不是靠当前状态推断。
    """
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    _publish(env, asset_id=asset_id, action=ACTION_ARCHIVE, reason="下线")
    view = _view(env, asset_id)
    assert view.state == ASSET_STATE_ARCHIVED
    assert view.asset.archived_at is not None
    assert view.active_version is not None and view.active_version.id == v1_id
    assert [item["action"] for item in view.history] == [
        ACTION_ACTIVATE,
        ACTION_ARCHIVE,
    ]


def test_publish_restore_clears_archive_via_new_event(env: KnowledgeEnv) -> None:
    """恢复：**通过新事件**，不改历史。事件流里保留"归档过又恢复"整条轨迹。"""
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    _publish(env, asset_id=asset_id, action=ACTION_ARCHIVE)
    _publish(env, asset_id=asset_id, action=ACTION_RESTORE, reason="重新启用")

    view = _view(env, asset_id)
    assert view.state != ASSET_STATE_ARCHIVED
    assert view.asset.archived_at is None
    assert [item["action"] for item in view.history] == [
        ACTION_ACTIVATE,
        ACTION_ARCHIVE,
        ACTION_RESTORE,
    ]


def test_publish_duplicate_archive_is_conflict(env: KnowledgeEnv) -> None:
    """重复归档被拒（`allowed_actions` 判据）—— 否则事件表被同义事件撑满。"""
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    _publish(env, asset_id=asset_id, action=ACTION_ARCHIVE)
    with pytest.raises(ApiError) as caught:
        _publish(env, asset_id=asset_id, action=ACTION_ARCHIVE)
    assert caught.value.code.value == "INVALID_TRANSITION"


def test_publish_duplicate_restore_is_conflict(env: KnowledgeEnv) -> None:
    """重复恢复被拒（没有归档时 RESTORE 不改变任何东西）。"""
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    with pytest.raises(ApiError) as caught:
        _publish(env, asset_id=asset_id, action=ACTION_RESTORE)
    assert caught.value.code.value == "INVALID_TRANSITION"


def test_publish_rejects_supersede_from_client(env: KnowledgeEnv) -> None:
    """`SUPERSEDE` 不在客户端可选动作里（服务层直接拒）。"""
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    with pytest.raises(ApiError) as caught:
        _publish(env, asset_id=asset_id, action=ACTION_SUPERSEDE, version_id=v1_id)
    assert caught.value.code.value == "VALIDATION_FAILED"


def test_publish_activate_requires_version_id(env: KnowledgeEnv) -> None:
    """`ACTIVATE` 必须指定版本（否则"让谁生效"没有答案）。"""
    asset_id, _ = _create_fact(env)
    with pytest.raises(ApiError) as caught:
        _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=None)
    assert caught.value.code.value == "VALIDATION_FAILED"


def test_publish_rejects_version_of_another_asset(env: KnowledgeEnv) -> None:
    """**目标版本必须属于本 asset**（跨知识激活会让 A 返回 B 的内容）。"""
    asset_a, _ = _create_fact(env, statement="A 的事实")
    _asset_b, v_b = _create_fact(env, statement="B 的事实")
    with pytest.raises(ApiError) as caught:
        _publish(env, asset_id=asset_a, action=ACTION_ACTIVATE, version_id=v_b)
    assert caught.value.code.value == "VALIDATION_FAILED"
    errors = caught.value.details.get("fieldErrors") or []
    assert any(item.get("type") == "version_not_in_asset" for item in errors)


def test_publish_fact_lookup_uses_active_version(env: KnowledgeEnv) -> None:
    """**精确读取路径可用且不经过索引**（规格源 §277 末句）。

    "重要事实可以从数据库精确获取；不能因索引延迟读取错误旧事实" ——
    这里在两个版本都还没 chunk（索引未就绪）时，
    按 `(product_id, category)` 取到的仍是**当前生效版本**。
    """
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        asset = repo.get_asset(asset_id)
        assert asset is not None
        v2, _r, _e = kn_service.add_version(
            repo,
            asset=asset,
            body="修正后的事实",
            structured_payload=_fact_payload("续航 19 小时"),
            change_reason="补测修正",
            actor_id=env.owner_id,
        )
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v2.id)

    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        hits = repo.list_live_assets_for_fact_lookup(
            product_id=env.product_id, category=CATEGORY_FACT
        )
        matched = [item for item in hits if item.id == asset_id]
        assert len(matched) == 1
        # 精确读取拿到的一定是**当前生效版本**，且它此刻没有 chunk。
        assert matched[0].active_version_id == v2.id
        assert repo.count_chunks(v2.id) == 0


def test_publish_fact_lookup_excludes_archived(env: KnowledgeEnv) -> None:
    """精确读取路径**排除已归档**的知识（与部分索引的 WHERE 一致）。"""
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    _publish(env, asset_id=asset_id, action=ACTION_ARCHIVE)
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        hits = repo.list_live_assets_for_fact_lookup(
            product_id=env.product_id, category=CATEGORY_FACT
        )
        assert all(item.id != asset_id for item in hits)


def test_publish_writes_outbox_event(env: KnowledgeEnv) -> None:
    """发布**在同一事务里发出索引事件**（规格源 §277 末句"写索引 Outbox"）。

    事件名与聚合类型定义在 `types.py`（WP21 的投递器要按名字订阅）——
    两处各写一遍字符串、拼错时**不会报错**，只会静默不索引。
    """
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    with session_scope(env.settings) as session:
        rows = session.execute(
            text(
                "select event_type, aggregate_type, aggregate_id from outbox_events"
                " where aggregate_id = :a order by id"
            ),
            {"a": asset_id},
        ).all()
    types = [row.event_type for row in rows]
    assert "knowledge.index_requested" in types
    assert all(row.aggregate_type == "knowledge_asset" for row in rows)


def test_publish_archive_writes_removal_event(env: KnowledgeEnv) -> None:
    """`ARCHIVE` 发的是"从索引移除"事件（而不是继续请求索引）。"""
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    _publish(env, asset_id=asset_id, action=ACTION_ARCHIVE)
    with session_scope(env.settings) as session:
        rows = session.execute(
            text(
                "select event_type from outbox_events"
                " where aggregate_id = :a order by id"
            ),
            {"a": asset_id},
        ).all()
    assert "knowledge.index_removed" in [row.event_type for row in rows]


def test_publish_sequence_increments(env: KnowledgeEnv) -> None:
    """事件序号从 1 递增 —— 它是投影的**排序键**（不能靠时间戳）。

    `now()` 在 PG 里是**事务开始时刻**，同一事务内插入的多条事件时间戳
    完全相同；按时间排序会得到不确定的顺序，而投影对顺序敏感。
    """
    asset_id, v1_id = _create_fact(env)
    first = _publish(
        env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id
    )
    second = _publish(env, asset_id=asset_id, action=ACTION_ARCHIVE)
    third = _publish(env, asset_id=asset_id, action=ACTION_RESTORE)
    assert (first.sequence, second.sequence, third.sequence) == (1, 2, 3)


# --- 2.4 列表（全员检索）---------------------------------------------------


def test_list_assets_filters_by_category_and_product(env: KnowledgeEnv) -> None:
    """列表筛选：类别 + 产品（其余筛选同理，这里只验两个维度）。"""
    fact_id, _ = _create_fact(env, statement="产品事实")
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        styled = kn_service.create_asset(
            repo,
            category=CATEGORY_STYLE,
            title="全局风格",
            scope=SCOPE_GLOBAL,
            brand_id=None,
            product_id=None,
            project_id=None,
            body="首图要有人。",
            structured_payload=_payload_for(CATEGORY_STYLE),
            actor_id=env.owner_id,
        )
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        facts = kn_service.list_assets(
            repo,
            category=CATEGORY_FACT,
            brand_id=None,
            product_id=env.product_id,
            project_id=None,
            title_query=None,
            state=None,
            include_archived=True,
        )
    ids = {asset.id for asset, _state in facts}
    assert fact_id in ids
    assert styled.asset.id not in ids


def test_list_assets_excludes_archived_by_default(env: KnowledgeEnv) -> None:
    """默认列表**排除归档**；`include_archived=True` 才放出来。

    归档不是删除：它仍可按 id 直读（"旧引用并存"），
    只是不该把默认列表刷成一堆历史。
    """
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    _publish(env, asset_id=asset_id, action=ACTION_ARCHIVE)

    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        default = kn_service.list_assets(
            repo, category=CATEGORY_FACT, brand_id=None, product_id=env.product_id,
            project_id=None, title_query=None, state=None, include_archived=False,
        )
        widened = kn_service.list_assets(
            repo, category=CATEGORY_FACT, brand_id=None, product_id=env.product_id,
            project_id=None, title_query=None, state=None, include_archived=True,
        )
    assert asset_id not in {asset.id for asset, _s in default}
    assert asset_id in {asset.id for asset, _s in widened}


def test_list_assets_filters_by_state(env: KnowledgeEnv) -> None:
    """`state` 筛选（投影算出来的，在 Python 侧过滤）。"""
    draft_id, _ = _create_fact(env, statement="草稿事实")
    live_id, live_v = _create_fact(env, statement="已发布事实")
    _publish(env, asset_id=live_id, action=ACTION_ACTIVATE, version_id=live_v)

    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        drafts = kn_service.list_assets(
            repo, category=CATEGORY_FACT, brand_id=None, product_id=env.product_id,
            project_id=None, title_query=None, state=ASSET_STATE_DRAFT,
            include_archived=True,
        )
    ids = {asset.id for asset, _s in drafts}
    assert draft_id in ids
    assert live_id not in ids


def test_list_assets_rejects_unknown_state(env: KnowledgeEnv) -> None:
    """`state` 取值非法 → 报错。

    静默返回空列表会让客户端以为"没有这样的知识"，而实际是筛选值拼错了。
    """
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        with pytest.raises(ApiError) as caught:
            kn_service.list_assets(
                repo, category=None, brand_id=None, product_id=None,
                project_id=None, title_query=None, state="NOT_A_STATE",
                include_archived=False,
            )
    assert caught.value.code.value == "VALIDATION_FAILED"


def test_list_assets_title_query_escapes_like_wildcards(env: KnowledgeEnv) -> None:
    """标题搜索**转义 LIKE 通配符**（不做就是一个注入式的 bug）。

    用户搜 `50%` 时那个 `%` 会被 LIKE 当成"任意字符"，搜出所有以 `50`
    开头的东西 —— **不会报错**，只给出看起来合理但错误的结果。
    """
    plain_id, _ = _create_fact(env, statement="普通", title="续航 18 小时")
    pct_title = f"折扣 50% 专场 {uuid.uuid4().hex[:6]}"
    pct_id, _ = _create_fact(env, statement="折扣", title=pct_title)
    decoy_id, _ = _create_fact(env, statement="诱饵", title=f"折扣 50 专场 {uuid.uuid4().hex[:6]}")

    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        hits = kn_service.list_assets(
            repo, category=None, brand_id=None, product_id=env.product_id,
            project_id=None, title_query="50%", state=None, include_archived=True,
        )
    ids = {asset.id for asset, _s in hits}
    assert pct_id in ids
    # **关键断言**：`50` 开头但不含 `50%` 的诱饵**不能**被搜出来。
    assert decoy_id not in ids
    assert plain_id not in ids


# --- 2.5 版本视图与引用 ----------------------------------------------------


def test_list_version_views_returns_projection_state(env: KnowledgeEnv) -> None:
    """历史版本列表带**投影状态**（每个版本是第几个生效的都能看出来）。"""
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        asset = repo.get_asset(asset_id)
        assert asset is not None
        v2, _r, _e = kn_service.add_version(
            repo, asset=asset, body="新版", structured_payload=_fact_payload("新"),
            change_reason="第二版", actor_id=env.owner_id,
        )
        v2_id = v2.id
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v2_id)

    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        asset = repo.get_asset(asset_id)
        assert asset is not None
        rows = kn_service.list_version_views(repo, asset=asset)
    states = {item.id: state for item, state in rows}
    assert states == {v1_id: "SUPERSEDED", v2_id: "ACTIVE"}


def test_list_references_filters_by_visible_project(env: KnowledgeEnv) -> None:
    """**引用摘要受项目可见性过滤**（"不能借此读无权项目正文"）。"""
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    parent_id = _link_reference(env, target_version_id=v1_id, project_id=env.project_id)

    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        # 能看到本项目的运营
        visible = kn_service.list_references(
            repo, version_id=v1_id,
            visible_project_ids=frozenset({env.project_id}),
            is_admin=False, limit=10,
        )
        # 看不到本项目的运营
        hidden = kn_service.list_references(
            repo, version_id=v1_id,
            visible_project_ids=frozenset(),
            is_admin=False, limit=10,
        )
    assert [item.parent_version_id for item in visible] == [parent_id]
    assert hidden == ()


def test_list_references_admin_sees_everything(env: KnowledgeEnv) -> None:
    """管理者不过滤项目（`is_admin=True`）。"""
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    _link_reference(env, target_version_id=v1_id, project_id=env.project_id)
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        rows = kn_service.list_references(
            repo, version_id=v1_id, visible_project_ids=None, is_admin=True, limit=10
        )
    assert len(rows) == 1


def test_list_references_summary_has_no_body(env: KnowledgeEnv) -> None:
    """摘要**只有 `(parent, purpose, project, hash)`，没有正文**。

    这是协议 §9 的硬约束："不能借此读无权项目正文"。
    """
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    _link_reference(env, target_version_id=v1_id, project_id=env.project_id)
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        rows = kn_service.list_references(
            repo, version_id=v1_id,
            visible_project_ids=frozenset({env.project_id}),
            is_admin=False, limit=10,
        )
    fields = set(vars(rows[0]))
    assert fields == {
        "parent_version_id",
        "purpose",
        "project_id",
        "selected_fragment_hash",
    }


def test_list_references_respects_limit(env: KnowledgeEnv) -> None:
    """`limit` 生效（服务端内部一次最多取多少的保护）。"""
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    for _ in range(3):
        _link_reference(env, target_version_id=v1_id, project_id=env.project_id)
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        rows = kn_service.list_references(
            repo, version_id=v1_id,
            visible_project_ids=frozenset({env.project_id}),
            is_admin=False, limit=2,
        )
        none_at_all = kn_service.list_references(
            repo, version_id=v1_id,
            visible_project_ids=frozenset({env.project_id}),
            is_admin=False, limit=0,
        )
    assert len(rows) == 2
    assert none_at_all == ()


# --- 2.6 提案决策 ----------------------------------------------------------


def _make_proposal(
    env: KnowledgeEnv,
    *,
    target_asset_id: uuid.UUID | None = None,
    category: str = CATEGORY_FACT,
    scope: str = SCOPE_PRODUCT,
    payload: dict[str, Any] | None = None,
) -> uuid.UUID:
    """造一条 AI 候选（**产生候选不在本包** —— 这里直接插表）。"""
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        row = repo.insert_proposal(
            project_id=env.project_id,
            source_feedback_id=None,
            source_experiment_id=None,
            target_asset_id=target_asset_id,
            proposed_payload=payload
            or {
                "category": category,
                "title": f"候选-{uuid.uuid4().hex[:6]}",
                "body": "候选正文",
                "structuredPayload": _payload_for(category),
                "productId": str(env.product_id),
            },
            scope=scope,
        )
        # **必须 flush 再读 `row.id`**：`id` 是 server_default
        # （`gen_random_uuid()`），ORM 侧此刻还是 `None` ——
        # 直接读会拿到 None，而 `session.get(表, None)` 会给出
        # `SAWarning: fully NULL primary key identity cannot load any object`
        # 并返回 None，表现为"刚插的候选查不到"（本机实测）。
        repo.flush()
        return row.id


def test_decide_proposal_accept_creates_version_and_activates(env: KnowledgeEnv) -> None:
    """**ACCEPT 生成明确版本与生效事件**（协议 §9 的约束原文）。

    三件事缺一不可：建版本 / 写生效事件 / 标记提案。
    少了第 2 件就是"接受了但没生效"（候选答应了却不落地）。
    """
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    proposal_id = _make_proposal(
        env,
        target_asset_id=asset_id,
        payload={
            "body": "候选补充的事实",
            "structuredPayload": _fact_payload("续航 19 小时"),
            "changeReason": "由反馈实验得出",
        },
    )
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        proposal = repo.get_proposal(proposal_id)
        assert proposal is not None
        result = kn_service.decide_proposal(
            repo, proposal=proposal, decision=DECISION_ACCEPT, scope=None,
            reason="采纳", actor_id=env.owner_id,
        )
    assert result.state == PROPOSAL_STATE_ACCEPTED
    assert result.created_version_id is not None
    assert result.activated is True
    # 提案落地后：那条知识确实切到了新版本。
    view = _view(env, asset_id)
    assert view.active_version is not None
    assert view.active_version.id == result.created_version_id


def test_decide_proposal_reject_requires_reason(env: KnowledgeEnv) -> None:
    """`REJECT` **必须填理由** —— 否则下一个人只能重走一遍判断。"""
    proposal_id = _make_proposal(env)
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        proposal = repo.get_proposal(proposal_id)
        assert proposal is not None
        with pytest.raises(ApiError) as caught:
            kn_service.decide_proposal(
                repo, proposal=proposal, decision=DECISION_REJECT, scope=None,
                reason="   ", actor_id=env.owner_id,
            )
    assert caught.value.code.value == "VALIDATION_FAILED"


def test_decide_proposal_reject_marks_state(env: KnowledgeEnv) -> None:
    """`REJECT` 标终态且**不建版本**。"""
    proposal_id = _make_proposal(env)
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        proposal = repo.get_proposal(proposal_id)
        assert proposal is not None
        result = kn_service.decide_proposal(
            repo, proposal=proposal, decision=DECISION_REJECT, scope=None,
            reason="证据不足", actor_id=env.owner_id,
        )
    assert result.state == PROPOSAL_STATE_REJECTED
    assert result.created_version_id is None
    assert result.activated is False


def test_decide_proposal_twice_is_conflict(env: KnowledgeEnv) -> None:
    """已处置的候选不能重复决策（否则覆盖第一次的确认人与理由）。"""
    proposal_id = _make_proposal(env)
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        proposal = repo.get_proposal(proposal_id)
        assert proposal is not None
        kn_service.decide_proposal(
            repo, proposal=proposal, decision=DECISION_REJECT, scope=None,
            reason="第一次拒绝", actor_id=env.owner_id,
        )
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        proposal = repo.get_proposal(proposal_id)
        assert proposal is not None
        with pytest.raises(ApiError) as caught:
            kn_service.decide_proposal(
                repo, proposal=proposal, decision=DECISION_ACCEPT, scope=None,
                reason="再接受一次", actor_id=env.owner_id,
            )
    assert caught.value.code.value == "INVALID_TRANSITION"


def test_decide_proposal_unknown_decision(env: KnowledgeEnv) -> None:
    """未知决策显式报错。"""
    proposal_id = _make_proposal(env)
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        proposal = repo.get_proposal(proposal_id)
        assert proposal is not None
        with pytest.raises(ApiError) as caught:
            kn_service.decide_proposal(
                repo, proposal=proposal, decision="MAYBE", scope=None,
                reason="", actor_id=env.owner_id,
            )
    assert caught.value.code.value == "VALIDATION_FAILED"


def test_decide_proposal_without_target_creates_asset_not_published(
    env: KnowledgeEnv,
) -> None:
    """**无目标 asset 的候选 ACCEPT → 建新知识但**不自动发布**。

    "新建一条知识"与"让它生效"是两件事 —— 新建出来的东西应当先被看一遍。
    这个不对称是有意的（已登记为待定项）。
    """
    proposal_id = _make_proposal(env)  # 无 target_asset_id
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        proposal = repo.get_proposal(proposal_id)
        assert proposal is not None
        result = kn_service.decide_proposal(
            repo, proposal=proposal, decision=DECISION_ACCEPT, scope=None,
            reason="采纳并新建", actor_id=env.owner_id,
        )
    assert result.created_version_id is not None
    assert result.activated is False
    # 新知识处于 DRAFT（没有生效事件）。
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        version = repo.get_version(result.created_version_id)
        assert version is not None
        asset = repo.get_asset(version.asset_id)
        assert asset is not None
        view = kn_service.asset_view(repo, asset=asset)
    assert view.state == ASSET_STATE_DRAFT


def test_decide_proposal_scope_override_is_validated(env: KnowledgeEnv) -> None:
    """决策时**覆盖 scope 要重新过范围校验**。

    否则"ACCEPT 并改成 GLOBAL"会让 `models.py` 的 CHECK 在提交时才炸 ——
    错误来自数据库约束，消息里没有"哪个字段错了"。
    """
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    proposal_id = _make_proposal(
        env, target_asset_id=asset_id,
        payload={"body": "候选", "structuredPayload": _fact_payload()},
    )
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        proposal = repo.get_proposal(proposal_id)
        assert proposal is not None
        with pytest.raises(ApiError) as caught:
            kn_service.decide_proposal(
                repo, proposal=proposal, decision=DECISION_ACCEPT,
                scope=SCOPE_GLOBAL,  # FACT 不允许 GLOBAL
                reason="改成全组织", actor_id=env.owner_id,
            )
    assert "范围必须明确" in (caught.value.message or "")


def test_list_proposals_filters(env: KnowledgeEnv) -> None:
    """候选列表按 `state` / `project_id` 筛选。"""
    pending_id = _make_proposal(env)
    rejected_id = _make_proposal(env)
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        proposal = repo.get_proposal(rejected_id)
        assert proposal is not None
        kn_service.decide_proposal(
            repo, proposal=proposal, decision=DECISION_REJECT, scope=None,
            reason="不采纳", actor_id=env.owner_id,
        )
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        pending = kn_service.list_proposals(
            repo, state=PROPOSAL_STATE_PENDING, project_id=env.project_id
        )
        settled = kn_service.list_proposals(
            repo, state=PROPOSAL_STATE_REJECTED, project_id=env.project_id
        )
    assert pending_id in {item.id for item in pending}
    assert rejected_id in {item.id for item in settled}
    assert rejected_id not in {item.id for item in pending}


def test_proposal_state_constants_are_consistent() -> None:
    """决策 → 终态的映射**只有一处定义**（两处各写 `if` 迟早漂移）。"""
    from rednote.modules.knowledge.types import DECISION_RESULT_STATE

    assert DECISION_RESULT_STATE[DECISION_ACCEPT] == PROPOSAL_STATE_ACCEPTED
    assert DECISION_RESULT_STATE[DECISION_REJECT] == PROPOSAL_STATE_REJECTED


# ===========================================================================
# 内部辅助（放在最后，避免打断测试的阅读流）
# ===========================================================================


def _insert_chunk(
    env: KnowledgeEnv,
    *,
    version_id: uuid.UUID,
    chunk_no: int,
    text_content: str,
) -> uuid.UUID:
    """插一条 chunk（**本包只建表不生产** —— 这里模拟 WP21 产出的结果）。

    用途：把 `active_version_id` 的索引就绪判据翻成 `True`，
    从而验证"有 chunk → `ACTIVE`、无 chunk → `INDEXING`"这一对判据。
    """
    import hashlib

    chunk_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "insert into knowledge_chunks (id, knowledge_version_id, chunk_no,"
                " text_content, text_hash, token_count, locator, keywords)"
                " values (:id, :vid, :no, :text, :hash, :tokens,"
                " cast('{}' as jsonb), cast(:keywords as text[]))"
            ),
            {
                "id": chunk_id,
                "vid": version_id,
                "no": chunk_no,
                "text": text_content,
                "hash": hashlib.sha256(text_content.encode("utf-8")).hexdigest(),
                "tokens": max(1, len(text_content)),
                # **空数组必须走 `ARRAY[]::text[]`**：PG 不接受 `'[]'::text[]`
                # 这种文本字面量（报 `malformed array literal: "[]"`）——
                # 它要求 `{...}` 数组字面量形式。参数化后由驱动按数组编码，
                # 比手拼 `'{}'` 更稳（后者对含逗号/引号的元素会出错）。
                "keywords": [],
            },
        )
    return chunk_id


def _link_reference(
    env: KnowledgeEnv, *, target_version_id: uuid.UUID, project_id: uuid.UUID
) -> uuid.UUID:
    """造一条"某个版本引用了这条知识版本"的依赖边，返回引用方版本 id。

    **本包不生产引用**（引用由 WP18 / WP22 / WP23 建立），
    这里只为验证 `list_references` 的可见性过滤 ——
    因此直接插 `version_nodes` + `version_dependencies` 两行。

    **引用方节点必须有配对领域行吗**：`version_dependencies.parent_version_id`
    指向一个 `CONTENT` 节点，而 `CONTENT` 的领域表还不存在 ——
    WP11 的注册表允许"只有节点、没有领域行"（那是别的包的进度问题）。
    本包不在 `version_nodes` 上加通用约束，因此这里可以这样造。
    """
    parent_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "insert into version_nodes (id, entity_type, entity_id, revision_no,"
                " payload_hash, schema_version, project_id)"
                " values (:id, 'CONTENT', :eid, 1, :hash, 1, :pid)"
            ),
            {
                "id": parent_id,
                "eid": uuid.uuid4(),
                "hash": "0" * 64,
                "pid": project_id,
            },
        )
        # **`version_dependencies` 没有 `id` 列** —— 它是三列复合主键
        # `(parent_version_id, dependency_version_id, purpose)`。
        # 复合主键含 `purpose` 是 WP11 的取舍：同一个下游版本可以因**不同用途**
        # 引用同一个上游版本（例如既把某条知识当 FACT 又当 STYLE 参考）。
        session.execute(
            text(
                "insert into version_dependencies (parent_version_id,"
                " dependency_version_id, purpose)"
                " values (:parent, :dep, 'REFERENCE')"
            ),
            {"parent": parent_id, "dep": target_version_id},
        )
    return parent_id


# ===========================================================================
# 第 3 层：数据库约束活性（原生 SQL，绕过 ORM 与 service）
#
# **为什么这一层必须存在**：不可变保护与跨表不变量的设计动机就是
# "绕过 ORM 的写入路径（psql / 数据回填 / 运维脚本）没有代码评审"。
# 若测试也只用 ORM，那么测试与实现共享同一个盲区。
#
# **三条铁律**（本项目已踩过）：
#   1. 断言 `sqlstate`（或约束名），不是只断言"抛了异常"——
#      只 `pytest.raises(DBAPIError)` 区分不了"真的拦住了"与"因为别的原因报错"。
#   2. **活性验证必须成对**：该拦的拦 + 该过的过（对照组）。
#      一个永远 `RAISE` 的触发器也能让"正向拦、反向拦"两条全绿。
#   3. 取判定**不要用 `commit()`** —— 用 `SET CONSTRAINTS ALL IMMEDIATE`，
#      最后无条件 rollback。本项目的不可变表物理删不掉
#      （`DELETE FROM knowledge_versions` 会被 `rednote_reject_mutation()` 拦下），
#      用 commit 取判定等于"把炸弹点着了看它炸不炸"，还会留下删不掉的残留。
# ===========================================================================


# --- 3.0 基础设施 ----------------------------------------------------------


def _raw(env: KnowledgeEnv, sql: str, params: dict[str, Any] | None = None) -> Any:
    """在**独立事务**里执行一条原生 SQL 并提交。返回结果行（若有）。"""
    with session_scope(env.settings) as session:
        result = session.execute(text(sql), params or {})
        return result.fetchall() if result.returns_rows else None


def _raw_expect_error(
    env: KnowledgeEnv, sql: str, params: dict[str, Any] | None = None
) -> DBAPIError:
    """执行一条**应当失败**的原生 SQL，返回被捕获的异常供断言 sqlstate。"""
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            session.execute(text(sql), params or {})
    return caught.value


def _raw_expect_error_in_tx(
    env: KnowledgeEnv, statements: list[tuple[str, dict[str, Any]]]
) -> DBAPIError:
    """在**同一事务**里按顺序跑多条语句，返回第一个被触发的异常。

    与 `_assert_deferred_blocked` 的区别：它不要求"抛异常时已到
    `SET CONSTRAINTS` 那一步"—— 用于"前一条语句建前置行、
    后一条语句触发 CHECK"这类场景。
    """
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            for sql, params in statements:
                session.execute(text(sql), params)
    return caught.value


def _assert_deferred_blocked(
    env: KnowledgeEnv, statements: list[tuple[str, dict[str, Any]]]
) -> str:
    """在一个事务里跑多条语句，**用 `SET CONSTRAINTS ALL IMMEDIATE` 取出判定**。

    返回被触发的 `sqlstate`。

    **为什么不用 `commit()` 取判定**：本包的不可变表**物理删不掉** ——
    `DELETE FROM knowledge_versions` 会被 `rednote_reject_mutation()` 拦下，
    连自己的探针残留都清不掉（WP18 实测，ISS-63）。
    延迟约束的判定时机可以用 `SET CONSTRAINTS ALL IMMEDIATE` **提前**到
    事务内的任意点：事务仍未提交，判定已经拿到，之后无条件 rollback，
    一行不留。
    """
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            for sql, params in statements:
                session.execute(text(sql), params)
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    return caught.value.orig.sqlstate  # type: ignore[attr-defined]


#: 一个合法的 jsonb 测试载荷。
#:
#: **为什么是常量而不是在 SQL 里写字面量**：`text()` 会把 JSON 里的 `:1`
#: 当成绑定参数（`{"a":1}` 直接报 "A value is required for bind parameter '1'"）。
#: 本项目的高频坑第 4 条 —— 一律用参数或**不含冒号**的字面量。
_JSONB_OBJ = '{"probe": true}'


def _insert_version_node_sql() -> str:
    return (
        "insert into version_nodes (id, entity_type, entity_id, revision_no,"
        " payload_hash, schema_version, project_id)"
        " values (:id, :etype, :eid, :rev, :hash, 1, :pid)"
    )


def _node_params(
    *,
    node_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    revision_no: int = 1,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "etype": entity_type,
        "eid": entity_id,
        "rev": revision_no,
        "hash": "0" * 64,
        "pid": project_id,
    }


def _asset_row_params(
    *,
    asset_id: uuid.UUID,
    # **默认用 `STYLE` + `GLOBAL`**：这两个取值的组合不需要任何限定列，
    # 因此"只想插一行 asset"的用例不会被范围 CHECK 挡住
    # （用 `FACT` 就**必须**给 `product_id` —— 那是"事实范围必须明确"）。
    category: str = CATEGORY_STYLE,
    title: str = "裸插入的知识",
    scope: str = SCOPE_GLOBAL,
    product_id: uuid.UUID | None = None,
    brand_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    active_version_id: uuid.UUID | None = None,
    archived_at: str | None = None,
) -> dict[str, Any]:
    return {
        "id": asset_id,
        "category": category,
        "title": title,
        "scope": scope,
        "brand": brand_id,
        "product": product_id,
        "project": project_id,
        "active": active_version_id,
        "archived": archived_at,
    }


_ASSET_INSERT_SQL = (
    "insert into knowledge_assets (id, category, title, scope, brand_id,"
    " product_id, project_id, active_version_id, archived_at)"
    " values (:id, :category, :title, :scope, :brand, :product, :project,"
    " :active, :archived)"
)

_VERSION_INSERT_SQL = (
    "insert into knowledge_versions (id, asset_id, body, structured_payload,"
    " change_reason) values (:id, :asset, :body, cast(:payload as jsonb), :reason)"
)


def _insert_live_asset_raw(
    env: KnowledgeEnv,
    *,
    asset_id: uuid.UUID,
    scope: str = SCOPE_PRODUCT,
) -> uuid.UUID:
    """用原生 SQL 造一条**已生效**的知识（含节点 + 版本行），返回版本 id。

    **节点与子表在同一事务里插**：OP-88 的两个触发器都是
    `DEFERRABLE INITIALLY DEFERRED`，它们**在提交时**才跑。
    节点若单独提交，那一刻还没有配对的 `knowledge_versions` 行 ——
    反向触发器会先以"孤立节点"报错，而"节点与子表矛盾"这类用例
    想验证的判据根本轮不到（WP18 已踩过）。
    """
    version_id = uuid.uuid4()
    product_id = env.product_id if scope == SCOPE_PRODUCT else None
    brand_id = env.brand_id if scope == SCOPE_BRAND else None
    with session_scope(env.settings) as session:
        session.execute(
            text(_ASSET_INSERT_SQL),
            _asset_row_params(
                asset_id=asset_id,
                scope=scope,
                product_id=product_id,
                brand_id=brand_id,
            ),
        )
        session.execute(
            text(_insert_version_node_sql()),
            _node_params(
                node_id=version_id,
                entity_type=ENTITY_TYPE_KNOWLEDGE,
                entity_id=asset_id,
            ),
        )
        session.execute(
            text(_VERSION_INSERT_SQL),
            {
                "id": version_id,
                "asset": asset_id,
                "body": "裸插入的正文",
                "payload": '{"fact.statement": {"status": "FILLED", "value": "x"}}',
                "reason": "裸插入",
            },
        )
    return version_id


def _assert_version_check_blocked(
    env: KnowledgeEnv,
    *,
    asset_id: uuid.UUID,
    body: str = "正文",
    payload: str = _JSONB_OBJ,
) -> DBAPIError:
    """在**同一事务**里先建节点、再插版本，返回被触发的异常。

    用途：验证 `knowledge_versions` 上的 CHECK 约束。
    **节点必须一起建**：`knowledge_versions.id` FK 到 `version_nodes.id`，
    只插版本行的话先撞 23503（外键）—— 目标的 CHECK 根本轮不到，
    而"抛了异常"这个断言会让测试假绿（本项目已踩过的坑）。

    节点必须用 **`revision_no=2`**：调用方都用 `_insert_live_asset_raw`
    给同一个 asset 造过一条 revision 1 的节点，再用 1 号会撞
    `uq_version_nodes_entity_type_entity_id_revision_no`（本机实测）——
    那样测的是唯一约束，而不是目标 CHECK。
    """
    version_id = uuid.uuid4()
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            session.execute(
                text(_insert_version_node_sql()),
                _node_params(
                    node_id=version_id,
                    entity_type=ENTITY_TYPE_KNOWLEDGE,
                    entity_id=asset_id,
                    revision_no=2,
                ),
            )
            session.execute(
                text(_VERSION_INSERT_SQL),
                {
                    "id": version_id,
                    "asset": asset_id,
                    "body": body,
                    "payload": payload,
                    "reason": "理由",
                },
            )
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    return caught.value


# --- 3.1 不可变保护（2 张表，成对验证）--------------------------------------


@pytest.mark.parametrize("table", ["knowledge_versions", "knowledge_publications"])
def test_immutable_tables_reject_update(env: KnowledgeEnv, table: str) -> None:
    """**不可变表拒绝 UPDATE** —— `sqlstate` 必须是 `23001`。

    `23001` 是 `platform.immutability` 定义的 `IMMUTABLE_RECORD_SQLSTATE`。
    断言它而不是"抛了异常"：不然一个**别的原因**（例如列不存在）
    也会让测试通过，而不可变保护实际是失效的。
    """
    asset_id = uuid.uuid4()
    version_id = _insert_live_asset_raw(env, asset_id=asset_id)
    if table == "knowledge_publications":
        _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=version_id)
        # **目标必须是 publication 行自己的 id**，不是版本 id ——
        # 用版本 id 去更新 publication 会匹配 0 行、静默通过
        # （`UPDATE ... WHERE id = <不存在的 id>` 不报错，只影响 0 行）。
        target, column = _publication_id_of(env, asset_id), "action"
    else:
        target, column = version_id, "body"

    exc = _raw_expect_error(
        env, f"update {table} set {column} = 'x' where id = :i", {"i": target}
    )
    assert exc.orig.sqlstate == IMMUTABLE_RECORD_SQLSTATE  # type: ignore[attr-defined]


@pytest.mark.parametrize("table", ["knowledge_versions", "knowledge_publications"])
def test_immutable_tables_reject_delete(env: KnowledgeEnv, table: str) -> None:
    """**不可变表拒绝 DELETE**（"改了就是篡改历史"）。

    `DELETE` 与 `UPDATE` 是同一件事的两个方向：删掉一版等于让
    "这条知识曾经是什么"从历史里消失 —— 而 `version_dependencies`
    的边仍指向它，溯源会断。
    """
    asset_id = uuid.uuid4()
    version_id = _insert_live_asset_raw(env, asset_id=asset_id)
    if table == "knowledge_publications":
        _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=version_id)
        target = _publication_id_of(env, asset_id)
    else:
        target = version_id

    exc = _raw_expect_error(env, f"delete from {table} where id = :i", {"i": target})
    assert exc.orig.sqlstate == IMMUTABLE_RECORD_SQLSTATE  # type: ignore[attr-defined]


def test_knowledge_chunks_is_not_immutable(env: KnowledgeEnv) -> None:
    """**对照组（关键）**：`knowledge_chunks` 的 UPDATE/DELETE **必须成功**。

    它是**可重建的物化结果**，不是历史事实。对可重建的派生数据加
    "改了就是篡改历史"的语义，会让 WP21 无法实现"模型换版不混向量"
    （换 embedding 模型时重切、删旧 chunk 是正常运维）。

    没有这条对照，"两张不可变表被拦"这个断言在一个
    "所有表都加了不可变触发器"的实现上同样会全绿 ——
    而那会让 WP21 的核心验收项无法实现。
    """
    asset_id = uuid.uuid4()
    version_id = _insert_live_asset_raw(env, asset_id=asset_id)
    chunk_id = _insert_chunk(env, version_id=version_id, chunk_no=1, text_content="原文")

    _raw(env, "update knowledge_chunks set text_content = '改过' where id = :i", {"i": chunk_id})
    rows = _raw(env, "select text_content from knowledge_chunks where id = :i", {"i": chunk_id})
    assert rows[0][0] == "改过"

    _raw(env, "delete from knowledge_chunks where id = :i", {"i": chunk_id})
    assert _raw(env, "select 1 from knowledge_chunks where id = :i", {"i": chunk_id}) == []


def test_immutable_error_message_mentions_doc_section(env: KnowledgeEnv) -> None:
    """不可变的报错文案指向数据结构文档 —— 运维在 psql 里看到它要知道去哪查。"""
    asset_id = uuid.uuid4()
    version_id = _insert_live_asset_raw(env, asset_id=asset_id)
    exc = _raw_expect_error(
        env, "update knowledge_versions set body = 'x' where id = :i", {"i": version_id}
    )
    assert "不可变" in str(exc.orig)  # type: ignore[attr-defined]


# --- 3.2 OP-88 判据 1~3（节点必须配合法子表）--------------------------------


def test_op88_rejects_node_with_wrong_entity_type(env: KnowledgeEnv) -> None:
    """**OP-88 判据 2**：节点的 `entity_type` 不是 `KNOWLEDGE` → 提交时 23514。

    这防止"一个 `CONTENT` 节点被当成知识版本" —— 注册表与子表
    说的不是同一件事，而下游（`version_dependencies` 的溯源）取的是节点侧。
    """
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(_ASSET_INSERT_SQL),
            _asset_row_params(asset_id=asset_id, scope=SCOPE_PRODUCT,
                              product_id=env.product_id),
        )
    sqlstate = _assert_deferred_blocked(
        env,
        [
            (
                _insert_version_node_sql(),
                _node_params(
                    node_id=version_id,
                    entity_type="CONTENT",  # 错的类型
                    entity_id=asset_id,
                ),
            ),
            (
                _VERSION_INSERT_SQL,
                {
                    "id": version_id,
                    "asset": asset_id,
                    "body": "正文",
                    "payload": _JSONB_OBJ,
                    "reason": "理由",
                },
            ),
        ],
    )
    assert sqlstate == kn_ddl.CONSISTENCY_SQLSTATE


def test_op88_rejects_node_with_wrong_entity_id(env: KnowledgeEnv) -> None:
    """**OP-88 判据 3**：节点的 `entity_id` ≠ `knowledge_versions.asset_id` → 23514。

    否则"这一版属于哪条知识"有两个互相矛盾的答案。
    """
    asset_id = uuid.uuid4()
    other_asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(text(_ASSET_INSERT_SQL), _asset_row_params(asset_id=asset_id))
        session.execute(
            text(_ASSET_INSERT_SQL), _asset_row_params(asset_id=other_asset_id)
        )
    sqlstate = _assert_deferred_blocked(
        env,
        [
            (
                _insert_version_node_sql(),
                _node_params(
                    node_id=version_id,
                    entity_type=ENTITY_TYPE_KNOWLEDGE,
                    entity_id=other_asset_id,  # 指向另一条知识
                ),
            ),
            (
                _VERSION_INSERT_SQL,
                {
                    "id": version_id,
                    "asset": asset_id,
                    "body": "正文",
                    "payload": _JSONB_OBJ,
                    "reason": "理由",
                },
            ),
        ],
    )
    assert sqlstate == kn_ddl.CONSISTENCY_SQLSTATE


def test_op88_rejects_version_without_node(env: KnowledgeEnv) -> None:
    """**OP-88 判据 1**：`knowledge_versions.id` 没有 `version_nodes` 行 → 23514。

    **FK 管不住这一条**：`knowledge_versions.id` 确实 FK 到
    `version_nodes.id`，所以"节点不存在"时 FK 会立刻报错（23503）。
    判据 1 的触发器查的是"节点存在**且类型对**"—— 两条一起才完整。
    因此这里断言的是**那条 23503**，它正是"节点必须存在"的活性证据。
    """
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(text(_ASSET_INSERT_SQL), _asset_row_params(asset_id=asset_id))
    exc = _raw_expect_error(
        env,
        _VERSION_INSERT_SQL,
        {
            "id": version_id,
            "asset": asset_id,
            "body": "正文",
            "payload": _JSONB_OBJ,
            "reason": "理由",
        },
    )
    assert exc.orig.sqlstate == "23503"  # type: ignore[attr-defined] foreign_key_violation


def test_op88_rejects_orphan_knowledge_node(env: KnowledgeEnv) -> None:
    """**OP-88 反向判据**：`KNOWLEDGE` 节点没有配对的版本行 → 23514。

    **反例必须按实现的实际判据构造**（本项目已踩过的坑）：
    反向触发器**刻意收窄**为"只对**真实存在的逻辑知识**强制" ——
    即 `knowledge_assets` 里有 `id = entity_id` 的行时才要求配对。
    因此孤立节点必须建在**真实存在的 asset 上**；
    若 `entity_id` 是个随机 uuid，触发器会直接放行（那是登记过的代价，
    不是缺陷）。
    """
    asset_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(_ASSET_INSERT_SQL),
            _asset_row_params(asset_id=asset_id, scope=SCOPE_PRODUCT,
                              product_id=env.product_id),
        )
    sqlstate = _assert_deferred_blocked(
        env,
        [
            (
                _insert_version_node_sql(),
                _node_params(
                    node_id=uuid.uuid4(),
                    entity_type=ENTITY_TYPE_KNOWLEDGE,
                    entity_id=asset_id,  # 真实存在的 asset
                ),
            ),
        ],
    )
    assert sqlstate == kn_ddl.CONSISTENCY_SQLSTATE


def test_op88_orphan_node_with_unknown_asset_is_allowed(env: KnowledgeEnv) -> None:
    """**对照组 + 登记的代价**：`entity_id` 指向不存在的 asset 时**放行**。

    收窄的理由：8 类版本实体里只有 `BRIEF` 与 `KNOWLEDGE` 在本批落地，
    其余 6 类的领域表还不存在。若不加收窄，任何"注册节点但暂无领域行"
    的路径都会在提交时失败 —— 那会把别的包的进度变成本包的阻塞。

    代价：指向不存在 asset 的"悬空节点"不会被拦下。危害等级低
    （它不被任何东西引用，溯源也走不到它），已登记为待定项。
    这条对照同时防止"上一条测试在永远 RAISE 的实现上全绿"。
    """
    sqlstate_holder: list[str] = []
    with session_scope(env.settings) as session:
        session.execute(
            text(_insert_version_node_sql()),
            _node_params(
                node_id=uuid.uuid4(),
                entity_type=ENTITY_TYPE_KNOWLEDGE,
                entity_id=uuid.uuid4(),  # 不存在于 knowledge_assets
            ),
        )
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        sqlstate_holder.append("passed")
    assert sqlstate_holder == ["passed"]


def test_op88_happy_path_node_and_version_together(env: KnowledgeEnv) -> None:
    """**对照组（关键）**：合法配对必须**通过**。

    没有这条对照，"节点/子表矛盾被拦"那几条在一个
    **永远报错**的触发器上也会全绿 —— 而那样的实现会让
    所有正常写入都失败（知识库完全不可用）。
    """
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(text(_ASSET_INSERT_SQL), _asset_row_params(asset_id=asset_id))
        session.execute(
            text(_insert_version_node_sql()),
            _node_params(
                node_id=version_id,
                entity_type=ENTITY_TYPE_KNOWLEDGE,
                entity_id=asset_id,
            ),
        )
        session.execute(
            text(_VERSION_INSERT_SQL),
            {
                "id": version_id,
                "asset": asset_id,
                "body": "正文",
                "payload": '{"fact.statement": {"status": "FILLED", "value": "x"}}',
                "reason": "理由",
            },
        )
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    rows = _raw(env, "select body from knowledge_versions where id = :i", {"i": version_id})
    assert rows[0][0] == "正文"


def test_op88_wp18_brief_node_still_works(env: KnowledgeEnv) -> None:
    """**跨包对照**：本包挂在共用表 `version_nodes` 上的触发器
    **不影响 `BRIEF` 节点**（`WHEN` 收窄到 `KNOWLEDGE`）。

    若忘了 `WHEN`，WP18 的每一批测试都会在提交时集体炸 ——
    而本包的测试未必能发现（本包造的节点都是 `KNOWLEDGE`）。
    这条用例用**别的 `entity_type`** 走一遍，正是为了防这件事。
    """
    with session_scope(env.settings) as session:
        session.execute(
            text(_insert_version_node_sql()),
            _node_params(
                node_id=uuid.uuid4(),
                entity_type="PROMPT",  # 别的包的类型，本包不该管
                entity_id=uuid.uuid4(),
            ),
        )
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    # 通过即证明：本包的反向触发器没有越界管辖别的 entity_type。


# --- 3.3 发布事件的范围必须与本 asset 一致 ----------------------------------


def test_publication_rejects_version_of_another_asset(env: KnowledgeEnv) -> None:
    """**事件说"asset A 的版本 v 生效"，而 v 属于 asset B** → 23514。

    **为什么这条比 OP-88 更危险**：OP-88 的两个节点至少指向同一个实体的
    两个说法；而这里会让**投影算出"asset A 的当前生效版本是 B 的版本"**——
    于是 `GET /knowledge/assets/A` 返回的是另一条知识的内容。
    一条"看起来有内容、实际是别人的内容"的知识，比"没有内容"危险得多。
    """
    asset_a = uuid.uuid4()
    version_a = _insert_live_asset_raw(env, asset_id=asset_a)
    asset_b = uuid.uuid4()
    version_b = _insert_live_asset_raw(env, asset_id=asset_b)

    assert version_a is not None  # 用一下，明确 A 也是合法数据
    sqlstate = _assert_deferred_blocked(
        env,
        [
            (
                "insert into knowledge_publications (id, asset_id, version_id,"
                " action, sequence) values (:id, :asset, :version, 'ACTIVATE', 1)",
                {"id": uuid.uuid4(), "asset": asset_a, "version": version_b},
            ),
        ],
    )
    assert sqlstate == kn_ddl.CONSISTENCY_SQLSTATE


def test_publication_accepts_matching_asset(env: KnowledgeEnv) -> None:
    """**对照组**：事件与版本同属一个 asset 时必须**通过**。

    没有这条对照，上一条在一个"拦下所有 publication"的实现上也会全绿。
    """
    asset_id = uuid.uuid4()
    version_id = _insert_live_asset_raw(env, asset_id=asset_id)
    publication_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "insert into knowledge_publications (id, asset_id, version_id,"
                " action, sequence) values (:id, :asset, :version, 'ACTIVATE', 1)"
            ),
            {"id": publication_id, "asset": asset_id, "version": version_id},
        )
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    rows = _raw(
        env, "select action from knowledge_publications where id = :i", {"i": publication_id}
    )
    assert rows[0][0] == "ACTIVATE"


def test_publication_rejects_duplicate_sequence(env: KnowledgeEnv) -> None:
    """同一 asset 内 `sequence` 唯一 → 重复时 23505。

    它是发布事务的**真正串行化点**（比 ETag 更合适：ETag 描述的是
    一个投影列，用来保护"追加事件"是用错的对象）。
    """
    asset_id = uuid.uuid4()
    version_id = _insert_live_asset_raw(env, asset_id=asset_id)
    _raw(
        env,
        "insert into knowledge_publications (id, asset_id, version_id, action,"
        " sequence) values (:id, :asset, :version, 'ACTIVATE', 1)",
        {"id": uuid.uuid4(), "asset": asset_id, "version": version_id},
    )
    exc = _raw_expect_error(
        env,
        "insert into knowledge_publications (id, asset_id, version_id, action,"
        " sequence) values (:id, :asset, :version, 'ARCHIVE', 1)",
        {"id": uuid.uuid4(), "asset": asset_id, "version": version_id},
    )
    assert exc.orig.sqlstate == "23505"  # type: ignore[attr-defined] unique_violation


# --- 3.4 `active_version_id` 必须属于本 asset -------------------------------


def test_active_version_must_belong_to_same_asset(env: KnowledgeEnv) -> None:
    """`knowledge_assets.active_version_id` 指向**别的知识**的版本 → 23514。

    它是一列**投影**，可以被裸 SQL 就地改写 ——
    而"这条知识的当前版本指向另一条知识的版本"同样是静默的错误。
    """
    asset_a = uuid.uuid4()
    _insert_live_asset_raw(env, asset_id=asset_a)
    asset_b = uuid.uuid4()
    version_b = _insert_live_asset_raw(env, asset_id=asset_b)

    sqlstate = _assert_deferred_blocked(
        env,
        [
            (
                "update knowledge_assets set active_version_id = :v where id = :a",
                {"v": version_b, "a": asset_a},
            ),
        ],
    )
    assert sqlstate == kn_ddl.CONSISTENCY_SQLSTATE


def test_active_version_accepts_own_version(env: KnowledgeEnv) -> None:
    """**对照组**：指向**自己的**版本时必须通过。"""
    asset_id = uuid.uuid4()
    version_id = _insert_live_asset_raw(env, asset_id=asset_id)
    with session_scope(env.settings) as session:
        session.execute(
            text("update knowledge_assets set active_version_id = :v where id = :a"),
            {"v": version_id, "a": asset_id},
        )
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    rows = _raw(
        env, "select active_version_id from knowledge_assets where id = :i", {"i": asset_id}
    )
    assert rows[0][0] == version_id


# --- 3.5 CHECK 约束（逐条活性）---------------------------------------------


def test_check_rejects_unknown_category(env: KnowledgeEnv) -> None:
    """`ck_knowledge_assets_category_valid`：类别必须是六类之一。"""
    exc = _raw_expect_error(
        env,
        _ASSET_INSERT_SQL,
        _asset_row_params(asset_id=uuid.uuid4(), category="NOT_A_CATEGORY"),
    )
    assert exc.orig.sqlstate == "23514"  # type: ignore[attr-defined]
    assert "ck_knowledge_assets_category_valid" in str(exc.orig)  # type: ignore[attr-defined]


def test_check_rejects_fact_global_scope(env: KnowledgeEnv) -> None:
    """**`ck_knowledge_assets_fact_requires_concrete_scope`**：绕过服务层也进不去。

    这是"事实范围必须明确"的数据库层 —— 导入脚本与数据回填都绕不过它。
    """
    exc = _raw_expect_error(
        env,
        _ASSET_INSERT_SQL,
        _asset_row_params(
            asset_id=uuid.uuid4(), category=CATEGORY_FACT, scope=SCOPE_GLOBAL
        ),
    )
    assert "ck_knowledge_assets_fact_requires_concrete_scope" in str(exc.orig)  # type: ignore[attr-defined]


def test_check_scope_requires_matching_column(env: KnowledgeEnv) -> None:
    """`scope='PRODUCT'` 却没有 `product_id` → 被 CHECK 拦下。

    逐条验四条范围 CHECK，因为它们是四段独立的 SQL ——
    漏一条不会让别的三条失效，只会留下一个静默的洞。
    """
    cases = [
        (SCOPE_PRODUCT, "ck_knowledge_assets_scope_product_needs_product_id"),
        (SCOPE_BRAND, "ck_knowledge_assets_scope_brand_needs_brand_id"),
        (SCOPE_PROJECT, "ck_knowledge_assets_scope_project_needs_project_id"),
    ]
    for scope, constraint in cases:
        exc = _raw_expect_error(
            env,
            _ASSET_INSERT_SQL,
            _asset_row_params(
                asset_id=uuid.uuid4(),
                category=CATEGORY_STYLE,  # 非 FACT，避开另一条 CHECK
                scope=scope,
            ),
        )
        assert constraint in str(exc.orig), f"{scope} 的 CHECK 没生效"  # type: ignore[attr-defined]


def test_check_global_scope_rejects_qualifier(env: KnowledgeEnv) -> None:
    """`ck_knowledge_assets_scope_global_has_no_qualifier`：GLOBAL 不带限定列。"""
    exc = _raw_expect_error(
        env,
        _ASSET_INSERT_SQL,
        _asset_row_params(
            asset_id=uuid.uuid4(),
            category=CATEGORY_STYLE,
            scope=SCOPE_GLOBAL,
            brand_id=env.brand_id,
        ),
    )
    assert "ck_knowledge_assets_scope_global_has_no_qualifier" in str(exc.orig)  # type: ignore[attr-defined]


def test_check_rejects_empty_payload(env: KnowledgeEnv) -> None:
    """**`ck_knowledge_versions_structured_payload_not_empty`** ——
    "禁止无结构散文替代事实字段"的数据库层。

    `body` 里那句"我们产品续航 18 小时"若没有结构，就成了一条
    **无法被程序消费、也无法被程序校验**的事实 ——
    下游只能整段塞进 Prompt，而"这个数字有没有条件限定"永远问不出来。
    """
    asset_id = uuid.uuid4()
    _insert_live_asset_raw(env, asset_id=asset_id)
    exc = _assert_version_check_blocked(
        env, asset_id=asset_id, body="散文正文", payload="{}"
    )
    assert exc.orig.sqlstate == "23514"  # type: ignore[attr-defined]
    assert "structured_payload_not_empty" in str(exc.orig)  # type: ignore[attr-defined]


def test_check_rejects_empty_payload_directly(env: KnowledgeEnv) -> None:
    """同一条 CHECK 的**名字级**验证：断言约束名而不只是 sqlstate。

    `structured_payload` 上有两条 CHECK（非空 / 必须是 object），
    只断言 `23514` 区分不了是哪一条拦下的。两条一起才能确定
    "空 payload"这件事确实由**非空**那条管。
    """
    asset_id = uuid.uuid4()
    _insert_live_asset_raw(env, asset_id=asset_id)
    exc = _assert_version_check_blocked(
        env, asset_id=asset_id, body="散文正文", payload="{}"
    )
    assert "ck_knowledge_versions_structured_payload_not_empty" in str(exc.orig)  # type: ignore[attr-defined]


def test_check_rejects_non_object_payload(env: KnowledgeEnv) -> None:
    """`ck_knowledge_versions_structured_payload_is_object`：payload 必须是 object。

    允许数组会让"按字段路径取值"这件事没有定义。
    """
    asset_id = uuid.uuid4()
    _insert_live_asset_raw(env, asset_id=asset_id)
    exc = _assert_version_check_blocked(
        env, asset_id=asset_id, payload="[1, 2, 3]"
    )
    assert "ck_knowledge_versions_structured_payload_is_object" in str(exc.orig)  # type: ignore[attr-defined]


def test_check_rejects_empty_body(env: KnowledgeEnv) -> None:
    """`ck_knowledge_versions_body_not_empty`：正文不能为空（长度 0）。

    只有结构、没有叙述的知识对人不可读 —— 而"给审核人看"是发布前的一环。

    **数据库层只判 `char_length > 0`，"纯空白"由服务层拦** ——
    这是全项目一贯的口径（WP18 的 `ck_brief_claims_claim_text_not_empty`
    等 10 条 CHECK 全是同一写法）。两层的分工：
    · 数据库层挡住**结构性错误**（忘传字段、导入脚本给了 `''`）；
    · 服务层挡住**语义错误**（只敲了空格就提交），并给出可读的 422。
    因此这里断言的是空字符串；纯空白的情形由
    `test_blank_body_passes_db_check_but_service_rejects_it` 明确记录。
    """
    asset_id = uuid.uuid4()
    _insert_live_asset_raw(env, asset_id=asset_id)
    exc = _assert_version_check_blocked(env, asset_id=asset_id, body="")
    assert "ck_knowledge_versions_body_not_empty" in str(exc.orig)  # type: ignore[attr-defined]


def test_check_rejects_effective_window_reversed(env: KnowledgeEnv) -> None:
    """`ck_knowledge_versions_effective_window_ordered`：生效窗口的起点不能晚于终点。"""
    asset_id = uuid.uuid4()
    _insert_live_asset_raw(env, asset_id=asset_id)
    version_id = uuid.uuid4()
    exc = _raw_expect_error_in_tx(
        env,
        [
            (
                _insert_version_node_sql(),
                # revision 2：`_insert_live_asset_raw` 已占了 1 号。
                _node_params(
                    node_id=version_id,
                    entity_type=ENTITY_TYPE_KNOWLEDGE,
                    entity_id=asset_id,
                    revision_no=2,
                ),
            ),
            (
                "insert into knowledge_versions (id, asset_id, body,"
                " structured_payload, change_reason, effective_from, effective_until)"
                " values (:id, :asset, '正文', cast(:payload as jsonb), '理由',"
                " timestamptz '2030-01-02', timestamptz '2030-01-01')",
                {"id": version_id, "asset": asset_id, "payload": _JSONB_OBJ},
            ),
        ],
    )
    assert "ck_knowledge_versions_effective_window_ordered" in str(exc.orig)  # type: ignore[attr-defined]


def test_check_rejects_unsupported_publication_action(env: KnowledgeEnv) -> None:
    """`ck_knowledge_publications_action_valid`：动作必须是四种之一。"""
    asset_id = uuid.uuid4()
    version_id = _insert_live_asset_raw(env, asset_id=asset_id)
    exc = _raw_expect_error(
        env,
        "insert into knowledge_publications (id, asset_id, version_id, action,"
        " sequence) values (:id, :asset, :version, 'NOT_AN_ACTION', 1)",
        {"id": uuid.uuid4(), "asset": asset_id, "version": version_id},
    )
    assert "ck_knowledge_publications_action_valid" in str(exc.orig)  # type: ignore[attr-defined]


def test_check_rejects_non_positive_sequence(env: KnowledgeEnv) -> None:
    """`ck_knowledge_publications_sequence_positive`：序号必须为正。

    序号从 1 开始 —— 允许 0 会让"零号事件"出现在投影里，
    而"它是第几个"这件事就没有答案了。
    """
    asset_id = uuid.uuid4()
    version_id = _insert_live_asset_raw(env, asset_id=asset_id)
    exc = _raw_expect_error(
        env,
        "insert into knowledge_publications (id, asset_id, version_id, action,"
        " sequence) values (:id, :asset, :version, 'ACTIVATE', 0)",
        {"id": uuid.uuid4(), "asset": asset_id, "version": version_id},
    )
    assert "ck_knowledge_publications_sequence_positive" in str(exc.orig)  # type: ignore[attr-defined]


def test_check_chunks_unique_version_chunk_no(env: KnowledgeEnv) -> None:
    """`uq_knowledge_chunks_version_chunk_no`：同一版本的块号唯一。

    **允许重复块号**会让"按块号定位这段原文"有两个答案，
    而引用方存的就是块对应的片段文本。
    """
    asset_id = uuid.uuid4()
    version_id = _insert_live_asset_raw(env, asset_id=asset_id)
    _insert_chunk(env, version_id=version_id, chunk_no=1, text_content="第一块")
    exc = _raw_expect_error(
        env,
        "insert into knowledge_chunks (id, knowledge_version_id, chunk_no,"
        " text_content, text_hash, token_count, locator, keywords)"
        " values (:id, :vid, 1, '重复块号', :hash, 1, cast('{}' as jsonb),"
        " cast(:kw as text[]))",
        {
            "id": uuid.uuid4(),
            "vid": version_id,
            "hash": "a" * 64,
            "kw": [],
        },
    )
    assert exc.orig.sqlstate == "23505"  # type: ignore[attr-defined]


def test_check_chunks_rejects_bad_text_hash_length(env: KnowledgeEnv) -> None:
    """`ck_knowledge_chunks_text_hash_length`：摘要必须是 64 位十六进制。

    长度不对的摘要看起来"有内容"，但比对时永远不相等 ——
    于是"这段原文有没有变过"这个问题会永远答"变了"。
    """
    asset_id = uuid.uuid4()
    version_id = _insert_live_asset_raw(env, asset_id=asset_id)
    exc = _raw_expect_error(
        env,
        "insert into knowledge_chunks (id, knowledge_version_id, chunk_no,"
        " text_content, text_hash, token_count, locator, keywords)"
        " values (:id, :vid, 1, '文本', 'tooshort', 1, cast('{}' as jsonb),"
        " cast(:kw as text[]))",
        {"id": uuid.uuid4(), "vid": version_id, "kw": []},
    )
    assert "ck_knowledge_chunks_text_hash_length" in str(exc.orig)  # type: ignore[attr-defined]


def test_check_proposals_settled_needs_confirmer(env: KnowledgeEnv) -> None:
    """`ck_knowledge_proposals_settled_needs_confirmer`：已处置的候选必须有确认人。

    "谁确认的"正是审计要回答的问题 —— 一条 ACCEPTED 但没有确认人的候选，
    在出问题时无法定责。
    """
    exc = _raw_expect_error(
        env,
        "insert into knowledge_proposals (id, proposed_payload, scope, state)"
        " values (:id, cast(:payload as jsonb), 'GLOBAL', 'ACCEPTED')",
        {"id": uuid.uuid4(), "payload": _JSONB_OBJ},
    )
    assert "ck_knowledge_proposals_settled_needs_confirmer" in str(exc.orig)  # type: ignore[attr-defined]


def test_check_proposals_pending_has_no_confirmer(env: KnowledgeEnv) -> None:
    """`ck_knowledge_proposals_pending_has_no_confirmer`：PENDING 的候选不该有确认人。

    有确认人却还是 PENDING，说明状态与审计信息矛盾。
    """
    exc = _raw_expect_error(
        env,
        "insert into knowledge_proposals (id, proposed_payload, scope, state,"
        " confirmed_by) values (:id, cast(:payload as jsonb), 'GLOBAL',"
        " 'PENDING', :who)",
        {"id": uuid.uuid4(), "who": env.owner_id, "payload": _JSONB_OBJ},
    )
    assert "ck_knowledge_proposals_pending_has_no_confirmer" in str(exc.orig)  # type: ignore[attr-defined]


def test_check_proposals_reject_needs_reason(env: KnowledgeEnv) -> None:
    """`ck_knowledge_proposals_reject_needs_reason`：拒绝必须有理由。

    一条被拒的 AI 建议若无理由，下一个看到它的人只能重走一遍判断。
    """
    exc = _raw_expect_error(
        env,
        "insert into knowledge_proposals (id, proposed_payload, scope, state,"
        " confirmed_by, decision_reason)"
        " values (:id, cast(:payload as jsonb), 'GLOBAL', 'REJECTED', :who, NULL)",
        {"id": uuid.uuid4(), "who": env.owner_id, "payload": _JSONB_OBJ},
    )
    assert "ck_knowledge_proposals_reject_needs_reason" in str(exc.orig)  # type: ignore[attr-defined]


def test_check_proposals_payload_not_empty(env: KnowledgeEnv) -> None:
    """`ck_knowledge_proposals_proposed_payload_not_empty`：候选载荷不能是空对象。

    一条"建议改点什么、但什么都没说"的候选无法被决策 ——
    确认人只能重走一遍 AI 判断。
    """
    exc = _raw_expect_error(
        env,
        "insert into knowledge_proposals (id, proposed_payload, scope, state)"
        " values (:id, cast('{}' as jsonb), 'GLOBAL', 'PENDING')",
        {"id": uuid.uuid4()},
    )
    assert "ck_knowledge_proposals_proposed_payload_not_empty" in str(exc.orig)  # type: ignore[attr-defined]


def test_check_archived_implies_had_version(env: KnowledgeEnv) -> None:
    """`ck_knowledge_assets_archived_implies_had_version`：归档的前提是曾生效过。

    "从未生效就归档"是自相矛盾的 —— 归档的语义是"退出可用"，
    而它从未可用过。
    """
    exc = _raw_expect_error(
        env,
        _ASSET_INSERT_SQL,
        _asset_row_params(
            asset_id=uuid.uuid4(),
            scope=SCOPE_PRODUCT,
            product_id=env.product_id,
            archived_at="2030-01-01T00:00:00+00:00",
        ),
    )
    assert "ck_knowledge_assets_archived_implies_had_version" in str(exc.orig)  # type: ignore[attr-defined]


def test_archived_asset_row_is_accepted(env: KnowledgeEnv) -> None:
    """**对照组**：有生效版本 + 归档时间 → 必须通过。"""
    asset_id = uuid.uuid4()
    version_id = _insert_live_asset_raw(env, asset_id=asset_id)
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "update knowledge_assets set active_version_id = :v,"
                " archived_at = now() where id = :a"
            ),
            {"v": version_id, "a": asset_id},
        )
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    rows = _raw(
        env, "select archived_at from knowledge_assets where id = :i", {"i": asset_id}
    )
    assert rows[0][0] is not None


# --- 3.6 部分索引（"少了 WHERE，唯一约束会静默退化"）------------------------


def test_fact_lookup_partial_index_exists_with_where(env: KnowledgeEnv) -> None:
    """**精确读取索引存在，且带正确的 `WHERE`**（规格源 §14 的索引计划）。

    > | 事实精确读取 | `knowledge_assets(product_id,category,active_version_id)` |

    少了 `WHERE` 会让"归档的知识也在索引里"—— 查询仍能用索引，
    但会多扫一批不该出现的行。因此除了存在性，还要**逐条核对 WHERE**。
    """
    rows = _raw(
        env,
        "select indexdef from pg_indexes where tablename = 'knowledge_assets'"
        " and indexname = 'ix_knowledge_assets_fact_lookup'",
    )
    assert rows, "精确读取索引不存在 —— 规格源 §14 明确要求它"
    indexdef = rows[0][0]
    assert "product_id" in indexdef
    assert "category" in indexdef
    assert "active_version_id" in indexdef
    # **`WHERE` 必须逐条核对**：PG 会把 `state = 'OPEN'` 规范化为
    # `((state)::text = 'OPEN'::text)` 这类形式，因此按"关键片段"断言。
    assert "WHERE" in indexdef
    assert "active_version_id IS NOT NULL" in indexdef
    assert "archived_at IS NULL" in indexdef


def test_all_expected_triggers_exist(env: KnowledgeEnv) -> None:
    """六个触发器**都真的建出来了**（名字与 `ddl.py` 的唯一来源一致）。

    迁移与验证脚本若各自手写名字，就会出现"迁移建了 `trg_x`、
    验证脚本找 `trg_y`"这种**看起来通过、实际没有保护**的情况。
    因此名字只有一处来源，测试从那里取。
    """
    rows = _raw(
        env,
        "select tgname from pg_trigger where not tgisinternal"
        " and tgrelid in ('knowledge_versions'::regclass,"
        " 'knowledge_publications'::regclass, 'knowledge_assets'::regclass,"
        " 'version_nodes'::regclass)",
    )
    present = {row[0] for row in rows}
    for name in kn_ddl.trigger_names():
        assert name in present, f"触发器 {name} 不存在"


def test_all_knowledge_check_constraints_exist(env: KnowledgeEnv) -> None:
    """五张表的 CHECK 约束**逐条存在**（防止迁移漏建）。"""
    expected = {
        c.name
        for model in OWN_TABLES
        for c in model.constraints
        if type(c).__name__ == "CheckConstraint"
    }
    rows = _raw(
        env,
        "select conname from pg_constraint where contype = 'c' and conrelid in"
        " ('knowledge_assets'::regclass, 'knowledge_versions'::regclass,"
        " 'knowledge_publications'::regclass, 'knowledge_chunks'::regclass,"
        " 'knowledge_proposals'::regclass)",
    )
    present = {row[0] for row in rows}
    missing = expected - present
    assert not missing, f"这些 CHECK 没有建出来：{sorted(missing)}"


# --- 3.7 零残留（**与运行前基线差分**，不是"全表 0 行"）---------------------


def test_no_orphan_rows_left_by_this_module(env: KnowledgeEnv) -> None:
    """本模块的探针**不留无法归属的孤儿行**。

    **判据是"每个版本行都有它的 asset"**，而不是"全表 0 行"——
    表里有别的包的合法遗留，而且不可变表**物理删不掉**
    （`DELETE FROM knowledge_versions` 会被 `rednote_reject_mutation()` 拦下，
    WP18 实测连自己的探针残留都清不掉）。
    "全表 0 行"这个判据在任何非空库上都会假失败。

    孤儿版本行（有版本、没 asset）是最危险的一种残留：
    它会让"这条知识有几个版本"的计数对不上，而投影读不到它。
    """
    rows = _raw(
        env,
        "select count(*) from knowledge_versions v left join knowledge_assets a"
        " on a.id = v.asset_id where a.id is null",
    )
    assert rows[0][0] == 0


def test_no_orphan_publications(env: KnowledgeEnv) -> None:
    """发布事件不留孤儿（每个事件都有它的 asset 与版本）。"""
    rows = _raw(
        env,
        "select count(*) from knowledge_publications p"
        " left join knowledge_assets a on a.id = p.asset_id"
        " left join knowledge_versions v on v.id = p.version_id"
        " where a.id is null or v.id is null",
    )
    assert rows[0][0] == 0


def test_probe_asset_row_is_still_physically_present(env: KnowledgeEnv) -> None:
    """**记录一个事实（不是缺陷）**：本层探针造的 asset 行**物理删不掉**。

    不可变保护只作用于版本表，`knowledge_assets` 是可变的 ——
    但它的版本行删不掉，于是 asset 行也就不能删（FK RESTRICT）。
    这条用例把这个事实**写成断言**，让"残留无法清理"成为已知代价，
    而不是下一次有人发现"表里有垃圾"时重新调查一遍。
    """
    asset_id = uuid.uuid4()
    version_id = _insert_live_asset_raw(env, asset_id=asset_id)
    exc = _raw_expect_error(
        env, "delete from knowledge_versions where id = :i", {"i": version_id}
    )
    assert exc.orig.sqlstate == IMMUTABLE_RECORD_SQLSTATE  # type: ignore[attr-defined]
    # asset 行仍在（这正是"残留删不掉"的机制）。
    assert _raw(env, "select 1 from knowledge_assets where id = :i", {"i": asset_id})


def _publication_id_of(env: KnowledgeEnv, asset_id: uuid.UUID) -> uuid.UUID:
    """取某条知识的最后一条发布事件 id。"""
    rows = _raw(
        env,
        "select id from knowledge_publications where asset_id = :a"
        " order by sequence desc limit 1",
        {"a": asset_id},
    )
    assert rows, "该知识没有任何发布事件"
    return rows[0][0]


def test_blank_body_passes_db_check_but_service_rejects_it(env: KnowledgeEnv) -> None:
    """**已知边界（写成断言，避免下次重新调查）**：纯空白正文

    **数据库层放行**（`char_length('   ') = 3 > 0`），
    但**服务层拒绝**（`_require_body` 先 `strip()` 再判空）。
    这是全项目一贯的两层分工（WP18 的 10 条 `*_not_empty` CHECK 同构）：
    · 数据库层挡**结构性错误**（忘传字段、导入脚本给了 `''`）；
    · 服务层挡**语义错误**（只敲了空格就提交）并给出可读 422。

    代价：绕过服务层的裸 SQL 可以写入纯空白正文。危害低
    （它不影响任何判据的计算，只是显示难看），已登记为待定项。
    """
    asset_id = uuid.uuid4()
    _insert_live_asset_raw(env, asset_id=asset_id)
    version_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(_insert_version_node_sql()),
            _node_params(
                node_id=version_id,
                entity_type=ENTITY_TYPE_KNOWLEDGE,
                entity_id=asset_id,
                revision_no=2,
            ),
        )
        session.execute(
            text(_VERSION_INSERT_SQL),
            {
                "id": version_id,
                "asset": asset_id,
                "body": "   ",  # 纯空白：数据库层放行
                "payload": _JSONB_OBJ,
                "reason": "理由",
            },
        )
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    # 服务层拒绝（这是真正的守门处）。
    with session_scope(env.settings) as session:
        repo = KnowledgeRepository(session)
        asset = repo.get_asset(asset_id)
        assert asset is not None
        with pytest.raises(ApiError) as caught:
            kn_service.add_version(
                repo,
                asset=asset,
                body="   ",
                structured_payload={"fact.statement": "x"},
                change_reason="理由",
                actor_id=env.owner_id,
            )
    assert caught.value.code.value == "VALIDATION_FAILED"


# ===========================================================================
# 第 4 层：HTTP
#
# 端点是**薄层**（只做校验 + 调 service），因此这一层只覆盖"接线是否正确"：
# 状态码、响应形状、权限、`If-Match` 语义、错误体形状。
# 业务判据由第 2 层负责，不在这里重复。
#
# **本层是唯一验证"权限依赖真的接上了"的地方** ——
# 一个忘写 `dependencies=KNOWLEDGE_WRITE` 的写端点，在服务层测试里
# 完全看不出来（它们直接调 service）。
# ===========================================================================

CSRF_SECRET_PLAINTEXT = "test-csrf-secret-plaintext"

#: `verify_csrf` 第一道防线比对的 Origin。
#: `TestClient` 不会自动补 `Origin`/`Referer`，而 `verify_csrf` 在
#: 两者都缺失时是**拒绝**而不是放行。值必须与 `request.url.netloc` 一致。
TEST_ORIGIN = "http://testserver"


#: 端点 URL 前缀。本层的断言全部用**带前缀的完整 URL** —— 因为第 4 层
#: 的价值就在于验证"前端要拼的那条 URL"能通。因此夹具必须把
#: `settings.api_prefix` 传给 `include_router`：router 内部声明的路径是
#: 裸 `/knowledge/...`（`main.py` 负责加前缀），漏传会让它直接暴露在根
#: 路径下，于是测试测的是**一个生产里不存在的 URL**（表现为整层 404）。
#:
#: **与 WP18 的口径偏差（有意）**：`test_brief_wp18.py` 的夹具不带
#: prefix，那边的 URL 是裸 `/projects/...` —— 那是"测 router 单元"的
#: 口径。本包选择带前缀，是为了让"前缀漂移"（例如有人把 `/api/v2` 写进
#: settings）在本层被抓住，而不是等到前端联调。
API_PREFIX = "/api/v1"


@pytest.fixture
def app(env: KnowledgeEnv) -> FastAPI:
    """按注入配置装配 app，并**在 `main.py` 尚未接线时**补挂本模块 router。

    `main.py` 的 `include_router` 由主 agent 收口时补（接缝冻结 §1 禁改）。
    因此这里显式挂上 —— 否则测试测的是"主 agent 还没做的接线"，
    而那会让本文件在收口前一直是红的。

    **但挂载必须幂等**：收口后 `create_app` 自己就会挂一次，此时再挂一次
    会让每条路由**重复注册**，症状是 `app.routes` 里同一路径出现两遍
    （本包收口时实测：`test_router_exposes_exactly_nine_endpoints`
    数到 18 个操作而不是 9 个）。因此这里先探测"是否已挂"，已挂就跳过 ——
    这样本文件在**收口前**（红灯变绿）与**收口后**（不重复）都是对的。

    探测依据用 `RouteOperationKey`（path + methods），而不是 router 对象身份：
    `create_app` 内部 import 的是同一个模块级 `router`，但
    `include_router` 会把它**复制成新路由对象**，身份比较不可靠。
    """
    from rednote.main import create_app

    # 断言而非硬编码：本文件的 URL 字面量写死了 `/api/v1`，
    # 若注入配置给的 prefix 不同，这一层就是在验错的 URL —— 直接红灯。
    assert env.settings.api_prefix == API_PREFIX, (
        f"测试断言写死了 {API_PREFIX!r}，注入配置给的是 "
        f"{env.settings.api_prefix!r} —— 两者必须一致"
    )

    application = create_app(env.settings)

    already_mounted = any(
        getattr(route, "path", "").startswith(f"{API_PREFIX}/knowledge")
        for route in application.routes
    )
    if not already_mounted:
        from rednote.modules.knowledge.api import router as knowledge_router

        application.include_router(knowledge_router, prefix=env.settings.api_prefix)
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, env: KnowledgeEnv, *, user_id: uuid.UUID) -> None:
    """造一条会话并写 Cookie（含 CSRF 令牌）。

    **CSRF 必须一起处理**：`current_user` 依赖对写方法强制 `verify_csrf`，
    且它是**结构上无法绕过**的 —— 所以测试必须像真实前端一样
    同时带 Cookie 与 `X-CSRF-Token`。绕过它（例如 monkeypatch）
    会让 HTTP 层测试不再验证真实的鉴权链。
    """
    from datetime import datetime, timedelta, timezone

    from rednote.platform.csrf import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
    from rednote.platform.tokens import generate_session_token, hash_token

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


def _write_headers(*, if_match: str | None = None) -> dict[str, str]:
    """写请求的公共头：CSRF 令牌 + Origin + 可选 `If-Match`。

    两者都带上，测试里的 403 断言才只可能来自**权限**判定。
    """
    headers = {"X-CSRF-Token": CSRF_SECRET_PLAINTEXT, "Origin": TEST_ORIGIN}
    if if_match is not None:
        headers["If-Match"] = if_match
    return headers


def _create_body(
    env: KnowledgeEnv,
    *,
    category: str = CATEGORY_FACT,
    title: str = "HTTP 创建的知识",
    scope: str | None = None,
) -> dict[str, Any]:
    """构造 `POST /knowledge/assets` 的请求体。"""
    effective_scope = scope or (
        SCOPE_PRODUCT if category in CATEGORIES_REQUIRING_CONCRETE_SCOPE else SCOPE_GLOBAL
    )
    body: dict[str, Any] = {
        "category": category,
        "title": title,
        "scope": effective_scope,
        "body": f"{category} 的正文",
        "structuredPayload": _payload_for(category),
    }
    if effective_scope == SCOPE_PRODUCT:
        body["productId"] = str(env.product_id)
    if effective_scope == SCOPE_BRAND:
        body["brandId"] = str(env.brand_id)
    if effective_scope == SCOPE_PROJECT:
        body["projectId"] = str(env.project_id)
    return body


# --- 4.1 鉴权与权限 --------------------------------------------------------


def test_list_requires_login(client: TestClient) -> None:
    """读端点也要登录（知识是"全员可查"，而"全员"指组织内成员，不是公网）。"""
    assert client.get("/api/v1/knowledge/assets").status_code == 401


def test_create_requires_login(client: TestClient, env: KnowledgeEnv) -> None:
    """未登录写 → 401。"""
    response = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    )
    assert response.status_code == 401


def test_create_requires_csrf_token(client: TestClient, env: KnowledgeEnv) -> None:
    """登录但缺 CSRF 令牌 → 403（第 5 步的两道防线之一）。

    **这条必须存在**：它证明 CSRF 校验真的接在这条链上 ——
    一个"忘了把写端点放进受保护依赖"的实现会让它变成 201。
    """
    _login(client, env, user_id=env.owner_id)
    response = client.post(
        "/api/v1/knowledge/assets",
        json=_create_body(env),
        headers={"Origin": TEST_ORIGIN},  # 故意不给 X-CSRF-Token
    )
    assert response.status_code == 403


def test_operator_can_create(client: TestClient, env: KnowledgeEnv) -> None:
    """**运营也能管理知识** —— 需求 §2.2 的权限矩阵最后两行是"全员知识库"。

    这与项目级权限不同：知识是跨项目的组织级资产，
    因此用 `require_org_action(KNOWLEDGE_MANAGE)`（运营仅此一项允许）。
    """
    _login(client, env, user_id=env.owner_id)
    response = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    )
    assert response.status_code == 201


def test_admin_can_create(client: TestClient, env: KnowledgeEnv) -> None:
    """管理者同样可以（权限矩阵两行都允许）。"""
    _login(client, env, user_id=env.admin_id)
    response = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    )
    assert response.status_code == 201


# --- 4.2 创建 --------------------------------------------------------------


def test_http_create_returns_201_with_identity(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """201 且返回**草稿身份与版本**（协议 §9 的原文用词）。

    `state` 必须是 `DRAFT` —— 建与发布是两个动作（见 api docstring）。
    """
    _login(client, env, user_id=env.owner_id)
    response = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert set(data) >= {"assetId", "versionId", "revisionNo", "etag", "state"}
    assert data["state"] == ASSET_STATE_DRAFT
    assert data["revisionNo"] == 1
    assert data["etag"]


def test_http_create_rejects_missing_payload_with_field_errors(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """422 且带 `fieldErrors` / `missingFields` —— 客户端据此定位缺哪个字段。"""
    _login(client, env, user_id=env.owner_id)
    body = _create_body(env)
    del body["structuredPayload"]
    response = client.post(
        "/api/v1/knowledge/assets", json=body, headers=_write_headers()
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["details"]["missingFields"] == ["fact.statement"]


def test_http_create_rejects_fact_global(client: TestClient, env: KnowledgeEnv) -> None:
    """422：`FACT` + `GLOBAL` 被服务层拦（数据库 CHECK 是第二层）。"""
    _login(client, env, user_id=env.owner_id)
    body = _create_body(env, scope=SCOPE_GLOBAL)
    response = client.post(
        "/api/v1/knowledge/assets", json=body, headers=_write_headers()
    )
    assert response.status_code == 422


def test_http_create_rejects_unknown_category(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """422：未知类别（不能静默建一条无 Schema 的知识）。"""
    _login(client, env, user_id=env.owner_id)
    body = _create_body(env)
    body["category"] = "NOT_A_CATEGORY"
    response = client.post(
        "/api/v1/knowledge/assets", json=body, headers=_write_headers()
    )
    assert response.status_code == 422


# --- 4.3 详情与列表 --------------------------------------------------------


def test_http_get_detail_shape(client: TestClient, env: KnowledgeEnv) -> None:
    """详情必须给齐协议 §9 要求的四样：内容、来源、历史状态、可用范围。

    归档知识也走这条路（"归档可追溯"）—— 这里先验草稿的形态。
    """
    _login(client, env, user_id=env.owner_id)
    created = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    ).json()["data"]
    response = client.get(f"/api/v1/knowledge/assets/{created['assetId']}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) >= {
        "assetId",
        "category",
        "title",
        "scope",
        "state",
        "activeVersionId",
        "version",
        "versions",
        "publicationHistory",
        "activeChunkCount",
    }
    assert data["state"] == ASSET_STATE_DRAFT
    assert data["activeVersionId"] is None
    assert data["publicationHistory"] == []


def test_http_get_detail_with_version_id_reads_old_version(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """`?versionId=` 能读**指定版本**（"旧引用并存"的读取路径）。"""
    _login(client, env, user_id=env.owner_id)
    created = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    ).json()["data"]
    asset_id, v1_id = created["assetId"], created["versionId"]
    client.post(
        f"/api/v1/knowledge/assets/{asset_id}/versions",
        json={
            "body": "第二版正文",
            "structuredPayload": _payload_for(CATEGORY_FACT, "第二版"),
            "changeReason": "第二版",
        },
        headers=_write_headers(),
    )
    response = client.get(
        f"/api/v1/knowledge/assets/{asset_id}", params={"versionId": v1_id}
    )
    assert response.status_code == 200
    assert response.json()["data"]["version"]["versionId"] == v1_id


def test_http_get_unknown_asset_404(client: TestClient, env: KnowledgeEnv) -> None:
    """未知 id → 404（不是空对象）。"""
    _login(client, env, user_id=env.owner_id)
    response = client.get(f"/api/v1/knowledge/assets/{uuid.uuid4()}")
    assert response.status_code == 404


def test_http_list_envelope_shape(client: TestClient, env: KnowledgeEnv) -> None:
    """列表是分页信封（`data` + `page` 的游标信息）。"""
    _login(client, env, user_id=env.owner_id)
    client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    )
    response = client.get(
        "/api/v1/knowledge/assets", params={"productId": str(env.product_id)}
    )
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload["data"], list)
    assert payload["data"]
    assert "page" in payload


def test_http_list_rejects_unknown_state(client: TestClient, env: KnowledgeEnv) -> None:
    """422：`state` 拼错要报错，不能静默给空列表。"""
    _login(client, env, user_id=env.owner_id)
    response = client.get("/api/v1/knowledge/assets", params={"state": "NOT_A_STATE"})
    assert response.status_code == 422


def test_http_list_include_archived_defaults_false(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """`includeArchived` 默认 false（本包新增的参数，协议没有）。"""
    _login(client, env, user_id=env.owner_id)
    created = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    ).json()["data"]
    asset_id, v1_id = created["assetId"], created["versionId"]
    client.post(
        f"/api/v1/knowledge/assets/{asset_id}/publications",
        json={"versionId": v1_id, "action": ACTION_ACTIVATE},
        headers=_write_headers(),
    )
    client.post(
        f"/api/v1/knowledge/assets/{asset_id}/publications",
        json={"action": ACTION_ARCHIVE},
        headers=_write_headers(),
    )
    default = client.get(
        "/api/v1/knowledge/assets", params={"productId": str(env.product_id)}
    ).json()["data"]
    widened = client.get(
        "/api/v1/knowledge/assets",
        params={"productId": str(env.product_id), "includeArchived": "true"},
    ).json()["data"]
    assert asset_id not in {item["id"] for item in default}
    assert asset_id in {item["id"] for item in widened}


# --- 4.4 加版本 ------------------------------------------------------------


def test_http_add_version_returns_201(client: TestClient, env: KnowledgeEnv) -> None:
    """201 且返回 `(versionId, revisionNo, etag)`。"""
    _login(client, env, user_id=env.owner_id)
    created = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    ).json()["data"]
    response = client.post(
        f"/api/v1/knowledge/assets/{created['assetId']}/versions",
        json={
            "body": "补充口径后的正文",
            "structuredPayload": _payload_for(CATEGORY_FACT, "补充口径"),
            "changeReason": "补充测量口径",
        },
        headers=_write_headers(),
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["revisionNo"] == 2
    assert data["versionId"] != created["versionId"]


def test_http_add_version_requires_change_reason(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """缺 `changeReason` 时 pydantic 直接 422（它是必填字段）。

    版本不可变 —— `changeReason` 是**唯一**能解释"为什么又发了一版"的地方，
    因此它在请求模型里就是必填的（不是靠服务层兜）。
    """
    _login(client, env, user_id=env.owner_id)
    created = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    ).json()["data"]
    response = client.post(
        f"/api/v1/knowledge/assets/{created['assetId']}/versions",
        json={"body": "正文", "structuredPayload": _payload_for(CATEGORY_FACT)},
        headers=_write_headers(),
    )
    assert response.status_code == 422


def test_http_add_version_rejects_foreign_base_version(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """`baseVersionId` 不是本知识的版本 → 422。

    静默忽略会让客户端以为基线记对了 —— 而它记的是别人那版。
    """
    _login(client, env, user_id=env.owner_id)
    first = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    ).json()["data"]
    second = client.post(
        "/api/v1/knowledge/assets",
        json=_create_body(env, title="另一条知识"),
        headers=_write_headers(),
    ).json()["data"]
    response = client.post(
        f"/api/v1/knowledge/assets/{first['assetId']}/versions",
        json={
            "body": "正文",
            "structuredPayload": _payload_for(CATEGORY_FACT),
            "changeReason": "基于别人那版",
            "baseVersionId": second["versionId"],
        },
        headers=_write_headers(),
    )
    assert response.status_code == 422


# --- 4.5 发布 --------------------------------------------------------------


def test_http_publish_activates(client: TestClient, env: KnowledgeEnv) -> None:
    """发布返回 `(publicationId, sequence, action, assetState, activeVersionId)`。"""
    _login(client, env, user_id=env.owner_id)
    created = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    ).json()["data"]
    response = client.post(
        f"/api/v1/knowledge/assets/{created['assetId']}/publications",
        json={"versionId": created["versionId"], "action": ACTION_ACTIVATE},
        headers=_write_headers(),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["sequence"] == 1
    assert data["action"] == ACTION_ACTIVATE
    assert data["activeVersionId"] == created["versionId"]
    # **写入侧与读取侧状态一致**（见 service `_project_asset` 的说明）。
    detail = client.get(
        f"/api/v1/knowledge/assets/{created['assetId']}"
    ).json()["data"]
    assert detail["state"] == data["assetState"]


def test_http_publish_supersede_is_rejected(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """`SUPERSEDE` 不在客户端可选动作里 → 422。

    它是 ACTIVATE 的**副作用**；让它可选会出现"当前生效版本被标成取代、
    但没有版本接过它"的空窗。
    """
    _login(client, env, user_id=env.owner_id)
    created = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    ).json()["data"]
    response = client.post(
        f"/api/v1/knowledge/assets/{created['assetId']}/publications",
        json={"versionId": created["versionId"], "action": ACTION_SUPERSEDE},
        headers=_write_headers(),
    )
    assert response.status_code == 422


def test_http_publish_duplicate_archive_is_conflict(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """重复归档 → 409（`INVALID_TRANSITION`）。"""
    _login(client, env, user_id=env.owner_id)
    created = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    ).json()["data"]
    asset_id, v1_id = created["assetId"], created["versionId"]
    client.post(
        f"/api/v1/knowledge/assets/{asset_id}/publications",
        json={"versionId": v1_id, "action": ACTION_ACTIVATE},
        headers=_write_headers(),
    )
    first = client.post(
        f"/api/v1/knowledge/assets/{asset_id}/publications",
        json={"action": ACTION_ARCHIVE},
        headers=_write_headers(),
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/v1/knowledge/assets/{asset_id}/publications",
        json={"action": ACTION_ARCHIVE},
        headers=_write_headers(),
    )
    assert second.status_code == 409


def test_http_publish_if_match_mismatch_412(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """`If-Match` 提供但**不匹配当前事件序号** → 412。

    本实现接收并校验它，但**不强制要求**（缺省放行）——
    见 api docstring 第二节的取舍。这里验"想防冲突时可以防"。
    """
    _login(client, env, user_id=env.owner_id)
    created = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    ).json()["data"]
    response = client.post(
        f"/api/v1/knowledge/assets/{created['assetId']}/publications",
        json={"versionId": created["versionId"], "action": ACTION_ACTIVATE},
        headers=_write_headers(if_match='W/"someone-else:999"'),
    )
    assert response.status_code == 412


def test_http_publish_without_if_match_is_allowed(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """**对照组**：不带 `If-Match` 必须放行（协议写它但语义不匹配，故不强制）。"""
    _login(client, env, user_id=env.owner_id)
    created = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    ).json()["data"]
    response = client.post(
        f"/api/v1/knowledge/assets/{created['assetId']}/publications",
        json={"versionId": created["versionId"], "action": ACTION_ACTIVATE},
        headers=_write_headers(),
    )
    assert response.status_code == 200


# --- 4.6 版本列表与引用 ----------------------------------------------------


def test_http_list_versions(client: TestClient, env: KnowledgeEnv) -> None:
    """历史版本列表带 `versionState` 投影状态。"""
    _login(client, env, user_id=env.owner_id)
    created = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    ).json()["data"]
    asset_id, v1_id = created["assetId"], created["versionId"]
    client.post(
        f"/api/v1/knowledge/assets/{asset_id}/publications",
        json={"versionId": v1_id, "action": ACTION_ACTIVATE},
        headers=_write_headers(),
    )
    response = client.get(f"/api/v1/knowledge/assets/{asset_id}/versions")
    assert response.status_code == 200
    items = response.json()["data"]
    assert items
    assert items[0]["versionState"] == VERSION_STATE_ACTIVE


def test_http_references_empty_for_unpublished(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """从未发布的知识 → 空引用页（不是 404）。

    "这条知识存在，但没人引用它"是一个**有效答案**。
    """
    _login(client, env, user_id=env.owner_id)
    created = client.post(
        "/api/v1/knowledge/assets", json=_create_body(env), headers=_write_headers()
    ).json()["data"]
    response = client.get(
        f"/api/v1/knowledge/assets/{created['assetId']}/references"
    )
    assert response.status_code == 200
    assert response.json()["data"] == []


def test_http_references_hides_invisible_project(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """**引用摘要受项目可见性过滤** —— 非成员的运营看不到别人项目的引用。

    这是"不能借此读无权项目正文"的接线验证。
    """
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    _link_reference(env, target_version_id=v1_id, project_id=env.project_id)

    # 另一个运营（不是本项目的成员）看不到这条引用。
    outsider = _create_user(env.settings, name="外部运营")
    _login(client, env, user_id=outsider)
    response = client.get(f"/api/v1/knowledge/assets/{asset_id}/references")
    assert response.status_code == 200
    assert response.json()["data"] == []


def test_http_references_visible_to_project_member(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """**对照组**：本项目的成员能看到它。"""
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    _link_reference(env, target_version_id=v1_id, project_id=env.project_id)

    _login(client, env, user_id=env.owner_id)
    response = client.get(f"/api/v1/knowledge/assets/{asset_id}/references")
    assert response.status_code == 200
    assert len(response.json()["data"]) == 1


def test_http_references_admin_sees_all(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """管理者不受项目过滤（与 WP11 的 `can_view_version` 同口径）。"""
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    _link_reference(env, target_version_id=v1_id, project_id=env.project_id)

    _login(client, env, user_id=env.admin_id)
    response = client.get(f"/api/v1/knowledge/assets/{asset_id}/references")
    assert response.status_code == 200
    assert len(response.json()["data"]) == 1


# --- 4.7 候选 --------------------------------------------------------------


def test_http_list_proposals(client: TestClient, env: KnowledgeEnv) -> None:
    """候选列表（默认可读，登录即通过）。"""
    proposal_id = _make_proposal(env)
    _login(client, env, user_id=env.owner_id)
    response = client.get(
        "/api/v1/knowledge/proposals", params={"projectId": str(env.project_id)}
    )
    assert response.status_code == 200
    # JSON 里的 id 是**字符串**，而 `_make_proposal` 返回 UUID ——
    # 不转字符串的话这条断言永远失败（形如 `UUID(...) in {"..."}` 恒为 False）。
    assert str(proposal_id) in {item["id"] for item in response.json()["data"]}


def test_http_decide_proposal_accept(client: TestClient, env: KnowledgeEnv) -> None:
    """ACCEPT 返回 `(state, createdVersionId, activated)` —— 真的产生了版本。"""
    asset_id, v1_id = _create_fact(env)
    _publish(env, asset_id=asset_id, action=ACTION_ACTIVATE, version_id=v1_id)
    proposal_id = _make_proposal(
        env,
        target_asset_id=asset_id,
        payload={
            "body": "候选正文",
            "structuredPayload": _payload_for(CATEGORY_FACT),
            "changeReason": "采纳候选",
        },
    )
    _login(client, env, user_id=env.owner_id)
    response = client.post(
        f"/api/v1/knowledge/proposals/{proposal_id}/decisions",
        json={"decision": DECISION_ACCEPT, "reason": "采纳"},
        headers=_write_headers(),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["state"] == PROPOSAL_STATE_ACCEPTED
    assert data["createdVersionId"]
    assert data["activated"] is True


def test_http_decide_proposal_reject_needs_reason(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """REJECT 缺理由 → 422。"""
    proposal_id = _make_proposal(env)
    _login(client, env, user_id=env.owner_id)
    response = client.post(
        f"/api/v1/knowledge/proposals/{proposal_id}/decisions",
        json={"decision": DECISION_REJECT, "reason": ""},
        headers=_write_headers(),
    )
    assert response.status_code == 422


def test_http_decide_proposal_twice_is_conflict(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """已处置的候选再决策 → 409。"""
    proposal_id = _make_proposal(env)
    _login(client, env, user_id=env.owner_id)
    client.post(
        f"/api/v1/knowledge/proposals/{proposal_id}/decisions",
        json={"decision": DECISION_REJECT, "reason": "不采纳"},
        headers=_write_headers(),
    )
    response = client.post(
        f"/api/v1/knowledge/proposals/{proposal_id}/decisions",
        json={"decision": DECISION_ACCEPT, "reason": "再试"},
        headers=_write_headers(),
    )
    assert response.status_code == 409


def test_http_decide_unknown_proposal_404(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """未知候选 → 404。"""
    _login(client, env, user_id=env.owner_id)
    response = client.post(
        f"/api/v1/knowledge/proposals/{uuid.uuid4()}/decisions",
        json={"decision": DECISION_REJECT, "reason": "不采纳"},
        headers=_write_headers(),
    )
    assert response.status_code == 404


def test_http_searches_endpoint_now_belongs_to_wp21(
    client: TestClient, env: KnowledgeEnv
) -> None:
    """**`POST /knowledge/searches` 由 WP21 交付** —— 本包当年刻意的空缺已闭合。

    本断言的前身是 `test_http_searches_endpoint_is_not_registered`，
    它当年断言这个端点**不存在**（404），并写明：

    > 这条断言把"划界"写成可执行的证据：若有人误加了这个端点，
    > 它会立刻变红，提醒"那属于 WP21 的设计空间"。

    **WP21 已交付该端点**（`modules/knowledge/search_api.py`），
    因此那条断言**按设计意图**变红 —— 它不是"被改坏"，
    而是完成了使命。这里把它改成"交接确认"：

      · 端点**已注册**（不再是 404）；
      · 它**不在本包的 router 里** —— 而是 WP21 单独的 `search_router`。
        这一点值得钉住：它证明交付方是 WP21 而不是 WP20 被顺手加了东西。

    **为什么不断言 200**：本包（WP20）的测试夹具不建
    `embedding_profiles`，因此检索会因"没有可用 profile"返回 404。
    200 的判据属于 WP21 的测试（`tests/test_knowledge_wp21.py`）。
    """
    _login(client, env, user_id=env.owner_id)
    response = client.post(
        "/api/v1/knowledge/searches",
        json={"query": "续航"},
        headers=_write_headers(),
    )
    # 关键：**不再是 404 的"路径不存在"**。若仍为 404，
    # 说明端点没注册（划界未闭合）。
    assert response.status_code != 404, (
        "WP21 交付后该端点必须已注册；404 说明它没被挂上。"
    )

    # 端点的归属：在 WP21 的 search_router 里，不在 WP20 的 router 里。
    from rednote.modules.knowledge import router as wp20_router
    from rednote.modules.knowledge import search_router as wp21_router

    wp20_paths = {getattr(route, "path", "") for route in wp20_router.routes}
    wp21_paths = {getattr(route, "path", "") for route in wp21_router.routes}
    assert "/knowledge/searches" not in wp20_paths, (
        "该端点不得被加进 WP20 的 router —— 它属于 WP21 的交付范围"
    )
    assert "/knowledge/searches" in wp21_paths


# --- 4.8 端点清单（与协议 §9 逐行对齐）--------------------------------------


@pytest.mark.regression
def test_router_exposes_exactly_nine_endpoints(app: FastAPI) -> None:
    """本包**恰好注册 9 个知识端点**（协议 §9 的 10 行减去 `searches`）。

    多一个少一个都要在这里显式面对 —— 数量对不上通常意味着
    "把别的包的端点写进了本包"或"漏了一个"。

    ## ⚠️ 判据范围：只数**本包 router 自己**声明的操作

    这里刻意**不数 `app.routes` 里所有 `/api/v1/knowledge*` 的操作**。
    那样写会在别的包合法地往同一路径段加端点时变红 —— 而那不是
    "本包多了一个或少了一个"，是**别人的包在工作**。

    **实测教训（判据纪律第 1 条第七次现身）**：WP23 加了
    `POST /knowledge/proposals`（闭合 OP-150 的候选**产生方**，
    与 WP20 的 `GET /knowledge/proposals` 同路径不同方法）之后，
    按 `app.routes` 计数的版本数到 **10** 个而红。
    症状是"WP20 的测试挂了"，真相是"WP23 交付了一个端点" ——
    **报错指向的地方不是出问题的地方**。

    本项目已在 ISS-44 / ISS-64 / ISS-71 踩过同一形态
    （历史证据只能断言"我建的还在"，不能断言"全项目恰好有 N 个"）。
    因此判据改成两句，各自成立：

      1. **本包 router 自己**恰好声明 9 个操作（本包的事实，稳定）；
      2. `app` 里**包含**这 9 个（接线正确，允许别人在同一段加端点）。
    """
    paths = {
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/v1/knowledge")
    }
    #: 本包自己的 7 条路径 —— 都必须在 app 里（`POST /knowledge/proposals`
    #: 由 WP23 提供，因此路径集合本身不受影响）。
    assert {
        "/api/v1/knowledge/assets",
        "/api/v1/knowledge/assets/{assetId}",
        "/api/v1/knowledge/assets/{assetId}/versions",
        "/api/v1/knowledge/assets/{assetId}/publications",
        "/api/v1/knowledge/assets/{assetId}/references",
        "/api/v1/knowledge/proposals",
        "/api/v1/knowledge/proposals/{proposalId}/decisions",
    } <= paths, f"本包的路径缺失：{paths}"

    # --- 判据 1：本包 router 自己恰好 9 个操作 ---------------------------
    from rednote.modules.knowledge.api import router as knowledge_router

    own_operations = {
        (route.path, method)
        for route in knowledge_router.routes
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }
    # `/knowledge/assets` 与 `.../versions` 各有 GET + POST 两个操作，
    # 合计 9 个操作。
    assert len(own_operations) == 9, sorted(own_operations)

    # --- 判据 2：这 9 个必须真的挂在 app 上（接线正确）-------------------
    mounted = {
        (route.path, method)
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/v1/knowledge")
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }
    # router 内部声明的是裸路径（不带 `/api/v1` 前缀），比对前补上前缀。
    prefix = "/api/v1"
    expected_mounted = {(f"{prefix}{p}", m) for p, m in own_operations}
    missing = expected_mounted - mounted
    assert not missing, f"本包的端点在 app 里找不到：{sorted(missing)}"


def test_write_endpoints_require_knowledge_manage(app: FastAPI) -> None:
    """三个写端点都挂了权限依赖（`KNOWLEDGE_WRITE`）。

    **这条只能在这一层测**：服务层测试直接调 `service`，
    一个忘写 `dependencies=KNOWLEDGE_WRITE` 的端点在那里完全看不出来。
    """
    write_paths = {
        "/api/v1/knowledge/assets",
        "/api/v1/knowledge/assets/{assetId}/versions",
        "/api/v1/knowledge/assets/{assetId}/publications",
        "/api/v1/knowledge/proposals/{proposalId}/decisions",
    }
    checked: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if path not in write_paths:
            continue
        if "POST" not in getattr(route, "methods", set()):
            continue
        deps = getattr(route, "dependencies", [])
        assert deps, f"{path} 的写操作没有挂权限依赖"
        checked.add(path)
    assert checked == write_paths, f"有写端点没被检查到：{write_paths - checked}"
