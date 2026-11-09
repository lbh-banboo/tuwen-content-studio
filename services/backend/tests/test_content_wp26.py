"""WP26（文案结构生成 + 标题候选 + 采纳）的四层测试。

对应 `superpower-workflow` 阶段 6。与 `test_billing_wp25.py` / `test_planning_wp24.py`
同一范式（1 纯函数 → 2 服务层真库 → 3 原生 SQL 绕过 ORM → 4 HTTP `TestClient`）。

## 四层各测什么（以及为什么不能合并）

| 层 | 测什么 | 手段 | 为什么单独一层 |
|---|---|---|---|
| 1 | 纯函数：`GenerationRequest` 往返与 fail-closed、`normalize_scope` / `task_kinds_for_scope` / `calls_for` 边界、DDL 文本与名单自洽、5 表 38 条 CHECK 名单自洽 | 直接调用，**不碰库** | 快；且"范围归一的边界"必须能被**穷举**证明 —— 一旦它能读库，穷举就要一个数据库 |
| 2 | 服务层编排：`start_generation` 幂等（同键复用/不同体）、`prepare_regeneration` 蓝图配对、`adopt_title_candidate` 追加一版（字段复用规则） | 真库 + `service_*` | "命中即原样返回""投影前进"是**事务语义**，只有在真事务里才成立 |
| 3 | **数据库约束自己会不会拦** | 原生 SQL（绕过 ORM 与 service） | 检查"绕过本层的写入"；若测试也只用 ORM，测试与实现共享同一个盲区 |
| 4 | HTTP 接线：6 条路径、鉴权（`VIEW`/`EDIT`）、202/200/201/403/404/409 | `TestClient` | 端点组装是独立的一层，服务层全绿也可能挂错前缀或漏了 `access.require` |

## ★★ 本文件最重要的实测口径：延迟约束必须**显式**强制检查

本包有 **4 条 `DEFERRABLE INITIALLY DEFERRED`** FK + **3 个 `DEFERRABLE INITIALLY
DEFERRED` CONSTRAINT TRIGGER**。它们的检查发生在**事务提交**时。

这带来一个**测试自身的判别陷阱**（本文件写就前实测踩到）：

> 用 `session.begin_nested()`（savepoint）包住违规写入，**savepoint 释放时不检查**
> ⇒ 违规写入**静默通过** ⇒ 判据变成"NO ERROR"，看起来像"防线不存在"。

**正确做法**（`_expect_sqlstate` 的落地）：savepoint 内写入后，先
`SET CONSTRAINTS ALL IMMEDIATE` 把延迟约束**强制立即检查**，再释放 savepoint。
这样每一条违规都在**自己的 savepoint 里**报错，且失败只回滚那一小段
（不拖垮整个测试用例）。

## ★ 反向陷阱：判据之间的**污染**（同一个坑的另一面）

若"造夹具行"的语句落在 savepoint **外**，而"触发违规"的语句在 savepoint **内**：
违规被回滚、夹具行留下 ⇒ 夹具行违反的一致性判据会**推迟到后续**
`SET CONSTRAINTS ALL IMMEDIATE` 里引爆 ⇒ 后面**每一条**判据都报同一条错。

实测形态：`version_nodes` 留一条孤立 CONTENT 节点后，后续 8 条判据
（含 4 张不可变表的 UPDATE/DELETE）**全部**报同一条
`23514 孤立节点` —— 而它们与那条节点毫无关系。

因此本文件的所有 DB 判据都遵守：
**制造判据所需的一切（节点 + 数据行 + 依赖行）都放在同一个 savepoint 内。**

## ★ 每条 DB 判据的 sqlstate 都是**实测**的，不是推测的

写就前逐条跑过（探针 `/tmp/wp26_behavior_probe4.py` 的口径），实测结果：

| 判据 | sqlstate | constraint |
|---|---|---|
| OP-88 判据 1（节点不存在） | **23503** | `fk_content_versions_id_version_nodes` |
| OP-88 判据 2（`entity_type != CONTENT`） | **23514** | —（触发器 RAISE） |
| OP-88 判据 3（`entity_id` 不匹配） | **23514** | —（触发器 RAISE） |
| 3.1 蓝图跨内容 / 3.2 采纳跨内容 / 3.3 上下文跨内容 | **23514** | —（触发器 RAISE） |
| 反向（孤立 CONTENT 节点） | **23514** | —（触发器 RAISE） |
| 环 1（`current_version_id` 不存在） | **23503** | `fk_content_items_current_version_id_content_versions` |
| 环 2（`generation_context_id` 不存在） | **23503** | `fk_content_versions_generation_context_id_generation_contexts` |
| 不可变 4 表 × UPDATE / DELETE | **23001** | —（`rednote_reject_mutation` RAISE） |
| CHECK ×N | **23514** | `ck_*` |
| 负向 FK ×2（必须**不**延迟） | **23503** | `fk_content_versions_content_item_id_content_items` 等 |

**★ OP-88 判据 1 报 `23503` 而不是 `23514`** 是实测结果，与直觉相反：
`content_versions.id` → `version_nodes.id` 那条 FK **是延迟的**，
在 `SET CONSTRAINTS ALL IMMEDIATE` 时被 **FK 检查先拦**，触发器根本没跑到
"节点不存在"那一句。判据 2/3 里节点**存在**，FK 通过，才轮到触发器。
写断言时若按"触发器 23514"写判据 1，会**恒红**。

## ★ OP-88 判据 2/3 为什么不用约束名断言

那三条判据合在 `rednote_check_content_version_consistency()` **一个函数**里
（`ddl.py` 的 docstring 已论证：同一张表、同一次插入、修法同类）。
触发器 `RAISE EXCEPTION` 不带 `CONSTRAINT` 名 ⇒ `diag.constraint_name` 为空。
能断言的是 **sqlstate**；消息文本是给人读的，**不写进断言**。

## 为什么 `content_versions` 等 4 张表的残留只能用 TRUNCATE 清

4 张表都是**不可变表**（`trg_*_immutable` 拦 UPDATE/DELETE，SQLSTATE `23001`）。
因此第 2/3 层的写入会**累积**在测试库里 ——
需要"表里只有本次的那几行"的判据一律先 `TRUNCATE`（夹具已做），
而不是写 `DELETE`。（行级 BEFORE 触发器不拦 `TRUNCATE`。）

## 夹具为什么断言"**跨包**入向 FK 不变"而不是"入向 FK 为 0"

本包 5 张表**有 9 条入向 FK，但全部是包内 FK**（本机实测）：
`content_items ↔ content_versions` / `content_versions ↔ generation_contexts` /
`content_versions ↔ title_candidates` / `narrative_blueprints → content_items`。

WP24 的 `test_planning_wp24.py` 断言 `inbound == 0` 是对的（它那两张表确实
没有入向 FK）。**照抄会失败**。本包的正确口径是
`cross_package_foreign_keys(engine, DROP_ORDER)` 前后相等 ——
它已排除"引用方也在 `DROP_ORDER` 里"的包内 FK。

⚠️ **WP27 落库后跨包入向 FK 不再是 0 条**：`media_items` 与
`media_generation_contexts` 各有一条 FK 指向 `content_items`（本机实测 2 条）。
这件事有两个后果，都在本文件里处理：
① 夹具的 `TRUNCATE` 必须带上**入向引用闭包**（4 张 media 表），见
   `conftest.truncate_targets` —— 否则**每个用例崩一次**（实测 93 errors，
   而症状看起来像"实现坏了"，排障方向会被误导）；
② `test_cross_package_inbound_foreign_keys_are_whitelisted` 的口径由
   "= 0" 改为"⊆ 已登记白名单"。**新出现跨包 FK 仍会报红** —— 那是这条判据的
   本职：它保护的是"夹具行为变了必须有人当场知道"。

## `REASON_CONTENT_VERSION_NOT_FOUND` 分支在真库上**不可达**（实测）

`adopt_title_candidate` 里 `previous is None ⇒ CONTENT_VERSION_NOT_FOUND`
这一支**到不了**：候选行必然指向一条 `content_versions` 行（FK），
而那条版本行的 `content_item_id` 又必须等于本内容（判据 3.2）⇒
"有候选可采纳"蕴含"本内容必有版本"。

写一条"造出无版本内容 + 有效候选"的用例**不可能**（那正是判据 3.2 要拒的形态）。
本文件因此把它登记为**防御性分支**，不做真库用例 ——
硬造只会造出一个"绕过 DDL 才存在"的状态，而那证明不了这条防线。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from conftest import (
    cross_package_foreign_keys,
    drop_own_tables,
    truncate_targets,
)

# FK 目标表：**必须在 `create_all` 之前 import**，否则 SQLAlchemy 解析
# `ForeignKey("version_nodes.id")` 时找不到表 ⇒ `NoReferencedTableError`。
# 而且 `test_schema_parity.py` 有一条判据断言"模型声明的 FK 库里必须存在"，
# 漏 import 会让**那个文件**失败（不是本文件）—— 排障方向会被误导。
import rednote.modules.baseline.models  # noqa: F401
import rednote.modules.gateway.models  # noqa: F401
import rednote.modules.identity.models  # noqa: F401
import rednote.modules.jobs.models  # noqa: F401
import rednote.modules.projects.models  # noqa: F401
from rednote.modules.content import ddl as c_ddl
from rednote.modules.content import service as c_service
from rednote.modules.content.models import (
    ContentItem,
    ContentVersion,
    GenerationContext,
    NarrativeBlueprint,
    TitleCandidate,
)
from rednote.modules.content.repository import ContentRepository
from rednote.modules.content.types import (
    BINDING_KEYS,
    BLUEPRINT_FIELDS,
    CONTENT_SCHEMA_VERSION,
    CONTENT_STATES,
    CONTENT_STATE_DRAFT,
    CONTENT_STATE_GENERATED,
    CONTENT_STATE_LIST_SQL,
    COPY_FIELDS,
    GENERATION_REQUEST_KEYS,
    IDEMPOTENCY_SCOPE_CONTENT_GENERATE,
    KIND_CONTENT_GENERATE,
    MAX_BODY_CHARS,
    MAX_TITLE_CANDIDATES,
    MIN_TITLE_CANDIDATES,
    QUEUE_CONTENT,
    REGENERATION_SCOPES,
    REASON_CONTENT_NOT_FOUND,
    REASON_GENERATION_REQUEST_INVALID,
    REASON_PROJECTION_CONFLICT,
    REASON_SCOPE_REQUIRES_BLUEPRINT,
    REASON_TITLE_CANDIDATE_MISMATCH,
    REASON_TITLE_CANDIDATE_NOT_FOUND,
    REQUEST_KEY_BATCH_KIND,
    REQUEST_KEY_MODEL_PROFILE_ID,
    REQUEST_KEY_SCOPE,
    SCOPE_COPY,
    SCOPE_FULL,
    SCOPE_TAGS,
    SCOPE_TASK_KINDS,
    SCOPE_TITLE,
    TASK_CONTENT_COPY,
    TASK_CONTENT_STRUCTURE,
    TASK_KINDS,
    TITLE_SCORE_DIMENSIONS,
    GenerationRequest,
    calls_for,
    normalize_scope,
    task_kinds_for_scope,
)
from rednote.modules.gateway.repository import GatewayRepository
from rednote.modules.gateway.types import (
    CAPABILITY_TEXT_STRUCTURED,
    NETWORK_EXTERNAL,
)
from rednote.modules.identity.models import ROLE_ADMIN, ROLE_OPERATOR
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.projects.models import Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.modules.versions.repository import VersionRepository
from rednote.platform.base import Base
from rednote.modules.versions.service import register_version
from rednote.platform.configuration import Settings
from rednote.platform.database import get_engine, reset_engines, session_scope
from rednote.platform.immutability import IMMUTABLE_RECORD_SQLSTATE

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
CSRF_SECRET_PLAINTEXT = "test-csrf-secret-plaintext"
TEST_ORIGIN = "http://testserver"

#: 仓库内 `services/backend/` 的绝对路径 —— 给"读源码自查"型判据用。
#: 从本文件（`services/backend/tests/`）上溯两级，**不写死绝对路径**
#: （写死会让整仓挪位置时判据静默指向不存在的目录）。
SERVICES_BACKEND = Path(__file__).resolve().parents[1]

#: 本包自己的 5 张表。
OWN_TABLES: tuple[Any, ...] = (
    ContentItem.__table__,
    ContentVersion.__table__,
    NarrativeBlueprint.__table__,
    TitleCandidate.__table__,
    GenerationContext.__table__,
)

#: DROP 顺序：**引用方在前**（行级触发器之外的第二重保险）。
#: 三条链：`content_versions → generation_contexts`（延迟环 2）、
#: `content_versions → title_candidates`（环 3，延迟）、
#: `content_versions → narrative_blueprints`。
#: `content_items.current_version_id → content_versions` 是环 1（延迟），
#: 两表互相引用，先后都会撞 FK —— 靠延迟约束兜住（`drop_own_tables` 已按
#: "没有跨包入向 FK"筛过一遍）。
DROP_ORDER: tuple[str, ...] = (
    "title_candidates",
    "generation_contexts",
    "narrative_blueprints",
    "content_versions",
    "content_items",
)

#: 本包对"跨包"的定义域 —— 给 `cross_package_foreign_keys` 用。
_TABLE_NAMES: tuple[str, ...] = (
    "content_items",
    "content_versions",
    "narrative_blueprints",
    "title_candidates",
    "generation_contexts",
)

#: 真库实测的延迟 FK（`condeferrable=t, condeferred=t`），逐条点名。
#: ★ 恰好 4 条 —— 多一条少一条都要在这里改，改的时刻就是"有人动了环"。
DEFERRED_FOREIGN_KEYS: tuple[str, ...] = (
    "fk_content_items_current_version_id_content_versions",
    "fk_content_versions_generation_context_id_generation_contexts",
    "fk_content_versions_id_version_nodes",
    "fk_generation_contexts_content_version_id_content_versions",
)

#: ★ 必须**不**延迟的两条（负向用例）。
#: 延迟它们会让环 FK 的检查宽到事务末尾 ⇒ 两条本该当场拒的写会通过。
#: 逐条点名的价值：将来有人"顺手把本包所有 FK 都设成延迟"时能当场看见。
NON_DEFERRED_FOREIGN_KEYS: tuple[str, ...] = (
    "fk_content_versions_content_item_id_content_items",
    "fk_title_candidates_content_version_id_content_versions",
)

#: 环 FK（`op.create_foreign_key` 后置补的那 3 条）。
CYCLE_FOREIGN_KEYS: tuple[str, ...] = DEFERRED_FOREIGN_KEYS[:3]

#: 表 → 用于"不可变 UPDATE"判据的**真实存在列**。
#:
#: 为什么用真实列：写 `SET note = note` 而该表没有 `note` 列时，会先撞
#: `42703`（列不存在）——**没走到触发器**却返回了一个"非 23001 的 sqlstate"，
#: 判据会以"防线失效"的形式报红，而真实原因是列名写错
#: （`test_billing_wp25.py` 已记过同一个坑）。
_IMMUTABLE_UPDATE_COLUMN: dict[str, str] = {
    "content_versions": "title",
    "narrative_blueprints": "context",
    "title_candidates": "title",
    "generation_contexts": "task_kind",
}

#: 本包 5 表上的 6 个触发器（4 不可变 + 2 一致性）。
#: `trg_version_nodes_content_paired` **不在本包表上**（挂在 `version_nodes`），
#: 因此第 3 层的"真库触发器"判据分两句各自成立（见 `test_*_trigger_*`）。
_OWN_TABLE_TRIGGERS: tuple[str, ...] = (
    "trg_content_versions_immutable",
    "trg_narrative_blueprints_immutable",
    "trg_title_candidates_immutable",
    "trg_generation_contexts_immutable",
    "trg_content_versions_node_consistency",
    "trg_generation_contexts_content_consistency",
)
#: 挂在 `version_nodes` 上的那一个（反向判据）。
_VERSION_NODES_TRIGGER = "trg_version_nodes_content_paired"

#: 真库实测的 40 条（含接缝 §5.15 新增 2 条） CHECK，按表分组（供第 3 层核对）。
_EXPECTED_CHECKS: dict[str, tuple[str, ...]] = {
    "content_items": (
        "ck_content_items_form_key_not_blank",  # 接缝 §5.15 新增
        "ck_content_items_scene_key_not_blank",  # 接缝 §5.15 新增
        "ck_content_items_state_valid",
        "ck_content_items_title_not_blank",
        "ck_content_items_version_no_non_negative",
    ),
    "content_versions": (
        "ck_content_versions_bindings_is_array",
        "ck_content_versions_body_length",
        "ck_content_versions_body_not_empty",
        "ck_content_versions_payload_is_object",
        "ck_content_versions_schema_version_positive",
        "ck_content_versions_tags_is_array",
        "ck_content_versions_title_length",
        "ck_content_versions_title_not_empty",
        "ck_content_versions_version_no_positive",
    ),
    "narrative_blueprints": (
        "ck_narrative_blueprints_conclusion_strength_valid",
        "ck_narrative_blueprints_context_length",
        "ck_narrative_blueprints_context_not_empty",
        "ck_narrative_blueprints_decision_process_length",
        "ck_narrative_blueprints_decision_process_not_empty",
        "ck_narrative_blueprints_evidence_beats_length",
        "ck_narrative_blueprints_evidence_beats_not_empty",
        "ck_narrative_blueprints_interaction_question_length",
        "ck_narrative_blueprints_interaction_question_not_empty",
        "ck_narrative_blueprints_limitation_length",
        "ck_narrative_blueprints_limitation_not_empty",
        "ck_narrative_blueprints_opening_function_length",
        "ck_narrative_blueprints_opening_function_not_empty",
        "ck_narrative_blueprints_paragraph_plan_length",
        "ck_narrative_blueprints_paragraph_plan_not_empty",
        "ck_narrative_blueprints_schema_version_positive",
        "ck_narrative_blueprints_usage_scene_length",
        "ck_narrative_blueprints_usage_scene_not_empty",
    ),
    "title_candidates": (
        "ck_title_candidates_candidate_index_non_negative",
        "ck_title_candidates_highlight_length",
        "ck_title_candidates_keywords_is_array",
        "ck_title_candidates_risk_note_length",
        "ck_title_candidates_scores_is_object",
        "ck_title_candidates_title_length",
        "ck_title_candidates_title_not_empty",
    ),
    "generation_contexts": ("ck_generation_contexts_task_kind_valid",),
}


def _test_database_url() -> str:
    """DSN 从环境变量取（分支用 `rednote_wp26` 私有库），缺省本机 postgres。

    形态必须与 `tools/quality/check_test_db_isolation.py` 的两跳判定一致：
    **先命名常量 `DEFAULT_TEST_DATABASE_URL`，再 `os.environ.get(...)` 取它
    并 `.strip()`**。写成别的等价形态（例如内联字面量、或 `or` 兜底）
    会被那条判据判为"硬编码测试 DSN"。
    """
    return os.environ.get(
        "REDNOTE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL
    ).strip()


def _make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": _test_database_url(),
        "environment": "test",
        "session_cookie_secure": False,
        "oidc_issuer": TEST_ISSUER,
        "oidc_token_encryption_key": TEST_ENCRYPTION_KEY,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ===========================================================================
# 建表夹具（module 级）
# ===========================================================================


@pytest.fixture(scope="module", autouse=True)
def _ensure_own_tables() -> Iterator[None]:
    """只建本包的 5 张表 + 7 个触发器。

    ## 为什么 `create_all` 之前必须 import FK 目标表

    `Base.metadata.create_all(tables=[...])` **只注册传入的那几张表**。
    本包 5 张表引用了 `projects` / `users` / `version_nodes` /
    `prompt_templates` / `prompt_versions` / `production_baselines` /
    `model_profiles` / `provider_calls` —— 那些表不在 `tables=` 里，
    SQLAlchemy 解析 `ForeignKey` 时会抛 `NoReferencedTableError`。
    因此文件顶部 import 了五个 `models` 模块。

    ## ★ 为什么**不**用 `metadata.sorted_tables`

    本仓有若干模块的模型（例如 `notifications`）引用了**不在本仓**的表
    （`domain_events`）⇒ 触碰 `metadata.sorted_tables` 会抛
    `NoReferencedTableError: ... could not find table 'domain_events'`。
    `create_all(tables=[5 张表])` 本身不做全库拓扑排序，因此不受影响
    （本机已实测）。

    ## ★ 入向 FK 断言的口径（**不要照抄 WP24**）

    WP24 断言 `inbound == 0`。本包**有 9 条包内入向 FK**（本机实测）。
    所以正确口径是"**跨包**入向 FK 集合不变"——
    `cross_package_foreign_keys` 已排除引用方也在 `DROP_ORDER` 里的包内 FK。

    ⚠️ **WP27 落库后跨包入向 FK = 2 条**（`media_items` /
    `media_generation_contexts` → `content_items`）。于是上方
    `fks_before == fks_after` 的断言**依然成立、且变得更有意义**：
    它现在真正在守"夹具没有增删这 2 条 FK"。

    ## 为什么 TRUNCATE 不加 CASCADE

    加了 CASCADE 会在有跨包入向 FK 时**静默清空引用方表**（ISS-106 实测过的
    破坏形态）。不写 CASCADE 让那种情况变成**当场报错** —— WP27 落库时它如期
    报错了，只是"报错"发生在夹具里而非断言里（93 errors）。
    本文件因此改为**显式列出入向引用闭包**（`conftest.truncate_targets`）：
    既不静默（清了谁看得见），也不崩（闭包自动跟上新增引用）。
    `drop_own_tables` 也已按"被引用的表不删"筛过一遍，两条互为保险。
    """
    reset_engines()
    engine = get_engine(_make_settings())

    fks_before = cross_package_foreign_keys(engine, _TABLE_NAMES)
    dropped, kept = drop_own_tables(engine, DROP_ORDER)

    ContentItem.metadata.create_all(engine, tables=list(OWN_TABLES))

    fks_after = cross_package_foreign_keys(engine, _TABLE_NAMES)
    assert fks_after == fks_before, (
        f"建表夹具改变了跨包外键集合：before={sorted(fks_before)} "
        f"after={sorted(fks_after)}（dropped={dropped} kept={kept}）"
    )

    # ⚠️ 目标清单必须含**入向引用闭包**（WP27 落库后多出 4 张 media 表）。
    #    只列 `DROP_ORDER` 会撞
    #    `cannot truncate a table referenced in a foreign key constraint`
    #    —— 且 PG 要的是**整条链**（`media_versions` 又引用 `media_items`），
    #    补一层不够。闭包由 `confrelid` 递归算出，不写名单（见 conftest）。
    truncate_list = truncate_targets(engine, DROP_ORDER)

    with engine.begin() as conn:
        # 顺序在触发器之前：此刻还没挂触发器；即便挂着也没关系
        # —— TRUNCATE 不触发行级 BEFORE 触发器。
        conn.execute(text("TRUNCATE " + ", ".join(truncate_list)))

        for (
            _table,
            func_ddl,
            trigger_ddl,
            drop_trigger,
            _drop_func,
        ) in c_ddl.all_trigger_ddl():
            conn.execute(text(drop_trigger))  # 幂等关键：CREATE TRIGGER 不幂等
            conn.execute(text(func_ddl))
            conn.execute(text(trigger_ddl))
    yield


@pytest.fixture(autouse=True)
def _clean_own_rows() -> Iterator[None]:
    """**每个用例前**清空本包 5 张表（autouse ⇒ 先于 `env` 执行）。

    ## 为什么 module 级那一次 TRUNCATE 不够

    本文件里有若干判据**看全表**（例：`list_contents` 传一个项目看它全部内容；
    `adopt` 之后数 `generation_contexts` 的行数）。不清的话它们会看到
    **兄弟用例留下的行** —— 症状与"实现真的错了"在输出上**完全同形**。

    正确处置是**让起点干净**，而不是收窄断言范围（那会丢掉判据的价值）。

    ## 为什么用 TRUNCATE 而不是 DELETE

    4 张表是不可变表（行级触发器拦 UPDATE/DELETE，返回 `23001`）。
    只有 TRUNCATE 能绕过行级触发器。清空顺序按引用关系倒序。
    """
    reset_engines()
    engine = get_engine(_make_settings())
    # 同 module 级夹具：必须带上入向引用闭包（WP27 的 4 张 media 表）。
    truncate_list = truncate_targets(engine, DROP_ORDER)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(truncate_list)))
    yield


# ===========================================================================
# 数据夹具（自包含，不依赖 conftest 的隐式状态）
# ===========================================================================


@dataclass
class ContentEnv:
    settings: Settings
    project_id: uuid.UUID
    owner_id: uuid.UUID
    admin_id: uuid.UUID
    #: 模型档案（`generation_request.modelProfileId` 指向它）
    provider_id: uuid.UUID
    model_profile_id: uuid.UUID
    #: 另一套项目，用于"跨项目隔离"反例
    other_project_id: uuid.UUID
    #: **不是任何项目成员**的运营 —— 用于验证 404（防项目存在性枚举）
    outsider_id: uuid.UUID


def _create_user(
    settings: Settings, *, role: str = ROLE_OPERATOR, name: str = "Content 测试用户"
) -> uuid.UUID:
    with session_scope(settings) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name=name,
            role=role,
        )
        return user.id


def _seed_project(settings: Settings, *, owner_id: uuid.UUID) -> uuid.UUID:
    """造 Brand → Product → Project（含 owner 成员关系），返回 `project_id`。"""
    with session_scope(settings) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name=f"产品-{uuid.uuid4().hex[:6]}")
        session.add(product)
        session.flush()
        project = Project(
            project_code=f"CNT-{uuid.uuid4().hex[:8].upper()}",
            name="内容测试项目",
            brand_id=brand.id,
            product_id=product.id,
            owner_id=owner_id,
            target_delivery_count=10,
            due_date=date(2030, 12, 31),
        )
        session.add(project)
        session.flush()
        ProjectRepository(session).add_owner_and_collaborators(
            project.id, owner_id=owner_id, collaborator_ids=()
        )
        return project.id


def _seed_provider(settings: Settings) -> tuple[uuid.UUID, uuid.UUID]:
    """造 Provider → ModelProfile，返回 `(provider_id, model_profile_id)`。"""
    with session_scope(settings) as session:
        repo = GatewayRepository(session)
        provider = repo.insert_provider(
            name=f"供应商-{uuid.uuid4().hex[:8]}",
            capability_types=[CAPABILITY_TEXT_STRUCTURED],
            network_mode=NETWORK_EXTERNAL,
            endpoint="https://api.test.invalid/v1",
        )
        session.flush()
        profile = repo.insert_model_profile(
            provider_id=provider.id,
            model_name=f"model-{uuid.uuid4().hex[:6]}",
            capabilities=[CAPABILITY_TEXT_STRUCTURED],
        )
        session.flush()
        return provider.id, profile.id


@pytest.fixture
def env() -> ContentEnv:
    reset_engines()
    settings = _make_settings()
    owner = _create_user(settings)
    admin = _create_user(settings, role=ROLE_ADMIN, name="Content 测试管理员")
    outsider = _create_user(settings, name="Content 非成员")
    project_id = _seed_project(settings, owner_id=owner)
    other_project_id = _seed_project(settings, owner_id=owner)
    provider_id, profile_id = _seed_provider(settings)
    return ContentEnv(
        settings=settings,
        project_id=project_id,
        owner_id=owner,
        admin_id=admin,
        provider_id=provider_id,
        model_profile_id=profile_id,
        other_project_id=other_project_id,
        outsider_id=outsider,
    )


# --- 2.0 基础设施 ----------------------------------------------------------


def _raw(env: ContentEnv, sql: str, params: dict[str, Any] | None = None) -> Any:
    """在**独立事务**里执行一条原生 SQL 并提交。返回结果行（若有）。"""
    with session_scope(env.settings) as session:
        result = session.execute(text(sql), params or {})
        return result.fetchall() if result.returns_rows else None


def _scalar(env: ContentEnv, sql: str, params: dict[str, Any] | None = None) -> Any:
    with session_scope(env.settings) as session:
        return session.execute(text(sql), params or {}).scalar_one_or_none()


def _sqlstate(exc: DBAPIError) -> str:
    return getattr(getattr(exc, "orig", None), "sqlstate", "") or ""


def _constraint_name(exc: DBAPIError) -> str:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", "") or ""


@contextmanager
def _repo(env: ContentEnv) -> Iterator[ContentRepository]:
    """一次事务里的 `ContentRepository`（退出即提交）。"""
    with session_scope(env.settings) as session:
        yield ContentRepository(session)


@contextmanager
def _repos(env: ContentEnv) -> Iterator[tuple[ContentRepository, VersionRepository]]:
    """一次事务里的 `(ContentRepository, VersionRepository)` —— 同一 session。

    `adopt_title_candidate` 要两个仓库共 session（`dependencies.py` 已论证：
    节点落在 A session、版本落在 B session ⇒ 反向触发器报错）。
    """
    with session_scope(env.settings) as session:
        yield ContentRepository(session), VersionRepository(session)


def _generation_request(
    *, scope: str = SCOPE_COPY, batch_kind: str = "TRIAL",
    model_profile_id: uuid.UUID | None = None,
) -> GenerationRequest:
    return GenerationRequest(
        scope=scope,
        batch_kind=batch_kind,
        model_profile_id=model_profile_id or uuid.uuid4(),
    )


def _expect_sqlstate(
    env: ContentEnv, sql: str, params: dict[str, Any] | None = None, *,
    prepare: str | Sequence[str] | None = None,
    prepare_params: dict[str, Any] | None = None,
) -> str:
    """执行一条**应当失败**的原生 SQL，返回 sqlstate。

    ## ★ 为什么需要 `SET CONSTRAINTS ALL IMMEDIATE`

    本包的 4 条延迟 FK 与 3 个延迟 CONSTRAINT TRIGGER 只在**事务提交**时检查。
    用裸 `session_scope` 跑违规写入，异常会在**退出时**抛出，
    且此时**已经无法区分是哪一句**造成的。

    更糟的是 `begin_nested()`（savepoint）**不触发**延迟约束检查 ⇒
    违规写入静默通过（写就前实测：全部判据报 "NO ERROR"）。

    因此这里显式 `SET CONSTRAINTS ALL IMMEDIATE`：把本事务剩余的延迟约束
    **强制立即检查**。此时违规当场抛出，异常与语句一一对应。

    ## `prepare` 参数的用途

    有些判据需要"先造一行合法数据"（例如不可变表的 UPDATE 用例要先有行）。
    这些准备工作**必须与触发语句在同一事务内** —— 否则失败回滚后，
    准备行留下的状态（例：孤立 `version_nodes` 节点）会让**后续每条判据**
    都报同一条不相关的错（写就前实测污染了 8 条）。

    用 `session_scope` 包住两句即可：异常时整个事务回滚，不留残余。

    ## ★★ 为什么 `SET CONSTRAINTS ALL IMMEDIATE` 在**触发语句之后**

    顺序必须是 **prepare → 触发语句 → 强制检查**。

    若把强制检查放在触发语句**之前**，它会顺带检查 prepare 阶段
    挂起的约束 —— 而 prepare 造的是**半成品**（例：只造了
    `version_nodes` 节点、还没造配对的版本行）⇒
    `trg_version_nodes_content_paired`（延迟）当场报 `23514 孤立节点`，
    **触发语句根本没机会执行**。全部"期望 23503"的判据会
    以 23514 报红，看起来像 FK 不存在（写就后实测：5 条同时红）。

    放在触发语句**之后**就没有这个问题：
    触发语句执行时延迟约束仍挂起（不早拦，这正是被测行为），
    执行完后状态已完整，此时强制检查才结算。

    ## ★ 为什么 `prepare` **逐条** `execute`，而不是拼成一条多语句

    实测（本机 psycopg 3 + PG17，见会话记录）：

        execute(text("SELECT 1; SELECT 2"), {})        → OK
        execute(text("SELECT :x; SELECT 2"), {"x": 1}) → 42601
            "cannot insert multiple commands into a prepared statement"

    **带参数绑定时 PG 走 extended protocol（prepared statement），
    而 prepared statement 只容许一条命令。** 于是所有"多语句 + 有参数"
    的 prepare 都会以 `42601`（语法错）失败 ——
    它看起来像 SQL 写错了，实际是**协议限制**，极易误诊。

    `42601` 还有个副作用：它**不是** 23503/23514，
    于是判据会以"防线失效"的形式报红，把排障方向引到 DDL 上。

    因此 `prepare` 接受**语句序列**，逐条 `execute`（共用一份参数字典）。
    多余参数键是被容忍的（实测 `SELECT :x` 配 3 个键 OK），
    所以调用方不必为了"少给一个键"而按表分支。
    """
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            for statement in _as_statements(prepare):
                session.execute(text(statement), prepare_params or {})
            session.execute(text(sql), params or {})
            # ★ 必须放在触发语句**之后**（理由见 docstring）
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    return _sqlstate(caught.value)


def _expect_constraint(
    env: ContentEnv, sql: str, params: dict[str, Any] | None = None, *,
    prepare: str | Sequence[str] | None = None,
    prepare_params: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """同 `_expect_sqlstate`，但**同时**返回 `(sqlstate, constraint_name)`。"""
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            for statement in _as_statements(prepare):
                session.execute(text(statement), prepare_params or {})
            session.execute(text(sql), params or {})
            # ★ 必须放在触发语句**之后**（理由见 `_expect_sqlstate` docstring）
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    return _sqlstate(caught.value), _constraint_name(caught.value)


def _as_statements(prepare: str | Sequence[str] | None) -> tuple[str, ...]:
    """把 `prepare` 归一成"语句元组"。

    `str` ⇒ 单条（绝大多数判据只需造一行）；
    `Sequence[str]` ⇒ 逐条（依赖链：内容 → 节点 → 版本 → 目标行）。

    ★ **不按 `;` 拆分** —— 本包 SQL 里有 `'[]'::jsonb` 这类字面量，
    按分号裸切会在将来某条语句含分号时**静默切错**。
    调用方显式给序列，就没有这个猜测。
    """
    if prepare is None:
        return ()
    if isinstance(prepare, str):
        return (prepare,)
    return tuple(prepare)


#: 插一条 `version_nodes` 行（OP-88 判据的"节点"）。
#:
#: ★ `id` **必须显式给**（不是靠 `gen_random_uuid()` 默认值）。
#: 理由：本仓的 `content_versions.id` 与 `version_nodes.id` **同值**
#: （`models.py` 模块 docstring 第二节原文）——
#: `version_nodes.id = content_versions.id` 且
#: `version_nodes.entity_id = content_versions.content_item_id`。
#: 让服务端随机生成节点 id，就等于造出"版本行引用了不存在的节点"，
#: 那条延迟 FK（`fk_content_versions_id_version_nodes`）会在
#: `SET CONSTRAINTS ALL IMMEDIATE` 时当场炸 23503 ——
#: 而它会**污染后续每一条判据**（全部改报这条无关的 FK 错）。
#: 这正是写就前实测踩到的坑，所以这里把 id 变成必填参数。
_INSERT_NODE = (
    "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
    " payload_hash, schema_version)"
    " VALUES (:node_id, :entity_type, :entity_id, :revision_no, :payload_hash, 1)"
)

#: 插一条 `content_items` 行。
#:
#: ★ 占位符是 `:content_id` 而**不是** `:id` —— 这不是风格问题：
#: `_prepare_row_sql()` 会把本条与 `_INSERT_VERSION` 拼成**一条**多语句
#: SQL 交给 psycopg。两条若同名 `:id`，psycopg 会把**同一个值**绑到
#: 两处（它按名字绑定），于是"内容行的 id"与"版本行的 id"被迫相等。
#: 换个全局唯一的名字，这个错误就写不出来。
_INSERT_ITEM = (
    "INSERT INTO content_items (id, project_id, state, current_version_no,"
    " lock_version) VALUES (:content_id, :project_id, 'DRAFT', 0, 0)"
)

#: 插一条合法的 `content_versions` 行（`id` 必须 == 某个 `version_nodes.id`）。
#:
#: ★ 三个占位符的名字与 `_prepare_row_params()` 的键**逐一对齐**
#: （`:version_id` / `:content_id` / `:node_id`），不留任何
#: `.replace()` 改名步骤 —— 漏改一次的报错是
#: `A value is required for bind parameter 'id'`，
#: 发生在 SQLAlchemy 绑定期，看不出是哪条语句写错。
_INSERT_VERSION = (
    "INSERT INTO content_versions (id, content_item_id, version_no, title, body,"
    " tags, bindings, payload, payload_hash, schema_version)"
    " VALUES (:version_id, :content_id, :version_no, '标题', '正文',"
    " '[]'::jsonb, '[]'::jsonb, '{}'::jsonb, :payload_hash, 1)"
)

#: `_prepare_row_sql()` 专用：造一条与目标版本行**配对**的 `version_nodes` 行。
#:
#: ★ 与 `_INSERT_NODE` 分开的原因：`.replace()` 式的占位符改名
#: （`:entity_id` → `:content_id`）会漏改 —— 漏了就是
#: `A value is required for bind parameter 'entity_id'`，
#: 报错发生在 **SQLAlchemy 绑定期**（不是数据库），排障方向容易被引偏。
#: 这里把占位符直接写成参数名，没有可漏的映射。
_INSERT_PREP_NODE = (
    "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
    " payload_hash, schema_version)"
    " VALUES (:node_id, 'CONTENT', :content_id, 1, :payload_hash, 1)"
)

#: 插一条 `title_candidates` 行。
_INSERT_CANDIDATE = (
    "INSERT INTO title_candidates (content_version_id, candidate_index, title,"
    " scores, keywords) VALUES (:content_version_id, :candidate_index, :title,"
    " '{}'::jsonb, '[]'::jsonb) RETURNING id"
)


def _seed_valid_content(
    env: ContentEnv, *, project_id: uuid.UUID | None = None,
    blueprint_id: uuid.UUID | None = None,
    generation_context_id: uuid.UUID | None = None,
    body: str = "正文",
    tags: str = "'[]'::jsonb",
    bindings: str = "'[]'::jsonb",
) -> tuple[uuid.UUID, uuid.UUID]:
    """造"一条内容 + 一版合法版本"（含节点）。返回 `(content_id, version_id)`。

    这两行是后续多条判据的共同底料，因此**先提交**（不是 prepare 参数）：
    它们本身合法，不会污染任何判据。

    ## ★ 为什么可选字段要用**参数**而不是造完再 `UPDATE`

    `content_versions` **不可变** —— 造完再 `UPDATE ... SET blueprint_id = …`
    会撞 `rednote_reject_mutation`（`23001`）。
    实测（写就后首轮）就踩了这个：测试想在铺底阶段补 `blueprint_id` /
    `generation_context_id`，结果整条用例以 `RestrictViolation` 失败，
    看起来像"采纳功能坏了"，其实是**测试自己在改不可变表**。

    因此这些字段必须**在 INSERT 那一刻就给全**。

    ### 关于 `tags` / `bindings`

    收**字面量 SQL**（例如 `"'[]'::jsonb"`）而不是 Python 列表：
    这两列是 `jsonb`，而本文件的原生 SQL 参数不做过类型推断，
    传 Python 值会得到 `IndeterminateDatatype`（见 `_raw` 的说明）。
    """
    content_id = uuid.uuid4()
    version_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(text(_INSERT_ITEM), {
            "content_id": content_id, "project_id": project_id or env.project_id,
        })
        session.execute(text(_INSERT_NODE), {
            "node_id": version_id, "entity_type": "CONTENT",
            "entity_id": content_id,
            "revision_no": 1, "payload_hash": f"h-{version_id.hex[:12]}",
        })
        session.execute(
            text(
                "INSERT INTO content_versions (id, content_item_id, version_no,"
                " title, body, tags, bindings, payload, payload_hash,"
                " schema_version, blueprint_id, generation_context_id)"
                f" VALUES (:version_id, :content_id, 1, '标题', :body,"
                f" {tags}, {bindings}, '{{}}'::jsonb, :payload_hash, 1,"
                " :blueprint_id, :generation_context_id)"
            ),
            {
                "version_id": version_id, "content_id": content_id,
                "body": body,
                "payload_hash": f"ph-{version_id.hex[:12]}",
                "blueprint_id": blueprint_id,
                "generation_context_id": generation_context_id,
            },
        )
    return content_id, version_id


def _seed_candidate(
    env: ContentEnv, *, version_id: uuid.UUID, title: str = "候选标题",
    index: int = 0,
) -> uuid.UUID:
    """给某条版本行挂一个标题候选，返回候选 id。"""
    row = _raw(
        env, _INSERT_CANDIDATE,
        {"content_version_id": version_id, "candidate_index": index, "title": title},
    )
    return row[0][0]


# ===========================================================================
# 第 1 层：纯函数（不碰库）
# ===========================================================================


def test_task_kind_constants_match_prompts() -> None:
    """`TASK_KINDS` 恰好两个，且与 `SCOPE_TASK_KINDS` 的取值域一致。

    ## 为什么这条必须存在

    `TASK_KIND` 是**跨包标识符**：`prompts` 模块按它存提示词模板、
    `generation_contexts.task_kind` 按它落库、`resolve_active_version`
    按它查 active 版本。本包的常量与 `prompts` 的不一致时，
    症状是"生成时提示词模板找不到"—— 报错点离根因隔了两个模块。

    断言**取值域**（两个 kind 都出现、且没有第三个）而不是逐个比对字符串：
    比对字符串只能证明"我写的等于我写的"。
    """
    assert TASK_KINDS == frozenset({TASK_CONTENT_COPY, TASK_CONTENT_STRUCTURE})
    assert len(TASK_KINDS) == 2
    # 每个 task_kind 至少被一个 scope 用到（否则它是死值）
    used = {kind for kinds in SCOPE_TASK_KINDS.values() for kind in kinds}
    assert used == TASK_KINDS, (
        f"`SCOPE_TASK_KINDS` 用到的 kind={sorted(used)} 与 `TASK_KINDS`="
        f"{sorted(TASK_KINDS)} 不等 —— 有 kind 永远不会被跑到"
    )


def test_scope_task_kinds_covers_every_scope() -> None:
    """`SCOPE_TASK_KINDS` 的键集 == `REGENERATION_SCOPES`（**双向**）。

    只判"每个 scope 都有映射"会漏掉"映射里有已经不存在的 scope"——
    后者是死键，它让 `task_kinds_for_scope` 对一个**不会到来**的输入
    有答案，而排障时会先去查那个不存在的 scope。
    """
    assert set(SCOPE_TASK_KINDS) == set(REGENERATION_SCOPES)
    assert REGENERATION_SCOPES == frozenset(
        {SCOPE_COPY, SCOPE_FULL, SCOPE_TAGS, SCOPE_TITLE}
    )


def test_normalize_scope_maps_tags_to_copy() -> None:
    """★ `TAGS` 归一为 `COPY`，其余三个原样。

    ## 为什么 TAGS → COPY 是**正确**的而不是权宜

    "只重做标签"与"只重做正文"跑的是**同一个** `task_kind`
    （`CONTENT_COPY` 一次产出标题 + 正文 + 标签三件）。
    因此"仅标签"在任务层与"仅正文"无法区分 —— 它不是一个新的取值范围，
    而是 COPY 的一种**说法**。

    响应里回带归一后的值（`COPY`）而不是原值（`TAGS`）：
    回带原值会让前端以为"系统支持只改标签"，而实际产出的三件里
    正文也变了。
    """
    assert normalize_scope(SCOPE_TAGS) == SCOPE_COPY
    assert normalize_scope(SCOPE_COPY) == SCOPE_COPY
    assert normalize_scope(SCOPE_FULL) == SCOPE_FULL
    assert normalize_scope(SCOPE_TITLE) == SCOPE_TITLE


def test_normalize_scope_rejects_unknown_fail_closed() -> None:
    """未知 scope **当场拒**，不是回落到某个默认值。

    回落（例如未知值当 `COPY`）会让"前端发了个拼错的 scope"变成
    "系统默默跑了另一件事"，且没有任何错误 —— 用户看到内容被改了，
    却不知道自己请求的是别的东西。
    """
    for bad in ("", "copy", "ALL", "FULL ", "TITLE_AND_BODY", "None"):
        with pytest.raises(ValueError):
            normalize_scope(bad)


def test_task_kinds_for_scope_full_runs_structure_first() -> None:
    """★ `FULL` 的 kind **顺序**有意义：`CONTENT_STRUCTURE` 在前。

    ## 为什么顺序是判据而不是实现细节

    结构调用产出**蓝图**，而文案调用要拿蓝图渲染提示词
    （`generation_prompt_values(blueprint=…)`）。顺序反了的症状是
    "文案调用报缺 blueprint 值"—— 报错点在提示词渲染，根因在调度顺序。

    其余三个 scope 都只跑 `CONTENT_COPY`，且**不含**结构。
    """
    assert task_kinds_for_scope(SCOPE_FULL) == (
        TASK_CONTENT_STRUCTURE,
        TASK_CONTENT_COPY,
    )
    for scope in (SCOPE_COPY, SCOPE_TAGS, SCOPE_TITLE):
        kinds = task_kinds_for_scope(scope)
        assert kinds == (TASK_CONTENT_COPY,), f"{scope} 应当只跑文案"
        assert TASK_CONTENT_STRUCTURE not in kinds, (
            f"{scope} 不含结构 —— `prepare_regeneration` 的蓝图配对判据"
            "正是靠这一点成立"
        )


def test_task_kinds_for_scope_rejects_unknown() -> None:
    for bad in ("", "ALL", "copy"):
        with pytest.raises(ValueError):
            task_kinds_for_scope(bad)


def test_calls_for_boundaries() -> None:
    """`calls_for(kind, count)` 的三类边界。

    ## 为什么 `count=0` **不抛**而 `count=-1` 抛

    `calls_for` 回答的是"这一次生成要买几次模型调用"，它的输入来自
    **已校验的**策略（`content_count ≥ 1` 在端点侧已挡）。
    0 次是**合法的计算结果**（某条路径不产生调用），
    而负数**不可能是计算结果** —— 它是上游算错了，必须当场响。

    `calls_for(kind, 0) == 0` 这条尤其要写：若实现写成 `max(1, count)`，
    它会把"0 次"悄悄升级成"1 次" ⇒ 一次不存在的调用被计费，
    而账面上看不出来。
    """
    assert calls_for(TASK_CONTENT_COPY, 0) == 0
    assert calls_for(TASK_CONTENT_COPY, 1) == 1
    assert calls_for(TASK_CONTENT_COPY, 3) == 3
    assert calls_for(TASK_CONTENT_STRUCTURE, 0) == 0
    assert calls_for(TASK_CONTENT_STRUCTURE, 2) == 2

    with pytest.raises(ValueError):
        calls_for(TASK_CONTENT_COPY, -1)
    with pytest.raises(ValueError):
        calls_for("NOT_A_KIND", 1)


def test_generation_request_roundtrip_is_identity() -> None:
    """`from_payload(as_payload())` 是**恒等**。

    不等意味着"落库再读出来"的参数与当初写进去的不是同一个 ——
    而 `content_items.generation_request` 正是执行阶段唯一的参数来源
    （处理器读它来构造 `GenerationRequest`），不等会让执行阶段的
    行为与发起时的意图**静默分叉**。
    """
    for scope in (SCOPE_COPY, SCOPE_FULL, SCOPE_TAGS, SCOPE_TITLE):
        for batch in ("TRIAL", "SCALE", "BULK", "任意非空串"):
            original = _generation_request(scope=scope, batch_kind=batch)
            payload = original.as_payload()
            assert set(payload) == GENERATION_REQUEST_KEYS
            restored = GenerationRequest.from_payload(payload)
            assert restored == original, (
                f"scope={scope} batch={batch} 往返不等："
                f"{restored} != {original}"
            )
            # ★ 归一发生在**构造时**：`TAGS` 存进库里的就是 `COPY`
            assert payload[REQUEST_KEY_SCOPE] == normalize_scope(scope)


def test_generation_request_rejects_non_dict_payload() -> None:
    """非 dict 的载荷**当场拒**（含 `None` / 列表 / 字符串 / 数字）。"""
    for bad in (None, [], "x", 1, 3.5, ()):
        with pytest.raises(ValueError):
            GenerationRequest.from_payload(bad)  # type: ignore[arg-type]


def test_generation_request_rejects_missing_keys() -> None:
    """缺键要**说清缺了哪个** —— 只说"载荷非法"会让排障从零开始猜。"""
    full = _generation_request().as_payload()
    for key in sorted(GENERATION_REQUEST_KEYS):
        partial = {k: v for k, v in full.items() if k != key}
        with pytest.raises(ValueError) as caught:
            GenerationRequest.from_payload(partial)
        assert key in str(caught.value), (
            f"缺 {key} 时异常消息里没提到它：{caught.value}"
        )


def test_generation_request_rejects_extra_keys() -> None:
    """★ 多余键**也拒**（`extra` 不许静默丢弃）。

    静默丢弃的后果：客户端传 `{"scope": ..., "contentCount": 5}` 以为
    条数生效了，而它被丢掉 —— 请求成功、条数却按默认来。

    这也是"响应里没有 `taskKinds`"的同一条判据：不确定的输入要响，
    不要在服务端做猜测性的解释。
    """
    full = _generation_request().as_payload()
    for extra in ("contentCount", "projectId", "taskKinds", "reason"):
        with pytest.raises(ValueError) as caught:
            GenerationRequest.from_payload({**full, extra: 1})
        assert extra in str(caught.value), (
            f"多余键 {extra} 未被点名：{caught.value}"
        )


def test_generation_request_rejects_bad_values() -> None:
    """空 `batchKind` / 非法 uuid / 未知 scope，各自当场拒。"""
    full = _generation_request().as_payload()

    with pytest.raises(ValueError):
        GenerationRequest.from_payload({**full, REQUEST_KEY_BATCH_KIND: ""})

    with pytest.raises(ValueError):
        GenerationRequest.from_payload(
            {**full, REQUEST_KEY_MODEL_PROFILE_ID: "not-a-uuid"}
        )

    for bad_scope in ("", "ALL", "copy"):
        with pytest.raises(ValueError):
            GenerationRequest.from_payload({**full, REQUEST_KEY_SCOPE: bad_scope})


def test_generation_request_accepts_uuid_string() -> None:
    """`modelProfileId` 是**字符串** uuid 时能解出来（JSONB 里存的就是字符串）。

    这条锁住"落库形态与内存形态可以不同"：`as_payload()` 出来的是字符串，
    而 `from_payload()` 必须接得住 —— 否则往返在**第一次读库**时就断。
    """
    profile_id = uuid.uuid4()
    payload = _generation_request(model_profile_id=profile_id).as_payload()
    assert isinstance(payload[REQUEST_KEY_MODEL_PROFILE_ID], str)
    restored = GenerationRequest.from_payload(payload)
    assert restored.model_profile_id == profile_id
    assert isinstance(restored.model_profile_id, uuid.UUID)


def test_binding_and_copy_field_names_are_camel_case() -> None:
    """JSONB 内部键名用 camelCase（存储形状口径）。

    转换点只有 `api._title_candidate_view` 一处；`bindings` **不转换**
    （端点原样给，见 `api.py` 的注释）。若这里改成 snake_case，
    `test_content_detail` 的响应会与 `types` 的声明静默分叉。
    """
    assert BINDING_KEYS == frozenset(
        {"text", "paragraphIndex", "reason", "claimId"}
    )
    assert COPY_FIELDS == ("title", "body", "tags")
    # 段落索引是数字（不是字符串）：`validate_bindings` 按 int 比较
    assert "paragraphIndex" in BINDING_KEYS
    assert "paragraph_index" not in BINDING_KEYS


def test_content_state_constants_match_check_constraint() -> None:
    """`CONTENT_STATES` 与 `content_items` 的 CHECK 取值域**同源**。

    `CONTENT_STATE_LIST_SQL` 是给原生 SQL 用的字面量列表；
    它与 `CONTENT_STATES` 分叉时，症状是"某状态的行在列表查询里
    永远不出现"—— 不报错。
    """
    assert CONTENT_STATES == frozenset(
        {"DRAFT", "STRUCTURED", "GENERATED", "FAILED"}
    )
    assert CONTENT_STATE_DRAFT in CONTENT_STATES
    assert CONTENT_STATE_GENERATED in CONTENT_STATES
    for state in CONTENT_STATES:
        assert f"'{state}'" in CONTENT_STATE_LIST_SQL, (
            f"{state} 不在 CONTENT_STATE_LIST_SQL 里：{CONTENT_STATE_LIST_SQL}"
        )


def test_idempotency_and_queue_constants() -> None:
    """幂等 scope / 任务 kind / 队列名与 `jobs` 侧的登记一致。

    这三者是**跨包标识符**：写错时 `enqueue_job` 照样成功（它们是自由字符串），
    而任务永远不会被本包的分组消费到 —— 症状是"任务躺着不动"。
    """
    assert IDEMPOTENCY_SCOPE_CONTENT_GENERATE == "content-generate"
    assert KIND_CONTENT_GENERATE == "CONTENT_GENERATE"
    assert QUEUE_CONTENT == "content"


def test_title_score_dimensions_are_six_and_unique() -> None:
    """六维评分维度**不重名**（重复会让满分是"两个维度"而不是"六维"）。"""
    assert len(TITLE_SCORE_DIMENSIONS) == 6
    assert len(set(TITLE_SCORE_DIMENSIONS)) == 6


def test_title_candidate_count_bounds() -> None:
    """候选条数的下界 ≥ 1、上界与 `MAX_TITLE_CANDIDATES` 一致。

    `MIN_TITLE_CANDIDATES = 1` 的判据意义：**"0 个候选"不是合法产物** ——
    它会让详情页的 `titleCandidates` 为空数组，而用户以为生成失败了。
    """
    assert MIN_TITLE_CANDIDATES == 1
    assert MAX_TITLE_CANDIDATES == 12
    assert MIN_TITLE_CANDIDATES <= MAX_TITLE_CANDIDATES


# --- 1.1 DDL 文本与名单自洽（不碰库） --------------------------------------


def test_ddl_index_names_are_seventeen_and_unique() -> None:
    """`ddl.index_names()` 恰好 17 项且不重名。

    17 是本包 `models.py` 的 `Index(...)` 总数（不含 5 个主键）
    —— 真库实测 `pg_indexes` 排除 `pk_*` 后正是这 17 项。
    """
    names = c_ddl.index_names()
    assert len(names) == 17
    assert len(set(names)) == 17, "索引名单里有重名"


def test_ddl_trigger_names_are_seven_and_partitioned() -> None:
    """`trigger_names()` 7 项 == 4 不可变 + 3 一致性（**无重叠无遗漏**）。"""
    names = c_ddl.trigger_names()
    immutable = c_ddl.immutable_trigger_names()
    consistency = c_ddl.consistency_trigger_names()

    assert len(names) == 7
    assert len(set(names)) == 7
    assert len(immutable) == 4
    assert len(consistency) == 3
    assert set(names) == set(immutable) | set(consistency), (
        "两个子名单的并集与总名单不等 —— 有触发器没被分到任何一类"
    )
    assert not (set(immutable) & set(consistency)), "两个子名单有重叠"


def test_ddl_consistency_function_names_match_items() -> None:
    """`consistency_function_names()` 3 项，且与 `consistency_items()` 对得上。

    `consistency_items()` 每项是 `(table, func_ddl, trigger_ddl, drop_trigger,
    drop_func)`。这条判据把"函数名的两个来源"钉在一起 ——
    它们是**同一份事实的两种写法**，分叉时挂上去的函数与声明的名字不一致。
    """
    names = c_ddl.consistency_function_names()
    items = c_ddl.consistency_items()
    assert len(names) == 3
    assert len(items) == 3
    for (table, func_ddl, trigger_ddl, drop_trigger, drop_func), fn in zip(
        items, names, strict=True
    ):
        assert fn in func_ddl, f"{fn} 不在它的 func_ddl 里（table={table}）"
        assert fn in drop_func, f"{fn} 不在它的 drop_func 里（table={table}）"
        assert table in trigger_ddl, f"触发器 DDL 没挂在 {table} 上"
        assert "CREATE" in trigger_ddl.upper()


def test_ddl_all_trigger_ddl_is_seven_items() -> None:
    """`all_trigger_ddl()` 7 项 —— 夹具与迁移都靠它，多数少数都会静默少挂。

    ★ 前 4 项是**不可变**（复用 `rednote_reject_mutation`），
    后 3 项是**一致性**。顺序反了不会报错（都是 CREATE TRIGGER），
    但"哪些是本包自己的函数"这条判断会错。
    """
    items = c_ddl.all_trigger_ddl()
    assert len(items) == 7
    for _table, func_ddl, _trigger_ddl, _drop_trigger, _drop_func in items:
        assert "CREATE OR REPLACE FUNCTION" in func_ddl.upper()

    # 前 4 项复用跨包共用的函数（不许在本包重定义它）
    for _table, func_ddl, _t, _dt, _df in items[:4]:
        assert "rednote_reject_mutation" in func_ddl

    # 后 3 项的触发器必须是延迟 CONSTRAINT TRIGGER
    # （否则"同事务先插入引用方、后插入被引用方"会撞 23503）
    for _table, _f, trigger_ddl, _dt, _df in items[4:]:
        upper = trigger_ddl.upper()
        assert "CONSTRAINT TRIGGER" in upper, (
            f"一致性触发器不是 CONSTRAINT TRIGGER：{trigger_ddl[:80]}"
        )
        assert "DEFERRABLE INITIALLY DEFERRED" in upper, (
            f"一致性触发器不是延迟的：{trigger_ddl[:80]}"
        )


def test_ddl_immutable_function_is_shared_not_local() -> None:
    """`immutable_function_names()` 只有 `rednote_reject_mutation` 一项。

    它是**跨包共用**的函数（WP11 建），本包 4 张不可变表都复用它。
    本包若自己再造一个同名函数，`DROP FUNCTION` 时会连带伤到别的包
    （`test_billing_wp25.py` 记过：共用函数被误删导致依赖错误）。
    """
    assert c_ddl.immutable_function_names() == ("rednote_reject_mutation",)


def test_ddl_consistency_sqlstate_is_23514() -> None:
    """一致性判据的 sqlstate 是 `23514`（check_violation）。

    选它而不是自定义 `P0001`：`23514` 是**标准 sqlstate**，
    客户端与连接池的通用错误处置（重试、降级）已经认得它。
    """
    assert c_ddl.CONSISTENCY_SQLSTATE == "23514"


def test_expected_check_names_match_models() -> None:
    """本文件登记的 38 条 CHECK 名与 `models.py` 的 `CheckConstraint` 一致。

    ## 为什么这条要写在**第 1 层**（不碰库）

    第 3 层的真库核对需要"期望集"。期望集若来自真库自己，那条判据就是
    恒真式（比对库与库）。因此期望集写在**本文件**（人写的常量），
    而这里再把它与 `models.py` 的 `CheckConstraint.name` 对账 ——
    两个**独立来源**（本文档 vs 模型）相遇，才能发现"两边一起漂了"。

    真库是第三个来源（第 3 层核对）。
    """
    from rednote.modules.content import models as c_models

    declared: dict[str, set[str]] = {}
    for table in (
        c_models.ContentItem.__table__,
        c_models.ContentVersion.__table__,
        c_models.NarrativeBlueprint.__table__,
        c_models.TitleCandidate.__table__,
        c_models.GenerationContext.__table__,
    ):
        declared[table.name] = {
            c.name for c in table.constraints if c.name and c.name.startswith("ck_")
        }

    expected = {t: set(names) for t, names in _EXPECTED_CHECKS.items()}
    assert expected == declared, (
        "本文件登记的 CHECK 名单与 models.py 不一致：\n"
        f"  只在登记里："
        f"{ {t: sorted(v - declared.get(t, set())) for t, v in expected.items()} }\n"
        f"  只在模型里："
        f"{ {t: sorted(v - expected.get(t, set())) for t, v in declared.items()} }"
    )
    total = sum(len(v) for v in expected.values())
    assert total == 40, f"登记的 CHECK 总数应当是 40，实际 {total}"


# ===========================================================================
# 第 2 层：服务层（真库 + service_*）
# ===========================================================================


def test_start_generation_materializes_rows(env: ContentEnv) -> None:
    """`start_generation` 物化 N 条身份行，且**不预占**（与钱无关）。

    ## 三条断言各自的意义

    1. `created is True`：这是**新**批次（幂等未命中）；
    2. 行数 == `content_count`：N 条身份行；
    3. `generation_request` 已随 INSERT 写入（**不是**分开写的）——
       `repository.insert_content_item` 的 docstring 已论证：分开写会造出
       "身份在、参数还没写"的中间态，而那个中间态恰好是处理器读到的样子
       （读到 `None` ⇒ 判装配故障）。

    另外断言 `state=DRAFT` / `current_version_no=0` 走的是**数据库默认值**
    （服务层不写死）—— 让状态机取值的来源只有一处（`types.py`）。
    """
    with _repo(env) as repo:
        request = _generation_request(
            scope=SCOPE_COPY, batch_kind="TRIAL",
            model_profile_id=env.model_profile_id,
        )
        rows, created = c_service.start_generation(
            repo, project_id=env.project_id, request=request, content_count=3,
            idempotency_key="batch-1", created_by=env.owner_id,
        )

    assert created is True
    assert len(rows) == 3
    for row in rows:
        assert row.project_id == env.project_id
        assert row.generation_request == request.as_payload()
        assert row.state == CONTENT_STATE_DRAFT
        assert row.current_version_no == 0
        assert row.current_version_id is None
        assert row.created_by == env.owner_id


def test_start_generation_is_idempotent_on_same_key(env: ContentEnv) -> None:
    """★ 同键重放 ⇒ `created=False` 且**复用第一批**（不新建）。

    ## 为什么断言用**集合**而不是列表

    实测发现 `list_content_items_by_idempotency` **没有 `ORDER BY`**
    ⇒ 重放回来的行顺序**不稳定**（本机连跑两次顺序不同）。
    用列表比对会得到一条**随机红**的判据（有时过有时不过），
    那比没有判据更糟 —— 它会让人怀疑一个正确的实现。

    断言集合相等 + `created is False`，这才是"复用"的准确口径。
    """
    request = _generation_request(model_profile_id=env.model_profile_id)
    with _repo(env) as repo:
        first, created_first = c_service.start_generation(
            repo, project_id=env.project_id, request=request, content_count=3,
            idempotency_key="replay-1", created_by=env.owner_id,
        )
    with _repo(env) as repo:
        second, created_second = c_service.start_generation(
            repo, project_id=env.project_id, request=request, content_count=3,
            idempotency_key="replay-1", created_by=env.owner_id,
        )

    assert created_first is True
    assert created_second is False
    assert {r.id for r in second} == {r.id for r in first}
    assert len(second) == 3

    # 库里确实只有 3 条（没有因为重放多建一批）
    total = _scalar(
        env,
        "SELECT count(*) FROM content_items WHERE last_idempotency_key = :k",
        {"k": "replay-1"},
    )
    assert total == 3


def test_start_generation_replay_does_not_overwrite_request(env: ContentEnv) -> None:
    """★ 重放时**参数不覆盖**（`service` 的 docstring 原文：命中即返回）。

    ## 为什么"不覆盖"是正确的

    覆盖会让"第一次说 3 条、第二次说 5 条"这种**同键不同体**的请求
    悄悄改掉第一批的参数，而那一批的任务可能已经跑完了 ——
    改了参数、结果还是旧的，且**没有任何报错**。

    这里传一个**不同 scope** 的同键请求：断言库里的参数仍是第一次的。
    """
    first_request = _generation_request(
        scope=SCOPE_COPY, batch_kind="TRIAL",
        model_profile_id=env.model_profile_id,
    )
    with _repo(env) as repo:
        c_service.start_generation(
            repo, project_id=env.project_id, request=first_request,
            content_count=2, idempotency_key="nooverwrite",
            created_by=env.owner_id,
        )

    other_request = _generation_request(
        scope=SCOPE_FULL, batch_kind="SCALE",
        model_profile_id=env.model_profile_id,
    )
    with _repo(env) as repo:
        rows, created = c_service.start_generation(
            repo, project_id=env.project_id, request=other_request,
            content_count=2, idempotency_key="nooverwrite",
            created_by=env.owner_id,
        )

    assert created is False
    for row in rows:
        assert row.generation_request == first_request.as_payload(), (
            "重放把参数覆盖了 —— 端点会据此判 409，但服务层不该自己改"
        )

    stored = _scalar(
        env,
        "SELECT generation_request->>'scope' FROM content_items"
        " WHERE last_idempotency_key = :k LIMIT 1",
        {"k": "nooverwrite"},
    )
    assert stored == SCOPE_COPY


def test_start_generation_rejects_zero_count(env: ContentEnv) -> None:
    """★ `content_count < 1` ⇒ `GENERATION_REQUEST_INVALID`。

    `service` 的 docstring 原文：「0 条会让端点入 0 个任务而**返回 202**，
    调用方以为提交成功」。端点用 `ge=1` 挡了主路径，这是**第二道**
    （防御性的：将来有人从别的入口调服务层）。
    """
    with _repo(env) as repo, pytest.raises(c_service.ContentGenerationError) as caught:
        c_service.start_generation(
            repo, project_id=env.project_id, request=_generation_request(),
            content_count=0, idempotency_key="zero", created_by=env.owner_id,
        )
    assert caught.value.reason == REASON_GENERATION_REQUEST_INVALID

    total = _scalar(
        env, "SELECT count(*) FROM content_items WHERE last_idempotency_key = :k",
        {"k": "zero"},
    )
    assert total == 0, "被拒的请求留下了行"


def test_start_generation_scopes_are_isolated_by_project(env: ContentEnv) -> None:
    """★ 幂等键的作用域是**项目**，不是全局。

    ## 为什么这条要单独测

    `list_content_items_by_idempotency(project_id=…, idempotency_key=…)`
    两个条件都要。若实现漏了 `project_id`，两个**不同项目**用了同一个
    键（例如前端固定传 `"batch-1"`）时，第二个项目会**拿到第一个项目的
    内容行** —— 而 `start_generation` 会返回它们并说 `created=False`。
    响应看起来完全正常，内容是别人家的。
    """
    request = _generation_request(model_profile_id=env.model_profile_id)
    with _repo(env) as repo:
        _, created_a = c_service.start_generation(
            repo, project_id=env.project_id, request=request, content_count=2,
            idempotency_key="shared-key", created_by=env.owner_id,
        )
    with _repo(env) as repo:
        rows_b, created_b = c_service.start_generation(
            repo, project_id=env.other_project_id, request=request,
            content_count=2, idempotency_key="shared-key",
            created_by=env.owner_id,
        )

    assert created_a is True
    assert created_b is True, (
        "另一个项目用同一个键时被判成重放 —— 幂等键的作用域漏了 project_id，"
        "第二个项目会拿到第一个项目的内容行"
    )
    for row in rows_b:
        assert row.project_id == env.other_project_id


def test_prepare_regeneration_requires_blueprint_for_copy(env: ContentEnv) -> None:
    """★ 无蓝图 + `COPY`/`TAGS`/`TITLE` ⇒ `SCOPE_REQUIRES_BLUEPRINT`（422）。

    `service` 的 docstring 原文：文案调用要拿**蓝图**渲染提示词，
    没有蓝图时会在 `render_system_prompt` 处抛"缺 `blueprint` 值"——
    报错点是提示词渲染，根因是"没有蓝图"，两者隔了一层。
    本函数在**发起**那一刻就拒，让报错点即根因。

    `FULL` 不受限（它自己会跑 `CONTENT_STRUCTURE` 产出蓝图）——
    见下一条用例。
    """
    content_id, _ = _seed_valid_content(env)
    for scope in (SCOPE_COPY, SCOPE_TAGS, SCOPE_TITLE):
        with _repo(env) as repo, pytest.raises(
            c_service.ContentGenerationError
        ) as caught:
            c_service.prepare_regeneration(
                repo, content_id=content_id,
                request=_generation_request(scope=scope),
                current_blueprint_id=None,
            )
        assert caught.value.reason == REASON_SCOPE_REQUIRES_BLUEPRINT, scope


def test_prepare_regeneration_allows_full_without_blueprint(env: ContentEnv) -> None:
    """`FULL` + 无蓝图 ⇒ **通过**（并回带两个 kind，顺序正确）。"""
    content_id, _ = _seed_valid_content(env)
    with _repo(env) as repo:
        prepared = c_service.prepare_regeneration(
            repo, content_id=content_id,
            request=_generation_request(scope=SCOPE_FULL),
            current_blueprint_id=None,
        )
    assert prepared["contentId"] == str(content_id)
    assert prepared["scope"] == SCOPE_FULL
    assert prepared["taskKinds"] == [TASK_CONTENT_STRUCTURE, TASK_CONTENT_COPY]


def test_prepare_regeneration_allows_copy_with_blueprint(env: ContentEnv) -> None:
    """有蓝图 + `COPY` ⇒ **通过**。"""
    content_id, _ = _seed_valid_content(env)
    with _repo(env) as repo:
        prepared = c_service.prepare_regeneration(
            repo, content_id=content_id,
            request=_generation_request(scope=SCOPE_COPY),
            current_blueprint_id=uuid.uuid4(),
        )
    assert prepared["scope"] == SCOPE_COPY
    assert prepared["taskKinds"] == [TASK_CONTENT_COPY]


def test_prepare_regeneration_normalizes_tags_scope(env: ContentEnv) -> None:
    """★ `TAGS` 进来的 scope 回带是 `COPY`（**不撒谎**）。

    `service` 的 docstring 原文：用 `task_kinds_for_scope(request.scope)`
    而不是在端点里再写一份映射 —— 分叉的后果是**响应撒谎**
    （响应说 `TAGS`，实际跑了 `CONTENT_COPY`，而它连正文一起改了）。
    """
    content_id, _ = _seed_valid_content(env)
    with _repo(env) as repo:
        prepared = c_service.prepare_regeneration(
            repo, content_id=content_id,
            request=_generation_request(scope=SCOPE_TAGS),
            current_blueprint_id=uuid.uuid4(),
        )
    assert prepared["scope"] == SCOPE_COPY
    assert prepared["taskKinds"] == [TASK_CONTENT_COPY]


def test_prepare_regeneration_overwrites_request(env: ContentEnv) -> None:
    """★ 重做**覆盖**参数（与 `start_generation` 的重放**不覆盖**相反）。

    ## 为什么两者相反是对的

    * **发起**的重放：同一次请求的重复投递 ⇒ 覆盖就是"改了别人的意图"；
    * **重做**：本质就是"换一个意图再来一次"（"仅标题" → "整篇"）⇒
      必须覆盖，否则重做走不通。

    `repository.set_generation_request` 的 docstring 已论证：
    它是**最后一次意图**，不是历史事实；历史事实在每次生成一行的
    `generation_contexts`。
    """
    content_id, _ = _seed_valid_content(env)
    with _repo(env) as repo:
        c_service.start_generation(
            repo, project_id=env.project_id,
            request=_generation_request(
                scope=SCOPE_COPY, batch_kind="TRIAL",
                model_profile_id=env.model_profile_id,
            ),
            content_count=1, idempotency_key="overwrite", created_by=env.owner_id,
        )
    # 手改一下该内容的请求参数为 TITLE（模拟"上一轮只重做标题"）
    #
    # ★ `CAST(:p AS text)` **必须显式转型**。`jsonb_build_object` 的变参签名是
    # `"any"`，PG 无法从上下文推断绑定参数的类型 ⇒ 报
    # `IndeterminateDatatype: could not determine data type of parameter $1`。
    # 它不是"表/列不存在"那类错误，报错里也不含 SQLAlchemy 的模型信息，
    # 容易误判成驱动问题。
    #
    # ⚠️ 用 `CAST(:p AS text)` 而**不是** `:p::text` —— 后者在本仓
    # 报 `42601 syntax error at or near ":"`：SQLAlchemy 的 `text()`
    # 绑定语法把 `:p:` 当成了参数名的一部分。`CAST()` 写法没有这个歧义。
    _raw(
        env,
        "UPDATE content_items SET generation_request ="
        " jsonb_build_object('scope','TITLE','batchKind','TRIAL',"
        " 'modelProfileId', CAST(:p AS text)) WHERE id = :c",
        {"p": str(env.model_profile_id), "c": content_id},
    )

    with _repo(env) as repo:
        prepared = c_service.prepare_regeneration(
            repo, content_id=content_id,
            request=_generation_request(
                scope=SCOPE_FULL, batch_kind="BULK",
                model_profile_id=env.model_profile_id,
            ),
            current_blueprint_id=None,
        )
    assert prepared["scope"] == SCOPE_FULL

    stored = _scalar(
        env,
        "SELECT generation_request->>'scope' FROM content_items WHERE id = :c",
        {"c": content_id},
    )
    assert stored == SCOPE_FULL, "重做没有覆盖参数 —— '仅标题'→'整篇' 会走不通"
    stored_batch = _scalar(
        env,
        "SELECT generation_request->>'batchKind' FROM content_items WHERE id = :c",
        {"c": content_id},
    )
    assert stored_batch == "BULK"


def test_prepare_regeneration_missing_content(env: ContentEnv) -> None:
    """内容不存在 ⇒ `CONTENT_NOT_FOUND`（端点翻 404）。"""
    with _repo(env) as repo, pytest.raises(c_service.ContentGenerationError) as caught:
        c_service.prepare_regeneration(
            repo, content_id=uuid.uuid4(),
            request=_generation_request(),
            current_blueprint_id=uuid.uuid4(),
        )
    assert caught.value.reason == REASON_CONTENT_NOT_FOUND


def test_adopt_title_candidate_appends_version(env: ContentEnv) -> None:
    """★★ 采纳 = **追加一版**（不是 UPDATE），且字段复用规则逐条照表。

    ## 落库规则（`service` 的 docstring 六 的表）

    | 字段 | 新版本行取什么 |
    |---|---|
    | `title` | **被采纳候选的标题** |
    | `body` / `tags` / `bindings` / `blueprint_id` / `generation_context_id` | **原样复用上一版** |
    | `adopted_title_candidate_id` | 被采纳的候选 id |

    ## 为什么"复用"要逐字段断言

    "采纳"的动作语义是"只换标题"。若实现顺手重拼了 `payload`（而不是
    以上一版的 `payload` 为基线），`payload` 里**还有别的键**（将来 schema
    演进）会在这一版上悄悄丢掉 —— 而两版对比时表现为"采纳把某个字段清空了"。

    这里给上一版填满 5 个可复用字段（`body` / `tags` / `bindings` /
    `blueprint_id` / `generation_context_id`），再逐条断言它们原样到了新版。
    """
    # --- 铺底：一版合法版本（带齐可复用字段）+ 一条蓝图 + 一条上下文 + 一个候选 ---
    #
    # ★ 顺序说明：上下文引用版本行，版本行引用上下文（环 2，两侧都延迟），
    # 因此这里让版本行**先**建（`generation_context_id` 暂缺），
    # 再建上下文 —— 但要立刻回填版本行的 `generation_context_id`……
    # **回填就是 UPDATE 不可变表**（23001）。
    #
    # 所以走另一条路：版本行 INSERT 时就把 `generation_context_id` 填上
    # —— 它引用一个**尚不存在**的上下文，靠延迟 FK 在提交时结算。
    # 前提是上下文行必须在**同一事务**里建出来（否则提交时 FK 失败）。
    blueprint_id = uuid.uuid4()
    context_id = uuid.uuid4()
    content_id = uuid.uuid4()
    version_id = uuid.uuid4()

    with session_scope(env.settings) as session:
        session.execute(text(_INSERT_ITEM), {
            "content_id": content_id, "project_id": env.project_id,
        })
        session.execute(text(_INSERT_NODE), {
            "node_id": version_id, "entity_type": "CONTENT",
            "entity_id": content_id,
            "revision_no": 1, "payload_hash": f"h-{version_id.hex[:12]}",
        })
        session.execute(
            text(
                "INSERT INTO narrative_blueprints (id, content_item_id,"
                " opening_function, context, decision_process, usage_scene,"
                " evidence_beats, limitation, conclusion_strength,"
                " interaction_question, paragraph_plan)"
                " VALUES (:id, :c, 'a', 'b', 'c', 'd', 'e', 'f',"
                " 'CONSERVATIVE', 'g', 'h')"
            ),
            {"id": blueprint_id, "c": content_id},
        )
        # 版本行：blueprint_id 与 generation_context_id **一次给全**
        session.execute(
            text(
                "INSERT INTO content_versions (id, content_item_id, version_no,"
                " title, body, tags, bindings, payload, payload_hash,"
                " schema_version, blueprint_id, generation_context_id)"
                " VALUES (:v, :c, 1, '原标题', '原正文',"
                " '[\"t1\"]'::jsonb, '[{\"text\": \"x\"}]'::jsonb,"
                " '{}'::jsonb, :ph, 1, :bp, :gc)"
            ),
            {"v": version_id, "c": content_id, "ph": f"ph-{version_id.hex[:12]}",
             "bp": blueprint_id, "gc": context_id},
        )
        # 上下文行：与版本行同事务（环 2 的延迟 FK 依赖这个顺序）
        session.execute(
            text(
                "INSERT INTO generation_contexts (id, project_id, content_id,"
                " content_version_id, task_kind, idempotency_key)"
                " VALUES (:id, :p, :c, :cv, 'CONTENT_COPY', :k)"
            ),
            {"id": context_id, "p": env.project_id, "c": content_id,
             "cv": version_id, "k": f"gc-{uuid.uuid4().hex[:8]}"},
        )

    candidate_id = _seed_candidate(
        env, version_id=version_id, title="被采纳的新标题"
    )

    # ★ 采纳是 UPDATE 不了不可变表的（23001），因此"字段复用"只能靠
    #   读回**新版**行来验证。用原生 SQL 读，避免 ORM identity map 缓存
    #   （实测踩到：不 expire 时读到的是旧值）。
    with _repos(env) as (repo, version_repo):
        summary = c_service.adopt_title_candidate(
            repo, version_repo=version_repo, content_id=content_id,
            candidate_id=candidate_id, created_by=env.owner_id,
        )

    assert summary.version_no == 2, "采纳应当是第 2 版（追加，不是修改第 1 版）"
    assert summary.title == "被采纳的新标题"
    assert summary.adopted_title_candidate_id == candidate_id
    assert summary.blueprint_id == blueprint_id
    assert summary.generation_context_id == context_id
    assert summary.created_by == env.owner_id

    # --- 第 1 版**原样还在**（不可变） ---
    old_row = _raw(
        env,
        "SELECT title, body, tags, adopted_title_candidate_id"
        " FROM content_versions WHERE id = :v",
        {"v": version_id},
    )
    assert old_row[0][0] == "原标题", "第 1 版被改了 —— 它不是不可变的吗"
    assert old_row[0][1] == "原正文"
    assert list(old_row[0][2]) == ["t1"], "第 1 版的 tags 被改动了"
    assert old_row[0][3] is None, "生成版本的 adopted_title_candidate_id 必须恒为 NULL"

    # --- 新版行的可复用字段逐条核对 ---
    new_row = _raw(
        env,
        "SELECT title, body, tags, bindings, blueprint_id,"
        " generation_context_id, adopted_title_candidate_id, version_no"
        " FROM content_versions WHERE content_item_id = :c AND version_no = 2",
        {"c": content_id},
    )
    assert len(new_row) == 1, "没有产生第 2 版"
    (
        n_title, n_body, n_tags, n_bindings, n_bp, n_gc, n_adopted, n_no,
    ) = new_row[0]
    assert n_title == "被采纳的新标题", "新版标题应当是候选的标题"
    assert n_body == "原正文", "body 没有原样复用上一版"
    assert list(n_tags) == ["t1"], "tags 没有原样复用上一版"
    assert list(n_bindings) == [{"text": "x"}], "bindings 没有原样复用上一版"
    assert n_bp == blueprint_id, "blueprint_id 没有原样复用上一版"
    assert n_gc == context_id, (
        "generation_context_id 没有原样复用 —— 采纳不调模型，"
        "造一条新上下文行等于**报出一次没发生的调用**"
    )
    assert n_adopted == candidate_id
    assert n_no == 2


def test_adopt_title_candidate_advances_projection(env: ContentEnv) -> None:
    """采纳后**内容条目的投影**前进（版本号 + 锁版本号）。

    ## ★ 为什么必须 `expire_all()`

    第 2 层用真 session。`get_content_item` 拿回来的 ORM 对象在
    identity map 里**缓存**：采纳写完投影后，再读同一 id 会拿到**旧值**
    （写就前实测踩到：投影看起来没前进，而库里其实已经是新的）。

    这会让判据"投影没推进"报红 —— 而实现是对的。
    用原生 SQL 读（或 `expire_all()`）才看得到真值。
    """
    content_id, version_id = _seed_valid_content(env)
    candidate_id = _seed_candidate(env, version_id=version_id, title="新标题")

    before = _raw(
        env,
        "SELECT current_version_no, lock_version FROM content_items WHERE id = :c",
        {"c": content_id},
    )
    assert before[0][0] == 0

    with _repos(env) as (repo, version_repo):
        summary = c_service.adopt_title_candidate(
            repo, version_repo=version_repo, content_id=content_id,
            candidate_id=candidate_id, created_by=env.owner_id,
        )

    after = _raw(
        env,
        "SELECT current_version_no, current_version_id, lock_version"
        " FROM content_items WHERE id = :c",
        {"c": content_id},
    )
    assert after[0][0] == summary.version_no == 2
    assert after[0][1] == summary.id, "投影没有指向新版本"
    assert after[0][2] > before[0][1], "lock_version 没有前进"


def test_adopt_title_candidate_creates_no_generation_context(env: ContentEnv) -> None:
    """★ 采纳**不新增** `generation_contexts` 行（无模型调用 ⇒ 无调用记录）。

    `service` 的 docstring 原文：「**没有模型调用 ⇒ 没有新的
    `generation_contexts` 行** —— 这也解释了 `content_versions.
    generation_context_id` 为什么可空。**计费路径为空**」。

    这条判据数**全表**行数（因此 `_clean_own_rows` 的清空是前提）：
    采纳前后相等。
    """
    content_id, version_id = _seed_valid_content(env)
    candidate_id = _seed_candidate(env, version_id=version_id, title="新标题")

    before = _scalar(env, "SELECT count(*) FROM generation_contexts")
    with _repos(env) as (repo, version_repo):
        c_service.adopt_title_candidate(
            repo, version_repo=version_repo, content_id=content_id,
            candidate_id=candidate_id, created_by=env.owner_id,
        )
    after = _scalar(env, "SELECT count(*) FROM generation_contexts")

    assert after == before, (
        "采纳产生了新的 generation_contexts 行 —— 那等于报出一次没发生的模型调用"
    )


def test_adopt_title_candidate_rejects_missing_candidate(env: ContentEnv) -> None:
    """候选不存在 ⇒ `TITLE_CANDIDATE_NOT_FOUND`（端点翻 **404**）。"""
    content_id, _ = _seed_valid_content(env)
    with _repos(env) as (repo, version_repo), pytest.raises(
        c_service.ContentGenerationError
    ) as caught:
        c_service.adopt_title_candidate(
            repo, version_repo=version_repo, content_id=content_id,
            candidate_id=uuid.uuid4(), created_by=env.owner_id,
        )
    assert caught.value.reason == REASON_TITLE_CANDIDATE_NOT_FOUND


def test_adopt_title_candidate_rejects_cross_content_candidate(
    env: ContentEnv,
) -> None:
    """★ 候选属于**别的内容** ⇒ `TITLE_CANDIDATE_MISMATCH`（端点翻 **422**）。

    ## 为什么这条 FK 管不住

    `content_versions.adopted_title_candidate_id` 那条 FK 只保证
    "这个候选**存在**"，不保证"它属于我这条内容"。
    跨内容互相采纳是**语义错误**（AQ-01 要按内容算事实正确率，
    采纳别人家的标题会让"这条内容的标题是哪来的"无法回答），
    且它**不会有任何报错**。

    本层给可读 4xx；DDL（判据 3.2）给"绕过本层也拦得住"—— 两层都要。
    """
    _, version_id_a = _seed_valid_content(env)
    other_content_id, _ = _seed_valid_content(env)
    candidate_of_a = _seed_candidate(
        env, version_id=version_id_a, title="A 的候选"
    )

    with _repos(env) as (repo, version_repo), pytest.raises(
        c_service.ContentGenerationError
    ) as caught:
        c_service.adopt_title_candidate(
            repo, version_repo=version_repo, content_id=other_content_id,
            candidate_id=candidate_of_a, created_by=env.owner_id,
        )
    assert caught.value.reason == REASON_TITLE_CANDIDATE_MISMATCH


def test_adopt_title_candidate_missing_content(env: ContentEnv) -> None:
    """内容不存在 ⇒ `CONTENT_NOT_FOUND`（**先**判内容，再判候选）。"""
    candidate_id = uuid.uuid4()
    with _repos(env) as (repo, version_repo), pytest.raises(
        c_service.ContentGenerationError
    ) as caught:
        c_service.adopt_title_candidate(
            repo, version_repo=version_repo, content_id=uuid.uuid4(),
            candidate_id=candidate_id, created_by=env.owner_id,
        )
    assert caught.value.reason == REASON_CONTENT_NOT_FOUND


def test_adopt_title_candidate_context_cycle_is_closed(env: ContentEnv) -> None:
    """★ 环 2 的闭环：`content_versions.generation_context_id` 引用**同事务**
    插入的 `generation_contexts` 行（延迟 FK 允许）。

    这条判据证的是"延迟形态**真的在生效**"，而不只是"pg_constraint 里
    写着 `condeferrable=true`"：

    先插一条 `content_versions`（它引用一个**还不存在**的上下文），
    再插那条上下文 —— 事务提交时两条 FK 都满足 ⇒ 成功。
    若非延迟，第一条 INSERT 就会撞 `23503`
    （`generate` 路径上"环 2 的解法"依赖这个行为）。
    """
    content_id = uuid.uuid4()
    version_id = uuid.uuid4()
    context_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(text(_INSERT_ITEM), {
            "content_id": content_id, "project_id": env.project_id,
        })
        session.execute(text(_INSERT_NODE), {
            "node_id": version_id, "entity_type": "CONTENT",
            "entity_id": content_id,
            "revision_no": 1, "payload_hash": f"h-{version_id.hex[:12]}",
        })
        # ① 版本行**先**插入，引用一个还不存在的上下文
        session.execute(
            text(
                "INSERT INTO content_versions (id, content_item_id, version_no,"
                " generation_context_id, title, body, tags, bindings, payload,"
                " payload_hash, schema_version)"
                " VALUES (:id, :c, 1, :gc, '标题', '正文', '[]'::jsonb,"
                " '[]'::jsonb, '{}'::jsonb, :ph, 1)"
            ),
            {"id": version_id, "c": content_id, "gc": context_id,
             "ph": f"ph-{version_id.hex[:12]}"},
        )
        # ② 上下文行**后**插入（同一事务）
        session.execute(
            text(
                "INSERT INTO generation_contexts (id, project_id, content_id,"
                " content_version_id, task_kind, idempotency_key)"
                " VALUES (:id, :p, :c, :cv, 'CONTENT_COPY', :k)"
            ),
            {"id": context_id, "p": env.project_id, "c": content_id,
             "cv": version_id, "k": f"cycle-{uuid.uuid4().hex[:8]}"},
        )
    # 走到了这里就是成功（事务已提交）

    linked = _scalar(
        env,
        "SELECT generation_context_id FROM content_versions WHERE id = :v",
        {"v": version_id},
    )
    assert linked == context_id


# --- 2.7 OP-160 影响面反查（ISS-184）--------------------------------------


def _seed_prompt_template_in_session(
    session: Any, *, task_kind: str = "CONTENT_COPY"
) -> tuple[uuid.UUID, uuid.UUID]:
    """在**调用方的事务里**造一组 `prompt_templates` + `prompt_versions`。

    返回 `(template_id, version_id)`。

    ## 为什么必须复用调用方的 session（不能自己 `session_scope`）

    `fk_prompt_versions_id_version_nodes` 与 `trg_version_nodes_prompt_paired`
    **都是延迟约束**：节点与版本行必须在**同一事务**里插入，否则节点在
    提交那一刻是孤立的 ⇒ `23514`。这与 `test_prompts_wp23.py` 的
    `_insert_node_raw` 义务同源（那里把"复用调用方连接"写进了 docstring）。
    """
    template_id = uuid.uuid4()
    version_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO prompt_templates (id, task_kind, name)"
            " VALUES (:id, :k, :n)"
        ),
        {"id": template_id, "k": task_kind, "n": f"op160-{template_id.hex[:8]}"},
    )
    session.execute(
        text(
            "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
            " payload_hash, schema_version)"
            " VALUES (:id, 'PROMPT', :tid, 1, :h, 1)"
        ),
        {"id": version_id, "tid": template_id, "h": f"op160-{version_id.hex[:8]}"},
    )
    session.execute(
        text(
            "INSERT INTO prompt_versions (id, template_id, system_template,"
            " input_schema, output_schema, allowed_tools, change_reason)"
            " VALUES (:vid, :tid, 'sys', '{}'::jsonb, '{}'::jsonb,"
            " '{}'::text[], 'op160')"
        ),
        {"vid": version_id, "tid": template_id},
    )
    return template_id, version_id


def _seed_content_in_session(
    session: Any, *, project_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """在**调用方的事务里**造"一条内容 + 一版合法版本"（含节点）。

    与 `_seed_valid_content` 的区别只有一处：那个函数自己 `session_scope`
    并**提交**，本函数复用调用方事务以便整体回滚（理由见用例 docstring）。
    """
    content_id = uuid.uuid4()
    version_id = uuid.uuid4()
    session.execute(
        text(_INSERT_ITEM), {"content_id": content_id, "project_id": project_id}
    )
    session.execute(
        text(_INSERT_NODE),
        {
            "node_id": version_id, "entity_type": "CONTENT",
            "entity_id": content_id, "revision_no": 1,
            "payload_hash": f"h-{version_id.hex[:12]}",
        },
    )
    session.execute(
        text(
            "INSERT INTO content_versions (id, content_item_id, version_no,"
            " title, body, tags, bindings, payload, payload_hash, schema_version)"
            " VALUES (:version_id, :content_id, 1, '标题', '正文', '[]'::jsonb,"
            " '[]'::jsonb, '{}'::jsonb, :payload_hash, 1)"
        ),
        {
            "version_id": version_id, "content_id": content_id,
            "payload_hash": f"ph-{version_id.hex[:12]}",
        },
    )
    return content_id, version_id


def _insert_context_in_session(
    session: Any, *, project_id: uuid.UUID, content_id: uuid.UUID,
    content_version_id: uuid.UUID, prompt_template_id: uuid.UUID,
    prompt_version_id: uuid.UUID,
) -> uuid.UUID:
    """在**调用方的事务里**插一条带 `prompt_version_id` 的生成上下文。"""
    context_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO generation_contexts (id, project_id, content_id,"
            " content_version_id, task_kind, idempotency_key,"
            " prompt_template_id, prompt_version_id)"
            " VALUES (:id, :p, :c, :cv, 'CONTENT_COPY', :k, :tid, :vid)"
        ),
        {
            "id": context_id, "p": project_id, "c": content_id,
            "cv": content_version_id, "k": f"op160-{context_id.hex[:12]}",
            "tid": prompt_template_id, "vid": prompt_version_id,
        },
    )
    return context_id


@pytest.mark.regression
def test_read_generation_contexts_by_prompt_version_filters(env: ContentEnv) -> None:
    """★ OP-160 的核心判据：按 `prompt_version_id` 反查历史生成（ISS-184）。

    ## 这条判据要证的事

    WP26 文档 §3.6 为 OP-160 写下的关闭条件是"`test_content_wp26.py` 里
    『按 `prompt_version_id` 反查历史生成』的用例通过"，而该用例此前
    **不存在**（ISS-184 机械坐实：全 tests 目录对
    `read_generation_contexts_by_prompt_version` /
    `list_contexts_by_prompt_version` 命中 0 次）。

    机制的四件（`generation_contexts` 表 / `prompt_version_id` 列 /
    `ix_generation_contexts_prompt_version` 索引 / 反查函数）都真实存在，
    缺的是把它们串起来跑一次。本用例就是那次"跑一次"。

    ## 为什么要**区分两个世界**（而不是只断言"查到了"）

    只断言"按 version_a 查得到行"，在实现退化成"返回全表 / 返回空表"
    时**无法区分**：前者会过、后者会红但看不出是过滤错了还是数据没造上。
    因此这里造**两版**提示词、上下文**分属两版**，并额外查一个
    **不存在**的版本 ⇒ 三种世界（命中 A / 命中 B / 空）互相可区分。

    ## ★ 为什么整个用例"造完就回滚"

    `prompt_templates` / `prompt_versions` / `version_nodes` **不在**本包的
    `DROP_ORDER` 里（`_clean_own_rows` 只清本包 5 张表），而它们
    **都是不可变表**（`trg_*_immutable` 拦 UPDATE/DELETE）⇒ 提交后
    在用例内**清不掉**。又不能 `TRUNCATE version_nodes`：那是全仓共享表
    （baseline / knowledge / parsing 的节点都在里面），清它会连带毁掉
    兄弟包的夹具行。

    还有一个**更隐蔽**的理由：`prompts_bridge.resolve_active_version` 走
    `list_templates(task_kind=…, limit=1)`，即按 `created_at DESC` 取
    **最新一条**模板。若本用例提交了一条 `CONTENT_COPY` 模板，
    它就会成为该 kind 的"最新"模板 —— 一旦它没有激活版本，
    后续任何 kind=CONTENT_COPY 的生成解析都会 fail-closed
    （`PROMPT_NOT_ACTIVATED`）。**不提交**从根上避免了这个污染。

    因此：一个 savepoint 内造齐夹具 + 跑查询 + 断言，退出即回滚 ——
    零残留（与 `test_parsing_wp17.py` 的 OP-134 手法同源），
    并在块外用一次独立查询**核对零残留**（这条自身就是判据）。
    """
    with session_scope(env.settings) as session:
        nested = session.begin_nested()
        try:
            repo = ContentRepository(session)

            template_a, version_a = _seed_prompt_template_in_session(session)
            _template_b, version_b = _seed_prompt_template_in_session(session)

            content_a, cv_a = _seed_content_in_session(
                session, project_id=env.project_id
            )
            content_b, cv_b = _seed_content_in_session(
                session, project_id=env.project_id
            )
            content_c, cv_c = _seed_content_in_session(
                session, project_id=env.project_id
            )

            # version_a 被两条生成消费；version_b 被一条消费。
            ctx_a1 = _insert_context_in_session(
                session, project_id=env.project_id, content_id=content_a,
                content_version_id=cv_a, prompt_template_id=template_a,
                prompt_version_id=version_a,
            )
            ctx_a2 = _insert_context_in_session(
                session, project_id=env.project_id, content_id=content_b,
                content_version_id=cv_b, prompt_template_id=template_a,
                prompt_version_id=version_a,
            )
            ctx_b1 = _insert_context_in_session(
                session, project_id=env.project_id, content_id=content_c,
                content_version_id=cv_c, prompt_template_id=_template_b,
                prompt_version_id=version_b,
            )

            # --- 世界 1：按 version_a 反查 ⇒ 恰两条，且都是 version_a 的 ---
            rows_a = c_service.read_generation_contexts_by_prompt_version(
                repo, prompt_version_id=version_a, limit=20, offset=0
            )
            assert len(rows_a) == 2
            assert {row.id for row in rows_a} == {ctx_a1, ctx_a2}
            # 逐行钉住"返回的就是这一版"，而不是"库里恰好有两条"。
            assert {row.prompt_version_id for row in rows_a} == {version_a}
            # 关联列如实透传（`_context_view` 的字段一一对账）。
            assert {row.content_id for row in rows_a} == {content_a, content_b}
            assert {row.prompt_template_id for row in rows_a} == {template_a}
            assert {row.content_version_id for row in rows_a} == {cv_a, cv_b}
            assert {row.task_kind for row in rows_a} == {"CONTENT_COPY"}
            assert {row.project_id for row in rows_a} == {env.project_id}

            # --- 世界 2：按 version_b 反查 ⇒ 恰一条（证明是**过滤**不是全表）---
            rows_b = c_service.read_generation_contexts_by_prompt_version(
                repo, prompt_version_id=version_b, limit=20, offset=0
            )
            assert len(rows_b) == 1
            assert rows_b[0].id == ctx_b1
            assert rows_b[0].prompt_version_id == version_b
            # 兄弟版本的行**不能**混进来。
            assert ctx_a1 not in {row.id for row in rows_b}
            assert ctx_a2 not in {row.id for row in rows_b}

            # --- 世界 3：按一个不存在的版本反查 ⇒ 空（不是报错）---
            rows_none = c_service.read_generation_contexts_by_prompt_version(
                repo, prompt_version_id=uuid.uuid4(), limit=20, offset=0
            )
            assert rows_none == []

            # --- 分页语义：limit=1 只回一条（避免"分页参数被吞"的静默降级）---
            page = c_service.read_generation_contexts_by_prompt_version(
                repo, prompt_version_id=version_a, limit=1, offset=0
            )
            assert len(page) == 1
            assert page[0].prompt_version_id == version_a
        finally:
            nested.rollback()

    # --- 块外核对：**零残留**（这条自身就是判据，ISS-95 的可执行形态）---
    assert (
        _scalar(
            env,
            "SELECT count(*) FROM prompt_templates WHERE id IN (:a, :b)",
            {"a": template_a, "b": _template_b},
        )
        == 0
    )
    assert (
        _scalar(
            env,
            "SELECT count(*) FROM prompt_versions WHERE id IN (:a, :b)",
            {"a": version_a, "b": version_b},
        )
        == 0
    )
    assert (
        _scalar(
            env,
            "SELECT count(*) FROM generation_contexts WHERE prompt_version_id"
            " IN (:a, :b)",
            {"a": version_a, "b": version_b},
        )
        == 0
    )


# ===========================================================================
# 第 3 层：原生 SQL 绕过 ORM
# ===========================================================================


@pytest.mark.regression
def test_immutable_tables_reject_update_and_delete(env: ContentEnv) -> None:
    """★★ 4 张不可变表的 UPDATE / DELETE 各撞 `23001`（`IMMUTABLE_RECORD_SQLSTATE`）。

    ## 为什么要用**真实列**

    `SET note = note` 而该表没有 `note` 列时会先撞 `42703`（列不存在）
    —— **没走到触发器**却返回一个"非 23001 的 sqlstate"，
    判据会以"防线失效"的形式报红，真实原因是列名写错
    （`test_billing_wp25.py` 已记过同一个坑）。

    ## 为什么每张表都要先 `prepare` 一行

    **空表的 UPDATE / DELETE 不触发行级触发器**（没有行就没有 `FOR EACH ROW`）。
    写就前实测：空 `generation_contexts` 的 UPDATE 报的是**别的**错误
    （`23514`，因为探针自己污染了事务），看起来像"触发器不存在"。
    本用例用 `prepare` 在同一事务内先造一行，再 UPDATE/DELETE。

    ## ★ 为什么 `prepare` 必须与触发语句**同事务**

    若准备行在事务外（已提交），而触发语句失败回滚：
    准备行留下 ⇒ 它违反的一致性判据推迟到**后续每条判据**引爆
    （写就前实测污染了 8 条判据，全部报同一条不相关的 `23514`）。

    `_expect_sqlstate` 用**一个** `session_scope` 包住两句，异常时整段回滚。
    """
    for table, column in _IMMUTABLE_UPDATE_COLUMN.items():
        update_state = _expect_sqlstate(
            env,
            f"UPDATE {table} SET {column} = {column}",
            prepare=_prepare_row_sql(table),
            prepare_params=_prepare_row_params(env, table),
        )
        assert update_state == IMMUTABLE_RECORD_SQLSTATE, (
            f"{table}.UPDATE 的 sqlstate 是 {update_state}，"
            f"期望 {IMMUTABLE_RECORD_SQLSTATE}（不可变记录）"
        )

        delete_state = _expect_sqlstate(
            env,
            f"DELETE FROM {table}",
            prepare=_prepare_row_sql(table),
            prepare_params=_prepare_row_params(env, table),
        )
        assert delete_state == IMMUTABLE_RECORD_SQLSTATE, (
            f"{table}.DELETE 的 sqlstate 是 {delete_state}，"
            f"期望 {IMMUTABLE_RECORD_SQLSTATE}（不可变记录）"
        )


def _prepare_row_params(env: ContentEnv, table: str) -> dict[str, Any]:
    """给 `_prepare_row_sql(table)` 铺一行所需的参数。

    ## 为什么四张表**共用一张**参数字典

    各表需要的行不同（`title_candidates` 要版本行、`generation_contexts`
    要内容 + 版本 …），但**依赖是被 `prepare` SQL 里的语句决定的**，
    不是被这张字典决定的 —— 多给几个没人用的键，psycopg 不报错。

    反过来「按表分支少给几个键」会引入一个真实的失败模式：
    `_prepare_row_sql` 加了语句、这里忘加参数 ⇒ 报 `:xxx not found`，
    看起来像 SQL 写错。**共一张字典就写不出这个不一致**。

    ## ★ `node_id` 与 `version_id` 必须**同值**

    `version_nodes.id = content_versions.id`（`models.py` 第二节）。
    这里显式写成同一个变量，而不是各生成一个 uuid ——
    后者会造出"版本行引用了不存在的节点"，引爆
    `fk_content_versions_id_version_nodes`（23503），
    并污染**后续每一条**判据。
    """
    content_id = uuid.uuid4()
    version_id = uuid.uuid4()
    return {
        "project_id": env.project_id,
        "content_id": content_id,
        "version_id": version_id,
        # ★ 与 version_id 同值（见 docstring）
        "node_id": version_id,
        "payload_hash": f"ph-{version_id.hex[:12]}",
        # `_INSERT_VERSION` 需要
        "version_no": 1,
        # 目标行自己的主键（content_versions 用 version_id）
        "row_id": version_id if table == "content_versions" else uuid.uuid4(),
        "idempotency_key": f"prep-{uuid.uuid4().hex[:8]}",
    }


def _prepare_row_sql(table: str) -> tuple[str, ...]:
    """造一行 `table` 的记录（含全部依赖），供不可变判据使用。

    ## 为什么返回**语句元组**而不是一条分号拼接的字符串

    ★ 见 `_expect_sqlstate` 的实测段：**多语句 + 参数绑定**会被 PG 拒绝
    （`42601 cannot insert multiple commands into a prepared statement`），
    因为带参数时走 extended protocol，而 prepared statement 只容许一条命令。
    因此这里返回元组，由调用方**逐条** `execute`。

    ## ★ 占位符为什么**全局唯一**

    多条语句共用一份参数字典。若两条语句都用 `:id`，
    psycopg 按**名字**绑定 ⇒ 两处被绑成**同一个值** ——
    "内容行的 id == 版本行的 id"，而这在 `content_items` 与
    `content_versions` 之间是**错的**（版本行的 id 该等于**节点**的 id）。
    这个错误不会报错，只会让判据在别处莫名其妙地红。
    因此：`:content_id` / `:version_id` / `:node_id` / `:row_id` 各管各的。

    ## 各表依赖链

    * `content_versions` —— 内容 → 节点 → 版本行（节点与版本行同值）
    * `narrative_blueprints` —— 内容 → 蓝图（**不挂节点**，见 `models.py` 第三节）
    * `title_candidates` —— 内容 → 节点 → 版本行 → 候选
    * `generation_contexts` —— 内容 → 节点 → 版本行 → 上下文
    """
    if table == "content_versions":
        return (
            _INSERT_ITEM,
            _INSERT_PREP_NODE,
            _INSERT_VERSION,
        )
    if table == "narrative_blueprints":
        return (
            _INSERT_ITEM,
            "INSERT INTO narrative_blueprints (id, content_item_id,"
            " opening_function, context, decision_process, usage_scene,"
            " evidence_beats, limitation, conclusion_strength,"
            " interaction_question, paragraph_plan)"
            " VALUES (:row_id, :content_id, 'a', 'b', 'c', 'd', 'e', 'f',"
            " 'CONSERVATIVE', 'g', 'h')",
        )
    if table == "title_candidates":
        return (
            _INSERT_ITEM,
            _INSERT_PREP_NODE,
            _INSERT_VERSION,
            "INSERT INTO title_candidates (id, content_version_id,"
            " candidate_index, title, scores, keywords)"
            " VALUES (:row_id, :version_id, 0, '候选', '{}'::jsonb, '[]'::jsonb)",
        )
    if table == "generation_contexts":
        return (
            _INSERT_ITEM,
            _INSERT_PREP_NODE,
            _INSERT_VERSION,
            "INSERT INTO generation_contexts (id, project_id, content_id,"
            " content_version_id, task_kind, idempotency_key)"
            " VALUES (:row_id, :project_id, :content_id, :version_id,"
            " 'CONTENT_COPY', :idempotency_key)",
        )
    raise AssertionError(f"未登记的不可变表：{table}")


@pytest.mark.regression
def test_op88_reason_one_missing_node_hits_foreign_key(env: ContentEnv) -> None:
    """★ OP-88 判据 1（节点不存在）**实际报 `23503`**（被 FK 先拦）。

    ## 为什么这条判据的 sqlstate 是反直觉的

    `content_versions.id → version_nodes.id` 那条 FK 是**延迟的**
    （`fk_content_versions_id_version_nodes` 在 `DEFERRED_FOREIGN_KEYS` 里）。
    在 `SET CONSTRAINTS ALL IMMEDIATE` 时，**FK 检查先跑**，
    触发器里"节点不存在 ⇒ RAISE 23514"那一句**根本没机会执行**。

    因此按"触发器 23514"写这条断言会**恒红**。
    本判据断言 `23503` 并**点名**那条约束 —— 这样将来有人把这条 FK
    改成非延迟或去掉时，会在这里当场看见。

    ## 与判据 2/3 的对比

    判据 2/3 里节点**存在**（只是 `entity_type` 或 `entity_id` 不对），
    FK 通过，才轮到触发器 ⇒ 那两条报 `23514`（见下一条用例）。
    """
    content_id = uuid.uuid4()
    version_id = uuid.uuid4()
    state, constraint = _expect_constraint(
        env,
        _INSERT_VERSION,
        {"version_id": version_id, "content_id": content_id, "version_no": 1,
         "payload_hash": "h"},
        prepare=_INSERT_ITEM,
        prepare_params={"content_id": content_id, "project_id": env.project_id},
    )
    assert state == "23503", (
        f"OP-88 判据 1 的 sqlstate 是 {state}，期望 23503"
        "（延迟 FK 先于触发器检查 —— 这是实测结果，不是推测）"
    )
    assert constraint == "fk_content_versions_id_version_nodes", (
        f"命中的约束是 {constraint!r}，期望 fk_content_versions_id_version_nodes"
    )


@pytest.mark.regression
def test_op88_reason_two_and_three_hit_consistency_trigger(env: ContentEnv) -> None:
    """★ OP-88 判据 2（`entity_type != CONTENT`）/ 3（`entity_id` 不匹配）
    报 `23514`（触发器 RAISE）。

    ## 为什么不对约束名断言

    这三条判据合在 `rednote_check_content_version_consistency()`
    **一个函数**里（`ddl.py` 的 docstring 已论证：同一张表、同一次插入、
    修法同类）。触发器 `RAISE EXCEPTION` **不带 `CONSTRAINT` 名**
    ⇒ `diag.constraint_name` 为空。能断言的是 sqlstate；
    消息文本是给人读的，**不写进断言**（写进去会在措辞变化时恒红）。
    """
    # --- 判据 2：entity_type 不是 CONTENT（节点存在） ---
    content_id = uuid.uuid4()
    version_id = uuid.uuid4()
    state2 = _expect_sqlstate(
        env,
        _INSERT_VERSION,
        {"version_id": version_id, "content_id": content_id, "version_no": 1,
         "payload_hash": "h"},
        prepare=(
            _INSERT_ITEM,
            "INSERT INTO version_nodes (id, entity_type, entity_id,"
            " revision_no, payload_hash, schema_version)"
            " VALUES (:node_id, 'BRIEF', :content_id, 1, 'h', 1)"
        ),
        prepare_params={
            "content_id": content_id, "project_id": env.project_id,
            "node_id": version_id,
        },
    )
    assert state2 == c_ddl.CONSISTENCY_SQLSTATE, (
        f"判据 2（entity_type!=CONTENT）的 sqlstate 是 {state2}，"
        f"期望 {c_ddl.CONSISTENCY_SQLSTATE}"
    )

    # --- 判据 3：entity_id 与本行的 content_item_id 不匹配 ---
    content_id3 = uuid.uuid4()
    other_content_id = uuid.uuid4()
    version_id3 = uuid.uuid4()
    state3 = _expect_sqlstate(
        env,
        _INSERT_VERSION,
        {"version_id": version_id3, "content_id": content_id3, "version_no": 1,
         "payload_hash": "h"},
        prepare=(
            _INSERT_ITEM,
            "INSERT INTO version_nodes (id, entity_type, entity_id,"
            " revision_no, payload_hash, schema_version)"
            " VALUES (:node_id, 'CONTENT', :other, 1, 'h', 1)"
        ),
        prepare_params={
            "content_id": content_id3, "project_id": env.project_id,
            "node_id": version_id3, "other": other_content_id,
        },
    )
    assert state3 == c_ddl.CONSISTENCY_SQLSTATE, (
        f"判据 3（entity_id 不匹配）的 sqlstate 是 {state3}，"
        f"期望 {c_ddl.CONSISTENCY_SQLSTATE}"
    )


@pytest.mark.regression
def test_version_nodes_reverse_trigger_rejects_orphan_for_existing_content(
    env: ContentEnv,
) -> None:
    """★ 反向判据：**内容真实存在、但节点没有配对版本行** ⇒ `23514`。

    这是 `trg_version_nodes_content_paired`（挂在 `version_nodes` 上，
    `WHEN NEW.entity_type = 'CONTENT'`）+ `rednote_check_content_node_paired()`。

    ## 为什么"反向"必须单独存在

    正向判据（判据 1/2/3）保证"每个版本行都有合法节点"；
    反向保证"每个 CONTENT 节点（**其内容身份真实存在时**）都有版本行"。
    少了反向，`register_version` 注册了节点但 `insert_version` 失败时
    **不报错** —— 节点计数与内容版本数静默分叉
    （`dependencies.py` 记过这个症状）。

    ## ★★ 为什么必须造**真实的** `content_items` 行

    反向判据是**收窄**过的（与 WP18/WP20/WP22/WP23 同一处理）：
    只对"`content_items` 里真实存在 `id = entity_id` 的行"强制。
    因此**随机 `uuid4()` 的 `entity_id` 会走"放行"分支**，
    这条判据会以 "NO ERROR" 报红 —— 而那是收窄的**预期行为**，
    不是缺陷（对照判据见下一条）。

    本包写入顺序（`service.py:840-847`）是先落身份行、再 `register_version`，
    故生产路径上 `content_items` 行**必然已存在** ⇒ 收窄不会误放。
    这条判据正是锁住"真正的防线还在"。
    """
    content_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(text(_INSERT_ITEM), {
            "content_id": content_id, "project_id": env.project_id,
        })

    state = _expect_sqlstate(
        env,
        "INSERT INTO version_nodes (entity_type, entity_id, revision_no,"
        " payload_hash, schema_version)"
        " VALUES ('CONTENT', :e, 99, 'h', 1)",
        {"e": content_id},
    )
    assert state == c_ddl.CONSISTENCY_SQLSTATE, (
        f"孤立 CONTENT 节点（内容存在）的 sqlstate 是 {state}，"
        f"期望 {c_ddl.CONSISTENCY_SQLSTATE}"
    )


@pytest.mark.cross_wp
def test_version_nodes_reverse_trigger_allows_orphan_for_unknown_entity(
    env: ContentEnv,
) -> None:
    """**对照组 + 登记的代价**：`entity_id` 指向不存在的内容时**放行**。

    这条**主动锁定收窄行为**（对齐 WP18 的
    `test_op88_allows_orphan_node_for_unknown_entity` / WP20 的
    `test_op88_orphan_node_with_unknown_asset_is_allowed`）。

    ## 为什么必须有这条对照

    没有它，上一条判据在一个**永远 `RAISE`** 的实现上也会全绿 ——
    而那样的实现会让 WP11 的通用用例（`ENTITY_TYPE_CONTENT` 是
    它 8 类样本实体之一）直接失败，即"别的包的进度被本包阻塞"。
    本仓实测过这个后果：不收窄时 WP11 既有 2 例报红。

    代价：指向不存在内容的"悬空节点"不会被拦下。危害等级低
    （它不被任何东西引用，溯源也走不到它），已登记为待定项。
    若将来有人去掉收窄，这条测试会红 —— 那时需要同步更新登记册，
    而不是让一个未声明的行为变化悄悄发生。
    """
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO version_nodes (entity_type, entity_id,"
                " revision_no, payload_hash, schema_version)"
                " VALUES ('CONTENT', :e, 99, 'h', 1)"
            ),
            {"e": uuid.uuid4()},  # 不存在于 content_items
        )
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


@pytest.mark.regression
def test_cross_table_checks_hit_consistency_trigger(env: ContentEnv) -> None:
    """★ 三条跨表链接判据各撞 `23514`：

    * **3.1** 蓝图跨内容（`content_versions.blueprint_id` 指向别的内容的蓝图）
    * **3.2** 采纳候选跨内容（`adopted_title_candidate_id` 指向别的内容的候选）
    * **3.3** 上下文版本跨内容（`generation_contexts.content_version_id`
      指向别的内容的版本）

    ## 为什么这三条"FK 管不住"

    蓝图 / 候选 / 版本三条 FK 都只保证"被引用行**存在**"，
    不保证"它属于**同一条内容**"。构造：造 A、B 两条内容，
    各带一版；然后把 A 的蓝图 / 候选 / B 的版本挂到对方身上。

    ## 3.2 判据穿透两层

    `ddl.py` 原文：候选 → 它所属的版本 → 那条版本的内容身份。
    **不穿透就退化成"候选存在"（= FK 已保证的事，恒真）**。
    因此这里用"A 的候选挂到 B 的版本行上"（候选存在、内容不同）。
    """
    content_a, version_a = _seed_valid_content(env)
    content_b, version_b = _seed_valid_content(env)

    # 给 A 造一条蓝图（用它自己的 candidate 无所谓）
    blueprint_a = _raw(
        env,
        "INSERT INTO narrative_blueprints (id, content_item_id, opening_function,"
        " context, decision_process, usage_scene, evidence_beats, limitation,"
        " conclusion_strength, interaction_question, paragraph_plan)"
        " VALUES (gen_random_uuid(), :c, 'a', 'b', 'c', 'd', 'e', 'f',"
        " 'CONSERVATIVE', 'g', 'h') RETURNING id",
        {"c": content_a},
    )[0][0]

    # 给 A 造一个候选（挂在 A 的版本行上）
    candidate_a = _seed_candidate(env, version_id=version_a, title="A 的候选")

    # --- 3.1 蓝图跨内容：把 A 的蓝图挂到 B 的新版本行上 ---
    version_b2 = uuid.uuid4()
    state31 = _expect_sqlstate(
        env,
        "INSERT INTO content_versions (id, content_item_id, version_no,"
        " blueprint_id, title, body, tags, bindings, payload, payload_hash,"
        " schema_version)"
        " VALUES (:id, :c, 2, :bp, '标题', '正文', '[]'::jsonb, '[]'::jsonb,"
        " '{}'::jsonb, :ph, 1)",
        {"id": version_b2, "c": content_b, "bp": blueprint_a,
         "ph": f"ph-{version_b2.hex[:12]}"},
        prepare=(
            "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
            " payload_hash, schema_version)"
            " VALUES (:node_id, 'CONTENT', :c, 2, 'h', 1)"
        ),
        prepare_params={"node_id": version_b2, "c": content_b},
    )
    assert state31 == c_ddl.CONSISTENCY_SQLSTATE, (
        f"3.1 蓝图跨内容的 sqlstate 是 {state31}，"
        f"期望 {c_ddl.CONSISTENCY_SQLSTATE}"
    )

    # --- 3.2 采纳候选跨内容：把 A 的候选挂到 B 的新版本行上 ---
    version_b3 = uuid.uuid4()
    state32 = _expect_sqlstate(
        env,
        "INSERT INTO content_versions (id, content_item_id, version_no,"
        " adopted_title_candidate_id, title, body, tags, bindings, payload,"
        " payload_hash, schema_version)"
        " VALUES (:id, :c, 3, :tc, '标题', '正文', '[]'::jsonb, '[]'::jsonb,"
        " '{}'::jsonb, :ph, 1)",
        {"id": version_b3, "c": content_b, "tc": candidate_a,
         "ph": f"ph-{version_b3.hex[:12]}"},
        prepare=(
            "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
            " payload_hash, schema_version)"
            " VALUES (:node_id, 'CONTENT', :c, 3, 'h', 1)"
        ),
        prepare_params={"node_id": version_b3, "c": content_b},
    )
    assert state32 == c_ddl.CONSISTENCY_SQLSTATE, (
        f"3.2 采纳候选跨内容的 sqlstate 是 {state32}，"
        f"期望 {c_ddl.CONSISTENCY_SQLSTATE}"
    )

    # --- 3.3 上下文版本跨内容：上下文说是 B 的，版本却是 A 的 ---
    state33 = _expect_sqlstate(
        env,
        "INSERT INTO generation_contexts (project_id, content_id,"
        " content_version_id, task_kind, idempotency_key)"
        " VALUES (:p, :c, :cv, 'CONTENT_COPY', :k)",
        {"p": env.project_id, "c": content_b, "cv": version_a,
         "k": f"x-{uuid.uuid4().hex[:8]}"},
    )
    assert state33 == c_ddl.CONSISTENCY_SQLSTATE, (
        f"3.3 上下文版本跨内容的 sqlstate 是 {state33}，"
        f"期望 {c_ddl.CONSISTENCY_SQLSTATE}"
    )


@pytest.mark.regression
def test_cycle_one_current_version_id_must_exist(env: ContentEnv) -> None:
    """环 1（`content_items.current_version_id` → `content_versions`）：
    引用不存在的版本 ⇒ `23503`，**约束名**是那条环 FK。

    ## 为什么这条 FK 是延迟的

    "插内容行"与"插第一版"是同一事务里的两步，而内容行的
    `current_version_id` 指向第二步行 —— 非延迟会让第一步就撞 FK。
    延迟到提交时检查，让"先建身份、后建版本"成为可能。

    ## 判据的**方向**

    这里写的是**坏值**（不存在的 uuid）。若把这条 FK 改成非延迟，
    本用例仍然报 `23503`（FK 一样会拦），**但报的时机不同** ——
    因此本用例的价值在"约束名点名"与"它确实在检查"，
    而"延迟形态"由 `test_deferred_foreign_keys_count` 单独钉住。
    """
    content_id = uuid.uuid4()
    state, constraint = _expect_constraint(
        env,
        "UPDATE content_items SET current_version_id = :v WHERE id = :i",
        {"v": uuid.uuid4(), "i": content_id},
        prepare=_INSERT_ITEM,
        prepare_params={"content_id": content_id, "project_id": env.project_id},
    )
    assert state == "23503", f"环 1 的 sqlstate 是 {state}，期望 23503"
    assert constraint == "fk_content_items_current_version_id_content_versions", (
        f"环 1 命中的约束是 {constraint!r}"
    )


@pytest.mark.regression
def test_cycle_two_generation_context_id_must_exist(env: ContentEnv) -> None:
    """环 2（`content_versions.generation_context_id` → `generation_contexts`）：
    引用不存在的上下文 ⇒ `23503`，约束名点名。

    ## 反向对照

    环 2 的**正**用法（先插版本、后插上下文，同事务）由
    `test_adopt_title_candidate_context_cycle_is_closed` 覆盖 ——
    两条合起来才说明"延迟是真的在生效"：
    正向能过（不早拦）+ 反向能拦（不是形同虚设）。
    """
    content_id = uuid.uuid4()
    version_id = uuid.uuid4()
    state, constraint = _expect_constraint(
        env,
        "INSERT INTO content_versions (id, content_item_id, version_no,"
        " generation_context_id, title, body, tags, bindings, payload,"
        " payload_hash, schema_version)"
        " VALUES (:id, :c, 1, :gc, '标题', '正文', '[]'::jsonb, '[]'::jsonb,"
        " '{}'::jsonb, :ph, 1)",
        {"id": version_id, "c": content_id, "gc": uuid.uuid4(),
         "ph": f"ph-{version_id.hex[:12]}"},
        prepare=(
            _INSERT_ITEM,
            "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
            " payload_hash, schema_version)"
            " VALUES (:node_id, 'CONTENT', :content_id, 1, 'h', 1)"
        ),
        prepare_params={
            "content_id": content_id, "project_id": env.project_id,
            "node_id": version_id,
        },
    )
    assert state == "23503", f"环 2 的 sqlstate 是 {state}，期望 23503"
    assert constraint == (
        "fk_content_versions_generation_context_id_generation_contexts"
    ), f"环 2 命中的约束是 {constraint!r}"


@pytest.mark.regression
def test_non_deferred_foreign_keys_reject_immediately(env: ContentEnv) -> None:
    """★ 负向判据：两条 FK **必须不延迟**，坏值当场撞 `23503`。

    ## 为什么"不延迟"是正确取舍

    * `content_versions.content_item_id → content_items`：
      "先有内容、再有版本"是**必然**顺序（版本行不可能比它所属的内容早）。
      延迟它等于放弃一条免费的检查 —— 而放弃的代价是
      "版本挂在不存在的 内容上"能在事务里存活到提交前。
    * `title_candidates.content_version_id → content_versions`：
      候选**必然**在它所属的版本行之后产生。

    这两条与 4 条延迟 FK 的**分界**是"两表行是否在同一事务里互相依赖"：
    延迟的 4 条要求"后插入的那行先被引用"，这 2 条不要求。

    ## 断言方式

    用 `_expect_sqlstate`（含 `SET CONSTRAINTS ALL IMMEDIATE`）。
    若这两条被误改为延迟，`IMMEDIATE` 仍会检查 —— 但那已经是
    "人为强制"，而不是"本来就立即检查"。因此这条用例的**价值**在
    约束名点名 + 与下一条"延迟条数"用例配对。
    """
    content_id = uuid.uuid4()
    version_id = uuid.uuid4()

    # ① content_versions.content_item_id 指向不存在的内容
    state_cv, constraint_cv = _expect_constraint(
        env,
        _INSERT_VERSION,
        {"version_id": version_id, "content_id": uuid.uuid4(), "version_no": 2,
         "payload_hash": "h"},
        prepare=(
            "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
            " payload_hash, schema_version)"
            " VALUES (:node_id, 'CONTENT', :content_id, 2, 'h', 1)"
        ),
        prepare_params={"node_id": version_id, "content_id": content_id},
    )
    assert state_cv == "23503", (
        f"fk_content_versions_content_item_id_content_items 的 sqlstate "
        f"是 {state_cv}，期望 23503"
    )
    assert constraint_cv == (
        "fk_content_versions_content_item_id_content_items"
    ), f"命中的约束是 {constraint_cv!r}"

    # ② title_candidates.content_version_id 指向不存在的版本
    state_tc, constraint_tc = _expect_constraint(
        env,
        _INSERT_CANDIDATE,
        {"content_version_id": uuid.uuid4(), "candidate_index": 0, "title": "x"},
    )
    assert state_tc == "23503", (
        f"fk_title_candidates_content_version_id_content_versions 的 sqlstate "
        f"是 {state_tc}，期望 23503"
    )
    assert constraint_tc == (
        "fk_title_candidates_content_version_id_content_versions"
    ), f"命中的约束是 {constraint_tc!r}"


@pytest.mark.regression
def test_check_constraints_reject_bad_values(env: ContentEnv) -> None:
    """三条代表性 CHECK 各撞 `23514`，并**点名**约束。

    ## 三条覆盖三种列类型

    | 约束 | 覆盖的列形态 |
    |---|---|
    | `ck_content_items_state_valid` | 枚举（`state`） |
    | `ck_content_versions_version_no_positive` | 数值下界（`version_no`） |
    | `ck_generation_contexts_task_kind_valid` | 枚举（`task_kind`） |

    逐条点名（而不是只断 sqlstate）的价值：`23514` 是本包**最常见**的
    sqlstate（38 条 CHECK + 3 个一致性触发器都是它）。
    只断 sqlstate 无法区分"题述那条 CHECK 拦住了"与"别的东西拦住了"
    —— 而后者会让判据在题述 CHECK 被删掉后**照样绿**。
    """
    state = _expect_constraint(
        env,
        "INSERT INTO content_items (id, project_id, state)"
        " VALUES (:i, :p, 'NOPE')",
        {"i": uuid.uuid4(), "p": env.project_id},
    )
    assert state == ("23514", "ck_content_items_state_valid"), state

    content_id = uuid.uuid4()
    version_id = uuid.uuid4()
    state = _expect_constraint(
        env,
        _INSERT_VERSION,
        {"version_id": version_id, "content_id": content_id, "version_no": 0,
         "payload_hash": "h"},
        prepare=(
            _INSERT_ITEM,
            "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
            " payload_hash, schema_version)"
            " VALUES (:node_id, 'CONTENT', :content_id, 1, 'h', 1)"
        ),
        prepare_params={
            "content_id": content_id, "project_id": env.project_id,
            "node_id": version_id,
        },
    )
    assert state == ("23514", "ck_content_versions_version_no_positive"), state

    state = _expect_constraint(
        env,
        "INSERT INTO generation_contexts (project_id, content_id, task_kind,"
        " idempotency_key) VALUES (:p, :c, 'NOPE', :k)",
        {"p": env.project_id, "c": uuid.uuid4(),
         "k": f"k-{uuid.uuid4().hex[:8]}"},
    )
    assert state == ("23514", "ck_generation_contexts_task_kind_valid"), state


@pytest.mark.regression
def test_truncate_bypasses_immutable_triggers(env: ContentEnv) -> None:
    """★ 判据工具的**前提**：`TRUNCATE` 能清空不可变表（行级触发器不拦它）。

    ## 为什么这条要写

    整份文件的清空策略建立在"TRUNCATE 绕过行级触发器"之上。
    若某天有人加了 `FOR EACH STATEMENT` 的 TRUNCATE 触发器
    （或 PG 行为变化），全部用例会以"清不干净"的形式失败，
    而失败点分散在 30 多个用例里 —— 排障方向会指向每个用例的实现。

    把前提单独测一条：失败时**第一句**就告诉你是清空机制坏了。

    ## 与"不可变"不矛盾

    `TRUNCATE` 不是 `DELETE`：它不做行级检查（也不做 `FOR EACH ROW`
    触发器）。不可变的口径是"已落库的记录不许被**改写**"，
    而 TRUNCATE 是"把测试库的这张表倒空"—— 生产代码里没有任何
    TRUNCATE 路径（`repository` 只有 `insert_*`）。
    """
    content_id, version_id = _seed_valid_content(env)
    before = _scalar(env, "SELECT count(*) FROM content_versions")
    assert before >= 1

    # 同夹具：显式列闭包（否则 PG 拒绝 TRUNCATE 被引用的表）。
    # 本用例只断言 `content_versions` 被清空，多清几张不改变它的结论。
    truncate_list = truncate_targets(get_engine(env.settings), DROP_ORDER)
    with session_scope(env.settings) as session:
        session.execute(text("TRUNCATE " + ", ".join(truncate_list)))

    after = _scalar(env, "SELECT count(*) FROM content_versions")
    assert after == 0, "TRUNCATE 没能清空不可变表 —— 全部用例的清空策略失效"

    # 清空之后，版本行确实没了（`content_id` 只作为局部变量留着）
    gone = _scalar(
        env, "SELECT count(*) FROM content_versions WHERE id = :v",
        {"v": version_id},
    )
    assert gone == 0
    _ = content_id


@pytest.mark.regression
def test_deferred_foreign_keys_count_is_exactly_four(env: ContentEnv) -> None:
    """★★ 本包出向 FK 里**恰好 4 条**延迟，且逐条点名。

    ## 为什么这条判据不能只数个数

    "恰好 4 条"能被两种方式骗过：
    ① 换掉其中一条（比如把环 1 改成非延迟、把另一条改成延迟）—— 数目不变；
    ② 将来某个包在**本包表上**加了 FK 并设为延迟 —— 数目变了但集合也变了。

    因此本用例做**两件事**：
    1. 延迟 FK 的**集合** == `DEFERRED_FOREIGN_KEYS`（逐条点名，本文件常量）；
    2. 两条**必须不延迟**的 FK（`NON_DEFERRED_FOREIGN_KEYS`）确实不延迟。

    第 2 件是**负向**用例，它才是真正的防线 ——
    "有人顺手把本包所有 FK 都设成延迟"这种改动只被它抓到。
    """
    rows = _raw(
        env,
        "SELECT conname, condeferrable, condeferred FROM pg_constraint"
        " WHERE contype = 'f' AND conrelid::regclass::text = ANY(:t)",
        {"t": list(_TABLE_NAMES)},
    ) or []
    assert rows, "一条出向 FK 都没查到 —— 查表的 SQL 或表名写错了"

    deferred = {
        name for name, cd, cdf in rows if cd and cdf
    }
    assert deferred == set(DEFERRED_FOREIGN_KEYS), (
        f"延迟 FK 集合不符。\n  多出：{sorted(deferred - set(DEFERRED_FOREIGN_KEYS))}\n"
        f"  缺少：{sorted(set(DEFERRED_FOREIGN_KEYS) - deferred)}"
    )
    assert len(deferred) == 4

    not_deferred = {name for name, cd, cdf in rows if not (cd and cdf)}
    for name in NON_DEFERRED_FOREIGN_KEYS:
        assert name in not_deferred, (
            f"{name} 变成了延迟 FK —— 它必须**不**延迟"
            "（两表行不在同一事务里互相依赖，延迟等于白放弃一条检查）"
        )


@pytest.mark.regression
def test_cycle_foreign_keys_are_three(env: ContentEnv) -> None:
    """★ 后置补的 **3 条环 FK** 都在（`op.create_foreign_key` 的那 3 条）。

    ## 为什么"环"要单独数

    3 条环 FK 是迁移里**后置**创建的（建表时两表还不都存在）。
    后置语句漏掉时，`create_all` 出来的库（按模型建）**有**它们、
    迁移建出来的库**没有** —— 而两种库都能通过绝大多数测试。
    表现是"生产库上环 1/环 2 的引用完整性没检查"，
    而测试库全绿（`test_schema_parity.py` 的 IS-107 正是这个形态）。

    本用例与 `test_deferred_foreign_keys_count_is_exactly_four` 互补：
    那条管"延迟形态"，这条管"环 FK 本身在不在"。
    """
    rows = _raw(
        env,
        "SELECT conname FROM pg_constraint"
        " WHERE contype = 'f' AND conrelid::regclass::text = ANY(:t)",
        {"t": list(_TABLE_NAMES)},
    ) or []
    present = {row[0] for row in rows}
    for name in CYCLE_FOREIGN_KEYS:
        assert name in present, f"环 FK {name} 不在库里（迁移的后置语句漏了？）"
    assert len(CYCLE_FOREIGN_KEYS) == 3


@pytest.mark.regression
def test_foreign_key_delete_action_is_restrict(env: ContentEnv) -> None:
    """全部 21 条出向 FK 都是 `ON DELETE RESTRICT`（`confdeltype='r'`）。

    ## 为什么 RESTRICT 而不是 CASCADE

    本包 5 张表里 4 张不可变、1 张是身份行。CASCADE 会让
    "删一条内容"**静默带走**它的版本、蓝图、候选、上下文 ——
    而版本是**历史事实**（R5：不可变），它的消失必须是显式动作，
    不是某条 DELETE 的附带效果。

    RESTRICT 把那种情况变成**当场报错**：删不掉会告诉你"还有 N 行引用它"。
    """
    rows = _raw(
        env,
        "SELECT confdeltype FROM pg_constraint"
        " WHERE contype = 'f' AND conrelid::regclass::text = ANY(:t)",
        {"t": list(_TABLE_NAMES)},
    ) or []
    assert len(rows) == 21, (
        f"出向 FK 应当是 21 条，实际 {len(rows)} 条 —— "
        "多/少一条都要在这里改（本用例是「本包 FK 面」的总闸）"
    )
    bad = [r[0] for r in rows if r[0] != "r"]
    assert not bad, (
        f"有 FK 的删除动作不是 RESTRICT：confdeltype={sorted(set(bad))}"
        "（'c'=CASCADE 会静默带走不可变的历史行）"
    )


@pytest.mark.regression
def test_index_names_match_ddl_declaration(env: ContentEnv) -> None:
    """★★ 三方对账：`ddl.index_names()` == 真库非主键索引 == `models.py`。

    ## 为什么排除 `pk_*`

    `pg_indexes` 里主键索引的名字是 **`pk_<table>`**（本仓的命名约定），
    不是 `*_pkey`。按"过滤 `_pkey`"写法会漏掉它们，
    得到 22 ≠ 17 的**假红**（写就前实测踩到）。

    正确口径：排除 `pk_` 前缀后应当**恰好**是 `ddl.index_names()` 的 17 项。

    ## 三个来源各自独立

    * `ddl.index_names()`：给迁移与夹具用的声明；
    * 真库 `pg_indexes`：`create_all` 实际建出来的；
    * `models.py` 的 `Index(...)`：SQLAlchemy 的源声明。

    两两相等才能说明"迁移、模型、DDL 名单"三处一致。
    """
    live_rows = _raw(
        env,
        "SELECT indexname FROM pg_indexes"
        " WHERE tablename = ANY(:t) AND indexname NOT LIKE 'pk\\_%'",
        {"t": list(_TABLE_NAMES)},
    ) or []
    live = {row[0] for row in live_rows}
    declared = set(c_ddl.index_names())

    assert live == declared, (
        f"真库非主键索引与 ddl.index_names() 不符。\n"
        f"  只在真库：{sorted(live - declared)}\n"
        f"  只在声明：{sorted(declared - live)}"
    )

    from rednote.modules.content import models as c_models
    model_indexes: set[str] = set()
    for table in (
        c_models.ContentItem.__table__,
        c_models.ContentVersion.__table__,
        c_models.NarrativeBlueprint.__table__,
        c_models.TitleCandidate.__table__,
        c_models.GenerationContext.__table__,
    ):
        for index in table.indexes:
            assert index.name is not None
            model_indexes.add(index.name)

    assert model_indexes == declared, (
        f"models.py 的索引与 ddl.index_names() 不符。\n"
        f"  只在模型：{sorted(model_indexes - declared)}\n"
        f"  只在声明：{sorted(declared - model_indexes)}"
    )


@pytest.mark.regression
def test_trigger_names_match_db(env: ContentEnv) -> None:
    """★ 真库触发器与 `ddl.trigger_names()` 逐项对上，**并分开问两张表**。

    ## 为什么必须分两句

    `trg_version_nodes_content_paired` 挂在 **`version_nodes`** 上
    （那不是本包的表）。把它与本包 5 表上的 6 个混在一个查询里数，
    会得到"7 个触发器挂在 5 张表上"这种说不清的结论。

    正确口径：

      1. **本包 5 张表上**恰好 6 个（4 不可变 + 2 一致性）；
      2. **`version_nodes` 上**有本包的那 1 个（`..._content_paired`）；
      3. 并集 == `ddl.trigger_names()` 的 7 项。

    `version_nodes` 上还有别的包的触发器（`_baseline_paired` /
    `_brief_paired` / `_knowledge_paired` / `_prompt_paired` /
    `_immutable`），因此第 2 句**只问本包那一个**，不断言总数
    （判据纪律：只能断言"我建的还在"）。
    """
    own_rows = _raw(
        env,
        "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
        " WHERE NOT t.tgisinternal AND c.relname = ANY(:t)",
        {"t": list(_TABLE_NAMES)},
    ) or []
    own = {row[0] for row in own_rows}
    assert own == set(_OWN_TABLE_TRIGGERS), (
        f"本包 5 表上的触发器不符。\n  只在真库：{sorted(own - set(_OWN_TABLE_TRIGGERS))}\n"
        f"  只在声明：{sorted(set(_OWN_TABLE_TRIGGERS) - own)}"
    )
    assert len(own) == 6

    nodes_rows = _raw(
        env,
        "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
        " WHERE NOT t.tgisinternal AND c.relname = 'version_nodes'"
        " AND t.tgname = :n",
        {"n": _VERSION_NODES_TRIGGER},
    ) or []
    assert [r[0] for r in nodes_rows] == [_VERSION_NODES_TRIGGER], (
        f"{_VERSION_NODES_TRIGGER} 不在 version_nodes 上"
        "（反向判据失去载体）"
    )

    assert set(c_ddl.trigger_names()) == own | {_VERSION_NODES_TRIGGER}


@pytest.mark.regression
def test_check_constraints_match_db(env: ContentEnv) -> None:
    """★ 真库 38 条 CHECK 与 `_EXPECTED_CHECKS`（人写常量）逐表对上。

    ## 为什么期望集是**人写的常量**而不是从库里查

    从库里查再比对库 —— 那是恒真式（"库等于库"）。
    期望集必须来自**独立来源**：本文件的常量（人按设计写的），
    再与 `models.py`（第 1 层已对账）和真库（本条）三方相遇。

    写就前实测真库 38 条（3 + 9 + 18 + 7 + 1），与本文件常量一致。
    """
    rows = _raw(
        env,
        "SELECT conname, conrelid::regclass::text FROM pg_constraint"
        " WHERE contype = 'c' AND conrelid::regclass::text = ANY(:t)",
        {"t": list(_TABLE_NAMES)},
    ) or []
    live: dict[str, set[str]] = {}
    for name, table in rows:
        live.setdefault(table, set()).add(name)

    expected = {t: set(names) for t, names in _EXPECTED_CHECKS.items()}
    assert live == expected, (
        f"真库 CHECK 与登记不符。\n"
        f"  只在真库："
        f"{ {t: sorted(v - expected.get(t, set())) for t, v in live.items()} }\n"
        f"  只在登记："
        f"{ {t: sorted(v - live.get(t, set())) for t, v in expected.items()} }"
    )
    total = sum(len(v) for v in live.values())
    assert total == 40, f"CHECK 总数应当是 40，实际 {total}"


#: 已登记的**跨包入向 FK**（别的包建在本包 5 表上的 FK 名）。
#:
#: ## 这里为什么可以有一份"人写名单"
#:
#: 判据纪律禁止"覆盖范围由人写"。但这里人写的**不是覆盖范围**，而是
#: **已经发生并被批准的事件**（"哪些跨包引用被接受了"）。范围的真相由两个
#: **互相独立**的来源给出，并被下面两条判据逼着相遇：
#:
#: | 来源 | 读法 | 对应判据 |
#: |---|---|---|
#: | **真库** `pg_constraint` | `cross_package_foreign_keys` | 库侧不许有未登记的 |
#: | **ORM 元数据**（靠磁盘枚举模块） | `_derived_cross_package_fk_names` | 名单侧不许有元数据里不存在的 |
#:
#: 任一侧多出条目都会红。**新增条目时必须先确认夹具跟得上**
#: （`conftest.truncate_targets` 会自动带上入向引用闭包，所以通常跟得上），
#: 再登记到这里 **并同步改 `docs/regression-register.md` RG-130**。
#: ⚠️ **不得为了变绿而删条目** —— 那是"把断言放宽"，RG-130 明确禁止。
_KNOWN_CROSS_PACKAGE_FKS: frozenset[str] = frozenset(
    {
        # WP27 阶段 4（媒体表落库）：两条 FK 指向 `content_items`。
        # 它们同时把 4 张 media 表拖进 `truncate_targets` 的闭包
        # （沿 `media_versions` / `media_renditions` 继续扩散）。
        "fk_media_items_content_item_id_content_items",
        "fk_media_generation_contexts_content_item_id_content_items",
        # WP35（固定版本 ZIP 导出）落库：`export_items.content_item_id` 指向
        # `content_items`。本条把 `export_items` 拖进 `truncate_targets` 的闭包。
        # 夹具侧无需改动：`truncate_targets` 取全量 FK 边递归到不动点、
        # `drop_own_tables` 保留被跨包引用的表，两者都是通用算法（已核 conftest）。
        # ⚠️ 本条曾**假绿一轮**（2026-10-16 清晨那轮全量复跑）：当时 `rednote_test`
        #    尚未迁到含 `export_items` 的 head，库侧 `pg_constraint` 查不到这条 FK
        #    ⇒ 本判据通过而真相是"库落后于迁移链"（ISS-95 形态）。库升到 head 后即红。
        "fk_export_items_content_item_id_content_items",
        # WP29（预览/编辑/反馈定位）落库：`content_feedback_items` /
        # `content_feedback_events` / `content_reviews` 三表共 6 条 FK 指向
        # `content_items` / `content_versions`。把 feedback 的三张表拖进
        # `truncate_targets` 的闭包；夹具侧同样无需改动（通用算法）。
        # 其中两条约束名经手动缩短（见 `modules/feedback/models.py`
        # `ContentFeedbackEvent` 的注释）：自动生成的全名超过 PostgreSQL
        # 63 字节标识符上限会被库静默截断，所以这里登记的是**缩短后**的名字。
        "fk_content_feedback_items_content_item_id_content_items",
        "fk_content_feedback_items_observed_version_id_content_versions",
        "fk_content_feedback_items_resolved_version_id_content_versions",
        "fk_content_feedback_events_resulting_version_id_versions",
        "fk_content_reviews_content_item_id_content_items",
        "fk_content_reviews_subject_version_id_content_versions",
        # WP30（AI 自检/硬软规则）落库：`self_check_runs` 两条 FK 指向
        # `content_items` / `content_versions`（第五次发生，规律同上）。
        "fk_self_check_runs_content_item_id_content_items",
        "fk_self_check_runs_subject_version_id_content_versions",
        # WP36（KOC 记录/交付与替换/发布登记）落库：`deliveries.content_item_id`
        # 指向 `content_items`（第六次发生）。
        "fk_deliveries_content_item_id_content_items",
        # WP31（试产关口 / 淘汰补产）落库：`trial_round_slots.content_item_id` 指向
        # `content_items`（第七次发生）。
        "fk_trial_round_slots_content_item_id_content_items",
        # WP32（扩产 / 批产）落库：`batch_items.content_item_id` 指向 `content_items`
        # （第八次发生）。两包同日并行落地，FK 名互不相关。
        "fk_batch_items_content_item_id_content_items",
        # WP33（重复检查 / 质量报告）落库：`quality_report_items` 三条 FK 指向
        # `content_items` / `content_versions` / `generation_contexts`（第九次发生）。
        # 它们把 `quality_report_items` 拖进 `truncate_targets` 的闭包；夹具侧无需改动。
        "fk_quality_report_items_content_item_id_content_items",
        "fk_quality_report_items_content_version_id_content_versions",
        "fk_quality_report_items_generation_context_id",
    }
)


def _derived_cross_package_fk_names() -> set[str]:
    """从**磁盘**枚举全部模块的模型，派生"别家建在本包 5 表上的 FK 名"。

    与 `migrations/env.py` 同法：模块清单**由磁盘决定**，不手写
    （判据纪律第 14 条）。手写清单核对不了"模型里到底有没有这条 FK"——
    而白名单腐烂（某条 FK 被删后条目残留）会让上一条判据**永久放行**那个名字。
    """
    import importlib
    import pathlib

    import rednote.modules as _modules_root

    root = pathlib.Path(_modules_root.__file__).parent
    for path in sorted(root.iterdir()):
        if path.is_dir() and (path / "models.py").exists():
            importlib.import_module(f"rednote.modules.{path.name}.models")

    own = set(_TABLE_NAMES)
    found: set[str] = set()
    for table in Base.metadata.tables.values():
        if table.name in own:
            continue
        for constraint in table.constraints:
            # 鸭子类型：只有 ForeignKeyConstraint 才有 `referred_table`。
            referred = getattr(constraint, "referred_table", None)
            if referred is not None and referred.name in own:
                found.add(constraint.name)
    return found


@pytest.mark.regression
def test_cross_package_inbound_foreign_keys_are_whitelisted(
    env: ContentEnv,
) -> None:
    """★ 本包 5 表上的跨包入向 FK **只允许**是已登记的那几条（当前 2 条）。

    ## 这条判据保护的是什么

    `conftest.drop_own_tables` 的筛选逻辑是"只删没有跨包入向 FK 的表"。
    一旦有别的包在本包表上建 FK，那两张表的处置就变了（**保留而非 DROP**），
    同时 `conftest.truncate_targets` 的闭包会变大。**"变了"必须有人当场知道**
    —— 否则是"夹具静默少删一张表"，症状会在别处表现为一堆莫名其妙的失败
    （WP27 落库时实测：93 errors，看着像"实现坏了"）。

    ## 与初版（`..._are_zero`）的关系：口径更新，不是放宽

    初版断言 `cross == set()`（本包表上零跨包 FK）。WP27 落库后这是**已知的
    设计事实**（`media_items` / `media_generation_contexts` → `content_items`），
    故改成"⊆ 白名单"。但**新增的未登记 FK 仍然报红** —— "第一现场"的职责没变，
    变的只是把"零"换成"一份显式且有对账的白名单"。
    单方向放宽由 `test_known_cross_package_fk_whitelist_is_not_stale` 补回：
    两条合起来是**双向**的（库侧不多、名单侧不少）。
    """
    live = cross_package_foreign_keys(get_engine(_make_settings()), _TABLE_NAMES)
    unregistered = live - _KNOWN_CROSS_PACKAGE_FKS
    assert not unregistered, (
        f"出现了**未登记**的跨包入向 FK：{sorted(unregistered)} —— "
        "`drop_own_tables` 会因此保留本包的表、`truncate_targets` 的闭包会变大，"
        "夹具行为已变。处置：先确认夹具跟得上，再登记进 "
        "`_KNOWN_CROSS_PACKAGE_FKS` **并同步改 `docs/regression-register.md` RG-130**。"
        "**不要**为了变绿而删条目（RG-130 明确禁止把断言放宽）。"
    )


@pytest.mark.regression
def test_known_cross_package_fk_whitelist_is_not_stale() -> None:
    """★ 登记的白名单**不得腐烂**：每条都必须在 ORM 元数据里真实存在。

    这是上一条判据的另一半（两个独立来源的相遇）。没有它，"白名单"会退化成
    "只要能过就往上加"的抽屉：某条 FK 被删掉后，残留条目会让上一条判据
    **永久放行**那个名字 —— 同形态在本仓已有先例
    （`check_media_asset_usability_single_source` 的 G9b「引用豁免白名单不腐烂」）。

    ⚠️ 本判据**只看元数据、不看库** ⇒ 对"测试库停在旧迁移版本"免疫
    （若它读库，本机 `rednote_test` 停在 WP25 就会假红）。
    """
    derived = _derived_cross_package_fk_names()
    stale = _KNOWN_CROSS_PACKAGE_FKS - derived
    assert not stale, (
        f"白名单里的跨包 FK 在 ORM 元数据里不存在（已腐烂）：{sorted(stale)} —— "
        f"磁盘枚举派生出的实际集合：{sorted(derived)}。"
        "请删掉白名单里的陈旧条目。"
    )


# ===========================================================================
# 第 4 层：HTTP（TestClient）
# ===========================================================================

API_PREFIX = "/api/v1"


@pytest.fixture
def app(env: ContentEnv) -> FastAPI:
    """按注入配置装配 app，并**在 `main.py` 尚未接线时**补挂本模块 router。

    **挂载必须幂等**：`main.py` 已 `include_router(content_router, ...)`
    （本机实测 app 里 6 条 content 路由），此时再挂一次会让每条路由
    **重复注册**（WP20 收口时实测：路由数翻倍）。

    探测依据用**路径前缀**而不是 router 对象身份：`create_app` 内部 import
    的是同一个模块级 `router`，但 `include_router` 会把它**复制成新对象**，
    身份比较不可靠。
    """
    from rednote.main import create_app

    assert env.settings.api_prefix == API_PREFIX, (
        f"本文件写死了 {API_PREFIX!r}，注入配置给的是 "
        f"{env.settings.api_prefix!r} —— 两者必须一致"
    )

    application = create_app(env.settings)

    mounted_path = f"{API_PREFIX}/projects/{{projectId}}/content-generations"
    already_mounted = any(
        getattr(route, "path", "") == mounted_path for route in application.routes
    )
    if not already_mounted:
        from rednote.modules.content.api import router as content_router

        application.include_router(content_router, prefix=env.settings.api_prefix)
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, env: ContentEnv, *, who: str = "owner") -> None:
    """造一条会话并写 Cookie（含 CSRF 令牌）。

    ## 为什么 CSRF 必须一起处理（不能绕过）

    `current_user` 依赖对**写方法**强制 `verify_csrf`，且它是**结构上无法
    绕过**的。绕过它会让本层不再验证真实的鉴权链 —— 那时"403 断言"
    就成了对一段假鉴权的断言。

    ## `who` 的三档

    | `who` | 用户 | 本层用途 |
    |---|---|---|
    | `"owner"` | `ROLE_OPERATOR`，项目成员 | 正常路径 200/201/202 |
    | `"admin"` | `ROLE_ADMIN`，**非成员** | 管理员全权（`allows` 第一句 `if is_admin: return True`） |
    | `"outsider"` | `ROLE_OPERATOR`，**非成员** | 404（防枚举） |
    """
    from rednote.platform.csrf import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
    from rednote.platform.tokens import generate_session_token, hash_token

    user_id = {
        "owner": env.owner_id,
        "admin": env.admin_id,
        "outsider": env.outsider_id,
    }[who]
    raw_token = generate_session_token()
    with session_scope(env.settings) as session:
        IdentityRepository(session).create_session(
            user_id=user_id,
            token_hash=hash_token(raw_token),
            csrf_secret_hash=hash_token(CSRF_SECRET_PLAINTEXT),
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    client.cookies.set(CSRF_COOKIE_NAME, CSRF_SECRET_PLAINTEXT)


def _write_headers(**extra: str) -> dict[str, str]:
    """写请求的公共头：CSRF 令牌 + Origin。

    两者都带上，本层的 403 断言才只可能来自**权限**判定
    （否则 403 可能来自 CSRF，而那是环境问题，不是权限问题）。
    """
    headers = {"X-CSRF-Token": CSRF_SECRET_PLAINTEXT, "Origin": TEST_ORIGIN}
    headers.update(extra)
    return headers


def _generations_url(env: ContentEnv, *, project_id: uuid.UUID | None = None) -> str:
    return f"{API_PREFIX}/projects/{project_id or env.project_id}/content-generations"


def _contents_url(env: ContentEnv, *, project_id: uuid.UUID | None = None) -> str:
    return f"{API_PREFIX}/projects/{project_id or env.project_id}/contents"


def _detail_url(content_id: uuid.UUID) -> str:
    return f"{API_PREFIX}/contents/{content_id}"


def _versions_url(content_id: uuid.UUID) -> str:
    return f"{API_PREFIX}/contents/{content_id}/versions"


def _regenerations_url(content_id: uuid.UUID) -> str:
    return f"{API_PREFIX}/contents/{content_id}/regenerations"


def _title_adoptions_url(content_id: uuid.UUID) -> str:
    return f"{API_PREFIX}/contents/{content_id}/title-adoptions"


def _generation_body(
    env: ContentEnv, *, content_count: int = 1, scope: str = SCOPE_COPY,
    batch_kind: str = "TRIAL",
) -> dict[str, Any]:
    return {
        "contentCount": content_count,
        "scope": scope,
        "batchKind": batch_kind,
        "modelProfileId": str(env.model_profile_id),
    }


def _regeneration_body(
    env: ContentEnv, *, scope: str = SCOPE_COPY, batch_kind: str = "TRIAL",
) -> dict[str, Any]:
    return {
        "scope": scope,
        "batchKind": batch_kind,
        "modelProfileId": str(env.model_profile_id),
    }


@pytest.mark.regression
def test_router_exposes_exactly_six_endpoints(app: FastAPI) -> None:
    """本模块**恰好注册 6 个端点**。

    多一个少一个都会让 `api.py` docstring 里那张 6 行表失去可核对性 ——
    而那张表是本包对协议的唯一形状声明。

    ## 判据范围：只数**本包 router 自己**声明的操作

    刻意**不数 `app.routes` 里所有相关路径**：
    `/projects/{projectId}/...` 是**共用前缀**（几乎所有业务包都有），
    按 `app.routes` 计数会在别的包合法加端点时变红。

    因此分两句各自成立：

      1. **本包 router 自己**恰好 6 个操作（本包的事实，稳定）；
      2. 6 个全都真的挂在 `app` 上（接线正确，允许别人加端点）。

    第 1 句用 `router.routes` 而不是 `app.routes`，这是本项目
    ISS-44/64/71/83/87 的重复教训：**判据只能断言"我建的还在"，
    不能断言"全项目恰好有 N 个"**。
    """
    from rednote.modules.content.api import router as content_router

    own_operations = {
        (route.path, method)
        for route in content_router.routes
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }
    expected = {
        ("/projects/{projectId}/content-generations", "POST"),
        ("/projects/{projectId}/contents", "GET"),
        ("/contents/{contentId}", "GET"),
        ("/contents/{contentId}/versions", "GET"),
        ("/contents/{contentId}/regenerations", "POST"),
        ("/contents/{contentId}/title-adoptions", "POST"),
    }
    assert own_operations == expected, (
        f"多出：{sorted(own_operations - expected)}；"
        f"缺少：{sorted(expected - own_operations)}"
    )
    assert len(expected) == 6

    mounted = {
        (route.path, method)
        for route in app.routes
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }
    missing = {(f"{API_PREFIX}{path}", method) for path, method in expected} - mounted
    assert not missing, f"本包的端点在 app 里找不到：{sorted(missing)}"


def test_create_generations_returns_202(client: TestClient, env: ContentEnv) -> None:
    """★ `POST .../content-generations` 成功码 **202**（不是 200/201）。

    ## 202 的语义

    请求**已接受**，但内容**还没生成**（任务已入队，由 worker 执行）。
    201 会让前端以为"内容已经在那儿了"而立刻去查详情 —— 查不到。

    ## 响应形状（三条）

    1. `contentCount` == 请求的条数；
    2. `contents[]` 每项含 `contentId`（新行的 id **只有服务端知道**）
       + `jobId` + `kind` + `state` + `created`；
    3. `scope` 是**归一后**的值（`TAGS` → `COPY`）。
    """
    _login(client, env, who="owner")
    response = client.post(
        _generations_url(env),
        json=_generation_body(env, content_count=3, scope=SCOPE_COPY),
        headers=_write_headers(),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"data", "meta"}
    data = body["data"]
    assert data["contentCount"] == 3
    assert data["scope"] == SCOPE_COPY
    assert len(data["contents"]) == 3
    for item in data["contents"]:
        assert set(item) == {
            "contentId", "jobId", "kind", "state", "created",
            "sceneKey", "formKey",  # 接缝 §5.15 新增
        }
        assert item["kind"] == KIND_CONTENT_GENERATE
        assert item["created"] is True
        uuid.UUID(item["contentId"])
    assert "requestId" in body["meta"]


def test_create_generations_normalizes_tags_scope(
    client: TestClient, env: ContentEnv
) -> None:
    """★ `TAGS` 进来 ⇒ 响应回带 **`COPY`**（不撒谎）。

    回带 `TAGS` 会让前端以为"只改了标签"，而实际 `CONTENT_COPY`
    连正文一起改了 —— 用户看到正文变了却没有任何提示。
    """
    _login(client, env, who="owner")
    response = client.post(
        _generations_url(env),
        json=_generation_body(env, scope=SCOPE_TAGS),
        headers=_write_headers(),
    )
    assert response.status_code == 202, response.text
    assert response.json()["data"]["scope"] == SCOPE_COPY


def test_create_generations_rejects_zero_count(
    client: TestClient, env: ContentEnv
) -> None:
    """`contentCount=0` ⇒ **422**（请求体 `ge=1`）。

    0 条会建 0 个任务却返回 202 —— 调用方以为提交了
    （`service.start_generation` 的 docstring 原文）。
    """
    _login(client, env, who="owner")
    response = client.post(
        _generations_url(env),
        json=_generation_body(env, content_count=0),
        headers=_write_headers(),
    )
    assert response.status_code == 422, response.text


def test_create_generations_rejects_over_max_count(
    client: TestClient, env: ContentEnv
) -> None:
    """`contentCount > 50` ⇒ **422**（端点侧硬上限）。

    上限存在的意义：一次请求建 N 条身份行 + N 个任务，N 无上限时
    一个请求能压垮任务表。50 与 `MAX_CONTENT_COUNT_PER_REQUEST` 同值
    （本文件从 `api` 读那个常量，不写死字面量）。
    """
    from rednote.modules.content.api import MAX_CONTENT_COUNT_PER_REQUEST

    _login(client, env, who="owner")
    response = client.post(
        _generations_url(env),
        json=_generation_body(env, content_count=MAX_CONTENT_COUNT_PER_REQUEST + 1),
        headers=_write_headers(),
    )
    assert response.status_code == 422, response.text


def test_create_generations_is_idempotent_with_same_key(
    client: TestClient, env: ContentEnv
) -> None:
    """★ 同一个 `Idempotency-Key` + 同一个体 ⇒ 复用（`created=False`）。

    两次请求返回**同一批** `contentId`（用集合比对 —— 仓储无 `ORDER BY`，
    顺序不稳定，见第 2 层的实测记录）。
    """
    _login(client, env, who="owner")
    body = _generation_body(env, content_count=2)
    headers = _write_headers(**{"Idempotency-Key": "http-replay-1"})

    first = client.post(_generations_url(env), json=body, headers=headers)
    assert first.status_code == 202, first.text
    second = client.post(_generations_url(env), json=body, headers=headers)
    assert second.status_code == 202, second.text

    ids_first = {item["contentId"] for item in first.json()["data"]["contents"]}
    ids_second = {item["contentId"] for item in second.json()["data"]["contents"]}
    assert ids_first == ids_second
    assert all(item["created"] is True for item in first.json()["data"]["contents"])
    assert all(item["created"] is False for item in second.json()["data"]["contents"])


def test_create_generations_conflicts_on_same_key_different_body(
    client: TestClient, env: ContentEnv
) -> None:
    """★★ 同键不同体 ⇒ **409 `IDEMPOTENCY_CONFLICT`**（不是 422）。

    ## 为什么是 409 而不是 422

    `platform/errors.py` 的 `_MESSAGE_BY_CODE` 原文是"相同幂等键对应了
    不同的请求内容"，且它**不可重试**（`_RETRYABLE_BY_CODE` 只放行
    429 / 503）—— 客户端该做的是**换一个 key 重发**，而不是重试同一个。

    ## 两种"不同体"都要判

    `api.py` 的判据是**两个独立事实的相遇**（docstring 原文）：

      · 命中的那批行的 `generation_request` 是否等于本次请求的载荷；
      · 命中的条数是否等于本次请求的 `contentCount`。

    **只判载荷会漏掉"第一次 3 条、第二次改传 5 条但参数不变"**
    （载荷相同、条数不同），而那时的表现是"响应里只有 3 条却返回 202"。

    本用例覆盖**条数不同**这一半（载荷那半由下一条覆盖）。
    同时断言 `details` 的四个字段 —— 它们是客户端能据此排障的唯一线索。
    """
    _login(client, env, who="owner")
    headers = _write_headers(**{"Idempotency-Key": "http-conflict-1"})

    first = client.post(
        _generations_url(env),
        json=_generation_body(env, content_count=3),
        headers=headers,
    )
    assert first.status_code == 202, first.text

    second = client.post(
        _generations_url(env),
        json=_generation_body(env, content_count=5),
        headers=headers,
    )
    assert second.status_code == 409, second.text
    body = second.json()
    assert body["error"]["code"] == "IDEMPOTENCY_CONFLICT", body
    details = body["error"]["details"]
    assert details["reason"] == "idempotency_key_reused"
    assert details["idempotencyKey"] == "http-conflict-1"
    assert details["idempotencyScope"] == IDEMPOTENCY_SCOPE_CONTENT_GENERATE
    assert details["existingContentCount"] == 3
    assert details["requestedContentCount"] == 5


def test_create_generations_conflicts_on_same_key_different_scope(
    client: TestClient, env: ContentEnv
) -> None:
    """同键 + 条数相同但 **scope 不同** ⇒ 409（载荷那一半）。

    与上一条配对：条数相同、载荷不同。少了这条，
    `existing_payload != payload` 那半个条件可能被删掉而**无人发现**。
    """
    _login(client, env, who="owner")
    headers = _write_headers(**{"Idempotency-Key": "http-conflict-2"})

    first = client.post(
        _generations_url(env),
        json=_generation_body(env, content_count=2, scope=SCOPE_COPY),
        headers=headers,
    )
    assert first.status_code == 202, first.text

    second = client.post(
        _generations_url(env),
        json=_generation_body(env, content_count=2, scope=SCOPE_FULL),
        headers=headers,
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_create_generations_requires_edit_permission(
    client: TestClient, env: ContentEnv
) -> None:
    """★ 非成员发起生成 ⇒ **404**（不是 403）。

    ## 为什么是 404

    `resolve_project_access` 的 docstring：项目不存在 ⇒ 404；
    **非成员 ⇒ 404**（防项目存在性枚举）。403 会让攻击者用
    "403 还是 404"区分"项目存在但我不在"与"项目不存在"。
    """
    _login(client, env, who="outsider")
    response = client.post(
        _generations_url(env),
        json=_generation_body(env),
        headers=_write_headers(),
    )
    assert response.status_code == 404, response.text


def test_create_generations_unauthenticated_is_401(
    client: TestClient, env: ContentEnv
) -> None:
    """未登录 ⇒ 401（连 CSRF 都过不了）。

    这条与上一条配对：401（没身份）与 404（有身份但不是成员）
    是**两个不同的**失败，混在一起说明鉴权链少了一环。
    """
    response = client.post(
        _generations_url(env),
        json=_generation_body(env),
        headers=_write_headers(),
    )
    assert response.status_code == 401, response.text


def test_create_generations_admin_can_write(
    client: TestClient, env: ContentEnv
) -> None:
    """管理员（**非成员**）可以发起生成 —— `allows` 第一句 `if is_admin`。

    这条与"outsider 404"配对，说明 404 来自**成员判定**而不是"所有非成员都拒"。
    """
    _login(client, env, who="admin")
    response = client.post(
        _generations_url(env),
        json=_generation_body(env),
        headers=_write_headers(),
    )
    assert response.status_code == 202, response.text


def test_list_contents_returns_paginated_envelope(
    client: TestClient, env: ContentEnv
) -> None:
    """★ `GET .../contents` 返回 `PaginatedEnvelope`（`data[]` + `page`）。

    ## 游标分页的形状

    `page` 块只有 `nextCursor` / `hasMore`（**没有 `total`**）。
    `total` 被刻意丢弃：协议 §13 的游标分页不需要总数，
    硬塞会被 `response_model` 静默剥掉。

    列表项**不含正文**（`ContentSummaryBody` 只有 7 个字段）——
    列表页不需要把 20KB 正文乘上条数拉回来。
    """
    _login(client, env, who="owner")
    client.post(
        _generations_url(env),
        json=_generation_body(env, content_count=2),
        headers=_write_headers(),
    )

    response = client.get(_contents_url(env))
    assert response.status_code == 200, response.text
    body = response.json()
    # ★ `page` 是**顶层**键，不是 `meta` 的子键（`pagination.list_response()` 原文）。
    # 这里必须断三个顶层键：`page` 被 `response_model` 剥掉是本仓踩过的坑
    # （`PaginatedEnvelope` docstring：症状是"只能看第一页"且无任何报错）。
    assert set(body) == {"data", "page", "meta"}
    assert isinstance(body["data"], list)
    assert len(body["data"]) == 2
    for item in body["data"]:
        assert set(item) == {
            "contentId", "projectId", "title", "state",
            "currentVersionNo", "createdAt", "updatedAt",
            "sceneKey", "formKey",  # 接缝 §5.15 新增
        }
        assert item["state"] == CONTENT_STATE_DRAFT
        assert item["currentVersionNo"] == 0
        assert "body" not in item, "列表项带上了正文"
    assert "nextCursor" in body["page"]
    assert "hasMore" in body["page"]
    # ★ `total` 被刻意丢弃（游标分页不需要总数）—— 断言它**不在**
    assert "total" not in body["page"]


def test_list_contents_requires_view_permission(
    client: TestClient, env: ContentEnv
) -> None:
    """非成员读列表 ⇒ 404（与写路径同款，防枚举）。"""
    _login(client, env, who="outsider")
    response = client.get(_contents_url(env))
    assert response.status_code == 404, response.text


def test_get_content_detail_shape(client: TestClient, env: ContentEnv) -> None:
    """★ `GET /contents/{contentId}` 的 10 个字段，**payload / blueprint /
    bindings 用 `dict` 不展开**（各自的理由见 `api.py`）。

    ## 这条判据证的是 AQ-01 的核对面

    `bindings` 给出**逐段**的证据绑定（哪一段依据哪条 claim），
    而不是"整篇引用了基线"。字段在响应里就是这条能力的存在证明。

    ## 刚发起的内容：`payload` 为 `None`、`currentVersionNo` 为 0

    "已发起但还没生成"是一个**合法中间态**，详情端点必须能读它
    —— 前端在 202 之后立刻跳详情页，看到的就是这个状态。
    """
    _login(client, env, who="owner")
    created = client.post(
        _generations_url(env),
        json=_generation_body(env),
        headers=_write_headers(),
    )
    content_id = created.json()["data"]["contents"][0]["contentId"]

    response = client.get(_detail_url(uuid.UUID(content_id)))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == {
        "contentId", "projectId", "state", "currentVersionId",
        "currentVersionNo", "payload", "blueprint", "titleCandidates",
        "bindings", "contexts",
    }
    assert data["contentId"] == content_id
    assert data["projectId"] == str(env.project_id)
    assert data["state"] == CONTENT_STATE_DRAFT
    assert data["currentVersionId"] is None
    assert data["currentVersionNo"] == 0
    assert data["payload"] is None
    assert data["blueprint"] is None
    assert data["titleCandidates"] == []
    assert data["bindings"] == []
    assert data["contexts"] == []


def test_get_content_detail_not_found(client: TestClient, env: ContentEnv) -> None:
    """不存在的 `contentId` ⇒ **404**（两步骤鉴权的第一步）。"""
    _login(client, env, who="owner")
    response = client.get(_detail_url(uuid.uuid4()))
    assert response.status_code == 404, response.text


def test_get_content_detail_outsider_is_404(
    client: TestClient, env: ContentEnv
) -> None:
    """非成员读详情 ⇒ 404（两步骤鉴权的第二步）。"""
    _login(client, env, who="owner")
    created = client.post(
        _generations_url(env),
        json=_generation_body(env),
        headers=_write_headers(),
    )
    content_id = created.json()["data"]["contents"][0]["contentId"]

    _login(client, env, who="outsider")
    response = client.get(_detail_url(uuid.UUID(content_id)))
    assert response.status_code == 404, response.text


def test_list_versions_empty_then_paginated(
    client: TestClient, env: ContentEnv
) -> None:
    """★ `GET /contents/{contentId}/versions`：无版本时给空列表（不是 404）。

    内容存在但还没有版本是**合法状态**（刚发起）。返回 404 会让前端
    以为内容不存在 —— 而它就在列表里。

    版本行本身**不给 `body` / `bindings` / `payload`**（版本历史是
    "挑一版去看"，正文由详情端点给）。
    """
    _login(client, env, who="owner")
    created = client.post(
        _generations_url(env),
        json=_generation_body(env),
        headers=_write_headers(),
    )
    content_id = uuid.UUID(created.json()["data"]["contents"][0]["contentId"])

    response = client.get(_versions_url(content_id))
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"data", "page", "meta"}
    assert body["data"] == []
    # ★ 顶层 `page`，不是 `meta.page`（同 `test_list_contents_returns_paginated_envelope`）
    assert body["page"]["hasMore"] is False

    # 造一版 + 一次采纳 ⇒ 2 版
    #
    # ★ 节点行与版本行必须在**同一事务**里插入。
    # 分成两次 `_raw()`（各自 commit）会让第一次提交时节点处于
    # "已插入、尚无配对版本行"的状态 —— `trg_version_nodes_content_paired`
    # 是**延迟**的，它在 commit 那一刻检查，于是当场报
    # `23514 孤立节点`。这不是"测试写错了"，而是**不可变性设计的必然**：
    # 两张表都不许事后补，只能同事务一起成立。
    # 这也正是 `_seed_valid_content()` 用**一个** `session_scope` 的原因。
    version_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(_INSERT_PREP_NODE),
            {"node_id": version_id, "content_id": content_id,
             "payload_hash": f"h-{version_id.hex[:12]}"},
        )
        session.execute(
            text(_INSERT_VERSION),
            {"version_id": version_id, "content_id": content_id,
             "version_no": 1, "payload_hash": f"ph-{version_id.hex[:12]}"},
        )
    candidate = _seed_candidate(env, version_id=version_id, title="新标题")

    adopted = client.post(
        _title_adoptions_url(content_id),
        json={"candidateId": str(candidate)},
        headers=_write_headers(),
    )
    assert adopted.status_code == 201, adopted.text

    response = client.get(_versions_url(content_id))
    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    assert len(rows) == 2
    # 新的在前（仓储 order_by version_no desc）
    assert rows[0]["versionNo"] == 2
    assert rows[1]["versionNo"] == 1
    for row in rows:
        assert set(row) == {
            "versionId", "versionNo", "title", "blueprintId",
            "adoptedTitleCandidateId", "generationContextId",
            "payloadHash", "schemaVersion", "createdAt", "createdBy",
        }
        assert "body" not in row, "版本历史带上了正文"


def test_create_regeneration_returns_202(client: TestClient, env: ContentEnv) -> None:
    """★ `POST /contents/{contentId}/regenerations` ⇒ **202** + `taskKinds`。

    `taskKinds` 由 `service.prepare_regeneration` 算出（**不在端点重算**）——
    重算就是同一份映射的第二处定义（ISS-66 形态），分叉时响应撒谎。
    """
    _login(client, env, who="owner")
    created = client.post(
        _generations_url(env),
        json=_generation_body(env),
        headers=_write_headers(),
    )
    content_id = created.json()["data"]["contents"][0]["contentId"]

    response = client.post(
        _regenerations_url(uuid.UUID(content_id)),
        json=_regeneration_body(env, scope=SCOPE_FULL),
        headers=_write_headers(),
    )
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["contentId"] == content_id
    assert data["scope"] == SCOPE_FULL
    assert data["taskKinds"] == [TASK_CONTENT_STRUCTURE, TASK_CONTENT_COPY]
    assert set(data["job"]) == {"jobId", "kind", "state", "created"}


def test_create_regeneration_without_blueprint_is_422(
    client: TestClient, env: ContentEnv
) -> None:
    """★★ 无蓝图 + `COPY` ⇒ **422**，且 `fieldErrors` 指出 `scope`。

    ## 为什么是 422（请求的错）而不是 409（状态）

    `_HTTP_BY_REASON` 的分组判据原文是「**换一个请求参数能否成功？**」——
    换 `FULL` 就能成功 ⇒ 是请求的错 ⇒ 422。
    `fieldErrors` 的 `field` 是 `"scope"`（让前端能定位到那个输入框）。

    ## 为什么必须在这里（而不是等到模型调用时）

    `service` 的 docstring：没有蓝图时文案调用会在 `render_system_prompt`
    处抛"缺 `blueprint` 值" —— 报错点是提示词渲染，根因是"没有蓝图"。
    两者隔了一层，排障会去查提示词模板。
    """
    _login(client, env, who="owner")
    created = client.post(
        _generations_url(env),
        json=_generation_body(env),
        headers=_write_headers(),
    )
    content_id = uuid.UUID(created.json()["data"]["contents"][0]["contentId"])

    response = client.post(
        _regenerations_url(content_id),
        json=_regeneration_body(env, scope=SCOPE_COPY),
        headers=_write_headers(),
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED", error
    assert error["details"]["reason"] == REASON_SCOPE_REQUIRES_BLUEPRINT
    fields = {item["field"] for item in error["details"]["fieldErrors"]}
    assert "scope" in fields, error["details"]


def test_create_regeneration_full_without_blueprint_is_202(
    client: TestClient, env: ContentEnv
) -> None:
    """`FULL` + 无蓝图 ⇒ **202**（与上一条配对：拒的是范围，不是缺蓝图）。"""
    _login(client, env, who="owner")
    created = client.post(
        _generations_url(env),
        json=_generation_body(env),
        headers=_write_headers(),
    )
    content_id = created.json()["data"]["contents"][0]["contentId"]

    response = client.post(
        _regenerations_url(uuid.UUID(content_id)),
        json=_regeneration_body(env, scope=SCOPE_FULL),
        headers=_write_headers(),
    )
    assert response.status_code == 202, response.text


def test_create_regeneration_not_found(client: TestClient, env: ContentEnv) -> None:
    """不存在的内容 ⇒ 404。"""
    _login(client, env, who="owner")
    response = client.post(
        _regenerations_url(uuid.uuid4()),
        json=_regeneration_body(env),
        headers=_write_headers(),
    )
    assert response.status_code == 404, response.text


def test_create_regeneration_requires_edit(client: TestClient, env: ContentEnv) -> None:
    """非成员重做 ⇒ 404（与写路径同款）。"""
    _login(client, env, who="owner")
    created = client.post(
        _generations_url(env),
        json=_generation_body(env),
        headers=_write_headers(),
    )
    content_id = uuid.UUID(created.json()["data"]["contents"][0]["contentId"])

    _login(client, env, who="outsider")
    response = client.post(
        _regenerations_url(content_id),
        json=_regeneration_body(env),
        headers=_write_headers(),
    )
    assert response.status_code == 404, response.text


def test_create_title_adoption_returns_201(
    client: TestClient, env: ContentEnv
) -> None:
    """★★ `POST /contents/{contentId}/title-adoptions` ⇒ **201**（不是 200）。

    ## 为什么是 201

    它**创建了一个新资源**（一条 `content_versions` 行）——
    `models.py` docstring 六 的口径：「采纳一个标题候选 ⇒ **追加一版**」。
    因此返回体是**新版本**的摘要，而不是"被修改的内容"。

    200 会让前端以为"内容被改了一下"，而实际发生的是
    "内容的第 N+1 版产生了、第 N 版原样留在历史里" ——
    这个区别就是 demo 上"只重做标题"与"整篇重做"能对比的前提。

    ## 同步 201（不入队）

    与两条 202 不同：采纳**不调模型**（标题已在候选行里），
    因此没有异步可言 —— 它是纯数据库写，同一事务内完成。
    """
    _login(client, env, who="owner")
    content_id, version_id = _seed_valid_content(env)
    candidate = _seed_candidate(env, version_id=version_id, title="被采纳标题")

    response = client.post(
        _title_adoptions_url(content_id),
        json={"candidateId": str(candidate)},
        headers=_write_headers(),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"data", "meta"}
    data = body["data"]
    assert data["versionNo"] == 2
    assert data["title"] == "被采纳标题"
    assert data["adoptedTitleCandidateId"] == str(candidate)
    assert data["createdBy"] == str(env.owner_id)


def test_create_title_adoption_missing_candidate_is_404(
    client: TestClient, env: ContentEnv
) -> None:
    """候选不存在 ⇒ **404**（`TITLE_CANDIDATE_NOT_FOUND`）。

    候选是被**寻址的引用对象** ⇒ 404（与"请求字段错"的 422 分界）。
    """
    _login(client, env, who="owner")
    content_id, _ = _seed_valid_content(env)
    response = client.post(
        _title_adoptions_url(content_id),
        json={"candidateId": str(uuid.uuid4())},
        headers=_write_headers(),
    )
    assert response.status_code == 404, response.text
    assert (
        response.json()["error"]["details"]["reason"]
        == REASON_TITLE_CANDIDATE_NOT_FOUND
    )


def test_create_title_adoption_cross_content_is_422(
    client: TestClient, env: ContentEnv
) -> None:
    """★★ 候选属于别的内容 ⇒ **422**，`fieldErrors` 指出 `candidateId`。

    ## 为什么 422 而不是 404

    候选**存在**（不需要换个资源路径），只是它属于另一条内容 ——
    **换一个候选就好** ⇒ 是请求的错 ⇒ 422。
    这是 `_HTTP_BY_REASON` 分组判据（"换一个请求参数能否成功"）的直接后果。

    ## 与 DDL 判据 3.2 的分工

    本层给可读 4xx；DDL 给"绕过本层也拦得住"（第 3 层已覆盖）。
    两层都要：服务层是给人看的、DDL 是给"另一条写入路径"看的。
    """
    _login(client, env, who="owner")
    _, version_a = _seed_valid_content(env)
    other_content_id, _ = _seed_valid_content(env)
    candidate_of_a = _seed_candidate(env, version_id=version_a, title="A 的候选")

    response = client.post(
        _title_adoptions_url(other_content_id),
        json={"candidateId": str(candidate_of_a)},
        headers=_write_headers(),
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED", error
    assert error["details"]["reason"] == REASON_TITLE_CANDIDATE_MISMATCH
    fields = {item["field"] for item in error["details"]["fieldErrors"]}
    assert "candidateId" in fields, error["details"]


def test_create_title_adoption_not_found_content(
    client: TestClient, env: ContentEnv
) -> None:
    """不存在的内容 ⇒ 404（两步骤鉴权第一步）。"""
    _login(client, env, who="owner")
    response = client.post(
        _title_adoptions_url(uuid.uuid4()),
        json={"candidateId": str(uuid.uuid4())},
        headers=_write_headers(),
    )
    assert response.status_code == 404, response.text


def test_create_title_adoption_requires_edit(
    client: TestClient, env: ContentEnv
) -> None:
    """非成员采纳 ⇒ 404。

    采纳是**写**动作（`ProjectAction.EDIT`）—— 少写一次 `access.require`
    会让任何项目成员都能采纳，而它是一个会改变"当前版本"的动作。
    """
    _, version_id = _seed_valid_content(env)
    content_id = _scalar(
        env,
        "SELECT content_item_id FROM content_versions WHERE id = :v",
        {"v": version_id},
    )
    candidate = _seed_candidate(env, version_id=version_id, title="新标题")

    _login(client, env, who="outsider")
    response = client.post(
        _title_adoptions_url(content_id),
        json={"candidateId": str(candidate)},
        headers=_write_headers(),
    )
    assert response.status_code == 404, response.text


def test_create_title_adoption_admin_can_write(
    client: TestClient, env: ContentEnv
) -> None:
    """管理员（非成员）可以采纳（`allows` 第一句 `is_admin`）。"""
    content_id, version_id = _seed_valid_content(env)
    candidate = _seed_candidate(env, version_id=version_id, title="新标题")

    _login(client, env, who="admin")
    response = client.post(
        _title_adoptions_url(content_id),
        json={"candidateId": str(candidate)},
        headers=_write_headers(),
    )
    assert response.status_code == 201, response.text


def test_write_endpoints_require_csrf(client: TestClient, env: ContentEnv) -> None:
    """★ 写端点缺 CSRF 令牌 ⇒ **403**（结构性防线，不是可选装饰）。

    这条与各"非成员 404"配对：说明那些 404 来自**权限**判定，
    而不是"所有非成员都被 CSRF 挡了"（那会让权限判据失去意义）。
    """
    _login(client, env, who="owner")
    response = client.post(
        _generations_url(env),
        json=_generation_body(env),
        headers={"Origin": TEST_ORIGIN},  # 故意不给 X-CSRF-Token
    )
    assert response.status_code == 403, response.text


def test_read_endpoints_allow_get_without_csrf(
    client: TestClient, env: ContentEnv
) -> None:
    """读端点（GET）**不**要求 CSRF —— CSRF 只对写方法强制。

    若 GET 也要 CSRF，"查看详情"会变成需要先取令牌的奇怪流程。
    """
    _login(client, env, who="owner")
    response = client.get(_contents_url(env))
    assert response.status_code == 200, response.text


# ===========================================================================
# 装配自查：`main.py` 必须注册本模块 router（否则第 4 层测的是假接线）
# ===========================================================================


@pytest.mark.regression
def test_main_registers_content_router(app: FastAPI) -> None:
    """`main.py` 已注册本模块 router（app 里能看到 6 条 content 路由）。

    ## 为什么这条要读**源码**

    `api` fixture 会在"未挂载"时补挂 —— 于是第 4 层全部用例
    **即使 `main.py` 忘了注册也全绿**。那让第 4 层测的是"我补挂的接线"，
    而不是"上线时真正生效的接线"（ISS-95 的形态：声称有防线，
    而防线是测试自己搭的）。

    因此这条判据直接读 `main.py` 源码里的 `include_router` 调用
    —— 它不依赖任何 fixture 的补挂行为。
    """
    main_source = (SERVICES_BACKEND / "src/rednote/main.py").read_text(encoding="utf-8")
    assert "content" in main_source, "main.py 里没有 content 相关的组装代码"
    assert "include_router" in main_source
    # 找到与 content 同段的那次 include_router
    lines = main_source.splitlines()
    assert any(
        "content" in line.lower() and "import" in line.lower()
        for line in lines
    ) or any(
        "content_router" in line for line in lines
    ), "main.py 没有引入 content router"