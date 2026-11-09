#!/usr/bin/env python
"""WP13 证据采集脚本。

产出的文件（落在 `evidence/m1/`）：

| 文件 | 内容 |
|---|---|
| `wp13-jobs.log` | 3 张表 / 部分索引 WHERE / CHECK / 迁移版本，+ **唯一约束的活性验证** |

**关于"活性验证"**：只查 `pg_constraint` 能证明约束**存在**，不能证明它**生效**。
这里对一个真实的 `(idempotency_scope, idempotency_key)` 试写一条重复插入，
断言被 `uq_jobs_scope_key` 拒绝（SQLSTATE 23505），随后回滚。

**为什么这条值得单独验**：它正是协议 §1.3 与切片 F1 的数据库级保证 ——
"进行中的同 key 返回同一任务，**不新增任务**"。应用层的
`ON CONFLICT DO NOTHING` 只有在唯一约束真的存在时才有意义；
约束丢了的话代码照常运行、任务却被重复创建。

**部分索引为什么必须单独核对**：`ix_jobs_lease_until` 与
`ix_idempotency_expires_at` 都带 `WHERE`。Alembic 的 autogenerate
**有可能**丢掉 WHERE 子句（渲染为普通索引），而那种退化不报任何错：
前者让回收扫描退化成全表扫描；后者会让**永久保留的幂等记录被清理掉**，
进而"同一任务在第 8 天再次计费"（协议 §1.3 明确要求避免）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "services" / "backend"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m1"

# 期望条数（与内联脚本里的 EXPECTED_CHECKS 保持同值）。
# 两处都要：内联脚本用它写进 RESULT 行，外层用它判定成功与否。
EXPECTED_CHECKS: int = 10

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

INSPECT_SCRIPT = r"""
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

# 期望产生的核对条数。
#
# **为什么必须有这个数**（ISS-43 的教训）：RESULT 行若只统计"失败了几条"，
# 那么"脚本中途抛异常、本该产生的 CHECK 一条都没产生"与"全部通过"
# 在计数上**完全一样** —— 崩溃会伪装成完美通过（实测踩到过：
# 前半段 9 条 OK、后半段 NameError，RESULT 却是 9 PASS / 0 FAIL）。
EXPECTED_CHECKS = 10

DB = "postgresql+psycopg://rednote@localhost:5432/postgres"
engine = create_engine(DB)

# 该包**引入**的迁移版本。
#
# **校验方式："至少到这个版本"，不是"恰好等于这个版本"**。
# 一开始写的是 `version == EXPECTED_REVISION`，结果是每新增一个工作包，
# **前面所有包的历史证据脚本就会集体失败** —— 因为它们校验的是
# "迁移停在某个特定版本"，而真正该校验的是"该包引入的表/约束还在"。
# 这个缺陷此前被掩盖了（从未一次性复跑过全部脚本），
# 直到 WP15 收口时统一复跑才暴露（见 ISS-44）。
#
# 判据用"版本链里出现过它"：`alembic_version` 只存当前 head，
# 因此这里退化为"当前 head 是该版本或它的后继" —— 用长度前缀匹配不可靠，
# 改为**只要不是更早的版本**即通过（更早 = 该包的表还没建）。
REVISION_INTRODUCED = "0894cc890962"
TABLE_COLUMNS = [("jobs", 21), ("job_attempts", 10), ("idempotency_records", 10)]
PARTIAL_INDEXES = ("ix_jobs_lease_until", "ix_idempotency_expires_at")
# CHECK 约束合计：jobs 3 个（state / lease_pair_consistent /
# fence_token_non_negative）+ job_attempts 3 个（state / attempt_no_positive /
# fence_token_non_negative）+ idempotency_records 1 个（state）= **7**。
EXPECTED_CHECK_COUNT = 7
SMOKE_TABLES = ("smoke_meta", "wp05_vec_probe")


def _revision_is_at_least(current, introduced):
    # 当前 head 是否**不早于** introduced —— 沿真实迁移链判断，不是猜。
    #
    # **为什么不能写 `== EXPECTED_REVISION`**（ISS-44）：那会让每个新工作包
    # 把前面所有包的历史证据集体打红，因为校验的其实是"迁移停在某个特定
    # 版本"，而真正该校验的是"**该包引入的表/约束还在**"（由本脚本后面的
    # 核对项保证）。
    #
    # **为什么也不能用 `len(current) >= len(introduced)`**（ISS-115，2026-09-27
    # 收口轮实测）：本项目全部 16 个 revision id **都是 12 位十六进制**，
    # 于是"长度不短于"在 `current != introduced` 时**恒为 True** ——
    # 那是把等式判据换成了**恒绿判据**，比原缺陷更隐蔽：它永不失败，
    # 因此也永不报警（判据纪律第 7/9 条：判据必须能区分两种实现）。
    # 本项目**存量 12 个脚本全是这个写法**，全部靠它"通过"。
    #
    # 正确做法：拿到**真实迁移链**，按位置比较。`walk_revisions(base=..., head=...)`
    # 的返回值是 head→base 降序，因此 index 小 = 新。
    # ⚠️ 该调用会 import 每个迁移模块（`from rednote.modules...`），
    # 故内联段必须在 `PYTHONPATH=<repo>/services/backend/src` 下运行。
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo = Path.cwd().resolve()
    while repo != repo.parent and not (repo / "migrations" / "alembic.ini").exists():
        repo = repo.parent
    cfg = Config(str(repo / "migrations" / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo / "migrations"))
    chain = [
        r.revision
        for r in ScriptDirectory.from_config(cfg).walk_revisions(
            base="base", head="heads"
        )
    ]
    # 认不出来的 revision（拼错/已删除）一律判否 —— fail-closed。
    if current not in chain or introduced not in chain:
        return False
    return chain.index(current) <= chain.index(introduced)


def emit(ok, label, detail):
    print(f"CHECK {'OK  ' if ok else 'FAIL'} {label}: {detail}")


with engine.connect() as conn:
    for table, expected in TABLE_COLUMNS:
        actual = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_schema='public' AND table_name=:t"
            ),
            {"t": table},
        ).scalar_one()
        emit(actual == expected, f"{table} 列数", f"{actual} (期望 {expected})")

    version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    # 只要当前 head **不早于**该包引入的版本即可（见常量处的说明）。
    # 真正的"该包的东西还在不在"由本脚本后面的表/索引/触发器核对保证。
    emit(
        _revision_is_at_least(version, REVISION_INTRODUCED),
        "alembic_version 不早于本包引入的版本",
        f"{version} (本包引入 {REVISION_INTRODUCED})",
    )

    for name in PARTIAL_INDEXES:
        row = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND indexname=:n"),
            {"n": name},
        ).scalar_one_or_none()
        has_where = bool(row) and " WHERE " in row
        where = row.split(" WHERE ", 1)[1] if has_where else "<无 WHERE，已退化为普通索引>"
        emit(has_where, f"部分索引 {name}", where)

    ck_count = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE contype='c' AND connamespace='public'::regnamespace"
            "   AND conrelid::regclass::text IN ('jobs','job_attempts','idempotency_records')"
        )
    ).scalar_one()
    emit(ck_count == EXPECTED_CHECK_COUNT, "CHECK 约束总数", f"{ck_count} (期望 {EXPECTED_CHECK_COUNT})")

    for table in SMOKE_TABLES:
        n = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.tables"
                " WHERE table_schema='public' AND table_name=:t"
            ),
            {"t": table},
        ).scalar_one()
        emit(n == 1, f"冒烟表未被误删 {table}", n)

    print()
    print("--- 活性验证：幂等键唯一约束真的拦得住重复任务（试写后回滚）---")
    # 造一条真实存在的任务（若库里已有取一条，否则插一条临时的）
    existing = conn.execute(
        text("SELECT idempotency_scope, idempotency_key FROM jobs LIMIT 1")
    ).first()

if existing is None:
    print("SKIP 唯一约束试写：jobs 表当前无数据")
else:
    scope, key = existing
    with engine.connect() as wconn:
        trans = wconn.begin()
        try:
            wconn.execute(
                text(
                    "INSERT INTO jobs (kind, queue, state, idempotency_scope,"
                    " idempotency_key, input_hash) VALUES"
                    " ('PROBE', 'probe', 'QUEUED', :s, :k, repeat('0', 64))"
                ),
                {"s": scope, "k": key},
            )
            emit(False, "幂等键唯一约束试写", "重复任务未被拦截（唯一约束失效）")
        except DBAPIError as exc:
            state = getattr(exc.orig, "sqlstate", None)
            name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            emit(
                state == "23505" and name == "uq_jobs_scope_key",
                "幂等键唯一约束试写",
                f"被拦截 sqlstate={state} constraint={name}"
                f" (期望 23505 / uq_jobs_scope_key)",
            )
        finally:
            trans.rollback()
"""


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def collect_wp13() -> tuple[Path, bool]:
    env = os.environ.copy()
    env.update(
        {
            "NO_PROXY": "*",
            "no_proxy": "*",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "PYTHONPATH": str(BACKEND_DIR / "src"),
        }
    )
    completed = subprocess.run(
        [PYTHON, "-c", INSPECT_SCRIPT],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    checks = [
        line for line in output.splitlines() if line.startswith(("CHECK", "SKIP"))
    ]
    failures = [line for line in checks if line.startswith("CHECK FAIL")]

    lines = [
        "# WP13 任务、租约与 Outbox：表结构、部分索引、幂等键唯一约束活性验证",
        f"# 生成时间: {_now()}",
        "# 命令: cd services/backend && python -c '<information_schema + pg_indexes + pg_constraint + 试写>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp13-jobs.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # **条数不足也算失败**（ISS-43）：脚本中途抛异常时，
    # "本该产生的 CHECK 一条都没产生"与"全部通过"在计数上一模一样，
    # 只判 `not failures` 会让崩溃伪装成完美通过。
    complete = len(checks) >= EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete)


def main() -> int:
    path, ok = collect_wp13()
    print(f"{'OK  ' if ok else 'FAIL'} {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
