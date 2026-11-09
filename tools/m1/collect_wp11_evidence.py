#!/usr/bin/env python
"""WP11 证据采集脚本。

**为什么要有脚本而不是手工敲命令再粘贴日志**：
证据的价值在于"可复现"。手工粘贴的日志无法证明"这真的是当前代码跑出来的" ——
代码改过之后没人知道日志是不是旧的。脚本把"跑什么、记什么"固定下来，
任何人（或 CI）重跑一次就能得到等价证据。

产出的文件（全部落在 `evidence/m1/`）：

| 文件 | 内容 |
|---|---|
| `wp11-versions.log` | 表结构 / 触发器 / 函数 / 迁移版本核对，+ **不可变性的活性验证** |

设计约定（沿用 `collect_wp10_evidence.py`）：
  - **一律用绝对路径**。相对路径会随调用方的 cwd 变化而失效。
  - **记录退出码**。只看输出不看退出码会漏掉"命令失败但打印了看似正常的内容"。
  - **核对项问库，不问 ORM**。ORM 定义与迁移实际落库的结果可能不一致
    （autogenerate 漏项、人工审校改错都真实发生过）。

**关于"活性验证"**：只查 `pg_trigger` 能证明触发器**存在**，不能证明它**生效**
（函数体写错、`ERRCODE` 写错、触发器挂在错误的表上，查询一样查得到）。
因此这里额外对一个真实节点试写 UPDATE / DELETE，捕获异常并断言 `sqlstate=23001`，
随后回滚 —— 数据不变，但"保护确实在工作"变成了可观测事实。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "services" / "backend"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m1"

# 期望条数（与内联脚本里的 EXPECTED_CHECKS 保持同值）。
# 两处都要：内联脚本用它写进 RESULT 行，外层用它判定成功与否。
EXPECTED_CHECKS: int = 18

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# 内联脚本：用常量而不是 f-string 拼接（脚本里有 % 与 {}，拼接易错且难读）。
# ---------------------------------------------------------------------------

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
EXPECTED_CHECKS = 18

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
REVISION_INTRODUCED = "cedfe85c1bf9"
COLUMN_CHECKS = [
    ("version_nodes 列数", "version_nodes", 9),
    ("version_dependencies 列数", "version_dependencies", 5),
]
TRIGGERS = (
    "trg_version_nodes_immutable",
    "trg_version_dependencies_immutable",
    "trg_version_dependencies_scope",
)
FUNCTIONS = ("rednote_reject_mutation", "rednote_check_dependency_scope")
# G2 铁律：autogenerate 会周期性提议删掉 M0 冒烟残留表，必须证明它们还在。
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
    for label, table, expected in COLUMN_CHECKS:
        actual = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_schema='public' AND table_name=:t"
            ),
            {"t": table},
        ).scalar_one()
        emit(actual == expected, label, f"{actual} (期望 {expected})")

    for name in TRIGGERS:
        n = conn.execute(
            text(
                "SELECT count(*) FROM pg_trigger t"
                " JOIN pg_class c ON c.oid = t.tgrelid"
                " WHERE NOT t.tgisinternal AND t.tgname = :n"
            ),
            {"n": name},
        ).scalar_one()
        emit(n == 1, f"触发器存在 {name}", n)

    for name in FUNCTIONS:
        n = conn.execute(
            text("SELECT count(*) FROM pg_proc WHERE proname = :n"), {"n": name}
        ).scalar_one()
        emit(n == 1, f"函数存在 {name}", n)

    version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    # 只要当前 head **不早于**该包引入的版本即可（见常量处的说明）。
    # 真正的"该包的东西还在不在"由本脚本后面的表/索引/触发器核对保证。
    emit(
        _revision_is_at_least(version, REVISION_INTRODUCED),
        "alembic_version 不早于本包引入的版本",
        f"{version} (本包引入 {REVISION_INTRODUCED})",
    )

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
    print("--- 活性验证：触发器真的拦得住写入（自造探针行 + 试写 + 整体回滚）---")

# **为什么不"从库里捞一行现成的 version_nodes 再改它"**（2026-09-30 修）：
# 那个写法把这条判据**建立在"库里恰好有业务数据"这个前提上**。默认验收库
# `postgres` 已在 WP-DEBT-02 按 ISS-145 清空（66 万行 → 0），于是它退化成
# `SKIP 不可变性试写：version_nodes 当前无数据`，CHECK 条数从 18 掉到 11
# ⇒ 整个脚本 FAIL。
# 更值得记的是它的**错误形态**：判据把"环境状态（有没有存量行）"当成了
# "被测性质（触发器生不生效）"。**"无数据 ⇒ 跳过"与"保护失效 ⇒ 报红"
# 在观感上毫无关系，但在"这条脚本能不能过"这一维上都是红** —— 于是
# 一个纯粹的环境变更（清库）会让一条本该恒绿的判据变红，而真正的
# "保护失效"混在同一片红里，无法区分（判据纪律第 9 条 / ISS-95 同族）。
# 改为**自造探针行**：插入一行 `entity_type='CONTENT'` 的节点
# （实测该取值没有配对约束触发器 —— `trg_version_nodes_*_paired` 只挂在
# BASELINE / BRIEF / KNOWLEDGE / PROMPT 四个取值上），试写 UPDATE/DELETE
# 拿判定，最后整体 rollback —— 一行不留，且**不再依赖库里有没有数据**。
# 这与 WP18/19/20/22/23 各包的"自建数据 + 回滚"形态对齐。
with engine.connect() as wconn:
    trans = wconn.begin()
    probe_id = uuid.uuid4()
    wconn.execute(
        text(
            "INSERT INTO version_nodes"
            " (id, entity_type, entity_id, revision_no, payload_hash)"
            " VALUES (:i, 'CONTENT', :e, 1, :h)"
        ),
        {"i": probe_id, "e": uuid.uuid4(), "h": "0" * 64},
    )
    for label, sql in (
        ("UPDATE", "UPDATE version_nodes SET payload_hash = :v WHERE id = :i"),
        ("DELETE", "DELETE FROM version_nodes WHERE id = :i"),
    ):
        # 每条试写包一个 SAVEPOINT：PG 中任何失败语句都会把整个事务置为
        # aborted，不回到保存点的话后续语句全部报 current transaction is
        # aborted（WP10/ISS-22 踩过同一个坑）。
        try:
            with wconn.begin_nested():
                wconn.execute(text(sql), {"v": "1" * 64, "i": probe_id})
            emit(False, f"不可变性试写 {label}", "未被拦截（保护失效）")
        except DBAPIError as exc:
            state = getattr(exc.orig, "sqlstate", None)
            emit(state == "23001", f"不可变性试写 {label}", f"被拦截 sqlstate={state} (期望 23001)")
    # **整体回滚** —— 探针行一条不留。
    trans.rollback()

print()
print("--- 索引清单 ---")
with engine.connect() as conn:
    rows = conn.execute(
        text(
            "SELECT indexname FROM pg_indexes"
            " WHERE schemaname='public'"
            "   AND tablename IN ('version_nodes','version_dependencies')"
            " ORDER BY indexname"
        )
    ).all()
    for (name,) in rows:
        print(f"INFO  索引 {name}")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def collect_wp11() -> tuple[Path, bool]:
    """WP11 专项：表结构 + 触发器/函数存在性 + 不可变性活性验证。"""
    import os
    import subprocess

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
    checks = [line for line in output.splitlines() if line.startswith(("CHECK", "SKIP", "INFO"))]
    failures = [line for line in checks if line.startswith("CHECK FAIL")]

    lines = [
        "# WP11 版本注册与引用：表结构、触发器、不可变性活性验证",
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
    target = EVIDENCE_DIR / "wp11-versions.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # **条数不足也算失败**（ISS-43）：脚本中途抛异常时，
    # "本该产生的 CHECK 一条都没产生"与"全部通过"在计数上一模一样，
    # 只判 `not failures` 会让崩溃伪装成完美通过。
    complete = len(checks) >= EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete)


def main() -> int:
    path, ok = collect_wp11()
    print(f"{'OK  ' if ok else 'FAIL'} {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
