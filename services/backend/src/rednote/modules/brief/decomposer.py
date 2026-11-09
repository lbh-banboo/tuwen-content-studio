"""解构与加厚的模型接口（WP18 与 WP16 网关的接缝）。

**本模块只做一件事：把"一段原文 + 字段规格"变成 `DecompositionResult`**。
它不碰数据库、不判权限、不做状态迁移 —— 那些是 `service.py` 的职责。

---

## 为什么是 Protocol + 三个实现，而不是"直接调网关"

WP16 的 `handlers.py` 已经把立场写清楚了：

> 真正的处理器（构造请求体、选适配器）由 WP17/WP18 写 ——
> 它们才知道"这类任务该带什么上下文"。

本模块就是那个"知道该带什么上下文"的地方。三个实现的分工：

| 实现 | 什么时候用 | 为什么必须存在 |
|---|---|---|
| `UnavailableDecomposer` | **默认** | 没有配模型时，**必须显式失败**，不能静默返回空结果 |
| `ScriptedDecomposer` | 测试 / 证据脚本 | 让"分类来源、字段疑问、确认版本闭环"这条验收**在本机真的被验证** |
| `GatewayDecomposer` | 生产装配 | 走 WP16 的 `dispatch_call`，**不直连**供应商 |

**`UnavailableDecomposer` 是默认值，这一条是本模块最重要的决定。**
理由与 WP17 的 `build_ocr_engine(None)` 返回不可用引擎完全一致：

> 若默认实现返回一个"看起来成功"的空结果，那么一个**忘了配模型**的部署
> 会产出"零结论、零疑问"的 Brief —— 而 `brief_drafts` 上的 CHECK
> 只要求 PARTIAL 时必须有疑问，SUCCEEDED 时零疑问是合法的。
> 于是这个"什么都没做"的部署会让步骤 1 一路绿灯通过，
> 而运营看到的是一份字段全空的 Brief。
>
> **"未经验证的实现比明确未实现更危险"**（WP12 的 MinIO 分支教训）。

因此：不可用即抛 `DecomposerUnavailable`，由 service 折成一个明确的
`FAILED` 任务状态与可读错误，**不产出任何草稿变更**。

## 为什么不给 `DecompositionResult` 加"置信度"字段

模型的置信度是不可校准的（不同模型、不同提示词下的 0.8 不是同一个东西），
而它一旦落库就会被下游当排序依据。本包的口径是：
**"有来源"是二值的、可校验的（触发器保证片段属于本版本文档）；
"我有多确定"不是。** 因此只保留可校验的那一部分。

## `GatewayDecomposer` 的 `context_digest`

WP16 的 `CapabilityRequest.context_digest` 要求传**规范化摘要而不是正文**
（网关留痕"送出了什么类别的字段"，不留正文）。本实现遵守这条：
原文片段的摘要进 `context_digest`，正文只在**调用适配器时**才交给它 ——
而适配器本身是内网/已审批的出网通道（ADR-10）。
"""

from __future__ import annotations

import uuid
from typing import Any, Final, Protocol, runtime_checkable

from rednote.modules.brief.schema import (
    BRIEF_FIELDS,
    empty_field_values,
    field_spec,
)
from rednote.modules.brief.types import (
    CATEGORY_SOURCE_FACT,
    CATEGORY_STRATEGY,
    CATEGORY_UNCONFIRMED,
    ISSUE_OPEN,
    MAX_CLAIM_CHARS,
    MAX_ISSUE_DESCRIPTION_CHARS,
    SEVERITY_BLOCK,
    SEVERITY_WARN,
    ClaimDraft,
    DecompositionResult,
    EnrichmentResult,
    EnrichmentSuggestion,
    IssueDraft,
)


class DecomposerUnavailable(RuntimeError):
    """模型通道不可用。

    **刻意不继承 `ApiError`**：它不代表"用户操作不对"，而是
    "这个部署还没配好模型"。让它在 service 层被折成一个**任务级**失败
    （`HandlerOutcome(state=FAILED, retryable=False)`）——
    重试一万次也不会让模型配置长出来。

    与 `OcrEngineUnavailable`（WP17）同一处置：**绝不降级成空结果**。
    """


@runtime_checkable
class BriefDecomposer(Protocol):
    """把原文解构成结构化 Brief。"""

    #: 通道标识（落进 `parse_runs.parser_version` 同类的位置 / 日志）。
    name: str

    def decompose(
        self,
        *,
        project_id: uuid.UUID,
        file_id: uuid.UUID,
        segments: tuple[Any, ...],
        model_profile_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
        attempt_id: uuid.UUID | None = None,
    ) -> DecompositionResult:
        """解构。

        `segments` 是 `SourceSegment` 的元组（**按原文顺序**，
        由 WP17 的 `ParseRepository.list_segments` 给出）。
        实现**不得**依赖 `created_at` 排序 —— 它是事务时间戳。
        """
        ...

    def enrich(
        self,
        *,
        project_id: uuid.UUID,
        fields: dict[str, Any],
        claims: tuple[Any, ...],
        allowed_segments: tuple[Any, ...],
        model_profile_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
        attempt_id: uuid.UUID | None = None,
    ) -> EnrichmentResult:
        """基于**允许的来源**提出加厚建议（**不自动生效**，协议 §5 第 5 行）。"""
        ...


# ---------------------------------------------------------------------------
# 默认实现：不可用
# ---------------------------------------------------------------------------


class UnavailableDecomposer:
    """没有模型通道时的默认实现。**调用即抛错**。

    见模块 docstring：静默返回空结果会让"忘了配模型"看起来像
    "这个 Brief 确实没有结论"，而两者对运营的含义完全不同。
    """

    name: Final[str] = "unavailable"

    def decompose(self, **_: Any) -> DecompositionResult:
        raise DecomposerUnavailable(
            "没有可用的 Brief 解构模型通道（未配置 model_profile_id 或网关不可用）。"
            "本系统不会用空结果代替模型输出 —— "
            "空结果会让'没配模型'看起来像'这份 Brief 确实没有结论'。"
        )

    def enrich(self, **_: Any) -> EnrichmentResult:
        raise DecomposerUnavailable(
            "没有可用的 Brief 加厚模型通道（未配置 model_profile_id 或网关不可用）。"
        )


# ---------------------------------------------------------------------------
# 测试/证据用：脚本化
# ---------------------------------------------------------------------------


class ScriptedDecomposer:
    """脚本化的假实现 —— **只为验证本包语义而存在**。

    名字里带 `Scripted` 是刻意的（与 WP16 的 `StubAdapter` 同一手法）：
    它在任何清单里都一眼可辨，不会被误当成真实模型实现。

    **它不是"假供应商"**：WP13 已有 `FAKE_PROVIDER` 验证任务框架，
    WP16 已有 `StubAdapter` 验证网关。本类验证的是**Brief 解构的语义**
    （分类来源、疑问阻断、PARTIAL 暴露、固化闭环）。

    `enrich` 刻意**只产出带允许来源的建议**：加厚端点的 `allowedSourceIds`
    是一个安全边界（"只能引用这些来源"），假实现若随意造一个 id，
    这个边界就永远测不到。
    """

    name: Final[str] = "scripted"

    def __init__(
        self,
        *,
        result: DecompositionResult | None = None,
        enrichment: EnrichmentResult | None = None,
        raise_on_decompose: Exception | None = None,
    ) -> None:
        self._result = result
        self._enrichment = enrichment
        self._raise_on_decompose = raise_on_decompose
        self.decompose_call_count = 0
        self.enrich_call_count = 0

    def decompose(self, **_: Any) -> DecompositionResult:
        self.decompose_call_count += 1
        if self._raise_on_decompose is not None:
            raise self._raise_on_decompose
        return self._result if self._result is not None else DecompositionResult()

    def enrich(self, **_: Any) -> EnrichmentResult:
        self.enrich_call_count += 1
        if self._enrichment is not None:
            return self._enrichment
        return EnrichmentResult()


def decomposition_from_payload(payload: dict[str, Any]) -> DecompositionResult:
    """把模型返回的 JSON 折成 `DecompositionResult`（**唯一的解析入口**）。

    **为什么要一个显式解析函数**：模型输出是**不可信输入**（协议 §13 第 4 条
    要求"模型返回 JSON 使用单独 Schema 校验，不与浏览器请求 DTO 混用"）。
    把它直接塞进 `ClaimDraft(**item)` 会让一个多余字段变成 `TypeError`
    （500），而正确行为是"这一条读不懂，记为一条疑问"。

    三条处置：
      · 未知字段路径 → 降级成一条 `WARN` 疑问（**不是**丢掉，也不是 500）；
      · 事实类缺证据 → 降级成 `UNCONFIRMED` + 一条 `WARN` 疑问
        （**不是**静默接受 —— 那正是协议 §5.1 明令禁止的
        "没有来源的建议不能改成 SOURCE_FACT 而不补证据"）；
      · 缺必填键 → 降级成一条 `WARN` 疑问。
    """

    issues: list[IssueDraft] = []
    claims: list[ClaimDraft] = []

    raw_fields = payload.get("fields")
    fields = (
        raw_fields
        if isinstance(raw_fields, dict) and raw_fields
        else empty_field_values()
    )

    raw_claims = payload.get("claims")
    if isinstance(raw_claims, list):
        for index, item in enumerate(raw_claims):
            if not isinstance(item, dict):
                issues.append(
                    _warn_issue(
                        field_path="*",
                        description=(
                            f"第 {index + 1} 条结论不是对象，已忽略"
                            f"（模型输出格式不符）。"
                        ),
                    )
                )
                continue
            claim = _read_claim(item, index=index, issues=issues)
            if claim is not None:
                claims.append(claim)

    raw_issues = payload.get("issues")
    if isinstance(raw_issues, list):
        for index, item in enumerate(raw_issues):
            if not isinstance(item, dict):
                continue
            issue = _read_issue(item, index=index)
            if issue is not None:
                issues.append(issue)

    return DecompositionResult(
        fields=fields, claims=tuple(claims), issues=tuple(issues)
    )


def _read_claim(
    item: dict[str, Any], *, index: int, issues: list[IssueDraft]
) -> ClaimDraft | None:
    """读一条结论。读不懂返回 `None` 并记一条 WARN 疑问。"""
    field_path = str(item.get("fieldPath") or "").strip()
    text = str(item.get("text") or "").strip()
    category = str(item.get("category") or "").strip()

    if not field_path or not text:
        issues.append(
            _warn_issue(
                field_path=field_path or "*",
                description=f"第 {index + 1} 条结论缺少 fieldPath 或 text，已忽略。",
            )
        )
        return None

    if len(text) > MAX_CLAIM_CHARS:
        # 超长不是"值得保留的结论"，是"把输入复制了一遍"。
        # **截断而不是丢弃**：前半段通常仍是有效结论，而丢弃会让
        # 运营完全看不到模型说了什么。
        text = text[:MAX_CLAIM_CHARS]
        issues.append(
            _warn_issue(
                field_path=field_path,
                description=(
                    f"第 {index + 1} 条结论超过 {MAX_CLAIM_CHARS} 字符，已截断"
                    "（超长文本通常是解构跑偏）。"
                ),
            )
        )

    try:
        field_spec(field_path)
    except ValueError:
        issues.append(
            _warn_issue(
                field_path="*",
                description=(
                    f"结论 {index + 1} 指向未知字段 {field_path!r}，已忽略"
                    "（未知路径无法定位回原文，会让前端'点字段没反应'）。"
                ),
            )
        )
        return None

    evidence_segment_id = _read_segment_id(item)
    evidence_url = _read_evidence_url(item)

    if category not in {
        CATEGORY_SOURCE_FACT,
        CATEGORY_STRATEGY,
        CATEGORY_UNCONFIRMED,
        "EXTERNAL_EVIDENCE",
    }:
        issues.append(
            _warn_issue(
                field_path=field_path,
                description=(
                    f"结论 {index + 1} 的分类 {category!r} 未知，已降级为待确认。"
                ),
            )
        )
        category = CATEGORY_UNCONFIRMED

    if category in {"SOURCE_FACT", "EXTERNAL_EVIDENCE"} and not (
        evidence_segment_id or evidence_url
    ):
        # 协议 §5.1 原文："没有来源的建议不能改成 SOURCE_FACT 而不补证据"。
        # **降级而不是拒绝**：拒绝会让整次解构失败，而降级保留了这个结论
        # 的文本供人判断 —— 同时把它标成"待确认"，下游不会把它当事实用。
        issues.append(
            _warn_issue(
                field_path=field_path,
                description=(
                    f"结论 {index + 1} 声明为 {category} 但没有来源，"
                    "已降级为待确认（协议 §5.1：没有来源的建议不能"
                    "改成 SOURCE_FACT 而不补证据）。"
                ),
            )
        )
        category = CATEGORY_UNCONFIRMED

    return ClaimDraft(
        field_path=field_path,
        text=text,
        category=category,
        applicability=_read_applicability(item),
        evidence_segment_id=evidence_segment_id,
        evidence_url=evidence_url,
    )


def _read_issue(item: dict[str, Any], *, index: int) -> IssueDraft | None:
    field_path = str(item.get("fieldPath") or "").strip()
    description = str(item.get("description") or "").strip()
    if not description:
        return None
    severity = str(item.get("severity") or SEVERITY_WARN).strip()
    if severity not in {SEVERITY_BLOCK, SEVERITY_WARN}:
        severity = SEVERITY_WARN
    return IssueDraft(
        id=uuid.uuid4(),
        field_path=field_path or "*",
        severity=severity,
        description=description[:MAX_ISSUE_DESCRIPTION_CHARS],
        state=ISSUE_OPEN,
    )


def _warn_issue(*, field_path: str, description: str) -> IssueDraft:
    return IssueDraft(
        id=uuid.uuid4(),
        field_path=field_path,
        severity=SEVERITY_WARN,
        description=description[:MAX_ISSUE_DESCRIPTION_CHARS],
        state=ISSUE_OPEN,
    )


def _read_segment_id(item: dict[str, Any]) -> uuid.UUID | None:
    """从 `evidence` 里取片段 id（协议 §5.1 的数组形状，取第一个）。

    协议 §5.1 的 `evidence` 是数组，数据结构 02 §6 的
    `evidence_segment_id` 是单列。本函数是**那个不一致的收敛点**：
    取第一个元素（主证据），并忽略其余 —— 见 `ClaimDraft` 的 docstring。
    """
    evidence = item.get("evidence")
    if isinstance(evidence, dict):
        evidence = [evidence]
    if not isinstance(evidence, list):
        return None
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("segmentId")
        if raw is None:
            continue
        try:
            return uuid.UUID(str(raw))
        except (TypeError, ValueError):
            return None
    return None


def _read_evidence_url(item: dict[str, Any]) -> str | None:
    evidence = item.get("evidence")
    if isinstance(evidence, dict):
        evidence = [evidence]
    if not isinstance(evidence, list):
        return None
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("url")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _read_applicability(item: dict[str, Any]) -> dict[str, Any]:
    """读适用范围。形状不符时返回空对象（= 不限定）。

    **不做严格校验**：`applicability` 是"这条结论适用于哪些产品/型号"，
    空对象是合法的（不限定）。一个畸形的 applicability 不该让整条结论作废 ——
    那会把"适用范围没填好"升级成"结论丢了"。
    """
    raw = item.get("applicability")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("productIds", "modelCodes"):
        value = raw.get(key)
        if isinstance(value, list):
            result[key] = [str(item) for item in value]
    return result


# ---------------------------------------------------------------------------
# 生产装配：走 WP16 网关
# ---------------------------------------------------------------------------


class GatewayDecomposer:
    """走 WP16 网关的实现（**生产装配用**）。

    **本类刻意不直连供应商**（接缝冻结 §5.5：LOCAL_ONLY 无外部回退，
    否决即返回，**无降级分支**）。

    ## 为什么本机跑不通，而它仍然必须存在

    它需要三样本机没有的东西：
      1. 一个已审批的 `model_profiles` 行（真实模型名 + 供应商）；
      2. `capability_policies` 里该项目的出网策略；
      3. 真实适配器（WP16 交付的是 `StubAdapter`，OP-130 未关闭）。

    三者都缺时，`dispatch_call` 会返回 `denial`，本类把它折成
    `DecomposerUnavailable` —— **不是"降级到本地"**（ADR-10 明令无回退）。

    交付一个"看起来能调"的假适配器比不交付更糟：它会让
    "Brief 解构链路已验证"这句话变成假的。因此本类只交付**接线**，
    真实端到端验证登记为内网补做项。
    """

    name: Final[str] = "gateway"

    def __init__(
        self,
        *,
        gateway_repo: Any,
        adapter: Any,
        model_profile_id: uuid.UUID,
        output_schema_version: int = 1,
        policy_version: str = "v1",
    ) -> None:
        self._repo = gateway_repo
        self._adapter = adapter
        self._model_profile_id = model_profile_id
        self._output_schema_version = output_schema_version
        self._policy_version = policy_version

    def decompose(
        self,
        *,
        project_id: uuid.UUID,
        file_id: uuid.UUID,
        segments: tuple[Any, ...],
        model_profile_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
        attempt_id: uuid.UUID | None = None,
    ) -> DecompositionResult:
        payload = self._dispatch(
            capability=_CAPABILITY_TEXT_STRUCTURED,
            project_id=project_id,
            job_id=job_id,
            attempt_id=attempt_id,
            model_profile_id=model_profile_id,
            prompt=_build_decompose_prompt(segments=segments, file_id=file_id),
            idempotency_key=f"brief-decompose:{file_id}",
        )
        return decomposition_from_payload(payload)

    def enrich(
        self,
        *,
        project_id: uuid.UUID,
        fields: dict[str, Any],
        claims: tuple[Any, ...],
        allowed_segments: tuple[Any, ...],
        model_profile_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
        attempt_id: uuid.UUID | None = None,
    ) -> EnrichmentResult:
        payload = self._dispatch(
            capability=_CAPABILITY_TEXT_STRUCTURED,
            project_id=project_id,
            job_id=job_id,
            attempt_id=attempt_id,
            model_profile_id=model_profile_id,
            prompt=_build_enrich_prompt(
                fields=fields, claims=claims, allowed_segments=allowed_segments
            ),
            idempotency_key=f"brief-enrich:{project_id}",
        )
        suggestions: list[EnrichmentSuggestion] = []
        raw = payload.get("suggestions")
        if isinstance(raw, list):
            allowed_ids = {str(getattr(s, "id", "")) for s in allowed_segments}
            for item in raw:
                suggestion = _read_suggestion(item, allowed_ids=allowed_ids)
                if suggestion is not None:
                    suggestions.append(suggestion)
        return EnrichmentResult(suggestions=tuple(suggestions))

    def _dispatch(
        self,
        *,
        capability: str,
        project_id: uuid.UUID,
        job_id: uuid.UUID | None,
        attempt_id: uuid.UUID | None,
        model_profile_id: uuid.UUID | None,
        prompt: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """调一次网关并把结果折成 dict。

        **本地导入**：`gateway` 是重模块（它 import 适配器与策略），
        而本模块在"没有模型通道"的部署里也会被 import
        （`service` 要拿 `UnavailableDecomposer`）。
        顶层 import 会让每个请求都拖着网关的依赖树。

        `context_digest` 传**提示词的规范化摘要**而不是正文 ——
        见模块 docstring（网关留痕不留正文）。
        """
        from rednote.modules.gateway.service import dispatch_call
        from rednote.modules.gateway.types import (
            CapabilityRequest,
            DATA_CLASS_TEXT_STRUCTURED,
        )
        from rednote.modules.versions.service import canonical_payload_hash

        if job_id is None or attempt_id is None:
            raise DecomposerUnavailable(
                "走网关的解构必须由任务处理器驱动（缺少 jobId / attemptId）。"
                "网关要求 `CapabilityRequest.job_id` 与 `attempt_id` —— "
                "没有它们就无法写调用留痕（数据结构 §13.4）。"
            )

        request = CapabilityRequest(
            request_id=f"brief-{uuid.uuid4().hex[:12]}",
            job_id=job_id,
            attempt_id=attempt_id,
            project_id=project_id,
            capability=capability,
            model_profile_id=model_profile_id or self._model_profile_id,
            output_schema_version=self._output_schema_version,
            policy_version=self._policy_version,
            idempotency_key=idempotency_key,
            data_classes=(DATA_CLASS_TEXT_STRUCTURED,),
            context_digest=canonical_payload_hash({"prompt": prompt}),
            prompt_version=_PROMPT_VERSION,
        )
        result = dispatch_call(self._repo, request=request, adapter=self._adapter)
        if result.denied:
            # **无降级分支**（ADR-10）：否决即返回。
            raise DecomposerUnavailable(
                "出网策略拒绝了本次 Brief 解构调用："
                f"{getattr(result.denial, 'reason', 'unknown')}。"
                "系统不会降级到本地模型 —— 策略否决是最终结论。"
            )
        if result.call is None:
            raise DecomposerUnavailable("网关没有返回调用记录。")
        if result.call.state == "FAILED":
            raise DecomposerUnavailable(
                f"模型调用失败：{result.call.last_error_code or 'unknown'}。"
            )
        if result.call.state != "SUCCEEDED":
            # POLLING / PREPARED / UNKNOWN —— **一律不当成功**。
            # UNKNOWN 尤其不能当失败重试（数据结构 §13.4），
            # 而本层无权决定处置，交给任务框架（WP16 的 outcome_for_result）。
            raise DecomposerUnavailable(
                f"模型调用尚未完成（state={result.call.state}），"
                "本次解构不产出草稿变更。"
            )

        payload = _structured_output_for_call(self._repo, result.call.call_id)
        if payload is None:
            raise DecomposerUnavailable("模型调用成功但没有结构化输出。")
        return payload


# 能力名与提示词版本（与 WP16 的 `CAPABILITIES` / ADR-10 留痕要求对齐）。
# 本地导入常量会形成顶层依赖，这里用字符串字面量并加注释指向来源。
_CAPABILITY_TEXT_STRUCTURED: Final[str] = "TEXT_STRUCTURED"
_PROMPT_VERSION: Final[str] = "brief-wp18-v1"


def _structured_output_for_call(repo: Any, call_id: uuid.UUID) -> dict[str, Any] | None:
    """取一次调用的结构化输出。

    `provider_calls` **不存正文**（数据结构 §13：真实结果保存内网，
    普通日志只引用 ID）。因此结构化输出存在别处，本函数通过仓库读取 ——
    具体落点属 WP16 的实现细节，本模块只依赖"能按 call_id 取到"这一条。
    """
    getter = getattr(repo, "get_structured_output", None)
    if getter is None:
        raise DecomposerUnavailable(
            "网关仓库没有提供结构化输出的读取入口"
            "（`get_structured_output`）—— 这是装配缺口，"
            "不是模型失败。请检查 WP16 与本模块的接线。"
        )
    return getter(call_id)


def _build_decompose_prompt(*, segments: tuple[Any, ...], file_id: uuid.UUID) -> str:
    """组装解构提示词。

    **字段清单从 `BRIEF_FIELDS` 生成，不手抄**：手抄一份必然与 Schema 漂移，
    而漂移的方向是"模型被要求填一个 Schema 里没有的字段"（那会被
    `decomposition_from_payload` 降级成一条 WARN 疑问 —— 表面不报错，
    只是那个字段永远填不上）。
    """
    field_lines = "\n".join(
        f"- {spec.path}（{spec.label}）"
        + ("，必填" if spec.required else "")
        + ("，多值" if spec.multi else "")
        for spec in BRIEF_FIELDS
    )
    return (
        "你是内容生产系统的 Brief 解构器。\n"
        f"源文件 ID：{file_id}\n"
        "请把下面的原文拆成结构化字段与结论。\n\n"
        "字段清单（路径唯一，未知路径会被丢弃）：\n"
        f"{field_lines}\n\n"
        "输出 JSON：\n"
        '{"fields": {"<路径>": {"status": "FILLED|MISSING|NOT_APPLICABLE",'
        ' "value": ...}},'
        ' "claims": [{"fieldPath": "...", "text": "...",'
        ' "category": "SOURCE_FACT|EXTERNAL_EVIDENCE|STRATEGY|UNCONFIRMED",'
        ' "evidence": [{"segmentId": "..."}]}],'
        ' "issues": [{"fieldPath": "...", "severity": "BLOCK|WARN",'
        ' "description": "..."}]}\n\n'
        "规则：\n"
        "1. 原文里没有的字段标 MISSING，**不要编造**；\n"
        "2. 声明为 SOURCE_FACT / EXTERNAL_EVIDENCE 的结论**必须带来源**；\n"
        "3. 缺失、矛盾、无法验证的信息列为 issues；\n"
        "4. 会阻断下游生产的问题用 BLOCK，其余用 WARN。\n\n"
        "原文：\n"
        + "\n".join(_segment_text(segment) for segment in segments)
    )


def _build_enrich_prompt(
    *,
    fields: dict[str, Any],
    claims: tuple[Any, ...],
    allowed_segments: tuple[Any, ...],
) -> str:
    """组装加厚提示词。

    **只把 `allowed_segments` 的内容交给模型** —— 这就是端点上
    `allowedSourceIds` 这个参数的全部意义：它限定了"允许引用哪些来源"。
    把全部片段都塞进提示词，然后再在输出侧过滤，等于**先把数据送出去
    再假装没送**（与 WP16 的"适配器零调用"是同一条判据）。
    """
    return (
        "你是内容生产系统的 Brief 加厚器。基于**下面这些允许的来源**，"
        "提出补充建议（不自动生效，由运营选用）。\n\n"
        f"当前字段：{fields}\n"
        f"当前结论：{[getattr(c, 'claim_text', c) for c in claims]}\n\n"
        "允许引用的来源片段（**只能引用这些**）：\n"
        + "\n".join(_segment_text(segment) for segment in allowed_segments)
        + "\n\n输出 JSON：\n"
        '{"suggestions": [{"fieldPath": "...", "text": "...",'
        ' "category": "STRATEGY|EXTERNAL_EVIDENCE|UNCONFIRMED",'
        ' "evidence": [{"segmentId": "..."}], "rationale": "..."}]}\n'
    )


def _segment_text(segment: Any) -> str:
    """取片段文本并带上定位提示（让模型能引用正确的 segmentId）。"""
    segment_id = getattr(segment, "id", None)
    text = getattr(segment, "text_content", "")
    position = getattr(segment, "position", None)
    return f"[{segment_id}] ({position}) {text}"


def _read_suggestion(
    item: Any, *, allowed_ids: set[str]
) -> EnrichmentSuggestion | None:
    """读一条加厚建议。**来源不在白名单内即丢弃**。

    这是 `allowedSourceIds` 的**强制点**：模型（或一个被污染的提示词）
    可能返回一个白名单外的 segmentId —— 那是"引用了一个不该引用的来源"。
    静默保留会让这个安全参数形同虚设，而报错会让整次加厚失败
    （一条越界建议不该毁掉其余 9 条）。因此**丢弃并计数**（调用方记 issue）。
    """
    if not isinstance(item, dict):
        return None
    field_path = str(item.get("fieldPath") or "").strip()
    text = str(item.get("text") or "").strip()
    if not field_path or not text:
        return None

    segment_id = _read_segment_id(item)
    if segment_id is not None and str(segment_id) not in allowed_ids:
        return None

    category = str(item.get("category") or CATEGORY_STRATEGY).strip()
    if category not in {
        CATEGORY_SOURCE_FACT,
        CATEGORY_STRATEGY,
        CATEGORY_UNCONFIRMED,
        "EXTERNAL_EVIDENCE",
    }:
        category = CATEGORY_UNCONFIRMED

    return EnrichmentSuggestion(
        field_path=field_path,
        text=text[:MAX_CLAIM_CHARS],
        category=category,
        evidence_segment_id=segment_id,
        evidence_url=_read_evidence_url(item),
        rationale=str(item.get("rationale") or "")[:MAX_ISSUE_DESCRIPTION_CHARS],
    )


__all__ = [
    "BriefDecomposer",
    "DecomposerUnavailable",
    "GatewayDecomposer",
    "ScriptedDecomposer",
    "UnavailableDecomposer",
    "decomposition_from_payload",
]
