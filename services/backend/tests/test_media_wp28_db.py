"""WP28 · 4.2 数据库行为测试：`media_form` 列、`media_components`、`live_profiles`、组件一致性触发器。

## 为什么单独一个文件

pytest 夹具不跨模块共享；本文件从 `test_media_wp27` **导入**建表/清表/环境夹具，
不复制一份（复制会在 wp27 夹具演进时静默分叉）。

## 每条用例断言什么

一律断言 **sqlstate + 约束名**（"被拒"≠"因对的原因被拒"）：

| 防线 | 期望 |
|---|---|
| CHECK | `23514` + 约束名 `ck_<表>_<名>` |
| 唯一索引 | `23505` + 索引名 |
| 一致性触发器（延迟） | `23514` 且 `constraint_name` 为空（触发器 RAISE，不是 CHECK） |
| 不可变触发器 | `23001` |

红线：`verification` 只有 `SYNTHETIC`、`live_profiles.state` 只有 `PLACEHOLDER`，
放宽它们必须走迁移 —— 这里钉死"写别的值会被拒"。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

# 夹具直接导入（autouse 夹具导入后在本模块同样生效）。
from test_media_wp27 import (  # noqa: F401
    MediaEnv,
    _clean_own_rows,
    _constraint_name,
    _ensure_own_tables,
    _seed_content_item,
    _seed_media_item,
    _sqlstate,
    env,
)

from rednote.modules.media.types import (
    COMPONENT_ROLE_MANIFEST,
    COMPONENT_ROLE_PAIRED_MOTION,
    COMPONENT_ROLE_PREVIEW,
    COMPONENT_ROLE_STILL,
    MEDIA_FORM_LIVE,
    MEDIA_FORM_STATIC,
    VERIFICATION_SYNTHETIC,
)
from rednote.platform.database import session_scope

SHA_A = "a" * 64
SHA_B = "b" * 64


# ===========================================================================
# 种子辅助
# ===========================================================================


def _new_file(session: Any, env: MediaEnv) -> uuid.UUID:
    """造 `blobs` + `file_objects`，返回 file_object id。"""
    blob_id = session.execute(
        text(
            "INSERT INTO blobs (storage_backend, object_key, sha256, byte_size,"
            " mime_type) VALUES ('LOCAL', :key, :sha, 1, 'image/png')"
            " RETURNING id"
        ),
        {"key": f"wp28/{uuid.uuid4().hex}", "sha": uuid.uuid4().hex * 2},
    ).scalar_one()
    return session.execute(
        text(
            "INSERT INTO file_objects (blob_id, original_name, visibility,"
            " project_id, classification, uploaded_by)"
            " VALUES (:blob, 'wp28.png', 'PROJECT', :project, 'SUPPORTED', :owner)"
            " RETURNING id"
        ),
        {"blob": blob_id, "project": env.project_id, "owner": env.owner_id},
    ).scalar_one()


def _seed_version(
    env: MediaEnv, *, media_form: str | None = MEDIA_FORM_STATIC
) -> tuple[uuid.UUID, uuid.UUID]:
    """造一版合法 `media_versions`（含素材、节点）。返回 `(version_id, image_file_id)`。

    `media_form=None` ⇒ 不写该列，用来验证 server_default。
    """
    content_id = _seed_content_item(env)
    item_id = _seed_media_item(env, content_id)
    version_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        image_file = _new_file(session, env)
        asset_file = _new_file(session, env)
        asset_id = session.execute(
            text(
                "INSERT INTO reference_assets (id, project_id, file_object_id,"
                " state, product_model) VALUES (gen_random_uuid(), :p, :f,"
                " 'ACTIVE', 'M-1') RETURNING id"
            ),
            {"p": str(env.project_id), "f": str(asset_file)},
        ).scalar_one()
        session.execute(
            text(
                "INSERT INTO version_nodes (id, entity_type, entity_id,"
                " revision_no, payload_hash, schema_version)"
                " VALUES (:id, 'MEDIA', :item, 1, :h, 1)"
            ),
            {"id": str(version_id), "item": str(item_id), "h": SHA_A},
        )
        cols = (
            "id, media_item_id, version_no, paragraph_ref, sort_order,"
            " reference_bindings, image_file_id, prompt, payload, payload_hash"
        )
        vals = (
            ":id, :item, 1, 'p0-00000000', 0, CAST(:rb AS jsonb), :img,"
            " '提示词', '{}'::jsonb, :h"
        )
        params = {
            "id": str(version_id),
            "item": str(item_id),
            "rb": f'[{{"assetId": "{asset_id}"}}]',
            "img": str(image_file),
            "h": SHA_A,
        }
        if media_form is not None:
            cols += ", media_form"
            vals += ", :form"
            params["form"] = media_form
        session.execute(
            text(f"INSERT INTO media_versions ({cols}) VALUES ({vals})"), params
        )
    return version_id, image_file


def _insert_component(
    session: Any,
    version_id: uuid.UUID,
    *,
    role: str,
    file_id: uuid.UUID,
    verification: str = VERIFICATION_SYNTHETIC,
    sha256: str = SHA_A,
    byte_size: int = 10,
) -> None:
    session.execute(
        text(
            "INSERT INTO media_components (id, media_version_id, role, file_id,"
            " verification, sha256, byte_size)"
            " VALUES (gen_random_uuid(), :v, :role, :f, :ver, :sha, :size)"
        ),
        {
            "v": str(version_id),
            "role": role,
            "f": str(file_id),
            "ver": verification,
            "sha": sha256,
            "size": byte_size,
        },
    )


def _component_error(env: MediaEnv, version_id: uuid.UUID, **kwargs: Any) -> DBAPIError:
    """插入一个组件并期望被拒，返回异常（插入成功则让用例失败）。"""
    with pytest.raises(DBAPIError) as excinfo:
        with session_scope(env.settings) as session:
            _insert_component(session, version_id, **kwargs)
    return excinfo.value


def _count(env: MediaEnv, sql: str, **params: Any) -> int:
    with session_scope(env.settings) as session:
        return int(session.execute(text(sql), params).scalar_one())


# ===========================================================================
# media_form 列
# ===========================================================================


def test_media_form_defaults_to_static(env: MediaEnv) -> None:
    """不写 `media_form` ⇒ 由 server_default 回填 `STATIC`（存量行回填的同一机制）。"""
    version_id, _ = _seed_version(env, media_form=None)
    form = _count(
        env,
        "SELECT count(*) FROM media_versions WHERE id = :v AND media_form = 'STATIC'",
        v=str(version_id),
    )
    assert form == 1


def test_media_form_accepts_live(env: MediaEnv) -> None:
    version_id, _ = _seed_version(env, media_form=MEDIA_FORM_LIVE)
    assert (
        _count(
            env,
            "SELECT count(*) FROM media_versions WHERE id = :v AND media_form = 'LIVE'",
            v=str(version_id),
        )
        == 1
    )


def test_media_form_rejects_unknown_value(env: MediaEnv) -> None:
    with pytest.raises(DBAPIError) as excinfo:
        _seed_version(env, media_form="VIDEO")
    assert _sqlstate(excinfo.value) == "23514"
    assert _constraint_name(excinfo.value) == "ck_media_versions_media_form_valid"


# ===========================================================================
# media_components：CHECK / 唯一
# ===========================================================================


def test_component_roundtrip_on_live_version(env: MediaEnv) -> None:
    """合法路径：LIVE 版本上四个角色各一个，STILL 复用版本的 image_file_id。"""
    version_id, image_file = _seed_version(env, media_form=MEDIA_FORM_LIVE)
    with session_scope(env.settings) as session:
        _insert_component(
            session, version_id, role=COMPONENT_ROLE_STILL, file_id=image_file
        )
        for role in (
            COMPONENT_ROLE_PAIRED_MOTION,
            COMPONENT_ROLE_MANIFEST,
            COMPONENT_ROLE_PREVIEW,
        ):
            _insert_component(
                session, version_id, role=role, file_id=_new_file(session, env)
            )
    assert (
        _count(
            env,
            "SELECT count(*) FROM media_components WHERE media_version_id = :v",
            v=str(version_id),
        )
        == 4
    )


@pytest.mark.parametrize(
    ("kwargs", "constraint"),
    [
        ({"role": "ORIGINAL"}, "ck_media_components_role_valid"),
        ({"verification": "VERIFIED"}, "ck_media_components_verification_valid"),
        ({"sha256": "A" * 64}, "ck_media_components_sha256_shape"),
        ({"sha256": "a" * 63}, "ck_media_components_sha256_shape"),
        ({"byte_size": 0}, "ck_media_components_byte_size_positive"),
    ],
)
def test_component_check_constraints(
    env: MediaEnv, kwargs: dict[str, Any], constraint: str
) -> None:
    version_id, _ = _seed_version(env, media_form=MEDIA_FORM_LIVE)
    with session_scope(env.settings) as session:
        other_file = _new_file(session, env)
    payload: dict[str, Any] = {"role": COMPONENT_ROLE_MANIFEST, "file_id": other_file}
    payload.update(kwargs)
    exc = _component_error(env, version_id, **payload)
    assert _sqlstate(exc) == "23514"
    assert _constraint_name(exc) == constraint


def test_component_unique_version_role(env: MediaEnv) -> None:
    """同一版本同一角色至多一个。"""
    version_id, _ = _seed_version(env, media_form=MEDIA_FORM_LIVE)
    with session_scope(env.settings) as session:
        _insert_component(
            session,
            version_id,
            role=COMPONENT_ROLE_MANIFEST,
            file_id=_new_file(session, env),
        )
        second_file = _new_file(session, env)
    exc = _component_error(
        env, version_id, role=COMPONENT_ROLE_MANIFEST, file_id=second_file
    )
    assert _sqlstate(exc) == "23505"
    assert _constraint_name(exc) == "uq_media_components_version_role"


# ===========================================================================
# 一致性触发器（S2）
# ===========================================================================


def test_component_on_static_version_is_rejected(env: MediaEnv) -> None:
    """STATIC 版本挂组件 ⇒ 被拒（形态只有一个答案）。"""
    version_id, _ = _seed_version(env, media_form=MEDIA_FORM_STATIC)
    with session_scope(env.settings) as session:
        other_file = _new_file(session, env)
    exc = _component_error(
        env, version_id, role=COMPONENT_ROLE_MANIFEST, file_id=other_file
    )
    assert _sqlstate(exc) == "23514"
    assert _constraint_name(exc) is None  # 触发器 RAISE，不是 CHECK


def test_still_must_equal_image_file(env: MediaEnv) -> None:
    """STILL 的 file_id 不等于 image_file_id ⇒ 被拒。"""
    version_id, _ = _seed_version(env, media_form=MEDIA_FORM_LIVE)
    with session_scope(env.settings) as session:
        other_file = _new_file(session, env)
    exc = _component_error(
        env, version_id, role=COMPONENT_ROLE_STILL, file_id=other_file
    )
    assert _sqlstate(exc) == "23514"
    assert _constraint_name(exc) is None


def test_non_still_role_not_bound_to_image_file(env: MediaEnv) -> None:
    """非 STILL 角色的文件可以是任意文件（约束只针对 STILL）。"""
    version_id, _ = _seed_version(env, media_form=MEDIA_FORM_LIVE)
    with session_scope(env.settings) as session:
        _insert_component(
            session,
            version_id,
            role=COMPONENT_ROLE_PAIRED_MOTION,
            file_id=_new_file(session, env),
        )
    assert (
        _count(
            env,
            "SELECT count(*) FROM media_components WHERE media_version_id = :v",
            v=str(version_id),
        )
        == 1
    )


# ===========================================================================
# live_profiles
# ===========================================================================


def _insert_profile(
    env: MediaEnv,
    *,
    key: str,
    version: int = 1,
    state: str = "PLACEHOLDER",
    notes: str | None = None,
) -> None:
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO live_profiles (id, profile_key, version, state, notes)"
                " VALUES (gen_random_uuid(), :k, :v, :s, :n)"
            ),
            {"k": key, "v": version, "s": state, "n": notes},
        )


def test_live_profile_accepts_placeholder(env: MediaEnv) -> None:
    key = f"k-{uuid.uuid4().hex[:8]}"
    _insert_profile(env, key=key)
    assert (
        _count(env, "SELECT count(*) FROM live_profiles WHERE profile_key = :k", k=key)
        == 1
    )


@pytest.mark.parametrize(
    ("kwargs", "constraint"),
    [
        ({"state": "VERIFIED"}, "ck_live_profiles_state_valid"),
        ({"version": 0}, "ck_live_profiles_version_positive"),
        ({"key": ""}, "ck_live_profiles_profile_key_not_empty"),
    ],
)
def test_live_profile_check_constraints(
    env: MediaEnv, kwargs: dict[str, Any], constraint: str
) -> None:
    payload: dict[str, Any] = {"key": f"k-{uuid.uuid4().hex[:8]}"}
    payload.update(kwargs)
    with pytest.raises(DBAPIError) as excinfo:
        _insert_profile(env, **payload)
    assert _sqlstate(excinfo.value) == "23514"
    assert _constraint_name(excinfo.value) == constraint


@pytest.mark.parametrize(
    "kwargs", [{"key": "x" * 101}, {"notes": "n" * 501}], ids=["key_101", "notes_501"]
)
def test_live_profile_overlong_text_is_rejected_by_column_width(
    env: MediaEnv, kwargs: dict[str, Any]
) -> None:
    """超长文本被**列宽** `varchar(n)` 拒（22001），而不是长度 CHECK（23514）。

    实测：`profile_key_length` / `notes_length` 两条 CHECK 与列宽同界，永远排在列宽之后，
    **不可达**；保留它们是与本包其它表（如 `regeneration_reason_length`）一致的纵深防御。
    本用例钉的是**真正起作用的那道防线**，不假装 CHECK 在拦。
    """
    payload: dict[str, Any] = {"key": f"k-{uuid.uuid4().hex[:8]}"}
    payload.update(kwargs)
    with pytest.raises(DBAPIError) as excinfo:
        _insert_profile(env, **payload)
    assert _sqlstate(excinfo.value) == "22001"


def test_live_profile_unique_key_version(env: MediaEnv) -> None:
    key = f"k-{uuid.uuid4().hex[:8]}"
    _insert_profile(env, key=key, version=1)
    _insert_profile(env, key=key, version=2)  # 追加新版本是合法的
    with pytest.raises(DBAPIError) as excinfo:
        _insert_profile(env, key=key, version=1)
    assert _sqlstate(excinfo.value) == "23505"
    assert _constraint_name(excinfo.value) == "uq_live_profiles_key_version"


# ===========================================================================
# 不可变
# ===========================================================================


def test_components_are_immutable(env: MediaEnv) -> None:
    version_id, _ = _seed_version(env, media_form=MEDIA_FORM_LIVE)
    with session_scope(env.settings) as session:
        _insert_component(
            session,
            version_id,
            role=COMPONENT_ROLE_MANIFEST,
            file_id=_new_file(session, env),
        )
    for sql in (
        "UPDATE media_components SET byte_size = 99 WHERE media_version_id = :v",
        "DELETE FROM media_components WHERE media_version_id = :v",
    ):
        with pytest.raises(DBAPIError) as excinfo:
            with session_scope(env.settings) as session:
                session.execute(text(sql), {"v": str(version_id)})
        assert _sqlstate(excinfo.value) == "23001", sql


def test_live_profiles_are_immutable(env: MediaEnv) -> None:
    key = f"k-{uuid.uuid4().hex[:8]}"
    _insert_profile(env, key=key)
    for sql in (
        "UPDATE live_profiles SET notes = 'x' WHERE profile_key = :k",
        "DELETE FROM live_profiles WHERE profile_key = :k",
    ):
        with pytest.raises(DBAPIError) as excinfo:
            with session_scope(env.settings) as session:
                session.execute(text(sql), {"k": key})
        assert _sqlstate(excinfo.value) == "23001", sql
