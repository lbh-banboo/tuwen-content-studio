"""场景形式草稿与数量配置的依赖注入（WP24）。

**本模块存在的理由**：端点与 `service` 必须共用同一个 session。
本包比一般包更在意这件事，因为 **WP24 的读端点会借用写仓储**：

- `GET /projects/{projectId}/plan-draft` 名为读，实际可能**惰性物化**一张草稿
  （`service.require_draft` 在草稿不存在时插入一行再重读）。
  若端点用一个 session、`service` 用另一个，"刚插入的那行"在端点的事务里
  根本不存在 —— 端点会 404 或读到旧快照，而 `service` 那边事务提交后
  数据库里确实有行。表现为"第一次 GET 404，第二次 GET 有数据"。

- `PUT /settings/global/{key}` 的**级联刷新副本**（§3.6.2）要在
  **同一个事务**里把 N 个项目副本一起改掉。若 `service` 拿到的不是
  请求的那个 session，"全局已改、副本没跟上"就会**提交成功且不报错** ——
  正是判据 J2 要拦的那个形态。

| 依赖 | 提供 | 谁用 |
|---|---|---|
| `PlanningRepoDep` | `PlanningRepository` | 全部 10 个端点 |
| `VersionRepoDep` | `VersionRepository` | 只有 `POST .../plan-draft/submissions`（`submit_draft` 要注册版本节点） |

## 为什么提供 `VersionRepoDep`，而 WP20/WP22/WP23 也提供了同一个名字

四个模块各自定义一个 `VersionRepoDep`，看起来是重复。**刻意如此**：

`rednote.modules.baseline.dependencies.VersionRepoDep` 是那个模块的依赖别名。
若本模块去 import 它，Planning 模块就多了一条**指向 baseline 模块的 import** ——
而这两个模块之间**没有任何业务关系**（审核基线是审核基线，方案排期是方案排期）。
一条只为"少写三行"而建的跨模块依赖，会在将来"baseline 改结构"时
变成 Planning 的编译错误。

`Annotated[..., Depends(...)]` 是**类型别名**，不是单例 ——
四处各写一份的代价是零，收益是模块之间不产生假依赖（与 WP22/WP23 同一判断）。

## 为什么只有 `submit_draft` 需要 `VersionRepoDep`

`register_version` 是**唯一**写 `version_nodes` 的入口（WP11 接缝），
而本包只有"提交草稿固化成 PLAN 版本"这一个动作会往注册表里加节点。
其余写动作（配置变更 / 草稿保存）都只写本包自己的两张表。

**不把它做成 `settings_service` / `service` 内部的工厂**：这两个模块拿不到
`db` session（它们只接收仓储实例），若在业务层
`from ...identity.dependencies import get_db_session` 就需要 session 来源 ——
那会把"依赖从哪来"这个问题下沉到业务层。由端点注入是唯一干净的位置。

## 刻意不提供"Planning 专用权限依赖"

与 WP22/WP23 同一条取向：**不新建 `ProjectAction`**。
本包的权限需求全部落在既有动作上：

- 读配置 / 读草稿 = `VIEW`
- 改草稿 / 提交草稿 = `EDIT`（草稿是"方案内容"的输入，
  与 WP18 `PUT /brief-draft` 同款；**不用 `REVIEW`** ——
  那是 WP22 审核工作区的动作）
- 改全局配置 = `OrgAction.KNOWLEDGE_MANAGE`
  （`_ORG_MATRIX[ROLE_OPERATOR][KNOWLEDGE_MANAGE] = True`，
  是运营唯一开放的组织级操作；`MANAGE_DICTIONARY` 对运营为 False，
  用它会把"生成部运营改批次上限"这条路径整条堵死）

多一个动作就多一处矩阵定义，而两处一旦不一致，
**放宽的那一处会生效**（因为它是先被调用的那层）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.identity.dependencies import DBSession
from rednote.modules.planning.repository import PlanningRepository
from rednote.modules.versions.repository import VersionRepository


def get_planning_repository(db: DBSession) -> PlanningRepository:
    return PlanningRepository(db)


PlanningRepoDep = Annotated[PlanningRepository, Depends(get_planning_repository)]


def get_version_repository(db: DBSession) -> VersionRepository:
    """版本仓库（`submit_draft` → `register_version` 的输入）。

    **与 `submit_draft` 内部会造的那个是同一个 session 上的两个实例** ——
    本模块不缓存实例，因为 `VersionRepository` 是无状态的薄封装
    （它只持有 session），多造一个的代价是零，而缓存会引入
    "这个实例对应哪个 session"的生命周期问题（与 WP22/WP23 同款说明）。
    """
    return VersionRepository(db)


VersionRepoDep = Annotated[VersionRepository, Depends(get_version_repository)]


__all__ = [
    "PlanningRepoDep",
    "VersionRepoDep",
    "get_planning_repository",
    "get_version_repository",
]
