"""任务、租约与 Outbox 派发（WP13）。

**对外接口**：`service.py` 里的函数（`enqueue_job` / `claim_jobs` / `run_job` /
`reclaim_expired_leases` / `begin_idempotent_request` …）。
下游模块只调这些函数，不直接读表。

**本模块不提供 HTTP 端点**（与 WP11 同）。协议 03 的端点表里没有 `/jobs/*`
路径 —— 任务是由别的动作**产生**的（上传完成、提交批次、发起解析），
消费者是各模块的代码与 Worker，不是浏览器。验收因此打在服务层，
而本包的验收项（重复投递不重复执行、旧 Worker 不覆盖新状态）本来就
只能在服务层验证。

**分层**（单向，见 `types.py` 的说明）：

    types ← handlers ← service
    types ← service → repository → models
    dispatch（投递通道与派发器）→ repository

显式 import 模型，使 `migrations/env.py` 的 autogenerate 能看见这 3 张表。
"""

from rednote.modules.jobs import models  # noqa: F401

__all__: list[str] = []
