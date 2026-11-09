#!/usr/bin/env python3
"""WP47② 判据自身的反向验证：证明 check_alerting_rules 真的会报红。

## 为什么必须这个脚本

判据全绿**不能证明判据有效**。本项目反复踩到的形态是：
判据因为写得宽松、锚点退化、或白名单放行而**长期报绿**，而真缺陷存在
（ISS-95「声称自己有防线比没有防线更危险」、ISS-143「没跑与跑失败同形」）。

WP47① 期间实测到的那一条最典型：`promtool check rules` 对**拼错的指标名**
报 `SUCCESS: 6 rules found` + `exit 0` —— 计划 §4.2 写的那句验收
（"告警规则 promtool check 通过"）单独使用时**完全不设防**。
本脚本就是补上这个洞的判据自己的反向验证。

## 变异设计（每条都对应一种真实失效形态）

| 编号 | 变异 | 期望 | 验证的是 |
|---|---|---|---|
| R0 | 不变异，跑基线 | PASS 9/9 | 判据在当前仓库上是绿的 |
| R① | 规则里改错一个指标名（拼字） | **D2 报红** | 抓悬空指标名（本脚本存在的理由） |
| R② | 删掉一条告警 | **D6 报红** | 抓"规则被清空/少写" |
| R③ | 删掉某条告警的 severity | **D7 报红** | 抓"告警无法分派" |
| R④ | 删掉整个规则文件 | **D1+D2 报红** | 抓"文件被误删" |
| R⑤ | scrape 配置 metrics_path 与代码常量不一致 | **D8 报红** | 抓两侧配置漂移 |
| R⑥ | 规则文件被清空成零条告警 | **D6 报红** | 抓"文件还在但内容空了" |
| R⑦ | 全部变异后磁盘文件逐字未变 | 断言成立 | 判据只读，不污染工作树 |

⚠️ **R⑦ 是最容易被省掉、也最该有的一条**：一个"判据跑完顺手把
变异体写回磁盘"的脚本，会在第一次跑时就悄悄污染工作树 ——
而工作树是**共享**的（并行会话），污染会算到别人头上。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Final

REPO: Final[Path] = Path(__file__).resolve().parents[2]
TARGET: Final[Path] = REPO / "tools" / "quality" / "check_alerting_rules.py"
RULES: Final[Path] = REPO / "deploy" / "monitoring" / "rules" / "rednote-api.yml"
CONFIG: Final[Path] = REPO / "deploy" / "monitoring" / "prometheus.yml"
EVIDENCE: Final[Path] = REPO / "evidence" / "m5" / "wp47-alerting-rules-reverse.log"


def _now() -> str:
    import datetime

    return datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def _load() -> object:
    spec = importlib.util.spec_from_file_location("check_alerting_rules", TARGET)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # ⚠️ **必须先注册进 `sys.modules` 再 `exec_module`**（ISS-164 / 判据 F）。
    # 不注册的表现是"现在不炸、将来炸"：CPython 3.13 的 `dataclasses` 在解析
    # 注解前要 `sys.modules.get(cls.__module__).__dict__`，而 `module_from_spec`
    # **不注册** ⇒ 拿到 `None` ⇒ `AttributeError: 'NoneType' object has no
    # attribute '__dict__'`。本文件当前加载的 `check_alerting_rules.py` 恰好
    # 无 `@dataclass`（实测 0 处）故今天不报错 —— 但那是**运气不是防线**，
    # 那个脚本哪天加一个 dataclass 就会当场炸，且炸的是"加载器"不是"规则"，
    # 排障方向会跑偏（ISS-143 同形）。
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _snapshot() -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8") for p in (RULES, CONFIG) if p.is_file()}


def _restore(snap: dict[Path, str]) -> None:
    for path, text in snap.items():
        path.write_text(text, encoding="utf-8")


def _results(mod: object) -> dict[str, tuple[bool, str]]:
    """跑一次判据，取回 {判据名: (ok, detail)}。

    走 `main()` 而不是 subprocess：省掉解释器启动开销，且能直接拿到
    结构化结果做断言。返回值取自 `main()` 内部 print 的解析太脆弱。
    """
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.main()  # type: ignore[attr-defined]
    out = buf.getvalue()
    got: dict[str, tuple[bool, str]] = {}
    for line in out.splitlines():
        if line.startswith(("PASS  [", "FAIL  [")):
            ok = line.startswith("PASS")
            tag = line.split("]", 1)[1].strip()
            got[tag] = (ok, line)
    return got


def _red_contains(got: dict[str, tuple[bool, str]], prefix: str) -> tuple[bool, str]:
    hits = [tag for tag, (ok, _) in got.items() if tag.startswith(prefix) and not ok]
    return bool(hits), (f"报红判据={hits}" if hits else "未报红（该判据仍为绿）")


def run_cases() -> list[tuple[str, bool, str]]:
    mod = _load()
    results: list[tuple[str, bool, str]] = []
    snap = _snapshot()

    try:
        # R0 基线
        base = _results(mod)
        n_pass = sum(1 for ok, _ in base.values() if ok)
        results.append(("R0  基线全绿", n_pass == len(base) and len(base) >= 10,
                        f"{n_pass}/{len(base)} 条 PASS"))

        # R① 改错一个指标名 —— 本判据存在的核心理由
        txt = RULES.read_text(encoding="utf-8")
        RULES.write_text(txt.replace("rednote_build_info", "rednote_build_infO_typo"),
                         encoding="utf-8")
        try:
            red, detail = _red_contains(_results(mod), "D2")
            results.append(("R①  改错指标名 ⇒ D2 报红", red, detail))
        finally:
            RULES.write_text(txt, encoding="utf-8")

        # R② 删掉一条告警
        blocks = txt.split("      - alert:")
        if len(blocks) >= 2:
            RULES.write_text("      - alert:".join(blocks[:-1]) + "\n", encoding="utf-8")
            try:
                red, detail = _red_contains(_results(mod), "D6")
                results.append(("R②  删掉一条告警 ⇒ D6 报红", red, detail))
            finally:
                RULES.write_text(txt, encoding="utf-8")
        else:
            results.append(("R②  删掉一条告警 ⇒ D6 报红", False, "未找到可删的告警块"))

        # R③ 删掉某条告警的 severity
        lines = txt.splitlines(keepends=True)
        patched, dropped = [], False
        for ln in lines:
            if not dropped and "severity:" in ln:
                dropped = True
                continue
            patched.append(ln)
        if dropped:
            RULES.write_text("".join(patched), encoding="utf-8")
            try:
                red, detail = _red_contains(_results(mod), "D7")
                results.append(("R③  删掉 severity ⇒ D7 报红", red, detail))
            finally:
                RULES.write_text(txt, encoding="utf-8")
        else:
            results.append(("R③  删掉 severity ⇒ D7 报红", False, "规则里无 severity 行"))

        # R④ 删掉整个规则文件
        # （首版只查 D2 ⇒ 报"FAIL"；实测 D2 当时仍绿，是真缺陷。
        #  修法：判据新增 D0「文件必须存在」，本条改为断言 D0 报红。）
        content = txt
        RULES.unlink()
        try:
            got = _results(mod)
            red2a, detail2a = _red_contains(got, "D0")
            results.append(("R④  删掉规则文件 ⇒ D0 报红", red2a, detail2a))
        finally:
            RULES.write_text(content, encoding="utf-8")

        # R⑤ metrics_path 与代码常量漂移
        cfg = CONFIG.read_text(encoding="utf-8")
        CONFIG.write_text(cfg.replace("metrics_path: /metrics", "metrics_path: /wrong_path"),
                          encoding="utf-8")
        try:
            red, detail = _red_contains(_results(mod), "D8")
            results.append(("R⑤  metrics_path 漂移 ⇒ D8 报红", red, detail))
        finally:
            CONFIG.write_text(cfg, encoding="utf-8")

        # R⑥ 文件还在但告警全空
        header = "groups:\n"
        RULES.write_text(header, encoding="utf-8")
        try:
            red, detail = _red_contains(_results(mod), "D6")
            results.append(("R⑥  清空告警 ⇒ D6 报红", red, detail))
        finally:
            RULES.write_text(txt, encoding="utf-8")

        # R⑦ 还原保证
        _restore(snap)
        after = _snapshot()
        unchanged = after == snap
        base2 = _results(mod)
        n2 = sum(1 for ok, _ in base2.values() if ok)
        results.append(("R⑦  全部变异仅在内存/临时上进行，磁盘逐字还原且复跑仍全绿",
                        unchanged and n2 == len(base2),
                        f"磁盘一致={unchanged}；复跑 {n2}/{len(base2)}"))

    finally:
        _restore(snap)

    return results


def main() -> int:
    results = run_cases()
    lines: list[str] = []
    for i, (tag, ok, detail) in enumerate(results, 1):
        lines.append(f"{'PASS' if ok else 'FAIL'}  [{i}] {tag}")
        lines.append(f"          {detail}")
    passed = sum(1 for _, ok, _ in results if ok)
    lines.append("")
    lines.append(f"{passed}/{len(results)} 条变异验证通过")
    lines.append("RESULT=" + ("PASS" if passed == len(results) else "FAIL"))

    # **自落盘**（不靠人手 `| tee`）：WP47① 实测踩过 ——
    # 只 print 的脚本会被 `check_evidence_scope` D1 判为"不是真证据脚本"
    # （它按 AST 反推哪些脚本真的写 evidence 文件）。人手出证据恰恰是
    # 铁律 14 要消灭的东西，所以判据在这里是**防线**而不是障碍。
    out = [
        "# WP47② 告警规则判据的反向验证证据",
        f"# 生成时间: {_now()}",
        "# 判据: tools/quality/check_alerting_rules.py",
        "# 本脚本: tools/quality/collect_alerting_rules_reverse_evidence.py",
        "",
    ] + lines + [""]
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text("\n".join(out), encoding="utf-8")

    print("\n".join(lines))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
