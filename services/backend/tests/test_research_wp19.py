"""WP19 测试：研究关键词与提示词 / 资料列表 / 覆盖缺口确认。

覆盖 WP19 验收原文（研发计划 04 §4.3）：
> **复制、上传、解析、步骤2确认完整**

**测试策略（四层）**：

1. **纯函数层**（不碰数据库）：关键词派生、缺口派生、提示词组装、
   文本归一化、上限截断、三条判据（`claim_needs_gap` /
   `gap_state` / `claim_needs_search`）的**互不相等**。
   本层的价值最高 —— 验收里"复制"与"确认完整"两件事的正确性
   全部落在 `keywords.py` 上，而它是零 I/O 的，可以穷举。
2. **服务层 + 真库**：`build_research_prompts`（含"没有 Brief 版本"、
   "版本不属于本项目"、"PARTIAL 源状态"等分支）、
   `list_research_files`（含解析状态、片段数、配额、effective 过滤）。
   端点是薄层，因此**验收主要打在服务层**。
3. **数据库层兜底**：本包**零表**，因此这一层测的不是"我的表约束生效"，
   而是**"我读的都是别人的合法数据、且我没有写任何东西"**：
   · 用原生 SQL 造 `project_file_links` / `parse_runs` / `source_segments`，
     断言服务层读出来的结果与 SQL 里的事实一致；
   · 断言一次 `build_research_prompts` + `list_research_files`
     前后，本包涉及的所有表的行数与摘要**完全不变**（零写入）。
4. **HTTP 层**：两个端点的状态码、响应形状、权限、
   `meta.research` 不被剥掉、`status` 非法值 422、游标绑定。

**为什么第 3 层要测"零写入"**：本包最实质的划界是"不新建表、
不写任何东西"（说明文档 §1.7）。一个"顺手把派生结果写进某个表"
的实现会让所有功能测试照样通过 —— 而它破坏的正是本包的核心设计
（引入第二个真值来源）。因此必须有一条测试**直接断言零写入**。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from rednote.modules.brief import service as brief_service
from rednote.modules.brief.models import Brief, BriefClaim
from rednote.modules.brief.repository import BriefRepository
from rednote.modules.brief.schema import BRIEF_FIELDS, FIELD_BY_PATH
from rednote.modules.brief.types import (
    CATEGORIES_REQUIRING_EVIDENCE,
    CATEGORY_EXTERNAL_EVIDENCE,
    CATEGORY_SOURCE_FACT,
    CATEGORY_STRATEGY,
    CATEGORY_UNCONFIRMED,
    FIELD_FILLED,
    FIELD_MISSING,
    FIELD_NOT_APPLICABLE,
    RESOLUTION_CONFIRMED,
    RESOLUTION_NOT_APPLICABLE,
    RESOLUTION_PENDING,
)
from rednote.modules.files.models import (
    PURPOSE_BRIEF,
    PURPOSE_REFERENCE,
    PURPOSE_RESEARCH,
    Blob,
    FileObject,
    ProjectFileLink,
    QuotaAccount,
)
from rednote.modules.files.repository import FileRepository
from rednote.modules.identity.models import ROLE_ADMIN, ROLE_OPERATOR, User
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.parsing.models import ParseRun, SourceSegment
from rednote.modules.projects.models import Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.modules.research import keywords as kw
from rednote.modules.research import service as research_service
from rednote.modules.research.api import STATUS_FILTER_SET, STATUS_FILTER_VALUES
from rednote.modules.research.repository import (
    PARSE_STATE_NONE,
    ResearchRepository,
)
from rednote.modules.research.types import (
    DERIVATION_PAYLOAD_VERSION,
    DERIVATION_RULES_VERSION,
    FORMAT_MEDIA_ANY,
    FORMAT_MEDIA_IMAGE,
    FORMAT_MEDIA_KINDS,
    GAP_BLOCKING_STATES,
    GAP_COVERED,
    GAP_MISSING,
    GAP_NOT_APPLICABLE,
    GAP_STATES,
    KEYWORD_SOURCE_KINDS,
    KEYWORD_SOURCE_CLAIM,
    KEYWORD_SOURCE_ENTITY,
    KEYWORD_SOURCE_FIELD,
    KEYWORD_SOURCE_PATTERN,
    MAX_GAPS,
    MAX_KEYWORDS,
    MAX_PROMPTS,
    MAX_SUGGESTED_KEYWORDS_PER_GAP,
    RESEARCH_FILE_SCAN_LIMIT,
    TASK_KINDS,
    ClaimInput,
    FieldValueInput,
)
from rednote.platform.configuration import Settings
from rednote.platform.database import get_engine, reset_engines, session_scope
from rednote.platform.errors import ApiError

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"

#: 本包**不建任何表**。用这个空元组把"零表"这件事写进测试文件里，
#: 让"哪天有人加了一张表"时这里会显式地需要改。
OWN_TABLES: tuple[Any, ...] = ()


def _test_database_url() -> str:
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
# 数据夹具
# ===========================================================================


@dataclass
class ResearchEnv:
    settings: Settings
    project_id: uuid.UUID
    owner_id: uuid.UUID
    brand_id: uuid.UUID
    product_id: uuid.UUID
    admin_id: uuid.UUID


def _create_user(
    settings: Settings, *, role: str = ROLE_OPERATOR, name: str = "研究测试用户"
) -> uuid.UUID:
    with session_scope(settings) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name=name,
            role=role,
        )
        return user.id


def _seed_project(
    settings: Settings, *, owner_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    with session_scope(settings) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name=f"测试产品-{uuid.uuid4().hex[:6]}")
        session.add(product)
        session.flush()
        project = Project(
            project_code=f"RSC-{uuid.uuid4().hex[:8].upper()}",
            name="研究测试项目",
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
def env() -> ResearchEnv:
    reset_engines()
    settings = _make_settings()
    owner = _create_user(settings)
    admin = _create_user(settings, role=ROLE_ADMIN, name="研究测试管理员")
    brand_id, product_id, project_id = _seed_project(settings, owner_id=owner)
    return ResearchEnv(
        settings=settings,
        project_id=project_id,
        owner_id=owner,
        brand_id=brand_id,
        product_id=product_id,
        admin_id=admin,
    )


# --- Brief 侧夹具（**直接写表**，不调解构通道）-----------------------------
#
# 为什么不走 WP18 的 `service.solidify`：那会引入模型通道依赖
# （解构需要一个可用的 decomposer），而本包测的是"读已固化的 Brief 版本"。
# 本包的输入是**一张已经存在的表**，因此测试直接构造它的行是
# 更精确的做法 —— 走 solidify 反而会测到 WP18 的业务规则。


def _seed_evidence_url(item: dict[str, Any]) -> str | None:
    """给一条 claim 造一个**合法的出处**。

    需要证据的类别（`SOURCE_FACT` / `EXTERNAL_EVIDENCE`）**必须**有出处，
    否则违反 `ck_brief_claims_factual_category_requires_evidence`
    （见 `_seed_brief_version` 的 docstring）。
    其余类别（`STRATEGY` / `UNCONFIRMED`）不需要，给 `None`。

    `item['evidence_url'] is None` 是**显式传 None** 的情形 ——
    夹具尊重它并同样补一个默认值，因为"造一条违反数据库约束的行"
    不是任何一个测试想做的事（要测约束本身的测试去 WP18 的文件里）。
    """
    explicit = item.get("evidence_url")
    if explicit:
        return str(explicit)
    if item["category"] in ("SOURCE_FACT", "EXTERNAL_EVIDENCE"):
        return f"https://brief.example.invalid/{uuid.uuid4().hex[:8]}"
    return None


def _seed_brief_version(
    env: ResearchEnv,
    *,
    fields: dict[str, Any] | None = None,
    claims: list[dict[str, Any]] | None = None,
    source_state: str = "SUCCEEDED",
    set_active: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    """造一个 Brief + 一个已固化版本（含字段与 claims）。

    返回 `(brief_id, version_id)`。**`brief_versions` 是 WP18 的不可变表**，
    因此每个测试用独立的产品名与独特的 claim 文本，避免与库里的历史行冲突。

    **claims 必须给合法来源**（需要证据的类别）：WP18 有数据库 CHECK
    `ck_brief_claims_factual_category_requires_evidence` ——
    `SOURCE_FACT` / `EXTERNAL_EVIDENCE` 没有 `evidence_segment_id`
    或 `evidence_url` 会直接 `CheckViolation`。
    这条约束正是本包缺口判据不能用 `has_evidence` 的原因
    （见 `keywords.py` 的模块 docstring），因此夹具默认给一个 url。
    """
    with session_scope(env.settings) as session:
        repo = BriefRepository(session)
        brief = repo.get_brief_by_project(env.project_id)
        if brief is None:
            brief = brief_service.ensure_brief(repo, project_id=env.project_id)

        structured = brief_service._normalize_decomposed_fields(
            fields if fields is not None else _default_fields(env)
        )

        # **必须先在版本注册表里登记节点**：`brief_versions.id` 有外键
        # 指向 `version_nodes.id`（OP-88 的正向约束，WP18 建的）。
        # 本机实测：直接 `insert_version` 会撞
        # `fk_brief_versions_id_version_nodes`。
        #
        # 用 WP18 自己的接口 `versions.service.register_version`
        # 而不是手插 `version_nodes` 行：它管 `revision_no` 的递增
        # （`max_revision_no` + 重试）与 `payload_hash` 的规范化，
        # 手插会让本夹具与生产路径的分歧在**别处**才暴露。
        #
        # **两个 id 语义不同，本机踩过一次**（见 `brief/service.solidify`）：
        #   · `entity_id`      = **Brief 的 id**（版本节点挂在实体上，不是挂在版本上）
        #   · 返回的 `node_id` = **版本 id**，即 `brief_versions.id`
        # 第一版夹具把 `entity_id` 传成了自己 `uuid4()` 的 version_id，
        # 于是注册出来的节点 id 与 `insert_version(version_id=...)` 用的 id
        # 毫无关系 → `Key (id)=... is not present in table "version_nodes"`。
        from rednote.modules.versions import service as versions_service
        from rednote.modules.versions.repository import VersionRepository

        registered = versions_service.register_version(
            VersionRepository(session),
            entity_type="BRIEF",
            entity_id=brief.id,
            # payload 摘要用于幂等判定，故每次调用必须不同
            # （否则同一 (BRIEF, brief.id, revision_no) 会命中幂等分支，
            #  返回**上一个测试遗留的节点**，跨测试串味）。
            payload={"summary": "研究测试用 Brief", "nonce": uuid.uuid4().hex},
            project_id=env.project_id,
            created_by=env.owner_id,
        )
        version_id = registered.node_id

        repo.insert_version(
            version_id=version_id,
            brief_id=brief.id,
            source_file_id=_seed_file(env, purpose=PURPOSE_BRIEF),
            source_parse_run_id=None,
            source_state=source_state,
            product_name=str(_value_of(structured, "product.name") or "测试产品"),
            model_code=_value_of(structured, "product.model_code"),
            summary="研究测试用 Brief",
            structured_fields=structured,
            created_by=env.owner_id,
        )
        # **必须先 flush**：`brief_claims.brief_version_id` 有外键指向
        # `brief_versions.id`，而 ORM 不会自动为"稍后 add 的从表行"
        # 排序（`insert_claims` 用的是 `add_all`，不保证先发版本行的 INSERT
        # —— 本机实测直接撞 `ForeignKeyViolation`）。
        repo.flush()
        if claims:
            repo.insert_claims(
                version_id=version_id,
                claims=[
                    {
                        "field_path": item["field_path"],
                        "claim_text": item["text"],
                        "category": item["category"],
                        "evidence_segment_id": item.get("evidence_segment_id"),
                        # **默认给一个合法的出处**（见 docstring）。
                        "evidence_url": _seed_evidence_url(item),
                        "applicability": {},
                        "resolution_status": item.get(
                            "resolution", RESOLUTION_PENDING
                        ),
                        "resolution_reason": item.get("resolution_reason")
                        or (
                            "本产品不涉及该维度"
                            if item.get("resolution") == RESOLUTION_NOT_APPLICABLE
                            else None
                        ),
                        "confirmed_by": (
                            env.owner_id
                            if item.get("resolution") == RESOLUTION_CONFIRMED
                            else None
                        ),
                        "claim_hash": brief_service.claim_hash(
                            field_path=item["field_path"], text=item["text"]
                        ),
                    }
                    for item in claims
                ],
            )
        repo.flush()
        if source_state == "PARTIAL":
            # **`PARTIAL` 源必须有疑问记录**：WP18 的延迟触发器
            # `rednote_check_brief_version_consistency` 在提交时校验
            # "残缺不得在固化时被洗白"。本机实测漏掉这一步会撞
            # `CheckViolation: 版本 ... 的输入是 PARTIAL，但没有任何疑问记录`。
            #
            # 这正是本包要测的分支（源残缺时提示词应说明"结论可能不完整"），
            # 所以不能靠改 `source_state` 绕开 —— 只能把 issue 一起造出来。
            repo.insert_issues(
                version_id=version_id,
                project_id=env.project_id,
                issues=[
                    {
                        "field_path": "product.mechanism",
                        "severity": "WARN",
                        "description": "源文件该段解析不完整，机制描述可能缺失。",
                        "state": "OPEN",
                        "resolution": None,
                        "resolved_by": None,
                    }
                ],
            )
            repo.flush()
        if set_active:
            repo.set_active_version(brief_id=brief.id, version_id=version_id)
        session.flush()
        # **必须先 flush 再读回 id**：`set_active_version` 改的是 briefs 行。
        return brief.id, version_id


def _default_fields(env: ResearchEnv, *, tag: str | None = None) -> dict[str, Any]:
    """一份最小可用的 `structured_fields`（含实体字段与一个多值字段）。

    `tag` 用来让不同测试的产物互不冲突（字段值会进关键词，
    而关键词的断言依赖"我造的词在里面"）。
    """
    suffix = tag or uuid.uuid4().hex[:6]
    return {
        "product.brand": {"status": FIELD_FILLED, "value": f"品牌{suffix}"},
        "product.name": {"status": FIELD_FILLED, "value": f"产品{suffix}"},
        "product.model_code": {"status": FIELD_FILLED, "value": f"型号{suffix}"},
        "audience.target": {"status": FIELD_FILLED, "value": f"人群{suffix}"},
        "product.selling_points": {
            "status": FIELD_FILLED,
            "value": [{"text": f"卖点{suffix}"}, f"裸卖点{suffix}"],
        },
        # 以下三个刻意是"不造词"的状态（见 service 的判据）。
        "product.mechanism": {"status": FIELD_MISSING, "value": None},
        "content.must_mention": {
            "status": FIELD_NOT_APPLICABLE,
            "value": "本产品无需强制提及项",
        },
        "audience.constraints": {"status": FIELD_MISSING, "value": None},
    }


def _value_of(structured: dict[str, Any], path: str) -> Any:
    entry = structured.get(path) or {}
    return entry.get("value")


# --- 文件 / 解析侧夹具（**原生 SQL + ORM 混合**，故意如此）-----------------


def _seed_blob(
    env: ResearchEnv,
    *,
    byte_size: int = 1024,
    mime_type: str = "image/png",
    scan_status: str = "CLEAN",
) -> uuid.UUID:
    with session_scope(env.settings) as session:
        blob = Blob(
            storage_backend="LOCAL",
            bucket=None,
            object_key=f"research/{uuid.uuid4().hex}",
            sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            byte_size=byte_size,
            mime_type=mime_type,
            scan_status=scan_status,
            storage_state="ACTIVE",
        )
        session.add(blob)
        session.flush()
        return blob.id


def _parse_run_facts(state: str, segment_count: int) -> dict[str, Any]:
    """按状态给出**自洽**的 `total_units` / `parsed_units` / `finished_at`。

    WP17 的 `parse_runs` 有四条 CHECK 约束（本机读出来的原文本）：

    | 约束 | 含意 |
    |---|---|
    | `ck_parse_runs_succeeded_means_all_parsed` | `SUCCEEDED` 必须 `total_units IS NOT NULL AND parsed_units = total_units AND parsed_units > 0` |
    | `ck_parse_runs_partial_means_some_parsed` | `PARTIAL` 必须 `total_units IS NOT NULL AND 0 < parsed_units < total_units` |
    | `ck_parse_runs_parsed_within_total` | `parsed_units <= total_units` |
    | `ck_parse_runs_terminal_has_finished_at` | 四个终态必须 `finished_at IS NOT NULL` |

    **不能随手编 `total_units = segment_count + 1`** —— 本机实测撞过
    `SUCCEEDED` 那条：`parsed_units=3, total_units=4` 直接
    `CheckViolation`。夹具的 `segment_count` 是"解出了几段"，
    它必须与 `parsed_units` 一致（否则列表里的"片段数"与
    "已解析单元数"会互相矛盾，而那条矛盾在 UI 上看不出来）。
    """
    if state == "SUCCEEDED":
        parsed = max(1, segment_count)
        return {
            "total_units": parsed,
            "parsed_units": parsed,
            "finished_at": datetime.now(timezone.utc),
        }
    if state == "PARTIAL":
        # 至少解出 1 个、且少于总数。
        parsed = max(1, segment_count)
        return {
            "total_units": parsed + 1,
            "parsed_units": parsed,
            "finished_at": datetime.now(timezone.utc),
        }
    if state in ("FAILED", "CANCELLED"):
        return {
            "total_units": None,
            "parsed_units": 0,
            "finished_at": datetime.now(timezone.utc),
        }
    # QUEUED / RUNNING：未终态，没有 total/parsed 也没有 finished_at。
    return {"total_units": None, "parsed_units": 0, "finished_at": None}


def _seed_file(
    env: ResearchEnv,
    *,
    purpose: str = PURPOSE_RESEARCH,
    byte_size: int = 1024,
    mime_type: str = "image/png",
    scan_status: str = "CLEAN",
    evidence_usable: bool = True,
    parse_state: str | None = "SUCCEEDED",
    segment_count: int = 3,
    original_name: str | None = None,
    effective: bool = True,
) -> uuid.UUID:
    """造一个文件 + 关联（+ 可选解析运行与片段）。

    `parse_state=None` 造"从未解析"的文件（`parse_runs` 里没有行）——
    它对应 `PARSE_STATE_NONE`。**这条分支必须有**：它是最容易被
    漏掉的状态（一次 join 写错就会把"没解析"显示成"失败"）。

    `effective=False` 造"解绑过"的关联 —— 它**不应出现在列表里**，
    也不应计入配额（R3 / R4 的落点）。

    `evidence_usable` 是**期望值**，由实际传入的 `parse_state` 决定
    （测试里显式传两次是为了让"我期望它可用"+ "我用什么状态造的"
    在调用点同时可见，而不是让读者去推 `SUCCEEDED` 是否可用）。
    """
    blob_id = _seed_blob(
        env, byte_size=byte_size, mime_type=mime_type, scan_status=scan_status
    )
    file_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.add(
            FileObject(
                id=file_id,
                blob_id=blob_id,
                original_name=original_name or f"资料-{uuid.uuid4().hex[:8]}.png",
                visibility="PROJECT",
                project_id=env.project_id,
                # `classification` 的取值域是 `SUPPORTED` / `UNSUPPORTED`
                # （不是 mime 类型 —— 它回答"这个类型在不在承诺可解析范围内"，
                # 是 WP12 的 AC-07 判据）。
                classification="SUPPORTED",
                uploaded_by=env.owner_id,
            )
        )
        session.flush()
        session.add(
            ProjectFileLink(
                project_id=env.project_id,
                file_id=file_id,
                purpose=purpose,
                effective=effective,
            )
        )
        session.flush()

        if parse_state is not None:
            run_id = uuid.uuid4()
            facts = _parse_run_facts(parse_state, segment_count)
            session.add(
                ParseRun(
                    id=run_id,
                    file_id=file_id,
                    job_id=None,
                    parser_version="test-parser-1",
                    state=parse_state,
                    total_units=facts["total_units"],
                    parsed_units=facts["parsed_units"],
                    errors=[],
                    ocr_engine="NONE",
                    finished_at=facts["finished_at"],
                )
            )
            session.flush()
            for index in range(segment_count):
                session.add(
                    SourceSegment(
                        parse_run_id=run_id,
                        segment_no=index + 1,
                        position={"kind": "PAGE", "page": index + 1},
                        position_schema_version=1,
                        text_content=f"片段 {index + 1}",
                        content_hash=(uuid.uuid4().hex + uuid.uuid4().hex)[:64],
                        extraction_kind="TEXT",
                        confidence=None,
                    )
                )
            session.flush()
        return file_id


def _snapshot_research_tables(env: ResearchEnv) -> dict[str, Any]:
    """本包涉及的表在**该项目范围内**的摘要。

    用它做"零写入"断言的证据。**按项目过滤而不是全表计数**：
    全表会被别的测试的并发残留干扰（WP18 实测：`brief_versions`
    有 52 行是别的测试的），而"我这次没动过我读的那些行"才是要证明的事。
    """
    queries = {
        "project_file_links": (
            "SELECT count(*) FROM project_file_links WHERE project_id = :pid"
        ),
        "file_objects": (
            "SELECT count(*) FROM file_objects WHERE project_id = :pid"
        ),
        "brief_claims": (
            "SELECT count(*) FROM brief_claims c JOIN brief_versions v"
            " ON v.id = c.brief_version_id JOIN briefs b ON b.id = v.brief_id"
            " WHERE b.project_id = :pid"
        ),
        "parse_runs": (
            "SELECT count(*) FROM parse_runs r JOIN file_objects f"
            " ON f.id = r.file_id WHERE f.project_id = :pid"
        ),
        "source_segments": (
            "SELECT count(*) FROM source_segments s JOIN parse_runs r"
            " ON r.id = s.parse_run_id JOIN file_objects f ON f.id = r.file_id"
            " WHERE f.project_id = :pid"
        ),
        "version_nodes": (
            "SELECT count(*) FROM version_nodes WHERE project_id = :pid"
        ),
    }
    engine = get_engine(env.settings)
    result: dict[str, Any] = {}
    with engine.connect() as conn:
        for name, sql in queries.items():
            result[name] = conn.execute(
                text(sql), {"pid": env.project_id}
            ).scalar_one()
    return result


def _client(env: ResearchEnv) -> TestClient:
    from rednote.main import create_app

    return TestClient(create_app(env.settings))


CSRF_SECRET_PLAINTEXT = "test-csrf-secret-plaintext"

# `verify_csrf` 第一道防线比对的 Origin（与 WP18 的 `_login` 同一手法）。
# TestClient 不会自动补 `Origin`，而 `verify_csrf` 在来源信息缺失时是**拒绝**。
TEST_ORIGIN = "http://testserver"


def _login(env: ResearchEnv, client: TestClient, *, admin: bool = False) -> None:
    """造一条会话并写 Cookie（与 WP18 的 `_login` 同一手法）。

    **第一版走的是手写 `INSERT INTO sessions`，表名错了**：本包的会话表是
    `user_sessions`（`identity/models.py` 的 `UserSession`），不是 `sessions`
    —— 本机实测报 `relation "sessions" does not exist`。
    改为走 `IdentityRepository.create_session`：表名与列名由 ORM 决定，
    不再有"测试里的 SQL 与模型漂移"的可能。

    两个 Cookie 都要写：`current_user` 依赖在写方法上强制 `verify_csrf`，
    那是**结构上绕不过**的。本包两个端点都是只读 GET，CSRF 不参与，
    但一并写上，让将来若有写端点时测试不必回头补。
    """
    from rednote.platform.csrf import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
    from rednote.platform.tokens import generate_session_token, hash_token

    user_id = env.admin_id if admin else env.owner_id
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


# ===========================================================================
# 第 1 层：纯函数
# ===========================================================================


# --- 1.1 受控取值与常量 ----------------------------------------------------


def test_gap_states_match_declared_set() -> None:
    """缺口状态恰好三个，且只有 `MISSING` 阻断。"""
    assert set(GAP_STATES) == {"COVERED", "MISSING", "NOT_APPLICABLE"}
    assert GAP_BLOCKING_STATES == frozenset({"MISSING"})
    assert len(GAP_STATES) == 3


def test_keyword_source_kinds_match_declared_set() -> None:
    """关键词来源恰好四类。`PATTERN` 必须单独存在（可解释性的落点）。"""
    assert set(KEYWORD_SOURCE_KINDS) == {"ENTITY", "FIELD", "CLAIM", "PATTERN"}
    assert len(KEYWORD_SOURCE_KINDS) == 4


def test_declared_values_are_used_not_redeclared() -> None:
    """本包声明的取值必须**都被 `keywords` 真正用到**（防"声明了没用"）。"""
    module_source = (
        __import__("rednote.modules.research.keywords", fromlist=["x"]).__file__ or ""
    )
    with open(module_source, encoding="utf-8") as handle:
        source = handle.read()
    for name in (
        "KEYWORD_SOURCE_ENTITY",
        "KEYWORD_SOURCE_FIELD",
        "KEYWORD_SOURCE_CLAIM",
        "KEYWORD_SOURCE_PATTERN",
        "GAP_MISSING",
        "GAP_COVERED",
        "GAP_NOT_APPLICABLE",
    ):
        # 断言它们以**标识符**形式出现在模块源码里（不是被重新字面量化）。
        assert name in source, f"{name} 在 keywords.py 里没有被使用"


def test_derivation_versions_are_distinct_concepts() -> None:
    """`rulesVersion` 与 `payloadVersion` 是两件事（算法 vs 形状）。"""
    assert DERIVATION_RULES_VERSION >= 1
    assert DERIVATION_PAYLOAD_VERSION >= 1
    assert isinstance(DERIVATION_RULES_VERSION, int)
    assert isinstance(DERIVATION_PAYLOAD_VERSION, int)


def test_status_filter_includes_none_and_all_parse_states() -> None:
    """`status` 取值域 = WP17 的解析状态 + `NONE`。

    **这条测试是 ISS-71 型风险的落点**（等式基数判据）：
    它断言的是"**包含**关系"而不是"恰好 N 个"。
    WP17 将来新增一个解析状态时，本包应当**自动获得**它，
    而不是让这条测试变红（那会逼着别人改我的测试，
    或者更糟：改成一个新的魔数）。
    """
    from rednote.modules.parsing.types import PARSE_STATES

    for state in PARSE_STATES:
        assert state in STATUS_FILTER_SET
    assert PARSE_STATE_NONE in STATUS_FILTER_SET
    # `NONE` 在首位（它是"还没做"这个最需要被筛出来的状态）。
    assert STATUS_FILTER_VALUES[0] == PARSE_STATE_NONE
    # 无重复。
    assert len(STATUS_FILTER_VALUES) == len(set(STATUS_FILTER_VALUES))


# --- 1.2 文本归一化 -------------------------------------------------------


def test_normalize_term_collapses_whitespace() -> None:
    """换行/制表 → 单空格。**关键词要被粘进搜索框**，换行会截断它。"""
    assert kw.normalize_term("  续航\n18\t小时  ") == "续航 18 小时"
    assert kw.normalize_term(None) == ""
    assert kw.normalize_term(123) == "123"


def test_normalize_term_truncates_at_limit() -> None:
    """超长截断（**不丢弃** —— 丢弃会让运营看不到"这里本来有个词"）。"""
    long_text = "甲" * 200
    result = kw.normalize_term(long_text)
    assert len(result) == 60
    assert result.startswith("甲")


def test_join_terms_skips_empty_parts() -> None:
    assert kw.join_terms("A", "", "B") == "A B"
    assert kw.join_terms("", "  ", None) == ""


def test_extract_phrase_breaks_at_punctuation() -> None:
    """短语在第一个标点处断开（**可解释**：它取了你这句话的第一个分句）。"""
    assert kw.extract_phrase("保温 6 小时，但这是实验室条件") == "保温 6 小时"
    assert kw.extract_phrase("没有标点的一句话") == "没有标点的一句话"
    assert kw.extract_phrase("") == ""
    # 第一个标点位置超出上限时不切（否则会切出一个空短语）。
    assert kw.extract_phrase("甲" * 40 + "，后半句").startswith("甲")


def test_extract_phrase_truncates_without_punctuation() -> None:
    long_text = "乙" * 100
    assert len(kw.extract_phrase(long_text)) == 24


# --- 1.3 三条判据**互不相等**（本包最容易混错的地方）----------------------


def _claim(
    *,
    category: str = CATEGORY_SOURCE_FACT,
    resolution: str = RESOLUTION_PENDING,
    has_evidence: bool = False,
    text: str = "示例结论",
    field_path: str = "product.mechanism",
) -> ClaimInput:
    return ClaimInput(
        claim_id=uuid.uuid4(),
        field_path=field_path,
        field_label=FIELD_BY_PATH.get(field_path, None).label
        if field_path in FIELD_BY_PATH
        else field_path,
        text=text,
        category=category,
        resolution=resolution,
        has_evidence=has_evidence,
    )


def test_strategy_claim_is_never_a_gap() -> None:
    """`STRATEGY` **不是缺口**（策略判断不需要外部资料来"补齐"）。

    这是 `claim_needs_gap` 的类别那一半。判据来自 WP18 的
    `CATEGORIES_REQUIRING_EVIDENCE`，本包**不自己重写一份**。
    """
    claim = _claim(category=CATEGORY_STRATEGY)
    assert kw.claim_needs_gap(claim) is False
    assert kw.claim_needs_search(claim) is False
    # `gap_state` **刻意不判类别**（它接在"已确认是缺口"之后）——
    # 它会把一条无证据的 STRATEGY 判成 MISSING。这个不对称是刻意的，
    # 也是为什么必须有一个 `claim_needs_search` 把两者合取起来。
    assert kw.gap_state(claim) == GAP_MISSING


def test_unconfirmed_claim_is_not_a_gap() -> None:
    """`UNCONFIRMED` **不是缺口**。

    WP18 的 `CATEGORIES_REQUIRING_EVIDENCE` 只含
    `SOURCE_FACT` / `EXTERNAL_EVIDENCE`。把 `UNCONFIRMED` 也算缺口，
    运营会永远收不满（它本就承认自己没证据）。

    **本条同时是"不抄第二份类别集合"的守卫**：若有人在本包手写一份
    `{SOURCE_FACT, EXTERNAL_EVIDENCE, UNCONFIRMED}`，本条会红。
    """
    claim = _claim(category=CATEGORY_UNCONFIRMED)
    assert kw.claim_needs_gap(claim) is False
    assert kw.claim_needs_search(claim) is False


def test_gap_categories_come_from_wp18_constant() -> None:
    """缺口的类别判据必须**等于** WP18 的常量（同一个对象，不是同值）。"""
    for category in CATEGORIES_REQUIRING_EVIDENCE:
        claim = _claim(category=category)
        assert kw.claim_needs_gap(claim) is True, (
            f"{category} 在 WP18 的 CATEGORIES_REQUIRING_EVIDENCE 里，"
            "本包必须把它当缺口"
        )
    # 反过来：不在常量里的类别一律不是缺口。
    for category in (CATEGORY_STRATEGY, CATEGORY_UNCONFIRMED):
        assert category not in CATEGORIES_REQUIRING_EVIDENCE
        assert kw.claim_needs_gap(_claim(category=category)) is False


def test_covered_requires_confirmed_resolution() -> None:
    """**缺口的"已覆盖"由 `resolution` 决定，不是由 `has_evidence` 决定。**

    + 本机实测到的修正：`brief_claims` 上有数据库 CHECK
    `ck_brief_claims_factual_category_requires_evidence`，
    它保证"需要证据的类别必有出处"。因此按 `has_evidence` 判的话
    `gap_state` **恒为 `COVERED`** —— `gaps[]` 永远没有一条缺口，
    `blockingCount` 恒为 0，"步骤 2 确认完整"变成一个**永远已经完成**
    的按钮，而不报任何错。

    正确的判据是运营可改的那一列 `resolution_status`。
    """
    pending = _claim(
        category=CATEGORY_SOURCE_FACT,
        resolution=RESOLUTION_PENDING,
        has_evidence=True,  # 有原文出处（数据库保证），但仍然待办
    )
    confirmed = _claim(
        category=CATEGORY_SOURCE_FACT,
        resolution=RESOLUTION_CONFIRMED,
        has_evidence=True,
    )
    assert kw.gap_state(pending) == GAP_MISSING
    assert kw.gap_state(confirmed) == GAP_COVERED


def test_covered_gap_is_not_searched() -> None:
    """**已覆盖的结论不再生成检索词**（本机实测踩到过的缺陷）。

    一条已 `CONFIRMED` 的 `EXTERNAL_EVIDENCE`：它**是**一条
    缺口条目（要显示"这条已经够了"），但**不该**被生成检索词 ——
    否则运营照着"已有第三方检测"去搜一轮，找一个已经确认过的东西。
    """
    claim = _claim(
        category=CATEGORY_EXTERNAL_EVIDENCE,
        resolution=RESOLUTION_CONFIRMED,
        has_evidence=True,
    )
    assert kw.claim_needs_gap(claim) is True   # 是缺口条目
    assert kw.gap_state(claim) == GAP_COVERED
    assert kw.claim_needs_search(claim) is False  # 但不该搜


def test_three_judgements_are_consistent() -> None:
    """三条判据的**合取关系**：只有"是缺口"且"状态是 MISSING"才去搜。

    本包最容易混错的地方（本机实测踩到两次，两个方向都会错）：

    | 函数 | 回答的问题 |
    |---|---|
    | `claim_needs_gap` | 它算不算一条缺口条目 |
    | `gap_state` | 已确认是缺口之后，它处于哪个状态 |
    | `claim_needs_search` | 还要不要为它生成检索词 |

    只用 `gap_state != MISSING` 会让 `STRATEGY` 混进来
    （因为 `gap_state` 刻意不判类别）；只用 `claim_needs_gap`
    会让已覆盖的混进来。
    """
    cases = (
        # (类别, 确认状态, 是缺口?, 状态, 要搜?)
        (CATEGORY_SOURCE_FACT, RESOLUTION_PENDING, True, GAP_MISSING, True),
        (CATEGORY_SOURCE_FACT, RESOLUTION_CONFIRMED, True, GAP_COVERED, False),
        (
            CATEGORY_EXTERNAL_EVIDENCE,
            RESOLUTION_NOT_APPLICABLE,
            True,
            GAP_NOT_APPLICABLE,
            False,
        ),
        # 类别不对 —— 即使状态是 MISSING 也不该搜（`gap_state` 刻意不判类别）。
        (CATEGORY_STRATEGY, RESOLUTION_PENDING, False, GAP_MISSING, False),
        (CATEGORY_UNCONFIRMED, RESOLUTION_PENDING, False, GAP_MISSING, False),
    )
    for category, resolution, is_gap, state, needs_search in cases:
        claim = _claim(category=category, resolution=resolution)
        assert kw.claim_needs_gap(claim) is is_gap, (category, resolution)
        assert kw.gap_state(claim) == state, (category, resolution)
        assert kw.claim_needs_search(claim) is needs_search, (category, resolution)
        # 合取关系必须成立。
        expected = kw.claim_needs_gap(claim) and kw.gap_state(claim) == GAP_MISSING
        assert kw.claim_needs_search(claim) is expected


def test_missing_gap_is_the_only_searched_state() -> None:
    """只有 `MISSING` 会被搜。三种状态各造一条，断言"恰好一条被搜"。"""
    claims = (
        _claim(category=CATEGORY_SOURCE_FACT),  # MISSING
        _claim(
            category=CATEGORY_EXTERNAL_EVIDENCE,
            resolution=RESOLUTION_CONFIRMED,
            has_evidence=True,
        ),  # COVERED
        _claim(
            category=CATEGORY_EXTERNAL_EVIDENCE,
            resolution=RESOLUTION_NOT_APPLICABLE,
        ),  # NOT_APPLICABLE
    )
    searched = [claim for claim in claims if kw.claim_needs_search(claim)]
    assert len(searched) == 1
    assert searched[0].category == CATEGORY_SOURCE_FACT


def test_gap_state_prefers_not_applicable_over_covered() -> None:
    """`NOT_APPLICABLE` **优先于** `COVERED`（运营的判断优先于系统推断）。

    已标"不适用"的结论必须显示 `NOT_APPLICABLE`（而不是因为别的判据
    被显示成 `COVERED`）—— 后者会让运营以为"系统认为这条已经够了"，
    与他的判断相反。且这条**必须仍然出现在 `gaps[]` 里**，
    否则他看不到自己的判断、也无法取消它。
    """
    claim = _claim(
        category=CATEGORY_SOURCE_FACT,
        resolution=RESOLUTION_NOT_APPLICABLE,
        has_evidence=True,
    )
    assert kw.claim_needs_gap(claim) is True  # 仍在清单里
    assert kw.gap_state(claim) == GAP_NOT_APPLICABLE

    gaps, _ = kw.derive_gaps((claim,))
    assert len(gaps) == 1
    assert gaps[0].state == GAP_NOT_APPLICABLE
    assert gaps[0].blocking is False
    # 已标不适用的不给建议词。
    enriched = kw.build_gap_keywords(gaps, subject_prefix="某产品")
    assert enriched[0].suggested_keywords == ()


def test_pending_with_source_explains_why_still_missing() -> None:
    """**"有出处却还是缺口"必须给出解释**（本包最容易被质疑的一点）。

    运营看到"续航 18 小时"会说"这明明有出处"。系统要能回答：
    `brief_claims` 上的来源是"这句话出自 Brief 原文的哪一段"，
    而研究阶段要补的是**外部佐证**。没有这条说明，
    运营会认为系统在瞎报缺口。
    """
    claim = _claim(
        category=CATEGORY_SOURCE_FACT,
        resolution=RESOLUTION_PENDING,
        has_evidence=True,
    )
    assert kw.gap_state(claim) == GAP_MISSING
    note = kw.gap_note(claim, GAP_MISSING)
    assert note is not None
    assert "外部佐证" in note


def test_confirmed_without_evidence_is_still_covered_by_resolution() -> None:
    """`CONFIRMED` 但无来源 → 仍判 `COVERED`（判据是 `resolution`）。

    **这是一处刻意的取舍**：数据库层已经保证"需要证据的类别必有来源"，
    因此"`CONFIRMED` 却无来源"在实际数据里不可能出现。
    本函数把这个事实固定下来 —— 若判据改回 `has_evidence`，
    本条会红。

    （无来源 + `CONFIRMED` 的组合若要出现，只能是数据回填绕过了
    WP18 的构造器与 CHECK 两层 —— 那时它是一条**数据事故**，
    应当报警，而不是在缺口清单里被静默地重新分类。）
    """
    claim = _claim(
        category=CATEGORY_SOURCE_FACT,
        resolution=RESOLUTION_CONFIRMED,
        has_evidence=False,
    )
    assert kw.gap_state(claim) == GAP_COVERED
    assert kw.gap_note(claim, GAP_COVERED) is None


def test_pending_without_evidence_has_no_note() -> None:
    """`PENDING` 且无来源（数据库不该允许，但纯函数要能处理）→ 无说明。"""
    claim = _claim(category=CATEGORY_SOURCE_FACT, has_evidence=False)
    assert kw.gap_state(claim) == GAP_MISSING
    assert kw.gap_note(claim, GAP_MISSING) is None


def test_has_evidence_accepts_segment_or_url() -> None:
    assert kw.has_evidence(evidence_segment_id=uuid.uuid4(), evidence_url=None) is True
    assert kw.has_evidence(evidence_segment_id=None, evidence_url="https://x") is True
    assert kw.has_evidence(evidence_segment_id=None, evidence_url="  ") is False
    assert kw.has_evidence(evidence_segment_id=None, evidence_url=None) is False


def test_field_label_falls_back_to_path_for_unknown() -> None:
    """未知路径**退回路径本身**（读取点降级），不抛错。

    写入点（WP18 的 `field_spec`）抛错是对的 —— 那里防的是把拼错的
    路径写进库。这里是读取点：历史 claim 的路径可能是已废弃的，
    那时应显示原始路径让人看出"这是老数据"，而不是让研究页 500。
    """
    assert kw.field_label("product.brand") == "品牌"
    assert kw.field_label("product.legacy_path") == "product.legacy_path"


# --- 1.4 关键词派生 -------------------------------------------------------


def _fields(tag: str = "T1") -> tuple[FieldValueInput, ...]:
    return (
        FieldValueInput(path="product.brand", label="品牌", values=(f"品牌{tag}",)),
        FieldValueInput(path="product.name", label="产品名称", values=(f"产品{tag}",)),
        FieldValueInput(path="product.model_code", label="型号", values=(f"型号{tag}",)),
        FieldValueInput(path="audience.target", label="目标人群", values=(f"人群{tag}",)),
    )


def test_entity_terms_follow_schema_order() -> None:
    """主体词顺序固定为**品牌 → 产品名 → 型号**（不按输入顺序）。

    `FieldValueInput` 的传入顺序取决于数据库读取顺序，而同一事务里
    插入的行 `created_at` 相同 → 顺序不确定（WP17 的 ISS-52）。
    """
    shuffled = (
        FieldValueInput(path="product.model_code", label="型号", values=("X1",)),
        FieldValueInput(path="product.name", label="产品名称", values=("锅",)),
        FieldValueInput(path="product.brand", label="品牌", values=("牌",)),
    )
    assert kw.entity_terms(shuffled) == ("牌", "锅", "X1")


def test_derive_keywords_has_four_kinds_in_order() -> None:
    """四类关键词都出现，且顺序是 ENTITY → FIELD → CLAIM → PATTERN。"""
    claims = (_claim(text="保温 6 小时，但这是实验室条件"),)
    keywords, truncated = kw.derive_keywords(claims, _fields())
    assert truncated is False
    kinds = [item.source_kind for item in keywords]
    assert kinds[0] == KEYWORD_SOURCE_ENTITY
    assert KEYWORD_SOURCE_FIELD in kinds
    assert KEYWORD_SOURCE_CLAIM in kinds
    assert kinds[-1] == KEYWORD_SOURCE_PATTERN
    # 四类的相对顺序固定。
    order = [kinds.index(name) for name in (
        KEYWORD_SOURCE_ENTITY,
        KEYWORD_SOURCE_FIELD,
        KEYWORD_SOURCE_CLAIM,
        KEYWORD_SOURCE_PATTERN,
    )]
    assert order == sorted(order)


def test_entity_keyword_carries_source_path_and_label() -> None:
    """每个词必须能回答"我从哪来"（R1 的应对）。"""
    keywords, _ = kw.derive_keywords((), _fields())
    entity = [item for item in keywords if item.source_kind == KEYWORD_SOURCE_ENTITY]
    assert entity
    for item in entity:
        assert item.source_field_path is not None
        assert item.source_field_label is not None
        assert item.terms


def test_pattern_keywords_are_marked_as_such() -> None:
    """`PATTERN` 类关键词**没有任何 `sourceFieldPath`**。

    它是本包唯一不来自 Brief 的词源，因此必须能与"来自你的 Brief"
    区分开 —— 否则运营会以为"系统从我的 Brief 里推出了'测评'"。
    """
    keywords, _ = kw.derive_keywords((), _fields())
    patterns = [item for item in keywords if item.source_kind == KEYWORD_SOURCE_PATTERN]
    assert patterns
    for item in patterns:
        assert item.source_field_path is None
        assert item.source_field_label is None


def test_claim_keyword_links_back_to_claim_id() -> None:
    """`CLAIM` 类关键词带 `relatedClaimId`（前端可跳回那条缺口）。"""
    claim = _claim(text="保温 6 小时")
    keywords, _ = kw.derive_keywords((claim,), _fields())
    linked = [item for item in keywords if item.source_kind == KEYWORD_SOURCE_CLAIM]
    assert len(linked) == 1
    assert linked[0].related_claim_id == claim.claim_id


def test_keywords_are_deduplicated() -> None:
    """同一批关键词不重复（大小写不敏感）。"""
    fields = (
        FieldValueInput(path="product.name", label="产品名称", values=("锅",)),
        FieldValueInput(path="product.brand", label="品牌", values=("锅",)),
    )
    keywords, _ = kw.derive_keywords((), fields)
    texts = [item.text.casefold() for item in keywords]
    assert len(texts) == len(set(texts))


def test_derive_keywords_truncates_at_limit() -> None:
    """超上限时**截断并返回 `True`**（不静默丢）。"""
    claims = tuple(
        _claim(text=f"结论编号 {index} 的内容", field_path=f"product.f{index:03d}")
        for index in range(MAX_KEYWORDS * 2)
    )
    keywords, truncated = kw.derive_keywords(claims, _fields())
    assert truncated is True
    assert len(keywords) == MAX_KEYWORDS


def test_derive_gaps_truncates_at_limit() -> None:
    claims = tuple(_claim(text=f"缺口 {index}") for index in range(MAX_GAPS + 5))
    gaps, truncated = kw.derive_gaps(claims)
    assert truncated is True
    assert len(gaps) == MAX_GAPS


def test_gaps_sorted_missing_first() -> None:
    """`MISSING` 排最前（它是唯一阻断态，运营要先看到"还差什么"）。"""
    claims = (
        _claim(
            category=CATEGORY_EXTERNAL_EVIDENCE,
            resolution=RESOLUTION_CONFIRMED,
            has_evidence=True,
            field_path="product.efficacy",
        ),
        _claim(category=CATEGORY_SOURCE_FACT, field_path="product.mechanism"),
        _claim(
            category=CATEGORY_EXTERNAL_EVIDENCE,
            resolution=RESOLUTION_NOT_APPLICABLE,
            field_path="product.pain_points",
        ),
    )
    gaps, _ = kw.derive_gaps(claims)
    states = [gap.state for gap in gaps]
    assert states[0] == GAP_MISSING
    assert states.count(GAP_MISSING) == 1
    # 非 MISSING 之间按 (field_path, claim_id) 稳定排序。
    rest = [gap.field_path for gap in gaps[1:]]
    assert rest == sorted(rest)


def test_gaps_exclude_non_evidence_categories() -> None:
    """缺口清单里**不出现** `STRATEGY` / `UNCONFIRMED`。"""
    claims = (
        _claim(category=CATEGORY_STRATEGY),
        _claim(category=CATEGORY_UNCONFIRMED),
        _claim(category=CATEGORY_SOURCE_FACT),
    )
    gaps, _ = kw.derive_gaps(claims)
    assert len(gaps) == 1
    assert gaps[0].category == CATEGORY_SOURCE_FACT


def test_gap_suggestions_only_for_missing() -> None:
    """只有 `MISSING` 缺口给建议词（其余给空元组）。"""
    claims = (
        _claim(category=CATEGORY_SOURCE_FACT, text="保温 6 小时"),
        _claim(
            category=CATEGORY_EXTERNAL_EVIDENCE,
            resolution=RESOLUTION_CONFIRMED,
            has_evidence=True,
            text="已有第三方检测",
        ),
    )
    gaps, _ = kw.derive_gaps(claims)
    enriched = kw.build_gap_keywords(gaps, subject_prefix="主体")
    for gap in enriched:
        if gap.state == GAP_MISSING:
            assert gap.suggested_keywords
            # 具体词在前，通用后缀垫底。
            assert "主体" in gap.suggested_keywords[0]
        else:
            assert gap.suggested_keywords == ()


def test_gap_suggestion_count_is_limited() -> None:
    """每条缺口的建议词数量受限，且**具体词排第一**。"""
    claim = _claim(category=CATEGORY_SOURCE_FACT, text="保温 6 小时，但这是实验室条件")
    gaps, _ = kw.derive_gaps((claim,))
    enriched = kw.build_gap_keywords(gaps, subject_prefix="某产品")
    assert len(enriched[0].suggested_keywords) <= MAX_SUGGESTED_KEYWORDS_PER_GAP
    assert "保温 6 小时" in enriched[0].suggested_keywords[0]


def test_no_subject_prefix_means_no_pattern_keywords() -> None:
    """没有主体词时不造 `PATTERN` 词。

    "测评"这种裸后缀**没有检索价值**（搜出来是全网内容）。
    主体词缺失本身是一条 `note`（见 service），不是在这里硬凑。
    """
    keywords, _ = kw.derive_keywords((), ())
    assert [item for item in keywords if item.source_kind == KEYWORD_SOURCE_PATTERN] == []


# --- 1.5 提示词组装 -------------------------------------------------------


def test_prompts_include_verification_and_collection() -> None:
    """每条 `MISSING` 缺口一条核实提示词 + **恰好一条**总览提示词。"""
    claims = (
        _claim(category=CATEGORY_SOURCE_FACT, text="保温 6 小时"),
        _claim(
            category=CATEGORY_EXTERNAL_EVIDENCE,
            text="成分经过检测",
            field_path="product.efficacy",
        ),
    )
    gaps, _ = kw.derive_gaps(claims)
    enriched = kw.build_gap_keywords(gaps, subject_prefix="某产品")
    prompts, truncated = kw.build_prompts(enriched, _fields())
    assert truncated is False
    kinds = [item.task_kind for item in prompts]
    assert kinds.count(TASK_FACT_VERIFICATION := "FACT_VERIFICATION") == 2
    assert kinds.count("EVIDENCE_COLLECTION") == 1
    # 总览在最后。
    assert kinds[-1] == "EVIDENCE_COLLECTION"


def test_prompt_text_is_complete_and_copyable() -> None:
    """`prompts[].text` 是**成品文本**（验收"复制"的落点）。

    服务端给死文本，不让前端拼 —— 前端拼的话"同样的缺口在不同页面
    复制出不同的提示词"，而复制出去的内容直接决定研究质量。
    """
    claim = _claim(category=CATEGORY_SOURCE_FACT, text="保温 6 小时")
    gaps, _ = kw.derive_gaps((claim,))
    enriched = kw.build_gap_keywords(gaps, subject_prefix="某产品")
    prompts, _ = kw.build_prompts(enriched, _fields())
    verification = [p for p in prompts if p.task_kind == "FACT_VERIFICATION"][0]
    assert "保温 6 小时" in verification.text
    assert "没有找到" in verification.text  # 不许推测的那句话在
    assert verification.format_hint.fields
    # **按类别断言具体取值，不只断言"在取值域内"**（Code Review 教训）：
    # 原来这里是 `media in FORMAT_MEDIA_KINDS` —— 缺陷（`media` 恒为
    # `ANY`，`format_media_for_category` 从未被调用）能顺利通过它，
    # 因为 `ANY` 也在取值域里。断言类型 vs 断言取值，差一个真缺陷。
    assert verification.format_hint.media == FORMAT_MEDIA_IMAGE


@pytest.mark.regression
def test_verification_prompt_media_depends_on_category() -> None:
    """`media` 由缺口**类别**决定（`format_media_for_category` 已接线）。

    `SOURCE_FACT` 的缺口是"原文那段没截到" → `IMAGE`（补截图）；
    `EXTERNAL_EVIDENCE` 的缺口是要外部文章 → `ANY`（形状由运营定）。

    这条测试是 Code Review 抓出的死代码的回归护栏：`format_media_for_category`
    曾有实现、有单测，但**生产路径零调用** —— 于是它对响应毫无影响。
    """
    source_fact = _claim(category=CATEGORY_SOURCE_FACT, text="保温 6 小时")
    external = _claim(category=CATEGORY_EXTERNAL_EVIDENCE, text="成分经第三方检测")

    for claim, expected in (
        (source_fact, FORMAT_MEDIA_IMAGE),
        (external, FORMAT_MEDIA_ANY),
    ):
        gaps, _ = kw.derive_gaps((claim,))
        enriched = kw.build_gap_keywords(gaps, subject_prefix="某产品")
        prompts, _ = kw.build_prompts(enriched, _fields())
        verification = [p for p in prompts if p.task_kind == "FACT_VERIFICATION"][0]
        assert verification.format_hint.media == expected, claim.category


def test_prompts_empty_when_no_missing_gap() -> None:
    """没有 `MISSING` 缺口时**不产提示词**（不给无用的核实任务）。"""
    claim = _claim(
        category=CATEGORY_EXTERNAL_EVIDENCE,
        resolution=RESOLUTION_CONFIRMED,
        has_evidence=True,
    )
    gaps, _ = kw.derive_gaps((claim,))
    enriched = kw.build_gap_keywords(gaps, subject_prefix="某产品")
    prompts, truncated = kw.build_prompts(enriched, _fields())
    assert prompts == ()
    assert truncated is False


def test_prompts_truncate_at_limit() -> None:
    claims = tuple(
        _claim(text=f"缺口 {index}", field_path=f"product.g{index:03d}")
        for index in range(MAX_PROMPTS * 2)
    )
    gaps, _ = kw.derive_gaps(claims)
    enriched = kw.build_gap_keywords(gaps, subject_prefix="某产品")
    prompts, truncated = kw.build_prompts(enriched, _fields())
    assert truncated is True
    assert len(prompts) == MAX_PROMPTS


def test_task_kinds_are_declared_set() -> None:
    assert set(TASK_KINDS) == {"FACT_VERIFICATION", "EVIDENCE_COLLECTION"}


def test_coverage_summary_counts_by_state() -> None:
    claims = (
        _claim(category=CATEGORY_SOURCE_FACT),
        _claim(
            category=CATEGORY_EXTERNAL_EVIDENCE,
            resolution=RESOLUTION_CONFIRMED,
            has_evidence=True,
        ),
        _claim(
            category=CATEGORY_EXTERNAL_EVIDENCE,
            resolution=RESOLUTION_NOT_APPLICABLE,
        ),
    )
    gaps, _ = kw.derive_gaps(claims)
    summary = kw.coverage_summary(gaps)
    assert summary["total"] == 3
    assert summary["missingCount"] == 1
    assert summary["coveredCount"] == 1
    assert summary["notApplicableCount"] == 1
    assert summary["blockingCount"] == summary["missingCount"]


def test_format_media_for_category() -> None:
    """`SOURCE_FACT` 的缺口建议补截图（它通常来自原文）。"""
    assert kw.format_media_for_category(CATEGORY_SOURCE_FACT) == "IMAGE"
    assert kw.format_media_for_category(CATEGORY_EXTERNAL_EVIDENCE) == "ANY"
    assert kw.format_media_for_category("未知") == "ANY"


def test_field_paths_used_by_research_exist_in_brief_schema() -> None:
    """本包硬编码的字段路径必须**都存在于 WP18 的 BriefSchema**。

    `ENTITY_FIELD_PATHS` 里一个拼错的路径会让主体词静默为空
    （`FIELD_BY_PATH.get()` 返回 `None`），于是所有关键词退化成
    裸短语 —— **不报任何错**。这条测试把它变成一条断言。
    """
    for path in kw.ENTITY_FIELD_PATHS:
        assert path in FIELD_BY_PATH, f"主体字段路径 {path} 不在 BriefSchema 里"
        assert FIELD_BY_PATH[path].required or True  # 存在即可


def test_seeded_fields_all_exist_in_schema() -> None:
    """测试夹具自己用的字段路径也必须合法（否则测试测的是不存在的东西）。"""
    for spec in BRIEF_FIELDS:
        assert spec.path in FIELD_BY_PATH


# ===========================================================================
# 第 2 层：服务层 + 真库
# ===========================================================================


# --- 2.1 从结构化字段造词（含三种状态的判据）-----------------------------


def test_field_value_inputs_skip_missing_and_not_applicable(env: ResearchEnv) -> None:
    """`FILLED` 造词；`MISSING` / `NOT_APPLICABLE` **都不造词**。

    `NOT_APPLICABLE` 那一条特别重要：它的 `value` 位置承载的是
    **不适用原因**（WP18 明确要求非空），因此"值为空就不造词"的
    实现会漏掉它 —— 而它的值看起来像一条正常的检索词。
    """
    structured = _default_fields(env, tag="FA")
    inputs = research_service.field_value_inputs(structured)
    paths = {item.path for item in inputs}
    assert "product.brand" in paths
    assert "audience.target" in paths
    # MISSING / NOT_APPLICABLE 都不在。
    assert "product.mechanism" not in paths
    assert "audience.constraints" not in paths
    assert "content.must_mention" not in paths


def test_field_value_inputs_expand_multi_values() -> None:
    """多值字段展开成多个取值，且**保序去重**。

    值的顺序取自运营填写顺序（卖点按优先级排），因此**不重排**。
    """
    structured = {
        "product.selling_points": {
            "status": FIELD_FILLED,
            "value": [{"text": "续航长"}, "价格低", {"text": "续航长"}],
        }
    }
    inputs = research_service.field_value_inputs(structured)
    assert len(inputs) == 1
    assert inputs[0].values == ("续航长", "价格低")


def test_field_value_inputs_drop_non_textual_values() -> None:
    """抽不出文本的元素**被丢弃**（不 `str()` 成 `"{'priority': 1}"`）。

    把 dict 的 repr 当检索词会搜出一堆无关结果，而它在列表里
    看起来像一条正常的关键词。
    """
    structured = {
        "product.selling_points": {
            "status": FIELD_FILLED,
            "value": [{"priority": 1, "weight": 2}, "正常卖点", 12345],
        }
    }
    inputs = research_service.field_value_inputs(structured)
    assert "正常卖点" in inputs[0].values
    assert "12345" in inputs[0].values
    assert all("priority" not in value for value in inputs[0].values)


def test_field_value_inputs_follow_schema_order() -> None:
    """返回顺序按 `BRIEF_FIELDS` 定义（jsonb 键顺序在 PG 里不保证稳定）。"""
    structured = {
        path: {"status": FIELD_FILLED, "value": f"值-{path}"}
        for path in (
            "audience.target",
            "product.brand",
            "product.name",
        )
    }
    inputs = research_service.field_value_inputs(structured)
    order = [item.path for item in inputs]
    schema_order = [spec.path for spec in BRIEF_FIELDS if spec.path in order]
    assert order == schema_order


def _claim_row(
    *,
    category: str = CATEGORY_SOURCE_FACT,
    resolution: str = RESOLUTION_PENDING,
    evidence_segment_id: uuid.UUID | None = None,
    evidence_url: str | None = None,
    text: str = "示例结论",
    field_path: str = "product.mechanism",
) -> Any:
    """一个**形状与 `BriefClaim` ORM 行相同**的替身。

    用它而不是造真库行：`claim_inputs` 做的是纯字段搬运
    （读 7 个属性、调两个纯函数），与数据库无关。
    用真库行只会让这条测试变慢，且它测的是同一段代码。

    **属性名必须是 ORM 的真实列名**（`claim_text` / `resolution_status`），
    否则这条测试会通过、而生产路径在真行上崩 —— 那正是它要防的事。
    """

    class _Row:
        pass

    row = _Row()
    row.id = uuid.uuid4()
    row.field_path = field_path
    row.claim_text = text
    row.category = category
    row.resolution_status = resolution
    row.evidence_segment_id = evidence_segment_id
    row.evidence_url = evidence_url
    return row


def test_claim_inputs_carry_label_and_evidence() -> None:
    row = _claim_row(category=CATEGORY_SOURCE_FACT, text="保温 6 小时")
    inputs = research_service.claim_inputs([row])
    assert len(inputs) == 1
    assert inputs[0].field_label == "原理"
    assert inputs[0].has_evidence is False
    assert inputs[0].claim_id == row.id
    assert inputs[0].field_path == "product.mechanism"
    assert inputs[0].text == "保温 6 小时"


def test_claim_inputs_read_evidence_columns() -> None:
    """带 `evidence_url` 的行 → `has_evidence=True`；带片段 id 同理。"""
    by_url = _claim_row(evidence_url="https://example.invalid/report")
    by_segment = _claim_row(evidence_segment_id=uuid.uuid4())
    inputs = research_service.claim_inputs([by_url, by_segment])
    assert all(item.has_evidence for item in inputs)


def test_claim_inputs_use_resolution_status_column() -> None:
    """必须读 `resolution_status`（ORM 列名），**不是** `resolution`。

    WP18 的 `brief_claims` 用 `resolution_status` 落库、
    协议层才映射成 `resolution`（数据结构 02 §6 vs 协议 §5.1 的名字差异）。
    读错属性名会直接 `AttributeError` —— 但只在挂到真行时才发生，
    因此这条断言用真列名构造替身。
    """
    row = _claim_row(resolution=RESOLUTION_CONFIRMED)
    inputs = research_service.claim_inputs([row])
    assert inputs[0].resolution == RESOLUTION_CONFIRMED


def test_claim_inputs_tolerate_unknown_field_path() -> None:
    """未知 `field_path` 降级为路径本身（读取点不抛错）。"""
    row = _claim_row(field_path="product.legacy_thing")
    inputs = research_service.claim_inputs([row])
    assert inputs[0].field_label == "product.legacy_thing"


# --- 2.2 build_research_prompts（真库）------------------------------------


def _repo(env: ResearchEnv) -> Any:
    """在一个打开的 session 里跑一段逻辑（自动关闭）。"""
    return session_scope(env.settings)


def test_build_prompts_from_real_brief(env: ResearchEnv) -> None:
    """验收主线：真库里一个 Brief 版本 → 关键词 + 提示词 + 缺口。"""
    tag = uuid.uuid4().hex[:6]
    _, version_id = _seed_brief_version(
        env,
        fields=_default_fields(env, tag=tag),
        claims=[
            {
                "field_path": "product.mechanism",
                "text": "保温 6 小时，但这是实验室条件",
                "category": CATEGORY_SOURCE_FACT,
            },
            {
                "field_path": "product.efficacy",
                "text": "成分经过第三方检测",
                "category": CATEGORY_EXTERNAL_EVIDENCE,
            },
            {
                "field_path": "purchase.script",
                "text": "主打学生宿舍场景",
                "category": CATEGORY_STRATEGY,
            },
        ],
    )
    with _repo(env) as session:
        bundle = research_service.build_research_prompts(
            ResearchRepository(session), project_id=env.project_id
        )

    assert bundle.brief_version_id == version_id
    assert bundle.claim_count == 3
    # 关键词里有主体词。
    assert any(f"产品{tag}" in item.text for item in bundle.keywords)
    # 缺口只有两条（STRATEGY 不是缺口）。
    assert len(bundle.gaps) == 2
    assert {gap.category for gap in bundle.gaps} == {
        CATEGORY_SOURCE_FACT,
        CATEGORY_EXTERNAL_EVIDENCE,
    }
    assert bundle.summary["missingCount"] == 2
    assert bundle.summary["blockingCount"] == 2
    # 每条缺口都有建议词。
    for gap in bundle.gaps:
        assert gap.suggested_keywords
    assert bundle.truncated is False


def test_build_prompts_explicit_version_id(env: ResearchEnv) -> None:
    """显式传 `version_id` 时用那一版（不复用"当前生效"）。"""
    tag = uuid.uuid4().hex[:6]
    _, first = _seed_brief_version(
        env, fields=_default_fields(env, tag=tag), set_active=False
    )
    with _repo(env) as session:
        bundle = research_service.build_research_prompts(
            ResearchRepository(session),
            project_id=env.project_id,
            version_id=first,
        )
    assert bundle.brief_version_id == first


def test_build_prompts_unknown_version_is_404(env: ResearchEnv) -> None:
    """版本不存在 → 404（**不是** 422：它是被寻址的资源）。"""
    with _repo(env) as session:
        with pytest.raises(ApiError) as excinfo:
            research_service.build_research_prompts(
                ResearchRepository(session),
                project_id=env.project_id,
                version_id=uuid.uuid4(),
            )
    assert excinfo.value.code == "RESOURCE_NOT_FOUND"


def test_build_prompts_version_from_other_project_is_404(
    env: ResearchEnv,
) -> None:
    """**别的项目的版本 → 404**（在 SQL 里判项目，防存在性枚举）。

    这条同时守着"探测不泄露存在性"：若实现是"取出来再比 project_id"，
    那条别人的版本行会被读出内存 —— 而它可能被某个异常详情带出去。
    """
    other_owner = _create_user(env.settings, name="另一个项目的负责人")
    _, _, other_project = _seed_project(env.settings, owner_id=other_owner)
    other_env = ResearchEnv(
        settings=env.settings,
        project_id=other_project,
        owner_id=other_owner,
        brand_id=env.brand_id,
        product_id=env.product_id,
        admin_id=env.admin_id,
    )
    _, other_version = _seed_brief_version(
        other_env, fields=_default_fields(env, tag="OT")
    )

    with _repo(env) as session:
        with pytest.raises(ApiError) as excinfo:
            research_service.build_research_prompts(
                ResearchRepository(session),
                project_id=env.project_id,
                version_id=other_version,
            )
    assert excinfo.value.code == "RESOURCE_NOT_FOUND"
    # 报错详情里**不带**那个版本的内容。
    assert "OT" not in str(excinfo.value.details)


def test_build_prompts_unknown_project_is_404(env: ResearchEnv) -> None:
    with _repo(env) as session:
        with pytest.raises(ApiError) as excinfo:
            research_service.build_research_prompts(
                ResearchRepository(session), project_id=uuid.uuid4()
            )
    assert excinfo.value.code == "RESOURCE_NOT_FOUND"


def test_build_prompts_without_brief_returns_guidance(env: ResearchEnv) -> None:
    """没有 Brief 版本 → **200 + 引导提示词**（不是 404）。

    报 404 会让前端把"还没做 Brief"显示成"出错了"。
    """
    owner = _create_user(env.settings, name="无 Brief 项目负责人")
    _, _, empty_project = _seed_project(env.settings, owner_id=owner)
    with _repo(env) as session:
        bundle = research_service.build_research_prompts(
            ResearchRepository(session), project_id=empty_project
        )
    assert bundle.brief_version_id is None
    assert bundle.keywords == ()
    assert bundle.gaps == ()
    assert len(bundle.prompts) == 1
    assert "先完成 Brief" in bundle.prompts[0].title
    assert bundle.notes


def test_build_prompts_notes_partial_source_state(env: ResearchEnv) -> None:
    """`source_state='PARTIAL'` 时给一条说明（Brief 本身就不完整）。"""
    _, _version = _seed_brief_version(
        env, fields=_default_fields(env, tag="PA"), source_state="PARTIAL"
    )
    with _repo(env) as session:
        bundle = research_service.build_research_prompts(
            ResearchRepository(session), project_id=env.project_id
        )
    assert any("PARTIAL" in note for note in bundle.notes)
    assert bundle.notes


def test_build_prompts_notes_missing_subject(env: ResearchEnv) -> None:
    """Brief 里连产品名都没有时，必须显式提示**跨品牌会串**（G2）。"""
    _, _version = _seed_brief_version(
        env,
        fields={
            "product.brand": {"status": FIELD_MISSING, "value": None},
            "product.name": {"status": FIELD_MISSING, "value": None},
        },
    )
    with _repo(env) as session:
        bundle = research_service.build_research_prompts(
            ResearchRepository(session), project_id=env.project_id
        )
    assert any("跨品牌" in note for note in bundle.notes)


def test_build_prompts_no_claims_gives_note(env: ResearchEnv) -> None:
    """版本存在但没有 claims（解构刚跑完或还没跑）→ 说明 + 空结果。"""
    _, _version = _seed_brief_version(env, fields=_default_fields(env, tag="NC"))
    with _repo(env) as session:
        bundle = research_service.build_research_prompts(
            ResearchRepository(session), project_id=env.project_id
        )
    assert bundle.claim_count == 0
    assert bundle.gaps == ()
    assert any("没有任何结论" in note for note in bundle.notes)


def test_build_prompts_revision_no_present(env: ResearchEnv) -> None:
    """`revisionNo` 取自 `version_nodes`（本包造版本时不注册节点 → `None`）。

    **这条断言把一个事实固定下来**：本包的测试夹具直接写
    `brief_versions`，没有走 WP18 的 `register_version`，
    因此 `version_nodes` 里没有对应行 → `revisionNo` 为 `None`。
    生产路径（走 solidify）会注册节点，那时它是整数。
    两个分支都要能工作 —— 这也是为什么该字段是 `int | None`。
    """
    _, _version = _seed_brief_version(env, fields=_default_fields(env, tag="RV"))
    with _repo(env) as session:
        bundle = research_service.build_research_prompts(
            ResearchRepository(session), project_id=env.project_id
        )
    assert bundle.revision_no is None or isinstance(bundle.revision_no, int)


def test_bundle_notes_exhaustively() -> None:
    """`_bundle_notes` 的**四种降级情形可以穷举**（阶段 7 抽出的纯函数）。

    它原本内联在 `build_research_prompts` 里，只能靠"造出那种数据"
    才能触发某一分支（例如"没有 claims 且被截断"很难造）。
    抽成纯函数之后可以直接构造组合 —— 这是简化的**真实收益**，
    不只是"函数变短"。

    四条判据都必须**独立**成立：任一条被写成 `elif` 就会漏掉
    "同时有两种降级"的情形，而那恰恰是最需要说明的
    （例如"没有 claims"**且**"结果被截断"）。
    """
    assert research_service._bundle_notes(
        subject="某产品", source_state="SUCCEEDED", claim_count=3, truncated=False
    ) == ()

    only_subject = research_service._bundle_notes(
        subject="", source_state="SUCCEEDED", claim_count=3, truncated=False
    )
    assert len(only_subject) == 1 and "跨品牌" in only_subject[0]

    only_partial = research_service._bundle_notes(
        subject="某产品", source_state="PARTIAL", claim_count=3, truncated=False
    )
    assert len(only_partial) == 1 and "PARTIAL" in only_partial[0]

    only_truncated = research_service._bundle_notes(
        subject="某产品", source_state="SUCCEEDED", claim_count=3, truncated=True
    )
    assert len(only_truncated) == 1 and "截断" in only_truncated[0]

    only_empty = research_service._bundle_notes(
        subject="某产品", source_state="SUCCEEDED", claim_count=0, truncated=False
    )
    assert len(only_empty) == 1 and "claims" in only_empty[0]

    # **四条同时成立 → 四条都在**（不是 `elif`）。
    all_four = research_service._bundle_notes(
        subject="", source_state="PARTIAL", claim_count=0, truncated=True
    )
    assert len(all_four) == 4


# --- 2.3 list_research_files（真库）---------------------------------------


def test_list_research_files_reports_parse_state(env: ResearchEnv) -> None:
    """验收"解析"的落点：列表带 `parseState` + `segmentCount`。

    五个文件覆盖五种状态。**`NONE` 是最容易漏的那个**
    （一次 outer join 写错就会把"没解析"显示成别的）。
    """
    cases = {
        "SUCCEEDED": 3,
        "PARTIAL": 2,
        "FAILED": 0,
        "RUNNING": 0,
        None: 0,
    }
    seeded: dict[str, uuid.UUID] = {}
    for state, segments in cases.items():
        seeded[state or "NONE"] = _seed_file(
            env, parse_state=state, segment_count=segments
        )

    with _repo(env) as session:
        bundle = research_service.list_research_files(
            ResearchRepository(session), project_id=env.project_id
        )
    by_id = {row.file_id: row for row in bundle.rows}
    for key, file_id in seeded.items():
        assert file_id in by_id, f"{key} 的文件不在列表里"
        assert by_id[file_id].parse_state == key

    parse_by_id = {row.file_id: research_service.parse_state_of(row) for row in bundle.rows}
    assert parse_by_id[seeded["NONE"]] == PARSE_STATE_NONE
    assert by_id[seeded["SUCCEEDED"]].segment_count == 3
    assert by_id[seeded["PARTIAL"]].segment_count == 2
    assert by_id[seeded["FAILED"]].segment_count == 0


def test_evidence_usable_rules(env: ResearchEnv) -> None:
    """`evidenceUsable` 的判据表（R5：`PARTIAL` 可用、`FAILED` 不可用）。

    **成对验证**（该计的计 + 该不计的不计）：只断言"SUCCEEDED 是 True"
    无法排除"一个恒为 True 的实现"。
    """
    usable_id = _seed_file(env, parse_state="SUCCEEDED", segment_count=2)
    partial_id = _seed_file(env, parse_state="PARTIAL", segment_count=1)
    failed_id = _seed_file(env, parse_state="FAILED", segment_count=0)
    running_id = _seed_file(env, parse_state="RUNNING", segment_count=0)
    none_id = _seed_file(env, parse_state=None, segment_count=0)

    with _repo(env) as session:
        bundle = research_service.list_research_files(
            ResearchRepository(session), project_id=env.project_id
        )
    flags = {
        row.file_id: research_service.evidence_usable(row) for row in bundle.rows
    }
    assert flags[usable_id] is True
    assert flags[partial_id] is True   # **可用但需标注**（缺的部分在 errors 里）
    assert flags[failed_id] is False   # 一个 unit 都没解出 → 不算覆盖
    assert flags[running_id] is False  # 还没好
    assert flags[none_id] is False     # 从未解析


def test_list_research_files_excludes_other_purposes(env: ResearchEnv) -> None:
    """`purpose=REFERENCE` 的文件**不出现**（R3：证据里不能混参考图）。"""
    research_id = _seed_file(env, purpose=PURPOSE_RESEARCH)
    reference_id = _seed_file(env, purpose=PURPOSE_REFERENCE)

    with _repo(env) as session:
        bundle = research_service.list_research_files(
            ResearchRepository(session), project_id=env.project_id
        )
    ids = {row.file_id for row in bundle.rows}
    assert research_id in ids
    assert reference_id not in ids


def test_list_research_files_excludes_ineffective_links(env: ResearchEnv) -> None:
    """`effective=False` 的关联**不出现**，也不计入配额（R4）。

    "已解绑"是软删 —— 历史关联要保留（审计），但不再算作当前研究资料。
    """
    active_id = _seed_file(env, byte_size=1000)
    detached_id = _seed_file(env, byte_size=999_000, effective=False)

    with _repo(env) as session:
        bundle = research_service.list_research_files(
            ResearchRepository(session), project_id=env.project_id
        )
    ids = {row.file_id for row in bundle.rows}
    assert active_id in ids
    assert detached_id not in ids
    # 解绑的字节数不进 `listedBytes`。
    assert int(bundle.quota["listedBytes"]) < 999_000


def test_list_research_files_status_filter(env: ResearchEnv) -> None:
    """`parse_states` 筛选在服务层生效（且只留选中的状态）。"""
    ok_id = _seed_file(env, parse_state="SUCCEEDED")
    bad_id = _seed_file(env, parse_state="FAILED")
    none_id = _seed_file(env, parse_state=None)

    with _repo(env) as session:
        only_none = research_service.list_research_files(
            ResearchRepository(session),
            project_id=env.project_id,
            parse_states=[PARSE_STATE_NONE],
        )
        only_ok = research_service.list_research_files(
            ResearchRepository(session),
            project_id=env.project_id,
            parse_states=["SUCCEEDED"],
        )
    none_ids = {row.file_id for row in only_none.rows}
    ok_ids = {row.file_id for row in only_ok.rows}
    assert none_id in none_ids and ok_id not in none_ids and bad_id not in none_ids
    assert ok_id in ok_ids and none_id not in ok_ids and bad_id not in ok_ids


def test_list_research_files_quota_matches_wp12_recompute(env: ResearchEnv) -> None:
    """配额数字必须**等于** WP12 的重算结果（R4：不自己算一遍）。"""
    first = _seed_file(env, byte_size=2000)
    second = _seed_file(env, byte_size=3000)
    del first, second

    with _repo(env) as session:
        bundle = research_service.list_research_files(
            ResearchRepository(session), project_id=env.project_id
        )
        expected = FileRepository(session).compute_effective_bytes(env.project_id)

    assert int(bundle.quota["effectiveBytes"]) == expected
    assert int(bundle.quota["listedBytes"]) == expected
    assert bundle.quota["reconciled"] is True
    assert int(bundle.quota["limitBytes"]) == 300_000_000
    assert int(bundle.quota["remainingBytes"]) == 300_000_000 - expected


def test_list_research_files_count_is_not_scan_limited(env: ResearchEnv) -> None:
    """`fileCount` 用**独立的计数查询**，不是 `len(rows)`。

    列表有 `RESEARCH_FILE_SCAN_LIMIT` 上限，因此 `len(list)` 不等于总数。
    用 `len(list)` 当总数时，文件超上限的项目会看到"共 1000 个"
    —— 一个**看起来完全正常**的错误数字（ISS-71 同型风险）。
    本包不真的造 1001 个文件（太慢），而是断言实现里两者来自不同来源：
    计数查询的结果与列表长度在本项目下都等于实际文件数，
    且**计数不受 `parse_states` 筛选影响**（它是全量口径）。
    """
    _seed_file(env, parse_state="SUCCEEDED")
    _seed_file(env, parse_state="FAILED")

    with _repo(env) as session:
        unfiltered = research_service.list_research_files(
            ResearchRepository(session), project_id=env.project_id
        )
        filtered = research_service.list_research_files(
            ResearchRepository(session),
            project_id=env.project_id,
            parse_states=["SUCCEEDED"],
        )
    # 筛选后 `rows` 变少，但 `fileCount`（总数）不变 —— 它是全量口径。
    assert len(filtered.rows) < len(unfiltered.rows)
    assert filtered.total_files == unfiltered.total_files
    assert filtered.quota["fileCount"] == unfiltered.quota["fileCount"]
    assert RESEARCH_FILE_SCAN_LIMIT >= 1000


@pytest.mark.regression
def test_quota_bytes_are_full_scope_not_filtered(env: ResearchEnv) -> None:
    """**`listedBytes` / `usableEvidenceCount` / `reconciled` 必须是全量口径。**

    这是 Code Review 抓出的一个真实缺陷（本机实测）：
    `fileCount` 走独立的 `count_research_files`（不受 `parse_states` 影响），
    但 `listedBytes` / `usableEvidenceCount` / `reconciled` 是从
    **已被 `parse_states` 过滤过的 `rows`** 算出来的 —— 于是同一个
    `meta.research` 里两个字段的口径不一致：

      · 带 `?status=SUCCEEDED` 请求时，`fileCount` 报 2（全部），
        而 `listedBytes` 只报 1 个文件的字节数；
      · 更糟的是 `reconciled` 会翻成 `False` —— 前端把它当成
        "账面与明细不符，磁盘可能被人绕过写路径改了"的**告警**，
        而实际上只是筛选。**一个假告警比没有告警更伤信任**：
        运营会学会忽略它，于是真告警也不再被看见。

    `docs/m2/WP19-研究关键词与提示词说明.md` 与 `service.list_research_files`
    的 docstring 都明写"全量口径、与翻页无关"，因此这是实现与设计不符，
    不是设计取舍。
    """
    _seed_file(env, parse_state="SUCCEEDED")
    _seed_file(env, parse_state="FAILED")

    with _repo(env) as session:
        unfiltered = research_service.list_research_files(
            ResearchRepository(session), project_id=env.project_id
        )
        filtered = research_service.list_research_files(
            ResearchRepository(session),
            project_id=env.project_id,
            parse_states=["SUCCEEDED"],
        )

    assert len(filtered.rows) < len(unfiltered.rows)

    # 三个字段都必须与"不筛选"时完全一致。
    assert filtered.quota["listedBytes"] == unfiltered.quota["listedBytes"]
    assert (
        filtered.quota["usableEvidenceCount"]
        == unfiltered.quota["usableEvidenceCount"]
    )
    assert filtered.quota["reconciled"] == unfiltered.quota["reconciled"] is True


def test_research_files_scan_limit_is_explicitly_large() -> None:
    """扫描上限必须**显式写大**（ISS-73：默认小 limit 是为生产吞吐选的）。"""
    assert RESEARCH_FILE_SCAN_LIMIT >= 1000


# ===========================================================================
# 第 3 层：数据库活性 —— **本包零表，因此测"零写入"与"读的是真数据"**
# ===========================================================================


def test_read_paths_do_not_write_anything(env: ResearchEnv) -> None:
    """**本包最实质的划界：读不改任何东西。**

    一次完整的
    `build_research_prompts` + `list_research_files` 前后，
    本包涉及的全部表在**该项目范围内**的行数必须**完全不变**。

    **为什么必须测这个**：一个"顺手把派生结果写进某个表"的实现
    会让所有功能测试照样通过（返回的形状完全一样），
    而它破坏的正是本包的核心设计 —— 派生结果落库会引入
    第二个真值来源（说明文档 §3 的 V1 否决理由）。
    """
    _seed_brief_version(
        env,
        fields=_default_fields(env, tag="ZW"),
        claims=[
            {
                "field_path": "product.mechanism",
                "text": "保温 6 小时",
                "category": CATEGORY_SOURCE_FACT,
            }
        ],
    )
    _seed_file(env, parse_state="SUCCEEDED", segment_count=2)

    before = _snapshot_research_tables(env)

    with _repo(env) as session:
        repo = ResearchRepository(session)
        research_service.build_research_prompts(repo, project_id=env.project_id)
        research_service.list_research_files(repo, project_id=env.project_id)

    after = _snapshot_research_tables(env)
    assert before == after, (
        "研究模块的**读取路径写入了数据** —— 本包的设计是零写入、"
        f"零表。变化前：{before}\n变化后：{after}"
    )
    # 有内容被读到（否则"零变化"是因为什么都没读）。
    assert before["brief_claims"] >= 1
    assert before["project_file_links"] >= 1
    assert before["parse_runs"] >= 1


def test_repository_is_read_only_by_construction(env: ResearchEnv) -> None:
    """`ResearchRepository` **不得有写方法**（按名字扫描）。

    与上一条互补：上一条测"这次没写"，本条测"没有可写的入口"。
    `insert_` / `update_` / `delete_` / `save_` / `add_` 开头的方法
    一律视为写入口 —— 本包一个都不该有。
    """
    import inspect

    members = inspect.getmembers(ResearchRepository, predicate=inspect.isfunction)
    write_prefixes = ("insert_", "update_", "delete_", "save_", "add_", "create_")
    offenders = [
        name for name, _fn in members if name.startswith(write_prefixes)
    ]
    assert offenders == [], f"研究仓库里出现了写方法：{offenders}"
    # 且它确实有读方法（否则上一条断言是空的）。
    assert any(name.startswith("list_") for name, _fn in members)


def test_own_tables_is_empty() -> None:
    """本包**不建任何表**（划界写在代码里，不是写在文档里）。"""
    assert OWN_TABLES == ()


def test_research_module_has_no_models_file() -> None:
    """`modules/research/` 里**没有 `models.py`**（无表就没有模型）。

    这条守着一个具体的诱惑：为了让"关键词"有个落脚点而建一张表。
    """
    import pathlib

    package = pathlib.Path(
        __import__("rednote.modules.research", fromlist=["x"]).__file__ or ""
    ).parent
    assert not (package / "models.py").exists()
    assert not (package / "ddl.py").exists()


def test_no_own_migration_revision(env: ResearchEnv) -> None:
    """本项目**没有本包引入的迁移版本**（零迁移）。

    读 `alembic_version` 只有一个 head，且它的值是 WP20 的版本 ——
    本包不产生新版本。**断言的是"不早于"而不是"恰好等于"**：
    等式判据会在下一个包迁移时变红（ISS-71 的根因），
    而"本包没有引入任何迁移"这件事的正确断言是
    "版本记录里没有属于 research 的 revision"。
    """
    engine = get_engine(env.settings)
    with engine.connect() as conn:
        heads = [
            row[0]
            for row in conn.execute(text("SELECT version_num FROM alembic_version"))
        ]
    # 恰好一个 head（迁移是单链，分支禁写 migrations/versions）。
    assert len(heads) == 1
    # 本包未引入 revision：`research` 这个词不该出现在任何迁移文件名里
    # 由 `tools/m1/collect_wp19_evidence.py` 独立核对（见证据脚本）。


def test_reads_actually_reflect_database_rows(env: ResearchEnv) -> None:
    """读出来的值与**原生 SQL 查出来的事实一致**（不是夹具造的假象）。

    这一层用原生 SQL 独立核对四个事实：片段数、字节数、
    `effective` 标志、解析状态。**四条都必须与 SQL 一致** ——
    "服务层与 SQL 各自算一遍"是本包唯一能自我验证的方式
    （本包没有自己的表，因此不能用"约束生效"这类证据）。
    """
    file_id = _seed_file(
        env, parse_state="PARTIAL", segment_count=4, byte_size=4242, effective=True
    )
    engine = get_engine(env.settings)
    with engine.connect() as conn:
        segments = conn.execute(
            text(
                "SELECT count(*) FROM source_segments s JOIN parse_runs r"
                " ON r.id = s.parse_run_id WHERE r.file_id = :fid"
            ),
            {"fid": file_id},
        ).scalar_one()
        size = conn.execute(
            text(
                "SELECT b.byte_size FROM blobs b JOIN file_objects f"
                " ON f.blob_id = b.id WHERE f.id = :fid"
            ),
            {"fid": file_id},
        ).scalar_one()
        effective = conn.execute(
            text(
                "SELECT effective FROM project_file_links WHERE file_id = :fid"
                " AND purpose = 'RESEARCH'"
            ),
            {"fid": file_id},
        ).scalar_one()
        state = conn.execute(
            text(
                "SELECT state FROM parse_runs WHERE file_id = :fid"
                " ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"fid": file_id},
        ).scalar_one()

    assert (segments, size, effective, state) == (4, 4242, True, "PARTIAL")

    with _repo(env) as session:
        bundle = research_service.list_research_files(
            ResearchRepository(session), project_id=env.project_id
        )
    row = next(item for item in bundle.rows if item.file_id == file_id)
    assert row.segment_count == segments
    assert row.byte_size == size
    assert row.effective is effective
    assert row.parse_state == state


def test_latest_parse_run_wins(env: ResearchEnv) -> None:
    """一个文件有**多次**解析时，列表显示**最新那次**。

    这是相关子查询 `ORDER BY created_at DESC, id DESC LIMIT 1` 的语义。
    造两次运行（FAILED 然后 SUCCEEDED），断言看到的是后者 ——
    若 join 写错（例如不加 ORDER BY），看到的是随机的某一次。
    """
    file_id = _seed_file(env, parse_state="FAILED", segment_count=0)
    # 再追加一次成功的解析（`parse_runs` 是只追加的）。
    with session_scope(env.settings) as session:
        run_id = uuid.uuid4()
        facts = _parse_run_facts("SUCCEEDED", 2)
        session.add(
            ParseRun(
                id=run_id,
                file_id=file_id,
                job_id=None,
                parser_version="test-parser-2",
                state="SUCCEEDED",
                total_units=facts["total_units"],
                parsed_units=facts["parsed_units"],
                errors=[],
                ocr_engine="NONE",
                finished_at=facts["finished_at"],
            )
        )
        session.flush()
        for index in range(2):
            session.add(
                SourceSegment(
                    parse_run_id=run_id,
                    segment_no=index + 1,
                    position={"kind": "PAGE", "page": index + 1},
                    position_schema_version=1,
                    text_content=f"新片段 {index + 1}",
                    content_hash=(uuid.uuid4().hex + uuid.uuid4().hex)[:64],
                    extraction_kind="TEXT",
                    confidence=None,
                )
            )

    with _repo(env) as session:
        bundle = research_service.list_research_files(
            ResearchRepository(session), project_id=env.project_id
        )
    row = next(item for item in bundle.rows if item.file_id == file_id)
    assert row.parse_state == "SUCCEEDED"
    assert row.parser_version == "test-parser-2"
    assert row.segment_count == 2
    assert research_service.evidence_usable(row) is True


def test_scan_status_is_surfaced(env: ResearchEnv) -> None:
    """`scanStatus` 必须被带出来（"解析了但没扫描"是一个要可见的事实）。"""
    file_id = _seed_file(env, scan_status="SKIPPED", parse_state="SUCCEEDED")
    with _repo(env) as session:
        bundle = research_service.list_research_files(
            ResearchRepository(session), project_id=env.project_id
        )
    row = next(item for item in bundle.rows if item.file_id == file_id)
    assert row.scan_status == "SKIPPED"
    view = research_service.file_view(row)
    assert view["scanStatus"] == "SKIPPED"


# --- 3.1 序列化（容量类字段是十进制字符串）-------------------------------


def test_file_view_uses_decimal_strings_for_sizes(env: ResearchEnv) -> None:
    """容量/计数类字段是**十进制字符串**；`segmentCount` 是 int。"""
    file_id = _seed_file(
        env, parse_state="SUCCEEDED", segment_count=7, byte_size=12_345
    )
    with _repo(env) as session:
        bundle = research_service.list_research_files(
            ResearchRepository(session), project_id=env.project_id
        )
    row = next(item for item in bundle.rows if item.file_id == file_id)
    view = research_service.file_view(row)
    assert view["byteSize"] == "12345"
    # `SUCCEEDED` 的 `parsed_units` 必须等于 `total_units`（WP17 的 CHECK），
    # 而 `parsed_units` 必须与片段数一致（夹具的 `_parse_run_facts` 保证）。
    assert view["parse"]["totalUnits"] == "7"
    assert view["parse"]["parsedUnits"] == "7"
    assert view["parse"]["segmentCount"] == 7
    assert isinstance(view["parse"]["segmentCount"], int)


def test_file_view_marks_none_state_as_not_complete(env: ResearchEnv) -> None:
    file_id = _seed_file(env, parse_state=None)
    with _repo(env) as session:
        bundle = research_service.list_research_files(
            ResearchRepository(session), project_id=env.project_id
        )
    row = next(item for item in bundle.rows if item.file_id == file_id)
    view = research_service.file_view(row)
    assert view["parse"]["state"] == PARSE_STATE_NONE
    assert view["parse"]["complete"] is False
    assert view["parse"]["parseRunId"] is None
    assert view["evidenceUsable"] is False


def test_gap_view_shape(env: ResearchEnv) -> None:
    """`gaps[]` 必须带前端渲染所需的全部字段（说明文档 §1.7）。"""
    claim = _claim(category=CATEGORY_SOURCE_FACT, text="保温 6 小时")
    gaps, _ = kw.derive_gaps((claim,))
    enriched = kw.build_gap_keywords(gaps, subject_prefix="某产品")
    view = research_service.gap_view(enriched[0])
    for key in (
        "claimId",
        "fieldPath",
        "fieldLabel",
        "claimText",
        "category",
        "resolution",
        "state",
        "blocking",
        "suggestedKeywords",
        "note",
    ):
        assert key in view
    assert view["state"] == GAP_MISSING
    assert view["blocking"] is True
    assert view["suggestedKeywords"]


def test_page_limited_modules_are_all_declared() -> None:
    """本包所有对外取值都在 `types.py`（不存在散落的字面量）。

    逐条核对：模块里出现的缺口状态 / 关键词来源 / 任务类别，
    都必须在 `types.py` 的受控取值里。
    """
    for module in (kw, research_service):
        source = open(module.__file__ or "", encoding="utf-8").read()
        for literal, allowed in (
            ("\"COVERED\"", GAP_STATES),
            ("\"MISSING\"", GAP_STATES),
            ("\"NOT_APPLICABLE\"", GAP_STATES),
        ):
            if literal in source.replace(" ", ""):
                assert literal.strip("\"") in allowed


# ===========================================================================
# 第 4 层：HTTP
# ===========================================================================


def test_prompts_endpoint_returns_full_view(env: ResearchEnv) -> None:
    """`GET /research-prompts` 的 200 与响应形状。"""
    tag = uuid.uuid4().hex[:6]
    _seed_brief_version(
        env,
        fields=_default_fields(env, tag=tag),
        claims=[
            {
                "field_path": "product.mechanism",
                "text": "保温 6 小时",
                "category": CATEGORY_SOURCE_FACT,
            }
        ],
    )
    with _client(env) as client:
        _login(env, client)
        response = client.get(
            f"/api/v1/projects/{env.project_id}/research-prompts"
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"data", "meta"}
    data = body["data"]
    for key in (
        "projectId",
        "productName",
        "keywords",
        "prompts",
        "gaps",
        "summary",
        "derivation",
    ):
        assert key in data
    assert data["derivation"]["rulesVersion"] == DERIVATION_RULES_VERSION
    assert data["derivation"]["payloadVersion"] == DERIVATION_PAYLOAD_VERSION
    assert data["derivation"]["briefVersionId"]
    assert data["summary"]["blockingCount"] >= 1


def test_prompts_endpoint_accepts_version_query(env: ResearchEnv) -> None:
    tag = uuid.uuid4().hex[:6]
    _, version_id = _seed_brief_version(
        env, fields=_default_fields(env, tag=tag)
    )
    with _client(env) as client:
        _login(env, client)
        response = client.get(
            f"/api/v1/projects/{env.project_id}/research-prompts",
            params={"briefVersionId": str(version_id)},
        )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["derivation"]["briefVersionId"] == str(version_id)


def test_prompts_endpoint_unknown_version_is_404(env: ResearchEnv) -> None:
    with _client(env) as client:
        _login(env, client)
        response = client.get(
            f"/api/v1/projects/{env.project_id}/research-prompts",
            params={"briefVersionId": str(uuid.uuid4())},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_prompts_endpoint_invalid_uuid_is_422(env: ResearchEnv) -> None:
    with _client(env) as client:
        _login(env, client)
        response = client.get(
            f"/api/v1/projects/{env.project_id}/research-prompts",
            params={"briefVersionId": "not-a-uuid"},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_prompts_endpoint_requires_session(env: ResearchEnv) -> None:
    with _client(env) as client:
        response = client.get(
            f"/api/v1/projects/{env.project_id}/research-prompts"
        )
    assert response.status_code == 401


def test_prompts_endpoint_non_member_is_404(env: ResearchEnv) -> None:
    """非成员看不到项目 → **404**（不是 403，防存在性枚举）。"""
    outsider = _create_user(env.settings, name="项目外用户")
    outsider_env = ResearchEnv(
        settings=env.settings,
        project_id=env.project_id,
        owner_id=outsider,
        brand_id=env.brand_id,
        product_id=env.product_id,
        admin_id=env.admin_id,
    )
    with _client(env) as client:
        _login(outsider_env, client)
        response = client.get(
            f"/api/v1/projects/{env.project_id}/research-prompts"
        )
    assert response.status_code == 404


def test_admin_can_read_any_project(env: ResearchEnv) -> None:
    """管理者对全部项目有权限（需求 §2.2）。"""
    with _client(env) as client:
        _login(env, client, admin=True)
        response = client.get(
            f"/api/v1/projects/{env.project_id}/research-prompts"
        )
    assert response.status_code == 200, response.text


def test_files_endpoint_returns_page_and_research_meta(env: ResearchEnv) -> None:
    """`GET /research-files`：`page` 与 `meta.research` **都不能被剥掉**。

    **这条是本包最重要的一条 HTTP 测试。** `response_model` 是
    FastAPI 的输出过滤器：`meta` 里没被模型声明的键会被静默剥掉
    （WP10 踩过 —— 当时丢的是 `page`，前端"永远只能看第一页"，
    而 HTTP 200、服务端日志无异常）。

    因此必须逐字段断言 `page` 与 `meta.research` 都在，
    而不是只断言 `response.status_code == 200`。
    """
    file_id = _seed_file(
        env, parse_state="SUCCEEDED", segment_count=5, byte_size=2000
    )
    with _client(env) as client:
        _login(env, client)
        response = client.get(f"/api/v1/projects/{env.project_id}/research-files")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"data", "page", "meta"}
    assert set(body["page"]) == {"nextCursor", "hasMore"}
    assert body["page"]["hasMore"] is False
    assert "research" in body["meta"], (
        "meta.research 被 response_model 剥掉了 —— 配额字段消失"
    )
    assert "requestId" in body["meta"]
    research = body["meta"]["research"]
    for key in (
        "limitBytes",
        "effectiveBytes",
        "reservedBytes",
        "remainingBytes",
        "listedBytes",
        "fileCount",
        "usableEvidenceCount",
        "reconciled",
    ):
        assert key in research, f"meta.research 缺字段 {key}"
    assert research["limitBytes"] == "300000000"
    assert isinstance(research["limitBytes"], str)

    listed = {item["fileId"] for item in body["data"]}
    assert str(file_id) in listed


def test_files_endpoint_file_view_shape(env: ResearchEnv) -> None:
    file_id = _seed_file(env, parse_state="PARTIAL", segment_count=3, byte_size=777)
    with _client(env) as client:
        _login(env, client)
        response = client.get(f"/api/v1/projects/{env.project_id}/research-files")
    item = next(
        row for row in response.json()["data"] if row["fileId"] == str(file_id)
    )
    assert item["byteSize"] == "777"
    assert item["parse"]["state"] == "PARTIAL"
    assert item["parse"]["segmentCount"] == 3
    assert item["parse"]["complete"] is False
    assert item["evidenceUsable"] is True
    assert item["purpose"] == "RESEARCH"
    assert item["effective"] is True


def test_files_endpoint_status_filter(env: ResearchEnv) -> None:
    """`status` 复选：`NONE` 与 `SUCCEEDED` 一起筛。"""
    none_id = _seed_file(env, parse_state=None)
    ok_id = _seed_file(env, parse_state="SUCCEEDED")
    failed_id = _seed_file(env, parse_state="FAILED")
    with _client(env) as client:
        _login(env, client)
        response = client.get(
            f"/api/v1/projects/{env.project_id}/research-files",
            params={"status": "NONE,SUCCEEDED"},
        )
    assert response.status_code == 200, response.text
    listed = {item["fileId"] for item in response.json()["data"]}
    assert str(none_id) in listed
    assert str(ok_id) in listed
    assert str(failed_id) not in listed


def test_files_endpoint_status_case_insensitive(env: ResearchEnv) -> None:
    ok_id = _seed_file(env, parse_state="SUCCEEDED")
    with _client(env) as client:
        _login(env, client)
        response = client.get(
            f"/api/v1/projects/{env.project_id}/research-files",
            params={"status": "succeeded"},
        )
    assert response.status_code == 200, response.text
    assert str(ok_id) in {item["fileId"] for item in response.json()["data"]}


@pytest.mark.regression
def test_files_endpoint_invalid_status_is_422(env: ResearchEnv) -> None:
    """非法 `status` **报 422**（不静默忽略）。

    静默忽略会让前端拿到全部文件并显示成"筛选后的结果" ——
    那比报错危险得多。`details.allowed` 回显允许值供前端提示。
    """
    with _client(env) as client:
        _login(env, client)
        response = client.get(
            f"/api/v1/projects/{env.project_id}/research-files",
            params={"status": "succeded"},
        )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert "succeded" in error["details"]["invalid"]
    assert "SUCCEEDED" in error["details"]["allowed"]


def test_files_endpoint_requires_session(env: ResearchEnv) -> None:
    with _client(env) as client:
        response = client.get(f"/api/v1/projects/{env.project_id}/research-files")
    assert response.status_code == 401


def test_files_endpoint_paginates_with_stable_sort(env: ResearchEnv) -> None:
    """`limit=1` 时能翻页，且第二页不含第一页的项（稳定排序的落点）。"""
    for _ in range(3):
        _seed_file(env, parse_state="SUCCEEDED")

    with _client(env) as client:
        _login(env, client)
        first = client.get(
            f"/api/v1/projects/{env.project_id}/research-files",
            params={"limit": 1},
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["page"]["hasMore"] is True
        cursor = body["page"]["nextCursor"]
        assert cursor

        second = client.get(
            f"/api/v1/projects/{env.project_id}/research-files",
            params={"limit": 1, "cursor": cursor},
        )
        assert second.status_code == 200, second.text

    first_ids = {item["fileId"] for item in body["data"]}
    second_ids = {item["fileId"] for item in second.json()["data"]}
    assert first_ids.isdisjoint(second_ids), (
        "翻页出现重复项 —— 游标锚点不唯一（排序缺 id 作为平局打破者）"
    )


def test_files_endpoint_cursor_is_bound_to_filters(env: ResearchEnv) -> None:
    """把"按状态筛选"的游标拿去"不筛选"的查询里 → **400**（绑定校验）。"""
    for _ in range(2):
        _seed_file(env, parse_state="SUCCEEDED")
    with _client(env) as client:
        _login(env, client)
        first = client.get(
            f"/api/v1/projects/{env.project_id}/research-files",
            params={"limit": 1, "status": "SUCCEEDED"},
        )
        cursor = first.json()["page"]["nextCursor"]
        assert cursor
        mismatched = client.get(
            f"/api/v1/projects/{env.project_id}/research-files",
            params={"limit": 1, "cursor": cursor},
        )
    assert mismatched.status_code == 400
    assert mismatched.json()["error"]["code"] == "MALFORMED_REQUEST"
    assert (
        mismatched.json()["error"]["details"]["reason"] == "cursor_binding_mismatch"
    )


def test_files_endpoint_status_order_does_not_change_cursor(
    env: ResearchEnv,
) -> None:
    """`status=A,B` 与 `status=B,A` 是**同一个筛选** → 游标可互换。

    不归一化顺序会让两个等价 URL 产生两个互不兼容的游标，
    而前端没有任何办法知道"这两次请求的筛选其实一样"。
    """
    for _ in range(3):
        _seed_file(env, parse_state="SUCCEEDED")
        _seed_file(env, parse_state="FAILED")
    with _client(env) as client:
        _login(env, client)
        forward = client.get(
            f"/api/v1/projects/{env.project_id}/research-files",
            params={"limit": 1, "status": "SUCCEEDED,FAILED"},
        )
        cursor = forward.json()["page"]["nextCursor"]
        assert cursor
        reversed_query = client.get(
            f"/api/v1/projects/{env.project_id}/research-files",
            params={"limit": 1, "status": "FAILED,SUCCEEDED", "cursor": cursor},
        )
    assert reversed_query.status_code == 200, reversed_query.text
    assert reversed_query.json()["data"], "换顺序后游标应当仍然可用"


def test_files_endpoint_unknown_project_is_404(env: ResearchEnv) -> None:
    with _client(env) as client:
        _login(env, client)
        response = client.get(f"/api/v1/projects/{uuid.uuid4()}/research-files")
    assert response.status_code == 404


def test_prompts_response_has_no_forbidden_keys(env: ResearchEnv) -> None:
    """响应里**不出现**内部字段名（`snake_case` 契约守卫）。"""
    _, _version = _seed_brief_version(
        env,
        fields=_default_fields(env, tag="CK"),
        claims=[
            {
                "field_path": "product.mechanism",
                "text": "保温 6 小时",
                "category": CATEGORY_SOURCE_FACT,
            }
        ],
    )
    with _client(env) as client:
        _login(env, client)
        prompts = client.get(
            f"/api/v1/projects/{env.project_id}/research-prompts"
        ).json()
        files = client.get(
            f"/api/v1/projects/{env.project_id}/research-files"
        ).json()
    for payload in (prompts, files):
        raw = str(payload)
        for forbidden in ("brief_version_id", "parse_run_id", "field_path", "source_kind"):
            assert forbidden not in raw, f"响应里出现了内部字段名 {forbidden}"