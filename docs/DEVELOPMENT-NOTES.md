# rednote-production

Rednote 内容生产 AI Native 系统 —— 企业内网应用。

| 项 | 值 |
|---|---|
| 当前阶段 | **M1 已交付；M2 全部六包已交付（WP18~WP23）；M3 进行中（首包 WP24 已交付：场景形式草稿 + 运行期数量配置）；M0 出口待企业输入到位** |
| 执行依据 | `outputs/Rednote研发执行计划_V2.0_2026-09-19.md` |
| 设计基线 | 需求说明 V1.0 + 研发计划 01~04 V1.0 |
| 架构模式 | 模块化单体（API 双实例 + Worker 分组 + 模型网关），模式 A 受控出网（ADR-10） |
| 进度看板 | `docs/待定项与问题登记册.md`（**接手本项目先读这一份**） |

## 必读入口

| 文档 | 用途 |
|---|---|
| `docs/待定项与问题登记册.md` | **单一事实来源**：全部 205 条待定项 + 225 条开发过程问题记录 + 7 条铁律 |
| `docs/m0/M0执行手册.md` | M0 的 5 个问题、逐日安排、G0 判定规则、红线 |
| `docs/m0/G0-检查点清单.md` | G0 的 24 项判据 |
| `docs/adr/ADR-10~17` | 增量架构决策（部署模式、基础设施、AI 验收、SSE、向量、采购等） |
| `docs/m1/WP08-身份与会话说明.md` | 会话安全模型、OIDC、CSRF 双防线 |
| `docs/m1/WP09-项目权限说明.md` | 权限矩阵、部分唯一索引、404/403 语义 |
| `docs/m1/WP10-项目与字典说明.md` | 编号策略、两层并发保护、键集分页下推、游标指纹 |
| `docs/m1/WP11-版本注册与引用说明.md` | 双层不可变保护、依赖图环检测（访问集合去重）、草稿 ETag → 不可变 ETag |
| `docs/m1/WP12-分片上传与配额说明.md` | 配额原子预占（AC-06）、分片断点续传、UNSUPPORTED 标记（AC-07）、存储适配器、`CompletionOutcome` 的取舍 |
| `docs/m1/WP13-任务与Outbox说明.md` | Outbox 派发（先发布后标记）、Worker 租约与 `fence_token`、UNKNOWN 恢复、通用幂等；**执行路径不依赖 MQ** 的理由 |
| `docs/m1/WP14-SSE与断线续传说明.md` | ADR-14 三层机制（落库为真相 + LISTEN/NOTIFY + 补漏）、**逐事件鉴权**、Last-Event-ID 补漏与 410 超限信号；双实例 fan-out 双向验证 |
| `docs/m1/WP15-审计与待办说明.md` | 审计**不存正文**（哈希复用版本算法）、双层不可变保护（**两层都要落**）、待办部分唯一索引去重、完成待办"不自动通过业务对象"；**关闭 OP-85** |
| `docs/m1/WP16-模型网关说明.md` | 出网策略判定（纯函数，**否决即返回、无降级分支**）、`request_key` **跨 attempt 稳定**（否则租约过期后重复计费）、`UNKNOWN` 只能经核对落终态、出网留痕**独立表 + 不可篡改**；**不新增 HTTP 端点** |
| `docs/m1/WP17-文档解析与OCR说明.md` | 状态**由计数推导**（解析器无法自报成功）+ 三条 CHECK 拦「虚报成功」、OCR 引擎不可用**显式失败**（绝不降级为空文本）、`segment_no` 是**显式落库的顺序事实**、`parsed_units` **数 unit 不是数片段**；解析状态挂既有 `GET /files/{id}` |
| `docs/m2/WP18-Brief结构化解构说明.md` | 三条验收项**全部沉到数据库层**（35 CHECK + 7 触发器）、**假溯源**在服务层判不了必须落库、`PARTIAL` 三层落地（残缺不得洗白）、**OP-88 双向判据闭合**（含对照组）；§5.1 记录了"反向判据收窄是必需的"这一实测结论 |
| `docs/m2/WP20-知识CRUD与版本说明.md` | 六类知识的**字段规格两层**（schema 给可读错误 + CHECK 管绕过 service 的写入）、**状态判据单一来源**（`_project_asset` 同时供写入侧与读取侧）、**`chunks` 刻意不是不可变表**（可重建的物化结果）、**OP-88 的 `KNOWLEDGE` 那一半闭合**（含反向判据的收窄与对照组）；§3 给出 4 套方案与取舍 |
| `docs/m2/WP19-研究关键词与提示词说明.md` | **零表 / 零迁移 / 零写端点**（"能算出来的东西不要存"的划界）、**缺口判据必须是 `resolution_status` 而不是 `has_evidence`**（后者被数据库 CHECK 保证恒真 → `blockingCount` 恒为 0 且不报错）、**三条判据必须分开**（`claim_needs_gap` / `gap_state` / `claim_needs_search`）、`meta.research` 必须自定义 envelope（否则被 `response_model` 静默剥掉）；§9 沉淀五条铁律 |
| `docs/m2/WP23-Prompt版本与反馈提议说明.md` | **"校验通过 ≠ 权限没被扩大"**（编辑路径**结构性**不收 `allowedTools`，防线建在入口而非"收进来再校验"）、**`extra="forbid"` 是请求体一致性铁律**（缺它 → 越权字段被静默丢弃，实测 201 而非 422）、**内存分页必须拿到全量数据**（给它截断子集 → 报 `hasMore=False` → 静默丢尾部）、**`text[]` 能落 CHECK 而 `jsonb` 不能**（列类型是设计约束）、**"重试时重读"必须用可控替身做确定性判据**（顺序调用构造的判据**恒真**）；§9 沉淀八条铁律 |
| `docs/m2/M2并行开发接缝冻结.md` | WP18 ∥ WP19 ∥ WP20 并行的 **8 处共享文件全部归主 agent**、分支只写三处、测试库物理隔离（`REDNOTE_TEST_DATABASE_URL` 覆盖）；`entity_type` 受控取值按包划分 |

> **开工前必过一遍 `docs/待定项与问题登记册.md` §4.4 的 7 条铁律。** 踩过的坑不重复踩第二遍。

## 目录约定

```text
rednote-production/
  docs/               # 全量设计文档
    m0/               # M0 工作包协议、执行手册、G0 判定
    m1/               # M1 工作包说明（骨架、身份、权限、项目与字典、版本注册、解析）
    adr/              # 架构决策记录（ADR-01~17）
    issues/           # 开发过程问题记录（JSONL 数据源）
    待定项与问题登记册.md  # 单一事实来源
  deploy/dev/         # 开发/试点环境编排（docker compose）
  datasets/eval/      # AI 质量评测集（正常 + 对抗）
  tools/
    m0/               # M0 可执行工具（环境冒烟、评测打分、数据校验）
    m1/               # M1/M2 工作包证据采集（可复跑，产出 evidence/m1/*）
                      #   ⚠️ M2 沿用本目录，不另建 tools/m2 —— 证据与工具与 M1 同源，
                      #   且迁移脚本的路径引用、自检器的扫描范围都基于 m1/
                      #   （接缝文档 §7 写的 `tools/m2/collect_wp<NN>_evidence.py` 是笔误，已按实况执行）
                      #   另有四个收口专用工具（见下方「工作包证据采集」）：
                      #   refresh_register_evidence.py / check_evidence_scripts.py / audit_consistency.py
    issues/           # 问题记录工具（追加/校验/渲染）
    contracts/        # OpenAPI 导出与契约门禁
  evidence/           # 证据归档（G0 判定与工作包验收凭据）
    m0/  m1/
  services/backend/   # 后端（FastAPI，模块化单体）
  contracts/          # OpenAPI 契约产物
  migrations/         # Alembic 迁移
  apps/               # 前端（M1 后续工作包建立）
                      #   ⚠️ 当前前端 Demo 不在本仓：outputs/rednote-demo/（33 个静态 HTML 页，
                      #   纯原型，未接后端；见 outputs/ 而非本目录）
```

生产目录结构以研发计划 01 §5 为准，本文件只列出已落地的部分。

## 快速开始

### 后端（本机已验证路径）

> ⚠️ **零号前置步骤：库必须先建好并启用 `pgvector`。** 本仓从 WP21 起，
> 迁移里断言「`vector` 扩展已启用」——在**没有该扩展的新库**上直接跑
> `alembic upgrade head` / `pytest` 会当场失败（实测报
> `RuntimeError: pgvector 扩展可用但未启用`，且看起来像代码 bug，实际是环境没到位）。
> 一次性执行下面两条即可，之后重跑不会再需要：

```bash
# 1) 建测试库（库名随你，这里用 rednote_test）
/opt/homebrew/opt/postgresql@17/bin/createdb rednote_test

# 2) 启用 pgvector 扩展（**必须在跑迁移/测试之前**）
/opt/homebrew/opt/postgresql@17/bin/psql -d rednote_test -c 'CREATE EXTENSION IF NOT EXISTS vector'

# 3) 跑迁移把表建出来
cd /Users/<user>/内容运营AI Native/rednote-production
/Users/<user>/.workbuddy/binaries/python/envs/default/bin/python -m alembic \
  -c migrations/alembic.ini upgrade head
```

```bash
cd services/backend
/Users/<user>/.workbuddy/binaries/python/envs/default/bin/python -m pytest
```

> **不要再加 `-q`**：`pyproject.toml` 的 `addopts` 里已经有一个，CLI 再传一个会
> 叠加成 `-qq`，连最后的汇总行一起压掉（只剩进度条，而退出码仍为 0）—— 见 ISS-24。

> **并行会话各自隔离库**（铁律 17）：多个会话同时跑真 pytest / 真库判据时，
> 用 `REDNOTE_TEST_DATABASE_URL` 指向**各自的**库，例如
> `REDNOTE_TEST_DATABASE_URL='postgresql+psycopg://rednote@localhost:5432/rednote_wp<NN>'`。
> 共用一个库会双向污染（本仓实测踩过两次）。跑前另需确认该库上没有其他连接。

### 迁移

```bash
# 注意：用 python -m alembic，不依赖 PATH（见登记册 ISS-16）
cd /Users/<user>/内容运营AI Native/rednote-production
/Users/<user>/.workbuddy/binaries/python/envs/default/bin/python -m alembic \
  -c migrations/alembic.ini upgrade head
```

> **铁律 G2**：每次 `autogenerate` 后必须人工检查并删除 `op.drop_table` / `op.drop_index` 语句，
> 并重写 `downgrade`（M0 冒烟残留表会周期性被提议删除，已复现两次）。

### 契约门禁

```bash
/Users/<user>/.workbuddy/binaries/python/envs/default/bin/python tools/contracts/export_openapi.py --check
```

### 待定项与问题记录

```bash
PY=/Users/<user>/.workbuddy/binaries/python/envs/default/bin/python

$PY tools/issues/log_issue.py --check      # 校验问题记录合法性（0=通过）
$PY tools/issues/log_issue.py --render     # 刷新登记册渲染区
$PY tools/issues/log_issue.py --stats      # 统计
$PY tools/issues/log_issue.py --selftest   # 工具自测（32 PASS）
$PY tools/issues/log_issue.py --new \
    --scope WP10 --title "现象" --symptom "..." --root-cause "..." \
    --resolution "..." --lesson "..." --severity MAJOR --status CLOSED
```

### 工作包证据采集（可复跑）

```bash
# 一条命令刷新某工作包的全部证据（pytest / 契约 / 结构核对）
$PY tools/m1/collect_wp10_evidence.py
# → evidence/m1/backend-pytest.log、contract-export.log、wp10-projects.log

$PY tools/m1/collect_wp11_evidence.py
# → evidence/m1/wp11-versions.log（结构核对 + 不可变性「活性验证」）

$PY tools/m1/collect_wp12_evidence.py
# → evidence/m1/wp12-versions.log（7 表 / 4 个部分索引 WHERE / 24 CHECK + 配额不变量「活性验证」）

$PY tools/m1/collect_wp13_evidence.py
# → evidence/m1/wp13-jobs.log（3 表 / 2 个部分索引 WHERE / 7 CHECK + 幂等键唯一约束「活性验证」）

$PY tools/m1/collect_wp14_evidence.py
# → evidence/m1/wp14-events.log（domain_events / Identity 序列 / 契约端点 410 + sequence「活性验证」）

$PY tools/m1/collect_wp15_evidence.py
# → evidence/m1/wp15-audit.log（2 表列数 / 触发器 / 2 个部分索引 + 不可篡改与待办去重「两处活性验证」）

$PY tools/m1/collect_wp16_evidence.py
# → evidence/m1/wp16-gateway.log（5 表 / 唯一约束 / 部分索引 / 触发器 + 四处活性验证）

$PY tools/m1/collect_wp17_evidence.py
# → evidence/m1/wp17-parsing.log（2 表 / 三条「不虚报」CHECK / 触发器 + 五处活性验证）

$PY tools/m1/collect_wp18_evidence.py
# → evidence/m1/wp18-brief.log（5 表 / 35 CHECK / 7 触发器 / 4 函数 + 十处活性验证 + 契约）

$PY tools/m1/collect_wp20_evidence.py
# → evidence/m1/wp20-knowledge.log（5 表 / 53 列 / 30 CHECK / 15 FK / 6 触发器 + 五处活性验证 + 契约）

$PY tools/m1/collect_wp19_evidence.py
# → evidence/m1/wp19-research.log（零表/零迁移 + 8 处活性验证，含「读路径零写入」与零残留）

$PY tools/m1/collect_wp21_evidence.py
# → evidence/m1/wp21-embedding.log（2 表 / 8 CHECK / 1 触发器 / HNSW 部分索引 + 六组活性验证）

$PY tools/m1/collect_wp23_evidence.py
# → evidence/m1/wp23-prompt.log（3 表 / 13 CHECK / 6 触发器 + 五处活性验证）

$PY tools/m1/refresh_register_evidence.py
# → evidence/m1/op-register.log、issue-log-stats.log（计数全部现场派生；含看板逐类交叉核对、
#   §3.1 责任方「声明 vs 编号列」逐行核对、§3.2 目标时机「自由文本 → 固定桶」分桶核对）

$PY tools/m1/check_evidence_scripts.py
# → 证据脚本机械自检（内联段可独立 compile / 有 EXPECTED_CHECKS / 不用"恰好等于"判据）

$PY tools/m1/audit_consistency.py
# → 跨文档一致性对账（计数 vs README / 版本号唯一 / §1.2 状态分布 / 两个可执行门禁）
```

> **收口时必做：复跑全部历史证据脚本。** 单跑本包发现不了跨包失效 ——
> WP18 收口时复跑就发现 WP17 的证据被判退化（它的否定式判据写成了全契约范围，
> 见 ISS-64）；**WP20 收口时复跑又发现 WP18 的证据被判退化**（它把"不可变触发器
> 恰好 8 个"写成了等式判据，本包合法新增 2 个后变 10，见 ISS-71）——
> **同一类根因在本项目已经现身五次**。这是一个**真实发生过两次**的教训，不是理论风险。
>
> 判据：**历史证据只能断言"我建的还在"，不能断言"全项目恰好有 N 个"。**

> **证据文件的元信息必须现场派生，不能手写。** WP19 收口时发现三处证据说的还是旧事实
> 却**不报任何错**：`backend-pytest.log` 标题停在"WP06~WP10"（实际已到 WP20）、
> `op-register.log` 整份停在 78 条 OP（实际 154 条，且它当时**没有生成脚本**）。
> 已改为从事实反推（`_delivered_wps()`）与现场派生（`refresh_register_evidence.py`），
> 见 ISS-78。判据：**凡"描述这份证据覆盖了什么、有多少条"的文字，如果它会随后续开发
> 变大，它就不是文案而是计算结果。**

> **另一个容易漏的复跑口径：全量 pytest 要在默认库 `postgres` 上跑，不只是干净库。**
> WP20 收口时只在干净库跑全量是绿的；换到默认库跑立刻暴露一条（ISS-73）——
> WP16 的 `list_due_polls` 用例断言"我造的那条在返回列表里"，而它是**全局扫描**、
> 用例写死 `limit=100`，库里已积累 **104** 条历史 due polls → 被挤出窗口。
> **判据：断言"我造的对象出现在全局列表里"时，测试调用处必须显式传大 limit**
> （默认值是为**生产吞吐**选的，不是为**测试可见性**选的）。

> `wp11-versions.log` 除结构核对外，还会对真实节点**试写** UPDATE / DELETE，
> 断言被拦截且 `sqlstate=23001`，随后回滚。只查 `pg_trigger` 只能证明触发器
> **存在**，不能证明它**生效**。

> **写"断言某操作被拦"的探针时，取判定的动作本身不能有不可逆后果**（ISS-63）。
> 本项目的不可变表是**物理级**的 —— 实测连自己的探针残留都删不掉。
> 延迟约束场景用 `SET CONSTRAINTS ALL IMMEDIATE` 取判定（事务不提交即拿结论），
> 不要用 `commit()`。

### 最小环境（M0，需 Docker 或原生进程替代）

```bash
cd deploy/dev && cp .env.example .env && docker compose up -d

cd ../../tools/m0/wp05_smoke
/Users/<user>/.workbuddy/binaries/python/envs/default/bin/python smoke.py --write
# 重启后
/Users/<user>/.workbuddy/binaries/python/envs/default/bin/python smoke.py --verify

cd ../wp04_eval
/Users/<user>/.workbuddy/binaries/python/envs/default/bin/python score.py --selftest
```

## 当前状态

| 项 | 状态 |
|---|---|
| M0 本机可验证部分 | ✅ 已交付（G0-19 / G0-21 / pgvector 能力已用原生进程关闭） |
| M0 真机与外网部分 | ⏸ **未测** —— Live 四条链路、容器链、企业网络，全部待企业输入（见登记册 §1.3） |
| M1 已交付 | WP06/WP07 骨架、WP08 身份与会话、WP09 项目权限、WP10 项目与字典、WP11 版本注册与引用、WP12 分片上传与配额、WP13 任务与 Outbox、WP14 SSE 与断线续传、WP15 审计与待办、WP16 模型网关、WP17 文档解析与 OCR |
| M2 已交付 | **WP18 Brief 结构化解构**（5 张表 / 35 CHECK / 7 触发器 / 5 个端点；闭合 OP-88 的 `BRIEF` 那一半）、**WP20 知识 CRUD 与版本**（5 张表 / 53 列 / 30 CHECK / 15 FK / 6 触发器 / 9 个端点；闭合 OP-88 的 `KNOWLEDGE` 那一半）、**WP19 研究关键词与提示词**（**零表 / 零迁移 / 零写端点** —— 关键词、缺口、提示词全部由 Brief 版本**纯函数派生**，2 个只读端点；缺口判据读 WP18 的 `CATEGORIES_REQUIRING_EVIDENCE` 常量）、**WP23 Prompt 版本与反馈提议初始接口**（3 张表 / 6 触发器 / 13 CHECK / **6 个端点**；验收原文"模板变更可追溯、不扩大工具权限"两条都有**活性防线**；闭合 WP20 的 OP-150 产生方那一半）、**WP21 切块 + embedding + 范围过滤 + 检索**（2 张表 / 8 CHECK / 1 个触发器 / 2 个端点；**无维度 `vector` 列 + 每 profile 部分 HNSW 索引** 让多维度 profile 共存；闭合验收原文「近似品牌案例不串事实」的检索侧）、**WP22 信息审核与基线**（4 张表 / 不可变 `step_approvals` + `supersedes` 链 / 生产基线部分唯一索引；**已交付** —— 收口轮全量 pytest 暴露 6 个 MAJOR 已修复，另发现并修复 3 个跨包缺陷 ISS-106~ISS-108） |
| M3 已交付 | **WP24 场景形式草稿与运行期数量配置**（2 张表 `plan_drafts` / `runtime_settings` / 15 CHECK / 5 FK / 2 部分唯一索引 / 1 不可变触发器 / **8 个端点**，全仓第一个非 `/projects` 前缀的 `/settings/**` 路由组；**闭合 OP-60**「支持且必须有项目级副本」—— 三态模型（全局 / 副本 / 覆盖）+ 同事务级联刷新 + `diverged` 显式暴露 + J1~J4 四条判据；比例用**千分位整数**、分配用**最大余数法 + 容量削峰**且 `unallocated` 不硬塞）、**WP25 供应商计价 / 预算并发预占 / 费用账本**（5 张表 `pricing_versions` / `cost_estimates` / `project_budgets` / `budget_reservations` / `usage_ledger`；**2 张不可变表**（`pricing_versions` / `usage_ledger`，复用 `rednote_reject_mutation()` 触发器）+ **1 条部分唯一索引**（`uq_usage_ledger_idempotency`，只对 `idempotency_key IS NOT NULL` 生效）+ **14 个端点**；核心正确性放在**单条条件 UPDATE 的 `WHERE`** 里 `committed_amount + reserved_amount + :delta <= limit_amount`，`project_budgets` 刻意**不带 `lock_version`**；闭合 W10「预算并发预占测试」与 AC-26「估算/账单/重试核对」；**协议侧零费用端点** ⇒ 形状由本包定义并登记为待回填项）、**WP26 文案结构生成与标题候选**（**5 张表** `content_items` / `content_versions` / `narrative_blueprints` / `title_candidates` / `generation_contexts`；**38 CHECK / 21 FK / 17 显式索引**；**4 张不可变表**（`content_versions` / `narrative_blueprints` / `title_candidates` / `generation_contexts`，复用 `rednote_reject_mutation()` 触发器）+ **0 条部分索引**（有意形状，非漏写 `WHERE`）+ **1 个挂共享表 `version_nodes` 的 `CONSTRAINT TRIGGER`**（`trg_version_nodes_content_paired`，OP-88 的 `CONTENT` 反向那一半，`WHEN (NEW.entity_type = 'CONTENT')` 收窄）+ **6 个端点**；**延迟 FK 4 条**（`content_items.current_version_id` / `content_versions.generation_context_id` / `generation_contexts.content_version_id` / `content_versions.id → version_nodes.id`），其中**环 FK 只有 3 条**（不是 4 条 —— `content_versions.content_item_id` **非空且刻意保持 IMMEDIATE**，延迟它只是把真实错误从"当场拒"推迟到"COMMIT 拒"，无收益；范本 `brief/models.py` 的注释与实测 `pg_constraint` 矛盾，实测 `fk_brief_drafts_brief_id_briefs` 是 `deferrable=false` ⇒ 本包据实钉住，并写成**负向判据**）；闭合 **OP-160**（"哪个提示词版本被哪些生成消费"由 `generation_contexts.prompt_version_id` 反查）与 **OP-88 的 `CONTENT` 那一半**；AQ-01 事实正确率本机只交付「可核对性」（段级证据绑定 + `limitation` 必填 + 清单外事实拦截点），**正确率数字 `BLOCKED_ON_EXTERNAL`**） |
| 测试 | **2170 passed / 0 skipped**（`evidence/m1/backend-pytest.log`，EXIT=0；骨架基线 197）。口径构成：M2 收口基线 **1539 条** + M3 首包 WP24 新增 **190 条** + WP-DEBT-02 判据自身的判据 **58 条**（`test_test_database_target.py` **14** / `test_test_residue.py` 19 / `test_migration_model_parity.py` 8 / **`test_test_db_isolation.py` 17**，均为 ISS-158 修复的阳性+阴性对照） + WP-DEBT-03 纪律判据化判据自身的判据 **30 条**（`test_evidence_scope.py` 15 判据 D 的三方对账 / `test_shared_db_quiet.py` 15 判据 E 的静止前置，均为 ISS-162 / ISS-163 的阳性+阴性对照） + M3 第二包 WP25 新增 **107 条**（`test_billing_wp25.py`） + **M3 第三包 WP26 新增 92 条**（`test_content_wp26.py`，其中 **20 条带护栏标记**＝ 19 条 `regression` + **1 条 `cross_wp`**（挂共享表 `version_nodes` 的 `trg_version_nodes_content_paired` ⇒ 准入规则 3 要求的「别家不受影响」对照，登记 §3.2 的 RG-133））＝ **2136**（上式各项是各轮的**历史增量记录**；后续包会顺带修改既有测试文件，故**总数一律以本条实测为准**，不以上式加总为准 —— **WP27及其并行会话的增量（跨包入向 FK 白名单 2 条判据 / `truncate_targets` 闭包修复 / B3 接缝 17 条）与 WP47①② 的增量（`test_observability.py` / `test_content_wp26_exec.py` / 备份恢复演练）已整体计入本条实测，不再单列**，因为共享工作树里两侧增量不可机械分离，单列即等于人写账），**0 条跳过**（⚠️ 口径更正：上一版此处写「2164 passed / 1 skipped」并自标**待核项**（须在收口重跑时定位归属）；**收口重跑已定位** —— 该 1 条 skip 的真因是 `REDNOTE_TEST_DATABASE_URL` 原样喂给 `psycopg.connect()`、libpq 不认带方言后缀的 DSN，**已于 `c56f9eb` 修（ISS-174）**，故重跑自然归零；`+6` 的差额**逐用例拆解**（`--collect-only -o addopts=''` 取两树用例 id 集合做差，非按文件数估算）：`4f8f741` 树收集 **2165**（=`2164 passed + 1 skipped`，自洽）→ HEAD 树收集 **2170**（=`2170 passed + 0 skipped`，自洽），新增 5 条**全部**来自收口批：`test_media_wp27.py` 的 3 条 CHECK 行为断言（`53f734b`）+ `test_test_db_isolation.py` 的 2 条隔离判据对照（`86416f9`），删除 0 条；该 1 条 skip 现已归零，**与「ISS-174 修复后应为 0 条」一致**），（原记「1 条按环境跳过」并给出根因「要求以超管读 `pg_database`，本机 DSN 为普通用户」—— **该根因与事实不符**：真因是该用例把 `REDNOTE_TEST_DATABASE_URL` 原样喂给 `psycopg.connect()`，而该变量按本仓约定带 SQLAlchemy 方言后缀 `postgresql+psycopg://`、libpq 不认 ⇒ `ProgrammingError` 被 `except Exception` 吞成 skip（见 **ISS-174**）；已加一行协议归一化，14 条用例在两种 DSN 口径下均全过）。证据是**全仓**实跑（`cd services/backend && pytest`），涵盖全部按包测试（WP10~WP26）+ 非 WP 编号测试。**⚠️ 该 log 必须用与并行会话隔离的私有库重跑取得**：本包收口期曾因与并行会话共用 `rednote_test` 而并发污染（320 errors，见 ISS-160）；**并行会话也在同一工作树刷新同一文件**，读数前须先确认环境静止（PG 无其他连接 + 工作树零变化）。 |
| 契约 | **77 paths / 95 operations / 237 component schemas**（`contracts/openapi.json`，`--check` EXIT=0）。WP24 新增 7 paths / 8 operations / 22 schemas；M3 第二包 WP25 新增 10 paths / 14 operations / 27 schemas（费用域全部 14 个操作，协议 §1.4 实测零费用端点 ⇒ 形状由本包定义并登记为待回填项）；**M3 第三包 WP26 新增 6 paths / 6 operations / 19 schemas**（`/projects/{id}/content-generations`、`/projects/{id}/contents`、`/contents/{id}`、`/contents/{id}/versions`、`/contents/{id}/title-adoptions`、`/contents/{id}/regenerations`；协议 03 里很可能不存在这 6 个操作 ⇒ 形状由本包定义并登记为待回填项，与 OP-162 同族）；**M3 第四包 WP27 新增 5 paths / 6 operations / 20 schemas**（`/projects/{id}/media-generations`、`/projects/{id}/media-items`、`/media-items/{id}`、`/media-items/{id}/renditions`、`/media-items/{id}/regenerations`；协议 04 的静态图端点表尚未回填 ⇒ 形状由本包定义并登记为待回填项 OP-194~OP-196 + OP-205） |
| 待定项 | **205 条**：OPEN 198 / IN_PROGRESS 1 / RESOLVED 6，其中 🔴 P0 26 条 —— 答案都不在本机（**M3 第二包 WP25 新增 OP-181**，与 OP-176 / OP-162 / OP-173 同族：上游协议无本包端点定义 ⇒ 由本包定义并待协议回填；**WP25 收口期新增 OP-182**：全仓 124 处「判据纪律第 N 条」引用一套无权威定义表的编号 ⇒ 改写为语义锚点属独立工作，见 `docs/判据纪律-编号口径.md`；**WP26 开工前拍板新增 OP-183**：**WP28（Live 生成与封装）整体挂起** —— 输入形状全部由 WP02 真机结论定义而当前 0 条真机证据，唯一前置 = OP-76，处置 = **先做 WP26/WP27**；**M3 第三包 WP26 新增 OP-184~OP-188**（5 条，全 G 类 / OPEN / 🟡 P2，来自本包**附录 A** 的 8 条待定项 —— 其余 3 条不新开编号：A2 已被 OP-34 + OP-38 覆盖、A6 承 OP-130、A7 承 OP-161）：**OP-184** 本包 6 个内容端点在协议 03 里不存在 ⇒ 须回填协议（与 OP-176 / OP-181 / OP-162 / OP-173 同族）/ **OP-185** 标题候选"保留几个"未拍板 / **OP-186** 标题评分 6 维的评分口径与量纲未定 / **OP-187** 标签是否独立重做（需第三种 `task_kind`，而 WP23 只定义两种）/ **OP-188** 生成类端点的权限模型未定（与 OP-170 同族，但管写/触发而非读）；**WP47① 新增 OP-189~OP-193**（5 条）；**WP27 阶段 4 的 `files` 跨包接缝新增 OP-194~OP-196**（3 条，全 G 类 / OPEN / 🟡 P2 / 目标时机均为 `M3 收口`，见 WP27 文档 §2.4.2）：**OP-194** 服务端生成资产该发什么 Outbox 事件 / 要不要病毒扫描 / **OP-195** 生成资产的字节由谁在内存里持有 / **OP-196** `blob.mime_type` 记 `application/octet-stream` 是否可接受；**阻塞 A 收口新增 OP-197**（1 条，G 类 / OPEN / 🟡 P2 / `M3 收口`）：**正文切段口径的权威定义放哪、三处实现如何同步** —— 切段决定 `paragraph_ref` 的坐标系，本仓有**三处**实现（`content` / `knowledge` / `media`）且**因模块平级不可互 import 而无法收敛成一处**，故用判据 `check_paragraph_split_parity.py` 钉住一致性；口径已钉死为「只按空行切」，实测现场 `content` 侧写的是 `splitlines()`（段内软换行也切）⇒ 11 个场景 4 个分叉，含**小红书正文常态**（emoji + 段内换行）；**WP27 收口回查新增 OP-198~OP-199**（2 条，全 G 类 / OPEN / `M3 收口`，见 §4.3 **ISS-210**）：**OP-198** 「每篇固定 3 张」与原型可配 6/4/9 张的**显式偏差**（老大 2026-10-07 已拍板固定 3 张 ⇒ 待确认的是"是否本期口径"）/ **OP-199** 「3 张图分别画哪一段」的**对位规则**（老大 2026-10-08 已拍板「顺序对位」⇒ 待确认的是"是否符合长期语义"）—— ⚠️ 两条**此前只在代码注释里声称"已登记"、实际从未进册**，占位号 `OP-新7/8` 还被另一批内容复用（占位号无命名空间 ⇒ 撞号），而幽灵引用判据的正则只认数字形态 ⇒ 那 14 处引用**从未被核对过**） |
| 问题记录 | **225 条 / 0 error**（CRITICAL 2 / MAJOR 172 / MINOR 51；CLOSED 200 / MITIGATED 5 / OPEN 20；**2026-10-26 新增 ISS-225（item 级 404 同形，WP36/WP37 已修，其余模块未修）**；**WP27 阶段 5 Code Review 新增 ISS-218~ISS-220、WP-DEBT-04 交付新增 ISS-221~ISS-222**（CRITICAL-1 幂等查重声称 vs `media/service.py` 零调用 / MAJOR-1 六处 docstring「零覆盖」声称已过时 / TESTS-1 两条被点名断言零命中）；WP22 新增 ISS-96~ISS-101，回归护栏建设 ISS-102~ISS-105，收口轮跨包缺陷 ISS-106~ISS-108，收口复查 ISS-109，补 CI 门时发现的「覆盖范围靠记性」四处 ISS-111~ISS-114，WP22 收口尾轮复核 ISS-115~ISS-117，**前端接后端建设 ISS-119**（dev 登录死代码），**收口审计判据缺口 ISS-118 / ISS-120 / ISS-122**（证据时效 / README 计数自相矛盾 / 证据那次运行是红的），**ISS-123** 更正 ISS-121 的「偶发 flaky」定性 —— 实为**数据量依赖的必红**，断言已改「存在性 + 下界」并反向验证 3/3；**ISS-110** 为 `alembic check` 恒失败（OPEN，归 WP23 一侧）；**M3 首包 WP24 新增 ISS-124~ISS-141**（18 条：`_capacity_shave` 不收敛 / 停用项白拿分配额 / `unallocated` 可为负 / 读取侧不校验项目存在 / 级联刷新 N+1（7671 → 2 条 SELECT）+ 判据自身的两类缺陷 —— **恒真式穷举**「错误 ⑪」与**抛出点巧合**「错误 ⑫」+ 证据脚本自检覆盖范围**第三次复发** + 反向验证方法本身**不校验红的形状**；**ISS-124** 为文档声明 3 个从未产出过的证据文件，已补机械判据 `check_declared_evidence_exists`）；**WP24 收口复跑新增 ISS-142 / ISS-143**（2 条，**都是"单跑本包永远发现不了"的缺陷**：**ISS-142** = 本包 6 条 `regression` 测试从未登记进护栏登记册 ⇒ CI 门 3 的 `check_register.py` 会立刻红；**ISS-143** = 复跑载体把跨包脚本路径写死 ⇒ 两个脚本 `rc=2` **根本没跑**却被打印成 FAIL，且「覆盖范围由人手写」同一族缺陷**第四次**现身）；**工程债清偿包 WP-DEBT-01 新增 ISS-144**（`pyproject` 只声明 7 个直依赖而 `src` 实际需要更多 —— 干净环境按声明装完跑 `alembic upgrade head` **第一步就 `ModuleNotFoundError: cryptography`**，而 `parsers/*.py` 里 4 处注释却在说「依赖已在 pyproject 锁定」；修复 = 直依赖 7 → 15 + 新建反向覆盖判据 `tools/quality/check_dependency_declarations.py`（AST 硬导入 vs pyproject，带 10/10 反向验证）+ 挂进 CI 门 3；**同族复发第 5、6 次**：本判据自身的 `rglob`/`ast.walk` 覆盖范围、以及两个自检载体的收录范围只认 `*_evidence.py` 而漏掉 4 个 `collect_*code_review.py`，均已改由磁盘决定。见 `docs/m3/工程债-依赖声明一致性说明.md`）；**工程债清偿包 WP-DEBT-02「测试库可信度」新增 ISS-145~ISS-148**（三条判据各对一个真实失效面：**ISS-145** = 默认测试 DSN 指向本机 `postgres` 系统库，累积 66 万行垃圾、全量 pytest 慢约 130 倍；**ISS-146** = 迁移链与 SQLAlchemy 模型是两条独立建表路径，产物不一致而 1729 条测试全绿（测试只走 `create_all`）；**ISS-147** = 测试残留 HNSW 索引跨天累积，`hnsw_index_drop_ddl` 零调用点；**ISS-148** = `test_planning_wp24.py` 的 J2 计数判据把「跨时刻的全库项目计数」当不变量，共享库被并发写入时**正确的实现被判红**（实测 `4632 == 4631 + 1` ⇒ 实现无错，漂移在期望值那次读）—— 属 ISS-37/49/50/73 的 TOCTOU 同族；**另新增 ISS-149~ISS-152 四条「反向验证/登记工具自身」的缺陷**：**ISS-149** = 反向验证断言了判据**从不输出**的标记 `RESULT=PASS` ⇒ 两个「期望假绿」用例恒失败（并补上「绿的形状」这一维：`[PASS]` 且不含 `[FAIL]`，是 ISS-140 的镜像）；**ISS-150** = 变异前提写错 —— 只摘掉一道守卫，而 `check_test_residue.main()` 有两道独立守卫，「摘掉一道」不等于「防线不存在」；**ISS-151** = 登记册 §4.3 标题的编号区间由人写而静默漂移（写着 `ISS-01 ~ ISS-141`，渲染区实为 144 条）—— `replace_block()` 只管标记内文本，铁律 14 又一次复发，已让 `log_issue.py` 由数据源机械派生区间并加跨文件对账；**ISS-152** = 「真树未被触碰」的失败文案把两个互斥成因（本工具写坏真树 / 外部在窗口内写盘）印成同一条断言，且把可能性低的那个写成默认解释 —— 与 ISS-143 同族，两个成因同形即等于没有诊断力。见 `docs/m3/工程债-测试库可信度说明.md`）；**同包收口复核又新增 ISS-153**（完整版反向验证实际产出 **17 条 CHECK**，而 `EXPECTED_CHECKS_FULL` 仍写 **15**，门 3 要求的 `complete = len(checks) >= EXPECTED_CHECKS` **只拦变少、不拦变多** ⇒ 判据与常量脱钩后照旧印「期望 N 条」，读证据的人会以为已核对过；修法 = 修常量 + **另加一条相等判据** `exact = len(checks) == EXPECTED_CHECKS` 并入 verdict 与 RESULT 行，并变异验证它有区分力（常量改 7→6 实测 `RESULT=FAIL` / 别名「实际 7 条 ≠ 期望 6 条」）。同一轮还修掉 **ISS-142 复发**：本包三个新测试文件的 **28 个** `regression` 函数未登记，`tools/regression/check_register.py` 报 28 个 R3 孤儿 ⇒ CI 门 3 必红；已回填 `docs/regression-register.md` **RG-052~RG-079**（§3.1 计数 51 → 79、文件头 v1.1 → v1.2），登记册 **89 行 = 89 个带护栏函数 / 0 FAIL** 回绿）；**同包收口末轮又新增 ISS-155 / ISS-156**（**ISS-155** = 本包三个新测试文件为直接调判据的 `main()` 而 `import check_xxx`，这三个名字未登记进 `LOCAL_IMPORT_NAMES` ⇒ CI 门 3 的依赖声明判据报 **3 处红**（ISS-144 第三次复发）；教训 = 「三处同步」不够，收口应**把门 3 全判据各跑一遍** —— 根子仍是"收口清单由人写"（铁律 14）。**ISS-156** = 反向验证的**副本目录名写死常量** `/tmp/rdbtdc-{tag}`，两次**并发同模式运行共用同一目录**、互相 `rmtree`/`copytree`：实测一次完整版 13m31s 后 `FileNotFoundError: …/projects/models.py`，且残留副本内文件的 mtime（15:23）**晚于**该次运行起点（15:17:48）⇒ 副本是**别人的**；更隐蔽的是副本可能停在别人的中间态，让判据 B/C 报出与本次变异无关的红 —— 与 ISS-154 **同一根因的另一个面**（把"该路径归本实例独占"当不变量，而它是跨实例可变量）。修法 = `mkdtemp` 由系统原子分配唯一目录名 + `_copy_is_alive()` 前置检查把"副本消失"报成可读的判据红而非裸崩栈；**修 ISS-156 时又自引入 ISS-157** —— 新加的存活探针把 `TOOL_A`（已含 `tools/quality/` 前缀）又拼了一层，路径恒不存在 ⇒ 探针**恒假** ⇒ 3 条变异全部误报成"副本已消失"（与 ISS-143 同形：一个永远为假的前提与"真的坏了"输出完全一样）；修法 = 探针路径抽成 `_COPY_PROBES` 常量 + `_probe_is_sound()` 拿**真树**做阳性对照 + 新增 `probe-sound` CHECK（完整版 17 → 18 条、`--fast` 7 → 8 条）。**同轮收口又新增 ISS-158 / ISS-159**（**ISS-158** = 隔离判据 `check_test_db_isolation` 只认两种合规形态，本包新写的 3 个测试文件 + 2 个 tools 脚本被报 **21 处假阳性**（真硬编码 0 处）：6 处是 `.replace(...)` 的协议替换实参（`replace` 不在 `_PATTERN_METHODS`）、1 处是把默认值放在 `or` 链尾、13 处是 `assert` 期望值 / `parametrize` 参数表这类**被测数据**、1 处是夹具 `os.environ["REDNOTE_TEST_DB…"] = …` **在主动写隔离变量** —— 判据把「句法位置」当成了「数据流性质」；而它挂在 **CI 门 3**，所以 CI **自 `79dd24c` 起就是红的**，同提交信息却写着「验证（全绿）」（列出 7 条判据，**恰漏了报红的这条**）。修法 = 按位置补三类豁免（结构判定，**不建文件名白名单**）+ 新增 `test_test_db_isolation.py` **17 条**双向对照（阴性：6 种真硬编码必须仍报红，含"字面量是 `replace` 的调用者而非实参"这个关键边界；阳性：7 种合规写法必须 0 报红），并做**双向变异验证**（豁免放宽 ⇒ 3 红全为阴性对照；豁免清空 ⇒ 5 红全为阳性对照），21 → 0；**ISS-159** = 证据行 `PASS ci-step:<path>` 名字声称"这一步通过了"，实际判定只有 `step in ci_text`（该路径字符串在 `ci.yml` 里出现过）—— 隔离判据报 21 处红时这一行**照印 PASS**，连收口复核都被读过去；性质是"名字声称的事超出了它检查的事"（ISS-143 / ISS-95 合流），且「挂载完整性」与「执行完整性」是两个独立事实、前者不能给后者背书；修法 = 改名 `ci-step-declared:` + 同步反向验证的 `expect_fail_marker`；**ISS-160** = 本包收口时**违反自己 §4.2-D 的库隔离纪律**：残留判据的 pytest 与并行会话的全量取证 pytest 在同一个共享测试库 `rednote_test` 上**并发**（时间窗 16:19:46~16:23:00），双向污染 —— 对方 `DROP TABLE … CASCADE` 等锁超时 ⇒ 320 errors 写进 `backend-pytest.log`（该文件是基线计数来源），我方 `test_events_wp14.py` 的跨实例 fan-out 收到**对方的 NOTIFY** ⇒ `Left contains one more item: 2227` ⇒ 2 failed；**判定性验证**：切到私有库 `rednote_wpdebt02` （alembic 到 `daea2b27212c`）后 `1785 passed / 1 skipped / [PASS] 无残留`，2 failed 完全消失、耗时 659.72s → 170.08s；根因 = 把「共享资源在本实例运行期间归我独占」当**不变量**（同族第 5 面：ISS-148 / ISS-154 / ISS-156 / ISS-158），而「并行会话各自隔离库」此前**只写在文档里、无判据守住**（ISS-95））；**同轮并行会话（WP25）新增 ISS-161**（共享 venv 的运行环境被无关第三方包 `typesafe-sdk` 顶版 —— pydantic `2.9.2 → 2.13.5`；差异只在**产物的结构**：单值 `Literal["X"]` 在 2.9.2 emit `{"const","enum"}`、在 2.13.5 **丢掉 `enum`**，多值 `Literal` 两版一致 ⇒ 恰好命中 `InternalErrorBody.code`，`test_contract.py:235` 报 `KeyError: 'enum'`，而其余 1100+ 条照常全绿；源码 `Literal` 形态完好 ⇒ 矛盾唯一指向**生成 schema 的那一方**，回钉 `pydantic==2.9.2` + 重导出契约后 `--check` EXIT=0。根因 = `check_dependency_declarations.py` 只守「源码 import ⊆ pyproject 声明」，**没有任何判据守「实际装的版本 == pyproject 钉的版本」** —— 声明侧被守住、运行侧是空的，与 ISS-160 同族：**把共享资源在运行期归我独有当不变量**（一个共享数据库、一个共享解释器））；**WP-DEBT-03（纪律判据化）新增 ISS-162 / ISS-163** —— 把两条此前只写在文档里的纪律落成真判据：**ISS-162** = 铁律 14 的第 7 次现身，这次的「覆盖范围由人写」是**证据脚本名单** —— 复跑载体 `rerun_all_evidence.py::SCOPE_PATTERNS`（A1，3 条 glob）与自检载体 `check_evidence_scripts.py::_iter_scripts()`（A2，4 条 glob）是**两套独立的人写名单**，分叉后果是「某脚本永远不被复跑却照样写证据」或「门禁不检查它的 `EXPECTED_CHECKS` 形态」，而真正的事实（谁在写 `evidence/`）全项目无人过问；修法 = 引入**第三方视角 B**（AST 遍历 `tools/**/*.py`，按**行为**反推「会写入 `evidence/` 下非临时文件」的脚本，完全不看文件名），三方对账 D1 A1⊆B / D2 B⊆A1 / D3 A1⊆A2 / D4 `len(B)>=27`，外加 D2b「豁免白名单不腐烂」（逐条核对理由是否仍成立，**不建文件名白名单**）；实测 A1=28 / A2=29 / B=29，豁免 1 条且理由成立；**ISS-163** = 铁律 17 此前只是**文档里的一句话**（技能里明确标注「该判据尚未实现，故本条目前只是自觉」= ISS-95），WP-DEBT-02 收口期两次被并行会话污染（对方 320 errors / 我方 2 failed）；修法 = `check_shared_db_quiet.py` 用**独立连接**查 `pg_stat_activity`（观测口径从 `ps` 改为问 PG 本身 —— 沙箱会拦 `ps`，且「取不到观测」会被伪装成「观测结果是干净」），有他人连接即**拒跑**（fail-fast 而非事后告警），E1 读不到库名也拒跑、E2 报红必须**指名 pid**（可处置）、E3 不可达 DSN **fail-closed**、E4 `--ci` **响亮 SKIP**（CI runner 以 `services: postgres` 独占实例，该问题在 CI 上无区分度；**判据 E 刻意不进 CI**，其载体是收口期完整模式）；接入点是顺序问题 —— `_assert_shared_db_quiet()` 必须在**任何子进程启动之前**；两条判据均配反向验证（判据 D 的 D①~D⑤ 含**成对变异**证明「红确实是这条判定造成的」；判据 E 的 E① **自己开占位连接**，不依赖并行会话、任何时刻可复现）；**同包收口期又发现并修复 ISS-164（P0）** —— 铁律 14 判据化（ISS-162）与铁律 17 判据化（ISS-163）**各自都是绿的**，但它们的**集成路径从来没有跑通过一次**：`tools/m1/rerun_all_evidence.py`（收口第 11 步的载体）在加载 `check_shared_db_quiet.py` 做静止前置检查时，用 `importlib.util.module_from_spec()` + `exec_module()` 却不注册 `sys.modules`，而 CPython 3.13 的 `dataclasses` 在解析注解前必须 `sys.modules.get(cls.__module__).__dict__` ⇒ 拿到 `None` ⇒ `AttributeError: 'NoneType' object has no attribute '__dict__'`；该异常被同一段的 `except Exception` 吞掉，并被**谎报**成"目标库上有他人连接"，而实测 PG 上 `rednote*` 库**零连接**、判据 E 本体单跑正是 `[PASS]`。后果：第 11 步从未真正执行过，反向验证也没覆盖到它（判据 D 的 D3 只检查 A1⊆A2 名单，不检查脚本能否跑）。**四层叠加**：① 漏注册 `sys.modules`（真因）② 异常处理 fail-open 到"跳过检查"③ 文案把"无法确认"印成"确有占用"（两个互斥成因同形，ISS-143/ISS-152 同族）④ 没有任何判据覆盖"零件装不装得上"。修法 = `sys.modules[name] = module` + 拒跑返回值改**三态** `quiet/busy/unverifiable` 并各配文案 + fail-open 改 fail-closed（与 E4 对齐）+ 新建**判据 F** `tools/quality/check_loader_registration.py`（AST 扫全仓 `spec_from_file_location` 加载点，要求同作用域内注册 `sys.modules`，含 `import sys as _sys` 别名解析；F1 主判据 + F2 活性下界 ≥5 + F3 解析失败必须报红；实测 8 个加载点全注册）并挂进 CI 门 3；`check_evidence_scope.py::_load_module` 同款补上（当前加载的两个模块恰好无 `@dataclass`，缺这行时不报错 —— 定时炸弹）；反向验证加 F①（删掉注册行必须报红，命中 `未注册 1 个`）/F②（`MIN_LOAD_SITES` 抬到 999 必须报红），`--fast` 11 → 14、完整 18 → 21；三遍法 Code Review 的 `tail_secs` 同步为 `{9,15,16,17}`。**教训**：**判据本体的绿 ≠ 集成路径的绿** —— 五维度（Security/Performance/Correctness/Maintainability/Testing）里没有一个维度问"这些零件装到一起还能跑吗"，故本包新增第 6 维度 **Integrability**。见 `docs/m3/工程债-纪律判据化说明.md` §5.3c）；**同包收口期再发现并修复 ISS-165 / ISS-166（2 条，均为 MAJOR）** —— 两条都是**"为了让判据可信而做的动作，反过来把判据弄脏"**，且都发生在**刚新建的取数口径里**：**ISS-165** = `services/backend/tests/test_test_database_target.py` 用「变异-还原」验证 `check_test_database_target.py` 会报红（`read_text → replace → write_text` 真树 → 跑判据 → `write_text` 还原），**内容还原了、mtime 没有**（`Path.write_text` 必然刷新 mtime）；而 `tools/m1/audit_consistency.py` 的证据时效判据当时把「被证据目录下最新 mtime」当作「代码最后改动时刻」⇒ 每跑一次全量 pytest 就把该时刻往前推 ⇒ `backend-pytest.log` **永久假红**（实测差 56612 秒，而该文件内容与 HEAD 分毫不差、`git diff --quiet` exit 0；判定性验证：单跑那一个用例使 `test_devtools.py` 的 mtime 前进 **92.24 秒**），且与「真改了代码忘了重跑证据」**在输出上完全同形**（ISS-143 族）。修法两条腿同时落地：**取数口径**改为「按内容取数」（`_tracked_last_change()`：`git log -1 --format=%ct` 取最后提交时刻 + 未提交改动的文件取其 mtime，两者取最大；取不到 git 事实时**降级并出声说明**是降级，静默降级 = 判据失明）+ **动作**改为「变异只落在 `tmp_path` 副本里」（真树只读由**路径结构**保证，不靠自觉），并新增反向验证 `test_mutation_stays_inside_the_copy` 断言真树 `services/backend/tests` 下每个文件的 `(大小, mtime_ns)` 逐条不变 —— **必须含 `mtime_ns`**，因为病灶恰恰是「内容没变而 mtime 变了」，只比内容的话这个缺陷能大摇大摆通过它自己的反向验证（ISS-115）。**ISS-166** = 上面新写的取数口径第一版就踩了 `git status --porcelain` 的默认行为：它把非 ASCII 路径**转义成八进制**（`docs/\345\276\205...`），Python 侧 `REPO_ROOT / rel` 找不到该文件 ⇒ **静默跳过**该文件的未提交改动 ⇒ `op-register.log` 报「未提交改动 **0** 个文件」而 `docs/待定项与问题登记册.md` 明明是 ` M` ⇒ 该证据被误判同代。**失效方向是偏绿（漏报陈旧），比假红危险** —— 假红会被看到，漏报只会让陈旧证据安静留在库里。修法 = `_git()` 统一加 `-c core.quotepath=false`；并**同族排查**：另外两处 `git status --porcelain` 调用点里，本包产物 `tools/quality/collect_discipline_reverse_evidence.py` 一并修（那里方向相反、是**假红**，因 `TREE_SCOPE` 内实测 0 个非 ASCII 文件名），已提交的 `tools/quality/collect_test_db_credibility_reverse_evidence.py` 同名函数**如实登记为残留盲区**。**教训**：**"验证判据会报红"的手段本身必须遵守它要守的纪律**；凡「写盘再还原」都把 mtime 当成了可还原量，而它不是。见 `docs/m3/工程债-纪律判据化说明.md` **WP25 收口新增 ISS-168~ISS-171**（每条都是"单跑本包时看不见、收口复跑时抓到的"）：**ISS-168** = 两个 CR 证据收集器**恒返回退出码 0** —— 实测 `collect_wp25_code_review.py` 打印 `31 PASS / 4 FAIL` 而 `echo $?` 是 **0**，因为脚本把结论只写进文本；而收口第 11 步对每个脚本只断言 `rc==0` 与「证据 mtime 前进」⇒ 恒返回 0 的脚本**两条都 PASS** ⇒ **判据报红与判据全绿在机器可读层面同形**（ISS-143 变体）。同款形态当时也在 `collect_wp24_code_review.py`（随 `afdd91f` 提交），只因「恰好 0 FAIL」而从未暴露 —— **那是运气，不是防线**。修法 = 两个收集器补 `EXPECTED_CHECKS` + 条数核对 + `raise SystemExit(0 if 结论 else 1)`，并新增门禁 `check_evidence_scripts.py::check_returns_exit_code`（用 **AST** 判节点，因脚本刻意在引号里保留旧写法说明文字 —— 第一版用正则会被引文骗过，**写判据的人犯了他要防的那个错**）；**ISS-169** = 上述缺陷被 **4 处**标成 `ISS-167`，而 `ISS-167` 实为「反向验证把并行会话在途文件当成失败」—— **注释里的「见 ISS-NN」是弱引用**（`ISS-117` 第 2 次复发；`ISS-117` 的 lesson 已写明该判据缺口，但 7 天内从未实现）。**刻意不实现** ISS-117 建议的「断言注释主题与被引用条目相符」判据（那需要自然语言理解，用字符串重合度实现只会造出恒真或高假阳性的判据 —— ISS-95 / ISS-115 同族），改为纪律；**ISS-170** = `test_estimate_cost_without_limits_configured_is_422` **把「这个库没跑过 WP24+ 迁移」当成通过前提**：WP24 迁移 `022ba9804d66` 末尾 `bootstrap_defaults(...)` 种下 GLOBAL `batch.limits`，`get_effective_limits` 对「全局有 + 项目无」返回 `source=GLOBAL` ⇒ 项目**继承了默认值** ⇒ 不抛 422。实跑三库对照：`rednote_test`（夹具建表）✅ / `rednote_test_wp25`＋`rednote_fresh_probe`（迁移建出）🔴 —— **只有换一个更接近生产的库才暴露**。修法 = 打桩显式构造「全局、项目都没有」的语义（`runtime_settings` 是不可变表，清库路线根本不存在），并做**区分力**反向验证（退化实现兜底默认值 ⇒ 用例报红 ⇒ 证明它没被改成恒绿），机械判据化为 CR 收集器的 `C34/C34b/C34c`；**ISS-171** = **反向验证脚本自己的基线写死**（`"36 PASS / 0 FAIL / 36 checks"`）：被验收集器从 36 涨到 39 再到 **40**，两处断言从未同步 ⇒ `baseline` 与 `restored` 两条**恒 FAIL**，而该脚本自 36 时代生成过一次证据后**再没重跑** ⇒ `wp25-code-review-reverse.log` **自此陈旧**（文件在、格式对，只有结论早已不是事实）。红的形状是「条数行未命中」，读起来像「收集器坏了」，**实际是尺子没跟着涨**。修法复用 `ISS-167` 的同一句话（绝对断言 → 相对基线）—— 新增 `_expected_checks_from_tool()` 从**被测脚本自己的源码**读 `EXPECTED_CHECKS`（并断言它只出现一次），`CHECK` 名改为 `baseline-all-pass-0-fail`（**名字里不再带数字**）。**教训：参照物与被测对象是两个会独立演化的东西；判据的参照值也必须从被验对象派生。** 见 `docs/m3/WP25-CodeReview.md`）；**WP25 收口第 11 步复跑又新增 ISS-172** —— 本包阶段 6 新增的 `test_billing_wp25.py::test_router_exposes_exactly_fourteen_endpoints` 带 `@pytest.mark.regression` 却未同步登记进 `docs/regression-register.md`，复跑时被 CI 门 3 的 `check_register.py` 以 **R3 孤儿**当场点名（登记册 96 行 vs 代码 97 个带护栏函数），`collect_regression_evidence.py` 因此报 `RESULT=FAIL checks=41/41 failures=2` —— 而**单跑 WP25 自己的证据脚本永远是绿的**（R3 是代码 ⇄ 登记册的**跨文件**判据，单包脚本只看本包对象）。这是 **ISS-142 / ISS-153 同族的第三次现身**：前两次都已把纪律写进文档并落成 R3，本条证明「把同步动作写进文档」对这类缺陷无效，有效的是让它在**同一原子提交里绕不过去**（或让门 3 在分支上就跑，而不只在收口复跑时跑一次）。修法 = 登记册补 **RG-087**（来源写 ISS-83 + ISS-172，形态与 RG-039 / RG-051 同形，§2 准入规则 4 明确允许「本包自己的路由数」）、§3.1 计数 86 → 87、文件头 v1.2 → v1.3，复跑 `check_register.py` 回绿（**97 行 == 97 个带护栏函数 / 0 FAIL**）；**同包收口又新增 ISS-173 / ISS-174**（两条同族 —— **"判据的尺子本身不对"**，且都是**单跑本包时看不见**的）：**ISS-173** = 全仓 **124 处**「判据纪律第 N 条 / 铁律第 N 条」引用一套**无权威定义表**的编号（编号跨 1~14/17、前缀归一后 **22 种形态**；穷举搜「编号 + 定义」零命中），更硬的两条是「判据纪律第 5 条」与「第 12 条」被引作**同一语义**（『没跑』与『跑失败』同形），以及 `docs/m3/WP24-…说明.md` 写「承接 §4.4 的既有 **11** 条」而该表在全部 12 个提交里**历来只有 7 条**（G1~G7）—— 性质是**弱引用**（ISS-117 / ISS-169 同族），且用「改对编号」修不掉（判定哪个编号才对本身就依赖那份不存在的权威序）；修法 = 新建权威口径 `docs/判据纪律-编号口径.md`（§2 定义表 / §3 已登记未定义编号 **22** 条 / §5 关闭条件）+ 审计新增 **§8** `check_discipline_references()` 做口径台账 ⇄ 实测引用的**双向**对账（新增未登记编号报红 + 台账腐烂报红，已做双向注入验证），改写 124 处引用**刻意不并入**本包、登记为 **OP-182**；判据本轮抓到的第一个缺陷就是它自己的作者（口径文件 §5 写「已登记为 OP-182」而当时册里没有 OP-182）。**ISS-174** = 本包阶段 6 的对照用例 `test_test_database_target.py::test_static_reserved_databases_covers_all_pg_templates` 把 `REDNOTE_TEST_DATABASE_URL` **原样**交给 `psycopg.connect()`，而该变量带 SQLAlchemy 方言后缀 `postgresql+psycopg://`、libpq 不认 ⇒ `ProgrammingError: missing "=" after …` 被 `except Exception` 吞成 `pytest.skip("无法连接 PG")`，于是 **PG 明明可达也照跳**，表现为**私有库口径 1923 passed / 1 skipped、默认口径 1924 passed / 0 skipped**（骨架差分只差这 1 条）；判定性证据 = 同一条 DSN 换 `postgresql://` 即连通（读到模板库 2 个），且同族的 `test_migration_model_parity.py::_connect_or_skip` 早已归一化故从不跳 —— 证明是**遗漏而非设计**；修法 = 该用例补一行 `.replace("postgresql+psycopg://", "postgresql://", 1)`（不新增 DSN 字面量，用隔离判据已豁免的协议替换形态）+ 同步本文档与 WP25 两份文档计数 + **勘误**上文那条 skip 的根因。**教训**：**文档里的根因若是猜的，会长期以「已知」的身份骗人** —— 本次靠 `psycopg` 两 DSN 直连对照才把它勘误掉；且凡「先 try 再 except 就 skip」的用例，捕获范围必须窄到**资源不可达**，捕宽了就把「我传错了参数」伪装成「环境不适用」（ISS-143 反复复发的同形病）。见 `docs/判据纪律-编号口径.md` / `docs/m3/WP25-供应商计价与费用账本说明.md`）；**M3 第三包 WP26 新增 ISS-175 / ISS-176**（2 条，均为 MAJOR，且都是**收口期才现身**的）：**ISS-175** = 本包反向判据漏掉「实体不归我管就放行」这个守卫分支 —— `test_version_nodes_reverse_trigger_*` 起初只造 `content_items` 行，而守卫收窄后随机 uuid4 会走**放行**分支，于是「反向判据阳性面」从未真正被触发；错因是把「仅扫 `src/` 的零消费结论」当成了全仓结论（只做了一半的「完全同构」）；修法 = 造**真实** `content_items` 行命中拒绝分支 + 另加一条放行分支的对照（没有对照则「永远 RAISE」的实现也全绿）。**ISS-176** = 本包 20 个 `@pytest.mark.regression` 函数未登记进 `docs/regression-register.md` ⇒ CI 门 3 的 `check_register.py` 报 **20 个 R3 孤儿**（登记册 110 行 vs 代码 130 个带护栏函数）—— **ISS-142 / ISS-153 / ISS-172 同族的第 4 次现身**（与 ISS-172 同型第 2 次）；而 `audit_consistency.py` 当时只报 7 处不一致、**完全不包含这 20 处** ⇒ 「主审计报 7 处」≠「只剩 7 处」，因为主审计**不跑 CI 门 3 的 `check_register.py`**（R3 是「代码 ⇄ 登记册」的跨文件判据，单跑本包证据永远发现不了）；修法 = 回填 **RG-113~RG-132**（§3.1 计数 98 → 118、文件头 v1.4 → v1.5），复跑 `check_register.py` 回绿（**130 行 == 130 个带护栏函数 / 0 FAIL**））；**M3 第三包 WP26 收口新增 ISS-177~ISS-184**（8 条：CRITICAL 1 / MAJOR 2 / MINOR 5；CLOSED 1 / OPEN 7）：**ISS-177**（🔴 **CRITICAL**，OPEN）= 本包 R4 证据绑定防线**只写在 docstring 里** —— `claim_scope` 声称"服务层据此拒绝（`REASON_BASELINE_NOT_READY`）"，而该 reason 在 `service.py` 的抛点为 **0** ⇒ 基线无 claim 时**静默放行**、R4 降级为"不判绑定"（**ISS-95「声称有防线」的最强形态**：连 reason 常量都定义了、就是没抛）；**ISS-178**（MAJOR，OPEN）= 本包**最高风险执行路径零覆盖** —— `execute_generation` 在全 `tests/` 目录**命中 0 次**，而 R1/R2/R4/R5/R6/R8 六条高风险项的唯一落点就是它；**ISS-179**（MINOR，OPEN）= `api.py:337` 注释与实现方向相反（注释说"静默变成 422"，实现是 `raise RuntimeError` 全局兜成 500）；**ISS-180**（MINOR，**CLOSED**）= CR 判据用「集合差集」下缺陷结论，把一个**不可达**的 reason 误判为"触发 HTTP 500"—— 加入**可达性闭包**后本轮的 CRITICAL 定性被**推翻**（**判据自身的缺陷**，与"错误 ⑪⑫"同族）；**ISS-181**（MINOR，OPEN）= 两处 docstring 互相矛盾（`resolve_provider_id` 声称有两个调用方，`api.py` 实测 **0 次**且自述"没有金额/预估字段"）；**ISS-182**（MINOR，OPEN）= 5 条 `REASON_*` 常量定义并导出后**全模块无引用**，其中 `REASON_IDEMPOTENCY_CONFLICT` 被 `api.py` 的**字符串字面量**替代；**ISS-183**（MINOR，OPEN）= `models.py` 有 4 处**模块级裸 `assert`** 做导入期对账，`python -O` 下会**静默消失**；**ISS-184**（MAJOR，OPEN）= 本包文档为 **OP-160** 写下的**关闭条件**（"按 `prompt_version_id` 反查历史生成的用例"）**在测试里不存在**（`test_content_wp26.py` 与全 `tests/` 目录对该标识符**零命中**、反查函数无任何测试引用）⇒ 若照它关闭 `OP-160` 等于**用不存在的证据关闭一条待定项**（ISS-84 原话的现身）；处置 = 文档降级 + `OP-160` 状态只推进为 `IN_PROGRESS` + 补用例列入下一包清理项（见 `docs/m3/WP26-文案结构生成与标题候选说明.md` §7.9）；**WP26 收口审计轮又新增 ISS-185 / ISS-186**（2 条，均为 MAJOR，且**都发生在「判据自己的取数口径」上** —— 判据自身有缺陷时，它的绿与红都不足采信）：**ISS-185** = `audit_consistency.py` 的「README 同一计数多处声明」判据**自身失效**，三条计数里两条正则各有病 —— ① `OP 条数` 的第二条 `全部\s*\*\*(\d+)\s*条\*\*` **恒不命中**（README 从头到尾写「全部 188 条待定项」，星号位置不符；该正则自 `b0f9ae6` 引入起**一次都没匹配过** = 判据的一个入口从来是空的，而输出与"判过"逐字同形，ISS-95 / ISS-115 的对称形态「恒不命中」）② `OP 条数` 的第一条 `(\d+)\s*条待定项` **无锚点**，抓到 WP26 看板行里的局部叙述「来自本包**附录 A** 的 8 条待定项」⇒ 取值集合 `{8, 188}` ⇒ 判据按「同类声明必须只有一个取值」报**假红**（README 无任何真实矛盾：8 是附 A 条数、188 是全局条数，两句都对）；修法 = **改工具不是改数字** —— 三条计数各自锚定来源（概述句加 `全部` 前缀、看板行加 `^\|\s*<字段名>\s*\|\s*\*\*` 行首锚）+ `re.finditer(..., re.M)`（`^` 锚行的前提，已在 docstring 写成显式警告：漏了 `re.M` 会让三条一起静默失配），并做**变异验证**（L251 的 `188` 改 `189` ⇒ 立即 `[188, 189]` FAIL；真实内容 `8 条待定项` ⇒ 不误伤）；**ISS-186** = 文档声明的证据路径判据只认「**连写**」形态（`evidence/<层>/<名>.log` 在同一处），而目录与文件名**分写**的声明（`evidence/m3/` + 括号里 `wp26-content-generation.log`）**完全不被命中** ⇒ 与它要消灭的缺陷（ISS-124）同形；修法 = 逐行先把连写命中用等长空格抠掉、再要求该行恰好剩 1 个目录 + 1 个反引号文件名（首版未抠连写命中，把同一行里**两处连写命中**交叉配对，配出一条**并不存在**的路径 —— 层名取自前一处、文件名取自后一处）+ 机械排除 `~` 形态（`wp10~wp16-code-review.log` 是范围记法，实指 7 个文件）；**且本条附带更正 4 处误引** —— `audit_consistency.py` 注释与 WP26 文档共 4 处写「记 **ISS-177**」，而 `ISS-177` 实为**另一条无关的 CRITICAL**（`claim_scope` 那条），经全库检索确认「分写形态」**从未登记过**（**ISS-117 / ISS-169 同族**：注释与文档里的「见/记 ISS-NN」是弱引用，且本项目只对 `OP-` 引用有机械对账、对 `ISS-` 引用没有），已补登记并全部改指 ISS-186（见 `docs/m3/WP26-文案结构生成与标题候选说明.md` §8.4 / §8.6 / §10.3））；**WP26 收口审计轮再新增 ISS-187**（MAJOR，**与 ISS-185 / ISS-186 同为「判据自己的取数口径」缺陷** —— 判据自身有缺陷时，它的绿与红都不足采信）：**ISS-187** = `audit_consistency.py` §7（`check_op_references_resolve`，专管「文档引用的 OP 编号必须真实存在」，即 ISS-172 那类幽灵引用的防线）把编号位数**写死成 3 位**：`_OP_REF_RE = OP-\d{3}` / `_OP_ROW_DEF_RE = ^\|\s*(OP-\d{3})\s*\|`。而本项目 OP 编号**从 2 位起步**（OP-01…OP-99）、过百后才自然变 3 位 —— 实测登记册 3 位表格行 **89** 条（OP-100~OP-188）、2 位表格行 **99** 条（OP-01~OP-99），合并唯一 **188** 条、缺号 0、重号 0。⇒ 前 99 条编号在**定义侧与引用侧同时失明**，最刺眼的后果是**同一份运行、同一个事实来源，§1 打印「OP 条数 实测 188 OK」而 §7 紧接着打印「登记册已定义 OP 89 条」—— 两个计数在同一次输出里自相矛盾**；更危险的是 §7 那句读起来完全正常的 **「幽灵引用 0 处」**：引用侧实测 3 位口径抓到 80 个编号、2~3 位口径抓到 **126** 个 ⇒ **46 处双位数引用（占全库引用 37%）从未被核对过一次**，而判据每次都说「0 处」（**ISS-115 的对称形态**：那次是「恒真式伪装成判据」，这次是「**恒不命中**」）；尤须记的是，偏巧 WP25 收口时踩到的三次真幽灵（OP-172 / OP-173 / OP-174）**全在双位数编号段** ⇒ 这条判据从建立起就对自己最该盯的那一半数据失明，**它当时报绿不能说明那三次是被它抓住的，只说明它没机会报错**。修法（**不停在改数字，改工具**）：两条正则放开为 `\d{2,3}`（引用侧须带 `(?!\d)` 负向前瞻，否则 4 位以上编号会被截成前 3 位、**造出假编号**）+ 同步 docstring 口径 + **落地前先探针验真**（「3 位定义 vs 3 位引用」与「2~3 位定义 vs 2~3 位引用」两遍对跑，确认放开后新增暴露的**真**幽灵 = 0）+ 新增一条**同源对账判据**：§7 的定义侧取数必须与 §1 的 `OP-\d+` 逐条相等，不等即报红并打印差集 —— 从此两处**读同一份事实，不允许再各说各话**（铁律 14：覆盖范围由人写就必须另加判据对账它）。见 `tools/m1/audit_consistency.py` §7）；**M3 第四包 WP27 开工首日新增 ISS-188**（MAJOR，CLOSED）—— 落 `modules/media/types.py` 时 `__all__` 里手写了**两个模块体从未定义**的名字（`"MAX_IMAGE_PIXELS"`，实为应从 `parsing.parsers.image` 转出；`"REASON_REGENERATION_REASON_CHARS"`，实为 `MAX_REGENERATION_REASON_CHARS` 之误），`import` 期**不报错**（`__all__` 不参与导入解析），只有 `from x import *` 或「`__all__` ⊆ 命名空间」校验才当场炸。全仓普查 **172 个含 `__all__` 的文件 / 2140 个条目，A1 违规 0 例** ⇒ 该类缺陷历史上从未发生、因此从来没有判据；而已挂的 `check_undefined_globals.py` 用 `symtable` 判「名字被引用而模块级未定义」，`__all__` 里是**字符串**、符号表根本看不见（实测 `X=1; __all__=["X","NOPE"]` 的符号表只有 `{'X','__all__'}`）⇒ **结构互补、非重复**。修法 = 新增 `tools/quality/check_all_exports.py`（A1 条目均已绑定 / A2 无重复 / A3 必须字面量「反 ISS-95：扫不到必须报红而非静默跳过」/ A4 活性下界 120 文件·1500 条目）+ `tools/quality/collect_all_exports_reverse_evidence.py`（7 条变异含 2 条成对证明，`--fast`；变异脚本自带**生效断言**与 `sha256` 留痕）挂 CI 门 3；本判据**刻意不建三遍法**第三件套（无「文档声称 ⇄ 实现」可核对，为对称而仪式化 = 空声称），该「不适用」按本仓惯例写进 `ci.yml`。**另注：反向验证自身连踩 3 个坑**（变异未落在真实条目上 ⇒ 假绿；成对证明没隔离变量；`_literal_entries` 有两条 `return None` 只变异一条）——全部属铁律 14「覆盖范围由人写」在**变异脚本作者**身上的复发，已逐条写进脚本 docstring）。**WP27 阶段 4 起步另新增两条判据缺口 ISS-188 / ISS-189（均 MAJOR / CLOSED，均属铁律 14「覆盖范围由人写」）**：**ISS-188** —— 全仓 172 个含 `__all__` 的文件 / 2140 个条目**无任何判据**对账手写公开面，WP27 落 `modules/media/types.py` 时当场自引入 2 例；已实证 `check_undefined_globals`（`symtable` 判「名字被**引用**而模块级未定义」）与本案（`__all__` 里是**字符串不是引用**）是**结构互补、非重复** ⇒ 新增 `tools/quality/check_all_exports.py`（A1 绑定 / A2 无重复 / A3 必须字面量 / A4 活性下界）+ 反向证据挂 CI 门 3。**ISS-189** —— 跨包共用表 `version_nodes` 的「**放行守卫**」无任何判据，WP25/WP26 两次漏掉时**本包全绿、红的是 WP11 的既有用例**（排障者会先怀疑别的包）；四个现有工具在结构上都抓不到（含 WP26 的 C7/C8 成对探针是**运行期**且只覆盖自己那一个触发器）⇒ 新增 `tools/quality/check_node_paired_guard.py`（G1~G7；G5 用**名字约定 vs 行为约定**两个独立来源对账落实铁律 14）+ 反向证据挂 CI 门 3；**M5 WP47 备份恢复 + 回滚演练（本机侧）新增 ISS-192~ISS-197**（6 条：MAJOR 4 / MINOR 2；CLOSED 4 / MITIGATED 2；演练首跑与收口期各抓一半，**全部属"判据自己的取数口径 / 防线声称"一类**）：**ISS-192**（MAJOR，CLOSED）= 备份恢复演练首跑报红**不是数据错了、是比对口径错了** —— PG 的「反解析 → 重解析」把数组字面量的强制转换从整体形式改写为逐元素形式（`::text[]` → `ARRAY[...]::text[]`），**语义等价而文本不等** ⇒ 任何"裸比 `pg_get_constraintdef` 定义文本"的判据必然假红（而假红的代价是逼人削弱判据 —— 第 8 条纪律）；修法 = 比**解析后的语义**而非文本，并同时修掉"`order by proname` 键不唯一"这一处比对歧义。**ISS-193**（MAJOR，MITIGATED）= `check_evidence_scope.py` 的集合 B（用 AST **按行为**反推"谁在写 `evidence/`"）`_module_consts()` **只收模块级赋值** ⇒ "路径经函数局部变量间接引用"的脚本（`check_backup_restore_drill.py` / `check_rollback_drill.py`，均在 `main()` 里定义 `evidence_dir`）被判成"不写证据"（**假阴性**；且因它从未进过 B，D4 的活性下界**看不见**它 ⇒ 静默）；**刻意不修实现** —— 补进去等于把 B 变成**第三套人写名单**（铁律 14 反噬），改为把该形态写进 docstring 的"已知盲区"并指明由 CI 门 3 的 `ci-steps-covered`（按**已声明步骤**双向对账 `ci.yml` ⇄ `REQUIRED_CI_STEPS`）兜住；同时**更正**该 docstring 原文一处过度声明（原文称 D4 能拦住所有反推不到的写法 —— 对局部变量形态并不成立，正是 ISS-95 变体）。**ISS-194**（MAJOR，CLOSED）= `select to_regclass('public.不存在的表')` 返回的是**一行 NULL**而不是零行 ⇒ naive 写法 `bool(fetchall())` **恒真** ⇒ 回滚演练幕 5①"部分 DDL 已回滚"的存在性判据整条恒真（ISS-115；开发期探针 2026-10-07 实测就是这么写错的）；修法 = 判 `row[0] is not None`，并**成对**补阳性对照（真存在必须判 `True`）—— 只有阴性对照时，"反向过度修"（判什么都不存在）同样会让它恒绿。**ISS-195**（MINOR，CLOSED）= `ast.unparse(fn)` 会把函数 **docstring 一起输出** ⇒ "源码里不许出现 X"这类断言被**讲解该坑的那段文案本身**判红（假红）；修法 = 断言前用 `_undoc()` 摘掉 docstring。**ISS-196**（MAJOR，CLOSED）= "整段源码里含 `total != EXPECTED_CHECKS`"是**恒真式** —— `_report()` 的 `return` 语句重复了同一比较 ⇒ 取值必须精确到**那个 `if` 的谓词**，而不是"全段任意位置含该串"（ISS-115 族）。**ISS-197**（MINOR，CLOSED）= 演练脚本把**本机口径当成了通用口径**（基准 DSN 拼 `rednote@localhost:5432`、pg 工具走 Homebrew 路径）⇒ 换台机器 / 上 CI 必红；修法 = 基准 DSN **只换库名**地派生自环境变量 + 三级回退 `REDNOTE_PG_BIN` → `which pg_dump` → 默认，且回退知识**只许存在一份**（`backup_pg` 委托 `drill_db.pg_bin`）。**收口期另发现 ISS-198**（MAJOR，CLOSED）= `audit_consistency.py::_README_DECL_PATTERNS["ISS 条数"]` 的**第一条正则恒不命中**（`全部\s*(\d+)\s*条开发过程问题记录`，而 README 实际写"全部 188 条**待定项** + 197 条开发过程问题记录"），于是"多处声明对账"实际只剩看板行**一个来源** —— 与 ISS-185 / ISS-186 / ISS-187 同族（**判据自己的取数口径坏了，它的绿与红都不足采信**）；修法 = 正则改锚真实句式 + 新增**活性自检**（每条来源正则必须至少命中一次，否则判据自身报红）；**WP47 收口期发现 ISS-199**（MAJOR，OPEN）= `ci.yml` 引用的演练脚本**从未入库**（`5f43770` 把 backup-restore drill 步骤提交进 `ci.yml`，而 `tools/quality/check_backup_restore_drill.py` 从未提交）⇒ **干净检出跑门 3 会在那一步 `python: cannot open file` 直接失败，而本机全绿**（工作树里有文件）；病根是本文件所有判据读的都是**工作树**而 **CI 读的是版本库**，二者只在「有未跟踪文件」时分叉且无人发现 —— 铁律第 14 条从「名单由人写」挪到了「**提交哪些文件由人挑**」；修法 = 新增第 53 条判据 `ci-steps-tracked`（对 `_ci_tool_steps()` 得到的每个路径先判存在、再判 `git ls-files --error-unmatch` 入库，无 `.git` 时降级**并出声**），与 `ci-steps-covered` 是**两个独立事实**（一个查名字登记、一个查文件真进版本库）；**M5/WP47 补 §4 盲区时发现 ISS-200**（MAJOR，CLOSED）= `audit_consistency.py::check_declared_evidence_exists`（§4）的**三个正则全部以 `\.log` 结尾** ⇒ **`.json` / `.jsonl` 证据零覆盖**，而输出照旧打印「文档声明的证据文件 85 处 / 不存在 0 处」（**与「判过」完全同形**）；实测 `docs/m5/WP47-演练记录.md` §6 正式引用的 8 个 `evidence/m5/*.json` **一处都没被扫过**，扩到 `log/json/jsonl` 后同一份工作树立刻变成 **97 处**（`.log` 85 / `.json` 11 / `.jsonl` 1）—— 差的这 12 处正是「从来没被看过」的部分；这是 **ISS-186 同一个函数的第二个现身面**（186 是「换排版就抓不到」，本条是「换后缀就抓不到」），病根仍是铁律第 14 条（覆盖范围由人写的字面形状决定，缩窄是静默的）；**更隐蔽的一层**：扩展首版的自测**遍历后缀白名单断言命中** ⇒ 白名单一缩、自测跟着缩（变异实测退回 `("log",)` 时自测全程静默、报红 0 条），等于把铁律 14 的病原样搬进了「判据的判据」里；修法 = 白名单显式化 + 结尾边界 `(?![A-Za-z0-9_.\-])`（防 `x.jsonl` 被读成 `x.json` 而凭空造出缺失路径）+ 机械豁免表（`md` 3 处属 M0「将要产出」、`png` 1 处为通配）+ 自测期望集合**改由真实文档派生**并另设白名单完备性对账 + 分写两条不变式改用**合成样本**断言 + 新建反向验证脚本 `tools/quality/collect_declared_evidence_reverse_evidence.py`（13 条 check / 沙箱里真树全程只读 / 9 个变异全部被抓住） |；**WP47① 新增 ISS-201~ISS-205**（3 MAJOR + 2 MINOR：`get_request_id_for_logging` **悬空复发** —— ISS-95 反向形态「声称自己不是摆设」 / `ensure_metrics_installed` **先查后建非原子** ⇒ `/metrics` 偶发 500、Prometheus `up=0` / 日志级别设在 **root** ⇒ 默认打开 SQL 绑定参数、内网 URL、全量 access log / 证据依赖读数被 `uvicorn[standard]` 的 `]` 截断而**把错的数字存档** / 等价断言把易变测量值 `latencyMs` 当身份 ⇒ 偶发假红） —— 其中**并发判据的形态教训**值得单列：判据**不能是「复现它」**（首版两线程 + `Barrier` 反向验证失败；挪进竞态窗口又会让判据**把正确实现 deadlock 死**），必须是「**断言那条不变量**」（`threading.Lock.locked()` 是可查询的事实），且「**范围对 ≠ 互斥真**」须成对断言 ；**WP27 收口期新增 ISS-207**（MAJOR，**OPEN**）= `audit_consistency.py` §5 证据时效判据把「**未提交的在途改动**」也算作被证据代码 —— 取的是 `git log -1` 与未提交文件 mtime 的**较大者**，判据分不清「谁改的」、只认「工作树里有没有变」⇒ 共享工作树里**只要对方还在改代码，我的证据就永久判陈旧**：本轮实测 `backend-pytest.log` 生成于 09:58:41，而对方 11:09:43 仍在写 `test_observability.py` ⇒ 差 4262 秒报红，且**无法靠重跑消除**（重跑期间对方仍在改，且会掩盖真实状态），唯一正确动作是等对方提交 —— 而判据没给出这条出路。本轮按「如实记录、不代修、不顺手重跑」处理（遗留红写进纯证据提交 `2bdac1a` 正文），缺陷登记为 OPEN，候选修法 A/B/C 待「谁该负责」定案后择一（见 jsonl）。**教训**：这是判据纪律第 8 条（mtime 不能作归属依据）的**判据侧**形态 —— mtime 不可作归属依据，那么把 mtime 当判据输入的判据也不可信；判据读到「未提交改动」时须问「这是谁的改动」，**答不出就不该报 FAIL**；**ISS-208**（MAJOR，CLOSED）= §2b 反向验证脚本 `collect_readme_decl_anchor_reverse_evidence.py` 把基线**写死成常量**（`193` / `205` + 12 处期望值），而 ISS-207 收口把 README 计数改到 196/207 后 `str.replace` 打空 ⇒ 抛 `AssertionError: R③ 锚点未命中`，且该抛出**把 R0/R①/R② 已判出的红一并吞掉**（实测只见一句 AssertionError，看不到 R0 期望 [193] 实得 [196]）—— 属 ISS-171 教训的**第 8 次**现身（判据的参照值与被验对象是两个会独立演化的东西），且是 ISS-143 的新面：**「崩了」与「全判错」在读证据时极易被读成同一个结论**。修法 = 基线全部从真声明行派生（新增 `_DECL_ROW_RE` + `_baseline_numbers()`，看板行锚点也改成 `f'{iss_n} 条 / 0 error'`）+ 三处「变异未生效」从 `assert` 抛出改为**记红并早退**（`[0]` 前置 / `[0b]` 派生 / `[4]` R③ / `[5]` R④，节号序列恒定）⇒ 四条变异（派生退回硬编码 / 派生入口失效 / R③ 锚点打空 / 真声明行不存在）实测**全部以可读形态记红、0 处裸崩栈**，基线仍 `RESULT=PASS`；同包顺带修 `_load_audit` 漏注册 `sys.modules`（ISS-164 同型，判据 F 报红 3 处 → 2 处，剩下 2 处非本包）；**ISS-209**（MAJOR，OPEN）= `log_issue.py` **只有 `--new` 追加、没有编辑既有条目的入口** ⇒ 为 ISS-207 取证时**否证了我自己写进 resolution 的两条理由**（修法 A 说「证据↔文件映射目前无」—— 实测 `audit_consistency._EVIDENCE_SOURCES` 早已存在，只是粒度是目录；修法 B 说「包归属可由 `SCOPE_PATTERNS` 派生」—— 实测那是 `tools/**/collect_*` 三条 glob、纯脚本名维度，与包号无关），却**改不进去** ⇒ 回填只能① 手改 jsonl（绕过编号分配与渲染纪律）② 写进别的文档（错误结论继续被 `--check` 判绿、被§4.3 渲染、被审计引用）。根因 = CLI 只实现「追加」与「派生视图」，缺最基本的「按 id 更新」⇒ **append-only 的登记册必须显式提供「更正」这个动作**，否则更正只会以「绕过登记工具」的方式发生，且不留痕、不被 `--check` 覆盖。教训另两条：写 ISS 时把「候选方案」连同**其成立的理由**一起写进去等于给自己埋雷（候选被否证后，理由就成了错误结论的一部分，而它当时是真心的）⇒ 须标明「理由待验证」。**ISS-207 的取证结论（已推翻其原候选理由）**：修法 A/B 的盲区实测为 **11 个未提交文件**（现有映射只罩住 6 个，且 `ci.yml` / `tools/**` 全部罩不住）；修法 C（保持 FAIL + 输出指明归属）成本最低且**不引入新的名单**，仍是唯一不违反铁律 14 的选项；**工程债清偿包 WP-DEBT-05「测试库数据体量」新增 ISS-223 / ISS-224**（**ISS-223** = `projects`/`users`/`brands` 三张"无主根表"被全仓几十张表反向 FK 引用，`conftest.py` 的 `drop_own_tables` 闭包算法因此把它们排除在删除范围外、从未被任何模块重建，21 个测试文件各自建种子数据却无人清理 ⇒ 共享库 `rednote_test` 从 2026-09-30 到 2026-10-18（18 天）线性累积到 116,822/166,099/117,875 行，判据 B 在共享库上跑全量 pytest 因此超过 3600s 超时（私有干净库同一判据只需 172s / 2333 passed）；修法 = 新增判据 D `check_test_data_volume.py`（阈值由私有库两轮实测 × 安全倍数 4 机械派生，只测三张根表、不测闭包全表——根表行数天然上界住整条派生链）+ 手动重置工具 `tools/dev/reset_shared_test_db.py`（复用迁出的 `rednote.platform.db_graph.truncate_targets` 闭包，单条多表 TRUNCATE 不用 CASCADE，执行前先 `check_shared_db_quiet.assert_quiet` 探活）；反向验证 14/14、Code Review 16/16。**ISS-224** = WP-DEBT-02 文档 §8 已知边界声称"数据残留由 `drop_own_tables` 覆盖"，对这三张无主根表不成立——该声明写于收口当时未逐表核对，属 ISS-95 形态（声称有防线实际没有）；已改文档交叉引用本包。见 `docs/m3/工程债-测试库数据体量说明.md`） |

## M0 出口（G0）

见 `docs/m0/G0-检查点清单.md`。未过 G0 不得进 M1 业务功能开发。
