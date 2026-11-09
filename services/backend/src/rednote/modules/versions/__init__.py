"""通用版本注册与引用（WP11）。

**对外接口**：`service.py` 里的函数。下游模块（Brief / 知识 / 方案 / 内容 /
任务 / 影响图）只调这些函数，不直接读表。

**本模块不提供 HTTP 端点**。协议 03 的端点表里没有任何 `/versions/*` 路径；
各领域模块自己的版本历史端点（`GET /contents/{id}/versions`、
`GET /knowledge/assets/{id}/versions` 等）才是真实入口，
它们复用同一套游标分页工具与本模块的服务函数。
这个决定与理由写在 `docs/m1/WP11-版本注册与引用说明.md` §3，
并登记为待定项（通用版本端点是否需要）。
"""

from rednote.modules.versions.models import (  # noqa: F401  确保模型随包被 import
    VersionDependency,
    VersionNode,
)

__all__: list[str] = []
