"""WP26 的第一条接缝：从 `prompts` 模块解析 `task_kind` 的激活提示词版本。

**为什么需要这个文件**（说明文档 §1.4 第 6 条）：上游 `prompts`（WP23）**没有**
"按 `task_kind` 取当前激活版本"的公开入口 —— `prompts/service.py` 的 8 个公开
函数里没有它；`prompts/repository.py` 有 `find_template_by_name(task_kind, name)`
与 `list_templates(task_kind=...)`，但两者都没有"取激活版本"这一步。

**处置：本包不修改 WP23 的签名**（M2 接缝冻结铁律），改为在 `content/` 侧写一个
**只读的组合**：`list_templates(task_kind=X, limit=1)` → 取 `active_version_id`
→ `get_version(...)`。查不到激活版本一律 **fail-closed**（抛 `PromptUnavailable`），
即**拒绝生成、不出网** —— 而不是"用空提示词也能跑"（§2.6 R6）。

## 三条本模块特有的约定

1. **只读。** 本模块不写 `prompt_templates` / `prompt_versions`，也不激活版本。
   "有没有激活版本"是 WP23 的事（OP-161 未关闭，见 §1.5「提示词模板的种子数据」）。
   本模块只回答"现在生效的是哪一版"。

2. **失败是 `ValueError` + `.reason`，不是新异常族。** `schemas.SchemaViolation`
   的 docstring 已经指定了这条约定：调用方（`gateway_bridge` → `service`）会在
   一条 `except ValueError` 里把"模型输出不合法"与"提示词版本没配"分开处理，
   **两者的区分靠 `reason`，不靠类型**。本模块沿用 —— `PromptUnavailable` 继承
   `ValueError`，携带 `.reason`（取值全部来自 `content/types.py` 的错误码，
   本模块**不新造码**）。

3. **占位符替换在本模块实现，不在 `prompts/render.py`。** 上游的 `render.py`
   **只做"可渲染性校验"**（占位符 ⊆ 声明字段），**不做替换**。本模块调用它的
   `extract_placeholders` / `check_renderable` 做校验，替换由本模块实现。
   **定界与命名规则一律取自 `prompts/types.py` 的常量**（`PLACEHOLDER_OPEN` /
   `PLACEHOLDER_CLOSE` / `PLACEHOLDER_NAME_PATTERN`），不在本模块重写一套 ——
   否则就是本仓 ISS-66「同一判据两处实现」的形态。

## 为什么 `list_templates(..., limit=1)` 而不是"扫到第一个有激活版本的"

说明文档 §1.4 第 6 条**逐字指定了** `limit=1`。本模块照做，并在此把它的隐含
前提写清楚，供复核：

| 前提 | 若不成立会怎样 |
|---|---|
| 同一 `task_kind` 下**预期只有一条**模板（`uq_prompt_templates_kind_name` 允许同 kind 多条不同名，但本包消费方约定单一激活模板） | 最新的那条若没激活，即使更早的那条有激活版本，本模块也会 fail-closed |
| 按 `created_at DESC` 取最新 | 若"最新"不是"该用的"，会解析到错误的提示词 |

两个前提的任何一条不成立 ⇒ **本模块的行为是"拒绝生成"而不是"用错模板"** ——
这是刻意的：`R6` 的取向是"默认必须是最小的那一档"（与 WP23 的
`PLATFORM_DEFAULT_TOOLS = ()` 同一取向）。**该前提已登记为待复核项**（附录 A5
同族：本包定义的形状需在产品/协议复核时逐条确认）。

## 本模块与 R7（顶层 import 网关）的关系

**本模块不 import `gateway`**：它只碰 `prompts` 的模型与纯函数，都是轻模块。
"本地导入 `gateway.service.dispatch_call`" 的落点在 `gateway_bridge.py`。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from rednote.modules.content.types import (
    REASON_PROMPT_NOT_ACTIVATED,
    REASON_PROMPT_TEMPLATE_MISSING,
    REASON_TASK_KIND_UNKNOWN,
    TASK_KINDS,
)
from rednote.modules.prompts.render import check_renderable, extract_placeholders
from rednote.modules.prompts.repository import PromptRepository
from rednote.modules.prompts.types import (
    PLACEHOLDER_CLOSE,
    PLACEHOLDER_NAME_PATTERN,
    PLACEHOLDER_OPEN,
)

# 完整的占位符 `{{name}}`。
#
# **与 `prompts/render.py` 的私有 `_PLACEHOLDER_RE` 是同一个模式串**：上游没有
# 导出那个编译对象，而"占位符长什么样"的权威定义仍然是 `prompts/types.py` 的
# 三个常量（本文件 import 它们来拼）。重复的只是 5 行拼接，模式串本身单源。
#
# **为什么用正则一次替换而不是 `str.replace` 逐个换**：`str.replace` 会对
# **已被替换进去的值**再扫一遍 —— 若某个值里含 `{{other}}`，第二个
# `replace("{{other}}", ...)` 会连值里的那份一起换掉（链式替换）。单次
# `re.sub` 扫一遍原文、回调取值，不会链式触发。
_PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(
    re.escape(PLACEHOLDER_OPEN)
    + f"({PLACEHOLDER_NAME_PATTERN})"
    + re.escape(PLACEHOLDER_CLOSE)
)


class PromptUnavailable(ValueError):
    """解析不到可用的激活提示词版本。

    **继承 `ValueError`（不是 `RuntimeError`）**：与 `schemas.SchemaViolation`
    对齐 —— 调用方在一条 `except ValueError` 里同时接住"模型输出不合法"与
    "提示词没配"，靠 `reason` 区分。取值见本文件各 `raise` 处。

    **`.reason` 的取值域**就是 `content/types.py` 里既有的三个码，
    本模块不新造：`TASK_KIND_UNKNOWN` / `PROMPT_TEMPLATE_MISSING` /
    `PROMPT_NOT_ACTIVATED`。

    **刻意不继承 `ApiError`**（与 `brief/decomposer.py:79` 的
    `DecomposerUnavailable` 同一理由）：它不代表"用户操作不对"，
    而是"这个部署的提示词还没配好"。重试一万次也不会让模板长出来，
    因此 `service` 侧要折成一个**任务级**失败（`retryable=False`）。
    与 `DecomposerUnavailable` 的区别只在**语言**：那个说"模型通道不可用"，
    这个说"提示词没配"；都是"部署没配好"，都不是用户的错。
    """

    def __init__(self, *, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ActivePromptVersion:
    """`task_kind` 当前生效的提示词版本（**只读快照**）。

    **为什么把 6 个字段一起返回而不是只给 `version_id`**：调用方需要
    `template_id` / `version_id` 去落 `generation_contexts`（OP-160 的落点），
    需要 `system_template` 去渲染，需要 `input_schema` 去判"值够不够"，
    而 `output_schema` 是这一版对模型输出的**声明**（§1.6 R1 的"逐字段校验"
    以它为准，下游 WP30/WP33 也读它）。再回一次库取这些字段会让
    "解析到的版本"与"渲染用的版本"分属两次读 —— 同一条 `ActivePromptVersion`
    在两次读之间被换版的话，会渲染出与 `generation_contexts` 记的不是同一版。

    `input_schema` / `output_schema` 用 `Mapping` 而不是 `dict`：它们是
    `JSONB` 列读出来的**不可变快照**，本模块不修改它们。
    """

    template_id: uuid.UUID
    version_id: uuid.UUID
    task_kind: str
    system_template: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]


def resolve_active_version(
    prompt_repo: PromptRepository,
    *,
    task_kind: str,
) -> ActivePromptVersion:
    """解析 `task_kind` 的激活提示词版本；任何一步查不到即 fail-closed。

    四道门（**顺序固定**，每一道给出各自的 `reason`）：

    | # | 条件 | `reason` | 为什么这样归类 |
    |---|---|---|---|
    | 1 | `task_kind` 不在 `content/types.TASK_KINDS` | `TASK_KIND_UNKNOWN` | 拼错的 kind 若放行到 `list_templates`，会静默返回空列表、报成"没有模板"—— 用户以为"没配"，实际是"拼错了"（与 WP23 把 `task_kind` 落 CHECK 的理由同源） |
    | 2 | 没有该 `task_kind` 的模板行 | `PROMPT_TEMPLATE_MISSING` | 真的没建模板 |
    | 3 | 有模板行但 `active_version_id IS NULL` | `PROMPT_NOT_ACTIVATED` | 建了但没激活（WP23 的 `active_version_id` 是**投影**，权威在 `prompt_publications` 事件流） |
    | 4 | `active_version_id` 非空但 `get_version` 返回 `None` | `PROMPT_TEMPLATE_MISSING` | 投影指向的版本行不存在 ⇒ 数据不一致。用 `MISSING` 而不是 `NOT_ACTIVATED`：**它不是"没激活"，是"该行没了"**，排障方向完全不同（查 FK / 触发器的写入路径，不是查发布记录） |

    第 4 道之后**还有一道**（不占表里的编号，因为它不是"查不到"而是"查到了但不可用"）：
    `check_renderable` 的 `problems` 或 `unclosed` 非空 ⇒ 同样抛
    `PROMPT_TEMPLATE_MISSING`。理由：模板里留着未声明的占位符（取不到值）或
    未闭合的 `{{`（大概率是作者写错）时，**渲染出来的提示词会带字面量**，
    而"提示词里混进 `{{x}}` 字面量"与"没有提示词"一样不安全。
    `render.py` 把 `unclosed` 归为"疑似"并由调用方决定是否拒绝 —— 本模块
    **显式拒绝**，因为这条路径通向一次**真实出网调用**，不能带着疑似缺陷发出去。

    `task_kind` 只做**存在性**校验（∈ `TASK_KINDS`），不做"本包支持哪些"的
    二次判断：`TASK_KINDS` 就是本包的取值域，两者同源，不另设名单（铁律 14：
    覆盖范围由磁盘/常量决定，不由人写）。
    """
    if task_kind not in TASK_KINDS:
        raise PromptUnavailable(
            reason=REASON_TASK_KIND_UNKNOWN,
            message=(
                f"未知的生成类型：{task_kind!r}（本包只消费 {sorted(TASK_KINDS)}）。"
            ),
        )

    templates = prompt_repo.list_templates(task_kind=task_kind, limit=1, offset=0)
    if not templates:
        raise PromptUnavailable(
            reason=REASON_PROMPT_TEMPLATE_MISSING,
            message=(
                f"没有 task_kind={task_kind!r} 的提示词模板。"
                "本包 fail-closed：没有模板就不生成（说明文档 §1.4 第 6 条 / §2.6 R6）。"
                "首次部署须先配置该 task_kind 的模板并激活一版（承 OP-161）。"
            ),
        )

    template = templates[0]
    version_id = template.active_version_id
    if version_id is None:
        raise PromptUnavailable(
            reason=REASON_PROMPT_NOT_ACTIVATED,
            message=(
                f"task_kind={task_kind!r} 的模板 {template.id} 尚无激活版本"
                "（active_version_id 为空）。本包 fail-closed：没有激活版本就不生成。"
            ),
        )

    version = prompt_repo.get_version(version_id)
    if version is None:
        raise PromptUnavailable(
            reason=REASON_PROMPT_TEMPLATE_MISSING,
            message=(
                f"模板 {template.id} 的 active_version_id={version_id} 指向的版本行不存在。"
                "这是数据不一致（投影指向了不存在的 prompt_versions 行），"
                "排障方向是写入路径与 FK，不是发布记录。"
            ),
        )

    render_check = check_renderable(version.system_template, version.input_schema)
    if render_check.problems or render_check.unclosed:
        raise PromptUnavailable(
            reason=REASON_PROMPT_TEMPLATE_MISSING,
            message=(
                f"提示词版本 {version.id} 不可渲染，拒绝生成。"
                f"problems={list(render_check.problems)}，"
                f"unclosed_positions={list(render_check.unclosed)}。"
            ),
        )

    return ActivePromptVersion(
        template_id=template.id,
        version_id=version.id,
        task_kind=task_kind,
        system_template=version.system_template,
        input_schema=dict(version.input_schema),
        output_schema=dict(version.output_schema),
    )


def render_system_prompt(
    prompt: ActivePromptVersion,
    *,
    values: Mapping[str, str],
) -> str:
    """把 `{{name}}` 换成 `values[name]`；**缺值即 fail-closed**。

    三个刻意的取舍：

    1. **只判"模板用到的占位符有没有值"（`⊆`），不判"值有没有被用上"。**
       与 `prompts/render.py` 的判据方向保持**同一口径**：多给了值不算错
      （`values` 可以带上游用不到的字段），少给了值必须拒。两个方向若各写一套，
       就会出现"校验说可渲染、渲染时又缺值"的分叉。

    2. **缺值抛 `PROMPT_TEMPLATE_MISSING` 而不是 `KeyError`。** `KeyError` 会
       被调用方的 `except ValueError` 漏掉（`KeyError` 不是 `ValueError` 子类），
       变成 500。归类为"提示词不可用"是准确的：**这一版模板要求的值，
       上游上下文给不出来** —— 是模板与调用方不匹配，不是用户输入错。

    3. **不在这里再查 `unclosed`。** 那是 `resolve_active_version` 的门，
       本函数收到的 `ActivePromptVersion` 假定已过那道门。两处都查会让
       "同一判据两处实现"（ISS-66 形态）。**若测试直接构造 `ActivePromptVersion`
       调本函数，请自行保证模板已过 `resolve_active_version`** —— 测试若要验
       "未闭合被拒"，应打 `resolve_active_version`。

    值一律按 `str(value)` 处理：模型提示词是文本，非字符串值在这里转成文本，
    不在这里做类型判定（类型判定是 `schemas` 的活）。
    """
    names = extract_placeholders(prompt.system_template)
    missing = tuple(name for name in names if name not in values)
    if missing:
        raise PromptUnavailable(
            reason=REASON_PROMPT_TEMPLATE_MISSING,
            message=(
                f"提示词版本 {prompt.version_id} 需要这些值但上游没给：{list(missing)}。"
                f"模板声明的字段：{sorted(prompt.input_schema)}。"
            ),
        )

    def _substitute(match: re.Match[str]) -> str:
        return str(values[match.group(1)])

    return _PLACEHOLDER_RE.sub(_substitute, prompt.system_template)


__all__ = [
    "ActivePromptVersion",
    "PromptUnavailable",
    "render_system_prompt",
    "resolve_active_version",
]
