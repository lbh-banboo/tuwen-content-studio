"""WP19 研究关键词与提示词的受控取值、不可变值类型与常量。

**本包一张表都不建**（划界判据见说明文档 §1.7）。因此本模块是
"取值与类型的唯一来源"，而不是"表约束的来源" —— 它提供：

| 内容 | 谁用 |
|---|---|
| 缺口状态（`COVERED` / `MISSING` / `NOT_APPLICABLE`） | `keywords.derive_gaps`、`schema`、`api` |
| 关键词来源种类（`ENTITY` / `FIELD` / `CLAIM` / `PATTERN`） | `keywords`、`schema` |
| 提示词任务类别（`FACT_VERIFICATION` / `EVIDENCE_COLLECTION`） | `keywords`、`schema` |
| 建议返回格式的介质（`TEXT` / `IMAGE` / `DOCUMENT` / `URL` / `ANY`） | `keywords`、`schema` |
| 上限常量 | `keywords`（截断点）、`repository`（扫描 limit） |
| 输入/输出的 frozen dataclass | `keywords`（纯函数签名）、`service`（组装） |

**为什么输入类型也是 frozen dataclass 而不是直接用 ORM 对象**：
`keywords.py` 是本包的核心（验收全靠它），它必须是**零 I/O 纯函数**，
才能用构造出来的数据穷举测试。若签名收 `BriefClaim`（ORM 实例），
纯函数就与 session 生命周期绑定了 —— 本项目的 ORM 实例离开 session 即
`DetachedInstanceError`（WP18 已踩）。因此 `service` 层做一次
`BriefClaim` → `ClaimInput` 的转换，纯函数只认 dataclass。

**为什么没有 `*_LIST_SQL` 常量**：本包无表、无 CHECK，那些常量在别处
是为 `CHECK (x IN (...))` 拼 SQL 用的。留着会让人以为"数据库层已经管住了"
（WP18 的 `FIELD_STATUS_LIST_SQL` 刻意不提供，同一条判据）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Final

from rednote.modules.parsing.types import (
    PARSE_STATE_PARTIAL,
    PARSE_STATE_SUCCEEDED,
)

# ---------------------------------------------------------------------------
# 缺口状态（gaps[].state）
# ---------------------------------------------------------------------------
# 规格源（需求 / 数据结构 02 / 协议 03）都**没有给这个取值域** ——
# 「覆盖缺口确认」只有一个词，没有形状。本包自行定义并登记（见说明文档 §1.4）。
#
# 判据是"这条缺口现在能不能往下走"：
#
#   COVERED         已有可追溯的来源 → 步骤 2 可以推进
#   MISSING         还缺证据 → 阻断"确认完整"
#   NOT_APPLICABLE  运营已明确标记"这条对本次生产无关" → 不阻断
#
# **为什么不是布尔 `covered: bool`**：`NOT_APPLICABLE` 与 `MISSING`
# 都是"没有证据"，但处置方式相反 —— 布尔会把"已判定不适用"折叠成
# "还没做"，于是运营每看一眼清单都会被那几条永远收不到的项干扰。
# 需求 §207 原文（"确实不适用的字段可由运营明确标记'不适用'并填写原因"）
# 正是要求这两者分开。

GAP_COVERED: Final[str] = "COVERED"
GAP_MISSING: Final[str] = "MISSING"
GAP_NOT_APPLICABLE: Final[str] = "NOT_APPLICABLE"

GAP_STATES: Final[tuple[str, ...]] = (
    GAP_COVERED,
    GAP_MISSING,
    GAP_NOT_APPLICABLE,
)

#: 阻断"步骤 2 确认完整"的状态（唯一的那个）。抽成集合而不是散写
#: `if state == GAP_MISSING`：将来若新增状态（例如 `STALE`），
#: "它阻不阻断"是一个必须显式回答的问题。
GAP_BLOCKING_STATES: Final[frozenset[str]] = frozenset({GAP_MISSING})

# ---------------------------------------------------------------------------
# 可用证据的解析状态
# ---------------------------------------------------------------------------
#: 哪些解析状态产出的片段**可以被当作研究证据**。
#:
#: `SUCCEEDED` 显然是；`PARTIAL` **也是** —— 它解出了一部分内容，
#: 而那部分是真的（WP17 明确 `PARTIAL` 的 `parsed_units > 0`）。
#: 把它排除会让"扫描件质量差但确实抽到了关键页"的文件被算作
#: 无证据，运营会以为白传了。
#:
#: `FAILED` / `CANCELLED` 不在其中：它们**零片段**（`parsed_units = 0`），
#: 用它当证据等于把"没读到"当成"读到了没有"。
#:
#: **这个集合必须只有一份**：`service.evidence_usable`（Python 侧判据）
#: 与 `repository.quota_totals`（SQL 侧全量计数）都要用它。
#: 两处各写一份 `('SUCCEEDED', 'PARTIAL')` 的话，将来新增一个
#: "部分可用"的状态时必然只改一处，而两处的偏差会表现为
#: "列表说这个文件可用、配额说不可用"—— 一个没人会想到去查的分歧。
EVIDENCE_USABLE_STATES: Final[frozenset[str]] = frozenset(
    {PARSE_STATE_SUCCEEDED, PARSE_STATE_PARTIAL}
)

# ---------------------------------------------------------------------------
# 关键词来源种类（keywords[].sourceKind）
# ---------------------------------------------------------------------------
# 本包的最高风险是 R1（关键词质量低 → 包变成摆设）。应对不是"把词造得更聪明"
# （那需要 NLP，且无法解释），而是**让每个词都回答"我从哪来"**：
#
#   ENTITY   产品的名/品牌/型号 —— 词的主体，来自 Brief 的实体字段
#   FIELD    结构化字段的取值（目标人群、购买场景、痛点）—— 可直接当检索词用
#   CLAIM    某条 Brief 结论 —— 词 = 主体 + 该结论的短语
#   PATTERN  平台通用检索模式（"测评 / 口碑 / 对比"等后缀）——
#            **这一类的可解释性是"它来自平台惯例，不是你的 Brief"**，
#            必须显式标出来，否则运营会以为"系统从我的 Brief 里推出了'测评'"
#
# `PATTERN` 单独一类是刻意的：它是本包**唯一**不来自 Brief 的词源。
# 把它混进 `ENTITY` 会让可解释性变成一句空话。

KEYWORD_SOURCE_ENTITY: Final[str] = "ENTITY"
KEYWORD_SOURCE_FIELD: Final[str] = "FIELD"
KEYWORD_SOURCE_CLAIM: Final[str] = "CLAIM"
KEYWORD_SOURCE_PATTERN: Final[str] = "PATTERN"

KEYWORD_SOURCE_KINDS: Final[tuple[str, ...]] = (
    KEYWORD_SOURCE_ENTITY,
    KEYWORD_SOURCE_FIELD,
    KEYWORD_SOURCE_CLAIM,
    KEYWORD_SOURCE_PATTERN,
)

#: 来源种类出现在 `keywords[]` 里的稳定排序顺序。
#: **排序必须确定**：关键词列表每次刷新都换顺序，是最难查的一类用户反馈
#: （WP17 的 ISS-52 同型问题）。
KEYWORD_SOURCE_ORDER: Final[dict[str, int]] = {
    name: index for index, name in enumerate(KEYWORD_SOURCE_KINDS)
}

# ---------------------------------------------------------------------------
# 提示词任务类别（prompts[].taskKind）
# ---------------------------------------------------------------------------
# 协议 03 §5 原文只说"推荐关键词、**可复制提示词与建议返回格式**"，
# 没有说明提示词有几种、按什么分类。本包只定义**两种**，判据是
# "这条提示词要拿回什么形状的东西"：
#
#   FACT_VERIFICATION     验证一条可判真假的结论（"续航 18 小时"是不是真的）
#                             → 要的是**来源**
#   EVIDENCE_COLLECTION   把已搜到的东西整理成可提交的证据清单
#                             → 要的是**清单**（不引入新事实）
#
# **为什么不按"竞品对比 / 成分 / 场景"分类**：那需要一个"内容主题体系"，
# 而它只能来自真实的运营话术 —— 现在没有。**编一套主题分类比不分类更危险**：
# 分类错了会让运营以为"系统懂我的业务"（同 WP20 拒绝"覆盖率评分"的判据）。

TASK_FACT_VERIFICATION: Final[str] = "FACT_VERIFICATION"
TASK_EVIDENCE_COLLECTION: Final[str] = "EVIDENCE_COLLECTION"

TASK_KINDS: Final[tuple[str, ...]] = (
    TASK_FACT_VERIFICATION,
    TASK_EVIDENCE_COLLECTION,
)

# ---------------------------------------------------------------------------
# 建议返回格式的介质（prompts[].formatHint.media）
# ---------------------------------------------------------------------------
# 协议原文的"建议返回格式"没有形状，本包定义为结构化对象（说明文档 §1.4 第 8 条）。
# `media` 回答的是"该拿回什么东西"，它决定运营在外部平台该做什么动作：
#
#   IMAGE      截图（小红书笔记、商品页、评论区）
#   DOCUMENT   可下载的文档 / 报告（第三方检测、论文）
#   URL        只需要链接（公开页面，系统会解析网页）
#   TEXT       一段可直接复制的文字
#   ANY        不限定（"能找到什么算什么"）
#
# **不给更多值**（如 `VIDEO`）：多一个取值就多一条前端分支，
# 而当前没有任何判据能区分"该要视频"与"该要图片"。

FORMAT_MEDIA_TEXT: Final[str] = "TEXT"
FORMAT_MEDIA_IMAGE: Final[str] = "IMAGE"
FORMAT_MEDIA_DOCUMENT: Final[str] = "DOCUMENT"
FORMAT_MEDIA_URL: Final[str] = "URL"
FORMAT_MEDIA_ANY: Final[str] = "ANY"

FORMAT_MEDIA_KINDS: Final[tuple[str, ...]] = (
    FORMAT_MEDIA_TEXT,
    FORMAT_MEDIA_IMAGE,
    FORMAT_MEDIA_DOCUMENT,
    FORMAT_MEDIA_URL,
    FORMAT_MEDIA_ANY,
)

# ---------------------------------------------------------------------------
# 派生规则版本
# ---------------------------------------------------------------------------
# 关键词与缺口**不落库**（本包的核心划界）。这意味着它们的正确性完全建立在
# "可由输入重算"这一点上。而"重算"要成立，必须能回答"用哪一版规则算的" ——
# 因此响应里带 `derivation.rulesVersion`，规则改动时递增。
#
# **它是本包的"表结构版本"**：别的包用迁移版本（`*_schema_version` 列）
# 让旧数据可被识别，本包用这个整数让旧派生结果可被识别。

DERIVATION_RULES_VERSION: Final[int] = 1

#: 派生结果里"来源"字段的形状版本（与 `rulesVersion` 分开）：
#: 前者管"算法变了"，后者管"输出的 JSON 形状变了"。
#: 分开是因为前端要据后者决定解析方式，据前者决定要不要提示"重新生成"。
DERIVATION_PAYLOAD_VERSION: Final[int] = 1

# ---------------------------------------------------------------------------
# 上限常量（全部是"截断点"，不是"业务规则"）
# ---------------------------------------------------------------------------
# 本包对输入规模没有控制权：`MAX_CLAIMS_PER_VERSION = 500`（WP18），
# 而每个 claim 可以产生多个关键词。不设上限时，一个跑偏的解构结果
# 会让响应体失控（同 WP18 的 `_enforce_claim_limit` 判据）。
#
# **截断必须可见**：`build_research_prompts` 的返回里带 `truncated` 标记
# （不是静默丢）。静默截断会让运营以为"就这么多"。

#: `keywords[]` 上限。60 是一个"一屏能扫完"的数量 ——
#: 再多运营就不会逐个看了，等于没给。
MAX_KEYWORDS: Final[int] = 60

#: `gaps[]` 上限。与 WP18 的 `MAX_CLAIMS_PER_VERSION` 同值：
#: 缺口来自 claims，因此它的天然上限就是 claims 的上限。
#: **不在本包更严地截断**：漏掉一条缺口意味着运营以为收够了。
MAX_GAPS: Final[int] = 500

#: `prompts[]` 上限。一条缺口一条提示词 + 一条总览。
#: 截断到 20 是因为"20 条提示词"已经远超一次研究会逐个执行的量。
MAX_PROMPTS: Final[int] = 20

#: 每条缺口给出的建议关键词数
MAX_SUGGESTED_KEYWORDS_PER_GAP: Final[int] = 4

#: 关键词长度上限（中文按字符计）。超长的"关键词"在外部平台搜不出东西 ——
#: 它是"短语被误当成词"的信号，截断比保留更有用。
KEYWORD_MAX_CHARS: Final[int] = 60

#: 从 claim 文本里取短语的长度上限
MAX_PHRASE_CHARS: Final[int] = 24

#: 从结构化字段（多值字段）里取值的条数上限
MAX_FIELD_VALUES_PER_PATH: Final[int] = 2

#: `research-files` 一次扫描的行数上限。
#:
#: **这是 R2（数据量依赖）的应对**，本项目已踩三次（ISS-49 / ISS-59 / ISS-73）。
#: 列表本身按 `project_id` 过滤（不是全局列表），但一个项目的
#: RESEARCH 文件在配额内仍可能上千（300MB / 小图）。默认 limit 是为
#: **生产吞吐**选的，不是为**测试可见性**选的（ISS-73 的教训）——
#: 因此 repository 的扫描上限显式写大，端点分页在内存里做。
RESEARCH_FILE_SCAN_LIMIT: Final[int] = 1_000

# ---------------------------------------------------------------------------
# 输入类型（service → 纯函数）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimInput:
    """一条 Brief 结论的纯数据表示。

    **为什么 `has_evidence` 是一个 bool 而不是裸的 segment_id / url**：
    纯函数只关心"这条结论有没有可追溯的来源"（缺口的判据），
    不需要知道来源的具体形态。把 segment_id 传进来会让纯函数有能力
    去校验"那个片段是否存在"—— 那是 service 的职责，
    而一个能查库的"纯函数"就不再是纯函数了。
    """

    claim_id: uuid.UUID
    field_path: str
    #: 字段的中文标签（由 service 从 WP18 的 BriefSchema 解析后带进来）。
    #: **不在本模块解析 label**：`brief.schema.field_spec` 对未知路径**抛错**，
    #: 而历史 claim 的 `field_path` 可能是已废弃的路径。把一个会抛异常的
    #: 依赖放进纯函数，会让"纯函数可穷举测试"这个前提作废。
    field_label: str
    text: str
    category: str
    resolution: str
    has_evidence: bool


@dataclass(frozen=True)
class FieldValueInput:
    """一个结构化字段的取值（可作检索词的）。

    只带**已经有内容**的字段：`MISSING` / `NOT_APPLICABLE` 的字段不产生关键词
    （对空值造词会让运营去搜一个 Brief 里根本没提的东西）。
    `NOT_APPLICABLE` 的字段尤其不能造词 —— 它带的是一个**不适用原因**
    （WP18 的 `_read_entry` 把原因放在 value 位置），把它当检索词会搜出
    一堆完全不相关的结果。
    """

    path: str
    label: str
    values: tuple[str, ...]


# ---------------------------------------------------------------------------
# 输出类型（纯函数 → service）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Keyword:
    """一个推荐关键词。

    `terms` 是"这个词由哪些部分组成"（例如
    `("某产品", "续航")`）—— 可解释性的第二层。前端可以把
    `sourceLabel` 当标题、`terms` 当"拼装明细"展示。
    """

    text: str
    source_kind: str
    terms: tuple[str, ...]
    source_field_path: str | None = None
    source_field_label: str | None = None
    related_claim_id: uuid.UUID | None = None


@dataclass(frozen=True)
class FormatHint:
    """建议返回格式（协议 03 §5 原文的那个词）。"""

    media: str
    fields: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class Prompt:
    """一条可直接复制的提示词。"""

    task_kind: str
    title: str
    text: str
    format_hint: FormatHint
    related_field_paths: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Gap:
    """一条覆盖缺口。

    `note` 承载"这条缺口为什么是这个状态"的补充说明。它**只在
    状态与预期不符时非空** —— 例如一条 `CONFIRMED` 的
    `EXTERNAL_EVIDENCE` 却没有来源（数据回填绕过了 WP18 的构造器），
    此时状态被降级为 `MISSING`，`note` 记下原因。
    """

    claim_id: uuid.UUID
    field_path: str
    field_label: str
    claim_text: str
    category: str
    resolution: str
    state: str
    suggested_keywords: tuple[str, ...] = ()
    note: str | None = None

    @property
    def blocking(self) -> bool:
        """这条缺口是否阻断"步骤 2 确认完整"。"""
        return self.state in GAP_BLOCKING_STATES


@dataclass(frozen=True)
class Derivation:
    """派生过程的元信息。

    **为什么必须随响应返回**：关键词与缺口不落库，因此"这批结果是什么时候、
    按哪一版规则算的"没有任何别的地方可查。没有它，前端无法回答
    "我上次看到的那 3 条缺口去哪了"（规则变过之后结果本就该变）。
    """

    rules_version: int
    payload_version: int
    brief_version_id: uuid.UUID | None
    claim_count: int
    truncated: bool
    notes: tuple[str, ...] = field(default_factory=tuple)


__all__ = [
    "DERIVATION_PAYLOAD_VERSION",
    "DERIVATION_RULES_VERSION",
    "EVIDENCE_USABLE_STATES",
    "FORMAT_MEDIA_ANY",
    "FORMAT_MEDIA_DOCUMENT",
    "FORMAT_MEDIA_IMAGE",
    "FORMAT_MEDIA_KINDS",
    "FORMAT_MEDIA_TEXT",
    "FORMAT_MEDIA_URL",
    "GAP_BLOCKING_STATES",
    "GAP_COVERED",
    "GAP_MISSING",
    "GAP_NOT_APPLICABLE",
    "GAP_STATES",
    "KEYWORD_MAX_CHARS",
    "KEYWORD_SOURCE_CLAIM",
    "KEYWORD_SOURCE_ENTITY",
    "KEYWORD_SOURCE_FIELD",
    "KEYWORD_SOURCE_KINDS",
    "KEYWORD_SOURCE_ORDER",
    "KEYWORD_SOURCE_PATTERN",
    "MAX_FIELD_VALUES_PER_PATH",
    "MAX_GAPS",
    "MAX_KEYWORDS",
    "MAX_PHRASE_CHARS",
    "MAX_PROMPTS",
    "MAX_SUGGESTED_KEYWORDS_PER_GAP",
    "RESEARCH_FILE_SCAN_LIMIT",
    "TASK_EVIDENCE_COLLECTION",
    "TASK_FACT_VERIFICATION",
    "TASK_KINDS",
    "ClaimInput",
    "Derivation",
    "FieldValueInput",
    "FormatHint",
    "Gap",
    "Keyword",
    "Prompt",
]