#!/usr/bin/env python
"""`check_all_exports.py` 的**反向验证**：把错误实现放回去，判据必须报红。

产出：`evidence/m3/all-exports-reverse.log`

## 为什么需要这个文件

`tools/quality/check_all_exports.py` 此刻打印 `[PASS]`，但那**只证明
"当前工作区看起来自洽"**，不能证明"它被违反时会报红"。两者之差就是
判据纪律第 9 条：

> **「声称自己有防线」比「没有防线」更危险。**

一个恒真判据（ISS-115）、一个覆盖范围被悄悄收窄的判据（ISS-139/143）、
一个只有警告没有报红的判据 —— 三者都打印 `[PASS]`。

## ⚠️ 本脚本初版踩到的四个坑（全部是**变异测试自身**的失效形态）

第一版变异**没落在真实条目上**：从 `content/types.py` 拷来底座，却去
replace 只有 media 模块才有的 `"SORT_ORDER_COVER"` ⇒ 那次 replace
**什么也没改**，于是溢出文件与底座**逐字节相同**，判据"正确地"给出了
`PASS`。我当时看到的是：

```
m1_a1  exit=0     ← 变异"通过"了
m2_dup exit=0
```

若就此收工，就会得到一条**假绿**的反向验证，而它与"判据能报红"
**从输出上无法区分** —— 正是纪律第 5 条（ISS-143）：

> **「没跑」与「跑失败」同形。**

于是加了 `_write_variant()` 里的两条**生效断言**：锚点必须真的出现在
底座里；产物必须与底座**字节不同**。每条变异的 `sha256[:12]` 也打进 log，
让"变异确实发生了"**可复跑、可对账**。

### 坑 2：成对证明没隔离变量（A3 第一次）

`[6]` 想证明"`[3]` 的红是 A3 造成的"。但 A3 的变异会**连带**触发 A4
（非字面量 ⇒ 该文件被判据排除 ⇒ 条目数归零 ⇒ 活性下界报红）⇒
摘掉 A3 判定后**仍然是红的**，成对证明假失败。
修法：单变量实验必须把**所有**会连带触发的面一起按住。

### 坑 3：变异把脚本弄崩，不是"变绿"（A3 第二次）

A3 初版把 `if literal is None:` 整支置 `False`，于是紧跟着的 `continue`
也没了 ⇒ `entries.extend(None)` 抛 `TypeError` ⇒ `rc=1`。
那**看起来也报了红**，但成对证明要的是**假绿**：它证明的是
"这个变异会让脚本崩"，**没**证明"红是 A3 造成的"。

### 坑 4：`_literal_entries` 有**两条** `return None`（A3 第三次）

只把尾部那条改成 `return []`，`ast.literal_eval` 抛异常那条**仍然**让
A3 报红。⇒ 单变量变异必须覆盖**该函数的全部失效路径**。
这就是本仓反复出现的形态：**"覆盖范围由人写"就一定会漏**（铁律 14），
哪怕写它的是变异脚本作者。

## 变异清单（逐条与 `main()` 里的 `[N]` 节一一对应）

| # | 节 | 变异 | 期望 | 它证明的失效面 |
|---|---|---|---|---|
| 0 | `[0]` | 副本基线（未变异） | `[PASS]` | 判据在未变异时是绿的 |
| 1 | `[1]` | 加一个模块级**不存在**的名字进 `__all__` | `[FAIL]` + `A1` + 该名字 | A1 抓得住"手写公开面写错名" |
| 2 | `[2]` | 让 `__all__` 出现**重复**条目 | `[FAIL]` + `A2` + 该名字 | A2 抓得住重复登记 |
| 3 | `[3]` | 把 `__all__` 改成**非字面量**（变量拼装） | `[FAIL]` + `A3` | A3 反 ISS-95：判据扫不到时必须**报红**而不是静默跳过 |
| 4 | `[4]` | 在 `[1]` 基础上**摘掉 A1 判定** | 假绿 | 成对证明：`[1]` 的红**确实是 A1 造成的**（不是别的巧合） |
| 5 | `[5]` | `MIN_SCOPED_ENTRIES` 抬到 999 | `[FAIL]` + `A4` | A4 活性下界真的在判（不是装饰） |
| 6 | `[6]` | 在 `[3]` 基础上**摘掉 A3 判定** | 假绿 | 成对证明：`[3]` 的红**确实是 A3 造成的** |

> ⚠️ `[4]`/`[6]` 这两条**成对证明**是本仓的固定手法：只证明"变异 → 报红"
> 还不足以说明**是这条判据**报的红（可能是另一条判据恰好也红了）。
> 把判定摘掉后必须转为假绿，才证明因果关系。

## 两种模式（`--fast` 不是空壳参数）

| 模式 | 变异 | 产物 | 备注 |
|---|---|---|---|
| `--fast` | `[0]`~`[6]` 全部 | 同一个 log | 本脚本全部用例都是纯 AST / 副本，无慢用例；`--fast` 与完整模式**覆盖相同**，故不设差异分支 |
| 完整 | 同上 | 同上 | 本脚本**没有慢用例**，保留同名开关只为与同族脚本（discipline / test-DB）调用口径一致 |

> ⚠️ 上面这句"没有慢用例"是**判据自己要守的话**：由
> `tools/quality/collect_discipline_code_review.py` 同族的三遍法逐行核对
> —— 若将来本脚本长出真库用例，这张表必须同步改，否则表在说谎。

跑法：`python tools/quality/collect_all_exports_reverse_evidence.py [--fast]`
退出码 0 = 全部变异都被抓住（且成对证明成立）。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "tools" / "quality" / "check_all_exports.py"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m3"
LOG_PATH = EVIDENCE_DIR / "all-exports-reverse.log"

#: 底座：取一个**真实存在**、结构典型（大量常量 + 若干 dataclass + 函数）的
#: `__all__` 文件。选它而不是自造一份，是因为"变异要落在真文件上"——
#: 自造底座的形态可能与真文件不同，变异就变成测"我编的语法"而非"真代码"。
BASE_REL = "services/backend/src/rednote/modules/media/types.py"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sha12(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _run(checker: Path, target: Path | None) -> tuple[int, str]:
    cmd = [sys.executable, str(checker)]
    if target is not None:
        cmd.append(str(target))
    env = {"NO_PROXY": "*", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
    return proc.returncode, proc.stdout + proc.stderr


def _anchor_needle(src: str) -> tuple[str, str]:
    """从底座里取一个真实 `__all__` 条目作锚点，返回 `(名字, 源码里的整行)`。

    ⚠️ 取不到就**崩**（不是跳过）：底座换了、或 `__all__` 写法变了，
    会让所有变异静默失效 ⇒ 假绿。宁可崩，不可假绿（ISS-143）。
    """
    import ast

    tree = ast.parse(src)
    entries: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            value = ast.literal_eval(node.value)
            entries = [x for x in value if isinstance(x, str)]
    assert entries, f"底座 {BASE_REL} 里没有字面量 __all__ ⇒ 变异无从下手"
    name = entries[len(entries) // 2]
    line = f'    "{name}",'
    assert line in src, f"锚点整行 {line!r} 不在底座源码里 ⇒ 变异必然失效"
    return name, line


def _write_variant(path: Path, text: str, base_text: str) -> str:
    """写变异产物，并**断言变异确实生效**。返回 sha12。"""
    path.write_text(text, encoding="utf-8")
    assert text != base_text, f"{path.name} 与底座逐字节相同 ⇒ 变异未生效（ISS-143 假绿）"
    return _sha12(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true", help="（保留参数：本脚本无慢用例，与同族脚本口径一致）")
    args = parser.parse_args()

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def emit(text: str = "") -> None:
        lines.append(text)
        print(text)

    checker_sha = _sha12(CHECKER)
    emit(f"# `check_all_exports.py` 反向验证 —— {_now()}")
    emit(f"# 被测判据：tools/quality/check_all_exports.py  sha256[:12]={checker_sha}")
    emit(f"# 模式：{'--fast' if args.fast else '完整'}")
    emit()

    base_path = REPO_ROOT / BASE_REL
    if not base_path.is_file():
        emit(f"RESULT=FAIL 底座不存在：{BASE_REL}")
        LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 1
    base_text = base_path.read_text(encoding="utf-8")
    anchor, needle = _anchor_needle(base_text)
    emit(f"# 底座：{BASE_REL}  sha256[:12]={_sha12(base_path)}")
    emit(f"# 变异锚点：__all__ 条目 {anchor!r}")
    emit()

    checks: list[tuple[str, bool]] = []

    def add(name: str, ok: bool) -> None:
        checks.append((name, ok))

    with tempfile.TemporaryDirectory(prefix="wb-allexport-rev-") as tmp:
        tmpdir = Path(tmp)

        # ---- [0] 基线：未变异的副本必须 PASS
        base_copy = tmpdir / "base.py"
        base_copy.write_text(base_text, encoding="utf-8")
        rc0, out0 = _run(CHECKER, base_copy)
        ok0 = rc0 == 0 and "[PASS]" in out0
        emit(f"[0] 基线（未变异）           rc={rc0}  {'PASS' if ok0 else 'FAIL'}  sha={_sha12(base_copy)}")
        add("基线绿", ok0)

        # ---- [1] A1：加一个模块级不存在的名字
        v1 = tmpdir / "mut_a1.py"
        s1 = _write_variant(v1, base_text.replace(needle, needle + '\n    "NOT_BOUND_ANYWHERE_X",', 1), base_text)
        rc1, out1 = _run(CHECKER, v1)
        ok1 = rc1 != 0 and "A1" in out1 and "NOT_BOUND_ANYWHERE_X" in out1
        emit(f"[1] A1 加不存在名            rc={rc1}  {'PASS' if ok1 else 'FAIL'}  sha={s1}")
        emit(_indent(out1))
        add("A1 抓得住", ok1)

        # ---- [2] A2：重复条目
        v2 = tmpdir / "mut_a2.py"
        s2 = _write_variant(v2, base_text.replace(needle, needle + f'\n    "{anchor}",', 1), base_text)
        rc2, out2 = _run(CHECKER, v2)
        ok2 = rc2 != 0 and "A2" in out2 and anchor in out2
        emit(f"[2] A2 重复条目              rc={rc2}  {'PASS' if ok2 else 'FAIL'}  sha={s2}")
        emit(_indent(out2))
        add("A2 抓得住", ok2)

        # ---- [3] A3：非字面量
        v3 = tmpdir / "mut_a3.py"
        s3 = _write_variant(v3, _make_nonliteral(base_text), base_text)
        rc3, out3 = _run(CHECKER, v3)
        ok3 = rc3 != 0 and "A3" in out3 and "字面量" in out3
        emit(f"[3] A3 非字面量              rc={rc3}  {'PASS' if ok3 else 'FAIL'}  sha={s3}")
        emit(_indent(out3))
        add("A3 抓得住", ok3)

        # ---- [4] 成对证明：在 [1] 基础上摘掉 A1 判定 ⇒ 必须转假绿
        c4 = tmpdir / "checker_no_a1.py"
        c4_text = _strip_check(CHECKER.read_text(encoding="utf-8"), "A1")
        c4.write_text(c4_text, encoding="utf-8")
        assert c4_text != CHECKER.read_text(encoding="utf-8"), "摘除 A1 判定未生效"
        rc4, out4 = _run(c4, v1)
        ok4 = rc4 == 0 and "[FAIL]" not in out4
        emit(f"[4] 摘掉 A1 判定（成对）      rc={rc4}  {'PASS' if ok4 else 'FAIL'}  sha={_sha12(c4)}")
        add("[1] 的红确实是 A1 造成的", ok4)

        # ---- [5] A4：活性下界抬到 999
        c5 = tmpdir / "checker_floor.py"
        c5_text = re.sub(r"^MIN_SCOPED_ENTRIES = \d+", "MIN_SCOPED_ENTRIES = 999", CHECKER.read_text(encoding="utf-8"), flags=re.M)
        assert c5_text != CHECKER.read_text(encoding="utf-8"), "抬高下界未生效"
        c5.write_text(c5_text, encoding="utf-8")
        rc5, out5 = _run(c5, base_copy)
        ok5 = rc5 != 0 and "A4" in out5
        emit(f"[5] A4 下界抬到 999          rc={rc5}  {'PASS' if ok5 else 'FAIL'}  sha={_sha12(c5)}")
        add("A4 活性下界在判", ok5)

        # ---- [6] 成对证明：在 [3] 基础上摘掉 A3 判定 ⇒ 必须转假绿
        # ⚠️ 必须**同时**把下界归零，否则不是单变量实验：A3 的变异会连带
        #    触发 A4（非字面量 ⇒ 该文件被判据排除 ⇒ 条目数归零 ⇒ 活性下界报红）。
        #    本机实测过这一版：[6] 曾因 A4 仍在红而判 FAIL —— 那不是判据的毛病，
        #    是**成对证明没隔离变量**。铁律：成对证明必须只改一个东西。
        c6 = tmpdir / "checker_no_a3.py"
        c6_src = CHECKER.read_text(encoding="utf-8")
        c6_text = _strip_check(c6_src, "A3")
        # ⚠️ 显式路径模式下 A4 的两个下界**都要**归零（`MIN_SCOPED_FILES` 与
        #    `MIN_SCOPED_ENTRIES`）—— 只归零后者，`0 >= 1` 仍会让 A4 报红。
        #    本机第三次踩到同一族的错：**成对证明的隔离要穷举被隔离面**。
        c6_text = re.sub(
            r"^MIN_SCOPED_(FILES|ENTRIES) = \d+",
            lambda m: f"MIN_SCOPED_{m.group(1)} = 0",
            c6_text,
            flags=re.M,
        )
        assert c6_text != c6_src, "摘除 A3 判定 + 下界归零未生效"
        c6.write_text(c6_text, encoding="utf-8")
        rc6, out6 = _run(c6, v3)
        ok6 = rc6 == 0 and "[FAIL]" not in out6
        emit(f"[6] 摘掉 A3 判定（成对）      rc={rc6}  {'PASS' if ok6 else 'FAIL'}  sha={_sha12(c6)}")
        add("[3] 的红确实是 A3 造成的", ok6)

    passed = sum(1 for _, ok in checks if ok)
    emit()
    for name, ok in checks:
        emit(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    verdict = passed == len(checks)
    emit(f"RESULT={'PASS' if verdict else 'FAIL'} checks={passed}/{len(checks)}")
    LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写出 {LOG_PATH.relative_to(REPO_ROOT)}")
    return 0 if verdict else 1


def _indent(text: str) -> str:
    keep = [ln for ln in text.splitlines() if ln.strip().startswith(("[FAIL]", "★", "-", "  -"))]
    return "\n".join("      " + ln.strip() for ln in keep[:6])


def _make_nonliteral(src: str) -> str:
    """把 `__all__ = [ ... ]` 改成 `__all__ = _NAMES + ["X"]`（非字面量）。"""
    out: list[str] = []
    skipping = False
    for ln in src.splitlines(keepends=True):
        if not skipping and ln.startswith("__all__ = ["):
            out.append("_NAMES = [\n")
            skipping = True
            continue
        if skipping:
            if ln.rstrip() == "]":
                skipping = False
                out.append("]\n")
                out.append('__all__ = _NAMES + ["OK"]\n')
            continue
        out.append(ln)
    return "".join(out)


def _strip_check(src: str, tag: str) -> str:
    """把某条判据**失效**（模拟"这条判据不存在"），返回变异后的源码。

    ⚠️ 变异必须**真的只让那条判据失效**，不能让脚本崩。
    本机踩过：A3 初版把 `if literal is None:` 整支置 `False`，于是后面
    紧跟的 `continue` 也没了 ⇒ `entries.extend(None)` 抛 `TypeError`
    ⇒ rc=1。那看起来"也报了红"，但成对证明要的是**假绿**：
    它证明了"变异会让脚本崩"，**没**证明"红是 A3 造成的"。
    正解是让 `_literal_entries` 对非字面量返回 `[]`（= 静默当成"没有
    `__all__`"）—— 那正是 A3 要防的 ISS-95 形态。
    """
    if tag == "A1":
        return src.replace(
            "if missing:\n            a1_hits.append(Found(path, \"条目未在模块级绑定\", missing))",
            "if False:\n            a1_hits.append(Found(path, \"条目未在模块级绑定\", missing))",
            1,
        )
    if tag == "A3":
        # ⚠️ `_literal_entries` 有**两条**返回 `None` 的路径：
        #     ① `ast.literal_eval` 抛异常（`__all__ = _NAMES + ["X"]` 走这条）
        #     ② 求值成功但不是"全字符串的 list/tuple"
        #   只变异其中一条 ⇒ 另一条仍让 A3 报红 ⇒ 成对证明的 [6] 永远是红的。
        #   本机连踩两次（第一次连 `continue` 一起摘掉导致 TypeError）。
        #   单变量变异必须覆盖**该函数的全部失效路径**。
        step1 = src.replace(
            "    except Exception:\n        return None\n", "    except Exception:\n        return []\n", 1
        )
        assert step1 != src, "A3 变异①未生效（`literal_eval` 的 `except → return None` 形状变了）"
        step2 = step1.replace(
            "        return list(literal)\n    return None", "        return list(literal)\n    return []", 1
        )
        assert step2 != step1, "A3 变异②未生效（`return None` 形状变了）"
        return step2
    raise ValueError(tag)


if __name__ == "__main__":
    sys.exit(main())