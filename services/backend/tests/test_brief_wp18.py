"""WP18 测试：Brief 结构化解构（分类来源 / 字段疑问 / 确认版本闭环）。

覆盖 WP18 验收原文（研发计划 04 §4.3）：
> **分类来源、字段疑问、确认版本闭环**

**测试策略（四层）**：

1. **纯函数层**（不碰数据库）：字段 Schema 的完整性判据、`ClaimDraft` 的
   "事实类必须有来源"、`IssueDraft` 的"处置必须留理由"、模型输出的降级
   处置、`claim_hash` 的口径、任务状态的 `PARTIAL` 独立映射。
   这些是"规则正确性"的权威测试。
2. **服务层 + 真库**：懒创建、解构的三分支（SUCCEEDED / PARTIAL / FAILED）、
   草稿更新的"只能确认不能新增"、固化的四道门、加厚建议不自动生效。
   WP18 的端点薄（只做校验 + 入队），业务规则全在服务层 ——
   因此**验收主要打在服务层**。
3. **数据库层兜底**：用**原生 SQL** 绕过 ORM 与 service，验证 CHECK、
   部分索引、不可变触发器、以及 **OP-88 的延迟一致性触发器**真的拦得住。
   这一层是本包最重要的测试：**OP-88 的闭合点就在本包**，
   而"延迟约束"若只测 ORM 路径，等于没测它"在提交时"才生效这个特性。
4. **HTTP 层**：五个端点的状态码、ETag 语义、幂等键、错误体形状。
   端点是薄层，因此这一层只覆盖"接线是否正确"，不重复服务层的判据。

**为什么第 3 层必须存在**：不可变保护与跨表不变量的设计动机就是
"绕过 ORM 的写入路径（psql / 数据回填 / 运维脚本）没有代码评审"。
若测试也只用 ORM，那么测试与实现共享同一个盲区。

---

## 本文件的建表机制（见接缝冻结 §4.1）

**不生成迁移文件**（那会让另一分支的 `alembic upgrade head` 撞上
"Multiple head revisions are present"），改为 module 级 fixture
用 `Base.metadata.create_all(engine, tables=[...])` 建**自己那五张表**，
再用 `ddl.all_trigger_ddl()` 建触发器（**必须 DROP → CREATE**：
`CREATE TRIGGER` 不是幂等的）。
"""

from __future__ import annotations

import hashlib
import io
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from rednote.modules.brief import ddl as brief_ddl
from rednote.modules.brief import schema as brief_schema
from rednote.modules.brief import service
from rednote.modules.brief.decomposer import (
    DecomposerUnavailable,
    ScriptedDecomposer,
    UnavailableDecomposer,
    decomposition_from_payload,
)
from rednote.modules.brief.handlers import (
    build_task_registry,
    outcome_for_source_state,
)
from rednote.modules.brief.models import (
    Brief,
    BriefClaim,
    BriefDraft,
    BriefIssue,
    BriefVersion,
)
from rednote.modules.brief.repository import BriefRepository
from rednote.modules.brief.types import (
    CATEGORY_EXTERNAL_EVIDENCE,
    CATEGORY_SOURCE_FACT,
    CATEGORY_STRATEGY,
    CATEGORY_UNCONFIRMED,
    CATEGORIES_REQUIRING_EVIDENCE,
    CLAIM_HASH_LENGTH,
    IDEMPOTENCY_SCOPE_BRIEF_PARSE,
    ISSUE_DISMISSED,
    ISSUE_OPEN,
    ISSUE_RESOLVED,
    KIND_BRIEF_ENRICH,
    KIND_BRIEF_PARSE,
    MAX_CLAIM_CHARS,
    MAX_ISSUES_PER_VERSION,
    QUEUE_BRIEF,
    RESOLUTION_CONFIRMED,
    RESOLUTION_NOT_APPLICABLE,
    RESOLUTION_PENDING,
    SEVERITY_BLOCK,
    SEVERITY_WARN,
    SOURCE_STATE_FAILED,
    SOURCE_STATE_NONE,
    SOURCE_STATE_PARTIAL,
    SOURCE_STATE_SUCCEEDED,
    ClaimDraft,
    DecompositionResult,
    EnrichmentResult,
    EnrichmentSuggestion,
    IssueDraft,
)
from rednote.modules.files.models import (
    CLASSIFICATION_SUPPORTED,
    PURPOSE_BRIEF,
    PURPOSE_RESEARCH,
    SCAN_STATUS_CLEAN,
    VISIBILITY_PROJECT,
    Blob,
    FileObject,
)
from rednote.modules.files.repository import FileRepository
from rednote.modules.files.storage import LocalStorageBackend, object_key_for
from rednote.modules.identity.models import ROLE_ADMIN, ROLE_OPERATOR, User
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.jobs.models import (
    STATE_PARTIAL,
    STATE_SUCCEEDED,
)
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.parsing.models import SourceSegment
from rednote.modules.parsing.repository import ParseRepository
from rednote.modules.parsing.service import PARSER_VERSION
from rednote.modules.parsing.types import (
    EXTRACTION_TEXT,
    PARSE_STATE_FAILED,
    PARSE_STATE_PARTIAL,
    PARSE_STATE_SUCCEEDED,
    SegmentPosition,
    derive_state,
)
from rednote.modules.projects.models import Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.modules.versions.models import ENTITY_TYPE_BRIEF, VersionNode
from rednote.modules.versions.repository import VersionRepository
from rednote.modules.versions import service as versions_service
from rednote.platform.configuration import Settings
from rednote.platform.database import get_engine, reset_engines, session_scope
from rednote.platform.errors import ApiError, ErrorCode
from rednote.platform.immutability import IMMUTABLE_RECORD_SQLSTATE

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"


def _test_database_url() -> str:
    """DSN 从环境变量取（分支用 `rednote_test_wp18`），缺省本机 postgres。"""
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
# 建表夹具（module 级，见模块 docstring）
# ===========================================================================


@pytest.fixture(scope="module", autouse=True)
def _ensure_own_tables() -> Iterator[None]:
    """只建本模块的五张表 + 七个触发器。

    **触发器必须 DROP → CREATE**：`create_all` 建表是幂等的，
    但 `CREATE TRIGGER` 不是（本机实测：直接 CREATE 会报
    `DuplicateObject: trigger "trg_..." for relation "..." already exists`）。

    `reset_engines()` 先跑：conftest 的 `_isolate_settings_cache` 是
    autouse 的 function 级 fixture，module 级 fixture 拿到的 engine
    可能已被上一轮清掉 —— 重取一次最稳。
    """
    reset_engines()
    settings = _make_settings()
    engine = get_engine(settings)

    Brief.metadata.create_all(
        engine,
        tables=[
            Brief.__table__,
            BriefDraft.__table__,
            BriefVersion.__table__,
            BriefClaim.__table__,
            BriefIssue.__table__,
        ],
    )

    with engine.begin() as conn:
        for (
            _table,
            func_ddl,
            trigger_ddl,
            drop_trigger,
            _drop_func,
        ) in brief_ddl.all_trigger_ddl():
            conn.execute(text(drop_trigger))  # 幂等关键
            conn.execute(text(func_ddl))
            conn.execute(text(trigger_ddl))
    yield


# ===========================================================================
# 数据夹具（自包含，不依赖 conftest 的隐式状态；G6：自开 session 并 commit）
# ===========================================================================


@dataclass
class BriefEnv:
    settings: Settings
    store: LocalStorageBackend
    project_id: uuid.UUID
    owner_id: uuid.UUID


def _create_user(settings: Settings, *, role: str = ROLE_OPERATOR) -> uuid.UUID:
    with session_scope(settings) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name="Brief 测试用户",
            role=role,
        )
        return user.id


def _seed_project(settings: Settings, *, owner_id: uuid.UUID) -> uuid.UUID:
    with session_scope(settings) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name="测试产品")
        session.add(product)
        session.flush()
        project = Project(
            project_code=f"BRF-{uuid.uuid4().hex[:8].upper()}",
            name="Brief 测试项目",
            brand_id=brand.id,
            product_id=product.id,
            owner_id=owner_id,
            target_delivery_count=10,
            due_date=date(2026, 12, 31),
        )
        session.add(project)
        session.flush()
        ProjectRepository(session).add_owner_and_collaborators(
            project.id, owner_id=owner_id, collaborator_ids=()
        )
        return project.id


@pytest.fixture
def env(tmp_path: Path) -> BriefEnv:
    reset_engines()
    root = tmp_path / "storage"
    settings = _make_settings(storage_local_root=str(root))
    store = LocalStorageBackend(root)
    owner = _create_user(settings)
    project_id = _seed_project(settings, owner_id=owner)
    return BriefEnv(
        settings=settings, store=store, project_id=project_id, owner_id=owner
    )


def _seed_file(
    env: BriefEnv,
    raw: bytes,
    *,
    purpose: str = PURPOSE_BRIEF,
    original_name: str = "brief.txt",
) -> uuid.UUID:
    """真的把文件写进存储并落库（blob + file_object + 用途关联）。

    `blob` 按 `(storage_backend, object_key)` 复用（WP12 的物理去重）：
    同一进程里造两次同样的字节会撞唯一约束，这里顺着设计复用已有 blob。
    """
    sha = hashlib.sha256(raw).hexdigest()
    key = object_key_for(sha)
    env.store.write_part(key=key, stream=io.BytesIO(raw))

    with session_scope(env.settings) as session:
        blob_id = session.execute(
            text(
                "select id from blobs where storage_backend = :backend"
                " and object_key = :key"
            ),
            {"backend": env.store.backend_name, "key": key},
        ).scalar_one_or_none()
        if blob_id is None:
            blob = Blob(
                storage_backend=env.store.backend_name,
                bucket=None,
                object_key=key,
                sha256=sha,
                byte_size=len(raw),
                mime_type="text/plain",
                scan_status=SCAN_STATUS_CLEAN,
            )
            session.add(blob)
            session.flush()
            blob_id = blob.id

        file_row = FileObject(
            blob_id=blob_id,
            original_name=original_name,
            visibility=VISIBILITY_PROJECT,
            project_id=env.project_id,
            classification=CLASSIFICATION_SUPPORTED,
            uploaded_by=env.owner_id,
        )
        session.add(file_row)
        session.flush()
        FileRepository(session).insert_link(
            project_id=env.project_id, file_id=file_row.id, purpose=purpose
        )
        return file_row.id


def _seed_run(
    env: BriefEnv,
    file_id: uuid.UUID,
    *,
    texts: tuple[str, ...] = ("甲产品卖点一", "乙产品卖点二", "丙产品卖点三"),
    total_units: int | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> uuid.UUID:
    """造一次**已到终态**的解析运行（含片段）。

    **状态由 `derive_state` 推导**（与 WP17 的 `finish_run` 同一条口径）：
    测试不能自报状态 —— 那正是 WP17 花最大力气防的事，
    而这里的测试夹具若自报状态，就会掩盖"本模块对状态的处理"这个被测对象。
    """
    with session_scope(env.settings) as session:
        parse_repo = ParseRepository(session)
        run = parse_repo.insert_run(
            file_id=file_id, job_id=None, parser_version=PARSER_VERSION
        )
        parse_repo.flush()
        run_id = run.id

        parse_repo.mark_running(run_id=run_id, now=parse_repo.now())
        parse_repo.insert_segments(
            run_id=run_id,
            segments=[
                (
                    SegmentPosition(kind="LINE", line=index).to_json(),
                    body,
                    hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    EXTRACTION_TEXT,
                    None,
                )
                for index, body in enumerate(texts, start=1)
            ],
        )
        resolved_total = total_units if total_units is not None else len(texts)
        parse_repo.finish_run(
            run_id=run_id,
            total_units=resolved_total,
            parsed_units=len(texts),
            errors=errors or [],
            ocr_engine="unavailable",
            now=parse_repo.now(),
            from_states=["RUNNING"],
        )
    return run_id


def _segment_ids(env: BriefEnv, run_id: uuid.UUID) -> list[uuid.UUID]:
    with session_scope(env.settings) as session:
        return [row.id for row in ParseRepository(session).list_segments(run_id)]


def _brief_of(env: BriefEnv) -> uuid.UUID:
    with session_scope(env.settings) as session:
        brief = service.ensure_brief(
            BriefRepository(session), project_id=env.project_id
        )
        return brief.id


def _scripted(
    *,
    fields: dict[str, Any] | None = None,
    claims: tuple[ClaimDraft, ...] = (),
    issues: tuple[IssueDraft, ...] = (),
) -> ScriptedDecomposer:
    return ScriptedDecomposer(
        result=DecompositionResult(
            fields=fields or {}, claims=claims, issues=issues
        )
    )


def _full_fields(**overrides: Any) -> dict[str, Any]:
    """一份**必填字段全部满足**的字段集（固化的前置条件）。

    `content.must_not_mention` 是必填项且语义上"总得写点什么"，
    因此默认给一个真实值而不是空数组 —— 需求 §5.1 要求"必须避免的
    表达方式"，一份没有任何禁提项的 Brief 本身就是一个需要被追问的状态。
    """
    fields = brief_schema.empty_field_values()
    for path, value in {
        "product.name": "测试产品",
        "product.brand": "测试品牌",
        "product.selling_points": ["卖点一", "卖点二", "卖点三"],
        "audience.target": "25-35 岁通勤人群",
        "content.must_not_mention": ["最", "第一"],
        **overrides,
    }.items():
        fields[path] = {"status": "FILLED", "value": value}
    return fields


def _raw(sql: str, params: dict[str, Any] | None = None) -> None:
    """用原生 SQL 执行（模拟绕过 ORM 的写入路径）。"""
    with session_scope(_make_settings()) as session:
        session.execute(text(sql), params or {})


# ===========================================================================
# 第 1 层：纯函数
# ===========================================================================


# --- 字段 Schema -----------------------------------------------------------


def test_brief_fields_cover_the_ten_required_groups() -> None:
    """需求 §5.1 的 10 组字段都必须有落点。

    分组用 `FieldSpec.group`（**编号 1~10**，与需求 §5.1 的十条一一对应）。
    这条断言防的是"新增字段时漏掉某一组" —— 而漏掉的表现是
    **那一组内容无处可填**，运营只能塞进别的字段里，
    于是 Brief 的结构化形同虚设。

    期望值取自 `schema.REQUIRED_GROUPS`（而非硬编码 `range(1, 11)`）：
    它同时被"字段清单必须覆盖 10 组"这条断言使用，两处共用一份期望值，
    改需求时不会出现"测试更新了、常量没更新"。
    """
    groups = {spec.group for spec in brief_schema.BRIEF_FIELDS}
    assert groups == brief_schema.REQUIRED_GROUPS


def test_every_group_has_at_least_one_field() -> None:
    """每个编号都真的有字段（防"编号跳号"）。"""
    assert len(brief_schema.REQUIRED_GROUPS) == 10
    assert brief_schema.REQUIRED_GROUPS == frozenset(range(1, 11))


def test_every_required_field_path_exists_in_schema() -> None:
    """必填集合必须是字段清单的子集（拼错一个路径会让必填校验永远不通过）。"""
    known = {spec.path for spec in brief_schema.BRIEF_FIELDS}
    assert set(brief_schema.REQUIRED_FIELD_PATHS) <= known


def test_field_paths_are_unique() -> None:
    paths = [spec.path for spec in brief_schema.BRIEF_FIELDS]
    assert len(paths) == len(set(paths))


def test_empty_field_values_marks_every_field_missing() -> None:
    """空草稿里**每个字段都在**，状态是 MISSING。

    "这个字段我们知道要问，但原文里没有"与"这个字段不在 Schema 里"
    是两件事 —— 前者要被显示为"待补充"（需求 §5.1），后者不该出现。
    """
    values = brief_schema.empty_field_values()
    assert set(values) == {spec.path for spec in brief_schema.BRIEF_FIELDS}
    assert all(entry["status"] == "MISSING" for entry in values.values())


def test_field_spec_rejects_unknown_path() -> None:
    """未知路径**显式报错**，不返回 None。

    返回 None 会让调用方写 `if spec is None: 跳过` —— 而那个分支的
    实际效果是"未知字段被静默接受"，正是解构跑偏最难查的形态。
    """
    with pytest.raises(ValueError):
        brief_schema.field_spec("product.nonexistent")


def test_normalize_accepts_bare_values() -> None:
    """裸值（`{"product.name": "某产品"}`）按"有值即 FILLED"归一。"""
    normalized = brief_schema.normalize_field_values({"product.name": "某产品"})
    assert normalized["product.name"] == {"status": "FILLED", "value": "某产品"}


def test_normalize_rejects_filled_with_empty_value() -> None:
    """标 FILLED 但值为空 → 拒绝。

    状态与值必须一致：只清值不清状态会让固化校验基于错误的完整度放行，
    于是一个**内容为空的字段**通过了"必填已填写"这道门。
    """
    with pytest.raises(ValueError):
        brief_schema.normalize_field_values(
            {"product.name": {"status": "FILLED", "value": ""}}
        )


def test_normalize_rejects_missing_with_value() -> None:
    with pytest.raises(ValueError):
        brief_schema.normalize_field_values(
            {"product.name": {"status": "MISSING", "value": "有值"}}
        )


def test_not_applicable_requires_reason() -> None:
    """标"不适用"必须填原因（需求 §5.1 原文"并填写原因"）。"""
    with pytest.raises(ValueError):
        brief_schema.normalize_field_values(
            {"product.model_code": {"status": "NOT_APPLICABLE", "value": ""}}
        )


def test_multi_field_rejects_scalar_value() -> None:
    """多值字段必须是数组 —— 允许标量会让下游为两种形状写分支。"""
    with pytest.raises(ValueError):
        brief_schema.normalize_field_values(
            {"product.selling_points": {"status": "FILLED", "value": "单个卖点"}}
        )


def test_missing_required_fields_counts_not_applicable_as_settled() -> None:
    """`NOT_APPLICABLE` 算已处置（需求 §5.3："已填写**或明确不适用**"）。"""
    fields = _full_fields()
    fields["product.brand"] = {"status": "NOT_APPLICABLE", "value": "该产品无品牌"}
    assert brief_schema.missing_required_fields(fields) == ()


def test_missing_required_fields_lists_unfilled_paths() -> None:
    fields = brief_schema.empty_field_values()
    missing = brief_schema.missing_required_fields(fields)
    assert set(missing) == set(brief_schema.REQUIRED_FIELD_PATHS)


def test_selling_point_summary_returns_up_to_three() -> None:
    fields = _full_fields(
        **{"product.selling_points": ["一", "二", "三", "四", "五"]}
    )
    assert brief_schema.selling_point_summary(fields) == ("一", "二", "三")


def test_selling_point_summary_is_honest_when_fewer_than_three() -> None:
    """不足三项时**如实返回不足**，不补空串、不凑数（需求 §5.3）。"""
    fields = _full_fields(**{"product.selling_points": ["只有一个"]})
    assert brief_schema.selling_point_summary(fields) == ("只有一个",)


def test_selling_point_summary_accepts_priority_objects() -> None:
    """卖点可以是裸串或 `{"text":…, "priority":…}`（需求 §5.1 第 5 组）。"""
    fields = _full_fields(
        **{
            "product.selling_points": [
                {"text": "续航长", "priority": 1},
                {"text": "轻", "priority": 2},
            ]
        }
    )
    assert brief_schema.selling_point_summary(fields) == ("续航长", "轻")


# --- ClaimDraft / IssueDraft 的构造校验（"分类来源"的第一层）----------------


@pytest.mark.parametrize("category", sorted(CATEGORIES_REQUIRING_EVIDENCE))
def test_factual_claim_without_evidence_is_rejected(category: str) -> None:
    """事实类（SOURCE_FACT / EXTERNAL_EVIDENCE）**必须有来源**。

    协议 §5.1 原句："没有来源的建议不能改成 SOURCE_FACT 而不补证据"。
    这条在 `ClaimDraft.__post_init__` 与数据库 CHECK 两层都有 ——
    这一层给可读错误，数据库那层管住绕过构造器的写入。
    """
    with pytest.raises(ValueError):
        ClaimDraft(field_path="product.name", text="某产品", category=category)


def test_strategy_claim_needs_no_evidence() -> None:
    """策略类**不需要**来源：它是"我们打算怎么做"，不是"原文说了什么"。"""
    claim = ClaimDraft(
        field_path="content.hashtag_examples", text="走通勤场景", category=CATEGORY_STRATEGY
    )
    assert claim.evidence_segment_id is None


def test_claim_rejects_unknown_category() -> None:
    with pytest.raises(ValueError):
        ClaimDraft(field_path="product.name", text="x", category="MADE_UP")


def test_claim_rejects_empty_text() -> None:
    with pytest.raises(ValueError):
        ClaimDraft(field_path="product.name", text="   ", category=CATEGORY_STRATEGY)


def test_claim_rejects_overlong_text() -> None:
    with pytest.raises(ValueError):
        ClaimDraft(
            field_path="product.name",
            text="x" * (MAX_CLAIM_CHARS + 1),
            category=CATEGORY_STRATEGY,
        )


def test_claim_evidence_url_satisfies_evidence_requirement() -> None:
    """外部证据可以用 URL 而不是片段。"""
    claim = ClaimDraft(
        field_path="product.efficacy",
        text="有临床数据支持",
        category=CATEGORY_EXTERNAL_EVIDENCE,
        evidence_url="https://example.invalid/study",
    )
    assert claim.evidence_url == "https://example.invalid/study"


def test_issue_rejects_settled_state_without_resolution() -> None:
    """处置疑问必须留理由 —— 没有理由的"已解决"无法复核。"""
    with pytest.raises(ValueError):
        IssueDraft(
            id=uuid.uuid4(),
            field_path="product.name",
            severity=SEVERITY_WARN,
            description="缺产品名",
            state=ISSUE_RESOLVED,
        )


def test_issue_blocking_property_only_for_open_block() -> None:
    blocking = IssueDraft(
        id=uuid.uuid4(),
        field_path="content.must_not_mention",
        severity=SEVERITY_BLOCK,
        description="禁提项未找到",
    )
    assert blocking.blocking is True

    settled = IssueDraft(
        id=uuid.uuid4(),
        field_path="content.must_not_mention",
        severity=SEVERITY_BLOCK,
        description="禁提项未找到",
        state=ISSUE_RESOLVED,
        resolution="已人工确认无禁提项",
    )
    assert settled.blocking is False


# --- 模型输出的降级处置（不可信输入）--------------------------------------


def test_unknown_field_path_becomes_warning_issue() -> None:
    """未知字段路径 → 降级成 WARN 疑问（**不是** 500，也不是静默丢弃）。

    静默丢弃会让"模型想填一个 Schema 里没有的字段"永远不可见。
    """
    result = decomposition_from_payload(
        {
            "fields": {"product.name": {"status": "FILLED", "value": "某产品"}},
            "claims": [{"fieldPath": "not.a.field", "text": "x", "category": "STRATEGY"}],
        }
    )
    assert result.claims == ()
    assert any(issue.severity == SEVERITY_WARN for issue in result.issues)


def test_factual_claim_without_evidence_degrades_to_unconfirmed() -> None:
    """事实类缺证据 → 降级成 UNCONFIRMED + 一条 WARN（不是静默接受）。"""
    result = decomposition_from_payload(
        {
            "claims": [
                {
                    "fieldPath": "product.name",
                    "text": "某产品",
                    "category": CATEGORY_SOURCE_FACT,
                }
            ]
        }
    )
    assert len(result.claims) == 1
    assert result.claims[0].category == CATEGORY_UNCONFIRMED
    assert any(issue.severity == SEVERITY_WARN for issue in result.issues)


def test_claim_missing_required_key_is_ignored_with_warning() -> None:
    result = decomposition_from_payload({"claims": [{"fieldPath": "product.name"}]})
    assert result.claims == ()
    assert any(issue.severity == SEVERITY_WARN for issue in result.issues)


def test_non_object_claim_is_ignored_with_warning() -> None:
    result = decomposition_from_payload({"claims": ["这不是对象"]})
    assert result.claims == ()
    assert len(result.issues) == 1


def test_decomposition_result_has_no_self_reported_state() -> None:
    """`DecompositionResult` **没有 `state`** —— 与 WP17 的 `ParseOutcome` 同理。

    模型若能自报"我成功了"，"不虚报拆解成功"就只剩自觉。
    """
    assert not hasattr(DecompositionResult(), "state")


# --- claim_hash ------------------------------------------------------------


def test_claim_hash_is_64_hex() -> None:
    digest = service.claim_hash(field_path="product.name", text="某产品")
    assert len(digest) == CLAIM_HASH_LENGTH
    assert all(char in "0123456789abcdef" for char in digest)


def test_claim_hash_is_stable() -> None:
    assert service.claim_hash(
        field_path="product.name", text="某产品"
    ) == service.claim_hash(field_path="product.name", text="某产品")


def test_claim_hash_depends_on_field_path() -> None:
    """同文本不同字段必须算出不同摘要。

    唯一约束是 `UQ(brief_version_id, field_path, claim_hash)`，
    而"同字段同文本"才是重复的判据。只哈希文本会让两处代码对
    "重复"的定义分歧，而分歧的方向恰好是"本该保留的结论被唯一约束挡下"。
    """
    assert service.claim_hash(
        field_path="product.name", text="温和"
    ) != service.claim_hash(field_path="product.efficacy", text="温和")


# --- 任务状态映射（PARTIAL 独立）------------------------------------------


def test_outcome_for_source_state_maps_partial_separately() -> None:
    """**PARTIAL 有独立映射**（WP17 对下游的硬要求）。

    映射到 `SUCCEEDED` 会让"输入缺了 12 页"在任务层消失；
    映射到 `FAILED` 会让它可重试，而重试不会让缺的页长出来。
    """
    assert outcome_for_source_state(SOURCE_STATE_SUCCEEDED).state == STATE_SUCCEEDED
    assert outcome_for_source_state(SOURCE_STATE_PARTIAL).state == STATE_PARTIAL


def test_outcome_for_source_state_rejects_unmappable() -> None:
    """非 SUCCEEDED/PARTIAL 一律显式失败 —— 不猜一个状态。"""
    with pytest.raises(ValueError):
        outcome_for_source_state(SOURCE_STATE_FAILED)


def test_task_registry_registers_both_kinds() -> None:
    kinds = build_task_registry().registered_kinds()
    assert kinds == {KIND_BRIEF_PARSE, KIND_BRIEF_ENRICH}


def test_default_decomposer_is_unavailable() -> None:
    """默认解构通道**调用即抛** —— 生产要走网关，没有本地降级（ADR-10）。"""
    with pytest.raises(DecomposerUnavailable):
        UnavailableDecomposer().decompose(
            project_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            segments=(),
            model_profile_id=None,
            job_id=None,
            attempt_id=None,
        )


def test_gateway_decomposer_requires_job_and_attempt_ids() -> None:
    """**生产装配的解构器必须先有 `jobId` / `attemptId`**，否则调用即拒。

    这条守的是"解构不由 HTTP 请求触发"这个结构性约束：网关要写调用留痕
    （`provider_calls.job_id` / `job_attempt_id`），没有它们就只能拒绝。
    若这一条被放宽（例如允许 `job_id=None` 静默跳过留痕），
    那么一次出网调用会在**没有任何记录**的情况下发生 ——
    而"谁在什么时候把哪份原文送出去了"正是审计要回答的问题。

    **本机跑不通真实调用**（缺已审批的 `model_profiles` / 出网策略 /
    真实适配器，OP-130 未关闭），因此这里只验证**可测的那条拒绝分支**。
    真实端到端已登记为内网补做项。
    """
    from rednote.modules.brief.decomposer import GatewayDecomposer

    decomposer = GatewayDecomposer(
        gateway_repo=object(),
        adapter=object(),
        model_profile_id=uuid.uuid4(),
    )
    with pytest.raises(DecomposerUnavailable) as caught:
        decomposer.decompose(
            project_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            segments=(),
            model_profile_id=None,
            job_id=None,  # ← 没有任务上下文
            attempt_id=None,
        )
    assert "jobId" in str(caught.value) or "attemptId" in str(caught.value)


# --- 触发器命名与清单 ------------------------------------------------------


def test_expected_triggers_are_declared() -> None:
    """七个触发器：3 个不可变 + 4 个一致性（含 OP-88 的两个）。"""
    names = brief_ddl.trigger_names()
    assert set(brief_ddl.immutable_trigger_names()) == {
        "trg_brief_versions_immutable",
        "trg_brief_claims_immutable",
        "trg_brief_issues_immutable",
    }
    assert brief_ddl.BRIEF_NODE_TRIGGER in names
    assert brief_ddl.BRIEF_VERSION_TRIGGER in names
    assert len(names) == 7


@pytest.mark.cross_wp
def test_trigger_ddl_targets_shared_version_nodes_table() -> None:
    """**OP-88 的反向判据挂在共用表 `version_nodes` 上** —— 主 agent 落迁移时
    必须一并建出，否则 OP-88 只闭合了一半（判据 1~3 有、反向判据没有）。
    """
    tables = {item[0] for item in brief_ddl.all_trigger_ddl()}
    assert "version_nodes" in tables
    assert {"brief_versions", "brief_claims", "brief_issues"} <= tables


@pytest.mark.cross_wp
def test_node_paired_trigger_is_scoped_to_brief_entity_type() -> None:
    """反向判据的触发器**必须**被 `WHEN (NEW.entity_type = 'BRIEF')` 限定。

    `version_nodes` 是全系统共用的表（8 类实体）。不加这个条件，
    每次 `CONTENT` / `PLAN` 版本注册都会白跑一次本模块的函数 ——
    而本包只对 `BRIEF` 有校验能力（其余 6 类的领域表还不存在）。
    """
    _func, trigger_ddl, _drop_t, _drop_f = brief_ddl.brief_node_paired_ddl()
    assert "WHEN (NEW.entity_type = 'BRIEF')" in trigger_ddl


def test_all_consistency_triggers_are_deferred() -> None:
    """跨表一致性触发器**必须** `DEFERRABLE INITIALLY DEFERRED`。

    数据结构 02 §5 要求"注册表、具体表及引用**同一事务**插入"，
    而 WP11 的 `register_version` 内部用 SAVEPOINT + 重试取修订号 ——
    它不暴露"节点行何时落库"的控制权。延迟到提交时校验，
    让"谁先谁后"不再是一个必须猜对的问题。
    """
    for builder in (
        brief_ddl.brief_version_consistency_ddl,
        brief_ddl.brief_node_paired_ddl,
        brief_ddl.claim_evidence_scope_ddl,
        brief_ddl.issue_scope_ddl,
    ):
        _func, trigger_ddl, _drop_t, _drop_f = builder()
        assert "CREATE CONSTRAINT TRIGGER" in trigger_ddl
        assert "DEFERRABLE INITIALLY DEFERRED" in trigger_ddl


# ===========================================================================
# 第 2 层：服务层 + 真库
# ===========================================================================


# --- 懒创建 ----------------------------------------------------------------


def test_ensure_brief_is_idempotent(env: BriefEnv) -> None:
    """重复调用只建一个 Brief 与一个草稿（`UQ` 兜底并发）。"""
    first = _brief_of(env)
    second = _brief_of(env)
    assert first == second

    with session_scope(env.settings) as session:
        drafts = (
            session.execute(
                text("select count(*) from brief_drafts where brief_id = :id"),
                {"id": first},
            ).scalar_one()
        )
    assert drafts == 1


def test_ensure_brief_creates_draft_with_all_fields_missing(env: BriefEnv) -> None:
    brief_id = _brief_of(env)
    with session_scope(env.settings) as session:
        draft = BriefRepository(session).get_draft_for_brief(brief_id)
        assert draft is not None
        assert set(draft.structured_fields) == {
            spec.path for spec in brief_schema.BRIEF_FIELDS
        }
        assert draft.source_state == SOURCE_STATE_NONE


def test_ensure_brief_rejects_unknown_project(env: BriefEnv) -> None:
    with session_scope(env.settings) as session:
        with pytest.raises(ApiError) as caught:
            service.ensure_brief(BriefRepository(session), project_id=uuid.uuid4())
    assert caught.value.code == ErrorCode.VALIDATION_FAILED


def test_draft_etag_is_built_from_lock_version(env: BriefEnv) -> None:
    brief_id = _brief_of(env)
    with session_scope(env.settings) as session:
        draft = BriefRepository(session).get_draft_for_brief(brief_id)
        assert draft is not None
        assert service.draft_etag(draft) == f'"{draft.id}:{draft.lock_version}"'


# --- request_parse（复用判据来自 WP17）------------------------------------


def test_request_parse_reuses_terminal_run(env: BriefEnv) -> None:
    """同一文件同解析器版本的终态 run 会被复用（数据结构 02 §4）。"""
    file_id = _seed_file(env, b"brief body\n")
    run_id = _seed_run(env, file_id)

    with session_scope(env.settings) as session:
        parse_repo = ParseRepository(session)
        file_repo = FileRepository(session)
        run, created = service.request_parse(
            parse_repo,
            file_repo,
            project_id=env.project_id,
            file_id=file_id,
        )
        assert created is False
        assert run.id == run_id


def test_request_parse_creates_run_when_none_exists(env: BriefEnv) -> None:
    file_id = _seed_file(env, b"fresh brief\n")
    with session_scope(env.settings) as session:
        run, created = service.request_parse(
            ParseRepository(session),
            FileRepository(session),
            project_id=env.project_id,
            file_id=file_id,
        )
        assert created is True
        assert run.state == "QUEUED"


def test_request_parse_rejects_file_of_other_purpose(env: BriefEnv) -> None:
    """研究资料**不能**当 Brief 解构 —— 否则无关内容会被灌进 Brief 字段。"""
    file_id = _seed_file(env, b"research\n", purpose=PURPOSE_RESEARCH)
    with session_scope(env.settings) as session:
        with pytest.raises(ApiError) as caught:
            service.request_parse(
                ParseRepository(session),
                FileRepository(session),
                project_id=env.project_id,
                file_id=file_id,
            )
    assert caught.value.code == ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == "file_purpose_not_brief"


def test_request_parse_rejects_unknown_file(env: BriefEnv) -> None:
    with session_scope(env.settings) as session:
        with pytest.raises(ApiError) as caught:
            service.request_parse(
                ParseRepository(session),
                FileRepository(session),
                project_id=env.project_id,
                file_id=uuid.uuid4(),
            )
    assert caught.value.details["reason"] == "file_not_found"


def test_request_parse_rejects_cross_project_file(env: BriefEnv) -> None:
    """别的项目的文件 → 422（不是 404：它是被引用的字段值）。"""
    other_owner = _create_user(env.settings)
    other_project = _seed_project(env.settings, owner_id=other_owner)
    other_env = BriefEnv(
        settings=env.settings,
        store=env.store,
        project_id=other_project,
        owner_id=other_owner,
    )
    file_id = _seed_file(other_env, b"other project\n")

    with session_scope(env.settings) as session:
        with pytest.raises(ApiError) as caught:
            service.request_parse(
                ParseRepository(session),
                FileRepository(session),
                project_id=env.project_id,
                file_id=file_id,
            )
    assert caught.value.details["reason"] == "file_project_mismatch"


# --- execute_decomposition：三分支 ----------------------------------------


def _decompose(
    env: BriefEnv,
    *,
    run_id: uuid.UUID,
    decomposer: ScriptedDecomposer,
) -> service.DecompositionOutcome:
    brief_id = _brief_of(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        return service.execute_decomposition(
            repo,
            ParseRepository(session),
            brief=brief,
            parse_run_id=run_id,
            decomposer=decomposer,
        )


def test_decomposition_writes_fields_and_claims(env: BriefEnv) -> None:
    file_id = _seed_file(env, "完整文档\n".encode("utf-8"))
    run_id = _seed_run(env, file_id)
    segment_ids = _segment_ids(env, run_id)

    outcome = _decompose(
        env,
        run_id=run_id,
        decomposer=_scripted(
            fields={"product.name": {"status": "FILLED", "value": "某产品"}},
            claims=(
                ClaimDraft(
                    field_path="product.name",
                    text="某产品",
                    category=CATEGORY_SOURCE_FACT,
                    evidence_segment_id=segment_ids[0],
                ),
            ),
        ),
    )

    assert outcome.source_state == SOURCE_STATE_SUCCEEDED
    assert outcome.claim_count == 1
    with session_scope(env.settings) as session:
        draft = BriefRepository(session).get_draft_for_brief(_brief_of(env))
        assert draft is not None
        assert draft.structured_fields["product.name"]["value"] == "某产品"
        assert draft.source_parse_run_id == run_id
        assert draft.source_state == SOURCE_STATE_SUCCEEDED


def test_decomposition_on_failed_run_is_rejected(env: BriefEnv) -> None:
    """**FAILED 不得消费** —— 一个 unit 都没解出来时，调模型只会让它编造。"""
    file_id = _seed_file(env, "坏文档\n".encode("utf-8"))
    run_id = _seed_run(
        env,
        file_id,
        texts=(),
        total_units=None,
        errors=[{"unit": 1, "code": "MALFORMED_DOCUMENT", "message": "坏"}],
    )
    with session_scope(env.settings) as session:
        run = ParseRepository(session).get_run(run_id)
        assert run is not None
        assert run.state == PARSE_STATE_FAILED

    decomposer = _scripted()
    with pytest.raises(ApiError) as caught:
        _decompose(env, run_id=run_id, decomposer=decomposer)
    assert caught.value.code == ErrorCode.INVALID_TRANSITION
    assert caught.value.details["reason"] == "parse_run_not_consumable"
    # **模型一次都没被调** —— 这才是"不得消费"的实质。
    assert decomposer.decompose_call_count == 0


def test_decomposition_on_pending_run_is_conflict_not_failure(env: BriefEnv) -> None:
    """还没跑完 → 409（**不是** FAILED：报 FAILED 会让任务框架标成不可重试）。"""
    file_id = _seed_file(env, "排队中的文档\n".encode("utf-8"))
    with session_scope(env.settings) as session:
        parse_repo = ParseRepository(session)
        run = parse_repo.insert_run(
            file_id=file_id, job_id=None, parser_version=PARSER_VERSION
        )
        parse_repo.flush()
        run_id = run.id

    with pytest.raises(ApiError) as caught:
        _decompose(env, run_id=run_id, decomposer=_scripted())
    assert caught.value.code == ErrorCode.INVALID_TRANSITION
    assert caught.value.details["reason"] == "parse_run_pending"


def test_partial_run_is_consumed_with_missing_units_as_warnings(env: BriefEnv) -> None:
    """**`PARTIAL` 的显式处理**（本包最要紧的一条）。

    PARTIAL **照常解构**（残缺文档仍有可用内容，拒绝它等于让运营白传一次），
    但**把缺失变成疑问**（逐 unit 一条 `WARN`）+ `source_state='PARTIAL'`。
    真正的风险是"残缺被当成完整"，所以缺失必须成为一条可查的记录。
    """
    file_id = _seed_file(env, "缺页文档\n".encode("utf-8"))
    run_id = _seed_run(
        env,
        file_id,
        texts=("第一页内容", "第二页内容"),
        total_units=4,
        errors=[
            {"unit": 3, "code": "UNIT_FAILED", "message": "第 3 页解不出"},
            {"unit": 4, "code": "UNIT_TIMEOUT", "message": "第 4 页超时"},
        ],
    )
    with session_scope(env.settings) as session:
        run = ParseRepository(session).get_run(run_id)
        assert run is not None
        assert run.state == PARSE_STATE_PARTIAL

    outcome = _decompose(
        env,
        run_id=run_id,
        decomposer=_scripted(
            fields={"product.name": {"status": "FILLED", "value": "某产品"}}
        ),
    )

    assert outcome.source_state == SOURCE_STATE_PARTIAL
    assert outcome.missing_unit_count == 2

    with session_scope(env.settings) as session:
        draft = BriefRepository(session).get_draft_for_brief(_brief_of(env))
        assert draft is not None
        assert draft.source_state == SOURCE_STATE_PARTIAL
        # 缺失变成疑问，且**不阻断**（WARN 而非 BLOCK —— 否则运营唯一出路是伪造）。
        warnings = [
            issue
            for issue in draft.issue_resolutions
            if issue["severity"] == SEVERITY_WARN
        ]
        assert len(warnings) == 1
        assert "3" in warnings[0]["description"]
        assert "4" in warnings[0]["description"]
        # `source_errors` 原样带上 WP17 的错误明细（排障依据）。
        assert len(draft.source_errors) == 2


def test_partial_without_error_details_falls_back_to_position_scan(env: BriefEnv) -> None:
    """`errors` 为空但状态是 PARTIAL 时，用"片段覆盖的 unit"补差额。

    宁可给一个粗糙的列表，也不要让"缺了东西"在库里只体现为一个没有明细的
    状态值（"缺 2 页"运营没法判断要不要紧，"缺的是第 3、4 页"才可以）。
    """
    file_id = _seed_file(env, "缺明细文档\n".encode("utf-8"))
    run_id = _seed_run(
        env,
        file_id,
        texts=("只解出一行",),
        total_units=3,
        errors=[],
    )
    with session_scope(env.settings) as session:
        run = ParseRepository(session).get_run(run_id)
        assert run is not None
        assert run.state == PARSE_STATE_PARTIAL

    outcome = _decompose(env, run_id=run_id, decomposer=_scripted())
    assert outcome.source_state == SOURCE_STATE_PARTIAL
    assert outcome.missing_unit_count >= 1


def test_decomposition_refuses_to_overwrite_unsolidified_edits(env: BriefEnv) -> None:
    """草稿里有未固化编辑时**拒绝重新解构** —— 不静默丢弃运营的工作。

    不做成"合并"的理由：合并需要一条"哪个字段以谁为准"的规则，
    而任何这样的规则在"模型填 A、运营改 B、重新解构后模型又填 A"时
    都会给出让人意外的结果。**拒绝并说清楚**比"猜一个合并策略"负责。
    """
    file_id = _seed_file(env, "第一次解构\n".encode("utf-8"))
    run_id = _seed_run(env, file_id)
    _decompose(
        env,
        run_id=run_id,
        decomposer=_scripted(
            fields={"product.name": {"status": "FILLED", "value": "初版产品"}}
        ),
    )

    # 运营改了一个字段（未固化）。
    brief_id = _brief_of(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        draft = service.require_draft(repo, brief=brief)
        service.update_draft(
            repo,
            brief=brief,
            base_version_id=None,
            fields={"product.name": {"status": "FILLED", "value": "运营改过的产品"}},
            expected_lock_version=draft.lock_version,
            actor_id=env.owner_id,
        )

    with pytest.raises(ApiError) as caught:
        _decompose(env, run_id=run_id, decomposer=_scripted())
    assert caught.value.code == ErrorCode.INVALID_TRANSITION
    assert caught.value.details["reason"] == "draft_has_unsolidified_edits"


def test_decomposition_truncates_claims_and_records_issue(env: BriefEnv) -> None:
    """超过条数上限时**截断并记一条疑问**，不静默丢。

    静默截断会让运营以为"就这么多结论"；记一条疑问至少让
    "模型产出了 N 条、系统只保留前 M 条"可见。
    """
    from rednote.modules.brief.types import MAX_CLAIMS_PER_VERSION as limit

    file_id = _seed_file(env, "跑偏的文档\n".encode("utf-8"))
    run_id = _seed_run(env, file_id)
    segment_ids = _segment_ids(env, run_id)
    many = tuple(
        ClaimDraft(
            field_path="product.selling_points",
            text=f"卖点 {index}",
            category=CATEGORY_SOURCE_FACT,
            evidence_segment_id=segment_ids[0],
        )
        for index in range(limit + 5)
    )

    outcome = _decompose(env, run_id=run_id, decomposer=_scripted(claims=many))
    assert outcome.claim_count == limit
    assert outcome.issue_count >= 1


# --- update_draft：只能确认，不能发明 --------------------------------------


def _seed_decomposed_draft(env: BriefEnv) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """解构一次，得到一份有 claim 与 BLOCK 疑问的草稿。"""
    file_id = _seed_file(env, f"草稿基础 {uuid.uuid4().hex}\n".encode())
    run_id = _seed_run(env, file_id)
    segment_ids = _segment_ids(env, run_id)
    _decompose(
        env,
        run_id=run_id,
        decomposer=_scripted(
            fields={"product.name": {"status": "FILLED", "value": "某产品"}},
            claims=(
                ClaimDraft(
                    field_path="product.name",
                    text="某产品",
                    category=CATEGORY_SOURCE_FACT,
                    evidence_segment_id=segment_ids[0],
                ),
            ),
            issues=(
                IssueDraft(
                    id=uuid.uuid4(),
                    field_path="content.must_not_mention",
                    severity=SEVERITY_BLOCK,
                    description="原文没有禁提项，需要人工确认。",
                ),
            ),
        ),
    )
    return _brief_of(env), segment_ids


def test_update_draft_confirms_claim(env: BriefEnv) -> None:
    brief_id, _ = _seed_decomposed_draft(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        draft = service.require_draft(repo, brief=brief)
        # **先取出旧值**：`update_draft` 改的是同一个 ORM 实例（identity map），
        # 更新后再读 `draft.lock_version` 拿到的已是新值，断言会恒等。
        before = draft.lock_version
        updated = service.update_draft(
            repo,
            brief=brief,
            base_version_id=None,
            claims=[
                {
                    "fieldPath": "product.name",
                    "text": "某产品",
                    "resolution": RESOLUTION_CONFIRMED,
                }
            ],
            expected_lock_version=before,
            actor_id=env.owner_id,
        )
        assert updated.claims[0]["resolution"] == RESOLUTION_CONFIRMED
        # `lock_version` 递增（ETag 载体）。
        assert updated.lock_version == before + 1


def test_update_draft_rejects_invented_claim(env: BriefEnv) -> None:
    """运营**不能新增结论** —— 结论是文档的产物，运营的工作是确认它们。"""
    brief_id, _ = _seed_decomposed_draft(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        draft = service.require_draft(repo, brief=brief)
        with pytest.raises(ApiError) as caught:
            service.update_draft(
                repo,
                brief=brief,
                base_version_id=None,
                claims=[
                    {
                        "fieldPath": "product.name",
                        "text": "我编的结论",
                        "resolution": RESOLUTION_CONFIRMED,
                    }
                ],
                expected_lock_version=draft.lock_version,
                actor_id=env.owner_id,
            )
    assert caught.value.details["reason"] == "claim_not_in_draft"


def test_update_draft_rejects_unknown_issue_id(env: BriefEnv) -> None:
    """运营**不能新增或删除疑问** —— 否则"核心疑问阻断"退化成"运营说没有疑问"。"""
    brief_id, _ = _seed_decomposed_draft(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        draft = service.require_draft(repo, brief=brief)
        with pytest.raises(ApiError) as caught:
            service.update_draft(
                repo,
                brief=brief,
                base_version_id=None,
                issue_resolutions=[
                    {"id": str(uuid.uuid4()), "state": ISSUE_RESOLVED, "resolution": "我解决了"}
                ],
                expected_lock_version=draft.lock_version,
                actor_id=env.owner_id,
            )
    assert caught.value.details["reason"] == "issue_not_in_draft"


def test_update_draft_fills_resolved_by_server_side(env: BriefEnv) -> None:
    """`resolvedBy` 由服务端填 —— 不信客户端传的。

    信客户端意味着任何人都能把处置归到别人名下，而审计里看不出区别。
    """
    brief_id, _ = _seed_decomposed_draft(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        draft = service.require_draft(repo, brief=brief)
        issue_id = draft.issue_resolutions[0]["id"]
        updated = service.update_draft(
            repo,
            brief=brief,
            base_version_id=None,
            issue_resolutions=[
                {
                    "id": issue_id,
                    "state": ISSUE_RESOLVED,
                    "resolution": "已人工确认",
                    "resolvedBy": str(uuid.uuid4()),  # 客户端伪造的
                }
            ],
            expected_lock_version=draft.lock_version,
            actor_id=env.owner_id,
        )
        assert updated.issue_resolutions[0]["resolvedBy"] == str(env.owner_id)


def test_update_draft_requires_reason_when_settling_issue(env: BriefEnv) -> None:
    brief_id, _ = _seed_decomposed_draft(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        draft = service.require_draft(repo, brief=brief)
        issue_id = draft.issue_resolutions[0]["id"]
        with pytest.raises(ApiError) as caught:
            service.update_draft(
                repo,
                brief=brief,
                base_version_id=None,
                issue_resolutions=[{"id": issue_id, "state": ISSUE_RESOLVED}],
                expected_lock_version=draft.lock_version,
                actor_id=env.owner_id,
            )
    assert caught.value.details["reason"] == "issue_resolution_reason_required"


def test_update_draft_empty_request_does_not_bump_lock_version(env: BriefEnv) -> None:
    """空请求只回读 —— **不递增** `lock_version`。

    递增会让"点了保存但什么都没改"凭空产生一次版本冲突，
    而前端下一次提交必然 412。
    """
    brief_id = _brief_of(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        draft = service.require_draft(repo, brief=brief)
        before = draft.lock_version
        returned = service.update_draft(
            repo,
            brief=brief,
            base_version_id=None,
            expected_lock_version=draft.lock_version,
            actor_id=env.owner_id,
        )
        assert returned.lock_version == before


def test_update_draft_wrong_lock_version_raises_412(env: BriefEnv) -> None:
    brief_id = _brief_of(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        draft = service.require_draft(repo, brief=brief)
        with pytest.raises(ApiError) as caught:
            service.update_draft(
                repo,
                brief=brief,
                base_version_id=None,
                fields={"product.name": {"status": "FILLED", "value": "x"}},
                expected_lock_version=draft.lock_version + 99,
                actor_id=env.owner_id,
            )
    assert caught.value.code == ErrorCode.VERSION_CONFLICT
    assert "currentEtag" in caught.value.details


def test_update_draft_rejects_cross_project_base_version(env: BriefEnv) -> None:
    """`baseVersionId` 跨项目 → 422（否则溯源链指向一份看不到的文档）。"""
    other_owner = _create_user(env.settings)
    other_project = _seed_project(env.settings, owner_id=other_owner)
    with session_scope(env.settings) as session:
        # 走 WP11 的公开入口（`insert_node` 没有 `payload` 参数 ——
        # 注册表只存摘要，正文在各领域表里，见 `versions/models.py`）。
        # `entity_id` 用一个**新 uuid**：它不在 `briefs` 表里，
        # 因此 OP-88 的反向触发器（有意收窄为"只对真实存在的 Brief 强制"）
        # 会放行这个裸节点，本用例才测得到 `update_draft` 的跨项目判据。
        registered = versions_service.register_version(
            VersionRepository(session),
            entity_type=ENTITY_TYPE_BRIEF,
            entity_id=uuid.uuid4(),
            payload={"x": 1},
            project_id=other_project,
            created_by=other_owner,
        )
        session.flush()
        foreign_version_id = registered.node_id

    brief_id = _brief_of(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        draft = service.require_draft(repo, brief=brief)
        with pytest.raises(ApiError) as caught:
            service.update_draft(
                repo,
                brief=brief,
                base_version_id=foreign_version_id,
                expected_lock_version=draft.lock_version,
                actor_id=env.owner_id,
            )
    assert caught.value.details["reason"] == "cross_project_reference"


# --- solidify：四道门 ------------------------------------------------------


def _solidify(env: BriefEnv, *, actor: uuid.UUID | None = None) -> service.SolidifiedVersion:
    brief_id = _brief_of(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        return service.solidify(repo, brief=brief, actor_id=actor or env.owner_id)


def _prepare_solidifiable(env: BriefEnv) -> uuid.UUID:
    """准备一份可以直接固化的草稿（必填齐全、无未答复 BLOCK 疑问）。"""
    file_id = _seed_file(env, f"可固化 {uuid.uuid4().hex}\n".encode())
    run_id = _seed_run(env, file_id)
    segment_ids = _segment_ids(env, run_id)
    _decompose(
        env,
        run_id=run_id,
        decomposer=_scripted(
            fields=_full_fields(),
            claims=(
                ClaimDraft(
                    field_path="product.name",
                    text="测试产品",
                    category=CATEGORY_SOURCE_FACT,
                    evidence_segment_id=segment_ids[0],
                ),
            ),
        ),
    )
    return _brief_of(env)


def test_solidify_creates_version_with_registry_node(env: BriefEnv) -> None:
    """**确认版本闭环**：注册表节点与具体版本行同 id、同事务。"""
    brief_id = _prepare_solidifiable(env)
    result = _solidify(env)

    assert result.created is True
    assert result.revision_no == 1
    with session_scope(env.settings) as session:
        version = BriefRepository(session).get_version(result.version_id)
        assert version is not None
        assert version.brief_id == brief_id
        node = VersionRepository(session).get_node(result.version_id)
        assert node is not None
        assert node.entity_type == ENTITY_TYPE_BRIEF
        assert node.entity_id == brief_id
        assert node.revision_no == result.revision_no
        # 版本行的 id 就是节点 id（数据结构 02 §5）。
        assert node.id == version.id
        # 不可变 ETag 来自节点摘要，不是本模块重算。
        assert result.etag == node.etag


def test_solidify_updates_active_version(env: BriefEnv) -> None:
    _prepare_solidifiable(env)
    result = _solidify(env)
    with session_scope(env.settings) as session:
        brief = BriefRepository(session).get_brief_by_project(env.project_id)
        assert brief is not None
        assert brief.active_version_id == result.version_id


def test_solidify_freezes_claims_and_issues(env: BriefEnv) -> None:
    brief_id = _prepare_solidifiable(env)
    result = _solidify(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        claims = repo.list_claims(result.version_id)
        assert len(claims) == 1
        assert claims[0].category == CATEGORY_SOURCE_FACT
        assert claims[0].evidence_segment_id is not None
        assert claims[0].claim_hash == service.claim_hash(
            field_path="product.name", text="测试产品"
        )
        # 疑问随版本冻结（"这一版当时带着哪些提醒"）。
        assert repo.list_issues(result.version_id) == []
        assert brief_id is not None


def test_solidify_rejects_missing_required_fields(env: BriefEnv) -> None:
    """环节 1：必填字段未填 → 422，`details.missingFields` 列出具体路径。"""
    file_id = _seed_file(env, "字段不全\n".encode("utf-8"))
    run_id = _seed_run(env, file_id)
    _decompose(env, run_id=run_id, decomposer=_scripted())
    with pytest.raises(ApiError) as caught:
        _solidify(env)
    assert caught.value.code == ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == "required_fields_missing"
    assert set(caught.value.details["missingFields"]) == set(
        brief_schema.REQUIRED_FIELD_PATHS
    )


def test_solidify_rejects_open_blocking_issue(env: BriefEnv) -> None:
    """环节 2：有未答复的 BLOCK 疑问 → 409，列出具体疑问。"""
    brief_id, _ = _seed_decomposed_draft(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        draft = service.require_draft(repo, brief=brief)
        service.update_draft(
            repo,
            brief=brief,
            base_version_id=None,
            fields=_full_fields(),
            expected_lock_version=draft.lock_version,
            actor_id=env.owner_id,
        )

    with pytest.raises(ApiError) as caught:
        _solidify(env)
    assert caught.value.code == ErrorCode.INVALID_TRANSITION
    assert caught.value.details["reason"] == "blocking_issues_open"
    assert len(caught.value.details["blockingIssues"]) == 1


def test_solidify_allows_settled_blocking_issue(env: BriefEnv) -> None:
    """处置过的 BLOCK 不再阻断（"确认版本闭环"的第 2→3 步）。"""
    brief_id, _ = _seed_decomposed_draft(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        draft = service.require_draft(repo, brief=brief)
        issue_id = draft.issue_resolutions[0]["id"]
        service.update_draft(
            repo,
            brief=brief,
            base_version_id=None,
            fields=_full_fields(),
            issue_resolutions=[
                {
                    "id": issue_id,
                    "state": ISSUE_RESOLVED,
                    "resolution": "已人工确认原文确实无禁提项",
                }
            ],
            expected_lock_version=draft.lock_version,
            actor_id=env.owner_id,
        )

    result = _solidify(env)
    assert result.created is True
    assert result.issue_count == 1
    with session_scope(env.settings) as session:
        issues = BriefRepository(session).list_issues(result.version_id)
        assert issues[0].state == ISSUE_RESOLVED
        assert issues[0].resolved_by == env.owner_id


def test_solidify_rejects_unchanged_draft(env: BriefEnv) -> None:
    """环节 3：内容与基准版本相同 → 409 `draft_unchanged`。

    判据是**内容摘要**而不是 `lock_version`：后者每次保存都递增
    （哪怕改回原值），用它判"变没变"会把"改了又改回来"误判成有变更 ——
    而那正是本判据要拦的情形。
    """
    _prepare_solidifiable(env)
    _solidify(env)
    with pytest.raises(ApiError) as caught:
        _solidify(env)
    assert caught.value.code == ErrorCode.INVALID_TRANSITION
    assert caught.value.details["reason"] == "draft_unchanged"


def test_solidify_after_real_edit_creates_second_revision(env: BriefEnv) -> None:
    """改了内容再固化 → 新修订号（**不覆盖**旧版，需求 §227）。"""
    brief_id = _prepare_solidifiable(env)
    first = _solidify(env)

    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        draft = service.require_draft(repo, brief=brief)
        service.update_draft(
            repo,
            brief=brief,
            base_version_id=first.version_id,
            fields=_full_fields(**{"product.name": "改名后的产品"}),
            expected_lock_version=draft.lock_version,
            actor_id=env.owner_id,
        )

    second = _solidify(env)
    assert second.revision_no == 2
    assert second.version_id != first.version_id

    # 旧版仍在（不可变、不被更新）。
    with session_scope(env.settings) as session:
        old = BriefRepository(session).get_version(first.version_id)
        assert old is not None
        assert old.product_name == "测试产品"


def test_solidify_clears_suggestions_and_moves_base_version(env: BriefEnv) -> None:
    """固化后建议清空、基准版本前移，但**草稿保留**（协议 §5 末段）。"""
    brief_id = _prepare_solidifiable(env)
    result = _solidify(env)
    with session_scope(env.settings) as session:
        draft = BriefRepository(session).get_draft_for_brief(brief_id)
        assert draft is not None
        assert draft.base_version_id == result.version_id
        assert draft.suggestions == []
        assert draft.suggestions_version_id is None


def test_solidify_summary_reports_selling_point_count(env: BriefEnv) -> None:
    """摘要如实报告卖点条数（不足三项时不凑数）。"""
    file_id = _seed_file(env, f"两条卖点 {uuid.uuid4().hex}\n".encode())
    run_id = _seed_run(env, file_id)
    _decompose(
        env,
        run_id=run_id,
        decomposer=_scripted(
            fields=_full_fields(
                **{"product.selling_points": ["卖点一", "卖点二"]}
            )
        ),
    )
    result = _solidify(env)
    assert "2" in result.summary


# --- 加厚：建议不自动生效 ------------------------------------------------


def test_enrichment_request_must_have_allowed_sources(env: BriefEnv) -> None:
    """`allowedSourceIds` 为空 → 422。

    空白名单意味着"允许引用零个来源"，而那只会让模型编造 ——
    正是需求 §5.2 禁止的。
    """
    brief_id = _prepare_solidifiable(env)
    result = _solidify(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        with pytest.raises(ApiError) as caught:
            service.request_enrichment(
                repo,
                brief=brief,
                brief_version_id=result.version_id,
                allowed_source_ids=[],
            )
    assert caught.value.details["reason"] == "allowed_sources_empty"


def test_enrichment_request_rejects_version_of_other_brief(env: BriefEnv) -> None:
    brief_id = _prepare_solidifiable(env)
    result = _solidify(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        with pytest.raises(ApiError) as caught:
            service.request_enrichment(
                repo,
                brief=brief,
                brief_version_id=uuid.uuid4(),
                allowed_source_ids=[uuid.uuid4()],
            )
    assert caught.value.details["reason"] == "brief_version_not_found"


def test_enrichment_writes_suggestions_not_claims(env: BriefEnv) -> None:
    """**建议写 `suggestions`，不写 `claims`**（协议 §5 第 5 行"不自动生效"）。

    运营要采纳，是在 `PUT /brief-draft` 里显式加进 `claims` ——
    那一步会走 `ClaimDraft` 的校验（事实类必须有来源）。
    于是"建议变成事实"永远要经过一次显式确认。
    """
    brief_id = _prepare_solidifiable(env)
    solidified = _solidify(env)
    segment_ids = _segment_ids_of_latest(env, brief_id)

    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        _version, request = service.request_enrichment(
            repo,
            brief=brief,
            brief_version_id=solidified.version_id,
            allowed_source_ids=[segment_ids[0]],
        )

    # 处理器路径：读请求 → 调解构 → 写建议。
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        request_row = service.read_enrichment_request(
            repo, project_id=env.project_id
        )
        assert request_row is not None
        assert request_row.allowed_source_ids == (segment_ids[0],)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        outcome = service.execute_enrichment(
            repo,
            brief=brief,
            brief_version_id=solidified.version_id,
            allowed_source_ids=request_row.allowed_source_ids,
            segments=tuple(ParseRepository(session).list_segments(
                _parse_run_of_version(env, solidified.version_id)
            )),
            decomposer=ScriptedDecomposer(
                enrichment=EnrichmentResult(
                    suggestions=(
                        EnrichmentSuggestion(
                            field_path="product.selling_points",
                            text="可补充：快充 30 分钟",
                            category=CATEGORY_EXTERNAL_EVIDENCE,
                            evidence_segment_id=segment_ids[0],
                            rationale="同系列产品的公开参数",
                        ),
                    )
                )
            ),
        )

    assert outcome.suggestion_count == 1
    with session_scope(env.settings) as session:
        draft = BriefRepository(session).get_draft_for_brief(brief_id)
        assert draft is not None
        assert len(draft.suggestions) == 1
        assert draft.suggestions_version_id == solidified.version_id
        # **claims 没变** —— 建议没有自动生效。
        assert all(
            item["text"] != "可补充：快充 30 分钟" for item in draft.claims
        )


def test_enrichment_request_is_consumed_once(env: BriefEnv) -> None:
    """请求读一次即清空（它是一次性的，留着会让下一轮误读旧参数）。"""
    brief_id = _prepare_solidifiable(env)
    solidified = _solidify(env)
    segment_ids = _segment_ids_of_latest(env, brief_id)

    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        service.request_enrichment(
            repo,
            brief=brief,
            brief_version_id=solidified.version_id,
            allowed_source_ids=[segment_ids[0]],
        )

    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        assert (
            service.read_enrichment_request(repo, project_id=env.project_id)
            is not None
        )
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        assert (
            service.read_enrichment_request(repo, project_id=env.project_id)
            is None
        )


def test_enrichment_drops_suggestions_outside_allowed_sources(env: BriefEnv) -> None:
    """白名单外的 `segmentId` 被**丢弃**。

    只做"只把白名单内的片段交给模型"是不够的 —— 一个被污染的提示词
    仍能让模型引用一个它没见过的 id。两处都要。
    """
    brief_id = _prepare_solidifiable(env)
    solidified = _solidify(env)
    segment_ids = _segment_ids_of_latest(env, brief_id)

    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        run_id = _parse_run_of_version(env, solidified.version_id)
        segments = tuple(ParseRepository(session).list_segments(run_id))
        outcome = service.execute_enrichment(
            repo,
            brief=brief,
            brief_version_id=solidified.version_id,
            allowed_source_ids=[segment_ids[0]],
            segments=segments,
            decomposer=ScriptedDecomposer(
                enrichment=EnrichmentResult(
                    suggestions=(
                        EnrichmentSuggestion(
                            field_path="product.selling_points",
                            text="引用了白名单外的来源",
                            category=CATEGORY_STRATEGY,
                            evidence_segment_id=segment_ids[1],
                        ),
                    )
                )
            ),
        )
    # `ScriptedDecomposer` 不做过滤（它是"模型"），过滤发生在
    # `GatewayDecomposer` 与 `_read_suggestion`。这里断言的是
    # **service 层不会因为越界建议而崩** —— 真实的丢弃在
    # `test_suggestion_outside_allowed_ids_is_dropped` 里单独验证。
    assert outcome.suggestion_count == 1


def test_suggestion_outside_allowed_ids_is_dropped() -> None:
    """`_read_suggestion` 丢弃白名单外的 id（纯函数层）。"""
    from rednote.modules.brief.decomposer import _read_suggestion

    allowed = {str(uuid.uuid4())}
    outside = str(uuid.uuid4())
    assert (
        _read_suggestion(
            {
                "fieldPath": "product.selling_points",
                "text": "越界建议",
                "category": "STRATEGY",
                "evidence": {"segmentId": outside},
            },
            allowed_ids=allowed,
        )
        is None
    )


# --- get_view --------------------------------------------------------------


def test_get_view_includes_source_claims_and_evidence_segments(env: BriefEnv) -> None:
    """`GET /brief` 的视图：原文对照审核的最小充分集。

    claims 只带 `segmentId`，没有文本就对照不了 ——
    因此 `evidenceSegments` 必须把被引用的片段文本带出来。
    """
    brief_id = _prepare_solidifiable(env)
    result = _solidify(env)

    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        run_id = _parse_run_of_version(env, result.version_id)
        segments = {
            segment.id: segment
            for segment in ParseRepository(session).list_segments(run_id)
        }
        view = service.get_view(repo, brief=brief, segments_by_id=segments)

    assert view["activeVersionId"] == str(result.version_id)
    assert view["version"] is not None
    assert view["version"]["versionId"] == str(result.version_id)
    assert view["version"]["etag"] is not None
    assert len(view["version"]["claims"]) == 1
    assert len(view["version"]["evidenceSegments"]) == 1
    assert view["version"]["evidenceSegments"][0]["text"]
    assert view["draft"]["etag"]
    assert len(view["versions"]) == 1


def test_get_view_marks_missing_evidence_segment(env: BriefEnv) -> None:
    """引用了查不到的片段时返回**占位**（`missing=True`），不静默跳过。

    "这条结论引用了一个查不到的片段"正是需要被看到的事实
    （触发器保证正常数据下不会发生，所以它出现就意味着异常）。
    """
    brief_id = _prepare_solidifiable(env)
    _solidify(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        view = service.get_view(repo, brief=brief, segments_by_id={})
    assert view["version"]["evidenceSegments"][0]["missing"] is True


def test_get_view_rejects_unknown_version(env: BriefEnv) -> None:
    brief_id = _brief_of(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        with pytest.raises(ApiError) as caught:
            service.get_view(repo, brief=brief, version_id=uuid.uuid4())
    assert caught.value.code == ErrorCode.RESOURCE_NOT_FOUND


# --- 任务处理器 ------------------------------------------------------------


def _enqueue_and_claim(env: BriefEnv, *, kind: str, file_id: uuid.UUID):
    """入队一个任务并手工构造 `ClaimedJob`（不走 Worker 循环）。

    **必须像真实端点一样先 `ensure_brief`**：`brief_parse_handler` 由
    文件反查 Brief，查不到就报 `BRIEF_NOT_FOUND` 并不重试。
    真实路径里 `POST /brief-parse-jobs` 会先 `ensure_brief`（api.py:231），
    本 helper 绕过端点直接入队，因此这一步要自己补 ——
    否则测的是"任务在 Brief 不存在时失败"，而不是解构本身。
    """
    from rednote.modules.jobs import service as jobs_service
    from rednote.modules.jobs.types import ClaimedJob

    with session_scope(env.settings) as session:
        job_repo = JobRepository(session)
        parse_repo = ParseRepository(session)
        file_repo = FileRepository(session)
        brief_repo = BriefRepository(session)
        service.ensure_brief(brief_repo, project_id=env.project_id)
        run, _created = service.request_parse(
            parse_repo, file_repo, project_id=env.project_id, file_id=file_id
        )
        view = jobs_service.enqueue_job(
            job_repo,
            kind=kind,
            queue=QUEUE_BRIEF,
            idempotency_scope=IDEMPOTENCY_SCOPE_BRIEF_PARSE,
            idempotency_key=f"{file_id}:{uuid.uuid4().hex}",
            input_hash=service.parse_input_hash(
                file_id=file_id, parser_version=run.parser_version
            ),
            project_id=env.project_id,
        )
        run.job_id = view.job_id
        parse_repo.flush()
        job_id = view.job_id

    return ClaimedJob(
        job_id=job_id,
        kind=kind,
        attempt_id=uuid.uuid4(),
        fence_token=1,
        attempt_no=1,
        worker_id="test-worker",
    )


def test_brief_parse_handler_runs_parse_then_decompose(env: BriefEnv) -> None:
    """一个任务里既解析又解构（见 `handlers.py` 模块 docstring 第一节）。"""
    from rednote.modules.brief.handlers import brief_parse_handler

    file_id = _seed_file(env, "处理器路径的文档\n".encode("utf-8"))
    claimed = _enqueue_and_claim(env, kind=KIND_BRIEF_PARSE, file_id=file_id)

    with session_scope(env.settings) as session:
        outcome = brief_parse_handler(
            claimed,
            JobRepository(session),
            decomposer=_scripted(
                fields={"product.name": {"status": "FILLED", "value": "处理器产品"}}
            ),
            storage=env.store,
        )
    assert outcome.state == STATE_SUCCEEDED

    with session_scope(env.settings) as session:
        draft = BriefRepository(session).get_draft_for_brief(_brief_of(env))
        assert draft is not None
        assert draft.structured_fields["product.name"]["value"] == "处理器产品"


def test_brief_parse_handler_maps_partial_to_partial_state(env: BriefEnv) -> None:
    """处理器把 PARTIAL **如实映射**到任务状态（不是 SUCCEEDED）。"""
    from rednote.modules.brief.handlers import brief_parse_handler

    file_id = _seed_file(env, "处理器路径的缺页文档\n".encode("utf-8"))
    run_id = _seed_run(
        env,
        file_id,
        texts=("只有一页",),
        total_units=3,
        errors=[{"unit": 2, "code": "UNIT_FAILED", "message": "解不出"}],
    )
    with session_scope(env.settings) as session:
        run = ParseRepository(session).get_run(run_id)
        assert run is not None
        assert run.state == PARSE_STATE_PARTIAL

    claimed = _enqueue_and_claim(env, kind=KIND_BRIEF_PARSE, file_id=file_id)
    # 让处理器指向那条已存在的 run（`_enqueue_and_claim` 新建了一条 QUEUED run，
    # 这里改指向 PARTIAL 的那条 —— 模拟"解析早已完成、任务后到"）。
    with session_scope(env.settings) as session:
        session.execute(
            text("update parse_runs set job_id = null where job_id = :jid"),
            {"jid": claimed.job_id},
        )
        session.execute(
            text("update parse_runs set job_id = :jid where id = :rid"),
            {"jid": claimed.job_id, "rid": run_id},
        )

    with session_scope(env.settings) as session:
        outcome = brief_parse_handler(
            claimed, JobRepository(session), decomposer=_scripted(), storage=env.store
        )
    assert outcome.state == STATE_PARTIAL


def test_brief_parse_handler_without_run_fails_without_retry(env: BriefEnv) -> None:
    """任务没有对应 run → 失败且**不可重试**（重试不会让它长出 run）。"""
    from rednote.modules.brief.handlers import brief_parse_handler
    from rednote.modules.jobs.types import ClaimedJob

    claimed = ClaimedJob(
        job_id=uuid.uuid4(),
        kind=KIND_BRIEF_PARSE,
        attempt_id=uuid.uuid4(),
        fence_token=1,
        attempt_no=1,
        worker_id="test-worker",
    )
    with session_scope(env.settings) as session:
        outcome = brief_parse_handler(claimed, JobRepository(session))
    assert outcome.state == "FAILED"
    assert outcome.retryable is False


def test_brief_enrich_handler_without_request_fails(env: BriefEnv) -> None:
    """没有待处理请求 → 失败且不可重试。"""
    from rednote.modules.brief.handlers import brief_enrich_handler
    from rednote.modules.jobs.types import ClaimedJob

    claimed = ClaimedJob(
        job_id=uuid.uuid4(),
        kind=KIND_BRIEF_ENRICH,
        attempt_id=uuid.uuid4(),
        fence_token=1,
        attempt_no=1,
        worker_id="test-worker",
    )
    with session_scope(env.settings) as session:
        outcome = brief_enrich_handler(claimed, JobRepository(session))
    assert outcome.state == "FAILED"
    assert outcome.retryable is False


def test_brief_parse_handler_reports_unavailable_decomposer(env: BriefEnv) -> None:
    """解构通道不可用 → 失败且**不可重试**（重试可能再买一次模型调用）。"""
    from rednote.modules.brief.handlers import brief_parse_handler

    file_id = _seed_file(env, "通道不可用\n".encode("utf-8"))
    claimed = _enqueue_and_claim(env, kind=KIND_BRIEF_PARSE, file_id=file_id)
    with session_scope(env.settings) as session:
        outcome = brief_parse_handler(
            claimed, JobRepository(session), storage=env.store
        )
    assert outcome.state == "FAILED"
    assert outcome.retryable is False


# ===========================================================================
# 第 3 层：数据库约束活性（原生 SQL，绕过 ORM 与 service）
# ===========================================================================


def _version_row_params(
    *,
    version_id: uuid.UUID,
    brief_id: uuid.UUID,
    source_file_id: uuid.UUID,
    source_parse_run_id: uuid.UUID | None,
    source_state: str = SOURCE_STATE_SUCCEEDED,
) -> dict[str, Any]:
    return {
        "id": version_id,
        "brief_id": brief_id,
        "file": source_file_id,
        "run": source_parse_run_id,
        "state": source_state,
        "name": "测试产品",
        "summary": "摘要",
        "fields": "{}",
    }


_INSERT_VERSION_SQL = text(
    "insert into brief_versions (id, brief_id, source_file_id,"
    " source_parse_run_id, source_state, product_name, summary,"
    " structured_fields) values (:id, :brief_id, :file, :run, :state,"
    " :name, :summary, cast(:fields as jsonb))"
)


_INSERT_NODE_SQL = text(
    "insert into version_nodes (id, entity_type, entity_id, revision_no,"
    " payload_hash, schema_version, project_id)"
    " values (:id, :etype, :eid, :rev, :phash, 1, :pid)"
)


def _node_row_params(
    *, node_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID,
    project_id: uuid.UUID, revision_no: int = 1,
) -> dict[str, Any]:
    """版本节点的行参数（与 `_INSERT_NODE_SQL` 配套）。

    **列清单与 `version_nodes` 的实际结构一一对应**：注册表按文档 02 §5
    的原文"只存摘要不存正文"，因此**没有 `payload` 列** ——
    正文在各领域的具体版本表里（`brief_versions.structured_fields`）。
    本组用例要构造的正是"注册表与子表说的不是同一件事"，
    所以两边必须用同一份真实结构，否则测的是一个不存在的 schema。
    """
    return {
        "id": node_id,
        "etype": entity_type,
        "eid": entity_id,
        "rev": revision_no,
        "phash": "0" * 64,
        "pid": project_id,
    }


def _insert_node_in(
    session: Any,
    *,
    node_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    project_id: uuid.UUID,
    revision_no: int = 1,
) -> None:
    """在**调用方的事务内**插入版本节点，与后续子表插入一起提交。

    **为什么不提供"独立事务插节点"的 helper**：OP-88 的反向触发器是
    `DEFERRABLE INITIALLY DEFERRED` 的约束触发器，它**在 COMMIT 时**才跑。
    节点若单独提交，那一刻还没有配对的 `brief_versions` 行 ——
    触发器会先以"孤立节点"报错，而"节点正常、子表违规"这类用例
    想验证的判据根本轮不到。所以凡是"节点 + 子表"的场景，
    两边必须在同一个事务里，延迟触发器看到的才是一个完整的最终状态。

    需要"节点单独提交"的用例（`test_op88_rejects_orphan_node_*`）
    直接 `_raw(_INSERT_NODE_SQL.text, _node_row_params(...))`。
    """
    session.execute(
        _INSERT_NODE_SQL,
        _node_row_params(
            node_id=node_id, entity_type=entity_type, entity_id=entity_id,
            project_id=project_id, revision_no=revision_no,
        ),
    )


def _insert_brief_raw(env: BriefEnv, *, brief_id: uuid.UUID) -> uuid.UUID:
    """在**新项目**里插入一个裸 Brief（绕过 `ensure_brief`），返回项目 id。

    **为什么必须自开项目而不是复用 `env.project_id`**：
    `briefs.project_id` 是唯一列（一个项目一份 Brief），而复用会撞
    `uq_briefs_project_id` —— 那个错误读起来像"测试写错了 id"，
    实际是"这个项目已经有一份 Brief 了"。

    **为什么返回项目 id**：`brief_issues.project_id` 必须等于版本所属
    Brief 的项目（`trg_brief_issues_project_scope` 会校验），
    调用方必须拿得到它。
    """
    project_id = _seed_project(env.settings, owner_id=env.owner_id)
    _raw(
        "insert into briefs (id, project_id) values (:id, :pid)",
        {"id": brief_id, "pid": project_id},
    )
    return project_id


def test_op88_rejects_node_with_wrong_entity_type(env: BriefEnv) -> None:
    """**OP-88 判据 2**：节点存在但 `entity_type` 不是 `BRIEF` → 提交时抛 23514。

    这条是"注册表与子表说的不是同一件事"的直接拦截面。
    判据 1（节点存在）由 FK 保证，因此这里专测判据 2 与 3。

    **节点与版本必须同一事务**：反向触发器（孤立节点）是延迟的，
    节点若单独提交会先以"孤立节点"报错，判据 2 根本轮不到。
    """
    brief_id = uuid.uuid4()
    project_id = _insert_brief_raw(env, brief_id=brief_id)
    node_id = uuid.uuid4()

    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            _insert_node_in(
                session,
                node_id=node_id,
                entity_type="CONTENT",  # ← 故意错
                entity_id=brief_id,
                project_id=project_id,
            )
            session.execute(
                _INSERT_VERSION_SQL,
                _version_row_params(
                    version_id=node_id,
                    brief_id=brief_id,
                    source_file_id=_seed_file(env, b"op88 scope\n"),
                    source_parse_run_id=None,
                ),
            )
    assert caught.value.orig.sqlstate == brief_ddl.CONSISTENCY_SQLSTATE


def test_op88_rejects_node_with_mismatched_entity_id(env: BriefEnv) -> None:
    """**OP-88 判据 3**：`entity_id` 与 `brief_id` 不一致 → 23514。

    这条防的是"这一版属于哪个逻辑 Brief 有两个互相矛盾的答案" ——
    而下游（`version_dependencies` 的溯源）取的是节点侧的那个。
    """
    brief_id = uuid.uuid4()
    other_brief_id = uuid.uuid4()
    project_id = _insert_brief_raw(env, brief_id=brief_id)
    _insert_brief_raw(env, brief_id=other_brief_id)
    node_id = uuid.uuid4()

    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            _insert_node_in(
                session,
                node_id=node_id,
                entity_type="BRIEF",
                entity_id=other_brief_id,  # ← 指向另一个 Brief
                project_id=env.project_id,
            )
            session.execute(
                _INSERT_VERSION_SQL,
                _version_row_params(
                    version_id=node_id,
                    brief_id=brief_id,
                    source_file_id=_seed_file(env, b"op88 scope\n"),
                    source_parse_run_id=None,
                ),
            )
    assert caught.value.orig.sqlstate == brief_ddl.CONSISTENCY_SQLSTATE


def test_op88_accepts_consistent_node_and_version(env: BriefEnv) -> None:
    """判据 1~3 全满足时**必须放行**（否则触发器就是"一律拒绝"的噪声）。"""
    brief_id = uuid.uuid4()
    project_id = _insert_brief_raw(env, brief_id=brief_id)
    node_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        _insert_node_in(
            session,
            node_id=node_id,
            entity_type="BRIEF",
            entity_id=brief_id,
            project_id=project_id,
        )
        session.execute(
            _INSERT_VERSION_SQL,
            _version_row_params(
                version_id=node_id,
                brief_id=brief_id,
                source_file_id=_seed_file(env, b"op88 ok\n"),
                source_parse_run_id=None,
            ),
        )
    with session_scope(env.settings) as session:
        assert (
            session.execute(
                text("select count(*) from brief_versions where id = :id"),
                {"id": node_id},
            ).scalar_one()
            == 1
        )


def test_op88_rejects_orphan_node_for_existing_brief(env: BriefEnv) -> None:
    """**OP-88 反向判据**：BRIEF 节点没有配对 `brief_versions` → 23514。

    **收窄是刻意的**（见 `ddl.py` docstring）：只对"`briefs` 表里真实存在的
    `entity_id`"强制 —— 8 类实体里只有 2 类在本批落地，
    而 WP11 的测试夹具会造无领域行的 BRIEF 节点。
    """
    brief_id = uuid.uuid4()
    project_id = _insert_brief_raw(env, brief_id=brief_id)

    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            session.execute(
                _INSERT_NODE_SQL,
                _node_row_params(
                    node_id=uuid.uuid4(),
                    entity_type="BRIEF",
                    entity_id=brief_id,
                    project_id=project_id,
                ),
            )
    assert caught.value.orig.sqlstate == brief_ddl.CONSISTENCY_SQLSTATE


def test_op88_allows_orphan_node_for_unknown_entity(env: BriefEnv) -> None:
    """`entity_id` 指向不存在的 Brief 时**放行**（有意收窄的代价）。

    这条**主动锁定收窄行为**：它是一个已知的、被登记为待定项的取舍。
    若将来有人把收窄去掉，这条测试会红 —— 那时需要同步更新登记册，
    而不是让一个未声明的行为变化悄悄发生。
    """
    _raw(
        _INSERT_NODE_SQL.text,
        _node_row_params(
            node_id=uuid.uuid4(),
            entity_type="BRIEF",
            entity_id=uuid.uuid4(),
            # 这里刻意用一个**新项目**：本用例要证明"悬空节点"不被拦，
            # 而 `version_nodes.project_id` 有指向 `projects` 的外键，
            # 随便造一个 uuid 会先撞 23503，测不到触发器那一条。
            project_id=_seed_project(env.settings, owner_id=env.owner_id),
        ),
    )


def test_partial_version_without_issues_is_rejected_by_trigger(env: BriefEnv) -> None:
    """**硬约束第三层**：`source_state='PARTIAL'` 的版本必须有 `brief_issues` 行。

    固化不得把残缺洗白 —— 这一条必须放在触发器里，因为 `brief_issues`
    是另一张表，且在同一事务里晚于 `brief_versions` 插入。
    """
    brief_id = uuid.uuid4()
    project_id = _insert_brief_raw(env, brief_id=brief_id)
    node_id = uuid.uuid4()
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            _insert_node_in(
                session,
                node_id=node_id,
                entity_type="BRIEF",
                entity_id=brief_id,
                project_id=project_id,
            )
            session.execute(
                _INSERT_VERSION_SQL,
                _version_row_params(
                    version_id=node_id,
                    brief_id=brief_id,
                    source_file_id=_seed_file(env, b"partial version\n"),
                    source_parse_run_id=None,
                    source_state=SOURCE_STATE_PARTIAL,
                ),
            )
    assert caught.value.orig.sqlstate == brief_ddl.CONSISTENCY_SQLSTATE


def test_partial_version_with_issue_is_accepted(env: BriefEnv) -> None:
    """带上疑问就放行 —— 残缺**可以固化**，但必须被记录（本包的口径）。"""
    brief_id = uuid.uuid4()
    project_id = _insert_brief_raw(env, brief_id=brief_id)
    node_id = uuid.uuid4()
    file_id = _seed_file(env, b"partial with issue\n")

    with session_scope(env.settings) as session:
        _insert_node_in(
            session,
            node_id=node_id,
            entity_type="BRIEF",
            entity_id=brief_id,
            project_id=project_id,
        )
        session.execute(
            _INSERT_VERSION_SQL,
            _version_row_params(
                version_id=node_id,
                brief_id=brief_id,
                source_file_id=file_id,
                source_parse_run_id=None,
                source_state=SOURCE_STATE_PARTIAL,
            ),
        )
        session.execute(
            text(
                "insert into brief_issues (id, project_id, brief_version_id,"
                " field_path, severity, description, state)"
                " values (:id, :pid, :vid, '*', 'WARN', '缺页', 'OPEN')"
            ),
            {"id": uuid.uuid4(), "pid": project_id, "vid": node_id},
        )

    with session_scope(env.settings) as session:
        assert (
            session.execute(
                text("select count(*) from brief_issues where brief_version_id = :v"),
                {"v": node_id},
            ).scalar_one()
            == 1
        )


def test_claim_evidence_from_other_run_is_rejected(env: BriefEnv) -> None:
    """**假溯源防护**：证据片段必须属于本版本那次解析。

    只要求"有 `evidence_segment_id`"是不够的：一个指向别的项目、
    别的文件的片段 id 同样能满足它，而那是**假溯源** ——
    它比"没有来源"更危险，因为它看起来可追溯。
    """
    brief_id = uuid.uuid4()
    project_id = _insert_brief_raw(env, brief_id=brief_id)
    file_a = _seed_file(env, b"evidence source A\n")
    file_b = _seed_file(env, b"evidence source B\n")
    run_a = _seed_run(env, file_a, texts=("A 的片段",))
    run_b = _seed_run(env, file_b, texts=("B 的片段",))
    segments_a = _segment_ids(env, run_a)
    segments_b = _segment_ids(env, run_b)

    node_id = uuid.uuid4()
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            _insert_node_in(
                session,
                node_id=node_id,
                entity_type="BRIEF",
                entity_id=brief_id,
                project_id=project_id,
            )
            session.execute(
                _INSERT_VERSION_SQL,
                _version_row_params(
                    version_id=node_id,
                    brief_id=brief_id,
                    source_file_id=file_a,
                    source_parse_run_id=run_a,
                ),
            )
            # ← 引用 B 的片段（属于另一次解析）
            session.execute(
                text(
                    "insert into brief_claims (id, brief_version_id, field_path,"
                    " claim_text, category, evidence_segment_id, claim_hash)"
                    " values (:id, :vid, 'product.name', '某产品', 'SOURCE_FACT',"
                    " :seg, :hash)"
                ),
                {
                    "id": uuid.uuid4(),
                    "vid": node_id,
                    "seg": segments_b[0],
                    "hash": "a" * 64,
                },
            )
    assert caught.value.orig.sqlstate == brief_ddl.CONSISTENCY_SQLSTATE


def test_claim_evidence_from_same_run_is_accepted(env: BriefEnv) -> None:
    """同一次解析的片段放行 —— 否则触发器就是"一律拒绝"的噪声。"""
    brief_id = uuid.uuid4()
    project_id = _insert_brief_raw(env, brief_id=brief_id)
    file_id = _seed_file(env, b"evidence same run\n")
    run_id = _seed_run(env, file_id, texts=("同一批片段",))
    segment_ids = _segment_ids(env, run_id)

    node_id = uuid.uuid4()

    with session_scope(env.settings) as session:
        _insert_node_in(
            session,
            node_id=node_id,
            entity_type="BRIEF",
            entity_id=brief_id,
            project_id=project_id,
        )
        session.execute(
            _INSERT_VERSION_SQL,
            _version_row_params(
                version_id=node_id,
                brief_id=brief_id,
                source_file_id=file_id,
                source_parse_run_id=run_id,
            ),
        )
        session.execute(
            text(
                "insert into brief_claims (id, brief_version_id, field_path,"
                " claim_text, category, evidence_segment_id, claim_hash)"
                " values (:id, :vid, 'product.name', '某产品', 'SOURCE_FACT',"
                " :seg, :hash)"
            ),
            {
                "id": uuid.uuid4(),
                "vid": node_id,
                "seg": segment_ids[0],
                "hash": "a" * 64,
            },
        )

    with session_scope(env.settings) as session:
        assert (
            session.execute(
                text("select count(*) from brief_claims where brief_version_id = :v"),
                {"v": node_id},
            ).scalar_one()
            == 1
        )


def test_issue_project_mismatch_is_rejected(env: BriefEnv) -> None:
    """`brief_issues.project_id` 必须等于版本所属项目 → 否则 23514。

    冗余列不校验就会漂移成"两个互相矛盾的答案"，
    而"核心疑问未解决阻断步骤 1"正是按它来查的 ——
    漂移方向恰好是"漏掉某个项目的阻断疑问"。
    """
    other_owner = _create_user(env.settings)
    other_project = _seed_project(env.settings, owner_id=other_owner)

    brief_id = uuid.uuid4()
    project_id = _insert_brief_raw(env, brief_id=brief_id)
    node_id = uuid.uuid4()
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            _insert_node_in(
                session,
                node_id=node_id,
                entity_type="BRIEF",
                entity_id=brief_id,
                project_id=project_id,
            )
            session.execute(
                _INSERT_VERSION_SQL,
                _version_row_params(
                    version_id=node_id,
                    brief_id=brief_id,
                    source_file_id=_seed_file(env, b"issue scope\n"),
                    source_parse_run_id=None,
                ),
            )
            session.execute(
                text(
                    "insert into brief_issues (id, project_id, brief_version_id,"
                    " field_path, severity, description, state)"
                    " values (:id, :pid, :vid, '*', 'WARN', '缺页', 'OPEN')"
                ),
                # ← 故意用别的项目
                {"id": uuid.uuid4(), "pid": other_project, "vid": node_id},
            )
    assert caught.value.orig.sqlstate == brief_ddl.CONSISTENCY_SQLSTATE


def test_raw_factual_claim_without_evidence_is_rejected(env: BriefEnv) -> None:
    """**数据库层的"分类来源"**：绕过构造器直接插入事实类无证据 → 23514。"""
    brief_id = uuid.uuid4()
    project_id = _insert_brief_raw(env, brief_id=brief_id)
    node_id = uuid.uuid4()
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            _insert_node_in(
                session,
                node_id=node_id,
                entity_type="BRIEF",
                entity_id=brief_id,
                project_id=project_id,
            )
            session.execute(
                _INSERT_VERSION_SQL,
                _version_row_params(
                    version_id=node_id,
                    brief_id=brief_id,
                    source_file_id=_seed_file(env, b"raw factual\n"),
                    source_parse_run_id=None,
                ),
            )
            session.execute(
                text(
                    "insert into brief_claims (id, brief_version_id, field_path,"
                    " claim_text, category, claim_hash)"
                    " values (:id, :vid, 'product.name', '某产品', 'SOURCE_FACT',"
                    " :hash)"
                ),
                {"id": uuid.uuid4(), "vid": node_id, "hash": "a" * 64},
            )
    assert caught.value.orig.sqlstate == "23514"


def test_raw_blocking_issue_open_is_rejected(env: BriefEnv) -> None:
    """**"核心疑问未解决阻断步骤 1" 的数据库级表述**。

    它防的是**服务层回归**：某天有人改了固化校验而没改这里，
    一条"带着未答复核心疑问的版本"就会写进去 —— 而那正是本包存在的理由。
    """
    brief_id = uuid.uuid4()
    project_id = _insert_brief_raw(env, brief_id=brief_id)
    node_id = uuid.uuid4()
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            _insert_node_in(
                session,
                node_id=node_id,
                entity_type="BRIEF",
                entity_id=brief_id,
                project_id=project_id,
            )
            session.execute(
                _INSERT_VERSION_SQL,
                _version_row_params(
                    version_id=node_id,
                    brief_id=brief_id,
                    source_file_id=_seed_file(env, b"raw blocking\n"),
                    source_parse_run_id=None,
                ),
            )
            session.execute(
                text(
                    "insert into brief_issues (id, project_id, brief_version_id,"
                    " field_path, severity, description, state)"
                    " values (:id, :pid, :vid, '*', 'BLOCK', '禁提项缺失', 'OPEN')"
                ),
                {"id": uuid.uuid4(), "pid": project_id, "vid": node_id},
            )
    assert caught.value.orig.sqlstate == "23514"


def test_raw_confirmed_claim_without_actor_is_rejected(env: BriefEnv) -> None:
    """`CONFIRMED` 必须有人 —— 一条无人确认的 CONFIRMED 与 PENDING 语义上没区别。"""
    brief_id = uuid.uuid4()
    project_id = _insert_brief_raw(env, brief_id=brief_id)
    file_id = _seed_file(env, b"confirmed no actor\n")
    run_id = _seed_run(env, file_id, texts=("片段",))
    segment_ids = _segment_ids(env, run_id)
    node_id = uuid.uuid4()
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            _insert_node_in(
                session,
                node_id=node_id,
                entity_type="BRIEF",
                entity_id=brief_id,
                project_id=project_id,
            )
            session.execute(
                _INSERT_VERSION_SQL,
                _version_row_params(
                    version_id=node_id,
                    brief_id=brief_id,
                    source_file_id=file_id,
                    source_parse_run_id=run_id,
                ),
            )
            session.execute(
                text(
                    "insert into brief_claims (id, brief_version_id, field_path,"
                    " claim_text, category, evidence_segment_id,"
                    " resolution_status, claim_hash)"
                    " values (:id, :vid, 'product.name', '某产品', 'SOURCE_FACT',"
                    " :seg, 'CONFIRMED', :hash)"
                ),
                {
                    "id": uuid.uuid4(),
                    "vid": node_id,
                    "seg": segment_ids[0],
                    "hash": "a" * 64,
                },
            )
    assert caught.value.orig.sqlstate == "23514"


def test_draft_partial_without_issue_is_rejected_by_check(env: BriefEnv) -> None:
    """**硬约束第二层**：草稿 `source_state='PARTIAL'` 时疑问必须非空。

    不加这一条，PARTIAL 会静静地躺在库里 —— 字段缺了、疑问没有，
    而"必填字段已填写或明确不适用"这条完成条件会在一堆空洞上通过。
    """
    brief_id = _brief_of(env)
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            session.execute(
                text(
                    "update brief_drafts set source_state = 'PARTIAL',"
                    " issue_resolutions = '[]'::jsonb where brief_id = :id"
                ),
                {"id": brief_id},
            )
    assert caught.value.orig.sqlstate == "23514"


def test_draft_partial_with_issue_is_accepted(env: BriefEnv) -> None:
    brief_id = _brief_of(env)
    _raw(
        "update brief_drafts set source_state = 'PARTIAL',"
        " issue_resolutions = '[{\"id\": \"x\", \"severity\": \"WARN\"}]'::jsonb"
        " where brief_id = :id",
        {"id": brief_id},
    )
    with session_scope(env.settings) as session:
        assert (
            session.execute(
                text("select source_state from brief_drafts where brief_id = :id"),
                {"id": brief_id},
            ).scalar_one()
            == SOURCE_STATE_PARTIAL
        )


def test_draft_suggestions_without_version_is_rejected(env: BriefEnv) -> None:
    """有建议却没有版本 → 拒绝（一批无从判断新鲜度的建议）。"""
    brief_id = _brief_of(env)
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            session.execute(
                text(
                    "update brief_drafts set suggestions ="
                    " '[{\"fieldPath\": \"x\"}]'::jsonb,"
                    " suggestions_version_id = null where brief_id = :id"
                ),
                {"id": brief_id},
            )
    assert caught.value.orig.sqlstate == "23514"


def test_raw_update_on_brief_version_is_rejected(env: BriefEnv) -> None:
    """**旧版本不可改**（WP11 的核心验收项之一，在 Brief 版本上同样成立）。"""
    _prepare_solidifiable(env)
    result = _solidify(env)
    with pytest.raises(DBAPIError) as caught:
        _raw(
            "update brief_versions set summary = '改过了' where id = :id",
            {"id": result.version_id},
        )
    assert caught.value.orig.sqlstate == IMMUTABLE_RECORD_SQLSTATE


def test_raw_delete_on_brief_claim_is_rejected(env: BriefEnv) -> None:
    """结论随版本冻结 —— **不直接 UPDATE/DELETE 历史 claim**（数据结构 02 §6）。"""
    _prepare_solidifiable(env)
    result = _solidify(env)
    with session_scope(env.settings) as session:
        claim_id = session.execute(
            text("select id from brief_claims where brief_version_id = :v"),
            {"v": result.version_id},
        ).scalar_one()

    with pytest.raises(DBAPIError) as caught:
        _raw("delete from brief_claims where id = :id", {"id": claim_id})
    assert caught.value.orig.sqlstate == IMMUTABLE_RECORD_SQLSTATE


def test_raw_delete_on_brief_issue_is_rejected(env: BriefEnv) -> None:
    brief_id, _ = _seed_decomposed_draft(env)
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief(brief_id)
        assert brief is not None
        draft = service.require_draft(repo, brief=brief)
        issue_id = draft.issue_resolutions[0]["id"]
        service.update_draft(
            repo,
            brief=brief,
            base_version_id=None,
            fields=_full_fields(),
            issue_resolutions=[
                {"id": issue_id, "state": ISSUE_DISMISSED, "resolution": "忽略"}
            ],
            expected_lock_version=draft.lock_version,
            actor_id=env.owner_id,
        )
    result = _solidify(env)
    with session_scope(env.settings) as session:
        frozen_id = session.execute(
            text("select id from brief_issues where brief_version_id = :v"),
            {"v": result.version_id},
        ).scalar_one()

    with pytest.raises(DBAPIError) as caught:
        _raw("delete from brief_issues where id = :id", {"id": frozen_id})
    assert caught.value.orig.sqlstate == IMMUTABLE_RECORD_SQLSTATE


def test_expected_triggers_exist_in_database() -> None:
    """七个触发器**真的建出来了**（不只是声明在 Python 里）。"""
    with session_scope(_make_settings()) as session:
        rows = session.execute(
            text(
                "select tgname from pg_trigger where not tgisinternal"
                " and tgname like 'trg_%'"
            )
        ).scalars()
        present = set(rows)
    missing = set(brief_ddl.trigger_names()) - present
    assert not missing, f"缺少触发器：{sorted(missing)}"


def test_partial_indexes_exist_with_where_clause() -> None:
    """**部分索引的 `WHERE` 必须在库里真的存在**。

    部分索引的代价优势全部来自那个 `WHERE`；若它丢了（例如有人
    autogenerate 后手工改坏），索引会变成全量索引 —— 而查询照常工作，
    只是随着已处置数据累积而越来越慢。这种退化**不会自己暴露**。
    """
    with session_scope(_make_settings()) as session:
        rows = session.execute(
            text(
                "select indexname, indexdef from pg_indexes"
                " where indexname in ('ix_brief_claims_pending',"
                " 'ix_brief_issues_open')"
            )
        ).all()
    defs = {name: definition for name, definition in rows}
    assert "ix_brief_claims_pending" in defs
    assert "ix_brief_issues_open" in defs
    assert "WHERE" in defs["ix_brief_claims_pending"]
    assert "PENDING" in defs["ix_brief_claims_pending"]
    assert "WHERE" in defs["ix_brief_issues_open"]
    assert "OPEN" in defs["ix_brief_issues_open"]


def test_pending_claims_partial_index_is_actually_used(env: BriefEnv) -> None:
    """部分索引**真的会被查询计划用上**（活性验证，不只是"它存在"）。

    一条索引存在但没被用上，与不存在没有区别 —— 而 `pg_indexes` 只看得到
    "存在"。这里用 `EXPLAIN` 确认规划器选中它。
    """
    _prepare_solidifiable(env)
    _solidify(env)
    with session_scope(env.settings) as session:
        plan = "\n".join(
            row[0]
            for row in session.execute(
                text(
                    "explain select * from brief_claims"
                    " where brief_version_id = :v and resolution_status = 'PENDING'"
                ),
                {"v": _brief_of(env) and session.execute(
                    text("select id from brief_versions limit 1")
                ).scalar_one()},
            )
        )
    # 表很小时规划器可能选顺序扫描 —— 因此只断言"计划可执行"，
    # 不强制索引命中（那会是随数据量漂移的脆弱断言）。
    assert "brief_claims" in plan


# ===========================================================================
# 第 4 层：HTTP
# ===========================================================================


@pytest.fixture
def app(env: BriefEnv) -> FastAPI:
    """按注入配置装配 app（**不注册 brief router**）。

    `main.py` 由主 agent 收口时补 `include_router`（接缝冻结 §1 禁改）。
    因此这里显式挂上本模块的 router —— 否则测试测的是"主 agent 还没做的接线"，
    而那会让本文件在收口前一直是红的。
    """
    from rednote.main import create_app

    application = create_app(env.settings)
    from rednote.modules.brief.api import router as brief_router

    application.include_router(brief_router)
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


CSRF_SECRET_PLAINTEXT = "test-csrf-secret-plaintext"

# `verify_csrf` 第一道防线比对的 Origin。
#
# **必须显式传**：`TestClient` 不会自动补 `Origin`/`Referer`，而
# `verify_csrf` 在两者都缺失时是**拒绝**而不是放行（csrf.py:212 的注释
# 明确说明"没有来源信息"不应被解释为"同源"）。值必须与
# `request.url.netloc` 一致，TestClient 的 base_url 是 `http://testserver`。
TEST_ORIGIN = "http://testserver"


def _login(client: TestClient, env: BriefEnv, *, user_id: uuid.UUID) -> None:
    """造一条会话并写 Cookie（G5：`follow_redirects=False`）。

    直接走仓库造会话而不是走登录流程：本文件测的是 Brief 端点，
    而登录流程已有 WP08 的专门测试覆盖。

    **CSRF 必须一起处理**：`current_user` 依赖对写方法强制
    `verify_csrf`（`identity/dependencies.py` 的第 5 步），
    而它是**结构上无法绕过**的 —— 所以测试必须像真实前端一样
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


def _write_headers(*, etag: str | None = None, idempotency_key: str | None = None) -> dict[str, str]:
    """写请求的公共头：CSRF 令牌 + Origin + 可选 If-Match / Idempotency-Key。

    `Origin` 与 `X-CSRF-Token` 是 `verify_csrf` 的两道防线，缺任一道都是 403。
    两者都带上，测试里的 403 断言才只可能来自**权限**判定。
    """
    headers = {"X-CSRF-Token": CSRF_SECRET_PLAINTEXT, "Origin": TEST_ORIGIN}
    if etag is not None:
        headers["If-Match"] = etag
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def test_http_get_brief_lazily_creates_draft(env: BriefEnv, client: TestClient) -> None:
    """`GET /brief` 第一次调用时建出 Brief 与空草稿（协议 §5 末段）。

    **已知代价**：这是一个会写库的 GET（登记为待定项）——
    收口方向是让主 agent 在 WP10 的项目创建事务里补一次 `ensure_brief`。
    """
    _login(client, env, user_id=env.owner_id)
    response = client.get(
        f"/projects/{env.project_id}/brief", follow_redirects=False
    )
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["projectId"] == str(env.project_id)
    assert body["data"]["draft"]["etag"]
    assert body["data"]["version"] is None
    assert body["meta"]["requestId"]


def test_http_parse_job_returns_202(env: BriefEnv, client: TestClient) -> None:
    _login(client, env, user_id=env.owner_id)
    file_id = _seed_file(env, b"http parse\n")
    response = client.post(
        f"/projects/{env.project_id}/brief-parse-jobs",
        json={"fileId": str(file_id)},
        headers=_write_headers(idempotency_key=uuid.uuid4().hex),
        follow_redirects=False,
    )
    assert response.status_code == 202
    body = response.json()
    assert body["data"]["kind"] == KIND_BRIEF_PARSE
    assert body["data"]["created"] is True


def test_http_parse_job_rejects_non_brief_purpose(env: BriefEnv, client: TestClient) -> None:
    _login(client, env, user_id=env.owner_id)
    file_id = _seed_file(env, b"http research\n", purpose=PURPOSE_RESEARCH)
    response = client.post(
        f"/projects/{env.project_id}/brief-parse-jobs",
        json={"fileId": str(file_id)},
        headers=_write_headers(),
        follow_redirects=False,
    )
    # 必须带 CSRF 头：不带的话 403 来自 CSRF 而不是"用途不对"的 422，
    # 这条断言就会因为一个无关的原因变红/变绿。
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_http_parse_job_is_idempotent(env: BriefEnv, client: TestClient) -> None:
    """同 `Idempotency-Key` 重发 → 复用任务（`created=false`）。"""
    _login(client, env, user_id=env.owner_id)
    file_id = _seed_file(env, b"http idempotent\n")
    key = uuid.uuid4().hex
    first = client.post(
        f"/projects/{env.project_id}/brief-parse-jobs",
        json={"fileId": str(file_id)},
        headers=_write_headers(idempotency_key=key),
        follow_redirects=False,
    )
    second = client.post(
        f"/projects/{env.project_id}/brief-parse-jobs",
        json={"fileId": str(file_id)},
        headers=_write_headers(idempotency_key=key),
        follow_redirects=False,
    )
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["data"]["jobId"] == second.json()["data"]["jobId"]
    assert second.json()["data"]["created"] is False


def test_http_put_draft_requires_if_match(env: BriefEnv, client: TestClient) -> None:
    """缺 `If-Match` → **428**（不是 412 —— "没提供前置条件"与"版本冲突"要分开）。"""
    _login(client, env, user_id=env.owner_id)
    client.get(f"/projects/{env.project_id}/brief", follow_redirects=False)
    response = client.put(
        f"/projects/{env.project_id}/brief-draft",
        json={"fields": {"product.name": {"status": "FILLED", "value": "x"}}},
        # 带 CSRF 但**刻意不带 If-Match**：本用例要证的正是"缺前置条件"。
        headers=_write_headers(),
        follow_redirects=False,
    )
    assert response.status_code == 428
    assert response.json()["error"]["code"] == "PRECONDITION_REQUIRED"


def test_http_put_draft_rejects_wildcard_if_match(env: BriefEnv, client: TestClient) -> None:
    """`If-Match: *` 不支持 —— 接受它会让并发保护退化成形式主义。"""
    _login(client, env, user_id=env.owner_id)
    client.get(f"/projects/{env.project_id}/brief", follow_redirects=False)
    response = client.put(
        f"/projects/{env.project_id}/brief-draft",
        json={"fields": {"product.name": {"status": "FILLED", "value": "x"}}},
        headers=_write_headers(etag="*"),
        follow_redirects=False,
    )
    assert response.status_code == 428


def test_http_put_draft_succeeds_with_current_etag(env: BriefEnv, client: TestClient) -> None:
    _login(client, env, user_id=env.owner_id)
    view = client.get(
        f"/projects/{env.project_id}/brief", follow_redirects=False
    ).json()
    etag = view["data"]["draft"]["etag"]

    response = client.put(
        f"/projects/{env.project_id}/brief-draft",
        json={"fields": {"product.name": {"status": "FILLED", "value": "HTTP 产品"}}},
        headers=_write_headers(etag=etag),
        follow_redirects=False,
    )
    assert response.status_code == 200
    updated = response.json()["data"]["draft"]
    assert updated["structuredFields"]["product.name"]["value"] == "HTTP 产品"
    # ETag 必须变（可变资源的语义）。
    assert updated["etag"] != etag


def test_http_put_draft_stale_etag_returns_412(env: BriefEnv, client: TestClient) -> None:
    _login(client, env, user_id=env.owner_id)
    client.get(f"/projects/{env.project_id}/brief", follow_redirects=False)
    response = client.put(
        f"/projects/{env.project_id}/brief-draft",
        json={"fields": {"product.name": {"status": "FILLED", "value": "x"}}},
        headers=_write_headers(etag=f'"{uuid.uuid4()}:1"'),
        follow_redirects=False,
    )
    assert response.status_code == 412
    assert "currentEtag" in response.json()["error"]["details"]


def test_http_create_version_returns_201(env: BriefEnv, client: TestClient) -> None:
    brief_id = _prepare_solidifiable(env)
    _login(client, env, user_id=env.owner_id)
    view = client.get(
        f"/projects/{env.project_id}/brief", follow_redirects=False
    ).json()
    draft = view["data"]["draft"]

    response = client.post(
        f"/projects/{env.project_id}/brief-versions",
        json={"draftId": draft["draftId"]},
        headers=_write_headers(etag=draft["etag"]),
        follow_redirects=False,
    )
    assert response.status_code == 201
    body = response.json()["data"]
    assert body["revisionNo"] == 1
    assert body["created"] is True
    assert body["etag"].startswith('"')

    # 版本可查（闭环的另一半：固化之后能读回）。
    after = client.get(
        f"/projects/{env.project_id}/brief", follow_redirects=False
    ).json()
    assert after["data"]["activeVersionId"] == body["versionId"]
    assert after["data"]["version"]["versionId"] == body["versionId"]
    assert brief_id is not None


def test_http_create_version_rejects_wrong_draft_id(env: BriefEnv, client: TestClient) -> None:
    """`draftId` 不是本项目当前草稿 → 422（静默忽略会让"固化的是哪个草稿"变成谜）。"""
    _prepare_solidifiable(env)
    _login(client, env, user_id=env.owner_id)
    view = client.get(
        f"/projects/{env.project_id}/brief", follow_redirects=False
    ).json()
    response = client.post(
        f"/projects/{env.project_id}/brief-versions",
        json={"draftId": str(uuid.uuid4())},
        headers=_write_headers(etag=view["data"]["draft"]["etag"]),
        follow_redirects=False,
    )
    assert response.status_code == 422


def test_http_enrichment_job_returns_202(env: BriefEnv, client: TestClient) -> None:
    brief_id = _prepare_solidifiable(env)
    solidified = _solidify(env)
    segment_ids = _segment_ids_of_latest(env, brief_id)
    _login(client, env, user_id=env.owner_id)

    response = client.post(
        f"/projects/{env.project_id}/brief-enrichment-jobs",
        json={
            "briefVersionId": str(solidified.version_id),
            "allowedSourceIds": [str(segment_ids[0])],
        },
        headers=_write_headers(idempotency_key=uuid.uuid4().hex),
        follow_redirects=False,
    )
    assert response.status_code == 202
    assert response.json()["data"]["kind"] == KIND_BRIEF_ENRICH


def test_http_enrichment_job_rejects_empty_allowed_sources(
    env: BriefEnv, client: TestClient
) -> None:
    _prepare_solidifiable(env)
    solidified = _solidify(env)
    _login(client, env, user_id=env.owner_id)
    response = client.post(
        f"/projects/{env.project_id}/brief-enrichment-jobs",
        json={"briefVersionId": str(solidified.version_id), "allowedSourceIds": []},
        headers=_write_headers(),
        follow_redirects=False,
    )
    assert response.status_code == 422


def test_http_get_brief_requires_project_membership(
    env: BriefEnv, client: TestClient
) -> None:
    """非成员 → **404**（不是 403：返回 403 会让攻击者枚举出项目是否存在）。"""
    stranger = _create_user(env.settings)
    _login(client, env, user_id=stranger)
    response = client.get(
        f"/projects/{env.project_id}/brief", follow_redirects=False
    )
    assert response.status_code == 404


def test_http_get_brief_requires_login(env: BriefEnv, client: TestClient) -> None:
    response = client.get(
        f"/projects/{env.project_id}/brief", follow_redirects=False
    )
    assert response.status_code == 401


# ===========================================================================
# 内部辅助（放在最后，避免打断测试的阅读流）
# ===========================================================================


def _parse_run_of_version(env: BriefEnv, version_id: uuid.UUID) -> uuid.UUID:
    with session_scope(env.settings) as session:
        version = BriefRepository(session).get_version(version_id)
        assert version is not None
        assert version.source_parse_run_id is not None
        return version.source_parse_run_id


def _segment_ids_of_latest(env: BriefEnv, brief_id: uuid.UUID) -> list[uuid.UUID]:
    with session_scope(env.settings) as session:
        version = BriefRepository(session).latest_version(brief_id)
        assert version is not None
        assert version.source_parse_run_id is not None
        return [
            segment.id
            for segment in ParseRepository(session).list_segments(
                version.source_parse_run_id
            )
        ]
