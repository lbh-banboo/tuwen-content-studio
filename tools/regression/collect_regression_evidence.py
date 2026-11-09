#!/usr/bin/env python
"""回归护栏证据采集脚本。

产出的文件：`evidence/m1/regression-guardrails.log`

**为什么回归这件事必须有证据**：标记体系、CI 三道门、登记册三样东西的
共同点是 —— **它们失效时不会报错**：

  · 标记全部漏写 → pytest 照常全绿，只是"回归子集"变成了全集（或空集）；
  · CI 某个 job 被删 → 本地一切正常，只有合并那天才发现没人拦；
  · 登记册与实际标记漂移 → 两边都是文本，谁也不会自己喊。

因此这里不跑"回归用例本身"（那是 CI 门 3 的事），只采集
**护栏自身的活性证据**：标记注册了、落点了、可选了、登记册对得上、
CI 三道门都在。

设计约定（与 `tools/m1/collect_wp*_evidence.py` 一致）：
  - 一律用绝对路径；
  - 记录子进程退出码，不只记录输出；
  - **判据只写"我的这些都在"与"下界"，不写"全项目恰好 N 个"**（本仓铁律）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
#: 本脚本自身路径。**必须与 `REPO_ROOT` 同源（都 `resolve()`）** ——
#: macOS 上 `/var` 是 `/private/var` 的符号链接：`REPO_ROOT` 用 `resolve()`
#: 得到 `/private/var/...`，而裸 `__file__` 在临时目录下是 `/var/...`，
#: 两边 `relative_to` 会抛 `ValueError`，**脚本整体崩溃、一条 CHECK 都不产生**
#: —— 与"全部通过"在计数上完全同形（本仓 ISS-43 的形态）。
#: 实测：在 `/var/folders/...` 临时副本里调用必然触发。
SELF_PATH = Path(__file__).resolve()
BACKEND_DIR = REPO_ROOT / "services" / "backend"
TESTS_DIR = BACKEND_DIR / "tests"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m1"
PYPROJECT = BACKEND_DIR / "pyproject.toml"
CI_FILE = REPO_ROOT / ".github" / "workflows" / "ci.yml"
OUT_FILE = EVIDENCE_DIR / "regression-guardrails.log"

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

#: `pyproject.toml` 里必须注册的标记。少一个，`-m <name>` 会以
#: "no tests ran"（退出码 5）或静默全选的方式失效，而不是报错。
REQUIRED_MARKERS = ("regression", "cross_wp", "slow", "smoke")

#: CI 必须存在的三道门（job id）与每道门必须有的关键步骤片段。
#: 用"job id"而不是"job 名"：名字可以随便改，id 决定依赖关系。
#: 其中把本脚本自己也列进来 —— 步骤被删掉时，本地收口这一跑就会红。
#:
#: ⚠️ **这份名单本身有守门人**（`ci-steps-covered`）：
#: 只看这份名单是"靠记性维护覆盖范围"的病 —— 往 `ci.yml` 加了新步骤、
#: 忘了在这里登记，名单与实际的差异**没有任何判据**能发现。
#: 因此 §5 里额外断言"`ci.yml` 里跑的所有 `python tools/**` 步骤
#: 都在本名单中出现过"（见 `_ci_tool_steps`）。下面那批
#: `tools/quality/*` 就是以这种方式被强制同步进来的 ——
#: WP-DEBT-01（依赖声明）与 WP-DEBT-02（测试库可信度）往门 3 加了 8 个步骤，
#: 当时 `ci-steps-covered` 当场报红并逐条点名，故补登记（这正是它存在的理由：
#: **"加了脚本忘了登记"必须有一条判据能报红**，而不是靠人记得回来改）。
#:
#: ⚠️ 2026-09-30 本仓第 7 次同族复发：收口第 11 步复跑时
#: `ci-steps-covered` 报"未登记 8 个"—— 说明这条判据**在工作**，
#: 而且它抓到的是自己（名单与磁盘脱钩），不是别人。
REQUIRED_CI_JOBS = ("smoke", "full", "guard")
REQUIRED_CI_STEPS = (
    "alembic upgrade head",
    "export_openapi.py --check",
    "tools/contracts/export_openapi.py",
    "tools/regression/check_register.py",
    "tools/m1/check_evidence_scripts.py",
    "tools/quality/check_test_db_isolation.py",
    "tools/m1/audit_consistency.py",
    "tools/regression/collect_regression_evidence.py",
    "tools/regression/collect_regression_reverse_evidence.py",
    "tools/m1/collect_schema_parity_reverse_evidence.py",
    # --- WP-DEBT-01：依赖声明一致性（补登记，见上注）---
    "tools/quality/check_dependency_declarations.py",
    "tools/quality/collect_dependency_declaration_reverse_evidence.py",
    "tools/quality/collect_dependency_declaration_code_review.py",
    # --- WP-DEBT-02：测试库可信度三条判据 + 反向验证 + 三遍法 ---
    "tools/quality/check_test_database_target.py",
    "tools/quality/check_migration_model_parity.py",
    # --- WP-DEBT-04：ISS-79 往返迁移判据（回滚路径此前无任何机械判据）---
    # ⚠️ 这是本仓第 9 次同族复发：加新步骤的动作发生在"零件"侧（ci.yml），
    #    而"名单"侧（本元组）没人提醒 —— 与铁律 14 同病。加它的同时
    #    `ci-steps-covered`（§5）会双向对账，忘了改这里它会当场点名。
    "tools/quality/check_migration_roundtrip.py",
    "tools/quality/check_test_residue.py",
    # --- WP47②：告警规则判据（ADR-11 C6，计划 §4.2 验收项）---
    # ⚠️ 本仓第 16 次同族复发：往 ci.yml 加判据的动作在"零件"侧，
    # 而这份人写的名单没人提醒。`ci-steps-covered`（§5）会双向对账并当场点名。
    # ⚠️ 登记它的**实质理由**（不是"凑数"）：计划 §4.2 写的验收是
    # 「告警规则 promtool check 通过」，而本机实测 promtool 对**拼错的指标名**
    # 同样报 SUCCESS + exit 0 ⇒ 那条验收单独使用**不设防**。
    # 这两条（判据 + 它的反向验证）才是真正补上那个洞的载体。
    "tools/quality/check_alerting_rules.py",
    "tools/quality/collect_alerting_rules_reverse_evidence.py",
    "tools/quality/collect_test_db_credibility_reverse_evidence.py",
    "tools/quality/collect_test_db_credibility_code_review.py",
    # --- WP-DEBT-03：纪律判据化（判据 D/F + 反向验证 + 三遍法）---
    # ⚠️ **本批是收口第 11 步第一次真正跑通时才补上的**（ISS-164 修掉之前该步从未跑成）。
    # `ci-steps-covered` 当场报「未登记 4 个」并逐条点名 —— 这正是它存在的理由：
    # 「往 CI 加了脚本、忘了在这里登记」必须有一条判据能报红，而不是靠人记得回来改。
    # 这是本仓第 8 次同族复发，且与 §5.3c 同源：**加新步骤的动作发生在"零件"侧，
    # 而"名单"侧（人写的 REQUIRED_CI_STEPS）没人提醒** —— 与铁律 14 同病。
    #
    # ⚠️ 判据 E（`check_shared_db_quiet.py`）**刻意不在本名单里** ——
    # 它不进 CI（`--ci` 显式 SKIP，runner 独占库时"有没有别人用"无区分度），
    # 故它不属于"CI 实际跑的脚本"。它的载体是收口期 `rerun_all_evidence.py`。
    "tools/quality/check_evidence_scope.py",
    "tools/quality/check_loader_registration.py",
    "tools/quality/collect_discipline_reverse_evidence.py",
    "tools/quality/collect_discipline_code_review.py",
    # --- WP47：备份恢复演练（只有脚本没人真恢复 = "没跑"，ISS-143）---
    # ⚠️ 第 10 次同族：加步骤在 ci.yml 侧、名单在本元组侧，靠 `ci-steps-covered`
    #    双向对账才不脱钩。本次同样"先改清单再跑判据"。
    "tools/quality/check_backup_restore_drill.py",
    # --- WP47④：队列对账判据的反向验证（异常矩阵第 ⑭ 项"备份恢复对账尚未测"）---
    # ⚠️ 本仓第 17 次同族。加它不是为了"多一道门"，而是因为队列对账那 6 条
    #    **有过一次真实假绿**：2026-10-07 首跑只造 users / version_nodes 两张表，
    #    四张队列表全 0 行 ⇒ `0 == 0` 恒真 ⇒ 备份把队列内容全丢了照样报绿。
    #    判据在自己的队列表上犯了它 docstring 里写下的 ISS-115。
    #    ⇒ 必须有"逐个注入破坏、要求报红"的载体，否则半年后又是一次静默恒真。
    #    与 `check_backup_restore_drill.py` 的分工：那个证"恢复出来的库逐列相等"，
    #    这个证"逐列相等这条判据自己还会不会失效"。
    "tools/quality/collect_queue_reconcile_reverse_evidence.py",
    # --- WP47 ⑤⑥：回滚演练（升级失败必须能回退到已知良好版本）---
    # ⚠️ 第 11 次同族。与上面两个 drill 一样，**只挂 CI 门 3、刻意不进
    #    `rerun_all_evidence.py::SCOPE_PATTERNS`**：收口第 11 步的定位是"复跑
    #    历史包的证据再生成"，而演练的载体是 CI 门 3（每次 push 都真跑一遍）。
    #    该分工的判据是 ISS-193 记录的"check_evidence_scope 的 B 集合看不见
    #    「函数局部变量」形态的证据路径" ⇒ D2 不会要求把它登记进 SCOPE_PATTERNS；
    #    它的"忘了登记"由本条 + `ci-steps-covered` 双向对账拦住。
    "tools/quality/check_rollback_drill.py",
    # --- WP26/WP27 侧已落进 ci.yml 门 3 的四条判据 + 四条反向验证（2026-10-08 收口期补登记）---
    # ⚠️ **第 12 次同族，且这一次是跨包的**：这 8 个步骤是 WP26/WP27 收口时加进
    #    `ci.yml` 门 3 的（2026-10-07/08，带完整房规注释，见 ci.yml 第 276~356 行），
    #    但**名单侧（本元组）没同步** ⇒ `ci-steps-covered` 当场报「未登记 8 个」并逐条点名。
    #    这正是它存在的理由：加步骤的动作发生在"零件"侧、名单在"人写"侧，
    #    两者脱钩必须有判据能报红（与铁律 14 同病；ISS-162/ISS-139 已把它判据化）。
    #    ⚠️ 同一批脚本**另一处已被同步**：`rerun_all_evidence.py`（MIN_SCRIPTS 32 /
    #    SCRIPTS 37，即 `check_evidence_scope.py` 输出的 A1=37）—— 唯独本名单漏了。
    #    "两处都该改、只改了一处"正是脱钩最容易发生的样子：改的人记得证据载体、
    #    忘了 CI 登记册，而后者**不会主动报错**，只在下一次收口复跑时被点名。
    "tools/quality/check_all_exports.py",
    "tools/quality/collect_all_exports_reverse_evidence.py",
    "tools/quality/check_node_paired_guard.py",
    "tools/quality/collect_node_paired_guard_reverse_evidence.py",
    "tools/quality/check_media_asset_usability_single_source.py",
    "tools/quality/collect_media_asset_usability_reverse_evidence.py",
    "tools/quality/check_migration_value_domain_no_live_import.py",
    "tools/quality/collect_migration_value_domain_reverse_evidence.py",
    # --- WP47① / ISS-206：审计 §2b「README 多处声明对账」的反向验证 ---
    # ⚠️ 加它不是为了"多一道门"，而是因为 §2b 那条判据的**唯一演化方向
    #    是朝着失效走**：每次假红都诱导人把锚再收一点，而"收成永不命中"的
    #    输出与"判过"完全同形（ISS-95 / ISS-115）。没有 R④（删掉真声明整行
    #    ⇒ 第 1 条来源命中数须归 0）就分辨不出「锚得对」与「锚得死」。
    #    本仓第 10 次同族复发：加 ci.yml 步骤的动作在"零件"侧，名单侧没人提醒。
    "tools/quality/collect_readme_decl_anchor_reverse_evidence.py",
    # --- 审计 §4「文档声明的证据必须存在」的反向验证 ---
    # ⚠️ **本条是 M5 审计 §4 补盲区时就落进 ci.yml 的，却一直漏登记**
    #    （第 15 次同族复发，2026-10-08 WP47① 收口被 `ci-steps-covered` 当场点名）。
    #    漏了很久却一直没红，是因为 `ci-steps-covered` 一直在报红 ——
    #    **但那两条红被当成了"别处的问题"**。这正是"判据报了红而没人认领"的形态：
    #    与"假绿"同源（都是"没人当真"），只是方向相反。
    "tools/quality/collect_declared_evidence_reverse_evidence.py",
    # --- WP-DEBT-04（后半）：跨会话共享文件的写权边界（V2.1 §7.1 未判据化项）---
    # ⚠️ **本仓第 18 次同族复发**：加步骤的动作在"零件"侧（`ci.yml`），
    #    名单侧（本元组）没人提醒 —— 与铁律 14 同病，`ci-steps-covered` 双向对账兜住。
    # ⚠️ 登记它的**实质理由**（不是凑数）：V2.1 §10.3 自己写明"§7.1 的四条纪律
    #    目前全是自觉、没有一条判据 ⇒ 这等价于 ISS-95 形态"，而 M4 并行线开工在即。
    #    判据本体在 CI 上 `--ci` 显式 SKIP（无"提交集"输入 ⇒ 无区分度），
    #    真实载体是收口期提交前步骤；**但"它在 CI 文件里存在"这件事本身要被登记**，
    #    否则读 CI 的人会以为这条纪律没人管（与 `check_shared_db_quiet.py` 同款处置）。
    #    ⚠️ 它的反向验证**必须在 CI 真跑**（不 SKIP）—— 因为"纪律判据从没生效"
    #    与"纪律被遵守"在它自己的 PASS 输出上完全同形（ISS-95 / ISS-143）。
    "tools/quality/check_cross_session_shared_files.py",
    "tools/quality/collect_cross_session_shared_files_reverse_evidence.py",
    # --- WP-DEBT-05：测试库数据体量判据（ISS-223）+ 反向验证 + 三遍法 ---
    # 加步骤的动作在 ci.yml 侧，名单在本元组侧 ——`ci-steps-covered`（§5）会双向对账，
    # 忘了改这里它会当场点名（与铁律 14 同病，本仓已数次复发）。
    "tools/quality/check_test_data_volume.py",
    "tools/quality/collect_test_data_volume_reverse_evidence.py",
    "tools/quality/collect_test_data_volume_code_review.py",
    # --- WP36：下载不计交付 / 替换是追加（AST 判据）---
    # 加步骤的动作在 ci.yml 侧，名单在本元组侧 —— `ci-steps-covered`（§5）会双向
    # 对账，忘了改这里它会当场点名（与铁律 14 同病，本仓已数次复发）。
    "tools/quality/check_download_not_delivery.py",
    # --- WP46：离线安装包构建 + SBOM 覆盖判据 + 反向验证（2026-10-19 登记）---
    # ⚠️ `check_sbom_coverage.py` / `check_offline_bundle.py` 要求真实的
    # wheelhouse/sbom/images.lock（fail-closed，不许当 SKIP），门 3 因此新增了
    # `build_offline_bundle.py` 这一构建步骤（只跑 wheelhouse/repo/sbom/manifest，
    # 不跑 images——理由见 ci.yml 对应注释）。这一个脚本也走 `python tools/...`
    # 调用形态，同样要登记，否则 `ci-steps-covered` 会把它当成"未登记"。
    "tools/ops/build_offline_bundle.py",
    "tools/quality/check_offline_bundle.py",
    "tools/quality/check_sbom_coverage.py",
    "tools/quality/collect_sbom_coverage_reverse_evidence.py",
)


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def _repo_rel(path: Path) -> str:
    """仓库内的展示路径；不在仓库内时退回绝对路径。

    **不抛异常**：这是展示用的字符串，让"打印一行日志"成为脚本崩溃的原因是
    荒谬的。两边都 `resolve()` 再比，躲开 `/var` ↔ `/private/var` 这类符号链接
    造成的"同一文件、两条路径"。
    """
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _run(args: list[str], *, cwd: Path) -> tuple[int, str]:
    """跑子进程并连退出码一起返回。"""
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _marked_functions() -> dict[str, int]:
    """扫描 `tests/`，统计每条标记装饰了几个测试函数。

    **用 `ast` 而不是正则**：正则会把注释里、docstring 里提到的
    `mark.regression` 一起数进去（本仓的判据文档大量引用标记名），
    于是"标记数"会随文档变多而虚高。

    只认形如：

        @pytest.mark.<name>
        def test_*(...)

    的**直接**装饰（`@pytest.mark.x` 与 `@pytest.mark.regression` 叠加时
    两个都算）。模块级 `pytestmark = pytest.mark.smoke` 单独按文件计。
    """
    import ast

    counts: dict[str, int] = {}
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            for deco in node.decorator_list:
                name = _marker_name(deco)
                if name is not None:
                    counts[name] = counts.get(name, 0) + 1
    return counts


def _marker_name(deco: ast.AST) -> str | None:
    """从装饰器节点里取出标记名（`pytest.mark.<name>` / `mark.<name>`）。"""
    import ast

    if not isinstance(deco, ast.Attribute):
        return None
    inner = deco.value
    if not isinstance(inner, ast.Attribute) or inner.attr != "mark":
        return None
    return deco.attr


def _smoke_files() -> list[str]:
    """模块级 `pytestmark = pytest.mark.smoke` 的文件（整文件打标记）。"""
    import ast

    found = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "pytestmark" in targets and _marker_name(node.value) == "smoke":
                found.append(path.name)
                break
    return found


def _ci_run_commands(text: str) -> str:
    """只取 CI 里 `run:` 之后的 shell 命令文本（内联 + 块标量两种写法）。

    **为什么不能全文匹配**：`-q` 这件事的说明文字本身就会写在文件里 ——
    本文件第 8 行那条注释就写着"pytest 不再加 -q"，直接全文搜 `pytest.*-q`
    会把**合规**判成**违规**（假阳性）。同样，步骤 `name:` 里出现 `-q`
    也不是命令。因此必须先把"命令面"切出来，再在命令面上判。
    """
    out: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(r"^(\s*)(?:-\s*)?run:\s*(.*)$", line)
        if match is None:
            index += 1
            continue
        indent = len(match.group(1))
        rest = match.group(2).strip()
        if rest and rest not in ("|", ">"):
            out.append(rest)  # 内联写法：run: python -m pytest ...
            index += 1
            continue
        index += 1  # 块标量：收集比 `run:` 缩进更深的行
        while index < len(lines):
            nxt = lines[index]
            if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                break
            out.append(nxt)
            index += 1
    return "\n".join(out)


#: 与 `addopts = "-q"` 叠加后会变成 `-qq` 的那类选项。
#: 覆盖三种真实写法：`-q`、`-qq`、以及短选项簇里的 `-xq`；`--quiet` 同理。
_EXTRA_Q = re.compile(r"(?:^|\s)(?:--quiet\b|-[A-Za-z]*q[A-Za-z]*)")


def _pytest_extra_q(commands: str) -> str | None:
    """在 CI 的 **pytest 命令行** 上找额外 `-q`，找到就返回命中的 token。

    **两级收窄，缺一不可**：

      1. 只在 `run:` 的命令面上找 —— 注释/步骤名里的 `-q` 是说明不是命令
         （本文件顶部就有一条解释 ISS-24 的注释）；
      2. 只在**含 `pytest` 的行**上找 —— 否则 `test "$heads" -eq 1` 里的
         `-eq` 会被"短选项簇"规则命中，把**合规**判成**违规**。
         这条是实测踩出来的：`-eq` 含 `q`，`-e`+`q` 看着就像 `-xq`。
    """
    for line in commands.splitlines():
        if not re.search(r"\bpytest\b", line):
            continue
        match = _EXTRA_Q.search(line)
        if match is not None:
            return match.group(0).strip()
    return None


def _registered_markers() -> set[str]:
    """从 `pyproject.toml` 的 `markers = [` 块里取出已注册的标记名。"""
    text = PYPROJECT.read_text(encoding="utf-8")
    block = re.search(r"markers\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if block is None:
        return set()
    return set(re.findall(r'"([a-z_]+):', block.group(1)))


#: `--collect-only` 的汇总行。两种形态都要认：
#:   有筛选时 → "35/1511 tests collected (1476 deselected) in 2.28s"
#:   无筛选时 → "1511 tests collected in 2.28s"
#: **优先取汇总行的第一个数**（= 本次收集到的条数），不靠数用例行：
#: 用例行的格式随 pytest 版本变（高版本是 `tests/x.py::test_y`，
#: 老版/`--verbose` 下是 `<Module ...>:N: <Name>`），数行会随版本静默归零，
#: 而汇总行是稳定契约。
_COLLECT_SUMMARY = re.compile(r"^(\d+)(?:/\d+)? tests? collected", re.MULTILINE)


def _ci_tool_steps(text: str) -> set[str]:
    """从 `ci.yml` 的 `run:` 命令面里挑出所有 `python tools/**/*.py` 引用。

    **为什么需要它**：`REQUIRED_CI_STEPS` 是手工名单。手工名单的病是
    "加了新脚本、忘了登记" —— 而那种差异**没有任何判据**能发现。
    本函数把"CI 实际跑了哪些 tool 脚本"现场算出来，供 §5 与名单对账。
    这个病在本轮真实发生过：往门 3 补两条护栏时，若不同步这份名单，
    `ci-step:*` 全部照常 PASS。
    """
    found: set[str] = set()
    for line in _ci_run_commands(text).splitlines():
        for match in re.finditer(r"python\s+(tools/[\w./\-]+\.py)", line):
            found.add(match.group(1))
    return found


def _git_tracked(rel_path: str) -> bool:
    """该仓库相对路径是否**已被 git 跟踪**（`git ls-files --error-unmatch`）。

    **为什么不能用 `Path.exists()`**（ISS-199）：`exists()` 读的是**工作树**，
    而未跟踪的文件在工作树里一样"存在" —— 正是病根。所有既有判据都读工作树，
    而 **CI 读的是版本库**；二者在"有未跟踪/未提交文件"时分叉，且没有任何判据
    会发现（本机全绿、干净检出必红）。

    实测：`5f43770` 把 `backup-restore drill` 步骤提交进 `ci.yml`，而它引用的
    `tools/quality/check_backup_restore_drill.py` 从未入库 ⇒ 干净检出跑门 3
    会在那一步 `python: can't open file`。这是"覆盖范围由人写"（铁律 14）在
    **提交动作**上的复发：提交哪些文件由人挑，挑漏是静默的。

    ⚠️ 取不到 git 事实（不在工作树内）时**返回 False 并让调用方 fail-closed**，
    不静默放行 —— 与 `check_shared_db_quiet.py` 的 E3 同款口径。
    """
    proc = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", rel_path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _collect_only(expr: str) -> tuple[int, int]:
    """`pytest --collect-only -m <expr>` → (退出码, 收集到的用例数)。

    只收集不执行：这一步验证的是"标记能被选中"，秒级完成。
    `-q` 会与 `addopts` 的 `-q` 叠加成 `-qq`（ISS-24），因此**不加**。
    数量从 `--collect-only` 的**汇总行**里取，不依赖 pytest 的退出码语义。
    """
    code, out = _run(
        [
            PYTHON,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "-m",
            expr,
        ],
        cwd=BACKEND_DIR,
    )
    match = _COLLECT_SUMMARY.search(out)
    if match is not None:
        return code, int(match.group(1))
    # 汇总行没解析到（例如 pytest 报错退出）→ 拿用例行兜底。
    # 这一步只在异常路径上跑，正常路径不依赖它，因此版本漂移不会
    # 把"正常"误判成 0 —— 只会把"异常"从 0 恢复成真实值或仍是 0。
    return code, len(re.findall(r"^\S*test_\w+\.py::\w+", out, re.MULTILINE))


def main() -> int:
    checks: list[tuple[str, bool, str]] = []
    lines: list[str] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    def emit(text: str = "") -> None:
        lines.append(text)

    # ------------------------------------------------------------------ §0
    emit("# 回归护栏证据（标记体系 / CI 三道门 / 登记册）")
    emit(f"# 生成时间: {_now()}")
    emit(f"# 命令: python {_repo_rel(SELF_PATH)}")
    emit()

    # ------------------------------------------------------------------ §1
    emit("## [1] pyproject 标记注册")
    registered = _registered_markers()
    for name in REQUIRED_MARKERS:
        add(f"marker-registered:{name}", name in registered)
    emit(f"已注册标记：{', '.join(sorted(registered)) or '(无)'}")
    emit(f"要求标记：{', '.join(REQUIRED_MARKERS)}")
    emit()

    # ------------------------------------------------------------------ §2
    emit("## [2] 标记落点（ast 扫描 tests/）")
    counts = _marked_functions()
    smoke_files = _smoke_files()
    emit(
        "函数级标记："
        + "，".join(f"{k}={v}" for k, v in sorted(counts.items()))
    )
    emit(f"模块级 smoke 文件（{len(smoke_files)} 个）：{', '.join(smoke_files)}")
    # **只写下界**：后续工作包会继续加标记，"恰好 N 个"必失效（本仓铁律）。
    add("regression-marked>=1", counts.get("regression", 0) >= 1)
    add("cross_wp-marked>=1", counts.get("cross_wp", 0) >= 1)
    add("smoke-files>=2", len(smoke_files) >= 2)
    emit()

    # ------------------------------------------------------------------ §3
    emit("## [3] 登记册 ⇄ 代码对账（tools/regression/check_register.py）")
    code, out = _run(
        [PYTHON, str(REPO_ROOT / "tools" / "regression" / "check_register.py")],
        cwd=REPO_ROOT,
    )
    for line in out.splitlines():
        emit(f"$ {line}")
    add("register-check-exit0", code == 0, f"exit={code}")
    add("register-check-PASS", "RESULT PASS" in out)
    emit()

    # ------------------------------------------------------------------ §4
    emit("## [4] 未定义全局扫描（tools/quality/check_undefined_globals.py）")
    code, out = _run(
        [PYTHON, str(REPO_ROOT / "tools" / "quality" / "check_undefined_globals.py")],
        cwd=REPO_ROOT,
    )
    for line in out.splitlines():
        emit(f"$ {line}")
    add("undefined-globals-exit0", code == 0, f"exit={code}")
    add("undefined-globals-PASS", "RESULT PASS" in out)
    emit()

    # ------------------------------------------------------------------ §5
    emit("## [5] CI 三道门（.github/workflows/ci.yml）")
    ci_text = CI_FILE.read_text(encoding="utf-8")
    for job in REQUIRED_CI_JOBS:
        present = re.search(rf"^  {job}:", ci_text, re.MULTILINE) is not None
        add(f"ci-job:{job}", present)
    for step in REQUIRED_CI_STEPS:
        # ⚠️ 判定内容只有一件事：**这一串文本在 ci.yml 里出现过**（= 已挂载）。
        # 它**不证明**该步骤跑过、更不证明它通过 —— 这里从不执行那些步骤
        # （guard 门 17 条命令里含全量 pytest，本脚本秒级完成）。
        #
        # 因此名字必须叫 `ci-step-declared:`。原名 `ci-step:` 是个**谎**：
        # `evidence/m1/regression-guardrails.log` 里印着
        #     `PASS ci-step:tools/quality/check_test_db_isolation.py`
        # 读起来是"隔离判据通过了"，而 2026-09-30 实测那一刻它正报 21 处红
        # （`79dd24c` 引进的，见 ISS-158/ISS-159）—— 连本仓自己的收口复核
        # 都被这行字读过去了。**名字声称的事超出了它检查的事**，与
        # ISS-143（没跑 vs 跑失败同形）/ ISS-95（声称有防线）同一族。
        add(f"ci-step-declared:{step}", step in ci_text)
    # 铁律：CI 里不许再给 pytest 加 `-q`（会叠加成 -qq 吞掉汇总行，ISS-24）。
    # **只在 `run:` 命令面的 pytest 行上判** —— 注释/步骤名里的 `-q` 是说明，
    # `test ... -eq` 里的 `-eq` 是 shell 语法，两个都不是 pytest 选项。
    ci_commands = _ci_run_commands(ci_text)
    hit = _pytest_extra_q(ci_commands)
    add(
        "ci-no-extra-q",
        hit is None,
        f"命中 {hit!r}" if hit else "pytest 命令行未出现叠加 -q",
    )
    # 名单 ≠ 实际的病：CI 里跑着的 tool 脚本必须都在 REQUIRED_CI_STEPS 里
    # （否则"加了脚本忘了登记"没有任何判据）。
    #
    # ⚠️ **双向**判定，不能只查一个方向：
    #   · 只查 `actual - declared` → 名单里躺着一条**早已被删**的步骤，
    #     判据照常绿，而那条 `ci-step:*` 会永远 FAIL（口径打架无人管）；
    #   · 只查 `declared - actual` → 加了脚本忘了登记，静默通过。
    # 两个方向对应两种不同的病，缺一个就等于给一种病发了通行证。
    declared = {s for s in REQUIRED_CI_STEPS if s.startswith("tools/")}
    actual = _ci_tool_steps(ci_text)
    unlisted = sorted(actual - declared)
    stale = sorted(declared - actual)
    add(
        "ci-steps-covered",
        not unlisted and not stale,
        (
            f"未登记：{unlisted}；已失效：{stale}"
            if (unlisted or stale)
            else f"{len(actual)} 个双向对齐"
        ),
    )
    # ------------------------------------------------------------------
    # 「引用即入库」（ISS-199）：`ci.yml` 里每个 `python <path>` 步骤引用的文件，
    # 必须**存在**、且**已被 git 跟踪** —— 而不只是"在工作树里存在"。
    #
    # **为什么必须单独立一条**：上面 `ci-steps-covered` 比的是**名字**，且本文件里
    # 所有判据读的都是**工作树**；而 **CI 读的是版本库**。二者在"有未跟踪/未提交
    # 文件"时分叉，分歧本身没有任何判据会发现 —— 实测 `5f43770` 把
    # `backup-restore drill` 步骤提交进 `ci.yml`，而它引用的
    # `tools/quality/check_backup_restore_drill.py` **从未入库** ⇒ 干净检出跑门 3
    # 会在那一步 `python: can't open file` 直接失败，**而本机全绿**（本机有文件）。
    # 性质是"覆盖范围由人写"（铁律第 14 条）在**提交动作**上的复发：提交哪些文件
    # 由人挑，挑漏是静默的。
    #
    # ⚠️ **无 `.git` 时降级并出声**（副本 / 导出成 tar 的场景，如本仓的
    #    `collect_regression_reverse_evidence.py` 把整仓复制到 /tmp 时刻意排除
    #    `.git`）：此时如实打印"只校验了存在性、没校验入库"，而不是伪装成判过
    #    （ISS-165 的既定口径：取不到 git 事实要降级**并说明**，静默降级 = 判据失明）。
    missing_files = [s for s in sorted(actual) if not (REPO_ROOT / s).is_file()]
    git_present = (REPO_ROOT / ".git").exists()
    untracked = (
        [
            s
            for s in sorted(actual)
            if s not in missing_files and not _git_tracked(s)
        ]
        if git_present
        else []
    )
    add(
        "ci-steps-tracked",
        not missing_files and not untracked,
        (
            f"不存在：{missing_files}；未入库：{untracked}"
            if (missing_files or untracked)
            else (
                "⚠️ 降级：无 .git（副本/导出），只校验了文件存在，未校验是否入库"
                if not git_present
                else f"{len(actual)} 个步骤引用的脚本均已入库"
            )
        ),
    )
    emit(f"门：{', '.join(REQUIRED_CI_JOBS)}")
    emit(f"关键步骤：{'；'.join(REQUIRED_CI_STEPS)}")
    emit(
        f"CI 实际跑的 tool 脚本：{len(actual)} 个"
        f"；未登记 {len(unlisted)} 个；名单已失效 {len(stale)} 个"
    )
    emit(f"pytest 命令含额外 -q：{'是（' + str(hit) + '）' if hit else '否'}")
    emit()

    # ------------------------------------------------------------------ §6
    emit("## [6] 标记可选择性（--collect-only，不执行）")
    expressions = {
        "smoke": "smoke",
        "regression or cross_wp or slow": "regression or cross_wp or slow",
        "not slow": "not slow",
    }
    for label, expr in expressions.items():
        code, collected = _collect_only(expr)
        emit(f"-m '{label}' → 收集 {collected} 条（exit={code}）")
        add(f"collect:{label}>0", collected > 0, f"collected={collected}")
    emit()

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


#: 本脚本应当产生的 CHECK 条数下界。
#: **它的作用是让"脚本崩溃、一条 CHECK 都没产生"与"全部通过"不再同形**：
#: 只看 `failures == 0` 时，这两种情形的输出完全一样（本仓 ISS-43）。
#: 覆盖范围是 4(标记注册) + 3(落点下界) + 2(登记册) + 2(未定义全局)
#: + 3(CI job) + N(CI 步骤) + 1(CI 不加 -q) + 2(步骤名单对账 + 步骤入库) + 3(可选择性)
#: = 20 + N。
#: **N 随 `REQUIRED_CI_STEPS` 增长**（当前 33 条 ⇒ 53）；不写死 N，
#: 免得改一处忘一处（下方各轮实测给出当时的 N 与总数）。
#: ⚠️ 改名单/加判据时必须同改这个数 —— 上一版是 24，本轮加了 2 条步骤
#: （隔离自检、schema 反验）与 1 条对账判据，并把漏登记的
#: `tools/contracts/export_openapi.py` 补进名单，共 +5 → 29。
#: **2026-09-30 收口第 11 步复跑**：`ci-steps-covered` 报"未登记 8 个"
#: （WP-DEBT-01/02 往门 3 加的 `tools/quality/*` 五条判据 + 三个证据脚本）
#: ⇒ 名单 10 → 18 条步骤，条数 29 → 37。这正是"人写名单必须另设判据对账"
#: 的活教材：**判据没有失败，它刚刚抓到了名单与磁盘脱钩**。
#: ⚠️ 教训：这个常量是 `>=` 语义（`complete = len(checks) >= EXPECTED_CHECKS`），
#: 所以名单加长时**若忘了改它，不会报红** —— 与 ISS-153 同族。
#: 本次已同步，并且新加的那 8 条 `ci-step:*` 本身就是"名单真的变长了"的证据。
#: **2026-10-01 收口第 11 步首次真正跑通**：本包（WP-DEBT-03）往门 3 加了 4 个步骤
#: （判据 D/E 外的判据 D/F + 两个反向验证），名单 18 → 22 条步骤，条数 37 → 41。
#: 这 4 条原本漏登记、正是被 `ci-steps-covered` 当场点名才补上的 —— 再次印证
#: "人写名单必须另设判据对账"。
#: **2026-10-05 WP-DEBT-04（ISS-79）**：往门 3 加了 1 个步骤
#: （`check_migration_roundtrip.py`，回滚路径判据），名单 22 → 23 条步骤，条数 41 → 42。
#: ⚠️ 与 WP-DEBT-01/02/03 是**同族第 9 次**：加步骤发生在 ci.yml 侧、清单在本元组侧，
#:    全靠 `ci-steps-covered` 双向对账才不脱钩。本次是"先改清单、再跑判据"（未复现报红），
#:    但该判据的区分力已由前三次收口实测（每次都当场点名漏登项）。
#: **2026-10-07 WP47**：往门 3 加了 1 个步骤（`check_backup_restore_drill.py`），
#: 名单 23 → 24 条步骤，条数 42 → 43。同族第 10 次，同样"先改清单再跑判据"。
#: **2026-10-08 WP47 ⑤⑥**：往门 3 加了 1 个步骤（`check_rollback_drill.py`，
#: 回滚演练），名单 24 → 25 条步骤，条数 43 → 44。同族第 11 次。
#: **2026-10-08 WP47 收口复跑**：`ci-steps-covered` 报「未登记 8 个」——
#:   WP26/WP27 往门 3 加的四条判据 + 四条反向验证（`check_all_exports` /
#:   `check_node_paired_guard` / `check_media_asset_usability_single_source` /
#:   `check_migration_value_domain_no_live_import` 及其 `collect_*_reverse_evidence`）
#:   已落进 ci.yml，但名单侧没同步 ⇒ 收口期补齐，名单 25 → 33 条步骤，条数 44 → 52。
#:   同族第 12 次（首次跨包）。**注意方向上的一致性**：`>=` 语义下补登记只会让
#:   `len(checks)` 变大，若不改这个常量，`len(checks) >= 44` 仍成立、不会报红 ——
#:   所以"名单加长却忘了改常量"是**静默**的（ISS-153 同族），此处已同步。
#: **2026-10-08 WP47 收口期新增第 N+1 条对账判据** `ci-steps-tracked`（ISS-199）：
#:   条数 52 → 53。它与 `ci-steps-covered` 是**两个独立事实**，不能互相顶替 ——
#:   · `ci-steps-covered` 查**名字登记**：`ci.yml` 里引用的 tool 脚本是否都写进了
#:     `REQUIRED_CI_STEPS`（比的是两个**字符串集合**，两边都可以指向同一份
#:     手工名单，且都读工作树文本）；
#:   · `ci-steps-tracked` 查**文件真进了版本库**：`git ls-files --error-unmatch`。
#:   ⚠️ 前者在"引用了从未入库的脚本"时**照常绿**（名字确实登记了），而干净检出跑
#:      CI 会在那一步 `python: can't open file` 直接失败。实测 `5f43770` 提交了
#:      `backup-restore drill` 步骤、其脚本 `check_backup_restore_drill.py` 从未入库。
#:   同族第 13 次，且是**新病灶**：前 12 次都是"ci.yml 加了步骤、清单没跟"，
#:   这次是"动作没漏、**提交漏了**"—— 病根从"人写名单"挪到了"人挑提交文件"，
#:   故第 14 条纪律（覆盖范围由磁盘决定）在此处必须由 `git ls-files`（版本库）
#:   而非 `Path.exists()`（工作树）来兑现。
#: **2026-10-11 WP-DEBT-04（后半）**：往门 3 加了 2 个步骤
#:   （`check_cross_session_shared_files.py` + 它的反向验证），
#:   名单 38 → 40 条步骤，条数 53 → 55。同族第 18 次，同样"先改清单再跑判据"。
#: **2026-10-19 WP36/WP46**：往门 3 加了 5 个步骤（`check_download_not_delivery.py`、
#:   `tools/ops/build_offline_bundle.py` 构建步骤、`check_offline_bundle.py`、
#:   `check_sbom_coverage.py`、`collect_sbom_coverage_reverse_evidence.py`），
#:   名单 43 → 48 条步骤，条数 63 → 68。同族第 19 次，同样"先改清单再跑判据"
#:   （本次改动前先用 `services/backend/.venv/bin/python3` 实测过一遍，43/63 为
#:   改动前的实测基线，不是上一条日志的 40/55——中间还有 WP-DEBT-05 的 3 个步骤
#:   未补这条日志，属已存在的登记债，这里不借道回填）。
EXPECTED_CHECKS = 60


if __name__ == "__main__":
    raise SystemExit(main())
