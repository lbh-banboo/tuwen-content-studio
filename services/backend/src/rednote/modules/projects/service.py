"""项目领域服务（WP10）。

**为什么要有这一层**：
创建项目不是"插一行"。它包含三件事，且必须一起成功或一起失败：
1. 生成全局唯一的展示编号（需求 §4.1）
2. 写入负责人与协作者成员关系（需求 §2.3"每个项目必须有且只有一名主负责人"）
3. 插入项目行本身

放在 API 层会让端点函数被重试逻辑淹没；放在仓储层会让仓储握住
"编号策略"这种业务决定（仓储应该只翻译存储细节）。
因此用一个薄服务层表达"创建项目"这个领域动作。

**编号冲突的重试必须包在 SAVEPOINT 里**：
随机后缀撞车的概率极低但非零，撞了要能再试一次。
直接 catch IntegrityError 而不回滚到 SAVEPOINT 的话，
PG 会把整个事务标记为 aborted，后续语句全部失败 —— 重试反而变成新的报错。
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.exc import IntegrityError

from rednote.modules.projects.models import (
    EXTERNAL_POLICY_LOCAL_ONLY,
    Project,
)
from rednote.modules.projects.repository import ProjectRepository
from rednote.platform.codes import PROJECT_CODE_PREFIX, generate_display_code
from rednote.platform.errors import ApiError, ErrorCode

# 编号冲突重试次数。
# 6 位 base32 下连续 5 次都撞的概率约 (月项目数 / 10.7 亿)^5，
# 小到可以忽略。设上限是为了"绝不无限循环"——
# 若真撞满 5 次，说明是别的问题（例如并发压测打满），应当暴露而不是硬撑。
MAX_CODE_ATTEMPTS = 5

_PROJECT_CODE_CONSTRAINT = "uq_projects_project_code"

# 交接过程中可能与并发请求相撞的两个成员约束：
#   - uq_project_members_active：同项目同用户至多一条活跃成员行
#     （并发交接给同一个"尚不是成员"的用户时，A 先插入，B 撞这里）
#   - uq_project_members_active_owner：同项目至多一名活跃负责人
#     （并发交接给不同用户时，后到的那个撞这里）
# 两者都表示"另一个交接请求抢先了"，语义相同，按同一类处理。
_OWNER_TRANSFER_CONSTRAINTS = (
    "uq_project_members_active",
    "uq_project_members_active_owner",
)


def _constraint_name(exc: IntegrityError) -> str | None:
    """取驱动报告的约束名（psycopg 在 `err.diag.constraint_name`）。"""
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", None)


def _mentions_constraint(exc: IntegrityError, name: str) -> bool:
    """约束名匹配：先看驱动字段，取不到时退化为错误文本匹配。

    文本匹配是兼容不同驱动与版本的兜底 —— 但**只在驱动没给约束名时**才有意义，
    因此保留成独立函数，让"精确判定"与"兜底判定"的边界清晰。
    """
    if _constraint_name(exc) == name:
        return True
    return name in str(getattr(exc, "orig", exc))


def _is_project_code_conflict(exc: IntegrityError) -> bool:
    """判断这个 IntegrityError 是不是"项目编号撞车"。

    **必须精确判断，不能一律重试**：如果冲突来自
    `uq_project_members_active`（同一用户被重复加为成员）等其它约束，
    盲目重试会掩盖真实的业务错误 —— 用户看到的是"重试 5 次后失败"，
    而真正的原因（比如负责人同时出现在协作者列表里）被吞掉了。
    """
    return _mentions_constraint(exc, _PROJECT_CODE_CONSTRAINT)


def is_owner_transfer_conflict(exc: IntegrityError) -> bool:
    """判断这个 IntegrityError 是不是"并发交接"相撞。

    **为什么需要它**：`create_assignment` 原本把**任何** IntegrityError
    都解释成"负责人刚被其他操作变更"，把外键违规、CHECK 违规等真正的
    程序缺陷一并伪装成 412 —— 排障时看到的是一个看起来"很正常"的
    并发冲突，而实际是 bug。收敛到具体的两个成员约束后，
    其它 IntegrityError 会原样抛出（500），这才是它们应有的表现。
    """
    return any(_mentions_constraint(exc, name) for name in _OWNER_TRANSFER_CONSTRAINTS)


def create_project(
    repo: ProjectRepository,
    *,
    name: str,
    brand_id: uuid.UUID,
    product_id: uuid.UUID,
    owner_id: uuid.UUID,
    target_delivery_count: int,
    due_date: date,
    collaborator_ids: list[uuid.UUID] | None = None,
    target_publication_count: int | None = None,
    external_policy: str = EXTERNAL_POLICY_LOCAL_ONLY,
    description: str | None = None,
) -> Project:
    """创建项目（含负责人与协作者成员关系）。返回已写入的项目对象。

    失败语义：
      - 编号连续冲突 → 503 `DEPENDENCY_UNAVAILABLE`（可重试）
      - 其它唯一约束冲突 → 由上层捕获，转成对应的业务错误
    """
    collaborators = collaborator_ids or []

    for _ in range(MAX_CODE_ATTEMPTS):
        code = generate_display_code(PROJECT_CODE_PREFIX)
        try:
            with repo.savepoint():
                project = repo.create_project(
                    project_code=code,
                    name=name,
                    brand_id=brand_id,
                    product_id=product_id,
                    owner_id=owner_id,
                    target_delivery_count=target_delivery_count,
                    target_publication_count=target_publication_count,
                    due_date=due_date,
                    external_policy=external_policy,
                    description=description,
                )
                repo.add_owner_and_collaborators(
                    project.id,
                    owner_id=owner_id,
                    collaborator_ids=collaborators,
                )
                return project
        except IntegrityError as exc:
            if not _is_project_code_conflict(exc):
                # 不是编号问题 —— 交给上层，不要吞掉。
                raise

    raise ApiError(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        "项目编号生成连续冲突，请稍后重试。",
        details={"attempts": MAX_CODE_ATTEMPTS, "reason": "code_generation_exhausted"},
    )


__all__ = [
    "MAX_CODE_ATTEMPTS",
    "create_project",
    "is_owner_transfer_conflict",
]
