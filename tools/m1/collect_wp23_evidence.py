#!/usr/bin/env python
"""WP23 证据采集脚本。

产出的文件（落在 `evidence/m1/`）：

| 文件 | 内容 |
|---|---|
| `wp23-prompt.log` | 3 张表结构 / 索引 / 约束 / 6 个触发器 / 迁移版本，+ **九处活性验证** |

## 九处活性验证

结构核对只能证明约束**存在**，不能证明它**生效**。本包验收原文
（04 §4.3「模板变更可追溯，**不扩大工具权限**」）的第二条是一条安全约束 ——
安全约束的特征是"做不到不报错，只是有一个口子一直开着"。
因此每一条防线都必须**拿本该被拦的输入去试写**。

1. **工具上界受管（超集被拦）**：给 `prompt_versions.allowed_tools` 试写
   一个不在平台上界内的工具名 → 断言被 `ck_prompt_versions_allowed_tools_within_ceiling`
   拦下（`23514`）。这是「不扩大工具权限」**唯一一条绕过 service 也拦得住**的防线。
2. **工具上界（子集必须通过）** —— **第 1 条的对照组**：
   一个"见谁都拒"的 CHECK 也能让第 1 条全绿。因此必须断言
   `allowed_tools ⊆ 上界` 的合法子集能写进去，且空数组（`{}`）也能
   （空集是任何集合的子集，这是 fail-closed 的默认态）。
3. **不可变表真的不可篡改**：对真实 `prompt_versions` 行与
   `prompt_publications` 行试写 `UPDATE` → 断言被
   `rednote_reject_mutation()` 拦下（`23001`）。
   **同时**断言 `prompt_templates`（**刻意不是不可变表**）的 `UPDATE`
   **能通过** —— 否则一个"对所有表都加锁"的实现也能让前两条全绿，
   而它会挡住 `active_version_id` 这个投影列的更新（激活就做不了了）。
4. **OP-88 正向判据生效**：注册一个 `entity_type='PLAN'` 的版本节点，
   再插一行 `prompt_versions` 指向它 → 断言 INSERT 阶段**不报错**、
   强制校验时被延迟触发器拦下（`23514`）。
5. **OP-88 反向判据生效**：`prompt_templates` 行存在 + `entity_type='PROMPT'`
   的版本节点 + **不插** `prompt_versions` 行 → 断言被
   `trg_version_nodes_prompt_paired` 拦下（`23514`）。
   **注意反例的准确形状**：该触发器刻意收窄为"`entity_id` 是一个真实存在的
   模板时才强制"。因此"孤立节点"必须理解为"**有** template 行、**无** version 行"。
   **并且要有收窄放行的对照**：`entity_id` 指向不存在的模板时**不该**被拦。
6. **合法配对必须能通过**（第 4、5 条的**对照**）：
   `prompt_templates` + `PROMPT` 节点（`entity_id = template_id`）+
   `prompt_versions` 行 → 断言通过强制校验。
7. **激活事件的版本归属受管**：试插一行 `prompt_publications`，
   声明 `template_id = T` 但 `version_id` 属于模板 U → 断言被
   `trg_prompt_publications_template_scope` 拦下（`23514`）。
   这条防的是"投影会算出别人的版本是本模板的当前版本" ——
   那会让下游拿到**另一个任务类型**的提示词去生成。
8. **被取代版本的归属受管**：同上触发器里的**第二条**判据 ——
   `superseded_version_id` 指向别的模板的版本 → 断言被拦下。
   它是审计链的一部分（"这次激活取代了谁"），必须单独证明。
9. **生效版本的归属受管**：`active_version_id` 是**投影列**，
   它可被裸 SQL 就地改写（与 `prompt_versions` 不同，那张表有不可变触发器）。
   把模板 B 的生效版本改成模板 A 的版本 → 断言被
   `trg_prompt_templates_active_scope` 拦下（`23514`）。

## 探针自身踩过的四处坑（写在这里，避免重复踩）

本节是"证据脚本自己出错"的记录 —— 与 WP18 的 ISS-63 同源
（探针出错会**伪装成实现有缺陷**，比不写探针更危险）。

1. **内联脚本里不能有双引号嵌套**（`"……"只对真实模板强制"……"`）。
   症状是 `SyntaxError: invalid syntax`，而 `RESULT` 行显示
   `0 PASS / 0 FAIL` —— 崩溃伪装成"没东西可查"（ISS-43 的同型）。
2. **期望值必须实测**，不能凭印象：第一次跑错了 3 处 ——
   `prompt_templates` 是 6 列不是 7 列、CHECK 是 11 条不是 13 条、
   **上界里没有 `KNOWLEDGE_WRITE`**（合法子集探针自己撞了 CHECK，
   于是"对照组必须通过"被判成 FAIL）。
3. **一条语句报错会把整个事务打成 aborted**（后续语句返回 `25P02`）。
   因此"被拦"与"对照通过"这两类探针**必须各自独立事务** ——
   放同一个事务里会让对照拿到 `25P02` 而不是"能改"，误判成实现锁过头。
4. **同一模板下多个探针节点的 `revision_no` 必须不同**
   （`uq_version_nodes_entity_type_entity_id_revision_no` 会拦重复）。
   本脚本第一次跑时两个节点都用默认值 2，撞了唯一约束。

## 跨包回归项（本包最容易误伤别人的地方）

- 共用的不可变触发器函数 `rednote_reject_mutation()` 必须仍在
  （本包 `downgrade` 刻意不删它 —— 所有不可变表共用）；
- `version_nodes` 表**不得**被本包 downgrade 删除（本包只删挂在它上面的
  1 个触发器）；
- WP18 / WP20 的 `rednote_check_*` 函数必须存活（本包不碰别人的函数）；
- `prompt_versions.id` 指向 `version_nodes` 的外键必须是
  `DEFERRABLE INITIALLY DEFERRED`；
- **跨包 `TASK_KINDS` 对齐**：WP19 的取值必须是本包的子集
  （本包已实测过一次漂移，现在有机械判据）。

## 探针数据的处置

每条验证跑在**独立连接**里，用 `SET CONSTRAINTS ALL IMMEDIATE` 强制触发
延迟判据拿到结论，最后**无条件 rollback** → **零残留**。理由：
`prompt_versions` / `prompt_publications` 都不可变（触发器拦 DELETE），
一旦提交就删不掉（WP18 的 ISS-63 已实测）。

**为什么用 `SET CONSTRAINTS ALL IMMEDIATE` 而不是 `trans.commit()`**：
`commit()` 之后再 `rollback()` 只能回滚"空事务"，数据已落库 → 污染证据库。
强制校验把"提交时才跑"的延迟触发器提前到当前语句执行，事务仍未提交，
因此 rollback 之后一行不留。

**本脚本用在哪个库**：默认 `rednote_verify_wp23`（收口时新建的干净库，
迁移从 bootstrap 一路跑到 head 建出全部结构）。用它的理由：
分支测试库里的表是**测试夹具 create_all 建的**，不是迁移建的 ——
在那种库上取证据，等于在验证夹具而不是验证迁移。
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
EXPECTED_CHECKS: int = 46

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
EXPECTED_CHECKS = 46

DB = "postgresql+psycopg://rednote@localhost:5432/rednote_verify_wp23"
engine = create_engine(DB)

CONTRACT_PATH = Path(
    "/Users/<user>/内容运营AI Native/rednote-production/contracts/openapi.json"
)

# 本包引入的迁移版本。判据用"不早于"而不是"恰好等于"（ISS-44：
# 硬编码"恰好停在某个版本"会让每个新工作包把历史证据集体打红）。
REVISION_INTRODUCED = "3cb74d02edc5"

# 本包 3 张表的期望列数。
# ⚠️ 这几个数字必须按 information_schema **实测**填写，不能凭印象 ——
# 本脚本第一次跑就因此错了 3 处（把它当成"我记得是 7 列"）：
#   · prompt_templates 是 **6 列**（id/task_kind/name/active_version_id/
#     created_at/updated_at），没有额外的业务列；
#   · CHECK 总数是 **11** 而不是 13（`name_not_empty` 与 `task_kind_valid`
#     落在 templates，versions 6 条，publications 2 条）；
#   · 上界里的合法取值是 KNOWLEDGE_READ / PROJECT_READ / RESEARCH_SEARCH /
#     IMAGE_ANALYZE / CONTENT_DRAFT / METRIC_READ —— **没有 KNOWLEDGE_WRITE**。
TABLE_COLUMNS = (
    ("prompt_templates", 6),
    ("prompt_versions", 10),
    ("prompt_publications", 10),
)

# 本包 6 个触发器。
PROMPT_TRIGGERS = (
    "trg_prompt_versions_immutable",
    "trg_prompt_publications_immutable",
    "trg_prompt_versions_node_consistency",
    "trg_version_nodes_prompt_paired",
    "trg_prompt_publications_template_scope",
    "trg_prompt_templates_active_scope",
)

# 本包 4 个一致性函数。
PROMPT_FUNCTIONS = (
    "rednote_check_prompt_version_consistency",
    "rednote_check_prompt_node_paired",
    "rednote_check_prompt_publication_scope",
    "rednote_check_prompt_active_scope",
)

# 本包 CHECK 总数（实测 11：templates 2 + versions 6 + publications 2 + ...）。
PROMPT_CHECK_TOTAL = 11

# WP18 + WP20 的一致性函数总数（实测 8：brief 4 + knowledge 4）。
CROSS_PACK_FUNCTION_TOTAL = 8

# 平台上界里的两个工具（用于活性验证的"合法子集"）。
# **必须取自真实上界** —— 本脚本第一次跑时写了 `KNOWLEDGE_WRITE`，
# 而它不在上界里，于是"合法子集"探针自己撞了 CHECK，把
# "对照组必须通过"这条判成了 FAIL。
CEILING_SAMPLE = ("KNOWLEDGE_READ", "CONTENT_DRAFT")
# 一个**不在**上界内的工具名（超集探针）。
OUT_OF_CEILING = "ROOT_SHELL"

# 本包 3 张表（零残留判据用）。
OWN_TABLES = ("prompt_versions", "prompt_publications", "prompt_templates")


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


# ---------------------------------------------------------------------------
# 探针的公共前置：造项目 + 用户（可变表，可正常清理，但仍然整体 rollback）
# ---------------------------------------------------------------------------

def _seed_tx(conn):
    # 造最小可用的 project / user，返回 (project_id, user_id)。
    # ⚠️ 列名与非空集合全部按 information_schema 实测，不要凭印象写
    # （WP18 一次踩中 5 处；WP20 先后踩了 4 处列名、5 处非空）。
    #   · users    : 没有 email，是 (issuer, subject) 唯一
    #   · projects : 没有 code/created_by；brand_id / product_id /
    #                target_delivery_count / due_date 都是 NOT NULL
    pid = uuid.uuid4()
    uid = uuid.uuid4()
    brand = uuid.uuid4()
    prod = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, display_name, role, status)"
            " VALUES (:u, :iss, :sub, :n, 'ADMIN', 'ACTIVE')"
        ),
        {"u": str(uid), "iss": "wp23-probe", "sub": f"probe-{uid}", "n": "WP23 探针"},
    )
    conn.execute(
        text("INSERT INTO brands (id, name, status) VALUES (:b, :n, 'ACTIVE')"),
        {"b": str(brand), "n": f"WP23 探针品牌 {str(brand)[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO products (id, brand_id, name, model_code, status)"
            " VALUES (:p, :b, :n, :c, 'ACTIVE')"
        ),
        {
            "p": str(prod), "b": str(brand),
            "n": f"WP23 探针产品 {str(prod)[:8]}", "c": f"WP23-{str(prod)[:8]}",
        },
    )
    conn.execute(
        text(
            "INSERT INTO projects"
            " (id, project_code, name, brand_id, product_id, owner_id, phase,"
            "  target_delivery_count, due_date, timezone)"
            " VALUES (:p, :c, :n, :b, :pr, :u, 'PREPARATION', 1,"
            "         (now() + interval '30 days')::date, 'Asia/Shanghai')"
        ),
        {
            "p": str(pid), "c": f"WP23PROBE{str(pid)[:8]}", "n": "WP23 探针项目",
            "b": str(brand), "pr": str(prod), "u": str(uid),
        },
    )
    return pid, uid


def _template_tx(conn, *, task_kind="CONTENT_COPY", name=None):
    tid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO prompt_templates (id, task_kind, name)"
            " VALUES (:i, :k, :n)"
        ),
        {"i": str(tid), "k": task_kind, "n": name or f"WP23 探针模板 {str(tid)[:8]}"},
    )
    return tid


def _node_tx(conn, node_id, entity_type, entity_id, pid, uid, revision_no=2):
    # `revision_no` 可传：同一模板下**多个探针节点必须用不同修订号**
    # （`uq_version_nodes_entity_type_entity_id_revision_no` 会拦重复）。
    # 本脚本第一次跑时两个探针节点都用了默认值 2，撞了唯一约束。
    conn.execute(
        text(
            "INSERT INTO version_nodes"
            " (id, entity_type, entity_id, revision_no, project_id,"
            "  payload_hash, schema_version, created_by)"
            " VALUES (:i, :et, :eid, :rev, :p, :h, 1, :u)"
        ),
        {
            "i": str(node_id), "et": entity_type, "eid": str(entity_id),
            "rev": revision_no, "p": str(pid), "h": "a" * 64, "u": str(uid),
        },
    )


def _version_tx(conn, version_id, template_id, uid, *, tools_sql="'{}'::text[]",
                tools_param=None):
    # tools_sql 是**受控字面量**（探针自己的字符串，不来自外部输入）。
    # 走参数的路子用 tools_param（list），由 SQLAlchemy 转成 text[]。
    if tools_param is None:
        assign = tools_sql
        params = {}
    else:
        assign = ":tools"
        params = {"tools": tools_param}
    conn.execute(
        text(
            "INSERT INTO prompt_versions"
            " (id, template_id, system_template, input_schema, output_schema,"
            "  allowed_tools, change_reason, created_by)"
            " VALUES (:i, :t, :st, cast(:ins as jsonb), cast(:outs as jsonb),"
            "         " + assign + ", :cr, :u)"
        ),
        {
            "i": str(version_id), "t": str(template_id),
            "st": "请为 {{subject}} 写文案。",
            "ins": json.dumps({"properties": {"subject": {"type": "string"}}}),
            "outs": json.dumps({}),
            "cr": "探针变更原因", "u": str(uid), **params,
        },
    )


# ---------------------------------------------------------------------------
# 结构核对
# ---------------------------------------------------------------------------
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

    for name in PROMPT_TRIGGERS:
        n = conn.execute(
            text("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname = :n"),
            {"n": name},
        ).scalar_one()
        emit(n == 1, f"触发器存在 {name}", n)

    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'rednote_check_prompt%'")
    ).scalar_one()
    emit(n == len(PROMPT_FUNCTIONS), "一致性函数已建", f"{n} (期望 {len(PROMPT_FUNCTIONS)})")

    # CHECK 总数（本包 11 条，实测）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conrelid::regclass::text LIKE 'prompt_%' AND contype='c'"
        )
    ).scalar_one()
    emit(n == PROMPT_CHECK_TOTAL, "CHECK 约束总数", f"{n} (期望 {PROMPT_CHECK_TOTAL})")

    # UNIQUE 总数（2 条：template 的 kind+name、publication 的 template+sequence）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conrelid::regclass::text LIKE 'prompt_%' AND contype='u'"
        )
    ).scalar_one()
    emit(n == 2, "UNIQUE 约束总数", f"{n} (期望 2)")

    # FK 总数（本包 8 条：templates.active_version_id、versions.id/template_id/
    # created_by、publications.template_id/version_id/superseded_version_id/actor_id）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conrelid::regclass::text LIKE 'prompt_%' AND contype='f'"
        )
    ).scalar_one()
    emit(n == 8, "FK 约束总数", f"{n} (期望 8)")

    # allowed_tools 的列类型必须是 ARRAY（不是 jsonb）——
    # 这是"能落成 CHECK"的前提（jsonb 无法表达子集）。
    row = conn.execute(
        text(
            "SELECT data_type, udt_name FROM information_schema.columns"
            " WHERE table_schema='public' AND table_name='prompt_versions'"
            "   AND column_name='allowed_tools'"
        )
    ).one()
    emit(
        row.udt_name == "_text",
        "allowed_tools 列类型是 text[]（不是 jsonb）",
        f"{row.data_type} / {row.udt_name}",
    )

    # 延迟外键（prompt_versions.id → version_nodes.id）。
    deferred = conn.execute(
        text(
            "SELECT condeferrable AND condeferred FROM pg_constraint WHERE conname ="
            " 'fk_prompt_versions_id_version_nodes'"
        )
    ).scalar_one_or_none()
    emit(
        deferred is True,
        "外键是 DEFERRABLE INITIALLY DEFERRED fk_prompt_versions_id_version_nodes",
        deferred,
    )

    # 并发保护的真正落点：template+sequence 唯一约束必须存在。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conname = 'uq_prompt_publications_template_sequence'"
        )
    ).scalar_one()
    emit(n == 1, "并发保护 uq_prompt_publications_template_sequence 存在", n)

    # 跨包：共用不可变函数必须仍在（本包 downgrade 刻意不删它）。
    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname = 'rednote_reject_mutation'")
    ).scalar_one()
    emit(n == 1, "共用函数 rednote_reject_mutation 仍在", n)

    # 跨包：version_nodes 表必须仍在（本包只删挂在它上面的 1 个触发器）。
    n = conn.execute(
        text("SELECT count(*) FROM information_schema.tables"
             " WHERE table_schema='public' AND table_name='version_nodes'")
    ).scalar_one()
    emit(n == 1, "共用表 version_nodes 仍在", n)

    # 跨包：WP18 / WP20 的一致性函数必须存活（本包不碰别人的函数）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_proc"
            " WHERE proname LIKE 'rednote_check_brief%'"
            "    OR proname LIKE 'rednote_check_knowledge%'"
        )
    ).scalar_one()
    emit(n == CROSS_PACK_FUNCTION_TOTAL, "WP18+WP20 的一致性函数存活", f"{n} (期望 {CROSS_PACK_FUNCTION_TOTAL})")

    # 跨包 TASK_KINDS 对齐（本包实测过一次漂移，现在有机械判据）。
    # 这里查代码而不是数据库：取值域定义在 types.py，没有落成枚举类型。
    from rednote.modules.prompts.types import TASK_KINDS as WP23_KINDS
    from rednote.modules.research.types import TASK_KINDS as WP19_KINDS
    missing = set(WP19_KINDS) - set(WP23_KINDS)
    emit(
        not missing,
        "跨包 TASK_KINDS 对齐（WP19 ⊆ WP23）",
        f"WP19={len(WP19_KINDS)} WP23={len(WP23_KINDS)} 缺失={sorted(missing)}",
    )

    # 契约增量：本包端点必须在已导出的契约里（"写了端点但没进契约"是静默的）。
    spec = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    paths = spec["paths"]
    expected_paths = (
        "/api/v1/prompts",
        "/api/v1/prompts/{promptId}/versions",
        "/api/v1/prompts/{promptId}/publications",
        "/api/v1/prompts/{promptId}/tool-grants",
        "/api/v1/knowledge/proposals",
    )
    for p in expected_paths:
        emit(p in paths, f"契约含路径 {p}", sorted(paths.get(p, {}).keys()) or "缺失")

    # 契约里**不得**出现越权字段（请求体的属性集合里没有 allowedTools）。
    schemas = spec.get("components", {}).get("schemas", {})
    add_version = schemas.get("rednote__modules__prompts__api__AddVersionBody", {})
    props = set(add_version.get("properties", {}))
    emit(
        "allowedTools" not in props,
        "契约的 AddVersionBody 无 allowedTools",
        sorted(props),
    )

    # 契约里 4 个端点必须都声明了 422（项目规范：不依赖框架默认）。
    ok422 = True
    detail422 = []
    for p in expected_paths:
        for method, op in paths.get(p, {}).items():
            if method not in ("get", "post"):
                continue
            codes = set(op.get("responses", {}))
            detail422.append(f"{method.upper()} {p}:{'422' in codes}")
            if "422" not in codes:
                ok422 = False
    emit(ok422, "本包端点均声明 422", "; ".join(detail422))

# ---------------------------------------------------------------------------
# 活性验证 1：工具上界（超集被拦）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid = _seed_tx(conn)
        tid = _template_tx(conn)
        vid = uuid.uuid4()
        _node_tx(conn, vid, "PROMPT", tid, pid, uid)
        try:
            # 超集：ROOT_SHELL 不在平台上界内 -> 必须被 CHECK 拦下。
            _version_tx(conn, vid, tid, uid, tools_param=[OUT_OF_CEILING])
            emit(False, "工具上界：超集被拦", "竟然写进去了 —— 防线失效")
        except DBAPIError as exc:
            name = constraint_name(exc)
            emit(
                sqlstate(exc) == "23514" and (name or "").endswith("allowed_tools_within_ceiling"),
                "工具上界：超集被拦",
                f"sqlstate={sqlstate(exc)} constraint={name}",
            )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "工具上界：超集被拦", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 2：工具上界（合法子集 + 空集必须通过）—— 第 1 条的对照组
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid = _seed_tx(conn)
        tid = _template_tx(conn)
        # 2a：合法子集
        vid_ok = uuid.uuid4()
        _node_tx(conn, vid_ok, "PROMPT", tid, pid, uid, revision_no=2)
        _version_tx(conn, vid_ok, tid, uid, tools_param=list(CEILING_SAMPLE))
        emit(True, "工具上界：合法子集通过", f"{list(CEILING_SAMPLE)}")
        # 2b：空集（fail-closed 默认态）—— **同一模板下修订号必须不同**。
        vid_empty = uuid.uuid4()
        _node_tx(conn, vid_empty, "PROMPT", tid, pid, uid, revision_no=3)
        _version_tx(conn, vid_empty, tid, uid)
        emit(True, "工具上界：空集通过（默认态）", "{}")
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "工具上界：合法子集/空集通过", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 3：不可变表真的不可篡改（+ prompt_templates 可改的对照）
#
# ⚠️ 两步必须**各自独立事务**。PostgreSQL 里一条语句报错后整个事务进入
# aborted 状态，后续任何语句都直接返回 `25P02`（in_failed_sql_transaction）
# —— 本脚本第一次跑时把两步放在同一事务里，于是"对照"那步拿到的是
# `25P02` 而不是"能改"，被误判成"投影列被误锁"。
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid = _seed_tx(conn)
        tid = _template_tx(conn)
        vid = uuid.uuid4()
        _node_tx(conn, vid, "PROMPT", tid, pid, uid)
        _version_tx(conn, vid, tid, uid)
        try:
            conn.execute(
                text("UPDATE prompt_versions SET change_reason='篡改' WHERE id=:i"),
                {"i": str(vid)},
            )
            emit(False, "不可变：prompt_versions UPDATE 被拦", "竟然成功了")
        except DBAPIError as exc:
            emit(
                sqlstate(exc) == "23001",
                "不可变：prompt_versions UPDATE 被拦",
                f"sqlstate={sqlstate(exc)}",
            )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "不可变：prompt_versions UPDATE 被拦", f"探针异常 {type(exc).__name__}: {exc}")

# 对照（**独立事务**）：prompt_templates 必须可改（active_version_id 是投影列）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid = _seed_tx(conn)
        tid = _template_tx(conn)
        try:
            conn.execute(
                text("UPDATE prompt_templates SET name=:n WHERE id=:i"),
                {"n": "改名探针", "i": str(tid)},
            )
            emit(True, "对照：prompt_templates UPDATE 可改（投影列需要）", "通过")
        except DBAPIError as exc:
            emit(
                False,
                "对照：prompt_templates UPDATE 可改（投影列需要）",
                f"被误拦 sqlstate={sqlstate(exc)}",
            )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "对照：prompt_templates UPDATE 可改", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 4：OP-88 正向（entity_type 必须是 PROMPT）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid = _seed_tx(conn)
        tid = _template_tx(conn)
        vid = uuid.uuid4()
        # 刻意造一个 entity_type='PLAN' 的节点（不是 PROMPT）。
        _node_tx(conn, vid, "PLAN", tid, pid, uid)
        _version_tx(conn, vid, tid, uid)
        emit(True, "OP-88 正向：INSERT 阶段静默通过（延迟判据）", "未在 INSERT 时报错")
        try:
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            emit(False, "OP-88 正向：强制校验被拦", "竟然通过了")
        except DBAPIError as exc:
            emit(
                sqlstate(exc) == "23514",
                "OP-88 正向：强制校验被拦",
                f"sqlstate={sqlstate(exc)}",
            )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "OP-88 正向", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 5：OP-88 反向（PROMPT 节点必须有配对版本行）+ 收窄放行对照
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid = _seed_tx(conn)
        tid = _template_tx(conn)
        orphan = uuid.uuid4()
        # 5a：template 真实存在 + PROMPT 节点 + **不插** prompt_versions 行。
        _node_tx(conn, orphan, "PROMPT", tid, pid, uid)
        try:
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            emit(False, "OP-88 反向：孤立节点被拦", "竟然通过了")
        except DBAPIError as exc:
            emit(
                sqlstate(exc) == "23514",
                "OP-88 反向：孤立节点被拦",
                f"sqlstate={sqlstate(exc)}",
            )
    # 5b：收窄放行的对照 —— entity_id 指向**不存在**的模板时不该被拦。
    # 注意：5a 的强制校验会把事务打坏，因此换新连接重来。
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid = _seed_tx(conn)
        ghost_node = uuid.uuid4()
        _node_tx(conn, ghost_node, "PROMPT", uuid.uuid4(), pid, uid)
        try:
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            emit(
                True,
                "OP-88 反向的收窄：模板不存在时不拦（对照）",
                "通过（符合只对真实模板强制的设计）",
            )
        except DBAPIError as exc:
            emit(
                False,
                "OP-88 反向的收窄：模板不存在时不拦（对照）",
                f"被误拦 sqlstate={sqlstate(exc)}",
            )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "OP-88 反向", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 6：合法配对必须能通过（第 4、5 条的对照）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid = _seed_tx(conn)
        tid = _template_tx(conn)
        vid = uuid.uuid4()
        _node_tx(conn, vid, "PROMPT", tid, pid, uid)
        _version_tx(conn, vid, tid, uid)
        try:
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            emit(True, "合法配对通过（对照）", "PROMPT 节点 + 版本行 + entity_id 一致")
        except DBAPIError as exc:
            emit(
                False,
                "合法配对通过（对照）",
                f"被误拦 sqlstate={sqlstate(exc)} —— 判据把一切都拒绝了",
            )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "合法配对通过", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 7：激活事件的版本归属受管
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid = _seed_tx(conn)
        tid_a = _template_tx(conn, name="WP23 探针模板 A")
        tid_b = _template_tx(conn, name="WP23 探针模板 B")
        vid_a = uuid.uuid4()
        _node_tx(conn, vid_a, "PROMPT", tid_a, pid, uid)
        _version_tx(conn, vid_a, tid_a, uid)
        # 声明 template_id = B 但 version_id 属于 A -> 必须被拦。
        conn.execute(
            text(
                "INSERT INTO prompt_publications"
                " (template_id, version_id, action, sequence, affected_task_kinds)"
                " VALUES (:t, :v, 'ACTIVATE', 1, ARRAY['CONTENT_COPY']::text[])"
            ),
            {"t": str(tid_b), "v": str(vid_a)},
        )
        try:
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            emit(False, "激活事件范围：跨模板引用被拦", "竟然通过了")
        except DBAPIError as exc:
            emit(
                sqlstate(exc) == "23514",
                "激活事件范围：跨模板引用被拦",
                f"sqlstate={sqlstate(exc)}",
            )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "激活事件范围", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 8：被取代的版本的归属受管（同一触发器里的第二条判据）
#
# `publication_scope_ddl` 里有两条判据、共用一个函数：`version_id` 与
# `superseded_version_id` 都必须属于本模板。前者由验证 7 覆盖，
# 后者（"这次激活取代了谁"）**是审计链的一部分**，必须单独证明它受管。
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid = _seed_tx(conn)
        tid_a = _template_tx(conn, name="WP23 探针模板 A2")
        tid_b = _template_tx(conn, name="WP23 探针模板 B2")
        vid_a = uuid.uuid4()
        _node_tx(conn, vid_a, "PROMPT", tid_a, pid, uid, revision_no=2)
        _version_tx(conn, vid_a, tid_a, uid)
        vid_b = uuid.uuid4()
        _node_tx(conn, vid_b, "PROMPT", tid_b, pid, uid, revision_no=2)
        _version_tx(conn, vid_b, tid_b, uid)
        # version_id 属于 B（合法），但 superseded_version_id 指向 A 的版本（越界）。
        conn.execute(
            text(
                "INSERT INTO prompt_publications"
                " (template_id, version_id, action, sequence,"
                "  superseded_version_id, affected_task_kinds)"
                " VALUES (:t, :v, 'ACTIVATE', 1, :s, ARRAY['CONTENT_COPY']::text[])"
            ),
            {"t": str(tid_b), "v": str(vid_b), "s": str(vid_a)},
        )
        try:
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            emit(False, "被取代版本的归属：跨模板 superseded 被拦", "竟然通过了")
        except DBAPIError as exc:
            emit(
                sqlstate(exc) == "23514",
                "被取代版本的归属：跨模板 superseded 被拦",
                f"sqlstate={sqlstate(exc)}",
            )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "被取代版本的归属", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 9：生效版本的归属受管（投影列不能被裸 SQL 改坏）
#
# `active_version_id` 是投影列，它**可以被裸 SQL 就地改写**（与
# `prompt_versions` 不同，那张表有不可变触发器）。因此需要一条独立的
# AFTER UPDATE 校验来保证"这个模板的当前版本是本模板的版本"。
# 若不管，投影会算出"模板 T 的当前版本是 U 的版本" ——
# 于是下游（WP26）拿去生成时会用**另一个任务类型**的系统指令。
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid = _seed_tx(conn)
        tid_a = _template_tx(conn, name="WP23 探针模板 A3")
        tid_b = _template_tx(conn, name="WP23 探针模板 B3")
        vid_a = uuid.uuid4()
        _node_tx(conn, vid_a, "PROMPT", tid_a, pid, uid, revision_no=2)
        _version_tx(conn, vid_a, tid_a, uid)
        try:
            conn.execute(
                text(
                    "UPDATE prompt_templates SET active_version_id = :v WHERE id = :t"
                ),
                {"v": str(vid_a), "t": str(tid_b)},
            )
            # 触发器是**延迟**的，必须强制校验才拿得到结论。
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            emit(False, "生效版本归属：指向别人的版本被拦", "竟然通过了")
        except DBAPIError as exc:
            emit(
                sqlstate(exc) == "23514",
                "生效版本归属：指向别人的版本被拦",
                f"sqlstate={sqlstate(exc)}",
            )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "生效版本归属", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 零残留核对
# ---------------------------------------------------------------------------
with engine.connect() as conn:
    for table in OWN_TABLES:
        n = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        emit(n == 0, f"零残留 {table}", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM prompt_templates WHERE name LIKE 'WP23 探针%'")
    ).scalar_one()
    emit(n == 0, "零残留 探针模板", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM users WHERE issuer = 'wp23-probe'")
    ).scalar_one()
    emit(n == 0, "零残留 探针用户", f"{n} 行")

print(f"RESULT_EMITTED (期望 {EXPECTED_CHECKS} 条)")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def collect_wp23() -> tuple[Path, bool]:
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
        "# WP23 Prompt 版本与工具上界：3 张表 / 13 CHECK / 6 触发器 / 七处活性验证 / 契约",
        f"# 生成时间: {_now()}",
        "# 目标库: rednote_verify_wp23（收口时由迁移从 bootstrap 建出，非测试夹具建表）",
        "# 命令: cd services/backend && python -c '<information_schema + 延迟约束试写 + 试插>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp23-prompt.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # **条数不足也算失败**（ISS-43：崩溃伪装成"全过"）。
    complete = len(checks) >= EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete)


def main() -> int:
    path, ok = collect_wp23()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())