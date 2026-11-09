"""按对象访问的依赖注入（WP09）。

**本模块解决的核心问题**：如何保证"每个访问项目的端点都做了权限检查"。

答案是**把权限检查做成依赖，让端点无法绕过**：
端点签名里写 `project: ProjectAccess`，FastAPI 就会先执行权限检查、
失败即中断。开发者不需要（也无法）在业务代码里手写权限判断 ——
**"忘了加检查"这个漏洞类型被结构性地消除了**。

研发计划 01 §10.2 原文：
> 所有接口、后台任务与 SSE 检查对象权限。
> 不能只在菜单隐藏管理入口。

---

**404 与 403 的语义区分（协议 §1.2 + 本模块的核心设计）：**

| 场景 | 返回 | 理由 |
|---|---|---|
| 项目不存在 | 404 | — |
| 用户不是项目成员（且非管理者） | **404** | 协议 §1.2："对无权获知存在的对象也可返回404"。返回 403 会让攻击者通过枚举 UUID 判断"哪些项目存在" —— 这本身就是信息泄露（能推出公司项目数量、编号规律） |
| 是成员但缺具体权限（如协作者尝试结项） | **403** | 用户已经能看到这个项目，隐瞒它没有意义，反而让用户困惑"项目怎么突然消失了" |

这个区分不是"随手定的"，而是"防枚举"与"可理解性"两个目标的平衡点。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Path, Request

from rednote.modules.identity.dependencies import (
    CurrentUser,
    CurrentUserDep,
    DBSession,
)
from rednote.modules.identity.models import ROLE_ADMIN, ROLE_OPERATOR
from rednote.modules.projects.models import Project
from rednote.modules.projects.permissions import (
    MembershipInfo,
    OrgAction,
    ProjectAction,
    ProjectPermissions,
    action_label,
    allows_org_action,
    compute_project_permissions,
)
from rednote.modules.projects.repository import ProjectRepository
from rednote.modules.work_items.repository import WorkItemRepository
from rednote.platform.concurrency import require_if_match as _require_if_match
from rednote.platform.errors import ApiError, ErrorCode


def get_project_repository(db: DBSession) -> ProjectRepository:
    return ProjectRepository(db)


ProjectRepoDep = Annotated[ProjectRepository, Depends(get_project_repository)]


def get_work_item_repository(db: DBSession) -> WorkItemRepository:
    return WorkItemRepository(db)


WorkItemRepoDep = Annotated[WorkItemRepository, Depends(get_work_item_repository)]


@dataclass(frozen=True)
class ProjectAccess:
    """已通过权限校验的项目访问上下文。

    端点拿到这个对象就说明：
      1. 项目存在；
      2. 当前用户有权以某种身份访问它；
      3. 权限集合已算好（`permissions`），可直接判断具体动作。

    **为什么把 project 与 permissions 打包**：
    避免端点分别注入 project 与 permissions 时"用了 project 忘了检查权限"。
    一个对象承载"已授权"这个事实，无法只取一半。
    """

    project: Project
    permissions: ProjectPermissions

    @property
    def project_id(self) -> uuid.UUID:
        return self.project.id

    def require(self, action: ProjectAction) -> None:
        """要求某个具体动作的权限，不满足抛 403。

        端点里这样用：
            access = ...  # 已通过成员校验
            access.require(ProjectAction.CLOSE)

        **为什么是方法而不是依赖**：动作是"端点内部的决定"，
        不同代码路径可能需要不同动作（例如"编辑自己的草稿"vs"编辑他人草稿"）。
        做成依赖会强制每个端点只能声明一个动作。
        """
        if not self.permissions.allows(action):
            raise ApiError(
                ErrorCode.PERMISSION_DENIED,
                f"当前角色无法执行「{action_label(action)}」。",
                details={
                    "projectId": str(self.project_id),
                    "action": str(action),
                    # 回显当前角色，让前端能给出"需要什么角色"的提示。
                    "currentRole": self.permissions.membership.role or "NONE",
                    "isAdmin": self.permissions.is_admin,
                },
            )

    def require_if_match(self, request: Request) -> None:
        """校验 `If-Match` 前置条件（协议 §1.1）。

        `current_version` 取自刚读到的 `lock_version` —— 也就是说，
        "读"与"校验"在同一请求的同一事务里，读到的版本就是写入时的期望版本。

        注意这里**只是前置校验**：真正的并发保证在
        `repository.update_project_versioned` 的 `WHERE lock_version = ?`。
        两者都要有 —— 只有前置校验会留下窗口，只有 WHERE 条件则无法
        在提交前给客户端一个明确的 412 与当前 ETag。
        """
        _require_if_match(
            request,
            resource_id=self.project.id,
            current_version=self.project.lock_version,
            resource_label="项目",
        )


def resolve_project_access(
    repo: ProjectRepository,
    *,
    project_id: uuid.UUID,
    user: CurrentUser,
) -> ProjectAccess:
    """按项目 id 解析访问权。

    **判定顺序（不可调换）**：
      1. 查项目；不存在 → 404
      2. 管理者 → 直接放行（需求 §2.2：管理者对全部项目有权限）
      3. 非管理者查成员关系；非成员 → **404**（不是 403，见模块 docstring）
      4. 成员 → 算出权限集合

    **为什么第 2 步在成员查询之前**：管理者通常不是每个项目的成员，
    先查成员会白跑一次查询；更重要的是，让"管理者全权"这条规则
    在代码里显式可见，而不是靠"给管理者也插入成员记录"来间接实现 ——
    后者在新增项目时容易漏插，导致管理者看不到自己刚建的项目。

    **为什么从 `load_project_access` 里抽出来**：项目 id 并不总在路径里。
    WP12 的上传端点按 `uploadId` 寻址，要先用会话反查出 `project_id`
    再判权限 —— 那时没有 `Path(alias="projectId")` 可用。
    若不抽出来，`modules/files` 就得把上面这四步抄一遍，
    而"两处各判一次权限"正是最容易漂移的重复：某天有人给 404/403
    的取舍改了规则，只会改其中一处。
    """
    project = repo.get_project(project_id)
    if project is None:
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "项目不存在或无权访问。",
            details={"projectId": str(project_id)},
        )

    if user.is_admin:
        # 管理者全权。membership 给一个"非成员"占位 ——
        # 权限判定里 is_admin 优先，role 不会被用到。
        membership = MembershipInfo(
            project_id=project_id, user_id=user.id, role=None
        )
        return ProjectAccess(
            project=project,
            permissions=compute_project_permissions(
                is_admin=True,
                membership=membership,
                project_phase=project.phase,
            ),
        )

    membership = repo.get_membership(project_id, user.id)
    if not membership.is_member:
        # **非成员返回 404**：防项目存在性枚举（协议 §1.2）。
        # 与"项目不存在"用同一个错误体，使两者不可区分。
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "项目不存在或无权访问。",
            details={"projectId": str(project_id)},
        )

    return ProjectAccess(
        project=project,
        permissions=compute_project_permissions(
            is_admin=False,
            membership=membership,
            project_phase=project.phase,
        ),
    )


def load_project_access(
    request: Request,
    user: CurrentUserDep,
    repo: ProjectRepoDep,
    project_id: Annotated[uuid.UUID, Path(alias="projectId")],
) -> ProjectAccess:
    """路径参数版的项目访问依赖（`{projectId}` 在路径里的端点用）。"""
    return resolve_project_access(repo, project_id=project_id, user=user)


ProjectAccessDep = Annotated[ProjectAccess, Depends(load_project_access)]


def require_org_action(action: OrgAction):
    """构造一个"要求某组织级权限"的依赖。

    **为什么返回闭包而不是直接写依赖函数**：
    组织级动作有五种，写五个几乎相同的依赖函数是重复。
    用工厂生成，动作成为参数，新增动作不需要新增函数。

    用法：
        @router.post("/projects", dependencies=[Depends(require_org_action(OrgAction.CREATE_PROJECT))])
    """

    def _dependency(user: CurrentUserDep) -> None:
        role = ROLE_ADMIN if user.is_admin else ROLE_OPERATOR
        if not allows_org_action(is_admin=user.is_admin, action=action):
            raise ApiError(
                ErrorCode.PERMISSION_DENIED,
                "当前角色无法执行该操作。",
                details={
                    "action": str(action),
                    "currentRole": role,
                    "isAdmin": user.is_admin,
                },
            )

    return _dependency


__all__ = [
    "ProjectAccess",
    "ProjectAccessDep",
    "ProjectRepoDep",
    "WorkItemRepoDep",
    "get_project_repository",
    "get_work_item_repository",
    "load_project_access",
    "require_org_action",
    "resolve_project_access",
]