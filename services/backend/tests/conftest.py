"""测试公共 fixture。

三个关键设计：

1. **用 `create_app(settings)` 注入配置，而不是 monkeypatch 全局单例。**
   app 工厂的存在就是为了这个 —— 测试能拿到"未配置数据库"的 app，
   而不必去改进程级环境变量再想办法还原。

2. **`get_settings.cache_clear()` 是必须的。** Settings 用 lru_cache 缓存，
   测试改了环境变量但不清缓存的话，下一个测试会拿到旧配置，
   产生"单跑通过、连跑失败"的顺序依赖型假象。

3. **不 mock 数据库。** 健康检查竖切片的全部价值就是证明"真的能连上 PG"，
   用 mock 替换它等于把这条链路变成一句断言 `assert True`。
   连不上就让它失败 —— 那正是需要被暴露的信息。
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from rednote.main import create_app
from rednote.platform.configuration import Settings, get_settings
from rednote.platform.database import reset_engines
from rednote.platform.db_graph import cross_package_foreign_keys, truncate_targets

# 本机原生 PG 的默认 DSN。测试用它，但**只作为测试的显式输入**，
# 不进生产代码、不写进 alembic.ini、不做成 Settings 的默认值。
# 允许用环境变量覆盖，便于 CI 指向别的库。
TEST_DATABASE_URL_ENV = "REDNOTE_TEST_DATABASE_URL"
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"


def _test_database_url() -> str:
    return os.environ.get(TEST_DATABASE_URL_ENV, DEFAULT_TEST_DATABASE_URL).strip()


@pytest.fixture(autouse=True)
def _isolate_settings_cache() -> Iterator[None]:
    """每个测试前后都清配置缓存与 engine 缓存。

    为什么 engine 也要清：engine 按 DSN 的哈希缓存。上一个测试建了连某库的池，
    下一个测试若换了 DSN，不清缓存会复用旧池，测出来的是上一个测试的连接。
    """
    get_settings.cache_clear()
    reset_engines()
    yield
    get_settings.cache_clear()
    reset_engines()


@pytest.fixture
def settings_without_database() -> Settings:
    """显式构造一个"未配置数据库"的配置。

    直接实例化 `Settings(database_url=None)` 而不是删环境变量：
    后者会受开发者本机 .env 影响，测试结果不可复现。
    """
    return Settings(database_url=None, environment="test")


@pytest.fixture
def app_without_database(settings_without_database: Settings) -> FastAPI:
    return create_app(settings_without_database)


@pytest.fixture
def client_without_database(app_without_database: FastAPI) -> Iterator[TestClient]:
    with TestClient(app_without_database) as client:
        yield client


@pytest.fixture
def app_with_database() -> FastAPI:
    """连真库的 app。DSN 从环境变量或本机默认值取。"""
    settings = Settings(database_url=_test_database_url(), environment="test")
    return create_app(settings)


@pytest.fixture
def client_with_database(app_with_database: FastAPI) -> Iterator[TestClient]:
    with TestClient(app_with_database) as client:
        yield client


@pytest.fixture
def any_app() -> FastAPI:
    """不带任何配置假设的 app，供错误/分页等与数据库无关的测试使用。"""
    return create_app(Settings(database_url=None, environment="test"))


# ===========================================================================
# 跨包建表清理：`DROP ... CASCADE` 会连带摘除**别人**的外键
#
# `_bare_table_name` / `cross_package_foreign_keys` / `truncate_targets` 已迁到
# `rednote.platform.db_graph`（WP-DEBT-05）：`tools/dev/reset_shared_test_db.py`
# 需要同一套闭包算法，且不应反向 import 这个测试层的 `conftest.py`。
# 这里只保留 re-export（见文件头 import），21 个既有 `from conftest import
# truncate_targets, cross_package_foreign_keys, drop_own_tables` 不必改动。
# ===========================================================================


def drop_own_tables(
    engine: Engine, names: Sequence[str]
) -> tuple[list[str], list[str]]:
    """DROP 本包自己的表 —— **但只删"删了不会伤到别的包"的那些**。

    返回 `(dropped, kept)`，让夹具能把"删了什么、留了什么"断言出来
    （本类缺陷的教训就是"静默"）。

    ## 为什么不能无脑 `DROP ... CASCADE`（ISS-106）

    各包夹具都要先 `DROP TABLE IF EXISTS <自己的表> CASCADE` 再 `create_all`
    （`create_all` 只对"表不存在"幂等；表已存在但结构是旧的时候它会
    **静默跳过**，于是测试跑在结构不对的表上）。

    问题出在 `CASCADE`：PG 删被引用表时会级联删掉**所有依赖对象** ——
    其中包含**别的包建在我这张表上的外键**。干净库迁移后实测，本仓恰好
    3 条（只有 WP20 的夹具命中）：

    | 被 `DROP knowledge_*` 摘除的外键 | 引用方 | 包 |
    |---|---|---|
    | `fk_baseline_items_knowledge_asset_id_knowledge_assets` | `baseline_items` | **WP22** |
    | `fk_baseline_items_knowledge_version_id_knowledge_versions` | `baseline_items` | **WP22** |
    | `fk_chunk_embeddings_chunk_id_knowledge_chunks` | `chunk_embeddings` | **WP21** |

    ## 两条错误修法（都实测过，不要重走）

    **① 先记后补（`DROP` → `create_all` → `ADD CONSTRAINT`）**：在**同一个库里
    别的包已经造过数据**的情况下会撞 `IntegrityError`。实测：WP22 测试先跑
    （字母序 `baseline` < `knowledge`），造了 57 行 `baseline_items`；WP20 夹具把
    `knowledge_assets` 删成空表后，那 57 行里 5 行成了**孤儿** → `ADD CONSTRAINT`
    校验既有行失败。而 `baseline_items` 是**不可变表**（`trg_baseline_items_immutable`
    拦 DELETE），孤儿连删都删不掉。**"删了再补"这个思路本身就错了** ——
    中间态（空表）已经破坏了别人的数据。

    **② 改成 `TRUNCATE ... CASCADE`**：FK 确实保住了、也不违反不可变性，
    但 `CASCADE` 会**连带清空引用方表**（实测 `baseline_items` 57 行被清）。
    换了个地方破坏别人的数据而已。

    ## 正确判据：只删"没有跨包入向外键"的表

    被别人引用的表**根本不能删** —— 删掉就必然破坏它（要么摘防线，要么造孤儿，
    要么清数据）。因此本函数只对**安全**的表执行 DROP，被引用的表**原样保留**，
    交给 `create_all`（幂等）保证存在性、交给迁移保证结构。

    "安全"要取**闭包**，不能只看单表：若 A 被保留、而 A 又引用 B，则 B 也不能删
    （删 B 会连带摘掉 A→B 的包内外键，而 A 会被 `create_all` 跳过，那条外键
    就永久丢了）。算法：以待删集合 = 本包表；重复"把被『保留表或包外表』引用的表
    移出待删集合"直到不动点。

    ⚠️ **不要用 `session_replication_role='replica'` 去强删孤儿**：那要 superuser 权限，
    且删的是别人包的数据 —— 夹具没有这个权力。
    """
    own = {name.lower() for name in names}
    with engine.begin() as conn:
        edges = conn.execute(
            text(
                "select confrelid::regclass::text as target,"
                "       conrelid::regclass::text as ref_table"
                " from pg_constraint"
                " where contype = 'f' and confrelid::regclass::text = any(:names)"
            ),
            {"names": list(own)},
        ).fetchall()

        # 保留集合的闭包：跨包引用方 → 其目标必须保留；
        # 保留表中的包内引用 → 其目标也必须保留。
        kept: set[str] = {
            target.lower() for target, ref in edges if ref.lower() not in own
        }
        while True:
            grown = kept | {
                target.lower()
                for target, ref in edges
                if ref.lower() in kept and target.lower() not in kept
            }
            if grown == kept:
                break
            kept = grown

        dropped: list[str] = []
        kept_order: list[str] = []
        for name in names:
            if name.lower() in kept:
                kept_order.append(name)
                continue
            conn.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
            dropped.append(name)
    return dropped, kept_order
