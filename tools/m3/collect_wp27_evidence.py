#!/usr/bin/env python
"""WP27 证据采集脚本。

产出的文件（落在 `evidence/m3/`）：

| 文件 | 内容 |
|---|---|
| `wp27-media-pipeline.log` | 5 张表结构 / 40 CHECK / 24 FK / 22 索引 / 8 触发器（7 挂本包表 + 1 挂**共用表** `version_nodes`）/ 4 条延迟 FK / 库内活性验证（不可变 8 条 + 一致性 8 条 + 环 FK 4 条）+ 纯函数判据 + 契约 |

## 本包的结构性异同（与前几个包比）

- 与 WP26 相同：`downgrade` 只删自己的函数（`consistency_items()` 有意只含 4 项），
  共用函数 `rednote_reject_mutation()` 一个都不许删；**1 个触发器挂在共用表
  `version_nodes` 上**（`trg_version_nodes_media_paired`，是第 6 个 `*_paired`）。
- 与 WP26 不同（**本包独有**）：
  1. **表数是 5 张**（`media_items` / `media_versions` / `reference_assets` /
     `media_renditions` / `media_generation_contexts`），与本包之外的
     `version_nodes` / `jobs` / `file_objects` 全部是**引用**关系。
  2. **4 张不可变表**（`media_versions` / `reference_assets` / `media_renditions` /
     `media_generation_contexts`），因此"该拦的拦"从 2×2 变 4×2 = **8 条**；
     `media_items` 是**可变**身份行（`current_version_id` 投影 + `lock_version`），
     它必须**能改** —— 缺这条，一个把所有表都锁上的实现能让 8 条全绿而系统完全不可用。
  3. **`reference_bindings` 是 JSONB，没有任何 FK** ⇒ 两条绑定一致性判据
     （`BINDING_LINK_FUNCTION`）**不是冗余，是唯一防线**。这与 WP26 的
     3.1/3.2 恰好相反（那里 FK 已保证存在、`IF NOT FOUND` 实测不可达）。
     ⚠️ 把本包的这一条写成"防御性分支"是**错的**（ISS-95 的反向形态）。
  4. **4 条延迟 FK**（环 FK 3 条 + `fk_media_versions_id_version_nodes` 1 条）。
     环：`media_items.current_version_id ↔ media_versions`、
     `media_versions.generation_context_id ↔ media_generation_contexts`
     （WP26 是 `generation_contexts.content_version_id`，本包是反向的一侧）。
  5. **`media_items.job_id` + `uq_media_items_job_id`**（WP17 的"任务 → 领域行"
     寻址接缝；WP17 用普通索引，本包用**唯一**索引 —— 见 `c4d9f2a17e35` §二）。
  6. **本包无部分索引**（有意形状）。`ddl.index_names()` 的 22 条里
     17 条普通 + 5 条唯一，`indexdef` 全部不含 `WHERE`。

## 覆盖范围由**磁盘**决定，不由手写名单决定（判据纪律第 14 条）

40 条 CHECK / 24 条 FK **全部**由两侧机械枚举后**逐条对账**：

- 期望侧：`media_models.Base.metadata.tables[t]` 的 `sa.CheckConstraint` /
  `sa.ForeignKeyConstraint` 集合（来自 `models.py`）；
- 实际侧：`pg_constraint` 里该表的同名集合。

22 条索引同理：期望侧 `media_ddl.index_names()`（**从 ORM `__table__.indexes` 派生**），
实际侧 `pg_indexes`。因此"少一条"或"换一条"都会报红并打印差集 ——
总数相等但成员不同时，只比总数的判据是**恒绿**的（判据纪律第 4 条）。

## 探针数据的处置

每条验证跑在**独立连接**里，最后**无条件 `rollback()`** → **零残留**。
理由：本包 5 张表里 4 张是**不可变表**（触发器拦 `DELETE`），一旦 commit 就删不掉
（WP18 的 ISS-63、WP24 的坑 4、WP25 的 T 节、WP26 的坑 7 均已实测）。

**本脚本用在哪个库**：默认 `rednote_verify_wp27`（收口时新建的干净库，迁移从
bootstrap 一路跑到 head 建出全部结构，实测 67 张表、停在 `c4d9f2a17e35`）。
用它的理由与 WP24/WP25/WP26 完全一致：分支测试库里的表是**测试夹具 `create_all`
建的**，不是迁移建的 —— 在那种库上取证据，等于在验证夹具而不是验证迁移。

## 跨包回归项（本包最容易误伤别人的地方）

- 共用的不可变触发器函数 `rednote_reject_mutation()` 必须仍在（本包 `downgrade`
  **刻意不删它** —— 全项目不可变表共用）；
- `version_nodes` 上 **WP18 / WP20 / WP22 / WP23 / WP26** 的 5 个 `*_paired`
  触发器必须共存（本包只挂自己的第 6 个，不得误删别人的）；
- WP18 / WP20 / WP22 / WP23 / WP26 的一致性函数必须存活（下界判据，按包分列）；
- 本包**不拥有** `users` / `brands` / `products` / `projects` / `version_nodes` /
  `jobs` / `file_objects` 等表，只作为 FK 目标引用它们 —— 探针用**独立事务 +
  rollback**，不污染它们。

## 条数 = EXPECTED_CHECKS（分节实测，见脚本末尾 RESULT 行）

| 节 | 内容 |
|---|---|
| S | 迁移 head / 5 表存在 / 库总表数下界 / 5 表列数 / 逐表 CHECK+FK 名单 / 总数 / 触发器集合与函数 / 延迟 FK 点名 / 索引点名 / 无部分索引 / 跨包共存 |
| T | 4 张不可变表 ×(UPDATE+DELETE) 该拦的拦 + `media_items` 该过的过 |
| C | OP-88 正向 3 + 反向 1 + 收窄对照 1 + 绑定一致性 4（含唯一防线 2）+ 上下文一致性 2 |
| F | 两条环 FK 的**正向能过 / 反向能拦** + 延迟 FK 的 `id → version_nodes` + 非延迟 FK 当场拒 |
| D | 形状约束（每条配"合法值必须通过"的对照，防"见谁都拒"） |
| P | 纯函数层（含反向，证明正向判据能区分两种实现） |
| K | 契约 |
| Z | 零残留 |

## 探针自身踩过的坑（写在这里，避免重复踩）

本节是"证据脚本自己出错"的记录 —— 与 WP18 的 ISS-63、WP23/24/25/26 的坑同源
（探针出错会**伪装成实现有缺陷**，比不写探针更危险）。

1. **★ 期望条数必须实测，不能凭印象**（WP24 坑 2 / WP25 坑 1 / WP26 坑 1 同型）：
   `complete = len(checks) >= EXPECTED_CHECKS` 在期望值偏低时**恒真**，
   于是"静默少跑若干条"**永远不会报红**（ISS-43）。本脚本另加一条相等判据
   `exact = len(checks) == EXPECTED_CHECKS` 并入 verdict。
2. **★ 本包的"最后一张表"不是本包的表**：`version_nodes` 是共用表、
   `jobs` / `file_objects` 是别人的表。`OWN_TABLES` 只含本包 5 张 ——
   触发器核对必须**分两组**：本包表上 7 个（集合比对）+ 共用表上 1 个（点名）。
3. **★ 收窄守卫必须先造真实身份行**：`rednote_check_media_node_paired` 的第一道
   判断是"`media_items` 里有 `id = NEW.entity_id` 的行吗"，没有就放行。
   测"孤立节点必须被拦"时**必须先造真实的 `media_items` 行**，
   否则会以 "NO ERROR" 报红，而那是收窄的**预期行为**（ISS-175）。
   本脚本 C4（拦）/ C5（放行）成对，少任一都会让另一条的结论不可信。
4. **延迟约束不会在 INSERT 那一刻检查**：4 条延迟 FK 与 4 个 CONSTRAINT TRIGGER
   都在提交期跑。测它们**必须在事务里显式 `SET CONSTRAINTS ALL IMMEDIATE`**，
   否则"坏值写进去了"会被读成"防线失效"，而其实是"检查点还没到"。
5. **`SET CONSTRAINTS ALL IMMEDIATE` 会把**所有**延迟约束一起强制** ——
   因此铺底数据必须**先自洽**，否则报出来的是另一条无关 FK 的 `23503`
   （"被拒的结论正确不代表被拒的原因正确"，WP24 坑 5 同族）。
6. **★ `media_versions.id` **同时**是 `version_nodes.id` 的 FK**：
   本包版本的 id 必须**先在** `version_nodes` 里注册（`fk_media_versions_id_version_nodes`）。
   漏了这一步，每条探针都会撞那条延迟 FK 的 `23503`，
   而报出来的结论会伪装成"一致性触发器生效" —— **它根本没跑到**。
   本脚本的 `_pair_tx` 把"节点 + 版本"成对写入，就是为了消灭这个形态。
7. **不可变表探针绝不能 commit**：一旦提交就**永远删不掉**（触发器拦 `DELETE`）。
   每条探针独立连接 + 无条件 `rollback()` ⇒ 零残留（Z 节判据专门核对）。
8. **内联脚本只允许单行注释且不能出现三引号**（ISS-45）：三引号会提前终止外层
   `r\"\"\"...\"\"\"`，后面全被当外层代码解析。本文件由 `check_evidence_scripts.py` 机械核对。
9. **★ `reference_assets` 的 `RETIRED` 必须带 `supersedes_asset_id`**
   （`ck_reference_assets_retired_needs_supersedes`）—— 造"已退役素材"的
   探针不注意这一点会撞一条无关 CHECK，把结论伪装成"素材状态检查生效"。
10. **★ `file_objects` 的可见性有两列互相牵制**（`visibility='PROJECT'` 必须
    带 `project_id`）—— 造 blob 与 file_object 时两者必须一起给。
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
#: ⚠️ 本值是**实测回填**的：首轮按分节估值写 108，实跑 117 ⇒ 估值偏低
#: 会让 `len(checks) >= EXPECTED_CHECKS` **恒真**，于是"静默少跑 9 条"
#: 永远不会报红（WP24 坑 2 / WP25 坑 1 / WP26 坑 1 同型，第 4 次复发）。
EXPECTED_CHECKS: int = 117

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

from rednote.modules.media import ddl as media_ddl
from rednote.modules.media import models as media_models
from rednote.modules.media import schemas as media_schemas
from rednote.modules.media import types as media_types

# 期望产生的核对条数（ISS-43：崩溃伪装成"全过"的防线）。
# 实测回填：首轮按分节估值写 108、实跑 117 ⇒ 估值偏低会让
# `len(checks) >= EXPECTED_CHECKS` 恒真（坑 1）。两处必须同值。
EXPECTED_CHECKS = 117

DB = "postgresql+psycopg://rednote@localhost:5432/rednote_verify_wp27"
engine = create_engine(DB)

CONTRACT_PATH = Path(
    "/Users/<user>/内容运营AI Native/rednote-production/contracts/openapi.json"
)

# 本包引入的迁移版本。判据用"不早于"而不是"恰好等于"（ISS-44：
# 硬编码"恰好停在某个版本"会让每个新工作包把历史证据集体打红）。
REVISION_INTRODUCED = "df6bb2f41f84"

# 本包 5 张表的期望列数（按 information_schema **实测**填写，不凭印象）。
TABLE_COLUMNS = (
    ("media_items", 17),
    ("media_versions", 16),  # 15 + media_form（WP28 4.3，迁移 a9d3f6c2b815）
    ("reference_assets", 12),
    ("media_renditions", 12),
    ("media_generation_contexts", 12),
)

# ★ 本脚本的取证范围固定为 WP27 当时的 5 张表（字面量，不读活的 `OWN_TABLES`）。
#   WP28 往 `media.models.OWN_TABLES` / `media.ddl` 追加了 `media_components`、
#   `live_profiles` 及其 3 条索引 / 3 个触发器；这些由 WP28 自己的证据脚本取证。
#   若本脚本继续读活清单，每个后继包加表都会把 WP27 历史证据集体打红（同 ISS-44）。
WP27_OWN_TABLES = (
    "media_items",
    "media_versions",
    "reference_assets",
    "media_renditions",
    "media_generation_contexts",
)
WP28_LATER_INDEXES = frozenset({
    "uq_media_components_version_role",
    "ix_media_components_file",
    "uq_live_profiles_key_version",
})
WP28_LATER_TRIGGERS = frozenset({
    "trg_media_components_immutable",
    "trg_live_profiles_immutable",
    "trg_media_components_consistency",
})
WP28_LATER_TABLES = frozenset({"media_components", "live_profiles"})

# 本包 5 表上的 7 个触发器（4 不可变 + 3 一致性）。
# ★ `trg_version_nodes_media_paired` **不在这 7 个里面** —— 它挂在共用表
# `version_nodes` 上，必须单独点名核对（见 SHARED_NODE_TRIGGER）。
OWN_TABLE_TRIGGERS = (
    "trg_media_versions_immutable",
    "trg_reference_assets_immutable",
    "trg_media_renditions_immutable",
    "trg_media_generation_contexts_immutable",
    "trg_media_versions_node_consistency",
    "trg_media_versions_binding_consistency",
    "trg_media_generation_contexts_consistency",
)

# 本包挂在**共用表** version_nodes 上的那一个（OP-88 的 MEDIA 反向那一半）。
SHARED_NODE_TRIGGER = "trg_version_nodes_media_paired"
SHARED_NODE_FUNCTION = "rednote_check_media_node_paired"

# 别的包挂在同一张 version_nodes 上的配对触发器 —— 必须共存（本包不得误删）。
# 只断言"这 5 个还在"，**不**断言 version_nodes 上触发器总数（判据纪律第 1 条）。
OTHER_PACK_NODE_TRIGGERS = (
    "trg_version_nodes_baseline_paired",
    "trg_version_nodes_brief_paired",
    "trg_version_nodes_content_paired",
    "trg_version_nodes_knowledge_paired",
    "trg_version_nodes_prompt_paired",
)

# ★ 真库实测的 4 条延迟 FK（condeferrable=t AND condeferred=t），逐条点名。
# ⚠️ 其中"环 FK"只有 3 条（第 1/2/4 条）—— 第 3 条是 `id → version_nodes`
#    （不是环，是"注册表先于领域行"的那一条）。两个口径必须分开说。
DEFERRED_FOREIGN_KEYS = (
    "fk_media_items_current_version_id_media_versions",
    "fk_media_versions_generation_context_media_generation_contexts",
    "fk_media_versions_id_version_nodes",
    "fk_media_generation_contexts_media_version_id_media_versions",
)

# ★ 必须**不**延迟的两条（负向判据）：延迟它们会把一条真实错误从"当场拒绝"
# 推迟到"COMMIT 时才拒"，没有任何收益，只有损失。
NON_DEFERRED_FOREIGN_KEYS = (
    "fk_media_versions_image_file_id_file_objects",
    "fk_media_renditions_media_version_id_media_versions",
)

# 跨包一致性函数的下界（按包分列，这样"某个包整批消失"依然能被看见）。
CROSS_PACK_FUNCTIONS = (
    ("baseline", 6),
    ("brief", 4),
    # content 包实测只有 2 个（rednote_check_content_node_paired /
    # rednote_check_content_version_consistency）—— 与 WP26 的叙述一致。
    ("content", 2),
    ("knowledge", 4),
    ("prompt", 4),
)

# 契约里本包的 6 个路径 / 7 个操作（api.py AST 实测，与 WP27 文档 §5 一致）。
OWN_PATHS = (
    "/api/v1/contents/{contentId}/media",
    "/api/v1/contents/{contentId}/media-generations",
    "/api/v1/media/{mediaId}",
    "/api/v1/media/{mediaId}/regenerations",
    "/api/v1/media/{mediaId}/renditions",
    "/api/v1/projects/{projectId}/reference-assets",
)

# 探针用的标记：全部探针数据的名字都带它，便于零残留核对。
PROBE_ISSUER = "wp27-probe"
PROBE_BRAND_PREFIX = "WP27 品牌"
PROBE_PRODUCT_PREFIX = "WP27 产品"
PROBE_PROJECT_NAME = "WP27 探针项目"
PROBE_PROJECT_PREFIX = "WP27PROBE"
PROBE_NODE_HASH_PREFIX = "wp27h-"

# 不可变违反的 sqlstate（触发器 ERRCODE，platform/immutability.py 定义）。
IMMUTABLE_VIOLATION_SQLSTATE = "23001"

# 一致性触发器的 sqlstate（ddl.CONSISTENCY_SQLSTATE）。
CONSISTENCY_SQLSTATE = media_ddl.CONSISTENCY_SQLSTATE


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
    # 不带 CONSTRAINT 名，diag.constraint_name 为空（media/ddl.py 已论证）。
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
    # 4 条延迟 FK 与 4 个 CONSTRAINT TRIGGER 都在 COMMIT 期才跑，
    # 而本脚本**从不 commit**（不可变表 commit 了就删不掉）⇒
    # 不显式 SET CONSTRAINTS，它们的检查点永远不会到。
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


# ---------------------------------------------------------------------------
# 铺底：造最小可用的 user / brand / product / project
# ---------------------------------------------------------------------------

def _seed(conn):
    # 非空集合全部按 information_schema 实测（本项目已多次踩中列名/非空）。
    #   · users    : 没有 email，是 (issuer, subject) 唯一；status 有默认值
    #   · products : 有 model_code（不是 code），且可空
    #   · projects : project_code / name / brand_id / product_id / owner_id /
    #                target_delivery_count / due_date 非空（phase / timezone /
    #                external_policy 有默认值，可不写）
    uid = uuid.uuid4()
    brand = uuid.uuid4()
    prod = uuid.uuid4()
    proj = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, display_name, role, status)"
            " VALUES (:u, :iss, :sub, 'WP27 探针', 'ADMIN', 'ACTIVE')"
        ),
        {"u": str(uid), "iss": PROBE_ISSUER, "sub": f"probe-{uid}"},
    )
    conn.execute(
        text("INSERT INTO brands (id, name, status) VALUES (:b, :n, 'ACTIVE')"),
        {"b": str(brand), "n": f"{PROBE_BRAND_PREFIX} {str(brand)[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO products (id, brand_id, name, model_code, status)"
            " VALUES (:p, :b, :n, :c, 'ACTIVE')"
        ),
        {
            "p": str(prod), "b": str(brand),
            "n": f"{PROBE_PRODUCT_PREFIX} {str(prod)[:8]}",
            "c": f"WP27-{str(prod)[:8]}",
        },
    )
    conn.execute(
        text(
            "INSERT INTO projects (id, project_code, name, brand_id, product_id,"
            " owner_id, target_delivery_count, due_date)"
            " VALUES (:p, :c, :n, :b, :pr, :o, 1, (now() + interval '30 days')::date)"
        ),
        {
            "p": str(proj), "c": f"{PROBE_PROJECT_PREFIX}{str(proj)[:8]}",
            "n": PROBE_PROJECT_NAME,
            "b": str(brand), "pr": str(prod), "o": str(uid),
        },
    )
    return {
        "user": str(uid), "brand": str(brand), "product": str(prod),
        "project": str(proj),
    }


# ---------------------------------------------------------------------------
# 铺底：content_items 行（media_items / media_generation_contexts 的 FK 目标）
# ---------------------------------------------------------------------------

def _content_tx(conn, ids):
    # ★ `media_items.content_item_id` 与 `media_generation_contexts.content_item_id`
    #   都是**非延迟** FK（`fk_media_items_content_item_id_content_items`）⇒
    #   不先插真实行，**每条探针**都会撞 23503，而结论会伪装成"一致性触发器生效"
    #   ——它根本没跑到。这是本包探针最容易漏的一步（首轮实测 47 条红全因于此）。
    #   content_items 由 WP26 拥有；本包**只作为 FK 目标引用**它，
    #   探针用独立事务 + rollback，不留痕（Z 节核对）。
    content_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO content_items (id, project_id, state, current_version_no,"
            " lock_version)"
            " VALUES (:c, :p, 'DRAFT', 0, 0)"
        ),
        {"c": str(content_id), "p": ids["project"]},
    )
    return content_id


# ---------------------------------------------------------------------------
# 5 张表的插入辅助（形状约束由调用方负责 —— 探针要能造出违规行）
# ---------------------------------------------------------------------------

def _item_tx(conn, ids, content_id, *, state="PENDING",
             current_version_id=None, current_version_no=0, sort_order=0,
             paragraph_ref=None, job_id=None, pending_reason=None):
    # media_items 是**可变**身份行。paragraph_ref 形状受 CHECK 约束
    # （`^p[0-9]+-[0-9a-f]{8}$`）⇒ 默认值必须合法。
    item_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO media_items (id, project_id, content_item_id,"
            " paragraph_ref, sort_order, state, current_version_id,"
            " current_version_no, job_id, pending_regeneration_reason,"
            " created_by, lock_version)"
            " VALUES (:i, :p, :c, :pr, :so, :s, :cv, :cno, :j, :pgr, :cb, 0)"
        ),
        {
            "i": str(item_id), "p": ids["project"], "c": str(content_id),
            "pr": paragraph_ref or f"p0-{uuid.uuid4().hex[:8]}",
            "so": sort_order, "s": state,
            "cv": str(current_version_id) if current_version_id else None,
            "cno": current_version_no,
            "j": str(job_id) if job_id else None,
            "pgr": pending_reason,
            "cb": ids["user"],
        },
    )
    return item_id


def _node_tx(conn, ids, node_id, entity_id, *, entity_type="MEDIA",
             revision_no=1):
    # 版本节点。★ `node_id` 是**这次版本**的 id，`entity_id` 是**逻辑实体**身份
    # （见坑 6）；两者是**不同**的值：对 MEDIA 而言 node_id 是 media_versions.id，
    # entity_id 是 media_items.id。
    conn.execute(
        text(
            "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
            " payload_hash, schema_version)"
            " VALUES (:n, :t, :e, :r, :h, 1)"
        ),
        {"n": str(node_id), "t": entity_type, "e": str(entity_id),
         "r": revision_no, "h": f"{PROBE_NODE_HASH_PREFIX}{str(node_id)[:12]}"},
    )


def _blob_and_file(conn, ids, *, project_id=None, visibility="PROJECT"):
    # 造 blob + file_object（reference_assets / media_versions 的 FK 目标）。
    # ★ `file_objects` 的两列互相牵制：visibility='PROJECT' 必须带 project_id
    #   （ck_file_objects_visibility_matches_project）—— 见坑 10。
    blob = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO blobs (id, storage_backend, object_key, sha256,"
            " byte_size, mime_type)"
            " VALUES (:b, 'LOCAL', :k, :s, 1024, 'image/png')"
        ),
        {"b": str(blob), "k": f"wp27-probe/{blob}.png",
         # ★ sha256 必须**恰好 64 字符**（ck_blobs_sha256_length）——
         #   两个 uuid4().hex 正好 64 字符，不是巧合而是刻意的。
         "s": uuid.uuid4().hex + uuid.uuid4().hex},
    )
    fo = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO file_objects (id, blob_id, original_name, visibility,"
            " project_id, classification, uploaded_by)"
            " VALUES (:f, :b, 'wp27-probe.png', :v, :p, 'SUPPORTED', :u)"
        ),
        {"f": str(fo), "b": str(blob), "v": visibility,
         "p": str(project_id or ids["project"]), "u": ids["user"]},
    )
    return fo


def _version_tx(conn, ids, version_id, media_item_id, *, version_no=1,
                paragraph_ref=None, sort_order=0, bindings=None,
                image_file_id=None, prompt="提示词", payload="{}",
                regeneration_reason=None, generation_context_id=None):
    # ★ `bindings=None` ⇒ **自动造一条同项目的真实素材**并引用它。
    #   理由：`ck_media_versions_bindings_size` 要求 1~8 条（0 条 = "看起来
    #   有绑定实际没绑"）⇒ 默认 `"[]"` 会让**每条**探针都先撞这条 CHECK，
    #   报出的结论会伪装成"一致性触发器生效"（首轮实测 18 条红全因于此）。
    #   `bindings="[]"`（显式）仍然照传 —— D10 就是专门测那一格的。
    if bindings is None:
        bindings = json.dumps([{"assetId": str(_asset_tx(conn, ids))}])
    # 版本行。jsonb 列用 cast(:x as jsonb) 传**文本**，不传 Python 对象 ——
    # 原生 SQL 参数不做过类型推断（WP24 的 _raw 说明已经踩过）。
    # ★ image_file_id 非空 ⇒ 不传时必须先造一个 file_object（由调用方保证）。
    conn.execute(
        text(
            "INSERT INTO media_versions (id, media_item_id, version_no,"
            " paragraph_ref, sort_order, reference_bindings, image_file_id,"
            " prompt, regeneration_reason, generation_context_id, payload,"
            " payload_hash, schema_version)"
            " VALUES (:id, :mi, :vno, :pr, :so, cast(:rb as jsonb), :ifid,"
            " :pr_, :rr, :gc, cast(:pl as jsonb), :ph, 1)"
        ),
        {
            "id": str(version_id), "mi": str(media_item_id), "vno": version_no,
            "pr": paragraph_ref or f"p0-{uuid.uuid4().hex[:8]}",
            "so": sort_order, "rb": bindings, "ifid": str(image_file_id),
            "pr_": prompt,
            "rr": regeneration_reason,
            "gc": str(generation_context_id) if generation_context_id else None,
            "pl": payload, "ph": uuid.uuid4().hex + uuid.uuid4().hex,
        },
    )


def _pair_tx(conn, ids, media_item_id, *, version_no=1, bindings=None,
             image_file_id=None, **kw):
    # "节点 + 版本"成对写入（本包最常见的合法底料）。
    # ★ 必须成对：`media_versions.id` 同时是 `version_nodes.id` 的**延迟 FK**
    #   ⇒ 只插版本不插节点，每条探针都会撞那条 FK 的 23503，
    #   而结论会伪装成"一致性触发器生效"（它根本没跑到）。见坑 6。
    version_id = uuid.uuid4()
    _node_tx(conn, ids, version_id, media_item_id, revision_no=version_no)
    _version_tx(conn, ids, version_id, media_item_id, version_no=version_no,
                bindings=bindings, image_file_id=image_file_id, **kw)
    return version_id


def _asset_tx(conn, ids, *, state="ACTIVE", project_id=None, supersedes=None,
              product_model=None):
    # 参考素材行。★ state='RETIRED' 必须带 supersedes_asset_id
    # （ck_reference_assets_retired_needs_supersedes）—— 见坑 9。
    fo = _blob_and_file(conn, ids, project_id=project_id)
    asset_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO reference_assets (id, project_id, file_object_id,"
            " state, product_model, supersedes_asset_id, created_by)"
            " VALUES (:a, :p, :f, :s, :m, :sup, :u)"
        ),
        {
            "a": str(asset_id), "p": str(project_id or ids["project"]),
            "f": str(fo), "s": state,
            # ★ 与 `_context_tx` 的幂等键**同一个坑的第二处**：空串是合法测试
            #   输入（D19 专门测它必须被 CHECK 拒），`or` 会把它换成默认值
            #   ⇒ 探针"竟然写进去了"而结论指向**实现缺陷**（其实是探针自己错）。
            "m": (
                product_model
                if product_model is not None
                else f"WP27-{str(asset_id)[:8]}"
            ),
            "sup": str(supersedes) if supersedes else None,
            "u": ids["user"],
        },
    )
    return asset_id


def _rendition_tx(conn, ids, version_id, *, kind="PREVIEW", attempt_no=1,
                  state="PENDING", file_object_id=None):
    row = conn.execute(
        text(
            "INSERT INTO media_renditions (id, media_version_id, kind,"
            " attempt_no, state, file_object_id)"
            " VALUES (gen_random_uuid(), :v, :k, :a, :s, :f) RETURNING id"
        ),
        {"v": str(version_id), "k": kind, "a": attempt_no, "s": state,
         "f": str(file_object_id) if file_object_id else None},
    )
    return row.scalar_one()


def _context_tx(conn, ids, content_id, *, context_id=None, media_version_id=None,
                task_kind="STATIC_IMAGE", idempotency_key=None):
    # ★ `context_id` 可显式指定：**环 FK 的正向判据必须能先写出"指着尚不存在
    #   对象的引用"**，再把它指的那个对象补进去。若 id 由 `gen_random_uuid()`
    #   就地生成，引用方就**永远不可能**指到它 —— 于是"正向应当通过"会被
    #   误读成"延迟没生效"（WP26 坑 9 同型）。
    ctx_id = str(context_id) if context_id else str(uuid.uuid4())
    row = conn.execute(
        text(
            "INSERT INTO media_generation_contexts (id, project_id,"
            " content_item_id, media_version_id, task_kind, idempotency_key,"
            " created_by)"
            " VALUES (cast(:id as uuid), :p, :c, :mv, :k, :ik, :cb)"
            " RETURNING id"
        ),
        {
            "id": ctx_id,
            "p": ids["project"], "c": str(content_id),
            "mv": str(media_version_id) if media_version_id else None,
            "k": task_kind,
            # ★ 用 `is not None` 而不是 `or`：空串是**合法的测试输入**
            #   （D16 专门测它必须被 CHECK 拒），`or` 会把它换成默认值
            #   ⇒ 探针"竟然写进去了"而判据报红，结论指向**错误的实现缺陷**。
            "ik": (
                idempotency_key
                if idempotency_key is not None
                else f"wp27-probe-{uuid.uuid4().hex[:12]}"
            ),
            "cb": ids["user"],
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
    own = WP27_OWN_TABLES
    emit(
        set(own) <= present,
        "[S2] 本包 5 张表全部存在",
        f"缺失={sorted(set(own) - present) or '无'}",
    )

    total_tables = len(present)
    # 下界而不是等号：后续包（乃至 M4）会继续加表（判据纪律第 1 条）。
    emit(total_tables >= 62, "[S3] 库总表数下界", f"{total_tables} (下界 62)")

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
        t = media_models.Base.metadata.tables[table]
        if kind == "c":
            return sorted(
                c.name for c in t.constraints if isinstance(c, sa.CheckConstraint)
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
    # 40 + 1：WP28 给 media_versions 加了 `media_form_valid`（逐条名单由 S5 对账）。
    emit(tot_chk == 41, "[S7] 本包 CHECK 总数（实测 41 = 40 + media_form_valid）", tot_chk)
    emit(tot_fk == 24, "[S8] 本包 FK 总数（实测 24）", tot_fk)

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
        imm_fns == set(media_ddl.immutable_function_names()),
        "[S10] 4 个不可变触发器指向共用函数 rednote_reject_mutation",
        f"实际={sorted(imm_fns)}",
    )

    # 3 个一致性触发器必须指向本包自己的 3 个函数（**不含**共用表上那一个）。
    cons_trg = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid"
                " WHERE NOT t.tgisinternal AND c.relname = ANY(:ts)"
                "   AND t.tgname NOT LIKE '%\\_immutable'"
            ),
            {"ts": list(own)},
        ).fetchall()
    }
    emit(
        cons_trg == (
            set(media_ddl.consistency_trigger_names())
            - {SHARED_NODE_TRIGGER} - WP28_LATER_TRIGGERS
        ),
        "[S11] 本包 3 个一致性触发器集合与声明一致",
        f"实际={sorted(cons_trg)}",
    )

    # 跨包：共用不可变函数必须仍在。★ 用**下界**而不是 ==1 —— 它是全项目共用的，
    # 本包只断言"它还在"（判据纪律第 1 条）。
    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname = 'rednote_reject_mutation'")
    ).scalar_one()
    emit(n >= 1, "[S12] 共用函数 rednote_reject_mutation 仍在", f"{n} (下界 1)")

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
        emit(False, "[S13] 共用表 version_nodes 上的反向触发器存在", "未找到")
    else:
        emit(
            row[0] == "version_nodes" and row[1] == SHARED_NODE_FUNCTION
            and "AFTER INSERT" in row[2] and "WHEN" in row[2],
            "[S13] 共用表 version_nodes 上的反向触发器存在（含 WHEN 收窄）",
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
        "[S14] 4 条延迟 FK 逐条点名一致",
        f"缺失={sorted(set(DEFERRED_FOREIGN_KEYS) - deferred) or '无'}"
        f" 多余={sorted(deferred - set(DEFERRED_FOREIGN_KEYS)) or '无'}",
    )

    # ★ 22 条索引逐条点名。期望侧**从 ORM 元数据派生**（铁律 14：手写清单会漏，
    # 而漏了不报错 —— 只是少查一条，那条索引可以静默消失）。
    declared_idx = set(media_ddl.index_names()) - WP28_LATER_INDEXES
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
    # （该函数 docstring 已说明）。这里比的是**本模块显式声明的**那 22 条。
    missing_idx = sorted(declared_idx - actual_idx)
    emit(
        not missing_idx,
        "[S15] 22 条声明索引在库中全部存在",
        f"声明={len(declared_idx)} 缺失={missing_idx or '无'}"
        f"（库内本包 5 表共 {len(actual_idx)} 条，含 5 条 pk_*）",
    )

    # ★ 唯一索引的**形状**（`unique=True` ⇒ 它必须真的是唯一的）。
    #   名字对不代表语义对：把 `unique=True` 去掉，S15 照样绿。
    uniq = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT c.relname FROM pg_index i"
                " JOIN pg_class c ON c.oid = i.indexrelid"
                " WHERE i.indisunique AND i.indisvalid"
                "   AND c.relname IN ('uq_media_items_job_id',"
                " 'uq_media_items_content_sort', 'uq_media_versions_item_version_no',"
                " 'uq_media_renditions_version_kind_attempt',"
                " 'uq_media_generation_contexts_idempotency')"
            )
        ).fetchall()
    }
    emit(
        len(uniq) == 5,
        "[S16] 5 条 uq_* 索引在库中确实是 UNIQUE 且有效",
        f"实到={sorted(uniq)}",
    )

    # ★ 本包**没有部分索引** —— 与 `consistency_items()` 有意为非空同一种形态：
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
        "[S17] 本包 5 表无部分索引（有意形状，非漏写 WHERE）",
        f"实到={partial}",
    )

    # 跨包：另 5 个包挂在同一张 version_nodes 上的配对触发器必须共存
    # （本包只挂自己的第 6 个，不得误删别人的）。
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
        "[S18] version_nodes 上另 5 个包的配对触发器共存",
        "; ".join(alive),
    )

    # 跨包：共用表 version_nodes 必须仍在（本包只删挂在自己上面的 1 个触发器）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM information_schema.tables"
            " WHERE table_schema='public' AND table_name='version_nodes'"
        )
    ).scalar_one()
    emit(n == 1, "[S19] 共用表 version_nodes 仍在", n)

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
            f"[S20] 跨包一致性函数 rednote_check_{_pkg}_* 存活",
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
            content_id = _content_tx(conn, ids)
            item = _item_tx(conn, ids, content_id)
            if table == "media_versions":
                vid = _pair_tx(conn, ids, item,
                               image_file_id=_blob_and_file(conn, ids))
                where = f"id = '{vid}'"
            elif table == "reference_assets":
                aid = _asset_tx(conn, ids)
                where = f"id = '{aid}'"
            elif table == "media_renditions":
                vid = _pair_tx(conn, ids, item,
                               image_file_id=_blob_and_file(conn, ids))
                rid = _rendition_tx(conn, ids, vid)
                where = f"id = '{rid}'"
            else:
                ctx = _context_tx(conn, ids, content_id)
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
    "media_versions": "prompt",
    "reference_assets": "product_model",
    "media_renditions": "note",
    "media_generation_contexts": "task_kind",
}
_MUTATE_VALUE = {
    "media_versions": "'改过了'",
    "reference_assets": "'改过了'",
    "media_renditions": "'改过了'",
    "media_generation_contexts": "'STATIC_IMAGE'",
}

for _tbl in ("media_versions", "reference_assets", "media_renditions",
             "media_generation_contexts"):
    for _op in ("UPDATE", "DELETE"):
        _immutable_probe(_tbl, _op)

# 对照：`media_items` 是**可变**身份行，UPDATE 与 DELETE 都必须通过。
# 若这两条被拦，说明有人把不可变触发器挂错表了 ——
# 而"把 5 张表全锁上"的实现也能让上面 8 条全绿，那样系统完全不可用
# （`current_version_id` 投影无法前进、`lock_version`（ETag）无法递增）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        item = _item_tx(conn, ids, content_id)
        conn.execute(
            text("UPDATE media_items SET state = 'FAILED' WHERE id = :i"),
            {"i": str(item)},
        )
        emit(True, "[T] 对照：可变表 media_items 的 UPDATE 能通过",
             "写入成功（符合预期）")
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[T] 对照：可变表 media_items 的 UPDATE 能通过",
         f"探针异常 {type(exc).__name__}: {exc}")

try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        item = _item_tx(conn, ids, content_id)
        conn.execute(
            text("DELETE FROM media_items WHERE id = :i"), {"i": str(item)}
        )
        emit(True, "[T] 对照：可变表 media_items 的 DELETE 能通过",
             "写入成功（符合预期）")
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[T] 对照：可变表 media_items 的 DELETE 能通过",
         f"探针异常 {type(exc).__name__}: {exc}")

# ===========================================================================
# C 节 —— 一致性触发器（OP-88 正向 3 + 反向 1 + 收窄对照 1 + 绑定 4 + 上下文 2）
# ===========================================================================

# C1 OP-88 判据 1：`media_versions.id` 没有对应的 `version_nodes` 行。
# ★ 实际报的是 `23503`（被**延迟 FK** 先拦），不是触发器的 23514 ——
#   在 SET CONSTRAINTS ALL IMMEDIATE 时 FK 检查先跑，触发器那句 RAISE 没机会执行。
#   按"触发器 23514"写这条会恒红；断言 23503 并**点名**那条 FK，
#   这样将来有人把这条 FK 改成非延迟或去掉时会当场看见。
# ★ 必须先造真实的 media_items 行 + 真实的 file_object（后者是 FK 目标）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        item = _item_tx(conn, ids, content_id)
        fo = _blob_and_file(conn, ids)
        _version_tx(conn, ids, uuid.uuid4(), item, image_file_id=fo)
        _expect_check(
            "[C1] OP-88 判据 1（节点不存在）报 23503 且点名延迟 FK",
            conn,
            lambda: _force(conn),
            sqlstate_expected="23503",
            constraint="fk_media_versions_id_version_nodes",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C1] OP-88 判据 1（节点不存在）", f"探针异常 {type(exc).__name__}: {exc}")

# C2 OP-88 判据 2：节点存在，但 `entity_type` 不是 MEDIA。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        item = _item_tx(conn, ids, content_id)
        fo = _blob_and_file(conn, ids)
        vid = uuid.uuid4()
        _node_tx(conn, ids, vid, item, entity_type="BRIEF")
        _version_tx(conn, ids, vid, item, image_file_id=fo)
        _expect_sqlstate(
            "[C2] OP-88 判据 2（entity_type != MEDIA）报 23514",
            conn,
            lambda: _force(conn),
            sqlstate_expected=CONSISTENCY_SQLSTATE,
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C2] OP-88 判据 2（entity_type != MEDIA）",
         f"探针异常 {type(exc).__name__}: {exc}")

# C3 OP-88 判据 3：节点存在且 entity_type 正确，但 `entity_id` 与本行的
# `media_item_id` 不一致（"给 A 媒体位注册了一个声称属于 B 媒体位的版本节点"）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        item = _item_tx(conn, ids, content_id)
        other = _item_tx(conn, ids, content_id, sort_order=1)
        fo = _blob_and_file(conn, ids)
        vid = uuid.uuid4()
        _node_tx(conn, ids, vid, other)
        _version_tx(conn, ids, vid, item, image_file_id=fo)
        _expect_sqlstate(
            "[C3] OP-88 判据 3（entity_id 与 media_item_id 不一致）报 23514",
            conn,
            lambda: _force(conn),
            sqlstate_expected=CONSISTENCY_SQLSTATE,
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C3] OP-88 判据 3（entity_id 不一致）",
         f"探针异常 {type(exc).__name__}: {exc}")

# C4 反向判据：媒体位**真实存在**、但节点没有配对的版本行 ⇒ 23514。
# ★ 必须先造**真实的** media_items 行 —— 收窄后的守卫只对"真实存在的媒体身份"
#   强制；随机 uuid4 会走**放行**分支（见坑 3 与 C5 对照）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        item = _item_tx(conn, ids, content_id)
        _node_tx(conn, ids, uuid.uuid4(), item, revision_no=99)
        _expect_sqlstate(
            "[C4] 反向：孤立 MEDIA 节点（媒体位存在）报 23514",
            conn,
            lambda: _force(conn),
            sqlstate_expected=CONSISTENCY_SQLSTATE,
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C4] 反向：孤立 MEDIA 节点（媒体位存在）",
         f"探针异常 {type(exc).__name__}: {exc}")

# C5 收窄对照：`entity_id` 指向**不存在**的媒体位 ⇒ **放行**。
# ★ 没有这条，C4 在一个"永远 RAISE"的实现上也会全绿 —— 而那样的实现会让
#   WP11 的通用用例（MEDIA 是它 8 类样本实体之一）直接失败，即"别的包的进度
#   被本包阻塞"（WP26 实测：不收窄时 WP11 既有 2 例报红，ISS-175）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        _node_tx(conn, ids, uuid.uuid4(), uuid.uuid4(), revision_no=99)
        _expect_ok(
            "[C5] 对照：孤立 MEDIA 节点（媒体位不存在）放行（收窄行为）",
            conn,
            lambda: _force(conn),
            detail="放行（符合预期，收窄守卫生效）",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C5] 对照：孤立 MEDIA 节点（媒体位不存在）放行",
         f"探针异常 {type(exc).__name__}: {exc}")

# C6 合法底料必须**能写进去** —— 后面所有"被拦"的对照基准。
#   缺了它，一个"见谁都拒"的实现能让 C1~C5 全绿。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        item = _item_tx(conn, ids, content_id)
        asset = _asset_tx(conn, ids)
        fo = _blob_and_file(conn, ids)
        _pair_tx(conn, ids, item, image_file_id=fo,
                 bindings=json.dumps([{"assetId": str(asset)}]))
        _expect_ok(
            "[C6] 对照：合法「媒体位 + 节点 + 版本（含 1 条真实绑定）」能通过",
            conn,
            lambda: _force(conn),
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C6] 对照：合法底料能通过", f"探针异常 {type(exc).__name__}: {exc}")

# C7 判据 3.1（唯一防线）：绑定的 assetId 指向**不存在**的素材。
#   `reference_bindings` 是 JSONB，**没有任何 FK** ⇒ 本触发器是唯一能拦的地方。
#   断言 23514 且**不带约束名**（触发器 RAISE 没有 conname）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        item = _item_tx(conn, ids, content_id)
        fo = _blob_and_file(conn, ids)
        _pair_tx(conn, ids, item, image_file_id=fo,
                 bindings=json.dumps([{"assetId": str(uuid.uuid4())}]))
        _expect_sqlstate(
            "[C7] 判据 3.1（绑定素材不存在，JSONB 唯一防线）报 23514",
            conn,
            lambda: _force(conn),
            sqlstate_expected=CONSISTENCY_SQLSTATE,
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C7] 判据 3.1（绑定素材不存在）",
         f"探针异常 {type(exc).__name__}: {exc}")

# C8 判据 3.2（唯一防线）：素材真实存在，但属于**别的项目**。
#   FK 只能保证"素材存在"，保证不了"它属于同一项目" ⇒ 只能由触发器管。
#   AQ-03 按项目盲审，跨项目引用会让别的项目的产品静默混入。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        item = _item_tx(conn, ids, content_id)
        # 第二个项目 + 属于它的素材
        other_proj = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO projects (id, project_code, name, brand_id,"
                " product_id, owner_id, target_delivery_count, due_date)"
                " VALUES (:p, :c, 'WP27 探针项目2', :b, :pr, :o, 1,"
                " (now() + interval '30 days')::date)"
            ),
            {
                "p": str(other_proj), "c": f"{PROBE_PROJECT_PREFIX}2{str(other_proj)[:8]}",
                "b": ids["brand"], "pr": ids["product"], "o": ids["user"],
            },
        )
        foreign_asset = _asset_tx(conn, ids, project_id=other_proj)
        fo = _blob_and_file(conn, ids)
        _pair_tx(conn, ids, item, image_file_id=fo,
                 bindings=json.dumps([{"assetId": str(foreign_asset)}]))
        _expect_sqlstate(
            "[C8] 判据 3.2（绑定素材跨项目，JSONB 唯一防线）报 23514",
            conn,
            lambda: _force(conn),
            sqlstate_expected=CONSISTENCY_SQLSTATE,
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C8] 判据 3.2（绑定素材跨项目）",
         f"探针异常 {type(exc).__name__}: {exc}")

# C9 判据 3.3：`media_generation_contexts` 引用的媒体版本必须属于**本行的
#   `content_item_id`**。跨两级核对（version → media_item → content_item）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_a = _content_tx(conn, ids)
        content_b = _content_tx(conn, ids)
        item_a = _item_tx(conn, ids, content_a)
        item_b = _item_tx(conn, ids, content_b)
        fo = _blob_and_file(conn, ids)
        vid_b = _pair_tx(conn, ids, item_b, image_file_id=fo)
        # 上下文声明属于 content_a，但它引用的版本属于 content_b
        _context_tx(conn, ids, content_a, media_version_id=vid_b)
        _expect_sqlstate(
            "[C9] 判据 3.3（上下文引用的版本跨内容）报 23514",
            conn,
            lambda: _force(conn),
            sqlstate_expected=CONSISTENCY_SQLSTATE,
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C9] 判据 3.3（上下文引用的版本跨内容）",
         f"探针异常 {type(exc).__name__}: {exc}")

# C10 对照：`media_version_id IS NULL`（首次生成）必须**放行** ——
#   那是合法常态（首次生成时版本行还没有），不是漏判。
#   若对这个 NULL 也要求配对，首次生成的上下文行永远留不下来，
#   于是"这张图是哪版提示词生成的"永远查不到（OP-160 要回答的问题）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        _item_tx(conn, ids, content_id)
        _context_tx(conn, ids, content_id, media_version_id=None)
        _expect_ok(
            "[C10] 对照：首次生成的上下文（无版本）放行",
            conn,
            lambda: _force(conn),
            detail="放行（符合预期：该 NULL 是合法常态）",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C10] 对照：首次生成的上下文（无版本）放行",
         f"探针异常 {type(exc).__name__}: {exc}")

# C11 对照：绑定**形状**（长度为 1、含 note/part 的可选键）合法 ⇒ 必须放行。
#   与 C7/C8 成对：一个"见到任何绑定就拒"的实现能让 C7/C8 全绿。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        item = _item_tx(conn, ids, content_id)
        asset = _asset_tx(conn, ids)
        fo = _blob_and_file(conn, ids)
        _pair_tx(conn, ids, item, image_file_id=fo,
                 bindings=json.dumps(
                     [{"assetId": str(asset), "note": "正面图", "part": "机身"}]
                 ))
        _expect_ok(
            "[C11] 对照：绑定含可选键 note/part 且同项目 ⇒ 放行",
            conn,
            lambda: _force(conn),
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C11] 对照：绑定含可选键 note/part 放行",
         f"探针异常 {type(exc).__name__}: {exc}")

# C12 对照：8 条绑定（上界）⇒ 必须放行（防"见多就拒"）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        item = _item_tx(conn, ids, content_id)
        assets = [_asset_tx(conn, ids) for _ in range(8)]
        fo = _blob_and_file(conn, ids)
        _pair_tx(conn, ids, item, image_file_id=fo,
                 bindings=json.dumps(
                     [{"assetId": str(a)} for a in assets]
                 ))
        _expect_ok(
            "[C12] 对照：8 条绑定（上界）⇒ 放行",
            conn,
            lambda: _force(conn),
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[C12] 对照：8 条绑定（上界）放行",
         f"探针异常 {type(exc).__name__}: {exc}")

# ===========================================================================
# F 节 —— 延迟 FK 的"正向能过 / 反向能拦" + 非延迟 FK 的当场拒
# ===========================================================================

# F1 环 1 正向：先插媒体位（`current_version_id` 指向**尚不存在**的版本），
#    再插版本行 + 节点 —— 同事务，提交期检查 ⇒ 必须通过。
#    这是"延迟这条 FK 的**唯一**理由"，缺了它，那条延迟就是无根据的放宽。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        vid = uuid.uuid4()
        item = _item_tx(conn, ids, content_id, current_version_id=vid,
                        current_version_no=1)
        fo = _blob_and_file(conn, ids)
        _node_tx(conn, ids, vid, item)
        _version_tx(conn, ids, vid, item, image_file_id=fo)
        _expect_ok(
            "[F1] 环 1 正向：先媒体位（悬空 current_version_id）后版本，同事务通过",
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
        content_id = _content_tx(conn, ids)
        _item_tx(conn, ids, content_id, current_version_id=uuid.uuid4(),
                 current_version_no=1)
        _expect_check(
            "[F2] 环 1 反向：current_version_id 悬空报 23503",
            conn,
            lambda: _force(conn),
            sqlstate_expected="23503",
            constraint="fk_media_items_current_version_id_media_versions",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[F2] 环 1 反向", f"探针异常 {type(exc).__name__}: {exc}")

# F3 环 2 正向：先插版本（`generation_context_id` 指向**尚不存在**的上下文），
#    再插上下文 —— 同事务通过。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        ctx = uuid.uuid4()
        item = _item_tx(conn, ids, content_id)
        fo = _blob_and_file(conn, ids)
        vid = uuid.uuid4()
        _node_tx(conn, ids, vid, item)
        _version_tx(conn, ids, vid, item, image_file_id=fo,
                    generation_context_id=ctx)
        _context_tx(conn, ids, content_id, context_id=ctx, media_version_id=vid)
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
        content_id = _content_tx(conn, ids)
        item = _item_tx(conn, ids, content_id)
        fo = _blob_and_file(conn, ids)
        vid = uuid.uuid4()
        _node_tx(conn, ids, vid, item)
        _version_tx(conn, ids, vid, item, image_file_id=fo,
                    generation_context_id=uuid.uuid4())
        _expect_check(
            "[F4] 环 2 反向：generation_context_id 悬空报 23503",
            conn,
            lambda: _force(conn),
            sqlstate_expected="23503",
            constraint="fk_media_versions_generation_context_media_generation_contexts",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[F4] 环 2 反向", f"探针异常 {type(exc).__name__}: {exc}")

# F5 环 3 反向：`media_generation_contexts.media_version_id` 指向不存在的版本
#    ⇒ 23503，点名那条 FK。（这就是 WP26 的环 2 在本包的**另一半**。）
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        _item_tx(conn, ids, content_id)
        _context_tx(conn, ids, content_id, media_version_id=uuid.uuid4())
        _expect_check(
            "[F5] 环 3 反向：contexts.media_version_id 悬空报 23503",
            conn,
            lambda: _force(conn),
            sqlstate_expected="23503",
            constraint="fk_media_generation_contexts_media_version_id_media_versions",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[F5] 环 3 反向", f"探针异常 {type(exc).__name__}: {exc}")

# F6 延迟 FK `id → version_nodes` 的**正向**：节点与版本同事务写入（合法常态）。
#    这条 FK 不是环的一部分，而是"注册表先于领域行"的**顺序放宽**。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        item = _item_tx(conn, ids, content_id)
        fo = _blob_and_file(conn, ids)
        _pair_tx(conn, ids, item, image_file_id=fo)
        _expect_ok(
            "[F6] 延迟 FK `id → version_nodes` 正向：节点 + 版本同事务通过",
            conn,
            lambda: _force(conn),
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[F6] 延迟 FK 正向", f"探针异常 {type(exc).__name__}: {exc}")

# F7 负向：`media_versions.image_file_id` **必须不延迟** ⇒ 坏值在 INSERT
#    那一刻就撞 23503（连 SET CONSTRAINTS 都不用）。
#    ★ 这条是"我们没有过度延迟"的**正向证据**：延迟它只是把一条真实错误
#      从"当场拒绝"推迟到"COMMIT 时才拒"，没有任何收益，只有损失。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        item = _item_tx(conn, ids, content_id)
        vid = uuid.uuid4()
        _node_tx(conn, ids, vid, item)
        _expect_check(
            "[F7] 非延迟：media_versions.image_file_id 坏值当场报 23503",
            conn,
            lambda: _version_tx(conn, ids, vid, item, image_file_id=uuid.uuid4()),
            sqlstate_expected="23503",
            constraint="fk_media_versions_image_file_id_file_objects",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[F7] 非延迟：image_file_id 坏值当场拒",
         f"探针异常 {type(exc).__name__}: {exc}")

# F8 负向：`media_renditions.media_version_id` **必须不延迟** ⇒ 当场 23503。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        _expect_check(
            "[F8] 非延迟：media_renditions.media_version_id 坏值当场报 23503",
            conn,
            lambda: _rendition_tx(conn, ids, uuid.uuid4()),
            sqlstate_expected="23503",
            constraint="fk_media_renditions_media_version_id_media_versions",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[F8] 非延迟：media_version_id 坏值当场拒",
         f"探针异常 {type(exc).__name__}: {exc}")

# ===========================================================================
# D 节 —— 形状约束（每条配"合法值必须通过"的对照，防"见谁都拒"）
# ===========================================================================


def _shape_probe(label, constraint, mutate_fn, sqlstate_expected="23514"):
    # 铺底统一由本函数做；`mutate_fn` 只负责"往这个底料上写一条该被拒的行"。
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            ids = _seed(conn)
            content_id = _content_tx(conn, ids)
            item = _item_tx(conn, ids, content_id)
            fo = _blob_and_file(conn, ids)
            _expect_check(
                label, conn, lambda: mutate_fn(conn, ids, item, content_id, fo),
                sqlstate_expected=sqlstate_expected, constraint=constraint,
            )
            trans.rollback()
    except Exception as exc:  # noqa: BLE001
        emit(False, label, f"探针异常 {type(exc).__name__}: {exc}")


def _bad_paragraph_ref_shape(conn, ids, item, content_id, fo):
    conn.execute(
        text("UPDATE media_items SET paragraph_ref = 'nope' WHERE id = :i"),
        {"i": str(item)},
    )


def _bad_paragraph_ref_empty(conn, ids, item, content_id, fo):
    conn.execute(
        text("UPDATE media_items SET paragraph_ref = '' WHERE id = :i"),
        {"i": str(item)},
    )


def _bad_sort_order(conn, ids, item, content_id, fo):
    conn.execute(
        text("UPDATE media_items SET sort_order = -1 WHERE id = :i"),
        {"i": str(item)},
    )


def _bad_item_state(conn, ids, item, content_id, fo):
    conn.execute(
        text("UPDATE media_items SET state = 'NOPE' WHERE id = :i"),
        {"i": str(item)},
    )


def _bad_projection_coherent(conn, ids, item, content_id, fo):
    vid = uuid.uuid4()
    _node_tx(conn, ids, vid, item)
    conn.execute(
        text(
            "UPDATE media_items SET current_version_id = :v,"
            " current_version_no = 0 WHERE id = :i"
        ),
        {"v": str(vid), "i": str(item)},
    )


def _bad_pending_bindings_type(conn, ids, item, content_id, fo):
    conn.execute(
        text(
            "UPDATE media_items SET pending_reference_bindings ="
            " cast('{}' as jsonb) WHERE id = :i"
        ),
        {"i": str(item)},
    )


def _bad_pending_bindings_size(conn, ids, item, content_id, fo):
    conn.execute(
        text(
            "UPDATE media_items SET pending_reference_bindings ="
            " cast('[]' as jsonb) WHERE id = :i"
        ),
        {"i": str(item)},
    )


def _bad_version_no_positive(conn, ids, item, content_id, fo):
    _version_tx(conn, ids, uuid.uuid4(), item, version_no=0, image_file_id=fo)


def _bad_prompt_empty(conn, ids, item, content_id, fo):
    _version_tx(conn, ids, uuid.uuid4(), item, prompt="", image_file_id=fo)


def _bad_bindings_size_zero(conn, ids, item, content_id, fo):
    _version_tx(conn, ids, uuid.uuid4(), item, bindings="[]", image_file_id=fo)


def _bad_schema_version(conn, ids, item, content_id, fo):
    # ★ 本条是**手写 INSERT**（要为 `schema_version` 指定 0），因此不能走
    #   `_version_tx` 的默认绑定 ⇒ 必须**自己给一条真实绑定**。
    #   给它 `'[]'` 会先撞 `ck_media_versions_bindings_size`，
    #   报出的约束名指向另一条 —— "被拒的结论正确不代表被拒的原因正确"
    #   （WP24 坑 5 / WP25 坑 3 / WP26 坑 5 同族）。
    vid = uuid.uuid4()
    _node_tx(conn, ids, vid, item)
    asset = _asset_tx(conn, ids)
    conn.execute(
        text(
            "INSERT INTO media_versions (id, media_item_id, version_no,"
            " paragraph_ref, sort_order, reference_bindings, image_file_id,"
            " prompt, payload, payload_hash, schema_version)"
            " VALUES (:id, :mi, 1, :pr, 0, cast(:rb as jsonb), :f, 'p',"
            " cast('{}' as jsonb), :ph, 0)"
        ),
        {"id": str(vid), "mi": str(item), "pr": f"p0-{uuid.uuid4().hex[:8]}",
         "rb": json.dumps([{"assetId": str(asset)}]),
         "f": str(fo), "ph": uuid.uuid4().hex + uuid.uuid4().hex},
    )


def _bad_rendition_kind(conn, ids, item, content_id, fo):
    vid = _pair_tx(conn, ids, item, image_file_id=fo)
    _rendition_tx(conn, ids, vid, kind="NOPE")


def _bad_rendition_attempt_no(conn, ids, item, content_id, fo):
    vid = _pair_tx(conn, ids, item, image_file_id=fo)
    _rendition_tx(conn, ids, vid, attempt_no=0)


def _bad_rendition_ready_without_artifact(conn, ids, item, content_id, fo):
    vid = _pair_tx(conn, ids, item, image_file_id=fo)
    _rendition_tx(conn, ids, vid, state="READY")


def _bad_context_task_kind(conn, ids, item, content_id, fo):
    _context_tx(conn, ids, content_id, task_kind="NOPE")


def _bad_context_key_empty(conn, ids, item, content_id, fo):
    _context_tx(conn, ids, content_id, idempotency_key="")


_shape_probe("[D01] media_items.paragraph_ref 形状（非 pN-xxxxxxxx）",
             "ck_media_items_paragraph_ref_shape", _bad_paragraph_ref_shape)
_shape_probe("[D02] media_items.paragraph_ref 空串",
             "ck_media_items_paragraph_ref_not_empty", _bad_paragraph_ref_empty)
_shape_probe("[D03] media_items.sort_order 负数",
             "ck_media_items_sort_order_non_negative", _bad_sort_order)
_shape_probe("[D04] media_items.state 取值域外",
             "ck_media_items_state_valid", _bad_item_state)
_shape_probe("[D05] media_items 投影不自洽（有版本号却 version_no=0）",
             "ck_media_items_projection_coherent", _bad_projection_coherent)
_shape_probe("[D06] media_items.pending_reference_bindings 非数组",
             "ck_media_items_pending_bindings_is_array", _bad_pending_bindings_type)
_shape_probe("[D07] media_items.pending_reference_bindings 空数组",
             "ck_media_items_pending_bindings_size", _bad_pending_bindings_size)
_shape_probe("[D08] media_versions.version_no 为 0",
             "ck_media_versions_version_no_positive", _bad_version_no_positive)
_shape_probe("[D09] media_versions.prompt 空串",
             "ck_media_versions_prompt_not_empty", _bad_prompt_empty)
_shape_probe("[D10] media_versions.reference_bindings 空数组",
             "ck_media_versions_bindings_size", _bad_bindings_size_zero)
_shape_probe("[D11] media_versions.schema_version 为 0",
             "ck_media_versions_schema_version_positive", _bad_schema_version)
_shape_probe("[D12] media_renditions.kind 取值域外",
             "ck_media_renditions_kind_valid", _bad_rendition_kind)
_shape_probe("[D13] media_renditions.attempt_no 为 0",
             "ck_media_renditions_attempt_no_positive", _bad_rendition_attempt_no)
_shape_probe("[D14] media_renditions READY 但无产物",
             "ck_media_renditions_ready_requires_artifact",
             _bad_rendition_ready_without_artifact)
_shape_probe("[D15] media_generation_contexts.task_kind 取值域外",
             "ck_media_generation_contexts_task_kind_valid", _bad_context_task_kind)
_shape_probe("[D16] media_generation_contexts.idempotency_key 空串",
             "ck_media_generation_contexts_idempotency_key_not_empty",
             _bad_context_key_empty)


# ★ `reference_assets` 是**不可变表** ⇒ `UPDATE` 会被 `rednote_reject_mutation()`
#   先以 23001 拦下，**根本到不了列级 CHECK**。用 UPDATE 写这三条探针，
#   报出来的约束名会是 23001/None —— 结论指向"不可变保护生效"，
#   而**形状约束这一格根本没被观测到**（首轮实测 3 条红全因于此）。
#   正确做法：**直接 INSERT 非法行**（不可变只拦 UPDATE/DELETE，不拦 INSERT）。
def _asset_insert_probe(label, constraint, **kw):
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            ids = _seed(conn)
            _asset_tx(conn, ids, **kw)
            emit(False, label, "竟然写进去了 —— 约束未生效")
            trans.rollback()
    except DBAPIError as exc:
        emit(
            sqlstate(exc) == "23514" and constraint_name(exc) == constraint,
            label,
            f"sqlstate={sqlstate(exc)} constraint={constraint_name(exc)}",
        )
    except Exception as exc:  # noqa: BLE001
        emit(False, label, f"探针异常 {type(exc).__name__}: {exc}")


def _asset_insert_probe_retired(label, constraint):
    # "RETIRED 但无 supersedes" 与 "RETIRED + 有 supersedes" 长得很像，
    # 差别只在那一列 ⇒ 前者必须被拒、后者必须通过（D21 是对照）。
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            ids = _seed(conn)
            _asset_tx(conn, ids, state="RETIRED")
            emit(False, label, "竟然写进去了 —— 约束未生效")
            trans.rollback()
    except DBAPIError as exc:
        emit(
            sqlstate(exc) == "23514" and constraint_name(exc) == constraint,
            label,
            f"sqlstate={sqlstate(exc)} constraint={constraint_name(exc)}",
        )
    except Exception as exc:  # noqa: BLE001
        emit(False, label, f"探针异常 {type(exc).__name__}: {exc}")


_asset_insert_probe("[D17] reference_assets.state 取值域外（INSERT）",
                    "ck_reference_assets_state_valid", state="NOPE")
_asset_insert_probe_retired(
    "[D18] reference_assets RETIRED 但无 supersedes（INSERT）",
    "ck_reference_assets_retired_needs_supersedes",
)
_asset_insert_probe("[D19] reference_assets.product_model 空串（INSERT）",
                    "ck_reference_assets_product_model_not_empty",
                    product_model="")

# D20 对照：合法素材（ACTIVE + 完整字段）⇒ 必须通过。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        _asset_tx(conn, ids, product_model="WP27-对照")
        emit(True, "[D20] 对照：合法参考素材（ACTIVE）⇒ 通过",
             "写入成功（符合预期）")
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D20] 对照：合法参考素材通过", f"探针异常 {type(exc).__name__}: {exc}")

# D21 对照：RETIRED + 带 supersedes 的素材 ⇒ 必须通过（防"见到 RETIRED 就拒"）。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        old = _asset_tx(conn, ids)
        _asset_tx(conn, ids, state="RETIRED", supersedes=old)
        emit(True, "[D21] 对照：RETIRED + supersedes 素材 ⇒ 通过",
             "写入成功（符合预期）")
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D21] 对照：RETIRED + supersedes 通过",
         f"探针异常 {type(exc).__name__}: {exc}")

# D22 对照：良构版本（合法 paragraph_ref / prompt / 形状对的绑定）⇒ 必须通过。
try:
    with engine.connect() as conn:
        trans = conn.begin()
        ids = _seed(conn)
        content_id = _content_tx(conn, ids)
        item = _item_tx(conn, ids, content_id)
        fo = _blob_and_file(conn, ids)
        _pair_tx(conn, ids, item, image_file_id=fo, prompt="一条合法的提示词")
        emit(True, "[D22] 对照：良构版本行 ⇒ 通过", "写入成功（符合预期）")
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "[D22] 对照：良构版本行通过", f"探针异常 {type(exc).__name__}: {exc}")

# ===========================================================================
# P 节 —— 纯函数层（含反向，证明正向判据能区分两种实现）
# ===========================================================================

# P1~P3：顺序对位（老大 2026-10-08 拍板：第 i 张配第 i 段，不足 3 段回落末段）。
try:
    _body = "第一段\n\n第二段\n\n第三段\n\n第四段"
    _refs = media_schemas.paragraph_refs_for_body(_body)
    emit(len(_refs) == 4, "[P1] paragraph_refs_for_body 按空行切出 4 段",
         f"{len(_refs)} 段")
except Exception as exc:  # noqa: BLE001
    emit(False, "[P1] paragraph_refs_for_body 切段", f"探针异常 {type(exc).__name__}: {exc}")

try:
    _one = media_schemas.paragraph_refs_for_body("只有一段")
    _r1 = media_schemas.assign_paragraph_refs(_one)
    emit(_r1 == (_one[0][2],) * 3,
         "[P2] 1 段 ⇒ 三张都落唯一那段（[0,0,0]）", f"{_r1}")
except Exception as exc:  # noqa: BLE001
    emit(False, "[P2] 1 段 ⇒ 三张落唯一段", f"探针异常 {type(exc).__name__}: {exc}")

try:
    _two = media_schemas.paragraph_refs_for_body("A段\n\nB段")
    _r2 = media_schemas.assign_paragraph_refs(_two)
    emit(_r2 == (_two[0][2], _two[1][2], _two[1][2]),
         "[P3] 2 段 ⇒ 第 3 张回落末段（[0,1,1]）", f"{_r2}")
except Exception as exc:  # noqa: BLE001
    emit(False, "[P3] 2 段 ⇒ 第 3 张回落末段", f"探针异常 {type(exc).__name__}: {exc}")

# P4 反向：恒长 3 且与「第 i 张配第 i 段」逐位一致（不是"随便三个 ref"）。
try:
    _three = media_schemas.paragraph_refs_for_body("甲\n\n乙\n\n丙\n\n丁")
    _r3 = media_schemas.assign_paragraph_refs(_three)
    _expect3 = (_three[0][2], _three[1][2], _three[2][2])
    emit(len(_r3) == media_types.images_per_content() and _r3 == _expect3,
         "[P4] ≥3 段 ⇒ 恒长 3 且逐位等于前 3 段",
         f"{_r3} 期望={_expect3}")
except Exception as exc:  # noqa: BLE001
    emit(False, "[P4] ≥3 段逐位对位", f"探针异常 {type(exc).__name__}: {exc}")

# P5 空正文 ⇒ fail-closed（PARAGRAPH_REF_UNKNOWN），**不编 ref、不跳过**。
try:
    try:
        media_schemas.assign_paragraph_refs(())
        emit(False, "[P5] 空段落 ⇒ 必须 fail-closed", "竟然返回了结果")
    except media_schemas.SchemaViolation as exc:
        emit(exc.reason == media_types.REASON_PARAGRAPH_REF_UNKNOWN,
             "[P5] 空段落 ⇒ fail-closed（PARAGRAPH_REF_UNKNOWN）", f"{exc.reason}")
except Exception as exc:  # noqa: BLE001
    emit(False, "[P5] 空段落 fail-closed", f"探针异常 {type(exc).__name__}: {exc}")

# P6 `paragraph_ref_for_sort_order` 与 `assign_paragraph_refs` **同源**
#    （两处各写一份实现会出现"端点算 A、执行期算 B"而**完全静默**）。
try:
    _same = all(
        media_schemas.paragraph_ref_for_sort_order(_three, i) == _r3[i]
        for i in range(media_types.images_per_content())
    )
    emit(_same, "[P6] paragraph_ref_for_sort_order 与 assign 同源（逐位一致）", _same)
except Exception as exc:  # noqa: BLE001
    emit(False, "[P6] 两个入口同源", f"探针异常 {type(exc).__name__}: {exc}")

# P7 越界 sort_order ⇒ fail-closed（**不回落末段** —— 与"段落不足"是两件事）。
try:
    try:
        media_schemas.paragraph_ref_for_sort_order(_three, 9)
        emit(False, "[P7] 越界 sort_order ⇒ 必须 fail-closed", "竟然返回了结果")
    except media_schemas.SchemaViolation as exc:
        emit(exc.reason == media_types.REASON_PARAGRAPH_REF_UNKNOWN,
             "[P7] 越界 sort_order ⇒ fail-closed（不回落末段）", f"{exc.reason}")
except Exception as exc:  # noqa: BLE001
    emit(False, "[P7] 越界 sort_order fail-closed",
         f"探针异常 {type(exc).__name__}: {exc}")

# P8 单源：切段实现只许在 `schemas.py`（service 侧不得有第二份）。
#   两处实现会造出"图刚生成就报 PARAGRAPH_REF_UNKNOWN"的形态。
try:
    _src = (Path.cwd() / "src" / "rednote" / "modules" / "media")
    _service = (_src / "service.py").read_text(encoding="utf-8")
    emit("_PARAGRAPH_SPLIT" not in _service,
         "[P8] 切段实现单源（service.py 不得有第二份 _PARAGRAPH_SPLIT）",
         "service.py 无 _PARAGRAPH_SPLIT")
except Exception as exc:  # noqa: BLE001
    emit(False, "[P8] 切段实现单源", f"探针异常 {type(exc).__name__}: {exc}")

# P9 `images_per_content()` 与 `IMAGES_PER_CONTENT` 同值（预占口径的唯一来源）。
try:
    emit(media_types.images_per_content() == media_types.IMAGES_PER_CONTENT == 3,
         "[P9] images_per_content() == IMAGES_PER_CONTENT == 3",
         f"{media_types.images_per_content()}")
except Exception as exc:  # noqa: BLE001
    emit(False, "[P9] images_per_content 同值", f"探针异常 {type(exc).__name__}: {exc}")

# P10 DDL 派生：`index_names()` 必须由 ORM `__table__.indexes` 派生（不是手写清单）。
#   手写清单会漏，而漏了不报错 —— 只是少查一条（判据纪律第 14 条）。
try:
    _declared = set(media_ddl.index_names()) - WP28_LATER_INDEXES
    _from_orm = {
        ix.name
        for _t in WP27_OWN_TABLES
        for ix in media_models.Base.metadata.tables[_t].indexes
    }
    emit(_declared == _from_orm,
         "[P10] index_names() 与 ORM __table__.indexes 全等（非手写清单）",
         f"ddl={len(_declared)} orm={len(_from_orm)}"
         f" 差={sorted(_declared ^ _from_orm) or '无'}")
except Exception as exc:  # noqa: BLE001
    emit(False, "[P10] index_names 派生自 ORM", f"探针异常 {type(exc).__name__}: {exc}")

# P11 DDL 派生：`trigger_names()` == 不可变 4 + 一致性 4，且无重复。
try:
    _trg = tuple(n for n in media_ddl.trigger_names() if n not in WP28_LATER_TRIGGERS)
    _imm = tuple(
        n for n in media_ddl.immutable_trigger_names() if n not in WP28_LATER_TRIGGERS
    )
    _cons = tuple(
        n for n in media_ddl.consistency_trigger_names() if n not in WP28_LATER_TRIGGERS
    )
    emit(
        len(_trg) == 8 and len(set(_trg)) == 8
        and set(_trg) == set(_imm) | set(_cons) and not (set(_imm) & set(_cons)),
        "[P11] trigger_names() == 不可变 4 + 一致性 4（无重复、无交叠）",
        f"{len(_trg)} 条 {sorted(_trg)}",
    )
except Exception as exc:  # noqa: BLE001
    emit(False, "[P11] trigger_names 组成自洽", f"探针异常 {type(exc).__name__}: {exc}")

# P12 `consistency_items()` 4 项 与 `consistency_*_names()` 等长（判据的一部分）。
try:
    _wp27_items = [i for i in media_ddl.consistency_items() if i[0] not in WP28_LATER_TABLES]
    emit(
        len(_wp27_items) == len(_cons) == 4,
        "[P12] consistency_items()（扣除 WP28 新增）4 项 == consistency_trigger_names() 4 项",
        f"{len(_wp27_items)} / {len(_cons)}",
    )
except Exception as exc:  # noqa: BLE001
    emit(False, "[P12] consistency_items 等长", f"探针异常 {type(exc).__name__}: {exc}")

# P13 `billing_bridge` 的预估与 `images_for` 同源（预占口径单源）。
try:
    _n = media_types.images_for(1)
    emit(_n == 3, "[P13] images_for(1 条内容) == 3（预占口径单源）", f"{_n}")
except Exception as exc:  # noqa: BLE001
    emit(False, "[P13] images_for 口径", f"探针异常 {type(exc).__name__}: {exc}")

# P14 `calls_for` 与 `images_for` **同源**（不是第二份算法）。
#   实测口径：`calls_for(TASK, 1) == images_for(1) == 1 × images_per_content() == 3`。
#   ⚠️ 首轮把期望写成 1 —— 那是把"调用次数"当成了"任务数"。
#   `calls_for` 的 `quantity` 是**按张计费**的张数（billing_bridge 的入参），
#   不是"本批有几个任务"。写错会让判据**恒红**且指向错误结论。
try:
    _c = media_types.calls_for(media_types.TASK_STATIC_IMAGE, 1)
    _expect_c = media_types.images_for(1)
    emit(
        _c == _expect_c == media_types.images_per_content(),
        "[P14] calls_for 与 images_for 同源（1 条内容 ⇒ 3 张）",
        f"calls_for={_c} images_for={_expect_c}",
    )
except Exception as exc:  # noqa: BLE001
    emit(False, "[P14] calls_for 与 images_for 同源", f"探针异常 {type(exc).__name__}: {exc}")

# P15 任务寻址常量（KIND / QUEUE / 幂等域）必须字面量正确 —— 它们是排障口径。
try:
    emit(
        media_types.KIND_MEDIA_GENERATE == "MEDIA_GENERATE"
        and media_types.QUEUE_MEDIA == "media"
        and media_types.IDEMPOTENCY_SCOPE_MEDIA_GENERATE == "media-generate",
        "[P15] 任务寻址三常量字面量正确",
        f"{media_types.KIND_MEDIA_GENERATE}/{media_types.QUEUE_MEDIA}"
        f"/{media_types.IDEMPOTENCY_SCOPE_MEDIA_GENERATE}",
    )
except Exception as exc:  # noqa: BLE001
    emit(False, "[P15] 任务寻址常量", f"探针异常 {type(exc).__name__}: {exc}")

# P16 `active_asset_states()` 是**单例** `(ASSET_STATE_ACTIVE,)` ——
#   ⚠️ 它**不是** `ASSET_STATES`（那个是全体取值域 {ACTIVE, RETIRED}）。
#   `ddl.py` 的 docstring 已说明：它回答的是"登记动作的状态"，
#   不是"某素材当前可用吗"（后者要两列合起来，落在 repository 的单一函数）。
#   首轮把它与 `ASSET_STATES` 等同比 —— 那会把一个**正确实现**判红。
try:
    _aas = media_ddl.active_asset_states()
    emit(
        _aas == (media_types.ASSET_STATE_ACTIVE,)
        and set(_aas) < set(media_types.ASSET_STATES),
        "[P16] active_asset_states() 是单例 {ACTIVE} 且严格包含于 ASSET_STATES",
        f"{_aas} ⊂ {sorted(media_types.ASSET_STATES)}",
    )
except Exception as exc:  # noqa: BLE001
    emit(False, "[P16] active_asset_states 口径", f"探针异常 {type(exc).__name__}: {exc}")

# P17 `parse_paragraph_ref` 与 `paragraph_ref_for` 互逆（ref 的拼法只有一个实现）。
#   ⚠️ `parse_paragraph_ref` 返回**二元组 `(段序号, 哈希)`**，不是裸序号。
#   首轮拿它与整数 2 比 —— 那会把正确实现判红（实到 `(2, 'a0d8cb99')`）。
#   哈希侧也一并核对：它必须是 hash8（8 位十六进制），与 `fragment_hash8` 同源。
try:
    _ref = media_types.paragraph_ref_for(2, "某段正文")
    _idx, _hash = media_types.parse_paragraph_ref(_ref)
    emit(
        _idx == 2 and len(_hash) == 8
        and _hash == media_types.fragment_hash8("某段正文"),
        "[P17] parse/paragraph_ref_for 互逆且哈希与 fragment_hash8 同源",
        f"ref={_ref} -> ({_idx}, {_hash})",
    )
except Exception as exc:  # noqa: BLE001
    emit(False, "[P17] ref 拼法互逆", f"探针异常 {type(exc).__name__}: {exc}")

# P18 生成请求载荷校验：缺 taskKind ⇒ fail-closed（不猜一个默认值）。
try:
    try:
        media_schemas.validate_generation_request_payload({})
        emit(False, "[P18] 缺 taskKind 的载荷 ⇒ 必须 fail-closed", "竟然通过了")
    except media_schemas.SchemaViolation as exc:
        emit(True, "[P18] 缺 taskKind 的载荷 ⇒ fail-closed", f"{exc.reason}")
except Exception as exc:  # noqa: BLE001
    emit(False, "[P18] 载荷校验 fail-closed", f"探针异常 {type(exc).__name__}: {exc}")

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
    emit(_ops == 7, "[K2] 本包契约操作数 == 7", f"{_ops}（6 paths）")
    _all_ops = sum(
        1
        for _p, _item in _paths.items()
        for _m in _item
        if _m.lower() in ("get", "post", "put", "patch", "delete")
    )
    _schemas = len(_spec.get("components", {}).get("schemas", {}))
    # 下界而不是等号（判据纪律第 1 条）：后续包会继续加路径。
    emit(
        len(_paths) >= 77 and _all_ops >= 95,
        "[K3] 全库契约规模下界",
        f"paths={len(_paths)}(>=77) ops={_all_ops}(>=95) schemas={_schemas}",
    )
except Exception as exc:  # noqa: BLE001
    emit(False, "[K] 契约核对", f"探针异常 {type(exc).__name__}: {exc}")

# ===========================================================================
# Z 节 —— 零残留（不可变表一旦 commit 就删不掉，故必须核对）
# ===========================================================================
with engine.connect() as conn:
    for _t in WP27_OWN_TABLES:
        n = conn.execute(text(f"SELECT count(*) FROM {_t}")).scalar_one()
        emit(n == 0, f"[Z] 零残留 {_t}", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM users WHERE issuer = :i"), {"i": PROBE_ISSUER}
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 探针用户", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM projects WHERE name LIKE :p"),
        {"p": f"{PROBE_PROJECT_NAME}%"},
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 探针项目", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM brands WHERE name LIKE :p"),
        {"p": f"{PROBE_BRAND_PREFIX}%"},
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 探针品牌", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM products WHERE name LIKE :p"),
        {"p": f"{PROBE_PRODUCT_PREFIX}%"},
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 探针产品", f"{n} 行")
    # ★ 跨包：探针**不得**在共用表 version_nodes 上留行（本包里它是 FK 目标）。
    n = conn.execute(
        text("SELECT count(*) FROM version_nodes WHERE payload_hash LIKE :p"),
        {"p": f"{PROBE_NODE_HASH_PREFIX}%"},
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 共用表 version_nodes 无探针节点", f"{n} 行")
    # ★ 跨包：探针**不得**在 jobs / file_objects / blobs 上留行。
    n = conn.execute(
        text("SELECT count(*) FROM file_objects WHERE original_name = 'wp27-probe.png'")
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 file_objects 无探针文件", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM blobs WHERE object_key LIKE 'wp27-probe/%'")
    ).scalar_one()
    emit(n == 0, "[Z] 零残留 blobs 无探针对象", f"{n} 行")

print(f"RESULT_EMITTED (期望 {EXPECTED_CHECKS} 条)")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def collect_wp27() -> tuple[Path, bool]:
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
        "# WP27 媒体生成流水线：5 张表 / 40 CHECK / 24 FK / 22 索引"
        " / 4 条延迟 FK / 8 个触发器（7 挂本包表 + 1 挂共用表 version_nodes）"
        " / 库内活性验证 + 纯函数判据 / 契约",
        f"# 生成时间: {_now()}",
        "# 目标库: rednote_verify_wp27（收口时由迁移从 bootstrap 建出，非测试夹具建表）",
        "# 命令: cd services/backend && python -c '<information_schema + 试写 + 试插 + 纯函数>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp27-media-pipeline.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # **条数不足也算失败**（ISS-43：崩溃伪装成"全过"）。
    # **条数变多也算失败**（ISS-153：门禁只拦变少、不拦变多 ⇒ 常量脱钩后照旧印"期望 N 条"）。
    complete = len(checks) >= EXPECTED_CHECKS
    exact = len(checks) == EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete and exact)


def main() -> int:
    path, ok = collect_wp27()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())