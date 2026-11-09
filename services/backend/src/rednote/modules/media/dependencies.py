"""静态图适配与参考素材的依赖注入（WP27）。

**本模块存在的理由**：端点与 `service` 必须共用同一个 session。
本包对这件事的敏感度与 WP26 同为**结构性**的：

| 场景 | 若端点与 `service` 用两个 session | 症状 |
|---|---|---|
| `start_media_generation`（建 N 条身份行） | 端点读到的项目 / 幂等状态与 `service` 写的不一致 | 幂等键第二次数不到第一批 ⇒ **重复建 N 条媒体位**，而两次请求各拿到一组合法响应 |
| 入队端点（建身份行 → `enqueue_job` → 回填 `job_id`） | 身份行落在一个 session、`job_id` 落在另一个 | `media_items.job_id` 与 `jobs` 行的可见性不一致 ⇒ 处理器 `find_media_item_for_job` 反查不到 ⇒ **任务永久 `TARGET_MISSING`**（且没有任何东西会重试成功） |
| `read_media_detail`（4 张表一起读） | 四张表分属不同事务 | 同一个响应里的字段来自不同时刻（WP25 / WP26 同款论证） |

第二行是本包最危险的一个：它是**三步跨两张表**的写
（`insert_media_item` → `enqueue_job` → `set_job_id`），
而其中任一步落在另一个 session 上，表现都是**静默的** ——
HTTP 202 照常返回，只是那个任务永远找不到目标。

| 依赖 | 提供 | 谁用 |
|---|---|---|
| `MediaRepoDep` | `MediaRepository` | 全部七个端点 |
| `JobRepoDep` | `JobRepository` | 两个入队端点（发起生成 / 单张重做） |

## ★ 刻意**只有**两个（按需声明，R6）

`content/dependencies.py` 提供了三个（多一个 `VersionRepoDep`），
`files/dependencies.py` 提供了 `FileRepoDep` + `StorageBackendDep`。
本包**一个都不照抄** —— 逐个说清为什么不需要：

| 不提供的 | 为什么本包没有消费点 |
|---|---|
| `VersionRepoDep` | content 需要它是因为 `adopt_title_candidate` 要端点侧注入 `register_version`。本包的 `register_version` 只发生在 `service._persist_generation` 内部，而那里**已经**能就地构造（它持有 `media_repo.session`）；⚠️ 本包**没有**"不调模型却要加版本节点"的端点（content 的采纳端点才是那种） |
| `FileRepoDep` | `service.register_reference_asset` 的三道校验（项目存在 / 文件存在 / 文件属本项目）走的是 **`MediaRepository` 自己的方法**（`project_exists` / `file_object_exists` / `file_object_project`）—— 仓储层已按"跨模块取数用裸 SQL 放自己 `repository.py`"的既有口径做过一次，端点不需要第二个仓储 |
| `StorageBackendDep` | 七个端点**没有一个读字节**：详情端点给的是 `imageFileId` / `fileObjectId`，下载走 `files` 包自己的端点。⇒ 端点层不需要 storage。生成侧的 storage 是**装配期**依赖（进 `handlers.build_task_registry(storage=…)`，由**将来的 worker 装配点**从 `settings` 构造一次），不是请求级 `Depends`。⚠️ 而该装配点**当前全仓不存在**（`run_job` 只在测试里被调用）⇒ 本包不在这里造一个"看起来已接线"的默认值 |

把上面三个"顺手加上"的代价不是多几行 —— 是**模块之间多出三条指向
`versions` / `files` 的 import**，而它们对本包的职责一个都没参与。
这与 `content/dependencies.py` 拒绝复用 prompts 的 `VersionRepoDep`
是同一条判断，只是方向相反（那里是"不借别人的"，这里是"不照抄别人的"）。

## 刻意不提供"Media 专用权限依赖"

与 WP22~WP26 同一条取向：**不新建 `ProjectAction` / `OrgAction`**。
本包的权限需求全部落在**既有**动作上：

| 动作用途 | 动作 | 依据 |
|---|---|---|
| 素材列表 / 媒体列表 / 媒体详情（`GET`） | `ProjectAction.VIEW` | `permissions.py`"查看项目与其内容" |
| 素材登记 / 发起生成 / 单张重做 / 重新加工（`POST`） | `ProjectAction.EDIT` | `permissions.py`"编辑 Brief、方案、**内容**" |

判定走 `projects.dependencies.ProjectAccessDep` + `access.require(...)`。
⚠️ **`ProjectAccessDep` 只回答"能不能看到这个项目"，不回答"能不能改它"** ——
每个端点都必须自己调一次 `access.require(...)`；少写一次就是
"任何项目成员都能发起生成"，而那是**花钱**的动作（R2 的预占在执行阶段，
但触发它的任务正是被这个端点入的队）。

## 为什么提供 `JobRepoDep`，而 `jobs` 模块自己有同名别名

与 `content/dependencies.py` 同一条理由：本模块**不 import**
`rednote.modules.jobs.dependencies.JobRepoDep` —— 那会在 Media 与 Jobs
之间建一条"只为少写三行"的 import。`Annotated[..., Depends(...)]`
是**类型别名**而不是单例，各写一份的代价是零；而
`JobRepository` 本身是无状态薄封装（只持有 session），多造一个的代价同样是零。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.identity.dependencies import DBSession
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.media.repository import MediaRepository


def get_media_repository(db: DBSession) -> MediaRepository:
    """请求级仓储实例（与端点 session 同源）。"""
    return MediaRepository(db)


#: 全部七个端点共用。
MediaRepoDep = Annotated[MediaRepository, Depends(get_media_repository)]


def get_job_repository(db: DBSession) -> JobRepository:
    """任务仓库（两个 202 端点的入队输入）。

    **与 `jobs` 模块自己的 `JobRepoDep` 是同一个 session 上的两个实例** ——
    理由见模块 docstring 末节。
    """
    return JobRepository(db)


JobRepoDep = Annotated[JobRepository, Depends(get_job_repository)]


__all__ = [
    "JobRepoDep",
    "MediaRepoDep",
    "get_job_repository",
    "get_media_repository",
]
