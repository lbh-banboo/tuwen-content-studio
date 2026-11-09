"""Brief 模块的依赖注入（WP18）。

**本模块存在的理由**：端点的校验与 `service` 的调用必须共用同一个 session。
若端点自己 `BriefRepository(get_session())`、`service` 又拿另一个，
"端点刚校验过的那个草稿"与"service 改的那个"就会漂移 ——
本机实测过同类的坑（WP17 的 ISS-54：事件处理器连到了环境变量里的库）。
因此仓库的构造只写一次，端点与 service 都用它。

| 依赖 | 提供 | 谁用 |
|---|---|---|
| `BriefRepoDep` | `BriefRepository` | 全部五个端点 |
| `ParseRepoDep` | `ParseRepository` | 解析端点（登记 run）、`GET /brief`（读片段） |
| `FileRepoDep` | `FileRepository` | 解析端点（判"是 BRIEF 用途"） |
| `JobRepoDep` | `JobRepository` | 两个入队端点 |

**刻意不提供 `VersionRepoDep`**：固化端点需要节点的 `etag` / `revision_no`，
但那是 `service.solidify` 的返回值（`SolidifiedVersion` 已经把两者都带出来了）。
再给端点一个仓库，就会有两条路径去读同一份数据 ——
其中一条迟早会漏掉 service 里的判定。
`VersionRepository` 的构造在 `service.py` 内部（它需要与业务判定同事务）。

**刻意不提供 `DecomposerDep`**：**端点不调解构**。协议 §5 要求两个入队端点
返回 202，真正调解构的是 Worker 里的任务处理器（那里才有 `job_id` /
`attempt_id`，网关要求它们才能写调用留痕）。端点上的解构器依赖没有任何
消费点 —— 留着一个"看起来可以注入"的依赖，会让读者以为端点会调解构。
处理器侧的注入走 `handlers.build_task_registry(decomposer=...)`。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.brief.repository import BriefRepository
from rednote.modules.files.repository import FileRepository
from rednote.modules.identity.dependencies import DBSession
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.parsing.repository import ParseRepository


def get_brief_repository(db: DBSession) -> BriefRepository:
    return BriefRepository(db)


BriefRepoDep = Annotated[BriefRepository, Depends(get_brief_repository)]


def get_parse_repository(db: DBSession) -> ParseRepository:
    return ParseRepository(db)


ParseRepoDep = Annotated[ParseRepository, Depends(get_parse_repository)]


def get_file_repository(db: DBSession) -> FileRepository:
    return FileRepository(db)


FileRepoDep = Annotated[FileRepository, Depends(get_file_repository)]


def get_job_repository(db: DBSession) -> JobRepository:
    return JobRepository(db)


JobRepoDep = Annotated[JobRepository, Depends(get_job_repository)]


__all__ = [
    "BriefRepoDep",
    "FileRepoDep",
    "JobRepoDep",
    "ParseRepoDep",
    "get_brief_repository",
    "get_file_repository",
    "get_job_repository",
    "get_parse_repository",
]
