"""开发期专用工具（**仅 dev/test 环境装配**）。

本模块存在的唯一理由，是让"开发环境能登录"这件事**不污染生产代码路径**。

## 为什么需要一个独立模块

前端 Demo 需要走通 G1 的"登录 → 看到项目 → 上传文件 → 看到任务"链路。
生产环境的登录走企业 IdP（OIDC Authorization Code + PKCE，WP08 已实现），
但开发机上没有 IdP，也不应该为了演示去启动一个 Keycloak ——
那会把"能否演示"绑定到"能否拉起一套身份基础设施"。

因此需要一个**开发期登录旁路**。但如果把它写在 `identity/api.py` 里：

1. 那个文件是 WP08 的验收产物。改它会让本次改动波及已验收的工作包，
   违背 `docs/m2/M2并行开发接缝冻结.md` 的"分支只写自己模块内的新文件"。
2. 更要紧的是：**端点会存在于生产的 app 上**。哪怕内部用
   `if env != "dev": raise 403` 挡住，端点本身仍可被探测到 ——
   攻击面比"物理不存在"大一个量级。

独立模块 + **装配期按环境注册**同时解决这两点：生产环境的
`create_app()` 根本不会调用 `include_router(devtools_router)`，
`/api/v1/dev/auth/login` 在生产上返回 **404**（不是 403）。

## 两道防线（缺一不可）

| 防线 | 位置 | 挡住什么 |
|---|---|---|
| 装配期环境门 | `main.create_app` 的 `if resolved.environment in _DEV_LOGIN_ENVIRONMENTS` | 让端点在非 dev 环境**不存在** |
| 运行期环境门 | `api.py` 里每个端点首行的 `_assert_dev_environment` | 万一将来有人把 include 挪出 if，端点自身仍拒绝服务 |

第二道是**冗余**的 —— 但它必须存在，因为"第一道被误改"是完全可能的
（一次重构、一个 merge 冲突解决失误）。安全设计上，纵深防御的代价
只是一次字符串比较，收益是"单点失误不导致失守"。

**反向验证**（`tests/test_devtools.py`）：
用 `environment="prod"` 等非 dev 值构造 app，断言该路径**不在路由表里** ——
刻意**不**判"请求返回 404"：那只能证明"这次请求没成功"，证明不了
"端点没被发布出去"（端点若注册了、只在内部判环境，攻击面依然存在，
可被枚举到）。若不写这条测试，"装配期门"是否真的生效**无法被机械核对**。

⚠️ 2026-10-07 更正两处：原文件名 tests/test_devtools_wp_g1.py **不存在**
（真身 `tests/test_devtools.py::test_dev_route_is_absent_in_non_dev_environments`）；
且原文描述的技术（"断言返回 404 而非 403"）**弱于**实际实现的口径 ——
实际判的是路由表成员资格。
"""

from __future__ import annotations

from rednote.modules.devtools.api import router

__all__ = ["router"]
