"""`check_backup_restore_drill.py` 自身的判据（WP47）。

## 为什么判据还要有判据

`tools/quality/check_backup_restore_drill.py` 是**声称自己有防线**的一方（ISS-95）。
它声称"备份能恢复出一个逐项相等的库"，而这句话里最容易被悄悄削弱的有三处，
且**每一处削弱之后它照样全绿**：

1. **指纹算法可能恒等**。若 `normalize_def()` 被改成 `return text`，
   判据会在 2026-10-07 的实测形态上**假红**（dump/restore 的定义文本差异）；
   若被改成 `return ""`，所有定义都同形 ⇒ **假绿**。两者都是"没在干活"。
   ⇒ 故本文件用**真仓库上抓到的那对字符串**做阳性对照（第 2 条），
   用语义不同的定义做阴性对照（第 3 条）。**对照不是编的，是实测抓的**。

2. **比对范围可能缩水**。`row_counts()` 若改成写死的表名单，
   新表会静默不在行数对账里 —— 铁律 14 第 N 次现身。⇒ 第 5 条钉住。

3. **反向验证可能被删**。注入破坏那三条（`reverse:*`）一旦被删，
   判据仍然 21 条全绿，而"全等"退回成无法自证灵敏的恒真式
   （ISS-110 的教训：**绿灯本身没有信息，绿灯的灵敏度才有**）。⇒ 第 6 条钉住。

4. **队列表可能根本没被对账**（2026-10-08 新增）。这不是"可能"——
   首跑就是如此：`seed()` 只造 2 张表，而`compare:row-counts-equal`
   逐表比对 67 张表，其中四张队列表**全0 行**，那四条比对形态是 `0 == 0` **恒真**。
   **判据在自己的队列表上犯了它 docstring 里写下的 ISS-115。**
   ⇒ 第 7 组（**4 条**）钉住：表清单不得脱钩、种子状态必须全覆盖、
   种子行数要够走完状态、列项必须是二元组。

## 与演练脚本的分工

本文件在**单元层**验"函数被直接调用时判得对"；
`check_backup_restore_drill.py` 自己在 `main()` 里真建两库跑一遍，
并用注入破坏自证灵敏。两遍互补：
本文件保证对照本身没被删空，脚本保证对照真的在跑。
第 7 组的价值正在于**兜住演练脚本的退化**：演练脚本那条
`queue:states-match-model` 要真库跑两遍才看得到，pytest 这层是秒级的常驻防线。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_PATH = REPO_ROOT / "tools" / "quality" / "check_backup_restore_drill.py"
BACKUP_TOOL_PATH = REPO_ROOT / "tools" / "ops" / "backup_pg.py"

sys.path.insert(0, str(REPO_ROOT / "tools" / "ops"))
sys.path.insert(0, str(REPO_ROOT / "tools" / "quality"))

import backup_pg  # noqa: E402
import check_backup_restore_drill as tool  # noqa: E402
import drill_db  # noqa: E402

# ---------------------------------------------------------------------------
# 2026-10-07 在真库上抓到的那对字符串（不是编的）
#
# 来源：`python tools/quality/check_backup_restore_drill.py` 首次运行时
# compare:fingerprint-equal 报红，逐行 diff 得出的真值。
# 源库是 alembic 直接建的，目标库是 pg_dump 反解析再 pg_restore 解析回来的 ——
# PG 的解析器把数组字面量的强制转换从「整体转换」改写成「逐元素转换」，
# 语义完全等价（都是 text 对 text 比较），文本不等。
# ---------------------------------------------------------------------------
SRC_FORM = (
    "CHECK (((actor_kind)::text = ANY ((ARRAY['SERVICE'::character varying,"
    " 'SYSTEM'::character varying, 'USER'::character varying])::text[])))"
)
DST_FORM = (
    "CHECK (((actor_kind)::text = ANY (ARRAY[('SERVICE'::character varying)::text,"
    " ('SYSTEM'::character varying)::text, ('USER'::character varying)::text])))"
)


# ===========================================================================
# 判据 1：指纹类别清单自洽（铁律 14：名单 ⇄ SQL 双向对账）
# ===========================================================================


@pytest.mark.regression
def test_fingerprint_classes_match_the_sql_table() -> None:
    """`FINGERPRINT_CLASSES` 与 `_FINGERPRINT_SQLS` 必须逐项相等且不少于 6 类。

    这两处都是**手写清单**。少写一类会让"少比一类"静默发生 —— 而少比一类
    之后判据照样全绿（ISS-153 只防变少不防"少比"）。
    """
    sql_names = tuple(name for name, _sql, _has_def in backup_pg._FINGERPRINT_SQLS)  # noqa: SLF001
    assert sql_names == backup_pg.FINGERPRINT_CLASSES, (
        f"两处手写清单脱钩：常量={backup_pg.FINGERPRINT_CLASSES} SQL={sql_names}"
    )
    assert len(sql_names) >= 6, f"指纹只覆盖 {len(sql_names)} 类（下界 6）"


@pytest.mark.regression
def test_every_class_sql_selects_at_least_an_identity_column() -> None:
    """每条指纹 SQL 除了定义列，至少还有一列身份（否则身份集合退化成空串对空串）。

    "身份"若为空，`objects_sha256` 会变成一堆空行的哈希 ——
    身份集合相等就成了恒真式（ISS-115）。这是第 1 条判据**没覆盖**的那一格：
    清单对得上，不代表每条 SQL 真的产出可区分的身份。
    """
    for name, sql, has_def in backup_pg._FINGERPRINT_SQLS:  # noqa: SLF001
        select_part = sql.split("from", 1)[0]
        cols = [c.strip() for c in select_part.replace("select", "", 1).split(",")]
        assert len(cols) >= (2 if has_def else 1), (
            f"类别 {name} 的 SQL 列数 {len(cols)} 不足（has_def={has_def}）"
        )


# ===========================================================================
# 判据 2：规范化**真的在干活**（阳性对照，用真库抓到的字符串）
# ===========================================================================


@pytest.mark.regression
def test_normalizer_merges_the_real_dump_restore_artifact() -> None:
    """阳性对照：真库上那对 dump/restore 差异字符串必须被规范化成同一个。

    ⚠️ 这是本文件的**重点**。若 `normalize_def()` 退化成恒等函数，
    演练脚本会在每次真跑时报红（假红 ⇒ 迟早被人"修"成恒真 ⇒ 判据失效）。
    若它退化成空串，则一切定义都同形（假绿）。
    两种退化都被这一条挡住，因为对照用的是**真实形态**，不是编的形态。
    """
    assert SRC_FORM != DST_FORM, "对照前提失效：两条原始文本居然相等"
    assert backup_pg.normalize_def(SRC_FORM) == backup_pg.normalize_def(DST_FORM), (
        "规范化抹不掉真库上实测的 dump/restore 反解析差异 ⇒ 演练会假红，"
        "而假红的下场通常是被人把判据调绿（ISS-110 教训）"
    )


@pytest.mark.regression
def test_normalizer_is_idempotent() -> None:
    """规范化必须幂等：再跑一次结果不变。

    不幂等 ⇒ 源库算一遍、目标库算一遍可能停在不同"不动点"上，
    比对结果取决于各自算了几次 —— 那种绿是运气，不是结论。
    """
    once = backup_pg.normalize_def(DST_FORM)
    assert backup_pg.normalize_def(once) == once, "规范化不幂等"


# ===========================================================================
# 判据 3：规范化**没有过度抹平**（阴性对照）
# ===========================================================================


@pytest.mark.regression
def test_normalizer_keeps_semantically_different_definitions_apart() -> None:
    """阴性对照：语义不同的 CHECK 定义，规范化后**必须仍然不同**。

    规范化必然损失灵敏度（它抹掉了 `::类型`），这是**明知**的代价，
    所以必须有一条判据把代价钉在"只抹该抹的"这个范围内。
    这条一旦红，说明规范化已经变成"把判据调绿的旋钮"。
    """
    a = "CHECK (((status)::text = ANY (ARRAY['ACTIVE'::character varying])))"
    b = "CHECK (((status)::text = ANY (ARRAY['ARCHIVED'::character varying])))"
    assert backup_pg.normalize_def(a) != backup_pg.normalize_def(b), (
        "两个取不同的 CHECK 被规范化抹成同形 —— 定义比对已失去区分力"
    )

    # 结构差异（不只是字面量差异）也必须保留
    c = "CHECK (((len) > 1))"
    d = "CHECK (((len) < 1))"
    assert backup_pg.normalize_def(c) != backup_pg.normalize_def(d), (
        "比较方向的差异被抹掉了 —— 规范化把结构信息也抹了"
    )


@pytest.mark.regression
def test_normalizer_preserves_operator_precedence_grouping() -> None:
    """括号不能全抹：`(a OR b) AND c` 与 `a OR (b AND c)` 必须仍然不同。

    "去冗余括号"最容易顺手写成"去所有括号"，那样语义不同的谓词会变成同形 ——
    这是**最难被发现的**一种钝化，因为绝大多数定义仍能被区分开。
    """
    x = "CHECK (((a) OR (b)) AND (c))"
    y = "CHECK ((a) OR ((b) AND (c)))"
    assert backup_pg.normalize_def(x) != backup_pg.normalize_def(y), (
        "括号被全抹 ⇒ 逻辑分组信息丢失，两种不同的谓词被判为同形"
    )


# ===========================================================================
# 判据 4：DSN 方言分离（ISS-174 同族）
# ===========================================================================


@pytest.mark.regression
def test_dsn_dialects_are_separated() -> None:
    """libpq 口径与 SQLAlchemy 口径必须是两个函数，不能混。

    ISS-174 的同一类坑：`migrations/env.py::_resolve_database_url` **强制要求**
    `postgresql+psycopg://`，而 libpq 系列（`pg_dump`/`pg_restore`/`psycopg.connect`）
    **不认**这个前缀 —— 喂错了会被 `except` 吞成"连不上库"。
    演练脚本两侧都要用（alembic + pg_dump + pg_restore），是最容易混的地方。
    """
    assert tool._dsn("x").startswith("postgresql://"), (  # noqa: SLF001
        "libpq 口径必须是裸 `postgresql://`"
    )
    assert not tool._dsn("x").startswith("postgresql+psycopg://")  # noqa: SLF001
    assert tool._dsn_alembic("x").startswith("postgresql+psycopg://"), (  # noqa: SLF001
        "alembic 口径必须带 SQLAlchemy 方言后缀（否则 env.py 当场拒收）"
    )


# ===========================================================================
# 判据 5：行数快照的表名由**系统表**派生（铁律 14）
# ===========================================================================


@pytest.mark.regression
def test_row_counts_derives_table_names_from_the_catalog() -> None:
    """`row_counts()` 必须查系统表拿表名，不许写死名单。

    写死名单 ⇒ 新表静默不在行数对账里，"62 张表逐表相等"变成
    "名单里那几张表相等"。这正是铁律 14 说的：**覆盖范围由人写的地方
    必须改由磁盘决定**。
    """
    src = BACKUP_TOOL_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "row_counts"
    )
    body = ast.unparse(fn)
    assert "_SQL_TABLES" in body, (
        "`row_counts` 不再从 `_SQL_TABLES`（系统表）派生表名 —— 比对范围会缩水"
    )


# ===========================================================================
# 判据 6：反向验证不许被静默删除
# ===========================================================================


def _declared_check_names() -> set[str]:
    """从**磁盘**抽出脚本里声明的判据名（不是再抄一份名单）。

    铁律 14：判据名清单也是"人写"的，故从 AST 现场抽，再与
    `EXPECTED_CHECKS` 对账 —— 两侧都是磁盘上的事实，不靠人记。

    ⚠️ 必须按 `checks.append((...))` 的**结构**取第一个元素，不能用
    "含一个冒号的字符串"这种形态匹配：DSN 字面量 `postgresql://` 同样
    满足那个形态（2026-10-07 实测多算出 2 条 ⇒ 17 ≠ 15 的假红）。
    形态匹配是"看起来像"，结构匹配才是"确实是"。
    """
    tree = ast.parse(TOOL_PATH.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "append"):
            continue
        if not (isinstance(fn.value, ast.Name) and fn.value.id == "checks"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Tuple) and first.elts:
            head = first.elts[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                names.add(head.value)
    return names


@pytest.mark.regression
def test_check_name_count_matches_expected_checks() -> None:
    """脚本里声明的判据名条数必须 == `EXPECTED_CHECKS`。

    `main()` 用**全等**（不是 `>=`）核对条数，这是我特意选的（ISS-153：
    `>=` 只防变少）。但"常量与真实声明"仍可能脱钩 ——
    2026-10-07 我自己就把 15 条写成 13 条，被脚本当场抓出。
    本条在单元层再钉一次，让脱钩在 **pytest 阶段**就红。
    """
    names = _declared_check_names()
    assert len(names) == tool.EXPECTED_CHECKS, (
        f"脚本里声明 {len(names)} 条判据，EXPECTED_CHECKS={tool.EXPECTED_CHECKS}；"
        f"声明={sorted(names)}"
    )


@pytest.mark.regression
def test_reverse_injection_checks_are_still_declared() -> None:
    """三条 `reverse:*` 判据必须在列 —— 删了它们，判据仍会全绿但已失去信息。

    这是 ISS-110 lesson 的直接落地：**"判据变绿了"必须伴随一次注入破坏的
    反向验证**，否则分不清"修好了"和"因为瞎所以绿"。
    若这两条被删，15 条会变成 13 条（被上一条挡住），
    但上一条只管条数 —— 有人补两条无关判据凑数就能绕过去，故本条按名点名。
    """
    names = _declared_check_names()
    for required in (
        "reverse:fingerprint-detects-trigger-loss",
        "reverse:row-count-detects-deletion",
        # 2026-10-08 新增：队列逐列摘要的灵敏度自证。
        # 这一条与上面两条**不同类** —— 那两条验"结构/行数"能抓到破坏，
        # 这一条验"逐列摘要"能抓到**行数不变但内容变了**（把未投递事件标成已投递
        # ⇒ 丢任务）。只验前者的话，逐列摘要可能对内容变化完全不敏感，
        # 而"它比行数强在哪"这句话就没有依据。
        "reverse:queue-digest-detects-content-change",
    ):
        assert required in names, (
            f"反向验证 {required} 不在声明里 —— 演练会退回成无法自证灵敏的恒真式"
        )


# ===========================================================================
# 判据 7：队列对账的"范围"与"覆盖"由磁盘决定（铁律 14，同族）
# ===========================================================================


@pytest.mark.regression
def test_queue_digest_covers_every_declared_queue_table() -> None:
    """`QUEUE_DIGEST_COLUMNS` 的键必须**逐项等于** `QUEUE_TABLES`。

    两处都是手写清单，而它们脱钩的后果是**静默少比一张表**：
    `queue:column-digests-equal` 只在 `QUEUE_DIGEST_COLUMNS` 的键上跑，
    少一张 ⇒ 那张表既不比对也不报错，判据**照样全绿**。
    这与 `test_fingerprint_classes_match_the_sql_table` 同族，
    差别在于这里的失效形态更隐蔽（指纹少一类 vs 队列表少一张）。

    ⚠️ 这条不是"抄一份名单再对" —— 断言的是**两个常量之间相等**，
    两边都在 `drill_db.py` 里，同源可查。
    """
    assert tuple(drill_db.QUEUE_DIGEST_COLUMNS) == tuple(drill_db.QUEUE_TABLES), (
        f"队列摘要的表清单与队列表清单脱钩："
        f"digest={list(drill_db.QUEUE_DIGEST_COLUMNS)} tables={list(drill_db.QUEUE_TABLES)}"
        f" ⇒ 少比的那张表既不报错也不参与对账"
    )


@pytest.mark.regression
def test_queue_seed_covers_every_state_of_the_model() -> None:
    """队列种子的状态取值必须**覆盖 ORM 状态机的全部状态**。

    ⚠️ 这是本轮实测抓出的真缺陷的固化：首版种子 `SEED_JOBS=6` 只覆盖 6 态，
    漏掉 `CANCEL_REQUESTED` / `WAITING_PROVIDER` / `WAITING_RECONCILIATION`
    与 `job_attempts` 的 `RUNNING`，被演练脚本的 `queue:states-match-model` 当场抓红。

    **为什么它必须在 pytest 里再钉一次**：演练脚本那条要在真库上跑两遍
    （约 15 秒 + 建库），CI 上若因故跳过或超时，就没人发现覆盖又缩了。
    而这里的失效形态是"**判据是半个恒真式**"—— 逐列摘要照样全绿，
    只是那三个状态从未被验过。这种绿比红更危险。
    """
    import importlib

    backend_src = REPO_ROOT / "services" / "backend" / "src"
    if str(backend_src) not in sys.path:
        sys.path.insert(0, str(backend_src))
    models = importlib.import_module("rednote.modules.jobs.models")

    gaps = {
        "jobs": sorted(set(models.JOB_STATES) - set(drill_db.SEED_JOB_STATES)),
        "job_attempts": sorted(
            set(models.ATTEMPT_STATES) - set(drill_db.SEED_ATTEMPT_STATES)
        ),
        "idempotency_records": sorted(
            set(models.IDEMPOTENCY_STATES) - set(drill_db.SEED_IDEMPOTENCY_STATES)
        ),
    }
    leaked = {k: v for k, v in gaps.items() if v}
    assert not leaked, (
        f"队列种子未覆盖这些状态：{leaked}"
        f" ⇒ 这些状态从未被逐列对账验过，判据是半个恒真式"
    )


@pytest.mark.regression
def test_queue_seed_row_counts_cover_every_state() -> None:
    """每张队列表的种子行数必须 ≥ 它要覆盖的状态数。

    状态列全了但行数不够 ⇒ 循环取模会让后面的状态**一条都没造出来**
    （`SEED_JOB_STATES[i % 6]`，`i` 只到 4 时后3 态永远缺席）。
    这是上一条判据的**同族但不同形**：上一条查"状态清单有没有列全"，
    本条查"行数够不够把清单走完"。两条都要在。
    """
    import importlib

    backend_src = REPO_ROOT / "services" / "backend" / "src"
    if str(backend_src) not in sys.path:
        sys.path.insert(0, str(backend_src))
    models = importlib.import_module("rednote.modules.jobs.models")

    need = {
        "jobs": len(models.JOB_STATES),
        "job_attempts": len(models.ATTEMPT_STATES),
        "idempotency_records": len(models.IDEMPOTENCY_STATES),
        # 事件层不比"状态机"：它的三种投递形态（已投递/未投递/退避中）
        # 由 `SEED_OUTBOX % 3` 决定 ⇒ 至少 3 才能三种都出现。
        "outbox_events": 3,
    }
    have = {
        "jobs": drill_db.SEED_JOBS,
        "job_attempts": drill_db.SEED_ATTEMPTS,
        "idempotency_records": drill_db.SEED_IDEMPOTENCY,
        "outbox_events": drill_db.SEED_OUTBOX,
    }
    short = {t: (have[t], need[t]) for t in need if have[t] < need[t]}
    assert not short, (
        f"队列种子行数不够（现有, 需要）：{short}"
        f" ⇒ 取模循环造不出全部形态，某些状态一条都不会出现"
    )


@pytest.mark.regression
def test_queue_digest_columns_are_declared_as_pairs() -> None:
    """`QUEUE_DIGEST_COLUMNS` 的每项必须是 `(表达式, 别名)` 二元组。

    这不是形式主义：`queue_digest()` 按 `for expr, _ in columns` 解包。
    若有人写成三元素元组，解包当场 `ValueError`（好）；
    若写成字符串（而不是元组），解包出来是两个字符，SQL 里就成了
    `coalesce(i::text, ...)` —— **比的是列首字母**，
    所有行首字母相同 ⇒ 摘要退化成"行数 + 首字母"的哈希，
    而判据照样全绿。这是"格式退化 ⇒ 灵敏度静默归零"的典型。
    """
    for table, columns in drill_db.QUEUE_DIGEST_COLUMNS.items():
        assert isinstance(columns, tuple) and columns, f"{table} 的列清单为空"
        for item in columns:
            assert isinstance(item, tuple) and len(item) == 2, (
                f"{table} 的列项 {item!r} 不是 (表达式, 别名) 二元组"
                f" ⇒ 解包后 SQL 会只取首字母，摘要退化成'行数 + 首字母'"
            )


# ===========================================================================
# 判据 8：反向验证脚本自身的两个 fail-fast 不得退化成"永不触发"
#
# ★为什么这组也必要：`collect_queue_reconcile_reverse_evidence.py` 的
# **全部价值**就是"变异没生效"与"还原失败"这两条能当场报红。若它们退化成
# 永不触发的死代码，R1~R5 可能全是绿的而**一个变异都没真的打上去**——
# 而读数会长得跟正常一模一样（ISS-143 "没跑"与"跑失败"同形）。
# ===========================================================================

REV_TOOL_PATH = REPO_ROOT / "tools" / "quality" / "collect_queue_reconcile_reverse_evidence.py"


@pytest.mark.regression
def test_reverse_tool_fails_fast_when_anchor_does_not_hit() -> None:
    """锚点命中 0 次（或多次）必须抛异常，不得静默跳过。

    这条判的是 `_mutate()`：它把"变异没生效"从"跳过这个 case"改成抛异常，
    由调用方中止后续变异。**若退化成 `if hits == 0: return`**，
    全部变异会被跳过，脚本仍打�� `RESULT=PASS`。
    """
    src = REV_TOOL_PATH.read_text(encoding="utf-8")
    ast_tree = ast.parse(src)
    funcs = {n.name: n for n in ast_tree.body if isinstance(n, ast.FunctionDef)}
    mutate = funcs["_mutate"]

    raises = [n for n in ast.walk(mutate)
              if isinstance(n, ast.Raise)]
    assert raises, "_mutate 里没有 raise ⇒ 锚点不唯一时不会报红"
    # 判据不能建立在"有 raise"之上（那只是形状）：必须断言 raise 挂在
    # 命中数检查这条路径上。源码里的失败消息是这条判据的锚。
    assert "要求恰好1" in src or "要求恰好 1" in src, (
        "找不到「要求恰好 1」的失败消息 ⇒ _mutate 的 raise 可能已脱离命中数检查"
    )


@pytest.mark.regression
def test_reverse_tool_restore_returns_reason_instead_of_raising() -> None:
    """还原失败必须**返回原因**，不得抛异常。

    首版写的是 `assert new in current`：共享工作树下若并行会话正好改了
    `check_backup_restore_drill.py`，异常会崩掉整个脚本，
    **而磁盘停留在变异状态**（已复现于 Code Review）。
    ⇒ 现在必须返回 `str | None`，由调用方显式报红并中止。
    """
    src = REV_TOOL_PATH.read_text(encoding="utf-8")
    ast_tree = ast.parse(src)
    funcs = {n.name: n for n in ast_tree.body if isinstance(n, ast.FunctionDef)}
    restore = funcs["_restore"]

    assert not [n for n in ast.walk(restore)
                if isinstance(n, ast.Raise)], (
        "_restore 里仍有 raise ⇒ 还原失败会崩掉脚本并把磁盘留在变异状态"
    )

    # ⚠️ **不能只数 `return` 个数**（变异实测 V3 实踩：删掉「还原后二次确认」
    # 那条 return 后判据仍全绿 —— 因为成功路径的 `return None` 还在，
    # 计数照样 ≥ 2）。必须逐条钉住**两个语义失败路径**各自的返回语句。
    #
    # 判据的期望来自本脚本自己的两条语义需求（"同形两处的第二种事实"），
    # 不是来自被测代码的形状：① 找不到变异内容；② 还原后二次确认仍在。
    returns = [n.value for n in ast.walk(restore)
               if isinstance(n, ast.Return)]
    texts = [ast.dump(r) for r in returns if r is not None]
    for needle, why in (
        ("找不到变异内容", "变异内容不在文件里时必须返回原因，否则磁盘可能留残留"),
        ("还原后变异内容仍在", "写回后二次确认失败必须返回原因，否则残留无人知晓"),
    ):
        assert any(needle in t for t in texts), (
            f"_restore 没有返回含「{needle}」的原因 ⇒ {why}"
        )
