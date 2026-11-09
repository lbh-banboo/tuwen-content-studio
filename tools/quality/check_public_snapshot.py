#!/usr/bin/env python3
"""公开快照不变式自检 —— 公开仓 CI 的机械判据。

## 为什么单独有这份判据（2026-11-09，根因留档）

本仓是内部研发仓 `rednote-production` 的**脱敏白名单快照**，不是研发工作区。
原 `.github/workflows/ci.yml` 的三道门被白名单一起复制了进来，但它的输入在
公开快照里根本不存在：

* 门 2 `pytest -m "not slow"`：3208 用例里有内部 fixture 硬编码本机角色与
  自建私有库，且 autouse 夹具无条件覆盖 DSN ⇒ 干净 runner 上必然
  `role ... does not exist` / `database ... does not exist`。本地干净私有库
  实测 3194 passed / 1 failed / 10 errors，其中 1 failed 是脱敏把负样本断言
  打成了恒假（见发布审计目录 06 号文件第八节）。
* 门 3 的 40+ 条内部护栏：`evidence/**/*.log`（公开快照 0 个，84 个 tools
  脚本引用它）、内部文档声明、离线 wheelhouse 真构建、docker daemon、
  私有库 createdb —— 输入全被移除，或根本不该在公开仓出网。

把这套 CI 原样留着不跑 = ISS-95「声称有防线」；跑 = 永久红，且红的原因与
公开产物质量无关。故公开仓 CI 只跑「装依赖 → 起 PG+pgvector → 迁移 →
契约门禁 → smoke → 本判据」，**不适用的理由写进 workflow 文件头本身**
（完整三道门仍在内部研发仓，未开源、未删除）。

## 覆盖范围由磁盘决定

* 文件清单 = `git ls-files`（含本次提交后的新增文件），不靠人写清单；
* 文本文件分类只看扩展名/文件名，二进制不进文本扫描（随机字节会造假红）；
* 文件数只设**下界**（初始发布基线 874），不写死相等：允许后续合法新增，
  上界由仓库外的隐私扫描 + 线上 trees API 三方对账守。

## 刻意不写进本文件的内容

真实邮箱、真实用户名、内网域名、凭证字段名等 PII 检测字面量，一律不写。
把「要找什么」的清单公开在仓库里既无意义又等于泄题；那一层由发布审计
目录的 `verify_opensource_snapshot.py` 在**仓库外**执行，本判据只守结构。

## --self-test

在 /tmp 临时副本上逐条注入违规，要求对应判据**精确报红**，撤回后回绿。
副本是整仓拷贝、结束即删，不碰真实工作树；失败返回原因并显式报红，
还原不用 assert（崩掉会把副本留在变异态）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

H1 = "# 图文内容生产"
SLUG = "tuwen-content-studio"
COPYRIGHT = "Tuwen Content Studio Contributors"
BASELINE_FILES = 874
META_FILES = ("CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md")
BANNED_SUFFIXES = (".log", ".tar.gz", ".db", ".sqlite", ".pem", ".key", ".p12", ".pfx")
# 只写形状、不写发布审计里的敏感词字面量：把「要找什么」的清单公开进仓库
# 既无意义又等于泄题。子串匹配仍能抓到完整目录名。
BANNED_PARTS = ("browser-profile", "01-repo.tar.gz")
TEXT_EXT = {
    ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".json", ".jsonl", ".md",
    ".txt", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".example",
    ".html", ".htm", ".css", ".sh", ".sql", ".csv", ".xml", ".lock", ".svg", ".rst",
}
TEXT_BASE = {
    ".gitignore", ".env.example", "LICENSE", "Makefile", "Dockerfile",
    "README.md", "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md",
}
# jobs.<job_id>.env 位置不提供 env 上下文；写成表达式会让整个 workflow 连
# job 都起不来（2026-11-09 实测 Unrecognized named-value: 'env'）。
BAD_ENV_CONTEXT_RE = re.compile(r"\$\{\{\s*env\.")
# ⚠️ 刻意不做「本机 home 路径形状」检测：那需要把「home 目录 + 任意用户名」
#    这类检测形状写进公开仓，既等于泄题，又会被发布门禁 preflight 的
#    G04/G06 当成真实本机路径判红（2026-11-09 实测 PASS=8 FAIL=2）。
#    该层由仓库外的 verify_opensource_snapshot.py 与线上归档隐私扫描负责，
#    本判据只守结构。


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _tracked(root: Path) -> list[str]:
    raw = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout
    return [p.decode("utf-8") for p in raw.split(b"\0") if p]


def _is_text(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in TEXT_EXT or os.path.basename(name) in TEXT_BASE


def check_all(root: Path) -> dict[str, bool]:
    """返回每条判据的布尔结果。root 可以是临时变异副本。"""
    readme = _read(root / "README.md")
    lic = _read(root / "LICENSE")
    workflow = _read(root / ".github" / "workflows" / "ci.yml")
    files = _tracked(root)
    out: dict[str, bool] = {}

    out["G01 README H1 = 中文展示名"] = readme.splitlines()[:1] == [H1]
    out["G02 README 标注 ASCII slug"] = SLUG in readme
    out["G03 LICENSE 为 MIT 且版权人中性"] = "MIT License" in lic and COPYRIGHT in lic
    out["G04 三份开源元数据齐备"] = all((root / f).is_file() for f in META_FILES)
    out["G05 跟踪文件数 >= 发布基线"] = len(files) >= BASELINE_FILES
    out["G06 无执行日志/打包快照/凭证文件"] = not [
        f for f in files
        if f.endswith(BANNED_SUFFIXES) or any(b in f for b in BANNED_PARTS)
    ]
    out["G07 提交内无 .git 对象"] = not [f for f in files if f.startswith(".git/")]

    wf_files = [f for f in files if f.startswith(".github/workflows/")]
    # 注释行是负样本：YAML 注释不会被 Actions 解析，文件头里"这个写法会报错"
    # 的说明文字必须显式排除，否则判据会把自己的说明当成违规（铁律 16）。
    # 只扫生效行；生效行里只允许 steps.with 的那一个合法 env 用法。
    illegal = []
    for f in wf_files:
        active = "\n".join(
            line for line in _read(root / f).splitlines()
            if not line.lstrip().startswith("#")
        ).replace("${{ env.PYTHON_VERSION }}", "")
        if BAD_ENV_CONTEXT_RE.search(active):
            illegal.append(f)
    out["G09 workflow 无 jobs.env 非法上下文"] = bool(wf_files) and not illegal
    out["G10 存在 workflow 文件"] = bool(wf_files)
    return out


def run(root: Path) -> int:
    res = check_all(root)
    failed = [k for k, v in res.items() if not v]
    for k, v in res.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print(f"=== 公开快照自检: {len(res) - len(failed)} PASS / {len(failed)} FAIL / {len(res)} 项 ===")
    if failed:
        print("FAILED:", "; ".join(failed))
        return 1
    return 0


def self_test(root: Path) -> int:
    """在整仓临时副本上注入违规 → 必红 → 撤回 → 回绿。"""
    cases: list[tuple[str, str, str, str]] = [
        ("README H1 回退旧名", "G01 README H1 = 中文展示名",
         "README.md", "# old-project-name\n\nrepo " + SLUG),
        ("README 去掉 slug", "G02 README 标注 ASCII slug",
         "README.md", "# 图文内容生产\n\nno slug here"),
        ("LICENSE 换掉中性版权人", "G03 LICENSE 为 MIT 且版权人中性",
         "LICENSE", "MIT License\n\nCopyright (c) 2026 Someone Else"),
        ("删掉 SECURITY.md", "G04 三份开源元数据齐备",
         "SECURITY.md", None),
        ("塞入执行日志并纳入跟踪", "G06 无执行日志/打包快照/凭证文件",
         "evidence/injected.log", "x"),
        ("workflow 注入非法 env 上下文", "G09 workflow 无 jobs.env 非法上下文",
         ".github/workflows/ci.yml", "env:\n  X: ${{ env.PG_DSN }}\n"),
    ]

    tmp = Path(tempfile.mkdtemp(prefix="public-snapshot-selftest-"))
    passed = 0
    failures: list[str] = []
    try:
        dst = tmp / "repo"
        shutil.copytree(root, dst, ignore=shutil.ignore_patterns(".pytest_tmp"))
        base = check_all(dst)
        base_bad = [k for k, v in base.items() if not v]
        if base_bad:
            return _fail(f"副本基线自身不绿，无法做反向验证: {base_bad}")

        for label, key, rel, payload in cases:
            target = dst / rel
            backup = target.read_bytes() if target.is_file() else None
            try:
                # 注入
                if payload is None:
                    target.unlink(missing_ok=True)
                elif rel.endswith(".log"):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(payload, encoding="utf-8")
                    # -f：.gitignore 有 *.log，变异必须强制纳入跟踪，
                    # 否则测的是「被忽略」而不是 G06 的「被跟踪」。
                    subprocess.run(["git", "add", "-N", "-f", rel], cwd=dst,
                                   capture_output=True, check=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    existing = _read(target)
                    # 保留 workflow 里唯一合法的 python-version 行，避免连带判红
                    target.write_text(
                        (existing + "\n" + payload) if rel.endswith("ci.yml") else payload,
                        encoding="utf-8",
                    )
                after = check_all(dst)
                if after.get(key) is not False:
                    failures.append(f"{label}: 注入后 {key} 未报红")
                    continue
                # 撤回
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(backup)
                if rel.endswith(".log"):
                    subprocess.run(["git", "reset", "-q", "--", rel], cwd=dst,
                                   capture_output=True, check=True)
                restored = check_all(dst)
                if restored.get(key) is not True:
                    failures.append(f"{label}: 撤回后 {key} 未回绿")
                    continue
                passed += 1
                print(f"  [PASS] {label} ⇒ {key} 精确报红，撤回回绿")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{label}: 副本异常 {type(exc).__name__}: {exc}")
                # 尽力还原，失败也不抛（副本会被整体删掉）
                try:
                    if backup is None:
                        target.unlink(missing_ok=True)
                    else:
                        target.write_bytes(backup)
                except Exception:
                    pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total = len(cases)
    print(f"=== 反向验证: {passed} PASS / {len(failures)} FAIL / {total} 项 ===")
    for f in failures:
        print("  FAIL:", f)
    return 1 if failures else 0


def _fail(reason: str) -> int:
    print("  FAIL:", reason)
    print("=== 反向验证: 0 PASS / 1 FAIL ===")
    return 1


def main() -> int:
    root = Path(os.environ.get("SNAPSHOT_ROOT", os.getcwd())).resolve()
    if "--self-test" in sys.argv:
        return self_test(root)
    return run(root)


if __name__ == "__main__":
    sys.exit(main())
