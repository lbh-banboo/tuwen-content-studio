# WP30 AI 自检与硬软规则 说明

> 状态：**数据层 + 规则层 + 服务层 + 接口层已落盘（2026-10-19）**。§4 以后（阶段 4 实施记录、
> 判据清单、收口核对）留待主 agent 收口时补。
> 模块归属：`services/backend/src/rednote/modules/review/**`（14 个文件）。
> 前置依据：`docs/m3/M3并行开发接缝冻结.md` §5.12（content 读接缝）、§5.4（media 只读）、
> §5.6（planning 只读）、WP29 已交付的 `feedback.service.create_feedback` 与既有审核闸。
> 本包**不建迁移文件**（接缝冻结 §4.1：建迁移是主 agent 的独占动作）；
> DDL 草案见 `docs/m3/WP30-ddl-草案.py`。

## 0. 本包的一句话定位

**把"这一篇稿子合不合规"从一句人话（原型里写死的「关键事实问题未解决不能通过」）变成一次可重放、可溯源、逐规则有结论的机器判定：谁判的、判的是哪一版、哪条规则命中、命中在哪一段 / 哪张图、以及——哪几条规则本轮根本没判。**

最后那半句才是本包最难的部分：**"没判"与"通过"在数据结构上必须无法混淆**。

### 0.1 边界（对照前后三个包）

| 包 | 它解决的 | WP30 **不**解决的那部分 |
|---|---|---|
| **WP26**（已交付） | 生成正文 / 标题 / 绑定；写入时跑 `validate_bindings` | WP30 不重跑"写入期校验"——那会是恒绿规则。WP30 判的是**写入之后、基线可能已前进**的那一类事实 |
| **WP29**（已交付） | 反馈实体（锚点 / 分类 / 严重度 / 四态机）、人工审核、**"有未解决 BLOCKING 就不许 APPROVED"这道闸本身** | WP30 **不新建第二道审核闸**、**不新建第二套分类码**、**不重做批量审核**。它只负责**把 AI 判出来的阻断项喂进那道既有闸** |
| **WP31**（未开工） | 试产关口（"当前轮所有内容完成处置"、"无待处理阻断意见"…） | WP30 不实现关口聚合；它只保证关口能读到"这条内容自检过没有、结论是什么" |
| **WP33**（未开工） | 跨篇相似度 + 质量报告 | WP30 的 8 类里 `DUPLICATE` 是**跨篇**语义 ⇒ 本包**显式不判**（SKIPPED + 机器可读原因），不自己写一份相似度 |

### 0.2 本包明确**不**承担的三项计划条目（如实登记，不含糊）

1. **AQ-01 事实正确率 ≥95%**：计划把 WP30 列为责任包之一，但 AQ-01 要的是**盲审报告**，而 WP04 实测 `BLOCKED_ON_EXTERNAL (E4, E7)`（42 条评测集已建、**已评 0 条**）。⇒ **WP30 交付的是"事实类规则有落点"，不是"事实正确率达线"**。把后者写进本包的完成定义即为自写下即为假。
2. **AC-01~03 权限 / 自审 / 交接**：实测"自审"的产品口径是 `outputs/rednote-demo/members.html:593`「**自审允许但留痕**：所有审核保存审核人、时间、对象版本、结果与意见」——这四样 WP29 的 `content_reviews` 已全有。⇒ **WP30 对 AC-01~03 无新增责任面**。
3. **AC-28 协同**：A~F 六组判据全部落在 WP29 的编辑端点上，WP30 一条都不占。但本包有一条**同族义务**（不是 AC-28 条文，是同一类静默失败）：自检轮次必须绑定 `subject_version_id`，并在读时判"这一轮判的还是当前版吗"——照 WP29 `anchorState: FRESH|STALE` 的同款手法（见 §1.6 R5）。

---

## 1. 阶段 1：需求追问（superpower-workflow 强制 8 问）

### 1.1 这个需求真正解决什么问题？

前端原型 `services/backend/static/content-feedback.html:152-191` 有一块「**AI 自检结果** · 8 类建议分类」，八张卡片逐个写着状态与一句理由：

| 卡片 | 原型文案（逐字） | 状态 |
|---|---|---|
| 事实 | 「表活占比与 F-003 口径冲突」 | `1 阻断` |
| 语气 | 「口语化、无过度营销」 | 通过 |
| 结构 | 「场景引入→测试→成分→结论」 | 通过 |
| 场景 | 「与配置场景一致」 | 通过 |
| 重复 | 「开头与 T01-05 相似度 71%」 | `1 提示` |
| 媒体一致性 | 「6 张图均对应正文段落」 | 通过 |
| 技术格式 | 「1440×1920 · JPG · 均 <2MB」 | 通过 |
| 其他 | 「禁用词检测 0 命中」 | 无 |

列表页另有两列：`AI 自检` ∈「通过 · 0 项 / 提示 · N 项 / 阻断 · N 项事实 / **未自检**」，`人工状态` ∈「待审核 / 修改中 / 已通过 / 已淘汰」。批量通过按钮的提示语是「**仅对已完成自检、无阻断问题的内容执行批量通过**」。

WP29 交付之后，这块 UI 的数据来源**仍然一个字节都没有**，而且是**显式留空**的：

> `PreviewPayload.self_check_summary: None = None` ——「自检摘要的**占位**：自检能力属于 WP30（`modules/review`），本波未开工。`build_preview` 不编造任何自检数据 —— 宁可为空，也不让前端展示一份看起来'通过'的假摘要」（`feedback/service.py:141-144`）

所以本包真正解决的是：**把八张卡片的每一格变成一条有身份、有实现、有阳性对照用例的规则，并让"未判"与"通过"在数据结构上无法混淆**。

### 1.2 核心用户是谁？

| 用户 | 关心什么 | 本包给他什么 |
|---|---|---|
| 逐条审核的运营 | 这篇有没有硬错；哪一段 / 哪张图有问题；我能不能直接点通过 | 一次 `POST /contents/{id}/self-checks` → 8 类聚合 + 逐条 finding 带 `paragraphRef` / `mediaItemId`；阻断项**自动落成一条 AI 反馈**，于是 WP29 的审核闸当场拒绝"通过" |
| 批量处理的运营 | 10 条里哪几条能一键过 | `POST /projects/{id}/self-checks`（逐条独立 savepoint）+ `GET /projects/{id}/self-checks` 看板列 |
| **不信任 AI 的审核者** | 它凭什么说"通过"；它有没有偷偷跳过 | 每轮落 `self_check_rule_results` **逐规则一行**：`PASSED` / `FAILED` / `SKIPPED(+原因)` / `ERRORED(+原因)`。"8 类全绿"与"5 类绿 + 3 类未判"在响应里长得**不一样** |
| WP31 的关口判定 | 这条内容自检过没有、结论是什么 | `self_check_runs.outcome` + `subject_version_id`；关口只读，不在本包实现聚合 |
| WP33 的质量报告 | 规则命中的历史能不能统计 | 三张不可变表 + `rule_set_version` 落库快照（规则改版后历史仍可解释） |
| 排障的人 | 为什么这条规则没判 | `skip_reason` 是**受控取值**（如 `PLATFORM_MEDIA_LIMITS_NOT_CONFIGURED` / `CROSS_ITEM_SCOPE_OUT_OF_PACKAGE`），不是自由文本 |

### 1.3 最小 MVP 应包含哪些功能？

**六个端点**（4 条新 path；路径空间实测无冲突：落库前 `contracts/openapi.json` 的 86 条 path 里含 `self` 的**零条**）：

| # | 方法 + 路径 | 用途 | 成功码 | 权限 |
|---|---|---|---|---|
| ① | `POST /contents/{contentId}/self-checks` | 对**当前版本**跑一轮自检（同步），落 1 轮 + N 条规则结果 + M 条 finding；阻断项同事务落 AI 反馈 | 201 | `REVIEW` |
| ② | `GET /contents/{contentId}/self-checks` | 轮次历史（游标分页，倒序），每轮带 `subjectState: CURRENT\|STALE` | 200 | `VIEW` |
| ③ | `GET /self-checks/{runId}` | 单轮详情：8 类聚合 + 逐规则结果 + findings | 200 | `VIEW`（反查 `project_id`） |
| ④ | `POST /projects/{projectId}/self-checks` | 批量自检（`contentIds[]`，逐条独立、部分成功也 200） | 200 | `REVIEW` |
| ⑤ | `GET /projects/{projectId}/self-checks` | 看板列：每条内容**最新一轮**摘要 | 200 | `VIEW` |
| ⑥ | `GET /self-check-rules` | 规则目录（代码注册表的只读投影） | 200 | 登录即可 |

**三张表**（全部不可变，见 §3 的 V3）、**一张代码规则注册表**（11 条规则，见 §2.3）。

**五条不可省的判据**（每条都对着一个"看起来有防线、实际没有"的形态）：

1. **每条规则必须有阳性对照**：存在一组输入让它 `FAILED`，也存在一组让它 `PASSED`。否则它是恒绿规则 = 假防线（R1）。
2. **`SKIPPED` 不得被折算成通过**：`outcome` 的四条等价式里 `skipped_count` **不参与**；响应体把"未判数"单列。
3. **`outcome` 与计数不得漂移**：四条双向等价式 CHECK 下推到数据库（R6）。
4. **零切段实现**：段落坐标只经 `media_bridge` 转发（R3）。
5. **阻断项 → 反馈的写入幂等**：同一 `(content_item_id, rule_code, 锚点)` 上不得堆积多条未解决 AI 反馈（R4）。

### 1.4 描述有哪些疏漏应当补充？理由是什么？

| # | 疏漏 | 处置与理由 |
|---|---|---|
| 1 | **原型的 8 类卡片里，有 3 类在当前 schema 下判不了** | `TONE` 需要模型判定（MVP 不调模型，见 §1.5）；`DUPLICATE` 是跨篇（WP33）；`SCENE` **全仓没有"内容→场景"的关联列**（`content_items` 无 scene 列；唯一相关的 `narrative_blueprints.usage_scene` 是**自由文本**，与 `plan_drafts.scene_items` 的 key 之间没有任何机器可判的关联）。⇒ 三类**显式 SKIPPED + 受控原因码**，不伪造通过。**这直接改变 Demo 画面**（3 张卡片显示"未判"而不是"通过"），已由主会话拍板照办 |
| 2 | **"关键事实问题未解决不能通过"在哪一层，原型没说** | 实测**已经落在 WP29**（`feedback/service.py:872-888`：`decide_review` 对 `APPROVED` 前置查 `severity=BLOCKING AND state NOT IN (RESOLVED,REJECTED)`，命中抛 409 `UNRESOLVED_BLOCKING_FEEDBACK`）。⇒ WP30 **不实现这条规则**，只负责产出喂给它的数据（见 §2.4） |
| 3 | **原型写"6 张图均对应正文段落"，但后端固定 3 张** | `media/types.py:180 IMAGES_PER_CONTENT = 3`，且该文件原文写着「★★ 老大的拍板：固定值 —— **与原型的一处已知偏差**」。且 `media_form` 落在 `media_versions` **逐版本**而非逐内容 ⇒ "图数应该是几"在内容层面无唯一答案。⇒ **图数规则砍掉**；媒体一致性只判"每个 `media_items.paragraph_ref` 是否仍在当前段 ref 集合内" |
| 4 | **"每段一张图"是错的判法** | 契约 §4 权威定义：顺序对位、段落数不足 3 时多余张**回落最后一段**，且「**多张落同一段是合法的**……`paragraph_ref` 只是当前版本的投影、**不落唯一约束**」。⇒ 媒体一致性规则**不得**断言 ref 唯一或一一对应 |
| 5 | **技术格式的阈值不在代码里** | 平台限制是 `runtime_settings` 的**占位键**（`planning/types.py:76 SETTING_KEY_PLATFORM_MEDIA_LIMITS`，在 `PLACEHOLDER_SETTING_KEYS` 里；OP-64 仍 `OPEN`）。⇒ `TECH_FORMAT` 规则读 `get_effective_limits(..., materialize=False)`，未配置时返回 **SKIPPED(`PLATFORM_MEDIA_LIMITS_NOT_CONFIGURED`)**，**不**内置 1440×1920 / 2MB 字面量（内置等于在本包造第二处平台限制真值）。⚠️ 连带缺口：配置值的形状是 `{键: 数 \| null}`，**承载不了"允许的图片格式列表"** ⇒ `image_format` 本规则**不判**（如实登记，见 §2.6） |
| 6 | **自检该不该自动触发没定** | 自动触发要改 `content/handlers.py`（共享文件）或 `main.py` 的任务注册表。⇒ MVP **只做显式端点**；`trigger_kind` 的取值域**刻意不含 `AUTO`**（不留空转取值）。原型列表页本来就有「未自检」这一态，显式触发与它一致。自动触发登记为待定项 |
| 7 | **自检要不要注册版本节点** | **不注册**。`ENTITY_TYPES` 是受控取值域（八个值，无"自检"），加一个值要改 `modules/versions/*.py`（共享文件）。自检轮次是**观测**不是可引用版本；它靠 `subject_version_id` 指向被判的那一版就够 |
| 8 | **8 类分类码的归属** | WP29 说明 §4.4 原写「本包未涉及 8 类」——**与磁盘不符**：`feedback/types.py:49-56` 的 8 个值就是原型自检那 8 类的逐字对应。⇒ 口径**已经落地**，WP30 照抄其**值**、名字各写一份（`REVIEW_CATEGORY_*`），由跨包 parity 测试钉住。该条待定项已由主会话按"磁盘定案"闭合 |
| 9 | **`PreviewPayload.selfCheckSummary` 谁来填** | 填它要改 `modules/feedback/**`（WP29 已交付的别人模块，分支禁写）。⇒ 本包提供端点 ③⑤，前端**另取一次**；是否回填 `selfCheckSummary` 由主 agent 在收口期决定（登记为待定项） |
| 10 | **规则改版后的历史怎么解释** | 每轮落 `rule_set_version`（代码常量 `types.RULE_SET_VERSION` 的落库快照，与 `content_versions.schema_version` 同款）。没有它，"上周判通过、今天判阻断"无法区分"稿子变了"与"规则变了" |
| 11 | **事实类规则在"项目还没有生效基线"时怎么办**（本包实现期新增的一条） | 判据域为空时**既不能判通过**（没判据）**也不能判失败**（否则每条 binding 都会被系统性误报成超范围 —— 恒红的规则与恒绿的规则同样是假防线）。⇒ 第 5 个受控原因码 `NO_EFFECTIVE_BASELINE`。这是对方案原文的一处**补充**，已在交付报告里登记 |

### 1.5 哪些功能现在不该做？（砍掉清单）

| 不做 | 理由（逐条有出处） |
|---|---|
| **调模型做语气 / 事实判定** | ① ADR-10：`LOCAL_ONLY` 无外部回退（`gateway/policy.py`「这条没有例外、没有回落、没有降级」），而 `capability_policies.external_policy` 的**服务端默认值就是 `LOCAL_ONLY`** ⇒ 默认部署下模型路径当场被拒；② 外部额度是 E7 外部阻塞；③ 计费面：`content/billing_bridge.py` 的 `calls_for(task_kind, ...)` 只认 `CONTENT_STRUCTURE` / `CONTENT_COPY` 两个 kind，WP30 要调模型就得在本包再造一份次数换算 + 预占编排，而"出活前必须预占"是接缝文档 §5.7 的硬条款。⇒ MVP **纯本地判定**；`QUALITY_CHECK` 提示词档位虽已在 `prompts/types.py` 预留，本包**不消费它、也不声称消费它**（判据：AST 查本包不得 import `gateway` / `prompts`） |
| **跨篇重复检查（相似度）** | WP33 的分界就是"逐篇 vs 跨篇"。向量设施只覆盖 `knowledge_chunks`，正文没有 embedding 落点。自己写一份 = 仲裁项④ 里"两边各建一份"被明令禁止的那件事。⇒ 本包**一行相似度算法都不写** |
| **规则配置表 / 项目级阈值覆盖 / 规则 CRUD 端点** | ① 通用机制已存在且**唯一**：`planning/settings_service.py:273 get_effective_limits` docstring 原文「**全仓唯一的配置读取路径**」；新增 `SETTING_KEYS` 要改 `modules/planning/**`，而那是本批 **WP31 的写面** ⇒ 跨包争用；② 本包做一张自己的规则配置表 = 第二处配置真值；③ 无消费点的 `enabled` 开关 = ISS-95 族。⇒ **MVP 规则策略全在代码**（见 §3 的 V2 否决） |
| **规则命中自动修稿 / 自动重生成** | 自检自动触发重生成会产生"机器改了稿、没人点过头"的写路径，与 `quality-view.html:481` 原文「**生效动作保留显式确认**……把规则写进生成链必须由人点这一次」直接冲突 |
| **学习闭环 / 修复提案 / 回归评测集** | `static/quality-view.html` 整页是**双 Loop 学习闭环**的页面，不是 WP30。它要的"四类评测集 + 匿名成对盲评"正是 AQ-01 那条被 E4/E7 阻塞的路 |
| **禁用词词库的运营维护界面** | MVP 的禁用词表是代码常量（落 `OTHER` 类）。做成可维护词库 = 又一张配置表；且原型只给了「禁用词检测 0 命中」一句，没有维护入口 |
| **自检结果的导出 / 报表** | WP33 |
| **异步化（走 jobs 队列）** | 纯本地判定的一轮是 4~5 次库读 + 纯函数，没有长耗时与外部依赖。入队要新增 `jobs.kind` 取值 + 改 `modules/jobs/*.py`（共享文件）。⇒ 同步执行；若将来接入模型再异步 |
| **改 `content_items` 加"自检状态"投影列** | `modules/content/*.py` 根层禁改，且投影列必须配触发器钉住、否则是第二处真值。看板列由端点 ⑤ 的 `DISTINCT ON` 查询给出 |
| **标题的禁用词检测** | 标题没有段落坐标，混进来会让 `OTHER_BANNED_TERM_HIT` 同时产出两种锚点粒度。登记为后续项 |

### 1.6 这个系统有哪些技术风险？

| # | 风险 | 表现（静默失败的样子） | 本包的处置 |
|---|---|---|---|
| **R1** | **恒绿规则（永不命中的"防线"）** | 把已有数据库 CHECK 再写一遍（蓝图字段非空、标题长度、tags 是数组…），或把只能由 `validate_bindings` 唯一写入口产生的事实再判一次。结果：8 张卡片全绿、看起来自检很完备，而它**从未拒绝过任何一条内容**。这是 ISS-95 族的最纯形态，且**没有任何现成判据会报** | ① `evaluators.py` 是**纯函数**（零 IO / 零 session / 零 `datetime.now()`），造数成本降到构造一个 dataclass；② `_POSITIVE` / `_NEGATIVE` / `_ALWAYS_SKIPPED` 三张用例表与 `rules.RULES` 做**集合相等**断言 ⇒ 加规则不补用例当场红；③ 逐条参数化断言"阳性 FAILED、阴性 PASSED"；④ 砍掉所有已被数据库 CHECK 拦住的规则（见 §1.5 与 `evaluators.py` 的"砍掉的规则"表） |
| **R2** | **`SKIPPED` 被折算成通过** | 本包有 3 类恒 SKIPPED。若 outcome 把 skipped 算作 pass，或响应体只给"命中数"不给"未判数"，前端会把「5 类判了 + 3 类没判」渲染成「8 类全通过」——而原型的批量通过按钮正是以"已完成自检、无阻断问题"为前提 ⇒ **假绿直接放行一批内容** | ① `outcome` 的四条双向等价式 CHECK 里**不出现** `skipped_count`（数据库层）；② 响应体同时给五个数（`evaluatedCount` / `skippedCount` / `blockingCount` / `warningCount` / `infoCount`）；③ 用例断**具体值**："6 条没判时 `outcome='PASSED'` 且 `skippedCount=6`"；④ 分类聚合从 `REVIEW_CATEGORIES` 起算，某一类即便一行都没有也给一张全零卡片而**不是让它消失** |
| **R3** | **写出第 4 份切段实现** | 为"命中落在哪一段"随手 `body.split("\n\n")` ⇒ 段号坐标系与 media 分叉，`MEDIA_PARAGRAPH_REF_STALE` 这条规则会**系统性恒红**（它比较的两个集合来自两套切段），真正的失配被噪声埋掉。接缝冻结 §5.1 点名这是 OP-197 的第二次现身 | ① 取段只经 `review/media_bridge.py` → `media/schemas.paragraph_refs_for_body`；② AST 判据：本包 14 个源文件都不得出现 `split` / `splitlines` / `paragraph_ref_for` / `parse_paragraph_ref` 调用（用 AST 而不是文本搜索 —— docstring 里提到它会让文本判据恒绿）；③ 收口跑 `check_paragraph_split_parity.py`，`_IMPL_FILES` 仍应是 **3** 个（本机实测仍为 3） |
| **R4** | **AI 反馈重复堆积 / 或去重写错导致阻断项漏建** | 重跑 10 次自检 ⇒ 同一条事实错误堆 10 条 `OPEN` 反馈，运营的待处理列表被机器噪声淹没（而 WP29 的 `anchorState` 明令"条数不因判定而减少"，没法靠过滤救）。**反向错误更严重**：去重键写宽了 ⇒ 新的阻断项被误认为"已有"⇒ **不建反馈 ⇒ WP29 的审核闸放行**（静默） | ① 去重键 = `(rule_code, anchor_kind, paragraph_ref, media_item_id)`，且仅对 `author_kind='AI' AND state NOT IN (RESOLVED,REJECTED)` 的反馈生效；② 用例三条：**重跑两轮 ⇒ 反馈恰 1 条、finding 2 条且都指向同一 id**；**人工 RESOLVED 后重跑仍命中 ⇒ 新建第 2 条**；**换一条 rule_code 命中同一锚点 ⇒ 新建**；③ 同一轮内就地更新去重缓存（同一段里两条 binding 都超范围时不该在同一轮堆两条） |
| **R5** | **轮次与版本脱锚** | 自检判的是 v2，运营在 v3 上看到"通过"。原型列表页的 `AI 自检` 列不带版本号，前端很可能直接渲染最新一轮 ⇒ "上一版通过"被读成"当前版通过" | ① `subject_version_id` 必填 + 一致性触发器（版本必须属于本轮的内容）；② ②③⑤ 三个读端点**一律**返回 `subjectState: CURRENT\|STALE`；③ **只标记、不过滤、不删行、不自动重跑**（自动重跑是读路径上的写副作用，且会在批量看板上放大成 N 次写；把 STALE 藏起来会让看板上的内容突然变回"未自检"）；④ 用例："自检后走 WP29 的 `edit_content` 追加一版 ⇒ 该轮必被标 `STALE`，且**条数不减**" |
| **R6** | **`outcome` 与计数漂移（派生字段双声称）** | `outcome` 由服务层算、计数也由服务层写，两处都可能漏改。表现是"`outcome=PASSED` 但 `blocking_count=1`"——而**读端点通常只看 outcome** ⇒ 阻断内容显示为通过 | 四条双向等价式 CHECK 下推数据库。用例：直连 SQL 插入 6 组矛盾组合 ⇒ 断言 `sqlstate='23514'`（只 `pytest.raises` 无法区分"真的拦住了"与"因为别的原因报错"） |
| **R7** | **新取值域在迁移里被活引用（ISS-191）** | 本包一次引入 **7 个**新取值域（`trigger_kind` / `outcome` / `rule_kind` / `status` / `category` / `severity` / `anchor_kind`）。迁移里若 `from rednote.modules.review.types import ...` 再循环它，`check_migration_model_parity.py` **恒绿**（两侧取同一活常量、同时移动、永不分叉） | 迁移里取值域**写成字面量**（草案 §一 第 4 条已写明）。⚠️ 同时注意：`rule_code` / `skip_reason` **刻意不落 CHECK**，这一条要写进迁移 docstring，否则下一个人会"顺手补上"而把开集冻成化石。证据脚本另有一条**反向断言**专守这件事 |
| **R8** | **跨包入向 FK 改变 WP29 测试夹具的行为（最隐蔽的一处）** | `self_check_findings.feedback_item_id → content_feedback_items.id` 一旦建立，`conftest.drop_own_tables` 的"只删没有跨包入向 FK 的表"算法会把 `content_feedback_items` 从**删**变成**留**。于是 WP29 夹具不再重建它，改由 `create_all` 保证存在性——而 `create_all` 对"表已存在但结构是旧的"**静默跳过**。将来 WP29 的表结构若变，它的测试会**跑在旧结构上且全绿**。注意 `test_feedback_wp29.py` 那条 `fks_after == fks_before` 断言**抓不到这件事** | ① **已实测**（见 §2.6 与交付报告）：`dropped` 从三张变两张、`kept=['content_feedback_items']`；② 本包有一条判据钉住这个**前提**（`test_cross_package_inbound_fk_on_feedback_items_exists`）；③ 待主 agent 在 `test_content_wp26.py` 的 `_KNOWN_CROSS_PACKAGE_FKS` 登记并同步 **RG-130** |
| **R9** | **读端点上的隐式写副作用** | `get_effective_limits` 的 `materialize` 默认 `True`，而它是"读路径上的写副作用"。自检端点若用默认值，一个 `VIEW` 权限的读请求会往 `runtime_settings` 写一行继承副本；批量自检会放大成 N 次写 | `planning_bridge` 把 `materialize=False` **写死**（不做成参数 —— 做成参数就意味着某个调用点可以传 True）；用例断**具体行数不变**，并另断"这个键在本库确实未配置"（否则 0 变化可能只是因为它本来就有值） |
| **R10** | **测试数据体量超阈值（WP-DEBT-05 刚清偿的那条债）** | 本包新增两个测试文件，若各自铺底且不清理，就是新的体量贡献者 | ① 建表夹具与造数助手**复用** `test_content_wp26` 的既有夹具（不重写第二份种子）；② module 级建表一次，function 级只清本包三表 + `content_feedback_items`；③ 收口跑 `check_test_data_volume.py` |
| **R11** | **实现期擅自加模型调用** | 一旦某条软规则"顺手"调模型：① 绕过网关 = ADR-10 违规；② 不预占 = 接缝 §5.7 的硬条款；③ 默认部署 `LOCAL_ONLY` 下当场被拒 ⇒ 规则从"遇到缺口就停"退化成"永远停"，而这与 `SKIPPED` 在响应里**长得一样** | ① MVP 注册表里**没有**任何 `evaluator` 触达网关；② AST 判据：本包 14 个源文件不得 import 含 `gateway` / `prompts` 的模块，也不得调用 `dispatch_call` 等分发原语；③ 另一条 AST 判据钉住 `evaluators.py` 的**纯度**（只许 import 本包 `types`；不得出现 `execute`/`flush`/`commit`/`now` 等调用；每个公开函数的第一个参数必须是 `subject`；公开函数数必须等于注册表条数） |
| **R12** | **注册表里混进非纯函数的 evaluator**（本包实现期新增的一条） | 有人可以在 `service.py` 里写一个会连库的闭包塞进注册表，而 R11 的 AST 判据只看 `evaluators.py`，查不到它 ⇒ "规则逻辑是纯函数"这条纪律有了旁路 | 一条判据遍历 `rules.RULES`，断言每个 `evaluator.__module__ == evaluators.__name__` |

### 1.7 数据结构、接口、前端、后端怎么拆？

| 层 | 落点 | 内容 |
|---|---|---|
| 值域 / 边界 | `review/types.py` | 8 类分类、3 档严重度、3 种锚点、2 种规则种类、4 种规则状态、4 种轮次结论、2 种触发方式、5 个受控 `skip_reason`、`RULE_SET_VERSION`、6 个长度上限、`REASON_*`、5 个冻结 dataclass（`CheckSubject` / `RuleOutcome` / `FindingDraft` / 三个快照）。**不 import 任何 `rednote.modules.*`** |
| 规则身份 | `review/rules.py` | `RuleSpec` 冻结 dataclass + `RULES` 元组（**唯一真值**，11 条）+ `by_code` / `by_category` / `all_codes` + 导入期四条 assert |
| 规则实现 | `review/evaluators.py` | **纯函数**：`(CheckSubject) -> RuleOutcome`，零 IO / 零 session ⇒ 可无库单测 |
| 存储 | `review/models.py` / `review/ddl.py` | 3 张不可变表 + 30 条 CHECK + 8 条 FK + 1 条唯一约束 + 8 个索引 + 5 个触发器（3 不可变 + 2 一致性） |
| 取数 | `review/repository.py` | 本表写读 + 跨模块**裸 SQL 只读**（当前版本、媒体位与其当前版本的产物规格、生效基线与 claim 集合、生成时用的基线、同内容未解决的 AI 反馈） |
| 跨模块接缝 | `content_bridge.py` / `media_bridge.py` / `feedback_bridge.py` / `planning_bridge.py` | 只转发，不写业务逻辑，不 catch |
| 用例 | `review/service.py` | 6 个函数：一次装配 `CheckSubject` → 跑注册表 → 落三表 → 阻断项落 AI 反馈 |
| 装配 | `review/dependencies.py` | 本包仓储 + 三个"构造对方仓储去调对方公开 service"的别名（带本包前缀） |
| 契约 | `review/schemas.py` | 2 个请求体（`extra="forbid"`）+ 10 个响应体（camelCase alias） |
| 接口 | `review/api.py` | 6 端点、两步骤鉴权、**零 `If-Match`** |
| 前端 | `static/content-feedback.html` 的 8 类卡片 + 列表页 `AI 自检` 列 | 本包只提供数据与控制码 |
| 判据 | `tests/test_review_wp30.py` + `tests/test_review_wp30_api.py` | 阳性 / 阴性对照成对、取值域闭集、跨包 parity、三表 CHECK / 触发器 sqlstate、服务层编排、六端点精确状态码 |

### 1.8 至少输出 3 个版本方案

见 §3（V1 否决 / V2 否决 / V3 推荐并已实施 / V4 不选）。

---

## 2. 阶段 2：方案设计

### 2.1 需求本质（一两句话）

**让"这一篇稿子合不合规"成为一组有身份（`rule_code`）、有种类（`HARD`/`SOFT`）、有结论（`PASSED`/`FAILED`/`SKIPPED`/`ERRORED`）、有坐标（段 / 图 / 整篇）、有机器可判证据（`evidence`）、绑定具体版本（`subject_version_id`）、绑定具体规则集（`rule_set_version`）的不可变观测记录；并让其中的阻断项**自动成为 WP29 既有审核闸的输入**，而不是第二道闸。**

### 2.2 用户画像

| 画像 | 场景 | 本包的证据 |
|---|---|---|
| 逐条审核的运营 A | 打开一条内容，点"跑自检"；看到「事实 · 1 阻断」并直接跳到第 2 段 | ① 的 201 响应：`findings[].anchorKind=PARAGRAPH` + `paragraphRef` |
| 不信任 AI 的审核者 B | 想知道"它说通过，到底判了几条" | ③ 的 `ruleResults[]` 逐规则一行 + `skippedCount` 单列 + 每条 `skipReason` |
| 改稿的运营 C | 按自检结果改完正文，重跑一次 | ① 追加第二轮（不是覆盖）；第一轮在 ② 里被标 `STALE` 但**条数不减** |
| 批量处理的运营 D | 勾选 10 条"批量自检" | ④ 的逐条结果（哪几条成功、哪几条为什么失败） |
| WP31 的关口 | "这条内容自检过没有、结论是什么" | 裸 SQL 读 `self_check_runs`（**不 import 本包**），由主 agent 在接缝文档登记只读接缝 |
| WP33 的质量报告 | "这条规则历史上命中过多少次" | 三张不可变表 + `rule_set_version` 快照（规则改版后历史仍可解释） |

### 2.3 核心功能清单（MVP）

**11 条规则，覆盖全部 8 类**（5 类可判 + 3 类显式未判）：

| 类 | 规则 code | kind | 默认严重度 | 读点 / 判法 | 为什么不恒绿 |
|---|---|---|---|---|---|
| **FACT** | `FACT_BINDING_CLAIM_OUT_OF_SCOPE` | HARD | `BLOCKING` | 当前版 `bindings[].claimId` ⊄ **当前生效基线**的 claim 集合（`projects.active_baseline_id` → `production_baselines.brief_version_id` → `brief_claims`） | 写入期校验用的是**写入当时**的基线；基线之后可以前进（WP22 R1 的实测次场景：9-15 批准 62%、9-20 知识更正为 60%）⇒ 自检时重判是新信息。`evidence` 同时给 `generatedAgainstBaselineId` 与 `effectiveBaselineId`，两者不同就是"基线前进过"的机器可判证据 |
| | `FACT_UNBOUND_INCLUDED_CLAIM` | HARD | `WARNING` | 生效基线的 claim 里，有哪些**一条 binding 都没引用** | 生成只绑用到的 claim，漏绑是常态（`validate_bindings` 反过来还有一条"绑满全部 claim = 视为没绑"）⇒ 能命中 |
| **STRUCTURE** | `STRUCTURE_PARAGRAPH_COUNT_BELOW_IMAGE_PLAN` | HARD | `WARNING` | 当前正文段数 < 3（经 `media_bridge` 切段） | 契约 §4：段数 <3 时多张图回落同一段，合法但退化 ⇒ 可命中 |
| | `STRUCTURE_VERSION_WITHOUT_BLUEPRINT` | HARD | `INFO` | 当前版 `blueprint_id IS NULL` | 可空列 + 无 CHECK ⇒ 真能为 NULL（人工编辑追加的版本常见） |
| **MEDIA_CONSISTENCY** | `MEDIA_PARAGRAPH_REF_STALE` | HARD | `WARNING` | 每个 `media_items.paragraph_ref` ∈ 当前段 ref 集合？不在 ⇒ 逐图一条 finding（`anchor_kind='MEDIA'`） | `paragraph_ref` **无 FK、不落唯一约束**；正文一改就会失配 ⇒ 高频命中 |
| | `MEDIA_ITEM_NOT_READY` | HARD | `WARNING` | `media_items.state != 'READY'`（判"不是 READY"而不是"是 PENDING 或 FAILED"——后者在 media 多一个状态时会静默漏判） | PENDING/FAILED 是真实状态（原型「Live 3 生成中断」「失败」） |
| **TECH_FORMAT** | `TECH_RENDITION_OUT_OF_PLATFORM_LIMITS` | HARD | `WARNING` | READY 产物的 `width`/`height`/`byte_size` 对比 `platform.mediaLimits`；**未配置 ⇒ SKIPPED** | 阈值配上后能命中；未配时**不冒充通过**（OP-64 仍 OPEN） |
| **OTHER** | `OTHER_BANNED_TERM_HIT` | SOFT | `WARNING` | 本包代码常量词表（8 个促销式号召 / 绝对化表述），逐段扫，命中落 `anchor_kind='PARAGRAPH'` | 词表非空 ⇒ 可构造命中。对应原型「禁用词检测 0 命中」 |
| **TONE** | `TONE_MODEL_JUDGEMENT` | SOFT | （不适用） | **恒 SKIPPED(`MODEL_RULE_NOT_ENABLED`)** | 见下方"为什么保留恒 SKIPPED 的规则" |
| **DUPLICATE** | `DUPLICATE_CROSS_ITEM_SIMILARITY` | SOFT | （不适用） | **恒 SKIPPED(`CROSS_ITEM_SCOPE_OUT_OF_PACKAGE`)** | 同上（归 WP33） |
| **SCENE** | `SCENE_LINKAGE_UNAVAILABLE` | HARD | （不适用） | **恒 SKIPPED(`NO_SCENE_LINKAGE_IN_SCHEMA`)** | 同上（schema 无"内容→场景"关联列） |

**为什么保留三条"恒 SKIPPED"的规则，而不是干脆不注册**：
不注册的后果是那一类**没有任何行** ⇒ 响应里 8 张卡片变 5 张，或者前端把"没有行"渲染成"通过"。一条明确报告"我没判、原因是 X"的规则**是防线**；一条缺席而被读成通过的规则是**假绿**。这与"无'声明了但一行都没实现'的常量"不矛盾——它们**有实现**（返回 `SKIPPED` + 受控原因码）、**有用例**（断具体原因码）、**有响应位**（`implemented=false` 在规则目录里照常列出）。若将来 WP33 落地，改动面只有 `evaluators.py` 的一个函数，**三张表的结构一行都不用改**。

**硬 / 软的分界不是"严重度"，是"结论是否可复现"**：`HARD` = 同一输入必得同一结论（纯函数 + 库事实）；`SOFT` = 结论依赖启发式（如禁用词表），改词表就会改结论。因此 `kind` 落库（`rule_results.rule_kind`），让下游能回答"这个结论可复现吗"。严重度是**另一维** —— 一条 `SOFT` 规则也可以 `BLOCKING`，反之一条 `HARD` 规则可以只 `INFO`。两维刻意不合并：合并的表现是"把软提示当硬门"或"把硬错降成提示"，而 `quality-view.html:318` 原文正警告这一点：「软提示，不作为唯一质量标准」。

**一次自检的执行顺序（固定，不可调换）**：

```
1. content_bridge.read_edit_target(content_id)
      —— 一次读拿到 project_id / current_version_id / version_no /
         title / body / tags / bindings / blueprint_id（跨快照拼装的禁令）
2. repository 批量只读装配 CheckSubject（4 次查询，全部以第 1 步的
   current_version_id / body 为锚点展开）：
      · media_items + 其当前版本的产物规格（1 次 LEFT JOIN）
      · 当前生效基线 + 它的 claim 集合
      · 这一版生成时用的基线
      · planning_bridge 读 platform.mediaLimits（materialize=False）
3. media_bridge.split_body_into_paragraphs(body) → 段落快照（零切段实现）
4. 遍历 rules.RULES：逐条调 evaluator（纯函数）
      · 任一 evaluator 抛异常 ⇒ 该条 status=ERRORED + error_note，
        **不中断整轮**（一条规则崩掉不该让其余 7 类全部无结论）
5. 汇总五个计数 + 按定序算 outcome（BLOCKED > ERRORED > WARNED > PASSED）
6. 落 self_check_runs（flush）→ self_check_rule_results（flush）
7. 对 severity=BLOCKING 的 finding：去重后落 AI 反馈，
   再插 finding 并把 feedback_item_id **一起写进去**
8. flush()，**不 commit**（提交边界归 session_scope）
```

第 2 步"一次装配"与第 4 步"纯函数求值"的分离是本包可测性的**全部**来源：`evaluators.py` 的测试**不需要数据库**（R1）。
第 7 步"先建反馈、再插 finding"是因为三张表**全部不可变**：先插 finding 再回填 `feedback_item_id` 就是对不可变行的 UPDATE（`23001`）。

### 2.4 "关键事实问题未解决不能通过"落在哪一层

**落在 WP29 的审核服务层，WP30 不新增任何一层。** 实测链路：

```
WP30: run_self_check()
  → 规则命中 severity=BLOCKING
  → feedback_bridge → feedback.service.create_feedback(
        author_kind='AI', severity='BLOCKING', category=<8 类之一>,
        anchor_kind/paragraph_ref/media_item_id=<finding 的锚点>,
        observed_version_id=<run.subject_version_id>, scope='CURRENT_ONLY')
  → 落 content_feedback_items（state 默认 OPEN）+ 同事务首行事件
  → finding.feedback_item_id 回指（**插入那一刻**就是终值）

WP29（已存在，一行不改）: POST /contents/{id}/reviews  decision=APPROVED
  → feedback/service.py:872-888
  → 409 INVALID_TRANSITION
     details = {reason:'UNRESOLVED_BLOCKING_FEEDBACK',
                contentItemId, blockingFeedbackIds[]}
```

**为什么必须这样接，而不是在 WP30 自己判一次**：
① 原型那句话的消费点有**两个**（逐条"标记已通过" + 扩产关卡「无待处理阻断意见」，后者属 WP31）。若 WP30 自己判一次，全系统就有三份"什么叫未解决的阻断"，而三份之间的分歧**不会报错**——只表现为"自检说能过、审核说不能过"。
② `author_kind='AI'` 这条路是 WP29 **设计时就留好的**：`WP29-...说明.md:149` 画像表原文「AI 自检（WP30，未来）｜ 服务层 `authorKind=AI`；**不经过 HTTP 端点**」。本包就是兑现那句话 —— 调的是 service 函数，不是 HTTP 端点。

**本包因此必须解决的两个连带问题**：
- **去重**（R4）：`content_feedback_items` 没有 `rule_code` 列（也不该加——别人的表）⇒ 去重键只能落在**本包侧**：查 `self_check_findings JOIN self_check_rule_results JOIN self_check_runs JOIN content_feedback_items`，取"同内容、同 `rule_code`、同锚点，且反馈仍 `author_kind='AI'` 且未解决"的集合。命中则**复用既有反馈、不新建**，新 finding 的 `feedback_item_id` 指向同一行。
- **AI 不推进状态**：本包**只建不改**反馈。`advance_feedback` 要 `If-Match`，而机器持有 ETag 去抢人工工作区的锁没有业务收益。⇒ **规则下一轮不再命中时，反馈不会自动关闭**，由人处置。这一条见 §2.6 的已知取舍（否则运营会以为"改好了意见会自动消失"）。

### 2.5 本包为什么**零** `If-Match`

三张表全是追加写，没有"原地改"的路径 ⇒ 没有乐观锁载体，也没有"读-写窗口"要挡。
`POST /contents/{id}/self-checks` 并发两次的结果是**两轮**自检记录，这是**正确行为**（不是冲突）。
唯一要防的是"同一阻断项被两轮各建一条反馈" ⇒ 靠 §2.4 的去重 + `(run_id, rule_code)` 唯一约束，不靠 ETag。
**这一条必须显式写出来**，否则会被当成"漏了并发保护"。

### 2.6 已知取舍与缺口（**逐条如实登记，不含糊**）

| # | 取舍 / 缺口 | 代价 | 为什么接受 |
|---|---|---|---|
| 1 | **3 类恒 SKIPPED**（`TONE` / `DUPLICATE` / `SCENE`） | Demo 画面上 8 张卡片有 3 张显示"未判"而不是"通过" | 备选只有两条且都更差：伪造通过（假绿，直接违反"缺失不填 0"）或 8 类只做 5 类（卡片缺 3 张，前端会把缺席渲染成通过） |
| 2 | **`skipped_count` 不参与 `outcome`** | 一轮可以是 `outcome=PASSED` 而同时有 6 条规则没判 | 未判既不是通过也不是不通过。响应体把五个数全给出来，判断"这算通过吗"的权力留给调用方（关口 / 看板 / 人） |
| 3 | **`TECH_FORMAT` 不判 `image_format`** | 「JPG」这一项无法机器判定 | `runtime_settings` 的 value 形状是 `{键: 数 \| null}`（`settings_service` 的 `_normalize_numeric_mapping` 强制），**承载不了字符串集合**。要判它必须先扩 planning 的 value 形状，而那是 WP31 的写面 |
| 4 | **平台限制未配置时整类 SKIPPED** | OP-64 若在收口时仍 `OPEN`，Demo 上 `TECH_FORMAT` 也显示"未判" ⇒ **8 类里 4 类可判 + 4 类未判** | 在本包内置 1440×1920 / 2MB 就是造第二处平台限制真值，而两处真值的分叉是静默的 |
| 5 | **规则结论不可改，改的是它落成的那条反馈** | 与原型「分类可由运营修改」的字面不完全一致 | "改分类"的落点实测在 WP29 的反馈行（`suggested_category` 与 `category` 分两列）；`rule_results` 是不可变观测，改它会让"那一刻规则怎么判的"不可溯源。⚠️ **需产品确认** |
| 6 | **AI 反馈不自动关闭** | 规则下一轮不再命中时，上一轮建的那条 `OPEN` 反馈仍在待处理列表里，由人处置 | 机器持有 ETag 去抢人工工作区的锁没有业务收益；且"自动关闭"会让运营失去"这个问题被谁确认解决了"的记录 |
| 7 | **`rule_code` / `skip_reason` 不落数据库 CHECK** | 数据库对这两列的取值**一个字都不管**；唯一守卫是服务层的 `validate_rule_result`（未知值 422，不落库） | 它们是**会增长的开集**。若落 CHECK，按 ISS-191 必须写成字面量化石 ⇒ 每加一条规则要一次迁移，且化石与注册表的分叉**不会报错**（新规则的结果静默写不进去）。证据脚本另有一条**反向断言**专守"没人顺手补上" |
| 8 | **`evaluated_count >= 1` 是硬 CHECK** | 理论上若**所有** 11 条规则同时 `SKIPPED`/`ERRORED`，这一轮将无法落库（请求 500） | 两条结构规则（段数、蓝图）是纯函数且无前置条件，不可能 SKIPPED ⇒ 实践中不可达。保留它是为了让"空转轮次"（自检过了但什么都没判）无法落库 |
| 9 | **`feedback_item_id` 加了跨包入向 FK** | 改变 WP29 夹具的 drop/keep 语义（R8，**已实测**：`dropped` 从三张变两张、`kept=['content_feedback_items']`） | 引用完整性是审核闸那条链的最后一道（没有 FK 时，一条 finding 可以指向一条被删掉的反馈）。夹具语义的变化只要**显式登记**就不是静默失败 —— 因此本包交付报告与本文档都写明，并由主 agent 在 `_KNOWN_CROSS_PACKAGE_FKS` + RG-130 登记 |
| 10 | **`list_latest_runs` 不为未自检的内容返回行** | 前端必须把内容列表左连接本结果，缺失的渲染成「未自检」 | 为未自检的内容编造一行（哪怕 outcome 为空）会让"未自检"与"自检过"在数据结构上同形 —— 而那正是本包要防的那一类 |
| 11 | **批量自检不校验 `contentIds` 是否属于本项目** | 传入别的项目的内容时，那一条在它自己的 savepoint 里失败并在 `results[].error` 里报出 | 越界不会得逞：轮次的 `project_id` 取自**内容自己的**归属，而 `trg_self_check_runs_consistency` 要求"内容必须属于本轮的项目"⇒ 坏数据落不了库，而不是静默写进本项目的看板 |

---

## 3. 阶段 3：多版本方案

### 方案 V1 —— 自检结果塞进 WP29 的反馈表（**否决**）

做法：不建新表。自检直接往 `content_feedback_items` 写 `author_kind='AI'` 的行；"8 类卡片"按 `category` 分组计数得出。

否决理由（四条，逐条可验）：

1. **"通过"与"没判"无法区分**。反馈表只存**命中**，不存"这条规则跑了且没问题"。于是「语气 · 通过」与「语气 · 本轮没判」在数据上**完全同形**（都是零行）——这正是接缝文档 §6.3 第 7 条「**缺失不填 0**；NULL / 0 / 约数三者分离」要防的形态，也是原型卡片「通过 · 0 项」与「未自检」两态的差别所在。
2. **没有"轮次"概念**，于是"这次自检判的是哪一版、什么时候判的、用的哪版规则"三件事都没有落点。重跑一次就只能靠 `created_at` 区分，而「按某个恰好存在的列排序不是排序」是本仓已记的教训（ISS-52 同款）。
3. **污染人工工作区**。`content_feedback_items` 是**可变**表（`lock_version` 是 ETag 载体），运营要在上面推进四态机。把每轮几十条 `INFO` 级规则结果灌进去，运营的待处理列表会被机器噪声淹没；而 WP29 的 `anchorState` 设计明确要求"条数不因判定而减少"，所以也没法靠过滤解决。
4. **改不动**。真要这么做，得给 `content_feedback_items` 加 `rule_code` / `rule_set_version` 等列 ⇒ 改 `modules/feedback/models.py` + 迁移 ⇒ 那是别人已交付的模块，分支禁写。

### 方案 V2 —— 规则存配置表 + 表达式求值器（**否决**）

做法：建 `review_rules` 表（`code` / `category` / `severity` / `expression` / `enabled` / `threshold` JSONB），服务层读表、对表达式求值。

否决理由（五条）：

1. **自造 DSL = 自造解释器**。要么用 `eval`（直接的代码执行面），要么写一个小语言 + 词法 / 语法 / 求值三层，而这三层自己又需要一套判据。本仓没有任何表达式求值先例可循。
2. **迁移必须是化石，而规则表要活**。接缝文档 §4.5（ISS-191）明令「迁移里的取值域必须写成字面量」。规则码若进 CHECK 就冻成化石、每加一条规则要一次迁移；若不进 CHECK 就没有数据库层约束——两头都不舒服，而配置表方案把这个两难放大到"规则逻辑本身"。
3. **"有哪些规则"变成不可静态回答的问题**。于是"每条规则都有阳性对照用例"这条判据（本包的核心防线，R1）**无法机械对账**——判据得先连库读表才知道该测什么，而库里的内容随环境变。代码注册表反过来天然可对账（`RULES` 元组 ⇄ 三张用例表的集合相等断言）。
4. **`enabled` 开关会空转**。没有任何已知消费场景要求"关掉某条硬规则"；一个从不被改的开关列就是 ISS-95 族。
5. **配置真值已有唯一落点**。要可配，正确的地方是 `runtime_settings`（`get_effective_limits` 是"全仓唯一的配置读取路径"），不是本包再开一张。

### 方案 V3 —— 代码规则注册表 + 三张不可变表 + 阻断项回灌 WP29 既有闸（**推荐，已实施**）

| 组成 | 落点 |
|---|---|
| 规则身份与策略（code / category / kind / 默认严重度 / 锚点粒度 / 是否已实现） | `review/rules.py` 的 `RULES` 元组（**唯一真值**，11 条） |
| 规则逻辑 | `review/evaluators.py` 纯函数，输入是一次装配的 `CheckSubject` 快照 |
| 一轮自检 | `self_check_runs`（不可变，带 `subject_version_id` / `rule_set_version` / 四条 outcome 等价式 CHECK） |
| 逐规则结论 | `self_check_rule_results`（不可变，四态 + 四条双向配对 CHECK，`(run_id, rule_code)` 唯一） |
| 逐命中 | `self_check_findings`（不可变，锚点三选一，`evidence` 必须是 JSON 对象，`feedback_item_id` 回指） |
| "关键事实未解决不能通过" | **不新建**。阻断项 → `feedback_bridge` → WP29 `decide_review` 的既有 409 闸 |
| 分类 / 严重度 / 锚点 / scope / actorKind 口径 | 照抄 WP29 的**值**，名字用本包前缀，跨包 parity 测试钉住 |
| 平台限制 | 走 `planning_bridge`（`materialize=False` 写死）；未配置 ⇒ SKIPPED |
| 段落坐标 | `media_bridge` 转发，**本包零切段实现** |

推荐理由（四条，每条都是可反向验证的问题）：

1. **"它说通过，到底判了几条？"有答案。** `rule_results` 逐规则一行 + `skipped_count` 独立计列 + outcome 四条等价式 CHECK。V1 答不出，V2 答得出但答案随库变。
2. **"关键事实未解决不能通过"只有一处实现。** 实测它已在 `feedback/service.py:872-888`。本方案让 AI 阻断项成为那道闸的输入，于是**第二道闸不存在** ⇒ 不可能出现"自检闸放行、审核闸拒绝"或反之的分歧。V1 同样复用这道闸但失去轮次；若本包自己再判一次就是 ISS-137「同一事实两种写法」。
3. **规则集合可被机械对账。** `RULES` 是导入期可枚举的元组 ⇒ "每条规则都有阳性 + 阴性对照"这条判据能真跑；也能断言"注册表无重复 code"、"每个 category 至少一条规则"、"每条 evaluator 都住在 `evaluators.py` 里"（活性下界）。
4. **代价已登记。** 三张表、30 条 CHECK、5 个触发器、4 个 bridge 文件、一条跨包入向 FK；换来的是"未判不冒充通过 + 历史可解释 + 闸只有一道"。

### 方案 V4 —— 完整版：模型自检 + 跨篇相似度 + 规则学习闭环（**不选**）

不选理由：① 模型路径被 ADR-10（默认 `LOCAL_ONLY` 无回退）+ E7 外部额度双重阻塞，且需要本包自造计价次数换算；② 跨篇相似度是 WP33 的分界面，实现即触发仲裁项④ 的"两边各建一份"；③ 学习闭环（`quality-view.html` 的七问提案 + 四类评测集 + 盲评）本身是独立里程碑，其验证标准就是 AQ-01，而 AQ-01 当前"❌ 无基线"；④ 5~8 人日装不下其中任何一项。

### 3.5 方案选择与理由（一句话）

选 **V3**。判据是三个**可反向验证**的问题：

1. **"没判"能不能与"通过"分开？** V3 能（逐规则一行 + `skippedCount` 单列 + 8 类聚合从取值域起算）；V1 不能；V2 能但不可机械对账。
2. **"不能通过"这条产品规则有几份实现？** V3 零份新增（复用 WP29 那一份）；V2 两份。
3. **"有哪些规则"能不能在不连库的情况下回答？** V3 能 ⇒ 阳性对照判据成立；V2 不能 ⇒ 本包最核心的防线失去载体。

### 3.6 本包对接缝冻结核定点的回答

| 接缝冻结条目 | 本包的回答 |
|---|---|
| §5.12 content 读接缝（`EditTarget` / `read_edit_target`） | ① 只用 `read_edit_target`（一次读，身份侧 + 版本侧同快照）；签名未改；**不用** `append_edited_version`（自检不改稿） |
| §5.1 正文切段口径（三处实现，禁第四处） | `media_bridge` 只转发 `paragraph_refs_for_body` / `validate_paragraph_ref`；本包零切分代码。实测 `check_paragraph_split_parity.py` 的 `_IMPL_FILES` **仍为 3 个** |
| §5.4 `modules/media/**` 只读 | 本包不写 media 任何文件；只经纯函数桥接 + 裸 SQL 只读 |
| §5.6 `modules/planning/**` 是 WP31 的写面 | 本包**只读**调 `get_effective_limits`，**不新增 `SETTING_KEYS`** |
| §5.7 "出活前必须预占" | 本包不调模型、不出活 ⇒ 无预占面。AST 判据钉住"不得 import gateway / prompts" |
| §4.5 / ISS-191 迁移取值域字面量 | 本包无迁移文件；草案 §一 第 4~5 条已写明 7 个新取值域必须写字面量，且 `rule_code` / `skip_reason` **不得**落 CHECK |
| §5.2 `register_version` 幂等语义 | 本包**不注册版本节点**（自检是观测，不是可引用版本；`ENTITY_TYPES` 不加值） |
| 权限动作（不新建 `ProjectAction`） | 只用 `VIEW` / `REVIEW` |
| 仲裁项④（WP30 vs WP33） | **分开**，分界是"输入是一篇还是一批"。相似度算法**一行都不在本包**；WP33 落地后只改 `evaluators.py` 的一个函数，三张表结构不变 |

### 3.7 请主 agent 在收口期处置的点

1. **共享文件装配**（分支一行都没改）：`main.py` 注册 review router；`migrations/env.py` import review models；建迁移（接在 `efa306a18363` 之后）；重导出 `contracts/openapi.json`（预期 86 → **90 paths**、106 → **112 ops**）；`tools/m1/rerun_all_evidence.py` 的 `MIN_SCRIPTS` **+1**。
2. **R8 的登记**：`test_content_wp26.py` 的 `_KNOWN_CROSS_PACKAGE_FKS` 新增 `fk_self_check_findings_feedback_item_id_content_feedback_items`；同步 **RG-130**。
3. **证据脚本重定向**：`tools/m3/collect_wp30_evidence.py` 当前指向分支私有库 `rednote_test_wp30`，并把"迁移 head 不早于 WP30 引入点"这条显式 **SKIP**。迁移落地后请建 `rednote_verify_wp30`、改 `DEFAULT_DB`、把 `WP30_MIGRATION_REVISION` 填成那条 revision（`_revision_is_at_least` 已按链序比较的形态预先写好），SKIP 行即变成真正的 CHECK，`EXPECTED_CHECKS` 从 61 变 61（SKIP → CHECK，总数不变）。
4. **待定项登记**：自动触发（`trigger_kind` 不含 `AUTO`）、`PreviewPayload.selfCheckSummary` 回填、3 类 SKIPPED 的闭合条件（WP33 落地 / 模型额度到位 / schema 加"内容→场景"关联列）、`TECH_FORMAT` 不判 `image_format`（需扩 planning 的 value 形状）、标题禁用词检测、"未自检不许批量通过"是否要后端强制（原型有此提示语，但 WP29 的 `decide_reviews_batch` 只查阻断反馈、不查"有没有自检过"——本包不实现它）。
5. **接缝文档补 §5.13「WP30 的只读接缝」**：登记四个转发点及其签名 —— `content.service.read_edit_target`、`media.schemas.paragraph_refs_for_body` / `validate_paragraph_ref`、`feedback.service.create_feedback`、`planning.settings_service.get_effective_limits`（登记后不得改签名）；并注明"`paragraph_ref` 从 media 纯函数层取，本包零切段实现"。
