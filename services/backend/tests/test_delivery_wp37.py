"""WP37 链接规范化 / 笔记去重 / 发布内容状态的判据落点。

分三层：纯函数（`urls.normalize_url`）→ 仓储（distinct 计数）→ HTTP 端到端。

## 夹具纪律（照 `test_delivery_wp36.py`，但本文件不依赖迁移）

· 一律跑**私有库**（`REDNOTE_TEST_DATABASE_URL`），不碰共享 `rednote_test`；
· 表由夹具 `create_all` 建出（含本包新增的 `project_id` 与唯一索引），
  `publication_links / publications / deliveries / koc_records` 先 DROP 再建，保证结构是当前模型的；
· 每个用例新造 user / project，按项目隔离，不数全库行。

## 阴性 / 反向用例的写法

每条"被拒"断言都同时断言**库里没有多出行**，每条"放行"断言都有对照的"被拒"：
证明判据不是装饰（例如"管理者放行"配"运营被拒"，"项目内重复"配"换个路径就放行"）。
"""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import drop_own_tables, truncate_targets

import rednote.modules as _modules_pkg  # noqa: E402

from rednote.modules.delivery import service as d_service
from rednote.modules.delivery import types as d_types
from rednote.modules.delivery import urls as d_urls
from rednote.modules.delivery.models import OWN_TABLES, PublicationLink
from rednote.modules.delivery.repository import DeliveryRepository
from rednote.modules.delivery.urls import UrlInvalid, normalize_url
from rednote.modules.identity.models import ROLE_ADMIN, ROLE_OPERATOR
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.projects.models import PHASE_CLOSED, Brand, Product, Project
from rednote.modules.projects.permissions import (
    OrgAction,
    allows_org_action,
)
from rednote.modules.projects.repository import ProjectRepository
from rednote.platform.base import Base
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

def _import_all_model_modules() -> None:
    """FK 目标表必须在 `create_all` 之前全部注册（否则 NoReferencedTableError）。"""
    for info in pkgutil.iter_modules(_modules_pkg.__path__):
        try:
            importlib.import_module(f"rednote.modules.{info.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name != f"rednote.modules.{info.name}.models":
                raise


_import_all_model_modules()

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
TEST_DATABASE_URL_ENV = "REDNOTE_TEST_DATABASE_URL"
TEST_ORIGIN = "http://testserver"
API = "/api/v1"

DROP_ORDER = ("publication_links", "publications", "deliveries", "koc_records")

NOTE_URL = "https://www.example.com/explore/abc123"


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
# 纯函数层：规范化
# ===========================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # scheme / host 小写
        ("HTTPS://WWW.Example.COM/Path", "https://www.example.com/Path"),
        # 去默认端口（http:80 / https:443）
        ("http://a.com:80/x", "http://a.com/x"),
        ("https://a.com:443/x", "https://a.com/x"),
        # 非默认端口保留；跨 scheme 的"默认"不通用（https:80 要保留）
        ("https://a.com:8443/x", "https://a.com:8443/x"),
        ("https://a.com:80/x", "https://a.com:80/x"),
        # 去 fragment
        ("https://a.com/x#section", "https://a.com/x"),
        # 去尾部多余 /，根路径也去
        ("https://a.com/x//", "https://a.com/x"),
        ("https://a.com/", "https://a.com"),
        ("https://a.com", "https://a.com"),
        # 查询参数排序
        ("https://a.com/x?b=2&a=1", "https://a.com/x?a=1&b=2"),
        # 通用追踪参数被去掉，其余保留
        ("https://a.com/x?utm_source=s&id=7&fbclid=Z&gclid=Y", "https://a.com/x?id=7"),
        ("https://a.com/x?UTM_Medium=m&id=7", "https://a.com/x?id=7"),
        # 两端空白被去掉
        ("  https://a.com/x  ", "https://a.com/x"),
        # 空值参数保留（语义不明不乱删）
        ("https://a.com/x?flag", "https://a.com/x?flag="),
    ],
)
def test_normalize_rules(raw: str, expected: str) -> None:
    assert normalize_url(raw).canonical == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://a.com/x?utm_source=s&b=2&a=1#frag",
        "HTTP://A.com:80//x//?z=%E4%B8%AD&a=1+2",
        "https://a.com/?q=%zz",
        "https://a.com/x?a=1&a=0&&b",
        "https://[::1]:8080/x?b=1&a=2",
        "https://a.com",
        "https://a.com//x",
    ],
)
def test_normalize_is_idempotent(raw: str) -> None:
    once = normalize_url(raw)
    twice = normalize_url(once.canonical)
    assert twice == once


def test_tracking_params_are_generic_and_not_overbroad() -> None:
    # 反向：名字像追踪但不在通用名单里的参数必须保留（不凭记忆写平台规则）
    kept = normalize_url("https://a.com/x?utm=1&xsec_token=t&source=app").canonical
    assert kept == "https://a.com/x?source=app&utm=1&xsec_token=t"
    assert d_urls.TRACKING_PARAM_PREFIXES == ("utm_",)
    assert "xsec_token" not in d_urls.TRACKING_PARAM_EXACT


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not a url",
        "ftp://a.com/x",
        "javascript:alert(1)",
        "//a.com/x",
        "https://",
        "https://user:pw@a.com/x",
        "https://a.com:99999/x",
        "https://a.com:abc/x",
        "https://a.com/x y",
        "https://a.com/x\ny",
        "https://a.com/\x00x",
        "https://a.com/" + "x" * 3000,
    ],
    ids=lambda raw: raw if len(raw) < 60 else "too-long",
)
def test_normalize_rejects_invalid(raw: str) -> None:
    with pytest.raises(UrlInvalid):
        normalize_url(raw)


def test_link_kind_never_short() -> None:
    samples = [
        "https://a.com",
        "https://a.com/x",
        "https://xhslink.com/a/AbCdEf",  # 长得像短链，但本包不识别
        "http://t.cn/abc",
        "https://a.com/x?id=1",
        "https://a.com/?utm_source=s",
    ]
    kinds = {normalize_url(s).link_kind for s in samples}
    assert kinds == {d_types.LINK_KIND_LONG, d_types.LINK_KIND_OTHER}
    assert d_types.LINK_KIND_SHORT not in kinds
    # 取值域保留 SHORT（DDL 已定）
    assert d_types.LINK_KIND_SHORT in d_types.LINK_KINDS
    # 判法：只有主机 ⇒ OTHER；有路径 / 查询 ⇒ LONG
    assert normalize_url("https://a.com/?utm_source=s").link_kind == "OTHER"
    assert normalize_url("https://a.com/x").link_kind == "LONG"


def test_short_and_long_are_not_unified_known_gap() -> None:
    """已知缺口：本包不做短链 ⇄ 长链等价。这条断言把缺口钉住，将来补了要有意识地改它。"""
    short = normalize_url("https://xhslink.com/a/AbCdEf").canonical
    long = normalize_url("https://www.xiaohongshu.com/explore/abc123").canonical
    assert short != long


def test_normalizer_has_no_io_and_no_note_id() -> None:
    source = inspect.getsource(d_urls)
    for forbidden in ("urlopen", "import socket", "requests", "httpx", "http.client"):
        assert forbidden not in source
    assert set(normalize_url(NOTE_URL).__dataclass_fields__) == {"canonical", "link_kind"}


def test_confirm_permission_is_admin_only() -> None:
    assert allows_org_action(
        is_admin=True, action=OrgAction.CONFIRM_CROSS_PROJECT_NOTE
    )
    assert not allows_org_action(
        is_admin=False, action=OrgAction.CONFIRM_CROSS_PROJECT_NOTE
    )


# ===========================================================================
# 建表夹具（私有库，create_all）
# ===========================================================================


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables() -> Iterator[None]:
    reset_engines()
    from rednote.main import create_app  # noqa: F401  # 导入全部模型模块，FK 目标才齐

    engine = get_engine(_make_settings())
    drop_own_tables(engine, DROP_ORDER)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def _clean_own_rows() -> Iterator[None]:
    reset_engines()
    engine = get_engine(_make_settings())
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(truncate_targets(engine, DROP_ORDER))))
    yield


@dataclass(frozen=True)
class Env:
    settings: Settings
    project_id: uuid.UUID  # owner + collab
    other_project_id: uuid.UUID  # 仅 owner
    closed_project_id: uuid.UUID  # 仅 owner，已结项
    owner_id: uuid.UUID
    collab_id: uuid.UUID
    outsider_id: uuid.UUID
    admin_id: uuid.UUID


def _create_user(settings: Settings, *, role: str = ROLE_OPERATOR) -> uuid.UUID:
    with session_scope(settings) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name=f"WP37-{role}",
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
            project_code=f"WP37-{uuid.uuid4().hex[:8].upper()}",
            name="WP37 测试项目",
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
def env() -> Env:
    settings = _make_settings()
    owner = _create_user(settings)
    collab = _create_user(settings)
    return Env(
        settings=settings,
        project_id=_seed_project(settings, owner_id=owner, collaborators=(collab,)),
        other_project_id=_seed_project(settings, owner_id=owner),
        closed_project_id=_seed_project(settings, owner_id=owner, closed=True),
        owner_id=owner,
        collab_id=collab,
        outsider_id=_create_user(settings),
        admin_id=_create_user(settings, role=ROLE_ADMIN),
    )


@pytest.fixture
def client(env: Env) -> Iterator[TestClient]:
    """真 app；`main.py` 尚未挂本包 router 时补挂（幂等，按路径前缀探测）。"""
    from rednote.main import create_app
    from rednote.modules.delivery.api import router as delivery_router

    app: FastAPI = create_app(env.settings)
    prefix = env.settings.api_prefix
    existing = {getattr(route, "path", None) for route in app.routes}
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


# ---------------------------------------------------------------------------
# 快捷调用
# ---------------------------------------------------------------------------


def _mk_pub(client: TestClient, csrf: str, project_id: uuid.UUID) -> str:
    resp = client.post(
        f"{API}/projects/{project_id}/publications",
        json={"publicationKey": f"k-{uuid.uuid4().hex[:10]}"},
        headers=_wh(csrf),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


def _add_link(client: TestClient, csrf: str, pub_id: str, raw: str, **headers: str):
    return client.post(
        f"{API}/publications/{pub_id}/links",
        json={"rawUrl": raw},
        headers=_wh(csrf, **headers),
    )


def _verify(client: TestClient, csrf: str, pub_id: str, result: Any):
    return client.post(
        f"{API}/publications/{pub_id}/verifications",
        json={"result": result},
        headers=_wh(csrf),
    )


def _db_scalar(env: Env, sql: str, **params: Any) -> Any:
    with session_scope(env.settings) as session:
        return session.execute(text(sql), params).scalar_one()


def _link_count(env: Env, project_id: uuid.UUID) -> int:
    return _db_scalar(
        env,
        "SELECT count(*) FROM publication_links WHERE project_id = :p",
        p=project_id,
    )


# ===========================================================================
# 模型 / 索引形状
# ===========================================================================


def test_model_has_project_id_and_project_canonical_unique_index() -> None:
    table = PublicationLink.__table__
    assert not table.c.project_id.nullable
    fks = {fk.target_fullname: fk.ondelete for fk in table.c.project_id.foreign_keys}
    assert fks == {"projects.id": "RESTRICT"}
    idx = {i.name: i for i in table.indexes}
    uq = idx["uq_publication_links_project_canonical"]
    assert uq.unique
    assert [c.name for c in uq.columns] == ["project_id", "canonical_url"]
    # 原普通索引保留
    assert not idx["ix_publication_links_canonical"].unique
    assert PublicationLink.__tablename__ in OWN_TABLES


def test_db_enforces_project_canonical_uniqueness_but_allows_cross_project(
    env: Env, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    pub_a = _mk_pub(client, csrf, env.project_id)
    pub_b = _mk_pub(client, csrf, env.other_project_id)
    ins = (
        "INSERT INTO publication_links (publication_id, project_id, raw_url,"
        " canonical_url, link_kind) VALUES (:pub, :pid, 'r', 'https://x.com/u', 'LONG')"
    )
    with session_scope(env.settings) as session:
        session.execute(text(ins), {"pub": pub_a, "pid": env.project_id})
        # 跨项目同 canonical：库层允许
        session.execute(text(ins), {"pub": pub_b, "pid": env.other_project_id})
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError) as info:
        with session_scope(env.settings) as session:
            session.execute(text(ins), {"pub": pub_a, "pid": env.project_id})
    assert getattr(info.value.orig, "sqlstate", None) == "23505"


# ===========================================================================
# 登记链接
# ===========================================================================


def test_add_link_ok_and_backfills_url_canonical(env: Env, client: TestClient) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    pub = _mk_pub(client, csrf, env.project_id)
    assert _db_scalar(
        env, "SELECT url_canonical FROM publications WHERE id = :i", i=pub
    ) is None

    resp = _add_link(client, csrf, pub, "HTTPS://WWW.Example.com/explore/abc123/?utm_source=x#f")
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["canonicalUrl"] == "https://www.example.com/explore/abc123"
    assert data["rawUrl"] == "HTTPS://WWW.Example.com/explore/abc123/?utm_source=x#f"
    assert data["linkKind"] == "LONG"
    assert data["publicationId"] == pub
    assert data["crossProjectConfirmed"] is False
    assert set(data) == {
        "id", "publicationId", "rawUrl", "canonicalUrl", "linkKind",
        "crossProjectConfirmed",
    }
    assert (
        _db_scalar(env, "SELECT url_canonical FROM publications WHERE id = :i", i=pub)
        == "https://www.example.com/explore/abc123"
    )
    # project_id 冗余列 = 发布所属项目
    assert _db_scalar(
        env, "SELECT project_id FROM publication_links WHERE id = :i", i=data["id"]
    ) == env.project_id


def test_second_link_does_not_overwrite_first_url_canonical(
    env: Env, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    pub = _mk_pub(client, csrf, env.project_id)
    assert _add_link(client, csrf, pub, "https://a.com/one").status_code == 201
    assert _add_link(client, csrf, pub, "https://a.com/two").status_code == 201
    assert (
        _db_scalar(env, "SELECT url_canonical FROM publications WHERE id = :i", i=pub)
        == "https://a.com/one"
    )


def test_link_kind_other_for_bare_host_and_never_short_in_db(
    env: Env, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    pub = _mk_pub(client, csrf, env.project_id)
    for raw in ("https://a.com", "https://xhslink.com/a/Zz", "https://a.com/p?id=1"):
        assert _add_link(client, csrf, pub, raw).status_code == 201
    kinds = _db_scalar(
        env,
        "SELECT array_agg(DISTINCT link_kind ORDER BY link_kind) FROM publication_links"
        " WHERE project_id = :p",
        p=env.project_id,
    )
    assert kinds == ["LONG", "OTHER"]


def test_duplicate_in_project_is_409_even_via_other_publication(
    env: Env, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    pub1 = _mk_pub(client, csrf, env.project_id)
    pub2 = _mk_pub(client, csrf, env.project_id)
    assert _add_link(client, csrf, pub1, "https://a.com/n/1").status_code == 201

    # 写法不同、规范化后相同
    dup = _add_link(client, csrf, pub2, "HTTPS://A.com:443/n/1/?utm_campaign=z&fbclid=q#x")
    assert dup.status_code == 409, dup.text
    body = dup.json()
    assert body["error"]["code"] == "DUPLICATE_PUBLICATION"
    assert body["error"]["details"]["reason"] == "LINK_CANONICAL_TAKEN"
    assert body["error"]["details"].get("crossProject") is None
    assert _link_count(env, env.project_id) == 1

    # 同一发布重复登记同一链接也是 409
    assert _add_link(client, csrf, pub1, "https://a.com/n/1").status_code == 409

    # 反向：换个路径就放行 ⇒ 判据确实在比较 canonical
    assert _add_link(client, csrf, pub2, "https://a.com/n/2").status_code == 201
    assert _link_count(env, env.project_id) == 2


def test_cross_project_hit_blocks_non_admin_without_leaking(
    env: Env, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    pub_a = _mk_pub(client, csrf, env.project_id)
    pub_b = _mk_pub(client, csrf, env.other_project_id)
    assert _add_link(client, csrf, pub_a, "https://a.com/n/9").status_code == 201

    resp = _add_link(client, csrf, pub_b, "https://a.com/n/9?utm_source=q")
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "DUPLICATE_PUBLICATION"
    assert err["details"]["crossProject"] is True
    assert err["details"]["reason"] == "CROSS_PROJECT_NOTE"
    # 不泄露对方项目 / 发布 / 链接
    leak = resp.text
    assert str(env.project_id) not in leak
    assert pub_a not in leak
    assert "WP37 测试项目" not in leak
    # 没有落行
    assert _link_count(env, env.other_project_id) == 0
    assert _db_scalar(
        env, "SELECT url_canonical FROM publications WHERE id = :i", i=pub_b
    ) is None


def test_admin_may_register_cross_project_and_is_flagged(
    env: Env, client: TestClient
) -> None:
    owner_csrf = _login(client, env.settings, env.owner_id)
    pub_a = _mk_pub(client, owner_csrf, env.project_id)
    pub_b = _mk_pub(client, owner_csrf, env.other_project_id)
    assert _add_link(client, owner_csrf, pub_a, "https://a.com/n/7").status_code == 201

    admin_csrf = _login(client, env.settings, env.admin_id)
    ok = _add_link(client, admin_csrf, pub_b, "https://a.com/n/7")
    assert ok.status_code == 201, ok.text
    assert ok.json()["data"]["crossProjectConfirmed"] is True
    assert _link_count(env, env.other_project_id) == 1

    # 管理者不能绕过项目内重复
    pub_b2 = _mk_pub(client, admin_csrf, env.other_project_id)
    again = _add_link(client, admin_csrf, pub_b2, "https://a.com/n/7")
    assert again.status_code == 409
    assert again.json()["error"]["details"]["reason"] == "LINK_CANONICAL_TAKEN"


@pytest.mark.parametrize("raw", ["", "ftp://a.com/x", "not a url", "https://u:p@a.com/x"])
def test_invalid_raw_url_is_422_and_writes_nothing(
    env: Env, client: TestClient, raw: str
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    pub = _mk_pub(client, csrf, env.project_id)
    resp = _add_link(client, csrf, pub, raw)
    assert resp.status_code == 422, resp.text
    assert _link_count(env, env.project_id) == 0
    assert _db_scalar(
        env, "SELECT url_canonical FROM publications WHERE id = :i", i=pub
    ) is None


def test_raw_url_too_long_is_422_and_extra_fields_rejected(
    env: Env, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    pub = _mk_pub(client, csrf, env.project_id)
    assert _add_link(client, csrf, pub, "https://a.com/" + "x" * 2100).status_code == 422
    extra = client.post(
        f"{API}/publications/{pub}/links",
        json={"rawUrl": "https://a.com/x", "canonicalUrl": "https://evil/"},
        headers=_wh(csrf),
    )
    assert extra.status_code == 422
    assert _link_count(env, env.project_id) == 0


def test_idempotency_key_length_is_validated_on_writes(
    env: Env, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    pub = _mk_pub(client, csrf, env.project_id)
    bad = _add_link(client, csrf, pub, "https://a.com/x", **{"Idempotency-Key": "k" * 200})
    assert bad.status_code == 422
    good = _add_link(client, csrf, pub, "https://a.com/x", **{"Idempotency-Key": "k" * 20})
    assert good.status_code == 201
    vbad = client.post(
        f"{API}/publications/{pub}/verifications",
        json={"result": "VERIFIED"},
        headers=_wh(csrf, **{"Idempotency-Key": "k" * 200}),
    )
    assert vbad.status_code == 422


# ===========================================================================
# 内容状态
# ===========================================================================


def test_verify_writes_only_the_two_values(env: Env, client: TestClient) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    pub = _mk_pub(client, csrf, env.project_id)
    sql = "SELECT content_state FROM publications WHERE id = :i"
    assert _db_scalar(env, sql, i=pub) == "UNVERIFIED"

    ok = _verify(client, csrf, pub, "VERIFIED")
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["contentState"] == "VERIFIED"
    assert _db_scalar(env, sql, i=pub) == "VERIFIED"

    # 重复写同值：幂等放行
    assert _verify(client, csrf, pub, "VERIFIED").status_code == 200

    back = _verify(client, csrf, pub, "UNVERIFIED")
    assert back.status_code == 200
    assert _db_scalar(env, sql, i=pub) == "UNVERIFIED"


@pytest.mark.parametrize(
    "value", ["DELETED", "REMOVED", "FETCH_FAILED", "verified", "", None, 1]
)
def test_verify_rejects_anything_else_and_state_unchanged(
    env: Env, client: TestClient, value: Any
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    pub = _mk_pub(client, csrf, env.project_id)
    assert _verify(client, csrf, pub, "VERIFIED").status_code == 200
    resp = _verify(client, csrf, pub, value)
    assert resp.status_code == 422, resp.text
    assert (
        _db_scalar(env, "SELECT content_state FROM publications WHERE id = :i", i=pub)
        == "VERIFIED"
    )


def test_content_state_domain_has_no_deletion_value() -> None:
    assert d_types.CONTENT_STATES == ("VERIFIED", "UNVERIFIED")
    assert not any("DELET" in s.upper() for s in d_types.CONTENT_STATES)


def test_verify_note_is_accepted_but_not_persisted(env: Env, client: TestClient) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    pub = _mk_pub(client, csrf, env.project_id)
    resp = client.post(
        f"{API}/publications/{pub}/verifications",
        json={"result": "VERIFIED", "note": "人工看过"},
        headers=_wh(csrf),
    )
    assert resp.status_code == 200
    assert "note" not in resp.json()["data"]


# ===========================================================================
# 鉴权 / item 级 404
# ===========================================================================


def test_unauthenticated_is_401(env: Env, client: TestClient) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    pub = _mk_pub(client, csrf, env.project_id)
    client.cookies.clear()
    bare = {CSRF_HEADER_NAME: "x", "Origin": TEST_ORIGIN}
    r1 = client.post(f"{API}/publications/{pub}/links", json={"rawUrl": NOTE_URL}, headers=bare)
    r2 = client.post(
        f"{API}/publications/{pub}/verifications", json={"result": "VERIFIED"}, headers=bare
    )
    r3 = client.get(f"{API}/publication-links", params={"canonicalUrl": NOTE_URL})
    assert (r1.status_code, r2.status_code, r3.status_code) == (401, 401, 401)


def test_non_member_and_cross_project_ids_are_same_shape_404(
    env: Env, client: TestClient
) -> None:
    owner_csrf = _login(client, env.settings, env.owner_id)
    pub_other = _mk_pub(client, owner_csrf, env.other_project_id)  # collab 不是该项目成员

    csrf = _login(client, env.settings, env.collab_id)
    ghost = str(uuid.uuid4())
    for pub in (pub_other, ghost):
        link = _add_link(client, csrf, pub, NOTE_URL)
        ver = _verify(client, csrf, pub, "VERIFIED")
        assert (link.status_code, ver.status_code) == (404, 404)
        assert link.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    # 同形：非成员命中的 404 与不存在的 404 错误码一致，且不暴露项目 id
    a = _add_link(client, csrf, pub_other, NOTE_URL).json()["error"]
    b = _add_link(client, csrf, ghost, NOTE_URL).json()["error"]
    assert a["code"] == b["code"] and a["message"] == b["message"]
    assert set(a["details"]) == set(b["details"])
    assert str(env.other_project_id) not in str(a)
    # 非成员的写没有落库、没有改状态
    assert _link_count(env, env.other_project_id) == 0
    assert (
        _db_scalar(env, "SELECT content_state FROM publications WHERE id = :i", i=pub_other)
        == "UNVERIFIED"
    )


def test_closed_project_delivery_is_403_for_member(env: Env, client: TestClient) -> None:
    """403 用例：结项项目对非管理者的 DELIVER 被 permissions.py 硬拒。
    （成员角色之间没有"有 VIEW 无 DELIVER"的组合，造不出其他 403。）"""
    admin_csrf = _login(client, env.settings, env.admin_id)
    pub = _mk_pub(client, admin_csrf, env.closed_project_id)

    csrf = _login(client, env.settings, env.owner_id)
    link = _add_link(client, csrf, pub, NOTE_URL)
    ver = _verify(client, csrf, pub, "VERIFIED")
    assert (link.status_code, ver.status_code) == (403, 403)
    assert _link_count(env, env.closed_project_id) == 0
    # 对照：同一用户在未结项的项目里同样的请求是 201
    open_pub = _mk_pub(client, csrf, env.project_id)
    assert _add_link(client, csrf, open_pub, NOTE_URL).status_code == 201


# ===========================================================================
# GET /publication-links：跨项目查重 + 可见性隔离
# ===========================================================================


def _seed_two_projects_same_note(env: Env, client: TestClient) -> tuple[str, str]:
    admin_csrf = _login(client, env.settings, env.admin_id)
    pub_a = _mk_pub(client, admin_csrf, env.project_id)  # owner+collab 可见
    pub_b = _mk_pub(client, admin_csrf, env.other_project_id)  # 仅 owner 可见
    assert _add_link(client, admin_csrf, pub_a, "https://a.com/n/5").status_code == 201
    assert _add_link(client, admin_csrf, pub_b, "https://a.com/n/5").status_code == 201
    return pub_a, pub_b


def _get_links(client: TestClient, **params: Any):
    return client.get(f"{API}/publication-links", params=params)


def test_get_links_visibility_isolation(env: Env, client: TestClient) -> None:
    pub_a, pub_b = _seed_two_projects_same_note(env, client)
    q = {"canonicalUrl": "https://a.com/n/5"}

    _login(client, env.settings, env.collab_id)
    r = _get_links(client, **q)
    assert r.status_code == 200, r.text
    assert [x["publicationId"] for x in r.json()["data"]] == [pub_a]
    assert pub_b not in r.text  # 看不到另一个项目的命中

    _login(client, env.settings, env.owner_id)
    r = _get_links(client, **q)
    assert {x["publicationId"] for x in r.json()["data"]} == {pub_a, pub_b}

    _login(client, env.settings, env.outsider_id)
    r = _get_links(client, **q)
    assert r.status_code == 200 and r.json()["data"] == []

    _login(client, env.settings, env.admin_id)
    r = _get_links(client, **q)
    assert {x["publicationId"] for x in r.json()["data"]} == {pub_a, pub_b}
    # 契约字段，不多不少
    assert set(r.json()["data"][0]) == {
        "id", "publicationId", "rawUrl", "canonicalUrl", "linkKind"
    }


def test_get_links_removed_member_loses_visibility(env: Env, client: TestClient) -> None:
    _seed_two_projects_same_note(env, client)
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "UPDATE project_members SET removed_at = now()"
                " WHERE project_id = :p AND user_id = :u"
            ),
            {"p": env.project_id, "u": env.collab_id},
        )
    _login(client, env.settings, env.collab_id)
    assert _get_links(client, canonicalUrl="https://a.com/n/5").json()["data"] == []


def test_get_links_normalizes_the_query_and_validates_it(
    env: Env, client: TestClient
) -> None:
    pub_a, _ = _seed_two_projects_same_note(env, client)
    _login(client, env.settings, env.collab_id)
    hit = _get_links(client, canonicalUrl="HTTPS://a.com/n/5/?utm_source=x")
    assert [x["publicationId"] for x in hit.json()["data"]] == [pub_a]
    miss = _get_links(client, canonicalUrl="https://a.com/n/6")
    assert miss.json()["data"] == []
    assert _get_links(client, canonicalUrl="ftp://x").status_code == 422
    assert _get_links(client).status_code == 422
    assert _get_links(client, canonicalUrl="https://a.com/" + "x" * 2100).status_code == 422


def test_get_links_is_paginated(env: Env, client: TestClient) -> None:
    _seed_two_projects_same_note(env, client)
    _login(client, env.settings, env.owner_id)
    page1 = _get_links(client, canonicalUrl="https://a.com/n/5", limit=1)
    assert page1.status_code == 200, page1.text
    body1 = page1.json()
    assert len(body1["data"]) == 1
    assert body1["page"]["hasMore"] is True
    page2 = _get_links(
        client,
        canonicalUrl="https://a.com/n/5",
        limit=1,
        cursor=body1["page"]["nextCursor"],
    )
    assert page2.status_code == 200, page2.text
    body2 = page2.json()
    assert len(body2["data"]) == 1
    assert body2["data"][0]["id"] != body1["data"][0]["id"]
    assert body2["page"]["hasMore"] is False


# ===========================================================================
# distinct 计数（仓储层，无端点）
# ===========================================================================


def test_count_distinct_publications_counts_publications_not_links(
    env: Env, client: TestClient
) -> None:
    csrf = _login(client, env.settings, env.owner_id)
    pub1 = _mk_pub(client, csrf, env.project_id)
    pub2 = _mk_pub(client, csrf, env.project_id)
    pub_other = _mk_pub(client, csrf, env.other_project_id)
    for pub, raw in (
        (pub1, "https://a.com/1"),
        (pub1, "https://a.com/2"),  # 同一发布第二条链接
        (pub1, "https://a.com/3"),
        (pub2, "https://a.com/4"),
    ):
        assert _add_link(client, csrf, pub, raw).status_code == 201
    admin_csrf = _login(client, env.settings, env.admin_id)
    assert _add_link(client, admin_csrf, pub_other, "https://a.com/1").status_code == 201

    with session_scope(env.settings) as session:
        repo = DeliveryRepository(session)
        # 反向对照：链接行数是 4，发布数是 2
        assert repo.count_links_of_publication(uuid.UUID(pub1)) == 3
        assert repo.count_distinct_publications(env.project_id) == 2
        # 按 canonical：只数命中的发布；别的项目的命中不计入
        assert (
            repo.count_distinct_publications(
                env.project_id, canonical_url="https://a.com/1"
            )
            == 1
        )
        assert repo.count_distinct_publications(env.other_project_id) == 1
        assert (
            repo.count_distinct_publications(
                env.project_id, canonical_url="https://a.com/nope"
            )
            == 0
        )
        raw_rows = session.execute(
            text("SELECT count(*) FROM publication_links WHERE project_id = :p"),
            {"p": env.project_id},
        ).scalar_one()
        assert raw_rows == 4


def test_service_layer_rejects_unknown_publication(env: Env) -> None:
    with session_scope(env.settings) as session:
        repo = DeliveryRepository(session)
        with pytest.raises(d_service.DeliveryError) as info:
            d_service.add_publication_link(
                repo,
                publication_id=uuid.uuid4(),
                raw_url=NOTE_URL,
                can_confirm_cross_project=True,
            )
        assert info.value.reason == d_types.REASON_PUBLICATION_NOT_FOUND
