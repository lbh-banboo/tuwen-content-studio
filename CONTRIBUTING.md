# 贡献指南

## 开工前

1. 先读 `README.md` 的「项目状态」——很多组件是占位或未验证实现，别把它当已完成能力。
2. 读懂要改的模块所对应的 `docs/` 说明与 ADR，尤其是关于「判据」和「口径」的约定。

## 提交要求

- 提交前跑 `cd services/backend && python -m pytest`。
- 若改动涉及 `src/**` 的顶层 import，同步更新 `services/backend/pyproject.toml` 的直依赖列表，
  并跑 `tools/quality/check_dependency_declarations.py`。
- `tools/quality/` 下的 `check_*.py` 是工程判据，不是普通脚本；新增或改动判据时，
  同步更新对应的覆盖范围登记与 CI 门，避免出现「声称有防线但没跑」的情况。
- 提交信息用简短的说明性前缀（`feat` / `fix` / `docs` / `chore` / `test`）。

## 数据红线

- **不要**提交真实 `.env`、密钥、证书、Cookie、浏览器 profile、数据库导出或真实用户数据。
- 测试数据一律用虚构值：`@example.com`、`13800000000` 类示例号段、`https://example.com/...`。
- 不要在代码或文档里写本机绝对路径、本机用户名或内网域名。
- 新增示例数据请在 PR 描述里说明它是合成的。

## 设计约定

- 队列以 PostgreSQL 表为真相；事件「先 publish 后 mark_published」。
- 租约过期的 RUNNING 任务进入 `WAITING_RECONCILIATION`（失联不等于失败）。
- 「当前版本」由显式指针 `content_items.current_version_id` 投影决定，不按版本号取末版。
- 出网能力只声明在 `ContentGenerator` Protocol 上；预占、结算、提示词解析由 service 直调 bridge。

细节以 `docs/` 内对应说明为准。
