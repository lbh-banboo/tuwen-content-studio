# WP18 Brief 结构化解构说明

> 研发计划 04 §4.3：**WP18** Brief结构Schema；解析与加厚；原文对照审核。
> 依赖 WP16（模型网关）、WP17（解析）、WP11（版本注册）。
> 验收：**分类来源、字段疑问、确认版本闭环**。
> 上游依据：数据结构 02 §5/§6、协议 03 §5/§5.1、需求说明 §5.1/§5.2/§5.3、
> `docs/m1/WP11-版本注册与引用说明.md`、`docs/m1/WP17-文档解析与OCR说明.md`、
> `docs/m2/M2并行开发接缝冻结.md`。
>
> **本文档只写 §1~§3**（阶段 1~3）。§4~§10（实现 / Review / 测试 / 简化 /
> 待核实 / 关联）由主 agent 收口时补。

---

## 1. 阶段 1：需求追问（superpower-workflow 强制 8 问）

### 1.1 这个需求真正要解决什么问题？

**把"AI 从一份 Brief 文档里读出来的东西"变成"每条都能被回答『你凭什么这么说』，
每条不确定都被留成疑问，且确认过的版本不能回头改"的结构。**

三个定语一个都不能省：

- **每条都能被回答"凭什么"** —— 验收原文之一是「分类来源」。一条结论若是
  `SOURCE_FACT`（原文事实），它必须能指回原文的哪一段；若是 `EXTERNAL_EVIDENCE`，
  它必须带一个来源 URL；若是模型自己想的，它只能叫 `STRATEGY`（策略建议）
  或 `UNCONFIRMED`（待确认）。**没有第四种"看起来像事实但说不清来源"的档位** ——
  而 LLM 的默认输出恰好大量落在这个档位上。
- **每条不确定都被留成疑问** —— 验收原文之二是「字段疑问」。需求 §5.1 原文：
  "缺失字段必须显示'待补充'，**不得由 AI 填成确定事实**"。
  这句话要落地，就不能只靠 prompt 里写一句"不确定就不要填"：
  必须让"缺失"变成一个**有地方存、有地方看、有地方回答**的对象。
- **确认过的版本不能回头改** —— 验收原文之三是「确认版本闭环」。需求 §5.3 末段：
  "新上传 Brief 形成新版本，不覆盖旧版"。一条 `PENDING` 的结论被"确认"之后，
  这个确认动作必须留下**谁、什么时候、基于哪一版**的痕迹；
  而它不能是"把那一行的 `PENDING` 改成 `CONFIRMED`"—— 那会让
  "这一版当初被确认过吗"取决于有没有人改过它，**没有人能审计这件事**。

**本包真正的难点不是"怎么让模型输出 JSON"**（那是提示词工程与网关的事），
而是 **"怎么让『编造』在结构上无法通过"**。因为编造的代价不是"这一条错了"，
而是下游（WP22 信息草案 / WP26 文案 / WP39 交付）会把它当成**已确认的产品事实**
写进要发给客户的内容里 —— 那是一次不可撤回的错误。

### 1.2 核心用户是谁？

| 用户 | 何时用 | 关心什么 |
|---|---|---|
| **WP19 研究关键词**（下游代码） | 步骤 1 固化后 | 目标人群、痛点、卖点（用来生成研究关键词与提示词） |
| **WP22 信息草案**（下游代码） | 步骤 3 | `structured_fields` + **每条 claim 的分类与来源** + 带着哪些疑问 |
| **WP26/27 内容生成**（下游代码） | 步骤 4~7 | 必提/禁提项（违规红线）、卖点优先级、适用型号 |
| **运营（前端）** | 上传 Brief 后 | 字段填了没、**哪些字段是"待补充"**、原文在哪一页 |
| **审核人** | 审核 Brief 时 | 这条结论的依据是哪一段；这一版还差什么才能推进 |
| **AI（模型）** | 解构 / 加厚时 | 字段规格（问哪些）、允许引用的来源片段（`allowedSourceIds`） |

**"哪些字段是待补充"是一等公民**（与 WP17 的"哪些页没解出来"同一条判据）：
它是"步骤 1 能不能推进"的直接依据，也是一份材料不足的 Brief 能被诚实交付的唯一形态。

### 1.3 最小 MVP 应包含哪些功能？

1. 五张表（`briefs` / `brief_drafts` / `brief_versions` / `brief_claims` / `brief_issues`）
   与 DDL 草案；
2. 五个端点（协议 §5 前五行，一个不多一个不少）；
3. **BriefSchema**（需求 §5.1 的 10 组共 18 个字段，含"必填 / 多值"标记）；
4. 解构通道（**可注入 Protocol**，默认"不可用即抛"）；加厚通道（**建议不自动生效**）；
5. **`state='PARTIAL'` 的三层显式处理**（硬约束，见 §1.4 第 1 条）；
6. 分类与证据的**数据库级**约束（`SOURCE_FACT`/`EXTERNAL_EVIDENCE` 必须有来源）；
7. **疑问的阻断闭环**（未答复的 `BLOCK` 拒绝固化，`WARN` 允许带着走）；
8. 固化事务：注册表 + 子表 + `briefs.active_version_id` **同一事务**，
   并把 **OP-88 的延迟一致性检查闭合**（接缝冻结 §5.1 明写"由本批负责实现"）；
9. `GET /brief` 的**原文对照最小充分集**（本版 claims 引用到的片段文本 + 定位）。

### 1.4 描述有哪些疏漏应当补充？理由是什么？

| # | 疏漏 | 补什么 | 理由 |
|---|---|---|---|
| 1 | 数据结构 02 §6 只给了 `source_state`-类的语义，**没说 WP17 的 `PARTIAL` 怎么办** | `source_state` 受控枚举（`PARTIAL` **独立于** `SUCCEEDED`）+ 草稿 CHECK（`PARTIAL` 必须有疑问）+ 版本延迟触发器（`PARTIAL` 版本必须有 `brief_issues` 行） | WP17 对下游的契约原文是"必须显式处理 `PARTIAL`，它不是 `SUCCEEDED` 的变体"。而"处理"的**可验证形态**只有一种：残缺必须留下可查的记录。三层都做，是因为草稿会被下一轮编辑覆盖、而版本不可变 —— 只做草稿层等于**固化时把残缺洗白** |
| 2 | 需求 §5.1 的"必须覆盖 10 组"**没有给出字段路径、必填性、单值/多值** | `schema.BRIEF_FIELDS`：18 个 `FieldSpec(path/label/group/required/multi)` | 这是本包最真实的规格缺口。没有路径，`brief_claims.field_path` 就只能存自由文本（前端点不中）；没有必填性，"必填字段已填写或明确不适用"这条完成条件**无法判定**。本包的必填判据是"缺了它，下一步产出必然是编造或违规"（5 个） |
| 3 | `brief_claims.evidence_segment_id`（**单值**）与协议 §5.1 的 `evidence: Array` 冲突 | 按**数据结构**落单值列；协议层组装成单元素数组（`service._claim_evidence`） | 一处真实的规格不一致。数据结构的列就是表结构（不可违背），但**"一条结论多个来源"若确有必要，那是一次 schema 变更，不是在协议层悄悄扩展形状** —— 悄悄扩展会让 `brief_claims` 永远装不下协议承诺的东西 |
| 4 | `resolution_status`（数据结构）与 `resolution`（协议 §5.1）**同义不同名** | 落库用数据结构的名字；协议层映射（`service._claim_view`，**只有一处**） | 两个名字各有权威来源（一个管表、一个管接口）。映射散在多处会让某个字段在某条路径上漏映射 —— 前端拿到 `undefined`，不报错 |
| 5 | 数据结构 02 §6 的 `brief_drafts.issue_resolutions` 名义上是"id → 处置"的映射，**但草稿阶段疑问还没有 `brief_issues` 行** | 存**完整疑问对象数组**（含 `severity`/`description`/`state`/`resolution`） | 映射形式会让"这条疑问是什么"只存在于别处，而草稿阶段**别处还没有它**。存完整对象让"疑问从产生到处置"在同一处可读 |
| 6 | 协议说"初始空草稿由**项目创建事务**创建"，但项目创建是 WP10 的代码 | 幂等的**懒创建**（`service.ensure_brief`，`GET /brief` 首次调用时建） | 本包无权改 WP10。懒创建满足协议"不要求客户端猜测一个不存在资源的版本"。**代价已登记**：`GET` 会写库，违反"GET 是安全方法" |
| 7 | 协议 §5 说 `GET /brief` 返回"原文链接"，**但没有全文浏览端点** | 返回 `evidenceSegments`（**只含被引用的片段**）+ `source`（文件 id 供下载票据） | 一份 200 页 PDF 有上万个片段，全量返回会让响应体失控；而"原文对照审核"真正需要的是**这条结论对应的那一段**。全文浏览是另一个界面（另需一个协议里没有的端点，已登记） |
| 8 | `brief_issues.project_id` 是**冗余列**（可由版本推出），没说怎么防止漂移 | 延迟触发器校验它等于"版本所属 Brief 的 project_id" | 停下不校验，它会漂移成"两个互相矛盾的答案"；而"核心疑问未解决阻断步骤 1"这条判断**正是按它来查的** —— 漂移方向恰好是"漏掉某个项目的阻断疑问" |
| 9 | 没说"只有 `evidence_segment_id` 非空"算不算有来源 | **假溯源防护**：延迟触发器校验片段属于**本版本那次解析**（`source_segments.parse_run_id`） | 一个指向别的项目/别的文件的片段 id 同样能满足"有来源"，而它**比没有来源更危险** —— 它看起来可追溯，人工审核不会去怀疑它 |
| 10 | 没说出网策略否决 / 模型未配时的行为 | **显式失败**（`DecomposerUnavailable` → `FAILED` + 错误码），不产出任何草稿变更 | 与 WP17"OCR 不可用即显式失败"同一判据。返回一个"看起来成功"的空结果，会让一个忘配模型的部署产出字段全空的 Brief 而步骤 1 一路绿灯 |
| 11 | 没说加厚参数（`briefVersionId` + `allowedSourceIds`）落在哪 | 落在 `brief_drafts.enrichment_request`（jsonb，可空，**读完即清空**） | 三个候选都不行：`jobs.result_ref` 是**结果**引用（被输入占住后放不下产出）；`jobs.input_version_ids` 只装 `version_nodes` 的 id（装不下 `source_segments` 的 id）；新建表会自创数据结构里没有的表 |
| 12 | 没说加厚的产出**能不能自动变成事实** | 建议写 `brief_drafts.suggestions`，**不写 `claims`**；采纳必须在 `PUT /brief-draft` 里显式加进 `claims`（走 `ClaimDraft` 校验） | 协议 §5 第 5 行原文"返回分类建议，**不自动生效**"。混进 `claims` 之后运营再也分不清"这条是原文里的"还是"模型想出来的" |

### 1.5 哪些功能现在不该做？

| 不做 | 为什么 |
|---|---|
| 真实模型接入（`GatewayDecomposer` 的端到端） | 需要**已审批的 `model_profiles`** + 出网策略 + 真实适配器。本机三样都没有（OP-130）。本包交付 Protocol + 不可用实现 + 测试用确定性实现，**不让"链路已验证"变成假话** |
| 字段级来源数组（协议 §5.1 的 `evidence: Array` 多条） | 数据结构 02 §6 只给了单值列。扩成数组是一次 schema 变更，**不能由实现顺手扩**（见 §1.4 第 3 条） |
| 外部证据 URL 的**可验证性**（真的去访问那个 URL，核验内容与结论一致） | 需要出网 + 内容抽取 + 语义比对，且内网出网策略未定。本包只**保存** URL 并要求非空（已登记） |
| `brief_claims` 的**影响分析**（这条结论变了，哪些内容受影响） | 属 WP34（影响分析与重生成）。本包建好 `ix_brief_claims_evidence_segment_id` 反查索引，不做分析 |
| 全文原文对照视图（整篇文档 ↔ 整份结构） | 需要协议里没有的端点。本包只返回被引用的片段（见 §1.4 第 7 条） |
| 旧 Brief 版本的**归档 / 清理** | 数据结构 §15 的保留策略尚未定（登记册 §3）。不建死代码 |
| `brief_versions` 的**审核事件**（`review_events` 的 `target_version_id`） | 属 WP23（审核与批准流）。本包只保证版本可被审核（不可变 + 有 ETag） |
| 迁移文件 | 接缝冻结 §4：迁移是**唯一仲裁资源**，由主 agent 串行落成。本包只提交 DDL 草案 |
| 前端"原文对照"页面 | 属前端工作包。本包只保证 `GET /brief` 给出对照所需的一切 |

### 1.6 这个系统有哪些技术风险？

| # | 风险 | 等级 | 应对 |
|---|---|---|---|
| R1 | **模型把编造内容标成 `SOURCE_FACT`** | 🔴 高 | 两层：`ClaimDraft.__post_init__`（可读错误）+ `ck_brief_claims_factual_category_requires_evidence`（数据库层，绕过 service 也进不去）。**再加一层**：证据片段必须属于本版本文档那次解析（假溯源触发器） |
| R2 | **`PARTIAL` 被当成完整输入** | 🔴 高 | 三层落地（§1.4 第 1 条）。**不允许**"PARTIAL 就禁止固化" —— 那会让缺页的 Brief 永远推不动，运营唯一的出路是伪造内容；本包的口径是"允许固化，但残缺必须被记录且可查" |
| R3 | **运营的工作被重新解构静默覆盖** | 🟠 中 | `draft_has_unsolidified_edits` 判定 → 409，`details.reason` 说明。**不做"合并"**：任何合并策略在"模型填 A → 运营改 B → 重新解构又填 A"时都会给出意外结果，"拒绝并说清楚"更负责 |
| R4 | **两个项目同时有待处理加厚请求时互相消费** | 🟠 中 | `list_drafts_with_pending_enrichment` 加 `project_id` 过滤（join `briefs`），处理器从 job 取 `project_id` 传入。**这是本包实测发现并修复的真实缺陷**：加厚请求存在草稿行上，而草稿行本身不带项目（项目在 `briefs` 上），全局扫描会让 A 项目的 Worker 消费掉 B 项目的请求 |
| R5 | **固化事务的两行（节点 / 子表）插入顺序不确定** | 🟠 中 | 三处 `DEFERRABLE INITIALLY DEFERRED`（`brief_versions.id` → `version_nodes.id`、`briefs.active_version_id`、`briefs.draft_id`）+ 四个延迟 CONSTRAINT TRIGGER。理由：`register_version` 内部用 SAVEPOINT + 重试取号，**不暴露"节点行何时落库"的控制权** |
| R6 | **OP-88 的反向判据误伤其它工作包** | 🟠 中 | 反向触发器（`trg_version_nodes_brief_paired`）挂在**共用表** `version_nodes` 上，用 `WHEN (NEW.entity_type = 'BRIEF')` 限定；且**有意收窄**为"只对 `briefs` 表里真实存在的 `entity_id` 强制"。收窄的代价（悬空节点不被拦）已登记为 OP |
| R7 | **`BRIEF_PARSE` 一个任务里既解析又解构 → 大文件超租约** | 🟠 中 | `execute_parse` 幂等（终态时读回），因此"复用已有解析结果"天然走通；**不调** `LEASE_DURATION_SECONDS`（那是 WP13 的资产）。风险显式登记：**大文件解构需要 Worker 续租** |
| R8 | **不可变表导致测试残留清不掉** | 🟡 低 | 探针放"单事务 + SAVEPOINT + 整体 rollback"（WP16 OP-134 的同一手法） |
| R9 | **`GET /brief` 会写库**（懒建草稿） | 🟡 低 | 幂等（两个 UQ 兜底）。收口方向：主 agent 在 WP10 项目创建事务里补一次 `ensure_brief`，那时退化成纯读（已登记） |
| R10 | **`revision_no=None` 不是幂等的**（OP-91） | 🟡 低 | 本包在固化**之前**用内容摘要挡掉"内容没变"的重复提交（`draft_unchanged` 409），于是"点两次固化按钮"不会产生两个同内容版本 —— 这是本模块在**自己这一层**能做的缓解 |

### 1.7 数据结构、接口、前端、后端怎么拆？

```
modules/brief/
  types.py        受控取值域 + 值类型（ClaimDraft / IssueDraft / DecompositionResult
                  / EnrichmentSuggestion / EnrichmentResult）
  schema.py       BriefSchema：18 个 FieldSpec（需求 §5.1 的 10 组）+ 归一化 + 校验
  models.py       5 张表 + 全部 CHECK/UQ/索引（含 2 个部分索引）
  ddl.py          7 个触发器（3 不可变 + 4 跨表一致性，含 OP-88 的两个）
  repository.py   存储访问（草稿的条件 UPDATE、版本/claims/issues 的批量插入）
  decomposer.py   BriefDecomposer Protocol + Unavailable / Scripted / Gateway 三实现
  service.py      业务规则（**唯一对外入口**）：懒建、解构、改草稿、固化、加厚、组装视图
  handlers.py     WP13 的两个任务处理器（BRIEF_PARSE / BRIEF_ENRICH）
  api.py          五个端点（协议 §5 前五行）
  dependencies.py 四个仓库依赖（**刻意不提供** VersionRepoDep / DecomposerDep）
```

**端点只有 5 个**（协议 §5 前五行）。查询复用既有能力而不是新开端点：
原文下载用 WP12 的 `POST /files/{id}/download-tickets`；解析状态用 WP17 的
`GET /files/{fileId}`；版本 ETag 用 WP11 的 `version_nodes.payload_hash`。

**分层是单向的**（`types ← schema / decomposer / models / repository / service / api`）：
定义在 service 里会让其余几个反向 import service。

### 1.8 至少输出 3 个版本方案

见 §3（给出 4 套并含推荐）。

---

## 2. 阶段 2：方案设计

### 2.1 需求本质（一两句话）

**把"AI 读出来的东西"变成"每条都能回答『凭什么』、每条不确定都被留成疑问、
且确认过的版本不能回头改"的结构。**

难点不在提示词，而在**让"编造"与"无声的残缺"在结构上无法通过** ——
这两件事恰好是 LLM 产物最自然的形态（它倾向于给出确定语气；
它不会主动说"这份文档第 7 页我没读到"）。

### 2.2 用户画像

- **WP19 / WP22 / WP26**：只读消费方。需要
  `(field_path, value, status)`、`(claim_text, category, evidence, resolution)`、
  `(issue: severity, state, description)`。**它们必须把 `category` 当门槛而不是装饰**：
  `STRATEGY` 不能当事实用，`UNCONFIRMED` 不能当可发布内容用。
- **运营**：需要"这 5 个必填字段里还差 2 个"、"这 3 条结论还没有来源"、
  "第 7、12 页没解出来"这样的**可行动**信息。
- **审核人**：需要"这条结论凭什么" → 点一下能看到原文那一段；
  需要"这一版还差什么才能推进" → 未答复的 `BLOCK` 疑问列表。

### 2.3 核心功能清单（MVP）

同 §1.3 的 9 项。

### 2.4 待补充项及理由

见 §1.4 的 12 条与 §1.6 的 R7/R9/R10（均需在 §9 汇总为 `[待核实]` 项）。

### 2.5 砍掉清单

见 §1.5。

### 2.6 技术风险评估（含应对）

见 §1.6。

---

## 3. 阶段 3：多版本方案

### 方案 V1 —— 五张表 + 草稿读写，模型不接（**否决**）

- **做什么**：建五张表、通 `GET /brief` 与 `PUT /brief-draft`，
  字段靠运营手工从 PDF 里抄进表单。claims / issues 表建了但没人写。
- **优点**：一天能完，契约立刻能联调，前端可以先做起来。
- **缺点**：**三条验收项一条都没有落点**。「分类来源」没有来源可分类
  （`category` 永远是空列表）；「字段疑问」没有产生者（解构不存在，
  疑问从哪来）；「确认版本闭环」的闭环入口（`PUT` 改 `resolution`）
  改的是别人手抄进去的东西 —— 那不是"确认"，那是"录入"。
- **适合场景**：无。它连"Brief 结构化解构"这个名字都没兑现
  （没有"解构"，只有"表单"）。
- **风险**：**最危险的一种交付** —— 表建了、端点通了、测试能绿，
  但验收项一个都没真正达成。WP12 的 MinIO 分支教训
  （"未经验证的实现比明确未实现更危险"）会在这里第三次重演。

### 方案 V2 —— 全做，但"分类 + 证据"由服务层校验（**不推荐**）

- **做什么**：解构通道齐、加厚通道齐、五个端点齐。但
  "`SOURCE_FACT` 必须有来源"这条规则只写在 `service.py` 的一个 `if` 里。
- **优点**：实现直观，规则集中在一处，改起来方便。
- **缺点**：**"不得编造"退回成纪律要求**。绕过 service 的写入路径至少有四条：
  数据回填脚本、手工 SQL 修数据、将来某个新的批量导入端点、
  以及"某天有人重构 `update_draft` 时移动了那个 `if` 的位置"。
  每一条都能写进一条没有来源的 `SOURCE_FACT`，而**数据库不会拦**。
  更糟的是：**假溯源**（`evidence_segment_id` 指向别的文件的片段）
  在服务层根本判不了 —— 那需要一次跨表查询，而它不属于"校验一个字段非空"。
- **适合场景**：一次性脚本、无下游依赖的内部工具。
- **风险**：静默的数据污染，发现时下游已经基于它产出了发给客户的内容。

### 方案 V3 —— 分类/证据/残缺全部沉到数据库层 + 疑问阻断闭环（**推荐**）

- **做什么**：
  1. **分类与证据两层**：`ClaimDraft.__post_init__` 给可读的领域错误
     （测试能断言文案）+ `ck_brief_claims_factual_category_requires_evidence`
     管住绕过构造器的写入。两边同一判据。
  2. **假溯源防护**：延迟 `CONSTRAINT TRIGGER`
     （`trg_brief_claims_evidence_scope`）校验
     `evidence_segment_id` 属于本版本 `source_parse_run_id` 那次解析
     产出的片段集。
  3. **`PARTIAL` 三层**：受控枚举（`PARTIAL` 独立于 `SUCCEEDED`）
     + 草稿 CHECK（`PARTIAL` 时 `issue_resolutions` 必须非空）
     + 版本延迟触发器（`PARTIAL` 版本必须有 `brief_issues` 行）。
     因此**固化不会把残缺洗白**。
  4. **疑问阻断两层**：`service.solidify` 报 409 列出未答复的 `BLOCK`；
     `ck_brief_issues_blocking_issue_cannot_be_open` 在数据库层兜住
     "带着未答复核心疑问的版本被写进去"。第二层防的是**服务层回归**。
  5. **确认必须有人**：`ck_brief_claims_confirmed_requires_actor` ——
     一条无人确认的 `CONFIRMED` 与 `PENDING` 在语义上没有区别，
     只是前者看起来更可信。
  6. **OP-88 闭合**：三个判据（节点存在 / `entity_type='BRIEF'` /
     `entity_id = brief_id`）+ 反向"禁止孤立节点"，
     全部 `DEFERRABLE INITIALLY DEFERRED`。
  7. **固化即冻结**：`brief_versions` / `brief_claims` / `brief_issues`
     三张表共用 `platform.immutability` 的 `rednote_reject_mutation()`
     —— 要改结论就出新版本。
  8. 加厚建议走 `suggestions`，**不自动生效**。
- **优点**：
  - 三条验收项各有**结构性保证**（不靠自觉、不靠测试覆盖运气）；
  - "编造"与"假溯源"在**数据库层**被拦 —— 绕过 service 也进不去；
  - "残缺被洗白"在**固化事务的提交时刻**被拦 —— 而草稿会被覆盖，
    版本不会；
  - 测试可以在本机用 `ScriptedDecomposer` 真的走通"分类来源 → 字段疑问 →
    确认版本闭环"全过程（含 PARTIAL 与假溯源的反例）。
- **缺点**：不变量分散在三处（构造器 / CHECK / 触发器），
  改动时要同时想清楚"这条判据属于哪一层"；
  延迟触发器在事务提交时才报错，错误定位比即时 CHECK 稍难
  （应对：错误消息里带 `id` 与具体原因）。
- **适合场景**：本项目（有下游依赖、有验收项、有不可逆的数据沉淀）。
- **风险**：反向触发器挂在**共用表** `version_nodes` 上 ——
  若 `WHEN` 子句不可用（PG 版本差异），它会对全部 8 类实体生效，
  直接越界。**本机实测 PG 17.10 支持**（`CREATE CONSTRAINT TRIGGER` 上的 `WHEN`），
  并写了测试锁定它。

### 方案 V4 —— V3 之上再加：字段级多来源 + 影响分析 + 全文对照 + Embedding 预筛（**不选**）

- **做什么**：`evidence` 扩成数组（协议 §5.1 的原样）；claim 变更时自动算出
  受影响的内容版本；`GET /brief` 返回全文与完整对照；解构前用 embedding
  预筛片段以省 token。
- **优点**：更接近"生产级"，前端体验更好，长文档更省调用。
- **缺点**：
  - 多来源需要**改数据结构**（一次真实的 schema 变更 + 协议同步），
    不是实现能顺手做的决定；
  - 影响分析属 WP34，需要 `version_dependencies` 的完整图 ——
    本批只有 `BRIEF` 与 `KNOWLEDGE` 两类实体落地，图是残缺的，
    现在算出的"影响"会**漏报**，而漏报比不报更危险；
  - 全文对照会让 `GET /brief` 的响应体随文档页数线性增长
    （200 页 PDF 上万个片段）；
  - Embedding 预筛需要 WP21 的切块与向量库，而它不在本批。
- **适合场景**：WP21/WP34 落地之后、有真实长文档负载数据时。
- **风险**：现在做会在没有真实负载与完整依赖图的情况下把形状定死。

### 另外两个被考虑但未列为方案的方向

| 方向 | 为什么不做 |
|---|---|
| **把 `brief_drafts` 的 claims/issues 拆成独立表**（草稿阶段就落库） | 草稿是"可变工作区"（数据结构 02 §6 原文）。落库意味着每次编辑都在不可变历史的旁边制造一堆带 id 的可变行，而"固化"就退化成"把草稿行翻个标记" —— 于是"确认版本闭环"不再需要新版本，与需求 §5.3 冲突 |
| **用事件溯源表达确认**（`review_events` 风格的追加流，当前状态是投影） | 数据结构 02 §5 确实把审核做成了事件。但**审核是事件**与**Brief 的确认**是两件事：后者是版本内容的一部分（"这一版当时确认了哪些"），它已经由"不可变版本 + 逐条 `resolution` 快照"表达了。再叠一层事件流会让"这一版确认过吗"有两个答案 |

### 选择与理由

**选 V3**。理由与本项目前十二轮一致：**验收项必须落在结构上，不落在纪律上**。

三条验收项逐条对照：

| 验收原文 | V3 的落点 |
|---|---|
| **分类来源** | `category` 非空 + 受控枚举（CHECK）；事实类必须有来源（CHECK + 构造器）；来源必须真的是这份文档的（延迟触发器，防假溯源） |
| **字段疑问** | `PARTIAL` 逐 unit 一条 `WARN` 疑问（三层落地）；未答复的 `BLOCK` 阻断固化（服务层 409 + 数据库 CHECK）；疑问的处置必须填理由并记人（`IssueDraft` + `_apply_issue_resolutions` 服务端填 `resolvedBy`） |
| **确认版本闭环** | 固化 = 注册表 + 子表 + `active_version_id` **同一事务**；`brief_versions`/`brief_claims`/`brief_issues` 三张表不可变；`draft_unchanged` 挡掉空固化；OP-88 在提交时校验节点与子表一致 |

V1 的问题是**没有解构**（名字都没兑现）；V2 的问题是**把"不编造"交给纪律**
（绕过 service 的路径全开，且假溯源在服务层判不了）；
V4 的方向都对，但每一件都依赖本批不存在的上游 —— **显式登记而不是假装做了**。
---

> **以下 §4~§10 由主 agent（串行收口）补写。**
> §1~§3 是子 agent 在并行分支上产出的（设计阶段交付物）；
> §4 起是**收口阶段**的事实记录 —— 落迁移、跑证据、接通契约、跨包回归的实测结论。
> 两者视角不同：§1~§3 回答"为什么这么设计"，§4~§10 回答"**它真的这样运行吗**"。

## 4. 落库实现（主 agent）

### 4.1 五张表与真实列数

| 表 | 列数 | 不可变 | 说明 |
|---|---|---|---|
| `briefs` | 6 | ✗（可变） | 逻辑 Brief，项目内唯一（`uq_briefs_project_id`）；`active_version_id` / `draft_id` 指向当前 |
| `brief_drafts` | 17 | ✗（可变） | 工作区，项目内一 Brief 一草稿（`uq_brief_drafts_brief_id`）；承载 `structured_fields` / `claims` / `issue_resolutions` / `suggestions` |
| `brief_versions` | 12 | ✓（触发器拦 UPDATE/DELETE） | 固化版本；`id` **同时是** `version_nodes.id`（注册表与子表同行） |
| `brief_claims` | 13 | ✓ | 结论与来源；**事实类必须有证据**（CHECK + 触发器验真伪） |
| `brief_issues` | 10 | ✓ | 字段疑问；`BLOCK` 不允许停在 `OPEN` |

约束总量：**35 个 CHECK**、**2 个部分索引**、**3 个唯一约束**、**7 个触发器**、**4 个本包函数**。
逐项计数见 `evidence/m1/wp18-brief.log`（38 条核对）。

### 4.2 迁移的手工改动（autogenerate 做不到的三件事）

`migrations/versions/20260922_1207_19441cc26ce9_wp18_brief_structure_and_claims.py`：

**(1) 循环外键必须手工拆环。** `briefs.draft_id` ↔ `brief_drafts.brief_id` 互相引用，
而 autogenerate 按**字母序**建表（`brief_drafts` 排在 `briefs` 前面）→ 执行时必然
`UndefinedTable: relation "briefs" does not exist`。

**`DEFERRABLE` 救不了这件事** —— 这是本包最容易误解的一点：它管的是
**事务提交时**的行级校验，**不是 DDL 层面**的表存在性。拆环三步：

```python
op.create_table('briefs', ...)          # 先建，暂不带 draft_id 外键
op.create_table('brief_drafts', ...)    # 此时 briefs 已存在
op.execute(
    "ALTER TABLE briefs ADD CONSTRAINT fk_briefs_draft_id_brief_drafts "
    "FOREIGN KEY (draft_id) REFERENCES brief_drafts (id) ON DELETE RESTRICT "
    "DEFERRABLE INITIALLY DEFERRED"
)
```

`downgrade` 对称：先 `ALTER TABLE briefs DROP CONSTRAINT`，再删表（见 ISS-60）。

**(2) 触发器必须手工补，且与测试同源。** 迁移里 `op.execute` 调 `brief_ddl.all_trigger_ddl()` ——
**与测试用的是同一份 DDL 字符串**，不是手抄的第二份。手抄会漂移，而漂移方向是
"库里的触发器与测试断言的不是同一个东西"。

**(3) `script.py.mako` 必须引用 `${message}`。** 原模板把 docstring 首行写死，
`alembic revision -m "WP18 brief structure and claims"` 的 `-m` **被静默丢弃**
（日志与文件都显示模板原文，无法从日志判断跑了哪个迁移）—— 见 ISS-61。

### 4.3 `downgrade` 的归属判据

**判据是「哪些对象是我建的」，不是「哪些对象跟我的表有关」。**

| 对象 | 建它的是 | 是否要删 | 为什么 |
|---|---|---|---|
| `trg_version_nodes_brief_paired` | **本包** | ✅ 删 | 挂在 WP11 的 `version_nodes` 上，但由本包为闭合 OP-88 而建 |
| `rednote_reject_mutation()` | WP11 | ❌ **不删** | 所有不可变表共用；删了会让 WP15/16/17 的保护一起失效 |
| `rednote_check_brief_*`（4 个） | **本包** | ✅ 删 | 本包自有的一致性函数 |

第一版 `downgrade` 用了统一策略「跳过所有 `_drop_function`」，结果 4 个自有函数永久残留
（`alembic` 报 exit=0、表也确实删了，但清理不完整 —— "部分成功伪装成完全成功"）。
修法：`ddl.py` 的 `_consistency_items` **改名公开**为 `consistency_items()`，让迁移能单独
取到那 4 项。见 ISS-62。

### 4.4 共享文件的改动（8 处接缝只动了 3 处）

| 文件 | 改动 | 说明 |
|---|---|---|
| `migrations/env.py` | 加 `from rednote.modules.brief import models as _brief_models  # noqa: F401` | 修正字母序：audit → **brief** → events |
| `main.py` | 加 `brief_router` 的 import + `include_router` | —— |
| `migrations/script.py.mako` | 重写，首行改 `${message}` | 同时写入 G2 三件事 + 拆环 + 触发器需手工补 |

其余 5 处（`contracts/openapi.json` / 登记册 / `README.md` / `evidence/**` / `.workbuddy/memory/**`）
由**主 agent 串行收口**时更新，分支**禁写** —— 见 `docs/m2/M2并行开发接缝冻结.md`。

## 5. OP-88 闭合论证（本包的隐藏交付）

WP11 留下一条**未实现**的约束（理由是"没有子表"）：

> 「延迟约束检查 `entity_type`/`entity_id` 与子表一致」

`brief_versions` 是本系统**第一个领域版本表**，所以这个包必须把它闭合。判据是**双向**的：

| 方向 | 判据 | 实现 | 不做的后果 |
|---|---|---|---|
| 正向 | `brief_versions.id` 对应的节点必须 `entity_type='BRIEF'`，且 `entity_id = brief_id` | `trg_brief_versions_node_consistency` | 子表可以挂在 `PLAN` 节点上 —— 注册表与领域表指向两个不同的东西 |
| 反向 | `BRIEF` 节点必须配对一条 `brief_versions` 行 | `trg_version_nodes_brief_paired` | 注册表里可以有"没有内容"的 Brief 版本 |

两个都是 `CREATE CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED`，
**延迟到提交时校验**：`register_version` 内部用 SAVEPOINT + 重试取修订号，
它不暴露"节点行何时落库"的控制权；延迟让"谁先谁后"不再是一个必须猜对的问题。

### 5.1 反向判据为什么要收窄（本机实测推翻了我的第一判断）

`rednote_check_brief_node_paired()` 第一段是：

```sql
IF NOT EXISTS (SELECT 1 FROM briefs WHERE id = NEW.entity_id) THEN
    RETURN NULL;   -- entity_id 不是真实存在的逻辑 Brief -> 直接放过
END IF;
```

我第一版证据脚本造的"孤立节点"是**随机 uuid 的 `BRIEF` 节点**，结果没被拦，一度判断
"反向判据有洞"。**实测后确认是探针造错场景**，收窄是**必需的**：

- **不变的**：WP11 的测试夹具 `_register(entity_type: str = ENTITY_TYPE_BRIEF)` ——
  `BRIEF` 是**默认参数**，它只写 `version_nodes`、**不写 `briefs`**。
  若不收窄，本包一落库，WP11 的整批测试会在提交时集体炸。
- **正确的反例形状**：「**`briefs` 行存在** + `BRIEF` 节点 + **无** `brief_versions` 行」。
  实测该场景被正确拦下（`23514`）。

同一类"字面直觉 ≠ 实现判据"的错误，本包共犯两次（另一次是零残留判据，见 ISS-63）。

### 5.2 对照组：合法配对必须能通过

证据里刻意加了第三条：`briefs` + `BRIEF` 节点（`entity_id = brief_id`）+ `brief_versions`
→ **断言通过**。

**没有这一条，前两条无法排除"判据把一切都拒绝"** —— 一个永远 `RAISE` 的触发器
也能让"正向拦、反向拦"两条全绿。活性验证必须成对：*该拦的拦、该过的过*。

## 6. 证据清单

`tools/m1/collect_wp18_evidence.py` → `evidence/m1/wp18-brief.log`，
**38 条核对（`EXPECTED_CHECKS=38`）/ 38 PASS / 0 FAIL**。

> **为什么条数也要核对**：RESULT 行若只统计"失败了几条"，那么"脚本崩溃、
> 本该产生的 CHECK 一条都没产生"与"全部通过"在计数上完全一样（ISS-43）。

### 6.1 结构核对（22 条）

5 表列数 / 迁移版本（**判据用"不早于"** `_revision_is_at_least`，不是"恰好等于" —— ISS-44）/
7 触发器存在 / 4 个自有函数 / 共用函数 `rednote_reject_mutation` 仍在 /
跨包不可变触发器仍在（3 个 + 总数 8）/ 可变表无不可变触发器 /
2 个部分索引的 `WHERE` 未退化 / 3 个唯一约束 / 35 CHECK / M0 探针表仍在（G2）。

**部分索引的 `WHERE` 必须规范化后比对**：PG 会把 `state = 'OPEN'` 改写成
`WHERE ((state)::text = 'OPEN'::text)` —— 硬比对会把**正确实现**判成退化。

### 6.2 活性验证（10 条，**结构核对不能替代**）

| # | 验证 | 断言 | SQLSTATE |
|---|---|---|---|
| 1 | OP-88 正向：`INSERT` 阶段不报错 | 延迟语义成立 | —— |
| 2 | OP-88 正向：强制校验时被拦 | 子表指向错节点会拦 | `23514` |
| 3 | OP-88 反向：有 briefs 行但无子表行 | 孤立节点会拦 | `23514` |
| 4 | **合法配对必须通过**（对照组） | 判据不是一律拒绝 | —— |
| 5 | `brief_versions` 不可篡改 | `UPDATE` 被拦 | `23001` |
| 6 | 事实类 claim 缺证据 | 被拦 | `23514` |
| 7 | `BLOCK` 疑问停在 `OPEN` | 被拦 | `23514` |
| 8 | **假溯源**（片段不属于本版本解析运行） | 被拦 | `23514` |
| 9 | **疑问挂错项目** | 被拦 | `23514` |
| 10 | 草稿层 `PARTIAL` 却无疑问 | 被拦 | `23514` |

第 8/9/10 条是收口时**补上的覆盖缺口**：本包有 4 个一致性触发器，初版只验了 2 个的活性，
另 2 个只验了"存在"—— 而"存在 ≠ 生效"是本项目铁律。

### 6.3 探针取判定的正确姿势（ISS-63 的核心）

**用 `SET CONSTRAINTS ALL IMMEDIATE`，不用 `commit()`。**

```python
conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))  # 提前校验时机，事务仍未提交
...                                                  # 拿判定
trans.rollback()                                     # 一行不留
```

`commit()` 取判定等于"我把炸弹点着了看它炸不炸"：判据若失效就会提交成功，
而这些表**不可变（触发器拦 DELETE）又被 FK 钉住** —— 实测连自己的探针残留都删不掉：

```
ERROR: version_nodes 是不可变记录，不允许 DELETE（数据结构文档 02 §1）
```

这条错误**正是"不可变落到物理层"的强证据**，但也意味着共享库上跑的测试只会让不可变表
涨不会减（当前本机已有 52 行 pytest 遗留 `测试产品`）。因此零残留判据改为
**与运行前基线差分**，而不是"全表 0 行"。

### 6.4 跨包回归（收口必做，单跑发现不了）

复跑 wp11~wp18 全部证据脚本，**WP17 报 FAIL**：

```
CHECK FAIL 不另造解析查询端点（状态挂 GET /files/{fileId}）:
  ['/api/v1/projects/{projectId}/brief-parse-jobs']
```

WP17 的判据写成 `"parse" in p and not p.endswith("/parse-jobs")` —— **全契约范围**的模糊条件。
WP18 只是合法新增了 Brief 解析任务端点，历史证据就被判退化。收窄到 files 作用域后
31 PASS。同类错误的另一变体就是 ISS-44。见 ISS-64。

## 7. 跨包影响面

| 影响对象 | 本包做了什么 | 风险与处置 |
|---|---|---|
| `version_nodes`（WP11 的共用注册表） | **新增**一个触发器 `trg_version_nodes_brief_paired` | 用 `WHEN (NEW.entity_type = 'BRIEF')` 限定到本包负责的实体类型 —— 其余 6 类的领域表还不存在，本包对它们没有校验能力。**不改 WP11 任何代码或表结构** |
| `rednote_reject_mutation()`（WP11 的共用函数） | **只引用，不删** | `downgrade` 刻意保留；证据里专门验"共用表 `audit_events` 的不可变保护仍生效（23001）" |
| WP15/16/17 的不可变保护 | 无改动 | 不可变触发器总数 5 → **8**（本包 +3），证据逐条核对 |
| 契约 | 从 25/30 → **30 paths / 35 operations** | `--check` EXIT=0 |
| 测试库 | 分支 A 用 `rednote_test_wp18` | 通过 `REDNOTE_TEST_DATABASE_URL` 覆盖，**无需改 conftest** |

**唯一一处"挂到别人表上"的动作**（`version_nodes` 上的新触发器）是本包最容易被质疑的地方，
故在 §5.1 用实测把它的作用域边界写清楚了。

## 8. 测试与质量门禁

| 项目 | 结果 |
|---|---|
| WP18 专测 | **126 passed**（连续 3 轮稳定） |
| 全量 pytest | **871 passed / 0 failed** |
| 迁移双向可逆 | **20 PASS / 0 FAIL**（含"自有函数要删、共用函数要留"的精细区分） |
| 契约门禁 | `--check` **EXIT=0**，30 paths / 35 operations |
| 证据脚本 | **38 PASS / 0 FAIL**（条数命中 `EXPECTED_CHECKS`） |
| 证据脚本静态自检 | 9/9 通过（`check_evidence_scripts.py`） |
| 问题记录库 | **64 条 / 0 error / 0 warn** |

**共享文件零改动**是并行开发的硬约束 —— WP18 分支只写了
`modules/brief/**`、`tests/test_brief_wp18.py`、`docs/m2/WP18-*` 三处，
8 处共享文件全部由主 agent 串行收口。

## 9. 契约增量（5 个端点）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/projects/{projectId}/brief-parse-jobs` | 发起解构（把解析片段变结构化字段） |
| `GET` | `/projects/{projectId}/brief` | 读当前 Brief（草稿 + 当前版本） |
| `PUT` | `/projects/{projectId}/brief-draft` | 保存草稿 |
| `POST` | `/projects/{projectId}/brief-versions` | 固化（确认版本） |
| `POST` | `/projects/{projectId}/brief-enrichment-jobs` | 基于已有版本加厚 |

## 10. 遗留与下一步

### 10.1 本包新增的待定项（OP-141 ~ OP-145）

| 编号 | 待定项 | 阻塞 | 为什么本机关不掉 |
|---|---|---|---|
| OP-141 | 解构/加厚提示词**没有版本号与回归基线** | 🟠 | 提示词是硬编码字符串；改了之后旧结果被当成新提示词的产出，库里看不出来（与 WP17 的 OP-139 同源，应一起解决） |
| OP-142 | `DecompositionResult` 刻意不含"置信度" | 🟡 | 口径已定（"有来源"二值可校验、"有多确定"不可校准），**未关闭的是替代方案**：多来源冲突时没有推荐顺序 |
| OP-143 | 真实模型抽取准确率**完全未知** | 🟠 | 本包验收的是**机制**（用 `ScriptedDecomposer` 驱动），机制生效 ≠ 模型准。依赖 OP-136/137 与 OP-34/35 |
| OP-144 | 加厚**建议的"采纳"无法与手改区分** | 🟡 | `suggestions` 是建议列表，采纳方式就是写进 `structured_fields` —— 要算采纳率需独立的采纳记录表，而真实用法未定 |
| OP-145 | **不可变表的数据只能涨不能减** | 🟡 | 实测连自己的探针残留都删不掉。证据库与测试库需分离，否则测试残留永久混在证据库 |

### 10.2 与 WP20 的接缝（并行开发）

`entity_type` 的受控取值已按包划分，避免两个并行分支抢同一个值：

| 取值 | 归属 |
|---|---|
| `BRIEF` | **WP18**（本包） |
| `KNOWLEDGE` | WP20 |
| `BASELINE` | WP22 |
| `PROMPT` | WP23 |

**PARTIAL 的消费规则**（本包定，后续包必须遵守）：

- `SUCCEEDED` —— 全部解析成功，可当完整输入；
- `PARTIAL` —— **有页没解出来，必须暴露缺失，不得当完整输入**（数据库层已保证：
  `brief_drafts` 的 CHECK 要求 `PARTIAL` 时 `issue_resolutions` 非空；
  `brief_versions` 的触发器要求固化时至少一条 `brief_issues`）；
- `FAILED` —— 不得消费。

8 处共享文件的归属、分支写入范围、测试库隔离口径，见
`docs/m2/M2并行开发接缝冻结.md`。
