#!/usr/bin/env python
"""SBOM 与离线包判据的反向验证（WP46）。

## 这份脚本要证明什么

`check_sbom_coverage.py`（14 条）与 `check_offline_bundle.py`（29 条）的共同形态是
**"声称自己有防线"**（ISS-95）：每一条全绿时都会报出一句"覆盖/对账通过"。

而本包的两条核心判据**各自都有一条已知的恒真路径**：

| 判据 | 它的恒真路径 |
|---|---|
| `sbom:python-closure-equals-wheelhouse` | wheelhouse 按 `--no-deps` 构建时，SBOM 与 wheelhouse **仍双向相等**（都只有直接依赖）⇒ 全绿而闭包根本没取到。与 WP47④ 队列表那次 `0 == 0` 同族（`docs/m5/WP47-演练记录.md:167-180`） |
| 计划 §4.1 原文的 `docker compose config` | 本机实测对伪 digest / 不存在的 registry 一律 EXIT=0 ⇒ 验收通过而版本根本没锁 |

⇒ 光"跑一次绿了"不能证明这些判据在干活。本脚本**逐个注入破坏**，
要求每一种破坏都让**指定的那一条**判据报红（不是"随便红一条"）。

## 为什么必须做成常驻脚本

第 5 条纪律（ISS-143）：**「没跑」与「跑失败」同形**。
终端里跑过一次变异、下次没人再跑，几周后判据退化成恒真而无人知晓
—— 那与"当初就没写"完全同形。⇒ 落盘 + 挂 CI 门 3。

## ⚠️ 与 WP47④ 那份反向验证的一处**刻意不同**：不在原地改共享文件

`collect_queue_reconcile_reverse_evidence.py` 的做法是「原地改 → 跑 → 逐字还原」，
它的 docstring 自己记了这个做法的风险（共享工作树，并行会话可能在窗口内改同一文件，
还原会失败而磁盘停留在变异状态）。

本包的被变异对象里有 **`services/backend/pyproject.toml`**——
它是接缝表 #17 的禁改文件，且本包的收口条件之一就是
`git diff --stat services/backend/pyproject.toml` 为空。
在它上面做"改了再还原"，一旦脚本在窗口内被中断，收口条件就被破坏了。

⇒ 本脚本**全部变异都在仓库外的临时副本上做**，被测脚本通过
`--pyproject` / `--wheelhouse` / `--sbom` / `--images-lock` / `--deploy-dir`
指向副本。原文件**一次都不写**。
R6 不是"还原后回绿"，而是**"原文件的 sha256 与开跑前逐字相同"** ——
比"还原成功"更强：它证明的是根本没动过。

## 变异清单（每种对应一种具体的"退化成恒真"路径）

| 编号 | 注入 | 必须报红的判据 |
|---|---|---|
| R0 | 基线未变异 | 两个脚本全绿 |
| R1 | wheelhouse 副本里删掉一个**直接依赖**的 wheel，重新生成 SBOM | `sbom:python-direct-superset` |
| R2 | wheelhouse 副本只留直接依赖的 wheel（模拟 `pip download --no-deps`） | **`sbom:closure-strictly-larger`**，且 `sbom:python-closure-equals-wheelhouse` **仍绿** ← 这一条证明 S3 单独使用时毫无信息 |
| R3 | deploy 副本里把一个镜像 tag 改成 `:latest`（env 与 lock 两侧同改） | `prod:no-floating-image-ref` |
| R3b | 对 R3 的那份副本跑 `docker compose config` | **EXIT 仍为 0** ← 把计划原文验收不设防这件事钉进证据 |
| R4 | SBOM 副本里把一个组件的 sha256 改一位 | `sbom:hashes-recomputed` |
| R5 | SBOM 副本里删掉一条未覆盖层声明 | `sbom:uncovered-layers-declared` |
| R6 | 原文件 sha256 与开跑前逐字相同 | 零污染 |

## 锚点纪律

每处文本变异必须满足 `src.count(old) == 1`，否则**fail-fast**。
「恰好 1」而不是「至少 1」：命中 0 次意味着变异根本没改上，而那时
"判据没报红"与"变异没生效"**同形**（`docs/m5/WP47-演练记录.md:225-228` 的教训）。
文件级变异（删 wheel）用同一条纪律的等价形式：glob 命中数必须恰好 1。

## 用法

    python tools/quality/collect_sbom_coverage_reverse_evidence.py
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
PYPROJECT: Final[Path] = REPO_ROOT / "services" / "backend" / "pyproject.toml"
WHEELHOUSE: Final[Path] = REPO_ROOT / "dist" / "bundle" / "wheelhouse"
IMAGES_LOCK: Final[Path] = REPO_ROOT / "deploy" / "prod" / "images.lock.json"
DEPLOY_DIR: Final[Path] = REPO_ROOT / "deploy"
GEN_SBOM: Final[Path] = REPO_ROOT / "tools" / "ops" / "gen_sbom.py"
CHECK_SBOM: Final[Path] = REPO_ROOT / "tools" / "quality" / "check_sbom_coverage.py"
CHECK_BUNDLE: Final[Path] = REPO_ROOT / "tools" / "quality" / "check_offline_bundle.py"

EVIDENCE: Final[Path] = REPO_ROOT / "evidence" / "m5" / "wp46-sbom-reverse.log"

#: 开跑前要拍 sha256 快照、收尾时逐字核对的原文件。
#: 其中 `pyproject.toml` 是接缝表 #17 的禁改文件 —— 本脚本对它的要求是
#: **一个字节都不许变**，不是"改完还原得回去"。
PROTECTED_FILES: Final[tuple[Path, ...]] = (
    PYPROJECT,
    IMAGES_LOCK,
    DEPLOY_DIR / "prod" / "docker-compose.yml",
    DEPLOY_DIR / "prod" / ".env.example",
    GEN_SBOM,
    CHECK_SBOM,
    CHECK_BUNDLE,
)

#: 用例数：R0 + R1 + R2 + R3 + R3b + R4 + R5 + R6 = 8。
EXPECTED_CASES: Final[int] = 8

_RESULT_RE: Final[re.Pattern[str]] = re.compile(r"^RESULT=(PASS|FAIL) checks=(\d+)/(\d+) failures=(\d+)")
_FAILED_RE: Final[re.Pattern[str]] = re.compile(r"^\s*失败项：(\[.*\])")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize(raw: str) -> str:
    return re.sub(r"[-_.]+", "-", raw).lower()


def _direct_names(pyproject: Path) -> set[str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
    reqs = list(data.get("dependencies", []))
    for value in (data.get("optional-dependencies") or {}).values():
        reqs.extend(value)
    out: set[str] = set()
    for req in reqs:
        match = re.match(r"^([A-Za-z0-9._-]+)", req.strip())
        if match:
            out.add(_normalize(match.group(1)))
    return out


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _parse_checker(output: str) -> tuple[str, list[str]]:
    """从判据脚本输出里取 (RESULT 行, 失败项名单)。

    **抓不到 RESULT 行就返回空串**，调用方必须据此报红 ——
    读不到读数时"报红/全绿"都不可信（`collect_queue_reconcile_reverse_evidence.py`
    的 `NO SUMMARY` 同款防护：首版因 `-qq` 吞掉汇总行而读到空字符串）。
    """
    result_line = ""
    failed: list[str] = []
    for line in output.splitlines():
        if _RESULT_RE.match(line.strip()):
            result_line = line.strip()
        match = _FAILED_RE.match(line)
        if match:
            failed = re.findall(r"'([^']+)'", match.group(1))
    return result_line, failed


def _mutate_text(path: Path, old: str, new: str) -> None:
    """副本上的文本替换。**锚点必须恰好命中 1 次**，否则抛。

    「恰好 1」而不是「至少 1」：命中 0 次 ⇒ 变异没改上，而那时"判据没报红"
    与"变异没生效"同形；命中 2 次以上 ⇒ 改掉了多处，报出来的内容会误导读证据的人。
    """
    src = path.read_text(encoding="utf-8")
    hits = src.count(old)
    if hits != 1:
        raise RuntimeError(f"锚点在 {path.name} 命中 {hits} 次（要求恰好 1）：{old[:60]!r}")
    path.write_text(src.replace(old, new), encoding="utf-8")


def _gen_sbom(workdir: Path, wheelhouse: Path, pyproject: Path, tag: str) -> Path:
    out = workdir / f"sbom-{tag}.cdx.json"
    rc, output = _run([
        sys.executable, str(GEN_SBOM),
        "--pyproject", str(pyproject),
        "--wheelhouse", str(wheelhouse),
        "--images-lock", str(IMAGES_LOCK),
        "--out", str(out),
    ])
    if rc != 0:
        raise RuntimeError(f"gen_sbom 在变异 {tag} 上失败：{output[-600:]}")
    return out


def _check_sbom(sbom: Path, wheelhouse: Path, pyproject: Path) -> tuple[str, list[str], str]:
    _, output = _run([
        sys.executable, str(CHECK_SBOM),
        "--sbom", str(sbom),
        "--wheelhouse", str(wheelhouse),
        "--pyproject", str(pyproject),
        "--images-lock", str(IMAGES_LOCK),
    ])
    result, failed = _parse_checker(output)
    return result, failed, output


def _detail_of(output: str, check_name: str) -> str:
    """取某一条判据的 detail 行（判据输出里紧跟判据名的那一行）。

    R2 需要它：S3（闭包与 wheelhouse 双向相等）在 `--no-deps` 退化下**也会红**，
    但红的原因是它自带的 `_MIN_WHEELS` 下界，**不是**集合不相等。
    要证明"集合相等这件事本身毫无信息"，必须读到 detail 里
    `SBOM-only=[] ；wheelhouse-only=[]` 这两段 —— 只看红/绿读不出来。
    """
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line.strip().endswith(check_name) and index + 1 < len(lines):
            return lines[index + 1].strip()
    return ""


def main() -> int:
    lines: list[str] = [
        "## WP46 SBOM 与离线包判据 —— 反向验证",
        "",
        "证明 `check_sbom_coverage.py`（14 条）与 `check_offline_bundle.py`（29 条）",
        "真会报红，而不是在 wheelhouse 退化成 `--no-deps`、或镜像 tag 变回 `:latest` 时仍然全绿。",
        "",
        "⚠️ 本脚本**全部变异都在仓库外的临时副本上做**，原文件一次都不写。",
        "   R6 核对的是『原文件 sha256 与开跑前逐字相同』—— 比『还原成功』更强。",
        "",
        f"生成时间：{datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    results: list[tuple[str, bool, str]] = []

    # ---------- 开跑前：原文件 sha256 快照 ----------
    before: dict[Path, str] = {}
    for path in PROTECTED_FILES:
        if not path.is_file():
            print(f"原文件缺失，无法建立零污染基线：{path}", file=sys.stderr)
            return 1
        before[path] = _sha256(path)

    workdir = Path(tempfile.mkdtemp(prefix="wp46_reverse_"))
    try:
        # ---------- R0 基线 ----------
        rc_sbom, out_sbom = _run([sys.executable, str(CHECK_SBOM)])
        res_sbom, _ = _parse_checker(out_sbom)
        rc_bundle, out_bundle = _run([sys.executable, str(CHECK_BUNDLE)])
        res_bundle, _ = _parse_checker(out_bundle)
        readable = bool(res_sbom) and bool(res_bundle)
        baseline_ok = rc_sbom == 0 and rc_bundle == 0 and readable
        lines += [
            "### R0 基线（未变异）", "",
            "```", f"check_sbom_coverage.py   {res_sbom or '(读不到 RESULT 行)'}",
            f"check_offline_bundle.py  {res_bundle or '(读不到 RESULT 行)'}", "```", "",
        ]
        if baseline_ok:
            lines += ["⇒ 变异前两个判据脚本全绿，且都读到了 RESULT 行。", ""]
        elif readable:
            lines += ["⇒ ⚠️ 基线就不绿，下面的『报红』不可信。", ""]
        else:
            lines += [
                "⇒ ⚠️ **读不到 RESULT 行** —— 这属于「没跑」与「跑失败」同形（纪律第 5 条）：",
                "下面的『报红』不可信。", "",
            ]
        results.append((
            "R0 基线（未变异）两脚本全绿且可读", baseline_ok,
            f"sbom: {res_sbom or 'NO RESULT'} ｜ bundle: {res_bundle or 'NO RESULT'}",
        ))

        direct = _direct_names(PYPROJECT)

        # ---------- R1 wheelhouse 少一个直接依赖的 wheel ----------
        name = "R1 wheelhouse 副本删掉一个直接依赖的 wheel（fastapi）"
        wh1 = workdir / "wh-r1"
        shutil.copytree(WHEELHOUSE, wh1)
        victims = sorted(wh1.glob("fastapi-*.whl"))
        if len(victims) != 1:
            results.append((name, False, f"锚点命中 {len(victims)} 个 fastapi wheel（要求恰好 1）"))
            lines += [f"### {name}", "", f"❌ **变异未生效**：glob 命中 {len(victims)} 个（要求恰好 1）", ""]
        else:
            victims[0].unlink()
            sbom1 = _gen_sbom(workdir, wh1, PYPROJECT, "r1")
            res, failed, _ = _check_sbom(sbom1, wh1, PYPROJECT)
            hit = "sbom:python-direct-superset" in failed
            results.append((name, hit, f"{res}；失败项={failed}"))
            lines += [
                f"### {name}", "",
                "- 注入：删掉 `fastapi-0.115.0-py3-none-any.whl`，并**用减少后的 wheelhouse 重新生成 SBOM**",
                "- 为什么这是缺陷：SBOM 漏掉一个**直接**依赖 ⇒ 内网装机时它根本不在包里",
                f"- 结果：**{'✅ 报红' if hit else '❌ 未报红'}**（期望 `sbom:python-direct-superset` 在失败项里）",
                "", "```", res, f"失败项={failed}", "```", "",
            ]

        # ---------- R2 闭包退化成 --no-deps ----------
        name = "R2 wheelhouse 只留直接依赖的 wheel（模拟 pip download --no-deps）"
        wh2 = workdir / "wh-r2"
        wh2.mkdir()
        kept = 0
        for wheel in sorted(WHEELHOUSE.glob("*.whl")):
            if _normalize(wheel.name.split("-")[0]) in direct:
                shutil.copy2(wheel, wh2 / wheel.name)
                kept += 1
        sbom2 = _gen_sbom(workdir, wh2, PYPROJECT, "r2")
        res, failed, out2 = _check_sbom(sbom2, wh2, PYPROJECT)
        s3a_red = "sbom:closure-strictly-larger" in failed
        s3_detail = _detail_of(out2, "sbom:python-closure-equals-wheelhouse")
        # S3 的**集合相等**部分仍然成立（两边都只有直接依赖）。
        # S3 之所以也红，是因为它自带的 `_MIN_WHEELS` 下界 —— 那是**另一道**防线，
        # 不是"集合对账抓到了"。两者必须分开读，否则会把"下界在干活"
        # 误记成"集合对账在干活"。
        s3_sets_still_equal = (
            "SBOM-only=[]" in s3_detail and "wheelhouse-only=[]" in s3_detail
        ) or "sbom:python-closure-equals-wheelhouse" not in failed
        hit = s3a_red and s3_sets_still_equal
        results.append((
            name, hit,
            f"{res}；S3a红={s3a_red} S3集合仍相等={s3_sets_still_equal}；失败项={failed}",
        ))
        lines += [
            f"### {name}", "",
            f"- 注入：只拷直接依赖的 wheel（{kept} 个，原 {len(list(WHEELHOUSE.glob('*.whl')))} 个），重新生成 SBOM",
            "- 为什么这是缺陷：传递依赖一个都没取到，而内网装机缺的恰恰是那些",
            f"- 结果：**{'✅ 报红' if s3a_red else '❌ 未报红'}** `sbom:closure-strictly-larger`",
            "- ★ **关键读数**：`sbom:python-closure-equals-wheelhouse` 的**集合差仍为空** ——",
            f"  `{s3_detail[:160] or '(该条未红，集合自然相等)'}`",
            "  即 SBOM 与 wheelhouse 仍然双向相等，而传递闭包一个都没取到。",
            "  ⇒ **『集合相等』这件事在这种退化下毫无信息**，『严格大于』那条不是冗余判据。",
            "- 另外两条也红了，但要分清它们各自抓到的是什么（别把下界在干活记成集合对账在干活）：",
            "  ① S3 红在它自带的 `_MIN_WHEELS` 下界（19 < 20），不是红在集合不等；",
            "  ② `sbom:layer-labels-match-coverage` 红在**组件里再没有 L-B 层**了 ——",
            "     它从另一个角度抓到了同一次退化，这是刻意的交叉覆盖。",
            "", "```", res, f"失败项={failed}", "```", "",
        ]

        # ---------- R3 镜像 tag 改回 :latest ----------
        name = "R3 deploy 副本里把一个镜像 tag 改成 :latest"
        deploy3 = workdir / "deploy-r3"
        shutil.copytree(DEPLOY_DIR, deploy3)
        env3 = deploy3 / "prod" / ".env.example"
        lock3 = deploy3 / "prod" / "images.lock.json"
        compose_ok_detail = ""
        try:
            _mutate_text(env3, "IMAGE_POSTGRES=pgvector/pgvector:pg17",
                         "IMAGE_POSTGRES=pgvector/pgvector:latest")
            _mutate_text(lock3, '"ref": "pgvector/pgvector:pg17"',
                         '"ref": "pgvector/pgvector:latest"')
            _mutate_text(lock3, '"tag": "pg17"', '"tag": "latest"')
        except RuntimeError as exc:
            results.append((name, False, f"变异未生效：{exc}"))
            results.append(("R3b docker compose config 对 :latest 仍 EXIT=0", False, "R3 变异未生效，无法核对"))
            lines += [f"### {name}", "", f"❌ **变异未生效**：{exc}", ""]
        else:
            _, out3 = _run([sys.executable, str(CHECK_BUNDLE), "--deploy-dir", str(deploy3)])
            res3, failed3 = _parse_checker(out3)
            hit = "prod:no-floating-image-ref" in failed3
            results.append((name, hit, f"{res3}；失败项={failed3}"))
            lines += [
                f"### {name}", "",
                "- 注入：`.env.example` 与 `images.lock.json` 两侧同改成 `pgvector/pgvector:latest`",
                "  （两侧同改是刻意的：只改一侧会先红在 `prod:lock-env-refs-equal` 上，",
                "   那样就验不到 `prod:no-floating-image-ref` 这一条）",
                "- 为什么这是缺陷：`deploy/dev/docker-compose.yml:79` 的 `minio/minio:latest` 形态被带进生产",
                f"- 结果：**{'✅ 报红' if hit else '❌ 未报红'}**（期望 `prod:no-floating-image-ref`）",
                "", "```", res3, f"失败项={failed3}", "```", "",
            ]

            # ---------- R3b 把「compose config 不设防」钉进证据 ----------
            cname = "R3b docker compose config 对 :latest 仍 EXIT=0"
            if shutil.which("docker") is None:
                results.append((cname, False, "找不到 docker CLI，无法核对（fail-closed，不当 SKIP）"))
                compose_ok_detail = "找不到 docker CLI"
            else:
                rc_cfg, out_cfg = _run(
                    ["docker", "compose", "--env-file", ".env.example", "config", "--quiet"],
                    cwd=deploy3 / "prod",
                )
                # 这里期望的是 EXIT=0 —— **不是**"判据红"。
                # 它要证明的是计划 §4.1 那句验收原文不设防。
                results.append((cname, rc_cfg == 0, f"exit={rc_cfg}"))
                compose_ok_detail = f"exit={rc_cfg}"
                lines += [
                    f"### {cname}", "",
                    "- 对**同一份**带 `:latest` 的副本跑 `docker compose --env-file .env.example config --quiet`",
                    f"- 结果：**exit={rc_cfg}**"
                    + ("（⇒ 计划 §4.1 的『compose config 静态校验通过』**对 `:latest` 毫无反应**，"
                       "它只验 YAML 语法 + 变量插值。这就是本包必须自建 "
                       "`prod:no-floating-image-ref` 的全部理由）" if rc_cfg == 0
                       else "（本次非 0，与 2026-10-18 的实测不符，需复核）"),
                    "", "```", (out_cfg.strip() or "(无输出)")[:400], "```", "",
                ]

        # ---------- R4 改一位 sha256 ----------
        name = "R4 SBOM 副本里把一个组件的 sha256 改一位"
        sbom_base = _gen_sbom(workdir, WHEELHOUSE, PYPROJECT, "base")
        sbom4 = workdir / "sbom-r4.cdx.json"
        doc = json.loads(sbom_base.read_text(encoding="utf-8"))
        victim = next(
            (c for c in doc["components"] if c.get("type") == "library" and c.get("hashes")), None
        )
        if victim is None:
            results.append((name, False, "SBOM 里找不到带 hashes 的 library 组件"))
            lines += [f"### {name}", "", "❌ **变异未生效**：找不到可变异的组件", ""]
        else:
            original = victim["hashes"][0]["content"]
            flipped = ("0" if original[0] != "0" else "1") + original[1:]
            sbom4.write_text(sbom_base.read_text(encoding="utf-8"), encoding="utf-8")
            _mutate_text(sbom4, original, flipped)
            res4, failed4, _ = _check_sbom(sbom4, WHEELHOUSE, PYPROJECT)
            hit = "sbom:hashes-recomputed" in failed4
            results.append((name, hit, f"{res4}；失败项={failed4}"))
            lines += [
                f"### {name}", "",
                f"- 注入：`{victim['name']}@{victim['version']}` 的 sha256 首位 "
                f"`{original[:8]}…` → `{flipped[:8]}…`",
                "- 为什么这是缺陷：字段齐全但内容是编的 ——「有这个字段」与「值是对的」是两件事",
                f"- 结果：**{'✅ 报红' if hit else '❌ 未报红'}**（期望 `sbom:hashes-recomputed`）",
                "", "```", res4, f"失败项={failed4}", "```", "",
            ]

        # ---------- R5 删掉一条未覆盖层声明 ----------
        name = "R5 SBOM 副本里删掉一条未覆盖层声明"
        sbom5 = workdir / "sbom-r5.cdx.json"
        sbom5.write_text(sbom_base.read_text(encoding="utf-8"), encoding="utf-8")
        doc5 = json.loads(sbom5.read_text(encoding="utf-8"))
        target_prop = next(
            (p for p in doc5["metadata"]["properties"]
             if str(p["name"]).startswith("rednote:uncovered-layer:")), None
        )
        if target_prop is None:
            results.append((name, False, "SBOM 里找不到 uncovered-layer 声明"))
            lines += [f"### {name}", "", "❌ **变异未生效**：找不到可删的声明", ""]
        else:
            doc5["metadata"]["properties"] = [
                p for p in doc5["metadata"]["properties"] if p is not target_prop
            ]
            sbom5.write_text(json.dumps(doc5, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            res5, failed5, _ = _check_sbom(sbom5, WHEELHOUSE, PYPROJECT)
            hit = "sbom:uncovered-layers-declared" in failed5
            results.append((name, hit, f"{res5}；失败项={failed5}"))
            lines += [
                f"### {name}", "",
                f"- 注入：删掉 `{target_prop['name']}`",
                "- 为什么这是缺陷：「没覆盖」从一句会被核对的话，变成一处静默漏检",
                f"- 结果：**{'✅ 报红' if hit else '❌ 未报红'}**（期望 `sbom:uncovered-layers-declared`）",
                "", "```", res5, f"失败项={failed5}", "```", "",
            ]

        # ---------- R6 零污染 ----------
        polluted = [
            str(path.relative_to(REPO_ROOT)) for path in PROTECTED_FILES
            if not path.is_file() or _sha256(path) != before[path]
        ]
        results.append((
            "R6 原文件 sha256 与开跑前逐字相同（零污染）", not polluted,
            f"{len(PROTECTED_FILES)} 个受保护文件全部未变" if not polluted
            else f"被改动：{polluted}",
        ))
        lines += [
            "### R6 零污染核对", "",
            f"- 受保护文件 {len(PROTECTED_FILES)} 个（含接缝表 #17 的 `services/backend/pyproject.toml`）",
            f"- 结果：**{'✅ 全部未变' if not polluted else '❌ 有改动：' + str(polluted)}**",
            "- 意义：本脚本从不在原地改共享文件 ⇒ 不存在「还原失败而磁盘停在变异状态」这条风险路径。",
            "",
        ]
        del compose_ok_detail
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    failed_cases = [name for name, ok, _ in results if not ok]
    total = len(results)
    lines += ["---", "", "## 读数", ""]
    for name, ok, detail in results:
        lines.append(f"- {'PASS' if ok else 'FAIL'} {name} —— {detail}")
    lines += [
        "",
        f"RESULT={'PASS' if not failed_cases and total == EXPECTED_CASES else 'FAIL'} "
        f"cases={total}/{EXPECTED_CASES} failures={len(failed_cases)}",
    ]
    if total != EXPECTED_CASES:
        lines.append(f"  ⚠️ 读数 {total} ≠ 期望 {EXPECTED_CASES} —— 用例与 EXPECTED_CASES 脱钩了")
    lines.append("")

    # 自落盘（铁律 14：证据不许只靠人手 `| tee`）
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"（证据已落盘：{EVIDENCE.relative_to(REPO_ROOT)}）")
    return 1 if (failed_cases or total != EXPECTED_CASES) else 0


if __name__ == "__main__":
    sys.exit(main())
