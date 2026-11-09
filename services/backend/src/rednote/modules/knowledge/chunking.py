"""切块器（**纯函数**，不碰数据库）。

WP20 建了 `knowledge_chunks` 表但**不生产 chunk** ——
生产归本包（`docs/m2/WP20-侦察交接.md` L71 原文：
「本包只建表，不生产 chunk（生产归 WP21：切块与 embedding）」）。

**为什么是纯函数**：切块是「(文本, 切块器版本) → 块列表」的确定映射。
把它做成纯函数有三个直接收益：
  1. 可被单测穷举（边界、空文本、超长、overlap 大于块长……），
     不需要真库；
  2. 可**重算** —— 换切块器版本时，"同一份内容 + 新切块器"必须给出
     可预测的结果，而不是"跑一遍看看"；
  3. 让 `embedding_service` 的事务边界里**没有业务判定**（判定在纯函数里
     已经做完，服务层只负责写）。

---

## 一、切块策略：为什么是「段落优先 + 字符上限 + 重叠」

### 切块粒度不是自由选择，它被两个约束夹住

| 约束 | 来源 | 含义 |
|---|---|---|
| **下界：不能切碎事实** | 验收原文「近似品牌案例不串事实」的隐含前提 | 「续航 18 小时（实验室条件）」若被切成两块，召回时只拿到前半句 —— 于是"18 小时"变成一条**没有限定条件的事实**。切块切断了限定条件，等于**制造**了一条错误事实 |
| **上界：不能超过模型窗口** | embedding 模型的输入长度限制 | 超长文本会被模型静默截断（不同模型行为不同），于是"块的后半段从未参与向量计算" —— 而**不报错** |

**因此策略是**：先按**段落边界**（`\n\n`）聚合，聚合到接近上限才切；
只有当**单一段落本身就超上限**时，才按字符硬切。

### 为什么段落优先而不是固定字符窗口

固定窗口（每 500 字切一刀）的实现最简单，但它**必然**切断句子 ——
中文没有词边界，500 字处落在句中的概率极高。段落边界是作者
自己给出的语义边界，用它是最低成本地尊重语义。

**代价（诚实记录）**：段落长度分布极不均匀。一段 50 字的段落与一段
2000 字的段落，前者会产生"小碎块"。本实现的选择是**接受小碎块**
（小碎块的信息量低但不会丢信息），而不是"把相邻小段落合并到 500 字"
—— 后者会**跨段落合并**，而跨段落往往是跨话题。已登记为待定项
（OP-169：切块器版本升级时的重切策略，含粒度调优）。

### 重叠（overlap）的作用与代价

重叠 80 字（默认）。它解决的是"事实恰好跨在切点上"：
「A 的条件」在块 1 末尾、「A 的结论」在块 2 开头时，
有重叠则**至少有一个块同时含两者**。

**代价**：重叠部分会被 embedding **两次**，且检索时可能
返回两个内容高度重复的块。本实现不做"去重"（那需要判断"两个块是否
实质相同"，而这是另一个策略问题）。取舍已登记。

---

## 二、`CHUNKER_VERSION` 为什么必须存在

它与 WP17 的 `parser_version`、WP18 的 `BRIEF_SCHEMA_VERSION`、
WP23 的模板版本是同一手法：**任何会影响产物内容的参数，都必须有一个
可追溯的版本标识**。

若不设版本号，改 `DEFAULT_CHUNK_CHARS` 之后：
  · 库里新旧 chunk 混在一起，**没有任何列能区分它们**；
  · 于是"这条知识要不要重切"无法判断 —— 只能全量重切（浪费）
    或全不重切（错误）。

**判据**：`CHUNKER_VERSION` 进 `knowledge_chunks.locator`，
这样"这一块是哪个切块器产的"是**每一行自带的事实**，不是全局配置。

---

## 三、`text_hash` 与"重切后内容变没变"

WP20 建了 `text_hash`（SHA-256 十六进制，`models.py` L486 原文：
「用于『重切后内容变没变』的判定 —— 不靠它就得逐字符比对，
而 chunk 数量可能上千」）。

本包用 `hash_chunk_text()` 计算它。**哈希的是规范化后的文本**
（见 `normalize_text`），不是原始文本 —— 因为换行符差异
（`\r\n` vs `\n`）不是内容差异，而它会让哈希变化，
于是"重切后发现全变了"。

---

## 四、`keywords` 从哪来

`knowledge_chunks.keywords text[]`（规格源 02 §8 原文
「供关键词召回兜底」）。本包**从块文本里派生**（见 `derive_keywords`）。

**为什么是简单派生而不是 NLP 分词**：本机没有中文分词依赖，
且它的用途是**兜底召回**（`SEARCH_KEYWORD` 模式），不是主路径。
一个朴素的派生（按标点切短语 + 去停用词 + 长度过滤 + 去重）
足以支撑"关键词能捞回语义漏掉的东西"，而引入分词器会新增
一个依赖与一套版本管理。已登记为待定项。
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

from rednote.modules.knowledge.embedding_types import (
    DEFAULT_CHUNK_CHARS,
    DEFAULT_CHUNK_OVERLAP_CHARS,
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    ChunkDraft,
)

#: **切块器版本**。任何影响产物内容的参数（块长、重叠、分段规则、
#: 关键词规则、哈希口径）变化时都必须递增它。
#:
#: 它被写进每个 chunk 的 `locator.chunkerVersion` —— 于是
#: "这一块是哪个切块器产的"是**行自带的事实**（见模块 docstring 第二节）。
CHUNKER_VERSION: Final[str] = "knowledge-wp21-v1"

#: `locator.unit` 的取值。
UNIT_CHAR: Final[str] = "CHAR"
UNIT_FIELD: Final[str] = "FIELD"

#: 段落分隔符。`\n\n`（一个空行）是 Markdown / 纯文本的通用段落边界。
_PARAGRAPH_SPLIT: Final[re.Pattern[str]] = re.compile(r"\n\s*\n")

#: 句子边界（用于硬切超长段落时尽量落在句末）。
#: 中英文标点都收：知识库内容可能混排。
_SENTENCE_SPLIT: Final[re.Pattern[str]] = re.compile(r"(?<=[。！？!?；;])")

#: 关键词派生的停用词。**刻意短**：它的作用是去掉"检索时必然命中
#: 全部文档"的词（那类词只会稀释关键词召回的信噪比）。
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "的", "了", "是", "在", "和", "与", "或", "及", "等", "这", "那",
        "有", "无", "不", "也", "就", "都", "而", "但", "为", "以", "对",
        "我们", "他们", "可以", "需要", "进行", "一个", "一种", "以及",
        "the", "and", "for", "with", "that", "this", "from", "are", "was",
    }
)

#: 关键词长度边界（字符数）。太短没有区分度，太长不是"词"。
#:
#: ⚠️ **长度过滤只对非 CJK 候选生效**（见 `derive_keywords`）：
#: 中文没有词边界，一个"候选"往往是整句 —— 用 24 字去卡它
#: 等于"只保留短句、丢掉长句"，而长句里的主题词恰恰更重要。
#: CJK 候选走 2-gram 切分，切完都是 2 字，长度过滤对它无意义。
_MIN_KEYWORD_CHARS: Final[int] = 2
_MAX_KEYWORD_CHARS: Final[int] = 24

#: 单块最多派生多少个关键词。
#:
#: 取 32（而不是 12）：中文 2-gram 的数量与块长成正比，
#: 一块 500 字约有 400 个去重 2-gram。上限太小会让关键词
#: **只覆盖块的开头** —— 查询"卧室"（在第 20 个 2-gram 位置）
#: 会匹配不上，兜底召回失效。
#:
#: **32 是一个取舍**：它覆盖约 32 个不同 2-gram，
#: 按词频排序后（见 `derive_keywords`）保留的是**主题词**，
#: 而不是"位置靠前的词"。取舍已登记（收口方向：
#: 有真实数据后按召回率校准这个数）。
MAX_KEYWORDS_PER_CHUNK: Final[int] = 32

#: CJK 字符的 Unicode 区间（用于判断候选是否含中文）。
_CJK_RE: Final[re.Pattern[str]] = re.compile(r"[\u4e00-\u9fff]")

#: CJK 关键词的 n-gram 长度。取 2：中文双字词覆盖了绝大多数实词
#: （"除湿"/"静音"/"卧室"），且比 3-gram（覆盖少）与 1-gram
#: （单字区分度低）更实用。
_CJK_NGRAM: Final[int] = 2

#: 关键词候选的分隔符（非词字符）。中英文都收。
_KEYWORD_SPLIT: Final[re.Pattern[str]] = re.compile(
    r"[\s,，、。；;：:！!？?（）()\[\]【】\"'“”‘’/\\|—\-_+=*#@$%^&~`<>{}]+"
)


def normalize_text(text: str) -> str:
    """规范化文本（**哈希与切块前都先走它**）。

    三件事：
      1. 统一换行符（`\r\n` / `\r` → `\n`）—— 它不是内容差异，
         但会让哈希变化，于是"重切后发现全变了"；
      2. 去掉行尾空白 —— 编辑器差异，同理；
      3. 去掉首尾空白。

    **不做的事（有意）**：不去多余空行（空行是段落边界，去掉会改变切块
    结果）；不做全角半角转换（那是内容变更）；不 strip 每行行首空白
    （Markdown 的缩进有语义 —— 列表层级）。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    return normalized.strip()


def hash_chunk_text(text: str) -> str:
    """块的文本摘要（SHA-256 十六进制，64 字符）。

    **先规范化再哈希**：见 `normalize_text` 的说明。
    与 WP20 的 `text_hash` CHECK（`char_length(text_hash) = 64`）对齐。
    """
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def count_tokens(text: str) -> int:
    """估算 token 数。

    **这是一个估算，不是精确分词** —— 本机没有与目标 embedding 模型
    配套的 tokenizer（OP-44 未定，模型未选型）。

    口径：**中文按 1 字符 ≈ 1 token，拉丁字母按 4 字符 ≈ 1 token**。
    这个粗估的用途只有两个：
      1. `knowledge_chunks.token_count` 列（`>= 0` 的 CHECK）；
      2. 判断"这一块会不会超过模型窗口"的量级。

    **不做精确分词的代价已登记**：若将来需要精确的 token 预算，
    必须在选型后引入配套 tokenizer，并递增 `CHUNKER_VERSION`
    （因为 token_count 会变，而它是落库的列）。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    # 向上取整：宁可高估（触发更保守的切块）也不要低估（超窗口被静默截断）。
    return cjk + (other + 3) // 4


def derive_keywords(text: str) -> tuple[str, ...]:
    """从块文本派生关键词（供 `SEARCH_KEYWORD` 兜底召回）。

    ### 两条路径（**必须分开**，这是本函数最容易写错的地方）

    | 候选形态 | 处理 | 为什么 |
    |---|---|---|
    | 含 CJK | **切 2-gram** | 中文没有词边界 —— 按标点切出来的"候选"往往是**整句**（"除湿机静音卧室"），它作为一个整体永远匹配不上查询里的"卧室"。切 2-gram 后"除湿/湿机/机静/静音/音卧/卧室"都能被查询命中 |
    | 纯拉丁/数字 | 原样（小写化） | 英文有空格边界，`_KEYWORD_SPLIT` 已经切开了；再切 2-gram 会造出 `th`/`he` 这类无意义片段 |

    ### 排序：**按词频降序，同频按首次出现位置**

    `keywords && query_keywords` 是**集合运算**，数组顺序不影响匹配 ——
    但顺序决定**截断时保留谁**（`MAX_KEYWORDS_PER_CHUNK`）。
    按词频降序保留的是"这块反复提到的词"，即**主题词**；
    按出现顺序保留的是"块开头的词"，而开头往往是过渡句。

    ### 为什么不用 `set` 去重

    `set` 的迭代顺序随哈希种子变化（PYTHONHASHSEED），
    于是同一份文本在不同进程里派生出**顺序不同的关键词数组** ——
    而它会落库。重切块时 `text_hash` 相同但 `keywords` 不同，
    会让"重切后内容变没变"的判定出现假阳性。
    因此用 dict（保序）统计，排序键是**显式的**（频次 + 首次位置）。

    ### 停用词与长度过滤

    · 停用词在 2-gram 之前过滤（对整句候选判停用词 ——
      2-gram 里"的X"这类片段本来就会被长度/停用词规则处理）；
    · 长度过滤只对**拉丁**候选生效（见 `_MAX_KEYWORD_CHARS` 的说明）。
    """
    if not text:
        return ()
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    position = 0
    for raw in _KEYWORD_SPLIT.split(text):
        token = raw.strip()
        if not token:
            continue
        if token.lower() in _STOPWORDS:
            continue
        candidates = _candidate_keywords(token)
        for candidate in candidates:
            if candidate not in counts:
                counts[candidate] = 0
                first_seen[candidate] = position
                position += 1
            counts[candidate] += 1
    if not counts:
        return ()
    ordered = sorted(
        counts,
        key=lambda word: (-counts[word], first_seen[word]),
    )
    return tuple(ordered[:MAX_KEYWORDS_PER_CHUNK])


def _candidate_keywords(token: str) -> list[str]:
    """把一个候选串展开成关键词列表（见 `derive_keywords` 的两条路径）。"""
    if _CJK_RE.search(token):
        # CJK：切 2-gram。**同时保留原串**（若长度合适）——
        # 原串在"查询与块用同一个词"时匹配得更精确
        # （例如查询"除湿机静音"整串出现时，原串命中比 2-gram 命中更相关）。
        grams: list[str] = []
        if _MIN_KEYWORD_CHARS <= len(token) <= _MAX_KEYWORD_CHARS:
            grams.append(token)
        if len(token) >= _CJK_NGRAM:
            for index in range(len(token) - _CJK_NGRAM + 1):
                grams.append(token[index : index + _CJK_NGRAM])
        else:
            grams.append(token)
        return grams
    # 拉丁/数字：原样（小写化 —— 查询侧也会小写化，两侧对齐）。
    lowered = token.lower()
    if _MIN_KEYWORD_CHARS <= len(lowered) <= _MAX_KEYWORD_CHARS:
        return [lowered]
    return []


def _split_paragraphs(text: str) -> list[str]:
    """按空行切段落，丢掉空段落。"""
    return [p.strip() for p in _PARAGRAPH_SPLIT.split(text) if p.strip()]


def _hard_split(paragraph: str, limit: int) -> list[str]:
    """硬切一个超长段落（它自己就超过 `limit`）。

    优先落在**句末**（`_SENTENCE_SPLIT`），其次落在**字符上限**。

    **为什么优先句末**：一个 2000 字的段落如果按 500 字硬切，
    切点落在句中的概率很高。句末切分让"块以完整句子结尾"，
    这是在不引入模型的前提下能做到的最好的语义尊重。

    **保证进展**：若某个"句子"自己就超过 `limit`（例如一段没有标点的
    长串），退化为按 `limit` 硬切。**必须有这个兜底** —— 否则
    内层循环会切不出东西而死循环。
    """
    sentences = [s for s in _SENTENCE_SPLIT.split(paragraph) if s]
    pieces: list[str] = []
    buffer = ""
    for sentence in sentences:
        if len(sentence) > limit:
            # 句子自己超限：先冲掉 buffer，再把这句话按 limit 硬切。
            if buffer:
                pieces.append(buffer)
                buffer = ""
            for start in range(0, len(sentence), limit):
                pieces.append(sentence[start : start + limit])
            continue
        if len(buffer) + len(sentence) <= limit:
            buffer += sentence
        else:
            if buffer:
                pieces.append(buffer)
            buffer = sentence
    if buffer:
        pieces.append(buffer)
    return pieces


def _apply_overlap(pieces: list[str], overlap: int, limit: int) -> list[str]:
    """给相邻块加重叠：每块前面接上**前一块的尾部** `overlap` 个字符。

    **为什么是"前一块的尾部"而不是"后一块的头部"**：两者在信息上等价
    （都是让跨切点的事实至少被一块完整覆盖），但尾部衔接让
    `locator.start` 仍然指向"这一块的主内容起点"，
    而不是被重叠前缀污染。本实现取前者。

    ⚠️ **重叠必须计入块长上限**：加了重叠前缀之后，块长变成
    `len(body) + overlap`，会**超过 `limit`**。而 `limit`（即
    `chunk_chars`）的意义是"不超过模型窗口"（见模块 docstring 第一节）——
    超了会被模型**静默截断**，于是块尾从未参与向量计算。

    因此本函数**从主内容里扣掉重叠长度**（`body[:limit - overlap]`），
    保证 `len(前缀) + len(主内容) == limit`。

    **这个扣减是实测发现的**：第一版没有扣，实测 8 个块的长度是
    `[100, 110, 110, ...]` —— 100 是 `chunk_chars`，110 是它加 10 字重叠。
    而"超上限"这件事**不报错**（模型静默截断），
    只有打印块长才看得见。这正是本项目"计数类判据要用显式字段报告口径"
    （ISS-51）的同族：**一个数看起来对，是因为没人去量它**。

    **`limit` 由调用方显式传入，不从 `pieces` 反推**：
    反推（`max(len(p) for p in pieces)`）在"所有段落都很短"时会得出
    一个偏小的值，于是 `limit - overlap` 可能小于块长 ——
    结果是**切掉内容**。而"切掉内容"不报错、块数不变、
    只有块尾少了几个字。权威是 `chunk_chars`，不是已切出的块。

    `overlap <= 0` 时原样返回。
    """
    if overlap <= 0 or len(pieces) <= 1:
        return pieces
    result = [pieces[0]]
    for index in range(1, len(pieces)):
        previous_tail = pieces[index - 1][-overlap:]
        # `max(..., 1)` 保证主内容不为空 —— 否则块会退化成"只有重叠前缀"，
        # 而 `text_content_not_empty` 的 CHECK 会拦住空串
        # （虽然此处不会产生空串，但保证"主内容存在"让不变量更明确）。
        body = pieces[index][: max(limit - len(previous_tail), 1)]
        result.append(previous_tail + body)
    return result


def chunk_text(
    text: str,
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS,
) -> tuple[ChunkDraft, ...]:
    """把正文切成块。**纯函数**（同输入必同输出）。

    `chunk_no` **从 1 开始**（与 WP20 的 `MIN_CHUNK_NO = 1` 对齐），
    且**连续无洞** —— 因为 WP20 的 `UQ(knowledge_version_id, chunk_no)`
    只保证唯一，不保证连续，而"连续"是重切时判断
    "这块是新增的还是替换的"的可用信号。

    **空文本返回空元组**（不是"一个空块"）：WP20 的
    `text_content_not_empty` CHECK 拒绝空串，返回空块会直接撞约束。
    调用方（`embedding_service`）对空元组有显式分支 ——
    "这条知识没有可切的内容"是一个**需要被看到的事实**，不是错误。
    """
    if chunk_chars < MIN_CHUNK_CHARS or chunk_chars > MAX_CHUNK_CHARS:
        raise ValueError(
            f"chunk_chars 必须在 [{MIN_CHUNK_CHARS}, {MAX_CHUNK_CHARS}] 内，"
            f"收到 {chunk_chars}"
        )
    if overlap_chars < 0:
        raise ValueError(f"overlap_chars 不能为负，收到 {overlap_chars}")
    if overlap_chars >= chunk_chars:
        # 重叠 >= 块长会让每块都含上一块的全部内容 → 块之间无限相似，
        # 检索退化成"同一段返回 N 次"。
        raise ValueError(
            f"overlap_chars({overlap_chars}) 必须小于 chunk_chars({chunk_chars})"
        )

    normalized = normalize_text(text)
    if not normalized:
        return ()

    # 1) 段落聚合：把段落装进 ≤ chunk_chars 的桶。
    paragraphs = _split_paragraphs(normalized)
    buckets: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        if len(paragraph) > chunk_chars:
            if buffer:
                buckets.append(buffer)
                buffer = ""
            buckets.extend(_hard_split(paragraph, chunk_chars))
            continue
        if not buffer:
            buffer = paragraph
        elif len(buffer) + 1 + len(paragraph) <= chunk_chars:
            buffer += "\n" + paragraph
        else:
            buckets.append(buffer)
            buffer = paragraph
    if buffer:
        buckets.append(buffer)

    # 2) 重叠。`chunk_chars` 显式传入 —— 它是上限的权威
    #    （见 `_apply_overlap` 的说明：反推会切掉内容）。
    buckets = _apply_overlap(buckets, overlap_chars, chunk_chars)

    # 3) 组装。字符偏移按**规范化后**的文本算（与哈希同源）。
    #
    #    `cursor` 是"已产出的块覆盖到原文的哪个位置"。重叠块的前
    #    `overlap_chars` 个字符是上一块的尾部（**已经算过一遍**），
    #    因此它的 `start` 要从 `cursor` 回退 `overlap_chars` ——
    #    这样 `[start, end)` 描述的仍是"这一块覆盖的原文区间"，
    #    而不是"新增的那部分"。
    #
    #    **回退后用 `max(..., 0)` 夹住**：只有第一个块没有重叠，
    #    但若 `overlap_chars` 大于第一块的尾部长度（`_apply_overlap`
    #    在 `len(pieces) <= 1` 时直接返回原列表，所以第一块永远没有
    #    重叠前缀），这个夹取是防御性的 —— 它保证 `start >= 0`，
    #    不会产生一个负的字符偏移。
    drafts: list[ChunkDraft] = []
    cursor = 0
    for index, body in enumerate(buckets):
        start = max(cursor - (overlap_chars if index else 0), 0)
        end = start + len(body)
        cursor = end
        drafts.append(
            ChunkDraft(
                chunk_no=index + 1,
                text_content=body,
                text_hash=hash_chunk_text(body),
                token_count=count_tokens(body),
                locator={
                    "start": start,
                    "end": end,
                    "unit": UNIT_CHAR,
                    "chunkerVersion": CHUNKER_VERSION,
                },
                keywords=derive_keywords(body),
            )
        )
    return tuple(drafts)


def chunk_structured(
    payload: dict[str, object],
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
) -> tuple[ChunkDraft, ...]:
    """把 `structured_payload` 的**字段**各自成块。

    WP20 的硬约束是「**禁止无结构散文替代事实字段**」——
    `structured_payload` 里每个字段都是一条**可被程序消费的事实**。
    因此结构化字段的切块不是"按长度切"，而是**一个字段一块**：
    切碎一个字段会让"这个数字适用于哪个型号"永远问不出来
    （正是 WP20 那句备注要防的事）。

    **超长字段才按 `chunk_text` 硬切**，且切出的块 `unit` 仍是 `FIELD`
    但带 `path` —— 于是"这一块来自哪个字段"是可查的。

    `locator.unit = "FIELD"` 且带 `path`（见 `ChunkDraft` 的说明）：
    结构化字段**没有字符位置**，硬塞一个偏移量是编造一个事实。
    """
    drafts: list[ChunkDraft] = []
    for path in sorted(payload):
        value = payload[path]
        text = _render_field_value(value)
        if not text:
            continue
        if len(text) <= chunk_chars:
            pieces = [text]
        else:
            pieces = [d.text_content for d in chunk_text(text, chunk_chars=chunk_chars)]
        for piece in pieces:
            drafts.append(
                ChunkDraft(
                    chunk_no=len(drafts) + 1,
                    text_content=piece,
                    text_hash=hash_chunk_text(piece),
                    token_count=count_tokens(piece),
                    locator={
                        "path": path,
                        "unit": UNIT_FIELD,
                        "chunkerVersion": CHUNKER_VERSION,
                    },
                    keywords=derive_keywords(piece),
                )
            )
    return tuple(drafts)


def _render_field_value(value: object) -> str:
    """把一个结构化字段值渲染成可切块的文本。

    · `str`   → 原样（规范化）
    · `bool`  → `"true"` / `"false"`（**不用 Python 的 `True`** ——
      下游是模型与检索，不是 Python 解释器）
    · `None`  → 空串（该字段不进块）
    · 标量    → `str(value)`
    · `list`  → 逐元素渲染后用 `"、"` 连接（中文枚举的习惯分隔符）
    · `dict`  → 递归成 `"key: value"` 行（**保 key 顺序**：
      结构化字段的 key 顺序是作者给的，排序会丢掉"哪个字段更重要"的线索）

    **`dict` 递归而不是 `json.dumps`**：`json.dumps` 会产生
    `{"a": 1}` 这样的花括号与引号，它们在文本 embedding 里是噪声；
    而且它会把 key 按字典序排（`sort_keys` 默认 False，但 dict 本身
    在 Python 3.7+ 保序，所以其实不排 —— 但花括号噪声是确定的）。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        parts = [_render_field_value(item) for item in value]
        return "、".join(p for p in parts if p)
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            rendered = _render_field_value(item)
            if rendered:
                lines.append(f"{key}: {rendered}")
        return "\n".join(lines)
    return str(value)


def chunk_for_version(
    *,
    body: str,
    structured_payload: dict[str, object],
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS,
) -> tuple[ChunkDraft, ...]:
    """一条知识版本的完整切块：**正文块在前，结构化字段块在后**。

    **顺序有意义且必须稳定**：`chunk_no` 是 `UQ(version_id, chunk_no)` 的
    一半，重切时若顺序不稳定，`chunk_no` 会全变 —— 于是"哪些块是新增的"
    无法判断。因此顺序规则是显式的：先正文（按字符偏移升序），
    再结构化字段（按字段名**升序**）。

    **`body` 为空时结构化字段块仍然产出**：一条只填了结构化字段的
    知识（例如"价格：199 元"）是合法的 —— WP20 只要求
    `structured_payload` 非空，不要求 `body` 非空。
    """
    drafts = list(
        chunk_text(body, chunk_chars=chunk_chars, overlap_chars=overlap_chars)
    )
    structured = chunk_structured(structured_payload, chunk_chars=chunk_chars)
    for draft in structured:
        drafts.append(
            ChunkDraft(
                chunk_no=len(drafts) + 1,
                text_content=draft.text_content,
                text_hash=draft.text_hash,
                token_count=draft.token_count,
                locator=draft.locator,
                keywords=draft.keywords,
            )
        )
    return tuple(drafts)


__all__ = [
    "CHUNKER_VERSION",
    "MAX_KEYWORDS_PER_CHUNK",
    "UNIT_CHAR",
    "UNIT_FIELD",
    "chunk_for_version",
    "chunk_structured",
    "chunk_text",
    "count_tokens",
    "derive_keywords",
    "hash_chunk_text",
    "normalize_text",
]
