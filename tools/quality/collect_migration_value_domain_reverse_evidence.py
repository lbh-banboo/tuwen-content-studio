"""反向验证：把「迁移活引用取值域」的违规放回去，`check_migration_value_domain_no_live_import.py` 必须报红。

为什么必须有这个脚本（否则判据本身就是 ISS-95）：
    「有一条判据」和「这条判据抓得住它声称要抓的东西」是两件事。
    只跑一遍判据看到 `[PASS]`，可能只是因为它**扫不到东西** ——
    一个永远返回"没问题"的检查器，对任何输入都是绿的。

本脚本用**变异**证明判据真的在判：把违规放回去，它必须红。

# ---------------------------------------------------------------------------
# 变异矩阵
# ---------------------------------------------------------------------------
| #  | 变异                                                   | 预期             | 证明什么                                        |
|----|--------------------------------------------------------|------------------|------------------------------------------------|
| 0  | 未变异（真实迁移目录）                                  | `[PASS]`         | 判据在当前工作树上是绿的                        |
| 1  | CHECK 文本**直接**用活 import 的名字                    | `[FAIL]` + `G1`  | 判据抓得住直接形态                              |
| 2  | CHECK 用迁移内函数，函数 return 活 import（**WP23 真形态**） | `[FAIL]` + `G1`  | **传递闭包真的在算** —— 不做闭包就抓不到，而"抓不到"的表现是全绿 |
| 3  | `op.create_check_constraint`（改已有表那条路）          | `[FAIL]` + `G1`  | 第二种写法真被检查（不是只认 `sa.CheckConstraint`）|
| 4  | 清空豁免表（真实目录）                                  | `[FAIL]` + `G1`  | **豁免真的在起作用** —— 判据对 WP23 的 3 处确实会红 |
| 5  | 纯字面量 CHECK                                          | `[PASS]`         | 阴性对照：不是"见 CHECK 就红"                   |
| 6  | 变异 1 + `LIVE_MODULE_PREFIX` 改成不可能的值            | 假绿             | **成对证明**：变异 1 的红确实来自活引用判定      |
| 7  | `MIN_MIGRATIONS` 抬到 9999                              | `[FAIL]` + `G3a` | 活性下界真的在判（不是装饰）                    |
| 8  | 加一条**已失效**的豁免                                  | `[FAIL]` + `G2a` | 豁免不会滞留 —— 修了违规不删豁免就会红          |

> ⚠️ `[6]` 是**成对证明**，本仓固定手法：只证明"变异 → 报红"还不足以说明
> **是这条判据**报的红（可能是别的判据恰好红了）。把判定前提摘掉后必须
> 转为假绿，才证明因果关系。

# ---------------------------------------------------------------------------
# 变异底座：临时目录，不碰工作树
# ---------------------------------------------------------------------------
本判据的扫描目录是模块级常量 `MIGRATIONS_DIR`，可以用 `importlib` 加载后
覆写 —— 因此变异**不需要改真实迁移文件**（对比 `collect_node_paired_guard_
reverse_evidence.py` 必须就地改 `media/ddl.py`，因为那个判据接不了路径参数）。
临时目录刻意拼出 `migrations/versions/` 结构，让判据里的
`path.relative_to(REPO_ROOT)` 照常成立。

⚠️ `importlib` 按路径加载模块**必须**先注册 `sys.modules`（ISS-164）——
`tools/m1/rerun_all_evidence.py` 曾因漏注册而从未真正跑通一次。

跑法：`python tools/quality/collect_migration_value_domain_reverse_evidence.py`
退出码 0 = 全部变异都被抓住（且成对证明成立）。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "tools" / "quality" / "check_migration_value_domain_no_live_import.py"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"
LOG_PATH = EVIDENCE_DIR / "migration-value-domain-reverse.log"

#: 把所有活性下界归零 —— 变异跑在**临时目录**（只有 1~2 个迁移文件），
#: 若下界仍生效，G3 会先红，那这个"红"就不是我们要证明的东西。
#: 下界本身由变异 7 单独验证。
_ZERO_FLOORS: dict[str, int] = {
    "MIN_MIGRATIONS": 0,
    "MIN_CHECK_CONSTRAINTS": 0,
    "MIN_LIVE_IMPORTS": 0,
    "MIN_SA_STYLE_CHECKS": 0,
    "MIN_OP_STYLE_CHECKS": 0,
    "MIN_CLEAN_FILES_WITH_LIVE": 0,
    "MIN_LOCAL_NAME_IN_CHECK": 0,
}

#: 变异 1 —— CHECK 文本**直接**引用活 import 的名字。
_MUTANT_DIRECT = '''from __future__ import annotations

import sqlalchemy as sa
from rednote.modules.prompts.types import TASK_KIND_LIST_SQL


def upgrade() -> None:
    sa.CheckConstraint(f"task_kind IN ({TASK_KIND_LIST_SQL})", name=op.f("ck_direct"))
'''

#: 变异 2 —— **WP23 的真实形态**：迁移内函数包一层 import + return。
#: CHECK 文本里出现的是 `_task_kind_sql()`，不是 `TASK_KIND_LIST_SQL`；
#: 不做传递闭包就抓不到，而"抓不到"的表现是**全绿**。
_MUTANT_INDIRECT = '''from __future__ import annotations

import sqlalchemy as sa


def _task_kind_sql() -> str:
    from rednote.modules.prompts.types import TASK_KIND_LIST_SQL

    return TASK_KIND_LIST_SQL


def upgrade() -> None:
    sa.CheckConstraint(f"task_kind IN ({_task_kind_sql()})", name=op.f("ck_indirect"))
'''

#: 变异 3 —— 第二种写法：`op.create_check_constraint`（ALTER 已存在的表）。
#: 老库最容易踩本盲区的就是这条路，故必须与 `sa.CheckConstraint` 同判。
_MUTANT_OP_STYLE = '''from __future__ import annotations

from alembic import op
from rednote.modules.prompts.types import PUBLICATION_ACTION_LIST_SQL as LIVE_SQL


def upgrade() -> None:
    op.create_check_constraint(op.f("ck_op"), "t", LIVE_SQL)
'''

#: 变异 5（阴性对照）—— 迁移内自己定义的**字面量**常量，必须放行。
_MUTANT_CLEAN = '''from __future__ import annotations

import sqlalchemy as sa

_TASK_KIND_SQL = "'A', 'B'"


def upgrade() -> None:
    sa.CheckConstraint(f"task_kind IN ({_TASK_KIND_SQL})", name=op.f("ck_clean"))
'''

_LINES: list[str] = []


def emit(line: str = "") -> None:
    print(line)
    _LINES.append(line)


def _load_checker() -> ModuleType:
    """按路径加载判据模块。

    ⚠️ `sys.modules[spec.name] = mod` **不可省**（ISS-164）：漏注册会让
    dataclass / typing 在解析注解时找不到模块，症状是"莫名其妙的崩"或
    更坏的"被 except 吞掉后的假绿"。
    """
    spec = importlib.util.spec_from_file_location("_mig_value_domain_checker", CHECKER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载判据模块：{CHECKER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(module: ModuleType, **overrides: object) -> tuple[int, str]:
    """在覆写若干模块级常量后跑判据的 `main()`，返回 `(退出码, 全部输出)`。"""
    saved = {key: getattr(module, key) for key in overrides}
    buffer = io.StringIO()
    try:
        for key, value in overrides.items():
            setattr(module, key, value)
        with contextlib.redirect_stdout(buffer):
            code = module.main()
    finally:
        for key, value in saved.items():
            setattr(module, key, value)
    return code, buffer.getvalue()


def _tmp_tree(source: str) -> tuple[Path, Path]:
    """在临时目录里搭 `migrations/versions/`，返回 `(tmp_root, versions_dir)`。"""
    tmp_root = Path(tempfile.mkdtemp(prefix="mig-vd-"))
    versions = tmp_root / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "20260101_0000_deadbeef1234_mutant.py").write_text(source, encoding="utf-8")
    return tmp_root, versions


def _is_pass(code: int, out: str) -> bool:
    return code == 0 and "[PASS]" in out


def main() -> int:
    emit("# 反向验证：迁移 CHECK 文本不得活引用取值域（parity 恒绿盲区）")
    emit(f"# 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    emit(f"# 判据：{CHECKER.relative_to(REPO_ROOT).as_posix()}")
    emit()

    if not CHECKER.is_file():
        emit(f"RESULT=FAIL 判据不存在：{CHECKER}")
        return 1

    checks: list[tuple[str, str, str, bool]] = []
    tmp_roots: list[Path] = []

    def record(label: str, expect: str, ok: bool, detail: str = "") -> None:
        checks.append((label, expect, "PASS" if ok else "FAIL", ok))
        emit(f"  [{'PASS' if ok else 'FAIL'}] {label} —— 预期 {expect}{('；' + detail) if detail else ''}")

    try:
        # ---- [0] 未变异
        code, out = _run(_load_checker())
        record("[0] 未变异（真实迁移目录）", "[PASS]", _is_pass(code, out),
               f"exit={code}")

        # ---- [1] 直接活引用
        root, versions = _tmp_tree(_MUTANT_DIRECT)
        tmp_roots.append(root)
        code, out = _run(_load_checker(), REPO_ROOT=root, MIGRATIONS_DIR=versions,
                         EXEMPT_REFERENCES={}, **_ZERO_FLOORS)
        record("[1] CHECK 直接用活 import 名字", "[FAIL] + G1",
               code == 1 and "G1" in out and "FAIL" in out and "TASK_KIND_LIST_SQL" in out,
               f"exit={code}")

        # ---- [2] 间接（WP23 真形态）—— 传递闭包
        root, versions = _tmp_tree(_MUTANT_INDIRECT)
        tmp_roots.append(root)
        code, out = _run(_load_checker(), REPO_ROOT=root, MIGRATIONS_DIR=versions,
                         EXEMPT_REFERENCES={}, **_ZERO_FLOORS)
        record("[2] 间接形态（函数包 import，WP23 真写法）", "[FAIL] + G1",
               code == 1 and "G1" in out and "FAIL" in out and "_task_kind_sql" in out,
               f"exit={code}")

        # ---- [3] op.create_check_constraint
        root, versions = _tmp_tree(_MUTANT_OP_STYLE)
        tmp_roots.append(root)
        code, out = _run(_load_checker(), REPO_ROOT=root, MIGRATIONS_DIR=versions,
                         EXEMPT_REFERENCES={}, **_ZERO_FLOORS)
        record("[3] op.create_check_constraint 形态", "[FAIL] + G1",
               code == 1 and "G1" in out and "FAIL" in out and "LIVE_SQL" in out,
               f"exit={code}")

        # ---- [4] 清空豁免（真实目录）—— 证明豁免真的在起作用
        code, out = _run(_load_checker(), EXEMPT_REFERENCES={})
        record("[4] 清空豁免表（真实目录）", "[FAIL] + G1",
               code == 1 and "G1" in out and "_task_kind_sql" in out
               and "_tool_ceiling_sql" in out and "_publication_action_sql" in out,
               f"exit={code}")

        # ---- [5] 阴性对照：纯字面量必须放行
        root, versions = _tmp_tree(_MUTANT_CLEAN)
        tmp_roots.append(root)
        code, out = _run(_load_checker(), REPO_ROOT=root, MIGRATIONS_DIR=versions,
                         EXEMPT_REFERENCES={}, **_ZERO_FLOORS)
        record("[5] 阴性对照：迁移内字面量常量", "[PASS]", _is_pass(code, out),
               f"exit={code}")

        # ---- [6] 成对证明：摘掉活引用前提后，变异 1 必须转假绿
        root, versions = _tmp_tree(_MUTANT_DIRECT)
        tmp_roots.append(root)
        code, out = _run(_load_checker(), REPO_ROOT=root, MIGRATIONS_DIR=versions,
                         EXEMPT_REFERENCES={}, LIVE_MODULE_PREFIX="__no_such_module__",
                         **_ZERO_FLOORS)
        record("[6] 成对证明：变异 1 + 摘掉活引用前提", "假绿", _is_pass(code, out),
               f"exit={code}")

        # ---- [7] 活性下界
        code, out = _run(_load_checker(), MIN_MIGRATIONS=9999)
        record("[7] MIN_MIGRATIONS 抬到 9999", "[FAIL] + G3a",
               code == 1 and "G3a" in out and "FAIL" in out, f"exit={code}")

        # ---- [8] 失效豁免必须红（豁免不得滞留）
        stale_key = (
            "migrations/versions/20260924_1359_3cb74d02edc5_wp23_prompt_versions_and_tool_ceiling.py",
            "_this_name_does_not_exist_",
        )
        code, out = _run(_load_checker(),
                         EXEMPT_REFERENCES={**_load_checker().__dict__["EXEMPT_REFERENCES"],
                                            stale_key: "占位：本条是反向验证注入的失效豁免，用于证明 G2a 真的在判。"})
        record("[8] 失效豁免（对应违规已不存在）", "[FAIL] + G2a",
               code == 1 and "G2a" in out and "FAIL" in out, f"exit={code}")

    finally:
        for root in tmp_roots:
            shutil.rmtree(root, ignore_errors=True)

    emit()
    passed = sum(1 for _, _, _, ok in checks if ok)
    verdict = passed == len(checks)
    emit(f"RESULT={'PASS' if verdict else 'FAIL'} checks={passed}/{len(checks)}")

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("\n".join(_LINES) + "\n", encoding="utf-8")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
