#!/usr/bin/env python
"""测试库数据体量判据：**无主根表的行数不许无界累积**（ISS-223 / WP-DEBT-05）。

## 为什么需要这个（判据 B 管不到的那一半）

`check_test_residue.py`（判据 B）只测**结构**差分（表/索引/视图/序列/触发器/约束），
docstring 里已明确这是已知边界 —— 它管不到"表还是那些表，但行数一直在涨"。

2026-10-18 现场：`projects`/`users`/`brands` 三张表从 2026-09-30 累积到今天（18 天）
分别到 **116,822 / 166,099 / 117,875** 行，导致判据 B 自己在共享库上跑全量 pytest
超过 3600s 超时（私有干净库同样的判据只需 ~172s）。

## 为什么这三张表没人管

`services/backend/tests/conftest.py` 的 `drop_own_tables` 只删"没有跨包入向 FK"的表——
`projects`/`users`/`brands` 被全仓几十张表引用，永远落在"保留"集合，从未被任何模块
DROP/重建。`truncate_targets` 的闭包方向是"找引用者"，没有任何模块把这三张表列为
自己的种子集去清。全仓 21 个测试文件各自定义 `_seed_project`/`_create_project` 等
种子函数直接 `INSERT`，没有一个做清理（`grep -rln "DELETE FROM projects" services/backend/tests/*.py`
与对 `users`/`brands` 的同款搜索均命中 0）。

## 判据设计：只测三张根表，不测闭包全表

根表行数天然上界住整条派生链（FK 约束下不可能有无根行）——派生表的增长是根表
增长的函数，测根表即可覆盖信号。已知边界：不引用这三张根表的独立表若被测试灌脏，
本判据看不见（见 `--report-largest` 诊断开关，只提示不参与判定）。

## 阈值怎么定（不是拍脑袋的数字）

在私有干净库上实测两轮全量 `pytest -m "not slow"`，记录三表各自新增行数，
取两轮最大值作 `MEASURED_ROWS_PER_RUN`，乘安全倍数 `SAFETY_FACTOR` 向上取整到千位，
得到 `MAX_ROWS`。实测记录见 `docs/m3/工程债-测试库数据体量说明.md` §2.5。

跑法：
    python tools/quality/check_test_data_volume.py
    # 默认对 `rednote_test` 判定；可用环境变量指向别的库
    REDNOTE_TEST_DATABASE_URL=postgresql+psycopg://rednote@localhost:5432/rednote_x \\
        python tools/quality/check_test_data_volume.py
退出码 0 = 三表行数均未超阈值；1 = 超阈值（逐表指名）或连不上/表不存在（fail-closed）。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: DSN 口径与 `services/backend/tests/conftest.py` / `check_test_residue.py` 一致
#: （env 覆盖 > 默认常量）。**刻意不反向 import 它们**：判据要能独立跑，
#: import 测试层/其它判据会让它对那些模块的变动变脆；重复一份由
#: `test_test_data_volume.py::test_default_dsn_matches_conftest_and_residue_judge`
#: 做漂移对账。
TEST_DATABASE_URL_ENV = "REDNOTE_TEST_DATABASE_URL"
DEFAULT_TEST_DSN = "postgresql+psycopg://rednote@localhost:5432/rednote_test"

#: 无主根表 —— 被全仓几十张表 FK 引用、但没有任何测试模块把它们列进自己的
#: `DROP_ORDER`/种子清理集的三张表（见模块 docstring）。不测闭包全表的理由见上。
ROOT_TABLES: tuple[str, ...] = ("projects", "users", "brands")

#: 2026-10-18 实测：私有库 `rednote_wpdebt05_measure{1,2}`（迁移到 head
#: `a9d3f6c2b815`），全量 `pytest -m "not slow"`，两轮分别 2333 passed
#: （171.84s / 177.55s）。三表跑测试前后净增行数**两轮完全一致**（取较大值，
#: 此处两者相等）。记录见 `docs/m3/工程债-测试库数据体量说明.md` §2.5。
MEASURED_ROWS_PER_RUN: dict[str, int] = {
    "projects": 1588,
    "users": 2303,
    "brands": 1601,
}

#: 安全倍数：允许约 `SAFETY_FACTOR` 次全量测试不触发本判据，同时远早于
#: 判据 B 在共享库上因残留行数膨胀而超时（本次实测：18 天累积到约 100 倍单次
#: 运行量时，判据 B 已超 3600s）。调大此值须在文档里重新论证，不能无实测地改。
SAFETY_FACTOR = 4


def _round_up(value: int, step: int) -> int:
    """向上取整到 `step` 的整数倍（`step=1000` 把 6,347 变成 7,000）。"""
    if value <= 0:
        return step
    return ((value + step - 1) // step) * step


MAX_ROWS: dict[str, int] = {
    table: _round_up(MEASURED_ROWS_PER_RUN[table] * SAFETY_FACTOR, 1000)
    for table in ROOT_TABLES
}


@dataclass(frozen=True)
class TableReading:
    """一张根表的读数。`rows=None` 表示表不存在或读不到（独立于"0 行"）。"""

    table: str
    rows: int | None
    problem: str | None = None


@dataclass(frozen=True)
class Violation:
    table: str
    rows: int
    limit: int

    @property
    def ratio(self) -> float:
        return self.rows / self.limit if self.limit else float("inf")


def _test_dsn() -> str:
    return os.environ.get(TEST_DATABASE_URL_ENV, DEFAULT_TEST_DSN).strip()


def _libpq_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


def read_counts(dsn: str, tables: tuple[str, ...] = ROOT_TABLES) -> list[TableReading]:
    """只读：逐表先判存在性再 `count(*)`。表名只来自 `ROOT_TABLES` 常量，不接受外部输入。"""
    import psycopg

    readings: list[TableReading] = []
    try:
        conn = psycopg.connect(_libpq_dsn(dsn), connect_timeout=5)
    except Exception as exc:  # noqa: BLE001
        return [
            TableReading(
                table=t,
                rows=None,
                problem=f"连不上目标库（{type(exc).__name__}: {exc}）",
            )
            for t in tables
        ]
    try:
        with conn:
            for table in tables:
                with conn.cursor() as cur:
                    cur.execute(
                        "select to_regclass(%s) is not null", (f"public.{table}",)
                    )
                    row = cur.fetchone()
                    exists = bool(row and row[0])
                    if not exists:
                        readings.append(
                            TableReading(
                                table=table,
                                rows=None,
                                problem="表不存在（未迁移到该库，还是连错库了？）",
                            )
                        )
                        continue
                    cur.execute(f'select count(*) from "{table}"')  # noqa: S608
                    count_row = cur.fetchone()
                    readings.append(TableReading(table=table, rows=int(count_row[0])))
    except Exception as exc:  # noqa: BLE001
        readings.append(
            TableReading(table="(read)", rows=None, problem=f"读取中出错：{exc}")
        )
    return readings


def evaluate(
    readings: list[TableReading], limits: dict[str, int]
) -> tuple[list[Violation], list[str]]:
    """拆成"超阈值"（可继续判定）与"问题"（连不上/表不存在，fail-closed）两路。

    `rows > limit` 才算超阈值 —— 等于阈值放行（§2.2 的测量基准本身就该是绿的）。
    """
    violations: list[Violation] = []
    problems: list[str] = []
    for reading in readings:
        if reading.problem is not None or reading.rows is None:
            problems.append(f"{reading.table}: {reading.problem or '读数缺失'}")
            continue
        limit = limits.get(reading.table)
        if limit is not None and reading.rows > limit:
            violations.append(
                Violation(table=reading.table, rows=reading.rows, limit=limit)
            )
    return violations, problems


def format_report(
    readings: list[TableReading],
    violations: list[Violation],
    problems: list[str],
    *,
    dsn: str,
) -> str:
    lines = [
        "测试库数据体量判据（ISS-223）：无主根表行数不许无界累积",
        f"  目标库：{dsn}",
        "  读数：" + " / ".join(f"{r.table}={r.rows if r.rows is not None else '?'}" for r in readings),
        "",
    ]
    if problems:
        lines.append(f"[FAIL] {len(problems)} 张表读不到（不折算成 0 行）：")
        for p in problems:
            lines.append(f"  - {p}")
        lines.append("")
    if violations:
        lines.append(f"[FAIL] {len(violations)} 张根表超过阈值（阈值 = 实测单次运行 × {SAFETY_FACTOR}）：")
        for v in violations:
            lines.append(f"  - {v.table}: {v.rows:,} 行 > {v.limit:,}（{v.ratio:.1f} 倍）")
        lines.append("")
        lines.append("处置（先确认无并行 pytest/证据脚本，ISS-160）：")
        lines.append("  1) python tools/quality/check_shared_db_quiet.py")
        lines.append("  2) python tools/dev/reset_shared_test_db.py   （只读 dry-run，先看计划）")
        lines.append("  3) python tools/dev/reset_shared_test_db.py --yes --confirm <库名>")
        lines.append("  4) python tools/quality/check_test_data_volume.py")
    if not problems and not violations:
        lines.append("[PASS] 三张根表行数均未超阈值。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    tables = ROOT_TABLES
    if "--report-largest" in args:
        # 诊断性开关：只提示闭包外最大的若干表，不参与判定（见模块 docstring 已知边界）。
        _print_largest_tables(_test_dsn())

    dsn = _test_dsn()
    readings = read_counts(dsn, tables)
    violations, problems = evaluate(readings, MAX_ROWS)
    print(format_report(readings, violations, problems, dsn=dsn))
    return 1 if (violations or problems) else 0


def _print_largest_tables(dsn: str, limit: int = 10) -> None:
    """只读诊断：按 `pg_class.reltuples` 估算打印最大的几张表（不含根表本身）。

    这是本判据已知边界的补偿：不引用 `ROOT_TABLES` 的独立表若被灌脏，三张根表
    判据看不见——这里只提示、不判定，避免把"估算值"当成精确判据输入。
    """
    import psycopg

    try:
        with psycopg.connect(_libpq_dsn(dsn), connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select relname, reltuples::bigint from pg_class"
                    " join pg_namespace n on n.oid = relnamespace"
                    " where n.nspname = 'public' and relkind = 'r'"
                    " order by reltuples desc limit %s",
                    (limit,),
                )
                rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        print(f"  （--report-largest 读取失败，不影响判定：{exc}）")
        return
    print(f"  --report-largest：行数估算最大的 {len(rows)} 张表（诊断用，不参与判定）：")
    for name, approx in rows:
        print(f"    - {name}: ~{int(approx):,}")
    print()


if __name__ == "__main__":
    sys.exit(main())
