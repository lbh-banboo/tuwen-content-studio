"""WP35 `exports` 包的判据落点 —— 纯函数层 / 数据库行为层 / HTTP 端到端层。

## 夹具纪律

· 一律跑**私有库**（`REDNOTE_TEST_DATABASE_URL`），不碰共享 `rednote_test`；
· 只重建本包 `export_items` 一张表 + 2 个触发器，其余表按迁移已就绪；
· 每个用例新造 user / project / content，**按项目隔离**，不数全库行；
· 造数走裸 SQL，且**同一事务提交**（延迟 FK / 延迟触发器要求）；
· 数据库探针断言 **sqlstate**，不只断言"抛了异常"（被拒的结论对 ≠ 原因对）。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from conftest import (
    cross_package_foreign_keys,
    drop_own_tables,
    truncate_targets,
)

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

from rednote.modules.exports import ddl as x_ddl
from rednote.modules.exports import schemas as x_schemas
from rednote.modules.exports import service as x_service
from rednote.modules.exports import types as x_types
from rednote.modules.exports.models import OWN_TABLES, ExportItem
from rednote.modules.exports.repository import ExportRepository
from rednote.modules.files.models import STORAGE_BACKEND_LOCAL
from rednote.modules.files.repository import FileRepository
from rednote.modules.files.service import store_generated_asset
from rednote.modules.files.storage import LocalStorageBackend
from rednote.modules.identity.models import ROLE_ADMIN, ROLE_OPERATOR
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.projects.models import PHASE_CLOSED, Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.platform.configuration import Settings
from rednote.platform.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
)
from rednote.platform.database import get_engine, reset_engines, session_scope
from rednote.platform.tokens import (
    generate_csrf_secret,
    generate_session_token,
    hash_token,
)

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
TEST_DATABASE_URL_ENV = "REDNOTE_TEST_DATABASE_URL"
TEST_ORIGIN = "http://testserver"
API = "/api/v1"

DROP_ORDER = ("export_items",)
_TABLE_NAMES = tuple(OWN_TABLES)

PNG_HEAD = b"\x89PNG\r\n\x1a\n"


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
    """只重建 `export_items` + 触发器；前后对账跨包外键集合不变。"""
    reset_engines()
    engine = get_engine(_make_settings())

    fks_before = cross_package_foreign_keys(engine, _TABLE_NAMES)
    dropped, kept = drop_own_tables(engine, DROP_ORDER)
    ExportItem.metadata.create_all(
        engine,
        tables=[ExportItem.metadata.tables[name] for name in OWN_TABLES],
    )
    fks_after = cross_package_foreign_keys(engine, _TABLE_NAMES)
    assert fks_after == fks_before, (
        f"建表夹具改变了跨包外键集合：before={sorted(fks_before)} "
        f"after={sorted(fks_after)}（dropped={dropped} kept={kept}）"
    )

    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(truncate_targets(engine, DROP_ORDER))))
        for _t, func_ddl, trigger_ddl, drop_trigger, _df in x_ddl.all_trigger_ddl():
            conn.execute(text(drop_trigger))
            conn.execute(text(func_ddl))
            conn.execute(text(trigger_ddl))
    yield


@pytest.fixture(autouse=True)
def _clean_own_rows() -> Iterator[None]:
    """每个用例前清空 `export_items`（不可变表，只有 TRUNCATE 能清）。"""
    reset_engines()
    engine = get_engine(_make_settings())
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(truncate_targets(engine, DROP_ORDER))))
    yield


# ===========================================================================
# 环境：用户 / 项目 / 存储
# ===========================================================================


@dataclass(frozen=True)
class ExportEnv:
    settings: Settings
    storage: LocalStorageBackend
    storage_root: Path
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
            display_name=f"WP35-{role}",
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
            project_code=f"WP35-{uuid.uuid4().hex[:8].upper()}",
            name="WP35 测试项目",
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
def env(tmp_path: Path) -> ExportEnv:
    root = tmp_path / "storage"
    settings = _make_settings(root)
    owner = _create_user(settings)
    collab = _create_user(settings)
    outsider = _create_user(settings)
    admin = _create_user(settings, role=ROLE_ADMIN)
    return ExportEnv(
        settings=settings,
        storage=LocalStorageBackend(root),
        storage_root=root,
        project_id=_seed_project(settings, owner_id=owner, collaborators=(collab,)),
        closed_project_id=_seed_project(settings, owner_id=owner, closed=True),
        other_project_id=_seed_project(settings, owner_id=owner),
        owner_id=owner,
        collab_id=collab,
        outsider_id=outsider,
        admin_id=admin,
    )


# ===========================================================================
# 造数 helper（裸 SQL；一条内容 = 身份行 + N 个正文版本 + 媒体位）
# ===========================================================================


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _unique_png() -> bytes:
    """库里一定没有过的字节（blob 内容寻址去重会让历史数据影响断言）。"""
    return PNG_HEAD + uuid.uuid4().bytes + b"rednote-wp35"


def _store_png(env: ExportEnv, project_id: uuid.UUID, content: bytes | None = None) -> uuid.UUID:
    with session_scope(env.settings) as session:
        stored = store_generated_asset(
            FileRepository(session),
            env.storage,
            project_id=project_id,
            original_name="配图.png",
            content=content if content is not None else _unique_png(),
            mime_type="image/png",
            uploaded_by=env.owner_id,
        )
        return stored.file_id


@dataclass
class SeededContent:
    content_id: uuid.UUID
    project_id: uuid.UUID
    version_ids: list[uuid.UUID] = field(default_factory=list)
    media_item_ids: list[uuid.UUID] = field(default_factory=list)
    media_version_ids: list[uuid.UUID] = field(default_factory=list)


def _insert_version_node(conn, *, entity_type: str, entity_id: uuid.UUID,
                         project_id: uuid.UUID, revision_no: int, created_by: uuid.UUID) -> uuid.UUID:
    node_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO version_nodes (id, entity_type, entity_id, project_id,"
            " revision_no, schema_version, payload_hash, created_by)"
            " VALUES (:id, :t, :e, :p, :r, 1, :h, :by)"
        ),
        {"id": node_id, "t": entity_type, "e": entity_id, "p": project_id,
         "r": revision_no, "h": _hash(f"{entity_id}-{revision_no}"), "by": created_by},
    )
    return node_id


def _seed_content(
    env: ExportEnv,
    *,
    project_id: uuid.UUID | None = None,
    title: str = "标题",
    versions: int = 1,
    body_prefix: str = "正文",
    point_to_version: int | None = None,
) -> SeededContent:
    """造一条内容。`versions=0` ⇒ 一版都没有；`point_to_version=1` 且 `versions=2`
    ⇒ 回滚分叉（投影指向 v1，末版是 v2）。"""
    pid = project_id or env.project_id
    cid = uuid.uuid4()
    out = SeededContent(content_id=cid, project_id=pid)
    with session_scope(env.settings) as session:
        conn = session.connection()
        conn.execute(
            text(
                "INSERT INTO content_items (id, project_id, current_version_id,"
                " state, current_version_no, lock_version, created_by)"
                " VALUES (:c, :p, NULL, 'DRAFT', 0, 0, :by)"
            ),
            {"c": cid, "p": pid, "by": env.owner_id},
        )
        for no in range(1, versions + 1):
            node = _insert_version_node(
                conn, entity_type="CONTENT", entity_id=cid, project_id=pid,
                revision_no=no, created_by=env.owner_id,
            )
            conn.execute(
                text(
                    "INSERT INTO content_versions (id, content_item_id, version_no,"
                    " title, body, tags, bindings, payload, payload_hash,"
                    " schema_version, created_by)"
                    " VALUES (:id, :c, :no, :t, :b, '[]'::jsonb, '[]'::jsonb,"
                    " '{}'::jsonb, :h, 1, :by)"
                ),
                {"id": node, "c": cid, "no": no, "t": f"{title}-v{no}",
                 "b": f"{body_prefix}-v{no}", "h": _hash(f"cv-{cid}-{no}"),
                 "by": env.owner_id},
            )
            out.version_ids.append(node)
        if versions:
            idx = (point_to_version or versions) - 1
            conn.execute(
                text("UPDATE content_items SET current_version_id = :v,"
                     " current_version_no = :n WHERE id = :c"),
                {"v": out.version_ids[idx], "n": idx + 1, "c": cid},
            )
    return out


def _seed_reference_asset(conn, env: ExportEnv, project_id: uuid.UUID, file_id: uuid.UUID) -> uuid.UUID:
    ref_id = uuid.uuid4()
    conn.execute(
        text("INSERT INTO reference_assets (id, project_id, file_object_id, state,"
             " product_model) VALUES (:id, :p, :f, 'ACTIVE', 'M-WP35')"),
        {"id": ref_id, "p": project_id, "f": file_id},
    )
    return ref_id


def _seed_media_slot(
    env: ExportEnv,
    content: SeededContent,
    *,
    sort_order: int = 0,
    renditions: tuple[tuple[str, int, str], ...] = (("DELIVERY", 1, "READY"),),
    with_version: bool = True,
    image_format: str = "png",
) -> uuid.UUID:
    """造一个媒体位。`renditions` = `[(kind, attempt_no, state), ...]`。

    `with_version=False` ⇒ 只有位没有版本（投影为空），用来造"位在、图没出"。
    返回 `media_item_id`；READY 的档位各带一个真文件（真字节）。
    """
    pid = content.project_id
    mid = uuid.uuid4()
    file_ids = {
        (kind, attempt): _store_png(env, pid)
        for kind, attempt, state in renditions if state == "READY"
    }
    cover_file = _store_png(env, pid)
    with session_scope(env.settings) as session:
        conn = session.connection()
        conn.execute(
            text(
                "INSERT INTO media_items (id, project_id, content_item_id,"
                " paragraph_ref, sort_order, state, current_version_no,"
                " created_by, lock_version)"
                " VALUES (:id, :p, :c, 'p0-00000000', :so, 'PENDING', 0, :by, 0)"
            ),
            {"id": mid, "p": pid, "c": content.content_id, "so": sort_order,
             "by": env.owner_id},
        )
        content.media_item_ids.append(mid)
        if not with_version:
            return mid
        ref_id = _seed_reference_asset(conn, env, pid, cover_file)
        node = _insert_version_node(
            conn, entity_type="MEDIA", entity_id=mid, project_id=pid,
            revision_no=1, created_by=env.owner_id,
        )
        conn.execute(
            text(
                "INSERT INTO media_versions (id, media_item_id, version_no,"
                " paragraph_ref, sort_order, reference_bindings, image_file_id,"
                " prompt, payload, payload_hash, schema_version, created_by)"
                " VALUES (:id, :m, 1, 'p0-00000000', :so,"
                " CAST(:rb AS jsonb), :img, '提示词', '{}'::jsonb, :h, 1, :by)"
            ),
            {"id": node, "m": mid, "so": sort_order,
             "rb": json.dumps([{"assetId": str(ref_id)}]), "img": cover_file,
             "h": _hash(f"mv-{mid}"), "by": env.owner_id},
        )
        conn.execute(
            text("UPDATE media_items SET current_version_id = :v,"
                 " current_version_no = 1 WHERE id = :m"),
            {"v": node, "m": mid},
        )
        content.media_version_ids.append(node)
        for kind, attempt, state in renditions:
            ready = state == "READY"
            conn.execute(
                text(
                    "INSERT INTO media_renditions (media_version_id, kind,"
                    " attempt_no, state, file_object_id, width, height,"
                    " byte_size, image_format)"
                    " VALUES (:v, :k, :a, :s, :f, :w, :h, :b, :fmt)"
                ),
                {"v": node, "k": kind, "a": attempt, "s": state,
                 "f": file_ids[(kind, attempt)] if ready else None,
                 "w": 100 if ready else None, "h": 100 if ready else None,
                 "b": 64 if ready else None, "fmt": image_format if ready else None},
            )
    return mid


# ===========================================================================
# HTTP 夹具
# ===========================================================================


@pytest.fixture
def client(env: ExportEnv) -> Iterator[TestClient]:
    from rednote.main import create_app

    app: FastAPI = create_app(env.settings)
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, settings: Settings, user_id: uuid.UUID) -> str:
    """登录，返回 CSRF 明文。"""
    token = generate_session_token()
    csrf = generate_csrf_secret()
    with session_scope(settings) as session:
        IdentityRepository(session).create_session(
            user_id=user_id,
            token_hash=hash_token(token),
            csrf_secret_hash=hash_token(csrf),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=3600),
        )
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, token)
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    return csrf


def _wh(csrf: str, **extra: str) -> dict[str, str]:
    headers = {CSRF_HEADER_NAME: csrf, "Origin": TEST_ORIGIN}
    headers.update(extra)
    return headers


def _post_export(
    client: TestClient,
    csrf: str,
    project_id: uuid.UUID,
    content_ids: list[uuid.UUID],
    *,
    key: str | None = None,
    body_extra: dict[str, Any] | None = None,
):
    body: dict[str, Any] = {"contentItemIds": [str(c) for c in content_ids]}
    body.update(body_extra or {})
    extra = {"Idempotency-Key": key} if key is not None else {}
    return client.post(
        f"{API}/projects/{project_id}/exports", json=body, headers=_wh(csrf, **extra)
    )


def _read_zip(env: ExportEnv, sha256_hex_of_group_artifact: uuid.UUID) -> zipfile.ZipFile:
    """按产物文件对象读 ZIP 字节（经存储后端，不经 HTTP）。"""
    with session_scope(env.settings) as session:
        located = ExportRepository(session).file_blob_key(sha256_hex_of_group_artifact)
    assert located is not None
    data = b"".join(env.storage.open_stream(key=located[1]))
    return zipfile.ZipFile(io.BytesIO(data))


def _export_rows(env: ExportEnv, project_id: uuid.UUID) -> list[Any]:
    with session_scope(env.settings) as session:
        return list(
            session.execute(
                text("SELECT * FROM export_items WHERE project_id = :p ORDER BY created_at, id"),
                {"p": project_id},
            ).mappings().all()
        )


# ===========================================================================
# 最小闭环
# ===========================================================================


def test_single_text_only_export_end_to_end(env: ExportEnv, client: TestClient) -> None:
    """纯文字（无媒体位）一条内容 ⇒ 201，ZIP 里恰有 content.txt，库里恰 1 行。"""
    content = _seed_content(env, title="纯文字", versions=1)
    csrf = _login(client, env.settings, env.owner_id)

    resp = _post_export(client, csrf, env.project_id, [content.content_id])
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["exportKind"] == "SINGLE"
    assert data["format"] == "STATIC"
    assert data["itemCount"] == 1
    assert data["contentItemIds"] == [str(content.content_id)]
    assert data["created"] is True

    zf = _read_zip(env, uuid.UUID(data["artifactFileId"]))
    names = zf.namelist()
    assert len(names) == 1 and names[0].endswith("/content.txt"), names
    assert zf.read(names[0]).decode("utf-8") == "纯文字-v1\n\n正文-v1\n"

    rows = _export_rows(env, env.project_id)
    assert len(rows) == 1
    assert str(rows[0]["version_node_id"]) == str(content.version_ids[0])
    assert rows[0]["zip_sha256"] == data["zipSha256"]


# ===========================================================================
# 冻结口径（R2 当前版本 / R3 正式图 / 缺图拒绝）
# ===========================================================================


def _export_ok(client: TestClient, csrf: str, env: ExportEnv, ids: list[uuid.UUID], **kw: Any) -> dict[str, Any]:
    resp = _post_export(client, csrf, env.project_id, ids, **kw)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["data"]


def test_rollback_fork_exports_projection_not_latest_version(env: ExportEnv, client: TestClient) -> None:
    """R2：回滚后投影指向 v1、末版是 v2 ⇒ 必须导出 v1（投影），不是 v2（末版）。"""
    content = _seed_content(env, title="回滚", versions=2, point_to_version=1)
    csrf = _login(client, env.settings, env.owner_id)

    data = _export_ok(client, csrf, env, [content.content_id])
    zf = _read_zip(env, uuid.UUID(data["artifactFileId"]))
    text_ = zf.read(zf.namelist()[0]).decode("utf-8")
    assert "回滚-v1" in text_ and "v2" not in text_, text_
    rows = _export_rows(env, env.project_id)
    assert str(rows[0]["version_node_id"]) == str(content.version_ids[0])


def test_null_projection_falls_back_to_latest_version(env: ExportEnv, client: TestClient) -> None:
    """R2：投影为空（先建身份后建版本）⇒ 回落末版，而不是报"无版本"。"""
    content = _seed_content(env, title="回落", versions=2)
    with session_scope(env.settings) as session:
        session.execute(
            text("UPDATE content_items SET current_version_id = NULL,"
                 " current_version_no = 0 WHERE id = :c"),
            {"c": content.content_id},
        )
    csrf = _login(client, env.settings, env.owner_id)
    data = _export_ok(client, csrf, env, [content.content_id])
    zf = _read_zip(env, uuid.UUID(data["artifactFileId"]))
    assert "回落-v2" in zf.read(zf.namelist()[0]).decode("utf-8")


def test_content_without_any_version_is_rejected_409(env: ExportEnv, client: TestClient) -> None:
    content = _seed_content(env, versions=0)
    csrf = _login(client, env.settings, env.owner_id)
    resp = _post_export(client, csrf, env.project_id, [content.content_id])
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "INVALID_TRANSITION"
    assert err["details"]["reason"] == "CONTENT_NO_VERSION"
    assert err["details"]["contentItemId"] == str(content.content_id)
    assert _export_rows(env, env.project_id) == []


def test_content_with_ready_delivery_images_packs_them_in_order(env: ExportEnv, client: TestClient) -> None:
    content = _seed_content(env, title="图文", versions=1)
    _seed_media_slot(env, content, sort_order=1)
    _seed_media_slot(env, content, sort_order=0)
    csrf = _login(client, env.settings, env.owner_id)
    data = _export_ok(client, csrf, env, [content.content_id])
    names = _read_zip(env, uuid.UUID(data["artifactFileId"])).namelist()
    assert [n.rsplit("/", 1)[1] for n in names] == ["content.txt", "image_01.png", "image_02.png"], names


def test_slot_without_version_is_no_delivery_asset_409(env: ExportEnv, client: TestClient) -> None:
    """有位没图（位的投影为空）⇒ 拒绝，而不是只打文字。"""
    content = _seed_content(env, versions=1)
    _seed_media_slot(env, content, with_version=False)
    csrf = _login(client, env.settings, env.owner_id)
    resp = _post_export(client, csrf, env.project_id, [content.content_id])
    assert resp.status_code == 409, resp.text
    details = resp.json()["error"]["details"]
    assert details["reason"] == "NO_DELIVERY_ASSET"
    assert details["mediaSlotCount"] == 1 and details["deliveryAssetCount"] == 0


def test_all_failed_delivery_is_rejected(env: ExportEnv, client: TestClient) -> None:
    content = _seed_content(env, versions=1)
    _seed_media_slot(env, content, renditions=(("DELIVERY", 1, "FAILED"),))
    csrf = _login(client, env.settings, env.owner_id)
    resp = _post_export(client, csrf, env.project_id, [content.content_id])
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["reason"] == "NO_DELIVERY_ASSET"


def test_latest_attempt_failed_does_not_fall_back_to_older_ready(env: ExportEnv, client: TestClient) -> None:
    """R3 负样本：attempt1 READY、attempt2 FAILED ⇒ 必须拒绝，不得回落到 attempt1。"""
    content = _seed_content(env, versions=1)
    _seed_media_slot(
        env, content,
        renditions=(("DELIVERY", 1, "READY"), ("DELIVERY", 2, "FAILED")),
    )
    csrf = _login(client, env.settings, env.owner_id)
    resp = _post_export(client, csrf, env.project_id, [content.content_id])
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["reason"] == "NO_DELIVERY_ASSET"


def test_latest_ready_attempt_wins_over_older_ready(env: ExportEnv, client: TestClient) -> None:
    """R3 正样本：两次都 READY ⇒ 取 attempt 大的那张（文件对象不同可区分）。"""
    content = _seed_content(env, versions=1)
    _seed_media_slot(
        env, content,
        renditions=(("DELIVERY", 1, "READY"), ("DELIVERY", 2, "READY")),
    )
    with session_scope(env.settings) as session:
        newest = session.execute(
            text("SELECT file_object_id FROM media_renditions WHERE attempt_no = 2"
                 " AND media_version_id = ANY(:v)"),
            {"v": content.media_version_ids},
        ).scalar_one()
        repo = ExportRepository(session)
        assets = repo.delivery_assets(content.content_id)
    assert len(assets) == 1
    assert assets[0].attempt_no == 2 and assets[0].file_object_id == newest


def test_preview_only_rendition_is_not_a_delivery_asset(env: ExportEnv, client: TestClient) -> None:
    content = _seed_content(env, versions=1)
    _seed_media_slot(env, content, renditions=(("PREVIEW", 1, "READY"),))
    csrf = _login(client, env.settings, env.owner_id)
    resp = _post_export(client, csrf, env.project_id, [content.content_id])
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["reason"] == "NO_DELIVERY_ASSET"


def test_one_slot_short_of_images_rejects_whole_export(env: ExportEnv, client: TestClient) -> None:
    """两个位只有一张正式图（缺一张也算缺）；且同批另一条合格内容也不得被导出。"""
    good = _seed_content(env, title="好", versions=1)
    bad = _seed_content(env, title="缺", versions=1)
    _seed_media_slot(env, bad, sort_order=0)
    _seed_media_slot(env, bad, sort_order=1, renditions=(("DELIVERY", 1, "FAILED"),))
    csrf = _login(client, env.settings, env.owner_id)
    resp = _post_export(client, csrf, env.project_id, [good.content_id, bad.content_id])
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["contentItemId"] == str(bad.content_id)
    assert _export_rows(env, env.project_id) == []  # 整次拒绝，不留半截


# ===========================================================================
# 只读接缝 current_eligibility_hash（WP36 仲裁项①，2026-10-19）——
# 零新逻辑，逐字复用 freeze_content + eligibility_hash；这里只验证
# "重算的指纹会不会随正文改稿而变"这一个性质，不重测冻结口径本身
# （R2/R3 已在上面的用例覆盖）。
# ===========================================================================


def test_current_eligibility_hash_unchanged_without_a_new_version(env: ExportEnv, client: TestClient) -> None:
    """未改稿：同一内容连续两次重算指纹必须逐字相等。"""
    content = _seed_content(env, title="稳定", versions=1)
    with session_scope(env.settings) as session:
        repo = ExportRepository(session)
        first = x_service.current_eligibility_hash(repo, content_item_id=content.content_id)
        second = x_service.current_eligibility_hash(repo, content_item_id=content.content_id)
    assert first == second


def test_current_eligibility_hash_changes_after_a_new_version(env: ExportEnv, client: TestClient) -> None:
    """改稿后（追加一版 + 投影前进）：重算的指纹必须与改稿前不同。"""
    content = _seed_content(env, title="会改", versions=1)
    with session_scope(env.settings) as session:
        repo = ExportRepository(session)
        before = x_service.current_eligibility_hash(repo, content_item_id=content.content_id)

    with session_scope(env.settings) as session:
        conn = session.connection()
        node = _insert_version_node(
            conn, entity_type="CONTENT", entity_id=content.content_id,
            project_id=content.project_id, revision_no=2, created_by=env.owner_id,
        )
        conn.execute(
            text(
                "INSERT INTO content_versions (id, content_item_id, version_no,"
                " title, body, tags, bindings, payload, payload_hash,"
                " schema_version, created_by)"
                " VALUES (:id, :c, 2, :t, :b, '[]'::jsonb, '[]'::jsonb,"
                " '{}'::jsonb, :h, 1, :by)"
            ),
            {"id": node, "c": content.content_id, "t": "会改-v2",
             "b": "正文-v2-改过", "h": _hash(f"cv-{content.content_id}-2"),
             "by": env.owner_id},
        )
        conn.execute(
            text("UPDATE content_items SET current_version_id = :v,"
                 " current_version_no = 2 WHERE id = :c"),
            {"v": node, "c": content.content_id},
        )

    with session_scope(env.settings) as session:
        repo = ExportRepository(session)
        after = x_service.current_eligibility_hash(repo, content_item_id=content.content_id)
    assert after != before


def conn_scalar(session: Any, sql: str, params: dict[str, Any]) -> Any:
    return session.connection().execute(text(sql), params).scalar()


def test_current_eligibility_snapshot_matches_hash_and_current_version(
    env: ExportEnv, client: TestClient
) -> None:
    """S2（WP34）：快照二元组 = (当前版本节点, 与 current_eligibility_hash 逐字相同的指纹)。"""
    content = _seed_content(env, title="快照", versions=1)
    with session_scope(env.settings) as session:
        repo = ExportRepository(session)
        node_id, snap_hash = x_service.current_eligibility_snapshot(
            repo, content_item_id=content.content_id
        )
        plain_hash = x_service.current_eligibility_hash(repo, content_item_id=content.content_id)
        current = conn_scalar(
            session, "SELECT current_version_id FROM content_items WHERE id = :c",
            {"c": content.content_id},
        )
    assert snap_hash == plain_hash
    assert node_id == current


def test_current_eligibility_snapshot_refuses_like_the_hash_seam(
    env: ExportEnv, client: TestClient
) -> None:
    """拒绝路径与 current_eligibility_hash 相同：无正文版本 ⇒ ExportError，不降级。"""
    content = _seed_content(env, title="无版本", versions=0)
    with session_scope(env.settings) as session:
        repo = ExportRepository(session)
        with pytest.raises(x_service.ExportError):
            x_service.current_eligibility_snapshot(repo, content_item_id=content.content_id)
        with pytest.raises(x_service.ExportError):
            x_service.current_eligibility_hash(repo, content_item_id=content.content_id)


# ===========================================================================
# 幂等 / 确定性 / 入参 / 鉴权
# ===========================================================================


def test_same_key_same_body_replays_without_new_rows(env: ExportEnv, client: TestClient) -> None:
    content = _seed_content(env, versions=1)
    csrf = _login(client, env.settings, env.owner_id)
    first = _post_export(client, csrf, env.project_id, [content.content_id], key="k-1")
    again = _post_export(client, csrf, env.project_id, [content.content_id], key="k-1")
    assert first.status_code == 201 and again.status_code == 200, (first.text, again.text)
    a, b = first.json()["data"], again.json()["data"]
    assert a["created"] is True and b["created"] is False
    assert a["exportGroupId"] == b["exportGroupId"]
    assert a["zipSha256"] == b["zipSha256"]
    assert len(_export_rows(env, env.project_id)) == 1


def test_same_key_different_body_is_409(env: ExportEnv, client: TestClient) -> None:
    c1, c2 = _seed_content(env, versions=1), _seed_content(env, versions=1)
    csrf = _login(client, env.settings, env.owner_id)
    assert _post_export(client, csrf, env.project_id, [c1.content_id], key="k-2").status_code == 201
    resp = _post_export(client, csrf, env.project_id, [c2.content_id], key="k-2")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(_export_rows(env, env.project_id)) == 1


def test_replay_is_order_independent(env: ExportEnv, client: TestClient) -> None:
    c1, c2 = _seed_content(env, versions=1), _seed_content(env, versions=1)
    csrf = _login(client, env.settings, env.owner_id)
    first = _post_export(client, csrf, env.project_id, [c1.content_id, c2.content_id], key="k-3")
    swapped = _post_export(client, csrf, env.project_id, [c2.content_id, c1.content_id], key="k-3")
    assert first.status_code == 201 and swapped.status_code == 200, swapped.text


def test_no_key_is_not_idempotent_but_bytes_are_deterministic(env: ExportEnv, client: TestClient) -> None:
    """无键 ⇒ 两组；同输入 ⇒ 同 sha256（确定性打包），但组 id 不同。"""
    content = _seed_content(env, title="确定", versions=1)
    _seed_media_slot(env, content)
    csrf = _login(client, env.settings, env.owner_id)
    a = _export_ok(client, csrf, env, [content.content_id])
    b = _export_ok(client, csrf, env, [content.content_id])
    assert a["exportGroupId"] != b["exportGroupId"]
    assert a["zipSha256"] == b["zipSha256"] and a["zipByteSize"] == b["zipByteSize"]
    assert len(_export_rows(env, env.project_id)) == 2


def test_key_is_scoped_per_project(env: ExportEnv, client: TestClient) -> None:
    """同键用在另一个项目 ⇒ 互不影响（唯一索引含 project_id）。"""
    c1 = _seed_content(env, versions=1)
    c2 = _seed_content(env, project_id=env.other_project_id, versions=1)
    csrf = _login(client, env.settings, env.owner_id)
    assert _post_export(client, csrf, env.project_id, [c1.content_id], key="same").status_code == 201
    assert _post_export(client, csrf, env.other_project_id, [c2.content_id], key="same").status_code == 201


def test_concurrent_same_key_conflict_replays_instead_of_500(env: ExportEnv) -> None:
    """并发同键：胜者已提交、败者在 INSERT 撞唯一索引 ⇒ 回滚保存点后回放（不是 500）。

    确定性复现：让败者的"幂等预查"看不到胜者（预查返回空），
    于是它会继续打包并在 flush 时撞 `uq_export_items_idempotency`。
    """
    content = _seed_content(env, versions=1)
    ids = [content.content_id]

    def _run(*, hide_existing: bool):
        with session_scope(env.settings) as session:
            repo = ExportRepository(session)
            if hide_existing:
                real = repo.find_by_idempotency
                calls = {"n": 0}

                def _first_call_empty(**kw: Any):
                    calls["n"] += 1
                    return [] if calls["n"] == 1 else real(**kw)

                repo.find_by_idempotency = _first_call_empty  # type: ignore[method-assign]
            return x_service.create_export(
                repo, FileRepository(session), env.storage,
                project_id=env.project_id, content_item_ids=ids,
                export_format="STATIC", idempotency_key="race-1",
                created_by=env.owner_id,
            )

    winner = _run(hide_existing=False)
    loser = _run(hide_existing=True)
    assert winner.created is True and loser.created is False
    assert loser.export_group_id == winner.export_group_id
    assert len(_export_rows(env, env.project_id)) == 1


def test_validation_rejections_write_nothing(env: ExportEnv, client: TestClient) -> None:
    content = _seed_content(env, versions=1)
    csrf = _login(client, env.settings, env.owner_id)
    cid = str(content.content_id)

    empty = _post_export(client, csrf, env.project_id, [])
    assert empty.status_code == 422 and empty.json()["error"]["details"]["reason"] == "CONTENT_IDS_EMPTY"

    dup = client.post(f"{API}/projects/{env.project_id}/exports",
                      json={"contentItemIds": [cid, cid]}, headers=_wh(csrf))
    assert dup.status_code == 422 and dup.json()["error"]["details"]["reason"] == "CONTENT_IDS_DUPLICATE"

    many = client.post(f"{API}/projects/{env.project_id}/exports",
                       json={"contentItemIds": [str(uuid.uuid4()) for _ in range(51)]},
                       headers=_wh(csrf))
    assert many.status_code == 422 and many.json()["error"]["details"]["reason"] == "CONTENT_IDS_TOO_MANY"

    live = _post_export(client, csrf, env.project_id, [content.content_id], body_extra={"format": "LIVE"})
    assert live.status_code == 422 and live.json()["error"]["details"]["reason"] == "FORMAT_NOT_SUPPORTED"

    forged = _post_export(client, csrf, env.project_id, [content.content_id],
                          body_extra={"versionNodeId": str(content.version_ids[0])})
    assert forged.status_code == 422, "客户端不得指定版本"

    bad_key = _post_export(client, csrf, env.project_id, [content.content_id], key="x" * 161)
    assert bad_key.status_code == 422
    assert bad_key.json()["error"]["details"]["reason"] == "idempotency_key_invalid"

    assert _export_rows(env, env.project_id) == []


def test_cross_project_content_is_404_and_writes_nothing(env: ExportEnv, client: TestClient) -> None:
    foreign = _seed_content(env, project_id=env.other_project_id, versions=1)
    ghost = uuid.uuid4()
    csrf = _login(client, env.settings, env.owner_id)
    for cid in (foreign.content_id, ghost):
        resp = _post_export(client, csrf, env.project_id, [cid])
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["details"]["reason"] == "CONTENT_NOT_FOUND"
    # 跨项目与不存在同形：响应除 id 外不可区分
    assert _export_rows(env, env.project_id) == []
    assert _export_rows(env, env.other_project_id) == []


def test_non_member_gets_404_not_403(env: ExportEnv, client: TestClient) -> None:
    content = _seed_content(env, versions=1)
    csrf = _login(client, env.settings, env.outsider_id)
    assert _post_export(client, csrf, env.project_id, [content.content_id]).status_code == 404
    assert client.get(f"{API}/projects/{env.project_id}/exports").status_code == 404


def test_collaborator_and_admin_can_export(env: ExportEnv, client: TestClient) -> None:
    for user in (env.collab_id, env.admin_id):
        content = _seed_content(env, versions=1)
        csrf = _login(client, env.settings, user)
        assert _post_export(client, csrf, env.project_id, [content.content_id]).status_code == 201


def test_closed_project_denies_deliver_with_403_but_allows_view(env: ExportEnv, client: TestClient) -> None:
    """结项项目：DELIVER 被拒（403，且不是 CSRF 拒因），VIEW 仍可。"""
    content = _seed_content(env, project_id=env.closed_project_id, versions=1)
    csrf = _login(client, env.settings, env.owner_id)
    resp = _post_export(client, csrf, env.closed_project_id, [content.content_id])
    assert resp.status_code == 403, resp.text
    reason = (resp.json()["error"].get("details") or {}).get("reason")
    assert reason not in {"origin_mismatch", "referer_mismatch", "missing_origin_and_referer",
                          "missing_csrf_token", "csrf_token_mismatch"}, reason
    assert client.get(f"{API}/projects/{env.closed_project_id}/exports").status_code == 200
    assert _export_rows(env, env.closed_project_id) == []


def test_unauthenticated_is_rejected(env: ExportEnv, client: TestClient) -> None:
    resp = client.get(f"{API}/projects/{env.project_id}/exports")
    assert resp.status_code in (401, 403)


# ===========================================================================
# 下载 / 核验 / 列表
# ===========================================================================


def _artifact_path(env: ExportEnv, artifact_file_id: str) -> Path:
    with session_scope(env.settings) as session:
        located = ExportRepository(session).file_blob_key(uuid.UUID(artifact_file_id))
    assert located is not None
    return env.storage_root / located[1]


def test_download_streams_exact_bytes_with_etag_and_headers(env: ExportEnv, client: TestClient) -> None:
    content = _seed_content(env, title="下载", versions=1)
    _seed_media_slot(env, content)
    csrf = _login(client, env.settings, env.owner_id)
    data = _export_ok(client, csrf, env, [content.content_id])
    gid = data["exportGroupId"]

    resp = client.get(f"{API}/projects/{env.project_id}/exports/{gid}/download")
    assert resp.status_code == 200, resp.text
    assert hashlib.sha256(resp.content).hexdigest() == data["zipSha256"]
    assert len(resp.content) == data["zipByteSize"]
    assert resp.headers["etag"] == f'"{data["zipSha256"]}"'
    assert resp.headers["content-type"].startswith("application/zip")
    assert resp.headers["content-length"] == str(data["zipByteSize"])
    assert resp.headers["content-disposition"] == f'attachment; filename="export-{gid[:8]}.zip"'
    assert "private" in resp.headers["cache-control"]
    assert zipfile.ZipFile(io.BytesIO(resp.content)).testzip() is None


def test_download_if_none_match_returns_304_without_body(env: ExportEnv, client: TestClient) -> None:
    content = _seed_content(env, versions=1)
    csrf = _login(client, env.settings, env.owner_id)
    data = _export_ok(client, csrf, env, [content.content_id])
    url = f"{API}/projects/{env.project_id}/exports/{data['exportGroupId']}/download"
    hit = client.get(url, headers={"If-None-Match": f'"{data["zipSha256"]}"'})
    assert hit.status_code == 304 and hit.content == b""
    miss = client.get(url, headers={"If-None-Match": '"deadbeef"'})
    assert miss.status_code == 200 and len(miss.content) == data["zipByteSize"]


def test_download_and_verify_are_project_scoped(env: ExportEnv, client: TestClient) -> None:
    """别的项目的组 id、不存在的组 id、非成员 —— 全部 404，且同形。"""
    content = _seed_content(env, versions=1)
    csrf = _login(client, env.settings, env.owner_id)
    gid = _export_ok(client, csrf, env, [content.content_id])["exportGroupId"]
    for suffix in ("download", "verification"):
        cross = client.get(f"{API}/projects/{env.other_project_id}/exports/{gid}/{suffix}")
        ghost = client.get(f"{API}/projects/{env.project_id}/exports/{uuid.uuid4()}/{suffix}")
        assert cross.status_code == ghost.status_code == 404
        assert cross.json()["error"]["details"]["reason"] == "EXPORT_NOT_FOUND"
    outsider_csrf = _login(client, env.settings, env.outsider_id)
    assert outsider_csrf
    assert client.get(f"{API}/projects/{env.project_id}/exports/{gid}/download").status_code == 404


def test_missing_bytes_is_404_artifact_missing_not_truncated_200(env: ExportEnv, client: TestClient) -> None:
    content = _seed_content(env, versions=1)
    csrf = _login(client, env.settings, env.owner_id)
    data = _export_ok(client, csrf, env, [content.content_id])
    _artifact_path(env, data["artifactFileId"]).unlink()
    for suffix in ("download", "verification"):
        resp = client.get(f"{API}/projects/{env.project_id}/exports/{data['exportGroupId']}/{suffix}")
        assert resp.status_code == 404, (suffix, resp.text)
        assert resp.json()["error"]["details"]["reason"] == "ARTIFACT_MISSING"


def test_verification_matches_then_detects_tampering(env: ExportEnv, client: TestClient) -> None:
    content = _seed_content(env, versions=1)
    csrf = _login(client, env.settings, env.owner_id)
    data = _export_ok(client, csrf, env, [content.content_id])
    url = f"{API}/projects/{env.project_id}/exports/{data['exportGroupId']}/verification"

    ok = client.get(url).json()["data"]
    assert ok["matches"] is True
    assert ok["expectedSha256"] == ok["actualSha256"] == data["zipSha256"]
    assert ok["expectedByteSize"] == ok["actualByteSize"] == data["zipByteSize"]

    path = _artifact_path(env, data["artifactFileId"])
    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 0xFF  # 同长度翻一个字节：只比字节数发现不了
    path.write_bytes(bytes(raw))
    bad = client.get(url)
    assert bad.status_code == 200  # 核验结论是响应内容，不是错误码
    body = bad.json()["data"]
    assert body["matches"] is False
    assert body["actualByteSize"] == body["expectedByteSize"]
    assert body["actualSha256"] != body["expectedSha256"]

    path.write_bytes(bytes(raw)[:-1])  # 截断：长度也变
    short = client.get(url).json()["data"]
    assert short["matches"] is False and short["actualByteSize"] == short["expectedByteSize"] - 1


def test_list_groups_collapses_rows_orders_desc_and_paginates(env: ExportEnv, client: TestClient) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    c1, c2, c3 = (_seed_content(env, versions=1) for _ in range(3))
    g_batch = _export_ok(client, csrf, env, [c1.content_id, c2.content_id])
    g_single = _export_ok(client, csrf, env, [c3.content_id])
    g_again = _export_ok(client, csrf, env, [c1.content_id])
    assert len(_export_rows(env, env.project_id)) == 4  # 2 + 1 + 1 行，3 个组

    url = f"{API}/projects/{env.project_id}/exports"
    full = client.get(url, params={"limit": 100}).json()
    ids = [g["exportGroupId"] for g in full["data"]]
    assert ids == [g_again["exportGroupId"], g_single["exportGroupId"], g_batch["exportGroupId"]]
    assert full["data"][2]["itemCount"] == 2 and len(full["data"][2]["contentItemIds"]) == 2
    assert full["page"]["hasMore"] is False

    seen: list[str] = []
    cursor = None
    for _ in range(5):
        params: dict[str, Any] = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = client.get(url, params=params).json()
        seen.extend(g["exportGroupId"] for g in page["data"])
        if not page["page"]["hasMore"]:
            break
        cursor = page["page"]["nextCursor"]
    assert seen == ids  # 不漏不重不乱序


def test_list_is_project_scoped(env: ExportEnv, client: TestClient) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    mine = _seed_content(env, versions=1)
    other = _seed_content(env, project_id=env.other_project_id, versions=1)
    _export_ok(client, csrf, env, [mine.content_id])
    assert _post_export(client, csrf, env.other_project_id, [other.content_id]).status_code == 201
    listed = client.get(f"{API}/projects/{env.project_id}/exports").json()["data"]
    assert len(listed) == 1 and listed[0]["contentItemIds"] == [str(mine.content_id)]


# ===========================================================================
# 数据库行为：不可变 / 组一致性 / CHECK（断言 sqlstate，不只断言"抛了"）
# ===========================================================================


def _row_kwargs(env: ExportEnv, content: SeededContent, *, group: uuid.UUID,
                artifact: uuid.UUID, count: int = 1, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": uuid.uuid4(), "p": content.project_id, "g": group,
        "c": content.content_id, "v": content.version_ids[0],
        "eh": _hash("e"), "kind": "SINGLE" if count == 1 else "BATCH_ZIP",
        "fmt": "STATIC", "cnt": count, "art": artifact, "zs": _hash("zip"),
        "zb": 10, "rh": _hash("req"), "by": env.owner_id,
    }
    base.update(over)
    return base


_INSERT_SQL = text(
    "INSERT INTO export_items (id, project_id, export_group_id, content_item_id,"
    " version_node_id, eligibility_hash, export_kind, export_format, item_count,"
    " artifact_file_id, zip_sha256, zip_byte_size, request_hash, created_by)"
    " VALUES (:id, :p, :g, :c, :v, :eh, :kind, :fmt, :cnt, :art, :zs, :zb, :rh, :by)"
)


def _sqlstate_of_commit(env: ExportEnv, statements: list[dict[str, Any]]) -> str | None:
    """在**一个事务**里按序插入，提交；返回失败的 sqlstate（成功 ⇒ None）。

    组一致性是延迟触发器，只在提交时检查 ⇒ 探针必须走到 COMMIT。
    """
    engine = get_engine(env.settings)
    conn = engine.connect()
    try:
        trans = conn.begin()
        try:
            for params in statements:
                conn.execute(_INSERT_SQL, params)
            trans.commit()
            return None
        except DBAPIError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None)
            trans.rollback()
            return sqlstate
    finally:
        conn.close()


def test_group_trigger_accepts_a_consistent_two_row_group(env: ExportEnv) -> None:
    c1, c2 = _seed_content(env, versions=1), _seed_content(env, versions=1)
    art = _store_png(env, env.project_id)
    g = uuid.uuid4()
    assert _sqlstate_of_commit(env, [
        _row_kwargs(env, c1, group=g, artifact=art, count=2),
        _row_kwargs(env, c2, group=g, artifact=art, count=2),
    ]) is None
    assert len(_export_rows(env, env.project_id)) == 2


def test_group_trigger_rejects_inconsistent_group_attributes(env: ExportEnv) -> None:
    c1, c2 = _seed_content(env, versions=1), _seed_content(env, versions=1)
    art = _store_png(env, env.project_id)
    g = uuid.uuid4()
    state = _sqlstate_of_commit(env, [
        _row_kwargs(env, c1, group=g, artifact=art, count=2),
        _row_kwargs(env, c2, group=g, artifact=art, count=2, zs=_hash("other")),
    ])
    assert state == "23514"
    assert _export_rows(env, env.project_id) == []


def test_group_trigger_rejects_incomplete_group(env: ExportEnv) -> None:
    """声明 2 条实际只有 1 行 ⇒ 提交时拒绝（防"半个组"）。"""
    c1 = _seed_content(env, versions=1)
    art = _store_png(env, env.project_id)
    state = _sqlstate_of_commit(env, [
        _row_kwargs(env, c1, group=uuid.uuid4(), artifact=art, count=2),
    ])
    assert state == "23514"
    assert _export_rows(env, env.project_id) == []


def test_group_trigger_rejects_content_from_another_project(env: ExportEnv) -> None:
    foreign = _seed_content(env, project_id=env.other_project_id, versions=1)
    art = _store_png(env, env.project_id)
    state = _sqlstate_of_commit(env, [
        _row_kwargs(env, foreign, group=uuid.uuid4(), artifact=art, count=1,
                    p=env.project_id),
    ])
    assert state == "23514"
    assert _export_rows(env, env.project_id) == []


@pytest.mark.parametrize(
    ("over", "label"),
    [
        ({"kind": "BATCH_ZIP"}, "export_kind_matches_count（1 条却是 BATCH_ZIP）"),
        ({"fmt": "LIVE"}, "export_format_valid"),
        ({"zb": 0}, "zip_byte_size_positive"),
        ({"zs": "XYZ" * 21 + "A"}, "zip_sha256_shape"),
        ({"eh": "a" * 63}, "eligibility_hash_shape"),
        ({"rh": "G" * 64}, "request_hash_shape"),
        ({"cnt": 0}, "item_count_range"),
    ],
)
def test_check_constraints_reject_bad_rows(env: ExportEnv, over: dict[str, Any], label: str) -> None:
    content = _seed_content(env, versions=1)
    art = _store_png(env, env.project_id)
    state = _sqlstate_of_commit(env, [_row_kwargs(env, content, group=uuid.uuid4(), artifact=art, **over)])
    assert state == "23514", f"{label} 应被 CHECK 拒绝（sqlstate={state}）"


def test_duplicate_content_in_group_is_unique_violation(env: ExportEnv) -> None:
    c1 = _seed_content(env, versions=1)
    art = _store_png(env, env.project_id)
    g = uuid.uuid4()
    state = _sqlstate_of_commit(env, [
        _row_kwargs(env, c1, group=g, artifact=art, count=2),
        _row_kwargs(env, c1, group=g, artifact=art, count=2),
    ])
    assert state == "23505"


@pytest.mark.parametrize("statement", [
    "UPDATE export_items SET zip_byte_size = zip_byte_size + 1",
    "DELETE FROM export_items",
])
def test_export_items_are_immutable(env: ExportEnv, client: TestClient, statement: str) -> None:
    content = _seed_content(env, versions=1)
    csrf = _login(client, env.settings, env.owner_id)
    _export_ok(client, csrf, env, [content.content_id])
    engine = get_engine(env.settings)
    with engine.connect() as conn:
        trans = conn.begin()
        with pytest.raises(DBAPIError) as info:
            conn.execute(text(statement + " WHERE project_id = :p"), {"p": env.project_id})
        assert getattr(info.value.orig, "sqlstate", None) == "23001"
        trans.rollback()
    assert len(_export_rows(env, env.project_id)) == 1


def test_invariants_survive_full_http_flow_for_batch(env: ExportEnv, client: TestClient) -> None:
    """批量导出经真实 HTTP 落库后，组级属性逐列一致、行数 = item_count。"""
    contents = [_seed_content(env, title=f"批{i}", versions=1) for i in range(3)]
    _seed_media_slot(env, contents[1])
    csrf = _login(client, env.settings, env.owner_id)
    data = _export_ok(client, csrf, env, [c.content_id for c in contents])
    assert data["exportKind"] == "BATCH_ZIP" and data["itemCount"] == 3
    rows = _export_rows(env, env.project_id)
    assert len(rows) == 3
    for col in ("export_group_id", "artifact_file_id", "zip_sha256", "zip_byte_size",
                "created_by", "request_hash", "export_kind", "item_count"):
        assert len({str(r[col]) for r in rows}) == 1, col
    assert len({r["eligibility_hash"] for r in rows}) == 3  # 逐内容各异


def test_zip_member_layout_for_batch(env: ExportEnv, client: TestClient) -> None:
    c1 = _seed_content(env, title="甲", versions=1)
    c2 = _seed_content(env, title="乙/../x", versions=1)
    _seed_media_slot(env, c2)
    csrf = _login(client, env.settings, env.owner_id)
    data = _export_ok(client, csrf, env, [c1.content_id, c2.content_id])
    zf = _read_zip(env, uuid.UUID(data["artifactFileId"]))
    names = zf.namelist()
    assert names[0].startswith("001_甲-v1_") and names[0].endswith("/content.txt")
    assert names[1].startswith("002_") and names[2].endswith("/image_01.png")
    for n in names:
        assert ".." not in n.split("/") and not n.startswith("/") and "\\" not in n, n
    assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in zf.infolist())


# ===========================================================================
# 纯函数 / 取值域 / 流式内存
# ===========================================================================


def test_request_hash_ignores_order_but_not_content_or_format() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    assert x_types.request_hash((a, b), "STATIC") == x_types.request_hash((b, a), "STATIC")
    assert x_types.request_hash((a, b), "STATIC") != x_types.request_hash((a,), "STATIC")
    assert x_types.request_hash((a,), "STATIC") != x_types.request_hash((a,), "LIVE")


def test_safe_segment_strips_traversal_and_falls_back() -> None:
    for dirty in ("../x", "a/b", "a\\b", "a\x00b", "..", "."):
        cleaned = x_schemas.safe_segment(dirty, fallback="untitled")
        assert "/" not in cleaned and "\\" not in cleaned and "\x00" not in cleaned
        assert cleaned not in ("", ".", "..")
    assert x_schemas.safe_segment("   ", fallback="untitled") == "untitled"
    assert len(x_schemas.safe_segment("长" * 200, fallback="u")) <= 40


def test_value_domains_match_media_side_literals() -> None:
    """与 media 侧同值（平级不互 import，各写一份 ⇒ 由本判据对账）。"""
    from rednote.modules.media import types as m_types

    assert x_types.RENDITION_KIND_DELIVERY == m_types.RENDITION_DELIVERY == "DELIVERY"
    assert x_types.RENDITION_STATE_READY == m_types.RENDITION_STATE_READY == "READY"


def test_export_kind_for_boundaries() -> None:
    assert x_types.export_kind_for(1) == "SINGLE"
    assert x_types.export_kind_for(2) == "BATCH_ZIP"
    assert x_types.export_kind_for(50) == "BATCH_ZIP"


def test_build_zip_is_byte_deterministic_and_streams_to_file(tmp_path: Path) -> None:
    from rednote.modules.exports.types import DeliveryAsset, FrozenContent

    fid = uuid.uuid4()
    frozen = FrozenContent(
        content_item_id=uuid.UUID(int=1), version_node_id=uuid.UUID(int=2), version_no=1,
        title="T", body="B",
        assets=(DeliveryAsset(uuid.UUID(int=3), uuid.UUID(int=4), 0, uuid.UUID(int=5), 1, fid, 4, "png"),),
    )

    def build() -> bytes:
        target = tmp_path / f"{uuid.uuid4().hex}.zip"
        with target.open("wb") as handle:
            x_schemas.build_zip(handle, [frozen], lambda _f: iter((b"ab", b"cd")))
        return target.read_bytes()

    assert build() == build()


def test_packing_memory_stays_far_below_total_size(tmp_path: Path) -> None:
    """R1：打 64 MiB 图片，峰值内存必须远小于 64 MiB（含探针有效性对照）。"""
    import tracemalloc
    from rednote.modules.exports.types import DeliveryAsset, FrozenContent

    chunk = b"\x01" * (1024 * 1024)
    total_chunks = 64

    def source(_f):
        for _ in range(total_chunks):
            yield chunk

    frozen = FrozenContent(
        content_item_id=uuid.UUID(int=1), version_node_id=uuid.UUID(int=2), version_no=1,
        title="大", body="B",
        assets=(DeliveryAsset(uuid.UUID(int=3), uuid.UUID(int=4), 0, uuid.UUID(int=5), 1,
                              uuid.uuid4(), len(chunk) * total_chunks, "png"),),
    )

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        with (tmp_path / "big.zip").open("wb") as handle:
            x_schemas.build_zip(handle, [frozen], source)
        _, streaming_peak = tracemalloc.get_traced_memory()

        tracemalloc.reset_peak()
        sink = io.BytesIO()  # 对照：BytesIO 目标一定随总量增长，证明探针能抓到"进内存"
        x_schemas.build_zip(sink, [frozen], source)
        _, in_memory_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert (tmp_path / "big.zip").stat().st_size > 64 * 1024 * 1024
    assert in_memory_peak > 48 * 1024 * 1024, "探针失效：BytesIO 对照没抓到内存增长"
    assert streaming_peak < 8 * 1024 * 1024, f"流式打包峰值 {streaming_peak} 字节，疑似进了内存"

