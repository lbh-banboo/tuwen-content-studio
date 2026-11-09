"""文案结构生成与标题候选的依赖注入（WP26）。

**本模块存在的理由**：端点与 `service` 必须共用同一个 session。
本包对这件事的敏感度与 WP25 同为**结构性**的，而不是"偶尔借用"：

| 场景 | 若端点与 `service` 用两个 session | 症状 |
|---|---|---|
| `start_generation`（建 N 条身份行） | 端点读到的项目/幂等状态与 `service` 写的不一致 | 幂等键第二次数不到第一批 ⇒ **重复建 N 条内容**，且两次请求各拿到一组合法响应 |
| `adopt_title_candidate`（`register_version` + `insert_version` + `advance_projection`） | 注册表节点落在一个 session、内容版本落在另一个 | `version_nodes` 有节点而 `content_versions` 没有行 ⇒ 反向触发器报错，或更糟：**节点计数与内容版本数静默分叉** |
| `prepare_regeneration`（读蓝图 + 覆盖参数） | 读到的 `current_blueprint_id` 与写入时的那一版不是同一时刻 | 判据 `SCOPE_REQUIRES_BLUEPRINT` 在窗口里失效 ⇒ 只重做标题的内容进到文案调用后才报"缺 blueprint" |
| `read_content_detail`（5 张表 + `version_nodes` 一起读） | 五张表分属不同事务 | 同一个响应里的字段来自不同时刻（WP25 同款论证） |

第一行是本包最危险的一个：它是**批量**写（`content_count` 条），
而重复建行**不报错** —— 用户看到 202、看到 6 条内容，只是其中 3 条是上次请求的。

| 依赖 | 提供 | 谁用 |
|---|---|---|
| `ContentRepoDep` | `ContentRepository` | 全部六个端点 |
| `JobRepoDep` | `JobRepository` | 两个入队端点（发起 / 局部重做） |
| `VersionRepoDep` | `VersionRepository` | 只有 `POST .../title-adoptions`（`register_version` 要注册版本节点） |

## 为什么提供 `VersionRepoDep`，而 WP22/WP23/WP24 也提供了同一个名字

四个模块各自定义一个 `VersionRepoDep`，看起来是重复。**刻意如此**：

`rednote.modules.prompts.dependencies.VersionRepoDep` 是那个模块的依赖别名。
若本模块去 import 它，Content 模块就多了一条**指向 prompts 模块的 import** ——
而这两个模块之间**没有任何业务关系**（提示词是提示词，内容是内容）。

`Annotated[..., Depends(...)]` 是**类型别名**，不是单例 ——
四处各写一份的代价是零，收益是模块之间不产生假依赖
（与 WP22 `dependencies.py` 第 17~28 行同一判断）。

## 为什么只有采纳端点需要 `VersionRepoDep`

`register_version` 是**唯一**写 `version_nodes` 的入口（WP11 接缝），
而本包只有"采纳标题"这一个动作会在**没有模型调用**的情况下加节点。
生成路径的节点注册在 `execute_generation` 内部（那里已经有
`content_repo.session`，就地构造 `VersionRepository` 的代价与这里相同）。

**不把它做成 `service` 内部的工厂**：`service` 拿不到 `db` session
（它只接收仓储实例）。由端点注入是唯一干净的位置 ——
与 WP22 `dependencies.py` 第 36~39 行同一条理由。

## 刻意不提供"Content 专用权限依赖"

与 WP22/WP23/WP24/WP25 同一条取向：**不新建 `ProjectAction` / `OrgAction`**。
本包的权限需求全部落在**既有**动作上：

| 动作用途 | 动作 | 依据 |
|---|---|---|
| 读内容列表 / 详情 / 版本历史（`GET`） | `ProjectAction.VIEW` | `permissions.py:82`"查看项目与其内容" —— **"内容"就是本包** |
| 发起生成 / 局部重做 / 采纳标题（`POST`） | `ProjectAction.EDIT` | `permissions.py:84`"编辑 Brief、方案、**内容**" —— 明写"内容" |

判定走 `projects.dependencies.ProjectAccessDep` + `access.require(...)`。

## ⚠️ `ProjectAccessDep` 只做**成员级**判定，动作级必须显式 `require`

`resolve_project_access` 回来的 `ProjectAccess` 回答的是
"这个人**能不能看到**这个项目"，**不回答**"他能不能改它"。
`ProjectPermissions.allows` 里的动作矩阵是**另一层**：

    项目成员（非管理员）→ `access.require(ProjectAction.VIEW)` 通过、`EDIT` 视角色而定
    管理员              → 两句都通过（`allows` 第一句就是 `if self.is_admin: return True`）

**每个端点都必须自己调一次 `access.require(...)`** ——
少写一次就是"任何项目成员都能发起生成"，而那是**花钱**的动作。
这一点在 WP25 的 `api.py` 里已按"两步骤鉴权"落地，本包照做：
先由资源 id 反查 `project_id`（不存在 ⇒ 404），再 `resolve_project_access`
+ `access.require(...)`。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.content.repository import ContentRepository
from rednote.modules.identity.dependencies import DBSession
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.versions.repository import VersionRepository


def get_content_repository(db: DBSession) -> ContentRepository:
    """请求级仓储实例（与端点 session 同源）。"""
    return ContentRepository(db)


#: 全部六个端点共用。
ContentRepoDep = Annotated[ContentRepository, Depends(get_content_repository)]


def get_job_repository(db: DBSession) -> JobRepository:
    """任务仓库（两个 202 端点的入队输入）。

    **与 `jobs` 模块自己的 `JobRepoDep` 是同一个 session 上的两个实例** ——
    本模块不 import 它的别名，理由与 `VersionRepoDep` 同：
    那会在 Content 与 Jobs 之间建一条"只为少写三行"的 import。
    `JobRepository` 是无状态薄封装（只持有 session），多造一个的代价是零。
    """
    return JobRepository(db)


JobRepoDep = Annotated[JobRepository, Depends(get_job_repository)]


def get_version_repository(db: DBSession) -> VersionRepository:
    """版本仓库（`adopt_title_candidate` → `register_version` 的输入）。

    **与 `service` 内部会造的那个是同一个 session 上的两个实例** ——
    本模块不缓存实例，因为 `VersionRepository` 是无状态的薄封装
    （它只持有 session），多造一个的代价是零，而缓存会引入
    "这个实例对应哪个 session"的生命周期问题（与 WP22 同款说明）。
    """
    return VersionRepository(db)


VersionRepoDep = Annotated[VersionRepository, Depends(get_version_repository)]


__all__ = [
    "ContentRepoDep",
    "JobRepoDep",
    "VersionRepoDep",
    "get_content_repository",
    "get_job_repository",
    "get_version_repository",
]