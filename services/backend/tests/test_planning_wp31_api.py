"""WP31 API 层端到端：真实 HTTP 走一遍 9 个端点（状态码 / 错误码 / 字段 / 并发头）。

`main.py` 是共享文件，router 注册属主线收口动作；本文件按 `test_review_wp30_api.py`
的先例补挂（用路径探测保证幂等）。`follow_redirects=False`（G5）。
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

from test_content_wp26 import API_PREFIX, _create_user
from test_planning_wp31 import (  # noqa: F401  （夹具按名注入）
    TrialEnv,
    _approve_all,
    _bind_all,
    _clean_own_rows,
    _clean_content_rows,
    _ensure_own_tables,
    _ensure_content_tables,
    _lv,
    _ready,
    _review,
    _reviewing,
    _rows,
    _scalar,
    _seed_content,
    _slots,
    env,
)
from rednote.modules.planning.trial import types as tt

CSRF_SECRET_PLAINTEXT = "wp31-api-csrf-secret-plaintext"
TEST_ORIGIN = "http://testserver"
_PROBE_PATH = f"{API_PREFIX}/projects/{{projectId}}/trial-rounds"


@pytest.fixture
def app(env: TrialEnv) -> FastAPI:
    from rednote.main import create_app

    application = create_app(env.settings)
    if not any(getattr(r, "path", "") == _PROBE_PATH for r in application.routes):
        from rednote.modules.planning.trial import router

        application.include_router(router, prefix=env.settings.api_prefix)
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _login(client: TestClient, env: TrialEnv, user_id: uuid.UUID | None = None) -> None:
    from rednote.modules.identity.repository import IdentityRepository

    raw = generate_session_token()
    with session_scope(env.settings) as s:
        IdentityRepository(s).create_session(
            user_id=user_id or env.owner_id,
            token_hash=hash_token(raw),
            csrf_secret_hash=hash_token(CSRF_SECRET_PLAINTEXT),
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
    client.cookies.set(SESSION_COOKIE_NAME, raw)
    client.cookies.set(CSRF_COOKIE_NAME, CSRF_SECRET_PLAINTEXT)


def _h(**extra: str) -> dict[str, str]:
    h = {"X-CSRF-Token": CSRF_SECRET_PLAINTEXT, "Origin": TEST_ORIGIN}
    h.update(extra)
    return h


def _etag(env: TrialEnv, rid: uuid.UUID) -> str:
    return f'"{rid}:{_lv(env, rid)}"'


def _base(env: TrialEnv) -> str:
    return f"{API_PREFIX}/projects/{env.project_id}"


def _post_round(client: TestClient, env: TrialEnv, plan: uuid.UUID, **extra: Any) -> Any:
    body = {"kind": "TRIAL", "planVersionId": str(plan), **extra.pop("body", {})}
    return client.post(f"{_base(env)}/trial-rounds", json=body, headers=_h(**extra))


# ---------------------------------------------------------------------------
# 401 / 404（全 9 个端点）
# ---------------------------------------------------------------------------


def _all_endpoints(env: TrialEnv, rid: uuid.UUID, sid: uuid.UUID) -> list[tuple[str, str, Any]]:
    b = _base(env)
    r = f"{b}/trial-rounds/{rid}"
    return [
        ("GET", f"{b}/trial-entry-check", None),
        ("POST", f"{b}/trial-rounds", {"kind": "TRIAL", "planVersionId": str(uuid.uuid4())}),
        ("GET", f"{b}/trial-rounds", None),
        ("GET", r, None),
        ("POST", f"{r}/transitions", {"toState": "CANCELLED", "reason": "x"}),
        ("PUT", f"{r}/slots/{sid}/content", {"contentItemId": str(uuid.uuid4())}),
        ("POST", f"{r}/slots/{sid}/replacements", None),
        ("GET", f"{r}/gate", None),
        ("POST", f"{r}/gate-decisions", {"approvedSceneKeys": ["s1"], "reason": "x"}),
    ]


def test_h01_unauthenticated_is_401_on_all_endpoints(client: TestClient, env: TrialEnv) -> None:
    for method, url, body in _all_endpoints(env, uuid.uuid4(), uuid.uuid4()):
        resp = client.request(method, url, json=body, headers=_h())
        assert resp.status_code == 401, (method, url, resp.text)


def test_h01_non_member_is_404_on_all_endpoints(client: TestClient, env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    outsider = _create_user(env.settings, name="WP31 非成员")
    _login(client, env, outsider)
    sid = next(iter(filled.items))
    for method, url, body in _all_endpoints(env, filled.rid, sid):
        resp = client.request(method, url, json=body, headers=_h(**{"If-Match": _etag(env, filled.rid)}))
        assert resp.status_code == 404, (method, url, resp.text)


def test_unknown_round_and_cross_project_round_are_404(client: TestClient, env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    _login(client, env)
    other = f"{API_PREFIX}/projects/{env.other_project_id}/trial-rounds/{filled.rid}"
    assert client.get(other).status_code == 404
    resp = client.get(f"{_base(env)}/trial-rounds/{uuid.uuid4()}")
    assert resp.status_code == 404 and resp.json()["error"]["details"]["reason"] == tt.REASON_ROUND_NOT_FOUND


# ---------------------------------------------------------------------------
# ① 入口预检 / ② 创建 / ③④ 读
# ---------------------------------------------------------------------------


def test_entry_check_200_shape_and_no_write(client: TestClient, env: TrialEnv) -> None:
    _login(client, env)
    resp = client.get(f"{_base(env)}/trial-entry-check")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["ok"] is False
    codes = {c["code"]: c for c in data["conditions"]}
    assert codes["ACTIVE_BASELINE"]["satisfied"] is False
    assert codes["PLAN_BOUND_TO_ACTIVE_BASELINE"]["satisfied"] is None  # 未评估 != False
    assert set(data["limits"]) == {"trial", "source"}
    ready = _ready(env)
    resp = client.get(f"{_base(env)}/trial-entry-check", params={"planVersionId": str(ready.plan_id)})
    assert resp.json()["data"]["ok"] is True and resp.json()["data"]["limits"]["trial"] == 10
    assert _scalar(env, "SELECT count(*) FROM trial_rounds") == 0


def test_create_round_201_then_replay_200_and_conflicts(client: TestClient, env: TrialEnv) -> None:
    ready = _ready(env)
    _login(client, env)
    resp = _post_round(client, env, ready.plan_id, **{"Idempotency-Key": "api-1"})
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    for key in ("id", "roundNo", "kind", "state", "plannedQuantity", "requestedQuantity",
                "unallocatedQuantity", "limitApplied", "limitSource", "etag", "slots",
                "planSnapshot", "statistics", "decision", "baselineId", "planVersionId"):
        assert key in data, key
    assert data["state"] == "PLANNED" and len(data["slots"]) == 10
    assert data["unallocatedQuantity"] == 0
    assert data["statistics"]["unbound"] == 10 and data["statistics"]["approvalRate"]["permille"] is None
    assert data["slots"][0]["status"] == "UNBOUND" and data["slots"][0]["contentItemId"] is None
    again = _post_round(client, env, ready.plan_id, **{"Idempotency-Key": "api-1"})
    assert again.status_code == 200 and again.json()["data"]["id"] == data["id"]
    diff = _post_round(client, env, ready.plan_id, body={"quantity": 4}, **{"Idempotency-Key": "api-1"})
    assert diff.status_code == 409 and diff.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    dup = _post_round(client, env, ready.plan_id)
    assert dup.status_code == 409 and dup.json()["error"]["details"]["reason"] == tt.REASON_ACTIVE_ROUND_EXISTS


def test_create_round_validation_errors(client: TestClient, env: TrialEnv) -> None:
    ready = _ready(env)
    _login(client, env)
    over = _post_round(client, env, ready.plan_id, body={"quantity": 11})
    assert over.status_code == 422
    details = over.json()["error"]["details"]
    assert details["reason"] == "QUANTITY_ABOVE_MAX" and details["limit"] == 10 and "source" in details
    assert _post_round(client, env, ready.plan_id, body={"bogus": 1}).status_code == 422  # extra=forbid
    assert _post_round(client, env, ready.plan_id, body={"kind": "X"}).status_code == 422
    no_baseline = client.post(
        f"{API_PREFIX}/projects/{env.other_project_id}/trial-rounds",
        json={"kind": "TRIAL", "planVersionId": str(ready.plan_id)}, headers=_h(),
    )
    assert no_baseline.status_code == 409
    assert no_baseline.json()["error"]["details"]["reason"] == tt.REASON_NO_ACTIVE_BASELINE


def test_list_and_detail_200_and_gets_do_not_write(client: TestClient, env: TrialEnv) -> None:
    ready = _ready(env)
    _login(client, env)
    rid = _post_round(client, env, ready.plan_id).json()["data"]["id"]
    before = _lv(env, uuid.UUID(rid))
    listing = client.get(f"{_base(env)}/trial-rounds", params={"limit": 1})
    assert listing.status_code == 200
    body = listing.json()
    assert body["data"][0]["id"] == rid and "page" in body and body["page"]["hasMore"] is False
    assert body["data"][0]["statistics"]["liveSlots"] == 10
    detail = client.get(f"{_base(env)}/trial-rounds/{rid}")
    assert detail.status_code == 200 and detail.json()["data"]["etag"] == f'"{rid}:{before}"'
    assert _lv(env, uuid.UUID(rid)) == before


def test_list_pagination_cursor(client: TestClient, env: TrialEnv) -> None:
    ready = _ready(env)
    _login(client, env)
    for _ in range(2):
        rid = _post_round(client, env, ready.plan_id).json()["data"]["id"]
        client.post(f"{_base(env)}/trial-rounds/{rid}/transitions",
                    json={"toState": "CANCELLED", "reason": "翻页"},
                    headers=_h(**{"If-Match": _etag(env, uuid.UUID(rid))}))
    first = client.get(f"{_base(env)}/trial-rounds", params={"limit": 1}).json()
    assert first["page"]["hasMore"] is True and first["data"][0]["roundNo"] == 2
    second = client.get(f"{_base(env)}/trial-rounds",
                        params={"limit": 1, "cursor": first["page"]["nextCursor"]}).json()
    assert second["data"][0]["roundNo"] == 1 and second["page"]["hasMore"] is False


# ---------------------------------------------------------------------------
# ⑤ 迁移 + If-Match 全套
# ---------------------------------------------------------------------------


def test_transition_if_match_matrix_and_retry_double_code(client: TestClient, env: TrialEnv) -> None:
    ready = _ready(env)
    _login(client, env)
    rid = uuid.UUID(_post_round(client, env, ready.plan_id).json()["data"]["id"])
    url = f"{_base(env)}/trial-rounds/{rid}/transitions"
    body = {"toState": "GENERATING"}
    assert client.post(url, json=body, headers=_h()).status_code == 428
    assert client.post(url, json=body, headers=_h(**{"If-Match": "*"})).status_code == 428
    assert client.post(url, json=body, headers=_h(**{"If-Match": "garbage"})).status_code == 400
    stale = _etag(env, rid)
    ok = client.post(url, json=body, headers=_h(**{"If-Match": stale}))
    assert ok.status_code == 200 and ok.json()["data"]["state"] == "GENERATING"
    assert ok.json()["data"]["etag"] == _etag(env, rid)
    # 重试：旧 ETag 412（带 currentEtag）；新 ETag 同动作 409。
    old = client.post(url, json=body, headers=_h(**{"If-Match": stale}))
    assert old.status_code == 412
    assert old.json()["error"]["details"]["currentEtag"] == _etag(env, rid)
    again = client.post(url, json=body, headers=_h(**{"If-Match": _etag(env, rid)}))
    assert again.status_code == 409 and again.json()["error"]["code"] == "INVALID_TRANSITION"


def test_transition_error_codes(client: TestClient, env: TrialEnv) -> None:
    ready = _ready(env)
    _login(client, env)
    rid = uuid.UUID(_post_round(client, env, ready.plan_id).json()["data"]["id"])
    url = f"{_base(env)}/trial-rounds/{rid}/transitions"

    def go(body: dict[str, Any]) -> Any:
        return client.post(url, json=body, headers=_h(**{"If-Match": _etag(env, rid)}))

    r = go({"toState": "GATE_PASSED"})
    assert r.status_code == 422 and r.json()["error"]["details"]["reason"] == tt.REASON_TO_STATE_USE_GATE_DECISION
    assert go({"toState": "NOPE"}).status_code == 422
    assert go({"toState": "CANCELLED"}).status_code == 422  # 取消必须带理由
    r = go({"toState": "REVIEWING"})
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == tt.REASON_ROUND_TRANSITION_ILLEGAL
    assert go({"toState": "CANCELLED", "reason": "取消"}).status_code == 200
    r = go({"toState": "GENERATING"})
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == tt.REASON_ROUND_TERMINAL


# ---------------------------------------------------------------------------
# ⑥ 绑定内容 / ⑦ 补产
# ---------------------------------------------------------------------------


def test_bind_content_put(client: TestClient, env: TrialEnv) -> None:
    ready = _ready(env)
    _login(client, env)
    rid = uuid.UUID(_post_round(client, env, ready.plan_id).json()["data"]["id"])
    client.post(f"{_base(env)}/trial-rounds/{rid}/transitions", json={"toState": "GENERATING"},
                headers=_h(**{"If-Match": _etag(env, rid)}))
    sid, _no, scene = _slots(env, rid)[0]
    cid, vid = _seed_content(env, scene)
    url = f"{_base(env)}/trial-rounds/{rid}/slots/{sid}/content"
    first = client.put(url, json={"contentItemId": str(cid)}, headers=_h())
    assert first.status_code == 200, first.text
    d = first.json()["data"]
    assert d["contentItemId"] == str(cid) and d["status"] == "AWAITING_REVIEW"
    assert d["currentVersionId"] == str(vid) and d["contentState"] == "GENERATED"
    assert client.put(url, json={"contentItemId": str(cid)}, headers=_h()).status_code == 200  # 重放
    other, _ov = _seed_content(env, scene)
    r = client.put(url, json={"contentItemId": str(other)}, headers=_h())
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == tt.REASON_SLOT_ALREADY_BOUND
    sid2 = _slots(env, rid)[1][0]
    bad, _bv = _seed_content(env, scene, batch_kind="SCALE")
    r = client.put(f"{_base(env)}/trial-rounds/{rid}/slots/{sid2}/content",
                   json={"contentItemId": str(bad)}, headers=_h())
    assert r.status_code == 422 and r.json()["error"]["details"]["reason"] == tt.REASON_CONTENT_BATCH_KIND_MISMATCH
    r = client.put(f"{_base(env)}/trial-rounds/{rid}/slots/{uuid.uuid4()}/content",
                   json={"contentItemId": str(cid)}, headers=_h())
    assert r.status_code == 404


def test_replacement_post(client: TestClient, env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    _login(client, env)
    victim = next(iter(filled.items))
    url = f"{_base(env)}/trial-rounds/{filled.rid}/slots/{victim}/replacements"
    assert client.post(url, headers=_h()).status_code == 428
    r = client.post(url, headers=_h(**{"If-Match": _etag(env, filled.rid)}))
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == tt.REASON_SLOT_NOT_ELIMINATED
    _review(env, filled.items[victim][1], filled.items[victim][2], "ELIMINATED")
    stale = _etag(env, filled.rid)
    created = client.post(url, headers=_h(**{"If-Match": stale}))
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["slot"]["replacesSlotId"] == str(victim) and data["slot"]["status"] == "UNBOUND"
    assert data["round"]["state"] == "GENERATING" and data["round"]["etag"] == _etag(env, filled.rid)
    assert client.post(url, headers=_h(**{"If-Match": stale})).status_code == 412
    r = client.post(url, headers=_h(**{"If-Match": _etag(env, filled.rid)}))
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == tt.REASON_SLOT_ALREADY_REPLACED
    detail = client.get(f"{_base(env)}/trial-rounds/{filled.rid}").json()["data"]
    by_id = {s["id"]: s for s in detail["slots"]}
    assert by_id[str(victim)]["status"] == "REPLACED" and by_id[str(victim)]["replacedBySlotId"] == data["slot"]["id"]


# ---------------------------------------------------------------------------
# ⑧ 关口 / ⑨ 批准
# ---------------------------------------------------------------------------


def test_gate_get_and_decision_flow(client: TestClient, env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    _login(client, env)
    gate_url = f"{_base(env)}/trial-rounds/{filled.rid}/gate"
    g = client.get(gate_url)
    assert g.status_code == 200
    gd = g.json()["data"]
    assert gd["satisfiable"] is False and gd["decision"] is None
    assert set(gd["conditions"]) == set(tt.GATE_CONDITION_CODES)
    assert gd["conditions"][tt.GATE_SELF_CHECK_COMPLETE]["blocking"] is False
    assert {d["sceneKey"] for d in gd["directions"]} == {"s1", "s2"}

    url = f"{_base(env)}/trial-rounds/{filled.rid}/gate-decisions"
    body = {"approvedSceneKeys": ["s1"], "reason": "第一方向放行"}
    assert client.post(url, json=body, headers=_h()).status_code == 428
    r = client.post(url, json=body, headers=_h(**{"If-Match": _etag(env, filled.rid)}))
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == tt.REASON_GATE_NOT_SATISFIED
    _approve_all(env, filled, except_scenes=("s2",))
    r = client.post(url, json={"approvedSceneKeys": ["s1", "s2"], "reason": "x"},
                    headers=_h(**{"If-Match": _etag(env, filled.rid)}))
    assert r.status_code == 422 and r.json()["error"]["code"] == "DIRECTION_UNVERIFIED"
    assert r.json()["error"]["details"]["sceneKeys"] == ["s2"]
    stale = _etag(env, filled.rid)
    ok = client.post(url, json=body, headers=_h(**{"If-Match": stale}))
    assert ok.status_code == 201, ok.text
    data = ok.json()["data"]
    assert data["round"]["state"] == "GATE_PASSED"
    dec = data["decision"]
    assert dec["approvedSceneKeys"] == ["s1"] and dec["stale"] is False and dec["staleReasons"] == []
    assert dec["reason"] == "第一方向放行" and len(dec["basis"]) == 10
    # 重试双码
    assert client.post(url, json=body, headers=_h(**{"If-Match": stale})).status_code == 412
    again = client.post(url, json=body, headers=_h(**{"If-Match": _etag(env, filled.rid)}))
    assert again.status_code == 409 and again.json()["error"]["details"]["reason"] == tt.REASON_ROUND_TERMINAL
    g2 = client.get(gate_url).json()["data"]
    assert g2["decision"]["id"] == dec["id"] and g2["state"] == "GATE_PASSED"


def test_gate_decision_missing_reason_is_422_and_extra_forbidden(client: TestClient, env: TrialEnv) -> None:
    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    _approve_all(env, filled)
    _login(client, env)
    url = f"{_base(env)}/trial-rounds/{filled.rid}/gate-decisions"
    h = _h(**{"If-Match": _etag(env, filled.rid)})
    assert client.post(url, json={"approvedSceneKeys": ["s1"]}, headers=h).status_code == 422
    assert client.post(url, json={"approvedSceneKeys": ["s1"], "reason": " "}, headers=h).status_code == 422
    assert client.post(url, json={"approvedSceneKeys": [], "reason": "x"}, headers=h).status_code == 422
    assert client.post(url, json={"approvedSceneKeys": ["s1"], "reason": "x", "extra": 1}, headers=h).status_code == 422
    assert _scalar(env, "SELECT count(*) FROM trial_gate_decisions") == 0


def test_end_to_end_seam_after_http_approval(client: TestClient, env: TrialEnv) -> None:
    from rednote.modules.planning.trial import trial_gate_status

    ready = _ready(env)
    filled = _reviewing(env, ready.plan_id)
    _approve_all(env, filled)
    _login(client, env)
    r = client.post(f"{_base(env)}/trial-rounds/{filled.rid}/gate-decisions",
                    json={"approvedSceneKeys": ["s1", "s2"], "reason": "全放行"},
                    headers=_h(**{"If-Match": _etag(env, filled.rid)}))
    assert r.status_code == 201
    with session_scope(env.settings) as s:
        st = trial_gate_status(s, project_id=env.project_id)
    assert st.passed and st.verified_scene_keys == ("s1", "s2")
    assert _rows(env, "SELECT count(*) FROM audit_events WHERE object_id = :r", {"r": filled.rid})[0][0] >= 1
