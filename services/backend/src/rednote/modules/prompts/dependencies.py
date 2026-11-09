"""Prompt 模块的依赖注入（WP23）。

**本模块存在的理由**：端点与 `service` 必须共用同一个 session。
若端点自己造一个 `PromptRepository`、`service` 又拿另一个，
"端点刚校验过的那条模板"与"service 改的那条"就会漂移 ——
本机实测过同类的坑（WP17 的 ISS-54：事件处理器连到了环境变量里的库）。

| 依赖 | 提供 | 谁用 |
|---|---|---|
| `PromptRepoDep` | `PromptRepository` | 四个端点 |
| `VersionRepoDep` | `VersionRepository` | 三个写端点（`register_version` 需要一个版本仓库） |

---

## 为什么提供 `VersionRepoDep`，而 WP20 也提供了同一个名字

两个模块各自定义一个 `VersionRepoDep`，看起来是重复。**刻意如此**：

`rednote.modules.knowledge.dependencies.VersionRepoDep` 是那个模块的依赖别名，
它的存在理由是"知识引用摘要端点要读 `version_dependencies`"。若本模块
去 import 它，Prompt 模块就多了一条**指向 knowledge 模块的 import** ——
而这两个模块之间**没有任何业务关系**（知识是知识，提示词是提示词）。
一条只为"少写三行"而建的跨模块依赖，会在将来"knowledge 改结构"时
变成 Prompt 的编译错误。

`Annotated[..., Depends(...)]` 是**类型别名**，不是单例 ——
两处各写一份的代价是零，收益是模块之间不产生假依赖。

## 刻意不提供"Prompt 专用权限依赖"

协议 03 §9 第 424 行原文：

> Prompt **全员可管理**沿用共享知识策略，但供应商密钥、系统安全指令及
> 允许工具列表由平台配置管理，普通模板编辑不能扩大工具权限或外部访问范围。

"全员可管理"= 与知识同一口径 → 直接用
`projects.dependencies.require_org_action(OrgAction.KNOWLEDGE_MANAGE)`
（需求 §2.2 的权限矩阵里，运营对知识库的操作是唯一开放的组织级操作）。

**不新建 `OrgAction.PROMPT_MANAGE`**：多一个动作就多一处矩阵定义，
而两处一旦不一致，**放宽的那一处会生效**（因为它是先被调用的那层）。

注意这里有一处**看起来矛盾**的地方，必须说清：
"全员可管理"与"不能扩大工具权限"**不矛盾** ——
前者管的是"谁能编辑模板"（人），后者管的是"编辑能改到什么范围"（边界）。
本包的做法是：**人**沿用知识的策略（全员），**边界**另设（`allowed_tools`
的上界 + 数据库 CHECK）。若把两者合成一条判据，就会出现
"因为你是全员之一，所以你可以改上界"——那是把安全约束当成了权限问题。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.identity.dependencies import DBSession
from rednote.modules.prompts.repository import PromptRepository
from rednote.modules.versions.repository import VersionRepository


def get_prompt_repository(db: DBSession) -> PromptRepository:
    return PromptRepository(db)


PromptRepoDep = Annotated[PromptRepository, Depends(get_prompt_repository)]


def get_version_repository(db: DBSession) -> VersionRepository:
    """版本仓库（`register_version` 的输入）。

    **与 `service` 内部造的那个是同一个 session 上的两个实例** ——
    本模块不缓存实例，因为 `VersionRepository` 是无状态的薄封装
    （它只持有 session），多造一个的代价是零，而缓存会引入
    "这个实例对应哪个 session"的生命周期问题。
    """
    return VersionRepository(db)


VersionRepoDep = Annotated[VersionRepository, Depends(get_version_repository)]


__all__ = [
    "PromptRepoDep",
    "VersionRepoDep",
    "get_prompt_repository",
    "get_version_repository",
]
