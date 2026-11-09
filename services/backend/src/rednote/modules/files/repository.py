"""文件、配额与上传会话的存储访问（WP12）。

**本层最要紧的一件事**：配额的每一次变动都是**单条条件 UPDATE**，
不允许出现"先 SELECT 查余量、再 UPDATE 扣减"。

原因（AC-06 的全部含金量都在这）：两个并发请求各自读到
"还剩 100 MB"，各自判断"我要传的 80 MB 放得下"，然后各自扣减 ——
结果账面 160 MB 进了 100 MB 的余量，**超限且没有任何一方察觉**。
把判断条件放进 `WHERE` 之后，两条语句在数据库层串行化：
后执行的那条会因为条件不成立而 `rowcount = 0`，调用方据此报配额不足。

**为什么不用 `SELECT ... FOR UPDATE`**：它同样正确，但会把"读"与"写"
分成两条语句、并在读时就持有行锁。条件 UPDATE 是一条语句拿到锁，
锁持有时间更短，且在只有一次往返这一点上更简单。两者选一是实现偏好，
但**"条件写进 WHERE"这个性质不能少**。

**为什么释放预占也要走条件 UPDATE**：文档 02 §13.1 要求
"每次释放只允许一次"。若用 `SET reserved = reserved - :delta` 无条件下发，
重复的取消请求会把额度**多还一次**（甚至还成负数被 CHECK 挡住 → 500）。
因此所有释放路径都先经过 `transition_session()` 改状态，只有
`rowcount = 1`（即状态确实迁移了）才去动账面。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from rednote.modules.files.models import (
    Blob,
    FileObject,
    ProjectFileLink,
    QuotaAccount,
    UploadPart,
    UploadSession,
)
from rednote.modules.outbox.models import OutboxEvent


class FileRepository:
    """文件与配额的读写入口。事务边界由调用方（service / API 层）控制。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        """本仓库持有的 session。

        **为什么公开它**（与 `JobRepository.session` 同一条理由）：
        `GET /files/{fileId}` 要顺带返回解析状态（WP17 扩展），
        而解析状态在另一张表里。若为此新开一个 session，
        同一个响应里的"文件元数据"与"解析状态"会来自**两个不同事务**
        （可能跨越一次提交），排障时无法解释"为什么两个字段对不上"。
        复用请求级 session 让整个响应来自同一个时点。
        """
        return self._session

    def flush(self) -> None:
        """把待写入的变更立即发给数据库。

        用途与 WP11 的 `VersionRepository.flush` 一致：让约束错误在
        **预期位置**抛出，而不是等到事务提交时才炸 ——
        提交发生在 `session_scope` 的退出处，那里已经离开了能给出
        可读 4xx 的代码位置，错误只会以 500 的形式冒出去。
        """
        self._session.flush()

    # ------------------------------------------------------------------
    # 配额
    # ------------------------------------------------------------------

    def ensure_quota_account(self, project_id: uuid.UUID) -> None:
        """保证项目有配额账户（缺则按默认上限建一个）。

        用 `ON CONFLICT DO NOTHING` 而不是"先查再插"：并发下两个请求
        同时发现"没有账户"，先查再插会让其中一个撞唯一约束报 500。
        这条语句是幂等的，重复调用无副作用。
        """
        self._session.execute(
            pg_insert(QuotaAccount)
            .values(project_id=project_id)
            .on_conflict_do_nothing(index_elements=["project_id"])
        )

    def get_quota_account(self, project_id: uuid.UUID) -> QuotaAccount | None:
        return self._session.get(QuotaAccount, project_id)

    def reserve_quota(self, *, project_id: uuid.UUID, delta: int) -> bool:
        """原子预占 `delta` 字节。放得下返回 True，否则 False。

        条件 `effective_bytes + reserved_bytes + :delta <= limit_bytes`
        **必须在本条语句的 WHERE 里**（见模块 docstring）。
        调用方拿到 False 即报配额不足，不需要也不应该再查一次余量 ——
        再查一次读到的是"别人已经改过"的账面，反而误导。
        """
        result = self._session.execute(
            update(QuotaAccount)
            .where(
                QuotaAccount.project_id == project_id,
                QuotaAccount.effective_bytes
                + QuotaAccount.reserved_bytes
                + delta
                <= QuotaAccount.limit_bytes,
            )
            .values(
                reserved_bytes=QuotaAccount.reserved_bytes + delta,
                updated_at=func.now(),
            )
        )
        return result.rowcount == 1

    def release_reservation(self, *, project_id: uuid.UUID, delta: int) -> None:
        """归还预占。**只应由持有预占的状态迁移触发一次**。

        `WHERE reserved_bytes >= :delta` 是防"还多了"的最后一道：
        正常流程下不会触发，但若某条路径被重复调用（或将来有人绕过
        状态机调用本方法），这里会让 UPDATE 不生效而不是把账面扣成负数。
        负值会被 CHECK 挡住并变成 500 —— 一个"静默少还"变成"显式崩溃"，
        对排障更友好，但仍不如直接不发生。
        """
        if delta <= 0:
            return
        self._session.execute(
            update(QuotaAccount)
            .where(
                QuotaAccount.project_id == project_id,
                QuotaAccount.reserved_bytes >= delta,
            )
            .values(
                reserved_bytes=QuotaAccount.reserved_bytes - delta,
                updated_at=func.now(),
            )
        )

    def finalize_reservation(
        self,
        *,
        project_id: uuid.UUID,
        reserved_release: int,
        effective_add: int,
    ) -> bool:
        """把预占转成有效占用，允许两者不相等（按实际大小修正）。

        `reserved_release` 是"当初预占了多少"，`effective_add` 是"实际该记多少"。
        两者相等是常态；不相等时本条语句顺带把差额还回余量 —— 这正是
        声明大小与实际大小不一致时的处理方式（OP-93 的选定口径）。

        条件里同时校验归还量足够、且转换后仍不超限：
        实际比声明大且超出余量时，返回 False，调用方报 `UPLOAD_TOO_LARGE`。
        两次检查仍是一条语句，不存在中间窗口。
        """
        result = self._session.execute(
            update(QuotaAccount)
            .where(
                QuotaAccount.project_id == project_id,
                QuotaAccount.reserved_bytes >= reserved_release,
                QuotaAccount.effective_bytes
                + QuotaAccount.reserved_bytes
                - reserved_release
                + effective_add
                <= QuotaAccount.limit_bytes,
            )
            .values(
                reserved_bytes=QuotaAccount.reserved_bytes - reserved_release,
                effective_bytes=QuotaAccount.effective_bytes + effective_add,
                updated_at=func.now(),
            )
        )
        return result.rowcount == 1

    def compute_effective_bytes(self, project_id: uuid.UUID) -> int:
        """按文件关联**重算**有效占用字节数（对账用，不参与写入路径）。

        文档 02 §13.1 要求"定时对账按文件关联重建账面并报警"。
        重算的口径必须与 `finalize_reservation` 累加的口径一致，
        否则对账会永远报差异：这里是
        `Σ blobs.byte_size JOIN project_file_links(project, purpose ∈ 配额用途, effective)`。
        """
        from rednote.modules.files.models import QUOTA_PURPOSES

        stmt = (
            select(func.coalesce(func.sum(Blob.byte_size), 0))
            .select_from(ProjectFileLink)
            .join(FileObject, FileObject.id == ProjectFileLink.file_id)
            .join(Blob, Blob.id == FileObject.blob_id)
            .where(
                ProjectFileLink.project_id == project_id,
                ProjectFileLink.effective.is_(True),
                ProjectFileLink.purpose.in_(sorted(QUOTA_PURPOSES)),
            )
        )
        return int(self._session.execute(stmt).scalar_one())

    # ------------------------------------------------------------------
    # 上传会话
    # ------------------------------------------------------------------

    def insert_session(
        self,
        *,
        session_id: uuid.UUID,
        project_id: uuid.UUID,
        purpose: str,
        original_name: str,
        classification: str,
        declared_size: int,
        reserved_bytes: int,
        state: str,
        expires_at: datetime,
        temporary_key: str,
        created_by: uuid.UUID,
        expected_sha256: str | None,
    ) -> UploadSession:
        """插入会话。

        `session_id` 由调用方生成而不是等 `gen_random_uuid()`：
        `temporary_key` 是 NOT NULL 且**由 session id 派生**，
        插入时就必须要它。等数据库生成再回来改，会多一次 UPDATE，
        也会让"分片该写到哪"在事务中途才确定。
        """
        session_row = UploadSession(
            id=session_id,
            project_id=project_id,
            purpose=purpose,
            original_name=original_name,
            classification=classification,
            declared_size=declared_size,
            reserved_bytes=reserved_bytes,
            received_bytes=0,
            state=state,
            expires_at=expires_at,
            temporary_key=temporary_key,
            created_by=created_by,
            expected_sha256=expected_sha256,
        )
        self._session.add(session_row)
        return session_row

    def get_session(self, session_id: uuid.UUID) -> UploadSession | None:
        """取会话，**带行锁**（`FOR UPDATE`）。

        完成上传与取消上传都是"读状态 → 判状态 → 写状态"的序列，
        且必须与配额账面一起保持一致。取行锁让同一会话的并发请求串行，
        避免两个 `completions` 同时进入"我来固化"分支从而产生两份 blob。
        不同会话之间不互相阻塞（锁的是行不是表），因此不影响并发上传。
        """
        return self._session.get(UploadSession, session_id, with_for_update=True)

    def get_session_readonly(self, session_id: uuid.UUID) -> UploadSession | None:
        """取会话，不加锁。用于纯查询（GET /uploads/{id}）。"""
        return self._session.get(UploadSession, session_id)

    def transition_session(
        self,
        *,
        session_id: uuid.UUID,
        from_states: Sequence[str],
        to_state: str,
        payload_hash: str | None = None,
        actual_sha256: str | None = None,
        received_bytes: int | None = None,
        file_id: uuid.UUID | None = None,
    ) -> bool:
        """条件状态迁移。**返回是否真的迁移了**。

        这是"每次释放只允许一次"的实现点：所有会动账面的路径都先调它，
        `rowcount == 1` 才继续。重复请求（第二次取消、重复完成）
        拿到 False，直接返回已有结果，不再触碰账面。

        `file_id` 只在完成路径上传入；`payload_hash` / `actual_sha256` /
        `received_bytes` 同理，用 None 表示"本次不改这一列"。
        """
        values: dict[str, Any] = {"state": to_state, "updated_at": func.now()}
        if actual_sha256 is not None:
            values["actual_sha256"] = actual_sha256
        if received_bytes is not None:
            values["received_bytes"] = received_bytes
        if file_id is not None:
            values["file_id"] = file_id

        result = self._session.execute(
            update(UploadSession)
            .where(
                UploadSession.id == session_id,
                UploadSession.state.in_(list(from_states)),
            )
            .values(**values)
        )
        return result.rowcount == 1

    def list_expired_sessions(
        self, *, now: datetime, limit: int, holding_states: Sequence[str]
    ) -> list[UploadSession]:
        """已过期、且预占还挂在账上的会话（TTL 回收的输入）。

        只捞"还在持有预占"的状态：已完结的会话即使过期也无需处理，
        把它们一起查出来只会让回收循环做无用功。
        """
        stmt = (
            select(UploadSession)
            .where(
                UploadSession.expires_at <= now,
                UploadSession.state.in_(list(holding_states)),
            )
            .order_by(UploadSession.expires_at)
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # 分片
    # ------------------------------------------------------------------

    def get_part(self, *, session_id: uuid.UUID, part_no: int) -> UploadPart | None:
        return self._session.get(UploadPart, (session_id, part_no))

    def list_parts(self, session_id: uuid.UUID) -> list[UploadPart]:
        stmt = (
            select(UploadPart)
            .where(UploadPart.upload_id == session_id)
            .order_by(UploadPart.part_no)
        )
        return list(self._session.execute(stmt).scalars().all())

    def insert_part(
        self,
        *,
        session_id: uuid.UUID,
        part_no: int,
        byte_size: int,
        sha256: str,
        storage_etag: str | None,
    ) -> UploadPart:
        part = UploadPart(
            upload_id=session_id,
            part_no=part_no,
            byte_size=byte_size,
            sha256=sha256,
            storage_etag=storage_etag,
        )
        self._session.add(part)
        return part

    def delete_parts(self, session_id: uuid.UUID) -> None:
        """删除会话的全部分片行（取消/过期/完成时）。

        **存储上的对象由 `StorageBackend.delete_prefix()` 清理** ——
        数据库行与物理对象在两个地方，必须都清。只清一边的后果都是
        "账实不符"：只清行 → 磁盘上躺着半份文件；只清对象 → 分片表里
        留着指向不存在对象的记录（后续重传同片号会与幽灵记录比对）。

        用 ORM 的 `delete()` 而不是原生 SQL：本模块其余写操作都走 ORM，
        混用会让"这一处为什么不一样"变成每次评审都要重新判断的问题。
        """
        self._session.execute(
            delete(UploadPart).where(UploadPart.upload_id == session_id)
        )

    # ------------------------------------------------------------------
    # Blob / 文件对象 / 关联
    # ------------------------------------------------------------------

    def get_blob(self, blob_id: uuid.UUID) -> Blob | None:
        return self._session.get(Blob, blob_id)

    def get_or_create_blob(
        self,
        *,
        storage_backend: str,
        bucket: str | None,
        object_key: str,
        sha256: str,
        byte_size: int,
        mime_type: str,
    ) -> tuple[Blob, bool]:
        """按 `(storage_backend, object_key)` 取或建，返回 `(blob, created)`。

        `ON CONFLICT DO NOTHING` + 回查，而不是"先查再插"：并发上传同一份
        内容时（本地后端下同 sha256 → 同 key），两个请求会同时发现
        "不存在"并各自插入，其中一个撞唯一约束 —— 那会变成 500，
        而这件事在语义上完全正常（两个用户传了同一份资料）。

        **注意这只是物理去重，不免项目配额**（文档 02 §4）：
        调用方拿到 `created=False` 时仍然要为本次上传计入本项目的有效占用。
        """
        stmt = (
            pg_insert(Blob)
            .values(
                storage_backend=storage_backend,
                bucket=bucket,
                object_key=object_key,
                sha256=sha256,
                byte_size=byte_size,
                mime_type=mime_type,
            )
            .on_conflict_do_nothing(
                index_elements=["storage_backend", "object_key"]
            )
            .returning(Blob.id)
        )
        created_id = self._session.execute(stmt).scalar_one_or_none()
        if created_id is not None:
            blob = self._session.get(Blob, created_id)
            assert blob is not None  # 同一事务内刚插入，必然可见
            return blob, True

        existing = self._session.execute(
            select(Blob).where(
                Blob.storage_backend == storage_backend,
                Blob.object_key == object_key,
            )
        ).scalar_one()
        return existing, False

    def insert_file_object(
        self,
        *,
        blob_id: uuid.UUID,
        original_name: str,
        visibility: str,
        project_id: uuid.UUID | None,
        classification: str,
        uploaded_by: uuid.UUID,
    ) -> FileObject:
        file_row = FileObject(
            blob_id=blob_id,
            original_name=original_name,
            visibility=visibility,
            project_id=project_id,
            classification=classification,
            uploaded_by=uploaded_by,
        )
        self._session.add(file_row)
        return file_row

    def get_file_object(self, file_id: uuid.UUID) -> FileObject | None:
        return self._session.get(FileObject, file_id)

    def find_active_link(
        self, *, project_id: uuid.UUID, file_id: uuid.UUID, purpose: str
    ) -> ProjectFileLink | None:
        stmt = select(ProjectFileLink).where(
            ProjectFileLink.project_id == project_id,
            ProjectFileLink.file_id == file_id,
            ProjectFileLink.purpose == purpose,
            ProjectFileLink.effective.is_(True),
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def insert_link(
        self,
        *,
        project_id: uuid.UUID,
        file_id: uuid.UUID,
        purpose: str,
    ) -> ProjectFileLink:
        link = ProjectFileLink(
            project_id=project_id, file_id=file_id, purpose=purpose, effective=True
        )
        self._session.add(link)
        return link

    # ------------------------------------------------------------------
    # Outbox
    # ------------------------------------------------------------------

    def insert_outbox_event(
        self,
        *,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> OutboxEvent:
        """与业务变更**同事务**写入。调用方不加 commit，由外层事务统一提交。"""
        event = OutboxEvent(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
        )
        self._session.add(event)
        return event

    def list_unpublished_events(self, *, limit: int) -> list[OutboxEvent]:
        """未发布的事件（供 WP13 的投递器接上）。

        本包只写不读；这个方法让 WP13 不必知道事件的产生方是谁。
        """
        stmt = (
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.created_at)
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # 权限辅助
    # ------------------------------------------------------------------

    def project_exists(self, project_id: uuid.UUID) -> bool:
        from rednote.modules.projects.models import Project

        return (
            self._session.execute(
                select(Project.id).where(Project.id == project_id).limit(1)
            ).first()
            is not None
        )


__all__ = ["FileRepository"]
