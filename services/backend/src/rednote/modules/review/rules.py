"""WP30 规则注册表 —— **全包唯一的"有哪些规则"的答案**。

## 为什么是代码常量，不是一张 `review_rules` 配置表

| 配置表方案的问题 | 后果 |
|---|---|
| 规则逻辑要用表达式求值 | 要么 `eval`（代码执行面），要么自造一门小语言（词法/语法/求值三层，各自又需要判据）。本仓没有任何表达式求值先例 |
| "有哪些规则"变成不可静态回答的问题 | 本包最核心的防线 ——"每条规则都有阳性 + 阴性对照用例"—— **失去载体**：判据得先连库读表才知道该测什么，而库里的内容随环境变 |
| `enabled` 开关 | 没有任何已知消费场景要求"关掉某条硬规则"；一个从不被改的开关列就是 ISS-95 族（声称有防线） |
| 规则码进 CHECK | 按 ISS-191 它必须写成字面量化石 ⇒ **每加一条规则要一次迁移**，且化石与注册表的分叉**不会报错** |

`RULES` 是导入期可枚举的元组 ⇒ 判据可以机械对账：
"注册表无重复 code"、"每个 category 至少一条规则"、"每条 code ≤ 60 字节"
在**导入期**就 assert；"每条 evaluator 都有阳性 + 阴性用例"在测试里遍历
本元组逐条断言（手法照 `tools/quality/check_paragraph_split_parity.py` 的
G5 活性下界）。

要可配时，**唯一正确的落点**是 `runtime_settings` + `get_effective_limits`
（docstring 原文"全仓唯一的配置读取路径"，已有先例 `platform.mediaLimits`），
由主 agent 串行加 key —— 不是在本包再开一张表。

## `kind`（HARD / SOFT）与 `default_severity` 刻意分两维

`kind` 回答"**这个结论可复现吗**"：`HARD` = 同一输入必得同一结论
（纯函数 + 库事实）；`SOFT` = 结论依赖启发式（如禁用词表），改词表就会改结论。
`default_severity` 回答"**命中了有多严重**"。

合并两维的表现是"把软提示当硬门"或"把硬错降成提示"——
而 `static/quality-view.html:318` 原文正警告这一点：「软提示，不作为唯一质量标准」。
因此一条 `SOFT` 规则也可以 `BLOCKING`，一条 `HARD` 规则也可以只 `INFO`。

## `anchor_kind` 在本表里是**声明的粒度**，不是约束

落库的锚点以每条 finding 自己的 `anchor_kind` 为准（三张表的 CHECK 守的是
那一层）。本字段是规则目录端点 ⑥ 对前端的声明："这条规则命中时，你通常
能拿到段 / 图 / 整篇哪一级坐标"。个别规则有回落（如 binding 的
`paragraphIndex` 越界时回落整篇），回落规则写在各 evaluator 的 docstring 里。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from rednote.modules.review import evaluators
from rednote.modules.review.types import (
    MAX_RULE_CODE_CHARS,
    REVIEW_ANCHOR_KIND_MEDIA,
    REVIEW_ANCHOR_KIND_PARAGRAPH,
    REVIEW_ANCHOR_KIND_WHOLE,
    REVIEW_ANCHOR_KINDS,
    REVIEW_CATEGORIES,
    REVIEW_CATEGORY_DUPLICATE,
    REVIEW_CATEGORY_FACT,
    REVIEW_CATEGORY_MEDIA_CONSISTENCY,
    REVIEW_CATEGORY_OTHER,
    REVIEW_CATEGORY_SCENE,
    REVIEW_CATEGORY_STRUCTURE,
    REVIEW_CATEGORY_TECH_FORMAT,
    REVIEW_CATEGORY_TONE,
    REVIEW_SEVERITIES,
    REVIEW_SEVERITY_BLOCKING,
    REVIEW_SEVERITY_INFO,
    REVIEW_SEVERITY_WARNING,
    RULE_KIND_HARD,
    RULE_KIND_SOFT,
    RULE_KINDS,
    CheckSubject,
    RuleOutcome,
)


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """一条规则的**身份与策略**（逻辑在 `evaluators.py`）。

    `implemented` 不是一个开关，而是一条**如实声明**：三条恒 SKIPPED 的规则
    （`TONE` / `DUPLICATE` / `SCENE`）在当前 schema + MVP 边界下判不了，
    规则目录端点要能把这件事说出来，否则前端只能从"一直没有命中"猜。

    **它不参与执行路径**：三条规则同样被遍历、同样返回 `SKIPPED` + 受控
    原因码、同样在库里留一行。若用它去跳过执行，那一类就会一行都没有 ——
    而"缺席"被渲染成"通过"的概率远高于被渲染成"未判"。
    """

    code: str
    category: str
    kind: str
    #: 命中时写进 `rule_results.severity`。恒 SKIPPED 的规则为 `None`
    #: （它永远不会 FAILED，给一个严重度是凭空声称）。
    default_severity: str | None
    anchor_kind: str
    evaluator: Callable[[CheckSubject], RuleOutcome]
    #: 本轮是否真的会给出 PASSED/FAILED 结论（False = 恒 SKIPPED）。
    implemented: bool
    #: 一句人读的说明，直接出现在规则目录端点的响应里。
    summary: str


RULES: Final[tuple[RuleSpec, ...]] = (
    # --- FACT --------------------------------------------------------------
    RuleSpec(
        code="FACT_BINDING_CLAIM_OUT_OF_SCOPE",
        category=REVIEW_CATEGORY_FACT,
        kind=RULE_KIND_HARD,
        default_severity=REVIEW_SEVERITY_BLOCKING,
        anchor_kind=REVIEW_ANCHOR_KIND_PARAGRAPH,
        evaluator=evaluators.fact_binding_claim_out_of_scope,
        implemented=True,
        summary="证据绑定引用的事实已不在当前生效基线的 claim 范围内。",
    ),
    RuleSpec(
        code="FACT_UNBOUND_INCLUDED_CLAIM",
        category=REVIEW_CATEGORY_FACT,
        kind=RULE_KIND_HARD,
        default_severity=REVIEW_SEVERITY_WARNING,
        anchor_kind=REVIEW_ANCHOR_KIND_WHOLE,
        evaluator=evaluators.fact_unbound_included_claim,
        implemented=True,
        summary="当前生效基线里有事实一条绑定都没引用（覆盖度提示）。",
    ),
    # --- STRUCTURE ---------------------------------------------------------
    RuleSpec(
        code="STRUCTURE_PARAGRAPH_COUNT_BELOW_IMAGE_PLAN",
        category=REVIEW_CATEGORY_STRUCTURE,
        kind=RULE_KIND_HARD,
        default_severity=REVIEW_SEVERITY_WARNING,
        anchor_kind=REVIEW_ANCHOR_KIND_WHOLE,
        evaluator=evaluators.structure_paragraph_count_below_image_plan,
        implemented=True,
        summary="正文段数少于图位数，多余的图会回落到最后一段。",
    ),
    RuleSpec(
        code="STRUCTURE_VERSION_WITHOUT_BLUEPRINT",
        category=REVIEW_CATEGORY_STRUCTURE,
        kind=RULE_KIND_HARD,
        default_severity=REVIEW_SEVERITY_INFO,
        anchor_kind=REVIEW_ANCHOR_KIND_WHOLE,
        evaluator=evaluators.structure_version_without_blueprint,
        implemented=True,
        summary="这一版没有关联叙事蓝图，结构计划无从溯源。",
    ),
    # --- MEDIA_CONSISTENCY -------------------------------------------------
    RuleSpec(
        code="MEDIA_PARAGRAPH_REF_STALE",
        category=REVIEW_CATEGORY_MEDIA_CONSISTENCY,
        kind=RULE_KIND_HARD,
        default_severity=REVIEW_SEVERITY_WARNING,
        anchor_kind=REVIEW_ANCHOR_KIND_MEDIA,
        evaluator=evaluators.media_paragraph_ref_stale,
        implemented=True,
        summary="图位的段落坐标已不对应当前正文的任何一段。",
    ),
    RuleSpec(
        code="MEDIA_ITEM_NOT_READY",
        category=REVIEW_CATEGORY_MEDIA_CONSISTENCY,
        kind=RULE_KIND_HARD,
        default_severity=REVIEW_SEVERITY_WARNING,
        anchor_kind=REVIEW_ANCHOR_KIND_MEDIA,
        evaluator=evaluators.media_item_not_ready,
        implemented=True,
        summary="图位还没出图（状态不是 READY），内容尚不可交付。",
    ),
    # --- TECH_FORMAT -------------------------------------------------------
    RuleSpec(
        code="TECH_RENDITION_OUT_OF_PLATFORM_LIMITS",
        category=REVIEW_CATEGORY_TECH_FORMAT,
        kind=RULE_KIND_HARD,
        default_severity=REVIEW_SEVERITY_WARNING,
        anchor_kind=REVIEW_ANCHOR_KIND_MEDIA,
        evaluator=evaluators.tech_rendition_out_of_platform_limits,
        implemented=True,
        summary="READY 产物的宽/高/字节数超出平台限制；限制未配置时不判。",
    ),
    # --- OTHER -------------------------------------------------------------
    RuleSpec(
        code="OTHER_BANNED_TERM_HIT",
        category=REVIEW_CATEGORY_OTHER,
        kind=RULE_KIND_SOFT,
        default_severity=REVIEW_SEVERITY_WARNING,
        anchor_kind=REVIEW_ANCHOR_KIND_PARAGRAPH,
        evaluator=evaluators.other_banned_term_hit,
        implemented=True,
        summary="正文命中禁用词（促销式号召 / 绝对化表述）。",
    ),
    # --- 三条恒 SKIPPED ----------------------------------------------------
    RuleSpec(
        code="TONE_MODEL_JUDGEMENT",
        category=REVIEW_CATEGORY_TONE,
        kind=RULE_KIND_SOFT,
        default_severity=None,
        anchor_kind=REVIEW_ANCHOR_KIND_WHOLE,
        evaluator=evaluators.tone_model_judgement,
        implemented=False,
        summary="语气判定需要模型，本包 MVP 纯本地判定 ⇒ 恒 SKIPPED。",
    ),
    RuleSpec(
        code="DUPLICATE_CROSS_ITEM_SIMILARITY",
        category=REVIEW_CATEGORY_DUPLICATE,
        kind=RULE_KIND_SOFT,
        default_severity=None,
        anchor_kind=REVIEW_ANCHOR_KIND_WHOLE,
        evaluator=evaluators.duplicate_cross_item_similarity,
        implemented=False,
        summary="跨篇相似度归 WP33（输入是一批而非一篇）⇒ 恒 SKIPPED。",
    ),
    RuleSpec(
        code="SCENE_LINKAGE_UNAVAILABLE",
        category=REVIEW_CATEGORY_SCENE,
        kind=RULE_KIND_HARD,
        default_severity=None,
        anchor_kind=REVIEW_ANCHOR_KIND_WHOLE,
        evaluator=evaluators.scene_linkage_unavailable,
        implemented=False,
        summary="schema 无内容→场景的机器可判关联列 ⇒ 恒 SKIPPED。",
    ),
)

# ---------------------------------------------------------------------------
# 导入期自检（**不是测试的替代，是测试的下界**）
# ---------------------------------------------------------------------------
# 为什么放在导入期：这四条若被违反，本包的**任何**行为都不再可解释
# （重复 code 会让 `(run_id, rule_code)` 唯一约束把第二条结论吞掉；
# 超长 code 会被 varchar(60) 截断或拒绝；少一个 category 会让那一类的
# 卡片缺席而被渲染成"通过"）。让 import 当场失败，比让一轮自检
# 写出半套结果要好。
assert len(RULES) == len({spec.code for spec in RULES}), (
    "RULES 里有重复的 rule_code —— `(run_id, rule_code)` 唯一约束会让"
    "第二条结论写不进去（静默少一条规则结论）。"
)
assert REVIEW_CATEGORIES == {spec.category for spec in RULES}, (
    "每个自检分类必须恰好有 ≥1 条规则：缺席的那一类在响应里会一行都没有，"
    "而前端把'没有行'渲染成'通过'的概率远高于渲染成'未判'。"
    f"缺：{sorted(REVIEW_CATEGORIES - {spec.category for spec in RULES})}；"
    f"多：{sorted({spec.category for spec in RULES} - REVIEW_CATEGORIES)}"
)
assert all(0 < len(spec.code) <= MAX_RULE_CODE_CHARS for spec in RULES), (
    f"rule_code 必须非空且 ≤ {MAX_RULE_CODE_CHARS} 字节（列宽）。"
)
assert all(
    spec.kind in RULE_KINDS
    and spec.anchor_kind in REVIEW_ANCHOR_KINDS
    and (spec.default_severity is None or spec.default_severity in REVIEW_SEVERITIES)
    for spec in RULES
), "RuleSpec 的 kind / anchor_kind / default_severity 必须落在取值域内。"
# 恒 SKIPPED 的规则不得声明严重度（它永远不会 FAILED，声明了就是凭空声称）；
# 已实现的规则必须声明严重度（否则命中时 `rule_results.severity` 没有来源）。
assert all(
    (spec.default_severity is None) == (not spec.implemented) for spec in RULES
), "`implemented=False` ⇔ `default_severity is None`（见 RuleSpec 的说明）。"


_BY_CODE: Final[dict[str, RuleSpec]] = {spec.code: spec for spec in RULES}


def by_code(code: str) -> RuleSpec | None:
    """按 code 取规则；未注册返回 `None`（服务层据此给 422，不落库）。

    `rule_code` **刻意不落数据库 CHECK**（见 `models.py` 的取舍），
    因此本函数 + 服务层的 membership 校验就是取值域的全部守卫。
    """
    return _BY_CODE.get(code)


def by_category(category: str) -> tuple[RuleSpec, ...]:
    """某一类下的全部规则（规则目录与响应聚合用）。"""
    return tuple(spec for spec in RULES if spec.category == category)


def all_codes() -> frozenset[str]:
    return frozenset(_BY_CODE)


__all__ = ["RULES", "RuleSpec", "all_codes", "by_category", "by_code"]
