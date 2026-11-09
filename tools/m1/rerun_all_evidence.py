#!/usr/bin/env python
"""收口第 11 步：复跑**全部**历史证据脚本（覆盖范围由**磁盘**决定）。

**为什么必须复跑**：新包合法新增对象会让**历史包**的等式判据变红 ——
（ISS-44 / ISS-64 / ISS-71 / ISS-83 / ISS-87，本仓同一根因已现身 5 次），
而**单跑新包永远发现不了**：新包的证据只证明"我建的还在"，
它不可能知道 WP18 的某条 `== N` 是否被本包的新对象打破。

**为什么要有这个入库脚本**（ISS-143 —— 同一族缺陷**第四次**复发）
---------------------------------------------------------------
在 2026-09-29（WP24 收口）之前，这一步的载体是**临时目录里手写的 zsh 脚本**：
清单形如 `for f in 10 11 … 23`，外加两条硬编码路径。它的病当场就发作了：

| # | 形态 | 后果 |
|---|---|---|
| ① | `for` 循环停在旧包号（23） | 新包 **WP24 必须有人记得手动追加** —— 忘了就静默漏跑 |
| ② | `tools/regression/` 两个脚本被写成 `tools/m1/…` | 那两次 `rc=2` 是**文件不存在**，在汇总里**长得像"脚本失败"**，而实际是**它们根本没跑** |

第 ② 种的危害最值得记：**如果不是这两个脚本恰好还存在于别处，
"复跑 19 个全部通过"这句话就是假的，且没有任何判据会报红。**
这与 ISS-104（写死目录）→ ISS-111（只换藏身处、病根未除）→ ISS-139
（glob 只扫 `tools/m1/`）是**同一个病**：

> **凡"覆盖范围"由人写（名单 / glob / 目录判断 / 包号上界），
> 就必须改由磁盘决定，并另有一条判据对账它。**（铁律第 14 条）

因此本脚本做三件事，缺一不可：

1. **范围 = `tools/**/collect_*_evidence.py` + `tools/**/refresh_*_evidence.py`
   + `tools/**/collect_*code_review.py`**（三个命名约定）
   —— 按**文件名**在**全仓**匹配，不按子目录，也不设包号上界。
   将来 `tools/m4/` 出现时**无需回来改这个脚本**（这正是病根）。
   （第三个约定于 2026-09-29 补上：code_review 族是同类证据脚本，
   只是命名不同，原先因不以 `_evidence` 结尾而**静默漏跑**。）
2. **与 `check_evidence_scripts.py` 交叉核对发现结果** —— 两者是**独立实现**
   （一个为"自检"、一个为"复跑"），必须对同一份磁盘给出**同一个数**。
   两个实现同时错成一样才是可接受的失败模式，而那比"单点靠记性"窄得多。
3. **逐个脚本核对"它真的写出了证据"**（`evidence/` 下最新 mtime 前进），
   而不是只看退出码 —— 一个"除零前就 return 0"的脚本，
   在退出码这一维与成功**完全同形**（ISS-82 同族：判据必须能区分两种实现）。
4. **跑之前先确认目标库静止**（铁律第 17 条 / ISS-163）。
   本脚本内含多种"真连库、真跑 pytest"的证据，若并行会话此刻正在同一个库上跑，
   两边会双向污染 —— WP-DEBT-02 收口实测 `1 failed, 1146 passed, 639 errors`
   与 `QueryCanceled × 1917`（ISS-160）。因此**在任何子进程启动前**先查
   `pg_stat_activity`：有他人连接即**拒跑**（以 FAIL 结束，把"拒跑"留在证据里）。

跑法：

```bash
python tools/m1/rerun_all_evidence.py            # 复跑全部（含全量 pytest，约半小时）
python tools/m1/rerun_all_evidence.py --list     # 只打印即将复跑的范围，不执行
```

退出码 0 = 全部通过（库静止 **且** 每个脚本 rc=0 **且** 写出了证据 **且** 交叉核对一致）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "evidence"
OUT_LOG = EVIDENCE_DIR / "m1" / "rerun-all-evidence.log"
SELF_CHECK = REPO_ROOT / "tools" / "m1" / "check_evidence_scripts.py"

PYTHON = sys.executable
TZ = timezone(timedelta(hours=8))

#: 覆盖范围的两个命名约定。**只写文件名，不写目录**（ISS-139/ISS-143）。
#:
#: ⚠️ **第四个 pattern 于 2026-09-29 补上（WP-DEBT-01 收口）**：前两个都以
#: `_evidence.py` 结尾，于是四个 `collect_*_code_review.py` **全部漏跑**
#: ——包括本包为 CI 门 3 新建的那个。它们是同一类证据脚本
#: （逐条 CHECK + `EXPECTED_CHECKS` + `RESULT=`），只是命名不同。
#: 这与 ISS-139/ISS-143 同病：**覆盖范围由"名字长什么样"决定，漏掉是静默的**。
#: 必须与 `check_evidence_scripts.py` 的 `_iter_scripts()` 保持同一覆盖范围 ——
#: 两者分叉会被下面那条交叉核对判据抓住（`expected_self = len(SCRIPTS) + 1`）。
SCOPE_PATTERNS = (
    "tools/**/collect_*_evidence.py",
    "tools/**/refresh_*_evidence.py",
    "tools/**/collect_*code_review.py",
)

#: 复跑规模**下界**。刻意写成下界而不是等式：新包会继续加脚本，
#: 写成"恰好 N 个"必然在下一个包失效（铁律 1 —— 判据只能断言"我建的还在"，
#: 不能断言"全项目恰好有 N 个"）。本值取"已知脚本数"，只增不减。
#: 2026-09-29 WP-DEBT-01 收口：19 → 24（纳入四个 `collect_*_code_review.py`）。
#: 2026-09-30 WP-DEBT-02 收口：24 → 26（本包的反向验证 + Code Review 各一）。
#: 2026-10-01 WP-DEBT-03 收口：26 → 28（本包的反向验证 + Code Review 各一）。
#: 2026-10-05 WP25：28 → 29（本包的反向验证 + Code Review 各一）。
#: 2026-10-07 WP26：29 → 30（本包的主题证据脚本 `tools/m3/collect_wp26_evidence.py`；
#:   与其配对的 `collect_wp26_code_review.py` 落地时再 +1）。
#: 2026-10-07 WP26 收口：30 → 31（配对脚本 `tools/m3/collect_wp26_code_review.py`
#:   已落地 —— 磁盘实测 `SCOPE_PATTERNS` 命中 **33** 个，本值取"已知脚本数"作下界）。
#: 2026-10-07 WP27（素材可用性判据）：31 → 32（新增
#:   `tools/quality/collect_media_asset_usability_reverse_evidence.py` ——
#:   `check_media_asset_usability_single_source.py` 的反向验证。**追加一个脚本就 +1**，
#:   与上面三条同款。⚠️ 它的**名字**是关键：`tools/quality/...` + `collect_*_evidence.py`
#:   → 被 `SCOPE_PATTERNS` 第 1 条命中；而**不是** `collect_media_*_evidence.py`
#:   （若带主题前缀会被 `collect_wp*_evidence.py` 那条**更窄**的 glob 抢走，
#:   那条要求内联段 —— WP25 收口踩过这个坑，见下）。）
#: 磁盘实测（2026-10-07 WP27）：`SCOPE_PATTERNS` 命中 **36** 个。
#: 2026-10-08 M5（审计 §4 补盲区）：32 → 33（新增
#:   `tools/quality/collect_declared_evidence_reverse_evidence.py` ——
#:   `tools/m1/audit_consistency.py` §4「文档声明的证据文件必须存在」的反向验证。
#:   ⚠️ 它的**名字**同样关键：`tools/quality/...` + `collect_*_evidence.py`，主题里
#:   **不带** `wp<NN>_` ⇒ 被 `SCOPE_PATTERNS` 第 1 条命中，而不是被更窄的
#:   `collect_wp*_evidence.py` 抢走（后者要求两层内联段 —— WP25 收口踩过，见下）。
#:   **追加一个脚本就 +1**，与上面五条同款；本值仍是滞后下界，不追平磁盘。）
#: 磁盘实测（2026-10-08 M5）：`SCOPE_PATTERNS` 命中 **38** 个。
#: 2026-10-08 WP47 ①（探针根路径别名 + `/metrics` + 日志 requestId）：33 → 34（新增
#:   `tools/m5/collect_wp47_evidence.py` —— 本包的主题证据脚本，跑真 app / 真请求 /
#:   真 registry 做七处核对，**零数据库**（本包零表、零迁移））。
#:   ⚠️ **本包与 WP24~WP26 有一处不同**：那三包的主题证据在测试库上跑，本包不需要 ——
#:   可观测面不落库，因此本脚本不受"并行会话各自隔离库"（铁律 17）影响，
#:   但仍受"并行会话改同一棵工作树"影响（它 import 的是共同的工作树）。
#:   与其配对的 `collect_wp47_code_review.py`（三遍法）落地时再 +1。
#: 磁盘实测（2026-10-08 WP47 ① 收口）：`SCOPE_PATTERNS` 命中 **40** 个
#: （39 + 本轮新增的 `collect_readme_decl_anchor_reverse_evidence.py`，
#:   ISS-206 的反向验证；它的名字落在 `tools/**/collect_*_evidence.py` 上）。
#: ⇒ `MIN_SCRIPTS` 同步 34 → **40**。**这个常量只能人工 +1**（铁律 14 四处手工点之一），
#: 另两处 `check_evidence_scripts.py` / `check_evidence_scope.py` 是自动的。
#:
#: 2026-10-08 WP47② 实测再 +2（40 → **42**）：`A1` 由磁盘自动跟到 41，
#: 新增的 `collect_alerting_rules_reverse_evidence.py` 是 WP47② 告警规则判据的反向验证
#:（它证明了那条判据真会报红 —— 否则"10/10 PASS"与"判据从没生效"完全同形）。
#: ⚠️ 本次它一落地就被 `check_evidence_scope` D1 报红（只 print 不落盘，
#:   被判为"不是真证据脚本"）⇒ 改成 `EVIDENCE.write_text(...)` 自落盘才转绿。
#:   **这是该判据第二次拦下同一类缺陷**（上一次是 WP47① 的同名问题）。
#:
#: ⚠️ WP25 收口实测踩到一次：反向脚本初名 `collect_wp25_code_review_reverse.py`，
#: **不被任何 SCOPE_PATTERNS 命中** ⇒ `check_evidence_scope.py` 的 D2
#: （`B ⊆ A1`，"凡写 evidence 的脚本都必须被复跑覆盖"）当场报红。
#: 这正是铁律 14 要防的形态，而**判据抓住了它**（不是靠人记得）。
#: 改名时又踩到第二层：`..._reverse_evidence.py` 会被 `collect_wp*_evidence.py`
#: 这条**更窄**的 glob 命中，而它要求"必须有两层内联段"（本脚本是单层）
#: ⇒ `check_evidence_scripts.py` 报"未找到内联脚本"。
#: 最终名 `collect_billing_wp25_cr_reverse_evidence.py` 对齐既有 5 个反向脚本
#: 的约定（主题里不带 `wp<NN>_`）。**两个判据共同把约束加在了文件名上。**
#:
#: 2026-10-09 WP27 收口：42 → **43**（新增 `tools/m3/collect_wp27_evidence.py`
#: —— 七个落库链核对 + 图片字节 fail-closed，117 条 PASS；它落在
#: `SCOPE_PATTERNS` 第 1 条 `tools/**/collect_*_evidence.py` 上）。
#: ⚠️ **本值必须与那个脚本同批入库**：它是**未跟踪**的新文件，
#: 若只把 `MIN_SCRIPTS` 提到 43 而脚本没进 git，干净检出上 `_discover()`
#: 只命中 42 个 ⇒ `42 >= 43` 报红（铁律 18：判据必须能在干净检出上跑）。
#:
#: 2026-10-10 WP27 收口续：43 → **44**（新增 `tools/m3/collect_wp27_code_review.py`
#: —— 补齐阶段 5 Code Review 缺口，33 条判据；它落在 `tools/**/collect_*code_review.py`
#: 这条 glob 上，由 `check_evidence_scripts.py` 自动纳入，无需另处登记）。
#: ⚠️ **本值必须与那个脚本同批入库**（同 43 的理由，铁律 18）。
#:
#: 2026-10-18 WP29 收口：44 → 45（新增 `tools/m3/collect_wp29_evidence.py`
#: —— 3 张反馈/审核表的结构 + CHECK/FK/触发器名集合对账 + 不可变性/锚点一致性
#: 行为验证，27 条 PASS；它落在 `SCOPE_PATTERNS` 第 1 条
#: `tools/**/collect_*_evidence.py` 上）。
#:
#: 2026-10-18 WP-DEBT-05 续：45 → **47**（与 WP29 条同日各自 +1 后合并入库，新增
#: `tools/quality/collect_test_data_volume_reverse_evidence.py` +
#: `collect_test_data_volume_code_review.py` —— 判据 D 的反向验证 + 三遍法，
#: 分别落在 `tools/**/collect_*_evidence.py` 与 `tools/**/collect_*code_review.py`
#: 两条 glob 上，自动纳入。判据 D 本体 `check_test_data_volume.py` 不进本计数
#: ——它由 CI 门 3 直接调用，不是 `rerun_all_evidence.py` 的 `SCOPE_PATTERNS` 对象，
#: 与 `check_test_residue.py` 同一分工）。
#: ⚠️ **本值必须与对应脚本同批入库**（同 43/44 的理由，铁律 18）。
#:
#: 2026-10-19 WP30/WP36/WP46 三包并行收口：47 → **52**（磁盘实测
#: `_discover()` 命中数，非人工推算）。新增 `tools/m3/collect_wp30_evidence.py`、
#: `tools/m4/collect_wp36_evidence.py`、`tools/m5/collect_wp46_evidence.py`、
#: `tools/quality/collect_sbom_coverage_reverse_evidence.py` 四个脚本落在
#: `SCOPE_PATTERNS` 第 1 条 `tools/**/collect_*_evidence.py` 上。
#:
#: 2026-10-21 WP31/WP32 收口：52 → **54**（磁盘实测 `_discover()` 命中数，非人工推算）。
#: 新增 `tools/m3/collect_wp31_evidence.py`（试产关口，299 条）与
#: `tools/m3/collect_wp32_evidence.py`（扩产 / 批产，150 条），均落在 `SCOPE_PATTERNS`
#: 第 1 条 `tools/**/collect_*_evidence.py` 上。与 WP30 不同：两包的迁移已存在，
#: 目标库是 `rednote_verify_wp31` / `rednote_verify_wp32`（迁移 head 判据为真检查，非 SKIP）。
#: ⚠️ **本值必须与这两个脚本同批入库**（同 43/44 的理由，铁律 18）。
#:
#: 2026-10-23 WP34 收口：54 → **55**（磁盘实测 `_discover()` 命中数，非人工推算）。
#: 新增 `tools/m3/collect_wp34_evidence.py`（回退影响图 / 导出失效，134 条，含 T-EXP-01 的证据版：
#: 追加失效事件前后被引用 `export_items` 行的 xmin / ctid / md5 不变），落在 `SCOPE_PATTERNS`
#: 第 1 条 `tools/**/collect_*_evidence.py` 上。目标库 `rednote_verify_wp34`。
#: ⚠️ **本值必须与该脚本同批入库**（同 43/44 的理由，铁律 18）。
MIN_SCRIPTS = 57

_RESULT_RE = re.compile(r"^RESULT[=:]\s*(.+)$", re.M)
_SELF_CHECK_COUNT_RE = re.compile(r"(?:全部|共)\s*(\d+)\s*个(?:证据)?脚本")


def _discover() -> list[Path]:
    """按文件名在全仓发现证据脚本（磁盘决定覆盖范围，不由人写清单决定）。"""
    found: dict[Path, None] = {}
    for pattern in SCOPE_PATTERNS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            found[path.resolve()] = None
    return sorted(found)


SCRIPTS = _discover()
#: 条数 = 4 条全局判据 + 每个脚本 2 条（退出码 / 真的写出了证据）。
#: **由发现结果推出**而不是写死 —— 它必须跟着磁盘走，否则又是一处"由人写"。
#: 4 = ① `scope>=MIN_SCRIPTS` ② `scope-spans-dirs>=2`
#:     ③ `shared-db-quiet(铁律17)` ④ `scope-agrees-with-self-check(+1 自指)`。
EXPECTED_CHECKS = 4 + 2 * len(SCRIPTS)

checks: list[tuple[str, bool, str]] = []


def _subprocess_env() -> dict[str, str]:
    """子进程环境：`NO_PROXY='*'` 不可省（本机代理会劫持 localhost 连接）。"""
    env = dict(os.environ)
    env["NO_PROXY"] = "*"
    return env


def _assert_shared_db_quiet() -> tuple[bool, str, str]:
    """收口第 11 步的**前置条件**：目标库必须静止（铁律第 17 条 / ISS-163）。

    **为什么必须放在这里**：本脚本是"真跑全部历史证据"的载体，
    而其中多个脚本要真连库、真跑 pytest。若并行会话此刻正在同一个库上跑，
    两边会**双向污染** —— WP-DEBT-02 收口实测 `1 failed, 1146 passed, 639 errors`
    与 `QueryCanceled × 1917`（ISS-160）。

    第 17 条纪律此前**纯靠自觉**（写在 `MEMORY.md` 里，没有判据守）。
    本函数是它的载体：把"跑前查 `pg_stat_activity`"从记性变成**拒跑**。

    ⚠️ **为什么不用 `ps` 判断**：本机沙箱会拦 `ps`（`Operation not permitted`），
    且进程名也不可靠。`pg_stat_activity` 是**服务端事实**，才是真凭据。

    返回值 `(是否可跑, 说明, 拒跑形态)`。**不抛异常**：本脚本的定位是"复跑"，
    它应当**打印拒跑原因并以 FAIL 结束**（而不是让调用方去处理异常）——
    这样证据文件里会留下"因库被占用而拒跑"这一事实，而不是一个 traceback。

    ## 第三个返回值 `kind` 是**必需的**（ISS-164，2026-10-01 实测踩到）

    拒跑有**两种互不相同的原因**，而它们此前在证据文件里**打印同一句话**：

    | kind | 真实原因 | 处置 |
    |---|---|---|
    | `"busy"` | 目标库上**真有**他人连接 | 换私有库 / 等静止窗 |
    | `"unverifiable"` | **连不上库 / 判据加载不了 / 读不到库名** | 修环境，与并行会话无关 |

    此前 `main()` 里把两者都写成「目标库上有他人连接 ⇒ 不进入复跑」——
    实测（本函数因 `AttributeError` 崩掉那次）证据文件里写的是
    **"目标库上有他人连接"，而 PG 上零连接**。那不只是文案不精确：
    它把「我的工具坏了」**伪装成**「环境不满足纪律」，且两者都会让后面的人
    去白等一个静止窗（ISS-143 同族：两种事实打印同一形状）。
    """

    def _unverifiable(reason: str) -> tuple[bool, str, str]:
        return False, f"静止前提**无法确认**（{reason}）⇒ 按 fail-closed 拒跑", "unverifiable"

    try:
        # 按文件路径导入同级 quality 目录的判据（不依赖 sys.path 顺序）。
        import importlib.util

        module_path = REPO_ROOT / "tools" / "quality" / "check_shared_db_quiet.py"
        name = "_shared_db_quiet"
        spec = importlib.util.spec_from_file_location(name, module_path)
        if spec is None or spec.loader is None:
            # ⚠️ 此前这里是 `return True, "（无法加载静止判据，跳过前置检查）"` —— **fail-open**：
            # 加载不了前置检查就**放行**，等于第 17 条在"判据自己坏了"时静默失效。
            # 与 `probe_quiet` 的 E4 口径不一致（那里是 fail-closed）。已改正。
            return _unverifiable(f"无法为 {module_path.name} 构造 spec")
        module = importlib.util.module_from_spec(spec)
        # ⚠️⚠️ **这一行是本脚本曾经完全跑不通的直接原因**（ISS-164，2026-10-01）：
        # 判据 E 用了 `@dataclass`，而 CPython 3.13 的 `dataclasses._is_type()` 会执行
        # `sys.modules.get(cls.__module__).__dict__` —— 模块**没注册进 `sys.modules`**
        # 时它是 `None` ⇒ `AttributeError: 'NoneType' object has no attribute '__dict__'`。
        # 下面那层 `except Exception` 把它兜成"拒跑"，于是**收口第 11 步从未真正跑通一次**，
        # 而误报的文案还说"目标库上有他人连接"。
        # 同仓 `collect_discipline_reverse_evidence.py` 的 `_default_dsn()` 里
        # **早就写明**了这个坑（说明作者知道），却漏在了唯一真正生效的这条路径上
        # —— 又一次 ISS-95：「声称自己有防线」比「没有防线」更危险。
        # 另有判据 `tools/quality/check_loader_registration.py` 静态守这件事。
        sys.modules[name] = module
        spec.loader.exec_module(module)
        report = module.probe_quiet(None)
        if report.quiet:
            return True, f"目标库 `{report.database}` 静止（无他人连接）", "quiet"
        detail = "；".join(o.describe() for o in report.occupants)
        return (
            False,
            f"目标库 `{report.database}` 上有 {len(report.occupants)} 个他人连接 ⇒ "
            f"按第 17 条拒跑（ISS-160 并发污染）。占用者：{detail}",
            "busy",
        )
    except Exception as exc:  # noqa: BLE001 —— 前置检查失败必须报红，不能静默放行
        return _unverifiable(f"{type(exc).__name__}: {exc}")


def _newest_evidence_mtime() -> float:
    newest = 0.0
    for path in EVIDENCE_DIR.rglob("*"):
        if path.is_file():
            newest = max(newest, path.stat().st_mtime)
    return newest


def _run(script: Path) -> tuple[int, str]:
    """跑一个证据脚本，返回 (退出码, RESULT 行)。

    ⚠️ **取最后一条 `RESULT=` 行，不取第一条**（2026-10-19 实测修正）：
    `collect_sbom_coverage_reverse_evidence.py` 这类反向验证脚本会把
    "变异后子进程的原始输出"逐条引用进 ```代码块```（便于人读"这次真的
    报红了"），而子进程自己的输出里天然含一行 `RESULT=FAIL ...`——它出现
    在文件**中段**，是"证据展示"，不是"本脚本的最终结论"。只有**最后**
    一行 `RESULT=` 才是本脚本自己的终审判决（`collect_sbom_coverage_
    reverse_evidence.py` 的 `RESULT=PASS cases=8/8` 恒排在文件末尾）。
    取第一条会把展示用的中间引用误判为终审，造成假红
    （实测：8/8 全过却被判 `FAIL checks=14/14 failures=2`）。
    对只打一条 `RESULT=` 行的既有脚本，"取第一条"与"取最后一条"等价，
    此改动不改变它们的判定。
    """
    completed = subprocess.run(
        [PYTHON, str(script)],
        cwd=REPO_ROOT,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    matches = list(_RESULT_RE.finditer(output))
    return completed.returncode, (matches[-1].group(1).strip() if matches else "(未产出 RESULT 行)")


def _self_check_scope_count() -> int | None:
    """取 `check_evidence_scripts.py` 自己发现并自检的脚本数（用于交叉核对）。

    **为什么这条交叉核对是必要的**：两个脚本各自维护 glob，若哪天只改了一个，
    "复跑的范围"与"自检的范围"就会**静默分叉** —— 而分叉的方向通常是
    "自检少了一批、复跑多了一批"或反之，两种都不会报错。
    """
    completed = subprocess.run(
        [PYTHON, str(SELF_CHECK)],
        cwd=REPO_ROOT,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
    )
    text = (completed.stdout or "") + (completed.stderr or "")
    match = _SELF_CHECK_COUNT_RE.search(text)
    return int(match.group(1)) if match else None


def main() -> int:
    now = datetime.now(TZ)
    if "--list" in sys.argv:
        print(f"覆盖范围（磁盘发现，{len(SCRIPTS)} 个）：")
        for script in SCRIPTS:
            print(f"  {script.relative_to(REPO_ROOT)}")
        return 0

    lines: list[str] = [
        "# 收口第 11 步：全部历史证据脚本复跑（范围由磁盘决定）",
        f"# 生成时间: {now.isoformat(timespec='seconds')}",
        "# 覆盖: tools/**/collect_*_evidence.py + tools/**/refresh_*_evidence.py",
        "#       + tools/**/collect_*code_review.py（第三个约定，与 SCOPE_PATTERNS 逐条对齐）",
        "# 为什么必须复跑: 新包合法新增对象会让历史包的等式判据变红",
        "#   （ISS-44 / 64 / 71 / 83 / 87，同一根因已现身 5 次），单跑新包发现不了。",
        "# 命令: cd <repo> && NO_PROXY='*' python tools/m1/rerun_all_evidence.py",
        "",
        f"## [1] 发现的脚本（{len(SCRIPTS)} 个）",
    ]
    for script in SCRIPTS:
        lines.append(f"  {script.relative_to(REPO_ROOT)}")
    lines.append("")

    checks.append(
        (
            "scope>=MIN_SCRIPTS",
            len(SCRIPTS) >= MIN_SCRIPTS,
            f"{len(SCRIPTS)} 个（下界 {MIN_SCRIPTS}）",
        )
    )
    # ---- 前置条件：目标库必须静止（铁律第 17 条 / ISS-163）------------------
    #
    # ⚠️ **把这条放在复跑之前**（而不是只写进文档）：它要在**任何子进程启动前**
    # 就拦下并发。若放在末尾，污染早已发生（WP-DEBT-02 的教训）。
    quiet_ok, quiet_detail, quiet_kind = _assert_shared_db_quiet()
    checks.append(("shared-db-quiet(铁律17)", quiet_ok, quiet_detail))
    lines.append("## [1b] 前置条件：共享库静止（铁律 17 / ISS-163）")
    lines.append("")
    lines.append(f"  {'PASS' if quiet_ok else '**FAIL**'} {quiet_detail}")
    lines.append("")
    if not quiet_ok:
        # 拒跑：不再执行任何子进程。写出证据并返回非 0。
        lines.append("## [2] 已拒跑（前置条件未满足）")
        lines.append("")
        # ⚠️ 按 `kind` 分派文案（ISS-164）。此前两种情况都写死
        # 「目标库上有他人连接」—— 实测在 PG 零连接时也这么打印，
        # 把"判据自己加载失败"谎报成"环境被占用"，让人去白等静止窗。
        if quiet_kind == "busy":
            lines.append(
                "  目标库上**确有他人连接** ⇒ 按第 17 条纪律**不进入复跑**。"
                "并发跑真库判据会双向污染（ISS-160 实测：639 errors / QueryCanceled × 1917）。"
            )
            lines.append("")
            lines.append(
                "  处置：`REDNOTE_TEST_DATABASE_URL=…_wpdebtNN` 换私有库，"
                "或等并行会话结束、PG 回到静止窗后再跑。"
            )
        else:
            lines.append(
                "  拒跑的**原因不是占用**，而是**静止前提无法确认**（见 [1b] 的括号内原因）"
                " ⇒ 这是**本工具/环境的问题**，与并行会话无关。"
            )
            lines.append("")
            lines.append(
                "  ⚠️ 不要把它当成「等一个静止窗就会好」：先修掉括号里那个异常"
                "（例如判据模块加载失败 / DSN 连不上），再重跑。"
            )
        lines.append("")
        passed = sum(1 for _, ok, _ in checks if ok)
        failed = len(checks) - passed
        reason = "因库被占用而拒跑" if quiet_kind == "busy" else "因静止前提无法确认而拒跑"
        summary = (
            f"RESULT=FAIL checks={passed} PASS / {failed} FAIL"
            f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条；{reason}）"
        )
        lines.append("")
        lines.append(summary)
        OUT_LOG.parent.mkdir(parents=True, exist_ok=True)
        OUT_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print()
        print(summary)
        print(f"写入 {OUT_LOG.relative_to(REPO_ROOT)}")
        return 1
    # 覆盖范围必须跨目录 —— 只覆盖一个目录正是 ISS-139/ISS-143 的形态。
    scope_dirs = sorted({script.parent.name for script in SCRIPTS})
    checks.append(
        (
            "scope-spans-dirs>=2",
            len(scope_dirs) >= 2,
            "、".join(scope_dirs),
        )
    )

    lines.append("## [2] 逐个复跑（退出码 + 是否真的写出证据）")
    lines.append("")
    lines.append("| 脚本 | 退出码 | 证据已刷新 | RESULT |")
    lines.append("|---|---|---|---|")
    for script in SCRIPTS:
        before = _newest_evidence_mtime()
        code, result = _run(script)
        written = _newest_evidence_mtime() > before
        rel = script.relative_to(REPO_ROOT)
        checks.append((f"rc==0 {rel}", code == 0, f"exit={code}"))
        checks.append((f"wrote-evidence {rel}", written, f"evidence mtime advanced={written}"))
        lines.append(
            f"| `{rel}` | {code} | {'是' if written else '**否**'} | {result} |"
        )
        print(f"[{'OK  ' if code == 0 and written else 'FAIL'}] {rel}  exit={code}  result={result}")
    lines.append("")

    lines.append("## [3] 与 check_evidence_scripts.py 交叉核对发现结果")
    self_count = _self_check_scope_count()
    # **自检器比本脚本多 1 个**：`rerun_all_evidence.py` 自己也是 `*_evidence.py`，
    # 会被自检纳入（正确 —— 它确实产出证据），但它**不可能复跑自己**。
    # 因此交叉核对写成 `self == len(SCRIPTS) + 1`，并把这个"1"的**理由**写在
    # 判据名里，而不是把判据放宽成 `>=`（放宽成 >= 会让"自检少扫了一批脚本"
    # 这种真正的分叉**静默通过** —— 那正是本条判据要抓的东西）。
    expected_self = len(SCRIPTS) + 1
    agree = self_count == expected_self
    checks.append(
        (
            "scope-agrees-with-self-check(+1 自指)",
            agree,
            f"rerun={len(SCRIPTS)} self-check={self_count}（期望 {expected_self}）",
        )
    )
    lines.append(
        f"  rerun_all_evidence.py 发现 **{len(SCRIPTS)}** 个；"
        f"check_evidence_scripts.py 发现 **{self_count}** 个 ⇒ "
        f"期望 {expected_self}（后者多 1 个 = 本脚本自己）⇒ "
        f"{'一致' if agree else '**不一致（两个实现的范围已分叉）**'}"
    )
    lines.append("")

    passed = sum(1 for _, ok, _ in checks if ok)
    failed = len(checks) - passed
    complete = len(checks) >= EXPECTED_CHECKS

    lines.append("## [4] 结论")
    lines.append("")
    lines.append("| 判据 | 结果 | 说明 |")
    lines.append("|---|---|---|")
    for name, ok, detail in checks:
        lines.append(f"| `{name}` | {'PASS' if ok else '**FAIL**'} | {detail} |")
    lines.append("")
    lines.append(f"共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条（完整={complete}）")

    ok_all = failed == 0 and complete
    summary = (
        f"RESULT={'PASS' if ok_all else 'FAIL'} "
        f"checks={passed} PASS / {failed} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）"
    )
    lines.append("")
    lines.append(summary)

    OUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    OUT_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    print(summary)
    print(f"写入 {OUT_LOG.relative_to(REPO_ROOT)}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
