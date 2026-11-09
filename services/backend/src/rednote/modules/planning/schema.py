"""WP24 的请求/响应 DTO。

**本模块的全部意义是一件事**：把 `types.EffectiveSetting` /
`service.DraftView` / `allocation.AllocationResult` 这三个跨层值对象
翻成协议形状，并让 **`EffectiveSetting` 的四条一起出现在响应里**（§3.6.4）。

---

## 一、★ `EffectiveSettingBody` 的四个字段必须一起出现

| 字段 | 类型 | 缺失的后果 |
|---|---|---|
| `value` | object 或 null | 前端没法显示"本项目实际用多少" |
| `source` | enum 或 null | 不知道生效值从哪来 —— 改全局时无法判断"这个项目会不会跟着变" |
| `globalValue` | object 或 null | **看不见差在哪**。前端那句「本项目已覆盖 · 当前 5 条 · 全局 10 条」需要它 |
| `diverged` | bool | **没有可断言的对象** —— 分叉就成了"要靠人记得去看"的状态 |

**为什么 `source` 与 `diverged` 不是冗余的**：`source='MANUAL'` 且
`diverged=false` 是一个**真实且常见**的状态（运营覆盖成了与全局相同的值）。
那时"已覆盖"与"有差异"是两件事，前端文案也不同
（前者提示"这个项目不再跟随全局"，后者提示"值不一样"）。
用 `source == 'MANUAL'` 去推断"有差异"会在这条路径上**误报**。

## 二、`configured` 为什么也要暴露

`runtime_settings` 里有**占位键**（`platform.mediaLimits`，OP-64 等实测）：
它的合法状态就是"没有值"。前端必须能区分：

    configured=false                    → 显示"尚未配置"（灰字，不可点）
    configured=true, value={"x": null}  → 显示"已配置为空"（黑字，可点）

`value=null` 无法区分这两者 ⇒ 必须带上 `configured`（与 `EffectiveSetting`
的 docstring 同一条理由）。

## 三、为什么写完 DTO 还要再校验一遍

`value` 在 DTO 里是 `dict[str, Any]`（宽松）而不是三个具体模型。
**这是有意的**：`runtime_settings.value` 的形状**按 key 分派**
（`batch.limits` 是 `{TRIAL,SCALE,BULK}`、`platform.mediaLimits` 是嵌套 map），
用 `Union` 会让 OpenAPI 里出现 `oneOf` 而前端生成器常常退化。
真正的校验在 `settings_service.validate_setting_value`（**唯一入口**），
它在服务层按 key 分派 —— DTO 层再写一遍就多了一处会漂移的副本。

## 四、三个 `of()` 类方法的存在理由相同

`SettingValueBody.of` / `AllocationBody.of` / `SubmittedPlanBody.of` 都**显式
逐个字段赋值**，不用 `model_validate(obj, from_attributes=True)`。
理由不是风格：`from_attributes` 按**属性名**取值，而本模块的字段名是
snake_case、响应键是 camelCase 别名 —— 看起来"能工作"只是因为
两套名字恰好对得上（`global_value` ↔ `GlobalValue`）。
一旦某个字段改名（比如 `global_value` → `global_snapshot`），
`from_attributes` 会**静默取到默认值**（`None`），响应里那个字段就消失了，
而没有任何错误。显式赋值让这种不一致在**导入时**就报 `AttributeError`。
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from rednote.modules.planning.service import DraftView
from rednote.modules.planning.types import (
    BATCH_KINDS,
    AllocationResult,
    EffectiveSetting,
)
from rednote.modules.versions.service import RegisteredVersion

# 所有 DTO 的公共配置：
#   · `populate_by_name` —— 内部代码可以用 snake_case 构造，输出仍是 camelCase
#   · `extra="forbid"` —— 多传字段直接 422，而不是静默忽略。
#     不做的话，前端把 `sceneItems` 拼成 `sceneitems` 会得到"保存成功但没生效"。
_DTO_CONFIG = ConfigDict(populate_by_name=True, extra="forbid")


# ---------------------------------------------------------------------------
# 配置（runtime_settings）
# ---------------------------------------------------------------------------


class SettingValueBody(BaseModel):
    """一处生效值及其来源（`EffectiveSetting` 的协议形态）。

    **`field_serializer` 不在这里**：`uuid.UUID` 与 `datetime` 的序列化
    由 FastAPI 的默认 JSON 编码器处理，本模块不重复定义。
    """

    model_config = _DTO_CONFIG

    key: str
    configured: bool
    value: dict[str, Any] | None = None
    source: str | None = None
    global_value: dict[str, Any] | None = Field(default=None, alias="globalValue")
    diverged: bool
    revision_no: int | None = Field(default=None, alias="revisionNo")
    project_id: uuid.UUID | None = Field(default=None, alias="projectId")

    @classmethod
    def of(cls, setting: EffectiveSetting) -> SettingValueBody:
        """从跨层值对象构造（**唯一的转换点**）。

        不做成 `model_validate(setting)`：`EffectiveSetting` 是 frozen
        dataclass，pydantic 的 from_attributes 会按**属性名**取值，
        而 DTO 的字段名是 snake_case、别名字段（`globalValue` 等）会
        因为 `populate_by_name` 也从属性名取值 —— 看起来能工作，
        但一旦将来给某个字段改名，两处会各自漂移而**不报错**。
        显式逐个赋值的代价是 7 行，收益是改名时 IDE 与类型检查都会报。
        """
        return cls(
            key=setting.key,
            configured=setting.configured,
            value=setting.value,
            source=setting.source,
            global_value=setting.global_value,
            diverged=setting.diverged,
            revision_no=setting.revision_no,
            project_id=setting.project_id,
        )


class ChangeGlobalSettingBody(BaseModel):
    """`PUT /settings/global/{key}` 的请求体。

    `value` 允许 `null`（清空），但**只有占位键允许**
    （`PLACEHOLDER_SETTING_KEYS`）—— 那条判定在服务层，
    因为它依赖 `key`，而 DTO 层不该知道"哪个 key 是占位键"
    （知道了就是第二处副本）。
    """

    model_config = _DTO_CONFIG

    value: dict[str, Any] | None = None
    reason: str = Field(description="变更理由（ADR-13 审计要求，必填）")


class ChangeProjectSettingBody(BaseModel):
    """`PUT /settings/projects/{projectId}/{key}` 的请求体。"""

    model_config = _DTO_CONFIG

    value: dict[str, Any] | None = None
    reason: str = Field(description="变更理由（ADR-13 审计要求，必填）")


class DivergedProjectBody(BaseModel):
    """`GET /settings/projects/diverged` 的一行（一个"已分叉"的项目）。"""

    model_config = _DTO_CONFIG

    project_id: uuid.UUID = Field(alias="projectId")
    value: dict[str, Any]
    source: str
    global_value: dict[str, Any] | None = Field(default=None, alias="globalValue")
    revision_no: int = Field(alias="revisionNo")


class BatchLimitsBody(BaseModel):
    """`batch.limits` 的**规范化**形态（供前端直接渲染三档输入框）。

    三档用三个具名字段而不是 `dict`：与 `types.BatchLimits` 同一条理由
    —— 拼错一个档位名会得到"那一档不见了"而不是报错。
    `extra="forbid"` 在这里尤其要紧：多传的档位名必须是 422。
    """

    model_config = _DTO_CONFIG

    trial: int
    scale: int
    bulk: int

    @classmethod
    def of(cls, value: dict[str, Any] | None) -> BatchLimitsBody | None:
        """按 `BATCH_KINDS` 取三档；任一档缺失返回 `None`（由调用方决定如何提示）。

        **不在这里抛"缺少档位"**：那个判定在
        `settings_service._validate_batch_limits`（写路径）——
        读路径遇到一份"历史上写坏了的"值时应能返回部分信息，
        否则一次数据事故会让整个读取端点 500。
        """
        if not value:
            return None
        if any(kind not in value for kind in BATCH_KINDS):
            return None
        try:
            return cls(
                trial=int(value["TRIAL"]),
                scale=int(value["SCALE"]),
                bulk=int(value["BULK"]),
            )
        except (TypeError, ValueError):
            return None


class SettingEntryBody(BaseModel):
    """`GET /settings/global` 里的一个键（全局视图）。"""

    model_config = _DTO_CONFIG

    key: str
    configured: bool
    is_placeholder: bool = Field(alias="isPlaceholder")
    value: dict[str, Any] | None = None
    revision_no: int | None = Field(default=None, alias="revisionNo")


class SettingOverviewBody(BaseModel):
    """`GET /settings/global` 的响应：**全部合法键**的全局视图。

    ## 为什么是"全部键"而不是"已配置的键"

    `SETTING_KEYS` 里的**占位键**（`platform.mediaLimits`，OP-64 待实测）
    合法状态就是"没有值"。若只列出已配置的键，这个键就**从页面上消失了** ——
    而"有个键还没填值"正是最需要被看见的信息（`configured` 字段的
    同一条理由，见 `EffectiveSetting` 的 docstring）。

    因此逐键输出，`configured=false` 就是"尚未配置"这个事实本身。

    `batchLimits` 是给 `batch.limits` 的**便利投影**：前端要渲染的是三个
    输入框，而不是一个 `{"TRIAL":10,...}` 对象。它是派生字段
    （`BatchLimitsBody.of` 的产物），**不是第二份真值** —— 值为空或形状
    不全时返回 `null`，前端据此显示"尚未配置"，而不是拿到一个半成品对象。
    """

    model_config = _DTO_CONFIG

    entries: list[SettingEntryBody]
    batch_limits: BatchLimitsBody | None = Field(default=None, alias="batchLimits")


class SettingHistoryEntryBody(BaseModel):
    """`GET /settings/global/{key}/history` 的一行（ADR-13 的审计读数）。

    字段与 ADR-13 的审计要求逐条对应：

    | 要求 | 字段 |
    |---|---|
    | 改了什么值 | `value` |
    | 谁改的 | `actorId` |
    | 什么时候 | `createdAt` |
    | 为什么 | `reason` |
    | 哪个请求 | `requestId` |

    **`actorId` 可以为 `null`**：迁移的 `bootstrap_defaults` 与运维手工 SQL
    没有操作人（前者是数据回填，后者是本包明确不鼓励但无法从代码层禁止的路径）。
    用 `null` 表示"系统行为"比编一个假用户诚实。
    """

    model_config = _DTO_CONFIG

    revision_no: int = Field(alias="revisionNo")
    scope_kind: str = Field(alias="scopeKind")
    project_id: uuid.UUID | None = Field(default=None, alias="projectId")
    origin: str
    value: dict[str, Any]
    actor_id: uuid.UUID | None = Field(default=None, alias="actorId")
    reason: str | None = None
    request_id: str | None = Field(default=None, alias="requestId")
    created_at: str = Field(alias="createdAt")


# ---------------------------------------------------------------------------
# 草稿（plan_drafts）
# ---------------------------------------------------------------------------


class DimensionItemBody(BaseModel):
    """一个维度条目（场景 / 形式）。**与 `models.PlanDraft` 的 JSONB 行同形。**

    `weightPermille` 是**千分位整数**（30% → 300‰）—— 全链路不得出现
    `float` 百分比（§2.6 实现约束第 1 条）。DTO 用它做类型边界：
    写成 `float` 会让 pydantic 接受 `0.3` 并静默转成 `0`（`int(0.3)`）。

    `disabledReason` 与 `state` 一起用：原型把"停用原因"做成表格里的一列
    （`static/trial-config.html`），因此它必须能被保存与读回。
    规则（详见 `service._validate_dimension`）：只在 `state='DISABLED'`
    时有意义，长度 ≤ `MAX_DISABLED_REASON_CHARS`，不强制必填。
    """

    model_config = _DTO_CONFIG

    key: str
    name: str = ""
    weight_permille: int = Field(default=0, alias="weightPermille")
    sort_order: int = Field(default=0, alias="sortOrder")
    capacity: int | None = None
    state: str = "ENABLED"
    disabled_reason: str | None = Field(default=None, alias="disabledReason")


class PairingBody(BaseModel):
    """场景 × 形式的配对一行（原型"系统建议配对，运营可逐行改配"）。"""

    model_config = _DTO_CONFIG

    scene_key: str = Field(alias="sceneKey")
    form_key: str = Field(alias="formKey")


class SaveDraftBody(BaseModel):
    """`PUT /projects/{projectId}/plan-draft` 的请求体。**部分更新**。

    ## 为什么全部字段都有默认值（即可选）

    `None` = **本次不改这一块**（与 WP18 `PUT /brief-draft` /
    WP22 `update_draft` 同款语义）。把 `None` 当成"改成空"会让
    "只想改 `sceneItems`"的请求静默清空 `totalQuantity` ——
    而那让下一次分配没有总数可用，且响应是 200。

    ⚠️ 因此**不能**给这些字段写 `Field(default_factory=list)` 之类的
    非 None 默认 —— 那会让"没传"变成"传了空数组"，语义正好相反。
    """

    model_config = _DTO_CONFIG

    baseline_id: uuid.UUID | None = Field(default=None, alias="baselineId")
    total_quantity: int | None = Field(default=None, alias="totalQuantity")
    scene_items: list[DimensionItemBody] | None = Field(
        default=None, alias="sceneItems"
    )
    form_items: list[DimensionItemBody] | None = Field(default=None, alias="formItems")
    pairings: list[PairingBody] | None = None


class AllocationLineBody(BaseModel):
    """分配结果的一行（**含可解释性字段**）。

    `floorPart` / `remainder` / `tieRank` 是原型「最大余数法换算过程」表的
    直接数据源，也是"为什么这一类多一篇"的唯一解释。
    没有它们，争议只能靠人复算（`AllocationLine` 的 docstring 同款理由）。
    """

    model_config = _DTO_CONFIG

    key: str
    allocated: int
    weight_permille: int = Field(alias="weightPermille")
    capacity: int | None = None
    sort_order: int = Field(alias="sortOrder")
    floor_part: int = Field(alias="floorPart")
    remainder: int
    tie_rank: int = Field(alias="tieRank")
    capacity_shaved: bool = Field(default=False, alias="capacityShaved")


class AllocationBody(BaseModel):
    """一个维度的分配结果。

    **不变量（前端可断言）**：`allocatedTotal + unallocated == total`
    —— `allocatedTotal` 冗余地给出来，是为了让前端不必自己求和
    （自己求和就会有一处算法副本，而它是"前端显示与后端数字不同"的入口）。
    """

    model_config = _DTO_CONFIG

    total: int
    lines: list[AllocationLineBody]
    allocated_total: int = Field(alias="allocatedTotal")
    unallocated: int
    rounds: int = 1
    notes: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, result: AllocationResult) -> AllocationBody:
        """从纯函数结果构造（**唯一转换点**）。

        逐个字段赋值而不是 `model_validate(result)`：`AllocationResult` 是
        frozen dataclass，`from_attributes` 会按**属性名**取值，而别名字段
        （`allocatedTotal` / `weightPermille` / `floorPart` / `tieRank`）
        恰好属性名就是 snake_case，看起来能工作 —— 但 `AllocationLine`
        的属性名是 `weight_permille`，pydantic 的 from_attributes 走的是
        **字段名**而不是别名，一旦字段名与属性名不一致就会静默取到默认值。
        显式赋值让这种不一致在导入时就报 `AttributeError`。

        `notes` 是 `tuple`，这里转 `list` —— JSON 没有元组，
        pydantic 会转，但显式转能让响应对象的类型与声明一致。
        """
        return cls(
            total=result.total,
            lines=[
                AllocationLineBody(
                    key=line.key,
                    allocated=line.allocated,
                    weight_permille=line.weight_permille,
                    capacity=line.capacity,
                    sort_order=line.sort_order,
                    floor_part=line.floor_part,
                    remainder=line.remainder,
                    tie_rank=line.tie_rank,
                    capacity_shaved=line.capacity_shaved,
                )
                for line in result.lines
            ],
            allocated_total=result.allocated_total,
            unallocated=result.unallocated,
            rounds=result.rounds,
            notes=list(result.notes),
        )


class PlanDraftBody(BaseModel):
    """草稿的完整形态（含两维分配读数）。

    ## 为什么草稿响应里就带分配

    原型 `trial-plan.html` 的界面是"编辑比例 → 立刻看到每类几条"。
    让前端再发一次"算分配"的请求会引入"两次读之间草稿已变"的窗口 ——
    而前端会拿着过期的分配去决定"能不能提交"。
    **同一事务内算出来的分配才是与这次保存一致的分配**
    （与 WP22 `update_draft` 返回 `readiness` 同一条理由）。

    ## `etag` 为什么必须有出口

    `PUT` 与 `POST .../submissions` 都**必带** `If-Match`（协议 §1.1），
    而它的值就是这把 `etag`。不回带的话，客户端保存成功之后拿不到新的
    ETag ⇒ 它下一次保存必然 412（因为它只能拿旧值），而它**其实什么都没做错**。
    与 `baseline/api.py::InformationDraftBody` 的 `etag` 同一条理由。
    """

    model_config = _DTO_CONFIG

    draft_id: uuid.UUID = Field(alias="draftId")
    project_id: uuid.UUID = Field(alias="projectId")
    baseline_id: uuid.UUID | None = Field(default=None, alias="baselineId")
    state: str
    total_quantity: int | None = Field(default=None, alias="totalQuantity")
    scene_items: list[DimensionItemBody] = Field(alias="sceneItems")
    form_items: list[DimensionItemBody] = Field(alias="formItems")
    pairings: list[PairingBody] = Field(default_factory=list)
    schema_version: int = Field(alias="schemaVersion")
    lock_version: int = Field(alias="lockVersion")
    etag: str

    scene_allocation: AllocationBody | None = Field(
        default=None, alias="sceneAllocation"
    )
    form_allocation: AllocationBody | None = Field(default=None, alias="formAllocation")

    @classmethod
    def of(
        cls,
        view: DraftView,
        *,
        etag: str,
        scene_allocation: AllocationBody | None = None,
        form_allocation: AllocationBody | None = None,
    ) -> PlanDraftBody:
        """从 `DraftView` 构造。

        **入参是 `DraftView` 而不是 ORM 草稿**：ORM 实例绑在 session 上，
        而响应组装发生在 session 关闭之后（`DetachedInstanceError`）。
        服务层的 `draft_view()` 已经把它翻成 frozen dataclass
        （与 WP18/WP22 同款处置）。

        **`etag` 是显式入参而不是内部算**：ETag 的算法只有一处
        （`service.draft_etag`，它需要 ORM 实例）。若在这里重算
        （`build_etag(view.draft_id, view.lock_version)`），就有了第二处
        —— 而两处一旦漂移，客户端拿着 A 处签发的 ETag 去过 B 处的校验，
        症状是**必然 412**，且看不出来是格式问题还是版本问题。
        """
        return cls(
            draft_id=view.draft_id,
            project_id=view.project_id,
            baseline_id=view.baseline_id,
            state=view.state,
            total_quantity=view.total_quantity,
            scene_items=[DimensionItemBody.model_validate(row) for row in view.scene_items],
            form_items=[DimensionItemBody.model_validate(row) for row in view.form_items],
            pairings=[PairingBody.model_validate(row) for row in view.pairings],
            schema_version=view.schema_version,
            lock_version=view.lock_version,
            etag=etag,
            scene_allocation=scene_allocation,
            form_allocation=form_allocation,
        )


class SubmitDraftBody(BaseModel):
    """`POST /projects/{projectId}/plan-draft/submissions` 的请求体。

    **只有 `reason`**：要固化的是**当前草稿的内容**，没有可传的业务字段。
    尤其**没有 `totalQuantity`** —— 那会把"提交时改总量"变成一个
    隐式路径，而草稿里的总量与分配结果必须一致（§2.6 "两处口径只能有一处"）。
    """

    model_config = _DTO_CONFIG

    reason: str = Field(description="提交理由（写入版本注册的审计）")


class SubmittedPlanBody(BaseModel):
    """固化结果（`version_nodes` 里那一行 PLAN 节点的投影）。"""

    model_config = _DTO_CONFIG

    node_id: uuid.UUID = Field(alias="nodeId")
    entity_type: str = Field(alias="entityType")
    entity_id: uuid.UUID = Field(alias="entityId")
    revision_no: int = Field(alias="revisionNo")
    payload_hash: str = Field(alias="payloadHash")
    etag: str
    # 幂等命中标志。**不能靠任何状态字段推断**（WP13 的 ISS-35 教训）——
    # 客户端据此把"提交"按钮置灰，而不是把重复提交显示成失败。
    created: bool

    @classmethod
    def of(cls, registered: RegisteredVersion) -> SubmittedPlanBody:
        return cls(
            node_id=registered.node_id,
            entity_type=registered.entity_type,
            entity_id=registered.entity_id,
            revision_no=registered.revision_no,
            payload_hash=registered.payload_hash,
            etag=registered.etag,
            created=registered.created,
        )


__all__ = [
    "AllocationBody",
    "AllocationLineBody",
    "BatchLimitsBody",
    "ChangeGlobalSettingBody",
    "ChangeProjectSettingBody",
    "DimensionItemBody",
    "DivergedProjectBody",
    "PairingBody",
    "PlanDraftBody",
    "SaveDraftBody",
    "SettingEntryBody",
    "SettingHistoryEntryBody",
    "SettingOverviewBody",
    "SettingValueBody",
    "SubmitDraftBody",
    "SubmittedPlanBody",
]
