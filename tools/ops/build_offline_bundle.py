#!/usr/bin/env python
"""离线安装包构建（WP46 ②）：镜像 tar + wheelhouse + 源码 tar + 校验清单。

## 为什么是一个构建脚本，而不是把包本身入库

制品是数 GB（镜像 tar + wheelhouse）。入库会毁掉仓库。
⇒ **入库的是「怎么造」和「造出来对不对」，不是「造出来的东西」**：
构建脚本 + 清单格式 + 判据 + 证据四类文本入库，制品落 `dist/`
（`.gitignore:43` 已忽略），判据 `prod:artifacts-not-tracked` 断言
`git ls-files` 里没有 `dist/`。

形态不是本包选的，是 ADR-11 的验证表定的：
「可出网机器 `docker pull` + `docker save` → 内网 `docker load` → **断网**后 `up -d`」。

## 为什么不复用 `sync-kit/01-打包/make-bundle.sh`

那份打包的是**开发机工作区**（把代码同步到另一台开发机），
本脚本打包的是**内网部署物**（镜像 + 目标平台 wheel + 校验清单）。
两者的输入、目标平台、校验要求全不同 —— 复用会把"同步代码"的口径
带进"离线部署"，而后者多出来的那部分（目标平台三元组、digest、介质校验）
恰恰是 WP46 的全部内容。

## ⚠️ 目标平台三元组是 `[待核实]` —— 这决定了 wheelhouse 是不是"演示构建"

本机是 **darwin/arm64**（实测 `uname -sm` → `Darwin arm64`）。
目标内网机的 CPU 架构 / libc / OS 发行版**未定**（硬件采购未落地，
可能是信创 ARM + 麒麟）。本脚本把平台三元组**写进 MANIFEST.json**，
并由判据 `bundle:wheel-tags-match-manifest` 断言每个 `.whl` 的 tag
与声明一致 —— 这挡住的是"声明一套、装进去另一套"。
它**挡不住**"声明本身就是错的"：那要等目标平台定下来。

⇒ 本机构建出的 wheelhouse 只能读作「清单可复现、校验值可重算」，
  **不能**读作「内网 Linux 可安装」。本机是 darwin/arm64，那些 manylinux wheel
  在本机根本无法 `pip install` 验证。

## 本机实测的能力边界（2026-10-18）

| 子命令 | 本机能跑？ | 读数 |
|---|---|---|
| `wheelhouse` | ✅ 能 | 19 条直接声明 → **51 个 wheel / 64 MB / 38 秒**（走 pip 配置里的镜像源，不是 pypi.org 直连） |
| `repo` | ✅ 能 | `git archive HEAD` |
| `sbom` | ✅ 能 | 委托 `tools/ops/gen_sbom.py` |
| `manifest` | ✅ 能 | SHA256SUMS.txt + MANIFEST.json，且**现场重算**一遍自校验 |
| `images` | ❌ **不能** | Docker daemon 不可达（实测 `docker info` → `dial unix /Users/<user>/.orbstack/run/docker.sock: no such file`）。且 7 条镜像引用**全部 digest 未回填**、其中 3 条连 tag 都是 PENDING ⇒ 就算有 daemon 也拉不到。此步**必须在可出网机器上跑**。 |

`images` 不可跑时本脚本**报错退出**，不静默跳过 ——
"跳过了"与"打包好了"在日志里不能同形。

## 索引来源也是事实的一部分

wheelhouse 从**哪个索引**下来的，会被写进 MANIFEST.json 的 `index_url`。
本机 pip 配置实测 `global.index-url = https://mirrors.aliyun.com/pypi/simple/`
（不是 pypi.org）。镜像站与上游理论上内容一致，但那是**假设**不是事实 ⇒
把实际用的索引记下来，内网若对不上校验值，至少知道从哪查。

## 用法

    python tools/ops/build_offline_bundle.py wheelhouse
    python tools/ops/build_offline_bundle.py manifest
    python tools/ops/build_offline_bundle.py all --out dist/bundle-20261018
    python tools/ops/build_offline_bundle.py images        # 本机必失败（无 daemon）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
PYPROJECT: Final[Path] = REPO_ROOT / "services" / "backend" / "pyproject.toml"
IMAGES_LOCK: Final[Path] = REPO_ROOT / "deploy" / "prod" / "images.lock.json"
#: 产物根目录。**不带时间戳**是刻意的：带了的话每次构建落一个新目录，
#: 判据与 gen_sbom 就得去"找最新那个"，而"找最新"在并发/残留目录下会挑错。
#: 固定路径 ⇒ 重跑直接覆盖，天然幂等，判据的默认路径也只有一个。
DEFAULT_OUT: Final[Path] = REPO_ROOT / "dist" / "bundle"

#: 目标 Python 版本的默认值。取自 `.github/workflows/ci.yml` 的
#: `PYTHON_VERSION: "3.13"` —— **不是**本机版本（本机这个 shell 里实测 3.11.9）。
#: 选 CI 口径而不是本机口径，是因为 CI 那个值是**项目声明过的**，
#: 本机值只是"这台机器恰好装了什么"。
#: ⚠️ 内网到底装哪个仍是 `[待核实]`（pyproject 写 `>=3.12`，CI 用 3.13）。
DEFAULT_PYTHON_VERSION: Final[str] = "3.13"

#: 目标平台 tag 的默认值。
#:
#: ⚠️ 为什么是三个而不是一个：pip 的 `--platform` 做**精确** tag 匹配，
#: 不替你推 manylinux 的向下兼容关系。只给 `manylinux_2_28_x86_64` 时，
#: 本机实测 `pydantic-core==2.23.4` 直接解析失败
#: （`from versions: 0.0.1` —— 它的 wheel 是 `manylinux_2_17` tag）。
#: 三个一起给才覆盖得住实际发行的那几种写法。
DEFAULT_PLATFORM_TAGS: Final[tuple[str, ...]] = (
    "manylinux_2_28_x86_64",
    "manylinux_2_17_x86_64",
    "manylinux2014_x86_64",
)

#: 子命令清单。**判据 `bundle:subcommands-selfcheck` 断言它与实际分发表全等**
#: （照 `backup_pg.py:88-97` 的 FINGERPRINT_CLASSES 处置：类别数单独判）。
#: 新增子命令忘了登记，或登记了没实现，都会当场报红。
SUBCOMMANDS: Final[tuple[str, ...]] = ("wheelhouse", "images", "repo", "sbom", "manifest", "all")

_REQ_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^([A-Za-z0-9._-]+)")


def _log(message: str) -> None:
    print(f"[bundle] {message}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def derive_requirements() -> list[str]:
    """从 pyproject **派生** requirements.txt 内容，不手抄。

    手抄一份 requirements.txt 就是第二处真相：pyproject 自述它是
    「项目需要哪些第三方包」的唯一权威，而手抄的那份会在下次加包时漂移
    （`docs/m3/工程债-依赖声明一致性说明.md` 记的 ISS-144 就是这一族：
    4 处注释写着"依赖已在 pyproject 锁定"，实际漏了 7 个包）。

    三组全要（`dependencies` + optional 的每一组），组名遍历磁盘不写死。
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project", {})
    reqs: list[str] = list(project.get("dependencies", []))
    for group_name in sorted((project.get("optional-dependencies") or {}).keys()):
        reqs.extend(project["optional-dependencies"][group_name])
    if not reqs:
        raise ValueError(f"{PYPROJECT} 解析出 0 条依赖 —— 空清单与『没有依赖』同形")
    return reqs


def _pip_index_url() -> str:
    """实际生效的 pip 索引地址。取不到就写明取不到，不编一个 pypi.org。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "config", "list"],
        capture_output=True,
        text=True,
    )
    for line in proc.stdout.splitlines():
        match = re.match(r"\s*global\.index-url\s*=\s*'?([^']+)'?", line)
        if match:
            return match.group(1).strip()
    return os.environ.get("PIP_INDEX_URL", "(未配置，pip 默认 https://pypi.org/simple/)")


def cmd_wheelhouse(out: Path, args: argparse.Namespace) -> dict[str, object]:
    """`pip download` 把**目标平台**的 wheel 全闭包拉到 out/wheelhouse。

    **不带 `--no-deps`**，这是关键：带了就只下直接依赖，闭包取不到，
    而判据 `sbom:python-closure-equals-wheelhouse` 仍会双向相等、全绿
    （与 `0 == 0` 恒真同族）。防线是 `sbom:closure-strictly-larger`
    —— 它断言闭包**严格大于**直接依赖集合。

    `--only-binary=:all:` 也是硬要求：允许 sdist 就意味着目标机要现场编译，
    而内网没有编译链、也没有编译期要拉的那些头文件。
    """
    wheelhouse = out / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    reqs = derive_requirements()
    req_file = out / "requirements.txt"
    req_file.write_text(
        "# 由 tools/ops/build_offline_bundle.py 从 services/backend/pyproject.toml 派生。\n"
        "# **不要手改** —— 改 pyproject 后重跑本脚本。\n"
        + "\n".join(reqs)
        + "\n",
        encoding="utf-8",
    )

    cmd = [
        sys.executable, "-m", "pip", "download",
        "--dest", str(wheelhouse),
        "--python-version", args.python_version,
        "--only-binary=:all:",
        "-r", str(req_file),
    ]
    for tag in args.platform_tag:
        cmd += ["--platform", tag]

    _log(f"pip download → {wheelhouse}（{len(reqs)} 条声明，目标 py{args.python_version}）")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"pip download 失败（exit={proc.returncode}）：\n"
            f"{(proc.stderr or proc.stdout)[-2000:]}"
        )

    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels:
        raise RuntimeError(f"pip download 退出码 0 但 {wheelhouse} 里没有 .whl —— 不可信")

    direct_names = {
        re.sub(r"[-_.]+", "-", _REQ_NAME_RE.match(r.strip()).group(1)).lower()  # type: ignore[union-attr]
        for r in reqs
    }
    _log(f"  {len(wheels)} 个 wheel（直接声明 {len(direct_names)} 个发行名）")
    return {
        "wheel_count": len(wheels),
        "direct_requirement_count": len(direct_names),
        "requirements_file": "requirements.txt",
        "index_url": _pip_index_url(),
    }


def cmd_images(out: Path, args: argparse.Namespace) -> dict[str, object]:
    """`docker save` 每个镜像引用成一个 tar。

    ## 本机**必失败**，而且必须失败得响亮

    本机 Docker daemon 不可达（实测）。更前置的问题：`images.lock.json` 里
    7 条引用**全部 digest 未回填**，其中 3 条的 tag 就是字面 `PENDING`
    ⇒ 就算有 daemon 也拉不到。

    这一步**不允许**"跳过并记一笔"。理由：日志里"跳过了"与"打包好了"
    一旦同形，运维就会带着一个没有镜像的包进内网 —— 而那时才发现，
    成本是一次进出机房。
    """
    lock = json.loads(IMAGES_LOCK.read_text(encoding="utf-8"))
    images = lock["images"]
    images_dir = out / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    unresolved = [i["ref"] for i in images if i.get("pin_state") == "unresolved"]
    if unresolved and not args.allow_unresolved:
        raise RuntimeError(
            "以下镜像引用尚未解析（tag 是 PENDING 标记），拉不下来：\n"
            + "".join(f"  - {ref}\n" for ref in unresolved)
            + "⇒ 先回填 images.lock.json 与 .env（见 deploy/prod/README.md §2）。\n"
            "  若只想跑通其余条目，加 --allow-unresolved —— 但产出的包是**不完整的**，"
            "MANIFEST.json 会把缺的条目列出来。"
        )

    probe = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise RuntimeError(
            "Docker daemon 不可达，`docker save` 无法执行：\n"
            f"  {(probe.stderr or '').strip()[:300]}\n"
            "⇒ 本步骤必须在**可出网且 daemon 可用**的机器上跑（ADR-11 验证表的第一步）。"
        )

    saved: list[dict[str, object]] = []
    for image in images:
        ref = str(image["ref"])
        if image.get("pin_state") == "unresolved":
            _log(f"  跳过（未解析）{ref}")
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", ref)
        tar = images_dir / f"{safe}.tar"
        _log(f"  docker pull + save {ref}")
        pull = subprocess.run(["docker", "pull", ref], capture_output=True, text=True)
        if pull.returncode != 0:
            raise RuntimeError(f"docker pull {ref} 失败：{(pull.stderr or '').strip()[:300]}")
        save = subprocess.run(
            ["docker", "save", "--output", str(tar), ref], capture_output=True, text=True
        )
        if save.returncode != 0:
            raise RuntimeError(f"docker save {ref} 失败：{(save.stderr or '').strip()[:300]}")
        # 顺手把真 digest 取回来 —— 这正是 OP-14/OP-52 要的那个值
        inspect = subprocess.run(
            ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", ref],
            capture_output=True, text=True,
        )
        saved.append({
            "ref": ref,
            "tar": tar.name,
            "repo_digest": (inspect.stdout or "").strip() or None,
        })

    return {
        "saved": saved,
        "unresolved": unresolved,
        "complete": not unresolved,
    }


def cmd_repo(out: Path, args: argparse.Namespace) -> dict[str, object]:
    """源码 tar（`git archive HEAD`）。

    用 `git archive` 而不是 `tar czf .`：后者会把 `.venv` / `dist/` /
    `__pycache__` / 未提交的脏文件全打进去，而内网拿到的应该是
    **某个明确 commit 的内容**。`git archive` 只打已追踪文件，
    并且 commit sha 就是那份源码的身份。
    """
    del args
    repo_dir = out / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    if head.returncode != 0:
        raise RuntimeError(f"取不到 HEAD：{(head.stderr or '').strip()[:300]}")
    sha = head.stdout.strip()
    tar = repo_dir / f"rednote-production-{sha[:12]}.tar.gz"
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", "--format=tar.gz",
         "--output", str(tar), "HEAD"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git archive 失败：{(proc.stderr or '').strip()[:300]}")

    dirty = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "-c", "core.quotepath=false",
         "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True,
    )
    dirty_lines = [line for line in dirty.stdout.splitlines() if line.strip()]
    _log(f"  源码 tar {tar.name}（HEAD={sha[:12]}，工作树脏文件 {len(dirty_lines)} 个）")
    return {
        "head": sha,
        "tar": tar.name,
        # 脏文件数**写进清单**：源码 tar 打的是 HEAD，不含未提交改动。
        # 若这个数不是 0，拿到包的人必须知道"包里没有那些改动"。
        "worktree_dirty_entries": len(dirty_lines),
    }


def cmd_sbom(out: Path, args: argparse.Namespace) -> dict[str, object]:
    """委托 `tools/ops/gen_sbom.py`，**不在本模块里重写一份生成逻辑**。"""
    sbom_dir = out / "sbom"
    sbom_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    target = sbom_dir / f"rednote-sbom-{stamp}.cdx.json"
    cmd = [
        sys.executable, str(REPO_ROOT / "tools" / "ops" / "gen_sbom.py"),
        "--wheelhouse", str(out / "wheelhouse"),
        "--out", str(target),
    ]
    if args.allow_missing_runtime_facts:
        cmd.append("--allow-missing-runtime-facts")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gen_sbom.py 失败：{(proc.stderr or proc.stdout)[-1500:]}")
    _log(f"  SBOM {target.name}")
    return {"sbom": f"sbom/{target.name}"}


def cmd_manifest(out: Path, args: argparse.Namespace) -> dict[str, object]:
    """SHA256SUMS.txt + MANIFEST.json，并**现场重算一遍自校验**。

    为什么写完要立刻再算一遍：写清单和清单正确是两件事。
    `backup_pg.py` 的 manifest 立的是同一条规 —— 校验值要能被独立重算出来，
    否则截断的文件与完好的文件在磁盘上长得一样。
    """
    sums_path = out / "SHA256SUMS.txt"
    manifest_path = out / "MANIFEST.json"

    files: list[Path] = []
    for path in sorted(out.rglob("*")):
        if not path.is_file():
            continue
        if path.name in ("SHA256SUMS.txt", "MANIFEST.json"):
            continue
        files.append(path)
    if not files:
        raise RuntimeError(
            f"{out} 下没有可校验的文件 —— 空目录与『打包完成』同形，拒绝写清单"
        )

    lines = []
    entries: list[dict[str, object]] = []
    for path in files:
        rel = path.relative_to(out).as_posix()
        digest = sha256_file(path)
        # GNU sha256sum / BSD shasum -a 256 共同认得的格式："<hex>  <path>"
        lines.append(f"{digest}  {rel}")
        entries.append({"path": rel, "sha256": digest, "size": path.stat().st_size})
    sums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    manifest = {
        "schema": "rednote.offline-bundle/1",
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "built_on": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        # ⚠️ 构建机 != 目标机。built_on 是**构建环境**的事实；
        #    target_platform 是**声明**，其中三项全是 [待核实]。
        #    两者分开写，是为了不让"本机构建成功"被读成"目标机装得上"。
        "target_platform": {
            "python_version": args.python_version,
            "platform_tags": list(args.platform_tag),
            "libc": None,
            "os_distribution": None,
            "pending_op": "[待申领]",
            "pending_reason": (
                "目标平台三元组（CPU 架构 / libc / OS 发行版）未定：硬件采购未落地，"
                "可能是信创 ARM。platform_tags 当前是 x86_64 口径的**演示值**，"
                "若目标是 ARM 则 wheelhouse 全部需重建。"
            ),
        },
        "steps": args.step_results,
        "files": entries,
        "file_count": len(entries),
        "total_bytes": sum(int(e["size"]) for e in entries),  # type: ignore[arg-type]
        "_读法": [
            "本清单只能读作：这些文件在构建机上存在、校验值可重算。",
            "它**不能**读作：内网装得上、断网起得来、镜像版本已经锁死到 digest。",
            "断网安装的验证归内网窗口，见 deploy/prod/README.md §5。",
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # ---- 自校验：逐行重算，比对 ----
    bad: list[str] = []
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        expected, rel = line.split("  ", 1)
        actual = sha256_file(out / rel)
        if actual != expected:
            bad.append(rel)
    if bad:
        raise RuntimeError(f"清单自校验失败（{len(bad)} 个文件对不上）：{bad[:5]}")

    _log(f"  SHA256SUMS.txt {len(entries)} 条 + MANIFEST.json（自校验通过）")
    return {"file_count": len(entries), "total_bytes": manifest["total_bytes"]}


#: 子命令分发表。`SUBCOMMANDS` 与本表的键做**全等**对账（判据 S-selfcheck）。
DISPATCH: Final[dict[str, Callable[[Path, argparse.Namespace], dict[str, object]]]] = {
    "wheelhouse": cmd_wheelhouse,
    "images": cmd_images,
    "repo": cmd_repo,
    "sbom": cmd_sbom,
    "manifest": cmd_manifest,
}

#: `all` 的执行顺序。manifest 必须最后（它要校验前面所有产物）。
ALL_ORDER: Final[tuple[str, ...]] = ("wheelhouse", "images", "repo", "sbom", "manifest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建内网离线安装包（WP46 ②）")
    parser.add_argument("subcommand", choices=list(SUBCOMMANDS))
    parser.add_argument("--out", type=Path, default=None,
                        help="产物目录；默认 dist/bundle（固定路径，重跑即覆盖）")
    parser.add_argument("--python-version", default=DEFAULT_PYTHON_VERSION)
    parser.add_argument("--platform-tag", action="append", default=None,
                        help="可重复；默认三个 manylinux x86_64 tag（见模块 docstring 的实测说明）")
    parser.add_argument("--allow-unresolved", action="store_true",
                        help="images：允许跳过 pin_state=unresolved 的条目（产出的包不完整）")
    parser.add_argument("--allow-missing-runtime-facts", action="store_true")
    args = parser.parse_args(argv)

    if args.platform_tag is None:
        args.platform_tag = list(DEFAULT_PLATFORM_TAGS)

    out = args.out or DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)

    args.step_results = {}
    steps = ALL_ORDER if args.subcommand == "all" else (args.subcommand,)
    for step in steps:
        _log(f"步骤 {step}")
        try:
            args.step_results[step] = DISPATCH[step](out, args)
        except (RuntimeError, ValueError, FileNotFoundError, OSError) as exc:
            # 失败即退出，**不继续**：后面的步骤会在不完整的输入上产出
            # "看起来成功"的清单，那比失败更坏。
            print(f"[bundle] 步骤 {step} 失败：{exc}", file=sys.stderr)
            return 1

    _log(f"完成：{out}")
    print(json.dumps(args.step_results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
