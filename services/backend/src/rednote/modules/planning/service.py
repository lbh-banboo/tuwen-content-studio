"""草稿读写 + 固化出 PLAN 版本（WP24 的业务层）。

**本模块是 `plan_drafts` 的唯一写入口**，也是"场景/形式比例 → 分配结果"
这条链的唯一编排者。

---

## 一、对外函数

| 函数 | 用途 |
|---|---|
| `require_draft(repo, project_id=…)` | 取本项目草稿，不存在即建（幂等懒创建） |
| `save_draft(repo, …)` | 保存比例与总量（部分更新 + 乐观锁） |
| `draft_view(draft)` | ORM → 跨层值对象（frozen） |
| `scene_allocation(draft, …)` / `form_allocation(draft, …)` | 按当前草稿算分配（纯计算，不读库） |
| `submit_draft(repo, …)` | 固化出 `entity_type='PLAN'` 的版本节点 |

---

## 二、三条必须守住的边界

### 1. 分配计算**不读库**

`scene_allocation` / `form_allocation` 的入参是 `draft` 的 JSONB 内容，
比例与容量都在里面。它们**不接收 repo**，因此不可能去读配置。

这不是洁癖：`allocate_by_weight` 的"和不漂移"性质是靠**穷举**证明的
（`total` 1..120 × 项数 2..8）。一旦分配路径能读库，穷举就需要一个数据库
才能跑，"穷举证明"就退化成"抽样测试"（§2.6 实现约束第 2 条）。

**上限（`batch.limits`）在这里不参与**：它是"一次提交最多几条"的关口，
由 `limits.validate_quantity` 在**提交/生产**时校验（WP31/WP32 的调用点）。
把它塞进分配会让"算出来的分配"依赖当前配置 —— 那么改一次上限，
历史分配结果就复现不出来了，而 C19 要的是**可复现**。

### 2. 草稿的每一块都是"可独立更新"的

`save_draft` 用 `None` 表示"本次不改这一块"（与 WP18/WP22 同款语义）。
**不能把 `None` 当成"改成空"** —— 只想改 `sceneItems` 的请求会把
`totalQuantity` 静默清空，而那让下一次分配**没有总数可用**且不报错。

### 3. 固化出 PLAN 版本走 WP11 的 `register_version`，不改它的签名

`entity_id` 取 **`project_id`**，理由与 WP22 的 `BASELINE` 完全相同：
`PLAN` 的"逻辑实体"是**该项目的那条计划链**，不是"某一条草稿" ——
草稿是工作区（可变），计划是链上的一个修订（不可变）。
传 `draft.id` 会让"同一项目的第二份计划"拿到修订号 1 而不是 2。

---

## 三、⚠️ 固化为什么必须**先锁草稿行**再读内容

`submit_draft` 的三个动作（读草稿内容 → 写 `version_nodes` → 改草稿状态）
必须**互斥**：

    T1 与 T2 同时提交同一份草稿
    · 两者都读到 state='EDITING'、拿到同一份内容
    · 两者都写 version_nodes（各自取号，两个 PLAN 版本）
    · 两者都改状态 ⇒ 第二改覆盖第一改
    结果：**两个 PLAN 版本、一个草稿状态**，而"第二次提交"在业务上
    应该被 409 挡下（草稿已提交过）。

因此 `submit_draft` 第一步就是 `repo.get_draft_for_update(project_id)`
（`SELECT … FOR UPDATE`）。乐观锁（`lock_version`）在这里不够 ——
它是给"客户端提交过期 ETag"用的，而上面这个交错的两个事务
**各自持有可能都是最新的 ETag**（都在同一瞬间读的）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

from rednote.modules.planning import permille as permille_rules
from rednote.modules.planning.allocation import (
    allocate_by_weight,
    allocate_from_remaining,
    capacity_remaining,
)
from rednote.modules.planning.repository import PlanningRepository
from rednote.modules.planning.types import (
    DRAFT_STATE_EDITING,
    DRAFT_STATE_SUBMITTED,
    DRAFT_STATES,
    ITEM_STATES,
    ITEM_STATE_DISABLED,
    ITEM_STATE_ENABLED,
    ITEM_STATE_LIST_SQL,
    MAX_DIMENSION_ITEMS,
    MAX_DISABLED_REASON_CHARS,
    MAX_ITEM_NAME_CHARS,
    MAX_QUANTITY_BOUND,
    MIN_QUANTITY,
    PERMILLE_TOTAL,
    REASON_BASELINE_MISMATCH,
    REASON_DIMENSION_LIMIT_EXCEEDED,
    REASON_DRAFT_ALREADY_SUBMITTED,
    REASON_DRAFT_NOT_FOUND,
    REASON_DUPLICATE_ITEM_KEY,
    REASON_PERMILLE_NOT_TOTAL,
    REASON_PROJECT_NOT_FOUND,
    REASON_TOTAL_QUANTITY_INVALID,
    AllocationItem,
    AllocationResult,
)
from rednote.platform.concurrency import build_etag, version_conflict
from rednote.platform.errors import ApiError, ErrorCode

#: 固化 PLAN 版本时用的 `entity_type`（WP11 已注册该取值，无需跨包改动）。
ENTITY_TYPE_PLAN: Final[str] = "PLAN"

#: E8 依赖边（PLAN → BASELINE）写入 `version_dependencies` 时的 purpose。
PURPOSE_PLAN_BASELINE: Final[str] = "PLAN_BASELINE"

#: 维度名 → 草稿里对应的列名。分配与校验都按它取数，**避免三处硬编码**
#: （场景维度叫 `sceneItems`、形式维度叫 `formItems`，但它们走同一套算法）。
DIMENSION_SCENE: Final[str] = "scene"
DIMENSION_FORM: Final[str] = "form"

_ITEMS_FIELD: Final[dict[str, str]] = {
    DIMENSION_SCENE: "sceneItems",
    DIMENSION_FORM: "formItems",
}

#: 分配结果里每行允许的字段 —— **白名单**，多出来的键一律拒绝。
#: 不做的话，一行里塞进 `{"allocated": 3, "allocatedX": 999}` 也照收，
#: 而读的一方取 `allocated`、写的一方以为改了 `allocatedX`，
#: 症状是"改了没效果"且不报错。
#:
#: `disabledReason` 在列（错误 4 的修复）：原型页面上"停用原因"是一列
#: 可见的表格字段（`static/trial-config.html` 有两处实文："停用原因：
#: 无对应已确认事实来源"），`models.PlanDraft` 的 JSONB 行示例也列了它。
#: 此前白名单没有它 ⇒ **前端照文档发请求会稳定 422**，
#: 而两边都没报错（测试 0 次引用）。现已纳入并做长度校验。
_ALLOWED_ITEM_KEYS: Final[frozenset[str]] = frozenset(
    {
        "key",
        "name",
        "weightPermille",
        "sortOrder",
        "capacity",
        "state",
        "disabledReason",
    }
)


# ---------------------------------------------------------------------------
# 错误构造（与 WP11/WP22/WP23 同一形状）
# ---------------------------------------------------------------------------


def _validation_error(field: str, message: str, *, reason: str, **extra: Any) -> ApiError:
    details: dict[str, Any] = {
        "fieldErrors": [{"field": field, "type": reason, "message": message}],
        "reason": reason,
    }
    details.update(extra)
    return ApiError(ErrorCode.VALIDATION_FAILED, message, details=details)


def _not_found(message: str, *, reason: str, **extra: Any) -> ApiError:
    details: dict[str, Any] = {"reason": reason}
    details.update(extra)
    return ApiError(ErrorCode.RESOURCE_NOT_FOUND, message, details=details)


def _conflict(message: str, *, reason: str, **extra: Any) -> ApiError:
    details: dict[str, Any] = {"reason": reason}
    details.update(extra)
    return ApiError(ErrorCode.INVALID_TRANSITION, message, details=details)


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DraftView:
    """草稿的跨层值对象。

    **为什么转成 frozen dataclass 而不是把 ORM 对象交给上层**：
    ORM 实例绑在 session 上，session 一关就 `DetachedInstanceError` ——
    而端点返回它发生在 session 关闭之后（与 `baseline/service.py::draft_view`
    同款处置）。
    """

    draft_id: uuid.UUID
    project_id: uuid.UUID
    baseline_id: uuid.UUID | None
    state: str
    total_quantity: int | None
    scene_items: tuple[dict[str, Any], ...]
    form_items: tuple[dict[str, Any], ...]
    pairings: tuple[dict[str, Any], ...]
    schema_version: int
    lock_version: int


# ---------------------------------------------------------------------------
# 草稿：读与懒创建
# ---------------------------------------------------------------------------


def draft_etag(draft: Any) -> str:
    """草稿的**可变** ETag（`build_etag(draft_id, lock_version)`）。

    与 WP18/WP22 完全同款：草稿阶段资源可变，ETag 每次保存递增。
    固化之后计划变成不可变版本节点，ETag 换 `build_immutable_etag`
    —— 这个切换点就是"草稿 → 版本"那条边界在 HTTP 层的可观察形态。
    """
    return build_etag(draft.id, draft.lock_version)


def require_draft(repo: PlanningRepository, *, project_id: uuid.UUID):
    """取本项目草稿；不存在即建（**幂等的懒创建**）。

    **为什么懒创建**：与 WP18 `ensure_brief` / WP22 `require_draft` 同一条
    理由 —— 协议把"创建初始工作区"归给项目创建事务，而项目创建是 WP10 的
    代码，本包不能改它（跨包接缝改动，与并行开发纪律冲突）。
    因此做成"第一次被调用时建出来"，此后每次都是读。

    **代价（已登记收口事项）**：`GET` 变成会写库的请求。它幂等
    （`plan_drafts.project_id` 唯一兜底），但违反"GET 是安全方法"的
    HTTP 约定。收口方向与 WP22 相同：让主 agent 在项目创建事务里补一次
    调用 —— 那时本函数退化成纯读。
    """
    _require_project(repo, project_id)

    draft = repo.get_draft(project_id)
    if draft is not None:
        return draft

    from sqlalchemy.exc import IntegrityError

    try:
        # 包在 SAVEPOINT 里：冲突只回滚这一段，**不动调用方在同一事务里
        # 已做的其它写入**。WP22 的 `require_draft` 用的是
        # `session.rollback()`，那是因为它在事务开头；本函数不假设这一点
        # （端点可能先做了别的校验写入），所以用 savepoint。
        with repo.savepoint():
            draft = repo.insert_draft(
                project_id=project_id, baseline_id=None, created_by=None
            )
            repo.flush()
    except IntegrityError:
        # 并发：另一个请求刚建出来（唯一约束拦下）。
        # **重新读一次才返回**，不把异常当失败 —— 两个请求的意图完全相同。
        # 这是"不能靠 state 推断首次"的又一实例（与 WP22 同款）：
        # 我这次没插进去，不代表没有草稿。
        existing = repo.get_draft(project_id)
        if existing is None:  # pragma: no cover - 唯一约束已保证有
            raise
        return existing
    return draft


def _require_project(repo: PlanningRepository, project_id: uuid.UUID) -> None:
    """项目必须存在（给人读的 404，而不是 FK 违反导致的 500）。"""
    if not repo.project_exists(project_id):
        raise _not_found(
            f"项目 {project_id} 不存在。",
            reason=REASON_PROJECT_NOT_FOUND,
            projectId=str(project_id),
        )


def draft_view(draft: Any) -> DraftView:
    """ORM 草稿 → `DraftView`（frozen，不绑 session）。"""
    return DraftView(
        draft_id=draft.id,
        project_id=draft.project_id,
        baseline_id=draft.baseline_id,
        state=draft.state,
        total_quantity=draft.total_quantity,
        scene_items=tuple(draft.scene_items or ()),
        form_items=tuple(draft.form_items or ()),
        pairings=tuple(draft.pairings or ()),
        schema_version=draft.schema_version,
        lock_version=draft.lock_version,
    )


# ---------------------------------------------------------------------------
# 草稿：保存（部分更新 + 乐观锁）
# ---------------------------------------------------------------------------


def save_draft(
    repo: PlanningRepository,
    *,
    project_id: uuid.UUID,
    expected_lock_version: int,
    baseline_id: uuid.UUID | None = None,
    total_quantity: int | None = None,
    scene_items: Any = None,
    form_items: Any = None,
    pairings: Any = None,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> Any:
    """保存草稿（**部分更新**），返回刷新后的 ORM 草稿。

    ## `None` = 本次不改这一块

    与 WP18 `PUT /brief-draft` / WP22 `update_draft` 同款。把 `None` 当成
    "改成空"会让"只想改 `sceneItems`"的请求静默清空 `totalQuantity`，
    而那让下一次分配没有总数可用，且响应是 200。

    ## 校验顺序：形状 → 比例合计 → 基线归属

    **形状先于合计**：`validate_total` 对形状错误的条目会抛
    `TypeError`（比如 `weightPermille` 是字符串），那是 500 而不是 422。
    形状校验必须先跑，让所有"输入不合法"都走同一种可读的错误形状。

    ## 比例合计**不在这里强制等于 1000**

    运营拖数字的过程中合计必然不等于 1000（§2.2 用户画像：他**会算不对**），
    而保存是"拖一下就自动保存"的交互。强制等于 1000 会让每拖一次都失败。

    真正的关口在**固化**（`submit_draft`）与**分配**：
      · `submit_draft` 要求合计 == 1000（否则算出来的分配没有意义）；
      · `scene_allocation` 内部会 `normalize_permille` 兜住不等的情形，
        并把这个事实写进 `notes`（**不静默修正**）。

    返回 `(draft, …)` 的**派生读数**由端点另调 `scene_allocation` 等取 ——
    这里只返回草稿本身，是为了让"保存"与"算分配"两条路径各自独立可测。

    ## `actor_id` / `request_id` 当前未使用（MINOR-5 的处置）

    两个参数在函数体里**一次都没用**，因为 `plan_drafts` 表**没有审计列**
    （保存是高频自动保存的交互，逐次留痕既无需求也会撑爆表）。
    真要审计的是**固化**（`submit_draft` 把 `actor_id` 传给
    `register_version` 的 `created_by`）与**配置变更**（`runtime_settings`
    的 `actor_id` 列 + 不可变触发器）。

    保留而不删签名：端点已按统一形状传参，且 WP25+ 若给草稿加审计
    就会真的用到；将来若确实不用，删掉这两个参数即可（本包内改动）。
    用 `del` 显式声明"我知道它没用"——与 `api.py` 里 `del user` /
    `del body` 同一风格，避免读者以为保存草稿有审计。
    """
    del actor_id, request_id
    draft = require_draft(repo, project_id=project_id)

    # --- 形状与受控取值（给人看的 422） ---------------------------------
    if baseline_id is not None:
        # ⚠️ FK 只保证基线存在，**不保证属于本项目**。不校验的话，
        # 项目 A 的草稿可以钉住项目 B 的基线 —— 静默的越权读取，
        # 固化出的 PLAN 版本会带着别的项目的基线内容，
        # 而权限判定只看草稿所属项目（与 WP23 `prompt_publications` 同形）。
        if not repo.baseline_belongs_to_project(
            baseline_id=baseline_id, project_id=project_id
        ):
            raise _validation_error(
                "baselineId",
                f"基线 {baseline_id} 不属于项目 {project_id}。",
                reason=REASON_BASELINE_MISMATCH,
                baselineId=str(baseline_id),
                projectId=str(project_id),
            )

    if total_quantity is not None:
        _require_total_quantity(total_quantity)

    if scene_items is not None:
        _validate_dimension(DIMENSION_SCENE, scene_items)
    if form_items is not None:
        _validate_dimension(DIMENSION_FORM, form_items)
    if pairings is not None:
        _validate_pairings(pairings)

    touches_something = any(
        block is not None
        for block in (
            baseline_id,
            total_quantity,
            scene_items,
            form_items,
            pairings,
        )
    )

    if not touches_something:
        # 一个字段都没改 ⇒ **不递增 `lock_version`**。递增会让客户端的
        # ETag 无谓失效（它下一步提交会被 412 挡下，而它其实什么都没做）。
        repo.flush()
        return draft

    # ⚠️ 条件 UPDATE 之前取 before 负载（若将来加审计，必须在这里取）——
    # `synchronize_session=False` 绕过了身份映射，"重读会不会就地刷新"
    # 是实现细节，依赖它等于让审计的"改前值"变成碰运气（WP22 MAJOR-5）。

    updated = repo.update_draft(
        draft_id=draft.id,
        expected_lock_version=expected_lock_version,
        baseline_id=baseline_id,
        total_quantity=total_quantity,
        state=None,  # 状态只由 submit / 显式接口改，保存不碰它
        scene_items=scene_items,
        form_items=form_items,
        pairings=pairings,
    )

    if updated == 0:
        # 读到之后有人改过 ⇒ 412 并回带**当前** ETag（协议 §1.1）。
        current = repo.get_draft(project_id)
        current_version = 0 if current is None else current.lock_version
        raise version_conflict(
            resource_id=draft.id,
            current_version=current_version,
            reason="lock_version_mismatch",
            message="计划草稿已被更新，请重新读取最新版本后再提交。",
        )

    # 条件 UPDATE 绕过了身份映射，读回来的是**过期实例** —— 必须显式重读，
    # 否则响应里回带的是保存**之前**的内容（WP22 MAJOR-4 同型）。
    refreshed = repo.reload_draft(draft.id)
    if refreshed is None:  # pragma: no cover - 刚更新过必然存在
        raise RuntimeError(f"草稿 {draft.id} 更新后读不到 —— 不变量被破坏。")
    return refreshed


def _require_total_quantity(value: Any) -> None:
    """总量必须是 1 ~ 上界 的整数。

    **下界在这里也校验一次**（DDL 有 `total_quantity >= 1`）：
    DDL 报出来是 `IntegrityError` ⇒ 500，而这是用户输入问题 ⇒ 422。
    两层都要有，DDL 那层守的是"绕过应用层也拦得住"（与 ADR-13 把下界
    留在 DDL 同一手法）。

    **`bool` 必须显式排除**：`True` 是 `int` 的子类，会被静默当成 1。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise _validation_error(
            "totalQuantity",
            f"总量必须是整数，收到 {value!r}。",
            reason=REASON_TOTAL_QUANTITY_INVALID,
        )
    if value < MIN_QUANTITY:
        raise _validation_error(
            "totalQuantity",
            f"总量至少 {MIN_QUANTITY}，收到 {value}。",
            reason=REASON_TOTAL_QUANTITY_INVALID,
        )
    if value > MAX_QUANTITY_BOUND:
        raise _validation_error(
            "totalQuantity",
            f"总量最多 {MAX_QUANTITY_BOUND}，收到 {value}。",
            reason=REASON_TOTAL_QUANTITY_INVALID,
            maxQuantity=MAX_QUANTITY_BOUND,
        )


def _validate_dimension(dimension: str, items: Any) -> list[dict[str, Any]]:
    """一个维度（场景 / 形式）的条目数组形状校验。返回原列表。

    ## 校验逐项列出，不做"通用 dict 检查"

    每一项的字段各有取值域（`weightPermille` 0~1000、`capacity` 可空正数、
    `state` 受控取值）。一个通用的"值是 dict 就行"会放行
    `{"weightPermille": -5}`，而负权重让分配函数抛 `ValueError`
    —— 那是 500 而不是可读的 422。

    ## 重复 `key` 必须拒绝

    `key` 是**平局次序的第三级**、也是"这条分配结果对应哪个场景"的唯一
    联系。重复 key 会让两行争夺同一个身份：分配算出来两行、前端只认得一行
    —— 症状是"某类少了一条"，而合计仍然对得上（`sum == total` 不检测这个）。

    ## 为什么拆成"数组层 + 单条目层"两个函数

    两层的失败语义完全不同：数组层管"这**一批**条目的规模合不合法"
    （是不是数组、有没有超上限），单条目层管"**这一条**的形状合不合法"。
    拆开之后，单条目层的判据（每个字段各自一个取值域）可以脱离数组
    上下文单独阅读与测试 —— 两者混在一处时，110 行里有 90 行都在讲
    "一个条目长什么样"，而函数名说的是"一个维度"。
    """
    field = _ITEMS_FIELD[dimension]
    if not isinstance(items, list):
        raise _validation_error(
            field, f"{field} 必须是数组。", reason=REASON_DIMENSION_LIMIT_EXCEEDED
        )
    if len(items) > MAX_DIMENSION_ITEMS:
        raise _validation_error(
            field,
            f"{field} 条数 {len(items)} 超过上限 {MAX_DIMENSION_ITEMS}。",
            reason=REASON_DIMENSION_LIMIT_EXCEEDED,
            maxItems=MAX_DIMENSION_ITEMS,
        )

    # `seen` 跨条目累积 —— 它是"数组层"的事实（重复是两条之间的事，
    # 单看任何一条都合法），因此由本函数持有并传给单条目校验。
    seen: set[str] = set()
    for index, item in enumerate(items):
        _validate_dimension_item(item, where=f"{field}[{index}]", seen=seen)
    return items


def _validate_dimension_item(item: Any, *, where: str, seen: set[str]) -> None:
    """**单个**维度条目的形状校验。

    `seen` 是入参而非局部变量：重复 key 是条目之间的性质，
    必须由调用方（数组层）持有累加。
    """
    if not isinstance(item, dict):
        raise _validation_error(
            where, f"{where} 必须是对象。", reason=REASON_DIMENSION_LIMIT_EXCEEDED
        )
    unknown = sorted(set(item) - _ALLOWED_ITEM_KEYS)
    if unknown:
        raise _validation_error(
            where,
            f"{where} 含未知字段：{unknown}（合法：{sorted(_ALLOWED_ITEM_KEYS)}）。",
            reason=REASON_DIMENSION_LIMIT_EXCEEDED,
            unknown=unknown,
        )

    key = item.get("key")
    if not isinstance(key, str) or not key.strip():
        raise _validation_error(
            f"{where}.key",
            f"{where}.key 必须是非空字符串。",
            reason=REASON_DIMENSION_LIMIT_EXCEEDED,
        )
    if key in seen:
        raise _validation_error(
            f"{where}.key",
            f"条目 key 重复：{key!r}。每个条目必须有唯一 key。",
            reason=REASON_DUPLICATE_ITEM_KEY,
            duplicateKey=key,
        )
    seen.add(key)

    name = item.get("name", "")
    if not isinstance(name, str) or len(name) > MAX_ITEM_NAME_CHARS:
        raise _validation_error(
            f"{where}.name",
            f"{where}.name 必须是不超过 {MAX_ITEM_NAME_CHARS} 字的字符串。",
            reason=REASON_DIMENSION_LIMIT_EXCEEDED,
        )

    weight = item.get("weightPermille", 0)
    try:
        permille_rules.validate_permille_value(weight, name=f"{where}.weightPermille")
    except ValueError as exc:
        raise _validation_error(
            f"{where}.weightPermille",
            str(exc),
            reason=REASON_DIMENSION_LIMIT_EXCEEDED,
        ) from exc

    sort_order = item.get("sortOrder", 0)
    if isinstance(sort_order, bool) or not isinstance(sort_order, int):
        raise _validation_error(
            f"{where}.sortOrder",
            f"{where}.sortOrder 必须是整数，收到 {sort_order!r}。",
            reason=REASON_DIMENSION_LIMIT_EXCEEDED,
        )

    capacity = item.get("capacity")
    if capacity is not None:
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise _validation_error(
                f"{where}.capacity",
                f"{where}.capacity 必须是整数或 null，收到 {capacity!r}。",
                reason=REASON_DIMENSION_LIMIT_EXCEEDED,
            )
        if capacity < 0:
            raise _validation_error(
                f"{where}.capacity",
                f"{where}.capacity 不能为负（收到 {capacity}）。",
                reason=REASON_DIMENSION_LIMIT_EXCEEDED,
            )

    state = item.get("state", ITEM_STATE_ENABLED)
    if state not in ITEM_STATES:
        raise _validation_error(
            f"{where}.state",
            f"{where}.state 只能是 {ITEM_STATE_LIST_SQL}，收到 {state!r}。",
            reason=REASON_DIMENSION_LIMIT_EXCEEDED,
        )

    # `disabledReason` 只在停用时才有意义。**不强制停用必填原因**：
    # 原型是"可填可不填"的说明性字段（两处实文都是补充说明，
    # 而非阻断操作的条件），强制必填会让"只是先停一下"的操作卡住。
    # 但**启用了却带着原因**是自相矛盾的数据 ⇒ 拒绝，
    # 否则前端按 `state` 渲染时会显示"启用中（停用原因：xxx）"。
    disabled_reason = item.get("disabledReason")
    if disabled_reason is not None:
        if not isinstance(disabled_reason, str):
            raise _validation_error(
                f"{where}.disabledReason",
                f"{where}.disabledReason 必须是字符串或 null，"
                f"收到 {disabled_reason!r}。",
                reason=REASON_DIMENSION_LIMIT_EXCEEDED,
            )
        if len(disabled_reason) > MAX_DISABLED_REASON_CHARS:
            raise _validation_error(
                f"{where}.disabledReason",
                f"{where}.disabledReason 不能超过 "
                f"{MAX_DISABLED_REASON_CHARS} 字。",
                reason=REASON_DIMENSION_LIMIT_EXCEEDED,
            )
        if state != ITEM_STATE_DISABLED:
            raise _validation_error(
                f"{where}.disabledReason",
                f"{where}.disabledReason 只在 state="
                f"{ITEM_STATE_DISABLED} 时有意义。",
                reason=REASON_DIMENSION_LIMIT_EXCEEDED,
            )


def _validate_pairings(pairings: Any) -> None:
    """配对数组的形状（场景 key × 形式 key）。

    **本包只做形状校验，不做"配对是否覆盖全部组合"的判定** ——
    那属于内容策略（哪个场景适合哪种形式），而原型里它是"系统建议、
    运营可逐行改配"。把"建议"当约束会让运营改不了。
    形状这一层仍要守：非数组、条目缺 key 都会让下游的 join 静默少行。
    """
    if not isinstance(pairings, list):
        raise _validation_error(
            "pairings", "pairings 必须是数组。", reason=REASON_DIMENSION_LIMIT_EXCEEDED
        )
    if len(pairings) > MAX_DIMENSION_ITEMS * MAX_DIMENSION_ITEMS:
        raise _validation_error(
            "pairings",
            f"pairings 条数 {len(pairings)} 超过上限 "
            f"{MAX_DIMENSION_ITEMS * MAX_DIMENSION_ITEMS}。",
            reason=REASON_DIMENSION_LIMIT_EXCEEDED,
        )
    for index, item in enumerate(pairings):
        where = f"pairings[{index}]"
        if not isinstance(item, dict):
            raise _validation_error(
                where, f"{where} 必须是对象。", reason=REASON_DIMENSION_LIMIT_EXCEEDED
            )
        for side in ("sceneKey", "formKey"):
            value = item.get(side)
            if not isinstance(value, str) or not value.strip():
                raise _validation_error(
                    f"{where}.{side}",
                    f"{where}.{side} 必须是非空字符串。",
                    reason=REASON_DIMENSION_LIMIT_EXCEEDED,
                )


# ---------------------------------------------------------------------------
# 分配：按草稿内容算（**不读库**）
# ---------------------------------------------------------------------------


def _to_allocation_items(rows: Any) -> list[AllocationItem]:
    """草稿的 JSONB 行 → `AllocationItem`（纯数据的边界转换）。

    这是**唯一**把"存储形态"翻成"分配输入"的地方。放在一处的好处是
    `state='DISABLED'` 的处理只有一处：停用项权重归零
    （`permille.DISABLED_PERMILLE`），而不是"从数组里删掉" ——
    删掉会让"这张表上原本有哪几行"这个事实消失（前端会看到行数变少）。
    """
    items: list[AllocationItem] = []
    for row in rows or ():
        disabled = row.get("state") == ITEM_STATE_DISABLED
        items.append(
            AllocationItem(
                key=str(row.get("key", "")),
                # 用常量而不是字面量 `0`：这句话在 docstring 里写了"停用项权重
                # 归零（`permille.DISABLED_PERMILLE`）"，而阶段 7 扫描发现
                # 实现用的是裸 `0` —— 别名 `permille_rules` 就在本文件里。
                # 字面量 0 与常量的取值一旦分叉（比如停用改成 -1‰ 表示"排除"），
                # 这里不会报错、只是悄悄按 0 走。
                weight_permille=(
                    permille_rules.DISABLED_PERMILLE
                    if disabled
                    else int(row.get("weightPermille", 0))
                ),
                capacity=row.get("capacity"),
                sort_order=int(row.get("sortOrder", 0)),
                name=str(row.get("name", "")),
            )
        )
    return items


def dimension_items(draft: Any, dimension: str) -> list[AllocationItem]:
    """取某个维度的分配输入（含停用项，权重已归零）。"""
    if dimension == DIMENSION_SCENE:
        return _to_allocation_items(draft.scene_items)
    if dimension == DIMENSION_FORM:
        return _to_allocation_items(draft.form_items)
    raise ValueError(f"未知的维度：{dimension!r}")


def _normalized_items_for_allocation(
    draft: Any, dimension: str
) -> tuple[list[AllocationItem], list[str]]:
    """取该维度的分配输入，**并归一化到合计 1000‰**。

    返回 `(条目, notes)`；notes 在发生归一化时非空。

    ## 为什么抽成共用函数（错误 3 的机械防线）

    归一化原先只在 `dimension_allocation` 里做，而 `next_batch_allocation`
    直接拿原始条目去分配 ⇒ **同一份草稿，两条路径给出不同的读数**：

        权重合计 2000‰（`save_draft` 有意允许暂存不完整配置）
        · `dimension_allocation`   → 归一为 1000‰ ⇒ {A:5, B:5}
        · `next_batch_allocation`  → 不归一     ⇒ {A:10, B:10}（batch_total=10）

    这不是"两条路径各自的取舍"，而是**同一个问题两个答案** ——
    本仓已踩过的"两套真相"同型。归一化因此必须**只有一处**。

    ## 归一化为什么不放在纯函数里

    `allocate_by_weight` 入口只**拒绝**合计越界（`_check_items`），
    不自动归一：归一化会改变入参语义（"运营存的 280‰ 被改成 282‰"），
    那是调用方要**记录在 notes 里告知用户**的决策，纯函数不该替它做。
    """
    items = dimension_items(draft, dimension)
    total_permille, deviation = permille_rules.validate_total(items)
    if deviation == 0 or total_permille == 0:
        # 偏差 0：已归好，**不重算**（重算会因取整改掉 [333,333,334]）。
        # 合计 0：全部停用，是合法状态，归一化不负责凭空发明比例。
        return items, []

    normalized = permille_rules.normalize_permille(items)
    after, _ = permille_rules.validate_total(normalized)
    return normalized, [
        f"比例合计 {total_permille}‰ ≠ {PERMILLE_TOTAL}‰，已归一为 {after}‰"
    ]


def dimension_allocation(draft: Any, dimension: str) -> AllocationResult:
    """按草稿里的比例算出该维度的分配（**纯计算，不读库、不写库**）。

    ## 三个性质

    1. **先归一，再分配**（见 `_normalized_items_for_allocation`）。
    2. **`total_quantity` 为 `None` 时总量视为 0**，返回全 0 的结果，
       **不报错** —— "先配比例、后定总量"是原型的真实交互顺序
       （§2.4 已登记）。报错会让运营配比例时一直看到失败。
    3. **不静默修正**。归一这一步发生时，`notes` 里写明"比例已归一"，
       让"我配的是 280 怎么变成 282 了"有据可查（而不是让人以为系统算错）。

    ## 为什么 `unallocated > 0` 是合法结果

    全部条目满容时剩余篇数计入 `unallocated`，**不硬塞**（C14 原文）——
    塞进去就是产出重复内容。宁可报"分不完"让人决定：补场景，还是降总量。
    """
    normalized, notes = _normalized_items_for_allocation(draft, dimension)
    total = draft.total_quantity or 0

    result = allocate_by_weight(normalized, total)
    if notes:
        return AllocationResult(
            total=result.total,
            lines=result.lines,
            unallocated=result.unallocated,
            rounds=result.rounds,
            notes=tuple(notes) + tuple(result.notes),
        )
    return result


def dimension_remaining(
    draft: Any, dimension: str, *, allocated_so_far: dict[str, int]
) -> dict[str, int | None]:
    """该维度每条目还剩多少容量（`None` = 无上限）—— 给 WP32 延续配比用。

    **转发 `allocation.capacity_remaining` 而不是在服务层重算**：
    "剩余额度"是前端与后端都要用的同一个量，两处各算一遍
    就会有"前端显示的数字与后端校验的数字不同"（与 WP23 的
    "双端校验共用一个函数"同一条纪律）。
    """
    return capacity_remaining(dimension_items(draft, dimension), allocated_so_far)


def next_batch_allocation(
    draft: Any, dimension: str, *, batch_total: int, allocated_so_far: dict[str, int]
) -> AllocationResult:
    """子配额：本批从**剩余额度**里分（C19"列和恒等于总计划"的落点）。

    委托 `allocate_from_remaining`；本函数只负责把草稿翻成输入。
    算法与"扣减容量而非扣减权重"的理由见该函数的 docstring。

    **与 `dimension_allocation` 共用同一份归一化**（见
    `_normalized_items_for_allocation`）：否则同一份草稿在
    "总分配"与"本批分配"上会出现两套读数（错误 3 实测）。
    """
    normalized, _ = _normalized_items_for_allocation(draft, dimension)
    return allocate_from_remaining(
        normalized,
        allocated_so_far,
        batch_total=batch_total,
    )


# ---------------------------------------------------------------------------
# 固化：草稿 → PLAN 版本
# ---------------------------------------------------------------------------


def submit_draft(
    repo: PlanningRepository,
    version_repo: Any,
    *,
    project_id: uuid.UUID,
    expected_lock_version: int,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> Any:
    """把草稿固化成 `entity_type='PLAN'` 的版本节点。返回 `RegisteredVersion`。

    ## 前置条件（不满足即 422，**不是**静默通过）

    | 条件 | 理由 |
    |---|---|
    | 草稿存在 | 没有草稿就没有"计划"可固化 |
    | `state == 'EDITING'` | 已提交的草稿再提交会产生第二个 PLAN 版本（见模块 docstring 三） |
    | `total_quantity` 已设 | 分配没有总数，固化出来的计划没有数量含义 |
    | 两维比例合计均 == 1000 | **"合计不等于 1000"在固化前必须拦住** —— 放行的话固化出来的计划带着一个需要靠 `normalize` 才能用的比例，而"计划里的比例"与"运营看到的比例"从此不同 |

    ## `lock_version` 双重保护

    前置 `expected_lock_version`（客户端带 `If-Match`）**不够** ——
    它挡不住两个同时读到同一版本的请求。因此这里用
    `get_draft_for_update`（`SELECT … FOR UPDATE`）做行锁：
    第二个事务会**等**，等到时看到 `state='SUBMITTED'` 从而被 409 挡下。

    这也是"为什么不用乐观锁代替行锁"的答案：乐观锁下两个事务都会
    通过校验然后各自写入，产出两个 PLAN 版本 —— 那是**结构性**的错误，
    不是"重试一下就好"的并发抖动。

    ## `version_repo` 由端点注入，不在本函数里造

    与 WP22 `create_baseline(repo, version_repo, …)` 同款。
    `service` 只接收仓储实例、拿不到 `db` session；若在这里
    `import get_version_repository` 就需要一个 session 来源 ——
    那会把"依赖从哪来"下沉到业务层，且更糟：**它会造出与 `repo`
    不同 session 的第二个仓库**，于是"刚在 `repo` 事务里读到的草稿"
    与"版本注册所在的事务"变成两个（WP17 的 ISS-54 同族故障）。
    由端点注入同一个 session 上的实例是唯一干净的位置。

    ## 固化后草稿**不删除**

    只把 `state` 改成 `SUBMITTED`。删掉会让"这份计划是基于哪些场景配的"
    这个事实消失，而原型要展示的正是那张配比表。

    ## ★ 提交**会消耗一个版本号**（实测确认，与 `api.py` 初稿假设相反）

    收尾那次 `update_draft` 走的是可变资源那条条件 UPDATE，
    而它**无条件**写 `lock_version = lock_version + 1`（`repository.update_draft`
    第 465 行）。因此**提交后 `lock_version` 必然 +1**，
    即使这次只改了 `state` 而没改任何内容。

    | 客户端重试时带的 ETag | 命中处 | 码 |
    |---|---|---|
    | `"<id>:<提交前版本>"` | `require_if_match`（更早的拦截点） | **412** |
    | `"<id>:<提交后版本>"`（重读得到） | 本函数的状态判定 | **409** |

    **两条都是正确的拒绝**，且构成层次关系：乐观锁先拦、业务语义后拦。

    **为什么不去掉 `+1` 以迎合"状态变更不该动版本"的直觉**：
    若状态变更不递增版本，则"改内容"与"改状态"两个并发请求
    可以**同时**命中同一个 `lock_version` 的条件 UPDATE（各自都通过），
    于是"固化的内容"与"客户端以为的内容"可能不是同一份 ——
    而那正是本端点要求 `If-Match` 要堵的静默缺口。
    递增版本让两个请求里**必有一个**失败重来，宁慢不错。
    """
    _require_project(repo, project_id)

    # ⚠️ 行锁必须在**读内容之前**（见模块 docstring 三）。
    draft = repo.get_draft_for_update(project_id)
    if draft is None:
        raise _not_found(
            f"项目 {project_id} 还没有计划草稿。",
            reason=REASON_DRAFT_NOT_FOUND,
            projectId=str(project_id),
        )

    if draft.state not in DRAFT_STATES:  # pragma: no cover - CHECK 已保证
        raise RuntimeError(f"草稿状态非法：{draft.state!r}")
    if draft.state != DRAFT_STATE_EDITING:
        raise _conflict(
            "该草稿已提交过，不能重复提交。",
            reason=REASON_DRAFT_ALREADY_SUBMITTED,
            draftState=draft.state,
        )

    if draft.lock_version != expected_lock_version:
        raise version_conflict(
            resource_id=draft.id,
            current_version=draft.lock_version,
            reason="lock_version_mismatch",
            message="计划草稿已被更新，请重新读取最新版本后再提交。",
        )

    if draft.total_quantity is None:
        raise _validation_error(
            "totalQuantity",
            "提交前必须设置总量（分配需要它）。",
            reason=REASON_TOTAL_QUANTITY_INVALID,
        )

    scene_result = dimension_allocation(draft, DIMENSION_SCENE)
    form_result = dimension_allocation(draft, DIMENSION_FORM)
    _require_permille_total(draft, DIMENSION_SCENE, scene_result)
    _require_permille_total(draft, DIMENSION_FORM, form_result)

    payload = plan_payload(
        draft,
        scene_result=scene_result,
        form_result=form_result,
    )

    # --- 写版本节点（WP11 接缝，**原样调用不改签名**） -------------------
    from rednote.modules.versions.service import link_dependency, register_version

    registered = register_version(
        version_repo,
        entity_type=ENTITY_TYPE_PLAN,
        # ⚠️ entity_id 是 **project_id** 而不是 draft.id：
        # PLAN 的逻辑实体是"该项目的那条计划链"，草稿只是它当前的工作区。
        # 传 draft.id 会让"同一项目的第二份计划"拿到修订号 1 而不是 2
        # （与 WP22 的 BASELINE 同一条理由）。
        entity_id=project_id,
        project_id=project_id,
        payload=payload,
        created_by=actor_id,
    )

    # --- E8 依赖边：PLAN → BASELINE（与 PLAN 版本注册同一事务） ----------
    # parent 用 PLAN 的**节点 id**（`entity_id` 是 project_id，多修订共用）；
    # `draft.baseline_id` 即 `production_baselines.id`，同时是 BASELINE 的
    # 版本节点 id（WP22 `id` FK `version_nodes.id`）。可空外键，空则跳过。
    # 写边失败不吞：异常上抛，整个事务（含 PLAN 节点）回滚。
    if draft.baseline_id is not None:
        link_dependency(
            version_repo,
            parent_version_id=registered.node_id,
            dependency_version_id=draft.baseline_id,
            purpose=PURPOSE_PLAN_BASELINE,
        )

    # --- 改草稿状态（条件 UPDATE，带 lock_version） ----------------------
    updated = repo.update_draft(
        draft_id=draft.id,
        expected_lock_version=draft.lock_version,
        baseline_id=None,
        total_quantity=None,
        state=DRAFT_STATE_SUBMITTED,
        scene_items=None,
        form_items=None,
        pairings=None,
    )
    if updated == 0:  # pragma: no cover - 行锁在，理论不可达
        # 持 `FOR UPDATE` 行锁期间不会有别人改这一行 ⇒ 走到这里说明
        # 有人绕过了行锁（真缺陷）。报出来而不是静默继续 ——
        # 静默继续会让草稿停在 EDITING，于是**可以再提交一次**，
        # 产出第二个 PLAN 版本。
        raise _conflict(
            "提交时草稿状态被并发修改，请重新读取后再试。",
            reason="draft_update_race",
            draftId=str(draft.id),
        )
    repo.flush()
    return registered


def _require_permille_total(
    draft: Any, dimension: str, result: AllocationResult
) -> None:
    """固化前的最后一道：该维度**启用项**比例合计必须严格 == 1000。

    **为什么不用 `result`（已归一的分配）来判**：分配路径会归一，
    因此它永远"看起来对"。要判的是**草稿里存的比例** ——
    否则"运营存了一份合计 990‰ 的配比"会被静默归一后固化，
    而计划里的人看到的比例与他配的不是同一组数字。
    """
    items = dimension_items(draft, dimension)
    total, _deviation = permille_rules.validate_total(items)
    # 全停用（合计 0）是合法状态：那意味着这个维度不产出 ——
    # 但固化一份"不产出任何内容"的计划没有意义，因此这里也拒绝。
    if total != PERMILLE_TOTAL:
        raise _validation_error(
            _ITEMS_FIELD[dimension],
            f"{_ITEMS_FIELD[dimension]} 的启用项比例合计为 {total}‰，"
            f"必须等于 {PERMILLE_TOTAL}‰ 才能提交。",
            reason=REASON_PERMILLE_NOT_TOTAL,
            actualTotal=total,
            expectedTotal=PERMILLE_TOTAL,
        )


def plan_payload(
    draft: Any,
    *,
    scene_result: AllocationResult,
    form_result: AllocationResult,
) -> dict[str, Any]:
    """固化进 `version_nodes.payload` 的内容（`payload_hash` 的输入）。

    ## 为什么 payload 里同时放**输入**与**结果**

    只放结果：事后无法回答"当时配的比例是多少"（结果是从比例算来的，
    而最大余数法**不可逆** —— 从 `[3,3,2,1,1]` 推不出 `[280,280,200,...]`）。
    只放输入：事后无法回答"当时的分配是什么"，而下游（WP31/32）要的是数量。

    两者都在，`payload_hash` 才是"这份计划"的完整指纹。

    ## 为什么**不含** `revision_no` / 时间戳 / actor

    `payload` 进 `payload_hash`，而哈希相同 = 同一份内容 = 幂等命中。
    把时间戳放进去会让"同一份内容两次提交得到不同哈希"，
    而那次"第二次提交"本该被识别成幂等重复（网络重试的常见形态）。

    修订号与操作人在 `version_nodes` 自己的列上，不在 payload 里 ——
    它们描述的是"这次注册行为"，不是"这份计划的内容"。
    """
    return {
        "schemaVersion": draft.schema_version,
        "projectId": str(draft.project_id),
        "baselineId": None if draft.baseline_id is None else str(draft.baseline_id),
        "totalQuantity": draft.total_quantity,
        "scenes": [
            {
                "key": line.key,
                "weightPermille": line.weight_permille,
                "allocated": line.allocated,
                "capacity": line.capacity,
                "sortOrder": line.sort_order,
            }
            for line in scene_result.lines
        ],
        "forms": [
            {
                "key": line.key,
                "weightPermille": line.weight_permille,
                "allocated": line.allocated,
                "capacity": line.capacity,
                "sortOrder": line.sort_order,
            }
            for line in form_result.lines
        ],
        "unallocated": {
            DIMENSION_SCENE: scene_result.unallocated,
            DIMENSION_FORM: form_result.unallocated,
        },
        "pairings": [dict(row) for row in (draft.pairings or ())],
    }


__all__ = [
    "DIMENSION_FORM",
    "DIMENSION_SCENE",
    "ENTITY_TYPE_PLAN",
    "DraftView",
    "dimension_allocation",
    "dimension_items",
    "dimension_remaining",
    "draft_etag",
    "draft_view",
    "next_batch_allocation",
    "plan_payload",
    "require_draft",
    "save_draft",
    "submit_draft",
]
