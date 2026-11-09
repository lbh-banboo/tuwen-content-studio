"""WP30 `review` 包的验收判据 —— 纯函数层 / 取值域层 / 数据库行为层 / 服务层 / 静态接缝。

## 夹具纪律（照抄 `test_feedback_wp29.py`）

· 一律跑**私有库**（`REDNOTE_TEST_DATABASE_URL`）；缺省也指向 WP30 私有库，
  不碰共享 `rednote_test`；
· 只重建本包 3 张表 + 5 个触发器，其余表按迁移已就绪；
· 每个用例新造 user / project / content，**按项目隔离**，不数全库行；
· 造数复用 `test_content_wp26` 的 `_seed_project` / `_seed_valid_content` /
  `_create_user` —— 重写一份"怎么造内容+版本"会在将来分叉；
· 数据库探针断言 **sqlstate**，不只断言"抛了异常"。

## 本文件守什么

| 组 | 判据 | 风险 |
|---|---|---|
| 纯函数层 | **每条已实现的规则都有阳性（FAILED）+ 阴性（PASSED）对照输入**，且集合与 `rules.RULES` 机械对账；三条恒 SKIPPED 的规则各有受控原因码的用例 | **R1 恒绿规则**（本包最大的风险：8 张卡片全绿、看起来很完备，而它从未拒绝过任何一条内容） |
| 取值域层 | 7 个闭集逐值断言 + 跨包 parity（8 类 / 3 档严重度 / 3 锚点 / scope / actorKind / 三个边界常量 / binding 键名） | 值漂移是静默的：本包与 WP29 的 8 类不一致时，阻断项会被 `create_feedback` 以 422 拒绝 ⇒ 审核闸放行 |
| 数据库行为层 | 三表不可变（23001）、`outcome` 四条等价式（23514）、规则结论四态配对（23514）、锚点三选一（23514）、evidence 形状、`(run_id, rule_code)` 唯一（23505）、两条一致性触发器（23514） | **R6 派生字段漂移**（`outcome=PASSED` 但 `blocking_count=1`，而读端点只看 outcome） |
| 服务层 | 8 类齐全、三类恒 SKIPPED 的具体计数、去重三条、STALE 判定、`materialize=False`、单条规则异常不中断整轮、与 WP29 联动的 409/201 | **R2 假绿**（skipped 被折算成通过）、**R4 去重两个方向的错**、**R9 读路径写副作用** |
| 静态接缝 | 本包零切段实现（AST）、不触达网关（AST）、`evaluators.py` 是纯函数（AST） | **R3 第四份切段实现**（会让媒体一致性规则系统性恒红）、**R11 擅自加模型调用** |

## 为什么纯函数层的判据必须是"机械对账"而不是逐条手写

手写用例会漏：加一条规则时没人提醒你补用例，而一条**没有阳性对照**的规则
与一条**恒绿**的规则在测试输出上完全同形（都是绿的）。
因此 `_POSITIVE` / `_NEGATIVE` / `_ALWAYS_SKIPPED` 三张表与 `rules.RULES`
做**集合相等**断言 —— 加规则不补用例会当场红。
"""

from __future__ import annotations

import ast
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from conftest import (
    cross_package_foreign_keys,
    drop_own_tables,
    truncate_targets,
)

# FK 目标表必须在 `create_all` 之前 import（否则 NoReferencedTableError）。
# 本包三张表的 FK 指向：projects / content_items / content_versions /
# media_items / users / content_feedback_items；而 content_versions 自己
# 又引用 version_nodes。
import rednote.modules.baseline.models  # noqa: F401
import rednote.modules.content.models  # noqa: F401
import rednote.modules.feedback.models  # noqa: F401
import rednote.modules.identity.models  # noqa: F401
import rednote.modules.media.models  # noqa: F401
import rednote.modules.planning.models  # noqa: F401
import rednote.modules.projects.models  # noqa: F401
import rednote.modules.review.models  # noqa: F401
import rednote.modules.versions.models  # noqa: F401

from rednote.modules import review as review_pkg
from rednote.modules.content.repository import ContentRepository
from rednote.modules.content.types import (
    BINDING_KEY_CLAIM_ID,
    BINDING_KEY_PARAGRAPH_INDEX,
)
from rednote.modules.feedback import service as feedback_service
from rednote.modules.feedback import types as feedback_types
from rednote.modules.feedback.repository import FeedbackRepository
from rednote.modules.media import types as media_types
from rednote.modules.planning.repository import PlanningRepository
from rednote.modules.planning.types import SETTING_KEY_PLATFORM_MEDIA_LIMITS
from rednote.modules.review import ddl as r_ddl
from rednote.modules.review import evaluators, media_bridge, rules
from rednote.modules.review import service as r_service
from rednote.modules.review.feedback_bridge import AI_AUTHOR_KIND
from rednote.modules.review.models import OWN_TABLES
from rednote.modules.review.repository import ReviewRepository
from rednote.modules.review.types import (
    AI_FEEDBACK_SCOPE,
    MAX_PARAGRAPH_REF_CHARS,
    MAX_RULE_CODE_CHARS,
    MAX_SKIP_REASON_CHARS,
    MEDIA_ITEM_STATE_READY,
    MEDIA_RENDITION_STATE_READY,
    MIN_PARAGRAPHS_FOR_IMAGE_PLAN,
    REVIEW_ANCHOR_KIND_MEDIA,
    REVIEW_ANCHOR_KIND_PARAGRAPH,
    REVIEW_ANCHOR_KIND_WHOLE,
    REVIEW_ANCHOR_KINDS,
    REVIEW_CATEGORIES,
    REVIEW_SEVERITIES,
    REVIEW_SEVERITY_BLOCKING,
    RULE_KINDS,
    RULE_SET_VERSION,
    RULE_STATUS_ERRORED,
    RULE_STATUS_FAILED,
    RULE_STATUS_PASSED,
    RULE_STATUS_SKIPPED,
    RULE_STATUSES,
    RUN_OUTCOME_BLOCKED,
    RUN_OUTCOME_ERRORED,
    RUN_OUTCOME_PASSED,
    RUN_OUTCOME_WARNED,
    RUN_OUTCOMES,
    SKIP_REASON_CROSS_ITEM_SCOPE_OUT_OF_PACKAGE,
    SKIP_REASON_MODEL_RULE_NOT_ENABLED,
    SKIP_REASON_NO_EFFECTIVE_BASELINE,
    SKIP_REASON_NO_SCENE_LINKAGE_IN_SCHEMA,
    SKIP_REASON_PLATFORM_MEDIA_LIMITS_NOT_CONFIGURED,
    SKIP_REASONS,
    SUBJECT_STATE_CURRENT,
    SUBJECT_STATE_STALE,
    TRIGGER_KIND_BATCH,
    TRIGGER_KIND_MANUAL,
    TRIGGER_KINDS,
    CheckSubject,
    MediaSnapshot,
    ParagraphSnapshot,
    RenditionSnapshot,
    failed as review_failed,
)
from rednote.modules.versions.repository import VersionRepository
from rednote.modules.baseline.repository import BaselineRepository
from rednote.platform.base import Base
from rednote.platform.configuration import Settings
from rednote.platform.database import get_engine, reset_engines, session_scope
from rednote.platform.errors import _STATUS_BY_CODE, ApiError, ErrorCode
from rednote.platform.immutability import IMMUTABLE_RECORD_SQLSTATE

# 复用 content 包的造数夹具与助手（autouse 夹具导入后在本模块同样生效）。
# 只读复用，不往 `test_content_wp26.py` 里写任何东西。
from test_content_wp26 import (
    _clean_own_rows as _clean_content_rows,  # noqa: F401  （autouse 夹具，按名注入）
    _create_user,
    _ensure_own_tables as _ensure_content_tables,  # noqa: F401  （同上）
    _seed_project,
    _seed_valid_content,
    _sqlstate,
)

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
#: WP30 私有测试库（**不是**共享的 `rednote_test`）。
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test_wp30"
TEST_DATABASE_URL_ENV = "REDNOTE_TEST_DATABASE_URL"

#: DROP/TRUNCATE 顺序：引用方在前（findings → rule_results → runs）。
DROP_ORDER = (
    "self_check_findings",
    "self_check_rule_results",
    "self_check_runs",
)
_TABLE_NAMES = tuple(OWN_TABLES)

#: PostgreSQL unique_violation。
UNIQUE_VIOLATION_SQLSTATE = "23505"


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
def _ensure_own_tables(_ensure_content_tables: None) -> Iterator[None]:
    """只重建本包 3 张表 + 5 个触发器。

    依赖 content 的建表夹具是**顺序保证**：本包三张表有 FK 指向
    `content_items` / `content_versions`，PostgreSQL 在 CREATE TABLE 时要求
    被引用表已存在；而 content 的夹具会 DROP/CREATE 它自己的表，
    若顺序反了，本包的 FK 会被 CASCADE 摘掉。

    `create_all` 只建传入的三张表（理由见 `test_content_wp26.py`：
    全量 `metadata.sorted_tables` 会被不在本仓的 `domain_events` 卡住）。
    触发器先 DROP 再 CREATE：`CREATE TRIGGER` 不幂等，而
    `CREATE OR REPLACE FUNCTION` 幂等。

    `fks_before == fks_after` 断言守的是"夹具没有增删**跨包入向**外键"。
    本包三张表目前**没有**任何跨包入向 FK（没人引用自检表），因此两侧都是
    空集 —— 断言仍有意义：将来 WP31 的关口若给 `self_check_runs` 加一条入向
    FK，这条断言会在夹具误删它时当场红。
    """
    reset_engines()
    engine = get_engine(_make_settings())

    fks_before = cross_package_foreign_keys(engine, _TABLE_NAMES)
    dropped, kept = drop_own_tables(engine, DROP_ORDER)
    Base.metadata.create_all(
        engine,
        tables=[Base.metadata.tables[name] for name in OWN_TABLES],
    )
    fks_after = cross_package_foreign_keys(engine, _TABLE_NAMES)
    assert fks_after == fks_before, (
        f"建表夹具改变了跨包外键集合：before={sorted(fks_before)} "
        f"after={sorted(fks_after)}（dropped={dropped} kept={kept}）"
    )

    truncate_list = truncate_targets(engine, DROP_ORDER)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(truncate_list)))
        for _table, func_ddl, trigger_ddl, drop_trigger, _drop_func in (
            r_ddl.all_trigger_ddl()
        ):
            conn.execute(text(drop_trigger))
            conn.execute(text(func_ddl))
            conn.execute(text(trigger_ddl))
    yield


@pytest.fixture(autouse=True)
def _clean_own_rows(_clean_content_rows: None) -> Iterator[None]:
    """每个用例前清空本包 3 张表 + WP29 的反馈表。

    为什么只能 TRUNCATE：本包三张表与 `content_feedback_events` 都是不可变表
    （行级 BEFORE 触发器拦 UPDATE/DELETE，`23001`）；TRUNCATE 不触发行级触发器。

    为什么要连 `content_feedback_items` 一起清：本包的阻断项会往那张表写
    AI 反馈，而去重判据（R4）**数具体条数**。兄弟用例留下的反馈会让
    "重跑两轮 ⇒ 恰 1 条"变成"恰 2 条"—— 症状与"去重实现错了"完全同形。
    `truncate_targets` 的闭包已把它纳入（本包 findings 引用它）。
    """
    reset_engines()
    engine = get_engine(_make_settings())
    targets = truncate_targets(engine, (*DROP_ORDER, "content_feedback_items"))
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(targets)))
    yield


# ===========================================================================
# 造数
# ===========================================================================


@dataclass(frozen=True)
class ReviewEnv:
    """本文件的最小环境：一个用户 + 一个项目（每次用例重新造）。"""

    settings: Settings
    project_id: uuid.UUID
    owner_id: uuid.UUID


@dataclass(frozen=True)
class SeededContent:
    """一条内容 + 一版正文 + 该正文的段落 ref。"""

    content_id: uuid.UUID
    version_id: uuid.UUID
    body: str
    paragraph_refs: tuple[str, ...]


@pytest.fixture
def env() -> ReviewEnv:
    reset_engines()
    settings = _make_settings()
    owner_id = _create_user(settings)
    project_id = _seed_project(settings, owner_id=owner_id)
    return ReviewEnv(settings=settings, project_id=project_id, owner_id=owner_id)


def _execute(env: ReviewEnv, sql: str, params: dict[str, Any] | None = None) -> None:
    """在**独立事务**里执行一条原生 SQL（退出即提交）。"""
    with session_scope(env.settings) as session:
        session.execute(text(sql), params or {})


def _scalar(env: ReviewEnv, sql: str, params: dict[str, Any] | None = None) -> Any:
    with session_scope(env.settings) as session:
        return session.execute(text(sql), params or {}).scalar_one_or_none()


def _rows(
    env: ReviewEnv, sql: str, params: dict[str, Any] | None = None
) -> list[tuple[Any, ...]]:
    with session_scope(env.settings) as session:
        return [
            tuple(row) for row in session.execute(text(sql), params or {}).fetchall()
        ]


def _jsonb_literal(payload: Any) -> str:
    """把 Python 值变成一段 JSONB 字面量 SQL。

    用 `json.dumps` 而不是手拼：手拼的引号转义错一次，症状是"插入失败"，
    看起来像实现坏了。
    """
    return "'" + json.dumps(payload, ensure_ascii=False).replace("'", "''") + "'::jsonb"


def _seed_content(
    env: ReviewEnv,
    *,
    paragraphs: int = 1,
    body: str | None = None,
    bindings: list[dict[str, Any]] | None = None,
) -> SeededContent:
    """造"一条内容 + 一版正文"（含 `version_nodes` 配对行）。

    段落 ref 由 `media_bridge` 从同一份正文派生 —— 这正是被测代码判
    `MEDIA_PARAGRAPH_REF_STALE` 时用的生成器（本包零切段实现）。
    """
    if body is None:
        body = "\n\n".join(
            f"第 {index + 1} 段：这是第 {index + 1} 段的内容。"
            for index in range(paragraphs)
        )
    content_id, version_id = _seed_valid_content(
        env,
        project_id=env.project_id,
        body=body,
        bindings=_jsonb_literal(bindings or []),
    )
    refs = tuple(
        ref for _index, _text, ref in media_bridge.split_body_into_paragraphs(body)
    )
    return SeededContent(
        content_id=content_id, version_id=version_id, body=body, paragraph_refs=refs
    )


def _seed_content_with_blueprint(
    env: ReviewEnv, *, paragraphs: int = 3
) -> SeededContent:
    """造"一条内容 + 一版**挂了叙事蓝图**的正文"。

    ## 为什么不能复用 `_seed_valid_content` + 事后挂蓝图

    `content/ddl.py` 有一条触发器要求"蓝图必须属于同一条内容"，而
    `content_versions` 是**不可变**表（挂完再 UPDATE 会撞 23001）。
    因此蓝图必须在 INSERT 版本行的**那一刻**就已存在且属于同一条内容
    ⇒ 三行必须按 内容 → 蓝图 → 版本 的顺序自己插一次。

    `current_version_id` 刻意**不**回填（与 `_seed_valid_content` 同款）：
    "投影为 NULL 时回落末版"是契约 §2 的合法中间态，顺带让本包的
    `current_content_version_id` 的回落分支在每个用例里都被走到。
    """
    body = "\n\n".join(
        f"第 {index + 1} 段：这是第 {index + 1} 段的内容。"
        for index in range(paragraphs)
    )
    content_id = uuid.uuid4()
    blueprint_id = uuid.uuid4()
    version_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO content_items (id, project_id, state,"
                " current_version_no, lock_version)"
                " VALUES (:c, :p, 'DRAFT', 0, 0)"
            ),
            {"c": content_id, "p": env.project_id},
        )
        session.execute(
            text(
                "INSERT INTO narrative_blueprints (id, content_item_id,"
                " opening_function, context, decision_process, usage_scene,"
                " evidence_beats, limitation, conclusion_strength,"
                " interaction_question, paragraph_plan)"
                " VALUES (:id, :c, '开场', '场景', '决策', '通勤', '证据', '局限',"
                " 'DIRECT', '你怎么看', '三段')"
            ),
            {"id": blueprint_id, "c": content_id},
        )
        session.execute(
            text(
                "INSERT INTO version_nodes (id, entity_type, entity_id,"
                " revision_no, payload_hash) VALUES (:id, 'CONTENT', :e, 1, :h)"
            ),
            {"id": version_id, "e": content_id, "h": f"h-{version_id.hex[:12]}"},
        )
        session.execute(
            text(
                "INSERT INTO content_versions (id, content_item_id, version_no,"
                " title, body, tags, bindings, payload, payload_hash,"
                " schema_version, blueprint_id)"
                " VALUES (:id, :c, 1, '标题', :body, '[]'::jsonb, '[]'::jsonb,"
                " '{}'::jsonb, :ph, 1, :bp)"
            ),
            {
                "id": version_id,
                "c": content_id,
                "body": body,
                "ph": f"ph-{version_id.hex[:12]}",
                "bp": blueprint_id,
            },
        )
    refs = tuple(
        ref for _index, _text, ref in media_bridge.split_body_into_paragraphs(body)
    )
    return SeededContent(
        content_id=content_id, version_id=version_id, body=body, paragraph_refs=refs
    )


def _seed_baseline(
    env: ReviewEnv, *, brief_version_id: uuid.UUID | None = None
) -> uuid.UUID:
    """造一条生效基线并把 `projects.active_baseline_id` 指向它。

    ⚠️ 四条语句必须在**同一个事务**里：基线行受 WP22 的三条延迟约束触发器
    管（`COMMIT` 时校验），少任何一件都会在提交时才炸、且报错点落在本
    helper 里（很容易被误读成 WP30 的实现缺陷）。实测缺一不可：

    | # | 要求 | 触发器 | 违背时 |
    |---|---|---|---|
    | 1 | `id` 显式给 | —— | `NotNullViolation` |
    | 2 | 同 id 的 `version_nodes` 行（`entity_type='BASELINE'`、`entity_id = project_id`） | `rednote_check_baseline_version_consistency` | 23514 |
    | 3 | 同事务一条未被取代的 `step_approvals`（step 3 / BASELINE / APPROVED） | `rednote_check_baseline_approval` | 23514 |

    写法照 `test_planning_wp24.py` 的同名 helper（**不重写第二份**造数口径）。

    `brief_version_id=None` ⇒ claim 集合为空。这是本文件里"让事实类规则
    **能命中**"的最轻造数路径：基线存在（于是规则不 SKIPPED），
    但 claim 范围为空（于是任何带 `claimId` 的绑定都超出范围）。
    这与生产里的真实命中场景同形（基线前进后旧 claim 不在新范围里），
    只是造数便宜得多。
    """
    baseline_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO version_nodes (id, entity_type, entity_id,"
                " revision_no, payload_hash, schema_version, project_id)"
                " VALUES (:id, 'BASELINE', :pid, 1, :h, 1, :pid)"
            ),
            {
                "id": str(baseline_id),
                "pid": str(env.project_id),
                "h": "0" * 64,
            },
        )
        session.execute(
            text(
                "INSERT INTO production_baselines (id, project_id,"
                " brief_version_id, ruleset_version, snapshot, change_reason)"
                " VALUES (:id, :pid, :bvid, :rs, '{}'::jsonb, 'WP30 判据铺底')"
            ),
            {
                "id": str(baseline_id),
                "pid": str(env.project_id),
                "bvid": None if brief_version_id is None else str(brief_version_id),
                "rs": f"rs-{uuid.uuid4().hex[:8]}",
            },
        )
        session.execute(
            text(
                "INSERT INTO step_approvals (project_id, step_no, subject_kind,"
                " subject_version_id, decision, reason, decided_by)"
                " VALUES (:pid, 3, 'BASELINE', :id, 'APPROVED', :reason, :who)"
            ),
            {
                "pid": str(env.project_id),
                "id": str(baseline_id),
                "reason": "WP30 判据铺底（造基线用的批准）",
                "who": str(env.owner_id),
            },
        )
        session.execute(
            text("UPDATE projects SET active_baseline_id = :bid WHERE id = :pid"),
            {"bid": baseline_id, "pid": env.project_id},
        )
    return baseline_id


def _null_bindings(paragraph_count: int) -> list[dict[str, Any]]:
    """一份**合法**的 `bindings`：段号齐全、`claimId=null` + 非空理由。

    `content/schemas.validate_bindings` 要求段号 `0..n-1` 各一次
    （编辑路径会重跑它，WP29 的 R1 强制）；缺了它，`edit_content` 会在
    "改正文"这一步以 422 失败，而那与"本包实现坏了"在输出上完全同形。
    """
    return [
        {
            BINDING_KEY_PARAGRAPH_INDEX: index,
            BINDING_KEY_CLAIM_ID: None,
            "reason": "本段无对应事实",
            "text": f"第 {index + 1} 段",
        }
        for index in range(paragraph_count)
    ]


def _seed_media_item(
    env: ReviewEnv,
    *,
    content_id: uuid.UUID,
    paragraph_ref: str,
    sort_order: int = 0,
    state: str = "PENDING",
) -> uuid.UUID:
    """造一条 `media_items` 身份行（投影为空 ⇒ 没有产物档位）。"""
    media_item_id = uuid.uuid4()
    _execute(
        env,
        "INSERT INTO media_items (id, project_id, content_item_id, paragraph_ref,"
        " sort_order, state, current_version_no, created_by, lock_version)"
        " VALUES (:id, :pid, :cid, :ref, :so, :state, 0, :by, 0)",
        {
            "id": media_item_id,
            "pid": env.project_id,
            "cid": content_id,
            "ref": paragraph_ref,
            "so": sort_order,
            "state": state,
            "by": env.owner_id,
        },
    )
    return media_item_id


def _run(
    env: ReviewEnv,
    content_id: uuid.UUID,
    *,
    trigger_kind: str = TRIGGER_KIND_MANUAL,
) -> r_service.RunDetail:
    """经服务层跑一轮自检（独立事务，退出即提交）。"""
    with session_scope(env.settings) as session:
        return r_service.run_self_check(
            ReviewRepository(session),
            ContentRepository(session),
            PlanningRepository(session),
            FeedbackRepository(session),
            content_id=content_id,
            trigger_kind=trigger_kind,
            created_by=env.owner_id,
        )


def _result_by_code(detail: r_service.RunDetail) -> dict[str, dict[str, Any]]:
    return {item["ruleCode"]: item for item in detail.rule_results}


# ===========================================================================
# 一、纯函数层：阳性 / 阴性对照（**本包最核心的防线，R1**）
# ===========================================================================

_CLAIM_IN_SCOPE = uuid.UUID("11111111-1111-4111-8111-111111111111")
_CLAIM_OUT_OF_SCOPE = uuid.UUID("22222222-2222-4222-8222-222222222222")
_BASELINE_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_MEDIA_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
_RENDITION_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")

_P0 = ParagraphSnapshot(index=0, text="第一段正文。", ref="p0-aaaaaaaa")
_P1 = ParagraphSnapshot(index=1, text="第二段正文。", ref="p1-bbbbbbbb")
_P2 = ParagraphSnapshot(index=2, text="第三段正文。", ref="p2-cccccccc")


def _subject(**overrides: Any) -> CheckSubject:
    """构造一个**什么都正常**的 `CheckSubject`，再按需覆盖一两个字段。

    默认值刻意让所有已实现的规则都 `PASSED`：
    三段正文、有蓝图、无图位、基线有一条 claim 且被绑定、平台限制已配置且
    产物在界内、正文无禁用词。于是每条阳性用例只需覆盖**它自己那一项**，
    "这条规则为什么命中"在 diff 里一眼可见。
    """
    base: dict[str, Any] = {
        "content_id": uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001"),
        "project_id": uuid.UUID("aaaaaaaa-0000-4000-8000-000000000002"),
        "subject_version_id": uuid.UUID("aaaaaaaa-0000-4000-8000-000000000003"),
        "version_no": 1,
        "title": "标题",
        "body": "第一段正文。\n\n第二段正文。\n\n第三段正文。",
        "tags": ("标签",),
        "bindings": (
            {
                BINDING_KEY_PARAGRAPH_INDEX: 0,
                BINDING_KEY_CLAIM_ID: str(_CLAIM_IN_SCOPE),
            },
        ),
        "blueprint_id": uuid.UUID("aaaaaaaa-0000-4000-8000-000000000004"),
        "paragraphs": (_P0, _P1, _P2),
        "media_items": (),
        "effective_baseline_id": _BASELINE_ID,
        "baseline_claim_ids": frozenset({_CLAIM_IN_SCOPE}),
        "generated_against_baseline_id": _BASELINE_ID,
        "platform_limits_configured": True,
        "platform_limits": {
            "maxWidth": 1440,
            "maxHeight": 1920,
            "maxByteSize": 2_097_152,
        },
    }
    base.update(overrides)
    return CheckSubject(**base)


def _ready_media(
    *,
    paragraph_ref: str | None = _P0.ref,
    state: str = MEDIA_ITEM_STATE_READY,
    width: int = 1080,
    height: int = 1440,
    byte_size: int = 500_000,
) -> MediaSnapshot:
    return MediaSnapshot(
        media_item_id=_MEDIA_ID,
        paragraph_ref=paragraph_ref,
        sort_order=0,
        state=state,
        renditions=(
            RenditionSnapshot(
                rendition_id=_RENDITION_ID,
                kind="DELIVERY",
                state=MEDIA_RENDITION_STATE_READY,
                width=width,
                height=height,
                byte_size=byte_size,
                image_format="jpg",
            ),
        ),
    )


#: 每条**已实现**规则的阳性输入（必须 FAILED）。
#: 加一条规则不补这里 ⇒ `test_every_implemented_rule_has_both_cases` 当场红。
_POSITIVE: dict[str, CheckSubject] = {
    "FACT_BINDING_CLAIM_OUT_OF_SCOPE": _subject(
        bindings=(
            {
                BINDING_KEY_PARAGRAPH_INDEX: 1,
                BINDING_KEY_CLAIM_ID: str(_CLAIM_OUT_OF_SCOPE),
            },
        )
    ),
    "FACT_UNBOUND_INCLUDED_CLAIM": _subject(
        baseline_claim_ids=frozenset({_CLAIM_IN_SCOPE, _CLAIM_OUT_OF_SCOPE})
    ),
    "STRUCTURE_PARAGRAPH_COUNT_BELOW_IMAGE_PLAN": _subject(paragraphs=(_P0,)),
    "STRUCTURE_VERSION_WITHOUT_BLUEPRINT": _subject(blueprint_id=None),
    "MEDIA_PARAGRAPH_REF_STALE": _subject(
        media_items=(_ready_media(paragraph_ref="p9-deadbeef"),)
    ),
    "MEDIA_ITEM_NOT_READY": _subject(media_items=(_ready_media(state="PENDING"),)),
    "TECH_RENDITION_OUT_OF_PLATFORM_LIMITS": _subject(
        media_items=(_ready_media(width=4096),)
    ),
    "OTHER_BANNED_TERM_HIT": _subject(
        paragraphs=(
            ParagraphSnapshot(index=0, text="这是最好的选择。", ref=_P0.ref),
            _P1,
            _P2,
        )
    ),
}

#: 每条**已实现**规则的阴性输入（必须 PASSED）。
#: 全部用默认 subject —— 默认值刻意让所有规则通过（见 `_subject` 的说明）。
#: 对媒体/技术三条另给一个"有图且一切正常"的输入，否则"没有图位"这种
#: 空集合输入会让它们**真空通过**（vacuous），而真空通过不足以证明
#: "这条规则在有数据时会放行"。
_NEGATIVE: dict[str, CheckSubject] = {
    "FACT_BINDING_CLAIM_OUT_OF_SCOPE": _subject(),
    "FACT_UNBOUND_INCLUDED_CLAIM": _subject(),
    "STRUCTURE_PARAGRAPH_COUNT_BELOW_IMAGE_PLAN": _subject(),
    "STRUCTURE_VERSION_WITHOUT_BLUEPRINT": _subject(),
    "MEDIA_PARAGRAPH_REF_STALE": _subject(media_items=(_ready_media(),)),
    "MEDIA_ITEM_NOT_READY": _subject(media_items=(_ready_media(),)),
    "TECH_RENDITION_OUT_OF_PLATFORM_LIMITS": _subject(
        media_items=(_ready_media(),)
    ),
    "OTHER_BANNED_TERM_HIT": _subject(),
}

#: 三条**恒 SKIPPED**的规则及其受控原因码。
_ALWAYS_SKIPPED: dict[str, str] = {
    "TONE_MODEL_JUDGEMENT": SKIP_REASON_MODEL_RULE_NOT_ENABLED,
    "DUPLICATE_CROSS_ITEM_SIMILARITY": SKIP_REASON_CROSS_ITEM_SCOPE_OUT_OF_PACKAGE,
    "SCENE_LINKAGE_UNAVAILABLE": SKIP_REASON_NO_SCENE_LINKAGE_IN_SCHEMA,
}


def test_every_implemented_rule_has_both_cases() -> None:
    """**R1 的机械对账**：注册表 ⇄ 用例表三向集合相等。

    这条判据是本包全部纯函数用例的"活性下界"：加一条规则而不补阳性/阴性
    输入时它当场红。没有它，一条**没有阳性对照**的规则与一条**恒绿**的规则
    在测试输出上完全同形（都是绿的）。
    """
    implemented = {spec.code for spec in rules.RULES if spec.implemented}
    unimplemented = {spec.code for spec in rules.RULES if not spec.implemented}
    assert set(_POSITIVE) == implemented, (
        f"缺阳性用例：{sorted(implemented - set(_POSITIVE))}；"
        f"多余：{sorted(set(_POSITIVE) - implemented)}"
    )
    assert set(_NEGATIVE) == implemented, (
        f"缺阴性用例：{sorted(implemented - set(_NEGATIVE))}；"
        f"多余：{sorted(set(_NEGATIVE) - implemented)}"
    )
    assert set(_ALWAYS_SKIPPED) == unimplemented, (
        f"恒 SKIPPED 用例与注册表不一致：{sorted(unimplemented ^ set(_ALWAYS_SKIPPED))}"
    )


@pytest.mark.parametrize("code", sorted(_POSITIVE))
def test_rule_positive_case_fails(code: str) -> None:
    """阳性对照：存在一组输入让这条规则 **FAILED**（不是恒绿）。

    同时断言结论形状：严重度 = 注册表声明的默认值、至少一条 finding、
    每条 finding 的锚点落在取值域内、`evidence` 是**非空对象**。
    `evidence` 为空字典也算坏结论 —— 那条 finding 就只剩一句人话，
    下游无法机器判定"超出了多少"。
    """
    spec = rules.by_code(code)
    assert spec is not None
    outcome = spec.evaluator(_POSITIVE[code])
    assert outcome.status == RULE_STATUS_FAILED, (
        f"{code} 在阳性输入下没有命中 —— 它是一条恒绿规则（假防线）。"
    )
    assert outcome.severity == spec.default_severity
    assert outcome.findings, f"{code} 判为 FAILED 却零 finding"
    for finding in outcome.findings:
        assert finding.anchor_kind in REVIEW_ANCHOR_KINDS
        assert finding.message
        assert isinstance(finding.evidence, dict) and finding.evidence, (
            f"{code} 的 finding 没有机器可判的 evidence"
        )


@pytest.mark.parametrize("code", sorted(_NEGATIVE))
def test_rule_negative_case_passes(code: str) -> None:
    """阴性对照：存在一组输入让这条规则 **PASSED**（不是恒红）。

    恒红与恒绿同样是假防线：一条永远命中的规则会让运营学会无视自检结果。
    """
    spec = rules.by_code(code)
    assert spec is not None
    outcome = spec.evaluator(_NEGATIVE[code])
    assert outcome.status == RULE_STATUS_PASSED, (
        f"{code} 在阴性输入下仍然命中 —— 它是一条恒红规则，"
        f"实际结论 {outcome.status}/{outcome.skip_reason}"
    )
    assert outcome.severity is None
    assert outcome.findings == ()


@pytest.mark.parametrize(("code", "reason"), sorted(_ALWAYS_SKIPPED.items()))
def test_always_skipped_rule_reports_controlled_reason(
    code: str, reason: str
) -> None:
    """三类恒 SKIPPED：**有实现、有受控原因码**，不是缺席、不是伪造通过。

    断**具体**原因码而不是"skip_reason 非空"：原因码是前端渲染
    「未判 · 为什么」的依据，拼写漂移不会被任何别的判据抓到。
    """
    spec = rules.by_code(code)
    assert spec is not None
    assert spec.implemented is False
    assert spec.default_severity is None
    outcome = spec.evaluator(_subject())
    assert outcome.status == RULE_STATUS_SKIPPED
    assert outcome.skip_reason == reason
    assert outcome.findings == ()


def test_fact_rules_skip_without_effective_baseline() -> None:
    """无生效基线 ⇒ 两条事实规则都 **SKIPPED**，不是失败也不是通过。

    判据域为空时判失败会把每条 binding 都误报成超范围（恒红），
    判通过则是凭空声称"事实没问题"。
    """
    subject = _subject(effective_baseline_id=None, baseline_claim_ids=frozenset())
    for code in ("FACT_BINDING_CLAIM_OUT_OF_SCOPE", "FACT_UNBOUND_INCLUDED_CLAIM"):
        spec = rules.by_code(code)
        assert spec is not None
        outcome = spec.evaluator(subject)
        assert outcome.status == RULE_STATUS_SKIPPED
        assert outcome.skip_reason == SKIP_REASON_NO_EFFECTIVE_BASELINE


def test_tech_format_skips_when_platform_limits_not_configured() -> None:
    """平台限制未配置 ⇒ **SKIPPED**，不冒充通过（OP-64 仍 OPEN）。

    两种"未配置"都要走同一条路：① 整个键没有值；
    ② 有值但不含本规则认识的任何上限键（配置里只写了别的东西）。
    """
    spec = rules.by_code("TECH_RENDITION_OUT_OF_PLATFORM_LIMITS")
    assert spec is not None
    not_configured = _subject(
        platform_limits_configured=False,
        platform_limits=None,
        media_items=(_ready_media(width=4096),),
    )
    outcome = spec.evaluator(not_configured)
    assert outcome.status == RULE_STATUS_SKIPPED
    assert outcome.skip_reason == SKIP_REASON_PLATFORM_MEDIA_LIMITS_NOT_CONFIGURED

    configured_but_irrelevant = _subject(
        platform_limits_configured=True,
        platform_limits={"somethingElse": 1},
        media_items=(_ready_media(width=4096),),
    )
    outcome = spec.evaluator(configured_but_irrelevant)
    assert outcome.status == RULE_STATUS_SKIPPED
    assert outcome.skip_reason == SKIP_REASON_PLATFORM_MEDIA_LIMITS_NOT_CONFIGURED


def test_media_rules_anchor_on_the_media_item() -> None:
    """媒体类命中的锚点必须是 `MEDIA` 且带 `media_item_id`（前端要能跳到那张图）。"""
    for code in ("MEDIA_PARAGRAPH_REF_STALE", "MEDIA_ITEM_NOT_READY"):
        spec = rules.by_code(code)
        assert spec is not None
        outcome = spec.evaluator(_POSITIVE[code])
        assert [f.anchor_kind for f in outcome.findings] == [
            REVIEW_ANCHOR_KIND_MEDIA
        ]
        assert outcome.findings[0].media_item_id == _MEDIA_ID
        assert outcome.findings[0].paragraph_ref is None


def test_banned_term_hit_anchors_on_the_paragraph() -> None:
    """禁用词命中的锚点必须是 `PARAGRAPH` 且带那一段的 ref。"""
    spec = rules.by_code("OTHER_BANNED_TERM_HIT")
    assert spec is not None
    outcome = spec.evaluator(_POSITIVE["OTHER_BANNED_TERM_HIT"])
    assert [f.anchor_kind for f in outcome.findings] == [REVIEW_ANCHOR_KIND_PARAGRAPH]
    assert outcome.findings[0].paragraph_ref == _P0.ref
    assert outcome.findings[0].media_item_id is None
    assert outcome.findings[0].evidence["terms"] == ["最好"]


def test_fact_binding_out_of_scope_anchors_on_the_bound_paragraph() -> None:
    """事实命中落在 binding 声明的那一段上；段号越界时回落整篇。"""
    spec = rules.by_code("FACT_BINDING_CLAIM_OUT_OF_SCOPE")
    assert spec is not None

    on_paragraph = spec.evaluator(_POSITIVE["FACT_BINDING_CLAIM_OUT_OF_SCOPE"])
    assert on_paragraph.findings[0].anchor_kind == REVIEW_ANCHOR_KIND_PARAGRAPH
    assert on_paragraph.findings[0].paragraph_ref == _P1.ref
    # evidence 必须能回答"基线前进过吗"。
    assert on_paragraph.findings[0].evidence["effectiveBaselineId"] == str(
        _BASELINE_ID
    )

    out_of_range = spec.evaluator(
        _subject(
            bindings=(
                {
                    BINDING_KEY_PARAGRAPH_INDEX: 99,
                    BINDING_KEY_CLAIM_ID: str(_CLAIM_OUT_OF_SCOPE),
                },
            )
        )
    )
    assert out_of_range.findings[0].anchor_kind == REVIEW_ANCHOR_KIND_WHOLE
    assert out_of_range.findings[0].paragraph_ref is None


def test_binding_without_claim_id_is_not_flagged() -> None:
    """`claimId: null` 是**合法形状**（配非空理由），不该被判成超出范围。

    判错的后果：每条"本段无对应事实"的绑定都会变成一条 BLOCKING 反馈
    ⇒ 所有内容都审不过去。
    """
    spec = rules.by_code("FACT_BINDING_CLAIM_OUT_OF_SCOPE")
    assert spec is not None
    outcome = spec.evaluator(
        _subject(
            bindings=(
                {
                    BINDING_KEY_PARAGRAPH_INDEX: 0,
                    BINDING_KEY_CLAIM_ID: None,
                    "reason": "本段无对应事实",
                },
            )
        )
    )
    assert outcome.status == RULE_STATUS_PASSED


def test_malformed_claim_id_is_flagged_as_out_of_scope() -> None:
    """解析不出 uuid 的 `claimId` 按"超出判据域"处置，并在 evidence 里标明。

    它只可能来自绕过 `validate_bindings` 的写入路径（脚本 / 数据修复），
    静默放行等于让坏数据通过自检。
    """
    spec = rules.by_code("FACT_BINDING_CLAIM_OUT_OF_SCOPE")
    assert spec is not None
    outcome = spec.evaluator(
        _subject(
            bindings=(
                {BINDING_KEY_PARAGRAPH_INDEX: 0, BINDING_KEY_CLAIM_ID: "不是-uuid"},
            )
        )
    )
    assert outcome.status == RULE_STATUS_FAILED
    assert outcome.findings[0].evidence["claimIdParsed"] is False


# ===========================================================================
# 二、取值域闭集 + 跨包 parity
# ===========================================================================


def test_value_domains_are_closed_sets() -> None:
    """7 个闭集逐值断言（**断具体取值集合，不断条数**）。

    断条数（`len(...) == 8`）会让"改掉一个值的拼写"恒绿 ——
    而拼写漂移正是取值域最常见的失效方式。
    """
    assert REVIEW_CATEGORIES == {
        "FACT",
        "TONE",
        "STRUCTURE",
        "SCENE",
        "DUPLICATE",
        "MEDIA_CONSISTENCY",
        "TECH_FORMAT",
        "OTHER",
    }
    assert REVIEW_SEVERITIES == {"BLOCKING", "WARNING", "INFO"}
    assert REVIEW_ANCHOR_KINDS == {"WHOLE", "PARAGRAPH", "MEDIA"}
    assert RULE_KINDS == {"HARD", "SOFT"}
    assert RULE_STATUSES == {"PASSED", "FAILED", "SKIPPED", "ERRORED"}
    assert RUN_OUTCOMES == {"PASSED", "WARNED", "BLOCKED", "ERRORED"}
    # ★ 刻意**不含** `AUTO`：自动触发本包不做，不留空转取值。
    assert TRIGGER_KINDS == {"MANUAL", "BATCH"}
    assert "AUTO" not in TRIGGER_KINDS


def test_category_values_match_feedback_package() -> None:
    """**跨包 parity**：本包 8 类的**值**必须与 WP29 已落库的那 8 个相等。

    名字各写一份（平级模块不可互 import），值必须相同 —— 否则本包把
    `category` 传给 `create_feedback` 时会被 422 拒绝
    （`FEEDBACK_CATEGORY_INVALID`），表现是"自检跑完了但阻断项一条都没落成
    反馈"⇒ WP29 的审核闸**放行**（静默事故）。

    先例：`test_content_wp26.py::test_task_kind_constants_match_prompts`
    守 content ⇄ prompts。
    """
    assert REVIEW_CATEGORIES == feedback_types.FEEDBACK_CATEGORIES


def test_severity_and_anchor_and_scope_values_match_feedback_package() -> None:
    """严重度 / 锚点 / scope / 作者类型的值同样要与 WP29 对齐。"""
    assert REVIEW_SEVERITIES == feedback_types.FEEDBACK_SEVERITIES
    assert REVIEW_ANCHOR_KINDS == feedback_types.ANCHOR_KINDS
    assert REVIEW_SEVERITY_BLOCKING == feedback_types.FEEDBACK_SEVERITY_BLOCKING
    assert AI_FEEDBACK_SCOPE == feedback_types.FEEDBACK_SCOPE_CURRENT_ONLY
    assert AI_AUTHOR_KIND == feedback_types.ACTOR_KIND_AI
    assert AI_AUTHOR_KIND in feedback_types.ACTOR_KINDS


def test_boundary_constants_match_their_source_packages() -> None:
    """三个"各写一份"的边界常量必须与来源包同值。

    · `MAX_PARAGRAPH_REF_CHARS` 决定 `paragraph_ref` 列宽：漂移会让
      "media 生成的 ref 插不进本包的列"；
    · `MIN_PARAGRAPHS_FOR_IMAGE_PLAN` 是结构规则的阈值，权威是
      `media/types.IMAGES_PER_CONTENT`；
    · 两个 `READY` 是媒体与产物状态的判据，漂移会让规则静默恒绿。
    """
    assert MAX_PARAGRAPH_REF_CHARS == media_types.MAX_PARAGRAPH_REF_CHARS
    assert MAX_PARAGRAPH_REF_CHARS == feedback_types.MAX_PARAGRAPH_REF_CHARS
    assert MIN_PARAGRAPHS_FOR_IMAGE_PLAN == media_types.IMAGES_PER_CONTENT
    assert MEDIA_ITEM_STATE_READY == media_types.MEDIA_STATE_READY
    assert MEDIA_RENDITION_STATE_READY == media_types.RENDITION_STATE_READY
    assert (
        r_service.MAX_AI_FEEDBACK_BODY_CHARS
        == feedback_types.MAX_FEEDBACK_BODY_CHARS
    )


def test_binding_key_names_match_content_package() -> None:
    """`bindings` 的键名必须与 content 的权威常量同值。

    写错键名的症状是"规则永远读到 None ⇒ 恒绿"，而那是本包最怕的失败形态。
    """
    assert evaluators.BINDING_KEY_CLAIM_ID == BINDING_KEY_CLAIM_ID
    assert evaluators.BINDING_KEY_PARAGRAPH_INDEX == BINDING_KEY_PARAGRAPH_INDEX


def test_skip_reasons_fit_the_column_and_are_used() -> None:
    """受控原因码：都在列宽内，且**每一个都有真实消费点**（不空转）。

    一个声明了但从未被任何 evaluator 返回的原因码就是 ISS-95 族
    （"声称有这种情况"），因此这里对账"注册表能产出的原因码集合"。
    """
    assert all(0 < len(reason) <= MAX_SKIP_REASON_CHARS for reason in SKIP_REASONS)
    produced = set(_ALWAYS_SKIPPED.values()) | {
        SKIP_REASON_NO_EFFECTIVE_BASELINE,
        SKIP_REASON_PLATFORM_MEDIA_LIMITS_NOT_CONFIGURED,
    }
    assert produced == SKIP_REASONS, (
        f"有原因码没有任何消费点（空转）：{sorted(SKIP_REASONS - produced)}"
    )


def test_rule_registry_is_well_formed() -> None:
    """注册表：code 唯一、在列宽内、每类 ≥1 条、取值域合法、严重度与实现配对。

    导入期已 assert 过同样四条 —— 这里再断一次是刻意的：导入期 assert 在
    `python -O` 下会被剥掉，而本仓的 CI 不保证不带 `-O`。
    """
    codes = [spec.code for spec in rules.RULES]
    assert len(codes) == len(set(codes))
    assert all(0 < len(code) <= MAX_RULE_CODE_CHARS for code in codes)
    assert {spec.category for spec in rules.RULES} == REVIEW_CATEGORIES
    for spec in rules.RULES:
        assert spec.kind in RULE_KINDS
        assert spec.anchor_kind in REVIEW_ANCHOR_KINDS
        assert (spec.default_severity is None) == (not spec.implemented)
        if spec.default_severity is not None:
            assert spec.default_severity in REVIEW_SEVERITIES
    assert rules.all_codes() == set(codes)
    assert rules.by_code("NOT_A_RULE") is None
    assert {spec.code for spec in rules.by_category("FACT")} == {
        "FACT_BINDING_CLAIM_OUT_OF_SCOPE",
        "FACT_UNBOUND_INCLUDED_CLAIM",
    }


def test_rule_catalog_projects_the_registry_without_touching_db() -> None:
    """规则目录端点的数据源是**纯函数**：不连库、与注册表逐条对齐。

    这正是"把注册表放在代码里而不是配置表里"换来的性质：
    "有哪些规则"可以在不连库的情况下回答 ⇒ 阳性对照判据才有载体。
    """
    catalog = r_service.rule_catalog()
    assert catalog["ruleSetVersion"] == RULE_SET_VERSION
    assert [item["code"] for item in catalog["rules"]] == [
        spec.code for spec in rules.RULES
    ]
    unimplemented = {
        item["code"] for item in catalog["rules"] if not item["implemented"]
    }
    assert unimplemented == set(_ALWAYS_SKIPPED)
    assert all(
        item["defaultSeverity"] is None
        for item in catalog["rules"]
        if not item["implemented"]
    )


# ===========================================================================
# 三、数据库行为层
# ===========================================================================


def _insert_run_raw(
    env: ReviewEnv,
    seed: SeededContent,
    *,
    run_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    content_id: uuid.UUID | None = None,
    subject_version_id: uuid.UUID | None = None,
    outcome: str = RUN_OUTCOME_PASSED,
    blocking_count: int = 0,
    warning_count: int = 0,
    info_count: int = 0,
    evaluated_count: int = 1,
    skipped_count: int = 0,
    errored_count: int = 0,
    immediate: bool = False,
) -> uuid.UUID:
    """**绕过服务层**插一条轮次（数据库行为层判据用）。

    绕过是有意的：服务层会在写入前算出自洽的计数，DB 判据要证明
    "即便绕过它，CHECK 与触发器也拦得住"。

    `immediate=True` 时在同一事务里 `SET CONSTRAINTS ALL IMMEDIATE` ——
    一致性触发器是 `DEFERRABLE INITIALLY DEFERRED`，不显式提前就要等到
    COMMIT 才拒绝，而那会被误读成"防线失效"。
    """
    run_id = run_id or uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO self_check_runs (id, project_id, content_item_id,"
                " subject_version_id, rule_set_version, trigger_kind, outcome,"
                " blocking_count, warning_count, info_count, evaluated_count,"
                " skipped_count, errored_count, started_at, finished_at, created_by)"
                " VALUES (:id, :pid, :cid, :vid, 1, :tk, :outcome, :bc, :wc, :ic,"
                " :ec, :sc, :errc, now(), now(), :by)"
            ),
            {
                "id": run_id,
                "pid": project_id or env.project_id,
                "cid": content_id or seed.content_id,
                "vid": subject_version_id or seed.version_id,
                "tk": TRIGGER_KIND_MANUAL,
                "outcome": outcome,
                "bc": blocking_count,
                "wc": warning_count,
                "ic": info_count,
                "ec": evaluated_count,
                "sc": skipped_count,
                "errc": errored_count,
                "by": env.owner_id,
            },
        )
        if immediate:
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    return run_id


def _insert_rule_result_raw(
    env: ReviewEnv,
    run_id: uuid.UUID,
    *,
    result_id: uuid.UUID | None = None,
    rule_code: str = "STRUCTURE_VERSION_WITHOUT_BLUEPRINT",
    category: str = "STRUCTURE",
    rule_kind: str = "HARD",
    status: str = RULE_STATUS_PASSED,
    severity: str | None = None,
    skip_reason: str | None = None,
    error_note: str | None = None,
    finding_count: int = 0,
) -> uuid.UUID:
    result_id = result_id or uuid.uuid4()
    _execute(
        env,
        "INSERT INTO self_check_rule_results (id, run_id, rule_code, category,"
        " rule_kind, status, severity, skip_reason, error_note, finding_count)"
        " VALUES (:id, :rid, :code, :cat, :kind, :status, :sev, :skip, :note, :fc)",
        {
            "id": result_id,
            "rid": run_id,
            "code": rule_code,
            "cat": category,
            "kind": rule_kind,
            "status": status,
            "sev": severity,
            "skip": skip_reason,
            "note": error_note,
            "fc": finding_count,
        },
    )
    return result_id


def _insert_finding_raw(
    env: ReviewEnv,
    result_id: uuid.UUID,
    *,
    anchor_kind: str = REVIEW_ANCHOR_KIND_WHOLE,
    paragraph_ref: str | None = None,
    media_item_id: uuid.UUID | None = None,
    message: str = "数据库判据用的命中",
    evidence: str = "'{}'::jsonb",
    feedback_item_id: uuid.UUID | None = None,
    immediate: bool = False,
) -> uuid.UUID:
    finding_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO self_check_findings (id, rule_result_id, anchor_kind,"
                " paragraph_ref, media_item_id, message, evidence, feedback_item_id)"
                f" VALUES (:id, :rid, :ak, :ref, :mid, :msg, {evidence}, :fid)"
            ),
            {
                "id": finding_id,
                "rid": result_id,
                "ak": anchor_kind,
                "ref": paragraph_ref,
                "mid": media_item_id,
                "msg": message,
                "fid": feedback_item_id,
            },
        )
        if immediate:
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    return finding_id


@pytest.mark.parametrize("table", sorted(OWN_TABLES))
def test_immutable_trigger_rejects_update(env: ReviewEnv, table: str) -> None:
    """三张表全部是只追加表：裸 UPDATE 被 **23001** 拒。

    断 sqlstate 而不只断"抛了异常"：`pytest.raises(DBAPIError)` 无法区分
    "真的被不可变守卫拦住了"与"因为别的约束失败"（接缝冻结 §6.2 第 4 条）。
    """
    seed = _seed_content(env)
    run_id = _insert_run_raw(env, seed)
    result_id = _insert_rule_result_raw(env, run_id)
    finding_id = _insert_finding_raw(env, result_id)
    target_id = {
        "self_check_runs": run_id,
        "self_check_rule_results": result_id,
        "self_check_findings": finding_id,
    }[table]
    column = {
        "self_check_runs": "trigger_kind = 'BATCH'",
        "self_check_rule_results": "rule_code = '偷偷改写'",
        "self_check_findings": "message = '偷偷改写'",
    }[table]

    with pytest.raises(DBAPIError) as excinfo:
        _execute(env, f"UPDATE {table} SET {column} WHERE id = :id", {"id": target_id})
    assert _sqlstate(excinfo.value) == IMMUTABLE_RECORD_SQLSTATE


def test_immutable_trigger_rejects_delete(env: ReviewEnv) -> None:
    """DELETE 同样被 23001 拒 —— 自检历史不能被抹掉。"""
    seed = _seed_content(env)
    run_id = _insert_run_raw(env, seed)
    with pytest.raises(DBAPIError) as excinfo:
        _execute(env, "DELETE FROM self_check_runs WHERE id = :id", {"id": run_id})
    assert _sqlstate(excinfo.value) == IMMUTABLE_RECORD_SQLSTATE
    assert _scalar(
        env, "SELECT count(*) FROM self_check_runs WHERE id = :id", {"id": run_id}
    ) == 1


#: 四条双向等价式各一组**矛盾**输入（outcome 与计数对不上）。
_CONTRADICTORY_OUTCOMES = (
    # outcome=PASSED 但有阻断项 —— 读端点只看 outcome ⇒ 阻断内容显示为通过
    (RUN_OUTCOME_PASSED, 1, 0, 0),
    # outcome=BLOCKED 但零阻断项
    (RUN_OUTCOME_BLOCKED, 0, 0, 0),
    # outcome=WARNED 但零提示项
    (RUN_OUTCOME_WARNED, 0, 0, 0),
    # outcome=PASSED 但有规则没跑成（fail-closed 被绕过）
    (RUN_OUTCOME_PASSED, 0, 0, 1),
    # outcome=ERRORED 但零错误
    (RUN_OUTCOME_ERRORED, 0, 1, 0),
    # outcome=WARNED 但有阻断项（降级）
    (RUN_OUTCOME_WARNED, 1, 1, 0),
)


@pytest.mark.parametrize(
    ("outcome", "blocking", "warning", "errored"), _CONTRADICTORY_OUTCOMES
)
def test_outcome_equivalence_checks_reject_contradictions(
    env: ReviewEnv, outcome: str, blocking: int, warning: int, errored: int
) -> None:
    """**R6**：`outcome` 与五个计数漂移时数据库当场拒（**23514**）。

    `outcome` 由服务层算、计数也由服务层写 ⇒ 两处都可能漏改，而表现是
    "`outcome='PASSED'` 但 `blocking_count=1`"——读端点通常只看 outcome，
    于是阻断内容显示为通过。四条双向等价式把这个漂移钉在数据库层。
    """
    seed = _seed_content(env)
    with pytest.raises(DBAPIError) as excinfo:
        _insert_run_raw(
            env,
            seed,
            outcome=outcome,
            blocking_count=blocking,
            warning_count=warning,
            errored_count=errored,
        )
    assert _sqlstate(excinfo.value) == r_ddl.CONSISTENCY_SQLSTATE
    assert _scalar(
        env,
        "SELECT count(*) FROM self_check_runs WHERE content_item_id = :c",
        {"c": seed.content_id},
    ) == 0


def test_run_requires_at_least_one_evaluated_rule(env: ReviewEnv) -> None:
    """`evaluated_count = 0` 是**空转轮次**（"自检过了"但什么都没判）⇒ 23514。"""
    seed = _seed_content(env)
    with pytest.raises(DBAPIError) as excinfo:
        _insert_run_raw(env, seed, evaluated_count=0)
    assert _sqlstate(excinfo.value) == r_ddl.CONSISTENCY_SQLSTATE


#: 规则结论的四态配对，每条给一组坏数据。
_BAD_RULE_RESULTS = (
    # FAILED 却没有严重度 ⇒ 审核闸无从分级
    {"status": RULE_STATUS_FAILED, "severity": None, "finding_count": 1},
    # FAILED 却零 finding ⇒ 前端显示"有问题"但点不开任何一处
    {"status": RULE_STATUS_FAILED, "severity": "WARNING", "finding_count": 0},
    # 没 FAILED 却带了严重度 ⇒ 统计会把它算进命中
    {"status": RULE_STATUS_PASSED, "severity": "WARNING", "finding_count": 0},
    # SKIPPED 却没有原因码 ⇒ "为什么没判"无从回答
    {"status": RULE_STATUS_SKIPPED, "skip_reason": None},
    # 没 SKIPPED 却带了原因码
    {
        "status": RULE_STATUS_PASSED,
        "skip_reason": SKIP_REASON_MODEL_RULE_NOT_ENABLED,
    },
    # ERRORED 却没有诊断串
    {"status": RULE_STATUS_ERRORED, "error_note": None},
    # 没 ERRORED 却带了诊断串
    {"status": RULE_STATUS_PASSED, "error_note": "不该有"},
    # 取值域：未知 status（闭集，必须落 CHECK）
    {"status": "MAYBE"},
    # 取值域：未知 category
    {"status": RULE_STATUS_PASSED, "category": "VIBES"},
    # 取值域：未知 rule_kind
    {"status": RULE_STATUS_PASSED, "rule_kind": "MEDIUM"},
    # 取值域：未知 severity
    {"status": RULE_STATUS_FAILED, "severity": "CATASTROPHIC", "finding_count": 1},
    # rule_code 空串（开集但非空）
    {"status": RULE_STATUS_PASSED, "rule_code": ""},
)


@pytest.mark.parametrize("overrides", _BAD_RULE_RESULTS)
def test_rule_result_check_constraints_reject_bad_rows(
    env: ReviewEnv, overrides: dict[str, Any]
) -> None:
    """规则结论的四态配对 + 闭集取值域：坏行一律 **23514**。"""
    seed = _seed_content(env)
    run_id = _insert_run_raw(env, seed)
    with pytest.raises(DBAPIError) as excinfo:
        _insert_rule_result_raw(env, run_id, **overrides)
    assert _sqlstate(excinfo.value) == r_ddl.CONSISTENCY_SQLSTATE


def test_rule_code_and_skip_reason_have_no_value_domain_check(
    env: ReviewEnv,
) -> None:
    """**刻意的取舍**：`rule_code` / `skip_reason` 是开集，库里不拦未知值。

    这条判据守的是"取舍没有被人顺手改掉"：若将来有人给这两列加上字面量
    CHECK，本用例会红 —— 那时必须先读 `models.py` 的取舍说明
    （每加一条规则就要一次迁移；且化石与注册表的分叉不会报错），
    而不是把这条用例删掉。

    代偿在服务层：`service.validate_rule_result` 对未知值抛 422
    （见 `test_validate_rule_result_rejects_unknown_rule_code`）。
    """
    seed = _seed_content(env)
    run_id = _insert_run_raw(env, seed)
    # 未注册的 code + 未登记的 skip_reason：数据库**放行**。
    _insert_rule_result_raw(
        env,
        run_id,
        rule_code="A_RULE_THAT_DOES_NOT_EXIST",
        status=RULE_STATUS_SKIPPED,
        skip_reason="A_REASON_NOBODY_REGISTERED",
    )
    assert _scalar(
        env,
        "SELECT count(*) FROM self_check_rule_results WHERE run_id = :r",
        {"r": run_id},
    ) == 1


def test_rule_result_is_unique_per_run_and_code(env: ReviewEnv) -> None:
    """一轮里一条规则只能有一个结论（**23505**）。

    没有这条约束时，同一规则在同一轮里可以有两个矛盾结论，
    而聚合只会取先读到的那个。
    """
    seed = _seed_content(env)
    run_id = _insert_run_raw(env, seed)
    _insert_rule_result_raw(env, run_id)
    with pytest.raises((IntegrityError, DBAPIError)) as excinfo:
        _insert_rule_result_raw(env, run_id)
    assert _sqlstate(excinfo.value) == UNIQUE_VIOLATION_SQLSTATE


#: 锚点三选一的三条 CHECK，各给一组坏数据。
_BAD_ANCHORS = (
    # PARAGRAPH 却没有 ref
    {"anchor_kind": REVIEW_ANCHOR_KIND_PARAGRAPH, "paragraph_ref": None},
    # MEDIA 却没有图 id
    {"anchor_kind": REVIEW_ANCHOR_KIND_MEDIA, "media_item_id": None},
    # WHOLE 却带了 ref
    {"anchor_kind": REVIEW_ANCHOR_KIND_WHOLE, "paragraph_ref": "p0-aaaaaaaa"},
    # 未知锚点（闭集）
    {"anchor_kind": "SENTENCE"},
)


@pytest.mark.parametrize("overrides", _BAD_ANCHORS)
def test_finding_anchor_shape_checks_reject_bad_rows(
    env: ReviewEnv, overrides: dict[str, Any]
) -> None:
    """锚点三选一：列占用与 `anchor_kind` 对不上 ⇒ **23514**。"""
    seed = _seed_content(env)
    run_id = _insert_run_raw(env, seed)
    result_id = _insert_rule_result_raw(env, run_id)
    with pytest.raises(DBAPIError) as excinfo:
        _insert_finding_raw(env, result_id, **overrides)
    assert _sqlstate(excinfo.value) == r_ddl.CONSISTENCY_SQLSTATE


def test_finding_evidence_must_be_a_json_object(env: ReviewEnv) -> None:
    """`evidence` 必须是 JSON **对象**：数组或标量 ⇒ 23514。

    形状不固定会让"读 `evidence.xxx`"这件事在不同 finding 上含义不同，
    前端只能逐条猜。
    """
    seed = _seed_content(env)
    run_id = _insert_run_raw(env, seed)
    result_id = _insert_rule_result_raw(env, run_id)
    for bad in ("'[]'::jsonb", "'1'::jsonb", "'\"x\"'::jsonb"):
        with pytest.raises(DBAPIError) as excinfo:
            _insert_finding_raw(env, result_id, evidence=bad)
        assert _sqlstate(excinfo.value) == r_ddl.CONSISTENCY_SQLSTATE


def test_finding_message_must_not_be_blank(env: ReviewEnv) -> None:
    """空 message ⇒ 23514（一条没有人话的命中在工作台上是一个空行）。"""
    seed = _seed_content(env)
    run_id = _insert_run_raw(env, seed)
    result_id = _insert_rule_result_raw(env, run_id)
    with pytest.raises(DBAPIError) as excinfo:
        _insert_finding_raw(env, result_id, message="")
    assert _sqlstate(excinfo.value) == r_ddl.CONSISTENCY_SQLSTATE


def test_run_consistency_trigger_rejects_cross_content_version(
    env: ReviewEnv,
) -> None:
    """内容 A 的自检不能判定内容 B 的版本（延迟触发器的判据 1）。

    FK 挡不住：两列各自的 FK 都满足。症状是"这轮判的是哪一版"有两个答案，
    且 STALE 判定永远为真。
    """
    content_a = _seed_content(env)
    content_b = _seed_content(env)
    with pytest.raises(DBAPIError) as excinfo:
        _insert_run_raw(
            env,
            content_a,
            subject_version_id=content_b.version_id,
            immediate=True,
        )
    assert _sqlstate(excinfo.value) == r_ddl.CONSISTENCY_SQLSTATE


def test_run_consistency_trigger_rejects_content_from_other_project(
    env: ReviewEnv,
) -> None:
    """自检轮次记的项目必须与内容的真实归属一致（延迟触发器的判据 2）。

    否则两步骤鉴权反查到的项目与内容真实归属不一致 ⇒ 看板会把别的项目的
    内容算进来。
    """
    seed = _seed_content(env)
    other_owner = _create_user(env.settings)
    other_project = _seed_project(env.settings, owner_id=other_owner)
    with pytest.raises(DBAPIError) as excinfo:
        _insert_run_raw(env, seed, project_id=other_project, immediate=True)
    assert _sqlstate(excinfo.value) == r_ddl.CONSISTENCY_SQLSTATE


def test_run_consistency_trigger_allows_same_content(env: ReviewEnv) -> None:
    """阴性对照：归属一致时放行（证明上面两条不是"什么都拒"）。"""
    seed = _seed_content(env)
    run_id = _insert_run_raw(env, seed, immediate=True)
    assert _scalar(
        env, "SELECT count(*) FROM self_check_runs WHERE id = :r", {"r": run_id}
    ) == 1


def test_finding_consistency_trigger_rejects_cross_content_media(
    env: ReviewEnv,
) -> None:
    """一条 finding 不能引用另一条内容的图（跨两跳 join，FK 管不住）。"""
    content_a = _seed_content(env)
    content_b = _seed_content(env)
    media_b = _seed_media_item(
        env,
        content_id=content_b.content_id,
        paragraph_ref=content_b.paragraph_refs[0],
    )
    run_id = _insert_run_raw(env, content_a, immediate=True)
    result_id = _insert_rule_result_raw(
        env,
        run_id,
        rule_code="MEDIA_ITEM_NOT_READY",
        category="MEDIA_CONSISTENCY",
        status=RULE_STATUS_FAILED,
        severity="WARNING",
        finding_count=1,
    )
    with pytest.raises(DBAPIError) as excinfo:
        _insert_finding_raw(
            env,
            result_id,
            anchor_kind=REVIEW_ANCHOR_KIND_MEDIA,
            media_item_id=media_b,
            immediate=True,
        )
    assert _sqlstate(excinfo.value) == r_ddl.CONSISTENCY_SQLSTATE


def test_finding_consistency_trigger_rejects_cross_content_feedback(
    env: ReviewEnv,
) -> None:
    """回指的 AI 反馈必须属于本轮判的那条内容。

    否则 WP29 的审核闸拦的是**另一条内容** —— 而那种错不会报错，
    只表现为"这条内容明明有阻断项却审过了"。
    """
    content_a = _seed_content(env)
    content_b = _seed_content(env)
    with session_scope(env.settings) as session:
        item = feedback_service.create_feedback(
            FeedbackRepository(session),
            project_id=env.project_id,
            content_id=content_b.content_id,
            observed_version_id=content_b.version_id,
            anchor_kind=feedback_types.ANCHOR_KIND_WHOLE,
            category=feedback_types.FEEDBACK_CATEGORY_FACT,
            severity=feedback_types.FEEDBACK_SEVERITY_BLOCKING,
            scope=feedback_types.FEEDBACK_SCOPE_CURRENT_ONLY,
            author_kind=feedback_types.ACTOR_KIND_AI,
            body="属于内容 B 的反馈",
            created_by=env.owner_id,
        )
        feedback_b = item.id

    run_id = _insert_run_raw(env, content_a, immediate=True)
    result_id = _insert_rule_result_raw(
        env,
        run_id,
        rule_code="FACT_BINDING_CLAIM_OUT_OF_SCOPE",
        category="FACT",
        status=RULE_STATUS_FAILED,
        severity="BLOCKING",
        finding_count=1,
    )
    with pytest.raises(DBAPIError) as excinfo:
        _insert_finding_raw(
            env, result_id, feedback_item_id=feedback_b, immediate=True
        )
    assert _sqlstate(excinfo.value) == r_ddl.CONSISTENCY_SQLSTATE


def test_finding_consistency_trigger_allows_same_content_media(
    env: ReviewEnv,
) -> None:
    """阴性对照：同内容的图放行。"""
    seed = _seed_content(env)
    media = _seed_media_item(
        env, content_id=seed.content_id, paragraph_ref=seed.paragraph_refs[0]
    )
    run_id = _insert_run_raw(env, seed, immediate=True)
    result_id = _insert_rule_result_raw(
        env,
        run_id,
        rule_code="MEDIA_ITEM_NOT_READY",
        category="MEDIA_CONSISTENCY",
        status=RULE_STATUS_FAILED,
        severity="WARNING",
        finding_count=1,
    )
    finding_id = _insert_finding_raw(
        env,
        result_id,
        anchor_kind=REVIEW_ANCHOR_KIND_MEDIA,
        media_item_id=media,
        immediate=True,
    )
    assert _scalar(
        env, "SELECT count(*) FROM self_check_findings WHERE id = :f", {"f": finding_id}
    ) == 1


def test_cross_package_inbound_fk_on_feedback_items_exists(env: ReviewEnv) -> None:
    """**R8 的前提条件**：本包确实给 `content_feedback_items` 加了一条入向 FK。

    它是"`conftest.drop_own_tables` 把 `content_feedback_items` 从 **删** 变成
    **留**"的唯一原因（那个算法只删"没有跨包入向 FK"的表）。
    这条断言把 R8 的前提钉在判据里：若将来有人把 FK 去掉（改成只存 uuid），
    本用例会红，提醒他同步修正说明文档 §3 的 R8 条目与 WP29 夹具的语义描述。

    ⚠️ 本用例**不**替代实测：drop/keep 语义的变化记在说明文档与交付报告里
    （本分支已在 `rednote_test_wp30` 上实测过）。
    """
    reset_engines()
    engine = get_engine(env.settings)
    inbound = cross_package_foreign_keys(engine, ("content_feedback_items",))
    assert "fk_self_check_findings_feedback_item_id_content_feedback_items" in inbound


# ===========================================================================
# 四、服务层
# ===========================================================================


def test_run_self_check_records_every_rule_once(env: ReviewEnv) -> None:
    """一轮自检给**每条**注册规则留一行结论，8 类聚合一个都不少。

    这是"未判不冒充通过"的结构前提：若某一类一行都没有，前端就只能从
    "没有行"猜，而猜成"通过"的概率远高于猜成"未判"。
    """
    seed = _seed_content(env, paragraphs=3)
    detail = _run(env, seed.content_id)

    by_code = _result_by_code(detail)
    assert set(by_code) == {spec.code for spec in rules.RULES}
    assert {item.category for item in detail.categories} == REVIEW_CATEGORIES
    assert detail.summary.rule_set_version == RULE_SET_VERSION
    assert detail.summary.trigger_kind == TRIGGER_KIND_MANUAL
    assert detail.summary.subject_version_id == seed.version_id
    assert detail.summary.subject_state == SUBJECT_STATE_CURRENT
    # 逐规则结论与注册表的 kind / category 对齐（落库快照不是另算一遍）。
    for spec in rules.RULES:
        assert by_code[spec.code]["category"] == spec.category
        assert by_code[spec.code]["ruleKind"] == spec.kind


def test_run_self_check_reports_skipped_without_folding_into_passed(
    env: ReviewEnv,
) -> None:
    """**R2 的正面判据**：6 条没判时 `outcome=PASSED` 且 `skippedCount=6`。

    断**具体值**而不是 `>= 0`：若把 skipped 折算成 pass，
    「5 类判了 + 6 条没判」就会被渲染成「全部通过」，
    而原型的批量通过按钮正是以"已完成自检、无阻断问题"为前提
    ⇒ 假绿直接放行一批内容。

    本用例的内容刻意"什么都对"：三段正文 + 有蓝图 + 无图位 + 无禁用词，
    于是 5 条已实现规则全 PASSED；未判的 6 条 = 2 条事实（无生效基线）
    + 1 条技术格式（平台限制未配置）+ 3 条恒 SKIPPED。
    """
    blueprint_owner = _seed_content_with_blueprint(env, paragraphs=3)
    seed = blueprint_owner

    detail = _run(env, seed.content_id)
    by_code = _result_by_code(detail)

    skipped = {
        code: item["skipReason"]
        for code, item in by_code.items()
        if item["status"] == RULE_STATUS_SKIPPED
    }
    assert skipped == {
        "FACT_BINDING_CLAIM_OUT_OF_SCOPE": SKIP_REASON_NO_EFFECTIVE_BASELINE,
        "FACT_UNBOUND_INCLUDED_CLAIM": SKIP_REASON_NO_EFFECTIVE_BASELINE,
        "TECH_RENDITION_OUT_OF_PLATFORM_LIMITS": (
            SKIP_REASON_PLATFORM_MEDIA_LIMITS_NOT_CONFIGURED
        ),
        **_ALWAYS_SKIPPED,
    }
    assert detail.summary.skipped_count == 6
    assert detail.summary.evaluated_count == 5
    assert detail.summary.errored_count == 0
    assert detail.summary.blocking_count == 0
    assert detail.summary.warning_count == 0
    assert detail.summary.info_count == 0
    assert detail.summary.outcome == RUN_OUTCOME_PASSED
    # 三类恒 SKIPPED 的卡片**存在**且 ruleCount=1、passed/failed 都是 0。
    cards = {item.category: item for item in detail.categories}
    for category in ("TONE", "DUPLICATE", "SCENE"):
        assert cards[category].rule_count == 1
        assert cards[category].skipped_count == 1
        assert cards[category].passed_count == 0
        assert cards[category].failed_count == 0


def test_run_self_check_warned_outcome_from_structure_rules(
    env: ReviewEnv,
) -> None:
    """一段正文 + 无蓝图 ⇒ 两条结构规则命中，`outcome=WARNED`。

    `INFO` 不参与 outcome（登记事实，不干预审核），因此 `info_count=1`
    而 outcome 由 `warning_count=1` 决定。
    """
    seed = _seed_content(env, paragraphs=1)
    detail = _run(env, seed.content_id)
    by_code = _result_by_code(detail)

    assert by_code["STRUCTURE_PARAGRAPH_COUNT_BELOW_IMAGE_PLAN"]["status"] == (
        RULE_STATUS_FAILED
    )
    assert by_code["STRUCTURE_PARAGRAPH_COUNT_BELOW_IMAGE_PLAN"]["severity"] == (
        "WARNING"
    )
    assert by_code["STRUCTURE_VERSION_WITHOUT_BLUEPRINT"]["severity"] == "INFO"
    assert detail.summary.warning_count == 1
    assert detail.summary.info_count == 1
    assert detail.summary.blocking_count == 0
    assert detail.summary.outcome == RUN_OUTCOME_WARNED


def test_run_self_check_blocks_wp29_review_and_unblocks_after_resolution(
    env: ReviewEnv,
) -> None:
    """**与 WP29 的联动**：自检出阻断 ⇒ `decide_review(APPROVED)` 必 409；
    处置掉后必落库成功。

    这条用例证明本包**不需要**第二道审核闸：阻断项经 `feedback_bridge`
    落成 `author_kind='AI'` 的 BLOCKING 反馈，WP29 既有的
    `decide_review` 前置查询当场拒绝"通过"。
    """
    _seed_baseline(env)
    seed = _seed_content(
        env,
        paragraphs=3,
        bindings=[
            {
                BINDING_KEY_PARAGRAPH_INDEX: 0,
                BINDING_KEY_CLAIM_ID: str(uuid.uuid4()),
                "reason": "引用了一条已不在基线范围内的事实",
            }
        ],
    )

    detail = _run(env, seed.content_id)
    by_code = _result_by_code(detail)
    assert by_code["FACT_BINDING_CLAIM_OUT_OF_SCOPE"]["status"] == RULE_STATUS_FAILED
    assert by_code["FACT_BINDING_CLAIM_OUT_OF_SCOPE"]["severity"] == (
        REVIEW_SEVERITY_BLOCKING
    )
    assert detail.summary.blocking_count == 1
    assert detail.summary.outcome == RUN_OUTCOME_BLOCKED

    # 阻断项落成了 AI 反馈，且 finding 回指它。
    blocking_findings = [
        item
        for item in detail.findings
        if item["ruleCode"] == "FACT_BINDING_CLAIM_OUT_OF_SCOPE"
    ]
    assert len(blocking_findings) == 1
    feedback_id = uuid.UUID(blocking_findings[0]["feedbackItemId"])
    assert _rows(
        env,
        "SELECT author_kind, severity, state, category, anchor_kind"
        " FROM content_feedback_items WHERE id = :f",
        {"f": feedback_id},
    ) == [(AI_AUTHOR_KIND, "BLOCKING", "OPEN", "FACT", REVIEW_ANCHOR_KIND_PARAGRAPH)]

    # WP29 的闸：必须 409，且不落任何审核行。
    with pytest.raises(ApiError) as excinfo:
        with session_scope(env.settings) as session:
            feedback_service.decide_review(
                FeedbackRepository(session),
                project_id=env.project_id,
                content_item_id=seed.content_id,
                subject_version_id=seed.version_id,
                decision=feedback_types.REVIEW_DECISION_APPROVED,
                reason="看起来没问题",
                decided_by=env.owner_id,
            )
    assert excinfo.value.code == ErrorCode.INVALID_TRANSITION
    assert _STATUS_BY_CODE[excinfo.value.code] == 409
    assert (
        excinfo.value.details["reason"]
        == feedback_service.REASON_UNRESOLVED_BLOCKING_FEEDBACK
    )
    assert excinfo.value.details["blockingFeedbackIds"] == [str(feedback_id)]
    assert _scalar(
        env,
        "SELECT count(*) FROM content_reviews WHERE content_item_id = :c",
        {"c": seed.content_id},
    ) == 0

    # 人工处置掉之后，审核可以通过。
    lock_version = int(
        _scalar(
            env,
            "SELECT lock_version FROM content_feedback_items WHERE id = :f",
            {"f": feedback_id},
        )
    )
    with session_scope(env.settings) as session:
        feedback_service.advance_feedback(
            FeedbackRepository(session),
            feedback_id=feedback_id,
            expected_lock_version=lock_version,
            to_state=feedback_types.FEEDBACK_STATE_REJECTED,
            actor_kind=feedback_types.ACTOR_KIND_USER,
            reject_reason="复核后确认事实无误",
        )
    with session_scope(env.settings) as session:
        review = feedback_service.decide_review(
            FeedbackRepository(session),
            project_id=env.project_id,
            content_item_id=seed.content_id,
            subject_version_id=seed.version_id,
            decision=feedback_types.REVIEW_DECISION_APPROVED,
            reason="阻断项已处置",
            decided_by=env.owner_id,
        )
        review_id = review.id
    assert _rows(
        env, "SELECT decision FROM content_reviews WHERE id = :r", {"r": review_id}
    ) == [("APPROVED",)]


def _seed_blocking_content(env: ReviewEnv) -> SeededContent:
    """造一条"会出阻断项"的内容（生效基线 + 一条超范围的 claim 绑定）。"""
    _seed_baseline(env)
    return _seed_content(
        env,
        paragraphs=3,
        bindings=[
            {
                BINDING_KEY_PARAGRAPH_INDEX: 0,
                BINDING_KEY_CLAIM_ID: str(uuid.uuid4()),
                "reason": "超出基线范围",
            }
        ],
    )


def test_rerun_reuses_the_existing_unresolved_ai_feedback(env: ReviewEnv) -> None:
    """**R4 去重第 1 条**：重跑两轮 ⇒ 反馈恰 1 条、finding 2 条且指向同一条。

    去重键写窄了（例如把 message 算进去）会让重跑 10 次堆 10 条 OPEN 反馈，
    运营的待处理列表被机器噪声淹没；而 WP29 的 `anchorState` 明令
    "条数不因判定而减少"，没法靠过滤救。
    """
    seed = _seed_blocking_content(env)
    first = _run(env, seed.content_id)
    second = _run(env, seed.content_id)

    assert first.summary.run_id != second.summary.run_id
    assert _scalar(
        env,
        "SELECT count(*) FROM content_feedback_items WHERE content_item_id = :c",
        {"c": seed.content_id},
    ) == 1

    ids = _rows(
        env,
        "SELECT DISTINCT f.feedback_item_id FROM self_check_findings f"
        " JOIN self_check_rule_results rr ON rr.id = f.rule_result_id"
        " JOIN self_check_runs r ON r.id = rr.run_id"
        " WHERE r.content_item_id = :c AND f.feedback_item_id IS NOT NULL",
        {"c": seed.content_id},
    )
    assert len(ids) == 1, "两轮的阻断 finding 必须指向同一条反馈"
    assert _scalar(
        env,
        "SELECT count(*) FROM self_check_findings f"
        " JOIN self_check_rule_results rr ON rr.id = f.rule_result_id"
        " JOIN self_check_runs r ON r.id = rr.run_id"
        " WHERE r.content_item_id = :c AND f.feedback_item_id IS NOT NULL",
        {"c": seed.content_id},
    ) == 2


def test_rerun_after_resolution_creates_a_second_feedback(env: ReviewEnv) -> None:
    """**R4 去重第 2 条**：人工 RESOLVED 之后重跑仍命中 ⇒ 新建第 2 条。

    阻断是"**当前事实**"，不是永久禁令。若去重键不筛状态，处置完再跑就
    永远不会重新建反馈 ⇒ 问题还在、闸却放行（静默）。
    """
    seed = _seed_blocking_content(env)
    _run(env, seed.content_id)
    feedback_id = _scalar(
        env,
        "SELECT id FROM content_feedback_items WHERE content_item_id = :c",
        {"c": seed.content_id},
    )
    lock_version = int(
        _scalar(
            env,
            "SELECT lock_version FROM content_feedback_items WHERE id = :f",
            {"f": feedback_id},
        )
    )
    with session_scope(env.settings) as session:
        feedback_service.advance_feedback(
            FeedbackRepository(session),
            feedback_id=feedback_id,
            expected_lock_version=lock_version,
            to_state=feedback_types.FEEDBACK_STATE_RESOLVED,
            actor_kind=feedback_types.ACTOR_KIND_USER,
        )

    _run(env, seed.content_id)
    assert _scalar(
        env,
        "SELECT count(*) FROM content_feedback_items WHERE content_item_id = :c",
        {"c": seed.content_id},
    ) == 2


def test_different_rule_code_on_the_same_anchor_creates_its_own_feedback(
    env: ReviewEnv,
) -> None:
    """**R4 去重第 3 条**：去重键含 `rule_code` ⇒ 换一条规则命中必须新建。

    只按锚点去重会让"同一段上的两个不同问题"合成一条反馈 ⇒ 运营处置掉
    其中一个之后，另一个连带消失（闸放行）。

    造法：把 `OTHER_BANNED_TERM_HIT` 的默认严重度临时提成 BLOCKING，
    让同一条内容在**两条不同规则**上各出一个阻断项。
    """
    _seed_baseline(env)
    body = "最好的选择。\n\n第二段正文。\n\n第三段正文。"
    seed = _seed_content(
        env,
        body=body,
        bindings=[
            {
                BINDING_KEY_PARAGRAPH_INDEX: 0,
                BINDING_KEY_CLAIM_ID: str(uuid.uuid4()),
                "reason": "超出基线范围",
            }
        ],
    )

    patched = tuple(
        replace(
            spec,
            default_severity=REVIEW_SEVERITY_BLOCKING,
            evaluator=_blocking_banned_term,
        )
        if spec.code == "OTHER_BANNED_TERM_HIT"
        else spec
        for spec in rules.RULES
    )
    original = rules.RULES
    try:
        rules.RULES = patched  # type: ignore[misc]
        detail = _run(env, seed.content_id)
    finally:
        rules.RULES = original  # type: ignore[misc]

    by_code = _result_by_code(detail)
    assert by_code["FACT_BINDING_CLAIM_OUT_OF_SCOPE"]["severity"] == "BLOCKING"
    assert by_code["OTHER_BANNED_TERM_HIT"]["severity"] == "BLOCKING"
    # 两条规则、同一段锚点 ⇒ 两条反馈（不是一条）。
    assert _scalar(
        env,
        "SELECT count(*) FROM content_feedback_items WHERE content_item_id = :c",
        {"c": seed.content_id},
    ) == 2


def _blocking_banned_term(subject: CheckSubject) -> Any:
    """测试用：把禁用词规则的严重度提成 BLOCKING（去重第 3 条的造数手段）。

    ⚠️ 这是**测试侧**的包装，不是本包的实现：生产注册表里
    `OTHER_BANNED_TERM_HIT` 的默认严重度是 `WARNING`。
    这么造是因为"同一锚点上的两条不同规则"只有在**两条都阻断**时才会
    各建一条反馈，而去重键是否包含 `rule_code` 只能这样验。
    """
    outcome = evaluators.other_banned_term_hit(subject)
    if outcome.status != RULE_STATUS_FAILED:
        return outcome
    return review_failed(REVIEW_SEVERITY_BLOCKING, outcome.findings)


def test_run_is_marked_stale_after_an_edit_without_losing_rows(
    env: ReviewEnv,
) -> None:
    """**R5**：自检后追加一版 ⇒ 该轮必被标 `STALE`，且**条数不减**。

    把 STALE 的轮次藏起来会让看板上的内容突然变回"未自检"，运营无从判断
    是"没跑过"还是"跑过但过期"。
    """
    seed = _seed_content(env, paragraphs=3, bindings=_null_bindings(3))
    detail = _run(env, seed.content_id)
    assert detail.summary.subject_state == SUBJECT_STATE_CURRENT

    with session_scope(env.settings) as session:
        feedback_service.edit_content(
            ContentRepository(session),
            version_repo=VersionRepository(session),
            baseline_repo=BaselineRepository(session),
            content_id=seed.content_id,
            expected_lock_version=0,
            body="改过的第一段。\n\n改过的第二段。\n\n改过的第三段。",
            bindings=_null_bindings(3),
        )

    with session_scope(env.settings) as session:
        page = r_service.list_runs(
            ReviewRepository(session), content_id=seed.content_id
        )
    # ① 条数不变（读时判定不删行、不过滤）
    assert len(page.items) == 1
    assert page.items[0]["runId"] == str(detail.summary.run_id)
    # ② 状态变了
    assert page.items[0]["subjectState"] == SUBJECT_STATE_STALE
    # ③ 库里也没被改（STALE 不落库、不写放大）
    assert _scalar(
        env,
        "SELECT count(*) FROM self_check_runs WHERE content_item_id = :c",
        {"c": seed.content_id},
    ) == 1


def test_run_self_check_does_not_write_runtime_settings(env: ReviewEnv) -> None:
    """**R9**：跑一轮自检后 `runtime_settings` 的行数**不变**（断具体行数）。

    `get_effective_limits` 的 `materialize` 默认 `True`，而惰性物化是
    **读路径上的写副作用**。本包在 `planning_bridge` 里把它写死成 `False` ——
    否则一个 `VIEW` 权限的读请求会往配置表插一行继承副本，批量自检会放大成
    N 次写。
    """
    seed = _seed_content(env, paragraphs=3)
    before = int(_scalar(env, "SELECT count(*) FROM runtime_settings"))
    _run(env, seed.content_id)
    after = int(_scalar(env, "SELECT count(*) FROM runtime_settings"))
    assert after == before, (
        "自检往 runtime_settings 写了行 —— `materialize=False` 被绕过了（R9）"
    )
    # 阴性对照：这个键在本库上确实"未配置"（否则上面的 0 变化可能是因为
    # 它本来就有值，而那样这条判据就不是在守 materialize 了）。
    assert _scalar(
        env,
        "SELECT count(*) FROM runtime_settings WHERE key = :k",
        {"k": SETTING_KEY_PLATFORM_MEDIA_LIMITS},
    ) == 0


def test_one_broken_rule_does_not_abort_the_whole_run(env: ReviewEnv) -> None:
    """单条规则抛异常 ⇒ 该条 `ERRORED` + `error_note`，**其余规则照常有结论**。

    一条规则崩掉不该让其余 7 类全部无结论（那会把"有一处实现有 bug"放大成
    "这篇稿子完全没被自检过"）。且 `ERRORED` 在 outcome 的定序里**压过**
    `WARNED`/`PASSED` —— fail-closed：规则没跑成绝不能被读成没问题。
    """

    def _boom(subject: CheckSubject) -> Any:
        raise RuntimeError("故意炸一条规则")

    patched = tuple(
        replace(spec, evaluator=_boom)
        if spec.code == "STRUCTURE_VERSION_WITHOUT_BLUEPRINT"
        else spec
        for spec in rules.RULES
    )
    seed = _seed_content(env, paragraphs=3)
    original = rules.RULES
    try:
        rules.RULES = patched  # type: ignore[misc]
        detail = _run(env, seed.content_id)
    finally:
        rules.RULES = original  # type: ignore[misc]

    by_code = _result_by_code(detail)
    assert len(by_code) == len(rules.RULES)
    errored = by_code["STRUCTURE_VERSION_WITHOUT_BLUEPRINT"]
    assert errored["status"] == RULE_STATUS_ERRORED
    assert "故意炸一条规则" in errored["errorNote"]
    assert errored["severity"] is None
    assert errored["findingCount"] == 0
    # 其余规则仍有结论。
    assert by_code["OTHER_BANNED_TERM_HIT"]["status"] == RULE_STATUS_PASSED
    assert detail.summary.errored_count == 1
    assert detail.summary.outcome == RUN_OUTCOME_ERRORED


def test_batch_partial_failure_does_not_abort_others(env: ReviewEnv) -> None:
    """批量自检：中间一条失败，前后两条仍然成功并落库。

    失败造法：一条**没有任何版本**的内容 —— `read_edit_target` 抛
    `CONTENT_VERSION_NOT_FOUND`（换任何参数都不行，要先跑一次生成）。
    """
    first = _seed_content(env, paragraphs=3)
    third = _seed_content(env, paragraphs=3)
    versionless = uuid.uuid4()
    _execute(
        env,
        "INSERT INTO content_items (id, project_id, state, current_version_no,"
        " lock_version) VALUES (:c, :p, 'DRAFT', 0, 0)",
        {"c": versionless, "p": env.project_id},
    )

    order = [first.content_id, versionless, third.content_id]
    with session_scope(env.settings) as session:
        results = r_service.run_self_checks_batch(
            ReviewRepository(session),
            ContentRepository(session),
            PlanningRepository(session),
            FeedbackRepository(session),
            content_item_ids=order,
            created_by=env.owner_id,
        )

    assert [item["ok"] for item in results] == [True, False, True]
    assert [item["contentItemId"] for item in results] == [str(c) for c in order]
    assert results[0]["run"]["triggerKind"] == TRIGGER_KIND_BATCH
    assert {
        row[0]
        for row in _rows(
            env,
            "SELECT content_item_id FROM self_check_runs WHERE project_id = :p",
            {"p": env.project_id},
        )
    } == {first.content_id, third.content_id}


def test_batch_rejects_empty_and_oversized_requests(env: ReviewEnv) -> None:
    """空批量与超量批量都给可读 422（`details.reason` 机器可判）。

    为什么不允许"不传就是全项目"：一个打错的请求会变成全项目扫描 +
    每条内容一轮自检 + N 条 AI 反馈，而那是不可撤销的写入（三张表不可变）。
    """
    with session_scope(env.settings) as session:
        repos = (
            ReviewRepository(session),
            ContentRepository(session),
            PlanningRepository(session),
            FeedbackRepository(session),
        )
        with pytest.raises(ApiError) as empty:
            r_service.run_self_checks_batch(*repos, content_item_ids=[])
        assert empty.value.details["reason"] == "CONTENT_IDS_EMPTY"

        with pytest.raises(ApiError) as too_many:
            r_service.run_self_checks_batch(
                *repos, content_item_ids=[uuid.uuid4() for _ in range(51)]
            )
        assert too_many.value.details["reason"] == "TOO_MANY_CONTENT_IDS"
        assert too_many.value.details["maxContentIds"] == 50


def test_list_latest_runs_returns_one_row_per_content(env: ReviewEnv) -> None:
    """看板列：每条内容**最新一轮**，未自检的内容**不出现**。

    用 `DISTINCT ON` 而不是"全取回来再分组"：后者的窗口被某条内容的历史
    轮次占满时，别的内容会一轮都看不到（看板显示"未自检"）。
    """
    first = _seed_content(env, paragraphs=3)
    second = _seed_content(env, paragraphs=3)
    never = _seed_content(env, paragraphs=3)
    _run(env, first.content_id)
    latest_first = _run(env, first.content_id)
    _run(env, second.content_id)

    with session_scope(env.settings) as session:
        runs = r_service.list_latest_runs(
            ReviewRepository(session), project_id=env.project_id
        )
    by_content = {item["contentItemId"]: item for item in runs}
    assert set(by_content) == {str(first.content_id), str(second.content_id)}
    assert str(never.content_id) not in by_content
    assert by_content[str(first.content_id)]["runId"] == str(
        latest_first.summary.run_id
    )

    # 过滤参数：只要第二条。
    with session_scope(env.settings) as session:
        filtered = r_service.list_latest_runs(
            ReviewRepository(session),
            project_id=env.project_id,
            content_item_ids=[second.content_id],
        )
    assert [item["contentItemId"] for item in filtered] == [str(second.content_id)]


def test_list_runs_is_newest_first(env: ReviewEnv) -> None:
    """轮次历史倒序（第一条即最新）。"""
    seed = _seed_content(env, paragraphs=3)
    older = _run(env, seed.content_id)
    newer = _run(env, seed.content_id)
    with session_scope(env.settings) as session:
        page = r_service.list_runs(
            ReviewRepository(session), content_id=seed.content_id
        )
    assert [item["runId"] for item in page.items] == [
        str(newer.summary.run_id),
        str(older.summary.run_id),
    ]
    assert page.page.has_more is False


def test_get_run_detail_not_found_gives_404_with_reason(env: ReviewEnv) -> None:
    """轮次不存在 ⇒ 404 且 `details.reason` 是受控常量。"""
    missing = uuid.uuid4()
    with pytest.raises(ApiError) as excinfo:
        with session_scope(env.settings) as session:
            r_service.get_run_detail(ReviewRepository(session), run_id=missing)
    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND
    assert _STATUS_BY_CODE[excinfo.value.code] == 404
    assert excinfo.value.details["reason"] == "SELF_CHECK_RUN_NOT_FOUND"


def test_validate_rule_result_rejects_unknown_rule_code() -> None:
    """开集字段的**唯一守卫**：未注册 code / 未登记 skip_reason ⇒ 422。

    `rule_code` 与 `skip_reason` 刻意不落数据库 CHECK，因此这一道就是它们
    取值域的全部防线（见 `models.py` 的取舍说明）。
    """
    with pytest.raises(ApiError) as unknown_code:
        r_service.validate_rule_result(
            rule_code="A_RULE_THAT_DOES_NOT_EXIST",
            status=RULE_STATUS_PASSED,
            severity=None,
            skip_reason=None,
        )
    assert unknown_code.value.code == ErrorCode.VALIDATION_FAILED
    assert _STATUS_BY_CODE[unknown_code.value.code] == 422
    assert unknown_code.value.details["reason"] == "UNKNOWN_RULE_CODE"

    with pytest.raises(ApiError) as bad_reason:
        r_service.validate_rule_result(
            rule_code=rules.RULES[0].code,
            status=RULE_STATUS_SKIPPED,
            severity=None,
            skip_reason="A_REASON_NOBODY_REGISTERED",
        )
    assert bad_reason.value.details["reason"] == "SKIP_REASON_INVALID"

    with pytest.raises(ApiError) as missing_severity:
        r_service.validate_rule_result(
            rule_code=rules.RULES[0].code,
            status=RULE_STATUS_FAILED,
            severity=None,
            skip_reason=None,
        )
    assert missing_severity.value.details["reason"] == "REVIEW_SEVERITY_INVALID"


def test_run_self_check_rejects_unknown_trigger_kind(env: ReviewEnv) -> None:
    """`trigger_kind` 是闭集 ⇒ 未知值 422（且**不含 `AUTO`**）。"""
    seed = _seed_content(env, paragraphs=3)
    with pytest.raises(ApiError) as excinfo:
        _run(env, seed.content_id, trigger_kind="AUTO")
    assert excinfo.value.details["reason"] == "TRIGGER_KIND_INVALID"
    assert _scalar(
        env,
        "SELECT count(*) FROM self_check_runs WHERE content_item_id = :c",
        {"c": seed.content_id},
    ) == 0


def test_media_rules_read_the_real_media_items(env: ReviewEnv) -> None:
    """装配层的活性：真造一个 PENDING 图位 ⇒ 媒体类两条规则都从库里读到它。

    这条用例守的是 `repository.list_media_snapshots` 的 `LEFT JOIN`：
    若写成内连接，**没有版本的图位会静默消失** ⇒
    `MEDIA_ITEM_NOT_READY` 在最该命中的场景下恒绿。
    """
    seed = _seed_content(env, paragraphs=3)
    media = _seed_media_item(
        env, content_id=seed.content_id, paragraph_ref="p9-deadbeef"
    )
    detail = _run(env, seed.content_id)
    by_code = _result_by_code(detail)

    assert by_code["MEDIA_ITEM_NOT_READY"]["status"] == RULE_STATUS_FAILED
    assert by_code["MEDIA_PARAGRAPH_REF_STALE"]["status"] == RULE_STATUS_FAILED
    media_findings = [
        item for item in detail.findings if item["anchorKind"] == "MEDIA"
    ]
    assert {item["mediaItemId"] for item in media_findings} == {str(media)}


# ===========================================================================
# 五、静态接缝（R3 / R11 / evaluators 纯度）
# ===========================================================================


def _review_source(name: str) -> str:
    root = Path(review_pkg.__file__).resolve().parent
    return (root / name).read_text(encoding="utf-8")


def _called_names(source: str) -> set[str]:
    """源码里出现过的**函数调用名**集合（AST，不看注释/文档字符串）。

    用 AST 而不是纯字符串搜索：文档字符串里提到 `body.split(` 是说明，
    不是调用。判据只认调用点 —— 否则本文件自己的 docstring 会让判据恒绿
    （WP29 R1 踩过的同款理由）。
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _imported_modules(source: str) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


_REVIEW_SOURCE_FILES = (
    "types.py",
    "rules.py",
    "evaluators.py",
    "models.py",
    "ddl.py",
    "repository.py",
    "content_bridge.py",
    "media_bridge.py",
    "feedback_bridge.py",
    "planning_bridge.py",
    "service.py",
    "dependencies.py",
    "schemas.py",
    "api.py",
)


def test_review_package_has_no_duplicate_paragraph_split() -> None:
    """**R3**：本包零切段实现，段落坐标只经 `media_bridge` 转发。

    两套切段会让 `MEDIA_PARAGRAPH_REF_STALE` 比较**两个不同口径**的集合
    ⇒ 不是偶尔误报，而是**系统性恒红**：每张图都判成"坐标失配"，
    真正的失配被噪声埋掉。

    收口还要跑 `tools/quality/check_paragraph_split_parity.py`，
    确认 `_IMPL_FILES` **仍是 3 个**（本包不该让它变成 4 个）。
    """
    for name in _REVIEW_SOURCE_FILES:
        called = _called_names(_review_source(name))
        assert "split" not in called, f"{name} 自己按模式切了正文"
        assert "splitlines" not in called, f"{name} 自己切了行"
        assert "paragraph_ref_for" not in called, f"{name} 自己拼了段落 ref"
        assert "parse_paragraph_ref" not in called, f"{name} 自己解析了段落 ref"

    # 正向对照：接缝文件**确实**转发给 media 的纯函数（证明读的是对的文件）。
    bridge = _called_names(_review_source("media_bridge.py"))
    assert "paragraph_refs_for_body" in bridge
    assert "validate_paragraph_ref" in bridge
    assert "split_body_into_paragraphs" in _called_names(_review_source("service.py"))


def test_review_package_does_not_touch_the_model_gateway() -> None:
    """**R11**：本包不调任何模型 / 网关（ADR-10 默认 `LOCAL_ONLY` 无回退）。

    一旦某条软规则"顺手"调模型：① 绕过网关 = ADR-10 违规；② 不预占 =
    接缝 §5.7 的硬条款；③ 默认部署下当场被拒 ⇒ 规则从"遇到缺口就停"
    退化成"永远停"，而那与 `SKIPPED` 在响应里**长得一样**。

    判据用 AST 查 import 与调用名，不查文本（docstring 里提到"网关"
    是说明，不是调用）。
    """
    forbidden_calls = {"dispatch_call", "invoke_model", "call_provider"}
    for name in _REVIEW_SOURCE_FILES:
        source = _review_source(name)
        modules = _imported_modules(source)
        leaked = {m for m in modules if "gateway" in m or "prompts" in m}
        assert not leaked, f"{name} import 了网关/提示词模块：{sorted(leaked)}"
        assert not (forbidden_calls & _called_names(source)), (
            f"{name} 调用了模型分发原语"
        )


def test_evaluators_are_pure_functions() -> None:
    """`evaluators.py` 必须是纯函数：零 IO、零 session、零 `now()`。

    这是本包可测性的**全部**来源（见 `evaluators.py` 的模块 docstring）。
    判据三条：
      ① 不 import 任何带 session / 仓储语义的模块；
      ② 不出现 `execute` / `flush` / `commit` / `now` / `today` 等调用；
      ③ 每个 `def` 的第一个参数名都是 `subject`（签名统一是注册表能机械
         遍历的前提）。
    """
    source = _review_source("evaluators.py")
    modules = _imported_modules(source)
    own = {module for module in modules if module.startswith("rednote.")}
    assert own == {"rednote.modules.review.types"}, (
        f"evaluators 只许 import 本包 types（标准库除外）：实际 {sorted(own)}"
    )
    forbidden = {
        "execute",
        "flush",
        "commit",
        "now",
        "utcnow",
        "today",
        "query",
        "scalar_one",
        "text",
    }
    leaked = forbidden & _called_names(source)
    assert not leaked, f"evaluators 里出现了非纯调用：{sorted(leaked)}"

    tree = ast.parse(source)
    public = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    ]
    assert len(public) == len(rules.RULES), (
        "evaluators 的公开函数数必须与注册表条数相等（没有写了不注册的规则，"
        "也没有注册了不存在的规则）"
    )
    for node in public:
        assert node.args.args, f"{node.name} 没有参数"
        assert node.args.args[0].arg == "subject", (
            f"{node.name} 的第一个参数不是 `subject`（签名必须统一）"
        )


def test_every_registered_evaluator_lives_in_evaluators_module() -> None:
    """注册表里的每个 `evaluator` 都必须来自 `evaluators.py`。

    否则"规则逻辑是纯函数"这条纪律就有了旁路：有人可以在 `service.py` 里
    写一个会连库的闭包塞进注册表，而上一条 AST 判据查不到它。
    """
    for spec in rules.RULES:
        assert spec.evaluator.__module__ == evaluators.__name__, (
            f"{spec.code} 的 evaluator 不在 evaluators 模块里："
            f"{spec.evaluator.__module__}"
        )
