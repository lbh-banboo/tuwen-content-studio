"""项目领域模块（WP09 / WP10）。

职责：
- 项目、品牌、产品、项目成员、负责人变更记录的存储模型（`models.py`）
- 项目级权限判定（`permissions.py`，需求 §2.2 矩阵的代码化）
- 按对象访问的依赖注入（`dependencies.py`）
- 项目接口（`api.py`）：列表 / 创建 / 详情 / 编辑 / 交接 / 成员
- 字典接口（`dictionaries.py`）：品牌 / 产品
- 共用的 OpenAPI 错误响应声明（`api_responses.py`）
- 创建项目的领域服务（`service.py`：编号生成 + 冲突重试 + 成员写入）

**WP09 与 WP10 的边界**：
- WP09：权限模型 —— 谁能看/改/结项哪个项目。
  验收标准是"管理者全权、运营限项目、知识共享互不混淆"。
- WP10（当前）：品牌产品字典、项目创建/列表/筛选、负责人交接。
  验收标准是"并发交接仍一名负责人；待办和历史正确"。

**WP10 相对 WP09 的两处表结构变更**（都在迁移 `20260921_1xxx` 中）：
1. `brands` / `products` 补 `lock_version` —— 字典是可改名/归档的
   mutable 资源，按数据结构文档 02 §1 应带 `lock_version` 作为 ETag 载体。
   WP09 建表时遗漏，没有它就无法对字典做乐观并发保护。
2. 新增 `work_items`（待办）与 `project_assignments`（负责人变更记录）——
   前者依据文档 02 §3 的定义，后者依据需求 §2.3 第 1 条
   "保存变更原因"与协议 §3 的 `201 Assignment`
   （文档 02 的表清单漏列了 assignment，属缺口，已登记）。

**`projects` 表本身在 WP10 未做任何变更** —— 负责人交接靠
`uq_project_members_active_owner` 与 `projects.lock_version` 两个既有机制完成。
"""

from rednote.modules.projects.api import router
from rednote.modules.projects.dictionaries import router as dictionaries_router

__all__ = ["dictionaries_router", "router"]