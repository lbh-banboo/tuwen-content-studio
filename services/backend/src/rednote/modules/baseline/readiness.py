"""步骤 3 的三条完成条件 —— **纯函数，且是唯一来源**（决策 D6）。

原型 `baseline.html` L139~165 列出 PRD §7.3 的三个完成条件：

    ① 阻断问题已经处理
    ② 至少有一个可生产场景
    ③ 运营明确点击确认

---

## 为什么必须是"唯一来源"（这是本模块存在的**全部**理由）

原型 L169~174 显示前端**也会**预检（"1 项阻断待处置"时按钮是灰的）。
于是一个自然的做法是在 API 里再判一次 —— **那就是同一判据的两处实现**。

两处实现的危险不在于"重复代码"，而在于**它们会漂移，且漂移时不报错**：

| 前端 | 后端 | 用户看到 |
|---|---|---|
| 说可以 | 拒绝 | **点了没反应**（没有任何错误信息，因为请求根本没发出去/被拦在前端）|
| 说不行 | 接受 | 按钮永远是灰的，而实际早就可以创建了 |

这与本项目"同一事实只能有一处实现"的铁律正面对撞
（WP19 R4 / WP20 `_project_asset` / 本包 D6）。

因此本模块的判据是：**API 的预检端点与创建基线的服务层都调 `step3_readiness()`**，
测试断言的是**"两处调用的是同一个函数"**，而不只是"结果相等"
（后者会被两份恰好写得一样的拷贝骗过 —— 与 WP19 §6 的
`test_gap_categories_come_from_wp18_constant` 同款断言）。

## 三条判据都写成"对明细行的否定式"，不读任何 count 列（D7）

本项目**已踩 10 次**同族问题（ISS-78/84/85/86/94/95）：
一个由别处维护的 `blocking_count` 与明细行脱节时**不报错**，
只会让"存在阻断项"这个判断失效 —— 而它的失效意味着**基线可以被非法推进**，
这是本包最严重的后果。因此：

  ① → `NOT EXISTS (conflict WHERE blocking AND state <> SETTLED)`
  ② → `EXISTS (scene WHERE state = PRODUCIBLE AND 至少一项已确认事实)`

两条都是**对明细行的查询**，明细行是唯一事实。

## "可生产场景"的定义（说明文档 §1.4 疏漏 #7）

原型 L154 说"5 个场景可生产"，L196~204 又有一个"已停用"的场景
（熬夜党急救，原因"无对应事实支持"）。**"可生产"的定义没有任何地方给出** ——
若它是运营手填的一个布尔，那它就是**第二个真值来源**。

本模块定义为**可派生的判据**：

    一个场景可生产  ⟺  它的状态是 PRODUCIBLE
                    且  它至少引用一项**已确认**的事实（`claims` 里 state=CONFIRMED）

于是"可生产场景数"是**算出来的**。原型 L196 那个"已停用"的场景
恰好命中这条定义的反面（它之所以停用是因为无事实支持）——
这不是巧合，而是这条定义解释了原型里那个样例。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from rednote.modules.baseline.baseline_types import (
    CLAIM_STATE_CONFIRMED,
    CONFLICT_STATE_SETTLED,
    SCENE_STATE_PRODUCIBLE,
)

# 三条完成条件的键。**受控取值**：API 响应、服务层拒绝的 details、
# 前端分支都用它 —— 散写成字符串会让"前端按 key 取条件"在改名后静默失效。
CONDITION_NO_BLOCKING_OPEN: Final[str] = "blockingResolved"
CONDITION_PRODUCIBLE_SCENE: Final[str] = "hasProducibleScene"
CONDITION_OPERATOR_CONFIRMED: Final[str] = "operatorConfirmed"

CONDITION_KEYS: Final[tuple[str, ...]] = (
    CONDITION_NO_BLOCKING_OPEN,
    CONDITION_PRODUCIBLE_SCENE,
    CONDITION_OPERATOR_CONFIRMED,
)

# 场景引用事实的字段名。原型 L149~205 的场景表里那一列叫「事实来源」。
# **定义在这里而不是读处现取**：`_fact_refs` 用两种写法兜底
# （`factRefs` / `fact_refs`）时，测试要能断言"两种都认"。
FACT_REF_KEYS: Final[tuple[str, ...]] = ("factRefs", "fact_refs")
CLAIM_ID_KEYS: Final[tuple[str, ...]] = ("claimId", "claim_id", "id")
CLAIM_STATE_KEYS: Final[tuple[str, ...]] = ("state", "claimState", "claim_state")
# 冲突 / 场景的 id 候选键。**提到这里而不是在调用处现写元组**：
# 这两组此前同时出现在本模块（`blocking_open_conflicts` /
# `producible_scene_ids`）与 `service.py`（`_require_conflicts` /
# `_require_scenes` 的存在性校验）—— 同一个事实两处实现，漂移时不报错。
# 漂移的方向是"服务层认第三种拼写、readiness 不认"（或反过来），
# 于是同一条草稿在保存时通过、在算完成条件时被当成缺 id 而静默丢弃。
CONFLICT_ID_KEYS: Final[tuple[str, ...]] = ("conflictId", "conflict_id", "id")
SCENE_ID_KEYS: Final[tuple[str, ...]] = ("sceneId", "scene_id", "id")


@dataclass(frozen=True, slots=True)
class ConditionResult:
    """一条完成条件的读数。

    `satisfied` 是判据本身；`reason` 是**给人看的**说明 ——
    API 的预检响应与拒绝错误体共用同一段文字（同源，不会一处说"1 项阻断"
    另一处说"存在阻断问题"）。
    """

    key: str
    satisfied: bool
    reason: str
    #: 未处置的阻断冲突 id（条件 ① 未满足时非空）。
    blocking_open: tuple[str, ...] = ()
    #: 可生产场景 id（条件 ② 的读数；`satisfied` 等价于它非空）。
    producible_scenes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Step3Readiness:
    """三条完成条件的整体读数。

    `ready` 是三条的合取 —— **它是唯一的"能不能创建基线"判据**。
    服务层拿它做拒绝、API 拿它做预检，两处不得各写一遍 `all(...)`。
    """

    ready: bool
    conditions: tuple[ConditionResult, ...]

    def by_key(self, key: str) -> ConditionResult:
        """按键取一条条件。未知 key **抛错**（不返回 None）。

        返回 `None` 会让调用方写 `if cond and cond.satisfied` ——
        而一个拼错的 key 就此恒为"未满足"，表现为"永远创建不了基线"
        且没有人知道原因（本项目的高频形态）。
        """
        for condition in self.conditions:
            if condition.key == key:
                return condition
        raise KeyError(
            f"未知的完成条件键 {key!r}。合法取值：{', '.join(CONDITION_KEYS)}"
        )

    def unsatisfied(self) -> tuple[ConditionResult, ...]:
        """未满足的那些条件（服务层拒绝时用来拼消息）。"""
        return tuple(condition for condition in self.conditions if not condition.satisfied)


def pick_field(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """按多个候选键取第一个存在的值。

    **为什么容忍两种拼写**：草稿里的 JSONB 由 API 写入，而 API 的请求体
    用 camelCase（与全项目一致）；但它也可能由数据回填或测试夹具直接写库。
    容忍两种写法**不是**"随便"，而是让判据在两种来源下都成立 ——
    精确到只认一种，会让另一条路径静默地"没有阻断项"（最坏的失效方向）。

    ⚠️ **公开（去掉下划线）是因为 `service.py` 也要读同一批键**：
    服务层校验草稿形状时要判 `state` 在不在受控域内，
    若它自己写一遍 `row.get("state") or row.get("claimState")`，
    就会出现第三处"知道草稿长什么样"的实现（ISS-66 形态：
    同一件事两处各写一遍，漂移时不报错）。
    """
    for key in keys:
        if key in row:
            return row[key]
    return None


def _fact_refs(scene: dict[str, Any]) -> tuple[str, ...]:
    raw = pick_field(scene, FACT_REF_KEYS)
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw)
    # 其它形状（dict / int）不猜测意图，归空 —— 与 `platform.errors.sanitize_details`
    # 对非 dict 输入的处理同一条取向。
    return ()


def confirmed_claim_ids(
    claims: tuple[dict[str, Any], ...],
) -> frozenset[str]:
    """已确认的事实 id 集合。

    **只收 `CONFIRMED`**：`CONFLICT` 与 `PENDING` 都不算 ——
    一个"带着冲突的事实"支撑起来的场景不是可生产场景，
    它是下一个待处置的阻断项。

    ⚠️ **公开（去掉下划线）是因为 `service.py` 的基线项派生要按同一条判据
    决定"哪些事实纳入"**：若服务层自己写一遍 `state == "CONFIRMED"`，
    那么"场景可生产"与"事实纳入"两个判据就有了两份实现 ——
    而它们漂移的方向是"基线里纳入了一条未确认的事实"（静默）。
    """
    return frozenset(
        str(claim_id)
        for claim in claims
        if (claim_id := pick_field(claim, CLAIM_ID_KEYS)) is not None
        and str(pick_field(claim, CLAIM_STATE_KEYS) or "") == CLAIM_STATE_CONFIRMED
    )


def blocking_open_conflicts(
    conflicts: tuple[dict[str, Any], ...],
) -> tuple[str, ...]:
    """未处置的**阻断**冲突 id（条件 ① 的判据，公开供服务层与测试共用）。

    **只算 `blocking=True` 的**：非阻断冲突（例如"时效存疑"）不阻止创建基线
    —— 否则"提示"与"阻断"就没有区别了，而 AC-09 说的是**阻断项**。
    """
    return tuple(
        str(conflict_id)
        for conflict in conflicts
        if bool(conflict.get("blocking"))
        and str(conflict.get("state") or "") != CONFLICT_STATE_SETTLED
        and (conflict_id := pick_field(conflict, CONFLICT_ID_KEYS))
        is not None
    )


def producible_scene_ids(
    scenes: tuple[dict[str, Any], ...],
    claims: tuple[dict[str, Any], ...],
) -> tuple[str, ...]:
    """可生产场景 id（条件 ② 的判据，公开供服务层与测试共用）。

    见模块 docstring「可生产场景的定义」——状态 + 至少一项已确认事实，
    两个条件都是**从明细行派生的**，不接受运营手填的布尔。
    """
    confirmed = confirmed_claim_ids(claims)
    if not confirmed:
        # 短路：没有任何已确认事实时，可生产场景必然是空集。
        # 显式短路让这条语义在读代码时可见（"没有事实 → 没有可生产场景"）。
        return ()
    return tuple(
        str(scene_id)
        for scene in scenes
        if str(scene.get("state") or "") == SCENE_STATE_PRODUCIBLE
        and any(ref in confirmed for ref in _fact_refs(scene))
        and (scene_id := pick_field(scene, SCENE_ID_KEYS)) is not None
    )


def step3_readiness(
    *,
    claims: tuple[dict[str, Any], ...],
    conflicts: tuple[dict[str, Any], ...],
    scenes: tuple[dict[str, Any], ...],
    operator_confirmed: bool,
) -> Step3Readiness:
    """步骤 3 的三条完成条件（PRD §7.3）。

    **这是唯一来源** —— API 预检端点与 `service.create_baseline` 都调它。

    输入全部是**明细行**（plain dict），不是 ORM 对象：
    这样它可以被单元测试直接喂构造出来的数据，不需要真库、不需要 session
    —— 这是"判据可被独立验证"的前提（与 WP20 `projection.py` 同一手法）。

    `operator_confirmed` 由调用方从草稿的对应字段读出后传入，
    而不是在这里读库：纯函数不碰 I/O。
    """
    open_blocking = blocking_open_conflicts(conflicts)
    producible = producible_scene_ids(scenes, claims)

    no_blocking = ConditionResult(
        key=CONDITION_NO_BLOCKING_OPEN,
        satisfied=not open_blocking,
        reason=(
            "阻断问题已全部处置。"
            if not open_blocking
            else f"{len(open_blocking)} 项阻断问题待处置："
            + "、".join(open_blocking)
        ),
        blocking_open=open_blocking,
    )
    has_scene = ConditionResult(
        key=CONDITION_PRODUCIBLE_SCENE,
        satisfied=bool(producible),
        reason=(
            f"{len(producible)} 个场景可生产。"
            if producible
            else "没有任何场景可生产（可生产 = 场景状态为 PRODUCIBLE 且至少引用一项已确认事实）。"
        ),
        producible_scenes=producible,
    )
    confirmed = ConditionResult(
        key=CONDITION_OPERATOR_CONFIRMED,
        satisfied=bool(operator_confirmed),
        reason=(
            "运营已明确点击确认。"
            if operator_confirmed
            else "运营尚未明确点击确认（PRD §7.3 条件③ 要求人工确认，不可由系统代判）。"
        ),
    )

    conditions = (no_blocking, has_scene, confirmed)
    return Step3Readiness(
        ready=all(condition.satisfied for condition in conditions),
        conditions=conditions,
    )


def step3_readiness_of(
    draft: dict[str, Any],
) -> Step3Readiness:
    """从一份草稿负载直接算完成条件（表端调用方的便利入口）。

    **刻意只是"取字段 + 转调"**，不含任何判定 —— 判定在 `step3_readiness()`
    里，一处。若把字段名映射也写进服务层，服务层就会成为第二个"知道
    草稿长什么样"的地方（ISS-66 形态）。
    """
    return step3_readiness(
        claims=tuple(draft.get("claims") or ()),
        conflicts=tuple(draft.get("conflicts") or ()),
        scenes=tuple(draft.get("scenes") or ()),
        operator_confirmed=bool(draft.get("operatorConfirmed") or draft.get("operator_confirmed")),
    )


__all__ = [
    "CLAIM_ID_KEYS",
    "CLAIM_STATE_KEYS",
    "CONDITION_KEYS",
    "CONDITION_NO_BLOCKING_OPEN",
    "CONDITION_OPERATOR_CONFIRMED",
    "CONDITION_PRODUCIBLE_SCENE",
    "CONFLICT_ID_KEYS",
    "FACT_REF_KEYS",
    "SCENE_ID_KEYS",
    "ConditionResult",
    "Step3Readiness",
    "blocking_open_conflicts",
    "confirmed_claim_ids",
    "pick_field",
    "producible_scene_ids",
    "step3_readiness",
    "step3_readiness_of",
]
