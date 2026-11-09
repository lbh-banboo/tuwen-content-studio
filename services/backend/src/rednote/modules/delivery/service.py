"""交付与发布登记的业务编排（WP36）。

本包是**事实登记层**：把"谁在什么时候拿到了哪一篇的哪一版"、"后来发成了哪条
笔记"原样记下来，并保证记下来的东西不会被后续动作改写。所有"从事实推结论"
（URL 规范化、笔记去重、指标、导出失效传播）都在它之后，分别归 WP37 / WP38 / WP34。

## 建交付的七步（顺序不可调换）

| # | 步 | 失败时 |
|---|---|---|
| 1 | 入参校验（`Idempotency-Key` 长度） | 422，什么都没写 |
| 2 | 幂等：同项目同键已有一行？同体 ⇒ **原样回放**；异体 ⇒ 409 | 不重复落行 |
| 3 | 项目存在 | 404 |
| 4 | 取**导出行**（`export_bridge`）并判归属 | 404（与不存在同形，防枚举） |
| 5 | KOC 归属（给了才判） | 404 |
| 6 | **复制**三个冻结字段，落 `PREPARED` 行 | 事务回滚 |
| 7 | `flush`（并发同键的唯一冲突在这里抛） | 回滚保存点后重查回放 |

第 4 步是"固定导出版本"的**唯一**来源：`content_item_id` / `version_node_id` /
`eligibility_hash` 全部从 `export_items` 逐列复制，**客户端不传版本**。
WP35 已为同一问题否决过"客户端传 `versionNodeId`"（`exports/api.py:117-122`
原文：陈旧缓存会导出旧版而不报错）。于是本包**零"当前版本"读法实现**，
也就不会成为 `docs/数据结构与接口契约.md` §2.2 的第三处取数实现。

## 标记已交付：「下载不计交付」+「资格复验」

- `delivered_at` 只由本函数经 `repository.mark_delivered` 置，**全包仅此一处**；
  下载端点属 `exports` 模块且全程只读 ⇒ 下载路径物理上触不到这一列。
- **资格复验是真的重算**：调 `export_bridge.current_eligibility_hash`（转发
  WP35 的只读接缝）按**当前**正文版本与正式图重算指纹，与交付行上复制的存量
  值比对。两边都读存量值的"复验"永远相等 = 没有复验（ISS-95 家族），不采用。

| 复验结果 | 处置 |
|---|---|
| 相等 | 放行，推 `DELIVERED` |
| 不等（已改稿 / 重出图） | 409 `ELIGIBILITY_STALE` —— 要重新导出再交付 |
| 算不出（内容已无版本 / 配图位缺图） | 409 `ELIGIBILITY_UNAVAILABLE` —— **不**降级成"通过" |

## 替换：只 INSERT，对旧行零 UPDATE

`replace_delivery` 追加一行新交付 + `replaces_delivery_id` 指向旧行，
**不碰旧行的任何一列**。旧行的 `lock_version` 不变是最硬的证据。
一条旧交付最多被替换一次（`uq_deliveries_replaces` 部分唯一），否则"替换链"
会变成"替换树"，"当前有效的那一版"就有了两个答案。

## 本包刻意不做

URL 规范化 / 笔记去重 / 发布状态抓取与验证（**WP37**）；任何指标（**WP38**）；
批量交付端点、交付撤回为 `PREPARED`、给 KOC 发通知、任何打包代码。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from rednote.modules.delivery import export_bridge
from rednote.modules.delivery.models import (
    Delivery,
    KocRecord,
    Publication,
    PublicationLink,
)
from rednote.modules.delivery.repository import DeliveryRepository
from rednote.modules.delivery.schemas import (
    SchemaViolation,
    require_transition,
    validate_display_name,
    validate_idempotency_key,
    validate_publication_key,
    validate_published_shape,
)
from rednote.modules.delivery.types import (
    CONTENT_STATE_INITIAL,
    CONTENT_STATES,
    DELIVERY_STATE_CANCELLED,
    DELIVERY_STATE_DELIVERED,
    DELIVERY_STATE_INITIAL,
    REASON_ALREADY_REPLACED,
    REASON_CROSS_PROJECT_NOTE,
    REASON_DELIVERY_CANCELLED,
    REASON_DELIVERY_NOT_FOUND,
    REASON_ELIGIBILITY_STALE,
    REASON_ELIGIBILITY_UNAVAILABLE,
    REASON_EXPORT_ITEM_NOT_FOUND,
    REASON_IDEMPOTENCY_KEY_REUSED,
    REASON_INVALID_TRANSITION,
    REASON_KOC_NOT_FOUND,
    REASON_LINK_CANONICAL_TAKEN,
    REASON_LINK_URL_INVALID,
    REASON_PROJECT_NOT_FOUND,
    REASON_PUBLICATION_KEY_TAKEN,
    REASON_PUBLICATION_NOT_FOUND,
    REASON_VERIFY_RESULT_INVALID,
    FrozenExportItem,
    delivery_create_request_hash,
    delivery_replace_request_hash,
    publication_create_request_hash,
)
from rednote.modules.delivery.urls import UrlInvalid, normalize_url

class DeliveryError(Exception):
    """交付/发布登记的业务错误。`reason` 是 `types.REASON_*`，由 API 层翻码。"""

    def __init__(self, reason: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details


@dataclass(frozen=True)
class DeliveryResult:
    """一次交付写操作的结果。`created=False` = 幂等回放（含"已是目标状态"）。"""

    delivery: Delivery
    created: bool


@dataclass(frozen=True)
class PublicationResult:
    publication: Publication
    created: bool


def _schema_error(exc: SchemaViolation) -> DeliveryError:
    return DeliveryError(exc.reason, str(exc), **exc.details)


# ---------------------------------------------------------------------------
# KOC 记录
# ---------------------------------------------------------------------------


def create_koc_record(
    repo: DeliveryRepository,
    *,
    project_id: uuid.UUID,
    display_name: str,
    platform_home_url: str | None = None,
    contact_ref: str | None = None,
    note: str | None = None,
) -> KocRecord:
    """建一条 KOC 记录。

    **不做**主页链接去重：同名不同人、同链不同参，唯一约束会误拒（草案 `:54`）。
    `home_url_canonical` **不填** —— 规范化是 WP37 的全部内容，本包一行都不写。
    """
    if not repo.project_exists(project_id):
        raise DeliveryError(REASON_PROJECT_NOT_FOUND, "项目不存在。")
    try:
        cleaned = validate_display_name(display_name)
    except SchemaViolation as exc:
        raise _schema_error(exc) from exc

    row = repo.insert_koc_record(
        project_id=project_id,
        display_name=cleaned,
        platform_home_url=platform_home_url,
        contact_ref=contact_ref,
        note=note,
    )
    repo.flush()
    return row


# ---------------------------------------------------------------------------
# 交付：创建
# ---------------------------------------------------------------------------


def _frozen_export_item(
    repo: DeliveryRepository, *, project_id: uuid.UUID, export_item_id: uuid.UUID
) -> FrozenExportItem:
    """取导出行并判归属。不存在 与 属于别的项目 **同形 404**（防枚举）。"""
    frozen = export_bridge.read_export_item(
        repo.session, export_item_id=export_item_id
    )
    if frozen is None or frozen.project_id != project_id:
        raise DeliveryError(
            REASON_EXPORT_ITEM_NOT_FOUND,
            "导出记录不存在。",
            exportItemId=str(export_item_id),
        )
    return frozen


def _require_koc_in_project(
    repo: DeliveryRepository, *, project_id: uuid.UUID, koc_record_id: uuid.UUID | None
) -> None:
    if koc_record_id is None:
        return
    koc = repo.get_koc_record(koc_record_id)
    if koc is None or koc.project_id != project_id:
        raise DeliveryError(
            REASON_KOC_NOT_FOUND, "KOC 记录不存在。", kocRecordId=str(koc_record_id)
        )


def _replay_delivery(
    repo: DeliveryRepository,
    *,
    project_id: uuid.UUID,
    idempotency_key: str | None,
    request_hash: str,
) -> DeliveryResult | None:
    """同键已有一行：同体 ⇒ 回放；异体 ⇒ 409；没有 ⇒ `None`。"""
    if idempotency_key is None:
        return None
    existing = repo.find_delivery_by_idempotency(
        project_id=project_id, idempotency_key=idempotency_key
    )
    if existing is None:
        return None
    if existing.request_hash != request_hash:
        raise DeliveryError(
            REASON_IDEMPOTENCY_KEY_REUSED,
            "相同幂等键对应了不同的请求内容。",
            idempotencyKey=idempotency_key,
        )
    return DeliveryResult(delivery=existing, created=False)


def _insert_with_idempotency_retry(
    repo: DeliveryRepository,
    *,
    project_id: uuid.UUID,
    idempotency_key: str | None,
    request_hash: str,
    build: Callable[[], Delivery],
) -> DeliveryResult:
    """把 INSERT + flush 包进保存点；并发同键的后到者撞唯一索引后重查回放。

    ⚠️ 只在**带键**时才可能是"并发同键"；无键的 `IntegrityError` 一律原样抛出
    （否则会把真正的约束违反伪装成幂等回放）。照 `exports/service.py:284-296`。
    """
    try:
        with repo.session.begin_nested():
            row = build()
            repo.flush()
            return DeliveryResult(delivery=row, created=True)
    except IntegrityError:
        if idempotency_key is None:
            raise
        replayed = _replay_delivery(
            repo,
            project_id=project_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replayed is None:
            raise
        return replayed


def create_delivery(
    repo: DeliveryRepository,
    *,
    project_id: uuid.UUID,
    export_item_id: uuid.UUID,
    koc_record_id: uuid.UUID | None,
    note: str | None,
    idempotency_key: str | None,
    created_by: uuid.UUID,
) -> DeliveryResult:
    """建一条 `PREPARED` 交付（七步见模块 docstring）。"""
    try:
        validate_idempotency_key(idempotency_key)
    except SchemaViolation as exc:
        raise _schema_error(exc) from exc

    req_hash = delivery_create_request_hash(
        export_item_id=export_item_id, koc_record_id=koc_record_id, note=note
    )
    replayed = _replay_delivery(
        repo,
        project_id=project_id,
        idempotency_key=idempotency_key,
        request_hash=req_hash,
    )
    if replayed is not None:
        return replayed

    if not repo.project_exists(project_id):
        raise DeliveryError(REASON_PROJECT_NOT_FOUND, "项目不存在。")

    frozen = _frozen_export_item(
        repo, project_id=project_id, export_item_id=export_item_id
    )
    _require_koc_in_project(repo, project_id=project_id, koc_record_id=koc_record_id)

    def _build() -> Delivery:
        return repo.insert_delivery(
            project_id=project_id,
            koc_record_id=koc_record_id,
            export_item_id=frozen.export_item_id,
            # --- 三个核心冻结字段：逐列复制，零计算、零客户端输入 ---
            content_item_id=frozen.content_item_id,
            version_node_id=frozen.version_node_id,
            eligibility_hash=frozen.eligibility_hash,
            state=DELIVERY_STATE_INITIAL,
            replaces_delivery_id=None,
            idempotency_key=idempotency_key,
            request_hash=req_hash,
            note=note,
            created_by=created_by,
        )

    return _insert_with_idempotency_retry(
        repo,
        project_id=project_id,
        idempotency_key=idempotency_key,
        request_hash=req_hash,
        build=_build,
    )


# ---------------------------------------------------------------------------
# 交付：状态机（标记已交付 / 取消）
# ---------------------------------------------------------------------------


def get_delivery(
    repo: DeliveryRepository, *, delivery_id: uuid.UUID, project_id: uuid.UUID | None
) -> Delivery:
    """按 id 取交付。不存在 与 不属于指定项目 **同形 404**。

    `project_id=None` 表示"调用方还没鉴权，先拿行以便反查项目" —— 只在
    `api.py` 的 item 级路径里用，拿到后立刻鉴权。
    """
    row = repo.get_delivery(delivery_id)
    if row is None or (project_id is not None and row.project_id != project_id):
        raise DeliveryError(
            REASON_DELIVERY_NOT_FOUND, "交付记录不存在。", deliveryId=str(delivery_id)
        )
    return row


def _reverify_eligibility(repo: DeliveryRepository, delivery: Delivery) -> str:
    """重算当前资格指纹并与交付行上的快照比对。不等 / 算不出 ⇒ 409。"""
    try:
        current = export_bridge.current_eligibility_hash(
            repo.session, content_item_id=delivery.content_item_id
        )
    except export_bridge.EligibilityUnavailable as exc:
        raise DeliveryError(
            REASON_ELIGIBILITY_UNAVAILABLE,
            f"无法重算当前的导出资格指纹：{exc}",
            deliveryId=str(delivery.id),
            upstreamReason=exc.upstream_reason,
        ) from exc
    if current != delivery.eligibility_hash:
        raise DeliveryError(
            REASON_ELIGIBILITY_STALE,
            "内容在导出之后已经变化，请重新导出后再交付。",
            deliveryId=str(delivery.id),
            frozenEligibilityHash=delivery.eligibility_hash,
            currentEligibilityHash=current,
        )
    return current


def confirm_delivery(
    repo: DeliveryRepository, *, delivery_id: uuid.UUID, confirmed_by: uuid.UUID
) -> DeliveryResult:
    """显式标记已交付。**这是全包唯一会置 `delivered_at` 的路径。**

    已是 `DELIVERED` ⇒ 幂等回放（`created=False`，`delivered_at` 不变），
    不是 409：重复点"标记已交付"是正常手滑。方向错了（`CANCELLED → DELIVERED`）
    才是 409 `INVALID_TRANSITION`。
    """
    delivery = get_delivery(repo, delivery_id=delivery_id, project_id=None)
    if delivery.state == DELIVERY_STATE_DELIVERED:
        return DeliveryResult(delivery=delivery, created=False)
    try:
        require_transition(delivery.state, DELIVERY_STATE_DELIVERED)
    except SchemaViolation as exc:
        raise _schema_error(exc) from exc

    _reverify_eligibility(repo, delivery)

    changed = repo.mark_delivered(
        delivery_id=delivery.id,
        expected_lock_version=delivery.lock_version,
        delivered_by=confirmed_by,
    )
    if changed == 0:
        # 并发：别人已经把它推走了。重读后要么已是 DELIVERED（幂等回放），
        # 要么方向不对（409）—— 不静默返回"成功"。
        fresh = repo.reload_delivery(delivery.id)
        if fresh is not None and fresh.state == DELIVERY_STATE_DELIVERED:
            return DeliveryResult(delivery=fresh, created=False)
        raise DeliveryError(
            REASON_INVALID_TRANSITION,
            "交付状态已被并发修改，无法标记为已交付。",
            deliveryId=str(delivery_id),
        )
    fresh = repo.reload_delivery(delivery.id)
    assert fresh is not None
    return DeliveryResult(delivery=fresh, created=True)


def cancel_delivery(
    repo: DeliveryRepository, *, delivery_id: uuid.UUID
) -> DeliveryResult:
    """取消一条尚未交付的交付。已 `CANCELLED` ⇒ 幂等回放；已 `DELIVERED` ⇒ 409。

    **没有"撤回已交付"** ——`DELIVERED` 是终态（口径文档只给了两条边）。
    """
    delivery = get_delivery(repo, delivery_id=delivery_id, project_id=None)
    if delivery.state == DELIVERY_STATE_CANCELLED:
        return DeliveryResult(delivery=delivery, created=False)
    try:
        require_transition(delivery.state, DELIVERY_STATE_CANCELLED)
    except SchemaViolation as exc:
        raise _schema_error(exc) from exc

    changed = repo.mark_cancelled(
        delivery_id=delivery.id, expected_lock_version=delivery.lock_version
    )
    if changed == 0:
        fresh = repo.reload_delivery(delivery.id)
        if fresh is not None and fresh.state == DELIVERY_STATE_CANCELLED:
            return DeliveryResult(delivery=fresh, created=False)
        raise DeliveryError(
            REASON_INVALID_TRANSITION,
            "交付状态已被并发修改，无法取消。",
            deliveryId=str(delivery_id),
        )
    fresh = repo.reload_delivery(delivery.id)
    assert fresh is not None
    return DeliveryResult(delivery=fresh, created=True)


# ---------------------------------------------------------------------------
# 交付：替换（追加一行，旧行零 UPDATE）
# ---------------------------------------------------------------------------


def replace_delivery(
    repo: DeliveryRepository,
    *,
    delivery_id: uuid.UUID,
    export_item_id: uuid.UUID,
    koc_record_id: uuid.UUID | None,
    note: str | None,
    idempotency_key: str | None,
    created_by: uuid.UUID,
) -> DeliveryResult:
    """改稿后给同一条内容追加一版新交付。

    **本函数对旧行没有任何 UPDATE 语句** —— 这是「替换是追加」这条产品规则的
    服务层载体，由 `tools/quality/check_download_not_delivery.py` 的 AST 判据
    机械核对（replace 路径里不得出现对 `deliveries` 的 UPDATE）。

    `koc_record_id` 不传 ⇒ **继承旧行的 KOC**（"重新给同一位 KOC 一版"是默认
    语义）。要换人也允许，但那是一次新的交付事实，仍落在新行上。
    """
    try:
        validate_idempotency_key(idempotency_key)
    except SchemaViolation as exc:
        raise _schema_error(exc) from exc

    old = get_delivery(repo, delivery_id=delivery_id, project_id=None)

    req_hash = delivery_replace_request_hash(
        replaces_delivery_id=old.id,
        export_item_id=export_item_id,
        koc_record_id=koc_record_id,
        note=note,
    )
    replayed = _replay_delivery(
        repo,
        project_id=old.project_id,
        idempotency_key=idempotency_key,
        request_hash=req_hash,
    )
    if replayed is not None:
        return replayed

    if old.state == DELIVERY_STATE_CANCELLED:
        raise DeliveryError(
            REASON_DELIVERY_CANCELLED,
            "已取消的交付没有可替换的对象。",
            deliveryId=str(old.id),
        )

    existing_replacement = repo.find_replacement_of(old.id)
    if existing_replacement is not None:
        raise DeliveryError(
            REASON_ALREADY_REPLACED,
            "这条交付已经被替换过了，请替换最新的那一条。",
            deliveryId=str(old.id),
            replacementId=str(existing_replacement.id),
        )

    frozen = _frozen_export_item(
        repo, project_id=old.project_id, export_item_id=export_item_id
    )
    if frozen.content_item_id != old.content_item_id:
        # 替换必须是**同一条内容**的新版本；换内容是另一次交付，不是替换。
        raise DeliveryError(
            REASON_EXPORT_ITEM_NOT_FOUND,
            "替换用的导出记录不属于同一条内容。",
            exportItemId=str(export_item_id),
        )

    target_koc = old.koc_record_id if koc_record_id is None else koc_record_id
    _require_koc_in_project(
        repo, project_id=old.project_id, koc_record_id=target_koc
    )

    def _build() -> Delivery:
        return repo.insert_delivery(
            project_id=old.project_id,
            koc_record_id=target_koc,
            export_item_id=frozen.export_item_id,
            content_item_id=frozen.content_item_id,
            version_node_id=frozen.version_node_id,
            eligibility_hash=frozen.eligibility_hash,
            state=DELIVERY_STATE_INITIAL,
            replaces_delivery_id=old.id,
            idempotency_key=idempotency_key,
            request_hash=req_hash,
            note=note,
            created_by=created_by,
        )

    return _insert_with_idempotency_retry(
        repo,
        project_id=old.project_id,
        idempotency_key=idempotency_key,
        request_hash=req_hash,
        build=_build,
    )


# ---------------------------------------------------------------------------
# 发布登记
# ---------------------------------------------------------------------------


def create_publication(
    repo: DeliveryRepository,
    *,
    project_id: uuid.UUID,
    publication_key: str,
    delivery_id: uuid.UUID | None,
    note_id: str | None,
    note_url: str | None,
    state: str,
    published_at: datetime | None,
    idempotency_key: str | None,
    created_by: uuid.UUID,
) -> PublicationResult:
    """登记一条发布（草稿 → 实发映射）。

    `note_id` / `note_url` 存**原样值**，不做清洗、不做规范化、不做去重 ——
    那三件事全是 WP37。`content_state` 一律落 `UNVERIFIED`（本包不抓取），
    **这不代表"笔记已删除"**。
    """
    try:
        validate_idempotency_key(idempotency_key)
        key = validate_publication_key(publication_key)
        validate_published_shape(state, published_at)
    except SchemaViolation as exc:
        raise _schema_error(exc) from exc

    req_hash = publication_create_request_hash(
        publication_key=key,
        delivery_id=delivery_id,
        note_id=note_id,
        note_url=note_url,
        state=state,
        published_at=published_at,
    )

    def _replay() -> PublicationResult | None:
        if idempotency_key is None:
            return None
        existing = repo.find_publication_by_idempotency(
            project_id=project_id, idempotency_key=idempotency_key
        )
        if existing is None:
            return None
        if existing.request_hash != req_hash:
            raise DeliveryError(
                REASON_IDEMPOTENCY_KEY_REUSED,
                "相同幂等键对应了不同的请求内容。",
                idempotencyKey=idempotency_key,
            )
        return PublicationResult(publication=existing, created=False)

    replayed = _replay()
    if replayed is not None:
        return replayed

    if not repo.project_exists(project_id):
        raise DeliveryError(REASON_PROJECT_NOT_FOUND, "项目不存在。")

    if delivery_id is not None:
        # 归属判定与"不存在"同形（防枚举）。
        get_delivery(repo, delivery_id=delivery_id, project_id=project_id)

    def _taken(existing: Publication) -> DeliveryError:
        return DeliveryError(
            REASON_PUBLICATION_KEY_TAKEN,
            "这条发布业务键在本项目已经登记过了。",
            publicationKey=key,
            publicationId=str(existing.id),
        )

    duplicate = repo.find_publication_by_key(project_id=project_id, publication_key=key)
    if duplicate is not None:
        raise _taken(duplicate)

    try:
        with repo.session.begin_nested():
            row = repo.insert_publication(
                project_id=project_id,
                delivery_id=delivery_id,
                publication_key=key,
                note_id=note_id,
                note_url=note_url,
                state=state,
                content_state=CONTENT_STATE_INITIAL,
                published_at=published_at,
                idempotency_key=idempotency_key,
                request_hash=req_hash,
                created_by=created_by,
            )
            repo.flush()
            return PublicationResult(publication=row, created=True)
    except IntegrityError:
        # 两条唯一索引都可能在这里抛：先试幂等回放，再看是不是业务键撞了。
        replayed = _replay()
        if replayed is not None:
            return replayed
        duplicate = repo.find_publication_by_key(
            project_id=project_id, publication_key=key
        )
        if duplicate is not None:
            raise _taken(duplicate) from None
        raise


# ---------------------------------------------------------------------------
# 发布链接 / 笔记去重 / 内容状态（WP37）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublicationLinkResult:
    link: PublicationLink
    #: 管理者本人登记并放行了一次"跨项目同笔记"。普通命中路径恒为 `False`。
    cross_project_confirmed: bool


def get_publication(
    repo: DeliveryRepository, *, publication_id: uuid.UUID
) -> Publication:
    """按 id 取发布。不存在 ⇒ 404。item 级路径先用它反查 `project_id` 再鉴权。"""
    row = repo.get_publication(publication_id)
    if row is None:
        raise DeliveryError(
            REASON_PUBLICATION_NOT_FOUND,
            "发布记录不存在。",
            publicationId=str(publication_id),
        )
    return row


def add_publication_link(
    repo: DeliveryRepository,
    *,
    publication_id: uuid.UUID,
    raw_url: str,
    can_confirm_cross_project: bool,
    idempotency_key: str | None = None,
) -> PublicationLinkResult:
    """登记一条发布链接：规范化 → 项目内查重 → 跨项目查重 → 入库 → 回填 `url_canonical`。

    - 项目内同 canonical 已有 ⇒ 409 `LINK_CANONICAL_TAKEN`（`DUPLICATE_PUBLICATION`）；
    - 别的项目已有且 `can_confirm_cross_project=False` ⇒ 409 `CROSS_PROJECT_NOTE`，
      `details.crossProject=true`，**不带**对方项目 id / 名称 / 发布 id（防枚举）；
    - 别的项目已有且调用者有确认权（管理者）⇒ 放行，`cross_project_confirmed=True`。

    `idempotency_key` 只校验长度：`publication_links` 没有幂等键列（迁移范围只加
    `project_id` + 唯一索引），所以重试同一请求会得到 409 重复，而不是回放。
    """
    try:
        validate_idempotency_key(idempotency_key)
    except SchemaViolation as exc:
        raise _schema_error(exc) from exc
    try:
        normalized = normalize_url(raw_url)
    except UrlInvalid as exc:
        raise DeliveryError(REASON_LINK_URL_INVALID, str(exc), field="rawUrl") from exc

    pub = get_publication(repo, publication_id=publication_id)
    project_id = pub.project_id

    def _taken(existing: PublicationLink) -> DeliveryError:
        return DeliveryError(
            REASON_LINK_CANONICAL_TAKEN,
            "这条链接（规范化后）在本项目已经登记过了。",
            canonicalUrl=normalized.canonical,
            publicationId=str(existing.publication_id),
            samePublication=existing.publication_id == pub.id,
        )

    existing = repo.find_link_in_project(
        project_id=project_id, canonical_url=normalized.canonical
    )
    if existing is not None:
        raise _taken(existing)

    cross = repo.exists_link_in_other_project(
        project_id=project_id, canonical_url=normalized.canonical
    )
    if cross and not can_confirm_cross_project:
        raise DeliveryError(
            REASON_CROSS_PROJECT_NOTE,
            "这条笔记已在其他项目登记过，需要管理者确认后才能在本项目登记。",
            crossProject=True,
            canonicalUrl=normalized.canonical,
        )

    try:
        with repo.session.begin_nested():
            row = repo.insert_publication_link(
                publication_id=pub.id,
                project_id=project_id,
                raw_url=raw_url.strip(),
                canonical_url=normalized.canonical,
                link_kind=normalized.link_kind,
            )
            repo.flush()
    except IntegrityError:
        # 并发：别人在查重与插入之间登记了同 canonical。
        winner = repo.find_link_in_project(
            project_id=project_id, canonical_url=normalized.canonical
        )
        if winner is None:
            raise
        raise _taken(winner) from None

    # 首条链接回填；已有值（含并发已回填）不覆盖。
    repo.backfill_url_canonical(
        publication_id=pub.id, url_canonical=normalized.canonical
    )
    return PublicationLinkResult(link=row, cross_project_confirmed=cross)


def verify_publication(
    repo: DeliveryRepository,
    *,
    publication_id: uuid.UUID,
    result: str,
    idempotency_key: str | None = None,
) -> Publication:
    """把调用方给出的核验结果写入 `publications.content_state`。

    只接受 `VERIFIED` / `UNVERIFIED`；取值域里没有"已删除"，所以**结构上推不出删除**。
    本包不联网，也就没有"抓取失败"：结果完全由调用方给出。天然幂等（同值重写不报错）。
    """
    try:
        validate_idempotency_key(idempotency_key)
    except SchemaViolation as exc:
        raise _schema_error(exc) from exc
    if result not in CONTENT_STATES:
        raise DeliveryError(
            REASON_VERIFY_RESULT_INVALID,
            f"result 只允许 {', '.join(CONTENT_STATES)}。",
            field="result",
        )
    pub = get_publication(repo, publication_id=publication_id)
    repo.set_publication_content_state(publication_id=pub.id, content_state=result)
    fresh = repo.reload_publication(pub.id)
    assert fresh is not None
    return fresh


__all__ = [
    "PublicationLinkResult",
    "add_publication_link",
    "get_publication",
    "verify_publication",
    "DeliveryError",
    "DeliveryResult",
    "PublicationResult",
    "cancel_delivery",
    "confirm_delivery",
    "create_delivery",
    "create_koc_record",
    "create_publication",
    "get_delivery",
    "replace_delivery",
]
