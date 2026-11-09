"""本包四表的仓储 + 跨模块只读裸 SQL（WP36）。

跨模块取数走**裸 SQL**（平级模块不可互 import），列名即对方契约：
写错列名当场 `UndefinedColumn`，不会静默。`export_items` 的取数**不在本文件**
—— 它是 `export_bridge.py` 的职责（那边还要转发资格复验接缝，两件事同一个出口）。

## 为什么没有通用的 `update_delivery(**columns)`

「下载不计交付」这条产品规则的服务层载体就是**没有通往 `delivered_at` 的通用
写入口**。本文件只提供两个**按动作命名的条件 UPDATE**：

| 方法 | 改哪几列 | 条件 |
|---|---|---|
| `mark_delivered` | `state` / `delivered_at` / `delivered_by` / `lock_version` | `id` + `state = 'PREPARED'` + `lock_version` |
| `mark_cancelled` | `state` / `cancelled_at` / `lock_version` | 同上 |

**条件 UPDATE 而不是读-改-写**：`WHERE id = :id AND state = :from AND
lock_version = :expected` 把"检查前置状态"与"写入"压在同一条语句里。两个并发
的标记只有一个能成功（另一个 `rowcount = 0`），失败的那个**不会静默覆盖**。
与 `feedback/repository.py:290-327` 同款手法。

`deliveries` 的其余列由 `trg_deliveries_frozen_columns` 在库层拦死 —— 即使有人
以后在这里加一个通用 UPDATE，冻结列也改不动（两道防线，不是一道）。

## 替换为什么只有 INSERT

`insert_delivery` 是本文件**唯一**往 `deliveries` 写新行的方法；替换路径
（`service.replace_delivery`）调它一次，对旧行**零 UPDATE**。
旧行的 `lock_version` 不变是最硬的证据：它变了就说明有人 UPDATE 过旧行。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from rednote.modules.delivery.models import (
    Delivery,
    KocRecord,
    Publication,
    PublicationLink,
)
from rednote.modules.delivery.types import (
    DELIVERY_STATE_CANCELLED,
    DELIVERY_STATE_DELIVERED,
    DELIVERY_STATE_PREPARED,
)


class DeliveryRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def flush(self) -> None:
        self._session.flush()

    # ------------------------------------------------------------------
    # 跨模块只读（裸 SQL）
    # ------------------------------------------------------------------

    def project_exists(self, project_id: uuid.UUID) -> bool:
        row = self._session.execute(
            text("SELECT 1 FROM projects WHERE id = :pid"), {"pid": project_id}
        ).first()
        return row is not None

    def content_project_id(self, content_item_id: uuid.UUID) -> uuid.UUID | None:
        """内容所属项目。用于"内容不属本项目 ⇒ 404"（与不存在同形，防枚举）。"""
        row = self._session.execute(
            text("SELECT project_id FROM content_items WHERE id = :cid"),
            {"cid": content_item_id},
        ).first()
        return None if row is None else row[0]

    def exported_not_marked(self, project_id: uuid.UUID) -> list[tuple[uuid.UUID, Any]]:
        """「**已导出未标记**」：本项目有冻结行、但还没有任何"已交付"记录的内容。

        ⚠️ 口径是「已**导出**未标记」，**不是**原型写的「已下载未标记」：
        本仓**没有下载事件表**（WP35 的 download 端点零写操作，`exports/api.py:327-368`），
        "下载过几次"在磁盘上没有任何载体。沿用原型字样就是声称有数据而没有。

        返回 `[(content_item_id, 最近一次导出时间), ...]`，按时间倒序。
        """
        rows = self._session.execute(
            text(
                "SELECT ei.content_item_id, max(ei.created_at) AS last_export_at"
                " FROM export_items ei"
                " WHERE ei.project_id = :pid"
                "   AND NOT EXISTS ("
                "       SELECT 1 FROM deliveries d"
                "        WHERE d.export_item_id = ei.id AND d.state = :delivered)"
                " GROUP BY ei.content_item_id"
                " ORDER BY last_export_at DESC, ei.content_item_id"
            ),
            {"pid": project_id, "delivered": DELIVERY_STATE_DELIVERED},
        ).all()
        return [(r[0], r[1]) for r in rows]

    # ------------------------------------------------------------------
    # koc_records
    # ------------------------------------------------------------------

    def insert_koc_record(self, **columns: Any) -> KocRecord:
        row = KocRecord(**columns)
        self._session.add(row)
        return row

    def get_koc_record(self, koc_record_id: uuid.UUID) -> KocRecord | None:
        return self._session.get(KocRecord, koc_record_id)

    def list_koc_records(self, project_id: uuid.UUID, *, limit: int) -> list[KocRecord]:
        stmt = (
            select(KocRecord)
            .where(KocRecord.project_id == project_id)
            .order_by(KocRecord.created_at.desc(), KocRecord.id.desc())
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # deliveries
    # ------------------------------------------------------------------

    def insert_delivery(self, **columns: Any) -> Delivery:
        """**唯一**的 `deliveries` 写新行入口（创建与替换都走它）。"""
        row = Delivery(**columns)
        self._session.add(row)
        return row

    def get_delivery(self, delivery_id: uuid.UUID) -> Delivery | None:
        return self._session.get(Delivery, delivery_id)

    def reload_delivery(self, delivery_id: uuid.UUID) -> Delivery | None:
        """强制从库重读（条件 UPDATE 用了 `synchronize_session=False`，session 里还是旧值）。

        只刷这一个对象，**不用** `session.expire_all()`：那会把调用方正在编辑的
        其它对象一起失效（WP17 ISS-53 的教训）。
        """
        return self._session.get(Delivery, delivery_id, populate_existing=True)

    def find_delivery_by_idempotency(
        self, *, project_id: uuid.UUID, idempotency_key: str
    ) -> Delivery | None:
        """同项目同键命中的那一行（一键一行，由 `uq_deliveries_idempotency` 保证）。"""
        stmt = select(Delivery).where(
            Delivery.project_id == project_id,
            Delivery.idempotency_key == idempotency_key,
        )
        return self._session.execute(stmt).scalars().first()

    def find_replacement_of(self, delivery_id: uuid.UUID) -> Delivery | None:
        """已经替换过这一行的那条新交付（`uq_deliveries_replaces` 保证最多一条）。"""
        stmt = select(Delivery).where(Delivery.replaces_delivery_id == delivery_id)
        return self._session.execute(stmt).scalars().first()

    def list_deliveries(self, project_id: uuid.UUID, *, limit: int) -> list[Delivery]:
        stmt = (
            select(Delivery)
            .where(Delivery.project_id == project_id)
            .order_by(Delivery.created_at.desc(), Delivery.id.desc())
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars().all())

    def mark_delivered(
        self,
        *,
        delivery_id: uuid.UUID,
        expected_lock_version: int,
        delivered_by: uuid.UUID,
    ) -> int:
        """`PREPARED → DELIVERED`（条件 UPDATE），返回受影响行数。

        ★ **全仓唯一一处写 `delivered_at` 的地方**（"下载不计交付"的服务层载体，
        由 `tools/quality/check_download_not_delivery.py` 机械核对恰好 1 处）。
        时间由**库**取（`func.now()`），不由应用进程取：多实例下进程时钟会分叉。
        """
        result = self._session.execute(
            update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.state == DELIVERY_STATE_PREPARED,
                Delivery.lock_version == expected_lock_version,
            )
            .values(
                state=DELIVERY_STATE_DELIVERED,
                delivered_at=func.now(),
                delivered_by=delivered_by,
                lock_version=Delivery.lock_version + 1,
            ),
            execution_options={"synchronize_session": False},
        )
        return int(result.rowcount or 0)

    def mark_cancelled(
        self, *, delivery_id: uuid.UUID, expected_lock_version: int
    ) -> int:
        """`PREPARED → CANCELLED`（条件 UPDATE），返回受影响行数。"""
        result = self._session.execute(
            update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.state == DELIVERY_STATE_PREPARED,
                Delivery.lock_version == expected_lock_version,
            )
            .values(
                state=DELIVERY_STATE_CANCELLED,
                cancelled_at=func.now(),
                lock_version=Delivery.lock_version + 1,
            ),
            execution_options={"synchronize_session": False},
        )
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------
    # publications
    # ------------------------------------------------------------------

    def insert_publication(self, **columns: Any) -> Publication:
        row = Publication(**columns)
        self._session.add(row)
        return row

    def get_publication(self, publication_id: uuid.UUID) -> Publication | None:
        return self._session.get(Publication, publication_id)

    def find_publication_by_idempotency(
        self, *, project_id: uuid.UUID, idempotency_key: str
    ) -> Publication | None:
        stmt = select(Publication).where(
            Publication.project_id == project_id,
            Publication.idempotency_key == idempotency_key,
        )
        return self._session.execute(stmt).scalars().first()

    def find_publication_by_key(
        self, *, project_id: uuid.UUID, publication_key: str
    ) -> Publication | None:
        """按业务主键找（`uq_publications_project_key` 保证唯一）。"""
        stmt = select(Publication).where(
            Publication.project_id == project_id,
            Publication.publication_key == publication_key,
        )
        return self._session.execute(stmt).scalars().first()

    def list_publications(
        self, project_id: uuid.UUID, *, limit: int
    ) -> list[Publication]:
        stmt = (
            select(Publication)
            .where(Publication.project_id == project_id)
            .order_by(Publication.created_at.desc(), Publication.id.desc())
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars().all())

    def set_publication_content_state(
        self, *, publication_id: uuid.UUID, content_state: str
    ) -> int:
        """写 `publications.content_state`（条件 UPDATE，`lock_version` +1），返回受影响行数。

        取值域由库侧 CHECK 与 `service` 的白名单双重把关（只有 VERIFIED / UNVERIFIED）。
        """
        result = self._session.execute(
            update(Publication)
            .where(Publication.id == publication_id)
            .values(
                content_state=content_state,
                lock_version=Publication.lock_version + 1,
            ),
            execution_options={"synchronize_session": False},
        )
        return int(result.rowcount or 0)

    def backfill_url_canonical(
        self, *, publication_id: uuid.UUID, url_canonical: str
    ) -> int:
        """仅当 `url_canonical IS NULL` 时写入（"首条链接"回填；已有值不覆盖）。"""
        result = self._session.execute(
            update(Publication)
            .where(
                Publication.id == publication_id,
                Publication.url_canonical.is_(None),
            )
            .values(
                url_canonical=url_canonical,
                lock_version=Publication.lock_version + 1,
            ),
            execution_options={"synchronize_session": False},
        )
        return int(result.rowcount or 0)

    def reload_publication(self, publication_id: uuid.UUID) -> Publication | None:
        """强制从库重读（条件 UPDATE 不同步 session）。"""
        return self._session.get(Publication, publication_id, populate_existing=True)

    # ------------------------------------------------------------------
    # publication_links（WP37）
    # ------------------------------------------------------------------

    def insert_publication_link(self, **columns: Any) -> PublicationLink:
        row = PublicationLink(**columns)
        self._session.add(row)
        return row

    def count_links_of_publication(self, publication_id: uuid.UUID) -> int:
        return int(
            self._session.execute(
                select(func.count())
                .select_from(PublicationLink)
                .where(PublicationLink.publication_id == publication_id)
            ).scalar_one()
        )

    def find_link_in_project(
        self, *, project_id: uuid.UUID, canonical_url: str
    ) -> PublicationLink | None:
        """项目内同 canonical 的那一行（`uq_publication_links_project_canonical` 保证至多一条）。"""
        stmt = select(PublicationLink).where(
            PublicationLink.project_id == project_id,
            PublicationLink.canonical_url == canonical_url,
        )
        return self._session.execute(stmt).scalars().first()

    def exists_link_in_other_project(
        self, *, project_id: uuid.UUID, canonical_url: str
    ) -> bool:
        """是否存在**别的项目**登记过同 canonical。只回答布尔，不返回对方是谁。"""
        stmt = (
            select(PublicationLink.id)
            .where(
                PublicationLink.project_id != project_id,
                PublicationLink.canonical_url == canonical_url,
            )
            .limit(1)
        )
        return self._session.execute(stmt).first() is not None

    def list_links_by_canonical(
        self,
        canonical_url: str,
        *,
        visible_to_user: uuid.UUID | None,
        limit: int,
    ) -> list[PublicationLink]:
        """按 canonical 跨项目查重。

        `visible_to_user=None` ⇒ 不限项目（仅管理者可传）；否则只返回该用户
        **有活跃成员关系**的项目里的命中，其他项目一行都不出现。
        """
        stmt = select(PublicationLink).where(
            PublicationLink.canonical_url == canonical_url
        )
        if visible_to_user is not None:
            # 跨模块只读走裸 SQL（平级模块不可互 import），列名即对方契约。
            member_projects = [
                r[0]
                for r in self._session.execute(
                    text(
                        "SELECT project_id FROM project_members"
                        " WHERE user_id = :uid AND removed_at IS NULL"
                    ),
                    {"uid": visible_to_user},
                ).all()
            ]
            stmt = stmt.where(PublicationLink.project_id.in_(member_projects))
        stmt = stmt.order_by(
            PublicationLink.created_at.desc(), PublicationLink.id.desc()
        ).limit(limit)
        return list(self._session.execute(stmt).scalars().all())

    def count_distinct_publications(
        self, project_id: uuid.UUID, *, canonical_url: str | None = None
    ) -> int:
        """项目内**按 distinct `publication_id`** 计数（一条发布挂多条链接只算一条）。

        `canonical_url` 给了 ⇒ 只数命中该 canonical 的发布；不给 ⇒ 数项目内有过链接的发布。
        只读函数，没有端点（契约没有）。
        """
        stmt = select(func.count(func.distinct(PublicationLink.publication_id))).where(
            PublicationLink.project_id == project_id
        )
        if canonical_url is not None:
            stmt = stmt.where(PublicationLink.canonical_url == canonical_url)
        return int(self._session.execute(stmt).scalar_one())


__all__ = ["DeliveryRepository"]
