#!/usr/bin/env python
"""WP14 证据采集脚本。

产出的文件（落在 `evidence/m1/`）：

| 文件 | 内容 |
|---|---|
| `wp14-events.log` | `domain_events` 表结构 / Identity 序列 / 索引 / CHECK / 迁移版本 / 契约端点，+ **写入-通知-补漏的活性验证** |

**关于"活性验证"**：WP14 的核心验收是"断网重连后状态正确"，
而它的地基是 `sequence` **真的由数据库序列生成**。
`autoincrement=True` 配 UUID 主键**不会生成序列**（本机实测 DDL 渲染成
裸 `BIGINT NOT NULL`，插入即违约）—— 而这件事用 `information_schema`
查不出来（`column_default` 是 None，但那不是"没有序列"的信号，
`is_identity` 才是）。因此这里既核对 `is_identity = 'YES'`，
又**真的插入一条**并断言 `sequence` 非空且递增（试写后回滚）。

**部分索引**：本包没有部分索引（事件是纯追加），因此改为核对
"三个查询路径索引都在"（`project+sequence`、`job`、`created_at`）
与"`sequence` 上有唯一索引"。
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
EXPECTED_CHECKS: int = 13

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

INSPECT_SCRIPT = r"""
from pathlib import Path

from sqlalchemy import create_engine, text

# 期望产生的核对条数。
#
# **为什么必须有这个数**（ISS-43 的教训）：RESULT 行若只统计"失败了几条"，
# 那么"脚本中途抛异常、本该产生的 CHECK 一条都没产生"与"全部通过"
# 在计数上**完全一样** —— 崩溃会伪装成完美通过（实测踩到过：
# 前半段 9 条 OK、后半段 NameError，RESULT 却是 9 PASS / 0 FAIL）。
EXPECTED_CHECKS = 13

DB = "postgresql+psycopg://rednote@localhost:5432/postgres"
engine = create_engine(DB)

# 契约文件用**绝对路径**：脚本按 `cwd=services/backend` 运行，
# 若写相对路径 "contracts/openapi.json" 会被解析成
# `services/backend/contracts/openapi.json` 而读不到（本机实测踩到）。
CONTRACT_PATH = Path("/Users/<user>/内容运营AI Native/rednote-production/contracts/openapi.json")

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
REVISION_INTRODUCED = "41b468c478a4"
EXPECTED_COLUMNS = 11
EXPECTED_INDEXES = (
    "ix_domain_events_project_sequence",
    "ix_domain_events_job",
    "ix_domain_events_created_at",
)
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
    cols = conn.execute(
        text(
            "SELECT count(*) FROM information_schema.columns"
            " WHERE table_schema='public' AND table_name='domain_events'"
        )
    ).scalar_one()
    emit(cols == EXPECTED_COLUMNS, "domain_events 列数", f"{cols} (期望 {EXPECTED_COLUMNS})")

    version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    # 只要当前 head **不早于**该包引入的版本即可（见常量处的说明）。
    # 真正的"该包的东西还在不在"由本脚本后面的表/索引/触发器核对保证。
    emit(
        _revision_is_at_least(version, REVISION_INTRODUCED),
        "alembic_version 不早于本包引入的版本",
        f"{version} (本包引入 {REVISION_INTRODUCED})",
    )

    # **关键**：`sequence` 必须是 Identity（is_identity = 'YES'）。
    # column_default 为 None 是 Identity 的正常表现（序列是独立对象，不是默认值），
    # 不能用 column_default 来判 —— 裸 BIGINT（没有序列）也是 None。
    identity = conn.execute(
        text(
            "SELECT is_identity FROM information_schema.columns"
            " WHERE table_name='domain_events' AND column_name='sequence'"
        )
    ).scalar_one()
    emit(identity == "YES", "sequence 是 Identity 序列", f"is_identity={identity} (期望 YES)")

    present = set(conn.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename='domain_events'")
    ).scalars().all())
    for name in EXPECTED_INDEXES:
        emit(name in present, f"索引存在 {name}", "在" if name in present else "缺失")
    emit("uq_domain_events_sequence" in present, "sequence 有唯一索引",
         "uq_domain_events_sequence" if "uq_domain_events_sequence" in present else "缺失")

    ck_count = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE contype='c' AND conrelid='public.domain_events'::regclass"
        )
    ).scalar_one()
    emit(ck_count == 3, "CHECK 约束数", f"{ck_count} (期望 3)")

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
    print("--- 活性验证：sequence 真的由序列生成（试写后回滚）---")

with engine.connect() as wconn:
    trans = wconn.begin()
    try:
        s1 = wconn.execute(
            text(
                "INSERT INTO domain_events (event_type, payload) VALUES ('job.queued', '{}'::jsonb)"
                " RETURNING sequence"
            )
        ).scalar_one()
        s2 = wconn.execute(
            text(
                "INSERT INTO domain_events (event_type, payload) VALUES ('job.queued', '{}'::jsonb)"
                " RETURNING sequence"
            )
        ).scalar_one()
        ok = (
            isinstance(s1, int)
            and isinstance(s2, int)
            and s1 > 0
            and s2 > s1
        )
        emit(ok, "sequence 由序列生成且单调", f"s1={s1}, s2={s2} (期望 s2>s1>0)")
    finally:
        trans.rollback()

    # 契约核对：/api/v1/events 必须注册且带 410
    print()
    print("--- 契约核对：SSE 端点与 410 ---")
    try:
        import json
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        events = contract["paths"].get("/api/v1/events")
        emit(events is not None, "/api/v1/events 已注册", "在契约里" if events else "缺失")
        if events:
            codes = list(events.get("get", {}).get("responses", {}))
            emit("410" in codes, "/events 的响应含 410", codes)
    except Exception as exc:  # pragma: no cover - 证据脚本自身防御
        emit(False, "契约核对", f"读取失败: {exc}")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def collect_wp14() -> tuple[Path, bool]:
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
    checks = [line for line in output.splitlines() if line.startswith(("CHECK", "SKIP"))]
    failures = [line for line in checks if line.startswith("CHECK FAIL")]

    lines = [
        "# WP14 领域事件与 SSE：表结构、Identity 序列、契约端点、活性验证",
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
    target = EVIDENCE_DIR / "wp14-events.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # **条数不足也算失败**（ISS-43）：脚本中途抛异常时，
    # "本该产生的 CHECK 一条都没产生"与"全部通过"在计数上一模一样，
    # 只判 `not failures` 会让崩溃伪装成完美通过。
    complete = len(checks) >= EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete)


def main() -> int:
    path, ok = collect_wp14()
    print(f"{'OK  ' if ok else 'FAIL'} {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
