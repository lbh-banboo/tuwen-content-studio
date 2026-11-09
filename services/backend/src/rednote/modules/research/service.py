"""WP19 的组装层：把数据库里的行变成纯函数的输入，再把纯函数的输出
变成响应形状。

**本模块是唯一的"ORM ↔ 纯函数"边界**。上面是 `api`（HTTP），
下面一半是 `repository`（SQL），另一半是 `keywords`（零 I/O 纯函数）。
它做三件事，每件事都有明确的理由：

| 做什么 | 为什么必须在这一层 |
|---|---|
| `BriefClaim` 行 → `ClaimInput` | `keywords` 必须是纯函数（可穷举测试），不能收 ORM 实例（离开 session 即 `DetachedInstanceError`，WP18 踩过） |
| `structured_fields` → `FieldValueInput` 序列 | 那是一个 jsonb 三态结构（`{"status","value"}`），"哪些字段能造词"的判据要落在一处 |
| 纯函数输出 → 响应 dict | `schema.py` 的 DTO 负责形状，本层负责**取值**与**排序** |

---

## 一、`structured_fields` → `FieldValueInput` 的三条判据

`brief_versions.structured_fields` 的每个值是
`{"status": FIELD_FILLED|FIELD_MISSING|FIELD_NOT_APPLICABLE, "value": ...}`
（WP18 的 `brief.schema`，见那里的"值与其状态必须在一起"）。

| 状态 | 造词？ | 理由 |
|---|---|---|
| `FILLED` | **是** | 这是 Brief 里真实有内容的部分 —— 最有价值的检索词 |
| `MISSING` | 否 | 对空值造词会让运营去搜一个 Brief 里根本没提的东西 |
| `NOT_APPLICABLE` | **否** | 它的 `value` 位置承载的是**不适用原因**（WP18 的 `_read_entry` 明确要求非空），把它当检索词会搜出一堆完全不相关的结果 |

**`NOT_APPLICABLE` 不造词这一条特别重要**，因为它的值**非空**
（"本产品不含酒精，故无此参数"）—— 只判"值非空"的实现会把它当
一条正常取值用。这是一个"看起来填了东西"的陷阱。

## 二、多值字段的取值展开

`FieldSpec.multi=True` 的字段（卖点、痛点、场景……）值是 **list**，
且元素可能是**裸字符串**或 `{"text": ..., ...}` 这类对象
（WP18 的 `_selling_point_text` 处理过同样两种形状）。
本层统一抽成字符串：裸字符串直接用，dict 按 `text` → `label` → `value`
的顺序找第一个非空串。**抽不出文本的元素被丢弃而不是 `str(dict)`** ——
把 `"{'priority': 1}"` 当检索词会搜出一堆无关结果，而它看起来
像一条正常的关键词。

## 三、为什么"关键词语料的取值顺序"必须固定

`derive_keywords` 内部对所有输入做了排序（`keyword.py` 的
`sorted(...)`）。本层再保证 `FieldValueInput` 的**值列表**有序，
两处配合才能做到"同一份 Brief 版本永远派生出同一批关键词"。
值列表的顺序取自 jsonb 里的**原始顺序**（运营填写的顺序），
那是有意义的（卖点按优先级排），因此**不重排**，只去重。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from rednote.modules.brief.models import BriefVersion
from rednote.modules.brief.schema import BRIEF_FIELDS, FIELD_BY_PATH
from rednote.modules.brief.types import FIELD_FILLED
from rednote.modules.files.models import QUOTA_LIMIT_BYTES
from rednote.modules.parsing.types import PARSE_STATE_PARTIAL, PARSE_STATE_SUCCEEDED
from rednote.modules.research import keywords as kw
from rednote.modules.research.repository import (
    PARSE_STATE_NONE,
    ResearchFileRow,
    ResearchRepository,
)
from rednote.modules.research.types import (
    DERIVATION_PAYLOAD_VERSION,
    DERIVATION_RULES_VERSION,
    EVIDENCE_USABLE_STATES,
    FORMAT_MEDIA_ANY,
    GAP_MISSING,
    TASK_EVIDENCE_COLLECTION,
    ClaimInput,
    FieldValueInput,
)
from rednote.platform.errors import ApiError, ErrorCode

# ---------------------------------------------------------------------------
# ORM → 纯函数输入
# ---------------------------------------------------------------------------


def claim_inputs(claims: Sequence[Any]) -> tuple[ClaimInput, ...]:
    """`BriefClaim` 行 → `ClaimInput`。

    `has_evidence` 由 `keywords.has_evidence` 判定（**那处判据只有一份**，
    与 WP18 的 `ClaimDraft.__post_init__` 同口径）。

    `field_label` 走 `keywords.field_label`（未知路径降级为路径本身，
    不抛错 —— 这里是读取点，历史 claim 可能有已废弃的路径）。
    """
    result: list[ClaimInput] = []
    for claim in claims:
        result.append(
            ClaimInput(
                claim_id=claim.id,
                field_path=claim.field_path,
                field_label=kw.field_label(claim.field_path),
                text=claim.claim_text,
                category=claim.category,
                resolution=claim.resolution_status,
                has_evidence=kw.has_evidence(
                    evidence_segment_id=claim.evidence_segment_id,
                    evidence_url=claim.evidence_url,
                ),
            )
        )
    return tuple(result)


def field_value_inputs(
    structured_fields: Mapping[str, Any] | None,
) -> tuple[FieldValueInput, ...]:
    """`structured_fields` → `FieldValueInput` 序列（见模块 docstring 第一节）。

    **按 `BRIEF_FIELDS` 的定义顺序返回**（需求 §5.1 的 1~10 组）：
    `structured_fields` 是 jsonb，键顺序在写入方不保证稳定
    （Python dict 保序，但 jsonb 在 PostgreSQL 里**不保序**）。
    依赖键顺序会让"同一份数据派生出两种顺序的关键词"。
    """
    fields = dict(structured_fields or {})
    result: list[FieldValueInput] = []
    for spec in BRIEF_FIELDS:
        entry = fields.get(spec.path)
        if not isinstance(entry, Mapping):
            continue
        if entry.get("status") != FIELD_FILLED:
            # MISSING / NOT_APPLICABLE 都不造词（见模块 docstring 第一节）。
            continue
        values = _field_values(entry.get("value"), multi=spec.multi)
        if not values:
            continue
        result.append(FieldValueInput(path=spec.path, label=spec.label, values=values))
    return tuple(result)


def _field_values(raw: Any, *, multi: bool) -> tuple[str, ...]:
    """字段值 → 非空字符串元组（保序去重）。

    `multi=False` 但收到 list 时**不报错**：`structured_fields` 是 jsonb，
    数据回填或历史版本可能给出与 Schema 不符的形状。本层是读取点，
    降级处理（取全部元素）比让研究页 500 更负责；
    写入点的严格校验由 WP18 的 `normalize_field_values` 承担。
    """
    candidates: Sequence[Any]
    if isinstance(raw, list):
        candidates = raw
    elif multi:
        candidates = [raw]
    else:
        candidates = [raw]

    values: list[str] = []
    for item in candidates:
        text = _text_of(item)
        if text and text not in values:
            values.append(text)
    return tuple(values)


def _text_of(item: Any) -> str:
    """从字段值里抽出一个非空字符串。

    三种形状都遇到过（WP18 的 `_selling_point_text` 处理前两种）：
      · 裸字符串 / 数字：直接用；
      · dict：按 `text` → `label` → `value` 找第一个非空串；
      · 其余（list / None / 嵌套结构）：丢弃。

    **丢弃而不是 `str(item)`**：把 `"{'priority': 1}"` 或 `"[1, 2]"`
    当检索词会搜出一堆无关结果，而它在列表里看起来像一条正常的关键词。
    """
    if item is None:
        return ""
    if isinstance(item, str):
        return kw.normalize_term(item)
    if isinstance(item, bool):
        return ""
    if isinstance(item, (int, float)):
        return kw.normalize_term(item)
    if isinstance(item, Mapping):
        for key in ("text", "label", "value", "name"):
            candidate = item.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return kw.normalize_term(candidate)
        return ""
    return ""


# ---------------------------------------------------------------------------
# research-prompts：组装
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptsBundle:
    """`build_research_prompts` 的返回值（供 api 直接序列化）。

    **不用 dict 直接返回**：api 层需要拿到 `brief_version_id` / `notes`
    等元信息去组装 `DerivationView`，而 dict 的键名是"对外形状"
    （camelCase），内部传递用 dict 会让"改一个对外字段名"变成
    一次跨文件的字符串搜索。
    """

    project_id: uuid.UUID
    product_name: str
    brief_version_id: uuid.UUID | None
    revision_no: int | None
    claim_count: int
    truncated: bool
    notes: tuple[str, ...]
    keywords: tuple[kw.Keyword, ...]
    prompts: tuple[kw.Prompt, ...]
    gaps: tuple[kw.Gap, ...]
    summary: dict[str, int]


def build_research_prompts(
    repo: ResearchRepository,
    *,
    project_id: uuid.UUID,
    version_id: uuid.UUID | None = None,
) -> PromptsBundle:
    """组装 `GET /projects/{projectId}/research-prompts` 的数据。

    **顺序**（不可调换）：

      1. **项目存在性** → 不存在即 404。先判它再读版本：
         否则一个"项目不存在"的请求会因为读不到版本而报"版本不存在"，
         把两种不同的 404 混成一种（排障时会误导）。
      2. **解析 Brief 版本**：给了 `version_id` 就按
         `(版本, 项目)` 联合查（**在 SQL 里判项目**，见 repository）；
         没给就取当前生效版本。
      3. **版本不存在 → 404**（它是被寻址的资源）。
         **与 `PUT /brief-draft` 的 `baseVersionId` 报 422 的判据不同**：
         那里是"请求体里的字段值不对"，这里是"URL 上没有这个资源"。
      4. **无版本 → 空结果（200，不是错误）**：项目刚建、Brief 还没解构过。
         这不是错误，是一个**正常的前置状态** —— 报 404 会让前端
         把"还没做 Brief"显示成"出错了"。此时 `keywords` / `gaps`
         为空，`prompts` 给一条"先去完成 Brief"的引导提示词。
      5. 读 claims → 转 `ClaimInput` → 纯函数派生 → 组装。

    **`notes` 承载"这批结果为什么长这样"的说明**，三种情况各一条：
    没有 Brief 版本、解析状态是 `PARTIAL`（Brief 本身就不完整）、
    结果被截断。
    """
    repo.resolve_project_or_404(project_id)

    version = (
        repo.brief_version_in_project(version_id=version_id, project_id=project_id)
        if version_id is not None
        else repo.active_brief_version(project_id=project_id)
    )

    if version_id is not None and version is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "Brief 版本不存在。",
            details={"versionId": str(version_id), "projectId": str(project_id)},
        )

    if version is None:
        return _empty_bundle(project_id)

    fields = field_value_inputs(version.structured_fields)
    claims = claim_inputs(repo.list_claims_for_version(version.id))
    revision_no = _revision_no(repo, version)

    subject = kw.primary_subject(kw.entity_terms(fields))
    raw_keywords, keywords_truncated = kw.derive_keywords(claims, fields)
    raw_gaps, gaps_truncated = kw.derive_gaps(claims)
    gaps = kw.build_gap_keywords(raw_gaps, subject_prefix=subject)
    prompts, prompts_truncated = kw.build_prompts(gaps, fields)

    truncated = keywords_truncated or gaps_truncated or prompts_truncated
    notes = _bundle_notes(
        subject=subject,
        source_state=version.source_state,
        claim_count=len(claims),
        truncated=truncated,
    )

    return PromptsBundle(
        project_id=project_id,
        product_name=version.product_name,
        brief_version_id=version.id,
        revision_no=revision_no,
        claim_count=len(claims),
        truncated=truncated,
        notes=notes,
        keywords=raw_keywords,
        prompts=prompts,
        gaps=gaps,
        summary=kw.coverage_summary(gaps),
    )


def _bundle_notes(
    *,
    subject: str,
    source_state: str,
    claim_count: int,
    truncated: bool,
) -> tuple[str, ...]:
    """"这批结果为什么长这样"的说明清单（每条对应一种**降级情形**）。

    ## 为什么抽成纯函数（阶段 7 简化）

    `build_research_prompts` 原本 98 行，其中 30 行是四条 `notes`
    的判据与文案。抽出来的额外好处比"变短"更实在：
    **它是一个纯函数，可以穷举测试**（`claim_count=0` 且
    `truncated=True` 且主体词为空等组合），而内联在服务函数里
    只能靠"造出那种数据"来触发。

    ## 四条判据各说明什么

    | 情形 | 为什么要说 | 不说会怎样 |
    |---|---|---|
    | 主体词为空 | 关键词缺主体限定，跨品牌会串（G2 验收原文） | 运营搜出一堆别家的内容，以为系统给错了词 |
    | 源是 `PARTIAL` | 派生输入本身就不完整 | 运营以为"缺口就这几条"，其实原文还没解全 |
    | 结果被截断 | 当前展示的不是全部 | 运营以为看全了，漏掉后面的缺口 |
    | 没有 claims | 没有可派生的东西 | 页面空白，运营以为功能坏了 |

    **全部是"不静默"原则的落点**：一个降级的结果若不说明，
    就是一个"看起来正常但实际不可用"的结果（ISS-71 同型风险）。
    """
    notes: list[str] = []
    if not subject:
        # 主体词为空意味着 Brief 里连品牌/产品名都没填上。此时关键词
        # 全是裸短语（"续航"），跨品牌会串 —— G2 验收明写"跨品牌事实不串"。
        notes.append(
            "Brief 里没有品牌/产品名，关键词缺少主体限定，"
            "跨品牌检索可能串结果 —— 请先补齐产品名称。"
        )
    if source_state == PARSE_STATE_PARTIAL:
        notes.append(
            "该 Brief 版本的原文解析是 PARTIAL（部分内容未解出），"
            "由它派生的关键词与缺口可能不完整。"
        )
    if truncated:
        notes.append(
            "结果已按上限截断（关键词 / 缺口 / 提示词各自有上限），"
            "当前展示的不是全部 —— 请先处理已列出的部分。"
        )
    if not claim_count:
        notes.append(
            "该 Brief 版本没有任何结论（claims），因此没有可派生的关键词与缺口。"
            "若刚刚完成解构，请确认解构任务已完成。"
        )
    return tuple(notes)


def _empty_bundle(project_id: uuid.UUID) -> PromptsBundle:
    """没有任何 Brief 版本时的 200 响应体。

    **给一条引导提示词而不是空列表**：研究页第一次打开时，
    运营看到的应该是"先去完成 Brief 解构"，而不是一个空白页面
    —— 空白页面会让人以为功能坏了。这条提示词的 `taskKind` 用
    `EVIDENCE_COLLECTION`（它是"整理一份清单"这件事），
    `relatedFieldPaths` 为空（它不对应任何字段）。
    """
    guidance = kw.Prompt(
        # 用常量而不是裸字符串：`TASK_KINDS` / `FORMAT_MEDIA_KINDS`
        # 是本包声明的受控取值域，裸写"EVIDENCE_COLLECTION"会让
        # 将来改名时这一处静默失配（`schema` 里是 `str` 类型，
        # 任何字符串都能通过校验）。
        task_kind=TASK_EVIDENCE_COLLECTION,
        title="先完成 Brief 解构",
        text=(
            "这个项目还没有已固化的 Brief 版本。\n"
            "研究阶段的关键词与覆盖缺口都是从 Brief 推断出来的 ——"
            "没有 Brief 就没有研究目标。\n\n"
            "请先上传 Brief 原文并完成解构（步骤 1），再回到本页面。"
        ),
        format_hint=kw.FormatHint(
            media=FORMAT_MEDIA_ANY,
            fields=("briefFileId", "briefVersionId"),
            description="上传 Brief 原文并完成解构后，本页面会自动给出关键词与缺口清单。",
        ),
    )
    return PromptsBundle(
        project_id=project_id,
        product_name="",
        brief_version_id=None,
        revision_no=None,
        claim_count=0,
        truncated=False,
        notes=(
            "该项目还没有 Brief 版本（可能未上传原文，或解构尚未完成）。",
        ),
        keywords=(),
        prompts=(guidance,),
        gaps=(),
        summary=kw.coverage_summary(()),
    )


def _revision_no(repo: ResearchRepository, version: BriefVersion) -> int | None:
    """版本在 `version_nodes` 上的 `revision_no`。

    **它不是 `brief_versions` 的列**（数据结构 02 §6 的列清单里没有它，
    它属于版本注册表，WP11）。因此要单独查一次。
    查不到时返回 `None` 而不是 0 —— "没有节点记录"与"第 0 版"
    是两件事（后者不可能，revision_no 从 1 起）。
    """
    from rednote.modules.versions.models import VersionNode

    node = repo.session.get(VersionNode, version.id)
    if node is None:
        return None
    return int(node.revision_no)


# ---------------------------------------------------------------------------
# research-files：组装
# ---------------------------------------------------------------------------


def parse_state_of(row: ResearchFileRow) -> str:
    """文件的解析状态（`PARSE_STATE_NONE` 表示从未发起解析）。"""
    return row.parse_state or PARSE_STATE_NONE


def evidence_usable(row: ResearchFileRow) -> bool:
    """这个文件的解析结果能不能当证据用（R5 的落点）。

    判据表见 `schema.ResearchFileView` 的 docstring。**只承认
    `SUCCEEDED` 与 `PARTIAL`**：

      · `PARTIAL` 判 `True` 是刻意的 —— 缺的部分由 `parse.errors` 说明，
        判 `False` 会让运营白扔一半可用内容；
      · 其余一律 `False`。特别是 `FAILED`：WP17 已经把"不支持格式"
        做成一个**可查询的终态**（`error_code='UNSUPPORTED_FORMAT'`），
        本层据 `state` 把它从覆盖里剔除 —— 否则"这个文件解析过、
        结论是不支持"在研究页上看起来像一份有效证据。

    **判据集合来自 `types.EVIDENCE_USABLE_STATES`**，不在这里内联
    `("SUCCEEDED", "PARTIAL")`：`repository.quota_totals` 要用同一个
    集合在 SQL 里数**全量**可用数，两处各写一份必然漂移，
    而漂移的表现是"列表说这个文件可用、配额里的可用数却没算它"。
    """
    return row.parse_state in EVIDENCE_USABLE_STATES


@dataclass(frozen=True)
class FilesBundle:
    """`list_research_files` 的返回值。"""

    rows: tuple[ResearchFileRow, ...]
    total_files: int
    quota: dict[str, Any]


def list_research_files(
    repo: ResearchRepository,
    *,
    project_id: uuid.UUID,
    parse_states: Sequence[str] | None = None,
) -> FilesBundle:
    """组装 `GET /projects/{projectId}/research-files` 的数据。

    **配额走 WP12 的唯一口径**（R4 的落点）：
    `files_service.quota_snapshot` 读账面，
    `FileRepository.compute_effective_bytes` 做重算。

    **为什么要同时给"账面"与"重算"两个数**：文档 02 §13.1 原文
    "定时对账按文件关联重建账面并报警"。账面（`quota_accounts`）
    是写路径累加出来的，重算是从明细推出的 —— 两者不一致就是
    "账面与明细不符"，必须让运营看见（`reconciled` 字段）。
    本包**不自己乘除一遍**：那会造出第三个口径。

    **`listedBytes` 与本页无关，是"本列表全部文件"的和**：
    它不是分页的（分页在 api 层，用内存游标），因此
    `listedBytes` 与 `fileCount` 都是**全量口径** —— 否则
    "配额用了多少"会随翻页变化，那是彻底错误的。

    ## `parse_states` 只筛 `rows`，不筛配额（Code Review 修正）

    本函数的第一版从 `rows` 算 `listedBytes` / `usableEvidenceCount`，
    而 `rows` 已被 `parse_states` 过滤 —— 于是带 `?status=SUCCEEDED`
    请求时同一个 `meta.research` 里出现两个口径：

      · `fileCount` 报全部（它走独立的计数查询）；
      · `listedBytes` 只报筛出来的那几个文件；
      · `reconciled` 翻成 `False`，前端把它读成
        "账面与明细不符，磁盘可能被人绕过写路径改了"的**告警**。

    **一个假告警比没有告警更伤信任**：运营学会忽略它之后，
    真告警也不会再被看见。因此配额三块改用 `quota_totals()`
    独立查询（与 `count_research_files` 同一手法，同样不受
    `RESEARCH_FILE_SCAN_LIMIT` 影响）—— **筛选属于列表，
    不属于配额**。
    """
    repo.resolve_project_or_404(project_id)
    rows = repo.list_research_files(project_id=project_id, parse_states=parse_states)
    # 配额走**未过滤**的独立查询（不与 `rows` 共用）。
    totals = repo.quota_totals(project_id=project_id)
    total = totals.file_count

    listed_bytes = totals.listed_bytes
    usable = totals.usable_count

    # 账面与重算都走 WP12 的 `FileRepository`（**唯一口径**）。
    # **刻意不调 `files_service.quota_snapshot`**：那个函数收
    # `(repo, project_id)`，要再造一个 `FileRepository` 实例；
    # 而下面两个方法本来就在同一个实例上 —— 同一个 session 上
    # 造两个实例是零代价的（它是无状态薄封装），但多一次构造
    # 会让读者以为"这两个数来自两套账面"。本层只用一套。
    from rednote.modules.files.repository import FileRepository

    file_repo = FileRepository(repo.session)
    effective_bytes = file_repo.compute_effective_bytes(project_id)
    quota_row = file_repo.get_quota_account(project_id)
    reserved_bytes = int(quota_row.reserved_bytes) if quota_row is not None else 0
    limit_bytes = (
        int(quota_row.limit_bytes) if quota_row is not None else QUOTA_LIMIT_BYTES
    )

    remaining = max(0, limit_bytes - effective_bytes - reserved_bytes)

    return FilesBundle(
        rows=tuple(rows),
        total_files=total,
        quota={
            "limitBytes": str(limit_bytes),
            "effectiveBytes": str(effective_bytes),
            "reservedBytes": str(reserved_bytes),
            "remainingBytes": str(remaining),
            "listedBytes": str(listed_bytes),
            "fileCount": total,
            "usableEvidenceCount": usable,
            # 明细之和是否等于账面。**两个口径本就该相等**——
            # 不等说明有文件没走完预占转换（WP12 的
            # `finalize_reservation`），或有人绕过写路径改了数据。
            "reconciled": listed_bytes == effective_bytes,
        },
    )


# ---------------------------------------------------------------------------
# 序列化（纯函数 → 响应 dict）
# ---------------------------------------------------------------------------


def keyword_view(keyword: kw.Keyword) -> dict[str, Any]:
    return {
        "text": keyword.text,
        "sourceKind": keyword.source_kind,
        # **term 的顺序就是"拼装顺序"**（主体词在前）——
        # 前端据此展示"这个词是怎么拼出来的"，让推荐可核对。
        "terms": list(keyword.terms),
        "sourceFieldPath": keyword.source_field_path,
        "sourceFieldLabel": keyword.source_field_label,
        "relatedClaimId": (
            str(keyword.related_claim_id) if keyword.related_claim_id else None
        ),
    }


def prompt_view(prompt: kw.Prompt) -> dict[str, Any]:
    return {
        "taskKind": prompt.task_kind,
        "title": prompt.title,
        "text": prompt.text,
        "formatHint": {
            "media": prompt.format_hint.media,
            "fields": list(prompt.format_hint.fields),
            "description": prompt.format_hint.description,
        },
        "relatedFieldPaths": list(prompt.related_field_paths),
        "keywords": list(prompt.keywords),
    }


def gap_view(gap: kw.Gap) -> dict[str, Any]:
    return {
        "claimId": str(gap.claim_id),
        "fieldPath": gap.field_path,
        "fieldLabel": gap.field_label,
        "claimText": gap.claim_text,
        "category": gap.category,
        "resolution": gap.resolution,
        "state": gap.state,
        "blocking": gap.blocking,
        "suggestedKeywords": list(gap.suggested_keywords),
        "note": gap.note,
    }


def file_view(row: ResearchFileRow) -> dict[str, Any]:
    """研究文件 → 响应 dict。

    容量类字段（`byteSize` / `totalUnits` / `parsedUnits`）是**十进制字符串**
    （协议 §1）；`segmentCount` 是 int（见 `schema.py` 第二节的判据）。
    """
    state = parse_state_of(row)
    usable = evidence_usable(row)
    return {
        "fileId": str(row.file_id),
        "originalName": row.original_name,
        "mimeType": row.mime_type,
        "byteSize": str(row.byte_size),
        "classification": row.classification,
        "scanStatus": row.scan_status,
        "purpose": row.purpose,
        "effective": row.effective,
        "uploadedBy": str(row.uploaded_by),
        "createdAt": row.created_at.isoformat(),
        "parse": {
            "state": state,
            "complete": state == PARSE_STATE_SUCCEEDED,
            "parseRunId": str(row.parse_run_id) if row.parse_run_id else None,
            "parserVersion": row.parser_version,
            "segmentCount": row.segment_count,
            "totalUnits": None if row.total_units is None else str(row.total_units),
            "parsedUnits": None if row.parsed_units is None else str(row.parsed_units),
            "errors": [dict(item) for item in row.parse_errors if isinstance(item, Mapping)],
        },
        "evidenceUsable": usable,
    }


def derivation_view(bundle: PromptsBundle) -> dict[str, Any]:
    return {
        "rulesVersion": DERIVATION_RULES_VERSION,
        "payloadVersion": DERIVATION_PAYLOAD_VERSION,
        "briefVersionId": (
            str(bundle.brief_version_id) if bundle.brief_version_id else None
        ),
        "revisionNo": bundle.revision_no,
        "claimCount": bundle.claim_count,
        "truncated": bundle.truncated,
        "notes": list(bundle.notes),
    }


#: 缺口状态 → 是否阻断（供 api 在筛选时复用，也可被测试直接断言）。
GAP_BLOCKING: frozenset[str] = frozenset({GAP_MISSING})

#: 供 `FIELD_BY_PATH` 的消费者核对：本模块依赖的字段标签来源。
FIELD_LABEL_SOURCE: str = "rednote.modules.brief.schema.BRIEF_FIELDS"


__all__ = [
    "FIELD_LABEL_SOURCE",
    "GAP_BLOCKING",
    "FilesBundle",
    "PromptsBundle",
    "build_research_prompts",
    "claim_inputs",
    "derivation_view",
    "evidence_usable",
    "field_value_inputs",
    "file_view",
    "gap_view",
    "keyword_view",
    "list_research_files",
    "parse_state_of",
    "prompt_view",
    "FIELD_BY_PATH",
]