"""WP24（场景/形式草稿 + 数量配置）的四层测试。

对应 `superpower-workflow` 阶段 6。与 `test_baseline_wp22.py`（第 1~3 层）/
`test_prompts_wp23.py`（第 4 层）同一范式。

## 四层各测什么（以及为什么不能合并）

| 层 | 测什么 | 手段 | 为什么单独一层 |
|---|---|---|---|
| 1 | 纯函数：千分位校验/归一、最大余数法、容量削峰、DDL 文本自洽 | 直接调用，**不碰库** | 快；且"分配的不变量"必须能被**穷举**证明 —— 一旦分配路径能读库，穷举就要一个数据库，证明退化成抽样 |
| 2 | 服务层编排：草稿懒创建、部分更新语义、提交固化、**J1~J4** | 真库 + `service_*` / `settings_service_*` | "全局变更的级联刷新"是事务语义，只有在真事务里才成立 |
| 3 | **数据库约束自己会不会拦** | 原生 SQL（绕过 ORM 与 service） | 检查"绕过本层的写入"；若测试也只用 ORM，测试与实现共享同一个盲区 |
| 4 | HTTP 接线：路径、鉴权、`If-Match`、状态码 | `TestClient` | 端点组装是独立的一层，服务层全绿也可能挂错前缀 |

## 本包的三条验收线，各自打在哪一层

1. **ADR-13 批次上限配置化** —— 第 1 层（`limits` 的边界）+ 第 2 层
   （`bootstrap_defaults` / 变更审计）+ 第 3 层（`runtime_settings` 的 CHECK）。
2. **OP-60 项目级副本**（老大拍板"支持且必须有项目级副本"）—— 第 2 层的
   **J1~J4** 是本包最要紧的一组判据，第 3 层的部分唯一索引是它们的结构基础。
3. **C14/C19 数量分配** —— 第 1 层的穷举不变量 + 第 2 层的固化前置条件。

## 关于 OP-60 的"分叉风险"如何被兜住

助手在提问时**明确不推荐**"项目级副本"（全局改值时项目副本不会跟着变、
系统不报错、两个项目的上限会悄悄分叉），老大仍选择支持。因此这里的
处置是：**把分叉从一个"沉默的状态"变成一条"可断言的性质"** ——

* **J2 与 J4 互相夹逼**：J2 要求"全局变更后所有 `INHERITED` 项目生效值
  == 新全局值"；J4 要求"`MANUAL` 项目**不**跟随"。只做 J2 会漏掉
  "该跳过的没跳过"（覆盖被冲掉），只做 J4 会漏掉"该跟随的没跟随"
  （副本停在旧值）。两条必须同时成立。
* **J3 让分叉可见**：`source='MANUAL'` + `diverged=true` + `global_value`
  非 null 三条一起出现在响应里。缺任何一条，分叉就只能靠人记得去看。

## 与 `ddl.py` 判据的对应关系

第 1 层的 DDL 文本判据**与迁移同源**：迁移与测试都调
`ddl.partial_unique_index_ddl()` / `ddl.all_trigger_ddl()`，不各写一遍。
这样"索引 DDL 改了但迁移没跟"会在测试里直接暴露。

## ⚠️ 本包最易写错处：`ON CONFLICT DO NOTHING` 的否决

初稿的惰性物化伪码用的是 `INSERT … ON CONFLICT DO NOTHING`，**已被实测否决**：
它在"同一个值"的竞争下是对的（幂等），但在"不同值"的竞争下会
**丢写且不报错** ——

    T1（全局改成 V2）与 T2（全局改成 V3）并发，项目 P 尚无副本行
    · T1 算得下一修订号 = 1，插入 (rev 1, V2)
    · T2 也算得 1，插入 (rev 1, V3) ← 被部分唯一索引拒绝
    · T2 的 ON CONFLICT DO NOTHING **静默跳过**
    结果：全局 = V3，项目 P 的生效副本 = **V2**（两个数字都在、都不报错）

这正是 J2 要拦的形态。实现改为**事务级 advisory lock 按 key 串行化 +
普通 INSERT + 插入后重读**，第 2 层有一条判据（`test_materialize_reads_back
_after_insert`）钉住"重读"这一步，第 3 层有一条判据钉住"部分唯一索引确实
会报 23505"（若它静默跳过，J2 的护栏就没了）。

## 为什么 `runtime_settings` 的残留不能用 DELETE 清

它是**不可变表**（`trg_runtime_settings_immutable` 拦 UPDATE/DELETE，
SQLSTATE `23001`）。因此第 2/3 层的写入会**累积**在测试库里 ——
所有断言一律写成**差分形态**（"本次新增 = 1 行"），不写"表里有 1 行"。
这与 WP22/WP23 的"零残留用基线差分"是同一条纪律。

同理，第 3 层要构造"重复修订号"时不能写死数字（第二次跑就撞上第一次的
残留），而是**先读当前 max，再取一个更大的号**（`_next_revision`）。
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from conftest import cross_package_foreign_keys, drop_own_tables

from rednote.modules.baseline.models import ProductionBaseline  # noqa: F401
from rednote.modules.identity.models import ROLE_ADMIN, ROLE_OPERATOR
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.planning import allocation as pa
from rednote.modules.planning import ddl as pl_ddl
from rednote.modules.planning import limits as pl_limits
from rednote.modules.planning import permille as pl_permille
from rednote.modules.planning import service as pl_service
from rednote.modules.planning import settings_service as pl_settings
from rednote.modules.planning.models import PlanDraft, RuntimeSetting
from rednote.modules.planning.repository import PlanningRepository
from rednote.modules.planning.types import (
    BATCH_KINDS,
    BATCH_KIND_BULK,
    BATCH_KIND_SCALE,
    BATCH_KIND_TRIAL,
    DEFAULT_BATCH_MAX,
    DEFAULT_RESEARCH_MAX_EFFECTIVE_FILES,
    DRAFT_STATE_EDITING,
    DRAFT_STATE_SUBMITTED,
    ITEM_STATES,
    ITEM_STATE_DISABLED,
    ITEM_STATE_ENABLED,
    ITEM_STATE_LIST_SQL,
    MAX_CHANGE_REASON_CHARS,
    MAX_DIMENSION_ITEMS,
    MAX_DISABLED_REASON_CHARS,
    MAX_PERMILLE,
    MAX_QUANTITY_BOUND,
    MIN_PERMILLE,
    MIN_QUANTITY,
    PERMILLE_TOTAL,
    PLACEHOLDER_SETTING_KEYS,
    REASON_ACTOR_NOT_FOUND,
    REASON_BASELINE_MISMATCH,
    REASON_BATCH_KIND_UNKNOWN,
    REASON_CHANGE_REASON_REQUIRED,
    REASON_DRAFT_ALREADY_SUBMITTED,
    REASON_DRAFT_NOT_FOUND,
    REASON_DIMENSION_LIMIT_EXCEEDED,
    REASON_DUPLICATE_ITEM_KEY,
    REASON_PERMILLE_NOT_TOTAL,
    REASON_PROJECT_NOT_FOUND,
    REASON_QUANTITY_ABOVE_MAX,
    REASON_QUANTITY_BELOW_MIN,
    REASON_SETTING_KEY_UNKNOWN,
    REASON_SETTING_NOT_CONFIGURED,
    REASON_SETTING_VALUE_INVALID,
    REASON_TOTAL_QUANTITY_INVALID,
    SETTING_KEY_BATCH_LIMITS,
    SETTING_KEY_PLATFORM_MEDIA_LIMITS,
    SETTING_KEY_RESEARCH_MAX_FILES,
    SETTING_KEYS,
    SETTING_ORIGIN_GLOBAL,
    SETTING_ORIGIN_INHERITED,
    SETTING_ORIGIN_MANUAL,
    SETTING_SCOPE_GLOBAL,
    SETTING_SCOPE_PROJECT,
    AllocationItem,
    BatchLimits,
    EffectiveSetting,
)
from rednote.modules.projects.models import Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.modules.versions.models import ENTITY_TYPE_PLAN
from rednote.modules.versions.repository import VersionRepository
from rednote.modules.versions.service import canonical_payload_hash
from rednote.platform.configuration import Settings
from rednote.platform.database import get_engine, reset_engines, session_scope
from rednote.platform.errors import ApiError, ErrorCode
from rednote.platform.immutability import IMMUTABLE_RECORD_SQLSTATE

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
CSRF_SECRET_PLAINTEXT = "test-csrf-secret-plaintext"
TEST_ORIGIN = "http://testserver"

#: 本包自己的两张表（`version_nodes` / `projects` / `users` /
#: `production_baselines` 都**不在其中** —— 它们是别的包的表）。
OWN_TABLES: tuple[Any, ...] = (
    PlanDraft.__table__,
    RuntimeSetting.__table__,
)

#: DROP 顺序：**引用方在前**。本包两张表之间无互相引用，
#: 但它们都引用 `projects` / `users`（`plan_drafts` 另有 `production_baselines`）
#: —— 那三张表**不删**（别的包的表）。
#: 已实测确认：本仓库**没有任何外部表**引用这两张表
#: （`pg_constraint where confrelid in (...)` 为空集），
#: 因此 `drop_own_tables` 会把两张都删掉，`CASCADE` 不会波及其它模块。
DROP_ORDER: tuple[str, ...] = (
    "plan_drafts",
    "runtime_settings",
)


def _test_database_url() -> str:
    """DSN 从环境变量取（分支用 `rednote_test_wp24`），缺省本机 postgres。"""
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
    """只建本包的两张表 + 一个不可变触发器 + 两条部分唯一索引。

    **先 DROP 自己的两张表**：测试库里可能有早先的探针残留，而
    `create_all` 只对"表不存在"是幂等的 —— 表已存在但结构是旧的时候
    它会**静默跳过**，于是测试跑在一个结构不对的表上。

    **触发器必须 DROP → CREATE**：`CREATE TRIGGER` 不是幂等的
    （本机实测报 `DuplicateObject: trigger "trg_..." already exists`）。
    本例只有 `trg_runtime_settings_immutable` 一个，且它挂在**本包自己的表**上
    —— 与 WP22/WP23 不同，本包没有挂在共用表（`version_nodes`）上的触发器。

    `reset_engines()` 先跑：conftest 的 `_isolate_settings_cache` 是
    autouse 的 function 级 fixture，module 级 fixture 拿到的 engine
    可能已被上一轮清掉 —— 重取一次最稳。

    **本夹具同时是 `test_schema_parity.py` 能通过的前提**：那个文件按
    "库里的 FK"核对模型声明，而这两张表只有在本文件的夹具里被建出来
    （它们**不**在任何迁移之外的路径上被 `create_all`）。

    ## ★ 建表后追加 `TRUNCATE`（本包特有的必需步骤，勿删）

    `drop_own_tables` + `create_all` **已经**清空了这两张表（DROP TABLE
    连数据一起删）—— 那为什么还要 TRUNCATE？因为那条路**随时可能被关掉**：
    `drop_own_tables` 只删"没有跨包入向外键"的表，一旦将来某个包把外键
    指向 `plan_drafts` / `runtime_settings`，这两张表就会进 `kept` 并
    **原样保留（含全部旧数据）**。那时若没有 TRUNCATE，测试就跑在一堆
    上轮残留之上（且不报错）。

    **TRUNCATE 是清理 `runtime_settings` 的唯一手段**（本机实测）：

    | 手段 | 结果 |
    |---|---|
    | `DELETE FROM runtime_settings` | 被 `trg_runtime_settings_immutable` 拒绝，SQLSTATE `23001` |
    | `UPDATE runtime_settings …` | 同上 |
    | `TRUNCATE runtime_settings` | **成功，清空** |

    原因是不可变触发器是 `BEFORE … FOR EACH ROW` —— **行级**触发器对
    `TRUNCATE` 根本不触发（PG 的 TRUNCATE 走独立路径，要拦它得用
    `BEFORE TRUNCATE … FOR EACH STATEMENT`）。本仓已登记此性质并**刻意**
    依赖它：`tools/dev/seed_demo_data.py --reset` 就是靠 TRUNCATE 清
    `source_segments` / `audit_events` 这两张不可变表。

    ⚠️ **只在夹具里用**：生产代码里出现 TRUNCATE 等于给"不可变"开后门。
    证据脚本 `collect_wp24_evidence.py` 有一条判据全仓扫描
    `src/**` 下的 TRUNCATE 字样（应为零命中）。

    **为什么不需要 `CASCADE`**：本机实测两张表的**入向**跨包外键都是空集
    （`confrelid IN (plan_drafts, runtime_settings)` 无行），
    因此 TRUNCATE 它们不会波及别的包 —— 若加了 CASCADE 反而是隐患
    （一旦将来有入向 FK，CASCADE 会**静默清空引用方表**，正是 ISS-106
    实测过的破坏形态）。这里**刻意不写 CASCADE**，让"将来多了入向 FK"
    变成一条**当场报错**而不是静默清别人数据。

    **为什么要清（不只是洁癖）**：`change_global_setting` 会**枚举全部项目**
    做级联刷新（§3.6.2 步骤 ②）。测试库里 `projects` 是累积的（M1/M2 各包
    测试留下的解析性残留，本包无权清理 —— 它是别的包的表），
    于是**每次全局变更都会给库里每个项目各插一行**。不清的话：
      · 库随跑测次数**线性膨胀**（本机实测单跑一条用例即写入 188 行）；
      · `list_diverged_projects` / J2 穷举这类**扫全库**的判据会被
        兄弟用例的残留污染（实测：J3 留下的 `MANUAL` 项目让
        "全跟随 ⇒ 清单为空"这条失败）。
    """
    reset_engines()
    settings = _make_settings()
    engine = get_engine(settings)

    # 夹具前后核对「跨包入向外键」集合必须不变（ISS-106 的判据）。
    fks_before = cross_package_foreign_keys(engine, DROP_ORDER)

    # 只删「没有跨包入向外键」的表；被别的包引用的表必须保留。
    # 本包当前无入向跨包外键，因此两张表都可删 —— 但流程统一，
    # 否则下一个包把 FK 指向本包表时，这里会静默摘掉它。
    dropped, kept = drop_own_tables(engine, DROP_ORDER)

    PlanDraft.metadata.create_all(engine, tables=list(OWN_TABLES))

    fks_after = cross_package_foreign_keys(engine, DROP_ORDER)
    assert fks_after == fks_before, (
        f"建表夹具改变了跨包外键集合：before={sorted(fks_before)} "
        f"after={sorted(fks_after)}（dropped={dropped} kept={kept}）"
    )

    with engine.begin() as conn:
        # 清空两张表（见 docstring「★ 建表后追加 TRUNCATE」）。
        # **顺序在触发器之前**：此刻还没来得及挂不可变触发器，
        # 但即便挂着也没关系 —— TRUNCATE 不触发行级触发器。
        # 紧接着下面的触发器会 DROP → CREATE，因此这里清完就是干净的起点。
        #
        # 实测断言（写死在 SQL 里，不靠注释）：两条入向跨包 FK 都必须是空集。
        # 若哪天有包把 FK 指向本包表，这里会**当场报错**而不是静默清别人数据。
        inbound = conn.execute(
            text(
                "select count(*) from pg_constraint"
                " where contype = 'f' and confrelid in"
                " ('plan_drafts'::regclass, 'runtime_settings'::regclass)"
            )
        ).scalar_one()
        assert inbound == 0, (
            f"本包两张表新增了 {inbound} 条入向外键引用 —— 此时 TRUNCATE"
            " 会有清理别人数据的风险，必须改为逐表判断（不要加 CASCADE 蒙过去）"
        )
        conn.execute(text("TRUNCATE plan_drafts, runtime_settings"))

        for (
            _table,
            func_ddl,
            trigger_ddl,
            drop_trigger,
            _drop_func,
        ) in pl_ddl.all_trigger_ddl():
            conn.execute(text(drop_trigger))  # 幂等关键
            conn.execute(text(func_ddl))
            conn.execute(text(trigger_ddl))
    yield


# ===========================================================================
# 数据夹具（自包含，不依赖 conftest 的隐式状态）
# ===========================================================================


@dataclass
class PlanningEnv:
    settings: Settings
    project_id: uuid.UUID
    brand_id: uuid.UUID
    product_id: uuid.UUID
    owner_id: uuid.UUID
    admin_id: uuid.UUID
    #: 另一套品牌/产品/项目，用于"跨项目"反例（基线归属、分叉清单）
    other_brand_id: uuid.UUID
    other_product_id: uuid.UUID
    other_project_id: uuid.UUID
    #: **不是任何项目成员**的运营 —— 用于验证 404（防项目存在性枚举）
    outsider_id: uuid.UUID


def _create_user(
    settings: Settings, *, role: str = ROLE_OPERATOR, name: str = "Planning 测试用户"
) -> uuid.UUID:
    with session_scope(settings) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name=name,
            role=role,
        )
        return user.id


def _seed_project(
    settings: Settings, *, owner_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """造 Brand → Product → Project，返回 `(brand_id, product_id, project_id)`。"""
    with session_scope(settings) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name=f"产品-{uuid.uuid4().hex[:6]}")
        session.add(product)
        session.flush()
        project = Project(
            project_code=f"PLN-{uuid.uuid4().hex[:8].upper()}",
            name="计划测试项目",
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
        return brand.id, product.id, project.id


@pytest.fixture(autouse=True)
def _clean_setting_rows() -> Iterator[None]:
    """**每个用例前**清空本包两张表。autouse ⇒ 先于 `env` 执行。

    ## 为什么 module 级那一次 TRUNCATE 不够（本该如此但实测不够）

    本文件里有三条判据**扫全库**，而它们正是 J2 的全部价值所在 ——
    "穷举全部项目，不抽样"（分叉的杀伤路径就是"某个项目被漏掉，
    而没人去点开它"）。但**扫全库**在同一个测试库里会看到**别的用例
    留下的行**，于是判据被污染：

    | 判据 | 被污染后的症状（本机实测） |
    |---|---|
    | J2 穷举 | 报 `项目 … 在全局变更后来源变成了 'MANUAL'` —— 那是 J3/J4 用例留下的真覆盖 |
    | 分叉清单为空 | 报清单非空，里面全是**上一轮用例**的旧全局值副本（`revision_no=6`） |

    这两类症状与"J2 真的失败了"**在输出上完全同形** —— 排障者会去翻
    `change_global_setting` 的级联循环，而那里的代码是对的。

    因此不能靠"收窄断言范围"绕开（那会**丢掉穷举性**，把本文件最要紧的
    一条判据降级成抽样）。正确处置是**让起点干净**：每个用例开始时
    表里没有别人的行，于是"扫全库"扫到的就是本次用例的真实状态。

    ## 清完为什么 J2 仍然成立

    `projects` 表里有 M1/M2 各包测试累积的存量项目（本包无权清理 ——
    那是别的包的表）。清理后它们**没有**项目级行，于是
    `change_global_setting` 的**路径 c**（"尚无 PROJECT 行"视同 INHERITED）
    会给每一个都补上新全局值。于是穷举断言看到的全部是
    `INHERITED` + 等于新全局值 —— **正是 J2 要断言的内容**，
    而不是被旧值污染的假象。

    ## 为什么用 TRUNCATE 而不是逐行 DELETE

    `runtime_settings` 是不可变表（行级触发器拦 UPDATE/DELETE，`23001`）。
    只有 TRUNCATE 能绕过行级触发器（见 `_ensure_own_tables` 的说明）。
    两张表都无入向跨包外键（夹具里已断言），因此不写 `CASCADE`。
    """
    reset_engines()
    engine = get_engine(_make_settings())
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE plan_drafts, runtime_settings"))
    yield


@pytest.fixture
def env() -> PlanningEnv:
    reset_engines()
    settings = _make_settings()
    owner = _create_user(settings)
    admin = _create_user(settings, role=ROLE_ADMIN, name="Planning 测试管理员")
    outsider = _create_user(settings, name="Planning 非成员")
    brand_id, product_id, project_id = _seed_project(settings, owner_id=owner)
    other_brand, other_product, other_project = _seed_project(
        settings, owner_id=owner
    )
    return PlanningEnv(
        settings=settings,
        project_id=project_id,
        brand_id=brand_id,
        product_id=product_id,
        owner_id=owner,
        admin_id=admin,
        other_brand_id=other_brand,
        other_product_id=other_product,
        other_project_id=other_project,
        outsider_id=outsider,
    )


# --- 2.0 基础设施 ----------------------------------------------------------


def _raw(env: PlanningEnv, sql: str, params: dict[str, Any] | None = None) -> Any:
    """在**独立事务**里执行一条原生 SQL 并提交。返回结果行（若有）。"""
    with session_scope(env.settings) as session:
        result = session.execute(text(sql), params or {})
        return result.fetchall() if result.returns_rows else None


def _scalar(env: PlanningEnv, sql: str, params: dict[str, Any] | None = None) -> Any:
    with session_scope(env.settings) as session:
        return session.execute(text(sql), params or {}).scalar_one_or_none()


def _raw_expect_error(
    env: PlanningEnv, sql: str, params: dict[str, Any] | None = None
) -> DBAPIError:
    """执行一条**应当失败**的原生 SQL，返回被捕获的异常供断言 sqlstate。"""
    with pytest.raises(DBAPIError) as caught:
        with session_scope(env.settings) as session:
            session.execute(text(sql), params or {})
    return caught.value


def _sqlstate(exc: DBAPIError) -> str:
    return getattr(getattr(exc, "orig", None), "sqlstate", "") or ""


def _primary(exc: DBAPIError) -> str:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "message_primary", "") or ""


@contextmanager
def _repo(env: PlanningEnv) -> Iterator[PlanningRepository]:
    """一次事务里的 `PlanningRepository`（退出即提交）。"""
    with session_scope(env.settings) as session:
        yield PlanningRepository(session)


def _next_revision(env: PlanningEnv) -> int:
    """取一个**严格大于现有全部修订号**的值。

    **不能写死数字**：`runtime_settings` 是不可变表，第 2/3 层的写入
    会跨运行累积，写死会在第二次运行时撞上自己的残留 ——
    而失败信息看起来像"实现缺陷"（实际是判据写错了）。
    """
    current = _scalar(env, "SELECT coalesce(max(revision_no), 0) FROM runtime_settings")
    return int(current or 0) + 1000


def _measure_statements(env: PlanningEnv, action: Any) -> dict[str, int]:
    """在 `action()` 执行期间统计各类语句条数（返回时已摘掉监听器）。

    ## 为什么要数语句而不是计时（MAJOR-4 的判据）

    "改得快了"与"改对了"是两件事，而**只有前者能被计时观察** ——
    但计时受机器负载影响、在 CI 上会闪。本判据因此测**查询条数**：
    它是离散的、可精确断言的事实。

    批量刷新（`list_project_settings` 一次 `DISTINCT ON`）与逐项目刷新
    （每项目 `current_project_setting` + `max_revision_no`）的语句条数
    相差一个数量级 —— 实测本项目测试库 3835 个项目时：
    **2 条 `SELECT`（与项目数无关） vs 7671 条（2 × 项目数）**。
    因此"`SELECT` 条数是否与项目数解耦"是一条能 FAIL 的判据。

    ## 为什么把 `SELECT` 与 `INSERT` 分开数

    级联刷新**必须**为每个项目插一行（少插一行就是漏刷，J2 会失败）。
    所以 `INSERT == 项目数` 是"没漏刷"的**正面证据**，不能拿它当性能问题。
    判据断言的是：`SELECT` 与项目数解耦、`INSERT` 与项目数相等。

    ## 为什么监听器挂 Engine 而不是 session

    `session_scope()` 每次新建 session 且自己拥有连接，在 session 上挂
    监听器会漏掉连接池里换进来的新连接。`before_cursor_execute` 在
    Engine 层级是**连接无关**的，因此覆盖 `action` 期间的每一条语句。
    """
    engine: Any = get_engine(env.settings)
    engine = engine.engine if hasattr(engine, "engine") else engine
    if not isinstance(engine, Engine):  # pragma: no cover - 防御性
        raise AssertionError(f"get_engine 返回的不是 Engine，而是 {engine!r}")

    counted: dict[str, int] = {"select_runtime": 0, "insert_runtime": 0}

    def _record(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        upper = statement.lstrip().upper()
        if "RUNTIME_SETTINGS" not in upper:
            return
        if upper.startswith("SELECT"):
            counted["select_runtime"] += 1
        elif upper.startswith("INSERT"):
            counted["insert_runtime"] += 1

    event.listen(engine, "before_cursor_execute", _record)
    try:
        action()
    finally:
        # 必须摘掉：`event.listen` 是全局的，留着会污染后续用例的计数
        # （下一个用例会"继承"这里的累加值 ⇒ 判据随执行顺序时红时绿）。
        event.remove(engine, "before_cursor_execute", _record)
    return counted


def _insert_baseline(env: PlanningEnv, *, project_id: uuid.UUID) -> uuid.UUID:
    """造一条**合法**的 `production_baselines`（本包草稿的 `baseline_id` 指向它）。

    本包只把基线当"一个存在且属于本项目的 id"用，但基线行本身受
    **WP22 的三条延迟约束触发器**管，少任何一件都会在 `COMMIT` 时才炸、
    且报错点落在本 helper 里（很容易被误读成 WP24 的实现缺陷）。
    已实测，三件缺一不可：

    | # | 要求 | 触发器 / 函数 | 违背时的报错 |
    |---|---|---|---|
    | 1 | `id` **必须显式给** | —— | `NotNullViolation: null value in column "id"` |
    | 2 | 同 `id` 的 `version_nodes` 行必须存在，且 `entity_type='BASELINE'`、`entity_id == project_id` | `rednote_check_baseline_version_consistency()` | 23514 |
    | 3 | 同事务内有一条未被取代的 `step_approvals`（`step_no=3` / `subject_kind='BASELINE'` / `subject_version_id=<id>` / `decision='APPROVED'`） | `rednote_check_baseline_approval()` | 23514 |

    **三条必须在同一个事务里**：触发器是 `DEFERRABLE INITIALLY DEFERRED`
    （`COMMIT` 时校验），拆成两个事务会让第一个提交的节点成为"孤立 BASELINE 节点"而被拒。
    """  # noqa: E501
    baseline_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO version_nodes"
                " (id, entity_type, entity_id, revision_no, payload_hash,"
                "  schema_version, project_id)"
                " VALUES (:bid, 'BASELINE', :pid, 1, :hash, 1, :pid)"
            ),
            {"bid": str(baseline_id), "pid": str(project_id), "hash": "0" * 64},
        )
        session.execute(
            text(
                "INSERT INTO production_baselines"
                " (id, project_id, ruleset_version, snapshot, change_reason)"
                " VALUES (:bid, :pid, :rs, '{\"active\": true}'::jsonb, :reason)"
            ),
            {
                "bid": str(baseline_id),
                "pid": str(project_id),
                "rs": f"rs-{uuid.uuid4().hex[:8]}",
                "reason": "WP24 测试",
            },
        )
        session.execute(
            text(
                "INSERT INTO step_approvals"
                " (project_id, step_no, subject_kind, subject_version_id,"
                "  decision, reason, decided_by)"
                " VALUES (:pid, 3, 'BASELINE', :bid, 'APPROVED', :reason, :who)"
            ),
            {
                "pid": str(project_id),
                "bid": str(baseline_id),
                "reason": "WP24 测试（造基线用的批准）",
                "who": str(env.owner_id),
            },
        )
    return baseline_id


def _draft_state(env: PlanningEnv, *, project_id: uuid.UUID) -> tuple[int, int]:
    """`(lock_version, 行数)` —— 用于差分断言（草稿表可变，行数可直接数）。"""
    rows = _raw(
        env,
        "SELECT lock_version FROM plan_drafts WHERE project_id = :pid",
        {"pid": str(project_id)},
    )
    count = _scalar(
        env, "SELECT count(*) FROM plan_drafts WHERE project_id = :pid",
        {"pid": str(project_id)},
    )
    return (0 if not rows else int(rows[0][0])), int(count or 0)


def _make_items(*specs: tuple[str, int]) -> list[AllocationItem]:
    """`[("a", 300), ("b", 700)]` → `AllocationItem` 列表（sortOrder 按下标）。"""
    return [
        AllocationItem(key=key, weight_permille=weight, sort_order=index)
        for index, (key, weight) in enumerate(specs)
    ]


def _item_body(key: str, weight: int, *, sort_order: int = 0, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "key": key,
        "name": key,
        "weightPermille": weight,
        "sortOrder": sort_order,
    }
    body.update(extra)
    return body


# ===========================================================================
# 第 1 层：纯函数（不碰库）
# ===========================================================================


# --- 1.1 千分位 ------------------------------------------------------------


@pytest.mark.parametrize("value", [MIN_PERMILLE, 1, 500, MAX_PERMILLE])
def test_permille_accepts_in_range_integers(value: int) -> None:
    pl_permille.validate_permille_value(value)  # 不抛即通过


@pytest.mark.parametrize("value", [MIN_PERMILLE - 1, MAX_PERMILLE + 1, -1, 1001])
def test_permille_rejects_out_of_range(value: int) -> None:
    """越界报 `ValueError`，且文案里**带上取值域**（否则"1001 被拒了"要靠猜）。"""
    with pytest.raises(ValueError, match="比例超出取值域"):
        pl_permille.validate_permille_value(value)


@pytest.mark.parametrize("value", [True, False])
def test_permille_rejects_bool_explicitly(value: bool) -> None:
    """**`bool` 是 `int` 的子类** —— 不显式排除的话 `True` 会被当成 1‰。

    这与 `_validate_batch_limits` / `_require_total_quantity` 是同一条处置：
    凡是"要整数"的地方都要显式排 bool，否则"手滑传了个布尔"会被静默接受。
    """
    with pytest.raises(ValueError, match="整数"):
        pl_permille.validate_permille_value(value)


@pytest.mark.parametrize("value", ["300", 300.0, None])
def test_permille_rejects_non_int(value: Any) -> None:
    with pytest.raises(ValueError, match="整数"):
        pl_permille.validate_permille_value(value)


def test_validate_total_reports_sum_and_deviation_without_raising() -> None:
    """`validate_total` **不抛异常** —— 它返回 `(合计, 偏差)`。

    这是刻意的：调用方有两类（"保存时允许不等于 1000"与
    "提交时必须等于 1000"），让这个函数抛异常会把第一类逼成 try/except。
    """
    items = _make_items(("a", 300), ("b", 300), ("c", 300))
    assert pl_permille.validate_total(items) == (900, -100)
    assert pl_permille.validate_total(_make_items(("a", 1000))) == (1000, 0)


def test_normalize_is_identity_when_total_is_already_1000() -> None:
    """**已经等于 1000 时返回原值，不重算。**

    重算会因取整把 `[333,333,334]` 变成别的组合，而
    "保存时不改变运营看到的值"是一条该被保住的性质
    （`normalize_permille` 的 docstring 明写）。
    """
    items = _make_items(("a", 333), ("b", 333), ("c", 334))
    normalized = pl_permille.normalize_permille(items)
    assert [(item.key, item.weight_permille) for item in normalized] == [
        ("a", 333),
        ("b", 333),
        ("c", 334),
    ]
    # 是同一个**对象**列表（不是"值相等的新列表"）—— 证明真的走了短路分支。
    assert normalized == list(items)


def test_normalize_scales_to_1000() -> None:
    normalized = pl_permille.normalize_permille(_make_items(("a", 1), ("b", 1), ("c", 1)))
    assert pl_permille.validate_total(normalized) == (1000, 0)
    assert [(item.key, item.weight_permille) for item in normalized] == [
        ("a", 334),
        ("b", 333),
        ("c", 333),
    ]


def test_normalize_returns_original_when_total_is_zero() -> None:
    """全 0 权重（全部停用）是合法输入 —— 原样返回，不去除零。"""
    items = _make_items(("a", 0), ("b", 0))
    assert pl_permille.normalize_permille(items) == list(items)
    assert pl_permille.normalize_permille([]) == []


def test_normalize_tie_break_prefers_smaller_sort_order() -> None:
    """平局次序的第二级：`sortOrder` 小的优先。"""
    # 三条余数完全相同（1*1000 % 3 都为 1），因此只能靠 sortOrder 定序。
    items = [
        AllocationItem(key="z", weight_permille=1, sort_order=2),
        AllocationItem(key="y", weight_permille=1, sort_order=1),
        AllocationItem(key="x", weight_permille=1, sort_order=0),
    ]
    normalized = pl_permille.normalize_permille(items)
    got = {item.key: item.weight_permille for item in normalized}
    assert got["x"] == 334  # sortOrder 最小 → 先拿那 1‰
    assert got["y"] == 333
    assert got["z"] == 333


def test_normalize_tie_break_falls_back_to_key_order() -> None:
    """第三级：`sortOrder` 也相同时按 `key` 字典序。**这条才是"稳定"的来源**。"""
    items = [
        AllocationItem(key="c", weight_permille=1, sort_order=0),
        AllocationItem(key="a", weight_permille=1, sort_order=0),
        AllocationItem(key="b", weight_permille=1, sort_order=0),
    ]
    got = {item.key: item.weight_permille for item in pl_permille.normalize_permille(items)}
    assert got["a"] == 334
    assert got["b"] == 333
    assert got["c"] == 333


def test_normalize_keeps_disabled_items_in_place_and_unnormalized() -> None:
    """`weight <= 0` 的条目**原样带回**（不被归一、不被丢掉）。

    丢掉会让"这张表上原本有哪几行"这个事实消失（前端会看到行数变少）。
    """
    items = [
        AllocationItem(key="a", weight_permille=500, sort_order=0),
        AllocationItem(key="disabled", weight_permille=0, sort_order=1),
        AllocationItem(key="b", weight_permille=500, sort_order=2),
    ]
    normalized = pl_permille.normalize_permille(items)
    assert [item.key for item in normalized] == ["a", "disabled", "b"]
    assert normalized[1].weight_permille == 0


def test_apply_draft_permille_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="不存在"):
        pl_permille.apply_draft_permille(_make_items(("a", 1000)), {"nope": 500})


def test_apply_draft_permille_applies_overrides() -> None:
    applied = pl_permille.apply_draft_permille(
        _make_items(("a", 1000), ("b", 0)), {"a": 400, "b": 600}
    )
    assert [(item.key, item.weight_permille) for item in applied] == [("a", 400), ("b", 600)]


def test_renormalize_after_disable_is_alias_of_normalize() -> None:
    items = _make_items(("a", 600), ("b", 0), ("c", 300))
    assert pl_permille.renormalize_after_disable(items) == pl_permille.normalize_permille(items)


# --- 1.2 最大余数法与容量削峰 ---------------------------------------------


def test_remainder_uses_total_times_weight_not_weight_alone() -> None:
    """★ **本包实测踩过的坑**：余数曾写成 `weight % 1000`。

    因为权重都 < 1000，那个式子**恒等于权重本身** —— 于是"按余数降序"
    静默退化成"按权重大小降序"。两者在多数输入下给相同结果，
    只在"大权重条目余数为 0、小权重条目余数很大"时分叉。
    """
    items = _make_items(("a", 300), ("b", 250))
    assert pa._remainder_of(items[0], 10) == 0  # 10*300 % 1000
    assert pa._remainder_of(items[1], 10) == 500  # 10*250 % 1000
    # 错误口径会给出 300 / 250（= 权重本身）
    assert pa._remainder_of(items[0], 10) != 300


@pytest.mark.regression
def test_largest_remainder_regression_300_250_200_150_60() -> None:
    """★ 回归：`[300,250,200,150,60]` × total=10 ⇒ `[3,3,2,1,1]`。

    错误口径（余数 = 权重）在此样本上给出 `[4,3,2,1,0]` ——
    **两者合计都是 10**，因此"和等于总数"这条不变量**检测不出**这个缺陷。
    这正是需要一条"逐条点名"的回归用例的原因。
    """
    items = _make_items(("a", 300), ("b", 250), ("c", 200), ("d", 150), ("e", 60))
    result = pa.allocate_by_weight(items, 10)
    assert result.allocated_by_key() == {"a": 3, "b": 3, "c": 2, "d": 1, "e": 1}
    assert result.allocated_total == 10
    assert result.unallocated == 0
    # 逐条点名（顺序与输入一致 —— 这是前端表格的行序）
    assert [line.allocated for line in result.lines] == [3, 3, 2, 1, 1]

    # 反向验证：把错误口径放回去必须给出**不同**结果。
    wrong = sorted(
        items,
        key=lambda item: (-(item.weight_permille % PERMILLE_TOTAL), item.sort_order, item.key),
    )
    wrong_allocated = {item.key: item.weight_permille * 10 // PERMILLE_TOTAL for item in items}
    leftover = 10 - sum(wrong_allocated.values())
    for item in wrong:
        if leftover == 0:
            break
        wrong_allocated[item.key] += 1
        leftover -= 1
    assert wrong_allocated == {"a": 4, "b": 3, "c": 2, "d": 1, "e": 0}
    assert sum(wrong_allocated.values()) == 10  # 合计相同 ⇒ 不变量检测不出
    assert wrong_allocated != result.allocated_by_key()


@pytest.mark.regression
def test_allocation_invariant_holds_exhaustively() -> None:
    """★ 穷举：`total` 1..120 × 项数 2..8 ⇒ **逐条**核对分配额。

    ## ⚠️ 这条用例为什么要重写（错误 ⑪：原判据是恒真式）

    原版断言 `result.allocated_total + result.unallocated == total`。
    但 `allocated_total` 是 `AllocationResult` 的 `@property`
    （`sum(line.allocated for line in lines)`），而 `unallocated`
    在生产侧就是 `total - sum(同一个 dict)` ——
    两者相加**在代数上恒等**，400 组离谱输入实测一次都没破。
    它从未抓到过任何缺陷（而同期"负 `unallocated`"就躺在那里）。

    **能 FAIL 的判据只能是"两条独立数据源互证"**：测试侧按
    C19 规则**另外实现一遍**期望分配额，与返回值逐条比对。
    实现里任何"分错人 / 少分 / 多分"都会被这个版本抓到，
    而且它是**真的会失败**的（见 `test_allocation_checker_detects_wrong_impl`）。

    ## 覆盖面

    · **无容量路径**（C19 最大余数法）：抽查权重组合含 0（停用项）。
    · **有容量路径**（C14 容量削峰）：`capacity` 按 `total` 的分数给定，
      覆盖 MAJOR-1（全零权重）与 MAJOR-5（停用项白拿）曾藏身的区域。
    · **权重合计 > 1000‰ 的输入**：必须抛 `ValueError`（错误 2 的防线），
      穷举里显式断言"要么合法、要么明确拒绝"，不允许静默算出错值。
    """
    weights = (0, 1, 7, 60, 150, 200, 250, 300, 333, 334, 500, 999, 1000)
    checked_ok = 0
    checked_rejected = 0
    checked_capacity = 0

    for count in range(2, 9):
        for total in range(1, 121):
            for offset in range(0, len(weights), count):
                picked = [
                    weights[(offset + index) % len(weights)] for index in range(count)
                ]

                # ---- 路径 A：无容量（最大余数法）----
                plain = [
                    AllocationItem(
                        key=f"k{index}", weight_permille=weight, sort_order=index
                    )
                    for index, weight in enumerate(picked)
                ]
                if sum(picked) > PERMILLE_TOTAL:
                    # 合计越界：必须被拒绝，而不是静默超发（错误 2）。
                    with pytest.raises(ValueError, match="超过"):
                        pa.allocate_by_weight(plain, total)
                    checked_rejected += 1
                else:
                    result = pa.allocate_by_weight(plain, total)
                    expected = _expected_largest_remainder(picked, total)
                    assert result.allocated_by_key() == expected, (
                        f"最大余数法与独立实现不一致：weights={picked} total={total} "
                        f"⇒ 实现 {result.allocated_by_key()} ≠ 期望 {expected}"
                    )
                    # 真正能 FAIL 的两条边界（原恒真式抓不到）：
                    assert result.unallocated >= 0, (
                        f"unallocated 为负：weights={picked} total={total} "
                        f"⇒ {result.unallocated}"
                    )
                    assert result.allocated_total <= total
                    # 停用项（权重 0）一条都不该拿（"停用 = 不参与"）。
                    for line in result.lines:
                        if line.weight_permille == 0:
                            assert line.allocated == 0, (
                                f"停用项拿到了分配额：{line.key} weight=0 "
                                f"⇒ allocated={line.allocated}"
                            )
                    checked_ok += 1

                # ---- 路径 B：有容量（容量削峰，C14）----
                # 容量与权重**解耦**：统一给 `total // 4`（至少 1）。
                # ⚠️ 这里踩过一次：初版写成 `(total*w//1000)//2`，
                # 于是 `w=0` 的条目 capacity 也等于 0 ⇒ **没有 room** ⇒
                # MAJOR-5（停用项白拿）反而不在穷举覆盖面里，
                # 反向验证因此**不报红**（判据没有区分力）。
                # 与权重解耦后，零权重条目始终有 room —— 那正是缺陷的触发条件。
                capped = [
                    AllocationItem(
                        key=f"k{index}",
                        weight_permille=weight,
                        sort_order=index,
                        capacity=max(1, total // 4),
                    )
                    for index, weight in enumerate(picked)
                ]
                if sum(picked) > PERMILLE_TOTAL:
                    with pytest.raises(ValueError, match="超过"):
                        pa.allocate_by_weight(capped, total)
                else:
                    result = pa.allocate_by_weight(capped, total)
                    got = result.allocated_by_key()
                    assert result.unallocated >= 0, (
                        f"容量路径 unallocated 为负：weights={picked} total={total} "
                        f"⇒ {result.unallocated}"
                    )
                    assert sum(got.values()) + result.unallocated == total
                    for line in result.lines:
                        if line.capacity is not None:
                            assert line.allocated <= line.capacity
                        if line.weight_permille == 0:
                            assert line.allocated == 0, (
                                f"容量路径下停用项拿到了分配额：{line.key} "
                                f"⇒ allocated={line.allocated}"
                            )
                    checked_capacity += 1

    assert checked_ok >= 1000, f"无容量路径只跑了 {checked_ok} 组"
    assert checked_capacity >= 1000, f"容量路径只跑了 {checked_capacity} 组"
    assert checked_rejected > 0, "没有构造出「权重合计越界」的输入 —— 防线没被覆盖"


def _expected_largest_remainder(weights: list[int], total: int) -> dict[str, int]:
    """C19 最大余数法的**独立实现**（测试侧，故意不复用生产代码）。

    这么写是本节判据的全部意义所在：若期望值也调 `pa` 里的函数，
    比对就成了"实现与自己做对比"，永远相等。

    规则（与 `allocation.py` docstring 第 3 节一致）：
      1. `floor = total × w // 1000`；
      2. 余数 = `total × w % 1000`（**是 total 的函数**）；
      3. 未分完的按「余数降序 → sort_order 升序 → key 字典序」逐条 +1；
      4. **权重 0 的条目不参与补 1**。
    """
    keys = [f"k{index}" for index in range(len(weights))]
    floors = [total * w // PERMILLE_TOTAL for w in weights]
    remainders = [total * w % PERMILLE_TOTAL for w in weights]

    allocated = {key: floors[index] for index, key in enumerate(keys)}
    leftover = total - sum(floors)
    order = sorted(
        range(len(weights)),
        key=lambda index: (-remainders[index], index, keys[index]),
    )
    for index in order:
        if leftover == 0:
            break
        if weights[index] == 0:
            continue  # 停用项不参与补 1
        allocated[keys[index]] += 1
        leftover -= 1
    return allocated


def test_allocation_checker_detects_wrong_impl() -> None:
    """★ 反向验证：把**错误实现**放回去，上面那条独立实现必须报红。

    这是"判据必须能区分两种实现"（项目铁律 #7）的机械落点。
    只加正例不算完成 —— 若本用例失败，说明 `_expected_largest_remainder`
    退化成恒真式（例如写成直接调 `pa.allocate_by_weight`）。
    """
    weights = [300, 250, 200, 150, 60, 40]
    total = 10
    correct = _expected_largest_remainder(weights, total)
    assert correct == {"k0": 3, "k1": 3, "k2": 2, "k3": 1, "k4": 1, "k5": 0}

    # 错误实现：余数用 `w % 1000` 而不是 `total*w % 1000`（本包踩过的坑）。
    def buggy(ws: list[int], t: int) -> dict[str, int]:
        keys = [f"k{i}" for i in range(len(ws))]
        floors = [t * w // PERMILLE_TOTAL for w in ws]
        rems = [w % PERMILLE_TOTAL for w in ws]
        alloc = {k: floors[i] for i, k in enumerate(keys)}
        left = t - sum(floors)
        for i in sorted(range(len(ws)), key=lambda i: (-rems[i], i, keys[i])):
            if left == 0:
                break
            if ws[i] == 0:
                continue
            alloc[keys[i]] += 1
            left -= 1
        return alloc

    wrong = buggy(weights, total)
    assert sum(wrong.values()) == total  # 合计相同 ⇒ 恒真式检测不出
    assert wrong != correct, "反向验证失败：错误实现与正确实现给出同一结果"


def test_allocation_zero_total_returns_zero_everywhere() -> None:
    """`total=0` 不报错（"只配置不生产"的预演是合法操作）。"""
    result = pa.allocate_by_weight(_make_items(("a", 500), ("b", 500)), 0)
    assert result.allocated_by_key() == {"a": 0, "b": 0}
    assert result.unallocated == 0


def test_allocation_with_no_items_puts_everything_in_unallocated() -> None:
    result = pa.allocate_by_weight([], 7)
    assert result.lines == ()
    assert result.unallocated == 7
    assert result.allocated_total == 0


@pytest.mark.parametrize(
    ("items", "pattern"),
    [
        (_make_items(("a", 500), ("a", 500)), "key 重复"),
        ([AllocationItem(key="a", weight_permille=-1)], "权重不能为负"),
        ([AllocationItem(key="a", weight_permille=1000, capacity=-1)], "容量不能为负"),
    ],
)
def test_allocation_rejects_invalid_input(items: list[AllocationItem], pattern: str) -> None:
    with pytest.raises(ValueError, match=pattern):
        pa.allocate_by_weight(items, 10)


def test_allocation_rejects_negative_total() -> None:
    with pytest.raises(ValueError, match="不能为负"):
        pa.allocate_by_weight(_make_items(("a", 1000)), -1)


def test_capacity_shave_moves_overflow_to_item_with_room() -> None:
    """容量削峰：A 只放得下 1 条，剩下 9 条转到 B（B 无上限）。

    实测样本（本机确认）：`a: cap=1, w=500` / `b: w=500` × total=10
    ⇒ `{a: 1, b: 9}`，`rounds=2`，`shaved=4`。
    """
    items = [
        AllocationItem(key="a", weight_permille=500, sort_order=0, capacity=1),
        AllocationItem(key="b", weight_permille=500, sort_order=1),
    ]
    result = pa.allocate_by_weight(items, 10)
    assert result.allocated_by_key() == {"a": 1, "b": 9}
    assert result.unallocated == 0
    assert result.rounds == 2
    assert result.notes == ("容量削峰 2 轮", "累计削峰 4 条")


def test_all_items_at_capacity_leaves_remainder_unallocated() -> None:
    """★ C14 原文：**全部满容时剩余篇数计入 `unallocated`，不硬塞**。

    硬塞就是产出重复内容。宁可报"分不完"让人决定：补场景，还是降总量。
    """
    items = [
        AllocationItem(key="a", weight_permille=500, sort_order=0, capacity=1),
        AllocationItem(key="b", weight_permille=500, sort_order=1, capacity=1),
    ]
    result = pa.allocate_by_weight(items, 10)
    assert result.allocated_by_key() == {"a": 1, "b": 1}
    assert result.unallocated == 8
    # ⚠️ 这里原先写的是 `result.allocated_total + result.unallocated == 10`——
    # **那是一条恒真式**（错误 ⑪ 的残留）：`allocated_total` 是 `@property`
    # 对 `lines` 求和，而 `unallocated` 在生产侧就是 `total - sum(同一个 dict)`，
    # 两者相加在代数上恒等于 `total`，与输入无关，**永远不会报红**。
    # 错误 ⑪ 的收口只改了穷举用例（`test_allocation_invariant_holds_exhaustively`），
    # 漏掉了本用例 —— 由 `tools/m3/collect_wp24_code_review.py` 的 AST 判据发现。
    # 改成钉**具体期望值**（真断言，且与上面两条一致）。
    assert result.allocated_total == 2
    assert result.notes == ("容量削峰 2 轮", "累计削峰 8 条")


def test_capacity_shaved_flag_marks_only_items_that_hit_their_cap() -> None:
    items = [
        AllocationItem(key="a", weight_permille=500, sort_order=0, capacity=1),
        AllocationItem(key="b", weight_permille=500, sort_order=1),
    ]
    flags = {line.key: line.capacity_shaved for line in pa.allocate_by_weight(items, 10).lines}
    assert flags == {"a": True, "b": False}


def test_tie_rank_only_numbers_items_that_actually_got_a_bonus() -> None:
    """`tie_rank` 是**分配动作的顺序**，不是"所有人的余数排名"。

    给所有人排名会让"排第 3 但没拿到"看起来像异常，而它其实是
    "额度用尽"的正常结果（`allocate_by_weight` 的注释明写）。
    """
    items = _make_items(("a", 300), ("b", 250), ("c", 200), ("d", 150), ("e", 60))
    ranks = {line.key: line.tie_rank for line in pa.allocate_by_weight(items, 10).lines}
    # 拿到补 1 的：e（余数 600，第 1 位）、b（余数 500，第 2 位）
    assert ranks == {"a": 0, "b": 2, "c": 0, "d": 0, "e": 1}
    assert sorted(value for value in ranks.values() if value) == [1, 2]


def test_allocate_from_remaining_deducts_capacity_not_weight() -> None:
    """子配额从**剩余容量**里分（C19"列和恒等于总计划"）。

    A 容量 4、已用 3 ⇒ 剩 1；B 容量 4、已用 0 ⇒ 剩 4。
    本批 5 条 ⇒ `{a: 1, b: 4}`（实测）。
    """
    items = [
        AllocationItem(key="a", weight_permille=500, sort_order=0, capacity=4),
        AllocationItem(key="b", weight_permille=500, sort_order=1, capacity=4),
    ]
    result = pa.allocate_from_remaining(items, {"a": 3, "b": 0}, batch_total=5)
    assert result.allocated_by_key() == {"a": 1, "b": 4}
    assert result.unallocated == 0


def test_allocate_from_remaining_rejects_over_consumed_item() -> None:
    """已分配量超过容量 ⇒ 抛错（**累计配额已经越界**，不是静默归零）。"""
    items = [AllocationItem(key="a", weight_permille=1000, capacity=2, sort_order=0)]
    with pytest.raises(ValueError, match="已超过其容量"):
        pa.allocate_from_remaining(items, {"a": 3}, batch_total=1)


def test_allocate_from_remaining_ignores_uncapped_items() -> None:
    """`capacity is None` 的条目**原样保留**（无上限），不参与扣减。"""
    items = [AllocationItem(key="a", weight_permille=1000, sort_order=0)]
    result = pa.allocate_from_remaining(items, {"a": 999}, batch_total=3)
    assert result.allocated_by_key() == {"a": 3}


def test_capacity_remaining_keeps_none_and_clamps_at_zero() -> None:
    items = [
        AllocationItem(key="a", weight_permille=500, capacity=4, sort_order=0),
        AllocationItem(key="b", weight_permille=500, sort_order=1),
    ]
    assert pa.capacity_remaining(items, {"a": 3, "b": 0}) == {"a": 1, "b": None}
    assert pa.capacity_remaining(items, {"a": 9, "b": 0}) == {"a": 0, "b": None}


# --- 1.3 数量关口（ADR-13 的应用层校验） ---------------------------------


@pytest.mark.parametrize(
    ("kind", "limit"),
    [
        (BATCH_KIND_TRIAL, DEFAULT_BATCH_MAX[BATCH_KIND_TRIAL]),
        (BATCH_KIND_SCALE, DEFAULT_BATCH_MAX[BATCH_KIND_SCALE]),
        (BATCH_KIND_BULK, DEFAULT_BATCH_MAX[BATCH_KIND_BULK]),
    ],
)
def test_validate_quantity_accepts_both_boundaries(kind: str, limit: int) -> None:
    """四边界 × 三档：**下界**（`MIN_QUANTITY`）与**上界**（该档上限）都放行。"""
    limits = pl_limits.default_limits()
    pl_limits.validate_quantity(limits, kind, MIN_QUANTITY)
    pl_limits.validate_quantity(limits, kind, limit)
    assert pl_limits.is_within_limits(limits, kind, limit) is True


@pytest.mark.parametrize(
    ("kind", "limit"),
    [
        (BATCH_KIND_TRIAL, DEFAULT_BATCH_MAX[BATCH_KIND_TRIAL]),
        (BATCH_KIND_SCALE, DEFAULT_BATCH_MAX[BATCH_KIND_SCALE]),
        (BATCH_KIND_BULK, DEFAULT_BATCH_MAX[BATCH_KIND_BULK]),
    ],
)
def test_validate_quantity_rejects_just_outside_boundaries(kind: str, limit: int) -> None:
    """**下界 - 1** 与 **上界 + 1** 都必须拒 —— 只测"上界+1"会漏掉下界。"""
    limits = pl_limits.default_limits()
    with pytest.raises(pl_limits.QuantityOutOfRange) as below:
        pl_limits.validate_quantity(limits, kind, MIN_QUANTITY - 1)
    assert below.value.reason == REASON_QUANTITY_BELOW_MIN

    with pytest.raises(pl_limits.QuantityOutOfRange) as above:
        pl_limits.validate_quantity(limits, kind, limit + 1)
    assert above.value.reason == REASON_QUANTITY_ABOVE_MAX
    assert above.value.limit == limit
    assert above.value.batch_kind == kind


def test_validate_quantity_rejects_unknown_batch_kind() -> None:
    with pytest.raises(pl_limits.QuantityOutOfRange) as caught:
        pl_limits.validate_quantity(pl_limits.default_limits(), "NOPE", 1)
    assert caught.value.reason == REASON_BATCH_KIND_UNKNOWN


def test_validate_quantity_is_clamped_by_max_quantity_bound() -> None:
    """配置里把上限写成天文数字时，**结构性上界仍然生效**。

    `MAX_QUANTITY_BOUND = 100000` 是"绕过应用层也拦得住"的那一层
    （与 `plan_drafts.total_quantity_within_bound` 同一手法）。
    """
    huge = BatchLimits(trial=MAX_QUANTITY_BOUND * 10, scale=1, bulk=1)
    with pytest.raises(pl_limits.QuantityOutOfRange) as caught:
        pl_limits.validate_quantity(huge, BATCH_KIND_TRIAL, MAX_QUANTITY_BOUND + 1)
    assert caught.value.limit == MAX_QUANTITY_BOUND
    # 恰好等于结构性上界仍放行（边界的另一侧）
    pl_limits.validate_quantity(huge, BATCH_KIND_TRIAL, MAX_QUANTITY_BOUND)


def test_is_within_limits_returns_false_instead_of_raising() -> None:
    limits = pl_limits.default_limits()
    assert pl_limits.is_within_limits(limits, BATCH_KIND_TRIAL, 0) is False
    assert pl_limits.is_within_limits(limits, "NOPE", 1) is False
    assert pl_limits.is_within_limits(limits, BATCH_KIND_TRIAL, 5) is True


def test_default_limits_match_adr13_defaults() -> None:
    """默认值的**单一来源**是 `DEFAULT_BATCH_MAX`（迁移的数据回填调它）。"""
    assert pl_limits.default_limits().as_dict() == dict(DEFAULT_BATCH_MAX)
    assert DEFAULT_BATCH_MAX == {
        BATCH_KIND_TRIAL: 10,
        BATCH_KIND_SCALE: 30,
        BATCH_KIND_BULK: 50,
    }


def test_quantity_out_of_range_is_not_an_api_error() -> None:
    """`QuantityOutOfRange` **不继承 `ApiError`** —— 它不是 HTTP 层的错误类型。

    它在 WP31/WP32 的调用点被翻成业务错误；若它继承 `ApiError`，
    "服务层直接抛出可翻译成响应的异常"就会让错误形状在两层之间分叉。
    """
    assert not issubclass(pl_limits.QuantityOutOfRange, ApiError)
    assert issubclass(pl_limits.QuantityOutOfRange, ValueError)


def test_describe_limits_marks_divergence_with_global_value() -> None:
    """分叉时的文案必须**同时给出当前值与全局值**（§3.6.4 的原文形态）。"""
    setting = EffectiveSetting(
        key=SETTING_KEY_BATCH_LIMITS,
        configured=True,
        value={BATCH_KIND_TRIAL: 5, BATCH_KIND_SCALE: 30, BATCH_KIND_BULK: 50},
        source=SETTING_ORIGIN_MANUAL,
        global_value={BATCH_KIND_TRIAL: 10, BATCH_KIND_SCALE: 30, BATCH_KIND_BULK: 50},
        diverged=True,
        revision_no=7,
        project_id=uuid.uuid4(),
    )
    described = pl_limits.describe_limits(setting)
    assert described["diverged"] is True
    assert described["source"] == SETTING_ORIGIN_MANUAL
    assert "本项目已覆盖" in described["divergenceNote"]
    assert "全局" in described["divergenceNote"]


def test_describe_limits_handles_incomplete_global_shape() -> None:
    """全局值形状不全时**不撒谎**（不显示一个错的全局值）。"""
    setting = EffectiveSetting(
        key=SETTING_KEY_BATCH_LIMITS,
        configured=True,
        value={BATCH_KIND_TRIAL: 5, BATCH_KIND_SCALE: 30, BATCH_KIND_BULK: 50},
        source=SETTING_ORIGIN_MANUAL,
        global_value={BATCH_KIND_TRIAL: 10},  # 缺两档
        diverged=True,
        revision_no=8,
        project_id=uuid.uuid4(),
    )
    assert "形状不完整" in pl_limits.describe_limits(setting)["divergenceNote"]


# --- 1.4 DDL 文本自洽 -----------------------------------------------------


def test_index_spec_drift_is_empty() -> None:
    """规格（`PARTIAL_UNIQUE_INDEXES`）与 `models.py` 逐字段一致。

    逐字段核对：名字双向、表、键列**含顺序**、`WHERE` 规范化后相等。
    空列表 = 一致（`index_spec_drift` 的约定）。
    """
    assert pl_ddl.index_spec_drift() == []


def test_partial_unique_indexes_are_exactly_the_two_registered() -> None:
    """★ 两条部分唯一索引**按名字点名**。少一条 = "当前生效值"失去结构基础。

    为什么不能只断言"有唯一索引"：普通
    `UNIQUE(key, scope_kind, project_id, revision_no)` 对全局行**完全不生效**
    （`project_id IS NULL`，而 PG 认为 `NULL != NULL`），
    于是同一个 `(key, 'GLOBAL', NULL, 1)` 可以无限重复插入且**不报错**。
    """
    assert pl_ddl.partial_unique_index_names() == (
        "uq_runtime_settings_global",
        "uq_runtime_settings_project",
    )
    assert set(pl_ddl.partial_index_specs()) == set(pl_ddl.partial_unique_index_names())


def test_partial_unique_index_where_clauses_are_not_vacuous() -> None:
    """★ `WHERE` 必须**真的是作用域过滤**（不是永真也不是永假）。

    这是"索引活性"的静态必要条件：`WHERE` 写错会**静默失效**
    （不报错，只是不再约束）。动态证据在第 3 层
    （`test_partial_unique_index_where_actually_filters`）。
    """
    where = pl_ddl.partial_unique_index_where()
    assert where["uq_runtime_settings_global"] == "scope_kind = 'GLOBAL'"
    assert where["uq_runtime_settings_project"] == "scope_kind = 'PROJECT'"
    for name, clause in where.items():
        assert "scope_kind" in clause, f"{name} 的 WHERE 没有按 scope_kind 过滤"
        assert "1=1" not in clause.replace(" ", "")
        assert "true" not in clause.lower()


def test_partial_unique_index_columns_keep_the_null_free_key() -> None:
    """全局索引的键列里**没有 `project_id`** —— 那正是它躲开 `NULL != NULL` 的办法。"""
    columns = pl_ddl.partial_unique_index_columns()
    assert columns["uq_runtime_settings_global"] == ("key", "revision_no")
    assert columns["uq_runtime_settings_project"] == ("key", "project_id", "revision_no")
    assert "project_id" not in columns["uq_runtime_settings_global"]


def test_normalize_sql_strips_postgres_outer_parens_and_cast() -> None:
    """`_normalize_sql` 的存在理由：PG 回写的 `indexdef` 会加外层括号。

    实测 PG 回写形态：`WHERE ((scope_kind)::text = 'GLOBAL'::text)`。
    逐字比会**假失败**，而假失败的核对最终会被人关掉 ——
    那比没有核对更糟（ISS-95）。
    """
    assert pl_ddl._normalize_sql("WHERE ((scope_kind)::text = 'GLOBAL'::text)") == (
        "WHERE ((scope_kind)::text = 'GLOBAL'::text)"
    )
    # 剥括号能力本身：
    assert pl_ddl._normalize_sql("(scope_kind = 'GLOBAL')") == "scope_kind = 'GLOBAL'"
    # 多行/多空格被压平：
    assert pl_ddl._normalize_sql("a  =\n b") == "a = b"
    # `(a) AND (b)` 不被误剥（括号配平判断）
    assert pl_ddl._normalize_sql("(a) AND (b)") == "(a) AND (b)"


def test_trigger_names_is_exactly_one_immutable_trigger() -> None:
    """本包**只有**不可变触发器，没有一致性触发器（与 WP18/WP20/WP22/WP23 不同）。"""
    assert pl_ddl.immutable_trigger_names() == ("trg_runtime_settings_immutable",)
    assert pl_ddl.consistency_trigger_names() == ()
    assert pl_ddl.trigger_names() == ("trg_runtime_settings_immutable",)
    assert pl_ddl.immutable_function_names() == ("rednote_reject_mutation",)


def test_consistency_items_is_empty_by_design() -> None:
    """`consistency_items()` 返回 `[]` 是**显式形状**，不是漏写。

    它让"本包没有一致性触发器"成为一条可断言的**事实**；
    若把函数删掉，迁移里的循环会静默少跑一段而没有判据发现。
    """
    assert pl_ddl.consistency_items() == []
    assert len(pl_ddl.all_trigger_ddl()) == 1


def test_all_trigger_ddl_shape_and_drop_form() -> None:
    """每个触发器项是 5 元组，且 `drop_trigger` 必须是 `IF EXISTS` 形态。

    **`IF EXISTS` 是夹具幂等的前提**：`CREATE TRIGGER` 不是幂等的，
    夹具靠"先 DROP 再 CREATE"达到可重复，少了 `IF EXISTS`
    第一次在干净库上跑就会报错。
    """
    items = pl_ddl.all_trigger_ddl()
    assert len(items) == 1
    for table, func_ddl, trigger_ddl, drop_trigger, drop_func in items:
        assert table == "runtime_settings"
        assert "CREATE OR REPLACE FUNCTION" in func_ddl
        assert "CREATE TRIGGER" in trigger_ddl
        assert drop_trigger.startswith("DROP TRIGGER IF EXISTS")
        assert f"ON {table}" in drop_trigger
        assert drop_func.startswith("DROP FUNCTION IF EXISTS")


def test_immutable_trigger_raises_sqlstate_23001() -> None:
    """不可变触发的 SQLSTATE 是 `23001`（`IMMUTABLE_RECORD_SQLSTATE`）。

    DDL 里的字符串与平台常量必须一致 —— 两处各写一遍就会漂移，
    而漂移的后果是"测试断言 23001、实现报 23002"这种**看起来像环境问题**的失败。
    """
    _table, func_ddl, trigger_ddl, _drop, _dropf = pl_ddl.all_trigger_ddl()[0]
    assert f"'{IMMUTABLE_RECORD_SQLSTATE}'" in func_ddl
    assert "不可变记录" in func_ddl
    assert "BEFORE UPDATE OR DELETE" in trigger_ddl
    # 字面量 23001 也要在 DDL 里（防止有人只把常量改了、DDL 写死另一个值）
    assert f"ERRCODE = '{IMMUTABLE_RECORD_SQLSTATE}'" in func_ddl


def test_partial_unique_index_ddl_uses_the_registered_where() -> None:
    """建索引的 SQL 与 `partial_unique_index_where()` **同源**（不各写一遍）。"""
    ddl_by_name = dict(pl_ddl.partial_unique_index_ddl())
    where = pl_ddl.partial_unique_index_where()
    columns = pl_ddl.partial_unique_index_columns()
    for name, clause in where.items():
        statement = ddl_by_name[name]
        assert statement.startswith(f"CREATE UNIQUE INDEX {name} ON runtime_settings")
        assert "(" + ", ".join(columns[name]) + ")" in statement
        assert f"WHERE {clause}" in statement


# --- 1.5 值对象与解析 -----------------------------------------------------


def test_parse_batch_limits_rejects_missing_kind() -> None:
    """缺档**抛错而不是取默认**（静默补默认会让"少写一档"表现为"用 10 条"）。"""
    with pytest.raises(ValueError, match="形状不正确"):
        from rednote.modules.planning.types import parse_batch_limits

        parse_batch_limits({BATCH_KIND_TRIAL: 10, BATCH_KIND_SCALE: 30})


def test_batch_limits_max_for_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="未知的批次档位"):
        pl_limits.default_limits().max_for("NOPE")
    assert pl_limits.default_limits().max_for(BATCH_KIND_SCALE) == 30


def test_effective_setting_as_limits_raises_when_unconfigured() -> None:
    """未配置时**抛错而不是返回默认值** —— 否则"读不到配置"表现为"用了 10/30/50"。"""
    setting = EffectiveSetting(
        key=SETTING_KEY_BATCH_LIMITS,
        configured=False,
        value=None,
        source=None,
        global_value=None,
        diverged=False,
        revision_no=None,
        project_id=None,
    )
    with pytest.raises(ValueError, match="尚未配置"):
        setting.as_limits()


def test_setting_keys_and_placeholder_keys_are_consistent() -> None:
    assert SETTING_KEYS == {
        SETTING_KEY_BATCH_LIMITS,
        SETTING_KEY_RESEARCH_MAX_FILES,
        SETTING_KEY_PLATFORM_MEDIA_LIMITS,
    }
    # 占位键必须是合法键的子集（否则"允许清空"就落在一个不存在的键上）
    assert PLACEHOLDER_SETTING_KEYS <= SETTING_KEYS
    assert DEFAULT_RESEARCH_MAX_EFFECTIVE_FILES == 2000
    assert BATCH_KINDS == {BATCH_KIND_TRIAL, BATCH_KIND_SCALE, BATCH_KIND_BULK}


def test_item_state_constants_are_the_single_source() -> None:
    """`state` 的取值域有**唯一常量源**（MINOR-2 的判据）。

    ## 这条判据防的是什么

    `ITEM_STATE_LIST_SQL` 是给人和给 SQL 看的**同一份**取值域文本
    （`'DISABLED', 'ENABLED'`）。若 `service._validate_dimension` 又回去
    手写 `("ENABLED", "DISABLED")` 字面量，`state` 将来加第三个取值时
    会**漏改一处而不报错** —— 新增的取值会被 `_to_allocation_items`
    当作启用、权重照算（MINOR-2 描述的正是这个后果）。

    ## 三条断言各自钉住什么

    | 断言 | 钉住的事 |
    |---|---|
    | `ITEM_STATES` 恰好两个值 | 加了第三个取值就要**显式**改这里（而不是悄悄漂移） |
    | `ITEM_STATE_LIST_SQL` 由 `ITEM_STATES` 派生 | 展示文本不会与取值域集合脱钩 |
    | 校验错误文案里带 `ITEM_STATE_LIST_SQL` | 服务层用的是常量而不是字面量 |

    第三条是**行为层**的印证：它读 `ApiError.details["fieldErrors"][0]["message"]`，
    比"去 grep 源码有没有字面量"可靠 —— 后者会被注释里的引号骗过。
    """
    assert ITEM_STATES == {ITEM_STATE_ENABLED, ITEM_STATE_DISABLED}
    assert ITEM_STATE_LIST_SQL == "'DISABLED', 'ENABLED'"
    for name in sorted(ITEM_STATES):
        assert f"'{name}'" in ITEM_STATE_LIST_SQL

    with pytest.raises(ApiError) as caught:
        pl_service._validate_dimension(
            pl_service.DIMENSION_SCENE,
            [_item_body("a", 500, state="ARCHIVED")],
        )
    message = caught.value.details["fieldErrors"][0]["message"]
    assert ITEM_STATE_LIST_SQL in message, (
        f"state 的错误文案里没有常量源 {ITEM_STATE_LIST_SQL!r}，实际：{message!r}"
    )


# ===========================================================================
# 第 2 层：服务层（真库）
# ===========================================================================


@contextmanager
def _repo_with_versions(
    env: PlanningEnv,
) -> Iterator[tuple[PlanningRepository, VersionRepository]]:
    """一次事务里的 `(PlanningRepository, VersionRepository)`。

    **必须同一个 session**：`submit_draft` 的注释已论证 —— 两个 session 会
    让"刚在 `repo` 里锁住的行"与"版本注册所在的事务"变成两个（ISS-54 同族）。
    本工厂的存在就是为了让测试不可能踩到那个坑。
    """
    with session_scope(env.settings) as session:
        yield PlanningRepository(session), VersionRepository(session)


def _draft_row(env: PlanningEnv, *, project_id: uuid.UUID) -> Any:
    rows = _raw(
        env,
        "SELECT state, total_quantity, lock_version, baseline_id,"
        " jsonb_array_length(scene_items), jsonb_array_length(form_items)"
        " FROM plan_drafts WHERE project_id = :pid",
        {"pid": str(project_id)},
    )
    assert len(rows) == 1, f"草稿应恰好 1 行，实际 {len(rows)} 行"
    return rows[0]


def _current_version(env: PlanningEnv, *, project_id: uuid.UUID | None = None) -> int:
    """草稿的当前 `lock_version`（草稿不存在时先惰性建出来再读）。

    ⚠️ **不要写死版本号**：`plan_drafts.lock_version` 的 DDL 默认值是
    **`DEFAULT 1`**（不是 0）。写死数字会让每条保存判据在一开始就 412，
    而失败信息看起来像"乐观锁坏了"——实际是判据写错了。
    与 `_next_revision` 同一条纪律：可变状态一律**现读**。
    """
    pid = env.project_id if project_id is None else project_id
    with _repo(env) as repo:
        return pl_service.require_draft(repo, project_id=pid).lock_version


def _save(env: PlanningEnv, **kwargs: Any) -> Any:
    """`save_draft` 的测试包装：`expected_lock_version` 自动取当前值。

    **单独写包装而不是在每条用例里手写版本**：那样每加一条判据就要重复
    一次"先读版本"，而漏掉的那条会以 412 的形式失败（看起来像乐观锁的
    问题）。包装让"过期的版本"变成必须**显式**构造的东西 ——
    即只有 `test_save_draft_rejects_stale_lock_version` 那种用例才写它。
    """
    project_id = kwargs.pop("project_id", env.project_id)
    kwargs.setdefault("expected_lock_version", _current_version(env, project_id=project_id))
    with _repo(env) as repo:
        return pl_service.save_draft(repo, project_id=project_id, **kwargs)


# --- 2.1 草稿：懒创建 ------------------------------------------------------


def test_require_draft_creates_once_and_is_idempotent(env: PlanningEnv) -> None:
    """第一次调用建 1 行；第二次调用**不新增行、不动 `lock_version`**。

    `lock_version` 这一条是关键：若第二次调用又"建了一遍"或碰了行，
    客户端的 ETag 会在它什么都没做的时候失效 —— 于是它下一步的 `PUT`
    被 412 挡下，而失败信息指向并发（误导排障方向）。
    """
    assert _draft_state(env, project_id=env.project_id)[1] == 0

    with _repo(env) as repo:
        first = pl_service.require_draft(repo, project_id=env.project_id)
        first_id, first_version = first.id, first.lock_version

    assert _draft_state(env, project_id=env.project_id) == (first_version, 1)

    with _repo(env) as repo:
        second = pl_service.require_draft(repo, project_id=env.project_id)
        assert second.id == first_id
        assert second.lock_version == first_version

    # 仍然只有 1 行，且版本没被碰过。
    assert _draft_state(env, project_id=env.project_id) == (first_version, 1)


def test_require_draft_rejects_missing_project(env: PlanningEnv) -> None:
    """不存在的项目 ⇒ 可读的 404，而不是 FK 违反导致的 500。"""
    ghost = uuid.uuid4()
    with _repo(env) as repo:
        with pytest.raises(ApiError) as caught:
            pl_service.require_draft(repo, project_id=ghost)
    assert caught.value.code == ErrorCode.RESOURCE_NOT_FOUND
    assert caught.value.details["reason"] == REASON_PROJECT_NOT_FOUND


def test_new_draft_starts_in_editing_with_no_content(env: PlanningEnv) -> None:
    """新草稿的初始形态：`EDITING` / 无总量 / 空数组 / `lock_version` 为 DDL 默认值。

    **空数组用 `'[]'::jsonb` 而不是 `NULL`**：`NULL` 与 `[]` 在前端是
    两种渲染（"加载中" vs "还没有条目"），而 DDL 的 `jsonb_array_length`
    在 `NULL` 上返回 `NULL`（不是 0）—— 那会让所有长度判断变成三值逻辑。

    **`lock_version` 取 DDL 默认值而不是常量 0**：DDL 里是 `DEFAULT 1`。
    判据写 `== 0` 会在这里红，而根因不在实现 —— 所以这条断言**直接与
    DDL 默认值比对**（读 `PlanDraft.__table__` 而不是写死一个数字），
    将来默认值改成 0 或 2 时这条判据仍然是对的。
    """
    with _repo(env) as repo:
        pl_service.require_draft(repo, project_id=env.project_id)
    state, total, lock_version, baseline, scenes, forms = _draft_row(
        env, project_id=env.project_id
    )
    assert state == DRAFT_STATE_EDITING
    assert total is None
    assert lock_version == _ddl_lock_version_default()
    assert baseline is None
    assert scenes == 0
    assert forms == 0


def _ddl_lock_version_default() -> int:
    """`plan_drafts.lock_version` 的 DDL 默认值（从模型读，不写死）。"""
    column = PlanDraft.__table__.columns["lock_version"]
    assert column.server_default is not None
    return int(str(column.server_default.arg))


# --- 2.2 草稿：保存的部分更新语义 -----------------------------------------


def test_save_draft_does_not_clear_untouched_blocks(env: PlanningEnv) -> None:
    """**`None` = 本次不改这一块**：只传 `sceneItems` 时 `totalQuantity` 必须保留。

    这是 WP18/WP22 反复出现的 MAJOR 同型：把 `None` 当"改成空"会让
    "只想改场景比例"的请求静默清空总量 —— 而响应是 200，
    下一次分配**没有总数可用**且不报错。
    """
    first = _save(
        env,
        total_quantity=20,
        scene_items=[_item_body("a", 600), _item_body("b", 400)],
    )
    version_after_first = first.lock_version

    _save(
        env,
        expected_lock_version=version_after_first,
        scene_items=[_item_body("a", 500), _item_body("b", 300)],
    )

    _state, total, lock_version, _baseline, scenes, _forms = _draft_row(
        env, project_id=env.project_id
    )
    assert total == 20, "只改 sceneItems 时 totalQuantity 被清空了"
    assert scenes == 2
    assert lock_version == version_after_first + 1


def test_save_draft_without_touching_anything_keeps_lock_version(
    env: PlanningEnv,
) -> None:
    """一个字段都没传 ⇒ **不递增 `lock_version`**（否则 ETag 无谓失效）。

    反向验证：把 `touches_something` 那段短路的代码删掉（改成无条件
    `update_draft`），本判据必须 FAIL —— 因为那时 `lock_version` 会 +1。
    """
    version = _current_version(env)
    with _repo(env) as repo:
        pl_service.save_draft(
            repo, project_id=env.project_id, expected_lock_version=version
        )
    assert _draft_state(env, project_id=env.project_id) == (version, 1)


def test_save_draft_rejects_stale_lock_version(env: PlanningEnv) -> None:
    """`expected_lock_version` 过期 ⇒ **412 且回带当前 ETag**。

    回带 `currentEtag` 是前端"不发第二次 GET 就能重试"所依赖的字段，
    缺它只能靠前端再查一次（而那次查与这次重试之间又有窗口）。

    ⚠️ 这是**唯一**显式构造"过期版本"的用例：其余保存用例走 `_save`
    自动取当前版本。"能否构造出这个错"本身就是要被钉住的性质。
    """
    stale = _current_version(env)
    fresh = _save(env, total_quantity=10)
    assert fresh.lock_version == stale + 1

    with _repo(env) as repo:
        with pytest.raises(ApiError) as caught:
            pl_service.save_draft(
                repo,
                project_id=env.project_id,
                expected_lock_version=stale,
                total_quantity=11,
            )
    assert caught.value.code == ErrorCode.VERSION_CONFLICT
    assert caught.value.details["reason"] == "lock_version_mismatch"
    assert caught.value.details["currentEtag"] == (
        f'"{_draft_id(env)}:{fresh.lock_version}"'
    )

    # 值没被改（失败是原子的）。
    assert _draft_row(env, project_id=env.project_id)[1] == 10


def _draft_id(env: PlanningEnv) -> str:
    return str(
        _scalar(
            env,
            "SELECT id FROM plan_drafts WHERE project_id = :pid",
            {"pid": str(env.project_id)},
        )
    )


def test_save_draft_rejects_foreign_baseline(env: PlanningEnv) -> None:
    """**FK 只保证基线存在，不保证属于本项目** —— 必须应用层校验。

    不校验的话，项目 A 的草稿能钉住项目 B 的基线：静默的越权读取，
    而固化出的 PLAN 版本会带着别的项目的基线内容，
    权限判定却只看草稿所属项目（与 WP23 同形）。
    """
    foreign = _insert_baseline(env, project_id=env.other_project_id)
    with _repo(env) as repo:
        with pytest.raises(ApiError) as caught:
            pl_service.save_draft(
                repo,
                project_id=env.project_id,
                expected_lock_version=_current_version(env),
                baseline_id=foreign,
            )
    assert caught.value.code == ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == REASON_BASELINE_MISMATCH

    # 自己的基线必须通过（否则这条校验就是"全拒"，判据会假绿）。
    own = _insert_baseline(env, project_id=env.project_id)
    draft = _save(env, baseline_id=own)
    assert draft.baseline_id == own


@pytest.mark.parametrize("bad", [0, -1, MAX_QUANTITY_BOUND + 1])
def test_save_draft_rejects_out_of_range_total(
    env: PlanningEnv, bad: int
) -> None:
    """总量越界 ⇒ 422（而不是 DDL 报出的 500）。"""
    with pytest.raises(ApiError) as caught:
        _save(env, total_quantity=bad)
    assert caught.value.code == ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == REASON_TOTAL_QUANTITY_INVALID


def test_save_draft_rejects_bool_total(env: PlanningEnv) -> None:
    """**`bool` 是 `int` 的子类** —— `True` 不显式排除会被当成 1。

    反向验证：删掉 `isinstance(value, bool)` 那半个条件，本判据必须 FAIL。
    """
    with pytest.raises(ApiError) as caught:
        _save(env, total_quantity=True)
    assert caught.value.details["reason"] == REASON_TOTAL_QUANTITY_INVALID


def test_save_draft_rejects_duplicate_item_key(env: PlanningEnv) -> None:
    """重复 `key` ⇒ 422。**合计对得上也拦**（`sum == total` 检测不到它）。"""
    with pytest.raises(ApiError) as caught:
        _save(env, scene_items=[_item_body("s1", 500), _item_body("s1", 500)])
    assert caught.value.code == ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == REASON_DUPLICATE_ITEM_KEY
    assert caught.value.details["duplicateKey"] == "s1"


def test_save_draft_rejects_unknown_item_field(env: PlanningEnv) -> None:
    """未知字段 ⇒ 422（放行会让垃圾字段永久留在 JSONB 里）。"""
    with pytest.raises(ApiError) as caught:
        _save(env, scene_items=[_item_body("s1", 500, note="这是垃圾字段")])
    assert caught.value.details["reason"] == REASON_DIMENSION_LIMIT_EXCEEDED
    assert "note" in caught.value.details["unknown"]


def test_save_draft_rejects_too_many_items(env: PlanningEnv) -> None:
    """条数上限按 `MAX_DIMENSION_ITEMS` 卡（不卡会让单请求写入无界 JSONB）。"""
    items = [_item_body(f"s{i}", 0) for i in range(MAX_DIMENSION_ITEMS + 1)]
    with pytest.raises(ApiError) as caught:
        _save(env, scene_items=items)
    assert caught.value.details["reason"] == REASON_DIMENSION_LIMIT_EXCEEDED
    assert caught.value.details["maxItems"] == MAX_DIMENSION_ITEMS


def test_save_draft_rejects_negative_capacity(env: PlanningEnv) -> None:
    """容量为负 ⇒ 422（放行会让分配算出负数，而负数是"少了几条"的静默表现）。"""
    with pytest.raises(ApiError) as caught:
        _save(env, form_items=[_item_body("f1", 1000, capacity=-1)])
    assert caught.value.details["reason"] == REASON_DIMENSION_LIMIT_EXCEEDED


def test_save_draft_accepts_permille_total_not_1000(env: PlanningEnv) -> None:
    """**保存**接受合计 ≠ 1000（拖一下就自动保存的交互）。

    关口在提交。若保存就强制 1000，运营每拖一次都失败 ——
    而"运营会算不对"是 §2.2 显式写进用户画像的。
    """
    before = _current_version(env)
    draft = _save(env, total_quantity=10, scene_items=[_item_body("a", 300)])
    assert draft.lock_version == before + 1
    assert _draft_row(env, project_id=env.project_id)[4] == 1


def test_save_draft_returns_reread_row(env: PlanningEnv) -> None:
    """返回值必须是**重读**过的行 —— 条件 UPDATE 绕过了身份映射。

    反向验证：把 `repo.reload_draft(draft.id)` 换成 `return draft`，
    本判据必须 FAIL（那时 `lock_version` 停在旧值，而库里已是新值）。
    """
    before = _current_version(env)
    draft = _save(env, total_quantity=7)
    assert draft.total_quantity == 7
    assert draft.lock_version == before + 1
    # 与库里的实际状态一致（不是"碰巧"）。
    assert _draft_row(env, project_id=env.project_id)[1] == 7
    assert _draft_state(env, project_id=env.project_id)[0] == before + 1


# --- 2.3 分配：草稿上的派生读数 -------------------------------------------


def test_dimension_allocation_without_total_returns_zeros(
    env: PlanningEnv,
) -> None:
    """总量未设 ⇒ 全 0 且**不报错**（"先配比例、后定总量"是真实交互顺序）。"""
    _save(env, scene_items=[_item_body("a", 600), _item_body("b", 400)])
    with _repo(env) as repo:
        draft = repo.get_draft(env.project_id)
        result = pl_service.dimension_allocation(draft, pl_service.DIMENSION_SCENE)
    assert result.total == 0
    assert result.unallocated == 0
    assert {line.key: line.allocated for line in result.lines} == {"a": 0, "b": 0}


def test_dimension_allocation_notes_normalization(env: PlanningEnv) -> None:
    """合计 ≠ 1000 时**归一并在 notes 里说明**（不静默修正）。

    "我配的是 280 怎么变成 282 了"必须有据可查 —— 否则用户会以为系统算错。
    """
    _save(
        env,
        total_quantity=10,
        scene_items=[_item_body("a", 280), _item_body("b", 280)],
    )
    with _repo(env) as repo:
        draft = repo.get_draft(env.project_id)
        result = pl_service.dimension_allocation(draft, pl_service.DIMENSION_SCENE)
    assert result.notes, "归一发生了但 notes 是空的 —— 这是一次静默修正"
    assert "已归一" in result.notes[0]
    assert "560" in result.notes[0]
    # 归一后合计恰为 total（不变量）。
    assert sum(line.allocated for line in result.lines) + result.unallocated == 10


def test_dimension_allocation_keeps_exact_total_untouched(
    env: PlanningEnv,
) -> None:
    """合计恰为 1000（含 `[333,333,334]`）⇒ **不归一、无 notes**。

    重算会因取整把 `[333,333,334]` 变成别的组合，而"保存时不改变
    运营看到的值"是一条该被保住的性质。
    """
    _save(
        env,
        total_quantity=9,
        scene_items=[
            _item_body("a", 333, sort_order=0),
            _item_body("b", 333, sort_order=1),
            _item_body("c", 334, sort_order=2),
        ],
    )
    with _repo(env) as repo:
        draft = repo.get_draft(env.project_id)
        result = pl_service.dimension_allocation(draft, pl_service.DIMENSION_SCENE)
    assert result.notes == ()
    assert [line.weight_permille for line in result.lines] == [333, 333, 334]


def test_disabled_item_keeps_its_row_with_zero_weight(env: PlanningEnv) -> None:
    """停用项**不从数组里删掉**，只是权重归零。

    删掉会让"这张表上原本有哪几行"这个事实消失（前端会看到行数变少）。
    """
    _save(
        env,
        total_quantity=10,
        scene_items=[
            _item_body("a", 1000, sort_order=0),
            _item_body("b", 500, sort_order=1, state="DISABLED"),
        ],
    )
    with _repo(env) as repo:
        draft = repo.get_draft(env.project_id)
        result = pl_service.dimension_allocation(draft, pl_service.DIMENSION_SCENE)
        items = pl_service.dimension_items(draft, pl_service.DIMENSION_SCENE)
    # 两行都在，停用项权重 0。
    assert [item.key for item in items] == ["a", "b"]
    assert [item.weight_permille for item in items] == [1000, 0]
    # 全部 10 条给 a（1000‰），b 拿 0 —— 且没触发归一。
    assert result.notes == ()
    assert {line.key: line.allocated for line in result.lines} == {"a": 10, "b": 0}


def test_disabled_reason_roundtrips_with_disabled_state(env: PlanningEnv) -> None:
    """**MINOR-1**：`disabledReason` 能保存且能读回（原型表格里的实文列）。

    ## 这条判据补的是一个"两边都不报错"的分歧

    修复前：`models.PlanDraft` 的 JSONB 行示例写着 `disabledReason`，
    原型 `static/trial-config.html` 也有这一列（两处实文：
    "停用原因：无对应已确认事实来源"），而 `service._ALLOWED_ITEM_KEYS`
    不认它 ⇒ **前端照文档发请求稳定 422**。测试文件里 `disabledReason`
    出现 **0 次**，因此这个分歧在两侧都无声无息。

    ## 为什么断言"读回"而不是只断言"保存没报错"

    只断言 200 的话，一个"接收了但丢弃"的实现也通过（正是
    `_validate_dimension` 只返回原列表、不做落库的那种形态）。
    这里从**独立数据源**（`jsonb` 列的原生 SQL 读）取回来比对，
    避免与 ORM 的同一份内存对象自证。
    """
    reason = "无对应已确认事实来源（原型表格实文）"
    _save(
        env,
        total_quantity=10,
        scene_items=[
            _item_body("a", 1000, sort_order=0),
            _item_body(
                "b",
                0,
                sort_order=1,
                state=ITEM_STATE_DISABLED,
                disabledReason=reason,
            ),
        ],
    )

    rows = _raw(
        env,
        "SELECT scene_items -> 1 ->> 'disabledReason' FROM plan_drafts"
        " WHERE project_id = :pid",
        {"pid": str(env.project_id)},
    )
    assert rows and rows[0][0] == reason, (
        f"disabledReason 没落进 JSONB，实际 {rows[0][0]!r}"
    )

    # API 层的行也带着它（前端表格直接渲染这一列）。
    with _repo(env) as repo:
        draft = repo.get_draft(env.project_id)
    body = pl_service.draft_view(draft)
    second = body.scene_items[1]
    assert second["state"] == ITEM_STATE_DISABLED
    assert second["disabledReason"] == reason


@pytest.mark.parametrize(
    "bad_item, pattern",
    [
        # 启用了却带着"停用原因" —— 自相矛盾，前端会渲染出
        # "启用中（停用原因：xxx）"。
        ({"disabledReason": "不该有原因"}, "只在 state="),
        # 超长：前端表格会被撑爆，且落库的是无界文本。
        ({"state": "DISABLED", "disabledReason": "x" * (MAX_DISABLED_REASON_CHARS + 1)},
         "不能超过"),
        # 类型错：写进 JSONB 后前端拿到的不是字符串。
        ({"state": "DISABLED", "disabledReason": 42}, "必须是字符串或 null"),
    ],
)
def test_disabled_reason_rejects_inconsistent_values(
    env: PlanningEnv, bad_item: dict[str, Any], pattern: str
) -> None:
    """`disabledReason` 的三条不一致：启用却填、超长、非字符串 ⇒ 全部 422。"""
    with pytest.raises(ApiError) as caught:
        _save(
            env,
            scene_items=[_item_body("a", 1000, sort_order=0, **bad_item)],
        )
    assert caught.value.details["reason"] == REASON_DIMENSION_LIMIT_EXCEEDED
    error = caught.value.details["fieldErrors"][0]
    assert error["field"] == "sceneItems[0].disabledReason"
    assert pattern in error["message"], f"错误文案里没有 {pattern!r}：{error['message']!r}"


def test_disabled_reason_boundary_is_inclusive(env: PlanningEnv) -> None:
    """恰好 `MAX_DISABLED_REASON_CHARS` 字**通过**（边界包含，不是"小于"）。

    与 `MAX_ITEM_NAME_CHARS` / `MAX_CHANGE_REASON_CHARS` 同一条处置：
    "最多 N 字"是指 N 也接受。判据只测上界外会漏掉"写成 `>=`"的差一错误
    —— 那种实现的症状是**合法的长原因被拒**，而它看起来像"原因太长"。
    """
    exact = "x" * MAX_DISABLED_REASON_CHARS
    draft = _save(
        env,
        scene_items=[
            _item_body(
                "a",
                1000,
                sort_order=0,
                state=ITEM_STATE_DISABLED,
                disabledReason=exact,
            )
        ],
    )
    assert draft.scene_items[0]["disabledReason"] == exact


def test_dimension_remaining_delegates_to_allocation(env: PlanningEnv) -> None:
    """`dimension_remaining` 与 `capacity_remaining` 的读数一致（不重算一遍）。"""
    _save(
        env,
        form_items=[_item_body("f1", 500, capacity=4), _item_body("f2", 500)],
    )
    with _repo(env) as repo:
        draft = repo.get_draft(env.project_id)
        remaining = pl_service.dimension_remaining(
            draft, pl_service.DIMENSION_FORM, allocated_so_far={"f1": 3}
        )
    assert remaining == {"f1": 1, "f2": None}


# --- 2.4 固化：草稿 → PLAN 版本 -------------------------------------------


def _seed_submittable_draft(env: PlanningEnv, *, total: int = 10) -> int:
    """造一份"可以提交"的草稿，返回它的 `lock_version`。"""
    draft = _save(
        env,
        total_quantity=total,
        scene_items=[
            _item_body("s1", 600, sort_order=0),
            _item_body("s2", 400, sort_order=1),
        ],
        form_items=[_item_body("f1", 1000, sort_order=0)],
        pairings=[{"sceneKey": "s1", "formKey": "f1"}],
    )
    return draft.lock_version


def test_submit_draft_registers_plan_version(env: PlanningEnv) -> None:
    """正常路径：产出 `entity_type='PLAN'` 的版本节点，"实体"是**项目**。

    `entity_id == project_id` 而不是 `draft_id` —— 传 `draft.id` 会让
    "同一项目的第二份计划"拿到修订号 1 而不是 2。
    """
    version = _seed_submittable_draft(env)
    with _repo_with_versions(env) as (repo, version_repo):
        registered = pl_service.submit_draft(
            repo,
            version_repo,
            project_id=env.project_id,
            expected_lock_version=version,
            actor_id=env.owner_id,
        )
    assert registered.entity_type == ENTITY_TYPE_PLAN
    assert registered.entity_id == env.project_id, (
        "PLAN 的 entity_id 必须是 project_id（否则同一项目的第二份计划会拿到修订号 1）"
    )
    assert registered.project_id == env.project_id
    assert registered.created is True
    assert registered.revision_no == 1

    # 草稿状态被改成 SUBMITTED（**不删除** —— 配比表还要展示）。
    assert _draft_row(env, project_id=env.project_id)[0] == DRAFT_STATE_SUBMITTED


def test_submit_draft_payload_has_inputs_and_results(env: PlanningEnv) -> None:
    """`payload` 同时含输入（比例）与结果（分配数量），且不含时间戳。

    只放结果：事后无法回答"当时配的比例是多少"（最大余数法**不可逆**）。
    放时间戳：同一份内容两次提交会得到不同 `payload_hash`，
    于是网络重试的那一次不再被识别成幂等重复。

    ⚠️ **不能从 `version_nodes` 里读 payload** —— 实测该表只有
    `payload_hash` 列，**没有 `payload` 列**。这是 WP11 的设计
    （`versions/models.py`：注册表"只存内容摘要，不存正文"），
    正文归各领域模块。因此本判据分两段，缺一不可：

      · 内容断言：`plan_payload()` **当场组装**的那份（`submit_draft`
        固化时用的就是它，见 `service.submit_draft`）；
      · 落库断言：库里那行的 `payload_hash` **等于**这份内容的规范化摘要
        —— 这一段才证明"固化用的是这份内容"，否则前一段只是
        在断言一个没被使用的局部变量。

    反向验证：把 `submit_draft` 改成注册一个"少了 `scenes`"的 payload，
    第二段必须 FAIL（第一段不会，因为是当场组装的）。
    """
    version = _seed_submittable_draft(env, total=10)

    # ① 固化前，先独立组装一份 —— 判据自己算，不引用实现里的中间量。
    with _repo(env) as repo:
        draft = repo.get_draft(env.project_id)
        expected = pl_service.plan_payload(
            draft,
            scene_result=pl_service.dimension_allocation(
                draft, pl_service.DIMENSION_SCENE
            ),
            form_result=pl_service.dimension_allocation(
                draft, pl_service.DIMENSION_FORM
            ),
        )

    version = _current_version(env)
    with _repo_with_versions(env) as (repo, version_repo):
        registered = pl_service.submit_draft(
            repo,
            version_repo,
            project_id=env.project_id,
            expected_lock_version=version,
            actor_id=env.owner_id,
        )

    payload = expected
    assert payload["projectId"] == str(env.project_id)
    assert payload["totalQuantity"] == 10
    assert payload["baselineId"] is None
    assert {row["key"]: row["weightPermille"] for row in payload["scenes"]} == {
        "s1": 600,
        "s2": 400,
    }
    assert {row["key"]: row["allocated"] for row in payload["scenes"]} == {
        "s1": 6,
        "s2": 4,
    }
    assert {row["key"]: row["allocated"] for row in payload["forms"]} == {"f1": 10}
    assert payload["pairings"] == [{"sceneKey": "s1", "formKey": "f1"}]
    # 不含时间戳 / actor —— 它们描述"这次注册行为"，不是"这份计划的内容"。
    for forbidden in ("created", "createdAt", "actor", "actorId", "revisionNo"):
        assert forbidden not in payload

    # ② 落库的那一行，摘要必须等于这份内容 —— 这一段接住"固化的不是这份内容"。
    stored_hash = _scalar(
        env,
        "SELECT payload_hash FROM version_nodes WHERE id = :nid",
        {"nid": str(registered.node_id)},
    )
    assert stored_hash == canonical_payload_hash(payload)
    # `payload_hash` 是 SHA-256 十六进制（长度 64）—— 顺带钉住它没被截断。
    assert len(stored_hash) == 64


def test_submit_draft_twice_is_rejected(env: PlanningEnv) -> None:
    """第二次提交 ⇒ 409 `DRAFT_ALREADY_SUBMITTED`（不是产出第二个 PLAN 版本）。

    这里用 `expected_lock_version` 传**当前**值，证明拦下它的是
    `state != EDITING` 那一支，而不是乐观锁 —— 两条防线各测各的。
    """
    version = _seed_submittable_draft(env)
    with _repo_with_versions(env) as (repo, version_repo):
        pl_service.submit_draft(
            repo,
            version_repo,
            project_id=env.project_id,
            expected_lock_version=version,
            actor_id=env.owner_id,
        )
    current = _draft_state(env, project_id=env.project_id)[0]

    with _repo_with_versions(env) as (repo, version_repo):
        with pytest.raises(ApiError) as caught:
            pl_service.submit_draft(
                repo,
                version_repo,
                project_id=env.project_id,
                expected_lock_version=current,
                actor_id=env.owner_id,
            )
    assert caught.value.code == ErrorCode.INVALID_TRANSITION
    assert caught.value.details["reason"] == REASON_DRAFT_ALREADY_SUBMITTED

    # 没有第二个 PLAN 版本。
    count = _scalar(
        env,
        "SELECT count(*) FROM version_nodes"
        " WHERE entity_type = :t AND entity_id = :e",
        {"t": ENTITY_TYPE_PLAN, "e": str(env.project_id)},
    )
    assert count == 1


def test_submit_draft_requires_total_quantity(env: PlanningEnv) -> None:
    """未设总量 ⇒ 422（分配没有总数，固化出来的计划没有数量含义）。"""
    version = _current_version(env)
    with _repo_with_versions(env) as (repo, version_repo):
        with pytest.raises(ApiError) as caught:
            pl_service.submit_draft(
                repo,
                version_repo,
                project_id=env.project_id,
                expected_lock_version=version,
                actor_id=env.owner_id,
            )
    assert caught.value.code == ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == REASON_TOTAL_QUANTITY_INVALID


def test_submit_draft_requires_permille_total(env: PlanningEnv) -> None:
    """**固化前**两维比例合计必须严格 1000（判的是**草稿存的**，不是归一的）。

    ⚠️ 这是本包最容易写错的一条判据：用 `result`（已归一的分配）来判
    会恒真 —— 分配路径永远把合计归一成 1000，所以"看起来对"。
    要判的是草稿里存的比例。

    反向验证：把 `_require_permille_total` 改成读 `result.lines` 的权重之和，
    本判据必须 FAIL（那时它会通过）。
    """
    draft = _save(
        env,
        total_quantity=10,
        scene_items=[
            _item_body("s1", 300, sort_order=0),
            _item_body("s2", 300, sort_order=1),
        ],
        form_items=[_item_body("f1", 1000, sort_order=0)],
    )

    with _repo_with_versions(env) as (repo, version_repo):
        with pytest.raises(ApiError) as caught:
            pl_service.submit_draft(
                repo,
                version_repo,
                project_id=env.project_id,
                expected_lock_version=draft.lock_version,
                actor_id=env.owner_id,
            )
    assert caught.value.code == ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == REASON_PERMILLE_NOT_TOTAL
    # 报的是**草稿存的** 600，而不是归一后的 1000。
    assert caught.value.details["actualTotal"] == 600
    assert caught.value.details["expectedTotal"] == PERMILLE_TOTAL

    # 草稿仍停在 EDITING（失败是原子的）。
    assert _draft_row(env, project_id=env.project_id)[0] == DRAFT_STATE_EDITING


def test_submit_draft_rejects_forbidden_form_total(env: PlanningEnv) -> None:
    """**形式**那一维独立判一次 —— 只判场景会让"形式合计 990"溜过去。"""
    draft = _save(
        env,
        total_quantity=10,
        scene_items=[_item_body("s1", 1000, sort_order=0)],
        form_items=[_item_body("f1", 990, sort_order=0)],
    )

    with _repo_with_versions(env) as (repo, version_repo):
        with pytest.raises(ApiError) as caught:
            pl_service.submit_draft(
                repo,
                version_repo,
                project_id=env.project_id,
                expected_lock_version=draft.lock_version,
                actor_id=env.owner_id,
            )
    assert caught.value.details["reason"] == REASON_PERMILLE_NOT_TOTAL
    assert caught.value.details["actualTotal"] == 990


def test_submit_draft_rejects_stale_lock_version(env: PlanningEnv) -> None:
    """乐观锁那一支：`expected_lock_version` 与库里不符 ⇒ 412。"""
    version = _seed_submittable_draft(env)
    with _repo_with_versions(env) as (repo, version_repo):
        with pytest.raises(ApiError) as caught:
            pl_service.submit_draft(
                repo,
                version_repo,
                project_id=env.project_id,
                expected_lock_version=version - 1,
                actor_id=env.owner_id,
            )
    assert caught.value.code == ErrorCode.VERSION_CONFLICT
    assert caught.value.details["reason"] == "lock_version_mismatch"


def test_submit_draft_without_draft_returns_not_found(env: PlanningEnv) -> None:
    """没有草稿就提交 ⇒ 404（不是"顺手建一份空草稿再提交" —— 那会 422 总量）。"""
    assert _draft_state(env, project_id=env.project_id)[1] == 0
    with _repo_with_versions(env) as (repo, version_repo):
        with pytest.raises(ApiError) as caught:
            pl_service.submit_draft(
                repo,
                version_repo,
                project_id=env.project_id,
                expected_lock_version=_ddl_lock_version_default(),
                actor_id=env.owner_id,
            )
    assert caught.value.code == ErrorCode.RESOURCE_NOT_FOUND
    assert caught.value.details["reason"] == REASON_DRAFT_NOT_FOUND
    # 仍然没有草稿（提交路径**不**惰性建草稿）。
    assert _draft_state(env, project_id=env.project_id)[1] == 0


def test_plan_payload_is_deterministic_for_same_draft(env: PlanningEnv) -> None:
    """同一份草稿两次组装 payload ⇒ **完全相同**（`payload_hash` 才有幂等意义）。

    这是"payload 里不能放时间戳"的可观测后果 —— 放了的话第二次
    会算出不同哈希，于是它变成"内容变了"。
    """
    _save(
        env,
        total_quantity=10,
        scene_items=[_item_body("s1", 1000, sort_order=0)],
        form_items=[_item_body("f1", 1000, sort_order=0)],
    )
    with _repo(env) as repo:
        draft = repo.get_draft(env.project_id)
        scene_result = pl_service.dimension_allocation(
            draft, pl_service.DIMENSION_SCENE
        )
        form_result = pl_service.dimension_allocation(
            draft, pl_service.DIMENSION_FORM
        )
        first = pl_service.plan_payload(
            draft, scene_result=scene_result, form_result=form_result
        )
        second = pl_service.plan_payload(
            draft, scene_result=scene_result, form_result=form_result
        )
        view = pl_service.draft_view(draft)

    assert first == second
    assert view.total_quantity == 10
    assert view.project_id == env.project_id


# ===========================================================================
# 2.5 配置读取与惰性物化（OP-60 的 J1）
# ===========================================================================


def _seed_global(
    env: PlanningEnv,
    *,
    key: str = SETTING_KEY_BATCH_LIMITS,
    value: dict[str, Any] | None = None,
    reason: str = "WP24 测试全局默认值",
) -> None:
    """只写一条**全局行**，**不**碰任何项目级行。

    ## 为什么不用 `change_global_setting`（本 helper 的第一版就是它）

    `change_global_setting` 是生产唯一全局写入口，它**同事务级联刷新所有
    项目**（§3.6.2 步骤 ②，含"尚无 PROJECT 行"的路径 c）。因此用它造"起点"
    会有一个连带后果：**本用例刚造的项目立刻就有了 `INHERITED` 副本行**。

    这直接摧毁了两条判据的前提：

    | 判据 | 需要的前提 | 用 `change_global_setting` 造起点时 |
    |---|---|---|
    | J1 惰性物化 | "读之前**确实没有**项目级行" | 已有副本行 ⇒ 前提**必然不成立** |
    | J2 路径 c 回填 | "这个项目**从未有过**任何项目级行" | 同上 |

    两者都是"先断言缺失、再断言补上"的形态，起点若已被级联污染，
    判据就变成了一句**永远为假的前置**（实测报 `assert 1 == 0`）。

    于是本 helper 的职责收窄为：**建立"全局有值"这一个事实**，
    级联路径交给专门测它的用例（就地调 `change_global_setting`）。

    ## 为什么直接插原生 SQL 是合法的

    本表允许**追加**（只有 UPDATE/DELETE 被拦，SQLSTATE `23001`），
    且四类 CHECK 都容易满足：`scope_kind='GLOBAL'` ⇒ `project_id IS NULL`
    且 `origin='GLOBAL'`（`ck_runtime_settings_origin_matches_scope`
    与 `ck_runtime_settings_scope_matches_project` 的要求）。

    `revision_no` 取**该范围当前的 max + 1** —— 不能写死 1：
    夹具只在 module 起始 TRUNCATE 一次，同一次运行内前面的用例
    可能已写过 GLOBAL 行，写死会撞 `uq_runtime_settings_global`
    （部分唯一索引，实测报 23505）。

    **取号与插入分两条语句**（而不是写成一个带子查询的 INSERT）：
    实测那样会被 psycopg 的类型推断拒绝 ——
    `... key = :k ...` 与 `VALUES (:k, ...)` 里的同一个 `:k` 落在
    `text` 与 `character varying` 两个上下文，报
    `AmbiguousParameter: inconsistent types deduced for parameter $1`。
    分成两条既避开这个坑，也让"取了哪个号"在失败时可见。
    测试内无并发，两步之间不存在竞争窗口。

    **不传 `actor_id`**：`reason` 允许非空串、`actor_id` 允许 `NULL`
    （都是可空审计字段）。传了反而要多造一个用户，收益为零。
    """
    seed_value = dict(DEFAULT_BATCH_MAX) if value is None else value
    next_revision = int(
        _scalar(
            env,
            "SELECT coalesce(max(revision_no), 0) FROM runtime_settings"
            " WHERE key = :k AND scope_kind = 'GLOBAL'",
            {"k": key},
        )
        or 0
    ) + 1
    _raw(
        env,
        "INSERT INTO runtime_settings"
        " (key, scope_kind, project_id, value, revision_no, origin, reason)"
        " VALUES (:k, 'GLOBAL', NULL, CAST(:v AS jsonb), :rev, 'GLOBAL', :reason)",
        {
            "k": key,
            "v": json.dumps(seed_value),
            "rev": next_revision,
            "reason": reason,
        },
    )


def _setting_rows(
    env: PlanningEnv, *, key: str, project_id: uuid.UUID | None
) -> int:
    """数一个 `(key, scope, project)` 组合下的行数 —— 差分断言的基准。"""
    if project_id is None:
        return int(
            _scalar(
                env,
                "SELECT count(*) FROM runtime_settings"
                " WHERE key = :k AND scope_kind = 'GLOBAL'",
                {"k": key},
            )
            or 0
        )
    return int(
        _scalar(
            env,
            "SELECT count(*) FROM runtime_settings"
            " WHERE key = :k AND scope_kind = 'PROJECT' AND project_id = :pid",
            {"k": key, "pid": str(project_id)},
        )
        or 0
    )


def _current_setting_row(
    env: PlanningEnv, *, key: str, project_id: uuid.UUID | None
) -> dict[str, Any] | None:
    """取当前行（`revision_no` 最大）的 `(value, origin, revision_no)`。"""
    if project_id is None:
        sql = (
            "SELECT value, origin, revision_no FROM runtime_settings"
            " WHERE key = :k AND scope_kind = 'GLOBAL'"
            " ORDER BY revision_no DESC LIMIT 1"
        )
        params: dict[str, Any] = {"k": key}
    else:
        sql = (
            "SELECT value, origin, revision_no FROM runtime_settings"
            " WHERE key = :k AND scope_kind = 'PROJECT' AND project_id = :pid"
            " ORDER BY revision_no DESC LIMIT 1"
        )
        params = {"k": key, "pid": str(project_id)}
    rows = _raw(env, sql, params)
    if not rows:
        return None
    return {"value": rows[0][0], "origin": rows[0][1], "revision_no": rows[0][2]}


def _effective(
    env: PlanningEnv,
    *,
    key: str = SETTING_KEY_BATCH_LIMITS,
    project_id: uuid.UUID | None = None,
    materialize: bool = True,
) -> EffectiveSetting:
    with _repo(env) as repo:
        return pl_settings.get_effective_limits(
            repo, key, project_id, materialize=materialize
        )


def test_bootstrap_defaults_is_idempotent(env: PlanningEnv) -> None:
    """`bootstrap_defaults` 第二次调用**不新增行、不递增修订号**。

    迁移是可重入的（`alembic upgrade head` 在一个已升到 head 的库上是 no-op），
    因此这个函数必须幂等。不幂等的话，重跑迁移会在"第 1 版默认值"之上
    叠一个"第 2 版"—— 而两版内容完全一样，审计里却多出一条无从解释的变更。

    本判据用**差分**而不是"表里有 1 行"：`runtime_settings` 不可变，
    异常路径（`pytest.raises` 里的事务）也可能留下行。
    """
    with _repo(env) as repo:
        pl_settings.bootstrap_defaults(repo, actor_id=env.admin_id)
    after_first = _current_setting_row(
        env, key=SETTING_KEY_BATCH_LIMITS, project_id=None
    )
    assert after_first is not None
    assert after_first["value"] == dict(DEFAULT_BATCH_MAX)
    assert after_first["origin"] == SETTING_ORIGIN_GLOBAL

    with _repo(env) as repo:
        pl_settings.bootstrap_defaults(repo, actor_id=env.admin_id)
    after_second = _current_setting_row(
        env, key=SETTING_KEY_BATCH_LIMITS, project_id=None
    )
    assert after_second == after_first, (
        "bootstrap_defaults 第二次调用改了全局行 —— 迁移将不再可重入"
    )


def test_bootstrap_defaults_backfills_existing_projects(env: PlanningEnv) -> None:
    """存量项目被回填 `INHERITED` 副本（§3.6.3 路径 a）。

    **"存量"= 全部项目**（含刚造的两个）—— 归档项目也可能被重开
    （WP09 的 `REOPEN_PROJECT`），那时它的配置不该是空的。

    反向验证：把 `refresh_existing_projects` 的默认值改成 `False`，
    本判据必须 FAIL。
    """
    for pid in (env.project_id, env.other_project_id):
        assert _setting_rows(
            env, key=SETTING_KEY_BATCH_LIMITS, project_id=pid
        ) == 0, "前置：此刻还没有任何项目级行"

    with _repo(env) as repo:
        pl_settings.bootstrap_defaults(repo, actor_id=env.admin_id)

    for pid in (env.project_id, env.other_project_id):
        row = _current_setting_row(
            env, key=SETTING_KEY_BATCH_LIMITS, project_id=pid
        )
        assert row is not None, f"项目 {pid} 没有被回填副本"
        assert row["value"] == dict(DEFAULT_BATCH_MAX)
        assert row["origin"] == SETTING_ORIGIN_INHERITED


def test_bootstrap_defaults_does_not_touch_existing_manual_row(
    env: PlanningEnv,
) -> None:
    """已有 `MANUAL` 行的项目**不动** —— 迁移不该覆盖任何已存在的决策。

    **反例正是"刷新全部"**：那样一次迁移会把运营特意给某个项目调小的
    上限改回全局默认值，而且**不报错**（下次读到的是 10，与全局一样）。
    """
    _seed_global(env)
    custom = {"TRIAL": 3, "SCALE": 30, "BULK": 50}
    with _repo(env) as repo:
        pl_settings.change_project_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            project_id=env.project_id,
            value=custom,
            reason="本项目试产期上限更严",
            actor_id=env.owner_id,
        )
    before = _current_setting_row(
        env, key=SETTING_KEY_BATCH_LIMITS, project_id=env.project_id
    )
    assert before is not None and before["value"] == custom

    with _repo(env) as repo:
        pl_settings.bootstrap_defaults(repo, actor_id=env.admin_id)

    after = _current_setting_row(
        env, key=SETTING_KEY_BATCH_LIMITS, project_id=env.project_id
    )
    assert after == before, "迁移覆盖了运营已做的项目级决策"


def test_get_effective_limits_returns_global_view_when_no_project(
    env: PlanningEnv,
) -> None:
    """`project_id=None` ⇒ 全局视图：`source='GLOBAL'`、`diverged=False`。"""
    _seed_global(env)
    setting = _effective(env)
    assert setting.configured is True
    assert setting.value == dict(DEFAULT_BATCH_MAX)
    assert setting.source == SETTING_ORIGIN_GLOBAL
    assert setting.project_id is None
    assert setting.diverged is False


def test_unconfigured_placeholder_key_is_not_an_error(env: PlanningEnv) -> None:
    """**占位键**未配置时返回 `configured=False`，而不是抛异常（OP-64）。

    `platform.mediaLimits` 是"机制已建、实测值未到"的键，它的合法状态
    就是没有值。若这里抛错，前端展示"尚未配置"的那一块会变成 500。

    **`configured` 不能退化成 `value is None`**：`{"a": null}` 是
    "有值但值为空"（形状上应当被校验层放行），与"没有值"是两件事。
    """
    setting = _effective(env, key=SETTING_KEY_PLATFORM_MEDIA_LIMITS)
    assert setting.configured is False
    assert setting.value is None
    assert setting.source is None
    assert setting.revision_no is None

    # `require_configured` 是"必须已有值"的消费方入口 ⇒ 这里必须 422。
    with pytest.raises(ApiError) as caught:
        with _repo(env) as repo:
            pl_settings.require_configured(setting)
    assert caught.value.code == ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == REASON_SETTING_NOT_CONFIGURED

    # 已配置的键必须能通过（否则这条校验就是"全拒"，判据会假绿）。
    _seed_global(env)
    assert pl_settings.require_configured(_effective(env)).configured is True


def test_read_materializes_inherited_copy_lazily(env: PlanningEnv) -> None:
    """**J1**：读过一次之后，项目就有了自己的副本行，且**来源标注为 `INHERITED`**。

    这是 OP-60 "支持且必须有项目级副本"的直接落点，三个断言缺一不可：

      · **读取前确实没有副本行**（否则"读取后有了"这件事无从归因 ——
        可能它本来就在）；
      · 读取后**恰好新增 1 行**（差分，不写"表里有 N 行"）；
      · `materialize=False` 时**只读不写**（否则迁移与证据脚本会意外写库）。

    最后一条同时解释了 `materialize` 这个参数为什么必须存在：
    J1 要先断言"缺失"，而默认行为是"读到就补" —— 两条诉求只有一个参数能同时满足。
    """
    _seed_global(env)
    pid = env.project_id
    assert (
        _setting_rows(env, key=SETTING_KEY_BATCH_LIMITS, project_id=pid) == 0
    ), "前置：物化前不该有项目级行"

    # ① 只看不写：读数正确，但不产生任何行。
    read_only = _effective(env, project_id=pid, materialize=False)
    assert read_only.configured is True
    assert read_only.value == dict(DEFAULT_BATCH_MAX)
    assert read_only.source == SETTING_ORIGIN_GLOBAL, (
        "materialize=False 时应如实报告'值来自全局'，而不是伪装成已有副本"
    )
    assert (
        _setting_rows(env, key=SETTING_KEY_BATCH_LIMITS, project_id=pid) == 0
    ), "materialize=False 写库了 —— 迁移与证据脚本会意外产生数据"

    # ② 正常读：补出副本，且**来源变成 INHERITED**。
    materialized = _effective(env, project_id=pid)
    assert materialized.source == SETTING_ORIGIN_INHERITED
    assert materialized.value == dict(DEFAULT_BATCH_MAX)
    assert materialized.project_id == pid
    assert materialized.diverged is False

    row = _current_setting_row(
        env, key=SETTING_KEY_BATCH_LIMITS, project_id=pid
    )
    assert row is not None
    assert row["origin"] == SETTING_ORIGIN_INHERITED
    assert _setting_rows(env, key=SETTING_KEY_BATCH_LIMITS, project_id=pid) == 1

    # ③ 再读一次：幂等，不再新增行。
    again = _effective(env, project_id=pid)
    assert again.source == SETTING_ORIGIN_INHERITED
    assert _setting_rows(env, key=SETTING_KEY_BATCH_LIMITS, project_id=pid) == 1


def test_materialize_reads_back_after_insert(env: PlanningEnv) -> None:
    """钉住物化路径的**重读**这一步（实现里显式论证过的那一步）。

    `_materialize_inherited_copy` 在插入后**不信任自己构造的 ORM 对象**，
    而是按 `revision_no` 最大**重读**一次。为什么这条要单独有判据：

    | 写法 | 结果 |
    |---|---|
    | `return repo.insert_inherited_copy(...)`（不重读） | 拿到的是**未提交的**内存对象；若别人抢先插过而我的插入被拒，我返回的仍是"我以为的那一行" |
    | `return repo.current_project_setting(...)`（重读） | 拿到的是**本次事务的最终状态**，也就是真正生效的那一行 |

    这条判据的可观测形态：返回值必须**与库里那一行完全一致**（含
    `revision_no`）—— 构造对象时的 `revision_no` 是"我算的号"，
    重读拿到的是"实际生效的号"，两者在竞争下会不同。

    反向验证：把实现末尾的 `current_project_setting` 换成 `return row`
    （插入时那个对象），本判据在**无竞争**时仍会通过 —— 因此它
    配 `test_materialize_uses_locked_reread_under_contention` 一起用。
    """
    _seed_global(env)
    pid = env.project_id
    setting = _effective(env, project_id=pid)
    row = _current_setting_row(
        env, key=SETTING_KEY_BATCH_LIMITS, project_id=pid
    )
    assert row is not None
    assert setting.revision_no == row["revision_no"], (
        "返回的 revision_no 与库里那一行不一致 —— 说明返回的不是重读的结果"
    )
    assert setting.value == row["value"]


def test_materialize_respects_existing_manual_row(env: PlanningEnv) -> None:
    """已有 `MANUAL` 行时**不物化**（`project_row is None` 才补）。

    物化的条件是"**没有当前项目行**"，不是"`origin != MANUAL`"。
    写错的话，一次读取会把运营的覆盖"刷新"成全局值 ——
    而服务不报错，只是那个项目的上限悄悄变了（J4 失败的那一种）。
    """
    _seed_global(env)
    pid = env.project_id
    custom = {"TRIAL": 2, "SCALE": 30, "BULK": 50}
    with _repo(env) as repo:
        pl_settings.change_project_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            project_id=pid,
            value=custom,
            reason="覆盖",
            actor_id=env.owner_id,
        )
    before = _setting_rows(env, key=SETTING_KEY_BATCH_LIMITS, project_id=pid)

    setting = _effective(env, project_id=pid)
    assert setting.source == SETTING_ORIGIN_MANUAL
    assert setting.value == custom
    assert (
        _setting_rows(env, key=SETTING_KEY_BATCH_LIMITS, project_id=pid) == before
    ), "已有 MANUAL 行时读取仍然写了库（物化条件写成 origin != MANUAL 了）"


def test_get_effective_limits_rejects_unknown_key(env: PlanningEnv) -> None:
    """未知键 ⇒ 422，且带上合法取值（可读的排障信息）。"""
    with _repo(env) as repo:
        with pytest.raises(ApiError) as caught:
            pl_settings.get_effective_limits(repo, "no.such.key")
    assert caught.value.code == ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == REASON_SETTING_KEY_UNKNOWN


# ===========================================================================
# 2.6 J2 —— 全局变更后，**所有** INHERITED 项目都跟随
# ===========================================================================


def test_j2_all_inherited_projects_follow_global_change(env: PlanningEnv) -> None:
    """**J2**：全局变更后，每个 `INHERITED` 项目的生效值 == 新全局值。

    **穷举全部项目，不抽样**。理由：分叉的杀伤路径正是"某个项目被漏掉了，
    而没有任何人去点开它"。抽样判据对同一个缺陷的检出概率是"抽中它的概率"，
    而真实项目数是几十个 —— 抽不中的那次，缺陷就直接进生产了。

    因此断言的对象是 `repo.list_project_settings(key)`（`DISTINCT ON`
    取每个项目的当前行 ⇒ 结果集是**项目全集**），而不是"我造的那两个"。

    判据形态：**先把全集读出来，再逐个项目比对**，并额外断言
    "全集里确实包含我造的那两个项目" —— 否则一个空结果集会让本判据
    在"没有任何项目级行"时**恒真通过**（这是本项目最典型的一类假绿）。
    """
    _seed_global(env)
    # 让两个项目都先有副本（惰性物化的正常路径）。
    for pid in (env.project_id, env.other_project_id):
        assert _effective(env, project_id=pid).source == SETTING_ORIGIN_INHERITED

    new_value = {"TRIAL": 7, "SCALE": 21, "BULK": 42}
    with _repo(env) as repo:
        pl_settings.change_global_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            value=new_value,
            reason="收紧试产与放量上限",
            actor_id=env.admin_id,
        )

    with _repo(env) as repo:
        rows = repo.list_project_settings(key=SETTING_KEY_BATCH_LIMITS)
        global_row = repo.current_global_setting(key=SETTING_KEY_BATCH_LIMITS)

    assert global_row is not None and global_row.value == new_value
    seen = {row.project_id for row in rows}
    # 防"空集恒真"：我造的两个项目必须在全集里被看到。
    assert {env.project_id, env.other_project_id} <= seen, (
        "项目全集里看不到刚造的项目 —— 判据会退化成恒真"
    )
    # 穷举：每一个 INHERITED 项目都必须等于新全局值。
    for row in rows:
        assert row.origin == SETTING_ORIGIN_INHERITED, (
            f"项目 {row.project_id} 在全局变更后来源变成了 {row.origin!r}"
        )
        assert row.value == new_value, (
            f"项目 {row.project_id} 的生效值 {row.value} 未跟随新全局值 {new_value}"
        )

    # 读数层也要一致（服务层与仓储层不能各说各话）。
    for pid in (env.project_id, env.other_project_id):
        setting = _effective(env, project_id=pid)
        assert setting.value == new_value
        assert setting.source == SETTING_ORIGIN_INHERITED
        assert setting.global_value == new_value
        assert setting.diverged is False


def test_j2_backfills_project_that_never_had_a_row(env: PlanningEnv) -> None:
    """**J2 的边界**：从未被读取过的项目，也要在全局变更时拿到副本（§3.6.3 路径 c）。

    漏掉它的话："全局改到 V2 时项目 P 还没有行" ⇒ P 之后第一次读取时
    惰性物化，拿到 V2，**看起来也对**。但那是一次碰巧 —— 真正丢掉的是
    "P 在全局还是 V1 时创建"这个事实：`materialize=False` 的读取
    （管理侧总览、证据脚本）会看到 `source='GLOBAL'` 而不是 `INHERITED`，
    于是"每个项目都有一份标注了来源的副本"这条不变量在任何时序下都不成立。
    """
    _seed_global(env, value={"TRIAL": 10, "SCALE": 30, "BULK": 50})
    never_read = env.other_project_id
    assert (
        _setting_rows(env, key=SETTING_KEY_BATCH_LIMITS, project_id=never_read) == 0
    ), "前置：这个项目还没有任何项目级行"

    new_value = {"TRIAL": 5, "SCALE": 15, "BULK": 25}
    with _repo(env) as repo:
        pl_settings.change_global_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            value=new_value,
            reason="全局收紧",
            actor_id=env.admin_id,
        )

    row = _current_setting_row(
        env, key=SETTING_KEY_BATCH_LIMITS, project_id=never_read
    )
    assert row is not None, "从未被读取过的项目没有被补副本"
    assert row["origin"] == SETTING_ORIGIN_INHERITED
    assert row["value"] == new_value


def test_j2_cascade_uses_constant_number_of_selects(env: PlanningEnv) -> None:
    """**MAJOR-4**：级联刷新的 `SELECT` 条数与项目数**解耦**（不是 N+1）。

    ## 这条判据防的是一次"为性能改坏语义"

    初版在循环里逐项目 `current_project_setting` + `max_revision_no`
    ⇒ 每个项目 2 条 `SELECT`。实测本项目测试库 3835 个项目时，
    一次全局变更 **7671 条 `SELECT` / 1.09 s，且全程持着全局 advisory lock**
    —— 期间所有同 key 的写请求（项目覆盖、惰性物化）排队。

    MAJOR-4 的修复把它换成一次 `list_project_settings`（`DISTINCT ON`
    取每个项目的最大修订行）+ 预取号。**这条判据钉住"改完之后仍然是
    那一条批量查询"**：若将来有人为了修一个边缘 bug 把循环改回逐项目查询，
    这里必须报红。

    ## 为什么断言"条数"而不是"耗时"

    耗时在 CI 上会闪（同一函数 1.0s~1.4s 抖动），做成阈值判据必然假失败
    （项目铁律 #6：阈值型防线的阈值本身也要维护）。语句条数是离散的：
    **修复后恒为 2**（全局取号 1 条 + 项目当前行 1 条），与项目数无关。

    ## 与 J2 的分工

    `test_j2_all_inherited_projects_follow_global_change` 断言的是**结果**
    （每个项目都等于新全局值）；本条断言的是**代价**（用常数条查询做到）。
    只有前者 ⇒ "为了正确性把 N+1 加回来"不会被发现；
    只有后者 ⇒ "查得很快但漏刷了项目"不会被发现。两条互补。

    ## 反向验证

    把 `change_global_setting` 的批量查询换回循环里的
    `repo.current_project_setting(...)`，本判据必须报红
    （条数会变成 `2 + 2 × 项目数`，而判据要求 `select_runtime <= 3`）。
    """
    _seed_global(env)
    for pid in (env.project_id, env.other_project_id):
        assert _effective(env, project_id=pid).source == SETTING_ORIGIN_INHERITED

    new_value = {"TRIAL": 8, "SCALE": 28, "BULK": 48}

    def _do_change() -> None:
        with _repo(env) as repo:
            pl_settings.change_global_setting(
                repo,
                key=SETTING_KEY_BATCH_LIMITS,
                value=new_value,
                reason="量级联刷新的查询条数",
                actor_id=env.admin_id,
            )

    # 级联在 `_do_change()` 的**事务内部**枚举 `projects` 并逐个插副本行。
    # 为了给"没漏刷"一个**可判定**的期望值，项目数必须在**同一个窗口**内量 ——
    # 见断言 ③ 的「为什么期望值是一个区间」。
    #
    # ## 先保证项目数**足够**（2026-09-30 实测修正）
    #
    # 断言 ② 的形状是"`SELECT` 条数 < 项目数"。正确实现恒为 2 条，因此
    # 项目数 ≥ 3 时它才**有可能**通过。原先本用例默认"共享测试库里累积了
    # 几千个项目"（其他包的历史残留），于是它在共享库上碰巧成立 ——
    # **判据依赖了一个与本包无关、且不受本包控制的前提**。
    #
    # 独立临时库（干净库）实测原形报红，且报的是：
    #
    # ```
    # E  AssertionError: SELECT 条数 2 不小于项目数 2 —— 查询量仍在随项目数线性增长
    # E  assert 2 < 2
    # ```
    #
    # 这是**对正确实现报红**（假阳性），且措辞把"库里项目太少"误报成
    # "查询量线性增长" —— 排障者会去翻级联循环，而那里是对的。
    # 与 ISS-143 同族：判据无法区分"缺陷"与"前提不成立"。
    #
    # 处置：不足则**自己补造**，让判据在任何库上都有可判定的基准。
    _MIN_PROJECTS_FOR_DECOUPLING = 6
    _have = len(_raw(env, "SELECT id FROM projects"))
    for _ in range(max(0, _MIN_PROJECTS_FOR_DECOUPLING - _have)):
        _seed_project(env.settings, owner_id=env.owner_id)

    project_count_before = len(_raw(env, "SELECT id FROM projects"))
    counted = _measure_statements(env, _do_change)
    project_count_after = len(_raw(env, "SELECT id FROM projects"))

    assert project_count_before >= _MIN_PROJECTS_FOR_DECOUPLING, (
        f"前置：造项目后库里仍只有 {project_count_before} 个项目"
        f"（期望 ≥ {_MIN_PROJECTS_FOR_DECOUPLING}）—— 判据的基准不成立"
    )

    # ① 与项目数解耦：全局取号 + 项目当前行，共 2 条；留 1 条余量给
    #    将来可能新增的一次少量查询（例如"项目是否存在"）。
    assert counted["select_runtime"] <= 3, (
        f"级联刷新用了 {counted['select_runtime']} 条 runtime_settings SELECT"
        f"（项目数 {project_count_before}）—— 逐项目查询回来了，这是 N+1"
    )
    # ② `SELECT` 条数**小于项目数** ⇒ 不可能还在"每个项目查一次"。
    #    这一条比 ① 更难被"调大阈值"绕过：它同时随项目数变化。
    #    取两次读的**较小值**（更保守 ⇒ 更难通过），避免用漂移的较大值放松它。
    project_count_floor = min(project_count_before, project_count_after)
    assert counted["select_runtime"] < project_count_floor, (
        f"SELECT 条数 {counted['select_runtime']} 不小于项目数 {project_count_floor}"
        " —— 查询量仍在随项目数线性增长"
    )
    # ③ 项目副本数 == 级联枚举到的项目数：**没漏刷**的正面证据。
    #    行数 = 项目副本 + 1 行全局（`change_global_setting` 第一步也往本表插一行）。
    #    实测首次写成 `== project_count` 时报 `3848 != 3847` —— 差的就是那一行。
    #
    #    ## 为什么期望值是一个**区间**而不是一个数（2026-09-30 实测修正）
    #
    #    级联枚举 `projects` 发生在 `_do_change()` **事务内部**（read committed），
    #    而"全库项目数"是另外两次快照读出来的 —— **不是同一个时刻**。于是
    #    「恰好等于另一个时刻的全库计数」在**共享库**上根本不可判定：
    #    本包反向验证的完整版（期间另一进程正同跑全量 pytest）报
    #    `4632 != 4774`，差额 142。逐项核对定性：`4632 = 4631 + 1` ⇒
    #    **实现写的行数完全正确**，漂移全在右侧那次读。142 个项目是在级联
    #    持锁写 4631 行的约 7.5 秒窗口里由**并发进程**插进来的（本机实测
    #    ≈19 项目/秒）。这与 ISS-37/49/50/73 同族：**判据把"跨时刻的全库
    #    计数"当成了不变量**。
    #
    #    处置：把枚举值**夹在**测量前/后两次快照之间 —— 级联的快照必然落在
    #    这个窗口内，因此 `insert_runtime - 1 ∈ [before, after]` **严格成立**
    #    （不假设"项目只增"，用 min/max 两端）。独占库时 `before == after`，
    #    本条**退化为原来的等式，区分力不降**；共享库时窗口就是并发漂移的
    #    实测上界，而不是靠猜一个容差。
    #
    #    ⚠️ 残余风险（显式记下，不用一个好看的等式把它盖住）：并发写入量若
    #    超过该窗口，**少量**漏刷可能落在区间内而不报红。这条缺口由
    #    `test_j2_all_inherited_projects_follow_global_change` 的**穷举**独立
    #    兜底 —— 它逐个项目断言 `INHERITED` + 等于新全局值。本条的职责因此
    #    收敛为"量级正确（没有漏掉一片项目）"。
    lo, hi = sorted((project_count_before, project_count_after))
    assert counted["insert_runtime"] >= lo + 1, (
        f"级联只写了 {counted['insert_runtime']} 行，而窗口内至少有 {lo} 个项目"
        f" + 1 行全局 = {lo + 1} 行 —— 级联刷新漏掉了一片项目"
    )
    assert lo <= counted["insert_runtime"] - 1 <= hi, (
        f"级联写了 {counted['insert_runtime']} 行（扣掉全局行 = "
        f"{counted['insert_runtime'] - 1} 个项目副本），而测量窗口内的项目数是"
        f" [{lo}, {hi}] —— 副本数落在窗口外：要么漏刷了项目，要么多写了行"
    )

    # ④ 性能改动没有改坏语义：结果与逐项目路径一致。
    with _repo(env) as repo:
        rows = {row.project_id: row for row in repo.list_project_settings(
            key=SETTING_KEY_BATCH_LIMITS
        )}
    assert {env.project_id, env.other_project_id} <= set(rows), (
        "批量查询看不到刚造的项目 —— 判据会退化成恒真"
    )
    for pid in (env.project_id, env.other_project_id):
        row = rows[pid]
        assert row.origin == SETTING_ORIGIN_INHERITED
        assert row.value == new_value


def test_change_global_setting_appends_new_revision(env: PlanningEnv) -> None:
    """全局变更**追加新修订号**，不更新旧行（`runtime_settings` 不可变）。

    修订号是"1 起、逐次 +1"的连续数列（本项目 R9：排序键用 `revision_no`
    而不是 `created_at`，因为同一事务内插入的多行 `now()` 完全相同）。
    """
    _seed_global(env)
    first = _current_setting_row(
        env, key=SETTING_KEY_BATCH_LIMITS, project_id=None
    )
    assert first is not None

    with _repo(env) as repo:
        pl_settings.change_global_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            value={"TRIAL": 12, "SCALE": 30, "BULK": 50},
            reason="放宽试产",
            actor_id=env.admin_id,
        )

    second = _current_setting_row(
        env, key=SETTING_KEY_BATCH_LIMITS, project_id=None
    )
    assert second is not None
    assert second["revision_no"] == first["revision_no"] + 1
    assert second["value"] == {"TRIAL": 12, "SCALE": 30, "BULK": 50}
    # 旧行仍在（不可变 ⇒ 历史可查）。
    assert _setting_rows(env, key=SETTING_KEY_BATCH_LIMITS, project_id=None) >= 2


def test_change_global_setting_records_audit_fields(env: PlanningEnv) -> None:
    """变更行必须带上 **人 / 理由 / requestId**（ADR-13 的审计要求）。

    这三项是"配置被改了但没人知道是谁改的"这个状态的反面。
    `request_id` 用请求幂等键，把"配置变更"与"触发它的那次 HTTP 请求"
    关联起来 —— 排障时这是唯一能把两者接上的字段。
    """
    rid = f"req-{uuid.uuid4().hex[:12]}"
    with _repo(env) as repo:
        pl_settings.change_global_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            value=dict(DEFAULT_BATCH_MAX),
            reason="初始化默认值",
            actor_id=env.admin_id,
            request_id=rid,
        )
    rows = _raw(
        env,
        "SELECT actor_id, reason, request_id, origin FROM runtime_settings"
        " WHERE key = :k AND scope_kind = 'GLOBAL'"
        " ORDER BY revision_no DESC LIMIT 1",
        {"k": SETTING_KEY_BATCH_LIMITS},
    )
    assert rows
    actor_id, reason, request_id, origin = rows[0]
    assert actor_id == env.admin_id
    assert reason == "初始化默认值"
    assert request_id == rid
    assert origin == SETTING_ORIGIN_GLOBAL


# ===========================================================================
# 2.7 J3 —— 分叉必须**可见**；J4 —— 覆盖必须**稳定**
# ===========================================================================


def test_j3_manual_override_is_visible_and_marked_diverged(
    env: PlanningEnv,
) -> None:
    """**J3**：覆盖必须同时暴露 `source='MANUAL'` + `diverged=True` + `global_value`。

    三条缺一不可，缺任何一条分叉就只能靠人记得去看：

    | 缺哪条 | 后果 |
    |---|---|
    | `source` | 看到 5 条，不知道这是"本项目特有"还是"全局就是 5 条" |
    | `diverged` | 得自己拿两个值比 —— 而前端不会替它比 |
    | `global_value` | 知道分叉了，但不知道差在哪（无法渲染「本项目 5 · 全局 10」） |
    """
    _seed_global(env, value={"TRIAL": 10, "SCALE": 30, "BULK": 50})
    custom = {"TRIAL": 5, "SCALE": 30, "BULK": 50}
    with _repo(env) as repo:
        result = pl_settings.change_project_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            project_id=env.project_id,
            value=custom,
            reason="本项目试产期上限收紧到 5",
            actor_id=env.owner_id,
        )
    assert result.source == SETTING_ORIGIN_MANUAL
    assert result.diverged is True
    assert result.global_value == {"TRIAL": 10, "SCALE": 30, "BULK": 50}
    assert result.value == custom

    # 读路径同样三条齐备（不能只有写路径返回它们）。
    setting = _effective(env, project_id=env.project_id)
    assert setting.source == SETTING_ORIGIN_MANUAL
    assert setting.diverged is True
    assert setting.global_value == {"TRIAL": 10, "SCALE": 30, "BULK": 50}
    assert setting.value == custom


def test_j3_same_value_override_is_not_diverged(env: PlanningEnv) -> None:
    """`MANUAL` 但值**恰好等于**全局值时 `diverged=False`。

    `diverged` 的定义是"生效值 ≠ 全局值"，不是"有没有被覆盖过"。
    若把它写成"来源是不是 MANUAL"，前端会对一个值完全相同的项目
    显示"已覆盖"告警 —— 那是假告警，会训练使用者忽略这个标记
    （ISS-76 的同款形态：整体状态字段被列表筛选污染）。
    """
    value = {"TRIAL": 9, "SCALE": 30, "BULK": 50}
    _seed_global(env, value=value)
    with _repo(env) as repo:
        result = pl_settings.change_project_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            project_id=env.project_id,
            value=value,
            reason="与全局一致（不应报分叉）",
            actor_id=env.owner_id,
        )
    assert result.source == SETTING_ORIGIN_MANUAL
    assert result.diverged is False, "值相同却报了分叉 —— 假告警"


def test_j4_manual_override_survives_global_change(env: PlanningEnv) -> None:
    """**J4**：`MANUAL` 项目在全局变更后**保持自己的值**（覆盖的意义）。

    这是 J2 的对偶，也是 `origin` 列存在的全部理由 ——
    它必须与 `test_j2_all_inherited_projects_follow_global_change` 同时成立：

      · 只做 J2（无差别全刷）⇒ 本条 FAIL（覆盖被冲掉）；
      · 只做 J4（压根不刷）⇒ J2 FAIL（副本停在旧值）；
      · 两条都对 ⇒ "该跟随的跟随、该稳定的稳定"。

    实现里对应的就是级联循环里那句 `if current.origin == MANUAL: continue`。
    """
    _seed_global(env, value={"TRIAL": 10, "SCALE": 30, "BULK": 50})
    custom = {"TRIAL": 4, "SCALE": 30, "BULK": 50}
    with _repo(env) as repo:
        pl_settings.change_project_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            project_id=env.project_id,
            value=custom,
            reason="本项目上限特例",
            actor_id=env.owner_id,
        )
    # 另一个项目先拿到 INHERITED 副本（作为"该跟随"的对照）。
    assert (
        _effective(env, project_id=env.other_project_id).source
        == SETTING_ORIGIN_INHERITED
    )
    overridden_rows = _setting_rows(
        env, key=SETTING_KEY_BATCH_LIMITS, project_id=env.project_id
    )

    new_global = {"TRIAL": 20, "SCALE": 40, "BULK": 60}
    with _repo(env) as repo:
        pl_settings.change_global_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            value=new_global,
            reason="全局放宽",
            actor_id=env.admin_id,
        )

    # 被覆盖的项目：值不变、来源不变、行为上**没有新增行**。
    kept = _effective(env, project_id=env.project_id)
    assert kept.value == custom, "全局变更冲掉了本项目的覆盖（J4 失败）"
    assert kept.source == SETTING_ORIGIN_MANUAL
    assert kept.global_value == new_global, "覆盖项目的 globalValue 应始终是最新全局值"
    assert kept.diverged is True
    assert (
        _setting_rows(env, key=SETTING_KEY_BATCH_LIMITS, project_id=env.project_id)
        == overridden_rows
    ), "MANUAL 项目在级联刷新时被追加了新行 —— 那一句 origin 判断漏了"

    # 对照组：另一个项目**必须**跟随（否则"没被刷新"与"被跳过"分不清）。
    followed = _effective(env, project_id=env.other_project_id)
    assert followed.value == new_global
    assert followed.source == SETTING_ORIGIN_INHERITED


def test_list_diverged_projects_reports_only_real_divergence(
    env: PlanningEnv,
) -> None:
    """分叉清单：`MANUAL` + 值不同 ⇒ 在清单里；其余一律不在。

    **为什么必须有这个清单而不是前端逐个项目调读接口**：
    50 个项目 = 50 次请求，而"分叉了没人知道"的杀伤路径正是
    "没人会去逐个点开 50 个项目找差异"。一次查询给出"要看哪几个"。

    本判据同时覆盖两个反例：`INHERITED`（跟随者）不应出现；
    `MANUAL` 但值相同的也不应出现（否则清单会被噪音填满）。
    """
    _seed_global(env, value={"TRIAL": 10, "SCALE": 30, "BULK": 50})
    # 项目 A：真分叉
    with _repo(env) as repo:
        pl_settings.change_project_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            project_id=env.project_id,
            value={"TRIAL": 2, "SCALE": 30, "BULK": 50},
            reason="A 分叉",
            actor_id=env.owner_id,
        )
    # 项目 B：MANUAL 但值相同（不应出现）
    with _repo(env) as repo:
        pl_settings.change_project_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            project_id=env.other_project_id,
            value={"TRIAL": 10, "SCALE": 30, "BULK": 50},
            reason="B 与全局一致",
            actor_id=env.owner_id,
        )

    with _repo(env) as repo:
        diverged = pl_settings.list_diverged_projects(repo, SETTING_KEY_BATCH_LIMITS)

    by_project = {item.project_id: item for item in diverged}
    assert env.project_id in by_project, "真分叉的项目不在清单里"
    assert env.other_project_id not in by_project, (
        "值相同的 MANUAL 项目出现在分叉清单里 —— 清单会被噪音填满"
    )
    item = by_project[env.project_id]
    assert item.source == SETTING_ORIGIN_MANUAL
    assert item.value == {"TRIAL": 2, "SCALE": 30, "BULK": 50}
    assert item.global_value == {"TRIAL": 10, "SCALE": 30, "BULK": 50}
    assert item.revision_no >= 1


def test_list_diverged_projects_is_empty_when_everyone_follows(
    env: PlanningEnv,
) -> None:
    """全部 `INHERITED` ⇒ 清单为空。

    这条判据的另一半价值是**证明上一条不是恒真**：如果 `list_diverged_projects`
    被写成"把所有项目行都返回"，上一条只会看到"A 在里面"就通过，
    而这条会在"全跟随"时仍然返回一堆行 —— 于是两条一起把实现夹住。
    """
    _seed_global(env)
    assert (
        _effective(env, project_id=env.project_id).source
        == SETTING_ORIGIN_INHERITED
    )
    with _repo(env) as repo:
        diverged = pl_settings.list_diverged_projects(repo, SETTING_KEY_BATCH_LIMITS)
    assert diverged == [], f"全跟随状态下清单非空：{diverged}"


def test_change_project_setting_rejects_missing_project(
    env: PlanningEnv,
) -> None:
    """项目不存在 ⇒ 404（而不是把 FK 违反变成 500）。"""
    with _repo(env) as repo:
        with pytest.raises(ApiError) as caught:
            pl_settings.change_project_setting(
                repo,
                key=SETTING_KEY_BATCH_LIMITS,
                project_id=uuid.uuid4(),
                value=dict(DEFAULT_BATCH_MAX),
                reason="幽灵项目",
                actor_id=env.owner_id,
            )
    assert caught.value.code == ErrorCode.RESOURCE_NOT_FOUND
    assert caught.value.details["reason"] == REASON_PROJECT_NOT_FOUND


def test_get_effective_limits_rejects_missing_project(env: PlanningEnv) -> None:
    """★ MAJOR-3 的判据：**读取**路径也必须拒"项目不存在"，给 404 而非 500。

    ## 为什么这条必须有（它此前不存在）

    写入侧一直校验（上一条用例），读取侧却漏了。不校验时的实际后果是：

        一个不存在的 project_id 走到惰性物化 → 插一行 runtime_settings
        → 外键 `ON DELETE RESTRICT` 拒绝（23503）
        → `_is_setting_revision_conflict` 只认那两条**部分唯一索引**的名字、
          不认 FK ⇒ 原样抛出 ⇒ **500**

    注意触发条件是 `materialize=True` **且有全局行**：没有全局行时
    根本不会走到物化。因此这条用例先 `bootstrap_defaults` 造出全局行，
    否则测的是另一条分支（"全局无值 ⇒ configured=False"），
    那样即便把修复回滚掉，用例也会通过 —— **判据没有区分力**。
    """
    with _repo(env) as repo:
        pl_settings.bootstrap_defaults(repo)
        repo.flush()

    ghost = uuid.uuid4()
    with _repo(env) as repo:
        with pytest.raises(ApiError) as caught:
            pl_settings.get_effective_limits(repo, SETTING_KEY_BATCH_LIMITS, ghost)
    assert caught.value.code == ErrorCode.RESOURCE_NOT_FOUND
    assert caught.value.details["reason"] == REASON_PROJECT_NOT_FOUND
    assert caught.value.details["projectId"] == str(ghost)

    # 反向验证：同一个幽灵项目在**关掉物化**时也必须是 404。
    # 若校验写在"物化分支内部"，这一路就会漏（`materialize=False` 不走物化）
    # ⇒ 修复就成了"只堵住其中一个入口"。
    with _repo(env) as repo:
        with pytest.raises(ApiError) as caught2:
            pl_settings.get_effective_limits(
                repo, SETTING_KEY_BATCH_LIMITS, ghost, materialize=False
            )
    assert caught2.value.details["reason"] == REASON_PROJECT_NOT_FOUND


def test_change_setting_rejects_missing_actor(env: PlanningEnv) -> None:
    """`actor_id` 不存在 ⇒ 422（可读的字段错误，而不是 FK 违反的 500）。

    审计字段的 FK 是最后一道网；服务层先判一次是为了给出**能指导修正**的
    错误（"哪个字段错了"），而不是让排障的人去比对约束名。
    """
    ghost = uuid.uuid4()
    with _repo(env) as repo:
        with pytest.raises(ApiError) as caught:
            pl_settings.change_global_setting(
                repo,
                key=SETTING_KEY_BATCH_LIMITS,
                value=dict(DEFAULT_BATCH_MAX),
                reason="操作人不存在",
                actor_id=ghost,
            )
    assert caught.value.code == ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == REASON_ACTOR_NOT_FOUND

    with _repo(env) as repo:
        with pytest.raises(ApiError) as caught:
            pl_settings.change_project_setting(
                repo,
                key=SETTING_KEY_BATCH_LIMITS,
                project_id=env.project_id,
                value=dict(DEFAULT_BATCH_MAX),
                reason="操作人不存在",
                actor_id=ghost,
            )
    assert caught.value.details["reason"] == REASON_ACTOR_NOT_FOUND


# ===========================================================================
# 2.8 变更理由与取值形状（ADR-13 的审计前置）
# ===========================================================================


@pytest.mark.parametrize("bad_reason", [None, "", "   ", "\n\t "])
def test_change_requires_reason(
    env: PlanningEnv, bad_reason: str | None
) -> None:
    """理由为 **NULL / 空串 / 纯空白** ⇒ 422。

    ⚠️ **DDL 那条 `reason_not_blank` 拦不住 `NULL`** —— 它只拦空串，
    而"没传理由"在 SQL 里是 `NULL`（约束放行）。因此这一层必须在服务层。
    本判据的三个取值分别对应三种到达方式：字段缺失（`None`）、
    前端传了空串（`""`）、用户按了一串空格（`"   "`）。

    反向验证：删掉 `_require_reason` 的 `reason is None or not reason.strip()`
    里的 `not reason.strip()` 那一半，`""`/`"   "` 两条必须 FAIL。
    """
    with _repo(env) as repo:
        with pytest.raises(ApiError) as caught:
            pl_settings.change_global_setting(
                repo,
                key=SETTING_KEY_BATCH_LIMITS,
                value=dict(DEFAULT_BATCH_MAX),
                reason=bad_reason,  # type: ignore[arg-type]
                actor_id=env.admin_id,
            )
    assert caught.value.code == ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == REASON_CHANGE_REASON_REQUIRED


def test_change_rejects_overlong_reason(env: PlanningEnv) -> None:
    """理由超过 `MAX_CHANGE_REASON_CHARS` ⇒ 422（与 DDL 的 varchar 上限同源）。"""
    too_long = "理" * (MAX_CHANGE_REASON_CHARS + 1)
    with _repo(env) as repo:
        with pytest.raises(ApiError) as caught:
            pl_settings.change_global_setting(
                repo,
                key=SETTING_KEY_BATCH_LIMITS,
                value=dict(DEFAULT_BATCH_MAX),
                reason=too_long,
                actor_id=env.admin_id,
            )
    assert caught.value.details["reason"] == REASON_CHANGE_REASON_REQUIRED

    # 边界内必须通过（否则这条校验变成"全拒"，判据会假绿）。
    with _repo(env) as repo:
        pl_settings.change_global_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            value=dict(DEFAULT_BATCH_MAX),
            reason="理" * MAX_CHANGE_REASON_CHARS,
            actor_id=env.admin_id,
        )


def test_validate_batch_limits_requires_all_kinds() -> None:
    """`batch.limits` 缺档 ⇒ 422，且 details 里点名缺了哪一档。

    缺档若被静默放行，表现为"那一档用默认值"而**不报错** ——
    运营以为把放量上限调成 20 了，实际它一直是 30。

    反向验证：把 `missing` 的构造改成"恒为空列表"，本判据必须 FAIL。
    """
    with pytest.raises(ApiError) as caught:
        pl_settings.validate_setting_value(
            SETTING_KEY_BATCH_LIMITS, {"TRIAL": 10, "SCALE": 30}
        )
    assert caught.value.code == ErrorCode.VALIDATION_FAILED
    assert caught.value.details["reason"] == REASON_SETTING_VALUE_INVALID
    assert caught.value.details["missing"] == [BATCH_KIND_BULK]


def test_validate_batch_limits_rejects_unknown_kind() -> None:
    """多出未知档 ⇒ 422，且点名多的是哪一个。

    多出来的档位是被**忽略**还是被**拒绝**，是本包的一处刻意选择：
    `validate_setting_value` 对"多余键"的通用立场是忽略（读历史行时
    可能有旧版本的额外键），但 `batch.limits` 的档位是**枚举**，
    多一个说明调用方认错了键（比如把 `TRIAL` 写成 `PILOT`）——
    忽略它会让那一档悄悄用默认值。
    """
    with pytest.raises(ApiError) as caught:
        pl_settings.validate_setting_value(
            SETTING_KEY_BATCH_LIMITS,
            {"TRIAL": 10, "SCALE": 30, "BULK": 50, "PILOT": 5},
        )
    assert caught.value.details["reason"] == REASON_BATCH_KIND_UNKNOWN
    assert caught.value.details["unknown"] == ["PILOT"]


@pytest.mark.parametrize("bad", [0, -1, MAX_QUANTITY_BOUND + 1])
def test_validate_batch_limits_range(bad: int) -> None:
    """档位上界必须落在 `[MIN_QUANTITY, MAX_QUANTITY_BOUND]` 内。"""
    with pytest.raises(ApiError) as caught:
        pl_settings.validate_setting_value(
            SETTING_KEY_BATCH_LIMITS, {"TRIAL": bad, "SCALE": 30, "BULK": 50}
        )
    assert caught.value.details["reason"] == REASON_SETTING_VALUE_INVALID


def test_validate_batch_limits_rejects_bool_value() -> None:
    """**`bool` 是 `int` 的子类** —— `True` 不显式排除会被当成 1 通过。

    这条与草稿总量的 `test_save_draft_rejects_bool_total` 是**同一个坑的
    两处落脚点**：一处漏了，另一处会被"看起来通过"掩盖。
    """
    with pytest.raises(ApiError) as caught:
        pl_settings.validate_setting_value(
            SETTING_KEY_BATCH_LIMITS, {"TRIAL": True, "SCALE": 30, "BULK": 50}
        )
    assert caught.value.details["reason"] == REASON_SETTING_VALUE_INVALID


def test_validate_setting_value_returns_fresh_dict_and_rejects_extra_kind() -> None:
    """`batch.limits` 的多余档位 ⇒ **报错**（不是忽略）；两个分支都返回**新 dict**。

    ⚠️ 本条一开始写成"多余键一律忽略"，**当场报红** ——
    那是我把**通用分支**的宽容误当成了整个函数的行为。
    `validate_setting_value` 按 key 分派，两个分支的立场**故意不同**：

    | 分支 | 多余键 | 为什么是这个立场 |
    |---|---|---|
    | `batch.limits` | **422**（`REASON_BATCH_KIND_UNKNOWN`） | 三档是**闭集**。放行等于让 `{"TRIAL":10,"SCALE":30,"BULK":50,"BULK2":50}` 这类手滑静默多出一个档位，而读取方只认前三档 ⇒ "改了不生效"且不报错 |
    | 通用键（OP-64 形状未定） | **保留**（形状校验通过即可） | 现在锁死键集会让将来实测值落不下来；这一层能守的只有"值是数或 null" |

    **两者唯一的共同点**：返回**新对象**，不把调用方那个 dict 原样存进 JSONB
    （`runtime_settings` 不可变，"存进去的垃圾清不掉"）。
    这条差异也正是"每个 key 一个校验分支"（R8 的处置）要付的代价：
    口径多了，容易被记成一条。
    """
    # ① batch.limits：多余档位不是"忽略"，是报错。
    with pytest.raises(ApiError) as caught:
        pl_settings.validate_setting_value(
            SETTING_KEY_BATCH_LIMITS,
            {"TRIAL": 10, "SCALE": 30, "BULK": 50, "note": "顺手带的"},
        )
    assert caught.value.details["reason"] == REASON_BATCH_KIND_UNKNOWN
    assert caught.value.details["unknown"] == ["note"]

    # ② 合法输入 ⇒ 三档齐全、顺序归一（sorted），且**不是同一个对象**。
    source = {"TRIAL": 10, "SCALE": 30, "BULK": 50}
    normalized = pl_settings.validate_setting_value(
        SETTING_KEY_BATCH_LIMITS, source
    )
    assert normalized == {"TRIAL": 10, "SCALE": 30, "BULK": 50}
    assert normalized is not source, "返回了原对象 —— 调用方后续改动会污染落库值"

    # ③ 通用键：未列举的键**保留**（形状未定，不能锁死），值仍受形状约束。
    generic = pl_settings.validate_setting_value(
        SETTING_KEY_RESEARCH_MAX_FILES,
        {"maxEffectiveFiles": 2000, "futureKey": 5},
    )
    assert generic == {"maxEffectiveFiles": 2000, "futureKey": 5}
    with pytest.raises(ApiError) as caught:
        pl_settings.validate_setting_value(
            SETTING_KEY_RESEARCH_MAX_FILES, {"futureKey": "两千"}
        )
    assert caught.value.details["reason"] == REASON_SETTING_VALUE_INVALID


def test_placeholder_key_allows_none_but_others_do_not() -> None:
    """`None`（清空）**只有占位键允许**；非占位键清空 ⇒ 422。

    理由（`types.PLACEHOLDER_SETTING_KEYS`）：占位键的合法状态就是
    "没有值"（OP-64 实测值未到）；而 `batch.limits` 被清空意味着
    "批次数上限不存在了"—— 下游会退化成某个默认值，且**不报错**。
    """
    assert pl_settings.validate_setting_value(
        SETTING_KEY_PLATFORM_MEDIA_LIMITS, None
    ) == {}
    with pytest.raises(ApiError) as caught:
        pl_settings.validate_setting_value(SETTING_KEY_BATCH_LIMITS, None)
    assert caught.value.details["reason"] == REASON_SETTING_VALUE_INVALID


def test_validate_setting_value_rejects_non_mapping() -> None:
    """非对象值 ⇒ 422（消息里带上实际类型，便于排障）。"""
    with pytest.raises(ApiError) as caught:
        pl_settings.validate_setting_value(
            SETTING_KEY_BATCH_LIMITS, [10, 30, 50]  # type: ignore[arg-type]
        )
    assert caught.value.details["reason"] == REASON_SETTING_VALUE_INVALID


def test_positive_int_map_accepts_nested_nulls() -> None:
    """通用形状里 `null` 是**合法值**（"有值但值为空"），`bool` 不是。

    这是 `configured` 不能退化成 `value is None` 的另一面：
    `{"maxEffectiveFiles": null}` 是"已配置、值为空"，与"未配置"不同。
    """
    assert pl_settings.validate_setting_value(
        SETTING_KEY_RESEARCH_MAX_FILES, {"maxEffectiveFiles": None}
    ) == {"maxEffectiveFiles": None}
    assert pl_settings.validate_setting_value(
        SETTING_KEY_RESEARCH_MAX_FILES, {"maxEffectiveFiles": 2000}
    ) == {"maxEffectiveFiles": 2000}

    for bad in (True, "两千", [1]):
        with pytest.raises(ApiError) as caught:
            pl_settings.validate_setting_value(
                SETTING_KEY_RESEARCH_MAX_FILES, {"maxEffectiveFiles": bad}
            )
        assert caught.value.details["reason"] == REASON_SETTING_VALUE_INVALID


def test_effective_setting_as_limits_roundtrip(env: PlanningEnv) -> None:
    """`EffectiveSetting.as_limits()` 解析出与 `DEFAULT_BATCH_MAX` 一致的档位。

    **未配置时抛错而不是回落默认值**：回落会让"读不到配置"表现为
    "用了 10/30/50"且不报错 —— 于是"我改的值为什么没生效"无从查起。
    """
    _seed_global(env)
    limits: BatchLimits = _effective(env).as_limits()
    for kind, expected in DEFAULT_BATCH_MAX.items():
        assert limits.max_for(kind) == expected

    unconfigured = _effective(env, key=SETTING_KEY_PLATFORM_MEDIA_LIMITS)
    with pytest.raises(ValueError):
        unconfigured.as_limits()


# ===========================================================================
# 第 3 层：数据库约束（原生 SQL，绕过 ORM）
# ===========================================================================
#
# ## 为什么这一层必须存在（而不是"第 2 层已经覆盖了"）
#
# 第 2 层走服务层与 ORM，ORM 会**替调用方做很多事**：类型转换、默认值填充、
# 会话内的 flush 顺序、甚至把非法值在 Python 侧先拦住。于是"约束到底存不存在"
# 在那一层**看不见** —— 一条被删掉的 CHECK 不会让第 2 层变红，
# 因为非法输入在到达数据库之前就被 pydantic / 服务层校验拒了。
#
# 本层的做法是**用原生 SQL 直接捅数据库**（完全绕开 ORM 与 pydantic）。
# 它回答的问题只有一个：**数据库自己会不会拦**。
#
# 这一层与第 1 层 §1.4（DDL 文本自洽）互补：那边检查"SQL 文本写对了吗"，
# 这边检查"写进去的 SQL 真的生效了吗"。文本对而执行不生效的唯一常见形态
# 就是**部分唯一索引的 `WHERE` 写错**（静默失效 —— 建索引不报错，
# 只是不再约束），因此本层为它专门做了**双向活性验证**。
#
# ## ★ 本层的一条实测纪律：CHECK 约束之间存在**蕴含关系**，归因不精确
#
# 同步断言 `diag.constraint_name` 看起来很严谨，但实测有两条例外
# （本机 PostgreSQL 实测，见 `test_runtime_settings_check_scope_kind_and_origin_*`）：
#
# | 反例 | 理论应命中 | **实测命中** |
# |---|---|---|
# | `scope_kind='TENANT'` | `ck_.._scope_kind_valid` | `ck_.._origin_matches_scope` |
# | `origin='COPIED'` | `ck_.._origin_valid` | `ck_.._origin_matches_scope` |
#
# 原因是 `origin_matches_scope` 把 `scope_kind` 与 `origin` 的**全部合法组合**
# 枚举成了一个析取式，任何一个取值不在枚举里都会让它先失败 ——
# PostgreSQL 按约束的检查顺序报告，而它不保证按名字顺序。
#
# 因此本层对这两条**只断言 SQLSTATE `23514`、不断言名字**，
# 并在用例 docstring 里写明原因。若强行断言名字，将来 PG 改了检查顺序
# 就会**假失败** —— 而假失败最终会被人关掉，那比没有断言更糟（ISS-95）。
#
# ---
# 3.0 不可变性（表级性质）
# 3.1 部分唯一索引的双向活性（本层最要紧的一节）
# 3.2 `runtime_settings` 的 8 条 CHECK
# 3.3 `plan_drafts` 的 7 条 CHECK
# ---------------------------------------------------------------------------


# --- 3.0 不可变性 ----------------------------------------------------------


@pytest.mark.regression
def test_runtime_settings_rejects_update_and_delete(env: PlanningEnv) -> None:
    """`runtime_settings` 是**追加式不可变**表：UPDATE / DELETE 都必须被拒。

    这是 ADR-13 验收条件"审计不可篡改"的**唯一实施点**。
    它与"审计完整"（`GET .../history` 能读出来）是一对：
    完整性靠读取出口，不可篡改性靠这里。

    ## 为什么必须用原生 SQL 测

    服务层**没有任何** UPDATE / DELETE `runtime_settings` 的代码路径，
    因此 ORM 层测不出来 —— 一条被删掉的触发器不会让任何服务层用例变红。
    而"不可篡改性消失"的后果是**静默的**：一切照常工作，
    只是历史行可以被改掉，而没人会知道。

    ## 为什么 `reason` 改成同一个值也拦

    触发器是 `BEFORE UPDATE OR DELETE`（行级），它**不看改了什么** ——
    连"改成完全相同的值"也拒。这是刻意的（见 DDL 里的说明）：
    若允许"无害 UPDATE"，那么"哪些 UPDATE 算无害"就成了第二个判定点，
    而那个判定点没有判据守着。

    ## 实测的两个 SQLSTATE 都是 `23001`

    `IMMUTABLE_RECORD_SQLSTATE`，且消息里带中文"不可变记录"。
    断言消息是为了区分"**本表的**触发器拒绝"与"别的表/别的触发器拒绝"
    —— `23001` 是通用 SQLSTATE，单看它不足以定位。
    """
    _seed_global(env)
    # 取一条真实存在的行（不写死修订号 —— 见 `_next_revision` 的说明）。
    revision = _scalar(
        env,
        "SELECT max(revision_no) FROM runtime_settings WHERE scope_kind = 'GLOBAL'",
    )
    assert revision is not None, "前置失败：`_seed_global` 没有插进任何行"

    update_error = _raw_expect_error(
        env,
        "UPDATE runtime_settings SET reason = '被改过的理由'"
        " WHERE scope_kind = 'GLOBAL' AND revision_no = :rev",
        {"rev": int(revision)},
    )
    assert _sqlstate(update_error) == IMMUTABLE_RECORD_SQLSTATE, (
        f"UPDATE 未被不可变触发器拒绝。SQLSTATE={_sqlstate(update_error)!r}，"
        f"消息={_primary(update_error)!r}"
    )
    assert "不可变记录" in _primary(update_error)

    delete_error = _raw_expect_error(
        env,
        "DELETE FROM runtime_settings"
        " WHERE scope_kind = 'GLOBAL' AND revision_no = :rev",
        {"rev": int(revision)},
    )
    assert _sqlstate(delete_error) == IMMUTABLE_RECORD_SQLSTATE
    assert "不可变记录" in _primary(delete_error)

    # **行还在**：上面两条都失败 ⇒ 必须验证失败确实回滚了（不是"报错但改了"）。
    remaining = _setting_rows(env, key=SETTING_KEY_BATCH_LIMITS, project_id=None)
    assert remaining == 1, (
        f"拒绝之后行数变成了 {remaining}（期望 1）—— "
        "UPDATE/DELETE 在抛错之前已生效，触发器没有真正拦住"
    )


def test_plan_drafts_is_mutable(env: PlanningEnv) -> None:
    """`plan_drafts` **可以** UPDATE —— 与 `runtime_settings` 形成对照。

    ## 为什么需要这条"应该成功"的用例

    上面那条测的是"必须被拒"。若某个实现把不可变触发器**误挂在两张表上**，
    上面那条仍然全绿（两张表都被拒），而草稿保存会全线 500。
    "该拦的拦住了"与"该放的放行了"是**两个方向**，缺一半就等于没测。

    这里用原生 SQL 直接改 `state`（服务层不允许这么改，但表本身允许 ——
    `update_draft` 走的就是 UPDATE）。
    """
    draft_id = _insert_plan_draft(env)
    before = _draft_schema_version(env, draft_id)
    assert before == 1, (
        f"新建草稿的 schema_version 期望 1（DDL 的 server_default），实际 {before}"
    )
    _raw(
        env,
        "UPDATE plan_drafts SET schema_version = schema_version + 1 WHERE id = :did",
        {"did": str(draft_id)},
    )
    assert _draft_schema_version(env, draft_id) == before + 1


# --- 3.1 部分唯一索引的双向活性 --------------------------------------------


@pytest.mark.regression
def test_partial_unique_index_global_rejects_duplicate_revision(
    env: PlanningEnv,
) -> None:
    """正向活性：同 `(key, revision_no)` 的第二条 GLOBAL 行 ⇒ `23505`。

    这是"当前生效值"的**结构基础**：没有它，同一个 `(batch.limits, 1)`
    可以有任意多行，而"哪个是当前值"只能靠 `max(revision_no)` 猜 ——
    平局时任取一行，且**不报错**。

    ## 为什么断言的是 `diag.constraint_name` 而不是消息文本

    消息文本（`duplicate key value violates unique constraint "..."`）会随
    PostgreSQL 版本与 locale 变。`diag.constraint_name` 是结构化字段，
    本机实测 `psycopg 3.3.5` 可用。

    ## 为什么先插一条"同 key 但另一个修订号"

    只测"第二次同键插入失败"是有歧义的：若索引的键里**多了** `project_id`
    （或任何恒为 NULL 的列），所有行都会因 `NULL != NULL` 而"互不冲突"，
    用例虽然能过（第二次插入成功 ⇒ 断言失败报红），但**报红原因指向错误**。
    这里插入的中间那条为了让"键里确实含 `revision_no`"也被看见：
    同 key 不同 `revision_no` **必须成功**。
    """
    base_revision = _next_revision(env)
    _raw(
        env,
        "INSERT INTO runtime_settings"
        " (key, scope_kind, project_id, value, revision_no, origin, reason)"
        " VALUES (:k, 'GLOBAL', NULL, CAST(:v AS jsonb), :rev, 'GLOBAL', :reason)",
        {
            "k": SETTING_KEY_BATCH_LIMITS,
            "v": json.dumps(dict(DEFAULT_BATCH_MAX)),
            "rev": base_revision,
            "reason": "3.1 正向活性",
        },
    )

    # 同 key **不同修订号** ⇒ 必须成功（验证键里真的含 revision_no）。
    _raw(
        env,
        "INSERT INTO runtime_settings"
        " (key, scope_kind, project_id, value, revision_no, origin, reason)"
        " VALUES (:k, 'GLOBAL', NULL, CAST(:v AS jsonb), :rev, 'GLOBAL', :reason)",
        {
            "k": SETTING_KEY_BATCH_LIMITS,
            "v": json.dumps(dict(DEFAULT_BATCH_MAX)),
            "rev": base_revision + 1,
            "reason": "3.1 正向活性（另一个修订号）",
        },
    )

    # 同 key **同**修订号再来一次 ⇒ 必须被 `uq_runtime_settings_global` 拒。
    error = _raw_expect_error(
        env,
        "INSERT INTO runtime_settings"
        " (key, scope_kind, project_id, value, revision_no, origin, reason)"
        " VALUES (:k, 'GLOBAL', NULL, CAST(:v AS jsonb), :rev, 'GLOBAL', :reason)",
        {
            "k": SETTING_KEY_BATCH_LIMITS,
            "v": json.dumps(dict(DEFAULT_BATCH_MAX)),
            "rev": base_revision,
            "reason": "3.1 正向活性（重复）",
        },
    )
    assert _sqlstate(error) == pl_ddl.UNIQUE_VIOLATION_SQLSTATE, (
        f"重复的 (key, revision_no) 未被唯一索引拒绝。"
        f"SQLSTATE={_sqlstate(error)!r}，消息={_primary(error)!r}"
    )
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == "uq_runtime_settings_global", (
        "命中的不是 uq_runtime_settings_global —— 说明另有约束（或名字漂移）在起作用，"
        "而本条判据要钉的正是这条部分唯一索引本身"
    )


@pytest.mark.regression
def test_partial_unique_index_project_rejects_duplicate_revision(
    env: PlanningEnv,
) -> None:
    """正向活性：同 `(key, project_id, revision_no)` 的第二条 PROJECT 行 ⇒ `23505`。

    `project_id` 在这里**不是跳过的列** —— 它是键的一部分（与全局索引的区别）。
    这正是两条索引不能合并成一条的原因：全局行的 `project_id IS NULL`
    会让键里的 `project_id` 退化成恒 NULL，于是 `NULL != NULL`
    使得"同一个全局键可以无限重复"。

    ⚠️ 本用例的第一版**漏了这一步**：只插了一条 PROJECT 行就断言
    "第二条会被拒"，而第二条根本没插 —— 用例通过但什么都没验证。
    现在显式插两次，并把"不同项目同修订号必须成功"也放进来，
    让"键里含 `project_id`"成为一个**被验证的事实**而非假设。
    """
    revision = _next_revision(env)
    insert_sql = (
        "INSERT INTO runtime_settings"
        " (key, scope_kind, project_id, value, revision_no, origin, reason)"
        " VALUES (:k, 'PROJECT', :pid, CAST(:v AS jsonb), :rev, 'INHERITED', :reason)"
    )
    params = {
        "k": SETTING_KEY_BATCH_LIMITS,
        "v": json.dumps(dict(DEFAULT_BATCH_MAX)),
        "rev": revision,
        "reason": "3.1 项目索引正向活性",
    }

    _raw(env, insert_sql, {**params, "pid": str(env.project_id)})

    # 同项目 **不同修订号** ⇒ 成功（键里含 revision_no）。
    _raw(env, insert_sql, {**params, "pid": str(env.project_id), "rev": revision + 1})

    # **另一个项目**、同修订号 ⇒ 成功（键里含 project_id，项目之间互不干扰。
    # 这一条同时是全局索引的反向验证：若 `uq_runtime_settings_global` 的
    # `WHERE` 变成永真，它会把这条也拦下 —— 那时本行会失败并指向全局索引）。
    _raw(env, insert_sql, {**params, "pid": str(env.other_project_id)})

    # 同项目 **同**修订号 ⇒ 必须被 `uq_runtime_settings_project` 拒。
    error = _raw_expect_error(
        env, insert_sql, {**params, "pid": str(env.project_id)}
    )
    assert _sqlstate(error) == pl_ddl.UNIQUE_VIOLATION_SQLSTATE
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == "uq_runtime_settings_project"


def test_both_partial_indexes_exist_in_pg_indexes_not_pg_constraint(
    env: PlanningEnv,
) -> None:
    """★ 两条部分唯一索引**只**出现在 `pg_indexes` 里，不在 `pg_constraint` 里。

    ## 为什么必须单独测这件事

    部分唯一索引**不是约束**（`CREATE UNIQUE INDEX ... WHERE ...`），
    因此 PostgreSQL **不会**把它登记进 `pg_constraint`。
    本机实测：`runtime_settings` 的 `contype='u'` 条数为 **0**。

    后果是：任何"查 `pg_constraint` 来验证唯一性存在"的判据
    **永远查不到这两条索引**，而它会以两种方式骗人：

    | 写法 | 结果 |
    |---|---|
    | `assert name in {pg_constraint 的名字}` | **恒假** ⇒ 报红，但原因指向错误（让人以为索引没建） |
    | `assert 没有多余的唯一约束` | **恒真** ⇒ 静默通过，覆盖为零 |

    因此本仓的 `ddl.py` 用 `pg_indexes.indexdef` 作为真值来源，
    并用 `_normalize_sql` 处理 PG 回写的括号（§1.4 已测）。

    ## 这条断言的价值

    它把"**必须查 `pg_indexes`**"这个事实**钉死在测试里**。
    若哪天有人把索引改成表级 `UNIQUE` 约束（形态变了但"唯一性"仍在），
    本用例报红 —— 那时要重新审视：全局行在普通 UNIQUE 下会
    因 `NULL != NULL` 而**完全不生效**，是个真缺陷，不是等价重构。
    """
    with session_scope(env.settings) as session:
        unique_constraints = session.execute(
            text(
                "SELECT count(*) FROM pg_constraint"
                " WHERE conrelid = 'runtime_settings'::regclass AND contype = 'u'"
            )
        ).scalar_one()
    assert int(unique_constraints) == 0, (
        "runtime_settings 上出现了 UNIQUE 型 pg_constraint —— "
        "本包的唯一性必须由两条部分唯一索引提供（见 ddl.PARTIAL_UNIQUE_INDEXES）"
    )

    with session_scope(env.settings) as session:
        rows = session.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes"
                " WHERE tablename = 'runtime_settings'"
                " AND indexname IN ('uq_runtime_settings_global',"
                " 'uq_runtime_settings_project')"
            )
        ).fetchall()
    assert {row[0] for row in rows} == set(pl_ddl.partial_unique_index_names())
    # `indexdef` 里必须真的带 `WHERE`（不带就是索引建错了形态）。
    for name, definition in rows:
        assert "WHERE" in definition, f"{name} 没有被建成部分索引：{definition}"

    # 且那个 `WHERE` 必须**真的是作用域过滤**：含 `scope_kind` 与对应的取值。
    #
    # ⚠️ 这里刻意**不**做"indexdef 的 WHERE 与注册字符串等价"的比对。
    # 第一版就是这么写的，**实测报红**（本用例的真实失败）：
    #
    #     PG 回写：WHERE ((scope_kind)::text = 'GLOBAL'::text)
    #     注册值：WHERE scope_kind = 'GLOBAL'
    #
    # 两者**字面永不相等**，而且 `_normalize_sql` 刻意不把它们归一
    # （§1.4 已测：它只剥"包着整个字符串的单层括号"，`((a)::text = 'b'::text)`
    # 不在其列 —— 因为要正确剥掉它就得理解 `::text` 的语义，
    # 那是 SQL 解析器的事，不是正则的事）。
    #
    # 因此那版断言是**结构性假失败**：不是索引有问题，是判据要求了两个
    # 本该不同的东西相等。假失败的最终命运是被注释掉（ISS-95），
    # 于是"WHERE 有没有退化成永真"就彻底没人守了。
    #
    # 真正能守这件事的是**动态活性**：
    #   · `test_partial_unique_index_global_rejects_duplicate_revision`（正向）
    #   · `test_partial_unique_index_project_rejects_duplicate_revision` 里
    #     "另一个项目同修订号必须成功"那一行（反向）
    # 下面这两条静态断言只做**廉价的形态检查**，作为动态证据的补充。
    expected_scope = {
        "uq_runtime_settings_global": "GLOBAL",
        "uq_runtime_settings_project": "PROJECT",
    }
    for name, definition in rows:
        assert "scope_kind" in definition, (
            f"{name} 的 WHERE 没有按 scope_kind 过滤 —— 这样的索引"
            f"不是作用域过滤，它会跨作用域误拦：{definition}"
        )
        assert expected_scope[name] in definition, (
            f"{name} 的 WHERE 里没有 {expected_scope[name]!r}：{definition}"
        )


def test_partial_unique_index_project_where_cannot_be_reversed(
    env: PlanningEnv,
) -> None:
    """★ **反向验证**：`uq_runtime_settings_project` 的 `WHERE` **无法**被反向测。

    ## 什么叫"反向验证"，为什么这里测不了

    上面对全局索引做了一个漂亮的反向验证：造两行 PROJECT、不同 `project_id`、
    同 `(key, revision_no)` —— 若全局索引的 `WHERE` 变成永真，它会误拦。
    于是"能插进去"就证明了全局索引的 `WHERE` **真的在按 scope_kind 过滤**。

    项目索引**做不到**这件事，原因不是偷懒，而是**逻辑上不可构造**：

    | 要构造的行 | 需要的取值 | 立刻撞上 |
    |---|---|---|
    | "键匹配全局索引的键（`key, revision_no`），但不匹配它的 `WHERE`" | `scope_kind='PROJECT'` —— 那就**匹配**了，不是反例 | —— |
    | 想反过来：不匹配项目索引的 `WHERE`（即 `scope_kind='GLOBAL'`），但键匹配（`project_id` 非空） | `scope_kind='GLOBAL'` + `project_id IS NOT NULL` | `ck_runtime_settings_scope_matches_project`（实测 23514） |

    最后一行就是**反向验证的落点**：本用例证明"第二条路走不通"——
    即"项目索引的反例在任何合法行上都不存在"。

    ## 这条断言的真实价值

    它把"项目索引的 `WHERE` 写错"这个风险的**唯一可观测后果**说清楚了：
    若有人把项目索引的 `WHERE` 改成 `scope_kind = 'GLOBAL'`，
    那么**项目行**（`scope_kind='PROJECT'`）就完全不受它约束了
    —— 而上面 `test_partial_unique_index_project_rejects_duplicate_revision`
    会立刻报红（重复的项目行能插进去）。因此**覆盖并没有缺失**，
    只是由那条正向用例承担。本用例的价值是**显式记录这个推理**，
    免得后来者以为"少了一个反向验证 = 判据不严"而徒劳地再试一次。

    已实测：这一行的 SQLSTATE 是 `23514`、约束名是
    `ck_runtime_settings_scope_matches_project`。
    """
    error = _raw_expect_error(
        env,
        "INSERT INTO runtime_settings"
        " (key, scope_kind, project_id, value, revision_no, origin, reason)"
        " VALUES (:k, 'GLOBAL', :pid, CAST(:v AS jsonb), :rev, 'GLOBAL', :reason)",
        {
            "k": SETTING_KEY_BATCH_LIMITS,
            "pid": str(env.project_id),
            "v": json.dumps(dict(DEFAULT_BATCH_MAX)),
            "rev": _next_revision(env),
            "reason": "3.1 项目索引反向验证（构造不出反例）",
        },
    )
    assert _sqlstate(error) == "23514"
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == (
        "ck_runtime_settings_scope_matches_project"
    )


# --- 3.2 runtime_settings 的 8 条 CHECK ------------------------------------


def test_runtime_settings_check_key_valid(env: PlanningEnv) -> None:
    """`key` 必须在 `SETTING_KEYS` **闭集**内（DDL 里是 `IN (...)` 字面量）。

    为什么这条必须落在数据库层：`key` 是**分派依据** ——
    读取方按 `key` 选解析分支（`batch.limits` 走三档校验，其它走通用形状）。
    一个拼错的键（`batch.limit`）落库后，读取路径会把它当"未知键"报 422，
    而**写入方以为写成功了**。数据库这一层的闭集让"写进去的键一定认得"。

    `key` 的合法集合在 DDL 里是**字面量**（不是查 `SETTING_KEYS`），
    因此 `SETTING_KEY_LIST_SQL` 与 `types.SETTING_KEYS` 的一致性
    由 §1.4 的 DDL 文本判据保证（那边对账两处）。本用例只验证"闭集生效"。
    """
    error = _raw_expect_error(
        env,
        "INSERT INTO runtime_settings"
        " (key, scope_kind, project_id, value, revision_no, origin, reason)"
        " VALUES (:k, 'GLOBAL', NULL, CAST(:v AS jsonb), :rev, 'GLOBAL', :reason)",
        {
            "k": "batch.limit",  # 少一个 s —— 典型手滑
            "v": "{}",
            "rev": _next_revision(env),
            "reason": "3.2 key_valid",
        },
    )
    assert _sqlstate(error) == "23514"
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == "ck_runtime_settings_key_valid"


def test_runtime_settings_check_scope_kind_valid_is_shadowed(
    env: PlanningEnv,
) -> None:
    """★ `scope_kind` 非法值被拒，但**不归因到** `ck_.._scope_kind_valid`。

    ## 这是本层最反直觉的一条发现（实测）

    反例取 `scope_kind='TENANT'`。理论上应命中
    `ck_runtime_settings_scope_kind_valid`（它就是这个取值的闭集校验），
    但**实测命中的是 `ck_runtime_settings_origin_matches_scope`**：

        ck_runtime_settings_origin_matches_scope:
          CHECK ((scope_kind = 'GLOBAL' AND origin = 'GLOBAL')
              OR (scope_kind = 'PROJECT' AND origin IN ('INHERITED','MANUAL')))

    它把两个列的**全部合法组合**枚举成了析取式，因此任何"取值不在枚举里"
    的情形都让它先失败。PostgreSQL 报告哪个约束失败**不保证按名字顺序**，
    实测它报了这一个。

    ## 处置：只断言 SQLSTATE，不断言名字

    若强行断言 `== 'ck_runtime_settings_scope_kind_valid'`，本用例会在
    "PG 换了检查顺序"时**假失败**。而假失败的最终命运是被注释掉
    —— 那时"scope_kind 有闭集"这件事就彻底没人守了（ISS-95）。

    于是这里断言两件**稳定**的事：

      1. SQLSTATE == `23514`（它确实被 CHECK 拒了，不是被别的东西拒的）；
      2. 拒绝它的约束名**在**本表的 CHECK 集合里（不是外键 / 唯一索引 / 触发器）。

    第 2 条是关键：它挡住"被 `NOT NULL` 或 FK 拒"这类**错误原因下的通过**。
    """
    error = _raw_expect_error(
        env,
        "INSERT INTO runtime_settings"
        " (key, scope_kind, project_id, value, revision_no, origin, reason)"
        " VALUES (:k, 'TENANT', NULL, CAST(:v AS jsonb), :rev, 'GLOBAL', :reason)",
        {
            "k": SETTING_KEY_BATCH_LIMITS,
            "v": "{}",
            "rev": _next_revision(env),
            "reason": "3.2 scope_kind_valid",
        },
    )
    assert _sqlstate(error) == "23514", (
        f"scope_kind='TENANT' 未被 CHECK 拒绝。SQLSTATE={_sqlstate(error)!r}，"
        f"消息={_primary(error)!r}"
    )
    diag = getattr(getattr(error, "orig", None), "diag", None)
    actual = getattr(diag, "constraint_name", None)
    assert actual in _runtime_settings_check_names(env), (
        f"拒绝 scope_kind='TENANT' 的是 {actual!r}，它不是本表的 CHECK —— "
        "说明失败原因不是取值闭集（见 docstring：本条不断言具体是哪个 CHECK）"
    )
    # 显式记录"实测是哪一个"，让这条发现可被复查（改了不算失败）。
    assert actual == "ck_runtime_settings_origin_matches_scope", (
        "实测归因发生了变化（原本是 origin_matches_scope 抢先命中）—— "
        "不是缺陷，但说明本用例的 docstring 需要同步更新"
    )


def test_runtime_settings_check_origin_valid_is_shadowed(
    env: PlanningEnv,
) -> None:
    """★ `origin` 非法值被拒，同样**不归因到** `ck_.._origin_valid`。

    与 `scope_kind` 那条**完全同因**（`origin_matches_scope` 的析取式
    把两个列的合法组合枚举在一起）。反例取 `origin='COPIED'` ——
    实测命中的仍是 `ck_runtime_settings_origin_matches_scope`。

    **为什么要分成两个用例而不是一个**：两条是被**同一个**约束遮蔽的，
    但它们的方向不同（一个改 `scope_kind`、一个改 `origin`）。
    合起来写会让"只修好了一个方向的闭集"这种情况漏检 ——
    比如 `scope_kind_valid` 被人删掉，而合并用例仍是红的（因为 `origin` 那条还生效）。
    """
    error = _raw_expect_error(
        env,
        "INSERT INTO runtime_settings"
        " (key, scope_kind, project_id, value, revision_no, origin, reason)"
        " VALUES (:k, 'GLOBAL', NULL, CAST(:v AS jsonb), :rev, 'COPIED', :reason)",
        {
            "k": SETTING_KEY_BATCH_LIMITS,
            "v": "{}",
            "rev": _next_revision(env),
            "reason": "3.2 origin_valid",
        },
    )
    assert _sqlstate(error) == "23514"
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == (
        "ck_runtime_settings_origin_matches_scope"
    )


def test_runtime_settings_check_revision_no_positive(env: PlanningEnv) -> None:
    """`revision_no >= 1` —— 修订号从 1 起（0 是"从未配置"的哨兵，不落库）。

    为什么这一条要紧：`max(revision_no)` 是"当前生效值"的取值依据
    （`ORDER BY revision_no DESC LIMIT 1`）。若允许 0 或负数，
    一条 `revision_no=-1` 的行会**永远排在最后**因而不生效，
    但它在表里、在历史里都看得见 —— 那是"我明明写了却没用"的经典症状。
    """
    error = _raw_expect_error(
        env,
        "INSERT INTO runtime_settings"
        " (key, scope_kind, project_id, value, revision_no, origin, reason)"
        " VALUES (:k, 'GLOBAL', NULL, CAST(:v AS jsonb), 0, 'GLOBAL', :reason)",
        {
            "k": SETTING_KEY_BATCH_LIMITS,
            "v": "{}",
            "reason": "3.2 revision_no_positive",
        },
    )
    assert _sqlstate(error) == "23514"
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == (
        "ck_runtime_settings_revision_no_positive"
    )


def test_runtime_settings_check_scope_matches_project(env: PlanningEnv) -> None:
    """`GLOBAL` ⇒ `project_id IS NULL`；`PROJECT` ⇒ `project_id IS NOT NULL`。

    没有这条的话，`(scope_kind='GLOBAL', project_id=<某项目>)` 可以落库 ——
    而读取路径按 `scope_kind` 取行，于是这行会被当成**全局值**
    （`project_id IS NULL` 的查询看不到它，但 `scope_kind='GLOBAL'`
    的查询会看到它），却带着一个项目归属。那是"全局值莫名来自某个项目"。

    反例取 `scope_kind='GLOBAL'` + `project_id` 非空（另一半
    `PROJECT` + `project_id IS NULL` 会让 NOT NULL 先报，指向错误原因）。
    """
    error = _raw_expect_error(
        env,
        "INSERT INTO runtime_settings"
        " (key, scope_kind, project_id, value, revision_no, origin, reason)"
        " VALUES (:k, 'GLOBAL', :pid, CAST(:v AS jsonb), :rev, 'GLOBAL', :reason)",
        {
            "k": SETTING_KEY_BATCH_LIMITS,
            "pid": str(env.project_id),
            "v": "{}",
            "rev": _next_revision(env),
            "reason": "3.2 scope_matches_project",
        },
    )
    assert _sqlstate(error) == "23514"
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == (
        "ck_runtime_settings_scope_matches_project"
    )


def test_runtime_settings_check_origin_matches_scope(env: PlanningEnv) -> None:
    """`GLOBAL` ⇒ `origin='GLOBAL'`；`PROJECT` ⇒ `origin IN ('INHERITED','MANUAL')`。

    ## 这条是 J1~J4 的**结构前提**

    `origin` 是"这行是否跟随全局变更"的载体（`INHERITED` 跟随、`MANUAL` 不跟随），
    而"跟随"只在**项目级**行上才有意义：

      * 全局行写 `origin='INHERITED'` ⇒ 它自己"跟随谁"？（无解，语义空洞）
      * 项目行写 `origin='GLOBAL'` ⇒ 它自称"我就是全局值"？（身份混乱，
        而读取路径正是按 `origin` 决定"是否覆盖"）

    因此这条约束把"来源取值"与"作用域"绑在一起。它也是上面两条
    `*_is_shadowed` 用例里**抢先命中的那一个** —— 三者在实现上是同一段析取式。

    反例取 `scope_kind='GLOBAL'` + `origin='MANUAL'`（合法 scope、合法 origin，
    **组合**非法）。这是唯一能精确命中本约束的构造 ——
    因为改 `scope_kind` 或改 `origin` 都会让它先于另外两条失败。
    """
    error = _raw_expect_error(
        env,
        "INSERT INTO runtime_settings"
        " (key, scope_kind, project_id, value, revision_no, origin, reason)"
        " VALUES (:k, 'GLOBAL', NULL, CAST(:v AS jsonb), :rev, 'MANUAL', :reason)",
        {
            "k": SETTING_KEY_BATCH_LIMITS,
            "v": "{}",
            "rev": _next_revision(env),
            "reason": "3.2 origin_matches_scope",
        },
    )
    assert _sqlstate(error) == "23514"
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == (
        "ck_runtime_settings_origin_matches_scope"
    )


def test_runtime_settings_check_value_is_object(env: PlanningEnv) -> None:
    """`value` 必须是 JSON **对象**（`jsonb_typeof(value) = 'object'`）。

    ## 为什么不是"任意 JSON"

    读取路径一律按**映射**取值（`setting.value.get("TRIAL")`、
    `BatchLimitsBody.of(value)` 里的 `value["TRIAL"]`）。若存进去一个数组
    （`[10, 30, 50]`），取值会变成 `value[0]` 或 `TypeError` ——
    两种都不报错地错。而 `null` 也有去处：清空用**新增一行 `{}`** 表达
    （本包约定），不是把整个 `value` 置 NULL（那会让 `jsonb_typeof(NULL)`
    是 NULL 而非 'object'，取值路径得不到可用的映射）。

    反例取 `[1,2]`（数组）—— 它是最容易被误当成"三档"的形态。
    """
    error = _raw_expect_error(
        env,
        "INSERT INTO runtime_settings"
        " (key, scope_kind, project_id, value, revision_no, origin, reason)"
        " VALUES (:k, 'GLOBAL', NULL, CAST(:v AS jsonb), :rev, 'GLOBAL', :reason)",
        {
            "k": SETTING_KEY_BATCH_LIMITS,
            "v": "[1,2]",
            "rev": _next_revision(env),
            "reason": "3.2 value_is_object",
        },
    )
    assert _sqlstate(error) == "23514"
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == (
        "ck_runtime_settings_value_is_object"
    )


def test_runtime_settings_check_reason_not_blank(env: PlanningEnv) -> None:
    """`reason` 允许 `NULL`，但**不允许空串**。

    这是 ADR-13 审计要求里"为什么"那一列的**最低门槛**：
    `NULL` 诚实地表示"无操作人/无理由"（迁移回填与运维 SQL），
    而空串是"填了个框但什么都没写"—— 两者在页面上看不出区别，
    但在判据里必须可区分（否则"审计完整"退化成"列存在"）。

    ⚠️ 反例必须是**空串**而不是 `NULL`：`NULL` 是**合法**的
    （`reason IS NULL OR char_length(reason) > 0`）。若拿 `NULL` 当反例，
    用例会失败，而失败原因会被误读成"约束太严"。
    因此这里**同时**验证 `NULL` 能插进去 —— 这是"约束没有过严"的方向。
    """
    error = _raw_expect_error(
        env,
        "INSERT INTO runtime_settings"
        " (key, scope_kind, project_id, value, revision_no, origin, reason)"
        " VALUES (:k, 'GLOBAL', NULL, CAST(:v AS jsonb), :rev, 'GLOBAL', '')",
        {
            "k": SETTING_KEY_BATCH_LIMITS,
            "v": "{}",
            "rev": _next_revision(env),
        },
    )
    assert _sqlstate(error) == "23514"
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == (
        "ck_runtime_settings_reason_not_blank"
    )

    # 反向：`NULL` 与"有内容的串"都必须能插 —— 约束不能过严。
    base = _next_revision(env)
    for offset, reason_sql, params in (
        (0, "NULL", {}),
        (1, ":reason", {"reason": "有理由"}),
    ):
        _raw(
            env,
            "INSERT INTO runtime_settings"
            " (key, scope_kind, project_id, value, revision_no, origin, reason)"
            f" VALUES (:k, 'GLOBAL', NULL, CAST(:v AS jsonb), :rev, 'GLOBAL', {reason_sql})",
            {
                "k": SETTING_KEY_BATCH_LIMITS,
                "v": "{}",
                "rev": base + offset,
                **params,
            },
        )


# --- 3.3 plan_drafts 的 7 条 CHECK ------------------------------------------


def test_plan_drafts_check_state_valid(env: PlanningEnv) -> None:
    """`state` 闭集：`EDITING` / `SUBMITTED`。

    为什么闭集在数据库层：`state` 是**流程闸门** —— `submit_draft` 靠
    `state != 'EDITING'` 挡重复提交。若允许 `state='DRAFT'` 落库，
    那么所有"只认 EDITING/SUBMITTED"的判定都会把它当成"已提交"
    （因为 `!= 'EDITING'`），于是草稿**永远提交不了**，而报错说"已提交过"
    —— 与事实完全相反。
    """
    error = _insert_plan_draft_expect_error(env, state="DRAFT")
    assert _sqlstate(error) == "23514"
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == "ck_plan_drafts_state_valid"


def test_plan_drafts_check_total_quantity_positive(env: PlanningEnv) -> None:
    """`total_quantity` 允许 `NULL`（未设），但设了就必须 `>= 1`。

    与 `reason` 同款的双向要求：`NULL` 是合法状态（"还没定总量"），
    而 `0` 不是 —— 总量 0 会让分配恒为 0（`floor(0 * w / 1000) = 0`），
    于是"这一批生产 0 条"成为一个能提交的合法计划。
    """
    error = _insert_plan_draft_expect_error(env, total_quantity=0)
    assert _sqlstate(error) == "23514"
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == (
        "ck_plan_drafts_total_quantity_positive"
    )

    # `NULL` 必须合法（约束不能过严）。
    _insert_plan_draft(env, total_quantity=None)


def test_plan_drafts_check_total_quantity_within_bound(env: PlanningEnv) -> None:
    """`total_quantity <= MAX_QUANTITY_BOUND`（= 100_000）。

    这是**成本防线**：分配会把总量摊到各场景/形式，而下游按条数计价。
    一个手滑多打两个零的 1_000_000 会让下游生成一百万个任务 ——
    它是"输入错误直接变成账单"的入口，因此这个上界必须落在数据库层
    （服务层那层判据可以被人绕过，CHECK 不行）。

    断言里用 `MAX_QUANTITY_BOUND` 常量而不是字面量 `100000`：
    两处各写一遍会漂移，而漂移的症状是"服务层放行、数据库拒绝"，
    报错落在用户操作上，看起来像偶发。
    """
    error = _insert_plan_draft_expect_error(
        env, total_quantity=MAX_QUANTITY_BOUND + 1
    )
    assert _sqlstate(error) == "23514"
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == (
        "ck_plan_drafts_total_quantity_within_bound"
    )

    # 边界值**必须合法** —— 否则"上界"实际是"上界减一"。
    _insert_plan_draft(env, total_quantity=MAX_QUANTITY_BOUND)


def test_plan_drafts_check_scene_items_is_array(env: PlanningEnv) -> None:
    """`scene_items` 必须是 JSON **数组**。

    读取路径对它做下标与遍历（`dimension_items` 逐条取 `weight_permille`）。
    若存成对象（`{"a": 300}`），遍历会拿到**键名**而不是条目 ——
    分配结果会是一堆"名字为 a、权重缺失"的行，且不报错。
    """
    error = _insert_plan_draft_expect_error(env, scene_items="{}")
    assert _sqlstate(error) == "23514"
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == (
        "ck_plan_drafts_scene_items_is_array"
    )


def test_plan_drafts_check_form_items_is_array(env: PlanningEnv) -> None:
    """`form_items` 必须是 JSON **数组**（与 `scene_items` 同款）。

    单独一条而不是与场景合并：两列是两个维度，**分别**被读取、
    **分别**算分配、**分别**校验合计。合并会让"只改坏了形式那一列"漏检
    （场景那半仍然合法）。
    """
    error = _insert_plan_draft_expect_error(env, form_items="{}")
    assert _sqlstate(error) == "23514"
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == (
        "ck_plan_drafts_form_items_is_array"
    )


def test_plan_drafts_check_pairings_is_array(env: PlanningEnv) -> None:
    """`pairings` 必须是 JSON **数组**。

    `pairings` 的条目是 `{sceneKey, formKey}`，"场景 × 形式"配对一行。
    它不是映射（同一个 scene 可以配多个 form），因此数组是唯一合理形态
    —— 若存成 `{"s1": "f1"}`，同一个场景的第二个配对会**静默覆盖**第一个。
    """
    error = _insert_plan_draft_expect_error(env, pairings="{}")
    assert _sqlstate(error) == "23514"
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == (
        "ck_plan_drafts_pairings_is_array"
    )


def test_plan_drafts_check_schema_version_positive(env: PlanningEnv) -> None:
    """`schema_version >= 1`。

    这个列的存在理由是"JSONB 结构将来会演进"（`scene_items` 的条目字段
    可能增加）。`0` 或负数没有含义，且它会被读取路径当成"旧版本"，
    从而走进某个兼容分支 —— 而那个分支不该被任何真实行触达。
    """
    error = _insert_plan_draft_expect_error(env, schema_version=0)
    assert _sqlstate(error) == "23514"
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == (
        "ck_plan_drafts_schema_version_positive"
    )


def test_plan_drafts_unique_project_id_is_a_real_constraint(
    env: PlanningEnv,
) -> None:
    """★ **对照用例**：`plan_drafts` 的"一个项目一份草稿"是**普通** UNIQUE。

    与 `runtime_settings` 的两条部分唯一索引形成对照 ——
    这条是表级约束，**在 `pg_constraint` 里**（`contype='u'`，实测）。

    两表为什么不同，是本包 DDL 设计的核心：

    | 表 | 唯一性目标 | 用什么 | 为什么不用另一种 |
    |---|---|---|---|
    | `runtime_settings` | 同一 `(scope, key)` 的同一修订号不重复 | **部分唯一索引**（两条） | 普通 UNIQUE 含 `project_id`，而全局行的它是 NULL ⇒ `NULL != NULL` ⇒ **全局行完全不生效** |
    | `plan_drafts` | 一个项目一份草稿 | **普通 UNIQUE** | `project_id` 非空（NOT NULL）⇒ 没有 NULL 问题，普通 UNIQUE 就够 |

    因此这条用例钉的是"**选型差异有理由**"：若有人把 `plan_drafts`
    的 UNIQUE 也改成部分唯一索引（无异于画蛇添足），本用例报红提示复审。

    ## 反向验证：重复插入必须被拒

    只断言"约束在 `pg_constraint` 里"是**静态**的 —— 一个名字对但
    `DEFERRABLE` 且事务里没触发的约束也能满足它。因此这里真的插两次。
    """
    with session_scope(env.settings) as session:
        row = session.execute(
            text(
                "SELECT contype FROM pg_constraint"
                " WHERE conrelid = 'plan_drafts'::regclass"
                " AND conname = 'uq_plan_drafts_project_id'"
            )
        ).fetchone()
    assert row is not None, "uq_plan_drafts_project_id 不在 pg_constraint 里"
    assert row[0] == "u"

    _insert_plan_draft(env)
    same_project = _new_project_id(env)
    _insert_plan_draft(env, project_id=str(same_project))
    error = _insert_plan_draft_expect_error(env, project_id=str(same_project))
    assert _sqlstate(error) == pl_ddl.UNIQUE_VIOLATION_SQLSTATE
    diag = getattr(getattr(error, "orig", None), "diag", None)
    assert getattr(diag, "constraint_name", None) == "uq_plan_drafts_project_id"

    # 反向：**另一个项目**装同一份草稿必须成功（唯一性是按项目分的，
    # 不是全局唯一 —— 后者会让第二个项目永远建不出草稿）。
    _insert_plan_draft(env, project_id=str(_new_project_id(env)))


# --- 3.x 第 3 层的基础设施 --------------------------------------------------
#
# 本层全部用原生 SQL 直捅数据库。下面三个 helper 只负责"把行造出来"，
# **不含任何业务校验** —— 那是第 1/2 层的事。若这里也校验一遍，
# 判据就变成"我的校验拦住了我的校验"，而数据库那一层到底有没有约束
# 仍然不可知（这正是本层存在的理由）。


def _runtime_settings_check_names(env: PlanningEnv) -> set[str]:
    """本表**全部** CHECK 约束名（从 `pg_constraint` 现取，不写死清单）。

    **为什么不写死**：写死会在有人新增一条 CHECK 时静默过期 ——
    而那句断言（"拒绝它的名字在本表 CHECK 集合里"）本来就是为了
    挡"被别的东西拒"，清单过期会让它变成恒真。
    """
    rows = _raw(
        env,
        "SELECT conname FROM pg_constraint"
        " WHERE conrelid = 'runtime_settings'::regclass AND contype = 'c'",
    )
    return {row[0] for row in rows or []}


def _new_project_id(env: PlanningEnv) -> uuid.UUID:
    """新建一个项目，返回它的 id（用于"每次插草稿都要一个新项目"）。"""
    _brand, _product, project_id = _seed_project(env.settings, owner_id=env.owner_id)
    return project_id


def _plan_draft_params(env: PlanningEnv, overrides: dict[str, Any]) -> dict[str, Any]:
    """草稿行的参数（合法默认值 + 覆盖）。

    ## 为什么两个插入 helper 共用这一个

    两处各写一份默认值，会让"合法那条"与"非法那条"在**别的列**上悄悄不同，
    于是失败原因指向错误的列（本层最怕的诊断歧义）。

    ## `project_id` 的两条路

      * **不传** ⇒ 新建一个项目（默认，用于"我只关心这一列"的用例）；
      * **传了** ⇒ 用调用方的那个项目（用于"同项目插两次"的唯一性用例）。

    第二条是 `uq_plan_drafts_project_id` 那条用例的必需形态：它必须在
    **同一个项目**上插两次 —— 而默认的"每次新项目"会让它永远插得进去。
    """
    params: dict[str, Any] = {
        "project_id": str(overrides.pop("project_id", None) or _new_project_id(env)),
        "state": "EDITING",
        "total_quantity": 10,
        "scene_items": "[]",
        "form_items": "[]",
        "pairings": "[]",
        "schema_version": 1,
    }
    params.update(overrides)
    return {**params, "created_by": str(env.owner_id)}


_INSERT_DRAFT_SQL = (
    "INSERT INTO plan_drafts"
    " (project_id, state, total_quantity, scene_items, form_items, pairings,"
    "  schema_version, created_by)"
    " VALUES (:project_id, :state, :total_quantity, CAST(:scene_items AS jsonb),"
    "  CAST(:form_items AS jsonb), CAST(:pairings AS jsonb), :schema_version,"
    "  :created_by)"
)


def _insert_plan_draft(env: PlanningEnv, **overrides: Any) -> uuid.UUID:
    """插一条**合法**的 `plan_drafts` 行，返回它的 id。

    ## 为什么用 `RETURNING id` 而不是自己造 uuid

    `id` 的 DDL 默认值是 `gen_random_uuid()`（无显式给值）。自己造一个
    也能行，但 `RETURNING` 让"这一行的 id 到底是什么"由数据库回答 ——
    少一个"以为插的是这个 id"的可能。

    ## 为什么走 ORM 造项目

    插的是 `projects` / `brands` / `products`（**别的包的表**），
    走 `_seed_project` 而不是原生 SQL：那些表的 CHECK 不是本层要测的对象
    （各自包的测试已守着），而走既有 helper 能让"本项目必须存在"
    这个**前置条件**不必在本层重写一遍。
    """
    rows = _raw(env, f"{_INSERT_DRAFT_SQL} RETURNING id", _plan_draft_params(env, overrides))
    assert rows, "INSERT ... RETURNING 没有返回行 —— 前置条件失败（用户不存在？）"
    return uuid.UUID(str(rows[0][0]))


def _insert_plan_draft_expect_error(env: PlanningEnv, **overrides: Any) -> DBAPIError:
    """插一条**应当失败**的 `plan_drafts` 行，返回异常供断言。

    与 `_insert_plan_draft` 共用 `_INSERT_DRAFT_SQL` 与 `_plan_draft_params`
    —— 见后者 docstring。
    """
    return _raw_expect_error(env, _INSERT_DRAFT_SQL, _plan_draft_params(env, overrides))


def _draft_schema_version(env: PlanningEnv, draft_id: uuid.UUID) -> int:
    return int(
        _scalar(
            env,
            "SELECT schema_version FROM plan_drafts WHERE id = :did",
            {"did": str(draft_id)},
        )
        or 0
    )


# ===========================================================================
# 第 4 层：HTTP（接线是否正确）
# ===========================================================================
#
# 前三层测的是"算法对不对、服务层对不对、数据库拦不拦"。
# **这一层测的是接线**：路径对不对、方法对不对、鉴权链装没装上、
# `If-Match` 的前置校验有没有真的接在端点上、响应形状是不是契约里的那个。
#
# ## 为什么必须单独有一层（前三层全绿也可能接线是错的）
#
# 服务层用例直接调 `service.save_draft(...)`，因此它**不经过**
# `access.require(ProjectAction.EDIT)`、不经过 `require_if_match`、
# 不经过 FastAPI 的路径解析。一个"端点漏了 `access.require`"的缺陷
# 会让前三层全部通过 —— 而那是一个**越权**缺陷。
#
# ## 本层的两个实测约束（都写进了对应用例）
#
# 1. **`PUT /settings/global/{key}` 无法构造 403**:
#    `_ORG_MATRIX` 里 `KNOWLEDGE_MANAGE` 对 `ROLE_ADMIN` 与 `ROLE_OPERATOR`
#    都开放（运营唯一开放的组织级操作）。而本 environment 只造得出
#    这两种角色 ⇒ **该端点只能测 401（未登录）**。
#    强行造"非成员 403"会失败，而失败会被误读成"权限没生效"。
# 2. **非成员访问项目端点返回 404 而不是 403**:
#    `resolve_project_access` 刻意这么设计（防项目存在性枚举）。
#    因此 outsider 的期望码是 **404**。
# ---------------------------------------------------------------------------

API_PREFIX = "/api/v1"


@pytest.fixture
def app(env: PlanningEnv) -> FastAPI:
    """按注入配置装配 app，并**在 `main.py` 尚未接线时**补挂本模块 router。

    **挂载必须幂等**：`create_app` 自己已挂一次（`main.py` 第 320 行），
    此时再挂一次会让每条路由**重复注册**（WP20 收口时实测：路由数翻倍）。

    探测依据用**路径前缀**而不是 router 对象身份：`create_app` 内部 import 的
    是同一个模块级 `router`，但 `include_router` 会把它**复制成新对象**，
    身份比较不可靠（WP23 §第 4 层蓝本的同款说明）。
    """
    from rednote.main import create_app

    assert env.settings.api_prefix == API_PREFIX, (
        f"本文件写死了 {API_PREFIX!r}，注入配置给的是 "
        f"{env.settings.api_prefix!r} —— 两者必须一致"
    )

    application = create_app(env.settings)

    mounted_path = f"{API_PREFIX}/projects/{{projectId}}/plan-draft"
    already_mounted = any(
        getattr(route, "path", "") == mounted_path for route in application.routes
    )
    if not already_mounted:
        from rednote.modules.planning.api import router as planning_router

        application.include_router(planning_router, prefix=env.settings.api_prefix)
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, env: PlanningEnv, *, who: str = "owner") -> None:
    """造一条会话并写 Cookie（含 CSRF 令牌）。

    ## 为什么 CSRF 必须一起处理（不能绕过）

    `current_user` 依赖对**写方法**强制 `verify_csrf`，且它是**结构上无法
    绕过**的。绕过它会让本层不再验证真实的鉴权链 —— 那时"401/403 断言"
    就成了对一段假鉴权的断言。

    ## `who` 的三档

    | `who` | 用户 | 本层用途 |
    |---|---|---|
    | `"owner"` | `env.owner_id`（`ROLE_OPERATOR`，项目成员） | 正常路径 200 |
    | `"admin"` | `env.admin_id`（`ROLE_ADMIN`，**非成员**） | 验证"管理者全权"（非成员也放行） |
    | `"outsider"` | `env.outsider_id`（`ROLE_OPERATOR`，**非成员**） | 验证 404（防枚举） |

    `outsider` 与 `admin` 的对照是本层最要紧的一组：
    **两者都不是项目成员**，但一个 404、一个 200 —— 差别只在角色。
    若把两者合成"非成员应该被拒"，就测不出"管理者全权"这条规则。
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


def _draft_url(env: PlanningEnv, *, project_id: uuid.UUID | None = None) -> str:
    return f"{API_PREFIX}/projects/{project_id or env.project_id}/plan-draft"


def _read_draft(client: TestClient, env: PlanningEnv) -> dict[str, Any]:
    """读草稿并把 `data` 返回（本层几乎每条用例都以此开头）。"""
    response = client.get(_draft_url(env))
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.regression
def test_router_exposes_exactly_eight_endpoints(app: FastAPI) -> None:
    """本模块**恰好注册 8 个端点**（3 项目级 + 5 配置级）。

    多一个少一个都会让 `api.py` docstring 里那张 8 行表失去可核对性 ——
    而那张表是本包对协议的唯一形状声明（协议 §7 在本仓没有副本，
    见 `api.py` docstring 第一段）。

    ## 判据范围：只数**本包 router 自己**声明的操作

    刻意**不数 `app.routes` 里所有相关路径**。两个理由：

      * `/settings/*` 是**共用路径段**（其它包也会有 `/settings/...`），
        按 `app.routes` 计数会在别的包合法加端点时变红；
      * `POST /projects/{projectId}/plan-draft/submissions` 与
        WP22 的 `/projects/{projectId}/...` 端点**共前缀**。

    因此分两句各自成立：

      1. **本包 router 自己**恰好 8 个操作（本包的事实，稳定）；
      2. 8 个全都真的挂在 `app` 上（接线正确，允许别人加端点）。

    第 1 句用 `router.routes` 而不是 `app.routes`，这是本项目
    ISS-44/64/71/83/87 的重复教训：**判据只能断言"我建的还在"，
    不能断言"全项目恰好有 N 个"**。
    """
    from rednote.modules.planning.api import router as planning_router

    own_operations = {
        (route.path, method)
        for route in planning_router.routes
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }
    expected = {
        ("/projects/{projectId}/plan-draft", "GET"),
        ("/projects/{projectId}/plan-draft", "PUT"),
        ("/projects/{projectId}/plan-draft/submissions", "POST"),
        ("/settings/global", "GET"),
        ("/settings/global/{key}/history", "GET"),
        ("/settings/global/{key}", "PUT"),
        ("/settings/projects/diverged", "GET"),
        ("/settings/projects/{projectId}/{key}", "PUT"),
    }
    assert own_operations == expected, (
        f"多出：{sorted(own_operations - expected)}；"
        f"缺少：{sorted(expected - own_operations)}"
    )
    assert len(expected) == 8

    mounted = {
        (route.path, method)
        for route in app.routes
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }
    missing = {(f"{API_PREFIX}{path}", method) for path, method in expected} - mounted
    assert not missing, f"本包的端点在 app 里找不到：{sorted(missing)}"


def test_plan_draft_read_requires_login(client: TestClient, env: PlanningEnv) -> None:
    """未登录读草稿 ⇒ **401**（`CurrentUserDep` 的那一层）。

    这条看似废话，但它守的是**依赖链有没有装**：
    若某天有人把 `access: ProjectAccessDep` 换成直接读参数，
    未登录就会走到"项目不存在 ⇒ 404"，而 404 与 401 的**客户端行为完全不同**
    （401 让前端跳登录，404 让前端显示"项目不见了"）。
    """
    response = client.get(_draft_url(env))
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == ErrorCode.SESSION_EXPIRED.value


def test_plan_draft_read_rejects_outsider_with_404(
    client: TestClient, env: PlanningEnv
) -> None:
    """★ 非成员读草稿 ⇒ **404**（不是 403）。

    这是 `resolve_project_access` 的**刻意**设计：403 会告诉攻击者
    "这个项目存在，只是你无权" —— 于是可以用它枚举项目 id。
    404 让"无权访问"与"项目不存在"**不可区分**。

    因此本用例的断言里 404 是**正确行为**，不是"权限没生效"。
    与之对照的是下面 `test_plan_draft_read_allows_admin_non_member`
    （同一个"非成员"身份，因为是管理者而放行）—— 两条必须都在，
    否则"非成员一律 404"这个**错误实现**也能通过。
    """
    _login(client, env, who="outsider")
    response = client.get(_draft_url(env))
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == ErrorCode.RESOURCE_NOT_FOUND.value


def test_plan_draft_read_allows_admin_non_member(
    client: TestClient, env: PlanningEnv
) -> None:
    """管理者**不是项目成员也能读** —— 与上面那条形成夹逼。

    上一条说"非成员 404"，这一条说"非成员（管理者）200"。
    缺任何一条都会让一个错误实现通过：

      * 只留 outsider 那条 ⇒ "所有人一律 404"通过（含管理者，而管理者
        确实需要看任何项目 —— `ProjectPermissions.allows()` 里 admin 全权）；
      * 只留这条 ⇒ "非成员一律放行"通过（越权）。
    """
    _login(client, env, who="admin")
    draft = _read_draft(client, env)
    assert draft["projectId"] == str(env.project_id)


def test_plan_draft_read_lazily_creates_and_returns_full_shape(
    client: TestClient, env: PlanningEnv
) -> None:
    """owner 读草稿 ⇒ 200，且**草稿被懒创建**，响应含两维分配读数。

    ## 为什么断言"懒创建"（而不是只断言 200）

    `service.require_draft` 在草稿不存在时插一行（`api.py` docstring
    第三节记录了"GET 会写库"这个已知代价）。若它变成"不存在就报 404"，
    前端在项目刚建好时会看到一个需要自行创建的空状态 —— 而"初始工作区"
    本该由系统给（协议把这件事归给项目创建事务，本包不能改 WP10 的代码）。

    因此这里**先断言表里没有行**、再读、再断言有一行 —— 三个动作缺一不可：
    只断言"读到了 200"的话，一个"读到别人项目草稿"的实现也能通过。

    ## 为什么必须先登录再断言"没有行"

    用例级夹具（`_clean_setting_rows`）已经把 `plan_drafts` 清空，
    但**清空与登录是两件独立的事**：这条用例第一版就漏了 `_login`，
    于是 `_read_draft` 拿到 401 而报错指向 `no_session_cookie` ——
    看起来像"懒创建没生效"，实际是**前置没做**。
    凡在 HTTP 层读表的用例，`_login` 都必须在最前（见本节其余用例）。
    """
    assert _draft_state(env, project_id=env.project_id) == (0, 0), (
        "前置失败：本用例要求的起点是『这个项目还没有草稿』"
    )

    _login(client, env)

    draft = _read_draft(client, env)

    assert _draft_state(env, project_id=env.project_id)[1] == 1, "GET 没有懒创建草稿"
    assert draft["draftId"]
    assert draft["projectId"] == str(env.project_id)
    assert draft["state"] == DRAFT_STATE_EDITING
    assert draft["schemaVersion"] == 1
    assert draft["lockVersion"] == 1, (
        f"新建草稿的 lockVersion 期望 1（DDL 的 server_default），"
        f"实际 {draft['lockVersion']}"
    )
    assert draft["sceneItems"] == [] and draft["formItems"] == []
    # ETag 必须回带（不回带的话客户端下一次 PUT 必然 412 而它没做错任何事）。
    # ★ 带双引号是 `build_etag` 的既定形态（RFC 9110 的强 entity-tag），
    #   也是 `parse_etag` 的**准入条件**——本文件已有两条用例在此假失败，
    #   凡断言 ETag 一律用 `f'"{...}"'`，不要图省事省引号。
    assert draft["etag"] == f'"{draft["draftId"]}:1"', (
        f"ETag 形态应为带引号的 '<draftId>:<lockVersion>'，实际 {draft['etag']!r}"
    )
    # 两维分配读数都在（`null` 也不接受 —— 草稿里没有条目时应给出空结果）。
    assert draft["sceneAllocation"] is not None
    assert draft["formAllocation"] is not None
    assert draft["sceneAllocation"]["total"] == draft["sceneAllocation"]["allocatedTotal"]


def test_plan_draft_save_requires_if_match(
    client: TestClient, env: PlanningEnv
) -> None:
    """★ 保存草稿缺 `If-Match` ⇒ **428**（不是 412、不是 400）。

    三者的区别是 `require_if_match` 的判定顺序（`platform/concurrency.py`）：

    | 输入 | 码 | 为什么 |
    |---|---|---|
    | 头**缺失** | **428** | `PRECONDITION_REQUIRED`：请求本身不完整，客户端应**带条件重试** |
    | 头**空串** | 428 | 同"缺失"（空串不是"我拿了版本 0"） |
    | 通配符 `*` | 428 | 本仓不支持无条件覆盖（协议 §1.1 要求前置条件） |
    | 格式**非法** | 400 | `MALFORMED_REQUEST`：客户端发错了东西，重试也不会好 |
    | 格式对但**不匹配** | 412 | `VERSION_CONFLICT`：客户端拿的是过期版本，应**重新读取** |

    428 与 412 的区分是**可用性**问题：都返回 412 会让"忘了带 `If-Match`"
    表现为"我的版本总是冲突"，而客户端会去重读、重试、再冲突 —— 死循环。
    """
    _login(client, env)
    _read_draft(client, env)  # 先把草稿建出来

    response = client.put(
        _draft_url(env),
        json={"totalQuantity": 12},
        headers=_write_headers(),
    )
    assert response.status_code == 428, response.text
    assert response.json()["error"]["code"] == ErrorCode.PRECONDITION_REQUIRED.value


def test_plan_draft_save_rejects_malformed_and_stale_etag(
    client: TestClient, env: PlanningEnv
) -> None:
    """★ 400（格式非法）与 412（过期）必须分开 —— 本用例同时钉两者。

    ## 为什么合在一条用例里

    它们共用同一个前置（草稿已存在 + 已登录），且**判定的差异只在 `If-Match`
    的值**。分成两条会让读者以为它们是两个不同的机制。

    ## 412 必须回带 `currentEtag`

    没有它，客户端只能"重新 GET 一次"才能拿到新 ETag —— 而那次 GET
    与这次 412 之间又有窗口（可能在草稿被改之后读到 `lockVersion=7`，
    却在写的时候你已经是 8）。回带 `currentEtag` 让客户端**立即**可以对账。

    ## ★ 引号不是装饰（本用例第一版就在这里假失败）

    `build_etag` 返回的是**带双引号**的强 ETag（`'"<id>:<version>"'`），
    而 `parse_etag` 只接受带引号的形式（`concurrency.py` 第 92 行
    `text.startswith('"') and text.endswith('"')`）。
    因此 `If-Match: <id>:<version>`（不带引号）**不是"过期"而是"格式错"**，
    会拿到 400 而非 412 —— 这正是两个码必须分开的意义，也是本用例
    同时钉住两者的原因：

    | 用例里的取值 | 解析结果 | 期望码 |
    |---|---|---|
    | `"not-an-etag"` | 无引号 ⇒ `None` | **400** |
    | `W/"<id>:1"` | 弱 ETag ⇒ `None` | **400**（本仓从不签发弱 ETag） |
    | `"<id>:<current+99>"` | 能解析，版本不匹配 | **412** |
    | `"<别的资源id>:<current>"` | 能解析，资源不匹配 | **412** |

    末行的含义：**"改错对象"与"版本过期"对客户端是同一处置**
    （去重新读一次正确的那个资源），因此合并成同一个码；
    若混在 400 里，客户端会以为自己的 ETag 是坏的而去改序列化代码。
    """
    _login(client, env)
    draft = _read_draft(client, env)
    draft_id = draft["draftId"]
    current = draft["lockVersion"]
    assert current == 1, "前置：草稿刚建，lockVersion 应为 1"

    # ① 无引号（也缺冒号）：无法解析 ⇒ 400。这**不是**"版本过期"。
    malformed = client.put(
        _draft_url(env),
        json={"totalQuantity": 12},
        headers=_write_headers(**{"If-Match": "not-an-etag"}),
    )
    assert malformed.status_code == 400, malformed.text
    assert malformed.json()["error"]["code"] == ErrorCode.MALFORMED_REQUEST.value
    assert malformed.json()["error"]["details"]["reason"] == "if_match_malformed"

    # ② 弱 ETag：形态是合法 entity-tag，但本仓只签发强 ETag ⇒ 同样 400。
    weak = client.put(
        _draft_url(env),
        json={"totalQuantity": 12},
        headers=_write_headers(**{"If-Match": f'W/"{draft_id}:{current}"'}),
    )
    assert weak.status_code == 400, weak.text
    assert weak.json()["error"]["code"] == ErrorCode.MALFORMED_REQUEST.value

    # ③ 格式对（带引号）但版本过期 ⇒ 412，且回带 currentEtag。
    stale = client.put(
        _draft_url(env),
        json={"totalQuantity": 12},
        headers=_write_headers(**{"If-Match": f'"{draft_id}:{current + 99}"'}),
    )
    assert stale.status_code == 412, stale.text
    payload = stale.json()["error"]
    assert payload["code"] == ErrorCode.VERSION_CONFLICT.value
    assert payload["details"]["currentEtag"] == f'"{draft_id}:{current}"', (
        f"412 必须回带带引号的 currentEtag，实际 {payload['details']['currentEtag']!r}"
    )
    # `providedEtag` 原样回带客户端发来的值（排障时要知道它到底发了什么）。
    assert payload["details"]["providedEtag"] == f'"{draft_id}:{current + 99}"'
    assert payload["details"]["reason"] == "version_mismatch"
    assert payload["details"]["resourceId"] == draft_id

    # ④ 资源 id 不匹配（"别人的草稿的 ETag"）⇒ 也是 412，不是"改错对象"。
    #    这里必须用一个**解析得出来但不是本资源**的 id，否则又退回 400。
    other_id = str(uuid.uuid4())
    wrong_target = client.put(
        _draft_url(env),
        json={"totalQuantity": 12},
        headers=_write_headers(**{"If-Match": f'"{other_id}:{current}"'}),
    )
    assert wrong_target.status_code == 412, wrong_target.text
    assert (
        wrong_target.json()["error"]["details"]["currentEtag"]
        == f'"{draft_id}:{current}"'
    )

    # ⑤ 三次失败都不许改库：草稿的 lock_version 与内容必须原封不动。
    #    缺这条的话，一个"先把请求写进去再校验 ETag"的实现也能通过
    #    （前四个断言只看响应码）。
    assert _draft_state(env, project_id=env.project_id) == (current, 1), (
        "ETag 校验失败的请求污染了草稿"
    )


def test_plan_draft_save_succeeds_and_bumps_lock_version(
    client: TestClient, env: PlanningEnv
) -> None:
    """带正确 `If-Match` 保存 ⇒ 200，`lockVersion` 递增，分配读数跟着变。

    ## 三条断言各自防什么

    | 断言 | 防的实现缺陷 |
    |---|---|
    | `lockVersion == 2` 且 `etag` 带 `:2` | 保存了但**没递增版本** ⇒ 下一次保存用旧 ETag 仍能通过 ⇒ 乐观并发失效 |
    | `sceneItems` / `totalQuantity` 真的变了 | 返回 200 但**没落库**（只改了内存对象） |
    | `sceneAllocation.total == 30` 且 12/18 | 分配读数**没跟着这次保存重算**（前端拿着旧配比去提交，而提交按新配比固化） |

    分配的数字写死（12 / 18）而不是"自己求和再比"：30 × 400‰ = 12、
    30 × 600‰ = 18 **恰好整除**，没有余数补位，因此这两个数字是
    最大余数法在"无余数"路径上的**确定输出**。自己求和会让
    "总量算错但内部自洽"的实现也通过。
    """
    _login(client, env)
    draft = _read_draft(client, env)
    draft_id = draft["draftId"]
    assert draft["lockVersion"] == 1

    saved = client.put(
        _draft_url(env),
        json={
            "totalQuantity": 30,
            "sceneItems": [
                _item_body("scene-a", 400, sort_order=0),
                _item_body("scene-b", 600, sort_order=1),
            ],
            "formItems": [_item_body("form-a", 1000, sort_order=0)],
        },
        headers=_write_headers(**{"If-Match": f'"{draft_id}:1"'}),
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()["data"]

    assert body["lockVersion"] == 2, (
        f"保存必须递增 lockVersion（否则乐观并发失效），实际 {body['lockVersion']}"
    )
    assert body["etag"] == f'"{draft_id}:2"'
    assert body["totalQuantity"] == 30
    assert [item["key"] for item in body["sceneItems"]] == ["scene-a", "scene-b"]
    assert body["sceneAllocation"]["total"] == 30
    assert body["sceneAllocation"]["allocatedTotal"] == 30
    assert body["sceneAllocation"]["unallocated"] == 0
    assert {line["key"]: line["allocated"] for line in body["sceneAllocation"]["lines"]} == {
        "scene-a": 12,
        "scene-b": 18,
    }
    assert body["formAllocation"]["total"] == 30

    # 落库必须与响应一致（响应好看但没写库是"只改内存对象"的典型症状）。
    assert _draft_state(env, project_id=env.project_id) == (2, 1)
    row = _draft_row(env, project_id=env.project_id)
    assert row[0] == DRAFT_STATE_EDITING
    assert row[1] == 30, "total_quantity 没落库"
    assert row[4] == 2, "scene_items 没落库"


def test_plan_draft_submit_freezes_plan_and_returns_201(
    client: TestClient, env: PlanningEnv
) -> None:
    """提交 ⇒ **201**，产出 `entity_type='PLAN'` 的版本节点，草稿转 `SUBMITTED`。

    ## 为什么 `entityId` 必须是 `projectId` 而不是 `draftId`

    这是本包最容易被"顺手写错"的一处（`service.submit_draft` 的注释
    已论证）：PLAN 的逻辑实体是**该项目那条计划链**，草稿只是它的当前
    工作区。若传 `draftId`，同一项目的第二份计划会拿到 `revisionNo=1`
    而不是 2 —— 而两份"第 1 版计划"在版本树里是**互不覆盖**的两个节点，
    于是"最新计划是哪份"变成无解的查询。

    因此断言 `entityId == projectId` 且 `revisionNo == 1`（首次提交）。
    """
    _login(client, env)
    draft = _read_draft(client, env)
    draft_id = draft["draftId"]

    saved = client.put(
        _draft_url(env),
        json={
            "totalQuantity": 20,
            "sceneItems": [_item_body("scene-a", 1000, sort_order=0)],
            "formItems": [_item_body("form-a", 1000, sort_order=0)],
        },
        headers=_write_headers(**{"If-Match": f'"{draft_id}:1"'}),
    )
    assert saved.status_code == 200, saved.text
    version = saved.json()["data"]["lockVersion"]
    assert version == 2

    nodes_before = int(
        _scalar(
            env,
            "SELECT count(*) FROM version_nodes WHERE entity_type = 'PLAN'"
            " AND entity_id = :pid",
            {"pid": str(env.project_id)},
        )
        or 0
    )
    assert nodes_before == 0, "前置：本项目还没有 PLAN 版本节点"

    submitted = client.post(
        f"{_draft_url(env)}/submissions",
        json={"reason": "WP24 测试：固化计划"},
        headers=_write_headers(**{"If-Match": f'"{draft_id}:{version}"'}),
    )
    assert submitted.status_code == 201, submitted.text
    body = submitted.json()["data"]

    assert body["entityType"] == ENTITY_TYPE_PLAN == "PLAN"
    assert body["entityId"] == str(env.project_id), (
        "entityId 必须是 projectId（PLAN 的逻辑实体是项目那条计划链），"
        f"实际 {body['entityId']!r}"
    )
    assert body["revisionNo"] == 1, "首次提交应为第 1 版"
    assert body["created"] is True
    assert len(body["payloadHash"]) == 64
    assert body["nodeId"]

    # 版本节点的 ETag 是**不可变**形态（`h` + 摘要前 16 位），
    # 与草稿的 `"<id>:<lockVersion>"` 形状不同 —— 这个差异就是
    # "草稿 → 版本"那条边界在 HTTP 层的可观察形态（`service.draft_etag` 的论证）。
    assert body["etag"].startswith('"'), f"ETag 必须带引号，实际 {body['etag']!r}"
    assert ":h" in body["etag"], (
        f"版本节点应用 `build_immutable_etag`（带 h 前缀），实际 {body['etag']!r}"
    )

    # 落库：节点真的建了，草稿状态真的转了，且**草稿没被删**。
    nodes_after = int(
        _scalar(
            env,
            "SELECT count(*) FROM version_nodes WHERE entity_type = 'PLAN'"
            " AND entity_id = :pid",
            {"pid": str(env.project_id)},
        )
        or 0
    )
    assert nodes_after == nodes_before + 1
    row = _draft_row(env, project_id=env.project_id)
    assert row[0] == DRAFT_STATE_SUBMITTED, "草稿状态没转成 SUBMITTED"
    assert row[1] == 20, "固化后草稿内容必须保留（原型要展示那张配比表）"
    assert row[4] == 1 and row[5] == 1, "固化后草稿条目必须保留"


def test_plan_draft_submit_retry_is_rejected_in_two_distinct_ways(
    client: TestClient, env: PlanningEnv
) -> None:
    """★ 重试提交：旧 ETag ⇒ **412**；新 ETag ⇒ **409**。两者都必须被拒。

    ## 这条用例同时是一个**文档缺陷的实测**（`api.py` 第 76~78 行）

    那段 docstring 声称：

    > 提交会把 `state` 改成 `SUBMITTED`，重试时同一 ETag 仍指向同一
    > `lock_version`（**状态变更不改 `lock_version`**），因此重试会走到
    > "已提交"的 409 分支

    前半句的**前提是错的**：`repository.update_draft` 第 465 行**无条件**
    写 `lock_version = lock_version + 1`，提交走的正是这个函数 ⇒
    提交后 `lock_version` 从 1 变 2。

    于是"同一 ETag 重试"实际得到的**不是 409 而是 412**
    （`require_if_match` 先于 `submit_draft` 执行，看到 `current_version=2`
    而客户端手里是 `"<id>:1"`）。

    这条用例把**两种码都钉住**，而不是只钉 docstring 预测的那一种：

    | 客户端第二次带的 ETag | 命中处 | 码 | 客户端该做什么 |
    |---|---|---|---|
    | `"<id>:1"`（第一次读到的） | `require_if_match` | **412** | 重新读草稿 ⇒ 看到 `SUBMITTED` |
    | `"<id>:2"`（重读后拿到的） | `submit_draft` 的状态判定 | **409** | 显示"已提交"，不要重试 |

    **为什么不能只留一条**：只断言 412 会让"把 `update_draft` 改成
    状态变更不递增版本"的实现通过（那条判据本身没错，但它把并发保护的
    一个绕过口留了下来 —— 状态变更不递增版本意味着"改内容"与"改状态"
    两个请求可以同时通过条件 UPDATE）；只断言 409 则**当下就失败**。

    正确的处置是**保留实现、修正 docstring**（已回填至 WP24 文档
    §1.6 R11 同节）：递增版本是更安全的语义，412 也是更早的拦截点。
    """
    _login(client, env)
    draft = _read_draft(client, env)
    draft_id = draft["draftId"]

    saved = client.put(
        _draft_url(env),
        json={
            "totalQuantity": 20,
            "sceneItems": [_item_body("scene-a", 1000, sort_order=0)],
            "formItems": [_item_body("form-a", 1000, sort_order=0)],
        },
        headers=_write_headers(**{"If-Match": f'"{draft_id}:1"'}),
    )
    assert saved.status_code == 200, saved.text
    version = saved.json()["data"]["lockVersion"]

    first = client.post(
        f"{_draft_url(env)}/submissions",
        json={"reason": "第一次提交"},
        headers=_write_headers(**{"If-Match": f'"{draft_id}:{version}"'}),
    )
    assert first.status_code == 201, first.text

    # `update_draft` 无条件递增 ⇒ 提交后版本号必然变大。
    after_submit = _draft_state(env, project_id=env.project_id)[0]
    assert after_submit == version + 1, (
        "提交必须经过条件 UPDATE（`update_draft` 无条件递增 lock_version），"
        f"期望 {version + 1}，实际 {after_submit}"
    )

    # ① 用**第一次读到的** ETag 重试 ⇒ 412（`require_if_match` 先拦下）。
    stale_retry = client.post(
        f"{_draft_url(env)}/submissions",
        json={"reason": "重试（旧 ETag）"},
        headers=_write_headers(**{"If-Match": f'"{draft_id}:{version}"'}),
    )
    assert stale_retry.status_code == 412, (
        f"旧 ETag 重试应被 `require_if_match` 拦下（412），实际 "
        f"{stale_retry.status_code}：{stale_retry.text}"
    )
    assert stale_retry.json()["error"]["code"] == ErrorCode.VERSION_CONFLICT.value

    # ② 用**重读后拿到的** ETag 重试 ⇒ 409（状态判定拦下）。
    fresh_retry = client.post(
        f"{_draft_url(env)}/submissions",
        json={"reason": "重试（新 ETag）"},
        headers=_write_headers(**{"If-Match": f'"{draft_id}:{after_submit}"'}),
    )
    assert fresh_retry.status_code == 409, (
        f"新 ETag 重试应被状态判定拦下（409），实际 "
        f"{fresh_retry.status_code}：{fresh_retry.text}"
    )
    error = fresh_retry.json()["error"]
    assert error["code"] == ErrorCode.INVALID_TRANSITION.value
    assert error["details"]["reason"] == REASON_DRAFT_ALREADY_SUBMITTED

    # ③ 两次被拒的重试都不许产生第二个 PLAN 版本（这才是 409 存在的意义）。
    nodes = int(
        _scalar(
            env,
            "SELECT count(*) FROM version_nodes WHERE entity_type = 'PLAN'"
            " AND entity_id = :pid",
            {"pid": str(env.project_id)},
        )
        or 0
    )
    assert nodes == 1, f"重试产生了额外的 PLAN 版本节点（共 {nodes} 个）"


def test_plan_draft_submit_requires_if_match(
    client: TestClient, env: PlanningEnv
) -> None:
    """提交缺 `If-Match` ⇒ **428**（与保存同款，协议没要求但本包加的）。

    见 `api.py` 模块 docstring 第四节：`reason` 来自客户端屏幕，
    而固化进 `payload` 的比例与分配来自**服务端当时读到的草稿**。
    两者若来自不同版本，固化出来的 PLAN 会把"客户端以为的内容"与
    "服务端实际算出的数字"混在一起，且**不报错**（201、数字都对，
    只是不属于同一时刻）。因此这个前置条件是**语义正确性**要求，
    不是并发优化 —— 它必须与保存用同一个码。
    """
    _login(client, env)
    _read_draft(client, env)

    response = client.post(
        f"{_draft_url(env)}/submissions",
        json={"reason": "缺 If-Match"},
        headers=_write_headers(),
    )
    assert response.status_code == 428, response.text
    assert response.json()["error"]["code"] == ErrorCode.PRECONDITION_REQUIRED.value
    assert response.json()["error"]["details"]["reason"] == "if_match_missing"


# ---------------------------------------------------------------------------
# 4.5 配置：读（登录即可，无需任何项目上下文）
# ---------------------------------------------------------------------------
#
# 这三个端点是 ADR-13 验证方式第 3 条「运营能在页面上自助确认当前生效值」
# 的落点。它们的共同特征是**不要求项目上下文、不要求管理权限** ——
# 因为那句话的主语是"运营"，而运营不是管理者。
#
# 因此本节的用例**只用 owner（`ROLE_OPERATOR`）与未登录两种身份**，
# 刻意不引入 admin：若某天有人给读端点加上组织级权限，owner 会 403
# 而这条当场报红 —— 这正是要守的。


def _settings_global_url() -> str:
    return f"{API_PREFIX}/settings/global"


def _setting_url(
    env: PlanningEnv, key: str = SETTING_KEY_BATCH_LIMITS
) -> str:
    """`PUT /settings/global/{key}` 的地址（`env` 只用于签名一致性）。"""
    del env
    return f"{API_PREFIX}/settings/global/{key}"


def _project_setting_url(
    env: PlanningEnv,
    key: str = SETTING_KEY_BATCH_LIMITS,
    *,
    project_id: uuid.UUID | None = None,
) -> str:
    return f"{API_PREFIX}/settings/projects/{project_id or env.project_id}/{key}"


def test_global_settings_overview_requires_login(
    client: TestClient, env: PlanningEnv
) -> None:
    """未登录读全局配置 ⇒ **401**。

    这条与草稿那条（`test_plan_draft_read_requires_login`）守的是同一件事，
    但**成本完全不同**：草稿的 GET 会写库（懒创建），配置总览的 GET
    是**真安全方法**（`materialize=False` 且 `project_id=None`）。
    两条都留，是因为"读端点必须登录"这条规则在每个端点上都要成立，
    而漏一个的表现是"功能正常、测试也过"。
    """
    response = client.get(_settings_global_url())
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == ErrorCode.SESSION_EXPIRED.value


def test_global_settings_overview_lists_every_key_and_placeholder_state(
    client: TestClient, env: PlanningEnv
) -> None:
    """全局总览：**未配置也必须在响应里**（占位键的最要紧状态）。

    ## 这条用例防的是"按表里有什么就返回什么"

    `platform.mediaLimits` 是占位键（OP-64 待实测），它的**合法状态就是
    没有值**。若端点按"表里已有行"组装，这个键会从响应里**消失** ——
    于是"有个键还没填值"这件事在页面上无处可见，而这正是最需要
    被看见的信息（`SettingOverviewBody` 的 docstring 已论证）。

    因此断言 `entries` 的键集合 **恰好等于** `SETTING_KEYS`
    （不是"包含"）—— 多了少了都报红：
      · 少了 ⇒ 按表组装（本用例要拦的那个实现）；
      · 多了 ⇒ 有人往 `SETTING_KEYS` 里加键却没更新这里（也是要拦的）。

    ## 两个状态都要测（同一个用例内，只发一次登录）

    | 时刻 | `configured` | `batchLimits` | 断言它防什么 |
    |---|---|---|---|
    | 造全局值之前 | `False` | `null` | "安静地把未配置报成已配置"（前端会显示一个假的 10/30/50） |
    | 造全局值之后 | `True` | `{trial:10,scale:30,bulk:50}` | `batchLimits` 投影没跟着真值走（前端渲染的是硬编码默认） |

    `batchLimits` 的**三档字段名**一并钉住（`trial`/`scale`/`bulk`）：
    它是给前端直接绑输入框的派生字段，键名漂移会让三个框全部空白，
    而响应里其它字段看起来都正常。
    """
    _login(client, env)

    before = client.get(_settings_global_url())
    assert before.status_code == 200, before.text
    overview = before.json()["data"]

    keys = {entry["key"] for entry in overview["entries"]}
    assert keys == set(SETTING_KEYS), (
        f"总览的键集合必须恰好是 SETTING_KEYS，实际 {sorted(keys)} "
        f"（期望 {sorted(SETTING_KEYS)}）"
    )

    placeholders = [e for e in overview["entries"] if e["isPlaceholder"]]
    assert [e["key"] for e in placeholders] == sorted(PLACEHOLDER_SETTING_KEYS)
    assert placeholders[0]["configured"] is False, (
        "占位键在没有任何行时应报 configured=false（把『尚未配置』"
        "报成已配置会让前端显示一个假的上限）"
    )
    assert placeholders[0]["value"] is None
    assert placeholders[0]["revisionNo"] is None
    assert overview["batchLimits"] is None, (
        "未配置时 batchLimits 必须是 null —— 返回半成品对象（缺档）"
        "会让前端渲染出空的输入框而不报错"
    )

    # 造出全局值后重读：同一个端点在两种状态下都必须说真话。
    _seed_global(env)

    after = client.get(_settings_global_url())
    assert after.status_code == 200, after.text
    by_key = {entry["key"]: entry for entry in after.json()["data"]["entries"]}
    batch_entry = by_key[SETTING_KEY_BATCH_LIMITS]
    assert batch_entry["configured"] is True
    assert batch_entry["isPlaceholder"] is False
    assert batch_entry["value"] == DEFAULT_BATCH_MAX
    assert batch_entry["revisionNo"] == 1

    limits = after.json()["data"]["batchLimits"]
    assert limits == {
        "trial": DEFAULT_BATCH_MAX[BATCH_KIND_TRIAL],
        "scale": DEFAULT_BATCH_MAX[BATCH_KIND_SCALE],
        "bulk": DEFAULT_BATCH_MAX[BATCH_KIND_BULK],
    }, f"batchLimits 投影与真值不一致：{limits}"

    # 其余两个键**不受影响**（防止"给一个键造值把别的键也点亮"）。
    other = by_key[SETTING_KEY_RESEARCH_MAX_FILES]
    assert other["configured"] is False, (
        "给 batch.limits 造值不应让 research.maxEffectiveFiles 变成已配置"
    )


def test_global_setting_history_requires_login(
    client: TestClient, env: PlanningEnv
) -> None:
    """未登录读变更历史 ⇒ 401（审计读数不是公开信息）。"""
    response = client.get(f"{_setting_url(env)}/history")
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == ErrorCode.SESSION_EXPIRED.value


def test_global_setting_history_covers_both_scopes_and_rejects_unknown_key(
    client: TestClient, env: PlanningEnv
) -> None:
    """历史端点：**含全局行与项目行**，按修订号**升序**；未知键 ⇒ 422。

    ## 为什么要断言"两种 scope 都在"

    ADR-13 把"审计完整"列为验收条件（不可篡改由触发器保证，
    但"改了什么能被读出来"必须有出口）。而"这个键被谁改过"这个问题
    的回答里，**全局改动与某个项目的覆盖是同一件事的两半** ——
    只报全局会让"某项目被单独调过"这件事查不出来，
    而"某个项目悄悄偏离了全局"正是 OP-60 拍板要暴露的风险。

    ## 顺序断言用"非降序"而不是写死的修订号

    两行（全局 1 行、项目 1 行）的 `revision_no` **都是 1** ——
    修订号是**按 `(key, scope, project)` 各自取号**的（`max_revision_no`
    的 where 条件含 scope），因此"全局 1、项目 1"是正常的，不是重复。
    写死顺序会在两者 `created_at` 相同时（同一事务内 `now()` 相同）
    变成一条脆弱的判据。这里只断言"升序"这个契约本身。

    ## 未知键 422 **而不是**空列表

    空列表会把"键名拼错"表现成"这个键从没被改过"，而运营据此会以为
    "配置没被谁动过" —— 与 `diverged` 端点的口径**刻意不同**
    （见下一条用例的实测记录）。
    """
    _login(client, env)

    _seed_global(env)
    with _repo(env) as repo:
        pl_settings.change_project_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            project_id=env.project_id,
            value={"TRIAL": 5, "SCALE": 30, "BULK": 50},
            reason="本项目收紧试产上限",
            actor_id=env.owner_id,
        )

    response = client.get(f"{_setting_url(env)}/history")
    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    assert len(rows) == 2, f"期望 1 行全局 + 1 行项目，实际 {rows}"

    scopes = {row["scopeKind"] for row in rows}
    assert scopes == {SETTING_SCOPE_GLOBAL, SETTING_SCOPE_PROJECT}, (
        f"历史必须覆盖两种作用域（只报全局会漏掉『某项目被单独调过』），"
        f"实际 {scopes}"
    )

    project_rows = [r for r in rows if r["scopeKind"] == SETTING_SCOPE_PROJECT]
    assert project_rows[0]["projectId"] == str(env.project_id)
    assert project_rows[0]["origin"] == SETTING_ORIGIN_MANUAL
    assert project_rows[0]["value"] == {"TRIAL": 5, "SCALE": 30, "BULK": 50}

    revisions = [row["revisionNo"] for row in rows]
    assert revisions == sorted(revisions), (
        f"历史必须按 revisionNo 升序（时间线由旧到新），实际 {revisions}"
    )

    # 审计四要素在 HTTP 层真的出现了（ADR-13 逐条对应）。
    global_row = next(r for r in rows if r["scopeKind"] == SETTING_SCOPE_GLOBAL)
    assert global_row["reason"] == "WP24 测试全局默认值"
    assert global_row["createdAt"], "createdAt 不能为空"
    assert "actorId" in global_row and "requestId" in global_row

    unknown = client.get(f"{_setting_url(env, 'batch.limt')}/history")
    assert unknown.status_code == 422, (
        f"未知键应返回 422（拼错键必须报错，不能表现为『这个键没被改过』），"
        f"实际 {unknown.status_code}：{unknown.text}"
    )
    assert (
        unknown.json()["error"]["details"]["reason"]
        == REASON_SETTING_KEY_UNKNOWN
    )


def test_diverged_projects_defaults_to_batch_limits_key(
    client: TestClient, env: PlanningEnv
) -> None:
    """`GET /settings/projects/diverged` 不传 `key` ⇒ **默认 `batch.limits`**。

    ## 为什么默认键要用"另一个键造了分叉"来验证

    只造 `batch.limits` 的分叉、然后断言"它出现在清单里"，无法区分
    "默认值是 `batch.limits`"与"默认值是『全部键』"这两种实现。
    这里再造一个 `research.maxEffectiveFiles` 的分叉，断言它
    **不在**不带参数的响应里 —— 于是"默认键"这个事实被夹住了。

    ## 同一条用例也覆盖 §3.6.4 的"值相同不算分叉"

    `MANUAL` 但值恰好等于全局的项目必须**不在**清单里。否则清单会被
    "所有被覆盖过的项目"填满（运营覆盖成同值是常见操作），
    而管理者就失去了"看哪几个真的不一样"这个能力 ——
    那正是 `diverged` 这个字段与这个端点存在的全部理由。
    """
    _login(client, env)

    global_value = {"TRIAL": 10, "SCALE": 30, "BULK": 50}
    _seed_global(env, value=global_value)
    _seed_global(
        env,
        key=SETTING_KEY_RESEARCH_MAX_FILES,
        value={"maxEffectiveFiles": DEFAULT_RESEARCH_MAX_EFFECTIVE_FILES},
        reason="WP24 测试：研究文件上限",
    )

    with _repo(env) as repo:
        # 项目 A：真分叉（batch.limits）
        pl_settings.change_project_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            project_id=env.project_id,
            value={"TRIAL": 2, "SCALE": 30, "BULK": 50},
            reason="A 收紧试产",
            actor_id=env.owner_id,
        )
        # 项目 B：MANUAL 但值相同 ⇒ 不算分叉
        pl_settings.change_project_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            project_id=env.other_project_id,
            value=dict(global_value),
            reason="B 覆盖成与全局相同",
            actor_id=env.owner_id,
        )

    with _repo(env) as repo:
        # 另一个键上也造一个真分叉（用于验证默认键不是"全部键"）。
        pl_settings.change_project_setting(
            repo,
            key=SETTING_KEY_RESEARCH_MAX_FILES,
            project_id=env.project_id,
            value={"maxEffectiveFiles": 50},
            reason="研究文件上限收紧",
            actor_id=env.owner_id,
        )

    response = client.get(f"{API_PREFIX}/settings/projects/diverged")
    assert response.status_code == 200, response.text
    rows = response.json()["data"]

    # ⚠️ `projectId` 在 JSON 里是**字符串**，而 `env.project_id` 是 `UUID`
    # —— `UUID in {str: ...}` 恒为 False（第一版就在这里假失败，
    # 失败信息读起来像"分叉没被返回"）。统一按字符串比较。
    by_project = {row["projectId"]: row for row in rows}
    this_id = str(env.project_id)
    other_id = str(env.other_project_id)
    assert this_id in by_project, (
        "真分叉的项目不在清单里 —— 而『分叉了没人知道』正是这个端点要消灭的状态"
    )
    assert other_id not in by_project, (
        "MANUAL 但值等于全局的项目出现在清单里 —— 清单会被噪音填满"
    )

    row = by_project[this_id]
    assert row["source"] == SETTING_ORIGIN_MANUAL
    assert row["value"] == {"TRIAL": 2, "SCALE": 30, "BULK": 50}
    assert row["globalValue"] == global_value, (
        "globalValue 必须回带 —— 没有它前端渲染不出『本项目 2 · 全局 10』"
    )
    assert row["revisionNo"] >= 1

    # ★ 默认键的夹逼：带 key 查 research 时分叉在，不带 key 时不在。
    research = client.get(
        f"{API_PREFIX}/settings/projects/diverged",
        params={"key": SETTING_KEY_RESEARCH_MAX_FILES},
    )
    assert research.status_code == 200, research.text
    research_rows = {r["projectId"]: r for r in research.json()["data"]}
    assert this_id in research_rows, "带 key 查 research 时分叉必须出现"
    assert research_rows[this_id]["value"] == {"maxEffectiveFiles": 50}, (
        "带不同 key 查询时返回的值必须换成那个键的值（否则前端会显示错的值）"
    )
    assert this_id in by_project, (
        "不带 key 时只应返回 batch.limits 的分叉；本断言与上一条共同证明"
        "『默认键』不是『全部键』"
    )
    assert research_rows[this_id]["value"] != by_project[this_id]["value"], (
        "两次查询（默认键 / research 键）返回了同一个值 —— "
        "说明 key 参数没有真正参与查询"
    )


def test_diverged_projects_does_not_reject_unknown_key(
    client: TestClient, env: PlanningEnv
) -> None:
    """★ **已登记的口径不一致**：`diverged` 对未知键**不报错**（返回空清单）。

    姊妹端点 `GET /settings/global/{key}/history` 对同一个未知键
    **返回 422**（它经 `get_effective_limits`，那里有 `SETTING_KEYS` 判定）。
    而 `list_diverged_projects` 直接查库比对、**不做键校验**，
    于是未知键得到"没有项目分叉"这个**看起来正常**的答案。

    这条用例把**实际行为**钉住（而不是把它写成期望行为）：它的价值在于
    · 若有人给 `diverged` 补上键校验，这条会报红 —— 那一刻必须**同时**
      更新本 docstring 与 WP24 文档的登记条目，而不是悄悄改掉；
    · 它同时是一份"两个同级读端点的口径可以不一致"的实证记录 ——
      排障时"为什么这个端点 422、那个端点 200 空列表"有据可查。

    **为什么不现在就修**：修法有两种（`diverged` 改为 422，或 `history`
    改为空列表），两者都是**契约层变更**（改变前端可观察行为），
    不属于本包单方面能决定的（与 `api.py` 第五节"配置端点的 If-Match"
    同一处置口径）。已登记收口事项，由协议侧裁定。
    """
    _login(client, env)

    response = client.get(
        f"{API_PREFIX}/settings/projects/diverged",
        params={"key": "batch.limt"},
    )
    assert response.status_code == 200, (
        f"当前实现不校验未知键（已登记）；若返回 {response.status_code}，"
        "说明有人补了校验 —— 请同步更新本 docstring 与文档登记条目"
    )
    assert response.json()["data"] == []


def test_overview_reads_each_setting_key_exactly_once(
    client: TestClient, env: PlanningEnv
) -> None:
    """**MINOR-6**：总览端点**每个键只查一次库**（不是"查完再单独补一次"）。

    ## 这条判据防的是一处"同键两次往返"

    初版 `_overview_body` 在遍历 `SETTING_KEYS` 的循环之外，**又**查了一次
    `batch.limits` 拿去构造 `BatchLimitsBody` —— 同一个键两次往返。
    当时的注释还写着"顺手复用"，而实现并没有复用。

    两次结果在**同一事务内**必然相同，因此它**不会**表现为功能错误：
    响应完全正确，只是多一次查询。但它是"将来给读取加缓存"的定时炸弹 ——
    那时同一响应里的两个字段（`entries[batch.limits].value` 与
    `batchLimits`）会出现分歧，而症状是**同一个响应自相矛盾**，
    这是最难排查的一类。

    ## 为什么敢用 `==`（本项目铁律 #1 说"总数一律 `>=` 下界"）

    那条铁律针对的是**跨模块、会随别人开发而变大**的计数（"全项目恰好 8 个路由"）。
    这里数的是**单个端点在单次请求内的语句条数** —— 它只由本包实现决定，
    且**两个方向都对应真实缺陷**：

      · **多于** `len(SETTING_KEYS)` ⇒ 有键被查了两次（本 MINOR 的形态）；
      · **少于** `len(SETTING_KEYS)` ⇒ 有键的值**不是从库里读的**
        （硬编码默认值），那正是上一条用例要拦的"假配置"。

    因此这是一个双向都能 FAIL 的判据，不是"断言实现恰好这样"。

    ## 实测

    本机实测该请求的 `runtime_settings` 相关语句恰好 **3 条**
    （`batch.limits` / `research.maxEffectiveFiles` / `platform.mediaLimits`
    各一次），与 `len(SETTING_KEYS) == 3` 相等。登录链的
    `user_sessions` / `users` 两条不计入（`_measure_statements` 只数
    含 `RUNTIME_SETTINGS` 的语句）。

    ## 反向验证

    把 `_overview_body` 里 `values_by_key` 的复用去掉、改回循环后再查一次
    `batch.limits`，本判据必须报红（会变成 4 条）。
    """
    _login(client, env)
    _seed_global(env)

    def _read_overview() -> None:
        response = client.get(_settings_global_url())
        assert response.status_code == 200, response.text

    counted = _measure_statements(env, _read_overview)

    assert counted["select_runtime"] == len(SETTING_KEYS), (
        f"总览请求读了 {counted['select_runtime']} 次 runtime_settings"
        f"（应为每个键恰好一次 = {len(SETTING_KEYS)}）—— "
        f"多了说明有键被重复查询（MINOR-6 回归），"
        f"少了说明有键的值不是从库里来的"
    )
    assert counted["insert_runtime"] == 0, (
        "全局总览是**只读**端点（materialize=False + project_id=None），"
        "不许有写副作用"
    )


def test_history_reads_rows_exactly_once(
    client: TestClient, env: PlanningEnv
) -> None:
    """**MINOR-7**：历史端点只查**一次** `runtime_settings`（已知键路径）。

    ## 这条判据防的是"用读一次来当键名校验"

    初版的 `get_history` 里写着：

        settings_service.get_effective_limits(repo, key, None, materialize=False)
        settings_service.ensure_setting_key(key)          # 修复后才加
        return single_response(_history_body(repo, key=key))

    第一行的返回值**被直接丢弃** —— 它的唯一作用是"顺带校验键名"
    （`get_effective_limits` 会调 `ensure_setting_key`）。于是每次读历史
    都白跑一条 `current_global_setting`，随后 `list_history` 再查一条
    ⇒ **2 条**。修复后只剩 `list_history` 那一条。

    ## ★ 为什么不能拿"未知键"来测这一条（我第一版就是这么写的，恒真）

    第一版的判据是"未知键 ⇒ 0 条 `runtime_settings` 查询"，
    并声称反向验证会报红。**实测不报红** —— 因为旧实现里
    `get_effective_limits` 的**第一行就是** `ensure_setting_key`：

        def get_effective_limits(repo, key, ...):
            ensure_setting_key(key)        # ← 未知键在这里就抛了
            global_row = repo.current_global_setting(key=key)   # 走不到

    即：**未知键在两种实现下都是 0 条**（抛错发生在查库之前）。
    一条对"正确实现"与"缺陷实现"给出相同结果的断言**是恒真式**，
    它不守卫任何东西 —— 这正是本项目"错误 ⑪"的同类（判据自身缺陷），
    只是这次藏得更深：判据看起来在测一个真实缺陷，实际测的是
    "异常的抛出点恰好在查库之前"这个与缺陷无关的巧合。

    因此判据改成**已知键**：缺陷实现 2 条、正确实现 1 条，**两向可分**。

    ## 为什么断言 `== 1` 而不是 `<= 1`

    `list_history` 必须真的查（返回空列表也要有一次查询）——
    断言 `<= 1` 会被一个"返回常量空列表、根本不查库"的实现通过，
    而那会让整个端点变成假的。`== 1` 双向都拦。

    ## 实测与反向验证

    正确实现：1 条（`list_history`）。反向验证把 `ensure_setting_key(key)`
    换回 `get_effective_limits(..., materialize=False)` 后：
    **2 条** ⇒ 报红（已实测确认）。
    """
    _login(client, env)
    _seed_global(env)

    def _read_history() -> None:
        response = client.get(f"{_setting_url(env)}/history")
        assert response.status_code == 200, response.text

    counted = _measure_statements(env, _read_history)

    assert counted["select_runtime"] == 1, (
        f"读历史用了 {counted['select_runtime']} 条 runtime_settings 查询"
        f"（应恰好 1 条 = `list_history`）—— 多了说明键名校验又退化成了"
        f"「先读一次再判」，那条查询的返回值会被直接丢弃"
    )

    # 未知键的行为仍然要钉住（它是契约：422 而不是空列表）。
    # ⚠️ 但它**不是**上面那条判据的依据 —— 见本用例 docstring。
    unknown = client.get(f"{_setting_url(env, 'batch.limt')}/history")
    assert unknown.status_code == 422, unknown.text
    assert (
        unknown.json()["error"]["details"]["reason"] == REASON_SETTING_KEY_UNKNOWN
    )


def test_project_level_setting_read_has_no_endpoint(
    client: TestClient, env: PlanningEnv
) -> None:
    """**MAJOR-3 的可达性边界**：项目级配置**没有 HTTP 读出口**。

    ## 为什么要把"没有"写成一条判据

    MAJOR-3 的修复（`get_effective_limits` 读取侧校验项目存在 ⇒ 404）
    **只从服务层可达** —— 本包 router 的 8 个端点里，带 `{projectId}`
    的只有 `/projects/{projectId}/plan-draft` 三兄弟（草稿），
    `/settings/projects/...` 下**只有 `diverged`（跨项目清单）与
    `{projectId}/{key}` 的 PUT**，没有"读某个项目的某个配置键"这条路径。

    因此**不能**为读取侧写一条 HTTP 判据 —— 写了就是在测一个不存在的端点。
    但"不存在"本身会随时间变化：某天有人补上这个读端点，
    就必须**同时**补它自己的 404 判据（否则那条新路径上的幽灵项目
    会退回到 FK 23503 ⇒ 500）。这条用例就是那个提醒的机械形式。

    ## 实测记录：`GET /settings/projects/{ghost}/{key}` 返回的是 **400**

    顺手实测了这个"不存在的读端点"，得到一个反直觉的结果：
    **400（`MALFORMED_REQUEST`）而不是 405**。成因不在本包：

        devtools 的 `Mount("/")`（`main.py` 第 398 行）会兜住未匹配的请求
        → `StaticFiles` 对非 GET 方法抛 405
        → `errors._CODE_BY_STATUS` 里**没有 405 条目**（第 129~142 行）
        → 被折成 `MALFORMED_REQUEST` ⇒ 400

    该行为在 `test_devtools.py` 第 443 行有同款登记，且**刻意不修**
    （引入 405 映射会改变"未挂载"时既有的一批契约行为）。
    把它记在这里，是为了让"为什么不是 405"在排障时不用重新推一遍。

    ## 写侧的等价路径确实会 404（且由**依赖**先拦，服务层校验是第二道）

    实测 `PUT /settings/projects/{ghost}/{key}` ⇒ **404 + `projectId`**，
    但语句构成只有 3 条（`user_sessions` / `users` / `projects`）——
    **没有** `runtime_settings` 的任何查询。这说明拦下它的是
    `ProjectAccessDep` 的 `resolve_project_access`（查 `projects` 表
    取不到 ⇒ 404），**不是**服务层 `change_project_setting` 里的
    `project_exists`。

    即：HTTP 路径上服务层那道校验被依赖**遮蔽**了 ——
    这不是缺陷（双层防线，依赖在前），但它解释了为什么
    `test_get_effective_limits_rejects_missing_project` 必须直接调服务层：
    **只有绕开依赖才够得到那道校验**。
    """
    from rednote.modules.planning.api import router as planning_router

    own_gets = {
        route.path
        for route in planning_router.routes
        if "GET" in (getattr(route, "methods", set()) or set())
    }
    assert "/settings/projects/{projectId}/{key}" not in own_gets, (
        "本包新增了项目级配置读端点 —— 必须同时补两条判据："
        "① 幽灵 project_id ⇒ 404 + projectId；"
        "② 该路径不应落到 devtools 的 400（见本用例 docstring 的实测记录）。"
        "补完再把这条断言改成新的期望。"
    )
    assert "/settings/projects/diverged" in own_gets, (
        "跨项目分叉清单（§3.6.4）必须**在**读出口里 —— "
        "它消失意味着「哪些项目偏离了全局」又没有地方可看了"
    )

    # writer 侧：幽灵项目 ⇒ 依赖层 404（不是 500、也不是 403）。
    _login(client, env)
    ghost = uuid.uuid4()
    blocked = client.put(
        f"{API_PREFIX}/settings/projects/{ghost}/{SETTING_KEY_BATCH_LIMITS}",
        json={"value": dict(DEFAULT_BATCH_MAX), "reason": "幽灵项目的覆盖"},
        headers=_write_headers(),
    )
    assert blocked.status_code == 404, (
        f"幽灵项目写配置应得 404（与「无权访问」不可区分，防枚举），"
        f"实际 {blocked.status_code}：{blocked.text}"
    )
    assert blocked.json()["error"]["code"] == ErrorCode.RESOURCE_NOT_FOUND.value
    assert blocked.json()["error"]["details"]["projectId"] == str(ghost), (
        "404 的 details 必须回带 projectId —— 前端要靠它定位是哪个项目"
    )


# ---------------------------------------------------------------------------
# 4.6 配置：写（全局 = 组织级权限；项目级 = 项目 EDIT）
# ---------------------------------------------------------------------------


def test_change_global_setting_requires_login(
    client: TestClient, env: PlanningEnv
) -> None:
    """★ 全局写端点**只能测 401** —— 本环境造不出 403。

    ## 为什么这条用例只断言 401（不是漏了 403 的用例）

    `PUT /settings/global/{key}` 的依赖是
    `require_org_action(OrgAction.KNOWLEDGE_MANAGE)`，而
    `_ORG_MATRIX` 里 `KNOWLEDGE_MANAGE` 对 **`ROLE_ADMIN` 与 `ROLE_OPERATOR`
    都开放**（它是运营**唯一**开放的组织级操作 —— 正因为如此，
    `api.py` 才选它而不是 `MANAGE_DICTIONARY`）。

    本 environment 只造得出这两种角色 ⇒ **不存在一个"已登录但无权限"的
    用户**。硬造会得到 200，而失败信息看起来像"权限没生效"。

    ⚠️ 因此这条用例的断言是 401 **而不是 403**。若某天 `_ORG_MATRIX`
    把 `KNOWLEDGE_MANAGE` 从运营手里收回，本用例仍会通过（它只测未登录）
    —— 那时需要**新增**一条 403 用例，而不是改这一条。
    这个射程边界写在这里，免得后人以为"权限已经测过了"。
    """
    response = client.put(
        _setting_url(env),
        json={"value": dict(DEFAULT_BATCH_MAX), "reason": "未登录尝试"},
        headers=_write_headers(),
    )
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == ErrorCode.SESSION_EXPIRED.value


def test_change_global_setting_succeeds_and_cascades_to_followers(
    client: TestClient, env: PlanningEnv
) -> None:
    """★ 全局写：200 + 响应四件套 + **同事务级联刷新未覆盖的项目**。

    ## 为什么级联的语义断言留在第 2 层，这里只断言"接线通了"

    本文件第 2 层已用**穷举**验证过 J2（"全局变更后所有 `INHERITED` 项目
    的生效值 == 新全局值"，逐项目比对，不抽样）。这里重复穷举没有收益，
    而**代价是实打实的**：测试库里有 M1/M2 各包累积的 1200 个左右项目，
    一次 `change_global_setting` 会写 1200+ 行（本机实测 1.09s / 1184 行，
    成本与项目数线性）。第 4 层跑很多次会让整个文件从秒级变成十几秒。

    因此这里取**一个被级联到的项目**（`other_project_id`）做见证：
    它从未有过项目级行 ⇒ 走 §3.6.3 路径 c ⇒ 必须出现一行 `INHERITED`。
    **有**它即证明级联在 HTTP 路径上真的发生了（服务层用例已证明
    它对全部项目都发生）。

    ## 响应四件套为什么必须齐（即使全局视角上 `diverged` 恒为 false）

    `PUT /settings/global/{key}` 与 `PUT /settings/projects/...` 的响应
    **形状必须一致**，否则前端要写两个分支，而第二个分支迟早只剩一个在用
    （`api.py` 该端点 docstring 的论证）。`diverged=false` 在这里是
    **恒真**的（全局值不可能与自己不同），但它仍要出现在响应里。
    """
    _login(client, env)

    new_value = {"TRIAL": 12, "SCALE": 30, "BULK": 50}
    with _repo(env) as repo:
        followers_before = _setting_rows(
            env, key=SETTING_KEY_BATCH_LIMITS, project_id=env.other_project_id
        )
    assert followers_before == 0, "前置：见证项目还没有项目级行"

    response = client.put(
        _setting_url(env),
        json={"value": new_value, "reason": "放宽试产批次上限"},
        headers=_write_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()["data"]

    assert body["key"] == SETTING_KEY_BATCH_LIMITS
    assert body["configured"] is True
    assert body["value"] == new_value
    assert body["globalValue"] == new_value
    assert body["source"] == SETTING_ORIGIN_GLOBAL
    assert body["diverged"] is False, "全局视角上 diverged 恒为 false"
    assert body["projectId"] is None, "全局写的响应不应带 projectId"
    assert body["revisionNo"] == 1, "首次写入应为修订号 1"

    # 级联见证：从未有过项目级行的项目现在有了一份 INHERITED 副本。
    rows = _raw(
        env,
        "SELECT origin, value FROM runtime_settings"
        " WHERE key = :k AND scope_kind = 'PROJECT' AND project_id = :pid"
        " ORDER BY revision_no DESC LIMIT 1",
        {"k": SETTING_KEY_BATCH_LIMITS, "pid": str(env.other_project_id)},
    )
    assert rows, (
        "全局变更后，从未有过项目级行的项目仍没有副本 —— "
        "§3.6.3 路径 c 在 HTTP 路径上没有生效"
    )
    assert rows[0][0] == SETTING_ORIGIN_INHERITED
    assert rows[0][1] == new_value, (
        "副本的值必须是**新的**全局值（刷成旧值等于没刷）"
    )

    # 全局历史里必须留下这次变更（ADR-13 的审计出口）。
    history = client.get(f"{_setting_url(env)}/history")
    assert history.status_code == 200, history.text
    changed = [
        row
        for row in history.json()["data"]
        if row["scopeKind"] == SETTING_SCOPE_GLOBAL
    ]
    assert len(changed) == 1
    assert changed[0]["reason"] == "放宽试产批次上限"
    assert changed[0]["actorId"] == str(env.owner_id), (
        "审计里的操作人必须是这次请求的登录用户"
    )


def test_change_global_setting_rejects_invalid_bodies(
    client: TestClient, env: PlanningEnv
) -> None:
    """四条请求体反例 ⇒ **全部 422**，且 `details.reason` 各不相同。

    | 请求体 | 期望 reason | 谁拦的 |
    |---|---|---|
    | `value=null`（非占位键） | `SETTING_VALUE_INVALID` | `validate_setting_value` |
    | `value` 缺一档 | `SETTING_VALUE_INVALID` | `_validate_batch_limits` |
    | `value` 多一档 | `BATCH_KIND_UNKNOWN` | `_validate_batch_limits` |
    | `reason=""` | `CHANGE_REASON_REQUIRED` | `_require_reason` |
    | 路径 `key` 未知 | `SETTING_KEY_UNKNOWN` | `validate_setting_value` |

    ## 为什么这几条适合放在第 4 层（第 1/2 层已有同族用例）

    前三层的同族用例证明的是**规则本身**；这里证明的是**端点没有
    自己先拦一遍**。两者不是重复：若有人图省事在端点上加一层
    `if body.value is None: raise ...`，规则会变成两处副本 ——
    而后改的那处必然漂移。断言 `details.reason` 的具体取值就是
    在钉"错误码来自服务层那唯一一处"（那层用 `_validation_error`
    带 `reason`；FastAPI 自己的 422 则**没有** `details.reason`，
    而是 `detail` 列表 —— 两者的响应形状不同）。

    ## 全部是校验期失败 ⇒ **不产生级联**

    这五条都在取锁与级联之前抛错，因此每条都只花一次请求的时间。
    本用例是整个第 4 层里唯一"能测多个全局写反例而不付级联代价"的地方
    —— 这也是把它单独成条（而不是并进上一条）的理由。
    """
    _login(client, env)
    url = _setting_url(env)

    def _put(payload: dict[str, Any], *, path: str | None = None):
        return client.put(
            path or url, json=payload, headers=_write_headers()
        )

    cases: list[tuple[str, Any, str]] = [
        (
            "非占位键不允许清空",
            {"value": None, "reason": "试图清空"},
            REASON_SETTING_VALUE_INVALID,
        ),
        (
            "缺一档",
            {"value": {"TRIAL": 10, "SCALE": 30}, "reason": "少写的 BULK"},
            REASON_SETTING_VALUE_INVALID,
        ),
        (
            "多一档",
            {
                "value": {"TRIAL": 10, "SCALE": 30, "BULK": 50, "HUGE": 99},
                "reason": "拼错的档位名",
            },
            REASON_BATCH_KIND_UNKNOWN,
        ),
        (
            "理由是空串",
            {"value": dict(DEFAULT_BATCH_MAX), "reason": ""},
            REASON_CHANGE_REASON_REQUIRED,
        ),
    ]

    for label, payload, expected_reason in cases:
        response = _put(payload)
        assert response.status_code == 422, f"{label}：{response.text}"
        error = response.json()["error"]
        assert error["code"] == ErrorCode.VALIDATION_FAILED.value
        assert error["details"]["reason"] == expected_reason, (
            f"{label}：期望 reason={expected_reason}，"
            f"实际 {error['details'].get('reason')} —— 说明这一层的错误"
            "不是服务层的 `_validation_error` 给出的（可能是端点自己拦的）"
        )

    unknown_key = _put(
        {"value": dict(DEFAULT_BATCH_MAX), "reason": "键名拼错"},
        path=_setting_url(env, "batch.limt"),
    )
    assert unknown_key.status_code == 422, unknown_key.text
    assert (
        unknown_key.json()["error"]["details"]["reason"]
        == REASON_SETTING_KEY_UNKNOWN
    )

    # 五次失败都不许留下任何行（含全局行与级联副本）—— 缺这条的话，
    # 一个"先写再校验"的实现也能通过上面的响应码断言。
    remaining = _scalar(env, "SELECT count(*) FROM runtime_settings")
    assert int(remaining or 0) == 0, (
        f"被拒的请求污染了 runtime_settings（共 {remaining} 行）"
    )


def test_change_project_setting_writes_manual_override(
    client: TestClient, env: PlanningEnv
) -> None:
    """项目级写：200 + `source='MANUAL'` + `diverged` + 落库 origin。

    ## 三个断言各自防什么

    | 断言 | 防的实现缺陷 |
    |---|---|
    | 响应 `source=='MANUAL'` | 写成了 `INHERITED` ⇒ 下一次全局变更会**静默覆盖**运营的手工设定（J4 失效） |
    | 响应 `diverged is True` + `globalValue` 齐 | 前端渲染不出「本项目 2 · 全局 10」（§3.6.4 的原文诉求） |
    | 库里 `origin='MANUAL'` | 响应说 MANUAL 但落库是 INHERITED（"只改内存对象"的同族症状） |

    第三条不能省：前两条只看响应，而**J4 的成立取决于库里的列**
    （`change_global_setting` 的级联循环正是按 `origin` 判"跳不跳"）。

    ## 顺带钉住 `diverged` 与 `MANUAL` 不是同一件事

    本用例的覆盖值确实与全局不同（2 vs 10）⇒ `diverged=True`。
    而"`MANUAL` 但值相同 ⇒ `diverged=False`"由第 2 层
    `test_j3_same_value_override_is_not_diverged` 守住。
    两条合起来说明 `source` 与 `diverged` **都是必需的、不可互推**。
    """
    _login(client, env)

    global_value = {"TRIAL": 10, "SCALE": 30, "BULK": 50}
    _seed_global(env, value=global_value)
    custom = {"TRIAL": 2, "SCALE": 30, "BULK": 50}

    response = client.put(
        _project_setting_url(env),
        json={"value": custom, "reason": "本项目试产期只做 2 条"},
        headers=_write_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()["data"]

    assert body["source"] == SETTING_ORIGIN_MANUAL
    assert body["diverged"] is True
    assert body["value"] == custom
    assert body["globalValue"] == global_value
    assert body["projectId"] == str(env.project_id), (
        "项目级写的响应必须带 projectId（全局写的响应不给它）"
    )
    assert body["revisionNo"] == 1

    stored = _raw(
        env,
        "SELECT origin, value, project_id FROM runtime_settings"
        " WHERE key = :k AND scope_kind = 'PROJECT' AND project_id = :pid"
        " ORDER BY revision_no DESC LIMIT 1",
        {"k": SETTING_KEY_BATCH_LIMITS, "pid": str(env.project_id)},
    )
    assert stored, "响应 200 但库里没有项目级行"
    assert stored[0][0] == SETTING_ORIGIN_MANUAL, (
        "落库的 origin 不是 MANUAL —— 下一次全局变更会静默覆盖这次覆盖（J4 失效）"
    )
    assert stored[0][1] == custom
    assert str(stored[0][2]) == str(env.project_id)

    # 覆盖之后，全局变更**不得**动它（J4 的 HTTP 侧见证）。
    changed = client.put(
        _setting_url(env),
        json={"value": {"TRIAL": 11, "SCALE": 30, "BULK": 50}, "reason": "全局放宽"},
        headers=_write_headers(),
    )
    assert changed.status_code == 200, changed.text

    after = _raw(
        env,
        "SELECT origin, value FROM runtime_settings"
        " WHERE key = :k AND scope_kind = 'PROJECT' AND project_id = :pid"
        " ORDER BY revision_no DESC LIMIT 1",
        {"k": SETTING_KEY_BATCH_LIMITS, "pid": str(env.project_id)},
    )
    assert after[0][0] == SETTING_ORIGIN_MANUAL, (
        "全局变更后发现当前行不是 MANUAL —— 覆盖被级联冲掉了（J4 失效）"
    )
    assert after[0][1] == custom, (
        f"覆盖值被全局变更改写成了 {after[0][1]}（本应是 {custom}）"
    )


def test_change_project_setting_access_matrix(
    client: TestClient, env: PlanningEnv
) -> None:
    """项目级写的三档身份：未登录 **401** / 非成员 **404** / 管理者 **200**。

    ## 为什么是 404 而不是 403

    与草稿那条同款：非成员返回 403 等于告诉攻击者"这个项目存在，只是你
    无权"，于是可以用它枚举项目 id。`resolve_project_access` 让
    "无权访问"与"项目不存在"**不可区分**。

    因此本用例里 **404 是正确行为**，不是"权限没生效"。

    ## 为什么必须有 admin 那一档

    `outsider` 与 `admin` **都不是项目成员**，但一个 404、一个 200 ——
    差别只在角色。缺 admin 那条的话，"非成员一律 404"这个**错误实现**
    也能通过前两档；而那个实现会让管理者再也改不了任何项目的配置。

    ## 与全局写端点的不对称

    全局写用组织级依赖（`KNOWLEDGE_MANAGE`），因此**只能测 401**
    （见 `test_change_global_setting_requires_login` 的说明）；
    项目级写用 `ProjectAction.EDIT`，三种权限结果都造得出来。
    这处不对称是刻意的：改"我这个项目的批次上限"是项目级参数调整，
    与改方案内容同级，不该收进组织级权限（该端点 docstring 已论证）。
    """
    payload = {"value": {"TRIAL": 3, "SCALE": 30, "BULK": 50}, "reason": "权限矩阵"}

    # ① 未登录 ⇒ 401
    anonymous = client.put(
        _project_setting_url(env), json=payload, headers=_write_headers()
    )
    assert anonymous.status_code == 401, anonymous.text
    assert anonymous.json()["error"]["code"] == ErrorCode.SESSION_EXPIRED.value

    # ② 非成员运营 ⇒ 404（防项目存在性枚举）
    _login(client, env, who="outsider")
    blocked = client.put(
        _project_setting_url(env), json=payload, headers=_write_headers()
    )
    assert blocked.status_code == 404, (
        f"非成员应得 404（防枚举），实际 {blocked.status_code}：{blocked.text}"
    )
    assert blocked.json()["error"]["code"] == ErrorCode.RESOURCE_NOT_FOUND.value

    # ③ 管理者（同样不是项目成员）⇒ 200
    _login(client, env, who="admin")
    allowed = client.put(
        _project_setting_url(env), json=payload, headers=_write_headers()
    )
    assert allowed.status_code == 200, (
        f"管理者不是项目成员也应能改（admin 全权），实际 "
        f"{allowed.status_code}：{allowed.text}"
    )
    assert (
        allowed.json()["data"]["source"] == SETTING_ORIGIN_MANUAL
    ), "管理者的写入同样必须落 MANUAL"

    rows = _scalar(
        env,
        "SELECT count(*) FROM runtime_settings"
        " WHERE key = :k AND scope_kind = 'PROJECT'",
        {"k": SETTING_KEY_BATCH_LIMITS},
    )
    assert int(rows or 0) == 1, (
        f"两次被拒的请求污染了配置表（项目级行数 {rows}，应为 1）"
    )

