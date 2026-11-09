"""文件与上传的依赖注入（WP12）。

**本模块解决的核心问题**：上传端点按 `uploadId` 寻址，而 `projectId`
不在路径里 —— 于是"这个用户能不能动这个上传"必须先用会话反查出
`project_id`，再走项目权限判定。

**为什么复用 `resolve_project_access`**：若不复用，本模块就要把
"查项目 → 管理者放行 → 查成员 → 非成员报 404"抄一遍，而
**"两处各判一次权限"正是最容易漂移的重复** —— 某天有人调整了
404/403 的取舍，只会改其中一处，另一处静默地给出不同的结论。
WP12 已经把那个函数抽到 `projects/dependencies.py` 供这里调用。

**存储后端按请求构造**：`LocalStorageBackend.__init__` 只做一次
`mkdir(exist_ok=True)`，代价可忽略。相比在 `app.state` 上挂单例，
按请求构造的好处是"配置改了不用重启"在测试里天然成立 ——
测试注入不同 `storage_local_root` 时不会拿到上一个测试的实例。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path as FsPath
from typing import Annotated

from fastapi import Depends, Path, Request

from rednote.modules.files.models import (
    VISIBILITY_PROJECT,
    FileObject,
    UploadSession,
)
from rednote.modules.files.repository import FileRepository
from rednote.modules.files.service import require_session
from rednote.modules.files.storage import StorageBackend, build_storage_backend
from rednote.modules.identity.dependencies import CurrentUserDep, DBSession
from rednote.modules.projects.dependencies import (
    ProjectAccess,
    ProjectRepoDep,
    resolve_project_access,
)
from rednote.platform.configuration import get_app_settings


def get_file_repository(db: DBSession) -> FileRepository:
    return FileRepository(db)


FileRepoDep = Annotated[FileRepository, Depends(get_file_repository)]


def get_storage_backend(request: Request) -> StorageBackend:
    """按配置构造存储后端。

    配置取自 `request.app.state.settings`（与 `get_db_session` 同一通道）——
    不是全局单例，否则"app 用注入配置装配、存储却按环境变量连"会成为
    第二套真相（WP08 已踩过这个坑）。
    """
    settings = get_app_settings(request.app)
    # ⚠️ 必须用 `FsPath`（pathlib）：本文件同时 import 了 `fastapi.Path`，
    # 同名会让 `Path(root)` 静默变成 FastAPI 的路径参数声明，
    # 一旦配置了本地存储根就在请求期抛 "Path parameters cannot have a default value"。
    root = (
        FsPath(settings.storage_local_root) if settings.storage_local_root else None
    )
    return build_storage_backend(backend=settings.storage_backend, local_root=root)


StorageBackendDep = Annotated[StorageBackend, Depends(get_storage_backend)]


@dataclass(frozen=True)
class UploadAccess:
    """已通过权限校验的上传会话访问上下文。

    与非成员的处理与项目端点一致：**返回 404 而不是 403**，
    使"会话不存在"与"无权知道它存在"不可区分。
    """

    session_row: UploadSession
    project_access: ProjectAccess

    @property
    def project_id(self) -> uuid.UUID:
        return self.session_row.project_id

    @property
    def upload_id(self) -> uuid.UUID:
        return self.session_row.id

    def require_write(self) -> None:
        """要求"能对这个项目写入资料"。

        **为什么只判成员身份、不判具体 ProjectAction**：
        需求 §2.3 的权限矩阵里**没有"上传文件"这一行** ——
        这是一个需求缺口（已登记为待定项 OP-100）。
        矩阵里的 `EDIT` 指的是"编辑 Brief、方案、内容"（项目的**产物**），
        而研究资料是**输入**；用 `EDIT` 做判据会让只有 EDIT 权限的人
        能上传、而其他成员不能，这个结论没有需求依据。

        因此当前口径是：**能访问这个项目，就能往里放研究资料**。
        要收紧的话只需改这一个方法 —— 这正是把它做成方法而不是
        散在各端点里的理由。
        """


def load_upload_access(
    user: CurrentUserDep,
    repo: FileRepoDep,
    project_repo: ProjectRepoDep,
    upload_id: Annotated[uuid.UUID, Path(alias="uploadId")],
) -> UploadAccess:
    """按 `uploadId` 加载会话并校验项目权限。

    **顺序不可调换**：先取会话（不存在 → 404），再判项目权限
    （非成员 → 同一个 404 文案）。反过来先判权限的话，
    就得先知道会话的 `project_id`，而那正是要取的东西。
    """
    session_row = require_session(repo, upload_id)
    access = resolve_project_access(
        project_repo, project_id=session_row.project_id, user=user
    )
    return UploadAccess(session_row=session_row, project_access=access)


UploadAccessDep = Annotated[UploadAccess, Depends(load_upload_access)]


@dataclass(frozen=True)
class FileAccess:
    """已通过权限校验的文件访问上下文。

    `project_access` 为 `None` 表示这是**知识库文件**（`visibility=KNOWLEDGE`）——
    它不属于任何项目，因此没有项目权限可判。当前 WP12 只产生
    `PROJECT` 可见性的文件，`KNOWLEDGE` 分支是为 WP20（知识库）预留的，
    现在就把两种情形写清楚，比将来在这里加一个 `if` 更省事。
    """

    file_row: FileObject
    project_access: ProjectAccess | None

    @property
    def file_id(self) -> uuid.UUID:
        return self.file_row.id


def load_file_access(
    user: CurrentUserDep,
    repo: FileRepoDep,
    project_repo: ProjectRepoDep,
    file_id: Annotated[uuid.UUID, Path(alias="fileId")],
) -> FileAccess:
    """按 `fileId` 加载文件并校验可见性。"""
    file_row = repo.get_file_object(file_id)
    if file_row is None:
        raise _file_not_found(file_id)

    if file_row.visibility != VISIBILITY_PROJECT:
        # 知识库文件：全员可见（知识库是共享主数据）。
        return FileAccess(file_row=file_row, project_access=None)

    assert file_row.project_id is not None  # CHECK 保证 PROJECT 必有项目
    access = resolve_project_access(
        project_repo, project_id=file_row.project_id, user=user
    )
    return FileAccess(file_row=file_row, project_access=access)


def _file_not_found(file_id: uuid.UUID):
    from rednote.platform.errors import ApiError, ErrorCode

    return ApiError(
        ErrorCode.RESOURCE_NOT_FOUND,
        "文件不存在或无权访问。",
        details={"resourceId": str(file_id)},
    )


FileAccessDep = Annotated[FileAccess, Depends(load_file_access)]


__all__ = [
    "FileAccess",
    "FileAccessDep",
    "FileRepoDep",
    "StorageBackendDep",
    "UploadAccess",
    "UploadAccessDep",
    "get_file_repository",
    "get_storage_backend",
    "load_file_access",
    "load_upload_access",
]
