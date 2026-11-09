"""跨包 Schema 一致性：**模型建的形态** 与 **迁移建的形态** 必须一致。

## 为什么需要这个文件（ISS-106 / ISS-107）

本仓的建表路径有**两条**，它们必须产出同一套约束：

| 路径 | 谁用 | 建表方式 |
|---|---|---|
| 迁移 | 生产 / CI 的 `alembic upgrade head` | 迁移脚本逐条 DDL |
| 夹具 | 各包测试的 module 级 `_ensure_own_tables` | `Base.metadata.create_all` |

此前**没有任何判据**比较这两条路径的产物。后果是两个真实缺陷都静默通过：

* **ISS-106**：WP20 夹具 `DROP knowledge_assets CASCADE` 连带摘除 WP22/WP21
  建在它上面的 3 条外键 —— 全量 1511 条测试全绿。
* **ISS-107**：`prompt_templates.active_version_id` 的 FK 在**迁移**里是
  `DEFERRABLE INITIALLY DEFERRED`（有明确设计理由），但**模型里漏写**。
  夹具按模型重建表时把迁移的延迟 FK「治愈」成非延迟 —— 同一个库，
  "迁移建出"与"夹具建出"形态不同，而**没有一条测试发现**。

本文件把这两条路径的产物**直接对比**，因此这类漂移无法再静默。

## 判据设计：为什么是「模型 → 库」方向而不是全量 diff

全量 diff 会被**合理的**差异淹没（运行期动态建的 HNSW 索引、
探针残留表等）。本文件只钉一件事：**模型声明为 deferrable 的 FK，
库里必须也是 deferrable** —— 这是"声明了却没生效"的单向缺陷，
方向固定、不会误报。

反向（库里有、模型没有）**不查**：迁移可以建模型没声明的对象
（如 WP21 的 HNSW 索引按 profile 动态创建），那是刻意的。
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.sql.schema import ForeignKeyConstraint

from rednote.platform.base import Base
from rednote.platform.configuration import Settings
from rednote.platform.database import get_engine, reset_engines

# 环境与各包测试同口径（库由环境变量指定，缺省本机 postgres）。
_DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="

# 需要 import 才能让 metadata 收齐全部表。
# 少 import 一个模块，它的表就不在 metadata 里 —— 于是那个模块的
# deferrable FK 会被本文件**静默漏检**。
#
# ⚠️ **铁律 14：清单由磁盘派生，不是人写的名单。**
#
# 初版是人写的 15 项，实测漏了 2 个（`billing` / `work_items`），
# 而漏掉**不会失败，只会静默缩小覆盖**：`billing` 的 5 张表在 WP25 之前
# 不存在，`work_items` 则从 WP10 起就是盲区。判据纪律第 9 条：
# "声称自己有防线"比"没有防线"更危险 —— 一份会自己缩水的名单就是这种形态。
#
# 派生规则与 `tools/quality/check_migration_model_parity.py` 的
# `_model_modules()` 同源（`modules/*/models.py` 的父目录名），
# 两处口径由磁盘统一，不再各写各的。
def _discover_model_modules() -> tuple[str, ...]:
    """扫出所有带 `models.py` 的业务模块（覆盖范围由磁盘决定）。"""
    import pathlib

    # 本文件在 services/backend/tests/ 下，模块目录相对它定位。
    modules_dir = pathlib.Path(__file__).resolve().parents[1] / "src" / "rednote" / "modules"
    top = {p.parent.name for p in modules_dir.glob("*/models.py")}
    # 子包里的模型（`planning/trial`、`versions/impact`）：模块名用点号路径，
    # 与 `rednote.modules.{name}.models` 的导入形式天然兼容。只扫一层会让这些表
    # 的外键不被核对（判据仍全绿而覆盖范围悄悄缩水）——WP34 收口时实测踩到。
    nested = {
        ".".join(p.parent.relative_to(modules_dir).parts)
        for p in modules_dir.glob("*/*/models.py")
    }
    return tuple(sorted(top | nested))


_MODEL_MODULES: tuple[str, ...] = _discover_model_modules()


def _load_all_models() -> None:
    for name in _MODEL_MODULES:
        importlib.import_module(f"rednote.modules.{name}.models")
    # WP21 的 embedding 模型单独一个模块，不在 models.py 里。
    importlib.import_module("rednote.modules.knowledge.embedding_models")


_load_all_models()


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    reset_engines()
    url = os.environ.get("REDNOTE_TEST_DATABASE_URL", _DEFAULT_TEST_DATABASE_URL).strip()
    eng = get_engine(
        Settings(
            database_url=url,
            environment="test",
            session_cookie_secure=False,
            oidc_issuer=TEST_ISSUER,
            oidc_token_encryption_key=TEST_ENCRYPTION_KEY,
        )
    )
    yield eng


def _model_deferrable_fks() -> dict[str, tuple[str, str]]:
    """模型里声明为 deferrable 的 FK：`约束名 → (表名, "deferrable/initially")`。"""
    found: dict[str, tuple[str, str]] = {}
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            if constraint.deferrable or constraint.initially:
                found[constraint.name] = (
                    table.name,
                    f"{constraint.deferrable}/{constraint.initially}",
                )
    return found


def _db_deferrable_fks(engine: Engine, names: list[str]) -> dict[str, tuple[bool, bool]]:
    """库里这些约束名对应的 `(condeferrable, condeferred)`。"""
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "select conname, condeferrable, condeferred from pg_constraint"
                " where contype = 'f' and conname = any(:names)"
            ),
            {"names": names},
        )
        return {row[0]: (row[1], row[2]) for row in rows}


# ===========================================================================
# 判据 1：模型声明 deferrable 的 FK，库里必须也是 deferrable（ISS-107）
# ===========================================================================


@pytest.mark.regression
def test_all_fk_deferrability_matches_between_model_and_db(engine: Engine) -> None:
    """**核心判据**：每一条 FK 的"延迟形态"，模型与库必须**逐条相等**。

    失败含义：模型与迁移/夹具的产物不一致。两个真实缺陷都是这个形态：

    * **ISS-107**：迁移写 `DEFERRABLE INITIALLY DEFERRED`，模型漏写
      `deferrable` → 夹具按模型重建表时把延迟 FK「治愈」成非延迟。
      同一个库，"迁移建出"与"夹具建出"形态不同，而**没有测试发现**。
    * 反向漂移：模型声明延迟而迁移漏了 → 生产库上"同事务先引用后创建"
      会报 23503，而测试库（夹具按模型建）却是好的 —— 更难查。

    ## 为什么必须逐条比对"两边的形态"，而不是只查"模型声明延迟的"

    只查单向（模型声明延迟 → 库里核对）会漏掉**模型漏写**这一半：
    模型漏写时它不声明延迟，单向判据直接跳过，依然全绿 —— 正是 ISS-107
    逃过 1511 条测试的同一个盲区。因此这里取**全部 FK**，两边都算
    "是否延迟"，再逐条比相等。

    反向验证（**已实测，证据**: `evidence/m1/wp22-schema-parity-reverse.log`
    用例 ①）：把 `prompt_templates.active_version_id` 的
    `deferrable=True, initially="DEFERRED"` 三行去掉，本测试必须 FAIL。
    复跑命令：`python tools/m1/collect_schema_parity_reverse_evidence.py`。
    """
    # --- 模型侧：全部 FK 的延迟形态 ---
    model_state: dict[str, bool] = {}
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            if constraint.name is None:
                raise AssertionError(
                    f"{table.name} 上有未命名外键 —— 无法与库核对。"
                    "请确认 metadata 带 naming_convention（platform/base.py）"
                )
            model_state[constraint.name] = bool(
                constraint.deferrable and constraint.initially
            )

    assert len(model_state) >= 100, (
        f"模型里只有 {len(model_state)} 条外键（期望 >= 100）—— "
        "大概率是某个模块没被 import，metadata 不完整"
    )

    # --- 库侧：同名约束的实际形态 ---
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "select conname, condeferrable, condeferred from pg_constraint"
                " where contype = 'f' and conname = any(:names)"
            ),
            {"names": list(model_state)},
        )
        db_state = {r[0]: bool(r[1] and r[2]) for r in rows}

    missing = sorted(set(model_state) - set(db_state))
    assert not missing, (
        f"模型声明的 {len(missing)} 条外键在库里不存在（表被夹具删过？迁移漏了？）："
        f"\n  " + "\n  ".join(missing)
    )

    mismatched = [
        f"{name}：模型 deferrable={model_state[name]}，库 deferrable={db_state[name]}"
        for name in sorted(model_state)
        if model_state[name] != db_state[name]
    ]
    assert not mismatched, (
        "模型与库的外键延迟形态不一致（同一应用代码会在两条建表路径下表现不同）：\n  "
        + "\n  ".join(mismatched)
    )


@pytest.mark.regression
def test_deferrable_fk_count_is_stable() -> None:
    """延迟外键的**条数**单独钉住，防止"漏 import 模块"造成的静默漏检。

    本文件的核对以 metadata 为准。若某模块没被 import，它的表不在
    metadata 里，它的外键就**不会被核对** —— 判据仍然全绿，
    但覆盖范围悄悄缩水（判据纪律第 9 条："声称自己有防线"比"没有防线"更危险）。

    ## 覆盖范围本身由**两条**判据守着（铁律 14 的两半）

    清单已改为磁盘派生（`_discover_model_modules()`），但"派生"本身也会失效 ——
    比如 `glob` 的模式写错、`parents[1]` 层级改错，都会**静默返回空元组**。
    因此这里在**两个方向**各钉一条：

    1. **模块数下界**：派生出 17 个模块（磁盘上的三处口径一致：
       `test_schema_parity.py` / `tools/quality/check_migration_model_parity.py`
       / `migrations/env.py` 的 import 块）。低于 15 说明 `glob` 没扫到东西。
    2. **延迟外键数下界**：`>= 13`。

    ## 反向验证（**已实测**）

    - 初版（人写清单）的证据：`evidence/m1/wp22-schema-parity-reverse.log`
      用例 ④ —— 把 `_MODEL_MODULES` 里的 `"prompts"` 去掉，本测试必须 FAIL
      （13 → 11）。**只写"本文件有下界断言"而不验证它会红，就是 ISS-95 的形态**。
    - 派生版的证据：把 `_discover_model_modules()` 的 `glob("*/models.py")`
      改成 `glob("*/modelz.py")`（打错一个字），本测试必须 FAIL
      （模块数 17 → 0）。这就是"派生自身失效"的反向验证 ——
      改的是**取数的那个模式**，不是名单里的一项。
    """
    # ① 模块清单下界：派生必须真的扫到东西。
    assert len(_MODEL_MODULES) >= 15, (
        f"磁盘只派生到 {len(_MODEL_MODULES)} 个带 models.py 的模块（期望 >= 15）。"
        "磁盘上实测 17 个 —— 低于下界说明 `_discover_model_modules()` 的 "
        f"glob 或路径基点失效（**不是**说真少了模块）。实际：{sorted(_MODEL_MODULES)}"
    )

    # ② 延迟外键条数下界：覆盖范围缩水的传统形态。
    model_fks = _model_deferrable_fks()
    assert len(model_fks) >= 13, (
        f"只找到 {len(model_fks)} 条延迟外键（期望 >= 13）。"
        "若确实新增了包，请上调本下界；若是少了，说明某个模块没被 import 进"
        f" metadata —— 那会让本文件的核对静默漏掉它。实际：{sorted(model_fks)}"
    )


# ===========================================================================
# 判据 2：夹具前后，跨包外键集合不变（ISS-106）
# ===========================================================================


@pytest.mark.regression
def test_cross_package_fk_helper_roundtrip_is_identity(engine: Engine) -> None:
    """`cross_package_foreign_keys` 是只读的 —— 调它不许改变任何约束。

    这条是给夹具判据本身立的：夹具用它做"前后一致"断言，如果这个函数
    自己有副作用（比如顺带建/删了什么），那条断言就失去意义。

    反向验证（**已实测，证据**: `evidence/m1/wp22-schema-parity-reverse.log`
    用例 ②）：把 `conftest.drop_own_tables` 改回无脑 `DROP ... CASCADE`，
    `test_knowledge_wp20.py` 的 module 级 autouse 夹具
    `_ensure_own_tables` 里的 `fks_after == fks_before` 断言必须 FAIL
    （实测 `set() == {3 条已登记的跨包外键}`）—— 那 3 条 FK 本文件已在
    下面逐条点名，两边是同一组对象。
    """
    from conftest import cross_package_foreign_keys

    names = [
        "knowledge_chunks",
        "knowledge_publications",
        "knowledge_versions",
        "knowledge_proposals",
        "knowledge_assets",
    ]
    before = cross_package_foreign_keys(engine, names)
    middle = cross_package_foreign_keys(engine, names)
    after = cross_package_foreign_keys(engine, names)
    assert before == middle == after, "读取跨包外键集合不应有副作用"
    # WP22 的 2 条必须在这里（这是 ISS-106 的原始受害对象）
    assert "fk_baseline_items_knowledge_asset_id_knowledge_assets" in before
    assert "fk_baseline_items_knowledge_version_id_knowledge_versions" in before
    # WP21 的 1 条也在
    assert "fk_chunk_embeddings_chunk_id_knowledge_chunks" in before
