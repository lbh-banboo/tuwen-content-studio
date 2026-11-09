"""WP29 反馈定位与审核决定的依赖注入。

**本模块存在的理由**：端点与 `service` 必须共用同一个 session。这一点在本包
是**结构性**的，不是"偶尔借用"：

| 用例 | 若端点与 `service` 用两个 session | 症状 |
|---|---|---|
| `create_feedback` | 反馈行与首行事件分属两个事务 | "有反馈行但没有创建事件"的溯源断档（R6） |
| `advance_feedback` | 条件 UPDATE 与迁移事件分属两个事务 | 状态前进了但没有历史，或历史说了谎 |
| `edit_content` | 内容行锁与版本行插入不在同一事务 | 412 的判定点看到的是另一个快照 |
| `decide_reviews_batch` | 每条 SAVEPOINT 挂在外层事务之外 | 逐条失败不再"只回滚那一条" |

`session_scope` 在请求正常结束时统一提交、异常时回滚（`identity/dependencies.py`
的 `get_db_session`），所以端点与服务层都**只 flush、不 commit**。

## 为什么本模块自己定义 `ContentRepoDep` / `VersionRepoDep` / `BaselineRepoDep`

这三个仓储是**别的模块的公开对象**，本包只为"把实例交给 `content_bridge`
去调对方公开的 service 函数"而构造它们 —— 与 content 的 `billing_bridge.py`
import `BillingRepository` 是同一类事（调用对方公开接口），**不是**
"互相 import ORM 模型去拼查询"（后者由 `repository.py` 的裸 SQL 承担）。

别名各写一份、不从对方模块 import 同名别名，理由与 `content/dependencies.py`
第 22~32 行逐字相同：`Annotated` 是**类型别名**不是单例，四处各写一份
代价为零，收益是模块之间不产生假 import（import 了对方的依赖别名，
等于声明"本模块依赖对方模块的装配方式"）。因此本包用
`FeedbackContentRepoDep` / `FeedbackVersionRepoDep` / `FeedbackBaselineRepoDep`
三个带前缀的名字 —— 与 content 自己的 `ContentRepoDep` 撞名会让读者
误以为它们是同一个依赖。

## 为什么不提供"反馈专用权限依赖"

与 WP22~WP28 同一条取向：**不新建 `ProjectAction`**。本包的权限需求全部
落在既有动作上（逐端点的理由见 `api.py` 的模块 docstring）。

## ⚠️ `ProjectAccessDep` 只做**成员级**判定

`resolve_project_access` 回答"这个人能不能看到这个项目"，不回答
"他能不能做这个动作"。动作级必须由端点显式调 `access.require(...)` ——
本包六个按 `{contentId}` / `{feedbackId}` 寻址的端点一个都没省
（`api.py` 里逐个可核对）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.baseline.repository import BaselineRepository
from rednote.modules.content.repository import ContentRepository
from rednote.modules.feedback.repository import FeedbackRepository
from rednote.modules.identity.dependencies import DBSession
from rednote.modules.versions.repository import VersionRepository


def get_feedback_repository(db: DBSession) -> FeedbackRepository:
    """本包的仓储（七个端点共用）。"""
    return FeedbackRepository(db)


FeedbackRepoDep = Annotated[FeedbackRepository, Depends(get_feedback_repository)]


def get_content_repository_for_feedback(db: DBSession) -> ContentRepository:
    """内容仓储 —— **只用于两步骤鉴权的反查与 `content_bridge` 的入参**。

    本包不直接读写 `content_items` / `content_versions`：所有内容侧的读
    都经 `content_bridge.read_content_edit_target`（权威快照），所有写都经
    `content_bridge.apply_content_edit`（权威乐观锁）。这个实例存在的意义
    是让 `content.service` 拿到**与端点同一个 session**。
    """
    return ContentRepository(db)


FeedbackContentRepoDep = Annotated[
    ContentRepository, Depends(get_content_repository_for_feedback)
]


def get_version_repository_for_feedback(db: DBSession) -> VersionRepository:
    """版本仓储（`edit_content` → `register_version` 的输入）。

    编辑会追加一条 `content_versions` 行并注册 `version_nodes` 节点，
    两件事必须在同一个 session 里（接缝冻结 §5.2：注册表与子表同一事务）。
    """
    return VersionRepository(db)


FeedbackVersionRepoDep = Annotated[
    VersionRepository, Depends(get_version_repository_for_feedback)
]


def get_baseline_repository_for_feedback(db: DBSession) -> BaselineRepository:
    """基线仓储（`edit_content` 的判据域来源）。

    编辑要重跑 `validate_bindings`，而 claim 的取值范围来自**当前生效基线**。
    本包不缓存基线，也不自己查 `production_baselines` —— 取法归
    `content.service.append_edited_version`（与生成路径同源）。
    """
    return BaselineRepository(db)


FeedbackBaselineRepoDep = Annotated[
    BaselineRepository, Depends(get_baseline_repository_for_feedback)
]


__all__ = [
    "FeedbackBaselineRepoDep",
    "FeedbackContentRepoDep",
    "FeedbackRepoDep",
    "FeedbackVersionRepoDep",
    "get_baseline_repository_for_feedback",
    "get_content_repository_for_feedback",
    "get_feedback_repository",
    "get_version_repository_for_feedback",
]
