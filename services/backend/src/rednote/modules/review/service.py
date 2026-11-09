"""WP30 自检服务层：6 个用例 + 一次固定顺序的自检执行。

## 六个用例

| 用例 | 函数 | 要点 |
|---|---|---|
| 跑一轮自检 | `run_self_check` | 一次装配 → 纯函数求值 → 落三表 → 阻断项落 AI 反馈（带去重） |
| 轮次历史 | `list_runs` | 游标分页，倒序；每轮带 `subjectState` |
| 单轮详情 | `get_run_detail` | 8 类聚合 + 逐规则结果 + findings |
| 批量自检 | `run_self_checks_batch` | 逐条独立 savepoint，部分成功也返回结果 |
| 项目看板 | `list_latest_runs` | 每条内容最新一轮的摘要 |
| 规则目录 | `rule_catalog` | 代码注册表的只读投影（**纯函数，不碰库**） |

## 一次自检的执行顺序（**固定，不可调换**）

```
1. content_bridge.read_content_edit_target(content_id)
      —— 一次读拿到 project_id / current_version_id / version_no /
         title / body / tags / bindings / blueprint_id（跨快照拼装的禁令）
2. repository 批量只读装配 CheckSubject（4 次查询，全部以第 1 步的
   current_version_id / body 为锚点展开）：
      · media_items + 其当前版本的产物规格（1 次 LEFT JOIN）
      · 当前生效基线 + 它的 claim 集合
      · 这一版生成时用的基线
      · planning_bridge 读 platform.mediaLimits（materialize=False）
3. media_bridge.split_body_into_paragraphs(body) → 段落快照（零切段实现）
4. 遍历 rules.RULES：逐条调 evaluator（纯函数）
      · 任一 evaluator 抛异常 ⇒ 该条 status=ERRORED + error_note，
        **不中断整轮**（一条规则崩掉不该让其余 7 类全部无结论）
5. 汇总五个计数 + 按定序算 outcome
6. 落 self_check_runs → self_check_rule_results（一次 flush）
7. 对 severity=BLOCKING 的 finding：去重后落 AI 反馈，
   再插 finding 并把 feedback_item_id 一起写进去
8. flush()，**不 commit**（提交边界归 session_scope）
```

### 第 2 步"一次装配"与第 4 步"纯函数求值"为什么必须分离

这是本包可测性的**全部**来源：`evaluators.py` 的测试不需要数据库，
于是"每条规则都有阳性 + 阴性对照用例"这条判据的造数成本降到构造一个
dataclass —— 否则造一组让规则命中的真数据要铺 5~8 张表，成本高到会让人
只写阴性对照，而**恒绿规则无人发现**才是本包最大的风险（R1）。

### 第 7 步为什么"先建反馈、再插 finding"

三张表**全部不可变**（ORM 层 `before_update` 守卫 + DB 触发器）。
若先插 finding 再回填 `feedback_item_id`，一旦中间发生过 flush，
那次回填就是对不可变行的 UPDATE ⇒ `ImmutableRecordError` / `23001`。
把顺序倒过来，`feedback_item_id` 在**插入那一刻**就是终值，不存在回填。

## 本包为什么**零** `If-Match`

三张表全是追加写，没有"原地改"的路径 ⇒ 没有乐观锁载体，也没有
"读-写窗口"要挡。`POST /contents/{id}/self-checks` 并发两次的结果是
**两轮**自检记录，这是正确行为（不是冲突）。唯一要防的是"同一阻断项被
两轮各建一条反馈"，那靠 §去重 + `(run_id, rule_code)` 唯一约束，不靠 ETag。
这一条必须显式写出来，否则会被当成"漏了并发保护"。

## 去重（R4）：两个方向的错都很贵

| 写错的方向 | 后果 |
|---|---|
| 去重键太宽（如只用 `rule_code`） | 新的阻断项被误认为"已有" ⇒ **不建反馈 ⇒ WP29 的审核闸放行**（静默） |
| 去重键太窄（如把 `message` 算进去） | 重跑 10 次堆 10 条 OPEN 反馈，运营的待处理列表被机器噪声淹没；而 WP29 的 `anchorState` 明令"条数不因判定而减少"，没法靠过滤救 |

键 = `(rule_code, anchor_kind, paragraph_ref, media_item_id)`，
且只对 `author_kind='AI' AND state NOT IN ('RESOLVED','REJECTED')` 的反馈生效。
人工处置掉之后重跑**应当**新建第二条 —— 阻断是"当前事实"，不是永久禁令。

## AI 不推进反馈状态

本包**只建不改**反馈。`advance_feedback` 要 `If-Match`，而机器持有 ETag 去抢
人工工作区的锁没有业务收益。⇒ **规则下一轮不再命中时，反馈不会自动关闭**，
由人处置。（说明文档 §3 的已知取舍。）
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from rednote.modules.review import (
    content_bridge,
    feedback_bridge,
    media_bridge,
    planning_bridge,
    rules,
)
from rednote.modules.review.models import SelfCheckRun
from rednote.modules.review.repository import ReviewRepository
from rednote.modules.review.types import (
    AI_FEEDBACK_SCOPE,
    MAX_BATCH_CONTENT_IDS,
    MAX_ERROR_NOTE_CHARS,
    REASON_CONTENT_IDS_EMPTY,
    REASON_SELF_CHECK_RUN_NOT_FOUND,
    REASON_SEVERITY_INVALID,
    REASON_SKIP_REASON_INVALID,
    REASON_TOO_MANY_CONTENT_IDS,
    REASON_TRIGGER_KIND_INVALID,
    REASON_UNKNOWN_RULE_CODE,
    REVIEW_CATEGORIES,
    REVIEW_SEVERITIES,
    REVIEW_SEVERITY_BLOCKING,
    REVIEW_SEVERITY_INFO,
    REVIEW_SEVERITY_WARNING,
    RULE_SET_VERSION,
    RULE_STATUS_ERRORED,
    RULE_STATUS_FAILED,
    RULE_STATUS_PASSED,
    RULE_STATUS_SKIPPED,
    RUN_OUTCOME_BLOCKED,
    RUN_OUTCOME_ERRORED,
    RUN_OUTCOME_PASSED,
    RUN_OUTCOME_WARNED,
    SKIP_REASONS,
    SUBJECT_STATE_CURRENT,
    SUBJECT_STATE_STALE,
    TRIGGER_KINDS,
    TRIGGER_KIND_BATCH,
    TRIGGER_KIND_MANUAL,
    CheckSubject,
    FindingDraft,
    ParagraphSnapshot,
    RuleOutcome,
)
from rednote.platform.errors import ApiError, ErrorCode
from rednote.platform.pagination import (
    DEFAULT_LIMIT,
    CursorPosition,
    CursorSpec,
    PageInfo,
    PageParams,
    decode_cursor,
    encode_cursor,
)

#: 阻断项落成 AI 反馈时，正文的前缀。带上 rule_code 是刻意的：
#: `content_feedback_items` 没有 `rule_code` 列（也不该加 —— 别人的表），
#: 运营在工作台上只能从正文看出"这条是哪条规则报的"。
AI_FEEDBACK_BODY_TEMPLATE: Final[str] = "[AI 自检 · {code}] {message}"

#: `feedback/types.MAX_FEEDBACK_BODY_CHARS` 同值（各写一份 + parity 测试）。
#: 超长会被 `create_feedback` 以 422 拒绝 ⇒ 阻断项落不成反馈 ⇒ 审核闸放行。
MAX_AI_FEEDBACK_BODY_CHARS: Final[int] = 2000


# ---------------------------------------------------------------------------
# 返回值结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CategorySummary:
    """一个自检分类的聚合（原型的一张卡片）。

    **只给计数，不给一个派生的 `status` 字符串**：那会是"这一类算通过吗"
    的第二处定义（第一处是 `outcome` 的四条等价式）。前端要显示
    「通过 / 提示 · N 项 / 阻断 · N 项 / 未判」时，判据是这里的计数：
    `blockingCount > 0` ⇒ 阻断；`erroredCount > 0` ⇒ 未跑成；
    `failedCount = 0 且 passedCount = 0` ⇒ 这一类**没判**（而不是通过）。
    """

    category: str
    rule_count: int
    passed_count: int
    failed_count: int
    skipped_count: int
    errored_count: int
    blocking_count: int
    warning_count: int
    info_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "ruleCount": self.rule_count,
            "passedCount": self.passed_count,
            "failedCount": self.failed_count,
            "skippedCount": self.skipped_count,
            "erroredCount": self.errored_count,
            "blockingCount": self.blocking_count,
            "warningCount": self.warning_count,
            "infoCount": self.info_count,
        }


@dataclass(frozen=True, slots=True)
class RunSummary:
    """一轮自检的摘要（不含逐规则结果与 findings）。

    `subject_state` 是**读时判定**的 `CURRENT` / `STALE`：
    `STALE` 只表示"这一轮判的不是当前版"，**不**表示这一轮可以丢弃 ——
    列表照常返回它（与 WP29 的 `anchorState` 同一条纪律：只标记、不过滤、
    不删行、不自动重跑）。

    为什么不自动重跑：那是读路径上的写副作用，且会在批量看板上放大成
    N 次写。为什么不隐藏：把 STALE 的轮次藏起来会让看板上的内容突然变回
    "未自检"，运营无从判断是"没跑过"还是"跑过但过期"。
    """

    run_id: uuid.UUID
    project_id: uuid.UUID
    content_item_id: uuid.UUID
    subject_version_id: uuid.UUID
    subject_state: str
    rule_set_version: int
    trigger_kind: str
    outcome: str
    blocking_count: int
    warning_count: int
    info_count: int
    evaluated_count: int
    skipped_count: int
    errored_count: int
    started_at: datetime
    finished_at: datetime
    created_by: uuid.UUID | None
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": str(self.run_id),
            "projectId": str(self.project_id),
            "contentItemId": str(self.content_item_id),
            "subjectVersionId": str(self.subject_version_id),
            "subjectState": self.subject_state,
            "ruleSetVersion": self.rule_set_version,
            "triggerKind": self.trigger_kind,
            "outcome": self.outcome,
            "blockingCount": self.blocking_count,
            "warningCount": self.warning_count,
            "infoCount": self.info_count,
            "evaluatedCount": self.evaluated_count,
            "skippedCount": self.skipped_count,
            "erroredCount": self.errored_count,
            "startedAt": self.started_at.isoformat(),
            "finishedAt": self.finished_at.isoformat(),
            "createdBy": None if self.created_by is None else str(self.created_by),
            "createdAt": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class RunDetail:
    """一轮自检的完整视图：摘要 + 8 类聚合 + 逐规则结果 + findings。"""

    summary: RunSummary
    categories: tuple[CategorySummary, ...]
    rule_results: tuple[dict[str, Any], ...]
    findings: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        payload = self.summary.to_dict()
        payload["categories"] = [item.to_dict() for item in self.categories]
        payload["ruleResults"] = list(self.rule_results)
        payload["findings"] = list(self.findings)
        return payload


@dataclass(frozen=True, slots=True)
class RunPage:
    """轮次历史的一页。"""

    items: tuple[dict[str, Any], ...]
    page: PageInfo


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------
def _invalid(reason: str, message: str, **details: Any) -> ApiError:
    """构造一个可读的 422（取值域 / 请求体校验失败）。

    统一走 `VALIDATION_FAILED`：这些错误都是"请求里的值与取值域对不上"，
    客户端应改请求而不是刷新重试。
    """
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        message,
        details={"reason": reason, **details},
    )


def _required_choice(
    value: str, *, allowed: frozenset[str], reason: str, field: str
) -> None:
    if value not in allowed:
        raise _invalid(
            reason,
            f"{field} 取值非法：{value!r}，允许 {sorted(allowed)}。",
            field=field,
        )


def validate_rule_result(
    *, rule_code: str, status: str, severity: str | None, skip_reason: str | None
) -> None:
    """落库前对**开集字段**做 membership 校验（`rule_code` / `skip_reason`）。

    ## 为什么这一道不能省

    `rule_code` 与 `skip_reason` **刻意不落数据库 CHECK**（开集，见
    `models.py` 的取舍说明）⇒ 数据库对它们的取值**一个字都不管**。
    本函数就是这两列取值域的**唯一**守卫：未注册的 code 或未登记的
    skip_reason 一旦落库，统计口径会静默多出一个"规则"，而没有任何
    判据会报。

    闭集字段（`category` / `status` / `rule_kind` / `severity`）由数据库
    CHECK 兜底，这里只校验与 `status` 的配对关系中最容易写错的那一对
    （`FAILED` 必须有严重度）—— 其余配对交给四条双向 CHECK。
    """
    if rules.by_code(rule_code) is None:
        raise _invalid(
            REASON_UNKNOWN_RULE_CODE,
            f"未注册的规则码 {rule_code!r}：权威在 `rules.RULES`。"
            "它不落数据库 CHECK（开集），因此这里是唯一的守卫。",
            field="ruleCode",
            ruleCode=rule_code,
        )
    if skip_reason is not None and skip_reason not in SKIP_REASONS:
        raise _invalid(
            REASON_SKIP_REASON_INVALID,
            f"未登记的 skip_reason {skip_reason!r}，允许 {sorted(SKIP_REASONS)}。",
            field="skipReason",
        )
    if severity is not None:
        _required_choice(
            severity,
            allowed=REVIEW_SEVERITIES,
            reason=REASON_SEVERITY_INVALID,
            field="severity",
        )
    if status == RULE_STATUS_FAILED and severity is None:
        raise _invalid(
            REASON_SEVERITY_INVALID,
            f"规则 {rule_code} 判为 FAILED 却没有严重度 —— "
            "`rule_results.severity` 没有来源，审核闸无从分级。",
            field="severity",
        )


def _resolve_outcome(
    *, blocking_count: int, warning_count: int, errored_count: int
) -> str:
    """按**定序** `BLOCKED > ERRORED > WARNED > PASSED` 算一轮的结论。

    这段逻辑与 `models.py` 的四条双向等价式 CHECK 是**同一件事的两处表达**，
    而两处表达必须一致 —— 不一致时数据库会当场拒（23514），
    而不是让一个矛盾的组合落库。换句话说：CHECK 是这段代码的判据，
    不是它的重复。

    `skipped_count` 与 `info_count` **不参与**：未判既不是通过也不是不通过；
    `INFO` 是登记事实，不干预审核结论。
    """
    if blocking_count > 0:
        return RUN_OUTCOME_BLOCKED
    if errored_count > 0:
        # fail-closed："规则没跑成"绝不能被读成"没问题"。
        return RUN_OUTCOME_ERRORED
    if warning_count > 0:
        return RUN_OUTCOME_WARNED
    return RUN_OUTCOME_PASSED


def _subject_state(repo: ReviewRepository, run: SelfCheckRun) -> str:
    """读时判定这一轮判的还是不是当前版（`CURRENT` / `STALE`）。

    基准是 `current_content_version_id`（投影优先 + NULL 回落末版），
    **不是** `ORDER BY version_no DESC`：回滚后投影指向旧版，而末版是
    回滚前的那一版 ⇒ 回滚过的内容会把"当前版的自检"误判成 STALE。
    """
    current = repo.current_content_version_id(run.content_item_id)
    if current is not None and current == run.subject_version_id:
        return SUBJECT_STATE_CURRENT
    return SUBJECT_STATE_STALE


def _run_summary(repo: ReviewRepository, run: SelfCheckRun) -> RunSummary:
    return RunSummary(
        run_id=run.id,
        project_id=run.project_id,
        content_item_id=run.content_item_id,
        subject_version_id=run.subject_version_id,
        subject_state=_subject_state(repo, run),
        rule_set_version=int(run.rule_set_version),
        trigger_kind=run.trigger_kind,
        outcome=run.outcome,
        blocking_count=int(run.blocking_count),
        warning_count=int(run.warning_count),
        info_count=int(run.info_count),
        evaluated_count=int(run.evaluated_count),
        skipped_count=int(run.skipped_count),
        errored_count=int(run.errored_count),
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_by=run.created_by,
        created_at=run.created_at,
    )


def _dedup_key(
    rule_code: str, finding: FindingDraft
) -> tuple[str, str, str | None, uuid.UUID | None]:
    """阻断项 → AI 反馈的去重键（见模块 docstring"去重"一节）。"""
    return (
        rule_code,
        finding.anchor_kind,
        finding.paragraph_ref,
        finding.media_item_id,
    )


# ---------------------------------------------------------------------------
# ① 跑一轮自检
# ---------------------------------------------------------------------------
def _assemble_subject(
    review_repo: ReviewRepository,
    content_repo: Any,
    planning_repo: Any,
    *,
    content_id: uuid.UUID,
) -> CheckSubject:
    """§6.5 步骤 1-3：一次装配被检对象的全部输入。

    **一次读拿身份侧 + 版本侧**（`read_edit_target`），其余查询全部以它的
    `current_version_id` / `body` 为锚点展开 —— 跨快照拼装会让段落坐标来自
    旧版、`subject_version_id` 来自新版，于是 finding 全部指向错误的段。
    """
    target = content_bridge.read_content_edit_target(
        content_repo, content_id=content_id
    )
    paragraphs = tuple(
        ParagraphSnapshot(index=index, text=text_value, ref=ref)
        for index, text_value, ref in media_bridge.split_body_into_paragraphs(
            target.body
        )
    )
    media_items = review_repo.list_media_snapshots(content_id)
    baseline_id, claim_ids = review_repo.effective_baseline_claim_scope(
        target.project_id
    )
    generated_against = review_repo.generation_baseline_id(
        target.current_version_id
    )
    limits = planning_bridge.read_platform_media_limits(
        planning_repo, project_id=target.project_id
    )
    return CheckSubject(
        content_id=target.content_id,
        project_id=target.project_id,
        subject_version_id=target.current_version_id,
        version_no=target.version_no,
        title=target.title,
        body=target.body,
        tags=target.tags,
        bindings=target.bindings,
        blueprint_id=target.blueprint_id,
        paragraphs=paragraphs,
        media_items=media_items,
        effective_baseline_id=baseline_id,
        baseline_claim_ids=claim_ids,
        generated_against_baseline_id=generated_against,
        platform_limits_configured=limits.configured,
        platform_limits=limits.value,
    )


def _evaluate_all(
    subject: CheckSubject,
) -> tuple[tuple[rules.RuleSpec, RuleOutcome, str | None], ...]:
    """§6.5 步骤 4：遍历注册表逐条求值。**单条异常不中断整轮。**

    返回 `(spec, outcome, error_note)` 三元组序列。

    ## 为什么 catch 得这么宽（`except Exception`）

    一条规则崩掉不该让其余 7 类全部无结论 —— 那会把"有一处实现有 bug"
    放大成"这篇稿子完全没被自检过"，而前端只能显示 500。
    宽 catch 的代价（真 bug 被折算成一行 `ERRORED`）由两件事补偿：
      · `ERRORED` 在 outcome 的定序里**压过** `WARNED`/`PASSED`
        （fail-closed：规则没跑成绝不能被读成没问题）；
      · `error_note` 落库，排障时能直接看到异常文本。

    `BaseException`（KeyboardInterrupt / SystemExit）**不**catch：那不是
    规则的缺陷，而是进程要退出。
    """
    outcomes: list[tuple[rules.RuleSpec, RuleOutcome, str | None]] = []
    for spec in rules.RULES:
        try:
            outcome = spec.evaluator(subject)
        except Exception as exc:  # noqa: BLE001 - 单条规则异常不中断整轮
            note = f"{type(exc).__name__}: {exc}"
            outcomes.append(
                (
                    spec,
                    RuleOutcome(status=RULE_STATUS_ERRORED),
                    note[:MAX_ERROR_NOTE_CHARS],
                )
            )
            continue
        outcomes.append((spec, outcome, None))
    return tuple(outcomes)


def run_self_check(
    review_repo: ReviewRepository,
    content_repo: Any,
    planning_repo: Any,
    feedback_repo: Any,
    *,
    content_id: uuid.UUID,
    trigger_kind: str = TRIGGER_KIND_MANUAL,
    created_by: uuid.UUID | None = None,
) -> RunDetail:
    """对**当前版本**跑一轮自检（同步），落 1 轮 + N 条规则结果 + M 条 finding。

    阻断项（`severity=BLOCKING` 的 finding）在**同一事务**内经
    `feedback_bridge` 落成 AI 反馈 ⇒ WP29 的既有审核闸（`decide_review`
    对 `APPROVED` 的前置查询）当场拒绝"通过"。本包**不新建**第二道闸。

    执行顺序见模块 docstring。本函数全程只 `flush()`、从不 `commit()` ——
    提交边界归调用方（请求级 `session_scope`）。
    """
    _required_choice(
        trigger_kind,
        allowed=TRIGGER_KINDS,
        reason=REASON_TRIGGER_KIND_INVALID,
        field="triggerKind",
    )

    started_at = datetime.now(timezone.utc)
    subject = _assemble_subject(
        review_repo, content_repo, planning_repo, content_id=content_id
    )
    evaluated = _evaluate_all(subject)
    finished_at = datetime.now(timezone.utc)

    # --- 步骤 5：汇总计数 ---------------------------------------------------
    severity_counts = {
        REVIEW_SEVERITY_BLOCKING: 0,
        REVIEW_SEVERITY_WARNING: 0,
        REVIEW_SEVERITY_INFO: 0,
    }
    evaluated_count = 0
    skipped_count = 0
    errored_count = 0
    for spec, outcome, _note in evaluated:
        validate_rule_result(
            rule_code=spec.code,
            status=outcome.status,
            severity=outcome.severity,
            skip_reason=outcome.skip_reason,
        )
        if outcome.status in (RULE_STATUS_PASSED, RULE_STATUS_FAILED):
            evaluated_count += 1
        elif outcome.status == RULE_STATUS_SKIPPED:
            skipped_count += 1
        else:
            errored_count += 1
        if outcome.status == RULE_STATUS_FAILED and outcome.severity is not None:
            severity_counts[outcome.severity] += len(outcome.findings)

    outcome_value = _resolve_outcome(
        blocking_count=severity_counts[REVIEW_SEVERITY_BLOCKING],
        warning_count=severity_counts[REVIEW_SEVERITY_WARNING],
        errored_count=errored_count,
    )

    # --- 步骤 6：落 run + rule_results ------------------------------------
    # 主键在 Python 侧先定（与 `feedback/service.create_feedback` 同款）：
    # 这样 run / rule_results / findings 的父子关系在**一次 flush 之前**
    # 就已确定，不需要为了拿 `gen_random_uuid()` 的返回值先 flush 一次。
    run_id = uuid.uuid4()
    review_repo.insert_run(
        id=run_id,
        project_id=subject.project_id,
        content_item_id=subject.content_id,
        subject_version_id=subject.subject_version_id,
        rule_set_version=RULE_SET_VERSION,
        trigger_kind=trigger_kind,
        outcome=outcome_value,
        blocking_count=severity_counts[REVIEW_SEVERITY_BLOCKING],
        warning_count=severity_counts[REVIEW_SEVERITY_WARNING],
        info_count=severity_counts[REVIEW_SEVERITY_INFO],
        evaluated_count=evaluated_count,
        skipped_count=skipped_count,
        errored_count=errored_count,
        started_at=started_at,
        finished_at=finished_at,
        created_by=created_by,
    )
    # ★ 先把 run 单独落库，再插 rule_results。
    #
    # **为什么必须是两次 flush**：SQLAlchemy 的 UOW 只按 `relationship()`
    # 推导跨表写入顺序，而本包三张表**刻意没有 ORM relationship**
    # （它们是追加式观测记录，不需要对象图导航；加 relationship 会让
    # "读一条 finding"顺带把整轮拉进 session）。没有 relationship 时
    # 同一次 flush 里两张表的 INSERT 顺序**不保证** —— 本机实测就撞到了
    # `fk_self_check_rule_results_run_id_self_check_runs` 的外键违例。
    # 显式分两次 flush 是唯一不依赖 ORM 内部排序的写法。
    review_repo.flush()

    result_ids: dict[str, uuid.UUID] = {}
    for spec, outcome, note in evaluated:
        result_id = uuid.uuid4()
        result_ids[spec.code] = result_id
        review_repo.insert_rule_result(
            id=result_id,
            run_id=run_id,
            rule_code=spec.code,
            category=spec.category,
            rule_kind=spec.kind,
            status=outcome.status,
            severity=outcome.severity,
            skip_reason=outcome.skip_reason,
            error_note=note,
            finding_count=len(outcome.findings),
        )
    # rule_results 也要先落库：findings 的一致性触发器要跨
    # finding → rule_result → run 两跳 join，父行必须已可见。
    review_repo.flush()

    # --- 步骤 7：findings（阻断项先建反馈，再把 feedback_item_id 一起插）---
    existing = review_repo.unresolved_ai_feedback_by_anchor(subject.content_id)
    for spec, outcome, _note in evaluated:
        for finding in outcome.findings:
            feedback_item_id: uuid.UUID | None = None
            if outcome.severity == REVIEW_SEVERITY_BLOCKING:
                feedback_item_id = _ensure_ai_feedback(
                    feedback_repo,
                    existing=existing,
                    subject=subject,
                    spec=spec,
                    finding=finding,
                    created_by=created_by,
                )
            review_repo.insert_finding(
                id=uuid.uuid4(),
                rule_result_id=result_ids[spec.code],
                anchor_kind=finding.anchor_kind,
                paragraph_ref=finding.paragraph_ref,
                media_item_id=finding.media_item_id,
                message=finding.message,
                evidence=finding.evidence,
                feedback_item_id=feedback_item_id,
            )
    review_repo.flush()

    run = review_repo.get_run(run_id)
    if run is None:  # pragma: no cover - 同一事务里刚插入的行不可能消失
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"自检轮次 {run_id} 落库后读不回来。",
            details={"reason": REASON_SELF_CHECK_RUN_NOT_FOUND},
        )
    return _build_detail(review_repo, run)


def _ensure_ai_feedback(
    feedback_repo: Any,
    *,
    existing: dict[tuple[str, str, str | None, uuid.UUID | None], uuid.UUID],
    subject: CheckSubject,
    spec: rules.RuleSpec,
    finding: FindingDraft,
    created_by: uuid.UUID | None,
) -> uuid.UUID:
    """阻断项 → AI 反馈：命中去重键则**复用**，否则新建（R4）。

    `existing` 在本函数里**就地更新**：同一轮里两条 finding 落在同一个
    去重键上（例如同一段里两条 binding 都超出基线范围）时，第二条要复用
    第一条刚建的那条反馈 —— 否则同一处问题在同一轮里就堆了两条。
    """
    key = _dedup_key(spec.code, finding)
    hit = existing.get(key)
    if hit is not None:
        return hit

    body = AI_FEEDBACK_BODY_TEMPLATE.format(code=spec.code, message=finding.message)
    if len(body) > MAX_AI_FEEDBACK_BODY_CHARS:
        # 截断而不是抛：超长会被 `create_feedback` 以 422 拒绝 ⇒ 阻断项落不成
        # 反馈 ⇒ WP29 的审核闸放行（静默）。完整事实在 finding.evidence 里。
        body = body[: MAX_AI_FEEDBACK_BODY_CHARS - 1] + "…"

    item = feedback_bridge.create_ai_feedback(
        feedback_repo,
        project_id=subject.project_id,
        content_id=subject.content_id,
        observed_version_id=subject.subject_version_id,
        anchor_kind=finding.anchor_kind,
        category=spec.category,
        severity=REVIEW_SEVERITY_BLOCKING,
        scope=AI_FEEDBACK_SCOPE,
        body=body,
        paragraph_ref=finding.paragraph_ref,
        media_item_id=finding.media_item_id,
        created_by=created_by,
    )
    existing[key] = item.id
    return item.id


# ---------------------------------------------------------------------------
# ② 轮次历史
# ---------------------------------------------------------------------------
def list_runs(
    review_repo: ReviewRepository,
    *,
    content_id: uuid.UUID,
    page_params: PageParams | None = None,
) -> RunPage:
    """一条内容的自检轮次（新的在前），每轮带 `subjectState`。

    ## 为什么用 keyset 而不是 offset

    `created_at` 是持续写入的排序键。`OFFSET` 的前提是"前 n 条没变"，
    而轮次会不断新增 ⇒ 翻页时漏项或重复。游标绑定排序与筛选条件
    （`CursorSpec.filters`），把别的内容的游标用到这个查询上会被 400 拒绝，
    而不是静默返回错位的一页。

    ## STALE 不过滤

    见 `RunSummary` 的说明：条数与"判的是不是当前版"**无关**。
    """
    params = page_params or PageParams(limit=DEFAULT_LIMIT)
    spec = CursorSpec(
        sort_fields=("createdAt", "id"),
        direction="desc",
        filters={"contentItemId": str(content_id)},
    )

    before: tuple[datetime, uuid.UUID] | None = None
    if params.cursor:
        position = decode_cursor(params.cursor, spec)
        try:
            before = (
                datetime.fromisoformat(str(position.sort_values[0])),
                uuid.UUID(position.item_id),
            )
        except (ValueError, TypeError, IndexError) as exc:
            raise ApiError(
                ErrorCode.MALFORMED_REQUEST,
                details={"reason": "cursor_malformed", "field": "cursor"},
            ) from exc

    rows = review_repo.list_runs_for_content(
        content_item_id=content_id, before=before, limit=params.limit + 1
    )
    has_more = len(rows) > params.limit
    page_rows = rows[: params.limit]

    next_cursor: str | None = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(
            spec,
            CursorPosition(
                sort_values=(last.created_at.isoformat(), str(last.id)),
                item_id=str(last.id),
            ),
        )
    return RunPage(
        items=tuple(
            _run_summary(review_repo, run).to_dict() for run in page_rows
        ),
        page=PageInfo(next_cursor=next_cursor, has_more=has_more),
    )


# ---------------------------------------------------------------------------
# ③ 单轮详情
# ---------------------------------------------------------------------------
def _build_detail(review_repo: ReviewRepository, run: SelfCheckRun) -> RunDetail:
    """一轮的完整视图。**8 类聚合一个都不少**（含恒 SKIPPED 的三类）。

    聚合从 `REVIEW_CATEGORIES` 起算而不是从库里读到的行起算：
    若某一类本轮一行都没有（规则被误删、或写入被静默吞掉），
    这里会给出一个全零的卡片而**不是让那一类消失** —— 缺席被前端渲染成
    "通过"的概率远高于渲染成"未判"。
    """
    results = review_repo.list_rule_results(run.id)
    findings = review_repo.list_findings_for_run(run.id)

    buckets: dict[str, dict[str, int]] = {
        category: {
            "rule_count": 0,
            "passed_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "errored_count": 0,
            "blocking_count": 0,
            "warning_count": 0,
            "info_count": 0,
        }
        for category in sorted(REVIEW_CATEGORIES)
    }
    status_field = {
        RULE_STATUS_PASSED: "passed_count",
        RULE_STATUS_FAILED: "failed_count",
        RULE_STATUS_SKIPPED: "skipped_count",
        RULE_STATUS_ERRORED: "errored_count",
    }
    severity_field = {
        REVIEW_SEVERITY_BLOCKING: "blocking_count",
        REVIEW_SEVERITY_WARNING: "warning_count",
        REVIEW_SEVERITY_INFO: "info_count",
    }
    for result in results:
        bucket = buckets.setdefault(
            result.category,
            {
                "rule_count": 0,
                "passed_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "errored_count": 0,
                "blocking_count": 0,
                "warning_count": 0,
                "info_count": 0,
            },
        )
        bucket["rule_count"] += 1
        bucket[status_field[result.status]] += 1
        if result.status == RULE_STATUS_FAILED and result.severity is not None:
            bucket[severity_field[result.severity]] += int(result.finding_count)

    categories = tuple(
        CategorySummary(category=category, **counts)
        for category, counts in sorted(buckets.items())
    )
    return RunDetail(
        summary=_run_summary(review_repo, run),
        categories=categories,
        rule_results=tuple(
            {
                "ruleResultId": str(result.id),
                "ruleCode": result.rule_code,
                "category": result.category,
                "ruleKind": result.rule_kind,
                "status": result.status,
                "severity": result.severity,
                "skipReason": result.skip_reason,
                "errorNote": result.error_note,
                "findingCount": int(result.finding_count),
            }
            for result in results
        ),
        findings=tuple(
            {
                "findingId": str(finding.id),
                "ruleCode": rule_code,
                "anchorKind": finding.anchor_kind,
                "paragraphRef": finding.paragraph_ref,
                "mediaItemId": (
                    None
                    if finding.media_item_id is None
                    else str(finding.media_item_id)
                ),
                "message": finding.message,
                "evidence": finding.evidence,
                "feedbackItemId": (
                    None
                    if finding.feedback_item_id is None
                    else str(finding.feedback_item_id)
                ),
            }
            for finding, rule_code in findings
        ),
    )


def get_run_detail(
    review_repo: ReviewRepository, *, run_id: uuid.UUID
) -> RunDetail:
    """单轮详情。轮次不存在 ⇒ 404（`details.reason` 机器可判）。"""
    run = review_repo.get_run(run_id)
    if run is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"自检轮次 {run_id} 不存在。",
            details={
                "reason": REASON_SELF_CHECK_RUN_NOT_FOUND,
                "resource": "selfCheckRun",
                "resourceId": str(run_id),
            },
        )
    return _build_detail(review_repo, run)


# ---------------------------------------------------------------------------
# ④ 批量自检
# ---------------------------------------------------------------------------
def run_self_checks_batch(
    review_repo: ReviewRepository,
    content_repo: Any,
    planning_repo: Any,
    feedback_repo: Any,
    *,
    content_item_ids: Sequence[uuid.UUID],
    created_by: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """批量自检：**逐条独立判定、不整批失败**。

    ## 为什么每条一个 SAVEPOINT

    若整批共用一个事务且不带 savepoint，第 3 条因"内容没有任何版本"抛错时，
    调用方为了继续处理第 4 条必须吞掉异常 —— 但 PostgreSQL 在语句报错后会把
    整个事务标为 aborted，后续任何语句都失败。`begin_nested()` 把每条包进
    子事务：失败只回滚那一条。

    ## 为什么返回逐条结果而不是抛第一个错

    批量的价值是"一次处理多条"。因一条不合规整批回滚，运营要逐条重试并
    自己找出哪条坏了。逐条结果让前端直接展示"哪几条成功、哪几条为什么失败"。

    成功的条目留在**外层事务**里（savepoint 释放即并入外层），
    最终由调用方统一提交。本函数不 commit。
    """
    if not content_item_ids:
        raise _invalid(
            REASON_CONTENT_IDS_EMPTY,
            "contentIds 不能为空 —— 一个空批量请求没有可执行的动作。",
            field="contentIds",
        )
    if len(content_item_ids) > MAX_BATCH_CONTENT_IDS:
        raise _invalid(
            REASON_TOO_MANY_CONTENT_IDS,
            f"一次批量自检最多 {MAX_BATCH_CONTENT_IDS} 条，"
            f"收到 {len(content_item_ids)} 条。",
            field="contentIds",
            maxContentIds=MAX_BATCH_CONTENT_IDS,
        )

    results: list[dict[str, Any]] = []
    for content_item_id in content_item_ids:
        try:
            with review_repo.session.begin_nested():
                detail = run_self_check(
                    review_repo,
                    content_repo,
                    planning_repo,
                    feedback_repo,
                    content_id=content_item_id,
                    trigger_kind=TRIGGER_KIND_BATCH,
                    created_by=created_by,
                )
            results.append(
                {
                    "contentItemId": str(content_item_id),
                    "ok": True,
                    "run": detail.summary.to_dict(),
                }
            )
        except Exception as exc:  # noqa: BLE001 - 逐条独立判定是硬性要求
            results.append(
                {
                    "contentItemId": str(content_item_id),
                    "ok": False,
                    "error": str(exc),
                }
            )
    return results


# ---------------------------------------------------------------------------
# ⑤ 项目看板（每条内容最新一轮）
# ---------------------------------------------------------------------------
def list_latest_runs(
    review_repo: ReviewRepository,
    *,
    project_id: uuid.UUID,
    content_item_ids: Sequence[uuid.UUID] | None = None,
) -> tuple[dict[str, Any], ...]:
    """项目下每条内容**最新一轮**的摘要（原型列表页的 `AI 自检` 列）。

    **没有自检过的内容不会出现在结果里** —— 这是刻意的：本端点回答的是
    "跑过的那些，结论是什么"，而"哪些内容存在"是 content 的职责。
    前端把"内容列表左连接本结果"后缺失的那些渲染成「未自检」，
    与原型列表页本来就有的那一态一致。若本端点为未自检的内容编造一行
    （哪怕 outcome 为空），那就是在数据结构上让"未自检"与"自检过"同形。
    """
    run_ids = review_repo.latest_run_ids_for_project(
        project_id=project_id, content_item_ids=content_item_ids
    )
    return tuple(
        _run_summary(review_repo, run).to_dict()
        for run in review_repo.get_runs(run_ids)
    )


# ---------------------------------------------------------------------------
# ⑥ 规则目录（纯函数，不碰库）
# ---------------------------------------------------------------------------
def rule_catalog() -> dict[str, Any]:
    """代码注册表的只读投影。

    `implemented=False` 的三条（`TONE` / `DUPLICATE` / `SCENE`）**照常列出**
    并带上 `skipReason` 的解释 —— 前端据此把那三张卡片画成"未判 · 原因"，
    而不是从"一直没有命中"猜成"通过"。
    """
    return {
        "ruleSetVersion": RULE_SET_VERSION,
        "rules": [
            {
                "code": spec.code,
                "category": spec.category,
                "ruleKind": spec.kind,
                "defaultSeverity": spec.default_severity,
                "anchorKind": spec.anchor_kind,
                "implemented": spec.implemented,
                "summary": spec.summary,
            }
            for spec in rules.RULES
        ],
    }


__all__ = [
    "AI_FEEDBACK_BODY_TEMPLATE",
    "MAX_AI_FEEDBACK_BODY_CHARS",
    "CategorySummary",
    "RunDetail",
    "RunPage",
    "RunSummary",
    "get_run_detail",
    "list_latest_runs",
    "list_runs",
    "rule_catalog",
    "run_self_check",
    "run_self_checks_batch",
    "validate_rule_result",
]
