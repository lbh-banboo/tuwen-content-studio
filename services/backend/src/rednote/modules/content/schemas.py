"""WP26 的结构化输出契约与**反恒真式**校验（纯函数，无 IO、无 session）。

**本模块是 R1 / R4 两条高风险的唯一落点**（说明文档 §1.6）。两者都属于
"看起来有防线"的形态，本仓已在 WP19（ISS-75）与 WP23（`allowed_tools`）
各踩过一次，因此这里的写法**刻意不是**最自然的那种。

---

## 一、R1：为什么校验**不能**写成 `payload is not None`

最自然的写法是：

    if payload is None:  reject()
    return payload          # ← 恒真地放行 {}、{"x":1}、任意 JSON

它**永远为真**（只要模型返回了任何东西），于是"结构化输出已验证"
这句话是假的：下游拿到的"正文"可能是空 dict，而系统报成功，
错误要等到用户看见一篇空白内容才暴露 —— 那时已经付过钱。

**本模块的四类必拒输入**（说明文档 §9 候选第 21 条）：

| 类别 | 例 | 本模块的判定 |
|---|---|---|
| **空 dict** | `{}` | `REASON_OUTPUT_FIELD_MISSING`（第一个必需字段就不在） |
| **缺字段** | 蓝图少 `limitation` | `REASON_OUTPUT_FIELD_MISSING`（带 `field` 定位） |
| **类型错** | `tags` 是字符串而不是数组 | `REASON_OUTPUT_FIELD_TYPE` |
| **域外值** | `conclusion_strength = "VERY_STRONG"` | `REASON_OUTPUT_FIELD_VALUE` |

**只测"好输入能过"不构成判据** —— 那种测试与"没有校验器"在覆盖度上
没有区别（都只证明了恒真的那一半）。因此 `test_content_wp26.py` 的
校验器用例是**反例驱动**的：上表四类**各自**有断言失败的用例。

### 为什么**还要**拒绝多余字段

R1 的表述只要求"9 个必需字段各自存在且合法"，没说多余字段怎么办。
本模块选择**拒绝**（`REASON_OUTPUT_FIELD_VALUE`），理由是说明文档 §1.7
给出的那个后果：**"字段漂移无人发现"**。

模型多吐一个 `summary` 字段时，若静默接受，那么"提示词要求 9 个字段、
模型给了 10 个"这件事**没有任何地方会留下痕迹**；下一次提示词改成
11 个字段、模型给 9 个，也照样静默。漂移只有被拒绝时才可见。

**代价已登记**：严格拒绝会让"模型多写一个无害字段"变成生成失败。
这是刻意的取舍 —— 与本仓"默认必须是最小的那一档"（WP23 的
`PLATFORM_DEFAULT_TOOLS = ()`）同一取向。

---

## 二、R4：绑定必须**逐段**，且"绑了全部 claim"要**被拒**

`ParagraphBinding` 的值类型说明里已写了"整篇引用整份基线等于没有绑定"。
本模块把它落成**两条可判的规则**：

1. **段必须齐全**：`body` 的每一个非空段都必须有一条绑定 ——
   绑了 claim 的写 `claim_id`，没绑的写 `claim_id=None` + 理由。
   少一段即拒绝。缺了这条，模型可以只给前两段绑定，
   而"后面几段话依据什么"这个问题**复现不出来**（那些段落根本没有记录）。
2. **不许绑满**：若**去重后的绑定 claim 数 == 基线全部 claim 数**，
   拒绝（`REASON_BINDING_COVERS_ALL_CLAIMS`）。

第 2 条是本包最容易被质疑的一条，因此把它的边界写清楚：

- 它在**基线只有 1 条 claim** 时不生效（1 个 claim 的内容绑 1 条是
  唯一合理的结果，此时 `len(bound) == total == 1` 会被误判）。
  处置：**`total_claim_count > 1` 时才启用**。
  这是"规则本身也要有适用条件"的实例 —— 一条没有适用条件的规则，
  在边界上必然误判，而误判会让它被整体关掉（比没有更糟）。
- 它不能抓"绑了 10 条 claim 里的 9 条、但每条都是错的那种"——
  那需要真实模型与评测集，登记为 `BLOCKED_ON_EXTERNAL`（AQ-01）。
- **它抓的是"绑定集合与正文无关"这个结构特征**：整篇引用的特征是
  "每篇的绑定集合都相同且等于全集"，而这个特征**与正文写了什么无关**。

---

## 三、为什么是纯函数

无 `session`、无 `dispatch_call`、无 `datetime.now()`。理由：这些校验是
**判据的落点**，而判据必须能在**不连数据库**的情况下跑（§6 的
"反例驱动"用例全部是纯函数用例）—— 一条需要真库才能跑的校验判据，
在 CI 上就是一条会被 skip 掉的判据。
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Final

from rednote.modules.content.types import (
    BLUEPRINT_FIELDS,
    CONCLUSION_STRENGTHS,
    COPY_FIELDS,
    MAX_BINDING_REASON_CHARS,
    MAX_BLUEPRINT_FIELD_CHARS,
    MAX_BODY_CHARS,
    MAX_HIGHLIGHT_CHARS,
    MAX_KEYWORD_CHARS,
    MAX_PARAGRAPH_CHARS,
    MAX_PARAGRAPHS,
    MAX_RISK_NOTE_CHARS,
    MAX_TAGS,
    MAX_TAG_CHARS,
    MAX_TITLE_CANDIDATES,
    MAX_TITLE_CHARS,
    MIN_TITLE_CANDIDATES,
    REASON_BINDING_COVERS_ALL_CLAIMS,
    REASON_BINDING_MISSING_REASON,
    REASON_BINDING_UNKNOWN_CLAIM,
    REASON_OUTPUT_FIELD_MISSING,
    REASON_OUTPUT_FIELD_TYPE,
    REASON_OUTPUT_FIELD_VALUE,
    REASON_OUTPUT_NOT_OBJECT,
    REASON_TOO_FEW_TITLE_CANDIDATES,
    REASON_TOO_MANY_TITLE_CANDIDATES,
    TITLE_SCORE_DIMENSION_SET,
    BlueprintDraft,
    GeneratedCopy,
    ParagraphBinding,
    TitleCandidateDraft,
)

#: 9 个蓝图字段里唯一的受控枚举（其余 8 个是自由文本）。
_STRENGTH_FIELD: Final[str] = "conclusion_strength"

#: 蓝图 9 字段的合法键集合（拒绝多余字段用）。
_BLUEPRINT_KEY_SET: Final[frozenset[str]] = frozenset(BLUEPRINT_FIELDS)

#: 基线里只有这么几条 claim 时，"绑满"规则**不启用**（见模块 docstring 二）。
_ALL_CLAIMS_RULE_MIN_TOTAL: Final[int] = 2


class SchemaViolation(ValueError):
    """一次结构化输出校验失败。

    三个字段都是**机器可判**的（`field` 定位、`reason` 分类），
    与 `planning.limits.QuantityOutOfRange` 同一手法：
    `message` 是给人读的，`reason` 是给测试与前端用的。

    **继承 `ValueError` 而不是自定义基类**：调用方
    （`gateway_bridge` → `service`）在一条 `except ValueError` 里
    把"模型输出不合法"与"提示词版本没配"分开处理，
    而后者也是 `ValueError`。两者的区分靠 `reason`，不靠类型 ——
    本仓的 `QuantityOutOfRange` 也是同一选择。
    """

    def __init__(self, *, field: str, reason: str, message: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(message)


# ---------------------------------------------------------------------------
# 基础断言（三条各自对应一类必拒输入）
# ---------------------------------------------------------------------------


def _require_object(payload: Any, *, what: str) -> dict[str, Any]:
    """第一类：**空 dict / 非对象**都从这里拒。

    注意：`[]` 与 `"..."` 都**不是** dict，会被这一条拒掉；
    而 `{}` 是 dict，会走到下面的"缺字段"分支 —— 两者报的码不同
    （`OUTPUT_NOT_OBJECT` vs `OUTPUT_FIELD_MISSING`），
    因为"模型给了一个数组"与"模型给了一个空对象"是两种不同的故障。
    """
    if not isinstance(payload, dict):
        raise SchemaViolation(
            field="$",
            reason=REASON_OUTPUT_NOT_OBJECT,
            message=(
                f"{what}必须是一个 JSON 对象，实际是 {type(payload).__name__}。"
                "（这一条拒掉的是『模型返回了数组/字符串/标量』这类形状错误）"
            ),
        )
    return payload


def _require_text(
    payload: dict[str, Any],
    name: str,
    *,
    limit: int,
    what: str,
) -> str:
    """第二 / 三 / 四类：**缺字段 / 类型错 / 域外值**逐字段判。

    `strip()` 之后判空：`"   "` 与 `""` 同样是"没写"。
    这一点是**刻意的**且与"拒绝多余字段"同一取向 ——
    一个全是空格的 `limitation` 在数据库里"存在"，
    但它承载不了"不许把空白补成使用经历"这条约束（说明文档 §1.6 R10）。
    """
    if name not in payload:
        raise SchemaViolation(
            field=name,
            reason=REASON_OUTPUT_FIELD_MISSING,
            message=f"{what}缺少必需字段 {name!r}。",
        )
    value = payload[name]
    if not isinstance(value, str):
        raise SchemaViolation(
            field=name,
            reason=REASON_OUTPUT_FIELD_TYPE,
            message=(
                f"{what}的字段 {name!r} 必须是字符串，"
                f"实际是 {type(value).__name__}。"
            ),
        )
    if not value.strip():
        raise SchemaViolation(
            field=name,
            reason=REASON_OUTPUT_FIELD_VALUE,
            message=f"{what}的字段 {name!r} 不得为空或纯空白。",
        )
    if len(value) > limit:
        raise SchemaViolation(
            field=name,
            reason=REASON_OUTPUT_FIELD_VALUE,
            message=(
                f"{what}的字段 {name!r} 超长：{len(value)} > {limit} 字符。"
                "（上界存在的理由：该值会进不可变的内容版本行，写进去删不掉）"
            ),
        )
    return value


def _reject_extra_keys(
    payload: dict[str, Any],
    allowed: frozenset[str],
    *,
    what: str,
) -> None:
    """拒绝多余字段（模块 docstring 一末节）。

    **报 `OUTPUT_FIELD_VALUE` 而不是新造一个码**：它对调用方而言
    与"域外值"是同一件事（"你给了一个我不认识的东西"），
    而多造一个码会让 `REASON_*` 的分支数与实际处理路径不成比例。
    """
    extra = sorted(set(payload) - allowed)
    if extra:
        raise SchemaViolation(
            field=extra[0],
            reason=REASON_OUTPUT_FIELD_VALUE,
            message=(
                f"{what}出现了未定义的字段：{extra}。"
                f"合法字段是 {sorted(allowed)}。"
                "（拒绝而不是忽略：静默忽略会让『提示词要求 9 个字段、"
                "模型给了 10 个』这件事没有任何地方留下痕迹）"
            ),
        )


def _as_number(value: Any) -> float:
    """评分数值的收窄。`bool` 被**显式排除** —— 它是 `int` 的子类。"""
    if isinstance(value, bool):
        raise TypeError("布尔值不能当评分")
    if isinstance(value, (int, float)):
        return float(value)
    raise TypeError(f"{type(value).__name__} 不是数值")


# ---------------------------------------------------------------------------
# 蓝图（TASK_CONTENT_STRUCTURE 的产出）
# ---------------------------------------------------------------------------


def validate_blueprint(payload: Any) -> BlueprintDraft:
    """校验 9 字段蓝图。**9 个字段逐个走**（不循环取默认值）。

    逐字段写出来（而不是 `for name in BLUEPRINT_FIELDS: ...`）是**故意的**：
    循环写法在"字段清单与 dataclass 字段漂移"时不会报错 ——
    循环读完 9 个名字、构造时少一个字段，`TypeError` 会说"缺参数"，
    而不是"校验器漏查了 X"。显式写出来则**类构造本身就是对账**：
    少一个字段立刻在导入期就炸。
    `BLUEPRINT_FIELDS` 仍然保留，供 `models.py` 列名与测试对账。
    """
    obj = _require_object(payload, what="叙事蓝图")
    _reject_extra_keys(obj, _BLUEPRINT_KEY_SET, what="叙事蓝图")

    strength = _require_text(
        obj,
        _STRENGTH_FIELD,
        limit=32,
        what="叙事蓝图",
    )
    if strength not in CONCLUSION_STRENGTHS:
        raise SchemaViolation(
            field=_STRENGTH_FIELD,
            reason=REASON_OUTPUT_FIELD_VALUE,
            message=(
                f"叙事蓝图的 {_STRENGTH_FIELD} 取值非法：{strength!r}。"
                f"合法取值 {sorted(CONCLUSION_STRENGTHS)}。"
                "（这一维必须是受控枚举：它决定这句话需要多强的证据）"
            ),
        )

    def _text(name: str) -> str:
        return _require_text(
            obj, name, limit=MAX_BLUEPRINT_FIELD_CHARS, what="叙事蓝图"
        )

    return BlueprintDraft(
        opening_function=_text("opening_function"),
        context=_text("context"),
        decision_process=_text("decision_process"),
        usage_scene=_text("usage_scene"),
        evidence_beats=_text("evidence_beats"),
        limitation=_text("limitation"),
        conclusion_strength=strength,
        interaction_question=_text("interaction_question"),
        paragraph_plan=_text("paragraph_plan"),
    )


# ---------------------------------------------------------------------------
# 标题候选
# ---------------------------------------------------------------------------


_PARAGRAPH_SPLIT: Final[re.Pattern[str]] = re.compile(r"\n\s*\n")


def split_paragraphs(body: str) -> tuple[str, ...]:
    """把正文切成段（**只按空行分段**）。

    **为什么分段规则必须唯一**：段级绑定（R4）的段号是
    `paragraph_index`，而"一段"的定义若在
    "校验侧"与"展示侧"各写一遍，两边会切出不同的段数 ——
    表现是"绑定数对不上正文段数"，而那是**看起来像模型输出有问题**
    的一种程序缺陷（与本仓 ISS-137 的"同一事实的两种写法"同族）。

    规则：按**空行**切（`\\n\\s*\\n`），去掉每段首尾空白，丢弃纯空白的段。
    小红书正文就是"段落 + 空行"，没有嵌套结构。

    ⚠️ **段内软换行不切段**。本函数早先写成 `body.splitlines()`，
    那会把段内软换行也切成两段，而它自己的 docstring 就写着
    "只按空行分段" ⇒ **文档与实现自相矛盾**。实测（2026-10-08，
    8 个场景对比本仓三处切段实现）该写法在 4 个场景上与
    `knowledge.chunking._split_paragraphs`（`\\n\\s*\\n`）和
    `media.schemas.paragraph_refs_for_body`（`\\n\\s*\\n`）切法不同：

    | 正文 | 本函数（旧） | 另两处 |
    |---|---|---|
    | `A\\nB`（段内软换行） | 2 段 | 1 段 |
    | `A1\\nA2\\n\\nB` | 3 段 | 2 段 |
    | `A\\n  B`（缩进） | 2 段 | 1 段 |
    | `A\\U0001F600\\nB\\U0001F602` | 2 段 | 1 段 |

    后两行是**小红书正文常态**（emoji + 段内换行）。后果不是"切得不一样"
    而是**段号坐标系不同**：`content` 侧按 N 段判"绑定覆盖全部段落"，
    `media` 侧按 M 段（M < N）生成图，`paragraph_ref` 与段落绑定根本对不上。

    ⚠️ **本仓三处切段（`content` / `knowledge` / `media`）必须同口径**，
    但**模块平级不可互import**（见 `media/repository.py` 的同纪律说明），
    所以三处各有一份实现、无法收敛成一处。**一致性由判据钉住**
    （`tools/quality/check_paragraph_split_parity.py`），
    口径的**权威定义与委托关系登记在 OP-197**，改任一处必须同步另两处。
    """
    return tuple(
        chunk.strip() for chunk in _PARAGRAPH_SPLIT.split(body) if chunk.strip()
    )


def validate_title_candidates(
    candidates: Any,
    *,
    what: str = "标题候选",
) -> tuple[TitleCandidateDraft, ...]:
    """校验标题候选列表：数量在 `[MIN, MAX]` 内、6 维评分**齐全且不多不少**。

    6 维"齐全"是本包对 R1 在候选层的落点：只判 `scores` 是个非空 dict
    会恒真地放行 `{"a": 1}`（维度名拼错、少 5 维都不知道）。

    **"不多不少"而不是"至少包含"**：多一维意味着模型在与提示词
    不同的量纲上打分（说明文档附录 A4 明确"量纲未定"），
    静默接受会让 6 维与第 7 维混在一起入库，而"哪一维是权威的"无从判断。
    """
    if not isinstance(candidates, list):
        raise SchemaViolation(
            field="titleCandidates",
            reason=REASON_OUTPUT_FIELD_TYPE,
            message=f"{what}必须是数组，实际是 {type(candidates).__name__}。",
        )
    if len(candidates) < MIN_TITLE_CANDIDATES:
        raise SchemaViolation(
            field="titleCandidates",
            reason=REASON_TOO_FEW_TITLE_CANDIDATES,
            message=f"{what}至少要有 {MIN_TITLE_CANDIDATES} 个，实际 {len(candidates)} 个。",
        )
    if len(candidates) > MAX_TITLE_CANDIDATES:
        raise SchemaViolation(
            field="titleCandidates",
            reason=REASON_TOO_MANY_TITLE_CANDIDATES,
            message=(
                f"{what}最多 {MAX_TITLE_CANDIDATES} 个，实际 {len(candidates)} 个。"
                "（上界存在的理由：6 维评分表超过 12 行没有人会看完）"
            ),
        )

    drafts: list[TitleCandidateDraft] = []
    for index, item in enumerate(candidates):
        label = f"{what}第 {index} 个"
        obj = _require_object(item, what=label)
        title = _require_text(obj, "title", limit=MAX_TITLE_CHARS, what=label)

        if "scores" not in obj:
            raise SchemaViolation(
                field="scores",
                reason=REASON_OUTPUT_FIELD_MISSING,
                message=f"{label}缺少 6 维评分（scores）。",
            )
        raw_scores = obj["scores"]
        if not isinstance(raw_scores, dict):
            raise SchemaViolation(
                field="scores",
                reason=REASON_OUTPUT_FIELD_TYPE,
                message=(
                    f"{label}的 scores 必须是对象，"
                    f"实际是 {type(raw_scores).__name__}。"
                ),
            )
        missing = sorted(TITLE_SCORE_DIMENSION_SET - set(raw_scores))
        if missing:
            raise SchemaViolation(
                field="scores",
                reason=REASON_OUTPUT_FIELD_MISSING,
                message=f"{label}的 6 维评分缺少维度：{missing}。",
            )
        unknown = sorted(set(raw_scores) - TITLE_SCORE_DIMENSION_SET)
        if unknown:
            raise SchemaViolation(
                field="scores",
                reason=REASON_OUTPUT_FIELD_VALUE,
                message=(
                    f"{label}的评分出现未知维度：{unknown}。"
                    f"合法的 6 维是 {sorted(TITLE_SCORE_DIMENSION_SET)}。"
                    "（量纲尚未产品拍板，因此不接受第 7 维）"
                ),
            )
        scores: dict[str, float] = {}
        for name in sorted(TITLE_SCORE_DIMENSION_SET):
            try:
                scores[name] = _as_number(raw_scores[name])
            except TypeError as exc:
                raise SchemaViolation(
                    field=f"scores.{name}",
                    reason=REASON_OUTPUT_FIELD_TYPE,
                    message=f"{label}的评分维度 {name!r} 不是数值：{exc}。",
                ) from exc

        drafts.append(
            TitleCandidateDraft(
                title=title,
                scores=scores,
                keywords=_read_keywords(obj, what=label),
                highlight=_read_optional_text(
                    obj, "highlight", limit=MAX_HIGHLIGHT_CHARS, what=label
                ),
                risk_note=_read_optional_text(
                    obj, "riskNote", limit=MAX_RISK_NOTE_CHARS, what=label
                ),
            )
        )
    return tuple(drafts)


def _read_keywords(obj: dict[str, Any], *, what: str) -> tuple[str, ...]:
    """读 `keywords`（可不给）。给了就必须是**字符串数组**且元素非空。"""
    if "keywords" not in obj:
        return ()
    raw = obj["keywords"]
    if not isinstance(raw, list):
        raise SchemaViolation(
            field="keywords",
            reason=REASON_OUTPUT_FIELD_TYPE,
            message=f"{what}的 keywords 必须是数组，实际是 {type(raw).__name__}。",
        )
    out: list[str] = []
    for position, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise SchemaViolation(
                field=f"keywords[{position}]",
                reason=REASON_OUTPUT_FIELD_VALUE,
                message=f"{what}的关键词必须是非空字符串，第 {position} 个不合法。",
            )
        if len(item) > MAX_KEYWORD_CHARS:
            raise SchemaViolation(
                field=f"keywords[{position}]",
                reason=REASON_OUTPUT_FIELD_VALUE,
                message=(
                    f"{what}的关键词超长：{len(item)} > {MAX_KEYWORD_CHARS}。"
                ),
            )
        out.append(item)
    return tuple(out)


def _read_optional_text(
    obj: dict[str, Any], name: str, *, limit: int, what: str
) -> str:
    """读一个**可不给**的自由文本字段。给了就必须是非空字符串且在界内。"""
    if name not in obj or obj[name] is None:
        return ""
    value = obj[name]
    if not isinstance(value, str):
        raise SchemaViolation(
            field=name,
            reason=REASON_OUTPUT_FIELD_TYPE,
            message=f"{what}的 {name!r} 必须是字符串，实际是 {type(value).__name__}。",
        )
    if len(value) > limit:
        raise SchemaViolation(
            field=name,
            reason=REASON_OUTPUT_FIELD_VALUE,
            message=f"{what}的 {name!r} 超长：{len(value)} > {limit}。",
        )
    return value.strip()


# ---------------------------------------------------------------------------
# 证据绑定（R4 的落点）
# ---------------------------------------------------------------------------


def validate_bindings(
    raw: Any,
    *,
    known_claim_ids: frozenset[uuid.UUID],
    total_claim_count: int,
    paragraph_count: int,
) -> tuple[ParagraphBinding, ...]:
    """校验段落级证据绑定（R4）。

    ## 三条规则，各自对应一种"看起来有防线"

    | 规则 | 不做的后果 | 报的码 |
    |---|---|---|
    | 段号**齐全且不重**（`0..paragraph_count-1` 各一次） | 模型只绑前两段 ⇒ 后面几段"依据什么"无处可查 | `OUTPUT_FIELD_VALUE` |
    | `claim_id is None` 必须带理由 | "没绑定"变成可随手用、事后查不出的后门 | `BINDING_MISSING_REASON` |
    | `claim_id` 必须**在基线里** | 绑一个不存在的 claim ⇒ 绑定"看起来有"但指向空气 | `BINDING_UNKNOWN_CLAIM` |
    | 去重后绑定的 claim 数 **≠ 基线全部 claim 数** | 整篇引用整份基线 = 没有绑定 | `BINDING_COVERS_ALL_CLAIMS` |

    ## 四个入参为什么都是必需的

    - `known_claim_ids` —— **必须是基线里真实存在的 claim**。
      没有它，"绑了 claim"只意味着"有个 UUID"，而那个 UUID 可以指向
      任何东西（包括别的项目的 claim）。这条同时是**跨项目越界**的拦截点。
    - `total_claim_count` —— "绑满"规则的分母，来自**基线**而不是
      来自绑定列表本身（用绑定列表的分母会让规则恒真，
      那正是 ISS-44/64 反复出现的"同源自证"）。
    - `paragraph_count` —— 来自**正文**（`split_paragraphs`），
      不是来自绑定列表。同上：用绑定列表的长度会让"段齐全"恒真。

    **这三个外部事实与绑定列表是四个独立的来源** —— 这正是
    说明文档 §1.6 R4 要的"两种独立事实相遇"，而不是"同一事实的两种写法"。
    """
    if not isinstance(raw, list):
        raise SchemaViolation(
            field="bindings",
            reason=REASON_OUTPUT_FIELD_TYPE,
            message=f"证据绑定必须是数组，实际是 {type(raw).__name__}。",
        )

    parsed: list[ParagraphBinding] = []
    seen: set[int] = set()
    for position, item in enumerate(raw):
        label = f"第 {position} 条绑定"
        obj = _require_object(item, what=label)

        index = obj.get("paragraphIndex")
        if not isinstance(index, int) or isinstance(index, bool):
            raise SchemaViolation(
                field="paragraphIndex",
                reason=REASON_OUTPUT_FIELD_TYPE,
                message=f"{label}的 paragraphIndex 必须是非负整数，实际是 {index!r}。",
            )
        if index < 0 or index >= paragraph_count:
            raise SchemaViolation(
                field="paragraphIndex",
                reason=REASON_OUTPUT_FIELD_VALUE,
                message=(
                    f"{label}的段落号 {index} 越界：正文只有 {paragraph_count} 段"
                    f"（合法区间 0..{paragraph_count - 1}）。"
                ),
            )
        if index in seen:
            raise SchemaViolation(
                field="paragraphIndex",
                reason=REASON_OUTPUT_FIELD_VALUE,
                message=(
                    f"{label}的段落号 {index} 重复。一段只能有一条绑定 —— "
                    "重复会让'这一段绑了哪条证据'有两个答案。"
                ),
            )
        seen.add(index)

        raw_claim = obj.get("claimId")
        claim_id: uuid.UUID | None
        if raw_claim is None:
            claim_id = None
        else:
            try:
                claim_id = uuid.UUID(str(raw_claim))
            except (ValueError, AttributeError, TypeError) as exc:
                raise SchemaViolation(
                    field="claimId",
                    reason=REASON_OUTPUT_FIELD_TYPE,
                    message=f"{label}的 claimId 不是合法 UUID：{raw_claim!r}。",
                ) from exc

        reason_text = obj.get("reason")
        if reason_text is not None and not isinstance(reason_text, str):
            raise SchemaViolation(
                field="reason",
                reason=REASON_OUTPUT_FIELD_TYPE,
                message=f"{label}的 reason 必须是字符串。",
            )
        reason_text = (reason_text or "").strip()

        if claim_id is None:
            if not reason_text:
                raise SchemaViolation(
                    field="reason",
                    reason=REASON_BINDING_MISSING_REASON,
                    message=(
                        f"{label}没有绑定 claim，也没有给出理由。"
                        "（允许不绑，但必须显式说明为什么这一段没有事实依据 —— "
                        "否则'没绑定'就是一个可以随手用、事后查不出的后门）"
                    ),
                )
        elif claim_id not in known_claim_ids:
            raise SchemaViolation(
                field="claimId",
                reason=REASON_BINDING_UNKNOWN_CLAIM,
                message=(
                    f"{label}绑定的 claim {claim_id} 不在本次基线里。"
                    "（绑定指向不存在的 claim 会让'有绑定'看似成立而实际指向空气；"
                    "这也是跨项目越界的拦截点）"
                ),
            )
        if len(reason_text) > MAX_BINDING_REASON_CHARS:
            raise SchemaViolation(
                field="reason",
                reason=REASON_OUTPUT_FIELD_VALUE,
                message=(
                    f"{label}的理由超长：{len(reason_text)} > "
                    f"{MAX_BINDING_REASON_CHARS}。"
                ),
            )

        text = obj.get("text", "")
        if not isinstance(text, str):
            raise SchemaViolation(
                field="text",
                reason=REASON_OUTPUT_FIELD_TYPE,
                message=f"{label}的 text 必须是字符串。",
            )

        parsed.append(
            ParagraphBinding(
                paragraph_index=index,
                claim_id=claim_id,
                reason=reason_text or None,
                text=text,
            )
        )

    # --- 规则一：段号齐全（0..paragraph_count-1 各一次） ---------------------
    expected = set(range(paragraph_count))
    if seen != expected:
        missing = sorted(expected - seen)
        raise SchemaViolation(
            field="paragraphIndex",
            reason=REASON_OUTPUT_FIELD_VALUE,
            message=(
                f"证据绑定没有覆盖全部段落：缺 {missing}。"
                f"正文共 {paragraph_count} 段，必须有 {paragraph_count} 条绑定"
                "（无事实可绑的段落要写 claimId=null + 理由）。"
                "（缺了这条，模型可以只绑前两段，而后面几段'依据什么'无处可查）"
            ),
        )

    # --- 规则二：不许绑满整份基线 ------------------------------------------
    # `total_claim_count > 1` 才启用：基线只有 1 条 claim 时，
    # "绑 1 条 == 全部"是唯一合理结果，规则在此误判（见模块 docstring 二）。
    if total_claim_count >= _ALL_CLAIMS_RULE_MIN_TOTAL:
        bound = {b.claim_id for b in parsed if b.claim_id is not None}
        if len(bound) >= total_claim_count:
            raise SchemaViolation(
                field="bindings",
                reason=REASON_BINDING_COVERS_ALL_CLAIMS,
                message=(
                    f"绑定覆盖了基线的全部 {total_claim_count} 条 claim。"
                    "这不构成绑定 —— 每篇内容都绑全部 claim，"
                    "等于'这句话依据哪条证据'仍然答不出来（AQ-01 的盲审只能靠印象）。"
                    "绑定必须**只含这一段真正用到的事实**。"
                ),
            )

    return tuple(sorted(parsed, key=lambda item: item.paragraph_index))


# ---------------------------------------------------------------------------
# 文案三件（TASK_CONTENT_COPY 的产出）
# ---------------------------------------------------------------------------


def validate_copy(
    payload: Any,
    *,
    known_claim_ids: frozenset[uuid.UUID],
    total_claim_count: int,
) -> GeneratedCopy:
    """校验文案产出：标题 + 正文 + 标签（+ 绑定 + 标题候选）。

    **`bindings` 与 `titleCandidates` 也在这一个入口里判完**
    （而不是留给 `service` 各调一次）：它们是**同一次调用的输出**。
    分两处判会让"标题合法但绑定非法"的中间态漏出去 ——
    能不能落库取决于调用方记不记得调第二个校验器，
    而"忘了调"的表现是**数据入库且不报错**。

    ## `paragraph_count` 为什么由本函数自己算

    它来自 `body`（`split_paragraphs`），而 `body` 就在同一个 payload 里。
    从**外层另传一个** `paragraph_count` 会造出第二种可能：
    传进来的值与正文实际段数不一致 —— 那时"段齐全"规则判的是
    一个与正文无关的数，而它照样会通过或失败，**看不出依据错了**。

    其余两个入参由 `service` 用**基线**算好传进来（见 `validate_bindings`）：
    基线不在本 payload 里，只能从外面给。
    """
    obj = _require_object(payload, what="文案产出")
    allowed = frozenset(COPY_FIELDS) | {"bindings", "titleCandidates"}
    _reject_extra_keys(obj, allowed, what="文案产出")

    title = _require_text(obj, "title", limit=MAX_TITLE_CHARS, what="文案产出")
    body = _require_text(obj, "body", limit=MAX_BODY_CHARS, what="文案产出")
    tags = _read_tags(obj)

    paragraphs = split_paragraphs(body)
    if not paragraphs:
        raise SchemaViolation(
            field="body",
            reason=REASON_OUTPUT_FIELD_VALUE,
            message=(
                "正文切不出任何段落。"
                "（正文是纯文本 + 换行；切不出段落说明它只有空白字符 —— "
                "而空白字符在 `_require_text` 里已经该被拒，"
                "走到这里说明切段规则与判空规则不一致，属于程序缺陷）"
            ),
        )
    if len(paragraphs) > MAX_PARAGRAPHS:
        raise SchemaViolation(
            field="body",
            reason=REASON_OUTPUT_FIELD_VALUE,
            message=(
                f"正文段落数超界：{len(paragraphs)} > {MAX_PARAGRAPHS}。"
                "（上界防的是『正文被写成一字一段』这种形态："
                "它会让段落级绑定的行数爆炸，且每段都绑不上具体事实）"
            ),
        )
    for position, text in enumerate(paragraphs):
        if len(text) > MAX_PARAGRAPH_CHARS:
            raise SchemaViolation(
                field=f"body[{position}]",
                reason=REASON_OUTPUT_FIELD_VALUE,
                message=(
                    f"第 {position} 段超长：{len(text)} > {MAX_PARAGRAPH_CHARS}。"
                ),
            )

    bindings = validate_bindings(
        obj.get("bindings"),
        known_claim_ids=known_claim_ids,
        total_claim_count=total_claim_count,
        paragraph_count=len(paragraphs),
    )
    candidates = validate_title_candidates(obj.get("titleCandidates"))

    return GeneratedCopy(
        title=title,
        body=body,
        tags=tags,
        bindings=bindings,
        title_candidates=candidates,
    )


def _read_tags(obj: dict[str, Any]) -> tuple[str, ...]:
    """读话题标签（`COPY_FIELDS` 的第三件）。

    **去重但不排序**：标签的**展示顺序**有意义（小红书里前置标签
    权重更高），排序会改掉模型给的顺序；而重复标签是小红的显式错误
    （同一话题出现两次），必须拒 —— 拒而不是去重，
    因为"模型重复给了"是提示词要改的信号，静默去重会让它永远不被发现。
    """
    if "tags" not in obj:
        raise SchemaViolation(
            field="tags",
            reason=REASON_OUTPUT_FIELD_MISSING,
            message="文案产出缺少必需字段 'tags'。",
        )
    raw = obj["tags"]
    if not isinstance(raw, list):
        raise SchemaViolation(
            field="tags",
            reason=REASON_OUTPUT_FIELD_TYPE,
            message=f"tags 必须是数组，实际是 {type(raw).__name__}。",
        )
    if len(raw) > MAX_TAGS:
        raise SchemaViolation(
            field="tags",
            reason=REASON_OUTPUT_FIELD_VALUE,
            message=f"标签最多 {MAX_TAGS} 个，实际 {len(raw)} 个。",
        )
    out: list[str] = []
    for position, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise SchemaViolation(
                field=f"tags[{position}]",
                reason=REASON_OUTPUT_FIELD_VALUE,
                message=f"第 {position} 个标签必须是非空字符串。",
            )
        tag = item.strip()
        if len(tag) > MAX_TAG_CHARS:
            raise SchemaViolation(
                field=f"tags[{position}]",
                reason=REASON_OUTPUT_FIELD_VALUE,
                message=f"第 {position} 个标签超长：{len(tag)} > {MAX_TAG_CHARS}。",
            )
        if tag in out:
            raise SchemaViolation(
                field=f"tags[{position}]",
                reason=REASON_OUTPUT_FIELD_VALUE,
                message=(
                    f"标签 {tag!r} 重复。"
                    "（不静默去重：模型重复给标签是提示词要改的信号，"
                    "去重会让这件事永远不被发现）"
                ),
            )
        out.append(tag)
    return tuple(out)


__all__ = [
    "SchemaViolation",
    "split_paragraphs",
    "validate_blueprint",
    "validate_bindings",
    "validate_copy",
    "validate_title_candidates",
]