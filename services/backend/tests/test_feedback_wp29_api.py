"""WP29 API 层端到端烟雾测试：真实 HTTP 请求走一遍 AC-28 核心场景。

与 `test_feedback_wp29.py` 的区别：那份文件直接调 `service.py` 的函数，
验证的是编排逻辑本身；本文件经 `TestClient` 发真实请求，验证
`api.py`/`dependencies.py`/`schemas.py` 这一层把请求解析、鉴权、
`If-Match` header 正确接到了服务层——这条路径此前只做过 `ast.parse`
与路由注册检查，没有发过一次真实请求。

只覆盖 AC-28 最核心的一条：两次编辑并发时，第二次撞 412 且零写入，
用回带的 `currentEtag` 重放能成功（协议 §1.2 + 开工方案 §5.2 的时序）。
鉴权/CSRF 设施照抄 `test_content_wp26.py` 的 `_login`/`_write_headers`，
只是把 `who` 固定成 `owner`（`FeedbackEnv` 没有 admin/outsider 两个角色，
本文件不需要测权限矩阵，那是 content 包自己的覆盖范围）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rednote.platform.csrf import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from rednote.platform.database import session_scope
from rednote.platform.tokens import generate_session_token, hash_token

from test_content_wp26 import API_PREFIX
from test_feedback_wp29 import FeedbackEnv, _seed_content, env  # noqa: F401

CSRF_SECRET_PLAINTEXT = "wp29-api-csrf-secret-plaintext"
TEST_ORIGIN = "http://testserver"


@pytest.fixture
def app(env: FeedbackEnv) -> FastAPI:
    """真实 `create_app`：验证 `main.py` 已经把 `feedback_router` 接上，
    不需要本文件再手工 `include_router`（与 `test_content_wp26.py` 的
    `app` fixture不同——那是在 `main.py` 尚未接线时的兜底，本包已接线）。
    """
    from rednote.main import create_app

    return create_app(env.settings)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, env: FeedbackEnv) -> None:
    from rednote.modules.identity.repository import IdentityRepository as _IdentityRepository

    raw_token = generate_session_token()
    with session_scope(env.settings) as session:
        _IdentityRepository(session).create_session(
            user_id=env.owner_id,
            token_hash=hash_token(raw_token),
            csrf_secret_hash=hash_token(CSRF_SECRET_PLAINTEXT),
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    client.cookies.set(CSRF_COOKIE_NAME, CSRF_SECRET_PLAINTEXT)


def _write_headers(**extra: str) -> dict[str, str]:
    headers = {"X-CSRF-Token": CSRF_SECRET_PLAINTEXT, "Origin": TEST_ORIGIN}
    headers.update(extra)
    return headers


def test_preview_edit_412_conflict_and_replay_round_trip(
    client: TestClient, env: FeedbackEnv
) -> None:
    """AC-28 核心场景（开工方案 §5.2 的时序），走真实 HTTP：

    T0  GET preview       → 拿到 ETag "X:1"
    T1  POST edits（带 "X:1"）→ 201，新 ETag "X:2"
    T2  POST edits（仍带旧 "X:1"）→ 412，details.currentEtag="X:2"，
        且这次提交的新标题**没有**进库（断言下一次 GET 看不到它）
    T3  用 T2 回带的 currentEtag 重放 → 201 成功
    """
    _login(client, env)
    seeded = _seed_content(env, paragraphs=1)
    content_id = seeded.content_id

    preview = client.get(f"{API_PREFIX}/contents/{content_id}/preview")
    assert preview.status_code == 200, preview.text
    etag_1 = preview.headers["ETag"]

    resp_1 = client.post(
        f"{API_PREFIX}/contents/{content_id}/edits",
        json={"title": "第一次编辑的标题"},
        headers=_write_headers(**{"If-Match": etag_1}),
    )
    assert resp_1.status_code == 201, resp_1.text

    resp_2 = client.post(
        f"{API_PREFIX}/contents/{content_id}/edits",
        json={"title": "第二次编辑：应当撞 412"},
        headers=_write_headers(**{"If-Match": etag_1}),
    )
    assert resp_2.status_code == 412, resp_2.text
    body_2 = resp_2.json()
    current_etag = body_2["error"]["details"]["currentEtag"]
    assert current_etag != etag_1

    preview_after_conflict = client.get(f"{API_PREFIX}/contents/{content_id}/preview")
    assert preview_after_conflict.json()["data"]["title"] != "第二次编辑：应当撞 412", (
        "412 的那次提交必须零写入——第二次的标题不该出现在库里"
    )

    resp_3 = client.post(
        f"{API_PREFIX}/contents/{content_id}/edits",
        json={"title": "第二次编辑：应当撞 412"},
        headers=_write_headers(**{"If-Match": current_etag}),
    )
    assert resp_3.status_code == 201, (
        f"用 412 回带的 currentEtag 重放必须成功，否则前端会无限重试死循环: {resp_3.text}"
    )


def test_advance_feedback_requires_if_match_428(
    client: TestClient, env: FeedbackEnv
) -> None:
    """缺 `If-Match` 必须 428（不是 412）——协议 §1.2 的"没带前置条件"分支。"""
    _login(client, env)
    seeded = _seed_content(env, paragraphs=1)

    create_resp = client.post(
        f"{API_PREFIX}/contents/{seeded.content_id}/feedback",
        json={
            "observedVersionId": str(seeded.version_id),
            "anchorKind": "WHOLE",
            "category": "OTHER",
            "severity": "INFO",
            "scope": "CURRENT_ONLY",
            "body": "HTTP 层烟雾测试意见",
        },
        headers=_write_headers(),
    )
    assert create_resp.status_code == 201, create_resp.text
    feedback_id = create_resp.json()["data"]["id"]

    resp = client.patch(
        f"{API_PREFIX}/feedback/{feedback_id}",
        json={"toState": "IN_PROGRESS"},
        headers=_write_headers(),
    )
    assert resp.status_code == 428, resp.text
