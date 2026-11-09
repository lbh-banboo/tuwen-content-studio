"""WP28 4.4 · Live 封装桩与动态段桩（全程 `SYNTHETIC`）。

## 本模块是什么、不是什么

- `LivePackStub`：把「静帧 + 动态段 + manifest」按 OP-02 规格 §5 的
  `LivePackRequest → LivePackResult` 封装成一组组件。**纯本地、不出网、不碰数据库**。
  字节落盘经注入的 `ComponentSink`（4.5 用 `files.store_generated_asset` 实现），
  所以本模块没有对 `files` / `versions` 的 import（平级不互 import）。
- `LiveMotionStub`：`LiveMotionGenerator` 的本机实现，用 ffmpeg 由静帧合成 3 秒动态段。
  **它不是假供应商**——只为让系统链路在没有真实视频模型时跑通（OP-76 修订版）。
- **不声称**平台（Apple Photos / 小红书）能识别为 Live。`metadata_validation=PASS`
  只表示「文件结构检查通过」（规格 §5 关键约束），不替代真机发布认证。
- **不模拟发布成功**：只到「封装资产已产出」为止。

## 三条 fail-closed 路径（文档 4.4 验证要求）

| 路径 | 错误码 | 行为 |
|---|---|---|
| 组内体积超限 | `GROUP_TOO_LARGE` | 在**写任何字节之前**判，零落盘 |
| 缺 ffmpeg | `FFMPEG_MISSING` | 明确报错，**不**回落到占位字节假装有动态段 |
| 半组（落盘中途失败） | `COMPONENT_STORE_FAILED` | `FAILED`，`components` 为空，已落的文件 id 放 `orphan_file_ids` 供 4.5 回滚 |

体积口径：`STILL + PAIRED_MOTION + MANIFEST` 合计（规格 §7.2）；`PREVIEW` 不进正式交付，不计入。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess  # noqa: S404 - 只调用 ffmpeg，参数为列表，不经 shell
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from rednote.modules.media.types import (
    COMPONENT_ROLE_MANIFEST,
    COMPONENT_ROLE_PAIRED_MOTION,
    COMPONENT_ROLE_PREVIEW,
    COMPONENT_ROLE_STILL,
    LIVE_PROFILE_PLACEHOLDER,
    VERIFICATION_SYNTHETIC,
)

#: 单组体积上限的缺省值（S6）。当前不读 `runtime_settings`；由 service/handler 注入值覆盖。
LIVE_GROUP_MAX_BYTES_DEFAULT: Final[int] = 20 * 1024 * 1024
#: 动态段缺省时长（毫秒）。
DEFAULT_MOTION_DURATION_MS: Final[int] = 3000
#: 兼容性档案版本：占位，**不是**已验证版本。
COMPATIBILITY_PROFILE_VERSION: Final[str] = "placeholder-0"
#: manifest 的来源标记（红线：只能是合成）。
MANIFEST_SOURCE: Final[str] = "SYNTHETIC_GENERATOR"

STATE_SUCCEEDED: Final[str] = "SUCCEEDED"
STATE_FAILED: Final[str] = "FAILED"
# 注意：刻意**没有** UNKNOWN —— 本桩纯本地，没有「不知道成没成」的情形（规格 §5 允许，但桩不产出）。

META_PASS: Final[str] = "PASS"
META_FAIL: Final[str] = "FAIL"

AUDIO_POLICIES: Final[tuple[str, ...]] = ("NONE", "KEEP", "STRIP")

ERR_GROUP_TOO_LARGE: Final[str] = "GROUP_TOO_LARGE"
ERR_FFMPEG_MISSING: Final[str] = "FFMPEG_MISSING"
ERR_MOTION_FAILED: Final[str] = "MOTION_FAILED"
ERR_STORE_FAILED: Final[str] = "COMPONENT_STORE_FAILED"
ERR_REQUEST_INVALID: Final[str] = "REQUEST_INVALID"
ERR_PROFILE_NOT_PLACEHOLDER: Final[str] = "PROFILE_STATE_UNSUPPORTED"

_MIME: Final[Mapping[str, str]] = {
    COMPONENT_ROLE_STILL: "image/jpeg",
    COMPONENT_ROLE_PAIRED_MOTION: "video/quicktime",
    COMPONENT_ROLE_MANIFEST: "application/json",
    COMPONENT_ROLE_PREVIEW: "video/mp4",
}
_EXT: Final[Mapping[str, str]] = {
    COMPONENT_ROLE_STILL: "jpg",
    COMPONENT_ROLE_PAIRED_MOTION: "mov",
    COMPONENT_ROLE_MANIFEST: "json",
    COMPONENT_ROLE_PREVIEW: "mp4",
}


class LiveMotionError(RuntimeError):
    """动态段生成失败。`reason` 机器可判。"""

    def __init__(self, *, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


# ---------------------------------------------------------------------------
# ffmpeg 探测
# ---------------------------------------------------------------------------


def probe_ffmpeg(*, which: Callable[[str], str | None] = shutil.which) -> str | None:
    """返回 ffmpeg 可执行文件路径；找不到返回 `None`。

    `which` 可注入：测试用它造「缺 ffmpeg」，**不改 PATH、不 mock subprocess**。
    """
    return which("ffmpeg")


def require_ffmpeg(*, which: Callable[[str], str | None] = shutil.which) -> str:
    path = probe_ffmpeg(which=which)
    if path is None:
        raise LiveMotionError(
            reason=ERR_FFMPEG_MISSING,
            message=(
                "本机未找到 ffmpeg，无法合成 Live 动态段。"
                "本包 fail-closed：不用占位字节冒充动态段。"
                "（ffmpeg 为系统二进制，不在 pyproject；已登记进上线前清单。）"
            ),
        )
    return path


# ---------------------------------------------------------------------------
# 动态段（LiveMotionGenerator）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MotionOutput:
    content: bytes
    container: str
    duration_ms: int
    synthetic: bool = True


@runtime_checkable
class LiveMotionGenerator(Protocol):
    """动态段的**出网**边界（F2：不并入 `MediaGenerator`）。"""

    def dispatch_motion(
        self, *, still_content: bytes, duration_ms: int
    ) -> MotionOutput:  # pragma: no cover - 协议声明
        ...


@dataclass(frozen=True, slots=True)
class LiveMotionStub:
    """用 ffmpeg 由静帧合成动态段（缓慢推近）。`which` 可注入以便测试缺失路径。"""

    which: Callable[[str], str | None] = shutil.which
    timeout_s: int = 60

    def dispatch_motion(self, *, still_content: bytes, duration_ms: int) -> MotionOutput:
        ffmpeg = require_ffmpeg(which=self.which)
        if duration_ms <= 0:
            raise LiveMotionError(reason=ERR_MOTION_FAILED, message="动态段时长必须为正。")
        seconds = duration_ms / 1000
        with tempfile.TemporaryDirectory(prefix="live-motion-") as tmp:
            src = Path(tmp) / "still.img"
            dst = Path(tmp) / "motion.mov"
            src.write_bytes(still_content)
            cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-loop", "1", "-i", str(src), "-t", f"{seconds:.3f}",
                "-vf", "scale=720:960:force_original_aspect_ratio=increase,crop=720:960,"
                       "zoompan=z='min(zoom+0.0015,1.15)':d=1:s=720x960:fps=30",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", "-f", "mov", str(dst),
            ]
            try:
                proc = subprocess.run(  # noqa: S603 - 参数列表，无 shell
                    cmd, capture_output=True, timeout=self.timeout_s, check=False
                )
            except subprocess.TimeoutExpired as exc:
                raise LiveMotionError(
                    reason=ERR_MOTION_FAILED, message="ffmpeg 合成动态段超时。"
                ) from exc
            if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
                tail = proc.stderr.decode("utf-8", "replace")[-300:]
                raise LiveMotionError(
                    reason=ERR_MOTION_FAILED, message=f"ffmpeg 合成动态段失败：{tail}"
                )
            return MotionOutput(content=dst.read_bytes(), container="mov", duration_ms=duration_ms)


def make_preview_mp4(motion: bytes, *, which: Callable[[str], str | None] = shutil.which) -> bytes:
    """由动态段转一份浏览器可播的 MP4 预览（`PREVIEW`，不进正式交付）。"""
    ffmpeg = require_ffmpeg(which=which)
    with tempfile.TemporaryDirectory(prefix="live-preview-") as tmp:
        src, dst = Path(tmp) / "m.mov", Path(tmp) / "p.mp4"
        src.write_bytes(motion)
        proc = subprocess.run(  # noqa: S603
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(dst)],
            capture_output=True, timeout=60, check=False,
        )
        if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            raise LiveMotionError(reason=ERR_MOTION_FAILED, message="ffmpeg 生成预览失败。")
        return dst.read_bytes()


# ---------------------------------------------------------------------------
# 封装
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LivePackRequest:
    """规格 §5 `LivePackRequest` 的桩侧形态：文件 id 换成字节（纯本地，不查库）。"""

    request_id: str
    job_id: str
    still_content: bytes
    motion_content: bytes
    live_profile_key: str
    live_profile_state: str = LIVE_PROFILE_PLACEHOLDER
    cover_timestamp_ms: int = 0
    audio_policy: str = "NONE"


@dataclass(frozen=True, slots=True)
class StoredComponent:
    role: str
    file_id: uuid.UUID
    sha256: str
    byte_size: int
    verification: str = VERIFICATION_SYNTHETIC


@runtime_checkable
class ComponentSink(Protocol):
    """字节落点。4.5 用 `files.store_generated_asset` 实现；测试用内存实现。"""

    def store(
        self, *, role: str, name: str, content: bytes, mime_type: str
    ) -> uuid.UUID:  # pragma: no cover - 协议声明
        ...


@dataclass(frozen=True, slots=True)
class LivePackResult:
    """规格 §5 `LivePackResult`。`state` 只会是 `SUCCEEDED` / `FAILED`。"""

    state: str
    components: Sequence[StoredComponent] = ()
    preview_file_id: uuid.UUID | None = None
    metadata_validation: str = META_FAIL
    compatibility_profile_version: str = COMPATIBILITY_PROFILE_VERSION
    error_code: str | None = None
    error_message: str | None = None
    verification: str = VERIFICATION_SYNTHETIC
    #: 失败时：已经落盘、需要 4.5 在事务回滚里一并清理引用的文件 id。
    orphan_file_ids: Sequence[uuid.UUID] = field(default_factory=tuple)

    def by_role(self) -> dict[str, StoredComponent]:
        return {c.role: c for c in self.components}


def _fail(code: str, message: str, orphans: Sequence[uuid.UUID] = ()) -> LivePackResult:
    return LivePackResult(
        state=STATE_FAILED,
        metadata_validation=META_FAIL,
        error_code=code,
        error_message=message,
        orphan_file_ids=tuple(orphans),
    )


def build_manifest(
    request: LivePackRequest, *, still: bytes, motion: bytes
) -> bytes:
    """配对 manifest。`source` 恒为合成；**不含**任何「已验证」字样。"""
    doc = {
        "source": MANIFEST_SOURCE,
        "verification": VERIFICATION_SYNTHETIC,
        "liveProfileKey": request.live_profile_key,
        "liveProfileState": request.live_profile_state,
        "compatibilityProfileVersion": COMPATIBILITY_PROFILE_VERSION,
        "coverTimestampMs": request.cover_timestamp_ms,
        "audioPolicy": request.audio_policy,
        "still": {"sha256": hashlib.sha256(still).hexdigest(), "byteSize": len(still)},
        "pairedMotion": {"sha256": hashlib.sha256(motion).hexdigest(), "byteSize": len(motion)},
        "note": "合成模式：平台可识别性待设备验证（上线前验收项）。",
    }
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


@dataclass(frozen=True, slots=True)
class LivePackStub:
    """纯本地 Live 封装桩。"""

    sink: ComponentSink
    max_group_bytes: int = LIVE_GROUP_MAX_BYTES_DEFAULT
    which: Callable[[str], str | None] = shutil.which

    def pack(self, request: LivePackRequest) -> LivePackResult:
        # ① 入参校验（全部在写任何字节之前）
        if not request.still_content or not request.motion_content:
            return _fail(ERR_REQUEST_INVALID, "静帧与动态段的字节都不能为空。")
        if request.audio_policy not in AUDIO_POLICIES:
            return _fail(ERR_REQUEST_INVALID, f"audio_policy 只能是 {list(AUDIO_POLICIES)}。")
        if request.cover_timestamp_ms < 0:
            return _fail(ERR_REQUEST_INVALID, "cover_timestamp_ms 不能为负。")
        if request.live_profile_state != LIVE_PROFILE_PLACEHOLDER:
            return _fail(
                ERR_PROFILE_NOT_PLACEHOLDER,
                "本阶段只接受 PLACEHOLDER 档案；其余状态须真机验证后另行放开。",
            )

        # ② 组装全部字节 + 体积判定（STILL+MOTION+MANIFEST，PREVIEW 不计）
        manifest = build_manifest(request, still=request.still_content, motion=request.motion_content)
        total = len(request.still_content) + len(request.motion_content) + len(manifest)
        if total > self.max_group_bytes:
            return _fail(
                ERR_GROUP_TOO_LARGE,
                f"Live 组体积 {total} 字节超过上限 {self.max_group_bytes} 字节。",
            )

        # ③ 预览（需要 ffmpeg；缺失 ⇒ 整组失败，不留半组）
        try:
            preview = make_preview_mp4(request.motion_content, which=self.which)
        except LiveMotionError as exc:
            return _fail(exc.reason, str(exc))

        # ④ 顺序落盘；任一失败 ⇒ FAILED + 已落文件 id 交回（不返回半组）
        plan = (
            (COMPONENT_ROLE_STILL, request.still_content),
            (COMPONENT_ROLE_PAIRED_MOTION, request.motion_content),
            (COMPONENT_ROLE_MANIFEST, manifest),
            (COMPONENT_ROLE_PREVIEW, preview),
        )
        stored: list[StoredComponent] = []
        for role, content in plan:
            try:
                file_id = self.sink.store(
                    role=role,
                    name=f"live-{request.request_id}-{role.lower()}.{_EXT[role]}",
                    content=content,
                    mime_type=_MIME[role],
                )
            except Exception as exc:  # noqa: BLE001 - 任何落盘异常都按半组处理
                return _fail(
                    ERR_STORE_FAILED,
                    f"落盘 {role} 失败：{exc}",
                    orphans=[c.file_id for c in stored],
                )
            stored.append(
                StoredComponent(
                    role=role,
                    file_id=file_id,
                    sha256=hashlib.sha256(content).hexdigest(),
                    byte_size=len(content),
                )
            )

        preview_component = next(c for c in stored if c.role == COMPONENT_ROLE_PREVIEW)
        return LivePackResult(
            state=STATE_SUCCEEDED,
            components=tuple(stored),
            preview_file_id=preview_component.file_id,
            metadata_validation=META_PASS,  # 仅表示结构检查通过，见模块 docstring
        )


__all__ = [
    "AUDIO_POLICIES",
    "COMPATIBILITY_PROFILE_VERSION",
    "ComponentSink",
    "ERR_FFMPEG_MISSING",
    "ERR_GROUP_TOO_LARGE",
    "ERR_MOTION_FAILED",
    "ERR_REQUEST_INVALID",
    "ERR_STORE_FAILED",
    "LIVE_GROUP_MAX_BYTES_DEFAULT",
    "LiveMotionError",
    "LiveMotionGenerator",
    "LiveMotionStub",
    "LivePackRequest",
    "LivePackResult",
    "LivePackStub",
    "META_FAIL",
    "META_PASS",
    "MotionOutput",
    "STATE_FAILED",
    "STATE_SUCCEEDED",
    "StoredComponent",
    "build_manifest",
    "make_preview_mp4",
    "probe_ffmpeg",
    "require_ffmpeg",
]
