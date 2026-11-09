"""WP33 API 层端到端：真实 HTTP 走一遍 7 个 operation（状态码 / 错误码 / 字段 / 权限 / 契约）。

`main.py` 是共享文件，router 注册属主线收口动作；本文件按 `test_versions_wp34_api.py` 的先例补挂
（路径探测保证幂等）。造数与夹具复用 `test_quality_wp33.py`。
403 面：`ProjectAction.REPORT` 对 owner / collaborator / admin 恒为 True，**造不出「有 VIEW 无 REPORT」的成员**，
故 403 在本环境不可达，此处如实不测（PHASE_CLOSED 只禁 EDIT / REVIEW / DELIVER，REPORT 仍允许，下面有一条正向用例）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rednote.platform.csrf import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from rednote.platform.database import session_scope
from rednote.platform.tokens import generate_session_token, hash_token

from test_quality_wp33 import (  # noqa: F401  （夹具按名注入）
    Env,
    _clean_own_rows,
    _ensure_own_tables,
    _seed_content,
    _three_with_pair,
    env,
)

API = "/api/v1"
CSRF = "wp33-api-csrf-secret-plaintext"
ORIGIN = "http://testserver"
_PROBE = f"{API}/projects/{{projectId}}/quality-reports"


@pytest.fixture
def app(env: Env) -> FastAPI:
    from rednote.main import create_app

    application = create_app(env.settings)
    if not any(getattr(r, "path", "") == _PROBE for r in application.routes):
        from rednote.modules.quality import router

        application.include_router(router, prefix=env.settings.api_prefix)
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _login(client: TestClient, env: Env, user_id: uuid.UUID | None = None) -> None:
    from rednote.modules.identity.repository import IdentityRepository

    raw = generate_session_token()
    with session_scope(env.settings) as s:
        IdentityRepository(s).create_session(
            user_id=user_id or env.owner_id,
            token_hash=hash_token(raw),
            csrf_secret_hash=hash_token(CSRF),
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, raw)
    client.cookies.set(CSRF_COOKIE_NAME, CSRF)


def _h() -> dict[str, str]:
    return {"X-CSRF-Token": CSRF, "Origin": ORIGIN}


def _post_report(client: TestClient, env: Env, ids: list[uuid.UUID], project_id: uuid.UUID | None = None) -> Any:
    return client.post(
        f"{API}/projects/{project_id or env.project_id}/quality-reports",
        json={"scope": {"kind": "CONTENT_SET", "contentIds": [str(i) for i in ids]}},
        headers=_h(),
    )


def test_unauthenticated_is_401_on_all_endpoints(client: TestClient, env: Env) -> None:
    some = uuid.uuid4()
    calls = [
        ("POST", f"{API}/projects/{env.project_id}/quality-reports", {"scope": {"kind": "PROJECT"}}),
        ("GET", f"{API}/projects/{env.project_id}/quality-reports", None),
        ("GET", f"{API}/quality-reports/{some}", None),
        ("GET", f"{API}/quality-report-versions/{some}", None),
        ("GET", f"{API}/quality-report-versions/{some}/pairs", None),
        ("POST", f"{API}/quality-report-versions/{some}/artifacts", {"format": "JSON"}),
        ("GET", f"{API}/quality-dimensions", None),
    ]
    for method, url, body in calls:
        resp = client.request(method, url, json=body, headers=_h())
        assert resp.status_code == 401, (method, url, resp.text)


def test_create_201_then_200_replay_and_response_shape(client: TestClient, env: Env) -> None:
    contents = _three_with_pair(env)
    _login(client, env)
    ids = [c.content_id for c in contents]
    first = _post_report(client, env, ids)
    assert first.status_code == 201, first.text
    data = first.json()["data"]
    assert data["created"] is True and data["revisionNo"] == 1 and data["outcome"] == "ALERTED"
    assert data["thresholdBasis"] == "PLACEHOLDER_PENDING_OP-228" and "占位" in data["thresholdNote"]
    assert [d["dimension"] for d in data["dimensions"]] == [
        "TITLE", "OPENING", "ENDING", "BODY", "STRUCTURE", "IMAGE_SIMILARITY", "SCENE_CONCENTRATION"]
    nc = [d for d in data["dimensions"] if d["status"] == "NOT_COMPUTED"]
    assert len(nc) == 3 and all(d["verdict"] is None and d["comparedItemCount"] is None for d in nc)
    assert data["okDimensionCount"] + data["alertDimensionCount"] + data["notComputedDimensionCount"] == 7
    for key in ("items", "alignmentSummary", "inputFingerprint", "algoVersion", "staleItemCount"):
        assert key in data, f"响应缺 {key}（被 response_model 剥掉了）"
    again = _post_report(client, env, ids)
    assert again.status_code == 200, again.text
    assert again.json()["data"]["created"] is False and again.json()["data"]["id"] == data["id"]


def test_create_validation_errors_422_and_malformed_400(client: TestClient, env: Env) -> None:
    a = _seed_content(env, seed=0)
    foreign = _seed_content(env, seed=1000, project_id=env.other_project_id)
    _login(client, env)
    resp = _post_report(client, env, [a.content_id, foreign.content_id])
    assert resp.status_code == 422 and resp.json()["error"]["details"]["reason"] == "QUALITY_CONTENT_NOT_IN_PROJECT"
    resp = _post_report(client, env, [a.content_id])
    assert resp.status_code == 422 and resp.json()["error"]["details"]["reason"] == "QUALITY_SCOPE_EMPTY"
    resp = client.post(f"{API}/projects/{env.project_id}/quality-reports",
                       json={"scope": {"kind": "WEEKLY"}}, headers=_h())
    assert resp.status_code == 422 and resp.json()["error"]["details"]["reason"] == "QUALITY_SCOPE_INVALID"
    resp = client.post(f"{API}/projects/{env.project_id}/quality-reports",
                       json={"scope": {"kind": "PROJECT"}, "extra": 1}, headers=_h())
    assert resp.status_code in (400, 422)


def test_non_member_and_cross_project_are_404_everywhere(client: TestClient, env: Env) -> None:
    contents = _three_with_pair(env)
    _login(client, env)
    created = _post_report(client, env, [c.content_id for c in contents]).json()["data"]
    series_id, version_id = created["reportId"], created["id"]
    _login(client, env, env.outsider_id)
    for method, url, body in [
        ("POST", f"{API}/projects/{env.project_id}/quality-reports", {"scope": {"kind": "PROJECT"}}),
        ("GET", f"{API}/projects/{env.project_id}/quality-reports", None),
        ("GET", f"{API}/quality-reports/{series_id}", None),
        ("GET", f"{API}/quality-report-versions/{version_id}", None),
        ("GET", f"{API}/quality-report-versions/{version_id}/pairs", None),
        ("POST", f"{API}/quality-report-versions/{version_id}/artifacts", {"format": "JSON"}),
        ("GET", f"{API}/quality-reports/{uuid.uuid4()}", None),   # 不存在与无权不可区分
    ]:
        resp = client.request(method, url, json=body, headers=_h())
        assert resp.status_code == 404, (method, url, resp.text)
        assert resp.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_read_endpoints_detail_stale_pairs_and_catalog(client: TestClient, env: Env) -> None:
    from test_quality_wp33 import _add_version, _doc

    contents = _three_with_pair(env)
    _login(client, env)
    created = _post_report(client, env, [c.content_id for c in contents]).json()["data"]
    t, b = _doc(4000)
    _add_version(env, contents[2], title=t, body=b)
    detail = client.get(f"{API}/quality-reports/{created['reportId']}").json()["data"]
    assert detail["currentVersionId"] == created["id"] and detail["currentRevisionNo"] == 1
    cv = detail["currentVersion"]
    assert cv["staleItemCount"] == 1 and sorted(i["subjectState"] for i in cv["items"]) == ["CURRENT", "CURRENT", "STALE"]
    listing = client.get(f"{API}/projects/{env.project_id}/quality-reports").json()
    assert "page" in listing and listing["data"][0]["currentVersion"]["id"] == created["id"]
    assert "items" not in listing["data"][0]["currentVersion"]   # 列表不含 subjectState / 明细
    ver = client.get(f"{API}/quality-report-versions/{created['id']}")
    assert ver.status_code == 200 and "pairs" not in ver.json()["data"]
    assert all(i["subjectState"] is None for i in ver.json()["data"]["items"])
    pairs = client.get(f"{API}/quality-report-versions/{created['id']}/pairs?limit=3&dimension=BODY")
    assert pairs.status_code == 200 and len(pairs.json()["data"]) == 1
    assert pairs.json()["data"][0]["similarityPermille"] == 1000 and pairs.json()["data"][0]["approximate"] is True
    bad = client.get(f"{API}/quality-report-versions/{created['id']}/pairs?dimension=NOPE")
    assert bad.status_code == 422 and bad.json()["error"]["details"]["reason"] == "QUALITY_DIMENSION_INVALID"
    cat = client.get(f"{API}/quality-dimensions")
    assert cat.status_code == 200 and [c["implemented"] for c in cat.json()["data"]] == [True] * 4 + [False] * 3
    assert cat.json()["data"][0]["thresholdBasis"] == "PLACEHOLDER_PENDING_OP-228"


def test_artifact_201_then_200_and_unsupported_format(client: TestClient, env: Env) -> None:
    contents = _three_with_pair(env)
    _login(client, env)
    vid = _post_report(client, env, [c.content_id for c in contents]).json()["data"]["id"]
    url = f"{API}/quality-report-versions/{vid}/artifacts"
    first = client.post(url, json={"format": "JSON"}, headers=_h())
    assert first.status_code == 201, first.text
    assert first.json()["data"]["created"] is True and first.json()["data"]["format"] == "JSON"
    again = client.post(url, json={"format": "JSON"}, headers=_h())
    assert again.status_code == 200 and again.json()["data"]["id"] == first.json()["data"]["id"]
    bad = client.post(url, json={"format": "PDF"}, headers=_h())
    assert bad.status_code == 422 and bad.json()["error"]["details"]["reason"] == "QUALITY_ARTIFACT_FORMAT_UNSUPPORTED"


def test_collaborator_admin_and_closed_project_can_generate(client: TestClient, env: Env) -> None:
    contents = _three_with_pair(env)
    ids = [c.content_id for c in contents]
    _login(client, env, env.collab_id)
    assert _post_report(client, env, ids).status_code == 201
    _login(client, env, env.admin_id)
    assert _post_report(client, env, ids).status_code == 200
    closed = [_seed_content(env, seed=s, project_id=env.closed_project_id) for s in (0, 0)]
    _login(client, env)
    assert _post_report(client, env, [c.content_id for c in closed], env.closed_project_id).status_code == 201


def test_routes_are_exactly_seven_operations(app: FastAPI) -> None:
    from rednote.modules.quality import router

    ops = {(m, r.path) for r in router.routes for m in getattr(r, "methods", set())}  # type: ignore[attr-defined]
    assert len(ops) == 7
    assert {p for _m, p in ops} == {
        "/projects/{projectId}/quality-reports", "/quality-reports/{reportId}",
        "/quality-report-versions/{versionId}", "/quality-report-versions/{versionId}/pairs",
        "/quality-report-versions/{versionId}/artifacts", "/quality-dimensions"}
