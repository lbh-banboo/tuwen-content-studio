#!/usr/bin/env python
"""备份恢复演练判据（WP47 ③⑥）：备份必须能恢复出一个**与源库逐项相等**的库。

## 为什么必须有这条判据

WP47 的验收原文是「RPO ≤ 24h / RTO ≤ 8h 有**真跑过**的证据」。`pg_dump` 退出 0
**不证明任何事**：它只说明"写文件没报错"，不说明文件完整、更不说明恢复得回来。
本仓第 5 条纪律（ISS-143）写得很直白：**「没跑」与「跑失败」同形**。只有一个
`backup_pg.py` 而没有人把它恢复一遍逐项比对，就是"没跑"。

## 判据形状：两库都从零点建，逐项比相等

```
库 A（源）：createdb → vector → alembic upgrade head → 造种子数据 → 备份（含 manifest）
库 B（目标）：createdb → vector → pg_restore
        ↓
三重比对：① 结构指纹（6 类对象）② 行数（逐表）③ 业务摘要（payload_hash 集合）
        ↓
注入破坏反向验证：删一个触发器 → 指纹必须变；删一行 → 行数必须变
```

## 队列对账（异常矩阵第 ⑭ 项，2026-10-08 补）

**这一组判据的由来是一处自伤**：2026-10-07 首跑时`seed()` 只造 `users` 与
`version_nodes` 两张表，而本脚本的 `compare:row-counts-equal` 逐表比对 62 张表 ——
其中 `outbox_events` / `jobs` / `job_attempts` / `idempotency_records`
**四张全0 行**。那四条比对形态是 `0 == 0` **恒真**：备份把队列内容全丢了，
恢复后照样报"行数相等"。

**判据在自己的队列表上犯了它 docstring 里写下的ISS-115**（"空库对账是 0==0 恒真"）。
所以队列对账不是"再加一条比对"，而是**先把种子补上，再逐列比**：

| 判据 | 拦什么 |
|---|---|
| `queue:seed-present-in-source` | 种子没插进去 ⇒ 后面全部退化成 0==0 |
| `queue:states-match-model` | 种子状态抄本与 ORM 状态机漂移 ⇒ 有状态从未被验 |
| `queue:column-digests-equal` | 逐列内容不等（行数相等也抓不到的那类） |
| `queue:not-empty-after-restore` | 摘要相等但恢复出来是空表（表清单漏了某张） |
| `queue:publish-triad-present` | 三种投递状态有形态缺失 ⇒ "已投递恢复后仍已投递"未验 |
| `reverse:queue-digest-detects-content-change` | 逐列摘要对内容变化不敏感（否则比行数强在哪说不清） |

⚠️ **"队列"在本仓有确定含义**：它就是 PostgreSQL 表（事件层 `outbox_events` +
任务层 `jobs`/`job_attempts`/`idempotency_records`），**本机没有 RabbitMQ**
（`jobs/dispatch.py` 模块 docstring 明写，内网才有 3 节点）。
⚠️ 别与本仓另外三处"对账"混淆：`billing/reconciliation.py`（费用）、
`files/repository.py:179`（配额账面重算）、`research/service.py:469`（`reconciled` 字段）。

## 为什么必须造种子数据

空库对账是 0 == 0，恒真（ISS-115）。不造数据的话本判据永远绿而毫无信息，
正是 ISS-110 lesson 骂的那种"绿灯"。故源库在备份前先插种子行。

## 为什么指纹类别数要单独判

铁律 14：覆盖范围由人写的地方必须有对账。`FINGERPRINT_CLASSES` 与
`_FINGERPRINT_SQLS` 是两处手写清单，少写一类会让"少比一类"静默发生 ⇒
第 1 条判据断言二者**逐项相等且不少于 6 类**。
队列侧同理：`QUEUE_TABLES` 与 `QUEUE_DIGEST_COLUMNS` 由
`queue:not-empty-after-restore` 与注入破坏两条兜住。

## 用法

    python tools/quality/check_backup_restore_drill.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools" / "ops"))

import backup_pg  # noqa: E402
import drill_db  # noqa: E402

#: pg 工具目录。**是函数别名，不是常量** —— 单一事实来源在 `drill_db.pg_bin()`
#: （写死 Homebrew 路径 ⇒ CI 上 `pg_restore` not found，与结论无关的红）。
_pg_bin = drill_db.pg_bin

# 共用 DB 操作收在 `tools/ops/drill_db.py`（DSN 方言知识单一事实来源，ISS-174）。
# 这里保留同名字号是为了让 `tests/test_backup_restore_drill.py` 的方言分离判据
# 有可断言的对象 —— **不是**为了图省事的别名。
_dsn = drill_db.dsn
_dsn_alembic = drill_db.dsn_alembic
_create_db = drill_db.create_db
_drop_db = drill_db.drop_db
_upgrade_head = drill_db.upgrade_head
_seed = drill_db.seed
SEED_USERS = drill_db.SEED_USERS
SEED_VERSION_NODES = drill_db.SEED_VERSION_NODES

# --- 队列对账（异常矩阵第 ⑭ 项"备份恢复队列对账"，2026-10-08 补）---
_queue_digest = drill_db.queue_digest
QUEUE_TABLES = drill_db.QUEUE_TABLES
QUEUE_DIGEST_COLUMNS = drill_db.QUEUE_DIGEST_COLUMNS
SEED_JOBS = drill_db.SEED_JOBS
SEED_OUTBOX = drill_db.SEED_OUTBOX
SEED_ATTEMPTS = drill_db.SEED_ATTEMPTS
SEED_IDEMPOTENCY = drill_db.SEED_IDEMPOTENCY

#: 每张队列表的种子下界。**由 seed 常量派生，不是手写** ——
#: 手写下界与seed 常量是两个事实来源，改了一个忘另一个就会"判据恒过"
#: （下界 < 实际）或"无故变红"（下界 > 实际）。派生则恒等于实际。
#:
#: ⚠️ 这里**断言 `>=` 下界而不是 `==` 实际值**：实际值就是 seed 常量本身，
#: 拿它当期望值等于"自己断言自己"（纪律第 6 条：期望值不能来自被测对象）。
QUEUE_MIN_ROWS: dict[str, int] = {
    "outbox_events": SEED_OUTBOX,
    "jobs": SEED_JOBS,
    "job_attempts": SEED_ATTEMPTS,
    "idempotency_records": SEED_IDEMPOTENCY,
}

#: RTO 预算（WP47 目标 ≤ 8h）。本机数据量下必然远低于 ⇒ 这条断言**在本机是弱判据**，
#: 真正的 RTO 必须内网真实数据量下测。写在这里是为了让"超预算"立刻红，
#: 而不是为了证明达标 —— 达标结论只能在内网下。
RTO_BUDGET_SECONDS = 8 * 3600

#: 静态 1 + 动态 15 + 队列 6 = 22（2026-10-21 起；此前 21）。新增/删除判据必须同步本值
#: （ISS-153：只防变少不防变多，故脚本另断言 `checks == EXPECTED_CHECKS` 全等，不是 >=）。
#: 2026-10-07：本值曾被我手写错成 13（实际 15），被脚本自己的全等断言当场抓出
#: ⇒ 这条"条数全等"断言确实在干活，不是摆设。
#: 2026-10-08：15 → 22（补队列对账）→ 实跑后修正为 **21**：
#: 我按"队列 7 条"写的，实际落6 条（种子在场1 + 状态对账1 + 逐列摘要1 +
#: 恢复后非空1 + 投递三形态1 + 反向验证1）。条数声明被自己的全等断言当场抓出
#: —— 与 2026-10-07 那次同形，故这条断言**两次都真在干活**。
#: 2026-10-21：21 → **22**（新增 `normalizer:known-equivalents-not-stale`，见 `KNOWN_EQUIVALENT_DEFS`）。
EXPECTED_CHECKS = 22

#: 规范化后同形、但**原始文本不同**的约束对（已核对确属语义等价）。
#: 键 = 约束所在表 + 约束名；值 = 该约束 `pg_get_constraintdef` 的原始文本，两条必须**恰好**只差强制转换。
#: 来历（2026-10-21 实测）：`content_reviews.reason` 是 `Text` 列 ⇒ `char_length(reason)`；
#: `step_approvals.reason` 是 `varchar(500)` 列 ⇒ `char_length((reason)::text)`。两者规范化后同为
#: `CHECK(char_length(reason)>0)`，被 `no-over-collapse` 计为 1 次抹平。
#: 这是**窄白名单，不是放宽**：整体仍要求其它所有定义不得被抹平；白名单本身另有
#: `normalizer:known-equivalents-not-stale` 兜底 —— 任何一条消失、原文改变、或不再规范化同形都报红
#: （否则白名单会退化成『只要红了就往里加』的抽屉，参见 RG-174 对 `_KNOWN_CROSS_PACKAGE_FKS` 的同类处置）。
KNOWN_EQUIVALENT_DEFS: tuple[tuple[str, str, str], ...] = (
    ("content_reviews", "ck_content_reviews_reason_not_blank", "CHECK ((char_length(reason) > 0))"),
    (
        "step_approvals",
        "ck_step_approvals_reason_not_empty",
        "CHECK ((char_length((reason)::text) > 0))",
    ),
)

#: 行数快照的表数下界（源库 upgrade head 后的表数，防止 metadata/迁移没跑完
#: 就让对账静默缩水）。2026-10-07 实测 57 张表。
MIN_TABLES = 40


def main() -> int:
    checks: list[tuple[str, bool, str]] = []
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    src_db = f"rednote_drill_src_{stamp}"
    dst_db = f"rednote_drill_dst_{stamp}"
    workdir = Path(f"/tmp/rednote_drill_{stamp}")

    # ---------- 静态 1：指纹类别清单自洽 ----------
    sql_names = tuple(n for n, _, _ in backup_pg._FINGERPRINT_SQLS)  # noqa: SLF001
    ok = sql_names == backup_pg.FINGERPRINT_CLASSES and len(sql_names) >= 6
    checks.append((
        "static:fingerprint-classes-declared", ok,
        f"清单={list(backup_pg.FINGERPRINT_CLASSES)} SQL={list(sql_names)}"
        + ("" if ok else " ⇒ 两处手写清单不一致，会导致'少比一类'静默发生"),
    ))

    try:
        # ---------- ① 源库：从零点建起 + 造种子 ----------
        _create_db(src_db)
        code, tail = _upgrade_head(src_db)
        if code != 0:
            checks.append(("src:upgrade-head", False, tail))
            return _report(checks)
        checks.append(("src:upgrade-head", True, "alembic upgrade head EXIT=0"))

        import psycopg

        with psycopg.connect(_dsn(src_db)) as conn:
            with conn.cursor() as cur:
                seeded = _seed(cur)
            conn.commit()
        ok = seeded["users"] >= SEED_USERS and seeded["version_nodes"] >= SEED_VERSION_NODES
        checks.append((
            "src:seed-data-present", ok,
            f"users={seeded['users']} version_nodes={seeded['version_nodes']}"
            + ("" if ok else " ⇒ 种子没插进去，对账会退化成 0==0 的恒真式"),
        ))

        # ---------- ①-b 队列种子必须在场（防"队列表对账退化成 0==0"） ----------
        # 这条是整个队列对账的**前置**。2026-10-07 首跑没有它：
        # `compare:row-counts-equal` 逐表比对 62 张表，其中四张队列表全0 行，
        # 那四条比对形态是 `0 == 0` 恒真 —— 备份把队列内容全丢了照样报"行数相等"。
        # 即判据在自己的队列表上犯了它 docstring 里写下的ISS-115。
        missing_seed = {
            t: int(seeded.get(t, 0)) for t in QUEUE_TABLES
            if int(seeded.get(t, 0)) < QUEUE_MIN_ROWS[t]
        }
        checks.append((
            "queue:seed-present-in-source", not missing_seed,
            "队列种子 " + ", ".join(
                f"{t}={int(seeded.get(t, 0))}(下界 {QUEUE_MIN_ROWS[t]})" for t in QUEUE_TABLES
            )
            + ("" if not missing_seed else f" ⇒ 不足：{missing_seed}"
                                          " ⇒ 队列对账会退化成 0==0 恒真"),
        ))

        # ---------- ①-c 队列状态取值必须与 ORM 状态机一致 ----------
        # `drill_db.SEED_JOB_STATES` 是抄 `jobs/models.py` 的（见该处注释）。
        # 抄本会漂移 ⇒ 这里对账。写错状态的**后果不是"数据怪"而是"对账恒过"**：
        # 若 seed 用了一组库里 CHECK 不认的状态，插入当场炸（好的）；
        # 若 seed 用的状态都在库里合法但**少覆盖了一态**（比如漏了 PARTIAL），
        # 逐列对账照样全绿而这一态从未被验过。
        model_states = _read_model_states()
        state_gap = {
            "jobs": sorted(set(model_states["jobs"]) - set(drill_db.SEED_JOB_STATES)),
            "job_attempts": sorted(
                set(model_states["job_attempts"]) - set(drill_db.SEED_ATTEMPT_STATES)
            ),
            "idempotency_records": sorted(
                set(model_states["idempotency_records"])
                - set(drill_db.SEED_IDEMPOTENCY_STATES)
            ),
        }
        checks.append((
            "queue:states-match-model", not any(state_gap.values()),
            f"ORM 状态数 jobs={len(model_states['jobs'])} "
            f"attempts={len(model_states['job_attempts'])} "
            f"idem={len(model_states['idempotency_records'])}；"
            "种子未覆盖=" + (", ".join(
                f"{k}:{v}" for k, v in state_gap.items() if v) or "无")
            + ("" if not any(state_gap.values())
               else " ⇒ 这些状态从未被逐列对账验过，判据是半个恒真式"),
        ))

        # ---------- ② 备份 ----------
        dump_path, manifest = backup_pg.dump(_dsn(src_db), workdir)
        ok = dump_path.exists() and dump_path.stat().st_size > 0 and len(
            str(manifest["dump_sha256"])) == 64
        checks.append((
            "backup:dump-produced", ok,
            f"{dump_path.name} bytes={manifest['dump_bytes']} "
            f"sha256={str(manifest['dump_sha256'])[:16]}…",
        ))

        rc = manifest["row_counts"]
        ok = len(rc) >= MIN_TABLES
        checks.append((
            "backup:row-count-scope", ok,
            f"{len(rc)} 张表入快照（下界 {MIN_TABLES}）"
            + ("" if ok else " ⇒ 迁移没跑完，对账范围静默缩水"),
        ))

        # ---------- ③ 恢复到独立库 + 计时 ----------
        _create_db(dst_db)
        pg_bin = _pg_bin()
        env = dict(os.environ, PATH=f"{pg_bin}:{os.environ.get('PATH','')}")
        import time

        t0 = time.monotonic()
        proc = subprocess.run(
            [f"{pg_bin}/pg_restore", "-d", _dsn(dst_db), "--no-owner", "--no-acl",
             str(dump_path)],
            capture_output=True, text=True, env=env,
        )
        restore_seconds = round(time.monotonic() - t0, 3)
        # pg_restore 对"对象已存在"等会返回非 0 但仍恢复成功；这里只把
        # **完全失败**（目标库连不上 / dump 损坏）当红。
        checks.append((
            "restore:pg_restore-ran", proc.returncode in (0, 1),
            f"exit={proc.returncode} 耗时 {restore_seconds}s"
            + ("" if proc.returncode in (0, 1) else f" ⇒ {(proc.stderr or '')[-600:]}"),
        ))

        ok = 0 < restore_seconds <= RTO_BUDGET_SECONDS
        checks.append((
            "restore:rto-within-budget", ok,
            f"恢复耗时 {restore_seconds}s（预算 {RTO_BUDGET_SECONDS}s）"
            " ⚠️ 本机数据量下必然远低于预算 ⇒ 本条在本机是**弱判据**，"
            "真正的 RTO 达标结论只能在内网真实数据量下得出",
        ))

        # ---------- ④ 三重比对 ----------
        with psycopg.connect(_dsn(dst_db)) as conn:
            with conn.cursor() as cur:
                dst_fp = backup_pg.compute_fingerprint(cur)
                dst_rc = backup_pg.row_counts(cur)
                dst_bd = backup_pg.business_digest(cur)
                dst_q = _queue_digest(cur)

        src_fp = manifest["fingerprint"]

        # ---------- ④-A 队列逐列对账（异常矩阵第 ⑭ 项） ----------
        # 源库的队列摘要在 seed 之后、备份之前算；恢复后算目标的。
        # ⚠️ 必须逐列，不能只比行数：`UPDATE outbox_events SET published_at = now()`
        # 行数一点不变，但那正是"未投递事件被错标成已投递" ⇒ 丢任务。
        with psycopg.connect(_dsn(src_db)) as conn:
            with conn.cursor() as cur:
                src_q = _queue_digest(cur)

        # A-1 逐列摘要相等（按表逐张报告，便于定位是哪张表坏）
        q_bad = [t for t in QUEUE_TABLES
                 if src_q[t]["rows_sha256"] != dst_q[t]["rows_sha256"]]
        q_detail = ", ".join(
            f"{t}:{dst_q[t]['rows']}行/{len(dst_q[t]['columns'])}列 "
            f"{dst_q[t]['rows_sha256'][:12]}…" for t in QUEUE_TABLES
        )
        checks.append((
            "queue:column-digests-equal", not q_bad,
            f"逐列摘要{'四表全等' if not q_bad else '不等：' + ','.join(q_bad)}（{q_detail}）"
            + ("" if not q_bad else
               "\n     源=  " + ", ".join(
                   f"{t}:{src_q[t]['rows_sha256'][:12]}" for t in q_bad)
               + "\n     目标=" + ", ".join(
                   f"{t}:{dst_q[t]['rows_sha256'][:12]}" for t in q_bad)),
        ))

        # A-2 恢复后队列非空（**防"恢复出来的队列是空的但摘要相等"**）
        # 这不是冗余：若 seed 与digest 的表清单不一致（漏了某张队列表），
        # A-1 只在"已声明的那几张"上相等，漏掉那张仍是空表。
        q_empty = [t for t in QUEUE_TABLES
                   if int(dst_q[t]["rows"]) < QUEUE_MIN_ROWS[t]]
        checks.append((
            "queue:not-empty-after-restore", not q_empty,
            "恢复后 " + ", ".join(
                f"{t}={dst_q[t]['rows']}(下界 {QUEUE_MIN_ROWS[t]})"
                for t in QUEUE_TABLES)
            + ("" if not q_empty else f" ⇒ 空表：{q_empty} ⇒ 该表对账是 0==0 恒真"),
        ))

        # A-3 投递状态三形态仍可区分（**防"逐列相等但全是同一种值"**）
        # 逐列摘要相等有个盲区：如果所有行的 `published_at` 都相同
        # （比如 seed 退化成全未投递），摘要照样自洽，但"已投递事件恢复后
        # 仍是已投递"这条**根本没被验**。故断言三形态都真实存在。
        with psycopg.connect(_dsn(dst_db)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select count(*) filter (where published_at is not null),"
                    "       count(*) filter (where published_at is null"
                    "                          and next_attempt_at is null),"
                    "       count(*) filter (where published_at is null"
                    "                          and next_attempt_at is not null),"
                    "       count(*) filter (where attempts > 0)"
                    "  from public.outbox_events"
                )
                pub, pend, backoff, retried = (int(x) for x in cur.fetchone())
        triad_ok = pub >= 1 and pend >= 1 and backoff >= 1 and retried >= 1
        checks.append((
            "queue:publish-triad-present", triad_ok,
            f"已投递={pub} 未投递={pend} 退避中={backoff} 曾重试={retried}"
            + ("" if triad_ok else
               " ⇒ 三种投递状态有形态缺失 ⇒'已投递恢复后仍已投递'未被验证"),
        ))

        # ④-a 身份集合（硬断言，无需规范化）
        obj_diff = [k for k in backup_pg.FINGERPRINT_CLASSES
                    if src_fp[k]["objects_sha256"] != dst_fp[k]["objects_sha256"]]  # type: ignore[index]
        counts = ", ".join(
            f"{k}={dst_fp[k]['count']}" for k in backup_pg.FINGERPRINT_CLASSES  # type: ignore[index]
        )
        checks.append((
            "compare:object-sets-equal", not obj_diff,
            f"身份集合{'全等' if not obj_diff else '不等：' + ','.join(obj_diff)}（{counts}）"
            + ("" if not obj_diff else
               f"\n     源={ {k: src_fp[k]['objects_sha256'] for k in obj_diff} }"  # type: ignore[index]
               f"\n     目标={ {k: dst_fp[k]['objects_sha256'] for k in obj_diff} }"),  # type: ignore[index]
        ))

        # ④-b 规范化定义文本（软信号；规范化规则见 backup_pg.normalize_def）
        def_diff = [k for k in backup_pg.FINGERPRINT_CLASSES
                    if src_fp[k].get("defs_sha256") != dst_fp[k].get("defs_sha256")]  # type: ignore[index]
        checks.append((
            "compare:definition-text-equal", not def_diff,
            f"规范化定义{'全等' if not def_diff else '不等：' + ','.join(def_diff)}"
            + ("" if not def_diff else "（⚠️ 若数量相等而文本不等，多半是"
                                      " dump/restore 反解析差异，见 backup_pg 文档）"),
        ))

        # ④-c 规范化不得过度抹平：去重数量下降 = 两种不同定义被抹成同形
        # 已知等价对（KNOWN_EQUIVALENT_DEFS）每对只贡献 1 次抹平，从 constraints 的差额里扣掉；
        # 其它类别、以及超出这一对的任何额外抹平，仍然报红。
        known_collapse = len(KNOWN_EQUIVALENT_DEFS) - 1
        collapsed = []
        for k in backup_pg.FINGERPRINT_CLASSES:
            if "distinct_raw_defs" not in dst_fp[k]:  # type: ignore[index]
                continue
            lost = int(dst_fp[k]["distinct_raw_defs"]) - int(dst_fp[k]["distinct_norm_defs"])  # type: ignore[index]
            if k == "constraints":
                lost -= known_collapse
            if lost != 0:
                collapsed.append(
                    f"{k}({dst_fp[k]['distinct_raw_defs']}→{dst_fp[k]['distinct_norm_defs']}，"  # type: ignore[index]
                    f"扣除已知等价对后仍多抹平 {lost}）"
                )
        checks.append((
            "normalizer:no-over-collapse", not collapsed,
            "规范化未抹平任何不同的定义"
            + ("" if not collapsed else f" ⇒ 被抹平：{', '.join(collapsed)}"
                                        "（规范化成了'把判据调绿的旋钮'）"),
        ))

        # ④-d 白名单不得空转：每条已知等价约束必须仍在库里、原文与登记一致，且规范化后确实同形
        with psycopg.connect(_dsn(dst_db)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select c.conrelid::regclass::text, c.conname, pg_get_constraintdef(c.oid)"
                    "  from pg_constraint c join pg_namespace n on n.oid = c.connamespace"
                    " where n.nspname = 'public'"
                )
                live_defs = {(t, n): d for t, n, d in cur.fetchall()}
        stale = [
            f"{t}.{n}" for t, n, raw in KNOWN_EQUIVALENT_DEFS if live_defs.get((t, n)) != raw
        ]
        norm_forms = {backup_pg.normalize_def(raw) for _t, _n, raw in KNOWN_EQUIVALENT_DEFS}
        stale_ok = not stale and len(norm_forms) == 1 and len(
            {raw for _t, _n, raw in KNOWN_EQUIVALENT_DEFS}
        ) == len(KNOWN_EQUIVALENT_DEFS)
        checks.append((
            "normalizer:known-equivalents-not-stale", stale_ok,
            f"已知等价对 {len(KNOWN_EQUIVALENT_DEFS)} 条仍在库、原文一致、规范化后同形"
            + ("" if stale_ok else
               f" ⇒ 白名单腐烂：缺失或原文已变={stale or '无'}，规范化形态数={len(norm_forms)}（须为 1）"),
        ))

        rc_diff = sorted(
            t for t in set(rc) | set(dst_rc) if rc.get(t) != dst_rc.get(t)
        )
        checks.append((
            "compare:row-counts-equal", not rc_diff,
            f"{len(rc)} 张表行数{'逐表相等' if not rc_diff else '不等：' + ','.join(rc_diff[:10])}",
        ))

        bd_ok = manifest["business_digest"] == dst_bd
        checks.append((
            "compare:business-digest-equal", bd_ok,
            f"version_nodes={dst_bd['version_nodes_rows']} 行，"
            f"payload_hash 集合摘要 {dst_bd['payload_hash_set_sha256'][:16]}…"
            + ("" if bd_ok else f" ⇒ 源={manifest['business_digest']} 目标={dst_bd}"),
        ))

        # ---------- ⑤ 注入破坏反向验证：判据必须自证灵敏 ----------
        # 删一个触发器 → 结构指纹必须变（否则"指纹全等"是恒真的）。
        # ⚠️ 必须是 **DROP** 不是 DISABLE：`pg_get_triggerdef` 的输出**不含**启用标志
        # （`tgenabled`），DISABLE 后指纹纹丝不动 ⇒ 反向验证会假红。2026-10-07 确认。
        with psycopg.connect(_dsn(dst_db), autocommit=True) as conn:
            row = conn.execute(
                "select tgname, tgrelid::regclass::text from pg_trigger"
                " where not tgisinternal order by tgname limit 1"
            ).fetchone()
            if row is None:
                checks.append((
                    "reverse:fingerprint-detects-trigger-loss", False,
                    "库里一个非内部触发器都没有 ⇒ 注入破坏无从下手，本条无法自证灵敏",
                ))
            else:
                trig, tbl = row[0], row[1]
                conn.execute(f'drop trigger "{trig}" on public."{tbl}"')
                with psycopg.connect(_dsn(dst_db)) as c2:
                    with c2.cursor() as cur:
                        mutated_fp = backup_pg.compute_fingerprint(cur)
                # 逐层判定：身份集合要变，规范化定义也要变（后者防"规范化抹平"）
                obj_changed = (mutated_fp["triggers"]["objects_sha256"]
                               != dst_fp["triggers"]["objects_sha256"])
                def_changed = (mutated_fp["triggers"].get("defs_sha256")
                               != dst_fp["triggers"].get("defs_sha256"))
                fp_changed = obj_changed and def_changed
                checks.append((
                    "reverse:fingerprint-detects-trigger-loss", fp_changed,
                    f"DROP 触发器 {trig}@{tbl} 后 身份{'变' if obj_changed else '未变'}"
                    f"/定义{'变' if def_changed else '未变'}"
                    f"（{'两层都变 ⇒ 判据灵敏' if fp_changed else '有层未变 ⇒ 该层是恒真的'}）",
                ))

        # 删一行 → 行数必须变（否则"行数全等"是恒真的）。
        # ⚠️ 不能用 `version_nodes`：它是**不可变记录**，`rednote_reject_mutation()`
        # 触发器会直接抛 `RestrictViolation`（2026-10-07 实测）—— 这恰好反证"库里
        # 真的装了不可变约束"，但注入破坏得挑一张可变表，这里用种子建的 `users`。
        before = dst_rc.get("users", 0)
        try:
            with psycopg.connect(_dsn(dst_db), autocommit=True) as conn:
                conn.execute("delete from public.users where issuer = 'drill'")
                after = conn.execute("select count(*) from public.users").fetchone()[0]
            rc_changed = after != before
            detail = f"删行后 users {before} → {after}"
        except Exception as exc:  # noqa: BLE001 - 注入失败本身就是要报出来的事实
            rc_changed = False
            detail = f"注入失败：{type(exc).__name__}: {exc}"
        checks.append((
            "reverse:row-count-detects-deletion", rc_changed,
            detail + f"（{'已变（判据灵敏）' if rc_changed else '未变 ⇒ 判据是恒真的'}）",
        ))

        # ---------- 注入破坏：队列摘要必须变（否则"队列逐列相等"是恒真的） ----------
        # ⚠️ 破坏动作选 `UPDATE ... SET published_at = now()` 而不是 DELETE：
        # 删行 `compare:row-counts-equal` 已经能抓到，而这一条要验的是**逐列摘要**
        # 能不能抓到"行数不变但内容变了"。只 DELETE 的话，摘要相等可能只是因为
        # 两张表的行数恰好一起变，逐列比对的灵敏度**从未被证明**。
        # 挑`outbox_events` 是因为它有"未投递 → 已投递"这个**丢任务**的真实后果。
        q_probe_before = dst_q["outbox_events"]["rows_sha256"]
        try:
            with psycopg.connect(_dsn(dst_db), autocommit=True) as conn:
                mutated = conn.execute(
                    "update public.outbox_events set published_at = now()"
                    " where published_at is null returning id"
                ).rowcount
            with psycopg.connect(_dsn(dst_db)) as c2:
                with c2.cursor() as cur:
                    q_probe_after = _queue_digest(cur)["outbox_events"]["rows_sha256"]
            q_digest_changed = q_probe_after != q_probe_before
            q_detail2 = (
                f"把 {mutated} 条未投递事件标成已投递后，outbox 摘要 "
                f"{'变' if q_digest_changed else '未变'}"
                f"（{q_probe_before[:12]}… → {q_probe_after[:12]}…）"
            )
            q_reverse_ok = q_digest_changed and mutated >= 1
        except Exception as exc:  # noqa: BLE001 - 注入失败本身就是要报出来的事实
            q_reverse_ok = False
            q_detail2 = f"注入失败：{type(exc).__name__}: {exc}"
        checks.append((
            "reverse:queue-digest-detects-content-change", q_reverse_ok,
            q_detail2 + ("（逐列摘要判据灵敏）" if q_reverse_ok else
                         " ⇒ 逐列摘要对内容变化不敏感 ⇒ 它比行数强在哪要说清"),
        ))

        # 证据落盘：演练记录（WP47 ⑥ 要求的"耗时 vs RTO"）
        evidence_dir = REPO_ROOT / "evidence" / "m5"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        rec = {
            "drill_at": datetime.now(timezone.utc).isoformat(),
            "source_db": src_db,
            "target_db": dst_db,
            "dump_bytes": manifest["dump_bytes"],
            "dump_sha256": manifest["dump_sha256"],
            "dump_seconds": manifest["dump_seconds"],
            "restore_seconds": restore_seconds,
            "rto_budget_seconds": RTO_BUDGET_SECONDS,
            "tables": len(rc),
            "rows_restored": sum(dst_rc.values()),
            "object_sets_equal": not obj_diff,
            "definition_text_equal": not def_diff,
            "normalizer_no_over_collapse": not collapsed,
            "row_counts_equal": not rc_diff,
            "business_digest_equal": bd_ok,
            # 队列对账结论（异常矩阵第 ⑭ 项）
            "queue_digests_equal": not q_bad,
            "queue_rows": {t: dst_q[t]["rows"] for t in QUEUE_TABLES},
            "queue_digest": {t: dst_q[t]["rows_sha256"] for t in QUEUE_TABLES},
            "queue_publish_triad": {
                "published": pub, "pending": pend,
                "backing_off": backoff, "retried": retried,
            },
            "queue_digest_detects_content_change": q_reverse_ok,
            "fingerprint": {k: dst_fp[k] for k in backup_pg.FINGERPRINT_CLASSES},
        }
        (evidence_dir / f"backup-restore-drill-{stamp}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        checks.append((
            "evidence:drill-record-written", True,
            f"evidence/m5/backup-restore-drill-{stamp}.json",
        ))

    finally:
        _drop_db(src_db)
        _drop_db(dst_db)

    return _report(checks)


def _read_model_states() -> dict[str, set[str]]:
    """从 ORM 读三张队列表的状态机取值。**读不到就抛**，不返回空集。

    为什么必须真读而不是抄：`drill_db.SEED_JOB_STATES` 是 `jobs/models.py` 的抄本
    （该文件不 import 业务代码，见其注释）。抄本必然漂移 ⇒ 判据读ORM 原文对账。

    ⚠️ **失败必须炸**：若这里 `except: return {}`，那么
    `set(model) - set(seed)` 恒为空 ⇒ `queue:states-match-model` **恒绿**，
    而它正是防"状态没被覆盖全"的那条。空集合与"完全一致"同形 ——
    与 ISS-143 / D0 fail-fast 同一族。
    """
    import importlib
    import pathlib
    import sys

    backend_src = REPO_ROOT / "services" / "backend" / "src"
    if str(backend_src) not in sys.path:
        sys.path.insert(0, str(backend_src))
    models = importlib.import_module("rednote.modules.jobs.models")
    return {
        "jobs": set(models.JOB_STATES),
        "job_attempts": set(models.ATTEMPT_STATES),
        "idempotency_records": set(models.IDEMPOTENCY_STATES),
    }


def _report(checks: list[tuple[str, bool, str]]) -> int:
    print(f"## 备份恢复演练（WP47）")
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'} {name}（{detail}）")
    failed = [n for n, ok, _ in checks if not ok]
    total = len(checks)
    print(f"\nRESULT={'PASS' if not failed else 'FAIL'} checks={total}/{EXPECTED_CHECKS} "
          f"failures={len(failed)}")
    if total != EXPECTED_CHECKS:
        print(f"  ⚠️ 判据条数 {total} ≠ 期望 {EXPECTED_CHECKS} —— 新增/删除判据须同步 "
              f"EXPECTED_CHECKS")
    return 1 if (failed or total != EXPECTED_CHECKS) else 0


if __name__ == "__main__":
    sys.exit(main())
