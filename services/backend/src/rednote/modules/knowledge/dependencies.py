"""知识模块的依赖注入（WP20）。

**本模块存在的理由**：端点的校验与 `service` 的调用必须共用同一个 session。
若端点自己 `KnowledgeRepository(get_session())`、`service` 又拿另一个，
"端点刚校验过的那条知识"与"service 改的那条"就会漂移 ——
本机实测过同类的坑（WP17 的 ISS-54：事件处理器连到了环境变量里的库）。
因此仓库的构造只写一次，端点与 service 都用它。

| 依赖 | 提供 | 谁用 |
|---|---|---|
| `KnowledgeRepoDep` | `KnowledgeRepository` | 全部九个端点 |
| `VersionRepoDep` | `VersionRepository` | 引用摘要端点（读 `version_dependencies`） |

**刻意不提供 `PermissionDep` 这类"知识专用权限依赖"**：
知识的写权限就是 `projects.dependencies.require_org_action(OrgAction.KNOWLEDGE_MANAGE)`
（需求 §2.2 的"全员知识库" → 运营与管理者都允许）。
再包一层本模块自己的权限依赖，会让"知识谁能改"这条规则有两处定义 ——
而两处一旦不一致，**放宽的那一处会生效**（因为它是先被调用的那层）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.identity.dependencies import DBSession
from rednote.modules.knowledge.repository import KnowledgeRepository
from rednote.modules.versions.repository import VersionRepository


def get_knowledge_repository(db: DBSession) -> KnowledgeRepository:
    return KnowledgeRepository(db)


KnowledgeRepoDep = Annotated[KnowledgeRepository, Depends(get_knowledge_repository)]


def get_version_repository(db: DBSession) -> VersionRepository:
    """版本仓库（引用摘要端点用）。

    **与 `service.publish` 内部造的那个是同一个 session 上的两个实例** ——
    本模块不缓存实例，因为 `VersionRepository` 是无状态的薄封装
    （它只持有 session），多造一个的代价是零，而缓存会引入
    "这个实例对应哪个 session"的生命周期问题。
    """
    return VersionRepository(db)


VersionRepoDep = Annotated[VersionRepository, Depends(get_version_repository)]


__all__ = [
    "KnowledgeRepoDep",
    "VersionRepoDep",
    "get_knowledge_repository",
    "get_version_repository",
]