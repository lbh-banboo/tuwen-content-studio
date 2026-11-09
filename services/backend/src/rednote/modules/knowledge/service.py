"""知识 CRUD 与版本的业务规则（WP20 的**唯一对外入口**）。

验收原文（研发计划 04 §4.3 的 WP20 行）：**归档可追溯，生效版本与旧引用并存**。

本模块回答六个问题：

| 问题 | 函数 |
|---|---|
| 怎么建一条知识、怎么加一个版本？ | `create_asset` / `add_version` |
| 怎么让它生效、归档、恢复？ | `publish` |
| 怎么读一条知识（含它的状态与可用范围）？ | `asset_view` |
| 怎么列出全员可查的知识？ | `list_assets` |
| 谁引用了这条知识？（受权限过滤） | `list_references` |
| AI 的候选怎么被采纳？ | `decide_proposal` |

---

## 一、"归档可追溯"怎么落地

这句话拆成三件必须同时成立的事：

| # | 要求 | 落点 |
|---|---|---|
| 1 | 归档**不删版本、不改旧版本** | `publish` 的 ARCHIVE 分支**只改 `archived_at`**，`knowledge_versions` 是只读的（表级不可变） |
| 2 | 归档**不篡改历史状态** | 状态由 `projection.project_versions` 从事件流**算出**，不是存在列上被就地改写 |
| 3 | "曾经归档过"**可查** | `publication_history` 返回完整事件流；`archived_between` 给出区间 |

第 2 条是本包最重要的设计决定：**若状态是一列，RESTORE 就必须把
`ARCHIVED` 改回 `ACTIVE` —— 改完之后"它曾经归档过"这件事在状态里消失了**。
那时"归档可追溯"只能靠人去翻事件表，而不是靠状态本身。

## 二、"生效版本与旧引用并存"怎么落地

这句话里的"旧引用"是**别的领域的版本（内容/方案/报告）曾经引用的
知识版本**。它与本包的三种动作都有关：

| 动作 | 对旧引用的影响 | 为什么 |
|---|---|---|
| `ACTIVATE(new)` | **无** | 旧版本仍可读，只是不再 ACTIVE。它的内容一字未改 |
| `ARCHIVE` | **无** | 归档只置 `archived_at`。按 id 直读 `GET /knowledge/assets/{id}?versionId=<旧版>` **仍返回内容** |
| `RESTORE` | 无 | 只清 `archived_at` |

**关键实现选择**：ARCHIVE **不清空 `active_version_id`**。
若清空它，`GET /knowledge/assets/{id}` 就没有"当前是什么"的答案了 ——
而规格源 §277 要求"重要事实可以从数据库精确获取"。
归档 = "不再出现在默认列表里"，而不是"内容消失"。

`list_references` 走 WP11 的 `version_dependencies`（反向），
**并按权限过滤** —— 协议 §9 原文：

> `GET /knowledge/assets/{id}/references` | 受权限过滤的引用摘要；
> **不能借此读无权项目正文**

因此本函数返回的是**摘要**（谁在什么项目引用了它、以什么用途），
**不返回引用方的正文**。并且只看**用户有权看到的项目**的引用 ——
否则这个端点会变成"探测哪些项目引用了这条知识"的信息泄漏渠道。

## 三、发布是一个事务（规格源 §277 的逐步落地）

规格源原文：

> 锁定 `knowledge_asset`；校验新版本来源、范围、字段完整性；
> 插入 `ACTIVATE` 事件，切换 `active_version`，写索引 Outbox。
> 索引尚未完成时知识状态显示"已生效／索引处理中"，
> 重要事实可以从数据库精确获取；不能因索引延迟读取错误旧事实。

逐步对应：

| 原文 | 本实现 |
|---|---|
| 锁定 knowledge_asset | `repo.get_asset_for_update`（`SELECT ... FOR UPDATE`） |
| 校验新版本来源 | `_require_asset_version` + `ddl.py` 的 `trg_knowledge_publications_asset_scope` |
| 校验范围 | `schema.scope_violation`（在 `create_asset` / `decide_proposal`） |
| 校验字段完整性 | `schema.missing_required_fields` |
| 插入 ACTIVATE 事件 | `repo.insert_publication` |
| 切换 active_version | `repo.set_active_version` |
| 写索引 Outbox | `jobs.repository.JobRepository.enqueue_event`（**复用 WP13，不改它的表**） |
| 索引处理中 | `projection.project_versions` 的 `ASSET_STATE_INDEXING` |
| 精确获取不受索引延迟影响 | `repo.list_live_assets_for_fact_lookup`（走关系索引，不走向量） |

**为什么写 Outbox 用 `JobRepository` 的方法**：`outbox_events` 由 WP12/WP13
建，写入入口只有一个（`JobRepository.enqueue_event`）。本包**不自己
`session.add(OutboxEvent(...))`** —— 那需要 import WP12 的模型，
而"往共用表写"应当有单一入口（否则 WP13 改写入约定时，
本包会静默写错形状的事件）。已登记为待定项（若 WP13 提供
独立的 Outbox 门面，本包应改用它）。

## 四、为什么"加版本"不校验必填字段，只有"创建"与"发布"校验

判据是**失败模式**：

  · `create_asset` 是入口 —— 它必须校验，否则一条空的"事实"能进库；
  · `add_version` 是"再发一版" —— 它**也要**校验（内容完整性是
    版本自身的属性，不是发布时才有的）。见 `_validate_draft`。
  · `publish` 校验的是**发布前置**（版本属于本 asset、asset 未归档），
    不重复校验字段（那些在写版本时就校验过了）。重复校验看似安全，
    实际是**两处判据迟早漂移**，而漂移后报错来自哪一处难以分辨。

## 五、`create_asset` 会同时建出版本，但**不发布**

协议 §9 的 `POST /knowledge/assets` 原文：

> 201 **草稿身份与版本**

"草稿身份与版本"意味着一次请求建出 asset 与它的第一个 version，
但**不产生生效事件** —— 那要走 `POST /publications` 的 ACTIVATE。
这个划分让"建了但还没确认"与"已生效"成为两个显式状态，
而不是靠"有没有调用过 publications"来推断。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from rednote.modules.knowledge import projection as knowledge_projection
from rednote.modules.knowledge import schema as knowledge_schema
from rednote.modules.knowledge.models import (
    KnowledgeAsset,
    KnowledgeProposal,
    KnowledgeVersion,
)
from rednote.modules.knowledge.repository import KnowledgeRepository
from rednote.modules.knowledge.types import (
    ACTION_ACTIVATE,
    ACTION_ARCHIVE,
    AGGREGATE_KNOWLEDGE,
    ASSET_STATE_DRAFT,
    ASSET_STATES,
    CATEGORIES,
    CLIENT_PUBLICATION_ACTIONS,
    DECISION_RESULT_STATE,
    EVENT_KNOWLEDGE_INDEX_REMOVED,
    EVENT_KNOWLEDGE_INDEX_REQUESTED,
    KNOWLEDGE_SCHEMA_VERSION,
    MAX_CHANGE_REASON_CHARS,
    MAX_REASON_CHARS,
    MAX_TITLE_CHARS,
    PROPOSAL_DECISIONS,
    PROPOSAL_SETTLED_STATES,
    PROPOSAL_STATES,
    SCOPES,
)
from rednote.modules.versions.models import (
    ENTITY_TYPE_KNOWLEDGE,
    VersionDependency,
    VersionNode,
)
from rednote.modules.versions.repository import VersionRepository
from rednote.modules.versions.service import register_version
from rednote.platform.errors import ApiError, ErrorCode

# 发布事件取号的重试上限。与 WP11 的 `MAX_REGISTER_ATTEMPTS` 同一条理由：
# 并发撞唯一约束时重取号，但必须有个上限，否则会变成一个无限循环。
MAX_PUBLISH_ATTEMPTS: Final[int] = 5

# 唯一约束名（撞上它说明"序号被并发抢了"，重试有意义；
# 撞上别的约束说明是程序缺陷或前置校验漏项，原样抛出）。
_UQ_PUBLICATION_SEQUENCE: Final[str] = "uq_publications_asset_sequence"


# ---------------------------------------------------------------------------
# 错误构造（与 WP18 的 `_validation_error` / `_conflict` 同形）
# ---------------------------------------------------------------------------


def _validation_error(field: str, message: str, *, reason: str, **extra: Any) -> ApiError:
    details: dict[str, Any] = {
        "fieldErrors": [{"field": field, "type": reason, "message": message}],
        "reason": reason,
    }
    details.update(extra)
    return ApiError(ErrorCode.VALIDATION_FAILED, message, details=details)


def _conflict(message: str, *, reason: str, **extra: Any) -> ApiError:
    details: dict[str, Any] = {"reason": reason}
    details.update(extra)
    return ApiError(ErrorCode.INVALID_TRANSITION, message, details=details)


def _not_found(*, resource: str, resource_id: uuid.UUID) -> ApiError:
    """不可见 / 不存在一律 404（延续 WP09/WP10/WP11 的约定）。

    403 等于告诉攻击者"这个 id 存在，只是你没权限"，
    于是可以把 UUID 空间枚举成"哪些知识真实存在"。
    """
    return ApiError(
        ErrorCode.RESOURCE_NOT_FOUND,
        f"{resource}不存在。",
        details={"resourceId": str(resource_id)},
    )


# ---------------------------------------------------------------------------
# 输入校验（纯逻辑，不查库）
# ---------------------------------------------------------------------------


def _require_category(category: str) -> str:
    if category not in CATEGORIES:
        raise _validation_error(
            "category",
            f"未知的知识类别：{category!r}。",
            reason="unknown_category",
            allowed=list(CATEGORIES),
        )
    return category


def _require_scope(scope: str) -> str:
    if scope not in SCOPES:
        raise _validation_error(
            "scope",
            f"未知的范围：{scope!r}。",
            reason="unknown_scope",
            allowed=list(SCOPES),
        )
    return scope


def _require_title(title: str) -> str:
    normalized = title.strip()
    if not normalized:
        raise _validation_error("title", "标题不能为空。", reason="title_empty")
    if len(normalized) > MAX_TITLE_CHARS:
        raise _validation_error(
            "title",
            f"标题长度不能超过 {MAX_TITLE_CHARS} 字符。",
            reason="title_too_long",
        )
    return normalized


def _require_body(body: str) -> str:
    normalized = body.strip()
    if not normalized:
        # `body` 与 `structured_payload` 是两件不同的事：
        # 后者给程序消费，前者给人读。只给结构不给正文的知识，
        # 审核人无法判断"这条知识说的是什么"。
        raise _validation_error("body", "正文不能为空。", reason="body_empty")
    return body


def _require_change_reason(reason: str) -> str:
    normalized = reason.strip()
    if not normalized:
        # 版本不可变，这份理由是一经写入就无法补充的唯一解释。
        raise _validation_error(
            "changeReason",
            "变更理由不能为空。",
            reason="change_reason_empty",
            hint="版本不可变，事后无法补写理由。",
        )
    if len(normalized) > MAX_CHANGE_REASON_CHARS:
        raise _validation_error(
            "changeReason",
            f"变更理由长度不能超过 {MAX_CHANGE_REASON_CHARS} 字符。",
            reason="change_reason_too_long",
        )
    return normalized


def validate_draft(
    *,
    category: str,
    body: str,
    structured_payload: Any,
    change_reason: str,
) -> tuple[str, dict[str, Any], str]:
    """校验并归一化一个版本草稿。返回 `(body, normalized_payload, reason)`。

    **公开（无下划线）是有意的**：端点要在**写库之前**给出 422
    （而不是让 service 在事务里炸），而两处若各写一份校验，
    判据迟早漂移。公开它让端点复用它 —— 与 WP18 的
    `service` 里那些 `_` 开头的辅助函数不同，那些只在 service 内部用。

    判据来源：规格源 02 §8 的备注「类别对应 Schema，
    **禁止无结构散文替代事实字段**」。
    """
    _require_category(category)
    clean_body = _require_body(body)
    reason = _require_change_reason(change_reason)

    try:
        payload = knowledge_schema.normalize_payload(category, structured_payload)
    except ValueError as exc:
        # `normalize_payload` 抛 ValueError（它是纯函数，不认识 ApiError）。
        # 在**边界**把它翻译成 422 —— 让纯函数保持无依赖是本包的分层要求。
        raise _validation_error(
            "structuredPayload", str(exc), reason="invalid_structured_payload"
        ) from exc

    missing = knowledge_schema.missing_required_fields(category, payload)
    if missing:
        raise _validation_error(
            "structuredPayload",
            "必填结构字段尚未填写或明确标记为不适用。",
            reason="required_fields_missing",
            missingFields=list(missing),
            hint=(
                "规格源 02 §8：「类别对应 Schema，禁止无结构散文替代事实字段」；"
                "确实不适用的字段可标记 NOT_APPLICABLE 并填写原因。"
            ),
        )
    return clean_body, payload, reason


def _require_scope_consistency(
    *,
    category: str,
    scope: str,
    brand_id: uuid.UUID | None,
    product_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
) -> None:
    """校验"范围声明"与"范围限定列"自洽（规格源 §8「事实范围必须明确」）。

    纯函数 `schema.scope_violation` 给出判据，这里把它的返回（原因字符串）
    翻译成 422。`models.py` 的 5 条 CHECK 是同一判据的数据库层。
    """
    violation = knowledge_schema.scope_violation(
        category=category,
        scope=scope,
        brand_id=brand_id,
        product_id=product_id,
        project_id=project_id,
    )
    if violation is not None:
        raise _validation_error(
            "scope", violation, reason="scope_inconsistent"
        )


def _require_dimension_exists(
    repo: KnowledgeRepository,
    *,
    brand_id: uuid.UUID | None,
    product_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
) -> None:
    """校验范围限定列指向的对象存在。

    **为什么在这里判而不是靠外键**：外键也会拦住，但它抛的是
    `IntegrityError`（500 或难读的 422）。在服务层判能给出**哪个字段**
    不存在的可读 422（`fieldErrors`）—— 这是同一个"内外两层"
    与 WP18 的 `_require_same_project` + 数据库触发器一致的手法。
    """
    if brand_id is not None and not repo.brand_exists(brand_id):
        raise _validation_error(
            "brandId", "品牌不存在。", reason="brand_not_found"
        )
    if product_id is not None and not repo.product_exists(product_id):
        raise _validation_error(
            "productId", "产品不存在。", reason="product_not_found"
        )
    if project_id is not None and not repo.project_exists(project_id):
        raise _validation_error(
            "projectId", "项目不存在。", reason="project_not_found"
        )


# ---------------------------------------------------------------------------
# 读：资产视图
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetView:
    """一条知识的完整视图（`GET /knowledge/assets/{id}` 的 data）。

    协议 §9 原文要求它给出：「内容、来源、历史状态、**可用范围**」。
    四项对应：
      · 内容 —— `body` / `structured_payload`（在 `version` 块里）
      · 来源 —— `source_segment_ids`（在 `version` 块里）
      · 历史状态 —— `publication_history` + `versions`（每个版本的投影状态）
      · 可用范围 —— `scope` + 三个限定 id，以及 `state`
    """

    asset: KnowledgeAsset
    #: 当前生效版本（`None` = 从未发布过）。
    active_version: KnowledgeVersion | None
    #: 全部版本（最新在前），带投影状态。
    versions: tuple[tuple[KnowledgeVersion, str], ...]
    #: 发布事件流（按 sequence 升序）—— **归档可追溯的落点**。
    history: tuple[dict[str, Any], ...]
    #: 投影算出的 asset 状态（`DRAFT` / `ACTIVE` / `INDEXING` / `ARCHIVED`）。
    state: str
    #: 当前生效版本的 chunk 数（0 表示索引尚未处理 —— 见 `service` 的说明）。
    active_chunk_count: int


def _project_asset(
    repo: KnowledgeRepository, *, asset: KnowledgeAsset
) -> tuple[Any, int]:
    """投影一条知识的事件流，返回 `(投影结果, 生效版本的 chunk 数)`。

    **为什么必须收敛到一个函数**：`asset_view`（读路径）与 `publish`
    （写路径）都要算状态。若两处各自传 `index_ready`，它们会**不一致** ——
    本机冒烟实测过：写入侧传 `True` 得到 `ACTIVE`，而紧接着的读回
    （按"该版本有没有 chunk"判定）得到 `INDEXING`。同一个响应里
    `data.state` 与随后 `GET` 的 `data.state` 不同，客户端会以为
    状态被并发改过 —— 而实际只是两处判据不同。

    **"哪个版本的 chunk"必须取投影结果，不能取行上的 `active_version_id`**：
    `publish` 在写回投影列**之前**调用本函数，此时行上还是**旧**生效版本。
    用旧版本查 chunk 会在换版时得到错误答案 —— 例如旧版已被 WP21 切过块
    （`chunk_count > 0` → 判成 ready → `ACTIVE`），而新版一个块都没有
    （读路径按新版判 → `INDEXING`），两处又不一致。
    以投影的 `active_version_id` 为准，写入侧与读取侧就必然同解。

    `index_ready` 的判据是**该版本已经有 chunk**。本包不生产 chunk
    （那是 WP21），因此对"刚发布、WP21 还没处理"的知识它会是 `False`
    → 显示"索引处理中"（规格源 §277 的「已生效／索引处理中」）。
    这是**正确**的显示：那一刻语义检索确实召回不到它，
    而精确读取（按 id / 按产品+类别）已经可用。
    """
    publications = repo.list_publications(asset.id)
    versions = repo.list_versions(asset.id)
    version_ids = tuple(item.id for item in versions)

    # 第一次投影：只为拿到"哪一版生效"。`index_ready=True` 让状态可直接采信。
    base = knowledge_projection.project_versions(
        [_as_event(row) for row in publications],
        version_ids=version_ids,
        index_ready=True,
    )
    effective = base.active_version_id
    chunk_count = repo.count_chunks(effective) if effective is not None else 0

    # **没有生效版本 / 没有版本时一律 ready**：那种情况下 INDEXING 这个
    # 状态无意义（`_asset_state` 会直接给出 DRAFT）。
    if effective is None or not versions or chunk_count > 0:
        return base, chunk_count

    return (
        knowledge_projection.project_versions(
            [_as_event(row) for row in publications],
            version_ids=version_ids,
            index_ready=False,
        ),
        chunk_count,
    )


def asset_view(
    repo: KnowledgeRepository, *, asset: KnowledgeAsset
) -> AssetView:
    """构造一条知识的视图（**读路径的唯一入口**）。

    **状态是从事件流算出来的**（`projection.project_versions`），
    不是读某一列 —— 这正是"归档可追溯"的实现方式（见模块 docstring 第一节）。

    索引就绪的判据见 `_project_asset`。
    """
    publications = repo.list_publications(asset.id)
    versions = repo.list_versions(asset.id)
    projected, chunk_count = _project_asset(repo, asset=asset)
    active_version_id = asset.active_version_id

    active_version = (
        repo.get_version(active_version_id) if active_version_id is not None else None
    )

    return AssetView(
        asset=asset,
        active_version=active_version,
        versions=tuple(
            (item, projected.versions[item.id].state)
            if item.id in projected.versions
            else (item, ASSET_STATE_DRAFT)
            for item in versions
        ),
        history=tuple(_publication_view(row) for row in publications),
        # **`state` 直接取投影结果** —— 不由这里再判一次归档/索引，
        # 否则"asset 状态"就有了两处定义，而两处迟早漂移。
        state=projected.state,
        active_chunk_count=chunk_count,
    )


def _as_event(row: Any) -> Any:
    """把 ORM 行转成 `projection.PublicationEvent`。

    **投影只接受纯数据**（见 `projection.py` 的说明）：它要能被
    构造出来的事件序列单测。这个转换是唯一的桥 —— 放在这里而不是
    projection 里，是为了让 `projection` 不 import 任何存储层的东西。
    """
    from rednote.modules.knowledge.types import PublicationEvent

    return PublicationEvent(
        sequence=row.sequence,
        version_id=row.version_id,
        action=row.action,
    )


def _publication_view(row: Any) -> dict[str, Any]:
    """一条发布事件的外形（协议 §9 的"保存审计"要求）。

    `sequence` 一并给出：它是**幂等与并发**的抓手 ——
    客户端可以用它做"If-Match 式"的前置校验（比见 `publish` 的说明）。
    """
    return {
        "id": str(row.id),
        "versionId": str(row.version_id),
        "action": row.action,
        "sequence": row.sequence,
        "actorId": str(row.actor_id) if row.actor_id else None,
        "reason": row.reason,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


# ---------------------------------------------------------------------------
# 写：创建知识
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreatedAsset:
    asset: KnowledgeAsset
    version: KnowledgeVersion
    revision_no: int
    etag: str


def create_asset(
    repo: KnowledgeRepository,
    *,
    category: str,
    title: str,
    scope: str,
    brand_id: uuid.UUID | None,
    product_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    body: str,
    structured_payload: Any,
    source_segment_ids: Sequence[uuid.UUID] = (),
    change_reason: str = "初始版本",
    actor_id: uuid.UUID | None = None,
) -> CreatedAsset:
    """建一条知识 + 它的第一个版本（**不发布**）。

    协议 §9 原文：`POST /knowledge/assets` → 201 **草稿身份与版本**。

    顺序（不可调换）：

      1. **纯输入校验**（类别、标题、范围取值、字段完整性）—— 不查库，早报错；
      2. **范围自洽 + 限定对象存在** —— 查库，但仍在写之前；
      3. **建 asset 行**（状态：无生效版本 = `DRAFT`）；
      4. **注册版本节点 + 写版本行**（**同事务**，数据结构 02 §5 的要求）。

    第 4 步用 WP11 的 `register_version(entity_type=ENTITY_TYPE_KNOWLEDGE, …)`，
    **不改它的签名**（接缝冻结 §5.1）。`revision_no` 用 `None`
    （新建实体，从 1 开始）。

    **为什么 `change_reason` 有默认值而 `add_version` 没有**：
    初始版本的"为什么建它"就是"它刚被建出来"，没有额外信息；
    而后续版本的 `changeReason` 是**唯一**能解释"为什么又发了一版"的地方
    （版本不可变），必须由人给出。
    """
    category = _require_category(category)
    clean_title = _require_title(title)
    scope = _require_scope(scope)

    _require_dimension_exists(
        repo, brand_id=brand_id, product_id=product_id, project_id=project_id
    )
    _require_scope_consistency(
        category=category,
        scope=scope,
        brand_id=brand_id,
        product_id=product_id,
        project_id=project_id,
    )

    clean_body, payload, clean_reason = validate_draft(
        category=category,
        body=body,
        structured_payload=structured_payload,
        change_reason=change_reason,
    )

    asset = repo.insert_asset(
        category=category,
        title=clean_title,
        scope=scope,
        brand_id=brand_id,
        product_id=product_id,
        project_id=project_id,
        created_by=actor_id,
    )
    # flush 让 asset.id 生成出来（版本行要引用它）。
    # 也让 CHECK（范围自洽的 5 条）在**这里**抛出，而不是等提交。
    repo.flush()

    registered = register_version(
        VersionRepository(repo.session),
        entity_type=ENTITY_TYPE_KNOWLEDGE,
        entity_id=asset.id,
        payload=_payload_for_hash(clean_body, payload),
        # 项目级知识归属该项目；其余（含 GLOBAL）不带项目 ——
        # 带一个不属于它的 project_id 会让版本的可见范围被误判。
        project_id=project_id,
        created_by=actor_id,
        schema_version=KNOWLEDGE_SCHEMA_VERSION,
    )
    version = repo.insert_version(
        version_id=registered.node_id,
        asset_id=asset.id,
        body=clean_body,
        structured_payload=payload,
        source_segment_ids=tuple(source_segment_ids),
        change_reason=clean_reason,
        effective_from=None,
        effective_until=None,
        created_by=actor_id,
    )
    repo.flush()
    return CreatedAsset(
        asset=asset,
        version=version,
        revision_no=registered.revision_no,
        etag=registered.etag,
    )


def add_version(
    repo: KnowledgeRepository,
    *,
    asset: KnowledgeAsset,
    body: str,
    structured_payload: Any,
    change_reason: str,
    source_segment_ids: Sequence[uuid.UUID] = (),
    effective_from: Any | None = None,
    effective_until: Any | None = None,
    actor_id: uuid.UUID | None = None,
) -> tuple[KnowledgeVersion, int, str]:
    """给已有知识加一个新版本（**不改原文**）。返回 `(版本, revision_no, etag)`。

    协议 §9 原文：`POST /knowledge/assets/{id}/versions` → **新版本，不改原文**。

    **"不改原文"是结构性的**：`knowledge_versions` 是表级不可变的
    （触发器 + ORM 守卫）。本函数只插入新行，没有修改旧行的代码路径。

    **为什么 `add_version` 也要校验字段完整性**：见模块 docstring 第四节。

    **`baseVersionId` 的取舍**（协议 §9 的 `POST /versions` 输入里有它）：
    本实现**接收但不强制**它。理由：`register_version` 的幂等判据是
    `(entity_type, entity_id, revision_no)` + 内容摘要，而 `revision_no`
    由服务端按 `max + 1` 取 —— 客户端无法预知它（OP-91：`revision_no=None`
    时注册不是幂等的）。因此 `baseVersionId` 在本包不能承担
    "并发保护的锚点"这一角色，它的作用是**记录"这一版基于哪一版"**，
    由调用方在同事务里用 `versions.link_dependency` 表达（跨包，本包不代做）。
    已在端点层校验"它必须是本 asset 的版本"，防止传一个别的知识
    的版本 id 而静默通过。
    """
    clean_body, payload, clean_reason = validate_draft(
        category=asset.category,
        body=body,
        structured_payload=structured_payload,
        change_reason=change_reason,
    )
    if asset.archived_at is not None:
        # 归档的知识不能加版本：那会让"已归档"与"有新内容"同时成立，
        # 而列表默认不显示归档知识 —— 新版本等于写进了一个看不见的地方。
        raise _conflict(
            "该知识已归档，不能新增版本。请先恢复（RESTORE）。",
            reason="asset_archived",
            assetId=str(asset.id),
        )

    registered = register_version(
        VersionRepository(repo.session),
        entity_type=ENTITY_TYPE_KNOWLEDGE,
        entity_id=asset.id,
        payload=_payload_for_hash(clean_body, payload),
        project_id=asset.project_id,
        created_by=actor_id,
        schema_version=KNOWLEDGE_SCHEMA_VERSION,
    )
    version = repo.insert_version(
        version_id=registered.node_id,
        asset_id=asset.id,
        body=clean_body,
        structured_payload=payload,
        source_segment_ids=tuple(source_segment_ids),
        change_reason=clean_reason,
        effective_from=effective_from,
        effective_until=effective_until,
        created_by=actor_id,
    )
    repo.flush()
    return version, registered.revision_no, registered.etag


def _payload_for_hash(body: str, payload: dict[str, Any]) -> dict[str, Any]:
    """参与内容摘要的内容（**正文 + 结构化载荷**）。

    **为什么不只摘要 `structured_payload`**：`body` 是人读的正文，
    它变了而结构没变（例如把"续航 18 小时"改成"约 18 小时"）
    也是一次真实的变更 —— 只摘要结构会让这种版本在
    `register_version` 的幂等判定里被当成"同一版本"。
    """
    return {"body": body, "structuredPayload": payload}


# ---------------------------------------------------------------------------
# 写：发布（ACTIVATE / ARCHIVE / RESTORE）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishResult:
    publication_id: uuid.UUID
    sequence: int
    action: str
    asset_state: str
    active_version_id: uuid.UUID | None
    created: bool


def publish(
    repo: KnowledgeRepository,
    *,
    asset: KnowledgeAsset,
    action: str,
    version_id: uuid.UUID | None,
    actor_id: uuid.UUID | None,
    reason: str | None = None,
) -> PublishResult:
    """发布事务（规格源 §277 的逐步落地，见模块 docstring 第三节）。

    **六步，顺序不可调换**：

      1. **锁定 asset**（`get_asset_for_update` —— 规格源原文第一句）；
      2. 校验动作是客户端可选的三种（`CLIENT_PUBLICATION_ACTIONS`）；
      3. 校验目标版本属于本 asset（`ACTIVATE` 必须有版本，其余可选）；
      4. 校验动作在**当前投影下**有意义（`projection.allowed_actions`）；
      5. 取事件序号 → 插事件 → 投影出新状态 → 写三个投影列；
      6. 写索引 Outbox（`ACTIVATE` 发请求事件，`ARCHIVE` 发移除事件）。

    **第 4 步的判据来自投影而不是列**：例如"已经归档了还 ARCHIVE"
    在列上看不出来（`archived_at` 非空就看得出，但"是否重复归档"
    要看事件流才能给出一致的结论）。这一条让"状态判定"只有一个来源。

    **幂等**：`ACTIVATE` 同一个版本两次**不报错**（并发重投的正常现象），
    但会**写出第二条事件** —— 这是有意的：事件表是"发生过什么"的完整记录，
    去重会让"客户端重试了几次"这件事丢失。代价是序列号会增长，
    已在说明文档里说明并登记为待定项。

    **并发**：两个发布请求各自取到同一个 `sequence` 时，
    后提交的会撞 `UQ(asset_id, sequence)`。本函数捕获后**重取号再试**
    （最多 `MAX_PUBLISH_ATTEMPTS` 次），与 WP11 的 `register_version`
    同一手法。重试必须包在 SAVEPOINT 里 —— 否则一次约束冲突会把整个
    事务标记为 aborted，后续语句全部失败（WP10 已踩过）。
    """
    if action not in CLIENT_PUBLICATION_ACTIONS:
        raise _validation_error(
            "action",
            f"不支持的动作：{action!r}。",
            reason="unsupported_publication_action",
            allowed=sorted(CLIENT_PUBLICATION_ACTIONS),
            hint=(
                "SUPERSEDE 不是客户端可选动作 —— 它是 ACTIVATE 的副作用"
                "（激活新版本时旧版本自动被取代）。"
            ),
        )

    locked = repo.get_asset_for_update(asset.id)
    if locked is None:  # pragma: no cover - 调用方刚拿到 asset，防御性
        raise _not_found(resource="知识", resource_id=asset.id)

    if action == ACTION_ACTIVATE:
        if version_id is None:
            raise _validation_error(
                "versionId", "ACTIVATE 必须指定版本。", reason="version_id_required"
            )
        _require_asset_version(
            repo, asset_id=asset.id, version_id=version_id
        )
    else:
        # ARCHIVE / RESTORE 与版本无关（它们作用于整条 asset）。
        # 若客户端传了版本，**必须**是本 asset 的版本 —— 传一个别的
        # 知识的版本说明客户端搞错了对象，静默忽略会让它以为生效了。
        if version_id is not None:
            _require_asset_version(
                repo, asset_id=asset.id, version_id=version_id
            )
        else:
            # 未指定版本时用当前生效版本（可能为 None）—— 事件表的
            # `version_id` 是非空的（规格源 02 §8 原文），因此必须有一个值。
            version_id = _require_event_version(
                repo, asset=locked, action=action
            )

    current = asset_view(repo, asset=locked)
    allowed = knowledge_projection.allowed_actions(
        _project_asset(repo, asset=locked)[0]
    )
    if action not in allowed:
        raise _conflict(
            f"当前状态下 {action} 不会改变任何东西。",
            reason="publication_no_op",
            action=action,
            assetState=current.state,
        )

    normalized_reason = _normalize_reason(reason)

    last_conflict: IntegrityError | None = None
    for _attempt in range(MAX_PUBLISH_ATTEMPTS):
        try:
            with _savepoint(repo.session):
                sequence = repo.next_publication_sequence(locked.id)
                row = repo.insert_publication(
                    asset_id=locked.id,
                    version_id=version_id,
                    action=action,
                    sequence=sequence,
                    actor_id=actor_id,
                    reason=normalized_reason,
                )
                # flush 让唯一约束在**这个 SAVEPOINT 内**抛出，
                # 从而可以被下面的 except 捕获并重试。
                repo.flush()
                break
        except IntegrityError as exc:
            if not _mentions_constraint(exc, _UQ_PUBLICATION_SEQUENCE):
                # 其它约束（外键、CHECK、跨表触发器）是程序缺陷或前置校验
                # 漏项，原样抛出 —— 把它们伪装成"并发冲突请重试"
                # 会让真 bug 长期潜伏。
                raise
            last_conflict = exc
    else:
        raise _conflict(
            f"并发发布冲突：连续 {MAX_PUBLISH_ATTEMPTS} 次都没能占到事件序号，"
            "请稍后重试。",
            reason="publication_sequence_exhausted",
            assetId=str(locked.id),
        ) from last_conflict

    # --- 第 5 步：投影并写回三个投影列（规格源原文的"切换 active_version"）
    # 投影走 `_project_asset`（与读路径同一个判据），因此本函数返回的
    # `asset_state` 与随后的 `GET` 必然一致（见 `_project_asset` 的说明）。
    projected, _chunk_count = _project_asset(repo, asset=locked)
    _apply_projection(
        repo, asset=locked, projected=projected, now=repo.now()
    )

    # --- 第 6 步：写索引 Outbox（规格源原文末句）
    _enqueue_index_event(
        repo, asset=locked, action=action, version_id=version_id, sequence=sequence
    )

    refreshed = repo.refresh_asset(locked.id)
    assert refreshed is not None  # 刚写过，必然存在
    return PublishResult(
        publication_id=row.id,
        sequence=sequence,
        action=action,
        asset_state=projected.state,
        active_version_id=refreshed.active_version_id,
        created=True,
    )


def _require_asset_version(
    repo: KnowledgeRepository, *, asset_id: uuid.UUID, version_id: uuid.UUID
) -> KnowledgeVersion:
    """目标版本必须**属于本 asset**。

    **为什么这条校验不能只靠数据库触发器**：`ddl.py` 的
    `trg_knowledge_publications_asset_scope` 会在提交时拦下它（23514），
    但那时错误来自一个延迟触发器 —— 消息里没有"哪个字段错了"，
    客户端无法据此修正输入。服务层这一道给出可读的 422。
    两道都要：这一道给人看，数据库那一道管住绕过本函数的所有写入。
    """
    version = repo.get_version(version_id)
    if version is None:
        raise _validation_error(
            "versionId", "版本不存在。", reason="version_not_found"
        )
    if version.asset_id != asset_id:
        raise _validation_error(
            "versionId",
            "该版本不属于这条知识。",
            reason="version_not_in_asset",
            assetId=str(asset_id),
            versionAssetId=str(version.asset_id),
        )
    return version


def _require_event_version(
    repo: KnowledgeRepository, *, asset: KnowledgeAsset, action: str
) -> uuid.UUID:
    """未指定版本时，为事件行挑一个版本 id。

    事件表的 `version_id` 非空（规格源 02 §8 的列定义），因此
    `ARCHIVE` / `RESTORE` 也必须带一个。规则：

      · 有当前生效版本 → 用它（事件说的就是"这个版本所在的知识归档了"）；
      · 没有（从未发布过的知识要归档）→ 用**最新的一版** ——
        它是"这条知识现在长什么样"的最近代表；
      · 一版都没有 → 不可能：`create_asset` 必然建出第一版。

    **为什么不是"随便挑一个"**：规格源没有规定这一列在
    ARCHIVE 事件里的语义，本实现选"当前生效版本"是因为它是
    那个时刻的**事实**（读数最不容易产生歧义）。取舍已登记为待定项。
    """
    if asset.active_version_id is not None:
        return asset.active_version_id
    versions = repo.list_versions(asset.id)
    if not versions:  # pragma: no cover - create_asset 必然建出第一版
        raise _conflict(
            "这条知识没有任何版本，无法记录发布事件。",
            reason="asset_has_no_version",
            assetId=str(asset.id),
        )
    return versions[0].id


def _apply_projection(
    repo: KnowledgeRepository,
    *,
    asset: KnowledgeAsset,
    projected: Any,
    now: Any,
) -> None:
    """把投影结果写回 `knowledge_assets` 的两个投影列。

    **只写"与当前值不同"的列**：`set_active_version` / `set_archived`
    各自会触发一次 `populate_existing` 读回，无谓的写入既多一次往返，
    又会让 `updated_at` 变化（让"这条知识被改过"的审计信号失真）。

    **`archived_at` 用 `now()`（事务开始时刻）**：与 `created_at` 的
    时间源一致。用 Python 侧的 `datetime.now()` 会让同一事务里的
    两个时间戳来自不同时钟（本项目统一用服务端时间）。
    """
    if asset.active_version_id != projected.active_version_id:
        repo.set_active_version(
            asset_id=asset.id, version_id=projected.active_version_id
        )
    if projected.archived and asset.archived_at is None:
        repo.set_archived(asset_id=asset.id, archived_at=now)
    elif not projected.archived and asset.archived_at is not None:
        repo.set_archived(asset_id=asset.id, archived_at=None)


def _enqueue_index_event(
    repo: KnowledgeRepository,
    *,
    asset: KnowledgeAsset,
    action: str,
    version_id: uuid.UUID,
    sequence: int,
) -> None:
    """写索引 Outbox 事件（**与业务变更同事务**）。

    | action | 事件 | 消费方该做什么 |
    |---|---|---|
    | `ACTIVATE` | `knowledge.index_requested` | 为新生效版本建 chunk 与向量 |
    | `ARCHIVE` | `knowledge.index_removed` | 从索引里移除这条知识 |
    | `RESTORE` | `knowledge.index_requested` | 重新建索引（它从归档回来了） |

    **为什么 RESTORE 也发"请求索引"而不是单列一种事件**：
    恢复之后这条知识应当重新可被召回 —— 对索引而言它与"新激活"
    是同一件事（都是"确保它在索引里"）。多列一种事件类型会让
    消费方多一个分支，而那个分支的行为与 request 完全相同。

    **为什么用 `JobRepository.insert_outbox_event`**：见模块 docstring 第三节 ——
    往共用表写应当有单一入口。`outbox_events` 是 WP13 的表，本包**不改它**
    （接缝冻结 §5），只按它的既有写入方法投一条事件。
    """
    from rednote.modules.jobs.repository import JobRepository

    event_type = (
        EVENT_KNOWLEDGE_INDEX_REMOVED
        if action == ACTION_ARCHIVE
        else EVENT_KNOWLEDGE_INDEX_REQUESTED
    )
    JobRepository(repo.session).insert_outbox_event(
        aggregate_type=AGGREGATE_KNOWLEDGE,
        aggregate_id=asset.id,
        event_type=event_type,
        payload={
            "assetId": str(asset.id),
            "versionId": str(version_id),
            "action": action,
            "sequence": sequence,
            "category": asset.category,
            "scope": asset.scope,
        },
    )


def _normalize_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    normalized = reason.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_REASON_CHARS:
        raise _validation_error(
            "reason",
            f"理由长度不能超过 {MAX_REASON_CHARS} 字符。",
            reason="reason_too_long",
        )
    return normalized


def _savepoint(session: Any) -> Any:
    """与 WP11 的 `VersionRepository.savepoint()` 同一手法。

    直接复用 `VersionRepository` 的方法：本包不重复实现一遍
    事务边界逻辑（那会让"SAVEPOINT 怎么开"有两处定义）。
    """
    return VersionRepository(session).savepoint()


def _mentions_constraint(exc: IntegrityError, name: str) -> bool:
    return name in str(getattr(exc, "orig", exc))


# ---------------------------------------------------------------------------
# 读：全员可查的知识列表
# ---------------------------------------------------------------------------


def list_assets(
    repo: KnowledgeRepository,
    *,
    category: str | None = None,
    brand_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    title_query: str | None = None,
    state: str | None = None,
    include_archived: bool = False,
) -> list[tuple[KnowledgeAsset, str]]:
    """全员可查的知识摘要列表（协议 §9 的 `GET /knowledge/assets`）。

    返回 `[(asset, 投影状态), …]`，**已按 `created_at DESC, id DESC` 排序**。

    **`state` 筛选在 Python 侧做**（其余筛选在 SQL 侧）：
    状态是投影算出来的（见 `projection.py`），无法下推到 SQL。
    取舍：对当前规模（几千条）够用；代价是"带 state 筛选时要多取
    一些行"。已在说明文档的方案取舍里写明，并登记为待定项
    （收口方向：把投影结果物化成一张只读视图或缓存列）。

    **`state` 取值非法时显式报错**：静默返回空列表会让客户端以为
    "没有这样的知识"，而实际是筛选值拼错了。
    """
    if state is not None:
        if state not in ASSET_STATES:
            raise _validation_error(
                "state",
                f"未知的状态：{state!r}。",
                reason="unknown_state",
                allowed=sorted(ASSET_STATES),
            )

    rows = repo.list_assets(
        category=category,
        brand_id=brand_id,
        product_id=product_id,
        project_id=project_id,
        title_query=title_query,
        include_archived=include_archived,
    )
    result: list[tuple[KnowledgeAsset, str]] = []
    for asset in rows:
        view = asset_view(repo, asset=asset)
        if state is not None and view.state != state:
            continue
        result.append((asset, view.state))
    return result


# ---------------------------------------------------------------------------
# 读：引用摘要（受权限过滤）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceSummary:
    """一条"谁引用了这条知识"的摘要。

    **只有摘要，没有正文** —— 协议 §9 原文：

    > `GET /knowledge/assets/{id}/references` | 受权限过滤的引用摘要；
    > **不能借此读无权项目正文**
    """

    parent_version_id: uuid.UUID
    purpose: str
    project_id: uuid.UUID | None
    selected_fragment_hash: str | None


def list_references(
    repo: KnowledgeRepository,
    *,
    version_id: uuid.UUID,
    visible_project_ids: frozenset[uuid.UUID] | None,
    is_admin: bool,
    limit: int,
) -> tuple[ReferenceSummary, ...]:
    """哪些版本引用了这个知识版本（**按可见项目过滤**）。

    `visible_project_ids` 由调用方（端点）从"当前用户的活跃项目"算出；
    `None` 表示**不做项目过滤**（只对管理者）。

    **为什么过滤必须在服务层而不是端点**：这是一个"安全边界"，
    它的判据（`parent.project_id` 是否在可见集合里）必须与
    "平台级版本（`project_id IS NULL`）如何处理"这条规则一起看：

      · `project_id IS NULL` 的引用方是**平台级版本**（如 Prompt 模板）——
        平台级版本只对管理者可见（WP11 的 `can_view_version` 规则）。
        因此非管理者**看不到**这一类引用；
      · 有项目的引用方，只有其项目在可见集合里才看得到。

    把这两条写在一处（而不是端点里一个 `if`），是为了让"引用可见性"
    与 WP11 的版本可见性规则**用同一个口径** —— 否则
    `GET /versions/{id}` 看不到的东西，会从这个端点漏出去。
    """
    if limit <= 0:
        return ()

    edges = list(
        repo.session.execute(
            select(VersionDependency)
            .where(VersionDependency.dependency_version_id == version_id)
            .order_by(
                VersionDependency.parent_version_id.asc(),
                VersionDependency.purpose.asc(),
            )
        )
        .scalars()
        .all()
    )
    if not edges:
        return ()

    # 取引用方节点的项目归属 —— 一次查询，不是逐条查
    # （逐条查在"被引用很多次"时会变成 N+1，而那正是影响分析最热的场景）。
    parent_ids = [edge.parent_version_id for edge in edges]
    nodes = {
        row.id: row
        for row in repo.session.execute(
            select(VersionNode).where(VersionNode.id.in_(parent_ids))
        )
        .scalars()
        .all()
    }

    summaries: list[ReferenceSummary] = []
    for edge in edges:
        node = nodes.get(edge.parent_version_id)
        project_id = node.project_id if node is not None else None
        if not _reference_visible(
            project_id=project_id,
            visible_project_ids=visible_project_ids,
            is_admin=is_admin,
        ):
            continue
        summaries.append(
            ReferenceSummary(
                parent_version_id=edge.parent_version_id,
                purpose=edge.purpose,
                project_id=project_id,
                selected_fragment_hash=edge.selected_fragment_hash,
            )
        )
        if len(summaries) >= limit:
            break
    return tuple(summaries)


def _reference_visible(
    *,
    project_id: uuid.UUID | None,
    visible_project_ids: frozenset[uuid.UUID] | None,
    is_admin: bool,
) -> bool:
    """纯函数：一条引用对该用户是否可见（与 WP11 的可见性规则同口径）。"""
    if is_admin:
        return True
    if project_id is None:
        # 平台级版本只对管理者可见（WP11 的 `can_view_version` 规则）。
        return False
    if visible_project_ids is None:
        # 未提供可见集合 = 只对管理者放开（调用方没算，说明不该放开）。
        return False
    return project_id in visible_project_ids


# ---------------------------------------------------------------------------
# 写：提案决策
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalDecisionResult:
    proposal: KnowledgeProposal
    state: str
    #: ACCEPT 时生成的新版本 id（REJECT 时为 None）。
    created_version_id: uuid.UUID | None
    #: ACCEPT 时是否同时产生了生效事件。
    activated: bool


def decide_proposal(
    repo: KnowledgeRepository,
    *,
    proposal: KnowledgeProposal,
    decision: str,
    scope: str | None,
    reason: str,
    actor_id: uuid.UUID,
) -> ProposalDecisionResult:
    """处置一条 AI 候选（协议 §9：`decision ACCEPT/REJECT,scope,reason`）。

    协议 §9 的约束原文：「**接受生成明确版本与生效事件**」。

    因此 ACCEPT 在本实现里做三件事（同一事务）：

      1. 用 `proposed_payload` 建出一个**明确版本**（走 `validate_draft`
         的完整校验 —— 候选不能绕过字段完整性）；
      2. 在**有目标 asset** 时，写一条 `ACTIVATE` 生效事件（发布事务的
         全部步骤，复用 `publish`）；
      3. 把提案标为 ACCEPTED。

    三件事缺一不可 —— 少了第 1 件就只是"标记了一下"，
    少了第 2 件则"接受了但没生效"（候选答应了却不落地）。

    **无目标 asset 的候选（建议新建知识）怎么办**：ACCEPT 时**建一条新的
    知识**（`create_asset`），但**不自动发布** —— 因为"新建一条知识"
    与"让它生效"是两件事：新建出来的东西应当先被看一遍。
    这个不对称是有意的，已登记为待定项。

    **`scope` 可在决策时覆盖**：AI 提的范围是建议，确认人有权改窄。
    覆盖后要重新做范围校验（`_require_scope_consistency`）——
    否则"ACCEPT 并改成 GLOBAL"会让 `models.py` 的 CHECK 在提交时才炸。

    **REJECT 必须有理由**：`models.py` 的 CHECK 也保证这一点
    （`reject_needs_reason`）。一条被拒的 AI 建议若无理由，
    下一个看到它的人只能重走一遍判断。
    """
    if decision not in PROPOSAL_DECISIONS:
        raise _validation_error(
            "decision",
            f"未知的决策：{decision!r}。",
            reason="unknown_decision",
            allowed=list(PROPOSAL_DECISIONS),
        )

    locked = repo.get_proposal_for_update(proposal.id)
    if locked is None:  # pragma: no cover - 调用方刚拿到提案，防御性
        raise _not_found(resource="候选", resource_id=proposal.id)

    if locked.state in PROPOSAL_SETTLED_STATES:
        # 已处置的候选不能再处置第二次 —— 那会覆盖第一次的确认人与理由，
        # 而"谁确认的"正是审计要回答的问题。
        raise _conflict(
            "该候选已被处置，不能重复决策。",
            reason="proposal_already_settled",
            proposalState=locked.state,
        )

    normalized_reason = reason.strip()
    if decision == "REJECT":
        if not normalized_reason:
            raise _validation_error(
                "reason",
                "拒绝候选必须填写理由。",
                reason="reject_needs_reason",
            )
    elif len(normalized_reason) > MAX_REASON_CHARS:
        raise _validation_error(
            "reason",
            f"理由长度不能超过 {MAX_REASON_CHARS} 字符。",
            reason="reason_too_long",
        )

    target_state = DECISION_RESULT_STATE[decision]
    created_version_id: uuid.UUID | None = None
    activated = False

    if decision == "ACCEPT":
        created_version_id, activated = _accept_proposal(
            repo, proposal=locked, scope=scope, actor_id=actor_id
        )

    repo.settle_proposal(
        proposal_id=locked.id,
        state=target_state,
        confirmed_by=actor_id,
        decision_reason=normalized_reason or None,
        decided_at=repo.now(),
    )
    repo.flush()

    refreshed = repo.get_proposal(locked.id)
    assert refreshed is not None  # 刚写过，必然存在
    return ProposalDecisionResult(
        proposal=refreshed,
        state=target_state,
        created_version_id=created_version_id,
        activated=activated,
    )


def _accept_proposal(
    repo: KnowledgeRepository,
    *,
    proposal: KnowledgeProposal,
    scope: str | None,
    actor_id: uuid.UUID,
) -> tuple[uuid.UUID, bool]:
    """采纳一条候选：生成明确版本（+ 生效事件）。返回 `(版本 id, 是否生效)`。"""
    effective_scope = _require_scope(scope) if scope is not None else proposal.scope
    payload = proposal.proposed_payload

    if proposal.target_asset_id is not None:
        # --- 有目标：在既有知识上追加一版并激活 ---
        asset = repo.get_asset_for_update(proposal.target_asset_id)
        if asset is None:
            raise _validation_error(
                "targetAssetId",
                "候选指向的知识不存在。",
                reason="target_asset_not_found",
            )
        _require_scope_consistency(
            category=asset.category,
            scope=effective_scope,
            brand_id=asset.brand_id,
            product_id=asset.product_id,
            project_id=asset.project_id,
        )
        body, structured, change_reason = _proposal_content(
            payload, default_reason="采纳 AI 候选"
        )
        version, _revision_no, _etag = add_version(
            repo,
            asset=asset,
            body=body,
            structured_payload=structured,
            change_reason=change_reason,
            actor_id=actor_id,
        )
        # 协议原文："接受生成明确版本**与生效事件**"。
        publish(
            repo,
            asset=asset,
            action=ACTION_ACTIVATE,
            version_id=version.id,
            actor_id=actor_id,
            reason=f"采纳候选 {proposal.id}",
        )
        return version.id, True

    # --- 无目标：建一条新知识（**不自动发布**，见 `decide_proposal` 的说明）
    category = payload.get("category")
    title = payload.get("title")
    if not isinstance(category, str) or not isinstance(title, str):
        raise _validation_error(
            "proposedPayload",
            "新建类候选必须在 proposedPayload 里给出 category 与 title。",
            reason="proposal_missing_identity",
        )
    _require_scope(effective_scope)
    _require_scope_consistency(
        category=category,
        scope=effective_scope,
        brand_id=payload.get("brandId"),
        product_id=payload.get("productId"),
        project_id=payload.get("projectId") or proposal.project_id,
    )
    body, structured, change_reason = _proposal_content(
        payload, default_reason="采纳 AI 候选"
    )
    created = create_asset(
        repo,
        category=category,
        title=title,
        scope=effective_scope,
        brand_id=payload.get("brandId"),
        product_id=payload.get("productId"),
        project_id=payload.get("projectId") or proposal.project_id,
        body=body,
        structured_payload=structured,
        change_reason=change_reason,
        actor_id=actor_id,
    )
    return created.version.id, False


def _proposal_content(
    payload: dict[str, Any], *, default_reason: str
) -> tuple[str, Any, str]:
    """从候选载荷里取 (body, structured_payload, change_reason)。

    **候选可以只给 `structuredPayload`**（AI 提取的往往是结构化的字段），
    此时 `body` 用一句生成的说明 —— 但**不编造事实**：
    它只是把结构化字段的 label 列出来，让审核人看到"这一版有哪些内容"。
    规格源 02 §8 的备注「禁止无结构散文替代事实字段」防的是
    "只有散文、没有结构"，而这里是反过来（只有结构、散文是生成的摘要），
    不违反那条判据。
    """
    body = payload.get("body")
    if not isinstance(body, str) or not body.strip():
        structured_raw = payload.get("structuredPayload")
        labels = _labels_of(structured_raw, payload.get("category"))
        body = "（由 AI 候选生成）" + "、".join(labels) if labels else "（由 AI 候选生成）"
    change_reason = payload.get("changeReason")
    if not isinstance(change_reason, str) or not change_reason.strip():
        change_reason = default_reason
    return body, payload.get("structuredPayload"), change_reason


def _labels_of(structured_raw: Any, category: Any) -> list[str]:
    """把非空的结构化字段列成中文标签（给生成的正文明细用）。"""
    labels: list[str] = []
    if not isinstance(structured_raw, dict) or not isinstance(category, str):
        return labels
    for path, entry in structured_raw.items():
        spec = knowledge_schema.FIELD_BY_PATH.get(path)
        if spec is None:
            continue
        if isinstance(entry, dict) and knowledge_schema.has_value(entry.get("value")):
            labels.append(spec.label)
    return labels


# ---------------------------------------------------------------------------
# 读：提案列表
# ---------------------------------------------------------------------------


def list_proposals(
    repo: KnowledgeRepository,
    *,
    state: str | None = None,
    project_id: uuid.UUID | None = None,
) -> list[KnowledgeProposal]:
    """AI 候选变更列表（协议 §9 的 `GET /knowledge/proposals`）。

    **产生候选的是 WP23 / WP41**（依赖反馈与实验），本包只做列表与决策。
    判据：候选的**产生**需要一整套策略（从哪些反馈里提、提什么、
    去重与阈值），那是另一个包的设计空间；而**决策**是标准状态机
    （见 §5 的划界判断表）。
    """
    if state is not None:
        if state not in PROPOSAL_STATES:
            raise _validation_error(
                "state",
                f"未知的候选状态：{state!r}。",
                reason="unknown_state",
                allowed=sorted(PROPOSAL_STATES),
            )
    return repo.list_proposals(state=state, project_id=project_id)


# ---------------------------------------------------------------------------
# 读：版本历史（协议 §9 的 `GET /knowledge/assets/{id}/versions`）
# ---------------------------------------------------------------------------


def list_version_views(
    repo: KnowledgeRepository, *, asset: KnowledgeAsset
) -> tuple[tuple[KnowledgeVersion, str], ...]:
    """全部历史版本 + 各自的投影状态（最新在前）。

    **为什么不直接返回 `repo.list_versions`**：客户端要区分
    "这一版是当前生效的"与"这一版已被取代" —— 那个信息不在
    `knowledge_versions` 里（它不可变，没有状态列），
    而在事件流的投影里。这正是"生效版本与旧引用并存"的展示面。
    """
    return asset_view(repo, asset=asset).versions


__all__ = [
    "MAX_PUBLISH_ATTEMPTS",
    "AssetView",
    "CreatedAsset",
    "ProposalDecisionResult",
    "PublishResult",
    "ReferenceSummary",
    "add_version",
    "asset_view",
    "create_asset",
    "decide_proposal",
    "list_assets",
    "list_proposals",
    "list_references",
    "list_version_views",
    "publish",
    "validate_draft",
]