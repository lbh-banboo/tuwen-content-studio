# M4 mock server（只读样例 API）

> 用途：M4 契约先行线（`docs/m4/M4并行开发接缝冻结.md` §4.1）的联调桩。
> **不是生产代码**：不进 CI 门、不碰数据库、不持久化。

## 跑起来

```bash
cd tools/dev/mock_m4
# 本机代理会劫持 localhost ⇒ 必须显式绕开
NO_PROXY='*' /Users/<user>/.workbuddy/binaries/python/envs/default/bin/python mock_server.py --port 8787
```

打开 `http://127.0.0.1:8787/api/v1/projects/00000000-0000-4000-8000-000000000001/deliveries` 即可看到分页包裹 `{data, meta.page}`。

路径与 `contracts/draft/m4-openapi.yaml` 一致（项目范围 + 名词化子资源）；mock 内部路由仍是旧的平铺形态，由 `_normalize_path` 归一化，`projectId` 在 mock 里不参与过滤。

## 自检

```bash
NO_PROXY='*' /Users/<user>/.workbuddy/binaries/python/envs/default/bin/python mock_server.py --selftest
# 期望：RESULT: 12/12，EXIT=0
```

自检覆盖 12 条断言：分页包裹形状、单体包裹形状、404 错误体、错误码合法性
（在 18 个 `ErrorCode` 枚举内）、幂等冲突 409、重复链接 409、状态机非法迁移 409、
创建 201、`metric-latest` 数组。

## 文件

| 文件 | 说明 |
|---|---|
| `mock_server.py` | 标准库实现的 HTTP 服务 + 路由 + 自检（**零第三方依赖**） |
| `samples.json` | 只读样例数据（**全部虚构**，勿当真实数据） |

## 口径（必须与契约一致）

- 单体响应 `{data, meta}`；列表 `{data: [...], meta: {page: {nextCursor, hasMore, total}}}`。
- 错误 `{code, message, details, retryable}`；`code` 只取 18 个枚举值；只有 `RATE_LIMITED` / `DEPENDENCY_UNAVAILABLE` 可重试。
- 契约权威：`contracts/draft/m4-openapi.yaml`（36 paths / 46 ops，2026-10-25 路径统一修订后；schema 数以 `python3 -c` 实测为准）。

## 为什么用标准库而不是 FastAPI

mock 要在**还没装后端依赖**的环境里直接跑起来。`http.server` 零依赖，
前端同学 clone 下来一条命令即可。代价是没有自动生成的 `/docs` —— 需要的话看契约 YAML。

## 演示用约定

| 请求 | 结果 |
|---|---|
| `POST ...`，body 含 `"__conflict": true` | 返回 409 `IDEMPOTENCY_CONFLICT`（演示同键异体） |
| `POST /publications/{id}/links`，`canonicalUrl` 命中样例 | 返回 409 `DUPLICATE_PUBLICATION` |
| `POST /deliveries/{id}/deliver`，该交付非 `PREPARED` | 返回 409 `INVALID_TRANSITION` |
| `POST /metric-adapters/{id}/refresh` | 恒返回 `outcome=NO_CHANNEL`（D3：无渠道是预期内结果，不是缺陷） |
