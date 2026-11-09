"""WP28 4.4 · `live_pack.py` 测试（无数据库）。

三条失败路径各一例（文档 §3.4）：超限 / 缺 ffmpeg / 半组。
真合成用例在本机无 ffmpeg 时显式 SKIP（原因写明），**不假绿**；
「缺 ffmpeg」路径不依赖真 ffmpeg（注入 `which`），任何环境都跑。
"""

from __future__ import annotations

import io
import json
import shutil
import uuid

import pytest
from PIL import Image

from rednote.modules.media.live_pack import (
    ERR_FFMPEG_MISSING,
    ERR_GROUP_TOO_LARGE,
    ERR_REQUEST_INVALID,
    ERR_STORE_FAILED,
    LIVE_GROUP_MAX_BYTES_DEFAULT,
    LiveMotionError,
    LiveMotionGenerator,
    LiveMotionStub,
    LivePackRequest,
    LivePackStub,
    STATE_FAILED,
    STATE_SUCCEEDED,
    build_manifest,
    probe_ffmpeg,
    require_ffmpeg,
)
from rednote.modules.media.types import (
    COMPONENT_ROLE_MANIFEST,
    COMPONENT_ROLE_PAIRED_MOTION,
    COMPONENT_ROLE_PREVIEW,
    COMPONENT_ROLE_STILL,
    VERIFICATION_SYNTHETIC,
)

HAS_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(
    not HAS_FFMPEG, reason="本机无 ffmpeg：真合成用例显式跳过（缺失路径由注入 which 的用例覆盖）"
)


def _no_ffmpeg(_name: str) -> None:
    return None


def _jpeg(w: int = 768, h: int = 1024) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 120, 90)).save(buf, "JPEG")
    return buf.getvalue()


class MemSink:
    def __init__(self, *, fail_on_role: str | None = None) -> None:
        self.items: dict[uuid.UUID, tuple[str, int]] = {}
        self.fail_on_role = fail_on_role

    def store(self, *, role: str, name: str, content: bytes, mime_type: str) -> uuid.UUID:
        if role == self.fail_on_role:
            raise OSError("磁盘写失败（测试注入）")
        fid = uuid.uuid4()
        self.items[fid] = (role, len(content))
        return fid


def _req(motion: bytes = b"\x00" * 2048, still: bytes | None = None, **kw) -> LivePackRequest:
    return LivePackRequest(
        request_id="r1",
        job_id="j1",
        still_content=still if still is not None else _jpeg(),
        motion_content=motion,
        live_profile_key="placeholder-v1",
        **kw,
    )


# ---------------------------------------------------------------------------
# 成功路径（真 ffmpeg）
# ---------------------------------------------------------------------------


@needs_ffmpeg
def test_end_to_end_real_ffmpeg_four_components() -> None:
    still = _jpeg()
    motion = LiveMotionStub().dispatch_motion(still_content=still, duration_ms=3000)
    assert motion.synthetic is True and len(motion.content) > 0

    sink = MemSink()
    result = LivePackStub(sink=sink).pack(_req(motion=motion.content, still=still))

    assert result.state == STATE_SUCCEEDED
    assert result.metadata_validation == "PASS"
    assert result.verification == VERIFICATION_SYNTHETIC
    roles = result.by_role()
    assert set(roles) == {
        COMPONENT_ROLE_STILL,
        COMPONENT_ROLE_PAIRED_MOTION,
        COMPONENT_ROLE_MANIFEST,
        COMPONENT_ROLE_PREVIEW,
    }
    assert all(c.verification == VERIFICATION_SYNTHETIC for c in result.components)
    assert all(len(c.sha256) == 64 and c.byte_size > 0 for c in result.components)
    # 真的落了 4 个文件，且 preview_file_id 指向 PREVIEW
    assert len(sink.items) == 4
    assert result.preview_file_id == roles[COMPONENT_ROLE_PREVIEW].file_id


def test_motion_stub_satisfies_protocol() -> None:
    assert isinstance(LiveMotionStub(), LiveMotionGenerator)


# ---------------------------------------------------------------------------
# 失败路径 1：超限
# ---------------------------------------------------------------------------


def test_group_too_large_fails_before_any_write() -> None:
    sink = MemSink()
    big = b"\x00" * (LIVE_GROUP_MAX_BYTES_DEFAULT + 1)
    result = LivePackStub(sink=sink).pack(_req(motion=big))
    assert result.state == STATE_FAILED
    assert result.error_code == ERR_GROUP_TOO_LARGE
    assert result.components == ()
    assert sink.items == {}, "超限必须在写任何字节之前判定"


def test_group_limit_boundary_is_inclusive_of_manifest() -> None:
    """上限口径含 manifest：STILL+MOTION 恰好等于上限时，加上 manifest 必超。"""
    still = _jpeg()
    req = _req(motion=b"\x00" * 10, still=still)
    manifest_len = len(build_manifest(req, still=still, motion=req.motion_content))
    tight = len(still) + 10 + manifest_len
    sink = MemSink()
    over = LivePackStub(sink=sink, max_group_bytes=tight - 1).pack(req)
    assert over.error_code == ERR_GROUP_TOO_LARGE
    assert sink.items == {}


@needs_ffmpeg
def test_group_limit_exact_fit_passes() -> None:
    still = _jpeg()
    motion = LiveMotionStub().dispatch_motion(still_content=still, duration_ms=1000).content
    req = _req(motion=motion, still=still)
    exact = len(still) + len(motion) + len(build_manifest(req, still=still, motion=motion))
    result = LivePackStub(sink=MemSink(), max_group_bytes=exact).pack(req)
    assert result.state == STATE_SUCCEEDED


# ---------------------------------------------------------------------------
# 失败路径 2：缺 ffmpeg（注入 which，任何环境都跑）
# ---------------------------------------------------------------------------


def test_probe_reports_missing_ffmpeg() -> None:
    assert probe_ffmpeg(which=_no_ffmpeg) is None
    with pytest.raises(LiveMotionError) as ei:
        require_ffmpeg(which=_no_ffmpeg)
    assert ei.value.reason == ERR_FFMPEG_MISSING


def test_motion_stub_without_ffmpeg_does_not_fake_bytes() -> None:
    with pytest.raises(LiveMotionError) as ei:
        LiveMotionStub(which=_no_ffmpeg).dispatch_motion(still_content=_jpeg(), duration_ms=3000)
    assert ei.value.reason == ERR_FFMPEG_MISSING


def test_pack_without_ffmpeg_fails_whole_group_nothing_written() -> None:
    sink = MemSink()
    result = LivePackStub(sink=sink, which=_no_ffmpeg).pack(_req())
    assert result.state == STATE_FAILED
    assert result.error_code == ERR_FFMPEG_MISSING
    assert result.components == () and result.preview_file_id is None
    assert sink.items == {}


# ---------------------------------------------------------------------------
# 失败路径 3：半组
# ---------------------------------------------------------------------------


@needs_ffmpeg
@pytest.mark.parametrize(
    "fail_role",
    [COMPONENT_ROLE_STILL, COMPONENT_ROLE_PAIRED_MOTION, COMPONENT_ROLE_MANIFEST, COMPONENT_ROLE_PREVIEW],
)
def test_half_group_returns_failed_with_orphans(fail_role: str) -> None:
    still = _jpeg()
    motion = LiveMotionStub().dispatch_motion(still_content=still, duration_ms=1000).content
    sink = MemSink(fail_on_role=fail_role)
    result = LivePackStub(sink=sink).pack(_req(motion=motion, still=still))

    assert result.state == STATE_FAILED
    assert result.error_code == ERR_STORE_FAILED
    assert result.components == (), "失败时绝不返回半组"
    assert result.preview_file_id is None
    assert result.metadata_validation == "FAIL"
    # 失败前已落的文件 id 全部交回，数量 = 失败角色之前的顺位
    order = [COMPONENT_ROLE_STILL, COMPONENT_ROLE_PAIRED_MOTION, COMPONENT_ROLE_MANIFEST, COMPONENT_ROLE_PREVIEW]
    assert len(result.orphan_file_ids) == order.index(fail_role)
    assert set(result.orphan_file_ids) == set(sink.items)


# ---------------------------------------------------------------------------
# 入参与红线
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {"audio_policy": "LOUD"},
        {"cover_timestamp_ms": -1},
        {"live_profile_state": "VERIFIED"},
    ],
)
def test_invalid_requests_fail_closed(kw: dict) -> None:
    sink = MemSink()
    result = LivePackStub(sink=sink, which=_no_ffmpeg).pack(_req(**kw))
    assert result.state == STATE_FAILED
    assert result.error_code in {ERR_REQUEST_INVALID, "PROFILE_STATE_UNSUPPORTED"}
    assert sink.items == {}


def test_empty_bytes_rejected() -> None:
    result = LivePackStub(sink=MemSink(), which=_no_ffmpeg).pack(_req(motion=b""))
    assert result.error_code == ERR_REQUEST_INVALID


def test_state_never_unknown_and_manifest_has_no_verified_wording() -> None:
    req = _req()
    manifest = build_manifest(req, still=req.still_content, motion=req.motion_content)
    doc = json.loads(manifest)
    assert doc["source"] == "SYNTHETIC_GENERATOR"
    assert doc["verification"] == VERIFICATION_SYNTHETIC
    assert doc["liveProfileState"] == "PLACEHOLDER"
    text = manifest.decode("utf-8")
    assert "已验证" not in text and "VERIFIED" not in text.upper().replace("UNVERIFIED", "")
    # 所有失败/成功状态都在 {SUCCEEDED, FAILED}
    for r in (
        LivePackStub(sink=MemSink(), which=_no_ffmpeg).pack(req),
        LivePackStub(sink=MemSink(), max_group_bytes=1).pack(req),
    ):
        assert r.state in {STATE_SUCCEEDED, STATE_FAILED}


# ---------------------------------------------------------------------------
# 合成素材生成器（tools/dev/m0/make_live_sample.py）
# ---------------------------------------------------------------------------


def _sample_script():
    from pathlib import Path

    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "tools" / "dev" / "m0" / "make_live_sample.py"
        if cand.exists():
            return cand
    pytest.fail("找不到 tools/dev/m0/make_live_sample.py")


@needs_ffmpeg
def test_sample_generator_outputs_complete_group_with_sha256(tmp_path) -> None:
    import hashlib
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, str(_sample_script()), "--out", str(tmp_path)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    sums = json.loads((tmp_path / "SHA256SUMS.json").read_text(encoding="utf-8"))
    assert set(sums) == {"still.jpg", "motion.mov", "manifest.json"}
    for name, meta in sums.items():
        content = (tmp_path / name).read_bytes()
        assert hashlib.sha256(content).hexdigest() == meta["sha256"]
        assert len(content) == meta["byteSize"] > 0
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"] == "SYNTHETIC_GENERATOR" and manifest["verification"] == "SYNTHETIC"
    with Image.open(tmp_path / "still.jpg") as im:
        assert im.size[0] * 4 == im.size[1] * 3, "静帧必须是竖版 3:4"


def test_sample_generator_without_ffmpeg_exits_3_and_writes_no_motion(tmp_path) -> None:
    import os
    import subprocess
    import sys

    env = dict(os.environ, PATH="/nonexistent-bin")
    proc = subprocess.run(
        [sys.executable, str(_sample_script()), "--out", str(tmp_path)],
        capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 3, (proc.returncode, proc.stderr)
    assert not (tmp_path / "motion.mov").exists(), "缺 ffmpeg 不得写占位动态段"
    assert not (tmp_path / "manifest.json").exists()
