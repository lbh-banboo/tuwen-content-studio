"""研究端点（协议 03 §5 的第 6、7 行，**只有 2 个**）。

| 方法 | 路径 | 成功码 | 依据 |
|---|---|---|---|
| GET | `/projects/{projectId}/research-prompts` | 200 | §5 第 6 行 |
| GET | `/projects/{projectId}/research-files` | 200 | §5 第 7 行 |

**两个都是 GET，本包无写端点。** 上传走 WP12、解析走 WP17 ——
协议里没有研究专用的写端点，因此本包契约**净增 2 个 operation**
（30 paths / 35 operations → 32 paths / 37 operations）。

验收「复制、上传、解析、步骤2确认完整」的落点分布：

| 验收词 | 谁提供 |
|---|---|
| 复制 | 本包 `prompts[].text`（**服务端给死文本**，不靠前端拼） |
| 上传 | WP12（`purpose=RESEARCH` 已存在）—— 本包只保证列得出来 |
| 解析 | WP17（`POST /files/{fileId}/parse-jobs`）—— 本包只显示状态 |
| 步骤 2 确认完整 | 本包 `gaps[]` + `summary.blockingCount`（**"还差几条"的判据**） |

---

## 一、权限：`ProjectAccessDep` + `require(VIEW)`

两个端点的判定顺序：

  1. `ProjectAccessDep`（依赖层）：项目不存在 → 404；
     用户不是成员且非管理者 → **404**（防项目存在性枚举，协议 §1.2）；
  2. `access.require(ProjectAction.VIEW)`：是成员但角色不允许查看 → 403。

**为什么是 `VIEW` 而不是 `EDIT`**：研究阶段是**读 Brief 推断搜什么**，
它不改任何 Brief 数据（本包零写）。用 `EDIT` 会把"研究者"排除在外 ——
而需求 §2.2 的权限矩阵里，协作者本来就能看项目。真正的写动作
（上传、发起解析）由 WP12 / WP17 端点各自判它们该判的动作。

## 二、`briefVersionId` 是三态参数（不传 / 传真 / 传假）

| 输入 | 行为 | 理由 |
|---|---|---|
| 不传 | 用**当前生效版本** | 前端的默认路径 —— 运营打开页面时不该被迫先知道版本 id |
| 传了且属于本项目 | 用那一版 | 支持"对比上一版的关键词"这类用法 |
| 传了但不存在 / 不属于本项目 | **404** | 它是**被寻址的资源**，不是请求体里的字段值 —— 判据与 `PUT /brief-draft` 的 `baseVersionId`（422）不同（那里的字段值不对是校验失败） |

**不存在"项目没有 Brief 版本"这个错误码**：那是**正常的前置状态**
（项目刚建、还没上传 Brief），返回 200 + 空结果 + 一条引导提示词。
报 404 会让前端把"还没做 Brief"显示成"出错了"。

## 三、`status` 参数是**复选**（逗号分隔），不是单选

协议 §5 原文只写 `status`，没说是单选还是多选。本实现按**多选**：

  · 运营的真实诉求是"我要看哪些还没好的"（`NONE,QUEUED,RUNNING`）
    或"哪些能用了"（`SUCCEEDED,PARTIAL`）—— 单选要发两次请求，
    而两次请求之间的列表可能已经变了；
  · 取值域 = WP17 的 `PARSE_STATES` **加上本包的 `NONE`**。
    `NONE` 不是 `parse_runs` 的取值（"从未解析"在一张只追加的表里
    无法存在），但它必须在筛选里可用 —— 否则"还没解析的文件"
    永远筛不出来。

**非法取值静默忽略还是报错**：本实现**报 422**。理由：一个拼错的
`status=succeded` 若被静默忽略，前端会拿到**全部文件**并显示成
"筛选后的结果" —— 那比报错危险得多（同 WP17 对未知字段路径的判据）。

## 四、`GET /research-files` 的分页与"全量口径字段"

分页用 `platform.pagination` 的游标机制（在**内存**里对窗口做 keyset 分页，
排序键 `(createdAt DESC, id DESC)`）。

**但 `quota` 与 `summary` 是全量口径，不随翻页变化**：
"配额用了多少"引用的是整个项目的账面，若它随 `limit` 变化就是错的。
这一点由 `service.list_research_files` 先取全量窗口再交给分页保证
（见那里的说明）。

**`limit` 上限仍然是 100**（`platform.pagination.MAX_LIMIT`）——
本包**不为了测试放宽它**。ISS-73 的教训说的是"调用处要显式传大 limit"
（那指的是仓库层的扫描上限 `RESEARCH_FILE_SCAN_LIMIT`），
不是"端点的契约上限要变大"。端点的 `le=100` 是**对客户端的承诺**，
放宽它会让"一页 100 条"这个契约悄悄变成别的数字。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query

from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.parsing.types import PARSE_STATES
from rednote.modules.projects.dependencies import ProjectAccessDep
from rednote.modules.projects.permissions import ProjectAction
from rednote.modules.research import service
from rednote.modules.research.dependencies import ResearchRepoDep
from rednote.modules.research.repository import PARSE_STATE_NONE
from rednote.modules.research.schema import (
    ResearchFileView,
    ResearchFilesEnvelope,
    ResearchPromptsView,
)
from rednote.platform.api_responses import READ_RESPONSES
from rednote.platform.errors import ApiError, DataEnvelope, ErrorCode, build_meta
from rednote.platform.pagination import (
    CursorSpec,
    PageParamsDep,
    paginate_items,
)

router = APIRouter(tags=["research"])

# 两个端点都是只读：可能返回 400 / 401 / 403 / 404 / 422 / 500。
# **必须显式声明**（`platform/api_responses.py` 的教训）：FastAPI 只检查
# `responses` 里有没有 422，没有就自动挂上形状不同的 `HTTPValidationError`。
RESEARCH_RESPONSES = READ_RESPONSES

#: `status` 参数的完整取值域 = WP17 的解析状态 + 本包的 `NONE`。
#:
#: **由常量推导而不是手抄**：抄一份就会在 WP17 新增状态时漂移，
#: 而漂移方向恰好是"新状态筛不出来"（静默少一个可筛值）。
#: `NONE` 放在**首位**：它是"还没做"这个最需要被筛出来的状态。
STATUS_FILTER_VALUES: tuple[str, ...] = (PARSE_STATE_NONE, *PARSE_STATES)

STATUS_FILTER_SET: frozenset[str] = frozenset(STATUS_FILTER_VALUES)


@router.get(
    "/projects/{projectId}/research-prompts",
    response_model=DataEnvelope[ResearchPromptsView],
    responses=RESEARCH_RESPONSES,
    summary="研究关键词、可复制提示词与覆盖缺口",
)
def get_research_prompts(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: ResearchRepoDep,
    brief_version_id: uuid.UUID | None = Query(
        default=None, alias="briefVersionId"
    ),
) -> dict[str, Any]:
    """读研究关键词与提示词（协议 §5 第 6 行）。

    > 协议原文：`GET /projects/{id}/research-prompts` | `briefVersionId` |
    > **推荐关键词、可复制提示词与建议返回格式**

    三块都在，另加两块本包认为必需：

    | 块 | 协议原文对应 | 为什么必需 |
    |---|---|---|
    | `keywords` | "推荐关键词" | — |
    | `prompts` | "可复制提示词" + "建议返回格式" | `formatHint` 是"建议返回格式"的落点 |
    | `gaps` | —（工作包名"覆盖缺口确认"，协议未给形状） | **验收"步骤 2 确认完整"的唯一落点**：没有它，确认只能按心情点 |
    | `summary` | — | `blockingCount` 是"还差几条"的**服务端判据**，不让前端数 |
    | `derivation` | — | 关键词不落库，因此"按哪一版规则算的"必须随响应返回（客户端据此判 `stale`） |

    **不落任何派生结果**：派生结果 = f(Brief 版本, 规则版本)，可重算。
    落库会引入第二个来源（说明文档 §3 的 V1 否决理由）。
    """
    del user  # 权限已由 `ProjectAccessDep` + `access.require` 判定

    access.require(ProjectAction.VIEW)
    bundle = service.build_research_prompts(
        repo, project_id=access.project_id, version_id=brief_version_id
    )
    return {
        "data": {
            "projectId": str(bundle.project_id),
            "productName": bundle.product_name,
            "keywords": [service.keyword_view(item) for item in bundle.keywords],
            "prompts": [service.prompt_view(item) for item in bundle.prompts],
            "gaps": [service.gap_view(item) for item in bundle.gaps],
            "summary": dict(bundle.summary),
            "derivation": service.derivation_view(bundle),
        },
        "meta": build_meta(),
    }


@router.get(
    "/projects/{projectId}/research-files",
    response_model=ResearchFilesEnvelope[ResearchFileView],
    responses=RESEARCH_RESPONSES,
    summary="研究资料列表（配额与解析结果）",
)
def get_research_files(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: ResearchRepoDep,
    params: PageParamsDep,
    status: Annotated[str | None, Query(max_length=200)] = None,
) -> dict[str, Any]:
    """读研究资料列表（协议 §5 第 7 行）。

    > 协议原文：`GET /projects/{id}/research-files` | `status,cursor,limit` |
    > **配额与解析结果列表**

    **响应形状**：`{"data": [...], "page": {...}, "meta": {...}}`。
    配额与计数放在 `meta.research` 里（而不是塞进 `data`）：
    `data` 是**文件列表**，把一个"本项目整体状态"的对象混进去，
    前端就得每次从数组旁边那个对象里取 —— 而分页时 `data` 只有一页，
    "整体状态"放在 `data` 里会让人误以为它是"这一页的"。

    `status` 是逗号分隔的复选（见模块 docstring 第三节）。
    **非法取值报 422**，不静默忽略。
    """
    del user  # 权限已由 `ProjectAccessDep` + `access.require` 判定

    access.require(ProjectAction.VIEW)
    parse_states = _parse_status_filter(status)

    bundle = service.list_research_files(
        repo, project_id=access.project_id, parse_states=parse_states
    )

    items = [(service.file_view(row), row) for row in bundle.rows]

    spec = CursorSpec(
        sort_fields=("createdAt",),
        direction="desc",
        filters={
            "projectId": str(access.project_id),
            "status": parse_states,
        },
    )
    # `paginate_items` 要求调用方已按 spec 排序 —— repository 返回的
    # 就是 `created_at DESC, id DESC`（与 spec 一致）。
    window, page = paginate_items(
        items,
        params,
        spec,
        sort_key=lambda pair: (pair[1].created_at.isoformat(),),
        id_of=lambda pair: str(pair[1].file_id),
    )

    response = {
        "data": [view for view, _row in window],
        "page": page.model_dump(by_alias=True, mode="json"),
        # `meta` 必须**同时**带 `requestId` 与 `research` ——
        # `ResearchFilesEnvelope.meta` 声明了两者，缺一个会被
        # `response_model` 拒绝（`research` 是必填字段）。
        # 这里不用 `list_response()`：那个 helper 只拼 `requestId`，
        # 而本端点需要多一块（见 `schema.ResearchFilesEnvelope` 的说明）。
        "meta": {**build_meta(), "research": dict(bundle.quota)},
    }
    return response


def _parse_status_filter(raw: str | None) -> tuple[str, ...] | None:
    """解析 `status` 查询参数（逗号分隔，大小写不敏感）。

    返回 `None` 表示"不筛选"（参数缺失或全是空白）。

    **非法取值 → 422**，不静默忽略：一个拼错的 `status=succeded`
    若被忽略，前端会拿到全部文件并显示成"筛选后的结果" ——
    那比报错危险得多（同 WP17 对未知字段路径的判据：
    静默接受会让筛选退化成"看起来生效了"）。

    `details.allowed` 回显允许值：前端据此给出可选项，
    而不必去读文档（文档与实现漂移时前端仍然正确）。

    `details.invalid` 回显**用户原样输入的那个 token**，不是归一化后的大写：
    用户传 `status=succeded` 时回显 `SUCCEDED` 会让他对不上自己发的东西，
    更糟的是 `status=succeded,SUCCEEDED` 这种"一错一对"的输入
    回显出来只剩 `['SUCCEDED']`，"哪个错了"这条信息就丢了。
    """
    if raw is None:
        return None
    # 保留原始 token（用于 invalid 回显）与归一化后的值（用于匹配）两份。
    original = tuple(part.strip() for part in raw.split(",") if part.strip())
    wanted = tuple(item.upper() for item in original)
    if not wanted:
        return None
    invalid = [src for src, item in zip(original, wanted) if item not in STATUS_FILTER_SET]
    if invalid:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            f"status 含非法取值：{', '.join(invalid)}。",
            details={
                "field": "status",
                "invalid": invalid,
                "allowed": list(STATUS_FILTER_VALUES),
            },
        )
    # 去重且**按 `STATUS_FILTER_VALUES` 的顺序**返回：
    # 顺序影响 `CursorSpec.filters` 的指纹，而 `status=` 与
    # `status=SUCCEEDED,NONE` 与 `status=NONE,SUCCEEDED` 是同一个筛选 ——
    # 不归一化会让三个等价的 URL 产生三个互不兼容的游标。
    ordered = tuple(value for value in STATUS_FILTER_VALUES if value in set(wanted))
    return ordered


__all__ = [
    "RESEARCH_RESPONSES",
    "STATUS_FILTER_SET",
    "STATUS_FILTER_VALUES",
    "router",
]