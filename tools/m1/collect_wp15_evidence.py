#!/usr/bin/env python
"""WP15 证据采集脚本。

产出的文件（落在 `evidence/m1/`）：

| 文件 | 内容 |
|---|---|
| `wp15-audit.log` | 2 张表结构 / 索引 / CHECK / 触发器 / 迁移版本，+ **两处活性验证** |

**两处活性验证**（本包的两个验收项各一处）：

1. **审计不可篡改**：对真实审计行试写 `UPDATE` / `DELETE`，
   断言被触发器拦下（SQLSTATE `23001`），随后回滚。
   只查 `pg_trigger` 能证明触发器**存在**，不能证明它**生效**。
2. **待办去重**：对同一 `dedupe_key` 试插第二条未结待办，
   断言被部分唯一索引拦下（SQLSTATE `23505`）。
   这是"待办去重"这条验收的**数据库级证据** ——
   服务层的 `ON CONFLICT DO NOTHING` 只有在唯一索引真的存在时才有意义；
   索引丢了的话代码照常运行、待办却被重复创建。

**部分索引的 WHERE 必须单独核对**：`uq_work_items_open_dedupe`
（WP10 建）与 `ix_notification_receipts_unread`（本包建）都带 `WHERE`。
Alembic 的 autogenerate **有可能**丢掉 WHERE 子句，而那种退化不报任何错：
前者会让"已结待办也参与去重"（第二次提醒被永远挡住）；
后者会让索引包含全部已读历史（只增不减）。
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
EXPECTED_CHECKS: int = 16

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

INSPECT_SCRIPT = r"""
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

# 期望产生的核对条数。
#
# **为什么必须有这个数**（ISS-43 的教训）：RESULT 行若只统计"失败了几条"，
# 那么"脚本中途抛异常、本该产生的 CHECK 一条都没产生"与"全部通过"
# 在计数上**完全一样** —— 崩溃会伪装成完美通过（实测踩到过：
# 前半段 9 条 OK、后半段 NameError，RESULT 却是 9 PASS / 0 FAIL）。
EXPECTED_CHECKS = 16

DB = "postgresql+psycopg://rednote@localhost:5432/postgres"
engine = create_engine(DB)

CONTRACT_PATH = Path(
    "/Users/<user>/内容运营AI Native/rednote-production/contracts/openapi.json"
)
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
REVISION_INTRODUCED = "005e53eaed23"
TABLE_COLUMNS = [("audit_events", 13), ("notification_receipts", 5)]
PARTIAL_INDEXES = (
    "uq_work_items_open_dedupe",
    "ix_notification_receipts_unread",
)
TRIGGERS = ("trg_audit_events_immutable",)
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

    for name in TRIGGERS:
        n = conn.execute(
            text(
                "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
                " WHERE NOT t.tgisinternal AND t.tgname = :n"
            ),
            {"n": name},
        ).scalar_one()
        emit(n == 1, f"触发器存在 {name}", n)

    # **关键**：notification_receipts 不能有不可变触发器（它必须可改）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
            " WHERE NOT t.tgisinternal AND c.relname = 'notification_receipts'"
        )
    ).scalar_one()
    emit(n == 0, "notification_receipts 无不可变触发器（它必须可改）", n)

    for name in PARTIAL_INDEXES:
        row = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND indexname=:n"),
            {"n": name},
        ).scalar_one_or_none()
        has_where = bool(row) and " WHERE " in row
        where = row.split(" WHERE ", 1)[1] if has_where else "<无 WHERE，已退化为普通索引>"
        emit(has_where, f"部分索引 {name}", where)

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
    print("--- 活性验证 1：审计不可篡改（试写后回滚）---")
    probe_request_id = f"probe-{uuid.uuid4().hex[:12]}"

# 插一条探针审计行（绕过 ORM，验证触发器对原生 SQL 也拦）。
with engine.connect() as wconn:
    trans = wconn.begin()
    wconn.execute(
        text(
            "INSERT INTO audit_events (actor_kind, action, object_type, object_id,"
            " request_id) VALUES ('SYSTEM', 'probe.write', 'probe',"
            " gen_random_uuid(), :r)"
        ),
        {"r": probe_request_id},
    )
    audit_id = wconn.execute(
        text("SELECT id FROM audit_events WHERE request_id = :r"),
        {"r": probe_request_id},
    ).scalar_one()
    trans.commit()

for label, sql in (
    ("UPDATE", "UPDATE audit_events SET reason = 'tampered' WHERE id = :i"),
    ("DELETE", "DELETE FROM audit_events WHERE id = :i"),
):
    with engine.connect() as wconn:
        trans = wconn.begin()
        try:
            wconn.execute(text(sql), {"i": str(audit_id)})
            emit(False, f"审计不可篡改 {label}", "未被拦截（保护失效）")
        except DBAPIError as exc:
            state = getattr(exc.orig, "sqlstate", None)
            emit(state == "23001", f"审计不可篡改 {label}", f"被拦截 sqlstate={state} (期望 23001)")
        finally:
            trans.rollback()

print()
print("--- 活性验证 2：待办去重（试插后回滚）---")
probe_key = f"probe-dedupe-{uuid.uuid4().hex}"
with engine.connect() as wconn:
    trans = wconn.begin()
    try:
        wconn.execute(
            text(
                "INSERT INTO work_items (kind, subject_type, subject_id, state, dedupe_key)"
                " VALUES ('PROBE', 'probe', gen_random_uuid(), 'OPEN', :k)"
            ),
            {"k": probe_key},
        )
        # 第二条同 key 的未结待办必须被拦。
        try:
            wconn.execute(
                text(
                    "INSERT INTO work_items (kind, subject_type, subject_id, state, dedupe_key)"
                    " VALUES ('PROBE', 'probe', gen_random_uuid(), 'OPEN', :k)"
                ),
                {"k": probe_key},
            )
            emit(False, "待办去重", "第二条未结待办未被拦截（唯一索引失效）")
        except DBAPIError as exc:
            state = getattr(exc.orig, "sqlstate", None)
            name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            emit(
                state == "23505",
                "待办去重",
                f"被拦截 sqlstate={state} index={name} (期望 23505)",
            )
    finally:
        trans.rollback()

print()
print("--- 契约核对：WP15 的 3 个端点 ---")
try:
    import json

    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    for path in ("/api/v1/work-items", "/api/v1/audit-events"):
        emit(path in contract["paths"], f"{path} 已注册", "在契约里" if path in contract["paths"] else "缺失")
    resolutions = "/api/v1/work-items/{workItemId}/resolutions"
    emit(resolutions in contract["paths"], f"{resolutions} 已注册",
         "在契约里" if resolutions in contract["paths"] else "缺失")
    # receipts **不该**有端点（协议 §10 没有）。
    receipts = [p for p in contract["paths"] if "receipt" in p]
    emit(receipts == [], "receipts 无端点（协议未定义）", receipts or "无")
except Exception as exc:  # pragma: no cover - 证据脚本自身防御
    emit(False, "契约核对", f"读取失败: {exc}")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def collect_wp15() -> tuple[Path, bool]:
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
        "# WP15 审计与待办：表结构、触发器、部分索引、两处活性验证、契约端点",
        f"# 生成时间: {_now()}",
        "# 命令: cd services/backend && python -c '<information_schema + pg_trigger + 试写>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp15-audit.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # **条数不足也算失败**（ISS-43）：脚本中途抛异常时，
    # "本该产生的 CHECK 一条都没产生"与"全部通过"在计数上一模一样，
    # 只判 `not failures` 会让崩溃伪装成完美通过。
    complete = len(checks) >= EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete)


def main() -> int:
    path, ok = collect_wp15()
    print(f"{'OK  ' if ok else 'FAIL'} {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
