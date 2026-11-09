#!/usr/bin/env python
"""WP-DEBT-05：**测试库数据体量判据的反向验证**（把错误实现放回去，判据必须报红）。

产出：`evidence/m3/test-data-volume-reverse.log`

## 为什么需要这个文件（与判据纪律第 9 条同一句话）

判据 D（`check_test_data_volume.py`）此刻打印 `[PASS]`，但那只证明"当前实现
看起来合规"，不证明"它被削弱时会报红"。本脚本把"它会报红"从**声明**变成
**可复跑的证据**——完全照 WP-DEBT-02 的 `collect_test_db_credibility_reverse_evidence.py`
已验证过的模式：整仓复制到唯一临时目录、副本只读变异、断言报红/假绿、还原、
断言真树未被触碰。

## 与 WP-DEBT-02 的差异：用一次性临时库，不碰共享 `rednote_test`

判据 D 很简单（只读三个 `count(*)`），不需要像判据 B 那样真跑一遍全量 pytest
（2~3 分钟），也不需要像判据 C 那样跑 alembic/create_all。本脚本自建
**三个一次性临时库**（带 pid，`finally` 里 `DROP DATABASE`），精确控制三张
根表的行数，从而不依赖共享库的当前状态，也不会被共享库的并发写入污染
（ISS-160 的教训：真库判据的反向验证必须自己独立，不借道共享库）。

## 变异清单（每条对应一个真实失效面）

| # | 变异 | 期望 | 它证明的失效面 |
|---|---|---|---|
| B0 | 无变异，三表均低 | `[PASS]` | 判据基线是绿的 |
| B1 | 无变异，`projects` 恰为阈值 | `[PASS]` | 等于阈值必须放行（测量基准本身该是绿的） |
| R1 | 无变异，`projects` 恰为阈值+1 | `[FAIL]` + `projects` | 判据的基本盘：超阈值真的会报红 |
| M1 | `rows > limit` → `rows > limit * 100` | 假绿 | 红的原因是阈值比较在工作，不是别的巧合 |
| M2 | 计数 SQL 的表名参数写死成 `brands` | 假绿 | 证明每张表读的是自己的计数，不是共用一份 |
| M3 | `ROOT_TABLES` 缩到只剩 `("brands",)` | 假绿 | 覆盖范围来自这个名单，不是巧合覆盖了 |
| M4 | 缺表分支 `rows=None,problem=...` → `rows=0` | 假绿 | "缺表"不许被静默折算成"0 行通过"（ISS-143 同族） |
| M5 | `_test_dsn()` 忽略环境变量，直接返回 `DEFAULT_TEST_DSN` | 假绿 | 环境变量覆盖是并发开发线隔离的必要机制 |
| M6 | 连接失败分支 `problem=...` → `rows=0` | 假绿 | "连不上"不许被静默折算成"0 行通过" |
| M7 | `SAFETY_FACTOR` 改成 100000 | 假绿 | 证明 `MAX_ROWS` 真的在驱动判定，不是摆设 |
| R2 | 还原后重跑 B0 | `[PASS]` | 证明前面是"变异"而非"破坏" |
| O1 | 本脚本写盘范围 vs 取证范围 | 不相交 | "真树未被触碰"这句话要有结构依据 |
| O2 | 真树整树哈希（排除窗口前已脏路径） | 前后一致 | 本工具没有意外改坏真树 |

跑法：
    python tools/quality/collect_test_data_volume_reverse_evidence.py
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"
OUT_FILE = EVIDENCE_DIR / "test-data-volume-reverse.log"

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

TOOL_REL = "tools/quality/check_test_data_volume.py"

#: 取证范围：整树（与 WP-DEBT-02 同口径），只哈希几个文件是"自证清白"。
TREE_SCOPE = ("services/backend", "migrations", "tools")

#: 本脚本唯一会写盘的地方（相对仓库根）。结构化写出，供 `_own_writes_are_disjoint`
#: 当场核对——"真树未被触碰"这句话必须有依据，不能是作者自觉（ISS-95）。
_OWN_WRITE_PATHS = (str(OUT_FILE.relative_to(REPO_ROOT)),)

#: 探针：`_copy_is_alive` 用它判断副本取证中途是否消失（ISS-156/157 教训：
#: 路径必须相对仓库根、且必须在 `_probe_is_sound()` 里用真树验一遍自己没写错）。
_COPY_PROBES = (
    "tools/quality/check_test_data_volume.py",
    "services/backend/tests/test_test_data_volume.py",
)

_EXCLUDE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _snapshot(repo: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in TREE_SCOPE:
        base = repo / rel
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix == ".pyc":
                continue
            if any(part in _EXCLUDE_DIRS for part in path.parts):
                continue
            out[str(path.relative_to(repo))] = _sha(path)
    return out


def _own_writes_are_disjoint() -> tuple[bool, str]:
    clashes = [
        rel
        for rel in _OWN_WRITE_PATHS
        if any(rel == scope or rel.startswith(scope + "/") for scope in TREE_SCOPE)
    ]
    if clashes:
        return False, f"本脚本会写 {clashes}，它落在取证范围 {TREE_SCOPE} 内"
    return True, "写盘范围与取证范围不相交（副本在 /tmp，日志在 evidence/）"


def _concurrently_modified_paths() -> set[str]:
    """取证窗口开始之前就已经处于 git 脏状态的路径（ISS-154 同款隔离）。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode != 0:
        return set()
    out: set[str] = set()
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        rel = line[3:].strip().strip('"')
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1].strip().strip('"')
        out.add(rel)
    return out


def _make_copy() -> Path:
    """整仓复制到唯一临时目录（`mkdtemp`，ISS-156：不写死目录名）。"""
    base = Path(tempfile.mkdtemp(prefix="rdvol-rev-"))
    dest = base / "repo"
    shutil.copytree(
        REPO_ROOT,
        dest,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc", "evidence"),
    )
    return dest


def _probe_is_sound() -> bool:
    """在真树上验一遍：`_COPY_PROBES` 指向的东西确实存在（ISS-157 教训）。"""
    return all((REPO_ROOT / rel).exists() for rel in _COPY_PROBES)


def _copy_is_alive(repo: Path) -> bool:
    probes = (repo, *(repo / rel for rel in _COPY_PROBES))
    return all(p.exists() for p in probes)


def _run(repo: Path, *, env_overrides: dict[str, str]) -> tuple[int, str]:
    """在副本里跑判据 D，返回 `(退出码, 合并输出)`。判据本身是秒级的，30s 已有余量。"""
    env = {**os.environ, **env_overrides}
    try:
        proc = subprocess.run(
            [PYTHON, str(repo / TOOL_REL)],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return 124, f"[TIMEOUT] {TOOL_REL} 超过 30s 未返回"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ===========================================================================
# 一次性临时库：三个根表的行数由本脚本精确控制，不依赖/不污染共享库
# ===========================================================================


def _maintenance_dsn(test_dsn: str) -> str:
    """把测试 DSN 换成同主机的 `postgres` 维护库连接（用于 CREATE/DROP DATABASE）。"""
    prefix, _, _ = test_dsn.rpartition("/")
    return f"{prefix}/postgres"


#: 一次性临时库的 host/user 前缀必须可被环境变量覆盖（ISS-108：CI 的角色是
#: `postgres` 而非 `rednote`），与 `check_migration_roundtrip.py::_base_dsn` 同一手法 ——
#: 直接 f-string 拼 `rednote@localhost` 在本机能跑，CI 一接上就是 `role does not exist`。
_DEFAULT_BASE_DSN = "postgresql+psycopg://rednote@localhost:5432/rednote_test"


def _base_dsn_prefix() -> str:
    """取可被环境变量覆盖的基准 DSN 的 host/user 前缀（不含库名）。"""
    base = os.environ.get(
        "REDNOTE_DATABASE_URL", os.environ.get("REDNOTE_TEST_DATABASE_URL", _DEFAULT_BASE_DSN)
    )
    prefix, _, _ = base.rpartition("/")
    return prefix


def _create_database(maint_dsn: str, name: str) -> None:
    import psycopg

    libpq = maint_dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(libpq, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')  # noqa: S608


def _drop_database(maint_dsn: str, name: str) -> None:
    import psycopg

    libpq = maint_dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        with psycopg.connect(libpq, autocommit=True) as conn:
            conn.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity"
                " where datname = %s and pid <> pg_backend_pid()",
                (name,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')  # noqa: S608
    except Exception:  # noqa: BLE001
        pass


def _seed_tables(dsn: str, counts: dict[str, int]) -> None:
    """建表（若不存在）并把每张表的行数精确设成 `counts[table]`（先 TRUNCATE 再灌）。"""
    import psycopg

    libpq = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(libpq, autocommit=True) as conn:
        for table, n in counts.items():
            conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" (id serial primary key)')  # noqa: S608
            conn.execute(f'TRUNCATE "{table}"')  # noqa: S608
            if n > 0:
                conn.execute(
                    f'INSERT INTO "{table}" SELECT * FROM generate_series(1, %s)',  # noqa: S608
                    (int(n),),
                )


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT / "tools" / "quality"))
    import check_test_data_volume as real_tool

    checks: list[tuple[str, bool, str]] = []
    lines: list[str] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    def emit(text: str = "") -> None:
        lines.append(text)

    lines.append("# WP-DEBT-05 测试库数据体量判据的反向验证（把错误实现放回去，判据必须报红）")
    lines.append(f"# 生成时间: {_now()}")
    lines.append(f"# 命令: python tools/quality/collect_test_data_volume_reverse_evidence.py")
    lines.append(f"# 被测判据: {TOOL_REL}")
    lines.append("")

    dirty_at_start = _concurrently_modified_paths()
    before = _snapshot(REPO_ROOT)

    emit("## [-1] 探针自校验：`_COPY_PROBES` 用的路径在真树上必须存在")
    probe_ok = _probe_is_sound()
    add("probe-sound", probe_ok, f"{len(_COPY_PROBES)} 个探针路径在真树上均存在")
    emit(f"  {'OK ' if probe_ok else 'BAD'}  探针自校验 → {'真树里路径齐全' if probe_ok else '路径写错了'}")
    for rel in _COPY_PROBES:
        emit(f"        | {'存在' if (REPO_ROOT / rel).exists() else '缺失'} {rel}")
    emit()

    repo = _make_copy()
    maint_dsn = None
    main_db = f"rednote_volume_rev_{os.getpid()}"
    missing_db = f"rednote_volume_rev_{os.getpid()}_missing"
    clean_db = f"rednote_volume_rev_{os.getpid()}_clean"
    main_dsn = f"{_base_dsn_prefix()}/{main_db}"
    missing_dsn = f"{_base_dsn_prefix()}/{missing_db}"
    clean_dsn = f"{_base_dsn_prefix()}/{clean_db}"
    maint_dsn = _maintenance_dsn(main_dsn)

    low = {t: 10 for t in real_tool.ROOT_TABLES}
    projects_limit = real_tool.MAX_ROWS["projects"]
    over = {**low, "projects": projects_limit + 1}
    boundary = {**low, "projects": projects_limit}

    try:
        _create_database(maint_dsn, main_db)
        _create_database(maint_dsn, missing_db)
        _create_database(maint_dsn, clean_db)
        _seed_tables(missing_dsn, {"projects": 1, "users": 1})  # 刻意不建 brands
        _seed_tables(clean_dsn, low)

        def run_case(
            *,
            label: str,
            name: str,
            edits: list[tuple[str, str]],
            dsn_counts: dict[str, int] | None,
            env_overrides: dict[str, str],
            expect_marker: str,
            expect_exit_nonzero: bool,
            forbid_marker: str | None = None,
        ) -> None:
            """`edits` 是 `(old, new)` 对——本判据只有一个文件，不需要按路径分组。"""
            if not _copy_is_alive(repo):
                add(name, False, "副本已消失（并发实例删掉了它？见 ISS-156）")
                emit(f"  BAD  {label} —— 变异副本在取证中途消失：{repo}")
                return

            if dsn_counts is not None:
                _seed_tables(main_dsn, dsn_counts)

            tool_path = repo / TOOL_REL
            original = tool_path.read_text(encoding="utf-8")
            for old, _new in edits:
                if old not in original:
                    add(name, False, f"变异锚点未找到：{old[:60]!r}")
                    emit(f"  BAD  {label} —— 变异锚点未找到（脚本与代码已脱节）")
                    return
            try:
                mutated = original
                for old, new in edits:
                    mutated = mutated.replace(old, new, 1)
                tool_path.write_text(mutated, encoding="utf-8")

                code, out = _run(repo, env_overrides=env_overrides)
                hit = expect_marker in out
                exit_ok = (code != 0) if expect_exit_nonzero else (code == 0)
                vetoed = bool(forbid_marker) and forbid_marker in out
                ok = exit_ok and hit and not vetoed
                detail = f"exit={code}，{'命中' if hit else '未命中'} {expect_marker!r}"
                if forbid_marker:
                    detail += f"，{'出现了' if vetoed else '未出现'} {forbid_marker!r}"
                add(name, ok, detail)
                emit(f"  {'OK ' if ok else 'BAD'}  {label} → exit={code}")
                for line in out.splitlines():
                    s = line.strip()
                    if not s:
                        continue
                    if (
                        expect_marker in line
                        or (forbid_marker and forbid_marker in line)
                        or s.startswith(("[FAIL]", "[PASS]"))
                    ):
                        emit(f"        | {s[:160]}")
            finally:
                tool_path.write_text(original, encoding="utf-8")

        emit("## [0] 基线：三表均低，判据必须绿")
        run_case(
            label="B0 基线绿",
            name="B0-baseline-green",
            edits=[],
            dsn_counts=low,
            env_overrides={"REDNOTE_TEST_DATABASE_URL": main_dsn},
            expect_marker="[PASS]",
            expect_exit_nonzero=False,
            forbid_marker="[FAIL]",
        )

        emit("## [1] 边界：projects 恰为阈值，必须放行（等于不算超）")
        run_case(
            label="B1 边界=阈值",
            name="B1-boundary-equal-passes",
            edits=[],
            dsn_counts=boundary,
            env_overrides={"REDNOTE_TEST_DATABASE_URL": main_dsn},
            expect_marker="[PASS]",
            expect_exit_nonzero=False,
            forbid_marker="[FAIL]",
        )

        emit("## [2] projects 恰为阈值+1，判据必须报红并指名 projects")
        run_case(
            label="R1 超阈值报红",
            name="R1-red-when-over-threshold",
            edits=[],
            dsn_counts=over,
            env_overrides={"REDNOTE_TEST_DATABASE_URL": main_dsn},
            expect_marker="projects",
            expect_exit_nonzero=True,
        )

        emit("## [3] M1：把 `rows > limit` 改成 `rows > limit * 100`（断言假绿）")
        run_case(
            label="M1 阈值比较被废",
            name="M1-comparison-defeated-gives-false-pass",
            edits=[("reading.rows > limit:", "reading.rows > limit * 100:")],
            dsn_counts=None,  # 复用上一步 over 的库状态
            env_overrides={"REDNOTE_TEST_DATABASE_URL": main_dsn},
            expect_marker="[PASS]",
            expect_exit_nonzero=False,
            forbid_marker="[FAIL]",
        )

        emit("## [4] M2：计数 SQL 的表名参数写死成 brands（断言假绿）")
        run_case(
            label="M2 计数串表",
            name="M2-count-cross-wired-gives-false-pass",
            edits=[
                (
                    'cur.execute(f\'select count(*) from "{table}"\')',
                    'cur.execute(\'select count(*) from "brands"\')',
                )
            ],
            dsn_counts=over,
            env_overrides={"REDNOTE_TEST_DATABASE_URL": main_dsn},
            expect_marker="[PASS]",
            expect_exit_nonzero=False,
            forbid_marker="[FAIL]",
        )

        emit("## [5] M3：ROOT_TABLES 缩到只剩 (\"brands\",)（断言假绿）")
        run_case(
            label="M3 根表名单缩水",
            name="M3-root-tables-shrunk-gives-false-pass",
            edits=[
                (
                    'ROOT_TABLES: tuple[str, ...] = ("projects", "users", "brands")',
                    'ROOT_TABLES: tuple[str, ...] = ("brands",)',
                )
            ],
            dsn_counts=over,
            env_overrides={"REDNOTE_TEST_DATABASE_URL": main_dsn},
            expect_marker="[PASS]",
            expect_exit_nonzero=False,
            forbid_marker="[FAIL]",
        )

        emit("## [6] M4：缺表分支改成 rows=0（断言假绿，用缺 brands 表的库）")
        run_case(
            label="M4 缺表被吞",
            name="M4-missing-table-swallowed-gives-false-pass",
            edits=[
                (
                    'readings.append(\n'
                    '                            TableReading(\n'
                    '                                table=table,\n'
                    '                                rows=None,\n'
                    '                                problem="表不存在（未迁移到该库，还是连错库了？）",\n'
                    '                            )\n'
                    '                        )\n'
                    '                        continue',
                    'readings.append(TableReading(table=table, rows=0))\n'
                    '                        continue',
                )
            ],
            dsn_counts=None,
            env_overrides={"REDNOTE_TEST_DATABASE_URL": missing_dsn},
            expect_marker="[PASS]",
            expect_exit_nonzero=False,
            forbid_marker="[FAIL]",
        )

        emit("## [7] M5：_test_dsn() 忽略环境变量，直接返回 DEFAULT_TEST_DSN（断言假绿）")
        run_case(
            label="M5 DSN 被写死",
            name="M5-dsn-hardcoded-ignoring-env-gives-false-pass",
            edits=[
                (
                    f'DEFAULT_TEST_DSN = "{real_tool.DEFAULT_TEST_DSN}"',
                    f'DEFAULT_TEST_DSN = "{clean_dsn}"',
                ),
                (
                    "def _test_dsn() -> str:\n    return os.environ.get(TEST_DATABASE_URL_ENV, DEFAULT_TEST_DSN).strip()",
                    "def _test_dsn() -> str:\n    return DEFAULT_TEST_DSN",
                ),
            ],
            dsn_counts=over,  # main_dsn 仍是脏库；env 指向它，但 mutated 代码会忽略
            env_overrides={"REDNOTE_TEST_DATABASE_URL": main_dsn},
            expect_marker="[PASS]",
            expect_exit_nonzero=False,
            forbid_marker="[FAIL]",
        )

        emit("## [8] M6：连接失败分支改成 rows=0（断言假绿）")
        run_case(
            label="M6 连接失败被吞",
            name="M6-connection-failure-swallowed-gives-false-pass",
            edits=[
                (
                    'return [\n'
                    '            TableReading(\n'
                    '                table=t,\n'
                    '                rows=None,\n'
                    '                problem=f"连不上目标库（{type(exc).__name__}: {exc}）",\n'
                    '            )\n'
                    '            for t in tables\n'
                    '        ]',
                    'return [TableReading(table=t, rows=0) for t in tables]',
                )
            ],
            dsn_counts=None,
            env_overrides={"REDNOTE_TEST_DATABASE_URL": "postgresql+psycopg://rednote@localhost:1/rednote_unreachable"},
            expect_marker="[PASS]",
            expect_exit_nonzero=False,
            forbid_marker="[FAIL]",
        )

        emit("## [9] M7：SAFETY_FACTOR 改成 100000（断言假绿）")
        run_case(
            label="M7 阈值放大",
            name="M7-threshold-inflated-gives-false-pass",
            edits=[("SAFETY_FACTOR = 4", "SAFETY_FACTOR = 100000")],
            dsn_counts=over,
            env_overrides={"REDNOTE_TEST_DATABASE_URL": main_dsn},
            expect_marker="[PASS]",
            expect_exit_nonzero=False,
            forbid_marker="[FAIL]",
        )

        emit("## [10] 还原后重跑基线，必须回绿（证明前面是变异而非破坏）")
        run_case(
            label="R2 还原回绿",
            name="R2-restored-green",
            edits=[],
            dsn_counts=low,
            env_overrides={"REDNOTE_TEST_DATABASE_URL": main_dsn},
            expect_marker="[PASS]",
            expect_exit_nonzero=False,
            forbid_marker="[FAIL]",
        )
        emit()

        emit("## [11] 本工具写盘范围 vs 取证范围：不得相交")
        writes_ok, writes_detail = _own_writes_are_disjoint()
        add("own-writes-disjoint", writes_ok, writes_detail)
        emit(f"  {'OK ' if writes_ok else 'BAD'}  {writes_detail}")

        after = _snapshot(REPO_ROOT)
        changed = sorted(rel for rel in before if before.get(rel) != after.get(rel))
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))

        def _pre_existing(rel: str) -> bool:
            if rel in dirty_at_start:
                return True
            return any(rel == d or rel.startswith(d.rstrip("/") + "/") for d in dirty_at_start)

        foreign = sorted(rel for rel in (changed + added + removed) if _pre_existing(rel))
        own_changed = [rel for rel in changed if rel not in foreign]
        own_added = [rel for rel in added if rel not in foreign]
        own_removed = [rel for rel in removed if rel not in foreign]
        same = not own_changed and not own_added and not own_removed
        add("real-tree-untouched", same, f"{len(before)} 个文件哈希一致")
        emit(f"  {'OK ' if same else 'BAD'}  真树 {len(before)} 个文件哈希前后一致：{'是' if same else '否'}")
        for rel in own_changed:
            emit(f"        | 内容被改：{rel}")
        for rel in own_added:
            emit(f"        | 新增文件：{rel}")
        for rel in own_removed:
            emit(f"        | 文件丢失：{rel}")
        if foreign:
            emit(f"  —— 另有 {len(foreign)} 个路径窗口开始前就已脏，属并行会话（不计入本判据）")
        emit()
    finally:
        shutil.rmtree(repo.parent, ignore_errors=True)
        if maint_dsn:
            _drop_database(maint_dsn, main_db)
            _drop_database(maint_dsn, missing_db)
            _drop_database(maint_dsn, clean_db)

    failures = [name for name, ok, _ in checks if not ok]
    complete = len(checks) >= EXPECTED_CHECKS
    exact = len(checks) == EXPECTED_CHECKS
    emit("## [12] 结论")
    for name, ok, detail in checks:
        suffix = f"（{detail}）" if detail else ""
        emit(f"  {'PASS' if ok else 'FAIL'} {name}{suffix}")
    emit()
    verdict = not failures and complete and exact
    emit(
        f"RESULT={'PASS' if verdict else 'FAIL'} "
        f"checks={len(checks)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条，"
        f"条数核对 {'一致' if exact else '不一致'}）"
    )

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写出 {OUT_FILE.relative_to(REPO_ROOT)}")
    print(
        f"RESULT={'PASS' if verdict else 'FAIL'} "
        f"checks={len(checks)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条，"
        f"条数核对 {'一致' if exact else '不一致'}）"
    )
    for name in failures:
        print(f"  FAIL {name}")
    if not exact:
        print(f"  FAIL 条数核对：实际 {len(checks)} 条 ≠ 期望 {EXPECTED_CHECKS} 条")
    return 0 if verdict else 1


#: 探针 1 + B0 + B1 + R1 + M1~M7(7) + R2 + own-writes-disjoint + real-tree-untouched = 14
EXPECTED_CHECKS = 14


if __name__ == "__main__":
    raise SystemExit(main())
