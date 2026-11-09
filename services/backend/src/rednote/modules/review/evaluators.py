"""WP30 规则求值器：**11 个纯函数**，签名统一 `(CheckSubject) -> RuleOutcome`。

## 本文件的硬纪律（它是本包可测性的全部来源）

**零 IO、零 session、零 `datetime.now()`、零随机、零网关调用。**

逐条理由：

| 禁止 | 若违反会怎样 |
|---|---|
| 连库 | "每条规则都有阳性 + 阴性对照用例"这条判据必须连库才能跑。造一组让规则命中的真数据往往要铺 5~8 张表 —— 成本高到会让人只写阴性对照，于是**恒绿规则无人发现**（R1） |
| `datetime.now()` | 同一份输入在不同时刻得到不同结论 ⇒ `rule_kind=HARD`（"同一输入必得同一结论"）这句话变成假的 |
| 调模型 / 网关 | ADR-10 默认 `LOCAL_ONLY` 无回退（`gateway/policy.py`「这条没有例外、没有回落、没有降级」）⇒ 规则从"遇到缺口就停"退化成"永远停"，而那与 `SKIPPED` 在响应里**长得一样** |

数据库的取数全部在 `repository.py`，装配在 `service.py`（一次，§6.5 步骤 1-3）。

## 为什么有三条"恒 SKIPPED"的规则

`TONE` / `DUPLICATE` / `SCENE` 三类在当前 schema + MVP 边界下**判不了**：

| 类 | 判不了的硬原因 |
|---|---|
| `TONE` | 需要模型判定；本包 MVP 纯本地判定（ADR-10 + E7 外部额度阻塞） |
| `DUPLICATE` | 是**跨篇**语义，归 WP33；本包的输入是一篇，不是一批 |
| `SCENE` | 全仓**没有**"内容 → 场景"的机器可判关联列（`narrative_blueprints.usage_scene` 是自由文本） |

**不注册它们的后果比注册更糟**：那一类会一行都没有 ⇒ 响应里 8 张卡片变 5 张，
而前端把"没有行"渲染成"通过"的概率远高于渲染成"未判"。
一条明确报告"我没判、原因是 X"的规则**是防线**；一条缺席而被读成通过的
规则是**假绿**。这三条有实现、有受控原因码、有用例 —— 不是"声明了但一行
都没实现"的空转常量。

将来 WP33 落地时，改动面只有本文件的一个函数：那条规则的 `status` 从
`SKIPPED` 变 `PASSED`/`FAILED`，三张表的结构**一行都不用改**。

## 砍掉的规则（写在这里，防止实现时顺手加回来）

| 砍掉 | 理由 |
|---|---|
| "图数 = 6" / "图数 = `images_per_content(form)`" | 后端固定 3 张（`media/types.py:180`，与原型的一处**已知偏差**）；且 `media_form` 落在 `media_versions` **逐版本**而非逐内容，"图数应该是几"在内容层面无唯一答案 |
| "每段恰好一张图" / "`paragraph_ref` 唯一" | 契约 §4 明文：顺序对位、段落数不足时多余张**回落最后一段**，**多张落同一段是合法的** |
| "蓝图 9 字段非空" / "标题/正文/标签长度在界内" / "tags 是数组" | 数据库 CHECK 已拦（`content/models.py:689-701` 等）⇒ 再写一条只能**恒绿** |
| "bindings 的 paragraphIndex 越界" | `content/schemas.validate_bindings` 是唯一写入口，WP29 又强制编辑路径重跑它 ⇒ 正常路径下恒绿。阳性对照只能靠直连 SQL 造数据，而"生产里长期零命中的规则"会让人把自检当成没用 |
"""

from __future__ import annotations

import uuid
from typing import Any

from rednote.modules.review.types import (
    BANNED_TERMS,
    LIMIT_KEY_MAX_BYTE_SIZE,
    LIMIT_KEY_MAX_HEIGHT,
    LIMIT_KEY_MAX_WIDTH,
    MAX_FINDING_MESSAGE_CHARS,
    MEDIA_ITEM_STATE_READY,
    MEDIA_RENDITION_STATE_READY,
    MIN_PARAGRAPHS_FOR_IMAGE_PLAN,
    PLATFORM_LIMIT_KEYS,
    REVIEW_ANCHOR_KIND_MEDIA,
    REVIEW_ANCHOR_KIND_PARAGRAPH,
    REVIEW_ANCHOR_KIND_WHOLE,
    REVIEW_SEVERITY_BLOCKING,
    REVIEW_SEVERITY_INFO,
    REVIEW_SEVERITY_WARNING,
    SKIP_REASON_CROSS_ITEM_SCOPE_OUT_OF_PACKAGE,
    SKIP_REASON_MODEL_RULE_NOT_ENABLED,
    SKIP_REASON_NO_EFFECTIVE_BASELINE,
    SKIP_REASON_NO_SCENE_LINKAGE_IN_SCHEMA,
    SKIP_REASON_PLATFORM_MEDIA_LIMITS_NOT_CONFIGURED,
    CheckSubject,
    FindingDraft,
    RuleOutcome,
    failed,
    passed,
    skipped,
)

# ---------------------------------------------------------------------------
# `bindings` 的键名（camelCase）
# ---------------------------------------------------------------------------
# 与 `content/types.BINDING_KEY_*` 同值、**各写一份**（平级模块不可互 import）。
# 一致性由 `test_review_wp30.py` 的跨包 parity 测试对账 —— 写错键名的症状是
# "规则永远读到 None ⇒ 恒绿"，而那是本包最怕的失败形态。
BINDING_KEY_PARAGRAPH_INDEX = "paragraphIndex"
BINDING_KEY_CLAIM_ID = "claimId"


def _truncate(message: str) -> str:
    """把 finding 的 message 截到列宽内。

    **为什么截断而不是抛**：message 里可能嵌入用户正文片段（禁用词命中的
    那一段），长度不受本包控制。超长时抛异常会让一条**正确的命中**变成
    `ERRORED`（"规则没跑成"），而真正的信息（命中了什么）就丢了。
    截断保住结论；完整事实在 `evidence` 里（JSONB 无此长度限制）。
    """
    if len(message) <= MAX_FINDING_MESSAGE_CHARS:
        return message
    return message[: MAX_FINDING_MESSAGE_CHARS - 1] + "…"


def _claim_id_of(binding: Any) -> tuple[uuid.UUID | None, str | None]:
    """从一条 binding 里取 `claimId`，返回 `(解析成功的 uuid, 原始字符串)`。

    三种返回组合各有含义，**刻意不合并**：

    | 返回 | 含义 |
    |---|---|
    | `(None, None)` | 这条 binding 没绑 claim（`claimId: null` 是合法形状，配非空 `reason`） |
    | `(uuid, 原串)` | 绑了，且形状合法 |
    | `(None, 原串)` | 绑了一个**解析不出 uuid** 的值 —— 只可能来自绕过 `validate_bindings` 的写入路径，按"超出判据域"处置 |
    """
    if not isinstance(binding, dict):
        return None, None
    raw = binding.get(BINDING_KEY_CLAIM_ID)
    if raw is None:
        return None, None
    if isinstance(raw, uuid.UUID):
        return raw, str(raw)
    text_value = str(raw)
    try:
        return uuid.UUID(text_value), text_value
    except (ValueError, AttributeError, TypeError):
        return None, text_value


def _paragraph_anchor(
    subject: CheckSubject, binding: Any
) -> tuple[str, str | None]:
    """按 binding 的 `paragraphIndex` 给出锚点 `(anchor_kind, paragraph_ref)`。

    索引落在当前段落范围内 ⇒ 段落锚点（运营能直接跳到那一段）；
    否则回落整篇锚点。**回落而不是报错**：段号越界本身是另一条规则的
    判据面（且 `validate_bindings` 已在写入口拦），这里只负责"把命中
    指到一个合法坐标"，指不到就老实说"整篇"。
    """
    if not isinstance(binding, dict):
        return REVIEW_ANCHOR_KIND_WHOLE, None
    index = binding.get(BINDING_KEY_PARAGRAPH_INDEX)
    if isinstance(index, bool) or not isinstance(index, int):
        return REVIEW_ANCHOR_KIND_WHOLE, None
    if 0 <= index < len(subject.paragraphs):
        return REVIEW_ANCHOR_KIND_PARAGRAPH, subject.paragraphs[index].ref
    return REVIEW_ANCHOR_KIND_WHOLE, None


# ===========================================================================
# FACT —— 事实
# ===========================================================================


def fact_binding_claim_out_of_scope(subject: CheckSubject) -> RuleOutcome:
    """当前版的 `bindings[].claimId` 是否仍落在**当前生效基线**的 claim 范围内。

    ## 为什么这不是"把写入期校验再跑一遍"（R1 的正面回答）

    `content/schemas.validate_bindings` 用的是**写入当时**的基线判据域。
    而基线**之后可以前进**（WP22 R1 的实测次场景：9-15 批准"表活占比 62%"、
    9-20 知识更正为 60% ⇒ 新基线、新 claim 行）。自检这一刻重判，用的是
    **现在生效**的那条基线 —— 这是写入期拿不到的新信息，所以它不恒绿。

    `evidence` 里同时给出 `generatedAgainstBaselineId` 与
    `effectiveBaselineId`：两者不同就是"基线前进过"的机器可判证据。

    **无生效基线 ⇒ SKIPPED**（判据域为空，既不能判通过也不能判失败）。
    """
    if subject.effective_baseline_id is None:
        return skipped(SKIP_REASON_NO_EFFECTIVE_BASELINE)

    findings: list[FindingDraft] = []
    for binding in subject.bindings:
        claim_id, raw = _claim_id_of(binding)
        if raw is None:
            continue
        if claim_id is not None and claim_id in subject.baseline_claim_ids:
            continue
        anchor_kind, paragraph_ref = _paragraph_anchor(subject, binding)
        findings.append(
            FindingDraft(
                anchor_kind=anchor_kind,
                paragraph_ref=paragraph_ref,
                message=_truncate(
                    f"绑定引用的事实 {raw} 不在当前生效基线的 claim 范围内"
                    "（基线可能在本稿生成之后前进过，请复核这处事实）。"
                ),
                evidence={
                    "claimId": raw,
                    "claimIdParsed": claim_id is not None,
                    "effectiveBaselineId": str(subject.effective_baseline_id),
                    "generatedAgainstBaselineId": (
                        None
                        if subject.generated_against_baseline_id is None
                        else str(subject.generated_against_baseline_id)
                    ),
                    "baselineClaimCount": len(subject.baseline_claim_ids),
                },
            )
        )
    if findings:
        return failed(REVIEW_SEVERITY_BLOCKING, tuple(findings))
    return passed()


def fact_unbound_included_claim(subject: CheckSubject) -> RuleOutcome:
    """当前生效基线的 claim 里，有哪些**一条 binding 都没引用**。

    漏绑是常态（生成只绑用到的 claim，而 `validate_bindings` 反过来还有一条
    "绑满全部 claim = 视为没绑"的规则），所以这条是 `WARNING` 而不是阻断 ——
    它回答的是"这篇把基线里的事实用了几条"，是覆盖度而不是正确性。

    锚点是整篇：一条**没出现**的事实不存在"在第几段"。

    **无生效基线 ⇒ SKIPPED**（与上一条同理由）。
    """
    if subject.effective_baseline_id is None:
        return skipped(SKIP_REASON_NO_EFFECTIVE_BASELINE)

    bound: set[uuid.UUID] = set()
    for binding in subject.bindings:
        claim_id, _raw = _claim_id_of(binding)
        if claim_id is not None:
            bound.add(claim_id)

    unbound = sorted(
        str(claim_id) for claim_id in subject.baseline_claim_ids - bound
    )
    if not unbound:
        return passed()
    return failed(
        REVIEW_SEVERITY_WARNING,
        tuple(
            FindingDraft(
                anchor_kind=REVIEW_ANCHOR_KIND_WHOLE,
                message=_truncate(
                    f"基线事实 {claim_id} 在本稿里没有任何证据绑定引用。"
                ),
                evidence={
                    "claimId": claim_id,
                    "effectiveBaselineId": str(subject.effective_baseline_id),
                    "boundClaimCount": len(bound),
                    "baselineClaimCount": len(subject.baseline_claim_ids),
                },
            )
            for claim_id in unbound
        ),
    )


# ===========================================================================
# STRUCTURE —— 结构
# ===========================================================================


def structure_paragraph_count_below_image_plan(
    subject: CheckSubject,
) -> RuleOutcome:
    """正文段数是否够图位数（`MIN_PARAGRAPHS_FOR_IMAGE_PLAN`）。

    契约 §4：段落数不足时多余的图**回落最后一段** —— 合法，但那是退化
    （多张图指向同一段，运营看不出"这张图配的是哪句话"）。所以是提示而非阻断。
    """
    count = subject.paragraph_count
    if count >= MIN_PARAGRAPHS_FOR_IMAGE_PLAN:
        return passed()
    return failed(
        REVIEW_SEVERITY_WARNING,
        (
            FindingDraft(
                anchor_kind=REVIEW_ANCHOR_KIND_WHOLE,
                message=_truncate(
                    f"正文只切出 {count} 段，少于图位数 "
                    f"{MIN_PARAGRAPHS_FOR_IMAGE_PLAN}："
                    "多余的图会回落到最后一段（合法但退化）。"
                ),
                evidence={
                    "paragraphCount": count,
                    "imagesPerContent": MIN_PARAGRAPHS_FOR_IMAGE_PLAN,
                },
            ),
        ),
    )


def structure_version_without_blueprint(subject: CheckSubject) -> RuleOutcome:
    """这一版有没有挂叙事蓝图（`content_versions.blueprint_id`）。

    该列**可空且无 CHECK**，所以真能为 NULL（人工编辑追加的版本就常常没有
    蓝图）。没有蓝图不等于稿子不好，但"结构是按什么计划写的"从此无从溯源 ——
    因此是 `INFO`：登记事实，不干预审核。
    """
    if subject.blueprint_id is not None:
        return passed()
    return failed(
        REVIEW_SEVERITY_INFO,
        (
            FindingDraft(
                anchor_kind=REVIEW_ANCHOR_KIND_WHOLE,
                message=_truncate(
                    "这一版没有关联叙事蓝图，结构计划无从溯源"
                    "（人工编辑追加的版本常见）。"
                ),
                evidence={
                    "subjectVersionId": str(subject.subject_version_id),
                    "versionNo": subject.version_no,
                },
            ),
        ),
    )


# ===========================================================================
# MEDIA_CONSISTENCY —— 媒体一致性
# ===========================================================================


def media_paragraph_ref_stale(subject: CheckSubject) -> RuleOutcome:
    """每个图位的 `paragraph_ref` 是否仍在**当前正文**的段落 ref 集合里。

    `paragraph_ref` **无 FK、不落唯一约束**（契约 §4：它只是当前版本的投影），
    正文一改就会失配 ⇒ 这是全包最高频的真命中。

    ⚠️ 本规则比较的两个集合必须来自**同一套切段实现**：段 ref 集合由
    `media_bridge` 转发 `media/schemas.paragraph_refs_for_body` 生成，
    图位的 ref 由 media 在生成时用**同一个函数**写入。若本包自己切一次段，
    两个集合会**系统性**不等 ⇒ 这条规则变成恒红（R3）。
    """
    known = subject.paragraph_refs
    findings = tuple(
        FindingDraft(
            anchor_kind=REVIEW_ANCHOR_KIND_MEDIA,
            media_item_id=media.media_item_id,
            message=_truncate(
                f"第 {media.sort_order + 1} 张图的段落坐标 "
                f"{media.paragraph_ref} 已不对应当前正文的任何一段"
                "（正文改过之后图的位置失配）。"
            ),
            evidence={
                "mediaItemId": str(media.media_item_id),
                "paragraphRef": media.paragraph_ref,
                "sortOrder": media.sort_order,
                "knownParagraphRefCount": len(known),
            },
        )
        for media in subject.media_items
        if media.paragraph_ref is not None and media.paragraph_ref not in known
    )
    if findings:
        return failed(REVIEW_SEVERITY_WARNING, findings)
    return passed()


def media_item_not_ready(subject: CheckSubject) -> RuleOutcome:
    """每个图位是否已经出图（`media_items.state == READY`）。

    判的是"**不是** READY"而不是"是 PENDING 或 FAILED"：后者在 media
    将来多一个状态时会静默漏判（原型的「Live 3 生成中断」「失败」都是真实状态）。
    """
    findings = tuple(
        FindingDraft(
            anchor_kind=REVIEW_ANCHOR_KIND_MEDIA,
            media_item_id=media.media_item_id,
            message=_truncate(
                f"第 {media.sort_order + 1} 张图还没出图（当前状态 "
                f"{media.state}），这条内容尚不可交付。"
            ),
            evidence={
                "mediaItemId": str(media.media_item_id),
                "state": media.state,
                "expectedState": MEDIA_ITEM_STATE_READY,
                "sortOrder": media.sort_order,
            },
        )
        for media in subject.media_items
        if media.state != MEDIA_ITEM_STATE_READY
    )
    if findings:
        return failed(REVIEW_SEVERITY_WARNING, findings)
    return passed()


# ===========================================================================
# TECH_FORMAT —— 技术格式
# ===========================================================================


def _active_platform_limits(subject: CheckSubject) -> dict[str, int]:
    """从生效配置里取出本规则**认识的**数值上限。

    `runtime_settings` 的 value 形状是 `{键: 数 | null}`，所以 `null` 与
    "键不存在"都表示"这一项没有上限"。返回空字典 ⇒ 调用方按"未配置"处置。
    """
    if not subject.platform_limits_configured:
        return {}
    raw = subject.platform_limits or {}
    limits: dict[str, int] = {}
    for key in PLATFORM_LIMIT_KEYS:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        limits[key] = int(value)
    return limits


def tech_rendition_out_of_platform_limits(subject: CheckSubject) -> RuleOutcome:
    """READY 产物的宽 / 高 / 字节数是否在平台限制内。

    ## 未配置时 **SKIPPED**，不内置字面量

    平台限制是 `runtime_settings` 的**占位键**（`platform.mediaLimits`，
    OP-64 仍 OPEN：机制已建、实测值未到）。在本包内置 1440×1920 / 2MB
    就是造**第二处**平台限制真值 —— 而第二处真值与第一处的分叉是静默的。
    因此未配置 ⇒ `SKIPPED(PLATFORM_MEDIA_LIMITS_NOT_CONFIGURED)`，
    前端显示"未判"而不是"通过"。

    `image_format` **不判**：配置值的形状只能是数或 null，承载不了"允许的
    格式列表"。这是如实登记的能力缺口（说明文档 §3 的已知取舍），
    不是"忘了判"。
    """
    limits = _active_platform_limits(subject)
    if not limits:
        return skipped(SKIP_REASON_PLATFORM_MEDIA_LIMITS_NOT_CONFIGURED)

    measured = (
        (LIMIT_KEY_MAX_WIDTH, "width"),
        (LIMIT_KEY_MAX_HEIGHT, "height"),
        (LIMIT_KEY_MAX_BYTE_SIZE, "byte_size"),
    )
    findings: list[FindingDraft] = []
    for media in subject.media_items:
        for rendition in media.renditions:
            if rendition.state != MEDIA_RENDITION_STATE_READY:
                continue
            exceeded: dict[str, Any] = {}
            for limit_key, attribute in measured:
                limit = limits.get(limit_key)
                actual = getattr(rendition, attribute)
                if limit is None or actual is None:
                    continue
                if actual > limit:
                    exceeded[limit_key] = {"actual": actual, "limit": limit}
            if not exceeded:
                continue
            findings.append(
                FindingDraft(
                    anchor_kind=REVIEW_ANCHOR_KIND_MEDIA,
                    media_item_id=media.media_item_id,
                    message=_truncate(
                        f"第 {media.sort_order + 1} 张图的 {rendition.kind} 档位"
                        f"超出平台限制：{sorted(exceeded)}。"
                    ),
                    evidence={
                        "mediaItemId": str(media.media_item_id),
                        "renditionId": str(rendition.rendition_id),
                        "renditionKind": rendition.kind,
                        "imageFormat": rendition.image_format,
                        "exceeded": exceeded,
                    },
                )
            )
    if findings:
        return failed(REVIEW_SEVERITY_WARNING, tuple(findings))
    return passed()


# ===========================================================================
# OTHER —— 其他（禁用词）
# ===========================================================================


def other_banned_term_hit(subject: CheckSubject) -> RuleOutcome:
    """逐段扫禁用词（促销式号召 / 绝对化表述），命中落在**那一段**上。

    对应原型「禁用词检测 0 命中」那张卡片。`SOFT`：词表是启发式的，
    改词表会改结论，所以它**不可复现**（而 `HARD` 的定义是"同一输入必得
    同一结论"）—— 这正是 `rule_kind` 与严重度分两维的用处。

    只扫正文，**不扫标题**：标题没有段落坐标，混进来会让本规则同时产出
    两种锚点粒度。标题的禁用词检测登记为后续项。
    """
    findings: list[FindingDraft] = []
    for paragraph in subject.paragraphs:
        hits = tuple(term for term in BANNED_TERMS if term in paragraph.text)
        if not hits:
            continue
        findings.append(
            FindingDraft(
                anchor_kind=REVIEW_ANCHOR_KIND_PARAGRAPH,
                paragraph_ref=paragraph.ref,
                message=_truncate(
                    f"第 {paragraph.index + 1} 段命中禁用词：{list(hits)}。"
                ),
                evidence={
                    "paragraphIndex": paragraph.index,
                    "paragraphRef": paragraph.ref,
                    "terms": list(hits),
                },
            )
        )
    if findings:
        return failed(REVIEW_SEVERITY_WARNING, tuple(findings))
    return passed()


# ===========================================================================
# 三条恒 SKIPPED（见模块 docstring "为什么有三条恒 SKIPPED 的规则"）
# ===========================================================================


def tone_model_judgement(subject: CheckSubject) -> RuleOutcome:
    """语气类：**恒 SKIPPED**（需要模型判定，本包 MVP 纯本地）。

    参数保留但不读：签名统一是注册表能机械遍历的前提
    （`rules.py` 的导入期 assert 与"每条 evaluator 真被调用过"的活性下界
    都依赖同一个签名）。
    """
    _ = subject
    return skipped(SKIP_REASON_MODEL_RULE_NOT_ENABLED)


def duplicate_cross_item_similarity(subject: CheckSubject) -> RuleOutcome:
    """重复类：**恒 SKIPPED**（跨篇相似度归 WP33）。

    本包**一行相似度算法都不写**：仲裁项④ 的分界不是"算法在谁家"，
    而是"输入是一篇还是一批"。两边各建一份是被明令禁止的那件事。
    """
    _ = subject
    return skipped(SKIP_REASON_CROSS_ITEM_SCOPE_OUT_OF_PACKAGE)


def scene_linkage_unavailable(subject: CheckSubject) -> RuleOutcome:
    """场景类：**恒 SKIPPED**（schema 无"内容 → 场景"的机器可判关联列）。

    `content_items` 没有 scene 列；唯一相关的 `narrative_blueprints.usage_scene`
    是自由文本，与 `plan_drafts.scene_items` 的 key 之间没有任何可对账的联系。
    伪造"与配置场景一致"就是凭空声称。
    """
    _ = subject
    return skipped(SKIP_REASON_NO_SCENE_LINKAGE_IN_SCHEMA)


__all__ = [
    "BINDING_KEY_CLAIM_ID",
    "BINDING_KEY_PARAGRAPH_INDEX",
    "duplicate_cross_item_similarity",
    "fact_binding_claim_out_of_scope",
    "fact_unbound_included_claim",
    "media_item_not_ready",
    "media_paragraph_ref_stale",
    "other_banned_term_hit",
    "scene_linkage_unavailable",
    "structure_paragraph_count_below_image_plan",
    "structure_version_without_blueprint",
    "tech_rendition_out_of_platform_limits",
    "tone_model_judgement",
]
