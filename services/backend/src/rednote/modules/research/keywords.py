"""从 Brief 版本**纯函数派生**研究关键词与覆盖缺口（WP19 的核心）。

**零 I/O**。本模块不 import sqlalchemy、不碰 session、不读环境变量。
输入是两个 frozen dataclass 序列（`ClaimInput` / `FieldValueInput`），
输出是 `Keyword` / `Gap` / `Prompt`。因此它可以被**穷举测试** ——
而本包的验收（「复制、上传、解析、步骤2确认完整」）里
"复制"与"确认完整"两件事的正确性全部由本模块承担。

---

## 为什么派生而不落库（本模块存在的根本理由）

派生结果 = f(Brief 版本, 派生规则版本)。它**显然可重算**。
把它落库会引入**第二个来源**：

  · Brief 出了新版本 → 表里的词**没有任何机制**跟着更新，
    于是"系统推荐的是什么"与"表里现在是什么"分叉，且无人察觉；
  · 覆盖缺口更糟：运营把一条标成"已覆盖"之后 Brief 又变了，
    缺口状态就成了一个**死锁**（既不能信表、也不能重算）。

代价是每次请求算一遍。量级：一个 Brief 版本最多 500 条 claim
（WP18 的 `MAX_CLAIMS_PER_VERSION`），派生是 O(n) 的字符串拼接。
可以忽略。

## 缺口判据：**必须读 WP18 的常量**

`CATEGORIES_REQUIRING_EVIDENCE`（= `{SOURCE_FACT, EXTERNAL_EVIDENCE}`）
是 WP18 对"哪类结论必须有来源"的**唯一权威定义**。
本模块 `import` 它，**不抄第二份集合**。

抄第二份的具体后果：WP18 若新增一类需要证据的分类（例如 `DERIVED`），
它会同步更新 `CATEGORIES_REQUIRING_EVIDENCE`（因为那个常量是它的
`ClaimDraft` 构造器与数据库 CHECK 的共同来源），
而**抄来的那份不会动** → 新分类的缺口在本包里静默消失。

## 三类关键词的分工（可解释性 = R1 的应对）

| 种类 | 来自 | 例子 |
|---|---|---|
| `ENTITY` | Brief 的实体字段（产品名/品牌/型号） | `某品牌 某产品` |
| `FIELD` | 结构化字段的取值（人群、场景、痛点） | `租房小厨房` |
| `CLAIM` | 某条结论的短语（**必须是缺口相关的**） | `某产品 续航` |
| `PATTERN` | **平台检索惯例**（不是 Brief 里的东西） | `某产品 测评` |

`PATTERN` 的词里混入 Brief 里的主体词（"某产品 测评"），
但它**整类被显式标注**：运营看到 `sourceKind='PATTERN'` 就知道
"这个后缀是系统按平台惯例加的，不是我的 Brief 里说的"。
把它混进 `ENTITY` 会让可解释性变成一句空话。

## 缺口状态的判据（三条，顺序不可换）

`gaps[]` 里出现的每一条，状态由 `gap_state` 判定：

1. **要不要进 `gaps[]`**：`claim_needs_gap` —— **只判类别**
   （`category in CATEGORIES_REQUIRING_EVIDENCE`）。
   `STRATEGY` 是策略判断、`UNCONFIRMED` 本就承认自己没证据，
   都不需要外部资料来"补齐"，因此**不进 `gaps[]`**。
   **已标 `NOT_APPLICABLE` 的条目仍要进**（否则运营看不到自己
   标记过什么，也无法取消标记）—— 见 `claim_needs_gap` 的说明。
2. `resolution == NOT_APPLICABLE` → `GAP_NOT_APPLICABLE`（不阻断）。
3. `resolution == CONFIRMED` → `GAP_COVERED`；否则（`PENDING`）
   → `GAP_MISSING`（**唯一阻断态**）。

**第 2 条必须在第 3 条之前**：一条已标"不适用"的结论，
应该显示为 `NOT_APPLICABLE`（运营的明确判断优先于系统的推断），
而不是因为"已确认"被显示成 `COVERED` —— 那会让运营以为
"系统认为这条已经够了"，与他的判断相反。

---

## ⚠️ 为什么缺口判据不能用"有没有来源"（本包最实质的一处修正）

### 先说结论

**不能用 `brief_claims.evidence_segment_id IS NULL` 判缺口。** 原因是
WP18 在**数据库层**强制了这件事（`ck_brief_claims_factual_category_requires_evidence`）：

```sql
category NOT IN ('EXTERNAL_EVIDENCE', 'SOURCE_FACT')
  OR evidence_segment_id IS NOT NULL
  OR (evidence_url IS NOT NULL AND char_length(evidence_url) > 0)
```

也就是说：**凡是需要证据的类别（`SOURCE_FACT` / `EXTERNAL_EVIDENCE`），
库里那一行必然带来源。** 而 WP18 的 `decomposer` 还会把
"声明为事实但没来源"的结论**降级成 `UNCONFIRMED`**，从另一个方向
保证同一件事。

### 后果（如果不修正，本包会坏成什么样）

按"没有来源 → 缺口"实现的话：

    claim_needs_gap(c) == True   （类别要对）
    has_evidence(c)    == True   （数据库保证，恒真）
    → gap_state(c)     == COVERED（恒假：永远不会是 MISSING）

于是 `gaps[]` 里**全是 `COVERED`**，`summary.blockingCount` 恒为 0，
"步骤 2 确认完整"变成一个**永远已经完成**的按钮。
而它不会报任何错、不影响任何断言 —— 功能测试全绿，
只有真库上的运营会发现"缺口清单永远是空的"。

### 修正后的判据：缺口看 **`resolution_status`**

`resolution_status` 是**运营可改的那一列**（`PENDING` / `CONFIRMED` /
`NOT_APPLICABLE`，由 WP18 的 `PUT /projects/{id}/brief-draft` 的
`claims[].resolution` 写入）。用它判缺口有三个好处：

  · **它是唯一由人控制的状态** —— "缺口"本来就是一个待办事项，
    待办事项的完成状态必须由人标记；
  · **它使"步骤 2 确认完整"成为一个真实动作** —— 运营逐条确认后
    `blockingCount` 归零，步骤 2 完成；
  · **它不会被数据库的恒真条件吃掉** —— `PENDING` 是能做出来的。

### 那 `has_evidence` 还有什么用

它**仍然有用，但角色变了**：它不再决定"是不是缺口"，
而是解释"**为什么这条明明有出处，却还是缺口**"。

一定要说清楚这一点，因为运营的合理反应是：
"这条结论在 Brief 原文里有出处啊，为什么说它缺证据？"
答：**`brief_claims` 上的来源是"这句话出自 Brief 原文的哪一段"，
它是溯源性证据，不是外部佐证。** 研究阶段要补的是后者 ——
一条"续航 18 小时"即使能在 Brief 里指出原句，也仍然需要一个
第三方测评或检测报告来佐证它。

因此 `gap_note` 在"`MISSING` 且有来源"时给一句说明（见该函数）。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Final

from rednote.modules.brief.schema import FIELD_BY_PATH
from rednote.modules.brief.types import (
    CATEGORIES_REQUIRING_EVIDENCE,
    CATEGORY_EXTERNAL_EVIDENCE,
    CATEGORY_SOURCE_FACT,
    RESOLUTION_CONFIRMED,
    RESOLUTION_NOT_APPLICABLE,
    RESOLUTION_PENDING,
)
from rednote.modules.research.types import (
    FORMAT_MEDIA_ANY,
    FORMAT_MEDIA_DOCUMENT,
    FORMAT_MEDIA_IMAGE,
    GAP_COVERED,
    GAP_MISSING,
    GAP_NOT_APPLICABLE,
    KEYWORD_MAX_CHARS,
    KEYWORD_SOURCE_CLAIM,
    KEYWORD_SOURCE_ENTITY,
    KEYWORD_SOURCE_FIELD,
    KEYWORD_SOURCE_PATTERN,
    MAX_FIELD_VALUES_PER_PATH,
    MAX_GAPS,
    MAX_KEYWORDS,
    MAX_PHRASE_CHARS,
    MAX_PROMPTS,
    MAX_SUGGESTED_KEYWORDS_PER_GAP,
    TASK_EVIDENCE_COLLECTION,
    TASK_FACT_VERIFICATION,
    ClaimInput,
    FieldValueInput,
    FormatHint,
    Gap,
    Keyword,
    Prompt,
)

# ---------------------------------------------------------------------------
# 实体字段（关键词的"主体"）
# ---------------------------------------------------------------------------
# 关键词必须能定位到具体产品，否则跨品牌会串（G2 验收原文
# "跨品牌事实不串"）。主体词取自这三个字段 —— 它们是 BriefSchema 里
# **唯一**描述"这是什么产品"的字段。
#
# 用**字段路径常量**而不是硬编码字符串：WP18 明确"路径一经发布只增不改"，
# 硬编码会在改名时静默失配（`FIELD_BY_PATH.get()` 返回 None，主体词为空，
# 所有关键词退化成裸短语，而**不报任何错**）。

ENTITY_FIELD_PATHS: Final[tuple[str, ...]] = (
    "product.brand",
    "product.name",
    "product.model_code",
)

# ---------------------------------------------------------------------------
# 平台检索惯例后缀（PATTERN 类关键词）
# ---------------------------------------------------------------------------
# **这是本包唯一不来自 Brief 的词源**，因此它必须显式可识别。
#
# 四个后缀的判据是"小红书研究阶段真正会被搜的四种意图"：
#
#   测评  —— 找第三方主观评价（对抗"只有官方说辞"）
#   口碑  —— 找真实使用者反馈（含差评）
#   对比  —— 找与竞品的横向比较（Brief 里通常没有）
#   真实  —— 找"普通人怎么用"，过滤商业推广
#
# **刻意不加"推荐"**：它太泛，搜出来的结果几乎全是商业内容 ——
# 那正是研究阶段要对抗的东西。加它等于让运营花时间看广告。
PATTERN_SUFFIXES: Final[tuple[str, ...]] = ("测评", "口碑", "对比", "真实")

#: 一条缺口最多给几个 PATTERN 词（从 `PATTERN_SUFFIXES` 前 N 个取）。
#: 不给全部四个：一条缺口配 4 个通用后缀 + 若干具体词会让
#: `MAX_SUGGESTED_KEYWORDS_PER_GAP` 全被通用词占满，
#: 而**具体词才是这条缺口真正需要的东西**。
MAX_PATTERN_PER_GAP: Final[int] = 2


# ---------------------------------------------------------------------------
# 文本归一化（纯函数，无副作用）
# ---------------------------------------------------------------------------


def normalize_term(raw: object) -> str:
    """把一个取值归一化成可放进关键词的"词"。

    **为什么必须归一化**：`structured_fields` 是 jsonb，取值可能带
    首尾空白、换行、超长句子。而"关键词"是要粘到外部平台搜索框里的东西 ——
    换行会让搜索框里的内容被截断成两截，超长句子搜不出东西。

    超长时**截断到 `KEYWORD_MAX_CHARS`** 而不是丢弃：
    丢弃会让运营看不到"这里本来有个词"，而截断至少保留了一个可搜的片段。
    """
    if raw is None:
        return ""
    text = str(raw)
    # 换行/制表/连续空白 → 单个空格（关键词里不能有换行）。
    text = " ".join(text.split())
    if len(text) > KEYWORD_MAX_CHARS:
        text = text[:KEYWORD_MAX_CHARS].rstrip()
    return text


def join_terms(*parts: str) -> str:
    """把若干片段拼成一个关键词，跳过空片段。

    **用空格连接而不是无分隔拼接**：中文关键词里"某产品续航"与
    "某产品 续航"在平台上的检索行为不同 —— 后者是两个词的组合，
    更容易命中同时提到两者的笔记。而前者的语义要看分词器心情。
    """
    cleaned = [normalize_term(part) for part in parts]
    return " ".join(part for part in cleaned if part)


def extract_phrase(text: str) -> str:
    """从一条结论的文本里取一个短语，作为关键词的一部分。

    **取前 `MAX_PHRASE_CHARS` 个字符，并在标点处断开**：
    一条结论可能是"我们产品的续航在实验室条件下可达 18 小时，
    但这是极限值"。整句当关键词搜不出东西；取开头到第一个标点
    （"我们产品的续航在实验室条件下可达 18 小时"）仍然太长；
    因此先按标点切、再截断。

    **不做分词/NLP**：那需要引入依赖，且"为什么切在这里"无法向运营解释。
    按标点切是**可解释的**（"它取了你这句话的第一个分句"）。
    """
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return ""
    for separator in ("，", ",", "。", ".", "；", ";", "！", "!", "？", "?", "\n"):
        index = cleaned.find(separator)
        if 0 < index <= MAX_PHRASE_CHARS:
            cleaned = cleaned[:index]
            break
    if len(cleaned) > MAX_PHRASE_CHARS:
        cleaned = cleaned[:MAX_PHRASE_CHARS].rstrip()
    return cleaned


def has_evidence(
    *,
    evidence_segment_id: object | None,
    evidence_url: str | None,
) -> bool:
    """一条结论是否带可追溯来源。

    判据与 WP18 的 `ClaimDraft.__post_init__` 一致（片段或链接，
    有一个就算带）。**这是本模块唯一"与 WP18 判据重复"的地方**，
    必须重复 —— 因为纯函数收到的已经是"落库后的行"，而不是
    `ClaimDraft`。用一个工厂函数收口这处重复，让它只有一处实现。
    """
    if evidence_segment_id is not None:
        return True
    return bool(evidence_url and str(evidence_url).strip())


def field_label(path: str) -> str:
    """字段路径 → 中文标签；未知路径退回路径本身。

    **为什么退回路径而不是抛错**：`brief.schema.field_spec` 对未知路径
    抛 `ValueError`（那是**写入点**的正确行为 —— 它防的是把拼错的路径
    写进库）。而这里是**读取点**：历史 claim 的 `field_path` 可能是
    一次已废弃的路径，那时读取应该降级展示（显示原始路径，让人看出
    "这是老数据"），而不是让整个研究页 500。

    同一函数在"写"与"读"两个位置上职责不同，这是刻意的不对称。
    """
    spec = FIELD_BY_PATH.get(path)
    if spec is None:
        return path
    return spec.label


# ---------------------------------------------------------------------------
# 关键词派生
# ---------------------------------------------------------------------------


def _first_value_per_path(
    fields: tuple[FieldValueInput, ...], paths: tuple[str, ...]
) -> Iterator[tuple[str, FieldValueInput, str]]:
    """按 `paths` 的顺序产出 `(路径, 字段输入, 首个取值)`。

    ## 为什么抽出来（阶段 7 简化）

    `entity_terms` 与 `derive_keywords` 的第 1 步原本各写一份
    "按 `ENTITY_FIELD_PATHS` 顺序遍历 → 取 `values[:1]` → 归一化"，
    两处必然漂移。而且**顺序语义很微妙**，值得只有一处实现：

      · 遍历 `paths` 而不是 `fields` —— `FieldValueInput` 的传入顺序
        取决于数据库读取顺序，同一事务插入的行 `created_at` 相同
        → 顺序不确定（WP17 的 ISS-52）；
      · 只取 `values[:1]` —— 主体词每个字段只要一个（"品牌A"配
        "产品B"就够定位，多取会堆出一长串同义词）。

    抽成生成器而不是"返回 tuple 后各自处理"：两个调用方对空值
    的处置不同（`entity_terms` 去重、`derive_keywords` 交给 `add()`
    统一去重），生成器让各自保留自己的判据。
    """
    by_path = {item.path: item for item in fields}
    for path in paths:
        item = by_path.get(path)
        if item is None:
            continue
        for value in item.values[:1]:
            term = normalize_term(value)
            if term:
                yield path, item, term


def entity_terms(fields: tuple[FieldValueInput, ...]) -> tuple[str, ...]:
    """主体词（品牌 / 产品名 / 型号），按 BriefSchema 的字段顺序。

    **顺序固定为"品牌 → 产品名 → 型号"**（`ENTITY_FIELD_PATHS` 的顺序），
    不按输入顺序：`FieldValueInput` 的传入顺序取决于数据库读取顺序，
    而在同一事务里插入的行 `created_at` 相同 → 顺序不确定
    （WP17 的 ISS-52）。关键词列表每次刷新换顺序是最难查的一类反馈。

    **这里额外去重**：`entity_terms` 的产物要拼成检索前缀
    （`primary_subject`），而"品牌"与"产品名"填了同一个词是常见情形，
    不去重会拼出"某品牌 某品牌 型号"。
    """
    terms: list[str] = []
    for _path, _item, term in _first_value_per_path(fields, ENTITY_FIELD_PATHS):
        if term not in terms:
            terms.append(term)
    return tuple(terms)


def primary_subject(terms: tuple[str, ...]) -> str:
    """主体词拼成的检索前缀。

    取**全部**主体词（而不是只取产品名）：型号常常是检索的关键限定词
    （搜"某产品"会出来一堆别的型号）。全取会让词变长，但那正是
    "限定得足够窄"的代价 —— 而研究阶段的问题是搜太泛，不是搜太窄。
    """
    return join_terms(*terms)


def derive_keywords(
    claims: tuple[ClaimInput, ...],
    fields: tuple[FieldValueInput, ...],
    *,
    limit: int = MAX_KEYWORDS,
) -> tuple[tuple[Keyword, ...], bool]:
    """派生关键词列表。返回 `(keywords, truncated)`。

    **顺序**（稳定，可断言）：

      1. 主体词（`ENTITY`）—— 运营第一眼要看到"我在搜什么产品"
      2. 字段取值（`FIELD`）—— 最具体的检索词
      3. 缺口相关结论的短语（`CLAIM`）—— 只取**有缺口**的结论
      4. 平台惯例后缀（`PATTERN`）—— 最后，且整类被标注

    **第 3 步为什么只取缺口相关的 claim**：一条已经有来源的结论
    （`COVERED`）不需要再去搜 —— 给它的关键词只会稀释列表。
    而"该搜什么"的答案恰恰在缺口里。

    **逐条 claim 生成词的条数上限**：一条 claim 生成 1 个 `CLAIM` 词。
    不给多条（后缀组合）—— 一个 claim 配四个后缀会瞬间吃掉 `MAX_KEYWORDS`，
    让后面更重要的缺口排不进来。

    ## 简化记录（阶段 7）

    第一版是 113 行的单函数：四个步骤内联在同一层，每步都要
    自己处理"到上限就提前返回"。现在四步各是一个生成器
    （`_entity_keywords` / `_field_keywords` / `_claim_keywords` /
    `_pattern_keywords`），本函数只做"依次消费 + 去重 + 判上限"。

    **这不只是缩短行数**：原来"到上限"的返回路径有 4 处
    （每步一个 `return tuple(keywords), True`），加第 5 类词源时
    必然会漏掉一处的 `truncated` 标记 —— 而漏掉的后果是
    "结果被截断了但前端不知道"，运营以为已经看全。
    收成一处之后，`truncated` 只在一个地方被决定。
    """
    keywords: list[Keyword] = []
    seen: set[str] = set()
    subject_prefix = primary_subject(entity_terms(fields))

    for item in _iter_keyword_candidates(
        claims=claims, fields=fields, subject_prefix=subject_prefix
    ):
        key = item.text.casefold()
        if not item.text or key in seen:
            continue
        seen.add(key)
        keywords.append(item)
        if len(keywords) >= limit:
            return tuple(keywords), True
    return tuple(keywords), False


def _iter_keyword_candidates(
    *,
    claims: tuple[ClaimInput, ...],
    fields: tuple[FieldValueInput, ...],
    subject_prefix: str,
) -> Iterator[Keyword]:
    """四类关键词的候选流，**按优先级顺序产出**（去重与上限由调用方处理）。

    顺序是设计的一部分（`test_derive_keywords_has_four_kinds_in_order`
    按此断言），因此它写在一个地方而不是散在四个函数里。
    """
    yield from _entity_keywords(fields)
    yield from _field_keywords(fields, subject_prefix=subject_prefix)
    yield from _claim_keywords(claims, subject_prefix=subject_prefix)
    yield from _pattern_keywords(subject_prefix)


def _entity_keywords(fields: tuple[FieldValueInput, ...]) -> Iterator[Keyword]:
    """主体词（`ENTITY`）：产品的品牌 / 产品名 / 型号。"""
    for path, item, term in _first_value_per_path(fields, ENTITY_FIELD_PATHS):
        yield Keyword(
            text=term,
            source_kind=KEYWORD_SOURCE_ENTITY,
            terms=(term,),
            source_field_path=path,
            source_field_label=item.label,
        )


def _field_keywords(
    fields: tuple[FieldValueInput, ...], *, subject_prefix: str
) -> Iterator[Keyword]:
    """字段取值（`FIELD`）：目标人群 / 购买场景 / 痛点等。

    **跳过实体字段**（上一步已产出）；**按路径排序**（`fields` 的顺序
    来自 jsonb，不保序 —— 见 `service.field_value_inputs`）。
    """
    for item in sorted(fields, key=lambda entry: entry.path):
        if item.path in ENTITY_FIELD_PATHS:
            continue
        for value in item.values[:MAX_FIELD_VALUES_PER_PATH]:
            term = normalize_term(value)
            if not term:
                continue
            yield Keyword(
                text=join_terms(subject_prefix, term),
                source_kind=KEYWORD_SOURCE_FIELD,
                terms=tuple(filter(None, (subject_prefix, term))),
                source_field_path=item.path,
                source_field_label=item.label,
            )


def _claim_keywords(
    claims: tuple[ClaimInput, ...], *, subject_prefix: str
) -> Iterator[Keyword]:
    """缺口相关结论的短语（`CLAIM`）：**只取仍缺证据的那些**。

    判据收口在 `claim_needs_search`（= 类别需要证据 **且** 状态是
    `MISSING`）。已覆盖的不用再搜；`STRATEGY` / `UNCONFIRMED` 类
    不构成需要证据的缺口，也不该在这里造词。

    **排序是必须的**：`claims` 来自数据库，同一事务插入的行时间戳
    相同（WP17 的 ISS-52），不显式排序会让关键词顺序每次刷新都变。
    """
    for claim in sorted(claims, key=lambda entry: (entry.field_path, str(entry.claim_id))):
        if not claim_needs_search(claim):
            continue
        phrase = extract_phrase(claim.text)
        if not phrase:
            continue
        yield Keyword(
            text=join_terms(subject_prefix, phrase),
            source_kind=KEYWORD_SOURCE_CLAIM,
            terms=tuple(filter(None, (subject_prefix, phrase))),
            source_field_path=claim.field_path,
            source_field_label=claim.field_label,
            related_claim_id=claim.claim_id,
        )


def _pattern_keywords(subject_prefix: str) -> Iterator[Keyword]:
    """平台惯例后缀（`PATTERN`）：`测评` / `口碑` / `对比` / `真实`。

    主体词为空时**一个都不给**：`join_terms` 会让它们退化成
    裸后缀（"测评"），那不是一个可用的关键词（搜出来全平台的内容）。
    """
    if not subject_prefix:
        return
    for suffix in PATTERN_SUFFIXES:
        yield Keyword(
            text=join_terms(subject_prefix, suffix),
            source_kind=KEYWORD_SOURCE_PATTERN,
            terms=(subject_prefix, suffix),
        )


def claim_needs_gap(claim: ClaimInput) -> bool:
    """这条结论是否构成一条**缺口条目**（要不要出现在 `gaps[]` 里）。

    **只判类别**，判据完全来自 WP18 的 `CATEGORIES_REQUIRING_EVIDENCE`。

    ## 为什么它**不**排除 `NOT_APPLICABLE`（本机实测踩到过的缺陷）

    "已标不适用"的条目**必须仍然出现在 `gaps[]` 里**，显示为
    `state='NOT_APPLICABLE'`。若在这里把它排除掉：

      · 运营标记"这条对本次生产无关"之后，**那一条就从清单里消失了** ——
        他再也看不到自己的判断，也**无法取消这个标记**
        （要取消得先找到它，而它已经被过滤掉了）；
      · `coverage_summary` 的计数会少一条，`total` 与"Brief 里
        需要证据的结论数"对不上 —— 而这个不一致没有任何地方会报错。

    因此"类别"与"状态"两件事必须分开：类别决定**要不要出现在清单里**，
    状态决定**它现在处于哪个阶段**。判据表见模块 docstring。
    """
    return claim.category in CATEGORIES_REQUIRING_EVIDENCE


# ---------------------------------------------------------------------------
# 缺口派生
# ---------------------------------------------------------------------------


def gap_state(claim: ClaimInput) -> str:
    """一条需要证据的结论 → 缺口状态。

    **调用前必须先用 `claim_needs_gap` 过滤** —— 本函数不对"类别是否
    需要证据"做判断，它只管"已确认是缺口之后，现在处于哪个状态"。
    把两个判断混在一个函数里会让"不是缺口"与"缺口已覆盖"难以区分，
    而它们在响应里的处置完全不同（前者不进 `gaps[]`）。

    判据是 `resolution_status`（**不是** `has_evidence`）——
    理由见模块 docstring 的"为什么缺口判据不能用有没有来源"一节。
    **这一条是本包最实质的一处修正**：按 `has_evidence` 实现的话，
    `gaps[]` 会全是 `COVERED`（数据库层保证了"需要证据的类别必有来源"），
    于是"步骤 2 确认完整"变成一个永远已经完成的按钮 ——
    不报错、不影响任何断言，只有真库上的运营会发现清单永远是空的。
    """
    if claim.resolution == RESOLUTION_NOT_APPLICABLE:
        # 运营的明确判断优先（见模块 docstring 第 2 条）。
        return GAP_NOT_APPLICABLE
    if claim.resolution == RESOLUTION_CONFIRMED:
        return GAP_COVERED
    return GAP_MISSING


def claim_needs_search(claim: ClaimInput) -> bool:
    """这条结论**现在还值不值得去搜**。

    **这是一个独立的判据，且它必须是两个判据的合取。**
    它回答的问题与另外两个都不一样：

    | 函数 | 回答的问题 |
    |---|---|
    | `claim_needs_gap` | 它算不算一条**缺口条目**（要出现在缺口清单里） |
    | `gap_state` | **已确认是缺口**之后，它现在处于哪个状态 |
    | `claim_needs_search` | 还要不要**为它生成检索词** |

    三者容易混，且混错了**不报错**——本机实测踩到两次，两个方向都会错：

      · **只用 `claim_needs_gap`** → 已 `COVERED` 的结论也被生成
        检索词。后果：一条 `CONFIRMED` 且带来源的 `EXTERNAL_EVIDENCE`，
        它那句"已有第三方检测"被当成**推荐关键词**推给运营 ——
        运营照着搜一轮，找一个系统已经确认有了的东西。
      · **只用 `gap_state != GAP_MISSING`** → `STRATEGY` 类结论混进来。
        因为 `gap_state` **刻意不判类别**（它接在"已确认是缺口"之后），
        所以一条 `STRATEGY + PENDING + 无证据` 会被它判成 `MISSING`。
        后果：策略判断（"主打学生宿舍"）被当成需要外部证据的缺口去搜 ——
        而它是**产品决策**，搜是搜不出证据的，只会让运营白忙。

    合取把两者各管一半：**类别决定"要不要证据"，状态决定"证据够不够"**。

    **公开函数而不是内联两处 `and`**：同一个条件要用在三个地方
    （关键词派生、缺口建议词、测试），内联三次必然有两次先漂移。
    """
    return claim_needs_gap(claim) and gap_state(claim) == GAP_MISSING


def derive_gaps(
    claims: tuple[ClaimInput, ...],
    *,
    limit: int = MAX_GAPS,
) -> tuple[tuple[Gap, ...], bool]:
    """派生覆盖缺口清单。返回 `(gaps, truncated)`。

    **排序：`MISSING` 在前，再按 `(field_path, claim_id)`**。

    把 `MISSING` 排最前是因为它是**唯一阻断态** —— 运营打开页面
    第一眼要看到"还差什么"，而不是"已经够了什么"。
    `COVERED` / `NOT_APPLICABLE` 是已完成项，排在后面。

    平局用 `(field_path, claim_id)` 而不是 `created_at`：
    同一批插入的行时间戳相同（WP17 的 ISS-52）。

    `suggested_keywords` 在 `derive_gaps` 里**为空** —— 它由
    `build_gap_keywords` 单独填。理由：建议词需要知道主体词，
    而那是 `derive_keywords` 的输入；把主体词再传一遍会引入
    "两处各算一次主体词"的漂移点。
    """
    gaps: list[Gap] = []
    for claim in claims:
        if not claim_needs_gap(claim):
            continue
        state = gap_state(claim)
        gaps.append(
            Gap(
                claim_id=claim.claim_id,
                field_path=claim.field_path,
                field_label=claim.field_label,
                claim_text=normalize_term(claim.text),
                category=claim.category,
                resolution=claim.resolution,
                state=state,
                note=gap_note(claim, state),
            )
        )

    gaps.sort(key=lambda gap: (0 if gap.state == GAP_MISSING else 1, gap.field_path, str(gap.claim_id)))
    truncated = len(gaps) > limit
    return tuple(gaps[:limit]), truncated


def gap_note(claim: ClaimInput, state: str) -> str | None:
    """缺口状态的补充说明。**只在"状态需要解释"时非空**。

    两种触发，都是为了让运营看懂"为什么系统说这条缺东西"：

    | 触发 | 说明什么 |
    |---|---|
    | `MISSING` 且有来源 | **解释"为什么有出处还是缺口"**：`brief_claims` 上的来源是"出自原文哪一段"（溯源性），研究阶段要补的是外部佐证 —— 两者不是一回事 |
    | `MISSING` 且状态是 `CONFIRMED` | 不会发生（`CONFIRMED` 会判 `COVERED`），但保留这条判据以防将来 `gap_state` 改动后漏掉 |

    第一条是**本包最容易被质疑的一句话的答案**。运营看到
    "续航 18 小时"说"这明明有出处" —— 系统必须能回答：
    出处是"这句话在 Brief 原文里的位置"，佐证是"第三方说它是对的"。
    没有这条说明，运营会认为系统在瞎报缺口。
    """
    if state != GAP_MISSING:
        return None
    if claim.has_evidence:
        return (
            "该结论在 Brief 原文里有出处（原文片段或链接），"
            "但研究阶段需要的是**外部佐证**（第三方测评、检测报告、"
            "公开数据）—— 这两者不是一回事。"
        )
    if claim.resolution != RESOLUTION_PENDING:
        return (
            f"该结论的确认状态是 {claim.resolution}，但没有任何可追溯来源 ——"
            "请核对这条数据的来源（数据库约束应当拦住这种情况）。"
        )
    return None


def gap_needs_search(gap: Gap) -> bool:
    """这条**缺口条目**现在还值不值得去搜。

    与 `claim_needs_search` 的关系：`Gap` 是已经过类别过滤的产物
    （`derive_gaps` 只收 `claim_needs_gap` 为真的条目），因此这里
    判据退化成"状态是不是 `MISSING`"—— **类别那一半已经由构造过程保证**。

    **刻意不写成 `gap.state == GAP_MISSING` 内联**：那个表达式要在
    本模块与测试里各出现一次，而它表达的是一个**业务判断**
    （"已覆盖的不用再搜"），内联会让它在两处各自演化。
    """
    return gap.state == GAP_MISSING


def build_gap_keywords(
    gaps: tuple[Gap, ...],
    *,
    subject_prefix: str,
    limit: int = MAX_SUGGESTED_KEYWORDS_PER_GAP,
) -> tuple[Gap, ...]:
    """给每条缺口补上建议关键词（**具体词优先，通用后缀垫底**）。

    每条缺口的建议词 = 缺口的短语（`CLAIM` 来源）+
    平台惯例后缀（`PATTERN` 来源，最多 `MAX_PATTERN_PER_GAP` 个）。

    **具体词必须排在通用后缀之前**，且**通用后缀数量受限**：
    一条缺口配 4 个后缀会让"该搜什么"退化成"到处搜这个词" ——
    而这条缺口真正需要的是那个具体短语。

    **只有 `MISSING` 的缺口才给建议词**（其余状态**给空元组**）。
    理由与 `derive_keywords` 第 3 步同一条（判据收口在
    `gap_needs_search`）：`COVERED` 的缺"已经够了"，再建议它去搜
    等于让运营白做一轮；`NOT_APPLICABLE` 是运营明确判定
    "对本次生产无关"的项目，系统不该反过来推着人去搜它。
    **空元组与"没有建议"是同一件事**，前端据此隐藏建议区 ——
    若统一给词，前端就无法区分"建议是空的"与"这条不需要建议"。

    ## 简化记录（阶段 7）

    第一版把 `Gap(...)` 构造了两次（"不需要搜"与"需要搜"两个分支
    各一份完整的 9 字段构造）—— 加字段时必然只改一处。
    改用 `dataclasses.replace`（`Gap` 是 frozen dataclass）：
    只写**变化的那一个字段**，其余字段由 replace 保证与入参一致。
    """
    enriched: list[Gap] = []
    for gap in gaps:
        if not gap_needs_search(gap):
            # 明确写 `suggested_keywords=()`：`replace` 会继承构造它的
            # 那个 Gap 上的值，而 `derive_gaps` 产出的 Gap 本就是空元组
            # —— 但那是**上游的实现细节**，这里显式声明意图（"不搜
            # 就不给建议"），将来 `derive_gaps` 若改成预先填词，
            # 本分支仍然正确。
            enriched.append(replace(gap, suggested_keywords=()))
            continue
        enriched.append(replace(gap, suggested_keywords=_suggest_keywords(gap, subject_prefix, limit)))
    return tuple(enriched)


def _suggest_keywords(gap: Gap, subject_prefix: str, limit: int) -> tuple[str, ...]:
    """一条 `MISSING` 缺口的建议词：**具体短语优先，通用后缀垫底**。

    抽成独立函数是因为"具体词 + 后缀"这两步有明确的先后与上限语义，
    塞在 `build_gap_keywords` 的循环里会让那个循环同时承担
    "遍历 + 分支 + 组装词"三件事（嵌套也因此升到 4 层）。
    """
    suggestions: list[str] = []
    specific = join_terms(subject_prefix, extract_phrase(gap.claim_text))
    if specific:
        suggestions.append(specific)
    if subject_prefix:
        for suffix in PATTERN_SUFFIXES[:MAX_PATTERN_PER_GAP]:
            candidate = join_terms(subject_prefix, suffix)
            if candidate and candidate not in suggestions:
                suggestions.append(candidate)
    return tuple(suggestions[:limit])


# ---------------------------------------------------------------------------
# 提示词组装
# ---------------------------------------------------------------------------


def build_prompts(
    gaps: tuple[Gap, ...],
    fields: tuple[FieldValueInput, ...],
    *,
    limit: int = MAX_PROMPTS,
) -> tuple[tuple[Prompt, ...], bool]:
    """组装可复制的提示词。返回 `(prompts, truncated)`。

    **两条提示词，两种任务类别，判据是"要拿回什么形状的东西"**：

    | 类别 | 什么时候给 | 要拿回什么 |
    |---|---|---|
    | `FACT_VERIFICATION` | 有 `MISSING` 缺口时，**每条缺口一条** | 来源（证明这条结论真假的东西） |
    | `EVIDENCE_COLLECTION` | 有缺口时给**一条总览** | 清单（把搜到的整成可提交的证据表） |

    **总览那条必须有**：一条缺口一条提示词会造出 20 个"我要怎么证明它"，
    而运营实际的做法是"一次性搜一轮，回头整理"。总览那条承载后者。

    **提示词文本由服务端给死，不让前端拼**（验收"复制"的落点）：
    前端拼的话，"同样的缺口在不同页面复制出不同的提示词"，
    而复制出去的内容直接决定研究质量 —— 它必须是可版本化、
    可 diff 的文本（`DERIVATION_RULES_VERSION` 就是它的版本号）。

    协议原文只要求"可复制提示词与建议返回格式"，**没有要求提示词可编辑**。
    可编辑化归 WP23（`prompt_templates` 表属 Prompt 版本包）。
    """
    prompts: list[Prompt] = []
    missing_gaps = tuple(gap for gap in gaps if gap.state == GAP_MISSING)
    subject = primary_subject(entity_terms(fields)) or "该产品"

    for gap in missing_gaps:
        prompts.append(_verification_prompt(gap, subject))
        if len(prompts) >= limit:
            return tuple(prompts), True

    if missing_gaps:
        prompts.append(_collection_prompt(missing_gaps, subject))
        if len(prompts) > limit:
            return tuple(prompts[:limit]), True

    return tuple(prompts), False


def _verification_prompt(gap: Gap, subject: str) -> Prompt:
    """一条缺口的核实提示词。

    **`media` 由缺口类别决定**（`format_media_for_category`）——
    这不是装饰：`SOURCE_FACT` 是"来自 Brief 原文的事实"，
    它的缺口绝大多数是"原文那一段没截到"，所以让它去找一篇外部测评
    是**方向错的**（应该补截图）；`EXTERNAL_EVIDENCE` 才是真的
    要外部文章，而它的形状（测评 / 检测报告 / 公告）不该由系统替
    运营预设，因此是 `ANY`。

    本机实测教训：`format_media_for_category` 一度是**死代码** ——
    它有完整的实现与测试，但从未被 `_verification_prompt` 调用，
    于是 `media` 对两类缺口都是 `ANY`。测试之所以没发现，
    是因为它只断言 `media in FORMAT_MEDIA_KINDS`（取值域内即通过）
    —— 一个"断言太弱"的典型：测试测的是**类型**，
    而缺陷在**取值**。现在的测试按类别断言具体值。
    """
    media = format_media_for_category(gap.category)
    text = (
        f"请帮我核实关于「{subject}」的这条信息：\n"
        f"「{gap.claim_text}」\n\n"
        f"它对应 Brief 的字段「{gap.field_label}」。\n"
        "请优先找**第三方来源**（测评、检测报告、官方公告、权威媒体）；\n"
        "如果找到互相矛盾的说法，请把两边都列出来，不要只挑支持的一方。\n"
        "请对每一处说法给出：原文片段、来源链接或截图、发布日期。\n"
        "如果找不到任何来源，请直接说「没有找到」——不要推测。"
    )
    return Prompt(
        task_kind=TASK_FACT_VERIFICATION,
        title=f"核实：{gap.field_label}",
        text=text,
        format_hint=FormatHint(
            media=media,
            fields=("quote", "sourceUrl", "publishedAt", "screenshot"),
            description=(
                "每条说法给一段原文片段 + 来源链接或截图 + 发布日期。"
                "找不到来源的明确写「没有找到」。"
                if media != FORMAT_MEDIA_IMAGE
                else "这条结论本来出自 Brief 原文，最可能是原文那一段没有被截取到 ——"
                "请优先补**截图或原文截取**，并给出它在原文里的位置（页 / 段）。"
            ),
        ),
        related_field_paths=(gap.field_path,),
        keywords=gap.suggested_keywords,
    )


def _collection_prompt(missing_gaps: tuple[Gap, ...], subject: str) -> Prompt:
    lines = [
        f"以下是关于「{subject}」还没有找到来源的信息，"
        "请帮我整理一份研究成果清单：",
        "",
    ]
    for index, gap in enumerate(missing_gaps, start=1):
        lines.append(f"{index}. [{gap.field_label}] {gap.claim_text}")
    lines.extend(
        [
            "",
            "请把已经找到的资料整理成表格，一行一条，包含：",
            "· 它对应上面第几条",
            "· 原文里最关键的那句话",
            "· 来源链接或文件名 + 发布日期",
            "· 这条资料是支持、反驳、还是无关",
            "",
            "**没有找到对应资料的条目请保留在表里并标注「未找到」**——",
            "不要删掉它们，我需要知道还缺哪些。",
        ]
    )
    return Prompt(
        task_kind=TASK_EVIDENCE_COLLECTION,
        title="整理研究成果清单",
        text="\n".join(lines),
        format_hint=FormatHint(
            media=FORMAT_MEDIA_DOCUMENT,
            fields=("gapIndex", "quote", "sourceRef", "publishedAt", "stance"),
            description=(
                "一张表：缺口序号 / 原文片段 / 来源（链接或文件名）/ 发布日期 / "
                "支持或反驳。未找到的条目保留并标注「未找到」。"
            ),
        ),
        related_field_paths=tuple(gap.field_path for gap in missing_gaps),
        keywords=tuple(
            keyword
            for gap in missing_gaps
            for keyword in gap.suggested_keywords
        )[:MAX_KEYWORDS],
    )


# ---------------------------------------------------------------------------
# 汇总判据（供 api / service / 测试共用）
# ---------------------------------------------------------------------------


def coverage_summary(gaps: tuple[Gap, ...]) -> dict[str, int]:
    """缺口的分类计数。**顺序与取值都由 `GAP_STATES` 决定**。

    `blockingCount` 单独给出来而不是让前端从 `gaps[]` 里数：
    前端数的话，"什么算阻断"就成了前端的判断 ——
    而运营看到的"还差 3 条"必须与服务端下一步校验用的是同一个数。
    """
    counts = {state: 0 for state in (GAP_COVERED, GAP_MISSING, GAP_NOT_APPLICABLE)}
    for gap in gaps:
        counts[gap.state] = counts.get(gap.state, 0) + 1
    return {
        "total": len(gaps),
        "coveredCount": counts[GAP_COVERED],
        "missingCount": counts[GAP_MISSING],
        "notApplicableCount": counts[GAP_NOT_APPLICABLE],
        "blockingCount": counts[GAP_MISSING],
    }


def format_media_for_category(category: str) -> str:
    """某类结论的缺口，默认建议拿回什么介质。

    `SOURCE_FACT` 的证据**通常已经**在 Brief 原文里（它是"来自原文的事实"），
    因此缺口更多是"原文没截到那一段" → 建议 `IMAGE`（补截图）。
    `EXTERNAL_EVIDENCE` 是"需要外部来源" → 建议 `ANY`
    （可能是一篇测评，也可能是检测报告，不该由系统替运营决定）。
    """
    if category == CATEGORY_SOURCE_FACT:
        return FORMAT_MEDIA_IMAGE
    if category == CATEGORY_EXTERNAL_EVIDENCE:
        return FORMAT_MEDIA_ANY
    return FORMAT_MEDIA_ANY


__all__ = [
    "ENTITY_FIELD_PATHS",
    "MAX_PATTERN_PER_GAP",
    "PATTERN_SUFFIXES",
    "build_gap_keywords",
    "build_prompts",
    "claim_needs_gap",
    "claim_needs_search",
    "coverage_summary",
    "derive_gaps",
    "derive_keywords",
    "entity_terms",
    "extract_phrase",
    "field_label",
    "format_media_for_category",
    "gap_needs_search",
    "gap_note",
    "gap_state",
    "has_evidence",
    "join_terms",
    "normalize_term",
    "primary_subject",
]