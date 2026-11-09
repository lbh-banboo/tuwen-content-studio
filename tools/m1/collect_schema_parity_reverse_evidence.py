#!/usr/bin/env python
"""WP22 收口：**跨包 Schema 判据的反向验证**（把错误实现放回去，判据必须报红）。

产出：`evidence/m1/wp22-schema-parity-reverse.log`

## 为什么需要这个文件

`services/backend/tests/test_schema_parity.py` 与 `tools/quality/check_test_db_isolation.py`
是 WP22 收口时为 **ISS-106 / ISS-107 / ISS-108** 新建的两条跨包护栏。
它们此刻全绿，但这**只能证明"当前工作区看起来合规"**，
不能证明"它们在被违反时会报红"。

而 `test_schema_parity.py` 的 docstring 里已经**写下了反向验证的结论**：

> 反向验证（已实测）：把 `prompt_templates.active_version_id` 的
> `deferrable=True, initially="DEFERRED"` 三行去掉，本测试必须 FAIL。

**"已实测"这三个字如果没有证据文件，就是判据纪律第 9 条的正例**
（"声称自己有防线"比"没有防线"更危险）—— 后人只能选择相信或重做。
本脚本把这三个字变成一份**可复跑的证据**。

## 做法（照 `tools/regression/collect_regression_reverse_evidence.py` 的模式）

仓库整份复制到 `/tmp/rdbgr/repo`（**副本**，真树只读），在副本上做 4 种
"错误实现"，每次跑对应判据并断言它报红；最后断言副本还原后回到全绿、
真树**整树**哈希未变。

**为什么必须在副本里做**（ISS-103 的教训）：第一版反向验证直接变异真树 +
`finally` 还原，结果外层进程被强杀（exit 137），`finally` 没机会跑，
被注入的改动留在了工作区。**"靠 finally 保证还原"在进程被强杀时是假保证。**

## ⚠️ 本库必须是「迁移建出」的，不能是空库（否则整份证据是假绿）

`test_schema_parity.py` 判的是"**模型**建的形态 == **迁移**建的形态"。
若验证库是空的（或夹具建的），则：

* 判据 1 会因为"模型声明的 105 条 FK 在库里不存在"而报错 ——
  报错信息是 `missing`，**与"形态不一致"是两回事**，
  于是 ① 的"报红"就变成了"因为别的原因报错"，结论不成立；
* 判据 2（跨包外键集合）在空库上 `before == after == {}` **恒等**，
  ② 的变异**永远不会被抓到** —— 而它会显示 OK，因为退出码非 0 是别的原因。

因此本脚本在跑用例前，**先重建验证库并 `alembic upgrade head`**，
再核对 `alembic_version` 落在 head（`_db_at_head` 那条 CHECK）。
这条 CHECK 存在的理由就是"库就绪"与"库是空的"必须在证据里可区分。

## 四个用例

| # | 变异 | 期望 |
|---|---|---|
| ① | `prompts/models.py` 去掉 `deferrable=True, initially="DEFERRED"` | `test_schema_parity` 报红（ISS-107） |
| ② | `conftest.drop_own_tables` 改回无脑 `DROP ... CASCADE` | WP20 夹具判据报红（ISS-106） |
| ③ | `test_projects_wp10.py` 把 DSN 字面量从 `os.environ.get` 默认值位置移出，改成硬编码常量 | `check_test_db_isolation.py` 报红（ISS-108） |
| ④ | 把 `_discover_model_modules()` 的 `glob("*/models.py")` 打错成 `glob("*/modelz.py")` | `test_deferrable_fk_count_is_stable` 报红（覆盖范围缩水） |

用例 ④ 是**本脚本自己设计**的：它验证的不是"某条判据能不能红"，
而是**判据的覆盖范围本身有没有守门人** —— 这正是
`test_schema_parity.py` 里那句"若某模块没被 import，它的外键就不会被核对，
判据仍然全绿，但覆盖范围悄悄缩水"所担心的形态。若 ④ 不能报红，
那么 ① 的"报红"也可能是因为碰巧 —— 因为漏检与通过在该文件里同形。

## ⚠️ 用例 ④ 的锚点已随被测代码演进更换过（2026-09-30，本仓第 N 次"脚本与代码脱钩"）

初版 ④ 的锚点是 `services/backend/tests/test_schema_parity.py` 里的
`'    "prompts",\n'`（当时 `_MODEL_MODULES` 是一份**人写的名单**）。
后来该名单按**铁律 14** 改成磁盘派生（`_discover_model_modules()`），
那行字面量从磁盘上**消失了** ⇒ `run_case` 的
`if old not in original` 分支命中，本用例报
`变异锚点未找到：'    "prompts",\n'`。

**这个失败是"脚本与代码脱钩"，不是"判据失效"** —— 两者在输出上都是
一个 BAD，必须分开看（ISS-143 族：两种成因同一个记号）。
修法不是把 `"prompts"` 加回去（那是把判据退回人写名单），
而是把变异点挪到**新的覆盖范围来源**上：改 `glob` 的模式。
它验的性质**没变**（覆盖范围必须有守门人），只是守门人搬了家；
`test_schema_parity.py` 的 `test_deferrable_fk_count_is_stable`
docstring 里也早已写下了这次反向验证的**新版**口径
（"把 `glob("*/models.py")` 改成 `glob("*/modelz.py")`，本测试必须 FAIL，
模块数 17 → 0"）—— 本脚本与那段 docstring 由此恢复同形。

跑法：

    python tools/m1/collect_schema_parity_reverse_evidence.py

退出码 0 = 验证库就绪、4 种变异都被抓住、副本还原后回绿、真树未被触碰。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SELF_PATH = Path(__file__).resolve()
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m1"
OUT_FILE = EVIDENCE_DIR / "wp22-schema-parity-reverse.log"

BACKEND = "services/backend"
PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

#: 「真树未被触碰」的取证范围：**整树**。
#:
#: 这里**刻意不设**「我打算改的那几个文件」清单：只哈希那几个文件是
#: **自证清白** —— 若变异逻辑写错位置（路径拼错、误改了别处），
#: 那份清单上的哈希依然一致，于是"真树未被触碰"在**真的被触碰过**的
#: 情况下照样 PASS。（本文件第一版确有那个 `GUARDED_FILES` 常量，
#: 但从未被引用 —— 定义了却不用，且暗示"只查 4 个"，已删除。）
TREE_SCOPE = (
    "services/backend/src",
    "services/backend/tests",
    "migrations",
    "tools",
)

#: 验证库名。**与各包测试库、默认库都不同**（ISS-108 的约定）。
DB_NAME = "rednote_parity_reverse"

#: 默认基准 DSN（本机口径）。CI 用 `REDNOTE_DATABASE_URL` 覆盖。
#:
#: ⚠️ **不许在这里写死 `rednote@localhost` 并直接使用**：本脚本要进 CI 门 3，
#: 而 CI 的 PG 角色是 `postgres`（见 `ci.yml` 的 `PG_DSN`），不是 `rednote` ——
#: 硬编码会在 CI 上直接 `role "rednote" does not exist`。
#: 这正是 `tools/quality/check_test_db_isolation.py` 要防的 ISS-108，
#: 只不过那条判据的扫描范围只有 `services/backend/tests/`，
#: **扫不到本文件所在的 `tools/`**（已经在 `check_test_db_isolation.py`
#: 里补了「CI 引用的 tools 脚本」这一段扫描范围）。
#:
#: ⚠️ 库名**不得是 `postgres`**（ISS-145）：它是 PostgreSQL 的**系统维护库**。
#: 本脚本只把它当**基准**（库名会被 `_dsn_for()` 换成 `rednote_parity_reverse`
#: 等专用验证库），但基准指向系统库仍是坏默认值 ——
#: 一旦哪天有人直接拿它连库，污染的就是系统库。
DEFAULT_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"


def _base_raw() -> str:
    """基准 DSN 原文：环境变量优先，逐级回退到本机口径。

    顺序：`REDNOTE_DATABASE_URL`（CI 与 `migrations/env.py` 的口径）
    → `REDNOTE_TEST_DATABASE_URL`（测试库隔离的口径）→ 默认。

    ⚠️ 形态刻意写成**规范的两跳 `os.environ.get` 嵌套**，而不是
    `a or b or DEFAULT`：只有前者的字面量**处在"环境变量默认值"位置**，
    `tools/quality/check_test_db_isolation.py` 的 AST 判据才能认出它合规。
    用 `or` 链会让 `DEFAULT_DATABASE_URL` 看起来像一处硬编码 ——
    这正是"约束写在代码里，判据认不出来"的典型（判据与约定必须同形）。
    """
    return os.environ.get(
        "REDNOTE_DATABASE_URL",
        os.environ.get("REDNOTE_TEST_DATABASE_URL", DEFAULT_DATABASE_URL),
    )


def _dsn_for(dbname: str) -> str:
    """SQLAlchemy 形式、库名换成 `dbname` 的 DSN（pytest / alembic 用）。

    统一把驱动归一到 `postgresql+psycopg`：本仓装的是 psycopg v3，
    而 `postgresql://` 会被 SQLAlchemy 解析成默认驱动（psycopg2）。
    """
    from sqlalchemy.engine import make_url

    url = make_url(_base_raw())
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    return url.set(database=dbname).render_as_string(hide_password=False)


def _libpq_for(dbname: str) -> str:
    """libpq 形式、库名换成 `dbname` 的连接串（psycopg 直连用）。"""
    from sqlalchemy.engine import make_url

    return (
        make_url(_base_raw())
        .set(drivername="postgresql", database=dbname)
        .render_as_string(hide_password=False)
    )


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def _repo_rel(path: Path) -> str:
    """仓库内展示路径；不在仓库内时退回绝对路径（不抛异常）。

    ⚠️ 必须容错：macOS 上 `/var` 是 `/private/var` 的符号链接，副本路径
    走 `resolve()` 而 `REPO_ROOT` 已 resolve，两边不同源会 `ValueError`。
    这个坑在 `collect_regression_evidence.py` 上真实发生过并导致**整脚本崩溃、
    一条 CHECK 都不产生**（ISS-102）—— 崩溃与全过在计数上同形。
    """
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _snapshot(repo: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in TREE_SCOPE:
        base = repo / rel
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            out[str(path.relative_to(repo))] = _sha(path)
    return out


def _make_copy() -> Path:
    """整仓副本（排除 .git / 缓存）。真树只读。

    **副本建在 `/tmp`**（不是 `tempfile.mkdtemp()`）：macOS 的 `TMPDIR` 是
    `/var/folders/...`，本机沙箱下"复制 + 跑 pytest"会被拖到 200 秒/次；
    换到 `/tmp` 后是秒级。这里同时也就是符号链接缺陷的现场。
    """
    base = Path("/tmp") / "rdbgr"
    shutil.rmtree(base, ignore_errors=True)
    dest = base / "repo"
    shutil.copytree(
        REPO_ROOT,
        dest,
        ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".pytest_cache", "*.pyc"
        ),
    )
    return dest


def _pytest_env(dbname: str) -> dict[str, str]:
    """跑 pytest 的环境：独立库 + 禁代理（G3 铁律）。

    `no_proxy` 小写也写一遍：本机代理会劫持 localhost（实测过）。
    DSN 由 `_dsn_for()` 派生 —— **不写死用户名**，否则 CI（角色 `postgres`）
    会 `role "rednote" does not exist`。
    """
    env = {
        **os.environ,
        "NO_PROXY": "*",
        "no_proxy": "*",
        "PYTHONPATH": "src",
        # 变异 ④（`models.py`→`modelz.py`）与还原 ⑤ 前后文件**等长**：pyc 靠
        # "mtime(秒) + 大小" 判有效，两步落在同一秒时 ⑤ 会读到 ④ 的旧字节码，
        # 表现为『已还原却仍红』（完整复跑里连红两次，单跑时断时续）。不写字节码即可。
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env["REDNOTE_TEST_DATABASE_URL"] = _dsn_for(dbname)
    return env


def _run_pytest(repo: Path, target: str, *, dbname: str) -> tuple[int, str]:
    """跑 pytest。

    ⚠️ **不要在这里加 `-q`**：`pyproject.toml` 的 `addopts` 已有 `-q`，
    再叠一个变成 `-qq`，pytest 会**不再打印短摘要**（FAILED 行与断言消息
    一起消失），于是所有"期望命中 marker"的断言全部假失败（ISS-24 同源）。
    """
    proc = subprocess.run(
        [PYTHON, "-m", "pytest", "-p", "no:cacheprovider", "--no-header", target],
        cwd=str(repo / BACKEND),
        capture_output=True,
        text=True,
        check=False,
        env=_pytest_env(dbname),
    )
    return proc.returncode, proc.stdout + proc.stderr


def _run_isolation_check(repo: Path) -> tuple[int, str]:
    """跑 ISS-108 的 AST 检查器（不碰数据库，无需库名）。"""
    proc = subprocess.run(
        [PYTHON, str(repo / "tools/quality/check_test_db_isolation.py")],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"},
    )
    return proc.returncode, proc.stdout + proc.stderr


def _prepare_db(name: str) -> tuple[bool, str, str]:
    """重建验证库并迁移到 head。返回 `(ok, head, detail)`。

    **为什么必须重建而不是"确保存在"**：本脚本用例 ② 会真的 `DROP` 掉
    `knowledge_*` 五张表（这正是被测的错误实现），跑完库就残了；
    第二次跑必然得到错误结论。而且残留的旧结构会让
    `create_all` 静默跳过 → 后面所有判据都在测一个结构不对的库。

    ⚠️ 连接信息**全部从基准 DSN 派生**：本机是 `rednote@localhost`，
    CI 是 `postgres@localhost`。写死任一个都会在另一侧直接失败
    （CI 上 `role "rednote" does not exist`，这正是 ISS-108 的形态）。
    """
    import psycopg

    # 管理连接：指向 `postgres` 维护库（要 CREATE/DROP DATABASE，必须换库。）
    admin = _libpq_for("postgres")
    try:
        with psycopg.connect(admin, autocommit=True) as conn:
            cur = conn.cursor()
            cur.execute(f'drop database if exists "{name}" with (force)')
            cur.execute(f'create database "{name}"')
        with psycopg.connect(_libpq_for(name), autocommit=True) as conn:
            # 迁移刻意不代建扩展（属部署层职责，见迁移里的 fail-closed 检查）。
            conn.cursor().execute("create extension if not exists vector")
    except Exception as exc:  # noqa: BLE001
        return False, "", f"{type(exc).__name__}: {exc}"

    env = {
        **os.environ,
        "REDNOTE_DATABASE_URL": _dsn_for(name),
        "PYTHONPATH": str(REPO_ROOT / BACKEND / "src"),
        "NO_PROXY": "*",
        "no_proxy": "*",
    }
    proc = subprocess.run(
        [PYTHON, "-m", "alembic", "-c", "migrations/alembic.ini", "upgrade", "head"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
        env=env,
    )
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr)[-800:]
        return False, "", f"alembic upgrade head 失败：{tail}"

    # 期望 head 从 `alembic heads` 现场取（**不是写死**，见 ISS-44/ISS-71）。
    # ⚠️ 不能用 `ScriptDirectory.from_config()` 在进程内取：迁移文件在模块级
    # 就 `from rednote.modules... import ...`，本脚本的 `sys.path` 里没有
    # backend/src → 直接 `ModuleNotFoundError: No module named 'rednote'`。
    # （实测踩到：脚本在"验证库就绪"那一步整体崩溃。）
    heads_proc = subprocess.run(
        [PYTHON, "-m", "alembic", "-c", "migrations/alembic.ini", "heads"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
        env={
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT / BACKEND / "src"),
            "NO_PROXY": "*",
            "no_proxy": "*",
        },
    )
    expected_head = ""
    for line in heads_proc.stdout.splitlines():
        if "(head)" in line:
            expected_head = line.split()[0]
            break
    if not expected_head:
        tail = (heads_proc.stdout + heads_proc.stderr)[-400:]
        return False, "", f"取不到迁移 head：{tail}"
    with psycopg.connect(_libpq_for(name), autocommit=True) as conn:
        row = conn.execute("select version_num from alembic_version").fetchone()
    actual = row[0] if row else "(空)"
    return True, actual, f"head={expected_head} 库内={actual}"


def main() -> int:
    checks: list[tuple[str, bool, str]] = []
    lines: list[str] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    def emit(text: str = "") -> None:
        lines.append(text)

    def run_case(
        *,
        repo: Path,
        rel: str,
        old: str,
        new: str,
        expect_marker: str,
        name: str,
        label: str,
        runner: str,
        target: str = "",
        dbname: str = "",
        refresh_db: bool = False,
    ) -> None:
        """在副本里做一次变异 → （可选）重建验证库 → 跑判据 → 断言报红 → 还原。

        ## 为什么每个用例前都要重建验证库（`refresh_db=True`）

        用例 ② 的被测错误实现**就是**"无脑 `DROP ... CASCADE`" ——
        它会真的把 `knowledge_*` 五张表从验证库里删掉。若不重建：

        * 后续用例会在一个**残库**上跑，报红/报绿都可能出于别的原因；
        * 尤其 §4：它期待 `test_deferrable_fk_count_is_stable` 报红，
          该判据只看 metadata、与库无关 —— 但它和
          `test_all_fk_deferrability_matches_between_model_and_db`
          **在同一个文件里**，后者会因为"表不存在"而先报错。
          于是 §4 的"报红"是因为**库里缺表**，而不是因为漏 import。
          这正是"判据必须能区分两种实现"（ISS-82）的反面：**两种原因
          报出同一个红，结论就不成立**。
        """
        path = repo / rel
        original = path.read_text(encoding="utf-8")
        if old not in original:
            add(name, False, f"变异锚点未找到：{old[:60]!r}")
            emit(f"  BAD  {label} —— 变异锚点未找到（脚本与代码已脱节）")
            return
        if refresh_db:
            ok_db, head, detail = _prepare_db(dbname)
            if not (ok_db and head):
                add(name, False, f"验证库重建失败：{detail}")
                emit(f"  BAD  {label} —— 验证库重建失败（{detail}）")
                return
            emit(f"        验证库已重建到 head（{head}）")
        try:
            path.write_text(original.replace(old, new, 1), encoding="utf-8")
            if runner == "pytest":
                code, out = _run_pytest(repo, target, dbname=dbname)
            else:
                code, out = _run_isolation_check(repo)
            ok = code != 0 and expect_marker in out
            add(
                name,
                ok,
                f"exit={code}，{'命中' if ok else '未命中'} {expect_marker!r}",
            )
            emit(f"  {'OK ' if ok else 'BAD'}  {label} → exit={code}")
            for line in out.splitlines():
                s = line.strip()
                if (
                    expect_marker in line
                    or s.startswith("FAILED")
                    or s.startswith("ERROR")
                    or s.startswith("[FAIL]")
                    or s.startswith("E ")
                ):
                    emit(f"        | {s[:150]}")
        finally:
            path.write_text(original, encoding="utf-8")

    # ------------------------------------------------------------------ §0
    emit("# WP22 跨包 Schema 判据的反向验证（把错误实现放回去，判据必须报红）")
    emit(f"# 生成时间: {_now()}")
    emit(f"# 命令: python {_repo_rel(SELF_PATH)}")
    emit(
        "# 被测判据: tests/test_schema_parity.py（ISS-106/107）"
        " + tools/quality/check_test_db_isolation.py（ISS-108）"
    )

    before = _snapshot(REPO_ROOT)
    repo = _make_copy()
    emit(f"# 变异副本: {repo}（真树只读）")

    # 验证库：**必须迁移建出**，理由见模块 docstring。
    ok_db, head, detail = _prepare_db(DB_NAME)
    add("reverse-db-at-head", ok_db and head != "", detail)
    emit(f"# 验证库: {DB_NAME}（重建 + alembic upgrade head）")
    emit(f"  {'OK ' if ok_db else 'BAD'}  验证库就绪 → {detail}")
    emit()

    try:
        # -------------------------------------------------------------- §1
        emit("## [1] ISS-107：模型漏写 deferrable → 形态判据必须报红")
        run_case(
            repo=repo,
            rel="services/backend/src/rednote/modules/prompts/models.py",
            old=(
                '            ondelete="RESTRICT",\n'
                "            deferrable=True,\n"
                '            initially="DEFERRED",\n'
            ),
            new=('            ondelete="RESTRICT",\n'),
            expect_marker="deferrability",
            name="rev-deferrable-missing",
            label="① 去掉 active_version_id 的 deferrable 三行",
            runner="pytest",
            target="tests/test_schema_parity.py",
            dbname=DB_NAME,
            refresh_db=True,
        )
        emit()

        # -------------------------------------------------------------- §2
        emit("## [2] ISS-106：夹具改回无脑 CASCADE → 跨包外键集合判据必须报红")
        emit(
            "   （判据宿主是 tests/test_knowledge_wp20.py 的 module 级 autouse 夹具"
        )
        emit(
            "     `_ensure_own_tables` —— 它在建表前后比对跨包外键集合；"
        )
        emit(
            "     触发它需要该模块里一条**真连库**的测试，故 target 取 `env` 用例）"
        )
        run_case(
            repo=repo,
            rel="services/backend/tests/conftest.py",
            old=(
                "        dropped: list[str] = []\n"
                "        kept_order: list[str] = []\n"
                "        for name in names:\n"
                "            if name.lower() in kept:\n"
                "                kept_order.append(name)\n"
                "                continue\n"
            ),
            new=(
                "        dropped: list[str] = []\n"
                "        kept_order: list[str] = []\n"
                "        for name in names:\n"
            ),
            expect_marker="跨包外键集合",
            name="rev-cascade-drop",
            label="② drop_own_tables 改回无脑 DROP CASCADE",
            runner="pytest",
            target=(
                "tests/test_knowledge_wp20.py::test_create_asset_yields_draft_with_version"
            ),
            dbname=DB_NAME,
            refresh_db=True,
        )
        emit()

        # -------------------------------------------------------------- §3
        emit("## [3] ISS-108：DSN 从隔离变量位置移出 → AST 判据必须报红")
        run_case(
            repo=repo,
            rel="services/backend/tests/test_projects_wp10.py",
            old=(
                'DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"\n'
                "TEST_DATABASE_URL = os.environ.get(\n"
                '    "REDNOTE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL\n'
                ").strip()"
            ),
            new=(
                'TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"\n'
                'DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/other"'
            ),
            expect_marker="[FAIL]",
            name="rev-hardcoded-dsn",
            label="③ DSN 硬编码、不再流向 REDNOTE_TEST_DATABASE_URL",
            runner="isolation",
        )
        emit()

        # -------------------------------------------------------------- §4
        emit("## [4] 覆盖范围本身有守门人：派生出的模块数塌缩必须被计数判据抓到")
        emit(
            "   （本用例验证的不是「某条判据能否红」，而是「漏检与通过是否同形」——"
        )
        emit(
            "    见 test_schema_parity.py 的 test_deferrable_fk_count_is_stable）"
        )
        emit(
            "   ⚠️ 变异点改过：原锚点是名单里的 `\"prompts\",` 一行，"
        )
        emit(
            "      但该名单已按铁律 14 改为磁盘派生，那行字面量不存在了。"
        )
        emit(
            "      现在改的是**取数的那个模式**（`*/models.py` → `*/modelz.py`），"
        )
        emit(
            "      验的性质不变（覆盖范围必须有守门人），只是守门人搬了家。"
        )
        run_case(
            repo=repo,
            rel="services/backend/tests/test_schema_parity.py",
            old='glob("*/models.py")',
            new='glob("*/modelz.py")',
            expect_marker="test_deferrable_fk_count_is_stable",
            name="rev-module-not-imported",
            label="④ glob 模式打错一个字（派生塌缩为 0 个模块）",
            runner="pytest",
            target="tests/test_schema_parity.py",
            dbname=DB_NAME,
            refresh_db=True,
        )
        emit()

        # -------------------------------------------------------------- §5
        emit("## [5] 还原后必须回到基线（证明前面是「变异」而非「破坏」）")
        ok_db, head, detail = _prepare_db(DB_NAME)
        if not (ok_db and head):
            add("rev-restore-baseline", False, f"验证库重建失败：{detail}")
            add("rev-restore-isolation", False, "验证库不可用，未执行")
            emit(f"  BAD  验证库重建失败（{detail}）")
            emit()
        else:
            code, out = _run_pytest(repo, "tests/test_schema_parity.py", dbname=DB_NAME)
            green = code == 0 and "passed" in out and "failed" not in out
            add("rev-restore-baseline", green, f"exit={code}，3 passed")
            emit(
                f"  {'OK ' if green else 'BAD'}  ⑤ 副本全部还原 → exit={code}"
                "（期望 3 passed）"
            )
            if not green:
                # 失败时留下 pytest 输出末尾：此前只记退出码，复跑里偶尔红时无从判断原因。
                for line in out.strip().splitlines()[-25:]:
                    emit(f"        | {line}")
            code2, out2 = _run_isolation_check(repo)
            green2 = code2 == 0 and "[PASS]" in out2
            add("rev-restore-isolation", green2, f"exit={code2}，[PASS]")
            emit(
                f"  {'OK ' if green2 else 'BAD'}  ⑥ 隔离检查器还原 → exit={code2}"
                "（期望 [PASS]）"
            )
            emit()

        # -------------------------------------------------------------- §6
        emit("## [6] 真树未被触碰（整树哈希，不是自证清白的那 4 个文件）")
        after = _snapshot(REPO_ROOT)
        changed = sorted(rel for rel in before if before.get(rel) != after.get(rel))
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        same = not changed and not added and not removed
        add(
            "real-tree-untouched",
            same,
            f"{len(before)} 个文件哈希一致",
        )
        emit(
            f"  {'OK ' if same else 'BAD'}  ⑦ 真树 {len(before)} 个文件哈希前后一致："
            f"{'是' if same else '否'}"
        )
        for rel in changed:
            emit(f"        | 内容被改：{rel} —— 这是本工具最严重的失败")
        for rel in added:
            emit(f"        | 新增文件：{rel}")
        for rel in removed:
            emit(f"        | 文件丢失：{rel}")
        emit()
    finally:
        shutil.rmtree(repo.parent, ignore_errors=True)

    # ------------------------------------------------------------------ §7
    failures = [name for name, ok, _ in checks if not ok]
    complete = len(checks) >= EXPECTED_CHECKS
    emit("## [7] 结论")
    for name, ok, detail in checks:
        suffix = f"（{detail}）" if detail else ""
        emit(f"  {'PASS' if ok else 'FAIL'} {name}{suffix}")
    emit()
    emit(
        f"RESULT={'PASS' if (not failures and complete) else 'FAIL'} "
        f"checks={len(checks)}/{EXPECTED_CHECKS} failures={len(failures)}"
    )

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"写出 {_repo_rel(OUT_FILE)}")
    print(
        f"RESULT={'PASS' if (not failures and complete) else 'FAIL'} "
        f"checks={len(checks)} 期望 {EXPECTED_CHECKS} 条 "
        f"failures={len(failures)}"
    )
    for name in failures:
        print(f"  FAIL {name}")
    return 0 if (not failures and complete) else 1


#: 本脚本应产生的 CHECK 条数下界
#: （1 库就绪 + 4 种变异 + 2 项还原 + 真树未动 = 8）。
#: 没有它，"脚本崩了"与"全部通过"在计数上同形（ISS-43）。
EXPECTED_CHECKS = 8


if __name__ == "__main__":
    raise SystemExit(main())
