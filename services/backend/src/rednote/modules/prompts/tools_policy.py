"""工具权限上界：验收原文「**不扩大工具权限**」的唯一落点。

## 这个模块存在的理由

验收原文（04 §4.3）的两个分句里，第二个是**安全约束**：
「不扩大工具权限」。协议 03 §9 第 424 行的原文给了它的完整语义：

> Prompt 全员可管理沿用共享知识策略，但**供应商密钥、系统安全指令及允许工具列表
> 由平台配置管理**，普通模板编辑**不能扩大工具权限或外部访问范围**。

拆开是三条约束：

| # | 约束 | 本模块的落点 |
|---|---|---|
| 1 | `allowed_tools` 只能是平台授予的**子集** | `check_tool_ceiling` |
| 2 | 普通模板编辑**不能**碰系统安全指令 | `PROTECTED_TOOL`（系统安全指令不是工具，见下） |
| 3 | 不能扩大**外部访问范围** | 上界里的出网类工具（`RESEARCH_SEARCH`）由平台决定是否授予 |

## 为什么"判非空"是假防线

最容易写错的实现是：

    if not allowed_tools:  reject()      # ← 只判非空

它**恒真地放行任何填了东西的输入**，于是"不能扩大"退化成"必须填点什么"。
这与 WP19 的 ISS-75（判据被数据库 CHECK 保证恒真 → 防线静默失效）**同源**：
**看起来有判据，实际不起约束作用，而且不报错**。

本模块的判据是**子集关系**（`⊆`），而子集关系**有反例**：
`{"KNOWLEDGE_READ", "ROOT_SHELL"}` 里的 `ROOT_SHELL` 不在上界内 → 必须被拒。
因此它**不可能恒真**，是一个真判据。

## 为什么上界是常量而不是查表

本机没有"平台工具配置"表（协议只说"由平台配置管理"，没说落哪张表）。
用**模块常量**表达上界的理由是：

1. **它是安全边界**，改动应该走代码评审 + ADR，而不是改一行数据；
2. 数据库 CHECK 需要一个**编译期已知的数组**（`allowed_tools <@ ARRAY[...]`）——
   若上界来自表，CHECK 就写不出来，防线只剩 service 层一层。

代价（诚实记录）：上界调整要发版。已登记为待定项 ——
将来若真要配置化，需要一条 ADR 说明"配置表的上界如何保证不被越权修改"。

## 数据库层与这里的关系

本模块是**第一层**（给可读的 422 错误）。**第二层**是 `prompt_versions.allowed_tools`
上的 CHECK 约束 `allowed_tools <@ ARRAY[...]::text[]`（见 `models.py`）。

两层缺一不可：
- 只有 service 层 → 绕过 service 的写入（导入脚本、数据回填、psql）能扩权；
- 只有 CHECK → 用户拿到一个 `sqlstate=23514` 而不知道哪个工具超界。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.prompts.types import PLATFORM_TOOL_CEILING

# 平台配置管理方保留的**系统安全指令**（协议原文的第二个列举项）。
#
# **它不是工具**，所以不在 `PLATFORM_TOOL_CEILING` 里 —— 放在这里是为了
# 让"模板里出现了它"能被显式识别并拒绝，而不是被当成一个未知工具名
# 混在"超出上界"的错误里（那样用户看到的是"TOOL_X 不在允许列表"，
# 而真实原因是"你试图覆盖系统安全指令"，两件事的处置完全不同）。
SYSTEM_PROMPT_DIRECTIVE: Final[str] = "SYSTEM_DIRECTIVE"

# 出网类工具：授予它 = 扩大外部访问范围（协议第三条约束）。
# 单独列出是为了让错误消息能说清"你扩的是外部访问范围，不只是加了个工具"。
EGRESS_TOOLS: Final[frozenset[str]] = frozenset({"RESEARCH_SEARCH"})

# 上界集合（frozenset 便于做子集运算）。
CEILING_SET: Final[frozenset[str]] = frozenset(PLATFORM_TOOL_CEILING)


def exceeded_tools(allowed_tools: tuple[str, ...]) -> tuple[str, ...]:
    """返回超出上界的工具名（**按输入顺序，保持稳定**）。

    返回 tuple 而不是 set：错误消息要按用户输入的顺序列出，
    这样用户能对着自己的输入逐个核对（set 的迭代顺序不保证稳定，
    同一条错误两次报出不同顺序会让人怀疑是不是别的问题）。
    """
    return tuple(name for name in allowed_tools if name not in CEILING_SET)


def added_egress_tools(
    allowed_tools: tuple[str, ...], previous: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """本次**新增**的出网类工具。

    `previous` 是同一模板上一版的 `allowed_tools`（没有则传空）。
    判"新增"而不是"包含"的理由：某模板从第一版起就有 `RESEARCH_SEARCH`
    是平台当初的授予；而**从无到有**地加上它才是"扩大外部访问范围"。
    两者都合规与否由平台决定，但**错误消息必须区分**，否则
    一个本来就合规的模板每次激活都被警告。
    """
    before = set(previous)
    return tuple(
        name for name in allowed_tools if name in EGRESS_TOOLS and name not in before
    )


def contains_system_directive(system_template: str) -> bool:
    """模板文本里是否出现了系统安全指令标记。

    这是**粗判**（子串匹配），刻意如此：
    协议要求的是"普通模板编辑不能改系统安全指令"，而"改"的形态无法穷举。
    本函数只拦最直白的一种 —— 在模板里写 `SYSTEM_DIRECTIVE`。
    真正的防线是**系统安全指令根本不在模板里**（它由平台在调用时注入），
    本函数的作用是**让越界尝试显式失败**，而不是静默通过。

    **不要把它当成完整的注入防护** —— 那是 WP30 的 AI 自检范围。
    登记为待定项：模板文本的注入面需要专门评审。
    """
    return SYSTEM_PROMPT_DIRECTIVE in system_template


def validate_allowed_tools(
    allowed_tools: tuple[str, ...], *, previous: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """校验并归一化 `allowed_tools`，返回问题列表（空 = 通过）。

    **归一化**（去重 + 保持首次出现顺序）在这里做而不是让调用方做：
    同一份输入在 service 与 repository 各归一化一次，两处一旦不一致
    就会出现"库里存的是 A、比较时用的是 B"（ISS-66 的形态）。
    """
    problems: list[str] = []

    exceeded = exceeded_tools(allowed_tools)
    if exceeded:
        problems.append(
            "allowed_tools 超出平台授予范围：" + ", ".join(exceeded)
        )

    added = added_egress_tools(allowed_tools, previous)
    if added:
        problems.append(
            "本次新增了出网类工具，属于扩大外部访问范围，需平台配置变更："
            + ", ".join(added)
        )

    return tuple(problems)


def normalize_tools(allowed_tools: tuple[str, ...]) -> tuple[str, ...]:
    """去重并保持首次出现顺序。"""
    seen: dict[str, None] = {}
    for name in allowed_tools:
        seen.setdefault(name, None)
    return tuple(seen)


__all__ = [
    "CEILING_SET",
    "EGRESS_TOOLS",
    "SYSTEM_PROMPT_DIRECTIVE",
    "added_egress_tools",
    "contains_system_directive",
    "exceeded_tools",
    "normalize_tools",
    "validate_allowed_tools",
]
