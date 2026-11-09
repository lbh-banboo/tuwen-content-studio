"""版本可见范围判定（WP11）。

**版本是"项目内资产"，但有一类例外**：平台级版本（`project_id IS NULL`，
典型是 `PROMPT` 模板版本）不属于任何项目。两者混在一起时，
可见性规则必须显式写下来，不能靠"反正也没人会查那个 id"。

规则（本文件的唯一权威表述）：

| 版本的 `project_id` | 管理者（`is_admin`） | 该项目活跃成员 | 其他已登录用户 |
|---|---|---|---|
| 非空 | 可见 | 可见 | **不可见** |
| 为空（平台级） | 可见 | 可见 | **不可见** |

两条设计决定：

1. **不可见一律返回 404，不返回 403**（延续 WP09/WP10 的约定）。
   403 等于告诉攻击者"这个 id 存在，只是你没权限"，
   于是可以把 UUID 空间枚举成"哪些项目/版本真实存在"。
   404 让"不存在"与"无权知道存在"不可区分。

2. **平台级版本不对普通用户开放**。理由不是安全，是语义：
   Prompt 模板是平台配置，普通用户既不生产它也不消费它 ——
   内容生产走的是服务端组装，用户看不到模板本身。
   若将来确有需要（例如模板管理员角色），在此加一条规则并补测试。

**为什么权限判定是纯函数**：
`can_view_version` 不查库，因此可以被毫秒级单测覆盖（与 WP09 的
`compute_project_permissions` 同一思路）。查库那一半由
`resolve_version_visibility` 负责，它只做"取一个布尔值"这一件事。
"""

from __future__ import annotations

import uuid

from rednote.modules.identity.dependencies import CurrentUser
from rednote.modules.versions.models import VersionNode
from rednote.modules.versions.repository import VersionRepository
from rednote.platform.errors import ApiError, ErrorCode


def can_view_version(
    *,
    project_id: uuid.UUID | None,
    is_admin: bool,
    is_project_member: bool,
) -> bool:
    """纯函数：该用户能否看到这个版本。"""
    if is_admin:
        return True
    if project_id is None:
        # 平台级版本只对管理者开放。
        return False
    return is_project_member


def can_link_versions(*, is_admin: bool, is_project_member: bool) -> bool:
    """纯函数：该用户能否在两个版本之间登记依赖边。

    规则与可见性一致 —— 能看见才能引用。管理者全权。
    """
    return is_admin or is_project_member


def resolve_version_visibility(
    *,
    repo: VersionRepository,
    node: VersionNode,
    user: CurrentUser,
) -> bool:
    """查库那一半：把 `can_view_version` 需要的成员事实取出来。

    非成员**不做短路**：即使版本属于某个项目，也必须先确认该用户是不是
    这个项目的活跃成员。不能因为"版本存在"就放行。
    """
    is_project_member = False
    if node.project_id is not None:
        is_project_member = repo.is_active_member(
            project_id=node.project_id, user_id=user.id
        )
    return can_view_version(
        project_id=node.project_id,
        is_admin=user.is_admin,
        is_project_member=is_project_member,
    )


def require_version_visible(
    *,
    repo: VersionRepository,
    node: VersionNode,
    user: CurrentUser,
) -> None:
    """不可见即抛 404（**不是 403**，理由见模块 docstring）。"""
    if not resolve_version_visibility(repo=repo, node=node, user=user):
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "版本不存在。",
            details={"resourceId": str(node.id)},
        )


__all__ = [
    "can_link_versions",
    "can_view_version",
    "require_version_visible",
    "resolve_version_visibility",
]
