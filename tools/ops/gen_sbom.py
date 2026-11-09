#!/usr/bin/env python
"""SBOM 生成（WP46 ③）：CycloneDX 1.5 JSON，**纯标准库**。

## 为什么不用 syft / cyclonedx-py / trivy

两条理由，第一条是实测，第二条是结构性的：

1. **本机四个工具全部缺失**（2026-10-18 实测
   `command -v syft cyclonedx-py pip-audit trivy` → 零命中）；内网断网更装不了。
2. 更要紧的：引入任何一个 Python 侧的 SBOM 库，都要往
   `services/backend/pyproject.toml` 的 `dependencies` 里加一条。那个文件
   自述「本列表是**项目需要哪些第三方包**的唯一权威」，被
   `tools/quality/check_dependency_declarations.py` 守在 CI 门 3 ——
   也就是说「为了生成 SBOM 而改变被 SBOM 描述的那份依赖清单」。
   ⇒ **设计约束而非巧合**：本模块只 import 标准库。
   核对方式：`git diff --stat services/backend/pyproject.toml` 必须为空。

## SBOM 覆盖哪三层、**不**覆盖哪一层

| 层 | 事实来源 | 覆盖 |
|---|---|---|
| L-A Python 直接依赖 | `pyproject.toml` 的 `[project.dependencies]` + `optional-dependencies` 各组 | ✅ |
| L-B Python 传递闭包 | `dist/wheelhouse/*.whl` 的**文件名**（pip 真解析出来的结果） | ✅ |
| L-C 镜像引用层 | `deploy/prod/images.lock.json` | ✅ |
| L-D **镜像内 OS 包层** | 需 syft/trivy 解析镜像层 + 各发行版包数据库 | ❌ **未覆盖** |

L-D 不是忘了，是**显式不做**（自己写"解析镜像层 tar + dpkg/rpm 数据库"是另一个
工作包的体量）。它与另外三条未覆盖项一起写进 `metadata.properties`
（见 `UNCOVERED_LAYERS`），并由 `tools/quality/check_sbom_coverage.py` 的
`sbom:uncovered-layers-declared` 与脚本侧常量做**双向对账** ——
"没覆盖"必须是一句**会被判据核对**的话，否则它就只是静默漏检。

## 为什么 L-B 必须取 wheelhouse 文件名，而不是把 pyproject 抄一遍

`pyproject.toml` 只有**直接**依赖（本机实测 19 条声明）。`fastapi==0.115.0`
会拖进 starlette / anyio / sniffio / idna / typing-extensions…
本机实测：19 条声明 → wheelhouse **51 个 wheel**。
抄 pyproject 就是"列了几个就算数"，而内网装机缺的恰恰是传递的那些
（`docs/m3/工程债-依赖声明一致性说明.md:30` 记过同族实测：缺 cryptography 时
`alembic upgrade head` 直接 ModuleNotFoundError 崩掉）。
wheelhouse 的文件名集合是 **pip 的真实解析结果**，人改不动它。

判据 `sbom:closure-strictly-larger` 断言 `len(闭包) > len(直接依赖)` ——
**严格大于**而不是相等。因为若有人把 wheelhouse 构建成 `pip download --no-deps`，
"闭包 == wheelhouse"这条仍然双向相等、全绿，而闭包根本没取到
（与 `0 == 0` 恒真同族）。

## hashes 是**现场算的**，不是抄来的

每个 pypi 组件的 `hashes[{alg:"SHA-256"}]` 由本模块对那个 `.whl` 文件
`hashlib.sha256()` 现场计算。判据侧（S7）**再算一遍**比对 ——
"有这个字段"与"这个字段的值是对的"是两件事。

## 运行时事实取真库，不抄文档

PostgreSQL 版本与 pgvector 版本走 `psql` 的只读查询（`select version()`、
`select extname, extversion from pg_extension`），**不抄** ADR-11 里写的
"17 主线 + pgvector 0.8 系列"。本机实测读数：PostgreSQL **17.10**、vector **0.8.6**
—— 与 ADR 写的通道一致，但那是**核对结果**，不是前提。
库不可达时本模块**报错退出**；`--allow-missing-runtime-facts` 可以越过，
但那样 SBOM 里会写 `runtime_facts_source = unavailable`，而判据 S8 **拒绝**
这个取值 ⇒ 降级路径存在但走不通，不会变成一条静默的绿。

## 本机验证边界

本模块产出的 SBOM 在本机**可生成、可自校验**。它**不能**读作
"SBOM 把所有依赖都列了"（L-D 未覆盖），也不能读作"这些版本是内网要装的版本"
（目标平台三元组 `[待核实]`，见 `build_offline_bundle.py` 的同名小节）。

## 用法

    python tools/ops/gen_sbom.py                      # 读默认路径，落 dist/sbom/
    python tools/ops/gen_sbom.py --out /tmp/x.cdx.json
    python tools/ops/gen_sbom.py --pyproject <副本> --wheelhouse <目录>   # 反向验证用
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DEFAULT_PYPROJECT: Final[Path] = REPO_ROOT / "services" / "backend" / "pyproject.toml"
DEFAULT_WHEELHOUSE: Final[Path] = REPO_ROOT / "dist" / "bundle" / "wheelhouse"
DEFAULT_IMAGES_LOCK: Final[Path] = REPO_ROOT / "deploy" / "prod" / "images.lock.json"
DEFAULT_SBOM_DIR: Final[Path] = REPO_ROOT / "dist" / "bundle" / "sbom"

#: CycloneDX 规范版本。1.5 而不是 1.6：1.5 的 `purl` + `hashes` 两个字段
#: 已经把"装什么 + 校验什么"说全，而本包不需要 1.6 新增的那些声明字段。
SPEC_VERSION: Final[str] = "1.5"

#: 本模块版本号。SBOM 里要写"是谁生成的"，而"谁"包含版本 ——
#: 否则将来口径变了，旧 SBOM 看不出它是按哪版规则算的。
TOOL_VERSION: Final[str] = "1.0.0"

#: **显式未覆盖的层**。每条两样：键 + 为什么不覆盖（不是"来不及"）。
#:
#: ⚠️ 这份清单是人写的覆盖范围声明（铁律 14）。它由
#: `tools/quality/check_sbom_coverage.py` 的同名常量做**双向对账** ——
#: 这里少一条、那里多一条，`sbom:uncovered-layers-declared` 立刻报红。
#: 两处都有是刻意的：SBOM 是**制品**，判据是**期望**，期望不能从制品里读
#: （否则是自己断言自己）。
UNCOVERED_LAYERS: Final[tuple[tuple[str, str], ...]] = (
    (
        "image-os-packages",
        "镜像内 OS 包层（dpkg/rpm 级清单）：需 syft/trivy 解析镜像层与发行版包数据库，"
        "本机四个工具实测全缺、内网也装不了；自己写是另一个工作包的体量。",
    ),
    (
        "frontend-assets",
        "前端静态资产：本仓当前无前端产物目录（apps/ 不存在），demo 在仓库外；"
        "且实测 demo 依赖 cdnjs 与 Google Fonts 外链，断网会降级 —— 处置归前端赛道。",
    ),
    (
        "ocr-model-weights",
        "OCR（PaddleOCR）版本与模型权重：OP-137 未定版本与权重来源，离线包体积未知。",
    ),
    (
        "dynamic-imports",
        "运行期动态导入（importlib / __import__ 构造出来的模块名）："
        "wheelhouse 覆盖的是 pip 解析出的发行包，解析不出"
        "「代码在运行时才拼出模块名」这一类；那要靠 check_dependency_declarations.py 的 AST 扫描兜。",
    ),
)

#: wheel 文件名形态（PEP 427）：name-version(-build)?-pytag-abitag-platformtag.whl
_WHEEL_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<name>[^-]+)-(?P<version>[^-]+)"
    r"(?:-(?P<build>\d[^-]*))?"
    r"-(?P<pytag>[^-]+)-(?P<abitag>[^-]+)-(?P<plat>[^-]+)\.whl$"
)

#: 需求串里把发行名切出来：`uvicorn[standard]==0.30.6` → `uvicorn`
_REQ_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^([A-Za-z0-9._-]+)")


def normalize_dist_name(raw: str) -> str:
    """PEP 503 规范化：小写 + `-`/`_`/`.` 连续串统一成单个 `-`。

    为什么必须规范化：同一个包在三处有三种写法 —— pyproject 写
    `prometheus_client`、wheel 文件名写 `prometheus_client`、purl 里该是
    `prometheus-client`。不规范化就会把同一个包算成两个，让集合对账假红。
    """
    return re.sub(r"[-_.]+", "-", raw).lower()


def read_direct_requirements(pyproject: Path) -> dict[str, str]:
    """从 pyproject 读**直接依赖**：规范化发行名 → 原始需求串。

    三组全读（`dependencies` + `optional-dependencies` 的每一组），
    **不**只读 `dependencies`：`[m0]` 组的 boto3/botocore 也要进离线包
    （`tools/m0/wp05_smoke/smoke.py` 靠它跑 MinIO 连通性冒烟），
    漏掉它们就是"内网装完 M0 探针跑不了"。

    组名清单由**磁盘决定**（遍历 optional-dependencies 的键），不写死
    `("dev", "m0")` —— 将来新增一组而这里没跟上，就是静默漏覆盖。
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project", {})
    groups: list[list[str]] = [list(project.get("dependencies", []))]
    for value in (project.get("optional-dependencies") or {}).values():
        groups.append(list(value))

    out: dict[str, str] = {}
    for group in groups:
        for req in group:
            match = _REQ_NAME_RE.match(req.strip())
            if not match:
                raise ValueError(f"需求串切不出发行名：{req!r}")
            out[normalize_dist_name(match.group(1))] = req.strip()
    return out


def read_wheelhouse(wheelhouse: Path) -> list[dict[str, object]]:
    """扫 wheelhouse，逐个 wheel 解析文件名 + **现场算 sha256**。

    **空目录/不存在时抛，不返回空列表。** 理由与
    `tools/quality/check_alerting_rules.py:47-48` 立的规同族：返回空列表会让
    下游的集合对账退化成 `∅ == ∅` 恒真 —— "跑不了所以绿"等于没有判据。
    """
    if not wheelhouse.is_dir():
        raise FileNotFoundError(
            f"无法构造对账：wheelhouse 不存在 {wheelhouse} —— Python 传递闭包取不到，"
            f"先跑 `python tools/ops/build_offline_bundle.py wheelhouse`"
        )
    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels:
        raise FileNotFoundError(
            f"无法构造对账：wheelhouse 为空 {wheelhouse} —— 空目录与『闭包已取到』同形，拒绝生成 SBOM"
        )

    out: list[dict[str, object]] = []
    for path in wheels:
        match = _WHEEL_RE.match(path.name)
        if not match:
            raise ValueError(f"wheel 文件名不符合 PEP 427：{path.name}")
        out.append(
            {
                "filename": path.name,
                "name": normalize_dist_name(match.group("name")),
                "version": match.group("version"),
                "python_tag": match.group("pytag"),
                "abi_tag": match.group("abitag"),
                "platform_tag": match.group("plat"),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    return out


def sha256_file(path: Path) -> str:
    """分块算 sha256。与 `tools/ops/backup_pg.py` 的 manifest 同口径（小写 hex）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_images_lock(images_lock: Path) -> dict[str, object]:
    """读镜像锁定清单。它是镜像侧的**唯一权威**（compose 只写 ${IMAGE_*}）。"""
    if not images_lock.is_file():
        raise FileNotFoundError(f"镜像清单不存在：{images_lock}")
    data = json.loads(images_lock.read_text(encoding="utf-8"))
    if not data.get("images"):
        raise ValueError(f"镜像清单的 images 为空：{images_lock} —— 空清单与『已登记』同形")
    return data


def _psql_path() -> str:
    """`psql` 的绝对路径。**委托给 `drill_db.pg_bin()`，本模块不自己回退。**

    `tools/ops/backup_pg.py:70-86` 的 docstring 记过这个坑：首版把
    `/opt/homebrew/opt/postgresql@17/bin` 写死，本机通、CI 必红。
    回退三级（`REDNOTE_PG_BIN` → `which` → Homebrew）**只允许存在一份**。

    延迟导入而非模块级：与 `backup_pg._pg_bin()` 同一处理 —— 本模块有两种
    被加载姿势（直接跑 / 被 tools/quality 下的脚本 `sys.path.insert` 后 import）。
    """
    sys.path.insert(0, str(REPO_ROOT / "tools" / "ops"))
    import drill_db

    return str(Path(drill_db.pg_bin()) / "psql")


def _psql_dsn() -> str:
    """只读查询用的 DSN。与 `drill_db.base_dsn()` 同一优先级，但转成 libpq 口径。

    `base_dsn()` 返回的是 SQLAlchemy 口径（`postgresql+psycopg://`），
    而 libpq 系列工具**不认** `+psycopg` 前缀（`drill_db` docstring 记的 ISS-174 同款坑）。
    """
    raw = os.environ.get(
        "REDNOTE_TEST_DATABASE_URL",
        os.environ.get("REDNOTE_DATABASE_URL", "postgresql://localhost:5432/postgres"),
    )
    return raw.replace("postgresql+psycopg://", "postgresql://", 1)


def read_runtime_facts(allow_missing: bool) -> dict[str, object]:
    """PG 与 pgvector 版本 —— **真库只读查询**，不抄文档。

    只读两条 SQL（`select version()` / `select extname, extversion from pg_extension`）：
    不建库、不写入、不跑迁移 ⇒ 不触碰共享测试库的静止要求。
    """
    queries = (
        ("postgresql_version", "select version()"),
        ("extensions", "select extname || '=' || extversion from pg_extension order by 1"),
    )
    psql = _psql_path()
    dsn = _psql_dsn()
    facts: dict[str, object] = {}
    for key, sql in queries:
        proc = subprocess.run(
            [psql, dsn, "-At", "-c", sql],
            capture_output=True,
            text=True,
            env=dict(os.environ, NO_PROXY="*"),
        )
        if proc.returncode != 0:
            if allow_missing:
                return {
                    "runtime_facts_source": "unavailable",
                    "error": (proc.stderr or "").strip()[:300],
                }
            raise RuntimeError(
                f"运行时事实取不到（{key}）：{(proc.stderr or '').strip()[:300]}\n"
                f"  SBOM 的 PG/pgvector 版本只能来自真库实测。若确要在无库环境生成，"
                f"加 --allow-missing-runtime-facts —— 但那样判据 sbom:runtime-facts-derived 会报红。"
            )
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        facts[key] = lines[0] if key == "postgresql_version" else lines
    facts["runtime_facts_source"] = "live-query"
    facts["queries"] = [sql for _, sql in queries]
    return facts


def build_sbom(
    *,
    pyproject: Path,
    wheelhouse: Path,
    images_lock: Path,
    allow_missing_runtime_facts: bool,
) -> dict[str, object]:
    """装配 CycloneDX 1.5 文档。"""
    direct = read_direct_requirements(pyproject)
    wheels = read_wheelhouse(wheelhouse)
    lock = read_images_lock(images_lock)
    runtime = read_runtime_facts(allow_missing_runtime_facts)

    app_meta = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})

    components: list[dict[str, object]] = []
    for wheel in wheels:
        name = str(wheel["name"])
        version = str(wheel["version"])
        purl = f"pkg:pypi/{name}@{version}"
        components.append(
            {
                "type": "library",
                "bom-ref": purl,
                "name": name,
                "version": version,
                "purl": purl,
                "hashes": [{"alg": "SHA-256", "content": wheel["sha256"]}],
                "properties": [
                    {"name": "rednote:layer", "value": "L-B" if name not in direct else "L-A"},
                    {"name": "rednote:direct", "value": "true" if name in direct else "false"},
                    {"name": "rednote:requirement", "value": direct.get(name, "")},
                    {"name": "rednote:wheel-filename", "value": wheel["filename"]},
                    {"name": "rednote:wheel-python-tag", "value": wheel["python_tag"]},
                    {"name": "rednote:wheel-abi-tag", "value": wheel["abi_tag"]},
                    {"name": "rednote:wheel-platform-tag", "value": wheel["platform_tag"]},
                    {"name": "rednote:wheel-size-bytes", "value": str(wheel["size"])},
                ],
            }
        )

    for image in lock["images"]:  # type: ignore[index]
        ref = str(image["ref"])
        digest = image.get("digest")
        # purl 的 docker 形态：有 digest 用 digest，没有就用 tag。
        # **不**在没有 digest 时编一个 —— 那正是 OP-14 备注禁止的事。
        purl = f"pkg:docker/{image['name']}@{digest}" if digest else f"pkg:docker/{image['name']}?tag={image['tag']}"
        components.append(
            {
                "type": "container",
                "bom-ref": f"image:{ref}",
                "name": str(image["name"]),
                "version": str(image["tag"]),
                "purl": purl,
                "properties": [
                    {"name": "rednote:layer", "value": "L-C"},
                    {"name": "rednote:image-ref", "value": ref},
                    {"name": "rednote:pin-state", "value": str(image["pin_state"])},
                    {"name": "rednote:pending-op", "value": str(image.get("pending_op") or "")},
                    {"name": "rednote:env-var", "value": str(image["env_var"])},
                    {"name": "rednote:component", "value": str(image["component"])},
                ],
            }
        )

    properties: list[dict[str, str]] = [
        {"name": "rednote:coverage:layers", "value": "L-A,L-B,L-C"},
        {"name": "rednote:coverage:not-covered", "value": ",".join(k for k, _ in UNCOVERED_LAYERS)},
        {"name": "rednote:source:pyproject", "value": _rel(pyproject)},
        {"name": "rednote:source:wheelhouse", "value": _rel(wheelhouse)},
        {"name": "rednote:source:images-lock", "value": _rel(images_lock)},
        {"name": "rednote:runtime:facts-source", "value": str(runtime.get("runtime_facts_source"))},
        {"name": "rednote:count:direct-requirements", "value": str(len(direct))},
        {"name": "rednote:count:wheelhouse-wheels", "value": str(len(wheels))},
        {"name": "rednote:count:images", "value": str(len(lock["images"]))},  # type: ignore[arg-type]
    ]
    for key, reason in UNCOVERED_LAYERS:
        properties.append({"name": f"rednote:uncovered-layer:{key}", "value": reason})
    if runtime.get("runtime_facts_source") == "live-query":
        properties.append(
            {"name": "rednote:runtime:postgresql", "value": str(runtime["postgresql_version"])}
        )
        for ext in runtime["extensions"]:  # type: ignore[union-attr]
            properties.append({"name": "rednote:runtime:extension", "value": str(ext)})
        for sql in runtime["queries"]:  # type: ignore[union-attr]
            properties.append({"name": "rednote:runtime:query", "value": str(sql)})
    else:
        properties.append(
            {"name": "rednote:runtime:error", "value": str(runtime.get("error", ""))}
        )

    return {
        "bomFormat": "CycloneDX",
        "specVersion": SPEC_VERSION,
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tools": [
                {
                    "vendor": "rednote",
                    "name": "tools/ops/gen_sbom.py",
                    "version": TOOL_VERSION,
                }
            ],
            "component": {
                "type": "application",
                "bom-ref": f"pkg:pypi/{normalize_dist_name(str(app_meta.get('name', 'rednote-backend')))}@{app_meta.get('version', '0')}",
                "name": str(app_meta.get("name", "rednote-backend")),
                "version": str(app_meta.get("version", "0")),
                "description": str(app_meta.get("description", "")),
            },
            "properties": properties,
        },
        "components": components,
    }


def _rel(path: Path) -> str:
    """相对仓库根的路径；落在仓库外时原样返回（反向验证用的临时副本就在仓库外）。"""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 CycloneDX 1.5 SBOM（纯标准库）")
    parser.add_argument("--pyproject", type=Path, default=DEFAULT_PYPROJECT)
    parser.add_argument("--wheelhouse", type=Path, default=DEFAULT_WHEELHOUSE)
    parser.add_argument("--images-lock", type=Path, default=DEFAULT_IMAGES_LOCK)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="输出路径；默认 dist/sbom/rednote-sbom-<ts>.cdx.json",
    )
    parser.add_argument(
        "--allow-missing-runtime-facts",
        action="store_true",
        help="库不可达时仍生成（SBOM 里会写 unavailable，判据 S8 会因此报红）",
    )
    args = parser.parse_args(argv)

    try:
        sbom = build_sbom(
            pyproject=args.pyproject,
            wheelhouse=args.wheelhouse,
            images_lock=args.images_lock,
            allow_missing_runtime_facts=args.allow_missing_runtime_facts,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1

    out = args.out
    if out is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        out = DEFAULT_SBOM_DIR / f"rednote-sbom-{stamp}.cdx.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sbom, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    libs = sum(1 for c in sbom["components"] if c["type"] == "library")  # type: ignore[index,union-attr]
    containers = sum(1 for c in sbom["components"] if c["type"] == "container")  # type: ignore[index,union-attr]
    print(f"OK: wrote {out}")
    print(f"  components: {libs} library(L-A/L-B) + {containers} container(L-C)")
    print(f"  未覆盖层（显式声明）: {', '.join(k for k, _ in UNCOVERED_LAYERS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
