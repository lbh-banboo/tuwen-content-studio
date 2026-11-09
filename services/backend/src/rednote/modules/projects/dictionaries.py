"""品牌 / 产品字典接口（WP10）。

**先说清楚一个缺口**：协议 §3 的端点表里**没有**字典端点 ——
只有 `POST /projects` 的入参引用 `brandId` / `productId`，
以及 `GET /knowledge/assets` 的筛选参数引用它们。
但 WP10 的工作包定义明确包含"品牌产品字典"（研发计划 04 §4.2），
需求 §2.2 的实施默认也写了"知识库关联的品牌／产品字典全员可见"。

也就是说：**字典必须有维护入口，但协议没定它的形状**。
本文件按既有约定补齐，并把缺口登记为待定项：

| 决定 | 依据 |
|---|---|
| 路径 `/brands`、`/products`（顶层，非项目子资源） | 字典是共享主数据，不隶属任何项目 |
| `GET` 全员可见 | 需求 §2.2 实施默认"全员可见" |
| `POST` / `PATCH` 归管理者 | 需求 §2.2 无对应行 → 按"组织级主数据"归管理者（`MANAGE_DICTIONARY`） |
| 用 If-Match 做并发保护 | 协议 §1.1"对可变资源修改要求其当前 ETag"，字典是可改名/可归档的可变资源 |
| **不提供删除** | 字典被项目引用后物理删除会产生悬挂外键；归档（`status=ARCHIVED`）已经表达了"不再启用"，且保留历史引用 |

**为什么没有 `GET /brands/{id}`**：列表响应已带全部字段与 `lockVersion`，
前端拿到列表即可发起 PATCH。协议对项目端点也没有列单项 GET 之外的子资源详情。
少一个端点少一份契约负担 —— 等真有需要再加。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.projects.api_responses import (
    READ_RESPONSES,
    WRITE_RESPONSES,
)
from rednote.modules.projects.dependencies import ProjectRepoDep, require_org_action
from rednote.modules.projects.models import DICT_ACTIVE, DICT_ARCHIVED
from rednote.modules.projects.permissions import OrgAction
from rednote.platform.concurrency import build_etag, require_if_match, version_conflict
from rednote.platform.errors import ApiError, DataEnvelope, ErrorCode, build_meta
from rednote.platform.pagination import (
    CursorSpec,
    PageParamsDep,
    PaginatedEnvelope,
    list_response,
    paginate_items,
)

router = APIRouter(tags=["dictionaries"])

DictStatus = Literal["ACTIVE", "ARCHIVED"]

# 字典扫描上限。
#
# **这是一个已知边界，不是随手取的数**：字典列表用内存分页
# （`paginate_items`），需要先把匹配行读出来。若行数超过本上限，
# 超出的部分将永远不可见 —— 这与"静默返回错位的一页"一样危险。
#
# 取 2000 是因为：品牌 = 合作品牌数、产品 = 各品牌 SKU 数，
# 试点期规模在数十到数百量级，2000 有充分余量。
# **超过 2000 时本端点会漏项**，届时应改为键集分页下推 SQL
# （参考 `ProjectRepository.list_projects_filtered`）。已登记为待定项。
DICT_SCAN_LIMIT = 2000


def _not_found(kind: str, resource_id: uuid.UUID) -> ApiError:
    return ApiError(
        ErrorCode.RESOURCE_NOT_FOUND,
        f"{kind}不存在。",
        details={f"{kind.lower()}Id": str(resource_id)},
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class BrandBody(BaseModel):
    """品牌条目。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str
    name: str
    status: str
    lock_version: int = Field(alias="lockVersion", serialization_alias="lockVersion")


class BrandCreateBody(BaseModel):
    """新建品牌。

    **不做重名校验**：数据结构文档 02 §3 明确品牌名"可重名但需提示"，
    因此不加唯一约束。真正的重复提示属于前端交互（输入时提示已有同名），
    服务端不拦 —— 拦了会让"同一集团下两个事业部的同名子品牌"无法录入。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(min_length=1, max_length=150)


class BrandUpdateBody(BaseModel):
    """更新品牌（改名 / 归档）。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=150)
    status: DictStatus | None = None


class ProductBody(BaseModel):
    """产品条目。"""

    # protected_namespaces=()：Pydantic v2 默认把 `model_` 前缀视为
    # 受保护命名空间（与 model_dump/model_config 等冲突）。
    # 本类字段 `model_code` 是业务名（产品型号），与 Pydantic 无关，
    # 因此显式关闭该保护 —— 保留业务术语优先于迁就框架的内部约定。
    model_config = ConfigDict(
        populate_by_name=True, extra="forbid", protected_namespaces=()
    )

    id: str
    brand_id: str = Field(alias="brandId", serialization_alias="brandId")
    name: str
    model_code: str | None = Field(
        default=None, alias="modelCode", serialization_alias="modelCode"
    )
    status: str
    lock_version: int = Field(alias="lockVersion", serialization_alias="lockVersion")


class ProductCreateBody(BaseModel):
    """新建产品。"""

    model_config = ConfigDict(
        populate_by_name=True, extra="forbid", protected_namespaces=()
    )

    brand_id: uuid.UUID = Field(alias="brandId", serialization_alias="brandId")
    name: str = Field(min_length=1, max_length=200)
    model_code: str | None = Field(default=None, max_length=100, alias="modelCode")


class ProductUpdateBody(BaseModel):
    """更新产品。

    **刻意不含 `brandId`**：换品牌等于换产品归属。
    已有项目通过 `projects.product_id` 引用它，改 brandId 会让
    "项目所属品牌"与"产品所属品牌"不一致（需求 §4.1 要求产品与品牌对应）。
    真要换归属应当新建产品并让新项目引用它 —— 历史项目保留原产品。
    """

    model_config = ConfigDict(
        populate_by_name=True, extra="forbid", protected_namespaces=()
    )

    name: str | None = Field(default=None, min_length=1, max_length=200)
    model_code: str | None = Field(default=None, max_length=100, alias="modelCode")
    status: DictStatus | None = None


# ---------------------------------------------------------------------------
# 转换
# ---------------------------------------------------------------------------


def _to_brand(brand: Any) -> dict[str, Any]:
    return BrandBody(
        id=str(brand.id),
        name=brand.name,
        status=brand.status,
        lockVersion=brand.lock_version,
    ).model_dump(by_alias=True, mode="json")


def _to_product(product: Any) -> dict[str, Any]:
    return ProductBody(
        id=str(product.id),
        brandId=str(product.brand_id),
        name=product.name,
        modelCode=product.model_code,
        status=product.status,
        lockVersion=product.lock_version,
    ).model_dump(by_alias=True, mode="json")


def _brand_spec(
    *, status_filter: str | None, keyword: str | None
) -> CursorSpec:
    """品牌列表的游标绑定条件。"""
    return CursorSpec(
        sort_fields=("name", "id"),
        direction="asc",
        filters={"status": status_filter, "keyword": keyword or None},
    )


def _product_spec(
    *, brand_id: uuid.UUID | None, status_filter: str | None, keyword: str | None
) -> CursorSpec:
    """产品列表的游标绑定条件。"""
    return CursorSpec(
        sort_fields=("name", "id"),
        direction="asc",
        filters={
            "brandId": str(brand_id) if brand_id else None,
            "status": status_filter,
            "keyword": keyword or None,
        },
    )


# ---------------------------------------------------------------------------
# 品牌
# ---------------------------------------------------------------------------


@router.get(
    "/brands",
    summary="品牌字典（全员可见）",
    response_model=PaginatedEnvelope[BrandBody],
    responses=READ_RESPONSES,
)
def list_brands(
    user: CurrentUserDep,
    repo: ProjectRepoDep,
    params: PageParamsDep,
    status_filter: Annotated[DictStatus | None, Query(alias="status")] = None,
    keyword: Annotated[str | None, Query(max_length=100)] = None,
) -> dict[str, Any]:
    """列出品牌字典。

    需求 §2.2 实施默认："知识库关联的品牌／产品字典全员可见"——
    因此这里**不做项目级隔离**，任何已登录成员都能读。
    写操作才受限（见下方 POST/PATCH）。

    默认返回全部状态（含已归档）：归档品牌的历史项目仍在引用它，
    前端需要能显示名字。需要只看启用的传 `status=ACTIVE`。
    """
    rows = repo.list_brands(limit=DICT_SCAN_LIMIT, status=status_filter, keyword=keyword)
    spec = _brand_spec(status_filter=status_filter, keyword=keyword)
    window, page = paginate_items(
        rows, params, spec, lambda brand: (brand.name,), lambda brand: str(brand.id)
    )
    return list_response([_to_brand(brand) for brand in window], page)


@router.post(
    "/brands",
    summary="新建品牌（管理者）",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[BrandBody],
    responses=WRITE_RESPONSES,
    dependencies=[Depends(require_org_action(OrgAction.MANAGE_DICTIONARY))],
)
def create_brand(
    payload: BrandCreateBody, repo: ProjectRepoDep, response: Response
) -> dict[str, Any]:
    """新建品牌。权限归管理者（字典是组织级主数据）。"""
    brand = repo.create_brand(name=payload.name)
    response.headers["ETag"] = build_etag(brand.id, brand.lock_version)
    return {"data": _to_brand(brand), "meta": build_meta()}


@router.patch(
    "/brands/{brandId}",
    summary="更新品牌（改名 / 归档，If-Match）",
    response_model=DataEnvelope[BrandBody],
    responses=WRITE_RESPONSES,
    dependencies=[Depends(require_org_action(OrgAction.MANAGE_DICTIONARY))],
)
def update_brand(
    request: Request,
    response: Response,
    payload: BrandUpdateBody,
    repo: ProjectRepoDep,
    brand_id: Annotated[uuid.UUID, Path(alias="brandId")],
) -> dict[str, Any]:
    """更新品牌。

    **归档保护**：若仍有项目引用该品牌，拒绝归档（409 `INVALID_TRANSITION`）。
    需求 §2.2 与数据结构文档 02 都要求"产品与品牌对应"，
    归档一个在被引用的品牌会让项目侧的品牌下拉出现"已归档但仍在使用"的
    矛盾状态 —— 该矛盾应该由管理者显式处理（先改项目），而不是靠系统无视。
    """
    brand = repo.get_brand(brand_id)
    if brand is None:
        raise _not_found("品牌", brand_id)

    require_if_match(
        request,
        resource_id=brand.id,
        current_version=brand.lock_version,
        resource_label="品牌",
    )

    values: dict[str, Any] = {}
    if "name" in payload.model_fields_set and payload.name is not None:
        values["name"] = payload.name

    if "status" in payload.model_fields_set and payload.status is not None:
        if payload.status == DICT_ARCHIVED and brand.status != DICT_ARCHIVED:
            referencing = repo.count_projects_for_brand(brand.id)
            if referencing > 0:
                raise ApiError(
                    ErrorCode.INVALID_TRANSITION,
                    f"该品牌仍被 {referencing} 个项目引用，不能归档。",
                    details={
                        "brandId": str(brand.id),
                        "referencingProjectCount": referencing,
                    },
                )
        values["status"] = payload.status

    if values:
        updated = repo.update_brand_versioned(
            brand, expected_version=brand.lock_version, values=values
        )
        if not updated:
            raise version_conflict(
                resource_id=brand.id,
                current_version=brand.lock_version,
                reason="concurrent_update",
                message="品牌已被更新，请重新读取后再提交。",
            )

    response.headers["ETag"] = build_etag(brand.id, brand.lock_version)
    return {"data": _to_brand(brand), "meta": build_meta()}


# ---------------------------------------------------------------------------
# 产品
# ---------------------------------------------------------------------------


@router.get(
    "/products",
    summary="产品字典（全员可见）",
    response_model=PaginatedEnvelope[ProductBody],
    responses=READ_RESPONSES,
)
def list_products(
    user: CurrentUserDep,
    repo: ProjectRepoDep,
    params: PageParamsDep,
    brand_id: Annotated[uuid.UUID | None, Query(alias="brandId")] = None,
    status_filter: Annotated[DictStatus | None, Query(alias="status")] = None,
    keyword: Annotated[str | None, Query(max_length=100)] = None,
) -> dict[str, Any]:
    """列出产品字典。可按 `brandId` 收敛（建项目时先选品牌再选产品）。"""
    rows = repo.list_products(
        limit=DICT_SCAN_LIMIT,
        brand_id=brand_id,
        status=status_filter,
        keyword=keyword,
    )
    spec = _product_spec(brand_id=brand_id, status_filter=status_filter, keyword=keyword)
    window, page = paginate_items(
        rows,
        params,
        spec,
        lambda product: (product.name,),
        lambda product: str(product.id),
    )
    return list_response([_to_product(product) for product in window], page)


@router.post(
    "/products",
    summary="新建产品（管理者）",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[ProductBody],
    responses=WRITE_RESPONSES,
    dependencies=[Depends(require_org_action(OrgAction.MANAGE_DICTIONARY))],
)
def create_product(
    payload: ProductCreateBody, repo: ProjectRepoDep, response: Response
) -> dict[str, Any]:
    """新建产品。

    校验品牌存在且未归档 —— 否则会造出"挂在归档品牌下的新产品"，
    而这样的产品永远无法被项目引用（建项目时会因品牌已归档被拒）。
    """
    brand = repo.get_brand(payload.brand_id)
    if brand is None:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "品牌不存在。",
            details={"fieldErrors": [{"field": "brandId", "message": "品牌不存在。"}]},
        )
    if brand.status != DICT_ACTIVE:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "该品牌已归档，不能在其下新建产品。",
            details={
                "fieldErrors": [
                    {"field": "brandId", "message": "该品牌已归档，不能在其下新建产品。"}
                ]
            },
        )

    product = repo.create_product(
        brand_id=payload.brand_id, name=payload.name, model_code=payload.model_code
    )
    response.headers["ETag"] = build_etag(product.id, product.lock_version)
    return {"data": _to_product(product), "meta": build_meta()}


@router.patch(
    "/products/{productId}",
    summary="更新产品（改名 / 改型号 / 归档，If-Match）",
    response_model=DataEnvelope[ProductBody],
    responses=WRITE_RESPONSES,
    dependencies=[Depends(require_org_action(OrgAction.MANAGE_DICTIONARY))],
)
def update_product(
    request: Request,
    response: Response,
    payload: ProductUpdateBody,
    repo: ProjectRepoDep,
    product_id: Annotated[uuid.UUID, Path(alias="productId")],
) -> dict[str, Any]:
    """更新产品。归档保护同品牌。

    **型号改动的唯一性**由 `uq_products_brand_model_code`（部分唯一索引）保证：
    同一品牌下型号不可重复，但可空型号不受约束。
    撞了会抛 `IntegrityError`，由全局异常处理转 500 ——
    这里不做预检，因为预检同样有并发窗口，而"两个管理员同时录入同一型号"
    本来就是罕见且应当报错的场景。
    """
    product = repo.get_product(product_id)
    if product is None:
        raise _not_found("产品", product_id)

    require_if_match(
        request,
        resource_id=product.id,
        current_version=product.lock_version,
        resource_label="产品",
    )

    values: dict[str, Any] = {}
    if "name" in payload.model_fields_set and payload.name is not None:
        values["name"] = payload.name
    if "model_code" in payload.model_fields_set:
        # model_code 允许显式置空（清空型号），因此这里不判 None。
        values["model_code"] = payload.model_code

    if "status" in payload.model_fields_set and payload.status is not None:
        if payload.status == DICT_ARCHIVED and product.status != DICT_ARCHIVED:
            referencing = repo.count_projects_for_product(product.id)
            if referencing > 0:
                raise ApiError(
                    ErrorCode.INVALID_TRANSITION,
                    f"该产品仍被 {referencing} 个项目引用，不能归档。",
                    details={
                        "productId": str(product.id),
                        "referencingProjectCount": referencing,
                    },
                )
        values["status"] = payload.status

    if values:
        updated = repo.update_product_versioned(
            product, expected_version=product.lock_version, values=values
        )
        if not updated:
            raise version_conflict(
                resource_id=product.id,
                current_version=product.lock_version,
                reason="concurrent_update",
                message="产品已被更新，请重新读取后再提交。",
            )

    response.headers["ETag"] = build_etag(product.id, product.lock_version)
    return {"data": _to_product(product), "meta": build_meta()}


__all__ = ["DICT_SCAN_LIMIT", "router"]
