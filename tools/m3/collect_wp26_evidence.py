#!/usr/bin/env python
"""WP26 证据采集脚本。

产出的文件（落在 `evidence/m3/`）：

| 文件 | 内容 |
|---|---|
| `wp26-content-generation.log` | 5 张表结构 / 38 CHECK / 21 FK / 17 索引 / 4 条延迟 FK / 7 个触发器（6 挂本包表 + 1 挂**共用表** `version_nodes`），+ **库内活性验证 + 纯函数判据** |

## 本包的结构性异同（与前几个包比）

- 与 WP25 相同：`downgrade` 必须**只删自己的函数**（`consistency_items()` 有意只含 3 项），
  共用函数 `rednote_reject_mutation()` 一个都不许删。
- 与 WP25 不同（**本包独有**）：
  1. **4 张不可变表**（`content_versions` / `narrative_blueprints` / `title_candidates` /
     `generation_contexts`），因此"该拦的拦"从 2×2 变 4×2 = **8 条**；
     `content_items` 是**可变**身份行（`current_version_id` 投影 + `lock_version`），
     它必须**能改** —— 这一条是"见谁都拒"的反面，缺了它，一个把所有表都锁上的
     实现能让 8 条全绿而系统完全不可用。
  2. **1 个触发器挂在共用表 `version_nodes` 上**（`trg_version_nodes_content_paired`）。
     它**不是**本包的表 —— 因此核对方式与另外 6 个**不同形**：
     本包表上的触发器用"表名 IN (本包 5 表)"取集合；这一个必须**点名**，
     且**不断言 `version_nodes` 上的触发器总数**（判据纪律第 1 条：别的包会继续往上挂）。
  3. **4 条延迟 FK**（环 FK 3 条 + `fk_content_versions_id_version_nodes` 1 条）。
     `README` 曾把"3 条"当成延迟 FK 总数，实测是**环 FK 3 条 / 延迟 FK 共 4 条** ——
     两个口径必须分开说（本脚本 S13 逐条点名 + F 节两条负向判据一起钉住）。
  4. **本包无部分索引**（有意形状）。`ddl.index_names()` 的 17 条里
     14 条普通 + 3 条普通唯一，`indexdef` 全部不含 `WHERE`。
     用 WP25 的"部分索引 WHERE"核对项在这里**不适用**，改为断言"一条都没有" ——
     与 `consistency_items() == []` 的写法同一种形态：把"有意为空"与"忘了写"分开。

## 覆盖范围由**磁盘**决定，不由手写名单决定（判据纪律第 14 条）

38 条 CHECK / 21 条 FK **全部**由两侧机械枚举后**逐条对账**：

- 期望侧：`content_models.Base.metadata.tables[t]` 的 `sa.CheckConstraint` /
  `sa.ForeignKeyConstraint` 集合（来自 `models.py`）；
- 实际侧：`pg_constraint` 里该表的同名集合。

17 条索引同理：期望侧 `content_ddl.index_names()`（**从 ORM `__table__.indexes` 派生**，
见该函数 docstring），实际侧 `pg_indexes`。因此"少一条"或"换一条"都会在那一张表上
报红并打印差集 —— 总数相等但成员不同时，只比总数的判据是**恒绿**的（判据纪律第 4 条）。

## 探针数据的处置

每条验证跑在**独立连接**里，最后**无条件 `rollback()`** → **零残留**。
理由：本包 5 张表里 4 张是**不可变表**（触发器拦 `DELETE`），一旦 commit 就删不掉
（WP18 的 ISS-63、WP24 的坑 4、WP25 的 T 节均已实测）。

**本脚本用在哪个库**：默认 `rednote_verify_wp26`（收口时新建的干净库，迁移从
bootstrap 一路跑到 head 建出全部结构，实测 62 张表、停在 `1c1c814b248e`）。
用它的理由与 WP24/WP25 完全一致：分支测试库里的表是**测试夹具 `create_all` 建的**，
不是迁移建的 —— 在那种库上取证据，等于在验证夹具而不是验证迁移。

## 跨包回归项（本包最容易误伤别人的地方）

- 共用的不可变触发器函数 `rednote_reject_mutation()` 必须仍在（本包 `downgrade`
  **刻意不删它** —— 全项目不可变表共用）；
- `version_nodes` 上 **WP18 / WP20 / WP22 / WP23** 的 4 个 `*_paired` 触发器必须共存
  （本包只挂自己的第 5 个，不得误删别人的）；
- WP18 / WP20 / WP22 / WP23 的一致性函数必须存活（下界判据，按包分列，
  这样"某个包整批消失"不会被合计数掩盖）；
- 本包**不拥有** `users` / `brands` / `products` / `projects` / `version_nodes` 等表，
  只作为 FK 目标引用它们 —— 探针用**独立事务 + rollback**，不污染它们。

## 条数 = EXPECTED_CHECKS（分节实测，见脚本末尾 RESULT 行）

| 节 | 内容 |
|---|---|
| S | 迁移 head / 5 表存在 / 库总表数下界 / 5 表列数 / 逐表 CHECK+FK 名单 / 总数 / 触发器集合与函数 / 延迟 FK 点名 / 索引点名 / 无部分索引 / 跨包共存 |
| T | 4 张不可变表 ×(UPDATE+DELETE) 该拦的拦 + `content_items` 该过的过 |
| C | OP-88 正向 3 + 反向 1 + 收窄对照 1 + 三条跨表链接 3 + 合法对照 1 |
| F | 两条环 FK 的**正向能过 / 反向能拦** + 两条非延迟 FK 的当场拒 |
| D | 形状约束（每条配"合法值必须通过"的对照，防"见谁都拒"） |
| P | 纯函数层（含反向，证明正向判据能区分两种实现） |
| K | 契约 |
| Z | 零残留 |

## 探针自身踩过的坑（写在这里，避免重复踩）

本节是"证据脚本自己出错"的记录 —— 与 WP18 的 ISS-63、WP23 的四处坑、WP24 的七处坑、
WP25 的五处坑同源（探针出错会**伪装成实现有缺陷**，比不写探针更危险）。

1. **★ 期望条数必须实测，不能凭印象**（WP24 坑 2 / WP25 坑 1 的同型）：
   初稿写的 `EXPECTED_CHECKS` 由人估，实跑输出与它不等。危害不是"数字不好看"：
   `complete = len(checks) >= EXPECTED_CHECKS` 在期望值偏低时**恒真**，
   于是"静默少跑若干条"**永远不会报红**（ISS-43）。本脚本按 WP-DEBT-02 的 ISS-153
   教训，**另加一条相等判据** `exact = len(checks) == EXPECTED_CHECKS` 并入 verdict。
2. **`version_nodes.entity_id` 与 `content_versions.id` 是两个不同的值**：
   对 `CONTENT`，`version_nodes.id == content_versions.id`（本行就是那个版本），
   而 `version_nodes.entity_id == content_versions.content_item_id`（内容身份）。
   把两者写成同一个 uuid 会让"反向触发器"与"正向判据"同时失去意义
   （`models.py` 模块 docstring 二专门记了这一点）。
3. **随机 `uuid4()` 的 `entity_id` 会走反向触发器的"放行"分支**：
   反向触发器是**收窄**过的（`IF NOT EXISTS (SELECT 1 FROM content_items WHERE id = NEW.entity_id)
   THEN RETURN NULL`）。因此测"孤立节点必须被拦"时**必须先造真实的 `content_items` 行**，
   否则会以 "NO ERROR" 报红，而那是收窄的**预期行为**（ISS-175）。
   本脚本 C7（拦）/ C8（放行）成对，少任一都会让另一条的结论不可信。
4. **延迟约束不会在 INSERT 那一刻检查**：`content_versions.id → version_nodes.id`、
   两条环 FK 都是 `DEFERRABLE INITIALLY DEFERRED`。测它们**必须在事务里显式
   `SET CONSTRAINTS ALL IMMEDIATE`**，否则"坏值写进去了"会被读成"防线失效"，
   而其实是"检查点还没到"。
5. **`SET CONSTRAINTS ALL IMMEDIATE` 会把**所有**延迟约束一起强制** ——
   因此铺底数据必须**先自洽**（节点与领域行都齐），否则报出来的是另一条无关 FK 的
   `23503`，与本条判据要测的东西无关（"被拒的结论正确不代表被拒的原因正确"，
   WP24 坑 5 / WP25 坑 3 同族）。
6. **一条语句报错会把整个事务打成 aborted**（后续语句返回 `25P02`）。
   因此"被拦"与"对照通过"两类探针**必须各自独立事务与独立连接**。
7. **不可变表探针绝不能 commit**：一旦提交就**永远删不掉**（触发器拦 `DELETE`）。
   每条探针独立连接 + 无条件 `rollback()` ⇒ 零残留（Z 节 9 条判据专门核对）。
8. **内联脚本只允许单行注释且不能出现三引号**（ISS-45）：三引号会提前终止外层
   `r\"\"\"...\"\"\"`，后面全被当外层代码解析。本文件由 `check_evidence_scripts.py` 机械核对。
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

#: 期望条数（与内联脚本里的 EXPECTED_CHECKS 保持同值）。
#: 两处都要：内联脚本用它写进 RESULT 行，外层用它判定成功与否（ISS-43）。
EXPECTED_CHECKS: int = 96

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

INSPECT_SCRIPT = r"""
# 内联脚本：只允许单行注释，且不能出现三引号（三引号会提前终止外层包裹，见 ISS-45）。

import json
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from rednote.modules.content import ddl as content_ddl
from rednote.modules.content import models as content_models
from rednote.modules.content import service as content_service
from rednote.modules.content import types as content_types

# 期望产生的核对条数（ISS-43：崩溃伪装成"全过"的防线）。
# 实测回填：首轮写 93 是人力估值，实跑 95 条 ⇒ 估值偏低会让
# `len(checks) >= EXPECTED_CHECKS` 恒真（坑 1）。改 95 后两处同值。
EXPECTED_CHECKS = 96

DB = "postgresql+psycopg://rednote@localhost:5432/rednote_verify_wp26"

# WP26 收口之后才由别的迁移加到本包表上的约束（迁移 f3a7c9d15b28：content 场景/形式接缝）。
# 本脚本验证的是 WP26 收口时冻结的快照库（head=1c1c814b248e），所以模型侧对账
# 必须排除它们，否则 S5 会把"后续迁移的产物"误报成"本包缺失"。
# 排除不得空转：下方 [S5b] 断言这些名字确实还在模型里（被删了就该来改这里）。
LATER_MIGRATION_CHECKS = frozenset(
    {
        "ck_content_items_scene_key_not_blank",
        "ck_content_items_form_key_not_blank",
    }
)
engine = create_engine(DB)

CONTRACT_PATH = Path(
    "/Users/<user>/内容运营AI Native/rednote-production/contracts/openapi.json"
)

# 本包引入的迁移版本。判据用"不早于"而不是"恰好等于"（ISS-44：
# 硬编码"恰好停在某个版本"会让每个新工作包把历史证据集体打红）。
REVISION_INTRODUCED = "1c1c814b248e"

# 本包 5 张表的期望列数（按 information_schema **实测**填写，不凭印象）。
TABLE_COLUMNS = (
    ("content_items", 12),
    ("content_versions", 15),
    ("narrative_blueprints", 14),
    ("title_candidates", 9),
    ("generation_contexts", 13),
)

# 本包 5 表上的 6 个触发器（4 不可变 + 2 一致性）。
# ★ `trg_version_nodes_content_paired` **不在这 6 个里面** —— 它挂在共用表
# `version_nodes` 上，必须单独点名核对（见 SHARED_NODE_TRIGGER）。
OWN_TABLE_TRIGGERS = (
    "trg_content_versions_immutable",
    "trg_narrative_blueprints_immutable",
    "trg_title_candidates_immutable",
    "trg_generation_contexts_immutable",
    "trg_content_versions_node_consistency",
    "trg_generation_contexts_content_consistency",
)

# 本包挂在**共用表** version_nodes 上的那一个（OP-88 的 CONTENT 反向那一半）。
SHARED_NODE_TRIGGER = "trg_version_nodes_content_paired"
SHARED_NODE_FUNCTION = "rednote_check_content_node_paired"

# 别的包挂在同一张 version_nodes 上的配对触发器 —— 必须共存（本包不得误删）。
# 只断言"这 4 个还在"，**不**断言 version_nodes 上触发器总数（判据纪律第 1 条）。
OTHER_PACK_NODE_TRIGGERS = (
    "trg_version_nodes_baseline_paired",
    "trg_version_nodes_brief_paired",
    "trg_version_nodes_knowledge_paired",
    "trg_version_nodes_prompt_paired",
)

# ★ 真库实测的 4 条延迟 FK（condeferrable=t AND condeferred=t），逐条点名。
# ⚠️ 其中"环 FK"只有 3 条（前三条）—— README 曾把"3 条"当成延迟 FK 总数；
#    两个口径必须分开说：环 FK 3 条 / 延迟 FK 共 4 条（含 id→version_nodes 那条）。
DEFERRED_FOREIGN_KEYS = (
    "fk_content_items_current_version_id_content_versions",
    "fk_content_versions_generation_context_id_generation_contexts",
    "fk_content_versions_id_version_nodes",
    "fk_generation_contexts_content_version_id_content_versions",
)

# ★ 必须**不**延迟的两条（负向判据）：延迟它们会让"版本挂在不存在的内容上"
# 能在事务里存活到提交前 —— 而"版本必属于已存在的内容"没有合法流程需要这个顺序。
NON_DEFERRED_FOREIGN_KEYS = (
    "fk_content_versions_content_item_id_content_items",
    "fk_title_candidates_content_version_id_content_versions",
)

# 跨包一致性函数的下界（按包分列，这样"某个包整批消失"依然能被看见）。
CROSS_PACK_FUNCTIONS = (
    ("baseline", 6),
    ("brief", 4),
    ("knowledge", 4),
    ("prompt", 4),
)

# 契约里本包的 6 个路径 / 6 个操作。
OWN_PATHS = (
    "/api/v1/contents/{contentId}",
    "/api/v1/contents/{contentId}/regenerations",
    "/api/v1/contents/{contentId}/title-adoptions",
    "/api/v1/contents/{contentId}/versions",
    "/api/v1/projects/{projectId}/content-generations",
    "/api/v1/projects/{projectId}/contents",
)

# 探针用的标记：全部探针数据的名字都带它，便于零残留核对。
PROBE_ISSUER = "wp26-probe"

# 不可变违反的 sqlstate（触发器 ERRCODE，platform/immutability.py 定义）。
IMMUTABLE_VIOLATION_SQLSTATE = "23001"

# 一致性触发器的 sqlstate（ddl.CONSISTENCY_SQLSTATE）。
CONSISTENCY_SQLSTATE = content_ddl.CONSISTENCY_SQLSTATE


def _revision_is_at_least(current, introduced):
    # 当前 head 是否**不早于** introduced —— 沿真实迁移链判断，不是猜。
    #
    # **为什么不能写 `== EXPECTED_REVISION`**（ISS-44）：那会让每个新工作包
    # 把前面所有包的历史证据集体打红，因为校验的其实是"迁移停在某个特定
    # 版本"，而真正该校验的是"**该包引入的表/约束还在**"。
    #
    # **为什么也不能用 `len(current) >= len(introduced)`**（ISS-115）：
    # 本项目全部 revision id 都是 12 位十六进制，于是"长度不短于"在
    # `current != introduced` 时**恒为 True** —— 那是把等式判据换成了
    # **恒绿判据**（判据纪律第 7/9 条）。
    #
    # 正确做法：拿到**真实迁移链**，按位置比较。`walk_revisions(base=..., head=...)`
    # 的返回值是 head→base 降序，因此 index 小 = 新。
    # 该调用会 import 每个迁移模块，故内联段必须在 PYTHONPATH=<repo>/src 下运行。
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


def _expect_sqlstate(label, conn, action, sqlstate_expected="23514"):
    # 与 _expect_check 同形，但**不断言约束名** —— 触发器 RAISE EXCEPTION
    # 不带 CONSTRAINT 名，diag.constraint_name 为空（WP26 的 ddl.py 已论证）。
    try:
        action()
        emit(False, label, "竟然写进去了 —— 防线未生效")
    except DBAPIError as exc:
        emit(
            sqlstate(exc) == sqlstate_expected,
            label,
            f"sqlstate={sqlstate(exc)} constraint={constraint_name(exc)}",
        )


def _expect_ok(label, conn, action, detail=""):
    # 反向外壳：action **必须**成功。用于"该过的过"的对照。
    # 一条语句报错会把事务打成 aborted（后续返回 25P02），因此每条探针
    # 自带独立连接与事务（WP24 的坑 3）。
    try:
        action()
        emit(True, label, detail or "写入成功（符合预期）")
    except DBAPIError as exc:
        emit(False, label, f"被拒了 —— 约束锁过头：sqlstate={sqlstate(exc)}"
                           f" constraint={constraint_name(exc)}")


def _force(conn):
    # 把全部延迟约束强制成立刻检查。★ 这是本包探针的**必需**前置：
    # 4 条延迟 FK 与 3 个 CONSTRAINT TRIGGER 都在 COMMIT 期才跑，
    # 而本脚本**从不 commit**（不可变表 commit 了就删不掉）⇒
    # 不显式 SET CONSTRAINTS，它们的检查点永远不会到。
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


# ---------------------------------------------------------------------------
# 铺底：造最小可用的 user / brand / product / project
# ---------------------------------------------------------------------------

def _seed(conn):
    # 非空集合全部按 information_schema 实测（本项目已多次踩中列名/非空）。
    #   · users    : 没有 email，是 (issuer, subject) 唯一；status 有默认值
    #   · products : 有 model_code（不是 code）
    #   · projects : 没有 code/created_by；brand_id / product_id / owner_id /
    #                target_delivery_count / due_date 非空（phase / timezone /
    #                external_policy 有默认值，可不写）
    uid = uuid.uuid4()
    brand = uuid.uuid4()
    prod = uuid.uuid4()
    proj = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, display_name, role, status)"
            " VALUES (:u, :iss, :sub, 'WP26 探针', 'ADMIN', 'ACTIVE')"
        ),
        {"u": str(uid), "iss": PROBE_ISSUER, "sub": f"probe-{uid}"},
    )
    conn.execute(
        text("INSERT INTO brands (id, name, status) VALUES (:b, :n, 'ACTIVE')"),
        {"b": str(brand), "n": f"WP26 品牌 {str(brand)[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO products (id, brand_id, name, model_code, status)"
            " VALUES (:p, :b, :n, :c, 'ACTIVE')"
        ),
        {
            "p": str(prod), "b": str(brand),
            "n": f"WP26 产品 {str(prod)[:8]}", "c": f"WP26-{str(prod)[:8]}",
        },
    )
    conn.execute(
        text(
            "INSERT INTO projects (id, project_code, name, brand_id, product_id,"
            " owner_id, target_delivery_count, due_date)"
            " VALUES (:p, :c, :n, :b, :pr, :o, 1, (now() + interval '30 days')::date)"
        ),
        {
            "p": str(proj), "c": f"WP26PROBE{str(proj)[:8]}", "n": "WP26 探针项目",
            "b": str(brand), "pr": str(prod), "o": str(uid),
        },
    )
    return {
        "user": str(uid), "brand": str(brand), "product": str(prod),
        "project": str(proj),
    }


# ---------------------------------------------------------------------------
# 5 张表的插入辅助（形状约束由调用方负责 —— 探针要能造出违规行）
# ---------------------------------------------------------------------------

def _item_tx(conn, ids, content_id, *, state="DRAFT", current_version_id=None):
    # content_items 是**可变**身份行。
    conn.execute(
        text(
            "INSERT INTO content_items (id, project_id, current_version_id, state,"
            " current_version_no, lock_version)"
            " VALUES (:c, :p, :cv, :s, 0, 0)"
        ),
        {"c": str(content_id), "p": ids["project"],
         "cv": str(current_version_id) if current_version_id else None,
         "s": state},
    )


def _node_tx(conn, ids, node_id, entity_id, *, entity_type="CONTENT",
             revision_no=1):
    # 版本节点。★ `node_id` 是**这次版本**的 id，`entity_id` 是**逻辑实体**身份
    # （见坑 2）；两者对 CONTENT 而言是**不同**的值。
    conn.execute(
        text(
            "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
            " payload_hash, schema_version)"
            " VALUES (:n, :t, :e, :r, :h, 1)"
        ),
        {"n": str(node_id), "t": entity_type, "e": str(entity_id),
         "r": revision_no, "h": f"h-{str(node_id)[:12]}"},
    )


def _version_tx(conn, ids, version_id, content_id, *, version_no=1,
                title="标题", body="正文", tags="[]", bindings="[]",
                payload="{}", schema_version=1, blueprint_id=None,
                adopted_title_candidate_id=None, generation_context_id=None):
    # 版本行。jsonb 列用 cast(:x as jsonb) 传**文本**，不传 Python 对象 ——
    # 原生 SQL 参数不做过类型推断（WP24 的 _raw 说明已经踩过）。
    conn.execute(
        text(
            "INSERT INTO content_versions (id, content_item_id, version_no,"
            " blueprint_id, adopted_title_candidate_id, generation_context_id,"
            " title, body, tags, bindings, payload, payload_hash, schema_version)"
            " VALUES (:id, :c, :vno, :bp, :ad, :gc, :title, :body,"
            " cast(:tags as jsonb), cast(:bindings as jsonb),"
            " cast(:payload as jsonb), :ph, :sv)"
        ),
        {
            "id": str(version_id), "c": str(content_id), "vno": version_no,
            "bp": str(blueprint_id) if blueprint_id else None,
            "ad": str(adopted_title_candidate_id) if adopted_title_candidate_id else None,
            "gc": str(generation_context_id) if generation_context_id else None,
            "title": title, "body": body, "tags": tags, "bindings": bindings,
            "payload": payload, "ph": f"ph-{str(version_id)[:12]}",
            "sv": schema_version,
        },
    )


def _pair_tx(conn, ids, version_id, content_id, **kw):
    # "节点 + 版本"成对写入（本包最常见的合法底料）。
    _node_tx(conn, ids, version_id, content_id, revision_no=kw.pop("revision_no", 1))
    _version_tx(conn, ids, version_id, content_id, **kw)


def _blueprint_tx(conn, ids, content_id, *, opening_function="开场",
                  context="背景", decision_process="决策过程",
                  usage_scene="使用场景", evidence_beats="证据节拍",
                  limitation="局限", conclusion_strength="CONSERVATIVE",
                  interaction_question="互动问题", paragraph_plan="段落规划"):
    # 9 个字段全是 NOT NULL + 列级 CHECK（8 个文本字段各 2 条 + 取值域 1 条）。
    row = conn.execute(
        text(
            "INSERT INTO narrative_blueprints (id, content_item_id, opening_function,"
            " context, decision_process, usage_scene, evidence_beats, limitation,"
            " conclusion_strength, interaction_question, paragraph_plan)"
            " VALUES (gen_random_uuid(), :c, :of, :ctx, :dp, :us, :eb, :lim,"
            " :cs, :iq, :pp) RETURNING id"
        ),
        {
            "c": str(content_id), "of": opening_function, "ctx": context,
            "dp": decision_process, "us": usage_scene, "eb": evidence_beats,
            "lim": limitation, "cs": conclusion_strength,
            "iq": interaction_question, "pp": paragraph_plan,
        },
    )
    return row.scalar_one()


def _candidate_tx(conn, ids, version_id, *, candidate_index=0, title="候选标题",
                  scores="{}", keywords="[]"):
    row = conn.execute(
        text(
            "INSERT INTO title_candidates (id, content_version_id, candidate_index,"
            " title, scores, keywords)"
            " VALUES (gen_random_uuid(), :v, :i, :t, cast(:s as jsonb),"
            " cast(:k as jsonb)) RETURNING id"
        ),
        {"v": str(version_id), "i": candidate_index, "t": title,
         "s": scores, "k": keywords},
    )
    return row.scalar_one()


def _context_tx(conn, ids, content_id, *, context_id=None, content_version_id=None,
                task_kind="CONTENT_STRUCTURE", idempotency_key=None):
    # ★ `context_id` 可显式指定：**两条环 FK 的正向判据必须能先写出"指着尚不存在
    #   对象的引用"**，再把它指的那个对象补进去。若 id 由 `gen_random_uuid()`
    #   就地生成，引用方就**永远不可能**指到它 —— 于是"正向应当通过"会被
    #   误读成"延迟没生效"（本脚本首轮 F3 即踩此坑，见坑 9）。
    ctx_id = str(context_id) if context_id else str(uuid.uuid4())
    row = conn.execute(
        text(
            "INSERT INTO generation_contexts (id, project_id, content_id,"
            " content_version_id, task_kind, idempotency_key)"
            " VALUES (cast(:id as uuid), :p, :c, :cv, :k, :ik) RETURNING id"
        ),
        {
            "id": ctx_id,
            "p": ids["project"], "c": str(content_id),
            "cv": str(content_version_id) if content_version_id else None,
            "k": task_kind,
            "ik": idempotency_key or f"wp26-probe-{uuid.uuid4().hex[:12]}",
        },
    )
    return row.scalar_one()


# ===========================================================================
# S 节 —— 迁移与结构（期望侧来自 models 元数据，实际侧来自 pg_constraint）
# ===========================================================================

with engine.connect() as conn:
    head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    emit(
        _revision_is_at_least(head, REVISION_INTRODUCED),
        "[S1] 迁移 head 不早于本包引入的版本",
        f"head={head} introduced={REVISION_INTRODUCED}",
    )

    present = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema='public'"
            )
        ).fetchall()
    }
    own = tuple(content_models.OWN_TABLES)
    emit(
        set(own) <= present,
        "[S2] 本包 5 张表全部存在",
        f"缺失={sorted(set(own) - present) or '无'}",
    )

    total_tables = len(present)
    # 下界而不是等号：后续包（乃至 M4）会继续加表（判据纪律第 1 条）。
    emit(total_tables >= 57, "[S3] 库总表数下界", f"{total_tables} (下界 57)")

    cols = {
        r[0]: r[1]
        for r in conn.execute(
            text(
                "SELECT table_name, count(*) FROM information_schema.columns"
                " WHERE table_schema='public' GROUP BY table_name"
            )
        ).fetchall()
    }
    drift_cols = [
        f"{t}:{cols.get(t)}!={n}" for t, n in TABLE_COLUMNS if cols.get(t) != n
    ]
    emit(
        not drift_cols,
        "[S4] 5 张表列数全部符合实测值",
        " ".join(f"{t}={cols.get(t)}" for t, n in TABLE_COLUMNS)
        + (f" 漂移={drift_cols}" if drift_cols else ""),
    )

    def _db_names(table, contype):
        return sorted(
            r[0]
            for r in conn.execute(
                text(
                    "SELECT conname FROM pg_constraint"
                    " WHERE conrelid = cast(:t as regclass) AND contype = :c"
                ),
                {"t": table, "c": contype},
            ).fetchall()
        )

    def _model_names(table, kind):
        t = content_models.Base.metadata.tables[table]
        if kind == "c":
            return sorted(
                c.name
                for c in t.constraints
                if isinstance(c, sa.CheckConstraint)
                and c.name not in LATER_MIGRATION_CHECKS
            )
        if kind == "f":
            return sorted(
                c.name
                for c in t.constraints
                if isinstance(c, sa.ForeignKeyConstraint)
            )
        return sorted(
            c.name for c in t.constraints if isinstance(c, sa.UniqueConstraint)
        )

    # CHECK / FK 的**成员逐条对账**（不是只比总数 —— 换一条也能让总数相等）。
    for table in own:
        want, got = _model_names(table, "c"), _db_names(table, "c")
        emit(
            want == got,
            f"[S5] {table} CHECK 名单与 models 逐条一致",
            f"n={len(got)} 缺失={sorted(set(want) - set(got)) or '无'}"
            f" 多余={sorted(set(got) - set(want)) or '无'}",
        )
    _all_model_checks = {
        c.name
        for t in own
        for c in content_models.Base.metadata.tables[t].constraints
        if isinstance(c, sa.CheckConstraint)
    }
    emit(
        LATER_MIGRATION_CHECKS <= _all_model_checks,
        "[S5b] 排除名单（WP26 之后的迁移所加 CHECK）仍在模型里（排除不空转）",
        f"排除 {len(LATER_MIGRATION_CHECKS)} 条，模型缺失="
        f"{sorted(LATER_MIGRATION_CHECKS - _all_model_checks) or '无'}",
    )
    for table in own:
        want, got = _model_names(table, "f"), _db_names(table, "f")
        emit(
            want == got,
            f"[S6] {table} FK 名单与 models 逐条一致",
            f"n={len(got)} 缺失={sorted(set(want) - set(got)) or '无'}"
            f" 多余={sorted(set(got) - set(want)) or '无'}",
        )

    tot_chk = sum(len(_db_names(t, "c")) for t in own)
    tot_fk = sum(len(_db_names(t, "f")) for t in own)
    emit(tot_chk == 38, "[S7] 本包 CHECK 总数（实测 38）", tot_chk)
    emit(tot_fk == 21, "[S8] 本包 FK 总数（实测 21）", tot_fk)

    # 本包 5 表上的触发器集合必须与声明一致（**不数总个数** —— 其余表还有触发器）。
    declared = set(OWN_TABLE_TRIGGERS)
    actual_trg = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid"
                " WHERE NOT t.tgisinternal AND c.relname = ANY(:ts)"
            ),
            {"ts": list(own)},
        ).fetchall()
    }
    emit(
        declared == actual_trg,
        "[S9] 本包 5 表上的触发器集合与声明一致",
        f"声明={sorted(declared)} 实际={sorted(actual_trg)}",
    )

    # 4 个不可变触发器必须都指向**跨包共用**的 rednote_reject_mutation
    # （挂错函数 = 保护语义不对，而"挂了个触发器"照样能被 S9 判过）。
    imm_fns = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT DISTINCT p.proname FROM pg_trigger t"
                " JOIN pg_class c ON c.oid=t.tgrelid"
                " JOIN pg_proc p ON p.oid=t.tgfoid"
                " WHERE NOT t.tgisinternal AND c.relname = ANY(:ts)"
                "   AND t.tgname LIKE '%\\_immutable'"
            ),
            {"ts": list(own)},
        ).fetchall()
    }
    emit(
        imm_fns == set(content_ddl.immutable_function_names()),
        "[S10] 4 个不可变触发器指向共用函数 rednote_reject_mutation",
        f"实际={sorted(imm_fns)}",
    )

    # 跨包：共用不可变函数必须仍在。★ 用**下界**而不是 ==1 —— 它是全项目共用的，
    # 本包只断言"它还在"（判据纪律第 1 条）。
    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname = 'rednote_reject_mutation'")
    ).scalar_one()
    emit(n >= 1, "[S11] 共用函数 rednote_reject_mutation 仍在", f"{n} (下界 1)")

    # 挂在**共用表** version_nodes 上的那一个：名字 + 函数 + WHEN 收窄。
    # ★ 单独点名（不并进 S9）—— 它不在本包的表上，混在一起会让
    #   "本包表上少了一个"与"共用表上多/少了一个"两种失效同形。
    row = conn.execute(
        text(
            "SELECT c.relname, p.proname, pg_get_triggerdef(t.oid)"
            " FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid"
            " JOIN pg_proc p ON p.oid=t.tgfoid"
            " WHERE NOT t.tgisinternal AND t.tgname = :g"
        ),
        {"g": SHARED_NODE_TRIGGER},
    ).fetchone()
    if row is None:
        emit(False, "[S12] 共用表 version_nodes 上的反向触发器存在", "未找到")
    else:
        emit(
            row[0] == "version_nodes" and row[1] == SHARED_NODE_FUNCTION
            and "AFTER INSERT" in row[2],
            "[S12] 共用表 version_nodes 上的反向触发器存在",
            f"表={row[0]} 函数={row[1]}",
        )

    # ★ 4 条延迟 FK 逐条点名（多一条少一条都要在这里改，改的时刻就是"有人动了环"）。
    deferred = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE contype='f'"
                " AND condeferrable AND condeferred"
                " AND conrelid = ANY(cast(:ts as regclass[]))"
            ),
            {"ts": list(own)},
        ).fetchall()
    }
    emit(
        deferred == set(DEFERRED_FOREIGN_KEYS),
        "[S13] 4 条延迟 FK 逐条点名一致",
        f"缺失={sorted(set(DEFERRED_FOREIGN_KEYS) - deferred) or '无'}"
        f" 多余={sorted(deferred - set(DEFERRED_FOREIGN_KEYS)) or '无'}",
    )

    # ★ 17 条索引逐条点名。期望侧**从 ORM 元数据派生**（铁律 14：手写清单会漏，
    # 而漏了不报错 —— 只是少查一条，那条索引可以静默消失）。
    declared_idx = set(content_ddl.index_names())
    actual_idx = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
                " AND tablename = ANY(:ts)"
            ),
            {"ts": list(own)},
        ).fetchall()
    }
    # 实际侧含 5 条 `pk_*`（主键隐含创建），它们不在 `ddl.index_names()` 里
    # （该函数 docstring 已说明）。这里比的是**本模块显式声明的**那 17 条。
    missing_idx = sorted(declared_idx - actual_idx)
    emit(
        not missing_idx,
        "[S14] 17 条声明索引在库中全部存在",
        f"声明={len(declared_idx)} 缺失={missing_idx or '无'}"
        f"（库内本包 5 表共 {len(actual_idx)} 条，含 5 条 pk_*）",
    )

    # ★ 本包**没有部分索引** —— 与 `consistency_items()` 有意为空同一种形态：
    # 这条断言把"有意为空"与"忘了写 WHERE"区分开。
    partial = sorted(
        r[0]
        for r in conn.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
                " AND tablename = ANY(:ts) AND indexdef LIKE '%WHERE%'"
            ),
            {"ts": list(own)},
        ).fetchall()
    )
    emit(
        partial == [],
        "[S15] 本包 5 表无部分索引（有意形状，非漏写 WHERE）",
        f"实到={partial}",
    )

    # 跨包：另 4 个包挂在同一张 version_nodes 上的配对触发器必须共存
    # （本包只挂自己的第 5 个，不得误删别人的）。
    alive = []
    for name in OTHER_PACK_NODE_TRIGGERS:
        n = conn.execute(
            text(
                "SELECT count(*) FROM pg_trigger"
                " WHERE NOT tgisinternal AND tgname = :n"
            ),
            {"n": name},
        ).scalar_one()
        alive.append(f"{name}={n}")
    emit(
        all(item.endswith("=1") for item in alive),
        "[S16] version_nodes 上另 4 个包的配对触发器共存",
        "; ".join(alive),
    )

    # 跨包：共用表 version_nodes 必须仍在（本包只删挂在自己上面的 1 个触发器）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM information_schema.tables"
            " WHERE table_schema='public' AND table_name='version_nodes'"
        )
    ).scalar_one()
    emit(n == 1, "[S17] 共用表 version_nodes 仍在", n)

    # 跨包：别的包的一致性函数必须存活，用下界（判据纪律第 1 条）。
    # 按包分列计数，这样"某个包整批消失"依然能被看见（合计数会被别包掩盖）。
    for _pkg, _lo in CROSS_PACK_FUNCTIONS:
        n = conn.execute(
            text(
                "SELECT count(*) FROM pg_proc"
                " WHERE proname LIKE 'rednote_check_' || :p || '%'"
            ),
            {"p": _pkg},
        ).scalar_one()
        emit(
            n >= _lo,
            f"[S18] 跨包一致性函数 rednote_check_{_pkg}_* 存活",
            f"{n} (下界 {_lo})",
        )

# ===========================================================================
# T 节 —— 不可变保护：8 条"该拦的拦" + 2 条"该过的过"
# ===========================================================================
# UPDATE 与 DELETE 是触发器里的两个分支，**必须分别证明**
# （只测 UPDATE 会漏掉"DELETE 分支写成了 INSERT"这种形态）。


def _immutable_probe(table, op):
    # 每张不可变表独立造一行、独立连接、独立事务，最后 rollback。
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            ids = _seed(conn)
            cid = uuid.uuid4()
            _item_tx(conn, ids, cid)
            if table == "content_versions":
                vid = uuid.uuid4()
                _pair_tx(conn, ids, vid, cid)
                where = f"id = '{vid}'"
            elif table == "narrative_blueprints":
                bid = _blueprint_tx(conn, ids, cid)
                where = f"id = '{bid}'"
            elif table == "title_candidates":
                vid = uuid.uuid4()
                _pair_tx(conn, ids, vid, cid)
                tid = _candidate_tx(conn, ids, vid)
                where = f"id = '{tid}'"
            else:
                ctx = _context_tx(conn, ids, cid)
                where = f"id = '{ctx}'"
            sql = (
                f"UPDATE {table} SET {_MUTATE_COLUMN[table]} = {_MUTATE_VALUE[table]}"
                f" WHERE {where}"
                if op == "UPDATE"
                else f"DELETE FROM {table} WHERE {where}"
            )
            _expect_check(
                f"[T] 不可变表 {table} 的 {op} 被拦",
                conn,
                lambda: conn.execute(text(sql)),
                sqlstate_expected=IMMUTABLE_VIOLATION_SQLSTATE,
            )
            trans.rollback()
    except Exception as exc:  # noqa: BLE001
        emit(False, f"[T] 不可变表 {table} 的 {op} 被拦",
             f"探针异常 {type(exc).__name__}: {exc}")


#: 各不可变表用于"改一行"的**真实列**与**合法新值**。
#: ⚠️ 必须用真实存在的列：写一个不存在的列会先撞 `42703`（列不存在）——
#: **没走到触发器**却返回了一个"非 23001 的 sqlstate"，判据会以"防线失效"
#: 报红，而真实原因是列名写错（WP25 与 test_billing_wp25.py 都记过这个坑）。
_MUTATE_COLUMN = {
    "content_versions": "title",
    "narrative_blueprints": "context",
    "title_candidates": "title",
    "generation_contexts": "task_kind",
}
_MUTATE_VALUE = {
    "content_versions": "'改过了'",
    "narrative_blueprints": "'改过了'",
    "title_candidates": "'改过了'",
    "generation_contexts": "'CONTENT_COPY'",
}

for _tbl in ("content_versions", "narrative_blueprints", "title_candidates",
             "generation_contexts"):
    for _op in ("UPDATE", "DELETE"):
        _immutable_probe(_tbl, _op)

# 对照：`content_items` 是**可变**身份行，UPDATE 与 DELETE 都必须通过。
# 若这两条被拦，说明有人把不可变触发器挂错表了 ——
# 而"把 5 张表全锁上"的实现也能让上面 8 条全绿，那样系统完全不可用
# （`current_version_id` 投影无法前进、`lock_version`（ETag）无法递增）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        cid = uuid.uuid4()
        _item_tx(conn, ids, cid)
        conn.execute(
            text("UPDATE content_items SET state = 'FAILED' WHERE id = :c"),
            {"c": str(cid)},
        )
        emit(True, "[T] 对照：可变表 content_items 的 UPDATE 能通过",
             "写入成功（符合预期）")
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[T] 对照：可变表 content_items 的 UPDATE 能通过",
         f"探针异常 {type(exc).__name__}: {exc}")

try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        cid = uuid.uuid4()
        _item_tx(conn, ids, cid)
        conn.execute(
            text("DELETE FROM content_items WHERE id = :c"), {"c": str(cid)}
        )
        emit(True, "[T] 对照：可变表 content_items 的 DELETE 能通过",
             "写入成功（符合预期）")
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[T] 对照：可变表 content_items 的 DELETE 能通过",
         f"探针异常 {type(exc).__name__}: {exc}")

# ===========================================================================
# C 节 —— 一致性触发器（OP-88 正向 3 + 反向 1 + 收窄对照 1 + 三条跨表链接）
# ===========================================================================

# C1 OP-88 判据 1：`content_versions.id` 没有对应的 `version_nodes` 行。
# ★ 实际报的是 `23503`（被**延迟 FK** 先拦），不是触发器的 23514 ——
#   在 SET CONSTRAINTS ALL IMMEDIATE 时 FK 检查先跑，触发器那句 RAISE 没机会执行。
#   按"触发器 23514"写这条会恒红；断言 23503 并**点名**那条 FK，
#   这样将来有人把这条 FK 改成非延迟或去掉时会当场看见。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        cid = uuid.uuid4()
        vid = uuid.uuid4()
        _item_tx(conn, ids, cid)
        _version_tx(conn, ids, vid, cid)
        _expect_check(
            "[C1] OP-88 判据 1（节点不存在）报 23503 且点名延迟 FK",
            conn,
            lambda: _force(conn),
            sqlstate_expected="23503",
            constraint="fk_content_versions_id_version_nodes",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C1] OP-88 判据 1（节点不存在）", f"探针异常 {type(exc).__name__}: {exc}")

# C2 OP-88 判据 2：节点存在，但 `entity_type` 不是 CONTENT。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        cid = uuid.uuid4()
        vid = uuid.uuid4()
        _item_tx(conn, ids, cid)
        _node_tx(conn, ids, vid, cid, entity_type="BRIEF")
        _version_tx(conn, ids, vid, cid)
        _expect_sqlstate(
            "[C2] OP-88 判据 2（entity_type != CONTENT）报 23514",
            conn,
            lambda: _force(conn),
            sqlstate_expected=CONSISTENCY_SQLSTATE,
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C2] OP-88 判据 2（entity_type != CONTENT）",
         f"探针异常 {type(exc).__name__}: {exc}")

# C3 OP-88 判据 3：节点存在且 entity_type 正确，但 `entity_id` 与本行的
# `content_item_id` 不一致（"给 A 内容注册了一个声称属于 B 内容的版本节点"）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        cid = uuid.uuid4()
        other = uuid.uuid4()
        vid = uuid.uuid4()
        _item_tx(conn, ids, cid)
        _node_tx(conn, ids, vid, other)
        _version_tx(conn, ids, vid, cid)
        _expect_sqlstate(
            "[C3] OP-88 判据 3（entity_id 与 content_item_id 不一致）报 23514",
            conn,
            lambda: _force(conn),
            sqlstate_expected=CONSISTENCY_SQLSTATE,
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C3] OP-88 判据 3（entity_id 不一致）",
         f"探针异常 {type(exc).__name__}: {exc}")

# C4 反向判据：内容**真实存在**、但节点没有配对的版本行 ⇒ 23514。
# ★ 必须先造**真实的** content_items 行 —— 收窄后的守卫只对"真实存在的内容身份"
#   强制；随机 uuid4 会走**放行**分支（见坑 3 与 C8 对照）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        cid = uuid.uuid4()
        _item_tx(conn, ids, cid)
        _node_tx(conn, ids, uuid.uuid4(), cid, revision_no=99)
        _expect_sqlstate(
            "[C4] 反向：孤立 CONTENT 节点（内容存在）报 23514",
            conn,
            lambda: _force(conn),
            sqlstate_expected=CONSISTENCY_SQLSTATE,
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C4] 反向：孤立 CONTENT 节点（内容存在）",
         f"探针异常 {type(exc).__name__}: {exc}")

# C5 收窄对照：`entity_id` 指向**不存在**的内容 ⇒ **放行**。
# ★ 没有这条，C4 在一个"永远 RAISE"的实现上也会全绿 —— 而那样的实现会让
#   WP11 的通用用例（CONTENT 是它 8 类样本实体之一）直接失败，即"别的包的进度
#   被本包阻塞"（本仓实测：不收窄时 WP11 既有 2 例报红）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        _node_tx(conn, ids, uuid.uuid4(), uuid.uuid4(), revision_no=99)
        _expect_ok(
            "[C5] 对照：孤立 CONTENT 节点（内容不存在）放行（收窄行为）",
            conn,
            lambda: _force(conn),
            detail="放行（符合预期，收窄守卫生效）",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C5] 对照：孤立 CONTENT 节点（内容不存在）放行",
         f"探针异常 {type(exc).__name__}: {exc}")

# C6 合法底料必须**能写进去** —— 后面所有"被拦"的对照基准。
#   缺了它，一个"见谁都拒"的实现能让 C1~C4 全绿。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        cid = uuid.uuid4()
        vid = uuid.uuid4()
        _item_tx(conn, ids, cid)
        _pair_tx(conn, ids, vid, cid)
        _expect_ok(
            "[C6] 对照：合法「内容 + 节点 + 版本」能通过（强制全部延迟约束）",
            conn,
            lambda: _force(conn),
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C6] 对照：合法底料能通过", f"探针异常 {type(exc).__name__}: {exc}")

# C7 判据 3.1：蓝图必须属于**同一条内容**（跨内容复用不得通过）。
#   FK 只保证"蓝图存在"，不保证"它属于同一条内容" ⇒ 这条只能由触发器管。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        cid_a = uuid.uuid4()
        cid_b = uuid.uuid4()
        vid_a = uuid.uuid4()
        _item_tx(conn, ids, cid_a)
        _item_tx(conn, ids, cid_b)
        bp_b = _blueprint_tx(conn, ids, cid_b)
        _node_tx(conn, ids, vid_a, cid_a)
        _version_tx(conn, ids, vid_a, cid_a, blueprint_id=bp_b)
        _expect_sqlstate(
            "[C7] 判据 3.1（蓝图跨内容）报 23514",
            conn,
            lambda: _force(conn),
            sqlstate_expected=CONSISTENCY_SQLSTATE,
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C7] 判据 3.1（蓝图跨内容）", f"探针异常 {type(exc).__name__}: {exc}")

# C8 判据 3.2：采纳的标题候选必须属于**同一条内容**。
#   判据**穿透两层**：候选 → 它所属的版本 → 那条版本的内容身份。
#   不穿透就退化成"候选存在"（= FK 已保证的事，恒真）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        cid_a = uuid.uuid4()
        cid_b = uuid.uuid4()
        vid_a = uuid.uuid4()
        vid_b = uuid.uuid4()
        _item_tx(conn, ids, cid_a)
        _item_tx(conn, ids, cid_b)
        _pair_tx(conn, ids, vid_b, cid_b)
        tc_b = _candidate_tx(conn, ids, vid_b)
        _node_tx(conn, ids, vid_a, cid_a, revision_no=1)
        vid_a2 = uuid.uuid4()
        _node_tx(conn, ids, vid_a2, cid_a, revision_no=2)
        _version_tx(conn, ids, vid_a2, cid_a, version_no=2,
                    adopted_title_candidate_id=tc_b)
        _expect_sqlstate(
            "[C8] 判据 3.2（采纳候选跨内容，穿透两层）报 23514",
            conn,
            lambda: _force(conn),
            sqlstate_expected=CONSISTENCY_SQLSTATE,
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C8] 判据 3.2（采纳候选跨内容）",
         f"探针异常 {type(exc).__name__}: {exc}")

# C9 判据 3.3：`generation_contexts` 的两列必须指向**同一条内容**。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        cid_a = uuid.uuid4()
        cid_b = uuid.uuid4()
        vid_b = uuid.uuid4()
        _item_tx(conn, ids, cid_a)
        _item_tx(conn, ids, cid_b)
        _pair_tx(conn, ids, vid_b, cid_b)
        _context_tx(conn, ids, cid_a, content_version_id=vid_b,
                    task_kind="CONTENT_COPY")
        _expect_sqlstate(
            "[C9] 判据 3.3（上下文版本跨内容）报 23514",
            conn,
            lambda: _force(conn),
            sqlstate_expected=CONSISTENCY_SQLSTATE,
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C9] 判据 3.3（上下文版本跨内容）",
         f"探针异常 {type(exc).__name__}: {exc}")

# C10 对照：`content_version_id IS NULL`（结构调用）必须**放行** ——
#   那是合法常态（`CONTENT_STRUCTURE` 发生时版本还没有），不是漏判。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        cid = uuid.uuid4()
        _item_tx(conn, ids, cid)
        _context_tx(conn, ids, cid, content_version_id=None,
                    task_kind="CONTENT_STRUCTURE")
        _expect_ok(
            "[C10] 对照：结构调用的上下文（无版本）放行",
            conn,
            lambda: _force(conn),
            detail="放行（符合预期：该 NULL 是合法常态）",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C10] 对照：结构调用的上下文（无版本）放行",
         f"探针异常 {type(exc).__name__}: {exc}")

# ===========================================================================
# F 节 —— 两条环 FK 的"正向能过 / 反向能拦" + 两条非延迟 FK 的当场拒
# ===========================================================================

# F1 环 1 正向：先插身份行（`current_version_id` 指向**尚不存在**的版本），
#    再插版本行 —— 同事务，提交期检查 ⇒ 必须通过。
#    这是"延迟这条 FK 的**唯一**理由"，缺了它，那条延迟就是无根据的放宽。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        cid = uuid.uuid4()
        vid = uuid.uuid4()
        _item_tx(conn, ids, cid, current_version_id=vid)
        _pair_tx(conn, ids, vid, cid)
        _expect_ok(
            "[F1] 环 1 正向：先身份（悬空 current_version_id）后版本，同事务通过",
            conn,
            lambda: _force(conn),
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[F1] 环 1 正向", f"探针异常 {type(exc).__name__}: {exc}")

# F2 环 1 反向：`current_version_id` 指向**永不出现**的版本 ⇒ 23503，点名那条 FK。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        cid = uuid.uuid4()
        _item_tx(conn, ids, cid, current_version_id=uuid.uuid4())
        _expect_check(
            "[F2] 环 1 反向：current_version_id 悬空报 23503",
            conn,
            lambda: _force(conn),
            sqlstate_expected="23503",
            constraint="fk_content_items_current_version_id_content_versions",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[F2] 环 1 反向", f"探针异常 {type(exc).__name__}: {exc}")

# F3 环 2 正向：先插版本（`generation_context_id` 指向**尚不存在**的上下文），
#    再插上下文 —— 同事务通过。
#    ★ 这个环是**编码期实测发现的**（第一版两条都是 IMMEDIATE ⇒ 两张表都不可变
#      ⇒ `TASK_CONTENT_COPY` 的主路径根本走不通）。这条判据锁住那个修复。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        cid = uuid.uuid4()
        vid = uuid.uuid4()
        ctx = uuid.uuid4()
        _item_tx(conn, ids, cid)
        _node_tx(conn, ids, vid, cid)
        _version_tx(conn, ids, vid, cid, generation_context_id=ctx)
        _context_tx(conn, ids, cid, context_id=ctx, content_version_id=vid,
                    task_kind="CONTENT_COPY")
        _expect_ok(
            "[F3] 环 2 正向：先版本（悬空 generation_context_id）后上下文，同事务通过",
            conn,
            lambda: _force(conn),
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[F3] 环 2 正向", f"探针异常 {type(exc).__name__}: {exc}")

# F4 环 2 反向：`generation_context_id` 指向不存在的上下文 ⇒ 23503，点名那条 FK。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        cid = uuid.uuid4()
        vid = uuid.uuid4()
        _item_tx(conn, ids, cid)
        _node_tx(conn, ids, vid, cid)
        _version_tx(conn, ids, vid, cid, generation_context_id=uuid.uuid4())
        _expect_check(
            "[F4] 环 2 反向：generation_context_id 悬空报 23503",
            conn,
            lambda: _force(conn),
            sqlstate_expected="23503",
            constraint="fk_content_versions_generation_context_id_generation_contexts",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[F4] 环 2 反向", f"探针异常 {type(exc).__name__}: {exc}")

# F5 负向：`content_versions.content_item_id` **必须不延迟** ⇒ 坏值在 INSERT
#    那一刻就撞 23503（连 SET CONSTRAINTS 都不用）。
#    ★ 这条是"我们没有过度延迟"的**正向证据**：延迟它只是把一条真实错误
#      从"当场拒绝"推迟到"COMMIT 时才拒"，没有任何收益，只有损失。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        vid = uuid.uuid4()
        # 造一条**真实存在**的内容 + 节点（让错误只可能来自 content_item_id）
        cid_real = uuid.uuid4()
        _item_tx(conn, ids, cid_real)
        _node_tx(conn, ids, vid, cid_real)
        _expect_check(
            "[F5] 非延迟：content_versions.content_item_id 坏值当场报 23503",
            conn,
            lambda: _version_tx(conn, ids, vid, uuid.uuid4()),
            sqlstate_expected="23503",
            constraint="fk_content_versions_content_item_id_content_items",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[F5] 非延迟：content_item_id 坏值当场拒",
         f"探针异常 {type(exc).__name__}: {exc}")

# F6 负向：`title_candidates.content_version_id` **必须不延迟** ⇒ 当场 23503。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        _expect_check(
            "[F6] 非延迟：title_candidates.content_version_id 坏值当场报 23503",
            conn,
            lambda: _candidate_tx(conn, ids, uuid.uuid4()),
            sqlstate_expected="23503",
            constraint="fk_title_candidates_content_version_id_content_versions",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[F6] 非延迟：content_version_id 坏值当场拒",
         f"探针异常 {type(exc).__name__}: {exc}")

# ===========================================================================
# D 节 —— 形状约束（每条配"合法值必须通过"的对照，防"见谁都拒"）
# ===========================================================================

def _shape_probe(label, constraint, mutate_fn, sqlstate_expected="23514"):
    # 铺底统一由本函数做（内容行 `cid` 一造好就交给 `mutate_fn`）；
    # `mutate_fn` 只负责"往这个 cid 上写一条该被拒的行"，不重复造底料。
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            ids = _seed(conn)
            cid = uuid.uuid4()
            _item_tx(conn, ids, cid)
            _expect_check(
                label, conn, lambda: mutate_fn(conn, ids, cid),
                sqlstate_expected=sqlstate_expected, constraint=constraint,
            )
            trans.rollback()
    except Exception as exc:  # noqa: BLE001
        emit(False, label, f"探针异常 {type(exc).__name__}: {exc}")


def _bad_version_no(conn, ids, cid):
    vid = uuid.uuid4()
    _node_tx(conn, ids, vid, cid)
    _version_tx(conn, ids, vid, cid, version_no=0)


def _bad_title_empty(conn, ids, cid):
    vid = uuid.uuid4()
    _node_tx(conn, ids, vid, cid)
    _version_tx(conn, ids, vid, cid, title="")


def _bad_tags_not_array(conn, ids, cid):
    vid = uuid.uuid4()
    _node_tx(conn, ids, vid, cid)
    _version_tx(conn, ids, vid, cid, tags="{}")


def _bad_item_state(conn, ids, cid2):
    _item_tx(conn, ids, uuid.uuid4(), state="NOPE")


def _bad_conclusion_strength(conn, ids, cid):
    _blueprint_tx(conn, ids, cid, conclusion_strength="NOPE")


def _bad_blueprint_context_empty(conn, ids, cid):
    _blueprint_tx(conn, ids, cid, context="")


def _bad_candidate_index(conn, ids, cid):
    vid = uuid.uuid4()
    _pair_tx(conn, ids, vid, cid)
    _candidate_tx(conn, ids, vid, candidate_index=-1)


def _bad_candidate_scores(conn, ids, cid):
    vid = uuid.uuid4()
    _pair_tx(conn, ids, vid, cid)
    _candidate_tx(conn, ids, vid, scores="[]")


def _bad_task_kind(conn, ids, cid):
    _context_tx(conn, ids, cid, task_kind="NOPE")


_shape_probe("[D1] content_versions.version_no = 0 被拒",
             "ck_content_versions_version_no_positive", _bad_version_no)
_shape_probe("[D2] content_versions.title 空串被拒",
             "ck_content_versions_title_not_empty", _bad_title_empty)
_shape_probe("[D3] content_versions.tags 非数组被拒",
             "ck_content_versions_tags_is_array", _bad_tags_not_array)
_shape_probe("[D4] content_items.state 非法取值被拒",
             "ck_content_items_state_valid", _bad_item_state)
_shape_probe("[D5] narrative_blueprints.conclusion_strength 非法取值被拒",
             "ck_narrative_blueprints_conclusion_strength_valid",
             _bad_conclusion_strength)
_shape_probe("[D6] narrative_blueprints.context 空串被拒",
             "ck_narrative_blueprints_context_not_empty",
             _bad_blueprint_context_empty)
_shape_probe("[D7] title_candidates.candidate_index = -1 被拒",
             "ck_title_candidates_candidate_index_non_negative",
             _bad_candidate_index)
_shape_probe("[D8] title_candidates.scores 非对象被拒",
             "ck_title_candidates_scores_is_object",
             _bad_candidate_scores)
_shape_probe("[D9] generation_contexts.task_kind 非法取值被拒",
             "ck_generation_contexts_task_kind_valid", _bad_task_kind)

# D10 对照：**全部合法值**必须通过 —— 一个"见谁都拒"的 CHECK 能让 D1~D9 全绿，
#     而系统完全不可用。这条把正面钉住（与 T 节的"该过的过"同一形态）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        cid = uuid.uuid4()
        _item_tx(conn, ids, cid, state="GENERATED")
        vid = uuid.uuid4()
        _node_tx(conn, ids, vid, cid)
        _version_tx(conn, ids, vid, cid, version_no=1, title="标题", body="正文",
                    tags="[]", bindings="[]", payload="{}")
        bpid = _blueprint_tx(conn, ids, cid, conclusion_strength="DIRECT")
        _candidate_tx(conn, ids, vid, candidate_index=0, title="候选",
                      scores="{}", keywords="[]")
        _context_tx(conn, ids, cid, task_kind="CONTENT_COPY")
        _expect_ok(
            "[D10] 对照：全部合法行（5 表齐写）能通过",
            conn,
            lambda: _force(conn),
            detail=f"写入成功（内容={str(cid)[:8]} 版本={str(vid)[:8]}"
                   f" 蓝图={str(bpid)[:8]}）",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D10] 对照：全部合法行能通过", f"探针异常 {type(exc).__name__}: {exc}")

# ===========================================================================
# P 节 —— 纯函数层（落不到数据库层的那几件事）
# ===========================================================================
# ⚠️ 纯函数探针必须对着**真实 API** 写，不能凭印象（WP24 的坑 7：四处记错
# 名字，症状是**一条 ImportError 打包了全部判据**）。
# 因此 except 一律把异常类型与消息原样写进 RESULT 行。

try:
    _t = content_types

    # P1 `calls_for` 未知 task_kind 必须抛（返回 0 会让预占金额为 0，
    #    报错点是"金额必须为正"，与真正的根因隔了两层）。
    _raised = None
    try:
        _t.calls_for("NOPE", 1)
    except ValueError as exc:
        _raised = str(exc)[:40]
    emit(_raised is not None, "[P1] calls_for 未知 task_kind 抛 ValueError", _raised)

    # P1- 反向（★ 证明上一条能区分两种实现）：合法 kind 必须返回正确值。
    emit(
        _t.calls_for(_t.TASK_CONTENT_COPY, 3) == 3
        and _t.calls_for(_t.TASK_CONTENT_STRUCTURE, 3) == 3,
        "[P1-] 反向：合法 kind 返回 content_count × 1（证明 P1 能区分）",
        f"COPY×3={_t.calls_for(_t.TASK_CONTENT_COPY, 3)}"
        f" STRUCTURE×3={_t.calls_for(_t.TASK_CONTENT_STRUCTURE, 3)}",
    )

    # P2 负数 content_count 必须抛（不返回负数次调用 —— 负数预占会**反向**
    #    增加可用额度）。
    _neg = None
    try:
        _t.calls_for(_t.TASK_CONTENT_COPY, -1)
    except ValueError as exc:
        _neg = str(exc)[:40]
    emit(_neg is not None, "[P2] calls_for 负数条数抛 ValueError", _neg)

    # P3 纯函数 0 条必须返回 0（合法边界，不是错误）。
    emit(_t.calls_for(_t.TASK_CONTENT_COPY, 0) == 0,
         "[P3] calls_for 0 条返回 0（合法边界）",
         _t.calls_for(_t.TASK_CONTENT_COPY, 0))

    # P4 `FULL` 的顺序有意义：结构必须在文案**之前**（前者产物是后者输入）。
    emit(
        _t.task_kinds_for_scope(_t.SCOPE_FULL)
        == (_t.TASK_CONTENT_STRUCTURE, _t.TASK_CONTENT_COPY),
        "[P4] task_kinds_for_scope(FULL) 顺序 = (STRUCTURE, COPY)",
        _t.task_kinds_for_scope(_t.SCOPE_FULL),
    )

    # P5 未知 scope 必须抛（fail-closed，不回落默认值）。
    _bad_scope = None
    try:
        _t.task_kinds_for_scope("NOPE")
    except ValueError as exc:
        _bad_scope = str(exc)[:40]
    emit(_bad_scope is not None, "[P5] task_kinds_for_scope 未知 scope 抛 ValueError",
         _bad_scope)

    # P6 `normalize_scope` 把 TAGS 归一为 COPY（"仅标签"不是新的取值范围，
    #    而是 COPY 的一种说法）。
    emit(_t.normalize_scope(_t.SCOPE_TAGS) == _t.SCOPE_COPY,
         "[P6] normalize_scope(TAGS) == COPY",
         f"{_t.SCOPE_TAGS} -> {_t.normalize_scope(_t.SCOPE_TAGS)}")

    # P7 未知 scope 必须抛。
    _bad_norm = None
    try:
        _t.normalize_scope("NOPE")
    except ValueError as exc:
        _bad_norm = str(exc)[:40]
    emit(_bad_norm is not None, "[P7] normalize_scope 未知 scope 抛 ValueError", _bad_norm)

    # P8 ★ 构造即归一：`GenerationRequest(scope=TAGS)` 的 `.scope` 必须是 COPY。
    #   不归一时 `as_payload()` 写 TAGS、`from_payload()` 读回 COPY ——
    #   同一个值在"落库的张量"与"执行的依据"两处**不相等**（roundtrip eq = False）。
    _req = _t.GenerationRequest(
        scope=_t.SCOPE_TAGS, batch_kind="TRIAL",
        model_profile_id=uuid.uuid4(),
    )
    _rt = _t.GenerationRequest.from_payload(_req.as_payload())
    emit(
        _req.scope == _t.SCOPE_COPY and _rt == _req,
        "[P8] GenerationRequest 构造即归一（TAGS→COPY，roundtrip 恒等）",
        f"scope={_req.scope} roundtrip_eq={_rt == _req}",
    )

    # P9 `from_payload` 缺键必须抛（不给默认值 —— 每个默认值都会把一次真实的
    #    装配故障变成一次静默的错误行为）。
    _missing = None
    try:
        _t.GenerationRequest.from_payload({"scope": "COPY"})
    except (ValueError, KeyError) as exc:
        _missing = f"{type(exc).__name__}: {str(exc)[:40]}"
    emit(_missing is not None, "[P9] from_payload 缺键抛错（不给默认值）", _missing)

    # P10 `ddl.index_names()` 必须 17 条且**唯一**（从 ORM 派生，铁律 14）。
    _idx = content_ddl.index_names()
    emit(
        len(_idx) == 17 and len(set(_idx)) == 17,
        "[P10] ddl.index_names() 17 条且唯一",
        f"n={len(_idx)} unique={len(set(_idx))}",
    )

    # P11 `ddl.trigger_names()` == 7（4 不可变 + 3 一致性），且与分项拼接一致。
    _trg = content_ddl.trigger_names()
    emit(
        len(_trg) == 7
        and set(_trg) == set(content_ddl.immutable_trigger_names())
        | set(content_ddl.consistency_trigger_names()),
        "[P11] ddl.trigger_names() == 7（4 不可变 + 3 一致性）",
        f"n={len(_trg)}",
    )

    # P12 ★ `consistency_items()` 恒为 3 项、表名正确、且与函数名**等长**。
    #   数错一个的后果是 `downgrade` 漏删一个函数（残留对象会挡住后续迁移重建）。
    _items = content_ddl.consistency_items()
    emit(
        [i[0] for i in _items] == ["content_versions", "version_nodes",
                                   "generation_contexts"]
        and len(_items) == len(content_ddl.consistency_function_names()),
        "[P12] consistency_items() 3 项 / 表名正确 / 与函数名等长",
        f"tables={[i[0] for i in _items]}"
        f" fns={len(content_ddl.consistency_function_names())}",
    )

    # P13 ★ ISS-79 的**真正**判据：`consistency_items()` 的 `drop_function`
    #   只能指向本包那 3 个函数，**不得**指向跨包共用的
    #   `rednote_reject_mutation()` —— 删了会撞依赖错误，并让其它 8 个包的
    #   不可变保护一起失效。
    #   ⚠️ 首轮写成了"items 的表名不在 IMMUTABLE_TABLES 里"，这是**错的判据**：
    #      `content_versions` / `generation_contexts` 同时是"不可变表"与
    #      "有一致性触发器"，两者是**正交**的两套，同表并存是本包的合法形状
    #      （见 ddl.py `all_trigger_ddl()`：先建 4 个不可变，再 extend 3 个一致性）。
    #      真正的风险不在"表名重合"，而在"drop 语句误删共用函数"。
    _drop_fns = " ".join(str(_it[4]) for _it in _items)
    _own_fns = content_ddl.consistency_function_names()
    emit(
        "rednote_reject_mutation" not in _drop_fns
        and all(_fn in _drop_fns for _fn in _own_fns),
        "[P13] consistency_items() 的 drop_function 只删本包 3 个函数"
        "（不含共用 rednote_reject_mutation，ISS-79）",
        f"命中本包函数={sum(_fn in _drop_fns for _fn in _own_fns)}/{len(_own_fns)}"
        f" 误含共用={'rednote_reject_mutation' in _drop_fns}",
    )

    # P14 `IMMUTABLE_TABLES` == 4 张且与 OWN_TABLES 的关系正确
    #   （OWN = content_items + 4 不可变）。
    emit(
        tuple(content_models.IMMUTABLE_TABLES)
        == ("content_versions", "narrative_blueprints", "title_candidates",
            "generation_contexts")
        and set(content_models.OWN_TABLES)
        == set(content_models.IMMUTABLE_TABLES) | {"content_items"},
        "[P14] IMMUTABLE_TABLES == 4 张 / OWN_TABLES == 它 + content_items",
        f"imm={len(content_models.IMMUTABLE_TABLES)}"
        f" own={len(content_models.OWN_TABLES)}",
    )

    # P15 无蓝图时**不含** blueprint 键（结构调用时蓝图还不存在；给它上一轮的
    #     旧蓝图会让结构与文案基于不同轮次工作，且不报错）。
    _v_no_bp = content_service.generation_prompt_values(
        content_id=uuid.uuid4(), project_id=uuid.uuid4(), batch_kind="TRIAL",
    )
    emit(
        "blueprint" not in _v_no_bp and len(_v_no_bp) == 3,
        "[P15] generation_prompt_values 无蓝图时不含 blueprint 键",
        f"keys={sorted(_v_no_bp)}",
    )

    # P16 有蓝图时必须**键序无关**（同一个蓝图两次调用序列化成同一个串，
    #     否则 context_digest 不稳定 ⇒ 幂等命中判不出来）。
    _v1 = content_service.generation_prompt_values(
        content_id=uuid.uuid4(), project_id=uuid.uuid4(), batch_kind="TRIAL",
        blueprint={"a": "1", "b": "2", "c": "3"},
    )
    _v2 = content_service.generation_prompt_values(
        content_id=uuid.uuid4(), project_id=uuid.uuid4(), batch_kind="TRIAL",
        blueprint={"c": "3", "a": "1", "b": "2"},
    )
    emit(
        _v1["blueprint"] == _v2["blueprint"] and _v1["blueprint"].startswith("{"),
        "[P16] generation_prompt_values 蓝图序列化与键序无关（sort_keys）",
        _v1["blueprint"],
    )
except Exception as exc:  # noqa: BLE001
    emit(False, "[P] 纯函数层（取值域 / 归一 / DDL 派生 / 提示词值）",
         f"探针异常 {type(exc).__name__}: {exc}")

# ===========================================================================
# K 节 —— 契约
# ===========================================================================
try:
    _spec = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    _paths = _spec["paths"]
    _missing_paths = sorted(set(OWN_PATHS) - set(_paths))
    emit(
        not _missing_paths,
        "[K1] 契约含本包全部 6 个路径",
        f"缺失={_missing_paths or '无'}",
    )
    _ops = 0
    for _p in OWN_PATHS:
        _item = _paths.get(_p, {})
        _ops += sum(
            1 for _m in _item if _m.lower() in ("get", "post", "put", "patch", "delete")
        )
    emit(_ops == 6, "[K2] 本包契约操作数 == 6", f"{_ops}（6 paths）")
    _all_ops = sum(
        1
        for _p, _item in _paths.items()
        for _m in _item
        if _m.lower() in ("get", "post", "put", "patch", "delete")
    )
    _schemas = len(_spec.get("components", {}).get("schemas", {}))
    # 下界而不是等号（判据纪律第 1 条）：后续包会继续加路径。
    emit(
        len(_paths) >= 71 and _all_ops >= 88,
        "[K3] 全库契约规模下界",
        f"paths={len(_paths)}(>=71) ops={_all_ops}(>=88) schemas={_schemas}",
    )
except Exception as exc:  # noqa: BLE001
    emit(False, "[K] 契约核对", f"探针异常 {type(exc).__name__}: {exc}")

# ===========================================================================
# Z 节 —— 零残留（不可变表一旦 commit 就删不掉，故必须核对）
# ===========================================================================
with engine.connect() as conn:
    for _t in content_models.OWN_TABLES:
        n = conn.execute(text(f"SELECT count(*) FROM {_t}")).scalar_one()
        emit(n == 0, f"[Z] 零残留 {_t}", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM users WHERE issuer = :i"), {"i": PROBE_ISSUER}
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 探针用户", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM projects WHERE name = 'WP26 探针项目'")
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 探针项目", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM brands WHERE name LIKE 'WP26 品牌%'")
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 探针品牌", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM products WHERE name LIKE 'WP26 产品%'")
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 探针产品", f"{n} 行")
    # ★ 跨包：探针**不得**在共用表 version_nodes 上留行（本包里它是 FK 目标）。
    n = conn.execute(
        text("SELECT count(*) FROM version_nodes WHERE payload_hash LIKE 'h-%'")
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 共用表 version_nodes 无探针节点（h-*）", f"{n} 行")

print(f"RESULT_EMITTED (期望 {EXPECTED_CHECKS} 条)")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def collect_wp26() -> tuple[Path, bool]:
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
        "# WP26 文案结构生成与标题候选：5 张表 / 38 CHECK / 21 FK / 17 索引"
        " / 4 条延迟 FK / 7 个触发器（6 挂本包表 + 1 挂共用表 version_nodes）"
        " / 库内活性验证 + 纯函数判据 / 契约",
        f"# 生成时间: {_now()}",
        "# 目标库: rednote_verify_wp26（收口时由迁移从 bootstrap 建出，非测试夹具建表）",
        "# 命令: cd services/backend && python -c '<information_schema + 试写 + 试插 + 纯函数>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp26-content-generation.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # **条数不足也算失败**（ISS-43：崩溃伪装成"全过"）。
    # **条数变多也算失败**（ISS-153：门禁只拦变少、不拦变多 ⇒ 常量脱钩后照旧印"期望 N 条"）。
    complete = len(checks) >= EXPECTED_CHECKS
    exact = len(checks) == EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete and exact)


def main() -> int:
    path, ok = collect_wp26()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())