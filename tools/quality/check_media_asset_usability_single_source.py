#!/usr/bin/env python
"""「某素材当前可用吗」的**唯一拼点**判据（ISS-177 / ISS-95 的判据化）。

## 这条判据在守什么

`reference_assets` 是**只增不减**的表：「停用」不是改行，而是**追加**一行
`state = 'RETIRED'` 且 `supersedes_asset_id = <被停用的那一行>`。
于是"某素材 `X` 当前可用吗"这句话的真值**不在单列里**：

| 列 | 它单独回答的问题 |
|---|---|
| `state` | 这一行代表的**登记动作**是什么（`ACTIVE` = 登记、`RETIRED` = 停用）|
| `supersedes_asset_id` | 这一行是否**接替**了别的一行 |

**两列合起来**才是答案：

    素材 `X` 当前可用 ⇔ `X` 存在，且不存在一行
                       `state = 'RETIRED' AND supersedes_asset_id = X.id`

于是有两个**互不相同**的错误能被写出来，而它们**都不报错**：

1. **只判 `state`**（如 `WHERE state = 'ACTIVE'`）—— 漏掉"是否已被接替"。
   一个 `ACTIVE` 的行**完全可能已经被人停用过了**（停用是往表里**加**一行，
   原行的 `state` 一个字节都没变）。后果：新图可以引用一个已停用的素材，
   AQ-03 的对照关系会悄悄指向一个已作废的型号标注。
2. **两处各拼一遍** —— 两处判断**必然分叉**（今天分叉或明天分叉），
   而分叉的那一刻只表现为"某个入口给错答案"，**不报错、不告警**。

`ddl.active_asset_states()` 的 docstring 已经把落点钉在"`repository.py` 的
单一函数"，`repository.py` 的模块 docstring 第 6 条把它钉在
`_usable_asset_condition`。**但注释不是防线** —— 本判据存在的唯一理由，
就是 ISS-95 那句话：

> **「声称自己有防线」比「没有防线」更危险。**

写这份判据的直接触发事件：2026-10-07 落 `modules/media/repository.py` 时，
我在它模块 docstring 第 6 条**点名了本脚本**（`check_media_asset_usability_single_source.py`），
而它**当时并不存在**。那一刻"声称"已经写进代码了 —— 若没人回去把它实现，
代码读者会以为有一条判据守着，而它**只是几个字符**。

## 判据（G1~G10）

| # | 判据 | 抓什么 |
|---|---|---|
| G1 | **唯一拼点**：函数体层面"构造查询 + 同时持有两符号"的函数**恰好 1 个** | 出现第二个拼点 ⇒ 两处判断必然分叉 |
| G1b | **结构排除的前提仍然成立**：持有两符号但**不在函数体**的位置必须存在 | 有人把那句 CHECK 搬进函数体 ⇒ G1 会变 2，报出的是"分叉"而**真因**是判据域被污染 |
| G2 | 唯一拼点的名字是私有约定名 `_usable_asset_condition` | 改名/换实现后 G3~G5 的锚点失效 |
| G3 | 它构造的是**取反的存在判定**（`~exists(...)`） | 写成 `exists(...)` ⇒ 可用性**整体反向**，且静默 |
| G4 | `state` 侧比较用**常量** `ASSET_STATE_RETIRED`，且**不得**用 `ASSET_STATE_ACTIVE` / 字面量 `'RETIRED'` | 退化成"只判 state"（上文错误①）、或值来源分叉 |
| G5 | 全部调用点落在**同一个模块**内，且数量 >= `MIN_CALLS` | 别处绕过私有入口自己拼（AST 上是 `Attribute` 不是 `Name`） |
| G6 | `_usable_asset_condition` **不在任何 `__all__` 里** | 把"条件"交出去 ⇒ 立刻多一个拼点 |
| G7 | 公开入口 `only_usable` 形参默认值**必须是 `True`** | 默认取 `False` ⇒ 默认多列一个已作废的素材 |
| G8 | 全仓**不存在**"函数体内既做 `state == ACTIVE` 比较、又持有 `supersedes_asset_id`" | 有人补个 `state == 'ACTIVE'` 的过滤当作可用性判据（错误①） |
| G9 | docstring 里点名的 `tools/quality/check_*.py` **必须真实存在** | **ISS-177 本体**：点名一个不存在的脚本 = 声称有防线 |
| G10 | 活性下界：扫描文件数 / 持两符号的位置数 | 判据自身 AST 规则失效后"全通过"（ISS-143 同族） |

## 为什么 G1 不需要"排除名单"（这是本判据最要紧的一处设计）

本仓**函数体之外**确有两处共现这两个符号（G1b 实测 = 2），但它们**都不是查询**：

| 位置 | 形态 | 为什么不是拼点 |
|---|---|---|
| `media/models.py` 的 `retired_needs_supersedes` | 表级 CHECK 约束（在 `class ReferenceAsset` 体）| 它约束的是"**登记动作**自洽"（停用必须指明接替谁），**不是查可用性**；且它落在**类体** |
| **本脚本自己**的模块级 | 四个关键词常量（`_STATE_SYMBOL` / `_COL_SYMBOL` 等）| 那是判据用来扫全仓的**骨头**，不构造任何查询 |

⚠️ `media/types.py` **不在**这个集合里（初版这里曾把它列为第二处 —— 错）：
它里面的 `supersedes_asset_id` 是 `ReferenceAssetPayload` 的**注解 target**，
`_is_load()` 按 `ast.Load` 已正确排除；它定义的 `ASSET_STATE_RETIRED` 是**单个**
符号，凑不出"两符号共现"。**判据对、注释错**，已订正。

⇒ **G1 的判据域就是"函数体"这一层结构**，那两处**结构上**不在域内。
**这是结构排除，不是名单排除** —— 没有任何文件名白名单，加一个 `EXEMPT_FILES`
就是"把报红的地方都加进来"的开始。

但"结构排除"有一个前提：**那两处必须继续待在函数体之外**。
若哪天有人把 CHECK 挪进一个函数（哪怕只是包一层），G1 会从 1 变 2，
而**报出的原因是"出现第二个拼点"** —— 与真因（判据域被污染）南辕北辙。
G1b 就是为这个前提写的：它独立断言"存在**不在函数体**的两符号共现位置"，
于是"领域被污染"与"真的多了一个拼点"能被分开诊断（判据纪律第 4 条：
两件独立事实不可互相替代）。

## ⚠️ G5 的实现有一处**必须走 AST `Attribute`** 的坑（与 `_raise_block` 同型）

`_usable_asset_condition` 是 `@staticmethod`，调用点长这样：

    self._usable_asset_condition(ReferenceAsset.id)

它在 AST 里是 **`ast.Attribute(attr="_usable_asset_condition")`**，
**不是** `ast.Name`。`tools/quality/check_node_paired_guard.py` 的
`_calls_helper()` 已经踩过一次同型的坑（它当初用字符串常量判 `_raise_block`
⇒ 6/6 全部误报"缺 `_raise_block`"）。因此这里**同时**收 `Name` 与 `Attribute`：

  · 收 `Name` —— 覆盖"模块内直接调用私有函数（`_foo(...)`）"这种将来可能的形态；
  · 收 `Attribute` —— 覆盖本仓现在的真实形态（`self._foo(...)`）。

只收一种都会静默漏 —— 而**漏的方向**恰好是"调用点计数为 0 ⇒ G5 报红"，
也就是说这个坑**不会**让判据假绿（好），但它会让判据**误报**（坏）。
初版就用 `Name` 写，实测 `REF(attr)=4 / REF(name)=0` —— 计数为 0 会报红，
那时若急着"修判据"就会把阈值调低，进而让它变成摆设。按事实改，不按阈值改。

## 不做假保证（已知盲区，写在这里而不是假装没有）

  · **纯 AST、零数据库**。它判"这段判断**只写在一个地方**"，
    **不断言**那段判断的逻辑在运行期给出正确答案（那由 WP27 证据脚本的
    真库探针守）。静态判结构，运行期判值。
    ⛔ 原文还并列了一个 `tests/test_media_wp27.py` —— 该文件**不存在**，
    `media` 包在 `services/backend/tests/` 下**零覆盖**（2026-10-07 实测
    更正：没有任何测试 import 过 `rednote.modules.media`）。⇒ 运行期这一侧
    **只有证据脚本，没有 pytest 用例**。
  · **不判 `supersedes_asset_id` 指向的行是否同项目** —— 那是 `CHECK`
    与 `service.py` 的事，本判据不越俎代庖。
  · **f-string 插值扫不到**（本仓已记录的静态分析通病）：若有人把符号名
    写成 `f"{prefix}RETIRED"`，G1 会**少**命中 ⇒ 判据报红要求改回常量。
    扫不到就报红、不静默 —— 这是刻意的（ISS-95）。

跑法：
    python tools/quality/check_media_asset_usability_single_source.py
退出码 0 = 十条判据全过。

反向验证（把错误实现放回去，必须报红）：
    python tools/quality/collect_media_asset_usability_reverse_evidence.py --fast
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 扫哪些后缀。与同族判据一致：只扫 Python（DDL 也是 Python 字符串）。
SUFFIXES = (".py",)

#: 跳过这些目录（第三方 / 生成物 / 证据文本）。
SKIP_DIRS: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".workbuddy",
        "site-packages",
        "evidence",
    }
)

# ---------------------------------------------------------------------------
# 判据的"关键词"全部放**模块级常量** —— 这是刻意的自透明设计
# ---------------------------------------------------------------------------
#
# ⚠️ 本判据扫的是"**函数体里出现哪些符号**"。若它自己的函数体里散落着
# `"ASSET_STATE_RETIRED"` 这样的字面量，那个函数体就会被 G1 命中 ⇒
# G1 从 1 变 2 ⇒ **判据被自己判红**。
# （同族先例：`check_node_paired_guard.py` 把 `_PAIRED_MARK` 放在模块级，
#   正是为了不让自己的函数体命中自己的收集器。）
#
# 因此：函数体内**只允许**出现这些 Name（`_STATE_SYMBOL` / `_COL_SYMBOL` …），
# 字面量只在模块这一层出现一次。这不是洁癖 —— 它是"判据必须对自己也成立"。

#: `state` 侧的状态名符号（`types.ASSET_STATE_RETIRED` 的**标识符**）。
_STATE_SYMBOL = "ASSET_STATE_RETIRED"

#: 自引用列名符号（`reference_assets.supersedes_asset_id`）。
_COL_SYMBOL = "supersedes_asset_id"

#: "另一个状态"的符号 —— G4/G8 用它判"有没有人拿 ACTIVE 当可用性判据"。
_ACTIVE_SYMBOL = "ASSET_STATE_ACTIVE"

#: 字面量形态的 `RETIRED`（G4 判"值来源是否分叉"）。
_STATE_LITERAL = "RETIRED"

#: 唯一拼点的私有约定名（改动它必须同步 `media/repository.py` 与 G2）。
_CONDITION_NAME = "_usable_asset_condition"

#: "构造查询"的证据：这些名字出现在函数体里，说明它在拼一段 SQLAlchemy 查询。
#: 它把"持有一个常量"（`types.py` 的定义）与"拿这个常量做查询"
#: （真正的拼点）分开 —— 这是 G1 精确性的来源。
QUERY_HINTS: Final[frozenset[str]] = frozenset(
    {
        "select",
        "exists",
        "where",
        "filter",
        "scalar",
        "scalars",
        "execute",
        "aliased",
        "and_",
        "or_",
        "not_",
        "func",
    }
)

#: `state` 属性名（判 Compare 的哪一侧是状态列）。
_STATE_ATTR = "state"

#: G7 的形参名（公开入口的"只列可用"开关）。
_ONLY_USABLE_ARG = "only_usable"

#: G5 的调用点**活性下界**。
#:
#: 刻意写成**下界**而不是等式（铁律 1：判据只能断言"我建的还在"，
#: 不能断言"全项目恰好有 N 个公开入口"—— 删一个入口是合法重构）。
#: 但"降到 1"以下说明调用点收集器失效（或有人把入口删光），必须报红。
#: 2026-10-07 实测 4 个（`usable_asset_ids` / `list_reference_assets` /
#: `count_reference_assets` / `claim_assets`）。
MIN_CALLS = 4

#: G7 的 `only_usable` 形参活性下界（实测 2 个：list / count）。
MIN_ONLY_USABLE = 2

#: G10 的**收集器**活性下界。
#:
#: ⚠️ **这里不写"持两符号的函数数"作为下界** —— 第一版把它写成 `>= 2`，
#: 首跑当场假红。原因是一个**错误的前提**：我以为 `media/models.py` 的
#: `retired_needs_supersedes` CHECK 落在某个函数体里、于是"持两符号的函数"
#: 应该有 2 个。实测 `in_function=False`（它在**类体**）—— 函数体层面
#: **恰好 1 个**才是正确状态。
#:
#: 故意不把下界调成 `>= 1` 了事：那会**掩盖**这条前提错误的教训，
#: 且 `>= 1` 与"恰好 1"在 G1 里已经判过了（重复判据）。
#: 正解是换一个**与结论无关**的活性指标：
#:
#:   · `files`（扫描文件数）—— 收集器的输入规模；
#:   · `symbol_sites`（两符号出现的**总次数**）—— 收集器的域非空。
#:
#: 两者都不依赖"共现在哪一层"，因此不会因为"CHECK 住在类体还是函数体"
#: 而漂移。2026-10-07 实测：files=312、symbol_sites=10。
MIN_FILES = 200

#: 两符号出现总次数（Name / Attribute / 非 docstring 字符串）的下界。
#: 只用于"收集器整体失效"这种灾难性回归（那时它会扫不到任何符号，
#: 而 G1 会因为域为空报"找不到拼点"—— 两个原因必须分开诊断）。
MIN_SYMBOL_SITES = 6

#: G1b 的"非函数体共现容器"活性下界。
#:
#: ## 实测（2026-10-07）：**恰好 2 处**
#:
#: | # | 位置 | 为什么在那里 |
#: |---|---|---|
#: | 1 | `media/models.py` 的 `class ReferenceAsset` 体 | `retired_needs_supersedes` **表级 CHECK**（约束的是"停用必须指明接替谁"这个**登记动作自洽**，不是查可用性）|
#: | 2 | **本脚本自己**的模块级 | 它的四个关键词常量（`_STATE_SYMBOL` 等）—— 那是判据自己的骨头 |
#:
#: ## ⚠️ 两个曾经写错、现在纠正的事实（本判据首跑的产物）
#:
#: · **不是 3 处**。初版下界旁边写的"实测 3 处（含 `types.py` 模块级）"是**错的**：
#:   `types.py` 里 `supersedes_asset_id` 是 `ReferenceAssetPayload` 的**注解 target**
#:   （`supersedes_asset_id: uuid.UUID | None`），`_is_load()` 按 `ast.Load` 正确
#:   把它排除了 —— 判据对，**注释错**。
#: · **不是"`models.py` 的模块级也命中"**。同一个 CHECK 曾同时出现在
#:   "模块级"与"class"两个标签下（首版把"模块级"的域写成了"整棵树减函数体"，
#:   于是类体节点被数了两遍）。现已按**互斥容器**修正 —— 见 `_collect_non_functional()`。
#:
#: **下界取 2 而不是 3**：`MIN_NON_FUNC_PAIRS` 守的是"结构排除的前提仍然成立"
#: （那两处必须继续待在函数体之外）。取 2 与实测一致；取大会在删掉
#: `models.py` 那个 CHECK 时误报（而删 CHECK 是别处判据的事）。
MIN_NON_FUNC_PAIRS = 2

#: G9 的引用豁免表（`{被点名的脚本相对路径: 理由}`）。
#:
#: ## 为什么需要它，以及为什么它**现在只有一条**
#:
#: G9 初版（2026-10-07 实测）当场报了 **1 处** —— 而它报的**就是本脚本自己**：
#: 模块 docstring 里为了说明"反例说明会被当成声称"这个坑，我逐字写了一个
#: 不存在的脚本名 `tools/quality/check_foo.py`，于是 G9 把**举例**读成了**声称**。
#:
#: 这个坑本身值得记：`check_evidence_scripts._strip_comments()` 的 docstring
#: 早就写过同型的假阳性（"本仓库的脚本里故意保留了旧写法的说明文字，
#: 直接全文正则会把这段说明当成还在用"）。**我读了那条教训，还是踩了。**
#:
#: ## 处置为什么是"豁免"而不是"改口径"
#:
#: 三条候选口径，逐条说为什么不选：
#:
#: ① **只扫 module docstring** —— 会漏掉函数 docstring 里的真声称
#:    （本仓 `check_node_paired_guard.py` 就在函数 docstring 里点名别的东西）。
#: ② **靠关键词（'曾经'/'原来'/'已被'）判反例** —— 判据会被措辞绕过：
#:    把"`check_x.py` **是**防线"改成"`check_x.py` **曾经是**防线"就能逃过 G9。
#:    这是 ISS-82 同族的病：判据被"怎么写"决定，而不是被"是什么"决定。
#: ③ **豁免 + 可核对的理由** —— 采用。每条豁免都必须能被核对，
#:    因此判据会断言：
#:      ① 该路径**确实被某个 docstring 引用**（否则豁免是多余的 ⇒ 报红）；
#:      ② 理由不能是空占位（空理由的白名单 = 没有白名单，ISS-95）。
#:
#: **本表当前只有一条，且那条在本脚本自己的 docstring 里** —— 这是刻意的：
#: 白名单要尽可能小。别的脚本里若出现同类反例，判据会报红并要求**当场**
#: 决定"这是声称还是举例" —— 而那正是我们想强迫发生的动作。
#:
#: （与 `check_evidence_scope.EXEMPT_FROM_A1` / `check_test_db_isolation.ALLOW_FILES`
#:   同一纪律：不核对理由的白名单会退化成"把报红的地方都加进来"。）
EXEMPT_REFERENCES: Final[dict[str, str]] = {
    "tools/quality/check_foo.py": (
        "本脚本模块 docstring 里的**反例举例**：用来演示『反例说明会被 G9 "
        "读成声称』这个坑本身（G9 首次运行时确实把它报成了缺失）。"
        "它不是一个被声称存在的防线，只是说明文字里的占位名 —— "
        "因此豁免，而不是为本判据的文档造一个空脚本。"
    ),
}

#: docstring 里"点名一个防线脚本"的形态。锚定到 `tools/quality/check_*.py`：
#: 本仓的判据脚本一律住在这里且以 `check_` 开头（`check_evidence_scope.py`
#: 也用同样的锚点反推覆盖范围）。
_SCRIPT_REF_RE = re.compile(r"tools/quality/(check_[a-z0-9_]+\.py)")


class FuncScan:
    """一个函数在本次判据关心的四个维度上的扫描结果。"""

    __slots__ = (
        "rel",
        "name",
        "lineno",
        "is_method",
        "has_state",
        "has_col",
        "has_query",
        "query_names",
    )

    def __init__(
        self,
        rel: str,
        name: str,
        lineno: int,
        is_method: bool,
        has_state: bool,
        has_col: bool,
        has_query: bool,
        query_names: list[str],
    ) -> None:
        self.rel = rel
        self.name = name
        self.lineno = lineno
        self.is_method = is_method
        self.has_state = has_state
        self.has_col = has_col
        self.has_query = has_query
        self.query_names = query_names

    @property
    def where(self) -> str:
        return f"{self.rel}:{self.lineno}（{self.name}）"

    @property
    def holds_both(self) -> bool:
        return self.has_state and self.has_col


def _iter_py_files() -> list[Path]:
    """覆盖范围由**磁盘**决定（铁律 14）：全仓 `*.py`，不写文件名名单。"""
    out: list[Path] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if path.suffix not in SUFFIXES or not path.is_file():
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        out.append(path)
    return out


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """模块 + 全部函数/类的 docstring 常量节点的 `id()` 集合。

    ⚠️ **必须排除它们**：docstring 也是 `ast.Constant`，会被 `ast.walk` 收进
    字符串常量集合。本判据自己的模块 docstring **逐字**写着
    `ASSET_STATE_RETIRED` 与 `supersedes_asset_id`（那是在解释判据），
    而 `media/repository.py` 的 docstring 第 6 条也逐字写着它们
    （那是在钉落点）。若不排除，**这三处文档**会把 G1 从 1 变成 4。

    ⚠️ 而且**不能只排除"被扫函数的 docstring"**：那些文档还写在
    `media/models.py` / `media/types.py` / `ddl.py` 的**模块级** docstring 里，
    而 `ast.walk` 在模块级遍历时同样会看到它们。因此这里收**三种**位置：
    模块 docstring、类 docstring、函数 docstring —— 一个不漏。

    （同族先例：`check_node_paired_guard._docstring_node()` 只排除了
      "被扫函数自己的 docstring"，在那条判据的域里够用；本判据的域更大，
      所以口径也必须更大。**域变了，口径就得跟着变** —— 抄口径是另一条坑。）
    """
    skip: set[int] = set()
    module_doc = ast.get_docstring(tree, clean=False)
    if module_doc is not None:
        # 模块 docstring 是 `tree.body[0].value`，把它标掉。
        first = tree.body[0] if getattr(tree, "body", None) else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            skip.add(id(first.value))
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        if not node.body:
            continue
        head = node.body[0]
        if (
            isinstance(head, ast.Expr)
            and isinstance(head.value, ast.Constant)
            and isinstance(head.value.value, str)
        ):
            skip.add(id(head.value))
    return skip


def _is_load(node: ast.AST) -> bool:
    """该节点是**读取**（而非赋值目标/形参）。

    ⚠️ 不加这一层会把 `x.attr = ...`（Store）与形参名也算成"引用"。
    本判据的域里暂时不会踩到（没有谁给 `supersedes_asset_id` 赋值），
    但**判据不能靠"暂时不会"** —— 那正是 ISS-95 的轻量版。
    """
    return isinstance(getattr(node, "ctx", None), ast.Load)


def _scope_strings(
    node: ast.AST, *, skip: set[int], start: int, end: int
) -> str:
    """`[start, end)` 行区间内、**排除 docstring** 的字符串常量拼接。

    用于 G3/G4 之外的一处辅助判断：`_state_literal_used` 需要区分
    "常量 `ASSET_STATE_RETIRED`"与"字面量 `'RETIRED'`"。
    按 `(lineno, col_offset)` 排序拼接，让文本与源码书写顺序一致。
    """
    items: list[tuple[int, int, str]] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Constant) or not isinstance(sub.value, str):
            continue
        if id(sub) in skip:
            continue
        if not (start <= sub.lineno <= end):
            continue
        items.append((sub.lineno, sub.col_offset, sub.value))
    items.sort()
    return "".join(text for _, _, text in items)


def _node_span(node: ast.AST) -> tuple[int, int]:
    """节点覆盖的行区间 `[lineno, end_lineno]`。

    ⚠️ 不使用 `end_lineno` 的兜底：CPython 3.8+ 一定填了它，
    但按"域变了口径跟着变"的纪律，这里显式判一次并给出可读错误。
    """
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", None)
    if end is None:  # pragma: no cover - 3.8+ 不会走到
        return start, 10**9
    return start, end


def _collect(tree: ast.AST, rel: str, skip: set[int]) -> list[FuncScan]:
    """扫一棵树，返回全部"持有相关符号"的函数。"""
    out: list[FuncScan] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # 只看**本函数体**内、排除 docstring 的引用。
        names: set[str] = set()
        attrs: set[str] = set()
        strings: list[str] = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and _is_load(sub):
                names.add(sub.id)
            elif isinstance(sub, ast.Attribute) and _is_load(sub):
                attrs.add(sub.attr)
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if id(sub) not in skip:
                    strings.append(sub.value)

        # 函数体内本身可能内嵌函数（本仓无此用法，但口径要正确）：
        # `ast.walk` 会连带收进来。这里不额外剔除 —— 内嵌函数的引用
        # 也算"这个函数知道这件事"，宁可多报不可漏报。
        text = "".join(strings)

        has_state = (_STATE_SYMBOL in names) or (_STATE_SYMBOL in text)
        has_col = (_COL_SYMBOL in names) or (_COL_SYMBOL in attrs) or (_COL_SYMBOL in text)
        query_names = sorted(names & QUERY_HINTS) + sorted(attrs & QUERY_HINTS)
        has_query = bool(query_names)

        # 形参里有 `self`/`cls` ⇒ 是方法（用于报告可读性，不参与判据）。
        args = list(node.args.posonlyargs) + list(node.args.args)
        is_method = any(a.arg in ("self", "cls") for a in args)

        if has_state or has_col:
            out.append(
                FuncScan(
                    rel,
                    node.name,
                    node.lineno,
                    is_method,
                    has_state,
                    has_col,
                    has_query,
                    query_names,
                )
            )
    return out


def _collect_non_functional(tree: ast.AST, rel: str, skip: set[int]) -> list[str]:
    """G1b 的输入：**不在任何函数体内**、且同时持有两符号的**容器**。

    返回可读的位置描述（`<rel>:<lineno>（<标签>）`）。

    ## 口径：按"容器"报，且容器之间**互斥**

    只看两个层级的容器：**模块级**与**类体**。裸赋值不单独报 ——
    它必然住在某个模块或某个类里，报了就是**同一个事实数两遍**
    （第一版实测：`models.py` 的同一个 CHECK 被报成 3 处，因为
    模块级扫描把类体节点也算进来了）。

    因此：

      · 扫"模块级"时，**排除全部类体节点**（否则 `models.py` 的 CHECK
        会同时出现在"模块级"与"class"两个标签下）；
      · 扫某个类时，**排除它的嵌套类节点**（同理，防嵌套类里的节点被数两次）。

    ⚠️ 这正是判据纪律第 4 条的用法：**"同一事实的两种写法"不是两个独立事实**。
    数两遍会让下界失真 —— 于是"真多了一个共现位置"与"扫描口径退化"
    再也分不开。
    """
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                inside.add(id(sub))

    def _ids(node: ast.AST) -> set[int]:
        return {id(s) for s in ast.walk(node)}

    all_classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    class_owned: set[int] = set()
    for cls in all_classes:
        class_owned |= _ids(cls)

    found: list[str] = []

    def _scan(container: ast.AST, label: str, *, exclude: set[int]) -> None:
        names: set[str] = set()
        attrs: set[str] = set()
        strings: list[str] = []
        for sub in ast.walk(container):
            if id(sub) in inside or id(sub) in exclude:
                continue
            if isinstance(sub, ast.Name) and _is_load(sub):
                names.add(sub.id)
            elif isinstance(sub, ast.Attribute) and _is_load(sub):
                attrs.add(sub.attr)
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if id(sub) not in skip:
                    strings.append(sub.value)
        text = "".join(strings)
        has_state = (_STATE_SYMBOL in names) or (_STATE_SYMBOL in text)
        has_col = (
            (_COL_SYMBOL in names) or (_COL_SYMBOL in attrs) or (_COL_SYMBOL in text)
        )
        if has_state and has_col:
            found.append(f"{rel}:{getattr(container, 'lineno', 0)}（{label}）")

    # ① 模块级：排除**全部**类体（类体由 ② 单独报）。
    _scan(tree, "模块级", exclude=class_owned)

    # ② 逐个类：排除自己的**嵌套类**（嵌套类会作为独立条目被 ② 报到）。
    for cls in all_classes:
        nested: set[int] = set()
        for inner in cls.body:
            if isinstance(inner, ast.ClassDef):
                nested |= _ids(inner)
        _scan(cls, f"class {cls.name}", exclude=nested)

    return sorted(found)


def _find_condition_func(funcs: list[FuncScan]) -> tuple[list[FuncScan], list[FuncScan]]:
    """返回 `(全部持两符号的函数, 其中同时构造查询的)`。"""
    both = [f for f in funcs if f.holds_both]
    query = [f for f in both if f.has_query]
    return both, query


def _condition_impl(tree: ast.AST) -> ast.FunctionDef | None:
    """取 `_usable_asset_condition` 的函数节点（若有）。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == _CONDITION_NAME:
                return node
    return None


def _has_negated_exists(node: ast.AST) -> bool:
    """G3：函数体内存在"**取反的**存在判定"。

    认三种等价写法：
      · `~exists(...)`        —— `ast.UnaryOp(op=Invert)` 包着 `exists(...)`
      · `not_(exists(...))`   —— SQLAlchemy 的 `not_`
      · `~select(...)...`     —— 直接对 select 取反（少见但合法）

    ⚠️ 反过来：`exists(...)` **裸调用**（没有被取反包裹）要能被识别为**违规**，
    判据才能区分"可用 ⇔ 不存在"与"可用 ⇔ 存在"（后者把答案整体反过来，
    且运行期不报错 —— 它只是给出相反的答案）。
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.UnaryOp) and isinstance(sub.op, ast.Invert):
            for inner in ast.walk(sub.operand):
                if isinstance(inner, ast.Call):
                    func = inner.func
                    name = (
                        func.id
                        if isinstance(func, ast.Name)
                        else (func.attr if isinstance(func, ast.Attribute) else "")
                    )
                    if name in ("exists", "select"):
                        return True
        if isinstance(sub, ast.Call):
            func = sub.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else (func.attr if isinstance(func, ast.Attribute) else "")
            )
            if name == "not_":
                return True
    return False


def _state_compares(node: ast.AST) -> list[ast.Compare]:
    """函数体内所有"某一侧是 `X.state`"的比较表达式。"""
    out: list[ast.Compare] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Compare):
            continue
        sides: list[ast.AST] = [sub.left, *sub.comparators]
        for side in sides:
            if isinstance(side, ast.Attribute) and side.attr == _STATE_ATTR:
                out.append(sub)
                break
    return out


def _compare_uses(node: ast.Compare, symbol: str) -> bool:
    """该比较里是否出现**常量** `symbol`。"""
    for sub in [node.left, *node.comparators]:
        if isinstance(sub, ast.Name) and sub.id == symbol:
            return True
    return False


def _compare_uses_literal(node: ast.Compare, literal: str) -> bool:
    """该比较里是否出现**字符串字面量** `literal`（G4 判"值来源分叉"）。"""
    for sub in [node.left, *node.comparators]:
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if sub.value.strip() == literal:
                return True
    return False


def _refs_to_condition(tree: ast.AST) -> list[tuple[str, int, str]]:
    """全模块内引用 `_usable_asset_condition` 的位置 `(kind, lineno, 所在函数名)`。

    返回的 `kind` ∈ `{"DEF", "REF(name)", "REF(attr)"}`。

    ⚠️ **必须同时收 `Name` 与 `Attribute`** —— 见模块 docstring 的 G5 一节。
    这是本判据第二个"必须走 AST 而不是字符串"的地方（第一个是 G1 的 docstring 排除）。
    """
    out: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == _CONDITION_NAME:
                out.append(("DEF", node.lineno, node.name))
                continue
            names = {
                s.id
                for s in ast.walk(node)
                if isinstance(s, ast.Name) and _is_load(s)
            }
            attrs = {
                s.attr
                for s in ast.walk(node)
                if isinstance(s, ast.Attribute) and _is_load(s)
            }
            if _CONDITION_NAME in names:
                out.append(("REF(name)", node.lineno, node.name))
            elif _CONDITION_NAME in attrs:
                out.append(("REF(attr)", node.lineno, node.name))
    return out


def _all_exports(tree: ast.AST) -> list[tuple[int, str]]:
    """模块级 `__all__` 里的全部字符串条目 `(lineno, name)`（含 `+` 拼接）。

    ⚠️ 不追 `__all__.extend(...)` / `__all__ += [...]` —— 本仓 0 例实测，
    且若将来出现，`check_all_exports.py`（已挂 CI 门 3）会先报红。
    本判据只负责"这个名字有没有被**导出**"这一个问题，不重复造那个轮子。
    """
    out: list[tuple[int, str]] = []
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            continue
        for sub in ast.walk(value):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                out.append((sub.lineno, sub.value))
    return out


def _only_usable_defaults(tree: ast.AST, rel: str) -> list[tuple[str, int, str]]:
    """G7：全部名为 `only_usable` 的形参 → `(函数名, lineno, 默认值源码)`。"""
    out: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = node.args
        pos = list(a.posonlyargs) + list(a.args)
        for idx, arg in enumerate(pos):
            if arg.arg != _ONLY_USABLE_ARG:
                continue
            n_def = len(a.defaults)
            first_def_idx = len(pos) - n_def
            if idx < first_def_idx:
                out.append((node.name, node.lineno, "(无默认值)"))
            else:
                out.append((node.name, node.lineno, ast.unparse(a.defaults[idx - first_def_idx])))
        for idx, arg in enumerate(a.kwonlyargs):
            if arg.arg != _ONLY_USABLE_ARG:
                continue
            default = a.kw_defaults[idx]
            out.append(
                (node.name, node.lineno, "(无默认值)" if default is None else ast.unparse(default))
            )
    return out


def _docstring_script_refs(tree: ast.AST) -> list[tuple[int, str, str]]:
    """G9：docstring 里点名 `tools/quality/check_*.py` 的位置 `(lineno, name, 所在容器)`。

    ⚠️ **只看 docstring，不看注释与裸字符串**（AST 口径）：
    本仓的脚本里**故意保留**了"曾经用 `tools/quality/check_foo.py` 做判据，
    后来被 X 取代"这类**反例说明**（`check_evidence_scripts.py` 的
    `_strip_comments()` 记过同型的假阳性）。全文正则会把反例当声称。
    限定在 docstring 之后，剩下的假阳性就只能由 `EXEMPT_REFERENCES` 逐条解释
    （理由要可核对）—— 那正是我们要的：**每一条"声称"都必须有人签字**。
    """
    out: list[tuple[int, str, str]] = []
    module_doc = ast.get_docstring(tree, clean=False)
    if module_doc:
        first = tree.body[0] if getattr(tree, "body", None) else None
        lineno = first.value.lineno if isinstance(first, ast.Expr) else 1
        for match in _SCRIPT_REF_RE.finditer(module_doc):
            out.append((lineno, match.group(1), "module"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        doc = ast.get_docstring(node, clean=False)
        if not doc:
            continue
        for match in _SCRIPT_REF_RE.finditer(doc):
            out.append((node.lineno, match.group(1), node.name))
    return out


def _count_symbol_sites(tree: ast.AST, rel: str, skip: set[int]) -> int:
    """全树里两符号出现的**总次数**（Name / Attribute / 非 docstring 字符串）。

    这是 G10 的活性输入。它**与"共现在哪一层"无关** —— 因此不会因为
    "某个 CHECK 从类体搬进函数体"而漂移（那是 G1b 的关切，不是它的）。
    """
    total = 0
    for sub in ast.walk(tree):
        if isinstance(sub, ast.Name) and _is_load(sub) and sub.id == _STATE_SYMBOL:
            total += 1
        elif isinstance(sub, ast.Attribute) and _is_load(sub) and sub.attr == _COL_SYMBOL:
            total += 1
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if id(sub) in skip:
                continue
            if _STATE_SYMBOL in sub.value:
                total += 1
            if _COL_SYMBOL in sub.value:
                total += 1
    return total


def main() -> int:
    print("判据：素材可用性判定必须只有一个拼点（ISS-177 / ISS-95）")
    print()

    files = _iter_py_files()
    print(f"  扫描 {len(files)} 个 .py 文件")
    print()

    all_funcs: list[FuncScan] = []
    non_func_pairs: list[str] = []
    parse_errors: list[str] = []
    refs_by_module: dict[str, list[tuple[str, int, str]]] = {}
    exports_with_condition: list[str] = []
    only_usable: list[tuple[str, str, int, str]] = []
    active_trap: list[str] = []
    script_refs: list[tuple[str, int, str, str]] = []
    symbol_sites = 0

    for path in files:
        rel = str(path.relative_to(REPO_ROOT))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            # 解析失败**必须报红**，不能静默跳过（否则该文件躲过全部判据）。
            parse_errors.append(f"{rel} 无法解析（{type(exc).__name__}: {exc}）")
            continue

        skip = _docstring_nodes(tree)
        symbol_sites += _count_symbol_sites(tree, rel, skip)
        all_funcs.extend(_collect(tree, rel, skip))
        non_func_pairs.extend(_collect_non_functional(tree, rel, skip))

        refs = _refs_to_condition(tree)
        if refs:
            refs_by_module[rel] = refs

        for lineno, name in _all_exports(tree):
            if name == _CONDITION_NAME:
                exports_with_condition.append(f"{rel}:{lineno}（__all__ 导出 {name}）")

        for func_name, lineno, default in _only_usable_defaults(tree, rel):
            only_usable.append((rel, func_name, lineno, default))

        # G8：函数体内**同时**做 ACTIVE 比较 与 持有 supersedes 符号。
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names: set[str] = set()
            attrs: set[str] = set()
            strings: list[str] = []
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and _is_load(sub):
                    names.add(sub.id)
                elif isinstance(sub, ast.Attribute) and _is_load(sub):
                    attrs.add(sub.attr)
                elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    if id(sub) not in skip:
                        strings.append(sub.value)
            text = "".join(strings)
            holds_col = (
                (_COL_SYMBOL in names) or (_COL_SYMBOL in attrs) or (_COL_SYMBOL in text)
            )
            if not holds_col:
                continue
            for cmp_node in _state_compares(node):
                if _compare_uses(cmp_node, _ACTIVE_SYMBOL) or _compare_uses_literal(
                    cmp_node, "ACTIVE"
                ):
                    active_trap.append(
                        f"{rel}:{cmp_node.lineno}（{node.name}）{ast.unparse(cmp_node)[:70]}"
                    )

        for lineno, name, where in _docstring_script_refs(tree):
            script_refs.append((rel, lineno, name, where))

    all_funcs.sort(key=lambda f: (f.rel, f.lineno))
    both, query = _find_condition_func(all_funcs)

    print(f"  持有 `{_STATE_SYMBOL}` 或 `{_COL_SYMBOL}` 的函数：{len(all_funcs)} 个")
    print(f"  其中**同时**持有两者：{len(both)} 个；**再**构造查询：{len(query)} 个")
    print()
    print(f"  {'文件:行':<64} {'函数':<28} {'state':<7} {'列':<5} {'查询':<5} {'查询符号'}")
    print("  " + "-" * 150)
    for func in both:
        print(
            f"  {func.where:<64}"
            f" {func.name:<28}"
            f" {('有' if func.has_state else '无'):<7}"
            f" {('有' if func.has_col else '无'):<5}"
            f" {('有' if func.has_query else '无'):<5}"
            f" {','.join(func.query_names)}"
        )
    print()
    print(f"  不在函数体内的两符号共现：{len(non_func_pairs)} 处")
    for item in non_func_pairs:
        print(f"    · {item}")
    print()

    problems: list[str] = []

    # ---- G1：唯一拼点 ---------------------------------------------------
    g1_bad = len(query) != 1
    print(
        f"  [{'PASS' if not g1_bad else 'FAIL'}] G1 唯一拼点 —— "
        f"『持两符号 + 构造查询』的函数 {len(query)} 个（应恰好 1 个）"
    )
    for func in query:
        print(f"         · {func.where}")
    if g1_bad:
        if len(query) == 0:
            problems.append(
                "找不到任何『同时在函数体里持有两符号、并构造查询』的函数 —— "
                "要么唯一拼点被删/改名，要么它的符号改成了 f-string 插值（静态扫不到）。"
                "⚠️ 这不等于『没有拼点』：请人工确认，不要靠调低阈值让它变绿"
            )
        else:
            problems.append(
                f"出现 {len(query)} 个拼点（应恰好 1 个）："
                + "、".join(f.where for f in query)
                + " —— 两处判断必然分叉，而分叉只表现为『某个入口给错答案』，**不报错**"
            )

    # ---- G1b：结构排除的前提仍然成立 ------------------------------------
    g1b_bad = len(non_func_pairs) < MIN_NON_FUNC_PAIRS
    print(
        f"  [{'PASS' if not g1b_bad else 'FAIL'}] G1b 结构排除的前提（非函数体共现 >= "
        f"{MIN_NON_FUNC_PAIRS}）—— {len(non_func_pairs)} 处"
    )
    if g1b_bad:
        # 消息里**不写**"预期是某某文件"——那是"覆盖范围由人写"（判据纪律第 14 条）。
        # 改成把**实测到的**那几处原样列出来：读消息的人看到的就是判据真正看到的东西。
        observed = "\n".join(f"           · {item}" for item in non_func_pairs) or "           （一处都没有）"
        problems.append(
            f"不在函数体内的两符号共现只剩 {len(non_func_pairs)} 处（下界 {MIN_NON_FUNC_PAIRS}）。\n"
            f"           实测到的：\n{observed}\n"
            "           下界守的是『结构排除的前提仍然成立』：函数体之外必须**还**有共现，"
            "否则说明『非函数体』这个标签已经空了（判据域与函数体域重合）。\n"
            "           若原来那几处被搬进了函数体，G1 的『恰好 1 个』将失去意义"
            "（真因是判据域被污染，而 G1 会报成『出现第二个拼点』）——**两种原因必须分开诊断**"
        )

    # ---- G2：唯一拼点的名字 ---------------------------------------------
    cond_nodes: list[tuple[str, ast.FunctionDef]] = []
    for path in files:
        rel = str(path.relative_to(REPO_ROOT))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        node = _condition_impl(tree)
        if node is not None:
            cond_nodes.append((rel, node))
    g2_ok = len(cond_nodes) == 1 and cond_nodes[0][0] == (query[0].rel if len(query) == 1 else "")
    print(
        f"  [{'PASS' if g2_ok else 'FAIL'}] G2 唯一拼点的名字是 `{_CONDITION_NAME}` —— "
        f"定义 {len(cond_nodes)} 处"
    )
    for rel, node in cond_nodes:
        print(f"         · {rel}:{node.lineno}")
    if not g2_ok:
        problems.append(
            f"名为 `{_CONDITION_NAME}` 的定义有 {len(cond_nodes)} 处（应恰好 1 处，"
            "且与 G1 找到的那个是同一个）—— 改名后 G3~G5 的锚点即失效"
        )

    # ---- G3/G4：取反 + 值来源 -------------------------------------------
    g3_ok = False
    g4_ok = False
    g4_bad_literal: list[str] = []
    g4_bad_active: list[str] = []
    if len(cond_nodes) == 1:
        _, node = cond_nodes[0]
        g3_ok = _has_negated_exists(node)
        compares = _state_compares(node)
        uses_retired_const = any(_compare_uses(c, _STATE_SYMBOL) for c in compares)
        for c in compares:
            if _compare_uses_literal(c, _STATE_LITERAL):
                g4_bad_literal.append(f"line {c.lineno}：{ast.unparse(c)[:70]}")
            if _compare_uses(c, _ACTIVE_SYMBOL) or _compare_uses_literal(c, "ACTIVE"):
                g4_bad_active.append(f"line {c.lineno}：{ast.unparse(c)[:70]}")
        g4_ok = uses_retired_const and not g4_bad_literal and not g4_bad_active

    print(
        f"  [{'PASS' if g3_ok else 'FAIL'}] G3 判定是**取反的存在**（`~exists(...)`）—— "
        f"{'是' if g3_ok else '★否★'}"
    )
    if not g3_ok:
        problems.append(
            f"`{_CONDITION_NAME}` 里找不到『取反的存在判定』（`~exists(...)` / `not_(...)`）"
            " —— 写成 `exists(...)` 会让可用性**整体反向**（可用 ⇔ 存在被接替），"
            "而运行期不报错，只是给出相反的答案"
        )

    print(
        f"  [{'PASS' if g4_ok else 'FAIL'}] G4 `state` 侧用常量 `{_STATE_SYMBOL}`，"
        f"不用 `{_ACTIVE_SYMBOL}` / 字面量 —— "
        f"字面量 {len(g4_bad_literal)} 处、ACTIVE {len(g4_bad_active)} 处"
    )
    for item in g4_bad_literal:
        print(f"         ★ 字面量：{item}")
    for item in g4_bad_active:
        print(f"         ★ ACTIVE：{item}")
    if not g4_ok:
        problems.append(
            f"`{_CONDITION_NAME}` 的 `state` 判据不合规（应为 `== {_STATE_SYMBOL}`）"
            " —— 用 `ACTIVE` 就是本判据存在的理由之一（漏掉『是否已被接替』），"
            "用字面量则让『状态值只有一个来源』失效"
        )

    # ---- G5：调用点全部同模块 + 数量下界 --------------------------------
    call_rows: list[tuple[str, str, int, str]] = []
    for rel, refs in sorted(refs_by_module.items()):
        for kind, lineno, owner in refs:
            call_rows.append((rel, kind, lineno, owner))
    call_sites = [r for r in call_rows if r[1] != "DEF"]
    call_modules = {r[0] for r in call_sites}
    g5_ok = len(call_sites) >= MIN_CALLS and len(call_modules) <= 1
    print(
        f"  [{'PASS' if g5_ok else 'FAIL'}] G5 调用点 >= {MIN_CALLS} 且同模块 —— "
        f"{len(call_sites)} 个，跨 {len(call_modules)} 个模块"
    )
    for rel, kind, lineno, owner in call_rows:
        print(f"         · {kind:<10} {rel}:{lineno}（{owner}）")
    if not g5_ok:
        detail = []
        if len(call_sites) < MIN_CALLS:
            detail.append(f"调用点 {len(call_sites)} 个 < 下界 {MIN_CALLS}（收集器可能失效）")
        if len(call_modules) > 1:
            detail.append(
                f"调用点跨 {len(call_modules)} 个模块：{'、'.join(sorted(call_modules))}"
                " —— 私有入口被绕到别处，或有人在第二个模块里自己拼了一遍"
            )
        problems.append(
            f"`{_CONDITION_NAME}` 的调用点不合规：" + "；".join(detail)
            + "。⚠️ 注意它是 `@staticmethod`，调用点在 AST 上是 **Attribute 不是 Name**，"
            "收集器必须同时收两种（与 `_raise_block` 同坑）"
        )

    # ---- G6：私有性 ------------------------------------------------------
    g6_ok = not exports_with_condition
    print(
        f"  [{'PASS' if g6_ok else 'FAIL'}] G6 `{_CONDITION_NAME}` 未被导出 —— "
        f"{len(exports_with_condition)} 处"
    )
    for item in exports_with_condition:
        print(f"         ★ {item}")
    if not g6_ok:
        problems.append(
            f"`{_CONDITION_NAME}` 出现在某个 `__all__` 里 —— 把『条件』交出去"
            "就等于允许第二处拼点（ISS-95：交出条件比没有条件更危险）"
        )

    # ---- G7：only_usable 默认值 -----------------------------------------
    g7_bad = [row for row in only_usable if row[3] != "True"]
    g7_ok = not g7_bad and len(only_usable) >= MIN_ONLY_USABLE
    print(
        f"  [{'PASS' if g7_ok else 'FAIL'}] G7 `{_ONLY_USABLE_ARG}` 默认值必须是 `True`"
        f"（>= {MIN_ONLY_USABLE} 处）—— {len(only_usable)} 处，异常 {len(g7_bad)} 处"
    )
    for rel, func_name, lineno, default in only_usable:
        mark = "" if default == "True" else "  ★"
        print(f"         · {rel}:{lineno}（{func_name}）默认 = {default}{mark}")
    if not g7_ok:
        detail = []
        if g7_bad:
            detail.append(
                "默认值不是 True："
                + "、".join(f"{r[0]}:{r[2]}（{r[1]}）={r[3]}" for r in g7_bad)
            )
        if len(only_usable) < MIN_ONLY_USABLE:
            detail.append(
                f"只找到 {len(only_usable)} 个 `{_ONLY_USABLE_ARG}` 形参"
                f"（下界 {MIN_ONLY_USABLE}）—— 收集器可能失效"
            )
        problems.append(
            "公开入口的 `only_usable` 默认值不合规：" + "；".join(detail)
            + "。默认必须是**最小**的那一档 —— 拿错时宁可少列，"
            "不可多列一个已作废的素材"
        )

    # ---- G8：全仓不得有 state==ACTIVE 当可用性判据 ----------------------
    g8_ok = not active_trap
    print(
        f"  [{'PASS' if g8_ok else 'FAIL'}] G8 无『`state` 与 ACTIVE 比较 + 持有列符号』"
        f"的组合 —— {len(active_trap)} 处"
    )
    for item in active_trap:
        print(f"         ★ {item}")
    if not g8_ok:
        problems.append(
            f"{len(active_trap)} 处函数体同时做 `state == ACTIVE` 比较与持有 "
            f"`{_COL_SYMBOL}` —— 这正是『只判 state』那个静默错误（漏掉是否已被接替）"
        )

    # ---- G9：docstring 点名的脚本必须存在 --------------------------------
    missing_refs: list[tuple[str, int, str, str]] = []
    for rel, lineno, name, where in script_refs:
        target = f"tools/quality/{name}"
        if (REPO_ROOT / target).is_file():
            continue
        if target in EXEMPT_REFERENCES:
            continue
        missing_refs.append((rel, lineno, name, where))
    g9_ok = not missing_refs
    total_refs = len({f"tools/quality/{n}" for _, _, n, _ in script_refs})
    print(
        f"  [{'PASS' if g9_ok else 'FAIL'}] G9 docstring 点名的判据脚本必须存在 —— "
        f"引用 {total_refs} 个不同脚本，缺失 {len(missing_refs)} 处"
    )
    for rel, lineno, name, where in missing_refs:
        print(f"         ★ {rel}:{lineno}（{where}）点名 `tools/quality/{name}`，但该文件不存在")
    if not g9_ok:
        problems.append(
            f"{len(missing_refs)} 处 docstring 点名了一个**不存在**的判据脚本 —— "
            "**这就是 ISS-177 本体**：「声称自己有防线」比「没有防线」更危险。"
            "处置：要么把脚本写出来，要么把声称改掉（不能只留声称）"
        )

    # ---- G9b：豁免白名单不腐烂 ------------------------------------------
    referenced_targets = {f"tools/quality/{n}" for _, _, n, _ in script_refs}
    stale_exempt: list[str] = []
    for target, reason in EXEMPT_REFERENCES.items():
        if target not in referenced_targets:
            stale_exempt.append(f"{target}（已无任何 docstring 引用它 ⇒ 豁免多余）")
        if len(reason.strip()) < 20:
            stale_exempt.append(f"{target}（豁免理由过短，写不下可核对的事实：{reason!r}）")
    g9b_ok = not stale_exempt
    print(
        f"  [{'PASS' if g9b_ok else 'FAIL'}] G9b 引用豁免白名单不腐烂 —— "
        f"{len(EXEMPT_REFERENCES)} 条，失效 {len(stale_exempt)} 条"
    )
    for item in stale_exempt:
        print(f"         ★ {item}")
    if not g9b_ok:
        problems.append("引用豁免白名单已腐烂：" + "；".join(stale_exempt))

    # ---- 解析错误（不能静默跳过）----------------------------------------
    if parse_errors:
        print(f"  [FAIL] 全部文件可解析 —— {len(parse_errors)} 个失败")
        for err in parse_errors:
            print(f"         ★ {err}")
        problems.extend(parse_errors)

    # ---- G10：活性下界 ---------------------------------------------------
    g10_bad = len(files) < MIN_FILES or symbol_sites < MIN_SYMBOL_SITES
    print(
        f"  [{'PASS' if not g10_bad else 'FAIL'}] G10 活性下界（收集器的输入规模）—— "
        f"文件 {len(files)} >= {MIN_FILES}，两符号出现 {symbol_sites} 次 >= {MIN_SYMBOL_SITES}"
    )
    if g10_bad:
        problems.append(
            f"活性下界未达（文件 {len(files)}/{MIN_FILES}，"
            f"两符号出现 {symbol_sites}/{MIN_SYMBOL_SITES}）—— 判据自身的收集规则可能已失效，"
            "它扫不到东西时会是『全通过』（ISS-143 同族）。"
            "⚠️ 这里刻意**不**用『持两符号的函数数』当下界：那个数依赖"
            "『共现在哪一层』，第一版拿它当下界时因一个错误前提当场假红"
        )

    print()
    if not problems:
        print(
            f"[PASS] 素材可用性判定只有一个拼点"
            f"（{len(query)} 个；调用点 {len(call_sites)} 个同模块；"
            f"`{_ONLY_USABLE_ARG}` 默认全为 True；{total_refs} 个被点名的脚本均存在）。"
        )
        return 0
    print(f"[FAIL] RESULT=FAIL 共 {len(problems)} 处问题：")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    raise SystemExit(0 if main() == 0 else 1)