"""WP30 请求体 / 响应体模型（协议 §1 的 camelCase 外部契约）。

## 为什么请求体用逐字段 `alias` 而不是全局 `alias_generator`

本仓的字段命名约定是**逐字段显式 alias**（`projects/api.py`、`content/api.py`、
`feedback/schemas.py` 同款），不是全局转换器。理由不是风格：全局转换器会把
**每一个**字段都改名，包括契约里本来就该与列名一致的键；一旦某天某个键
**不该**转换，就得为它写一份例外表 —— 而例外表与转换器共同决定输出，
漂移时谁也说不清是哪个生效的。

## 请求体为什么**不做**取值域校验

`TRIGGER_KINDS` 等受控取值的校验在 `service.py` 里（失败给 422 +
`details.reason` 机器可判 + `details.field` 定位），那是前端与断言共同依赖的
形状。若这里再用 `Literal[...]` 声明一遍：

1. 取值域就有了**两处**定义，服务层加一个取值而模型没加 ⇒ 请求在进
   `service` 之前被 pydantic 拦下，错误体形状变成 `HTTPValidationError`
   （协议 §1.2 之外）；
2. 同一个错误会按"哪一个先触发"给出两种不同的 `reason`。

因此请求体只做**类型**约束，取值域与边界全部交给服务层。

## 响应体为什么是显式模型而不是 `dict`

`response_model` 是 FastAPI 的**输出过滤器**：声明里没有的键会被静默剥掉。
本包把 `service.py` 返回的 dataclass 的可见字段逐条列出，形状以本文件为
唯一契约。

⚠️ `evidence` 用 `dict[str, Any]`：它的键随规则而不同（实测值 / 期望值 /
引用 id），这正是它存在的意义。为每条规则声明一个 evidence 模型会让
"加一条规则"变成"改一次契约"，而 `jsonb_typeof(evidence) = 'object'`
这条 CHECK 已经守住了它的**形状**（必须是对象，不是数组或标量）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _RequestBody(BaseModel):
    """请求体的共同配置：接受字段名与 alias 两种写法，**多余键即拒**。

    `extra="forbid"` 是刻意的（OP-156）：前端把 `contentId` 之类的路径参数
    误塞进 body 时，静默忽略会让调用方以为它生效了。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class _ResponseBody(BaseModel):
    """响应体的共同配置：序列化时输出 alias（camelCase）。"""

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# 一、请求体
# ---------------------------------------------------------------------------


class SelfCheckCreateRequestBody(_RequestBody):
    """`POST /contents/{contentId}/self-checks` 的请求体 —— **空体**。

    ## 为什么一个字段都没有

    | 候选字段 | 为什么不要 |
    |---|---|
    | `subjectVersionId` | 自检判的**永远是当前版本**。允许客户端指定版本，就得回答"判一个旧版的结论要不要影响审核闸"——而审核闸只关心当前版 |
    | `ruleCodes[]`（只跑某几条） | "8 类全绿"与"只跑了 2 条且都绿"在响应里会长得一样（`outcome=PASSED`），而那正是本包要防的假绿 |
    | `triggerKind` | 由端点固定：单条端点记 `MANUAL`、批量端点记 `BATCH`。让调用方自报会让"这一轮是谁触发的"失真 |
    | `ruleSetVersion` | 是服务端常量的落库快照，不是请求参数 |

    保留一个空的请求体模型（而不是干脆不声明）是为了 `extra="forbid"`：
    传任何未知字段都必须 **422**，而不是被静默忽略 —— 否则
    "我传了 `ruleCodes` 怎么没生效"会变成一次排障。
    """


class SelfCheckBatchRequestBody(_RequestBody):
    """`POST /projects/{projectId}/self-checks` 的请求体（批量）。

    **必须显式给 `contentIds`**，没有"不传就是全项目"的默认值：
    一个打错的请求会变成全项目扫描 + 每条内容一轮自检 + N 条 AI 反馈，
    而那是不可撤销的写入（三张表不可变）。

    **结果逐条返回、不整批失败** ⇒ 端点返回 200 而不是 201。
    """

    content_item_ids: list[uuid.UUID] = Field(alias="contentIds")


# ---------------------------------------------------------------------------
# 二、响应体：一轮摘要
# ---------------------------------------------------------------------------


class SelfCheckRunSummaryBody(_ResponseBody):
    """一轮自检的摘要（`service.RunSummary.to_dict()` 的逐字段镜像）。

    **五个计数全部出现在响应里**，缺一不可：
    `blockingCount` / `warningCount` / `infoCount` 回答"命中了多少"，
    `evaluatedCount` / `skippedCount` / `erroredCount` 回答"判了几条、
    **没判几条**、几条没跑成"。只给前三个会让「5 类判了 + 3 类没判」
    被渲染成「8 类全通过」——而原型的批量通过按钮正是以"已完成自检、
    无阻断问题"为前提（假绿直接放行一批内容）。

    `subjectState` 是**读时判定**的 `CURRENT` / `STALE`（不落库）。
    """

    run_id: uuid.UUID = Field(alias="runId")
    project_id: uuid.UUID = Field(alias="projectId")
    content_item_id: uuid.UUID = Field(alias="contentItemId")
    subject_version_id: uuid.UUID = Field(alias="subjectVersionId")
    subject_state: str = Field(alias="subjectState")
    rule_set_version: int = Field(alias="ruleSetVersion")
    trigger_kind: str = Field(alias="triggerKind")
    outcome: str
    blocking_count: int = Field(alias="blockingCount")
    warning_count: int = Field(alias="warningCount")
    info_count: int = Field(alias="infoCount")
    evaluated_count: int = Field(alias="evaluatedCount")
    skipped_count: int = Field(alias="skippedCount")
    errored_count: int = Field(alias="erroredCount")
    started_at: datetime = Field(alias="startedAt")
    finished_at: datetime = Field(alias="finishedAt")
    created_by: uuid.UUID | None = Field(default=None, alias="createdBy")
    created_at: datetime = Field(alias="createdAt")


# ---------------------------------------------------------------------------
# 三、响应体：分类聚合 / 逐规则 / 逐命中
# ---------------------------------------------------------------------------


class SelfCheckCategoryBody(_ResponseBody):
    """一个自检分类的聚合（原型的一张卡片）。

    **只有计数，没有一个派生的 `status` 字段** —— 见
    `service.CategorySummary` 的说明：那会是"这一类算通过吗"的第二处定义。
    """

    category: str
    rule_count: int = Field(alias="ruleCount")
    passed_count: int = Field(alias="passedCount")
    failed_count: int = Field(alias="failedCount")
    skipped_count: int = Field(alias="skippedCount")
    errored_count: int = Field(alias="erroredCount")
    blocking_count: int = Field(alias="blockingCount")
    warning_count: int = Field(alias="warningCount")
    info_count: int = Field(alias="infoCount")


class SelfCheckRuleResultBody(_ResponseBody):
    """一条规则在本轮的结论。

    `skipReason` **必须出现在响应里**：它是"为什么没判"的机器可判答案
    （受控取值，不是自由文本）。少了它，前端只能把"未判"画成一个
    没有解释的灰块，而运营会按"大概没问题"理解。
    """

    rule_result_id: uuid.UUID = Field(alias="ruleResultId")
    rule_code: str = Field(alias="ruleCode")
    category: str
    rule_kind: str = Field(alias="ruleKind")
    status: str
    severity: str | None = None
    skip_reason: str | None = Field(default=None, alias="skipReason")
    error_note: str | None = Field(default=None, alias="errorNote")
    finding_count: int = Field(alias="findingCount")


class SelfCheckFindingBody(_ResponseBody):
    """一条命中。锚点三选一；`feedbackItemId` 仅阻断项非空。"""

    finding_id: uuid.UUID = Field(alias="findingId")
    rule_code: str = Field(alias="ruleCode")
    anchor_kind: str = Field(alias="anchorKind")
    paragraph_ref: str | None = Field(default=None, alias="paragraphRef")
    media_item_id: uuid.UUID | None = Field(default=None, alias="mediaItemId")
    message: str
    #: 机器可判的事实。键随规则而不同（见模块 docstring）。
    evidence: dict[str, Any]
    #: 这条发现落成了哪条 AI 反馈（WP29 的审核闸据此拒绝"通过"）。
    feedback_item_id: uuid.UUID | None = Field(
        default=None, alias="feedbackItemId"
    )


class SelfCheckRunDetailBody(SelfCheckRunSummaryBody):
    """单轮详情 = 摘要 + 8 类聚合 + 逐规则结果 + findings。

    继承摘要而不是嵌套一个 `summary` 对象：前端的列表页与详情页读的是
    同一组字段，嵌套会让两个页面的取值路径不同（`run.outcome` vs
    `run.summary.outcome`），而那种不同迟早被写成两份解析代码。
    """

    categories: list[SelfCheckCategoryBody]
    rule_results: list[SelfCheckRuleResultBody] = Field(alias="ruleResults")
    findings: list[SelfCheckFindingBody]


# ---------------------------------------------------------------------------
# 四、响应体：批量 / 看板 / 规则目录
# ---------------------------------------------------------------------------


class SelfCheckBatchItemBody(_ResponseBody):
    """批量自检的**逐条**结果（一条失败不影响其余条目）。

    `ok = False` 时 `error` 是**人类可读的诊断串**，不是协议错误码 ——
    这是服务层的既有契约（与 `feedback.service.decide_reviews_batch` 同款）。
    要机器可判的 `reason` 请走单条端点。
    """

    content_item_id: uuid.UUID = Field(alias="contentItemId")
    ok: bool
    run: SelfCheckRunSummaryBody | None = None
    error: str | None = None


class SelfCheckBatchResponseBody(_ResponseBody):
    """批量自检的整体响应：`{"results": [...]}`。"""

    results: list[SelfCheckBatchItemBody]


class SelfCheckBoardResponseBody(_ResponseBody):
    """项目看板：每条内容**最新一轮**的摘要。

    **没有自检过的内容不在 `runs` 里** —— 前端把内容列表左连接本结果后
    缺失的那些渲染成「未自检」。本端点不为未自检的内容编造一行
    （那会让"未自检"与"自检过"在数据结构上同形）。
    """

    runs: list[SelfCheckRunSummaryBody]


class SelfCheckRuleBody(_ResponseBody):
    """规则目录里的一条。

    `implemented = False` 的三条（`TONE` / `DUPLICATE` / `SCENE`）**照常列出**：
    前端据此把那三张卡片画成"未判 · 原因"，而不是从"一直没有命中"
    猜成"通过"。
    """

    code: str
    category: str
    rule_kind: str = Field(alias="ruleKind")
    #: 恒 SKIPPED 的规则为 `null`（它永远不会 FAILED）。
    default_severity: str | None = Field(default=None, alias="defaultSeverity")
    anchor_kind: str = Field(alias="anchorKind")
    implemented: bool
    summary: str


class SelfCheckRuleCatalogResponseBody(_ResponseBody):
    """规则目录（代码注册表的只读投影）+ 当前规则集版本。"""

    rule_set_version: int = Field(alias="ruleSetVersion")
    rules: list[SelfCheckRuleBody]


__all__ = [
    "SelfCheckBatchItemBody",
    "SelfCheckBatchRequestBody",
    "SelfCheckBatchResponseBody",
    "SelfCheckBoardResponseBody",
    "SelfCheckCategoryBody",
    "SelfCheckCreateRequestBody",
    "SelfCheckFindingBody",
    "SelfCheckRuleBody",
    "SelfCheckRuleCatalogResponseBody",
    "SelfCheckRuleResultBody",
    "SelfCheckRunDetailBody",
    "SelfCheckRunSummaryBody",
]
