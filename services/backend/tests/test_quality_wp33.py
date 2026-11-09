"""WP33 `quality` 包的验收判据 —— 纯函数 / 结构性判据 / 数据库行为 / 服务层。

## 夹具纪律（照 `test_batches_wp32.py` / `test_versions_wp34.py`）

· 一律跑**私有库**（`REDNOTE_TEST_DATABASE_URL`；缺省指向 `rednote_test_wp33`）；
· 只重建本包 7 张表 + 13 个触发器（6 不可变 + 7 一致性），其余表按迁移已就绪；
  夹具断言「除被跨包引用的表外应可删」，并核对夹具没有改变跨包外键集合；
· 每个用例新造 user / project / 内容，**按项目隔离**，不数全库行；
· 数据库探针断言 **sqlstate**；延迟约束触发器用 `SET CONSTRAINTS ALL IMMEDIATE` 强制检查；
· 阈值是**占位（待 OP-228）**：测试里的具体数字只用于验证机制（边界 / 快照），不是业务口径。

## 本文件守什么

| 组 | 判据 |
|---|---|
| 纯函数 | 规范化 / 2-gram / Jaccard 千分比 / 交叉相乘比较 / 缺失不填 0 / 7 个维度 / 顺序对位 |
| 结构 | 零切段实现（AST）/ 纯度 / 分层 / 无 float 列 / 无 `ORDER BY version_no DESC` / 与 DDL 草案逐名对账 |
| 数据库 | 唯一约束与 CHECK / 6 张不可变表（23001）/ 7 个延迟一致性触发器（23514）|
| 服务层 | 报告落 REPORT 版本 / 幂等与指纹 / 投影 CAS / 范围与项目隔离 / E11 边与回滚 / 阈值快照 / 导出 |
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import sys
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Float, Numeric, text
from sqlalchemy.exc import DBAPIError

from conftest import cross_package_foreign_keys, drop_own_tables, truncate_targets

# FK 目标表必须在 `create_all` 之前 import（否则 NoReferencedTableError）。
import rednote.modules.baseline.models  # noqa: F401
import rednote.modules.content.models  # noqa: F401
import rednote.modules.files.models  # noqa: F401
import rednote.modules.gateway.models  # noqa: F401
import rednote.modules.identity.models  # noqa: F401
import rednote.modules.jobs.models  # noqa: F401
import rednote.modules.media.models  # noqa: F401
import rednote.modules.projects.models  # noqa: F401
import rednote.modules.prompts.models  # noqa: F401
import rednote.modules.versions.models  # noqa: F401
import rednote.modules.quality.models  # noqa: F401

import os

from rednote.modules.files.models import STORAGE_BACKEND_LOCAL
from rednote.modules.files.storage import LocalStorageBackend
from rednote.modules.identity.models import ROLE_ADMIN, ROLE_OPERATOR
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.projects.models import PHASE_CLOSED, Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.modules.quality import ddl as q_ddl
from rednote.modules.quality import dimensions as q_dimensions
from rednote.modules.quality import media_bridge
from rednote.modules.quality import service as q_service
from rednote.modules.quality import similarity as q_sim
from rednote.modules.quality import types as q_types
from rednote.modules.quality import versions_bridge
from rednote.modules.quality.models import IMMUTABLE_TABLES, OWN_TABLES
from rednote.modules.quality.repository import QualityRepository
from rednote.modules.quality.types import (
    DIMENSIONS,
    MediaSlot,
    Thresholds,
)
from rednote.platform.base import Base
from rednote.platform.configuration import Settings
from rednote.platform.database import get_engine, reset_engines, session_scope
from rednote.platform.errors import ApiError, ErrorCode
from rednote.platform.immutability import IMMUTABLE_RECORD_SQLSTATE

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
#: WP33 私有测试库（**不是**共享的 `rednote_test`）。
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test_wp33"
TEST_DATABASE_URL_ENV = "REDNOTE_TEST_DATABASE_URL"

DROP_ORDER = (
    "quality_report_artifacts",
    "quality_media_alignments",
    "quality_similarity_pairs",
    "quality_dimension_results",
    "quality_report_items",
    "quality_report_versions",
    "quality_reports",
)
CHECK_VIOLATION = "23514"
FK_VIOLATION = "23503"

SRC = Path(__file__).resolve().parents[1] / "src/rednote/modules/quality"
REPO_ROOT = Path(__file__).resolve().parents[3]
DDL_DRAFT = REPO_ROOT / "docs/m3/WP33-ddl-草案.py"

_NODE_ID = 0


def _test_database_url() -> str:
    return os.environ.get(TEST_DATABASE_URL_ENV, DEFAULT_TEST_DATABASE_URL).strip()


def _make_settings(storage_root: Path | None = None) -> Settings:
    payload: dict[str, Any] = {
        "database_url": _test_database_url(),
        "environment": "test",
        "session_cookie_secure": False,
        "oidc_issuer": TEST_ISSUER,
        "oidc_token_encryption_key": TEST_ENCRYPTION_KEY,
    }
    if storage_root is not None:
        payload["storage_backend"] = STORAGE_BACKEND_LOCAL
        payload["storage_local_root"] = str(storage_root)
    return Settings(**payload)


# ===========================================================================
# 建表夹具（module 级）
# ===========================================================================


@pytest.fixture(scope="module", autouse=True)
def _ensure_own_tables() -> Iterator[None]:
    """只重建本包 7 张表 + 13 个触发器；前后对账跨包外键集合不变。"""
    reset_engines()
    engine = get_engine(_make_settings())
    fks_before = cross_package_foreign_keys(engine, OWN_TABLES)
    dropped, kept = drop_own_tables(engine, DROP_ORDER)
    # 「除被跨包引用的表外应可删」：被留下的表，必须真的有来自本包之外的引用方。
    assert set(dropped) | set(kept) == set(DROP_ORDER)
    own = set(OWN_TABLES)
    with engine.connect() as conn:
        for name in kept:
            referrers = {
                row[0]
                for row in conn.execute(
                    text(
                        "select conrelid::regclass::text from pg_constraint"
                        " where contype='f' and confrelid = :t::regclass"
                    ),
                    {"t": name},
                )
            }
            assert referrers - own, f"{name} 被留下但没有任何跨包引用方：{referrers}"
    Base.metadata.create_all(
        engine, tables=[Base.metadata.tables[name] for name in OWN_TABLES]
    )
    fks_after = cross_package_foreign_keys(engine, OWN_TABLES)
    assert fks_after == fks_before, (
        f"夹具改变了跨包外键：before={sorted(fks_before)} after={sorted(fks_after)}"
        f"（dropped={dropped} kept={kept}）"
    )
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(truncate_targets(engine, DROP_ORDER))))
        for statement in q_ddl.install_statements():
            conn.execute(text(statement))
    yield


@pytest.fixture(autouse=True)
def _clean_own_rows() -> Iterator[None]:
    reset_engines()
    engine = get_engine(_make_settings())
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(truncate_targets(engine, DROP_ORDER))))
    yield


# ===========================================================================
# 环境与造数
# ===========================================================================


@dataclass(frozen=True)
class Env:
    settings: Settings
    storage_root: Path | None
    project_id: uuid.UUID
    closed_project_id: uuid.UUID
    other_project_id: uuid.UUID
    owner_id: uuid.UUID
    collab_id: uuid.UUID
    outsider_id: uuid.UUID
    admin_id: uuid.UUID
    baseline_id: uuid.UUID


def _create_user(settings: Settings, *, role: str = ROLE_OPERATOR) -> uuid.UUID:
    with session_scope(settings) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name=f"WP33-{role}",
            role=role,
        )
        return user.id


def _seed_project(
    settings: Settings,
    *,
    owner_id: uuid.UUID,
    collaborators: tuple[uuid.UUID, ...] = (),
    closed: bool = False,
) -> uuid.UUID:
    with session_scope(settings) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name=f"产品-{uuid.uuid4().hex[:6]}")
        session.add(product)
        session.flush()
        extra: dict[str, Any] = {}
        if closed:
            extra = {"phase": PHASE_CLOSED, "closed_at": datetime.now(timezone.utc)}
        project = Project(
            project_code=f"WP33-{uuid.uuid4().hex[:8].upper()}",
            name="WP33 测试项目",
            brand_id=brand.id,
            product_id=product.id,
            owner_id=owner_id,
            target_delivery_count=3,
            due_date=date(2030, 12, 31),
            **extra,
        )
        session.add(project)
        session.flush()
        ProjectRepository(session).add_owner_and_collaborators(
            project.id, owner_id=owner_id, collaborator_ids=collaborators
        )
        return project.id


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _seed_baseline(settings: Settings, project_id: uuid.UUID, owner_id: uuid.UUID) -> uuid.UUID:
    """造一条基线（版本节点 + 基线 + 同事务批准；写法照 `test_review_wp30.py::_seed_baseline`）。"""
    baseline_id = uuid.uuid4()
    with session_scope(settings) as session:
        session.execute(
            text(
                "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
                " payload_hash, schema_version, project_id)"
                " VALUES (:id, 'BASELINE', :pid, 1, :h, 1, :pid)"
            ),
            {"id": baseline_id, "pid": project_id, "h": "0" * 64},
        )
        session.execute(
            text(
                "INSERT INTO production_baselines (id, project_id, brief_version_id,"
                " ruleset_version, snapshot, change_reason)"
                " VALUES (:id, :pid, NULL, :rs, '{}'::jsonb, 'WP33 判据铺底')"
            ),
            {"id": baseline_id, "pid": project_id, "rs": f"rs-{uuid.uuid4().hex[:8]}"},
        )
        session.execute(
            text(
                "INSERT INTO step_approvals (project_id, step_no, subject_kind,"
                " subject_version_id, decision, reason, decided_by)"
                " VALUES (:pid, 3, 'BASELINE', :id, 'APPROVED', 'WP33 判据铺底', :who)"
            ),
            {"pid": project_id, "id": baseline_id, "who": owner_id},
        )
    return baseline_id


@pytest.fixture
def env(tmp_path: Path) -> Env:
    root = tmp_path / "storage"
    settings = _make_settings(root)
    owner = _create_user(settings)
    collab = _create_user(settings)
    project_id = _seed_project(settings, owner_id=owner, collaborators=(collab,))
    return Env(
        settings=settings,
        storage_root=root,
        project_id=project_id,
        closed_project_id=_seed_project(settings, owner_id=owner, closed=True),
        other_project_id=_seed_project(settings, owner_id=owner),
        owner_id=owner,
        collab_id=collab,
        outsider_id=_create_user(settings),
        admin_id=_create_user(settings, role=ROLE_ADMIN),
        baseline_id=_seed_baseline(settings, project_id, owner),
    )


def _rows(env: Env, sql: str, params: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
    with session_scope(env.settings) as session:
        return [tuple(r) for r in session.execute(text(sql), params or {}).fetchall()]


def _scalar(env: Env, sql: str, params: dict[str, Any] | None = None) -> Any:
    with session_scope(env.settings) as session:
        return session.execute(text(sql), params or {}).scalar_one_or_none()


def _exec(env: Env, sql: str, params: dict[str, Any] | None = None) -> None:
    with session_scope(env.settings) as session:
        session.execute(text(sql), params or {})


# --- 文本素材：全由互不相同的 CJK 字符构成，所有 2-gram 互异 -----------------------


def _cjk(n: int, start: int = 0) -> str:
    return "".join(chr(0x4E00 + start + i) for i in range(n))


def _doc(seed: int, *, paragraphs: int = 3) -> tuple[str, str]:
    """`seed` 相同 ⇒ 文本逐字相同（相似度 1000）；`seed` 相差 ≥1000 ⇒ 无任何共享 2-gram（相似度 0）。"""
    title = _cjk(12, seed)
    body = "\n\n".join(_cjk(40, seed + 100 * (i + 1)) for i in range(paragraphs))
    return title, body


@dataclass
class Seeded:
    content_id: uuid.UUID
    project_id: uuid.UUID
    version_ids: list[uuid.UUID] = field(default_factory=list)
    context_id: uuid.UUID | None = None


def _node(conn: Any, *, entity_type: str, project_id: uuid.UUID | None, entity_id: uuid.UUID,
          revision_no: int = 1, created_by: uuid.UUID | None = None,
          payload_hash: str | None = None, node_id: uuid.UUID | None = None) -> uuid.UUID:
    node_id = node_id or uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO version_nodes (id, entity_type, entity_id, project_id, revision_no,"
            " schema_version, payload_hash, created_by)"
            " VALUES (:id, :t, :e, :p, :r, 1, :h, :by)"
        ),
        {"id": node_id, "t": entity_type, "e": entity_id, "p": project_id, "r": revision_no,
         "h": payload_hash or _hash(f"{entity_id}-{revision_no}-{node_id}"), "by": created_by},
    )
    return node_id


def _insert_content_version(conn: Any, env: Env, c: Seeded, no: int, title: str, body: str,
                            context_id: uuid.UUID | None = None) -> uuid.UUID:
    node = _node(conn, entity_type="CONTENT", project_id=c.project_id, entity_id=c.content_id,
                 revision_no=no, created_by=env.owner_id)
    if context_id is not None:
        conn.execute(
            text(
                "INSERT INTO generation_contexts (id, project_id, content_id, content_version_id,"
                " task_kind, idempotency_key, baseline_id)"
                " VALUES (:id, :p, :c, :cv, 'CONTENT_COPY', :k, :b)"
            ),
            {"id": context_id, "p": c.project_id, "c": c.content_id, "cv": node,
             "k": f"gc-{uuid.uuid4().hex[:8]}", "b": env.baseline_id},
        )
    conn.execute(
        text(
            "INSERT INTO content_versions (id, content_item_id, version_no, title, body,"
            " tags, bindings, payload, payload_hash, schema_version, created_by,"
            " generation_context_id)"
            " VALUES (:id, :c, :no, :t, :b, '[]'::jsonb, '[]'::jsonb, '{}'::jsonb,"
            " :h, 1, :by, :gc)"
        ),
        {"id": node, "c": c.content_id, "no": no, "t": title, "b": body,
         "h": _hash(f"cv-{c.content_id}-{no}"), "by": env.owner_id, "gc": context_id},
    )
    c.version_ids.append(node)
    return node


def _seed_content(
    env: Env,
    *,
    project_id: uuid.UUID | None = None,
    title: str | None = None,
    body: str | None = None,
    seed: int = 0,
    no_version: bool = False,
    with_context: bool = False,
) -> Seeded:
    pid = project_id or env.project_id
    d_title, d_body = _doc(seed)
    c = Seeded(content_id=uuid.uuid4(), project_id=pid)
    with session_scope(env.settings) as session:
        conn = session.connection()
        conn.execute(
            text(
                "INSERT INTO content_items (id, project_id, current_version_id, state,"
                " current_version_no, lock_version, created_by)"
                " VALUES (:c, :p, NULL, 'DRAFT', 0, 0, :by)"
            ),
            {"c": c.content_id, "p": pid, "by": env.owner_id},
        )
        if not no_version:
            ctx = uuid.uuid4() if with_context else None
            c.context_id = ctx
            vid = _insert_content_version(
                conn, env, c, 1, title if title is not None else d_title,
                body if body is not None else d_body, ctx,
            )
            conn.execute(
                text("UPDATE content_items SET current_version_id=:v, current_version_no=1 WHERE id=:c"),
                {"v": vid, "c": c.content_id},
            )
    return c


def _add_version(env: Env, c: Seeded, *, title: str, body: str, move: bool = True) -> uuid.UUID:
    with session_scope(env.settings) as session:
        no = len(c.version_ids) + 1
        vid = _insert_content_version(session.connection(), env, c, no, title, body)
        if move:
            session.execute(
                text("UPDATE content_items SET current_version_id=:v, current_version_no=:n WHERE id=:c"),
                {"v": vid, "n": no, "c": c.content_id},
            )
    return vid


def _point_to(env: Env, c: Seeded, index: int | None) -> None:
    if index is None:
        _exec(env, "UPDATE content_items SET current_version_id=NULL, current_version_no=0 WHERE id=:c",
              {"c": c.content_id})
    else:
        _exec(env, "UPDATE content_items SET current_version_id=:v, current_version_no=:n WHERE id=:c",
              {"v": c.version_ids[index], "n": index + 1, "c": c.content_id})


def _refs_of(body: str) -> tuple[str, ...]:
    return tuple(ref for _i, _t, ref in media_bridge.split_body_into_paragraphs(body))


def _seed_media(env: Env, c: Seeded, *, sort_order: int, ref: str, state: str = "PENDING") -> uuid.UUID:
    mid = uuid.uuid4()
    _exec(
        env,
        "INSERT INTO media_items (id, project_id, content_item_id, paragraph_ref, sort_order,"
        " state, created_by) VALUES (:id, :p, :c, :r, :s, :st, :by)",
        {"id": mid, "p": c.project_id, "c": c.content_id, "r": ref, "s": sort_order,
         "st": state, "by": env.owner_id},
    )
    return mid


def _generate(
    env: Env,
    contents: list[Seeded] | None = None,
    *,
    kind: str = q_types.SCOPE_CONTENT_SET,
    project_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    content_ids: list[uuid.UUID] | None = None,
    actor: uuid.UUID | None = None,
) -> tuple[bool, uuid.UUID, uuid.UUID]:
    """独立事务里生成一份报告，返回 `(created, series_id, version_id)`。"""
    ids = content_ids if content_ids is not None else (
        [c.content_id for c in contents] if contents is not None else None
    )
    with session_scope(env.settings) as session:
        result = q_service.generate_report(
            QualityRepository(session),
            project_id=project_id or env.project_id,
            scope=q_service.ScopeRequest(
                kind=kind,
                content_ids=None if ids is None or kind != q_types.SCOPE_CONTENT_SET else tuple(ids),
                batch_id=batch_id,
            ),
            actor_id=actor or env.owner_id,
        )
        return result.created, result.series.id, result.version.id


def _dims(env: Env, version_id: uuid.UUID) -> dict[str, tuple[Any, ...]]:
    rows = _rows(
        env,
        "SELECT dimension, status, not_computed_reason, verdict, compared_item_count,"
        " flagged_item_count, pair_threshold_permille, alert_threshold_permille,"
        " max_similarity_permille FROM quality_dimension_results WHERE report_version_id=:v",
        {"v": version_id},
    )
    return {r[0]: r for r in rows}


def _api_error(exc: pytest.ExceptionInfo[ApiError], code: ErrorCode, reason: str) -> None:
    assert exc.value.code == code, (exc.value.code, exc.value.details)
    assert exc.value.details.get("reason") == reason, exc.value.details


def _sqlstate(exc: DBAPIError) -> str:
    return str(getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", ""))


# ===========================================================================
# D. 纯函数
# ===========================================================================


def test_normalize_folds_width_case_and_drops_punctuation_space_emoji() -> None:
    assert q_sim.normalize("ＡＢＣ １２３！") == "abc123"
    assert q_sim.normalize("Ä B") == "äb"
    assert q_sim.normalize("你好，世界！ 😀") == "你好世界"
    assert q_sim.normalize("a b\tc\nd") == "abcd"


def test_normalize_drops_variation_selector_with_emoji() -> None:
    assert q_sim.normalize("❤️好") == "好"


def test_grams_boundaries_empty_all_punctuation_and_very_short() -> None:
    assert q_sim.grams("") == frozenset()
    assert q_sim.grams("。。！？ \n") == frozenset()  # 全标点
    assert q_sim.grams("我") == frozenset({"我"})  # 极短：1 字取 1-gram
    assert q_sim.grams("我！") == frozenset({"我"})
    assert q_sim.grams("ab") == frozenset({"ab"})
    assert q_sim.grams("abc") == frozenset({"ab", "bc"})
    assert q_sim.grams("a b") == q_sim.grams("ab")


def test_pair_counts_none_is_not_zero() -> None:
    """缺失不填 0：任一侧无可比文本 ⇒ None，而不是 (0, n) 或相似度 0。"""
    assert q_sim.pair_counts(frozenset(), q_sim.grams("abc")) is None
    assert q_sim.pair_counts(q_sim.grams("abc"), frozenset()) is None
    assert q_sim.pair_counts(frozenset(), frozenset()) is None
    assert q_sim.pair_counts(q_sim.grams("abc"), q_sim.grams("xyz")) == (0, 4)


def test_identical_and_disjoint_concrete_values() -> None:
    a = q_sim.grams(_cjk(100, 0))
    assert q_sim.pair_counts(a, a) == (99, 99)
    assert q_sim.similarity_permille(99, 99) == 1000
    b = q_sim.grams(_cjk(100, 1000))
    inter, union = q_sim.pair_counts(a, b)  # type: ignore[misc]
    assert (inter, union) == (0, 198)
    assert q_sim.similarity_permille(inter, union) == 0
    # 只差 1 个字：交集 97、并集 101，floor(97000/101)=960
    edited = _cjk(100, 0)[:50] + chr(0x6000) + _cjk(100, 0)[51:]
    inter2, union2 = q_sim.pair_counts(a, q_sim.grams(edited))  # type: ignore[misc]
    assert (inter2, union2) == (97, 101)
    assert q_sim.similarity_permille(inter2, union2) == 960


def test_emoji_only_difference_is_judged_identical_known_tradeoff() -> None:
    a, b = q_sim.grams("今天天气真好😀"), q_sim.grams("今天天气真好🌧")
    counts = q_sim.pair_counts(a, b)
    assert counts is not None and q_sim.similarity_permille(*counts) == 1000


def test_threshold_uses_cross_multiplication_not_floor() -> None:
    """1/3：floor 后是 333；阈值 333 ⇒ 相似（1000 >= 999），阈值 334 ⇒ 不相似（1000 < 1002）。"""
    assert q_sim.similarity_permille(1, 3) == 333
    assert q_sim.is_similar(1, 3, 333) is True
    assert q_sim.is_similar(1, 3, 334) is False
    # 恰好等于：19/20 == 950‰ ⇒ 相似；差 1（18/19=947.4）⇒ 不相似
    assert q_sim.is_similar(19, 20, 950) is True
    assert q_sim.is_similar(18, 19, 950) is False


def test_alert_verdict_strictly_greater_boundary() -> None:
    """flagged*1000 == alert*compared ⇒ 不告警（严格大于）；多一篇 ⇒ 告警。"""
    assert q_sim.alert_verdict_is_alert(2, 4, 500) is False  # 2000 == 2000
    assert q_sim.alert_verdict_is_alert(3, 4, 500) is True
    with pytest.raises(ValueError):
        q_sim.alert_verdict_is_alert(0, 1, 500)


def _gs(*texts: str) -> dict[uuid.UUID, frozenset[str]]:
    ids = sorted(uuid.uuid4() for _ in texts)
    return {i: q_sim.grams(t) for i, t in zip(ids, texts)}


THR = Thresholds(950, 500)


def test_dimension_boundary_exactly_at_alert_threshold_is_ok_then_alert() -> None:
    same, other1, other2 = _cjk(40, 0), _cjk(40, 1000), _cjk(40, 2000)
    out = q_dimensions.compute_text_dimension(
        "BODY", _gs(same, same, other1, other2), THR
    )
    assert (out.compared_item_count, out.flagged_item_count) == (4, 2)
    assert out.verdict == q_types.VERDICT_OK  # 2*1000 == 500*4，不严格大于
    assert len(out.pairs) == 1 and out.pairs[0].similarity_permille == 1000
    out3 = q_dimensions.compute_text_dimension("BODY", _gs(same, same, same, other1), THR)
    assert (out3.flagged_item_count, out3.verdict) == (3, q_types.VERDICT_ALERT)
    assert len(out3.pairs) == 3


def test_dimension_computed_with_zero_flagged_is_not_none() -> None:
    """算了得 0：COMPUTED + flagged=0 + verdict OK；数值列不是 None。"""
    out = q_dimensions.compute_text_dimension(
        "TITLE", _gs(_cjk(12, 0), _cjk(12, 1000), _cjk(12, 2000)), THR
    )
    assert out.status == q_types.DIMENSION_STATUS_COMPUTED
    assert out.flagged_item_count == 0 and out.verdict == q_types.VERDICT_OK
    assert out.max_similarity_permille == 0 and out.pairs == ()
    assert out.pair_threshold_permille == 950 and out.alert_threshold_permille == 500


def test_dimension_fewer_than_two_comparable_is_not_computed_all_none() -> None:
    for grams_map in (_gs(), _gs(_cjk(12, 0)), _gs(_cjk(12, 0), "。。"), _gs("", "")):
        out = q_dimensions.compute_text_dimension("OPENING", grams_map, THR)
        assert out.status == q_types.DIMENSION_STATUS_NOT_COMPUTED
        assert out.not_computed_reason == q_types.NOT_COMPUTED_REASON_INSUFFICIENT_ITEMS
        assert out.verdict is None and out.compared_item_count is None
        assert out.flagged_item_count is None and out.max_similarity_permille is None
        assert out.pair_threshold_permille is None and out.alert_threshold_permille is None
        assert out.pairs == ()


def test_dimension_empty_text_item_is_not_counted_as_compared() -> None:
    out = q_dimensions.compute_text_dimension(
        "OPENING", _gs(_cjk(12, 0), _cjk(12, 0), "！！"), THR
    )
    assert out.compared_item_count == 2 and out.flagged_item_count == 2
    assert out.verdict == q_types.VERDICT_ALERT  # 2000 > 1000


def test_dimension_max_similarity_includes_non_hit_pairs() -> None:
    a = _cjk(100, 0)
    b = a[:50] + chr(0x6000) + a[51:]  # 960 vs a
    out = q_dimensions.compute_text_dimension("BODY", _gs(a, b), Thresholds(990, 500))
    assert out.status == q_types.DIMENSION_STATUS_COMPUTED
    assert out.pairs == () and out.flagged_item_count == 0
    assert out.max_similarity_permille == 960


def test_always_not_computed_dimensions_carry_reason_codes() -> None:
    assert set(q_types.ALWAYS_NOT_COMPUTED) == {"STRUCTURE", "IMAGE_SIMILARITY", "SCENE_CONCENTRATION"}
    for dimension, reason in q_types.ALWAYS_NOT_COMPUTED.items():
        out = q_dimensions.always_not_computed(dimension)
        assert out.status == q_types.DIMENSION_STATUS_NOT_COMPUTED
        assert out.not_computed_reason == reason
        assert out.verdict is None and out.compared_item_count is None
    assert q_types.ALWAYS_NOT_COMPUTED["STRUCTURE"] == "STRUCTURE_NOT_STRUCTURED"
    assert q_types.ALWAYS_NOT_COMPUTED["IMAGE_SIMILARITY"] == "IMAGE_SIMILARITY_UNAVAILABLE"


def test_dimension_registry_is_closed_seven() -> None:
    assert len(DIMENSIONS) == 7 and len(set(DIMENSIONS)) == 7
    assert set(DIMENSIONS) == set(q_types.TEXT_DIMENSIONS) | set(q_types.ALWAYS_NOT_COMPUTED)
    assert not set(q_types.TEXT_DIMENSIONS) & set(q_types.ALWAYS_NOT_COMPUTED)
    assert set(q_types.THRESHOLDS) == set(q_types.TEXT_DIMENSIONS)


def test_cross_package_vocabulary_parity_with_wp30_and_media() -> None:
    from rednote.modules.media.types import REASON_PARAGRAPH_REF_UNKNOWN
    from rednote.modules.review.types import SKIP_REASON_NO_SCENE_LINKAGE_IN_SCHEMA

    assert q_types.NOT_COMPUTED_REASON_SCENE == SKIP_REASON_NO_SCENE_LINKAGE_IN_SCHEMA
    assert q_types.ALIGN_REASON_PARAGRAPH_REF_UNKNOWN == REASON_PARAGRAPH_REF_UNKNOWN


def _slot(order: int, ref: str, state: str = "READY") -> MediaSlot:
    return MediaSlot(uuid.uuid4(), order, ref, state)


def test_align_media_aligned_misaligned_missing() -> None:
    rows, extra = q_dimensions.align_media(("r0", "r1", "r2"), [_slot(0, "r0"), _slot(1, "rX")])
    assert [(r.sort_order, r.verdict) for r in rows] == [
        (0, "ALIGNED"), (1, "MISALIGNED"), (2, "MISSING")
    ]
    assert rows[2].media_item_id is None and rows[2].actual_ref is None and rows[2].media_state is None
    assert rows[2].expected_ref == "r2" and extra == 0
    assert rows[1].expected_ref == "r1" and rows[1].actual_ref == "rX"


def test_align_media_same_ref_on_multiple_slots_is_legal() -> None:
    """多张落同一段合法：不因 ref 重复报错、不去重、不断言一一对应。"""
    rows, extra = q_dimensions.align_media(("r0", "r1", "r1"), [_slot(0, "r0"), _slot(1, "r1"), _slot(2, "r1")])
    assert [r.verdict for r in rows] == ["ALIGNED", "ALIGNED", "ALIGNED"] and extra == 0
    assert [r.expected_ref for r in rows] == ["r0", "r1", "r1"]


def test_align_media_not_evaluable_when_no_expected_refs_and_never_aligned() -> None:
    rows, extra = q_dimensions.align_media(None, [_slot(0, "r0"), _slot(5, "r1")])
    assert [r.verdict for r in rows] == ["NOT_EVALUABLE", "NOT_EVALUABLE"]
    assert all(r.expected_ref is None and r.reason == "PARAGRAPH_REF_UNKNOWN" for r in rows)
    assert extra == 0
    assert q_dimensions.align_media(None, [])[0] == ()


def test_align_media_slot_count_follows_expected_not_hardcoded_three() -> None:
    rows, _ = q_dimensions.align_media(("a", "b", "c", "d"), [])
    assert len(rows) == 4 and {r.verdict for r in rows} == {"MISSING"}
    rows2, extra = q_dimensions.align_media(("a",), [_slot(0, "a"), _slot(1, "a"), _slot(7, "z")])
    assert len(rows2) == 1 and extra == 2


@pytest.mark.parametrize("paragraphs", [1, 2, 3, 5])
def test_expected_refs_match_media_rule_for_1_2_ge3_paragraphs(paragraphs: int) -> None:
    from rednote.modules.media.schemas import assign_paragraph_refs, paragraph_refs_for_body

    _title, body = _doc(0, paragraphs=paragraphs)
    para = media_bridge.split_body_into_paragraphs(body)
    assert len(para) == paragraphs
    expected = media_bridge.expected_refs_for(para)
    assert expected == assign_paragraph_refs(paragraph_refs_for_body(body))
    refs = [r for _i, _t, r in para]
    if paragraphs == 1:
        assert expected == (refs[0], refs[0], refs[0])
    elif paragraphs == 2:
        assert expected == (refs[0], refs[1], refs[1])
    else:
        assert expected == (refs[0], refs[1], refs[2])


def test_expected_refs_zero_paragraphs_is_fail_closed() -> None:
    with pytest.raises(media_bridge.SchemaViolation) as exc:
        media_bridge.expected_refs_for(())
    assert exc.value.reason == q_types.ALIGN_REASON_PARAGRAPH_REF_UNKNOWN


# ===========================================================================
# S. 结构性判据
# ===========================================================================


def _py_files() -> list[Path]:
    return sorted(p for p in SRC.glob("*.py"))


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)
    return mods


def test_no_paragraph_split_implementation_in_quality_ast() -> None:
    """R3：quality/** 不得出现 split / splitlines / paragraph_ref_for / parse_paragraph_ref 调用（AST，不是文本搜索）。"""
    forbidden = {"split", "splitlines", "paragraph_ref_for", "parse_paragraph_ref"}
    files = _py_files()
    assert len(files) >= 15
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                assert name not in forbidden, f"{path.name}:{node.lineno} 调用了 {name}"


def test_paragraph_split_impl_files_still_three() -> None:
    """本包不新增第 4 处切段实现：`check_paragraph_split_parity._IMPL_FILES` 仍是 3（判据 F：先注册再 exec）。"""
    path = REPO_ROOT / "tools/quality/check_paragraph_split_parity.py"
    name = "check_paragraph_split_parity_wp33_probe"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        assert len(module._IMPL_FILES) == 3
        assert not any("quality" in rel for rel in module._IMPL_FILES.values())
    finally:
        sys.modules.pop(name, None)


def test_pure_modules_import_only_types_and_stdlib_and_have_no_io() -> None:
    allowed = {
        "types.py": set(),
        "similarity.py": {"rednote.modules.quality.types"},
        "dimensions.py": {"rednote.modules.quality.types", "rednote.modules.quality.similarity"},
    }
    for filename, ok in allowed.items():
        path = SRC / filename
        rednote = {m for m in _imports(path) if m.startswith("rednote")}
        assert rednote <= ok, f"{filename} 引入了不允许的模块：{rednote - ok}"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                assert name not in {"now", "utcnow", "open", "random", "uuid4", "print", "execute"}, \
                    f"{filename}:{node.lineno} 不是纯函数：{name}"
        assert not {"datetime", "random", "os", "pathlib"} & {m.split(".")[0] for m in _imports(path)}


def test_non_bridge_modules_do_not_import_other_business_modules() -> None:
    """跨模块调用只走 *_bridge.py；其余文件只许 import 本包与 platform / identity / projects / files 依赖注入。"""
    bridges = {"content_bridge.py", "media_bridge.py", "versions_bridge.py", "files_bridge.py"}
    api_ok = ("rednote.modules.quality", "rednote.platform", "rednote.modules.identity.dependencies",
              "rednote.modules.projects", "rednote.modules.files.dependencies")
    for path in _py_files():
        if path.name in bridges:
            continue
        for module in (m for m in _imports(path) if m.startswith("rednote")):
            allowed = ("rednote.modules.quality", "rednote.platform")
            if path.name in {"api.py", "dependencies.py"}:
                allowed = api_ok
            assert module.startswith(allowed), f"{path.name} 越层 import {module}"


def test_bridges_are_forwarding_only_and_never_import_review() -> None:
    for path in _py_files():
        assert not any(m.startswith("rednote.modules.review") for m in _imports(path)), path.name
    assert {m for m in _imports(SRC / "media_bridge.py") if m.startswith("rednote.modules")} == {
        "rednote.modules.media.schemas"
    }


def test_no_float_or_numeric_columns() -> None:
    for name in OWN_TABLES:
        for column in Base.metadata.tables[name].columns:
            assert not isinstance(column.type, (Float, Numeric)), f"{name}.{column.name}"


def test_no_order_by_version_no_desc_in_sql_strings() -> None:
    """契约 §2：禁以 `ORDER BY version_no / revision_no DESC` 作「当前」口径（只扫代码里的字符串，不含 docstring）。"""
    pattern = re.compile(r"order\s+by[^'\"]*(version_no|revision_no)", re.IGNORECASE)
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                assert not pattern.search(node.value), f"{path.name}:{node.lineno}"
        text_ = path.read_text(encoding="utf-8")
        assert ".version_no.desc" not in text_.replace("`", "") or path.name == "content_bridge.py"


def test_placeholder_thresholds_live_in_one_place_and_are_labelled() -> None:
    values = {q_types.PLACEHOLDER_PAIR_THRESHOLD_PERMILLE, q_types.PLACEHOLDER_ALERT_THRESHOLD_PERMILLE}
    assert values == {950, 500}
    for path in _py_files():
        if path.name == "types.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and type(node.value) is int:
                assert node.value not in values, f"{path.name}:{node.lineno} 硬编码了占位阈值 {node.value}"
    assert "OP-228" in q_types.THRESHOLD_BASIS_PLACEHOLDER and "PLACEHOLDER" in q_types.THRESHOLD_BASIS_PLACEHOLDER
    assert "占位" in q_types.THRESHOLD_NOTE and "OP-228" in q_types.THRESHOLD_NOTE
    assert "产品口径" not in q_types.THRESHOLD_NOTE.replace("不是产品结论", "")
    assert "OP-228" in (SRC / "types.py").read_text(encoding="utf-8")


def test_ddl_trigger_inventory() -> None:
    items = q_ddl.all_trigger_ddl()
    assert len(items) == 13 and all(len(i) == 5 for i in items)
    assert len(q_ddl.trigger_names()) == 13 and len(set(q_ddl.trigger_names())) == 13
    assert len(q_ddl.OWN_FUNCTIONS) == 7 and "rednote_reject_mutation" not in q_ddl.OWN_FUNCTIONS
    assert q_ddl.consistency_function_names() == q_ddl.OWN_FUNCTIONS
    assert set(q_ddl.immutable_trigger_names()) == {f"trg_{t}_immutable" for t in IMMUTABLE_TABLES}
    assert q_ddl.CONSISTENCY_SQLSTATE == CHECK_VIOLATION
    for _t, fn, _tr, _dt, drop_fn in q_ddl.consistency_items():
        assert "%" not in fn  # psycopg 会把 % 当占位符
        assert drop_fn.startswith("DROP FUNCTION IF EXISTS rednote_check_quality_")


def test_models_match_ddl_draft_constraint_and_index_names() -> None:
    """与 `docs/m3/WP33-ddl-草案.py` 逐名对账（草案：CONSTRAINT / CREATE INDEX 名）：库里实际的名字集合 ⊇ 草案集合，差集显式列出。"""
    draft = DDL_DRAFT.read_text(encoding="utf-8")
    draft_names = set(re.findall(r"CONSTRAINT\s+([a-z0-9_]+)", draft)) | set(
        re.findall(r"CREATE INDEX\s+([a-z0-9_]+)", draft)
    )
    assert len(draft_names) >= 90
    engine = get_engine(_make_settings())
    with engine.connect() as conn:
        actual = {
            r[0]
            for r in conn.execute(
                text(
                    "select conname from pg_constraint where contype <> 't' and conrelid = any("
                    " select oid from pg_class where relname = any(:t))"
                ),
                {"t": list(OWN_TABLES)},
            )
        } | {
            r[0]
            for r in conn.execute(
                text("select indexname from pg_indexes where tablename = any(:t)"),
                {"t": list(OWN_TABLES)},
            )
        }
    assert all(len(n) <= 63 for n in actual)
    assert draft_names - actual == set(), f"草案里有、库里没有：{sorted(draft_names - actual)}"
    assert actual - draft_names == set(), f"库里有、草案没有：{sorted(actual - draft_names)}"


def test_open_set_columns_have_no_check_constraint() -> None:
    engine = get_engine(_make_settings())
    with engine.connect() as conn:
        defs = [
            (r[0], r[1])
            for r in conn.execute(
                text(
                    "select conname, pg_get_constraintdef(oid) from pg_constraint"
                    " where contype='c' and conrelid = any(select oid from pg_class where relname = any(:t))"
                ),
                {"t": list(OWN_TABLES)},
            )
        ]
    for column in ("not_computed_reason", "exclude_reason", "media_state"):
        for name, definition in defs:
            # reason_paired 只判 NULL 性，不枚举取值；任何 IN (...) 枚举都不应出现在这些列上
            assert not re.search(rf"{column}[^)]*(ANY|IN\s*\()", definition), (name, definition)
    alignment_reason = [d for n, d in defs if n == "ck_quality_media_alignments_reason_paired"]
    assert alignment_reason and "ANY" not in alignment_reason[0]


# ===========================================================================
# R. 数据库层
# ===========================================================================

DIM_REASONS = {
    "TITLE": "INSUFFICIENT_ITEMS",
    "OPENING": "INSUFFICIENT_ITEMS",
    "ENDING": "INSUFFICIENT_ITEMS",
    "BODY": "INSUFFICIENT_ITEMS",
    "STRUCTURE": "STRUCTURE_NOT_STRUCTURED",
    "IMAGE_SIMILARITY": "IMAGE_SIMILARITY_UNAVAILABLE",
    "SCENE_CONCENTRATION": "NO_SCENE_LINKAGE_IN_SCHEMA",
}


def _nc_dim(dimension: str) -> dict[str, Any]:
    return {"dimension": dimension, "status": "NOT_COMPUTED", "reason": DIM_REASONS[dimension],
            "verdict": None, "compared": None, "flagged": None, "pair": None, "alert": None, "max": None}


def _item_spec(c: Seeded, *, position: int, status: str = "INCLUDED", reason: str | None = None,
               version: Any = "default", paragraphs: int | None = 3) -> dict[str, Any]:
    return {
        "content_id": c.content_id, "position": position, "status": status, "reason": reason,
        "version": (c.version_ids[0] if c.version_ids else None) if version == "default" else version,
        "paragraphs": paragraphs,
    }


def _insert_graph(
    conn: Any, env: Env, *,
    items: list[dict[str, Any]],
    dims: list[dict[str, Any]] | None = None,
    pairs: list[dict[str, Any]] | None = None,
    aligns: list[dict[str, Any]] | None = None,
    version_kw: dict[str, Any] | None = None,
    node_kw: dict[str, Any] | None = None,
    projection: Any = "set",
    series_id: uuid.UUID | None = None,
    revision_no: int = 1,
) -> dict[str, Any]:
    """裸 SQL 造一套报告图（系列 + REPORT 节点 + 版本 + 子表 + 投影）。默认合法（7 维全 NOT_COMPUTED）。"""
    dims = dims if dims is not None else [_nc_dim(d) for d in DIMENSIONS]
    series_id = series_id or uuid.uuid4()
    if series_id and revision_no == 1 and not conn.execute(
        text("SELECT 1 FROM quality_reports WHERE id=:i"), {"i": series_id}
    ).first():
        conn.execute(
            text(
                "INSERT INTO quality_reports (id, project_id, scope_kind, scope_digest)"
                " VALUES (:i, :p, 'PROJECT', :d)"
            ),
            {"i": series_id, "p": env.project_id, "d": _hash(f"scope-{series_id}")},
        )
    version_id = uuid.uuid4()
    payload_hash = _hash(f"payload-{version_id}")
    _node(conn, entity_type="REPORT", project_id=env.project_id, entity_id=series_id,
          revision_no=revision_no, payload_hash=payload_hash, node_id=version_id,
          created_by=env.owner_id)
    included = sum(1 for i in items if i["status"] == "INCLUDED")
    ok = sum(1 for d in dims if d["verdict"] == "OK")
    alert = sum(1 for d in dims if d["verdict"] == "ALERT")
    nc = sum(1 for d in dims if d["status"] == "NOT_COMPUTED")
    version = {
        "id": version_id, "report_id": series_id, "revision_no": revision_no,
        "fp": _hash(f"fp-{version_id}"), "algo": 1, "schema": 1, "item_count": len(items),
        "inc": included, "exc": len(items) - included, "ok": ok, "alert": alert, "nc": nc,
        "outcome": "ALERTED" if alert else "NO_ALERT", "payload": "{}", "ph": payload_hash,
        "started": datetime.now(timezone.utc), "finished": datetime.now(timezone.utc),
    }
    version.update(version_kw or {})
    conn.execute(
        text(
            "INSERT INTO quality_report_versions (id, report_id, revision_no, input_fingerprint,"
            " algo_version, schema_version, item_count, included_item_count, excluded_item_count,"
            " ok_dimension_count, alert_dimension_count, not_computed_dimension_count, outcome,"
            " payload, payload_hash, started_at, finished_at)"
            " VALUES (:id, :report_id, :revision_no, :fp, :algo, :schema, :item_count, :inc, :exc,"
            " :ok, :alert, :nc, :outcome, CAST(:payload AS jsonb), :ph, :started, :finished)"
        ),
        version,
    )
    for spec in items:
        conn.execute(
            text(
                "INSERT INTO quality_report_items (report_version_id, content_item_id,"
                " content_version_id, position, status, exclude_reason, paragraph_count)"
                " VALUES (:v, :c, :cv, :pos, :st, :rs, :pc)"
            ),
            {"v": version_id, "c": spec["content_id"], "cv": spec["version"], "pos": spec["position"],
             "st": spec["status"], "rs": spec["reason"], "pc": spec["paragraphs"]},
        )
    for d in dims:
        conn.execute(
            text(
                "INSERT INTO quality_dimension_results (report_version_id, dimension, status,"
                " not_computed_reason, verdict, compared_item_count, flagged_item_count,"
                " pair_threshold_permille, alert_threshold_permille, max_similarity_permille)"
                " VALUES (:v, :d, :s, :r, :vd, :c, :f, :p, :a, :m)"
            ),
            {"v": version_id, "d": d["dimension"], "s": d["status"], "r": d["reason"],
             "vd": d["verdict"], "c": d["compared"], "f": d["flagged"], "p": d["pair"],
             "a": d["alert"], "m": d["max"]},
        )
    for p in pairs or []:
        conn.execute(
            text(
                "INSERT INTO quality_similarity_pairs (report_version_id, dimension, content_item_a_id,"
                " content_item_b_id, intersection_count, union_count, similarity_permille)"
                " VALUES (:v, :d, :a, :b, :i, :u, :s)"
            ),
            {"v": version_id, "d": p["dimension"], "a": p["a"], "b": p["b"], "i": p["i"], "u": p["u"],
             "s": p["i"] * 1000 // p["u"]},
        )
    for a in aligns or []:
        conn.execute(
            text(
                "INSERT INTO quality_media_alignments (report_version_id, content_item_id, sort_order,"
                " media_item_id, media_state, expected_ref, actual_ref, verdict, reason)"
                " VALUES (:v, :c, :so, :m, :ms, :e, :ac, :vd, :r)"
            ),
            {"v": version_id, "c": a["content_id"], "so": a["sort_order"], "m": a.get("media"),
             "ms": a.get("state"), "e": a.get("expected"), "ac": a.get("actual"),
             "vd": a["verdict"], "r": a.get("reason")},
        )
    if projection == "set":
        conn.execute(
            text("UPDATE quality_reports SET current_version_id=:v, current_revision_no=:r WHERE id=:i"),
            {"v": version_id, "r": revision_no, "i": series_id},
        )
    return {"series_id": series_id, "version_id": version_id}


def _commit_expecting(env: Env, build: Any, sqlstate: str | None) -> None:
    """单事务 + SAVEPOINT：建图后 `SET CONSTRAINTS ALL IMMEDIATE` 强制延迟约束；sqlstate=None ⇒ 期望通过。"""
    with session_scope(env.settings) as session:
        nested = session.begin_nested()
        if sqlstate is None:
            build(session.connection())
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        else:
            with pytest.raises(DBAPIError) as exc:
                build(session.connection())
                session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            assert _sqlstate(exc.value) == sqlstate, (_sqlstate(exc.value), str(exc.value)[:300])
        nested.rollback()


def _two(env: Env) -> tuple[Seeded, Seeded]:
    return _seed_content(env, seed=0), _seed_content(env, seed=1000)


def test_db_valid_graph_commits_positive_control(env: Env) -> None:
    a, b = _two(env)
    _commit_expecting(
        env, lambda conn: _insert_graph(conn, env, items=[_item_spec(a, position=0), _item_spec(b, position=1)]),
        None,
    )


def test_db_unique_scope_per_project_and_revision(env: Env) -> None:
    a, b = _two(env)
    digest = _hash("same-scope")
    with session_scope(env.settings) as session:
        conn = session.connection()
        session.execute(text("INSERT INTO quality_reports (project_id, scope_kind, scope_digest)"
                          " VALUES (:p,'PROJECT',:d)"), {"p": env.project_id, "d": digest})
        nested = session.begin_nested()
        with pytest.raises(DBAPIError) as exc:
            session.execute(text("INSERT INTO quality_reports (project_id, scope_kind, scope_digest)"
                              " VALUES (:p,'PROJECT',:d)"), {"p": env.project_id, "d": digest})
        assert _sqlstate(exc.value) == "23505"
        assert exc.value.orig.diag.constraint_name == "uq_quality_reports_project_id_scope_digest"  # type: ignore[union-attr]
        nested.rollback()
        # 另一个项目同摘要合法
        session.execute(text("INSERT INTO quality_reports (project_id, scope_kind, scope_digest)"
                          " VALUES (:p,'PROJECT',:d)"), {"p": env.other_project_id, "d": digest})


@pytest.mark.parametrize(
    ("values", "name"),
    [
        ({"scope_kind": "WEEKLY", "batch_id": None}, "ck_quality_reports_scope_kind_valid"),
        ({"scope_kind": "BATCH", "batch_id": None}, "ck_quality_reports_batch_id_paired"),
        ({"scope_kind": "PROJECT", "batch_id": uuid.uuid4()}, "ck_quality_reports_batch_id_paired"),
        ({"scope_kind": "PROJECT", "batch_id": None, "digest": "short"}, "ck_quality_reports_scope_digest_length"),
        ({"scope_kind": "PROJECT", "batch_id": None, "rev": 1}, "ck_quality_reports_projection_paired"),
        ({"scope_kind": "PROJECT", "batch_id": None, "rev": -1, "cv": uuid.uuid4()}, "ck_quality_reports_revision_no_non_negative"),
        ({"scope_kind": "PROJECT", "batch_id": None, "lock": 0}, "ck_quality_reports_lock_version_positive"),
    ],
)
def test_db_quality_reports_checks(env: Env, values: dict[str, Any], name: str) -> None:
    params = {"p": env.project_id, "k": values["scope_kind"], "d": values.get("digest", _hash("x")),
              "b": values["batch_id"], "r": values.get("rev", 0), "l": values.get("lock", 1),
              "cv": values.get("cv")}
    with session_scope(env.settings) as session:
        nested = session.begin_nested()
        with pytest.raises(DBAPIError) as exc:
            session.execute(
                text("INSERT INTO quality_reports (project_id, scope_kind, scope_digest, batch_id,"
                     " current_revision_no, lock_version, current_version_id)"
                     " VALUES (:p,:k,:d,:b,:r,:l,:cv)"), params)
        assert _sqlstate(exc.value) == CHECK_VIOLATION
        assert exc.value.orig.diag.constraint_name == name  # type: ignore[union-attr]
        nested.rollback()


def test_db_batch_scope_batch_id_has_no_foreign_key(env: Env) -> None:
    """D2：`batch_id` 是无 FK 的审计指针（指向不存在的批次也能写；一致性由服务层核对）。"""
    with session_scope(env.settings) as session:
        session.execute(
            text("INSERT INTO quality_reports (project_id, scope_kind, scope_digest, batch_id)"
                 " VALUES (:p,'BATCH',:d,:b)"),
            {"p": env.project_id, "d": _hash("b"), "b": uuid.uuid4()})
    engine = get_engine(_make_settings())
    with engine.connect() as conn:
        targets = {r[0] for r in conn.execute(text(
            "select confrelid::regclass::text from pg_constraint where contype='f'"
            " and conrelid = any(select oid from pg_class where relname = any(:t))"), {"t": list(OWN_TABLES)})}
    assert not {"batches", "batch_items"} & targets
    assert not any(t.startswith(("self_check", "trial_", "plan_drafts")) for t in targets)


def _probe(env: Env, sql: str, params: dict[str, Any], sqlstate: str, name: str | None = None) -> None:
    with session_scope(env.settings) as session:
        nested = session.begin_nested()
        with pytest.raises(DBAPIError) as exc:
            session.execute(text(sql), params)
        assert _sqlstate(exc.value) == sqlstate, str(exc.value)[:300]
        if name:
            assert exc.value.orig.diag.constraint_name == name  # type: ignore[union-attr]
        nested.rollback()


def _dim_insert_params(**over: Any) -> dict[str, Any]:
    base = {"v": uuid.uuid4(), "d": "TITLE", "s": "COMPUTED", "r": None, "vd": "OK", "c": 3, "f": 0,
            "p": 950, "a": 500, "m": 100}
    base.update(over)
    return base


_DIM_INSERT = (
    "INSERT INTO quality_dimension_results (report_version_id, dimension, status, not_computed_reason,"
    " verdict, compared_item_count, flagged_item_count, pair_threshold_permille,"
    " alert_threshold_permille, max_similarity_permille) VALUES (:v,:d,:s,:r,:vd,:c,:f,:p,:a,:m)"
)


@pytest.mark.parametrize(
    ("over", "name"),
    [
        ({"c": None}, "ck_quality_dimension_results_computed_columns_present"),          # COMPUTED 但 compared NULL
        ({"vd": None}, "ck_quality_dimension_results_computed_columns_present"),         # COMPUTED 但 verdict NULL
        ({"s": "NOT_COMPUTED", "r": "X", "vd": "OK"}, "ck_quality_dimension_results_computed_columns_present"),
        ({"s": "NOT_COMPUTED", "r": "X", "vd": None, "c": None, "f": None, "p": None, "a": None,
          "m": 0}, "ck_quality_dimension_results_not_computed_columns_absent"),          # NOT_COMPUTED 却有 0
        ({"r": "should-be-null"}, "ck_quality_dimension_results_reason_paired"),
        ({"s": "NOT_COMPUTED", "r": None, "vd": None, "c": None, "f": None, "p": None, "a": None, "m": None},
         "ck_quality_dimension_results_reason_paired"),                                   # 没算却没有原因
        ({"c": 1, "f": 0}, "ck_quality_dimension_results_compared_at_least_two"),
        ({"f": 4}, "ck_quality_dimension_results_flagged_range"),
        ({"f": 1, "vd": "OK"}, "ck_quality_dimension_results_flagged_not_one"),
        ({"p": 1001}, "ck_quality_dimension_results_thresholds_range"),
        ({"m": 1001}, "ck_quality_dimension_results_max_similarity_range"),
        ({"vd": "ALERT", "f": 0}, "ck_quality_dimension_results_verdict_alert_equivalence"),
        ({"vd": "OK", "f": 3, "c": 3}, "ck_quality_dimension_results_verdict_alert_equivalence"),
        ({"d": "STRUCTURE"}, "ck_quality_dimension_results_unimplemented_not_computed"),
        ({"d": "SIMILARITY"}, "ck_quality_dimension_results_dimension_valid"),
        ({"vd": "PASSED"}, "ck_quality_dimension_results_verdict_valid"),
    ],
)
def test_db_dimension_results_null_zero_separation_checks(env: Env, over: dict[str, Any], name: str) -> None:
    """NULL / 0 分离的双向等价式：矛盾组合一律 23514（CHECK 先于外键触发，故版本 id 可随机）。"""
    _probe(env, _DIM_INSERT, _dim_insert_params(**over), CHECK_VIOLATION, name)


def test_db_dimension_verdict_equivalence_accepts_boundary(env: Env) -> None:
    """flagged*1000 == alert*compared ⇒ OK 合法（严格大于）；多一篇 ⇒ ALERT 合法。"""
    a, b = _two(env)
    ok_dims = [_nc_dim(d) for d in DIMENSIONS]
    ok_dims[0] = {"dimension": "TITLE", "status": "COMPUTED", "reason": None, "verdict": "OK",
                  "compared": 4, "flagged": 2, "pair": 950, "alert": 500, "max": 1000}

    def build(conn: Any) -> None:
        _insert_graph(conn, env, items=[_item_spec(a, position=0), _item_spec(b, position=1)],
                      dims=ok_dims,
                      pairs=[{"dimension": "TITLE", "a": min(a.content_id, b.content_id),
                              "b": max(a.content_id, b.content_id), "i": 5, "u": 5}])

    _commit_expecting(env, build, None)


@pytest.mark.parametrize(
    ("over", "name"),
    [
        ({"outcome": "PASSED"}, "ck_quality_report_versions_outcome_valid"),
        ({"alert": 1, "nc": 6, "outcome": "NO_ALERT"}, "ck_quality_report_versions_outcome_alerted_equivalence"),
        ({"alert": 0, "outcome": "ALERTED"}, "ck_quality_report_versions_outcome_alerted_equivalence"),
        ({"ok": 1, "nc": 7}, "ck_quality_report_versions_dimension_count_sum"),
        ({"item_count": 5}, "ck_quality_report_versions_item_count_sum"),
        ({"revision_no": 0}, "ck_quality_report_versions_revision_no_positive"),
        ({"fp": "short"}, "ck_quality_report_versions_fingerprint_length"),
        ({"ph": "short"}, "ck_quality_report_versions_payload_hash_length"),
        ({"payload": "[]"}, "ck_quality_report_versions_payload_is_object"),
        ({"algo": 0}, "ck_quality_report_versions_algo_version_positive"),
    ],
)
def test_db_report_version_checks(env: Env, over: dict[str, Any], name: str) -> None:
    base = {"id": uuid.uuid4(), "report_id": uuid.uuid4(), "revision_no": 1, "fp": "f" * 64, "algo": 1,
            "schema": 1, "item_count": 2, "inc": 2, "exc": 0, "ok": 0, "alert": 0, "nc": 7,
            "outcome": "NO_ALERT", "payload": "{}", "ph": "p" * 64,
            "started": datetime.now(timezone.utc), "finished": datetime.now(timezone.utc)}
    base.update(over)
    _probe(
        env,
        "INSERT INTO quality_report_versions (id, report_id, revision_no, input_fingerprint,"
        " algo_version, schema_version, item_count, included_item_count, excluded_item_count,"
        " ok_dimension_count, alert_dimension_count, not_computed_dimension_count, outcome,"
        " payload, payload_hash, started_at, finished_at)"
        " VALUES (:id,:report_id,:revision_no,:fp,:algo,:schema,:item_count,:inc,:exc,:ok,:alert,:nc,"
        ":outcome,CAST(:payload AS jsonb),:ph,:started,:finished)",
        base, CHECK_VIOLATION, name,
    )


def test_db_report_version_has_no_fingerprint_unique_constraint(env: Env) -> None:
    """A→B→A 需要第 3 版：指纹不加唯一约束。"""
    names = {r[0] for r in _rows(env, "select conname from pg_constraint where conrelid = 'quality_report_versions'::regclass")}
    assert not [n for n in names if "fingerprint" in n and n.startswith("uq_")]
    idx = [r[0] for r in _rows(env, "select indexdef from pg_indexes where tablename='quality_report_versions'")]
    assert not [d for d in idx if "UNIQUE" in d and "input_fingerprint" in d]


@pytest.mark.parametrize(
    ("over", "name"),
    [
        ({"st": "INCLUDED", "rs": "X"}, "ck_quality_report_items_exclude_reason_paired"),
        ({"st": "EXCLUDED", "rs": None, "cv": None, "pc": None}, "ck_quality_report_items_exclude_reason_paired"),
        ({"st": "INCLUDED", "rs": None, "cv": None}, "ck_quality_report_items_included_has_subject"),
        ({"st": "INCLUDED", "rs": None, "pc": 0}, "ck_quality_report_items_included_has_subject"),
        ({"st": "INCLUDED", "rs": None, "pc": None}, "ck_quality_report_items_included_has_subject"),
        ({"st": "SKIPPED", "rs": None}, "ck_quality_report_items_status_valid"),
        ({"pos": -1}, "ck_quality_report_items_position_non_negative"),
    ],
)
def test_db_report_item_checks(env: Env, over: dict[str, Any], name: str) -> None:
    base = {"v": uuid.uuid4(), "c": uuid.uuid4(), "cv": uuid.uuid4(), "pos": 0, "st": "INCLUDED",
            "rs": None, "pc": 3}
    base.update(over)
    _probe(
        env,
        "INSERT INTO quality_report_items (report_version_id, content_item_id, content_version_id,"
        " position, status, exclude_reason, paragraph_count) VALUES (:v,:c,:cv,:pos,:st,:rs,:pc)",
        base, CHECK_VIOLATION, name,
    )


def test_db_null_vs_zero_paragraph_count_is_distinguished_in_a_real_report(env: Env) -> None:
    """NO_VERSION ⇒ paragraph_count NULL；EMPTY_BODY ⇒ 0；INCLUDED ⇒ ≥1。"""
    a, b = _two(env)
    nov = _seed_content(env, seed=2000, no_version=True)
    empty = _seed_content(env, seed=3000, body="  \n\n  ")
    _created, _s, vid = _generate(env, [a, b, nov, empty])
    rows = {r[0]: r for r in _rows(
        env, "SELECT content_item_id, status, exclude_reason, paragraph_count, content_version_id"
             " FROM quality_report_items WHERE report_version_id=:v", {"v": vid})}
    assert rows[nov.content_id][1:4] == ("EXCLUDED", "NO_VERSION", None) and rows[nov.content_id][4] is None
    assert rows[empty.content_id][1:4] == ("EXCLUDED", "EMPTY_BODY", 0) and rows[empty.content_id][4] is not None
    assert rows[a.content_id][1] == "INCLUDED" and rows[a.content_id][3] == 3


@pytest.mark.parametrize(
    ("over", "name"),
    [
        ({"a": "b", "b": "a"}, "ck_quality_similarity_pairs_ordered"),
        ({"i": 6, "u": 5}, "ck_quality_similarity_pairs_counts_valid"),
        ({"u": 0, "i": 0}, "ck_quality_similarity_pairs_counts_valid"),
        ({"s": 999}, "ck_quality_similarity_pairs_permille_derived"),
        ({"dim": "STRUCTURE"}, "ck_quality_similarity_pairs_dimension_is_text"),
    ],
)
def test_db_pair_checks(env: Env, over: dict[str, Any], name: str) -> None:
    lo, hi = sorted((uuid.uuid4(), uuid.uuid4()))
    base = {"v": uuid.uuid4(), "dim": "BODY", "a": lo, "b": hi, "i": 5, "u": 5, "s": 1000}
    if over.get("a") == "b":
        base["a"], base["b"] = hi, lo
        over = {}
    base.update(over)
    _probe(
        env,
        "INSERT INTO quality_similarity_pairs (report_version_id, dimension, content_item_a_id,"
        " content_item_b_id, intersection_count, union_count, similarity_permille)"
        " VALUES (:v,:dim,:a,:b,:i,:u,:s)",
        base, CHECK_VIOLATION, name,
    )


@pytest.mark.parametrize(
    ("over", "name"),
    [
        # PG 按约束名字母序检查，故每个用例的数据只违反其目标约束（或在它之前不违反别的）。
        # 注：verdict_valid 被四条等价式蕴含，无法单独隔离，不设单独用例。
        ({"vd": "MISSING", "e": None}, "ck_quality_media_alignments_missing_equivalence"),
        ({"vd": "ALIGNED", "e": "r", "ac": "other"}, "ck_quality_media_alignments_aligned_equivalence"),
        ({"vd": "MISALIGNED", "m": None, "ms": None, "ac": None, "e": "r"},
         "ck_quality_media_alignments_misaligned_equivalence"),
        ({"vd": "MISSING", "m": None, "ms": None, "ac": None, "e": None},
         "ck_quality_media_alignments_not_evaluable_equivalence"),
        ({"vd": "ALIGNED", "ms": None}, "ck_quality_media_alignments_media_columns_paired"),
        ({"so": -1}, "ck_quality_media_alignments_sort_order_non_negative"),
        ({"vd": "NOT_EVALUABLE", "e": None, "reason": None}, "ck_quality_media_alignments_reason_paired"),
    ],
)
def test_db_alignment_checks(env: Env, over: dict[str, Any], name: str) -> None:
    base = {"v": uuid.uuid4(), "c": uuid.uuid4(), "so": 0, "m": uuid.uuid4(), "ms": "READY",
            "e": "r", "ac": "r", "vd": "ALIGNED", "reason": None}
    base.update(over)
    _probe(
        env,
        "INSERT INTO quality_media_alignments (report_version_id, content_item_id, sort_order,"
        " media_item_id, media_state, expected_ref, actual_ref, verdict, reason)"
        " VALUES (:v,:c,:so,:m,:ms,:e,:ac,:vd,:reason)",
        base, CHECK_VIOLATION, name,
    )


def test_db_alignment_has_no_expected_ref_unique_constraint() -> None:
    """契约 §4：多张落同一段合法 ⇒ 没有 (version, content, expected_ref) 唯一约束。"""
    for table in Base.metadata.tables.values():
        if table.name != "quality_media_alignments":
            continue
        for constraint in table.constraints:
            cols = {c.name for c in getattr(constraint, "columns", [])}
            assert not ({"expected_ref"} & cols and constraint.__class__.__name__ == "UniqueConstraint")


@pytest.mark.parametrize(
    ("over", "name"),
    [
        ({"fmt": "PDF"}, "ck_quality_report_artifacts_format_valid"),
        ({"sha": "short"}, "ck_quality_report_artifacts_sha256_length"),
        ({"size": -1}, "ck_quality_report_artifacts_byte_size_non_negative"),
    ],
)
def test_db_artifact_checks(env: Env, over: dict[str, Any], name: str) -> None:
    base = {"v": uuid.uuid4(), "fmt": "JSON", "f": uuid.uuid4(), "sha": "a" * 64, "size": 1}
    base.update(over)
    _probe(
        env,
        "INSERT INTO quality_report_artifacts (report_version_id, format, file_object_id,"
        " content_sha256, byte_size) VALUES (:v,:fmt,:f,:sha,:size)",
        base, CHECK_VIOLATION, name,
    )


def test_db_six_immutable_tables_reject_update_and_delete_with_23001(env: Env) -> None:
    contents = [_seed_content(env, seed=0), _seed_content(env, seed=0)]
    for c in contents:
        for i in range(3):
            _seed_media(env, c, sort_order=i, ref="p0-00000bad")
    _created, _s, vid = _generate(env, contents)
    # 导出一份产物，使 6 张表都有行
    from rednote.modules.files.storage import LocalStorageBackend as _L
    with session_scope(env.settings) as session:
        repo = QualityRepository(session)
        version = q_service.require_version(repo, vid)
        q_service.create_artifact(repo, _L(env.storage_root), version=version, project_id=env.project_id,
                                  fmt="JSON", actor_id=env.owner_id)
    for table in IMMUTABLE_TABLES:
        assert _rows(env, f"SELECT count(*) FROM {table}")[0][0] >= 1, table
        col = "id"
        for stmt in (f"UPDATE {table} SET created_at = now() WHERE {col} IS NOT NULL",
                     f"DELETE FROM {table} WHERE {col} IS NOT NULL"):
            with session_scope(env.settings) as session:
                nested = session.begin_nested()
                with pytest.raises(DBAPIError) as exc:
                    session.execute(text(stmt))
                assert _sqlstate(exc.value) == IMMUTABLE_RECORD_SQLSTATE, (table, stmt)
                nested.rollback()
    # quality_reports 是唯一可变表：投影之外也允许更新 updated_at
    _exec(env, "UPDATE quality_reports SET updated_at = now()")


def test_db_quality_reports_is_not_immutable_but_orm_guard_covers_the_other_six() -> None:
    triggers = {r for r in q_ddl.immutable_trigger_names()}
    assert "trg_quality_reports_immutable" not in triggers
    assert len(triggers) == 6


# --- 7 个延迟一致性触发器 ------------------------------------------------------


def test_trigger_projection_must_point_to_own_version_and_revision(env: Env) -> None:
    a, b = _two(env)
    items = [_item_spec(a, position=0), _item_spec(b, position=1)]
    first: dict[str, Any] = {}

    def build_other(conn: Any) -> None:
        g1 = _insert_graph(conn, env, items=items)
        g2 = _insert_graph(conn, env, items=items)
        # 把系列 1 的投影指向系列 2 的版本
        conn.execute(text("UPDATE quality_reports SET current_version_id=:v WHERE id=:i"),
                     {"v": g2["version_id"], "i": g1["series_id"]})
        first.update(g1)

    _commit_expecting(env, build_other, CHECK_VIOLATION)

    def build_revision(conn: Any) -> None:
        g = _insert_graph(conn, env, items=items)
        conn.execute(text("UPDATE quality_reports SET current_revision_no=5 WHERE id=:i"), {"i": g["series_id"]})

    _commit_expecting(env, build_revision, CHECK_VIOLATION)


def test_trigger_version_node_must_match_revision_and_hash(env: Env) -> None:
    """version_nodes 本身不可变（UPDATE 会 23001），所以用「版本行声称的值与节点不一致」构造。"""
    a, b = _two(env)
    items = [_item_spec(a, position=0), _item_spec(b, position=1)]
    _commit_expecting(env, lambda c: _insert_graph(c, env, items=items, version_kw={"ph": "9" * 64}), CHECK_VIOLATION)
    _commit_expecting(env, lambda c: _insert_graph(c, env, items=items, version_kw={"revision_no": 2}), CHECK_VIOLATION)


def test_trigger_report_node_must_have_paired_version_row_but_foreign_report_nodes_pass(env: Env) -> None:
    a, b = _two(env)
    series_id = uuid.uuid4()

    def orphan_of_ours(conn: Any) -> None:
        conn.execute(text("INSERT INTO quality_reports (id, project_id, scope_kind, scope_digest)"
                          " VALUES (:i,:p,'PROJECT',:d)"),
                     {"i": series_id, "p": env.project_id, "d": _hash("orphan")})
        _node(conn, entity_type="REPORT", project_id=env.project_id, entity_id=series_id)

    _commit_expecting(env, orphan_of_ours, CHECK_VIOLATION)

    def not_ours(conn: Any) -> None:
        # 不归本包管的 REPORT 节点（WP11 通用用例会注册）直接放行——跨包契约
        _node(conn, entity_type="REPORT", project_id=env.project_id, entity_id=uuid.uuid4())

    _commit_expecting(env, not_ours, None)


def test_trigger_version_aggregate_counts_must_match_children(env: Env) -> None:
    a, b = _two(env)
    items = [_item_spec(a, position=0), _item_spec(b, position=1)]
    # 声称 1 个 OK，但 7 个维度行其实全是 NOT_COMPUTED（CHECK 的求和仍成立：1+0+6=7）
    _commit_expecting(env, lambda c: _insert_graph(c, env, items=items, version_kw={"ok": 1, "nc": 6}),
                      CHECK_VIOLATION)
    # 声称 2 篇排除，但 items 全是 INCLUDED
    _commit_expecting(env, lambda c: _insert_graph(c, env, items=items, version_kw={"inc": 0, "exc": 2}),
                      CHECK_VIOLATION)
    # 维度行只有 6 条
    _commit_expecting(env, lambda c: _insert_graph(c, env, items=items, dims=[_nc_dim(d) for d in DIMENSIONS[:6]],
                                                   version_kw={"nc": 7}), CHECK_VIOLATION)


def test_trigger_item_version_and_project_ownership(env: Env) -> None:
    a, b = _two(env)
    foreign = _seed_content(env, seed=2000, project_id=env.other_project_id)
    wrong_version = _item_spec(a, position=0, version=b.version_ids[0])  # b 的版本挂在 a 上
    _commit_expecting(env, lambda c: _insert_graph(c, env, items=[wrong_version, _item_spec(b, position=1)]),
                      CHECK_VIOLATION)
    _commit_expecting(env, lambda c: _insert_graph(c, env, items=[_item_spec(a, position=0), _item_spec(foreign, position=1)]),
                      CHECK_VIOLATION)


def test_trigger_dimension_flagged_must_match_pairs(env: Env) -> None:
    a, b = _two(env)
    items = [_item_spec(a, position=0), _item_spec(b, position=1)]
    lo, hi = sorted((a.content_id, b.content_id))
    computed = {"dimension": "TITLE", "status": "COMPUTED", "reason": None, "verdict": "ALERT",
                "compared": 2, "flagged": 2, "pair": 950, "alert": 500, "max": 1000}
    dims = [computed] + [_nc_dim(d) for d in DIMENSIONS[1:]]
    # 声称被标记 2 篇，却没有任何相似对
    _commit_expecting(env, lambda c: _insert_graph(c, env, items=items, dims=dims), CHECK_VIOLATION)
    # 一致：有一对 ⇒ 2 篇
    _commit_expecting(
        env,
        lambda c: _insert_graph(c, env, items=items, dims=dims,
                                pairs=[{"dimension": "TITLE", "a": lo, "b": hi, "i": 5, "u": 5}]),
        None,
    )
    # flagged>0 但 max 低于单对阈值
    low = dict(computed, max=100)
    _commit_expecting(
        env,
        lambda c: _insert_graph(c, env, items=items, dims=[low] + dims[1:],
                                pairs=[{"dimension": "TITLE", "a": lo, "b": hi, "i": 5, "u": 5}]),
        CHECK_VIOLATION,
    )


def test_trigger_pair_requires_computed_dimension_threshold_and_included_members(env: Env) -> None:
    a, b = _two(env)
    excluded = _seed_content(env, seed=2000, no_version=True)
    lo, hi = sorted((a.content_id, b.content_id))
    items = [_item_spec(a, position=0), _item_spec(b, position=1)]
    # 1) 挂在 NOT_COMPUTED 维度上（没算 ≠ 没命中）
    _commit_expecting(
        env,
        lambda c: _insert_graph(c, env, items=items,
                                pairs=[{"dimension": "TITLE", "a": lo, "b": hi, "i": 5, "u": 5}]),
        CHECK_VIOLATION,
    )
    computed = {"dimension": "TITLE", "status": "COMPUTED", "reason": None, "verdict": "OK",
                "compared": 3, "flagged": 0, "pair": 950, "alert": 500, "max": 100}
    dims = [computed] + [_nc_dim(d) for d in DIMENSIONS[1:]]
    # 2) 相似度低于阈值（只存命中）：1/10 = 100‰ < 950
    _commit_expecting(
        env,
        lambda c: _insert_graph(c, env, items=items, dims=dims,
                                pairs=[{"dimension": "TITLE", "a": lo, "b": hi, "i": 1, "u": 10}]),
        CHECK_VIOLATION,
    )
    # 3) 涉及被排除的篇目
    ex_item = _item_spec(excluded, position=2, status="EXCLUDED", reason="NO_VERSION", version=None, paragraphs=None)
    lo2, hi2 = sorted((a.content_id, excluded.content_id))
    _commit_expecting(
        env,
        lambda c: _insert_graph(c, env, items=items + [ex_item], dims=dims,
                                pairs=[{"dimension": "TITLE", "a": lo2, "b": hi2, "i": 5, "u": 5}]),
        CHECK_VIOLATION,
    )


def test_fk_pair_members_must_belong_to_this_report(env: Env) -> None:
    """复合 FK：相似对的两篇必须是本报告版本的成员 ⇒ 23503（非延迟，语句级）。"""
    a, b = _two(env)
    stranger = _seed_content(env, seed=2000)
    lo, hi = sorted((a.content_id, stranger.content_id))
    computed = {"dimension": "TITLE", "status": "COMPUTED", "reason": None, "verdict": "OK",
                "compared": 3, "flagged": 0, "pair": 950, "alert": 500, "max": 100}
    dims = [computed] + [_nc_dim(d) for d in DIMENSIONS[1:]]
    _commit_expecting(
        env,
        lambda c: _insert_graph(c, env, items=[_item_spec(a, position=0), _item_spec(b, position=1)], dims=dims,
                                pairs=[{"dimension": "TITLE", "a": lo, "b": hi, "i": 5, "u": 5}]),
        FK_VIOLATION,
    )


def test_trigger_alignment_media_ownership_no_version_and_not_evaluable_state(env: Env) -> None:
    a, b = _two(env)
    nov = _seed_content(env, seed=2000, no_version=True)
    empty = _seed_content(env, seed=3000, body="  \n\n ")
    media_of_b = _seed_media(env, b, sort_order=0, ref="p0-0000000a")
    media_of_empty = _seed_media(env, empty, sort_order=0, ref="p0-0000000a")
    items = [_item_spec(a, position=0), _item_spec(b, position=1)]
    # 媒体位不属于该篇
    _commit_expecting(
        env,
        lambda c: _insert_graph(c, env, items=items, aligns=[
            {"content_id": a.content_id, "sort_order": 0, "media": media_of_b, "state": "READY",
             "expected": "r", "actual": "r", "verdict": "ALIGNED"}]),
        CHECK_VIOLATION,
    )
    # NO_VERSION 篇目不应有对位行
    ex = _item_spec(nov, position=2, status="EXCLUDED", reason="NO_VERSION", version=None, paragraphs=None)
    _commit_expecting(
        env,
        lambda c: _insert_graph(c, env, items=items + [ex], aligns=[
            {"content_id": nov.content_id, "sort_order": 0, "expected": "r", "verdict": "MISSING"}]),
        CHECK_VIOLATION,
    )
    # NOT_EVALUABLE 出现在 INCLUDED 篇目上
    _commit_expecting(
        env,
        lambda c: _insert_graph(c, env, items=items, aligns=[
            {"content_id": b.content_id, "sort_order": 0, "media": media_of_b, "state": "READY",
             "actual": "p0-0000000a", "verdict": "NOT_EVALUABLE", "reason": "PARAGRAPH_REF_UNKNOWN"}]),
        CHECK_VIOLATION,
    )
    # EMPTY_BODY 篇目上的 NOT_EVALUABLE 合法；ALIGNED 则矛盾
    ex_empty = _item_spec(empty, position=2, status="EXCLUDED", reason="EMPTY_BODY", paragraphs=0)
    _commit_expecting(
        env,
        lambda c: _insert_graph(c, env, items=items + [ex_empty], aligns=[
            {"content_id": empty.content_id, "sort_order": 0, "media": media_of_empty, "state": "PENDING",
             "actual": "p0-0000000a", "verdict": "NOT_EVALUABLE", "reason": "PARAGRAPH_REF_UNKNOWN"}]),
        None,
    )
    _commit_expecting(
        env,
        lambda c: _insert_graph(c, env, items=items + [ex_empty], aligns=[
            {"content_id": empty.content_id, "sort_order": 0, "media": media_of_empty, "state": "PENDING",
             "expected": "p0-0000000a", "actual": "p0-0000000a", "verdict": "ALIGNED"}]),
        CHECK_VIOLATION,
    )


def test_trigger_downgrade_scope_only_own_functions_exist_in_catalog(env: Env) -> None:
    funcs = {r[0] for r in _rows(env, "select proname from pg_proc where proname like 'rednote_check_quality_%'")}
    assert funcs == set(q_ddl.OWN_FUNCTIONS)
    trg = {r[0] for r in _rows(env, "select tgname from pg_trigger where not tgisinternal and tgname like 'trg_%quality%'"
                               " or tgname = 'trg_version_nodes_report_paired'")}
    assert set(q_ddl.trigger_names()) <= trg
    assert _rows(env, "select count(*) from pg_proc where proname='rednote_reject_mutation'")[0][0] == 1


# ===========================================================================
# V. 服务层
# ===========================================================================


def _three_with_pair(env: Env) -> list[Seeded]:
    return [_seed_content(env, seed=0), _seed_content(env, seed=0), _seed_content(env, seed=1000)]


def test_generate_persists_report_as_report_version_with_seven_dimensions(env: Env) -> None:
    contents = _three_with_pair(env)
    created, series_id, version_id = _generate(env, contents)
    assert created is True
    dims = _dims(env, version_id)
    assert set(dims) == set(DIMENSIONS)
    for d in ("TITLE", "OPENING", "ENDING", "BODY"):
        row = dims[d]
        assert row[1] == "COMPUTED" and row[2] is None
        assert (row[3], row[4], row[5]) == ("ALERT", 3, 2)          # 3 篇里 2 篇互相命中：2000 > 500*3
        assert (row[6], row[7], row[8]) == (950, 500, 1000)
    for d, reason in q_types.ALWAYS_NOT_COMPUTED.items():
        assert dims[d][1] == "NOT_COMPUTED" and dims[d][2] == reason
        assert dims[d][3:] == (None,) * 6                            # 没算：不是 0
    ver = _rows(env, "SELECT revision_no, outcome, ok_dimension_count, alert_dimension_count,"
                     " not_computed_dimension_count, included_item_count, excluded_item_count, algo_version,"
                     " schema_version FROM quality_report_versions WHERE id=:v", {"v": version_id})[0]
    assert ver == (1, "ALERTED", 0, 4, 3, 3, 0, 1, 1)
    # REPORT 节点：id 同时是 version_nodes.id，且 entity_id = 系列身份
    node = _rows(env, "SELECT entity_type, entity_id, revision_no, payload_hash FROM version_nodes WHERE id=:v",
                 {"v": version_id})[0]
    assert node[:3] == ("REPORT", series_id, 1)
    assert node[3] == _scalar(env, "SELECT payload_hash FROM quality_report_versions WHERE id=:v", {"v": version_id})
    proj = _rows(env, "SELECT current_version_id, current_revision_no, lock_version FROM quality_reports WHERE id=:s",
                 {"s": series_id})[0]
    assert proj == (version_id, 1, 2)
    pairs = _rows(env, "SELECT dimension, content_item_a_id, content_item_b_id, similarity_permille"
                       " FROM quality_similarity_pairs WHERE report_version_id=:v ORDER BY dimension", {"v": version_id})
    twins = sorted(c.content_id for c in contents[:2])
    assert [p[0] for p in pairs] == ["BODY", "ENDING", "OPENING", "TITLE"]
    assert all((p[1], p[2]) == tuple(twins) and p[3] == 1000 for p in pairs)


def test_generate_zero_pairs_is_computed_zero_not_missing(env: Env) -> None:
    contents = [_seed_content(env, seed=s) for s in (0, 1000, 2000)]
    _c, _s, version_id = _generate(env, contents)
    dims = _dims(env, version_id)
    for d in ("TITLE", "OPENING", "ENDING", "BODY"):
        assert dims[d][1] == "COMPUTED" and dims[d][3] == "OK" and dims[d][5] == 0 and dims[d][8] == 0
    ver = _rows(env, "SELECT outcome, ok_dimension_count, alert_dimension_count, not_computed_dimension_count"
                     " FROM quality_report_versions WHERE id=:v", {"v": version_id})[0]
    assert ver == ("NO_ALERT", 4, 0, 3)  # 3 个没算的与 4 个 OK 并列，不被读成通过
    assert _rows(env, "SELECT count(*) FROM quality_similarity_pairs WHERE report_version_id=:v", {"v": version_id})[0][0] == 0


def test_single_paragraph_bodies_have_no_ending_dimension_and_no_double_count(env: Env) -> None:
    contents = [_seed_content(env, seed=0, body=_doc(0, paragraphs=1)[1]) for _ in range(2)]
    _c, _s, version_id = _generate(env, contents)
    dims = _dims(env, version_id)
    assert dims["OPENING"][1] == "COMPUTED" and dims["OPENING"][4] == 2
    assert dims["ENDING"][1] == "NOT_COMPUTED" and dims["ENDING"][2] == "INSUFFICIENT_ITEMS"
    assert dims["ENDING"][4] is None
    # 混合：2 篇多段 + 1 篇单段 ⇒ ENDING 只比较多段那 2 篇
    mixed = [_seed_content(env, seed=0), _seed_content(env, seed=0),
             _seed_content(env, seed=0, body=_doc(0, paragraphs=1)[1])]
    _c, _s, v2 = _generate(env, mixed)
    assert _dims(env, v2)["ENDING"][4] == 2 and _dims(env, v2)["OPENING"][4] == 3


def test_replay_same_input_returns_existing_version_without_writing(env: Env) -> None:
    contents = _three_with_pair(env)
    created1, s1, v1 = _generate(env, contents)
    nodes_before = _scalar(env, "SELECT count(*) FROM version_nodes WHERE entity_type='REPORT' AND project_id=:p",
                           {"p": env.project_id})
    created2, s2, v2 = _generate(env, list(reversed(contents)))  # 成员顺序不影响系列身份
    assert (created1, created2) == (True, False) and s1 == s2 and v1 == v2
    assert _scalar(env, "SELECT count(*) FROM quality_report_versions WHERE report_id=:s", {"s": s1}) == 1
    assert _scalar(env, "SELECT count(*) FROM version_nodes WHERE entity_type='REPORT' AND project_id=:p",
                   {"p": env.project_id}) == nodes_before
    assert _scalar(env, "SELECT lock_version FROM quality_reports WHERE id=:s", {"s": s1}) == 2


def test_edit_makes_new_version_old_kept_stale_and_a_b_a_makes_third_version(env: Env) -> None:
    contents = _three_with_pair(env)
    _c, series_id, v1 = _generate(env, contents)
    fp1 = _scalar(env, "SELECT input_fingerprint FROM quality_report_versions WHERE id=:v", {"v": v1})
    victim = contents[0]
    new_title, new_body = _doc(4000)
    _add_version(env, victim, title=new_title, body=new_body)
    # 旧报告仍在，且其钉死的版本已不是当前（STALE）
    with session_scope(env.settings) as session:
        detail = q_service.get_series_detail(QualityRepository(session), q_service.require_series(QualityRepository(session), series_id))
    cv = detail["currentVersion"]
    states = {i["contentItemId"]: i["subjectState"] for i in cv["items"]}
    assert states[str(victim.content_id)] == "STALE" and cv["staleItemCount"] == 1
    assert [s for s in states.values()].count("CURRENT") == 2
    created, _s, v2 = _generate(env, contents)
    assert created is True and v2 != v1
    assert _scalar(env, "SELECT revision_no FROM quality_report_versions WHERE id=:v", {"v": v2}) == 2
    assert _scalar(env, "SELECT count(*) FROM quality_report_versions WHERE report_id=:s", {"s": series_id}) == 2
    assert _scalar(env, "SELECT current_version_id FROM quality_reports WHERE id=:s", {"s": series_id}) == v2
    # 回到原稿（A→B→A）：指纹与第 1 版相同，但当前版本是第 2 版 ⇒ 产生第 3 版，不被唯一约束挡
    _point_to(env, victim, 0)
    created3, _s, v3 = _generate(env, contents)
    assert created3 is True and v3 not in (v1, v2)
    assert _scalar(env, "SELECT input_fingerprint FROM quality_report_versions WHERE id=:v", {"v": v3}) == fp1
    assert _scalar(env, "SELECT revision_no FROM quality_report_versions WHERE id=:v", {"v": v3}) == 3
    # 历史版本一行不少
    assert _scalar(env, "SELECT count(*) FROM quality_report_items WHERE report_version_id=:v", {"v": v1}) == 3


def test_payload_hash_is_stable_for_same_input_across_two_series_runs(env: Env) -> None:
    """payload 无时间戳 / 随机量：同输入同摘要（用两条同内容、不同项目无关的方式：直接比对重算的 payload）。"""
    contents = _three_with_pair(env)
    _c, _s, v1 = _generate(env, contents)
    payload = _scalar(env, "SELECT payload FROM quality_report_versions WHERE id=:v", {"v": v1})
    flat = json.dumps(payload)
    assert "Z\"" not in flat and "T00" not in flat
    assert not re.search(r"\d{4}-\d{2}-\d{2}", flat)
    from rednote.modules.versions.service import canonical_payload_hash
    assert canonical_payload_hash(payload) == _scalar(env, "SELECT payload_hash FROM quality_report_versions WHERE id=:v", {"v": v1})
    assert payload["thresholdBasis"] == "PLACEHOLDER_PENDING_OP-228" and payload["algoVersion"] == 1


def test_current_version_is_projection_first_not_max_version_no(env: Env) -> None:
    """契约 §2：投影指向 v1、最大 version_no 是 v2 的回滚态 ⇒ 报告钉 v1（而 read_edit_target 会取 v2）；投影 NULL ⇒ 回落末版。"""
    from rednote.modules.content.repository import ContentRepository
    from rednote.modules.content.service import read_edit_target

    forked = _seed_content(env, seed=0)
    v2_title, v2_body = _doc(5000)
    v2 = _add_version(env, forked, title=v2_title, body=v2_body)
    _point_to(env, forked, 0)
    other = _seed_content(env, seed=0)
    with session_scope(env.settings) as session:
        # 反向验证：content 的 read_edit_target 按 version_no DESC，在分叉态取到 v2（所以本包不能直接用它）
        assert read_edit_target(ContentRepository(session), content_id=forked.content_id).current_version_id == v2
    _c, _s, vid = _generate(env, [forked, other])
    pinned = _scalar(env, "SELECT content_version_id FROM quality_report_items WHERE report_version_id=:v AND content_item_id=:c",
                     {"v": vid, "c": forked.content_id})
    assert pinned == forked.version_ids[0] != v2
    # 投影为 NULL ⇒ 回落末版
    fallback = _seed_content(env, seed=0)
    f2_title, f2_body = _doc(6000)
    f2 = _add_version(env, fallback, title=f2_title, body=f2_body)
    _point_to(env, fallback, None)
    _c, _s, vid2 = _generate(env, [fallback, other])
    assert _scalar(env, "SELECT content_version_id FROM quality_report_items WHERE report_version_id=:v AND content_item_id=:c",
                   {"v": vid2, "c": fallback.content_id}) == f2


def test_no_version_and_empty_body_are_excluded_not_zero_scored(env: Env) -> None:
    a, b = _two(env)
    nov = _seed_content(env, seed=2000, no_version=True)
    empty = _seed_content(env, seed=3000, body="  \n\n  ")
    _seed_media(env, empty, sort_order=0, ref="p0-0000000b")
    _seed_media(env, empty, sort_order=1, ref="p1-0000000c")
    created, _s, vid = _generate(env, [a, b, nov, empty])
    assert created
    ver = _rows(env, "SELECT item_count, included_item_count, excluded_item_count FROM quality_report_versions WHERE id=:v", {"v": vid})[0]
    assert ver == (4, 2, 2)
    # 被排除的篇目不出现在任何相似对 / 维度计数里
    dims = _dims(env, vid)
    assert dims["BODY"][4] == 2
    # NO_VERSION 无对位行；EMPTY_BODY 有图则只产出 NOT_EVALUABLE，且 expected_ref 为 NULL
    assert _scalar(env, "SELECT count(*) FROM quality_media_alignments WHERE report_version_id=:v AND content_item_id=:c",
                   {"v": vid, "c": nov.content_id}) == 0
    rows = _rows(env, "SELECT sort_order, verdict, expected_ref, reason, actual_ref FROM quality_media_alignments"
                      " WHERE report_version_id=:v AND content_item_id=:c ORDER BY sort_order", {"v": vid, "c": empty.content_id})
    assert rows == [(0, "NOT_EVALUABLE", None, "PARAGRAPH_REF_UNKNOWN", "p0-0000000b"),
                    (1, "NOT_EVALUABLE", None, "PARAGRAPH_REF_UNKNOWN", "p1-0000000c")]


def test_alignment_rows_follow_order_rule_and_do_not_assume_unique_refs(env: Env) -> None:
    two_para = _seed_content(env, seed=0, body=_doc(0, paragraphs=2)[1])
    three_para = _seed_content(env, seed=1000)
    r2, r3 = _refs_of(two_para.__dict__ and _doc(0, paragraphs=2)[1]), _refs_of(_doc(1000)[1])
    _seed_media(env, two_para, sort_order=0, ref=r2[0])
    _seed_media(env, two_para, sort_order=1, ref=r2[1])
    _seed_media(env, two_para, sort_order=2, ref=r2[1])          # 期望 (p0,p1,p1)：两张落同一段合法
    _seed_media(env, three_para, sort_order=0, ref=r3[0])
    _seed_media(env, three_para, sort_order=1, ref=r3[2])        # 指向真实存在的段，但不是顺序对位该指的那段
    _seed_media(env, three_para, sort_order=3, ref=r3[0])        # 多余图
    _c, _s, vid = _generate(env, [two_para, three_para])
    got2 = _rows(env, "SELECT sort_order, verdict, expected_ref FROM quality_media_alignments WHERE report_version_id=:v"
                      " AND content_item_id=:c ORDER BY sort_order", {"v": vid, "c": two_para.content_id})
    assert got2 == [(0, "ALIGNED", r2[0]), (1, "ALIGNED", r2[1]), (2, "ALIGNED", r2[1])]
    got3 = _rows(env, "SELECT sort_order, verdict FROM quality_media_alignments WHERE report_version_id=:v"
                      " AND content_item_id=:c ORDER BY sort_order", {"v": vid, "c": three_para.content_id})
    assert got3 == [(0, "ALIGNED"), (1, "MISALIGNED"), (2, "MISSING")]
    # WP30 的集合成员判据会放过它（r3[2] ∈ 当前段集合），WP33 的顺序对位判 MISALIGNED：两者都对
    assert r3[2] in set(r3)
    payload = _scalar(env, "SELECT payload FROM quality_report_versions WHERE id=:v", {"v": vid})
    assert payload["alignment"] == {"aligned": 4, "misaligned": 1, "missing": 1, "notEvaluable": 0, "extraMediaCount": 1}
    missing = _rows(env, "SELECT media_item_id, media_state, actual_ref FROM quality_media_alignments"
                         " WHERE report_version_id=:v AND verdict='MISSING'", {"v": vid})
    assert missing == [(None, None, None)]


def test_threshold_snapshot_is_not_rewritten_when_constants_change(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    contents = _three_with_pair(env)
    _c, series_id, v1 = _generate(env, contents)
    assert _dims(env, v1)["BODY"][6:8] == (950, 500)
    monkeypatch.setitem(q_types.THRESHOLDS, "BODY", Thresholds(900, 100))
    created, _s, v2 = _generate(env, contents)   # 文本没变，但阈值变了 ⇒ 指纹变 ⇒ 新版本
    assert created is True and v2 != v1
    assert _dims(env, v2)["BODY"][6:8] == (900, 100)
    assert _dims(env, v1)["BODY"][6:8] == (950, 500)                    # 历史行不被改写
    with session_scope(env.settings) as session:
        repo = QualityRepository(session)
        old = q_service.render_version_detail(repo, q_service.require_version(repo, v1))
    body_dim = next(d for d in old["dimensions"] if d["dimension"] == "BODY")
    assert (body_dim["pairThresholdPermille"], body_dim["alertThresholdPermille"]) == (950, 500)
    assert old["thresholdBasis"] == "PLACEHOLDER_PENDING_OP-228"
    assert _scalar(env, "SELECT count(*) FROM quality_report_versions WHERE report_id=:s", {"s": series_id}) == 2


def test_dependency_edges_e11_written_in_same_transaction(env: Env) -> None:
    contents = _three_with_pair(env)
    nov = _seed_content(env, seed=2000, no_version=True)   # 无版本 ⇒ 判空跳过，不让 link_dependency 抛 422
    _c, _s, vid = _generate(env, contents + [nov])
    edges = _rows(env, "SELECT dependency_version_id, purpose FROM version_dependencies WHERE parent_version_id=:v",
                  {"v": vid})
    assert {e[0] for e in edges} == {c.version_ids[0] for c in contents}
    assert {e[1] for e in edges} == {"REPORT_INPUT"} and len(edges) == 3


def test_dependency_edge_failure_rolls_back_everything(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    contents = _three_with_pair(env)
    calls = {"n": 0}
    real = versions_bridge.link

    def flaky(session: Any, **kw: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        real(session, **kw)

    monkeypatch.setattr(versions_bridge, "link", flaky)
    with pytest.raises(RuntimeError, match="boom"):
        _generate(env, contents)
    assert calls["n"] == 2
    for table in ("quality_reports", "quality_report_versions", "quality_report_items", "quality_dimension_results"):
        col = "project_id" if table == "quality_reports" else None
        if col:
            assert _scalar(env, f"SELECT count(*) FROM {table} WHERE project_id=:p", {"p": env.project_id}) == 0
    assert _scalar(env, "SELECT count(*) FROM version_nodes WHERE entity_type='REPORT' AND project_id=:p", {"p": env.project_id}) == 0
    assert _scalar(env, "SELECT count(*) FROM quality_report_versions v JOIN quality_report_items i ON i.report_version_id=v.id"
                        " WHERE i.content_item_id = ANY(:ids)", {"ids": [c.content_id for c in contents]}) == 0
    # 恢复后可正常生成（没有残留的半成品挡路）
    monkeypatch.setattr(versions_bridge, "link", real)
    created, _s, _v = _generate(env, contents)
    assert created is True


def test_projection_cas_miss_is_not_swallowed_and_rolls_back(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    contents = _three_with_pair(env)
    monkeypatch.setattr(QualityRepository, "advance_projection", lambda self, **kw: 0)
    with pytest.raises(RuntimeError, match="CAS"):
        _generate(env, contents)
    assert _scalar(env, "SELECT count(*) FROM quality_reports WHERE project_id=:p", {"p": env.project_id}) == 0
    assert _scalar(env, "SELECT count(*) FROM version_nodes WHERE entity_type='REPORT' AND project_id=:p", {"p": env.project_id}) == 0


def test_advance_projection_cas_semantics(env: Env) -> None:
    contents = _three_with_pair(env)
    _c, series_id, v1 = _generate(env, contents)
    with session_scope(env.settings) as session:
        repo = QualityRepository(session)
        assert repo.advance_projection(report_id=series_id, expected_lock_version=99, version_id=v1, revision_no=1) == 0
    assert _scalar(env, "SELECT lock_version FROM quality_reports WHERE id=:s", {"s": series_id}) == 2


def test_wp34_report_resolver_becomes_effective_once_tables_exist(env: Env) -> None:
    """WP34 的 REPORT 解析器是条件注册：本包表存在后自动生效，且 current 随新版本前进。"""
    from rednote.modules.versions.impact import currentness_bridge as cb

    contents = _three_with_pair(env)
    _c, _s, v1 = _generate(env, contents)
    with session_scope(env.settings) as session:
        assert cb.report_tables_exist(session) is True
        assert "REPORT" in cb.resolvable_types(session)
        assert cb.resolve_is_current(session, [(v1, "REPORT")]) == {v1: True}
    _add_version(env, contents[0], title=_doc(4000)[0], body=_doc(4000)[1])
    _c, _s, v2 = _generate(env, contents)
    with session_scope(env.settings) as session:
        assert cb.resolve_is_current(session, [(v1, "REPORT"), (v2, "REPORT")]) == {v1: False, v2: True}


def test_scope_errors_422_and_nothing_written(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = _two(env)
    foreign = _seed_content(env, seed=2000, project_id=env.other_project_id)

    def count() -> int:
        return _scalar(env, "SELECT count(*) FROM quality_reports WHERE project_id=:p", {"p": env.project_id})

    with pytest.raises(ApiError) as exc:
        _generate(env, [a, foreign])
    _api_error(exc, ErrorCode.VALIDATION_FAILED, "QUALITY_CONTENT_NOT_IN_PROJECT")
    assert exc.value.details["contentIds"] == [str(foreign.content_id)]
    with pytest.raises(ApiError) as exc:
        _generate(env, content_ids=[a.content_id, uuid.uuid4()])
    _api_error(exc, ErrorCode.VALIDATION_FAILED, "QUALITY_CONTENT_NOT_IN_PROJECT")
    with pytest.raises(ApiError) as exc:
        _generate(env, [a])
    _api_error(exc, ErrorCode.VALIDATION_FAILED, "QUALITY_SCOPE_EMPTY")
    with pytest.raises(ApiError) as exc:
        _generate(env, [a, a])  # 去重后只有 1 篇
    _api_error(exc, ErrorCode.VALIDATION_FAILED, "QUALITY_SCOPE_EMPTY")
    nov = _seed_content(env, seed=2000, no_version=True)
    with pytest.raises(ApiError) as exc:
        _generate(env, [a, nov])  # 可比篇数 < 2
    _api_error(exc, ErrorCode.VALIDATION_FAILED, "QUALITY_SCOPE_EMPTY")
    monkeypatch.setattr(q_service, "MAX_REPORT_ITEMS", 2)
    c3 = _seed_content(env, seed=3000)
    with pytest.raises(ApiError) as exc:
        _generate(env, [a, b, c3])
    _api_error(exc, ErrorCode.VALIDATION_FAILED, "QUALITY_SCOPE_TOO_LARGE")
    assert exc.value.details["limit"] == 2 and exc.value.details["count"] == 3
    for kw in ({"kind": "WEEKLY"}, {"kind": "PROJECT", "content_ids": [a.content_id]}):
        with session_scope(env.settings) as session:
            with pytest.raises(ApiError) as exc:
                q_service.generate_report(QualityRepository(session), project_id=env.project_id,
                                          scope=q_service.ScopeRequest(kind=kw["kind"], content_ids=tuple(kw.get("content_ids", [])) or None),
                                          actor_id=env.owner_id)
            _api_error(exc, ErrorCode.VALIDATION_FAILED, "QUALITY_SCOPE_INVALID")
    with session_scope(env.settings) as session:
        with pytest.raises(ApiError) as exc:
            q_service.generate_report(QualityRepository(session), project_id=env.project_id,
                                      scope=q_service.ScopeRequest(kind="CONTENT_SET"), actor_id=env.owner_id)
        _api_error(exc, ErrorCode.VALIDATION_FAILED, "QUALITY_SCOPE_INVALID")
    assert count() == 0
    assert _scalar(env, "SELECT count(*) FROM version_nodes WHERE entity_type='REPORT' AND project_id=:p", {"p": env.project_id}) == 0


def test_project_scope_includes_all_contents_with_no_version_excluded(env: Env) -> None:
    contents = _three_with_pair(env)
    nov = _seed_content(env, seed=2000, no_version=True)
    _seed_content(env, seed=0, project_id=env.other_project_id)   # 他项目的内容不入
    created, series_id, vid = _generate(env, kind="PROJECT")
    assert created
    items = _rows(env, "SELECT content_item_id, status, exclude_reason FROM quality_report_items WHERE report_version_id=:v", {"v": vid})
    assert {i[0] for i in items} == {c.content_id for c in contents} | {nov.content_id}
    assert [i for i in items if i[0] == nov.content_id][0][1:] == ("EXCLUDED", "NO_VERSION")
    row = _rows(env, "SELECT scope_kind, batch_id, scope_digest FROM quality_reports WHERE id=:s", {"s": series_id})[0]
    assert row[0] == "PROJECT" and row[1] is None
    assert row[2] == hashlib.sha256(f"PROJECT:{env.project_id}".encode()).hexdigest()
    with pytest.raises(ApiError) as exc:
        _generate(env, kind="PROJECT", project_id=env.closed_project_id)   # 空项目
    _api_error(exc, ErrorCode.VALIDATION_FAILED, "QUALITY_SCOPE_EMPTY")


def _make_batch(env: Env, contents: list[Seeded]) -> uuid.UUID:
    """借 WP32 的夹具造一个批次（plan / baseline 来自 `make_env`），并把成员按 ordinal 挂上。"""
    from test_batches_wp32 import _INSERT_BATCH, _raw_batch_params, make_env

    # WP32 的 make_env 每次读 REDNOTE_TEST_DATABASE_URL，缺省指向 rednote_test_wp32；
    # 必须临时切到本包库，否则批次落在别的库里，本包的 quality_reports 查不到它。
    prev = os.environ.get("REDNOTE_TEST_DATABASE_URL")
    os.environ["REDNOTE_TEST_DATABASE_URL"] = str(env.settings.database_url)
    try:
        benv = make_env()
    finally:
        if prev is None:
            os.environ.pop("REDNOTE_TEST_DATABASE_URL", None)
        else:
            os.environ["REDNOTE_TEST_DATABASE_URL"] = prev
    inner = Env(settings=benv.settings, storage_root=None, project_id=benv.project_id,
                closed_project_id=benv.project_id, other_project_id=env.other_project_id,
                owner_id=benv.owner_id, collab_id=benv.owner_id, outsider_id=env.outsider_id,
                admin_id=env.admin_id, baseline_id=benv.baseline_id)
    seeded = [_seed_content(inner, seed=c) for c in contents]  # type: ignore[arg-type]
    batch_id = uuid.uuid4()
    with session_scope(benv.settings) as session:
        session.execute(text(_INSERT_BATCH), _raw_batch_params(benv, id=batch_id, q=len(seeded)))
        for ordinal, c in enumerate(seeded, start=1):
            session.execute(
                text("INSERT INTO batch_items (batch_id, ordinal, content_item_id) VALUES (:b,:o,:c)"),
                {"b": batch_id, "o": ordinal, "c": c.content_id})
    _BATCH_CACHE[batch_id] = (inner, seeded)
    return batch_id


_BATCH_CACHE: dict[uuid.UUID, tuple[Env, list[Seeded]]] = {}


def test_batch_scope_members_come_from_batch_items_read_only(env: Env) -> None:
    batch_id = _make_batch(env, [0, 0, 1000])  # type: ignore[list-item]
    benv, seeded = _BATCH_CACHE[batch_id]
    created, series_id, vid = _generate(benv, kind="BATCH", batch_id=batch_id, project_id=benv.project_id)
    assert created
    row = _rows(benv, "SELECT scope_kind, batch_id, scope_digest FROM quality_reports WHERE id=:s", {"s": series_id})[0]
    assert row[0] == "BATCH" and row[1] == batch_id
    assert row[2] == hashlib.sha256(f"BATCH:{batch_id}".encode()).hexdigest()
    items = _rows(benv, "SELECT content_item_id, position FROM quality_report_items WHERE report_version_id=:v ORDER BY position", {"v": vid})
    assert [i[0] for i in items] == [c.content_id for c in seeded]     # 按 batch_items.ordinal
    created2, series2, v2 = _generate(benv, kind="BATCH", batch_id=batch_id, project_id=benv.project_id)
    assert (created2, series2, v2) == (False, series_id, vid)
    # 只读：没有任何 batches / batch_items 的写
    assert _scalar(benv, "SELECT count(*) FROM batch_items WHERE batch_id=:b", {"b": batch_id}) == 3


def test_batch_scope_rejects_other_project_or_unknown_batch_and_misuse(env: Env) -> None:
    batch_id = _make_batch(env, [0, 1000])  # type: ignore[list-item]
    benv, _seeded = _BATCH_CACHE[batch_id]
    with pytest.raises(ApiError) as exc:   # 批次属于别的项目
        _generate(env, kind="BATCH", batch_id=batch_id)
    _api_error(exc, ErrorCode.VALIDATION_FAILED, "QUALITY_BATCH_NOT_IN_PROJECT")
    with pytest.raises(ApiError) as exc:
        _generate(env, kind="BATCH", batch_id=uuid.uuid4())
    _api_error(exc, ErrorCode.VALIDATION_FAILED, "QUALITY_BATCH_NOT_IN_PROJECT")
    with session_scope(env.settings) as session:
        with pytest.raises(ApiError) as exc:
            q_service.generate_report(QualityRepository(session), project_id=env.project_id,
                                      scope=q_service.ScopeRequest(kind="BATCH"), actor_id=env.owner_id)
        _api_error(exc, ErrorCode.VALIDATION_FAILED, "QUALITY_SCOPE_INVALID")
        with pytest.raises(ApiError) as exc:
            q_service.generate_report(QualityRepository(session), project_id=env.project_id,
                                      scope=q_service.ScopeRequest(kind="PROJECT", batch_id=batch_id), actor_id=env.owner_id)
        _api_error(exc, ErrorCode.VALIDATION_FAILED, "QUALITY_SCOPE_INVALID")
    assert _scalar(env, "SELECT count(*) FROM quality_reports WHERE project_id=:p", {"p": env.project_id}) == 0


def test_concurrent_same_scope_same_input_yields_exactly_one_version(env: Env) -> None:
    contents = _three_with_pair(env)
    n = 6
    barrier = threading.Barrier(n)
    results: list[tuple[bool, uuid.UUID, uuid.UUID]] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=20)
            results.append(_generate(env, contents))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert len(results) == n
    assert sum(1 for r in results if r[0]) == 1               # 恰一个新建
    assert len({r[2] for r in results}) == 1 and len({r[1] for r in results}) == 1
    series_id = results[0][1]
    assert _scalar(env, "SELECT count(*) FROM quality_report_versions WHERE report_id=:s", {"s": series_id}) == 1
    assert _scalar(env, "SELECT count(*) FROM quality_reports WHERE project_id=:p", {"p": env.project_id}) == 1


def test_different_scopes_do_not_block_each_other(env: Env) -> None:
    a_set = _three_with_pair(env)
    b_set = [_seed_content(env, seed=s) for s in (0, 0, 1000)]
    # 线程 1：对 A 范围持有系列行锁（事务保持打开）
    ready, release = threading.Event(), threading.Event()

    def holder() -> None:
        with session_scope(env.settings) as session:
            repo = QualityRepository(session)
            members = sorted(c.content_id for c in a_set)
            digest = q_service.scope_digest(env.project_id, q_service.ScopeRequest(kind="CONTENT_SET", content_ids=tuple(members)), members)
            repo.ensure_series(project_id=env.project_id, scope_kind="CONTENT_SET", scope_digest=digest, batch_id=None, created_by=env.owner_id)
            repo.lock_series(project_id=env.project_id, scope_digest=digest)
            ready.set()
            release.wait(timeout=30)

    t = threading.Thread(target=holder)
    t.start()
    assert ready.wait(timeout=20)
    done: list[Any] = []
    worker = threading.Thread(target=lambda: done.append(_generate(env, b_set)))
    worker.start()
    worker.join(timeout=20)
    release.set()
    t.join(timeout=30)
    assert done and done[0][0] is True, "不同范围的生成被另一范围的行锁阻塞了"


def test_series_lock_serializes_same_scope(env: Env) -> None:
    """同范围：持锁期间的并发生成被挡住，释放后继续（证明串行点是系列行锁）。"""
    contents = _three_with_pair(env)
    members = sorted(c.content_id for c in contents)
    scope = q_service.ScopeRequest(kind="CONTENT_SET", content_ids=tuple(members))
    digest = q_service.scope_digest(env.project_id, scope, members)
    ready, release = threading.Event(), threading.Event()

    def holder() -> None:
        with session_scope(env.settings) as session:
            repo = QualityRepository(session)
            repo.ensure_series(project_id=env.project_id, scope_kind="CONTENT_SET", scope_digest=digest, batch_id=None, created_by=env.owner_id)
            repo.lock_series(project_id=env.project_id, scope_digest=digest)
            ready.set()
            release.wait(timeout=30)

    t = threading.Thread(target=holder)
    t.start()
    assert ready.wait(timeout=20)
    done: list[Any] = []
    worker = threading.Thread(target=lambda: done.append(_generate(env, contents)))
    worker.start()
    worker.join(timeout=0.8)
    assert worker.is_alive() and not done, "同范围生成没有被系列行锁串行"
    release.set()
    t.join(timeout=30)
    worker.join(timeout=30)
    assert done and done[0][0] is True


def test_traceability_context_and_baseline_are_pinned(env: Env) -> None:
    with_ctx = _seed_content(env, seed=0, with_context=True)
    plain = _seed_content(env, seed=1000)
    _c, _s, vid = _generate(env, [with_ctx, plain])
    rows = {r[0]: r[1:] for r in _rows(env, "SELECT content_item_id, generation_context_id, baseline_id FROM quality_report_items WHERE report_version_id=:v", {"v": vid})}
    assert rows[with_ctx.content_id] == (with_ctx.context_id, env.baseline_id)
    assert rows[plain.content_id] == (None, None)         # NULL = 无生成上下文，不是 0 / 空串


def test_artifact_is_idempotent_generated_purpose_and_unsupported_format_rejected(env: Env) -> None:
    contents = _three_with_pair(env)
    _c, series_id, vid = _generate(env, contents)
    storage = LocalStorageBackend(env.storage_root)
    with session_scope(env.settings) as session:
        repo = QualityRepository(session)
        version = q_service.require_version(repo, vid)
        first, created1 = q_service.create_artifact(repo, storage, version=version, project_id=env.project_id, fmt="JSON", actor_id=env.owner_id)
    with session_scope(env.settings) as session:
        repo = QualityRepository(session)
        version = q_service.require_version(repo, vid)
        again, created2 = q_service.create_artifact(repo, storage, version=version, project_id=env.project_id, fmt="JSON", actor_id=env.owner_id)
    assert (created1, created2) == (True, False) and first["id"] == again["id"]
    assert _scalar(env, "SELECT count(*) FROM quality_report_artifacts WHERE report_version_id=:v", {"v": vid}) == 1
    file_id = uuid.UUID(first["fileObjectId"])
    assert _scalar(env, "SELECT purpose FROM project_file_links WHERE file_id=:f", {"f": file_id}) == "GENERATED"
    assert _scalar(env, "SELECT count(*) FROM project_file_links WHERE project_id=:p AND purpose='RESEARCH'", {"p": env.project_id}) == 0
    assert first["byteSize"] > 0 and len(first["contentSha256"]) == 64
    with session_scope(env.settings) as session:
        repo = QualityRepository(session)
        with pytest.raises(ApiError) as exc:
            q_service.create_artifact(repo, storage, version=q_service.require_version(repo, vid), project_id=env.project_id, fmt="PDF", actor_id=env.owner_id)
        _api_error(exc, ErrorCode.VALIDATION_FAILED, "QUALITY_ARTIFACT_FORMAT_UNSUPPORTED")


def test_render_detail_lists_all_seven_dimensions_in_order_and_flags_approximate(env: Env) -> None:
    contents = _three_with_pair(env)
    _c, _s, vid = _generate(env, contents)
    with session_scope(env.settings) as session:
        repo = QualityRepository(session)
        detail = q_service.render_version_detail(repo, q_service.require_version(repo, vid))
    assert [d["dimension"] for d in detail["dimensions"]] == list(DIMENSIONS)
    assert all(d["approximate"] is True for d in detail["dimensions"])
    nc = [d for d in detail["dimensions"] if d["status"] == "NOT_COMPUTED"]
    assert {d["dimension"] for d in nc} == set(q_types.ALWAYS_NOT_COMPUTED)
    assert all(d["verdict"] is None and d["comparedItemCount"] is None for d in nc)
    assert [d["implemented"] for d in detail["dimensions"]] == [True] * 4 + [False] * 3
    assert detail["thresholdBasis"] == "PLACEHOLDER_PENDING_OP-228" and "占位" in detail["thresholdNote"]
    assert all(i["subjectState"] is None for i in detail["items"]) and detail["staleItemCount"] is None


def test_dimension_catalog_marks_placeholders() -> None:
    catalog = q_service.dimension_catalog()
    assert [c["dimension"] for c in catalog] == list(DIMENSIONS)
    assert [c["implemented"] for c in catalog] == [True] * 4 + [False] * 3
    for c in catalog[:4]:
        assert c["thresholdBasis"] == "PLACEHOLDER_PENDING_OP-228" and c["pairThresholdPermille"] == 950
    for c in catalog[4:]:
        assert c["pairThresholdPermille"] is None and c["notComputedReason"] is not None


def test_pairs_listing_orders_by_similarity_then_id_and_paginates_without_gaps(env: Env) -> None:
    from rednote.platform.pagination import PageParams

    contents = [_seed_content(env, seed=0) for _ in range(4)]      # 4 篇完全相同 ⇒ 6 对 × 4 维度 = 24
    _c, _s, vid = _generate(env, contents)
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        with session_scope(env.settings) as session:
            repo = QualityRepository(session)
            page = q_service.list_pairs(repo, version=q_service.require_version(repo, vid), dimension=None,
                                        page_params=PageParams(limit=7, cursor=cursor))
        seen.extend(i["id"] for i in page.items)
        pages += 1
        if not page.page.has_more:
            break
        cursor = page.page.next_cursor
        assert cursor
    assert len(seen) == len(set(seen)) == 24 and pages == 4
    with session_scope(env.settings) as session:
        repo = QualityRepository(session)
        only_body = q_service.list_pairs(repo, version=q_service.require_version(repo, vid), dimension="BODY",
                                         page_params=PageParams(limit=100))
    assert len(only_body.items) == 6 and {i["dimension"] for i in only_body.items} == {"BODY"}
