"""Content 端点（**六个**，形状由本包定义 —— 协议 03 原件不在本机）。

| 方法 | 路径 | 成功码 | 用途 |
|---|---|---|---|
| POST | `/projects/{projectId}/content-generations` | 202 | 发起一次生成（物化身份行 + 入队） |
| GET | `/projects/{projectId}/contents` | 200 | 内容列表（按项目） |
| GET | `/contents/{contentId}` | 200 | 单条内容详情 |
| GET | `/contents/{contentId}/versions` | 200 | 版本历史 |
| POST | `/contents/{contentId}/regenerations` | 202 | 局部重做（标题 / 正文 / 标签 / 整篇） |
| POST | `/contents/{contentId}/title-adoptions` | 201 | 采纳一个标题候选 |

---

## 一、为什么六个端点的形状全部由本包定义

说明文档 §1.4 第 1 条 / §2.4 第 5 项：**数据结构 02 与协议 03 的原件都不在本机**。
本包按"本包定义 + 登记待回填"的处置（与 WP24 / WP25 同一先例），
六条路径与路径参数命名（`{contentId}` / `{projectId}`）**逐条登记**在
说明文档 §2.4，原件到位后按**文档**（不读代码）复核。

这不是"顺手加的"。每一条都对应一处**结构性必需**：

| 端点 | 不加的后果 |
|---|---|
| `POST .../content-generations` | 内容**没有任何产生方式** —— 五张表全是只读，整个包没有主路径 |
| `GET .../contents` | 运营看不到"这个项目有几条内容、各自什么状态"；`content-feedback.html` 的列表页没有数据来源 |
| `GET /contents/{id}` | 手机预览的标题 / 正文 / 标签三段、结构说明、证据绑定**存了但取不出来**（AQ-01 的核对面） |
| `GET /contents/{id}/versions` | demo 的"仅标题重做"**无法对比新旧标题** —— 而那正是局部重做的核心价值（说明文档 §1.3 否决 V1 的理由之一） |
| `POST /contents/{id}/regenerations` | demo 的"仅标题 / 仅正文 / 仅标签 / 整篇"四个按钮**无从落地**；只能整篇重做，每次都花 ¥154.80 而不是 ¥16.20 |
| `POST /contents/{id}/title-adoptions` | `title_candidates` 只能读不能选 ⇒ 候选表**只进不出**，运营看到 3 个标题却选不了 |

## 二、权限：项目在路径里走依赖，不在路径里的走**两步骤鉴权**

| 端点的寻址方式 | 权限判定 |
|---|---|
| ①② `/projects/{projectId}/...` | `ProjectAccessDep` + `access.require(...)` |
| ③④⑤⑥ `/contents/{contentId}/...` | **先反查项目**（`_content_project_id`）→ 再 `resolve_project_access` → `access.require(...)` |

第 2 行的顺序**不可调换**（`billing/api.py` 第二节的同一处置）：
权限判定需要资源的 `project_id`，而它只能从资源本身来。
反查不到 ⇒ **404 且不区分"不存在"与"不属于你"**（协议 §1.2 防枚举）。

⚠️ **`ProjectAccessDep` 只做成员级判定，不含动作级** ——
`resolve_project_access` 的 docstring 明写它"解析访问权"，
而"能不能做这个动作"必须由 `access.require(ProjectAction.X)` 显式判。
本文件六个端点**每一个**都显式调了 `require`，一个都没省。

**动词与动作的对应**（逐条给理由，不随手挑）：

| 端点 | 动作 | 理由 |
|---|---|---|
| ①`content-generations` | `EDIT` | 权限矩阵原文是"编辑 Brief、**方案、内容**" —— 本包正是"内容"，且发起会**花钱**（预占在执行阶段） |
| ②`contents`（列表）、③详情、④版本历史 | `VIEW` | "查看项目与其内容" —— 内容是只读的那一半 |
| ⑤`regenerations` | `EDIT` | 重做改写内容的参数与产出，同 ① |
| ⑥`title-adoptions` | `EDIT` | 采纳追加一版内容（**改内容**），不是"审核" |

**为什么不用 `REVIEW`**：那是"生成、反馈、**人工审核**"，
其落点是 WP29~WP31 的审核动作（"标记已通过"在 `content-feedback.html` 里
报"无法标记"的那条路径）。本包六个端点没有一个在做"人工审核" ——
`title-adoptions` 是"选择一个产出"，不是"批准一个产出"。
`baseline/api.py` 用了 `REVIEW`（那是步骤 3 的人工闸门），本包**刻意不跟**：
动作名挑错在**当前权限矩阵下没有可观察差异**（`REVIEW` 与 `EDIT` 逐行相同），
所以它只能靠**语义**判对错 —— 这正是必须写清而不是随手挑一个的原因。

## 三、`content_items.lock_version` 是本包的 ETag 载体，但**六个端点都不用 `If-Match`**

`models.py` 把 `content_items.lock_version` 定义为"ETag 载体"，
而本包**没有**一条端点需要 `If-Match`。这不是漏了：

- 唯一的可变行是 `content_items`，而改它的有**两条**路径 ——
  ① 后台任务的 `advance_projection`（乐观锁，内部实现，客户端不参与）；
  ② `set_generation_request`（重做端点，见下）。
- 六条端点里**没有任何一条**让客户端**直接**改 `content_items`：
  生成是**异步**的（202 + 任务），采纳是**追加版本行**（`content_versions` 不可变）。
- 唯一"改写"是 ⑤ 的 `set_generation_request`（覆盖"这一轮请求了什么"）。
  它**不需要** `If-Match`：覆盖是**正确**行为（`repository.set_generation_request`
  的 docstring 明写"它是『最后一次意图』，不是历史事实；历史事实在每次生成
  一行的 `generation_contexts`"）。让重做带一个 ETag 前置条件，
  只会给"上次读到旧页面"的正常重做多加一个 412 失败分支（§1.6 R1 的同一条理由）。

⇒ 结论：本包的并发控制**不落在端点层**，落在服务层的乐观锁与
`uq_content_versions_item_version_no` 唯一约束上。已登记为已知取舍。

## 四、为什么端点**不**接收"重做原因"

`types.MAX_REGENERATION_REASON_CHARS` 定义了 500 字上限，但**没有任何消费点**
（全仓检索：只有 `types.py` 的定义与 `__all__` 两处）。

原因：**它无处落地**。`generation_request` 只有三个键
（`scope` / `batchKind` / `modelProfileId`），`GenerationRequest.from_payload()`
对**多余键即拒**（`types.py` 的 `from_payload` 逐条论证：多一个键说明
写入方与读取方对形状的理解已经分叉）。加第四个键要动那一处的判据；
而"重做原因"本就是**审计**信息，其落点是 WP14 的 `audit_logs`
（`record_audit` 的 `reason` / `context` 字段），不是内容表。

⇒ **端点不接收它**。收下一个校验了却无处存的字段 =
"调用方以为记下了"（ISS-95 的同一形态：声称有，实际没有）。
该常量与它的落点登记为待定项，交 WP14 的审计接线时一并处理。

## 五、两处**刻意**与 `bindings` 不对称的键名（登记，不是疏漏）

`types.py` 对 `bindings` 的键名（`paragraphIndex` / `claimId` / `reason` / `text`）
把口径定死为 **camelCase**，理由是"它是 JSONB（无列可依）"。
本文件另有三处结构，键名口径**逐处不同**，此处一次说清：

| 响应块 | 键名口径 | 依据 |
|---|---|---|
| `bindings[].*` | **camelCase** | `types.BINDING_KEY_*` 四常量（唯一来源，service 落库即此形状） |
| `titleCandidates[].*` | **camelCase** | 本文件 `_title_candidate_view` 显式转换 —— 源是 `service` 拼的 snake_case dict |
| `blueprint.*` | **snake_case**（= 列名） | `types.BLUEPRINT_FIELDS` 的注释原文："那是**列名**（snake_case，`insert_blueprint` 的入参键）" |

第三行是**刻意不转**的。转它需要第三份名单（9 个 camelCase 名字人手写一遍），
而那份名单与 `BLUEPRINT_FIELDS` / `models.py` 列名 / `insert_blueprint` 入参
构成**四方对账**（今天是三方）。第四方由人写 ⇒ 第 14 条纪律的典型触发形态
（"覆盖范围由人写"必然漂移）。前端要 camelCase 是 SDK 生成层的事
（协议 §13.3：前端据契约生成类型），不是本包再写一份映射。
**代价已登记**：`blueprint` 与 `bindings` 在同一个响应体里键名风格不同。

## 六、`response_model` 是**输出过滤器**，不是注释

`platform/pagination.py` 的 `PaginatedEnvelope` docstring 记着 WP10 踩过的那一次：
声明模型里没有的键会被**静默剥掉**，症状是"HTTP 200、`data` 正常、
前端只能看第一页"。

⇒ 两个列表端点（②④）的 `response_model` **必须**是 `PaginatedEnvelope[T]`
（带 `page`），不能是 `DataEnvelope[list[T]]`。
四个单资源端点的 `data` 形状必须显式列出顶层字段，
嵌套块（`payload` / `blueprint` / `bindings` / `scores`）用 `dict` ——
它们的形状由 `CONTENT_SCHEMA_VERSION` 与各自的值类型决定，
在端点层再展开一遍就是两处定义（契约说 A、实现给 B）。

## 七、`JobAcceptedBody` 为什么在本文件**再写一遍**

与 `billing/api.py` 自带 `_validation_error` / `_not_found` 是同一处置：
全仓检索，**没有任何 `api.py` 从另一个 `api.py` 借响应模型**。
`brief/api.py` 有一份同名的 `JobAcceptedBody`（四个字段逐字段相同），
但它是 brief 模块的**协议形状**；跨模块 import 它会让
"brief 改一个字段名 ⇒ content 的契约静默跟着变"（同一个名字，
没有编译器或判据会提示这是跨层耦合）。

⚠️ 两处字段集**必须**保持相同（前端拿同一套解析代码）。这条**当前没有判据**
守着 —— 已登记为待定项：真判据应从 OpenAPI 契约里比对两个 `$ref`
指向的 schema，而不是在测试里手写一份字段名清单（那是第 14 条形态）。

## 八、三处 `protected_namespaces=()` 的必要性（不是照抄）

Pydantic v2 默认把 `model_` 前缀视为**受保护命名空间**（避让
`model_dump` / `model_config` 这些框架自有方法）。本文件有三个模型带
`model_profile_id` 字段，不显式关闭保护会**每次导入都打一条
`UserWarning`**（`Field "model_profile_id" ... has conflict with
protected namespace "model_"`）。

这类"警告噪声"的危险不在噪声本身，而在**它会训练读者忽略导入期输出** ——
真出问题时（比如某次确实漏了字段）那条警告会淹没在同样的噪声里。

处置与 `projects/dictionaries.py:121`（`model_code`）、
`billing/schema.py:259`（`model_profile_id`）**逐字一致**：
`protected_namespaces=()`。取舍是明确的 ——
`model_profile_id` 是**业务术语**（模型档案的 id，`types.py` 全模块在用），
改字段名去迁就框架的内部约定是让领域词汇向实现细节让步。

⚠️ `default=str`：这三处**不**涉及，别混。`canonical_payload_hash` 的
`default=str` 是序列化兜底（UUID → str），与命名空间保护无关。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final, NoReturn

from fastapi import APIRouter, Header, Path, status
from pydantic import BaseModel, ConfigDict, Field

from rednote.modules.content import service
from rednote.modules.content.dependencies import (
    ContentRepoDep,
    JobRepoDep,
    VersionRepoDep,
)
from rednote.modules.content.repository import ContentRepository
from rednote.modules.content.types import (
    IDEMPOTENCY_SCOPE_CONTENT_GENERATE,
    KIND_CONTENT_GENERATE,
    QUEUE_CONTENT,
    REASON_CONTENT_NOT_FOUND,
    REASON_CONTENT_VERSION_NOT_FOUND,
    REASON_GENERATION_REQUEST_INVALID,
    REASON_MODEL_PROFILE_NOT_FOUND,
    REASON_PROJECTION_CONFLICT,
    REASON_SCOPE_REQUIRES_BLUEPRINT,
    REASON_TITLE_CANDIDATE_MISMATCH,
    REASON_TITLE_CANDIDATE_NOT_FOUND,
    GenerationRequest,
)
from rednote.modules.identity.dependencies import CurrentUserDep
from rednote.modules.jobs import service as jobs_service
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

router = APIRouter(tags=["content"])

# 端点可能返回 503（网关未就绪时的任务失败会在后续查询里体现，
# 但入队本身不依赖外部服务）—— 因此用通用的读写集合。
CONTENT_READ_RESPONSES = READ_RESPONSES
CONTENT_WRITE_RESPONSES = WRITE_RESPONSES

# 列表端点的**全量扫描上限**。
#
# 见 `list_contents` 端点里的说明：`platform.pagination` 是内存分页，
# 必须拿到全量，否则会静默截断（只交子集时 `paginate_items` 会在子集
# 末尾报 `hasMore=False`，客户端以为到底了）。
#
# 与 `baseline/api.py` 的 `_FULL_SCAN_LIMIT` 同值同义 —— 但**各写一份**：
# 它是"本模块自己的一次查询上限"，两个模块的数据量不同，
# 将来一个调大不该动到另一个。真正的修法是把锚点下推 SQL（登记待定项）。
_FULL_SCAN_LIMIT: int = 10_000

#: 一次发起请求允许的内容条数上限。
#:
#: ## 这不是"批次档位名单"的第二份 —— 它只是本模块的资源保护上限
#:
#: `batchKind` 的三档（`TRIAL` / `SCALE` / `BULK`）与"该档容许多少条"
#: 是 WP24 的契约，由 `estimate_cost` → `planning.validate_quantity` 校验。
#: 本包**刻意不复制那份联动关系**（`GenerationRequest.__post_init__` 的
#: docstring 已论证：端点里再写一遍 ⇒ 第二真值来源）。
#:
#: 但端点侧**必须**有一个硬上限，理由是**本端点的资源消耗是二次的**：
#: 一次请求会建 N 行 `content_items` **并且**入 N 个任务
#: （`enqueue_job` 调 N 次）。没有上限时，一个 `contentCount: 100000`
#: 的请求会安静地建出十万行内容与十万个任务 —— 而它**返回 202**，
#: 调用方以为提交成功了。
#:
#: 50 取自"最大档位的上界"（`BULK`）。⚠️ **它不是档位的函数**：
#: 将来若 `BULK` 改成 80，这个数**不该跟着变** —— 它约束的是
#: "一次 HTTP 请求最多产生多少次入队"，而不是"买了哪个档"。
#: 两者若被写成联动，就是"用档位反推条数"的镜像错误。
MAX_CONTENT_COUNT_PER_REQUEST: Final[int] = 50


# ---------------------------------------------------------------------------
# 零、本文件自己的错误工厂与翻码表（**不跨模块借私有名**）
# ---------------------------------------------------------------------------


def _validation_error(field: str, message: str, *, reason: str) -> ApiError:
    """构造 422 `VALIDATION_FAILED`（`fieldErrors` 形状与 `service` 同口径）。

    与 `billing/api.py:313` 同款、同理由：全仓检索，
    **没有任何 `api.py` 从 `service` 借私有名**（`from ...service import _xxx`
    命中 0 处）。借私有名的代价不是"能不能跑"，而是它把两层边界抹掉 ——
    服务层改一个 `fieldErrors` 的键名，本文件会**静默**跟着变。
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


#: `ContentGenerationError.reason` → `(协议错误码, 出错的请求字段)`。
#:
#: ## 分组的**唯一判据**：「换一个请求参数能否成功？」
#:
#: · 能（是**请求**的错）⇒ 422 `VALIDATION_FAILED` + `fieldErrors` 指出哪个字段；
#: · 不能（要等**资源状态**变化）⇒ 409 `INVALID_TRANSITION`，不要重试；
#: · 资源本身找不到 ⇒ 404 `RESOURCE_NOT_FOUND`。
#:
#: | reason | 码 | 字段 | 逐条理由 |
#: |---|---|---|---|
#: | `CONTENT_NOT_FOUND` | 404 | — | 点开了一个不存在的资源 |
#: | `TITLE_CANDIDATE_NOT_FOUND` | 404 | — | 同上（候选是被寻址的引用对象） |
#: | `TITLE_CANDIDATE_MISMATCH` | 422 | `candidateId` | 候选**存在**但属于别的内容 —— 换一个候选就好。FK 管不住这条（FK 只保证"候选存在"），落点见 `repository.candidate_owner` + `ddl.py` 触发器 3.2 |
#: | `SCOPE_REQUIRES_BLUEPRINT` | 422 | `scope` | 换 `FULL` 就能成功（`service.prepare_regeneration` 的 message 原文就写着"请改用 FULL 范围"）⇒ 是**请求**的错 |
#: | `GENERATION_REQUEST_INVALID` | 422 | — | 请求体自相矛盾（端点已用 `ge=1` 挡了主路径，这是防御性的第二道） |
#: | `MODEL_PROFILE_NOT_FOUND` | 422 | `modelProfileId` | `modelProfileId` 是**请求体字段**，不是被寻址的路径资源 ⇒ 与 `brief` 的 `baseVersionId` 同处置（422 而非 404） |
#: | `CONTENT_VERSION_NOT_FOUND` | 409 | — | "这条内容还没有任何版本" ⇒ 换**任何**候选都不行，要先生成一版 ⇒ 资源状态问题 |
#: | `PROJECTION_CONFLICT` | 409 | — | 乐观锁前进失败 ⇒ 状态冲突，前端该**刷新后重看**，不要重试同一个请求 |
#:
#: ## 未登记的 reason ⇒ 明确崩而不是静默 422
#:
#: 下面 `_raise_api_error` **不用** `.get(reason, 默认值)`：
#: 默认值会让"新增了一个 `REASON_*` 却忘了登记 HTTP 码"这件事
#: 静默变成 422 —— 而客户端看到的只是"提示语不太对"，
#: 排障时无从知道是漏登记。这与"覆盖范围由人写"是同一族问题，
#: 处置是让**漏登记**当场可见（500 + 一条能读出原因的消息）。
_HTTP_BY_REASON: Final[dict[str, tuple[ErrorCode, str | None]]] = {
    REASON_CONTENT_NOT_FOUND: (ErrorCode.RESOURCE_NOT_FOUND, None),
    REASON_TITLE_CANDIDATE_NOT_FOUND: (ErrorCode.RESOURCE_NOT_FOUND, None),
    REASON_TITLE_CANDIDATE_MISMATCH: (ErrorCode.VALIDATION_FAILED, "candidateId"),
    REASON_SCOPE_REQUIRES_BLUEPRINT: (ErrorCode.VALIDATION_FAILED, "scope"),
    REASON_GENERATION_REQUEST_INVALID: (ErrorCode.VALIDATION_FAILED, None),
    REASON_MODEL_PROFILE_NOT_FOUND: (ErrorCode.VALIDATION_FAILED, "modelProfileId"),
    REASON_CONTENT_VERSION_NOT_FOUND: (ErrorCode.INVALID_TRANSITION, None),
    REASON_PROJECTION_CONFLICT: (ErrorCode.INVALID_TRANSITION, None),
}


def _raise_api_error(exc: service.ContentGenerationError) -> NoReturn:
    """把服务层的 `ContentGenerationError` 翻成协议错误，**总是抛**。

    返回类型是 `NoReturn`：调用点写 `except ContentGenerationError as exc:
    _raise_api_error(exc)` 时，类型检查器能看出后面的代码不可达
    （不写 `NoReturn` 就得在每个调用点补一句 `raise` 或 `assert False`）。
    """
    entry = _HTTP_BY_REASON.get(exc.reason)
    if entry is None:  # pragma: no cover - 新增 reason 未登记时的显式崩溃
        raise RuntimeError(
            f"未登记 HTTP 映射的 reason={exc.reason!r}"
            f"（已在 _HTTP_BY_REASON 登记的有：{sorted(_HTTP_BY_REASON)}）。"
            "新增 REASON_* 时必须同步登记 —— 否则它会静默变成 422。"
        )
    code, field = entry
    details: dict[str, Any] = {"reason": exc.reason}
    if field is not None:
        details["fieldErrors"] = [
            {"field": field, "type": exc.reason, "message": str(exc)}
        ]
    raise ApiError(code, str(exc), details=details)


def _content_project_id(repo: ContentRepository, content_id: uuid.UUID) -> uuid.UUID:
    """由内容 id 反查项目 id。**不存在直接 404**（两步骤鉴权的第一步）。

    用 `get_content_item` 而不是 `read_content_detail`：这一步只问
    "它属于哪个项目"，不需要把 payload / 蓝图 / 候选 / 上下文全读出来。
    """
    item = repo.get_content_item(content_id)
    if item is None:
        raise _not_found(
            f"内容条目 {content_id} 不存在。",
            reason=REASON_CONTENT_NOT_FOUND,
            contentId=str(content_id),
        )
    return item.project_id


def _generation_request(
    *,
    scope: str,
    batch_kind: str,
    model_profile_id: uuid.UUID,
    scene_key: str | None = None,
    form_key: str | None = None,
) -> GenerationRequest:
    """构造 `GenerationRequest`，把构造器的 `ValueError` 说清是**哪个字段**。

    `GenerationRequest` 的构造器负责归一（`TAGS` → `COPY`）与校验，
    但它的 `ValueError` 只有消息、没有字段名（它是值类型，不该知道 HTTP 字段）。
    这里补上字段名 —— 让 422 的 `fieldErrors[].field` 指向真正出错的输入。

    **只可能因 `scope` 与 `batchKind` 走到异常分支**：
    `modelProfileId` 的类型由 pydantic 在进函数之前就校验了
    （请求体字段声明为 `uuid.UUID`）。
    """
    if not isinstance(batch_kind, str) or not batch_kind.strip():
        raise _validation_error(
            "batchKind",
            "batchKind 必须是非空字符串。",
            reason=REASON_GENERATION_REQUEST_INVALID,
        )
    try:
        return GenerationRequest(
            scope=scope,
            batch_kind=batch_kind,
            model_profile_id=model_profile_id,
            scene_key=scene_key,
            form_key=form_key,
        )
    except ValueError as exc:
        # 走到这里只剩 `scope` 与 sceneKey / formKey（后两者按消息里的字段名指回）。
        for field_name, api_name in (("scene_key", "sceneKey"), ("form_key", "formKey")):
            if str(exc).startswith(field_name):
                raise _validation_error(
                    api_name, str(exc), reason=REASON_GENERATION_REQUEST_INVALID
                ) from exc
        # 其余只剩 `scope`（`batchKind` 在上面判过、`modelProfileId`
        # 由 pydantic 判过）。**不把它包成 500**：未知 scope 是客户端的
        # 输入错，而构造器的消息里已经带着合法取值清单。
        raise _validation_error(
            "scope", str(exc), reason=REASON_GENERATION_REQUEST_INVALID
        ) from exc


# ---------------------------------------------------------------------------
# 一、请求体
# ---------------------------------------------------------------------------


class ContentGenerationBody(BaseModel):
    """`POST /projects/{projectId}/content-generations` 的请求体。

    **四个字段，正好是"发起一次生成"需要知道的全部**：

    | 字段 | 为什么必须在**发起**时给 |
    |---|---|
    | `contentCount` | 物化几行身份行。它**不**进 `generation_request`（任务粒度是单条内容，处理器的 `calls_for` 恒以 `count=1` 调用） |
    | `scope` | 要跑哪几个 `task_kind`（`FULL` 两个都跑 / 其余只跑文案）。它是**用户的意图**，库里没有任何列能反推 |
    | `batchKind` | `estimate_cost` 的必填入参（G3 的硬约束靠它生效）。**不由条数反推**：反推出来的档位必然容纳该条数 ⇒ 那次校验恒过，硬约束被静默关掉 |
    | `modelProfileId` | 决定用哪个模型（`provider_id` 由它唯一解出）。本仓无"项目默认模型档案"概念 |

    后三个进 `content_items.generation_request`（JSONB），由处理器在执行阶段
    读出来用；`contentCount` **只**用于这一次 INSERT 循环与入队循环。

    **没有 `projectId`**：它在路径里（`ProjectAccessDep` 已经把它校验过了，
    再让客户端传一个只会多一条"体里的与路径里的不一致"的失败路径）。

    **没有 `taskKinds`**：`scope` → `task_kind` 的映射是**唯一**的
    （`types.SCOPE_TASK_KINDS`，由 `task_kinds_for_scope` 提供）。
    让客户端传 kind 会让"scope 说是仅标题、kind 说跑结构"这种
    没有正确答案的状态成为可能。

    **没有金额 / 预估字段**：计费口径在全仓只有**一处** —— `estimate_cost`
    （本包经 `content/billing_bridge.py` 的 `estimate_generation_cost` 调用），
    执行阶段的预占用的也是**同一套** `estimate_cost`。本包若另算一份，
    金额就有了第二处口径。

    ⚠️ 但**不能**由此得出"展示用预估已由 WP25 的某个端点提供"的结论。本注释
    原先写的是 `POST /projects/{projectId}/budget-estimates` —— 该路径在全仓
    **从未注册**（`budget-estimates` 零命中），真实端点是
    **`POST /projects/{projectId}/cost-estimates`**（`modules/billing/api.py:882`）。

    按 ISS-190 立下的口径：**凡以"别包已有 X"为由否决一项设计，必须当场用一条
    grep 把 X 与它注册的装饰器对上，并把这条命令写在理由所在的那一行**：

        grep -rn '"/projects/\\{projectId\\}/[a-z-]+"' modules/billing/api.py

    而且"X 存在"不等于"X 接得了这个活"：`cost-estimates` 的入参是
    `EstimateCostBody`（`modules/billing/schema.py:250`），只有裸 `quantity`、
    **没有 `task_kind`**；而 `task_kind → quantity` 的换算（`calls_for`）在本包
    （`modules/content/types.py:825`）⇒ 那个端点给不出 §1.7 前端要的
    "文案生成 / 标题候选"**分开计价**。该缺口已登记为 **ISS-190**（MAJOR / OPEN），
    本注释不替它下结论 —— 它需要一次产品/架构决策，不是一条注释能盖住的。
    """

    # protected_namespaces=()：见模块 docstring 第八节。字段 `model_profile_id`
    # 是业务名（模型档案），与 Pydantic 内部的 `model_*` 方法无关。
    model_config = ConfigDict(
        populate_by_name=True, extra="forbid", protected_namespaces=()
    )

    content_count: int = Field(
        alias="contentCount", ge=1, le=MAX_CONTENT_COUNT_PER_REQUEST
    )
    scope: str
    batch_kind: str = Field(alias="batchKind")
    model_profile_id: uuid.UUID = Field(alias="modelProfileId")
    # 接缝 §5.15：可选。缺省 = 未指派（NULL）；格式校验在 `GenerationRequest` 构造器。
    scene_key: str | None = Field(default=None, alias="sceneKey")
    form_key: str | None = Field(default=None, alias="formKey")


class RegenerationBody(BaseModel):
    """`POST /contents/{contentId}/regenerations` 的请求体。

    前三个字段与 `ContentGenerationBody` 的后三个**逐字段同义**
    （它们最终都变成 `GenerationRequest`）—— 重复声明而不是抽一个基类：
    基类会让"发起"与"重做"共享一份字段集，而两者的**必填性将来可能不同**
    （比如重做可以省略 `modelProfileId` 表示"沿用上次"），
    继承关系在那时会变成障碍。当前两份都短，显式更好读。

    **没有 `contentCount`**：重做**不建身份行**（内容已存在），
    它的产物是新的一版。条数概念在重做上没有意义。

    **没有 `reason`**：见模块 docstring 第四节（无处落地）。
    """

    # protected_namespaces=()：同 `ContentGenerationBody`（模块 docstring 第八节）。
    model_config = ConfigDict(
        populate_by_name=True, extra="forbid", protected_namespaces=()
    )

    scope: str
    batch_kind: str = Field(alias="batchKind")
    model_profile_id: uuid.UUID = Field(alias="modelProfileId")


class TitleAdoptionBody(BaseModel):
    """`POST /contents/{contentId}/title-adoptions` 的请求体。

    **只有一个 `candidateId`**：被采纳的候选。其余一切都从库里读 ——
    采纳后的正文三件是"标题换成候选的、正文与标签原样复用上一版"
    （`service.adopt_title_candidate` 的落库规则表），
    让客户端把 `body` / `tags` 也传上来会给"客户端改了正文却被记成
    『采纳标题』"留一个入口。

    **没有 `adoptedBy`**：采纳人取自当前登录用户（`user.id`）。
    给一个可传的字段就是给"以别人的名义采纳"留入口 ——
    与 `baseline/api.py` 不接受 `decidedBy` 是同一条判据
    （**意图可以传，身份不能传**）。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    candidate_id: uuid.UUID = Field(alias="candidateId")


# ---------------------------------------------------------------------------
# 二、响应体
# ---------------------------------------------------------------------------


class JobAcceptedBody(BaseModel):
    """一次入队的结果（本文件自带一份，见模块 docstring 第七节）。

    `created=false` 表示复用了既有的排队中任务（幂等命中）——
    客户端据此区分"我刚发起的"与"系统里已经有了"。
    """

    model_config = ConfigDict(populate_by_name=True)

    job_id: uuid.UUID = Field(alias="jobId")
    kind: str
    state: str
    created: bool


class ContentJobBody(BaseModel):
    """`POST .../content-generations` 的 202 响应里，**单条内容**的那一项。

    **必须回带 `contentId`**：新内容行的 id 只有服务端知道
    （`uuid_pk()` 生成的），而前端"我想看第 2 条 / 只重做第 2 条"需要它。
    不回带的话前端只能再查一次列表，并且**分不清**哪几条是这一批新建的 ——
    `last_idempotency_key` 是内部字段，不在任何响应里。
    """

    model_config = ConfigDict(populate_by_name=True)

    content_id: uuid.UUID = Field(alias="contentId")
    job_id: uuid.UUID = Field(alias="jobId")
    kind: str
    state: str
    created: bool
    scene_key: str | None = Field(default=None, alias="sceneKey")
    form_key: str | None = Field(default=None, alias="formKey")


class ContentGenerationAcceptedBody(BaseModel):
    """`POST .../content-generations` 的 202 响应。

    **`scope` 回带的是归一后的值**（`TAGS` → `COPY`）——
    与 `service.start_generation` 的 `types.normalize_scope` 同源。
    回带原始值会让响应**撒谎**：用户说"重做标签"，
    实际执行的是整段正文（标签随正文一起重做），而响应里写着 `TAGS`
    ⇒ 用户看不出来（`types.normalize_scope` 的 docstring 逐条论证过这一点）。

    **`contentCount` = 本次请求的条数**（不是"新加的条数"）：
    重放时返回的是**第一批**的那几条，条数就是它们的条数。
    两者的差（"我请求了 5 条、只拿到 3 条"）在"同键不同体"时
    已经被 `IDEMPOTENCY_CONFLICT` 拒掉了，因此这里不会出现
    "请求数 ≠ 返回数"而不报错的情形。
    """

    model_config = ConfigDict(populate_by_name=True)

    content_count: int = Field(alias="contentCount")
    scope: str
    contents: list[ContentJobBody]


class RegenerationAcceptedBody(BaseModel):
    """`POST /contents/{contentId}/regenerations` 的 202 响应。

    `taskKinds` **回带实际要跑的 kind**（顺序有意义：结构在文案之前）——
    它由 `service.prepare_regeneration` 用 `task_kinds_for_scope` 算出来，
    而不是端点再写一份映射（分叉的表现是"响应说跑两个、实际只跑了一个"，
    而少跑的那次**不报错**）。
    """

    model_config = ConfigDict(populate_by_name=True)

    content_id: uuid.UUID = Field(alias="contentId")
    scope: str
    task_kinds: list[str] = Field(alias="taskKinds")
    job: JobAcceptedBody


class ContentSummaryBody(BaseModel):
    """`GET /projects/{projectId}/contents` 的一行（**不含正文**）。

    不含正文是刻意的：列表页不需要把 20KB 正文乘上条数拉回来
    （`types.ContentSummary` 的 docstring 原文）。
    """

    model_config = ConfigDict(populate_by_name=True)

    content_id: uuid.UUID = Field(alias="contentId")
    project_id: uuid.UUID = Field(alias="projectId")
    title: str | None
    state: str
    current_version_no: int = Field(alias="currentVersionNo")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")
    scene_key: str | None = Field(default=None, alias="sceneKey")
    form_key: str | None = Field(default=None, alias="formKey")


class GenerationContextBody(BaseModel):
    """一条生成上下文（`GET /contents/{contentId}` 的 `contexts[]`）。

    **六个 id 一起给而不是只给 `promptVersionId`**（OP-160 的影响面查询
    按它反查，但"改了提示词之后质量变了"与"换了模型之后质量变了"
    要靠同时给出 `baselineId` / `modelProfileId` 才能区分）——
    `types.GenerationContextView` 的 docstring 已逐条论证。
    """

    # protected_namespaces=()：`model_profile_id` 是业务名（模块 docstring 第八节）。
    model_config = ConfigDict(populate_by_name=True, protected_namespaces=())

    context_id: uuid.UUID = Field(alias="contextId")
    project_id: uuid.UUID = Field(alias="projectId")
    content_id: uuid.UUID = Field(alias="contentId")
    content_version_id: uuid.UUID | None = Field(alias="contentVersionId")
    task_kind: str = Field(alias="taskKind")
    prompt_template_id: uuid.UUID | None = Field(alias="promptTemplateId")
    prompt_version_id: uuid.UUID | None = Field(alias="promptVersionId")
    baseline_id: uuid.UUID | None = Field(alias="baselineId")
    model_profile_id: uuid.UUID | None = Field(alias="modelProfileId")
    created_at: str = Field(alias="createdAt")


class TitleCandidateBody(BaseModel):
    """一个标题候选（`GET /contents/{contentId}` 的 `titleCandidates[]`）。

    **`scores` 是 `dict` 而不是六个具名字段**：六维评分的**维度名**由
    `types.TITLE_SCORE_DIMENSIONS` 定义，而"这六个维度的取值域与量纲
    待产品拍板"（说明文档 §2.4 第 3 项）。把六个维度展开成六个 pydantic
    字段，就等于把一份**未拍板的**口径固化进契约 —— 产品改一个维度名，
    契约的字段名跟着变（前端 SDK 全部重生成）。用 `dict` 让口径的
    变更留在 `TITLE_SCORE_DIMENSIONS` 一处。
    """

    model_config = ConfigDict(populate_by_name=True)

    candidate_id: uuid.UUID = Field(alias="candidateId")
    candidate_index: int = Field(alias="candidateIndex")
    title: str
    scores: dict[str, Any]
    keywords: list[str]
    highlight: str | None
    risk_note: str | None = Field(alias="riskNote")


class ContentDetailBody(BaseModel):
    """`GET /contents/{contentId}` 的响应。

    ## 三处用 `dict` / `list[dict]` 的字段，各自理由不同

    | 字段 | 为什么不在契约里展开 |
    |---|---|
    | `payload` | 它的形状由 `CONTENT_SCHEMA_VERSION` 定义 —— **版本号决定形状，不由 Python 类型决定**（`types.ContentDetail` 的 docstring 原文）。展开成 v1 的字段集会让"`schema_version=2` 的行"在 v1 的契约里读不出来 |
    | `blueprint` | 键名是 `types.BLUEPRINT_FIELDS` 的 9 个**列名**（模块 docstring 第五节第三行：刻意不转 camelCase） |
    | `bindings` | JSONB，内部键名由 `types.BINDING_KEY_*` 定义（camelCase），元素数 = 段落数（≤ `MAX_PARAGRAPHS`）。它**不是**固定形状的九字段对象 |

    ## `contexts` 与 `titleCandidates` 则**都**展开了

    它们各自的字段集是**定死的**（`GenerationContextView` 10 个 /
    候选取 7 个），且不属于"随版本演进"的那一类 —— 展开它们
    让前端 SDK 能生成具体类型，而这正是协议 §13.3 要的。
    """

    model_config = ConfigDict(populate_by_name=True)

    content_id: uuid.UUID = Field(alias="contentId")
    project_id: uuid.UUID = Field(alias="projectId")
    state: str
    current_version_id: uuid.UUID | None = Field(alias="currentVersionId")
    current_version_no: int = Field(alias="currentVersionNo")
    payload: dict[str, Any] | None
    blueprint: dict[str, Any] | None
    title_candidates: list[TitleCandidateBody] = Field(alias="titleCandidates")
    bindings: list[dict[str, Any]]
    contexts: list[GenerationContextBody]


class ContentVersionBody(BaseModel):
    """一个内容版本（版本历史的一行，也是采纳端点的 201 响应）。

    **`adoptedTitleCandidateId` 必须在这里给出来**：采纳记在版本行上
    （不给 `title_candidates` 加 `adopted` 列），因此"这个候选有没有
    被采纳过"只能**反查版本** —— 版本历史正是那个反查面。
    少给这一列，前端就只能拿 `title` 与候选标题做**字符串比较**，
    而两个候选取同一个标题是完全合法的 ⇒ 字符串相等**不等于**被采纳
    （`types.ContentVersionSummary` 的 docstring 原文）。

    **不给 `body` / `bindings` / `payload`**：那是详情端点的内容。
    版本历史是"挑一版去看"，把 20KB 正文乘上版本数拉回来是
    `ContentSummaryBody` 同一条理由的复用。
    """

    model_config = ConfigDict(populate_by_name=True)

    version_id: uuid.UUID = Field(alias="versionId")
    version_no: int = Field(alias="versionNo")
    title: str
    blueprint_id: uuid.UUID | None = Field(alias="blueprintId")
    adopted_title_candidate_id: uuid.UUID | None = Field(
        alias="adoptedTitleCandidateId"
    )
    generation_context_id: uuid.UUID | None = Field(alias="generationContextId")
    payload_hash: str = Field(alias="payloadHash")
    schema_version: int = Field(alias="schemaVersion")
    created_at: str = Field(alias="createdAt")
    created_by: uuid.UUID | None = Field(alias="createdBy")


# ---------------------------------------------------------------------------
# 三、视图装配（服务层值类型 → 响应 dict）
# ---------------------------------------------------------------------------


def _job_view(view: Any) -> dict[str, Any]:
    """`JobView` → 响应 dict。三个入队端点共用（**一处**构造）。"""
    return {
        "jobId": str(view.job_id),
        "kind": view.kind,
        "state": view.state,
        "created": view.created,
    }


def _title_candidate_view(candidate: dict[str, Any]) -> dict[str, Any]:
    """候选 → 响应 dict。

    `service.read_content_detail` 拼的是 **snake_case** 键（它照列名拼），
    而线上形状是 camelCase —— 转换只在这一处。
    让 service 改用 camelCase 会把"线上键名"这个**协议层概念**
    塞进业务层，且 `types.BINDING_KEY_*` 的先例说明
    "JSONB 内部键名用 camelCase"是**存储形状**的口径，不是每一处都这样。
    """
    return {
        "candidateId": str(candidate["id"]),
        "candidateIndex": candidate["candidate_index"],
        "title": candidate["title"],
        "scores": candidate["scores"],
        "keywords": candidate["keywords"],
        "highlight": candidate["highlight"],
        "riskNote": candidate["risk_note"],
    }


def _context_view(context: Any) -> dict[str, Any]:
    """`GenerationContextView` → 响应 dict（10 个 id 逐字段给，见类型 docstring）。"""
    return {
        "contextId": str(context.id),
        "projectId": str(context.project_id),
        "contentId": str(context.content_id),
        "contentVersionId": (
            None if context.content_version_id is None else str(context.content_version_id)
        ),
        "taskKind": context.task_kind,
        "promptTemplateId": (
            None if context.prompt_template_id is None else str(context.prompt_template_id)
        ),
        "promptVersionId": (
            None if context.prompt_version_id is None else str(context.prompt_version_id)
        ),
        "baselineId": None if context.baseline_id is None else str(context.baseline_id),
        "modelProfileId": (
            None if context.model_profile_id is None else str(context.model_profile_id)
        ),
        "createdAt": context.created_at.isoformat(),
    }


def _version_view(version: Any) -> dict[str, Any]:
    """`ContentVersionSummary` → 响应 dict（版本历史与采纳端点共用）。"""
    return {
        "versionId": str(version.id),
        "versionNo": version.version_no,
        "title": version.title,
        "blueprintId": (
            None if version.blueprint_id is None else str(version.blueprint_id)
        ),
        "adoptedTitleCandidateId": (
            None
            if version.adopted_title_candidate_id is None
            else str(version.adopted_title_candidate_id)
        ),
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


# ---------------------------------------------------------------------------
# 四、端点：发起生成（**主路径，202**）
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{projectId}/content-generations",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DataEnvelope[ContentGenerationAcceptedBody],
    responses=CONTENT_WRITE_RESPONSES,
    summary="发起一次内容生成（物化身份行并逐条入队，202 不同步生成）",
)
def create_content_generations(
    access: ProjectAccessDep,
    user: CurrentUserDep,
    repo: ContentRepoDep,
    job_repo: JobRepoDep,
    body: ContentGenerationBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """发起一次生成（协议 §5 无此端点，形状由本包定义）。

    顺序（不可调换）：

      1. **权限**：`EDIT`（"编辑内容"是权限矩阵里的动作，且本路径会花钱）；
      2. **构造并校验 `GenerationRequest`**（构造器归一 + fail-closed）；
      3. **物化身份行**（`service.start_generation`，与钱无关）；
      4. **判"同键不同体"**（命中既有批次时，见下）；
      5. **逐条入队**（`enqueue_job`，`content_id` 让任务粒度落在单条内容上）；
      6. 202。

    ## 为什么第 3 步在入队之前、且**不预占**

    `service.start_generation` 的 docstring：「这一层**不预占**（R2 的边界）」。
    预占发生在**执行**阶段（`execute_generation`），因为：
    ① `estimate_cost` 必须在写事务里调，而"发起"与"执行"是两次事务；
    ② 发起时还没有 `provider_id` 之外的东西需要预占，而真正花钱的是那次模型调用；
    ③ 批量请求若在端点侧整批预占，一个"只重做标题"的用户也会被按整篇预占
       —— 那正是说明文档 §1.3 否决 V1 的理由之一。

    而"建身份行"必须在**返回 202 之前**：任务要靠 `content_id` 才能在
    执行阶段找回这一行（`ClaimedJob` 里**没有** `content_id`，
    处理器只能经 `JobRepository.get_job(...)` 反查 —— 见 `handlers.py`）。

    ## 幂等键的两层，各管一件事（`types.py` 的原文口径）

    | 层 | 键 | 含版本号？ | 管什么 |
    |---|---|---|---|
    | **任务层**（本端点） | `f"{content_id}:{request_key}"` | **不含** | "同一个入队请求不要建两个任务" |
    | **模型调用层**（`service` / `gateway_bridge`） | `f"content-generate:{content_id}:v{n}:{kind}"` | **含** | "同一次生成不要买两次" |

    含版本号与否**刻意相反**：重做时版本号会前进，若任务层的键含版本号，
    重做会撞键 ⇒ **重做不生效**且用户看不出来；
    而模型层的键若不含版本号，同一版本的重放会**重复买一次**。

    任务层的键里 `content_id` 是必须的：一次请求要建 N 个任务，
    而 `jobs` 的唯一约束是 `(idempotency_scope, idempotency_key)` ——
    键里少了 `content_id`，N 条内容只会建成**一个**任务，
    而"只跑了一条、其余 4 条安静地躺着"不报任何错（§1.6 R1 的同族）。

    ## `request_key` 从哪来

    客户端给了 `Idempotency-Key` 头 ⇒ 用它（重放同一次请求会命中既有批次）；
    没给 ⇒ **本次现生成**一个（`uuid4().hex`）。后者意味着"没给键 = 不做幂等"，
    每次都是新的一批 —— 这是协议 §1.3 的通用语义，不是本包的取舍。

    ## ★ "同键不同体"必须由**端点**拒（`service.start_generation` 明写）

    `service.start_generation` 的 docstring 原文：「同键不同体是**客户端的错**，
    正确处置是让端点按 `IDEMPOTENCY_CONFLICT` 拒（本函数不做 HTTP 判断，
    只保证『命中即原样返回』这个不含歧义的行为）」。

    因此本端点做这个判断，判据是**两个独立事实的相遇**：

      · 命中的那批行的 `generation_request` 是否等于本次请求的载荷；
      · 命中的条数是否等于本次请求的 `contentCount`。

    **两者都要判**：只判载荷会漏掉"第一次 3 条、第二次改传 5 条但参数不变"
    （载荷相同、条数不同），而那时的表现是**响应里只有 3 条却返回 202**
    —— 调用方以为提交了 5 条。

    用 409 `IDEMPOTENCY_CONFLICT`（不是 422）：`platform/errors.py`
    的 `_MESSAGE_BY_CODE` 原文是"相同幂等键对应了不同的请求内容"，
    且它**不可重试**（`_RETRYABLE_BY_CODE` 只放行 429 / 503）——
    客户端该做的是换一个 key 重发，而不是重试同一个。
    """
    access.require(ProjectAction.EDIT)

    # 第 2 步：构造即归一 + 构造即校验（`TAGS` → `COPY` 在这里发生）。
    generation_request = _generation_request(
        scope=body.scope,
        batch_kind=body.batch_kind,
        model_profile_id=body.model_profile_id,
        scene_key=body.scene_key,
        form_key=body.form_key,
    )
    payload = generation_request.as_payload()
    request_key = idempotency_key or uuid.uuid4().hex

    # 第 3 步：物化身份行（与钱无关）。`created=False` = 命中既有批次。
    rows, created = service.start_generation(
        repo,
        project_id=access.project_id,
        request=generation_request,
        content_count=body.content_count,
        idempotency_key=request_key,
        created_by=user.id,
    )

    # 第 4 步：命中既有批次 ⇒ 判"同键不同体"。
    #
    # ⚠️ 只在 `created=False` 时判：新批次是我们刚按本次请求建的，
    # 与本次请求必然一致（判它等于判自己）。
    if not created:
        existing_payload = (
            dict(rows[0].generation_request) if rows[0].generation_request else None
        )
        # 场景 / 形式（接缝 §5.15）是 `content_items` 的列、不在载荷里，单独比；
        # "一边有一边没带"同样算不同体。
        if (
            existing_payload != payload
            or len(rows) != body.content_count
            or rows[0].scene_key != generation_request.scene_key
            or rows[0].form_key != generation_request.form_key
        ):
            raise ApiError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "相同幂等键对应了不同的请求内容。",
                details={
                    "reason": "idempotency_key_reused",
                    "idempotencyScope": IDEMPOTENCY_SCOPE_CONTENT_GENERATE,
                    "idempotencyKey": request_key,
                    "existingContentCount": len(rows),
                    "requestedContentCount": body.content_count,
                },
            )

    # 第 5 步：逐条入队。
    #
    # `input_hash` 对 N 条内容**相同**（参数确实相同），而 `content_id`
    # 让 idempotency_key 各不相同 —— 于是唯一约束不冲突，
    # 且"同一次请求重放"时 N 个键全部命中既有任务（`created=False`）。
    input_hash = canonical_payload_hash(
        {
            **payload,
            "contentCount": body.content_count,
            "projectId": str(access.project_id),
        }
    )
    jobs = [
        jobs_service.enqueue_job(
            job_repo,
            kind=KIND_CONTENT_GENERATE,
            queue=QUEUE_CONTENT,
            idempotency_scope=IDEMPOTENCY_SCOPE_CONTENT_GENERATE,
            idempotency_key=f"{row.id}:{request_key}",
            input_hash=input_hash,
            project_id=access.project_id,
            content_id=row.id,
        )
        for row in rows
    ]

    return {
        "data": {
            "contentCount": len(rows),
            # 归一后的 scope（回带原始值会撒谎，见响应体 docstring）。
            "scope": generation_request.scope,
            "contents": [
                {
                    "contentId": str(row.id),
                    "jobId": str(view.job_id),
                    "kind": view.kind,
                    "state": view.state,
                    "created": view.created,
                    "sceneKey": row.scene_key,
                    "formKey": row.form_key,
                }
                for row, view in zip(rows, jobs, strict=True)
            ],
        },
        "meta": build_meta(),
    }


# ---------------------------------------------------------------------------
# 五、端点：内容列表（按项目，游标分页）
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{projectId}/contents",
    response_model=PaginatedEnvelope[ContentSummaryBody],
    responses=CONTENT_READ_RESPONSES,
    summary="内容列表（按项目，新到旧，游标分页）",
)
def list_project_contents(
    access: ProjectAccessDep,
    repo: ContentRepoDep,
    params: PageParamsDep,
) -> dict[str, Any]:
    """内容列表（协议 §5 无此端点，形状由本包定义）。

    **必须取全量**（`_FULL_SCAN_LIMIT`），不能用 `params.limit * 4` 这种
    "多取一批"的写法：`platform.pagination` 是**内存**分页，
    `paginate_items` 只在交给它的那个列表里找锚点、切窗口、判 `hasMore`。
    只交截断的子集，翻到子集末尾会报 `hasMore=False` ——
    **客户端以为到底了**，而库里剩下的内容**永远不会出现**，且没有任何错误。
    （`baseline/api.py` 的 `list_baselines` 已逐条论证，同一处置。）

    **`repository.list_content_items` 用的是 `limit`/`offset`，而这里是游标** ——
    形状不一致，登记待定项（与 WP23 `prompts/api.py` / WP24 `baseline/api.py`
    同一处待办）。当前数据量可控（一个项目几十到几百条内容），
    全量取回后内存分页是可接受的；真正的修法是把锚点下推 SQL。

    **排序键 `updatedAt` 与仓储一致**：`list_content_items` 的
    `order_by(updated_at.desc(), id)`。端点在内存里再排一次要**用同一个键**——
    两者不一致的表现是**翻页重复或漏行**（不报错）。
    这里 `sort_key=lambda view: (view["updatedAt"],)` + `id_of` 取 `contentId`，
    与仓储的 `(updated_at desc, id)` 同构。

    **`total` 刻意丢弃**：协议的 `page` 块只有 `nextCursor` / `hasMore`
    （游标分页不需要总数，`PageInfo` 里没有这个字段，硬塞会被
    `response_model` 静默剥掉）。服务层仍返回它，因为数据回填与测试对账需要。
    """
    access.require(ProjectAction.VIEW)

    rows, _total = service.list_contents(
        repo, project_id=access.project_id, limit=_FULL_SCAN_LIMIT, offset=0
    )

    views = [
        {
            "contentId": str(row.id),
            "projectId": str(row.project_id),
            "title": row.title,
            "state": row.state,
            "currentVersionNo": row.current_version_no,
            "createdAt": row.created_at.isoformat(),
            "updatedAt": row.updated_at.isoformat(),
            "sceneKey": row.scene_key,
            "formKey": row.form_key,
        }
        for row in rows
    ]

    spec = CursorSpec(
        sort_fields=("updatedAt",),
        direction="desc",
        # 过滤器参与游标指纹：跨项目的游标会因指纹不符被拒
        # （而不是静默地把另一个项目的行当成续页）。
        filters={"projectId": str(access.project_id)},
    )
    window, page = paginate_items(
        views,
        params,
        spec,
        sort_key=lambda view: (view["updatedAt"],),
        id_of=lambda view: view["contentId"],
    )
    return list_response(window, page)


# ---------------------------------------------------------------------------
# 六、端点：单条内容详情（`{contentId}` 无项目路径 ⇒ 两步骤鉴权）
# ---------------------------------------------------------------------------


@router.get(
    "/contents/{contentId}",
    response_model=DataEnvelope[ContentDetailBody],
    responses=CONTENT_READ_RESPONSES,
    summary="单条内容详情（当前版本 + 蓝图 + 标题候选 + 证据绑定 + 生成上下文）",
)
def get_content_detail(
    user: CurrentUserDep,
    repo: ContentRepoDep,
    project_repo: ProjectRepoDep,
    content_id: Annotated[uuid.UUID, Path(alias="contentId")],
) -> dict[str, Any]:
    """内容详情（协议 §5 无此端点，形状由本包定义）。

    **这是 AQ-01 的核对面**：`bindings` 给出**逐段**的证据绑定
    （哪一段依据哪条 claim），而不是"整篇引用了基线"。
    如果这里只给 `blueprintId`，运营 B 的"这句话依据哪条证据"
    就只能查库 —— 而那是说明文档 §2.2 用户画像里
    "背靠背审核者"的唯一动作。

    **蓝图的两级取法在服务层**（`service.read_content_detail`）：
    当前版本有 `blueprintId` ⇒ 取它；否则取本内容**最近**的一条 ——
    这让 `STRUCTURED`（结构成功、文案未完成）这个中间态也能看到蓝图，
    而"看不到"会让前端以为结构根本没跑。端点**不重算**这个判断。

    **`contexts` 一次给全**（不分页）：一条内容的生成次数就是它的重做次数，
    量级是"几次"而不是"几百次"；而 OP-160 的影响面分析是
    **跨内容**的（`service.read_generation_contexts_by_prompt_version`），
    那一个走独立入口（本包不设端点：它服务于运维查询，
    入口归 WP14 的审计面）。
    """
    project_id = _content_project_id(repo, content_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.VIEW)

    try:
        detail = service.read_content_detail(repo, content_id=content_id)
    except service.ContentGenerationError as exc:
        # 上面刚查过存在性 ⇒ 这里几乎不可达（两次查询之间被删才会走到）。
        # **仍然翻码而不是让它冒到 500**：留一条能读懂的 404 分支，
        # 比一个"理论上不会发生"的假设更可靠。
        _raise_api_error(exc)

    return single_response(
        {
            "contentId": str(detail.id),
            "projectId": str(detail.project_id),
            "state": detail.state,
            "currentVersionId": (
                None
                if detail.current_version_id is None
                else str(detail.current_version_id)
            ),
            "currentVersionNo": detail.current_version_no,
            "payload": detail.payload,
            "blueprint": detail.blueprint,
            "titleCandidates": [
                _title_candidate_view(candidate) for candidate in detail.title_candidates
            ],
            # `bindings` 原样给：它的键名（camelCase）由 `types.BINDING_KEY_*`
            # 定死，且 `service` 落库时就是那个形状 —— 端点再"转换"一次
            # 就是同一份映射的第二处定义（ISS-66 形态）。
            "bindings": [dict(binding) for binding in detail.bindings],
            "contexts": [_context_view(context) for context in detail.contexts],
        }
    )


# ---------------------------------------------------------------------------
# 七、端点：版本历史（游标分页）
# ---------------------------------------------------------------------------


@router.get(
    "/contents/{contentId}/versions",
    response_model=PaginatedEnvelope[ContentVersionBody],
    responses=CONTENT_READ_RESPONSES,
    summary="版本历史（新版本在前，游标分页）",
)
def list_content_versions(
    user: CurrentUserDep,
    repo: ContentRepoDep,
    project_repo: ProjectRepoDep,
    params: PageParamsDep,
    content_id: Annotated[uuid.UUID, Path(alias="contentId")],
) -> dict[str, Any]:
    """版本历史（协议 §5 无此端点，形状由本包定义）。

    **`contentId` 不存在 ⇒ 404，而不是空列表**：服务层先判内容在不在
    （`service.read_version_history` 的 docstring：「空版本历史」与
    「内容根本不存在」是**两件不同的事**，而空列表让两者同形 ——
    前端会把一个拼错的内容 id 显示成"这条内容还没有版本"）。

    **已存在但一条版本都没有**（`currentVersionNo == 0`）⇒ 合法的空历史，
    返回 `{"data": [], "page": {"hasMore": false}, ...}`。

    **为什么 `payloadHash` 与 `schemaVersion` 也在这里给**：
    版本行是**不可变**的，而 `payload` 的形状由 `schema_version` 决定。
    给出这两列，前端才可能判断"我拿到的两版能不能直接对比"
    （hash 相同 = 内容未变；schema 不同 = 形状不同，不能按同一套渲染）。
    它们各是一个短字段，不给的代价是前端只能拿标题比 —— 那是猜。
    """
    project_id = _content_project_id(repo, content_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.VIEW)

    rows, _total = service.read_version_history(
        repo, content_id=content_id, limit=_FULL_SCAN_LIMIT, offset=0
    )

    views = [_version_view(row) for row in rows]

    spec = CursorSpec(
        sort_fields=("versionNo",),
        direction="desc",
        # 过滤器用 `contentId`（本端点的寻址对象）——
        # 跨内容的游标会因指纹不符被拒。
        filters={"contentId": str(content_id)},
    )
    window, page = paginate_items(
        views,
        params,
        spec,
        sort_key=lambda view: (view["versionNo"],),
        id_of=lambda view: view["versionId"],
    )
    return list_response(window, page)


# ---------------------------------------------------------------------------
# 八、端点：局部重做（**202**）
# ---------------------------------------------------------------------------


@router.post(
    "/contents/{contentId}/regenerations",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DataEnvelope[RegenerationAcceptedBody],
    responses=CONTENT_WRITE_RESPONSES,
    summary="局部重做（仅标题 / 仅正文 / 仅标签 / 整篇，202 不同步生成）",
)
def create_content_regeneration(
    user: CurrentUserDep,
    repo: ContentRepoDep,
    job_repo: JobRepoDep,
    project_repo: ProjectRepoDep,
    content_id: Annotated[uuid.UUID, Path(alias="contentId")],
    body: RegenerationBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """局部重做（协议 §5 无此端点，形状由本包定义）。

    与发起的**两处结构差异**（不是同一件事的变体）：

    | | 发起（①） | 重做（⑤） |
    |---|---|---|
    | 建身份行 | N 行 | **不建**（内容已存在） |
    | 参数 | 随 INSERT 写入 | **覆盖**（`set_generation_request`） |
    | 幂等键写在哪 | 每行的 `last_idempotency_key` | 覆盖会**改掉**那一列 |
    | 入队数 | N 个任务 | **1** 个任务 |

    参数**覆盖**而不是新增一行历史是正确的（`repository.set_generation_request`
    的 docstring：它是"最后一次意图"，不是历史事实；历史事实在每次生成
    一行的 `generation_contexts`）。

    ## 任务层的键**不含版本号**，但含 `request_key`

    `f"{content_id}:{request_key}"`。这解决了 types.py 那句
    "同一内容的重做要建新任务"：

      · 重做时客户端给一个**新的** `Idempotency-Key`（或不给 ⇒ 现生成）
        ⇒ 键不同 ⇒ 建新任务 ✓；
      · 同一次重做因超时被重发（同 `Idempotency-Key`）⇒ 键相同 ⇒
        复用既有任务 ✓（不重复买一次）。

    若键里只有 `content_id`（不含 `request_key`），第一次重做之后
    **所有**后续重做都会撞键 ⇒ 返回既有任务 ⇒ **重做不生效**，
    而响应是 202、`created=false` —— 客户端以为提交了（§1.6 R1 的同族）。

    ## `currentBlueprintId` 的取法：**最新一条蓝图**

    `service.prepare_regeneration` 的判据原文是"若这条内容从来只跑过
    『仅标题』、**没有任何蓝图行**"。因此这里问的是"这条内容历史上
    产出过蓝图吗"，落点是 `repository.latest_blueprint` ——
    与 `service.read_content_detail` 的**二级取法**同源
    （那处也是"当前版本没有 blueprintId 时取最近一条"）。

    **为什么不从 `read_content_detail` 的响应里取**：那个 `blueprint`
    块给的是 9 个**列名**，**不含 `id`**（`types.BLUEPRINT_FIELDS` 里没有
    `id` —— 它只列业务字段）。从它取不到 id，而这里要的正是 id。

    **为什么不是"当前版本的 blueprintId"**：采纳标题产生的新版本会
    **复用**上一版的 `blueprintId`（`service.adopt_title_candidate` 的
    落库规则表），而"最新一条蓝图"在那种情形下与它相同；
    两者只在"当前版本引的是更早的蓝图"时不同，而那种状态
    在当前写入路径下不产生。取"最新一条"与判据的**语义**（有没有产出过蓝图）
    直接对应，取"当前版本引的那一条"则在语义上绕了一层。
    """
    project_id = _content_project_id(repo, content_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.EDIT)

    generation_request = _generation_request(
        scope=body.scope,
        batch_kind=body.batch_kind,
        model_profile_id=body.model_profile_id,
    )

    blueprint = repo.latest_blueprint(content_item_id=content_id)
    try:
        prepared = service.prepare_regeneration(
            repo,
            content_id=content_id,
            request=generation_request,
            current_blueprint_id=None if blueprint is None else blueprint.id,
        )
    except service.ContentGenerationError as exc:
        _raise_api_error(exc)

    request_key = idempotency_key or uuid.uuid4().hex
    view = jobs_service.enqueue_job(
        job_repo,
        kind=KIND_CONTENT_GENERATE,
        queue=QUEUE_CONTENT,
        idempotency_scope=IDEMPOTENCY_SCOPE_CONTENT_GENERATE,
        idempotency_key=f"{content_id}:{request_key}",
        input_hash=canonical_payload_hash(
            {
                **generation_request.as_payload(),
                "contentId": str(content_id),
                "projectId": str(access.project_id),
            }
        ),
        project_id=access.project_id,
        content_id=content_id,
    )

    return {
        "data": {
            "contentId": str(content_id),
            # `prepared` 是服务层算出来的（scope 已归一、taskKinds 由
            # `task_kinds_for_scope` 给出）—— **不在这里重算**。
            "scope": prepared["scope"],
            "taskKinds": list(prepared["taskKinds"]),
            "job": _job_view(view),
        },
        "meta": build_meta(),
    }


# ---------------------------------------------------------------------------
# 九、端点：采纳标题候选（**201，不调模型、不计费**）
# ---------------------------------------------------------------------------


@router.post(
    "/contents/{contentId}/title-adoptions",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[ContentVersionBody],
    responses=CONTENT_WRITE_RESPONSES,
    summary="采纳一个标题候选（追加一版，不调模型不计费）",
)
def create_title_adoption(
    user: CurrentUserDep,
    repo: ContentRepoDep,
    version_repo: VersionRepoDep,
    project_repo: ProjectRepoDep,
    content_id: Annotated[uuid.UUID, Path(alias="contentId")],
    body: TitleAdoptionBody,
) -> dict[str, Any]:
    """采纳标题候选（协议 §5 无此端点，形状由本包定义，**201**）。

    ## 为什么是 201 而不是 200

    它**创建了一个新资源**（一条 `content_versions` 行）——
    `models.py` docstring 六 的口径原文：「采纳一个标题候选 ⇒
    **追加一版**（不是 UPDATE）」。因此返回体是**新版本**的摘要，
    而不是"被修改的内容"。

    200 会让前端以为"内容被改了一下"，而实际发生的是
    "内容的第 N+1 版产生了、第 N 版原样留在历史里" ——
    这个区别就是 demo 上"只重做标题"与"整篇重做"能对比的前提。

    ## 同步返回 201（不入队）

    与 ①⑤ 两条 202 不同：采纳**不调模型**（标题已在候选行里），
    因此没有异步可言 —— 它是纯数据库写（`register_version` +
    `insert_version` + `advance_projection`，同一事务）。

    ## **计费路径为空**

    `service.adopt_title_candidate` 全程不碰 `billing_bridge`：
    没有模型调用 ⇒ 没有调用次数 ⇒ 没有可预占的钱。
    这也解释了 `content_versions.generation_context_id` 为什么**可空** ——
    这一版没有 `generation_contexts` 行（造一条等于**报出一次没发生的调用**）。

    ## 406 与 409 的分工（两条跨内容不法的分工）

    `service.adopt_title_candidate` 的 docstring 原文：

      · ① 候选不存在 ⇒ `TITLE_CANDIDATE_NOT_FOUND` ⇒ **404**；
      · ② 候选存在但属于**别的内容** ⇒ `TITLE_CANDIDATE_MISMATCH` ⇒ **422**。

    ②这条 **FK 管不住**（FK 只保证"候选存在"），落点是
    `repository.candidate_owner` + `ddl.py` 触发器 3.2。
    本端点给的是**可读的 4xx**，DDL 给的是"绕过本层也拦得住"——
    两层都要，因为服务层是给人看的、DDL 是给"另一条写入路径"看的。
    """
    project_id = _content_project_id(repo, content_id)
    access = resolve_project_access(project_repo, project_id=project_id, user=user)
    access.require(ProjectAction.EDIT)

    try:
        version = service.adopt_title_candidate(
            repo,
            version_repo=version_repo,
            content_id=content_id,
            candidate_id=body.candidate_id,
            # 采纳人取自登录用户（请求体里没有这个字段，见 `TitleAdoptionBody`）。
            created_by=user.id,
        )
    except service.ContentGenerationError as exc:
        _raise_api_error(exc)

    return single_response(_version_view(version))


__all__ = [
    "CONTENT_READ_RESPONSES",
    "CONTENT_WRITE_RESPONSES",
    "MAX_CONTENT_COUNT_PER_REQUEST",
    "ContentDetailBody",
    "ContentGenerationAcceptedBody",
    "ContentGenerationBody",
    "ContentJobBody",
    "ContentSummaryBody",
    "ContentVersionBody",
    "GenerationContextBody",
    "JobAcceptedBody",
    "RegenerationAcceptedBody",
    "RegenerationBody",
    "TitleAdoptionBody",
    "TitleCandidateBody",
    "router",
]