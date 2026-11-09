"""身份领域模块（WP08）。

职责：
- 会话的建立、校验、撤销（`api.py`、`repository.py`）
- 与企业 IdP 的 OIDC 交互（`oidc.py`）
- 存储模型（`models.py`）
- 认证依赖注入（`dependencies.py`）

**不含**：项目成员授权（WP09）、管理员成员管理（`/admin/users`）。
后者属于管理中心，会单独成模块 —— 把"我是谁"与"我能管谁"分开，
是因为它们的权限模型不同（前者人人可用，后者仅管理员）。
"""

from rednote.modules.identity.api import router

__all__ = ["router"]