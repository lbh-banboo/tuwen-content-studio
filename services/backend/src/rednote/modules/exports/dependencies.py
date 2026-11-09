"""固定版本 ZIP 导出的依赖注入（WP35）。

只提供本包端点真正消费的三样：

| 依赖 | 提供 | 谁用 |
|---|---|---|
| `ExportRepoDep` | `ExportRepository` | 全部端点 |
| `FileRepoDep` | `FileRepository` | 创建导出（落产物文件） |
| `StorageBackendDep` | 存储后端 | 创建导出（读图 + 写 ZIP）、下载、核验 |

后两者**直接复用 `files.dependencies` 的别名**：`FileRepoDep` / `StorageBackendDep`
就是 `files` 对外给出的装配点，不是私有名；自己再写一份会让"存储怎么构造"
出现第二个真相（WP08 已踩过按环境变量另连的坑）。

权限：不新建 `ProjectAction`，用既有的 `DELIVER`（交付）。
`ProjectAccessDep` 只回答"能不能看到这个项目"，**每个端点必须自己 `access.require(...)`**。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.exports.repository import ExportRepository
from rednote.modules.files.dependencies import FileRepoDep, StorageBackendDep
from rednote.modules.identity.dependencies import DBSession


def get_export_repository(db: DBSession) -> ExportRepository:
    """请求级仓储（与端点同一个 session）。"""
    return ExportRepository(db)


ExportRepoDep = Annotated[ExportRepository, Depends(get_export_repository)]

__all__ = [
    "ExportRepoDep",
    "FileRepoDep",
    "StorageBackendDep",
    "get_export_repository",
]
