"""WP34 回退影响图 / 导出失效的判据：纯函数 / 数据库约束 / 服务 / 只读接缝 / 并发。

夹具纪律照 `test_exports_wp35.py` / `test_planning_wp31.py`：
· 私有库（`REDNOTE_TEST_DATABASE_URL`，缺省指向 WP34 私有库），上游表由迁移（`alembic upgrade head`）已就绪；
· 只重建本包 `export_validity_events` 一张表 + 2 个触发器（`create_all` + `ddl.all_trigger_ddl()`，不走迁移）；
· 每个用例新造 user / project / content，按项目隔离，不数全库行；
· 约束探针断言 sqlstate（被拒的结论对 ≠ 原因对）；
· 不依赖迁移种下的全局行（本包不读 `batch.limits` 等），所以在没有 GLOBAL 行的库里也能过。

API 层端到端见 `test_versions_wp34_api.py`。
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import json
import re
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from conftest import cross_package_foreign_keys, drop_own_tables, truncate_targets

# FK 目标表必须在 `create_all` 之前 import（否则 NoReferencedTableError）。
import rednote.modules.content.models  # noqa: F401
import rednote.modules.files.models  # noqa: F401
import rednote.modules.gateway.models  # noqa: F401
import rednote.modules.identity.models  # noqa: F401
import rednote.modules.jobs.models  # noqa: F401
import rednote.modules.media.models  # noqa: F401
import rednote.modules.projects.models  # noqa: F401
import rednote.modules.prompts.models  # noqa: F401
import rednote.modules.versions.models  # noqa: F401
import rednote.modules.exports.models  # noqa: F401

import os

from rednote.modules.exports import service as x_service
from rednote.modules.exports import types as x_types
from rednote.modules.exports.repository import ExportRepository
from rednote.modules.files.models import STORAGE_BACKEND_LOCAL
from rednote.modules.files.repository import FileRepository
from rednote.modules.files.service import store_generated_asset
from rednote.modules.files.storage import LocalStorageBackend
from rednote.modules.identity.models import ROLE_ADMIN, ROLE_OPERATOR
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.projects.models import PHASE_CLOSED, Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.modules.versions.impact import ddl as i_ddl
from rednote.modules.versions.impact import export_bridge, service
from rednote.modules.versions.impact import types as it
from rednote.modules.versions.impact.models import OWN_TABLES, ExportValidityEvent
from rednote.modules.versions.impact.repository import ImpactRepository
from rednote.modules.versions.impact.traversal import traverse_dependents
from rednote.modules.versions.repository import VersionRepository
from rednote.modules.versions.service import link_dependency
from rednote.platform.base import Base
from rednote.platform.configuration import Settings
from rednote.platform.database import get_engine, reset_engines, session_scope
from rednote.platform.errors import ApiError
from rednote.platform.immutability import IMMUTABLE_RECORD_SQLSTATE

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test_wp34"
TEST_DATABASE_URL_ENV = "REDNOTE_TEST_DATABASE_URL"

DROP_ORDER = ("export_validity_events",)
_TABLE_NAMES = tuple(OWN_TABLES)
IMPACT_DIR = Path(__file__).resolve().parent.parent / "src/rednote/modules/versions/impact"
DDL_DRAFT = Path(__file__).resolve().parents[3] / "docs/m3/WP34-ddl-草案.py"
PNG_HEAD = b"\x89PNG\r\n\x1a\n"
CHECK_VIOLATION = "23514"


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
# 建表夹具
# ===========================================================================


@pytest.fixture(scope="module", autouse=True)
def _ensure_own_tables() -> Iterator[None]:
    """只重建 `export_validity_events` + 2 个触发器；前后对账跨包外键集合不变。"""
    reset_engines()
    engine = get_engine(_make_settings())
    fks_before = cross_package_foreign_keys(engine, _TABLE_NAMES)
    dropped, kept = drop_own_tables(engine, DROP_ORDER)
    # 「除被跨包引用的表外应可删」：被引用的按引用方保留，其余必须真的删掉了。
    assert set(dropped) | set(kept) == set(DROP_ORDER)
    Base.metadata.create_all(
        engine, tables=[Base.metadata.tables[name] for name in OWN_TABLES]
    )
    fks_after = cross_package_foreign_keys(engine, _TABLE_NAMES)
    assert fks_after == fks_before, (
        f"夹具改变了跨包外键：before={sorted(fks_before)} after={sorted(fks_after)}"
        f"（dropped={dropped} kept={kept}）"
    )
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(truncate_targets(engine, DROP_ORDER))))
        for _t, func_ddl, trigger_ddl, drop_trigger, _df in i_ddl.all_trigger_ddl():
            conn.execute(text(drop_trigger))
            conn.execute(text(func_ddl))
            conn.execute(text(trigger_ddl))
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
    storage: LocalStorageBackend
    project_id: uuid.UUID
    closed_project_id: uuid.UUID
    other_project_id: uuid.UUID
    owner_id: uuid.UUID
    collab_id: uuid.UUID
    outsider_id: uuid.UUID
    admin_id: uuid.UUID


def _create_user(settings: Settings, *, role: str = ROLE_OPERATOR) -> uuid.UUID:
    with session_scope(settings) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name=f"WP34-{role}",
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
            project_code=f"WP34-{uuid.uuid4().hex[:8].upper()}",
            name="WP34 测试项目",
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


@pytest.fixture
def env(tmp_path: Path) -> Env:
    root = tmp_path / "storage"
    settings = _make_settings(root)
    owner = _create_user(settings)
    collab = _create_user(settings)
    return Env(
        settings=settings,
        storage=LocalStorageBackend(root),
        project_id=_seed_project(settings, owner_id=owner, collaborators=(collab,)),
        closed_project_id=_seed_project(settings, owner_id=owner, closed=True),
        other_project_id=_seed_project(settings, owner_id=owner),
        owner_id=owner,
        collab_id=collab,
        outsider_id=_create_user(settings),
        admin_id=_create_user(settings, role=ROLE_ADMIN),
    )


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _exec(env: Env, sql: str, params: dict[str, Any] | None = None) -> None:
    with session_scope(env.settings) as session:
        session.execute(text(sql), params or {})


def _rows(env: Env, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    with session_scope(env.settings) as session:
        return [tuple(r) for r in session.execute(text(sql), params or {}).fetchall()]


def _scalar(env: Env, sql: str, params: dict[str, Any] | None = None) -> Any:
    with session_scope(env.settings) as session:
        return session.execute(text(sql), params or {}).scalar_one_or_none()


def _node(
    conn: Any,
    *,
    entity_type: str,
    project_id: uuid.UUID | None,
    entity_id: uuid.UUID | None = None,
    revision_no: int = 1,
    created_by: uuid.UUID | None = None,
) -> uuid.UUID:
    node_id = uuid.uuid4()
    eid = entity_id or uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO version_nodes (id, entity_type, entity_id, project_id,"
            " revision_no, schema_version, payload_hash, created_by)"
            " VALUES (:id, :t, :e, :p, :r, 1, :h, :by)"
        ),
        {"id": node_id, "t": entity_type, "e": eid, "p": project_id,
         "r": revision_no, "h": _hash(f"{eid}-{revision_no}-{node_id}"), "by": created_by},
    )
    return node_id


def _new_node(env: Env, entity_type: str = "BRIEF", *, project_id: uuid.UUID | None = ...) -> uuid.UUID:  # type: ignore[assignment]
    pid = env.project_id if project_id is ... else project_id
    with session_scope(env.settings) as s:
        return _node(s.connection(), entity_type=entity_type, project_id=pid, created_by=env.owner_id)


def _edge(env: Env, parent: uuid.UUID, dependency: uuid.UUID, purpose: str = "TEST_EDGE") -> None:
    """裸 SQL 造边（环等脏数据只能这样造；service 的 `link_dependency` 会拒环）。"""
    _exec(
        env,
        "INSERT INTO version_dependencies (parent_version_id, dependency_version_id, purpose)"
        " VALUES (:p, :d, :u)",
        {"p": parent, "d": dependency, "u": purpose},
    )


@dataclass
class Seeded:
    content_id: uuid.UUID
    project_id: uuid.UUID
    version_ids: list[uuid.UUID] = field(default_factory=list)
    media_item_ids: list[uuid.UUID] = field(default_factory=list)
    media_version_ids: list[uuid.UUID] = field(default_factory=list)


def _insert_content_version(conn: Any, env: Env, c: Seeded, no: int) -> uuid.UUID:
    node = _node(
        conn, entity_type="CONTENT", project_id=c.project_id, entity_id=c.content_id,
        revision_no=no, created_by=env.owner_id,
    )
    conn.execute(
        text(
            "INSERT INTO content_versions (id, content_item_id, version_no, title, body,"
            " tags, bindings, payload, payload_hash, schema_version, created_by)"
            " VALUES (:id, :c, :no, :t, :b, '[]'::jsonb, '[]'::jsonb, '{}'::jsonb,"
            " :h, 1, :by)"
        ),
        {"id": node, "c": c.content_id, "no": no, "t": f"标题-v{no}", "b": f"正文-v{no}",
         "h": _hash(f"cv-{c.content_id}-{no}"), "by": env.owner_id},
    )
    c.version_ids.append(node)
    return node


def _seed_content(
    env: Env, *, project_id: uuid.UUID | None = None, versions: int = 1,
    point_to_version: int | None = None,
) -> Seeded:
    """造一条内容。`point_to_version=1` 且 `versions=2` ⇒ 回滚分叉（投影指向 v1，末版是 v2）。"""
    pid = project_id or env.project_id
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
        for no in range(1, versions + 1):
            _insert_content_version(conn, env, c, no)
        if versions:
            idx = (point_to_version or versions) - 1
            conn.execute(
                text("UPDATE content_items SET current_version_id = :v,"
                     " current_version_no = :n WHERE id = :c"),
                {"v": c.version_ids[idx], "n": idx + 1, "c": c.content_id},
            )
    return c


def _add_version(env: Env, c: Seeded, *, move: bool = True) -> uuid.UUID:
    with session_scope(env.settings) as session:
        no = len(c.version_ids) + 1
        vid = _insert_content_version(session.connection(), env, c, no)
        if move:
            session.execute(
                text("UPDATE content_items SET current_version_id = :v,"
                     " current_version_no = :n WHERE id = :c"),
                {"v": vid, "n": no, "c": c.content_id},
            )
    return vid


def _point_to(env: Env, c: Seeded, index: int) -> None:
    _exec(
        env,
        "UPDATE content_items SET current_version_id = :v, current_version_no = :n WHERE id = :c",
        {"v": c.version_ids[index], "n": index + 1, "c": c.content_id},
    )


def _store_png(env: Env, project_id: uuid.UUID) -> uuid.UUID:
    with session_scope(env.settings) as session:
        return store_generated_asset(
            FileRepository(session), env.storage, project_id=project_id,
            original_name="配图.png",
            content=PNG_HEAD + uuid.uuid4().bytes + b"rednote-wp34",
            mime_type="image/png", uploaded_by=env.owner_id,
        ).file_id


def _seed_media_slot(
    env: Env, c: Seeded, *, with_version: bool = True, sort_order: int = 0
) -> uuid.UUID:
    """造一个带 DELIVERY attempt 1 READY 的媒体位；`with_version=False` ⇒ 只有位没有版本。"""
    mid = uuid.uuid4()
    cover = _store_png(env, c.project_id)
    deliver = _store_png(env, c.project_id)
    with session_scope(env.settings) as session:
        conn = session.connection()
        conn.execute(
            text(
                "INSERT INTO media_items (id, project_id, content_item_id, paragraph_ref,"
                " sort_order, state, current_version_no, created_by, lock_version)"
                " VALUES (:id, :p, :c, 'p0-00000000', :so, 'PENDING', 0, :by, 0)"
            ),
            {"id": mid, "p": c.project_id, "c": c.content_id, "so": sort_order, "by": env.owner_id},
        )
        c.media_item_ids.append(mid)
        if not with_version:
            return mid
        ref_id = uuid.uuid4()
        conn.execute(
            text("INSERT INTO reference_assets (id, project_id, file_object_id, state,"
                 " product_model) VALUES (:id, :p, :f, 'ACTIVE', 'M-WP34')"),
            {"id": ref_id, "p": c.project_id, "f": cover},
        )
        node = _node(conn, entity_type="MEDIA", project_id=c.project_id, entity_id=mid,
                     created_by=env.owner_id)
        conn.execute(
            text(
                "INSERT INTO media_versions (id, media_item_id, version_no, paragraph_ref,"
                " sort_order, reference_bindings, image_file_id, prompt, payload,"
                " payload_hash, schema_version, created_by)"
                " VALUES (:id, :m, 1, 'p0-00000000', 0, CAST(:rb AS jsonb), :img, '提示词',"
                " '{}'::jsonb, :h, 1, :by)"
            ),
            {"id": node, "m": mid, "rb": json.dumps([{"assetId": str(ref_id)}]), "img": cover,
             "h": _hash(f"mv-{mid}"), "by": env.owner_id},
        )
        conn.execute(
            text("UPDATE media_items SET current_version_id = :v, current_version_no = 1"
                 " WHERE id = :m"),
            {"v": node, "m": mid},
        )
        c.media_version_ids.append(node)
        conn.execute(
            text(
                "INSERT INTO media_renditions (media_version_id, kind, attempt_no, state,"
                " file_object_id, width, height, byte_size, image_format)"
                " VALUES (:v, 'DELIVERY', 1, 'READY', :f, 100, 100, 64, 'png')"
            ),
            {"v": node, "f": deliver},
        )
    return mid


def _reprocess_delivery(env: Env, c: Seeded, attempt_no: int) -> None:
    """同一媒体版本的图被重新加工：追加更高 attempt_no 的 READY DELIVERY（新文件）。"""
    fid = _store_png(env, c.project_id)
    _exec(
        env,
        "INSERT INTO media_renditions (media_version_id, kind, attempt_no, state,"
        " file_object_id, width, height, byte_size, image_format)"
        " VALUES (:v, 'DELIVERY', :a, 'READY', :f, 100, 100, 64, 'png')",
        {"v": c.media_version_ids[0], "a": attempt_no, "f": fid},
    )


def _make_export(env: Env, contents: list[Seeded], *, project_id: uuid.UUID | None = None) -> uuid.UUID:
    """走真实 `create_export`，返回导出组 id。"""
    with session_scope(env.settings) as session:
        result = x_service.create_export(
            ExportRepository(session), FileRepository(session), env.storage,
            project_id=project_id or env.project_id,
            content_item_ids=[c.content_id for c in contents],
            export_format="STATIC", idempotency_key=None, created_by=env.owner_id,
        )
        return result.export_group_id


def _items_of(env: Env, group_id: uuid.UUID) -> list[uuid.UUID]:
    return [r[0] for r in _rows(
        env, "SELECT id FROM export_items WHERE export_group_id = :g ORDER BY id", {"g": group_id}
    )]


def _exported(env: Env, **kw: Any) -> tuple[Seeded, uuid.UUID, uuid.UUID]:
    """造内容并导出一次：返回 (内容, 导出组, 唯一导出行)。"""
    c = _seed_content(env, **kw)
    g = _make_export(env, [c], project_id=c.project_id)
    return c, g, _items_of(env, g)[0]


def _sweep(env: Env, *, contents: list[uuid.UUID] | None = None,
           from_version: uuid.UUID | None = None,
           project_id: uuid.UUID | None = None) -> dict[str, Any]:
    with session_scope(env.settings) as s:
        return service.sweep(
            ImpactRepository(s), project_id=project_id or env.project_id, actor_id=env.owner_id,
            content_item_ids=contents, from_version_id=from_version,
        )


def _events(env: Env, item_id: uuid.UUID) -> list[tuple[int, str, str | None]]:
    return [tuple(r) for r in _rows(
        env,
        "SELECT seq, transition, reason FROM export_validity_events"
        " WHERE export_item_id = :i ORDER BY seq", {"i": item_id},
    )]


def _event_count(env: Env) -> int:
    return _scalar(
        env,
        "SELECT count(*) FROM export_validity_events e JOIN export_items x ON x.id = e.export_item_id"
        " WHERE x.project_id = ANY(:ps)",
        {"ps": [env.project_id, env.other_project_id, env.closed_project_id]},
    )


def _api_error(fn: Any) -> ApiError:
    with pytest.raises(ApiError) as info:
        fn()
    return info.value


def _reason(exc: ApiError) -> str:
    return exc.details["reason"]


def _in_session(env: Env, fn: Any) -> Any:
    with session_scope(env.settings) as s:
        return fn(ImpactRepository(s))


def _probe(env: Env, statements: list[tuple[str, dict[str, Any]]]) -> tuple[str, str]:
    """单事务执行语句；返回 (sqlstate, 约束名)，全部通过返回 ("", "")。整体回滚，不留痕。"""
    engine = get_engine(env.settings)
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            for sql, params in statements:
                conn.execute(text(sql), params)
            return "", ""
        except DBAPIError as exc:
            orig = exc.orig
            return (
                getattr(orig, "sqlstate", "") or "",
                getattr(getattr(orig, "diag", None), "constraint_name", "") or "",
            )
        finally:
            trans.rollback()


# ===========================================================================
# T-GRAPH：纯函数（不连库）
# ===========================================================================


def _u(i: int) -> uuid.UUID:
    return uuid.UUID(int=i)


def _fetcher(*edges: tuple[int, int, str] | tuple[int, int], calls: list[int] | None = None):
    """edges = (parent, dependency[, purpose])。"""
    all_edges = [
        it.Edge(_u(e[0]), _u(e[1]), e[2] if len(e) > 2 else "P") for e in edges  # type: ignore[misc]
    ]

    def fetch(frontier: Any) -> list[it.Edge]:
        if calls is not None:
            calls.append(len(frontier))
        ids = set(frontier)
        return [e for e in all_edges if e.dependency in ids]

    return fetch


def _ids(result: it.TraversalResult) -> list[int]:
    return [n.version_id.int for n in result.nodes]


def test_graph_linear_chain_layers() -> None:
    r = traverse_dependents(_fetcher((2, 1), (3, 2)), _u(1))
    assert [(n.version_id.int, n.depth) for n in r.nodes] == [(2, 1), (3, 2)]
    assert r.complete and r.truncation is None and r.max_depth_reached == 2


def test_graph_diamond_node_once_with_shortest_depth() -> None:
    # 1 <- 2 <- 4,  1 <- 3 <- 4 以及 1 <- 4 直连：4 取最短深度 1
    r = traverse_dependents(_fetcher((2, 1), (3, 1), (4, 2), (4, 3), (4, 1)), _u(1))
    assert sorted(_ids(r)) == [2, 3, 4]
    assert {n.version_id.int: n.depth for n in r.nodes}[4] == 1
    assert r.anomalies == () and r.cycle_node_count == 0  # 菱形不误报环


def test_graph_fan_out_all_depth_one() -> None:
    r = traverse_dependents(_fetcher(*[(i, 1) for i in range(2, 8)]), _u(1))
    assert len(r.nodes) == 6 and {n.depth for n in r.nodes} == {1}


def test_graph_start_excluded_and_zero_dependents_is_empty_not_null() -> None:
    r = traverse_dependents(_fetcher(), _u(1))
    assert r.nodes == () and r.complete is True and r.max_depth_reached == 0
    r2 = traverse_dependents(_fetcher((2, 1), (3, 2)), _u(1))
    assert 1 not in _ids(r2)


def test_graph_direction_from_dependency_reaches_parent_not_reverse() -> None:
    fetch = _fetcher((5, 9))  # E(parent=5 -> dependency=9)
    assert _ids(traverse_dependents(fetch, _u(9))) == [5]
    assert traverse_dependents(fetch, _u(5)).nodes == ()


def test_graph_dirty_cycle_terminates_and_is_flagged_not_raised() -> None:
    r = traverse_dependents(_fetcher((2, 1), (1, 2)), _u(1))  # 1 <-> 2
    assert r.complete is True
    assert r.anomalies == (it.ANOMALY_CYCLE,) and r.cycle_node_count == 2
    assert _ids(r) == [2]


def test_graph_start_on_a_longer_cycle_is_reported() -> None:
    # 1 -> 2 -> 3 -> 1（parent -> dependency）
    r = traverse_dependents(_fetcher((1, 2), (2, 3), (3, 1)), _u(1))
    assert r.anomalies == (it.ANOMALY_CYCLE,) and r.cycle_node_count == 3


def test_graph_node_limit_boundary_and_deterministic_prefix() -> None:
    exact = traverse_dependents(_fetcher(*[(i, 1) for i in range(2, 5)]), _u(1), max_nodes=3)
    assert exact.complete and len(exact.nodes) == 3
    edges = [(i, 1) for i in range(2, 6)]
    over_a = traverse_dependents(_fetcher(*edges), _u(1), max_nodes=3)
    over_b = traverse_dependents(_fetcher(*edges), _u(1), max_nodes=3)
    assert over_a.complete is False and over_a.truncation == it.TRUNCATION_NODE_LIMIT
    assert _ids(over_a) == [2, 3, 4] == _ids(over_b)  # 前缀确定


def test_graph_depth_limit_boundary() -> None:
    chain3 = [(2, 1), (3, 2), (4, 3)]
    ok = traverse_dependents(_fetcher(*chain3), _u(1), max_depth=3)
    assert ok.complete and ok.max_depth_reached == 3
    over = traverse_dependents(_fetcher(*chain3, (5, 4)), _u(1), max_depth=3)
    assert over.complete is False and over.truncation == it.TRUNCATION_DEPTH_LIMIT
    assert _ids(over) == [2, 3, 4]


def test_graph_round_trips_bounded_by_depth_not_paths() -> None:
    edges: list[tuple[int, int]] = [(10, 1), (11, 1)]
    for layer in range(1, 9):  # 每层 2 个节点，层间 2x2 全连：路径数指数增长
        for a in (10 + 2 * layer, 11 + 2 * layer):
            for b in (10 + 2 * (layer - 1), 11 + 2 * (layer - 1)):
                edges.append((a, b))
    calls: list[int] = []
    r = traverse_dependents(_fetcher(*edges, calls=calls), _u(1))
    assert len(r.nodes) == 18 and r.complete
    assert r.round_trips == len(calls) <= it.MAX_TRAVERSAL_DEPTH + 1


def test_graph_purposes_sorted_deduped_and_not_filtered() -> None:
    r = traverse_dependents(
        _fetcher((2, 1, "B"), (2, 1, "A"), (2, 1, "B"), (3, 2, "REPORT_INPUT")), _u(1)
    )
    by = {n.version_id.int: n.purposes for n in r.nodes}
    assert by[2] == ("A", "B") and by[3] == ("REPORT_INPUT",)


def test_derive_decision_table() -> None:
    v, other = _u(1), _u(2)
    same = it.Observed(v, "a" * 64)
    assert it.derive(v, "a" * 64, same).state == it.STATE_VALID
    d = it.derive(v, "b" * 64, it.Observed(other, "a" * 64))
    assert (d.state, d.reason) == (it.STATE_INVALID, it.REASON_VERSION_SUPERSEDED)
    d = it.derive(v, "b" * 64, it.Observed(v, "a" * 64))
    assert (d.state, d.reason) == (it.STATE_INVALID, it.REASON_ASSET_CHANGED)
    d = it.derive(v, "b" * 64, it.Unavailable("NO_DELIVERY_ASSET", "x"))
    assert (d.state, d.reason, d.unknown_reason) == (it.STATE_UNKNOWN, None, "NO_DELIVERY_ASSET")


def test_value_domains_parity_with_ddl_draft_and_exports() -> None:
    spec = importlib.util.spec_from_file_location("wp34_ddl_draft", DDL_DRAFT)
    assert spec and spec.loader
    draft = importlib.util.module_from_spec(spec)
    # 判据 F：每个动态加载点都要在 exec 之前注册 sys.modules（dataclass / pickle 依赖它）。
    sys.modules[spec.name] = draft
    spec.loader.exec_module(draft)
    domains = draft.VALUE_DOMAINS
    assert set(domains["export_validity_events.transition"]) == it.TRANSITIONS
    assert set(domains["export_validity_events.reason"]) == it.EVENT_REASONS
    assert set(domains["export_validity_events.scope_kind"]) == it.SCOPE_KINDS
    assert set(draft.RESPONSE_ONLY_DOMAINS["validity_state"]) == {
        it.STATE_VALID, it.STATE_INVALID, it.STATE_UNKNOWN}
    # 平级不可互 import，各写一份，对账
    assert it.MAX_SWEEP_CONTENT_IDS == x_types.MAX_CONTENT_ITEMS_PER_EXPORT
    assert it.REASON_CONTENT_IDS_EMPTY == x_types.REASON_CONTENT_IDS_EMPTY
    assert it.REASON_CONTENT_IDS_TOO_MANY == x_types.REASON_CONTENT_IDS_TOO_MANY
    assert it.REASON_CONTENT_IDS_DUPLICATE == x_types.REASON_CONTENT_IDS_DUPLICATE
    assert it.REASON_CONTENT_NOT_FOUND == x_types.REASON_CONTENT_NOT_FOUND
    assert it.REASON_EXPORT_NOT_FOUND == x_types.REASON_EXPORT_NOT_FOUND
    assert it.SHA256_HEX_CHARS == x_types.SHA256_HEX_CHARS


# ===========================================================================
# 源码静态判据（T-START-04 / T-EXP-02 / T-REUSE-02）
# ===========================================================================


def _impact_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(IMPACT_DIR.glob("*.py"))}


def test_t_start_04_no_version_no_desc_or_max_revision_reads() -> None:
    for name, src in _impact_sources().items():
        assert not re.search(r"ORDER\s+BY\s+(\w+\.)?version_no", src, re.I), name
        assert not re.search(r"MAX\s*\(\s*(\w+\.)?version_no", src, re.I), name
        assert "max_revision_no" not in src, name


def test_t_exp_02_no_write_statement_against_export_items() -> None:
    pattern = re.compile(
        r"(UPDATE\s+export_items|DELETE\s+FROM\s+export_items|INSERT\s+INTO\s+export_items)", re.I
    )
    for name, src in _impact_sources().items():
        assert not pattern.search(src), name


def test_t_reuse_02_no_second_implementation_of_hash_or_renditions() -> None:
    for name, src in _impact_sources().items():
        assert "hashlib" not in src, name
        assert "sha256" not in src.lower().replace("sha256_hex_chars", ""), name
        assert "eligibility_hash(" not in src, name
        assert "media_renditions" not in src, name
        assert "freeze_content" not in src, name


def test_exports_is_imported_only_by_export_bridge() -> None:
    importers = {
        name for name, src in _impact_sources().items()
        if re.search(r"^\s*(from|import)\s+rednote\.modules\.exports", src, re.M)
    }
    assert importers == {"export_bridge.py"}


def test_pure_modules_do_not_import_other_modules() -> None:
    for name in ("types.py", "traversal.py"):
        src = _impact_sources()[name]
        for m in re.findall(r"^\s*(?:from|import)\s+(rednote\.modules\.[\w.]+)", src, re.M):
            assert m.startswith("rednote.modules.versions.impact"), (name, m)


def test_versions_root_does_not_import_impact() -> None:
    root = IMPACT_DIR.parent
    for p in root.glob("*.py"):
        assert "versions.impact" not in p.read_text(encoding="utf-8"), p.name


# ===========================================================================
# T-GRAPH-DB：真库
# ===========================================================================


def test_graph_db_batch_lookup_can_use_dependency_index(env: Env) -> None:
    root = _new_node(env)
    kids = [_new_node(env, "CONTENT") for _ in range(3)]
    for k in kids:
        _edge(env, k, root)
    with session_scope(env.settings) as s:
        s.execute(text("SET LOCAL enable_seqscan = off"))
        s.execute(text("SET LOCAL enable_bitmapscan = on"))
        from rednote.modules.versions.impact import repository as repo_mod

        plan = "\n".join(
            r[0] for r in s.execute(
                text("EXPLAIN " + str(repo_mod._EDGES_SQL)),
                {"ids": [root], "pid": env.project_id},
            ).all()
        )
    assert "ix_version_dependencies_dependency" in plan, plan


def test_graph_db_other_project_and_platform_dependents_excluded(env: Env) -> None:
    # 跨项目边被 DB 触发器拒绝；只有平台级（project_id IS NULL）节点豁免，可同时被多个项目依赖。
    root = _new_node(env, "PROMPT", project_id=None)
    mine = _new_node(env, "CONTENT")
    foreign = _new_node(env, "CONTENT", project_id=env.other_project_id)
    platform = _new_node(env, "PROMPT", project_id=None)
    for k in (mine, foreign, platform):
        _edge(env, k, root)
    got = _in_session(env, lambda repo: [
        e.parent for e in repo.make_fetch_dependents(env.project_id)([root])
    ])
    assert got == [mine]


def test_graph_db_interoperates_with_link_dependency(env: Env) -> None:
    root, child = _new_node(env), _new_node(env, "CONTENT")
    with session_scope(env.settings) as s:
        link_dependency(VersionRepository(s), parent_version_id=child,
                        dependency_version_id=root, purpose="GENERATION_BASELINE")
    view = _in_session(env, lambda repo: service.preview_version_impact(
        repo, project_id=env.project_id, version_id=root))
    assert [(n["versionId"], n["purposes"], n["depth"]) for n in view["nodes"]] == [
        (str(child), ["GENERATION_BASELINE"], 1)]


def test_version_lookup_404_same_shape_for_missing_other_project_and_platform(env: Env) -> None:
    other = _new_node(env, project_id=env.other_project_id)
    platform = _new_node(env, "PROMPT", project_id=None)
    for vid in (uuid.uuid4(), other, platform):
        exc = _api_error(lambda v=vid: _in_session(env, lambda repo: service.preview_version_impact(
            repo, project_id=env.project_id, version_id=v)))
        assert exc.code.value == "RESOURCE_NOT_FOUND" and _reason(exc) == it.REASON_VERSION_NOT_FOUND
        assert set(exc.details) == {"reason", "versionId"}


def test_e3_chain_reaches_content_revisions_from_prompt_and_baseline(env: Env) -> None:
    """编辑只写 E3（新版 -> 上一版）：从 PROMPT / BASELINE 沿 E3 链走到最新版本。"""
    prompt = _new_node(env, "PROMPT", project_id=None)
    baseline = _new_node(env, "BASELINE")
    c = _seed_content(env, versions=3)
    v1, v2, v3 = c.version_ids
    _edge(env, v1, prompt, "GENERATION_PROMPT")
    _edge(env, v1, baseline, "GENERATION_BASELINE")
    _edge(env, v2, v1, "CONTENT_REVISION")
    _edge(env, v3, v2, "CONTENT_REVISION")
    view = _in_session(env, lambda repo: service.preview_version_impact(
        repo, project_id=env.project_id, version_id=baseline))
    assert [(n["versionId"], n["depth"]) for n in view["nodes"]] == [
        (str(v1), 1), (str(v2), 2), (str(v3), 3)]
    assert [n["purposes"] for n in view["nodes"]][1:] == [["CONTENT_REVISION"]] * 2
    # 投影在 v3：只有 v3 是 REVIEW_REQUIRED，其余 HISTORICAL
    assert [n["impactClass"] for n in view["nodes"]] == [
        it.IMPACT_HISTORICAL, it.IMPACT_HISTORICAL, it.IMPACT_REVIEW_REQUIRED]
    # PROMPT 是平台级版本，不能作为起点
    exc = _api_error(lambda: _in_session(env, lambda repo: service.preview_version_impact(
        repo, project_id=env.project_id, version_id=prompt)))
    assert _reason(exc) == it.REASON_VERSION_NOT_FOUND


# ===========================================================================
# T-START / T-ROLLBACK
# ===========================================================================


def _rollback(env: Env, c: Seeded, target: uuid.UUID, *, entity_type: str = "CONTENT",
              project_id: uuid.UUID | None = None, entity_id: uuid.UUID | None = None) -> dict[str, Any]:
    return _in_session(env, lambda repo: service.preview_rollback(
        repo, project_id=project_id or env.project_id, entity_type=entity_type,
        entity_id=entity_id or c.content_id, target_version_id=target))


def test_t_start_01_rollback_fork_starts_from_projection_not_latest(env: Env) -> None:
    c = _seed_content(env, versions=3, point_to_version=1)
    v1, v2, v3 = c.version_ids
    on_v1, on_v3 = _new_node(env, "CONTENT"), _new_node(env, "CONTENT")
    _edge(env, on_v1, v1)
    _edge(env, on_v3, v3)
    view = _rollback(env, c, v2)
    assert view["start"]["versionId"] == str(v1) and view["start"]["isCurrent"] is True
    assert [n["versionId"] for n in view["nodes"]] == [str(on_v1)]  # 末版 v3 的下游不在其中
    assert view["target"]["versionId"] == str(v2)


def test_t_start_02_null_projection_is_409_not_fallback(env: Env) -> None:
    c = _seed_content(env, versions=2)
    _exec(env, "UPDATE content_items SET current_version_id = NULL WHERE id = :c", {"c": c.content_id})
    exc = _api_error(lambda: _rollback(env, c, c.version_ids[0]))
    assert exc.code.value == "INVALID_TRANSITION" and _reason(exc) == it.REASON_NO_CURRENT_VERSION


def test_t_start_03_rollback_endpoint_takes_no_client_supplied_start() -> None:
    from rednote.modules.versions.impact.api import router

    route = next(r for r in router.routes if getattr(r, "path", "").endswith("/rollback-impacts"))
    names = {p.alias or p.name for p in route.dependant.query_params}
    assert names == {"entityType", "entityId", "targetVersionId"}


def test_t_rollback_01_argument_errors(env: Env) -> None:
    c = _seed_content(env, versions=2, point_to_version=1)
    other = _seed_content(env, versions=1)
    foreign = _seed_content(env, project_id=env.other_project_id, versions=1)
    v1, v2 = c.version_ids
    exc = _api_error(lambda: _rollback(env, c, v2, entity_type="MEDIA"))
    assert exc.code.value == "VALIDATION_FAILED" and _reason(exc) == it.REASON_ENTITY_TYPE_NOT_SUPPORTED
    exc = _api_error(lambda: _rollback(env, c, other.version_ids[0]))
    assert exc.code.value == "VALIDATION_FAILED" and _reason(exc) == it.REASON_TARGET_NOT_OF_ENTITY
    exc = _api_error(lambda: _rollback(env, c, v1))
    assert exc.code.value == "VALIDATION_FAILED" and _reason(exc) == it.REASON_TARGET_IS_CURRENT
    exc = _api_error(lambda: _rollback(env, c, uuid.uuid4()))
    assert exc.code.value == "RESOURCE_NOT_FOUND" and _reason(exc) == it.REASON_VERSION_NOT_FOUND
    exc = _api_error(lambda: _rollback(env, c, v2, entity_id=uuid.uuid4()))
    assert exc.code.value == "RESOURCE_NOT_FOUND" and _reason(exc) == it.REASON_CONTENT_NOT_FOUND
    exc = _api_error(lambda: _rollback(env, c, v2, entity_id=foreign.content_id))
    assert _reason(exc) == it.REASON_CONTENT_NOT_FOUND  # 他项目内容与不存在同形


def test_t_rollback_02_three_kinds_of_export_impact_and_recorded_state(env: Env) -> None:
    c = _seed_content(env, versions=2, point_to_version=1)
    v1, v2 = c.version_ids
    g_target = _make_export(env, [c])  # 冻结在 v1
    _point_to(env, c, 1)
    g_depart = _make_export(env, [c])  # 冻结在 v2（D）
    down = _seed_content(env, versions=1)
    _edge(env, down.version_ids[0], v2, "CONTENT_REVISION")
    g_down = _make_export(env, [down])
    # 投影在 v2 时扫描：冻结在 v1 的那条被判失效（VERSION_SUPERSEDED），记录态变 INVALID
    _sweep(env, contents=[c.content_id])
    view = _rollback(env, c, v1)
    by_group = {i["exportGroupId"]: i for i in view["exports"]["items"]}
    assert by_group[str(g_depart)]["exportImpact"] == it.EXPORT_IMPACT_DEPARTING
    assert by_group[str(g_target)]["exportImpact"] == it.EXPORT_IMPACT_TARGET
    assert by_group[str(g_target)]["recordedState"] == it.STATE_INVALID
    assert by_group[str(g_down)]["exportImpact"] == it.EXPORT_IMPACT_DOWNSTREAM
    assert by_group[str(g_depart)]["recordedState"] == it.STATE_VALID
    assert view["exports"]["complete"] is True and view["exports"]["countIsLowerBound"] is False


def test_t_rollback_03_unresolved_vs_historical_vs_review_required(env: Env) -> None:
    c = _seed_content(env, versions=2, point_to_version=1)
    v1, v2 = c.version_ids
    cur = _seed_content(env, versions=1)  # 下游内容，投影指向自己 ⇒ REVIEW_REQUIRED
    old = _seed_content(env, versions=2, point_to_version=2)  # v1 是历史 ⇒ HISTORICAL
    brief_like = _new_node(env, "PLAN")  # 无解析器 ⇒ UNRESOLVED
    report = _new_node(env, "REPORT")
    nodes = {"cur": cur.version_ids[0], "old": old.version_ids[0], "plan": brief_like, "report": report}
    for n in nodes.values():
        _edge(env, n, v1)
    view = _rollback(env, c, v2)
    cls = {n["versionId"]: n for n in view["nodes"]}
    assert cls[str(nodes["cur"])]["impactClass"] == it.IMPACT_REVIEW_REQUIRED
    assert cls[str(nodes["old"])]["impactClass"] == it.IMPACT_HISTORICAL
    for k in ("plan", "report"):
        n = cls[str(nodes[k])]
        assert n["isCurrent"] is None and n["impactClass"] == it.IMPACT_UNRESOLVED  # null，不是 false
    assert view["byType"] == {"CONTENT": 2, "PLAN": 1, "REPORT": 1}


def test_t_rollback_04_preview_writes_nothing(env: Env) -> None:
    c, g, item = _exported(env, versions=2, point_to_version=1)
    _point_to(env, c, 1)  # 冻结版本已被取代
    before = _event_count(env)
    _rollback(env, c, c.version_ids[0])
    _in_session(env, lambda repo: service.preview_version_impact(
        repo, project_id=env.project_id, version_id=c.version_ids[0]))
    _in_session(env, lambda repo: service.evaluate_group(
        repo, project_id=env.project_id, export_group_id=g))
    assert _event_count(env) == before == 0


def test_preview_truncation_is_200_with_lower_bound_flags(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _new_node(env)
    for _ in range(4):
        _edge(env, _new_node(env, "CONTENT"), root)
    monkeypatch.setattr(service, "MAX_TRAVERSAL_NODES", 3)
    view = _in_session(env, lambda repo: service.preview_version_impact(
        repo, project_id=env.project_id, version_id=root))
    t = view["traversal"]
    assert t["complete"] is False and t["truncation"] == it.TRUNCATION_NODE_LIMIT
    assert t["nodeCount"] == 3 and t["nodeCountIsLowerBound"] is True
    assert view["exports"]["complete"] is False and view["exports"]["countIsLowerBound"] is True


def test_preview_flags_dirty_cycle_without_raising(env: Env) -> None:
    a, b = _new_node(env), _new_node(env)
    _edge(env, b, a)
    _edge(env, a, b)
    view = _in_session(env, lambda repo: service.preview_version_impact(
        repo, project_id=env.project_id, version_id=a))
    assert view["traversal"]["anomalies"] == [it.ANOMALY_CYCLE] and view["traversal"]["complete"]


# ===========================================================================
# REPORT 条件注册（OQ-34-12）：无 quality_reports 表时恒 UNRESOLVED，且不得 UndefinedTable
# ===========================================================================


def _report_tables_present(env: Env) -> bool:
    return bool(_scalar(
        env, "SELECT to_regclass('quality_reports') IS NOT NULL OR"
             " to_regclass('quality_report_versions') IS NOT NULL"))


def test_report_resolver_absent_tables_unresolved_and_transaction_stays_usable(env: Env) -> None:
    if _report_tables_present(env):
        pytest.skip("本库已有真实 WP33 表，『无表』情形不可造")
    c = _seed_content(env, versions=1)
    rep = _new_node(env, "REPORT")
    _edge(env, rep, c.version_ids[0], "REPORT_INPUT")
    with session_scope(env.settings) as s:
        view = service.preview_version_impact(
            ImpactRepository(s), project_id=env.project_id, version_id=c.version_ids[0])
        # 同一事务内继续可用（UndefinedTable 会让事务 aborted）
        assert s.execute(text("SELECT 1")).scalar() == 1
    assert view["nodes"][0]["isCurrent"] is None
    assert view["nodes"][0]["impactClass"] == it.IMPACT_UNRESOLVED


def test_report_resolver_present_tables_resolve_current_and_historical(env: Env) -> None:
    if _report_tables_present(env):
        pytest.skip("本库已有真实 WP33 表，不覆盖也不删除")
    engine = get_engine(env.settings)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE quality_reports (id uuid PRIMARY KEY, current_version_id uuid)"))
            conn.execute(text(
                "CREATE TABLE quality_report_versions (id uuid PRIMARY KEY, report_id uuid NOT NULL)"))
        c = _seed_content(env, versions=1)
        report_id = uuid.uuid4()
        cur, old = _new_node(env, "REPORT"), _new_node(env, "REPORT")
        for n in (cur, old):
            _edge(env, n, c.version_ids[0], "REPORT_INPUT")
        _exec(env, "INSERT INTO quality_reports (id, current_version_id) VALUES (:r, :v)",
              {"r": report_id, "v": cur})
        for n in (cur, old):
            _exec(env, "INSERT INTO quality_report_versions (id, report_id) VALUES (:n, :r)",
                  {"n": n, "r": report_id})
        view = _in_session(env, lambda repo: service.preview_version_impact(
            repo, project_id=env.project_id, version_id=c.version_ids[0]))
        got = {n["versionId"]: n["impactClass"] for n in view["nodes"]}
        assert got == {str(cur): it.IMPACT_REVIEW_REQUIRED, str(old): it.IMPACT_HISTORICAL}
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS quality_report_versions"))
            conn.execute(text("DROP TABLE IF EXISTS quality_reports"))
    assert not _report_tables_present(env)


# ===========================================================================
# T-EXP：导出失效（真库）
# ===========================================================================


def test_t_exp_01_event_insert_does_not_touch_export_items_row(env: Env) -> None:
    """★ PG 语义推断的实测：子表 INSERT 取被引用行 FOR KEY SHARE，不是 UPDATE。

    断言：被引用 export_items 行的 xmin（行版本）与整行摘要在 INSERT 事件前后不变；
    其 BEFORE UPDATE 触发器（trg_export_items_immutable，拒绝一切 UPDATE）没有被触发——
    同一库里直接 UPDATE 同一行会被它以 23001 拒绝，而 INSERT 事件成功。
    """
    c, g, item = _exported(env, versions=2, point_to_version=1)
    engine = get_engine(env.settings)
    snap = (
        "SELECT xmin::text, md5(t::text), ctid::text FROM export_items t WHERE id = :i"
    )
    triggers = _rows(
        env,
        "SELECT tgname, (tgtype & 2) > 0 AS is_before, (tgtype & 16) > 0 AS on_update"
        "  FROM pg_trigger WHERE tgrelid = 'export_items'::regclass AND NOT tgisinternal",
    )
    assert any(n == "trg_export_items_immutable" and b and u for n, b, u in triggers)
    assert _probe(env, [("UPDATE export_items SET zip_byte_size = zip_byte_size WHERE id = :i",
                         {"i": item})])[0] == IMMUTABLE_RECORD_SQLSTATE
    before = _rows(env, snap, {"i": item})[0]
    _add_version(env, c)  # 冻结在 v1，投影前进 ⇒ 失效
    result = _sweep(env, contents=[c.content_id])
    assert result["summary"]["invalidated"] == 1
    after = _rows(env, snap, {"i": item})[0]
    assert before == after, (before, after)
    with engine.connect() as conn:  # 事务内也要不变（持锁期间）
        trans = conn.begin()
        try:
            xmin_in_txn = conn.execute(text(snap), {"i": item}).first()
            assert tuple(xmin_in_txn) == before
        finally:
            trans.rollback()
    print(f"T-EXP-01 xmin before={before[0]} after={after[0]} row_md5_equal={before[1] == after[1]}")


def test_t_exp_03_export_items_rows_stay_undeletable_with_events_referencing(env: Env) -> None:
    c, g, item = _exported(env, versions=2, point_to_version=1)
    _add_version(env, c)
    _sweep(env, contents=[c.content_id])
    assert len(_events(env, item)) == 1
    for sql in ("DELETE FROM export_items WHERE id = :i",
                "UPDATE export_items SET zip_byte_size = 1 WHERE id = :i"):
        assert _probe(env, [(sql, {"i": item})])[0] == IMMUTABLE_RECORD_SQLSTATE
    # 无引用的行同样 23001（不可变触发器先于 FK 拦下）
    _c2, _g2, item2 = _exported(env)
    assert _probe(env, [("DELETE FROM export_items WHERE id = :i", {"i": item2})])[0] == IMMUTABLE_RECORD_SQLSTATE


def test_t_exp_04_event_table_is_immutable(env: Env) -> None:
    c, g, item = _exported(env, versions=2, point_to_version=1)
    _add_version(env, c)
    _sweep(env, contents=[c.content_id])
    for sql in ("UPDATE export_validity_events SET seq = seq WHERE export_item_id = :i",
                "DELETE FROM export_validity_events WHERE export_item_id = :i"):
        assert _probe(env, [(sql, {"i": item})])[0] == IMMUTABLE_RECORD_SQLSTATE


def test_t_exp_05_unchanged_export_is_valid_and_sweep_writes_nothing(env: Env) -> None:
    c, g, item = _exported(env, versions=1)
    out = _sweep(env, contents=[c.content_id])
    assert out["summary"] == {"invalidated": 0, "restored": 0, "unchanged": 1, "unknown": 0}
    assert out["items"][0]["derivedState"] == it.STATE_VALID and _events(env, item) == []


def test_t_exp_06_new_version_invalidates_as_version_superseded(env: Env) -> None:
    c, g, item = _exported(env, versions=1)
    _add_version(env, c)
    out = _sweep(env, contents=[c.content_id])
    assert out["summary"]["invalidated"] == 1
    assert _events(env, item) == [(1, "INVALIDATED", "VERSION_SUPERSEDED")]
    row = _rows(env, "SELECT scope_kind, source_version_id, observed_version_id FROM"
                     " export_validity_events WHERE export_item_id = :i", {"i": item})[0]
    assert row[0] == "CONTENTS" and row[1] is None and row[2] == c.version_ids[1]


def test_t_exp_07_rollback_to_frozen_version_restores(env: Env) -> None:
    c, g, item = _exported(env, versions=1)
    _add_version(env, c)
    _sweep(env, contents=[c.content_id])
    _point_to(env, c, 0)  # 回退到被冻结的版本
    out = _sweep(env, contents=[c.content_id])
    assert out["summary"]["restored"] == 1
    assert _events(env, item) == [(1, "INVALIDATED", "VERSION_SUPERSEDED"), (2, "RESTORED", None)]
    _add_version(env, c)  # 再改稿 ⇒ 第三条
    _sweep(env, contents=[c.content_id])
    assert [e[1] for e in _events(env, item)] == ["INVALIDATED", "RESTORED", "INVALIDATED"]


def test_t_exp_08_reprocessed_image_is_asset_changed(env: Env) -> None:
    c = _seed_content(env, versions=1)
    _seed_media_slot(env, c)
    g = _make_export(env, [c])
    item = _items_of(env, g)[0]
    _reprocess_delivery(env, c, 2)
    out = _sweep(env, contents=[c.content_id])
    assert out["items"][0]["derivedReason"] == it.REASON_ASSET_CHANGED
    assert _events(env, item) == [(1, "INVALIDATED", "ASSET_CHANGED")]


def test_t_exp_09_unavailable_is_unknown_and_never_persisted(env: Env) -> None:
    c = _seed_content(env, versions=1)
    _seed_media_slot(env, c)
    g = _make_export(env, [c])
    item = _items_of(env, g)[0]
    _seed_media_slot(env, c, with_version=False, sort_order=1)  # 新增一个没出图的位 ⇒ 资格算不出
    out = _sweep(env, contents=[c.content_id])
    row = out["items"][0]
    assert row["derivedState"] == it.STATE_UNKNOWN and row["action"] == it.ACTION_UNKNOWN
    assert row["unknownReason"] == x_types.REASON_NO_DELIVERY_ASSET  # 透传，不折成 VALID
    assert out["summary"]["unknown"] == 1 and _events(env, item) == []
    view = _in_session(env, lambda repo: service.evaluate_group(
        repo, project_id=env.project_id, export_group_id=g))
    assert view["items"][0]["derivedState"] == it.STATE_UNKNOWN
    assert view["items"][0]["inSync"] is None  # 无法对账 ≠ false
    assert view["summary"] == {"valid": 0, "invalid": 0, "unknown": 1}


def test_t_exp_10_repeated_sweep_is_idempotent(env: Env) -> None:
    c, g, item = _exported(env, versions=1)
    _add_version(env, c)
    first = _sweep(env, contents=[c.content_id])
    second = _sweep(env, contents=[c.content_id])
    assert first["summary"]["invalidated"] == 1
    assert second["summary"] == {"invalidated": 0, "restored": 0, "unchanged": 1, "unknown": 0}
    assert len(_events(env, item)) == 1


def test_t_exp_11_invalid_to_invalid_with_changed_reason_appends_nothing(env: Env) -> None:
    c = _seed_content(env, versions=1)
    _seed_media_slot(env, c)
    g = _make_export(env, [c])
    item = _items_of(env, g)[0]
    _reprocess_delivery(env, c, 2)
    _sweep(env, contents=[c.content_id])  # ASSET_CHANGED
    _add_version(env, c)
    # 媒体位属于内容身份，新正文版本下仍可算资格；版本已变 ⇒ 原因变为 VERSION_SUPERSEDED
    out = _sweep(env, contents=[c.content_id])
    assert out["items"][0]["derivedState"] == it.STATE_INVALID
    assert out["items"][0]["derivedReason"] == it.REASON_VERSION_SUPERSEDED
    assert out["items"][0]["action"] == it.ACTION_UNCHANGED
    assert _events(env, item) == [(1, "INVALIDATED", "ASSET_CHANGED")]


def test_t_exp_12_t_reuse_03_shared_observation_one_seam_call_per_content(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _seed_content(env, versions=1)
    g1, g2 = _make_export(env, [c]), _make_export(env, [c])
    other = _seed_content(env, versions=1)
    _make_export(env, [other])
    calls: list[uuid.UUID] = []
    real = export_bridge.current_eligibility_snapshot

    def spy(repo: Any, *, content_item_id: uuid.UUID) -> Any:
        calls.append(content_item_id)
        return real(repo, content_item_id=content_item_id)

    monkeypatch.setattr(export_bridge, "current_eligibility_snapshot", spy)
    out = _sweep(env, contents=[c.content_id, other.content_id])
    assert len(out["items"]) == 3
    assert sorted(calls) == sorted([c.content_id, other.content_id])  # 每个内容至多 1 次


def test_t_reuse_01_bridge_is_byte_identical_to_the_seam(env: Env) -> None:
    c = _seed_content(env, versions=2, point_to_version=1)
    with session_scope(env.settings) as s:
        repo = ExportRepository(s)
        direct_version, direct_hash = x_service.current_eligibility_snapshot(
            repo, content_item_id=c.content_id)
        plain = x_service.current_eligibility_hash(repo, content_item_id=c.content_id)
        obs = export_bridge.observe(s, c.content_id)
    assert isinstance(obs, it.Observed)
    assert (obs.version_id, obs.eligibility_hash) == (direct_version, direct_hash)
    assert obs.eligibility_hash == plain and obs.version_id == c.version_ids[0]
    empty = _seed_content(env, versions=0)
    with session_scope(env.settings) as s:
        bad = export_bridge.observe(s, empty.content_id)
    assert isinstance(bad, it.Unavailable) and bad.upstream_reason == x_types.REASON_CONTENT_NO_VERSION


def test_t_exp_13_cross_project_is_404_and_writes_nothing(env: Env) -> None:
    foreign, _g, _i = _exported(env, project_id=env.other_project_id)
    exc = _api_error(lambda: _sweep(env, contents=[foreign.content_id]))
    assert exc.code.value == "RESOURCE_NOT_FOUND" and _reason(exc) == it.REASON_CONTENT_NOT_FOUND
    other_node = _new_node(env, project_id=env.other_project_id)
    exc = _api_error(lambda: _sweep(env, from_version=other_node))
    assert _reason(exc) == it.REASON_VERSION_NOT_FOUND
    exc = _api_error(lambda: _in_session(env, lambda repo: service.evaluate_group(
        repo, project_id=env.project_id, export_group_id=_g)))
    assert _reason(exc) == it.REASON_EXPORT_NOT_FOUND
    assert _event_count(env) == 0


def test_t_exp_14_from_version_sweeps_downstream_content_and_records_source(env: Env) -> None:
    baseline = _new_node(env, "BASELINE")
    c1, _g1, i1 = _exported(env, versions=1)
    c2, _g2, i2 = _exported(env, versions=1)
    untouched, _g3, i3 = _exported(env, versions=1)
    _edge(env, c1.version_ids[0], baseline, "GENERATION_BASELINE")
    _edge(env, c2.version_ids[0], baseline, "GENERATION_BASELINE")
    _add_version(env, c1)
    _add_version(env, c2)
    _add_version(env, untouched)  # 不在影响图内 ⇒ 不被本次扫描触碰
    out = _sweep(env, from_version=baseline)
    assert out["scope"]["kind"] == it.SCOPE_FROM_VERSION and out["scope"]["fromVersionId"] == str(baseline)
    assert out["summary"]["invalidated"] == 2
    assert _events(env, i3) == []
    row = _rows(env, "SELECT scope_kind, source_version_id FROM export_validity_events"
                     " WHERE export_item_id = :i", {"i": i1})[0]
    assert row == ("FROM_VERSION", baseline)


def test_t_exp_14_truncated_from_version_sweep_is_409_and_writes_zero_events(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _new_node(env, "BASELINE")
    items = []
    for _ in range(3):
        c, _g, item = _exported(env, versions=1)
        _edge(env, c.version_ids[0], baseline)
        _add_version(env, c)  # 本应被判失效
        items.append(item)
    monkeypatch.setattr(service, "MAX_TRAVERSAL_NODES", 2)
    exc = _api_error(lambda: _sweep(env, from_version=baseline))
    assert exc.code.value == "INVALID_TRANSITION" and _reason(exc) == it.REASON_IMPACT_TRUNCATED
    assert exc.details["truncation"] == it.TRUNCATION_NODE_LIMIT
    assert all(_events(env, i) == [] for i in items)  # fail-closed：一行都不写
    monkeypatch.undo()
    assert _sweep(env, from_version=baseline)["summary"]["invalidated"] == 3


def test_t_exp_15_scope_validation_and_limits(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _seed_content(env, versions=1)
    cid = c.content_id
    for kw, reason, code in (
        ({"contents": None}, it.REASON_SCOPE_INVALID, "VALIDATION_FAILED"),
        ({"contents": [cid], "from_version": uuid.uuid4()}, it.REASON_SCOPE_INVALID, "VALIDATION_FAILED"),
        ({"contents": []}, it.REASON_CONTENT_IDS_EMPTY, "VALIDATION_FAILED"),
        ({"contents": [cid, cid]}, it.REASON_CONTENT_IDS_DUPLICATE, "VALIDATION_FAILED"),
        ({"contents": [uuid.uuid4() for _ in range(it.MAX_SWEEP_CONTENT_IDS + 1)]},
         it.REASON_CONTENT_IDS_TOO_MANY, "VALIDATION_FAILED"),
    ):
        exc = _api_error(lambda kw=kw: _sweep(env, **kw))
        assert (exc.code.value, _reason(exc)) == (code, reason), kw
    # 命中导出行 > 上限 ⇒ 422，不静默截断
    a, _ga, _ia = _exported(env, versions=1)
    _make_export(env, [a])
    monkeypatch.setattr(service, "MAX_SWEEP_ITEMS", 1)
    exc = _api_error(lambda: _sweep(env, contents=[a.content_id]))
    assert exc.code.value == "VALIDATION_FAILED" and _reason(exc) == it.REASON_SWEEP_SCOPE_TOO_LARGE


# ===========================================================================
# 并发
# ===========================================================================


def test_t_conc_01_two_concurrent_sweeps_write_exactly_one_invalidation(env: Env) -> None:
    c, g, item = _exported(env, versions=1)
    _add_version(env, c)

    def run() -> dict[str, Any]:
        with session_scope(env.settings) as s:
            return service.sweep(
                ImpactRepository(s), project_id=env.project_id, actor_id=env.owner_id,
                content_item_ids=[c.content_id], from_version_id=None)

    cm = session_scope(env.settings)
    session_a = cm.__enter__()
    res_a = service.sweep(
        ImpactRepository(session_a), project_id=env.project_id, actor_id=env.owner_id,
        content_item_ids=[c.content_id], from_version_id=None)  # A 已插入、未提交
    with ThreadPoolExecutor(1) as ex:
        fut = ex.submit(run)  # B 在唯一索引上等 A
        time.sleep(1.0)
        assert not fut.done()
        cm.__exit__(None, None, None)  # A 提交
        res_b = fut.result(timeout=30)
    assert res_a["summary"]["invalidated"] == 1
    assert res_b["summary"] == {"invalidated": 0, "restored": 0, "unchanged": 1, "unknown": 0}
    assert _events(env, item) == [(1, "INVALIDATED", "VERSION_SUPERSEDED")]


def test_t_conc_02_items_are_processed_in_ascending_id_order(env: Env) -> None:
    cs = [_seed_content(env, versions=1) for _ in range(4)]
    _make_export(env, cs)
    out = _sweep(env, contents=[c.content_id for c in cs])
    ids = [i["exportItemId"] for i in out["items"]]
    assert ids == sorted(ids)


# ===========================================================================
# T-DB：DDL 与触发器（对照 WP34-ddl-草案.py）
# ===========================================================================

_EXPECTED_CONSTRAINTS = {
    "pk_export_validity_events",
    "ck_export_validity_events_seq_positive",
    "ck_export_validity_events_transition_valid",
    "ck_export_validity_events_reason_valid",
    "ck_export_validity_events_reason_paired",
    "ck_export_validity_events_scope_kind_valid",
    "ck_export_validity_events_scope_source_paired",
    "ck_export_validity_events_observed_hash_shape",
    "uq_export_validity_events_export_item_id_seq",
    "fk_export_validity_events_export_item_id_export_items",
    "fk_export_validity_events_observed_version_id_version_nodes",
    "fk_export_validity_events_source_version_id_version_nodes",
    "fk_export_validity_events_created_by_users",
}


def test_t_db_01_structure_matches_ddl_draft(env: Env) -> None:
    got = {r[0] for r in _rows(
        env, "SELECT conname FROM pg_constraint WHERE conrelid = 'export_validity_events'::regclass")}
    assert got == _EXPECTED_CONSTRAINTS
    assert all(len(n.encode()) <= 63 for n in got)
    assert {r[0] for r in _rows(
        env, "SELECT tgname FROM pg_trigger WHERE tgrelid = 'export_validity_events'::regclass"
             " AND NOT tgisinternal")} == set(i_ddl.trigger_names())
    cols = {r[0]: (r[1], r[2]) for r in _rows(
        env, "SELECT column_name, is_nullable, character_maximum_length FROM"
             " information_schema.columns WHERE table_name = 'export_validity_events'")}
    assert cols["reason"][0] == "YES" and cols["source_version_id"][0] == "YES"
    for notnull in ("export_item_id", "seq", "transition", "observed_version_id",
                    "observed_eligibility_hash", "scope_kind", "created_by", "created_at"):
        assert cols[notnull][0] == "NO", notnull
    assert (cols["transition"][1], cols["reason"][1], cols["scope_kind"][1]) == (12, 24, 16)
    # FK 全部 RESTRICT
    deltypes = {r[0] for r in _rows(
        env, "SELECT confdeltype::text FROM pg_constraint WHERE contype = 'f'"
             " AND conrelid = 'export_validity_events'::regclass")}
    assert deltypes == {"r"}
    # 没有多余索引：只有 pk 与 uq 各自的索引
    assert {r[0] for r in _rows(
        env, "SELECT indexname FROM pg_indexes WHERE tablename = 'export_validity_events'")} == {
        "pk_export_validity_events", "uq_export_validity_events_export_item_id_seq"}


def _item_ctx(env: Env) -> dict[str, Any]:
    c, g, item = _exported(env, versions=2, point_to_version=1)
    frozen_v, frozen_h = _rows(
        env, "SELECT version_node_id, eligibility_hash FROM export_items WHERE id = :i", {"i": item})[0]
    return {"item": item, "frozen_v": frozen_v, "frozen_h": frozen_h,
            "other_v": c.version_ids[1], "other_h": _hash("drifted")}


_EVENT_SQL = (
    "INSERT INTO export_validity_events (export_item_id, seq, transition, reason,"
    " observed_version_id, observed_eligibility_hash, scope_kind, source_version_id, created_by)"
    " VALUES (:item, :seq, :tr, :reason, :ov, :oh, :sk, :src, :by)"
)


def _ev(env: Env, ctx: dict[str, Any], **over: Any) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {
        "item": ctx["item"], "seq": 1, "tr": "INVALIDATED", "reason": "VERSION_SUPERSEDED",
        "ov": ctx["other_v"], "oh": ctx["other_h"], "sk": "CONTENTS", "src": None, "by": env.owner_id,
    }
    params.update(over)
    return _EVENT_SQL, params


def _restored(env: Env, ctx: dict[str, Any], seq: int, **over: Any) -> tuple[str, dict[str, Any]]:
    base = {"seq": seq, "tr": "RESTORED", "reason": None,
            "ov": ctx["frozen_v"], "oh": ctx["frozen_h"]}
    base.update(over)
    return _ev(env, ctx, **base)


@pytest.mark.parametrize(
    "over, constraint",
    [
        ({"seq": 0}, "seq_positive"),
        ({"tr": "BOGUS", "reason": None}, "transition_valid"),
        ({"reason": "BOGUS"}, "reason_valid"),
        ({"reason": None}, "reason_paired"),
        ({"scope_kind": "BOGUS"}, "scope_kind_valid"),
        ({"sk": "FROM_VERSION", "src": None}, "scope_source_paired"),
        ({"sk": "CONTENTS", "src": "SELF"}, "scope_source_paired"),
        ({"oh": "abc"}, "observed_hash_shape"),
        ({"oh": "G" * 64}, "observed_hash_shape"),
    ],
)
def test_t_db_02_check_constraints_reject_with_their_own_name(
    env: Env, over: dict[str, Any], constraint: str
) -> None:
    ctx = _item_ctx(env)
    over = dict(over)
    if "scope_kind" in over:
        over["sk"] = over.pop("scope_kind")
    if over.get("src") == "SELF":
        over["src"] = ctx["other_v"]
    state, name = _probe(env, [_ev(env, ctx, **over)])
    assert state == CHECK_VIOLATION and name == f"ck_export_validity_events_{constraint}", (state, name)


def test_t_db_02_reason_paired_both_directions(env: Env) -> None:
    ctx = _item_ctx(env)
    s1, n1 = _probe(env, [_restored(env, ctx, 1, reason="VERSION_SUPERSEDED")])
    assert (s1, n1) == (CHECK_VIOLATION, "ck_export_validity_events_reason_paired")


def test_t_db_03_trigger_counterexamples_all_23514(env: Env) -> None:
    ctx = _item_ctx(env)
    ok_first = _ev(env, ctx)
    cases = {
        "first_is_restored": [_restored(env, ctx, 1)],
        "first_seq_not_one": [_ev(env, ctx, seq=2)],
        "seq_gap": [ok_first, _restored(env, ctx, 3)],
        "same_transition_twice": [ok_first, _ev(env, ctx, seq=2)],
        "invalidated_hash_equals_frozen": [_ev(env, ctx, oh=ctx["frozen_h"])],
        "superseded_but_version_unchanged": [_ev(env, ctx, ov=ctx["frozen_v"])],
        "asset_changed_but_version_changed": [_ev(env, ctx, reason="ASSET_CHANGED")],
        "restored_hash_not_frozen": [ok_first, _restored(env, ctx, 2, oh=ctx["other_h"])],
        "restored_version_not_frozen": [ok_first, _restored(env, ctx, 2, ov=ctx["other_v"])],
    }
    for label, statements in cases.items():
        state, name = _probe(env, statements)
        assert (state, name) == (CHECK_VIOLATION, ""), (label, state, name)  # 触发器：无约束名


def test_t_db_03_trigger_accepts_the_alternating_sequence(env: Env) -> None:
    ctx = _item_ctx(env)
    assert _probe(env, [
        _ev(env, ctx),
        _restored(env, ctx, 2),
        _ev(env, ctx, seq=3, reason="ASSET_CHANGED", ov=ctx["frozen_v"]),
    ]) == ("", "")


def test_t_db_04_fk_rejects_unknown_export_item_23503(env: Env) -> None:
    ctx = _item_ctx(env)
    state, name = _probe(env, [_ev(env, ctx, item=uuid.uuid4())])
    assert state == "23503" and name == "fk_export_validity_events_export_item_id_export_items"


def test_t_db_05_duplicate_seq_is_unique_violation_23505(env: Env) -> None:
    ctx = _item_ctx(env)
    state, name = _probe(env, [_ev(env, ctx), _ev(env, ctx)])
    assert state == "23505" and name == "uq_export_validity_events_export_item_id_seq"


def test_t_db_06_own_functions_exclude_the_shared_reject_function() -> None:
    assert i_ddl.OWN_FUNCTIONS == ("rednote_check_export_validity_event",)
    assert "rednote_reject_mutation" not in i_ddl.OWN_FUNCTIONS
    assert i_ddl.trigger_names() == (
        "trg_export_validity_events_immutable", "trg_export_validity_events_consistency")


def test_t_db_08_fk_closure_puts_event_table_ahead_of_export_items() -> None:
    engine = get_engine(_make_settings())
    assert "export_validity_events" in truncate_targets(engine, ("export_items",))
    assert "fk_export_validity_events_export_item_id_export_items" in cross_package_foreign_keys(
        engine, ["export_items"])


def test_orm_guard_rejects_update_of_an_event_instance(env: Env) -> None:
    from rednote.platform.immutability import ImmutableRecordError

    c, g, item = _exported(env, versions=2, point_to_version=1)
    _add_version(env, c)
    _sweep(env, contents=[c.content_id])
    with pytest.raises(ImmutableRecordError):
        with session_scope(env.settings) as s:
            ev = s.query(ExportValidityEvent).filter_by(export_item_id=item).one()
            ev.seq = 99
            s.flush()


# ===========================================================================
# 事件历史 / 当前态
# ===========================================================================


def test_current_state_is_the_max_seq_transition_and_events_listed_newest_first(env: Env) -> None:
    c, g, item = _exported(env, versions=1)
    _add_version(env, c)
    _sweep(env, contents=[c.content_id])
    _point_to(env, c, 0)
    _sweep(env, contents=[c.content_id])
    with session_scope(env.settings) as s:
        repo = ImpactRepository(s)
        latest = repo.latest_events([item])
        events = service.list_events(repo, project_id=env.project_id, export_group_id=g,
                                     export_item_id=None)
    assert latest[item] == (2, "RESTORED")
    assert [e["seq"] for e in events] == [2, 1]
    with session_scope(env.settings) as s:
        view = service.evaluate_group(ImpactRepository(s), project_id=env.project_id, export_group_id=g)
    assert view["items"][0]["recordedState"] == it.STATE_VALID and view["items"][0]["inSync"] is True


def test_evaluate_reports_out_of_sync_before_any_sweep_and_never_writes(env: Env) -> None:
    c, g, item = _exported(env, versions=1)
    _add_version(env, c)
    view = _in_session(env, lambda repo: service.evaluate_group(
        repo, project_id=env.project_id, export_group_id=g))
    row = view["items"][0]
    assert (row["derivedState"], row["recordedState"], row["inSync"]) == (
        it.STATE_INVALID, it.STATE_VALID, False)
    assert row["derivedReason"] == it.REASON_VERSION_SUPERSEDED and _events(env, item) == []


def test_t_conc_03_observation_is_point_in_time_and_next_sweep_corrects(env: Env) -> None:
    c, g, item = _exported(env, versions=1)
    _add_version(env, c)
    _sweep(env, contents=[c.content_id])
    observed = _rows(env, "SELECT observed_version_id FROM export_validity_events"
                          " WHERE export_item_id = :i", {"i": item})[0][0]
    _add_version(env, c)  # 观测之后又被编辑
    assert observed == c.version_ids[1]  # 事件记的是当时所见，不是永久真相
    out = _sweep(env, contents=[c.content_id])
    assert out["items"][0]["action"] == it.ACTION_UNCHANGED  # 仍是 INVALID，不重复追加
    assert len(_events(env, item)) == 1
