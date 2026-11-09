"""WP34 API 层端到端：真实 HTTP 走一遍 5 个端点（状态码 / 错误码 / 字段 / 权限 / 契约）。

`main.py` 是共享文件，router 注册属主线收口动作；本文件按 `test_planning_wp31_api.py`
的先例补挂（路径探测保证幂等）。`follow_redirects=False`（G5）。造数与夹具复用 `test_versions_wp34.py`。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rednote.modules.versions.impact import types as it
from rednote.platform.csrf import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from rednote.platform.database import session_scope
from rednote.platform.tokens import generate_session_token, hash_token

from test_versions_wp34 import (  # noqa: F401  （夹具按名注入）
    Env,
    _add_version,
    _clean_own_rows,
    _create_user,
    _edge,
    _ensure_own_tables,
    _events,
    _exported,
    _make_export,
    _new_node,
    _point_to,
    _seed_content,
    env,
)

API = "/api/v1"
CSRF = "wp34-api-csrf-secret-plaintext"
ORIGIN = "http://testserver"
_PROBE = f"{API}/projects/{{projectId}}/export-validity-sweeps"


@pytest.fixture
def app(env: Env) -> FastAPI:
    from rednote.main import create_app

    application = create_app(env.settings)
    if not any(getattr(r, "path", "") == _PROBE for r in application.routes):
        from rednote.modules.versions.impact import router

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


def _base(env: Env, project_id: uuid.UUID | None = None) -> str:
    return f"{API}/projects/{project_id or env.project_id}"


def _all_endpoints(env: Env) -> list[tuple[str, str, Any]]:
    b, x = _base(env), uuid.uuid4()
    return [
        ("GET", f"{b}/rollback-impacts?entityType=CONTENT&entityId={x}&targetVersionId={x}", None),
        ("GET", f"{b}/versions/{x}/impact", None),
        ("GET", f"{b}/exports/{x}/validity", None),
        ("POST", f"{b}/export-validity-sweeps", {"contentItemIds": [str(x)]}),
        ("GET", f"{b}/export-validity-events", None),
    ]


def test_unauthenticated_is_401_on_all_endpoints(client: TestClient, env: Env) -> None:
    for method, url, body in _all_endpoints(env):
        resp = client.request(method, url, json=body, headers=_h())
        assert resp.status_code == 401, (method, url, resp.text)


def test_non_member_is_404_on_all_endpoints(client: TestClient, env: Env) -> None:
    _login(client, env, env.outsider_id)
    for method, url, body in _all_endpoints(env):
        resp = client.request(method, url, json=body, headers=_h())
        assert resp.status_code == 404, (method, url, resp.text)
        assert resp.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_rollback_preview_200_shape_and_error_bodies(client: TestClient, env: Env) -> None:
    c = _seed_content(env, versions=2, point_to_version=1)
    v1, v2 = c.version_ids
    down = _new_node(env, "PLAN")
    _edge(env, down, v1, "BATCH_PLAN")
    _exported_group = _make_export(env, [c])
    _login(client, env)
    q = f"entityType=CONTENT&entityId={c.content_id}&targetVersionId={v2}"
    resp = client.get(f"{_base(env)}/rollback-impacts?{q}")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert set(data) == {"start", "target", "nodes", "traversal", "byType", "exports"}
    assert data["start"] == {"versionId": str(v1), "entityType": "CONTENT",
                             "entityId": str(c.content_id), "revisionNo": 1, "isCurrent": True}
    node = data["nodes"][0]
    assert set(node) == {"versionId", "entityType", "entityId", "revisionNo", "depth",
                         "purposes", "isCurrent", "impactClass"}
    assert node["isCurrent"] is None and node["impactClass"] == it.IMPACT_UNRESOLVED  # null，不是 false
    assert node["purposes"] == ["BATCH_PLAN"]
    assert set(data["traversal"]) == {"complete", "truncation", "maxDepthReached", "nodeCount",
                                      "nodeCountIsLowerBound", "anomalies", "cycleNodeCount"}
    assert data["traversal"]["truncation"] is None and data["traversal"]["anomalies"] == []
    item = data["exports"]["items"][0]
    assert set(item) == {"exportItemId", "exportGroupId", "contentItemId", "frozenVersionId",
                         "exportImpact", "recordedState"}
    assert item["exportGroupId"] == str(_exported_group) and item["exportImpact"] == it.EXPORT_IMPACT_DEPARTING
    assert "requestId" in resp.json()["meta"]

    same = client.get(f"{_base(env)}/rollback-impacts?entityType=CONTENT&entityId={c.content_id}&targetVersionId={v1}")
    assert same.status_code == 422 and same.json()["error"]["details"]["reason"] == it.REASON_TARGET_IS_CURRENT
    media = client.get(f"{_base(env)}/rollback-impacts?entityType=MEDIA&entityId={c.content_id}&targetVersionId={v2}")
    assert media.status_code == 422
    assert media.json()["error"]["details"]["reason"] == it.REASON_ENTITY_TYPE_NOT_SUPPORTED
    ghost = client.get(f"{_base(env)}/rollback-impacts?entityType=CONTENT&entityId={uuid.uuid4()}&targetVersionId={v2}")
    assert ghost.status_code == 404 and ghost.json()["error"]["details"]["reason"] == it.REASON_CONTENT_NOT_FOUND
    missing = client.get(f"{_base(env)}/rollback-impacts?entityType=CONTENT")
    assert missing.status_code == 422
    _exec_null = c  # 投影置空 ⇒ 409
    from test_versions_wp34 import _exec

    _exec(env, "UPDATE content_items SET current_version_id = NULL WHERE id = :c", {"c": _exec_null.content_id})
    conflict = client.get(f"{_base(env)}/rollback-impacts?{q}")
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "INVALID_TRANSITION"
    assert conflict.json()["error"]["details"]["reason"] == it.REASON_NO_CURRENT_VERSION


def test_version_impact_200_and_404_for_unknown_other_project_platform(client: TestClient, env: Env) -> None:
    root = _new_node(env, "BASELINE")
    c = _seed_content(env, versions=1)
    _edge(env, c.version_ids[0], root, "GENERATION_BASELINE")
    _login(client, env)
    resp = client.get(f"{_base(env)}/versions/{root}/impact")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["target"] is None and data["start"]["isCurrent"] is None
    assert [(n["versionId"], n["impactClass"]) for n in data["nodes"]] == [
        (str(c.version_ids[0]), it.IMPACT_REVIEW_REQUIRED)]
    assert data["traversal"] == {"complete": True, "truncation": None, "maxDepthReached": 1,
                                 "nodeCount": 1, "nodeCountIsLowerBound": False, "anomalies": [],
                                 "cycleNodeCount": 0}
    empty = client.get(f"{_base(env)}/versions/{c.version_ids[0]}/impact").json()["data"]
    assert empty["nodes"] == [] and empty["traversal"]["nodeCount"] == 0 and empty["traversal"]["complete"]
    bodies = []
    for vid in (uuid.uuid4(), _new_node(env, project_id=env.other_project_id), _new_node(env, "PROMPT", project_id=None)):
        r = client.get(f"{_base(env)}/versions/{vid}/impact")
        assert r.status_code == 404
        bodies.append((r.json()["error"]["code"], r.json()["error"]["details"]["reason"]))
    assert set(bodies) == {("RESOURCE_NOT_FOUND", it.REASON_VERSION_NOT_FOUND)}  # 三者同形
    assert client.get(f"{_base(env)}/versions/not-a-uuid/impact").status_code == 422


def test_validity_read_does_not_write_and_sweep_then_agrees(client: TestClient, env: Env) -> None:
    c, g, item = _exported(env, versions=1)
    _add_version(env, c)
    _login(client, env)
    got = client.get(f"{_base(env)}/exports/{g}/validity")
    assert got.status_code == 200
    data = got.json()["data"]
    assert data["summary"] == {"valid": 0, "invalid": 1, "unknown": 0}
    row = data["items"][0]
    assert set(row) == {"exportItemId", "contentItemId", "frozenVersionId", "derivedState",
                        "derivedReason", "unknownReason", "recordedState", "inSync"}
    assert (row["derivedState"], row["derivedReason"], row["recordedState"], row["inSync"]) == (
        "INVALID", "VERSION_SUPERSEDED", "VALID", False)
    assert _events(env, item) == []

    swept = client.post(f"{_base(env)}/export-validity-sweeps", json={"contentItemIds": [str(c.content_id)]}, headers=_h())
    assert swept.status_code == 200, swept.text
    s = swept.json()["data"]
    assert s["summary"] == {"invalidated": 1, "restored": 0, "unchanged": 0, "unknown": 0}
    assert s["scope"] == {"kind": "CONTENTS", "contentItemIds": [str(c.content_id)], "fromVersionId": None}
    assert s["items"][0]["action"] == "INVALIDATED" and s["items"][0]["seq"] == 1
    again = client.post(f"{_base(env)}/export-validity-sweeps", json={"contentItemIds": [str(c.content_id)]}, headers=_h())
    assert again.status_code == 200 and again.json()["data"]["summary"]["unchanged"] == 1
    assert client.get(f"{_base(env)}/exports/{g}/validity").json()["data"]["items"][0]["inSync"] is True
    assert client.get(f"{_base(env)}/exports/{uuid.uuid4()}/validity").status_code == 404


def test_sweep_request_validation_and_scope_errors(client: TestClient, env: Env) -> None:
    c = _seed_content(env, versions=1)
    _login(client, env)
    url = f"{_base(env)}/export-validity-sweeps"
    cases: list[tuple[Any, int, str | None]] = [
        ({}, 422, it.REASON_SCOPE_INVALID),
        ({"contentItemIds": [str(c.content_id)], "fromVersionId": str(uuid.uuid4())}, 422, it.REASON_SCOPE_INVALID),
        ({"contentItemIds": []}, 422, it.REASON_CONTENT_IDS_EMPTY),
        ({"contentItemIds": [str(c.content_id)] * 2}, 422, it.REASON_CONTENT_IDS_DUPLICATE),
        ({"contentItemIds": [str(uuid.uuid4()) for _ in range(51)]}, 422, it.REASON_CONTENT_IDS_TOO_MANY),
        ({"contentItemIds": [str(uuid.uuid4())]}, 404, it.REASON_CONTENT_NOT_FOUND),
        ({"fromVersionId": str(uuid.uuid4())}, 404, it.REASON_VERSION_NOT_FOUND),
        ({"bogus": 1}, 422, None),  # extra=forbid
        ({"contentItemIds": ["x"]}, 422, None),
    ]
    for body, status, reason in cases:
        r = client.post(url, json=body, headers=_h())
        assert r.status_code == status, (body, r.text)
        if reason:
            assert r.json()["error"]["details"]["reason"] == reason, body
    assert client.post(url, content=b"not json", headers={**_h(), "Content-Type": "application/json"}).status_code in (400, 422)


def test_sweep_from_version_via_http_and_truncation_409(
    client: TestClient, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rednote.modules.versions.impact import service

    base = _new_node(env, "BASELINE")
    cs = []
    for _ in range(2):
        c, _g, item = _exported(env, versions=1)
        _edge(env, c.version_ids[0], base)
        _add_version(env, c)
        cs.append(item)
    _login(client, env)
    url = f"{_base(env)}/export-validity-sweeps"
    monkeypatch.setattr(service, "MAX_TRAVERSAL_NODES", 1)
    r = client.post(url, json={"fromVersionId": str(base)}, headers=_h())
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == it.REASON_IMPACT_TRUNCATED
    assert all(_events(env, i) == [] for i in cs)
    monkeypatch.undo()
    ok = client.post(url, json={"fromVersionId": str(base)}, headers=_h())
    assert ok.status_code == 200 and ok.json()["data"]["summary"]["invalidated"] == 2
    assert ok.json()["data"]["scope"]["fromVersionId"] == str(base)


def test_closed_project_allows_reads_but_denies_sweep_with_403(client: TestClient, env: Env) -> None:
    c = _seed_content(env, project_id=env.closed_project_id, versions=1)
    g = _make_export(env, [c], project_id=env.closed_project_id)
    _login(client, env)
    base = _base(env, env.closed_project_id)
    assert client.get(f"{base}/exports/{g}/validity").status_code == 200
    assert client.get(f"{base}/export-validity-events").status_code == 200
    r = client.post(f"{base}/export-validity-sweeps", json={"contentItemIds": [str(c.content_id)]}, headers=_h())
    assert r.status_code == 403 and r.json()["error"]["code"] == "PERMISSION_DENIED"


def test_collaborator_and_admin_can_sweep(client: TestClient, env: Env) -> None:
    c, g, item = _exported(env, versions=1)
    _add_version(env, c)
    for uid in (env.collab_id, env.admin_id):
        _login(client, env, uid)
        r = client.post(f"{_base(env)}/export-validity-sweeps", json={"contentItemIds": [str(c.content_id)]}, headers=_h())
        assert r.status_code == 200, (uid, r.text)
    assert len(_events(env, item)) == 1


def test_sweep_cross_project_is_404_and_writes_nothing(client: TestClient, env: Env) -> None:
    foreign, _g, item = _exported(env, project_id=env.other_project_id)
    _add_version(env, foreign)
    _login(client, env)
    r = client.post(f"{_base(env)}/export-validity-sweeps", json={"contentItemIds": [str(foreign.content_id)]}, headers=_h())
    assert r.status_code == 404 and _events(env, item) == []


def test_events_list_filters_pagination_and_404(client: TestClient, env: Env) -> None:
    c, g, item = _exported(env, versions=1)
    _add_version(env, c)
    post = lambda: client.post(  # noqa: E731
        f"{_base(env)}/export-validity-sweeps", json={"contentItemIds": [str(c.content_id)]}, headers=_h())
    _login(client, env)
    post()
    _point_to(env, c, 0)
    post()
    _add_version(env, c)
    post()
    url = f"{_base(env)}/export-validity-events"
    full = client.get(f"{url}?exportGroupId={g}&limit=100")
    assert full.status_code == 200
    body = full.json()
    assert [e["seq"] for e in body["data"]] == [3, 2, 1]  # 最新在前
    assert set(body["page"]) == {"nextCursor", "hasMore"}
    first = body["data"][0]
    assert set(first) == {"id", "exportItemId", "exportGroupId", "contentItemId", "seq", "transition",
                          "reason", "observedVersionId", "observedEligibilityHash", "scopeKind",
                          "sourceVersionId", "createdBy", "createdAt"}
    assert (first["transition"], first["reason"]) == ("INVALIDATED", "VERSION_SUPERSEDED")
    assert body["data"][1]["transition"] == "RESTORED" and body["data"][1]["reason"] is None
    p1 = client.get(f"{url}?exportItemId={item}&limit=2").json()
    assert [e["seq"] for e in p1["data"]] == [3, 2] and p1["page"]["hasMore"] is True
    p2 = client.get(f"{url}?exportItemId={item}&limit=2&cursor={p1['page']['nextCursor']}").json()
    assert [e["seq"] for e in p2["data"]] == [1] and p2["page"]["hasMore"] is False
    assert client.get(f"{url}?exportGroupId={uuid.uuid4()}").status_code == 404
    assert client.get(f"{url}?exportItemId={uuid.uuid4()}").status_code == 404
    other = _base(env, env.other_project_id)
    assert client.get(f"{other}/export-validity-events?exportGroupId={g}").status_code == 404


def test_openapi_declares_error_bodies_without_default_validation_error(app: FastAPI) -> None:
    spec = app.openapi()
    paths = {
        "/projects/{projectId}/rollback-impacts": "get",
        "/projects/{projectId}/versions/{versionId}/impact": "get",
        "/projects/{projectId}/exports/{exportGroupId}/validity": "get",
        "/projects/{projectId}/export-validity-sweeps": "post",
        "/projects/{projectId}/export-validity-events": "get",
    }
    for path, method in paths.items():
        op = spec["paths"][f"{API}{path}"][method]
        assert {"401", "403", "404", "422"} <= set(op["responses"]), path
        assert "HTTPValidationError" not in str(op["responses"]["422"]), path
        assert "200" in op["responses"]
    assert "HTTPValidationError" not in str(spec["paths"][f"{API}/projects/{{projectId}}/export-validity-sweeps"]["post"])
