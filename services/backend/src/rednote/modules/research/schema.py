"""WP19 的响应 DTO（协议 03 §5 的两个 GET 端点）。

**这一层的存在理由**（与本项目其余包一致）：`response_model` 不只是文档，
它是 FastAPI 的**输出过滤器** —— 声明模型里没有的键会被静默剥掉。
WP10 实测过（`PaginatedEnvelope` 那一节）：服务端拼了 `page`，
响应体里只剩 `data`，前端永远拿不到 `nextCursor`，而 HTTP 200、
日志无异常。因此每个端点的响应形状必须**显式声明**。

---

## 一、两类不同的 `data` 形状

| 端点 | `data` | 为什么 |

|---|---|---|
| `GET /research-prompts` | **对象**（`ResearchPromptsView`） | 它返回三个互相依赖的块（关键词 / 提示词 / 缺口），拆成三个端点会让前端发三次请求、且三者可能来自**不同时刻的 Brief 版本** |
| `GET /research-files` | **列表**（`PaginatedEnvelope`） | 协议 §5 原文给了 `cursor,limit` → 它是分页列表。**必须用 `PaginatedEnvelope`**，否则 `page` 被剥掉 |

## 二、容量与计数类字段一律十进制字符串

协议 §1："所有数据库 bigint 对外固定为十进制字符串。不要根据数值大小
在字符串和数字间切换。" 配额三个字段（`limitBytes` / `effectiveBytes` /
`reservedBytes`）照此办理 —— 与 WP12 的 `remainingQuotaBytes` 同口径。

**但 `segmentCount` 是 int**：它不是容量（不参与任何加法），
而是"解析出了几段"。把它也做成字符串会让前端为了 `.length` 之外
的用途反复 parse。判据是"它会不会被累加" —— 会 → 字符串；不会 → int。

## 三、`gaps[]` 必须自带前端渲染所需的全部字段

说明文档 §1.7 的结论：前端本次不做（全仓 0 前端代码），
但**接口形状要为它设计**。具体地，`GapView` 带 `fieldPath` + `fieldLabel`
+ `claimText` + `state` + `suggestedKeywords[]` ——
这五项齐了，前端才能渲染出"哪条缺口 / 缺的是 Brief 的哪个字段 /
缺口原文 / 现在什么状态 / 该搜什么词"这一整张卡片，
而不必再发一次请求或自己拼。
"""

from __future__ import annotations

import uuid
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from rednote.platform.errors import Meta
from rednote.platform.pagination import PageInfo

T = TypeVar("T")

# ---------------------------------------------------------------------------
# GET /projects/{projectId}/research-prompts
# ---------------------------------------------------------------------------


class KeywordView(BaseModel):
    """一个推荐关键词（`keywords[].`）。"""

    model_config = ConfigDict(populate_by_name=True)

    text: str
    source_kind: str = Field(alias="sourceKind")
    #: 这个词由哪些部分组成。前端可展示成"拼装明细"，
    #: 让"为什么推荐这个词"可核对（R1 的应对）。
    terms: list[str]
    source_field_path: str | None = Field(default=None, alias="sourceFieldPath")
    source_field_label: str | None = Field(default=None, alias="sourceFieldLabel")
    #: 这个词是为哪条结论生成的（`sourceKind='CLAIM'` 时非空）。
    related_claim_id: uuid.UUID | None = Field(default=None, alias="relatedClaimId")


class FormatHintView(BaseModel):
    """建议返回格式（协议 §5 原文的那个词，本包定义了它的形状）。"""

    model_config = ConfigDict(populate_by_name=True)

    media: str
    #: 要拿回的字段名清单 —— 运营照着填，比"随便给我点资料"有效得多。
    fields: list[str]
    description: str


class PromptView(BaseModel):
    """一条可直接复制的提示词。

    `text` 是**完整可复制文本**（不是模板片段）：验收里"复制"这个动作
    指的是"把这段文字整段拿走"，因此服务端必须给成品。
    """

    model_config = ConfigDict(populate_by_name=True)

    task_kind: str = Field(alias="taskKind")
    title: str
    text: str
    format_hint: FormatHintView = Field(alias="formatHint")
    related_field_paths: list[str] = Field(alias="relatedFieldPaths")
    keywords: list[str]


class GapView(BaseModel):
    """一条覆盖缺口（`gaps[].`）。

    `blocking` 由服务端算（它是 `state` 的函数），前端不自己判断 ——
    "什么算阻断"必须与服务端下一步校验用的是同一个判据。
    """

    model_config = ConfigDict(populate_by_name=True)

    claim_id: uuid.UUID = Field(alias="claimId")
    field_path: str = Field(alias="fieldPath")
    field_label: str = Field(alias="fieldLabel")
    claim_text: str = Field(alias="claimText")
    category: str
    resolution: str
    state: str
    blocking: bool
    suggested_keywords: list[str] = Field(alias="suggestedKeywords")
    #: 只在"状态与预期不符"时非空（见 `keywords.gap_note`）。
    note: str | None


class CoverageSummaryView(BaseModel):
    """缺口的分类计数。

    `blockingCount` 单独给出来而不是让前端从 `gaps[]` 里数 ——
    见 `keywords.coverage_summary`。
    """

    model_config = ConfigDict(populate_by_name=True)

    total: int
    covered_count: int = Field(alias="coveredCount")
    missing_count: int = Field(alias="missingCount")
    not_applicable_count: int = Field(alias="notApplicableCount")
    blocking_count: int = Field(alias="blockingCount")


class DerivationView(BaseModel):
    """派生过程的元信息。

    **必须随响应返回**：关键词与缺口不落库，因此"这批结果按哪一版规则
    算的"没有别的地方可查。`stale` 的判据也在这里 —— 客户端手里那份
    结果的 `rulesVersion` 与当前值不同就该重新拉取。
    """

    model_config = ConfigDict(populate_by_name=True)

    rules_version: int = Field(alias="rulesVersion")
    payload_version: int = Field(alias="payloadVersion")
    brief_version_id: uuid.UUID | None = Field(alias="briefVersionId")
    revision_no: int | None = Field(alias="revisionNo")
    claim_count: int = Field(alias="claimCount")
    truncated: bool
    notes: list[str]


class ResearchFilesMeta(Meta):
    """`GET /research-files` 的 `meta` 块：协议 §1 的 `meta` **加上** `research`。

    ## 为什么必须新定义一个 meta 模型，而不是往 dict 里塞

    `PaginatedEnvelope[T].meta` 的类型是 `platform.errors.Meta`，而它
    **只声明了 `requestId` 一个字段**。`response_model` 是 FastAPI 的
    输出过滤器：声明模型里没有的键会被**静默剥掉** ——
    服务端明明拼了 `meta.research`，响应体里却没有它，
    而 HTTP 200、日志无异常。这正是 WP10 踩过的
    `PaginatedEnvelope` 那个坑的同一型（当时丢的是 `page`）。

    因此配额必须出现在**被声明的模型**里。继承 `Meta` 而不是另起一个
    平行模型：`requestId` 是协议 §1 对所有响应（含错误响应）的要求，
    继承保证它一定在，且类型与其余端点一致。

    ## 为什么配额放在 `meta` 而不是 `data`

    `data` 是**文件列表**（分页的，一次只有一页）。配额回答的是
    "这个项目整体用掉多少" —— 把它放进 `data` 会让它看起来是
    "这一页的"，而它随翻页变化就是错的（见 `service.list_research_files`
    的说明：它取的是全量窗口）。
    协议 §1 原文的 `meta` 是 `{...}`（开放块，不是只允许 `requestId`），
    因此放这里不违反协议。

    ## 为什么也不新开一个 `GET /research-quota` 端点

    配额与列表是**同一次决策**的两面（"我看到这些文件，还装得下多少"），
    拆成两个端点会让前端发两次请求，而两次请求之间文件可能被删 ——
    那时配额与列表不同步，运营看到的是一个从未同时成立的状态。
    """

    model_config = ConfigDict(populate_by_name=True)

    research: ResearchQuotaView


class ResearchFilesEnvelope(BaseModel, Generic[T]):
    """列表响应：`{"data": [...], "page": {...}, "meta": {...}}`（协议 §1）。

    **为什么不能用 `platform.pagination.PaginatedEnvelope[T]`**：
    它的 `meta` 类型是 `Meta`（只有 `requestId`）与 `PageInfo`
    （只有 `nextCursor` / `hasMore`）。本端点要在 `meta` 里带
    `research`（配额），而 `response_model` 会把没声明的键**静默剥掉** ——
    这正是 WP10 踩过的坑（当时丢的是 `page`，前端"永远只能看第一页"，
    而 HTTP 200、服务端无任何异常日志）。

    因此本包**复制那个形状并扩展 meta**。刻意不修改
    `platform.pagination.PaginatedEnvelope`：它被 7 个端点共用，
    给它加一个"可选的 research"字段会让那 7 个端点的契约里
    都出现一块空着的 `research`（协议 §13.3：契约是对外承诺，
    不该带某包专用的块）。

    `page` 的形状逐字段重复而不是复用 `PageInfo`：**保留复用**。
    这里显式写 `PageInfo` 类型 —— 它与 `paginate_items` 返回的是同一个类，
    复用不会漂移（`PageInfo` 本身没有"某包专用"的字段）。
    """

    model_config = ConfigDict(populate_by_name=True)

    data: list[T]
    page: PageInfo
    meta: ResearchFilesMeta


class ResearchPromptsView(BaseModel):
    """`GET /projects/{projectId}/research-prompts` 的 `data`。

    | 块 | 协议原文对应 |
    |---|---|
    | `keywords` | "推荐关键词" |
    | `prompts` | "可复制提示词" |
    | `prompts[].formatHint` | "建议返回格式" |
    | `gaps` | 「覆盖缺口确认」（工作包名，协议未给形状） |
    | `projectId` / `productName` | 上下文（前端标题栏用） |
    """

    model_config = ConfigDict(populate_by_name=True)

    project_id: uuid.UUID = Field(alias="projectId")
    product_name: str = Field(alias="productName")
    keywords: list[KeywordView]
    prompts: list[PromptView]
    gaps: list[GapView]
    summary: CoverageSummaryView
    derivation: DerivationView


# ---------------------------------------------------------------------------
# GET /projects/{projectId}/research-files
# ---------------------------------------------------------------------------


class ResearchParseView(BaseModel):
    """研究文件最近一次解析的摘要。

    **`state` 可以是 `NONE`**（文件列进来了但从未发起解析）——
    它不是 `parse_runs` 的取值，而是本层新增的一个取值。
    理由：`parse_runs` 的 `QUEUED` 表示"已排队"，而"从未解析过"
    与"已排队"对运营是两个不同的行动（后者等，前者要去点解析）。
    把"从未解析"输出成 `QUEUED` 会让运营一直等一个不存在的任务 ——
    这与 WP17 的 `latest_summary` 返回 `None` 是同一判据
    （那里用 `None`，这里用显式取值，因为协议要求 `status` 参数可筛）。
    """

    model_config = ConfigDict(populate_by_name=True)

    state: str
    complete: bool
    parse_run_id: uuid.UUID | None = Field(alias="parseRunId")
    parser_version: str | None = Field(alias="parserVersion")
    #: 解析出的片段数。**int 而不是字符串**（见模块 docstring 第二节）。
    segment_count: int = Field(alias="segmentCount")
    total_units: str | None = Field(alias="totalUnits")
    parsed_units: str | None = Field(alias="parsedUnits")
    errors: list[dict]


class ResearchFileView(BaseModel):
    """一个研究文件（`data[]`）。

    **`evidenceUsable` 是本层的核心字段**（R5 的落点）：
    它回答"这个文件的解析结果能不能当证据用"。

    | `parseState` | `evidenceUsable` | 理由 |
    |---|---|---|
    | `SUCCEEDED` | `True` | 完整解出来了 |
    | `PARTIAL` | `True` | **可以用，但必须标注** —— 缺的部分由 `parse.errors` 说明。判 `False` 会让运营白扔一半可用内容 |
    | `FAILED` / `CANCELLED` | `False` | 一个 unit 都没可用内容，把它算进覆盖等于虚报 |
    | `QUEUED` / `RUNNING` | `False` | 还没好。"正在解析"不能算已覆盖 |
    | `NONE` | `False` | 从未发起解析 |

    **`FAILED` 的明确排除是 AC-07 的延伸**：WP17 已经把"不支持格式"
    做成一个可查询的终态，本层据此把它从覆盖里剔除 ——
    否则"这个文件解析过、结论是不支持"会在研究页上看起来像一份有效证据。
    """

    model_config = ConfigDict(populate_by_name=True)

    file_id: uuid.UUID = Field(alias="fileId")
    original_name: str = Field(alias="originalName")
    mime_type: str = Field(alias="mimeType")
    byte_size: str = Field(alias="byteSize")
    #: 解析分类（协议 §4.2 的 `classification`）—— 前端据此提示
    #: "这个类型不在承诺可解析范围内"。
    classification: str
    scan_status: str = Field(alias="scanStatus")
    purpose: str
    #: 该文件是否仍计入研究配额（`project_file_links.effective`）。
    #: 解绑过的文件不会出现在列表里，但保留这个字段让前端能对未来
    #: 的"显示历史关联"选项直接复用（现在恒为 `True`）。
    effective: bool
    uploaded_by: uuid.UUID = Field(alias="uploadedBy")
    created_at: str = Field(alias="createdAt")
    parse: ResearchParseView
    evidence_usable: bool = Field(alias="evidenceUsable")


class ResearchQuotaView(BaseModel):
    """研究配额视图（**复用 WP12 的口径，不自己算**）。

    需求 §6.2 原文："每项目有效研究原文件累计不超过 300 MB，
    按 300,000,000 字节计算"，且"不包含 Brief、生成媒体和系统提取出的
    内部索引" —— 即只有 `purpose=RESEARCH` 计入（WP12 的
    `QUOTA_PURPOSES`）。

    三个字节数都是**十进制字符串**（协议 §1），与
    `platform/api_responses.UPLOAD_ERROR_RESPONSES` 的
    `limitBytes / usedBytes / remainingQuotaBytes` 同口径 ——
    运营在上传时撞到的 413 里会看到同一个数字。

    **`usedBytes` 与 `effectiveBytes` 是两个不同的数，都要给**：
    · `usedBytes` = 本列表实际列出的文件字节和（**按用途**；
      它是"研究资料"这个口径）；
    · `effectiveBytes` = `QuotaRepository.compute_effective_bytes`
      的重算结果（与写路径累加的口径一致，见文件模块 §13.1）。
    两者不一致就是"账面与明细不符"，必须让运营看得见 ——
    静默取一个会让差异永远不被发现。
    """

    model_config = ConfigDict(populate_by_name=True)

    limit_bytes: str = Field(alias="limitBytes")
    effective_bytes: str = Field(alias="effectiveBytes")
    reserved_bytes: str = Field(alias="reservedBytes")
    remaining_bytes: str = Field(alias="remainingBytes")
    #: 本页列出的文件字节和（十进制字符串）。
    listed_bytes: str = Field(alias="listedBytes")
    #: 本列表可见的文件数与可用证据数。
    file_count: int = Field(alias="fileCount")
    usable_evidence_count: int = Field(alias="usableEvidenceCount")
    #: 账面与明细是否一致（不一致时前端应提示，而不是假装没这回事）。
    reconciled: bool


__all__ = [
    "CoverageSummaryView",
    "DerivationView",
    "FormatHintView",
    "GapView",
    "KeywordView",
    "PromptView",
    "ResearchFileView",
    "ResearchFilesEnvelope",
    "ResearchFilesMeta",
    "ResearchParseView",
    "ResearchPromptsView",
    "ResearchQuotaView",
]