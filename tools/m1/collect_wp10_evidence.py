#!/usr/bin/env python
"""WP10 证据采集脚本。

**为什么要有脚本而不是手工敲命令再粘贴日志**：
证据的价值在于"可复现"。手工粘贴的日志无法证明"这真的是当前代码跑出来的" ——
代码改过之后没人知道日志是不是旧的。脚本把"跑什么、记什么"固定下来，
任何人（或 CI）重跑一次就能得到等价证据。

产出的文件（全部落在 `evidence/m1/`）：

| 文件 | 内容 |
|---|---|
| `backend-pytest.log` | 后端全量 pytest 结果与计数 |
| `contract-export.log` | 契约导出 + `--check` 门禁结果 |
| `wp10-projects.log` | WP10 端点清单、路由注册核对、迁移与表结构核对 |

设计约定：
  - **一律用绝对路径**。相对路径会随调用方的 cwd 变化而失效（本机踩过坑）。
  - **记录退出码**。只看输出不看退出码会漏掉"命令失败但打印了看似正常的内容"。
  - **不用 shell 通配符**。清理与枚举都用明确的路径，避免误删（zsh 下
    `rm f*` 曾因中断把后续命令一起吞掉）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "services" / "backend"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m1"

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

# pytest 汇总行的样子："308 passed in 12.34s" / "1 failed, 307 passed in ..."
_PYTEST_SUMMARY_RE = re.compile(r"\b\d+ (?:passed|failed|error|errors|skipped)\b")
# 进度行："....F....   [ 46%]"
_PYTEST_PROGRESS_RE = re.compile(r"^([.FEsx]+)\s+\[")


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def _pytest_result(output: str, code: int) -> str:
    """从 pytest 输出里提取一行人可读的结果。

    **为什么不能简单地"取最后一行"**：`pyproject.toml` 的 `addopts` 里已经
    有 `-q`。若 CLI 再传一个 `-q`，两个会叠加成 `-qq` —— 那不仅压掉测试名，
    连最后的汇总行（`308 passed in 12.34s`）也一起压掉。此时最后一行只剩
    进度条 `....................  [100%]`，而退出码仍是 0，
    于是证据文件里会留下一行看起来"正常"、实则毫无信息量的 RESULT。

    因此这里优先抓汇总行；抓不到就退化为数进度标记，并在文案里写明
    "本轮无汇总行"，让读证据的人知道这是降级信息而不是真实结论。
    """
    for line in reversed(output.splitlines()):
        if _PYTEST_SUMMARY_RE.search(line):
            return line.strip()

    passed = failed = 0
    for line in output.splitlines():
        match = _PYTEST_PROGRESS_RE.match(line)
        if match:
            marks = match.group(1)
            passed += marks.count(".")
            failed += marks.count("F") + marks.count("E")

    if passed or failed:
        return (
            f"{passed} 个通过标记 / {failed} 个失败标记"
            f"（无汇总行，进度标记统计），exit={code}"
        )
    return f"NO SUMMARY（无汇总行且无进度标记），exit={code}"


def _run(
    args: list[str],
    *,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """跑一条命令，返回 (退出码, stdout+stderr)。"""
    env = os.environ.copy()
    # 本机环境代理会劫持到 localhost 的连接（见已登记的 ISS）。
    env.update(
        {
            "NO_PROXY": "*",
            "no_proxy": "*",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "PYTHONPATH": str(BACKEND_DIR / "src"),
        }
    )
    if extra_env:
        env.update(extra_env)

    completed = subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _write(name: str, lines: list[str]) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / name
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# 被采集的两段内联脚本。
# 用常量而不用 f-string 拼接：脚本里有 `%` 与 `{}`，拼接容易出错且难读。
# ---------------------------------------------------------------------------

ROUTES_SCRIPT = r"""
import json
from rednote.main import create_app
from rednote.platform.configuration import Settings

app = create_app(
    Settings(
        database_url="postgresql+psycopg://rednote@localhost:5432/postgres",
        environment="test",
        oidc_issuer="https://idp.test.invalid/realms/rednote",
        oidc_token_encryption_key="dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM=",
    )
)
routes = sorted(
    (method, route.path)
    for route in app.routes
    if hasattr(route, "methods")
    for method in (route.methods or set())
    if method in {"GET", "POST", "PATCH", "DELETE"}
)
print(json.dumps(routes, ensure_ascii=False))
"""

# 表结构核对。**问库而不是问 ORM** —— ORM 定义与迁移实际落库的结果可能不一致
# （autogenerate 漏项、人工审校改错都是真实发生过的事）。
INSPECT_SCRIPT = r"""
from sqlalchemy import create_engine, text

engine = create_engine("postgresql+psycopg://rednote@localhost:5432/postgres")
CHECKS = [
    ("work_items 列数", "SELECT count(*) FROM information_schema.columns WHERE table_name='work_items'", 11),
    ("project_assignments 列数", "SELECT count(*) FROM information_schema.columns WHERE table_name='project_assignments'", 9),
    ("brands.lock_version 存在", "SELECT count(*) FROM information_schema.columns WHERE table_name='brands' AND column_name='lock_version'", 1),
    ("products.lock_version 存在", "SELECT count(*) FROM information_schema.columns WHERE table_name='products' AND column_name='lock_version'", 1),
]
with engine.connect() as conn:
    for label, sql, expected in CHECKS:
        actual = conn.execute(text(sql)).scalar_one()
        flag = "OK  " if actual == expected else "FAIL"
        print(f"CHECK {flag} {label}: {actual} (期望 {expected})")
    version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    print(f"CHECK OK   alembic_version: {version}")
    partial = conn.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname='public' AND indexdef LIKE '%WHERE%' "
            "ORDER BY indexname"
        )
    ).all()
    for name, definition in partial:
        where = definition.split("WHERE", 1)[1].strip()
        print(f"CHECK OK   部分索引 {name} WHERE {where}")
"""


def _delivered_wps() -> list[int]:
    """从实际存在的测试文件反推"已交付的工作包编号"。"""
    tests_dir = BACKEND_DIR / "tests"
    found: set[int] = set()
    for path in tests_dir.glob("test_*_wp*.py"):
        match = re.search(r"_wp(\d+)\.py$", path.name)
        if match:
            found.add(int(match.group(1)))
    return sorted(found)


def _non_wp_test_files() -> list[str]:
    """不按 WP 编号命名的测试文件（骨架 + 跨包共用）。

    **为什么要现场派生而不是写死名单**：原先 `_BASE_COVERAGE` 把骨架名单写死成
    「骨架 + WP08 identity + WP09 权限 + 契约门禁」，于是后加的
    `test_schema_parity.py`（跨包 Schema 判据）与 `test_devtools.py`（dev 登录旁路）
    **一条都不在上面** —— 而它们的用例数**算进了** `RESULT=1539 passed`。
    读证据的人据此会以为 1539 全来自 WP 包，实际上有 25 条来自这两处。
    这正是本仓铁律「证据的元信息必须现场派生，写死就注定过期且不报错」的同族：
    覆盖范围少报**不会报错**，只会让人对整份证据的解读出错。
    """
    tests_dir = BACKEND_DIR / "tests"
    return sorted(
        path.name
        for path in tests_dir.glob("test_*.py")
        if not re.search(r"_wp\d+\.py$", path.name)
    )


def _coverage_line() -> str:
    wps = _delivered_wps()
    span = f"WP{min(wps):02d}~WP{max(wps):02d}" if wps else "WP00"
    listed = "、".join(f"WP{n:02d}" for n in wps)
    others = _non_wp_test_files()
    others_txt = "、".join(others) if others else "（无）"
    return (
        f"# 覆盖: 按包测试 {listed}（区间 {span}）"
        f" + 非 WP 编号测试 {others_txt}"
    )


def collect_pytest() -> tuple[Path, bool]:
    # 不加 `-q`：pyproject 的 addopts 已经有它了，再加一个会变成 `-qq`
    # 从而连汇总行一起压掉（见 `_pytest_result` 的说明）。
    code, output = _run(
        [PYTHON, "-m", "pytest", "-p", "no:cacheprovider"],
        cwd=BACKEND_DIR,
    )
    lines = [
        "# 后端全量 pytest 实跑证据（全仓，非单包）",
        f"# 生成时间: {_now()}",
        _coverage_line(),
        "# 命令: cd services/backend && PYTHONPATH=src python -m pytest"
        "（`-q` 由 pyproject 的 addopts 提供）",
        "",
        output.rstrip(),
        "",
        f"EXIT={code}",
        f"RESULT={_pytest_result(output, code)}",
    ]
    return _write("backend-pytest.log", lines), code == 0


def collect_contract() -> tuple[Path, bool]:
    script = REPO_ROOT / "tools" / "contracts" / "export_openapi.py"
    write_code, write_output = _run([PYTHON, str(script)], cwd=REPO_ROOT)
    check_code, check_output = _run([PYTHON, str(script), "--check"], cwd=REPO_ROOT)

    contract = REPO_ROOT / "contracts" / "openapi.json"
    schema = json.loads(contract.read_text(encoding="utf-8"))
    operations = sum(
        1
        for item in schema["paths"].values()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    )
    component_schemas = schema.get("components", {}).get("schemas", {})

    lines = [
        "# 契约导出与门禁证据（全仓）",
        f"# 生成时间: {_now()}",
        "",
        "## [1] 导出",
        f"$ python tools/contracts/export_openapi.py  ->  exit {write_code}",
        write_output.rstrip(),
        "",
        "## [2] 契约统计",
        f"openapi: {schema['openapi']}",
        f"paths: {len(schema['paths'])}",
        f"operations: {operations}",
        f"component schemas: {len(component_schemas)}",
        "",
        "## [3] 框架默认错误模型不得混入契约（WP07 已踩坑）",
        f"HTTPValidationError present: {'HTTPValidationError' in component_schemas}",
        f"ValidationError present: {'ValidationError' in component_schemas}",
        "",
        "## [4] 分页响应模型（列表端点必须有 page 块）",
        *[
            f"  {name}"
            for name in sorted(component_schemas)
            if name.startswith("PaginatedEnvelope")
        ],
        "",
        "## [5] 端点清单",
        *[
            f"  {method.upper():6} {path:48} {item[method]['operationId']}"
            for path, item in sorted(schema["paths"].items())
            for method in sorted(item)
            if method in {"get", "post", "put", "patch", "delete"}
        ],
        "",
        "## [6] --check 门禁（一致应退出 0）",
        f"$ python tools/contracts/export_openapi.py --check  ->  exit {check_code}",
        check_output.rstrip(),
    ]
    return _write("contract-export.log", lines), (write_code == 0 and check_code == 0)


def collect_wp10() -> tuple[Path, bool]:
    """WP10 专项：路由注册 + 表结构 + 迁移版本。"""
    routes_code, routes_output = _run(
        [PYTHON, "-c", ROUTES_SCRIPT], cwd=BACKEND_DIR
    )
    routes: list[list[str]] = []
    if routes_code == 0:
        # 只取最后一行 JSON：SQLAlchemy / pydantic 的告警可能混在 stdout 前部。
        for line in reversed(routes_output.splitlines()):
            if line.strip().startswith("["):
                routes = json.loads(line)
                break

    # 表结构核对：**直接问库**，而不是相信 ORM 定义。
    # 用 psycopg（项目已有依赖）而不是 psql 客户端 ——
    # 后者在本机的路径不固定（实测 `psql` 不在 PATH 上），
    # 一个"证据脚本"不该因为客户端二进制找不到而失败。
    inspect_code, inspect_output = _run(
        [PYTHON, "-c", INSPECT_SCRIPT], cwd=BACKEND_DIR
    )
    struct_lines = [
        line for line in inspect_output.splitlines() if line.startswith("CHECK ")
    ]

    lines = [
        "# WP10 项目与字典：端点、表结构、迁移版本",
        f"# 生成时间: {_now()}",
        "",
        "## [1] 路由注册核对（/api/v1 下全部已注册端点，逐个列出）",
        f"$ python -c '<create_app + 打印路由>'  ->  exit {routes_code}",
    ]
    if routes:
        wt10 = [r for r in routes if r[1].startswith("/api/v1/")]
        lines += [f"  {method:6} {path}" for method, path in wt10]
        lines += ["", f"  /api/v1 下共 {len(wt10)} 个端点"]
    else:
        lines += ["  <未取到路由清单>", routes_output.rstrip()]

    lines += [
        "",
        "## [2] 表结构核对（直接问库，不信 ORM 定义）",
        f"$ python -c '<information_schema + pg_indexes 查询>'  ->  exit {inspect_code}",
        *[f"  {line}" for line in struct_lines],
        "",
        "  期望：work_items=11 列、project_assignments=9 列、"
        "brands/products 均有 lock_version。",
        "  alembic_version 只**打印不断言**（迁移 head 单调推进，写成等式会在下一个包失效，见 ISS-44/ISS-71）。",
        "  部分索引的 WHERE 条件必须完整保留 —— 少了 WHERE，",
        "  `uq_project_members_active_owner` 会退化成「每项目只能有一条成员记录」。",
    ]
    return _write("wp10-projects.log", lines), routes_code == 0 and inspect_code == 0


def main() -> int:
    results = []
    for collector in (collect_pytest, collect_contract, collect_wp10):
        path, ok = collector()
        results.append((path.name, ok))
        print(f"{'OK  ' if ok else 'FAIL'} {path}")

    failed = [name for name, ok in results if not ok]
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print(f"OK: {len(results)} 份证据已刷新")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
