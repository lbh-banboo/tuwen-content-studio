"""WP36 `delivery` 包的判据落点 —— 纯函数层 / 数据库行为层 / HTTP 端到端层。

## 夹具纪律（整段照 `tests/test_exports_wp35.py:107-146` 的既有模式）

· 一律跑**私有库**（`REDNOTE_TEST_DATABASE_URL`），不碰共享 `rednote_test`；
· 只重建本包 4 张表 + 3 个触发器，其余表按迁移已就绪；
· 触发器**先 DROP 再 CREATE**（`CREATE TRIGGER` 不幂等，实测会 `DuplicateObject`）；
· 每个用例新造 user / project / content，**按项目隔离**，不数全库行；
· 造数走裸 SQL，且**同一事务提交**（延迟约束触发器要求）；
· 数据库探针断言 **sqlstate**，不只断言"抛了异常"（被拒的结论对 ≠ 原因对）。

## 本文件最要紧的两条断言

1. **逐列变异断言由磁盘派生**（`test_every_frozen_column_rejects_mutation`）：
   冻结列名单取 `models.frozen_delivery_columns()`，对**每一列**做一次变异
   UPDATE 并断言 sqlstate=23514。**没有任何地方由人手写冻结列清单** ——
   人写的名单在加列时会静默漏掉那一列，而库全绿、测试全绿。
2. **NULL 方向的变异**（`test_frozen_nullable_column_cannot_be_nulled_out`）：
   把一个非空的冻结可空列改成 NULL。这条是 `IS DISTINCT FROM` 与 `<>` 的
   **唯一区分点** —— 用 `<>` 写触发器时它会静默通过。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import DateTime, String, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import DBAPIError

from conftest import (
    cross_package_foreign_keys,
    drop_own_tables,
    truncate_targets,
)

# FK 目标表必须在 `create_all` 之前 import（否则 NoReferencedTableError）。
import rednote.modules.content.models  # noqa: F401
import rednote.modules.exports.models  # noqa: F401
import rednote.modules.files.models  # noqa: F401
import rednote.modules.gateway.models  # noqa: F401
import rednote.modules.identity.models  # noqa: F401
import rednote.modules.jobs.models  # noqa: F401
import rednote.modules.media.models  # noqa: F401
import rednote.modules.projects.models  # noqa: F401
import rednote.modules.prompts.models  # noqa: F401
import rednote.modules.versions.models  # noqa: F401

from rednote.modules.delivery import ddl as d_ddl
from rednote.modules.delivery import export_bridge as d_bridge
from rednote.modules.delivery import schemas as d_schemas
from rednote.modules.delivery import service as d_service
from rednote.modules.delivery import types as d_types
from rednote.modules.delivery.models import (
    MUTABLE_DELIVERY_COLUMNS,
    OWN_TABLES,
    Delivery,
    KocRecord,
    frozen_delivery_columns,
)
from rednote.modules.delivery.repository import DeliveryRepository
from rednote.modules.files.models import STORAGE_BACKEND_LOCAL
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

#: DROP 顺序 = 引用方在前（`publication_links → publications → deliveries → koc_records`）。
DROP_ORDER = ("publication_links", "publications", "deliveries", "koc_records")
_TABLE_NAMES = tuple(OWN_TABLES)

CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"


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
    """只重建本包 4 张表 + 3 个触发器；前后对账跨包外键集合不变。"""
    reset_engines()
    engine = get_engine(_make_settings())

    fks_before = cross_package_foreign_keys(engine, _TABLE_NAMES)
    dropped, kept = drop_own_tables(engine, DROP_ORDER)
    Delivery.metadata.create_all(
        engine,
        tables=[Delivery.metadata.tables[name] for name in OWN_TABLES],
    )
    fks_after = cross_package_foreign_keys(engine, _TABLE_NAMES)
    assert fks_after == fks_before, (
        f"建表夹具改变了跨包外键集合：before={sorted(fks_before)} "
        f"after={sorted(fks_after)}（dropped={dropped} kept={kept}）"
    )

    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(truncate_targets(engine, DROP_ORDER))))
        for _t, func_ddl, trigger_ddl, drop_trigger, _df in d_ddl.all_trigger_ddl():
            conn.execute(text(drop_trigger))  # 幂等关键：CREATE TRIGGER 不幂等
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
# 环境：用户 / 项目 / 存储
# ===========================================================================


@dataclass(frozen=True)
class DeliveryEnv:
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
            display_name=f"WP36-{role}",
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
            project_code=f"WP36-{uuid.uuid4().hex[:8].upper()}",
            name="WP36 测试项目",
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
def env(tmp_path: Path) -> DeliveryEnv:
    root = tmp_path / "storage"
    settings = _make_settings(root)
    owner = _create_user(settings)
    collab = _create_user(settings)
    outsider = _create_user(settings)
    admin = _create_user(settings, role=ROLE_ADMIN)
    return DeliveryEnv(
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
# 造数 helper（裸 SQL）
# ===========================================================================


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


@dataclass
class SeededContent:
    content_id: uuid.UUID
    project_id: uuid.UUID
    version_ids: list[uuid.UUID] = field(default_factory=list)


def _insert_version_node(
    conn, *, entity_id: uuid.UUID, project_id: uuid.UUID, revision_no: int,
    created_by: uuid.UUID,
) -> uuid.UUID:
    node_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO version_nodes (id, entity_type, entity_id, project_id,"
            " revision_no, schema_version, payload_hash, created_by)"
            " VALUES (:id, 'CONTENT', :e, :p, :r, 1, :h, :by)"
        ),
        {"id": node_id, "e": entity_id, "p": project_id, "r": revision_no,
         "h": _hash(f"{entity_id}-{revision_no}"), "by": created_by},
    )
    return node_id


def _insert_content_version(
    conn, content: SeededContent, *, version_no: int, title: str, body: str,
    created_by: uuid.UUID, advance_projection: bool = True,
) -> uuid.UUID:
    node = _insert_version_node(
        conn, entity_id=content.content_id, project_id=content.project_id,
        revision_no=version_no, created_by=created_by,
    )
    conn.execute(
        text(
            "INSERT INTO content_versions (id, content_item_id, version_no,"
            " title, body, tags, bindings, payload, payload_hash,"
            " schema_version, created_by)"
            " VALUES (:id, :c, :no, :t, :b, '[]'::jsonb, '[]'::jsonb,"
            " '{}'::jsonb, :h, 1, :by)"
        ),
        {"id": node, "c": content.content_id, "no": version_no, "t": title,
         "b": body, "h": _hash(f"cv-{content.content_id}-{version_no}-{body}"),
         "by": created_by},
    )
    if advance_projection:
        conn.execute(
            text("UPDATE content_items SET current_version_id = :v,"
                 " current_version_no = :n WHERE id = :c"),
            {"v": node, "n": version_no, "c": content.content_id},
        )
    content.version_ids.append(node)
    return node


def _seed_content(
    env: DeliveryEnv, *, project_id: uuid.UUID | None = None, title: str = "标题"
) -> SeededContent:
    """造一条纯文字内容（1 版正文、零媒体位）—— 纯文字导出是合法的。"""
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
        _insert_content_version(
            conn, out, version_no=1, title=f"{title}-v1", body="正文-v1",
            created_by=env.owner_id,
        )
    return out


def _amend_content(env: DeliveryEnv, content: SeededContent) -> uuid.UUID:
    """改稿：追加一版正文并让投影前进（WP29 的编辑路径在库层的等价效果）。"""
    next_no = len(content.version_ids) + 1
    with session_scope(env.settings) as session:
        return _insert_content_version(
            session.connection(), content, version_no=next_no,
            title=f"改过-v{next_no}", body=f"正文-v{next_no}-改过",
            created_by=env.owner_id,
        )


# ===========================================================================
# HTTP 夹具
# ===========================================================================


@pytest.fixture
def client(env: DeliveryEnv) -> Iterator[TestClient]:
    """真 app + **在 `main.py` 尚未接线时补挂本包 router**。

    ⚠️ `main.py` 是 §7.1 第 2 行的共享禁改文件，`include_router` 是**主 agent 的
    串行装配动作**。分支不能改它，但本包的端到端判据必须跑在真 app 上（鉴权、
    CSRF、错误体包裹、分页全在中间件与 `platform` 里）。

    **挂载必须幂等**：主 agent 收口后 `main.py` 会 `include_router`，此时再挂
    一次会让每条路由重复注册。探测依据用**路径前缀**而不是 router 对象身份
    （`include_router` 会把 router 复制成新对象，身份比较不可靠）——与
    `test_review_wp30_api.py` 的同款处置一致。
    """
    from rednote.main import create_app
    from rednote.modules.delivery.api import router as delivery_router

    app: FastAPI = create_app(env.settings)
    prefix = env.settings.api_prefix
    existing = {
        getattr(route, "path", None) for route in app.routes
    }
    ours = {f"{prefix}{route.path}" for route in delivery_router.routes}
    if not (existing & ours):
        app.include_router(delivery_router, prefix=prefix)
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, settings: Settings, user_id: uuid.UUID) -> str:
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


# ===========================================================================
# 与 WP35 的接缝：真的走一次导出，拿一个真的 export_item
# ===========================================================================


@dataclass(frozen=True)
class SeededExport:
    export_item_id: uuid.UUID
    export_group_id: uuid.UUID
    content_item_id: uuid.UUID
    version_node_id: uuid.UUID
    eligibility_hash: str


def _create_export(
    client: TestClient, csrf: str, env: DeliveryEnv, content: SeededContent
) -> SeededExport:
    """经 WP35 的真端点导出一条内容，再读回那一行 `export_items`。

    **不**自己往 `export_items` 插裸 SQL：那样造出来的 `eligibility_hash`
    与"当前重算的指纹"没有任何关系，资格复验的用例就变成了自证。
    """
    resp = client.post(
        f"{API}/projects/{content.project_id}/exports",
        json={"contentItemIds": [str(content.content_id)]},
        headers=_wh(csrf),
    )
    assert resp.status_code == 201, resp.text
    gid = uuid.UUID(resp.json()["data"]["exportGroupId"])
    with session_scope(env.settings) as session:
        row = session.execute(
            text(
                "SELECT id, content_item_id, version_node_id, eligibility_hash"
                " FROM export_items WHERE export_group_id = :g"
            ),
            {"g": gid},
        ).one()
    return SeededExport(
        export_item_id=row[0], export_group_id=gid, content_item_id=row[1],
        version_node_id=row[2], eligibility_hash=row[3],
    )


# ===========================================================================
# 读库 helper
# ===========================================================================


def _delivery_rows(env: DeliveryEnv, project_id: uuid.UUID) -> list[Any]:
    with session_scope(env.settings) as session:
        return list(
            session.execute(
                text("SELECT * FROM deliveries WHERE project_id = :p"
                     " ORDER BY created_at, id"),
                {"p": project_id},
            ).mappings().all()
        )


def _delivery_row(env: DeliveryEnv, delivery_id: uuid.UUID) -> dict[str, Any]:
    with session_scope(env.settings) as session:
        return dict(
            session.execute(
                text("SELECT * FROM deliveries WHERE id = :i"), {"i": delivery_id}
            ).mappings().one()
        )


def _publication_rows(env: DeliveryEnv, project_id: uuid.UUID) -> list[Any]:
    with session_scope(env.settings) as session:
        return list(
            session.execute(
                text("SELECT * FROM publications WHERE project_id = :p ORDER BY created_at"),
                {"p": project_id},
            ).mappings().all()
        )


def _sqlstate_of(env: DeliveryEnv, statements: list[tuple[str, dict[str, Any]]]) -> str | None:
    """在**一个事务**里按序执行并提交；返回失败的 sqlstate（成功 ⇒ None）。

    一致性触发器是 `DEFERRABLE INITIALLY DEFERRED`，只在提交时检查
    ⇒ 探针**必须走到 COMMIT**，否则"坏值写进去了"会被误读成"防线失效"。
    """
    engine = get_engine(env.settings)
    conn = engine.connect()
    try:
        trans = conn.begin()
        try:
            for sql, params in statements:
                conn.execute(text(sql), params)
            trans.commit()
            return None
        except DBAPIError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None)
            trans.rollback()
            return sqlstate
    finally:
        conn.close()


# ===========================================================================
# HTTP 快捷调用
# ===========================================================================


def _post_koc(client: TestClient, csrf: str, project_id: uuid.UUID, **body: Any):
    payload: dict[str, Any] = {"displayName": "素人甲"}
    payload.update(body)
    return client.post(
        f"{API}/projects/{project_id}/koc-records", json=payload, headers=_wh(csrf)
    )


def _post_delivery(
    client: TestClient, csrf: str, project_id: uuid.UUID, export_item_id: uuid.UUID,
    *, koc_record_id: uuid.UUID | None = None, note: str | None = None,
    key: str | None = None,
):
    body: dict[str, Any] = {"exportItemId": str(export_item_id)}
    if koc_record_id is not None:
        body["kocRecordId"] = str(koc_record_id)
    if note is not None:
        body["note"] = note
    extra = {"Idempotency-Key": key} if key is not None else {}
    return client.post(
        f"{API}/projects/{project_id}/deliveries", json=body, headers=_wh(csrf, **extra)
    )


def _post_publication(
    client: TestClient, csrf: str, project_id: uuid.UUID, *, key_value: str,
    delivery_id: uuid.UUID | None = None, note_id: str | None = None,
    state: str = "DRAFT", published_at: str | None = None, key: str | None = None,
):
    body: dict[str, Any] = {"publicationKey": key_value, "state": state}
    if delivery_id is not None:
        body["deliveryId"] = str(delivery_id)
    if note_id is not None:
        body["noteId"] = note_id
    if published_at is not None:
        body["publishedAt"] = published_at
    extra = {"Idempotency-Key": key} if key is not None else {}
    return client.post(
        f"{API}/projects/{project_id}/publications", json=body, headers=_wh(csrf, **extra)
    )


def _confirm(client: TestClient, csrf: str, delivery_id: str):
    return client.post(
        f"{API}/deliveries/{delivery_id}/confirmations", headers=_wh(csrf)
    )


def _cancel(client: TestClient, csrf: str, delivery_id: str):
    return client.post(
        f"{API}/deliveries/{delivery_id}/cancellations", headers=_wh(csrf)
    )


def _replace(
    client: TestClient, csrf: str, delivery_id: str, export_item_id: uuid.UUID,
    *, note: str | None = None, key: str | None = None,
):
    body: dict[str, Any] = {"exportItemId": str(export_item_id)}
    if note is not None:
        body["note"] = note
    extra = {"Idempotency-Key": key} if key is not None else {}
    return client.post(
        f"{API}/deliveries/{delivery_id}/replacements", json=body,
        headers=_wh(csrf, **extra),
    )


@dataclass(frozen=True)
class Prepared:
    """一条已落库的 `PREPARED` 交付 + 它的上游导出行与内容。"""

    content: SeededContent
    export: SeededExport
    koc_id: uuid.UUID
    delivery_id: str
    view: dict[str, Any]


def _prepare(client: TestClient, csrf: str, env: DeliveryEnv, *, title: str = "交付") -> Prepared:
    content = _seed_content(env, title=title)
    export = _create_export(client, csrf, env, content)
    koc = _post_koc(client, csrf, env.project_id)
    assert koc.status_code == 201, koc.text
    koc_id = uuid.UUID(koc.json()["data"]["id"])
    resp = _post_delivery(
        client, csrf, env.project_id, export.export_item_id, koc_record_id=koc_id
    )
    assert resp.status_code == 201, resp.text
    view = resp.json()["data"]
    return Prepared(
        content=content, export=export, koc_id=koc_id,
        delivery_id=view["id"], view=view,
    )


# ===========================================================================
# 1. 最小闭环
# ===========================================================================


def test_mvp_loop_koc_delivery_confirm_replace_publication(
    env: DeliveryEnv, client: TestClient
) -> None:
    """建 KOC → 建交付（PREPARED）→ 标记已交付 → 替换 → 登记发布，全程真 HTTP。"""
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env)

    # 建交付：三个冻结字段逐列等于导出行；初始一定是 PREPARED、无交付时间
    assert prepared.view["state"] == "PREPARED"
    assert prepared.view["deliveredAt"] is None
    assert prepared.view["deliveredBy"] is None
    assert prepared.view["contentItemId"] == str(prepared.export.content_item_id)
    assert prepared.view["versionNodeId"] == str(prepared.export.version_node_id)
    assert prepared.view["eligibilityHash"] == prepared.export.eligibility_hash
    assert prepared.view["replacesDeliveryId"] is None

    # 标记已交付
    confirmed = _confirm(client, csrf, prepared.delivery_id)
    assert confirmed.status_code == 200, confirmed.text
    marked = confirmed.json()["data"]
    assert marked["state"] == "DELIVERED"
    assert marked["deliveredAt"] is not None
    assert marked["deliveredBy"] == str(env.owner_id)

    # 改稿 → 重新导出 → 替换
    _amend_content(env, prepared.content)
    new_export = _create_export(client, csrf, env, prepared.content)
    assert new_export.eligibility_hash != prepared.export.eligibility_hash
    replaced = _replace(
        client, csrf, prepared.delivery_id, new_export.export_item_id, note="第二版"
    )
    assert replaced.status_code == 201, replaced.text
    new_row = replaced.json()["data"]
    assert new_row["replacesDeliveryId"] == prepared.delivery_id
    assert new_row["state"] == "PREPARED"
    assert new_row["versionNodeId"] == str(new_export.version_node_id)
    assert new_row["kocRecordId"] == str(prepared.koc_id)  # 继承旧行的 KOC

    # 登记发布
    pub = _post_publication(
        client, csrf, env.project_id, key_value="NOTE-0001",
        delivery_id=uuid.UUID(new_row["id"]), note_id="abc123",
        state="PUBLISHED", published_at="2026-10-19T08:00:00+00:00",
    )
    assert pub.status_code == 201, pub.text
    pub_data = pub.json()["data"]
    assert pub_data["publicationKey"] == "NOTE-0001"
    assert pub_data["state"] == "PUBLISHED"
    assert pub_data["contentState"] == "UNVERIFIED"  # 本包不抓取
    assert pub_data["urlCanonical"] is None  # WP37 规范器产，本包恒 NULL

    assert len(_delivery_rows(env, env.project_id)) == 2  # 替换是追加


# ===========================================================================
# 2. 「下载不计交付」
# ===========================================================================


def test_download_never_marks_delivery(env: DeliveryEnv, client: TestClient) -> None:
    """调 WP35 的 download 端点 3 次（200/200/304）⇒ 交付行四列**一个字节不变**。"""
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env, title="下载")
    before = _delivery_row(env, uuid.UUID(prepared.delivery_id))

    url = (
        f"{API}/projects/{env.project_id}/exports"
        f"/{prepared.export.export_group_id}/download"
    )
    first = client.get(url)
    assert first.status_code == 200, first.text
    etag = first.headers["etag"]
    assert client.get(url).status_code == 200
    assert client.get(url, headers={"If-None-Match": etag}).status_code == 304

    after = _delivery_row(env, uuid.UUID(prepared.delivery_id))
    for column in ("state", "delivered_at", "delivered_by", "lock_version", "updated_at"):
        assert before[column] == after[column], column
    assert after["state"] == "PREPARED" and after["delivered_at"] is None
    assert len(_delivery_rows(env, env.project_id)) == 1


def test_state_timestamps_check_blocks_delivered_at_without_state(
    env: DeliveryEnv, client: TestClient
) -> None:
    """DB 层：想置 `delivered_at` 必须同时把 `state` 推到 DELIVERED（双向等式 CHECK）。"""
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env)
    did = prepared.delivery_id

    only_timestamp = _sqlstate_of(env, [(
        "UPDATE deliveries SET delivered_at = now(), delivered_by = :u WHERE id = :i",
        {"i": did, "u": env.owner_id},
    )])
    assert only_timestamp == CHECK_VIOLATION

    only_state = _sqlstate_of(env, [(
        "UPDATE deliveries SET state = 'DELIVERED' WHERE id = :i", {"i": did},
    )])
    assert only_state == CHECK_VIOLATION

    # 时间戳与操作人必须同形（`ck_deliveries_delivered_by_shape`）
    no_actor = _sqlstate_of(env, [(
        "UPDATE deliveries SET state = 'DELIVERED', delivered_at = now() WHERE id = :i",
        {"i": did},
    )])
    assert no_actor == CHECK_VIOLATION


# ===========================================================================
# 3. 「原交付版本不随改稿变化」
# ===========================================================================


def test_amending_content_does_not_touch_the_delivery_row(
    env: DeliveryEnv, client: TestClient
) -> None:
    """改稿（追加一版 + 投影前进）后，原交付行**全列逐列不变**。"""
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env, title="不随改稿变")
    _confirm(client, csrf, prepared.delivery_id)
    before = _delivery_row(env, uuid.UUID(prepared.delivery_id))

    _amend_content(env, prepared.content)

    after = _delivery_row(env, uuid.UUID(prepared.delivery_id))
    assert before == after, "改稿后交付行发生了变化"

    # 端点返回的也必须还是老版本
    detail = client.get(f"{API}/deliveries/{prepared.delivery_id}").json()["data"]
    assert detail["versionNodeId"] == str(prepared.content.version_ids[0])
    assert detail["versionNodeId"] != str(prepared.content.version_ids[1])


def _mutation_expression(column: Any) -> str:
    """按列类型生成一个"一定与原值不同"的 SQL 表达式。**由磁盘派生，不写名单。**"""
    column_type = column.type
    if isinstance(column_type, PGUUID):
        return "gen_random_uuid()"
    if isinstance(column_type, DateTime):
        return "now() + interval '1 day'"
    if isinstance(column_type, String | Text):
        return "'wp36-mutated'"
    raise AssertionError(
        f"没有为列 {column.name}（{column_type!r}）准备变异表达式 —— "
        "新增了一种列类型就必须在这里登记，否则这一列的冻结性就没被验过"
    )


def test_every_frozen_column_rejects_mutation(
    env: DeliveryEnv, client: TestClient
) -> None:
    """★ 逐列变异断言：**由磁盘派生**的冻结列名单，每一列都必须被 DB 拒。

    名单 = `deliveries` 全部列 − `MUTABLE_DELIVERY_COLUMNS`（7 列白名单）。
    **不许人写冻结列名单** —— 人写的在加列时会静默漏掉，而库全绿、测试全绿。
    """
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env, title="变异")
    did = prepared.delivery_id

    frozen = frozen_delivery_columns()
    all_columns = set(Delivery.__table__.columns.keys())
    assert set(frozen) | set(MUTABLE_DELIVERY_COLUMNS) == all_columns
    assert not set(frozen) & set(MUTABLE_DELIVERY_COLUMNS)
    assert len(frozen) >= 10, f"冻结列只有 {len(frozen)} 列，派生疑似失效：{frozen}"

    rejected: list[str] = []
    for name in frozen:
        column = Delivery.__table__.columns[name]
        state = _sqlstate_of(env, [(
            f"UPDATE deliveries SET {name} = {_mutation_expression(column)}"
            " WHERE id = :i",
            {"i": did},
        )])
        assert state == CHECK_VIOLATION, (
            f"冻结列 {name} 的变异**没有**被 DB 拒绝（sqlstate={state}）—— "
            "这张表有一扇后门"
        )
        rejected.append(name)
    assert rejected == list(frozen)

    # 对照：白名单里的列必须真的能改，否则状态机本身就不工作
    ok = _sqlstate_of(env, [(
        "UPDATE deliveries SET note = 'wp36-note',"
        " lock_version = lock_version + 1 WHERE id = :i",
        {"i": did},
    )])
    assert ok is None, f"可变列也被拒了（sqlstate={ok}）—— 触发器拦得过宽"


def test_frozen_nullable_column_cannot_be_nulled_out(
    env: DeliveryEnv, client: TestClient
) -> None:
    """★ `IS DISTINCT FROM` 与 `<>` 的**唯一区分点**：把非空的冻结可空列改成 NULL。

    用 `<>` 写触发器时 `NULL <> 'x'` 求值为 **NULL**，`IF NULL THEN` 不进分支
    ⇒ 这条变异会**静默通过**，而表看起来"有防线"。
    """
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env, title="NULL 方向")
    did = prepared.delivery_id
    assert _delivery_row(env, uuid.UUID(did))["koc_record_id"] is not None

    to_null = _sqlstate_of(env, [(
        "UPDATE deliveries SET koc_record_id = NULL WHERE id = :i", {"i": did},
    )])
    assert to_null == CHECK_VIOLATION, "冻结可空列被改成 NULL 没有被拒 —— 触发器用了 <>"

    # 反方向：本来是 NULL 的冻结列被填上值，同样要拒
    other = _post_delivery(
        client, csrf, env.project_id,
        _create_export(client, csrf, env, _seed_content(env, title="无 KOC")).export_item_id,
    )
    assert other.status_code == 201, other.text
    other_id = other.json()["data"]["id"]
    assert _delivery_row(env, uuid.UUID(other_id))["koc_record_id"] is None
    from_null = _sqlstate_of(env, [(
        "UPDATE deliveries SET koc_record_id = :k WHERE id = :i",
        {"i": other_id, "k": prepared.koc_id},
    )])
    assert from_null == CHECK_VIOLATION


def test_delivery_rows_can_still_be_deleted_and_state_can_advance(
    env: DeliveryEnv, client: TestClient
) -> None:
    """列级冻结**不是**整行不可变：状态机要能走，DELETE 不被触发器拦。

    这条是"拦得过宽"的负样本 —— 若误用了 `guard_immutable`，状态机当场死。
    """
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env, title="可变")
    assert _cancel(client, csrf, prepared.delivery_id).status_code == 200
    removed = _sqlstate_of(env, [(
        "DELETE FROM deliveries WHERE id = :i", {"i": prepared.delivery_id},
    )])
    assert removed is None


# ===========================================================================
# 4. 替换是追加
# ===========================================================================


def test_replace_appends_and_leaves_the_old_row_byte_identical(
    env: DeliveryEnv, client: TestClient
) -> None:
    """替换后旧行**全列不变**（`lock_version` 不变是"没人 UPDATE 过它"的硬证据）。"""
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env, title="替换")
    _confirm(client, csrf, prepared.delivery_id)
    before = _delivery_row(env, uuid.UUID(prepared.delivery_id))
    assert before["state"] == "DELIVERED"

    _amend_content(env, prepared.content)
    new_export = _create_export(client, csrf, env, prepared.content)
    resp = _replace(client, csrf, prepared.delivery_id, new_export.export_item_id)
    assert resp.status_code == 201, resp.text

    after = _delivery_row(env, uuid.UUID(prepared.delivery_id))
    assert before == after, "替换动了旧行"
    assert after["state"] == "DELIVERED"
    assert after["lock_version"] == before["lock_version"]
    assert len(_delivery_rows(env, env.project_id)) == 2


def test_a_delivery_can_only_be_replaced_once(
    env: DeliveryEnv, client: TestClient
) -> None:
    """替换链不是替换树：第二次替换同一条 ⇒ 409 `ALREADY_REPLACED`；裸 SQL ⇒ 23505。"""
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env, title="只替一次")
    _amend_content(env, prepared.content)
    first = _create_export(client, csrf, env, prepared.content)
    assert _replace(client, csrf, prepared.delivery_id, first.export_item_id).status_code == 201

    _amend_content(env, prepared.content)
    second = _create_export(client, csrf, env, prepared.content)
    again = _replace(client, csrf, prepared.delivery_id, second.export_item_id)
    assert again.status_code == 409, again.text
    err = again.json()["error"]
    assert err["code"] == "INVALID_TRANSITION"
    assert err["details"]["reason"] == "ALREADY_REPLACED"

    # 绕过服务层也不行：部分唯一索引兜底
    rows = _delivery_rows(env, env.project_id)
    assert len(rows) == 2
    state = _sqlstate_of(env, [(
        "INSERT INTO deliveries (project_id, export_item_id, content_item_id,"
        " version_node_id, state, eligibility_hash, replaces_delivery_id,"
        " request_hash, created_by)"
        " VALUES (:p, :e, :c, :v, 'PREPARED', :eh, :r, :rh, :by)",
        {"p": env.project_id, "e": second.export_item_id,
         "c": second.content_item_id, "v": second.version_node_id,
         "eh": second.eligibility_hash, "r": prepared.delivery_id,
         "rh": _hash("probe"), "by": env.owner_id},
    )])
    assert state == UNIQUE_VIOLATION


def test_replace_rejects_cancelled_and_cross_content(
    env: DeliveryEnv, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env, title="替换拒绝")

    # 换内容不是替换
    other_content = _seed_content(env, title="另一条内容")
    other_export = _create_export(client, csrf, env, other_content)
    cross = _replace(client, csrf, prepared.delivery_id, other_export.export_item_id)
    assert cross.status_code == 404, cross.text
    assert cross.json()["error"]["details"]["reason"] == "EXPORT_ITEM_NOT_FOUND"

    # 已取消的交付没有可替换的对象
    assert _cancel(client, csrf, prepared.delivery_id).status_code == 200
    _amend_content(env, prepared.content)
    fresh = _create_export(client, csrf, env, prepared.content)
    cancelled = _replace(client, csrf, prepared.delivery_id, fresh.export_item_id)
    assert cancelled.status_code == 409, cancelled.text
    assert cancelled.json()["error"]["details"]["reason"] == "DELIVERY_CANCELLED"


# ===========================================================================
# 5. 资格复验（仲裁项① = D2 只读接缝）
# ===========================================================================


def test_confirm_passes_when_content_has_not_been_amended(
    env: DeliveryEnv, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env, title="未改稿")
    resp = _confirm(client, csrf, prepared.delivery_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["state"] == "DELIVERED"


def test_confirm_is_rejected_after_the_content_was_amended(
    env: DeliveryEnv, client: TestClient
) -> None:
    """★ 复验是**真的重算**：改稿后指纹不等 ⇒ 409，交付行一列不变。"""
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env, title="已改稿")
    before = _delivery_row(env, uuid.UUID(prepared.delivery_id))

    _amend_content(env, prepared.content)

    resp = _confirm(client, csrf, prepared.delivery_id)
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "INVALID_TRANSITION"
    assert err["details"]["reason"] == "ELIGIBILITY_STALE"
    assert err["details"]["frozenEligibilityHash"] == prepared.export.eligibility_hash
    assert (
        err["details"]["currentEligibilityHash"]
        != err["details"]["frozenEligibilityHash"]
    )
    assert _delivery_row(env, uuid.UUID(prepared.delivery_id)) == before


def test_bridge_forwards_the_readonly_seam_and_does_not_reimplement_it(
    env: DeliveryEnv, client: TestClient
) -> None:
    """桥接层与 WP35 的只读接缝必须**逐字节同值** —— 两份口径就是静默分叉的起点。"""
    from rednote.modules.exports.repository import ExportRepository
    from rednote.modules.exports.service import current_eligibility_hash as upstream

    content = _seed_content(env, title="同源")
    with session_scope(env.settings) as session:
        theirs = upstream(ExportRepository(session), content_item_id=content.content_id)
        ours = d_bridge.current_eligibility_hash(
            session, content_item_id=content.content_id
        )
    assert ours == theirs


def test_unverifiable_eligibility_is_409_not_a_silent_pass(
    env: DeliveryEnv, client: TestClient
) -> None:
    """算不出当前指纹（导出后又加了一个还没出图的配图位）⇒ 409，**不**降级成"复验通过"。

    为什么用"加配图位"而不是"删正文版本"：`content_versions` 是**不可变表**
    （`trg_content_versions_immutable` 拦 DELETE），删不掉。而"导出完才想起来要配图"
    是真实业务路径。
    """
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env, title="算不出")
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO media_items (id, project_id, content_item_id,"
                " paragraph_ref, sort_order, state, current_version_no,"
                " created_by, lock_version)"
                " VALUES (:id, :p, :c, :pr, 0, 'PENDING', 0, :by, 0)"
            ),
            {"id": uuid.uuid4(), "p": env.project_id,
             "c": prepared.content.content_id,
             "pr": f"p0-{uuid.uuid4().hex[:8]}", "by": env.owner_id},
        )
    resp = _confirm(client, csrf, prepared.delivery_id)
    assert resp.status_code == 409, resp.text
    details = resp.json()["error"]["details"]
    assert details["reason"] == "ELIGIBILITY_UNAVAILABLE"
    assert details["upstreamReason"] == "NO_DELIVERY_ASSET"
    assert _delivery_row(env, uuid.UUID(prepared.delivery_id))["state"] == "PREPARED"


# ===========================================================================
# 6. 状态机
# ===========================================================================


def test_confirm_twice_is_idempotent_and_delivered_at_does_not_move(
    env: DeliveryEnv, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env, title="重复确认")
    first = _confirm(client, csrf, prepared.delivery_id)
    assert first.status_code == 200 and first.json()["data"]["created"] is True
    snapshot = _delivery_row(env, uuid.UUID(prepared.delivery_id))

    again = _confirm(client, csrf, prepared.delivery_id)
    assert again.status_code == 200, again.text
    assert again.json()["data"]["created"] is False
    assert again.json()["data"]["deliveredAt"] == first.json()["data"]["deliveredAt"]
    assert _delivery_row(env, uuid.UUID(prepared.delivery_id)) == snapshot


def test_illegal_transitions_are_409_invalid_transition(
    env: DeliveryEnv, client: TestClient
) -> None:
    """`CANCELLED → DELIVERED` 与 `DELIVERED → CANCELLED` 都是 409，不是 400/422。"""
    csrf = _login(client, env.settings, env.owner_id)
    a = _prepare(client, csrf, env, title="取消后确认")
    assert _cancel(client, csrf, a.delivery_id).status_code == 200
    resp = _confirm(client, csrf, a.delivery_id)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "INVALID_TRANSITION"
    assert resp.json()["error"]["details"]["reason"] == "INVALID_TRANSITION"

    b = _prepare(client, csrf, env, title="确认后取消")
    assert _confirm(client, csrf, b.delivery_id).status_code == 200
    resp = _cancel(client, csrf, b.delivery_id)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["reason"] == "INVALID_TRANSITION"


def test_cancel_twice_is_idempotent(env: DeliveryEnv, client: TestClient) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env, title="重复取消")
    assert _cancel(client, csrf, prepared.delivery_id).json()["data"]["created"] is True
    snapshot = _delivery_row(env, uuid.UUID(prepared.delivery_id))
    again = _cancel(client, csrf, prepared.delivery_id)
    assert again.status_code == 200 and again.json()["data"]["created"] is False
    assert _delivery_row(env, uuid.UUID(prepared.delivery_id)) == snapshot


# ===========================================================================
# 7. 幂等（三个端点各两条 + 并发）
# ===========================================================================


def test_delivery_create_same_key_same_body_replays(
    env: DeliveryEnv, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    content = _seed_content(env, title="幂等建交付")
    export = _create_export(client, csrf, env, content)
    first = _post_delivery(client, csrf, env.project_id, export.export_item_id, key="d-1")
    again = _post_delivery(client, csrf, env.project_id, export.export_item_id, key="d-1")
    assert first.status_code == 201 and again.status_code == 200, (first.text, again.text)
    assert first.json()["data"]["created"] is True
    assert again.json()["data"]["created"] is False
    assert first.json()["data"]["id"] == again.json()["data"]["id"]
    assert len(_delivery_rows(env, env.project_id)) == 1


def test_delivery_create_same_key_different_body_is_409(
    env: DeliveryEnv, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    e1 = _create_export(client, csrf, env, _seed_content(env, title="甲"))
    e2 = _create_export(client, csrf, env, _seed_content(env, title="乙"))
    assert _post_delivery(client, csrf, env.project_id, e1.export_item_id, key="d-2").status_code == 201
    resp = _post_delivery(client, csrf, env.project_id, e2.export_item_id, key="d-2")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(_delivery_rows(env, env.project_id)) == 1


def test_replace_same_key_same_body_replays_and_different_body_is_409(
    env: DeliveryEnv, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    a = _prepare(client, csrf, env, title="幂等替换A")
    b = _prepare(client, csrf, env, title="幂等替换B")
    _amend_content(env, a.content)
    _amend_content(env, b.content)
    a_export = _create_export(client, csrf, env, a.content)
    b_export = _create_export(client, csrf, env, b.content)

    first = _replace(client, csrf, a.delivery_id, a_export.export_item_id, key="r-1")
    again = _replace(client, csrf, a.delivery_id, a_export.export_item_id, key="r-1")
    assert first.status_code == 201 and again.status_code == 200, (first.text, again.text)
    assert first.json()["data"]["id"] == again.json()["data"]["id"]

    # ★ 同一个键用在**另一条**交付的替换上 ⇒ 必须 409（载荷里有 `replaces`），
    #    否则"替换 B"会静默回放"替换 A"的结果 —— 用户以为换了版其实没换。
    cross = _replace(client, csrf, b.delivery_id, b_export.export_item_id, key="r-1")
    assert cross.status_code == 409, cross.text
    assert cross.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_publication_same_key_same_body_replays_and_different_body_is_409(
    env: DeliveryEnv, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    first = _post_publication(client, csrf, env.project_id, key_value="P-1", key="p-1")
    again = _post_publication(client, csrf, env.project_id, key_value="P-1", key="p-1")
    assert first.status_code == 201 and again.status_code == 200, (first.text, again.text)
    assert first.json()["data"]["id"] == again.json()["data"]["id"]

    other = _post_publication(client, csrf, env.project_id, key_value="P-2", key="p-1")
    assert other.status_code == 409, other.text
    assert other.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(_publication_rows(env, env.project_id)) == 1


def test_delivery_create_key_is_scoped_per_project(
    env: DeliveryEnv, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    mine = _create_export(client, csrf, env, _seed_content(env))
    theirs = _create_export(
        client, csrf, env, _seed_content(env, project_id=env.other_project_id)
    )
    assert _post_delivery(client, csrf, env.project_id, mine.export_item_id, key="same").status_code == 201
    assert _post_delivery(
        client, csrf, env.other_project_id, theirs.export_item_id, key="same"
    ).status_code == 201


def test_concurrent_same_key_delivery_replays_instead_of_500(env: DeliveryEnv, client: TestClient) -> None:
    """并发同键：败者在 INSERT 撞 `uq_deliveries_idempotency` ⇒ 回滚保存点后回放。

    确定性复现：让败者的"幂等预查"看不到胜者（预查首次返回 None），
    于是它会继续 INSERT 并在 flush 时撞唯一索引。照
    `tests/test_exports_wp35.py:777-809` 的手法。
    """
    csrf = _login(client, env.settings, env.owner_id)
    export = _create_export(client, csrf, env, _seed_content(env, title="并发"))

    def _run(*, hide_existing: bool):
        with session_scope(env.settings) as session:
            repo = DeliveryRepository(session)
            if hide_existing:
                real = repo.find_delivery_by_idempotency
                calls = {"n": 0}

                def _first_call_empty(**kw: Any):
                    calls["n"] += 1
                    return None if calls["n"] == 1 else real(**kw)

                repo.find_delivery_by_idempotency = _first_call_empty  # type: ignore[method-assign]
            return d_service.create_delivery(
                repo,
                project_id=env.project_id,
                export_item_id=export.export_item_id,
                koc_record_id=None,
                note=None,
                idempotency_key="race-1",
                created_by=env.owner_id,
            )

    winner = _run(hide_existing=False)
    winner_id = winner.delivery.id
    loser = _run(hide_existing=True)
    assert winner.created is True and loser.created is False
    assert loser.delivery.id == winner_id
    assert len(_delivery_rows(env, env.project_id)) == 1


def test_concurrent_same_key_publication_replays_instead_of_500(
    env: DeliveryEnv, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    assert csrf

    def _run(*, hide_existing: bool):
        with session_scope(env.settings) as session:
            repo = DeliveryRepository(session)
            if hide_existing:
                real = repo.find_publication_by_idempotency
                by_key = repo.find_publication_by_key
                calls = {"n": 0, "k": 0}

                def _first_call_empty(**kw: Any):
                    calls["n"] += 1
                    return None if calls["n"] == 1 else real(**kw)

                def _first_key_empty(**kw: Any):
                    calls["k"] += 1
                    return None if calls["k"] == 1 else by_key(**kw)

                repo.find_publication_by_idempotency = _first_call_empty  # type: ignore[method-assign]
                repo.find_publication_by_key = _first_key_empty  # type: ignore[method-assign]
            return d_service.create_publication(
                repo,
                project_id=env.project_id,
                publication_key="RACE-1",
                delivery_id=None,
                note_id=None,
                note_url=None,
                state="DRAFT",
                published_at=None,
                idempotency_key="prace-1",
                created_by=env.owner_id,
            )

    winner = _run(hide_existing=False)
    winner_id = winner.publication.id
    loser = _run(hide_existing=True)
    assert winner.created is True and loser.created is False
    assert loser.publication.id == winner_id
    assert len(_publication_rows(env, env.project_id)) == 1


def test_no_key_means_no_idempotency(env: DeliveryEnv, client: TestClient) -> None:
    """无键 ⇒ 不做幂等，每次一行新的（协议 §1.3 通用语义）。"""
    csrf = _login(client, env.settings, env.owner_id)
    export = _create_export(client, csrf, env, _seed_content(env, title="无键"))
    a = _post_delivery(client, csrf, env.project_id, export.export_item_id)
    b = _post_delivery(client, csrf, env.project_id, export.export_item_id)
    assert a.status_code == b.status_code == 201
    assert a.json()["data"]["id"] != b.json()["data"]["id"]
    assert len(_delivery_rows(env, env.project_id)) == 2


def test_oversized_idempotency_key_is_422(env: DeliveryEnv, client: TestClient) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    export = _create_export(client, csrf, env, _seed_content(env))
    resp = _post_delivery(
        client, csrf, env.project_id, export.export_item_id, key="x" * 161
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["reason"] == "idempotency_key_invalid"
    assert _delivery_rows(env, env.project_id) == []


# ===========================================================================
# 8. 发布登记
# ===========================================================================


def test_duplicate_business_key_is_409_duplicate_publication(
    env: DeliveryEnv, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    assert _post_publication(client, csrf, env.project_id, key_value="DUP-1").status_code == 201
    resp = _post_publication(client, csrf, env.project_id, key_value="DUP-1", note_id="x")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "DUPLICATE_PUBLICATION"
    assert resp.json()["error"]["details"]["reason"] == "PUBLICATION_KEY_TAKEN"
    assert len(_publication_rows(env, env.project_id)) == 1
    # 业务键只在项目内唯一
    assert _post_publication(
        client, csrf, env.other_project_id, key_value="DUP-1"
    ).status_code == 201


def test_publication_key_format_is_enforced(env: DeliveryEnv, client: TestClient) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    for bad in ("有逗号,的", "带 空格", "a" * 65, "", "斜杠/不行"):
        resp = _post_publication(client, csrf, env.project_id, key_value=bad)
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json()["error"]["details"]["reason"] == "PUBLICATION_KEY_INVALID"
    assert _publication_rows(env, env.project_id) == []


def test_published_state_requires_published_at_both_ways(
    env: DeliveryEnv, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    missing = _post_publication(client, csrf, env.project_id, key_value="PS-1", state="PUBLISHED")
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"]["details"]["reason"] == "PUBLISHED_AT_SHAPE"

    extra = _post_publication(
        client, csrf, env.project_id, key_value="PS-2", state="DRAFT",
        published_at="2026-10-19T08:00:00+00:00",
    )
    assert extra.status_code == 422, extra.text
    assert extra.json()["error"]["details"]["reason"] == "PUBLISHED_AT_SHAPE"
    assert _publication_rows(env, env.project_id) == []


def test_publication_content_state_is_always_unverified_and_canonical_is_null(
    env: DeliveryEnv, client: TestClient
) -> None:
    """本包不抓取 ⇒ 一律 `UNVERIFIED`；客户端**不能**自己声称已核验。"""
    csrf = _login(client, env.settings, env.owner_id)
    resp = _post_publication(client, csrf, env.project_id, key_value="CS-1")
    assert resp.json()["data"]["contentState"] == "UNVERIFIED"

    forged = client.post(
        f"{API}/projects/{env.project_id}/publications",
        json={"publicationKey": "CS-2", "state": "DRAFT", "contentState": "VERIFIED"},
        headers=_wh(csrf),
    )
    assert forged.status_code == 422, "客户端不得指定 contentState"
    forged_url = client.post(
        f"{API}/projects/{env.project_id}/publications",
        json={"publicationKey": "CS-3", "state": "DRAFT", "urlCanonical": "https://x"},
        headers=_wh(csrf),
    )
    assert forged_url.status_code == 422, "urlCanonical 是 WP37 规范器产，客户端不得传"

    rows = _publication_rows(env, env.project_id)
    assert len(rows) == 1 and rows[0]["url_canonical"] is None


def test_publication_delivery_must_belong_to_the_project(
    env: DeliveryEnv, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env, title="跨项目映射")
    resp = _post_publication(
        client, csrf, env.other_project_id, key_value="X-1",
        delivery_id=uuid.UUID(prepared.delivery_id),
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["details"]["reason"] == "DELIVERY_NOT_FOUND"


# ===========================================================================
# 9. 鉴权
# ===========================================================================


def test_non_member_gets_404_not_403(env: DeliveryEnv, client: TestClient) -> None:
    owner_csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, owner_csrf, env, title="非成员")

    csrf = _login(client, env.settings, env.outsider_id)
    assert _post_koc(client, csrf, env.project_id).status_code == 404
    assert client.get(f"{API}/projects/{env.project_id}/deliveries").status_code == 404
    assert client.get(f"{API}/deliveries/{prepared.delivery_id}").status_code == 404
    assert _confirm(client, csrf, prepared.delivery_id).status_code == 404


def test_unknown_delivery_id_is_404_with_the_same_shape(
    env: DeliveryEnv, client: TestClient
) -> None:
    """不存在的 id ⇒ 404 `DELIVERY_NOT_FOUND`。

    「别的项目的交付 id」这一侧由 `test_publication_delivery_must_belong_to_the_project`
    与 `test_non_member_gets_404_not_403` 覆盖：item 级路径先按 id 反查项目再鉴权，
    所以"跨项目"对调用者的表现就是"看不到那个项目" ⇒ 同形 404。
    """
    csrf = _login(client, env.settings, env.owner_id)
    assert csrf
    resp = client.get(f"{API}/deliveries/{uuid.uuid4()}")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["details"]["reason"] == "DELIVERY_NOT_FOUND"


def _error_shape(resp, own_id: str) -> dict[str, Any]:
    """404 响应体里除「自己的 id」之外的全部内容。把 id 换成占位符再比，
    这样「带出了别的项目 id」「message 不同」「details 多/少字段」都会让两边不相等。"""
    err = resp.json()["error"]
    raw = json.dumps(err, ensure_ascii=False, sort_keys=True)
    return json.loads(raw.replace(own_id, "<ID>"))


def test_non_member_404_body_is_indistinguishable_from_not_found_on_item_paths(
    env: DeliveryEnv, client: TestClient
) -> None:
    """item 级路径：「别项目的交付」与「不存在的交付」除 id 外响应体必须逐字相同。

    原先只断言状态码都是 404；而非成员那条走 `resolve_project_access` 的默认 404
    （message=「项目不存在或无权访问」，details 带**所属项目 id**），与不存在的
    （message=「交付记录不存在」，details 带 reason）不同形 ⇒ 调用者能区分
    「存在但无权」与「不存在」，并拿到项目 id。四个 item 级端点都要同形。
    """
    owner_csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, owner_csrf, env, title="同形")
    export_id = prepared.export.export_item_id
    csrf = _login(client, env.settings, env.outsider_id)

    def hit(delivery_id: str) -> list[Any]:
        return [
            client.get(f"{API}/deliveries/{delivery_id}"),
            _confirm(client, csrf, delivery_id),
            _cancel(client, csrf, delivery_id),
            _replace(client, csrf, delivery_id, export_id),
        ]

    foreign = hit(prepared.delivery_id)
    missing_id = str(uuid.uuid4())
    missing = hit(missing_id)
    for f, m in zip(foreign, missing, strict=True):
        assert f.status_code == m.status_code == 404
        assert _error_shape(f, prepared.delivery_id) == _error_shape(m, missing_id)
    # 项目 id 不得出现在任何一条非成员响应里
    for f in foreign:
        assert str(env.project_id) not in f.text, f.text


def test_collaborator_and_admin_can_deliver(env: DeliveryEnv, client: TestClient) -> None:
    for user in (env.collab_id, env.admin_id):
        csrf = _login(client, env.settings, user)
        content = _seed_content(env, title=f"权限-{user}")
        export = _create_export(client, csrf, env, content)
        assert _post_delivery(client, csrf, env.project_id, export.export_item_id).status_code == 201


def test_closed_project_denies_deliver_but_allows_view(
    env: DeliveryEnv, client: TestClient
) -> None:
    """结项项目：`DELIVER` 被既有权限矩阵硬拒（403，且不是 CSRF 拒因），`VIEW` 仍可。"""
    csrf = _login(client, env.settings, env.owner_id)
    csrf_reasons = {
        "origin_mismatch", "referer_mismatch", "missing_origin_and_referer",
        "missing_csrf_token", "csrf_token_mismatch",
    }
    for resp in (
        _post_koc(client, csrf, env.closed_project_id),
        _post_delivery(client, csrf, env.closed_project_id, uuid.uuid4()),
        _post_publication(client, csrf, env.closed_project_id, key_value="C-1"),
    ):
        assert resp.status_code == 403, resp.text
        reason = (resp.json()["error"].get("details") or {}).get("reason")
        assert reason not in csrf_reasons, reason

    assert client.get(f"{API}/projects/{env.closed_project_id}/deliveries").status_code == 200
    assert client.get(f"{API}/projects/{env.closed_project_id}/publications").status_code == 200
    assert _delivery_rows(env, env.closed_project_id) == []


def test_unauthenticated_is_rejected(env: DeliveryEnv, client: TestClient) -> None:
    resp = client.get(f"{API}/projects/{env.project_id}/deliveries")
    assert resp.status_code in (401, 403)


# ===========================================================================
# 10. 入参与归属
# ===========================================================================


def test_export_item_must_exist_and_belong_to_the_project(
    env: DeliveryEnv, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    foreign = _create_export(
        client, csrf, env, _seed_content(env, project_id=env.other_project_id)
    )
    for item_id in (uuid.uuid4(), foreign.export_item_id):
        resp = _post_delivery(client, csrf, env.project_id, item_id)
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["details"]["reason"] == "EXPORT_ITEM_NOT_FOUND"
    assert _delivery_rows(env, env.project_id) == []


def test_koc_must_belong_to_the_project(env: DeliveryEnv, client: TestClient) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    foreign_koc = _post_koc(client, csrf, env.other_project_id)
    assert foreign_koc.status_code == 201
    export = _create_export(client, csrf, env, _seed_content(env))
    resp = _post_delivery(
        client, csrf, env.project_id, export.export_item_id,
        koc_record_id=uuid.UUID(foreign_koc.json()["data"]["id"]),
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["details"]["reason"] == "KOC_NOT_FOUND"


def test_client_cannot_pick_the_version(env: DeliveryEnv, client: TestClient) -> None:
    """`extra="forbid"`：客户端传 `versionNodeId` / `contentItemId` ⇒ 当场 422。"""
    csrf = _login(client, env.settings, env.owner_id)
    content = _seed_content(env, title="不许传版本")
    export = _create_export(client, csrf, env, content)
    for forged in ("versionNodeId", "contentItemId", "eligibilityHash", "state"):
        resp = client.post(
            f"{API}/projects/{env.project_id}/deliveries",
            json={"exportItemId": str(export.export_item_id), forged: "x"},
            headers=_wh(csrf),
        )
        assert resp.status_code == 422, (forged, resp.text)
    assert _delivery_rows(env, env.project_id) == []


def test_blank_koc_display_name_is_422(env: DeliveryEnv, client: TestClient) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    resp = _post_koc(client, csrf, env.project_id, displayName="   ")
    assert resp.status_code == 422, resp.text
    details = resp.json()["error"]["details"]
    assert details["reason"] == "DISPLAY_NAME_BLANK"
    assert details["fieldErrors"][0]["field"] == "displayName"

    trimmed = _post_koc(client, csrf, env.project_id, displayName="  张三  ")
    assert trimmed.status_code == 201
    assert trimmed.json()["data"]["displayName"] == "张三"


def test_koc_home_url_is_not_unique_and_canonical_stays_null(
    env: DeliveryEnv, client: TestClient
) -> None:
    """同链不同人是合法业务 ⇒ **不做**唯一；`homeUrlCanonical` 恒 NULL（WP37 产）。"""
    csrf = _login(client, env.settings, env.owner_id)
    url = "https://www.xiaohongshu.com/user/profile/abc?xsec=1"
    for name in ("甲", "乙"):
        resp = _post_koc(client, csrf, env.project_id, displayName=name, platformHomeUrl=url)
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["homeUrlCanonical"] is None

    forged = _post_koc(
        client, csrf, env.project_id, displayName="丙", homeUrlCanonical="https://x"
    )
    assert forged.status_code == 422, "homeUrlCanonical 是 WP37 规范器产，客户端不得传"


# ===========================================================================
# 11. 列表 / 投影
# ===========================================================================


def test_lists_are_project_scoped_and_paginate(env: DeliveryEnv, client: TestClient) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    ids: list[str] = []
    for i in range(3):
        export = _create_export(client, csrf, env, _seed_content(env, title=f"列表{i}"))
        resp = _post_delivery(client, csrf, env.project_id, export.export_item_id)
        ids.append(resp.json()["data"]["id"])
    other_export = _create_export(
        client, csrf, env, _seed_content(env, project_id=env.other_project_id)
    )
    _post_delivery(client, csrf, env.other_project_id, other_export.export_item_id)

    url = f"{API}/projects/{env.project_id}/deliveries"
    full = client.get(url, params={"limit": 100}).json()
    assert [d["id"] for d in full["data"]] == list(reversed(ids))
    assert full["page"]["hasMore"] is False

    seen: list[str] = []
    cursor = None
    for _ in range(5):
        params: dict[str, Any] = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = client.get(url, params=params).json()
        seen.extend(d["id"] for d in page["data"])
        if not page["page"]["hasMore"]:
            break
        cursor = page["page"]["nextCursor"]
    assert seen == list(reversed(ids))  # 不漏不重不乱序


def test_unmarked_exports_projection_counts_exports_not_downloads(
    env: DeliveryEnv, client: TestClient
) -> None:
    """「已**导出**未标记」：导出后未标记 ⇒ 在列；标记已交付 ⇒ 出列。

    口径名字刻意不叫「已下载未标记」—— 本仓没有下载事件表。
    """
    csrf = _login(client, env.settings, env.owner_id)
    url = f"{API}/projects/{env.project_id}/unmarked-exports"
    assert client.get(url).json()["data"] == []

    prepared = _prepare(client, csrf, env, title="投影")
    listed = client.get(url).json()["data"]
    assert [r["contentItemId"] for r in listed] == [str(prepared.content.content_id)]
    assert listed[0]["lastExportAt"] is not None

    # 下载不影响投影（下载不计交付）
    client.get(
        f"{API}/projects/{env.project_id}/exports"
        f"/{prepared.export.export_group_id}/download"
    )
    assert len(client.get(url).json()["data"]) == 1

    assert _confirm(client, csrf, prepared.delivery_id).status_code == 200
    assert client.get(url).json()["data"] == []


# ===========================================================================
# 12. 数据库行为：CHECK / 一致性触发器（断言 sqlstate）
# ===========================================================================


def _insert_delivery_sql(**over: Any) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {
        "id": uuid.uuid4(), "p": None, "koc": None, "e": None, "c": None,
        "v": None, "state": "PREPARED", "eh": None, "r": None,
        "rh": _hash("probe"), "by": None,
    }
    params.update(over)
    return (
        "INSERT INTO deliveries (id, project_id, koc_record_id, export_item_id,"
        " content_item_id, version_node_id, state, eligibility_hash,"
        " replaces_delivery_id, request_hash, created_by)"
        " VALUES (:id, :p, :koc, :e, :c, :v, :state, :eh, :r, :rh, :by)",
        params,
    )


def test_consistency_trigger_rejects_copied_fields_that_do_not_match_the_export(
    env: DeliveryEnv, client: TestClient
) -> None:
    """★ 三个冻结字段**必须逐列等于**被引的导出行 —— 否则「固定导出版本」落空。"""
    csrf = _login(client, env.settings, env.owner_id)
    content = _seed_content(env, title="冻结一致")
    export = _create_export(client, csrf, env, content)
    other = _create_export(client, csrf, env, _seed_content(env, title="另一条"))

    good = _insert_delivery_sql(
        p=env.project_id, e=export.export_item_id, c=export.content_item_id,
        v=export.version_node_id, eh=export.eligibility_hash, by=env.owner_id,
    )
    assert _sqlstate_of(env, [good]) is None

    for label, over in (
        ("version_node_id 换成别的版本", {"v": other.version_node_id}),
        ("eligibility_hash 换成别的指纹", {"eh": other.eligibility_hash}),
        ("content_item_id 换成别的内容", {"c": other.content_item_id}),
    ):
        base: dict[str, Any] = {
            "p": env.project_id, "e": export.export_item_id,
            "c": export.content_item_id, "v": export.version_node_id,
            "eh": export.eligibility_hash, "by": env.owner_id,
        }
        base.update(over)
        assert _sqlstate_of(env, [_insert_delivery_sql(**base)]) == CHECK_VIOLATION, label


def test_consistency_trigger_rejects_cross_project_rows(
    env: DeliveryEnv, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    export = _create_export(client, csrf, env, _seed_content(env))
    foreign_koc = _post_koc(client, csrf, env.other_project_id)
    koc_id = uuid.UUID(foreign_koc.json()["data"]["id"])

    # 跨项目 KOC
    state = _sqlstate_of(env, [_insert_delivery_sql(
        p=env.project_id, koc=koc_id, e=export.export_item_id,
        c=export.content_item_id, v=export.version_node_id,
        eh=export.eligibility_hash, by=env.owner_id,
    )])
    assert state == CHECK_VIOLATION

    # 跨项目内容（整行声称属于 other_project，而导出行属于 project）
    state = _sqlstate_of(env, [_insert_delivery_sql(
        p=env.other_project_id, e=export.export_item_id,
        c=export.content_item_id, v=export.version_node_id,
        eh=export.eligibility_hash, by=env.owner_id,
    )])
    assert state == CHECK_VIOLATION


def test_consistency_trigger_rejects_replace_chain_across_content(
    env: DeliveryEnv, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    a = _prepare(client, csrf, env, title="链A")
    b_export = _create_export(client, csrf, env, _seed_content(env, title="链B"))
    state = _sqlstate_of(env, [_insert_delivery_sql(
        p=env.project_id, e=b_export.export_item_id, c=b_export.content_item_id,
        v=b_export.version_node_id, eh=b_export.eligibility_hash,
        r=uuid.UUID(a.delivery_id), by=env.owner_id,
    )])
    assert state == CHECK_VIOLATION


def test_publication_consistency_trigger_rejects_cross_project_delivery(
    env: DeliveryEnv, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    prepared = _prepare(client, csrf, env, title="发布一致性")
    state = _sqlstate_of(env, [(
        "INSERT INTO publications (project_id, delivery_id, publication_key, state,"
        " content_state, request_hash, created_by)"
        " VALUES (:p, :d, 'XP-1', 'DRAFT', 'UNVERIFIED', :rh, :by)",
        {"p": env.other_project_id, "d": prepared.delivery_id,
         "rh": _hash("probe"), "by": env.owner_id},
    )])
    assert state == CHECK_VIOLATION


@pytest.mark.parametrize(
    ("over", "label"),
    [
        ({"state": "SENT"}, "ck_deliveries_state_valid"),
        ({"eh": "a" * 63}, "ck_deliveries_eligibility_hash_shape（长度）"),
        ({"eh": "G" * 64}, "ck_deliveries_eligibility_hash_shape（非十六进制）"),
        ({"rh": "Z" * 64}, "ck_deliveries_request_hash_shape"),
    ],
)
def test_delivery_check_constraints_reject_bad_rows(
    env: DeliveryEnv, client: TestClient, over: dict[str, Any], label: str
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    export = _create_export(client, csrf, env, _seed_content(env))
    base: dict[str, Any] = {
        "p": env.project_id, "e": export.export_item_id,
        "c": export.content_item_id, "v": export.version_node_id,
        "eh": export.eligibility_hash, "by": env.owner_id,
    }
    base.update(over)
    state = _sqlstate_of(env, [_insert_delivery_sql(**base)])
    assert state == CHECK_VIOLATION, f"{label} 应被 CHECK 拒绝（sqlstate={state}）"


def test_self_replace_is_rejected(env: DeliveryEnv, client: TestClient) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    export = _create_export(client, csrf, env, _seed_content(env))
    row_id = uuid.uuid4()
    state = _sqlstate_of(env, [_insert_delivery_sql(
        id=row_id, p=env.project_id, e=export.export_item_id,
        c=export.content_item_id, v=export.version_node_id,
        eh=export.eligibility_hash, r=row_id, by=env.owner_id,
    )])
    assert state == CHECK_VIOLATION


def test_blank_display_name_is_rejected_by_db_too(env: DeliveryEnv) -> None:
    """应用层 strip 之外，库层也有 CHECK（裸 SQL 绕不过去）。"""
    state = _sqlstate_of(env, [(
        "INSERT INTO koc_records (project_id, display_name) VALUES (:p, '   ')",
        {"p": env.project_id},
    )])
    assert state == CHECK_VIOLATION


@pytest.mark.parametrize(
    ("columns", "values", "params", "label"),
    [
        (
            "publication_key, state, content_state",
            "'有,逗号', 'DRAFT', 'UNVERIFIED'",
            {},
            "ck_publications_publication_key_format",
        ),
        (
            "publication_key, state, content_state",
            "'OK-1', 'SENT', 'UNVERIFIED'",
            {},
            "ck_publications_state_valid",
        ),
        (
            "publication_key, state, content_state",
            "'OK-2', 'DRAFT', 'DELETED'",
            {},
            "ck_publications_content_state_valid（取值域里没有『已删除』）",
        ),
        (
            "publication_key, state, content_state, published_at",
            "'OK-3', 'DRAFT', 'UNVERIFIED', now()",
            {},
            "ck_publications_published_shape",
        ),
    ],
)
def test_publication_check_constraints_reject_bad_rows(
    env: DeliveryEnv, columns: str, values: str, params: dict[str, Any], label: str
) -> None:
    state = _sqlstate_of(env, [(
        f"INSERT INTO publications (project_id, request_hash, created_by, {columns})"
        f" VALUES (:p, :rh, :by, {values})",
        {"p": env.project_id, "rh": _hash("probe"), "by": env.owner_id, **params},
    )])
    assert state == CHECK_VIOLATION, f"{label} 应被 CHECK 拒绝（sqlstate={state}）"


def test_publication_links_table_exists_with_its_check_but_no_global_unique(
    env: DeliveryEnv, client: TestClient
) -> None:
    """`publication_links`：`link_kind` 的 CHECK 生效，

    且 **`canonical_url` 没有全局唯一** —— 否则第二个项目物理上无法登记同一条
    笔记，WP37 的「跨项目同笔记需管理者确认」分支永不可达。

    WP37 起唯一性是 **`(project_id, canonical_url)`**（拍板项 1）：同项目内同一个
    `canonical_url` 两行被挡，跨项目则不挡。本用例原先断言「除主键外无任何唯一索引」，
    那是 WP36 的取舍，已随 WP37 的迁移改成下面的形态；「无**全局**唯一」的意图不变。
    """
    csrf = _login(client, env.settings, env.owner_id)
    pub = _post_publication(client, csrf, env.project_id, key_value="LINK-1")
    pub_id = pub.json()["data"]["id"]

    insert = (
        "INSERT INTO publication_links (publication_id, project_id, raw_url,"
        " canonical_url, link_kind) VALUES (:p, :proj, :raw, :canon, :kind)"
    )

    bad = _sqlstate_of(env, [(
        insert, {"p": pub_id, "proj": str(env.project_id), "raw": "https://x",
                 "canon": "https://x", "kind": "TINY"},
    )])
    assert bad == CHECK_VIOLATION

    # 同项目内同一个 canonical_url 两行 ⇒ 被 (project_id, canonical_url) 唯一索引挡下
    twice = _sqlstate_of(env, [
        (insert, {"p": pub_id, "proj": str(env.project_id), "raw": "https://a",
                  "canon": "https://canon", "kind": "LONG"}),
        (insert, {"p": pub_id, "proj": str(env.project_id), "raw": "https://b",
                  "canon": "https://canon", "kind": "LONG"}),
    ])
    assert twice == UNIQUE_VIOLATION

    with session_scope(env.settings) as session:
        uniques = session.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes"
                " WHERE tablename = 'publication_links' AND indexdef LIKE '%UNIQUE%'"
                " ORDER BY indexname"
            )
        ).all()
    assert [u[0] for u in uniques] == [
        "pk_publication_links",
        "uq_publication_links_project_canonical",
    ], uniques
    # 核心意图：唯一索引必须带 project_id，不能退化成仅按 canonical_url 的全局唯一
    assert "(project_id, canonical_url)" in dict(uniques)["uq_publication_links_project_canonical"]


def test_structure_matches_the_models_on_disk(env: DeliveryEnv) -> None:
    """表 / CHECK / FK / 触发器的名集合必须与模型一致（结构漂移的机械判据）。"""
    with session_scope(env.settings) as session:
        tables = set(
            session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'public' AND table_name = ANY(:names)"
                ),
                {"names": list(OWN_TABLES)},
            ).scalars().all()
        )
        assert tables == set(OWN_TABLES)

        for table in OWN_TABLES:
            meta = Delivery.metadata.tables[table]
            for contype, kind in (("c", "CheckConstraint"), ("f", "ForeignKeyConstraint")):
                expected = {
                    c.name for c in meta.constraints if c.__class__.__name__ == kind
                }
                actual = set(
                    session.execute(
                        text(
                            "SELECT conname FROM pg_constraint"
                            " WHERE contype = :t AND conrelid = CAST(:r AS regclass)"
                        ),
                        {"t": contype, "r": table},
                    ).scalars().all()
                )
                assert expected == actual, (table, kind, expected ^ actual)

        triggers = set(
            session.execute(
                text(
                    "SELECT tgname FROM pg_trigger WHERE tgrelid = ANY("
                    "SELECT oid FROM pg_class WHERE relname = ANY(:names)"
                    ") AND NOT tgisinternal"
                ),
                {"names": list(OWN_TABLES)},
            ).scalars().all()
        )
        assert triggers == set(d_ddl.trigger_names())

        # 本包**不**依赖跨包共用的不可变函数，但也绝不能把它删掉（ISS-79）
        assert "rednote_reject_mutation" not in d_ddl.consistency_function_names()
        assert session.execute(
            text("SELECT count(*) FROM pg_proc WHERE proname = 'rednote_reject_mutation'")
        ).scalar_one() == 1


def test_lock_version_is_bigint_default_one(env: DeliveryEnv) -> None:
    """平台助手是 **BIGINT / DEFAULT 1**（DDL 草案 §0.1 的 `INTEGER DEFAULT 0` 已过期）。"""
    with session_scope(env.settings) as session:
        rows = session.execute(
            text(
                "SELECT table_name, data_type, column_default"
                " FROM information_schema.columns"
                " WHERE table_schema = 'public' AND column_name = 'lock_version'"
                "   AND table_name = ANY(:names) ORDER BY table_name"
            ),
            {"names": list(OWN_TABLES)},
        ).all()
    assert {r[0] for r in rows} == {"koc_records", "deliveries", "publications"}
    for name, data_type, default in rows:
        assert data_type == "bigint", (name, data_type)
        assert default == "1", (name, default)


def test_all_foreign_keys_are_restrict(env: DeliveryEnv) -> None:
    """每个外键 `ON DELETE RESTRICT`，零 `CASCADE`（删除须经状态机）。"""
    with session_scope(env.settings) as session:
        rows = session.execute(
            text(
                "SELECT conname, confdeltype FROM pg_constraint"
                " WHERE contype = 'f' AND conrelid = ANY("
                "SELECT oid FROM pg_class WHERE relname = ANY(:names))"
            ),
            {"names": list(OWN_TABLES)},
        ).all()
    assert rows, "一条外键都没扫到 —— 判据失效"
    for name, deltype in rows:
        assert deltype == "r", f"{name} 的 ON DELETE 不是 RESTRICT（{deltype}）"


# ===========================================================================
# 13. 纯函数 / 取值域
# ===========================================================================


def test_request_hash_scopes_are_disjoint() -> None:
    """创建与替换的摘要**必须不同** —— 否则同一个键下两者会被判成同体。"""
    item = uuid.uuid4()
    old = uuid.uuid4()
    create = d_types.delivery_create_request_hash(
        export_item_id=item, koc_record_id=None, note=None
    )
    replace = d_types.delivery_replace_request_hash(
        replaces_delivery_id=old, export_item_id=item, koc_record_id=None, note=None
    )
    assert create != replace

    # 替换的摘要吃进了"替换谁"
    other = d_types.delivery_replace_request_hash(
        replaces_delivery_id=uuid.uuid4(), export_item_id=item,
        koc_record_id=None, note=None,
    )
    assert replace != other

    # 同输入 ⇒ 同摘要（幂等回放的前提）
    assert create == d_types.delivery_create_request_hash(
        export_item_id=item, koc_record_id=None, note=None
    )
    # 每个参与字段都真的进了摘要
    assert create != d_types.delivery_create_request_hash(
        export_item_id=item, koc_record_id=uuid.uuid4(), note=None
    )
    assert create != d_types.delivery_create_request_hash(
        export_item_id=item, koc_record_id=None, note="x"
    )
    assert create != d_types.delivery_create_request_hash(
        export_item_id=uuid.uuid4(), koc_record_id=None, note=None
    )


def test_publication_request_hash_covers_every_field() -> None:
    base: dict[str, Any] = {
        "publication_key": "K", "delivery_id": None, "note_id": None,
        "note_url": None, "state": "DRAFT", "published_at": None,
    }
    baseline = d_types.publication_create_request_hash(**base)
    assert baseline == d_types.publication_create_request_hash(**base)
    for field_name, value in (
        ("publication_key", "K2"),
        ("delivery_id", uuid.uuid4()),
        ("note_id", "n"),
        ("note_url", "https://x"),
    ):
        changed = dict(base)
        changed[field_name] = value
        assert d_types.publication_create_request_hash(**changed) != baseline, field_name
    published = dict(base)
    published["state"] = "PUBLISHED"
    published["published_at"] = datetime(2026, 10, 19, tzinfo=timezone.utc)
    assert d_types.publication_create_request_hash(**published) != baseline


def test_hashes_are_64_hex_matching_the_column_shape() -> None:
    """摘要形状必须与 `String(64)` + 十六进制 CHECK 同形。"""
    value = d_types.delivery_create_request_hash(
        export_item_id=uuid.uuid4(), koc_record_id=None, note=None
    )
    assert len(value) == d_types.SHA256_HEX_CHARS
    assert all(c in "0123456789abcdef" for c in value)


def test_publication_key_validation_matches_the_db_regex() -> None:
    for good in ("A", "a-b_c.1", "N" * 64, "2026.10.19-001"):
        assert d_types.is_valid_publication_key(good), good
    for bad in ("", "N" * 65, "a,b", "a b", "a/b", "中文", "a\nb"):
        assert not d_types.is_valid_publication_key(bad), bad


def test_delivery_state_machine_has_no_back_edges() -> None:
    """终态没有出边；`PREPARED` 恰好两条边。"""
    assert d_schemas.DELIVERY_TRANSITIONS["PREPARED"] == frozenset(
        {"DELIVERED", "CANCELLED"}
    )
    assert d_schemas.DELIVERY_TRANSITIONS["DELIVERED"] == frozenset()
    assert d_schemas.DELIVERY_TRANSITIONS["CANCELLED"] == frozenset()
    assert set(d_schemas.DELIVERY_TRANSITIONS) == set(d_types.DELIVERY_STATES)
    for current, targets in d_schemas.DELIVERY_TRANSITIONS.items():
        assert current not in targets, f"{current} 有自环"
        assert targets <= set(d_types.DELIVERY_STATES)


def test_content_state_domain_cannot_express_deleted() -> None:
    """★ 取值域只有两个值 ⇒ **结构上**推不出「已删除」（草案点名的反例）。"""
    assert d_types.CONTENT_STATES == ("VERIFIED", "UNVERIFIED")
    assert d_types.CONTENT_STATE_INITIAL == "UNVERIFIED"
    assert not any("DELET" in s.upper() for s in d_types.CONTENT_STATES)


def test_value_domain_sql_literals_match_the_tuples() -> None:
    """`*_LIST_SQL` 与元组同源（迁移里写字面量时按它逐条对账，ISS-191）。"""
    for values, sql in (
        (d_types.DELIVERY_STATES, d_types.DELIVERY_STATE_LIST_SQL),
        (d_types.PUBLICATION_STATES, d_types.PUBLICATION_STATE_LIST_SQL),
        (d_types.CONTENT_STATES, d_types.CONTENT_STATE_LIST_SQL),
        (d_types.LINK_KINDS, d_types.LINK_KIND_LIST_SQL),
    ):
        assert sql == ", ".join(f"'{v}'" for v in values)


def test_idempotency_key_limit_matches_wp35() -> None:
    """与 WP35 同值（契约草案写 128，以磁盘为准；差异已进契约修订清单）。"""
    from rednote.modules.exports import types as x_types

    assert d_types.MAX_IDEMPOTENCY_KEY_CHARS == x_types.MAX_IDEMPOTENCY_KEY_CHARS == 160
    assert d_types.SHA256_HEX_CHARS == x_types.SHA256_HEX_CHARS == 64


def test_frozen_trigger_ddl_uses_is_distinct_from_for_every_frozen_column() -> None:
    """★ 触发器 DDL 必须对**每一个**冻结列用 `IS DISTINCT FROM`，且零 `<>`。"""
    function_ddl, trigger_ddl, drop_trigger, _ = d_ddl.frozen_columns_ddl()
    columns = frozen_delivery_columns()
    for name in columns:
        assert f"NEW.{name} IS DISTINCT FROM OLD.{name}" in function_ddl, name
    assert function_ddl.count("IS DISTINCT FROM") == len(columns)
    assert "<>" not in function_ddl, "触发器里出现了 <> —— 对可空列会静默失效"
    # BEFORE 触发器必须 RETURN NEW：返回 NULL 会**取消**这次 UPDATE（静默无操作）
    assert "RETURN NEW;" in function_ddl and "RETURN NULL" not in function_ddl
    assert "BEFORE UPDATE ON deliveries" in trigger_ddl
    assert "DROP TRIGGER IF EXISTS" in drop_trigger  # 夹具的幂等前提
    for name in MUTABLE_DELIVERY_COLUMNS:
        assert f"NEW.{name} IS DISTINCT FROM" not in function_ddl, name


def test_ddl_never_drops_the_shared_immutability_function() -> None:
    """`downgrade` 只删本包三个函数；`rednote_reject_mutation` 跨包共用（ISS-79）。"""
    names = d_ddl.consistency_function_names()
    assert len(names) == 3
    assert "rednote_reject_mutation" not in names
    for _t, _f, _tr, _dt, drop_function in d_ddl.all_trigger_ddl():
        assert "rednote_reject_mutation" not in drop_function


def test_ddl_tuple_shape_matches_the_repo_convention() -> None:
    """五元组 `(表名, 函数DDL, 触发器DDL, 触发器DROP, 函数DROP)`（夹具与迁移共用形状）。"""
    items = d_ddl.all_trigger_ddl()
    assert len(items) == 3
    for item in items:
        assert len(item) == 5
        table, function_ddl, trigger_ddl, drop_trigger, drop_function = item
        assert table in OWN_TABLES
        assert function_ddl.startswith("CREATE OR REPLACE FUNCTION")
        assert trigger_ddl.startswith("CREATE ")
        assert drop_trigger.startswith("DROP TRIGGER IF EXISTS")
        assert drop_function.startswith("DROP FUNCTION IF EXISTS")


def test_this_package_declares_no_immutable_tables() -> None:
    """本包没有整行不可变表 —— 误用 `guard_immutable` 会让状态机当场死。"""
    from rednote.modules.delivery import models as d_models

    assert d_models.IMMUTABLE_TABLES == ()
    assert not any(t.startswith("trg_") and t.endswith("_immutable")
                   for t in d_ddl.trigger_names())


def test_module_docstring_distinguishes_the_three_publication_tables() -> None:
    """三个同名概念的对比表必须在模块文档里（R11 的落点，不是口头约定）。"""
    import rednote.modules.delivery as package
    from rednote.modules.delivery import models as d_models

    for doc in (package.__doc__ or "", d_models.__doc__ or ""):
        assert "knowledge_publications" in doc
        assert "prompt_publications" in doc


def test_json_payload_of_request_hash_is_stable_across_key_order() -> None:
    """摘要用 `sort_keys=True` ⇒ 与字典构造顺序无关（纯函数的稳定性）。"""
    payload_a = json.dumps({"a": 1, "b": 2}, sort_keys=True, separators=(",", ":"))
    payload_b = json.dumps({"b": 2, "a": 1}, sort_keys=True, separators=(",", ":"))
    assert payload_a == payload_b


def test_koc_record_model_has_no_credential_columns() -> None:
    """**禁止**任何存放密码/令牌的列（草案 `:55` 明文）。"""
    forbidden = ("password", "passwd", "secret", "token", "credential", "cookie")
    for name in KocRecord.__table__.columns.keys():
        assert not any(bad in name.lower() for bad in forbidden), name
