#!/usr/bin/env python
"""WP16 证据采集脚本。

产出的文件（落在 `evidence/m1/`）：

| 文件 | 内容 |
|---|---|
| `wp16-gateway.log` | 5 张表结构 / 唯一约束 / 部分索引 / 触发器 / 迁移版本，+ **四处活性验证** |

**四处活性验证**：

1. **出网留痕不可篡改**：对真实留痕行试写 `UPDATE` / `DELETE`，
   断言被触发器拦下（SQLSTATE `23001`）。
   只查 `pg_trigger` 能证明触发器**存在**，不能证明它**生效**。
2. **幂等键唯一**：对同一 `(provider_id, request_key)` 试插第二条，
   断言被唯一约束拦下（SQLSTATE `23505`）。
   这是"未知调用状态不盲重试"这条验收的**数据库级证据** ——
   服务层的"派发前先查"只有在唯一约束真的存在时才有意义。
3. **缺省拒绝落在列默认值上**：不传 `external_policy` 插一行，
   断言得到 `LOCAL_ONLY`（而不是"允许"）。
4. **留痕的 decision 受 CHECK 约束**：试插一个非法 decision，断言被拦（`23514`）。

**跨包回归项**（本包最容易误伤别人的地方）：
- 共用的不可变触发器函数 `rednote_reject_mutation()` 必须仍在
  （WP16 的 `downgrade` 刻意不删它 —— 所有不可变表共用）；
- `trg_audit_events_immutable`（WP15）必须仍存在；
- `providers`（可变表）**不该**有不可变触发器。
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
# 两处都要：内联脚本用它写进 RESULT 行，外层用它判定成功与否（ISS-43）。
EXPECTED_CHECKS: int = 20

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

INSPECT_SCRIPT = r"""
# 内联脚本：只允许单行注释，且不能出现三引号（三引号会提前终止外层包裹，见 ISS-45）。

import json
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

# 期望产生的核对条数（ISS-43：崩溃伪装成"全过"的防线）。
EXPECTED_CHECKS = 20

DB = "postgresql+psycopg://rednote@localhost:5432/postgres"
engine = create_engine(DB)

CONTRACT_PATH = Path(
    "/Users/<user>/内容运营AI Native/rednote-production/contracts/openapi.json"
)

# 该包**引入**的迁移版本；判据是"不早于"而不是"恰好等于"（ISS-44）。
REVISION_INTRODUCED = "515fb8b0d31a"

TABLE_COLUMNS = [
    ("providers", 10),
    ("model_profiles", 10),
    ("capability_policies", 9),
    ("provider_calls", 14),
    ("gateway_egress", 13),
]
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


def sqlstate(exc):
    return getattr(exc.orig, "sqlstate", None)


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
    emit(
        _revision_is_at_least(version, REVISION_INTRODUCED),
        "alembic_version 不早于本包引入的版本",
        f"{version} (本包引入 {REVISION_INTRODUCED})",
    )

    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_trigger"
            " WHERE NOT tgisinternal AND tgname = 'trg_gateway_egress_immutable'"
        )
    ).scalar_one()
    emit(n == 1, "触发器存在 trg_gateway_egress_immutable", n)

    # 共用函数必须仍在（WP16 的 downgrade 刻意不删它）。
    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname = 'rednote_reject_mutation'")
    ).scalar_one()
    emit(n == 1, "共用不可变函数 rednote_reject_mutation 仍在", n)

    # 跨包回归：WP15 的审计触发器不能被本包误删。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_trigger"
            " WHERE NOT tgisinternal AND tgname = 'trg_audit_events_immutable'"
        )
    ).scalar_one()
    emit(n == 1, "跨包回归：审计触发器仍在", n)

    # 可变表不该有不可变触发器（providers 会被改：停用、改 endpoint）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
            " WHERE NOT t.tgisinternal AND c.relname = 'providers'"
        )
    ).scalar_one()
    emit(n == 0, "providers 无可变触发器（它必须可改）", n)

    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conrelid = 'provider_calls'::regclass AND contype = 'u'"
            " AND conname = 'uq_provider_calls_provider_request'"
        )
    ).scalar_one()
    emit(n == 1, "唯一约束 uq_provider_calls_provider_request 存在", n)

    row = conn.execute(
        text(
            "SELECT indexdef FROM pg_indexes"
            " WHERE schemaname='public' AND indexname='ix_provider_calls_poll_due'"
        )
    ).scalar_one_or_none()
    has_where = bool(row) and " WHERE " in row
    where = row.split(" WHERE ", 1)[1] if has_where else "<无 WHERE，已退化为普通索引>"
    emit(has_where, "部分索引 ix_provider_calls_poll_due", where)

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
print("--- 活性验证 1：出网留痕不可篡改（试写）---")
print("--- 活性验证 2：幂等键唯一（试插第二条）---")
print("--- 活性验证 3：缺省拒绝落在列默认值 ---")
print("--- 活性验证 4：留痕 decision 受 CHECK 约束 ---")

# **单事务 + SAVEPOINT**：所有探针都在一个事务里完成，最后整体回滚 ——
# 零残留。两次"预期失败"的写各包一个 SAVEPOINT，因为 PG 中任何失败语句
# 都会把整个事务置为 aborted，除非回滚到该语句之前的保存点
# （WP10/ISS-22 踩过同一个坑）。
#
# **为什么不像 WP15 那样"提交探针行、不清理"**：留痕表不可变（触发器拦 DELETE），
# 所以一行留痕会**永久钉住**它引用的 project 与 provider（外键 RESTRICT），
# 导致连"清理可变部分"都做不到。用 SAVEPOINT 就完全不需要清理。
with engine.connect() as wconn:
    trans = wconn.begin()
    probe = uuid.uuid4().hex[:10]

    # **自造 FK 目标，不"从库里捞现成的"**（2026-09-30 修）。
    #
    # 原实现是 `INSERT INTO projects … SELECT … FROM users u, brands b,
    # products p WHERE p.brand_id = b.id LIMIT 1` —— 即**假设验收库里有
    # 现成的 users/brands/products 行**。默认验收库 `postgres` 已在
    # WP-DEBT-02 按 ISS-145 清空（66 万行 → 0），这个 SELECT 返回 0 行，
    # `scalar_one()` 于是抛 `NoResultFound`，**整个脚本在活性验证段崩掉**：
    # 4 条活性 CHECK 一条都没产出，RESULT 停在 14/20 ⇒ FAIL。
    #
    # 错误形态值得记：这条"活性验证"的**前提**是另一个包（WP10）的数据，
    # 而它验证的性质（触发器拦不拦写）与"库里有没有人先灌过数据"
    # 毫无逻辑关系。**依赖外部数据 = 把环境的偶然状态写进判据的前提**，
    # 而环境一定会变（清库、换库、CI 空库）—— 判据应当**自己造出它需要的
    # 一切**。这与 wp11 本次修的是同一个病（判据纪律第 9 条 ISS-95 同族：
    # "声称自己有防线"依赖了不该依赖的东西）。
    #
    # 列与取值域以 information_schema / pg_constraint 实测为准：
    #   users.role ∈ {ADMIN, OPERATOR}；status ACTIVE 时 disabled_at 必须为 NULL
    #   （ck_users_disabled_at_consistent_with_status）
    #   projects: brand_id/product_id/owner_id/target_delivery_count/due_date 都 NOT NULL，
    #   external_policy 无默认必须显式给，非 CLOSED 时 closed_at 必须为 NULL
    owner_id = uuid.uuid4()
    brand_id = uuid.uuid4()
    product_id = uuid.uuid4()
    wconn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, display_name, role, status)"
            " VALUES (:u, 'https://idp.probe', :sub, 'WP16 探针', 'ADMIN', 'ACTIVE')"
        ),
        {"u": owner_id, "sub": f"wp16-probe-{probe}"},
    )
    wconn.execute(
        text("INSERT INTO brands (id, name) VALUES (:b, 'WP16 探针品牌')"),
        {"b": brand_id},
    )
    wconn.execute(
        text(
            "INSERT INTO products (id, brand_id, name)"
            " VALUES (:p, :b, 'WP16 探针产品')"
        ),
        {"p": product_id, "b": brand_id},
    )
    project_id = wconn.execute(
        text(
            "INSERT INTO projects (project_code, name, brand_id, product_id,"
            " owner_id, target_delivery_count, due_date, external_policy)"
            " VALUES (:code, '探针项目', :b, :p, :u, 1, CURRENT_DATE + 1,"
            " 'LOCAL_ONLY') RETURNING id"
        ),
        {"code": f"PROBE-{probe.upper()}", "b": brand_id, "p": product_id,
         "u": owner_id},
    ).scalar_one()

    provider_id = wconn.execute(
        text(
            "INSERT INTO providers (name, capability_types, network_mode, endpoint)"
            " VALUES (:n, '[\"TEXT_STRUCTURED\"]'::jsonb, 'EXTERNAL',"
            " 'https://probe.invalid') RETURNING id"
        ),
        {"n": f"probe-provider-{probe}"},
    ).scalar_one()

    job_id = wconn.execute(
        text(
            "INSERT INTO jobs (kind, queue, state, idempotency_scope,"
            " idempotency_key, input_hash, project_id)"
            " VALUES ('PROBE', :q, 'QUEUED', 'probe', :k, 'h', :p) RETURNING id"
        ),
        {"q": f"probe-{probe}", "k": probe, "p": project_id},
    ).scalar_one()

    attempt_id = wconn.execute(
        text(
            "INSERT INTO job_attempts (job_id, attempt_no, worker_id,"
            " fence_token, state) VALUES (:j, 1, 'probe-worker', 1, 'RUNNING')"
            " RETURNING id"
        ),
        {"j": job_id},
    ).scalar_one()

    request_key = f"TEXT_STRUCTURED:{provider_id}:{job_id}:r0"
    call_id = wconn.execute(
        text(
            "INSERT INTO provider_calls (job_attempt_id, provider_id,"
            " request_key, state, request_evidence_ref)"
            " VALUES (:a, :p, :k, 'UNKNOWN', 'trace:probe') RETURNING id"
        ),
        {"a": attempt_id, "p": provider_id, "k": request_key},
    ).scalar_one()

    egress_id = wconn.execute(
        text(
            "INSERT INTO gateway_egress (provider_call_id, project_id,"
            " provider_id, capability, data_classes, decision, policy_version,"
            " trace_id, payload_digest)"
            " VALUES (:c, :pr, :pv, 'TEXT_STRUCTURED',"
            " '[\"TEXT_STRUCTURED\"]'::jsonb, 'ALLOWED', 'pv-probe',"
            " 'trace:probe', :d) RETURNING id"
        ),
        {"c": call_id, "pr": project_id, "pv": provider_id, "d": "0" * 64},
    ).scalar_one()

    for label, sql in (
        ("留痕不可篡改 UPDATE", "UPDATE gateway_egress SET decision = 'DENIED' WHERE id = :i"),
        ("留痕不可篡改 DELETE", "DELETE FROM gateway_egress WHERE id = :i"),
    ):
        try:
            with wconn.begin_nested():
                wconn.execute(text(sql), {"i": str(egress_id)})
            emit(False, label, "未被拦截（保护失效）")
        except DBAPIError as exc:
            emit(
                sqlstate(exc) == "23001",
                label,
                f"被拦截 sqlstate={sqlstate(exc)} (期望 23001)",
            )

    try:
        with wconn.begin_nested():
            wconn.execute(
                text(
                    "INSERT INTO provider_calls (job_attempt_id, provider_id,"
                    " request_key, state, request_evidence_ref)"
                    " VALUES (:a, :p, :k, 'PREPARED', 'trace:probe-2')"
                ),
                {"a": attempt_id, "p": provider_id, "k": request_key},
            )
        emit(False, "幂等键唯一（重复 request_key）", "未被拦截（唯一约束失效）")
    except DBAPIError as exc:
        state = sqlstate(exc)
        name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        emit(
            state == "23505",
            "幂等键唯一（重复 request_key）",
            f"被拦截 sqlstate={state} index={name} (期望 23505)",
        )

    try:
        with wconn.begin_nested():
            value = wconn.execute(
                text(
                    "INSERT INTO capability_policies (project_id, policy_version)"
                    " VALUES (NULL, :v) RETURNING external_policy"
                ),
                {"v": f"pv-probe-{probe}"},
            ).scalar_one()
        emit(
            value == "LOCAL_ONLY",
            "缺省拒绝：external_policy 列默认 LOCAL_ONLY",
            value,
        )
    except DBAPIError as exc:  # pragma: no cover - 列默认值缺失时才会走到
        emit(False, "缺省拒绝：external_policy 列默认 LOCAL_ONLY", str(exc))

    try:
        with wconn.begin_nested():
            wconn.execute(
                text(
                    "INSERT INTO gateway_egress (provider_call_id, project_id,"
                    " provider_id, capability, data_classes, decision, policy_version,"
                    " trace_id)"
                    " VALUES (:c, :pr, :pv, 'TEXT_STRUCTURED',"
                    " '[\"TEXT_STRUCTURED\"]'::jsonb, 'MAYBE', 'pv-probe',"
                    " 'trace:probe')"
                ),
                {"c": call_id, "pr": project_id, "pv": provider_id},
            )
        emit(False, "留痕 decision 受 CHECK 约束", "非法 decision 未被拦截")
    except DBAPIError as exc:
        emit(
            sqlstate(exc) == "23514",
            "留痕 decision 受 CHECK 约束",
            f"被拦截 sqlstate={sqlstate(exc)} (期望 23514)",
        )

    # **整体回滚** —— 探针数据一条不留。
    trans.rollback()

print()
print("--- 契约核对：WP16 不新增 HTTP 端点 ---")
try:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    # 网关是内部服务（协议 §12），管理端点归属未定（OP-123/124）。
    forbidden = [
        p
        for p in contract["paths"]
        if p.startswith("/api/v1/admin") or "/jobs/" in p
    ]
    emit(
        forbidden == [],
        "WP16 不新增端点（管理/前端端点仍不在契约）",
        forbidden or "无",
    )
except Exception as exc:  # pragma: no cover - 证据脚本自身防御
    emit(False, "契约核对", f"读取失败: {exc}")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def collect_wp16() -> tuple[Path, bool]:
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
        "# WP16 模型网关：5 张表 / 唯一约束 / 部分索引 / 触发器 / 四处活性验证 / 契约",
        f"# 生成时间: {_now()}",
        "# 命令: cd services/backend && python -c '<information_schema + 试写 + 试插>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp16-gateway.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # **条数不足也算失败**（ISS-43）。
    complete = len(checks) >= EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete)


def main() -> int:
    path, ok = collect_wp16()
    print(f"{'OK  ' if ok else 'FAIL'} {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
