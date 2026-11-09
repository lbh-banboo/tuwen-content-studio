"""项目接口（WP09 建立权限演示，WP10 补齐完整 CRUD 与交接）。

**WP10 交付的端点（对照协议 §3 第 95~101 行）**：

| 方法与路径 | 协议要求 | 本文件实现 |
|---|---|---|
| `GET /projects` | phase, ownerId, keyword, cursor, limit | ✅ 键集分页下推 SQL + 三项筛选 |
| `POST /projects` | 201 Project，管理者 | ✅ 管理者限定，含成员关系写入 |
| `GET /projects/{id}` | Project + ETag | ✅ 补 ETag 响应头 |
| `PATCH /projects/{id}` | 可编辑业务字段、If-Match | ✅ 不能改 phase / ownerId |
| `POST /projects/{id}/assignments` | 201 Assignment，管理者 | ✅ 含待办转交 |
| `POST /projects/{id}/members` | 新协作者 | ✅ 幂等 |
| `DELETE /projects/{id}/members/{userId}` | 204，不能移除当前负责人 | ✅ 负责人被拒 409 |

**权限边界如何保证不被绕过**：
- 项目内的动作（详情、编辑、成员）：由 `ProjectAccessDep` 在依赖层完成
  "存在性 + 成员 + 权限"三件事，端点函数体里**没有权限判断代码**。
- 组织级动作（建项目、改负责人）：用 `require_org_action(...)` 作为路由依赖，
  漏写的端点会因为缺少依赖而**在测试里暴露**（管理员与运营行为无差别）。
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from rednote.modules.identity.dependencies import (
    CurrentUserDep,
    RepositoryDep as IdentityRepoDep,
)
from rednote.modules.identity.models import ROLE_ADMIN, STATUS_ACTIVE
from rednote.modules.projects.api_responses import (
    READ_RESPONSES,
    WRITE_RESPONSES,
)
from rednote.modules.projects.dependencies import (
    ProjectAccessDep,
    ProjectRepoDep,
    WorkItemRepoDep,
    require_org_action,
)
from rednote.modules.projects.models import (
    DICT_ACTIVE,
    MEMBER_COLLABORATOR,
    MEMBER_OWNER,
)
from rednote.modules.projects.permissions import (
    OrgAction,
    ProjectAction,
    ProjectPermissions,
)
from rednote.modules.projects.service import create_project as create_project_service
from rednote.modules.projects.service import is_owner_transfer_conflict
from rednote.platform.concurrency import build_etag, version_conflict
from rednote.platform.errors import (
    ApiError,
    DataEnvelope,
    ErrorCode,
    build_meta,
)
from rednote.platform.pagination import (
    CursorPosition,
    CursorSpec,
    PageInfo,
    PageParamsDep,
    PaginatedEnvelope,
    decode_cursor,
    encode_cursor,
    list_response,
)

router = APIRouter(tags=["projects"])

ProjectPhase = Literal["PREPARATION", "PRODUCTION", "DATA_COLLECTION", "CLOSED"]
ExternalPolicy = Literal["LOCAL_ONLY", "APPROVED_EXTERNAL"]

# 错误响应声明统一在 api_responses.py（与字典端点共用，避免两份拷贝漂移）。
# 每个写端点显式声明 responses，防止 FastAPI 自动注入的 HTTPValidationError 混进对外契约。


def _validation_error(field: str, message: str) -> ApiError:
    """构造 422 VALIDATION_FAILED，details 按协议 §1.2 的 fieldErrors 结构。

    集中构造是为了让所有字段级错误形状一致 ——
    前端只需要写一套 fieldErrors 渲染逻辑。
    """
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        message,
        details={"fieldErrors": [{"field": field, "message": message}]},
    )


def _parse_cursor_after(cursor: str, spec: CursorSpec) -> tuple[date, uuid.UUID] | None:
    """解析游标锚点。返回 None 表示不带游标（第一页）。

    游标载荷里的排序值以字符串形式存（JSON 只能存 JSON 类型），
    这里转回 `date` 与 `UUID` 供 SQL 比较。
    解析失败统一转 400 —— 对客户端而言"游标坏了"只有一种处理：丢掉重来。
    """
    if not cursor:
        return None

    position = decode_cursor(cursor, spec)
    values = list(position.sort_values)
    if len(values) != 2:
        raise ApiError(
            ErrorCode.MALFORMED_REQUEST,
            "游标载荷与当前排序不匹配。",
            details={"field": "cursor", "reason": "cursor_payload_arity"},
        )
    try:
        return date.fromisoformat(str(values[0])), uuid.UUID(str(values[1]))
    except (TypeError, ValueError) as exc:
        raise ApiError(
            ErrorCode.MALFORMED_REQUEST,
            "游标载荷无法解析。",
            details={"field": "cursor", "reason": "cursor_payload_invalid"},
        ) from exc


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class ProjectPermissionsBody(BaseModel):
    """当前用户对该项目的权限能力。

    **为什么把权限回给前端**：前端据此决定"哪些按钮可点"。
    但这**只是体验优化**，不是权限边界 ——
    每个写操作在服务端仍会重新校验（研发计划 01 §10.2：
    不能只在菜单隐藏管理入口）。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    is_admin: bool = Field(alias="isAdmin", serialization_alias="isAdmin")
    member_role: str | None = Field(
        default=None, alias="memberRole", serialization_alias="memberRole"
    )
    can_view: bool = Field(alias="canView", serialization_alias="canView")
    can_edit: bool = Field(alias="canEdit", serialization_alias="canEdit")
    can_review: bool = Field(alias="canReview", serialization_alias="canReview")
    can_deliver: bool = Field(alias="canDeliver", serialization_alias="canDeliver")
    can_report: bool = Field(alias="canReport", serialization_alias="canReport")
    can_view_cost: bool = Field(alias="canViewCost", serialization_alias="canViewCost")
    can_close: bool = Field(alias="canClose", serialization_alias="canClose")
    can_manage_members: bool = Field(
        alias="canManageMembers", serialization_alias="canManageMembers"
    )

    @classmethod
    def from_permissions(cls, permissions: ProjectPermissions) -> "ProjectPermissionsBody":
        """从权限集合生成响应体。

        集中转换而不是在端点里逐个拼字段：漏一个字段不会报错，
        只会让前端少一个按钮 —— 那是最难查的一类 bug。
        """
        return cls(
            isAdmin=permissions.is_admin,
            memberRole=permissions.membership.role,
            canView=permissions.allows(ProjectAction.VIEW),
            canEdit=permissions.allows(ProjectAction.EDIT),
            canReview=permissions.allows(ProjectAction.REVIEW),
            canDeliver=permissions.allows(ProjectAction.DELIVER),
            canReport=permissions.allows(ProjectAction.REPORT),
            canViewCost=permissions.allows(ProjectAction.VIEW_COST),
            canClose=permissions.allows(ProjectAction.CLOSE),
            canManageMembers=permissions.allows(ProjectAction.MANAGE_MEMBERS),
        )


class ProjectSummary(BaseModel):
    """项目摘要（列表项）。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str
    project_code: str = Field(alias="projectCode", serialization_alias="projectCode")
    name: str
    phase: str
    due_date: str = Field(alias="dueDate", serialization_alias="dueDate")
    target_delivery_count: int = Field(
        alias="targetDeliveryCount", serialization_alias="targetDeliveryCount"
    )
    owner_id: str = Field(alias="ownerId", serialization_alias="ownerId")
    brand_id: str = Field(alias="brandId", serialization_alias="brandId")
    product_id: str = Field(alias="productId", serialization_alias="productId")


class ProjectDetailBody(BaseModel):
    """项目详情。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str
    project_code: str = Field(alias="projectCode", serialization_alias="projectCode")
    name: str
    phase: str
    due_date: str = Field(alias="dueDate", serialization_alias="dueDate")
    timezone: str
    target_delivery_count: int = Field(
        alias="targetDeliveryCount", serialization_alias="targetDeliveryCount"
    )
    target_publication_count: int | None = Field(
        default=None,
        alias="targetPublicationCount",
        serialization_alias="targetPublicationCount",
    )
    external_policy: str = Field(
        alias="externalPolicy", serialization_alias="externalPolicy"
    )
    owner_id: str = Field(alias="ownerId", serialization_alias="ownerId")
    brand_id: str = Field(alias="brandId", serialization_alias="brandId")
    product_id: str = Field(alias="productId", serialization_alias="productId")
    description: str | None = None
    lock_version: int = Field(alias="lockVersion", serialization_alias="lockVersion")
    permissions: ProjectPermissionsBody


class ProjectCreateBody(BaseModel):
    """创建项目请求体（字段与协议 §3 的 `POST /projects` 一一对应）。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    brand_id: uuid.UUID = Field(alias="brandId", serialization_alias="brandId")
    product_id: uuid.UUID = Field(alias="productId", serialization_alias="productId")
    owner_id: uuid.UUID = Field(alias="ownerId", serialization_alias="ownerId")
    collaborator_ids: list[uuid.UUID] = Field(
        default_factory=list,
        alias="collaboratorIds",
        serialization_alias="collaboratorIds",
    )
    target_delivery_count: int = Field(
        gt=0, alias="targetDeliveryCount", serialization_alias="targetDeliveryCount"
    )
    due_date: date = Field(alias="dueDate", serialization_alias="dueDate")
    target_publication_count: int | None = Field(
        default=None,
        ge=0,
        alias="targetPublicationCount",
        serialization_alias="targetPublicationCount",
    )
    external_policy: ExternalPolicy = Field(
        default="LOCAL_ONLY",
        alias="externalPolicy",
        serialization_alias="externalPolicy",
    )
    description: str | None = Field(default=None, max_length=2000)


class ProjectUpdateBody(BaseModel):
    """更新项目请求体。

    **刻意不包含 `phase` 与 `ownerId`**，并用 `extra="forbid"` 让传入它们时
    直接 422。这不是"忘了加"，而是协议 §3 的明文要求：
    > PATCH `/projects/{id}` | 可编辑业务字段、If-Match | 不能直接修改 phase、ownerId、费用累计
    阶段由结项/重开端点变更，负责人由 assignments 端点变更 ——
    各走专门的端点，才能在服务端做对应的权限与状态校验。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    brand_id: uuid.UUID | None = Field(default=None, alias="brandId")
    product_id: uuid.UUID | None = Field(default=None, alias="productId")
    due_date: date | None = Field(default=None, alias="dueDate")
    target_delivery_count: int | None = Field(
        default=None, gt=0, alias="targetDeliveryCount"
    )
    target_publication_count: int | None = Field(
        default=None, ge=0, alias="targetPublicationCount"
    )
    external_policy: ExternalPolicy | None = Field(default=None, alias="externalPolicy")
    description: str | None = Field(default=None, max_length=2000)


class AssignmentCreateBody(BaseModel):
    """负责人交接请求体（协议 §3 + 需求 §2.3）。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    new_owner_id: uuid.UUID = Field(alias="newOwnerId", serialization_alias="newOwnerId")
    keep_old_collaborator: bool = Field(
        default=False,
        alias="keepOldCollaborator",
        serialization_alias="keepOldCollaborator",
    )
    # 需求 §2.3 第 1 条："选择新负责人并保存变更原因" —— 原因是必填项。
    reason: str = Field(min_length=1, max_length=500)


class AssignmentResultBody(BaseModel):
    """交接结果。

    **为什么返回 `transferredWorkItemCount`**：
    WP10 的验收标准是"并发交接仍一名负责人；**待办和历史正确**"。
    把转交条数放进响应，等于让验收可被外部观察 ——
    否则"待办转交"只能靠看日志相信。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str
    project_id: str = Field(alias="projectId", serialization_alias="projectId")
    previous_owner_id: str = Field(
        alias="previousOwnerId", serialization_alias="previousOwnerId"
    )
    new_owner_id: str = Field(alias="newOwnerId", serialization_alias="newOwnerId")
    keep_old_collaborator: bool = Field(
        alias="keepOldCollaborator", serialization_alias="keepOldCollaborator"
    )
    reason: str
    transferred_work_item_count: int = Field(
        alias="transferredWorkItemCount",
        serialization_alias="transferredWorkItemCount",
    )
    created_at: str = Field(alias="createdAt", serialization_alias="createdAt")
    lock_version: int = Field(alias="lockVersion", serialization_alias="lockVersion")


class MemberCreateBody(BaseModel):
    """添加协作者请求体（协议 §3：`userId`）。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    user_id: uuid.UUID = Field(alias="userId", serialization_alias="userId")


class MemberBody(BaseModel):
    """成员条目。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    user_id: str = Field(alias="userId", serialization_alias="userId")
    display_name: str = Field(alias="displayName", serialization_alias="displayName")
    member_role: str = Field(alias="memberRole", serialization_alias="memberRole")
    joined_at: str = Field(alias="joinedAt", serialization_alias="joinedAt")
    is_owner: bool = Field(alias="isOwner", serialization_alias="isOwner")


# ---------------------------------------------------------------------------
# 转换辅助
# ---------------------------------------------------------------------------


def _to_summary(project: Any) -> dict[str, Any]:
    """把 ORM 项目对象转成摘要 dict。"""
    return ProjectSummary(
        id=str(project.id),
        projectCode=project.project_code,
        name=project.name,
        phase=project.phase,
        dueDate=project.due_date.isoformat(),
        targetDeliveryCount=project.target_delivery_count,
        ownerId=str(project.owner_id),
        brandId=str(project.brand_id),
        productId=str(project.product_id),
    ).model_dump(by_alias=True, mode="json")


def _to_detail(access: Any) -> dict[str, Any]:
    """把"已授权的项目访问上下文"转成详情响应体。"""
    project = access.project
    return ProjectDetailBody(
        id=str(project.id),
        projectCode=project.project_code,
        name=project.name,
        phase=project.phase,
        dueDate=project.due_date.isoformat(),
        timezone=project.timezone,
        targetDeliveryCount=project.target_delivery_count,
        targetPublicationCount=project.target_publication_count,
        externalPolicy=project.external_policy,
        ownerId=str(project.owner_id),
        brandId=str(project.brand_id),
        productId=str(project.product_id),
        description=project.description,
        lockVersion=project.lock_version,
        permissions=ProjectPermissionsBody.from_permissions(access.permissions),
    ).model_dump(by_alias=True, mode="json")


def _require_active_user(identity_repo: Any, user_id: uuid.UUID, field: str) -> Any:
    """校验目标用户存在且启用。返回用户对象。

    为什么必须校验：把项目交给一个已停用的账号，等于让项目没有负责人
    （需求 §2.3"每个项目必须有且只有一名主负责人"）。
    数据库的 FK 只保证"用户存在"，不保证"用户可用"——那必须在这里查。
    """
    user = identity_repo.get_user(user_id)
    if user is None:
        raise _validation_error(field, "指定的用户不存在。")
    if user.status != STATUS_ACTIVE:
        raise _validation_error(field, "指定的用户已停用，不能作为项目负责人或成员。")
    return user


def _ensure_dictionary_choice(
    repo: ProjectRepoDep, *, brand_id: uuid.UUID, product_id: uuid.UUID
) -> None:
    """校验品牌/产品组合可用。

    三条规则：
      1. 品牌存在；
      2. 产品存在且**属于该品牌**（否则会造出"华为品牌的苹果手机"这种数据）；
      3. 两者都未归档（归档项不允许被新项目引用，但已有项目继续正常）。
    """
    brand = repo.get_brand(brand_id)
    if brand is None:
        raise _validation_error("brandId", "品牌不存在。")
    if brand.status != DICT_ACTIVE:
        raise _validation_error("brandId", "该品牌已归档，不能被新项目引用。")

    product = repo.get_product(product_id)
    if product is None:
        raise _validation_error("productId", "产品不存在。")
    if product.brand_id != brand_id:
        # 需求 §4.1："产品与品牌对应"（文档 02 §3 的约束说明）。
        raise _validation_error("productId", "所选产品不属于所选品牌。")
    if product.status != DICT_ACTIVE:
        raise _validation_error("productId", "该产品已归档，不能被新项目引用。")


def _apply_update_fields(payload: ProjectUpdateBody) -> dict[str, Any]:
    """把请求体里**显式给出**的字段转成列名 → 值的映射。

    用 `model_fields_set` 而不是"值不是 None"来判断"给没给"：
    字段类型允许 None 的场景下，`{"description": null}` 是"清空描述"，
    而不是"没给"。两者必须区分，否则前端无法把描述改回空。
    """
    mapping = {
        "name": "name",
        "brand_id": "brand_id",
        "product_id": "product_id",
        "due_date": "due_date",
        "target_delivery_count": "target_delivery_count",
        "target_publication_count": "target_publication_count",
        "external_policy": "external_policy",
        "description": "description",
    }
    return {
        column: getattr(payload, field_name)
        for field_name, column in mapping.items()
        if field_name in payload.model_fields_set
    }


# ---------------------------------------------------------------------------
# 项目：列表
# ---------------------------------------------------------------------------


@router.get(
    "/projects",
    summary="我可访问的项目列表（键集分页 + 筛选）",
    response_model=PaginatedEnvelope[ProjectSummary],
    responses=READ_RESPONSES,
)
def list_projects(
    user: CurrentUserDep,
    repo: ProjectRepoDep,
    params: PageParamsDep,
    phase: Annotated[ProjectPhase | None, Query()] = None,
    owner_id: Annotated[uuid.UUID | None, Query(alias="ownerId")] = None,
    keyword: Annotated[str | None, Query(max_length=100)] = None,
) -> dict[str, Any]:
    """列出当前用户可访问的项目。

    **权限隔离的直接影响点**（WP09 验收项"运营限项目"的延续）：
      - 管理者：不限范围 → 看到全部
      - 运营：`restrict_to_user_id` 下推到 SQL → 只看自己是活跃成员的项目

    隔离条件写在 SQL 的 JOIN 条件里，不是"查出来再过滤"——
    后者一旦有人改动取数逻辑就可能漏掉过滤，把别的项目泄露出去。

    **排序固定为 `dueDate ASC, id ASC`**（不对外暴露 sort 参数）：
    排序键必须是**稳定且唯一**的，否则游标锚点不唯一、翻页会漏项。
    当前业务只有"最近截止的排前面"这一个诉求，等真出现多排序需求时
    再加参数 —— 那时 `CursorSpec` 的指纹机制会自动保证
    "换了排序的游标不能复用"。
    """
    spec = CursorSpec(
        sort_fields=("dueDate", "id"),
        direction="asc",
        filters={
            # 把可见范围写进指纹：运营与管理者的游标不能互相复用，
            # 否则管理者把游标给运营，运营就能拿到越界的一页。
            "scope": "all" if user.is_admin else str(user.id),
            "phase": phase,
            "ownerId": str(owner_id) if owner_id else None,
            "keyword": keyword or None,
        },
    )

    after = _parse_cursor_after(params.cursor or "", spec)

    rows = repo.list_projects_filtered(
        restrict_to_user_id=None if user.is_admin else user.id,
        limit=params.limit,
        phase=phase,
        owner_id=owner_id,
        keyword=keyword,
        after=after,
    )

    # 仓储多取一条用于判断 hasMore（避免再发 COUNT，且在并发写入下更准）。
    has_more = len(rows) > params.limit
    window = rows[: params.limit]

    next_cursor: str | None = None
    if has_more and window:
        last = window[-1]
        next_cursor = encode_cursor(
            spec,
            CursorPosition(
                sort_values=(last.due_date.isoformat(), str(last.id)),
                item_id=str(last.id),
            ),
        )

    page = PageInfo(next_cursor=next_cursor, has_more=has_more)
    return list_response([_to_summary(project) for project in window], page)


# ---------------------------------------------------------------------------
# 项目：创建
# ---------------------------------------------------------------------------


@router.post(
    "/projects",
    summary="创建项目（管理者）",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[ProjectDetailBody],
    responses=WRITE_RESPONSES,
    dependencies=[Depends(require_org_action(OrgAction.CREATE_PROJECT))],
)
def create_project_endpoint(
    payload: ProjectCreateBody,
    user: CurrentUserDep,
    repo: ProjectRepoDep,
    identity_repo: IdentityRepoDep,
    response: Response,
) -> dict[str, Any]:
    """创建项目。

    权限：需求 §2.2 第 1 行"创建项目、分配及变更负责人"仅管理者 ——
    由路由依赖 `require_org_action(CREATE_PROJECT)` 强制。

    **事务性**：项目行、负责人成员行、协作者成员行在同一请求的同一事务里写入
    （`session_scope` 在请求结束时统一 commit）。
    若中途任何一步失败，整个事务回滚 —— 不会留下"项目建了但没有负责人"的
    半成品（那会违反需求 §2.3"每个项目必须有且只有一名主负责人"）。

    **编号冲突重试**：项目编号是随机生成的，撞车概率极低但非零。
    重试在 `service.create_project` 里用 SAVEPOINT 实现，
    这样一次撞车不会污染整个事务。
    """
    _require_active_user(identity_repo, payload.owner_id, "ownerId")
    for collaborator_id in payload.collaborator_ids:
        _require_active_user(identity_repo, collaborator_id, "collaboratorIds")

    _ensure_dictionary_choice(
        repo, brand_id=payload.brand_id, product_id=payload.product_id
    )

    try:
        project = create_project_service(
            repo,
            name=payload.name,
            brand_id=payload.brand_id,
            product_id=payload.product_id,
            owner_id=payload.owner_id,
            target_delivery_count=payload.target_delivery_count,
            due_date=payload.due_date,
            collaborator_ids=payload.collaborator_ids,
            target_publication_count=payload.target_publication_count,
            external_policy=payload.external_policy,
            description=payload.description,
        )
    except IntegrityError as exc:
        # 走到这里说明不是编号冲突（编号冲突已在 service 内重试掉），
        # 而是成员关系等约束被撞 —— 属于客户端输入问题。
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "成员关系冲突：同一用户不能在同一项目里同时拥有两条活跃成员记录。",
            details={
                "fieldErrors": [
                    {
                        "field": "collaboratorIds",
                        "message": "请检查协作者列表中是否有重复或与负责人重复。",
                    }
                ]
            },
        ) from exc

    # 新建项目用管理员视角组装响应（创建者必然是管理者）。
    from rednote.modules.projects.permissions import (
        MembershipInfo,
        compute_project_permissions,
    )

    access_like = _AccessView(
        project=project,
        permissions=compute_project_permissions(
            is_admin=user.role == ROLE_ADMIN,
            membership=MembershipInfo(
                project_id=project.id, user_id=user.id, role=None
            ),
            project_phase=project.phase,
        ),
    )
    response.headers["ETag"] = build_etag(project.id, project.lock_version)
    return {"data": _to_detail(access_like), "meta": build_meta()}


class _AccessView:
    """最小授权视图：让 `_to_detail` 复用于"刚创建的项目"。

    为什么不直接返回 `ProjectAccess`：创建时还没有"成员关系校验"这一步
    （项目是刚建出来的，成员行刚写入），硬造一个 ProjectAccess
    反而会让读者以为这里做过权限检查。
    """

    __slots__ = ("project", "permissions")

    def __init__(self, project: Any, permissions: ProjectPermissions) -> None:
        self.project = project
        self.permissions = permissions


# ---------------------------------------------------------------------------
# 项目：详情
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}",
    summary="项目详情（按对象权限校验）",
    response_model=DataEnvelope[ProjectDetailBody],
    responses=READ_RESPONSES,
)
def get_project(access: ProjectAccessDep, response: Response) -> dict[str, Any]:
    """项目详情。

    权限校验完全由 `ProjectAccessDep` 依赖完成：
    项目不存在 → 404；非成员 → 404（防枚举）；成员 → 放行。
    端点函数体内**没有任何权限判断代码** —— 这是"结构上无法绕过"的体现。

    **响应带 ETag 头**（协议 §3："Project + ETag"）：
    客户端保存它，PATCH / assignments / members 时通过 `If-Match` 回传。
    同时响应体里也带 `lockVersion`，便于前端在 JS 里直接取用
    （读响应头需要 `Access-Control-Expose-Headers`，多一个部署配置项）。
    """
    response.headers["ETag"] = build_etag(access.project.id, access.project.lock_version)
    return {"data": _to_detail(access), "meta": build_meta()}


# ---------------------------------------------------------------------------
# 项目：编辑
# ---------------------------------------------------------------------------


@router.patch(
    "/projects/{projectId}",
    summary="更新项目（If-Match 乐观并发）",
    response_model=DataEnvelope[ProjectDetailBody],
    responses=WRITE_RESPONSES,
)
def update_project(
    request: Request,
    response: Response,
    payload: ProjectUpdateBody,
    access: ProjectAccessDep,
    repo: ProjectRepoDep,
) -> dict[str, Any]:
    """更新项目的可编辑业务字段。

    权限：项目内 `EDIT`（负责人与协作者均可，需求 §2.2 第 3 行）。

    **不能改的字段**：`phase`（走结项/重开端点）、`ownerId`（走 assignments）。
    请求体用 `extra="forbid"`，传了会得到 422 —— 这是刻意的。

    **并发保护分两层**：
      1. `access.require_if_match(request)` —— 前置条件校验，缺失 428、不符 412；
      2. `repo.update_project_versioned(..., expected_version=...)` ——
         `UPDATE ... WHERE lock_version = ?` 才是原子的那一层。
    """
    access.require(ProjectAction.EDIT)
    access.require_if_match(request)

    values = _apply_update_fields(payload)
    if not values:
        # 空 body 是合法请求（无变更），直接回当前状态 —— 不递增版本号。
        response.headers["ETag"] = build_etag(
            access.project.id, access.project.lock_version
        )
        return {"data": _to_detail(access), "meta": build_meta()}

    # 品牌/产品任一变更时，都要重新校验组合（可能是只改了其中一个）。
    if "brand_id" in values or "product_id" in values:
        _ensure_dictionary_choice(
            repo,
            brand_id=values.get("brand_id", access.project.brand_id),
            product_id=values.get("product_id", access.project.product_id),
        )

    updated = repo.update_project_versioned(
        access.project,
        expected_version=access.project.lock_version,
        values=values,
    )
    if not updated:
        # 前置校验通过但 WHERE 未命中 —— 说明在读与写之间有人抢先提交。
        raise version_conflict(
            resource_id=access.project.id,
            current_version=access.project.lock_version,
            reason="concurrent_update",
            message="项目已被更新，请重新读取最新版本后再提交。",
        )

    response.headers["ETag"] = build_etag(
        access.project.id, access.project.lock_version
    )
    return {"data": _to_detail(access), "meta": build_meta()}


# ---------------------------------------------------------------------------
# 项目：负责人交接
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{projectId}/assignments",
    summary="变更项目负责人（管理者，含待办转交）",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[AssignmentResultBody],
    responses=WRITE_RESPONSES,
    dependencies=[Depends(require_org_action(OrgAction.CREATE_PROJECT))],
)
def create_assignment(
    request: Request,
    payload: AssignmentCreateBody,
    access: ProjectAccessDep,
    repo: ProjectRepoDep,
    work_repo: WorkItemRepoDep,
    identity_repo: IdentityRepoDep,
) -> dict[str, Any]:
    """变更项目负责人（需求 §2.3）。

    需求 §2.3 的五条，逐条落点：

    | # | 需求原文 | 落点 |
    |---|---|---|
    | 1 | 选择新负责人并保存变更原因 | `reason` 必填，写入 `project_assignments` |
    | 2 | 未认领的待办与原负责人待办转交新负责人；其他协作者的保留 | `work_repo.transfer_open_items` |
    | 3 | 已运行任务继续，创建人和费用归属不重写 | 本端点不触碰内容/费用表 |
    | 4 | 默认移除原负责人项目操作权；可勾选保留为协作者 | `keep_old_collaborator` |
    | 5 | 历史审核/评论/交付记录保留原操作人 | 成员行只标记 `removed_at`，不物理删除 |

    **权限**：需求 §2.2 第 1 行"创建项目、分配及变更负责人"= 管理者。
    与创建项目同一条权限（`CREATE_PROJECT`），因为它们在同一行需求里。

    **并发**：`If-Match` 前置校验 + `UPDATE ... WHERE lock_version = ?`；
    此外 `uq_project_members_active_owner` 从数据库层保证
    "任何时刻至多一名活跃负责人"——两个并发交接中后到的那个必然失败。
    """
    access.require_if_match(request)

    project = access.project
    previous_owner_id = project.owner_id
    # **先把需要的标量取出来，再进 try**。
    # 一旦 flush 失败，SQLAlchemy 会把 session 里所有实例标记为 expire
    # （保证内存状态不残留半写数据）。此时再去读 `project.lock_version`
    # 会触发一次惰性加载，而事务已被那次失败语句标记为 aborted ——
    # 结果抛 PendingRollbackError，本该是 412 的请求变成 500。
    # 本机实测踩到过：交接给项目内已有协作者时正是这个表现。
    project_id = project.id
    expected_version = project.lock_version

    if payload.new_owner_id == previous_owner_id:
        raise _validation_error("newOwnerId", "新负责人与当前负责人相同，无需变更。")

    _require_active_user(identity_repo, payload.new_owner_id, "newOwnerId")

    try:
        ok = repo.transfer_owner(
            project,
            expected_version=expected_version,
            new_owner_id=payload.new_owner_id,
            keep_old_collaborator=payload.keep_old_collaborator,
        )
    except IntegrityError as exc:
        # 只把"成员约束"相撞解释成并发交接。
        # 其它 IntegrityError（外键、CHECK 等）是程序缺陷，必须原样抛出 ——
        # 把它们伪装成 412 会让真正的 bug 看起来像正常的并发冲突。
        if not is_owner_transfer_conflict(exc):
            raise
        # 冲突信息里只用上面取好的标量，不碰 ORM 对象（session 已 expire）。
        raise version_conflict(
            resource_id=project_id,
            current_version=expected_version,
            reason="concurrent_assignment",
            message="项目负责人刚被其他操作变更，请重新读取后再提交。",
        ) from exc

    if not ok:
        raise version_conflict(
            resource_id=project_id,
            current_version=expected_version,
            reason="concurrent_update",
            message="项目已被更新，请重新读取最新版本后再提交。",
        )

    # 待办转交（需求 §2.3 第 2 条）。
    transferred = work_repo.transfer_open_items(
        project.id,
        from_assignee_id=previous_owner_id,
        to_assignee_id=payload.new_owner_id,
    )

    assignment = repo.record_assignment(
        project_id=project.id,
        previous_owner_id=previous_owner_id,
        new_owner_id=payload.new_owner_id,
        keep_old_collaborator=payload.keep_old_collaborator,
        reason=payload.reason,
        transferred_work_item_count=transferred,
        created_by=access.permissions.membership.user_id,
    )

    body = AssignmentResultBody(
        id=str(assignment.id),
        projectId=str(project.id),
        previousOwnerId=str(previous_owner_id),
        newOwnerId=str(payload.new_owner_id),
        keepOldCollaborator=payload.keep_old_collaborator,
        reason=payload.reason,
        transferredWorkItemCount=transferred,
        createdAt=assignment.created_at.isoformat(),
        lockVersion=project.lock_version,
    )
    return {"data": body.model_dump(by_alias=True, mode="json"), "meta": build_meta()}


# ---------------------------------------------------------------------------
# 项目：协作者成员
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{projectId}/members",
    summary="添加协作者",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[MemberBody],
    responses=WRITE_RESPONSES,
)
def add_project_member(
    payload: MemberCreateBody,
    access: ProjectAccessDep,
    repo: ProjectRepoDep,
    identity_repo: IdentityRepoDep,
    response: Response,
) -> dict[str, Any]:
    """把用户加为项目协作者。

    权限：`MANAGE_MEMBERS`（当前矩阵归管理者 —— 需求 §2.2
    "创建项目、分配及变更负责人"仅管理者，成员管理同源）。

    **幂等语义**：若该用户已是活跃成员，返回 200 与现有成员信息，不重复插入。
    这样前端"重试同一个请求"不会因为一次网络抖动就报错 ——
    协议对写操作要求 Idempotency-Key 的场景是"会产生计费/任务"的命令，
    成员添加不属于那类，用自然幂等（唯一约束保证）更轻。

    **负责人不能通过本端点变更**（协议 §3："负责人经 assignments 修改"）：
    传当前负责人 ID 会得到 422。
    """
    access.require(ProjectAction.MANAGE_MEMBERS)

    project = access.project
    if payload.user_id == project.owner_id:
        raise _validation_error(
            "userId", "该用户已是项目负责人；负责人变更请调用 assignments 端点。"
        )

    _require_active_user(identity_repo, payload.user_id, "userId")

    existing = repo.get_membership(project.id, payload.user_id)
    if existing.is_member:
        member = repo.get_member_row(project.id, payload.user_id)
        response.status_code = status.HTTP_200_OK
        return {
            "data": _to_member(member, identity_repo),
            "meta": build_meta(),
        }

    try:
        # **必须包在 SAVEPOINT 里**：唯一约束冲突会让 PG 把整个事务标记为
        # aborted，而下面 catch 之后还要执行一条"回查现有成员"的 SELECT ——
        # 不回滚到 SAVEPOINT 的话那条 SELECT 必然报
        # `current transaction is aborted`，幂等分支直接退化成 500。
        # （`tests/test_projects_wp10.py::test_add_member_duplicate_keeps_transaction_usable`
        # 就是守这一点的。）
        with repo.savepoint():
            member = repo.add_member(project.id, payload.user_id, MEMBER_COLLABORATOR)
    except IntegrityError as exc:
        # 并发下两个请求同时添加同一用户 —— 后到的撞唯一约束，按幂等处理。
        member = repo.get_member_row(project.id, payload.user_id)
        if member is None:
            # 回查也没有，说明冲突不是"重复成员"引起的（例如外键等），
            # 不该伪装成业务冲突。
            raise
        response.status_code = status.HTTP_200_OK

    return {"data": _to_member(member, identity_repo), "meta": build_meta()}


@router.delete(
    "/projects/{projectId}/members/{userId}",
    summary="移除协作者（不能移除负责人）",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=WRITE_RESPONSES,
)
def remove_project_member(
    access: ProjectAccessDep,
    repo: ProjectRepoDep,
    user_id: Annotated[uuid.UUID, Path(alias="userId")],
) -> Response:
    """移除项目协作者。

    权限：`MANAGE_MEMBERS`。

    **移除的是"成员关系"，不是用户**：标记 `removed_at` 而非删行 ——
    需求 §2.3 第 5 条要求"历史审核、评论、交付记录保留原操作人"。

    **不能移除当前负责人**（协议 §3 明文）：负责人必须先通过
    assignments 换人，否则项目会变成"没有负责人"的状态，
    违反需求 §2.3"每个项目必须有且只有一名主负责人"。

    **幂等**：成员不存在或已被移除，仍返回 204 ——
    对客户端而言"让他不在项目里"的目标已达成。
    """
    access.require(ProjectAction.MANAGE_MEMBERS)

    project = access.project
    if user_id == project.owner_id:
        raise ApiError(
            ErrorCode.INVALID_TRANSITION,
            "不能移除当前负责人；请先通过负责人变更端点指定新负责人。",
            details={"projectId": str(project.id), "userId": str(user_id)},
        )

    repo.remove_member(project.id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _to_member(member: Any, identity_repo: Any) -> dict[str, Any]:
    """把成员行转成响应体（补上显示名）。"""
    user = identity_repo.get_user(member.user_id)
    return MemberBody(
        userId=str(member.user_id),
        displayName=user.display_name if user is not None else "",
        memberRole=member.member_role,
        joinedAt=member.joined_at.isoformat(),
        isOwner=member.member_role == MEMBER_OWNER,
    ).model_dump(by_alias=True, mode="json")


__all__ = ["router"]
