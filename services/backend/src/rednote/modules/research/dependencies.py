"""研究模块的依赖注入（WP19）。

**本模块存在的理由**（与其余包一致）：端点的校验与 `service` 的调用
必须共用同一个 session。端点自己造一个 `ResearchRepository`、
service 又拿另一个，"端点刚判过的那个 Brief 版本"与"service 读的"
就可能漂移（WP17 的 ISS-54：事件处理器连到了环境变量里的库）。

| 依赖 | 提供 | 谁用 |
|---|---|---|
| `ResearchRepoDep` | `ResearchRepository` | 两个端点；`service.list_research_files` 会从它身上取 session 复用同一个 |

**刻意不提供"研究专用权限依赖"**：两个端点都是**读**，权限就是
`ProjectAccessDep`（它已判"项目存在 + 当前用户能访问"）+
`access.require(ProjectAction.VIEW)`。再包一层本模块自己的权限依赖，
会让"谁能看研究页"这条规则有两处定义 —— 而两处一旦不一致，
**放宽的那一处会生效**（因为它是先被调用的那层，同 WP20 的判据）。

**为什么不需要 `FileRepoDep` / `ParseRepoDep`**：本包的 service
不直接调 `FileRepository` / `ParseRepository` 的读取方法 ——
它需要的是"文件 + 关联 + blob + 最新解析 + 片段数"这个**联合视图**，
而那由 `ResearchRepository` 的一条 SQL 给出（理由见 repository.py
的 docstring 第一节：N+1 与一致性）。
配额那两个数确实走 `FileRepository`，但那是 service 内部
在**同一个 session** 上临时构造的（见 `service.list_research_files`），
不需要端点注入。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.identity.dependencies import DBSession
from rednote.modules.research.repository import ResearchRepository


def get_research_repository(db: DBSession) -> ResearchRepository:
    return ResearchRepository(db)


ResearchRepoDep = Annotated[ResearchRepository, Depends(get_research_repository)]


__all__ = [
    "ResearchRepoDep",
    "get_research_repository",
]