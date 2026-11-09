"""WP27 `media` 包的判据落点 —— 装配层 / 纯函数层 / 数据库行为层。

## 这个文件为什么必须存在（不只是"应该有一个测试"）

本包有三处文档**点名**了这个文件名作为防线入口：

| 点名处 | 原话大意 |
|---|---|
| `media/models.py` | "投影清空用例见 `test_media_wp27.py`" |
| `media/schemas.py` | 段落对位的行为判据 |
| `media/gateway_bridge.py` | 出网最小化的判据落点 |
| 两张 WP27 迁移 | "`tests/test_media_wp27.py` 覆盖唯一索引的**行为**" |

`tests/test_migration_roundtrip.py::test_guardrail_test_references_resolve_to_real_files`
把这几处点名变成了**机械判据**（ISS-169 第 4 次复发的对策）。因此在本文件落地之前，
全量测试里有 **3 条红**专门指着"这些注释点名的文件不存在" —— 那是判据在正确工作，
不是缺陷。本文件落地即解掉它们。

## 覆盖的三层，各自为什么必须在这里

### 第 1 层 装配（**不需要数据库**）

- `build_task_registry()` 必须注册 `MEDIA_GENERATE`。漏了不会报错，只会让生产 Worker
  走 `NO_HANDLER` 分支**静默判失败**（WP26 的 ISS-178 同款）。
- 它必须**不污染** `build_default_registry()`（`register` 返回新实例）。
- `ERROR_TARGET_MISSING` / `ERROR_ACTOR_MISSING` 的**取值**必须钉死字面量 ——
  它们是排障与告警的过滤口径，改名等于对外契约变更。
- `adapter=None` 必须造出 `UnavailableGenerator`（fail-closed，**一次出网都没有**）。

### 第 2 层 纯函数（**不需要数据库**）

- `assign_paragraph_refs` 的**顺序对位**（老大 2026-10-08 拍板）：1 段 `[0,0,0]`、
  2 段 `[0,1,1]`、≥3 段 `[0,1,2]`。这是"三张图各画哪一段"的**唯一**实现。
- `paragraph_ref_for_sort_order` 的**越界 fail-closed**（不回落末段 —— 那会让坏掉的
  身份键与合法的最后一张图**同形**）。
- `paragraph_refs_for_body` 的切段口径（按空行切、丢纯空白段、序号为过滤后下标）。

### 第 3 层 数据库行为（**需要真库**）

- `uq_media_items_job_id` 的**唯一性行为**：两条媒体行挂同一个 job 必须被拒。
  这是迁移 §二 的验收点 —— 只断言"索引存在"是不够的（存在但非唯一照样建得出）。
- `find_media_item_for_job` 的反查（接缝的另一半）与**反查不到时返回 `None`**。
- `ck_media_items_pending_regeneration_reason_length` 的长度行为。
- 4 条延迟 FK 的形态（deferrable + initially deferred）。

## ★ 判据的落点必须与"文档声称"分开

本文件的每一条都**不引用文档**，只引用**磁盘上的实现**（AST 读源码、真库读
`pg_constraint`）。文档写什么不影响任何一条的结论 —— 这是
`test_media_wp27.py` 存在的另一层意思：它是那几处文档声称的**第三遍核对**。

## 探针纪律（本仓反复踩过的三条）

1. **"被拒的结论正确 ≠ 被拒的原因正确"**：数据库探针一律断言 **sqlstate +
   约束名**，不只看"抛了异常"。
2. **一条语句报错会把事务打成 aborted**：每类探针**独立连接 + 独立事务**，
   最后 `rollback()` ⇒ 零残留。
3. **不断言"总个数"**：`version_nodes` 上挂着 6 个包的 `*_paired`，
   `pg_constraint` 里有别包的 FK。一律**点名**核对。
"""

from __future__ import annotations

import ast
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import Engine

from conftest import (
    cross_package_foreign_keys,
    drop_own_tables,
    truncate_targets,
)

# FK 目标表：**必须在 `create_all` 之前 import**，否则 SQLAlchemy 解析
# `ForeignKey` 时抛 `NoReferencedTableError`（WP26 夹具的同一处置）。
import rednote.modules.content.models  # noqa: F401
import rednote.modules.files.models  # noqa: F401
import rednote.modules.gateway.models  # noqa: F401
import rednote.modules.identity.models  # noqa: F401
import rednote.modules.jobs.models  # noqa: F401
import rednote.modules.projects.models  # noqa: F401
import rednote.modules.prompts.models  # noqa: F401
import rednote.modules.versions.models  # noqa: F401

from rednote.modules.identity.models import ROLE_OPERATOR
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.jobs.handlers import build_default_registry
from rednote.modules.jobs.models import STATE_FAILED
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.jobs.types import ClaimedJob
from rednote.modules.media import ddl as m_ddl
from rednote.modules.media import handlers as m_handlers
from rednote.modules.media import models as m_models
from rednote.modules.media import schemas as m_schemas
from rednote.modules.media import service as m_service
from rednote.modules.media import types as m_types
from rednote.modules.media.gateway_bridge import GatewayUnavailable
from rednote.modules.media.models import MediaItem, OWN_TABLES
from rednote.modules.media.repository import MediaRepository
from rednote.modules.media.schemas import SchemaViolation
from rednote.modules.media.service import UnavailableGenerator
from rednote.modules.media.types import (
    IMAGES_PER_CONTENT,
    KIND_MEDIA_GENERATE,
    MAX_REGENERATION_REASON_CHARS,
    REASON_PARAGRAPH_REF_UNKNOWN,
)
from rednote.modules.projects.models import Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.platform.configuration import Settings
from rednote.platform.database import get_engine, reset_engines, session_scope

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
TEST_DATABASE_URL_ENV = "REDNOTE_TEST_DATABASE_URL"

#: `services/backend/` 与 media 模块目录 —— AST 判据定位源码用。
#: 从本文件上溯定位，**不写死绝对路径**（写死会让整仓挪位置时判据静默指向
#: 不存在的目录 —— 而"静默指向空目录"与"没有违规"在输出上同形）。
SERVICES_BACKEND = Path(__file__).resolve().parents[1]
SRC_ROOT = SERVICES_BACKEND / "src"
MEDIA_DIR = SRC_ROOT / "rednote" / "modules" / "media"

#: 清理顺序（引用方在前）。⚠️ 只列本包表 —— 入向引用闭包由
#: `conftest.truncate_targets` 沿 `confrelid` 递归算出（不写死名单）。
#: WP28 起补入 `media_components` / `live_profiles`：不列入则 `create_all` 对已存在的表
#: 静默跳过，库里的旧结构（旧 CHECK / 旧触发器）会让断言跑在过期结构上
#: （变异验证抓到：放宽 `VERIFICATIONS` 后用例仍绿）。
DROP_ORDER = (
    "media_components",
    "live_profiles",
    "media_renditions",
    "media_versions",
    "media_generation_contexts",
    "reference_assets",
    "media_items",
)
_TABLE_NAMES = tuple(OWN_TABLES)


def _test_database_url() -> str:
    return os.environ.get(TEST_DATABASE_URL_ENV, DEFAULT_TEST_DATABASE_URL).strip()


def _make_settings(**overrides: object) -> Settings:
    payload: dict[str, Any] = {
        "database_url": _test_database_url(),
        "environment": "test",
    }
    payload.update(overrides)
    return Settings(**payload)


# ===========================================================================
# 建表夹具（module 级 + 每用例清空）
# ===========================================================================


@pytest.fixture(scope="module", autouse=True)
def _ensure_own_tables() -> Iterator[None]:
    """只建本包的 5 张表 + 8 个触发器（照 WP26 的 `_ensure_own_tables`）。

    ## 为什么 `create_all` 之前必须 import FK 目标表

    `Base.metadata.create_all(engine, tables=[...])` **只注册传入的那几张表**。
    本包 5 张表引用了 `projects` / `users` / `jobs` / `version_nodes` /
    `content_items` / `file_objects` / `prompt_templates` / `prompt_versions` /
    `model_profiles` —— 它们不在 `tables=` 里，漏 import 会让 SQLAlchemy
    解析 `ForeignKey` 时抛 `NoReferencedTableError`（且报错指向本文件）。
    """
    reset_engines()
    engine = get_engine(_make_settings())

    fks_before = cross_package_foreign_keys(engine, _TABLE_NAMES)
    dropped, kept = drop_own_tables(engine, DROP_ORDER)

    # ⚠️ `media.models.OWN_TABLES` 是**字符串元组**（与 WP26 的 Table 元组不同）
    #    ⇒ 必须先取出 Table 对象。直接传字符串会让 `create_all` 抛
    #    `AttributeError: 'str' object has no attribute 'name'`（实测）。
    MediaItem.metadata.create_all(
        engine,
        tables=[MediaItem.metadata.tables[name] for name in OWN_TABLES],
    )

    fks_after = cross_package_foreign_keys(engine, _TABLE_NAMES)
    assert fks_after == fks_before, (
        f"建表夹具改变了跨包外键集合：before={sorted(fks_before)} "
        f"after={sorted(fks_after)}（dropped={dropped} kept={kept}）"
    )

    # 目标清单必须含**入向引用闭包**（`media_versions` 又引用 `media_items` 等）。
    truncate_list = truncate_targets(engine, DROP_ORDER)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(truncate_list)))
        for (
            _table,
            func_ddl,
            trigger_ddl,
            drop_trigger,
            _drop_func,
        ) in m_ddl.all_trigger_ddl():
            conn.execute(text(drop_trigger))  # CREATE TRIGGER 不幂等
            conn.execute(text(func_ddl))
            conn.execute(text(trigger_ddl))
    yield


@pytest.fixture(autouse=True)
def _clean_own_rows() -> Iterator[None]:
    """**每个用例前**清空本包 5 张表（4 张是不可变表，只有 TRUNCATE 能清）。"""
    reset_engines()
    engine = get_engine(_make_settings())
    truncate_list = truncate_targets(engine, DROP_ORDER)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(truncate_list)))
    yield


# ===========================================================================
# 数据夹具：造一个可用的 project + owner（media 行必需）
# ===========================================================================


@dataclass(frozen=True, slots=True)
class MediaEnv:
    settings: Settings
    project_id: uuid.UUID
    owner_id: uuid.UUID


@pytest.fixture
def env() -> MediaEnv:
    settings = _make_settings()
    with session_scope(settings) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name="WP27 测试用户",
            role=ROLE_OPERATOR,
        )
        owner_id = user.id
    with session_scope(settings) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name=f"产品-{uuid.uuid4().hex[:6]}")
        session.add(product)
        session.flush()
        project = Project(
            project_code=f"WP27-{uuid.uuid4().hex[:8].upper()}",
            name="WP27 测试项目",
            brand_id=brand.id,
            product_id=product.id,
            owner_id=owner_id,
            target_delivery_count=3,
            # `projects.due_date` 是 NOT NULL（psql 实测）—— 漏它会得到
            # `IntegrityError: null value in column "due_date"`，而报错指向夹具。
            due_date=date(2030, 12, 31),
        )
        session.add(project)
        session.flush()
        ProjectRepository(session).add_owner_and_collaborators(
            project.id, owner_id=owner_id, collaborator_ids=()
        )
        project_id = project.id
    return MediaEnv(settings=settings, project_id=project_id, owner_id=owner_id)


def _seed_content_item(env: MediaEnv) -> uuid.UUID:
    """造一条 `content_items` 身份行（media 行的必填 FK 目标）。"""
    content_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO content_items (id, project_id, current_version_id,"
                " state, current_version_no, lock_version)"
                " VALUES (:c, :p, NULL, 'DRAFT', 0, 0)"
            ),
            {"c": str(content_id), "p": str(env.project_id)},
        )
    return content_id


def _seed_media_item(
    env: MediaEnv,
    content_id: uuid.UUID,
    *,
    sort_order: int = 0,
    job_id: uuid.UUID | None = None,
    pending_reason: str | None = None,
    pending_bindings: object | None = None,
) -> uuid.UUID:
    """插一行 `media_items`（`uq_media_items_content_sort` 要求 sort 唯一）。"""
    item_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO media_items (id, project_id, content_item_id,"
                " paragraph_ref, sort_order, state, current_version_no,"
                " created_by, lock_version, job_id, pending_regeneration_reason,"
                " pending_reference_bindings)"
                " VALUES (:id, :p, :c, :ref, :so, 'PENDING', 0, :by, 0, :job,"
                " :reason, CAST(:bindings AS jsonb))"
            ),
            {
                "id": str(item_id),
                "p": str(env.project_id),
                "c": str(content_id),
                "ref": "p0-00000000",
                "so": sort_order,
                "by": str(env.owner_id),
                "job": str(job_id) if job_id else None,
                "reason": pending_reason,
                "bindings": None if pending_bindings is None else json.dumps(pending_bindings),
            },
        )
    return item_id


def _seed_job(env: MediaEnv, content_id: uuid.UUID) -> uuid.UUID:
    """造一条真实的 `jobs` 行（`media_items.job_id` 的 FK 目标）。"""
    job_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO jobs (id, kind, queue, state, priority,"
                " idempotency_scope, idempotency_key, input_hash, project_id,"
                " content_id)"
                " VALUES (:id, :kind, :queue, 'QUEUED', 0, :scope, :key, :hash,"
                " :p, :c)"
            ),
            {
                "id": str(job_id),
                "kind": KIND_MEDIA_GENERATE,
                "queue": "media",
                "scope": "media-generate",
                "key": f"probe-{uuid.uuid4().hex[:12]}",
                "hash": f"h-{uuid.uuid4().hex[:12]}",
                "p": str(env.project_id),
                "c": str(content_id),
            },
        )
    return job_id


# ===========================================================================
# 第 1 层 —— 装配（不需要数据库）
# ===========================================================================


def test_task_registry_registers_media_generate() -> None:
    """★ `kind=MEDIA_GENERATE` 必须在注册表里 —— 否则任务静默判失败。

    注册表与入队点不在同一个文件：入队在 `media/api.py`，注册在
    `media/handlers.py`。漏注册**不报错** —— 生产 Worker 领到
    `MEDIA_GENERATE` 后走 `NO_HANDLER` 分支，把任务判成 `FAILED` 且
    `retryable=False`，排障时看到的是"没有处理器"，而代码里明明注册了。

    用 `in` 而不是 `==`：本包将来可能在同表加 kind（`==` 会让每次新增都要改断言）。
    """
    kinds = m_handlers.build_task_registry().registered_kinds()
    assert KIND_MEDIA_GENERATE in kinds, (
        f"`media.build_task_registry()` 没有注册 {KIND_MEDIA_GENERATE!r}，"
        f"实际注册的是 {sorted(kinds)}。入队点（`media/api.py`）会建出这个 kind "
        "的任务，而 Worker 找不到处理器 ⇒ `NO_HANDLER` ⇒ 任务被判失败且不可重试。"
    )


def test_task_registry_does_not_mutate_default() -> None:
    """本包的注册表是**新实例**，不污染 WP13 的默认集合。

    若有人改成 `build_default_registry()` 再原地 `register`，默认集合被污染，
    症状是**测试之间产生顺序依赖**（后跑的用例多了一个处理器），极难定位。
    """
    default_before = build_default_registry().registered_kinds()
    m_handlers.build_task_registry()
    assert build_default_registry().registered_kinds() == default_before, (
        "`media.build_task_registry()` 改动了 `build_default_registry()` 的集合 —— "
        "它必须返回新实例（`TaskHandlerRegistry.register` 的不可变语义）。"
    )


def test_error_codes_are_literal() -> None:
    """★ 两个 `ERROR_*` 的**取值**钉死字面量（不用常量自比 —— 那会两边同源恒绿）。

    它们是排障与告警的过滤口径（按 error_code 过滤日志），改名等于对外契约变更。
    先例：`test_content_wp26_exec.py:1205` 的同一处置。
    """
    assert m_handlers.ERROR_TARGET_MISSING == "MEDIA_GENERATE_TARGET_MISSING", (
        f"`ERROR_TARGET_MISSING` 实际是 {m_handlers.ERROR_TARGET_MISSING!r} —— "
        "它是排障口径，改名会让既有告警规则静默失配。"
    )
    assert m_handlers.ERROR_ACTOR_MISSING == "MEDIA_GENERATE_ACTOR_MISSING", (
        f"`ERROR_ACTOR_MISSING` 实际是 {m_handlers.ERROR_ACTOR_MISSING!r}。"
    )


def test_default_adapter_is_fail_closed() -> None:
    """★ `adapter=None` 时造出的是 `UnavailableGenerator`（**调用即抛**）。

    这条守的是"生产未接线时的唯一防线"：若有人把默认值改成能调的东西，
    "本地跑得通"会变成**一次真的出网调用**（ADR-10 明令避免的盲区）。

    直接测这个类的契约而不是绕处理器：绕处理器需要铺 billing / prompts 一整条链，
    而断言结论完全相同。
    """
    generator = UnavailableGenerator()
    with pytest.raises(GatewayUnavailable) as excinfo:
        generator.dispatch(
            media_item_id=uuid.uuid4(),
            content_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            task_kind="STATIC_IMAGE",
            model_profile_id=uuid.uuid4(),
            prompt_version_id=uuid.uuid4(),
            prompt="画一张静物图。",
            target_version_no=1,
        )
    # ★ 用**字面量**断言 reason（`UnavailableGenerator` 是 frozen+slots，
    #   类属性访问拿到的是 descriptor，不是默认值 —— WP26 实测踩过）。
    assert excinfo.value.reason == "GENERATOR_NOT_CONFIGURED", (
        f"未接线的生成器抛出的 reason 是 {excinfo.value.reason!r}，"
        "而排障口径约定 'GENERATOR_NOT_CONFIGURED'。"
    )


def test_configured_adapter_builds_gateway_generator(env: MediaEnv) -> None:
    """`adapter` 非 `None` ⇒ 造的是 `GatewayMediaGenerator`（二分支可区分）。

    只测 `None` 那一侧的话，`_build_generator` 被写成恒返回 `UnavailableGenerator`
    也能过 —— 而那个 bug 的症状是"生产接好线了却永远不出网"，且**不报错**。
    这里断言接线侧的类型 + `job_id` / `attempt_id` 透传（`gateway_bridge` 靠它们
    写 `provider_calls` 留痕，传错会让出网调用没有留痕）。

    ## 为什么用真 `JobRepository` 而不是假替身

    `_build_generator` 会用 `repo.session` 造 `GatewayRepository` ——
    用桩就要模拟 session 的构造契约，而那个契约（`GatewayRepository(session)`
    是否接受 `None`）本身**不是本用例要判的东西**。用真 session 一次到位，
    且本文件的 module 级夹具已经建好表。
    """
    adapter = object()  # `_build_generator` 只把它透传，不调用它
    claimed = ClaimedJob(
        job_id=uuid.uuid4(),
        kind=KIND_MEDIA_GENERATE,
        attempt_id=uuid.uuid4(),
        fence_token=1,
        attempt_no=1,
        worker_id="wp27-test",
        input_version_ids=(),
    )

    with session_scope(env.settings) as session:
        generator, _gateway_repo = m_handlers._build_generator(
            claimed, JobRepository(session), adapter=adapter,
            byte_source=None, output_schema_version=1, policy_version="v1",
        )

    assert isinstance(generator, m_service.GatewayMediaGenerator), (
        "给定 adapter 却造出了未接线的生成器 —— 生产接好线之后仍不会出网，且不报错。"
    )
    assert generator.job_id == claimed.job_id, (
        "`job_id` 没有透传到生成器 —— 出网调用会没有留痕（ADR-10 的盲区）。"
    )
    assert generator.attempt_id == claimed.attempt_id, "`attempt_id` 没有透传。"


# ===========================================================================
# 第 2 层 —— AST：翻码表必须覆盖**端点可达**的 reason（覆盖范围由磁盘决定）
# ===========================================================================


def _resolve_reason(expr: ast.expr) -> str | None:
    """把一个 reason 表达式**规范成字符串值**。

    两种形态：字符串字面量（`'PARAGRAPH_REF_UNKNOWN'`）与 `types` 里的常量名
    （`REASON_CONTENT_NOT_FOUND`）。**只认这两种** —— 别的形态（局部变量、
    拼接）返回 `None`，调用方把它当"解析不出来"，报红时点名。
    """
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.Name):
        value = getattr(m_types, expr.id, None)
        return value if isinstance(value, str) else None
    return None


def _http_by_reason_keys() -> set[str]:
    """从 `media/api.py` 的**源码**读出 `_HTTP_BY_REASON` 的键，并**解析成值**。

    ★ 键在源码里写的是**标识符**（`REASON_CONTENT_NOT_FOUND`），而消费它的是
    `.get(exc.reason)` —— `exc.reason` 是**字符串值**。直接把键的**写法**
    与 reason 的**值**相比是**两个不同的东西**（本判据初版正是这么写错的，
    实测报出 19 个假缺失）。因此必须经 `_resolve_reason` 解析成值。

    走 AST 而不是 import：`_HTTP_BY_REASON` 是模块私有名，且 import 会带上
    pydantic 模型的构造开销。AST 读的是**磁盘上的源码**，与运行时一致。
    """
    tree = ast.parse((MEDIA_DIR / "api.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "_HTTP_BY_REASON":
                assert isinstance(value, ast.Dict), "_HTTP_BY_REASON 不是字典字面量"
                return {
                    resolved
                    for k in value.keys
                    if (resolved := _resolve_reason(k)) is not None
                }
    raise AssertionError("`media/api.py` 里找不到 `_HTTP_BY_REASON`")


def _reasons_raised_in(module_name: str) -> set[str]:
    """某个 media 模块里**抛出**的 reason 值集合（`reason=` 关键字实参，解析成值）。

    ## 为什么只认 `reason=` 关键字实参

    本仓抛 reason 的地方是 `SchemaViolation(..., reason=…)` /
    `MediaGenerationError(..., reason=…)` / `_not_found(..., reason=…)` ——
    **全部**是关键字。用"扫所有大写字符串"会误收字段名、`SqlState`、
    文档字符串里的说明等（本判据初版正是这么写错的）。

    覆盖范围由磁盘决定（铁律 14）：扫的是**源码里真正抛出**的 reason，
    不是人手列的清单。
    """
    tree = ast.parse((MEDIA_DIR / module_name).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "reason":
                resolved = _resolve_reason(keyword.value)
                if resolved is not None:
                    found.add(resolved)
    return found


def test_http_by_reason_covers_every_reason_raised_in_media() -> None:
    """★ `_HTTP_BY_REASON` 必须覆盖 `api.py` / `schemas.py` 抛出的**每一个** reason。

    ## 这条为什么是必需的（而不是"顺手加的"）

    `_raise_api_error` 用的是 `_HTTP_BY_REASON.get(exc.reason)` **无默认值** ——
    漏登记的表现是 `KeyError` → **500**，而**不是**一句清晰的"未映射"消息。
    也就是说：漏一个 reason 会让一个业务 4xx 变成 500，
    而**没有任何测试**会因此红（除非它恰好触发了那条路径）。

    ## 覆盖对象为什么是"api + schemas"而不是全包

    `api.py` 会捕获并翻码的 reason 来自**它自身**与 **`schemas.py`**
    （端点的输入校验层）。其余模块（`service` / `handlers` / 两个 bridge）
    的 reason 走**任务路径**（`handlers` 折成 `HandlerOutcome.error_code`），
    **不经过** `_raise_api_error` —— 把它们也要求登记是**假阳性**。

    这条区分不是靠人写清单，而是靠**调用点事实**：api.py 里 `except
    SchemaViolation` 抓的就是 `schemas` 抛的那批。若将来 `api.py` 开始直接
    捕获 `service` 的异常，`service` 的 reason 应当一并纳入 —— 那时
    改的是本判据的**范围**，而范围缩小的表现（漏覆盖）会被下面第 ① 条钉住。
    """
    keys = _http_by_reason_keys()
    raised = _reasons_raised_in("api.py") | _reasons_raised_in("schemas.py")

    # ① 先钉住"扫到了足够多的东西"—— 否则 `raised` 为空/缩小时本判据恒绿。
    assert len(keys) >= 20, (
        f"`_HTTP_BY_REASON` 只解析出 {len(keys)} 个键（实测应为 20）—— "
        "解析方式或翻码表本身缩水了。"
    )
    assert len(raised) >= 10, (
        f"只从 api.py + schemas.py 扫到 {len(raised)} 个 reason（实测应 >= 10）—— "
        f"扫取方式大概率失效了（不是真的只有这么少）。实际：{sorted(raised)}"
    )

    # ② 覆盖判据。
    missing = sorted(raised - keys)
    assert not missing, (
        f"`api.py` / `schemas.py` 抛出的 reason 有 {len(missing)} 个**没有登记**在 "
        f"`_HTTP_BY_REASON` 里：{missing}。\n"
        "`_raise_api_error` 用 `.get(reason)` 无默认值 ⇒ 漏登记会让一个业务 4xx "
        "变成 500，且不会有任何测试因此红。"
    )

    # ③ 反向：翻码表的**每一个键**都必须是 `types` 里真实存在的 reason 值。
    #    防"键写错了名字/写了个永不出现的值" —— 那种键**永不命中**，
    #    而覆盖判据（②）只看方向"raised ⊆ keys"，抓不到多余的废键。
    known = {
        value
        for name, value in vars(m_types).items()
        if name.startswith("REASON_") and isinstance(value, str)
    }
    unknown = sorted(keys - known)
    assert not unknown, (
        f"`_HTTP_BY_REASON` 里有 {len(unknown)} 个键**不对应 `types` 里任何 "
        f"REASON_ 常量**：{unknown}。这种键永不命中 ⇒ 一个业务 4xx 仍会变成 500。"
    )


def test_paragraph_split_has_single_source_in_schemas() -> None:
    """★ 切段实现**只能有一处**（在 `schemas.py`），`service.py` 必须转出。

    `service.py` 长期是**未入库**的在途文件。若切段实现住在那里，
    以它为输入的一致性判据（`check_paragraph_split_parity.py`）在**干净检出**
    上必然 FAIL（`ModuleNotFoundError`）—— 判据被绑在一个还没进版本库的实现上
    （ISS-95 族）。因此 `schemas.py` 是唯一定义处，`service.py` 从它转出。

    判据走 AST：`schemas.py` 必须**定义**切段函数，`service.py` **不得**定义
    第二个 `_PARAGRAPH_SPLIT` 正则（那是第二份实现的形态）。
    """
    schemas_tree = ast.parse((MEDIA_DIR / "schemas.py").read_text(encoding="utf-8"))
    service_tree = ast.parse((MEDIA_DIR / "service.py").read_text(encoding="utf-8"))

    def _defines(tree: ast.Module, name: str) -> bool:
        return any(
            isinstance(n, ast.FunctionDef) and n.name == name for n in tree.body
        )

    def _assigns(tree: ast.Module, name: str) -> bool:
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == name:
                    return True
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == name:
                        return True
        return False

    assert _defines(schemas_tree, "paragraph_refs_for_body"), (
        "`media/schemas.py` 必须定义 `paragraph_refs_for_body` —— 它是切段的唯一实现，"
        "也是让 `check_paragraph_split_parity.py` 能在干净检出上跑的前提。"
    )
    assert not _assigns(service_tree, "_PARAGRAPH_SPLIT"), (
        "`media/service.py` 里出现了 `_PARAGRAPH_SPLIT` 正则 —— 那是**第二份切段实现**。"
        "切段只能有一处（`schemas.py`），`service.py` 必须从它转出。"
        "两份实现分叉的表现是「同一段正文在生成时与校验时得到不同的 ref」。"
    )


# ===========================================================================
# 第 3 层 —— 纯函数：顺序对位与执行期重算
# ===========================================================================


def _paragraphs(*texts: str) -> tuple[tuple[int, str, str], ...]:
    """从若干段文本造 `paragraph_refs_for_body` 形状的三元组（走真实实现）。"""
    body = "\n\n".join(texts)
    return m_schemas.paragraph_refs_for_body(body)


def test_assign_paragraph_refs_one_paragraph() -> None:
    """1 段 ⇒ `[0, 0, 0]`（三张都画唯一那段，同段多角度）。"""
    paragraphs = _paragraphs("唯一的一段正文。")
    refs = m_schemas.assign_paragraph_refs(paragraphs)
    assert len(refs) == IMAGES_PER_CONTENT == 3
    assert len(set(refs)) == 1, f"1 段时应三张同 ref，实际 {refs}"
    assert refs[0] == paragraphs[0][2]


def test_assign_paragraph_refs_two_paragraphs() -> None:
    """2 段 ⇒ `[0, 1, 1]`（第 3 张回落最后一段）。"""
    paragraphs = _paragraphs("第一段。", "第二段。")
    refs = m_schemas.assign_paragraph_refs(paragraphs)
    assert refs[0] == paragraphs[0][2]
    assert refs[1] == paragraphs[1][2]
    assert refs[2] == paragraphs[1][2], f"第 3 张应回落末段，实际 {refs}"


def test_assign_paragraph_refs_three_or_more() -> None:
    """≥3 段 ⇒ `[0, 1, 2]`（前 3 段各一张，第 4 段起不出图）。"""
    paragraphs = _paragraphs("一。", "二。", "三。", "四。")
    refs = m_schemas.assign_paragraph_refs(paragraphs)
    assert refs == (paragraphs[0][2], paragraphs[1][2], paragraphs[2][2]), (
        f"≥3 段应前 3 段各一张，实际 {refs}"
    )


def test_assign_paragraph_refs_empty_is_fail_closed() -> None:
    """段落为空 ⇒ 抛 `SchemaViolation(PARAGRAPH_REF_UNKNOWN)`（**不跳过**）。

    跳过会退化成"图照生成、坐标是编的"，且根因要等**每一张图各自失败一次**
    才暴露 —— 等于每张白买一次。
    """
    with pytest.raises(SchemaViolation) as excinfo:
        m_schemas.assign_paragraph_refs(())
    assert excinfo.value.reason == REASON_PARAGRAPH_REF_UNKNOWN


def test_assign_paragraph_refs_rejects_misaligned_index() -> None:
    """入参三元组的序号 ≠ 位置 ⇒ 抛 `ValueError`（内部判据，不是数据错）。

    序号错位时派出的 ref 会指向**别的段**，而因为 ref 形状合法、也在
    已知集合里，下游校验**全都过** —— 静默错位。这条是唯一能拦住它的地方。
    """
    paragraphs = _paragraphs("一。", "二。")
    tampered = ((0, "一。", paragraphs[0][2]), (5, "二。", paragraphs[1][2]))
    with pytest.raises(ValueError):
        m_schemas.assign_paragraph_refs(tampered)  # type: ignore[arg-type]


def test_paragraph_ref_for_sort_order_matches_assign() -> None:
    """执行期取点必须与 `assign_paragraph_refs` **逐位一致**（它是委托，不是第二实现）。

    不引入第二份对位规则：自己再写一遍 `paragraphs[i] if i < len(...) else last`
    就是同一规则的两个说法，分叉的表现是"入队建了 3 行、执行时第 3 行落到别的段上"，
    而两边各自的测试都过。
    """
    paragraphs = _paragraphs("一。", "二。")
    assigned = m_schemas.assign_paragraph_refs(paragraphs)
    for sort_order in range(IMAGES_PER_CONTENT):
        assert (
            m_schemas.paragraph_ref_for_sort_order(paragraphs, sort_order)
            == assigned[sort_order]
        )


def test_paragraph_ref_for_sort_order_out_of_range_is_fail_closed() -> None:
    """★ `sort_order` 越界 ⇒ 抛 `SchemaViolation`，**不回落末段**。

    回落会让"一行的身份键坏了"表现为"它安静地画了最后一段" ——
    而这与**合法的**最后一张图**同形**。
    """
    paragraphs = _paragraphs("一。", "二。")
    for bad in (-1, IMAGES_PER_CONTENT, IMAGES_PER_CONTENT + 5):
        with pytest.raises(SchemaViolation) as excinfo:
            m_schemas.paragraph_ref_for_sort_order(paragraphs, bad)
        assert excinfo.value.reason == REASON_PARAGRAPH_REF_UNKNOWN
    # 非整数也要拒（bool 是 int 的子类，必须排除）。
    with pytest.raises(SchemaViolation):
        m_schemas.paragraph_ref_for_sort_order(paragraphs, True)  # type: ignore[arg-type]


def test_paragraph_refs_for_body_skips_blank_paragraphs() -> None:
    """切段口径：按空行切、**丢弃纯空白段**、序号为过滤后的连续下标。

    若空白段参与编号，正文里删掉一个空段会让后面**所有段的序号平移** ⇒
    所有旧图的 `paragraph_ref` 全部失效（`PARAGRAPH_REF_UNKNOWN`）⇒
    改个排版就要重做全部图。
    """
    paragraphs = m_schemas.paragraph_refs_for_body("甲。\n\n   \n\n乙。\n\n丙。")
    assert [p[0] for p in paragraphs] == [0, 1, 2], f"序号应是过滤后连续下标：{paragraphs}"
    assert [p[1] for p in paragraphs] == ["甲。", "乙。", "丙。"]


def test_paragraph_refs_for_body_empty_returns_empty() -> None:
    """正文为空或纯空白 ⇒ 空元组（**由调用方拒** —— 不在这里编）。"""
    assert m_schemas.paragraph_refs_for_body("") == ()
    assert m_schemas.paragraph_refs_for_body("   \n\n  \n") == ()


# ===========================================================================
# 第 4 层 —— 数据库行为（真库）
# ===========================================================================


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def _constraint_name(exc: DBAPIError) -> str | None:
    return getattr(getattr(exc.orig, "diag", None), "constraint_name", None)


def test_unique_job_id_rejects_two_media_items(env: MediaEnv) -> None:
    """★ 两条媒体行挂同一个 job ⇒ 被 `uq_media_items_job_id` 拒（迁移 §二的验收点）。

    ## 为什么必须测**行为**而不是"索引存在"

    "索引存在"是主语级事实；**它是不是唯一的**才是这条防线要守的东西。
    一个非唯一的普通索引照样"存在"、照样被 `index_names()` 数到 ——
    而"一个 job 指向两格图"这个装配故障就**没有任何东西会拦**，
    处理器反查会随 `ORDER BY` 的实现细节拿到其中一格，**而后台不报错**。

    ## 判据点名的对象

    断言 sqlstate（`23505` unique_violation）**加约束名** ——
    "被拒的结论正确 ≠ 被拒的原因正确"（本仓反复踩过：撞的是另一条约束时
    也会返回一个非预期的 sqlstate，而"抛了异常"这个断言照样绿）。
    """
    content_id = _seed_content_item(env)
    job_id = _seed_job(env, content_id)
    _seed_media_item(env, content_id, sort_order=0, job_id=job_id)

    try:
        with session_scope(env.settings) as session:
            session.execute(
                text(
                    "INSERT INTO media_items (id, project_id, content_item_id,"
                    " paragraph_ref, sort_order, state, current_version_no,"
                    " created_by, lock_version, job_id)"
                    " VALUES (gen_random_uuid(), :p, :c, 'p1-11111111', 1,"
                    " 'PENDING', 0, :by, 0, :job)"
                ),
                {
                    "p": str(env.project_id),
                    "c": str(content_id),
                    "by": str(env.owner_id),
                    "job": str(job_id),
                },
            )
        raise AssertionError("两条媒体行挂同一个 job **写进去了** —— 唯一索引未生效")
    except DBAPIError as exc:
        assert _sqlstate(exc) == "23505", (
            f"期望 sqlstate 23505（unique_violation），实际 {_sqlstate(exc)}"
            f" constraint={_constraint_name(exc)}"
        )
        assert _constraint_name(exc) == "uq_media_items_job_id", (
            f"被拒的约束是 {_constraint_name(exc)!r}，而不是 "
            "`uq_media_items_job_id` —— 撞到的是另一条约束。"
        )


def test_unique_job_id_allows_many_nulls(env: MediaEnv) -> None:
    """对照：多行 `job_id IS NULL` 必须**共存**（PostgreSQL 的 UNIQUE 允许多个 NULL）。

    缺了这条，"把唯一索引写成 `NULLS NOT DISTINCT`"也能让上面那条全绿 ——
    而那个改动会让**第二行 NULL 就违反约束**，即"尚未入队的媒体行"根本建不出来
    （迁移 §二 明确警告过不要改这一处）。
    """
    content_id = _seed_content_item(env)
    for sort_order in range(IMAGES_PER_CONTENT):
        _seed_media_item(env, content_id, sort_order=sort_order)  # job_id=None
    with session_scope(env.settings) as session:
        n = session.execute(
            text("SELECT count(*) FROM media_items WHERE job_id IS NULL")
        ).scalar_one()
    assert n == IMAGES_PER_CONTENT, f"多行 NULL job_id 应共存，实际只剩 {n} 行"


def test_find_media_item_for_job_roundtrip(env: MediaEnv) -> None:
    """★ 接缝的另一半：`find_media_item_for_job` 必须反查到**那一行**。

    `ClaimedJob` 没有自由参数位，处理器唯一的寻址手段就是 `media_items.job_id`
    反查。反查错（返回别的行）与反查不到都**不报错** —— 前者静默做错，
    后者由处理器的 `ERROR_TARGET_MISSING` fail-closed 兜住。

    这里同时钉住"两条行挂不同 job 时各查各的"（防止反查实现忽略 `job_id` 参数、
    恒返回第一行）。
    """
    content_id = _seed_content_item(env)
    job_a = _seed_job(env, content_id)
    job_b = _seed_job(env, content_id)
    item_a = _seed_media_item(env, content_id, sort_order=0, job_id=job_a)
    item_b = _seed_media_item(env, content_id, sort_order=1, job_id=job_b)

    with session_scope(env.settings) as session:
        repo = MediaRepository(session)
        found_a = repo.find_media_item_for_job(job_a)
        found_b = repo.find_media_item_for_job(job_b)

    assert found_a is not None and str(found_a.id) == str(item_a)
    assert found_b is not None and str(found_b.id) == str(item_b)
    assert str(found_a.id) != str(found_b.id), (
        "两个 job 反查到了同一行 —— 反查实现忽略了 `job_id` 参数。"
    )


def test_find_media_item_for_job_missing_returns_none(env: MediaEnv) -> None:
    """反查不到 ⇒ 返回 `None`（**不抛**）—— 由处理器折成 fail-closed 的失败。

    返回 `None` 而不是抛异常是刻意的：处理器要把"装配故障"折成一个具名的
    `ERROR_TARGET_MISSING` + `retryable=False`，而不是让异常冒到框架
    （框架会折成 `retryable=True` ⇒ 可能反复领取、每次在同一处失败）。
    """
    with session_scope(env.settings) as session:
        repo = MediaRepository(session)
        assert repo.find_media_item_for_job(uuid.uuid4()) is None


def test_pending_regeneration_reason_length_check(env: MediaEnv) -> None:
    """★ 501 字的理由 ⇒ 被列宽拒（`22001`），这是迁移 §四实测的形态。

    迁移 §四 的实测量：正常列宽下**列宽先说话**（`22001`），放宽列宽后才轮到
    CHECK（`23514`）。因此本判据断言的是 `22001` 且**点名列宽**——
    照抄"CHECK 拦下"会恒红（那正是迁移里写下的那条实测）。

    端点必须在写库前按 `MAX_REGENERATION_REASON_CHARS` 校验并翻成业务码，
    否则用户会看见一个 SQLSTATE。
    """
    content_id = _seed_content_item(env)
    too_long = "字" * (MAX_REGENERATION_REASON_CHARS + 1)
    try:
        with session_scope(env.settings) as session:
            session.execute(
                text(
                    "INSERT INTO media_items (id, project_id, content_item_id,"
                    " paragraph_ref, sort_order, state, current_version_no,"
                    " created_by, lock_version, pending_regeneration_reason)"
                    " VALUES (gen_random_uuid(), :p, :c, 'p0-00000000', 0,"
                    " 'PENDING', 0, :by, 0, :reason)"
                ),
                {
                    "p": str(env.project_id),
                    "c": str(content_id),
                    "by": str(env.owner_id),
                    "reason": too_long,
                },
            )
        raise AssertionError("501 字的理由**写进去了** —— 长度防线未生效")
    except DBAPIError as exc:
        assert _sqlstate(exc) == "22001", (
            f"期望 sqlstate 22001（string_data_right_truncation，列宽先说话），"
            f"实际 {_sqlstate(exc)} constraint={_constraint_name(exc)}。"
            "（若这里是 23514，说明列宽被放宽了 —— 迁移 §四记了这处实测。）"
        )


def test_pending_regeneration_reason_allows_null_and_normal(env: MediaEnv) -> None:
    """对照：`NULL`（首次生成常态）与 500 字的理由都必须**能通过**。

    缺了这条，一个"见谁都拒"的 CHECK 能让上面那条全绿 ——
    而 `NULL` 是**常态**（首次生成没有理由可言）。
    """
    content_id = _seed_content_item(env)
    _seed_media_item(env, content_id, sort_order=0, pending_reason=None)
    _seed_media_item(
        env,
        content_id,
        sort_order=1,
        pending_reason="字" * MAX_REGENERATION_REASON_CHARS,
    )
    with session_scope(env.settings) as session:
        n = session.execute(text("SELECT count(*) FROM media_items")).scalar_one()
    assert n == 2, f"NULL 与 500 字都应能写入，实际只有 {n} 行"


def test_pending_bindings_is_array_rejects_object_and_non_array(env: MediaEnv) -> None:
    """★ `ck_media_items_pending_bindings_is_array` 的行为（迁移 `b7c1e4a9d203`）。

    ⚠️ **本用例是补的**（2026-10-10）：迁移 docstring §六 声称
    `test_media_wp27.py::test_pending_bindings_*` 覆盖这两条 CHECK 的
    **行为**，而实测这份文件的 23 个用例里 `pending_reference_bindings`
    **零命中** —— 属「声称有覆盖」缺口（与 ISS-184 同族）。
    本组三条 + 对照一条把这个声称变成事实。

    断言**点名 SQLSTATE + 约束名**：只断言"抛了异常"的话，
    任何别的 NOT NULL / FK 违规都会让它全绿。
    """
    content_id = _seed_content_item(env)
    for bad in ({"a": 1}, "not-an-array", 5):
        try:
            _seed_media_item(env, content_id, sort_order=0, pending_bindings=bad)
        except DBAPIError as exc:
            assert _constraint_name(exc) == "ck_media_items_pending_bindings_is_array", (
                f"期望约束 ck_media_items_pending_bindings_is_array，"
                f"实际 {_sqlstate(exc)} / {_constraint_name(exc)}（输入 {bad!r}）"
            )
        else:
            raise AssertionError(f"非数组形状 {bad!r} **写进去了** —— 形状防线未生效")
        # 每个用例独占 sort_order，避免唯一键把下一个用例的失败原因换掉。
        content_id = _seed_content_item(env)


def test_pending_bindings_size_rejects_empty_and_over_limit(env: MediaEnv) -> None:
    """★ `ck_media_items_pending_bindings_size` 的行为：`[]` 与 9 条都被拒。

    `[]`（空数组）与 `NULL`（没有）是**两种不同的事实** —— 迁移 §四
    明写「混起来会让『忘了传绑定』伪装成『明确传了空绑定』」。
    故这里把 `[]` 单列一个用例，与下面那条容许 `NULL` 的对照成对。
    """
    # ---- ① 空数组
    content_id = _seed_content_item(env)
    try:
        _seed_media_item(env, content_id, sort_order=0, pending_bindings=[])
    except DBAPIError as exc:
        assert _constraint_name(exc) == "ck_media_items_pending_bindings_size", (
            f"期望约束 ck_media_items_pending_bindings_size（空数组），"
            f"实际 {_sqlstate(exc)} / {_constraint_name(exc)}"
        )
    else:
        raise AssertionError("空数组 `[]` **写进去了** —— 会被误读成『明确传了空绑定』")

    # ---- ② 上界 +1（下界 1 / 上界 8 取自建迁移时的取值）
    content_id = _seed_content_item(env)
    nine = [{"index": i} for i in range(9)]
    try:
        _seed_media_item(env, content_id, sort_order=0, pending_bindings=nine)
    except DBAPIError as exc:
        assert _constraint_name(exc) == "ck_media_items_pending_bindings_size", (
            f"期望约束 ck_media_items_pending_bindings_size（9 条），"
            f"实际 {_sqlstate(exc)} / {_constraint_name(exc)}"
        )
    else:
        raise AssertionError("9 条绑定**写进去了** —— 数量上界未生效")


def test_repository_clears_pending_bindings_as_sql_null(env: MediaEnv) -> None:
    """仓储层的 None 必须写 SQL NULL，不能变成 JSONB 字面量 null。"""
    content_id = _seed_content_item(env)
    item_id = _seed_media_item(
        env,
        content_id,
        sort_order=0,
        pending_bindings=[{"assetId": str(uuid.uuid4()), "part": None, "note": None}],
    )
    with session_scope(env.settings) as session:
        changed = MediaRepository(session).set_pending_reference_bindings(
            media_item_id=item_id, bindings=None
        )
        assert changed == 1
        is_sql_null = session.execute(
            text(
                "SELECT pending_reference_bindings IS NULL "
                "FROM media_items WHERE id = :id"
            ),
            {"id": item_id},
        ).scalar_one()
        assert is_sql_null is True


def test_pending_bindings_allows_null_and_boundary_values(env: MediaEnv) -> None:
    """对照：`NULL` / 1 条 / 8 条（**恰好上界**）都必须能通过。

    没有这条，上面两条"见谁都拒"的实现同样会全绿 ——
    而 `NULL` 是**合法常态**（生成成功后投影列被清成 `NULL`）。
    8 条是上界本身（边界用 `<=` 而非 `<`），必须与 9 条被拒**成对**。
    """
    content_id = _seed_content_item(env)
    _seed_media_item(env, content_id, sort_order=0, pending_bindings=None)
    _seed_media_item(env, content_id, sort_order=1, pending_bindings=[{"index": 0}])
    _seed_media_item(
        env,
        content_id,
        sort_order=2,
        pending_bindings=[{"index": i} for i in range(8)],
    )
    with session_scope(env.settings) as session:
        n = session.execute(text("SELECT count(*) FROM media_items")).scalar_one()
    assert n == 3, f"NULL / 1 条 / 8 条都应能写入，实际只有 {n} 行"


def test_deferred_foreign_keys_match_model(env: MediaEnv) -> None:
    """★ 4 条延迟 FK 逐条点名（形态 = deferrable + initially deferred）。

    库里漏成非延迟时，"同事务先引用后创建"会在生产库上报 `23503`，
    而测试库（夹具按模型建）却是好的 —— 更难查。期望侧**从模型元数据派生**
    （不写死清单），再与 `pg_constraint` 逐条比。
    """
    import sqlalchemy as sa

    expected: dict[str, tuple[bool, bool]] = {}
    for table in OWN_TABLES:
        for constraint in MediaItem.metadata.tables[table].constraints:
            if isinstance(constraint, sa.ForeignKeyConstraint) and (
                constraint.deferrable or constraint.initially
            ):
                expected[constraint.name] = (
                    bool(constraint.deferrable),
                    bool(constraint.initially),
                )
    assert len(expected) == 4, (
        f"模型里 media 表的延迟 FK 是 {len(expected)} 条（期望 4）—— "
        f"若确实改过请同步本条与迁移：{sorted(expected)}"
    )

    with session_scope(env.settings) as session:
        rows = session.execute(
            text(
                "SELECT conname, condeferrable, condeferred FROM pg_constraint"
                " WHERE contype='f' AND conname = ANY(:names)"
            ),
            {"names": list(expected)},
        ).fetchall()
    actual = {r[0]: (r[1], r[2]) for r in rows}

    missing = sorted(set(expected) - set(actual))
    assert not missing, f"这些延迟 FK 在库里不存在：{missing}"
    mismatched = [
        f"{name}: 模型={expected[name]} 库={actual[name]}"
        for name in sorted(expected)
        if expected[name] != actual[name]
    ]
    assert not mismatched, "延迟形态不一致：\n  " + "\n  ".join(mismatched)


__all__ = [
    "MediaEnv",
    "env",
]