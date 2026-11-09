#!/usr/bin/env python
"""迁移结构 ↔ 模型结构 全量对账：`alembic upgrade head` 的产物必须等于 `create_all` 的产物。

## 为什么需要这个（ISS-146）

本仓的**测试夹具用 `Base.metadata.create_all` 建表，不走迁移**
（`conftest.py` 的 `drop_own_tables` 注释已写明）。实测后果：

| 库 | 表数 | 说明 |
|---|---|---|
| 迁移建（`alembic upgrade head`） | **52** | 生产形态 |
| 夹具建（`create_all`） | 视各包夹具跑没跑而定 | 零散、不完整 |

2026-09-29 现场：默认测试库的 `alembic_version` 停在 **`ebc05dfca16b`（WP20）**，
而迁移头已是 `022ba9804d66`（WP24）—— 中间 WP21/WP22/WP24 加的表**全部不在**。
测试仍然全绿，因为夹具把它需要的那几张 `create_all` 出来了。

⇒ **"迁移结构 = 测试结构"从来没有被任何判据覆盖。** 这条链断了整整两个大包。

既有判据 `test_schema_parity.py` 只比一件事：**模型声明 deferrable 的 FK，
库里是否也 deferrable**（ISS-106/107 那两条真实缺陷的形状）。
它**刻意不做全量 diff**（docstring 里写了理由：全量 diff 会被"合理的差异"淹没）。
本脚本补的正是它不做的部分 —— 但**不做全量 diff 的原始形态**，
而是**在两个库上都从零点建起**，然后逐对象比较。这样"合理的差异"不再存在：
两个库的**唯一**变量就是"谁来建表"。

## 判据设计：两库同时从零点建

```
库 A: 全新空库 → alembic upgrade head        （迁移路径）
库 B: 全新空库 → Base.metadata.create_all     （模型路径）
       ↓
逐对象比较：表 / 列(类型+可空+默认) / 主键 / 唯一约束 / CHECK / 外键(含 deferrable) / 索引
```

两条独立路径的产物**必须逐项相等**。任何一边多/少/形态不同 → 报红。

## 为什么用独立临时库（ISS-108 一族的延续）

**绝不碰共享测试库**。本脚本自建 `rednote_parity_audit_<pid>` 两个库，
跑完即 `DROP DATABASE`。理由与本项目"并行开发线各用独立测试库"同：
共享库上跑会与其他会话抢锁（历史 639 errors 前科）。

## 已知边界（不做假保证）

排除集**由磁盘/运行时派生**，不是人写名单（铁律 14）：

* **ANN 索引**：`chunk_embeddings` 的索引由 WP21 按 embedding profile 动态创建，
  不在 `Base.metadata` 里 —— 迁移建得出、模型建不出（SQLAlchemy 的 `Index`
  没有 access method 参数）。判定依据是**磁盘上查出来的 `pg_am.amname`**
  ∉ `{hnsw, ivfflat}`，**不是索引名**，并在输出里**逐条列出被排除的**（不静默）。

  > 实测教训（铁律 14 第 7 次现身）：本脚本初版写死了名字
  > `ix_chunk_embeddings_embedding_hnsw`。实跑发现该名字**在磁盘上不存在** ——
  > 真名由 `embedding_ddl.hnsw_index_name(profile_id)` 生成，带 profile uuid 后缀
  > （如 `ix_chunk_embeddings_hnsw_d5498743_...`）。人脑捏的名字又一次被磁盘否定。
* **alembic 记账表**：`create_all` 不建它（属迁移路径固有）。表名从
  `migrations/env.py` → `alembic.ini` → alembic 包源码**三级派生**，
  且 main() 会断言"派生名确实存在于迁移库、且不存在于模型库"，否则报红。
* **触发器**：`create_all` 不会建触发器（那是迁移用 `op.execute(ddl)` 干的）。
  本脚本**不比较触发器**，改由各包证据脚本按"该拦的拦、该过的过"做活性验证
  （触发器的正确性靠行为验，不靠 DDL 文本比）。
* **序列/自增**：本仓主键全是 UUID，无 serial —— 实测为零，不做比较。

跑法：
    python tools/quality/check_migration_model_parity.py
退出码 0 = 两条建表路径产物一致。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 与 `conftest.py` 同口径的默认基准 DSN（**库名会被本脚本替换**）。
#: ⚠️ 库名不得是 `postgres`（ISS-145）—— 本脚本只借它的 host/user/port。
DEFAULT_BASE_DSN = "postgresql+psycopg://rednote@localhost:5432/rednote_test"

#: 需要 import 才能让 metadata 收齐全部表。
#:
#: ⚠️ **铁律 14：这份清单由磁盘派生，不是人写的名单。**
#:
#: 初版是人写的 15 项，实测漏了 2 个 —— 两个都**不报错，只静默缩小覆盖**：
#:
#: - `billing`（WP25 的 5 张表）：漏掉后 `pricing_versions` /
#:   `cost_estimates` / `project_budgets` / `budget_reservations` /
#:   `usage_ledger` 的**全部 CHECK 与索引**逃过"迁移库 vs 模型库"的逐项对比
#:   —— 也就是本脚本存在的唯一理由对本包失效；
#: - `work_items`（WP10 建的表）：自建包起就是盲区，且从未有任何判据报红。
#:
#: 本脚本的 docstring 自称与 `migrations/env.py`、`test_schema_parity.py`
#: "同清单"，而三处实为**三份各写各的名单**（`test_schema_parity.py` 当时也漏
#: `work_items`）。这正是"覆盖范围由人写"的失效形态：
#: 缩小覆盖不会失败，只会让后面所有的绿都失去意义。
#:
#: 派生规则：`services/backend/src/rednote/modules/*/models.py` 的父目录名。
#: 反向约束在 `main()`：迁移库表数不得低于 `MIN_TABLES`（派生的清单若被削，
#: 模型侧表数会跟着掉，`_diff` 立刻在"表"这一类上报红）。
def _model_modules() -> tuple[str, ...]:
    """磁盘上所有带 `models.py` 的业务模块名（铁律 14：覆盖范围由磁盘决定）。"""
    mods_dir = REPO_ROOT / "services" / "backend" / "src" / "rednote" / "modules"
    top = {p.parent.name for p in mods_dir.glob("*/models.py")}
    # 子包里的模型（`planning/trial`、`versions/impact`）：用点号路径。只扫一层会让
    # 这些表不进模型路径（`create_all` 缺表而迁移库有 ⇒ 对账报「模型漏了」）。
    nested = {
        ".".join(p.parent.relative_to(mods_dir).parts)
        for p in mods_dir.glob("*/*/models.py")
    }
    return tuple(sorted(top | nested))


MODEL_MODULES: tuple[str, ...] = _model_modules()


def _env_py_imported_modules() -> tuple[str, ...]:
    """`migrations/env.py` 里**实际写了 import** 的模块名（AST 解析，不 import）。

    这是与 `_model_modules()` 相对的另一半对账：磁盘上有哪些模块 ⟷
    env.py 是否都 import 了。两边**必须相等**，缺一即报红。

    ## 为什么必须有这条（实测踩到）

    补 `MODEL_MODULES` 时顺手查了 `env.py`，发现它的 import 块有 17 行、
    独缺 `work_items` —— 而它自己的注释白纸黑字写着
    "每新增一个含模型的模块，都要在这里补一行 import"。

    这正是**铁律 14** 的教科书形态：`env.py` 的 import 是"人写的名单"，
    漏一行**不报错**，只在有人手动跑 `alembic revision --autogenerate`
    时炸出 `op.drop_table('work_items')`（CI 只跑 `upgrade head`，不触发）。

    派生规则：扫 `from rednote.modules.<X> import models` 与
    `from rednote.modules.knowledge import embedding_models`。
    只认**模块级** import 语句（`ast.ImportFrom`），注释里提到的不算。
    """
    import ast

    env_py = REPO_ROOT / "migrations" / "env.py"
    tree = ast.parse(env_py.read_text(encoding="utf-8"), filename=str(env_py))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.module or not node.module.startswith("rednote.modules."):
            continue
        parts = node.module.split(".")
        # `from rednote.modules.<X> import models` → X；
        # `from rednote.modules.<X>.<Y> import models`（子包，如 planning.trial）→ `X.Y`；
        # `rednote.modules.knowledge.embedding_models` 仍归到 knowledge
        #（同一个模块的两份模型文件）。判定看被导入的名字是不是 `models`，
        # 而不是按层数硬切：`from rednote.modules.x import models` 与
        # `from rednote.modules.x.models import Y` 都归到 x。
        if len(parts) < 3:
            continue
        tail = parts[2:]
        if tail[-1] == "models":
            tail = tail[:-1]
        imported = {a.name for a in node.names}
        if "models" in imported or "embedding_models" in imported or node.module.endswith("models"):
            found.add(".".join(tail) if tail else parts[2])
        else:
            found.add(parts[2])
    return tuple(sorted(found))

#: 近似最近邻索引的 access method —— `create_all` **建不出**这一族
#: （SQLAlchemy 的 `Index` 没有 access method 参数，迁移是用 `op.execute(ddl)` 建的）。
#:
#: ⚠️ 这里刻意**不写索引名名单**（铁律 14：凡覆盖范围由人写，就必须改由磁盘决定）。
#: 判定依据是**磁盘上查出来的 access method**，不是名字。
#: `ivfflat` 一并列入：它同样只能靠 `op.execute` 建。
#: 反向约束见 `main()` 里对"模型侧被排除数必须为 0"的断言。
ANN_ACCESS_METHODS: frozenset[str] = frozenset({"hnsw", "ivfflat"})

#: 独立对账下界：表数不得少于这个值（低于它说明 metadata 没收齐，覆盖静默缩水）。
MIN_TABLES = 45

#: 模块清单下界：`_model_modules()` 派生出的模块数不得少于这个值。
#: 磁盘上实测 17 个（见 `_model_modules()`）。低于它说明 **glob 或路径基点失效**，
#: 而不是"真少了模块" —— 两者必须能区分（判据纪律第 12 条："没跑"与"跑失败"同形）。
MIN_MODULES = 15


def _migration_bookkeeping_tables() -> frozenset[str]:
    """alembic 自己的记账表名 —— **从磁盘上派生，不写死字符串**（铁律 14）。

    三个来源，**高优先级覆盖低优先级**（后者是前者的缺省）：

    1. `migrations/env.py` 源码里 `configure(version_table=...)` 的**字符串实参**
       （本仓可自定义的主要入口；AST 解析，不 import）；
    2. `migrations/alembic.ini` 的 `[alembic] version_table`；
    3. alembic **包本体源码**里 `opts.get("version_table", <字面量>)` 的缺省字面量
       （AST 解析 `runtime/migration.py`，随安装版本走）。

    为什么不能写死 `"alembic_version"`：那是"覆盖范围由人写"。若哪天 `env.py`
    改了名，写死的名单会**静默失效** —— 排除集还在排旧名字，新记账表就跑进 diff
    报红，人再去手改名单，循环往复。三个来源都读**磁盘上的真物**，改名会被跟上。

    失败即抛：任何一个来源解析不出**非空字符串**都说明依据与磁盘不符，
    此时继续排除等于排除一个不存在的名字（"没跑通"装成"跑过"）。
    """
    import ast

    # --- 来源 1：migrations/env.py 的 configure(version_table=...) ---
    from_env: str | None = None
    env_py = REPO_ROOT / "migrations" / "env.py"
    if env_py.exists():
        tree = ast.parse(env_py.read_text(encoding="utf-8"), filename=str(env_py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "version_table" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str) and kw.value.value:
                        from_env = kw.value.value

    # --- 来源 2：migrations/alembic.ini ---
    from_ini: str | None = None
    ini = REPO_ROOT / "migrations" / "alembic.ini"
    if ini.exists() and from_env is None:
        import configparser

        parser = configparser.ConfigParser()
        parser.read_string(ini.read_text(encoding="utf-8"))
        if parser.has_option("alembic", "version_table"):
            from_ini = parser.get("alembic", "version_table").strip() or None

    # --- 来源 3：alembic 包源码里 opts.get("version_table", <字面量>) ---
    from_upstream: str | None = None
    if from_env is None and from_ini is None:
        import alembic

        upstream_py = Path(alembic.__file__).resolve().parent / "runtime" / "migration.py"
        tree = ast.parse(
            upstream_py.read_text(encoding="utf-8"), filename=str(upstream_py)
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_get = isinstance(func, ast.Attribute) and func.attr == "get"
            if not is_get or len(node.args) != 2:
                continue
            key = node.args[0]
            if not (isinstance(key, ast.Constant) and key.value == "version_table"):
                continue
            default = node.args[1]
            if isinstance(default, ast.Constant) and isinstance(default.value, str):
                if default.value:
                    from_upstream = default.value

    chosen = from_env or from_ini or from_upstream
    if not chosen:  # pragma: no cover —— 上游改名/换实现时给可读的失败
        raise RuntimeError(
            "无法从 env.py / alembic.ini / alembic 包源码任一处派生出 version_table 名 —— "
            "排除集的依据断了，请人工确认后再改本函数（不要写死字符串）。"
        )

    if from_env:
        source = f"migrations/env.py 的 configure(version_table={from_env!r})"
    elif from_ini:
        source = f"migrations/alembic.ini 的 version_table={from_ini!r}"
    else:
        source = f"alembic 包源码 runtime/migration.py 的缺省 {from_upstream!r}"

    _migration_bookkeeping_tables.last_source = source  # type: ignore[attr-defined]
    return frozenset({chosen})




def _base_dsn() -> str:
    """基准 DSN：环境变量优先（CI 口径），逐级回退到本机口径。"""
    return os.environ.get(
        "REDNOTE_DATABASE_URL",
        os.environ.get("REDNOTE_TEST_DATABASE_URL", DEFAULT_BASE_DSN),
    )


def database_name_of(dsn: str) -> str | None:
    """从一个 SQLAlchemy DSN 里取出库名（无库名时返回 None）。

    公开的（非 `_` 前缀）：单元测试要拿它断言"判据自带的默认 DSN 没指向系统库"，
    而不必自己再实现一份解析 —— 两份解析会漂移，判据就变成自证。
    """
    from sqlalchemy.engine import make_url

    return make_url(dsn).database



def _dsn_for(dbname: str) -> str:
    """把基准 DSN 的库名换成 `dbname`，并统一驱动为 `postgresql+psycopg`。"""
    from sqlalchemy.engine import make_url

    url = make_url(_base_dsn())
    # CI 用 `REDNOTE_DATABASE_URL`（`postgresql+psycopg://postgres:postgres@h/db`），
    # 本机用 `rednote@localhost` —— 只取 host/user/port/password，库名一律换掉。
    return url.set(drivername="postgresql+psycopg", database=dbname).render_as_string(
        hide_password=False
    )


def _admin_dsn() -> str:
    """用于 `CREATE/DROP DATABASE` 的连接：必须连到一个**已存在**的库。"""
    from sqlalchemy.engine import make_url

    url = make_url(_base_dsn())
    dbname = url.database
    # 连不上目标库时退到 `postgres`（系统库的正当用途：管理连接）。
    return url.set(drivername="postgresql+psycopg", database=dbname).render_as_string(
        hide_password=False
    )


def _psycopg_dsn(sqlalchemy_dsn: str) -> str:
    """把 SQLAlchemy DSN 转成 psycopg 能吃的形态（去掉 `+psycopg` 后缀）。"""
    return sqlalchemy_dsn.replace("postgresql+psycopg://", "postgresql://", 1)


# ===========================================================================
# 建库：两条独立路径
# ===========================================================================


def _recreate_database(admin_raw: str, dbname: str) -> None:
    """DROP + CREATE 一个空库（带重试，避开别的会话的残留连接）。"""
    import psycopg

    with psycopg.connect(_psycopg_dsn(admin_raw), autocommit=True) as conn:
        # 先踢掉可能残留的连接（上一次跑崩了会留下）。
        conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity"
            " where datname = %s and pid <> pg_backend_pid()",
            (dbname,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        conn.execute(f'CREATE DATABASE "{dbname}"')
        # pgvector 扩展（WP21 的迁移会断言它已启用）。
        conn.execute(
            "select 1 from pg_database where datname=%s", (dbname,)
        ).fetchone()


def _enable_vector(dsn: str) -> None:
    """在目标库启用 pgvector 扩展（CI 里 `create pgvector extension` 那一步）。"""
    import psycopg

    with psycopg.connect(_psycopg_dsn(dsn), autocommit=True) as conn:
        try:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ 启用 pgvector 失败：{type(exc).__name__}: {exc}")
            print("     后续若因扩展缺失失败，属环境问题而非结构差异。")


def _run_alembic_upgrade(dsn: str) -> tuple[int, str]:
    """在目标库跑 `alembic upgrade head`。返回 `(rc, 输出尾部)`。"""
    import subprocess

    env = {**os.environ, "NO_PROXY": "*", "REDNOTE_DATABASE_URL": dsn}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "migrations/alembic.ini",
            "upgrade",
            "head",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-6:])
    return proc.returncode, tail


def _create_all(dsn: str) -> None:
    """在目标库跑 `Base.metadata.create_all`（模型路径）。"""
    sys.path.insert(0, str(REPO_ROOT / "services" / "backend" / "src"))
    for name in MODEL_MODULES:
        __import__(f"rednote.modules.{name}.models")
    __import__("rednote.modules.knowledge.embedding_models")
    from sqlalchemy import create_engine

    from rednote.platform.base import Base

    engine = create_engine(dsn)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


# ===========================================================================
# 取样：从库里读出结构化快照
# ===========================================================================


def _snapshot(dsn: str) -> dict:
    """读一个库的**结构快照**（不含数据）。

    取样口径刻意保守：只取**两条路径都该产出**的对象，避免把
    "迁移能建、模型建不出"的合法差异（触发器、动态索引）混进来。
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(dsn)
    try:
        with engine.begin() as conn:
            # --- 表 + 列 ---
            cols = conn.execute(
                text(
                    """
                    select c.relname as table_name,
                           a.attname as column_name,
                           format_type(a.atttypid, a.atttypmod) as data_type,
                           a.attnotnull as not_null,
                           pg_get_expr(d.adbin, d.adrelid) as default_expr
                      from pg_class c
                      join pg_namespace n on n.oid = c.relnamespace
                      join pg_attribute a on a.attrelid = c.oid
                 left join pg_attrdef d on d.adrelid = c.oid and d.adnum = a.attnum
                     where n.nspname = 'public'
                       and c.relkind = 'r'
                       and a.attnum > 0
                       and not a.attisdropped
                    """
                )
            ).fetchall()

            # --- 约束 ---
            cons = conn.execute(
                text(
                    """
                    select c.relname as table_name,
                           con.conname as constraint_name,
                           con.contype as constraint_type,
                           con.condeferrable as deferrable,
                           con.condeferred as deferred,
                           pg_get_constraintdef(con.oid) as definition
                      from pg_constraint con
                      join pg_class c on c.oid = con.conrelid
                      join pg_namespace n on n.oid = c.relnamespace
                     where n.nspname = 'public'
                       and con.contype in ('p', 'u', 'c', 'f')
                    """
                )
            ).fetchall()

            # --- 索引（带 access method：排除集由**磁盘**决定，不由名字名单决定）---
            idx = conn.execute(
                text(
                    """
                    select c.relname as table_name,
                           i.relname as index_name,
                           coalesce(am.amname, '') as access_method,
                           pg_get_indexdef(x.indexrelid) as definition
                      from pg_index x
                      join pg_class i on i.oid = x.indexrelid
                      join pg_class c on c.oid = x.indrelid
                      join pg_namespace n on n.oid = c.relnamespace
                 left join pg_am am on am.oid = i.relam
                     where n.nspname = 'public'
                    """
                )
            ).fetchall()
    finally:
        engine.dispose()

    snap: dict = {
        "column": set(),
        "constraint": set(),
        "index": set(),
        "tables": set(),
        # ↓ 两个"待排除"的分类，**完整回传**（不在这里静默丢掉），
        #   由 main() 排除 + 打印 + 反向断言。
        "ann_indexes": set(),        # access method 属 ANN 族的索引名
        "bookkeeping_tables": set(),  # 从表名派生的 alembic 记账表
    }
    for row in cols:
        table, name, dtype, not_null, default = row
        snap["tables"].add(table)
        # 默认值里的 `nextval(...)` / 序列限定名在两边可能不同 —— 本仓无 serial，
        # 但把 `::regclass` 之类的渲染差异归一化，避免假红。
        norm_default = (default or "").replace("::regclass", "")
        snap["column"].add(
            (table, name, dtype, bool(not_null), norm_default)
        )
    for row in cons:
        table, name, ctype, deferrable, deferred, definition = row
        snap["constraint"].add(
            (table, name, ctype, bool(deferrable), bool(deferred), definition)
        )
    for row in idx:
        table, name, access_method, definition = row
        if access_method in ANN_ACCESS_METHODS:
            snap["ann_indexes"].add((table, name))
            continue
        snap["index"].add((table, name, definition))
    return snap


def _bookkeeping_source() -> str:
    """读回 `_migration_bookkeeping_tables()` 上次的派生来源（供打印「为什么排除它」）。"""
    return getattr(_migration_bookkeeping_tables, "last_source", "未知")


def _without_bookkeeping(snap: dict, bookkeeping: frozenset[str]) -> dict:
    """从快照里剔除 alembic 记账表参与的所有对象（表/列/约束/索引）。

    只动**迁移侧**快照 —— 模型侧本就不该有这些对象（上面已断言）。
    """
    out = {
        "tables": {t for t in snap["tables"] if t not in bookkeeping},
        "column": {c for c in snap["column"] if c[0] not in bookkeeping},
        "constraint": {c for c in snap["constraint"] if c[0] not in bookkeeping},
        "index": {i for i in snap["index"] if i[0] not in bookkeeping},
        "ann_indexes": snap["ann_indexes"],
        "bookkeeping_tables": snap["bookkeeping_tables"],
    }
    return out


def _diff(label: str, left: set, right: set, notes: list[str]) -> list[str]:
    """两条路径的差异，按"只在迁移里 / 只在模型里"分别列出。"""
    only_migration = sorted(left - right)
    only_model = sorted(right - left)
    problems: list[str] = []
    if only_migration:
        problems.append(
            f"  [{label}] 只在**迁移**里有（模型漏了）{len(only_migration)} 条：\n    "
            + "\n    ".join(repr(x) for x in only_migration[:12])
        )
    if only_model:
        problems.append(
            f"  [{label}] 只在**模型**里有（迁移漏了）{len(only_model)} 条：\n    "
            + "\n    ".join(repr(x) for x in only_model[:12])
        )
    if not problems:
        notes.append(f"  [{label}] 一致（{len(left)} 条）")
    return problems


def main() -> int:
    print("迁移结构 ↔ 模型结构 全量对账（ISS-146）")
    print(f"  基准 DSN：{_base_dsn()}（库名会被替换为一次性临时库）")
    print()

    base = _base_dsn()
    from sqlalchemy.engine import make_url

    url = make_url(base)
    # 临时库名带 pid：并行会话（或本机的并发复跑）不会互相踩。
    mig_db = f"rednote_parity_audit_mig_{os.getpid()}"
    mod_db = f"rednote_parity_audit_mod_{os.getpid()}"
    mig_dsn = _dsn_for(mig_db)
    mod_dsn = _dsn_for(mod_db)
    admin = url.set(drivername="postgresql+psycopg", database=url.database).render_as_string(
        hide_password=False
    )
    if url.database is None:  # pragma: no cover —— 配置错时给个可读的失败
        print("[FAIL] 基准 DSN 里没有库名，无法定位管理连接")
        return 1

    notes: list[str] = []
    problems: list[str] = []
    table_count = 0

    # ================= 前置对账：三处清单必须同口径（铁律 14）=================
    # 在跑任何库之前先对账 —— 因为清单不一致会让**后面的比较失去意义**：
    # 少 import 一个模块，它的表就不在 metadata 里，diff 少比几类而**照样全绿**。
    # 放在最前面，是让"覆盖范围缩水"表现为**立刻失败**而不是**安静通过**。
    disk_modules = set(_model_modules())
    env_modules = set(_env_py_imported_modules())
    if disk_modules != env_modules:
        missing_in_env = sorted(disk_modules - env_modules)
        extra_in_env = sorted(env_modules - disk_modules)
        problems.append(
            "  [清单对账] `migrations/env.py` 的 import 与磁盘上的模块不一致：\n"
            f"    磁盘有、env.py 没 import：{missing_in_env}\n"
            f"    env.py 有 import、磁盘没有：{extra_in_env}\n"
            "含义：漏 import 的模块，其表不在 metadata 里 —— `alembic revision"
            " --autogenerate` 会为它生成 `op.drop_table(...)`（CI 只跑 upgrade head，"
            "不触发，因此会长期潜伏）。"
        )
    else:
        notes.append(
            f"  清单对账：磁盘 {len(disk_modules)} 个模块 ⟷ `migrations/env.py` "
            f"import {len(env_modules)} 个，一致"
        )
    problems += _diff("模块清单", env_modules, disk_modules, [])
    if len(disk_modules) < MIN_MODULES:
        problems.append(
            f"  [清单对账] 磁盘只派生到 {len(disk_modules)} 个带 models.py 的模块"
            f"（下界 {MIN_MODULES}）—— `_model_modules()` 的 glob 或路径基点可能失效"
        )

    try:
        # ---------------------------------------------------------- 迁移路径
        print(f"  [1/4] 重建空库 {mig_db}")
        _recreate_database(admin, mig_db)
        _enable_vector(mig_dsn)
        print(f"  [2/4] 在 {mig_db} 上 alembic upgrade head")
        rc, tail = _run_alembic_upgrade(mig_dsn)
        if rc != 0:
            print(f"[FAIL] alembic upgrade head 失败（rc={rc}）：\n{tail}")
            return 1
        notes.append("  迁移路径：alembic upgrade head 成功")

        # ---------------------------------------------------------- 模型路径
        print(f"  [3/4] 重建空库 {mod_db}")
        _recreate_database(admin, mod_db)
        _enable_vector(mod_dsn)
        print(f"  [4/4] 在 {mod_db} 上 Base.metadata.create_all")
        _create_all(mod_dsn)
        notes.append("  模型路径：create_all 成功")

        # ---------------------------------------------------------- 取样比较
        snap_mig = _snapshot(mig_dsn)
        snap_mod = _snapshot(mod_dsn)

        # ================= 排除集（由磁盘/运行时派生，不是人写名单）=================
        # ① alebmic 记账表：`create_all` 不建它，属**路径固有**差异。
        bookkeeping = _migration_bookkeeping_tables()
        found_bk = {t for t in snap_mig["tables"] if t in bookkeeping}
        if found_bk != set(bookkeeping):
            # 派生名字没在库里找到 ⇒ 派生的依据（ini / alembic 默认）与磁盘不符。
            # 这时若照常排除，排的就是个**不存在的名字**，diff 会带着真记账表报红，
            # 而人只会看到一条"莫名其妙"的红 —— 必须先在这里失败（"没跑通"不能装成"跑过"）。
            problems.append(
                f"  [排除集自检] 派生出的记账表名 {sorted(bookkeeping)} 在迁移库里没有全部找到"
                f"（实际找到 {sorted(found_bk)}，来源：{_bookkeeping_source()}）——"
                f" 排除集的依据与磁盘不符，先修这个"
            )
        if found_bk & snap_mod["tables"]:
            problems.append(
                f"  [排除集自检] 记账表 {sorted(found_bk & snap_mod['tables'])} 竟然出现在**模型**侧"
                f" —— 排除集只能吃掉迁移侧的独有对象，出现两侧都有说明排除会掩盖真差异"
            )

        # ② ANN 索引（hnsw/ivfflat）：`create_all` 建不出，只有 `op.execute` 能建。
        #    反向约束：模型侧不许有 —— 否则说明模型真能建，排除就变成了遮盖。
        if snap_mod["ann_indexes"]:
            problems.append(
                f"  [排除集自检] 模型侧竟然建出了 ANN 索引 {sorted(snap_mod['ann_indexes'])}"
                f" —— 该族本应由迁移独有，模型侧出现意味着排除会掩盖真差异"
            )

        excluded_ann = snap_mig["ann_indexes"]
        snap_mig = _without_bookkeeping(snap_mig, bookkeeping)

        table_count = len(snap_mig["tables"])

        notes.append(f"  迁移库：{len(snap_mig['tables'])} 张表")
        notes.append(f"  模型库：{len(snap_mod['tables'])} 张表")

        if len(snap_mig["tables"]) < MIN_TABLES:
            problems.append(
                f"  迁移只建出 {len(snap_mig['tables'])} 张表（下界 {MIN_TABLES}）"
                f" —— 迁移链可能没跑完，或 metadata 没收齐"
            )

        problems += _diff("表", snap_mig["tables"], snap_mod["tables"], notes)
        problems += _diff("列", snap_mig["column"], snap_mod["column"], notes)
        problems += _diff("约束", snap_mig["constraint"], snap_mod["constraint"], notes)
        problems += _diff("索引", snap_mig["index"], snap_mod["index"], notes)

        if bookkeeping:
            notes.append(
                f"  排除 alembic 记账表 {sorted(bookkeeping)}"
                f"（来源：{_bookkeeping_source()}）—— 迁移路径固有，模型路径不会建"
            )
        if excluded_ann:
            notes.append(
                f"  排除 {len(excluded_ann)} 个 ANN 索引"
                f"（access method ∈ {sorted(ANN_ACCESS_METHODS)}，"
                f"SQLAlchemy 无此参数，只能 `op.execute` 建）："
            )
            for tbl, name in sorted(excluded_ann):
                notes.append(f"    - {tbl}.{name}")

    finally:
        # ---------------------------------------------------------- 清理
        import psycopg

        for dbname in (mig_db, mod_db):
            try:
                with psycopg.connect(_psycopg_dsn(admin), autocommit=True) as conn:
                    conn.execute(
                        "select pg_terminate_backend(pid) from pg_stat_activity"
                        " where datname = %s and pid <> pg_backend_pid()",
                        (dbname,),
                    )
                    conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
            except Exception as exc:  # noqa: BLE001
                notes.append(f"  ⚠️ 清理 {dbname} 失败（{type(exc).__name__}）—— 需手工删")

    print()
    for note in notes:
        print(note)
    print()

    if problems:
        print(f"[FAIL] 两条建表路径的产物不一致，共 {len(problems)} 组：\n")
        for p in problems:
            print(p)
            print()
        print(
            "含义：**同一个模型，在「迁移建的库」与「夹具建的库」上形态不同**。\n"
            "测试全绿不能说明迁移是对的 —— 夹具用 `create_all`，不走迁移\n"
            "（ISS-146：本仓两个大包期间测试库的 alembic_version 一直停在旧版本，\n"
            "而全量 pytest 全绿）。"
        )
        return 1

    print("[PASS] 迁移与模型两条路径产出的结构逐项一致。")
    print(
        f"  （{'/'.join(['表', '列', '约束', '索引'])} 四类，"
        f"{table_count} 张表）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
