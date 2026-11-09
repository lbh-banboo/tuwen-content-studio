#!/usr/bin/env python3
"""合成 Live 样本生成器（WP28 · OP-76 修订版 §2.2）。

不依赖供应商、不依赖真机、不依赖数据库：本机生成一组 Live 样本到目标目录，
并写 `SHA256SUMS.json`。

产出：
    still.jpg        竖版 3:4 静帧（Pillow 程序绘制，无版权问题）
    motion.mov       3 秒动态段（本机 ffmpeg 合成）
    manifest.json    配对关系（source 恒为 SYNTHETIC_GENERATOR）
    SHA256SUMS.json  每个文件的 sha256 与字节数

判据：**生成物结构完整、每个文件有 sha256、能被读回**。
**不判**「能否被相册识别为实况」—— 那是真机的事（上线前验收项）。

缺 ffmpeg：**退出码 3 并说明原因**，不写占位字节冒充动态段
（与 `media/live_pack.py` 的 fail-closed 同口径）。

用法：
    python tools/dev/m0/make_live_sample.py --out /tmp/live-sample
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess  # noqa: S404
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_NO_FFMPEG = 3
EXIT_FAILED = 4

WIDTH, HEIGHT = 768, 1024  # 3:4
DURATION_S = 3


def make_still() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (WIDTH, HEIGHT), (36, 44, 62))
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT):  # 竖向渐变
        t = y / HEIGHT
        draw.line([(0, y), (WIDTH, y)], fill=(int(36 + 150 * t), int(44 + 90 * t), int(62 + 40 * t)))
    draw.ellipse([WIDTH * 0.25, HEIGHT * 0.3, WIDTH * 0.75, HEIGHT * 0.3 + WIDTH * 0.5], fill=(255, 224, 160))
    draw.rectangle([60, HEIGHT - 160, WIDTH - 60, HEIGHT - 100], outline=(255, 255, 255), width=4)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def make_motion(still: bytes, workdir: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise FileNotFoundError("ffmpeg")
    src, dst = workdir / "_still.jpg", workdir / "_motion.mov"
    src.write_bytes(still)
    proc = subprocess.run(  # noqa: S603
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-loop", "1", "-i", str(src), "-t", str(DURATION_S),
            "-vf", f"zoompan=z='min(zoom+0.0015,1.15)':d=1:s={WIDTH}x{HEIGHT}:fps=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", "-f", "mov", str(dst),
        ],
        capture_output=True, timeout=120, check=False,
    )
    try:
        if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-300:])
        return dst.read_bytes()
    finally:
        src.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path, help="输出目录（不存在则创建）")
    args = ap.parse_args(argv)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    still = make_still()
    try:
        motion = make_motion(still, out)
    except FileNotFoundError:
        print("缺 ffmpeg：不生成动态段，也不写占位字节冒充。", file=sys.stderr)
        return EXIT_NO_FFMPEG
    except Exception as exc:  # noqa: BLE001
        print(f"动态段合成失败：{exc}", file=sys.stderr)
        return EXIT_FAILED

    manifest = {
        "source": "SYNTHETIC_GENERATOR",
        "verification": "SYNTHETIC",
        "pair": {"still": "still.jpg", "pairedMotion": "motion.mov"},
        "still": {"sha256": hashlib.sha256(still).hexdigest(), "byteSize": len(still)},
        "pairedMotion": {"sha256": hashlib.sha256(motion).hexdigest(), "byteSize": len(motion)},
        "note": "合成模式：平台可识别性待设备验证（上线前验收项）。",
    }
    files = {
        "still.jpg": still,
        "motion.mov": motion,
        "manifest.json": json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
    }
    sums = {}
    for name, content in files.items():
        (out / name).write_bytes(content)
        sums[name] = {"sha256": hashlib.sha256(content).hexdigest(), "byteSize": len(content)}
    (out / "SHA256SUMS.json").write_text(
        json.dumps(sums, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )

    # 读回自检：落盘字节与摘要一致
    for name, meta in sums.items():
        back = (out / name).read_bytes()
        if hashlib.sha256(back).hexdigest() != meta["sha256"]:
            print(f"读回校验失败：{name}", file=sys.stderr)
            return EXIT_FAILED
    print(f"OK {out} files={len(sums)} total_bytes={sum(m['byteSize'] for m in sums.values())}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
