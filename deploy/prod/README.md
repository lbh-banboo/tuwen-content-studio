# rednote-production 内网生产部署手册（WP46）

> 判据载体：`tools/quality/check_offline_bundle.py`（29 条）、`tools/quality/check_sbom_coverage.py`（14 条）
> 反向验证：`tools/quality/collect_sbom_coverage_reverse_evidence.py`（8 用例）
> 证据：`evidence/m5/wp46-*.log`、`evidence/m5/wp46-sbom-snapshot.cdx.json`
> 完整说明：`docs/m5/WP46-内网安装包与SBOM说明.md`
> 更新：2026-10-18

---

## 0. 先读这一节：这份手册能读作什么、不能读作什么

本包的交付对象是**断网内网 + 真机 + 真 digest**。这三样在产出本手册的开发机上
**一样都不存在**（Docker daemon 不可达、无隔离网络、拉不到镜像）。
因此本手册里的每一句话都必须带着验证层级读。

### 0.1 禁用句式（写在最前面，因为它决定了后面每一句怎么读）

下面四类说法在本包的任何文档与脚本输出里**都不会出现**，判据
`prod:no-overclaim-phrases` 逐行扫描并报红：

- ❌ 「离线安装包已验证可用」 [禁用句式示例]
- ❌ 「断网可一键安装」 [禁用句式示例]
- ❌ 「版本已锁定」（digest 还是占位时） [禁用句式示例]
- ❌ 「SBOM 覆盖全部依赖」（L-D 镜像内 OS 包层未覆盖） [禁用句式示例]

只能这样写：

- ✅ 「离线包**构建链**在本机跑通并产出清单与校验值；**断网安装未验**，见 §5 第 2 项」
- ✅ 「SBOM 覆盖 Python 直接依赖 + 传递闭包 + 镜像引用层**三层**；镜像内 OS 包层**未覆盖**，由判据 `sbom:uncovered-layers-declared` 显式声明」
- ✅ 「镜像引用的**形态**已落地（无 `latest`、无裸名、未锁 digest 的逐条登记）；**digest 实值 7 条全部待回填**，由判据 `prod:pending-digests-monotonic` 计数」

> 判据侧的机制：引用禁用句式本身的行必须带哨兵 `[禁用句式示例]`，
> 且**哨兵行数被计数钉住**（`prod:overclaim-exempt-lines-monotonic`）——
> 否则有人加个哨兵就能把真声称洗白。

### 0.2 当前状态的一句话总结

**这份编排在 digest / tag 回填前起不来，这是刻意的。**

`images.lock.json` 的 7 条镜像引用里：
- 3 条（minio / gateway / prometheus）连 tag 都未定，ref 里写的是字面 `PENDING`，`docker pull` 必失败；
- 4 条（postgres / rabbitmq / keycloak / app）带真实上游通道 tag，但**没锁 digest**；
- 0 条是 `name@sha256:<64hex>` 形态。

起不来比「悄悄拉到一个别的版本」安全。回填步骤见 §2。

---

## 1. 目录内容

| 文件 | 作用 |
|---|---|
| `docker-compose.yml` | 生产编排：C1 PG+pgvector / C2 RabbitMQ / C3 MinIO / C4 Keycloak（选装）/ C8 双 API + Worker + 反代 / C6 Prometheus |
| `.env.example` | 口令与镜像引用模板。`cp .env.example .env` 后改。真 `.env` 永不入库 |
| `images.lock.json` | **镜像引用的唯一权威**。`.env` 只是它的投影，判据会三方对账 |
| `nginx/rednote.conf` | 反代配置（**只有 HTTP** —— HTTPS 依赖 E2 内部 CA，未落地） |
| `install.sh` | 离线安装：校验介质 → `docker load` → 离线装 wheel → 迁移 → `up -d` → 就绪轮询 |
| `upgrade.sh` | 升级：停 worker → **调 `tools/ops/backup_pg.py` 备份** → load → 迁移 → 换镜像 → 就绪 |
| `uninstall.sh` | 卸载：`down`（**默认不删卷**）；`--purge` 才删卷，且要打出 `DELETE-VOLUMES` 二次确认 |

**不在这里的东西，以及为什么：**

| 不在 | 归属 |
|---|---|
| Harbor 的安装编排 | Harbor 是**宿主设施**（ADR-11 C5 / S1），装它的那一刻还没有离线包（鸡生蛋）⇒ 它是运维手册的「第 0 步」 |
| Prometheus 抓取配置与告警规则 | WP47② 已交付在 `deploy/monitoring/`。本编排**只读挂载**它，不复制第二份 —— 复制 ⇒ 两份规则漂移，而 `check_alerting_rules.py` 只盯 `deploy/monitoring/` ⇒ 这边的悬空指标无人守。判据 `prod:monitoring-not-duplicated` 断言本目录下不存在规则文件 |
| ansible / helm / K8s | `outputs/项目部署方式与发布节奏说明_2026-10-15.md:53` 明确不建议为本项目建完整 K8s；2~3 节点下 ansible 的收益低于它自身的离线安装与维护成本 |
| 应用镜像的 Dockerfile | ⚠️ **本仓当前没有 Dockerfile** ⇒ `IMAGE_APP` 现在造不出来。这是一个**真实缺口**，归属待拍板，见 `docs/m5/WP46-内网安装包与SBOM说明.md` §0.1 |

---

## 2. 回填镜像引用（内网首日第一件事）

在**可出网且 daemon 可用**的机器上：

```bash
# 1 取 digest
docker pull <ref>
docker inspect --format '{{index .RepoDigests 0}}' <ref>      # => name@sha256:<64hex>

# 2 写进 images.lock.json：ref / digest 两个字段 + pin_state 改成 "digest"
# 3 把 .env（或 .env.example）里对应的 IMAGE_* 改成同一个 ref
# 4 判据必须仍然 PASS，且占位计数**下降**：
python tools/quality/check_offline_bundle.py
#   prod:pending-digests-monotonic 会报「实测 N ≠ 基线 7」⇒
#   把脚本里的 _PENDING_DIGEST_BASELINE 同步调小（**只许减，不许增**）
```

内网走 Harbor 时 ref 的**名字前缀本身会变**（`registry.example.com/rednote/...`）。
这正是 compose 里每个 `image:` 都写成 `${IMAGE_*}` 的原因：改 `.env` 即可，
不用改 compose —— 而改过的 compose 不再被判据守着。

对应的待定项：OP-14（MinIO digest，登记册备注原文「当前**刻意留空，不凭印象填**」）、
OP-52（Keycloak 版本与 OIDC 端点）、以及 5 处**待主会话串行申领 OP 号**的条目
（判据 `prod:unallocated-op-monotonic` 把它们逐条列出来）。

---

## 3. 安装

```bash
cd deploy/prod
cp .env.example .env
# 改掉所有 CHANGE_ME；回填 IMAGE_*（见 §2）
./install.sh --bundle /media/rednote-offline-<ts>

# 先看一遍将要做什么（不产生任何副作用）：
./install.sh --bundle /media/rednote-offline-<ts> --dry-run
```

六步：前置检查 → 校验 `SHA256SUMS.txt` → `docker load` → 离线装 wheel
（`pip install --no-index --find-links`）→ 起数据面 + 等健康 → `alembic upgrade head`
→ 起应用面 + 网关。

**选装 Keycloak**：`--with-auth`。注意它需要一个**独立的库**（`.env` 的 `KEYCLOAK_DB`），
而 `../dev/initdb` 不会自动建它 ⇒ 需人工 `createdb`。
该路径**在任何环境均未验证过**（OP-52）。

### 本机对 install.sh 验到了什么

| 验到 | 层级 | 读法 |
|---|---|---|
| `bash -n` 语法通过 | L1 | 可读作「语法合法」 |
| 正文无联网动作（无 `curl`/`wget`/`docker pull`，每处 `pip install` 带 `--no-index`） | L1 | 只能读作「脚本里没写联网动作」。**不能**读作「运行时不联网」—— 容器自己是否发起外网请求要内网抓包 |
| `--dry-run` 走通 6 步、连跑两遍输出逐字相同 | L2 | 只能读作「参数解析与步骤顺序没问题、输出确定」。**不能**读作「装得上」：`docker load` / `up -d` **一步都没执行** |
| 断网下真的装上 | **L3** | 本机做不到，见 §5 第 2 项 |

---

## 4. 升级与回滚

```bash
./upgrade.sh --bundle /media/rednote-offline-<新版本ts> --backup-dir /srv/backup
```

六步：停 worker → **备份** → 校验并 load 新镜像 → 迁移 → 换镜像 → 就绪核验 → 恢复 worker。

**备份这一步调 `tools/ops/backup_pg.py`，不自己写 `pg_dump`**
（判据 `prod:upgrade-delegates-backup` 断言这件事）。理由：
① 那个脚本除 dump 之外还产出 manifest（6 类结构指纹 + 行数快照 + sha256），
而升级失败要回滚时手上必须有「回滚到哪个状态」的可机读描述；
② 它的 `_pg_bin()` 已经把「本机 Homebrew 路径 vs CI apt 路径」的回退收敛成**一份**
（该文件 `:70-86` 的 docstring 记了首版写死 Homebrew 路径、本机通 CI 必红那次）。

**回滚**：任何一步失败，手上都有第 2 步的备份。
- 迁移层回滚：`python -m alembic -c migrations/alembic.ini downgrade <上一 revision>`，
  演练见 `tools/quality/check_rollback_drill.py`（WP47⑤，24 条）；
- 数据层回滚：用第 2 步的备份恢复，演练见 `tools/quality/check_backup_restore_drill.py`（WP47④，21 条）。

⚠️ 真机升级 + 回滚的**幂等**属 L3（见 §5 第 6 项）。本机只验到 `--dry-run` 输出确定。

---

## 5. 内网待跑清单（本机无法验证，不得当生产证据）

| # | 项 | 为什么本机做不了 | 依据 |
|---|---|---|---|
| 1 | `docker save` / `docker load` | daemon 不可达（实测 `docker info` → orbstack socket 不存在）+ 拉不到镜像（ISS-01 受控出网） | `evidence/m5/wp46-env-baseline.log` |
| 2 | **断网 `up -d` 全绿、无 DNS/超时重试** | 本机无 daemon、无隔离网络 | G0-20：`docs/m0/WP05-最小环境说明.md:96`、`:144-146`；ADR-11 验证表 |
| 3 | 内网 Linux 上 `pip install --no-index --find-links wheelhouse` 成功 | 本机 **darwin/arm64**，manylinux wheel 在本机根本装不上 | `docs/m5/WP46-内网安装包与SBOM说明.md` §6 L2 第 1 条 |
| 4 | 每个镜像的**真 digest** 回填（7 条） | 需可出网机 `docker pull` + 内网 Harbor | OP-14、OP-52 + 5 处待申领 OP |
| 5 | Harbor 推送 / 拉取链路 | 需内网 Harbor（ADR-11 C5 / S1） | ADR-11 组件表 |
| 6 | 一键安装 / 升级 / 卸载的**真机幂等** | 需真机、真卷、真迁移 | 计划 §4.5「运维按手册独立完成一次升级+回滚」 |
| 7 | 反向代理 + HTTPS + 内部 CA（C8） | 依赖 E2 内部域名 / DNS / CA 未落地 | `nginx/rednote.conf` 末尾注释里留了要加的 HTTPS 段 |
| 8 | 双 API 实例 + **RabbitMQ 3 节点 quorum** | 需 ≥3 节点。本编排是**单节点** RabbitMQ，ADR-11 明写「单节点验证不得当生产证据」 | ADR-11 |
| 9 | 组件隔离（用户网段不可达 PG / AMQP / MinIO） | 需企业网络（WP01 N5）。本编排已做到「不发布管理端口」，但那只是结构性保障 | ADR-11 |
| 10 | `/metrics` 仅运维面可达的**部署层回执** | 需网关。关闭条件是「从外网实测一次应返回 403/404 而非 200」 | **OP-191**（🔴 P0） |
| 11 | 镜像内 OS 包级 SBOM（L-D） | 无 syft/trivy（本机实测四个工具零命中） | 判据 `sbom:uncovered-layers-declared` 显式登记为未覆盖层 |
| 12 | 介质完整性（U 盘拷贝后 sha256） | 无介质。`install.sh` 步骤 1 的校验逻辑已就位但未在真介质上跑过 | ADR-11 验证表 |
| 13 | 应用镜像的构建（`IMAGE_APP`） | **本仓无 Dockerfile** —— 这不是"本机做不到"，是**还没做** | 归属待拍板，见说明文档 §0.1 |

---

## 6. 卸载

```bash
./uninstall.sh                      # down —— 删容器与网络，**保留数据卷**
./uninstall.sh --purge              # 额外删卷（要打出 DELETE-VOLUMES 二次确认）
./uninstall.sh --purge --also-images
./uninstall.sh --dry-run            # 只打印
```

**默认不删卷**不是谨慎，是硬依据：ADR-11 的验证表明文写着
「重启不丢数据…**不得 `down -v`**」—— G0-19（数据持久化）的验收点靠的正是
「卷不被顺手删掉」。一个默认 `down -v` 的卸载脚本等于把那条验收点拆了，
而且是在「运维想把环境清干净重装一次」这种最无防备的场景下拆的。

判据用的是**行为核对**而不是文本扫描（扫文本要解析 shell 控制流，写错会静默放过）：
- `prod:uninstall-default-keeps-volumes`：跑 `--dry-run`，断言打出的 `down` 行**不含** `--volumes`；
- `prod:uninstall-purge-does-delete`：跑 `--purge --yes --dry-run`，断言**含** `--volumes`
  —— 这是对照组，否则上一条可能只是因为脚本什么都不做而绿。

---

## 7. 判据怎么跑

```bash
python tools/quality/check_offline_bundle.py                        # 29 条
python tools/quality/check_sbom_coverage.py                         # 14 条
python tools/quality/collect_sbom_coverage_reverse_evidence.py      # 8 用例
python tools/m5/collect_wp46_evidence.py                            # 证据落盘
```

SBOM 与 wheelhouse 判据是 **fail-closed** 的：wheelhouse 不存在或为空时
**报红并打印「无法构造对账」**，不会 SKIP。先构建：

```bash
python tools/ops/build_offline_bundle.py wheelhouse
python tools/ops/gen_sbom.py
python tools/ops/build_offline_bundle.py manifest
```

⚠️ `python tools/ops/build_offline_bundle.py images` 在本机**必失败**（无 daemon
+ 3 条引用未解析），这是设计如此 —— 日志里「跳过了」与「打包好了」不能同形。
