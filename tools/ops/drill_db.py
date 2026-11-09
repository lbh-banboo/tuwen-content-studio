#!/usr/bin/env python
"""备份/回滚演练共用的数据库操作（WP47）。

## 为什么单独一个模块，而不是各判据脚本各写一份

ISS-174 那一类坑只需**一处**正确：

* `postgresql://`   —— libpq 口径，给 `pg_dump` / `pg_restore` / `psycopg.connect`
* `postgresql+psycopg://` —— SQLAlchemy 口径，`migrations/env.py::_resolve_database_url`
  **强制要求**这个前缀

喂错方向的后果不是"报错"，而是被 `except` 吞成"连不上库" ⇒ 判据降级成 skip 或假绿。
把这两行知识复制到两个演练脚本里，将来改一处忘另一处就是静默失败 ——
本仓同族坑已踩过两次（ISS-174 与 WP47 首跑）。故单一事实来源放这里。

## 为什么库名必须由调用方传

每个演练建自己的私有库（`rednote_drill_*` / `rednote_roll_*`），
不能共用 `rednote_test` —— 铁律 17：并行会话各自隔离库，否则双向污染。
库名里带时间戳 ⇒ 两次运行互不干扰。

## 为什么基准口径必须读环境变量（2026-10-07 补的一条同族坑）

首版 `dsn()` 自己拼 `rednote@localhost:5432`。本机跑得通，**CI 上必红**：
CI 的角色是 `postgres:postgres`（`.github/workflows/ci.yml` 的 `PG_DSN`），
GitHub runner 上不存在 `rednote` 角色 ⇒ 报 `role "rednote" does not exist`。
那种红与"回滚路径坏了"毫无关系，是最容易误导人的一种红
（ISS-108 原样复发：`check_test_db_isolation.py` 的 docstring 记的就是这件事）。

故基准 DSN **只从环境变量读**（`REDNOTE_TEST_DATABASE_URL` → `REDNOTE_DATABASE_URL`），
与 `check_migration_model_parity.py::_base_dsn()` 同口径；
本函数只换库名，host / 端口 / 角色 / 口令一律照抄。

## 为什么 `pg_dump` 的路径也要能覆盖

Homebrew 的 PG 不在默认 PATH 上，而 CI 的客户端来自 apt。
路径写死 Homebrew ⇒ CI 上 `pg_dump` 直接 not found。
故按 `REDNOTE_PG_BIN` → `which pg_dump` 的目录 → Homebrew 三级回退。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 本机默认口径。CI 下由 `REDNOTE_TEST_DATABASE_URL` 覆盖（角色不同）。
DEFAULT_DRILL_DSN = "postgresql+psycopg://rednote@localhost:5432/postgres"

#: `pg_dump` / `pg_restore` / `psql` 所在目录的本机回退值。
DEFAULT_PG_BIN = "/opt/homebrew/opt/postgresql@17/bin"

#: 种子数据规模。下界判据防"种子没插进去"退化成空库对账（0 == 0 恒真，ISS-115）。
SEED_USERS = 5
SEED_VERSION_NODES = 20

#: 队列种子规模（异常矩阵第⑭ 项"备份恢复队列对账"）。
#:
#: **为什么必须单独造**：2026-10-07 首跑的演练只seed 了 `users` 与 `version_nodes`
#: 两张表，而 `check_backup_restore_drill.py` 的 `compare:row-counts-equal` 逐表比对
#: 62 张表 —— 其中 `outbox_events` / `jobs` / `job_attempts` / `idempotency_records`
#: 四张**全是 0 行**，那四条比对形态是 `0 == 0` **恒真**。备份把队列内容全丢了，
#: 恢复后照样报"行数相等"。
#: 这正是本模块 docstring 里ISS-115 骂的那件事，**判据在自己的队列表上犯了
#: 自己写下的这条罪**。
# ⚠️ 这三个数**不是随便定的**：必须 ≥ ORM 状态机里的状态数，否则有状态从未被验
# （2026-10-08 实测踩到：首版jobs=6 只覆盖 6 态，漏掉 CANCEL_REQUESTED /
# WAITING_PROVIDER / WAITING_RECONCILIATION，被 `queue:states-match-model` 当场抓红）。
# 下面按 `len(SEED_JOB_STATES)` 反推：9 态需要 ≥ 9 行。
SEED_JOBS = 9
SEED_OUTBOX = 6
# attempts 的 4 态同理（RUNNING 不能漏 —— 那是"正在跑"的形态）
SEED_ATTEMPTS = 4
SEED_IDEMPOTENCY = 4

#: 队列种子的状态分布。**刻意跨越各状态而不是造一批同态行** ——
#: 全是 `QUEUED` 的话，"已投递事件恢复后仍是已投递"这类逐列比对区分不出来
#: （两个候选值都是 `published_at IS NULL`）。分布本身是对账口径的一部分。
#: 取值来源`modules/jobs/models.py` 的状态机常量，不另写一份字符串。
# ⚠️ **必须列全**（9 态，一个不能少），且顺序取自 `jobs/models.py` 的状态机。
# 少列一个就有状态从未被逐列对账验过 —— 而 `queue:states-match-model` 会抓红，
# 这是它存在的意义（不是"看着好看"，是真在拦）。
# `WAITING_RECONCILIATION` 尤其重要：它是异常矩阵另一项"响应丢失 UNKNOWN 先查后重"
# 的落点，恢复后这个状态错变别的，后果是"把'不知道成没成'当成'可以重试'"⇒ 重复执行。
SEED_JOB_STATES: tuple[str, ...] = (
    "QUEUED", "RUNNING", "WAITING_PROVIDER", "WAITING_RECONCILIATION",
    "SUCCEEDED", "PARTIAL", "FAILED", "CANCEL_REQUESTED", "CANCELLED",
)
SEED_ATTEMPT_STATES: tuple[str, ...] = ("RUNNING", "SUCCEEDED", "FAILED", "ABANDONED")
SEED_IDEMPOTENCY_STATES: tuple[str, ...] = ("IN_PROGRESS", "COMPLETED")

#: 本仓"队列"的全部载体。判据的下界检查按这张表逐张断言 ⇒ **覆盖范围由磁盘决定**
#: 的同族纪律：这张清单与库里实际存在的队列表必须对账（见
#: `check_backup_restore_drill.py` 的 `queue:tables-declared`）。
QUEUE_TABLES: tuple[str, ...] = (
    "outbox_events", "jobs", "job_attempts", "idempotency_records",
)

#: 逐列对账要比哪些列。**不是 `select *`** ——
#: `pg_dump`/`pg_restore` 不保证列序，且 `select *` 会把 `updated_at` 这类
#: 每次读都可能变的列卷进来（虽然本仓用 `now()` 而非 `clock_timestamp()`，
#: 但依赖这一点是隐患）。逐列点名 + 稳定排序 ⇒ 摘要可比。
#:
#: ⚠️ **列名即契约**（与跨模块裸 SQL 同纪律）：写错列名当场`UndefinedColumn`，
#: 不会静默少比一列。
QUEUE_DIGEST_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    # 事件层：attempts / published_at / next_attempt_at 三列构成投递状态，
    # 少比任何一列都丢掉"退避重试"与"已投递"这两种真实形态。
    "outbox_events": (
        ("id", "id"), ("event_type", "event_type"),
        ("aggregate_type", "aggregate_type"),
        ("schema_version", "schema_version"), ("attempts", "attempts"),
        ("published_at", "published_at"), ("next_attempt_at", "next_attempt_at"),
    ),
    # 任务层：state / fence_token / lease 三列是执行互斥的载体。
    "jobs": (
        ("id", "id"), ("kind", "kind"), ("queue", "queue"),
        ("state", "state"), ("priority", "priority"),
        ("fence_token", "fence_token"), ("lease_owner", "lease_owner"),
        ("lease_until", "lease_until"), ("result_ref", "result_ref"),
        ("error_code", "error_code"),
    ),
    "job_attempts": (
        ("id", "id"), ("job_id", "job_id"), ("attempt_no", "attempt_no"),
        ("state", "state"), ("worker_id", "worker_id"),
        ("fence_token", "fence_token"), ("error_code", "error_code"),
        ("usage_pending", "usage_pending"),
    ),
    "idempotency_records": (
        ("scope", "scope"), ("key", "key"), ("state", "state"),
        ("request_hash", "request_hash"), ("resource_id", "resource_id"),
        ("response_status", "response_status"),
    ),
}


def base_dsn() -> str:
    """基准 DSN：环境变量优先（CI 口径），回退到本机口径。"""
    return os.environ.get(
        "REDNOTE_TEST_DATABASE_URL",
        os.environ.get("REDNOTE_DATABASE_URL", DEFAULT_DRILL_DSN),
    )


def pg_bin() -> str:
    """`pg_dump`/`pg_restore` 所在目录。三级回退，最后才是 Homebrew 硬编码。"""
    explicit = os.environ.get("REDNOTE_PG_BIN", "").strip()
    if explicit:
        return explicit
    found = shutil.which("pg_dump")
    if found:
        return str(Path(found).parent)
    return DEFAULT_PG_BIN


def dsn(db: str) -> str:
    """libpq 口径的 DSN（`psycopg` / `pg_dump` / `pg_restore` 用）。

    ⚠️ **只换库名，host/端口/角色/口令照抄基准 DSN** —— 本机角色是 `rednote`、
    CI 角色是 `postgres`。自己拼 `rednote@localhost` 会让演练一挂进 CI 门 3
    就红在 `role "rednote" does not exist`（见模块 docstring）。
    """
    from sqlalchemy.engine import make_url

    url = make_url(base_dsn())
    return url.set(drivername="postgresql", database=db).render_as_string(
        hide_password=False
    )


def dsn_alembic(db: str) -> str:
    """SQLAlchemy 口径的 DSN（alembic 用）。

    ISS-174 同款坑：`migrations/env.py` 的 `_resolve_database_url` **强制要求**
    `postgresql+psycopg://` 前缀，而 libpq 系列工具**不认**这个前缀。
    两边各用各的，不能混。
    """
    return dsn(db).replace("postgresql://", "postgresql+psycopg://", 1)


def _connect_autocommit(dbname: str = "postgres"):
    import psycopg

    return psycopg.connect(dsn(dbname), autocommit=True)


def create_db(name: str) -> None:
    """建一个干净库并装好 `vector` 扩展（迁移链依赖它）。"""
    with _connect_autocommit("postgres") as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.execute(f'CREATE DATABASE "{name}"')
    with _connect_autocommit(name) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")


def drop_db(name: str) -> None:
    """删库。失败**不抛出** —— 清理失败不该掩盖主结论。"""
    try:
        with _connect_autocommit("postgres") as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    except Exception:  # noqa: BLE001
        pass


def upgrade_head(db: str) -> tuple[int, str]:
    """`alembic upgrade head`。返回 (退出码, 输出尾部)。"""
    env = dict(os.environ, REDNOTE_DATABASE_URL=dsn_alembic(db), NO_PROXY="*")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "migrations/alembic.ini",
         "upgrade", "head"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
    )
    return proc.returncode, (proc.stdout + proc.stderr)[-1500:]


def migrate(db: str, *args: str) -> tuple[int, str]:
    """跑任意 alembic 子命令（如 `downgrade -1`）。返回 (退出码, 输出尾部)。

    与 `upgrade_head` 共享同一套 DSN 方言处理 —— 这是本模块存在的理由：
    `downgrade` 走的是同一条 env.py 路径，方言错了同样是"连不上库"的静默失败。
    """
    env = dict(os.environ, REDNOTE_DATABASE_URL=dsn_alembic(db), NO_PROXY="*")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "migrations/alembic.ini", *args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
    )
    return proc.returncode, (proc.stdout + proc.stderr)[-1500:]


def seed(conn) -> dict[str, int]:
    """造种子数据。返回各类插入行数（供下界判据核对）。

    ⚠️ 只插**可变表里没有业务约束**的行：`version_nodes` 是**不可变记录**，
    `rednote_reject_mutation()` 触发器会让 DELETE 抛 `RestrictViolation`
    （2026-10-07 实测）—— 它适合做"业务摘要"，不适合做"注入破坏"的目标。

    ## 队列种子为什么在这里而不是让判据脚本自己造

    队列表的插入**必须与 `users`/`version_nodes` 同事务**，否则备份时机可能落在
    两批数据之间 —— 那样源库本身就自相矛盾（job 的`project_id` 指向不存在的用户，
    或`job_attempts.job_id` 指向不存在的 job），对账会把"造数据造错了"报成"恢复不一致"。
    而判据脚本 `check_backup_restore_drill.py` 只调一次 `seed()`，
    在两处各造一半需要它自己管事务 —— 那正是把造数据这件事分给两个文件。
    故单一入口。
    """
    import hashlib

    for i in range(SEED_USERS):
        conn.execute(
            "insert into public.users (issuer, subject, display_name, role, status)"
            " values (%s, %s, %s, %s, %s)",
            ("drill", f"drill-{i}", f"演练用户{i}", "OPERATOR", "ACTIVE"),
        )
    for i in range(SEED_VERSION_NODES):
        payload = f"drill-payload-{i}"
        conn.execute(
            "insert into public.version_nodes"
            " (entity_type, entity_id, revision_no, payload_hash)"
            " values (%s, %s, %s, %s)",
            ("BRIEF", str(uuid.uuid4()), i + 1,
             hashlib.sha256(payload.encode()).hexdigest()),
        )
    counts = _seed_queue(conn)
    u = conn.execute("select count(*) from public.users").fetchone()[0]
    v = conn.execute("select count(*) from public.version_nodes").fetchone()[0]
    return {"users": int(u), "version_nodes": int(v), **counts}


def _seed_queue(conn) -> dict[str, int]:
    """造队列种子（事件层 + 任务层）。**须与 `seed()` 同事务调用**。

    **为什么用裸 SQL 而不是走 `enqueue_job`**：本模块刻意不import 业务代码
    ——`drill_db.py` 的存在意义是"DSN 方言知识单一事实来源"，一import
    `rednote.*` 就得连带 `PYTHONPATH=src`，而 `check_backup_restore_drill.py`
    的运行方式（`python tools/quality/...`）不带它。
    ⇒ 代价是**列名在这里被抄了一份**，与`jobs/models.py` 是两个事实来源。
    这笔账用两道判据兜住（见 `check_backup_restore_drill.py`）：
    ① `queue:tables-declared`断言 `QUEUE_TABLES` 里每张表在库里真实存在；
    ② `queue:digest-columns-exist` 断言 `QUEUE_DIGEST_COLUMNS` 里的每列真实可查。
    列名写错 ⇒ `UndefinedColumn` 当场炸，不会静默少比。

    **状态取值来自 `SEED_JOB_STATES` 而非散落的字符串**：状态机在
    `jobs/models.py`，这里抄一份必然漂移，所以判据另有一条
    `queue:states-match-model` 把两处对账（见判据脚本）。
    """
    import hashlib
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    from psycopg.types.json import Jsonb

    job_ids: list[str] = []
    # --- 任务层：每个 job 一条，状态按SEED_JOB_STATES 循环 ---
    for i in range(SEED_JOBS):
        state = SEED_JOB_STATES[i % len(SEED_JOB_STATES)]
        jid = str(uuid.uuid4())
        job_ids.append(jid)
        # 租约：只给 RUNNING 那一条（`lease_pair_consistent` CHECK 要求两列同在或同无）。
        # ⚠️ 时间**作为参数传**，不拼进 SQL 字符串 —— 拼字符串会让 `None` 变成裸
        # `None` 字面量（报 `UndefinedColumn`，2026-10-08 实测踩到），而参数化天然
        # 区分 NULL 与字面量，也免去"值里带引号"的转义问题。
        lease_owner = "drill-worker-0" if state == "RUNNING" else None
        lease_until = _dt.now(_tz.utc) + _td(seconds=60) if state == "RUNNING" else None
        conn.execute(
            "insert into public.jobs"
            " (id, kind, queue, state, priority, idempotency_scope,"
            "  idempotency_key, input_hash, input_version_ids, fence_token,"
            "  lease_owner, lease_until, result_ref, error_code)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (jid, "DRILL_KIND", "drill", state, i,
             "drill", f"drill-job-{i}",
             hashlib.sha256(f"drill-job-{i}".encode()).hexdigest(),
             [], i,  # fence_token 随 i 递增 ⇒ 逐列对账能区分"恢复后退回旧值"
             lease_owner, lease_until,
             "result-ref" if state == "SUCCEEDED" else None,
             "drill_error" if state == "FAILED" else None),
        )

    # --- 执行尝试：挂在 job_ids[0..] 上，覆盖 SUCCEEDED/FAILED/ABANDONED 三态 ---
    for i in range(SEED_ATTEMPTS):
        attempt_state = SEED_ATTEMPT_STATES[i % len(SEED_ATTEMPT_STATES)]
        conn.execute(
            "insert into public.job_attempts"
            " (id, job_id, attempt_no, state, worker_id, fence_token,"
            "  error_code, usage_pending)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), job_ids[i], 1, attempt_state,
             f"drill-worker-{i}", i,
             None if attempt_state == "SUCCEEDED" else "drill_error",
             i == 0),
        )

    # --- 事件层：已投递 / 未投递 / 退避重试 三种投递状态各占若干 ---
    # 这三种是`mark_published` / `mark_publish_failed` 留下的形态，
    # 恢复后必须逐列相等 —— 特别是 `published_at`：若恢复后未投递事件
    # 错变"已投递"，那这条事件会被永久跳过（**丢任务**）。
    now = _dt.now(_tz.utc)
    for i in range(SEED_OUTBOX):
        published = i % 3 == 0
        # 退避重试：未投递且带 next_attempt_at
        backing_off = i % 3 == 2
        conn.execute(
            "insert into public.outbox_events"
            " (id, aggregate_type, aggregate_id, event_type, schema_version,"
            "  payload, attempts, published_at, next_attempt_at)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), "job", job_ids[i % len(job_ids)],
             "job.enqueued", 1,
             #⚠️ 必须用 `Jsonb(...)` 包装：psycopg3 不会把 dict
             # 自动适配成 jsonb（`cannot adapt type 'dict'`，2026-10-08 实测）。
             # 写成 `'{...}'` 字符串也一样会被当 text 塞进去、类型错。
             Jsonb({"drill": True, "seq": i}),
             3 if backing_off else 0,
             now if published else None,
             now + _td(seconds=30) if backing_off else None),
        )

    # --- 幂等记录：IN_PROGRESS / COMPLETED 两态 ---
    for i in range(SEED_IDEMPOTENCY):
        permanent = i % 2 == 0
        conn.execute(
            "insert into public.idempotency_records"
            " (scope, key, request_hash, state, response_status, resource_id,"
            "  expires_at)"
            " values (%s, %s, %s, %s, %s, %s, %s)",
            (f"drill-scope-{i % 2}", f"drill-key-{i}",
             hashlib.sha256(f"drill-idem-{i}".encode()).hexdigest(),
             SEED_IDEMPOTENCY_STATES[i % len(SEED_IDEMPOTENCY_STATES)],
             200 if permanent else None,
             job_ids[i % len(job_ids)],
             # expires_at 为 NULL = 永久保留（生产任务类）⇒ 与 7 天后清理的
             # 一般响应缓存两种形态都覆盖到。
             None if permanent else now + _td(days=7)),
        )

    out: dict[str, int] = {}
    for table in QUEUE_TABLES:
        out[table] = int(
            conn.execute(f"select count(*) from public.{table}").fetchone()[0]  # noqa: S608 - 表名来自 QUEUE_TABLES 常量，非外部输入
        )
    return out


def queue_digest(cur) -> dict[str, dict[str, object]]:
    """队列表**逐列**摘要。返回 `{表名: {行数, 列摘要, 行摘要}}`。

    ## 为什么不能只比行数

    行数相等是**极弱**的判据：`UPDATE outbox_events SET published_at = now()`
    行数一点不变，但那正是"未投递事件被错标成已投递"⇒**丢任务**。
    异常矩阵第 ⑭ 项要的是"恢复出来的队列还认不认得这些任务"，
    认不认得取决于**内容**，不取决于条数。

    ## 为什么逐列点名而不是 `select *`

    见 `QUEUE_DIGEST_COLUMNS` 的注释。一句话：`select *` 的列序是 schema 的
    实现细节，跨版本不保证；而且会把 `updated_at` 这类随时钟变的列卷进来。

    ## 摘要怎么算才算稳定

    逐行 `coalesce(列::text, '\\x00')` 拼成一条带列名分隔的串，
    按**内容排序**（不是按 id —— uuid 无序，但排序键必须是内容才与写入顺序无关），
    最后 sha256。
    ⇒ 任何一格内容不同 ⇒ 摘要不同 ⇒ 判据红。
    ⚠️ `null` 与空串必须区分（`coalesce` 里用 `\\x00` 哨兵），
    否则 `published_at=NULL` 与 `published_at=''` 同形。
    """
    import hashlib

    out: dict[str, dict[str, object]] = {}
    for table, columns in QUEUE_DIGEST_COLUMNS.items():
        cols = ", ".join(f"coalesce({expr}::text, '\\x00')" for expr, _ in columns)
        rows = cur.execute(
            f"select {cols} from public.{table}"  # noqa: S608 - 表名/列名来自 QUEUE_DIGEST_COLUMNS 常量
        ).fetchall()
        materialized = sorted("|".join(str(cell) for cell in row) for row in rows)
        blob = "\n".join(materialized)
        out[table] = {
            "rows": len(rows),
            "columns": [expr for expr, _ in columns],
            "rows_sha256": hashlib.sha256(blob.encode()).hexdigest(),
        }
    return out
