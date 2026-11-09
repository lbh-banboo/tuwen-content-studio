# deploy/dev —— 内网开发/试点环境（WP05）

> **本机（开发机）未执行本编排。**
> 原因：本机 `docker` / `colima` 不可用 —— 拉取 colima VM 镜像时被网关阻断，重试两次均失败。
> 因此本目录的文件是「写好但未在本机跑过」的编排；**必须在具备 Docker 的内网环境执行并回填证据**。
> 本机可做的替代验证见本文末尾「四、本机替代验证（原生 PostgreSQL）」。
>
> 对应的 G0 条目：G0-18（最小链可启动）、G0-19（重启不丢数据）、G0-20（无公网启动）、G0-21（备份恢复小样）。

## 一、包含什么

| 文件 | 作用 |
|---|---|
| `docker-compose.yml` | PostgreSQL 17 + pgvector、MinIO、RabbitMQ 4、Keycloak（profile `auth`，默认不起） |
| `.env.example` | 全部环境变量样例；`cp .env.example .env` 后改口令 |
| `initdb/01_init.sql` | 首次初始化：装扩展、建最小表（`smoke_meta` / `quota_accounts`）、建非超级用户应用角色 |

技术基线来自 `outputs/Rednote研发执行计划_V2.0_2026-09-19.md` §5：

- 主库 PostgreSQL 17 + pgvector 0.8 系列 + pg_trgm → 镜像 `pgvector/pgvector:pg17`
- 队列 RabbitMQ 4.x（quorum queue）→ 镜像 `rabbitmq:4-management`
- 对象存储 MinIO → 镜像 `minio/minio:latest`（**dev 用 latest；生产必须锁 digest**，见下文「三、版本口径」）
- 身份 Keycloak 26.x → 镜像 `quay.io/keycloak/keycloak:26.0`（`[待核实]`）

## 二、端口表

| 服务 | 容器端口 | 宿主端口（默认 / 变量） | 用途 | 生产是否对用户网段暴露 |
|---|---|---|---|---|
| PostgreSQL | 5432 | 5432 / `PG_PORT` | 主库 | 否（仅应用网段） |
| MinIO S3 API | 9000 | 9000 / `MINIO_API_PORT` | 对象读写 | 否（仅应用网段） |
| MinIO Console | 9001 | 9001 / `MINIO_CONSOLE_PORT` | 人工核对 | 否 |
| RabbitMQ AMQP | 5672 | 5672 / `RABBITMQ_AMQP_PORT` | 任务队列 | 否（仅应用网段） |
| RabbitMQ Console | 15672 | 15672 / `RABBITMQ_CONSOLE_PORT` | 人工核对 | 否 |
| Keycloak | 8080 | 8080 / `KEYCLOAK_PORT` | OIDC（profile `auth`） | 需对用户网段开放（登录用） |

网络分两条（`docker-compose.yml` 末尾）：

- `rednote-dev-edge`：普通 bridge，承载上面这些「发布到宿主机」的端口，仅为本机调试。
- `rednote-dev-data`：`internal: true`，**没有默认网关**。M1 起的应用容器只挂这条网，天然拿不到公网出口；
  数据库 / AMQP / MinIO / Keycloak 同时挂两条网，应用与宿主机都能连到它们。

> 研发计划 §4 的口径：出口只有两处（模型网关、采集适配器）；数据库、AMQP、MinIO、Keycloak 不对用户网段开放。
> 本编排把端口发布出来**只是为了开发机调试**，内网部署时要删掉数据面服务的 `ports:` 段。

## 三、版本口径（重要）

- **MinIO 用 `latest` 是 dev 特权**。生产必须锁 digest（研发计划 §5：实施第一周锁补丁版本并产出 SBOM，不使用浮动 latest）。
  生产写法：`image: minio/minio@sha256:<64 位十六进制 digest>`，digest 值需在开场当周核官方发布页后写入。
  本文件**不写具体 RELEASE 日期 tag** —— 那个值是 `[待核实]` 项，不能凭印象填。
- **Keycloak `26.0` 通道基线待核实**。`[待核实: 内网 `docker pull quay.io/keycloak/keycloak:26.0` 是否命中；
  不命中则改为当周官方最新 26.x 补丁 tag]`
- **健康检查有两处待核实**（本机无法运行 Docker，无法验证）：
  1. MinIO 的 `curl /minio/health/live` 依赖镜像内存在 `curl`。
     `[待核实: `docker compose ps` 观察 minio 的 health 状态；若镜像无 curl，改为 `["CMD","mc","ready","local"]`]`
  2. Keycloak 用 bash 的 `/dev/tcp` 探管理端口 9000。
     `[待核实: 若镜像内无 bash，改为 `["CMD","/opt/keycloak/bin/kc.sh","show-config"]`]`
- **`KC_BOOTSTRAP_ADMIN_*` 变量名**：Keycloak 26 起用 `KC_BOOTSTRAP_ADMIN_USERNAME/PASSWORD` 取代旧的 `KEYCLOAK_ADMIN/KEYCLOAK_ADMIN_PASSWORD`。
  `[待核实: 以该 tag 的官方文档为准；变量名写错会导致管理员建不出来]`

## 四、启动 / 停止 / 重置

```bash
cd deploy/dev

# 0) 准备 .env（必须做，compose 里用 ${VAR:?} 强制要求这些变量存在）
cp .env.example .env
$EDITOR .env          # 把所有 CHANGE_ME_* 改掉

# 1) 启动数据面（不含 Keycloak）
docker compose up -d

# 1b) 需要 Keycloak 时
docker compose --profile auth up -d

# 2) 看状态与日志
docker compose ps
docker compose logs -f postgres
docker compose logs --tail=100 postgres   # 看 01_init.sql 的 NOTICE 输出（扩展清单）

# 3) 停止（保留数据卷）
docker compose down

# 4) 重启（G0-19 就是验证这一步之后数据还在）
docker compose restart

# 5) 彻底重置（会删掉所有数据卷！）
docker compose down -v
# 只重置某一个服务的数据：
docker compose down
docker volume rm rednote-dev-pg-data
```

**`01_init.sql` 只在数据目录为空时执行一次**（官方 entrypoint 行为）。
改了初始化 SQL 又想让它生效，必须 `docker compose down -v`（或删掉对应卷）后重来；否则会被静默跳过。

## 五、冒烟：写入 → 重启 → 校验（G0-19）

冒烟脚本在 `tools/m0/wp05_smoke/smoke.py`。容器环境里连的是 compose 里的 `rednote` / `rednote` 库，
与脚本默认值（`rednote` / `postgres`，面向本机原生 PG）不同，所以要用环境变量指过去：

```bash
cd <项目根>            # 必须在项目根执行，报告路径 evidence/m0/... 是相对路径

export PG_HOST=localhost
export PG_PORT=5432
export PG_USER=rednote
export PG_DATABASE=rednote
export PG_PASSWORD=<.env 里的 PG_PASSWORD>

export S3_ENDPOINT=http://localhost:9000
export S3_ACCESS_KEY=<.env 里的 MINIO_ROOT_USER>
export S3_SECRET_KEY=<.env 里的 MINIO_ROOT_PASSWORD>
export S3_BUCKET=rednote-dev

PY=/Users/<user>/.workbuddy/binaries/python/envs/default/bin/python

# 1) 写入：PG 建表 + 写一行元数据 + 往 MinIO 写一个对象
$PY tools/m0/wp05_smoke/smoke.py --write --backend s3 --report evidence/m0/WP05-smoke-write.json

# 2) 重启（注意：不要用 down -v，那会连卷一起删掉，测不出"重启不丢"）
docker compose -f deploy/dev/docker-compose.yml restart

# 3) 校验：读回最近一次写入，比对 SHA-256 与字节数
$PY tools/m0/wp05_smoke/smoke.py --verify --backend s3 --report evidence/m0/WP05-smoke-verify.json

# 4) 归档日志（把上面两条命令与输出存成证据）
$PY tools/m0/wp05_smoke/smoke.py --write  --backend s3  2>&1 | tee -a evidence/m0/WP05-smoke.log
$PY tools/m0/wp05_smoke/smoke.py --verify --backend s3  2>&1 | tee -a evidence/m0/WP05-smoke.log
```

退出码：全部通过 `0`，任一失败 `1`。报告 JSON 里 `passed` 字段是最终判定，`checks` 是逐项明细。

**顺序很重要**：`--verify` 取的是 `smoke_meta` 里 `created_at` 最新的一行，
所以必须先 `--write`、再重启、再 `--verify`，中间不要插第二次 `--write`。

## 六、本机替代验证（原生 PostgreSQL，Docker 不可用时）

本机没有 Docker，但有一个 brew 装的**原生 PostgreSQL 17.10**（`localhost:5432`，用户 `rednote`，库 `postgres`，无口令）。
它不能替代 MinIO / RabbitMQ，但**可以真实地验证 PG 腿 + 「重启不丢元数据」**：

```bash
cd <项目根>

# 1) 初始化 SQL 的语法与可执行性干跑（事务内执行后回滚，不留副作用）
psql -h localhost -p 5432 -U rednote -d postgres -v ON_ERROR_STOP=1 -X \
  -c "BEGIN;" -f deploy/dev/initdb/01_init.sql -c "ROLLBACK;"

# 2) 写入（文件腿用 fs 降级替身）
PY=/Users/<user>/.workbuddy/binaries/python/envs/default/bin/python
$PY tools/m0/wp05_smoke/smoke.py --write --backend fs --report evidence/m0/WP05-smoke-write.json

# 3) 重启 PostgreSQL —— 先确认服务名
brew services list
brew services restart postgresql@17

# 4) 校验
$PY tools/m0/wp05_smoke/smoke.py --verify --backend fs --report evidence/m0/WP05-smoke-verify.json
```

`--backend fs` 会把文件腿降级为本地目录（`evidence/m0/blobs/`），
脚本每次都会打印「**这是降级替身，不代表 MinIO 行为**」，报告里也标 `degraded: true`。
**对象存储相关的 G0 判据不能用 fs 结论去填。**

本机受执行环境限制产生的其他阻碍，逐条记录在 `evidence/m0/WP05-smoke.log` 的 [7] 系列小节里，
其中两条会影响后续复现，务必先看：

1. `brew services restart postgresql@17` 在本机被拒（无法写 `~/Library/LaunchAgents`）。
2. **由自动化执行环境拉起的 postgres 进程，在数据目录里做 `unlink`/`rename` 会被拒（Operation not permitted）**，
   会导致 checkpoint `PANIC` 并进入 crash recovery 循环。
   → 结论：**启停 PostgreSQL 请在普通终端里做**，不要在自动化执行环境里做。
   → 交回人工前已把崩溃残留的 `postmaster.pid` 重命名为 `postmaster.pid.wp05-stale-*`，
     避免人工启动时撞 `lock file already exists`。这些文件确认无误后可自行删除。

## 七、本机做不到、必须内网补做的条目

| G0 | 条目 | 为什么本机做不了 |
|---|---|---|
| G0-18 | 最小链可启动（PG+pgvector / MinIO / RabbitMQ） | 无 Docker；pgvector 也装不上（见 `docs/m0/WP05-最小环境说明.md`） |
| G0-19 | 重启后文件与元数据不丢 | 本机只用原生 PG + fs 验了元数据腿，**对象存储腿未验** |
| G0-20 | 无公网 CDN / 首次启动不下载依赖 | 需要先在断网环境拉齐镜像；本机连 Docker 都没有 |
| G0-21 | 备份小样可恢复（至少 dump 恢复一次） | 本机未做；`pg_dump` 在原生 PG 上可用，可在内网与其他项一并做 |
