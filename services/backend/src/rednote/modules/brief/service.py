"""Brief 结构化解构的业务规则（WP18 的**唯一对外入口**）。

本模块回答四个问题：

| 问题 | 函数 |
|---|---|
| 这个项目的 Brief 与草稿在哪？ | `ensure_brief` / `get_view` |
| 怎么从上传的文档解构出 Brief？ | `request_parse` / `execute_decomposition` |
| 运营怎么改草稿、怎么答复疑问？ | `update_draft` |
| 怎么把草稿固化成不可变版本？ | `solidify` |
| 怎么基于已有版本加厚？ | `request_enrichment` / `execute_enrichment` |

---

## 一、`state='PARTIAL'` 的显式处理（硬约束，本包最要紧的一条）

WP17 对下游的契约要求（`docs/m1/WP17-文档解析与OCR说明.md` §10.2 末段）：

> **必须显式处理 `state='PARTIAL'`**。它不是 `SUCCEEDED` 的变体，
> 而是一个独立状态 —— 拿残缺输入当完整输入，是本包花了最大力气去防的事。

本模块的处理**不是**一个 `if state != 'SUCCEEDED': raise`，而是三分支：

| `parse_runs.state` | 本模块做什么 | 为什么 |
|---|---|---|
| `SUCCEEDED` | 正常解构 | — |
| `PARTIAL` | **照常解构，但把缺失变成疑问**（`severity='WARN'`，逐 unit 一条）+ `source_state='PARTIAL'` | 残缺文档**仍然有可用内容**；拒绝它等于让运营白传一次。真正的风险是"残缺被当成完整"，所以要把缺失**显式变成一条可查的记录** |
| `FAILED` / `CANCELLED` | **拒绝消费**（422，不调模型、不写草稿） | "一个 unit 都没解出来"意味着没有任何可用输入。此时调模型只会让它凭空编造 |

`PARTIAL` 分支的三层落地（缺一不可，见 `models.py` / `ddl.py` 的 docstring）：
1. `source_state` 是受控枚举且 `PARTIAL` 独立于 `SUCCEEDED`；
2. `brief_drafts` 的 CHECK：`PARTIAL` 时 `issue_resolutions` 必须非空；
3. `brief_versions` 的延迟触发器：固化的 `PARTIAL` 版本必须有 `brief_issues` 行。

**为什么 `PARTIAL` 用 `WARN` 而不是 `BLOCK`**：需求 §5.3 的完成条件允许
"原材料不足时如实记录"，而 §5.1 明确"缺失字段必须显示'待补充'"。
若用 `BLOCK`，一份缺了两页的 Brief 会**永远无法固化** ——
运营唯一的出路是伪造内容。而真正的阻断项是"解构没看出禁提项"
这类**会导致违规**的疑问。判据是**失败模式**，不是"严重程度"。

## 二、为什么解构会拒绝覆盖一个"有未固化编辑"的草稿

`execute_decomposition` 会整体替换草稿的 `structured_fields` / `claims` /
`issue_resolutions`（它们是**这份文档的产物**）。若运营已经手工编辑过
而没固化，替换就等于**静默丢弃他的工作**。

因此本模块先判 `draft_has_unsolidified_edits`：有未固化编辑时返回
409 `INVALID_TRANSITION`，理由写在 `details.reason` 里。
运营的出路是"先固化，再重新解构" —— 固化会把他的编辑变成不可变版本，
于是没有任何东西丢失。

**为什么不做成"合并"**：合并需要一条"哪个字段以谁为准"的规则，
而任何这样的规则在"模型填了 A、运营改成 B、重新解构后模型又填 A"时
都会给出让人意外的结果。**拒绝并说清楚**比"猜一个合并策略"负责。

## 三、`solidify` 的闭环（验收原文：确认版本闭环）

四个环节，每一步都有结构性保证：

| # | 环节 | 保证 |
|---|---|---|
| 1 | 必填字段已填或标不适用 | 422 `VALIDATION_FAILED`，`details.missingFields` 列出具体路径 |
| 2 | 没有未答复的核心疑问 | 409 `INVALID_TRANSITION`，`details.blockingIssues` 列出具体疑问 |
| 3 | 内容真的变了 | 409 `INVALID_TRANSITION`，`reason='draft_unchanged'`（空保存不产生版本） |
| 4 | 注册表与具体表同事务 + 节点一致性 | `register_version` 返回 `node_id`，本模块用它作 `brief_versions.id`；OP-88 的延迟触发器在提交时校验 |

第 3 条的判据是**内容摘要**，不是 `lock_version`：
`lock_version` 在每次保存时都会递增（哪怕改回原值），用它判"变没变"
会把"改了又改回来"当成有变更 —— 而那正是 `draft_unchanged` 要拦的情形。

## 四、加厚建议**不自动生效**（协议 §5 第 5 行原文）

`execute_enrichment` 把建议写进 `brief_drafts.suggestions`，
**不写进 `claims`**。运营要采纳某条建议，是在 `PUT /brief-draft` 里
把它显式加进 `claims` —— 那一步会走 `ClaimDraft` 的校验
（分类必须有来源），于是"建议变成事实"这件事永远要经过一次显式确认。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy.exc import IntegrityError

from rednote.modules.brief import schema as brief_schema
from rednote.modules.brief.decomposer import BriefDecomposer
from rednote.modules.brief.models import (
    Brief,
    BriefClaim,
    BriefDraft,
    BriefIssue,
    BriefVersion,
)
from rednote.modules.brief.repository import BriefRepository
from rednote.modules.brief.types import (
    CLAIM_HASH_LENGTH,
    ISSUE_OPEN,
    ISSUE_SETTLED_STATES,
    ISSUE_STATES,
    MAX_CLAIMS_PER_VERSION,
    MAX_ISSUES_PER_VERSION,
    MIN_SELLING_POINTS_EXPECTED,
    RESOLUTION_CONFIRMED,
    RESOLUTION_PENDING,
    RESOLUTIONS,
    SEVERITY_BLOCK,
    SEVERITY_WARN,
    SOURCE_STATE_NONE,
    ClaimDraft,
    IssueDraft,
)
from rednote.modules.files.models import PURPOSE_BRIEF, VISIBILITY_PROJECT
from rednote.modules.files.repository import FileRepository
from rednote.modules.parsing.repository import ParseRepository
from rednote.modules.parsing.types import (
    PARSE_STATE_CANCELLED,
    PARSE_STATE_FAILED,
    PARSE_STATE_PARTIAL,
    PARSE_STATE_SUCCEEDED,
)
from rednote.modules.versions.models import ENTITY_TYPE_BRIEF
from rednote.modules.versions.repository import VersionRepository
from rednote.modules.versions.service import canonical_payload_hash, register_version
from rednote.platform.concurrency import build_etag
from rednote.platform.errors import ApiError, ErrorCode

# 能被解构消费的解析状态。**`PARTIAL` 刻意不在其中** ——
# 它不是"不能消费"，而是"消费路径不同"（要额外产出缺失疑问）。
# 用两个集合把这件事写清楚，比在一个 if 里塞三个条件可读得多。
_CONSUMABLE_STATES: Final[frozenset[str]] = frozenset(
    {PARSE_STATE_SUCCEEDED, PARSE_STATE_PARTIAL}
)

# 解析未完成的状态（任务还在跑）。**不是失败** ——
# 请求会在稍后重试，报 FAILED 会让任务框架把它标成不可重试。
_PENDING_STATES: Final[frozenset[str]] = frozenset({"QUEUED", "RUNNING"})

# 版本 payload 的 schema 版本（`version_nodes.schema_version`）。
# 它与 `brief_versions.schema_version`（结构化字段的形状版本）是两件事：
# 前者是"注册表里那个摘要算的是哪个形状"，后者是"字段怎么解读"。
# 两者分开是因为它们的变化原因不同 —— 与 WP11 的 `payload_schema_version` 同理。
PAYLOAD_SCHEMA_VERSION: Final[int] = 1


# ---------------------------------------------------------------------------
# 错误构造
# ---------------------------------------------------------------------------


def _validation_error(field: str, message: str, *, reason: str, **extra: Any) -> ApiError:
    details: dict[str, Any] = {
        "fieldErrors": [{"field": field, "type": reason, "message": message}],
        "reason": reason,
    }
    details.update(extra)
    return ApiError(ErrorCode.VALIDATION_FAILED, message, details=details)


def _conflict(message: str, *, reason: str, **extra: Any) -> ApiError:
    details: dict[str, Any] = {"reason": reason}
    details.update(extra)
    return ApiError(ErrorCode.INVALID_TRANSITION, message, details=details)


# ---------------------------------------------------------------------------
# 逻辑 Brief 与草稿
# ---------------------------------------------------------------------------


def ensure_brief(repo: BriefRepository, *, project_id: uuid.UUID) -> Brief:
    """取（或建出）项目的逻辑 Brief 与它的初始空草稿。

    **为什么必须存在这个函数**：协议 §5 末段原文 ——

    > Brief、信息和方案的初始空草稿由项目／上游结果事务创建；
    > GET 返回其 ETag，首次 PUT 也使用该 ETag，
    > **不要求客户端猜测一个不存在资源的版本**。

    协议把"创建初始草稿"归给项目创建事务，但项目创建是 WP10 的代码，
    本包不能改它。因此本模块把它做成**幂等的懒创建**：
    `GET /brief` 第一次被调用时建出来，此后每次都是读。

    **代价（已登记为待定项）**：`GET` 是一个会写库的请求。
    它幂等（`UQ(briefs.project_id)` + `UQ(brief_drafts.brief_id)` 兜底），
    但它违反了"GET 是安全方法"的 HTTP 约定 —— 一个只读的爬虫/预取
    会造出草稿行。收口方向是让主 agent 在 WP10 的项目创建事务里
    补一次调用，那时本函数退化成纯读。
    """
    brief = repo.get_brief_by_project(project_id)
    if brief is not None:
        if brief.draft_id is None:
            _create_initial_draft(repo, brief=brief)
        return brief

    if not repo.project_exists(project_id):
        raise _validation_error(
            "projectId", "项目不存在。", reason="project_not_found"
        )

    try:
        brief = repo.insert_brief(project_id=project_id)
        repo.flush()
    except IntegrityError:
        # 并发：另一个请求刚建出来（`UQ(briefs.project_id)` 拦下）。
        # **重新读一次**而不是报错 —— 两个请求的意图完全相同。
        repo.session.rollback()
        existing = repo.get_brief_by_project(project_id)
        if existing is None:
            raise
        brief = existing

    _create_initial_draft(repo, brief=brief)
    return brief


def _create_initial_draft(repo: BriefRepository, *, brief: Brief) -> BriefDraft:
    """建初始空草稿：**每个字段都在，状态是 MISSING**。

    见 `schema.empty_field_values` 的理由：`MISSING` 是一个显式事实
    （"这个字段我们知道要问，但原文里没有"），与"这个字段不在 Schema 里"
    是两件事。
    """
    draft = repo.get_draft_for_brief(brief.id)
    if draft is not None:
        repo.set_draft(brief_id=brief.id, draft_id=draft.id)
        return draft

    draft = repo.insert_draft(
        brief_id=brief.id, structured_fields=brief_schema.empty_field_values()
    )
    repo.flush()
    repo.set_draft(brief_id=brief.id, draft_id=draft.id)
    repo.flush()
    return draft


def require_draft(repo: BriefRepository, *, brief: Brief) -> BriefDraft:
    """取草稿；不存在即建（与 `ensure_brief` 同一条懒创建口径）。"""
    draft = (
        repo.get_draft(brief.draft_id)
        if brief.draft_id is not None
        else repo.get_draft_for_brief(brief.id)
    )
    if draft is None:
        return _create_initial_draft(repo, brief=brief)
    return draft


def draft_etag(draft: BriefDraft) -> str:
    """草稿的**可变** ETag（`build_etag(draft_id, lock_version)`）。

    与 WP11 的口径一致：草稿阶段资源可变、ETag 每次保存递增；
    固化之后换成不可变 ETag（`build_immutable_etag`）。
    这个切换点是本包与 WP11 的接缝，也是"确认版本闭环"的可观察证据。
    """
    return build_etag(draft.id, draft.lock_version)


# **刻意不提供 `version_etag(version)`**：不可变 ETag 的唯一来源是
# `version_nodes.payload_hash`（WP11 的 `VersionNode.etag`）。
# 若在本模块按版本行的字段重算一遍摘要，两处口径一旦不同
# （键序、浮点表示、新增字段），前端拿到的 ETag 就与
# `GET /versions/{id}` 返回的对不上 —— 而那是同一个东西。
# **同一个东西必须只有一个摘要来源**，因此本模块不造第二个。
# `_version_view` / `_version_summaries` 都通过 `VersionRepository.get_node`
# 取 `node.etag`。


# ---------------------------------------------------------------------------
# 解析与解构
# ---------------------------------------------------------------------------


def require_brief_file(
    file_repo: FileRepository,
    *,
    project_id: uuid.UUID,
    file_id: uuid.UUID,
) -> Any:
    """校验"这个文件是**本项目的 BRIEF 用途**文件"（协议 §5 第 1 行原文）。

    四个判据，顺序不可调换：

      1. 文件存在 → 否则 422（它出现在请求体里，是"被引用的值"，
         不是被寻址的资源 —— 与 WP11 的 `require_version_node` 同一判据）；
      2. `visibility=PROJECT` 且 `project_id` 匹配 → 否则 422
         （**不报 404**：同样是"你给的字段值无效"）；
      3. 用途关联是 `BRIEF` 且 `effective` → 否则 422。
         这是"先确认文件是该项目 BRIEF"这句原文的落点；
      4. `classification=SUPPORTED` 不在这里判 —— 不支持格式也要能
         发起解析，并得到一个"不支持"的**可查询状态**（WP17 的
         `request_parse` docstring 已钉死这条）。

    判据 3 需要 `project_file_links` 里有一条 `purpose='BRIEF'` 的
    有效关联。**若没有**，本函数报 422 并带上 `hint` 说明该怎么补 ——
    而不是静默放行：静默放行会让"研究资料被当成 Brief 解构"成为可能，
    而那会把一堆无关内容灌进 Brief 字段。

    **本函数不需要 `BriefRepository`**：它的四个判据全部落在
    `file_objects` 与 `project_file_links` 上。不接收一个用不到的仓库参数，
    是为了让"它到底依赖什么"从签名上就能读出来。
    """
    file_row = file_repo.get_file_object(file_id)
    if file_row is None:
        raise _validation_error(
            "fileId", "文件不存在。", reason="file_not_found"
        )
    if file_row.visibility != VISIBILITY_PROJECT or file_row.project_id != project_id:
        raise _validation_error(
            "fileId",
            "该文件不属于本项目。",
            reason="file_project_mismatch",
            fileProjectId=(
                str(file_row.project_id) if file_row.project_id else None
            ),
        )
    link = file_repo.find_active_link(
        project_id=project_id, file_id=file_id, purpose=PURPOSE_BRIEF
    )
    if link is None:
        raise _validation_error(
            "fileId",
            "该文件在本项目里的用途不是 BRIEF，不能作为 Brief 解构的来源。",
            reason="file_purpose_not_brief",
            hint=(
                "上传时把 purpose 设为 BRIEF（`POST /projects/{id}/uploads`），"
                "或补一条 purpose=BRIEF 的 project_file_links 关联。"
            ),
        )
    return file_row


def request_parse(
    parse_repo: ParseRepository,
    file_repo: FileRepository,
    *,
    project_id: uuid.UUID,
    file_id: uuid.UUID,
) -> tuple[Any, bool]:
    """登记一次 Brief 解析（**端点调用**，返回 `(run, created)`）。

    **本函数只做"登记"，不做解构** —— 与 WP17 的 `request_parse` 同一分工：
    端点返回 202，实际解构由 Worker 在 `BRIEF_PARSE` 任务里执行。
    合起来的话，一份 200 页 Brief 会让 HTTP 请求挂几分钟，
    而协议 §5 第 1 行明确要求 **202**。

    **为什么调用 WP17 的 `request_parse` 而不是自己插 run**：
    它是"什么时候可以复用一次解析"的**唯一权威**
    （`find_reusable_run(file_id, parser_version)`，数据结构 02 §4）。
    自己插一行会让系统里出现两个可能不一致的答案 ——
    而"结果可复用"被违反的代价是**同一份文档被解析两次**，
    于是同一个文件会有两条 run、两批片段，而 claims 的证据指向哪一批
    取决于时序。这正是本包最不想要的一类不确定性。

    因此本函数**先做本包的判据（文件是 BRIEF、属于本项目），
    再把登记整件事交给 WP17** —— 判据在调用方，语义在被调用方。
    """
    require_brief_file(file_repo, project_id=project_id, file_id=file_id)
    from rednote.modules.parsing import service as parsing_service

    file_row = file_repo.get_file_object(file_id)
    assert file_row is not None  # `require_brief_file` 已判存在
    return parsing_service.request_parse(parse_repo, file_row=file_row)


@dataclass(frozen=True)
class DecompositionOutcome:
    """一次解构落到草稿上的结果。"""

    draft_id: uuid.UUID
    source_state: str
    claim_count: int
    issue_count: int
    #: 输入缺失导致的疑问数（`PARTIAL` 时逐 unit 一条）。
    missing_unit_count: int
    #: 解构通道标识（日志与排障用）。
    decomposer: str


def execute_decomposition(
    repo: BriefRepository,
    parse_repo: ParseRepository,
    *,
    brief: Brief,
    parse_run_id: uuid.UUID,
    decomposer: BriefDecomposer,
    created_by: uuid.UUID | None = None,
    model_profile_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    attempt_id: uuid.UUID | None = None,
) -> DecompositionOutcome:
    """执行一次解构并把结果写进草稿（**任务处理器调用**）。

    **`state='PARTIAL'` 的显式处理在这里**（见模块 docstring 第一节）。

    顺序（不可调换）：

      1. 取 run；不存在 → 抛（这是装配缺陷，不是业务失败）；
      2. 判状态：`FAILED`/`CANCELLED` → 422（**不消费**）；
         `QUEUED`/`RUNNING` → 409（还没好，**不是失败** ——
         报 FAILED 会让任务框架标成不可重试）；
         `PARTIAL`/`SUCCEEDED` → 继续；
      3. **草稿是否干净**：有未固化编辑 → 409（不静默覆盖运营的工作）；
      4. 读片段（WP17 的 `list_segments`，**按原文顺序**）；
      5. 调解构通道；
      6. **`PARTIAL` 时把缺失变成疑问**（逐 unit 一条 `WARN`）；
      7. 写草稿（整体替换）+ `source_state` + `source_errors`。

    **第 4 步用 `segment_no` 顺序**（WP17 保证），不用 `created_at` ——
    后者是事务时间戳，同一批插入的行值相同（WP17 的 ISS-52 实测）。
    """
    run = parse_repo.get_run(parse_run_id)
    if run is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "解析记录不存在。",
            details={"parseRunId": str(parse_run_id)},
        )

    if run.state in (PARSE_STATE_FAILED, PARSE_STATE_CANCELLED):
        # **不得消费**：一个 unit 都没解出来时，调模型只会让它凭空编造。
        raise _conflict(
            f"解析结果是 {run.state}，没有可用内容，不能解构 Brief。"
            "（本系统不会用模型编造内容填补'一个 unit 都没解出来'的输入。）",
            reason="parse_run_not_consumable",
            parseRunId=str(run.id),
            state=run.state,
        )
    if run.state in _PENDING_STATES:
        # 还没跑完。**409 而不是 FAILED**：这不是失败，是"还没好"。
        raise _conflict(
            f"解析尚未完成（{run.state}），请稍后重试。",
            reason="parse_run_pending",
            parseRunId=str(run.id),
            state=run.state,
        )
    if run.state not in _CONSUMABLE_STATES:  # pragma: no cover - 防御性
        raise _conflict(
            f"未知的解析状态：{run.state}。",
            reason="parse_run_state_unknown",
            parseRunId=str(run.id),
            state=run.state,
        )

    draft = require_draft(repo, brief=brief)
    if draft_has_unsolidified_edits(repo, draft=draft):
        raise _conflict(
            "草稿里有尚未固化的编辑，重新解构会丢弃它们。"
            "请先固化当前草稿（POST /projects/{id}/brief-versions），再重新解构。",
            reason="draft_has_unsolidified_edits",
            draftId=str(draft.id),
        )

    segments = tuple(parse_repo.list_segments(run.id))
    result = decomposer.decompose(
        project_id=brief.project_id,
        file_id=run.file_id,
        segments=segments,
        model_profile_id=model_profile_id,
        job_id=job_id,
        attempt_id=attempt_id,
    )

    issues = list(result.issues)
    missing_units: tuple[int, ...] = ()
    if run.state == PARSE_STATE_PARTIAL:
        missing_units = _missing_units(run=run, segments=segments)
        issues = list(_missing_unit_issues(run=run, units=missing_units)) + issues

    fields = _normalize_decomposed_fields(result.fields)
    claims = _enforce_claim_limit(result.claims, issues=issues)

    _write_draft_decomposition(
        repo,
        draft=draft,
        fields=fields,
        claims=claims,
        issues=issues,
        source_file_id=run.file_id,
        source_parse_run_id=run.id,
        source_state=run.state,
        source_errors=list(run.errors or []),
    )

    return DecompositionOutcome(
        draft_id=draft.id,
        source_state=run.state,
        claim_count=len(claims),
        issue_count=len(issues),
        missing_unit_count=len(missing_units),
        decomposer=getattr(decomposer, "name", "unknown"),
    )


def _missing_units(*, run: Any, segments: Sequence[Any]) -> tuple[int, ...]:
    """`PARTIAL` 时哪些 unit 没解出来。

    **从 `run.errors` 取**（WP17 逐 unit 记的 `{unit, code, message}`），
    **不从计数差推**：`total_units - parsed_units` 只知道"缺几个"，
    不知道"缺哪些" —— 而"缺的是第 7、12 页"才是运营能行动的信息
    （"缺 2 页"他没法判断要不要紧）。

    **兜底**：`errors` 为空但状态是 `PARTIAL`（不该发生，但可能因为
    WP17 的实现在某个分支上漏记）时，用"片段覆盖的 unit 集合"补出差额 ——
    宁可给一个粗糙的列表，也不要让"缺了东西"这件事在库里只体现为一个
    没有明细的状态值。`segments` 参数就是为这条兜底存在的。
    """
    units: list[int] = []
    for item in run.errors or []:
        if not isinstance(item, dict):
            continue
        raw = item.get("unit")
        if isinstance(raw, int) and raw > 0:
            units.append(raw)
    if units:
        return tuple(sorted(set(units)))

    # 兜底：按 position 里的 unit 编号补差额。
    seen: set[int] = set()
    for segment in segments:
        position = getattr(segment, "position", None)
        if not isinstance(position, dict):
            continue
        for key in ("page", "index", "line"):
            value = position.get(key)
            if isinstance(value, int):
                seen.add(value)
    total = run.total_units or 0
    return tuple(sorted(set(range(1, total + 1)) - seen))


def _missing_unit_issues(*, run: Any, units: Sequence[int]) -> tuple[IssueDraft, ...]:
    """把"缺了哪些 unit"变成疑问记录（`severity='WARN'`）。

    **为什么是 `WARN` 而不是 `BLOCK`**：见模块 docstring 第一节 ——
    用 `BLOCK` 会让一份缺页的 Brief 永远无法固化，运营唯一的出路是伪造内容。

    `field_path` 用 `*`：缺失的是**文档的某一页**，不是某个具体字段 ——
    强行指向一个字段会让前端高亮一个它其实不知道有没有问题的输入框。
    """
    del run  # 只用于签名可读性；描述文本由 units 决定
    if not units:
        return ()
    preview = ", ".join(str(unit) for unit in units[:20])
    suffix = "" if len(units) <= 20 else f" 等 {len(units)} 个单元"
    return (
        IssueDraft(
            id=uuid.uuid4(),
            field_path="*",
            severity=SEVERITY_WARN,
            description=(
                f"来源文档解析为部分成功：有 {len(units)} 个单元未解出"
                f"（{preview}{suffix}）。这些单元的内容没有进入本次解构，"
                "相关字段可能显示为待补充。"
            ),
            state=ISSUE_OPEN,
        ),
    )


def _normalize_decomposed_fields(raw: Any) -> dict[str, Any]:
    """归一化模型返回的字段。形状不符时退回空草稿（**不报错**）。

    **为什么不报错**：模型输出是不可信输入（协议 §13 第 4 条）。
    一个畸形的 `fields` 不该让整次解构失败 —— 结论（claims）往往
    仍然是可用的，而"字段没填好"已经由必填校验在固化时兜住。
    """
    try:
        return brief_schema.normalize_field_values(raw)
    except ValueError:
        return brief_schema.empty_field_values()


def _enforce_claim_limit(
    claims: Sequence[ClaimDraft], *, issues: list[IssueDraft]
) -> tuple[ClaimDraft, ...]:
    """超过条数上限时**截断并记一条疑问**。

    **不静默丢**：`MAX_CLAIMS_PER_VERSION` 被超出说明解构跑偏了
    （把每个句子都当结论）。静默截断会让运营以为"就这么多结论"，
    而记一条疑问至少让"模型产出了 N 条，系统只保留前 M 条"可见。
    """
    if len(claims) <= MAX_CLAIMS_PER_VERSION:
        return tuple(claims)
    issues.append(
        IssueDraft(
            id=uuid.uuid4(),
            field_path="*",
            severity=SEVERITY_WARN,
            description=(
                f"解构产出 {len(claims)} 条结论，超过单版本上限 "
                f"{MAX_CLAIMS_PER_VERSION}，已保留前 {MAX_CLAIMS_PER_VERSION} 条"
                "（条数过多通常说明解构粒度跑偏）。"
            ),
            state=ISSUE_OPEN,
        )
    )
    return tuple(claims[:MAX_CLAIMS_PER_VERSION])


def _write_draft_decomposition(
    repo: BriefRepository,
    *,
    draft: BriefDraft,
    fields: dict[str, Any],
    claims: Sequence[ClaimDraft],
    issues: Sequence[IssueDraft],
    source_file_id: uuid.UUID,
    source_parse_run_id: uuid.UUID,
    source_state: str,
    source_errors: list[Any],
) -> None:
    """把解构结果整体写进草稿（**条件 UPDATE**，走 `lock_version`）。

    用 `update_draft` 而不是 ORM 属性赋值：本表有 `lock_version`，
    而"读出来 → 改属性 → 写回"在并发下会丢更新（两个并发的解构请求
    各写一半）。条件 UPDATE 把"检查"与"写入"压在同一条语句里。
    """
    values: dict[str, Any] = {
        "structured_fields": fields,
        "claims": [claim.to_json() for claim in claims],
        "issue_resolutions": [issue.to_json() for issue in issues],
        "source_file_id": source_file_id,
        "source_parse_run_id": source_parse_run_id,
        "source_state": source_state,
        "source_errors": source_errors,
        # 解构产出的建议与本次输入无关（上一轮加厚的结果），
        # 重新解构时一并清空 —— 留着它们会让运营看到一堆"基于旧版本"的建议。
        "suggestions": [],
        "suggestions_version_id": None,
    }
    updated = repo.update_draft(
        draft_id=draft.id, expected_lock_version=draft.lock_version, values=values
    )
    if updated is None:  # pragma: no cover - 并发路径
        raise _conflict(
            "草稿在解构期间被其他请求修改，请重新读取后再试。",
            reason="draft_concurrently_modified",
            draftId=str(draft.id),
        )
    repo.flush()


def draft_has_unsolidified_edits(
    repo: BriefRepository, *, draft: BriefDraft
) -> bool:
    """草稿里是否有"固化之后才产生"的编辑。

    判据分两种：

      · `base_version_id IS NULL`（还没固化过任何版本）→ 只要草稿不是
        全空（有任何 FILLED 字段、任何 claim、任何已处置疑问）就算有编辑；
      · 否则 → 把草稿内容算成 payload 摘要，与 `base_version_id` 那个
        版本节点的 `payload_hash` 比对，不同即有编辑。

    **第二种判据是"内容摘要"而不是 `lock_version`**：`lock_version`
    每次保存都递增（哪怕改回原值），用它判"有没有编辑"会把
    "改了又改回来"误判成有编辑 —— 于是运营无法重新解构一份
    他刚改回原样的草稿。
    """
    if draft.base_version_id is None:
        return _draft_is_nonempty(draft)

    node = VersionRepository(repo.session).get_node(draft.base_version_id)
    if node is None:
        # 基准版本节点不存在：保守地认为"有编辑"（宁可拒绝重新解构，
        # 也不要覆盖一份来源不明的草稿）。
        return True
    return canonical_payload_hash(_draft_payload(repo, draft=draft)) != node.payload_hash


def _draft_is_nonempty(draft: BriefDraft) -> bool:
    fields = draft.structured_fields or {}
    has_filled = any(
        isinstance(entry, dict) and entry.get("status") == "FILLED"
        for entry in fields.values()
    )
    if has_filled:
        return True
    if draft.claims:
        return True
    return any(
        isinstance(issue, dict) and issue.get("state") in ISSUE_SETTLED_STATES
        for issue in (draft.issue_resolutions or [])
    )


# ---------------------------------------------------------------------------
# 更新草稿
# ---------------------------------------------------------------------------


def update_draft(
    repo: BriefRepository,
    *,
    brief: Brief,
    base_version_id: uuid.UUID | None,
    fields: Any = None,
    claims: Any = None,
    issue_resolutions: Any = None,
    expected_lock_version: int,
    actor_id: uuid.UUID,
) -> BriefDraft:
    """更新草稿（`PUT /projects/{id}/brief-draft`）。

    **三个入参都可选**（`None` = 本次不动这一块）：前端可以只提交
    "我改了 3 个字段"，而不必把 500 条 claims 一起传回来 ——
    全量提交会让"两个运营分别改不同部分"变成互相覆盖。

    **`claims` 只能改 `resolution` 与 `resolutionReason`**，不能新增、
    不能改文本或分类：结论是**文档的产物**，运营的工作是**确认它们**，
    不是发明它们。要补结论就走加厚端点（建议 → 显式采纳）。

    **`issue_resolutions` 只能改已存在的疑问**（按 `id` 匹配）：
    运营不能自己新增一条"我已经解决的疑问"，也不能删除一条 ——
    否则"核心疑问阻断"就退化成"运营说没有疑问"。
    未知 id → 422（不静默忽略：静默忽略会让"我明明答复了"变成一个谜）。
    """
    draft = require_draft(repo, brief=brief)

    values: dict[str, Any] = {}
    if fields is not None:
        values["structured_fields"] = brief_schema.normalize_field_values(fields)
    if claims is not None:
        values["claims"] = _apply_claim_resolutions(
            draft.claims or [], claims
        )
    if issue_resolutions is not None:
        values["issue_resolutions"] = _apply_issue_resolutions(
            draft.issue_resolutions or [], issue_resolutions, actor_id=actor_id
        )

    if base_version_id is not None:
        _require_base_version(repo, brief=brief, base_version_id=base_version_id)
        values["base_version_id"] = base_version_id

    if not values:
        # 空请求：只回读当前草稿（**不递增 lock_version**）。
        # 递增会让"点了保存但什么都没改"凭空产生一次版本冲突，
        # 而前端下一次提交必然 412。
        return draft

    updated = repo.update_draft(
        draft_id=draft.id, expected_lock_version=expected_lock_version, values=values
    )
    if updated is None:
        from rednote.platform.concurrency import version_conflict

        current = repo.get_draft(draft.id)
        raise version_conflict(
            resource_id=draft.id,
            current_version=current.lock_version if current else expected_lock_version,
            reason="lock_version_mismatch",
            message="Brief 草稿已被更新，请重新读取最新版本后再提交。",
        )
    repo.flush()
    refreshed = repo.get_draft(draft.id)
    assert refreshed is not None
    return refreshed


def _require_base_version(
    repo: BriefRepository, *, brief: Brief, base_version_id: uuid.UUID
) -> None:
    """`baseVersionId` 必须是**本项目 Brief 的**版本。

    跨项目引用拒绝（WP11 的核心验收项之一）在依赖边上有实现，
    但这里是一个**独立入口**：`brief_drafts.base_version_id` 是一个
    没有跨项目校验的普通外键，而它的语义是"这次编辑从哪一版开始"。
    允许它指向别的项目，等于让溯源链指向一份看不到的文档。
    """
    node = VersionRepository(repo.session).get_node(base_version_id)
    if node is None:
        raise _validation_error(
            "baseVersionId", "引用的版本不存在。", reason="version_not_found"
        )
    if node.entity_type != ENTITY_TYPE_BRIEF:
        raise _validation_error(
            "baseVersionId",
            "该版本不是 Brief 版本。",
            reason="version_entity_type_mismatch",
            entityType=node.entity_type,
        )
    if node.project_id is not None and node.project_id != brief.project_id:
        raise _validation_error(
            "baseVersionId",
            "不能引用其他项目的 Brief 版本。",
            reason="cross_project_reference",
        )


def _apply_claim_resolutions(
    existing: Sequence[Any], incoming: Sequence[Any]
) -> list[dict[str, Any]]:
    """把客户端提交的确认结果合进既有 claims。

    匹配键是 `(fieldPath, text)`：草稿阶段的 claim 还没有数据库 id
    （它们要到固化时才落 `brief_claims`），而 `(fieldPath, text)` 是
    它们的自然身份 —— 也是 `uq_brief_claims_version_field_hash` 的键
    （`claim_hash` 就是 text 的摘要）。

    客户端提交一条既有列表里没有的 claim → **422**（见 `update_draft`）。
    """
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for item in existing:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("fieldPath") or ""), str(item.get("text") or ""))
        index[key] = dict(item)

    for item in incoming:
        if not isinstance(item, dict):
            raise _validation_error(
                "claims", "claims 的每一项都必须是对象。", reason="claim_not_object"
            )
        key = (str(item.get("fieldPath") or ""), str(item.get("text") or ""))
        target = index.get(key)
        if target is None:
            raise _validation_error(
                "claims",
                f"草稿里没有这条结论（字段 {key[0]!r}）。"
                "运营只能确认已有结论，不能新增 —— 补充结论请走加厚端点。",
                reason="claim_not_in_draft",
                fieldPath=key[0],
            )
        resolution = item.get("resolution")
        if resolution is not None:
            if resolution not in RESOLUTIONS:
                raise _validation_error(
                    "claims",
                    f"未知的确认状态：{resolution!r}。",
                    reason="claim_resolution_invalid",
                )
            target["resolution"] = resolution
        if "resolutionReason" in item:
            target["resolutionReason"] = item.get("resolutionReason")
    return list(index.values())


def _apply_issue_resolutions(
    existing: Sequence[Any], incoming: Sequence[Any], *, actor_id: uuid.UUID
) -> list[dict[str, Any]]:
    """把客户端提交的疑问处置合进既有疑问（按 `id` 匹配）。

    **`resolvedBy` 由服务端填 `actor_id`，不信客户端传的**：
    "谁解决了这条疑问"是审核追溯的关键字段。信客户端传参意味着
    任何人都能把处置归到别人名下，而审计里看不出区别。
    """
    index: dict[str, dict[str, Any]] = {}
    for item in existing:
        if not isinstance(item, dict):
            continue
        raw_id = str(item.get("id") or "")
        if raw_id:
            index[raw_id] = dict(item)

    for item in incoming:
        if not isinstance(item, dict):
            raise _validation_error(
                "issueResolutions",
                "issueResolutions 的每一项都必须是对象。",
                reason="issue_resolution_not_object",
            )
        raw_id = str(item.get("id") or "")
        target = index.get(raw_id)
        if target is None:
            raise _validation_error(
                "issueResolutions",
                f"草稿里没有这条疑问（id={raw_id!r}）。"
                "疑问由解构产生，运营只能处置、不能新增或删除。",
                reason="issue_not_in_draft",
            )
        state = item.get("state")
        if state is not None:
            if state not in ISSUE_STATES:
                raise _validation_error(
                    "issueResolutions",
                    f"未知的疑问状态：{state!r}。",
                    reason="issue_state_invalid",
                )
            target["state"] = state
        resolution = item.get("resolution")
        if resolution is not None:
            target["resolution"] = resolution
        if target.get("state") in ISSUE_SETTLED_STATES:
            if not str(target.get("resolution") or "").strip():
                # 与 `IssueDraft` 的构造校验同一判据，在写入点再报一次
                # （构造校验只在解构时跑；运营的处置走的是这条路径）。
                raise _validation_error(
                    "issueResolutions",
                    "处置疑问必须填写理由（resolution）—— 没有理由的'已解决'"
                    "在下一次审查时无法复核。",
                    reason="issue_resolution_reason_required",
                )
            target["resolvedBy"] = str(actor_id)
        else:
            target.pop("resolvedBy", None)
    return list(index.values())


# ---------------------------------------------------------------------------
# 固化版本
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SolidifiedVersion:
    """一次固化的结果。"""

    version_id: uuid.UUID
    revision_no: int
    etag: str
    product_name: str
    summary: str
    claim_count: int
    issue_count: int
    created: bool


def solidify(
    repo: BriefRepository,
    *,
    brief: Brief,
    actor_id: uuid.UUID,
) -> SolidifiedVersion:
    """把草稿固化成不可变版本（`POST /projects/{id}/brief-versions`）。

    见模块 docstring 第三节的四个环节。**注册表与具体表同一事务**：
    `register_version` 只 `add` 不 `flush`（它的 docstring 明写
    "不在这里 flush"），因此节点行、`brief_versions` 行、
    `brief_claims` / `brief_issues` 行、以及 `briefs.active_version_id`
    的更新全部落在调用方的事务里。

    **`revision_no=None`**（追加到末尾）：一次固化就是一次"发布新版本"，
    没有客户端可提供的幂等键（OP-91 已登记）。但本函数**在固化前**
    用内容摘要挡掉了"内容没变"的重复提交（第 3 条），
    于是"点两次固化按钮"不会产生两个同内容版本 ——
    这是本模块对 OP-91 在**自己这一层**能做的缓解。
    """
    draft = require_draft(repo, brief=brief)

    # --- 环节 1：必填字段已填写或明确不适用 -----------------------------
    missing = brief_schema.missing_required_fields(draft.structured_fields or {})
    if missing:
        raise _validation_error(
            "structuredFields",
            "必填字段尚未填写或明确标记为不适用。",
            reason="required_fields_missing",
            missingFields=list(missing),
            hint="需求 §5.3：必填字段已填写或明确不适用；"
            "确实不适用的字段可标记 NOT_APPLICABLE 并填写原因。",
        )

    # --- 环节 2：没有未答复的核心疑问 -----------------------------------
    blocking = _open_blocking_issues(draft.issue_resolutions or [])
    if blocking:
        raise _conflict(
            "还有未答复的核心疑问，不能固化版本。",
            reason="blocking_issues_open",
            blockingIssues=[
                {"id": item.get("id"), "fieldPath": item.get("fieldPath"),
                 "description": item.get("description")}
                for item in blocking
            ],
        )

    # --- 环节 3：内容真的变了 -------------------------------------------
    payload = _draft_payload(repo, draft=draft)
    if draft.base_version_id is not None:
        node = VersionRepository(repo.session).get_node(draft.base_version_id)
        if node is not None and canonical_payload_hash(payload) == node.payload_hash:
            raise _conflict(
                "草稿内容与基准版本完全相同，无需固化新版本。",
                reason="draft_unchanged",
                baseVersionId=str(draft.base_version_id),
            )

    product_name, model_code, summary = _derive_version_projection(
        fields=draft.structured_fields or {}
    )

    # --- 环节 4：注册表 + 具体表，同一事务 -------------------------------
    registered = register_version(
        VersionRepository(repo.session),
        entity_type=ENTITY_TYPE_BRIEF,
        entity_id=brief.id,
        payload=payload,
        project_id=brief.project_id,
        created_by=actor_id,
        schema_version=PAYLOAD_SCHEMA_VERSION,
    )

    if not registered.created:
        # 幂等命中：同一修订号已有内容摘要一致的节点。
        # 这是**并发或重投**的正常现象（不是错误）—— 直接返回既有版本。
        existing = repo.get_version(registered.node_id)
        if existing is not None:
            return SolidifiedVersion(
                version_id=existing.id,
                revision_no=registered.revision_no,
                etag=registered.etag,
                product_name=existing.product_name,
                summary=existing.summary,
                claim_count=len(repo.list_claims(existing.id)),
                issue_count=len(repo.list_issues(existing.id)),
                created=False,
            )

    claim_rows = _claim_rows(
        draft.claims or [], version_id=registered.node_id, actor_id=actor_id
    )
    issue_rows = _issue_rows(draft.issue_resolutions or [])
    if len(issue_rows) > MAX_ISSUES_PER_VERSION:
        raise _validation_error(
            "issueResolutions",
            f"疑问数 {len(issue_rows)} 超过单版本上限 {MAX_ISSUES_PER_VERSION}。",
            reason="too_many_issues",
        )

    repo.insert_version(
        version_id=registered.node_id,
        brief_id=brief.id,
        source_file_id=draft.source_file_id or _require_source_file(draft),
        source_parse_run_id=draft.source_parse_run_id,
        source_state=draft.source_state or SOURCE_STATE_NONE,
        product_name=product_name,
        model_code=model_code,
        summary=summary,
        structured_fields=dict(draft.structured_fields or {}),
        created_by=actor_id,
    )
    repo.flush()
    repo.insert_claims(version_id=registered.node_id, claims=claim_rows)
    repo.insert_issues(
        version_id=registered.node_id, project_id=brief.project_id, issues=issue_rows
    )
    repo.flush()

    repo.set_active_version(brief_id=brief.id, version_id=registered.node_id)
    # 固化后草稿的基准版本前移，且**清空建议**（它们是针对旧版本的）。
    # 草稿本身保留（协议 §5 末段："正式提交后保留新一轮草稿入口"）。
    updated = repo.update_draft(
        draft_id=draft.id,
        expected_lock_version=draft.lock_version,
        values={
            "base_version_id": registered.node_id,
            "suggestions": [],
            "suggestions_version_id": None,
        },
    )
    if updated is None:  # pragma: no cover - 并发路径
        raise _conflict(
            "草稿在固化期间被其他请求修改，请重新读取后再试。",
            reason="draft_concurrently_modified",
            draftId=str(draft.id),
        )
    repo.flush()

    return SolidifiedVersion(
        version_id=registered.node_id,
        revision_no=registered.revision_no,
        etag=registered.etag,
        product_name=product_name,
        summary=summary,
        claim_count=len(claim_rows),
        issue_count=len(issue_rows),
        created=True,
    )


def _require_source_file(draft: BriefDraft) -> uuid.UUID:
    """固化必须有来源文件（需求 §5.1："保留原文件、文件摘要、上传人、时间"）。

    没有来源文件的 Brief 版本，其所有"原文事实"都没有依据 ——
    而 `brief_versions.source_file_id` 是 NOT NULL，数据库会拦。
    这里给出可读的 422 而不是让一个 `IntegrityError` 变成 500。
    """
    raise _validation_error(
        "sourceFileId",
        "草稿还没有来源文件，不能固化 Brief 版本。"
        "请先上传 Brief 并发起解析（POST /projects/{id}/brief-parse-jobs）。",
        reason="source_file_required",
        draftId=str(draft.id),
    )


def _open_blocking_issues(issues: Sequence[Any]) -> list[dict[str, Any]]:
    """草稿里未答复的 `BLOCK` 疑问。"""
    result: list[dict[str, Any]] = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        if item.get("severity") != SEVERITY_BLOCK:
            continue
        if item.get("state") != ISSUE_OPEN:
            continue
        result.append(item)
    return result


def _derive_version_projection(
    *, fields: Mapping[str, Any]
) -> tuple[str, str | None, str]:
    """从结构化字段导出 `product_name` / `model_code` / `summary` 三个投影列。

    `summary` 的组成（需求 §5.3 完成条件第 2 条）：

    > 核心卖点可形成三项摘要；原材料不足三项时**如实记录**，不虚构卖点凑数。

    因此摘要里显式带上"卖点 N 项"这个数字 —— 它让"只凑到 1 项"
    这件事在版本列表里就看得见，而不必点进去数。
    """
    product_name = _field_text(fields, brief_schema.FIELD_PATH_PRODUCT_NAME)
    model_code = _field_text(fields, brief_schema.FIELD_PATH_MODEL_CODE) or None

    points = brief_schema.selling_point_summary(dict(fields))
    point_text = "；".join(points) if points else "（原文未提供）"
    must_mention = _field_list_length(fields, brief_schema.FIELD_PATH_MUST_MENTION)
    must_not = _field_list_length(fields, brief_schema.FIELD_PATH_MUST_NOT_MENTION)

    summary = (
        f"{product_name}｜卖点 {len(points)}/{MIN_SELLING_POINTS_EXPECTED} 项："
        f"{point_text}｜必须提及 {must_mention} 项｜禁止提及 {must_not} 项"
    )
    return product_name, model_code, summary


def _field_text(fields: Mapping[str, Any], path: str) -> str:
    entry = fields.get(path)
    if not isinstance(entry, dict):
        return ""
    value = entry.get("value")
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "、".join(str(item) for item in value if item)
    if value is None:
        return ""
    return str(value)


def _field_list_length(fields: Mapping[str, Any], path: str) -> int:
    entry = fields.get(path)
    if not isinstance(entry, dict):
        return 0
    value = entry.get("value")
    if isinstance(value, list):
        return len(value)
    return 1 if value else 0


def _claim_rows(
    claims: Sequence[Any], *, version_id: uuid.UUID, actor_id: uuid.UUID
) -> list[dict[str, Any]]:
    """草稿 claims（jsonb）→ `brief_claims` 行参数。

    **`confirmed_by` 由服务端填**（与 `_apply_issue_resolutions` 同一判据）：
    只有 `CONFIRMED` 的结论才记确认人，`PENDING` / `NOT_APPLICABLE` 留空 ——
    数据库 CHECK 也这么要求（`confirmed_requires_actor`）。
    """
    rows: list[dict[str, Any]] = []
    for item in claims:
        if not isinstance(item, dict):
            continue
        field_path = str(item.get("fieldPath") or "")
        text = str(item.get("text") or "")
        category = str(item.get("category") or "")
        resolution = str(item.get("resolution") or RESOLUTION_PENDING)
        evidence = item.get("evidence")
        segment_id = _evidence_segment_id(evidence)
        rows.append(
            {
                "field_path": field_path,
                "claim_text": text,
                "category": category,
                "evidence_segment_id": segment_id,
                "evidence_url": _evidence_url(evidence),
                "applicability": item.get("applicability") or {},
                "resolution_status": resolution,
                "resolution_reason": item.get("resolutionReason"),
                "confirmed_by": actor_id if resolution == RESOLUTION_CONFIRMED else None,
                "claim_hash": claim_hash(field_path=field_path, text=text),
            }
        )
    return rows


def _issue_rows(issues: Sequence[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        resolved_by = item.get("resolvedBy")
        rows.append(
            {
                "field_path": str(item.get("fieldPath") or "*"),
                "severity": str(item.get("severity") or SEVERITY_WARN),
                "description": str(item.get("description") or ""),
                "state": str(item.get("state") or ISSUE_OPEN),
                "resolution": item.get("resolution"),
                "resolved_by": _as_uuid(resolved_by),
            }
        )
    return rows


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def parse_input_hash(*, file_id: uuid.UUID, parser_version: str) -> str:
    """一次 Brief 解析任务的输入摘要（`jobs.input_hash`）。

    **直接复用 WP17 的同名函数**：`jobs` 的幂等判定是
    "同 scope 同 key、同 input_hash → 复用任务；不同 → 409"。
    若本模块自己算一个摘要，那么"事件自动解析"（WP17 的
    `file.uploaded` 处理器，scope=`parse`）与"运营手动发起 Brief 解析"
    （本模块，scope=`brief_parse`）在**同 key 不同摘要**时不会撞 ——
    但那两个 scope 不同，本来就各建一个任务，这条不是问题。

    **真正的理由是"同一个 scope 内的摘要必须稳定"**：
    `brief_parse` scope 下，同一个 `(file_id, parser_version)`
    必须每次算出同一个摘要，否则用户拿同一个 `Idempotency-Key` 重试
    会得到 409 `IDEMPOTENCY_CONFLICT`（而不是复用它刚建的任务）。
    复用 WP17 的实现让这个稳定性只有一处来源。
    """
    from rednote.modules.parsing.service import parse_input_hash as _wp17

    return _wp17(file_id=file_id, parser_version=parser_version)


def enrichment_input_hash(request: EnrichmentRequest) -> str:
    """一次加厚任务的输入摘要（`jobs.input_hash`）。

    **必须包含 `allowedSourceIds`**：它是这个任务**真正的内容**
    —— 同一版本、不同白名单是两次不同的加厚请求（不同的输入 → 不同的
    建议）。只哈希 `briefVersionId` 会让"我先允许来源 A，再改成只允许 B"
    被当成同一次请求，于是第二次的 `Idempotency-Key` 命中第一次的任务，
    返回一份基于 A 的旧建议。

    `canonical_payload_hash` 而不是 `hashlib`：输入是**结构**
    （有序数组参与语义），而它已经处理了"键序不影响摘要"
    （`allowedSourceIds` 的顺序不该影响摘要 —— 它是集合语义）。
    """
    return canonical_payload_hash(
        {
            "briefVersionId": str(request.brief_version_id),
            # 排序让"同一组来源以不同顺序提交"得到同一个摘要。
            "allowedSourceIds": sorted(
                str(item) for item in request.allowed_source_ids
            ),
        }
    )


def claim_hash(*, field_path: str, text: str) -> str:
    """结论摘要（`brief_claims.claim_hash`）。

    输入是 `(field_path, text)` 而不是只有 text —— 因为唯一约束是
    `UQ(brief_version_id, field_path, claim_hash)`，而"同字段同文本"
    才是"重复结论"的判据。只哈希 text 会让两处代码对"重复"的定义
    产生分歧（一处按字段内、一处按全局），而分歧的方向恰好是
    "本该保留的结论被唯一约束挡下"。

    **UTF-8 字节的 SHA-256**，与 WP17 的 `segment_content_hash` 同一算法：
    输入是纯文本而不是 JSON 结构，用 `canonical_payload_hash`
    会多一层引号与转义（且两者会被误认为是同一个口径）。
    """
    import hashlib

    digest = hashlib.sha256(f"{field_path}\x1f{text}".encode("utf-8")).hexdigest()
    if len(digest) != CLAIM_HASH_LENGTH:  # pragma: no cover - 防御性
        raise RuntimeError("claim 摘要长度异常，检查哈希实现")
    return digest


def _evidence_segment_id(evidence: Any) -> uuid.UUID | None:
    for entry in _evidence_entries(evidence):
        return _as_uuid(entry.get("segmentId"))
    return None


def _evidence_url(evidence: Any) -> str | None:
    for entry in _evidence_entries(evidence):
        raw = entry.get("url")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _evidence_entries(evidence: Any) -> list[dict[str, Any]]:
    if isinstance(evidence, dict):
        return [evidence]
    if isinstance(evidence, list):
        return [item for item in evidence if isinstance(item, dict)]
    return []


# ---------------------------------------------------------------------------
# payload（版本摘要的输入）—— 必须**确定性**
# ---------------------------------------------------------------------------


def _draft_payload(repo: BriefRepository, *, draft: BriefDraft) -> dict[str, Any]:
    """草稿 → 版本 payload。

    **这个形状是版本摘要的输入**，因此它必须**确定性**：
    同一份草稿任何时候算出的摘要必须相同，否则
    `draft_unchanged`（环节 3）与幂等固化都会失效。
    两条确定性来源：
      · claims / issues 按固定键排序（jsonb 数组的**顺序**会进摘要）；
      · 只取参与语义的字段（`updated_at` / `lock_version` 不进去 ——
        它们每次都变，进去会让"内容没变"永远判不出来）。
    """
    claims = sorted(
        (item for item in (draft.claims or []) if isinstance(item, dict)),
        key=lambda item: (
            str(item.get("fieldPath") or ""),
            claim_hash(
                field_path=str(item.get("fieldPath") or ""),
                text=str(item.get("text") or ""),
            ),
        ),
    )
    issues = sorted(
        (item for item in (draft.issue_resolutions or []) if isinstance(item, dict)),
        key=lambda item: (
            str(item.get("severity") or ""),
            str(item.get("fieldPath") or ""),
            str(item.get("id") or ""),
        ),
    )
    fields = draft.structured_fields or {}
    normalized_fields = {
        path: _payload_field_entry(fields.get(path)) for path in sorted(fields)
    }
    return {
        "schemaVersion": PAYLOAD_SCHEMA_VERSION,
        "briefId": str(draft.brief_id),
        "sourceFileId": str(draft.source_file_id) if draft.source_file_id else None,
        "sourceParseRunId": (
            str(draft.source_parse_run_id) if draft.source_parse_run_id else None
        ),
        "sourceState": draft.source_state or SOURCE_STATE_NONE,
        "structuredFields": normalized_fields,
        "claims": [_payload_claim(item) for item in claims],
        "issues": [_payload_issue(item) for item in issues],
    }


def _payload_field_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {"status": "MISSING", "value": None}
    return {"status": entry.get("status"), "value": entry.get("value")}


def _payload_claim(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "fieldPath": str(item.get("fieldPath") or ""),
        "text": str(item.get("text") or ""),
        "category": str(item.get("category") or ""),
        "applicability": item.get("applicability") or {},
        "evidence": _evidence_entries(item.get("evidence")) or None,
        "resolution": str(item.get("resolution") or RESOLUTION_PENDING),
        "resolutionReason": item.get("resolutionReason"),
    }


def _payload_issue(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "fieldPath": str(item.get("fieldPath") or "*"),
        "severity": str(item.get("severity") or SEVERITY_WARN),
        "description": str(item.get("description") or ""),
        "state": str(item.get("state") or ISSUE_OPEN),
        "resolution": item.get("resolution"),
    }


# ---------------------------------------------------------------------------
# 加厚（**建议不自动生效**）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnrichmentOutcome:
    """一次加厚落到草稿上的结果。"""

    draft_id: uuid.UUID
    suggestion_count: int
    version_id: uuid.UUID
    #: 被白名单挡掉的建议数（`allowedSourceIds` 的强制点，见 `decomposer`）。
    rejected_count: int


def request_enrichment(
    repo: BriefRepository,
    *,
    brief: Brief,
    brief_version_id: uuid.UUID,
    allowed_source_ids: Sequence[uuid.UUID],
) -> tuple[BriefVersion, EnrichmentRequest]:
    """校验一次加厚请求并**把它登记到草稿上**（`POST /projects/{id}/brief-enrichment-jobs`）。

    三个判据：
      1. 版本存在且属于本项目（否则 422 —— 它出现在请求体里）；
      2. `allowedSourceIds` **非空**（否则 422）。
         空白名单意味着"允许引用零个来源"，而加厚的价值就在于
         "基于可识别的来源提出建议"（需求 §5.2 的外部证据定义）。
         允许空会让模型只能编造，而那正是需求明令禁止的；
      3. 草稿存在（加厚建议要落在草稿上，见下）。

    **登记（写 `enrichment_request`）与入队必须同事务**：
    入队是一个 DB 写（`jobs` + `outbox_events`），登记是另一个。
    若登记失败而入队成功，Worker 会领到一个"没有请求参数"的任务 ——
    那正是本模块用 `ERROR_ENRICHMENT_REQUEST_MISSING` 兜的失败，
    但它本不该在正常流程里出现。同事务让这两件事一起成立或一起不成立。
    """
    version = repo.get_version(brief_version_id)
    if version is None:
        raise _validation_error(
            "briefVersionId", "Brief 版本不存在。", reason="brief_version_not_found"
        )
    if version.brief_id != brief.id:
        raise _validation_error(
            "briefVersionId",
            "该版本不属于本项目。",
            reason="brief_version_project_mismatch",
        )
    if not allowed_source_ids:
        raise _validation_error(
            "allowedSourceIds",
            "allowedSourceIds 不能为空 —— 加厚建议必须基于可识别的来源，"
            "空白名单只会让模型编造内容（需求 §5.2）。",
            reason="allowed_sources_empty",
        )
    draft = require_draft(repo, brief=brief)
    request = EnrichmentRequest(
        brief_id=brief.id,
        brief_version_id=brief_version_id,
        allowed_source_ids=tuple(allowed_source_ids),
    )
    updated = repo.update_draft(
        draft_id=draft.id,
        expected_lock_version=draft.lock_version,
        values={"enrichment_request": request.to_json()},
    )
    if updated is None:  # pragma: no cover - 并发路径
        raise _conflict(
            "草稿在登记加厚请求期间被其他请求修改，请重新读取后再试。",
            reason="draft_concurrently_modified",
            draftId=str(draft.id),
        )
    repo.flush()
    return version, request


@dataclass(frozen=True)
class EnrichmentRequest:
    """一次待处理的加厚请求（落在 `brief_drafts.enrichment_request` 上）。

    **它为什么是一等对象而不是一个裸 dict**：两个字段里有一个是
    `uuid.UUID` 数组。裸 dict 会让"读出来的是字符串还是 UUID"
    取决于它经过了哪条路径（jsonb 里只有字符串），而处理器需要的是
    `uuid.UUID`（要拿去和 `SourceSegment.id` 比）。把这次转换收在一个
    构造点里，就不必在每个消费方各写一遍 `uuid.UUID(...)` ——
    那种重复的失败模式是"某一处漏转换"，表现为**白名单永远匹配不上**，
    于是加厚悄悄产出零条建议（不报错）。
    """

    brief_id: uuid.UUID
    brief_version_id: uuid.UUID
    allowed_source_ids: tuple[uuid.UUID, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "briefId": str(self.brief_id),
            "briefVersionId": str(self.brief_version_id),
            "allowedSourceIds": [str(item) for item in self.allowed_source_ids],
        }

    @classmethod
    def from_json(cls, raw: Any) -> EnrichmentRequest | None:
        """从 jsonb 读回。**读不懂返回 `None`** —— 不抛错。

        这条路径的调用方是任务处理器：一个畸形的请求应当让任务
        以"请求缺失"失败（不可重试），而不是抛一个未捕获异常
        （那会被兜底处理器折成 500，任务卡在 RUNNING 直到租约过期）。
        """
        if not isinstance(raw, dict):
            return None
        brief_id = _as_uuid(raw.get("briefId"))
        version_id = _as_uuid(raw.get("briefVersionId"))
        if brief_id is None or version_id is None:
            return None
        raw_ids = raw.get("allowedSourceIds")
        allowed: list[uuid.UUID] = []
        if isinstance(raw_ids, list):
            for item in raw_ids:
                parsed = _as_uuid(item)
                if parsed is not None:
                    allowed.append(parsed)
        if not allowed:
            return None
        return cls(
            brief_id=brief_id,
            brief_version_id=version_id,
            allowed_source_ids=tuple(allowed),
        )


def read_enrichment_request(
    repo: BriefRepository, *, project_id: uuid.UUID
) -> EnrichmentRequest | None:
    """读出**本项目**待处理的加厚请求并**清空它**（处理器入口）。

    **`project_id` 是必填的**：加厚请求存在草稿行上，而草稿行本身不带
    项目（项目在 `briefs` 上）。若不带项目过滤，两个项目同时有待处理
    请求时，A 项目的 Worker 会消费掉 B 项目的请求 —— 表现为
    "B 的任务报请求缺失、A 的任务拿着 B 的来源跑"。
    这个参数由 `handlers.brief_enrich_handler` 从 job 上取。

    **清空是必须的**：请求是一次性的。留着它，下一轮任务（或一次
    重复投递）会读到同一份参数并**再跑一次加厚** —— 而加厚要花一次
    模型调用。清空让"这个请求已被消费"成为一个可观察的事实。

    清空与读取在同一个 `update_draft` 里完成（条件 UPDATE），
    因此两个并发的 attempt 只有一个能读到非空请求 ——
    另一个读到 `None` 并报"请求缺失"（不可重试），这正是想要的。
    """
    drafts = repo.list_drafts_with_pending_enrichment(
        project_id=project_id, limit=1
    )
    if not drafts:
        return None
    row = drafts[0]
    request = EnrichmentRequest.from_json(row.enrichment_request)
    updated = repo.update_draft(
        draft_id=row.id,
        expected_lock_version=row.lock_version,
        values={"enrichment_request": None},
    )
    if updated is None:
        # 并发：另一个 attempt 抢先消费了。返回 `None` 让本 attempt
        # 以"请求缺失"结束（不可重试）—— 不报错，因为这是正常竞态。
        return None
    repo.flush()
    return request


def execute_enrichment(
    repo: BriefRepository,
    *,
    brief: Brief,
    brief_version_id: uuid.UUID,
    allowed_source_ids: Sequence[uuid.UUID],
    segments: Sequence[Any],
    decomposer: BriefDecomposer,
    model_profile_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    attempt_id: uuid.UUID | None = None,
) -> EnrichmentOutcome:
    """执行加厚并把建议写进草稿（**任务处理器调用**）。

    **建议写 `brief_drafts.suggestions`，不写 `claims`** ——
    协议 §5 第 5 行原文"返回分类建议，**不自动生效**"。
    运营要采纳，是在 `PUT /brief-draft` 里显式把它加进 `claims`，
    而那一步会走 `ClaimDraft` 的校验（事实类必须有来源）。
    于是"建议变成事实"这件事永远要经过一次显式确认。

    **`allowed_source_ids` 在这里被强制**：只把白名单内的片段交给模型
    （`allowed_segments`），且模型返回白名单外的 `segmentId` 时**丢弃**
    （`decomposer._read_suggestion`）。两处都要 —— 只做前者，
    一个被污染的提示词仍能让模型引用一个它没见过的 id。
    """
    version = repo.get_version(brief_version_id)
    if version is None:  # pragma: no cover - api 层已校验
        raise _validation_error(
            "briefVersionId", "Brief 版本不存在。", reason="brief_version_not_found"
        )

    allowed = set(allowed_source_ids)
    allowed_segments = tuple(
        segment for segment in segments if getattr(segment, "id", None) in allowed
    )
    draft = require_draft(repo, brief=brief)

    result = decomposer.enrich(
        project_id=brief.project_id,
        fields=dict(draft.structured_fields or {}),
        claims=tuple(draft.claims or ()),
        allowed_segments=allowed_segments,
        model_profile_id=model_profile_id,
        job_id=job_id,
        attempt_id=attempt_id,
    )

    items = [suggestion.to_json() for suggestion in result.suggestions]
    rejected = len(result.suggestions) - len(items)
    updated = repo.update_draft(
        draft_id=draft.id,
        expected_lock_version=draft.lock_version,
        values={
            "suggestions": items,
            "suggestions_version_id": brief_version_id,
        },
    )
    if updated is None:  # pragma: no cover - 并发路径
        raise _conflict(
            "草稿在加厚期间被其他请求修改，请重新读取后再试。",
            reason="draft_concurrently_modified",
            draftId=str(draft.id),
        )
    repo.flush()

    return EnrichmentOutcome(
        draft_id=draft.id,
        suggestion_count=len(items),
        version_id=brief_version_id,
        rejected_count=rejected,
    )


# ---------------------------------------------------------------------------
# 读取（端点用）
# ---------------------------------------------------------------------------


def get_view(
    repo: BriefRepository,
    *,
    brief: Brief,
    version_id: uuid.UUID | None = None,
    segments_by_id: Mapping[uuid.UUID, Any] | None = None,
) -> dict[str, Any]:
    """组装 `GET /projects/{id}/brief` 的响应（协议 §5 第 2 行）。

    协议原文："BriefView：**原文链接、字段、claims、issues、版本**"。

    四块都在，另加两块本包认为必需的：

    | 块 | 内容 | 为什么必需 |
    |---|---|---|
    | `source` | 原文件 id / 名字 / 解析运行 id | "原文链接"的落点：前端用 WP12 已有的 `POST /files/{id}/download-tickets` 拿短期票据 |
    | `evidenceSegments` | 本版 claims 引用到的片段文本与定位 | **原文对照审核**的最小充分集：claims 只带 `segmentId`，没有文本就对照不了 |
    | `draft` | 草稿（含 ETag） | 协议 §5 末段：GET 返回草稿 ETag，首次 PUT 就用它 |
    | `version` | 指定或当前生效版本（含不可变 ETag） | — |
    | `versions` | 版本历史（摘要） | 前端要能切版本 |
    | `blockingIssueCount` | 未处置的核心疑问数 | 步骤 1 能否推进的判据 |

    **`evidenceSegments` 只返回被引用的片段**，不返回整个解析结果：
    一份 200 页 PDF 有上万个片段，全量返回会让响应体失控，
    而"原文对照"真正需要的是**这条结论对应的那一段**。
    全文浏览是另一个界面（需要一个协议里没有的端点，已登记为待定项）。
    """
    draft = require_draft(repo, brief=brief)

    version = (
        repo.get_version(version_id)
        if version_id is not None
        else _active_version(repo, brief=brief)
    )
    if version_id is not None and (
        version is None or version.brief_id != brief.id
    ):
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "Brief 版本不存在。",
            details={"versionId": str(version_id)},
        )

    claims = repo.list_claims(version.id) if version is not None else []
    issues = repo.list_issues(version.id) if version is not None else []

    return {
        "briefId": str(brief.id),
        "projectId": str(brief.project_id),
        "activeVersionId": (
            str(brief.active_version_id) if brief.active_version_id else None
        ),
        "draft": _draft_view(draft),
        "version": (
            _version_view(
                repo,
                version=version,
                claims=claims,
                issues=issues,
                segments_by_id=segments_by_id or {},
            )
            if version is not None
            else None
        ),
        "versions": _version_summaries(repo, brief=brief, limit=20),
        "blockingIssueCount": len(
            repo.list_blocking_issues(project_id=brief.project_id)
        ),
    }


def _active_version(repo: BriefRepository, *, brief: Brief) -> BriefVersion | None:
    """当前生效版本。

    **优先按 `briefs.active_version_id` 取**（它是权威），
    缺失时才退回"最新版本" —— 两者在正常数据下一致，
    但"最新"在并发固化时可能指向一个刚提交但还没被设成 active 的版本。
    """
    if brief.active_version_id is not None:
        version = repo.get_version(brief.active_version_id)
        if version is not None:
            return version
    return repo.latest_version(brief.id)


def draft_view(draft: BriefDraft) -> dict[str, Any]:
    """草稿的对外形状（`PUT /brief-draft` 的响应体用它）。

    **公开一个包装而不是让 api 调 `_draft_view`**：下划线前缀在本项目里
    是"模块内部"的约定。让 api 层跨模块调私有函数，等于把
    "它将来可以随意改名/改形状"这条承诺作废 —— 而 api 层是它的**消费者**，
    消费者需要的是一个稳定的名字。
    """
    return _draft_view(draft)


def _draft_view(draft: BriefDraft) -> dict[str, Any]:
    return {
        "draftId": str(draft.id),
        "baseVersionId": (
            str(draft.base_version_id) if draft.base_version_id else None
        ),
        "lockVersion": draft.lock_version,
        "etag": draft_etag(draft),
        "structuredFields": dict(draft.structured_fields or {}),
        "claims": list(draft.claims or []),
        "issueResolutions": list(draft.issue_resolutions or []),
        "suggestions": list(draft.suggestions or []),
        "suggestionsVersionId": (
            str(draft.suggestions_version_id)
            if draft.suggestions_version_id
            else None
        ),
        "sourceFileId": (
            str(draft.source_file_id) if draft.source_file_id else None
        ),
        "sourceParseRunId": (
            str(draft.source_parse_run_id) if draft.source_parse_run_id else None
        ),
        "sourceState": draft.source_state,
        "sourceErrors": list(draft.source_errors or []),
        "schemaVersion": draft.schema_version,
        "updatedAt": draft.updated_at,
    }


def _version_view(
    repo: BriefRepository,
    *,
    version: BriefVersion,
    claims: Sequence[BriefClaim],
    issues: Sequence[BriefIssue],
    segments_by_id: Mapping[uuid.UUID, Any],
) -> dict[str, Any]:
    """版本视图。

    **`etag` 从 `version_nodes` 取**（`node.etag`），不是本模块重算 ——
    理由见 `_version_view` 上方"不提供 `version_etag`"的说明。
    """
    node = VersionRepository(repo.session).get_node(version.id)
    return {
        "versionId": str(version.id),
        "revisionNo": node.revision_no if node is not None else None,
        "etag": node.etag if node is not None else None,
        "productName": version.product_name,
        "modelCode": version.model_code,
        "summary": version.summary,
        "sourceFileId": str(version.source_file_id),
        "sourceParseRunId": (
            str(version.source_parse_run_id)
            if version.source_parse_run_id
            else None
        ),
        "sourceState": version.source_state,
        "structuredFields": dict(version.structured_fields or {}),
        "claims": [_claim_view(claim) for claim in claims],
        "issues": [_issue_view(issue) for issue in issues],
        "evidenceSegments": _evidence_segments(
            claims=claims, segments_by_id=segments_by_id
        ),
        "schemaVersion": version.schema_version,
        "createdAt": version.created_at,
        "createdBy": str(version.created_by) if version.created_by else None,
    }


def _claim_view(claim: BriefClaim) -> dict[str, Any]:
    """`BriefClaim` → 协议 §5.1 的形状。

    **列名映射在这一处**：数据结构 02 §6 用 `claim_text` / `resolution_status`，
    协议 §5.1 用 `text` / `resolution`。两个名字各有一份来源，
    而映射只应有一处 —— 散在多处会让某个字段在某条路径上漏映射，
    前端只是"这个字段是 undefined"。
    """
    return {
        "id": str(claim.id),
        "fieldPath": claim.field_path,
        "text": claim.claim_text,
        "category": claim.category,
        "applicability": dict(claim.applicability or {}),
        "evidence": _claim_evidence(claim),
        "resolution": claim.resolution_status,
        "resolutionReason": claim.resolution_reason,
    }


def _claim_evidence(claim: BriefClaim) -> list[dict[str, Any]]:
    """协议 §5.1 的 `evidence` 是数组；本模块存的是单值（见 `ClaimDraft`）。

    因此这里**组装成单元素数组**（或空数组）。这条不一致已登记为待定项。
    """
    evidence: list[dict[str, Any]] = []
    if claim.evidence_segment_id is not None:
        evidence.append({"segmentId": str(claim.evidence_segment_id)})
    if claim.evidence_url:
        evidence.append({"url": claim.evidence_url})
    return evidence


def _issue_view(issue: BriefIssue) -> dict[str, Any]:
    return {
        "id": str(issue.id),
        "fieldPath": issue.field_path,
        "severity": issue.severity,
        "description": issue.description,
        "state": issue.state,
        "resolution": issue.resolution,
        "resolvedBy": str(issue.resolved_by) if issue.resolved_by else None,
    }


def _evidence_segments(
    *, claims: Sequence[BriefClaim], segments_by_id: Mapping[uuid.UUID, Any]
) -> list[dict[str, Any]]:
    """本版 claims 引用到的片段（**原文对照审核**的最小充分集）。

    `segments_by_id` 由 api 层用 WP17 的 `ParseRepository.list_segments`
    构造（`{segment.id: segment}`）—— 本模块**不直接 import parsing 的仓库**
    （那会让"谁依赖谁"变成双向）。

    **只返回被引用的片段**：一份 200 页 PDF 有上万个片段，全量返回会让
    响应体失控，而"原文对照"真正需要的是**这条结论对应的那一段**。
    全文浏览是另一个界面（需要一个协议里没有的端点，已登记为待定项）。

    **顺序按 claims 的引用顺序去重**，不按片段 id 排序：
    前端按 claims 顺序渲染对照面板，顺序一致才能做"点结论跳到对应原文"。
    """
    seen: set[uuid.UUID] = set()
    result: list[dict[str, Any]] = []
    for claim in claims:
        segment_id = claim.evidence_segment_id
        if segment_id is None or segment_id in seen:
            continue
        seen.add(segment_id)
        segment = segments_by_id.get(segment_id)
        if segment is None:
            # 片段查不到：**仍然返回一个占位**而不是静默跳过 ——
            # "这条结论引用了一个查不到的片段"正是需要被看到的事实
            # （触发器保证正常数据下不会发生，所以它出现就意味着异常）。
            result.append(
                {
                    "segmentId": str(segment_id),
                    "text": None,
                    "position": None,
                    "extractionKind": None,
                    "missing": True,
                }
            )
            continue
        result.append(
            {
                "segmentId": str(segment.id),
                "text": segment.text_content,
                "position": dict(segment.position or {}),
                "extractionKind": segment.extraction_kind,
                "missing": False,
            }
        )
    return result


def _version_summaries(
    repo: BriefRepository, *, brief: Brief, limit: int
) -> list[dict[str, Any]]:
    """版本历史摘要（最新在前）。**不带 claims / issues** ——
    历史列表的用途是"切到某一版"，正文由 `versionId` 再取一次。
    """
    rows = repo.list_versions(brief.id, limit=limit)
    version_repo = VersionRepository(repo.session)
    summaries: list[dict[str, Any]] = []
    for row in rows[:limit]:
        node = version_repo.get_node(row.id)
        summaries.append(
            {
                "versionId": str(row.id),
                "revisionNo": node.revision_no if node is not None else None,
                "productName": row.product_name,
                "summary": row.summary,
                "sourceState": row.source_state,
                "etag": node.etag if node is not None else None,
                "isActive": brief.active_version_id == row.id,
                "createdAt": row.created_at,
            }
        )
    return summaries


__all__ = [
    "PAYLOAD_SCHEMA_VERSION",
    "DecompositionOutcome",
    "EnrichmentOutcome",
    "EnrichmentRequest",
    "SolidifiedVersion",
    "claim_hash",
    "draft_etag",
    "draft_has_unsolidified_edits",
    "draft_view",
    "enrichment_input_hash",
    "ensure_brief",
    "execute_decomposition",
    "execute_enrichment",
    "get_view",
    "parse_input_hash",
    "read_enrichment_request",
    "request_enrichment",
    "request_parse",
    "require_brief_file",
    "require_draft",
    "solidify",
    "update_draft",
]
