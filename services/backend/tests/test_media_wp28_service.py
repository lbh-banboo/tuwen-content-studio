"""WP28 · 4.5 服务层端到端测试：Live 分支接入 `start_media_generation` / `execute_media_generation`。

## 这个文件证什么

| 面 | 断言 |
|---|---|
| 载荷 | `mediaForm` 恒写出；旧行缺键 ⇒ STATIC；未知形态被拒 |
| 发起 | Live 物化恰好 1 行；同篇内容形态不一致 ⇒ `MEDIA_FORM_CONFLICT` |
| 执行 | Live 端到端：版本 + 4 个组件落库，STILL 的 `file_id == image_file_id` |
| 回滚 | 封装失败（体积超限）⇒ 无版本、无组件、预占被释放、媒体 FAILED |
| 计费 | 估价 `form=LIVE` 且 `content_count=1`（quantity 由 `calls_for` 折出 2）；预占/结算 note 含 OP-206 |
| 装配 | Live 未注入 `motion_generator` ⇒ 预占之前拒（估价从未被调用） |
| 零回归 | 静态路径 `form=STATIC`、note 为 None、无组件 |

## 真库 vs 桩

计费三件套与提示词/provider 解析桩掉（它们是 WP23/24/25 的活）；
**其余全部真跑**：`MediaRepository` / `FileRepository` / `VersionRepository` / `LocalStorageBackend`。
`media_generation_contexts` 有不延迟的真 FK（prompt/model_profile/provider_call），
所以这些行必须在库里真造，不能给随机 uuid。
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import text

# 夹具直接导入（autouse 夹具导入后在本模块同样生效）。
from test_media_wp27 import (  # noqa: F401
    MediaEnv,
    _clean_own_rows,
    _ensure_own_tables,
    _make_settings,
    _seed_content_item,
    env,
)

from rednote.modules.billing.types import CostEstimateView, ReservationView
from rednote.modules.files.repository import FileRepository
from rednote.modules.files.storage import LocalStorageBackend
from rednote.modules.media import service as m_service
from rednote.modules.media.billing_bridge import OP206_NOTE
from rednote.modules.media.gateway_bridge import ImagePayload, MediaGenerationOutput
from rednote.modules.media.live_pack import ERR_GROUP_TOO_LARGE, LiveMotionStub, MotionOutput
from rednote.modules.media.prompts_bridge import ActivePromptVersion
from rednote.modules.media.repository import MediaRepository
from rednote.modules.media.schemas import SchemaViolation
from rednote.modules.media.types import (
    COMPONENT_ROLE_MANIFEST,
    COMPONENT_ROLE_PAIRED_MOTION,
    COMPONENT_ROLE_PREVIEW,
    COMPONENT_ROLE_STILL,
    MEDIA_FORM_LIVE,
    MEDIA_FORM_STATIC,
    MEDIA_STATE_FAILED,
    MEDIA_STATE_READY,
    REASON_GENERATOR_NOT_CONFIGURED,
    REASON_MEDIA_FORM_CONFLICT,
    TASK_STATIC_IMAGE,
    VERIFICATION_SYNTHETIC,
    MediaGenerationRequest,
    calls_for,
)
from rednote.modules.versions.repository import VersionRepository
from rednote.platform.configuration import Settings
from rednote.platform.database import session_scope

BODY = "第一段：产品外观。\n\n第二段：使用场景。\n\n第三段：细节特写。"


# ===========================================================================
# 小工具
# ===========================================================================


def _png(width: int = 96, height: int = 96) -> bytes:
    """真 PNG（`measure_image` 要求 ≥64×64 且 Pillow 能打开）。每次略有差异。"""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (uuid.uuid4().int % 256, 120, 200)).save(
        buf, format="PNG"
    )
    return buf.getvalue()


class _FakeImageGenerator:
    """只出网、不落库的静帧生成器（`MediaGenerator` 协议的内存实现）。"""

    def __init__(self, *, provider_call_id: uuid.UUID) -> None:
        self.provider_call_id = provider_call_id
        self.calls = 0

    def dispatch(self, **_kwargs: Any) -> MediaGenerationOutput:
        self.calls += 1
        content = _png()
        return MediaGenerationOutput(
            call_id=self.provider_call_id,
            state="SUCCEEDED",
            external_request_id=None,
            structured_output={},
            image=ImagePayload(
                content=content,
                width=96,
                height=96,
                byte_size=len(content),
                image_format="PNG",
            ),
            dispatched=True,
        )


class _FakeMotionGenerator:
    """动态段字节由真实 ffmpeg 合成，调用次数单独记录。"""

    def __init__(self) -> None:
        self.calls = 0
        self.delegate = LiveMotionStub()

    def dispatch_motion(self, *, still_content: bytes, duration_ms: int) -> MotionOutput:
        self.calls += 1
        return self.delegate.dispatch_motion(
            still_content=still_content, duration_ms=duration_ms
        )


@dataclass
class _Ledger:
    """计费三件套的调用记录。"""

    estimates: list[dict[str, Any]]
    reserves: list[dict[str, Any]]
    settles: list[dict[str, Any]]
    releases: list[dict[str, Any]]


@dataclass(frozen=True)
class _Rig:
    """一次端到端运行所需的全部真实依赖。"""

    env: MediaEnv
    settings: Settings
    storage: LocalStorageBackend
    storage_root: Path
    model_profile_id: uuid.UUID
    template_id: uuid.UUID
    prompt_version_id: uuid.UUID
    provider_call_id: uuid.UUID
    ledger: _Ledger


# ===========================================================================
# 库内造数（FK 链：provider → model_profile → prompt → job → attempt → call）
# ===========================================================================


def _seed_provider_chain(env: MediaEnv) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """返回 `(provider_id, model_profile_id, template_id, prompt_version_id, provider_call_id)`。

    prompt 模板与版本、版本节点必须同一事务（延迟约束）。
    """
    provider_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    template_id = uuid.uuid4()
    prompt_version_id = uuid.uuid4()
    job_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    call_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO providers (id, name, capability_types, network_mode,"
                " endpoint, status, policy_version)"
                " VALUES (:id, :n, '[\"STATIC_IMAGE\"]'::jsonb, 'LOCAL',"
                " 'http://localhost:0', 'ACTIVE', 'v1')"
            ),
            {"id": provider_id, "n": f"wp28-svc-{provider_id.hex[:8]}"},
        )
        session.execute(
            text(
                "INSERT INTO model_profiles (id, provider_id, model_name, capabilities)"
                " VALUES (:id, :p, 'wp28-model', '[\"STATIC_IMAGE\"]'::jsonb)"
            ),
            {"id": profile_id, "p": provider_id},
        )
        session.execute(
            text(
                "INSERT INTO prompt_templates (id, task_kind, name)"
                " VALUES (:id, 'STATIC_IMAGE', :n)"
            ),
            {"id": template_id, "n": f"wp28-{template_id.hex[:8]}"},
        )
        session.execute(
            text(
                "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
                " payload_hash, schema_version)"
                " VALUES (:id, 'PROMPT', :tid, 1, :h, 1)"
            ),
            {"id": prompt_version_id, "tid": template_id, "h": f"wp28-{prompt_version_id.hex[:8]}"},
        )
        session.execute(
            text(
                "INSERT INTO prompt_versions (id, template_id, system_template,"
                " input_schema, output_schema, allowed_tools, change_reason)"
                " VALUES (:vid, :tid, 'sys', '{}'::jsonb, '{}'::jsonb,"
                " '{}'::text[], 'wp28')"
            ),
            {"vid": prompt_version_id, "tid": template_id},
        )
        session.execute(
            text(
                "INSERT INTO jobs (id, kind, queue, state, priority,"
                " idempotency_scope, idempotency_key, input_hash, project_id)"
                " VALUES (:id, 'MEDIA_GENERATE', 'media', 'RUNNING', 0, 'wp28-svc',"
                " :k, :h, :p)"
            ),
            {"id": job_id, "k": f"k-{job_id.hex[:10]}", "h": f"h-{job_id.hex[:10]}", "p": env.project_id},
        )
        session.execute(
            text(
                "INSERT INTO job_attempts (id, job_id, attempt_no, state, worker_id, fence_token)"
                " VALUES (:id, :j, 1, 'RUNNING', 'wp28-worker', 0)"
            ),
            {"id": attempt_id, "j": job_id},
        )
        session.execute(
            text(
                "INSERT INTO provider_calls (id, job_attempt_id, provider_id, request_key,"
                " state, request_evidence_ref)"
                " VALUES (:id, :a, :p, :rk, 'SUCCEEDED', 'wp28-evidence')"
            ),
            {"id": call_id, "a": attempt_id, "p": provider_id, "rk": f"rk-{call_id.hex[:10]}"},
        )
    return provider_id, profile_id, template_id, prompt_version_id, call_id


def _seed_content_with_body(env: MediaEnv, body: str = BODY) -> uuid.UUID:
    """造内容身份 + 一版带正文的 `content_versions`（含 CONTENT 节点），并指向它。"""
    content_id = _seed_content_item(env)
    version_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
                " payload_hash, schema_version)"
                " VALUES (:id, 'CONTENT', :c, 1, :h, 1)"
            ),
            {"id": version_id, "c": content_id, "h": f"h-{version_id.hex[:12]}"},
        )
        session.execute(
            text(
                "INSERT INTO content_versions (id, content_item_id, version_no, title,"
                " body, tags, bindings, payload, payload_hash, schema_version)"
                " VALUES (:id, :c, 1, '标题', :body, '[]'::jsonb, '[]'::jsonb,"
                " '{}'::jsonb, :h, 1)"
            ),
            {"id": version_id, "c": content_id, "body": body, "h": f"ph-{version_id.hex[:12]}"},
        )
        session.execute(
            text("UPDATE content_items SET current_version_id = :v WHERE id = :c"),
            {"v": version_id, "c": content_id},
        )
    return content_id


def _seed_reference_asset(env: MediaEnv) -> uuid.UUID:
    """造一条可用的参考素材（含 blobs / file_objects）。"""
    with session_scope(env.settings) as session:
        blob_id = session.execute(
            text(
                "INSERT INTO blobs (storage_backend, object_key, sha256, byte_size, mime_type)"
                " VALUES ('LOCAL', :k, :sha, 1, 'image/png') RETURNING id"
            ),
            {"k": f"wp28svc/{uuid.uuid4().hex}", "sha": uuid.uuid4().hex * 2},
        ).scalar_one()
        file_id = session.execute(
            text(
                "INSERT INTO file_objects (blob_id, original_name, visibility, project_id,"
                " classification, uploaded_by)"
                " VALUES (:b, 'ref.png', 'PROJECT', :p, 'SUPPORTED', :o) RETURNING id"
            ),
            {"b": blob_id, "p": env.project_id, "o": env.owner_id},
        ).scalar_one()
        return session.execute(
            text(
                "INSERT INTO reference_assets (id, project_id, file_object_id, state,"
                " product_model) VALUES (gen_random_uuid(), :p, :f, 'ACTIVE', 'M-1')"
                " RETURNING id"
            ),
            {"p": env.project_id, "f": file_id},
        ).scalar_one()


# ===========================================================================
# 夹具：Rig（真依赖 + 桩掉的计费/提示词/provider 解析）
# ===========================================================================


@pytest.fixture
def rig(
    env: MediaEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> Iterator[_Rig]:
    settings = _make_settings()
    _provider_id, profile_id, template_id, version_id, call_id = _seed_provider_chain(env)
    ledger = _Ledger(estimates=[], reserves=[], settles=[], releases=[])

    def _fake_resolve(_repo: Any, *, task_kind: str) -> ActivePromptVersion:
        return ActivePromptVersion(
            template_id=template_id,
            version_id=version_id,
            task_kind=task_kind,
            system_template="画：{{paragraph}}",
            input_schema={},
            output_schema={},
        )

    def _fake_provider(_repo: Any, *, model_profile_id: uuid.UUID) -> uuid.UUID:
        return _provider_id

    def _fake_estimate(
        _repo: Any, *, project_id: uuid.UUID, batch_kind: str, content_count: int,
        task_kind: str, form: str = MEDIA_FORM_STATIC, **_kw: Any,
    ) -> CostEstimateView:
        ledger.estimates.append(
            {"content_count": content_count, "form": form, "task_kind": task_kind}
        )
        return CostEstimateView(
            id=uuid.uuid4(), project_id=project_id, batch_kind=batch_kind,
            quantity=calls_for(task_kind, content_count, form=form),
            currency="CNY", state="ESTIMATED", total_amount=Decimal("1"),
            actual_amount=None, variance_amount=None, pricing_version_id=None, lines=(),
        )

    def _fake_reserve(
        _repo: Any, *, project_id: uuid.UUID, note: str | None = None, **_kw: Any,
    ) -> ReservationView:
        res = ReservationView(
            id=uuid.uuid4(), project_id=project_id, state="RESERVED", currency="CNY",
            amount=Decimal("1"), settled_amount=None, ref=None, note=note,
        )
        ledger.reserves.append({"id": res.id, "note": note})
        return res

    def _fake_settle(_repo: Any, *, note: str | None = None, **_kw: Any) -> Any:
        ledger.settles.append({"note": note})
        return type("Settled", (), {"state": "SETTLED"})()

    def _fake_release(_repo: Any, *, reservation_id: uuid.UUID, **_kw: Any) -> Any:
        ledger.releases.append({"id": reservation_id})
        return None

    monkeypatch.setattr(m_service, "resolve_active_version", _fake_resolve)
    monkeypatch.setattr(m_service, "resolve_provider_id", _fake_provider)
    monkeypatch.setattr(m_service, "estimate_generation_cost", _fake_estimate)
    monkeypatch.setattr(m_service, "reserve_generation_budget", _fake_reserve)
    monkeypatch.setattr(m_service, "settle_generation_budget", _fake_settle)
    monkeypatch.setattr(m_service, "release_generation_budget", _fake_release)

    storage_root = tmp_path / "storage"
    yield _Rig(
        env=env,
        settings=settings,
        storage=LocalStorageBackend(storage_root),
        storage_root=storage_root,
        model_profile_id=profile_id,
        template_id=template_id,
        prompt_version_id=version_id, provider_call_id=call_id, ledger=ledger,
    )


# ===========================================================================
# 运行辅助
# ===========================================================================


def _request(rig: _Rig, *, form: str = MEDIA_FORM_STATIC) -> MediaGenerationRequest:
    return MediaGenerationRequest(
        task_kind=TASK_STATIC_IMAGE,
        batch_kind="TRIAL",
        model_profile_id=rig.model_profile_id,
        media_form=form,
    )


def _start(
    rig: _Rig, content_id: uuid.UUID, *, form: str, key: str | None = None
) -> tuple[tuple[Any, ...], bool]:
    """发起一次生成（物化身份行），返回 `(rows, created)`。"""
    asset_id = _seed_reference_asset(rig.env)
    binding = [{"assetId": str(asset_id), "part": None, "note": None}]
    orders = 1 if form == MEDIA_FORM_LIVE else 3
    with session_scope(rig.settings) as session:
        rows, created = m_service.start_media_generation(
            MediaRepository(session),
            project_id=rig.env.project_id,
            content_id=content_id,
            request=_request(rig, form=form),
            bindings_by_sort_order={i: binding for i in range(orders)},
            idempotency_key=key or f"idem-{uuid.uuid4().hex[:10]}",
            created_by=rig.env.owner_id,
        )
        # 行对象绑在 session 上，出作用域前取成纯值。
        snap = tuple(
            type("Row", (), {"id": r.id, "sort_order": r.sort_order,
                             "generation_request": dict(r.generation_request)})()
            for r in rows
        )
    return snap, created


def _run(
    rig: _Rig,
    content_id: uuid.UUID,
    media_item_id: uuid.UUID,
    *,
    motion: Any = None,
    live_group_max_bytes: int | None = None,
) -> Any:
    """跑一次执行（整个事务；异常原样上抛，事务由 session_scope 回滚）。"""
    kwargs: dict[str, Any] = {}
    if live_group_max_bytes is not None:
        kwargs["live_group_max_bytes"] = live_group_max_bytes
    try:
        with session_scope(rig.settings) as session:
            return m_service.execute_media_generation(
                MediaRepository(session),
                content_id=content_id,
                media_item_id=media_item_id,
                generator=_FakeImageGenerator(provider_call_id=rig.provider_call_id),
                prompt_repo=object(),
                billing_repo=object(),
                gateway_repo=object(),
                file_repo=FileRepository(session),
                storage=rig.storage,
                version_repo=VersionRepository(session),
                actor_id=rig.env.owner_id,
                request_id="req-wp28",
                motion_generator=motion,
                **kwargs,
            )
    except Exception:
        # 与任务处理器相同的事务边界：失败状态与补偿必须在业务事务之外提交。
        with session_scope(rig.settings) as session:
            MediaRepository(session).set_state(
                media_item_id=media_item_id, state=MEDIA_STATE_FAILED
            )
        raise


def _scalar(rig: _Rig, sql: str, **params: Any) -> Any:
    with session_scope(rig.settings) as session:
        return session.execute(text(sql), params).scalar_one()


# ===========================================================================
# 载荷（纯函数）
# ===========================================================================


def test_payload_always_writes_media_form_key(rig: _Rig) -> None:
    """`as_payload` 恒写四键，含 STATIC（写侧不省键）。"""
    static = _request(rig, form=MEDIA_FORM_STATIC).as_payload()
    live = _request(rig, form=MEDIA_FORM_LIVE).as_payload()
    assert static["mediaForm"] == MEDIA_FORM_STATIC
    assert live["mediaForm"] == MEDIA_FORM_LIVE
    assert len(static) == 4 and len(live) == 4


def test_payload_missing_media_form_defaults_to_static(rig: _Rig) -> None:
    """旧行（WP28 之前写的）缺 mediaForm ⇒ STATIC；缺必需三键仍拒。"""
    raw = _request(rig).as_payload()
    del raw["mediaForm"]
    assert MediaGenerationRequest.from_payload(raw).media_form == MEDIA_FORM_STATIC
    del raw["batchKind"]
    with pytest.raises(ValueError, match="缺键"):
        MediaGenerationRequest.from_payload(raw)


def test_payload_unknown_media_form_rejected(rig: _Rig) -> None:
    """未知形态在构造期被拒（不是读时才发现）。"""
    with pytest.raises(ValueError, match="mediaForm"):
        MediaGenerationRequest(
            task_kind=TASK_STATIC_IMAGE, batch_kind="TRIAL",
            model_profile_id=rig.model_profile_id, media_form="GIF",
        )


# ===========================================================================
# 发起期
# ===========================================================================


def test_start_live_materializes_exactly_one_row(rig: _Rig) -> None:
    """α：Live 每内容恰好 1 个身份行，sort_order=0，载荷带 LIVE。"""
    content_id = _seed_content_with_body(rig.env)
    rows, created = _start(rig, content_id, form=MEDIA_FORM_LIVE)
    assert created is True
    assert len(rows) == 1
    assert rows[0].sort_order == 0
    assert rows[0].generation_request["mediaForm"] == MEDIA_FORM_LIVE


def test_start_static_still_materializes_three_rows(rig: _Rig) -> None:
    """零回归：静态仍是 3 行，载荷带 STATIC。"""
    content_id = _seed_content_with_body(rig.env)
    rows, _ = _start(rig, content_id, form=MEDIA_FORM_STATIC)
    assert sorted(r.sort_order for r in rows) == [0, 1, 2]
    assert {r.generation_request["mediaForm"] for r in rows} == {MEDIA_FORM_STATIC}


def test_start_live_after_static_is_conflict(rig: _Rig) -> None:
    """同篇内容已有静态 ⇒ 再发起 Live 抛 MEDIA_FORM_CONFLICT（而不是撞唯一索引）。"""
    content_id = _seed_content_with_body(rig.env)
    _start(rig, content_id, form=MEDIA_FORM_STATIC)
    with pytest.raises(m_service.MediaGenerationError) as exc:
        _start(rig, content_id, form=MEDIA_FORM_LIVE)
    assert exc.value.reason == REASON_MEDIA_FORM_CONFLICT


def test_start_static_after_live_is_conflict(rig: _Rig) -> None:
    """反向同理。"""
    content_id = _seed_content_with_body(rig.env)
    _start(rig, content_id, form=MEDIA_FORM_LIVE)
    with pytest.raises(m_service.MediaGenerationError) as exc:
        _start(rig, content_id, form=MEDIA_FORM_STATIC)
    assert exc.value.reason == REASON_MEDIA_FORM_CONFLICT


def test_start_live_rejects_three_key_binding_map(rig: _Rig) -> None:
    """Live 键域恰为 {0}：给了 {0,1,2} 即拒（防静态口径漏进 Live）。"""
    content_id = _seed_content_with_body(rig.env)
    asset_id = _seed_reference_asset(rig.env)
    binding = [{"assetId": str(asset_id), "part": None, "note": None}]
    with session_scope(rig.settings) as session:
        with pytest.raises(SchemaViolation):
            m_service.start_media_generation(
                MediaRepository(session),
                project_id=rig.env.project_id,
                content_id=content_id,
                request=_request(rig, form=MEDIA_FORM_LIVE),
                bindings_by_sort_order={0: binding, 1: binding, 2: binding},
                idempotency_key="idem-bad-keys",
                created_by=rig.env.owner_id,
            )


# ===========================================================================
# 执行期：Live 端到端
# ===========================================================================


def _start_one_live(rig: _Rig) -> tuple[uuid.UUID, uuid.UUID]:
    content_id = _seed_content_with_body(rig.env)
    rows, _ = _start(rig, content_id, form=MEDIA_FORM_LIVE)
    return content_id, rows[0].id


def test_live_end_to_end_persists_version_and_four_components(rig: _Rig) -> None:
    content_id, item_id = _start_one_live(rig)
    outcome = _run(rig, content_id, item_id, motion=_FakeMotionGenerator())

    assert outcome.state == MEDIA_STATE_READY
    assert outcome.media_form == MEDIA_FORM_LIVE
    roles = {c.role for c in outcome.components}
    assert roles == {
        COMPONENT_ROLE_STILL, COMPONENT_ROLE_PAIRED_MOTION,
        COMPONENT_ROLE_MANIFEST, COMPONENT_ROLE_PREVIEW,
    }
    # 红线：全部合成，无任何"已验证"取值。
    assert {c.verification for c in outcome.components} == {VERIFICATION_SYNTHETIC}

    # S2：STILL 组件的 file_id 就是版本的 image_file_id（同一个量只有一个来源）。
    still = next(c for c in outcome.components if c.role == COMPONENT_ROLE_STILL)
    assert still.file_id == outcome.image_file_id

    # 落库事实：版本行形态、组件行数。
    assert _scalar(
        rig, "SELECT media_form FROM media_versions WHERE id = :v", v=outcome.version_id
    ) == MEDIA_FORM_LIVE
    assert _scalar(
        rig, "SELECT count(*) FROM media_components WHERE media_version_id = :v",
        v=outcome.version_id,
    ) == 4


def test_live_detail_read_carries_form_and_components(rig: _Rig) -> None:
    content_id, item_id = _start_one_live(rig)
    _run(rig, content_id, item_id, motion=_FakeMotionGenerator())

    with session_scope(rig.settings) as session:
        detail = m_service.read_media_detail(
            MediaRepository(session), media_item_id=item_id,
            version_limit=10, version_offset=0,
        )
        assert detail.media_form == MEDIA_FORM_LIVE
        assert len(detail.components) == 4
        assert {c.verification for c in detail.components} == {VERIFICATION_SYNTHETIC}
        assert detail.versions[0].media_form == MEDIA_FORM_LIVE


def test_live_manifest_is_synthetic_and_has_no_verified_wording(rig: _Rig) -> None:
    """红线：manifest 字节里不得出现"已验证"，且声明合成来源。"""
    content_id, item_id = _start_one_live(rig)
    outcome = _run(rig, content_id, item_id, motion=_FakeMotionGenerator())
    manifest = next(c for c in outcome.components if c.role == COMPONENT_ROLE_MANIFEST)
    blob_key = _scalar(
        rig,
        "SELECT b.object_key FROM file_objects f JOIN blobs b ON b.id = f.blob_id"
        " WHERE f.id = :f",
        f=manifest.file_id,
    )
    body = (rig.storage_root / blob_key).read_text(encoding="utf-8")
    assert "SYNTHETIC_GENERATOR" in body
    assert "已验证" not in body


# ===========================================================================
# 执行期：封装失败 ⇒ 整组回滚
# ===========================================================================


def test_live_group_too_large_rolls_back_everything(rig: _Rig) -> None:
    content_id, item_id = _start_one_live(rig)
    with pytest.raises(m_service.MediaGenerationError) as exc:
        _run(rig, content_id, item_id, motion=_FakeMotionGenerator(),
             live_group_max_bytes=1024)
    assert exc.value.reason == ERR_GROUP_TOO_LARGE

    # 不写半组：无版本、无组件、无生成上下文。
    assert _scalar(rig, "SELECT count(*) FROM media_versions") == 0
    assert _scalar(rig, "SELECT count(*) FROM media_components") == 0
    assert _scalar(rig, "SELECT count(*) FROM media_generation_contexts") == 0
    # 媒体被置 FAILED，预占被释放且从未结算。
    assert _scalar(rig, "SELECT state FROM media_items WHERE id = :i", i=item_id) == MEDIA_STATE_FAILED
    assert len(rig.ledger.reserves) == 1
    assert [r["id"] for r in rig.ledger.releases] == [rig.ledger.reserves[0]["id"]]
    assert rig.ledger.settles == []


# ===========================================================================
# 计费口径
# ===========================================================================


def test_live_estimate_uses_live_form_and_one_content(rig: _Rig) -> None:
    """估价以 form=LIVE、content_count=1 调用（张数 2 由 calls_for 折出，不在调用点写算式）。"""
    content_id, item_id = _start_one_live(rig)
    _run(rig, content_id, item_id, motion=_FakeMotionGenerator())
    assert rig.ledger.estimates == [
        {"content_count": 1, "form": MEDIA_FORM_LIVE, "task_kind": TASK_STATIC_IMAGE}
    ]
    assert calls_for(TASK_STATIC_IMAGE, 1, form=MEDIA_FORM_LIVE) == 2


def test_live_reserve_and_settle_both_carry_op206_note(rig: _Rig) -> None:
    content_id, item_id = _start_one_live(rig)
    _run(rig, content_id, item_id, motion=_FakeMotionGenerator())
    assert [r["note"] for r in rig.ledger.reserves] == [OP206_NOTE]
    assert [s["note"] for s in rig.ledger.settles] == [OP206_NOTE]


# ===========================================================================
# 装配守卫
# ===========================================================================


def test_live_without_motion_generator_fails_before_reserve(rig: _Rig) -> None:
    content_id, item_id = _start_one_live(rig)
    with pytest.raises(m_service.MediaGenerationError) as exc:
        _run(rig, content_id, item_id, motion=None)
    assert exc.value.reason == REASON_GENERATOR_NOT_CONFIGURED
    # 预占之前拒：估价与预占都从未发生。
    assert rig.ledger.estimates == []
    assert rig.ledger.reserves == []


# ===========================================================================
# 静态零回归
# ===========================================================================


def test_static_path_unchanged(rig: _Rig) -> None:
    content_id = _seed_content_with_body(rig.env)
    rows, _ = _start(rig, content_id, form=MEDIA_FORM_STATIC)
    outcome = _run(rig, content_id, rows[0].id)  # 静态不需要 motion_generator

    assert outcome.media_form == MEDIA_FORM_STATIC
    assert outcome.components == ()
    assert rig.ledger.estimates == [
        {"content_count": 1, "form": MEDIA_FORM_STATIC, "task_kind": TASK_STATIC_IMAGE}
    ]
    assert [r["note"] for r in rig.ledger.reserves] == [None]
    assert [s["note"] for s in rig.ledger.settles] == [None]
    assert _scalar(rig, "SELECT count(*) FROM media_components") == 0

    with session_scope(rig.settings) as session:
        detail = m_service.read_media_detail(
            MediaRepository(session), media_item_id=rows[0].id,
            version_limit=10, version_offset=0,
        )
        assert detail.media_form == MEDIA_FORM_STATIC
        assert detail.components == ()
