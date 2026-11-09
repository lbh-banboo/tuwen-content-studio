"""步骤 3 审核与基线的依赖注入（WP22）。

**本模块存在的理由**：端点与 `service` 必须共用同一个 session。
若端点自己造一个 `BaselineRepository`、`service` 又拿另一个，
"端点刚校验过的那份草稿"与"service 改的那份"就会漂移 ——
而审核工作区的漂移形态特别隐蔽：`If-Match` 校验用的是
**端点读到的 `lock_version`**，条件 UPDATE 用的是 **service 读到的**。
两者若来自不同 session，前置校验会通过而条件 UPDATE 必然失败
（或反之），表现为"偶发 412"。（与 WP17 的 ISS-54 同族：
事件处理器连到了环境变量里的那个库，而不是请求用的那个。）

| 依赖 | 提供 | 谁用 |
|---|---|---|
| `BaselineRepoDep` | `BaselineRepository` | 全部 8 个端点 |
| `VersionRepoDep` | `VersionRepository` | 只有 `POST .../baselines`（`create_baseline` 要注册版本节点） |

## 为什么提供 `VersionRepoDep`，而 WP20/WP23 也提供了同一个名字

三个模块各自定义一个 `VersionRepoDep`，看起来是重复。**刻意如此**：

`rednote.modules.prompts.dependencies.VersionRepoDep` 是那个模块的依赖别名。
若本模块去 import 它，Baseline 模块就多了一条**指向 prompts 模块的 import** ——
而这两个模块之间**没有任何业务关系**（提示词是提示词，审核是审核）。
一条只为"少写三行"而建的跨模块依赖，会在将来"prompts 改结构"时
变成 Baseline 的编译错误。

`Annotated[..., Depends(...)]` 是**类型别名**，不是单例 ——
三处各写一份的代价是零，收益是模块之间不产生假依赖（与 WP23 同一判断）。

## 为什么只有 `create_baseline` 需要 `VersionRepoDep`

`register_version` 是**唯一**写 `version_nodes` 的入口（WP11 接缝），
而本包只有创建基线这一个动作会往注册表里加节点。
其余写动作（草稿保存 / 批准记录）都只写本包自己的表。

**不把它做成 `service` 内部的工厂**：`service` 拿不到 `db` session
（它只接收仓储实例），若在 `service` 里 `from ... import get_version_repository`
就需要 session 来源 —— 那会把"依赖从哪来"这个问题下沉到业务层。
由端点注入是唯一干净的位置。

## 刻意不提供"Baseline 专用权限依赖"

与 WP23 同一条取向：**不新建 `ProjectAction`**。
本包的权限需求全部落在既有动作上（读= `VIEW`，改草稿= `EDIT`，
确认与批准= `REVIEW`），判定走
`projects.dependencies.ProjectAccessDep` + `access.require(...)`。
多一个动作就多一处矩阵定义，而两处一旦不一致，
**放宽的那一处会生效**（因为它是先被调用的那层）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.baseline.repository import BaselineRepository
from rednote.modules.identity.dependencies import DBSession
from rednote.modules.versions.repository import VersionRepository


def get_baseline_repository(db: DBSession) -> BaselineRepository:
    return BaselineRepository(db)


BaselineRepoDep = Annotated[BaselineRepository, Depends(get_baseline_repository)]


def get_version_repository(db: DBSession) -> VersionRepository:
    """版本仓库（`create_baseline` → `register_version` 的输入）。

    **与 `service` 内部会造的那个是同一个 session 上的两个实例** ——
    本模块不缓存实例，因为 `VersionRepository` 是无状态的薄封装
    （它只持有 session），多造一个的代价是零，而缓存会引入
    "这个实例对应哪个 session"的生命周期问题（与 WP23 同款说明）。
    """
    return VersionRepository(db)


VersionRepoDep = Annotated[VersionRepository, Depends(get_version_repository)]


__all__ = [
    "BaselineRepoDep",
    "VersionRepoDep",
    "get_baseline_repository",
    "get_version_repository",
]
