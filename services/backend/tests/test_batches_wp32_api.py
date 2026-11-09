"""WP32 API 层端到端测试：真实 HTTP 请求走一遍 7 个 operation。

与 `test_batches_wp32.py` 的区别：那份直接调 `service.py`，验证编排逻辑；本文件经
`TestClient` 发真实请求，验证 `api.py` / `dependencies.py` / `schemas.py` 把请求解析、
两步骤鉴权、关口依赖、`If-Match` / `Idempotency-Key`、响应过滤正确接到了服务层。

## 本文件守什么（断精确状态码 + `details.reason`）

| # | 判据 | 守的是什么 |
|---|---|---|
| 1 | 创建首次 **202**、同键同体重放 **200**（`replayed: true`）、同键异体 **409**、缺键 **422** | 幂等契约（批次会花钱） |
| 2 | 关口：默认依赖未接线 → **503** `trial_gate_not_wired`（fail-closed）；stub 未通过 → **409**；通过 → 202 | 没有关口结论不放行 |
| 3 | 数量越界 **422** 带 `limit` + `source` | ADR-13 |
| 4 | 详情带 `ETag`；取消缺 `If-Match` **428**、格式非法 **400**、过期 **412**（带 `currentEtag`） | 乐观锁 |
| 5 | 非成员 / 别的项目 **404**（不区分“不存在”与“不属于你”）；结项项目 EDIT **403**、VIEW **200** | 权限与项目隔离 |
| 6 | 列表 `page` 块不被 `response_model` 剥掉；`reviewSummary` / `progress.counts` 等键齐全 | 输出过滤器 |
| 7 | 路由恰 7 个 operation | 本包形状声明 |

## 为什么要手工 `include_router` / 覆盖关口依赖

`main.py` 是共享文件，分支禁改——router 的注册属主线收口动作，本文件按 `test_review_wp30_api.py`
的先例补挂一次并用路径前缀探测幂等。关口依赖默认未接线，需要通过的用例用
`app.dependency_overrides` 注入 stub（主线接 WP31 后，这些用例不用改）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rednote.modules.batches.dependencies import get_gate_checker
from rednote.modules.batches.types import GateVerdict
from rednote.platform.csrf import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from rednote.platform.database import session_scope
from rednote.platform.tokens import generate_session_token, hash_token

from test_batches_wp32 import (  # noqa: F401  （夹具按名注入）
    BatchEnv,
    _clean_own_rows,
    _ensure_own_tables,
    env,
    job_ids_of,
    make_env,
    set_job_state,
    sql,
)
from test_content_wp26 import API_PREFIX, _create_user

CSRF_SECRET_PLAINTEXT = "wp32-api-csrf-secret-plaintext"
TEST_ORIGIN = "http://testserver"
_PROBE_PATH = f"{API_PREFIX}/batches/{{batchId}}"


@pytest.fixture
def app(env: BatchEnv) -> FastAPI:
    """按注入配置装配 app；`main.py` 尚未接线时补挂 router（幂等）。"""
    from rednote.main import create_app

    assert env.settings.api_prefix == API_PREFIX
    application = create_app(env.settings)
    if not any(getattr(r, "path", "") == _PROBE_PATH for r in application.routes):
        from rednote.modules.batches.api import router as batches_router

        application.include_router(batches_router, prefix=env.settings.api_prefix)
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, env: BatchEnv, user_id: uuid.UUID | None = None) -> None:
    from rednote.modules.identity.repository import IdentityRepository

    raw_token = generate_session_token()
    with session_scope(env.settings) as session:
        IdentityRepository(session).create_session(
            user_id=user_id or env.owner_id,
            token_hash=hash_token(raw_token),
            csrf_secret_hash=hash_token(CSRF_SECRET_PLAINTEXT),
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    client.cookies.set(CSRF_COOKIE_NAME, CSRF_SECRET_PLAINTEXT)


def _headers(**extra: str) -> dict[str, str]:
    headers = {"X-CSRF-Token": CSRF_SECRET_PLAINTEXT, "Origin": TEST_ORIGIN}
    headers.update(extra)
    return headers


def _gate_ok(app: FastAPI) -> None:
    app.dependency_overrides[get_gate_checker] = lambda: (lambda _pid: GateVerdict(passed=True))


def _body(env: BatchEnv, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "batchKind": "SCALE",
        "quantity": 3,
        "planVersionId": str(env.plan_version_id),
        "modelProfileId": str(env.model_profile_id),
        "scope": "FULL",
    }
    body.update(overrides)
    return body


def _post_batch(
    client: TestClient, env: BatchEnv, *, key: str | None = "api-key-1", **overrides: Any
) -> Any:
    headers = _headers()
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post(
        f"{API_PREFIX}/projects/{env.project_id}/batches",
        json=_body(env, **overrides),
        headers=headers,
    )


# ===========================================================================
# ② 创建
# ===========================================================================


def test_create_returns_202_then_200_replay_then_409_on_different_body(
    client: TestClient, app: FastAPI, env: BatchEnv
) -> None:
    _gate_ok(app)
    _login(client, env)
    first = _post_batch(client, env)
    assert first.status_code == 202, first.text
    data = first.json()["data"]
    assert data["replayed"] is False and data["state"] == "SUBMITTED"
    assert data["displayCode"] == "BATCH-S01" and data["quantity"] == 3
    assert data["progress"]["state"] == "QUEUED" and data["progress"]["total"] == 3
    assert data["limit"]["applied"] == 30
    assert data["estimate"]["isEstimate"] is True and data["estimate"]["amount"] is not None
    assert data["reviewSummary"] is None
    assert first.headers["ETag"] == data["etag"]
    for key in ("allocation", "baselineState", "planVersionId", "baselineId", "createdAt"):
        assert key in data, f"响应缺 {key}（被 response_model 剥掉了）"

    again = _post_batch(client, env)
    assert again.status_code == 200, again.text
    assert again.json()["data"]["id"] == data["id"] and again.json()["data"]["replayed"] is True

    different = _post_batch(client, env, quantity=4)
    assert different.status_code == 409, different.text
    assert different.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert different.json()["error"]["details"]["reason"] == "idempotency_key_reused"


def test_create_without_idempotency_key_is_422(
    client: TestClient, app: FastAPI, env: BatchEnv
) -> None:
    _gate_ok(app)
    _login(client, env)
    resp = _post_batch(client, env, key=None)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["reason"] == "idempotency_key_required"


def test_create_rejects_unknown_body_field_with_422(
    client: TestClient, app: FastAPI, env: BatchEnv
) -> None:
    _gate_ok(app)
    _login(client, env)
    resp = _post_batch(client, env, sceneKey="早八通勤")
    assert resp.status_code == 422, resp.text


def test_create_default_gate_reads_wp31_and_blocks_when_not_passed(
    client: TestClient, env: BatchEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认依赖已接 WP31（`trial_bridge`）：无通过的关口 → 409 gate_not_satisfied；无任何写入。

    真实读 `trial_gate_decisions` 需要 WP31 的表；本测试库只建本包的表，所以桩掉桥的读取，
    只验证"默认依赖确实走桥并把未通过翻译成 409"。桥自身的翻译另有单元测试。
    """
    from rednote.modules.batches import dependencies as deps

    monkeypatch.setattr(
        deps,
        "read_gate_verdict",
        lambda _db, _pid: GateVerdict(passed=False, reason="no_gate_decision"),
    )
    _login(client, env)
    resp = _post_batch(client, env)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "INVALID_TRANSITION"
    assert resp.json()["error"]["details"]["reason"] == "gate_not_satisfied"
    assert sql(env, "SELECT count(*) FROM batches WHERE project_id=:p", {"p": env.project_id}) == [(0,)]


def test_create_gate_not_passed_is_409(client: TestClient, app: FastAPI, env: BatchEnv) -> None:
    app.dependency_overrides[get_gate_checker] = lambda: (
        lambda _pid: GateVerdict(passed=False, decision_id=uuid.uuid4(), reason="REJECTED")
    )
    _login(client, env)
    resp = _post_batch(client, env)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "INVALID_TRANSITION"
    assert resp.json()["error"]["details"]["reason"] == "gate_not_satisfied"


def test_create_quantity_above_max_is_422_with_limit_and_source(
    client: TestClient, app: FastAPI, env: BatchEnv
) -> None:
    _gate_ok(app)
    _login(client, env)
    resp = _post_batch(client, env, quantity=31)
    assert resp.status_code == 422, resp.text
    details = resp.json()["error"]["details"]
    assert details["reason"] == "quantity_above_max" and details["limit"] == 30
    assert details["source"] in {"GLOBAL", "INHERITED", "MANUAL"}


def test_create_over_budget_is_422(client: TestClient, app: FastAPI) -> None:
    low = make_env(budget="10")
    _gate_ok(app)  # 注意：app 夹具绑定的是 env 夹具的库配置，同一私有库
    _login(client, low)
    resp = client.post(
        f"{API_PREFIX}/projects/{low.project_id}/batches",
        json=_body(low),
        headers=_headers(**{"Idempotency-Key": "k-low"}),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "BUDGET_EXCEEDED"
    assert resp.json()["error"]["details"]["reason"] == "batch_estimate_exceeds_available"


# ===========================================================================
# ① 预览
# ===========================================================================


def test_preview_returns_200_and_reports_gate_not_passed(
    client: TestClient, env: BatchEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rednote.modules.batches import dependencies as deps

    monkeypatch.setattr(
        deps,
        "read_gate_verdict",
        lambda _db, _pid: GateVerdict(passed=False, reason="no_gate_decision"),
    )
    _login(client, env)
    resp = client.post(
        f"{API_PREFIX}/projects/{env.project_id}/batch-previews",
        json=_body(env, quantity=5),
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["gate"]["passed"] is False and data["gate"]["reason"] == "no_gate_decision"
    assert data["estimate"]["amount"] == "125.00000000"
    assert data["limit"]["applied"] == 30
    assert sql(env, "SELECT count(*) FROM batches WHERE project_id=:p", {"p": env.project_id}) == [(0,)]


# ===========================================================================
# ③④⑤ 读
# ===========================================================================


def test_get_detail_list_and_items(client: TestClient, app: FastAPI, env: BatchEnv) -> None:
    _gate_ok(app)
    _login(client, env)
    created = _post_batch(client, env, quantity=3).json()["data"]
    batch_id = created["id"]

    detail = client.get(f"{API_PREFIX}/batches/{batch_id}")
    assert detail.status_code == 200, detail.text
    assert detail.headers["ETag"] == detail.json()["data"]["etag"] == created["etag"]
    assert detail.json()["data"]["baselineState"] == "CURRENT"

    listing = client.get(f"{API_PREFIX}/projects/{env.project_id}/batches?limit=1")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert "page" in body and body["page"]["hasMore"] is False  # page 块没被剥掉
    assert body["data"][0]["id"] == batch_id and body["data"][0]["baselineState"] is None

    items = client.get(f"{API_PREFIX}/batches/{batch_id}/items?limit=2")
    assert items.status_code == 200, items.text
    page = items.json()
    assert [i["ordinal"] for i in page["data"]] == [1, 2] and page["page"]["hasMore"] is True
    assert page["data"][0]["jobState"] == "QUEUED" and page["data"][0]["sceneKey"] is None
    nxt = client.get(
        f"{API_PREFIX}/batches/{batch_id}/items?limit=2&cursor={page['page']['nextCursor']}"
    )
    assert [i["ordinal"] for i in nxt.json()["data"]] == [3]

    bad = client.get(f"{API_PREFIX}/projects/{env.project_id}/batches?batchKind=WEEKLY")
    assert bad.status_code == 422 and bad.json()["error"]["details"]["reason"] == "batch_kind_unknown"
    bad2 = client.get(f"{API_PREFIX}/projects/{env.project_id}/batches?state=DONE")
    assert bad2.status_code == 422


def test_unknown_batch_is_404(client: TestClient, env: BatchEnv) -> None:
    _login(client, env)
    resp = client.get(f"{API_PREFIX}/batches/{uuid.uuid4()}")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["details"]["reason"] == "BATCH_NOT_FOUND"


def test_non_member_and_other_project_get_404_everywhere(
    client: TestClient, app: FastAPI, env: BatchEnv
) -> None:
    """T-READ-03：非成员访问 → 404（与“不存在”不可区分）；覆盖全部 7 个 operation。"""
    _gate_ok(app)
    _login(client, env)
    batch_id = _post_batch(client, env, quantity=2).json()["data"]["id"]
    outsider = _create_user(env.settings)
    client.cookies.clear()
    _login(client, env, outsider)
    project = f"{API_PREFIX}/projects/{env.project_id}"
    responses = [
        client.get(f"{project}/batches"),
        client.get(f"{API_PREFIX}/batches/{batch_id}"),
        client.get(f"{API_PREFIX}/batches/{batch_id}/items"),
        client.post(f"{project}/batches", json=_body(env), headers=_headers(**{"Idempotency-Key": "x"})),
        client.post(f"{project}/batch-previews", json=_body(env), headers=_headers()),
        client.post(f"{API_PREFIX}/batches/{batch_id}/cancellations", headers=_headers()),
        client.post(
            f"{API_PREFIX}/batches/{batch_id}/retries", headers=_headers(**{"Idempotency-Key": "r"})
        ),
    ]
    assert [r.status_code for r in responses] == [404] * 7, [r.text for r in responses]


def test_closed_project_blocks_edit_but_allows_view(
    client: TestClient, app: FastAPI, env: BatchEnv
) -> None:
    _gate_ok(app)
    _login(client, env)
    batch_id = _post_batch(client, env, quantity=1).json()["data"]["id"]
    sql(
        env,
        "UPDATE projects SET phase='CLOSED', closed_at=now() WHERE id=:p",
        {"p": env.project_id},
    )
    assert client.get(f"{API_PREFIX}/batches/{batch_id}").status_code == 200
    denied = _post_batch(client, env, key="after-close")
    assert denied.status_code == 403, denied.text
    cancel = client.post(f"{API_PREFIX}/batches/{batch_id}/cancellations", headers=_headers())
    assert cancel.status_code == 403, cancel.text


# ===========================================================================
# ⑥ 取消
# ===========================================================================


def test_cancel_requires_if_match_and_is_idempotent(
    client: TestClient, app: FastAPI, env: BatchEnv
) -> None:
    _gate_ok(app)
    _login(client, env)
    created = _post_batch(client, env, quantity=2).json()["data"]
    url = f"{API_PREFIX}/batches/{created['id']}/cancellations"

    missing = client.post(url, headers=_headers())
    assert missing.status_code == 428, missing.text
    malformed = client.post(url, headers=_headers(**{"If-Match": "not-an-etag"}))
    assert malformed.status_code == 400, malformed.text
    stale = client.post(url, headers=_headers(**{"If-Match": f'"{created["id"]}:99"'}))
    assert stale.status_code == 412, stale.text
    assert stale.json()["error"]["details"]["currentEtag"] == created["etag"]

    ok = client.post(url, headers=_headers(**{"If-Match": created["etag"]}))
    assert ok.status_code == 202, ok.text
    data = ok.json()["data"]
    assert data["state"] == "CANCEL_REQUESTED" and data["cancelRequestedAt"] is not None
    assert data["etag"] != created["etag"] and ok.headers["ETag"] == data["etag"]
    assert data["progress"]["state"] == "CANCELLING"

    repeat = client.post(url, headers=_headers(**{"If-Match": data["etag"]}))
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["data"]["etag"] == data["etag"]  # lock_version 不再增长


# ===========================================================================
# ⑦ 重做
# ===========================================================================


def test_retries_contract(client: TestClient, app: FastAPI, env: BatchEnv) -> None:
    _gate_ok(app)
    _login(client, env)
    created = _post_batch(client, env, quantity=3).json()["data"]
    url = f"{API_PREFIX}/batches/{created['id']}/retries"
    job_ids = job_ids_of(env, uuid.UUID(created["id"]))
    for job_id, state in zip(job_ids, ["FAILED", "WAITING_RECONCILIATION", "SUCCEEDED"], strict=True):
        set_job_state(env, job_id, state)

    no_key = client.post(url, headers=_headers(**{"If-Match": created["etag"]}))
    assert no_key.status_code == 422, no_key.text
    assert no_key.json()["error"]["details"]["reason"] == "idempotency_key_required"
    no_match = client.post(url, headers=_headers(**{"Idempotency-Key": "k1"}))
    assert no_match.status_code == 428, no_match.text

    ok = client.post(url, headers=_headers(**{"Idempotency-Key": "k1", "If-Match": created["etag"]}))
    assert ok.status_code == 202, ok.text
    body = ok.json()["data"]
    assert body["retriedCount"] == 1 and body["retried"][0]["roundNo"] == 2

    # 重放：即使 If-Match 已过期（首次重做推进了 lock_version），也回放原响应。
    replay = client.post(url, headers=_headers(**{"Idempotency-Key": "k1", "If-Match": created["etag"]}))
    assert replay.status_code == 202 and replay.json()["data"] == body
    # 异体（不同批次同键由 scope 区分；同批次同键同体才是重放）——换键、旧 ETag → 412。
    stale = client.post(url, headers=_headers(**{"Idempotency-Key": "k2", "If-Match": created["etag"]}))
    assert stale.status_code == 412, stale.text

    # 未知态没有被重做：needsReconciliation 仍为 1。
    detail = client.get(f"{API_PREFIX}/batches/{created['id']}").json()["data"]
    assert detail["progress"]["needsReconciliation"] == 1
    assert detail["progress"]["state"] == "RUNNING"


def test_retry_nothing_to_retry_is_409(client: TestClient, app: FastAPI, env: BatchEnv) -> None:
    _gate_ok(app)
    _login(client, env)
    created = _post_batch(client, env, quantity=1).json()["data"]
    resp = client.post(
        f"{API_PREFIX}/batches/{created['id']}/retries",
        headers=_headers(**{"Idempotency-Key": "k", "If-Match": created["etag"]}),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["reason"] == "nothing_to_retry"


# ===========================================================================
# 形状声明
# ===========================================================================


def test_router_exposes_exactly_seven_operations(app: FastAPI) -> None:
    from rednote.modules.batches.api import router

    operations = {
        (route.path, method)
        for route in router.routes
        for method in getattr(route, "methods", set())
    }
    assert operations == {
        ("/projects/{projectId}/batch-previews", "POST"),
        ("/projects/{projectId}/batches", "POST"),
        ("/projects/{projectId}/batches", "GET"),
        ("/batches/{batchId}", "GET"),
        ("/batches/{batchId}/items", "GET"),
        ("/batches/{batchId}/cancellations", "POST"),
        ("/batches/{batchId}/retries", "POST"),
    }
    assert len({path for path, _ in operations}) == 6
    mounted = {getattr(r, "path", "") for r in app.routes}
    assert all(f"{API_PREFIX}{path}" in mounted for path, _ in operations)
