#!/usr/bin/env python
"""WP17 证据采集脚本。

产出的文件（落在 `evidence/m1/`）：

| 文件 | 内容 |
|---|---|
| `wp17-parsing.log` | 2 张表结构 / 索引 / 约束 / 触发器 / 迁移版本，+ **五处活性验证** |

**五处活性验证**（结构核对只能证明约束**存在**，不能证明它**生效**）：

1. **虚报成功进不去库里**：试插三种 `SUCCEEDED` 但没全解出来的组合，
   断言被 CHECK 拦下（SQLSTATE `23514`）。这是本包验收第二半
   「损坏及不支持格式不虚报成功」的**数据库级证据**。
2. **PARTIAL 的边界**：试插 `PARTIAL` 但 `parsed=0`（应为 FAILED）与
   `PARTIAL` 但 `parsed=total`（应为 SUCCEEDED），断言被拦（`23514`）。
3. **片段不可篡改**：对真实片段试写 `UPDATE` / `DELETE`，断言被触发器
   拦下（`23001`）。
4. **定位与内容约束**：试插空 `text_content` 与 `position = {}`，
   断言被拦（`23514`）。
5. **顺序事实存在**：断言 `source_segments.segment_no` 列与它的唯一约束
   存在 —— 没有它，"按原文顺序拿片段"会退化成按随机 UUID 排序
   （本机实测过这个缺陷）。

**跨包回归项**（本包最容易误伤别人的地方）：

- 共用的不可变触发器函数 `rednote_reject_mutation()` 必须仍在
  （WP17 的 `downgrade` 刻意不删它 —— 所有不可变表共用）；
- `trg_audit_events_immutable`（WP15）与 `trg_gateway_egress_immutable`（WP16）
  必须仍存在；
- `parse_runs`（可变表）**不该**有不可变触发器。

**探针数据的处置**：全部放在**一个事务里** + `begin_nested()` 逐个
SAVEPOINT，最后整体 rollback → **零残留**。理由：`source_segments` 不可变
（触发器拦 DELETE），一旦提交就删不掉，而它用外键钉住 `parse_runs`、
后者钉住 `file_objects`（外键 RESTRICT）—— 提交后连"清理可变部分"都做不到。
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
EXPECTED_CHECKS: int = 31

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
EXPECTED_CHECKS = 31

DB = "postgresql+psycopg://rednote@localhost:5432/postgres"
engine = create_engine(DB)

CONTRACT_PATH = Path(
    "/Users/<user>/内容运营AI Native/rednote-production/contracts/openapi.json"
)

# 该包**引入**的迁移版本；判据是"不早于"而不是"恰好等于"（ISS-44）。
REVISION_INTRODUCED = "55e35f7a4cb9"

TABLE_COLUMNS = [
    ("parse_runs", 15),
    ("source_segments", 12),
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


def constraint_name(exc):
    return getattr(getattr(exc.orig, "diag", None), "constraint_name", None)


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
            " WHERE NOT tgisinternal AND tgname = 'trg_source_segments_immutable'"
        )
    ).scalar_one()
    emit(n == 1, "触发器存在 trg_source_segments_immutable", n)

    # 共用函数必须仍在（WP17 的 downgrade 刻意不删它）。
    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname = 'rednote_reject_mutation'")
    ).scalar_one()
    emit(n == 1, "共用不可变函数 rednote_reject_mutation 仍在", n)

    # 跨包回归：WP15 / WP16 的触发器不能被本包误删。
    for name in ("trg_audit_events_immutable", "trg_gateway_egress_immutable"):
        n = conn.execute(
            text(
                "SELECT count(*) FROM pg_trigger"
                " WHERE NOT tgisinternal AND tgname = :n"
            ),
            {"n": name},
        ).scalar_one()
        emit(n == 1, f"跨包回归：{name} 仍在", n)

    # 可变表不该有不可变触发器（parse_runs 的状态与计数必须能推进）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
            " WHERE NOT t.tgisinternal AND c.relname = 'parse_runs'"
        )
    ).scalar_one()
    emit(n == 0, "parse_runs 无可变触发器（它必须可改）", n)

    # 三条"不虚报成功"的 CHECK 必须存在。
    for name in (
        "ck_parse_runs_parsed_within_total",
        "ck_parse_runs_succeeded_means_all_parsed",
        "ck_parse_runs_partial_means_some_parsed",
        "ck_parse_runs_terminal_has_finished_at",
    ):
        n = conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint"
                " WHERE conrelid = 'parse_runs'::regclass AND contype = 'c'"
                " AND conname = :n"
            ),
            {"n": name},
        ).scalar_one()
        emit(n == 1, f"CHECK 存在 {name}", n)

    # 顺序事实：`segment_no` 列 + 它的唯一约束（否则顺序会退化成随机 UUID）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM information_schema.columns"
            " WHERE table_schema='public' AND table_name='source_segments'"
            " AND column_name='segment_no'"
        )
    ).scalar_one()
    emit(n == 1, "source_segments.segment_no 列存在（顺序是显式事实）", n)

    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conrelid = 'source_segments'::regclass AND contype = 'u'"
            " AND conname = 'uq_source_segments_run_segment_no'"
        )
    ).scalar_one()
    emit(n == 1, "唯一约束 uq_source_segments_run_segment_no 存在", n)

    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conrelid = 'source_segments'::regclass AND contype = 'u'"
            " AND conname = 'uq_source_segments_run_hash_position'"
        )
    ).scalar_one()
    emit(n == 1, "唯一约束 uq_source_segments_run_hash_position 存在", n)

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
print("--- 活性验证 1：虚报成功进不去库里（试插三种非法组合）---")
print("--- 活性验证 2：PARTIAL 的两个边界 ---")
print("--- 活性验证 3：片段不可篡改（UPDATE / DELETE）---")
print("--- 活性验证 4：定位与内容约束（空文本 / 空 position）---")
print("--- 活性验证 5：合法组合必须能写进去（反向用例）---")

# **单事务 + SAVEPOINT**：所有探针都在一个事务里完成，最后整体回滚 ——
# 零残留。每次"预期失败"的写各包一个 SAVEPOINT，因为 PG 中任何失败语句
# 都会把整个事务置为 aborted，除非回滚到该语句之前的保存点
# （WP10/ISS-22 踩过同一个坑）。
#
# **为什么不像 WP15 那样"提交探针行、不清理"**：`source_segments` 不可变
# （触发器拦 DELETE），一行片段会**永久钉住**它引用的 parse_run，
# 后者钉住 file_object（外键 RESTRICT）—— 提交后连"清理可变部分"都做不到。
with engine.connect() as wconn:
    trans = wconn.begin()
    probe = uuid.uuid4().hex[:10]

    project_id = wconn.execute(
        text(
            "INSERT INTO projects (project_code, name, brand_id, product_id,"
            " owner_id, target_delivery_count, due_date)"
            " SELECT :code, '探针项目', b.id, p.id, u.id, 1, CURRENT_DATE + 1"
            " FROM users u, brands b, products p"
            " WHERE p.brand_id = b.id LIMIT 1"
            " RETURNING id"
        ),
        {"code": f"PROBE-{probe.upper()}"},
    ).scalar_one_or_none()

    if project_id is None:
        # 库里还没有品牌/产品/用户（首次跑）：造一套最小的。
        owner_id = wconn.execute(
            text(
                "INSERT INTO users (issuer, subject, display_name, role)"
                " VALUES ('probe', :s, '探针用户', 'ADMIN') RETURNING id"
            ),
            {"s": f"probe-{probe}"},
        ).scalar_one()
        brand_id = wconn.execute(
            text("INSERT INTO brands (name) VALUES (:n) RETURNING id"),
            {"n": f"探针品牌-{probe}"},
        ).scalar_one()
        product_id = wconn.execute(
            text(
                "INSERT INTO products (brand_id, name) VALUES (:b, :n) RETURNING id"
            ),
            {"b": brand_id, "n": f"探针产品-{probe}"},
        ).scalar_one()
        project_id = wconn.execute(
            text(
                "INSERT INTO projects (project_code, name, brand_id, product_id,"
                " owner_id, target_delivery_count, due_date)"
                " VALUES (:code, '探针项目', :b, :p, :o, 1, CURRENT_DATE + 1)"
                " RETURNING id"
            ),
            {
                "code": f"PROBE-{probe.upper()}",
                "b": brand_id,
                "p": product_id,
                "o": owner_id,
            },
        ).scalar_one()
        owner_id = owner_id
    else:
        owner_id = wconn.execute(
            text("SELECT owner_id FROM projects WHERE id = :p"), {"p": project_id}
        ).scalar_one()

    blob_id = wconn.execute(
        text(
            "INSERT INTO blobs (storage_backend, object_key, sha256, byte_size,"
            " mime_type) VALUES ('LOCAL', :k, :s, 3, 'text/plain') RETURNING id"
        ),
        {"k": f"probe/{probe}", "s": (probe * 7)[:64].ljust(64, "0")},
    ).scalar_one()

    file_id = wconn.execute(
        text(
            "INSERT INTO file_objects (blob_id, original_name, visibility,"
            " project_id, classification, uploaded_by)"
            " VALUES (:b, 'probe.txt', 'PROJECT', :p, 'SUPPORTED', :o)"
            " RETURNING id"
        ),
        {"b": blob_id, "p": project_id, "o": owner_id},
    ).scalar_one()

    # --- 活性验证 1：虚报成功（三种形态）---
    for label, state, total, parsed in (
        ("虚报成功：SUCCEEDED 但只解出一部分", "SUCCEEDED", 20, 3),
        ("虚报成功：SUCCEEDED 但不知道总量", "SUCCEEDED", None, 3),
        ("虚报成功：SUCCEEDED 但一个都没解出来", "SUCCEEDED", 0, 0),
    ):
        try:
            with wconn.begin_nested():
                wconn.execute(
                    text(
                        "INSERT INTO parse_runs (file_id, parser_version, state,"
                        " total_units, parsed_units, finished_at)"
                        " VALUES (:f, 'probe', :s, :t, :p, now())"
                    ),
                    {"f": file_id, "s": state, "t": total, "p": parsed},
                )
            emit(False, label, "未被拦截（虚报成功可以进库）")
        except DBAPIError as exc:
            emit(
                sqlstate(exc) == "23514",
                label,
                f"被拦截 sqlstate={sqlstate(exc)}"
                f" constraint={constraint_name(exc)} (期望 23514)",
            )

    # --- 活性验证 2：PARTIAL 的两个边界 ---
    for label, state, total, parsed in (
        ("PARTIAL 但一个都没解出来（应为 FAILED）", "PARTIAL", 20, 0),
        ("PARTIAL 但全解出来了（应为 SUCCEEDED）", "PARTIAL", 20, 20),
    ):
        try:
            with wconn.begin_nested():
                wconn.execute(
                    text(
                        "INSERT INTO parse_runs (file_id, parser_version, state,"
                        " total_units, parsed_units, finished_at)"
                        " VALUES (:f, 'probe', :s, :t, :p, now())"
                    ),
                    {"f": file_id, "s": state, "t": total, "p": parsed},
                )
            emit(False, label, "未被拦截（PARTIAL 的边界失效）")
        except DBAPIError as exc:
            emit(
                sqlstate(exc) == "23514",
                label,
                f"被拦截 sqlstate={sqlstate(exc)}"
                f" constraint={constraint_name(exc)} (期望 23514)",
            )

    # --- 活性验证 5（先做，因为它要造合法的 run 供后续用）---
    run_id = None
    try:
        with wconn.begin_nested():
            run_id = wconn.execute(
                text(
                    "INSERT INTO parse_runs (file_id, parser_version, state,"
                    " total_units, parsed_units, finished_at)"
                    " VALUES (:f, 'probe-ok', 'PARTIAL', 3, 1, now())"
                    " RETURNING id"
                ),
                {"f": file_id},
            ).scalar_one()
        emit(True, "合法组合可写：PARTIAL 3/1", "写入成功（反向用例）")
    except DBAPIError as exc:
        emit(False, "合法组合可写：PARTIAL 3/1", f"被错误拦截: {exc}")

    if run_id is None:
        emit(False, "片段探针的前置条件", "无法造出合法 run，后续片段验证跳过")
        trans.rollback()
    else:
        # --- 活性验证 4：定位与内容约束 ---
        for label, content, position in (
            ("空 text_content 被拦", "", '{"kind":"LINE","line":1}'),
            ("空 position 被拦（{} 不是定位）", "x", "{}"),
        ):
            try:
                with wconn.begin_nested():
                    wconn.execute(
                        text(
                            "INSERT INTO source_segments (parse_run_id, segment_no,"
                            " position, text_content, content_hash, extraction_kind)"
                            " VALUES (:r, 1, cast(:pos as jsonb), :c, :h, 'TEXT')"
                        ),
                        {
                            "r": run_id,
                            "pos": position,
                            "c": content,
                            "h": "0" * 64,
                        },
                    )
                emit(False, label, "未被拦截")
            except DBAPIError as exc:
                emit(
                    sqlstate(exc) == "23514",
                    label,
                    f"被拦截 sqlstate={sqlstate(exc)}"
                    f" constraint={constraint_name(exc)} (期望 23514)",
                )

        # 造一行真实片段供"不可篡改"验证用。
        #
        # **`position` 必须走绑定参数而不是内联 jsonb 字面量**：
        # 内联写法 `'{"kind":"PAGE","page":1,...}'::jsonb` 里的 `:1`
        # 会被 SQLAlchemy 当成**绑定参数**（报 "A value is required for
        # bind parameter '1'"）。本包在测试里已经踩过一次同样的坑，
        # 这里是第二次 —— 凡 jsonb 字面量含数字键值，一律用 `cast(:p as jsonb)`。
        segment_id = None
        try:
            with wconn.begin_nested():
                segment_id = wconn.execute(
                    text(
                        "INSERT INTO source_segments (parse_run_id, segment_no,"
                        " position, text_content, content_hash, extraction_kind,"
                        " confidence)"
                        " VALUES (:r, 1, cast(:pos as jsonb), '探针片段文本', :h,"
                        " 'OCR', 0.9) RETURNING id"
                    ),
                    {
                        "r": run_id,
                        "pos": '{"kind":"PAGE","page":1,"bbox":[0.1,0.1,0.9,0.2]}',
                        "h": "a" * 64,
                    },
                ).scalar_one()
            emit(True, "片段可写（含 bbox 与 confidence）", "写入成功")
        except DBAPIError as exc:
            emit(False, "片段可写（含 bbox 与 confidence）", f"写入失败: {exc}")

        # --- 活性验证 3：不可篡改 ---
        if segment_id is not None:
            for label, sql in (
                (
                    "片段不可篡改 UPDATE",
                    "UPDATE source_segments SET text_content = '改过了' WHERE id = :i",
                ),
                ("片段不可篡改 DELETE", "DELETE FROM source_segments WHERE id = :i"),
            ):
                try:
                    with wconn.begin_nested():
                        wconn.execute(text(sql), {"i": str(segment_id)})
                    emit(False, label, "未被拦截（保护失效）")
                except DBAPIError as exc:
                    emit(
                        sqlstate(exc) == "23001",
                        label,
                        f"被拦截 sqlstate={sqlstate(exc)} (期望 23001)",
                    )

            # 顺序事实：同一 run 内 segment_no 唯一。
            try:
                with wconn.begin_nested():
                    wconn.execute(
                        text(
                            "INSERT INTO source_segments (parse_run_id, segment_no,"
                            " position, text_content, content_hash, extraction_kind)"
                            " VALUES (:r, 1, cast(:pos as jsonb), '另一段', :h,"
                            " 'TEXT')"
                        ),
                        {
                            "r": run_id,
                            "pos": '{"kind":"PAGE","page":2}',
                            "h": "b" * 64,
                        },
                    )
                emit(False, "segment_no 在同一 run 内唯一", "未被拦截")
            except DBAPIError as exc:
                emit(
                    sqlstate(exc) == "23505",
                    "segment_no 在同一 run 内唯一",
                    f"被拦截 sqlstate={sqlstate(exc)}"
                    f" constraint={constraint_name(exc)} (期望 23505)",
                )

        # **整体回滚** —— 探针数据一条不留。
        trans.rollback()

print()
print("--- 契约核对：WP17 新增 1 个端点（协议 §4.2 第 7 行）---")
try:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    paths = contract["paths"]
    has_parse_jobs = any(
        p == "/api/v1/files/{fileId}/parse-jobs" for p in paths
    )
    emit(has_parse_jobs, "契约含 POST /files/{fileId}/parse-jobs", has_parse_jobs)
    # 解析状态挂在既有的 GET /files/{fileId} 上，不该另造查询端点。
    # **判据必须限定到 files 作用域**：最初写成 `"parse" in p`（全契约范围），
    # WP18 合法新增 `/projects/{id}/brief-parse-jobs` 后立刻被误判为退化。
    # 这条约束的主语是**本包的 file 解析**，不是"全系统任何路径都不许含 parse"。
    extra = [
        p
        for p in paths
        if "/files/" in p and "parse" in p and not p.endswith("/parse-jobs")
    ]
    emit(extra == [], "不另造解析查询端点（状态挂 GET /files/{fileId}）", extra or "无")
except Exception as exc:
    emit(False, "契约核对", f"读取失败: {exc}")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def collect_wp17() -> tuple[Path, bool]:
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
        "# WP17 文档解析与 OCR：2 张表 / 约束 / 索引 / 触发器 / 五处活性验证 / 契约",
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
    target = EVIDENCE_DIR / "wp17-parsing.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # **条数不足也算失败**（ISS-43）。
    complete = len(checks) >= EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete)


def main() -> int:
    path, ok = collect_wp17()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
