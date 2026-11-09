"""Media 端点（**七个**，形状由本包定义 —— 协议 03 原件不在本机）。

| 方法 | 路径 | 成功码 | 用途 |
|---|---|---|---|
| POST | `/projects/{projectId}/reference-assets` | 201 | 登记一个产品参考素材 |
| GET | `/projects/{projectId}/reference-assets` | 200 | 素材列表（按项目，游标分页） |
| POST | `/contents/{contentId}/media-generations` | 202 | 发起一次静态图生成（物化身份行 + 逐条入队） |
| GET | `/contents/{contentId}/media` | 200 | 该内容的媒体列表 |
| GET | `/media/{mediaId}` | 200 | 单条媒体详情（含版本历史与加工产物） |
| POST | `/media/{mediaId}/regenerations` | 202 | 单张重做 |
| POST | `/media/{mediaId}/renditions` | 201 | 登记一次重新加工（不重跑生成） |

---

## 一、为什么七个端点的形状全部由本包定义

说明文档 §1.4 / §2.4：**数据结构 02 与协议 03 的原件都不在本机**。
本包按"本包定义 + 登记待回填"的处置（与 WP24 / WP25 / WP26 同一先例），
七条路径与路径参数命名（`{mediaId}` / `{contentId}` / `{projectId}`）
**逐条登记**在说明文档 §1.7（那张表就是本节这张的上游），
原件到位后按**文档**（不读代码）复核。

这不是"顺手加的"。每一条都对应一处**结构性必需**：

| 端点 | 不加的后果 |
|---|---|
| `POST .../reference-assets` | `reference_assets` 只能由种子数据填 ⇒ R1「每张图绑到**具体的**参考素材」**没有写入路径**；AQ-03 的对照面建不起来 |
| `GET .../reference-assets` | 目检者看不到"我这张图能绑哪些素材" ⇒ demo 的并排目检退化成"凭印象像不像"（说明文档 §2.2 的原话） |
| `POST .../media-generations` | 图片**没有任何产生方式** —— 五张表全是只读，整个包没有主路径 |
| `GET .../media` | `content-feedback.html:140`「文案 + 静态图」的图片区**没有数据来源**；"单张媒体（重做按钮）"这一行列不出来 |
| `GET /media/{mediaId}` | 版本历史与加工产物**存了但取不出来**，"第 2 版与第 1 版哪里不同"答不出（F7 重做理由那一列正是为此存在的） |
| `POST .../regenerations` | demo 的「单张媒体（重做按钮）」**无从落地**；只能整批重跑（多买 2 张） |
| `POST .../renditions` | 加工规则变了之后**无法重新加工**，只能重跑生成（重买一次模型调用） |

## 二、权限：项目在路径里走依赖，不在路径里的走**两步骤鉴权**

| 端点的寻址方式 | 权限判定 |
|---|---|
| ①② `/projects/{projectId}/...` | `ProjectAccessDep` + `access.require(...)` |
| ③④ `/contents/{contentId}/...` | **先反查项目**（`_content_project_id`）→ 再 `resolve_project_access` → `access.require(...)` |
| ⑤⑥⑦ `/media/{mediaId}/...` | **先反查项目**（`_media_project_id`）→ 再 `resolve_project_access` → `access.require(...)` |

第 2/3 行的顺序**不可调换**（`billing/api.py` 第二节的同一处置）：
权限判定需要资源的 `project_id`，而它只能从资源本身来。
反查不到 ⇒ **404 且不区分"不存在"与"不属于你"**（协议 §1.2 防枚举）。

⚠️ **`ProjectAccessDep` 只做成员级判定，不含动作级** ——
本文件七个端点**每一个**都显式调了 `require`，一个都没省。

**动词与动作的对应**（逐条给理由，不随手挑）：

| 端点 | 动作 | 理由 |
|---|---|---|
| ①`reference-assets`（登记） | `EDIT` | 素材是**项目的输入资产**（别的图会绑它）；写它是编辑项目资料 |
| ②`reference-assets`（列表） | `VIEW` | 只读 |
| ③`media-generations` | `EDIT` | 权限矩阵原文"编辑 Brief、方案、**内容**" —— 图是内容的一部分，且本路径**会花钱** |
| ④`media`（列表）、⑤详情 | `VIEW` | "查看项目与其内容" |
| ⑥`regenerations` | `EDIT` | 重做改写产出，同 ③（也会花钱） |
| ⑦`renditions` | `EDIT` | 加工产出交付资产（**不花钱调模型**，但会写 `file_objects`） |

**为什么不用 `REVIEW`**：那是"生成、反馈、**人工审核**"，落点是 WP29~WP31。
本包没有一个端点在"批准"什么东西 —— ⑦ 是"登记一次加工"，
不是"批准这一版图"。

## 三、发起端点（③）的四步同事务顺序 —— **照 `parsing/api.py` 逐字同构**

    ① 物化身份行（`service.start_media_generation`，与钱无关）
    ② 逐条 `enqueue_job`
    ③ **回填 `media_items.job_id`**（`media_repo.set_job_id`）
    ④ `flush`

**为什么 ③ 必须在同一个事务里**：处理器拿到任务时用
`find_media_item_for_job(claimed.job_id)` 反查目标（`handlers.py` 第一节）。
若入队在 A 事务、回填在 B 事务，那么任务在 B 提交之前被 worker 领走时
**反查不到** ⇒ 永久 `FAILED(TARGET_MISSING)` —— 而 HTTP 202 早就返回了，
**没有任何地方报错**。四步同事务之后，"worker 看得到 job ⇒ 那一列的
回填必然已提交"成为**结构性事实**（与 WP17 的 `run.job_id` 同一条论证）。

⚠️ 与 WP17 的一处**写法差异**（不是取向差异）：那边是
`run.job_id = view.job_id`（ORM 属性赋值），本包是
`media_repo.set_job_id(...)`（条件 UPDATE）—— 因为
`service.start_media_generation` 返回的是 `insert_media_item` 造的行，
本包**与 `ContentRepository.insert_content_item` 逐字同款**地让它回到
ORM 身份映射，但 `set_job_id` 是仓储的公开原语（**唯一**写那一列的地方），
所以走它而不是属性赋值。两者的事务语义相同。

## 四、`request_key` 从哪来 / 三处幂等键的分工

| 层 | 键 | 含版本号？ | 管什么 |
|---|---|---|---|
| **发起**（端点，N 个任务） | `f"{media_item_id}:{request_key}"` | 不含 | "同一个入队请求不要建两个任务" |
| **重做**（端点，1 个任务） | `f"{media_item_id}:{request_key}"` | 不含 | 同上（重做要建**新**任务 ⇒ 客户端给新 key） |
| **服务层**（身份行查重） | `request_key`（`last_idempotency_key` 列） | 不含 | "命中的那一批原样返回" |
| **模型调用层**（`gateway_bridge`） | `f"media-generate:{media_item_id}:v{n}:{task_kind}"` | **含** | "同一次生成不要买两次"（R5） |

★ **任务层的键里 `media_item_id` 是必须的**：一次请求要建
`images_per_content()`（=3）个任务，而 `jobs` 的唯一约束是
`(idempotency_scope, idempotency_key)` —— 键里少了它，3 条只会建成
**一个**任务，而"只跑了一张、其余两张安静地躺着"**不报错**（R1 的同族）。

## 五、响应体的三处 `dict`（各自理由不同，不是偷懒）

| 块 | 为什么不在契约里展开 |
|---|---|
| `payload` | 它的形状由 `MEDIA_SCHEMA_VERSION` 定义 —— **版本号决定形状，不由 Python 类型决定**（`types.MediaDetail` 的 docstring 原文）。展开成 v1 的字段集会让"`schema_version=2` 的行"在 v1 的契约里读不出来 |
| `referenceAssets`（详情） / `referenceBindings`（版本项） | JSONB，内部键名由 `types.BINDING_KEY_*` 定义（camelCase），元素数 1~8。它**不是**固定形状的对象，且 `service` 落库时就是那个形状 ⇒ 端点再"转换"一次就是同一份映射的第二处定义（ISS-66 形态） |
| `payload`（版本项不含） | 版本历史**不给** `payload`：那是详情端点的主体内容，乘上版本数是 `ContentSummaryBody` 同一条理由的复用 |

## 六、`response_model` 是**输出过滤器**，不是注释

`platform/pagination.py` 的 `PaginatedEnvelope` docstring 记着 WP10 踩过的那一次：
声明模型里没有的键会被**静默剥掉**，症状是"HTTP 200、`data` 正常、
前端只能看第一页"。

⇒ 两个列表端点（②④）的 `response_model` **必须**是 `PaginatedEnvelope[T]`
（带 `page`），不能是 `DataEnvelope[list[T]]`。
四个单资源端点的 `data` 形状必须显式列出顶层字段，
嵌套块（`payload` / `referenceAssets` / `referenceBindings`）用 `dict`。

## 七、`JobAcceptedBody` 为什么在本文件**再写一遍**

与 `billing/api.py` 自带 `_validation_error` / `_not_found` 是同一处置：
全仓检索，**没有任何 `api.py` 从另一个 `api.py` 借响应模型**。
`brief/api.py` 与 `content/api.py` 各有一份同名的 `JobAcceptedBody`
（四个字段逐字段相同），但它是那个模块的**协议形状**；跨模块 import 它
会让"content 改一个字段名 ⇒ media 的契约静默跟着变"。

⚠️ 三处字段集**必须**保持相同（前端拿同一套解析代码）。这条**当前没有判据**
守着 —— 已登记为待定项（与 `content/api.py` 第七节同一条）。

## 八、`protected_namespaces=()`

本文件有**三个**模型带 `model_profile_id` 字段（`MediaGenerationBody` /
`RegenerationBody` **收下**它、`MediaGenerationAcceptedBody` **回带**它）。
Pydantic v2 默认把 `model_` 前缀视为受保护命名空间，
不显式关闭会**每次导入都打一条 `UserWarning`**（`content/api.py`
第八节已逐条论证噪声为何危险）。处置与 `projects/dictionaries.py` /
`billing/schema.py` / `content/api.py` **逐字一致**：
`protected_namespaces=()`。取舍明确 —— `model_profile_id` 是**业务术语**
（模型档案的 id，`types.py` 全模块在用）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final, NoReturn

from fastapi import APIRouter, Header, Path, status
from pydantic import BaseModel, ConfigDict, Field

from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.jobs import service as jobs_service
from rednote.modules.media import service
from rednote.modules.media.dependencies import JobRepoDep, MediaRepoDep
from rednote.modules.media.repository import MediaRepository
from rednote.modules.media.types import (
    IDEMPOTENCY_SCOPE_MEDIA_GENERATE,
    KIND_MEDIA_GENERATE,
    QUEUE_MEDIA,
    REASON_CONTENT_NOT_FOUND,
    REASON_GENERATION_REQUEST_INVALID,
    REASON_MEDIA_ITEM_NOT_FOUND,
    REASON_MEDIA_VERSION_NOT_FOUND,
    REASON_MODEL_PROFILE_NOT_FOUND,
    REASON_PROJECTION_CONFLICT,
    REASON_PROJECT_NOT_FOUND,
    REASON_REFERENCE_ASSET_MISMATCH,
    REASON_REFERENCE_ASSET_NOT_ACTIVE,
    REASON_REFERENCE_ASSET_NOT_FOUND,
    REASON_REFERENCE_BINDING_CROSS_PROJECT,
    REASON_REFERENCE_BINDING_DUPLICATE,
    REASON_REFERENCE_BINDING_MALFORMED,
    REASON_REFERENCE_BINDING_MISSING,
    REASON_REFERENCE_BINDING_TOO_MANY,
    REASON_RENDITION_KIND_UNKNOWN,
    REASON_TASK_KIND_UNKNOWN,
    MediaGenerationRequest,
    images_per_content,
)
from rednote.modules.projects.dependencies import (
    ProjectAccessDep,
    ProjectRepoDep,
    resolve_project_access,
)
from rednote.modules.projects.permissions import ProjectAction
from rednote.modules.versions.service import canonical_payload_hash
from rednote.platform.api_responses import READ_RESPONSES, WRITE_RESPONSES
from rednote.platform.errors import ApiError, DataEnvelope, ErrorCode, build_meta
from rednote.platform.pagination import (
    CursorSpec,
    PageParamsDep,
    PaginatedEnvelope,
    list_response,
    paginate_items,
    single_response,
)

router = APIRouter(tags=["media"])

# 从本模块顶层再看一眼：三个列表端点已说明为什么用通用集合
# （入队本身不依赖外部服务；加工端点写 `file_objects`）。
MEDIA_READ_RESPONSES = READ_RESPONSES
MEDIA_WRITE_RESPONSES = WRITE_RESPONSES

# 列表端点的**全量扫描上限**。
#
# 见 `list_project_reference_assets` / `list_content_media` 端点里的说明：
# `platform.pagination` 是内存分页，必须拿到全量，否则会静默截断
# （只交子集时 `paginate_items` 会在子集末尾报 `hasMore=False`，
# 客户端以为到底了）。
#
# 与 `content/api.py` / `baseline/api.py` 的 `_FULL_SCAN_LIMIT` 同值同义 ——
# 但**各写一份**：它是"本模块自己的一次查询上限"，两个模块的数据量不同，
# 将来一个调大不该动到另一个。真正的修法是把锚点下推 SQL（登记待定项）。
_FULL_SCAN_LIMIT: int = 10_000


# ---------------------------------------------------------------------------
# 零、本文件自己的错误工厂与翻码表（**不跨模块借私有名**）
# ---------------------------------------------------------------------------


def _validation_error(field: str, message: str, *, reason: str) -> ApiError:
    """构造 422 `VALIDATION_FAILED`（`fieldErrors` 形状与 `service` 同口径）。

    与 `billing/api.py` / `content/api.py` 同款、同理由：全仓检索，
    **没有任何 `api.py` 从 `service` 借私有名**。借私有名的代价不是
    "能不能跑"，而是它把两层边界抹掉 —— 服务层改一个 `fieldErrors` 的键名，
    本文件会**静默**跟着变。
    """
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        message,
        details={
            "fieldErrors": [
                {"field": field, "type": reason, "message": message}
            ],
            "reason": reason,
        },
    )


def _not_found(message: str, *, reason: str, **extra: Any) -> ApiError:
    """构造 404 `RESOURCE_NOT_FOUND`（同 `_validation_error` 的落地理由）。"""
    details: dict[str, Any] = {"reason": reason}
    details.update(extra)
    return ApiError(ErrorCode.RESOURCE_NOT_FOUND, message, details=details)


#: `MediaGenerationError` / `SchemaViolation` 的 `reason` → `(协议错误码, 出错字段)`。
#:
#: ## 分组的**唯一判据**：「换一个请求参数能否成功？」
#:
#: · 能（是**请求**的错）⇒ 422 `VALIDATION_FAILED` + `fieldErrors` 指出哪个字段；
#: · 不能（要等**资源状态**变化）⇒ 409 `INVALID_TRANSITION`，不要重试；
#: · 资源本身找不到 ⇒ 404 `RESOURCE_NOT_FOUND`。
#:
#: | reason | 码 | 字段 | 逐条理由 |
#: |---|---|---|---|
#: | `MEDIA_ITEM_NOT_FOUND` | 404 | — | 点开了一个不存在的图（**也**覆盖"图不属于这条内容"—— 防枚举，两者报同一个码） |
#: | `MEDIA_VERSION_NOT_FOUND` | 404 | — | 同上（那一版是被寻址的引用对象；"不属于这一格"也归这里） |
#: | `CONTENT_NOT_FOUND` | 404 | — | 同上（`/contents/{id}/...` 反查不到，或不属于本项目） |
#: | `PROJECT_NOT_FOUND` | 404 | — | 同上（`/projects/{id}/...` 的路径资源） |
#: | `REFERENCE_ASSET_NOT_FOUND` | 404 | — | 登记时引用的**文件对象**不存在 ⇒ 点开了一个不存在的资源 |
#: | `REFERENCE_ASSET_MISMATCH` | 422 | `fileObjectId` | 文件**存在**但属于别的项目 —— 换一个文件就好。FK 管不住这条（FK 只保证"文件存在"） |
#: | `REFERENCE_ASSET_NOT_ACTIVE` | 422 | `referenceBindings` | 素材存在、属本项目，但已停用 —— **换一条素材**能成功 |
#: | `REFERENCE_BINDING_MISSING` | 422 | `referenceBindings` | 绑少了（R1 要求至少一条）⇒ 加一条能成功 |
#: | `REFERENCE_BINDING_TOO_MANY` | 422 | `referenceBindings` | 绑多了（> `MAX_REFERENCE_ASSETS`）⇒ 删几条能成功 |
#: | `REFERENCE_BINDING_DUPLICATE` | 422 | `referenceBindings` | 同一条素材绑了两次 ⇒ 去重能成功 |
#: | `REFERENCE_BINDING_CROSS_PROJECT` | 422 | `referenceBindings` | 集合里有别的项目的素材 ⇒ 换一条能成功（与单资源寻址的 `REFERENCE_ASSET_MISMATCH` **刻意分开**：那个答"这个资源你能不能看"，这个答"这批引用里哪一条越了界"） |
#: | `REFERENCE_BINDING_MALFORMED` | 422 | `referenceBindings` | 元素键形状坏（缺键 / 多键 / `assetId` 不是 UUID）—— 修载荷能成功。★ **必须登记**：不登记它就会以 `ValueError` 穿到 500（WP26 的 `REASON_BASELINE_MISMATCH` 是同一形态） |
#: | `GENERATION_REQUEST_INVALID` | 422 | — | 请求体自相矛盾（端点已用 `ge` / 枚举挡了主路径，这是防御性的第二道） |
#: | `MODEL_PROFILE_NOT_FOUND` | 422 | `modelProfileId` | 它是**请求体字段**，不是被寻址的路径资源 ⇒ 与 `brief` 的 `baseVersionId` 同处置（422 而非 404） |
#: | `TASK_KIND_UNKNOWN` | 422 | `taskKind` | 同上（请求体字段，换一个能成功） |
#: | `RENDITION_KIND_UNKNOWN` | 422 | `kind` | 同上（`RENDITION_KINDS` 只有两档） |
#: | `PARAGRAPH_REF_UNKNOWN` / `PARAGRAPH_REF_MISSING` | 409 | — | **要等资源状态变化**：这是"正文切不出段落"或"没有正文版本"（WP26 那边还没生成过正文）⇒ 重试同一个请求无用 |
#: | `CONTENT_VERSION_NOT_FOUND` | 409 | — | "这条内容还没有任何版本" ⇒ 换**任何**参数都不行，要先跑一次内容生成 |
#: | `PROJECTION_CONFLICT` | 409 | — | 乐观锁前进失败 ⇒ 状态冲突，前端该**刷新后重看**，不要重试同一个请求 |
#:
#: ## 未登记的 reason ⇒ 明确崩而不是静默 422
#:
#: 下面 `_raise_api_error` **不用** `.get(reason, 默认值)`：
#: 默认值会让"新增了一个 `REASON_*` 却忘了登记 HTTP 码"这件事
#: 静默变成 422 —— 而客户端看到的只是"提示语不太对"，
#: 排障时无从知道是漏登记。
_HTTP_BY_REASON: Final[dict[str, tuple[ErrorCode, str | None]]] = {
    REASON_MEDIA_ITEM_NOT_FOUND: (ErrorCode.RESOURCE_NOT_FOUND, None),
    REASON_MEDIA_VERSION_NOT_FOUND: (ErrorCode.RESOURCE_NOT_FOUND, None),
    REASON_CONTENT_NOT_FOUND: (ErrorCode.RESOURCE_NOT_FOUND, None),
    REASON_PROJECT_NOT_FOUND: (ErrorCode.RESOURCE_NOT_FOUND, None),
    REASON_REFERENCE_ASSET_NOT_FOUND: (ErrorCode.RESOURCE_NOT_FOUND, None),
    REASON_REFERENCE_ASSET_MISMATCH: (ErrorCode.VALIDATION_FAILED, "fileObjectId"),
    REASON_REFERENCE_ASSET_NOT_ACTIVE: (
        ErrorCode.VALIDATION_FAILED,
        "referenceBindings",
    ),
    REASON_REFERENCE_BINDING_MISSING: (
        ErrorCode.VALIDATION_FAILED,
        "referenceBindings",
    ),
    REASON_REFERENCE_BINDING_TOO_MANY: (
        ErrorCode.VALIDATION_FAILED,
        "referenceBindings",
    ),
    REASON_REFERENCE_BINDING_DUPLICATE: (
        ErrorCode.VALIDATION_FAILED,
        "referenceBindings",
    ),
    REASON_REFERENCE_BINDING_CROSS_PROJECT: (
        ErrorCode.VALIDATION_FAILED,
        "referenceBindings",
    ),
    REASON_REFERENCE_BINDING_MALFORMED: (
        ErrorCode.VALIDATION_FAILED,
        "referenceBindings",
    ),
    REASON_GENERATION_REQUEST_INVALID: (ErrorCode.VALIDATION_FAILED, None),
    REASON_MODEL_PROFILE_NOT_FOUND: (ErrorCode.VALIDATION_FAILED, "modelProfileId"),
    REASON_TASK_KIND_UNKNOWN: (ErrorCode.VALIDATION_FAILED, "taskKind"),
    REASON_RENDITION_KIND_UNKNOWN: (ErrorCode.VALIDATION_FAILED, "kind"),
    "PARAGRAPH_REF_UNKNOWN": (ErrorCode.INVALID_TRANSITION, None),
    "PARAGRAPH_REF_MISSING": (ErrorCode.INVALID_TRANSITION, None),
    "CONTENT_VERSION_NOT_FOUND": (ErrorCode.INVALID_TRANSITION, None),
    REASON_PROJECTION_CONFLICT: (ErrorCode.INVALID_TRANSITION, None),
}


def _raise_api_error(exc: Exception) -> NoReturn:
    """把服务层的业务异常翻成协议错误，**总是抛**。

    返回类型是 `NoReturn`：调用点写 `except MediaGenerationError as exc:
    _raise_api_error(exc)` 时，类型检查器能看出后面的代码不可达
    （不写 `NoReturn` 就得在每个调用点补一句 `raise`）。
    """
    reason = str(getattr(exc, "reason", "") or "")
    entry = _HTTP_BY_REASON.get(reason)
    if entry is None:  # pragma: no cover - 新增 reason 未登记时的显式崩溃
        raise RuntimeError(
            f"未登记 HTTP 映射的 reason={reason!r}"
            f"（已在 _HTTP_BY_REASON 登记的有：{sorted(_HTTP_BY_REASON)}）。"
            "新增 REASON_* 时必须同步登记 —— 否则它会静默变成 422。"
        )
    code, field = entry
    details: dict[str, Any] = {"reason": reason}
    if field is not None:
        details["fieldErrors"] = [
            {"field": field, "type": reason, "message": str(exc)}
        ]
    raise ApiError(code, str(exc), details=details)


def _content_project_id(repo: MediaRepository, content_id: uuid.UUID) -> uuid.UUID:
    """由内容 id 反查项目 id。**不存在直接 404**（两步骤鉴权的第一步）。

    用 `content_item_project` 而不是把内容行整个读出来：这一步只问
    "它属于哪个项目"（`MediaRepository` 的裸 SQL 跨模块取数，
    列名即 `content` 的契约）。
    """
    project_id = repo.content_item_project(content_id)
    if project_id is None:
        raise _not_found(
            f"内容条目 {content_id} 不存在。",
            reason=REASON_CONTENT_NOT_FOUND,
            contentId=str(content_id),
        )
    return project_id


def _media_project_id(repo: MediaRepository, media_item_id: uuid.UUID) -> uuid.UUID:
    """由媒体 id 反查项目 id。**不存在直接 404**（两步骤鉴权的第一步）。

    与 `_content_project_id` 同款。⚠️ 与"图不存在"报同一个码（防枚举）：
    调用方不该能靠 404 的措辞分辨"没有这张图"与"这张图不是你的"。
    """
    item = repo.get_media_item(media_item_id)
    if item is None:
        raise _not_found(
            f"媒体条目 {media_item_id} 不存在。",
            reason=REASON_MEDIA_ITEM_NOT_FOUND,
            mediaId=str(media_item_id),
        )
    return item.project_id


# ---------------------------------------------------------------------------
# 一、请求体与响应体
# ---------------------------------------------------------------------------


class ReferenceAssetBody(BaseModel):
    """`POST /projects/{projectId}/reference-assets` 的请求体。

    ## 为什么 `fileObjectId` 必填、`productModel` **不**必填

    `fileObjectId` 指向一张**已上传**的图（`files` 的 `PURPOSE_REFERENCE`）——
    没有它这条素材指向空气。而 `productModel` 是**人工标注**
    （`reference_assets.product_model` 在 `models.py` 里可空）：
    素材可能先入库、后标注（说明文档 §2.2 的"素材维护者与出图消费者
    不是同一人"）。把标注设成必填会**挡住先入库这条路径**，而它正是
    目检流程的起点。

    **`supersedesAssetId` 的存在性不由端点判**（由 FK 兜住），
    但**归属由服务层判**（`register_reference_asset` 的第三道）——
    存在性可交给 DB，跨项目不可（FK 管不住），这与素材本身的两道判是
    **刻意**的差别。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    file_object_id: uuid.UUID = Field(alias="fileObjectId")
    product_model: str | None = Field(default=None, alias="productModel")
    product_color: str | None = Field(default=None, alias="productColor")
    product_part: str | None = Field(default=None, alias="productPart")
    source: str | None = None
    note: str | None = None
    supersedes_asset_id: uuid.UUID | None = Field(
        default=None, alias="supersedesAssetId"
    )


class MediaGenerationBody(BaseModel):
    """`POST /contents/{contentId}/media-generations` 的请求体。

    **没有 `imageCount` 字段**（刻意的）：张数由
    `types.images_per_content()`（固定 3）唯一决定（R3）。
    收下它并忽略 = "调用方以为配了 6 张"（ISS-95 形态）；
    收下它并生效 = 第二个取数来源，而分叉的表现是**钱**
    （`images_for` 的 docstring 已论证）。⇒ **不收**。

    **`referenceBindingsBySortOrder` 是本包的一个**受限**形状**：
    键是 `sort_order` 的**字符串**形式（JSON 对象键只能是字符串），
    值是那一条的绑定数组。服务层要求键域**恰好**是
    `[0, images_per_content())`（`start_media_generation` 第四节），
    缺键即拒 —— 否则"漏了一张的绑定"与"那张本来就该没有绑定"
    在存储上同形（都是 NULL），R1 退化成恒真。

    ★ **不在端点把字符串键转成 int**：转换会造出第二个"键域"判据
    （端点转一次、服务层判一次），而两处对"`"0"` 与 `0` 是否同键"
    的理解可能分叉。服务层按 JSON 的实际形状判（它读的是同一个 dict）。
    """

    # protected_namespaces=()：`model_profile_id` 是业务名（模块 docstring 第八节）。
    model_config = ConfigDict(
        populate_by_name=True, extra="forbid", protected_namespaces=()
    )

    task_kind: str = Field(alias="taskKind")
    batch_kind: str = Field(alias="batchKind")
    model_profile_id: uuid.UUID = Field(alias="modelProfileId")
    reference_bindings_by_sort_order: dict[str, list[dict[str, Any]]] = Field(
        default_factory=dict, alias="referenceBindingsBySortOrder"
    )


class RegenerationBody(BaseModel):
    """`POST /media/{mediaId}/regenerations` 的请求体。

    | 字段 | 语义 |
    |---|---|
    | `taskKind` / `batchKind` / `modelProfileId` | **覆盖** `generation_request`（"最后一次意图"，不是历史事实） |
    | `referenceBindings` | **可选**。给了 ⇒ 覆盖待生成绑定；不给（`null`）⇒ **保留**上一次留下的绑定 |
    | `reason` | **可选**。重做理由，落进**不可变版本行**的 `regeneration_reason`（F7） |

    ⚠️ **"不给 `referenceBindings`" 与 "给一个空数组" 是两件事**：
    前者保留旧绑定，后者会被 `bindings_size` CHECK 拒（R1 要求至少一条）。
    契约上两者的区别就是 `null` 与 `[]` —— 用 `list | None`
    而不是 `list = []` 的默认值（后者会把"没说"静默变成"清空"）。
    """

    model_config = ConfigDict(
        populate_by_name=True, extra="forbid", protected_namespaces=()
    )

    task_kind: str = Field(alias="taskKind")
    batch_kind: str = Field(alias="batchKind")
    model_profile_id: uuid.UUID = Field(alias="modelProfileId")
    reference_bindings: list[dict[str, Any]] | None = Field(
        default=None, alias="referenceBindings"
    )
    reason: str | None = None


class RenditionBody(BaseModel):
    """`POST /media/{mediaId}/renditions` 的请求体。

    `mediaVersionId` **必填**：加工是针对**某一版**的
    （`media_renditions.media_version_id` 是身份键的一半），
    默认取"当前版本"会让"我加工的是刚才那一版"这件事**没有留痕** ——
    而加工产物是交付资产，它指向哪一版必须可查。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    media_version_id: uuid.UUID = Field(alias="mediaVersionId")
    kind: str
    note: str | None = None


class JobAcceptedBody(BaseModel):
    """一个已入队的任务（三个 202 / 201 端点共用 —— 本文件**自带一份**）。

    与 `brief/api.py` / `content/api.py` 的同名模型字段逐字段相同；
    跨模块 import 它会让"那个模块改一个字段名 ⇒ 本模块的契约静默跟着变"
    （模块 docstring 第七节）。
    """

    model_config = ConfigDict(populate_by_name=True)

    job_id: uuid.UUID = Field(alias="jobId")
    kind: str
    state: str
    created: bool


class MediaJobBody(BaseModel):
    """`POST .../media-generations` 的 202 响应里，**单张图**的那一项。

    **必须回带 `mediaId`**：新身份行的 id 只有服务端知道
    （`uuid_pk()` 生成的），而前端"我想看第 2 张 / 只重做第 2 张"需要它。
    不回带的话前端只能再查一次列表，并且**分不清**哪几张是这一批新建的
    —— `last_idempotency_key` 是内部字段，不在任何响应里。
    """

    model_config = ConfigDict(populate_by_name=True)

    media_id: uuid.UUID = Field(alias="mediaId")
    paragraph_ref: str = Field(alias="paragraphRef")
    sort_order: int = Field(alias="sortOrder")
    job_id: uuid.UUID = Field(alias="jobId")
    kind: str
    state: str
    created: bool


class MediaGenerationAcceptedBody(BaseModel):
    """`POST .../media-generations` 的 202 响应。

    **`mediaCount` = 本次请求产生的条数**（= `images_per_content()`，
    **不是**请求里给的数 —— 请求里根本没有这个字段）。
    重放时返回的是**第一批**的那几张，条数就是它们的条数。
    """

    # protected_namespaces=()：本模型**回带** `modelProfileId`
    # （它是响应的字段之一），因此与两个请求体同样需要关掉保护
    # （模块 docstring 第八节：不关会每次导入打一条 UserWarning）。
    model_config = ConfigDict(populate_by_name=True, protected_namespaces=())

    content_id: uuid.UUID = Field(alias="contentId")
    media_count: int = Field(alias="mediaCount")
    task_kind: str = Field(alias="taskKind")
    batch_kind: str = Field(alias="batchKind")
    model_profile_id: uuid.UUID = Field(alias="modelProfileId")
    media: list[MediaJobBody]


class RegenerationAcceptedBody(BaseModel):
    """`POST /media/{mediaId}/regenerations` 的 202 响应。

    **回带 `regenerationReason`**（原样回带本次收下的理由）：
    它落进**不可变版本行**，而用户看不到"我写的那句话有没有被收下"
    就只能去查库。`null` 是合法值（首次生成与"重做但没写理由"
    在列上同形）。
    """

    model_config = ConfigDict(populate_by_name=True)

    media_id: uuid.UUID = Field(alias="mediaId")
    content_id: uuid.UUID = Field(alias="contentId")
    task_kind: str = Field(alias="taskKind")
    regeneration_reason: str | None = Field(alias="regenerationReason")
    job: JobAcceptedBody


class ReferenceAssetBodyOut(BaseModel):
    """一条产品参考素材（列表项与登记端点的 201 响应共用）。

    **`supersedesAssetId` 必须给出来**：素材表是**不可变**的
    （只追加），因此"这一条是新版还是替换了旧版"只能靠这一列读
    —— 不给它，目检者就无法知道"我绑的这条已经过时了"。
    """

    model_config = ConfigDict(populate_by_name=True)

    asset_id: uuid.UUID = Field(alias="assetId")
    project_id: uuid.UUID = Field(alias="projectId")
    file_object_id: uuid.UUID = Field(alias="fileObjectId")
    state: str
    product_model: str | None = Field(alias="productModel")
    product_color: str | None = Field(alias="productColor")
    product_part: str | None = Field(alias="productPart")
    source: str | None
    note: str | None
    supersedes_asset_id: uuid.UUID | None = Field(alias="supersedesAssetId")
    created_at: str = Field(alias="createdAt")
    created_by: uuid.UUID | None = Field(alias="createdBy")


class MediaSummaryBody(BaseModel):
    """`GET /contents/{contentId}/media` 的一行（**不含版本历史与加工产物**）。

    不含那些是刻意的：列表页不需要把每条图的全部版本乘上条数拉回来
    （`types.MediaSummary` 的 docstring 原文）。

    ★ **`paragraphRef` 是字符串而不是下标**（R4 的原话："对齐必须落成
    显式事实"）。但它**同时说明它是投影**：这一列可被"执行期按当时正文
    重算"改写（`types.MediaSummary` 的字段注释），
    历史事实在 `versions[].paragraphRef`。
    """

    model_config = ConfigDict(populate_by_name=True)

    media_id: uuid.UUID = Field(alias="mediaId")
    content_id: uuid.UUID = Field(alias="contentId")
    project_id: uuid.UUID = Field(alias="projectId")
    state: str
    paragraph_ref: str = Field(alias="paragraphRef")
    sort_order: int = Field(alias="sortOrder")
    current_version_id: uuid.UUID | None = Field(alias="currentVersionId")
    current_version_no: int = Field(alias="currentVersionNo")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")


class RenditionBodyOut(BaseModel):
    """一条加工产物（详情端点的 `renditions[]`）。

    **`state` 单独给出来**是这张表存在的理由之一：
    "没有预览"（行不存在）与"预览生成失败"（`state=FAILED`）
    **必须可分**（说明文档 §1.7 的表格原文）—— 而两者在"只给一个
    `fileObjectId`"的形状下同形（都是 null）。
    """

    model_config = ConfigDict(populate_by_name=True)

    rendition_id: uuid.UUID = Field(alias="renditionId")
    media_version_id: uuid.UUID = Field(alias="mediaVersionId")
    kind: str
    state: str
    file_object_id: uuid.UUID | None = Field(alias="fileObjectId")
    width: int | None
    height: int | None
    byte_size: int | None = Field(alias="byteSize")
    image_format: str | None = Field(alias="imageFormat")
    note: str | None
    created_at: str = Field(alias="createdAt")


class MediaVersionBody(BaseModel):
    """一个媒体版本（`GET /media/{mediaId}` 的 `versions[]`）。

    **不给 `payload`**：那是详情端点的主体内容（模块 docstring 第五节）。
    **给 `payloadHash` 与 `schemaVersion`**：版本行是**不可变**的，
    而 `payload` 的形状由 `schema_version` 决定 —— 给出这两列，
    前端才可能判断"我拿到的两版能不能直接对比"（hash 相同 = 图未变；
    schema 不同 = 形状不同，不能按同一套渲染）。
    """

    model_config = ConfigDict(populate_by_name=True)

    version_id: uuid.UUID = Field(alias="versionId")
    version_no: int = Field(alias="versionNo")
    paragraph_ref: str = Field(alias="paragraphRef")
    sort_order: int = Field(alias="sortOrder")
    image_file_id: uuid.UUID = Field(alias="imageFileId")
    generation_context_id: uuid.UUID | None = Field(alias="generationContextId")
    payload_hash: str = Field(alias="payloadHash")
    schema_version: int = Field(alias="schemaVersion")
    created_at: str = Field(alias="createdAt")
    created_by: uuid.UUID | None = Field(alias="createdBy")


class MediaDetailBody(BaseModel):
    """`GET /media/{mediaId}` 的响应。

    三处 `dict` / `list[dict]` 的理由见模块 docstring 第五节。

    **`referenceAssets` 是当前版本绑定过的那几条**（不可变的历史事实），
    不是"下一次要绑什么"（`media_items.pending_reference_bindings`）——
    详情页要回答的是"**这一版**画的时候参照了哪些素材"（AQ-03 的对照面）。
    """

    model_config = ConfigDict(populate_by_name=True)

    media_id: uuid.UUID = Field(alias="mediaId")
    content_id: uuid.UUID = Field(alias="contentId")
    project_id: uuid.UUID = Field(alias="projectId")
    state: str
    paragraph_ref: str = Field(alias="paragraphRef")
    sort_order: int = Field(alias="sortOrder")
    current_version_id: uuid.UUID | None = Field(alias="currentVersionId")
    current_version_no: int = Field(alias="currentVersionNo")
    payload: dict[str, Any] | None
    reference_assets: list[dict[str, Any]] = Field(alias="referenceAssets")
    renditions: list[RenditionBodyOut]
    versions: list[MediaVersionBody]


# ---------------------------------------------------------------------------
# 二、视图装配（服务层值类型 → 响应 dict）
# ---------------------------------------------------------------------------


def _job_view(view: Any) -> dict[str, Any]:
    """`JobView` → 响应 dict。三个入队端点共用（**一处**构造）。"""
    return {
        "jobId": str(view.job_id),
        "kind": view.kind,
        "state": view.state,
        "created": view.created,
    }


def _reference_asset_view(row: Any) -> dict[str, Any]:
    """`ReferenceAsset` ORM 行 → 响应 dict。

    ⚠️ **源是 ORM 行而不是 `ReferenceAssetView`**：那个视图类型全仓
    **没有构造点**（`service` 不造它，只有 `__all__` 引用它）⇒
    这里直接按列名转换，与 `content/api.py` 的 `_title_candidate_view`
    （源是 service 拼的 snake_case dict）是**两种源**，但都是"一处转换"。
    """
    return {
        "assetId": str(row.id),
        "projectId": str(row.project_id),
        "fileObjectId": str(row.file_object_id),
        "state": row.state,
        "productModel": row.product_model,
        "productColor": row.product_color,
        "productPart": row.product_part,
        "source": row.source,
        "note": row.note,
        "supersedesAssetId": (
            None if row.supersedes_asset_id is None else str(row.supersedes_asset_id)
        ),
        "createdAt": row.created_at.isoformat(),
        "createdBy": None if row.created_by is None else str(row.created_by),
    }


def _media_summary_view(summary: Any) -> dict[str, Any]:
    """`MediaSummary` → 响应 dict（列表端点与详情端点共用部分字段）。"""
    return {
        "mediaId": str(summary.id),
        "contentId": str(summary.content_id),
        "projectId": str(summary.project_id),
        "state": summary.state,
        "paragraphRef": summary.paragraph_ref,
        "sortOrder": summary.sort_order,
        "currentVersionId": (
            None
            if summary.current_version_id is None
            else str(summary.current_version_id)
        ),
        "currentVersionNo": summary.current_version_no,
        "createdAt": summary.created_at.isoformat(),
        "updatedAt": summary.updated_at.isoformat(),
    }


def _rendition_view(rendition: Any) -> dict[str, Any]:
    """`RenditionView`（视图值类型）→ 响应 dict。"""
    return {
        "renditionId": str(rendition.id),
        "mediaVersionId": str(rendition.media_version_id),
        "kind": rendition.kind,
        "state": rendition.state,
        "fileObjectId": (
            None if rendition.file_object_id is None else str(rendition.file_object_id)
        ),
        "width": rendition.width,
        "height": rendition.height,
        "byteSize": rendition.byte_size,
        "imageFormat": rendition.image_format,
        "note": rendition.note,
        "createdAt": rendition.created_at.isoformat(),
    }


def _version_view(version: Any) -> dict[str, Any]:
    """`MediaVersionSummary` → 响应 dict（版本历史与详情端点共用）。"""
    return {
        "versionId": str(version.id),
        "versionNo": version.version_no,
        "paragraphRef": version.paragraph_ref,
        "sortOrder": version.sort_order,
        "imageFileId": str(version.image_file_id),
        "generationContextId": (
            None
            if version.generation_context_id is None
            else str(version.generation_context_id)
        ),
        "payloadHash": version.payload_hash,
        "schemaVersion": version.schema_version,
        "createdAt": version.created_at.isoformat(),
        "createdBy": None if version.created_by is None else str(version.created_by),
    }


def _generation_request(
    *, task_kind: str, batch_kind: str, model_profile_id: uuid.UUID
) -> MediaGenerationRequest:
    """构造 `MediaGenerationRequest`，把构造器的 `ValueError` 说清是**哪个字段**。

    构造器负责校验 `task_kind ∈ types.TASK_KINDS`（fail-closed）。
    把它折成 422 而不是让它冒到 500 —— 它是**请求体字段**的问题
    （`REASON_TASK_KIND_UNKNOWN` 的翻码表条目就是为此登记）。
    """
    try:
        return MediaGenerationRequest(
            task_kind=task_kind,
            batch_kind=batch_kind,
            model_profile_id=model_profile_id,
        )
    except ValueError as exc:
        raise _validation_error(
            "taskKind", str(exc), reason=REASON_TASK_KIND_UNKNOWN
        ) from exc


# ---------------------------------------------------------------------------
# 三、端点 ①：登记参考素材（**201**，纯数据库写）
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{projectId}/reference-assets",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[ReferenceAssetBodyOut],
    responses=MEDIA_WRITE_RESPONSES,
    summary="登记一个产品参考素材（PURPOSE_REFERENCE 的入口，201）",
)
def create_reference_asset(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: MediaRepoDep,
    project_id: Annotated[uuid.UUID, Path(alias="projectId")],
    body: ReferenceAssetBody,
) -> dict[str, Any]:
    """登记一条产品参考素材（协议 §5 无此端点，形状由本包定义，**201**）。

    ## 为什么是 201 而不是 200

    它**创建了一个新资源**（一条 `reference_assets` 行）——
    `models.py` 的口径原文：素材表**不可变**（只追加）。
    200 会让前端以为"某个东西被改了一下"。

    ## 同步 201（不入队、不调模型、不计费）

    三步纯数据库写（三道校验 + 一次 INSERT），没有异步可言。
    计费路径为空：素材登记**不产生任何模型调用**。

    ## 为什么**不**在这里做"文件是不是图"的校验

    `files` 侧上传时已按 `PURPOSE_REFERENCE` 做过类型判定，
    而本端点的对象是**已经存在的文件对象**（`fileObjectId`）。
    在这里再判一次类型会造出第二个判据，且两处的"什么算图"
    可能分叉（一个按 MIME、一个按扩展名）⇒ 声明的用途与实际不符。
    本端点只判"存在 + 属本项目"（服务层的两道）。
    """
    access.require(ProjectAction.EDIT)

    try:
        asset_id = service.register_reference_asset(
            repo,
            project_id=project_id,
            asset=service.AssetInput(
                file_object_id=body.file_object_id,
                product_model=body.product_model,
                product_color=body.product_color,
                product_part=body.product_part,
                source=body.source,
                note=body.note,
                supersedes_asset_id=body.supersedes_asset_id,
            ),
            actor_id=user.id,
        )
    except service.MediaGenerationError as exc:
        _raise_api_error(exc)

    row = repo.get_reference_asset(asset_id)
    if row is None:  # pragma: no cover - 同一事务里刚插入，几乎不可达
        # 仍然翻码而不是让它冒到 500：留一条能读懂的 404 分支，
        # 比一个"理论上不会发生"的假设更可靠（与 content/api.py 同款）。
        raise _not_found(
            f"素材 {asset_id} 登记后读不到。",
            reason=REASON_REFERENCE_ASSET_NOT_FOUND,
            assetId=str(asset_id),
        )
    return single_response(_reference_asset_view(row))


# ---------------------------------------------------------------------------
# 四、端点 ②：素材列表（按项目，游标分页）
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/reference-assets",
    response_model=PaginatedEnvelope[ReferenceAssetBodyOut],
    responses=MEDIA_READ_RESPONSES,
    summary="素材列表（按项目，新到旧，游标分页）",
)
def list_project_reference_assets(
    access: ProjectAccessDep,
    repo: MediaRepoDep,
    params: PageParamsDep,
    project_id: Annotated[uuid.UUID, Path(alias="projectId")],
) -> dict[str, Any]:
    """素材列表（协议 §5 无此端点，形状由本包定义）。

    ## 直调仓储（不走 service）

    `MediaRepository.list_reference_assets` 已经是**完整的一次查询**
    （过滤 + 排序 + 分页参数下推），而 service 层没有对应入口
    —— 为它造一个只做转发的函数就是"多一层的空壳"。
    全仓先例：`content/api.py`（`repo.get_content_item` /
    `repo.latest_blueprint`）、`billing/api.py`、`knowledge/api.py`、
    `projects/api.py` 都有 `api.py` 直调仓储的调用点。

    **因此本端点是本文件里唯一不调 `service` 的那个** ——
    这是登记过的形态，不是漏了一层。

    ## 必须取全量（`_FULL_SCAN_LIMIT`），不能用 `params.limit * 4`

    `platform.pagination` 是**内存**分页，`paginate_items` 只在交给它的
    那个列表里找锚点、切窗口、判 `hasMore`。只交截断的子集，翻到子集
    末尾会报 `hasMore=False` —— **客户端以为到底了**，而库里剩下的素材
    **永远不会出现**，且没有任何错误（与 `content/api.py` 逐字同款）。

    ## `onlyUsable` 的默认值

    `MediaRepository.list_reference_assets(only_usable=True)` 是默认。
    本端点**不暴露**这个开关：目检者要看到的是"**能用的**素材"
    （停用素材绑不上，`validate_reference_bindings` 第三道会拒）。
    列出停用素材只会让目检者选一个必然失败的绑定
    （与 `types.REASON_REFERENCE_ASSET_NOT_ACTIVE` 的翻码条目同一取向）。
    """
    access.require(ProjectAction.VIEW)

    rows = repo.list_reference_assets(
        project_id=project_id, limit=_FULL_SCAN_LIMIT, offset=0
    )

    views = [_reference_asset_view(row) for row in rows]

    spec = CursorSpec(
        sort_fields=("createdAt",),
        direction="desc",
        # 过滤器参与游标指纹：跨项目的游标会因指纹不符被拒
        # （而不是静默地把另一个项目的行当成续页）。
        filters={"projectId": str(project_id)},
    )
    window, page = paginate_items(
        views,
        params,
        spec,
        sort_key=lambda view: (view["createdAt"],),
        id_of=lambda view: view["assetId"],
    )
    return list_response(window, page)


# ---------------------------------------------------------------------------
# 五、端点 ③：发起生成（**主路径，202**）
# ---------------------------------------------------------------------------


@router.post(
    "/contents/{contentId}/media-generations",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DataEnvelope[MediaGenerationAcceptedBody],
    responses=MEDIA_WRITE_RESPONSES,
    summary="发起一次静态图生成（物化固定 3 条身份行并逐条入队，202）",
)
def create_media_generations(
    user: CurrentUserDep,
    repo: MediaRepoDep,
    job_repo: JobRepoDep,
    project_repo: ProjectRepoDep,
    content_id: Annotated[uuid.UUID, Path(alias="contentId")],
    body: MediaGenerationBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """发起一次静态图生成（协议 §5 无此端点，形状由本包定义）。

    顺序（不可调换，与 `parsing/api.py` 的四步同事务同构）：

      1. **权限**：两步骤鉴权（反查项目 → `require(EDIT)`）；
      2. **构造并校验 `MediaGenerationRequest`**（构造器 fail-closed）；
      3. **物化身份行**（`service.start_media_generation`，与钱无关）；
      4. **判"同键不同体"**（命中既有批次时，见下）；
      5. **逐条入队 + 回填 `job_id`**（同一事务，`set_job_id` 按条）；
      6. 202。

    ## 为什么第 3 步在入队之前、且**不预占**

    `service.start_media_generation` 的 docstring：「本组**不预占、
    不派发、不入队**」。预占发生在**执行**阶段
    （`execute_media_generation`），因为发起与执行是两次事务，
    而 `estimate_cost` 必须在写事务里调。

    而"建身份行"必须在**返回 202 之前**：任务要靠
    `media_items.job_id` 反查这一行（`ClaimedJob` 里没有 `media_item_id`，
    处理器只能反查 —— 见 `handlers.py` 第一节）。

    ## ★ "同键不同体"必须由**端点**拒

    `service.start_media_generation` 明写：同键不同体是**客户端的错**，
    正确处置是让端点按 `IDEMPOTENCY_CONFLICT` 拒（服务层只保证
    "命中即原样返回"这个不含歧义的行为）。

    判据是**两个独立事实的相遇**：

      · 命中的那批行的 `generation_request` 是否等于本次请求的载荷；
      · 命中的**条数**是否等于 `images_per_content()`。

    **两者都要判**：只判载荷会漏掉"第一次 3 张、第二次改了
    `batchKind` 但条数不变"这类**同键不同体**（那种会安静地返回第一批）。
    另外还须判**绑定**是否一致 —— 绑定**不在** `generation_request`
    里（它是三键载荷），而是另一列 `pending_reference_bindings`：

      · 只判载荷会漏掉"同一次请求、换了绑定"⇒ 返回第一批的绑定，
        而用户以为新绑定生效了（R1 的静默失效）。

    ## 幂等键从哪来

    客户端给了 `Idempotency-Key` 头 ⇒ 用它；没给 ⇒ 本次现生成一个
    （`uuid4().hex`）。后者意味着"没给键 = 不做幂等"，每次都是新的一批
    —— 协议 §1.3 的通用语义。
    """
    project_id = _content_project_id(repo, content_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.EDIT)

    # 第 2 步：构造即校验（task_kind 不在 TASK_KINDS ⇒ 422）。
    generation_request = _generation_request(
        task_kind=body.task_kind,
        batch_kind=body.batch_kind,
        model_profile_id=body.model_profile_id,
    )
    request_key = idempotency_key or uuid.uuid4().hex
    raw_bindings: dict[int, Any] = {
        int(order): bindings
        for order, bindings in body.reference_bindings_by_sort_order.items()
    }

    # 第 3 步：物化身份行（与钱无关）。`created=False` = 命中既有批次。
    rows, created = service.start_media_generation(
        repo,
        project_id=project_id,
        content_id=content_id,
        request=generation_request,
        bindings_by_sort_order=raw_bindings,
        idempotency_key=request_key,
        created_by=user.id,
    )

    # 第 4 步：命中既有批次 ⇒ 判"同键不同体"。
    #
    # ⚠️ 只在 `created=False` 时判：新批次是我们刚按本次请求建的，
    # 与本次请求必然一致（判它等于判自己）。
    if not created:
        expected_count = images_per_content()
        existing_payload = (
            dict(rows[0].generation_request) if rows[0].generation_request else None
        )
        existing_bindings = {
            int(row.sort_order): row.pending_reference_bindings for row in rows
        }
        # 逐条比绑定（值必须逐条相等 —— 顺序由 sort_order 归一后再比）。
        given_bindings = {
            order: [dict(item) for item in bindings]
            for order, bindings in raw_bindings.items()
        }
        normalized_existing = {
            order: None if value is None else [dict(item) for item in value]
            for order, value in existing_bindings.items()
        }
        if (
            existing_payload != generation_request.as_payload()
            or len(rows) != expected_count
            or normalized_existing != given_bindings
        ):
            raise ApiError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "相同幂等键对应了不同的请求内容。",
                details={
                    "reason": "idempotency_key_reused",
                    "idempotencyScope": IDEMPOTENCY_SCOPE_MEDIA_GENERATE,
                    "idempotencyKey": request_key,
                    "existingMediaCount": len(rows),
                    "requestedMediaCount": expected_count,
                },
            )

    # 第 5 步：逐条入队 **并回填 `job_id`**（同一事务 —— 见模块 docstring 第三节）。
    #
    # `input_hash` 对 N 张**相同**（参数确实相同），而 `media_item_id`
    # 让 idempotency_key 各不相同 —— 于是唯一约束不冲突，
    # 且"同一次请求重放"时 N 个键全部命中既有任务（`created=False`）。
    payload = generation_request.as_payload()
    input_hash = canonical_payload_hash(
        {
            **payload,
            "contentId": str(content_id),
            "projectId": str(project_id),
            "mediaCount": images_per_content(),
        }
    )
    jobs: list[Any] = []
    for row in rows:
        view = jobs_service.enqueue_job(
            job_repo,
            kind=KIND_MEDIA_GENERATE,
            queue=QUEUE_MEDIA,
            idempotency_scope=IDEMPOTENCY_SCOPE_MEDIA_GENERATE,
            idempotency_key=f"{row.id}:{request_key}",
            input_hash=input_hash,
            project_id=project_id,
            # `jobs.content_id` 装内容 id（**不是** media_item_id）：
            # 处理器不靠它寻址（那是 `media_items.job_id` 的事），
            # 但 `jobs` 列表页按内容聚合时要有可读的关联对象。
            content_id=content_id,
        )
        repo.set_job_id(media_item_id=row.id, job_id=view.job_id)
        jobs.append(view)

    # `flush` 让回填在本事务内可见（与 `parsing/api.py` 的
    # `parse_repo.flush()` 同款）。⚠️ **不回读**：`parsing/api.py` 回读
    # 是因为它后面要用 `run`（那里要返回 run 的字段），而本端点返回的
    # 是 `view` 与 `row.id`（都已持有）⇒ 回读只会多一次查询。
    repo.flush()

    return {
        "data": {
            "contentId": str(content_id),
            "mediaCount": len(rows),
            "taskKind": generation_request.task_kind,
            "batchKind": generation_request.batch_kind,
            "modelProfileId": str(generation_request.model_profile_id),
            "media": [
                {
                    "mediaId": str(row.id),
                    "paragraphRef": row.paragraph_ref,
                    "sortOrder": int(row.sort_order),
                    "jobId": str(view.job_id),
                    "kind": view.kind,
                    "state": view.state,
                    "created": view.created,
                }
                for row, view in zip(rows, jobs, strict=True)
            ],
        },
        "meta": build_meta(),
    }


# ---------------------------------------------------------------------------
# 六、端点 ④：该内容的媒体列表（游标分页）
# ---------------------------------------------------------------------------


@router.get(
    "/contents/{contentId}/media",
    response_model=PaginatedEnvelope[MediaSummaryBody],
    responses=MEDIA_READ_RESPONSES,
    summary="该内容的媒体列表（按 sortOrder 升序，游标分页）",
)
def list_content_media(
    user: CurrentUserDep,
    repo: MediaRepoDep,
    project_repo: ProjectRepoDep,
    params: PageParamsDep,
    content_id: Annotated[uuid.UUID, Path(alias="contentId")],
) -> dict[str, Any]:
    """该内容的媒体列表（协议 §5 无此端点，形状由本包定义）。

    **`contentId` 不存在 ⇒ 404，而不是空列表**：服务层先判内容在不在
    （`list_media` 的 docstring：「这条内容一张图都没有」与「这条内容
    根本不存在」是**两件不同的事**，而空列表让两者同形 ——
    前端会把一个拼错的内容 id 显示成"还没有生成图片"）。

    **已存在但一张图都没有** ⇒ 合法的空列表
    （`{"data": [], "page": {"hasMore": false}, ...}`）。

    **排序键 `sortOrder` 与仓储一致**：`list_media_items` 的
    `order_by(sort_order, id)`。端点在内存里再排一次要**用同一个键**——
    两者不一致的表现是**翻页重复或漏行**（不报错）。
    ✅ 本处**刻意不重排**（`paginate_items` 只切窗口，不排序）：
    服务层已按仓储顺序返回，重排一次就是第二个排序口径。
    """
    project_id = _content_project_id(repo, content_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.VIEW)

    rows, _total = service.list_media(
        repo, content_id=content_id, limit=_FULL_SCAN_LIMIT, offset=0
    )

    views = [_media_summary_view(row) for row in rows]

    spec = CursorSpec(
        sort_fields=("sortOrder",),
        direction="asc",
        # 过滤器用 `contentId`（本端点的寻址对象）——
        # 跨内容的游标会因指纹不符被拒。
        filters={"contentId": str(content_id)},
    )
    window, page = paginate_items(
        views,
        params,
        spec,
        sort_key=lambda view: (view["sortOrder"],),
        id_of=lambda view: view["mediaId"],
    )
    return list_response(window, page)


# ---------------------------------------------------------------------------
# 七、端点 ⑤：单条媒体详情（`{mediaId}` 无项目路径 ⇒ 两步骤鉴权）
# ---------------------------------------------------------------------------


@router.get(
    "/media/{mediaId}",
    response_model=DataEnvelope[MediaDetailBody],
    responses=MEDIA_READ_RESPONSES,
    summary="单条媒体详情（当前版本 + 素材绑定 + 加工产物 + 版本历史）",
)
def get_media_detail(
    user: CurrentUserDep,
    repo: MediaRepoDep,
    project_repo: ProjectRepoDep,
    params: PageParamsDep,
    media_id: Annotated[uuid.UUID, Path(alias="mediaId")],
) -> dict[str, Any]:
    """单条媒体详情（协议 §5 无此端点，形状由本包定义）。

    **这是 AQ-03 的核对面**：`referenceAssets` 给出**当前版本**绑定过的
    那几条素材（型号 / 颜色 / 部件逐条可读），而不是"这个项目有哪些素材"。
    如果这里只给 `referenceAssetIds`，目检者要并排看到"生成的图"与
    "它绑的素材"就还得再查两次库 —— 而说明文档 §2.2 明写
    "界面必须让目检者**并排看到**"，否则目检退化成"凭印象像不像"。

    **`renditions` 只给当前版本的**（服务层已论证）：把历次版本的产物
    全拉回来，会让前端把"第 1 版的预览"显示在第 3 版下面，
    而两者画的**不是同一张图**。

    **`versions` 用 `params` 的 limit/offset**（`PageParamsDep` 的
    `limit` 直接下推，但**不做游标**）：版本历史是详情页的一个区块，
    不是独立列表端点（协议里没有 `GET /media/{id}/versions`）——
    给它一个游标会让响应里出现两个 `page` 块。当前量级是"几次重做"，
    `limit` 足够。
    """
    project_id = _media_project_id(repo, media_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.VIEW)

    try:
        detail = service.read_media_detail(
            repo,
            media_item_id=media_id,
            version_limit=params.limit,
            version_offset=0,
        )
    except service.MediaGenerationError as exc:
        # 上面刚查过存在性 ⇒ 这里几乎不可达（两次查询之间被删才会走到）。
        # **仍然翻码而不是让它冒到 500**：留一条能读懂的 404 分支，
        # 比一个"理论上不会发生"的假设更可靠（与 content/api.py 同款）。
        _raise_api_error(exc)

    return single_response(
        {
            "mediaId": str(detail.id),
            "contentId": str(detail.content_id),
            "projectId": str(detail.project_id),
            "state": detail.state,
            "paragraphRef": detail.paragraph_ref,
            "sortOrder": detail.sort_order,
            "currentVersionId": (
                None
                if detail.current_version_id is None
                else str(detail.current_version_id)
            ),
            "currentVersionNo": detail.current_version_no,
            "payload": detail.payload,
            # ★ `referenceAssets` 原样给：键名（camelCase）由
            #   `types.BINDING_KEY_*` 定死，且 `service` 读出来的就是那个形状
            #   —— 端点再"转换"一次就是同一份映射的第二处定义（ISS-66 形态）。
            "referenceAssets": [dict(binding) for binding in detail.reference_assets],
            "renditions": [_rendition_view(row) for row in detail.renditions],
            "versions": [_version_view(row) for row in detail.versions],
        }
    )


# ---------------------------------------------------------------------------
# 八、端点 ⑥：单张重做（**202**）
# ---------------------------------------------------------------------------


@router.post(
    "/media/{mediaId}/regenerations",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DataEnvelope[RegenerationAcceptedBody],
    responses=MEDIA_WRITE_RESPONSES,
    summary="单张重做（覆盖请求参数与待生成绑定，202）",
)
def create_media_regeneration(
    user: CurrentUserDep,
    repo: MediaRepoDep,
    job_repo: JobRepoDep,
    project_repo: ProjectRepoDep,
    media_id: Annotated[uuid.UUID, Path(alias="mediaId")],
    body: RegenerationBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """单张重做（协议 §5 无此端点，形状由本包定义）。

    与发起的**四处结构差异**（不是同一件事的变体）：

    | | 发起（③） | 重做（⑥） |
    |---|---|---|
    | 建身份行 | N 行（恒 3） | **不建**（媒体行已存在） |
    | 参数 | 随 INSERT 写入 | **覆盖**（`set_generation_request`） |
    | 绑定 | 随 INSERT 写入 | **可选覆盖**（`bindings is not None` 才写） |
    | 入队数 | N 个任务 | **1** 个任务 |

    参数**覆盖**而不是新增一行历史是正确的（`repository.set_generation_request`
    的 docstring：它是"最后一次意图"，不是历史事实；历史事实在每次生成
    一行的 `media_generation_contexts`）。

    ## 任务层的键**不含版本号**，但含 `request_key`

    `f"{media_id}:{request_key}"`。这解决了 types.py 那句
    "同一张图的重做要建新任务"：

      · 重做时客户端给一个**新的** `Idempotency-Key`（或不给 ⇒ 现生成）
        ⇒ 键不同 ⇒ 建新任务 ✓；
      · 同一次重做因超时被重发（同 `Idempotency-Key`）⇒ 键相同 ⇒
        复用既有任务 ✓（不重复买一张）。

    若键里只有 `media_id`（不含 `request_key`），第一次重做之后
    **所有**后续重做都会撞键 ⇒ 返回既有任务 ⇒ **重做不生效**，
    而响应是 202、`created=false` —— 客户端以为提交了。

    ## 为什么不做"同键不同体"的判定（与发起端点**刻意不同**）

    发起端点的**身份行是它自己建的**，因此"命中既有批次"意味着
    "这次请求的内容与上次那份**不一致**"（两批的载荷都是那次请求写的）。

    重做端点不同：`generation_request` 是**被覆盖的暂存位**，
    命中的既有任务可能来自**更早一轮** —— 那一轮的载荷早被后续重做
    覆盖掉了。拿它来判"同键不同体"会把**正常重做**判成冲突
    （用户第二次重做时，库里那一列是第一次重做写进去的值）。
    ⇒ 本端点**只保证"同键复用任务"**（幂等语义），
    不判载荷一致性。这条差异**必须写下来**：两处看起来同构而行为不同
    是最容易被"统一"掉的东西。
    """
    project_id = _media_project_id(repo, media_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.EDIT)

    generation_request = _generation_request(
        task_kind=body.task_kind,
        batch_kind=body.batch_kind,
        model_profile_id=body.model_profile_id,
    )

    try:
        item = service.start_regeneration(
            repo,
            media_item_id=media_id,
            request=generation_request,
            reason=body.reason,
            # ★ `None` 表示"不改这一列"，**不是**"清空"（`start_regeneration`
            #   的 docstring 逐条论证过这两条语义必须分开）。
            bindings=body.reference_bindings,
        )
    except (service.MediaGenerationError, service.SchemaViolation) as exc:
        _raise_api_error(exc)

    request_key = idempotency_key or uuid.uuid4().hex
    view = jobs_service.enqueue_job(
        job_repo,
        kind=KIND_MEDIA_GENERATE,
        queue=QUEUE_MEDIA,
        idempotency_scope=IDEMPOTENCY_SCOPE_MEDIA_GENERATE,
        idempotency_key=f"{media_id}:{request_key}",
        input_hash=canonical_payload_hash(
            {
                **generation_request.as_payload(),
                "mediaId": str(media_id),
                "contentId": str(item.content_item_id),
            }
        ),
        project_id=project_id,
        content_id=item.content_item_id,
    )
    # ★ 回填 `job_id`：重做是**覆盖**同一行的本列（旧任务已终结）——
    #   `media_items.job_id` 是"当前在途指针"而不是任务历史
    #   （`repository.set_job_id` / 迁移 `c4d9f2a17e35` §二 的口径）。
    #   不覆盖的后果：新任务反查得到旧行吗？—— 反查是按 job_id 找行，
    #   所以**旧 job_id 被替换后新任务照样能反查到**；但若**不**回填，
    #   新任务反查的是**空**⇒ 永久 `FAILED(TARGET_MISSING)`。
    repo.set_job_id(media_item_id=media_id, job_id=view.job_id)
    repo.flush()

    return {
        "data": {
            "mediaId": str(media_id),
            "contentId": str(item.content_item_id),
            "taskKind": generation_request.task_kind,
            "regenerationReason": body.reason,
            "job": _job_view(view),
        },
        "meta": build_meta(),
    }


# ---------------------------------------------------------------------------
# 九、端点 ⑦：登记一次重新加工（**201，不重跑生成**）
# ---------------------------------------------------------------------------


@router.post(
    "/media/{mediaId}/renditions",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[RenditionBodyOut],
    responses=MEDIA_WRITE_RESPONSES,
    summary="登记一次重新加工（不重跑生成，返回 PENDING 的产物行，201）",
)
def create_media_rendition(
    user: CurrentUserDep,
    repo: MediaRepoDep,
    project_repo: ProjectRepoDep,
    media_id: Annotated[uuid.UUID, Path(alias="mediaId")],
    body: RenditionBody,
) -> dict[str, Any]:
    """登记一次加工（协议 §5 无此端点，形状由本包定义，**201**）。

    ## ★ 本端点**不加工**，只建一条 `PENDING` 行

    全仓检索：`media_renditions` 只有仓储原语（`next_attempt_no` /
    `insert_rendition`）与取值常量，**没有任何加工 worker**
    （FFmpeg / Pillow 转码的调用点一处都没有）。因此本端点的职责边界是
    **登记**："这一次要加工，档位是 X" —— 真实加工（生成预览 / 转正式资产）
    是另一个工作包的事（登记为待定项，与 WP35 的 ZIP 规则一起定）。

    ⚠️ **不假装加工成功**：返回的 `state` 恒是 `PENDING`
    （`RENDITION_STATE_INITIAL`）。写 `READY` 会当场撞
    `ready_requires_artifact` CHECK（`READY` 要求五列齐全），
    而绕过 CHECK 去编一个 `fileObjectId` 会留下一行**指向不存在资产**
    的产物 —— 而 WP35 的 ZIP 打包会把它当成可交付资产打进去。

    ## 为什么是 201：它创建一个新资源（一条 `media_renditions` 行）

    与"重做"（202）**刻意不同**：重做会入队一个会花钱的任务；
    加工**不调模型、不计费**，是纯数据库写（取号 + INSERT，同一事务）。

    ## 本节点的权限是 `EDIT` 而不是 `VIEW`

    它会写 `media_renditions`，而加工产物是**交付资产**
    （`RENDITION_DELIVERY` 那一档会被 ZIP 打包带走）——
    "看看图"与"产出一份要交付的东西"不是同一个动作。
    """
    project_id = _media_project_id(repo, media_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.EDIT)

    try:
        rendition = service.start_rendition(
            repo,
            media_item_id=media_id,
            media_version_id=body.media_version_id,
            kind=body.kind,
            note=body.note,
        )
    except service.MediaGenerationError as exc:
        _raise_api_error(exc)

    return single_response(_rendition_view(rendition))


__all__ = [
    "MEDIA_READ_RESPONSES",
    "MEDIA_WRITE_RESPONSES",
    "JobAcceptedBody",
    "MediaDetailBody",
    "MediaGenerationAcceptedBody",
    "MediaGenerationBody",
    "MediaJobBody",
    "MediaSummaryBody",
    "MediaVersionBody",
    "ReferenceAssetBody",
    "ReferenceAssetBodyOut",
    "RegenerationAcceptedBody",
    "RegenerationBody",
    "RenditionBody",
    "RenditionBodyOut",
    "router",
]