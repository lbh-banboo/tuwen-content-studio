"""项目级权限判定（WP09）。

**本模块是需求 §2.2 权限矩阵的代码化。** 矩阵原文：

| 操作 | 管理者 | 项目负责人 | 项目协作者 | 非项目运营 |
|---|---|---|---|---|
| 创建项目、分配及变更负责人 | 是 | 否 | 否 | 否 |
| 查看完整项目与内容 | 全部 | 所属项目 | 所属项目 | 否 |
| 编辑 Brief、方案与内容 | 全部 | 是 | 是 | 否 |
| 生成、反馈、人工审核 | 全部 | 是 | 是 | 否 |
| 下载、交付、发布登记、回收数据 | 全部 | 是 | 是 | 否 |
| 生成阶段报告 | 全部 | 是 | 是 | 否 |
| 设置预算、查看公司费用 | 是 | 否 | 否 | 否 |
| 查看本项目费用 | 是 | 是 | 是 | 否 |
| 主动结项 | 是 | 是 | 否 | 否 |
| 重新打开已结项项目 | 是 | 否 | 否 | 否 |
| 全员知识库查看、创建、编辑、归档、确认生效 | 是 | 是 | 是 | 是 |
| 查看知识历史与变更记录 | 是 | 是 | 是 | 是 |

实施默认（需求 §2.2）：**项目操作按成员隔离，知识资料跨项目全员可见。**
进入知识库的项目研究资料、事实、反馈资产也可被全员查看；
**项目费用和未入库业务记录不因此自动公开。**

---

**三个关键设计决定：**

### 1. 权限判定必须集中，不能散落在各端点

散落会导致"这个端点的 403 检查忘了加"这类漏洞 —— 而它不会报错，
只在生产被利用。集中成 `ProjectPermissions` 后，每个端点只需声明
"我要求什么权限"，判定逻辑只有一处。

### 2. 非成员访问返回 404 而非 403（协议 §1.2）

协议 §1.2 原文："对无权获知存在的对象也可返回 404"。
若返回 403，攻击者可通过枚举项目 UUID 判断"哪些项目存在" ——
这本身就是信息泄露（能推出公司有多少项目、编号规律）。
404 让"不存在"与"无权访问"不可区分。

**注意：这里说的是"无权获知存在"的场景。** 对于"知道存在但无权限操作"
（例如协作者尝试结项），返回 403 是正确的 —— 用户已经能看到这个项目，
隐瞒它没有意义，反而让用户困惑"为什么项目突然消失了"。

### 3. 权限判定依赖成员查询，必须带超时

每次请求都要查 `project_members`。这个查询走 `ix_project_members_active_user`
索引，代价很低，但仍是外部调用（数据库）—— 超时由 engine 的
`statement_timeout` 统一注入（见 platform/database.py）。

### 4. 已移除成员立即失效

`removed_at IS NULL` 是活跃成员的唯一判据。项目成员移除后，
下一次请求就会失败 —— 不需要等会话过期。这与"账号停用立即失效"
（WP08）是同一原则在不同层级上的应用。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Final

from rednote.modules.identity.models import ROLE_ADMIN, ROLE_OPERATOR
from rednote.modules.projects.models import (
    MEMBER_COLLABORATOR,
    MEMBER_OWNER,
    PHASE_CLOSED,
)


@unique
class ProjectAction(StrEnum):
    """项目相关的操作类型。

    用枚举而非字符串：调用方写错动作名会立刻报错，
    而不是静默得到一个"未定义动作 → 默认放行/拒绝"的危险行为。
    """

    # 查看项目与其内容。
    VIEW = "VIEW"
    # 编辑 Brief、方案、内容。
    EDIT = "EDIT"
    # 生成、反馈、人工审核。
    REVIEW = "REVIEW"
    # 下载、交付、发布登记、回收数据。
    DELIVER = "DELIVER"
    # 生成阶段报告。
    REPORT = "REPORT"
    # 查看本项目费用。
    VIEW_COST = "VIEW_COST"
    # 主动结项。
    CLOSE = "CLOSE"
    # 管理项目成员（添加/移除协作者）。
    MANAGE_MEMBERS = "MANAGE_MEMBERS"


@unique
class OrgAction(StrEnum):
    """组织级操作（跨项目），不属于任何单个项目。"""

    # 创建项目、分配及变更负责人。
    CREATE_PROJECT = "CREATE_PROJECT"
    # 设置预算、查看公司费用。
    MANAGE_BUDGET = "MANAGE_BUDGET"
    # 重新打开已结项项目。
    REOPEN_PROJECT = "REOPEN_PROJECT"
    # 管理员成员管理。
    MANAGE_USERS = "MANAGE_USERS"
    # 品牌/产品字典的增改与归档（WP10 补）。
    # 需求 §2.2 的矩阵里没有"字典维护"这一行 —— 这是一个**需求缺口**。
    # 按"字典是全组织共享的主数据"定位，归管理者（与"设置预算、查看公司费用"
    # 同级的组织级配置权）。已登记为待定项，业务若要求运营可维护需修订矩阵。
    MANAGE_DICTIONARY = "MANAGE_DICTIONARY"
    # 全员知识库操作（默认允许所有登录成员）。
    KNOWLEDGE_MANAGE = "KNOWLEDGE_MANAGE"
    # 确认「跨项目登记同一条笔记」（WP37）。
    # 需求 §2.2 的矩阵里没有这一行 —— 与 MANAGE_DICTIONARY 同属**需求缺口**，
    # 依据是 M4 计划 §7.2 验收「跨项目同笔记需管理者确认」；仅管理者，运营/协作者一律否。
    CONFIRM_CROSS_PROJECT_NOTE = "CONFIRM_CROSS_PROJECT_NOTE"


# 项目级权限矩阵。**这是需求 §2.2 的逐行映射。**
#
# 管理者：全部为 True（"管理者全权"是 WP09 验收项之一）。
# 负责人：除 MANAGE_MEMBERS 外的项目内操作均可（成员管理归管理者，
#         需求 §2.2 "创建项目、分配及变更负责人"仅管理者可做）。
# 协作者：与负责人相同，**但主动结项不可**（需求 §2.2 "主动结项"行：管理者/负责人=是，协作者=否）。
#
# 非成员不在此表中 —— 他们连项目都不该看到，判定在更早的层被拦截（404）。
_PROJECT_MATRIX: Final[dict[str, dict[ProjectAction, bool]]] = {
    MEMBER_OWNER: {
        ProjectAction.VIEW: True,
        ProjectAction.EDIT: True,
        ProjectAction.REVIEW: True,
        ProjectAction.DELIVER: True,
        ProjectAction.REPORT: True,
        ProjectAction.VIEW_COST: True,
        ProjectAction.CLOSE: True,
        ProjectAction.MANAGE_MEMBERS: False,
    },
    MEMBER_COLLABORATOR: {
        ProjectAction.VIEW: True,
        ProjectAction.EDIT: True,
        ProjectAction.REVIEW: True,
        ProjectAction.DELIVER: True,
        ProjectAction.REPORT: True,
        ProjectAction.VIEW_COST: True,
        # **协作者不能主动结项** —— 需求 §2.2 明确。
        ProjectAction.CLOSE: False,
        ProjectAction.MANAGE_MEMBERS: False,
    },
}

# 组织级权限矩阵。
# 管理者全权；运营只能管理知识库（全员知识库，需求 §2.2 最后两行）。
_ORG_MATRIX: Final[dict[str, dict[OrgAction, bool]]] = {
    ROLE_ADMIN: {
        OrgAction.CREATE_PROJECT: True,
        OrgAction.MANAGE_BUDGET: True,
        OrgAction.REOPEN_PROJECT: True,
        OrgAction.MANAGE_USERS: True,
        OrgAction.MANAGE_DICTIONARY: True,
        OrgAction.KNOWLEDGE_MANAGE: True,
        OrgAction.CONFIRM_CROSS_PROJECT_NOTE: True,
    },
    ROLE_OPERATOR: {
        # 运营不能创建项目、不能碰预算、不能重开项目、不能管用户。
        OrgAction.CREATE_PROJECT: False,
        OrgAction.MANAGE_BUDGET: False,
        OrgAction.REOPEN_PROJECT: False,
        OrgAction.MANAGE_USERS: False,
        # 字典是共享主数据，改错会影响所有项目 —— 运营只读，改归管理者。
        OrgAction.MANAGE_DICTIONARY: False,
        # 跨项目同笔记须管理者确认（M4 计划 §7.2）—— 运营遇到命中只能被拒。
        OrgAction.CONFIRM_CROSS_PROJECT_NOTE: False,
        # **知识库全员可管理**（需求 §2.2）—— 这是唯一对运营开放的组织级操作。
        OrgAction.KNOWLEDGE_MANAGE: True,
    },
}


@dataclass(frozen=True)
class MembershipInfo:
    """用户在某个项目里的成员关系。

    `role` 为 None 表示不是活跃成员（可能从未加入，也可能已被移除）。
    """

    project_id: uuid.UUID
    user_id: uuid.UUID
    role: str | None

    @property
    def is_member(self) -> bool:
        return self.role is not None

    @property
    def is_owner(self) -> bool:
        return self.role == MEMBER_OWNER


@dataclass(frozen=True)
class ProjectPermissions:
    """某个用户对某个项目的权限集合。

    **为什么用数据类而不是逐次调用函数**：
    端点通常需要判断多个动作（例如"能看吗？能改吗？按钮要不要显示？"）。
    一次性算出全部权限，避免多次查库，也让"权限集合"可以被直接断言测试。
    """

    is_admin: bool
    membership: MembershipInfo
    project_phase: str

    def allows(self, action: ProjectAction) -> bool:
        """是否允许某项目内操作。"""
        # 管理者全权 —— 需求 §2.2 每行的"管理者"列都是"是"/"全部"。
        if self.is_admin:
            return True

        role = self.membership.role
        if role is None:
            # 非成员：任何项目内操作都不允许。
            # 注意这里返回 False 而不是抛异常 —— 抛不抛由调用方决定
            # （有的场景要 404 有的要 403，见模块 docstring 第 2 点）。
            return False

        allowed = _PROJECT_MATRIX.get(role, {}).get(action, False)
        if not allowed:
            return False

        # 项目已结项时，只允许查看（需求 §18.2：结项后禁止新生产任务；
        # 允许查看、下载、补录数据、处理事实纠错待办和生成新版报告）。
        if self.project_phase == PHASE_CLOSED and action in {
            ProjectAction.EDIT,
            ProjectAction.REVIEW,
            ProjectAction.DELIVER,
        }:
            return False

        return True

    def allows_org(self, action: OrgAction) -> bool:
        """是否允许某组织级操作。"""
        return allows_org_action(is_admin=self.is_admin, action=action)


def allows_org_action(*, is_admin: bool, action: OrgAction) -> bool:
    """组织级动作判定的**唯一入口**（纯函数，不依赖 ProjectPermissions）。

    **为什么单独抽出来**：`require_org_action` 是路由依赖，
    执行时还没有项目上下文，拿不到 `ProjectPermissions` 实例。
    若让它自己去读 `_ORG_MATRIX`，矩阵就有了两个读取点 ——
    改矩阵时容易只改一处。统一走这里，矩阵只有一个出口。
    """
    role = ROLE_ADMIN if is_admin else ROLE_OPERATOR
    return _ORG_MATRIX.get(role, {}).get(action, False)


def compute_project_permissions(
    *,
    is_admin: bool,
    membership: MembershipInfo,
    project_phase: str,
) -> ProjectPermissions:
    """组装权限集合。

    纯函数：不查库、不抛异常。查库由调用方（仓储层）完成，
    这样权限逻辑可以被单元测试直接覆盖，不需要数据库。
    """
    return ProjectPermissions(
        is_admin=is_admin,
        membership=membership,
        project_phase=project_phase,
    )


# 动作的中文名。用于 403 的 details，让运营看得懂"缺什么权限"。
_ACTION_LABELS: Final[dict[ProjectAction, str]] = {
    ProjectAction.VIEW: "查看项目",
    ProjectAction.EDIT: "编辑内容",
    ProjectAction.REVIEW: "审核内容",
    ProjectAction.DELIVER: "交付与发布登记",
    ProjectAction.REPORT: "生成报告",
    ProjectAction.VIEW_COST: "查看项目费用",
    ProjectAction.CLOSE: "结项",
    ProjectAction.MANAGE_MEMBERS: "管理项目成员",
}


def action_label(action: ProjectAction) -> str:
    return _ACTION_LABELS.get(action, str(action))


__all__ = [
    "MembershipInfo",
    "OrgAction",
    "ProjectAction",
    "ProjectPermissions",
    "action_label",
    "allows_org_action",
    "compute_project_permissions",
]