"""解析模块的依赖注入（WP17）。

**为什么需要 `load_blob_with_repo` 而不是直接拿 `access.file_row.blob_id`**：
`FileAccess`（WP12）只带 `file_row` 与项目权限，不带 blob。
本模块需要 blob 的 `object_key`（读字节）、`mime_type`（选解析器）
与 `scan_status`（协议要求"只对扫描通过文件"）。

把它做成一个**独立函数**而不是在 api.py 里内联查库，理由是它要被
api 与证据脚本两处使用 —— 两处各写一遍"查 blob、没有就 404"
正是最容易漂移的重复（WP12 的 `resolve_project_access` 同一条教训）。

**为什么是 `(repo, access)` 而不是 `(access)`**：`FileAccess` 是 frozen
dataclass 且**刻意不持有 session** —— 权限对象会被传递与缓存，
把 session 生命周期绑上去会让"这个对象还能不能用"取决于它被谁拿过。
因此显式传 repo：**谁需要数据库访问，谁就显式拿 repo**。

**`JobRepoDep` 也在这里定义**：WP13 没有对外提供依赖注入项
（它没有 HTTP 端点，因此不需要）。本包是第一个需要 `JobRepository`
的 HTTP 层，所以由本包提供 —— 放在 `jobs/` 里会让 WP13 凭空多一个
只有下游才用的东西。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.files.dependencies import FileAccess
from rednote.modules.files.models import Blob
from rednote.modules.files.repository import FileRepository
from rednote.modules.identity.dependencies import DBSession
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.parsing.repository import ParseRepository
from rednote.platform.errors import ApiError, ErrorCode


def get_parse_repository(db: DBSession) -> ParseRepository:
    return ParseRepository(db)


ParseRepoDep = Annotated[ParseRepository, Depends(get_parse_repository)]


def get_job_repository(db: DBSession) -> JobRepository:
    return JobRepository(db)


JobRepoDep = Annotated[JobRepository, Depends(get_job_repository)]


def load_blob_with_repo(repo: FileRepository, access: FileAccess) -> Blob:
    """取文件对应的物理记录。**不存在即 404**（与文件本身同样的语义）。

    用 404 而不是 500：`file_objects.blob_id` 有外键且 `ondelete=RESTRICT`，
    所以"文件在但 blob 不在"在当前 schema 下不可能发生。真发生了，
    说明有人手工删过数据 —— 对调用方而言，这个文件确实"查不到"。
    """
    blob = repo.get_blob(access.file_row.blob_id)
    if blob is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "文件物理记录不存在。",
            details={"fileId": str(access.file_row.id)},
        )
    return blob


__all__ = [
    "JobRepoDep",
    "ParseRepoDep",
    "get_job_repository",
    "get_parse_repository",
    "load_blob_with_repo",
]
