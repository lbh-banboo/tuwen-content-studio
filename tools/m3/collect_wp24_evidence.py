#!/usr/bin/env python
"""WP24 证据采集脚本。

产出的文件（落在 `evidence/m3/`）：

| 文件 | 内容 |
|---|---|
| `wp24-scene-draft.log` | 2 张表结构 / 索引 / 约束 / 1 个触发器 / 迁移版本，+ **20 处库内活性验证 + 3 条纯函数判据** |

## 本包与前几个包的**结构性不同**：这里没有跨表一致性触发器

WP18 / WP20 / WP22 / WP23 的活性验证主体是"注册表行与领域表行必须配对"
（OP-88）。**WP24 完全没有这一类不变量** —— `ddl.consistency_items()` 返回
**空列表**，且那是有意的显式形状（`ddl.py` 的 docstring 第三节）。

因此本包的验证重量全部压在两处：

1. **部分唯一索引的双向活性验证**（本包最容易"看起来做了、其实没做"的地方）；
2. **不可变表与可变工作区的**"该拦的拦 / 该过的过"**对照**。

## 20 处库内活性验证 + 3 条纯函数判据

结构核对只能证明约束**存在**，不能证明它**生效**。逐条列在下面，
每条都写了"为什么只做正向会漏掉什么"。末尾另附纯函数层的 3 条
（它们落不到数据库层，见第六节）。

### 一、部分唯一索引（第 1~4 条）—— 本包的核心防线

`uq_runtime_settings_global` / `uq_runtime_settings_project` 两条**部分**唯一
索引是"当前生效值"这个概念的**结构基础**（`models.py` docstring 第四节）。
它们的 `WHERE` 写错会让索引**静默退化**为不生效 —— 不报错，只是不再约束。

1. **GLOBAL 正向**：`(key, 'GLOBAL', NULL, rev)` 重复插入 → 断言被拒（`23505`，
   约束名 `uq_runtime_settings_global`）。
2. **GLOBAL 反向（★ 证明它确实按 `WHERE` 收窄）**：同样 `(key, rev)` 的两行、
   但 `scope_kind='PROJECT'` 且**不同项目** → 断言**两行都能插进去**。
   若这条被拒，说明索引丢掉了 `WHERE` 退化成"全局唯一"—— 而那会
   让**不同项目配同一个键**互相打架（OP-60 的项目级副本直接失效）。
   ⚠️ 只做第 1 条会漏掉"`WHERE` 写成了永远命不中的条件"这一整类失效。
3. **PROJECT 正向**：`(key, project, rev)` 重复 → 断言被拒（`23505`，
   `uq_runtime_settings_project`）。
4. **PROJECT 反向（★ 证明键含 `project_id`）**：`(key, rev)` 相同但**项目不同**
   → 断言**都能插入**。这条与第 2 条**不同**：第 2 条证明的是
   "GLOBAL 索引不越界管 PROJECT 行"，本条证明的是
   "PROJECT 索引按项目分区"—— 少了 `project_id` 这个键列，同一 `(key, rev)`
   在相邻项目之间就会互相顶掉。

### 二、不可变保护（第 5~8 条）—— 必须有"该过的过"的对照

5. `runtime_settings` 的 `UPDATE` 被拦（`23001`）。
6. `runtime_settings` 的 `DELETE` 被拦（`23001`）—— `UPDATE` 与 `DELETE`
   是触发器里的两个分支，**必须分别证明**（只测 `UPDATE` 会漏掉
   "`DELETE` 分支写成了 `INSERT`"这种形态）。
7. **对照 A（★）**：`plan_drafts` 的 `UPDATE` **能通过**。
   本包刻意**不给** `plan_drafts` 挂不可变触发器（它是工作区，运营反复调整）。
   若这条被拦，说明有人把触发器挂错了表 —— 而"两张表都锁上"的实现
   也能让第 5、6 条全绿。
8. **对照 B（★）**：`plan_drafts` 的 `DELETE` **能通过**。
   理由同第 7 条（`plan_drafts` 可删 —— 草稿被放弃是正常业务）。

### 三、`runtime_settings` 的五条形状约束（第 9~14 条）

**每一条都配一个"合法值必须通过"的对照**，否则"见谁都拒"的实现也能全绿。

9.  `scope_matches_project`：`GLOBAL` 行带 `project_id` → 被拒（`23514`）。
10. `scope_matches_project` 的反面：`PROJECT` 行**不带** `project_id` → 被拒。
    这条与第 9 条是**同一条 CHECK 的两个分支**，只测一边会漏掉另一边
    （而"漏掉的那一边"恰是"某个没有项目的项目级配置"这个静默缺陷）。
11. `origin_matches_scope`：`GLOBAL` 行标 `origin='MANUAL'` → 被拒。
    防的是级联刷新读 `origin` 得出错误结论。
12. `key_valid`：`key='batch.limt'`（拼错）→ 被拒。
    这是 R8"万能杂货表"的处置 —— 拼错的键会**静默**成为新配置项。
13. `value_is_object`：`value='[]'::jsonb`（数组）→ 被拒。
    标量/数组会让 `value['TRIAL']` 抛 `TypeError` 而不是可读错误。
14. `revision_no_positive`：`revision_no=0` → 被拒。

### 四、`plan_drafts` 的四条形状约束（第 15~18 条）

15. `total_quantity_within_bound`：`100001`（上界 +1）→ 被拒。
16. `total_quantity_positive`：`0` → 被拒。
17. **下界的对照**：`total_quantity IS NULL` → **能通过**（"还没定总量"是
    合法中间态，原型的交互顺序正是"先配比例、后定总量"）。
18. `state_valid`：`state='DRAFT'` → 被拒。

### 五、合法数据必须能写完（第 19~20 条）—— 全局对照

19. 一整套**合法**的 `runtime_settings` 行（GLOBAL + PROJECT 各一条）→ 都能插入。
20. 一整套**合法**的 `plan_drafts` 行（含 `NULL` 总量、`ENABLED` 状态）→ 能插入。

> 第 19、20 条是**整体**对照：前 18 条全是"断言被拒"，若约束写得过严
> （例如 `value_is_object` 写成了 `value IS NOT NULL AND ...` 之类），
> 只有这两条会红。

### 六、纯函数层（第 21~23 条）—— 落不到 CHECK 的那两件事

比例放在 JSONB 里，**落不了 CHECK**，所以"停用项不参与分配"与
"容量满时余数合法"这两条只能由纯函数层的判据守。它们各自对应一处已修/已决的项：

21. **停用项归零**（MAJOR-5 现场）：入口必须是生产侧的
    `service._to_allocation_items()`（JSONB 行 → 分配输入），
    断言停用行的 `weight_permille == permille.DISABLED_PERMILLE`。
    ⚠️ **不能**手工造一个 `weight_permille=0` 的 item 去断言"它分到 0"——
    权重 0 必然分 0，那是**恒真式**（判据纪律第 2、7 条），
    抓不到"归零这一步被漏做"。真实风险只在"存储形态 → 分配输入"的转换里。
22. **第 21 条的反向（★ 证明它能报红）**：把归零去掉（直接用
    `weightPermille` 构造 `AllocationItem`），停用项必须**拿到分配额**。
    不做这一步的话，"归零"与"未归零"两种实现都会让第 21 条通过。
23. **容量削峰后余数合法**（C14）：总 100 条、两个条目各容量 10 →
    断言 `granted == {'A': 10, 'B': 10}` 且 `unallocated == 80`，**不抛异常**。
    这条对应阶段 7 删掉的 `REASON_ALLOCATION_OVER_CAPACITY` ——
    那个错误码的语义（"超出容量是错误"）与 C14 的设计相反。

## 探针自身踩过的坑（写在这里，避免重复踩）

本节是"证据脚本自己出错"的记录 —— 与 WP18 的 ISS-63、WP23 的四处坑同源
（探针出错会**伪装成实现有缺陷**，比不写探针更危险）。

1. **内联脚本里不能有双引号嵌套**（`"……他说："好"……"`）。症状是
   `SyntaxError: invalid syntax`，而 `RESULT` 行显示 `0 PASS / 0 FAIL`
   —— 崩溃伪装成"没东西可查"（ISS-43 同型）。
2. **期望值必须实测**，不能凭印象。本脚本第一次跑就错了 2 处：
   `runtime_settings` 是 **11 列**不是 12（没有 `updated_at`，追加式表不需要）、
   `plan_drafts` 是 **13 列**不是 12（`lock_version` 是独立一列）。
3. **一条语句报错会把整个事务打成 aborted**（后续语句返回 `25P02`）。
   因此"被拦"与"对照通过"这两类探针**必须各自独立事务与独立连接** ——
   放同一个事务里会让对照拿到 `25P02` 而不是"能写"，误判成实现锁过头。
4. **`runtime_settings` 是不可变表**：探针**绝不能 commit**，否则
   那些行**永远删不掉**（触发器拦 `DELETE`）。因此每条探针
   独立连接 + 无条件 `rollback()` ⇒ 零残留。
5. **`batch.limits` 的 `GLOBAL` 第 1 版已被迁移占用**（`bootstrap_defaults`）。
   探针若用同一个 key + 同一个 `revision_no` 会**撞上真值的唯一索引**，
   于是"被拒"的结论**原因错了**（是撞了迁移的行，不是撞了探针自己造的重复行）。
   因此探针一律用 `research.maxEffectiveFiles`。
6. **`plan_drafts` 一项目只能有一条**（`uq_plan_drafts_project_id`）。
   想造"两条草稿"的探针会拿到 `23505` 而**不是**它想验的那条约束 ——
   因此所有草稿探针改成"同一事务里改同一条行的不同字段"。
7. **★ 纯函数探针必须对着真实 API 写，不能凭印象**（本轮实测）：
   初稿把 `allocate_by_weight` 记成 `allocate`、把 `AllocationResult.granted`
   记成 `allocated_by_key()` 之外的字段、把 `ITEM_STATE_DISABLED` 记成 `"DISABLED"`
   裸串 —— 四处全错，症状是**一条 `ImportError` 打包了全部 3 条判据**：
   看起来"只挂了一条"，实际是三条判据**一条都没跑**。
   ⇒ 纯函数探针的 `except` 必须把异常类型与消息原样写进 RESULT 行（本脚本已做），
   否则 `ImportError` 与"函数返回了错值"在日志里长得一样。

## 跨包回归项（本包最容易误伤别人的地方）

- 共用的不可变触发器函数 `rednote_reject_mutation()` 必须仍在
  （本包 `downgrade` **刻意不删它** —— 所有不可变表共用）；
- `version_nodes` / `production_baselines` 表**不得**被本包 `downgrade` 删除
  （本包只删自己那 2 张）；
- WP18 / WP20 / WP22 / WP23 的 `rednote_check_*` 函数必须存活
  （本包不碰别人的函数，`consistency_items()` 是空集）；
- `migrations/env.py` 必须 import 本包 models（G3 铁律：不 import 会被
  autogenerate 当成多余表提议 `DROP`）。

## 探针数据的处置

每条验证跑在**独立连接**里，最后**无条件 `rollback()`** → **零残留**。
理由见上文坑 4：`runtime_settings` 不可变（触发器拦 `DELETE`），
一旦提交就删不掉（WP18 的 ISS-63 已实测）。

**本脚本用在哪个库**：默认 `rednote_verify_wp24`（收口时新建的干净库，
迁移从 bootstrap 一路跑到 head 建出全部结构）。用它的理由：
分支测试库里的表是**测试夹具 `create_all` 建的**，不是迁移建的 ——
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
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"

# 期望条数（与内联脚本里的 EXPECTED_CHECKS 保持同值）。
# 两处都要：内联脚本用它写进 RESULT 行，外层用它判定成功与否（ISS-43）。
EXPECTED_CHECKS: int = 60

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
EXPECTED_CHECKS = 60

DB = "postgresql+psycopg://rednote@localhost:5432/rednote_verify_wp24"
engine = create_engine(DB)

CONTRACT_PATH = Path(
    "/Users/<user>/内容运营AI Native/rednote-production/contracts/openapi.json"
)

# 本包引入的迁移版本。判据用"不早于"而不是"恰好等于"（ISS-44：
# 硬编码"恰好停在某个版本"会让每个新工作包把历史证据集体打红）。
REVISION_INTRODUCED = "022ba9804d66"

# 本包 2 张表的期望列数（按 information_schema **实测**填写，不凭印象）。
#   · runtime_settings 是 11 列（**没有 updated_at** —— 追加式不可变表不更新）；
#   · plan_drafts 是 13 列（lock_version 是独立一列，不是与 updated_at 合并的）。
TABLE_COLUMNS = (
    ("runtime_settings", 11),
    ("plan_drafts", 13),
)

# 本包 1 个触发器（只有不可变保护；plan_drafts 刻意不挂）。
IMMUTABLE_TRIGGERS = ("trg_runtime_settings_immutable",)

# 本包的 CHECK 数（实测：runtime_settings 8 条 + plan_drafts 7 条）。
CHECK_TOTAL = {"runtime_settings": 8, "plan_drafts": 7}

# 本包的 FK 数（实测：runtime_settings 2 = project_id/actor_id；
# plan_drafts 3 = project_id/baseline_id/created_by）。
FK_TOTAL = {"runtime_settings": 2, "plan_drafts": 3}

# 本包的两条部分唯一索引（名字 + 必须出现在 indexdef 里的 WHERE 片段）。
# ⚠️ 这两条**不在 `pg_constraint` 里** —— 只查约束表完全看不到它们
# （WP22 已登记此坑；`ddl.py` docstring 第三节）。
PARTIAL_INDEXES = (
    ("uq_runtime_settings_global", "scope_kind", "GLOBAL"),
    ("uq_runtime_settings_project", "scope_kind", "PROJECT"),
)

# 本包的普通索引（4 + 1 = 5 条，**不是**约束，只核存在性）。
PLAIN_INDEXES = (
    "ix_runtime_settings_lookup",
    "ix_runtime_settings_origin",
    "ix_runtime_settings_project_id",
    "ix_runtime_settings_key_created",
    "ix_plan_drafts_updated_at",
)

# 契约里本包的 7 个路径 / 8 个操作。
OWN_PATHS = (
    "/api/v1/projects/{projectId}/plan-draft",
    "/api/v1/projects/{projectId}/plan-draft/submissions",
    "/api/v1/settings/global",
    "/api/v1/settings/global/{key}",
    "/api/v1/settings/global/{key}/history",
    "/api/v1/settings/projects/diverged",
    "/api/v1/settings/projects/{projectId}/{key}",
)

# 探针用的配置键。**刻意不用 `batch.limits`** —— 它的 GLOBAL 第 1 版已被
# 迁移的 `bootstrap_defaults` 占用，用它会让"被拒"的原因变成
# "撞了迁移的行"而不是"撞了探针自己造的重复行"（坑 5）。
PROBE_KEY = "research.maxEffectiveFiles"
PROBE_VALUE = {"maxEffectiveFiles": 2000}

# 本包 2 张表（零残留判据用）。
OWN_TABLES = ("runtime_settings", "plan_drafts")


def _revision_is_at_least(current, introduced):
    # 当前 head 是否**不早于** introduced —— 沿真实迁移链判断，不是猜。
    #
    # **为什么不能写 `== EXPECTED_REVISION`**（ISS-44）：那会让每个新工作包
    # 把前面所有包的历史证据集体打红，因为校验的其实是"迁移停在某个特定
    # 版本"，而真正该校验的是"**该包引入的表/约束还在**"（由本脚本后面的
    # 核对项保证）。
    #
    # **为什么也不能用 `len(current) >= len(introduced)`**（ISS-115，2026-09-27
    # 收口轮实测）：本项目全部 revision id **都是 12 位十六进制**，
    # 于是"长度不短于"在 `current != introduced` 时**恒为 True** ——
    # 那是把等式判据换成了**恒绿判据**，比原缺陷更隐蔽：它永不失败，
    # 因此也永不报警（判据纪律第 7/9 条：判据必须能区分两种实现）。
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
# 探针的公共前置：造项目 + 用户（可变表，但仍然整体 rollback）
# ---------------------------------------------------------------------------

def _seed_tx(conn):
    # 造最小可用的 brand / product / user / project。
    # ⚠️ 列名与非空集合全部按 information_schema 实测（本项目已多次踩中：
    # WP18 一次 5 处、WP20 先后踩了 4 处列名 / 5 处非空）。
    #   · users    : 没有 email，是 (issuer, subject) 唯一
    #   · products : 有 model_code
    #   · projects : 没有 code/created_by；brand_id / product_id / owner_id /
    #                target_delivery_count / due_date / timezone 都是 NOT NULL
    #                （external_policy 有默认值，可不写）
    uid = uuid.uuid4()
    brand = uuid.uuid4()
    prod = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, display_name, role, status)"
            " VALUES (:u, :iss, :sub, :n, 'ADMIN', 'ACTIVE')"
        ),
        {"u": str(uid), "iss": "wp24-probe", "sub": f"probe-{uid}", "n": "WP24 探针"},
    )
    conn.execute(
        text("INSERT INTO brands (id, name, status) VALUES (:b, :n, 'ACTIVE')"),
        {"b": str(brand), "n": f"WP24 探针品牌 {str(brand)[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO products (id, brand_id, name, model_code, status)"
            " VALUES (:p, :b, :n, :c, 'ACTIVE')"
        ),
        {
            "p": str(prod), "b": str(brand),
            "n": f"WP24 探针产品 {str(prod)[:8]}", "c": f"WP24-{str(prod)[:8]}",
        },
    )
    pid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO projects"
            " (id, project_code, name, brand_id, product_id, owner_id, phase,"
            "  target_delivery_count, due_date, timezone)"
            " VALUES (:p, :c, :n, :b, :pr, :u, 'PREPARATION', 1,"
            "         (now() + interval '30 days')::date, 'Asia/Shanghai')"
        ),
        {
            "p": str(pid), "c": f"WP24PROBE{str(pid)[:8]}", "n": "WP24 探针项目",
            "b": str(brand), "pr": str(prod), "u": str(uid),
        },
    )
    return pid, uid


def _setting_tx(
    conn, key, scope_kind, origin, revision_no, *,
    project_id=None, value=None, reason="探针",
):
    # 插一行 runtime_settings。形状约束由调用方负责，探针要能造出违规行。
    import json as _json

    conn.execute(
        text(
            "INSERT INTO runtime_settings"
            " (id, key, scope_kind, project_id, value, revision_no, origin, reason)"
            " VALUES (:i, :k, :s, :p, cast(:v as jsonb), :r, :o, :why)"
        ),
        {
            "i": str(uuid.uuid4()), "k": key, "s": scope_kind,
            "p": None if project_id is None else str(project_id),
            "v": _json.dumps(value if value is not None else PROBE_VALUE),
            "r": revision_no, "o": origin, "why": reason,
        },
    )


def _draft_tx(conn, project_id, *, total_quantity=None, state="EDITING"):
    # 插一行 plan_drafts（一项目一份 —— 唯一约束 uq_plan_drafts_project_id）。
    did = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO plan_drafts"
            " (id, project_id, state, total_quantity, scene_items, form_items,"
            "  pairings, schema_version)"
            " VALUES (:i, :p, :s, :q, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, 1)"
        ),
        {
            "i": str(did), "p": str(project_id), "s": state,
            "q": total_quantity,
        },
    )
    return did


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

    for name in IMMUTABLE_TRIGGERS:
        n = conn.execute(
            text("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname = :n"),
            {"n": name},
        ).scalar_one()
        emit(n == 1, f"触发器存在 {name}", n)

    # 反向：`plan_drafts` 上**不得**有任何用户触发器（刻意不挂，见 ddl.py 第一节）。
    # 只核"runtime_settings 有触发器"会漏掉"两张表都挂上了"这种实现。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_trigger t"
            " JOIN pg_class c ON c.oid = t.tgrelid"
            " WHERE NOT t.tgisinternal AND c.relname = 'plan_drafts'"
        )
    ).scalar_one()
    emit(n == 0, "plan_drafts 上无触发器（可变工作区，刻意不挂）", f"{n} 个")

    for table, expected in CHECK_TOTAL.items():
        n = conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint"
                " WHERE conrelid = cast(:t as regclass) AND contype='c'"
            ),
            {"t": table},
        ).scalar_one()
        emit(n == expected, f"{table} CHECK 总数", f"{n} (期望 {expected})")

    for table, expected in FK_TOTAL.items():
        n = conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint"
                " WHERE conrelid = cast(:t as regclass) AND contype='f'"
            ),
            {"t": table},
        ).scalar_one()
        emit(n == expected, f"{table} FK 总数", f"{n} (期望 {expected})")

    # `plan_drafts` 的一项目一份唯一约束（与 quota_accounts 同语义）。
    n = conn.execute(
        text("SELECT count(*) FROM pg_constraint WHERE conname = 'uq_plan_drafts_project_id'")
    ).scalar_one()
    emit(n == 1, "唯一约束 uq_plan_drafts_project_id 存在（一项目一份）", n)

    # ★ 两条部分唯一索引：必须查 `pg_indexes.indexdef`（不在 pg_constraint 里）。
    for name, col, value in PARTIAL_INDEXES:
        row = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes"
                " WHERE schemaname='public' AND indexname = :n"
            ),
            {"n": name},
        ).scalar_one_or_none()
        if row is None:
            emit(False, f"部分唯一索引存在 {name}", "未找到")
            continue
        # 判据不是"名字在不在"，而是"**WHERE 子句在不在、且指向正确的取值**"——
        # 名字相同、WHERE 不同（或没有 WHERE）是最可能的漂移形态。
        ok = "UNIQUE INDEX" in row and " WHERE " in row and col in row and value in row
        emit(ok, f"部分唯一索引 {name} 含正确的 WHERE", row)

    # 反向：这两条索引**不应**出现在 pg_constraint（若出现说明写成了普通约束，
    # 那样 `project_id IS NULL` 会让全局行完全失效 —— models.py 第四节）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conname IN ('uq_runtime_settings_global','uq_runtime_settings_project')"
        )
    ).scalar_one()
    emit(n == 0, "两条部分唯一索引**不在** pg_constraint 里（部分索引的固有性质）", n)

    for name in PLAIN_INDEXES:
        n = conn.execute(
            text("SELECT count(*) FROM pg_indexes WHERE schemaname='public' AND indexname = :n"),
            {"n": name},
        ).scalar_one()
        emit(n == 1, f"普通索引存在 {name}", n)

    # 迁移的数据回填：ADR-13 初始 GLOBAL 值必须由 `bootstrap_defaults` 写入
    # （而不是迁移手写的裸 SQL —— 那样值就有两个来源）。
    row = conn.execute(
        text(
            "SELECT value, revision_no, origin FROM runtime_settings"
            " WHERE key='batch.limits' AND scope_kind='GLOBAL'"
            " ORDER BY revision_no DESC LIMIT 1"
        )
    ).one_or_none()
    if row is None:
        emit(False, "迁移回填了 batch.limits 的 GLOBAL 初始值", "未找到")
    else:
        from rednote.modules.planning.types import DEFAULT_BATCH_MAX
        emit(
            dict(row[0]) == dict(DEFAULT_BATCH_MAX) and row[1] == 1 and row[2] == "GLOBAL",
            "迁移回填了 batch.limits 的 GLOBAL 初始值（与 types.DEFAULT_BATCH_MAX 同源）",
            f"value={row[0]} rev={row[1]} origin={row[2]}",
        )

    # ★ ddl.py 的规格与 models.py 的实现逐字段对账（三处副本的唯一机械判据）。
    # 只比对名字是不够的：名字相同、WHERE 不同是最会漂移的形态。
    from rednote.modules.planning import ddl as planning_ddl

    drift = planning_ddl.index_spec_drift()
    emit(
        not drift,
        "部分唯一索引规格与 models.py 实现逐字段一致（键列含顺序 + WHERE）",
        drift or "无漂移",
    )

    # 本包的触发器名集合必须与 ddl 的声明一致（不数总个数 —— 别的包也可能
    # 往 runtime_settings 挂触发器，数个数会把别人的算进来）。
    declared = set(planning_ddl.trigger_names())
    actual_trg = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid"
                " WHERE NOT t.tgisinternal AND c.relname='runtime_settings'"
            )
        ).fetchall()
    }
    emit(
        declared == actual_trg,
        "runtime_settings 的触发器集合与 ddl.trigger_names() 一致",
        f"声明={sorted(declared)} 实际={sorted(actual_trg)}",
    )

    # 跨包：共用不可变函数必须仍在（本包 downgrade 刻意不删它）。
    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname = 'rednote_reject_mutation'")
    ).scalar_one()
    emit(n == 1, "共用函数 rednote_reject_mutation 仍在", n)

    # 跨包：version_nodes / production_baselines 必须仍在（本包不碰它们）。
    # 判据用 `>=` 不用 `==`（判据纪律第 1 条）：这两张表是**别包的**，
    # 本脚本只该断言"本包没把它们带走"，不该断言"全项目恰好有 2 张" ——
    # 后者会让任何一个新工作包（乃至后续 M4 包）把这条历史证据打红。
    n = conn.execute(
        text(
            "SELECT count(*) FROM information_schema.tables"
            " WHERE table_schema='public'"
            "   AND table_name IN ('version_nodes','production_baselines')"
        )
    ).scalar_one()
    emit(n >= 2, "跨包表 version_nodes / production_baselines 仍在", f"{n} (下界 2)")

    # 跨包：WP18 / WP20 / WP22 / WP23 的一致性函数必须存活。
    # 同样用下界：`rednote_check_*` 的**总数会随后续包增大**，
    # 写死 18 会在 WP25 引入新函数时误报（且症状是"历史证据集体变红"，
    # 很难定位到是本条判据的锅）。
    # 另按包分列计数，这样"某个包整批消失"依然能被看见（合计数会被别包掩盖）。
    # 下界 = **收口时实测值**（2026-09-29：brief 4 / knowledge 4 / baseline 6 /
    # prompt 4，合计 18）。用实测值而不是估值：估值写低了等于没判据，
    # 写高了会当场误报。
    for _pkg, _lo in (
        ("brief", 4),
        ("knowledge", 4),
        ("baseline", 6),
        ("prompt", 4),
    ):
        n = conn.execute(
            text(
                "SELECT count(*) FROM pg_proc"
                " WHERE proname LIKE 'rednote_check_' || :p || '%'"
            ),
            {"p": _pkg},
        ).scalar_one()
        emit(n >= _lo, f"跨包一致性函数 rednote_check_{_pkg}* 存活",
             f"{n} 个 (下界 {_lo})")

    # 契约增量：本包 7 个路径都在（"写了端点但没进契约"是静默的）。
    spec = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    paths = spec["paths"]
    missing = [p for p in OWN_PATHS if p not in paths]
    emit(not missing, "契约含本包全部 7 个路径", missing or "都在")

    n_ops = sum(
        1
        for p in OWN_PATHS
        for m in paths.get(p, {})
        if m in ("get", "post", "put", "patch", "delete")
    )
    emit(n_ops == 8, "契约含本包 8 个操作", f"{n_ops} (期望 8)")

    total_ops = sum(
        1
        for v in paths.values()
        for m in v
        if m in ("get", "post", "put", "patch", "delete")
    )
    emit(
        len(paths) >= 55 and total_ops >= 68,
        "契约总数不早于本包（路径 >= 55 / 操作 >= 68）",
        f"{len(paths)} paths / {total_ops} operations",
    )

    # 本包端点必须都声明了 422（项目规范：不依赖 FastAPI 自动注入的
    # `HTTPValidationError`，它的形状与协议 §1.2 不同）。
    ok422 = True
    detail422 = []
    for p in OWN_PATHS:
        for method, op in paths.get(p, {}).items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            has = "422" in set(op.get("responses", {}))
            detail422.append(f"{method.upper()} {p.split('/')[-1]}:{'422' in set(op.get('responses', {}))}")
            if not has:
                ok422 = False
    emit(ok422, "本包 8 个端点均声明 422", "; ".join(detail422))

# ---------------------------------------------------------------------------
# 活性验证 1：部分唯一索引（GLOBAL）正向 —— 匹配 WHERE 的重复行被拒
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        _seed_tx(conn)
        _setting_tx(conn, PROBE_KEY, "GLOBAL", "GLOBAL", 1)
        try:
            _setting_tx(conn, PROBE_KEY, "GLOBAL", "GLOBAL", 1)
            emit(False, "部分唯一 GLOBAL 正向：重复 (key, rev) 被拒", "竟然写进去了 —— 防线失效")
        except DBAPIError as exc:
            name = constraint_name(exc)
            emit(
                sqlstate(exc) == "23505" and name == "uq_runtime_settings_global",
                "部分唯一 GLOBAL 正向：重复 (key, rev) 被拒",
                f"sqlstate={sqlstate(exc)} constraint={name}",
            )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "部分唯一 GLOBAL 正向", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 2：★ 部分唯一索引（GLOBAL）反向 —— 不匹配 WHERE 的重复行必须能插入
#
# 这是本包最重要的一条：它证明 `WHERE scope_kind = 'GLOBAL'` **确实在过滤**。
# 若索引丢掉了 WHERE（退化成 (key, revision_no) 全局唯一），下面第二行
# 会被拒 —— 而"不同项目各配同一个键"是本包的正常业务（OP-60 的项目级副本），
# 那样会直接把它堵死。**只做第 1 条会漏掉这一整类失效。**
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid_a, _ = _seed_tx(conn)
        pid_b, _ = _seed_tx(conn)
        # 两行同 (key, revision_no) 但 scope=PROJECT 且项目不同。
        _setting_tx(conn, PROBE_KEY, "PROJECT", "INHERITED", 1, project_id=pid_a)
        _setting_tx(conn, PROBE_KEY, "PROJECT", "INHERITED", 1, project_id=pid_b)
        emit(
            True,
            "部分唯一 GLOBAL 反向：PROJECT 行不受它管辖（证明 WHERE 在过滤）",
            "同 (key, rev) 的两个项目行都能插入",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(
        False,
        "部分唯一 GLOBAL 反向：PROJECT 行不受它管辖",
        f"被误拦 —— 索引可能退化成全局唯一。{type(exc).__name__}: {exc}",
    )

# ---------------------------------------------------------------------------
# 活性验证 3：部分唯一索引（PROJECT）正向 —— 同项目重复被拒
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, _ = _seed_tx(conn)
        _setting_tx(conn, PROBE_KEY, "PROJECT", "INHERITED", 1, project_id=pid)
        try:
            _setting_tx(conn, PROBE_KEY, "PROJECT", "INHERITED", 1, project_id=pid)
            emit(False, "部分唯一 PROJECT 正向：同项目重复 (key, rev) 被拒", "竟然写进去了")
        except DBAPIError as exc:
            name = constraint_name(exc)
            emit(
                sqlstate(exc) == "23505" and name == "uq_runtime_settings_project",
                "部分唯一 PROJECT 正向：同项目重复 (key, rev) 被拒",
                f"sqlstate={sqlstate(exc)} constraint={name}",
            )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "部分唯一 PROJECT 正向", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 4：★ 部分唯一索引（PROJECT）反向 —— 键里必须含 project_id
#
# 与验证 2 **不同**：验证 2 证明"GLOBAL 索引不越界管 PROJECT 行"，
# 本条证明"PROJECT 索引按项目分区"。少了 project_id 这个键列，
# 同一 (key, rev) 在相邻项目之间会互相顶掉 —— 而两个项目各配自己的
# research.maxEffectiveFiles 是**各自独立**的正常业务。
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid_a, _ = _seed_tx(conn)
        pid_b, _ = _seed_tx(conn)
        _setting_tx(conn, PROBE_KEY, "PROJECT", "MANUAL", 1, project_id=pid_a)
        _setting_tx(conn, PROBE_KEY, "PROJECT", "MANUAL", 1, project_id=pid_b)
        emit(
            True,
            "部分唯一 PROJECT 反向：两个项目同 (key, rev) 都能插入（键含 project_id）",
            "两个项目行都插入成功",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(
        False,
        "部分唯一 PROJECT 反向：两个项目同 (key, rev) 都能插入",
        f"被误拦 —— 索引可能丢了 project_id 键列。{type(exc).__name__}: {exc}",
    )

# ---------------------------------------------------------------------------
# 活性验证 5：不可变 —— runtime_settings 的 UPDATE 被拦
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        _setting_tx(conn, PROBE_KEY, "GLOBAL", "GLOBAL", 1)
        try:
            conn.execute(
                text("UPDATE runtime_settings SET reason='篡改' WHERE key = :k"),
                {"k": PROBE_KEY},
            )
            emit(False, "不可变：runtime_settings UPDATE 被拦", "竟然成功了")
        except DBAPIError as exc:
            emit(
                sqlstate(exc) == "23001",
                "不可变：runtime_settings UPDATE 被拦",
                f"sqlstate={sqlstate(exc)}",
            )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "不可变：runtime_settings UPDATE 被拦", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 6：不可变 —— runtime_settings 的 DELETE 被拦（触发器的另一个分支）
#
# UPDATE 与 DELETE 是同一个触发器函数里的两条 RAISE，**必须分别证明** ——
# 只测 UPDATE 会漏掉"DELETE 分支写错"这种形态。
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        _setting_tx(conn, PROBE_KEY, "GLOBAL", "GLOBAL", 1)
        try:
            conn.execute(
                text("DELETE FROM runtime_settings WHERE key = :k"), {"k": PROBE_KEY}
            )
            emit(False, "不可变：runtime_settings DELETE 被拦", "竟然成功了")
        except DBAPIError as exc:
            emit(
                sqlstate(exc) == "23001",
                "不可变：runtime_settings DELETE 被拦",
                f"sqlstate={sqlstate(exc)}",
            )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "不可变：runtime_settings DELETE 被拦", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 7：★ 对照 A —— plan_drafts 的 UPDATE **必须能通过**
#
# `plan_drafts` 是工作区（运营反复调整），**刻意不挂**不可变触发器。
# 若这条被拦，说明触发器挂错了表 —— 而"两张表都锁上"的实现
# 也能让验证 5、6 全绿。
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, _ = _seed_tx(conn)
        did = _draft_tx(conn, pid)
        try:
            conn.execute(
                text("UPDATE plan_drafts SET total_quantity = 10 WHERE id = :i"),
                {"i": str(did)},
            )
            emit(True, "对照 A：plan_drafts UPDATE 能通过（工作区必须可变）", "通过")
        except DBAPIError as exc:
            emit(
                False,
                "对照 A：plan_drafts UPDATE 能通过",
                f"被误拦 sqlstate={sqlstate(exc)} —— 触发器可能挂错了表",
            )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "对照 A：plan_drafts UPDATE 能通过", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 8：★ 对照 B —— plan_drafts 的 DELETE **必须能通过**
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, _ = _seed_tx(conn)
        did = _draft_tx(conn, pid)
        try:
            conn.execute(
                text("DELETE FROM plan_drafts WHERE id = :i"), {"i": str(did)}
            )
            emit(True, "对照 B：plan_drafts DELETE 能通过（草稿可放弃）", "通过")
        except DBAPIError as exc:
            emit(
                False,
                "对照 B：plan_drafts DELETE 能通过",
                f"被误拦 sqlstate={sqlstate(exc)}",
            )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "对照 B：plan_drafts DELETE 能通过", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 9~14：runtime_settings 的五条形状约束（每条配对照）
# ---------------------------------------------------------------------------

def _expect_check(label, conn, action, sqlstate_expected="23514", constraint=None):
    # 公共外壳：执行 action，断言被 CHECK 拦下且约束名对得上。
    # **不吞异常**：action 成功也要报 FAIL（"见谁都拒"的反面同样是缺陷）。
    try:
        action()
        emit(False, label, "竟然写进去了 —— 约束未生效")
    except DBAPIError as exc:
        name = constraint_name(exc)
        ok = sqlstate(exc) == sqlstate_expected and (
            constraint is None or name == constraint
        )
        emit(ok, label, f"sqlstate={sqlstate(exc)} constraint={name}")


# 验证 9：GLOBAL 行带 project_id → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, _ = _seed_tx(conn)
        _expect_check(
            "形状：GLOBAL 行带 project_id 被拒（scope_matches_project）",
            conn,
            lambda: _setting_tx(
                conn, PROBE_KEY, "GLOBAL", "GLOBAL", 1, project_id=pid
            ),
            constraint="ck_runtime_settings_scope_matches_project",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "形状：GLOBAL 行带 project_id 被拒", f"探针异常 {type(exc).__name__}: {exc}")

# 验证 10：PROJECT 行**不带** project_id → 被拒（同一条 CHECK 的另一分支）
try:
    with engine.connect() as conn:
        trans = conn.begin()
        _seed_tx(conn)
        _expect_check(
            "形状：PROJECT 行不带 project_id 被拒（同条 CHECK 的另一分支）",
            conn,
            lambda: _setting_tx(conn, PROBE_KEY, "PROJECT", "INHERITED", 1),
            constraint="ck_runtime_settings_scope_matches_project",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "形状：PROJECT 行不带 project_id 被拒", f"探针异常 {type(exc).__name__}: {exc}")

# 验证 11：GLOBAL 行标 origin='MANUAL' → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        _seed_tx(conn)
        _expect_check(
            "形状：GLOBAL 行标 origin=MANUAL 被拒（origin_matches_scope）",
            conn,
            lambda: _setting_tx(conn, PROBE_KEY, "GLOBAL", "MANUAL", 1),
            constraint="ck_runtime_settings_origin_matches_scope",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "形状：GLOBAL 行标 origin=MANUAL 被拒", f"探针异常 {type(exc).__name__}: {exc}")

# 验证 12：拼错的 key → 被拒（R8 万能杂货表的处置）
try:
    with engine.connect() as conn:
        trans = conn.begin()
        _seed_tx(conn)
        _expect_check(
            "形状：拼错的 key='batch.limt' 被拒（key 是闭集）",
            conn,
            lambda: _setting_tx(conn, "batch.limt", "GLOBAL", "GLOBAL", 1),
            constraint="ck_runtime_settings_key_valid",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "形状：拼错的 key 被拒", f"探针异常 {type(exc).__name__}: {exc}")

# 验证 13：value 是数组 → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        _seed_tx(conn)
        _expect_check(
            "形状：value 是数组被拒（value_is_object）",
            conn,
            lambda: _setting_tx(
                conn, PROBE_KEY, "GLOBAL", "GLOBAL", 1, value=[1, 2, 3]
            ),
            constraint="ck_runtime_settings_value_is_object",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "形状：value 是数组被拒", f"探针异常 {type(exc).__name__}: {exc}")

# 验证 14：revision_no = 0 → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        _seed_tx(conn)
        _expect_check(
            "形状：revision_no=0 被拒（revision_no_positive）",
            conn,
            lambda: _setting_tx(conn, PROBE_KEY, "GLOBAL", "GLOBAL", 0),
            constraint="ck_runtime_settings_revision_no_positive",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "形状：revision_no=0 被拒", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 15~18：plan_drafts 的形状约束
# ---------------------------------------------------------------------------
# 验证 15：total_quantity = 100001（防御性上界 +1）→ 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, _ = _seed_tx(conn)
        _expect_check(
            "形状：plan_drafts total_quantity=100001 被拒（防御性上界）",
            conn,
            lambda: _draft_tx(conn, pid, total_quantity=100001),
            constraint="ck_plan_drafts_total_quantity_within_bound",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "形状：total_quantity=100001 被拒", f"探针异常 {type(exc).__name__}: {exc}")

# 验证 16：total_quantity = 0 → 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, _ = _seed_tx(conn)
        _expect_check(
            "形状：plan_drafts total_quantity=0 被拒（下界留在 DDL）",
            conn,
            lambda: _draft_tx(conn, pid, total_quantity=0),
            constraint="ck_plan_drafts_total_quantity_positive",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "形状：total_quantity=0 被拒", f"探针异常 {type(exc).__name__}: {exc}")

# 验证 17：★ 下界的**对照** —— total_quantity IS NULL 必须能通过
# （"还没定总量"是合法中间态，原型的交互顺序正是"先配比例、后定总量"）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, _ = _seed_tx(conn)
        try:
            _draft_tx(conn, pid, total_quantity=None)
            emit(
                True,
                "对照：total_quantity IS NULL 能通过（合法中间态）",
                "通过",
            )
        except DBAPIError as exc:
            emit(
                False,
                "对照：total_quantity IS NULL 能通过",
                f"被误拦 sqlstate={sqlstate(exc)}",
            )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "对照：total_quantity IS NULL 能通过", f"探针异常 {type(exc).__name__}: {exc}")

# 验证 18：state='DRAFT'（不是 EDITING/SUBMITTED）→ 被拒
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, _ = _seed_tx(conn)
        _expect_check(
            "形状：plan_drafts state='DRAFT' 被拒（state 是闭集）",
            conn,
            lambda: _draft_tx(conn, pid, state="DRAFT"),
            constraint="ck_plan_drafts_state_valid",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "形状：state='DRAFT' 被拒", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 19：★ 全局对照 —— 一整套**合法**的 runtime_settings 行必须能写完
#
# 前 18 条里有 14 条是"断言被拒"。若约束写得过严（例如 value_is_object
# 被写成 `value IS NOT NULL AND ...`），只有这一类整体对照会红。
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, _ = _seed_tx(conn)
        _setting_tx(conn, PROBE_KEY, "GLOBAL", "GLOBAL", 1)
        _setting_tx(conn, PROBE_KEY, "GLOBAL", "GLOBAL", 2)  # 新修订号，合法
        _setting_tx(conn, PROBE_KEY, "PROJECT", "INHERITED", 1, project_id=pid)
        _setting_tx(conn, PROBE_KEY, "PROJECT", "MANUAL", 2, project_id=pid)
        _setting_tx(
            conn, "platform.mediaLimits", "GLOBAL", "GLOBAL", 1,
            value={"images": {"maxBytes": 10_000_000}},
        )
        n = conn.execute(
            text(
                "SELECT count(*) FROM runtime_settings WHERE key IN (:k1, :k2)"
            ),
            {"k1": PROBE_KEY, "k2": "platform.mediaLimits"},
        ).scalar_one()
        emit(n == 5, "全局对照：5 行合法配置全部写入（GLOBAL×3 + PROJECT×2）", f"{n} (期望 5)")
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "全局对照：合法配置全部写入", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 20：★ 全局对照 —— 一整套**合法**的 plan_drafts 行必须能写完
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, _ = _seed_tx(conn)
        _draft_tx(conn, pid, total_quantity=None, state="EDITING")
        # 反例（**不要**照抄）：同一项目再插第二条会撞 uq_plan_drafts_project_id（23505）。
        #   _draft_tx(conn, pid, total_quantity=50, state="SUBMITTED")
        # 见下面「探针自身的坑」第 6 条 —— 那里有实测到的 SQLSTATE 与约束名。
        n = conn.execute(
            text("SELECT count(*) FROM plan_drafts WHERE project_id = :p"),
            {"p": str(pid)},
        ).scalar_one()
        emit(n == 1, "全局对照：合法 plan_drafts 写入（EDITING + 总量 NULL）", f"{n} (期望 1)")
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "全局对照：合法 plan_drafts 写入", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证（补）：纯函数层 —— 停用项权重归零 + 容量满时 unallocated 合法
#
# 这两条**不在数据库层**（比例放在 JSONB 里，落不了 CHECK），
# 因此必须由纯函数层的第二数据源互证。它们各自对应一处已修的缺陷：
#   · 停用项权重归零 = MAJOR-5（停用项白拿分配额）的现场形态；
#   · 容量满时 unallocated > 0 = C14 的**合法结果**（阶段 7 删掉了
#     `REASON_ALLOCATION_OVER_CAPACITY`，因为它与这个设计相反）。
# ---------------------------------------------------------------------------
try:
    from rednote.modules.planning.allocation import allocate_by_weight
    from rednote.modules.planning.permille import DISABLED_PERMILLE
    from rednote.modules.planning.service import _to_allocation_items
    from rednote.modules.planning.types import ITEM_STATE_DISABLED, AllocationItem

    # 探针 21：停用项权重必须**经由常量**归零（MAJOR-5 现场）。
    #
    # ⚠️ 入口必须是生产侧的 `_to_allocation_items`（JSONB 行 → 分配输入），
    # **不能**手工造一个 `weight_permille=0` 的 item 去断言"它分到 0" ——
    # 权重 0 必然分 0，那是**恒真式**（判据纪律第 2/7 条），
    # 抓不到"归零这一步被漏做"。真实风险只在"存储形态 → 分配输入"的转换里。
    _rows = [
        {"key": "A", "state": "ENABLED", "weightPermille": 600, "sortOrder": 0},
        {"key": "B", "state": ITEM_STATE_DISABLED, "weightPermille": 400, "sortOrder": 1},
    ]
    _items = _to_allocation_items(_rows)
    _disabled_weight = next(i.weight_permille for i in _items if i.key == "B")
    emit(
        _disabled_weight == DISABLED_PERMILLE,
        "纯函数：停用项经 _to_allocation_items 后权重归零（常量，非裸 0）",
        f"DISABLED_PERMILLE={DISABLED_PERMILLE} 实际权重={_disabled_weight}",
    )

    # 探针 21-反向（★ 证明上一条能报红）：把"归零"去掉，结果必须**不同**。
    # 不做这一步的话，"归零"与"未归零"两种实现都会让上一条通过。
    _naive = [
        AllocationItem(
            key=r["key"],
            weight_permille=int(r["weightPermille"]),
            capacity=None,
            sort_order=r["sortOrder"],
            name=str(r.get("name", "")),
        )
        for r in _rows
    ]
    _real_b = allocate_by_weight(_items, 10).allocated_by_key().get("B", 0)
    _naive_b = allocate_by_weight(_naive, 10).allocated_by_key().get("B", 0)
    emit(
        _real_b == 0 and _naive_b > 0,
        "纯函数反向：不归零时停用项会白拿分配额（证明上条能报红）",
        f"归零后 B={_real_b} / 不归零 B={_naive_b}（必须 >0）",
    )

    # 探针 22：容量削峰后剩余计入 unallocated，**不报错**（C14 的合法结果）。
    # 这条对应阶段 7 删掉的 `REASON_ALLOCATION_OVER_CAPACITY` ——
    # 那个错误码的语义（"超出容量是错误"）与 C14 的设计相反。
    _capped = allocate_by_weight(
        [
            AllocationItem(key="A", weight_permille=500, capacity=10, sort_order=0, name="容量 10"),
            AllocationItem(key="B", weight_permille=500, capacity=10, sort_order=1, name="容量 10"),
        ],
        100,
    )
    _granted = _capped.allocated_by_key()
    emit(
        _granted == {"A": 10, "B": 10} and _capped.unallocated == 80,
        "纯函数：容量全满时剩余 80 条计入 unallocated 且不报错（C14 合法结果）",
        f"granted={_granted} unallocated={_capped.unallocated} rounds={_capped.rounds}",
    )
except Exception as exc:  # noqa: BLE001
    emit(False, "纯函数层（停用项归零 / 容量削峰）", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 零残留核对
# ---------------------------------------------------------------------------
with engine.connect() as conn:
    # `runtime_settings` 有迁移写入的真值行（batch.limits rev=1），
    # 因此零残留判据是"**只有**迁移那一行"。
    n = conn.execute(text("SELECT count(*) FROM runtime_settings")).scalar_one()
    emit(n == 1, "零残留 runtime_settings（只剩迁移回填的 1 行）", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM runtime_settings WHERE key = :k"), {"k": PROBE_KEY}
    ).scalar_one()
    emit(n == 0, "零残留 探针配置键", f"{n} 行")
    n = conn.execute(text("SELECT count(*) FROM plan_drafts")).scalar_one()
    emit(n == 0, "零残留 plan_drafts", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM users WHERE issuer = 'wp24-probe'")
    ).scalar_one()
    emit(n == 0, "零残留 探针用户", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM projects WHERE name = 'WP24 探针项目'")
    ).scalar_one()
    emit(n == 0, "零残留 探针项目", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM brands WHERE name LIKE 'WP24 探针品牌%'")
    ).scalar_one()
    emit(n == 0, "零残留 探针品牌", f"{n} 行")

print(f"RESULT_EMITTED (期望 {EXPECTED_CHECKS} 条)")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def collect_wp24() -> tuple[Path, bool]:
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
        "# WP24 场景形式草稿与数量配置：2 张表 / 15 CHECK / 1 触发器 / 2 部分唯一索引"
        " / 20 处库内验证 + 3 条纯函数判据 / 契约",
        f"# 生成时间: {_now()}",
        "# 目标库: rednote_verify_wp24（收口时由迁移从 bootstrap 建出，非测试夹具建表）",
        "# 命令: cd services/backend && python -c '<information_schema + 试写 + 试插>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp24-scene-draft.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # **条数不足也算失败**（ISS-43：崩溃伪装成"全过"）。
    complete = len(checks) >= EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete)


def main() -> int:
    path, ok = collect_wp24()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
