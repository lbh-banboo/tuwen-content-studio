"""WP30 AI 自检 / 硬软规则引擎的取值域、边界与两个快照类型。

本文件**只放常量与纯值类型**，不 import 任何 `rednote.modules.*`
（平级模块不可互 import；跨模块取数走 `repository.py` 的裸 SQL、
跨模块调用走四个 `*_bridge.py`）。分层是单向的：

    types ← rules ← evaluators ← models / repository ← service ← api

## 为什么取值域是这些

| 常量族 | 取值 | 依据 |
|---|---|---|
| `REVIEW_CATEGORY_*` | 8 类 | 原型 `content-feedback.html` 的 8 张自检卡片。**值逐字采用 WP29 `feedback/types.FEEDBACK_CATEGORY_*`**（已落库成 CHECK），名字各写一份（平级不可互 import），一致性由跨包 parity 测试钉住 |
| `REVIEW_SEVERITY_*` | `BLOCKING` / `WARNING` / `INFO` | 与 WP29 同值：阻断项要能喂进它的审核闸 |
| `REVIEW_ANCHOR_KIND_*` | `WHOLE` / `PARAGRAPH` / `MEDIA` | 命中只能落在"整篇 / 某段 / 某张图"三处 |
| `RULE_KIND_*` | `HARD` / `SOFT` | **不是严重度的同义词**：`HARD` = 同一输入必得同一结论（纯函数 + 库事实）；`SOFT` = 结论依赖启发式，可能随版本变。一条 `SOFT` 规则也可以 `BLOCKING`，反之一条 `HARD` 规则可以只 `INFO` |
| `RULE_STATUS_*` | `PASSED` / `FAILED` / `SKIPPED` / `ERRORED` | **"没判"必须与"通过"在数据结构上不可混淆** —— 这是本包最核心的一条 |
| `RUN_OUTCOME_*` | `PASSED` / `WARNED` / `BLOCKED` / `ERRORED` | 一轮的聚合结论；定序 `BLOCKED > ERRORED > WARNED > PASSED`（见 `models.py` 的四条等价式） |
| `TRIGGER_KIND_*` | `MANUAL` / `BATCH` | **刻意不含 `AUTO`**：自动触发本包不做，不留空转取值 |
| `SKIP_REASON_*` | 5 个 | `SKIPPED` 必须带**受控**原因码；自由文本会让"为什么没判"无法机器分类 |
| `SUBJECT_STATE_*` | `CURRENT` / `STALE` | 读时判定"这一轮判的还是当前版吗"，与 WP29 的 `anchorState` 同款手法：只标记、不过滤、不自动重跑 |

## `RULE_SET_VERSION` 为什么要落库

没有它，"上周判通过、今天判阻断"无法区分"稿子变了"与"规则变了"。
每轮把本常量的**快照**写进 `self_check_runs.rule_set_version`，
与 `content_versions.schema_version` 同款。

**改动规则集合（增删规则、改默认严重度、改 HARD/SOFT 归属）时必须 +1。**

## 边界常量为什么"各写一份"

`MAX_PARAGRAPH_REF_CHARS` 与 `media/types.py`、`feedback/types.py` 同值；
`MIN_PARAGRAPHS_FOR_IMAGE_PLAN` 与 `media/types.IMAGES_PER_CONTENT` 同值；
`AI_FEEDBACK_SCOPE` 与 `feedback/types.FEEDBACK_SCOPE_CURRENT_ONLY` 同值。
平级模块不可互 import ⇒ 各写一份，一致性由 `test_review_wp30.py` 的
跨包 parity 测试对账（手法照 `feedback/types.py:21-23`）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Final

# ---------------------------------------------------------------------------
# 规则集版本（落库快照）
# ---------------------------------------------------------------------------
#: 规则集合的版本号。增删规则 / 改默认严重度 / 改 HARD-SOFT 归属时 **+1**。
RULE_SET_VERSION: Final[int] = 1

# ---------------------------------------------------------------------------
# 自检分类（8 类；值与 WP29 `FEEDBACK_CATEGORY_*` 逐字相同）
# ---------------------------------------------------------------------------
REVIEW_CATEGORY_FACT: Final[str] = "FACT"
REVIEW_CATEGORY_TONE: Final[str] = "TONE"
REVIEW_CATEGORY_STRUCTURE: Final[str] = "STRUCTURE"
REVIEW_CATEGORY_SCENE: Final[str] = "SCENE"
REVIEW_CATEGORY_DUPLICATE: Final[str] = "DUPLICATE"
REVIEW_CATEGORY_MEDIA_CONSISTENCY: Final[str] = "MEDIA_CONSISTENCY"
REVIEW_CATEGORY_TECH_FORMAT: Final[str] = "TECH_FORMAT"
REVIEW_CATEGORY_OTHER: Final[str] = "OTHER"

REVIEW_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        REVIEW_CATEGORY_FACT,
        REVIEW_CATEGORY_TONE,
        REVIEW_CATEGORY_STRUCTURE,
        REVIEW_CATEGORY_SCENE,
        REVIEW_CATEGORY_DUPLICATE,
        REVIEW_CATEGORY_MEDIA_CONSISTENCY,
        REVIEW_CATEGORY_TECH_FORMAT,
        REVIEW_CATEGORY_OTHER,
    }
)
REVIEW_CATEGORY_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(REVIEW_CATEGORIES)
)

# ---------------------------------------------------------------------------
# 严重度（与 WP29 同值）
# ---------------------------------------------------------------------------
REVIEW_SEVERITY_BLOCKING: Final[str] = "BLOCKING"
REVIEW_SEVERITY_WARNING: Final[str] = "WARNING"
REVIEW_SEVERITY_INFO: Final[str] = "INFO"

REVIEW_SEVERITIES: Final[frozenset[str]] = frozenset(
    {REVIEW_SEVERITY_BLOCKING, REVIEW_SEVERITY_WARNING, REVIEW_SEVERITY_INFO}
)
REVIEW_SEVERITY_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(REVIEW_SEVERITIES)
)

# ---------------------------------------------------------------------------
# 锚点粒度（与 WP29 同值；列占用互斥由 CHECK 表达）
# ---------------------------------------------------------------------------
REVIEW_ANCHOR_KIND_WHOLE: Final[str] = "WHOLE"
REVIEW_ANCHOR_KIND_PARAGRAPH: Final[str] = "PARAGRAPH"
REVIEW_ANCHOR_KIND_MEDIA: Final[str] = "MEDIA"

REVIEW_ANCHOR_KINDS: Final[frozenset[str]] = frozenset(
    {
        REVIEW_ANCHOR_KIND_WHOLE,
        REVIEW_ANCHOR_KIND_PARAGRAPH,
        REVIEW_ANCHOR_KIND_MEDIA,
    }
)
REVIEW_ANCHOR_KIND_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(REVIEW_ANCHOR_KINDS)
)

# ---------------------------------------------------------------------------
# 规则种类：HARD / SOFT（**另一维**，不是严重度）
# ---------------------------------------------------------------------------
RULE_KIND_HARD: Final[str] = "HARD"
RULE_KIND_SOFT: Final[str] = "SOFT"

RULE_KINDS: Final[frozenset[str]] = frozenset({RULE_KIND_HARD, RULE_KIND_SOFT})
RULE_KIND_LIST_SQL: Final[str] = ", ".join(f"'{name}'" for name in sorted(RULE_KINDS))

# ---------------------------------------------------------------------------
# 单条规则的结论
# ---------------------------------------------------------------------------
RULE_STATUS_PASSED: Final[str] = "PASSED"
RULE_STATUS_FAILED: Final[str] = "FAILED"
RULE_STATUS_SKIPPED: Final[str] = "SKIPPED"
RULE_STATUS_ERRORED: Final[str] = "ERRORED"

RULE_STATUSES: Final[frozenset[str]] = frozenset(
    {
        RULE_STATUS_PASSED,
        RULE_STATUS_FAILED,
        RULE_STATUS_SKIPPED,
        RULE_STATUS_ERRORED,
    }
)
RULE_STATUS_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(RULE_STATUSES)
)

# ---------------------------------------------------------------------------
# 一轮的聚合结论
# ---------------------------------------------------------------------------
RUN_OUTCOME_PASSED: Final[str] = "PASSED"
RUN_OUTCOME_WARNED: Final[str] = "WARNED"
RUN_OUTCOME_BLOCKED: Final[str] = "BLOCKED"
RUN_OUTCOME_ERRORED: Final[str] = "ERRORED"

RUN_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        RUN_OUTCOME_PASSED,
        RUN_OUTCOME_WARNED,
        RUN_OUTCOME_BLOCKED,
        RUN_OUTCOME_ERRORED,
    }
)
RUN_OUTCOME_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(RUN_OUTCOMES)
)

# ---------------------------------------------------------------------------
# 触发方式（**无 `AUTO`**）
# ---------------------------------------------------------------------------
TRIGGER_KIND_MANUAL: Final[str] = "MANUAL"
TRIGGER_KIND_BATCH: Final[str] = "BATCH"

TRIGGER_KINDS: Final[frozenset[str]] = frozenset(
    {TRIGGER_KIND_MANUAL, TRIGGER_KIND_BATCH}
)
TRIGGER_KIND_LIST_SQL: Final[str] = ", ".join(
    f"'{name}'" for name in sorted(TRIGGER_KINDS)
)

# ---------------------------------------------------------------------------
# `SKIPPED` 的受控原因码
# ---------------------------------------------------------------------------
#: 语气类需要模型判定，而本包 MVP 纯本地判定（ADR-10 默认 `LOCAL_ONLY` 无回退）。
SKIP_REASON_MODEL_RULE_NOT_ENABLED: Final[str] = "MODEL_RULE_NOT_ENABLED"
#: 重复类是**跨篇**语义（WP33 的分界面）；本包的输入是一篇，不是一批。
SKIP_REASON_CROSS_ITEM_SCOPE_OUT_OF_PACKAGE: Final[str] = (
    "CROSS_ITEM_SCOPE_OUT_OF_PACKAGE"
)
#: 场景类全仓无"内容 → 场景"的机器可判关联列（`narrative_blueprints.usage_scene`
#: 是自由文本，与 `plan_drafts.scene_items` 的 key 之间没有任何可对账的联系）。
SKIP_REASON_NO_SCENE_LINKAGE_IN_SCHEMA: Final[str] = "NO_SCENE_LINKAGE_IN_SCHEMA"
#: 平台媒体限制是 `runtime_settings` 的**占位键**（OP-64 仍 OPEN）。
#: 未配置时**不冒充通过**，也不在本包内置 1440×1920 / 2MB 字面量
#: （内置等于在本包造第二处平台限制真值）。
SKIP_REASON_PLATFORM_MEDIA_LIMITS_NOT_CONFIGURED: Final[str] = (
    "PLATFORM_MEDIA_LIMITS_NOT_CONFIGURED"
)
#: 项目还没有生效基线 ⇒ 事实类规则的**判据域为空**。
#: 这时既不能判通过（没判据），也不能判失败（否则每条 binding 都会被
#: 系统性误报成"超出基线范围"——恒红的规则与恒绿的规则同样是假防线）。
SKIP_REASON_NO_EFFECTIVE_BASELINE: Final[str] = "NO_EFFECTIVE_BASELINE"

SKIP_REASONS: Final[frozenset[str]] = frozenset(
    {
        SKIP_REASON_MODEL_RULE_NOT_ENABLED,
        SKIP_REASON_CROSS_ITEM_SCOPE_OUT_OF_PACKAGE,
        SKIP_REASON_NO_SCENE_LINKAGE_IN_SCHEMA,
        SKIP_REASON_PLATFORM_MEDIA_LIMITS_NOT_CONFIGURED,
        SKIP_REASON_NO_EFFECTIVE_BASELINE,
    }
)

# ---------------------------------------------------------------------------
# 轮次与当前版本的关系（读时判定，不落库）
# ---------------------------------------------------------------------------
SUBJECT_STATE_CURRENT: Final[str] = "CURRENT"
SUBJECT_STATE_STALE: Final[str] = "STALE"

# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------
#: 与 `media/types.MAX_PARAGRAPH_REF_CHARS`、`feedback/types` 同值（各写一份）。
MAX_PARAGRAPH_REF_CHARS: Final[int] = 200
#: `rule_code` 的列宽。注册表里每条 code 都必须 ≤ 本值（导入期 assert）。
MAX_RULE_CODE_CHARS: Final[int] = 60
MAX_SKIP_REASON_CHARS: Final[int] = 40
MAX_FINDING_MESSAGE_CHARS: Final[int] = 500
MAX_ERROR_NOTE_CHARS: Final[int] = 500
#: 一次批量自检最多多少条内容（防一个请求打成全项目扫描）。
MAX_BATCH_CONTENT_IDS: Final[int] = 50

#: 与 `media/types.IMAGES_PER_CONTENT` 同值（各写一份 + parity 测试）。
#: `STRUCTURE_PARAGRAPH_COUNT_BELOW_IMAGE_PLAN` 的阈值：段数少于图位数时，
#: 契约 §4 规定多余的图**回落最后一段** —— 合法但退化，值得提示。
MIN_PARAGRAPHS_FOR_IMAGE_PLAN: Final[int] = 3

#: AI 阻断项落成反馈时用的 scope；与 `feedback/types.FEEDBACK_SCOPE_CURRENT_ONLY`
#: 同值（各写一份 + parity 测试）。自检判的是**当前这一版**，不沉淀到后续生成。
AI_FEEDBACK_SCOPE: Final[str] = "CURRENT_ONLY"

#: 与 `media/types.MEDIA_STATE_READY` 同值（各写一份 + parity 测试）。
#: `MEDIA_ITEM_NOT_READY` 的判据：**不是** READY 就提示 —— 逐个列出
#: PENDING/FAILED 会让"将来 media 多一个状态"时本规则静默漏判。
MEDIA_ITEM_STATE_READY: Final[str] = "READY"
#: 与 `media/types.RENDITION_STATE_READY` 同值（各写一份 + parity 测试）。
#: 只有 READY 档位的四列规格齐全（`ready_requires_artifact` CHECK），
#: 因此技术格式只对 READY 档位判。
MEDIA_RENDITION_STATE_READY: Final[str] = "READY"

# ---------------------------------------------------------------------------
# 平台媒体限制：本规则认识的上限键
# ---------------------------------------------------------------------------
# `runtime_settings` 的 value 形状是 `{键: 数 | null}`（`settings_service`
# 的 `_normalize_numeric_mapping` 强制），因此**无法**承载"允许的图片格式
# 列表"这种字符串集合 ⇒ 本规则只判三个数值上限，`image_format` 不判。
# 这是一处如实登记的能力缺口，不是漏做（见说明文档 §3 的已知取舍）。
LIMIT_KEY_MAX_WIDTH: Final[str] = "maxWidth"
LIMIT_KEY_MAX_HEIGHT: Final[str] = "maxHeight"
LIMIT_KEY_MAX_BYTE_SIZE: Final[str] = "maxByteSize"

PLATFORM_LIMIT_KEYS: Final[tuple[str, ...]] = (
    LIMIT_KEY_MAX_WIDTH,
    LIMIT_KEY_MAX_HEIGHT,
    LIMIT_KEY_MAX_BYTE_SIZE,
)

# ---------------------------------------------------------------------------
# 禁用词表（`OTHER_BANNED_TERM_HIT` 的判据域）
# ---------------------------------------------------------------------------
# **为什么是代码常量而不是一张配置表**：做成可维护词库 = 本包再开一处配置
# 真值（而 `runtime_settings` + `get_effective_limits` 已是"全仓唯一的配置
# 读取路径"）；而原型只给了「禁用词检测 0 命中」一句，没有维护入口。
# 要可配时按 `platform.mediaLimits` 的同款形态走 planning，由主 agent 加 key。
BANNED_TERMS: Final[tuple[str, ...]] = (
    "最好",
    "第一品牌",
    "全球首发",
    "100%有效",
    "绝对安全",
    "国家级",
    "彻底根治",
    "永久有效",
)

# ---------------------------------------------------------------------------
# 可读 4xx 的 reason（放进 `details.reason`，供前端与断言机器可判）
# ---------------------------------------------------------------------------
REASON_SELF_CHECK_RUN_NOT_FOUND: Final[str] = "SELF_CHECK_RUN_NOT_FOUND"
REASON_TRIGGER_KIND_INVALID: Final[str] = "TRIGGER_KIND_INVALID"
REASON_UNKNOWN_RULE_CODE: Final[str] = "UNKNOWN_RULE_CODE"
REASON_SKIP_REASON_INVALID: Final[str] = "SKIP_REASON_INVALID"
REASON_SEVERITY_INVALID: Final[str] = "REVIEW_SEVERITY_INVALID"
REASON_CONTENT_IDS_EMPTY: Final[str] = "CONTENT_IDS_EMPTY"
REASON_TOO_MANY_CONTENT_IDS: Final[str] = "TOO_MANY_CONTENT_IDS"


# ---------------------------------------------------------------------------
# 快照类型：一次自检的**全部**输入（装配一次，规则只读）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RenditionSnapshot:
    """一个产物档位的规格（`media_renditions` 的四列 + 身份）。

    只有 `state='READY'` 的行才保证四列齐全（`ready_requires_artifact` CHECK），
    因此 `width` / `height` / `byte_size` / `image_format` 仍声明为可空 ——
    规则必须自己处理"PENDING 档位没有规格"，而不是假定它有。
    """

    rendition_id: uuid.UUID
    kind: str
    state: str
    width: int | None
    height: int | None
    byte_size: int | None
    image_format: str | None


@dataclass(frozen=True, slots=True)
class MediaSnapshot:
    """一个媒体位（`media_items` 身份行）及其**当前版本**的产物档位。

    `paragraph_ref` 是**当前版本的投影**，不落唯一约束、无 FK
    （契约 §4：顺序对位、多张落同一段是合法的）。因此任何
    "ref 必须唯一"或"每段恰好一张图"的规则都是错的。
    """

    media_item_id: uuid.UUID
    paragraph_ref: str | None
    sort_order: int
    state: str
    renditions: tuple[RenditionSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class ParagraphSnapshot:
    """正文切段的一项。`ref` 由 `media_bridge` 转发 media 的纯函数生成。

    **本包零切段实现**：序号与 ref 的口径只有 media 一处来源，
    否则 `MEDIA_PARAGRAPH_REF_STALE` 会比较两套切段产物而**系统性误报**。
    """

    index: int
    text: str
    ref: str


@dataclass(frozen=True, slots=True)
class CheckSubject:
    """一次自检的被检对象快照 —— **装配一次，规则只读**。

    ## 为什么必须是一个快照而不是让规则各自取数

    ① 规则若自己查库，`evaluators.py` 就不再是纯函数 ⇒ "每条规则都有
       阳性 + 阴性对照用例"这条判据必须连库才能跑，而连库的用例造数成本
       高到会让人只写阴性对照（于是恒绿规则无人发现）；
    ② 同一轮里不同规则取到不同时刻的数据时，聚合结论没有意义
       （"段落来自旧版、图位来自新版"的 finding 指向错误坐标）。

    `platform_limits_configured` 与 `platform_limits` **分两维**：
    占位键的合法状态就是"没有值"，用 `platform_limits is None` 兼任会与
    "有值但为空字典"混淆（与 `planning/types.EffectiveSetting.configured`
    同一条理由）。
    """

    content_id: uuid.UUID
    project_id: uuid.UUID
    subject_version_id: uuid.UUID
    version_no: int
    title: str
    body: str
    tags: tuple[str, ...]
    bindings: tuple[dict[str, Any], ...]
    blueprint_id: uuid.UUID | None
    paragraphs: tuple[ParagraphSnapshot, ...]
    media_items: tuple[MediaSnapshot, ...]
    #: 项目**当前生效**基线（`projects.active_baseline_id`）。
    effective_baseline_id: uuid.UUID | None
    #: 当前生效基线的 claim 集合（判据域）。
    baseline_claim_ids: frozenset[uuid.UUID]
    #: 这一版**生成时**用的基线（`generation_contexts.baseline_id`）。
    #: 与 `effective_baseline_id` 不同即说明基线在生成之后前进过 ——
    #: 这正是事实类规则在写入期校验之外仍有新信息的来源。
    generated_against_baseline_id: uuid.UUID | None
    platform_limits_configured: bool
    platform_limits: dict[str, Any] | None

    @property
    def paragraph_refs(self) -> frozenset[str]:
        """当前正文的段落 ref 集合（`MEDIA_PARAGRAPH_REF_STALE` 的判据域）。"""
        return frozenset(paragraph.ref for paragraph in self.paragraphs)

    @property
    def paragraph_count(self) -> int:
        return len(self.paragraphs)


# ---------------------------------------------------------------------------
# 快照类型：一条规则的结论
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FindingDraft:
    """一条命中（还没落库，所以叫 draft）。

    `evidence` 是**机器可判的事实**（实测值 / 期望值 / 引用 id），
    不是 `message` 的重复。键名用 camelCase（与 `content_versions.bindings`
    同惯例）—— 它直接出现在 JSON 响应里。
    """

    anchor_kind: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    paragraph_ref: str | None = None
    media_item_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """一条规则跑完的结论。四个状态各有自己的必填项（由 DB 的双向 CHECK 兜底）。

    | status | severity | skip_reason | findings |
    |---|---|---|---|
    | `PASSED` | NULL | NULL | 空 |
    | `FAILED` | 非空 | NULL | **非空** |
    | `SKIPPED` | NULL | **非空** | 空 |
    | `ERRORED` | NULL | NULL | 空（`error_note` 由服务层填） |

    不提供"部分命中但算通过"的中间态：那会让"通过"这个词在不同规则里
    含义不同，而下游（审核闸、关口、看板列）只看聚合结论。
    """

    status: str
    severity: str | None = None
    skip_reason: str | None = None
    findings: tuple[FindingDraft, ...] = ()


def passed() -> RuleOutcome:
    """这条规则跑了，没问题。"""
    return RuleOutcome(status=RULE_STATUS_PASSED)


def failed(severity: str, findings: tuple[FindingDraft, ...]) -> RuleOutcome:
    """这条规则跑了，命中了 N 处。

    `findings` 为空时抛 `ValueError`：`FAILED` 而零 finding 是**坏结论**
    （前端会显示"有问题"但点不开任何一处），数据库的
    `(status='FAILED') = (finding_count > 0)` CHECK 同样会拒。
    在纯函数层先抛，错误就落在写规则的人手上，而不是落在一条 23514 上。
    """
    if not findings:
        raise ValueError(
            "FAILED 必须带至少一条 finding —— 否则前端显示'有问题'却点不开任何一处。"
        )
    return RuleOutcome(
        status=RULE_STATUS_FAILED, severity=severity, findings=tuple(findings)
    )


def skipped(reason: str) -> RuleOutcome:
    """这条规则**没判**，原因是受控码 `reason`。

    未知原因码抛 `ValueError`：`skip_reason` 刻意不落数据库 CHECK
    （见 `models.py` 的取舍说明），因此取值域的唯一守卫就是本函数
    与服务层的 membership 校验 —— 这里放过去就等于让自由文本落库。
    """
    if reason not in SKIP_REASONS:
        raise ValueError(
            f"未知的 skip_reason={reason!r}，允许 {sorted(SKIP_REASONS)}。"
        )
    return RuleOutcome(status=RULE_STATUS_SKIPPED, skip_reason=reason)


__all__ = [
    "AI_FEEDBACK_SCOPE",
    "BANNED_TERMS",
    "CheckSubject",
    "FindingDraft",
    "LIMIT_KEY_MAX_BYTE_SIZE",
    "LIMIT_KEY_MAX_HEIGHT",
    "LIMIT_KEY_MAX_WIDTH",
    "MAX_BATCH_CONTENT_IDS",
    "MAX_ERROR_NOTE_CHARS",
    "MAX_FINDING_MESSAGE_CHARS",
    "MAX_PARAGRAPH_REF_CHARS",
    "MAX_RULE_CODE_CHARS",
    "MAX_SKIP_REASON_CHARS",
    "MEDIA_ITEM_STATE_READY",
    "MEDIA_RENDITION_STATE_READY",
    "MIN_PARAGRAPHS_FOR_IMAGE_PLAN",
    "MediaSnapshot",
    "PLATFORM_LIMIT_KEYS",
    "ParagraphSnapshot",
    "REASON_CONTENT_IDS_EMPTY",
    "REASON_SELF_CHECK_RUN_NOT_FOUND",
    "REASON_SEVERITY_INVALID",
    "REASON_SKIP_REASON_INVALID",
    "REASON_TOO_MANY_CONTENT_IDS",
    "REASON_TRIGGER_KIND_INVALID",
    "REASON_UNKNOWN_RULE_CODE",
    "REVIEW_ANCHOR_KINDS",
    "REVIEW_ANCHOR_KIND_LIST_SQL",
    "REVIEW_ANCHOR_KIND_MEDIA",
    "REVIEW_ANCHOR_KIND_PARAGRAPH",
    "REVIEW_ANCHOR_KIND_WHOLE",
    "REVIEW_CATEGORIES",
    "REVIEW_CATEGORY_DUPLICATE",
    "REVIEW_CATEGORY_FACT",
    "REVIEW_CATEGORY_LIST_SQL",
    "REVIEW_CATEGORY_MEDIA_CONSISTENCY",
    "REVIEW_CATEGORY_OTHER",
    "REVIEW_CATEGORY_SCENE",
    "REVIEW_CATEGORY_STRUCTURE",
    "REVIEW_CATEGORY_TECH_FORMAT",
    "REVIEW_CATEGORY_TONE",
    "REVIEW_SEVERITIES",
    "REVIEW_SEVERITY_BLOCKING",
    "REVIEW_SEVERITY_INFO",
    "REVIEW_SEVERITY_LIST_SQL",
    "REVIEW_SEVERITY_WARNING",
    "RULE_KINDS",
    "RULE_KIND_HARD",
    "RULE_KIND_LIST_SQL",
    "RULE_KIND_SOFT",
    "RULE_SET_VERSION",
    "RULE_STATUSES",
    "RULE_STATUS_ERRORED",
    "RULE_STATUS_FAILED",
    "RULE_STATUS_LIST_SQL",
    "RULE_STATUS_PASSED",
    "RULE_STATUS_SKIPPED",
    "RUN_OUTCOMES",
    "RUN_OUTCOME_BLOCKED",
    "RUN_OUTCOME_ERRORED",
    "RUN_OUTCOME_LIST_SQL",
    "RUN_OUTCOME_PASSED",
    "RUN_OUTCOME_WARNED",
    "RenditionSnapshot",
    "RuleOutcome",
    "SKIP_REASONS",
    "SKIP_REASON_CROSS_ITEM_SCOPE_OUT_OF_PACKAGE",
    "SKIP_REASON_MODEL_RULE_NOT_ENABLED",
    "SKIP_REASON_NO_EFFECTIVE_BASELINE",
    "SKIP_REASON_NO_SCENE_LINKAGE_IN_SCHEMA",
    "SKIP_REASON_PLATFORM_MEDIA_LIMITS_NOT_CONFIGURED",
    "SUBJECT_STATE_CURRENT",
    "SUBJECT_STATE_STALE",
    "TRIGGER_KINDS",
    "TRIGGER_KIND_BATCH",
    "TRIGGER_KIND_LIST_SQL",
    "TRIGGER_KIND_MANUAL",
    "failed",
    "passed",
    "skipped",
]
