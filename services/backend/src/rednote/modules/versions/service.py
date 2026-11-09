"""版本注册、依赖登记与草稿升格（WP11）。

本模块是 WP11 的**唯一对外入口** —— 下游模块（WP18 Brief / WP20 知识 /
WP21 方案 / WP29 内容 / WP13 任务 / WP34 影响图）只调这里的函数，
不直接碰 `repository.py`，更不直接写表。

**为什么把"注册版本"收敛成一个函数**：
唯一性、并发重试、哈希规范化、跨项目校验这四件事，任何一件交给调用方做
都会有人做漏。例如"先 `max_revision_no()` 再 +1"看起来简单，
但两个请求同时读到 N，就会都去插 N+1 —— 而失败方拿到的是一个
`IntegrityError`，它甚至不知道该重试还是该报错。
把这件事收进来之后，调用方只需回答一个问题："这是哪一版的内容？"

**三层校验的分工**（本包的核心设计）：

| 层 | 管什么 | 为什么在这一层 |
|---|---|---|
| service（本文件） | 自引用 / 跨项目 / 成环 / 实体类型合法 | 能给出可读的 422 与 `fieldErrors`；判定所需的数据在**同一事务内**读到 |
| ORM 事件（`platform/immutability.py`） | 拒绝 UPDATE / DELETE | 开发期就把"改历史版本"这类错误暴露出来 |
| 数据库（迁移里的触发器 + CHECK + 唯一约束） | 兜住所有绕过 ORM 的写入 | 手工 SQL、数据回填、将来的运维工具都没有代码评审 |

**关于"草稿 ETag"**（本包验收项之一）：
WP11 **不持有草稿表** —— 草稿归各领域模块（`brief_drafts` / `content_drafts` /
`plan_drafts`），因为它们各自有领域专属字段。WP11 负责的是**升格这一刻的
语义切换**：

  · 草稿阶段：资源可变，ETag = `build_etag(draft_id, lock_version)`，
    每次保存递增；写请求必须带 `If-Match`（`require_if_match`）。
  · 调用 `promote_draft()` 之后：产生一个不可变版本节点，
    ETag = `build_immutable_etag(node_id, payload_hash)` —— **此后永不变化**。

因此 `promote_draft()` 的返回值里带 `etag` 字段：调用方固化版本后
直接把新 ETag 回给前端，前端下一次读取就能看到"这个 ETag 不再变"。
这个"不变"不是承诺，是数据库层面的既成事实（触发器守着）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy.exc import IntegrityError

from rednote.modules.identity.dependencies import CurrentUser
from rednote.modules.versions.ddl import (
    DEPENDENCY_SCOPE_SQLSTATE,
    DEPENDENCY_SCOPE_TRIGGER,
)
from rednote.modules.versions.models import (
    ENTITY_TYPE_LIST_SQL,
    ENTITY_TYPES,
    MAX_REVISION_NO,
    MIN_SCHEMA_VERSION,
    PAYLOAD_HASH_LENGTH,
    VersionDependency,
    VersionNode,
)
from rednote.modules.versions.permissions import (
    require_version_visible,
    resolve_version_visibility,
)
from rednote.modules.versions.repository import VersionRepository
from rednote.platform.errors import ApiError, ErrorCode

# 并发注册同一修订号时的重试次数。
#
# 取 5 的理由：每次重试都意味着"有另一个事务刚插入了更大的修订号"。
# 连续 5 次都撞上，说明写入压力极大或存在异常循环；
# 继续重试只会把请求线程占住（此时失败比拖慢更负责）。
MAX_REGISTER_ATTEMPTS: Final[int] = 5

# 约束名（与 models.py / 迁移里的一致）。改名必须同步改这里，
# 因为判定"这次 IntegrityError 是不是我们能处理的那种"依赖它。
_UQ_VERSION_NODE: Final[str] = "uq_version_nodes_entity_type_entity_id_revision_no"
_PK_VERSION_DEPENDENCY: Final[str] = "pk_version_dependencies"
_CK_NO_SELF_REFERENCE: Final[str] = "ck_version_dependencies_no_self_reference"


# ---------------------------------------------------------------------------
# 返回值
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegisteredVersion:
    """一次注册的结果。

    `created=False` 表示幂等命中：同一 `(entity_type, entity_id, revision_no)`
    已经存在且内容摘要一致。**这不是错误** —— 网络重试、消息重复投递都会
    导致同一请求到达两次，把它当错误处理会迫使每个调用方自己写去重。
    """

    node_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    project_id: uuid.UUID | None
    revision_no: int
    payload_hash: str
    etag: str
    created: bool


@dataclass(frozen=True)
class DependencyLink:
    """一次依赖登记的结果。`created=False` 表示这条边已存在（幂等命中）。"""

    parent_version_id: uuid.UUID
    dependency_version_id: uuid.UUID
    purpose: str
    created: bool


@dataclass(frozen=True)
class VersionPage:
    """一页版本历史（无 HTTP 端点，故不直接产出协议 §1 的 page 块）。"""

    items: tuple[VersionNode, ...]
    has_more: bool
    # 下一页的锚点 `(revision_no, id)`；`has_more` 为 False 时是 None。
    next_anchor: tuple[int, uuid.UUID] | None


# ---------------------------------------------------------------------------
# 内容摘要
# ---------------------------------------------------------------------------


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    """把 payload 规范化后取 SHA-256。

    **规范化必须固定，否则幂等判定会失效**：同一个逻辑版本，
    若第一次传 `{"a":1,"b":2}`、重试时传 `{"b":2,"a":1}`，
    不做键排序会算出两个不同摘要，于是"重复请求"被当成"内容变了"而被拒。

    三项规范化：
      · `sort_keys=True` —— 消除键顺序差异；
      · `separators=(",", ":")` —— 消除空格差异（`json.dumps` 默认带空格）；
      · `default=str` —— 容忍 UUID / datetime 等非 JSON 原生类型
        （不这样做会直接抛 `TypeError`，让"能不能注册"取决于 payload 里
        恰好有没有这些类型）。
    `ensure_ascii=False` 让中文按 UTF-8 编码后再哈希，摘要长度与可读性都更好。

    **已知边界**：浮点数的文本表示依赖 `repr`，跨语言/跨运行时的
    payload 生成方可能给出 `1.0` 与 `1` 这样语义相同但摘要不同的值。
    已登记为待定项（规范口径由协议层统一）。
    """
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fragment_hash(fragment: str) -> str:
    """片段摘要（`version_dependencies.selected_fragment_hash`）。

    与 payload 摘要分开：片段是纯文本，不需要 JSON 规范化。
    """
    return hashlib.sha256(fragment.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 错误构造
# ---------------------------------------------------------------------------


def _validation_error(field: str, message: str, *, reason: str, **extra: Any) -> ApiError:
    """构造 422 `VALIDATION_FAILED`，带可定位的 `fieldErrors`。

    协议 §1.2 的 422 语义是"定位字段／规则问题"，因此
    `fieldErrors` 用字段名而不是自由文本描述 —— 前端按字段高亮输入框，
    自由文本它没法用。
    """
    details: dict[str, Any] = {
        "fieldErrors": [{"field": field, "type": reason, "message": message}],
        "reason": reason,
    }
    details.update(extra)
    return ApiError(ErrorCode.VALIDATION_FAILED, message, details=details)


def _constraint_name(exc: IntegrityError) -> str | None:
    """从 psycopg 的异常里取约束名。取不到返回 None（不抛）。"""
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", None)


def _mentions_constraint(exc: IntegrityError, name: str) -> bool:
    """这个 IntegrityError 是不是某个约束引起的。

    **为什么既看 `diag.constraint_name` 又看错误文本**：
    `diag` 在 psycopg3 下通常可用，但若异常来自别的驱动/被包装过就会缺失。
    文本兜底让判定退化为"字符串包含"，虽然粗糙，但好过**静默把
    能处理的冲突当成程序缺陷抛出去**（那会变成 500）。
    """
    if _constraint_name(exc) == name:
        return True
    return name in str(getattr(exc, "orig", exc))


def _is_dependency_scope_violation(exc: IntegrityError) -> bool:
    """是不是"依赖边两端不在同一项目"这条不变量被触发。"""
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return sqlstate == DEPENDENCY_SCOPE_SQLSTATE


# ---------------------------------------------------------------------------
# 前置校验
# ---------------------------------------------------------------------------


def _require_entity_type(entity_type: str) -> None:
    if entity_type not in ENTITY_TYPES:
        raise _validation_error(
            "entityType",
            f"未知的版本实体类型；允许值：{ENTITY_TYPE_LIST_SQL}",
            reason="entity_type_unknown",
            allowedEntityTypes=sorted(ENTITY_TYPES),
        )


def _require_schema_version(schema_version: int) -> None:
    if schema_version < MIN_SCHEMA_VERSION:
        raise _validation_error(
            "schemaVersion",
            f"schemaVersion 不能小于 {MIN_SCHEMA_VERSION}。",
            reason="schema_version_too_small",
        )


def require_version_node(
    repo: VersionRepository, version_id: uuid.UUID, *, field: str
) -> VersionNode:
    """取版本节点，取不到就报 422（而不是 404）。

    **为什么引用一个不存在的版本是 422 而不是 404**：
    这个 id 出现在**请求体里**，它是一个"被引用的值"，不是一个被寻址的资源。
    协议 §1.2 的 404 用于"你要访问的对象不存在/你无权知道它存在"，
    而这里的失败原因是"你给的字段值无效" —— 客户端该做的是修正字段，
    不是提示"页面不存在"。用 `fieldErrors` 指明是哪个字段。
    """
    node = repo.get_node(version_id)
    if node is None:
        raise _validation_error(
            field,
            "引用的版本不存在。",
            reason="version_not_found",
        )
    return node


def _require_same_project(
    parent: VersionNode, dependency: VersionNode
) -> None:
    """跨项目版本引用拒绝 —— WP11 的验收项之一。

    规则：两端都有项目归属时必须一致；任一端为 `NULL`（平台级版本）
    视为"无项目范围"，不受此限制。

    **为什么"平台级"要豁免**：平台级版本（如 Prompt 模板版本）不属于任何项目，
    它本来就没有"项目"这个属性，谈不上跨项目。若强行要求它有项目，
    就得给一个虚假的项目 id，反而制造出错误的数据。

    **这条规则在数据库层也有一道**（`version_dependencies` 的 BEFORE INSERT
    触发器）。两道都要：这一道给出可读的 422 与两个项目的 id 便于排障；
    数据库那一道管住绕过本函数的所有写入。
    """
    if parent.project_id is None or dependency.project_id is None:
        return
    if parent.project_id != dependency.project_id:
        raise _validation_error(
            "dependencyVersionId",
            "不能引用其他项目的版本。",
            reason="cross_project_reference",
            parentProjectId=str(parent.project_id),
            dependencyProjectId=str(dependency.project_id),
        )


def _require_no_cycle(
    repo: VersionRepository, parent: VersionNode, dependency: VersionNode
) -> None:
    """禁止依赖成环。

    要新增 `parent → dependency` 这条边时，环的条件是
    **从 `dependency` 出发已经能到达 `parent`**：
    那样加上新边后，parent 能到 dependency、dependency 也能到 parent。

    为什么一定要禁环：下游的溯源遍历（影响图、来源链）都会沿依赖方向走。
    有环时"这条内容的来源"就变成一个无限循环，
    轻则页面转圈，重则服务端遍历挂死。

    **这个判定必须对任意形状的图都成立，尤其是"已经有环"的图** ——
    那恰恰是它要处理的情况。所以它不能设深度上限：上限之外若藏着环，
    就会返回"不可达"从而放行成环的边，而那正是要防的后果。
    终止性由 `dependency_reaches` 内部的访问集合去重保证
    （理由与实测见 `VersionRepository.dependency_reaches`）。
    """
    if repo.dependency_reaches(
        start_version_id=dependency.id,
        target_version_id=parent.id,
    ):
        raise _validation_error(
            "dependencyVersionId",
            "该引用会让依赖成环。",
            reason="dependency_cycle",
        )


# ---------------------------------------------------------------------------
# 注册版本
# ---------------------------------------------------------------------------


def _as_registered(node: VersionNode, *, created: bool) -> RegisteredVersion:
    """把 ORM 节点转成不可变返回值。

    **为什么不直接返回 ORM 对象**：返回值会被下游持有到事务之外，
    而 ORM 对象绑定在 session 上（session 关闭后访问属性抛
    `DetachedInstanceError`）。转成 frozen dataclass 后可以自由持有。
    """
    return RegisteredVersion(
        node_id=node.id,
        entity_type=node.entity_type,
        entity_id=node.entity_id,
        project_id=node.project_id,
        revision_no=node.revision_no,
        payload_hash=node.payload_hash,
        etag=node.etag,
        created=created,
    )


def register_version(
    repo: VersionRepository,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    payload: Mapping[str, Any],
    project_id: uuid.UUID | None = None,
    created_by: uuid.UUID | None = None,
    schema_version: int = MIN_SCHEMA_VERSION,
    revision_no: int | None = None,
) -> RegisteredVersion:
    """注册一个版本节点。

    `revision_no=None` 表示"追加到该实体末尾"（自动取 `max + 1`）。
    显式传值用于数据回填场景 —— 那时必须精确落到指定修订号上。

    **并发语义**：两个请求同时追加时，后到的会撞
    `uq_version_nodes_entity_type_entity_id_revision_no`，
    本函数捕获后**重新取号再试**（最多 `MAX_REGISTER_ATTEMPTS` 次）。
    重试必须包在 SAVEPOINT 里 —— 否则一次约束冲突会把整个事务标记为
    aborted，后续语句全部失败（WP10 已踩过这个坑）。

    **幂等语义**：同一修订号重复注册时，
      · 内容摘要一致 → 返回既有节点，`created=False`；
      · 内容摘要不同 → 409 `INVALID_TRANSITION`。
        因为修订号一旦占用就不可改写（版本不可变），
        "同一修订号装不同内容"只能是调用方搞错了版本号，
        静默覆盖会让已引用该版本的上下游全部指向错误内容。
    """
    _require_entity_type(entity_type)
    _require_schema_version(schema_version)

    if project_id is not None and not repo.project_exists(project_id):
        raise _validation_error(
            "projectId", "项目不存在。", reason="project_not_found"
        )
    if created_by is not None and not repo.user_exists(created_by):
        raise _validation_error(
            "createdBy", "操作人不存在。", reason="user_not_found"
        )

    payload_hash = canonical_payload_hash(payload)
    if len(payload_hash) != PAYLOAD_HASH_LENGTH:  # pragma: no cover - 防御性
        raise RuntimeError("payload 摘要长度异常，检查哈希实现")

    pending_revision = revision_no
    last_conflict: IntegrityError | None = None

    for _attempt in range(MAX_REGISTER_ATTEMPTS):
        try:
            with repo.savepoint():
                target_revision = (
                    pending_revision
                    if pending_revision is not None
                    else repo.max_revision_no(
                        entity_type=entity_type, entity_id=entity_id
                    )
                    + 1
                )

                if target_revision < 1 or target_revision > MAX_REVISION_NO:
                    raise _validation_error(
                        "revisionNo",
                        f"修订号超出范围（1 ~ {MAX_REVISION_NO}）。",
                        reason="revision_no_out_of_range",
                    )

                existing = repo.find_node(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    revision_no=target_revision,
                )
                if existing is not None:
                    if existing.payload_hash != payload_hash:
                        raise ApiError(
                            ErrorCode.INVALID_TRANSITION,
                            f"{entity_type} 的第 {target_revision} 版已存在且内容不同，"
                            "版本不可改写，请改用新的修订号。",
                            details={
                                "reason": "revision_payload_mismatch",
                                "entityType": entity_type,
                                "entityId": str(entity_id),
                                "revisionNo": target_revision,
                                "existingEtag": existing.etag,
                            },
                        )
                    return _as_registered(existing, created=False)

                node = repo.insert_node(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    project_id=project_id,
                    revision_no=target_revision,
                    schema_version=schema_version,
                    payload_hash=payload_hash,
                    created_by=created_by,
                )
                # flush 让唯一约束在**这个 SAVEPOINT 内**抛出，
                # 从而可以被下面的 except 捕获并重试。
                repo.flush()
                # 只取标量。即便这行之后抛异常，也不会再碰 ORM 对象
                # （session 内的实例会被 expire，读取会触发惰性加载 →
                #  在 aborted 事务上抛 PendingRollbackError，把业务问题升级成 500）。
                return _as_registered(node, created=True)

        except IntegrityError as exc:
            if not _mentions_constraint(exc, _UQ_VERSION_NODE):
                # 其它约束（外键、CHECK）是程序缺陷或前置校验漏项，
                # 原样抛出 —— 把它们伪装成"并发冲突请重试"会让真 bug 长期潜伏。
                raise
            last_conflict = exc
            # 下一轮重新取号：冲突说明有人抢先占了这个号。
            pending_revision = None

    raise ApiError(
        ErrorCode.INVALID_TRANSITION,
        f"并发注册冲突：连续 {MAX_REGISTER_ATTEMPTS} 次都没能占到修订号，请稍后重试。",
        details={
            "reason": "revision_allocation_exhausted",
            "entityType": entity_type,
            "entityId": str(entity_id),
        },
    ) from last_conflict


# ---------------------------------------------------------------------------
# 草稿升格
# ---------------------------------------------------------------------------


def promote_draft(
    repo: VersionRepository,
    *,
    base_version_id: uuid.UUID,
    payload: Mapping[str, Any],
    created_by: uuid.UUID | None = None,
    schema_version: int = MIN_SCHEMA_VERSION,
    project_id: uuid.UUID | None = None,
) -> RegisteredVersion:
    """把一份草稿固化成新的不可变版本。

    这是"**草稿 ETag → 版本 ETag**"的语义切换点（本包验收项之一）：

      · 入参 `base_version_id` 是草稿的 `base_version_id`，
        即"我这次编辑是从哪一版开始的"。调用方在草稿阶段用
        `If-Match: build_etag(draft_id, lock_version)` 防并发覆盖。
      · 出参 `RegisteredVersion.etag` 是**不可变 ETag**，
        此后无论过多久、谁去读，它都一样。

    **为什么必须传 `base_version_id` 而不是让服务端猜**：
    "从哪一版改的"是溯源事实，只能由编辑方声明。服务端若自己取"最新版"，
    就会把一个用户在旧版上做的修改记成"基于最新版" —— 溯源链从此失真，
    而且**没有任何报错**。

    三条约束：
      1. `base_version_id` 必须存在（否则 422 `version_not_found`）；
      2. 不能让版本跳到别的项目（否则 422 `cross_project_reference`）；
      3. 内容必须真的变了（否则 422 `draft_unchanged`）——
         "空保存"生成一个与上一版内容完全相同的新版本，
        会让版本历史里堆满无意义的记录，且下游"最近有没有变更"的判断失真。
    """
    base = require_version_node(repo, base_version_id, field="baseVersionId")

    if project_id is not None and project_id != base.project_id:
        raise _validation_error(
            "baseVersionId",
            "不能把版本升格到别的项目。",
            reason="cross_project_reference",
            baseProjectId=str(base.project_id) if base.project_id else None,
            requestedProjectId=str(project_id),
        )

    payload_hash = canonical_payload_hash(payload)
    if payload_hash == base.payload_hash:
        raise _validation_error(
            "payload",
            "内容与基准版本完全相同，无需固化新版本。",
            reason="draft_unchanged",
            baseVersionId=str(base.id),
        )

    return register_version(
        repo,
        entity_type=base.entity_type,
        entity_id=base.entity_id,
        payload=payload,
        project_id=base.project_id,
        created_by=created_by,
        schema_version=schema_version,
        revision_no=None,
    )


# ---------------------------------------------------------------------------
# 依赖边
# ---------------------------------------------------------------------------


def link_dependency(
    repo: VersionRepository,
    *,
    parent_version_id: uuid.UUID,
    dependency_version_id: uuid.UUID,
    purpose: str,
    selected_fragment: str | None = None,
) -> DependencyLink:
    """登记一条依赖边：`parent` 用到了 `dependency`。

    五道校验，顺序不可调换：
      1. **自引用**（422 `self_reference`）—— 先做，因为它不需要查库，
         且"自己依赖自己"是最容易犯的输入错误，早报早好。
      2. **`purpose` 非空**（422 `purpose_empty`）—— 同样不查库。
         空 purpose 会让同一条边被无限重复登记（主键含 purpose）。
      3. **两端存在**（422 `version_not_found`）
      4. **同项目**（422 `cross_project_reference`）—— 验收项
      5. **不成环**（422 `dependency_cycle`）—— 放在最后，
         因为它是唯一需要遍历依赖图的一项，前四条任一不过就不必付这个代价。

    **幂等回查排在环检测之前**（反直觉，但更快也更准）：
      · 主键查是一次索引命中，环检测要遍历依赖图 —— 对"同一条边重复登记"
        这个最常见的情形（消息重投、前端重复提交）直接省掉整趟遍历。
      · 语义上也不吃亏：一条边"已经存在"意味着它当初被成功插入过，
        而插入时会过环检测；因此正常数据下"边已存在"与"会成环"互斥。
        若两者真的同时成立（图里已有绕过应用层写入的脏环），
        返回 `created=False` 也更贴近事实 —— 调用方要做的事确实已经做成了，
        这时报 422 反而误导它以为失败。

    **幂等**：同一条边重复登记返回 `created=False`，
    不报错也不重复插入（重复插入会被主键挡住，报错会迫使调用方自己判重）。
    """
    if parent_version_id == dependency_version_id:
        raise _validation_error(
            "dependencyVersionId",
            "版本不能依赖自己。",
            reason="self_reference",
        )

    normalized_purpose = purpose.strip()
    if not normalized_purpose:
        raise _validation_error(
            "purpose", "purpose 不能为空。", reason="purpose_empty"
        )

    parent = require_version_node(repo, parent_version_id, field="parentVersionId")
    dependency = require_version_node(
        repo, dependency_version_id, field="dependencyVersionId"
    )

    _require_same_project(parent, dependency)

    # 幂等回查先于环检测：主键查是索引命中，环检测要遍历整张依赖图。
    # 取舍理由（含"边已存在且会成环"这种脏数据情形）见本函数 docstring。
    if (
        repo.get_dependency(
            parent_version_id=parent.id,
            dependency_version_id=dependency.id,
            purpose=normalized_purpose,
        )
        is not None
    ):
        return DependencyLink(
            parent_version_id=parent.id,
            dependency_version_id=dependency.id,
            purpose=normalized_purpose,
            created=False,
        )

    _require_no_cycle(repo, parent, dependency)

    fragment_digest = (
        fragment_hash(selected_fragment) if selected_fragment is not None else None
    )

    try:
        with repo.savepoint():
            repo.insert_dependency(
                parent_version_id=parent.id,
                dependency_version_id=dependency.id,
                purpose=normalized_purpose,
                selected_fragment_hash=fragment_digest,
            )
            repo.flush()
    except IntegrityError as exc:
        if _mentions_constraint(exc, _PK_VERSION_DEPENDENCY):
            # 并发插入了同一条边 —— 结果与我们要做的一致，按幂等成功返回。
            return DependencyLink(
                parent_version_id=parent.id,
                dependency_version_id=dependency.id,
                purpose=normalized_purpose,
                created=False,
            )
        if _mentions_constraint(exc, _CK_NO_SELF_REFERENCE):
            raise _validation_error(
                "dependencyVersionId",
                "版本不能依赖自己。",
                reason="self_reference",
            ) from exc
        if _is_dependency_scope_violation(exc):
            # 数据库触发器拦下的跨项目引用。正常情况下上面的
            # `_require_same_project` 已经拦掉了，走到这里说明
            # 两端的 project_id 在读之后发生了变化（或有人绕过本函数写表）。
            raise _validation_error(
                "dependencyVersionId",
                "不能引用其他项目的版本。",
                reason="cross_project_reference",
            ) from exc
        raise

    return DependencyLink(
        parent_version_id=parent.id,
        dependency_version_id=dependency.id,
        purpose=normalized_purpose,
        created=True,
    )


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def get_version(
    repo: VersionRepository, *, version_id: uuid.UUID, user: CurrentUser
) -> VersionNode:
    """取单个版本节点（带可见性校验）。

    不可见与不存在**返回同一个 404**，理由见 `permissions.py`。
    """
    node = repo.get_node(version_id)
    if node is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "版本不存在。",
            details={"resourceId": str(version_id)},
        )
    require_version_visible(repo=repo, node=node, user=user)
    return node


def list_version_history(
    repo: VersionRepository,
    *,
    user: CurrentUser,
    entity_type: str,
    entity_id: uuid.UUID,
    limit: int,
    after: tuple[int, uuid.UUID] | None = None,
) -> VersionPage:
    """某逻辑实体的版本历史（键集分页，最新在最前）。

    **可见性怎么判**：历史本身没有独立的归属，归属来自节点。
    这里先取"最新的一条"来判定可见性 —— 因为同一实体的所有版本必然
    同属一个项目（`register_version` 与 `promote_draft` 都保证了这点）。

    实体一个版本都没有时返回空页（`has_more=False`）：
    此时没有任何信息泄漏 —— "是否存在这个实体"本来就无从判断，
    调用方拿到空列表即可。
    """
    _require_entity_type(entity_type)

    rows = repo.list_history(
        entity_type=entity_type,
        entity_id=entity_id,
        limit=limit,
        after=after,
    )
    if not rows:
        return VersionPage(items=(), has_more=False, next_anchor=None)

    # 分页后再判可见性：若不可见，把整页降级成 404，而不是只过滤掉不可见的行
    # （过滤会让"第一页空、第二页有"这种矛盾结果出现，让客户端以为数据在变）。
    require_version_visible(repo=repo, node=rows[0], user=user)

    has_more = len(rows) > limit
    window = tuple(rows[:limit])
    next_anchor: tuple[int, uuid.UUID] | None = None
    if has_more and window:
        last = window[-1]
        next_anchor = (last.revision_no, last.id)
    return VersionPage(items=window, has_more=has_more, next_anchor=next_anchor)


def list_version_dependencies(
    repo: VersionRepository, *, version_id: uuid.UUID, user: CurrentUser
) -> tuple[VersionNode, tuple[VersionDependency, ...]]:
    """该版本的直接上游（它用到了哪些版本）。"""
    node = get_version(repo, version_id=version_id, user=user)
    return node, tuple(repo.list_dependencies(node.id))


def list_version_dependents(
    repo: VersionRepository, *, version_id: uuid.UUID, user: CurrentUser
) -> tuple[VersionNode, tuple[VersionDependency, ...]]:
    """该版本的直接下游（哪些版本用到了它）—— 影响分析的入口。"""
    node = get_version(repo, version_id=version_id, user=user)
    return node, tuple(repo.list_dependents(node.id))


def can_view(
    repo: VersionRepository, *, node: VersionNode, user: CurrentUser
) -> bool:
    """`resolve_version_visibility` 的薄封装，便于下游按需判权限。"""
    return resolve_version_visibility(repo=repo, node=node, user=user)


__all__ = [
    "DEPENDENCY_SCOPE_SQLSTATE",
    "DEPENDENCY_SCOPE_TRIGGER",
    "MAX_REGISTER_ATTEMPTS",
    "DependencyLink",
    "RegisteredVersion",
    "VersionPage",
    "can_view",
    "canonical_payload_hash",
    "fragment_hash",
    "get_version",
    "link_dependency",
    "list_version_dependencies",
    "list_version_dependents",
    "list_version_history",
    "promote_draft",
    "register_version",
    "require_version_node",
]
