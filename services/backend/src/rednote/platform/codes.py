"""展示编号生成（需求 §4.1）。

需求 §4.1：`项目编号 | 系统生成 | 全局唯一、不可变`。
**需求没有规定格式** —— 这里定一个并把理由记下来（属需求缺口，已登记）。

格式：`{PREFIX}-{YYYYMM}-{6 位 Crockford Base32}`
例：`PRJ-202609-7K3M2Q`

三个选择理由：

1. **带年月**：人看编号就能判断项目大概什么时候开的。
   排障与对账时这一点比"纯流水号"有用得多。
2. **6 位 base32 ≈ 30 bit ≈ 10.7 亿组合**：即使每月新增 1 万个项目，
   同月碰撞概率仍可忽略；且下方有唯一约束兜底 + 重试，不依赖概率。
3. **Crockford Base32 字母表去掉 I/L/O/U**：
   编号是要被人在邮件、工单、文件名里传抄的。
   常规 base32 里 `1/I/L` 与 `0/O` 手抄必错，Crockford 变体把
   I/L 归一为 1、O 归一为 0，从字母表层面消除歧义。

**编号必须文件系统安全**：需求 §3.x 规定交付包命名为
`项目编号_批次编号_导出时间/` —— 编号会直接进目录名。
本字母表只含大写字母与数字，不含空格、点、斜杠、冒号，可直接使用。

**为什么不用"月份 + 递增序号"**：那需要一个"当月已用最大序号"的读取，
在并发创建下要么锁表、要么容忍跳号；而跳号又与"编号连续好看"的诉求冲突。
随机后缀 + 唯一约束重试在并发下天然正确，且不需要任何额外状态。
"""

from __future__ import annotations

import secrets
from datetime import date
from typing import Final

# Crockford Base32：不含 I、L、O、U。
CODE_ALPHABET: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

DEFAULT_SUFFIX_LENGTH: Final[int] = 6

# 实体前缀。集中定义避免各处硬编码（content_code 等在后续工作包按需追加）。
PROJECT_CODE_PREFIX: Final[str] = "PRJ"


def random_code_suffix(length: int = DEFAULT_SUFFIX_LENGTH) -> str:
    """生成随机后缀。

    用 `secrets` 而不是 `random`：编号出现在 URL、文件名、对外沟通里，
    可预测的编号会让"枚举别人的项目编号"变得容易。
    这与"非成员返回 404 防枚举"是同一个考虑。
    """
    if length <= 0:
        raise ValueError("length 必须为正整数")
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def generate_display_code(
    prefix: str,
    *,
    today: date | None = None,
    suffix: str | None = None,
) -> str:
    """生成展示编号。

    `today` 与 `suffix` 可注入，是为了让测试能断言确定性的输出 ——
    否则测"前缀与年月格式正确"就得用正则去模糊匹配，测不出真实约束。
    """
    moment = today or date.today()

    normalized = prefix.strip().upper()
    # 前缀校验分两步，缺一不可：
    #   1. 逐字符只允许 ASCII 字母数字与连字符 —— 编号会进目录名与 URL，
    #      非 ASCII 会让"文件系统安全"这句承诺依赖平台；
    #   2. 至少有一个字母数字 —— 只由连字符组成的"前缀"能通过第 1 步，
    #      但生成出来的编号以连字符开头（如 `--202609-XXXXXX`），
    #      既不是合法形态，也让连字符的含义（分隔符）失去唯一性。
    if not normalized or not all(
        (char.isascii() and char.isalnum()) or char == "-" for char in normalized
    ):
        raise ValueError(f"非法的编号前缀：{prefix!r}")
    if not any(char.isalnum() for char in normalized):
        raise ValueError(f"编号前缀至少要含一个字母或数字：{prefix!r}")

    tail = suffix if suffix is not None else random_code_suffix()
    if not tail or any(char not in CODE_ALPHABET for char in tail):
        raise ValueError(f"非法的编号后缀：{suffix!r}")

    return f"{normalized}-{moment:%Y%m}-{tail}"


__all__ = [
    "CODE_ALPHABET",
    "DEFAULT_SUFFIX_LENGTH",
    "PROJECT_CODE_PREFIX",
    "generate_display_code",
    "random_code_suffix",
]
