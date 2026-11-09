#!/usr/bin/env python3
"""SBOM 覆盖判据（WP46 ③，**本包核心**）。

## 「SBOM 覆盖了全部依赖」这句话必须先被拆开才能判

它拆成四层，其中三层能本机判，第四层**显式不覆盖**：

| 层 | 事实来源（磁盘/运行时） | 本机能判？ |
|---|---|---|
| L-A **Python 直接依赖** | `services/backend/pyproject.toml` 的 `[project.dependencies]` + optional 各组 | ✅ |
| L-B **Python 传递闭包** | `dist/bundle/wheelhouse/*.whl` 的文件名（pip 真解析出来的结果） | ✅ |
| L-C **镜像引用层** | `deploy/prod/images.lock.json` | ✅ |
| L-D **镜像内 OS 包层** | 需 syft/trivy（本机实测四个工具零命中） | ❌ **不做，显式登记** |

⇒ 本判据全绿只能读作「SBOM 覆盖 L-A/L-B/L-C 三层，且这三层能被机械对账」。
  **不能**读作 SBOM 把所有依赖都列了 —— L-D 没覆盖，这件事由
  `sbom:uncovered-layers-declared` 当成一条**判据**钉着，而不是写在某页文档末尾。

## 本节最关键的一条：`sbom:closure-strictly-larger`

`sbom:python-closure-equals-wheelhouse` 断言「SBOM 的 (name, version) 集合」与
「wheelhouse 的 wheel 文件名解析结果」**双向相等**。这条很有用，但它
**单独使用时有一个致命的恒真路径**：

若有人把 wheelhouse 构建成 `pip download --no-deps`，那么 wheelhouse 里只有
直接依赖、SBOM 也只有直接依赖 ⇒ **双向相等，全绿**，而传递闭包根本没取到。
内网装机缺的恰恰是传递的那些（`docs/m3/工程债-依赖声明一致性说明.md:30` 记过
实测：缺 `cryptography` 时 `alembic upgrade head` 直接 ModuleNotFoundError 崩掉）。

这与 WP47④ 队列表那次 `0 == 0` 完全同族（`docs/m5/WP47-演练记录.md:167-180`：
备份把队列内容全丢了照样报"行数相等"）。

⇒ 必须另有一条断言**严格大于**：`len(闭包) > len(直接依赖)`。
本机实测读数：**19 条直接声明 → 51 个 wheel**。
反向验证 R2 专门造一个 `--no-deps` 形态的 wheelhouse，证明这条真会报红。

## 第二关键：`sbom:hashes-recomputed` 现场重算

「有 `hashes[SHA-256]` 这个字段」与「这个字段的值是对的」是两件事。
本判据对 wheelhouse 里每个 `.whl` **重新算一遍** sha256，与 SBOM 里写的逐个比。
**不读清单里写的数** —— 读它等于自己断言自己。

## fail-closed 纪律（不许"跑不了所以绿"）

- **wheelhouse 不存在或为空 ⇒ FAIL 并打印「无法构造对账」**，不许 SKIP。
  `check_alerting_rules.py:47-48` 已立此规：「一个『跑不了所以绿』的判据等于没有判据」。
  本包的具体风险：CI runner 上若不构建 wheelhouse，S3/S3a/S7 会**在 CI 上永远不生效**
  而输出是绿的。
- **SBOM 文件不存在 ⇒ FAIL**，同理。
- 判据条数 `== EXPECTED_CHECKS` **全等**断言（ISS-153）。

## ⚠️ 留给主会话拍板的一项（照 `ci.yml:157-167` 的纪律：理由必须写下来）

**CI 侧要不要真构建 wheelhouse？**

- 真构建 ⇒ 门 3 变慢（本机实测 51 个 wheel / 64 MB / 38 秒，CI 网络条件未知）
  且**依赖网络**（与「CI 不出网」若有冲突则此路不通）；
- 不构建 ⇒ 本脚本在 CI 上必红（fail-closed 的必然结果），等于门 3 被这条堵死。

两条路都要人拍板，而**无论选哪条都必须写进本 docstring** ——
不写就是 ISS-95 形态的空白（`ci.yml:157-167` 对 `rerun_all_evidence.py`
不进门 3 的理由就是这样写下来的）。本包**不自己决定**，因为它牵涉 CI 时间预算。

## 已知盲区（不假装覆盖）

- **不校验许可证合规**：CycloneDX 的 `licenses` 字段本判据不产出也不核对。
  许可证链是 SPDX 的强项，本项目当前无该需求（方案 §1.1）。
- **不做漏洞扫描**：`pip-audit` 需联网拉漏洞库，与 ADR-10 受控出网冲突，
  且 `docs/m3/工程债-依赖声明一致性说明.md:229` 已判定越界。
- **不校验 wheel 内容与声明的包名一致**：只核对文件名解析结果。
  伪造一个文件名正确但内容是别的包的 wheel，本判据抓不到（sha256 会变，
  但那只能证明"内容变了"，不能证明"内容是什么"）。

## 用法

    python tools/quality/check_sbom_coverage.py
    python tools/quality/check_sbom_coverage.py --sbom <path> --wheelhouse <dir> \\
        --pyproject <path> --images-lock <path>        # 反向验证用
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Final

REPO: Final[Path] = Path(__file__).resolve().parents[2]
DEFAULT_PYPROJECT: Final[Path] = REPO / "services" / "backend" / "pyproject.toml"
DEFAULT_WHEELHOUSE: Final[Path] = REPO / "dist" / "bundle" / "wheelhouse"
DEFAULT_IMAGES_LOCK: Final[Path] = REPO / "deploy" / "prod" / "images.lock.json"
DEFAULT_SBOM_DIRS: Final[tuple[Path, ...]] = (
    REPO / "dist" / "bundle" / "sbom",
    REPO / "evidence" / "m5",
)

#: CycloneDX 1.5 的必填字段（顶层用 `.` 表示嵌套）。
#:
#: ⚠️ 这是**人写的覆盖范围**（铁律 14）⇒ 配 `sbom:required-fields-selfcheck`：
#: 断言本常量与「实际被 S1 走过的字段集合」逐项相等。
#: 少校验一个字段这件事，必须有一条判据会红，否则它静默发生。
_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "bomFormat",
    "specVersion",
    "serialNumber",
    "version",
    "metadata.timestamp",
    "metadata.tools",
    "metadata.component",
    "metadata.properties",
    "components",
)

#: **显式未覆盖的层**。与 `tools/ops/gen_sbom.py::UNCOVERED_LAYERS` 的键集合
#: 做**双向对账**（`sbom:uncovered-layers-declared`）。
#:
#: ⚠️ 为什么两处各写一份而不是 import 过来：SBOM 是**制品**，本常量是**期望**。
#: 从制品里读期望就是自己断言自己（纪律第 6 条）。两处脱钩时判据报红，
#: 这正是它要抓的东西 —— 照 `tools/m1/audit_consistency.py:362-366` 的
#: `_EVIDENCE_SUFFIX_EXEMPT`：豁免必须逐条给理由，且"每个出现过的项都必须
#: 落在白名单∪豁免里"。
_UNCOVERED_LAYERS: Final[frozenset[str]] = frozenset(
    {"image-os-packages", "frontend-assets", "ocr-model-weights", "dynamic-imports"}
)

#: 组件层标签。L-D 刻意**不在**这个集合里（它是未覆盖层，不该有组件）。
_LAYERS: Final[frozenset[str]] = frozenset({"L-A", "L-B", "L-C"})

_PURL_RE: Final[re.Pattern[str]] = re.compile(r"^pkg:pypi/([a-z0-9-]+)@(.+)$")
_EXACT_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9][A-Za-z0-9._+!-]*$")
_DIGEST_REF_RE: Final[re.Pattern[str]] = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
_FLOATING_TAGS: Final[frozenset[str]] = frozenset(
    {"latest", "stable", "main", "master", "edge", "nightly", "dev", "release"}
)
_REQ_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^([A-Za-z0-9._-]+)")

#: 直接依赖条数的下界。
#:
#: ⚠️ 为什么是**下界**而不是实际值：实际值就是 pyproject 里的条数，
#: 拿它当期望值等于自己断言自己（`check_backup_restore_drill.py:110-111`
#: 的同一条纪律）。本常量只挡"pyproject 被解析成空/极少"这类退化。
#: 今日实测 19 条。
_MIN_DIRECT_REQUIREMENTS: Final[int] = 10

#: wheelhouse 条数下界。今日实测 51 个。挡的是"wheelhouse 只剩几个 wheel"的退化。
_MIN_WHEELS: Final[int] = 20

#: 判据条数。全等断言（ISS-153：`>=` 只防变少不防变多）。
#:
#: 2026-10-18：本值首版按"S1~S12 共 13 条"手写成 **13**，实跑是 **14**
#: （S1 与 S1b 被我当成一条数了）。**被脚本自己的全等断言当场抓出** ——
#: 与 `check_backup_restore_drill.py:122-131` 记的那两次完全同形，
#: 说明这条"条数全等"断言确实在干活，不是摆设。
EXPECTED_CHECKS: Final[int] = 14


def _normalize(raw: str) -> str:
    """PEP 503 规范化。三处写法不同（pyproject `prometheus_client`、
    wheel 文件名 `prometheus_client`、purl `prometheus-client`），
    不规范化会把同一个包算成两个 ⇒ 集合对账假红。"""
    return re.sub(r"[-_.]+", "-", raw).lower()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dig(obj: object, dotted: str) -> object:
    """按 `a.b.c` 取值；任一层缺失返回 `None`。"""
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _resolve_sbom(explicit: Path | None) -> Path | None:
    """定位 SBOM：显式参数优先，否则按 `DEFAULT_SBOM_DIRS` 顺序取各目录里最新的一份。

    `dist/bundle/sbom/` 在前、`evidence/m5/` 在后：前者是**刚构建的制品**，
    后者是**入库的快照**。顺序反了会让"改了代码重新构建"之后判据还在看旧快照。
    """
    if explicit is not None:
        return explicit if explicit.is_file() else None
    for directory in DEFAULT_SBOM_DIRS:
        if not directory.is_dir():
            continue
        candidates = sorted(directory.glob("*.cdx.json"))
        if candidates:
            return candidates[-1]
    return None


def _direct_requirements(pyproject: Path) -> dict[str, str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project", {})
    groups: list[list[str]] = [list(project.get("dependencies", []))]
    for value in (project.get("optional-dependencies") or {}).values():
        groups.append(list(value))
    out: dict[str, str] = {}
    for group in groups:
        for req in group:
            match = _REQ_NAME_RE.match(req.strip())
            if match:
                out[_normalize(match.group(1))] = req.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SBOM 覆盖判据（WP46）")
    parser.add_argument("--sbom", type=Path, default=None)
    parser.add_argument("--wheelhouse", type=Path, default=DEFAULT_WHEELHOUSE)
    parser.add_argument("--pyproject", type=Path, default=DEFAULT_PYPROJECT)
    parser.add_argument("--images-lock", type=Path, default=DEFAULT_IMAGES_LOCK)
    args = parser.parse_args(argv)

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, ok, detail))

    # ---------------- fail-closed 前置 ----------------
    sbom_path = _resolve_sbom(args.sbom)
    wheels = sorted(args.wheelhouse.glob("*.whl")) if args.wheelhouse.is_dir() else []

    blockers: list[str] = []
    if sbom_path is None:
        blockers.append(
            f"SBOM 文件不存在（查过 {[str(d.relative_to(REPO)) for d in DEFAULT_SBOM_DIRS]}）"
            f" —— 先跑 `python tools/ops/gen_sbom.py`"
        )
    if not wheels:
        blockers.append(
            f"wheelhouse 不存在或为空（{args.wheelhouse}）—— 先跑 "
            f"`python tools/ops/build_offline_bundle.py wheelhouse`"
        )
    if blockers:
        # 每一条判据都报同一个 blocker，而不是"少报几条"。
        # 理由：条数必须始终等于 EXPECTED_CHECKS，否则"条数不对"会掩盖"构造不出对账"。
        for index in range(EXPECTED_CHECKS):
            check(f"sbom:blocked-{index + 1:02d}", False,
                  "**无法构造对账**：" + "；".join(blockers)
                  + "。fail-closed —— 不许当 SKIP（一个『跑不了所以绿』的判据等于没有判据）")
        return _report(checks)

    assert sbom_path is not None
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    components = sbom.get("components", [])
    libs = [c for c in components if c.get("type") == "library"]
    containers = [c for c in components if c.get("type") == "container"]
    direct = _direct_requirements(args.pyproject)
    lock = json.loads(args.images_lock.read_text(encoding="utf-8"))

    # ---------------- S1 规范必填字段 ----------------
    visited_fields: list[str] = []
    missing_fields: list[str] = []
    for field in _REQUIRED_FIELDS:
        visited_fields.append(field)
        value = _dig(sbom, field)
        if value in (None, "", [], {}):
            missing_fields.append(field)
    spec_ok = (
        not missing_fields
        and sbom.get("bomFormat") == "CycloneDX"
        and str(sbom.get("specVersion")) == "1.5"
        and str(sbom.get("serialNumber", "")).startswith("urn:uuid:")
    )
    check("sbom:spec-fields-present", spec_ok,
          f"CycloneDX {sbom.get('specVersion')} 必填 {len(_REQUIRED_FIELDS)} 字段齐全，"
          f"serialNumber 是 urn:uuid"
          if spec_ok
          else f"缺字段={missing_fields}；bomFormat={sbom.get('bomFormat')!r}；"
               f"specVersion={sbom.get('specVersion')!r}；serial={str(sbom.get('serialNumber'))[:20]!r}")

    # ---------------- S1b 字段清单自检 ----------------
    selfcheck_ok = tuple(visited_fields) == _REQUIRED_FIELDS
    check("sbom:required-fields-selfcheck", selfcheck_ok,
          f"_REQUIRED_FIELDS 的 {len(_REQUIRED_FIELDS)} 项与实际走过的字段**逐项相等**"
          f"（防『少校验一个字段』静默发生）"
          if selfcheck_ok else f"脱钩：走过 {visited_fields} vs 声明 {list(_REQUIRED_FIELDS)}")

    # ---------------- S2 直接依赖是子集 ----------------
    sbom_pypi = {_normalize(str(c["name"])) for c in libs}
    direct_missing = sorted(set(direct) - sbom_pypi)
    s2_ok = not direct_missing and len(direct) >= _MIN_DIRECT_REQUIREMENTS
    check("sbom:python-direct-superset", s2_ok,
          f"SBOM 的 {len(sbom_pypi)} 个 pypi 组件 ⊇ pyproject 的 {len(direct)} 条直接声明"
          f"（名单**磁盘派生**，零写死包名；下界 {_MIN_DIRECT_REQUIREMENTS} 挡解析退化）"
          if s2_ok
          else f"漏 {direct_missing}；直接声明 {len(direct)} 条（下界 {_MIN_DIRECT_REQUIREMENTS}）")

    # ---------------- S3 闭包与 wheelhouse 双向相等 ----------------
    wheel_pairs: set[tuple[str, str]] = set()
    wheel_sha: dict[tuple[str, str], str] = {}
    parse_bad: list[str] = []
    for wheel in wheels:
        parts = wheel.name[: -len(".whl")].split("-")
        if len(parts) < 5:
            parse_bad.append(wheel.name)
            continue
        key = (_normalize(parts[0]), parts[1])
        wheel_pairs.add(key)
        wheel_sha[key] = _sha256_file(wheel)
    sbom_pairs = {(_normalize(str(c["name"])), str(c["version"])) for c in libs}
    s3_ok = not parse_bad and sbom_pairs == wheel_pairs and len(wheels) >= _MIN_WHEELS
    check("sbom:python-closure-equals-wheelhouse", s3_ok,
          f"(name, version) 集合双向相等：SBOM {len(sbom_pairs)} ⇄ wheelhouse {len(wheel_pairs)}"
          f"（wheel 数 {len(wheels)} >= 下界 {_MIN_WHEELS}）"
          if s3_ok
          else f"文件名解析失败={parse_bad[:3]}；SBOM-only={sorted(sbom_pairs - wheel_pairs)[:4]}；"
               f"wheelhouse-only={sorted(wheel_pairs - sbom_pairs)[:4]}")

    # ---------------- S3a 闭包**严格大于**直接依赖 ----------------
    # ★ 本脚本最关键的一条，见模块 docstring。
    strictly_larger = len(wheel_pairs) > len(direct)
    check("sbom:closure-strictly-larger", strictly_larger,
          f"传递闭包 {len(wheel_pairs)} **严格大于**直接依赖 {len(direct)} "
          f"（+{len(wheel_pairs) - len(direct)} 个传递依赖）—— "
          f"这条挡的是 wheelhouse 退化成 `pip download --no-deps`：那时 S3 仍双向相等、"
          f"全绿，而闭包根本没取到（与 0 == 0 恒真同族）"
          if strictly_larger
          else f"闭包 {len(wheel_pairs)} **未**严格大于直接依赖 {len(direct)} —— "
               f"wheelhouse 疑似按 --no-deps 构建，传递依赖没取到")

    # ---------------- S4 精确版本 + purl 形态 ----------------
    version_bad: list[str] = []
    for comp in libs:
        name = _normalize(str(comp["name"]))
        version = str(comp.get("version", ""))
        purl = str(comp.get("purl", ""))
        if not _EXACT_VERSION_RE.match(version):
            version_bad.append(f"{name}@{version!r}（非精确版本）")
        if purl != f"pkg:pypi/{name}@{version}":
            version_bad.append(f"{name}: purl={purl!r} 与 name/version 不一致")
    check("sbom:exact-versions-and-purl", not version_bad,
          f"{len(libs)} 个 pypi 组件均为精确版本（无 >= / ~= / *）且 "
          f"purl == pkg:pypi/<name>@<version>"
          if not version_bad else f"{len(version_bad)} 处：{version_bad[:5]}")

    # ---------------- S5 镜像集合 ⇄ images.lock 双向相等 ----------------
    def _prop(comp: dict[str, object], key: str) -> str:
        for item in comp.get("properties", []) or []:  # type: ignore[union-attr]
            if isinstance(item, dict) and item.get("name") == key:
                return str(item.get("value", ""))
        return ""

    sbom_refs = {_prop(c, "rednote:image-ref") for c in containers}
    lock_refs = {str(i["ref"]) for i in lock.get("images", [])}
    s5_ok = sbom_refs == lock_refs and bool(lock_refs)
    check("sbom:images-equal-lock", s5_ok,
          f"镜像引用集合双向相等：SBOM {len(sbom_refs)} ⇄ images.lock {len(lock_refs)}"
          f"（compose 侧的三方对账在 check_offline_bundle.py 的 prod:image-refs-three-way）"
          if s5_ok
          else f"SBOM-only={sorted(sbom_refs - lock_refs)}；lock-only={sorted(lock_refs - sbom_refs)}")

    # ---------------- S6 SBOM 里的镜像引用无浮动 tag ----------------
    float_bad: list[str] = []
    for comp in containers:
        ref = _prop(comp, "rednote:image-ref")
        if _DIGEST_REF_RE.match(ref):
            continue
        _, _, tag = ref.rpartition(":")
        if not tag or "/" in tag:
            float_bad.append(f"{ref}（无 tag）")
        elif tag.lower() in _FLOATING_TAGS:
            float_bad.append(f"{ref}（浮动 tag {tag}）")
    check("sbom:no-floating-image-ref", not float_bad,
          f"{len(containers)} 条镜像引用均非 latest/无 tag。"
          f"⚠️ 这条**不依赖** `docker compose config` —— 本机实测它对伪 digest 与"
          f"不存在的 registry 一律 EXIT=0（见 check_offline_bundle.py 的 docstring）"
          if not float_bad else f"{float_bad}")

    # ---------------- S7 sha256 **现场重算** ----------------
    hash_bad: list[str] = []
    hash_checked = 0
    for comp in libs:
        key = (_normalize(str(comp["name"])), str(comp["version"]))
        declared = ""
        for entry in comp.get("hashes", []) or []:
            if isinstance(entry, dict) and entry.get("alg") == "SHA-256":
                declared = str(entry.get("content", ""))
        if not declared:
            hash_bad.append(f"{key[0]}@{key[1]}（无 SHA-256 hash）")
            continue
        actual = wheel_sha.get(key)
        if actual is None:
            hash_bad.append(f"{key[0]}@{key[1]}（wheelhouse 里找不到对应 wheel）")
            continue
        hash_checked += 1
        if actual != declared:
            hash_bad.append(f"{key[0]}@{key[1]}（声明 {declared[:12]}… 实算 {actual[:12]}…）")
    check("sbom:hashes-recomputed", not hash_bad and hash_checked == len(libs),
          f"{hash_checked} 个 wheel 的 sha256 **现场重算**全部相等"
          f"（不读清单里写的数 —— 『有这个字段』与『值是对的』是两件事）"
          if not hash_bad and hash_checked == len(libs)
          else f"{len(hash_bad)} 处：{hash_bad[:5]}（重算了 {hash_checked}/{len(libs)} 个）")

    # ---------------- S8 运行时事实来自真库 ----------------
    meta_props = {
        str(p.get("name")): str(p.get("value"))
        for p in (_dig(sbom, "metadata.properties") or [])  # type: ignore[union-attr]
        if isinstance(p, dict)
    }
    facts_source = meta_props.get("rednote:runtime:facts-source", "")
    pg_version = meta_props.get("rednote:runtime:postgresql", "")
    queries = [
        str(p.get("value")) for p in (_dig(sbom, "metadata.properties") or [])  # type: ignore[union-attr]
        if isinstance(p, dict) and p.get("name") == "rednote:runtime:query"
    ]
    extensions = [
        str(p.get("value")) for p in (_dig(sbom, "metadata.properties") or [])  # type: ignore[union-attr]
        if isinstance(p, dict) and p.get("name") == "rednote:runtime:extension"
    ]
    s8_ok = (
        facts_source == "live-query"
        and pg_version.startswith("PostgreSQL ")
        and len(queries) >= 2
        and any(e.startswith("vector=") for e in extensions)
    )
    check("sbom:runtime-facts-derived", s8_ok,
          f"PG/pgvector 版本取自**真库只读查询**（{len(queries)} 条 SQL 记进 SBOM）："
          f"{pg_version[:48]}…；扩展 {[e for e in extensions if e.startswith('vector=')]}"
          f"。⚠️ 只能读作「本机实测版本」，**不能**读作「内网版本」"
          if s8_ok
          else f"facts-source={facts_source!r}（必须是 live-query，unavailable 不接受）；"
               f"pg={pg_version[:40]!r}；queries={len(queries)}；extensions={extensions[:4]}")

    # ---------------- S9 未覆盖层显式声明（双向对账）----------------
    declared_uncovered = {
        str(p.get("name")).split(":")[-1]
        for p in (_dig(sbom, "metadata.properties") or [])  # type: ignore[union-attr]
        if isinstance(p, dict) and str(p.get("name", "")).startswith("rednote:uncovered-layer:")
    }
    empty_reason = [
        str(p.get("name"))
        for p in (_dig(sbom, "metadata.properties") or [])  # type: ignore[union-attr]
        if isinstance(p, dict)
        and str(p.get("name", "")).startswith("rednote:uncovered-layer:")
        and len(str(p.get("value", "")).strip()) < 20
    ]
    s9_ok = declared_uncovered == _UNCOVERED_LAYERS and not empty_reason
    check("sbom:uncovered-layers-declared", s9_ok,
          f"SBOM 显式声明 {len(declared_uncovered)} 个未覆盖层且每条都给了理由，"
          f"与判据侧常量**双向相等**：{sorted(_UNCOVERED_LAYERS)}"
          f"（含 L-D 镜像内 OS 包层 —— 『没覆盖』必须是一句会被判据核对的话）"
          if s9_ok
          else f"SBOM-only={sorted(declared_uncovered - _UNCOVERED_LAYERS)}；"
               f"判据-only={sorted(_UNCOVERED_LAYERS - declared_uncovered)}；"
               f"理由过短={empty_reason}")

    # ---------------- S10 direct 标记与 pyproject 一致（双向）----------------
    flagged_direct = {
        _normalize(str(c["name"])) for c in libs if _prop(c, "rednote:direct") == "true"
    }
    s10_ok = flagged_direct == set(direct)
    check("sbom:direct-flag-matches-pyproject", s10_ok,
          f"标记 direct=true 的 {len(flagged_direct)} 个组件与 pyproject 的直接声明"
          f"**双向相等**（防『全部标成 direct』让 S2 变恒真）"
          if s10_ok
          else f"多标={sorted(flagged_direct - set(direct))}；漏标={sorted(set(direct) - flagged_direct)}")

    # ---------------- S11 层标签完整且与覆盖声明一致 ----------------
    comp_layers = {_prop(c, "rednote:layer") for c in components}
    coverage_prop = set(
        (meta_props.get("rednote:coverage:layers", "")).split(",")
    ) - {""}
    s11_ok = comp_layers <= _LAYERS and comp_layers == coverage_prop and bool(comp_layers)
    check("sbom:layer-labels-match-coverage", s11_ok,
          f"每个组件都带 rednote:layer，取值集合 {sorted(comp_layers)} 与 "
          f"metadata 的 coverage:layers 声明**双向相等**"
          f"（L-D 刻意不在其中 —— 它是未覆盖层，不该有组件）"
          if s11_ok
          else f"组件层标签={sorted(comp_layers)}；coverage 声明={sorted(coverage_prop)}；"
               f"合法集合={sorted(_LAYERS)}")

    # ---------------- S12 组件数与 metadata 计数一致 ----------------
    counts_ok = (
        meta_props.get("rednote:count:wheelhouse-wheels") == str(len(wheels))
        and meta_props.get("rednote:count:direct-requirements") == str(len(direct))
        and meta_props.get("rednote:count:images") == str(len(lock.get("images", [])))
        and len(libs) == len(wheels)
        and len(containers) == len(lock.get("images", []))
    )
    check("sbom:counts-agree-with-disk", counts_ok,
          f"metadata 里的三个计数与磁盘实测一致，且组件数对得上："
          f"library {len(libs)} == wheel {len(wheels)}，container {len(containers)} == "
          f"lock {len(lock.get('images', []))}"
          if counts_ok
          else f"metadata counts={{wheels:{meta_props.get('rednote:count:wheelhouse-wheels')},"
               f"direct:{meta_props.get('rednote:count:direct-requirements')},"
               f"images:{meta_props.get('rednote:count:images')}}}；"
               f"磁盘={{wheels:{len(wheels)},direct:{len(direct)},images:{len(lock.get('images', []))}}}；"
               f"组件={{lib:{len(libs)},container:{len(containers)}}}")

    print(f"（SBOM: {sbom_path.relative_to(REPO) if REPO in sbom_path.parents else sbom_path}）")
    return _report(checks)


def _report(checks: list[tuple[str, bool, str]]) -> int:
    print("## WP46 SBOM 覆盖判据（L-A 直接依赖 / L-B 传递闭包 / L-C 镜像引用；L-D 显式未覆盖）")
    print()
    for index, (name, ok, detail) in enumerate(checks, 1):
        print(f"{'PASS' if ok else 'FAIL'}  [{index}] {name}")
        print(f"          {detail}")
    failed = [name for name, ok, _ in checks if not ok]
    total = len(checks)
    print()
    print(f"RESULT={'PASS' if not failed and total == EXPECTED_CHECKS else 'FAIL'} "
          f"checks={total}/{EXPECTED_CHECKS} failures={len(failed)}")
    if total != EXPECTED_CHECKS:
        print(f"  ⚠️ 判据条数 {total} ≠ 期望 {EXPECTED_CHECKS} —— 新增/删除判据须同步 EXPECTED_CHECKS")
    if failed:
        print(f"  失败项：{failed}")
    return 1 if (failed or total != EXPECTED_CHECKS) else 0


if __name__ == "__main__":
    sys.exit(main())
