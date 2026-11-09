# WP28 Live 动态图生成 / 封装 说明

> 状态：**阶段 4.5 已完成；4.6–4.8 待继续**。阶段 1 待决项 D1~D5、P1、P2 已闭合（见 §5 与 §9）。
> 方案选择：**V4 完整版**（老大 2026-10-14 拍板）。
> 前置依据：`docs/m0/OP-76-解法与责任分工.md`（2026-10-11 修订，WP28 解封）。
> 本包**不得**宣称 Live 已验证；全部产出标 `SYNTHETIC`（见 §0 红线）。

## 0. 本包的一句话定位

**在没有真机的前提下，把 Live 链路在系统内完整跑通（生成 → 封装 → 留存 → 组件可读），并把每一处未经真机验证的环节显式标为 `SYNTHETIC`。**

这不是"做出一个能上 Live 的功能"。上线门槛仍是 OP-76 真机验证，本包只负责让链路与标记可演示、可复核。

## 1. 阶段 1：需求追问（superpower-workflow 强制 8 问）

### 1.1 这个需求真正解决什么问题？

Live 图是双首发硬要求之一（研发计划 V2.1 §2 "明确化 2"）。M3 的 W11 原计划里 WP28 排在 WP27 之后、WP35 之前，WP35 的 ZIP 下载包（AC-17）依赖 Live 产物存在。**如果没有 WP28，WP35 的 Live 分支只能是空壳，M4 的 WP36 会被整条卡住。**

所以本包解决的是：**让"Live 组"成为系统里一种真实存在、可被生成、可被封装、可被 WP35 打包的媒体形态**。它不解决"发布到小红书后能否被识别为实况"——那是 OP-76 的事。

风险在于：如果本包做得太像"已验证"，演示时会误导决策者。所以"标记"是本包的首要约束，而不是附属文案。

### 1.2 核心用户是谁？

| 用户 | 关心什么 | 本包给他什么 |
|---|---|---|
| 运营 | 一篇内容配几组 Live，能不能重做某一组 | 可见的 Live 组、`SYNTHETIC` 标记、单组重做入口（WP29 消费） |
| 老大 / 产品 | demo 是否闭环、哪些是假的 | 每个 Live 产物都带 `verification` 字段，一眼可辨 |
| WP35（下游） | 稳定的封装结构可打包 | `media_components` 的四角色结构（STILL / PAIRED_MOTION / MANIFEST / PREVIEW） |
| 工程（自己） | 判据能抓住"冒充已验证" | 判据覆盖标记字样与占位 profile 状态 |

### 1.3 最小 MVP 应包含哪些功能？

V4 的范围（老大已选，D1 全保留）：

1. **Live 生成任务**：走 `KIND_MEDIA_GENERATE` + `media_form = LIVE`（不新增 `TASK_LIVE`，见 D3），网关能力为 `CAPABILITY_LIVE_MOTION`（已存在）。
2. **封装桩 `LivePackStub`**：把静帧 + 动态片段 + manifest 真实写入对象存储，返回 OP-02 规格 §5 的 `LivePackResult` 结构，全程 `SYNTHETIC`。
3. **`media_components` 表**：角色 `STILL` / `PAIRED_MOTION` / `MANIFEST` / `PREVIEW`，与规格 §3 一致。
4. **`live_profiles` 表**：只放占位 profile，`state = PLACEHOLDER`。
5. **合成素材生成器**：`tools/dev/m0/make_live_sample.py`。静帧用 Pillow 生成竖版 3:4；动态段用本机 ffmpeg 8.1.2 生成 3 秒 MOV；manifest 写 JSON。每个文件落 sha256。
6. **Live 与静态共用绑定**：Live 组复用 WP27 的 `media_versions` 段落绑定与参考素材绑定，不另起一套。
7. **计费预占**：按张预占，复用 WP25 `reserve_budget`（见 D5、P2）。
8. **前端标记**：`delivery.html` 里的「Live 按已验证格式输出 HEVC + HEIC 配对」改为「合成模式（真实设备验证待上线前完成）」，用区分色。
9. **判据**：`SYNTHETIC` 标记必达、`PLACEHOLDER` 不得当 `VERIFIED`、禁止"已验证"字样出现在产出与前端。

### 1.4 描述有哪些疏漏应当补充？理由是什么？

| # | 疏漏 | 理由 / 裁定 |
|---|---|---|
| G1 | OP-183 的关闭条件要改写 | 已获 D2 同意，见 §8 |
| G2 | `CAPABILITY_LIVE_MOTION` 网关侧是否真能走通 | **已核实成立**：常量已定义，业务代码零消费（§6） |
| G3 | `media_versions` 没有 LIVE 类型概念 | 已裁定 D3：`media_form` 列 |
| G4 | ffmpeg 不在 `pyproject.toml` 里 | 系统二进制（`/opt/homebrew/bin/ffmpeg`），进上线前清单；CI 无 ffmpeg 时判据显式 `SKIP` 并报告，禁止假绿 |
| G5 | WP24 的「Live 3 组」没有换算落点 | **已核实成立**：`calls_for` 只有 `STATIC_IMAGE` 分支（§6） |
| G6 | 合成动态段与"真实动态"的区别要写死 | manifest 写 `source: SYNTHETIC_GENERATOR`，避免误当产品素材 |
| G7 | 原始输出的落点 | **已定（Q2，2026-10-14）：不设 `ORIGINAL` 角色**。原图即 `media_versions.image_file_id`，STILL 组件指向同一文件；manifest 说明合成来源，不省略 |
| G8 | 文件体积上限未定 | 已裁定 D4：单组 20 MB 占位上限（§7.2） |

### 1.5 哪些功能现在不该做？

| 不做 | 理由 |
|---|---|
| 真机发布验证 / Apple Photos 识别判定 | 属 OP-76，上线前 G0 验收项 |
| `metadataValidation = PASS` 的"平台兼容"语义 | 规格 §5：PASS 只表示结构检查通过，不替代真机认证 |
| 真实供应商动态生成 | 没有合同与真实接口，只用 `StubAdapter` 脚本化结果 |
| 把 Live 产物直接塞进 WP35 ZIP | WP35 属下一包，本包只保证结构稳定 |
| 任何"发布成功"状态 | 桩只到"封装资产已产出"，发布是外部行为（OP-76 §4-2） |
| 多 profile 切换 UI | 只有一个占位 profile |

### 1.6 这个系统有哪些技术风险？

| # | 风险 | 等级 | 应对 |
|---|---|---|---|
| R1 | 冒充已验证：演示或文档出现"已验证"字样 | 🔴 高 | 判据扫描产出字段与前端文件；`verification` 只允许 `SYNTHETIC` / `PLACEHOLDER` |
| R2 | 占位 profile 被当 VERIFIED 使用 | 🔴 高 | DB CHECK + 代码守卫 + 反向验证（改成 VERIFIED 必须报红） |
| R3 | ffmpeg 缺失导致生成器静默失败 | 🟠 中 | 启动探测；缺失则判据显式 `SKIP` 并标红 |
| R4 | 合成动态段被误认为真实产品素材 | 🟠 中 | manifest `source` 字段；前端标签；G6 |
| R5 | Live 组段号坐标与 WP27 不一致 | 🟠 中 | 复用 WP27 切段实现（`media/schemas.py`），不另写第二份 |
| R6 | 计费按组还是按张 | 🟡 低中 | 已裁定 D5：按张；动态段漏计见 P2 与 §7.3 |
| R7 | 对象存储写失败留下残缺组 | 🟠 中 | 组内三件全落或整组 FAILED，不允许半组 READY |
| R8 | WP35 把 SYNTHETIC 产物当正式交付 | 🔴 高 | `verification` 列；WP35 交付资格判据必须读它（写进 WP35 登记） |
| R9 | **X2a 破坏"张数唯一算法"的一半**（新增） | 🟠 中 | 静态经 `images_for`，Live 经 `images_per_content("LIVE")`；须加判据：两条路径对同一输入的静态结果必须一致 |
| R10 | **动态段成本漏计**（新增，P2 已接受） | 🟠 中 | 账本 notes 标注；登记待定项；上线前回改 WP25 |

### 1.7 数据结构、接口、前端、后端怎么拆？

**实测现状（2026-10-14）**：

| 项 | 实测 | 对本包的影响 |
|---|---|---|
| `CAPABILITY_LIVE_MOTION` / `DATA_CLASS_LIVE_MOTION` | 已定义（`gateway/types.py:59,85`） | 直接复用 |
| `StubAdapter` | 已存在（`gateway/adapters.py:161`） | 复用做 Live 脚本化结果 |
| `FORBIDDEN_EGRESS_DATA_CLASSES` | 已含 Live 的兄弟类别 | 确认 `LIVE_MOTION` 不在禁止集中 |
| Pillow | `pyproject.toml:46` 已有 | 静帧生成零新增依赖 |
| ffmpeg / ffprobe | `/opt/homebrew/bin/` 可用，不在 pyproject | 进上线前清单（G4） |
| `media_*` 表 | 5 张已有 | 新增 `media_components`、`live_profiles` |
| `files.store_generated_asset` | 已存在（`files/service.py:1160`） | 组件落库入口 |
| `media/service.py` | 2121 行 | Live 分支接入现有执行链路 |

**数据结构**：

- `live_profiles`：`id`、`profile_key`、`version`、`state`（CHECK：只允许 `PLACEHOLDER`）、`notes`。
- `media_components`：`media_version_id`、`role`（CHECK：`STILL|PAIRED_MOTION|MANIFEST|PREVIEW`），`file_id`、`verification`（CHECK：`SYNTHETIC`）、`sha256`。
- `media_versions` 增加 `media_form`（`STATIC|LIVE`，默认 `STATIC`，存量回填 `STATIC`）。

**接口**：

- `LivePackStub`：输入 `LivePackRequest`，输出 `LivePackResult`（规格 §5）。`state` 恒为 `SUCCEEDED` 或 `FAILED`，不产出 `UNKNOWN`。
- 生成任务：`KIND_MEDIA_GENERATE` + `media_form=LIVE`，张数经 `images_per_content("LIVE")`（见 §7.5）。

**前端**：`delivery.html` 的 Live 文案改标记；媒体详情对 Live 组显示 `verification` 标签。

**后端模块拆分**（沿用 `media/`，不新开模块）：

| 文件 | 新增内容 |
|---|---|
| `media/types.py` | `MEDIA_FORM_LIVE`、`VERIFICATION_SYNTHETIC`、`LIVE_PROFILE_PLACEHOLDER`、组件角色常量；`images_per_content(form)` 参数 |
| `media/models.py` | `MediaComponent`、`LiveProfile` |
| `media/ddl.py` | 新表 DDL 与 CHECK |
| `media/live_pack.py`（新） | `LivePackStub` |
| `media/service.py` | Live 分支接入执行链路 |
| `tools/dev/m0/make_live_sample.py`（新） | 合成素材生成器 |

## 2. 阶段 2：方案设计

已完成，见 **§11**（需求本质、画像、MVP、待补充、砍掉、技术风险）。

## 3. 阶段 3：多版本方案

> 老大 2026-10-14 已选 **V4 完整版**。下表保留四套比较，是为了让"为什么不选 V1~V3"有据可查，不是重开选型。

### 3.1 实测前提（四套共用，2026-10-14 读代码所得）

| # | 事实 | 出处 | 对方案的约束 |
|---|---|---|---|
| F1 | `media_versions.image_file_id` **NOT NULL**，一版一图 | `media/models.py:930` | Live 的 STILL 必须与它指向**同一文件**，不得另存一份 |
| F2 | `MediaGenerator` Protocol **只声明 `dispatch`（出图）**，且注释明令"不往里塞别的能力" | `media/service.py:199-270` | 动态段出网不进该 Protocol，另立 `LiveMotionGenerator`（WP26 同款切分） |
| F3 | `_persist_generation` 的落库顺序 ①~⑦ 由 DEFERRABLE FK 环 + 不可变表共同决定 | `service.py:1692-1750` | Live 组件落库**挂在 ② 之后、⑦ 之前**，不改既有顺序 |
| F4 | `media_versions` 是不可变表，`IMMUTABLE_TABLES` 与守卫有逐表对账断言 | `models.py:394 / 1378` | 新增 `media_components` 若不可变，必须同步进元组，否则模块导入即断言失败 |
| F5 | 迁移当前头 = `e5a8c3b1d704`（WP35 `export_items`），**未提交，属并行会话** | `migrations/versions/` | 本包迁移的 `down_revision` 须在写入前现查 `alembic heads`，不可凭记忆 |
| F6 | `exports` 模块**一律拒绝 `LIVE`**（`EXPORT_FORMATS = ("STATIC",)`，R11） | `exports/types.py:37-38` | 本包**不改** `exports`；见 §3.5 待决项 Q1 |
| F7 | 并行会话在 `files/service.py` 新增了 `store_generated_asset_from_path`，**未提交** | `git diff` | 本包只用**已入库**的 `store_generated_asset`（字节入参）；20 MB 上限下字节内存可接受。依赖未入库代码 = 干净检出必红（判据纪律第 18 条） |

### 3.2 四套方案

| | V1 最快上线 | V2 稳定可扩展 | V3 个人精简 | **V4 完整版（已选）** |
|---|---|---|---|---|
| 数据 | `media_versions` 加一列 JSONB 装组件 | `media_form` 列 + `media_components` 表 | 同 V2 | V2 + `live_profiles` 表 + 组件表进不可变守卫 |
| 动态段 | 内联在 `service.py` 里调 ffmpeg | `LiveMotionStub` 独立类 + ffmpeg | 仓库内置固定样本文件，不依赖 ffmpeg | V2 + 启动探测 ffmpeg，缺失 fail-closed |
| 封装 | 内联 | `live_pack.py::LivePackStub` | 同 V2 | V2 + 20 MB 上限走 `runtime_settings` |
| 标记 | 靠约定写 `SYNTHETIC` | CHECK 约束只允许 `SYNTHETIC` | 同 V2 | V2 + 禁词扫描 + 双路径一致 + 清单对账 |
| 优点 | 一天内能演示 | 结构清晰，WP35 可直接消费 | 无外部二进制依赖 | R1/R6（冒充已验证）由判据兜底，不靠自觉 |
| 缺点 | JSONB 里的组件无 FK、无 CHECK，**SYNTHETIC 全靠自觉**；与 D3/F4 冲突 | 缺判据，R1 无防线 | 动态段不是真生成，manifest 会"谎报"来源；不满足 MVP 第 2 项 | 工作量最大 |
| 风险 | R1 高、R6 高 | R1 中 | R1 中、MVP 缺项 | 迁移头冲突（F5）、范围膨胀 |
| 适合 | 只给老大看一眼 | 无判据体系的项目 | 无 ffmpeg 的环境 | 本项目：**判据纪律第 3 条"声称有防线比没防线危险"** |

不选 V1：违反已裁定的 D3（`media_form` 列）与 MVP 第 4 项（`media_components` 表）。
不选 V2：R1、R6 是本包最高风险，只有 CHECK 约束拦不住前端和文档里出现"已验证"。
不选 V3：动态段是假样本，不满足 MVP 第 2 项（动态段生成桩）。

### 3.3 V4 的设计决定（阶段 4 按此实施）

| # | 决定 | 理由 |
|---|---|---|
| S1 | `media_components.role` 取 `STILL / PAIRED_MOTION / MANIFEST / PREVIEW`，**去掉 `ORIGINAL`**（阶段 1 §1.7 草案里有） | F1：`image_file_id` 已承担"原图"，再来一个 `ORIGINAL` = 同一事实两个落点。**这是对 §1.7 草案的收窄，老大 2026-10-14 已确认**（Q2） |
| S2 | STILL 行的 `file_id` 必须等于所属版本的 `image_file_id` | F1；由 DDL 触发器兜（与既有"一致性触发器"同形），不靠 service 自觉 |
| S3 | `media_components` 进 `IMMUTABLE_TABLES` | F4：它挂在不可变版本上，可变的组件会让"这一版里有什么"随时间漂移 |
| S4 | 动态段出网走新 Protocol `LiveMotionGenerator.dispatch_motion`；封装 `LivePackStub` 是**纯本地**，不出网 | F2 |
| S5 | Live 分支在 `_persist_generation` 的 ② 之后落组件；任一组件失败 ⇒ 整组 `FAILED`，**不写半组** | D4；F3 |
| S6 | 20 MB 上限缺省为 `LIVE_GROUP_MAX_BYTES_DEFAULT`（20 MiB），允许由调用方注入覆盖；当前不读 `runtime_settings`（该 key 不在其白名单） | D4；偏离已确认，demo 阶段由装配参数控制，上线前按 OP-76 真机结论替换。⚠️ **注入点当前无生产消费者**（2026-10-16 核实）：`live_group_max_bytes` 与 `motion_generator` 的唯一注入入口 `media/handlers.py:454 build_task_registry` 在 `src/` 下**零调用者**（WP13 worker 入口缺失，`main.py:471-479` 自述「`run_job`/`claim_jobs`/`build_default_registry` 只在 `tests/` 被调用」）。⇒ demo 阶段 20 MiB 是**事实硬编码**，且 Live 在生产侧因无 `motion_generator` 而 fail-closed。此为 WP13 的既有状态（非 4.5 引入），但**不得**写成「已可在运行期配置」 |
| S7 | `live_profiles.state` CHECK 仅 `PLACEHOLDER` | 红线：占位不得当 `VERIFIED` |
| S8 | `media_components.verification` CHECK 仅 `SYNTHETIC`；manifest 内 `source = SYNTHETIC_GENERATOR` | R1 |
| S9 | 迁移 `down_revision` 在写入前现查，且写完立即复查单 head | F5 |
| S10 | `images_per_content(form)`：`form ∈ {STATIC, LIVE}`，其余 fail-closed；静态默认值保持 30 处调用语义不变 | X2a（P1 裁定） |

### 3.4 阶段 4 实施步骤（后端 → 判据，逐步可验）

| 步 | 内容 | 验证 |
|---|---|---|
| 4.1 | `types.py`：常量 + `images_per_content(form)` + `calls_for` Live 分支 | 回归：既有静态用例零改动全绿；新增双路径一致用例（R9） |
| 4.2 | `models.py` / `ddl.py`：`media_form` 列、`MediaComponent`、`LiveProfile`、触发器 S2、`IMMUTABLE_TABLES` 同步 | 模块可导入；守卫对账断言通过；`test_schema_parity`。**已完成（2026-10-14）**：`tests/test_media_wp28_db.py` 22 条 DB 行为用例（media_form 默认/非法、组件 CHECK×5、唯一、S2 触发器×3、live_profiles CHECK/唯一/列宽、两表不可变），私有库 wp27+wp28+db 共 97 passed；3 个变异（去 LIVE 判断 / 去 STILL 相等判断 / 放宽 verification）全红。`test_schema_parity` 待 4.3 迁移落地后才有意义 |
| 4.3 | 迁移（先 `alembic heads`）+ 私有库真跑升降级 | 单 head；存量行 `media_form` 回填 `STATIC` **已完成（2026-10-14）**：新迁移 `a9d3f6c2b815`（down=`e5a8c3b1d704`）：`media_form` 列 + CHECK、`media_components`、`live_profiles`、3 索引、3 触发器；CHECK 文本为化石字面量且**元素顺序与模型一致**。**联动修复**：`df6bb2f41f84` 实时调用的 `all_trigger_ddl()` 已含新表触发器，全新库迁移在该修订处必炸（实测 `relation "media_components" does not exist`）⇒ 该迁移按表名冻结为当时 8 项。实测：全新库 base→head、head↔WP35 head 升/降/再升均成功；存量行回填 `STATIC`；降级后新增对象全清、共用函数 `rednote_reject_mutation` 保留；`check_migration_roundtrip` 13/13、`check_migration_model_parity` 69 表/761 列/608 约束/262 索引一致、`value_domain_no_live_import` 338 处合规；`test_schema_parity`+wp27+wp28+db 共 100 passed；`collect_wp27_evidence` 117/117（取证范围冻结为 WP27 当时 5 表，CHECK 40→41、列 15→16）。 |
| 4.4 | `live_pack.py`：`LivePackStub` + `LiveMotionStub` + ffmpeg 探测 | 超限、缺 ffmpeg、半组三条失败路径各一例 **已完成（2026-10-15）**：新增 `media/live_pack.py`（`LivePackRequest/LivePackResult`、`LiveMotionGenerator` Protocol、`LiveMotionStub`、`LivePackStub`、`ComponentSink` 字节落点、`probe_ffmpeg/require_ffmpeg`）与 `tools/dev/m0/make_live_sample.py`。分层：封装桩**纯本地、不碰库、不 import `files`/`versions`**，字节出口抽成 `ComponentSink`（4.5 用 `store_generated_asset` 实现）。三条失败路径：超限 `GROUP_TOO_LARGE`（STILL+MOTION+MANIFEST 合计，**写任何字节前**判，零落盘；PREVIEW 不计）；缺 ffmpeg `FFMPEG_MISSING`（`which` 可注入，**不改 PATH、不 mock subprocess**，不用占位字节冒充）；半组 `COMPONENT_STORE_FAILED`（`components` 恒为空，已落文件 id 放 `orphan_file_ids` 供 4.5 回滚，4 个角色各失败一次）。`state` 只产出 `SUCCEEDED/FAILED`；档案状态非 `PLACEHOLDER` 一律拒；manifest `source=SYNTHETIC_GENERATOR`，无「已验证」字样。`tests/test_media_wp28_live_pack.py` 19 passed（本机 ffmpeg 8.1.2）；无 ffmpeg 的 PATH 下 11 passed + 6 **显式 SKIP**（原因写明，缺失路径用例仍跑）。变异 6/6 变红（超限不判、半组带 orphan 丢失、缺 ffmpeg 回落、manifest 改「已验证」、放开非占位档案、失败时带出 preview）。回归 `schema_parity+wp27+wp28+db+live_pack` 117 passed；`check_all_exports`/`undefined_globals`/`dependency_declarations`/`loader_registration` 全 PASS。真 ffmpeg 合成：768×1024 h264 3.0s。⚠️ `LiveMotionStub` 目前只合成「缓慢推近」，**不是**真实视频模型；`UNKNOWN` 状态桩不产出（规格允许，桩无此情形）。 |
| 4.5 | `service.py`：Live 分支接入；账本 notes 标注漏计（OP-206）；修复 pending JSONB 投影清空时 `None` 写成 JSON `null` 的既有缺陷；生成上下文调用统一为 `content_item_id` 仓储契约 | 端到端真库：Live 组生成 → 4 组件落库 → 详情读回；封装超限整组回滚；静态零回归；WP27/WP28 相关 **133 条通过**（口径 = **5 个文件**：`test_media_wp27` + `test_media_wp28` + `test_media_wp28_db` + `test_media_wp28_live_pack` + `test_media_wp28_service`；**只跑前两个文件是 43 条**，勿据 43 判回归） |
| 4.6 | `api.py` / `schemas.py`：详情响应带 `mediaForm` 与组件 `verification` | 契约用例 |
| 4.7 | 判据与证据：`SYNTHETIC` 必达、禁词扫描、`PLACEHOLDER≠VERIFIED`、干净检出可跑；`collect_wp28_evidence.py` | 同步四处（`check_evidence_scripts` / `check_evidence_scope` / ci.yml 门 3 / `MIN_SCRIPTS`） |
| 4.8 | 文档收口 + `WP28-CodeReview.md` | 阶段 5 起走 code-review |

### 3.5 已裁定的两件事（老大 2026-10-14）

- **Q1 · 选 (a)**：「可读」= 本包提供**组件读取接口**，`exports` 本包不动，由 WP35 日后自行消费 Live 组件。理由：`exports` 当前一律拒绝 `LIVE`（F6，WP35 的 R11），且在并行会话修改中，本包不介入。OP-183 与 §0 里的「交付包可读」已改为「组件可读」。
- **Q2 · 去掉 `ORIGINAL` 角色**：见 S1。原图即 `image_file_id`，STILL 组件指向同一文件。

注意：WP35 说明文档第 16 行写"Live 侧走桩 + SYNTHETIC"，而同文档 R11 写"LIVE 一律拒绝"，两处互相矛盾，属 WP35 自己的文档，我不改，只在此留痕。

## 4. 阶段 4~7 与收口

待阶段 3 结束后按 superpower-workflow 推进。

## 5. 老大裁定记录（2026-10-14）

| # | 问题 | 裁定 | 落地 |
|---|---|---|---|
| D1 | MVP 范围 | 9 项全保留 | §1.3 |
| D2 | OP-183 关闭条件 | 同意按 OP-76 修订版改写 | §8 |
| D3 | LIVE 承载方式 | `media_versions.media_form` 列，不新增 `TASK_LIVE` | §7.1 |
| D4 | 单组体积占位上限 | 20 MB（占位，非平台约束） | §7.2 |
| D5 | 计费单位 | 按张（`PRICING_UNIT_IMAGE`） | §7.3 |
| D6（Q1） | 「可读」的范围 | 组件读取接口；`exports` 不动，WP35 自行消费 | §3.5 |
| D7（Q2） | `ORIGINAL` 角色 | 去掉；原图即 `image_file_id` | §3.3 S1 |

## 6. 核实结论（阶段 1 前提）

| 项 | 核实结果 | 证据 |
|---|---|---|
| G2 网关 `LIVE_MOTION` | 成立：常量已定义，业务代码零消费 | `gateway/types.py:59,85`；`media/` 内无 `LIVE_MOTION` 引用 |
| G5 WP24「Live 3 组」换算 | 成立：`calls_for` 只有 `STATIC_IMAGE` 分支 | `media/types.py:96-97`；`calls_for` 无 Live 分支 |

## 7. 按裁定修订的设计

### 7.1 `media_form` 列（D3）

- `media_versions.media_form`，CHECK `STATIC | LIVE`，默认 `STATIC`，存量回填 `STATIC`。
- Live 与静态共用段落绑定与参考素材绑定，故不拆成两种 task_kind。
- 代价：`media_form` 进入不可变列集合，须在 `IMMUTABLE_TABLES` 守卫登记（`models.py:1378` 断言会变）。

### 7.2 20 MB 占位上限（D4）

- 组内三件（STILL / PAIRED_MOTION / MANIFEST）合计 ≤ 20 MB，超限直接 `FAILED`，不写半组。
- 缺省值为 `LIVE_GROUP_MAX_BYTES_DEFAULT = 20 * 1024 * 1024`；service/handler 支持显式注入覆盖。
- **实现偏离**：`live_group_max_bytes` 当前不在 `runtime_settings` 白名单，因此不能从运行时设置表读取。demo 阶段保留常量缺省 + 装配参数注入，不伪称为配置化；上线前由 OP-76 真机结论替换并重新裁定配置来源。

### 7.3 按张计费与漏计（D5 + P2）

- 预占单位 `PRICING_UNIT_IMAGE`，每篇内容物化 **1 个 Live 身份行**；该行对应 **1 组**，预占 **2 张**（STILL + PREVIEW）。服务层使用 `content_count=1`，由 `calls_for(..., form=LIVE)` 折算调用/张数。
- **PAIRED_MOTION 成本漏计（P2 已接受）**：不进账本。预占与结算共用 `OP206_NOTE`，明确标注 Live 动态段成本未计入及归属 WP25。
- 属 WP25 范围（`PRICING_UNIT` 缺视频单位），上线前回改。

### 7.4 `calls_for` 的 Live 组数（P1 方案 A 的部分）

- 方案 A：Live 组数在 `calls_for` 受控域内加 Live 分支。
- 但张数不经 `images_for`（见 §7.5），故 `calls_for(LIVE, n)` 的张数来自 `images_per_content("LIVE") × n`。

### 7.5 X2a：张数口径（P1 最终裁定）

- `images_for(content_count)` 签名不变，**内部固定取 `STATIC`**，不接受 Live。
- `images_per_content(form=STATIC)` 增加 `form` 参数，默认 `STATIC`：现有 **30 处**调用语义不变，无需逐处改。
- Live 只在新增的显式调用点传 `"LIVE"`，返回 2。
- `form` 只能取受控元组 `{STATIC, LIVE}`，其余值 fail-closed 报错。
- **已接受代价**：方案 A 的"唯一算法"只成立一半（静态经 `images_for`，Live 经 `images_per_content("LIVE")` 直乘）。判据须钉住两条路径对静态输入结果一致（R9）。

## 8. 登记册与前置文档待同步项

| 位置 | 现状 | 需改为 |
|---|---|---|
| `待定项与问题登记册.md` OP-183（v1.21，L189~197） | "WP28 挂起，等 OP-76" | 关闭条件改为：系统侧链路完成 + 全部产出 `SYNTHETIC` + 无「已验证」字样；OP-76 转为上线前 G0 验收项 |
| `研发执行计划_V2.1` §9 / W11 行 | `⛔ BLOCKED_ON_EXTERNAL` | 解封，标注 OP-76 修订版 |
| 待定项登记册（新增） | 无 | 登记"Live 动态段成本漏计"（P2），归属 WP25 |

以上在 WP28 阶段 2 结束前同步。

## 9. 待决项裁定归档（2026-10-14）

| # | 待决项 | 最终裁定 | 说明 |
|---|---|---|---|
| P1 | Live 组数换算 | **X2a** | 见 §7.4、§7.5。注：这是对裁定 X2 的收窄，因 X2 原表述"签名不变"不成立 |
| P2 | Live 动态段成本 | **接受漏计** | 见 §7.3 |

## 10. 阶段 2 入口

阶段 1 全部待决项已闭合（D1~D5、P1、P2）。进入阶段 2：输出需求本质、用户画像、MVP、待补充、砍掉、技术风险。

## 11. 阶段 2：方案设计（2026-10-14）

### 11.1 需求本质

在无真机条件下，把 Live 形态作为系统内**真实存在的媒体形态**跑通到封装留存，并让每一处未验证环节可被机械识别（`SYNTHETIC`）。本包不证明"能上 Live"，只证明"系统能生产、封装、留存、交付一组 Live，且诚实标注其来源"。

### 11.2 用户画像

| 角色 | 诉求 | 本包给的东西 |
|---|---|---|
| 运营 | 一篇内容配几组 Live；能重做某一组 | Live 组可见、`SYNTHETIC` 标记、单组重做入口（WP29 消费） |
| 老大 / 评审 | 演示时不得误以为已验证 | 全链路可见的 `SYNTHETIC` / `PLACEHOLDER` 标记；判据扫描禁用词 |
| WP35（下游） | 稳定的封装结构可打包 | `media_components` 四角色结构 |

### 11.3 MVP（D1 已确认 9 项全保留）

1. Live 组数换算（X2a：Live 每内容 2 张 = STILL + PREVIEW，静态路径不变）
2. 动态段生成桩 `LiveMotionStub`（复用 `StubAdapter`，产出合成动态片段，ffmpeg 合成素材）
3. 封装桩 `LivePackStub`（静帧 + 动态段 + manifest 写入对象存储，返回 `LivePackResult` 结构）
4. `media_components` 表（角色 `STILL / PAIRED_MOTION / MANIFEST / PREVIEW`）
5. `media_versions.media_form` 列（D3：`STATIC | LIVE`）
6. 单组体积占位上限 20 MB（D4，超限 `FAILED`，不写半组）
7. 计费按张（D5），`PAIRED_MOTION` 成本漏计（P2 已接受，账本 notes 标注）
8. 判据：`SYNTHETIC` 必达、`PLACEHOLDER` 不得当 `VERIFIED`、禁止"已验证"字样
9. 登记：OP-183 改写（已完成）、OP-76 转上线前 G0

### 11.4 待补充项（来自 §1 的 G 系列）

| # | 待补充 | 处理 |
|---|---|---|
| G2 | 合成动态段的时长与分辨率口径 | 固定为桩参数，写入 manifest |
| G5 | 单组体积无真实参照 | 20 MB 占位，标 `PLACEHOLDER` |
| G6 | 合成段与"真实动态"的区别 | manifest 写 `source: SYNTHETIC_GENERATOR` |
| G8 | 单组重做的幂等口径 | 沿用 WP29 既有重做语义，本包不新建 |

### 11.5 砍掉清单（本期不做）

| 砍掉项 | 理由 |
|---|---|
| 真实动态生成 / 真机验证 | 属 OP-76，上线前 G0 |
| Apple Photos 识别判定 | 同上，需真机 |
| 任何"发布成功"状态 | 桩只到"封装资产已产出"，发布是外部行为（OP-76 §4-2） |
| 多段动态 / 多风格 | 超出 demo 范围 |
| 动态段成本入账 | P2 已接受漏计 |

### 11.6 技术风险

| # | 风险 | 等级 | 缓解 |
|---|---|---|---|
| R1 | 冒充已验证（文档或前端出现"已验证"） | 🔴 高 | 判据扫描产出字段与前端；`verification` 只允许 `SYNTHETIC` / `PLACEHOLDER` |
| R2 | ffmpeg 为系统二进制，非 Python 依赖 | 🟡 中 | 启动时探测可用性，缺失则 `FAILED` 并给出明确错误，不静默降级 |
| R3 | 共享库 `rednote_test` 被并行会话占用，全量 pytest 不可跑 | 🟡 中 | 先 `pg_stat_activity` 核查，有他人连接则 fail-closed 等待 |
| R4 | `images_per_content` 签名变更波及 ~30 个调用点 | 🟡 中 | X2a 给 `form` 默认值 `STATIC`，静态调用点语义不变，靠回归测试兜底 |
| R5 | 合成动态段体积超 20 MB 占位上限 | 🟢 低 | 超限即 `FAILED`，不写半组 |
| R6 | WP35 把 `SYNTHETIC` 产物当正式交付 | 🔴 高 | `verification` 列；WP35 交付资格判据必须读它（写入 WP35 登记） |

### 11.7 阶段 2 结论

- MVP 9 项与砍掉清单无冲突，待补充项 G2 / G5 / G6 / G8 均有明确处置。
- 最高风险为 R1 与 R6（都属"冒充已验证"族），须由判据而非人工自觉兜底。
- 下一步：阶段 3，至少输出 4 套方案，已选定 V4（完整版）。
