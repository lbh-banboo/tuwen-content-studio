#!/usr/bin/env python
"""手动清空共享测试库的**数据**（WP-DEBT-05）—— 只清行，不动结构。

## 它解决什么问题

`projects`/`users`/`brands` 是全仓几十张表的"无主根表"——`conftest.py` 的
`drop_own_tables` 会把它们排除在删除范围外（被跨包 FK 引用），`truncate_targets`
的闭包方向是"找引用者"，没有任何模块的 fixture 把它们列为自己的种子集去清。
结果：全仓 21 个测试文件各自建种子数据、无人清理，共享库 `rednote_test` 从
2026-09-30 到 2026-10-18（18 天）线性累积到 116k/166k/117k 行，全量 pytest
因此在共享库上超时（判据见 `tools/quality/check_test_data_volume.py`）。

本脚本是那条判据报红后的**手动**处置入口——按本项目"判据只报红、不自动修"
的纪律（`docs/m3/工程债-测试库可信度说明.md` §1.5），清理动作必须是人触发的。

## 安全门（顺序即防线）

1. **只认 `REDNOTE_TEST_DATABASE_URL`**——刻意不读 `REDNOTE_DATABASE_URL`，
   避免"连接串指错环境变量"时清掉不该清的库。
2. **保留库白名单拒绝**（复用判据 A 的 `STATIC_RESERVED_DATABASES`）。
3. **默认只读 dry-run**：不带 `--yes` 只打印目标库/闭包表数/当前行数，不建写连接。
4. **真执行需要 `--yes --confirm <库名>`**：两次确认，且 `--confirm` 必须**原样**
   等于解析出的库名——防止"复制了上一次的命令、忘了改库名"这种误执行。
5. **执行前再探一次活跃连接**（复用 `check_shared_db_quiet.assert_quiet`，
   ISS-160 教训：任何真连库操作都必须先确认没有并行会话）。
6. **超时兜底**：`lock_timeout`/`statement_timeout` 让"中途来了别的 pytest"
   变成一条可读的失败，而不是无限等锁。

## 为什么用 TRUNCATE 闭包而不是按 project_id 定向 DELETE

查过 `pg_constraint`：引用 `projects`/`users`/`brands` 的外键几乎全是 `RESTRICT`，
行级 `DELETE` 不可行。`TRUNCATE` 走独立路径能一次性处理整条 FK 链，但要求
**显式列出整条引用闭包**（`rednote.platform.db_graph.truncate_targets`，与
`conftest.py` 内其它夹具共用同一实现，不自己另写一份）。不用 `TRUNCATE ...
CASCADE`：显式列出让"多清了谁"永远可见（同 `drop_own_tables` 的 ISS-106 论证）。

跑法：
    python tools/dev/reset_shared_test_db.py            # 不带 --yes = 只读 dry-run
    python tools/dev/reset_shared_test_db.py --yes --confirm rednote_test
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

# 本脚本在 <repo>/tools/dev/ 下，parents[2] 是仓库根（与 seed_demo_data.py 同口径）。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_SRC = _REPO_ROOT / "services" / "backend" / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))
_QUALITY_DIR = _REPO_ROOT / "tools" / "quality"
if str(_QUALITY_DIR) not in sys.path:
    sys.path.insert(0, str(_QUALITY_DIR))

from rednote.platform.db_graph import truncate_targets  # noqa: E402

#: DSN 口径：只认 `REDNOTE_TEST_DATABASE_URL`（刻意不读 `REDNOTE_DATABASE_URL`，
#: 见模块 docstring 安全门 ①）。默认值与 `check_test_data_volume.py` /
#: `check_test_residue.py` / `conftest.py` 三处一致，靠对账测试防漂移，不互相 import。
import os  # noqa: E402

TEST_DATABASE_URL_ENV = "REDNOTE_TEST_DATABASE_URL"
DEFAULT_TEST_DSN = "postgresql+psycopg://rednote@localhost:5432/rednote_test"

#: 无主根表——与 `check_test_data_volume.ROOT_TABLES` 保持一致（对账测试守住）。
ROOT_TABLES: tuple[str, ...] = ("projects", "users", "brands")


def _test_dsn() -> str:
    return os.environ.get(TEST_DATABASE_URL_ENV, DEFAULT_TEST_DSN).strip()


def _libpq_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


@dataclass(frozen=True)
class ResetPlan:
    dsn: str
    database: str
    tables: list[str]
    before_counts: dict[str, int]


def _database_name(dsn: str) -> str | None:
    import check_test_database_target as target_tool

    return target_tool.database_name(dsn)


def _guard_reserved(database: str | None) -> None:
    import check_test_database_target as target_tool

    if database is None:
        raise SystemExit(f"拒绝执行：DSN 解析不出库名——{_test_dsn()!r}")
    if database in target_tool.STATIC_RESERVED_DATABASES:
        raise SystemExit(
            f"拒绝执行：目标库 {database!r} 是保留库"
            f"（{sorted(target_tool.STATIC_RESERVED_DATABASES)}）——本脚本只清测试库。"
        )


def build_plan(engine, *, database: str) -> ResetPlan:
    """只读：算出闭包表清单 + 三张根表当前行数。不执行任何写操作。"""
    from sqlalchemy import text

    tables = truncate_targets(engine, ROOT_TABLES)
    before_counts: dict[str, int] = {}
    with engine.connect() as conn:
        for table in ROOT_TABLES:
            row = conn.execute(text(f'select count(*) from "{table}"')).fetchone()  # noqa: S608
            before_counts[table] = int(row[0]) if row else 0
    return ResetPlan(dsn=_test_dsn(), database=database, tables=tables, before_counts=before_counts)


def execute(engine, plan: ResetPlan) -> dict[str, int]:
    """真正执行 TRUNCATE。调用方必须已经过 `--yes --confirm` 与 `assert_quiet` 两道门。

    设 `lock_timeout`/`statement_timeout`：中途来了别的 pytest/证据脚本时，
    这里会抛一条可读的超时错误，而不是无限挂起（第 17 条纪律的落地之一）。
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("SET lock_timeout = '5s'"))
        conn.execute(text("SET statement_timeout = '60s'"))
        conn.execute(text(f"TRUNCATE {', '.join(plan.tables)}"))  # noqa: S608

    after_counts: dict[str, int] = {}
    with engine.connect() as conn:
        for table in ROOT_TABLES:
            row = conn.execute(text(f'select count(*) from "{table}"')).fetchone()  # noqa: S608
            after_counts[table] = int(row[0]) if row else 0
    return after_counts


def _build_engine(dsn: str):
    from sqlalchemy import create_engine

    return create_engine(dsn, pool_pre_ping=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="清空共享测试库的数据（只清行，不动结构）——WP-DEBT-05"
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="真正执行清空（不带此参数时为只读 dry-run，这是默认行为）",
    )
    parser.add_argument(
        "--confirm",
        metavar="数据库名",
        help="必须原样等于目标库名，防止复制上一次命令时忘了改库名",
    )
    args = parser.parse_args(argv)

    dsn = _test_dsn()
    database = _database_name(dsn)
    print(f"目标库（{TEST_DATABASE_URL_ENV}）：{database}")
    _guard_reserved(database)

    if not args.yes:
        engine = _build_engine(dsn)
        try:
            plan = build_plan(engine, database=database)
        finally:
            engine.dispose()
        print(f"[dry-run] 闭包表数：{len(plan.tables)}")
        print(f"[dry-run] 闭包表（前 20）：{', '.join(plan.tables[:20])}")
        print("[dry-run] 三张根表当前行数：")
        for table, count in plan.before_counts.items():
            print(f"  - {table}: {count:,}")
        print("\n要真正执行，请加 --yes --confirm <上面打印的库名>")
        return 0

    if args.confirm != database:
        raise SystemExit(
            f"拒绝执行：--confirm 必须原样等于目标库名 {database!r}"
            f"（收到 {args.confirm!r}）——这道确认是为了防止复制上一次命令时忘了改库名。"
        )

    sys.path.insert(0, str(_QUALITY_DIR))
    import check_shared_db_quiet

    check_shared_db_quiet.assert_quiet(dsn, scope="reset_shared_test_db")

    engine = _build_engine(dsn)
    try:
        plan = build_plan(engine, database=database)
        print(f"即将 TRUNCATE {len(plan.tables)} 张表（闭包含跨包入向引用者）：")
        for table in plan.tables:
            print(f"  - {table}")
        print("清空前三张根表行数：")
        for table, count in plan.before_counts.items():
            print(f"  - {table}: {count:,}")

        after_counts = execute(engine, plan)
    finally:
        engine.dispose()

    print("\n清空后三张根表行数：")
    residue = {t: c for t, c in after_counts.items() if c != 0}
    for table, count in after_counts.items():
        print(f"  - {table}: {count:,}")
    if residue:
        print(f"\n[FAIL] 清空后仍有残留：{residue}——可能并发写入抢在 TRUNCATE 前插入了新行")
        return 1
    print("\n[PASS] 三张根表已清空为 0 行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
