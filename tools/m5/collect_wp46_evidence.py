#!/usr/bin/env python
"""WP46 证据采集脚本（内网安装包 / SBOM）。

## 这份脚本落盘什么

| 文件 | 内容 |
|---|---|
| `evidence/m5/wp46-env-baseline.log` | **本机环境能力基线**：15 项实测（docker daemon / SBOM 工具链 / shell 工具 / pip 索引 / PG 版本…），逐项带命令、退出码、以及**「它决定了哪条验证属哪一档」** |
| `evidence/m5/wp46-offline-bundle.log` | `check_offline_bundle.py` 全量输出 + 三个安装脚本的 `--dry-run` 实跑（各两遍核对输出一致）+ compose 语法层 D1 读数 |
| `evidence/m5/wp46-sbom-coverage.log` | `check_sbom_coverage.py` 全量输出 + **独立重新派生**的 SBOM 结构摘要 |
| `evidence/m5/wp46-sbom-snapshot.cdx.json` | **入库的那一份 SBOM 快照**（制品本体落 `dist/`，不入库） |

`evidence/m5/wp46-sbom-reverse.log` 由
`tools/quality/collect_sbom_coverage_reverse_evidence.py` 自己落盘，本脚本不重复产出
（两处写同一个文件就是两处真相）。

## 为什么环境能力基线必须落盘，而不是写在文档里

本包 90% 的交付对象（断网内网、真机、真 digest）在这台开发机上不存在。
「哪些验证只能到哪一层」这件事的**全部依据**就是这份基线：
daemon 不可达 ⇒ `docker save`/`up -d` 属 L3；本机 darwin/arm64 ⇒
manylinux wheel 装不上 ⇒ wheelhouse 只能读作"清单可复现"。

写在文档里的话，环境变了（比如哪天 daemon 起来了）文档不会自己更新，
而那时文档里每一句"本机做不到"都成了无依据的话。
⇒ 基线**每次复跑都重新实测**。

## 为什么 SBOM 摘要要用**内联子进程**再算一遍

`SBOM_INSPECT_SCRIPT` 在一个**干净的子进程**里重新打开快照、
重新数组件数 / 层标签分布 / 带 SHA-256 的组件数 / 未覆盖层键集合。

它与 `gen_sbom.py` 的自报数、与 `check_sbom_coverage.py` 的判定**三条独立代码路径**。
这不是冗余：`check_dependency_declarations.py:200-205` 立过同一条规 ——
「刻意**不复用** `_scan_imports` 的遍历：若共用一条 `rglob`，
将来某一次改动会同时骗过两边」。同一个函数算两遍只能证明它自洽，
换一条路径算才构成对账。

内联段的两条硬约束（ISS-45）：**只能单行注释，且不能出现三引号** ——
三引号会提前终止外层的原始字符串包裹。
⚠️ 这条约束对**本文件的模块 docstring 自己也成立**：首版在这里写了
那个包裹形态的字面量，于是模块 docstring 当场被截断成 SyntaxError。

## 为什么 SBOM 快照用固定文件名而不带时间戳

`docs/m5/WP47-演练记录.md:345-349` 自己预言过一件事（已登记 OP-193）：
演练 JSON 带时间戳累积落盘 ⇒ 磁盘 35 份而文档只引用 13 份 ⇒
**没有任何判据会报红**。本包不重复那条路：快照用**固定名**，复跑即覆盖，
「跑过了」这件事由 **mtime 前进**体现，不由文件变多体现。

## 本脚本不做判定逻辑

证据回答「交付那一刻系统长什么样」，判据回答「对不对」。
判定逻辑在那两个 `check_*` 脚本里，这里**不复制一份** ——
本脚本只把原始读数落盘，并把 CHECK 行计数出来让
`rerun_all_evidence.py` 能识别成败（条数不足也算失败，ISS-43：崩溃会伪装成"没东西可查"）。

## 用法

    python tools/m5/collect_wp46_evidence.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
EVIDENCE_DIR: Final[Path] = REPO_ROOT / "evidence" / "m5"
PROD_DIR: Final[Path] = REPO_ROOT / "deploy" / "prod"
BUNDLE_DIR: Final[Path] = REPO_ROOT / "dist" / "bundle"
SBOM_DIR: Final[Path] = BUNDLE_DIR / "sbom"
TIMEZONE: Final[timezone] = timezone(timedelta(hours=8))

#: 期望的 CHECK 行条数。
#: 环境基线 1 + check_offline_bundle 1 + dry-run 3×2 + compose D1 1
#: + check_sbom_coverage 1 + 快照落盘 1 + 内联独立核对 4 = **15**。
#: 条数不足也算失败（ISS-43：崩溃会伪装成"没东西可查"，而那与"查过了没问题"同形）。
#:
#: 2026-10-18：本值首版手写成 16（把两条判据算成"判据 2 + …"又重复数了一次），
#: **被本脚本自己的条数核对当场抓出**（`RESULT=FAIL … 实得 15 条`）——
#: 与 `check_backup_restore_drill.py:122-131` 记的那两次同形，
#: 说明这条"条数不足也算失败"确实在干活。
EXPECTED_CHECKS: Final[int] = 15

#: 环境能力基线的探测项：(标题, 命令, 这项读数决定了什么)
#:
#: ⚠️ 每一项都必须写明"它决定了哪条验证属哪一档"，否则基线就只是一堆命令输出。
ENV_PROBES: Final[tuple[tuple[str, list[str], str], ...]] = (
    ("docker CLI", ["docker", "--version"],
     "有 CLI 才能跑 compose config（语法层 D1）"),
    ("docker compose 插件", ["docker", "compose", "version"],
     "版本决定 config 的行为（本机 5.3.1）"),
    ("docker daemon", ["docker", "info", "--format", "{{.ServerVersion}}"],
     "★ 决定 `docker save`/`load`/`up -d` 是 L1 还是 L3。不可达 ⇒ 全部 L3"),
    ("SBOM 工具链 syft", ["command", "-v", "syft"],
     "缺失 ⇒ 镜像内 OS 包层（L-D）无法覆盖，必须显式登记为未覆盖层"),
    ("SBOM 工具链 trivy", ["command", "-v", "trivy"], "同上"),
    ("SBOM 工具链 cyclonedx-py", ["command", "-v", "cyclonedx-py"],
     "缺失 ⇒ SBOM 只能自己写，且不许引入第三方依赖（会触碰 pyproject，接缝表 #17）"),
    ("漏洞扫描 pip-audit", ["command", "-v", "pip-audit"],
     "缺失 + 需联网漏洞库 ⇒ 漏洞扫描已被判越界，不在本包范围"),
    ("shell 静态检查 shellcheck", ["command", "-v", "shellcheck"],
     "★ 缺失 ⇒ shell 静态检查只能到 `bash -n` + `--dry-run` 行为核对 + 自建正则"),
    ("shell 格式化 shfmt", ["command", "-v", "shfmt"], "同上"),
    ("YAML 工具 yq", ["command", "-v", "yq"],
     "缺失 ⇒ compose 只能用窄行扫描器解析（且不许 import PyYAML，它不在 pyproject 里）"),
    ("nginx 二进制", ["command", "-v", "nginx"],
     "缺失 ⇒ `nginx -t` 跑不了，反代配置只能做形态层核对"),
    ("promtool", ["command", "-v", "promtool"],
     "WP47② 在用；本包不碰监控规则，只读挂载 deploy/monitoring/"),
    ("Python", [sys.executable, "-V"],
     "tomllib 需 3.11+；本包全部 TOML 解析走 tomllib，零第三方依赖"),
    ("pip 索引地址", [sys.executable, "-m", "pip", "config", "list"],
     "★ wheelhouse 从哪个索引下来的 —— 「镜像站与上游内容一致」是**假设**不是事实"),
    ("构建机平台", ["uname", "-sm"],
     "★ 本机 darwin/arm64 ⇒ manylinux wheel 在本机无法 `pip install` 验证"),
)

#: 独立重新派生 SBOM 结构摘要的内联脚本。
#: ⚠️ 只允许单行注释，且不能出现三引号（三引号会提前终止外层包裹，见 ISS-45）。
SBOM_INSPECT_SCRIPT = r"""
import json
import sys

# 这个子进程**不 import 仓库里的任何模块** —— 它换一条代码路径重算一遍，
# 与 gen_sbom.py 的自报数、check_sbom_coverage.py 的判定互为对账。
path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    doc = json.load(handle)

comps = doc.get("components", [])
libs = [c for c in comps if c.get("type") == "library"]
containers = [c for c in comps if c.get("type") == "container"]


def prop(comp, key):
    for item in comp.get("properties") or []:
        if item.get("name") == key:
            return item.get("value", "")
    return ""


layers = {}
for comp in comps:
    layers[prop(comp, "rednote:layer")] = layers.get(prop(comp, "rednote:layer"), 0) + 1

with_hash = 0
for comp in libs:
    for entry in comp.get("hashes") or []:
        if entry.get("alg") == "SHA-256" and len(str(entry.get("content", ""))) == 64:
            with_hash += 1
            break

meta_props = doc.get("metadata", {}).get("properties", []) or []
uncovered = sorted(
    str(p.get("name")).split(":")[-1]
    for p in meta_props
    if str(p.get("name", "")).startswith("rednote:uncovered-layer:")
)
declared = {str(p.get("name")): str(p.get("value")) for p in meta_props}

print("specVersion=" + str(doc.get("specVersion")))
print("serialNumber=" + str(doc.get("serialNumber")))
print("library=" + str(len(libs)) + " container=" + str(len(containers)))
print("layers=" + json.dumps(layers, sort_keys=True))
print("with_sha256=" + str(with_hash))
print("uncovered=" + ",".join(uncovered))
print("coverage_declared=" + declared.get("rednote:coverage:layers", ""))
print("runtime_source=" + declared.get("rednote:runtime:facts-source", ""))
print("runtime_pg=" + declared.get("rednote:runtime:postgresql", "")[:80])

# 四条独立核对：每一条都只用本子进程重算出来的数，不读外层传进来的任何期望值。
ok_spec = doc.get("bomFormat") == "CycloneDX" and str(doc.get("specVersion")) == "1.5"
print("CHECK " + ("PASS" if ok_spec else "FAIL") + " sbom-inline:is-cyclonedx-1.5")

ok_hash = with_hash == len(libs) and len(libs) > 0
print(
    "CHECK "
    + ("PASS" if ok_hash else "FAIL")
    + " sbom-inline:every-library-has-64hex-sha256"
    + "（独立重算：" + str(with_hash) + "/" + str(len(libs)) + "）"
)

# 层标签集合必须与 metadata 的 coverage 声明一致 —— 换一条路径再验一遍。
ok_layers = sorted(k for k in layers if k) == sorted(
    x for x in declared.get("rednote:coverage:layers", "").split(",") if x
)
print(
    "CHECK "
    + ("PASS" if ok_layers else "FAIL")
    + " sbom-inline:layer-labels-match-coverage-declaration"
    + "（" + json.dumps(layers, sort_keys=True) + "）"
)

ok_uncovered = len(uncovered) >= 1 and "image-os-packages" in uncovered
print(
    "CHECK "
    + ("PASS" if ok_uncovered else "FAIL")
    + " sbom-inline:l-d-declared-uncovered"
    + "（未覆盖层：" + ",".join(uncovered) + "）"
)
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    """跑一条命令。`command -v X` 走 shell（它是 shell 内建，不是可执行文件）。"""
    env = dict(os.environ, NO_PROXY="*", no_proxy="*")
    if cmd[0] == "command":
        proc = subprocess.run(
            " ".join(cmd), shell=True, capture_output=True, text=True, env=env,
            cwd=str(cwd) if cwd else None,
        )
    else:
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, env=env,
                cwd=str(cwd) if cwd else None,
            )
        except FileNotFoundError as exc:
            return 127, f"{exc}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def collect_env_baseline() -> tuple[Path, list[str]]:
    checks: list[str] = []
    lines = [
        "# WP46 本机环境能力基线（每次复跑重新实测）",
        f"# 生成时间: {_now()}",
        "#",
        "# ⚠️ 这份基线是「哪些验证只能到哪一层」的**全部依据**。",
        "#   L1 = 本机可真验 / L2 = 只能到脚本能跑、产物存在 / L3 = 完全不可本机验。",
        "#   环境变了基线会跟着变，而文档里写死的「本机做不到」不会自己更新 ——",
        "#   因此结论必须回到这份实测，不是回到文档。",
        "",
    ]
    for title, cmd, meaning in ENV_PROBES:
        rc, out = _run(cmd)
        # ⚠️ 这里的 rc != 0 **不算失败** —— "工具缺失"本身就是要记录的事实。
        # 本脚本不做判定（判定在 check_* 脚本里），只落盘原始读数。
        lines += [
            f"## {title}",
            f"CMD: {' '.join(cmd)}",
            f"STATUS: {'OK' if rc == 0 else f'EXIT={rc}'}",
            f"OUT: {out.splitlines()[0] if out.splitlines() else '(无输出)'}",
            f"意味着: {meaning}",
            "",
        ]

    # PG / pgvector 真库读数（SBOM 的 runtime facts 来源，判据 S8 依赖它）
    sys.path.insert(0, str(REPO_ROOT / "tools" / "ops"))
    try:
        import drill_db

        psql = str(Path(drill_db.pg_bin()) / "psql")
        dsn = os.environ.get(
            "REDNOTE_TEST_DATABASE_URL",
            os.environ.get("REDNOTE_DATABASE_URL", "postgresql://localhost:5432/postgres"),
        ).replace("postgresql+psycopg://", "postgresql://", 1)
        rc_pg, out_pg = _run([psql, dsn, "-At", "-c", "select version()"])
        rc_ext, out_ext = _run([psql, dsn, "-At", "-c",
                                "select extname||'='||extversion from pg_extension order by 1"])
    except Exception as exc:  # noqa: BLE001 —— 记录失败本身就是读数
        rc_pg, out_pg, rc_ext, out_ext = 1, f"{exc}", 1, ""
    lines += [
        "## PostgreSQL / pgvector（SBOM runtime facts 的来源）",
        "CMD: psql -At -c 'select version()' / 'select extname||\"=\"||extversion from pg_extension'",
        f"STATUS: EXIT={rc_pg}/{rc_ext}",
        f"OUT: {out_pg[:160]}",
        f"EXT: {out_ext[:160]}",
        "意味着: ★ SBOM 的 PG/pgvector 版本取真库实测而非抄 ADR。"
        "只能读作「**本机**实测版本」，**不能**读作「内网版本」",
        "",
    ]
    checks.append(
        f"CHECK {'PASS' if rc_pg == 0 else 'FAIL'} env:pg-reachable（SBOM 的 S8 依赖它）"
    )
    lines += checks + [""]
    target = EVIDENCE_DIR / "wp46-env-baseline.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target, checks


def collect_offline_bundle() -> tuple[Path, list[str]]:
    checks: list[str] = []
    rc_check, out_check = _run([sys.executable,
                                str(REPO_ROOT / "tools" / "quality" / "check_offline_bundle.py")])
    lines = [
        "# WP46 离线安装包与生产编排：判据输出 + 安装脚本 --dry-run 实跑",
        f"# 生成时间: {_now()}",
        "#",
        "# ⚠️ 读法（三档验证层级，见 docs/m5/WP46-内网安装包与SBOM说明.md §6）：",
        "#   下面的判据输出属 **L1**（本机可真验）—— 形态、对账、校验值都是真算的。",
        "#   下面的 --dry-run 输出属 **L2**（只能到脚本能跑、产物存在）——",
        "#     `docker load` / `up -d` **一步都没执行**（本机 daemon 不可达）。",
        "#   断网安装属 **L3**（完全不可本机验），见说明文档 §5 内网待跑清单。",
        "",
        "## 判据：tools/quality/check_offline_bundle.py",
        f"EXIT={rc_check}",
        "",
        out_check.rstrip(),
        "",
    ]
    checks.append(f"CHECK {'PASS' if rc_check == 0 else 'FAIL'} criteria:check_offline_bundle")

    dry_runs: list[tuple[str, list[str]]] = [
        ("install.sh", ["bash", str(PROD_DIR / "install.sh"),
                        "--bundle", str(BUNDLE_DIR), "--dry-run"]),
        ("upgrade.sh", ["bash", str(PROD_DIR / "upgrade.sh"),
                        "--bundle", str(BUNDLE_DIR),
                        "--backup-dir", "/tmp/wp46-dry-backup", "--dry-run"]),
        ("uninstall.sh", ["bash", str(PROD_DIR / "uninstall.sh"), "--dry-run"]),
    ]
    for name, cmd in dry_runs:
        rc1, out1 = _run(cmd)
        rc2, out2 = _run(cmd)
        identical = out1 == out2 and rc1 == rc2
        lines += [
            f"## --dry-run: {name}（连跑两遍核对输出一致）",
            f"CMD: {' '.join(cmd)}",
            f"EXIT: {rc1} / {rc2}",
            "",
            out1.rstrip(),
            "",
        ]
        checks.append(f"CHECK {'PASS' if rc1 == 0 else 'FAIL'} dryrun:{name}:exit-zero")
        checks.append(
            f"CHECK {'PASS' if identical else 'FAIL'} dryrun:{name}:deterministic"
            f"（两遍输出{'逐字相同' if identical else '不同'} —— "
            f"**只能读作 --dry-run 输出确定，不能读作真机重复执行幂等**："
            f"真幂等要看卷、容器、迁移三者的重入）"
        )

    rc_cfg, out_cfg = _run(["docker", "compose", "--env-file", ".env.example",
                            "config", "--quiet"], cwd=PROD_DIR)
    lines += [
        "## compose 语法层 D1（**降级使用**）",
        "CMD: cd deploy/prod && docker compose --env-file .env.example config --quiet",
        f"EXIT={rc_cfg}",
        (out_cfg.rstrip() or "(无输出)"),
        "",
        "⚠️ 此条**只证明 YAML 合法 + 变量可插值**。本机实测（2026-10-18，Compose 5.3.1）：",
        "   伪 digest（非 64 位十六进制）与不存在的 registry，`config --quiet` 一律 EXIT=0",
        "   ⇒ 它**不校验镜像引用是否有效**。真正的形态判据是 `prod:no-floating-image-ref`，",
        "   它不依赖 compose config。反向验证 R3b 把这件事钉成了一条常驻读数。",
        "",
    ]
    checks.append(f"CHECK {'PASS' if rc_cfg == 0 else 'FAIL'} compose:syntax-d1")

    lines += checks + [""]
    target = EVIDENCE_DIR / "wp46-offline-bundle.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target, checks


def collect_sbom_coverage() -> tuple[Path, list[str], Path | None]:
    checks: list[str] = []
    rc_check, out_check = _run([sys.executable,
                                str(REPO_ROOT / "tools" / "quality" / "check_sbom_coverage.py")])
    checks.append(f"CHECK {'PASS' if rc_check == 0 else 'FAIL'} criteria:check_sbom_coverage")

    # 入库快照：取 dist 里最新那份，固定名复制进 evidence（理由见 docstring）
    snapshot: Path | None = None
    candidates = sorted(SBOM_DIR.glob("*.cdx.json")) if SBOM_DIR.is_dir() else []
    if candidates:
        snapshot = EVIDENCE_DIR / "wp46-sbom-snapshot.cdx.json"
        shutil.copy2(candidates[-1], snapshot)
    checks.append(
        f"CHECK {'PASS' if snapshot else 'FAIL'} evidence:sbom-snapshot-written"
        f"（{snapshot.relative_to(REPO_ROOT) if snapshot else '未落盘 —— dist/bundle/sbom/ 为空'}）"
    )

    # ---- 内联子进程独立重新派生（三条独立代码路径互为对账，见 docstring）----
    inline_out = "(未执行：快照不存在)"
    if snapshot is not None:
        rc_inline, inline_out = _run([sys.executable, "-c", SBOM_INSPECT_SCRIPT, str(snapshot)])
        if rc_inline != 0:
            inline_out = f"EXIT={rc_inline}\n{inline_out}"
        checks.extend(
            line for line in inline_out.splitlines() if line.startswith("CHECK ")
        )

    lines = [
        "# WP46 SBOM 覆盖：判据输出 + 独立重新派生的结构摘要",
        f"# 生成时间: {_now()}",
        "#",
        "# ⚠️ 读法：判据全绿只能读作「SBOM 覆盖 L-A 直接依赖 + L-B 传递闭包 + L-C 镜像引用",
        "#   三层，且这三层能被机械对账」。**L-D 镜像内 OS 包层未覆盖**（无 syft/trivy），",
        "#   由判据 `sbom:uncovered-layers-declared` 显式钉着。",
        "",
        "## 判据：tools/quality/check_sbom_coverage.py",
        f"EXIT={rc_check}",
        "",
        out_check.rstrip(),
        "",
        "## 独立重新派生（干净子进程，不 import 仓库任何模块）",
        f"快照: {snapshot.relative_to(REPO_ROOT) if snapshot else '(无)'}",
        "",
        inline_out.rstrip(),
        "",
    ]
    lines += checks + [""]
    target = EVIDENCE_DIR / "wp46-sbom-coverage.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target, checks, snapshot


def main() -> int:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    checks: list[str] = []

    for collector in (collect_env_baseline, collect_offline_bundle):
        path, produced = collector()
        written.append(path)
        checks.extend(produced)

    path, produced, snapshot = collect_sbom_coverage()
    written.append(path)
    if snapshot is not None:
        written.append(snapshot)
    checks.extend(produced)

    failures = [line for line in checks if line.startswith("CHECK FAIL")]
    # 条数不足也算失败（ISS-43：崩溃会伪装成"没东西可查"，而那与"查过了没问题"同形）。
    complete = len(checks) >= EXPECTED_CHECKS

    print("## WP46 证据采集")
    for item in written:
        print(f"  wrote {item.relative_to(REPO_ROOT)}")
    print()
    for line in checks:
        print(f"  {line}")
    print()
    print(f"RESULT={'PASS' if not failures and complete else 'FAIL'} "
          f"checks={len(checks)} failures={len(failures)}"
          f"（期望 >= {EXPECTED_CHECKS} 条，实得 {len(checks)} 条）")
    if not complete:
        print(f"  ⚠️ CHECK 行 {len(checks)} 条 < 期望 {EXPECTED_CHECKS} —— "
              f"某个采集环节崩掉了，而崩掉与『没东西可查』同形")
    return 0 if (not failures and complete) else 1


if __name__ == "__main__":
    sys.exit(main())
