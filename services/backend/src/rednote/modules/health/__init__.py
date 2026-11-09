"""健康检查模块。

竖切片的定位：只证明"从 HTTP 入口 → 路由 → 平台层（配置/错误/分页/数据库）
→ 真实 PG"这条链路是通的，不含任何业务实体。业务模块（projects/contents 等）
在 M1 后续 WP10~WP12 加入。
"""

from rednote.modules.health.api import router

__all__ = ["router"]