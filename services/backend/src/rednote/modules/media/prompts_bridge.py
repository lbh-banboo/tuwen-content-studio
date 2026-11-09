"""WP27 的第一条接缝：从 `prompts` 模块解析静态图 `task_kind` 的激活提示词版本。

**与 `content/prompts_bridge.py` 的关系：同构不同域。** WP26 已经把"按
`task_kind` 取当前激活版本"这件事在 `prompts`侧组合出来了（`list_templates`
→ `active_version_id` → `get_version`），而 `prompts`（WP23）**没有**这个
公开入口，**M2 接缝冻结铁律又不允许改它的签名**。

那为什么不直接把 `content.prompts_bridge` 拿来用？因为 `media` 与 `content`
是**平级业务模块**（都只依赖 `types` / `platform`，互不依赖）。跨平级模块
import 会把"改 content 的提示词解析"变成"能改到 media 的出网行为" ——
而这种耦合**不会被任何判据抓到**（两个模块各自的测试都过）。这与
`media/schemas.py::SchemaViolation` 的 docstring 是同一条理由。

因此本文件**复制形态、不共享代码**：错误码换成 `media/types.py` 的
`TASK_KIND_UNKNOWN` / `PROMPT_TEMPLATE_MISSING` / `PROMPT_NOT_ACTIVATED`，
取值域换成 `media/types.TASK_KINDS`。

## 静态图侧与文案侧的三处**真实差异**（不是重写理由，是事实差异）

1. **渲染出来的提示词要给图片模型看**，因此 `values` 里必然携带一张
   `input_schema` 没有声明的字段（如"参考素材说明"）。`render_system_prompt`
   只判 `⊆`方向（模板要的都有），多给的不报错 —— 这条与文案侧同源，
   不因静态图而改。
2. **静态图的 `output_schema` 是"这一版对图片产物的声明"**，而图片产物的
   字节**不在模型返回里**（见 `gateway_bridge` 的 OP-195 说明）⇒
   `output_schema` 在本包里**只用于登记与对账**，不用于"逐字段校验返回值"。
   这与文案侧 `R1` 的用法不同，是真实的语义差异，不是取舍。
3. **本包不 import `gateway`**：与文案侧同。本地导入 `gateway.service.dispatch_call`
   的落点是 `gateway_bridge.py`。

## 为什么仍然 `list_templates(..., limit=1)`

与文案侧同一取舍（WP26 说明文档 §1.4 第 6 条逐字指定 `limit=1`）。两个隐含
前提（同一 `task_kind` 下预期只有一条模板 / "最新"即"该用的"）任一不成立时，
**本模块的行为是"拒绝生成"而不是"用错模板"** —— 静态图出网是真实花钱的调用，
用错模板的后果比拒绝严重。前提已登记为待复核项（OP-161 一族）。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from rednote.modules.media.types import (
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
# **模式串与 `prompts/render.py` 的私有 `_PLACEHOLDER_RE` 相同**：上游没导出
# 那个编译对象，而"占位符长什么样"的权威定义是 `prompts/types.py` 的三个常量
# （本文件 import 它们来拼）。**单源在 `prompts/types.py`，不在本文件。**
#
# **为什么用单次 `re.sub` 而不是 `str.replace` 逐个换**：`str.replace` 会对
# **已被替换进去的值**再扫一遍 —— 若某个值里含 `{{other}}`，第二次
# `replace("{{other}}", ...)` 会连值里的那份一起换掉（链式替换）。静态图侧
# 尤其危险：`values` 里的"参考素材说明"是**运营手写的自由文本**，含 `{{` 的
# 概率显著高于文案侧。
_PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(
    re.escape(PLACEHOLDER_OPEN)
    + f"({PLACEHOLDER_NAME_PATTERN})"
    + re.escape(PLACEHOLDER_CLOSE)
)


class PromptUnavailable(ValueError):
    """解析不到可用的激活提示词版本。

    **继承 `ValueError`（不是 `RuntimeError`）**：与 `schemas.SchemaViolation`
    对齐 —— 调用方在一条 `except ValueError` 里同时接住"生成请求载荷不合法"
    与"提示词没配"，靠 `reason` 区分。

    **`.reason` 取值域**就是 `media/types.py` 里既有的三个码，本模块**不新造**：
    `TASK_KIND_UNKNOWN` / `PROMPT_TEMPLATE_MISSING` / `PROMPT_NOT_ACTIVATED`。

    **刻意不继承 `ApiError`**：它不代表"用户操作不对"，而是"这个部署的提示词
    还没配好"。重试一万次也不会让模板长出来，因此 `service` 侧要折成一个
    **任务级**失败（`retryable=False`）。
    """

    def __init__(self, *, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ActivePromptVersion:
    """`task_kind` 当前生效的提示词版本（**只读快照**）。

    **为什么 6 个字段一起返回而不是只给 `version_id`**：调用方需要
    `template_id` / `version_id` 去落 `media_generation_contexts`，需要
    `system_template` 去渲染，需要 `input_schema` 去判"值够不够"，而
    `output_schema` 是这一版对图片产物的**声明**（落 `media_generation_contexts`
    之外的登记与对账要用它）。再回一次库取这些字段会让"解析到的版本"与
    "渲染用的版本"分属两次读 —— 同一条快照在两次读之间被换版的话，会渲染出与
    `media_generation_contexts` 记的不是同一版。

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
    """解析静态图 `task_kind` 的激活提示词版本；任何一步查不到即fail-closed。

    四道门（**顺序固定**，每一道给出各自的 `reason`）：

    | # | 条件 | `reason` | 为什么这样归类 |
    |---|---|---|---|
    | 1 | `task_kind` 不在 `media/types.TASK_KINDS` | `TASK_KIND_UNKNOWN` | 拼错的 kind 若放行到 `list_templates`，会静默返回空列表、报成"没有模板"—— 用户以为"没配"，实际是"拼错了" |
    | 2 | 没有该 `task_kind` 的模板行 | `PROMPT_TEMPLATE_MISSING` | 真的没建模板 |
    | 3 | 有模板行但 `active_version_id IS NULL` | `PROMPT_NOT_ACTIVATED` | 建了但没激活 |
    | 4 | `active_version_id` 非空但 `get_version` 返回 `None` | `PROMPT_TEMPLATE_MISSING` | 投影指向的版本行不存在 ⇒ 数据不一致。用 `MISSING` 而不是 `NOT_ACTIVATED`：**它不是"没激活"，是"该行没了"**，排障方向完全不同 |

    第 4 道之后**还有一道**（不占编号，因为它不是"查不到"而是"查到了但不可用"）：
    `check_renderable` 的 `problems` 或 `unclosed` 非空 ⇒ 同样抛
    `PROMPT_TEMPLATE_MISSING`。理由：模板里留着未声明的占位符（取不到值）或
    未闭合的 `{{` 时，**渲染出来的提示词会带字面量**，而"提示词里混进 `{{x}}`
    字面量"与"没有提示词"一样不安全。`render.py` 把 `unclosed` 归为"疑似"并
    由调用方决定是否拒绝 —— 本模块**显式拒绝**，因为这条路径通向一次**真实
    出网调用**。

    `task_kind` 只做**存在性**校验（∈ `TASK_KINDS`），不另设"本包支持哪些"的
    名单：`TASK_KINDS` 就是本包的取值域，两者同源（铁律 14：覆盖范围由常量
    决定，不由人写）。
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
                "本包 fail-closed：没有模板就不出图（说明文档 §1.4 第 6 条 / §2.6 R6）。"
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
                "（active_version_id 为空）。本包 fail-closed：没有激活版本就不出图。"
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
                f"提示词版本 {version.id} 不可渲染，拒绝出图。"
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

    三个刻意的取舍（与文案侧同源）：

    1. **只判"模板用到的占位符有没有值"（`⊆`），不判"值有没有被用上"。**
       与 `prompts/render.py` 的判据方向保持同一口径：多给了值不算错，少给了
       值必须拒。两个方向若各写一套，就会出现"校验说可渲染、渲染时又缺值"的分叉。
    2. **缺值抛 `PROMPT_TEMPLATE_MISSING` 而不是 `KeyError`。** `KeyError`
       **不是 `ValueError` 子类**，会被调用方的 `except ValueError` 漏掉变成
       500。归类为"提示词不可用"是准确的：**这一版模板要求的值，上游上下文
       给不出来** —— 是模板与调用方不匹配，不是用户输入错。
    3. **不在这里再查 `unclosed`。** 那是 `resolve_active_version` 的门。
       两处都查就是"同一判据两处实现"。**若测试直接构造 `ActivePromptVersion`
       调本函数，请自行保证模板已过 `resolve_active_version`。**

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