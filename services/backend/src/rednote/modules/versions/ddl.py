"""本模块表的**数据库级保证**（DDL）。

**为什么单独一个文件**：这些 DDL 有两个消费方 ——
  1. 迁移（`migrations/versions/…`）负责把它建出来；
  2. 验证脚本 / 测试负责回查它是否真的存在。

若两边各自手写 SQL 与名字，就会出现"迁移建了 `trg_x`、验证脚本找 `trg_y`"
这种**看起来通过、实际没有保护**的情况。名字与 SQL 只有一处来源才靠得住。

**为什么触发器而不是纯 CHECK**：需要跨行判断的不变量（"依赖边两端必须在
同一项目"）无法用 CHECK 表达 —— CHECK 只能看当前行。同理"不可变"也需要
触发器才能管住绕过 ORM 的写入。

**这里不 import FastAPI 相关模块**：迁移脚本要 import 本文件，
而迁移运行环境应当尽量轻。只有 SQLAlchemy 与 platform 层。

`RAISE EXCEPTION ... USING MESSAGE = ...` 一律用字符串拼接而**不用 `%` 占位符**：
psycopg 的 paramstyle 会把 `%` 当参数占位符，未提供参数时直接报
"unsupported format character"。这是真踩过的坑。
"""

from __future__ import annotations

from typing import Final

from rednote.platform.immutability import immutable_trigger_ddl

# 本模块用 `guard_immutable` 登记的表。迁移据此为每张表建不可变触发器。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = ("version_nodes", "version_dependencies")

# ---------------------------------------------------------------------------
# 依赖边的"同项目"不变量
# ---------------------------------------------------------------------------

DEPENDENCY_SCOPE_FUNCTION: Final[str] = "rednote_check_dependency_scope"
DEPENDENCY_SCOPE_TRIGGER: Final[str] = "trg_version_dependencies_scope"
# PostgreSQL check_violation。用 23514 而不是自定义码：
# 语义上它就是"某条数据规则被违反了"，与本表的 CHECK 同族，
# 都该映射到 422 VALIDATION_FAILED。
DEPENDENCY_SCOPE_SQLSTATE: Final[str] = "23514"


def dependency_scope_trigger_ddl() -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    规则：依赖边的两端若**都有**项目归属，则必须相同；
    任一端为 NULL（平台级版本）视为"无项目范围"，放行。

    为什么"平台级"要放行：平台级版本本来就没有"项目"这个属性，
    谈不上跨项目。若强行要求它归属某个项目，就得填一个虚假的项目 id，
    反而制造出错误的数据。

    只挂 `BEFORE INSERT`：本表不可变（UPDATE / DELETE 由另一个触发器拦），
    因此边一旦写入就不会变，不需要在 UPDATE 上再判一次。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {DEPENDENCY_SCOPE_FUNCTION}() RETURNS trigger AS $$\n"
        "DECLARE\n"
        "    parent_project uuid;\n"
        "    dependency_project uuid;\n"
        "BEGIN\n"
        "    SELECT project_id INTO parent_project\n"
        "      FROM version_nodes WHERE id = NEW.parent_version_id;\n"
        "    SELECT project_id INTO dependency_project\n"
        "      FROM version_nodes WHERE id = NEW.dependency_version_id;\n"
        "    IF parent_project IS NOT NULL\n"
        "       AND dependency_project IS NOT NULL\n"
        "       AND parent_project <> dependency_project THEN\n"
        "        RAISE EXCEPTION USING\n"
        "            MESSAGE = '不允许跨项目引用版本：'\n"
        "                      || parent_project::text\n"
        "                      || ' <> '\n"
        "                      || dependency_project::text,\n"
        f"            ERRCODE = '{DEPENDENCY_SCOPE_SQLSTATE}';\n"
        "    END IF;\n"
        "    RETURN NEW;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE TRIGGER {DEPENDENCY_SCOPE_TRIGGER}\n"
        "BEFORE INSERT ON version_dependencies\n"
        f"FOR EACH ROW EXECUTE FUNCTION {DEPENDENCY_SCOPE_FUNCTION}()"
    )
    drop_trigger = (
        f"DROP TRIGGER IF EXISTS {DEPENDENCY_SCOPE_TRIGGER} ON version_dependencies"
    )
    drop_function = f"DROP FUNCTION IF EXISTS {DEPENDENCY_SCOPE_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


def consistency_items() -> list[tuple[str, str, str, str, str]]:
    """本模块**自己的**函数（`downgrade` 第 2 步的唯一覆盖来源）。

    **只有 1 项**：`rednote_check_dependency_scope` —— 它由本包创建，
    只被 `version_dependencies` 的 `trg_version_dependencies_scope` 引用，
    因此 `downgrade` **必须**删它。

    **为什么不直接用 `all_trigger_ddl()` 做第 2 步（ISS-79 的根因）**：
    `all_trigger_ddl()` 的前 2 项来自 `immutable_trigger_ddl()`，它们的
    `drop_function` 指向 `DROP FUNCTION IF EXISTS rednote_reject_mutation()`
    —— 那是**跨包共用的函数**（WP15/16/17/18/20/22/23/25 都挂着它的触发器）。
    对 `all_trigger_ddl()` 的第 4 项做 `DROP FUNCTION`，就会在
    WP11 自己的表已经删掉之后，去删一个仍被 `trg_version_nodes_immutable`
    的"兄弟们"引用的函数。

    ★ 本包的两张表**同时**承载两类对象，这正是 ISS-79 只在本包与 WP15
    显形的结构性原因：
      · `version_nodes` / `version_dependencies` 的不可变触发器
        → 函数 `rednote_reject_mutation`（**别人的**，不许删）；
      · `version_dependencies` 的 scope 触发器
        → 函数 `rednote_check_dependency_scope`（**我的**，必须删）。
    两类函数在 `all_trigger_ddl()` 里长得一模一样（都是元组的第 5 项），
    只有本函数把"归属"这件事显式表达出来。

    对账见 `test_versions_wp11.py::test_consistency_items_excludes_shared_function`
    —— 它断言本函数的返回集合与 `all_trigger_ddl()` 里"第 5 项不是共用函数"
    的那部分**逐字相同**（防止将来有人把 scope 项误挪走或把不可变项塞进来）。
    """
    scope = dependency_scope_trigger_ddl()
    return [("version_dependencies", scope[0], scope[1], scope[2], scope[3])]


def all_trigger_ddl() -> list[tuple[str, str, str, str, str]]:
    """本模块需要建立的全部触发器。

    返回 `[(表名, 函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP), …]`。

    **顺序有意义**：函数 DDL 用 `CREATE OR REPLACE`，多张表共用同一个
    不可变函数，重复执行是安全的（幂等）；触发器 DROP 也必须先于函数 DROP，
    否则函数仍被触发器引用，PG 会拒绝删除。

    ⚠️ **第 5 项（函数 DROP）混合了两类归属**，因此 `downgrade` **不得**
    直接遍历本函数的返回值去 `DROP FUNCTION`（ISS-79）。第 2 步请用
    `consistency_items()`。
    """
    items: list[tuple[str, str, str, str, str]] = []
    for table in IMMUTABLE_TABLES:
        function_ddl, trigger_ddl, drop_trigger, drop_function = (
            immutable_trigger_ddl(table)
        )
        items.append((table, function_ddl, trigger_ddl, drop_trigger, drop_function))

    items.extend(consistency_items())
    return items


def trigger_names() -> tuple[str, ...]:
    """全部应存在的触发器名（验证脚本与测试用）。"""
    names = [f"trg_{table}_immutable" for table in IMMUTABLE_TABLES]
    names.append(DEPENDENCY_SCOPE_TRIGGER)
    return tuple(names)


def immutable_function_names() -> tuple[str, ...]:
    """本模块的不可变触发器**引用的**函数名：只有共用那一个。

    它是**跨包共用**的，`downgrade` 不许删（删了会让其它 8 个包的保护
    一起失效，且 WP11~WP25 的卸载顺序稍有不同就会撞
    `DependentObjectsStillExist`）。与 `immutable_function_names` 同名的
    函数在 WP25 的 `modules/billing/ddl.py` 也有，两处含义一致：
    "本模块用到的不可变函数，但不是我建的"。
    """
    return ("rednote_reject_mutation",)


__all__ = [
    "DEPENDENCY_SCOPE_FUNCTION",
    "DEPENDENCY_SCOPE_SQLSTATE",
    "DEPENDENCY_SCOPE_TRIGGER",
    "IMMUTABLE_TABLES",
    "all_trigger_ddl",
    "consistency_items",
    "dependency_scope_trigger_ddl",
    "immutable_function_names",
    "trigger_names",
]
