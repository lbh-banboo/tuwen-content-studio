"""渲染校验：`system_template` 的占位符必须与 `input_schema` 声明的字段一致。

**为什么这是一个独立的纯函数模块**：协议 03 §9 第 12 行原文只有四个字
「**需校验可渲染**」，没有说渲染引擎是什么、占位符长什么样、与谁一致。
本模块把这四个字落成一条**可单测的判据**：

> `system_template` 里的占位符集合 **⊆** `input_schema` 声明的字段集合。

**为什么不碰数据库**：判据本身是纯字符串运算，把它写成"接受两个 dict 返回问题列表"
的形式，就能用单元测试穷举边界（空模板、多余占位符、多余字段、嵌套路径、重复占位符），
而**不需要起数据库**。服务层只负责把库里的值喂进来。

---

## 判据为什么是"⊆"而不是"="

两个方向的含义完全不同：

| 方向 | 是否报错 | 为什么 |
|---|---|---|
| 模板里**有**占位符、schema 里**没**这个字段 | **必须报错** | 生成时取不到值 → 提示词里留下字面量 `{{x}}` 或抛异常。**这是真实的失败** |
| schema 里**有**字段、模板里**没**用 | **不报错** | 模板可以只使用 schema 的一部分（例如 `output_schema` 需要的字段、或按任务分支）。报错会迫使模板作者声明用不到的字段 |

**只判一个方向是本包刻意为之**，并且测试要**同时覆盖两个方向**
（只断言"该拦的拦住了"会漏掉"不该拦的拦住了"，反之亦然 ——
这正是技能里"活性验证必须成对"的要求）。

## 为什么占位符用 `{{x}}` 而不是 `{x}`

见 `types.py` 的说明：单花括号与 JSON 字面量冲突（提示词里常带 JSON 示例），
`%s` / `$1` 与 psycopg 的 paramstyle 冲突（WP17 ISS-55 踩过）。
`{{x}}` 在提示词文本里几乎不会自然出现。

## 未闭合的占位符**不报错**，但会返回

`{{x` 这种（有开无闭）在文本里**很可能是作者写错了**。本模块的处理是：
把它算作一个**独立的发现项**（`unclosed`），由服务层决定是否拒绝。
**不静默忽略** —— 静默忽略会让"写错了但看起来没写错"通过校验，
而这正是本项目反复踩到的"静默失效"形态。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

from rednote.modules.prompts.types import (
    PLACEHOLDER_CLOSE,
    PLACEHOLDER_NAME_PATTERN,
    PLACEHOLDER_OPEN,
)

# 完整的占位符：`{{name}}`。名字的字符集见 types 的说明。
_PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(
    re.escape(PLACEHOLDER_OPEN)
    + f"({PLACEHOLDER_NAME_PATTERN})"
    + re.escape(PLACEHOLDER_CLOSE)
)

# 有开无闭的残留（`{{` 后面直到文本末尾都没有 `}}`）。
# 用非贪婪 + 负向断言表达"这个 `{{` 不是任何完整占位符的开头"：
# 先去掉全部完整占位符，再看剩下的文本里还有没有 `{{`。
_OPEN_ONLY_RE: Final[re.Pattern[str]] = re.compile(re.escape(PLACEHOLDER_OPEN))


def extract_placeholders(system_template: str) -> tuple[str, ...]:
    """取出模板里的占位符名字，**按首次出现顺序去重**。

    返回 tuple（有序）而不是 frozenset：错误消息要能指出"第一个出问题的占位符"，
    而 set 的迭代顺序不保证稳定 —— 同一条坏模板两次报出不同的名字会让排障困惑。
    """
    seen: dict[str, None] = {}
    for match in _PLACEHOLDER_RE.finditer(system_template):
        seen.setdefault(match.group(1), None)
    return tuple(seen)


def find_unclosed_placeholders(system_template: str) -> tuple[int, ...]:
    """找出"有开无闭"的 `{{` 的**字符位置**（去掉了完整占位符之后再找）。

    返回位置而不是计数：位置能直接指给作者看（"第 137 个字符处"），
    而计数在文本很长时帮助有限。
    """
    # 先把完整占位符替换成等长空白，保持位置不变 —— 这样返回的位置
    # 与原文的偏移一致（用 `sub` 删掉会让后面的位置整体前移）。
    masked = _PLACEHOLDER_RE.sub(lambda m: " " * len(m.group(0)), system_template)
    return tuple(match.start() for match in _OPEN_ONLY_RE.finditer(masked))


def declared_fields(input_schema: dict[str, Any]) -> tuple[str, ...]:
    """从 `input_schema` 里取出**声明了的字段路径**。

    支持的形状（本包只定义这两种，其余形态显式忽略而不是猜）：

    | 形状 | 取法 |
    |---|---|
    | `{"properties": {"a": {...}, "b": {...}}}` | 取 `properties` 的键 —— JSON Schema 的标准写法 |
    | `{"fields": ["a", "b"]}` | 取数组元素 —— 简写形式 |

    **为什么只支持两种**：`input_schema` 是**模板作者写给系统看的**，
    不是通用 JSON Schema 校验器。支持任意 JSON Schema 意味着要引入一个
    Schema 校验库并处理 `$ref`/`allOf` 等组合 —— 而本包只需要回答
    "模板用了哪些字段、这些字段声明了没有"。**够用即止，不猜**。

    嵌套路径（`product.model_code`）以**点号扁平名**参与比较：
    `properties` 里写 `{"product.model_code": {...}}` 与
    `fields` 里写 `"product.model_code"` 等价。
    """
    if not isinstance(input_schema, dict):
        return ()

    properties = input_schema.get("properties")
    if isinstance(properties, dict):
        return tuple(str(key) for key in properties)

    fields = input_schema.get("fields")
    if isinstance(fields, list):
        return tuple(str(item) for item in fields)

    return ()


@dataclass(frozen=True, slots=True)
class RenderCheck:
    """渲染校验的结果。

    **`problems` 与 `unclosed` 分开**：
    · `problems` 是**确定要拒绝**的（占位符未声明）；
    · `unclosed` 是**疑似写错**（有开无闭），由调用方决定是否拒绝。
    把两者混成一个列表会让"确定"与"疑似"失去区分，
    而它们的处置不同（前者必须 422，后者可以选择只告警）。
    """

    placeholders: tuple[str, ...] = ()
    declared: tuple[str, ...] = ()
    undeclared: tuple[str, ...] = ()
    unused: tuple[str, ...] = ()
    unclosed: tuple[int, ...] = ()
    problems: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        """**只判 `problems`** —— 未闭合占位符不在这里。

        调用方若要更严（把 `unclosed` 也算失败），显式判 `not check.unclosed`。
        刻意不把两者合成一个 `ok`：那样会让"疑似"悄悄变成"确定"，
        而反过来（把确定降级成疑似）更危险。**默认从严，例外要显式写。**
        """
        return not self.problems


def check_renderable(
    system_template: str, input_schema: dict[str, Any]
) -> RenderCheck:
    """判据主体：占位符 ⊆ 声明字段。

    不抛异常、不返回 bool —— 返回一个带全部细节的结果对象。
    理由：服务层需要把"哪个占位符没声明"写进 422 的 `details`，
    而只返回 bool 会迫使服务层**再算一遍**（两处实现同一判据 = ISS-66 的形态）。
    """
    placeholders = extract_placeholders(system_template)
    declared = declared_fields(input_schema)
    declared_set = set(declared)

    undeclared = tuple(name for name in placeholders if name not in declared_set)
    used_set = set(placeholders)
    unused = tuple(name for name in declared if name not in used_set)
    unclosed = find_unclosed_placeholders(system_template)

    problems: list[str] = []
    if undeclared:
        problems.append(
            "模板使用了 input_schema 未声明的占位符："
            + ", ".join(undeclared)
        )
    if not system_template.strip():
        problems.append("system_template 为空")

    return RenderCheck(
        placeholders=placeholders,
        declared=declared,
        undeclared=undeclared,
        unused=unused,
        unclosed=unclosed,
        problems=tuple(problems),
    )


__all__ = [
    "RenderCheck",
    "check_renderable",
    "declared_fields",
    "extract_placeholders",
    "find_unclosed_placeholders",
]
