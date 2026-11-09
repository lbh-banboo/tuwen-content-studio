"""WP30 自检的依赖注入。

**本模块存在的理由**：端点与 `service` 必须共用同一个 session。这一点在本包
是**结构性**的，不是"偶尔借用"：

| 用例 | 若端点与 `service` 用两个 session | 症状 |
|---|---|---|
| `run_self_check` | 三张表与 AI 反馈分属两个事务 | "自检轮次落库了但阻断反馈没落"⇒ WP29 的审核闸**放行**（静默事故） |
| `run_self_check` | findings 与其父 rule_result 分属两个事务 | 延迟一致性触发器在提交时跨两跳 join 找不到父行 ⇒ 整轮失败 |
| `run_self_checks_batch` | 每条 SAVEPOINT 挂在外层事务之外 | 逐条失败不再"只回滚那一条" |
| `read_platform_media_limits` | 配置读取与本轮写入不同源 | R9 的"行数不变"判据会在另一个连接上恒绿 |

`session_scope` 在请求正常结束时统一提交、异常时回滚（`identity/dependencies.py`
的 `get_db_session`），所以端点与服务层都**只 flush、不 commit**。

## 为什么本模块自己定义 `ReviewContentRepoDep` / `ReviewFeedbackRepoDep` / `ReviewPlanningRepoDep`

这三个仓储是**别的模块的公开对象**，本包只为"把实例交给对应的 `*_bridge`
去调对方公开的 service 函数"而构造它们 —— 与 `feedback/dependencies.py`
import `ContentRepository` 是同一类事（调用对方公开接口），**不是**
"互相 import ORM 模型去拼查询"（后者由 `repository.py` 的裸 SQL 承担）。

别名**各写一份、带本包前缀**，不从对方模块 import 同名别名：
`Annotated` 是**类型别名**不是单例，各写一份代价为零，收益是模块之间不产生
假 import（import 了对方的依赖别名，等于声明"本模块依赖对方模块的装配方式"）。
撞名（直接叫 `ContentRepoDep`）会让读者误以为它们是同一个依赖。

## 为什么不提供"自检专用权限依赖"

与 WP22~WP29 同一条取向：**不新建 `ProjectAction`**。本包的权限需求全部落在
既有动作上（逐端点的理由见 `api.py` 的模块 docstring）。

## ⚠️ `ProjectAccessDep` 只做**成员级**判定

`resolve_project_access` 回答"这个人能不能看到这个项目"，不回答"他能不能做
这个动作"。动作级必须由端点显式调 `access.require(...)` —— 本包按
`{contentId}` / `{runId}` 寻址的四个端点一个都没省（`api.py` 里逐个可核对）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.content.repository import ContentRepository
from rednote.modules.feedback.repository import FeedbackRepository
from rednote.modules.identity.dependencies import DBSession
from rednote.modules.planning.repository import PlanningRepository
from rednote.modules.review.repository import ReviewRepository


def get_review_repository(db: DBSession) -> ReviewRepository:
    """本包的仓储（六个端点共用）。"""
    return ReviewRepository(db)


ReviewRepoDep = Annotated[ReviewRepository, Depends(get_review_repository)]


def get_content_repository_for_review(db: DBSession) -> ContentRepository:
    """内容仓储 —— **只用于两步骤鉴权的反查与 `content_bridge` 的入参**。

    本包不直接写 `content_items` / `content_versions`（自检不改稿）；
    内容侧的读全部经 `content_bridge.read_content_edit_target`（权威快照）。
    这个实例存在的意义是让 `content.service` 拿到**与端点同一个 session**。
    """
    return ContentRepository(db)


ReviewContentRepoDep = Annotated[
    ContentRepository, Depends(get_content_repository_for_review)
]


def get_feedback_repository_for_review(db: DBSession) -> FeedbackRepository:
    """反馈仓储（阻断项 → `feedback.service.create_feedback` 的输入）。

    必须与本包三张表同一个 session：反馈行、它的首行事件、回指它的 finding
    要么一起落库、要么一起回滚。分两个事务会造出
    "finding 指向一条不存在的反馈"或"反馈建了但 finding 没建"的半态 ——
    前者让详情页点不开，后者让运营的待处理列表里多出一条无来源的机器意见。
    """
    return FeedbackRepository(db)


ReviewFeedbackRepoDep = Annotated[
    FeedbackRepository, Depends(get_feedback_repository_for_review)
]


def get_planning_repository_for_review(db: DBSession) -> PlanningRepository:
    """配置仓储（`platform.mediaLimits` 的**只读**取数）。

    本包经 `planning_bridge` 调 `get_effective_limits(..., materialize=False)` ——
    `materialize` 写死在桥接里，不是这里的参数（R9：读路径上的写副作用）。
    """
    return PlanningRepository(db)


ReviewPlanningRepoDep = Annotated[
    PlanningRepository, Depends(get_planning_repository_for_review)
]


__all__ = [
    "ReviewContentRepoDep",
    "ReviewFeedbackRepoDep",
    "ReviewPlanningRepoDep",
    "ReviewRepoDep",
    "get_content_repository_for_review",
    "get_feedback_repository_for_review",
    "get_planning_repository_for_review",
    "get_review_repository",
]
