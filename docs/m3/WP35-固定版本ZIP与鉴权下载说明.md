# WP35 固定版本 ZIP / 鉴权下载 —— 说明文档

> 工作包：WP35（M3 生产，W13）　验收：AC-16~17（静态 + Live）　建议人日：3~5
> 状态：**阶段 1~3 完成，等老大在 §3 选方案后进阶段 4**
> 日期：2026-10-14　基线：HEAD `bc04cc4`，alembic 单 head `c4d9f2a17e35`
> 前置文档：`WP35-追问与拍板清单.md`（五问）、`WP35-五问答复.md`（五问答复稿，**有一处事实错误，见 §1.3**）

---

## 1. 阶段 1：需求追问结论

### 1.1 五问定案

| # | 问题 | 定案 | 备注 |
|---|---|---|---|
| 1 | Live 验收 | 静态先做，Live 留 `format` 字段 | 理由已变：WP28 解封，Live 侧走桩 + `SYNTHETIC` 标记，禁止出现「已验证」字样 |
| 2 | 固定版本源 | **打包时冻结快照**，用 `export_items` 承载 | 取数 = `current_version_id` 投影优先 + 为空回落末版，禁 `ORDER BY version_no DESC` |
| 3 | 鉴权 | 沿用项目角色鉴权 | 实测落点见 §2.3：`ProjectAction.DELIVER` 已存在 |
| 4 | 粒度 | ~~按批次~~ → **按项目内一组 content_id**（见 §1.2 Q1） | 只放正式资产 |
| 5 | 流式 | 强制流式，须有断言 | 须有探针，不能只写 docstring（ISS-95 家族） |

### 1.2 本轮三项拍板（2026-10-14）

| 编号 | 决策 | 起因 |
|---|---|---|
| **Q1** | WP35 改成按**项目内一组 content_id** 打包，**不引入批次** | 实测全后端**没有 `batches` 表**。`jobs.batch_id` 注释写明「`batches` 表属 WP24/29，刻意不加 FK」，WP24 已交付但没建。原答复问 4「按批次」没有可依附的实体 |
| **Q2** | **WP35 建 `export_items`**，从 WP36 迁移清单挪出，M4 总数仍 **19** | M4 DDL 草案把该表归 WP36，而 WP36 又硬依赖 WP35，形成循环依赖；磁盘上 `export_items` 零命中 |
| **Q3** | WP35 **先行**，不等 ISS-177/178 | WP35 上游全已交付（WP27 / WP11 / WP12 / WP13），不碰 WP26 的 `execute_generation` |

### 1.3 对原答复稿的更正（事实错误）

`WP35-五问答复.md` 问 4 写「只放正式资产（`media_renditions` 的 `formal`）」。**`media_renditions` 没有 `formal` 字段。**
实测取值域（`media/types.py` L281~L300）：

| 列 | 取值 |
|---|---|
| `kind` | `PREVIEW` / `DELIVERY` |
| `state` | `PENDING` / `READY` / `FAILED` |

⇒ 「正式资产」的判据是 **`kind = 'DELIVERY' AND state = 'READY'`**，每 `(media_version_id, kind)` 取 `MAX(attempt_no)` 的那一行。
`state='READY'` 已有蕴含式 CHECK `ready_requires_artifact`（`file_object_id`/`width`/`height`/`byte_size`/`image_format` 五列齐全）挡住 0 字节产物，所以不必再自己写非空检查，但**必须按 `READY` 过滤**，否则会把 `PENDING`/`FAILED` 行打进包。

### 1.4 阶段 1 其余必答项

| 问 | 答 |
|---|---|
| 真正要解决什么 | 把「一组已定稿的内容」变成**可复现、可鉴权下载、版本固定**的交付物，并留下「这一刻导出了哪一版」的不可变记录（M4 WP36 交付登记的前置） |
| 核心用户 | 运营（下载交付）、管理者（审计导出记录）；下游 WP36 是程序用户 |
| 最小 MVP | 创建导出（冻结快照）→ 流式 ZIP → 鉴权下载 → 重复请求幂等 |
| 疏漏补全 | ① Q1/Q2 暴露的两个缺口（批次实体 / 表归属）；② `formal` 字段不存在；③ **`content_versions.id` 与 `version_nodes.id` 是同一个值**：实测 `content/models.py` L446~L456，`content_versions.id` **本身就是主键兼外键**（延迟 FK → `version_nodes.id`），另有配对触发器 `trg_version_nodes_content_paired`（`content/ddl.py` L213）校验 `entity_type='CONTENT'`。`export_items.version_node_id` 直接引用即可，**不需要映射表**。（媒体侧 `media_versions` 经 `register_version(ENTITY_TYPE_MEDIA)` 同构，`media/ddl.py` L369 有对应触发器；写迁移前再实测一次 `media_versions.id` 的 FK 声明） |
| 现在不该做 | 一次性令牌；预览资产进包；Live 真实封装；按批次分组；ZIP 加密 |
| 技术风险 | 见 §2.6 |

---

## 2. 阶段 2：方案设计

### 2.1 需求本质

**把「当前这一组内容的哪一版」在导出这一刻冻结成不可变记录，并据此流式产出同一份 ZIP。**
「固定版本」的难点不在打包，在**冻结**：下载两次必须得到同一批字节。

### 2.2 用户画像

| 角色 | 怎么用 | 何时 |
|---|---|---|
| 运营 | 勾选若干内容 → 点导出 → 下载 ZIP 交给 KOC | 内容审核通过后 |
| 管理者 | 查导出记录，核对「当时导出的是哪一版」 | 交付争议、结项审计 |
| WP36（程序） | 以 `export_items.id` 为 `deliveries` 的版本来源 | 登记交付时 |

### 2.3 磁盘实测起点（不是推演）

| 事实 | 实测 | 对设计的意义 |
|---|---|---|
| 鉴权动作 | `ProjectAction.DELIVER`（`projects/permissions.py` L88，注释「下载、交付、发布登记、回收数据」） | **直接复用，不新造动作**；查看导出记录用 `VIEW` |
| 项目访问 | `ProjectAccessDep` + `access.require(...)`（`content/api.py` 全文同款） | 路径形如 `/projects/{projectId}/exports/...` 即可沿用 |
| 存储 | `StorageBackend.open_stream(key) -> Iterator[bytes]`、`write_part`、`assemble`、`size`、`exists` | 读源图现成；**但没有「边写边流出」的出口**（见 §2.5） |
| 落盘接缝 | `files.service.store_generated_asset(...)`，`purpose` 可传 `PURPOSE_EXPORT` | 本包是 `PURPOSE_EXPORT` 的**首个消费者**；`EXPORT` 不在 `QUOTA_PURPOSES`，不占研究配额，与需求 §6.2 一致 |
| 版本取数 | `content_items.current_version_id`（延迟 FK，可空，可指旧版） | 取数口径固定为「投影优先 + 为空回落末版」 |
| 内容 ↔ 图 | `media_items.content_item_id` → `media_versions` → `media_renditions(kind, state, attempt_no)` | 图的当前版取 `media_items.current_version_id`，渲染取 `DELIVERY`/`READY`/`MAX(attempt_no)` |
| 响应流 | 全仓仅 `events/api.py` 用过 `StreamingResponse` | ZIP 下载是第二个流式端点，无现成工具函数 |
| 契约 | `contracts/draft/m4-openapi.yaml` 已有 `/export-items` GET/POST、`ExportItem`、`ExportItemCreate`，**但无下载端点、无 content_id 集合入参** | 草案必须修订（§2.4 / §5） |
| 迁移 | 单 head `c4d9f2a17e35`；`export_items` 在 `migrations/` 与 `src/` 均零命中 | 本包新建迁移，`down_revision = c4d9f2a17e35` |

### 2.4 数据结构：`export_items` 的两种形态

Q1 把粒度改成「一组 content_id」后，**原 DDL 草案的 `export_items` 一行 = 一条内容**，表达不了「一组」。必须二选一，这是方案 V1~V4 之间最实质的分叉之一：

| 形态 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A 单表** | 一行 = 一次导出，冻结清单存 `manifest JSONB`（`[{contentItemId, versionNodeId}]`） | 一张表，迁移最小；M4 总数仍 19 | 清单在 JSONB 里，**按 `version_node_id` 反查「哪些导出用过这一版」需要 GIN 或全扫**；WP34「导出资格失效」要的正是这个反查 |
| **B 双表** | `export_batches`（一次导出）+ `export_items`（每内容一行） | 反查走普通索引；FK 保证引用完整；契约现有 `ExportItem` 形状基本保留 | 多一张表 ⇒ **M4 总数 20，而不是 19**，与 Q2「仍 19」冲突 |

⚠️ **这里有一处与 Q2 拍板的张力，需要老大确认**：Q2 说「M4 总数仍 19」，成立的前提是 WP35 只带走 `export_items` **一张表**。若选形态 B 就是 20 张。
我的倾向是 **形态 A + 在 `manifest` 之外冗余一张反查用的普通列**（见 V2），理由：WP34 的反查需求可以用「`export_items` 一行一内容」实现 —— 即**形态 A'**：

> **A'：保持原草案「一行 = 一个 (导出, 内容, 版本)」，新增一列 `export_group_id UUID NOT NULL` 把同一次导出的多行归成一组。**
> 一张表、19 张不变、反查走 `version_node_id` 普通索引、「一组」由 `export_group_id` 表达。幂等键落在组上（见下）。

A' 的代价：`export_group_id` 不是外键（没有「组表」），组级属性（`artifact_ref`、`zip_sha256`、`created_by`）会在组内每行重复 ⇒ 需要 CHECK / 触发器保证同组一致，或只存在「组首行」。这是本方案**最大的设计风险**，写进 §2.6。

### 2.5 流式的真实难点（Q5 的落点）

`StorageBackend` 只有「读出」（`open_stream`）和「写入」（`write_part`/`assemble`），**没有「边打包边吐给 HTTP 响应」的路径**。ZIP 需要两种产出方式，对应「固定版本」和「内存口径」的取舍：

| 方式 | 做法 | 复现性 | 内存 |
|---|---|---|---|
| **预生成** | 创建导出时流式写 ZIP → `store_generated_asset(purpose=EXPORT)` → 下载直接 `open_stream` 回放 | ✅ 字节固定、`sha256` 可核 | ✅ 打包时单图同阶；下载时 chunk 同阶 |
| **边下边打** | 下载时才打包，`StreamingResponse` 现场吐 | ❌ ZIP 的时间戳、遍历顺序若不钉死，两次字节可能不同 | ✅ |

**推荐预生成**：与「固定版本」四个字同构 —— 下载永远回放同一份落盘字节。代价是创建导出多一步同步/异步打包。

> ⚠️ Python `zipfile` 写入时会 `seek` 回头改本地文件头，**对不可 seek 的流要用 `zipfile.ZipFile(..., mode='w')` 配合 `ZipInfo` 的 data descriptor 模式**。打包目标必须是临时文件而非内存 `BytesIO`，否则内存与总包大小同阶，直接违反 Q5。这是 §4 的**强制断言点**。

### 2.6 技术风险评估

| # | 风险 | 级别 | 应对 |
|---|---|---|---|
| R1 | 打包时把总包读进内存（`BytesIO`），违反 Q5 | **高** | 打包目标用临时文件；**必须有探针断言**：打包 N 张图时峰值 RSS 增量 < 单图大小的固定倍数（`tracemalloc`），不能只写 docstring |
| R2 | 取数用了 `ORDER BY version_no DESC`，回滚分叉后导出错版 | **高** | 复用 WP27 已落库的「投影优先 + 空回落」口径；加回滚分叉用例（真库） |
| R3 | `PENDING`/`FAILED` 的 rendition 被打进包 | **高** | 查询条件钉死 `kind='DELIVERY' AND state='READY'`；加「全 FAILED 的内容」负样本 |
| R4 | A' 方案组内多行属性不一致 | 中 | 同组一致性用 DB 约束或触发器钉死；变异实测 |
| R5 | 下载端点绕过鉴权（直接猜 `export_id`） | **高** | 先反查项目 → `resolve_project_access` → `require(DELIVER)`；**跨项目 id 返回 404 而非 403**，避免泄露存在性 |
| R6 | 同一导出请求重试产生两份 ZIP | 中 | `idempotency_key` 部分唯一索引 + 同键异体 409（口径文档 `M4契约与幂等口径.md` 已有先例） |
| R7 | 导出后内容被改稿，用户误以为 ZIP 会变 | 中 | 冻结清单 + ZIP `sha256` 入记录；下载响应带 `ETag = zip_sha256` |
| R8 | 迁移 `downgrade` 误删跨包共用函数（ISS-79 同族） | 中 | 迁移只含本表；按归属删函数；过 `upgrade → downgrade base → upgrade` 往返判据 |
| R9 | 迁移里活引用取值域，parity 判据恒绿（ISS-191） | 中 | 取值域写字面量；`export_kind` 的 CHECK 写成化石 |
| R10 | 与 M4 契约草案、DDL 草案、`WP36` 三方不一致 | 中 | §5 同步改三处并加对账 |
| R11 | Live 分支被误标为已验证 | 中 | `format` 字段只允许 `STATIC`；`LIVE` 在本包一律拒绝（422），不产出任何「已验证」字样 |
| R12 | ISS-177/178 仍 OPEN，WP35 不能依赖 WP26 的生成路径 | 低 | 本包只**读**已生成的内容与图，不调 `execute_generation` |
| R13 | **`export_items.delivery_id` FK 指向尚不存在的 `deliveries`**（WP36 才建，迁移序晚于本包） | **高** | WP35 迁移里**不带 `delivery_id`**；WP36 建 `deliveries` 时反向引用（`deliveries.export_item_id`）。方向与原 DDL 草案相反，须同步 WP36 的说明文档与契约 `ExportItem.deliveryId` 字段（草案 L1048/L1059 现有该字段，需删） |

### 2.7 砍掉清单（现在别做）

一次性下载令牌 / ZIP 加密 / 预览资产入包 / 批次实体 / 按批次分组 / Live 真实封装与真机验证 / ZIP 内嵌水印 / 断点续传下载。

---

## 3. 阶段 3：四套版本方案

> 共同前提（四套都一样）：鉴权 = `ProjectAction.DELIVER`；资产判据 = `DELIVERY`/`READY`/`MAX(attempt_no)`；取数 = 投影优先 + 空回落；`format` 仅 `STATIC`；`export_items` 由本包建。

| | **V1 最快版** | **V2 稳定版（推荐）** | **V3 个人精简版** | **V4 完整版** |
|---|---|---|---|---|
| 定位 | 1~2 天跑通 | 可长期维护，对齐 WP36/34 | 单人最短路径 | 生产级 |
| 表 | `export_items` 单行=单内容，**不做分组** | `export_items` + `export_group_id`（形态 A'） | 同 V2 | 同 V2，另加 `export_group` 组表（⇒ M4 变 20 张） |
| 打包时机 | 下载时现打（边下边打） | **创建时预生成并落盘** | 创建时预生成 | 异步 Job + 预生成 + 完成回调 |
| 复现性 | ❌ 字节不保证一致 | ✅ `sha256` 入记录，ETag | ✅ | ✅ + 摘要核验端点 |
| 内存断言 | 无 | **有（tracemalloc 探针）** | 有 | 有 + 压测基线 |
| 幂等 | 无 | `Idempotency-Key` + 409 | 同 V2 | 同 V2 + 重放缓存 |
| 变异/反向验证 | 无 | 关键判据均有 | 仅 R1~R3 | 全量 |
| 人日 | ~2 | **~4** | ~3 | ~6~7 |

### V1 最快版
- **优点**：最快；无落盘；无需 `store_generated_asset`。
- **缺点**：违反「固定版本」本义（下载两次字节可能不同）；无冻结记录则 WP36 没有版本来源；Q2 拍板的「建 `export_items`」形同虚设。
- **适合**：只要演示一个能下载的按钮。
- **风险**：交付后无法证明「当时导出的是哪一版」，等于没做 WP35。**不建议。**

### V2 稳定版（推荐）
- **优点**：满足 Q1~Q5 全部拍板；M4 保持 19 张；字节固定、可审计、可幂等；WP36/34 直接可用。
- **缺点**：A' 组内一致性要额外约束；创建导出是同步打包，大包（50 篇 × 3 图）会拉长请求。
- **适合**：当前阶段（系统 demo、本机 `LocalStorageBackend`、单后端）。
- **风险**：同步打包的请求时长（50 篇量级约数秒，本机可接受；上线前若不够再升 V4）。

### V3 个人精简版
- **优点**：比 V2 少一轮变异验证，省 ~1 人日。
- **缺点**：变异验证覆盖面缩小，R4/R5/R6 的判据无反向验证 ⇒ 「声称有防线」比没防线危险（项目判据纪律第 3 条）。
- **适合**：时间极紧且愿意把验证债记登记册。
- **风险**：防线无变异实测，易出假绿。

### V4 完整版
- **优点**：异步化，大批量不阻塞请求；有重放缓存与摘要核验端点。
- **缺点**：**组表使 M4 总数变 20，违背 Q2「仍 19」**；要新增 Job 种类，牵动 `jobs` 模块与处理器注册；本包范围膨胀，挤占 WP29~34。
- **适合**：上线前若压测显示同步打包超时。
- **风险**：范围蔓延；与 Q2 拍板冲突。

### 推荐：**V2**

理由三句：满足全部拍板且不动 19 张；「固定版本」靠预生成 + `sha256` 兑现；V4 的异步化是上线前优化，现在做是过度设计。

---

## 4. 阶段 4 的拆分（V2，选定后执行）

| 步 | 内容 | 产出 |
|---|---|---|
| 4.1 | 数据结构：`types.py`（取值域、原因码）、`models.py`（`ExportItem`）、`schemas.py`（纯函数：清单冻结、资格指纹、目录命名） | 新模块 `modules/exports/` |
| 4.2 | 迁移 `export_items`，`down_revision=c4d9f2a17e35`，取值域字面量 | `migrations/versions/…_wp35_export_items.py` |
| 4.3 | 服务层：取数（投影优先）→ 冻结清单 → 流式打包到临时文件 → `store_generated_asset(PURPOSE_EXPORT)` → 落记录 | `service.py` |
| 4.4 | API：`POST /projects/{pid}/exports`（幂等）、`GET /projects/{pid}/exports`、`GET /projects/{pid}/exports/{id}/download`（流式，`DELIVER`） | `api.py` + 装配（`main.py` 由主 agent 串行改） |
| 4.5 | 测试：真库，覆盖 R1~R9；含回滚分叉、全 FAILED、跨项目 404、同键异体 409、内存探针 | `tests/test_exports_wp35.py` |
| 4.6 | 契约与草案同步（§5）、证据脚本、CodeReview、简化、纯证据提交 | |

## 5. 须同步修改的三处既有文档（阶段 4.6 执行，现在不动）

| 文件 | 改什么 |
|---|---|
| `docs/m4/M4-DDL草案.md` | **已同步（2026-10-14）**：§11 迁移表 WP36 5→4 表、WP35 +1 表、合计 19；§1.3 加归属变更说明。**待阶段 4.6 补**：`export_group_id` 列、删除 `delivery_id` 列 |
| `contracts/draft/m4-openapi.yaml` | `ExportItemCreate` 增 `contentItemIds[]`（一组）；新增下载端点；删除 `versionNodeId` 入参（改为服务端冻结，客户端不得指定） |
| `docs/m3/WP35-五问答复.md` | 问 4 的 `formal` 更正为 `kind=DELIVERY AND state=READY` |

> 原契约 `ExportItemCreate` 让**客户端传 `versionNodeId`**，这与「服务端冻结」冲突 —— 客户端指定版本等于允许导出任意旧版或他人版本。必须改为服务端按投影取。

## 6. 老大裁定（2026-10-14，阶段 4 开工依据）

| # | 问题 | 裁定 |
|---|---|---|
| 1 | 选方案 | 老大首选 **V4**，但 V4 的「组表」「异步 Job」与第 2、3 条互斥 ⇒ 二次裁定 **V2 + 增强项** |
| 2 | A' 与 Q2 | **同意 A'**：`export_items` 加 `export_group_id UUID NOT NULL`，不建组表，M4 仍 19 张 |
| 3 | 打包时机 | **同步打包**；异步化与组表留到上线前（压测不够再升） |

**「V2 + 增强项」的范围**（取 V4 中与 A'/同步不冲突的部分）：摘要核验端点、`Idempotency-Key` 重放缓存。
**明确不做**（与裁定冲突）：组表、异步 Job、新增 Job 种类。人日约 4~5。
