"""游标分页（协议 §1）。

**为什么不用偏移量（offset/limit）**：
偏移量分页在并发写入下会漏项或重复 —— `OFFSET n` 基于"前 n 条没变"这个前提，
而项目列表、内容列表都是持续写入的表。必须用 keyset 分页：
"从排序键为 X 的那条之后继续读"。

**游标绑定排序与筛选（`CursorSpec`）**：
把"排序字段 + 方向 + 筛选条件"的指纹写进游标。前端若把"按点赞数排序"的游标
拿去"按时间排序"的查询里复用，绑定校验会拒绝（400），而不是静默返回错位的下一页。
静默忽略会让前端以为在翻页、实际每次都拿第一页，形成难以察觉的数据重复。

**校验和只防手滑，不做安全边界**：
用 SHA-256 而非 HMAC 签名，理由是：
  1. 协议要求 API 双实例（ADR-11 C8），不能用进程内随机密钥签名 ——
     A 实例签发的游标在 B 实例会验签失败；
  2. 用全局密钥要引入配置项与轮换流程，成本高于收益；
  3. 关键判断：**游标载荷不承载权限**，它只是"从哪条之后继续读"的位置。
     伪造它最多让攻击者从任意位置开始读，改变不了他能读到什么。
     权限过滤必须在每个查询里重新执行（不能把权限编码进游标以省一次查询 ——
     那样权限在游标签发后被撤销时，旧游标仍会放行，造成越权）。

**稳定排序必须追加 id**：
两条记录排序值相同时，必须按 id 有序，否则游标锚点不唯一，翻页会漏项或重复。
`paginate_items` 强制把 id 作为最后一级排序键。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Final, Generic, TypeVar

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from rednote.platform.errors import ApiError, ErrorCode, Meta

# 协议 §1：默认 limit=20，最大 100。
DEFAULT_LIMIT: Final[int] = 20
MAX_LIMIT: Final[int] = 100

# 游标格式版本。格式变更时递增，旧游标会被明确拒绝而不是被误解析。
_CURSOR_VERSION: Final[int] = 1

# 指纹字段名在载荷里的缩写（v=version, f=fingerprint, k=sort values, i=item id）。
# 用缩写是让载荷保持短 —— 游标会出现在 URL 里，长度影响可读性与代理日志体积。
_FIELD_VERSION: Final[str] = "v"
_FIELD_FINGERPRINT: Final[str] = "f"
_FIELD_SORT_VALUES: Final[str] = "k"
_FIELD_ITEM_ID: Final[str] = "i"

T = TypeVar("T")


@dataclass(frozen=True)
class CursorSpec:
    """游标的"绑定条件"：排序字段、方向、筛选条件。

    `fingerprint` 是这三者的规范化摘要。签发与校验时必须一致，
    不一致即视为"另一个查询的游标"并拒绝。
    """

    sort_fields: tuple[str, ...]
    direction: str = "desc"
    filters: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.sort_fields:
            # 空排序字段无法构造稳定游标 —— 早失败好过生成一个不可用的游标。
            raise ValueError("sort_fields 不能为空")
        normalized = self.direction.lower()
        if normalized not in {"asc", "desc"}:
            raise ValueError(f"不支持的排序方向: {self.direction}")
        object.__setattr__(self, "direction", normalized)

    @property
    def fingerprint(self) -> str:
        """绑定条件的稳定摘要。

        用 `sort_keys=True` 让 filters 的键顺序不影响结果；
        用 `default=str` 容忍非 JSON 原生的筛选值（如 date/UUID）。
        """
        payload = {
            "s": list(self.sort_fields),
            "d": self.direction,
            "f": self.filters,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class CursorPosition:
    """游标承载的位置：最后一条的排序值 + id。"""

    sort_values: tuple[Any, ...]
    item_id: str


def _b64encode(raw: bytes) -> str:
    """base64url 且去掉 `=` 填充。

    去填充是让游标能安全出现在 URL 查询串里（`=` 在 query 中需转义）。
    """
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(encoded: str) -> bytes:
    padded = encoded + "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def encode_cursor(spec: CursorSpec, position: CursorPosition) -> str:
    """签发游标：`base64url(payload).sha256hex`。

    分开存载荷与校验和（用 `.` 分隔）而不是把校验和塞进 JSON，
    是为了让"校验和覆盖的范围"一眼可见 —— 它就是 `.` 左边那串字节。
    """
    payload = {
        _FIELD_VERSION: _CURSOR_VERSION,
        _FIELD_FINGERPRINT: spec.fingerprint,
        _FIELD_SORT_VALUES: list(position.sort_values),
        _FIELD_ITEM_ID: position.item_id,
    }
    # separators 去掉空格，保证同内容字节一致（游标可复现、便于断言）。
    raw = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    return f"{_b64encode(raw)}.{hashlib.sha256(raw).hexdigest()}"


def decode_cursor(cursor: str, spec: CursorSpec) -> CursorPosition:
    """解析并校验游标。任何问题都转成 400 MALFORMED_REQUEST。

    统一走 MALFORMED_REQUEST 而不是各自的错误码：对客户端而言
    "游标有问题"的处理方式只有一种 —— 丢弃游标、回到第一页。
    细分错误码只会让前端多写无意义的分支。
    `details.reason` 给出机器可判的原因，供排障与断言使用。
    """

    def _reject(reason: str) -> ApiError:
        return ApiError(
            ErrorCode.MALFORMED_REQUEST,
            details={"reason": reason, "field": "cursor"},
        )

    if not cursor:
        raise _reject("cursor_malformed")

    encoded, separator, checksum = cursor.rpartition(".")
    if not separator or not encoded or not checksum:
        # 没有 `.` 分隔符 -> 不是本系统签发的格式。
        raise _reject("cursor_malformed")

    try:
        raw = _b64decode(encoded)
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise _reject("cursor_not_base64") from exc

    # 先验校验和，再解析内容：篡改检测应该在"读懂内容"之前发生，
    # 避免把攻击者构造的畸形 JSON 喂给解析器。
    expected = hashlib.sha256(raw).hexdigest()
    if checksum != expected:
        raise _reject("cursor_checksum_mismatch")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _reject("cursor_malformed") from exc

    if not isinstance(payload, dict):
        raise _reject("cursor_malformed")

    version = payload.get(_FIELD_VERSION)
    if version != _CURSOR_VERSION:
        # 版本不符明确失效，不与新格式静默混用。
        raise _reject("cursor_version_unsupported")

    if payload.get(_FIELD_FINGERPRINT) != spec.fingerprint:
        # 排序/方向/筛选任一不符都在此被拦下。
        raise _reject("cursor_binding_mismatch")

    sort_values = payload.get(_FIELD_SORT_VALUES)
    item_id = payload.get(_FIELD_ITEM_ID)
    if not isinstance(sort_values, list) or not isinstance(item_id, str) or not item_id:
        raise _reject("cursor_malformed")

    return CursorPosition(sort_values=tuple(sort_values), item_id=item_id)


class PageParams:
    """分页请求参数。

    **为什么不是 FastAPI 依赖类的 pydantic 模型**：
    模块启用了 `from __future__ import annotations`，若把 `Query(...)` 写在类
    `__init__` 的注解上，注解会被字符串化，pydantic 在类内命名空间解析不到 `Query`
    （本机实测报 AttributeError: __pydantic_core_schema__）。
    因此改用模块级函数 `page_params()` 提供依赖，本类只做纯数据容器。
    """

    __slots__ = ("limit", "cursor")

    def __init__(self, limit: int, cursor: str | None = None) -> None:
        self.limit = limit
        self.cursor = cursor


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> PageParams:
    """分页依赖。

    边界校验交给 `Query(ge=1, le=100)` 而不是函数体里手写 ——
    框架层校验保证**所有**用这个依赖的端点都被覆盖，而手写校验会漏。
    """
    return PageParams(limit=limit, cursor=cursor)


# 依赖别名。直接用函数当依赖会与"参数名/类型"语义混淆，
# 用 Annotated 别名让端点签名保持 `params: PageParamsDep` 这一种写法。
PageParamsDep = Annotated[PageParams, Depends(page_params)]


class PageInfo(BaseModel):
    """协议 §1 的 page 块。"""

    model_config = ConfigDict(populate_by_name=True)

    next_cursor: str | None = Field(
        default=None, alias="nextCursor", serialization_alias="nextCursor"
    )
    has_more: bool = Field(alias="hasMore", serialization_alias="hasMore")


class PaginatedEnvelope(BaseModel, Generic[T]):
    """协议 §1：列表响应 `{"data": [...], "page": {...}, "meta": {...}}`。

    **为什么必须单独定义，不能复用 `DataEnvelope[list[T]]`：**

    端点的 `response_model` 不只是文档 —— 它是 FastAPI 的**输出过滤器**。
    声明模型里没有的键会被静默剥掉：`list_response()` 明明拼了 `page`，
    返回体里却只剩 `data` 与 `meta`，前端翻页时永远拿不到 `nextCursor`。

    症状极难定位：HTTP 200、`data` 正常、服务端无任何异常日志，
    前端表现为"只能看第一页"。WP10 实测踩到（`payload["page"]` 直接 KeyError）。

    结论：分页响应必须有**带 `page` 的**输出模型，并配一条契约测试守着
    （见 `tests/test_projects_wp10.py::test_brand_list_paginates`）。
    """

    model_config = ConfigDict(populate_by_name=True)

    data: list[T]
    page: PageInfo
    meta: Meta


def list_response(items: Sequence[Any], page: PageInfo) -> dict[str, Any]:
    """构造协议 §1 的列表响应：`{"data": [...], "page": {...}, "meta": {...}}`。

    返回 dict 而不是 `PaginatedEnvelope` 实例：handler 的返回类型统一是
    `dict[str, Any]`，由 `response_model` 负责序列化与过滤。
    **但 `response_model` 必须声明为 `PaginatedEnvelope[T]`** ——
    否则 `page` 会被剥掉（见 `PaginatedEnvelope` 的说明）。
    """
    from rednote.platform.errors import build_meta

    return {
        "data": list(items),
        "page": page.model_dump(by_alias=True, mode="json"),
        "meta": build_meta(),
    }


def single_response(data: Any) -> dict[str, Any]:
    """构造协议 §1 的单资源响应：`{"data": {...}, "meta": {...}}`。"""
    from rednote.platform.errors import build_meta

    return {"data": data, "meta": build_meta()}


class _Comparable(Generic[T]):
    """把"排序元组 + id"包装成可比较对象。

    为什么不直接比较元组：排序值可能是混合类型（int 与 str 混排会抛 TypeError），
    而且方向（asc/desc）要统一作用到所有字段上。这里显式做两件事：
    按方向决定是否取反、id 永远作为最后一级打破平局。
    """

    __slots__ = ("key", "item_id", "descending")

    def __init__(self, key: tuple[Any, ...], item_id: str, descending: bool) -> None:
        self.key = key
        self.item_id = item_id
        self.descending = descending

    def _sortable(self) -> tuple[Any, ...]:
        return self.key + (self.item_id,)

    def __lt__(self, other: "_Comparable[T]") -> bool:  # pragma: no cover - 见下方比较函数
        return self._sortable() < other._sortable()


def paginate_items(
    items: Sequence[T],
    params: PageParams,
    spec: CursorSpec,
    sort_key: Callable[[T], tuple[Any, ...]],
    id_of: Callable[[T], str],
) -> tuple[list[T], PageInfo]:
    """对**已取得的结果集**做游标分页。

    **为什么在内存里分页而不是生成 SQL**：
    骨架期各模块还没有真实查询层，而分页的正确性（不重不漏、绑定校验、
    平局处理）与数据来源无关。把算法先落地并测透，等 WP10 起接上真实查询时
    只需把 `sort_key`/`id_of` 换成数据库列、把过滤下推到 SQL。
    届时本函数仍复用于"小结果集精确扫描"场景（ADR-15 提到的回退路径）。

    要求调用方按同一 spec 的排序把 items 传进来；本函数只负责
    定位游标锚点、切出当前页、判断 hasMore。
    """
    descending = spec.direction == "desc"

    # 平局时 id 参与排序，方向与主排序一致（desc 下 id 也 desc）。
    # 这样"锚点唯一"这个前提才成立。
    def sort_tuple(item: T) -> tuple[Any, ...]:
        return sort_key(item) + (id_of(item),)

    ordered = sorted(items, key=sort_tuple, reverse=descending)

    start_index = 0
    if params.cursor:
        position = decode_cursor(params.cursor, spec)
        anchor = position.sort_values + (position.item_id,)
        # 找到锚点之后的第一个位置。用"严格大于/小于"而不是 index()+1：
        # 锚点记录可能已被删除，此时 index() 会抛 ValueError，
        # 而二分查找能优雅地落到"第一个比锚点更靠后的记录"。
        start_index = _bisect_after(ordered, anchor, sort_tuple, descending)

    window = ordered[start_index : start_index + params.limit]
    has_more = start_index + params.limit < len(ordered)

    next_cursor: str | None = None
    if has_more and window:
        last = window[-1]
        next_cursor = encode_cursor(
            spec,
            CursorPosition(sort_values=sort_key(last), item_id=id_of(last)),
        )

    return window, PageInfo(next_cursor=next_cursor, has_more=has_more)


def _bisect_after(
    ordered: Sequence[T],
    anchor: tuple[Any, ...],
    sort_tuple: Callable[[T], tuple[Any, ...]],
    descending: bool,
) -> int:
    """二分找出"锚点之后"的第一个下标。

    这样即使锚点那条记录已被删除，也能返回正确位置（而不是抛异常或返回 0）。
    返回 0 会让客户端陷入"永远拿第一页"的死循环 —— 那是更坏的结果。
    """
    low, high = 0, len(ordered)
    while low < high:
        mid = (low + high) // 2
        current = sort_tuple(ordered[mid])
        # desc 方向下，序列是递减的，"更靠后"意味着 current < anchor。
        if (current < anchor) if descending else (current > anchor):
            high = mid
        else:
            low = mid + 1
    return low


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "CursorPosition",
    "CursorSpec",
    "PageInfo",
    "PageParams",
    "PageParamsDep",
    "PaginatedEnvelope",
    "decode_cursor",
    "encode_cursor",
    "list_response",
    "page_params",
    "paginate_items",
    "single_response",
]