"""WP29 `feedback` 包的验收判据 —— 数据库行为层 / 服务层 / 静态接缝。

## 夹具纪律（照抄 `test_exports_wp35.py`）

· 一律跑**私有库**（`REDNOTE_TEST_DATABASE_URL`）；缺省也指向 WP29 私有库，
  不碰共享 `rednote_test`；
· 只重建本包 3 张表 + 3 个触发器，其余表（`projects` / `content_items` /
  `content_versions` / `media_items` / `users` / `version_nodes` …）按迁移已就绪；
· 每个用例新造 user / project / content，**按项目隔离**，不数全库行；
· 造数复用 `test_content_wp26` 的 `_seed_project` / `_seed_valid_content` /
  `_create_user`（与 `test_content_wp29_edit_seam.py` 导入 `test_content_wp26`
  的写法同款）—— 重写一份"怎么造内容+版本"会在将来分叉；
· 数据库探针断言 **sqlstate**，不只断言"抛了异常"。

## 本文件守什么

| 组 | 判据 |
|---|---|
| 数据库行为层 | 锚点一致性触发器（跨内容媒体/观察版本）、不可变触发器、REJECTED 理由 CHECK |
| R1（静态） | `feedback` 包不直接调用版本写入原语（`insert_version` / `advance_projection` / `register_version`） |
| R2（静态） | `feedback` 包零切段实现，切段只经 `media_bridge.split_body_into_paragraphs` |
| R3 | 428 由 `platform.concurrency.require_if_match` 负责；服务层自己的 412 |
| R4 | 412 的 `currentEtag` 能直接用于**原样重提**（往返，不是只断字段存在） |
| R5 | STALE 只打标记、**条数不变** |
| R6 | 首行事件与状态列**同一事务**（回滚后两者都不存在） |
| R7 | 迁移文件的取值域字面量判据 —— WP29 迁移由主 agent 后续建立，本测试先 skip |
| 7 个用例 | 每个至少一条正常路径；另含批量审核"单条失败不拖垮整批" |

## 为什么不自己写一份 content 建表夹具

`content_*` 表的 DDL 归 content 包；本文件按既有先例导入它的两个 autouse
夹具（`_ensure_own_tables` / `_clean_own_rows`）。本包的 module 级夹具**显式
依赖** content 夹具，保证"被 FK 引用的表先就位"（否则 `create_all` 建
`content_feedback_items` 时 PostgreSQL 找不到 `content_items`）。
"""

from __future__ import annotations

import ast
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from conftest import (
    cross_package_foreign_keys,
    drop_own_tables,
    truncate_targets,
)

# FK 目标表必须在 `create_all` 之前 import（否则 NoReferencedTableError）。
# 本包三张表的 FK 指向：projects / content_items / content_versions /
# media_items / users；而 content_versions 自己又引用 version_nodes。
import rednote.modules.baseline.models  # noqa: F401
import rednote.modules.content.models  # noqa: F401
import rednote.modules.feedback.models  # noqa: F401
import rednote.modules.identity.models  # noqa: F401
import rednote.modules.media.models  # noqa: F401
import rednote.modules.projects.models  # noqa: F401
import rednote.modules.versions.models  # noqa: F401

from rednote.modules import feedback as feedback_pkg
from rednote.modules.baseline.repository import BaselineRepository
from rednote.modules.content.repository import ContentRepository
from rednote.modules.feedback import ddl as fb_ddl
from rednote.modules.feedback import media_bridge
from rednote.modules.feedback import service as f_service
from rednote.modules.feedback.models import OWN_TABLES
from rednote.modules.feedback.repository import FeedbackRepository
from rednote.modules.feedback.types import (
    ACTOR_KIND_USER,
    ANCHOR_KIND_MEDIA,
    ANCHOR_KIND_PARAGRAPH,
    ANCHOR_KIND_WHOLE,
    FEEDBACK_CATEGORY_FACT,
    FEEDBACK_SEVERITY_BLOCKING,
    FEEDBACK_SEVERITY_WARNING,
    FEEDBACK_SCOPE_CURRENT_ONLY,
    FEEDBACK_STATE_IN_PROGRESS,
    FEEDBACK_STATE_OPEN,
    FEEDBACK_STATE_REJECTED,
    REASON_FEEDBACK_LOCK_VERSION_MISMATCH,
    REVIEW_DECISION_APPROVED,
)
from rednote.modules.versions.repository import VersionRepository
from rednote.platform.base import Base
from rednote.platform.concurrency import build_etag, parse_etag, require_if_match
from rednote.platform.configuration import Settings
from rednote.platform.database import get_engine, reset_engines, session_scope
from rednote.platform.errors import _STATUS_BY_CODE, ApiError, ErrorCode
from rednote.platform.immutability import IMMUTABLE_RECORD_SQLSTATE

# 复用 content 包的造数夹具与助手（autouse 夹具导入后在本模块同样生效）。
# 只读复用，不往 `test_content_wp26.py` 里写任何东西。
from test_content_wp26 import (
    _clean_own_rows as _clean_content_rows,  # noqa: F401
    _create_user,
    _ensure_own_tables as _ensure_content_tables,
    _seed_project,
    _seed_valid_content,
    _sqlstate,
)

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
#: WP29 私有测试库（**不是**共享的 `rednote_test`）。
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test_wp29"
TEST_DATABASE_URL_ENV = "REDNOTE_TEST_DATABASE_URL"

#: DROP/TRUNCATE 顺序：引用方在前（events / reviews → items）。
DROP_ORDER = (
    "content_reviews",
    "content_feedback_events",
    "content_feedback_items",
)
_TABLE_NAMES = tuple(OWN_TABLES)


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
    """只重建本包 3 张表 + 3 个触发器。

    依赖 content 的建表夹具是**顺序保证**：`content_feedback_items` 有 FK
    指向 `content_items` / `content_versions`，PostgreSQL 在 CREATE TABLE 时
    要求被引用表已存在。

    `create_all` 只建传入的三张表（理由见 `test_content_wp26.py`：
    全量 `metadata.sorted_tables` 会被不在本仓的 `domain_events` 卡住）。
    触发器先 DROP 再 CREATE：`CREATE TRIGGER` 不幂等，而 `CREATE OR REPLACE
    FUNCTION` 幂等。
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
            fb_ddl.all_trigger_ddl()
        ):
            conn.execute(text(drop_trigger))
            conn.execute(text(func_ddl))
            conn.execute(text(trigger_ddl))
    yield


@pytest.fixture(autouse=True)
def _clean_own_rows(_clean_content_rows: None) -> Iterator[None]:
    """每个用例前清空本包 3 张表。

    为什么只能 TRUNCATE：`content_feedback_events` / `content_reviews` 是
    不可变表，行级 BEFORE 触发器拦 UPDATE/DELETE（`23001`）；TRUNCATE
    不触发行级触发器。

    依赖 content 的清库夹具是为了让"content 表先清"有确定顺序；它的
    `truncate_targets` 已经把引用 content 的本包三张表纳入闭包。
    """
    reset_engines()
    engine = get_engine(_make_settings())
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(truncate_targets(engine, DROP_ORDER))))
    yield


# ===========================================================================
# 造数
# ===========================================================================


@dataclass(frozen=True)
class FeedbackEnv:
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
def env() -> FeedbackEnv:
    reset_engines()
    settings = _make_settings()
    owner_id = _create_user(settings)
    project_id = _seed_project(settings, owner_id=owner_id)
    return FeedbackEnv(settings=settings, project_id=project_id, owner_id=owner_id)


def _bindings_literal(paragraph_count: int) -> str:
    """生成一份**合法**的 `bindings` JSONB 字面量。

    规则一要求段号齐全（`0..n-1` 各一次）；规则二允许 `claimId=null`，
    但必须给出非空理由。用 `json.dumps` 而不是手拼 JSON：手拼的引号
    转义错一次，症状是"插入失败"，看起来像实现坏了。
    """
    payload = [
        {
            "paragraphIndex": index,
            "claimId": None,
            "reason": "本段无对应事实",
            "text": f"第 {index + 1} 段",
        }
        for index in range(paragraph_count)
    ]
    return "'" + json.dumps(payload, ensure_ascii=False).replace("'", "''") + "'::jsonb"


def _seed_content(
    env: FeedbackEnv,
    *,
    paragraphs: int = 2,
    body: str | None = None,
) -> SeededContent:
    """造"一条内容 + 一版正文"（含 `version_nodes` 配对行）。

    复用 content 包的铺底助手；段落 ref 由 `media_bridge` 从同一份正文
    派生 —— 这正是被测代码判 FRESH/STALE 时用的生成器。
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
        bindings=_bindings_literal(paragraphs),
    )
    refs = tuple(
        ref for _index, _text, ref in media_bridge.split_body_into_paragraphs(body)
    )
    return SeededContent(
        content_id=content_id,
        version_id=version_id,
        body=body,
        paragraph_refs=refs,
    )


def _seed_media_item(
    env: FeedbackEnv,
    *,
    content_id: uuid.UUID,
    paragraph_ref: str,
    sort_order: int = 0,
) -> uuid.UUID:
    """造一条 `media_items` 身份行（投影为空、状态走默认值）。"""
    media_item_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO media_items"
                " (id, project_id, content_item_id, paragraph_ref, sort_order,"
                "  created_by)"
                " VALUES (:id, :project_id, :content_id, :paragraph_ref,"
                "         :sort_order, :created_by)"
            ),
            {
                "id": media_item_id,
                "project_id": env.project_id,
                "content_id": content_id,
                "paragraph_ref": paragraph_ref,
                "sort_order": sort_order,
                "created_by": env.owner_id,
            },
        )
    return media_item_id


def _seed_title_candidate(
    env: FeedbackEnv,
    *,
    version_id: uuid.UUID,
    title: str = "候选标题",
    index: int = 0,
) -> uuid.UUID:
    """造一条 `title_candidates` 行（预览页的"标题候选"数据源）。"""
    candidate_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO title_candidates"
                " (id, content_version_id, candidate_index, title, scores, keywords)"
                " VALUES (:id, :version_id, :candidate_index, :title,"
                "         '{}'::jsonb, '[]'::jsonb)"
            ),
            {
                "id": candidate_id,
                "version_id": version_id,
                "candidate_index": index,
                "title": title,
            },
        )
    return candidate_id


def _execute(
    env: FeedbackEnv, sql: str, params: dict[str, Any] | None = None
) -> None:
    """在**独立事务**里执行一条原生 SQL（退出即提交）。"""
    with session_scope(env.settings) as session:
        session.execute(text(sql), params or {})


def _scalar(
    env: FeedbackEnv, sql: str, params: dict[str, Any] | None = None
) -> Any:
    with session_scope(env.settings) as session:
        return session.execute(text(sql), params or {}).scalar_one_or_none()


def _rows(
    env: FeedbackEnv, sql: str, params: dict[str, Any] | None = None
) -> list[tuple[Any, ...]]:
    with session_scope(env.settings) as session:
        return [tuple(row) for row in session.execute(text(sql), params or {}).fetchall()]


_FEEDBACK_INSERT = (
    "INSERT INTO content_feedback_items"
    " (id, project_id, content_item_id, observed_version_id, anchor_kind,"
    "  paragraph_ref, selected_text, media_item_id, category,"
    "  suggested_category, severity, scope, state, author_kind, body,"
    "  reject_reason, resolved_version_id, created_by)"
    " VALUES (:id, :project_id, :content_id, :version_id, :anchor_kind,"
    "         :paragraph_ref, NULL, :media_item_id, :category, NULL,"
    "         :severity, :scope, :state, :author_kind, :body,"
    "         :reject_reason, NULL, :created_by)"
)


def _insert_feedback_raw(
    env: FeedbackEnv,
    *,
    content_id: uuid.UUID,
    version_id: uuid.UUID,
    anchor_kind: str = ANCHOR_KIND_WHOLE,
    paragraph_ref: str | None = None,
    media_item_id: uuid.UUID | None = None,
    state: str = FEEDBACK_STATE_OPEN,
    reject_reason: str | None = None,
    body: str = "用原生 SQL 造的反馈",
) -> uuid.UUID:
    """**绕过服务层**插一条反馈（数据库行为层判据用）。

    绕过是有意的：服务层会在写入前给出可读 4xx，DB 判据要证明
    "即便绕过它，DDL 也拦得住"。
    """
    feedback_item_id = uuid.uuid4()
    _execute(
        env,
        _FEEDBACK_INSERT,
        {
            "id": feedback_item_id,
            "project_id": env.project_id,
            "content_id": content_id,
            "version_id": version_id,
            "anchor_kind": anchor_kind,
            "paragraph_ref": paragraph_ref,
            "media_item_id": media_item_id,
            "category": FEEDBACK_CATEGORY_FACT,
            "severity": FEEDBACK_SEVERITY_WARNING,
            "scope": FEEDBACK_SCOPE_CURRENT_ONLY,
            "state": state,
            "author_kind": ACTOR_KIND_USER,
            "body": body,
            "reject_reason": reject_reason,
            "created_by": env.owner_id,
        },
    )
    return feedback_item_id


def _create_feedback(
    env: FeedbackEnv,
    seed: SeededContent,
    *,
    anchor_kind: str = ANCHOR_KIND_PARAGRAPH,
    paragraph_ref: str | None = None,
    media_item_id: uuid.UUID | None = None,
    severity: str = FEEDBACK_SEVERITY_WARNING,
    body: str = "这条内容需要调整。",
) -> uuid.UUID:
    """经服务层建一条反馈（返回 id）。"""
    if anchor_kind == ANCHOR_KIND_PARAGRAPH and paragraph_ref is None:
        paragraph_ref = seed.paragraph_refs[0]
    with session_scope(env.settings) as session:
        item = f_service.create_feedback(
            FeedbackRepository(session),
            project_id=env.project_id,
            content_id=seed.content_id,
            observed_version_id=seed.version_id,
            anchor_kind=anchor_kind,
            category=FEEDBACK_CATEGORY_FACT,
            severity=severity,
            scope=FEEDBACK_SCOPE_CURRENT_ONLY,
            author_kind=ACTOR_KIND_USER,
            body=body,
            paragraph_ref=paragraph_ref,
            media_item_id=media_item_id,
            created_by=env.owner_id,
        )
        return item.id


def _list_feedback(env: FeedbackEnv, content_id: uuid.UUID) -> f_service.FeedbackPage:
    with session_scope(env.settings) as session:
        return f_service.list_feedback(
            FeedbackRepository(session), content_id=content_id
        )


def _request(headers: dict[str, str] | None = None) -> Request:
    """构造一个只带 headers 的 ASGI 请求（不需要起 HTTP server）。"""
    return Request(
        {
            "type": "http",
            "method": "PATCH",
            "path": "/",
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in (headers or {}).items()
            ],
        }
    )


# ===========================================================================
# 数据库行为层
# ===========================================================================


def test_anchor_consistency_trigger_rejects_cross_content_media_item(
    env: FeedbackEnv,
) -> None:
    """内容 A 的反馈不能引用内容 B 的媒体位（延迟触发器的判据 1）。"""
    content_a = _seed_content(env)
    content_b = _seed_content(env)
    media_b = _seed_media_item(
        env, content_id=content_b.content_id, paragraph_ref=content_b.paragraph_refs[0]
    )

    with pytest.raises(DBAPIError) as excinfo:
        _insert_feedback_raw(
            env,
            content_id=content_a.content_id,
            version_id=content_a.version_id,
            anchor_kind=ANCHOR_KIND_MEDIA,
            media_item_id=media_b,
        )

    assert _sqlstate(excinfo.value) == fb_ddl.CONSISTENCY_SQLSTATE
    # 被拒的结论对 ≠ 原因对：确认这行确实没落库。
    assert _scalar(
        env, "SELECT count(*) FROM content_feedback_items WHERE content_item_id = :c",
        {"c": content_a.content_id},
    ) == 0


def test_anchor_consistency_trigger_rejects_cross_content_observed_version(
    env: FeedbackEnv,
) -> None:
    """内容 A 的反馈不能观察内容 B 的版本（延迟触发器的判据 2）。"""
    content_a = _seed_content(env)
    content_b = _seed_content(env)

    with pytest.raises(DBAPIError) as excinfo:
        _insert_feedback_raw(
            env,
            content_id=content_a.content_id,
            version_id=content_b.version_id,
            anchor_kind=ANCHOR_KIND_WHOLE,
        )

    assert _sqlstate(excinfo.value) == fb_ddl.CONSISTENCY_SQLSTATE


def test_immutable_trigger_rejects_update_on_feedback_events(
    env: FeedbackEnv,
) -> None:
    """`content_feedback_events` 是只追加表：裸 UPDATE 被 23001 拒。"""
    seed = _seed_content(env)
    _create_feedback(env, seed)
    event_id = _scalar(
        env, "SELECT id FROM content_feedback_events ORDER BY seq_no LIMIT 1"
    )
    assert event_id is not None

    with pytest.raises(DBAPIError) as excinfo:
        _execute(
            env,
            "UPDATE content_feedback_events SET note = '偷偷改写历史' WHERE id = :id",
            {"id": event_id},
        )

    assert _sqlstate(excinfo.value) == IMMUTABLE_RECORD_SQLSTATE
    assert _scalar(
        env, "SELECT note FROM content_feedback_events WHERE id = :id", {"id": event_id}
    ) is None


def test_reject_reason_check_constraint(env: FeedbackEnv) -> None:
    """`state='REJECTED'` 与 `reject_reason IS NOT NULL` 必须同真同假。"""
    seed = _seed_content(env)

    with pytest.raises(DBAPIError) as excinfo:
        _insert_feedback_raw(
            env,
            content_id=seed.content_id,
            version_id=seed.version_id,
            state=FEEDBACK_STATE_REJECTED,
            reject_reason=None,
        )

    assert _sqlstate(excinfo.value) == fb_ddl.CONSISTENCY_SQLSTATE


# ===========================================================================
# 静态接缝（R1 / R2 / R7）
# ===========================================================================


def _feedback_source(name: str) -> str:
    root = Path(feedback_pkg.__file__).resolve().parent
    return (root / name).read_text(encoding="utf-8")


def _called_names(source: str) -> set[str]:
    """源码里出现过的**函数调用名**集合（AST，不看注释/文档字符串）。

    用 AST 而不是纯字符串搜索：文档字符串里提到 `insert_version(` 是
    说明，不是调用。判据只认调用点。
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


def test_feedback_module_has_no_direct_version_write() -> None:
    """R1：feedback 包不得自己写版本 / 推进投影。

    写入原语只有 `content.service` / `versions.service` 可以调用；本包
    若复制一份，并发判断会长出第二个判断点，两处在边界上迟早分叉
    （一条路径 412，另一条静默覆盖）。
    """
    forbidden = {"insert_version", "advance_projection", "register_version"}
    for name in ("content_bridge.py", "media_bridge.py", "service.py"):
        called = _called_names(_feedback_source(name))
        leaked = forbidden & called
        assert not leaked, f"{name} 直接调用了版本写入原语：{sorted(leaked)}"

    # 正向对照：接缝文件**确实**转发给 content 的编辑函数（证明读的是对的文件）。
    assert "append_edited_version" in _called_names(
        _feedback_source("content_bridge.py")
    )


def test_feedback_module_has_no_duplicate_paragraph_split() -> None:
    """R2：本包零切段实现，切段只经 `media_bridge`。

    两套切段逻辑会让同一段正文在"生成时"与"提意见时"得到不同的 ref，
    而分叉是静默的（各自测试都会过）。
    """
    for name in ("media_bridge.py", "service.py"):
        called = _called_names(_feedback_source(name))
        assert "splitlines" not in called, f"{name} 自己切了行"
        assert "split" not in called, f"{name} 自己按模式切了正文"

    bridge = _feedback_source("media_bridge.py")
    assert "paragraph_refs_for_body" in _called_names(bridge)
    assert "split_body_into_paragraphs" in _called_names(
        _feedback_source("service.py")
    )


@pytest.mark.skip(
    reason=(
        "WP29 迁移由主 agent 后续建立；届时跑 "
        "check_migration_value_domain_no_live_import.py 代替本测试"
    )
)
def test_migration_value_domain_literals_not_imported_from_feedback_types() -> None:
    """R7：迁移文件不得从 `feedback.types` 导入取值域（本次无迁移文件）。"""


# ===========================================================================
# R3 / R4：If-Match 与反馈锁的 412
# ===========================================================================


def test_require_if_match_missing_returns_428() -> None:
    """缺 `If-Match` ⇒ 428（不是 412：客户端该先读一次）。"""
    resource_id = uuid.uuid4()
    with pytest.raises(ApiError) as excinfo:
        require_if_match(
            _request(),
            resource_id=resource_id,
            current_version=3,
            resource_label="反馈",
        )
    assert excinfo.value.code == ErrorCode.PRECONDITION_REQUIRED
    assert _STATUS_BY_CODE[excinfo.value.code] == 428


def test_require_if_match_malformed_returns_400() -> None:
    """`If-Match` 不是合法 ETag ⇒ 400（格式问题，与版本无关）。"""
    resource_id = uuid.uuid4()
    with pytest.raises(ApiError) as excinfo:
        require_if_match(
            _request({"If-Match": "not-an-etag"}),
            resource_id=resource_id,
            current_version=3,
            resource_label="反馈",
        )
    assert excinfo.value.code == ErrorCode.MALFORMED_REQUEST
    assert _STATUS_BY_CODE[excinfo.value.code] == 400


def test_require_if_match_stale_etag_returns_412() -> None:
    """`If-Match` 格式合法但版本过期 ⇒ 412，并回带当前 ETag。"""
    resource_id = uuid.uuid4()
    with pytest.raises(ApiError) as excinfo:
        require_if_match(
            _request({"If-Match": build_etag(resource_id, 2)}),
            resource_id=resource_id,
            current_version=5,
            resource_label="反馈",
        )
    assert excinfo.value.code == ErrorCode.VERSION_CONFLICT
    assert _STATUS_BY_CODE[excinfo.value.code] == 412
    assert excinfo.value.details["currentEtag"] == build_etag(resource_id, 5)


def test_advance_feedback_rejects_stale_lock_version_with_412(
    env: FeedbackEnv,
) -> None:
    """服务层自己的乐观锁：过期 `expected_lock_version` ⇒ 412。"""
    seed = _seed_content(env)
    feedback_id = _create_feedback(env, seed)
    current_lock = int(
        _scalar(
            env,
            "SELECT lock_version FROM content_feedback_items WHERE id = :id",
            {"id": feedback_id},
        )
    )

    with pytest.raises(ApiError) as excinfo:
        with session_scope(env.settings) as session:
            f_service.advance_feedback(
                FeedbackRepository(session),
                feedback_id=feedback_id,
                expected_lock_version=current_lock + 7,
                to_state=FEEDBACK_STATE_IN_PROGRESS,
                actor_kind=ACTOR_KIND_USER,
            )

    assert excinfo.value.code == ErrorCode.VERSION_CONFLICT
    assert _STATUS_BY_CODE[excinfo.value.code] == 412
    assert (
        excinfo.value.details["reason"] == REASON_FEEDBACK_LOCK_VERSION_MISMATCH
    )
    assert excinfo.value.details["currentEtag"] == build_etag(
        feedback_id, current_lock
    )
    # 412 是"零写入"：状态与事件都没有前进。
    assert _scalar(
        env, "SELECT state FROM content_feedback_items WHERE id = :id",
        {"id": feedback_id},
    ) == FEEDBACK_STATE_OPEN
    assert _scalar(
        env,
        "SELECT count(*) FROM content_feedback_events WHERE feedback_item_id = :id",
        {"id": feedback_id},
    ) == 1


def test_advance_feedback_412_replay_with_current_etag_succeeds(
    env: FeedbackEnv,
) -> None:
    """R4：从 412 的 `currentEtag` 解析出版本，原样重提必须成功（往返）。"""
    seed = _seed_content(env)
    feedback_id = _create_feedback(env, seed)
    current_lock = int(
        _scalar(
            env,
            "SELECT lock_version FROM content_feedback_items WHERE id = :id",
            {"id": feedback_id},
        )
    )

    with pytest.raises(ApiError) as conflict:
        with session_scope(env.settings) as session:
            f_service.advance_feedback(
                FeedbackRepository(session),
                feedback_id=feedback_id,
                expected_lock_version=current_lock + 100,
                to_state=FEEDBACK_STATE_IN_PROGRESS,
                actor_kind=ACTOR_KIND_USER,
            )

    assert conflict.value.code == ErrorCode.VERSION_CONFLICT
    etag = conflict.value.details["currentEtag"]
    parsed = parse_etag(etag)
    assert parsed == (str(feedback_id), current_lock), (
        f"412 回带的 ETag 必须能被 parse_etag 还原成 (id, lock_version)，实际 {parsed!r}"
    )

    with session_scope(env.settings) as session:
        advanced = f_service.advance_feedback(
            FeedbackRepository(session),
            feedback_id=feedback_id,
            expected_lock_version=parsed[1],
            to_state=FEEDBACK_STATE_IN_PROGRESS,
            actor_kind=ACTOR_KIND_USER,
            note="按 412 回带的 ETag 重提",
        )
        assert advanced.state == FEEDBACK_STATE_IN_PROGRESS
        assert int(advanced.lock_version) == parsed[1] + 1

    # 状态与历史都对得上：两次事件，序号连续。
    assert _scalar(
        env, "SELECT state FROM content_feedback_items WHERE id = :id",
        {"id": feedback_id},
    ) == FEEDBACK_STATE_IN_PROGRESS
    assert _rows(
        env,
        "SELECT from_state, to_state FROM content_feedback_events"
        " WHERE feedback_item_id = :id ORDER BY seq_no",
        {"id": feedback_id},
    ) == [(None, FEEDBACK_STATE_OPEN), (FEEDBACK_STATE_OPEN, FEEDBACK_STATE_IN_PROGRESS)]


# ===========================================================================
# R5：STALE 只打标记，条数不变
# ===========================================================================


def test_list_feedback_marks_stale_without_dropping_count(
    env: FeedbackEnv,
) -> None:
    """编辑正文后旧段落锚点变 STALE —— 但这条反馈仍在列表里。"""
    seed = _seed_content(env, paragraphs=2)
    feedback_id = _create_feedback(
        env, seed, paragraph_ref=seed.paragraph_refs[0]
    )

    before = _list_feedback(env, seed.content_id)
    assert len(before.items) == 1
    assert before.items[0]["anchorState"] == f_service.ANCHOR_STATE_FRESH

    new_body = "改过的第一段（已经不含原来的段落 ref）。\n\n第 2 段：这是第 2 段的内容。"
    with session_scope(env.settings) as session:
        f_service.edit_content(
            ContentRepository(session),
            version_repo=VersionRepository(session),
            baseline_repo=BaselineRepository(session),
            content_id=seed.content_id,
            expected_lock_version=0,
            body=new_body,
        )

    new_refs = {
        ref for _i, _t, ref in media_bridge.split_body_into_paragraphs(new_body)
    }
    assert seed.paragraph_refs[0] not in new_refs, "铺底前提失效：编辑后旧 ref 仍然存在"

    after = _list_feedback(env, seed.content_id)
    # ① 条数不变（读时判定不删行、不过滤）
    assert len(after.items) == 1
    assert after.items[0]["id"] == str(feedback_id)
    # ② 锚点状态变了
    assert after.items[0]["anchorState"] == f_service.ANCHOR_STATE_STALE
    # ③ 库里也没被改（STALE 不落库、不写放大）
    assert _scalar(
        env,
        "SELECT count(*) FROM content_feedback_items WHERE content_item_id = :c",
        {"c": seed.content_id},
    ) == 1


# ===========================================================================
# R6：首行事件与状态列同一事务
# ===========================================================================


def test_create_feedback_event_and_state_same_transaction(
    env: FeedbackEnv,
) -> None:
    """`create_feedback` 只 flush 不 commit：回滚后反馈行与事件一起消失。

    这是"同一事务"的强判据 —— 若实现里先提交了事件（或状态），
    回滚后会在库里留下半态，本测试立刻红。
    """
    seed = _seed_content(env)
    engine = get_engine(env.settings)

    with Session(engine) as session:
        item = f_service.create_feedback(
            FeedbackRepository(session),
            project_id=env.project_id,
            content_id=seed.content_id,
            observed_version_id=seed.version_id,
            anchor_kind=ANCHOR_KIND_PARAGRAPH,
            category=FEEDBACK_CATEGORY_FACT,
            severity=FEEDBACK_SEVERITY_WARNING,
            scope=FEEDBACK_SCOPE_CURRENT_ONLY,
            author_kind=ACTOR_KIND_USER,
            body="同一事务判据",
            paragraph_ref=seed.paragraph_refs[0],
            created_by=env.owner_id,
        )
        # 记住 id：`rollback()` 会让 ORM 实例过期，之后再访问属性会触发
        # 一次"找不到行"的加载（ObjectDeletedError），掩盖真正的判据。
        feedback_item_id = item.id
        # 同一次 flush 之后，两者都可见：行是 OPEN，首行事件 from_state=None。
        events = FeedbackRepository(session).list_feedback_events(item.id)
        assert len(events) == 1
        assert events[0].from_state is None
        assert events[0].to_state == FEEDBACK_STATE_OPEN
        assert events[0].feedback_item_id == item.id
        assert item.state == FEEDBACK_STATE_OPEN

        session.rollback()

    # 回滚后：状态与历史都不存在（没有"事件写了但状态没变"的半态）。
    assert _scalar(
        env, "SELECT count(*) FROM content_feedback_items WHERE id = :id",
        {"id": feedback_item_id},
    ) == 0
    assert _scalar(
        env,
        "SELECT count(*) FROM content_feedback_events WHERE feedback_item_id = :id",
        {"id": feedback_item_id},
    ) == 0


# ===========================================================================
# 7 个用例的正常路径
# ===========================================================================


def test_build_preview_returns_paragraphs_media_and_candidates(
    env: FeedbackEnv,
) -> None:
    """预览一次装配：段落 / 图片位 / 标题候选 / 计数来自同一版正文。"""
    seed = _seed_content(env, paragraphs=2)
    media_item_id = _seed_media_item(
        env, content_id=seed.content_id, paragraph_ref=seed.paragraph_refs[0]
    )
    candidate_id = _seed_title_candidate(env, version_id=seed.version_id)

    with session_scope(env.settings) as session:
        payload = f_service.build_preview(
            FeedbackRepository(session),
            ContentRepository(session),
            VersionRepository(session),
            BaselineRepository(session),
            content_id=seed.content_id,
        )

    assert payload.current_version_id == seed.version_id
    assert payload.version_no == 1
    assert payload.lock_version == 0
    assert payload.etag == build_etag(seed.content_id, 0)
    assert [paragraph["ref"] for paragraph in payload.paragraphs] == list(
        seed.paragraph_refs
    )
    assert [item["mediaItemId"] for item in payload.media_items] == [
        str(media_item_id)
    ]
    assert payload.media_items[0]["paragraphRef"] == seed.paragraph_refs[0]
    assert [item["titleCandidateId"] for item in payload.title_candidates] == [
        str(candidate_id)
    ]
    assert payload.title_candidates[0]["isAdopted"] is False
    assert payload.feedback_count == 0
    assert payload.self_check_summary is None  # WP30 未开工，不编造自检数据


def test_edit_content_appends_version_via_bridge(env: FeedbackEnv) -> None:
    """编辑 = 追加一版（不是原地改），投影前进、锁版本 +1。"""
    seed = _seed_content(env, paragraphs=2)
    new_body = "新的第一段。\n\n新的第二段。"

    with session_scope(env.settings) as session:
        summary = f_service.edit_content(
            ContentRepository(session),
            version_repo=VersionRepository(session),
            baseline_repo=BaselineRepository(session),
            content_id=seed.content_id,
            expected_lock_version=0,
            body=new_body,
        )

    assert summary.version_no == 2
    assert summary.id != seed.version_id
    assert _scalar(
        env, "SELECT count(*) FROM content_versions WHERE content_item_id = :c",
        {"c": seed.content_id},
    ) == 2
    assert _rows(
        env,
        "SELECT current_version_id, lock_version FROM content_items WHERE id = :id",
        {"id": seed.content_id},
    ) == [(summary.id, 1)]
    assert _scalar(
        env,
        "SELECT body FROM content_versions WHERE content_item_id = :c"
        " ORDER BY version_no DESC LIMIT 1",
        {"c": seed.content_id},
    ) == new_body


def test_create_feedback_rejects_anchor_mismatch(env: FeedbackEnv) -> None:
    """`anchor_kind=PARAGRAPH` 却传 `media_item_id` ⇒ 可读 4xx。"""
    seed = _seed_content(env)
    media_item_id = _seed_media_item(
        env, content_id=seed.content_id, paragraph_ref=seed.paragraph_refs[0]
    )

    with pytest.raises(ApiError) as excinfo:
        _create_feedback(
            env,
            seed,
            anchor_kind=ANCHOR_KIND_PARAGRAPH,
            paragraph_ref=seed.paragraph_refs[0],
            media_item_id=media_item_id,
        )

    assert excinfo.value.code == ErrorCode.VALIDATION_FAILED
    assert _STATUS_BY_CODE[excinfo.value.code] == 422
    assert (
        excinfo.value.details["reason"]
        == f_service.REASON_ANCHOR_PARAGRAPH_MEDIA_CONFLICT
    )
    assert _scalar(
        env,
        "SELECT count(*) FROM content_feedback_items WHERE content_item_id = :c",
        {"c": seed.content_id},
    ) == 0


def test_create_feedback_rejects_media_anchor_from_other_content(
    env: FeedbackEnv,
) -> None:
    """服务层的可读 4xx 先于 DDL 触发器拦下跨内容媒体锚点。"""
    content_a = _seed_content(env)
    content_b = _seed_content(env)
    media_b = _seed_media_item(
        env, content_id=content_b.content_id, paragraph_ref=content_b.paragraph_refs[0]
    )

    with pytest.raises(ApiError) as excinfo:
        _create_feedback(
            env,
            content_a,
            anchor_kind=ANCHOR_KIND_MEDIA,
            media_item_id=media_b,
        )

    assert excinfo.value.code == ErrorCode.VALIDATION_FAILED
    assert (
        excinfo.value.details["reason"]
        == f_service.REASON_MEDIA_ITEM_CONTENT_MISMATCH
    )


def test_decide_review_blocks_when_unresolved_blocking_feedback_exists(
    env: FeedbackEnv,
) -> None:
    """存在未解决的 BLOCKING 反馈 ⇒ 409，且不落任何审核行。"""
    seed = _seed_content(env)
    blocking_id = _create_feedback(
        env, seed, severity=FEEDBACK_SEVERITY_BLOCKING, body="事实错误必须先改。"
    )

    with pytest.raises(ApiError) as excinfo:
        with session_scope(env.settings) as session:
            f_service.decide_review(
                FeedbackRepository(session),
                project_id=env.project_id,
                content_item_id=seed.content_id,
                subject_version_id=seed.version_id,
                decision=REVIEW_DECISION_APPROVED,
                reason="看起来没问题",
                decided_by=env.owner_id,
            )

    assert excinfo.value.code == ErrorCode.INVALID_TRANSITION
    assert _STATUS_BY_CODE[excinfo.value.code] == 409
    assert (
        excinfo.value.details["reason"]
        == f_service.REASON_UNRESOLVED_BLOCKING_FEEDBACK
    )
    assert excinfo.value.details["blockingFeedbackIds"] == [str(blocking_id)]
    assert _scalar(
        env, "SELECT count(*) FROM content_reviews WHERE content_item_id = :c",
        {"c": seed.content_id},
    ) == 0


def test_decide_review_approves_after_blocking_feedback_rejected(
    env: FeedbackEnv,
) -> None:
    """BLOCKING 反馈被拒后，审核通过可以落库（阻断是"当前事实"，不是永久禁令）。"""
    seed = _seed_content(env)
    blocking_id = _create_feedback(
        env, seed, severity=FEEDBACK_SEVERITY_BLOCKING, body="事实错误必须先改。"
    )

    current_lock = int(
        _scalar(
            env,
            "SELECT lock_version FROM content_feedback_items WHERE id = :id",
            {"id": blocking_id},
        )
    )
    with session_scope(env.settings) as session:
        f_service.advance_feedback(
            FeedbackRepository(session),
            feedback_id=blocking_id,
            expected_lock_version=current_lock,
            to_state=FEEDBACK_STATE_REJECTED,
            actor_kind=ACTOR_KIND_USER,
            reject_reason="复核后确认事实无误",
        )

    with session_scope(env.settings) as session:
        review = f_service.decide_review(
            FeedbackRepository(session),
            project_id=env.project_id,
            content_item_id=seed.content_id,
            subject_version_id=seed.version_id,
            decision=REVIEW_DECISION_APPROVED,
            reason="阻断项已处置",
            decided_by=env.owner_id,
        )
        review_id = review.id

    assert _rows(
        env,
        "SELECT decision, subject_version_id FROM content_reviews WHERE id = :id",
        {"id": review_id},
    ) == [(REVIEW_DECISION_APPROVED, seed.version_id)]


def test_decide_reviews_batch_partial_failure_does_not_abort_others(
    env: FeedbackEnv,
) -> None:
    """批量审核：中间一条因 BLOCKING 反馈失败，前后两条仍然成功并落库。"""
    first = _seed_content(env)
    blocked = _seed_content(env)
    third = _seed_content(env)
    _create_feedback(
        env, blocked, severity=FEEDBACK_SEVERITY_BLOCKING, body="这条不能通过。"
    )

    content_ids = [first.content_id, blocked.content_id, third.content_id]
    with session_scope(env.settings) as session:
        results = f_service.decide_reviews_batch(
            FeedbackRepository(session),
            project_id=env.project_id,
            content_item_ids=content_ids,
            decision=REVIEW_DECISION_APPROVED,
            reason="批量复核通过",
            decided_by=env.owner_id,
        )

    assert [result["ok"] for result in results] == [True, False, True]
    assert [result["contentItemId"] for result in results] == [
        str(content_id) for content_id in content_ids
    ]
    assert "阻断" in results[1]["error"]

    # 外层事务提交后：成功的两条留在库里，失败的那条没有留下半行。
    assert {
        row[0]
        for row in _rows(
            env,
            "SELECT content_item_id FROM content_reviews WHERE project_id = :p",
            {"p": env.project_id},
        )
    } == {first.content_id, third.content_id}
    assert _scalar(
        env,
        "SELECT count(*) FROM content_reviews WHERE content_item_id = :c",
        {"c": blocked.content_id},
    ) == 0
