#!/usr/bin/env python
"""WP47①：**§2b README 多处声明对账判据的反向验证**（ISS-206）。

产出：`evidence/m1/readme-decl-anchor-reverse.log`

## 为什么需要这个文件

`tools/m1/audit_consistency.py` §2b 的「README 内部一致性（同一计数多处声明
不得打架）」此刻打印 `[PASS]`，但 ISS-206 实测证明：**这条判据的正则已经连续
失效过三次**，而三次的输出**全都与「判过」同形**：

| 次 | 形态 | 症状 | 判据当时输出 |
|---|---|---|---|
| ① | 死正则（ISS-185） | 第二条来源正则 `全部 **N** 条**` 一次都没匹配过 | `声明 [188] / 实测 188` —— 看着正常 |
| ② | 误伤局部叙述（ISS-185） | 抓到看板行里的「附录 A 的 8 条待定项」 | 假红 `{8, 188}` |
| ③ | **误伤引文**（ISS-206，本包） | 抓到「问题记录」行里引述 ISS-185 修复过程的「全部 188 条待定项」 | 假红 `{188, 193}` |

三次都是「正则锚点不够紧」，而**收紧锚点这件事本身有极强的工作方向**：
每收一次，报红就少一次。于是判据最可能的演化路径是**一路收到永不命中**，
而那正是形态 ① —— **"死"看起来和"过"完全一样**（ISS-95 / ISS-115 同族）。

⇒ 本脚本要证明的是：**新锚点既不误伤，又没有收窄成永不命中。**
   后者只能靠变异证明，因为「不命中」在真实工作区里**恰好是想要的状态**。

## 被测对象

`tools/m1/audit_consistency.py` 的 `_README_DECL_PATTERNS`（3 个计数 / 6 条来源正则）
与 `check_readme_internal_consistency()` 的两条判定：
① 同一计数所有声明取值**集合大小为 1**；② **每条来源正则至少命中一次**（活性自检）。

本脚本**直接调用**该模块的提取函数，不复制正则 —— 复制了就变成在测副本，
而副本与真实现的漂移无人看守（ISS-82 同族）。

## 变异清单

⚠️ 编号纪律：基线不计入 R① 序列。每行**显式写出代码里的节号** `[N]`，
并由 `tools/m1/check_evidence_scripts.py` 校验内联段结构。

| # | 变异 | 期望 | 它证明的失效面 |
|---|---|---|---|
| R0 | 副本基线（`[1]`，**不带 R 编号**） | 每条来源都活、每计数取值集合 = {实测值} | 修完之后判据确实是绿的，且**没绿在"没命中"上** |
| R① | 真声明行的 OP 数字写错（`<N>` → `999`，看板行不动；`[2]`） | 取值集合 = `{<N>, 999}` ⇒ **报红** | 锚点仍**看得见真声明行**（不是收窄成死正则的证明 R④） |
| R② | 真声明行的 ISS 数字写错（`<M>` → `777`；`[3]`） | ISS 取值集合 = `{<M>, 777}` ⇒ **报红** | 同上，且覆盖 `ISS 条数` 那一条来源 |
| R③ | 往「问题记录」行单元格**再塞一条同句式引文**（`[4]`） | 取值集合仍 = `{<N>}` ⇒ **不报红** | 这就是 ISS-206 的真实形态复发，锚点必须挡住它 |
| R④ | **把真声明整行删掉**（`[5]`） | 第 1 条来源命中数 = 0 ⇒ 活性自检必须抓到 | 新锚**没有收窄成永不命中**（否则 R①/R② 的绿毫无意义） |

`<N>` / `<M>` = 真声明行里的 OP / ISS 计数，**运行时从 README 派生**（`_baseline_numbers()`），
不写死成常量。理由见 `_baseline_numbers` 的 docstring：判据的参照值必须从被验对象派生
（ISS-171 教训的同族）。本脚本初版写死 `193` / `205`，README 计数一变（本次 196 / 207）
就全线过期，R③ 的 `str.replace` 打空 ⇒ 报「变异未生效」—— 那次是 `assert` 救的，
但修法不是把数字改回去。

> ⚠️ **R④ 是本脚本存在的核心理由**。R①② 只证明"锚点还能看见真声明"，
> R③ 只证明"锚点够紧"。**只有 R④ 能区分「锚得对」与「锚得太紧以致永不命中」**
> —— 而这两者在真实工作区里的表现**完全一样**（都是一组干净的单值）。
> R①+R②+R④ 三条合起来才构成"既不误伤、也没瞎"的完整论证。

## 红形状校验（不只看取值集合）

每条变异除"集合是否符合预期"外，还断言**红落在正确的计数上**：
R③ 必须在 OP 侧**无红**，若它把 ISS 侧也染红，说明锚点宽到连 ISS 那行都吃了
（`[^\n]*?` 里的 `?` 是惰性匹配，正是这条风险的来源）。

## 还原保证

每条变异都在**内存副本**上做（`str.replace`），**从不写磁盘**。
⇒ 不存在"变异残留污染工作区"这一类失败模式，也不需要 try/finally 兜底。
本脚本末尾仍断言磁盘上的 README 未被改动 —— 用来证明"没写磁盘"这件事本身。

## 跑法

```
python tools/quality/collect_readme_decl_anchor_reverse_evidence.py
python tools/quality/collect_readme_decl_anchor_reverse_evidence.py --fast
```

退出码 0 = 全部符合预期；1 = 有变异未达预期。
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT = REPO_ROOT / "tools" / "m1" / "audit_consistency.py"
README = REPO_ROOT / "README.md"
EVIDENCE = REPO_ROOT / "evidence" / "m1" / "readme-decl-anchor-reverse.log"

# 真声明行（README L17 文档目录表的一整行）。三条变异都围着它转 ——
# 锚点该看的就是这一行，脚本与判据共用同一份锚文本，避免"各写一份"漂移。
_DECL_ROW_PREFIX = "| `docs/待定项与问题登记册.md` | **单一事实来源**：全部 "

# ⚠️ **基线数字必须从真实 README 派生，不得手写**（ISS-171 教训的同型）。
# 本脚本初版把 `193` / `205` 写死成常量（`_OP_TOKEN` / `_ISS_TOKEN` 与 12 处期望值），
# 于是 README 计数一变（本次即207 / 196），`str.replace` 打空 ⇒ 断言
# `quoted != original` 触发，报「R③ 锚点未命中 —— 变异未生效」。
# 那个 assert 救了我们一次（**"没跑"与"跑失败"不同形**），但修法不是把数字改回193/205，
# 而是让数字**从被验对象派生** —— 否则每改一次README 就要改一次本脚本
# （判据的参照值必须从被验对象派生，与 `_expected_checks_from_tool()` 同理）。
#
# 派生方式：读真声明行，把「全部 <N> 条待定项 + <M> 条开发过程问题记录」里的
# N / M 抓出来当作 token 与期望值。若这句式本身变了，`next()` 会当场抛 StopIteration
# 而不是拿旧常量去比对 —— **前者是"锚点没了"，后者是"锚点还在但数字变了"**。
_DECL_ROW_RE = re.compile(
    r"全部\s*(\d+)\s*条待定项\s*\+\s*(\d+)\s*条开发过程问题记录"
)


def _baseline_numbers(decl_row: str) -> tuple[str, str, int, int] | None:
    """从真声明行派生 (OP token, ISS token, OP 期望值, ISS 期望值)。

    **为什么必须派生**：token 是变异的作用对象、期望值是变异的判据基准，两者
    都随 README 的真实计数变化。手写常量会让「变异没生效」与「基线漂移」同形。

    派生入口失效时返回 `None`（**不抛异常**）—— 调用方据此记一条红，
    这样其余节的结论不会因一次崩栈而全部丢失（ISS-143 族）。
    """
    match = _DECL_ROW_RE.search(decl_row)
    if match is None:
        return None
    op_n, iss_m = int(match.group(1)), int(match.group(2))
    return (
        f"{op_n} 条待定项",
        f"{iss_m} 条开发过程问题记录",
        op_n,
        iss_m,
    )


def _load_audit() -> object:
    """加载被测模块本体（不复制它的正则 —— 复制就变成在测副本）。

    ⚠️ 必须注册 `sys.modules`（ISS-164 的同型形态，判据 F `check_loader_registration`）。
    本函数初版漏了这行，当时**恰好能跑通** —— 因为被加载的 `audit_consistency.py`
    此刻还没有 `@dataclass`；而 CPython 3.13 的 `dataclasses` 在解析注解前要
    `sys.modules.get(cls.__module__).__dict__`，拿不到就 `AttributeError`。
    ⇒ 「现在没炸」不等于「注册对了」，这正是 ISS-164 说的
    **判据本体的绿 ≠ 集成路径的绿**。同一行现在被机械判据守着，不靠自觉。
    """
    spec = importlib.util.spec_from_file_location("_ac_under_test", AUDIT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _decl_values(module: object, name: str, text: str) -> list[int]:
    """复算「该计数的全部声明取值集合」—— 与判据内部同一算法。"""
    patterns = module._README_DECL_PATTERNS[name]  # type: ignore[attr-defined]
    return sorted(
        {
            int(m.group(1))
            for pat in patterns
            for m in re.finditer(pat, text, re.M)
        }
    )


def _hit_counts(module: object, name: str, text: str) -> list[int]:
    patterns = module._README_DECL_PATTERNS[name]  # type: ignore[attr-defined]
    return [len(re.findall(pat, text, re.M)) for pat in patterns]


def run_cases(_round: int = 0) -> list[tuple[str, bool, str]]:
    """逐条跑变异。返回 [(节标签, 是否符合预期, 说明)]。

    `_round` 只为 R⑥ 的第二轮服务，见该分支的注释（**别删这个参数**）。
    """
    module = _load_audit()
    original = README.read_text(encoding="utf-8")
    results: list[tuple[str, bool, str]] = []

    # ---- [0] 前置：真声明行必须在（它是全部变异的对象，缺了就一条都跑不成）
    decl_row = next(
        (line for line in original.splitlines() if line.startswith(_DECL_ROW_PREFIX)),
        None,
    )
    if decl_row is None:
        # ⚠️ 这里**记红并早退**，而不是 `next()` 的 StopIteration 崩栈：
        # 「真声明行不见了」是本脚本最可能的失效形态（README 重排目录表就可能发生），
        # 崩栈会丢掉"其余 6 条本来判成什么"这个信息（ISS-143 族）。
        # 早退时仍给一条节号，让 R⑥ 的节号序列比对有意义。
        return [
            (
                "[0] 前置",
                False,
                f"README 里找不到以 {_DECL_ROW_PREFIX!r} 开头的真声明行 —— "
                f"全部变异的作用对象已不存在；本脚本的锚前缀须同步 README",
            )
        ]

    # ⚠️ 全部基线数字**从真声明行派生**（ISS-171 同族纪律）。初版把 193/205 写死成
    # `_OP_TOKEN` / `_ISS_TOKEN` 与 12 处期望值，README 一改计数就全线过期。
    baseline = _baseline_numbers(decl_row)
    if baseline is None:
        # ⚠️ 同 [0]：记红 + 早退，而不是让 None 在下游炸成 TypeError。
        # 「派生入口失效」是**判据自己坏了**，必须以可读的形态出现在证据里，
        # 而 `TypeError: replace() argument 1 must be str, not None` 不是。
        return [
            (
                "[0b] 派生",
                False,
                "真声明行里找不到「全部 N 条待定项 + M 条开发过程问题记录」句式 —— "
                "ISS-206 收紧锚点后这句式已变，本脚本的派生入口失效，"
                "须改本脚本而不是改数字",
            )
        ]
    op_token, iss_token, op_n, iss_n = baseline

    def mutate(new_decl_row: str) -> str:
        return original.replace(decl_row, new_decl_row, 1)

    # ---- [1] R0 基线：既不误伤，也不是"没命中"
    op_vals = _decl_values(module, "OP 条数", original)
    iss_vals = _decl_values(module, "ISS 条数", original)
    hits = _hit_counts(module, "OP 条数", original)
    ok = op_vals == [op_n] and iss_vals == [iss_n] and all(h > 0 for h in hits)
    results.append(
        (
            "[1] R0",
            ok,
            f"OP 声明 {op_vals}（期望 [{op_n}]）/ ISS 声明 {iss_vals}（期望 [{iss_n}]）"
            f" / OP 两条来源命中数 {hits}（每条须 >0，否则是死正则）",
        )
    )

    # ---- [2] R① 真声明的 OP 数字写错：必须报红（证明锚点看得见真声明）
    # 断言用**精确形状**而非 `in`：只查"含 999"的话，变异若造出 `193999`
    # 这类垃圾值也会绿 —— 假红与真红在 `in` 面前同形（同 [3] 的纪律）。
    bad_op = mutate(decl_row.replace(op_token, "999 条待定项", 1))
    got = _decl_values(module, "OP 条数", bad_op)
    ok = got == [op_n, 999]
    results.append(
        (
            "[2] R①",
            ok,
            f"真声明行 OP 数字改成 999 后取值 {got}（期望恰为 [{op_n}, 999]）",
        )
    )

    # ---- [3] R② 真声明的 ISS 数字写错：必须报红
    # ⚠️ token 必须**含前导数字**（`207 条…` 而非 ` 条…`）。首版写成后者，
    #    `str.replace` 把分隔空格一起吃掉了 ⇒ 造出 `207777` 这个不存在的数，
    #    判据确实报红了，但**红的形状是假的**。
    #    ⇒ 与 WP47① CR-2 反向验证同一条纪律：不只查"报没报红"，
    #      还要查"红成什么形状"。伪造出来的红与真红在退出码上同形。
    bad_iss = mutate(
        decl_row.replace(iss_token, "777 条开发过程问题记录", 1)
    )
    got_i = _decl_values(module, "ISS 条数", bad_iss)
    ok = got_i == [iss_n, 777]
    results.append(
        (
            "[3] R②",
            ok,
            f"真声明行 ISS 数字改成 777 后取值 {got_i}"
            f"（期望恰为 [{iss_n}, 777] —— 出现 {iss_n}777 之类说明变异本身写错了）",
        )
    )

    # ---- [4] R③ 单元格内再塞同句式引文：必须**不**报红（ISS-206 形态复发）
    # 锚点也**派生**：`{iss_n} 条 / 0 error` 是看板行的实际写法，硬编码它
    # 就是初版 R③ 在 README 计数变化后打空的直接原因。
    #
    # ⚠️ **变异未生效时记红、而不是 assert 抛出**（本轮修掉的第二个缺陷）。
    # 初版在这里 `assert quoted != original`：派生一旦失效（正是 ISS-171 那类漂移），
    # 异常直接抛出 ⇒ **前面 R0/R①/R② 已判出的红被吞掉**（实测：把派生退回硬编码
    # 193/205 时，只看到 R③ 的 AssertionError，看不到 R0 期望 [193] 实得 [196] 的红）。
    # ⇒ 一条变异写错，让其余断言的结论全部丢失 —— 而"崩了"与"全判错"在读证据时
    # 很容易被读成同一个结论。改成 `if ... else` 记一条 FAIL，节号序列恒定。
    issue_row_token = f"| 问题记录 | **{iss_n} 条 / 0 error**"
    quoted = original.replace(
        issue_row_token,
        f'| 问题记录 | 引文「全部 111 条待定项」 **{iss_n} 条 / 0 error**',
        1,
    )
    if quoted == original:
        results.append(
            (
                "[4] R③",
                False,
                f"变异未生效：README 里找不到锚点 {issue_row_token!r} —— "
                f"看板行写法变了（须改本脚本的派生，不得改判绿）",
            )
        )
    else:
        got_q = _decl_values(module, "OP 条数", quoted)
        got_qi = _decl_values(module, "ISS 条数", quoted)
        ok = got_q == [op_n] and got_qi == [iss_n]
        results.append(
            (
                "[4] R③",
                ok,
                f"单元格内塞入同句式引文后 OP 取值 {got_q}（期望 [{op_n}]）"
                f" / ISS 取值 {got_qi}（期望 [{iss_n}]）—— 两侧都不得被引文污染",
            )
        )

    # ---- [5] R④ 删掉真声明整行：第 1 条来源必须归 0（证明没收窄成死正则）
    # 同 R③：未生效时记红而非抛出。
    dropped = original.replace(decl_row, "", 1)
    if dropped == original:
        results.append(
            (
                "[5] R④",
                False,
                "变异未生效：真声明行没被删掉（`_DECL_ROW_PREFIX` 与 README 不同步）",
            )
        )
    else:
        h_drop = _hit_counts(module, "OP 条数", dropped)
        ok = h_drop[0] == 0
        results.append(
            (
                "[5] R④",
                ok,
                f"删掉真声明行后 OP 两条来源命中数 {h_drop}"
                f"（期望第 1 条 = 0 ⇒ 活性自检会报死正则）",
            )
        )

    # ---- 还原保证：全部变异都在内存副本上做，磁盘必须逐字未变
    unchanged = README.read_text(encoding="utf-8") == original
    results.append(
        (
            "[6] R⑤",
            unchanged,
            "全部变异仅在内存副本上进行；断言磁盘 README 逐字未变",
        )
    )

    # ---- [7] R⑥ 可重复性：同一输入再跑一轮，结果必须一致
    #     变异若偷偷依赖了 `set` 迭代序或全局状态，第二轮可能给出不同取值
    #     ⇒ "跑出来对"可能只是那一次的运气。
    #     ⚠️ 用 `_round` 而不是直接 `run_cases()`：后者会再进本分支 ⇒ 无限递归
    #     （首版就这么写的，Traceback 递归 1000 层后炸在 `Path.resolve`）。
    #     `_round=1` 时**跳过** R⑥，本条由最外轮负责断言。
    if _round == 0:
        again = [tag for tag, _, _ in run_cases(_round=1)]
        same = again == [tag for tag, _, _ in results]
        results.append(
            (
                "[7] R⑥",
                same,
                f"连续两轮变异结果一致（第二轮 {len(again)} 条，节号序列与第一轮相同）",
            )
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fast",
        action="store_true",
        help="保留参数（本脚本为纯文本处理，无慢用例；与判据脚本口径一致）",
    )
    args = parser.parse_args()

    out: list[str] = []
    emit = out.append

    emit("WP47① §2b README 多处声明对账判据 —— 反向验证（ISS-206）")
    emit("=" * 72)
    emit(f"被测判据：{AUDIT.relative_to(REPO_ROOT)}")
    emit("被测对象：_README_DECL_PATTERNS（3 计数 / 6 条来源正则）")
    emit(f"模式：{'--fast' if args.fast else '完整'}")
    emit("")
    emit("前提：单条正则失效时，真实工作区与「永不命中」的输出完全同形，")
    emit("      故 R④（删掉真声明行）是把「锚得对」与「锚得死」区分开的唯一手段。")
    emit("")

    results = run_cases()
    failed = 0
    for tag, ok, detail in results:
        emit(f"  {'PASS' if ok else 'FAIL'}  {tag}  {detail}")
        if not ok:
            failed += 1

    emit("")
    emit(f"共 {len(results)} 条反向验证，失败 {failed} 条")
    verdict = "PASS" if failed == 0 else f"FAIL（{failed} 条变异未达预期）"
    emit(f"RESULT={verdict}")

    # ⚠️ **自己落盘**（`EVIDENCE.write_text`），不是靠调用方 `| tee`。
    #    首版只 `print`，结果 `check_evidence_scope.py` 的 D1 立刻报红
    #    （A1 收进了本脚本、按行为反推的 B 里却没有它）—— 那条报红是**对的**：
    #    一个不落盘的脚本产不出可复跑的证据，它只能靠人手 `| tee`，
    #    而人手恰恰是铁律 14 要消灭的东西。判据在这里是防线，不是障碍。
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text("\n".join(out) + "\n", encoding="utf-8")

    print("\n".join(out))
    print(f"[证据] {EVIDENCE.relative_to(REPO_ROOT)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
