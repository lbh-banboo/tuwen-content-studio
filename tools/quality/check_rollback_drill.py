#!/usr/bin/env python
"""回滚演练判据（WP47 ⑤⑥）：升级失败必须能回到已知良好版本，且**真的回得去**。

## 为什么必须有这条判据

WP47 的验收原文是「RPO ≤ 24h / RTO ≤ 8h 有**真跑过**的证据」，手段是
④ 备份恢复演练（`check_backup_restore_drill.py`）与 **⑤ 回滚演练**。
"备份能恢复"与"升级失败能回滚"是**两条独立的能力**：备份救的是**数据**，
回滚救的是**版本**。只练前者，出事时仍可能卡在"回不去的版本"上。

而"回滚能用"这件事在本仓**此前没有任何判据走过**：CI 只跑 `alembic upgrade head`。

## 与 `check_migration_roundtrip.py` 的分工（刻意不重叠）

| 判据 | 走的路径 | 回答的问题 |
|---|---|---|
| `check_migration_roundtrip.py` | `upgrade head → downgrade base → upgrade head` | **整条链可逆吗**（ISS-79） |
| **本判据** | 单步 `downgrade -1`、升级失败注入、数据阻断回滚 | **生产升级失败时能不能回退**（⑤⑥） |

两者都碰迁移，但断言的对象不同：前者比"存在性集合"在 base 态是否清空，
后者比"我在 head 上改一个版本能不能回、失败了停在哪一步、回退会不会动数据"。
**刻意不复用它的实现** —— 复用的代价是两处需求被绑在一根绳上，
将来一个包要改口径就得同时说服另一个包。

## 判据形状：一个私有库上的六幕剧

```
[幕 1] 建到 head → 指纹 FA                    ← 基准态
[幕 2] 注入破坏（DROP 一个索引）→ 指纹必须变 → 再建回来 → 指纹必须复原
                                              ← 证明"指纹相等"这句话是**灵敏的**（ISS-110）
[幕 3] 单步回滚 downgrade -1 → 版本落位、
       head 的 upgrade() 造的对象**全部消失** → 再 upgrade → 指纹复原
                                              ← ★ 2026-10-09 换谓词（原为"CHECK 字面量域缩小"，
                                                只对'取值域型迁移'成立；现与迁移形态无关）
[幕 4] 存量数据阻断回滚：沿链找**最近的**"downgrade 会收窄 CHECK 取值域"的迁移，
       导航到它，插入一条"下版本不允许"的行 → 它的 downgrade 必须**失败**
       且必须原子（版本不动 / 行不丢 / 约束不半改）→ 删行后必须能回
                                              ← ★ 2026-10-09 换目标（原为"head 自己收窄"，
                                                而 head 是加列型；能力仍在，只是落在别的迁移上）
[幕 5] 升级失败注入（两种粒度）：
       ① 让**运行中的第一条**迁移失败 → 版本纹丝不动 + 它已造的对象被整体回滚
       ② 让**运行中的最后一条**迁移失败 → 版本推进到上一条已提交的迁移 + 其对象存活
                                              ← 这两条合起来才能区分
                                                `transaction_per_migration` 的真/假
[幕 6] 反向验证：把 `transaction_per_migration` 翻成 False，**同一个场景、同一个注入、
       只差一行**，落点**必须不同**（run 起点 vs 上一条已提交）
                                              ← 证明幕 5② 不是恒真式
```

## ★ 幕 5 为什么必须有两种粒度（判据纪律第 4 条的应用）

只做 ① 的话，`transaction_per_migration=True` 与 `False` 的观测**完全一样**
（整个 run 只有一条迁移，无论哪种语义，失败后版本都回到起点）⇒ 那条判据
**测不出它想测的东西**。必须让 run 里**两条迁移、后一条失败**，才能区分：

* `True`  ⇒ 版本停在**第一条**（它已提交）⇒ 我在线上"回到上一个已发布版本"是可行的；
* `False` ⇒ 版本停在**起点**（整个 run 一个事务）⇒ 前一条迁移的成果也被丢掉。

这两种语义对运维的含义完全不同（前者可逐步前进，后者一失败回到 run 起点），
所以它们是**两种独立事实**，不是同一事实的两种写法。

## ★ 幕 6 为什么必须真跑（ISS-110 / ISS-95）

幕 5② 与幕 5① 的观测**必须不同**，否则幕 5② 测的是空气。而"它们不同"这句话
在写判据的那一刻只是**我的推理**。判据纪律第 3 条：**「声称自己有防线」比
「没有防线」更危险** —— 文档里写"有判据保护"，就必须能当场指出脚本里**哪个函数**
在保证它。

故幕 6 把 `migrations/` 整棵复制到临时目录，只翻转副本 `env.py` 里的
`transaction_per_migration=True` 那一行（仓库文件**一个字节都不改** ——
`env.py` 的 `_REPO_ROOT` 与 `alembic.ini` 的 `script_location` 都是自定位的，
副本因此自洽），跑同一场景后断言：

| 臂 | `transaction_per_migration` | 末条失败后版本 | `prev_rev` 建的表 |
|---|---|---|---|
| 对照（幕 5② 本库） | `True` | `prev_rev`（上一条已提交） | 全部在 |
| 实验（沙箱副本） | `False` | `prev2_rev`（run 起点） | 全部不在 |

（2026-10-07 本机实测：`1c1c814b248e` / 4 张表在 ↔ `daea2b27212c` / 4 张表不在。）

两臂落点相同 ⇒ 反向验证那两条判据报红 ⇒ "幕 5② 是灵敏的"这句话被**判据**兜住，
而不是被文案兜住。

## 覆盖范围一律由磁盘/系统表决定（铁律 14）

本判据需要的四个"名单"**全部不写死**：

| 需要的东西 | 来源 |
|---|---|
| 回滚目标版本（head 的前一步） | 解析 `migrations/versions/*.py` 的 `revision`/`down_revision` 建链 |
| 迁移 `upgrade()` 造出的对象（表/列/索引/约束） | AST 解析该迁移 `upgrade()` 的 `op.create_*` / `op.add_column`，按**顶层语句源码序** |
| 迁移的后置对象（用于构造失败） | AST 解析该迁移 `upgrade()` 的 `op.drop_constraint`（tier-2 兜底手段） |
| "downgrade 会收窄取值域"的迁移 | 逐迁移比 `upgrade()`/`downgrade()` 里同名 `create_check_constraint` 的**字面量集合**之差 |
| 被回滚隔离的取值域+列 | 导航到那条迁移后，读 `pg_get_constraintdef` 的字面量集合与**渲染文本里的列名** |

若某处**推导不出来**，判据报 FAIL 并说明"无法构造注入"，
**不允许**退化成"跳过这一条"（ISS-143：没跑与跑失败同形）。

### ★ 2026-10-09 的一次真实失效（记录在此，因为它是本判据的第二次同类事故）

head 从 `e8f07ba8f2bb`（改取值域）前进到 `c4d9f2a17e35`（**加列型**）后，
本判据**一次红了 14 条**（幕 3 的 1 条 + 幕 4 的 5 条 + 幕 5 的 6 条 + 幕 6 的 2 条），
全都因为同一个病因：**能力被 head 的形态过拟合了**。

| 幕 | 旧假设 | 加列型 head 下的现实 |
|---|---|---|
| 3 | head 回滚时某 CHECK 字面量域缩小 | 新 CHECK 是 `char_length(...)<=500`，**无字面量** ⇒ 恒假 |
| 4 | 从 head 的收窄里取"被隔离的取值+列" | head 不收窄 ⇒ 推不出 |
| 5 | 迁移**建表**或**drop check** | 只 `add_column`/`create_check_constraint` ⇒ 推不出 |
| 6 | 对照臂看 `prev_rev` **建的表** | `prev_rev` 建 0 张表 ⇒ 集合空 ⇒ 无区分力 |

修法四条（都在本文件内，且都不写死版本号）：
① `_upgrade_steps()` 覆盖表/列/索引/约束四类后置对象 ⇒ 三种新注入手段；
② 幕 3 换成"head 造的对象在回滚前后**全在 → 全不在**"，与形态无关；
③ 幕 4 沿链找**最近的**收窄迁移（`_narrowing_revisions`）并导航过去演练；
④ 幕 5②/幕 6 的观测域从"表"扩到"全部后置对象"。

## ⚠️ 已知盲区（不做假保证）

1. **不覆盖数据迁移的正确性**：本仓迁移全是 DDL，无数据搬运。
2. **不覆盖 head 之前的中间版本**：只演练 head 与 head-1 两步。
   中间版本的回退由 `check_migration_roundtrip.py` 的 `downgrade base` 覆盖。
3. **注入的是"名字冲突 / 依赖缺失"这类确定性失败**，不是"磁盘满/超时/被 kill"。
   后者的语义（进程被杀 ⇒ 连接断开 ⇒ 服务器回滚当前语句）与前者不同，
   本机无法稳定复现 ⇒ 列入内网待跑，不在这里假装覆盖。
4. **本脚本写 evidence，但 `tools/quality/check_evidence_scope.py` 的 AST 反推（集合 B）
   看不见它** —— 见下方 §证据登记。

## §证据登记（一个已知的判据盲区，已登记为 ISS-193）

本脚本与它的姊妹 `check_backup_restore_drill.py` 采用同一写法：evidence 目录是
`main()` 里的**函数局部变量**（`evidence_dir = REPO_ROOT / "evidence" / "m5"`）。

而 `check_evidence_scope.py::_module_consts()` **只收集模块级赋值**（`tree.body`），
函数内的名字解析不到 ⇒ `writes_evidence()` 返回 `False` ⇒ 本脚本**不在集合 B 里**
⇒ 判据 D2（`B - A1 == ∅`）不会要求把它登记进 `rerun_all_evidence.py::SCOPE_PATTERNS`。

也就是说，**本脚本目前不需要登记，不是因为设计如此，而是因为那条判据有一个洞**。
洞的形态（"函数内局部别名"）**没有**写进 `check_evidence_scope.py` 的"已知盲区"清单
（它只列了 subprocess 重定向与运行期路径）⇒ 属于 ISS-95 形态：
**声称自己有覆盖，而覆盖的入口是虚的。**

本包**不改**那条判据（跨包；且修好它会让集合 B 立刻多出本脚本与姊妹脚本两条，
连锁要求改 `SCOPE_PATTERNS` + `MIN_SCRIPTS` + 两份共享文件，与并行会话有撞车风险）。
处置见 ISS-193。

## 用法

    python tools/quality/check_rollback_drill.py

退出码 0 = 回滚路径可用、升级失败的落位语义符合声明、数据阻断被正确拒绝。
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools" / "ops"))

import backup_pg  # noqa: E402
import drill_db  # noqa: E402

MIGRATIONS_DIR = REPO_ROOT / "migrations" / "versions"
ENV_PY = REPO_ROOT / "migrations" / "env.py"

#: 回滚/恢复预算（WP47 目标 ≤ 8h）。与恢复演练同口径。
#: ⚠️ 本机数据量下必然远低于预算 ⇒ 本条在本机是**弱判据**，
#: 达标结论只能在内网真实数据量下得出（与 `check_backup_restore_drill.py` 同款声明）。
ROLLBACK_BUDGET_SECONDS = 8 * 3600

#: head 态表数下界，防"迁移没跑完 ⇒ 指纹比对静默缩水"（ISS-115 的恒真式）。
MIN_TABLES = 40

#: 迁移链长度下界：`downgrade -2` 要有意义，链上至少 3 个版本。
MIN_REVISIONS = 3

#: 静态 3 + 动态 19 + 反向验证 2（幕 6：翻转 transaction_per_migration）= 24。
#: 新增/删除判据必须同步本值（ISS-153：`>=` 只防变少）。
#: 脚本自己断言 `len(checks) == EXPECTED_CHECKS` 全等 —— 手写错值会当场报红。
EXPECTED_CHECKS = 24

#: 「部分 DDL 回滚」这一格的下界：目标迁移必须先造**至少**这么多个对象排在障碍之前，
#: 才算真的观测到"半成品被整体回滚"。没有下界 ⇒ 一个对象都不在障碍之前时
#: `not leftover` 恒真 ⇒ 恒真式（ISS-115，本仓第 N 次）。
#:
#: ⚠️ 2026-10-09 由 `MIN_PRIOR_TABLES` 更名而来：障碍的粒度已从"表"泛化到
#: "迁移造出的**任意**后置对象"（表 / 列 / 索引 / 约束）。若仍只数表，
#: "加列型迁移"（本仓 head 连着两条都是）的 `prior` 恒为 0 ⇒ 本判据恒真
#: —— 那正是 2026-10-09 实测到的 14 条红的一半来源。
MIN_PRIOR_OBJECTS = 2

#: 幕 3 的下界：head 迁移必须至少造出这么多对象，"单步回滚真的改了东西"才自证得了。
#: 为 0 时 `after_present` 为空这件事**恒真**，与"回滚真的删掉了东西"同形。
MIN_HEAD_OBJECTS = 1

#: 幕 5② / 幕 6 观测 `prev_rev` 成果时的下界：0 个对象 ⇒ "全都在"与"全丢了"同形。
MIN_OBSERVED_OBJECTS = 1

#: 幕 6 要在**沙箱副本**里翻转的那一行。翻转后 `transaction_per_migration=False`
#: ⇒ 整个 run 一个事务 ⇒ 末条迁移失败时前面已成功的迁移也被丢掉。
#: 仓库里的 `migrations/env.py` **一个字节都不改**（自定位路径让副本能独立跑）。
_TPM_LITERAL = "transaction_per_migration=True"
_TPM_FLIPPED = "transaction_per_migration=False"

#: 从约束定义里抽字符串字面量（`'X'::类型` 形态）。
_LITERAL_RE = re.compile(r"'((?:[^']|'')*)'::")

#: 约束定义里"被约束的列"（`((col)::text = ANY ...)` 形态）。
_COLUMN_RE = re.compile(r"\(\((\w+)\)::")

#: **迁移源文件**里的 SQL 字符串字面量（`'BRIEF_DECOMPOSE'` 这种，**无** `::` 后缀）。
#:
#: ⚠️ 与 `_LITERAL_RE` 刻意分开：后者是给 `pg_get_constraintdef` 的**渲染文本**用的，
#: PG 会把字面量渲染成 `'X'::character varying` ⇒ 那个正则要求 `::`。
#: 迁移源码里没有类型后缀 ⇒ 拿 `_LITERAL_RE` 去扫会**恒零命中**，
#: 表现为"整条链没有收窄取值域的迁移"（假绿）—— 2026-10-09 开发期实测踩到。
_SQL_LITERAL_RE = re.compile(r"'((?:[^']|'')*)'")

#: `upgrade()` 里"造出对象"的 op → (对象 kind, 表名实参下标, 名字实参下标)。
#:
#: ⚠️ **参数位次逐 op 不同，不能一律取 args[0]**（2026-10-09 实测踩到：
#: `create_check_constraint(名字, 表, SQL)` 的 args[0] 是**约束名**，
#: 而我按"args[0] 是表"去解释 ⇒ 拿约束名当表名去 `alter table` ⇒
#: `relation "public.ck_..." does not exist`）：
#:
#: | op | 签名 | 表 | 名字 |
#: |---|---|---|---|
#: | `create_table` | `(表)` | 0 | 同表名 |
#: | `add_column` | `(表, Column)` | 0 | 从 `Column(...)` 取 |
#: | 其余 `create_*` | `(名字, 表, ...)` | 1 | 0 |
#:
#: 名字为 `None` ⇒ 表示"按 kind 另有取法"（表用表名、列从 `Column` 取）。
_OBJECT_OPS: dict[str, tuple[str, int, int | None]] = {
    "create_table": ("table", 0, None),
    "add_column": ("column", 0, None),
    "create_index": ("index", 1, 0),
    "create_unique_constraint": ("constraint", 1, 0),
    "create_check_constraint": ("constraint", 1, 0),
    "create_foreign_key": ("constraint", 1, 0),
    "create_primary_key": ("constraint", 1, 0),
    "create_exclude_constraint": ("constraint", 1, 0),
}

#: `upgrade()` 里"删除对象"的 op 名。用于 `_dropped_identifiers()` ——
#: tier-1 的桩必须减掉这些名字（详见该函数的论证：重建同名对象的迁移
#: 若被当"新增对象"预建，会在**注入阶段**就撞 `DuplicateObject`）。
_DROP_OPS = frozenset({
    "drop_table",
    "drop_column",
    "drop_index",
    "drop_constraint",
})

#: `injection_for()` 可能产出的**全部** kind。
#:
#: ⚠️ 这是"声明"；`_apply_injection()` / `_clear_injection()` 是"实现"。
#: `tests/test_rollback_drill.py` 用 AST 把两者对账 —— 加一种手段只改这里
#: 而忘了那两个函数 ⇒ 运行期会**静默走兜底分支**（注入没发生，幕 5 报
#: "注入竟然成功了"），比报错更难查。故声明单独成集合并被判据钉住。
INJECTION_KINDS = frozenset({
    "pre_create_table",
    "pre_create_column",
    "pre_create_index",
    "pre_create_constraint",
    "drop_owned_dependency",
})


# ===========================================================================
# 静态：迁移链与前提声明（都不碰数据库）
# ===========================================================================


def _assign_targets(node: ast.stmt) -> tuple[list[str], ast.expr] | None:
    """模块级 `NAME = <expr>` / `NAME: T = <expr>` → `([NAME], expr)`。"""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        tgt = node.targets[0]
        if isinstance(tgt, ast.Name):
            return [tgt.id], node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        if node.value is not None:
            return [node.target.id], node.value
    return None


def _module_string_consts(tree: ast.Module) -> dict[str, ast.expr]:
    """模块级名字 → 赋值表达式（供把 `op.f(_CHECK_NAME)` 解析回字面量）。"""
    out: dict[str, ast.expr] = {}
    for node in tree.body:
        got = _assign_targets(node)
        if got is None:
            continue
        names, value = got
        for n in names:
            out[n] = value
    return out


def parse_chain() -> tuple[dict[str, str | None], list[str], dict[str, Path]]:
    """解析磁盘上的迁移链，返回 `({revision: down_revision}, [head, head-1, ...], {revision: 文件})`。

    范围由 `glob("*.py")` 决定（铁律 14）：新迁移自动进入，不需要有人回来加名字。

    ⚠️ 第三个返回值（revision → 文件路径）**必须由这里给出**，不能在外面按
    文件名猜（例如 `stem.endswith(f"_{rev}")`）：那是**手写规则**，
    迁移文件一旦换命名风格就静默匹配不上 ⇒ 注入方案退化成 `None`
    ⇒ 幕 5 报"无法构造"甚至被误读成"这个迁移不需要演练"。
    这里是在解析 revision 的**同一遍**里记下的路径，revision 与文件同源，不存在对不上的可能。
    """
    down_of: dict[str, str | None] = {}
    parents: dict[str, list[str]] = {}
    rev_path: dict[str, Path] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        if path.name.startswith("__"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rev: str | None = None
        down: object = None
        for node in tree.body:
            got = _assign_targets(node)
            if got is None:
                continue
            names, value = got
            name = names[0]
            if name not in ("revision", "down_revision"):
                continue
            try:
                literal = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                literal = None
            if name == "revision":
                rev = literal if isinstance(literal, str) else None
            else:
                down = literal
        if rev is None:
            continue
        if isinstance(down, (list, tuple)):
            deps = [d for d in down if isinstance(d, str)]
        elif isinstance(down, str):
            deps = [down]
        else:
            deps = []
        down_of[rev] = deps[0] if len(deps) == 1 else None
        parents[rev] = deps
        rev_path[rev] = path

    # 每根子节点计数（检出分叉：一个 revision 被两个迁移当作 down_revision）
    child_count: dict[str, int] = {}
    for deps in parents.values():
        for d in deps:
            child_count[d] = child_count.get(d, 0) + 1

    heads = [r for r in down_of if child_count.get(r, 0) == 0 and len(parents[r]) <= 1]

    order: list[str] = []
    if len(heads) == 1:
        cur: str | None = heads[0]
        seen: set[str] = set()
        while cur is not None and cur in down_of and cur not in seen:
            seen.add(cur)
            order.append(cur)
            cur = down_of[cur]
    return down_of, order, rev_path


def declares_transaction_per_migration() -> bool:
    """`migrations/env.py` 是否声明 `transaction_per_migration=True`。

    这是**幕 5 的语义前提**：它决定"升级跑到一半失败"停在**起点**还是
    停在**上一条已提交的迁移**。它是"声明"，幕 5 是"观测" ——
    两者是独立事实（声明在而行为不同是可能的，例如某迁移里有非事务性 DDL），
    因此两处都留判据，不是重复。
    """
    tree = ast.parse(ENV_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "configure"):
            continue
        for kw in node.keywords:
            if kw.arg != "transaction_per_migration":
                continue
            if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                return True
    return False


# ===========================================================================
# 从迁移 AST 推导"注入升级失败"的手段（铁律 14：不写死表名/约束名）
# ===========================================================================


def _resolve_str(node: ast.AST, consts: dict[str, ast.expr], depth: int = 0) -> str | None:
    """把表达式解析成字符串：字面量 / 模块级名字 / `op.f(X)` 包裹。"""
    if depth > 6:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        ref = consts.get(node.id)
        return None if ref is None else _resolve_str(ref, consts, depth + 1)
    if isinstance(node, ast.Call):
        for arg in node.args:
            got = _resolve_str(arg, consts, depth + 1)
            if got is not None:
                return got
    return None


def _function_def(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _upgrade_steps(tree: ast.Module) -> list[dict[str, str]]:
    """`upgrade()` 里"造出对象"的语句，按**源码顺序**列出。

    每项形如 `{"kind", "table", "name"}`，`kind ∈ {table, column, index, constraint}`：

    | kind | 来源 op | `table` | `name` |
    |---|---|---|---|
    | `table` | `op.create_table` | `""` | 表名 |
    | `column` | `op.add_column` | 表名 | 列名 |
    | `index` | `op.create_index` | 表名 | 索引名 |
    | `constraint` | `op.create_*_constraint` / `create_foreign_key` / `create_primary_key` | 表名 | 约束名 |

    ## 为什么必须有这个函数（而不是继续只数 `create_table`）

    2026-10-09 实测：head（`c4d9f2a17e35`）与 head-1（`b7c1e4a9d203`）**都是纯加列型**
    迁移（`add_column` / `create_index` / `create_check_constraint`），一张表都不建。
    而旧版 `injection_for()` 只认"建表"与"drop check"两种手段 ⇒ 两个版本**双双
    推导不出**注入方案 ⇒ 幕 5 的 6 条判据退化成"无法构造"、幕 6 的 2 条失去对照。
    那不是"这个 head 不需要演练"，而是**判据的能力被 head 的形态过拟合了**。

    ## 为什么按 `fn.body` 顶层语句走（不能用 `ast.walk(fn)`）

    `ast.walk` 是**广度优先**，对"嵌套在 if/for 里的语句"不保证源码序。
    本判据要取"最后一个对象"当障碍（让失败点尽可能靠后、前面的对象都已成功造出），
    顺序错了就取不到最后一个，观测不到"部分 DDL 回滚"（会退化成空过）。
    """
    fn = _function_def(tree, "upgrade")
    if fn is None:
        return []
    consts = _module_string_consts(tree)
    out: list[dict[str, str]] = []
    for stmt in fn.body:  # 顶层语句：源码序
        for node in ast.walk(stmt):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            attr = node.func.attr
            if attr not in _OBJECT_OPS:
                continue
            kind, tbl_idx, name_idx = _OBJECT_OPS[attr]
            if len(node.args) <= tbl_idx:
                continue
            # ⚠️ `create_table` 的第一个实参就是表名（不是"表 + 别的"）；
            #    其余 op 用 `tbl_idx` 指的那一位。
            table = _resolve_str(node.args[tbl_idx], consts) or ""
            if not table:
                continue
            if kind == "table":
                out.append({"kind": "table", "table": "", "name": table})
                continue
            if name_idx is None:
                # `add_column`：列名在 `sa.Column(...)` 里
                if kind == "column":
                    col = _column_name(node, consts)
                    if col:
                        out.append({"kind": "column", "table": table, "name": col})
                continue
            if len(node.args) <= name_idx:
                continue
            name = _resolve_str(node.args[name_idx], consts)
            if name:
                out.append({"kind": kind, "table": table, "name": name})
    return out


def _column_name(add_column_call: ast.Call, consts: dict[str, ast.expr]) -> str | None:
    """从 `op.add_column("t", sa.Column("c", ...))` 里取列名。

    列名由 `sa.Column(...)` / `postgresql.UUID(...)` 等的**第一个字符串实参**给出。
    找不到 ⇒ 返回 `None` ⇒ 调用方跳过这一步（宁少一个对象，不猜一个错名字）。
    """
    for sub in ast.walk(add_column_call):
        if not isinstance(sub, ast.Call) or not isinstance(sub.func, ast.Attribute):
            continue
        if sub.func.attr != "Column" or not sub.args:
            continue
        got = _resolve_str(sub.args[0], consts)
        if got:
            return got
    return None


def _ident(step: dict[str, str]) -> str:
    """对象的可读标识：表用表名，其余用 `表.名`。"""
    if step["kind"] == "table":
        return step["name"]
    return f"{step['table']}.{step['name']}"


def _created_tables(tree: ast.Module) -> list[str]:
    """`upgrade()` 建的表名（源码序）。

    ⚠️ 只保留这一个薄封装，**遍历逻辑只在 `_upgrade_steps()` 里写一遍**
    （本仓纪律：口径不得两侧各写一份）。新增能力一律加到 `_upgrade_steps()`。
    """
    return [s["name"] for s in _upgrade_steps(tree) if s["kind"] == "table"]


def _dropped_check(tree: ast.Module) -> tuple[str, str] | None:
    """`upgrade()` 里第一条 `op.drop_constraint(<name>, <table>, type_="check")`。

    返回 `(约束名, 表名)`；找不到返回 `None`。

    这是 **tier-2 兜底**：对"只 drop 一条 CHECK 再重建"的迁移（如
    `e8f07ba8f2bb` 那种改取值域的），预删它要 drop 的 CHECK 让它**在
    '对象不存在'上失败**。`_upgrade_steps()` 看不见 `drop_*` 这类语句，
    所以这一路必须独立存在。
    """
    fn = _function_def(tree, "upgrade")
    if fn is None:
        return None
    consts = _module_string_consts(tree)
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "drop_constraint" or len(node.args) < 2:
            continue
        is_check = any(
            kw.arg == "type_"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value == "check"
            for kw in node.keywords
        )
        if not is_check:
            continue
        conname = _resolve_str(node.args[0], consts)
        table = _resolve_str(node.args[1], consts)
        if conname and table:
            return conname, table
    return None


def _drop_target(node: ast.Call, consts: dict[str, ast.expr]) -> tuple[str, str] | None:
    """一条 `op.drop_*` 的目标，返回 `(表, 名)`；取不到返回 `None`。

    表名对 `drop_table` 是 `""`（与 `_upgrade_steps` 的 `table` kind 同口径）。
    `drop_index` 的表名常以 `table_name=` 关键字给出，故关键字也认。
    """
    attr = node.func.attr
    args = node.args
    if attr == "drop_table":
        if not args:
            return None
        got = _resolve_str(args[0], consts)
        return ("", got) if got else None
    if attr == "drop_column":
        if len(args) < 2:
            return None
        tbl = _resolve_str(args[0], consts)
        col = _resolve_str(args[1], consts)
        return (tbl, col) if tbl and col else None
    if attr in ("drop_index", "drop_constraint"):
        if not args:
            return None
        name = _resolve_str(args[0], consts)
        tbl = _resolve_str(args[1], consts) if len(args) >= 2 else None
        if tbl is None:
            for kw in node.keywords:
                if kw.arg in ("table_name", "table"):
                    tbl = _resolve_str(kw.value, consts)
                    break
        return (tbl or "", name) if name else None
    return None


def _dropped_identifiers(tree: ast.Module) -> set[tuple[str, str]]:
    """`upgrade()` 里被 `op.drop_*` 掉的对象，按 `(表, 名)` 收集。

    ## 为什么 tier-1 的桩必须**先减去**这一集合（2026-10-09 干净检出实测）

    tier-1 的手段是"**预建**它要造的对象，让 `create_*` 撞 `DuplicateObject`"。
    这要求那个对象**在迁移前不存在**。而有一类迁移是**重建同名对象**（先 drop
    旧定义、再 create 新定义）—— 典型如 `e8f07ba8f2bb`：它把
    `ck_prompt_templates_task_kind_valid` 的取值域换成含 `STATIC_IMAGE` 的新集合。
    对这类迁移：

    - 它 `create_*` 的那个名字，**迁移前本来就存在**；
    - 于是"预建同名桩"会**撞上已存在的那一条**，在**注入阶段**就
      `DuplicateObject` 崩掉 —— 判据不是"某条断言红了"，而是整支脚本异常退出。

    ⚠️ **这个缺陷只在干净检出上现形**（铁律 18 的形态）：干净检出的链头是
    `df6bb2f41f84`、head-1 恰好是 `e8f07ba8f2bb`；而当时共享工作树的链头是两个
    **纯加列型**迁移 ⇒ tier-1 挑中的都是有真正新名字的对象，撞不上。
    **同一份判据在两棵树上一绿一红，而绿的那棵恰好不是 CI 口径。**

    减去这一集合的语义是：**"被本迁移 drop 掉的名字"不算"本迁移新引入的对象"**，
    tier-1 不该拿它做桩；该迁移应回落 tier-2（`drop_owned_dependency`）——
    而那条路径本来就是为"重建型"迁移准备的。**修复后，干净检出上 tier-2 由此
    变为可达**（此前它只在共享树的注入方案下不可达）。
    """
    fn = _function_def(tree, "upgrade")
    if fn is None:
        return set()
    consts = _module_string_consts(tree)
    out: set[tuple[str, str]] = set()
    for stmt in fn.body:
        for node in ast.walk(stmt):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _DROP_OPS:
                continue
            target = _drop_target(node, consts)
            if target is not None:
                out.add(target)
    return out


def _pick_obstacle(steps: list[dict[str, str]]) -> int | None:
    """挑一个"最靠后、且能构造出障碍"的 step 下标；挑不到返回 `None`。

    从后往前挑（越靠后 ⇒ 失败点越靠后 ⇒ 障碍之前已成功造出的对象越多 ⇒
    "部分 DDL 被整体回滚"这一格观测得越充分）。

    ## 为什么**每个** kind 都可注入（不需要在这一层做可用性筛选）

    | kind | 注入手法 | 为什么必然冲突 |
    |---|---|---|
    | `table` | 预建同名表 | `create_table` 撞 `42P07 relation already exists` |
    | `column` | 同表预建同名列（`integer`） | `add_column` 撞 `42701 column already exists` |
    | `index` | 在 `alembic_version` 上预建同名索引 | PG 索引名是**schema 级全局** ⇒ 撞 `42P10` |
    | `constraint` | 同表预建同名 `check (true)` | PG 约束名**表内唯一** ⇒ 撞 `42P07`/`42710` |

    四类都只需一个名字，**名字都已经从 AST 拿到了** ⇒ 不存在"推导不出"的 kind。
    ⇒ 返回 `None` 的唯一可能是 `steps` 为空（该迁移不造任何对象）。
    """
    return len(steps) - 1 if steps else None


def injection_for(rev: str, rev_path: dict[str, Path]) -> dict[str, object] | None:
    """为某个迁移构造"让它失败"的注入方案（优先让失败点尽可能靠后）。

    三档手段，按"能造成的部分完成量"排序：

      ① **预建它最后一个要造的对象**（`pre_create_table` / `pre_create_column` /
         `pre_create_index` / `pre_create_constraint`）—— 前面的对象都已成功造出，
         失败发生时库里已有半成品，正好用来验证它们是否被整体回滚。
         这一档覆盖了**加列/加索引/加约束型**迁移（2026-10-09 新增，此前是盲区）。
      ② `drop_owned_dependency` —— 删掉它 `upgrade()` 里要 DROP 的 CHECK
         ⇒ 它的 `drop_constraint` 会在"对象不存在"上失败。
         （对"只改约束、不建表、也不加列"的迁移，这是唯一可用的手段。）

    都推导不出 ⇒ 返回 `None` ⇒ 调用方必须报 FAIL 而不是跳过（ISS-143）。
    """
    path = rev_path.get(rev)
    if path is None:
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"))

    steps = _upgrade_steps(tree)
    # ⚠️ 先剔除"被本迁移 drop 掉的名字"再挑障碍：`create_*` 撞 `DuplicateObject`
    #    这个手法要求对象**迁移前不存在**；而**重建同名对象**的迁移（先 drop
    #    再 create）违反了这一前提 —— 桩会撞上迁移前旧定义，在**注入阶段**崩。
    #    详见 `_dropped_identifiers()` 的论证（2026-10-09 干净检出实测）。
    #
    # 口径：`(table, name)` 二元组；`table` kind 的表名放在 `name` 里
    # （与 `_dropped_identifiers()` 对 `drop_table` 返回 `("", 表名)` 对齐）。
    dropped = _dropped_identifiers(tree)
    fresh = [
        s for s in steps
        if (s["table"], s["name"]) not in dropped
    ]
    idx = _pick_obstacle(fresh)
    if idx is not None:
        obstacle = fresh[idx]
        return {
            "kind": f"pre_create_{obstacle['kind']}",
            "obstacle": obstacle,
            "obstacle_index": idx,
            "steps": fresh,
            "ident": _ident(obstacle),
            "table": obstacle["table"] or obstacle["name"],
            "name": obstacle["name"],
            "objects": [_ident(s) for s in fresh],
            "prior_objects": [_ident(s) for s in fresh[:idx]],
            "migration": path.name,
        }

    dropped_check = _dropped_check(tree)
    if dropped_check:
        return {"kind": "drop_owned_dependency", "constraint": dropped_check[0],
                "table": dropped_check[1], "ident": f"{dropped_check[1]}.{dropped_check[0]}",
                "name": dropped_check[0], "obstacle_index": None, "steps": [],
                "objects": [], "prior_objects": [],
                "migration": path.name}
    return None


def _prior_steps(inj: dict[str, object]) -> list[dict[str, str]]:
    """障碍**之前**（源码序）造出的对象。

    它们在障碍处失败前已成功造出 ⇒ 若"整体回滚"生效，这些对象应当**全部不存在**。
    障碍本身**不在**其中 —— 它是桩，本来就该在。
    """
    idx = inj.get("obstacle_index")
    steps = inj.get("steps") or []
    if not isinstance(idx, int) or not isinstance(steps, list):
        return []
    return [s for s in steps[:idx] if isinstance(s, dict)]


def _obstacle_step(inj: dict[str, object]) -> dict[str, str] | None:
    """障碍对象的 `{"kind", "table", "name"}` —— 两种手段统一口径。

    * `pre_create_*` ⇒ `inj["obstacle"]` 直接就是那个 step；
    * `drop_owned_dependency` ⇒ 该手段没有 `obstacle` 键（`obstacle_index=None`、
      `steps=[]`），障碍是**迁移自己要 DROP 的那条 CHECK**，从
      `table`/`name` 重建。

    为什么值得单独一个函数：`_object_exists()` 与 `_obstacle_intact()` 都要用
    障碍的 step 形态，两处各写一遍取法就会出现"某一处取错键 ⇒ 恒 False 的判据"
    （本仓纪律：口径不得两侧各写一份）。
    """
    obst = inj.get("obstacle")
    if isinstance(obst, dict) and "kind" in obst:
        return obst  # type: ignore[return-value]
    if str(inj.get("kind")) == "drop_owned_dependency":
        table = str(inj.get("table") or "")
        name = str(inj.get("name") or inj.get("constraint") or "")
        if table and name:
            return {"kind": "constraint", "table": table, "name": name}
    return None


def _check_defs(tree: ast.Module, fnname: str) -> dict[str, tuple[str, str]]:
    """某函数（`upgrade`/`downgrade`）里 `op.create_check_constraint(...)` 的
    `{约束名: (表名, SQL 文本)}`。按 `fn.body` 顶层语句走（源码序）。"""
    fn = _function_def(tree, fnname)
    if fn is None:
        return {}
    consts = _module_string_consts(tree)
    out: dict[str, tuple[str, str]] = {}
    for stmt in fn.body:
        for node in ast.walk(stmt):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "create_check_constraint" or len(node.args) < 3:
                continue
            name = _resolve_str(node.args[0], consts)
            table = _resolve_str(node.args[1], consts)
            sql = _resolve_str(node.args[2], consts)
            if name and table and sql:
                out[name] = (table, sql)
    return out


def _narrowing_revisions(
    order: list[str], rev_path: dict[str, Path]
) -> list[tuple[int, str, dict[str, dict[str, object]]]]:
    """链上"downgrade 会收窄某条 CHECK 取值域"的迁移，按**距 head 由近及远**排序。

    返回 `[(链上下标, revision, {约束名: {"table": 表, "values": [被收窄掉的值]}})]`。

    ## 为什么这必须由 AST 推导（铁律 14）

    "哪个迁移会收窄取值域"是迁移文件的**静态属性**：`downgrade()` 里同名
    `create_check_constraint` 的 SQL 字面量比 `upgrade()` 的**少**。
    写死一个版本号 ⇒ 链一变（本仓天天在加迁移）这条能力就悄悄失联：
    幕 4 退回成"无法构造"并报 5 条 FAIL，而**没人知道该去改哪里**。

    ⚠️ 2026-10-09 实测：本仓链上仅有 `e8f07ba8f2bb`（idx3）命中 ——
    它把 `prompt_templates.task_kind` 的取值域从 10 值写回 9 值。
    head 连着两条都是"加列型"，故幕 4 **必须导航到那条**才能演练。

    ⚠️ 本函数**不解析列名**：迁移源码里的 SQL 是 `col IN ('A', ...)` 形态，
    与 `pg_get_constraintdef` 的渲染形态（`((col)::text = ANY (...))`）不同，
    同一个正则套两种形态必然有一种恒不命中。列名由调用方在**导航到位后**
    从 `_check_literals()` 的渲染文本里取（那是 PG 的权威口径）。
    """
    out: list[tuple[int, str, dict[str, dict[str, object]]]] = []
    for i, rev in enumerate(order):
        path = rev_path.get(rev)
        if path is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        up = _check_defs(tree, "upgrade")
        dn = _check_defs(tree, "downgrade")
        narrowed: dict[str, dict[str, object]] = {}
        for name, (table, dsql) in dn.items():
            if name not in up:
                continue
            _u_table, u_sql = up[name]
            if not table or not dsql or not u_sql:
                continue
            d_lits = frozenset(_SQL_LITERAL_RE.findall(dsql))
            u_lits = frozenset(_SQL_LITERAL_RE.findall(u_sql))
            if not d_lits or not u_lits or not d_lits < u_lits:
                continue
            lost = sorted(u_lits - d_lits)
            if not lost:
                continue
            narrowed[name] = {"table": table, "values": lost}
        if narrowed:
            out.append((i, rev, narrowed))
    return out



# ===========================================================================
# 数据库侧的小工具
# ===========================================================================


def _fingerprint(db: str) -> dict:
    """复用备份演练的六类指纹实现（同一套算法，避免两处口径分叉）。"""
    import psycopg

    with psycopg.connect(drill_db.dsn(db)) as conn:
        with conn.cursor() as cur:
            return backup_pg.compute_fingerprint(cur)


def _fp_diff(a: dict, b: dict) -> list[str]:
    """逐类比较两份指纹，返回差异描述（空列表 = 全等）。"""
    out: list[str] = []
    for k in backup_pg.FINGERPRINT_CLASSES:
        if a[k]["objects_sha256"] != b[k]["objects_sha256"]:  # type: ignore[index]
            out.append(f"{k}(身份)")
        elif a[k].get("defs_sha256") != b[k].get("defs_sha256"):  # type: ignore[index]
            out.append(f"{k}(定义)")
    return out


def _version(db: str) -> str:
    import psycopg

    with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
        rows = conn.execute("select version_num from alembic_version").fetchall()
    return rows[0][0] if rows else "<none>"


def _table_exists(db: str, name: str) -> bool:
    """表是否存在。

    ⚠️ **必须判"值是否为 NULL"，不能判"结果列表是否非空"**：
    `select to_regclass(...)` 对不存在的表返回**一行 NULL**，
    `bool([(None,)])` 恒为 `True` ⇒ 存在性判据恒真。
    本判据的开发期探针正是这么写错的（2026-10-07 实测），已在
    `tests/test_rollback_drill.py` 里钉了一条阳性/阴性对照。
    """
    import psycopg

    with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
        row = conn.execute(
            "select to_regclass(%s)::text", (f"public.{name}",)
        ).fetchone()
    return bool(row and row[0] is not None)


def _column_count(db: str, name: str) -> int:
    import psycopg

    with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
        return int(conn.execute(
            "select count(*) from information_schema.columns"
            " where table_schema='public' and table_name=%s", (name,)
        ).fetchone()[0])


def _check_literals(db: str) -> dict[tuple[str, str], tuple[str, frozenset[str]]]:
    """public 下所有 CHECK 约束：`(表, 约束名) → (定义文本, 字符串字面量集合)`。

    覆盖范围由 `pg_constraint` 决定（铁律 14）。
    """
    import psycopg

    with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
        rows = conn.execute(
            """
            select c.conrelid::regclass::text, c.conname,
                   coalesce(pg_get_constraintdef(c.oid), '')
              from pg_constraint c
              join pg_class t on t.oid = c.conrelid
              join pg_namespace n on n.oid = t.relnamespace
             where c.contype = 'c' and n.nspname = 'public'
             order by 2
            """
        ).fetchall()
    out: dict[tuple[str, str], tuple[str, frozenset[str]]] = {}
    for table, name, ddl in rows:
        simple = table.split(".", 1)[-1]
        lits = frozenset(_LITERAL_RE.findall(ddl))
        out[(simple, name)] = (ddl, lits)
    return out


def _apply_injection(db: str, inj: dict[str, object]) -> str:
    """按 `injection_for()` 的方案在库里制造失败条件。返回桩对象标识（无桩则 `""`）。

    三种场景（幕 5① / 幕 5② / 幕 6 沙箱臂）共用同一份实现 ——
    复制三遍的代价是"某一处改了另两处没改"，而那种不一致**不会报错**，
    只会让某一幕悄悄测不到东西。

    ## 为什么 `pre_create_index` 的桩索引要挂在 `alembic_version` 上

    PG 的**索引名是 schema 级全局**（不像约束名只在表内唯一）。
    所以要让 `op.create_index("uq_x", "media_items", ...)` 撞名字，
    只需在**任意**表上造一个叫 `uq_x` 的索引 ——
    `alembic_version` 必然存在（任何跑过 alembic 的库都有）且只有一列，
    是这里最稳的落点。挂到被测迁移自己的表上反而可能因表还没建而失败
    （那会让"注入"与"迁移本身失败"同形 —— 而本条要的是**注入**导致的失败）。
    """
    import psycopg

    kind = str(inj["kind"])
    if kind == "pre_create_table":
        stub = str(inj.get("name") or inj.get("ident") or inj["table"])
        with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
            conn.execute(f'create table public."{stub}" (id integer)')
        return stub
    if kind == "pre_create_column":
        table, col = str(inj["table"]), str(inj["name"])
        with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
            conn.execute(f'alter table public."{table}" add column "{col}" integer')
        return f"{table}.{col}"
    if kind == "pre_create_index":
        idx = str(inj["name"])
        with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
            conn.execute(f'create index "{idx}" on public.alembic_version (version_num)')
        return idx
    if kind == "pre_create_constraint":
        table, name = str(inj["table"]), str(inj["name"])
        with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
            conn.execute(
                f'alter table public."{table}" add constraint "{name}" check (true)'
            )
        return f"{table}.{name}"
    # `drop_owned_dependency`：删掉它 upgrade() 里要 DROP 的对象 ⇒ 它在
    # "对象不存在"上失败。⚠️ **显式写 kind 判断**而不是 `else` 兜底：
    # 兜底形态下"声明了一种 kind 却没实现"会**静默走到这里**（注入发生的是
    # 另一种事），而 `tests/test_rollback_drill.py` 的"声明 ⟷ 实现"对账
    # 也会因此查不到这个名字 ⇒ 缺口不可见。
    if kind == "drop_owned_dependency":
        with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
            conn.execute(
                f'alter table public."{inj["table"]}" '
                f'drop constraint "{inj["constraint"]}"'
            )
        return ""
    raise ValueError(
        f"未知的注入手段 kind={kind!r}（声明集合 {sorted(INJECTION_KINDS)}）"
        " —— 新增手段必须同时实现这里与 `_clear_injection()`"
    )


def _clear_injection(db: str, inj: dict[str, object]) -> None:
    """把 `_apply_injection()` 造的桩移除（幂等）。

    ⚠️ 与 `_apply_injection` **必须成对**：幕 5① 用了注入①，幕 5② 要
    在同一库上继续升级 —— 桩不清掉，幕 5② 的观测会被注入① 的残骸污染。
    （旧版只 `drop table`，故只有 `pre_create_table` 是可清理的；
    泛化后四种桩都得清，否则新手段一旦启用，幕 5② 会莫名报红。）
    """
    import psycopg

    kind = str(inj["kind"])
    try:
        with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
            if kind == "pre_create_table":
                conn.execute(f'drop table if exists public."{inj.get("name")}" cascade')
            elif kind == "pre_create_column":
                conn.execute(
                    f'alter table public."{inj["table"]}"'
                    f' drop column if exists "{inj["name"]}" cascade'
                )
            elif kind == "pre_create_index":
                conn.execute(f'drop index if exists public."{inj["name"]}"')
            elif kind == "pre_create_constraint":
                conn.execute(
                    f'alter table public."{inj["table"]}"'
                    f' drop constraint if exists "{inj["name"]}"'
                )
            elif kind == "drop_owned_dependency":
                # 无桩可清：它删的是**迁移自己的**对象（该由后续 upgrade 重建），
                # 清不掉也不该清。显式列出而不是靠 `elif` 落空 —— 与
                # `_apply_injection()` 同一条纪律：声明与实现必须逐 kind 对应。
                pass
    except Exception:  # noqa: BLE001
        pass


def _object_exists(db: str, step: dict[str, str]) -> bool:
    """按 kind 判该对象是否存在于库里（kind ∈ table/column/index/constraint）。

    覆盖范围由系统表决定（铁律 14）—— 不写死任何名字。
    """
    import psycopg

    kind, table, name = step["kind"], step["table"], step["name"]
    if kind == "table":
        return _table_exists(db, name)
    with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
        if kind == "column":
            row = conn.execute(
                "select 1 from information_schema.columns"
                " where table_schema='public' and table_name=%s and column_name=%s",
                (table, name),
            ).fetchone()
            return row is not None
        if kind == "index":
            row = conn.execute(
                "select 1 from pg_indexes"
                " where schemaname='public' and indexname=%s",
                (name,),
            ).fetchone()
            return row is not None
        row = conn.execute(
            "select 1 from pg_constraint c join pg_class t on t.oid=c.conrelid"
            " join pg_namespace n on n.oid=t.relnamespace"
            " where n.nspname='public' and t.relname=%s and c.conname=%s",
            (table, name),
        ).fetchone()
        return row is not None


def _obstacle_intact(db: str, inj: dict[str, object]) -> tuple[bool, str]:
    """**注入的效果**在失败之后是否仍然成立。

    统一口径（2026-10-09 定）：问的不是"障碍长什么样"，而是
    **"我制造的那个失败条件还在不在"** ——

    | kind | 注入做的事 | "效果成立"= |
    |---|---|---|
    | `pre_create_table` | 预建同名桩表 `(id integer)` | 该表仍在且列数 == 1 |
    | `pre_create_column` | 同表预建同名列 `integer` | 该列仍在且类型是 `integer` |
    | `pre_create_index` | 在 `alembic_version` 上预建同名索引 | 该索引仍在且仍挂那张表 |
    | `pre_create_constraint` | 同表预建同名 `check (true)` | 该约束仍在且 def 是 `CHECK (true)` |
    | `drop_owned_dependency` | **删掉**它 `upgrade()` 要 DROP 的 CHECK | 该 CHECK **仍然不在** |

    ⚠️ 旧版把 `drop_owned_dependency` 写成"无桩 ⇒ 恒 `True`"。那是**两种**恒真：
    既让该分支自身恒定，又让整个谓词**只有一个使用点（必须为真）** ⇒
    结构性恒真（反向验证 M5 实测：把它改成 `return True` 后 24/24 全绿）。
    现在四种 kind 都返回**依状态变化**的值，故该谓词**必须翻转**才能绿 ——
    见幕 5① 的 `before_intact` / `after_intact` 配对。

    返回 `(效果是否仍成立, 人读描述)`。
    """
    import psycopg

    kind = str(inj["kind"])
    if kind == "drop_owned_dependency":
        step = _obstacle_step(inj)
        if step is None:
            return False, "⚠️ 无法从该注入方案重建障碍对象 ⇒ 本条无法自证"
        gone = not _object_exists(db, step)
        return gone, (
            f"被我删掉的 {_ident(step)} "
            + ("仍不在（注入仍生效）" if gone else "又被建回来了（注入被回滚抹掉）")
        )
    if kind == "pre_create_table":
        name = str(inj.get("name"))
        n = _column_count(db, name)
        return _table_exists(db, name) and n == 1, (
            f"障碍表 {name} 仍在且列数={n}（1 ⇒ 仍是我建的桩表，不是该迁移的成品表）"
        )
    if kind == "pre_create_column":
        table, col = str(inj["table"]), str(inj["name"])
        with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
            row = conn.execute(
                "select data_type from information_schema.columns"
                " where table_schema='public' and table_name=%s and column_name=%s",
                (table, col),
            ).fetchone()
        dt = row[0] if row else None
        return dt == "integer", (
            f"障碍列 {table}.{col} 类型={dt!r}（'integer' ⇒ 仍是我建的桩列）"
        )
    if kind == "pre_create_index":
        name = str(inj["name"])
        with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
            row = conn.execute(
                "select t.relname from pg_index i join pg_class c on c.oid=i.indexrelid"
                " join pg_class t on t.oid=i.indrelid join pg_namespace n on n.oid=c.relnamespace"
                " where n.nspname='public' and c.relname=%s",
                (name,),
            ).fetchone()
        owner = row[0] if row else None
        return owner == "alembic_version", (
            f"障碍索引 {name} 建在表 {owner!r} 上（'alembic_version' ⇒ 仍是我建的桩索引）"
        )
    table, name = str(inj["table"]), str(inj["name"])
    with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
        row = conn.execute(
            "select pg_get_constraintdef(c.oid) from pg_constraint c"
            " join pg_class t on t.oid=c.conrelid join pg_namespace n on n.oid=t.relnamespace"
            " where n.nspname='public' and t.relname=%s and c.conname=%s",
            (table, name),
        ).fetchone()
    ddl = row[0] if row else None
    return ddl == "CHECK (true)", (
        f"障碍约束 {table}.{name} 定义={ddl!r}（'CHECK (true)' ⇒ 仍是我建的桩约束）"
    )


def _navigate_to(dbname: str, order: list[str], target: str) -> str:
    """把库的 `alembic_version` 导航到 `target`。返回 `""` = 成功，否则为失败描述。

    ## 为什么必须**逐步**（不能 `downgrade <rev>` 直接跳）

    Alembic 的 `downgrade <rev>` 会**直接跳**到目标版本，中途那些迁移的
    `downgrade()` **一次都不跑** ⇒ 库会停在一个"版本号对、结构不对"的
    矛盾状态。本判据要的恰恰是"真的逐条回滚过"，故必须 `-1` 一步一步走。

    ## 为什么必须**双向**（2026-10-09 实测踩到）

    幕 4 会把库降到一个**比 `prev2_rev` 更旧**的版本上（它要导航到链上
    最近的"收窄取值域"迁移，那条在 head 下方好几步）。幕 5① 接着要把库
    复位到 `prev2_rev` —— 用"只降不升"的循环会一路降到 base，
    再在"表都不存在"的库上注入 ⇒ `UndefinedTable`（实测报错原文：
    `relation "public.media_items" does not exist`）⇒ **崩溃**，
    与"跑失败"同形（ISS-43）。

    方向由链上下标比较决定：目标在当前位置**上面**就 `upgrade <rev>`，
    否则 `downgrade -1`。`upgrade <rev>` 同样是逐步执行（alembic 语义）。
    """
    guard = 0
    limit = 2 * len(order) + 4
    while _version(dbname) != target:
        guard += 1
        if guard > limit:
            return f"导航未收敛（版本={_version(dbname)}，目标 {target}）"
        cur = _version(dbname)
        if cur not in order or target not in order:
            return f"版本 {cur!r} 或目标 {target!r} 不在链上"
        if order.index(target) < order.index(cur):
            # 目标更靠 head（下标更小）⇒ 往上走
            code, tail = drill_db.migrate(dbname, "upgrade", target)
        else:
            code, tail = drill_db.migrate(dbname, "downgrade", "-1")
        if code != 0:
            return tail
    return ""


def _dummy_sql(data_type: str, udt_name: str) -> str | None:
    """给"NOT NULL 且无默认值"的列造一个占位值。不认识的类型返回 None（报红）。"""
    if udt_name == "uuid":
        return "gen_random_uuid()"
    if data_type in ("character varying", "text", "character", "name"):
        return "'drill'"
    if data_type in ("integer", "bigint", "smallint", "numeric", "double precision",
                     "real", "money"):
        return "0"
    if data_type == "boolean":
        return "false"
    if data_type.startswith("timestamp") or data_type == "date":
        return "now()"
    if udt_name == "jsonb":
        return "'{}'::jsonb"
    if udt_name == "json":
        return "'{}'::json"
    if udt_name in ("_text", "text[]"):
        return "'{}'::text[]"
    return None


def _notnull_without_default(db: str, table: str) -> list[tuple[str, str, str]]:
    """`[(列名, data_type, udt_name)]`：NOT NULL 且无默认值、非生成列。"""
    import psycopg

    with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
        rows = conn.execute(
            """
            select column_name, data_type, udt_name
              from information_schema.columns
             where table_schema = 'public' and table_name = %s
               and is_nullable = 'NO' and column_default is null
               and coalesce(is_generated, 'NEVER') = 'NEVER'
               and is_identity = 'NO'
             order by ordinal_position
            """,
            (table,),
        ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


# ===========================================================================
# 幕 6 的沙箱：在**不动仓库文件**的前提下翻转 `transaction_per_migration`
# ===========================================================================
#
# 为什么必须真跑一遍，而不是在注释里写"翻转就会红"：
#   判据纪律第 3 条 / ISS-95 —— 「声称自己有防线」比「没有防线」更危险。
#   `granularity:*` 那两条断言的是"末条迁移失败时版本推进到上一条已提交的迁移"，
#   这句话**只在 `transaction_per_migration=True` 时成立**。不真跑一次 False，
#   我就无法指出脚本里**哪个函数**在保证这个前提，那句话就只是文案。
#
# 为什么能不碰仓库文件：`migrations/env.py` 用 `Path(__file__).resolve().parents[1]`
#   自定位仓库根，`alembic.ini` 用 `%(here)s` 自定位脚本目录 ⇒ 整棵 `migrations/`
#   复制到临时目录后仍是**自洽**的，翻转副本里的那一行即可独立运行。
#   反向依赖（`from rednote.modules... import`）由 `PYTHONPATH` 指向真 `services/backend/src`。


def _repo_root_of_env_py() -> Path:
    """`env.py` 认为的仓库根（用于把它需要的 `services/backend/src` 挂上 PYTHONPATH）。"""
    return ENV_PY.parents[1]


def _stage_migrations(sandbox: Path, flip: bool) -> tuple[Path, Path]:
    """把 `migrations/` 复制进沙箱并（可选）翻转那一行。返回 `(ini 路径, env.py 路径)`。

    ⚠️ 翻转前**先断言那一行唯一存在**：找不到 / 找到多处 ⇒ 抛错。
    退化成"没翻到就照原样跑"会让两臂变成同一个臂 ⇒ 幕 6 变成恒真式。
    """
    staged = sandbox / "migrations"
    shutil.copytree(
        MIGRATIONS_DIR.parent, staged,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    env_py = staged / "env.py"
    src = env_py.read_text(encoding="utf-8")
    if src.count(_TPM_LITERAL) != 1:
        raise RuntimeError(
            f"沙箱 env.py 里 `{_TPM_LITERAL}` 出现 {src.count(_TPM_LITERAL)} 次"
            "（应为 1）—— 翻转无从下手，幕 6 无法构造"
        )
    if flip:
        env_py.write_text(src.replace(_TPM_LITERAL, _TPM_FLIPPED), encoding="utf-8")
    return staged / "alembic.ini", env_py


def _alembic_in_sandbox(ini: Path, db: str, *args: str) -> tuple[int, str]:
    """用**沙箱里的** ini 跑 alembic（DSN 方言与 `drill_db` 同源）。"""
    env = dict(os.environ)
    env["REDNOTE_DATABASE_URL"] = drill_db.dsn_alembic(db)
    env["NO_PROXY"] = "*"
    env["PYTHONPATH"] = str(_repo_root_of_env_py() / "services" / "backend" / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ini), *args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
    )
    return proc.returncode, (proc.stdout + proc.stderr)[-1200:]


# ===========================================================================
# 主流程
# ===========================================================================


def main() -> int:
    checks: list[tuple[str, bool, str]] = []
    notes: list[str] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    # ---------------- 静态 3 条 ----------------
    down_of, order, rev_path = parse_chain()
    add("static:single-linear-head", len(order) >= 1 and len(order) == len(down_of),
        f"迁移 {len(down_of)} 个，从 head 走出的链长 {len(order)}"
        + ("" if len(order) == len(down_of) else " ⇒ 有分叉或多 head，"
           "`downgrade -1` 的目标不确定，后续动态判据的语义会含糊"),
    )
    add("static:chain-long-enough", len(order) >= MIN_REVISIONS,
        f"链长 {len(order)}（下界 {MIN_REVISIONS}，`downgrade -2` 需要 ≥3）")

    tpm = declares_transaction_per_migration()
    add("static:transaction-per-migration-declared", tpm,
        "migrations/env.py 声明 transaction_per_migration=True"
        if tpm else
        "未声明 ⇒ 幕 5 的'停在上一条已提交迁移'就没有前提，"
        "该语义会退化成'停在 run 起点'")

    if len(order) < MIN_REVISIONS or not tpm:
        return _report(checks, notes)

    head_rev, prev_rev, prev2_rev = order[0], order[1], order[2]

    inj_first = injection_for(prev_rev, rev_path)
    inj_last = injection_for(head_rev, rev_path)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    db = f"rednote_roll_{stamp}"
    notes.append(f"  库={db}  head={head_rev}  head-1={prev_rev}  head-2={prev2_rev}")
    notes.append(f"  注入①（首条迁移 {prev_rev}）：{inj_first}")
    notes.append(f"  注入②（末条迁移 {head_rev}）：{inj_last}")

    try:
        import psycopg

        # ---------------- 幕 1：建到 head ----------------
        drill_db.create_db(db)
        code, tail = drill_db.upgrade_head(db)
        add("build:upgrade-head", code == 0,
            "alembic upgrade head EXIT=0" if code == 0 else f"EXIT={code}：{tail}")
        if code != 0:
            return _report(checks, notes)

        fa = _fingerprint(db)
        ntables = int(fa["tables"]["count"])  # type: ignore[index]
        add("build:fingerprint-scope-sane", ntables >= MIN_TABLES,
            f"{ntables} 张表入指纹（下界 {MIN_TABLES}）"
            + ("" if ntables >= MIN_TABLES else " ⇒ 迁移没跑完，后续'指纹全等'会退化成空对空"))

        # ---------------- 幕 2：注入破坏，证明指纹灵敏 ----------------
        with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
            row = conn.execute(
                """
                select c.relname, pg_get_indexdef(i.indexrelid)
                  from pg_index i
                  join pg_class c on c.oid = i.indexrelid
                  join pg_namespace n on n.oid = c.relnamespace
                 where n.nspname = 'public' and c.relkind = 'i'
                   and not exists (select 1 from pg_constraint k
                                    where k.conindid = i.indexrelid)
                 order by 1 limit 1
                """
            ).fetchone()
        if row is None:
            add("reverse:fingerprint-detects-index-loss", False,
                "库里没有'不支撑约束'的独立索引 ⇒ 注入破坏无从下手，本条无法自证灵敏")
            add("reverse:index-restored-exactly", False, "上一条未能注入，本条无从验证")
        else:
            idx_name, idx_ddl = row[0], row[1]
            with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
                conn.execute(f'drop index public."{idx_name}"')
            mutated = _fingerprint(db)
            obj_changed = (mutated["indexes"]["objects_sha256"]
                           != fa["indexes"]["objects_sha256"])  # type: ignore[index]
            def_changed = (mutated["indexes"].get("defs_sha256")
                           != fa["indexes"].get("defs_sha256"))  # type: ignore[index]
            add("reverse:fingerprint-detects-index-loss", obj_changed and def_changed,
                f"DROP INDEX {idx_name} 后 身份{'变' if obj_changed else '未变'}"
                f"/定义{'变' if def_changed else '未变'}"
                f"（{'两层都变 ⇒ 判据灵敏' if obj_changed and def_changed else '有层未变 ⇒ 该层恒真'}）")
            with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
                conn.execute(idx_ddl)
            restored = _fingerprint(db)
            diff = _fp_diff(fa, restored)
            add("reverse:index-restored-exactly", not diff,
                f"按 pg_get_indexdef 原样重建 {idx_name} 后指纹"
                + ("复原" if not diff else f"仍差：{diff}"))

        # ---------------- 幕 3：单步回滚 + 复原 ----------------
        # ★ 2026-10-09 换谓词：旧版比"head 的 CHECK 字面量域缩小" ——
        #   那是**取值域型迁移**的特征。本仓 head 连着两条都是**加列型**
        #   （`add_column` + `create_index` + `create_check_constraint`），
        #   新 CHECK 是 `char_length(...) <= 500`，**一个字符串字面量都没有**
        #   ⇒ 旧谓词恒假 ⇒ 实测报红，且"回滚真的改了东西"这件事失去覆盖。
        #
        #   新谓词与迁移形态**无关**：head 的 `upgrade()` 造出的 N 个对象，
        #   在 head 态必须**全在**，`downgrade -1` 后必须**全不在**。
        #   对取值域型迁移它同样成立 ⇒ 更强、更通用。
        head_steps = _upgrade_steps(
            ast.parse(rev_path[head_rev].read_text(encoding="utf-8"))
        )
        before_present = [s for s in head_steps if _object_exists(db, s)]

        code, tail = drill_db.migrate(db, "downgrade", "-1")
        v_after = _version(db)
        add("rollback:single-step-rc0-and-version",
            code == 0 and v_after == prev_rev,
            f"downgrade -1 EXIT={code}，版本 {head_rev} → {v_after}"
            + ("" if v_after == prev_rev else f"（应落到 {prev_rev}）")
            + ("" if code == 0 else f"；{tail}"))

        after_present = [s for s in head_steps if _object_exists(db, s)]
        enough_head = len(before_present) >= MIN_HEAD_OBJECTS
        add("rollback:single-step-changes-object",
            enough_head and not after_present,
            f"head 的 upgrade() 造 {len(head_steps)} 个对象；"
            f"回滚前在库 {len(before_present)} 个，回滚后 {len(after_present)} 个"
            + ("" if enough_head else
               f" ⇒ 回滚前在库对象不足下界 {MIN_HEAD_OBJECTS}，**本条无法自证**（不是通过）")
            + ("" if not after_present else
               f"；回滚后仍有残留：{[_ident(s) for s in after_present]}"))

        code, tail = drill_db.migrate(db, "upgrade", "head")
        back = _fingerprint(db)
        diff = _fp_diff(fa, back)
        add("rollback:head-fingerprint-restored",
            code == 0 and not diff,
            (f"再 upgrade head EXIT={code}；" if code == 0 else f"EXIT={code}：{tail}；")
            + ("六类指纹全等" if not diff else f"仍差：{diff}"))

        # ★ 2026-10-09 换目标：旧版拿"head 回滚时被收窄的 CHECK"当靶子。
        #   本仓 head 是加列型、不缩取值域 ⇒ 靶子为空 ⇒ 5 条"无法构造"。
        #   但"数据阻断回滚"这项**能力**依然存在，只是落在链上**另一条**迁移上
        #   （`e8f07ba8f2bb`：把 `prompt_templates.task_kind` 从 10 值写回 9 值）。
        #   ⇒ 沿链找**最近的那条**，导航到它（10 值态）插探针行，再让它回滚。
        #   覆盖范围由 AST 决定（铁律 14），不写死版本号。
        narrowings = _narrowing_revisions(order, rev_path)
        notes.append(
            "  链上会收窄取值域的迁移："
            + (", ".join(f"{rev}(idx{i})" for i, rev, _ in narrowings) or "（无）")
        )
        drill_rev = ""        # 阻断演练的目标 revision（"被阻断的那一步"）
        below_rev = ""        # 它回滚后应落到的那条（`downgrade -1` 的目标）
        target: tuple[str, str, str, str] | None = None   # (表, 约束, 列, 取值)
        if narrowings:
            n_idx, drill_rev, narrowed = narrowings[0]
            below_rev = order[n_idx + 1] if n_idx + 1 < len(order) else ""
            for conname, meta in sorted(narrowed.items()):
                vals = sorted(meta["values"])
                if vals:
                    target = (str(meta["table"]), conname, "<resolved-at-nav>", vals[0])
                    break

        if target is None:
            for name in ("refuse:downgrade-blocked-by-incompatible-data",
                         "refuse:version-stays-at-drill-revision",
                         "refuse:row-not-silently-deleted",
                         "refuse:constraint-not-half-applied",
                         "refuse:succeeds-after-row-removed"):
                add(name, False,
                    "链上找不到'downgrade 会收窄 CHECK 取值域'的迁移 ⇒ **本条无法构造**。"
                    "不允许跳过（ISS-143）：要么整条链确实没有这种迁移（则本族应重写），"
                    "要么 `_narrowing_revisions()` 的推导逻辑要修。")
        else:
            table, conname, _placeholder, value = target

            # ---- 导航到 drill_rev（逐条回滚，见 `_navigate_to`）----
            nav_failed = _navigate_to(db, order, drill_rev)

            if nav_failed:
                for name in ("refuse:downgrade-blocked-by-incompatible-data",
                             "refuse:version-stays-at-drill-revision",
                             "refuse:row-not-silently-deleted",
                             "refuse:constraint-not-half-applied",
                             "refuse:succeeds-after-row-removed"):
                    add(name, False,
                        f"无法把库导航到 {drill_rev}（{nav_failed}）⇒ 阻断演练无法构造。")
            else:
                # 列名从 **PG 的渲染文本**取（权威口径），不从迁移源码猜：
                # 迁移里写的是 `col IN (...)`，`pg_get_constraintdef` 渲染成
                # `((col)::text = ANY (...))`，只有后者能被 `_COLUMN_RE` 命中。
                drill_checks = _check_literals(db)
                meta = narrowings[0][2][conname]
                ddl = drill_checks.get((table, conname), ("", frozenset()))[0]
                m = _COLUMN_RE.search(ddl)
                column = m.group(1) if m else ""
                if not column:
                    for name in ("refuse:downgrade-blocked-by-incompatible-data",
                                 "refuse:version-stays-at-drill-revision",
                                 "refuse:row-not-silently-deleted",
                                 "refuse:constraint-not-half-applied",
                                 "refuse:succeeds-after-row-removed"):
                        add(name, False,
                            f"在 {drill_rev} 态读不到约束 {conname} 的列名"
                            f"（渲染文本={ddl!r}）⇒ 探针行不知道往哪个列插")
                else:
                    notes.append(
                        f"  数据阻断目标：.{table}.{column} 取值 {value!r}"
                        f"（约束 {conname}；被阻断的一步 {drill_rev} → {below_rev}）"
                    )
                    cols: list[str] = [column]
                    vals: list[str] = ["'" + value.replace("'", "''") + "'"]
                    unsupported: list[str] = []
                    for cname, dtype, udt in _notnull_without_default(db, table):
                        if cname == column:
                            continue
                        expr = _dummy_sql(dtype, udt)
                        if expr is None:
                            unsupported.append(f"{cname}:{udt}")
                            continue
                        cols.append(cname)
                        vals.append(expr)
                    if unsupported:
                        for name in ("refuse:downgrade-blocked-by-incompatible-data",
                                     "refuse:version-stays-at-drill-revision",
                                     "refuse:row-not-silently-deleted",
                                     "refuse:constraint-not-half-applied",
                                     "refuse:succeeds-after-row-removed"):
                            add(name, False,
                                f"表 {table} 有无法造占位值的 NOT NULL 列：{unsupported}"
                                " ⇒ 无法插入探针行")
                    else:
                        col_sql = ", ".join(f'"{c}"' for c in cols)
                        # ⚠️ 只捕获 `psycopg.Error`（**宽到能覆盖"约束/类型不允许插这行"，
                        #    窄到不吞编程错误**）。构造失败必须报 FAIL 并说清原因 ——
                        #    让它冒成 traceback 等于 ISS-43：崩溃与"跑失败"同形。
                        probe_failed: str | None = None
                        try:
                            with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
                                conn.execute(
                                    f'insert into public."{table}" ({col_sql})'
                                    f' values ({", ".join(vals)})'
                                )
                        except psycopg.Error as exc:
                            probe_failed = (
                                f"{type(exc).__name__}: {exc}".replace("\n", " ")[:300]
                            )

                        if probe_failed is not None:
                            for name in ("refuse:downgrade-blocked-by-incompatible-data",
                                         "refuse:version-stays-at-drill-revision",
                                         "refuse:row-not-silently-deleted",
                                         "refuse:constraint-not-half-applied",
                                         "refuse:succeeds-after-row-removed"):
                                add(name, False,
                                    f"无法把探针行插进 {table}（{probe_failed}）"
                                    "⇒ 实体阻断无法构造。补救方向：给这些 NOT NULL 列补 "
                                    "`_dummy_sql` 分支或加 FK 夹具。")
                        else:
                            probe = (
                                f'select count(*) from public."{table}" '
                                f'where "{column}" = %s',
                                (value,),
                            )
                            with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
                                before_rows = int(conn.execute(*probe).fetchone()[0])

                            code, tail = drill_db.migrate(db, "downgrade", "-1")
                            add("refuse:downgrade-blocked-by-incompatible-data", code != 0,
                                "downgrade 被存量数据拒绝（EXIT≠0）" if code != 0 else
                                f"⚠️ downgrade **成功**了（{drill_rev} → {below_rev}）"
                                " ⇒ 它会静默丢数据，迁移 docstring 的承诺是假的")

                            v_refused = _version(db)
                            add("refuse:version-stays-at-drill-revision",
                                v_refused == drill_rev,
                                f"被拒后版本={v_refused}（应仍为 {drill_rev}）"
                                + ("" if v_refused == drill_rev else
                                   " ⇒ 失败的回滚**半途生效**了，库处于中间态"))

                            with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
                                after_rows = int(conn.execute(*probe).fetchone()[0])
                            add("refuse:row-not-silently-deleted",
                                before_rows > 0 and after_rows == before_rows,
                                f"探针行 {before_rows} → {after_rows}"
                                + ("" if before_rows > 0 and after_rows == before_rows else
                                   " ⇒ 要么探针没插进去（0==0 恒真），要么行被静默删了"))

                            now_checks = _check_literals(db)
                            same = (now_checks.get((table, conname), ("", frozenset()))[1]
                                    == drill_checks.get((table, conname), ("", frozenset()))[1])
                            add("refuse:constraint-not-half-applied", same,
                                f"约束 {conname} 的字面量域与 {drill_rev} 态一致" if same else
                                f"⚠️ 约束 {conname} 被改成了中间态："
                                f"{sorted(now_checks.get((table, conname), ('', frozenset()))[1])}")

                            with psycopg.connect(drill_db.dsn(db), autocommit=True) as conn:
                                conn.execute(
                                    f'delete from public."{table}" '
                                    f'where "{column}" = %s', (value,)
                                )
                            code, tail = drill_db.migrate(db, "downgrade", "-1")
                            add("refuse:succeeds-after-row-removed",
                                code == 0 and _version(db) == below_rev,
                                f"删行后再 downgrade -1 EXIT={code}，版本={_version(db)}"
                                + (f"（应落到 {below_rev}）"
                                   if _version(db) != below_rev else "")
                                + ("" if code == 0 else f"；{tail}"))

        # ---------------- 幕 5①：首条迁移失败 → 版本纹丝不动 + 整体回滚 ----------------
        # ⚠️ 幕 4 可能把库留在**比 prev2_rev 更旧**的版本上（它按链导航到
        #    最近的"收窄取值域"迁移）⇒ 复位必须**双向**，否则会一路降到 base
        #    再在空库上注入 ⇒ `UndefinedTable` 崩溃（2026-10-09 实测踩到）。
        reset_failed = _navigate_to(db, order, prev2_rev)
        if reset_failed:
            notes.append(f"  ⚠️ 回退到开始态失败：{reset_failed}")

        # ⚠️ `obstacle` 必须在**分支之外**先落地：幕 5② 会用它清理桩对象，
        #    而 `inj_first is None` 那条分支不赋值 ⇒ 不初始化就是 `NameError`
        #    （不是 FAIL 而是崩，ISS-43：崩溃与"跑失败"同形，都会掩盖真实结论）。
        obstacle = ""
        #: 幕 6 要拿 `first_objects` 做 A/B 对照，故它也必须活在分支之外。
        first_objects: list[dict[str, str]] = []
        if inj_first is None:
            add("fail:upgrade-rc-nonzero", False,
                f"无法为迁移 {prev_rev} 推导出注入手段 ⇒ 第①种粒度无法演练")
            add("fail:version-unchanged-when-first-migration-fails", False, "同上")
            add("fail:partial-ddl-rolled-back", False, "同上")
            add("fail:obstacle-intact", False, "同上")
        else:
            # ★ 2026-10-09 配对观测（修 M5 假绿）：`fail:obstacle-intact` 若只
            #   "断言效果仍在"，就是一个**结构性恒真式** —— 桩由 `_apply_injection()`
            #   以 autocommit 建在 Alembic 事务之外，而失败正是撞它造成的，
            #   全过程没有任何代码删它 ⇒ 它必然还在（反向验证 M5 实测：把
            #   `_obstacle_intact` 改成 `return True` 后 24/24 全绿）。
            #   ⇒ 必须让该谓词**翻转**：注入前必须**不**成立、注入后必须成立。
            #   这样"恒真"（M5）与"恒假"（谓词写错）都会报红。
            before_intact, before_detail = _obstacle_intact(db, inj_first)
            obstacle = _apply_injection(db, inj_first)

            t0 = time.monotonic()
            code, tail = drill_db.migrate(db, "upgrade", "head")
            first_fail_seconds = round(time.monotonic() - t0, 2)
            add("fail:upgrade-rc-nonzero", code != 0,
                f"注入后 upgrade head EXIT={code}" if code != 0 else
                "⚠️ 注入无效，upgrade 竟然成功了 ⇒ 后续两条断言全无意义")

            add("fail:version-unchanged-when-first-migration-fails",
                _version(db) == prev2_rev,
                f"失败后版本={_version(db)}（应停在开始态 {prev2_rev}）")

            # 只断言"该迁移在障碍**之前**造过的那些对象" —— 它们在失败前已成功造出，
            # 若整体回滚生效就应当**全部不存在**。障碍本身排除在外。
            #
            # ★ 2026-10-09 泛化：旧版只数"表"，故"加列型迁移"的 prior 恒为 0
            #   ⇒ 本判据恒真（ISS-115）。现在逐 step 判存在性（表/列/索引/约束），
            #   与迁移形态无关。
            #
            # ⚠️ 本条必须带下界（`len(prior) >= MIN_PRIOR_OBJECTS`）：
            #    `prior` 为空时 `not leftover` 恒真 ⇒ 一条恒真式，
            #    而它看上去与"真的验证了部分 DDL 回滚"一模一样（ISS-115）。
            prior = _prior_steps(inj_first)
            leftover = [s for s in prior if _object_exists(db, s)]
            enough = len(prior) >= MIN_PRIOR_OBJECTS
            add("fail:partial-ddl-rolled-back", enough and not leftover,
                f"该迁移在障碍前造了 {len(prior)} 个对象"
                f"（{[_ident(s) for s in prior]}）"
                + ("" if enough else
                   f" ⇒ 不足下界 {MIN_PRIOR_OBJECTS}，**本条无法自证**（不是通过）")
                + ("均已回滚" if enough and not leftover else
                   ("" if not leftover else
                    f"；仍有残留：{[_ident(s) for s in leftover]}")))

            after_intact, detail = _obstacle_intact(db, inj_first)
            # ⚠️ 三条件缺一不可：
            #   ① 注入前**不**成立 —— 否则"注入"这件事本身没发生（名字撞了已有的
            #      成品对象 / 上次残留未清），此时"注入后成立"是白捡的；
            #   ② 注入后成立 —— 失败确实由注入造成；
            #   ③ 中途没有被人为抹平。
            #   只有①+②齐备，本条才是一个能区分状态的谓词（不是恒真式）。
            add("fail:obstacle-intact", (not before_intact) and after_intact,
                f"注入前：{before_detail}；注入后：{detail}"
                + ("" if not before_intact else
                   " ⇒ ⚠️ 注入前该效果**就已成立** ⇒ 注入没真正制造出新条件"
                   "（桩名撞了成品对象，或上次残留没清干净），本条无区分力"))

        # ---------------- 幕 5②：末条迁移失败 → 版本推进到上一条已提交迁移 ----------------
        # 𝒃 2026-10-09：清理改为按 kind 分派（四种桩对象），见 `_clear_injection`。
        if inj_first is not None:
            _clear_injection(db, inj_first)

        if inj_last is None:
            add("granularity:version-advances-past-committed-migration", False,
                f"无法为迁移 {head_rev} 推导出注入手段 ⇒ 第②种粒度无法演练")
            add("granularity:committed-migration-objects-survive", False, "同上")
        else:
            _apply_injection(db, inj_last)

            code, tail = drill_db.migrate(db, "upgrade", "head")
            add("granularity:version-advances-past-committed-migration",
                code != 0 and _version(db) == prev_rev,
                f"末条迁移失败（EXIT={code}），版本={_version(db)}"
                f"（应推进到已提交的 {prev_rev}）"
                + ("" if code != 0 else " ⚠️ 注入无效，upgrade 成功了")
                + ("" if _version(db) == prev_rev else
                   " ⚠️ 若版本停在开始态，说明 transaction_per_migration 未生效"))

            # ★ 2026-10-09 泛化：旧版只看 `prev_rev` 建的**表**（`_created_tables`），
            #   而本仓 `prev_rev`（`b7c1e4a9d203`）一张表都不建 ⇒ 集合为空 ⇒
            #   "全都在"与"全丢了"同形 ⇒ 幕 5② 与幕 6 的两条判据都失去区分力。
            #   现在看 `upgrade()` 造出的**全部**对象（列/索引/约束也算）。
            first_objects = _upgrade_steps(
                ast.parse(rev_path[prev_rev].read_text(encoding="utf-8"))
            )
            present = [s for s in first_objects if _object_exists(db, s)]
            enough_obs = len(first_objects) >= MIN_OBSERVED_OBJECTS
            add("granularity:committed-migration-objects-survive",
                enough_obs and len(present) == len(first_objects),
                f"{prev_rev} 造的 {len(first_objects)} 个对象在库里现存 {len(present)} 个"
                + ("" if enough_obs else
                   f" ⇒ 对象数不足下界 {MIN_OBSERVED_OBJECTS}，**本条无法自证**（不是通过）")
                + ("" if len(present) == len(first_objects) else
                   f"；缺失：{[_ident(s) for s in first_objects if s not in present]}"
                   " ⇒ 与'上一条已提交迁移的成果保留'矛盾"))

        # ---------------- 幕 6：反向验证 —— 把 transaction_per_migration 翻成 False ----------------
        #
        # 幕 5② 的绿只在 `transaction_per_migration=True` 时为真。ISS-110：
        # **绿灯本身没有信息，绿灯的灵敏度才有**。故这里把那一行**翻转**后
        # 跑**同一个场景、同一个注入、只差一行**，断言落点**必须不同**：
        #   True  ⇒ 版本停在 prev_rev（上一条已提交），prev_rev 造的对象都在
        #   False ⇒ 版本停在 prev2_rev（run 起点），prev_rev 造的对象全丢
        # 两臂落点相同 ⇒ 幕 5② 区分不了这件事 ⇒ 它是恒真式。
        #
        # 翻转在 `migrations/` 的**临时副本**里做，仓库文件一个字节都不改。
        landed_true = _version(db)
        true_objects = [s for s in first_objects if _object_exists(db, s)]
        notes.append(
            f"  幕 6 对照臂（本库，tpm=True）：版本={landed_true}，"
            f"{prev_rev} 的对象 {len(true_objects)}/{len(first_objects)} 个在库"
        )

        sandbox = Path(tempfile.mkdtemp(prefix="rollrev-"))
        rev_db = f"rednote_rollrev_{stamp}"
        arm: dict[str, object] = {"db": rev_db}
        try:
            ini, _env_copy = _stage_migrations(sandbox, flip=True)
            drill_db.create_db(rev_db)
            arm["upgrade_head_rc"], tail_up = _alembic_in_sandbox(ini, rev_db, "upgrade", "head")
            if arm["upgrade_head_rc"] == 0:
                arm["downgrade_rc"], tail_dn = _alembic_in_sandbox(
                    ini, rev_db, "downgrade", "-2")
            if arm.get("downgrade_rc") == 0 and inj_last is not None:
                _apply_injection(rev_db, inj_last)
                arm["injected_upgrade_rc"], tail_inj = _alembic_in_sandbox(
                    ini, rev_db, "upgrade", "head")
                arm["landed_version"] = _version(rev_db)
                arm["objects_present"] = {
                    _ident(s): _object_exists(rev_db, s) for s in first_objects
                }
                arm["tail"] = tail_inj[-400:].replace("\n", " ")

            landed = str(arm.get("landed_version", ""))
            rc_inj = arm.get("injected_upgrade_rc")
            add("reverse:tpm-false-lands-at-run-start",
                rc_inj is not None and rc_inj != 0 and landed == prev2_rev,
                f"翻转 transaction_per_migration=False 后同一注入的落点="
                f"{landed or '<未取得>'}（应落在 run 起点 {prev2_rev}；"
                f"对照臂落在 {landed_true}）"
                + ("" if rc_inj not in (None, 0) else
                   "；⚠️ 沙箱臂注入无效（upgrade 成功了），本臂没测到东西")
                + ("" if landed != landed_true else
                   "；⚠️ **两臂落点相同** ⇒ `granularity:*` 区分不了 tpm 的真假，是恒真式"))

            flipped: dict[str, bool] = arm.get("objects_present") or {}  # type: ignore[assignment]
            kept = [s for s in first_objects if flipped.get(_ident(s))]
            add("reverse:tpm-off-discards-committed-migration",
                bool(first_objects) and not kept,
                f"翻转后 {prev_rev} 造的 {len(first_objects)} 个对象现存 {len(kept)} 个"
                f"（应全丢：整个 run 一个事务）；对照臂现存 {len(true_objects)} 个"
                + ("" if first_objects and not kept else
                   " ⇒ 两臂无差异，本条无从区分"))
        except (RuntimeError, OSError) as exc:
            for name in ("reverse:tpm-false-lands-at-run-start",
                         "reverse:tpm-off-discards-committed-migration"):
                add(name, False,
                    f"沙箱构造失败：{type(exc).__name__}: {exc} ⇒ 反向验证跑不起来。"
                    "不允许跳过（ISS-143）：要么 env.py 的声明形态变了（则 `_TPM_LITERAL` 要修），"
                    "要么沙箱技术失效（则幕 6 要重写）")
            notes.append(f"  ⚠️ 幕 6 沙箱异常：{type(exc).__name__}: {exc}")
        finally:
            drill_db.drop_db(rev_db)
            shutil.rmtree(sandbox, ignore_errors=True)

        # ---------------- 证据 ----------------
        evidence_dir = REPO_ROOT / "evidence" / "m5"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        rec = {
            "drill_at": datetime.now(timezone.utc).isoformat(),
            "database": db,
            "head_revision": head_rev,
            "rolled_back_revision": prev_rev,
            "chain_length": len(order),
            "first_migration_failure_seconds": first_fail_seconds
            if inj_first is not None else None,
            "rollback_budget_seconds": ROLLBACK_BUDGET_SECONDS,
            "data_incompatible_target": list(target) if target else None,
            "data_incompatible_drill_revision": drill_rev or None,
            "data_incompatible_below_revision": below_rev or None,
            "narrowing_revisions": [
                {"index": i, "revision": r} for i, r, _ in narrowings
            ],
            "injections": {"first_migration": inj_first, "last_migration": inj_last},
            # 幕 6 的 A/B 两臂落点：这两行是「granularity:* 不是恒真式」的**证据本体**。
            # 两臂的 `landed_version` 若相同，反向验证那两条判据会报红。
            "tpm_ab": {
                "true_landed_version": landed_true,
                "true_objects_present": [_ident(s) for s in true_objects],
                "false_arm": arm,
            },
            "fingerprint_at_head": fa,
        }
        (evidence_dir / f"rollback-drill-{stamp}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        add("evidence:drill-record-written", True,
            f"evidence/m5/rollback-drill-{stamp}.json")

    finally:
        drill_db.drop_db(db)

    return _report(checks, notes)


def _report(checks: list[tuple[str, bool, str]], notes: list[str]) -> int:
    print("## 回滚演练（WP47 ⑤⑥）")
    for n in notes:
        print(n)
    if notes:
        print()
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'} {name}（{detail}）")
    failed = [n for n, ok, _ in checks if not ok]
    total = len(checks)
    print(f"\nRESULT={'PASS' if not failed else 'FAIL'} checks={total}/{EXPECTED_CHECKS} "
          f"failures={len(failed)}")
    if total != EXPECTED_CHECKS:
        print(f"  ⚠️ 判据条数 {total} ≠ 期望 {EXPECTED_CHECKS} —— 新增/删除判据须同步 "
              f"EXPECTED_CHECKS（ISS-153：`>=` 只防变少不防变多）")
    return 1 if (failed or total != EXPECTED_CHECKS) else 0


if __name__ == "__main__":
    sys.exit(main())
