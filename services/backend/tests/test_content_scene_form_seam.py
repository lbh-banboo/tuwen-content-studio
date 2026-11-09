"""content 生成链路携带场景 / 形式的接缝（M3 接缝冻结 §5.15）。

覆盖：默认 NULL 向后兼容、带入落库、非法值拒绝、幂等重放（含不一致）、响应回显。
夹具沿用 test_content_wp26（同库同表，不改它）。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from test_content_wp26 import (
    ContentEnv,
    _clean_own_rows,  # noqa: F401
    _ensure_own_tables,  # noqa: F401
    _generations_url,
    _login,
    _raw,
    _repo,
    _scalar,
    _write_headers,
    app,  # noqa: F401
    client,  # noqa: F401
    env,  # noqa: F401
)

from rednote.modules.content import service as c_service
from rednote.modules.content.types import (
    GENERATION_REQUEST_KEYS,
    MAX_SCENE_FORM_KEY_CHARS,
    GenerationRequest,
)


def _body(env: ContentEnv, **extra: Any) -> dict[str, Any]:
    return {
        "contentCount": 2,
        "scope": "COPY",
        "batchKind": "TRIAL",
        "modelProfileId": str(env.model_profile_id),
        **extra,
    }


def _req(**kw: Any) -> GenerationRequest:
    return GenerationRequest(
        scope="COPY", batch_kind="TRIAL", model_profile_id=uuid.uuid4(), **kw
    )


# --- 类型层 ---------------------------------------------------------------


def test_default_is_none_and_payload_unchanged() -> None:
    r = _req()
    assert r.scene_key is None and r.form_key is None
    assert set(r.as_payload()) == GENERATION_REQUEST_KEYS
    assert GenerationRequest.from_payload(r.as_payload()) == r


def test_keys_are_stripped_and_kept_out_of_payload() -> None:
    r = _req(scene_key="  早八通勤 ", form_key=" 静态 ")
    assert (r.scene_key, r.form_key) == ("早八通勤", "静态")
    assert set(r.as_payload()) == GENERATION_REQUEST_KEYS


@pytest.mark.parametrize("bad", ["", "   ", "\t\n", "x" * (MAX_SCENE_FORM_KEY_CHARS + 1), 5])
@pytest.mark.parametrize("field", ["scene_key", "form_key"])
def test_invalid_values_rejected(field: str, bad: Any) -> None:
    with pytest.raises(ValueError, match=field):
        _req(**{field: bad})


def test_max_length_accepted() -> None:
    assert _req(scene_key="x" * MAX_SCENE_FORM_KEY_CHARS).scene_key is not None


# --- 服务 / 库层 -----------------------------------------------------------


def test_start_generation_default_null(env: ContentEnv) -> None:
    with _repo(env) as repo:
        rows, created = c_service.start_generation(
            repo, project_id=env.project_id, request=_req(),
            content_count=2, idempotency_key="sf-default",
        )
    assert created
    assert all(r.scene_key is None and r.form_key is None for r in rows)
    assert _scalar(
        env,
        "SELECT count(*) FROM content_items WHERE scene_key IS NOT NULL OR form_key IS NOT NULL",
    ) == 0


def test_start_generation_persists_and_replay_returns_same_batch(env: ContentEnv) -> None:
    kw: dict[str, Any] = dict(project_id=env.project_id, content_count=2, idempotency_key="sf-1")
    with _repo(env) as repo:
        rows, created = c_service.start_generation(
            repo, request=_req(scene_key="通勤", form_key="静态"), **kw
        )
        assert created
        # 重放：命中即原样返回，不覆盖列（即使本次带的值不同，冲突由端点判）
        again, created2 = c_service.start_generation(
            repo, request=_req(scene_key="别的", form_key=None), **kw
        )
    assert not created2
    assert {r.id for r in again} == {r.id for r in rows}
    assert all((r.scene_key, r.form_key) == ("通勤", "静态") for r in again)
    assert _scalar(env, "SELECT count(*) FROM content_items WHERE scene_key = '通勤'") == 2


def test_db_check_rejects_blank(env: ContentEnv) -> None:
    with pytest.raises(DBAPIError):
        _raw(
            env,
            "INSERT INTO content_items (id, project_id, scene_key) VALUES (gen_random_uuid(), :p, '  ')",
            {"p": env.project_id},
        )


# --- HTTP 层 ---------------------------------------------------------------


def test_http_default_and_echo(client: TestClient, env: ContentEnv) -> None:
    _login(client, env, who="owner")
    old = client.post(_generations_url(env), json=_body(env), headers=_write_headers())
    assert old.status_code == 202, old.text
    assert all(c["sceneKey"] is None and c["formKey"] is None for c in old.json()["data"]["contents"])

    new = client.post(
        _generations_url(env),
        json=_body(env, sceneKey=" 通勤 ", formKey="Live"),
        headers=_write_headers(),
    )
    assert new.status_code == 202, new.text
    assert all(
        (c["sceneKey"], c["formKey"]) == ("通勤", "Live") for c in new.json()["data"]["contents"]
    )
    listing = client.get(f"{_generations_url(env).rsplit('/', 1)[0]}/contents")
    assert listing.status_code == 200, listing.text
    seen = {(i["sceneKey"], i["formKey"]) for i in listing.json()["data"]}
    assert seen == {(None, None), ("通勤", "Live")}


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"sceneKey": "  "}, "sceneKey"),
        ({"formKey": ""}, "formKey"),
        ({"sceneKey": "x" * (MAX_SCENE_FORM_KEY_CHARS + 1)}, "sceneKey"),
    ],
)
def test_http_invalid_rejected_422(client: TestClient, env: ContentEnv, extra: dict, field: str) -> None:
    _login(client, env, who="owner")
    r = client.post(_generations_url(env), json=_body(env, **extra), headers=_write_headers())
    assert r.status_code == 422, r.text
    assert field in r.text
    assert _scalar(env, "SELECT count(*) FROM content_items") == 0


def test_http_replay_same_body_ok_and_mismatch_conflicts(client: TestClient, env: ContentEnv) -> None:
    _login(client, env, who="owner")
    h = _write_headers(**{"Idempotency-Key": "sf-http"})
    first = client.post(_generations_url(env), json=_body(env, sceneKey="通勤"), headers=h)
    assert first.status_code == 202, first.text

    same = client.post(_generations_url(env), json=_body(env, sceneKey="通勤"), headers=h)
    assert same.status_code == 202, same.text
    assert all(c["created"] is False for c in same.json()["data"]["contents"])

    for extra in ({"sceneKey": "别的"}, {}, {"sceneKey": "通勤", "formKey": "静态"}):
        r = client.post(_generations_url(env), json=_body(env, **extra), headers=h)
        assert r.status_code == 409, (extra, r.text)
        assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert _scalar(env, "SELECT count(*) FROM content_items") == 2


def test_http_replay_old_style_unchanged_and_new_vs_old_conflicts(client: TestClient, env: ContentEnv) -> None:
    _login(client, env, who="owner")
    h = _write_headers(**{"Idempotency-Key": "sf-old"})
    assert client.post(_generations_url(env), json=_body(env), headers=h).status_code == 202
    assert client.post(_generations_url(env), json=_body(env), headers=h).status_code == 202
    r = client.post(_generations_url(env), json=_body(env, formKey="静态"), headers=h)
    assert r.status_code == 409, r.text
