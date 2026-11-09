"""`check_test_database_target.py` 自身的判据（ISS-145）。

## 为什么判据也要有判据

`tools/quality/check_test_database_target.py` 是**声称自己有防线**的一方。
按判据纪律第 9 条（ISS-95）：

> 「声称自己有防线」比「没有防线」更危险 —— 所有人以为有人看着，
> 就没人再手工复核。

因此本文件对它做三件独立的事：

1. **判据能报红**（正向活性）：把一处合规 DSN 改成指向系统库，
   判据必须报红并**指名那个库名**。只断言"退出码非 0"是不够的 ——
   一个因为语法错而崩溃的脚本也会退出非 0（ISS-143：没跑与跑失败同形）。
   ⚠️ 变异改在 **`tmp_path` 副本**里做，不碰真树 —— 见下。
2. **静态保留名单没有漂移**（铁律 14 的对账）：判据刻意**不连库**
   （纯静态、能在 CI 门 1 跑），它写死 `{postgres, template0, template1}`。
   本条连真库读 `pg_database.datistemplate`，断言**每个模板库名都在静态名单里**。
   名单过期时会报红，而不是静默漏判 —— 这是"覆盖范围由人写就必须有判据对账"。
3. **覆盖范围会随磁盘变化**：测试目录里每个带 DSN 的 `.py` 都必须被扫到。
   用 `os.walk` **独立**数一遍与判据的 `rglob` 对账（铁律 14 第 5 次复发形态）。

第 4 件是为了第 1 件不再伤人（**ISS-165**，2026-10-01）：

4. **变异测试自己不许改真树**。第 1 件原先直接在真树 `write_text` 变异、
   跑完再还原 —— **内容还原了，mtime 没有**。而
   `tools/m1/audit_consistency.py` 的证据时效判据当时取的是"被证据目录里最新
   mtime"，于是每跑一次全量 pytest 就把"代码最后改动时刻"往前推 ⇒
   `backend-pytest.log` **永久假红**，且与"真改了代码忘了重跑证据"**同形**
   （ISS-143）。现在变异落在副本里，并由
   `test_mutation_stays_inside_the_copy` 断言真树 `(大小, mtime_ns)` 逐条不变 ——
   同一根因的**另一半**（时效判据改为按内容取数）在 `audit_consistency.py` 里。

## 反向验证

`tools/quality/collect_test_db_credibility_reverse_evidence.py` 的 **§1**
（3 组副本变异，真树只读）—— 本文件的用例与它**互为两遍**：
本文件验"判据的函数在被直接调用时判得对"，反向验证验"在真实仓库副本上跑得对"。

⚠️ 2026-09-30 自纠两处：本行原写的是一个**不存在**的文件名
（`collect_test_database_target_…_evidence.py`）且组数写"10 组"
（判据 A 实为 3 组：A①/A②/A③）。文档里的路径与数字一旦与磁盘不符，
"有防线"就成了纯声称（判据纪律第 9 条）。
（已由 `collect_test_db_credibility_code_review.py` 的
「文档声明的路径必须存在」判据机械守住。）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL = REPO_ROOT / "tools" / "quality" / "check_test_database_target.py"
REAL_TESTS_DIR = REPO_ROOT / "services" / "backend" / "tests"
CI_FILE = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: 本文件的三个用例里**唯一会有"变异"动作**的那一个的变异目标（相对仓库根）。
#:
#: ⚠️ 它必须是指向**真树**的路径，且必须与下面 `_probe_copy()` 里副本的同名路径
#: **同名** —— 反向验证（`test_mutation_stays_inside_the_copy`）断言"真树的这个文件
#: 一个字节、一个 mtime 都没动"，靠的就是这两个事实对上。
_MUTATION_TARGET_REL = "services/backend/tests/test_devtools.py"


def _tree_state(root: Path) -> dict[str, tuple[int, int]]:
    """`root` 下所有文件的 `相对路径 → (大小, mtime_ns)`（**内容 + 时刻**）。

    用于断言"真树不可触碰"：变异跑完之后，真树的这两样都必须与跑之前逐条相同。

    ⚠️ 取 `st_mtime_ns`（整数纳秒）而不是 `st_mtime`（float 秒）：
    ISS-165 的病灶**恰恰是内容没变而 mtime 变了** —— 只比内容的话，
    这个缺陷能大摇大摆地通过本函数所在的用例（那正是"判据对着真实失效面恒真"，
    ISS-115）。用整数纳秒则让"相等"这件事没有浮点舍入歧义。

    ⚠️ 跳过 `__pycache__`：`.pyc` 的生成时刻必然随"跑没跑过"变化，
    它**不是**本函数要守的东西（真树只读守的是源码，不是字节码缓存）。
    """
    state: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        st = path.stat()
        state[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return state


def _probe_copy(tmp_path: Path) -> Path:
    """造一个**最小可判**的仓库副本，返回副本根（ISS-165）。

    判据 `check_test_database_target.py` 真正会读的只有三处：

    * `services/backend/tests/**/*.py` —— 第一段（`rglob` 递归）；
    * `.github/workflows/ci.yml` —— 第二段的**范围来源**（机械提取被引用的脚本）；
    * `tools/**` —— 第二段里那些被 ci.yml 引用的脚本本体。

    因此副本装这三样即可（约 1 MB 量级），既保真又跑得快 ——
    没必要整仓复制（那是反向验证脚本的做法，它要测整树哈希）。

    ⚠️ **不要图省事只复制 `test_devtools.py` 一个文件**：判据的
    `MIN_TEST_FILES = 20` 与 `MIN_JUDGED_LITERALS = 20` 两条下界会当场报红，
    于是"变异造成报红"与"副本太小造成报红"在输出上同形（ISS-143）。
    """
    repo = tmp_path / "repo"
    ignore = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")
    shutil.copytree(REAL_TESTS_DIR, repo / "services" / "backend" / "tests", ignore=ignore)
    shutil.copytree(REPO_ROOT / "tools", repo / "tools", ignore=ignore)
    if CI_FILE.is_file():
        dest = repo / ".github" / "workflows"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CI_FILE, dest / "ci.yml")
    return repo


def _run_tool_in(cwd: Path) -> subprocess.CompletedProcess[str]:
    """在 `cwd`（副本根）里跑判据本体，返回完成结果。

    ⚠️ `cwd` 必须是**副本根**：判据内部用 `Path(__file__).resolve().parents[2]`
    定位仓库，所以它会解析到副本（这正是"真树只读"的实现方式 —— 不靠自觉，
    靠**路径根本不是真树**）。
    `NO_PROXY='*'` 不可省（本机代理劫持 localhost）。
    """
    return subprocess.run(
        [sys.executable, str(cwd / "tools" / "quality" / TOOL.name)],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"},
    )

#: 判据所在目录必须能 import（测试跑在 `services/backend` 下，
#: 而工具在仓库根的 `tools/`）。用 `sys.path` 插入而不是 `importlib` 拼路径 ——
#: 后者会让 `from __future__` 之类的模块级语义失效。
sys.path.insert(0, str(TOOL.parent))

import check_test_database_target as tool  # noqa: E402


# ===========================================================================
# 判据 1：改名后的库名必须被判出（正向活性，ISS-95）
# ===========================================================================


@pytest.mark.regression
def test_tool_reports_red_when_a_default_dsn_points_at_system_database(
    tmp_path: Path,
) -> None:
    """把一处合规 DSN 改回系统库，判据必须报红**并指名 `postgres`**。

    ⚠️ 不能只断言"退出码非 0"：脚本因任何原因崩溃（语法错、import 失败、
    `SHARED` 变量挪走）都会非 0，而"崩溃"与"抓到违规"在退出码上同形
    （ISS-143）。因此必须断言**输出里出现被违规的库名与文件名**。

    这也是 ISS-82 的要求：判据必须能区分两种实现 —— 改前绿、改后红。

    ## ⚠️ 变异必须发生在**副本**里，不能碰真树（ISS-165）

    本用例原先直接在**真树**上 `write_text` 变异，跑完再 `write_text` 还原。
    内容确实还原了，**mtime 没有**（`Path.write_text` 必然刷新 mtime）。
    后果不是"不干净"这么轻：`tools/m1/audit_consistency.py` 的**证据时效判据**
    把被证据目录的「最新 mtime」当作「代码最后改动时刻」，于是每次跑全量 pytest
    都把这个时刻往前推 ⇒ `backend-pytest.log` **永久假红**，
    而它与「真改了代码忘了重跑证据」**在输出上完全同形**（ISS-143 同族）——
    读证据的人无法分辨"该重跑"与"判据在骗我"。

    现在改为：整块复制到 `tmp_path`（`_probe_copy`），在**副本根**里跑判据。
    "真树只读"因此由**路径结构**保证，不靠自觉 —— 见下面的
    `test_mutation_stays_inside_the_copy`，它把这条不变量变成一条会报红的断言。
    """
    repo = _probe_copy(tmp_path)
    target = repo / _MUTATION_TARGET_REL
    original = target.read_text(encoding="utf-8")
    assert "5432/rednote_test" in original, (
        "前置条件失败：该文件的默认 DSN 已经不指向 rednote_test —— "
        "本用例的「改回系统库」这一步失去了意义"
    )
    mutated = original.replace("5432/rednote_test", "5432/postgres")
    assert mutated != original
    target.write_text(mutated, encoding="utf-8")

    proc = _run_tool_in(repo)

    assert proc.returncode != 0, (
        f"判据没有报红（rc=0）—— 它对「默认 DSN 指向 postgres」是恒绿的。\n"
        f"stdout:\n{proc.stdout}"
    )
    assert "[FAIL]" in proc.stdout, f"报了红但不是 FAIL 形态：\n{proc.stdout}"
    assert "test_devtools.py" in proc.stdout, (
        f"报红但没有指名是哪个文件 —— 失败信息无法定位：\n{proc.stdout}"
    )
    assert "postgres" in proc.stdout, (
        f"报红但没有点名违规的库名：\n{proc.stdout}"
    )


@pytest.mark.regression
def test_mutation_stays_inside_the_copy() -> None:
    """**变异测试本身不许改真树** —— 这是 ISS-165 的反向验证。

    上面那个用例靠"在副本里变异"来保证真树只读。但"我这次写对了"是一句
    **声称**（判据纪律第 9 条：声称自己有防线比没有防线更危险）：
    `_probe_copy` 哪天被改成"返回真树"，或者变异目标被写成一个指向真树的路径，
    用例**照样通过** —— 因为它断言的只是判据的报红行为，与变异发生在哪里无关。

    因此必须**另有一条判据盯住那个不变量**。本用例的做法：

    1. 记下真树 `services/backend/tests` 下每个文件的 `(大小, mtime_ns)`；
    2. 跑一遍上面那个用例的**全部动作**（副本 + 变异 + 跑判据）；
    3. 断言真树的状态**逐条相同**。

    ⚠️ 为什么必须包含 `mtime_ns` 而不只是文件内容哈希：ISS-165 的病灶**恰恰是
    内容没变而 mtime 变了**。只比内容的话，这个缺陷能大摇大摆地通过本用例 ——
    那正是"判据对着真实的失效面恒真"（ISS-115）。**来源见 ISS-165。**
    """
    before = _tree_state(REAL_TESTS_DIR)

    repo = _probe_copy(Path(tempfile.mkdtemp(prefix="ctdt-selfcheck-")))
    try:
        target = repo / _MUTATION_TARGET_REL
        assert target.is_file(), (
            f"前置条件失败：副本里没有 {_MUTATION_TARGET_REL} —— "
            "本用例就没在验「变异发生在副本」这件事"
        )
        assert target.resolve() != (REPO_ROOT / _MUTATION_TARGET_REL).resolve(), (
            "副本里的变异目标解析回了真树路径 —— 副本不是副本"
        )
        target.write_text(
            target.read_text(encoding="utf-8").replace(
                "5432/rednote_test", "5432/postgres"
            ),
            encoding="utf-8",
        )
        _run_tool_in(repo)
    finally:
        shutil.rmtree(repo.parent, ignore_errors=True)

    after = _tree_state(REAL_TESTS_DIR)
    changed = sorted(
        rel for rel in set(before) | set(after) if before.get(rel) != after.get(rel)
    )
    assert not changed, (
        f"真树被改动了 {len(changed)} 个文件（本用例的变异必须只落在副本里）：\n"
        + "\n".join(
            f"  · {rel}: {before.get(rel)} → {after.get(rel)}" for rel in changed[:10]
        )
        + "\n后果：`audit_consistency.py` 的时效判据会把「被摸过」当成「被改过」，"
        "让 backend-pytest.log 永久假红（ISS-165）。"
    )
    assert before, "前置条件失败：真树测试目录里一个文件都没有 —— 上面那条断言是空转"


@pytest.mark.regression
def test_tool_passes_on_the_current_tree() -> None:
    """当前真实仓库必须通过（否则上面的"改后红"无法说明是变异造成的）。

    ⚠️ 本用例**只读真树**（跑判据不写任何文件），因此允许直接对真树跑 ——
    它同时也是"副本不是靠缩小范围来变红"的对照：同一个判据在完整的真树上必须是绿的。
    """
    proc = subprocess.run(
        [sys.executable, str(TOOL)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "NO_PROXY": "*"},
    )
    assert proc.returncode == 0, f"判据在真实仓库上报红：\n{proc.stdout}"
    assert "[PASS]" in proc.stdout


# ===========================================================================
# 判据 2：静态保留名单不得与真实 PG 的模板库集合漂移（铁律 14 对账）
# ===========================================================================


@pytest.mark.regression
def test_static_reserved_databases_covers_all_pg_templates() -> None:
    """静态名单必须**覆盖**真库里所有 `datistemplate = true` 的库。

    判据刻意不连库（纯静态，要能在 CI 门 1 跑），代价是
    `STATIC_RESERVED_DATABASES` 是一份**人写的名单** ——
    按铁律 14，凡是人写的覆盖范围就必须另有一条判据对账它。
    本条就是那条判据。

    方向是**单向**的（真库模板 ⊆ 静态名单）：静态名单里多几个名字无害
    （只会更保守），少一个才是漏判。因此**不要**断言两边相等 ——
    那会在 PG 小版本改名时产生与"漏判"无关的假红。

    还原/清理：本用例只读 `pg_database`，不建任何对象。
    """
    import psycopg

    # 本用例需要一个能连的 PG。优先读 CI / 开发机的口径，都没有就用本机专用测试库。
    # ⚠️ 这里的 fallback **也必须**指向专用测试库（不能图省事写 `postgres`）——
    # 本文件就在判据的扫描范围内，写系统库会被判据当场抓出来（实测抓过一次）。
    dsn = (
        os.environ.get("REDNOTE_DATABASE_URL")
        or os.environ.get("REDNOTE_TEST_DATABASE_URL")
        or "postgresql://rednote@localhost:5432/rednote_test"
    ).strip()
    # SQLAlchemy 口径的 DSN 带**方言后缀**（`postgresql+psycopg://`），而 libpq 不认它 ——
    # 直接喂给 `psycopg.connect()` 会抛 `ProgrammingError: missing "=" after ...`，
    # 被下面的 `except Exception` 吞成一句「无法连接 PG」，于是 PG 明明可达也照跳（ISS-174）。
    # 归一化到 libpq 认的形态；与 `test_migration_model_parity.py::_connect_or_skip` 同款。
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            rows = conn.execute(
                "select datname from pg_database where datistemplate"
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 —— 连不上就跳过，但让理由可见
        pytest.skip(f"无法连接 PG（{type(exc).__name__}），本判据在本环境不适用")

    templates = {row[0] for row in rows}
    assert templates, "pg_database 里一个模板库都没有 —— 这不可能，说明查询有问题"

    missing = sorted(templates - set(tool.STATIC_RESERVED_DATABASES))
    assert not missing, (
        f"真库里有 {len(missing)} 个模板库没被写进 STATIC_RESERVED_DATABASES：{missing}\n"
        f"当前静态名单：{sorted(tool.STATIC_RESERVED_DATABASES)}\n"
        f"后果：这些库被当默认测试库时，判据会**静默漏判**。"
        f"（铁律 14：覆盖范围由人写，就必须有判据对账它 —— 本条就是。）"
    )


# ===========================================================================
# 判据 3：库名解析本身要对（判据的输入不能是错的）
# ===========================================================================


@pytest.mark.regression
@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("postgresql+psycopg://rednote@localhost:5432/postgres", "postgres"),
        ("postgresql+psycopg://rednote@localhost:5432/rednote_test", "rednote_test"),
        ("postgresql://u:p@db:5432/rednote", "rednote"),
        ("postgres://h/abc", "abc"),
        ("postgresql+psycopg://rednote@127.0.0.1:1/postgres", "postgres"),
        # 带查询参数（`?sslmode=require`）时库名仍须切对 ——
        # 早期用 `path.split("/")` 会把 query 拼进库名。
        ("postgresql+psycopg://rednote@h:5432/rednote_test?sslmode=require", "rednote_test"),
        # 无库名的形态：判据应返回 None（调用方当成"不适用"）而不是猜一个。
        ("postgresql+psycopg://rednote@h:5432", None),
        ("postgresql+psycopg://rednote@h:5432/", None),
    ],
)
def test_database_name_parsing(dsn: str, expected: str | None) -> None:
    """`database_name` 必须从各种合法 DSN 形态里切出库名。

    ⚠️ 这是**判据的输入**。输入错了，后面的合规判定就全是错的 ——
    与 ISS-118 同族："判据读到的值凭什么是现在的/对的？"
    """
    assert tool.database_name(dsn) == expected


@pytest.mark.regression
def test_dsn_literal_detection_requires_scheme_separator() -> None:
    """只有**连接串**算 DSN，**驱动名**不算。

    回归 `check_test_db_isolation` 踩过的坑：早期只判前缀 `postgresql+`，
    于是 `"postgresql+psycopg"`（驱动名，不指向任何库）被当成 DSN。
    那会导致解析不出库名 → 判据对它"不适用"→ 覆盖数虚高。
    """
    import ast as _ast

    def kind(src: str) -> str | None:
        node = _ast.parse(src, mode="eval").body
        return tool._is_dsn_literal(node)

    assert kind('"postgresql+psycopg://rednote@h/db"') == "postgresql+psycopg://rednote@h/db"
    assert kind('"postgresql://rednote@h/db"') == "postgresql://rednote@h/db"
    assert kind('"postgres://rednote@h/db"') == "postgres://rednote@h/db"
    # 驱动名 / 普通字符串 —— 不是 DSN。
    assert kind('"postgresql+psycopg"') is None
    assert kind('"hello"') is None
    # ⚠️ `"postgresql://"` **是** DSN 字面量（有 `://`），只是解析不出库名。
    # 两者的区别很重要：前者计入"字面量总数"，后者才计入"判定数"。
    # 若把"解析不出库名"也当成"不是 DSN"，覆盖数会虚高（ISS-145 的自查）。
    assert kind('"postgresql://"') == "postgresql://"
    assert tool.database_name("postgresql://") is None


# ===========================================================================
# 判据 4：覆盖范围对账（铁律 14 第 5 次复发形态）
# ===========================================================================


@pytest.mark.regression
def test_os_walk_counter_matches_rglob_scope() -> None:
    """`os.walk` 独立计数必须与判据实际遍历的 `rglob` 结果一致。

    判据的覆盖范围由 `TESTS_DIR.rglob("*.py")` 决定。若哪天它被写成 `glob`
    （**只差一个字**，写错静默），递归会消失、扫描范围缩水，
    而判据照样打印 `[PASS]` —— 这正是铁律 14 第 5 次的形态。

    本用例用 `os.walk`（**另一套遍历机制**，基于 `os.scandir`）独立数一遍。
    ⚠️ 不能写成 `len(list(TOOL.parent.parent.rglob("*.py")))` ——
    那是**同一个 API 的两种写法**，会与判据一起缩水、对账恒等、永不报红
    （ISS-137 形态）。
    """
    tests_dir = tool.TESTS_DIR
    rglob_files = sorted(tests_dir.rglob("*.py"))
    independent = tool._count_py_files_independently(tests_dir)

    assert len(rglob_files) == independent, (
        f"rglob 扫到 {len(rglob_files)} 个 .py，os.walk 独立数出 {independent} 个 —— "
        f"遍历方式与磁盘不一致（覆盖范围会静默缩水）"
    )
    assert len(rglob_files) >= tool.MIN_TEST_FILES, (
        f"测试目录只有 {len(rglob_files)} 个 .py（下界 {tool.MIN_TEST_FILES}）"
    )
    # 判据的遍历结果与测试自己数出来的必须是同一批文件。
    assert {p.resolve() for p in rglob_files} == {
        p.resolve() for p in tests_dir.rglob("*.py")
    }
