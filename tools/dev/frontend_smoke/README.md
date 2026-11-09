# frontend_smoke —— 前端接线冒烟（浏览器级）

验证 `services/backend/static/api.js` 与两个已接线页面（`project-overview.html`、`projects.html`）：
后端可达且已登录 → 渲染真实数据；不可达 / 未登录 / 接口报错 → 降级为原静态样例且无 JS 异常；
并检查 api.js 的错误解包、`Idempotency-Key` 复用、CSRF、`If-Match` 透传。

## 前置（本脚本不会安装任何东西）
- 本机已有 PostgreSQL 17（`/opt/homebrew/opt/postgresql@17/bin`，含 pgvector）。
- 后端 venv：`services/backend/.venv`。
- Node + 已装的 `playwright` 包（默认取 `/Users/<user>/.hermes/hermes-agent/node_modules`，可用 `PLAYWRIGHT_DIR` 覆盖）
  与 `~/Library/Caches/ms-playwright` 下匹配版本的 chromium headless shell。

## 用法
```bash
bash tools/dev/frontend_smoke/run.sh      # 退出码 0 = 全通过
```
`run.sh` 会：建/复用独立库 `rednote_fe_smoke` → alembic 迁移 → 以 dev 环境起后端（默认 8799，同源托管 static，
启用开发期登录旁路）→ 另起一个纯静态服务（8798，无 API，用来模拟「只开了静态页」）→ 跑 `smoke.mjs` → 结束时杀掉两个进程。
不碰其它库；库 `rednote_fe_smoke` 保留，每次运行会向其中追加带随机后缀的品牌/产品/项目。
可用环境变量 `PORT` / `SPORT` 改端口。后端日志在 `/tmp/fe_smoke_backend.log`。

## 覆盖
1. 已登录 + 可达：标题、编号、截止日期、品牌/产品、列表行来自接口；无硬编码样例行。
2. 网络中止（`route.abort`）、纯静态服务（无 API）、未登录（401）：保留样例 + 横幅提示 + 无 `pageerror`。
3. 业务接口返回标准错误体（503 `DEPENDENCY_UNAVAILABLE`）：保留样例，横幅显示后端原 code。
4. api.js：404 → `ApiError{kind:'BACKEND',code:'RESOURCE_NOT_FOUND'}`；写操作同 opts 复用幂等键、带 CSRF、透传 If-Match；真实 POST 成功。

## 局限
- 外链字体/图标走 CDN（fonts.googleapis.com、cdnjs），内网不可用，页面图标/字体会缺失但不影响功能；本脚本不处理。
- 未覆盖「后端掉线后再恢复」「登录态中途变化」等时序场景。
