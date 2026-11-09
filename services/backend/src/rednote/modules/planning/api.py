"""场景形式草稿 / 数量配置的端点（WP24）。

| 方法 | 路径 | 成功码 | 权限 | 依据 |
|---|---|---|---|---|
| GET | `/projects/{projectId}/plan-draft` | 200 | `VIEW` | **本包补充**（步骤 4 工作区读取） |
| PUT | `/projects/{projectId}/plan-draft` | 200 | `EDIT` | **本包补充**（步骤 4 工作区保存） |
| POST | `/projects/{projectId}/plan-draft/submissions` | 201 | `EDIT` | **本包补充**（固化出 PLAN 版本） |
| GET | `/settings/global` | 200 | 登录 | **本包补充**（ADR-13「运营能自助确认当前生效值」） |
| GET | `/settings/global/{key}/history` | 200 | 登录 | **本包补充**（ADR-13 审计读数） |
| PUT | `/settings/global/{key}` | 200 | `KNOWLEDGE_MANAGE` | **本包补充**（全局配置变更） |
| GET | `/settings/projects/diverged` | 200 | 登录 | §3.6.4（分叉显式暴露，`?key=` 可选） |
| PUT | `/settings/projects/{projectId}/{key}` | 200 | `EDIT` | **本包补充**（项目级覆盖） |

协议 §7（步骤 4 的端点表）在本仓**没有副本**（§1.4 第 11 条），
因此八个端点的形状全部由本包定义，路径按既有命名风格
（`{projectId}` / 连字符 / 复数资源名）。已登记 OP-176 待协议确认。

---

## 一、`/settings/global` 与 `/settings/projects/...` 为什么用**两个**前缀

它们看起来可以合并成一个 `/settings`，但**权限口径完全不同**：

| 前缀 | 作用域 | 谁可写 | 判定方式 |
|---|---|---|---|
| `/settings/global*` | 全平台唯一真值 | 管理者 + 运营（`KNOWLEDGE_MANAGE`） | `require_org_action`（**无项目上下文**） |
| `/settings/projects/...` | 单个项目 | 该项目负责人 / 协作者 | `ProjectAccessDep`（**有项目上下文**） |

`GET /settings/projects/diverged` 是**跨项目**读取，因此既不属于项目级
（没有单一 `projectId`）、也不写任何东西 —— 它用 `CurrentUserDep`
（登录即可），与"知识库全员可见"同一条取向：**分叉信息不该被藏起来**，
藏着就回到了"分叉了没人知道"（§3.6.4 的杀伤路径）。

## 二、★ 端点里**不出现**任何"副本"概念

§2.6 实现约束第 4 条：

> **写 `runtime_settings` 的路径只有两条**：`change_global_setting()` 与
> `change_project_setting()`。**副本的插入是这两条路径与惰性物化的内部行为**，
> 不得成为第三个公开入口。

因此本文件**没有**"补副本"、"刷新副本"这类端点 —— 它们是
`settings_service` 的内部行为，在 `PUT /settings/global/{key}` 的
**同一个事务**里发生。§3.6.2 的级联刷新对客户端是**不可见的**：
客户端只发一个 PUT，副本跟着变。

这一条是判据 J2 能否成立的前提：只要存在第三个写入口，
"副本跟随全局"就有了绕过口，而 J2 只能测到"走服务层时成立"。

## 三、`GET .../plan-draft` 会**写库**（已知代价，与 WP18/WP22 同款）

`service.require_draft` 在草稿不存在时**懒创建**一行。因此这个 `GET`
违反"GET 是安全方法"的 HTTP 约定。它幂等（`plan_drafts.project_id`
唯一约束兜底），且协议把"创建初始工作区"归给项目创建事务 ——
而项目创建是 WP10 的代码，本包不能改它（跨包接缝，与并行开发纪律冲突）。

**收口方向**：让主 agent 在项目创建事务里补一次调用，那时本端点退化成纯读。
与 `brief/api.py::get_brief`、`baseline/api.py::get_information_draft`
**完全同款**（三处已登记同一件事）。

## 四、`If-Match`：三个写端点都要

| 端点 | 为什么 |
|---|---|
| `PUT .../plan-draft` | 草稿是可变资源（协议 §1.1 直接要求） |
| `POST .../plan-draft/submissions` | ⚠️ **协议没要求，本包加的**。理由见下 |
| `PUT /settings/global/{key}`、`PUT /settings/projects/...` | **不带** —— 见第五节 |

`POST .../submissions` 必须带，因为存在一个**静默缺口**：
请求体里的 `reason` 来自客户端屏幕，而固化进 `payload` 的场景/形式比例
与分配结果来自**服务端当时读到的草稿**。两者若来自不同版本，
固化出来的 PLAN 会把"客户端以为的内容"与"服务端实际算出的数字"
混在一起，且**不报错**（HTTP 201、数字都对，只是不属于同一时刻）。

与 `baseline/api.py::create_baseline` 同款，且**与"超时重试必须成功"不冲突**：

> ⚠️ **本节初稿的前提是错的，已实测更正**（第 4 层用例
> `test_plan_draft_submit_retry_is_rejected_in_two_distinct_ways` 钉住）。
>
> 初稿写的是"提交会把 `state` 改成 `SUBMITTED`，重试时同一 ETag 仍指向同一
> `lock_version`（**状态变更不改 `lock_version`**）"。而
> `repository.update_draft` 第 465 行**无条件**写
> `lock_version = lock_version + 1` —— 提交走的正是这个函数，
> 因此**提交后 `lock_version` 会 +1**。

实际行为因此是**两种码都出现**，且都是正确的拒绝：

| 客户端第二次带的 ETag | 命中处 | 码 | 客户端该做什么 |
|---|---|---|---|
| `"<id>:1"`（第一次读到的） | `require_if_match` | **412** | 重新读草稿 ⇒ 看到 `SUBMITTED` |
| `"<id>:2"`（重读后拿到的） | `submit_draft` 的状态判定 | **409** | 显示"已提交"，不要重试 |

两条合起来才是完整的幂等语义：**旧 ETag 在更早的拦截点被拒**（412 发生在
`require_if_match`，早于任何业务判定），**新 ETag 才走到状态判定**（409）。
它们不冲突，而是"乐观锁先拦、业务语义后拦"的层次关系。

**为什么保留"递增版本"这个实现而不是去迎合初稿**：递增版本是更安全的语义 ——
若状态变更不递增版本，那么"改内容"与"改状态"两个并发请求可以**同时通过**
条件 UPDATE（各自拿同一个 `lock_version` 命中 WHERE），
于是"提交时固化的内容"与"客户端以为的内容"可能不是同一份，
而这正是本端点要 `If-Match` 想防的那件事（静默缺口）。

## 五、配置端点的 `If-Match`：**必须不带**，理由是"预期冲突不冲突"

`runtime_settings` 是**追加式不可变**表（每次变更插一行新修订号），
它没有 `lock_version`，**也没有"当前值被并发改坏"这个概念**：
两个运营同时改全局上限，产生的是修订号 N+1 与 N+2 两条历史，
后者生效 —— 这是**合理**的结果（后写的赢），不是数据事故。

若给配置端点强加 `If-Match`，就得为它凭空造一个版本号，
而那个版本号的语义与 `lock_version` 不同（配置不是"覆盖式编辑"），
会让客户端以为"配置也怕并发"。**协议 §1.1 的 If-Match 适用于
"可变资源的覆盖式修改"，配置不属此类** —— 它的并发语义由
`pg_advisory_xact_lock`（按 key 串行化）保证，比 ETag 更强：
ETag 只能让一个客户端失败重试，advisory lock 让两个写入**不可能交错**。

这条已登记：若协议作者要求配置端点也带 `If-Match`，
处置是把 `revision_no` 当作版本号（`build_etag` 的输入换成它）——
**那是一次协议层修订，不是本包能自行决定的**。

## 六、响应组装只读 `service` 的返回值，不戳 ORM 实例

三个端点的响应都由 `service.draft_view` / `draft_etag` /
`allocation` 的返回值拼出（`baseline/api.py::_draft_view` 的同款纪律）：
ORM 实例绑在 session 上，而响应组装发生在 session 关闭之后
（`DetachedInstanceError`）。本文件的三个 `_xxx_body` 帮助函数
**只接收 frozen 值对象或 ORM 实例用于算 ETag**，不读其它属性。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, status

from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.planning import service, settings_service
from rednote.modules.planning.dependencies import PlanningRepoDep, VersionRepoDep
from rednote.modules.planning.schema import (
    AllocationBody,
    BatchLimitsBody,
    ChangeGlobalSettingBody,
    ChangeProjectSettingBody,
    DivergedProjectBody,
    PlanDraftBody,
    SaveDraftBody,
    SettingEntryBody,
    SettingHistoryEntryBody,
    SettingOverviewBody,
    SettingValueBody,
    SubmitDraftBody,
    SubmittedPlanBody,
)
from rednote.modules.planning.types import (
    BATCH_KINDS,
    PLACEHOLDER_SETTING_KEYS,
    SETTING_KEY_BATCH_LIMITS,
    SETTING_KEYS,
)
from rednote.modules.projects.dependencies import (
    ProjectAccessDep,
    require_org_action,
)
from rednote.modules.projects.permissions import OrgAction, ProjectAction
from rednote.platform.api_responses import READ_RESPONSES, WRITE_RESPONSES
from rednote.platform.concurrency import require_if_match
from rednote.platform.errors import DataEnvelope, get_request_id
from rednote.platform.pagination import single_response

router = APIRouter(tags=["planning"])

# 与 `baseline/api.py` 同款别名：端点声明里用短名，读起来不被前缀淹没。
PLANNING_READ_RESPONSES = READ_RESPONSES
PLANNING_WRITE_RESPONSES = WRITE_RESPONSES

# 全局配置写端点的依赖列表。
#
# ⚠️ **必须包在 `Depends(...)` 里**：`require_org_action` 返回的是一个
# **依赖函数**，直接把它放进 `dependencies=[...]` 会让 FastAPI 尝试读它的
# `.dependency` 属性并抛 `AttributeError`（`knowledge/api.py` 第 106~109 行
# 已就本坑写过注释）。
#
# 动作取 `KNOWLEDGE_MANAGE` 而不是 `MANAGE_DICTIONARY`：
# `_ORG_MATRIX[ROLE_OPERATOR][KNOWLEDGE_MANAGE] = True`，是运营**唯一**
# 开放的组织级操作；而 `MANAGE_DICTIONARY` 对运营为 False ——
# 用它会把"生成部运营改批次上限"这条路径整条堵死，
# 而 ADR-13 的验证方式第 3 条明确要求"运营能在页面上自助确认/调整当前生效值"。
GLOBAL_SETTING_WRITE = [Depends(require_org_action(OrgAction.KNOWLEDGE_MANAGE))]


# ---------------------------------------------------------------------------
# 响应组装（只读 service 的返回值）
# ---------------------------------------------------------------------------


def _draft_body(
    draft: Any,
    *,
    with_allocation: bool = True,
) -> PlanDraftBody:
    """草稿 → 响应体（含两维分配读数）。

    **分配与草稿在同一处算出**：调用方刚读到的 `draft` 实例直接交给
    `dimension_allocation`（纯函数，不读库），因此"这次读/写"与"
    这次算出的分配"天然同版 —— 不需要额外的版本比对。

    `with_allocation=False` 的落点：本包目前没有只用草稿不关心分配的端点，
    但保留该开关让"某天加一个轻量读端点"时不必复制本函数。
    `# pragma: no cover` 不加 —— 开关被默认调用路径覆盖（默认 True）。
    """
    view = service.draft_view(draft)
    scene_allocation: AllocationBody | None = None
    form_allocation: AllocationBody | None = None
    if with_allocation:
        scene_allocation = AllocationBody.of(
            service.dimension_allocation(draft, service.DIMENSION_SCENE)
        )
        form_allocation = AllocationBody.of(
            service.dimension_allocation(draft, service.DIMENSION_FORM)
        )
    return PlanDraftBody.of(
        view,
        # ETag 的算法只有一处（`service.draft_etag`），这里只做透传。
        etag=service.draft_etag(draft),
        scene_allocation=scene_allocation,
        form_allocation=form_allocation,
    )


def _setting_body(setting: Any) -> SettingValueBody:
    return SettingValueBody.of(setting)


def _overview_body(
    repo: Any,
) -> SettingOverviewBody:
    """`GET /settings/global`：**全部合法键**的全局视图（`project_id=None`）。

    ## 为什么必须遍历 `SETTING_KEYS` 而不是查表里已有的行

    占位键（`platform.mediaLimits`）**可能一行都没有** —— 它的合法状态就是
    "没有值"。若按"表里有什么就返回什么"来组装，这个键就从响应里消失了，
    而"这个键还没填值"正是最需要被看见的信息（OP-64 待实测）。
    遍历常量集合让"键的完整性"由 `SETTING_KEYS` 单点保证。

    ## `materialize=False`

    这是**全局视图**（`project_id=None`），压根不走项目副本那条分支，
    因此这个参数在这里没有实际作用 —— 但显式写出来是为了让读者知道
    "本端点不会写库"（惰性物化只发生在带 `project_id` 的读路径上）。
    """
    entries: list[SettingEntryBody] = []
    values_by_key: dict[str, Any] = {}
    for key in sorted(SETTING_KEYS):
        setting = settings_service.get_effective_limits(
            repo, key, None, materialize=False
        )
        # 顺手记下 value，供下面 `BatchLimitsBody.of` 复用（MINOR-6 的修复）：
        # 初版在循环后又单独查了一次 `batch.limits`，同一个键两次往返。
        # 两次结果在**同一事务内**必然相同 —— 但将来若给读取加缓存，
        # 它们就会出现分歧，而症状是同一个响应里两个字段互相矛盾。
        values_by_key[key] = setting.value
        entries.append(
            SettingEntryBody(
                key=setting.key,
                configured=setting.configured,
                is_placeholder=key in PLACEHOLDER_SETTING_KEYS,
                value=setting.value,
                revision_no=setting.revision_no,
            )
        )

    return SettingOverviewBody(
        entries=entries,
        # `of()` 已处理"缺档返回 None"，因此这里不必判断 `configured`
        # ——两者是独立的判定（见 `BatchLimitsBody.of` 的 docstring）。
        batch_limits=BatchLimitsBody.of(values_by_key[SETTING_KEY_BATCH_LIMITS]),
    )


def _history_body(repo: Any, *, key: str) -> list[SettingHistoryEntryBody]:
    """`GET /settings/global/{key}/history`：**全作用域**的变更历史。

    含全局行与项目行（`repository.list_history` 的既有语义），
    因此一次请求就能回答"这个键被谁改过" —— 而不必先查全局再逐项目查。
    `scopeKind` 与 `projectId` 两列让前端能分清"改了全平台"与"改了某个项目"。

    **不在这里做权限过滤**：`runtime_settings` 里没有"项目私有配置"
    —— 全部键都是平台级机制（`batch.limits` / `research.maxEffectiveFiles` /
    `platform.mediaLimits`），项目行只是"这个项目选了什么值"。
    与"知识资料跨项目全员可见"同一条取向（`projects/permissions.py` 的
    实施默认）。若将来出现"项目私有配置键"，这个端点的权限口径必须重审
    —— 已登记在本文件的第七节。
    """
    return [
        SettingHistoryEntryBody(
            revision_no=row.revision_no,
            scope_kind=row.scope_kind,
            project_id=row.project_id,
            origin=row.origin,
            value=dict(row.value),
            actor_id=row.actor_id,
            reason=row.reason,
            request_id=row.request_id,
            created_at=row.created_at.isoformat(),
        )
        for row in repo.list_history(key=key)
    ]


# ---------------------------------------------------------------------------
# 项目级：步骤 4 工作区
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/plan-draft",
    response_model=DataEnvelope[PlanDraftBody],
    responses=PLANNING_READ_RESPONSES,
    summary="读取场景形式草稿（含两维分配读数）",
)
def get_plan_draft(
    access: ProjectAccessDep,
    repo: PlanningRepoDep,
) -> dict[str, Any]:
    """读步骤 4 工作区（模块 docstring 第三节：这个 GET 会写库）。

    **分配读数折在这里而不是另开端点**：原型 `trial-plan.html` 的界面是
    "配比表 + 每类几条"在**同一块面板**里，分两个端点只会让前端发两次请求、
    并引入"两次读之间草稿已变"的窗口 —— 那时前端会拿着过期的分配去
    决定"能不能提交"。与 WP22 `GET .../information-draft` 折进 `readiness`
    是同一条理由（那边是三条完成条件，这边是两维分配）。
    """
    access.require(ProjectAction.VIEW)
    draft = service.require_draft(repo, project_id=access.project_id)
    return single_response(_draft_body(draft))


@router.put(
    "/projects/{projectId}/plan-draft",
    response_model=DataEnvelope[PlanDraftBody],
    responses=PLANNING_WRITE_RESPONSES,
    summary="保存场景形式草稿（部分更新，需 If-Match）",
)
def save_plan_draft(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: PlanningRepoDep,
    body: SaveDraftBody,
    request: Request,
) -> dict[str, Any]:
    """保存草稿（**部分更新**，`None` = 本次不改这一块）。

    **`If-Match` 必带**（协议 §1.1）。两层并发保证都要有 ——
    前置校验（这里）挡"客户端拿的是过期值"，条件 UPDATE
    （`repository.update_draft` 的 `WHERE lock_version = ?`）挡
    "两个请求同时通过前置校验"。

    ## 为什么权限是 `EDIT` 而不是 `REVIEW`

    草稿是**方案内容的输入**（`ProjectAction.EDIT` 的注释是"编辑 Brief、
    **方案**、内容"）。WP22 的审核工作区用 `REVIEW` 是因为它是八步里
    唯一的人工确认闸门；本包的草稿是**生产参数**，运营拖比例就保存，
    不是审核动作。与 WP18 `PUT /brief-draft` 用 `EDIT` 同款。

    ## 比例合计**不在这里强制等于 1000**

    运营拖数字的过程中合计必然不等于 1000（§2.2 用户画像：他**会算不对**），
    而保存是"拖一下就自动保存"的交互。强制等于 1000 会让每拖一次都失败。
    真正的关口在提交（`submit_draft`）——
    **能保存不合法的中间态，不能把它固化成版本**。
    """
    access.require(ProjectAction.EDIT)
    draft = service.require_draft(repo, project_id=access.project_id)

    # 抛 428（缺）/ 400（格式非法）/ 412（不匹配，回带 currentEtag）。
    require_if_match(
        request,
        resource_id=draft.id,
        current_version=draft.lock_version,
        resource_label="计划草稿",
    )

    updated = service.save_draft(
        repo,
        project_id=access.project_id,
        expected_lock_version=draft.lock_version,
        baseline_id=body.baseline_id,
        total_quantity=body.total_quantity,
        # ⚠️⚠️ 三处 `model_dump` **必须显式 `by_alias=True`**。
        #
        # `DimensionItemBody` / `PairingBody` 用 alias 声明字段名
        # （`weightPermille` / `sortOrder` / `sceneKey` / `formKey`），
        # 而 pydantic 的 `model_dump()` **默认 `by_alias=False`**，
        # 于是它吐出的是 **snake_case**（`weight_permille` / `sort_order` /
        # `scene_key` / `form_key`）。服务层的白名单恰恰是 camelCase：
        # `service._ALLOWED_ITEM_KEYS = {'capacity','key','name',
        # 'sortOrder','state','weightPermille'}`、`_validate_pairings`
        # 读 `item.get("sceneKey")` / `item.get("formKey")`。
        #
        # 漏掉 `by_alias=True` 的后果不是"少个字段"，而是**整条保存/提交
        # 路径 422**（`sceneItems[0] 含未知字段：['sort_order',
        # 'weight_permille']`）—— 而**测试库的第 1/2/3 层全绿也拦不住它**，
        # 因为它只在 HTTP 边界显形（模型对象 → dict 的那一步）。
        # 本包实测踩过一次，已登记 ISS。
        #
        # ⚠️ `DimensionItemBody` 是 pydantic 模型，服务层的校验函数按
        # **映射**取值（`_validate_dimension` 里 `item.get(...)`）。
        # 直接把模型对象传下去会让 `isinstance(item, Mapping)` 为 False
        # 而产生"形状错误"的 422（而输入其实完全合法）。
        # `exclude_none=True` 让未填的可空字段不出现在 dict 里，
        # 与"JSONB 行里不该有没意义的 null"一致。
        scene_items=(
            None
            if body.scene_items is None
            else [
                item.model_dump(exclude_none=True, by_alias=True)
                for item in body.scene_items
            ]
        ),
        form_items=(
            None
            if body.form_items is None
            else [
                item.model_dump(exclude_none=True, by_alias=True)
                for item in body.form_items
            ]
        ),
        pairings=(
            None
            if body.pairings is None
            else [item.model_dump(by_alias=True) for item in body.pairings]
        ),
        actor_id=user.id,
        request_id=get_request_id(),
    )
    return single_response(_draft_body(updated))


@router.post(
    "/projects/{projectId}/plan-draft/submissions",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[SubmittedPlanBody],
    responses=PLANNING_WRITE_RESPONSES,
    summary="提交并固化计划（生成 PLAN 版本节点，需 If-Match）",
)
def submit_plan_draft(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: PlanningRepoDep,
    version_repo: VersionRepoDep,
    body: SubmitDraftBody,
    request: Request,
) -> dict[str, Any]:
    """把草稿固化成 `entity_type='PLAN'` 的版本节点（模块 docstring 第四节）。

    ## 为什么路径是 `.../plan-draft/submissions` 而不是 `.../plans`

    固化**不改实体身份**：PLAN 的 `entity_id` 就是 `project_id`
    （`submit_draft` 的注释已论证），一个项目只有一条计划链。
    因此"提交"是**草稿上的一个动作**，不是"新建一个计划资源"。
    路径上的 `submissions` 表达了"这是一次提交行为"（产生一条提交记录），
    与 `brief-versions` / `baselines` 那种"新建资源"的语义区分开。

    另一个方案是 `POST .../plan-draft/submit`（动词），否决理由：
    本仓路径全部用名词复数（`.../brief-parse-jobs`、`.../step-approvals`），
    动词式路径会破坏一致性，且 `submissions` 天然可扩展成
    "列出历史提交"（本包不做，需要时可加 `GET` 同一路径）。

    ## 权限与 `If-Match`

    权限 `EDIT`（与保存草稿一致 —— 能改就能提交）。
    `If-Match` **必带**，理由见模块 docstring 第四节。

    **`reason` 只有服务层用**（写进版本注册的审计），
    端点不做校验 —— 空串的判定在 `submit_draft` 的下游（`register_version`
    的 payload 审计）。本端点不重写这条规则（"两处口径只能有一处"）。
    """
    access.require(ProjectAction.EDIT)
    draft = service.require_draft(repo, project_id=access.project_id)

    require_if_match(
        request,
        resource_id=draft.id,
        current_version=draft.lock_version,
        resource_label="计划草稿",
    )

    registered = service.submit_draft(
        repo,
        version_repo,
        project_id=access.project_id,
        expected_lock_version=draft.lock_version,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    # `body.reason` 在此处**不参与业务判定**，只作为审计上下文透传。
    # ⚠️ 它当前没有下游接收点：`register_version` 的签名（WP11 接缝）
    # 没有 `reason` 参数，而不改那个签名是本包的既定约束。
    # 因此理由随 `request_id` 一起只在请求日志与 `plan_drafts` 的
    # 审计上下文里可见 —— **这是一处已知的信息损耗**，已登记收口事项
    # （修法：WP11 的 `register_version` 增一个可选 `note` 参数，
    # 属跨包接缝改动，需与主 agent 协调，不在本包单方面改）。
    del body
    return single_response(SubmittedPlanBody.of(registered))


# ---------------------------------------------------------------------------
# 全局配置：读（登录即可）
# ---------------------------------------------------------------------------


@router.get(
    "/settings/global",
    response_model=DataEnvelope[SettingOverviewBody],
    responses=PLANNING_READ_RESPONSES,
    summary="读取当前生效的全局配置（含未配置的占位键）",
)
def get_global_settings(
    user: CurrentUserDep,
    repo: PlanningRepoDep,
) -> dict[str, Any]:
    """读全部配置项的全局生效值。

    ## ADR-13 验证方式第 3 条：「运营能在页面上自助确认当前生效值」

    这个端点就是那条验证方式的**落点**。它**不要求任何项目上下文**，
    也不要求管理权限 —— 理由是 ADR-13 那句话的主语是"运营"，
    而运营不是管理者。若把读也收进 `KNOWLEDGE_MANAGE`，
    运营就要靠管理者截图才能知道自己改的上限生效了没有。

    **它不写库**（`materialize=False`，且 `project_id=None`），
    因此是一个真正的安全方法 —— 与 `GET .../plan-draft` 形成对比
    （那个会懒创建草稿，见模块 docstring 第三节）。

    `user` 参数**只是为了让依赖链上有身份校验**（未登录 → 401）。
    它的取值不被使用，因此显式 `del` 掉而不是留一个未使用变量 ——
    若将来这个端点需要"只看自己项目的分叉"，参数already 在此。
    """
    del user
    return single_response(_overview_body(repo))


@router.get(
    "/settings/global/{key}/history",
    response_model=DataEnvelope[list[SettingHistoryEntryBody]],
    responses=PLANNING_READ_RESPONSES,
    summary="读取某个配置键的变更历史（ADR-13 审计）",
)
def get_global_setting_history(
    user: CurrentUserDep,
    repo: PlanningRepoDep,
    key: Annotated[str, Path(description="配置键，如 batch.limits")],
) -> dict[str, Any]:
    """读出某个键的全部变更行（新到旧由 `revision_no` 升序给出，见下）。

    ## 为什么这个端点**必须有**（ADR-13 的审计要求）

    ADR-13 把"审计不可篡改"与"审计完整"列为**验收条件**
    （F2 的验收映射）。不可篡改由触发器保证（DDL 的
    `trg_runtime_settings_immutable`），而"审计**完整**"——即"改了什么、
    谁改的、为什么"能被读出来——**必须有读取出口**。
    没有它，审计记录只能查库，而"按 ADR-13 的说法，不可审计即不可验收"。

    ## 顺序是 `revision_no` **升序**（不是降序）

    `repository.list_history` 的既有语义。前端要展示的是"变更时间线"，
    而时间线天然是从旧到新读。降序会给前端一个反向列表，
    而它很容易忘了反转（症状是"最新一条显示在最下面"，
    看起来像功能而不是 bug）。

    ## 未知键返回 422 而不是空列表

    空列表会让"键名拼错"表现为"这个键从没被改过" —— 而运营会据此
    以为"配置没被谁动过"。`settings_service.ensure_setting_key` 给出
    可读的 422，**且它是纯校验、不查库**（MINOR-7 的修复）：
    此前这里用 `get_effective_limits(…, materialize=False)` 当校验用，
    返回值丢弃但**真的查了一次库**，随后 `_history_body` 又查一次 ——
    注释里写的"不为未知键先查历史"与实现相反。
    """
    del user
    settings_service.ensure_setting_key(key)
    return single_response(_history_body(repo, key=key))


@router.get(
    "/settings/projects/diverged",
    response_model=DataEnvelope[list[DivergedProjectBody]],
    responses=PLANNING_READ_RESPONSES,
    summary="列出生效值与全局值不同的项目（OP-60 分叉显式暴露）",
)
def list_diverged_projects(
    user: CurrentUserDep,
    repo: PlanningRepoDep,
    key: Annotated[str, Query(description="配置键，默认 batch.limits")] = (
        SETTING_KEY_BATCH_LIMITS
    ),
) -> dict[str, Any]:
    """§3.6.4 的落点：**分叉必须可见**。

    ## 为什么它不挂在 `/projects/{projectId}/...` 下

    它返回的是**跨项目**的清单 —— 挂在某个项目下会暗示"这是那个项目的
    属性"，而恰恰相反：它是"哪些项目与全局不一样"，
    是**管理者视角**的视图（§3.6.4 原文："管理者才知道要去看哪几个"）。

    ## `key` 为什么是**查询参数**而不是路径段

    首稿曾写成 `/settings/projects/diverged/{key}` 并给 `Path` 加默认值，
    意图是"不带参数也能用"。**实测否决**：

        AssertionError: Path parameters cannot have default values

    路径参数是**寻址的一部分**，它不可能"可选"—— 没有默认值可谈。
    而这里 `key` 确实是可选的（默认 `batch.limits`，最常用的键），
    选项只有两个：路径写成 `/settings/projects/diverged?key=...`，
    或把 `key` 做成路径段且**必填**。

    取前者。理由：`diverged` 是**一种视图**（"谁和全局不一样"），
    `key` 是它的**筛选条件** —— 与 `GET /projects?...` 的 `status`
    筛选同级，而路径段表达的是资源身份。把筛选条件写进路径会让
    "默认键"这个事实无处安放（只能靠前端硬编码一段路径）。

    ⚠️ 这也是本文件**唯一**带查询参数的端点，因此
    `PLANNING_READ_RESPONSES` 里的 422（`VALIDATION_FAILED`）
    在这条路径上是**活跃**的（`key` 给非字符串会被 FastAPI 拦下）。
    其余端点的 422 只覆盖请求体与路径参数的校验。

    ## `A list` 而不是 `PaginatedEnvelope`

    项目数上限是个位到几十（§1.2：约 50 人运营、项目数远小于此），
    且这个视图的用途是"看有没有分叉"而不是"翻页浏览全部"。
    分页会引入"翻到第二页才发现还有分叉"的风险 ——
    与 J3 的目的一致的是**一次看全**。已登记：若项目数破百，改为分页。
    """
    del user
    rows = settings_service.list_diverged_projects(repo, key)
    return single_response(
        [
            DivergedProjectBody(
                project_id=row.project_id,
                value=dict(row.value),
                source=row.source,
                global_value=row.global_value,
                revision_no=row.revision_no,
            )
            for row in rows
        ]
    )


# ---------------------------------------------------------------------------
# 全局配置：写（组织级权限）
# ---------------------------------------------------------------------------


@router.put(
    "/settings/global/{key}",
    response_model=DataEnvelope[SettingValueBody],
    responses=PLANNING_WRITE_RESPONSES,
    dependencies=GLOBAL_SETTING_WRITE,
    summary="变更全局配置（同事务级联刷新所有未覆盖副本）",
)
def change_global_setting(
    user: CurrentUserDep,
    repo: PlanningRepoDep,
    body: ChangeGlobalSettingBody,
    key: Annotated[str, Path(description="配置键，如 batch.limits")],
) -> dict[str, Any]:
    """**唯一全局写入口**（§3.6.2 的级联刷新在服务层同一事务内完成）。

    ## 响应为什么回带 `source` / `globalValue` / `diverged`

    全局写之后，**这个键在全局视角上** `diverged` 恒为 `false`
    （全局值不可能与自己不同）。但这三个字段仍要回带，理由是
    **形状一致**：前端处理 `PUT /settings/global/{key}` 与
    `PUT /settings/projects/{projectId}/{key}` 的响应应当用同一段代码。
    若前者少三个字段，前端会写两个分支，而第二个分支迟早只剩一个在用。

    ## `Request` **不注入**：本端点不需要 `If-Match`

    见模块 docstring 第五节：配置是追加式，没有 `lock_version`，
    并发语义由 advisory lock 保证。**不给它一个假的前置条件**。

    `request_id` 用 `get_request_id()`（不是 `Request` 对象）——
    它从中介件注入的 contextvar 取值，不需要端点声明 `Request` 参数。
    """
    setting = settings_service.change_global_setting(
        repo,
        key=key,
        value=None if body.value is None else dict(body.value),
        reason=body.reason,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    return single_response(_setting_body(setting))


# ---------------------------------------------------------------------------
# 项目级配置：写（项目权限）
# ---------------------------------------------------------------------------


@router.put(
    "/settings/projects/{projectId}/{key}",
    response_model=DataEnvelope[SettingValueBody],
    responses=PLANNING_WRITE_RESPONSES,
    summary="变更项目级配置（写 MANUAL 覆盖，不跟随全局变更）",
)
def change_project_setting(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: PlanningRepoDep,
    body: ChangeProjectSettingBody,
    key: Annotated[str, Path(description="配置键，如 batch.limits")],
) -> dict[str, Any]:
    """**唯一项目级写入口**。写 `origin='MANUAL'`（覆盖，J4：覆盖是稳定的）。

    ## 权限是 `EDIT` 而不是组织级动作

    改"我这个项目的批次上限"是**项目级参数调整**，与改方案内容同级
    （`EDIT` 的注释"编辑 Brief、方案、内容"里的"方案"包含它的生产参数）。
    若收进组织级权限，运营（项目负责人）就改不了自己项目的上限，
    而"每个项目可以不同"正是 OP-60 拍板要支持的能力。

    ## 响应里 `diverged` 是这次调用的**核心返回值**

    它是 J3 判据的观察点：`source='MANUAL'`、`diverged` 反映"是否与全局不同"、
    `globalValue` 让人看见差在哪。**前端据此渲染
    「本项目已覆盖 · 当前 5 条 · 全局 10 条」**（§3.6.4 的原文）。

    注意 `diverged=false` 是一个**合法且常见**的结果：
    运营覆盖成了与全局**相同**的值。那时"已覆盖"与"有差异"是两件事，
    不能用 `source == 'MANUAL'` 去推断"有差异"（`schema.py` 已论证）。

    ## 为什么 `access` 的 `project_id` 与路径参数不必比对

    `ProjectAccessDep` 从路径的 `{projectId}` 解析（`load_project_access`
    自己声明了 `Path(alias="projectId")`），因此 `access.project_id`
    **就是**路径里的那个值。额外声明一份 `project_id` 参数会拿到同一个值，
    于是只有两种结局：`del` 掉它，或让读者以为两个值可能不同
    （`baseline/api.py::save_information_draft` 的同款说明）。
    """
    access.require(ProjectAction.EDIT)
    setting = settings_service.change_project_setting(
        repo,
        key=key,
        project_id=access.project_id,
        value=None if body.value is None else dict(body.value),
        reason=body.reason,
        actor_id=user.id,
        request_id=get_request_id(),
    )
    return single_response(_setting_body(setting))


__all__ = [
    "GLOBAL_SETTING_WRITE",
    "PLANNING_READ_RESPONSES",
    "PLANNING_WRITE_RESPONSES",
    "router",
]
