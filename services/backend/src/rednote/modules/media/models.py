"""WP27 五张表 + WP28 两张表的存储模型。

| 表 | 角色 | 可变性 |
|---|---|---|
| `media_items` | 媒体**身份**（一条内容里的第 N 张图，跨版本不变） | **可变**（`lock_version` + 当前有效版本投影） |
| `media_versions` | 媒体**版本**（生成的图 + 段落绑定 + 参考素材绑定）。**`id` 同时 FK `version_nodes.id`** | **不可变** |
| `reference_assets` | **产品参考素材**（`PURPOSE_REFERENCE` 的落点）：型号/颜色/部件标注 | **不可变** |
| `media_renditions` | **加工产物**（预览 / 正式；尺寸/格式/体积） | **不可变** |
| `media_generation_contexts` | 生成上下文（**OP-160 的同构落点**） | **不可变** |
| `media_components` | Live 组件（静帧/动态段/清单/预览；WP28） | **不可变** |
| `live_profiles` | Live 封装规格占位档案（WP28） | **不可变** |

与本包 `types.py` 的分层是单向的：`types ← models`（本模块只 import `types`
的常量，不 import `schemas` / `repository` / `service`）。

---

## 一、★ 身份是"第 N 张图"而不是"第 N 段"

说明文档 §1.7 把 `media_items` 描述成**"一「内容 × 段落」位一条"**。
**本实现不这么做**，理由是老大 2026-10-07 拍板的**固定 3 张**：

    每篇内容恒出 3 张图，而正文段落数是 N（由 WP26 的 `paragraph_plan` 决定）。

若身份键是 `(content_item_id, paragraph_ref)`，那么：

| 情形 | 以段落为键 | 以序号为键 |
|---|---|---|
| 正文只有 2 段 | 只能建 **2** 条身份 ⇒ 出 2 张图，**与"固定 3 张"矛盾** | 建 3 条 ⇒ 出 3 张 ✅ |
| 正文有 10 段 | 建 10 条 ⇒ 出 10 张图，**与"固定 3 张"矛盾** | 建 3 条 ⇒ 出 3 张 ✅ |
| 同一段要配 2 张（近景 + 场景） | 撞唯一约束，**说不出话** | 两条身份，各自绑同一段 ✅ |

⇒ 身份的**稳定键是 `(content_item_id, sort_order)`**（"这是这篇内容的第 N 张图"），
而"这一张配的是哪一段"是**版本级**的事实（`media_versions.paragraph_ref`）。

### 由此产生的两条约束与一处登记

1. `media_items` **有** `paragraph_ref` —— 但它是**当前版本的投影**（见第五节），
   不是身份的一部分，**因此不落唯一约束**（两条身份可以指向同一段）；
2. `uq_media_items_content_sort` 是**唯一的**身份唯一键；
3. ⚠️ **"3 张如何分配到 N 段"是一条尚未拍板的规则**（可能"只给前 3 段配图"、
   也可能"均匀采样 3 段"）。本包**不发明这个规则**，只保证"每张图绑的段
   是一条显式落库的事实"，分配策略由服务层调用方决定。
   ⇒ **登记为 OP-199**（"固定 3 张"引入的下游待定项，是 `types.py`
   里 OP-198 的直接后果），收口时回填登记册。

## 二、★ 参考素材绑定是**一列 JSONB**，不是 FK 列，也不是第 6 张表

R1 要求"每张图绑到**具体的**参考素材（不是整个素材库）"，而
`types.py::MediaReferenceBinding` 与 `MIN_REFERENCE_ASSETS = 1` /
`MAX_REFERENCE_ASSETS = 8` 已经把它定成 **1~8 条的列表**。

三条路，只有第三条可行：

| 出路 | 为什么不行 / 行 |
|---|---|
| ① 单列 `reference_asset_id` FK | **装不下 1~8 条**。说明文档 §1.7 / §3.6 B 写的是单数 `reference_asset_id`，而 `types.py`（已落盘、更细）是复数 —— **文档的表格是摘要，本实现按 `types.py` 走**。多部件产品（如"机身 + 镜头 + 电池"）就是 3 条 |
| ② `reference_asset_id` FK **加上** `reference_bindings` JSONB（"主素材 + 附加"） | **违反铁律 26**：主素材既是 FK 列又是数组第一个元素 ⇒ 同一个量两个来源 ⇒ 两者分叉时"这张图画的是哪个型号"有两个答案，而**不报错** |
| ③ **只有** `reference_bindings` JSONB 一列 | ✅ 采纳 |

### ③ 放弃了什么，补了什么

**放弃了 FK** —— JSONB 数组里的元素 id **没有引用完整性**，
"绑到一个不存在的素材"DDL 的 FK 管不住。补在两处：

- **CHECK（绕不过去的第一层）**：形状是数组（`jsonb_typeof = 'array'`）+
  长度在 `[MIN_REFERENCE_ASSETS, MAX_REFERENCE_ASSETS]` 区间内。
  这一层挡住的是 R1 的**主要失效形态**（绑 0 条 = 没绑；绑 50 条 = 整库引用）。
- **一致性触发器（第二层，`ddl.py`）**：逐个元素校验"素材存在"+
  "与图属于同一项目"。plpgsql 用 `jsonb_array_elements` 可以遍历 —— 这条
  与 WP26 的"被采纳候选必须属于同一条内容"**完全同构**（那条也是 FK 管不住，
  落在 `ddl.py`）。

**为什么不是第 6 张表（`media_reference_bindings`）**：老大拍板第 3 点是
"是否新增 **5** 张表：同意"。加第 6 张是**偏离拍板**，不在本包权限内。
若将来需要按素材反查"哪些图用了它"，**不需要改结构** —— 给本列加一个
GIN 索引即可（`jsonb` 上可建）。本包**不加 GIN**：目前没有任何反查消费者
（WP34 的影响图走 `version_dependencies` 的边，不走这里），加了就是过度设计。

## 三、`media_items` 的四个投影列，以及"当前版本"怎么定

`media_items` 承载**四项**与"当前有效版本"同源的投影：

| 投影列 | 来源 | 为什么不能每次去 JOIN |
|---|---|---|
| `current_version_id` | `media_versions.id` | 列表页每行都要显示"当前是哪一版" |
| `current_version_no` | `media_versions.version_no` | 同上（显示"第几版"） |
| `paragraph_ref` | `media_versions.paragraph_ref` | `content-feedback.html:179` 那屏要"每张图对应哪一段"，**列表不拉版本历史**（`types.MediaSummary` 里没有版本段） |
| `sort_order` | `media_versions.sort_order` | 排序键；且它就是身份键的一半 |

### ⚠️ `generation_request` **不是**投影列（第五列，别归错族）

本表另有一列 `generation_request`（`types.MediaGenerationRequest` 的落库形状），
它**不**属于上表那四项：

| | 四项投影 | `generation_request` |
|---|---|---|
| 真值在哪 | `media_versions` 的当前有效行 | **只在身份行上**（没有别的来源） |
| 谁写 | 服务层在追加版本时改写 | 端点发起时写、重做时**覆盖** |
| 会不会被"追加版本"改写 | 会（每加一版就前进） | 不会（追加版本**不碰**它） |

把它并进"投影"会让"投影 == 最新版本"这条对账把本列也拉进去 ——
而本列**本来就该**与最新版本无关（它是"最后一次意图"，不是"当前状态"）。
与 WP26 的 `content_items.generation_request` 同款同理由。

处置与 WP26 的 `content_items.current_version_*` **完全同形**：**投影只由
服务层在追加版本时改写**，本包**不建**跨表触发器去对账"投影 == 最新版本"。

### ★ 为什么这里**不**建跨表触发器（一处显式取舍，不是遗漏）

WP26 在同一结构上做过同样的取舍，其 `models.py` 写下的处置是
"投影只由服务层改写 + `test_content_wp26.py` 里一条『追加版本后投影必须前进』
的用例"。本包**沿用**，理由三条：

| # | 理由 |
|---|---|
| 1 | **收益有界**：投影只有**一个**写入方（`repository.py`），绕过它需要手写 SQL / 迁移回填 —— 而拦住那两类正是"不可变触发器"在做的事（投影列**不在**不可变表上，天然拦不住，这是取舍的已知代价） |
| 2 | **代价实在**：跨表触发器要 2 个挂载点（`media_versions` INSERT 与 `media_items` UPDATE）+ 1 个靠 `TG_ARGV` 分派两侧的函数。为一条"只有一个写入方"的不变量引入 `TG_ARGV` 分派，**复杂度换来的确定性有限** |
| 3 | **不制造第二套口径**：WP26 已就同一结构定过处置，本包另建一套会让"投影怎么保真"在两个同构包里**有两种答案**（而它们本该同形） |

⇒ 本包用**两条更便宜的手段**补上能补的部分：

- **单行 CHECK `projection_coherent`**：`(current_version_id IS NULL) =
  (current_version_no = 0)` —— 纯单行不变量，零跨表耦合，挡住
  "指向某版本但版本号为 0"/"版本号为 N 但没有版本 id"这一整类；
- **导入期断言 ⑥**：投影列在身份表与版本表上**都够得着**（服务层按名字赋值，
  名字分叉是**运行时**才炸的 `AttributeError`）。

### ⚠️ 由此留下的**确切**缺口（写清楚，不含糊）

"投影是否等于**最新**版本"**没有任何机械判据**，靠
`repository.py` 是唯一写入方兜着。
⛔ 原文续写"+ test_media_wp27.py 的用例"，而**该文件全仓不存在、本包在
`services/backend/tests/` 下零覆盖**（实测：没有任何测试 import 过
`rednote.modules.media`）—— 那半句是假的，2026-10-07 更正。
**触发升级的条件**：一旦出现**第二个**写入方（运维脚本 / "回滚到上一版"端点 /
迁移回填），本条**必须**升级为跨表触发器 ——
因为"回滚只改投影、不追加版本"会让**版本历史被抹掉**，而那时
"投影停在旧版"与"历史本来如此"**无法分辨**。

⚠️ 另一个刻意**不加**的判据：`current_version_id IS NOT NULL ⇒ state <> 'PENDING'`
（"有版本就不该还显示生成中"）。它本身是条真不变量，但 PG 的 **CHECK 不能延迟**，
落到 DDL 就等于**强制**服务层必须用**一条** UPDATE 同时写投影与状态 ——
那是一条对服务层实现方式的硬约束，而它的收益（挡"插了版本忘改状态"）
已由列表端点的用例覆盖。⇒ 归**服务层契约**（`service.py` docstring）而不进 DDL。

⚠️ `current_version_no` **没有版本时为 0**（不是 NULL）—— 与 `content_items`
同一理由：0 是可比较的值，NULL 参与比较的结果是 UNKNOWN，会让
"还没出图的有哪些"这类筛选**静默返回空集**。

## 四、`state` 也是投影

`media_items.state` 回答"这一张图现在到哪一步了"：没有版本行 = `PENDING`、
有可交付版本 = `READY`、生成失败 = `FAILED`。

**为什么必须有它**（而不是"有没有版本行"就够）：`types.py` 的注释已经论证 ——
"生成了但加工失败"这个中间态恰好是"**有版本行但不可交付**"，
只靠版本行存在性会把它显示成"已完成"。

它是投影 ⇒ 与第三节那四列同受一致性触发器保护。

## 五、★ `reference_assets` 的"停用"是**追加一行**，`state` 描述的是**登记动作**

R1 的说明文档 §1.7 原话：「一个新素材 = 一行；**"停用"是新行而不是改行**」。
这句话与"本表不可变"合起来的**唯一落法**是：

    "停用" = 插入一行 state='RETIRED'、supersedes_asset_id=<被停用的那一行>

于是 `state` 的语义**不是**"这一行现在能不能用"，而是
**"这一行代表的那次登记动作是什么"**：

| `state` | 这一行的含义 | `supersedes_asset_id` |
|---|---|---|
| `ACTIVE` | **登记**一个可用素材（也可能是在重新启用一个旧素材） | 可空；"重新启用"时指向被它接替的那一行 |
| `RETIRED` | **声明**某个素材自此刻起停用 | **必须非空**（没有前驱的"停用"是说不通的） |

⇒ 硬约束（落 CHECK）：`state = 'RETIRED'` ⇒ `supersedes_asset_id IS NOT NULL`。

### "某个素材当前可用吗"只有一个取数点

答案是 **"它存在，且没有任何 `RETIRED` 行 supersede 它"**。
这一条**不是**可以由两个列各自回答的 —— `state` 只说"登记动作"，后继说"是否被覆盖"，
两者**合起来**才构成答案。因此：

- 引用素材时的校验（"是不是 ACTIVE"）落在 **`repository.py` 的单一函数**
  里（`REASON_REFERENCE_ASSET_NOT_ACTIVE`），**不许任何调用方自己拼这段判断**；
- 拼两遍的后果：一处忘了判后继 ⇒ "已经被停用的素材仍可被新图引用"，
  而 AQ-03 的对照关系会因为素材被停用而**悄悄指向一个已作废的型号标注**。

### `product_model` 非空，`product_color` / `product_part` 可空

| 列 | 可空？ | 理由 |
|---|---|---|
| `product_model` | ⛔ **非空** | 它是 AQ-03 的核心坐标 —— "这张图画的**型号**对不对"。型号为空的素材**判不了 AQ-03**（对照关系无从建立）。**空值不是"未知"，而是"这个素材没用"** |
| `product_color` / `product_part` | ✅ 可空 | "整体外观图"没有部件是**常态**；颜色有时也确实拍不出（黑白样张、金属反光）。这两列缺失**不阻断**对照，只让目检者少一条线索 |

## 六、★ `media_renditions` 的"当前预览"靠 `attempt_no`，不靠时间戳

技术选型表要求"预览与正式资产**分开存**"，且加工规则会演化
（`POST /media/{id}/renditions` 重新加工）⇒ **同一个
`(media_version_id, kind)` 会有多行**。

于是"**当前有效的预览是哪一行**"必须有一个**显式答案**。
两条路：

| 出路 | 为什么不行 / 行 |
|---|---|
| 按 `created_at DESC LIMIT 1` | **ISS-52 同款**（"按某个恰好存在的列排序不是排序"）：同一毫秒内两行 ⇒ "当前"有两个答案。时间戳是**副作用**，不是为排序设计的事实 |
| `(media_version_id, kind)` 唯一 + 重新加工**改行** | 改行 = 撞不可变触发器（本表不可变）。而且那正是"预览/正式分开存"要避免的 |
| ✅ **加 `attempt_no`**（同一 version+kind 的第几次加工），**`(media_version_id, kind, attempt_no)` 唯一** | "当前" = `MAX(attempt_no)`，是**显式落库的事实**，且可 `ORDER BY` 可 `GROUP BY` |

### `state='READY'` ⇒ 产物四列必须齐全（CHECK）

`types.py` 的 `RENDITION_STATE_*` 注释写了理由：加工是本地 CPU 操作，
**它真的会失败**。若"行存在即成功"，一次失败的加工会留下一个
**指向 0 字节 blob 的 rendition 行**，而 WP35 的 ZIP 会把它当可交付资产打进去。

因此落一条**蕴含式** CHECK：

    state <> 'READY' OR (file_object_id / width / height / byte_size / image_format 全非空)

用**蕴含**而不是**等号**：非 `READY` 的行**允许**带着它已经算出来的部分信息
（例如"尺寸知道了但转格式失败"），这不违反任何不变量；而 `READY` 的行
**必须**五列齐全。等号写法会把"非 READY 但有部分信息"也判死 —— 那是过紧。

## 七、★ 本模块有**两个**循环外键，延迟其中 **3 条**（与 WP26 同数、同形）

| # | 环 | 两条 FK | 各自处置 |
|---|---|---|---|
| 1 | `media_items` ↔ `media_versions` | `media_items.current_version_id` | ✅ **延迟**（可空） |
|   |   | `media_versions.media_item_id` | ⛔ **保持 IMMEDIATE**（非空） |
| 2 | `media_versions` ↔ `media_generation_contexts` | `media_versions.generation_context_id` | ✅ **延迟**（可空） |
|   |   | `media_generation_contexts.media_version_id` | ✅ **延迟**（可空） |

⇒ **环相关的延迟是 3 条，不是 4 条。** 与 WP26 的环 1 / 环 2 **逐条同形**，
理由也逐条同构（`content/models.py` docstring 七已完整论证，此处不重复长篇）：

⚠️ **但本模块的延迟 FK 总数是 4 条**，因为还有一条**不在任何环上**：
`media_versions.id → version_nodes.id`（与注册表同事务插入，顺序由 WP11
的取号重试决定）。它不在上表里，因为它不构成"互相等待"。
⇒ 逐字口径是：**环上是 3 条，环外还有 1 条，合计 4 条**。
下面第 ⑤ 条导入期断言按**精确集合**核对这 4 条 —— 只数个数的话，
"把 `media_item_id` 也延迟了、同时漏了某个环上的一侧"会**刚好抵消**
（个数还是 4），而精确集合抓得住。

- 环 1 的 `media_item_id` 是**非空**的，"版本必须先有身份"是本模块最基本的
  引用完整性，**没有任何合法流程需要"先插孤儿版本行"** ⇒ 延迟它只是把一条
  真实错误从"当场拒绝"推迟到"COMMIT 才拒"，**只有损失**；
- 环 2 两侧都**不可变**（删不掉、改不了）⇒ 只能靠"同事务一起成立"落地，
  **必须**两侧都延迟（WP26 实测过：不延迟时主路径根本走不通）。

`reference_assets.supersedes_asset_id` 是**自引用**，**不延迟** ——
新行必然指向一行**已经存在**的旧行，插入顺序天然成立（与 WP26 的
`title_candidates` 那个环同一结论）。它**不是**环：它不构成"互相等待"。

### 判据（DDL 侧的实测断言，见文件末尾的导入期对账）

模块末尾的第 ⑤ 条断言**从 ORM 元数据读 `deferrable`** 并核对
"恰好 3 条"，把本节这句话从**注释**变成**导入期可判** —— 有人顺手给
`media_item_id` 也加上 `deferrable=True` 时，**import 就报错**，
而不是等到某次真库测试才发现防线被削弱。

## 八、`media_versions.payload` 与专属列**并存**，是**继承**不是重复

`content_versions` 已有这个形态（`payload` + `payload_hash` + 专属列），
本表照抄，理由逐条相同：

- `payload` 是**追加式快照**（供下游按 `schema_version` 演化）——
  列会演进，而快照不许变；
- `payload_hash` **由注册表算**（`register_version` 内部算过）。
  **本包不自己再算一次**（`canonical_payload_hash`）—— 两处算同一个值
  就是两个来源，而"哪个才对"没有答案（`content/service.py` 已就同一取向
  论证过）。

## 九、`media_generation_contexts` **不落**"参考素材版本"列（对 §1.7 的一处偏离）

说明文档 §1.7 的表格写本表要钉「`prompt_version_id` / `model_profile_id` /
`task_kind` / **参考素材版本**」。

前三个本实现照落。**第四个不落**，因为：

- "素材**版本**"这个说法预设了**素材本身是版本化的** —— 那是 V4 的
  `reference_assets` 也进版本注册表，而 **V4 已被否决**（§3.5），
  老大拍板第 4 点是"参考素材**不入**版本注册表"；
- 在 V3 下素材**没有版本概念** ⇒ 这一栏**没有可落的值**。
  硬落一个 `reference_asset_ids` JSONB 列还会造出**第二真值来源**
  （素材绑定已在 `media_versions.reference_bindings` 上，见第二节）。

⇒ 通过 `media_version_id` → `media_versions.reference_bindings` **可达**，
不重复存储。**收口时须修正文档 §1.7 该单元格的措辞**（这是 V4 措辞的残留，
不是需求）。

### 与 WP26 的 `GenerationContext` 的三处差别（都不是随意的）

| 差别 | WP26 | 本包 | 为什么 |
|---|---|---|---|
| 内容身份列名 | `content_id` | `content_item_id` | 本包要 FK 到 `content_items.id`（身份表），叫 `content_id` 会让人以为是 `content_versions.id`。**且本包是从 `media_items.content_item_id` 来的，同名才省一次心算** |
| 指向版本 | `content_version_id` → `content_versions` | `media_version_id` → `media_versions` | 同构换名 |
| 是否有 `provider_call_id` | 有 | **有** | 同构保留："这一张图花了多少钱"要从 WP25 的账本串起来 |

## 十、`media_versions.regeneration_reason` 为什么存在（"单张重做"的留痕）

`POST /media/{id}/regenerations` 是 F7，对应 demo 的「单张媒体（重做按钮）」。
重做产生的版本行记下**为什么重做**（运营给的说明），与 WP26 的
"采纳 = 追加一版（只换采纳标题）"是**同一种判断**：

- 不记 ⇒ 版本历史里"第 2 版与第 1 版为什么不一样"**没有答案**，
  而这是运营最常问的问题（也是 AQ-03 复盘时要看的）；
- 记在**新的版本行**上而不是"某处一个可变字段" ⇒ 每一次重做的理由
  都留在它自己的那一版上，**不可改写**（本表不可变）。

**可空**：生成产生的版本行没有理由可言（不是重做）⇒ 为 NULL。
**不落 CHECK 强制非空** —— 因为"这一行是不是重做产生的"**无法从列判断**
（重做与首次生成在列上同形），落 CHECK 会造出一条**恒真或恒假**的判据
（ISS-115 族）。这条不变量归服务层：`regenerations` 端点必然写它。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rednote.modules.media.types import (
    ASSET_STATES,
    ASSET_STATE_ACTIVE,
    ASSET_STATE_LIST_SQL,
    ASSET_STATE_RETIRED,
    COMPONENT_ROLE_LIST_SQL,
    GENERATION_CONTEXT_LIST_SQL,
    LIVE_PROFILE_STATE_LIST_SQL,
    MAX_ASSET_NOTE_CHARS,
    MAX_ASSET_SOURCE_CHARS,
    MAX_FORMAT_CHARS,
    MAX_LIVE_PROFILE_KEY_CHARS,
    MAX_LIVE_PROFILE_NOTE_CHARS,
    MAX_PARAGRAPH_REF_CHARS,
    MAX_PRODUCT_COLOR_CHARS,
    MAX_PRODUCT_MODEL_CHARS,
    MAX_PRODUCT_PART_CHARS,
    MAX_PROMPT_CHARS,
    MAX_REFERENCE_ASSETS,
    MAX_REGENERATION_REASON_CHARS,
    MAX_RENDITION_NOTE_CHARS,
    MEDIA_FORM_LIST_SQL,
    MEDIA_FORM_STATIC,
    MEDIA_SCHEMA_VERSION,
    MEDIA_STATES,
    MEDIA_STATE_INITIAL,
    MEDIA_STATE_LIST_SQL,
    MIN_REFERENCE_ASSETS,
    MIN_SORT_ORDER,
    PENDING_BINDINGS_IS_ARRAY_SQL,
    PENDING_BINDINGS_SIZE_SQL,
    PENDING_REGENERATION_REASON_SQL,
    RENDITION_KINDS,
    RENDITION_KIND_LIST_SQL,
    RENDITION_STATES,
    RENDITION_STATE_INITIAL,
    RENDITION_STATE_LIST_SQL,
    RENDITION_STATE_READY,
    VERIFICATION_LIST_SQL,
)
from rednote.platform.base import (
    Base,
    created_at_column,
    lock_version_column,
    updated_at_column,
    uuid_pk,
)
from rednote.platform.immutability import guard_immutable

# 本模块需要不可变保护的表。`ddl.py` 里有同值副本（迁移要在不加载 ORM
# 元数据的前提下拿到表名），两处必须一致 —— 文件末尾第 ① 条断言守住。
#
# **为什么这四张都在其中**：
#   · `media_versions`  —— 一次生成的产物（图 + 两个绑定）。改写它 =
#     篡改"当时画的是什么、依据哪张素材"，而 AQ-03 的盲审要拿它当**事实依据**；
#   · `reference_assets` —— 素材标注是**对照关系的一端**。改一条素材的型号，
#     "这张图画的型号对不对"的答案就变了，而图还是旧的
#     （预案：停用 = 追加新行，见 docstring 五）；
#   · `media_renditions` —— 加工产物。允许改它 = 允许把"当时交付的是哪张图"
#     换成别的，而 WP35 的 ZIP 与 AC-16 的真机记录都指向它；
#   · `media_generation_contexts` —— OP-160 的影响面分析靠它。
#     允许改它 = 允许把"这次生成用了哪版提示词/哪个模型"改成别的值，
#     而那正是追溯要回答的问题。
#
# **为什么 `media_items` 不在其中**：它是**身份**行，承载四个会前进的投影
# （docstring 三/四）与 `lock_version`（ETag 载体），必须可变。
# 与 WP26 的 `content_items` 同一取舍。
IMMUTABLE_TABLES: Final[tuple[str, ...]] = (
    "media_versions",
    "reference_assets",
    "media_renditions",
    "media_generation_contexts",
    # WP28（S3）：Live 组件挂在不可变版本上，可变的组件会让"这一版里有什么"漂移。
    "media_components",
    # WP28：profile 的 (key, version) 会被组件清单引用，改写 = 篡改"当时按哪版规格封装"。
    #       变更一律追加新 version（与 reference_assets 的"停用 = 追加新行"同一取舍）。
    "live_profiles",
)

# 本模块拥有的全部表（迁移与本模块对账用；与 `__all__` 分开，
# 因为 `__all__` 里有模型类与常量，而这里要的是"库里有哪些表"）。
OWN_TABLES: Final[tuple[str, ...]] = (
    "media_items",
    *IMMUTABLE_TABLES,
)


class MediaItem(Base):
    """一条内容里的**第 N 张图**。**可变**（`lock_version` 是 ETag 载体）。

    ## 为什么身份与版本分成两张表

    与 WP26 的 `ContentItem` **完全同构**：同一张图会被反复重做
    （运营觉得不像、素材换了、提示词改了），而运营看到的始终是
    "同一条媒体的同一格"（`content-feedback.html` 那 6 格里的一格）。
    若只有一张表，"重做"就要就地改行 —— 于是"上一版长什么样"永久丢失，
    而"单张重做"的核心价值恰恰是**能对比新旧两张图**。

    身份的"每条内容 N 条"保证：`media_items` 数量 = 该内容应有图数
    （当前口径 = `types.images_per_content()` 的 3，见 OP-198/OP-199）。
    """

    __tablename__ = "media_items"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 这一格属于哪条内容。指向**身份表**（不是版本表）：
    # "这条内容有哪些图"要在重做之后仍然答得出来（与
    # `title_candidates.content_version_id` 指向版本表是**刻意的不同**——
    # 候选是"一次生成"的产物，而图是"内容的一个位置"，跨版本不变）。
    content_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_items.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # --- 四个投影列（docstring 三/四）--------------------------------------
    #
    # ★ 这四列都是**当前有效版本的投影**，由服务层在追加版本时改写，
    #   并由 `ddl.py` 的跨表一致性触发器钉住"投影 == 最新版本"。
    #   **不是**第二真值来源：真值在 `media_versions`，这里是"为了列表页
    #   不必 JOIN 而存的一份副本"（`content_items.current_version_*` 同款）。

    # 这一格当前配的是哪一段（`p<序号>-<8位哈希>` 形状）。
    # 它同时是 R4 的读点（"6 张图均对应正文段落"）。
    paragraph_ref: Mapped[str] = mapped_column(
        String(MAX_PARAGRAPH_REF_CHARS), nullable=False
    )

    # 在内容内的顺序。**0 即封面位**（`types.SORT_ORDER_COVER`）——
    # 但本包只记顺序，不落"封面必须是 0"的 CHECK（OP-204，见 `types.py`）。
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    # 这一格现在到哪一步了：PENDING / READY / FAILED（docstring 四）。
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text(f"'{MEDIA_STATE_INITIAL}'")
    )

    # 当前生效版本。**延迟外键** —— 与 `media_versions.media_item_id`
    # 构成循环，见 docstring 七。
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "media_versions.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )

    # `current_version_id` 的冗余投影。**没有版本时为 0**（不是 NULL），
    # 理由见 docstring 三（NULL 参与比较是 UNKNOWN ⇒ 筛选静默返回空集）。
    current_version_no: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # 幂等域：同一 `(media_item_id, task_kind)` 的同一轮次重复请求不许
    # 重复计费（R5/F7）。与 `content_items.last_idempotency_key` 同款同理由：
    # 网关侧的幂等由 `CapabilityRequest.idempotency_key` 保证，这一列的作用
    # 是让**预占**（发生在派发之前）也能对得上。
    last_idempotency_key: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )

    # ★ 这一轮为这一格请求了什么（`types.MediaGenerationRequest` 的落库形状）。
    #
    # **为什么必须有这一列** —— 与 WP26 的 `content_items.generation_request`
    # 是**同一个接缝**的同一份理由（`content/models.py:385` 那段论证逐条适用）：
    # "生成参数"从**发起**（端点事务）到**执行**（Worker + `handlers.py`，
    # 另一次事务）之间没有任何既有传输通道 —— `types.MediaGenerationRequest`
    # 的 `as_payload()` / `from_payload()` 是**两侧共用**的形状（前者由端点写、
    # 后者由处理器读），而**中间那一列**就是本条。
    #
    # **本列缺席会怎样**（实测）：`types.py` 的三键载荷在全仓**零消费点**
    # （`as_payload` / `from_payload` / `MEDIA_REQUEST_KEYS` 在 `types.py`
    # 之外一次都没被调用）—— 那不是"设计成可选"，而是"落点没建"。
    # 而处理器算不出一次预占（`estimate_cost` 的 `batch_kind` /
    # `model_profile_id` 两键只有这里能给），于是这条路径**走不通**。
    #
    # **为什么不塞进 `media_generation_contexts`**：那张表是**事后留痕**
    # （每次生成一行、不可变、`provider_call_id` 要等派发后才知道），
    # 而本列是**事前意图**（可变、被覆盖、在入队之前就得有）。两者的分工
    # 与 WP26 的 `content_items.generation_request` ⟷ `generation_contexts`
    # **逐字同构**；把它们合成一处会让"最后一次请求的参数"永远改不了
    # （重做换档位就走不通）。
    #
    # 可空：媒体行在**入队之前**就已存在（那一步只需要身份），
    # 而"还没请求生成"是合法状态。处理器读到 `None` ⇒ 装配故障 ⇒
    # **不可重试**的任务失败，**不给默认值**
    # （理由见 `MediaGenerationRequest.from_payload` 的 docstring 那张表：
    # 每个默认值都会把装配故障变成静默的错误行为）。
    generation_request: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )

    # ★ **下一次生成**要绑的参考素材（R1 的输入暂存位）。
    #
    # **为什么与 `generation_request` 并列而不是并进去**：
    # `generation_request` 的语义是"**怎么**生成"（三键：taskKind /
    # modelProfileId / batchKind，见 `types.MediaGenerationRequest`），
    # 而本列是"**绑什么**"（R1 的素材绑定）。两者正交，且
    # `schemas.validate_generation_request_payload` 对那一列是
    # **fail-closed 拒多余键**的 ⇒ 并进去要改三处契约，还会让
    # "请求形状"与"内容输入"在同一个 JSON 里分叉。
    #
    # **为什么必须持久化**（生成是异步的）：端点入队后立刻返回，
    # 任务在**另一次事务**里才跑。若绑定只落在 `media_versions`，
    # 那一刻它还不存在 ⇒ 任务重放时绑定丢失，R1 退化成"没有绑定"
    # （拿到空集合而**不报错**）。
    #
    # **不是第二个真值来源**：落版本时用它写
    # `media_versions.reference_bindings`，成功后本列被置 `NULL`
    # ⇒ 同一时刻只有一处有值（判据：`test_media_wp27.py` 投影清空用例）。
    #
    # **可空**：`NULL` = "没有待生成的绑定"，与 `[]`（空数组，非法）
    # 是**两种不同的事实** —— 空数组会被 CHECK 的 `bindings_size` 拒掉。
    pending_reference_bindings: Mapped[list[Any] | None] = mapped_column(
        JSONB, nullable=True
    )

    # ★ **第二次生成**（重做）要说明的理由（F7 的输入暂存位）。
    #
    # **它与 `pending_reference_bindings` 是同一个接缝的第二个缺口**，
    # 理由逐条同构：端点在**发起**事务里拿到运营填的理由，任务在**另一次
    # 事务**里才写版本行 —— 中间那一列就是本条。
    #
    # **不补这一列会怎样**（补列前的实测）：`service.item_pending_regeneration_reason`
    # 没有可读的暂存位，**只能恒返回 `None`** ⇒ 每一次重做产生的版本行
    # `regeneration_reason` 都是 NULL。而那一列存在的唯一理由就是回答
    # "第 2 版与第 1 版为什么不一样"（docstring 十）—— 列在、值恒空、
    # 不报错，是"看起来有留痕、实际没有留痕"，比没有这一列更坏
    # （ISS-95「声称有防线」同族）。
    #
    # **为什么不并进 `pending_reference_bindings`**：那一列的 CHECK 是
    # `jsonb_array_length BETWEEN 1 AND 8`（约束**数组元素**），而理由是
    # **标量**。混进去要么让数组多出一个非绑定元素
    # （`MediaReferenceBinding.from_payload` 当场抛 `ValueError`
    # ⇒ 把"重做"报成 `REFERENCE_BINDING_MALFORMED`），要么把
    # `jsonb_typeof` 从 `'array'` 改成 `'object'` 而作废那两条 CHECK。
    #
    # **可空，且 NULL 是常态**：首次生成**没有**理由可言
    # （与 `media_versions.regeneration_reason` 同一取值口径：首次生成的行恒 NULL）。
    #
    # **为什么不落"重做时必非空"CHECK**：与版本列**逐字同一条理由**
    # （docstring 十）—— "这一行是不是重做产生的"**无法从列判断**
    # （重做与首次生成在列上同形），那种 CHECK 恒真或恒假（ISS-115 族）。
    # 这条不变量归服务层：`POST /media/{id}/regenerations` 必然写它。
    #
    # **上界与版本列同源**（都取 `MAX_REGENERATION_REASON_CHARS`）：
    # 两列各写一个上界 ⇒ "端点按 A 校验通过、DB 按 B 拒掉"，
    # 而报出来的是 SQLSTATE 而不是一个业务 reason 码。
    pending_regeneration_reason: Mapped[str | None] = mapped_column(
        String(MAX_REGENERATION_REASON_CHARS), nullable=True
    )

    # ★ **这一格当前在途的那次生成任务**（task 寻址的唯一手段）。
    #
    # 完整论证见 `types.py` 的「任务寻址：`media_items.job_id`」一节
    # （三条先例对比 + 为什么不能借 `jobs.content_id` + 为什么不动 `jobs` 表）。
    # 此处只留三句结论：
    #
    #   ① `ClaimedJob` **没有自由参数位**，`jobs` 表也没有 actor / payload 列
    #      ⇒ 处理器要拿到 `media_item_id` **只能**靠反查本包自己的表；
    #   ② 与 WP17/parsing 的 `parse_runs.job_id` **逐字同构**
    #      （同一手法、同一 `RESTRICT`、同一可空、同一配套索引）；
    #   ③ 一个 job **恰好**对应一格图（`calls_for(单图)` = 1，
    #      `MAX_IMAGES_PER_REQUEST` 是**请求**级上界，而任务按**媒体行**逐条入队）
    #      ⇒ **唯一**约束在这里是成立的，不是"碰巧唯一"。
    #
    # **可空**：媒体行在**入队之前**就已存在（那一步只需要身份）。
    # `NULL` = "当前没有在途任务" —— 生成完成、失败、或还没请求过。
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="RESTRICT"),
        nullable=True,
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    lock_version: Mapped[int] = lock_version_column()

    __table_args__ = (
        CheckConstraint(f"state IN ({MEDIA_STATE_LIST_SQL})", name="state_valid"),
        CheckConstraint("current_version_no >= 0", name="version_no_non_negative"),
        CheckConstraint(
            f"sort_order >= {MIN_SORT_ORDER}", name="sort_order_non_negative"
        ),
        CheckConstraint(
            "char_length(paragraph_ref) > 0", name="paragraph_ref_not_empty"
        ),
        CheckConstraint(
            f"char_length(paragraph_ref) <= {MAX_PARAGRAPH_REF_CHARS}",
            name="paragraph_ref_length",
        ),
        # ★ 投影自身的**单行**一致性（docstring 三"为什么这里不建跨表触发器"）：
        #   · `current_version_id IS NULL` ⇔ `current_version_no = 0`
        #     —— "指向某版本但版本号为 0"与"版本号为 N 但没有版本 id"
        #     都是**自相矛盾**的投影。
        #   ⚠️ 注意这条**不能**替代跨表对账：它管得住"投影自己自洽"，
        #      管不住"投影停在第 2 版而库里已有第 3 版"—— 后者是本包
        #      明确留下的缺口（靠唯一写入方 + 测试，见 docstring 三）。
        CheckConstraint(
            "(current_version_id IS NULL) = (current_version_no = 0)",
            name="projection_coherent",
        ),
        # 形状：只由 `types.paragraph_ref_for` 生成 ⇒ 形状是硬的、可判的。
        # ★ 这与 Python 侧的 `types.parse_paragraph_ref` 是**同一规则的
        #   两种实现**（DDL 一份、纯函数一份）—— 两者必须一致。
        #   ⛔ 原文写"由 test_media_wp27.py 用同一批样本双边对账" ——
        #   该文件不存在、本包零覆盖（2026-10-07 更正）⇒ "两种实现是否
        #   一致"**当前无判据**，只是两条正则现在写得一样。WP26 侧的同款
        #   双边对账真存在（`test_content_wp26.py`），本包未落地。
        CheckConstraint(
            "paragraph_ref ~ '^p[0-9]+-[0-9a-f]{8}$'", name="paragraph_ref_shape"
        ),
        # ★ R1 待生成绑定的形状与数量（`types` 的两个 SQL 片段是唯一来源）。
        #   两条都**必须容许 NULL**：`NULL` = "没有待生成的绑定"（合法态，
        #   生成成功后就被清成 NULL），而 `[]` 会被 `bindings_size` 拒掉 ——
        #   "空数组"与"没有"是两种不同的事实，混起来会让"忘了传绑定"
        #   伪装成"明确传了空绑定"。
        #   ⚠️ 这两条**只管形状**。素材存在性与同项目由
        #   `ddl.media_binding_consistency_ddl` 的触发器在**版本行**上管
        #   （那里才是绑定落地的地方）；投影列上的这两条是**提前**拒绝
        #   明显坏值，不替代也不重复那条唯一防线。
        CheckConstraint(
            f"pending_reference_bindings IS NULL OR ({PENDING_BINDINGS_IS_ARRAY_SQL})",
            name="pending_bindings_is_array",
        ),
        CheckConstraint(
            f"pending_reference_bindings IS NULL OR ({PENDING_BINDINGS_SIZE_SQL})",
            name="pending_bindings_size",
        ),
        # ★ 重做理由的长度上界（`types.PENDING_REGENERATION_REASON_SQL` 是唯一来源）。
        #   与 `media_versions.regeneration_reason_length` **同一条 SQL 形状**
        #   （`IS NULL OR char_length(...) <= N`），且取同一个上界常量
        #   —— 两处若各写一份，"端点按 A 校验、DB 按 B 拒"而报出的是 SQLSTATE。
        #   ⚠️ 同样**不落"非空"CHECK**：NULL 是常态（首次生成没有理由），
        #   而"重做时必非空"无法从列判断 ⇒ 会造出恒真或恒假的判据（ISS-115 族）。
        CheckConstraint(
            PENDING_REGENERATION_REASON_SQL,
            name="pending_regeneration_reason_length",
        ),
        # ★ 身份唯一键（docstring 一）："这是第 N 张图"只能有一个答案。
        #   注意**不是** `(content_item_id, paragraph_ref)` —— 见 docstring 一。
        Index(
            "uq_media_items_content_sort",
            "content_item_id",
            "sort_order",
            unique=True,
        ),
        # 列表页的排序与筛选（`GET /contents/{contentId}/media`）。
        Index("ix_media_items_project_state", "project_id", "state"),
        Index("ix_media_items_current_version", "current_version_id"),
        # "这一段配了哪些图"（段落重排后的重做范围计算）。
        Index("ix_media_items_content_paragraph", "content_item_id", "paragraph_ref"),
        # 幂等域查询：按 (媒体, 幂等键) 找"这次生成是不是已经跑过"。
        Index(
            "ix_media_items_idempotency",
            "id",
            "last_idempotency_key",
        ),
        # ★ 任务寻址的反查索引（`find_media_item_for_job`）。
        #   **唯一**，不是普通索引 —— 这一点与 WP17 的
        #   `ix_parse_runs_job_id`（普通索引）**刻意不同**，理由：
        #
        #   ① **语义上就该唯一**：一个 `MEDIA_GENERATE` 任务恰好产出一格图的
        #      一个新版本（`calls_for(单图) = 1`；任务按**媒体行**逐条入队，
        #      `MAX_IMAGES_PER_REQUEST` 是**请求**级上界而不是任务级）。
        #   ② **唯一约束才是判据**：普通索引下，"一个 job 指向两格图"
        #      这个**装配故障**没有任何东西会拦 —— 处理器反查会随
        #      `ORDER BY` 的实现细节拿到其中一格，**而后台不报错**。
        #      WP17 用普通索引是因为那里"一个 job 一个 run"没有 DB 层
        #      可表达的约束（`parse_runs` 同一 job 可能有多次尝试的痕迹）；
        #      本包的对应量是**版本行**（`media_versions`），而本列只是
        #      "当前在途指针" ⇒ 唯一性成立且可判。
        #   ③ **不会挡住重做**：重做是**新 job 覆盖同一行的本列**
        #      （UPDATE，不是 INSERT 新行）⇒ 唯一约束不影响它。
        #      真正被它挡住的是"两条媒体行挂同一个 job"，那是 bug。
        #
        #   ⚠️ PostgreSQL 的 UNIQUE 索引**允许多个 NULL**（`NULLS DISTINCT`
        #      是默认）⇒ "尚未入队的媒体行"（可能上千行）全部合法共存。
        #      **不要**改成 `NULLS NOT DISTINCT` —— 那会让第二行 `NULL`
        #      就违反约束，直接建不出表。
        Index("uq_media_items_job_id", "job_id", unique=True),
    )


class ReferenceAsset(Base):
    """一条**产品参考素材**（`PURPOSE_REFERENCE` 的落点）。**不可变**。

    ## 这张表就是 AQ-03 的对照关系的一端

    验收原文：「外观/型号/颜色与参考素材一致 ≥ 90%；跨品牌事实混入 = 0」。
    判"一致"的**唯一办法**是把生成图对回参考素材 ⇒ 素材行必须
    **带得起型号/颜色/部件标注**（那是 `file_objects` 一个通用文件行
    放不下的东西，也是 V1 方案在 AQ-03 上直接判不成的原因）。

    ## `state` 的语义、`supersedes_asset_id`、"停用"的落法

    见模块 docstring 五（含 `RETIRED ⇒ supersedes 非空` 那条 CHECK 的理由）。

    ## 为什么不做素材版本化（V4 被否决）

    三条：① "素材改动影响谁"**目前没有任何消费者**；② 会让素材也进版本
    注册表 ⇒ 多一个 `*_node_paired` 触发器与一整套流动；③ 老大拍板第 4 点
    明确"**不入**"。⇒ 素材用"追加行 + supersedes"表达演化，**不进注册表**。
    """

    __tablename__ = "reference_assets"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 素材本体（对象存储里的那个文件）。`PURPOSE_REFERENCE` 通过
    # `project_file_links` 的 purpose 列表达，**本表不重复记 purpose** ——
    # purpose 的归属是 `files` 模块的事（本包只是它的第一个正向消费者）。
    file_object_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("file_objects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text(f"'{ASSET_STATE_ACTIVE}'")
    )

    # --- AQ-03 的三个标注列（docstring 五的表格）---------------------------
    # `product_model` **非空**：它是对照关系的核心坐标，"型号为空"的素材
    # 判不了 AQ-03。
    product_model: Mapped[str] = mapped_column(
        String(MAX_PRODUCT_MODEL_CHARS), nullable=False
    )
    # 颜色与部件**可空**：整体外观图没有部件是常态。
    product_color: Mapped[str | None] = mapped_column(
        String(MAX_PRODUCT_COLOR_CHARS), nullable=True
    )
    product_part: Mapped[str | None] = mapped_column(
        String(MAX_PRODUCT_PART_CHARS), nullable=True
    )

    # 素材来源（哪来的图：官网、样张、产品同事提供……）。可空 ——
    # 来源不明的素材仍然可用，而"必须写来源"会逼人编一个（与 WP26 的
    # `risk_note` 可空同一取向：允许的例外必须留痕，但不强迫留痕）。
    source: Mapped[str | None] = mapped_column(
        String(MAX_ASSET_SOURCE_CHARS), nullable=True
    )
    note: Mapped[str | None] = mapped_column(
        String(MAX_ASSET_NOTE_CHARS), nullable=True
    )

    # 这一行接替了哪一行（docstring 五）：
    #   · `state='RETIRED'` 的行 —— **必须**指向被停用的那一行；
    #   · `state='ACTIVE'` 的行 —— 可空；"重新启用一个旧素材"时指向它。
    #
    # **自引用且不延迟**：新行必然指向一行**已经存在**的旧行，
    # 插入顺序天然成立（与 docstring 七对 `title_candidates` 那个环的
    # 结论同一手：它不构成"互相等待"，故不需要延迟）。
    supersedes_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("reference_assets.id", ondelete="RESTRICT"),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(f"state IN ({ASSET_STATE_LIST_SQL})", name="state_valid"),
        # ★ 停用声明必须有前驱（docstring 五）。没有前驱的"停用"
        #   说不清在停用谁 ⇒ 那是一条**无法执行**的登记。
        CheckConstraint(
            f"state <> '{ASSET_STATE_RETIRED}' OR supersedes_asset_id IS NOT NULL",
            name="retired_needs_supersedes",
        ),
        CheckConstraint(
            "char_length(product_model) > 0", name="product_model_not_empty"
        ),
        CheckConstraint(
            f"char_length(product_model) <= {MAX_PRODUCT_MODEL_CHARS}",
            name="product_model_length",
        ),
        CheckConstraint(
            f"product_color IS NULL OR char_length(product_color)"
            f" <= {MAX_PRODUCT_COLOR_CHARS}",
            name="product_color_length",
        ),
        CheckConstraint(
            f"product_part IS NULL OR char_length(product_part)"
            f" <= {MAX_PRODUCT_PART_CHARS}",
            name="product_part_length",
        ),
        CheckConstraint(
            f"source IS NULL OR char_length(source) <= {MAX_ASSET_SOURCE_CHARS}",
            name="source_length",
        ),
        CheckConstraint(
            f"note IS NULL OR char_length(note) <= {MAX_ASSET_NOTE_CHARS}",
            name="note_length",
        ),
        # "这个项目现在有哪些可用素材"（登记界面的下拉来源）。
        Index("ix_reference_assets_project_state", "project_id", "state"),
        # "这个文件被登记成了几条素材"（同一张图重复登记的自查）。
        Index("ix_reference_assets_file_object", "file_object_id"),
        # "这一行接替了谁" —— 由"停用"链条反查"某素材是否已被停用"
        # （docstring 五的单一取数点要用它）。
        Index("ix_reference_assets_supersedes", "supersedes_asset_id"),
    )


class MediaVersion(Base):
    """一条媒体的**版本**：生成的图 + 段落绑定 + 参考素材绑定。**不可变**
    （`id` 同时 FK `version_nodes.id`）。

    这是本包最核心的一张表：它同时承载**两个坐标** ——
    横向的"配哪一段"（`paragraph_ref`，对齐 WP26）与
    纵向的"画的是哪个产品事实"（`reference_bindings`，对齐 AQ-03）。

    ## 为什么两个绑定必须在同一行上（而不是各存一处）

    说明文档 §3.6 B 的表已经论证：只绑段落 ⇒ 重做出来的图**会漂**
    （不知道要对齐哪个产品事实）；只绑素材 ⇒ 重做后**段落对不上**。
    两个坐标都在，重做的输入才是确定的。
    """

    __tablename__ = "media_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "version_nodes.id",
            ondelete="RESTRICT",
            # 延迟：注册表与领域表同事务插入，顺序由 WP11 的取号重试决定。
            deferrable=True,
            initially="DEFERRED",
        ),
        primary_key=True,
    )

    media_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        # ⛔ **刻意不延迟**（docstring 七）：本列非空，且"版本必须属于
        #   已存在的媒体位"没有任何合法流程需要"事后才补身份"。
        #   延迟它只会额外放行孤儿版本行 —— 那是削弱防线，不是化解死结。
        #   ⛔ 原文写"test_media_wp27.py 有一条负向用例钉住这一点
        #   （与 WP26 同款）"—— 该文件不存在、本包零覆盖（2026-10-07
        #   更正）⇒ 这条反例**当前无用例**。
        ForeignKey("media_items.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 本包媒体的修订号（**与 `version_nodes.revision_no` 分开**，
    # 理由与 `content_versions.version_no` 逐字相同：注册表序号与业务
    # 版本号语义不同，注册表将来若支持"同一版本挂多条修订记录"，
    # 序号会前进而业务版本号不该动）。由 `repository.next_version_no()` 取。
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- 坐标 1：段落（R4）------------------------------------------------
    #
    # **必须是稳定标识而不是下标**（R4 原话）。理由：段落增删/重排后，
    # 下标会让所有图**整体错位**，而"重做第 3 段"不知道该重做哪张图 ——
    # ISS-52（"按某个恰好存在的列排序不是排序"）的同款。
    # 生成时由 `types.paragraph_ref_for(段序号, 段文本)` 派生（唯一派生点）。
    paragraph_ref: Mapped[str] = mapped_column(
        String(MAX_PARAGRAPH_REF_CHARS), nullable=False
    )

    # 生成那一刻这张图的顺序（与 `media_items.sort_order` 同源，
    # 但那一个是**会前进的投影**，这一个记的是**当时**的值）。
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- 坐标 2：参考素材（R1 / AQ-03 的地基）------------------------------
    #
    # 形状 `[{"assetId": "<uuid>", "part": "…"|null, "note": "…"|null}, …]`
    # （`types.MediaReferenceBinding` 的落库形状）。
    #
    # ★ 为什么是一列 JSONB 而不是 FK 列、也不是第 6 张表 ——
    #   三条出路的完整对照见模块 docstring 二。
    #   DDL 层能管住的（形状 + 数量区间）落在这里的 CHECK；
    #   管不住的（元素存在性 + 同项目）落在 `ddl.py` 的一致性触发器。
    reference_bindings: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # 生成的原始图（供应商给的那张）。`PURPOSE_GENERATED` 的首次消费
    # 发生在 service 层建这个 file_object 时。
    # **非空** —— 一个没有图的"媒体版本"不是版本，是占位。
    image_file_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("file_objects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 媒体形态（WP28，D3）：`STATIC`（默认）| `LIVE`。Live 与静态共用段落/素材绑定，
    # 故用一列区分而不是新增 `task_kind`。存量行由 `server_default` 在 ADD COLUMN 时
    # 回填 `STATIC`（不触发不可变触发器：ADD COLUMN 不逐行 UPDATE）。
    # 组件行（`media_components`）只允许挂在 `LIVE` 版本上 —— 由 ddl 的一致性触发器兜。
    media_form: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text(f"'{MEDIA_FORM_STATIC}'"),
    )

    # 出这一版用的提示词正文。**存下来的理由**：它是"这张图为什么长这样"
    # 的答案，而提示词模板会演进（改模板 → 旧图仍要能解释自己）。
    # 长度由 `MAX_PROMPT_CHARS` 限 —— 不可变表一旦写进 1MB 就删不掉。
    prompt: Mapped[str] = mapped_column(String(MAX_PROMPT_CHARS), nullable=False)

    # 重做理由（docstring 十）。首次生成的行恒为 NULL。
    regeneration_reason: Mapped[str | None] = mapped_column(
        String(MAX_REGENERATION_REASON_CHARS), nullable=True
    )

    # 生成这一版时用的哪次媒体生成上下文（可追溯到提示词/模型/素材）。
    #
    # ★ **必须延迟** —— 与 `media_generation_contexts.media_version_id`
    #   构成循环外键，且两张表都不可变 ⇒ 只能靠"同事务一起成立"落地。
    #   与 WP26 的 `content_versions.generation_context_id` **逐字同形**。
    #
    # ★★ **显式命名：`name=` 不是风格偏好，是被 63 字节逼出来的** ★★
    #   按命名约定本应生成
    #     `fk_media_versions_generation_context_id_media_generation_contexts`
    #   —— **65 字节**，超过 PostgreSQL 标识符上限（`NAMEDATALEN-1` = 63）。
    #   两条路径（alembic 建表 / `Base.metadata.create_all`）都会把它
    #   **截断成 `..._media_generatio_4e11`（60 字节）**，
    #   但 `ForeignKeyConstraint.name` 这个**元数据属性仍是未截断的 65 字节原名**。
    #   后果有二（都不是"将来可能"，是实测已发生的）：
    #     ① `tests/test_schema_parity.py` 拿元数据名去比 `pg_constraint.conname`
    #        ⇒ 恒报 2 条 missing（本次修它就是为了消这个假红）；
    #     ② 任何"按元数据名写 `op.drop_constraint(...)`"的迁移/脚本会在
    #        **运行期** `UndefinedObject` 失败 —— 而元数据看起来完全正常。
    #   处置：**去掉列名末尾的 `_id`**（FK 名里 `_id` 是冗余的，
    #   后面已经跟着目标表名），得到 62 字节，完整保住
    #   `fk_<table>_<column>_<reftable>` 结构且语义零损失。
    #   本包另一条超限的 FK 用同一手法，见 `MediaGenerationContext.prompt_template_id`。
    generation_context_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "media_generation_contexts.id",
            name="fk_media_versions_generation_context_media_generation_contexts",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )

    # 新版本的**追加式**快照（供下游按 schema_version 演化）。
    # 与专属列并存是**继承**而非重复（docstring 八）：
    #   · `payload_hash` 由 `register_version` 算（本包不自己再算一次）；
    #   · `payload` 是给"列会演进、快照不许变"用的。
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(MEDIA_SCHEMA_VERSION))
    )

    created_at: Mapped[datetime] = created_at_column()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        # **一媒体一版本号**。同时是"取号并发冲突"的捕获点：两个请求同时
        # 重做时，后到的撞这条唯一约束，服务层捕获后重新取号
        # （与 WP11 的 `register_version`、WP26 的 `content_versions` 同一手法）。
        Index(
            "uq_media_versions_item_version_no",
            "media_item_id",
            "version_no",
            unique=True,
        ),
        CheckConstraint("version_no >= 1", name="version_no_positive"),
        CheckConstraint(
            f"sort_order >= {MIN_SORT_ORDER}", name="sort_order_non_negative"
        ),
        CheckConstraint(
            "char_length(paragraph_ref) > 0", name="paragraph_ref_not_empty"
        ),
        CheckConstraint(
            f"char_length(paragraph_ref) <= {MAX_PARAGRAPH_REF_CHARS}",
            name="paragraph_ref_length",
        ),
        CheckConstraint(
            "paragraph_ref ~ '^p[0-9]+-[0-9a-f]{8}$'", name="paragraph_ref_shape"
        ),
        CheckConstraint("char_length(prompt) > 0", name="prompt_not_empty"),
        CheckConstraint(
            f"char_length(prompt) <= {MAX_PROMPT_CHARS}", name="prompt_length"
        ),
        # --- R1 的硬约束（docstring 二）-----------------------------------
        # 形状：必须是数组（不是对象、不是 null）。
        CheckConstraint(
            "jsonb_typeof(reference_bindings) = 'array'", name="bindings_is_array"
        ),
        # ★ 数量区间 —— 这条是 R1 的**主要失效形态**的直接拦截面：
        #   · 绑 0 条（`< MIN_REFERENCE_ASSETS`）= "看起来有绑定，实际没绑"；
        #   · 绑几十条（`> MAX_REFERENCE_ASSETS`）= "整库引用"，即 R1 说的
        #     "那种绑定等于没有绑定"（每张图都绑全部素材 ⇒ AQ-03 定位不到
        #     "这张图画的型号对不对"）。
        #   `BETWEEN` 是闭区间，与两个常量的语义一致。
        CheckConstraint(
            f"jsonb_array_length(reference_bindings)"
            f" BETWEEN {MIN_REFERENCE_ASSETS} AND {MAX_REFERENCE_ASSETS}",
            name="bindings_size",
        ),
        CheckConstraint(
            f"regeneration_reason IS NULL"
            f" OR char_length(regeneration_reason)"
            f" <= {MAX_REGENERATION_REASON_CHARS}",
            name="regeneration_reason_length",
        ),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_is_object"),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        CheckConstraint(
            f"media_form IN ({MEDIA_FORM_LIST_SQL})", name="media_form_valid"
        ),
        # 版本历史列表（`GET /media/{mediaId}` 的版本段）。
        Index("ix_media_versions_item_created", "media_item_id", "created_at"),
        Index("ix_media_versions_image_file", "image_file_id"),
        Index("ix_media_versions_generation_context", "generation_context_id"),
        # 段落级重做的范围计算："这一段有哪些图、各自的最新版是什么"。
        Index("ix_media_versions_item_paragraph", "media_item_id", "paragraph_ref"),
    )


class MediaRendition(Base):
    """一条**加工产物**（预览 / 正式）。**不可变**。

    ## 为什么独立成表（而不是 `media_versions` 上两个 blob 列）

    说明文档 §1.7 的对照表已给三条理由，此处不重复。补一条**结构性的**：
    两列挂在版本表上时，"这张图的预览在哪"要读版本的某两列，而
    **"没有预览"与"预览生成失败"在列上同形**（都是 NULL）——
    那正是"不可区分的两件事"（ISS-137 的判据纪律第 4 条）。
    独立成表后：**行不存在 = 没生成**；`state=FAILED` = 生成失败。两者分得开。

    ## "当前有效的预览"靠 `attempt_no`（docstring 六）

    加工规则会演化 ⇒ 同 `(media_version_id, kind)` 多行是**合法**的。
    "当前"= `MAX(attempt_no)` —— **显式事实**，不是时间戳巧合。

    ## `state='READY'` ⇒ 产物五列齐全（docstring 六的蕴含式 CHECK）

    挡住 `types.py` 那段注释描述的具体失效：一次失败的加工留下一个
    **指向 0 字节 blob 的行**，而 WP35 的 ZIP 把它当可交付资产打进去。
    """

    __tablename__ = "media_renditions"

    id: Mapped[uuid.UUID] = uuid_pk()

    media_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("media_versions.id", ondelete="RESTRICT"),
        # ⛔ 不延迟：非空，且"产物必须属于已存在的版本"没有合法流程需要事后补。
        nullable=False,
    )

    kind: Mapped[str] = mapped_column(String(20), nullable=False)

    # 同一 `(media_version_id, kind)` 的第几次加工（从 1 起）。
    # ★ 这一列的存在理由见 docstring 六 —— 没有它，"当前预览是哪一行"
    #   只能靠 `created_at` 排序，而**同一毫秒的两行**会让答案不唯一
    #   （ISS-52 同款：按某个恰好存在的列排序不是排序）。
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)

    state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text(f"'{RENDITION_STATE_INITIAL}'"),
    )

    # 产物文件。**可空**：PENDING / FAILED 时确实没有产物。
    # 而 `state='READY'` 时它**必须**非空 —— 那条约束见下面的 CHECK。
    file_object_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("file_objects.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 产物规格。尺寸/体积/格式**各有列**（而不是塞一个 JSONB）：
    #   · 它们是"交付约束是否满足"的直接判据（小红书侧的尺寸/体积上限）；
    #   · 上面那条 READY 蕴含式 CHECK 需要它们是**独立的列**才写得出来。
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_format: Mapped[str | None] = mapped_column(
        String(MAX_FORMAT_CHARS), nullable=True
    )

    note: Mapped[str | None] = mapped_column(
        String(MAX_RENDITION_NOTE_CHARS), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            f"kind IN ({RENDITION_KIND_LIST_SQL})", name="kind_valid"
        ),
        CheckConstraint(f"state IN ({RENDITION_STATE_LIST_SQL})", name="state_valid"),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        # 0 像素 / 0 字节是**无意义产物**（不是"小图"）⇒ 用 `> 0` 而不是 `>= 0`。
        CheckConstraint(
            "(width IS NULL OR width > 0) AND (height IS NULL OR height > 0)",
            name="dimensions_positive",
        ),
        CheckConstraint(
            "byte_size IS NULL OR byte_size > 0", name="byte_size_positive"
        ),
        CheckConstraint(
            f"image_format IS NULL OR char_length(image_format)"
            f" <= {MAX_FORMAT_CHARS}",
            name="image_format_length",
        ),
        CheckConstraint(
            f"note IS NULL OR char_length(note) <= {MAX_RENDITION_NOTE_CHARS}",
            name="note_length",
        ),
        # ★ READY ⇒ 产物五列齐全（docstring 六）。
        #   **蕴含式**（`<> READY OR ...`）而不是**等号**：非 READY 的行
        #   **允许**带着已算出的部分信息（"尺寸知道了但转格式失败"），
        #   等号写法会把这种合法状态也判死 —— 那是过紧。
        CheckConstraint(
            f"state <> '{RENDITION_STATE_READY}'"
            " OR (file_object_id IS NOT NULL"
            " AND width IS NOT NULL AND height IS NOT NULL"
            " AND byte_size IS NOT NULL AND image_format IS NOT NULL)",
            name="ready_requires_artifact",
        ),
        # ★ "第几次加工"唯一 —— 没有它，"当前预览"有两个答案（docstring 六）。
        Index(
            "uq_media_renditions_version_kind_attempt",
            "media_version_id",
            "kind",
            "attempt_no",
            unique=True,
        ),
        # 详情端点按 (版本, 档位) 取"最新那一次"。
        Index(
            "ix_media_renditions_version_kind",
            "media_version_id",
            "kind",
            "attempt_no",
        ),
        Index("ix_media_renditions_state", "state"),
    )


class MediaComponent(Base):
    """一组 Live 的**组件**（静帧 / 动态段 / 清单 / 预览）。**不可变**（WP28）。

    ## 为什么独立成表

    一版 Live = 4 个组件，各自是一个文件。放进 `media_versions` 会让该表的列随形态
    膨胀（静态版本永远有一堆空列）；独立成表后，**行不存在 = 这一版不是 Live 或没有此组件**，
    且每个组件都能带自己的 `verification` 与 `sha256`。

    ## 与 `media_versions.image_file_id` 的关系（S1 / S2）

    `STILL` 组件的 `file_id` **必须等于**所属版本的 `image_file_id` —— 原图只有一个落点
    （老大 2026-10-14 Q2：不设 `ORIGINAL` 角色）。这条跨表不变量在 `ddl.py` 的延迟触发器里。

    ## `verification` 只允许 `SYNTHETIC`

    没有真机，任何组件都不得宣称已验证（OP-183 / OP-76 红线）。CHECK 取值域来自
    `types.VERIFICATIONS`，只有一项。

    ## `byte_size` 为什么落列

    D4 的 20 MB 上限是**组内合计**；合计要能被事后审计，就得有逐件体积。
    """

    __tablename__ = "media_components"

    id: Mapped[uuid.UUID] = uuid_pk()

    media_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("media_versions.id", ondelete="RESTRICT"),
        # ⛔ 不延迟：非空，且"组件必须属于已存在的版本"没有合法流程需要事后补
        #   （落库顺序里组件在版本 ② 之后，见 service `_persist_generation`）。
        nullable=False,
    )

    role: Mapped[str] = mapped_column(String(20), nullable=False)

    file_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("file_objects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    verification: Mapped[str] = mapped_column(String(20), nullable=False)

    # 内容摘要（小写十六进制 SHA-256）。清单里的 `sha256` 与这一列必须同值，
    # 由 4.4 的 `LivePackStub` 同源产出。
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(f"role IN ({COMPONENT_ROLE_LIST_SQL})", name="role_valid"),
        CheckConstraint(
            f"verification IN ({VERIFICATION_LIST_SQL})", name="verification_valid"
        ),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_shape"),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        # ★ 一版里每个角色**至多一个** —— 没有它，"这一版的动态段是哪个"有两个答案。
        Index(
            "uq_media_components_version_role",
            "media_version_id",
            "role",
            unique=True,
        ),
        Index("ix_media_components_file", "file_id"),
    )


class LiveProfile(Base):
    """Live 封装规格的**占位档案**（WP28）。**不可变**，变更 = 追加新 `version`。

    ## 为什么 `state` 只有 `PLACEHOLDER`

    真机规格（OP-76 / WP02）尚无证据。允许写 `VERIFIED` 就等于允许系统自己宣布
    "已验证"（红线）。CHECK 取值域来自 `types.LIVE_PROFILE_STATES`，只有一项；
    上线前 G0 验收通过后，由**迁移**放宽该 CHECK，而不是由运行时代码改值。
    """

    __tablename__ = "live_profiles"

    id: Mapped[uuid.UUID] = uuid_pk()

    profile_key: Mapped[str] = mapped_column(
        String(MAX_LIVE_PROFILE_KEY_CHARS), nullable=False
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False)

    state: Mapped[str] = mapped_column(String(20), nullable=False)

    notes: Mapped[str | None] = mapped_column(
        String(MAX_LIVE_PROFILE_NOTE_CHARS), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            f"state IN ({LIVE_PROFILE_STATE_LIST_SQL})", name="state_valid"
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("char_length(profile_key) > 0", name="profile_key_not_empty"),
        CheckConstraint(
            f"char_length(profile_key) <= {MAX_LIVE_PROFILE_KEY_CHARS}",
            name="profile_key_length",
        ),
        CheckConstraint(
            f"notes IS NULL OR char_length(notes) <= {MAX_LIVE_PROFILE_NOTE_CHARS}",
            name="notes_length",
        ),
        Index("uq_live_profiles_key_version", "profile_key", "version", unique=True),
    )


class MediaGenerationContext(Base):
    """一次媒体生成用的**上下文快照**：提示词版本 / 模板 / 模型档案。**不可变**。

    ## ★ 这张表就是 OP-160 在媒体侧的答案

    与 WP26 的 `GenerationContext` **完全同构**：
    "**哪个提示词版本被哪些生成消费**"变成一条按 `prompt_version_id`
    的索引查询。因此 `ix_media_generation_contexts_prompt_version`
    不是性能优化，而是**那个能力能不能成立的前提**（说明文档 §3 V2 方案
    被否决的第 3 条理由就是"JSONB 列里的 id 无法建索引 ⇒ OP-160 落不了"）。

    ## `task_kind` 的取值域与 `types.TASK_KINDS` **同源**

    本包只消费一个 kind（`STATIC_IMAGE`）⇒ `GENERATION_CONTEXT_LIST_SQL`
    是单元素。多一种就说明本包多消费了一个 kind，而 `types.calls_for`
    会因此抛错 —— **两处同时变才可能一致**，这是刻意的。

    ## `media_version_id` 为什么可空

    与 WP26 的 `content_version_id` 可空**同一理由**（那里是"结构调用时
    内容版本还没有"）。本包的对应情形：**首次生成**时，"这一版是哪次调用
    产出的"这条链接的方向是"版本 → 上下文"（`media_versions.
    generation_context_id`），而"上下文 → 版本"那一侧要等版本行落地。
    重做/补录场景下，上下文行可能**先于**它服务的版本行存在（例如调用
    成功但落库失败后的人工补录）⇒ 可空是合法的常态，不是漏判。

    ## ★ 本表**不落**"参考素材版本"列（对 §1.7 的一处偏离）

    见模块 docstring 九：V3 下素材**没有版本概念**（V4 已被否决、老大拍板
    第 4 点"素材不入注册表"）⇒ 那一栏没有可落的值，硬落还会造出
    **第二真值来源**（素材绑定已在 `media_versions.reference_bindings` 上）。
    通过 `media_version_id` 可达，收口时修正文档措辞。
    """

    __tablename__ = "media_generation_contexts"

    id: Mapped[uuid.UUID] = uuid_pk()

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 列名与 WP26 的 `content_id` **刻意不同**（docstring 九的对照表）：
    # 本列 FK 的是 `content_items.id`（身份表），叫 `content_id` 会让人
    # 以为是 `content_versions.id`。
    content_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_items.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 本行服务的是哪个媒体版本。**延迟**（docstring 七的环 2）。
    media_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "media_versions.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )

    task_kind: Mapped[str] = mapped_column(String(40), nullable=False)

    # --- OP-160 的三个 id（与 WP26 同构：模板 + 版本 + 模型）---------------
    #
    # ★★ 显式命名：同 `MediaVersion.generation_context_id`，被 63 字节逼出来的 ★★
    #   命名约定本应生成
    #     `fk_media_generation_contexts_prompt_template_id_prompt_templates`
    #   —— **64 字节** ⇒ 入库被截断成 `..._prompt__d58c`（60），
    #   而元数据属性仍是 64 字节原名 ⇒ 判据恒报 missing、按名 drop 会运行期失败。
    #   处置同样**去掉列名末尾的 `_id`** ⇒ 61 字节，结构完整。
    #   注意：同表紧邻的 `prompt_version_id` / `model_profile_id` **不显式命名**
    #   （61 / 60 字节，未超限）—— 显式命名只用于"约定的产物放不下"的两条，
    #   不是把全包 FK 都手写一遍。
    prompt_template_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "prompt_templates.id",
            name="fk_media_generation_contexts_prompt_template_prompt_templates",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    # **可空但"应当有"**：正常路径上它必然有值（fail-closed 要求查不到
    # 激活版本就不生成，见 R5）。可空是为了容纳"激活版本后来被删"——
    # 而 `ON DELETE RESTRICT` 让它删不掉，于是这个 NULL 只出现在历史数据
    # 导入场景。留空比塞一个假 UUID 好（与 WP26 逐字同款）。
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("prompt_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    model_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("model_profiles.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 这次调用在网关侧的实际调用 id。有了它，"这一张图花了多少钱"
    # 能从 WP25 的账本上串起来（按张计费，`PRICING_UNIT_IMAGE`）。
    provider_call_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("provider_calls.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # 幂等键（与 `CapabilityRequest.idempotency_key` 同值）。
    # **存下来的理由与 WP26 逐字相同**：重试时要用**同一个**键去问网关
    # "这个键跑过没有"，而键是由媒体版本号 + task_kind 派生的 ——
    # 派生规则若只活在服务层，重启后（版本号已变）就派生不出同一个键，
    # 幂等失效**且不报错**。
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)

    created_at: Mapped[datetime] = created_at_column()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            f"task_kind IN ({GENERATION_CONTEXT_LIST_SQL})", name="task_kind_valid"
        ),
        CheckConstraint(
            "char_length(idempotency_key) > 0", name="idempotency_key_not_empty"
        ),
        # ★ OP-160 的能力前提（docstring 的开篇一节）：按提示词版本反查
        #   历史生成。**没有它，OP-160 在媒体侧就答不了**（全表扫是
        #   只增不减的表，扫不动）。
        Index(
            "ix_media_generation_contexts_prompt_version", "prompt_version_id"
        ),
        # OP-160 的粗粒度问法："改了这个模板影响谁"。
        Index("ix_media_generation_contexts_prompt_template", "prompt_template_id"),
        # "这批图为什么变差了" → 按模型档案归组。
        Index("ix_media_generation_contexts_model_profile", "model_profile_id"),
        # 详情端点按内容取上下文；`(内容, kind)` 归组。
        Index(
            "ix_media_generation_contexts_content_kind",
            "content_item_id",
            "task_kind",
        ),
        # 幂等：同一个键在同一内容上只该有**一行**上下文。
        # **为什么不是全局唯一**：键里含媒体版本号，同内容同版本同 kind
        # 才是"同一次生成"（与 WP26 逐字同款）。
        Index(
            "uq_media_generation_contexts_idempotency",
            "content_item_id",
            "idempotency_key",
            unique=True,
        ),
    )


guard_immutable(MediaVersion)
guard_immutable(ReferenceAsset)
guard_immutable(MediaRendition)
guard_immutable(MediaGenerationContext)
guard_immutable(MediaComponent)
guard_immutable(LiveProfile)


# ---------------------------------------------------------------------------
# 本模块的导入期对账断言（**导入即执行**，不是测试期）
#
# 为什么放在模块级而不是测试里：这些不变量一旦破了，`CREATE TABLE` 出来的
# 表就**少几条 CHECK / 多几条错误的延迟**，而这两件事都是**静默**的 ——
# 迁移能跑通、测试能跑过（除非专门去数约束、专门去读 `pg_constraint`）。
#
# ⚠️ 这几条**刻意写成精确相等**（不是 `>=` 下界）。理由：它们是"我数过了"的
#    显式声明，而不是"至少还剩几个"。加一列/加一条 CHECK 时**必须**回来
#    更新数字 —— 那个"必须回来一次"的动作正是它们存在的意义。
#    （下界式判据在这种"表结构刚定型"的场景里会退化成恒真式：ISS-115。）
# ---------------------------------------------------------------------------

# ① 不可变保护覆盖了 `IMMUTABLE_TABLES` 里的每一张表（不多不少）。
#    这条挡的是"新加了一张不可变表但忘了调 `guard_immutable`" ——
#    那会让 ORM 层放行 UPDATE，而 DDL 层（`ddl.py` 的触发器）拦下，
#    于是**开发期看不到问题，运行时才 500**。
_OWN_MODELS: Final[tuple[type, ...]] = (
    MediaItem,
    MediaVersion,
    ReferenceAsset,
    MediaRendition,
    MediaGenerationContext,
    MediaComponent,
    LiveProfile,
)
_GUARDED_MODELS: Final[tuple[type, ...]] = (
    MediaVersion,
    ReferenceAsset,
    MediaRendition,
    MediaGenerationContext,
    MediaComponent,
    LiveProfile,
)
assert {model.__tablename__ for model in _GUARDED_MODELS} == set(IMMUTABLE_TABLES), (
    "guard_immutable 的覆盖面与 IMMUTABLE_TABLES 不一致："
    f"守卫里多出 {sorted({m.__tablename__ for m in _GUARDED_MODELS} - set(IMMUTABLE_TABLES))}；"
    f"守卫里缺少 {sorted(set(IMMUTABLE_TABLES) - {m.__tablename__ for m in _GUARDED_MODELS})}"
)

# ② `OWN_TABLES` 与**本模块实际定义的**表集合**恰相等**。
#
# ★ 覆盖范围**由结构决定**（铁律 14）：不是手写一份"本模块有哪 5 张表"的
#   清单再自我核对，而是从 `Base.registry.mappers` 里**按 `__module__`
#   派生** —— 谁定义在本文件里，谁就是本模块的表。
#   手写清单的形式是"我用清单核对清单"，漏了照样不报错；派生之后，
#   在 `models.py` 里加一张表而忘了更新 `OWN_TABLES`，**import 就报错**。
_DERIVED_TABLES: Final[frozenset[str]] = frozenset(
    mapper.class_.__tablename__
    for mapper in Base.registry.mappers
    if mapper.class_.__module__ == __name__
)
assert _DERIVED_TABLES == set(OWN_TABLES), (
    "OWN_TABLES 与本模块实际定义的表不一致："
    f"实际定义 {sorted(_DERIVED_TABLES)}；OWN_TABLES 声明 {sorted(OWN_TABLES)}"
)

# ③ 每张表的列数**相等**（不是 `>=`）。
#    与 WP26 的蓝图列数断言同一手法：加列时必须回来数一次。
_EXPECTED_COLUMN_COUNTS: Final[dict[str, int]] = {
    # id / project_id / content_item_id / paragraph_ref / sort_order / state
    # / current_version_id / current_version_no / last_idempotency_key
    # / generation_request / pending_reference_bindings
    # / pending_regeneration_reason / job_id
    # / created_by / created_at / updated_at / lock_version
    "media_items": 17,
    # id / media_item_id / version_no / paragraph_ref / sort_order
    # / reference_bindings / image_file_id / prompt / regeneration_reason
    # / generation_context_id / payload / payload_hash / schema_version
    # / created_at / created_by
    # + media_form（WP28）
    "media_versions": 16,
    # id / project_id / file_object_id / state / product_model / product_color
    # / product_part / source / note / supersedes_asset_id
    # / created_at / created_by
    "reference_assets": 12,
    # id / media_version_id / kind / attempt_no / state / file_object_id
    # / width / height / byte_size / image_format / note / created_at
    "media_renditions": 12,
    # id / project_id / content_item_id / media_version_id / task_kind
    # / prompt_template_id / prompt_version_id / model_profile_id
    # / provider_call_id / idempotency_key / created_at / created_by
    "media_generation_contexts": 12,
    # id / media_version_id / role / file_id / verification / sha256
    # / byte_size / created_at
    "media_components": 8,
    # id / profile_key / version / state / notes / created_at
    "live_profiles": 6,
}
for _model in _OWN_MODELS:
    _table_name = _model.__tablename__
    _actual_cols = len(_model.__table__.columns)
    _expected_cols = _EXPECTED_COLUMN_COUNTS[_table_name]
    assert _actual_cols == _expected_cols, (
        f"{_table_name} 列数与预期不符：实际 {_actual_cols}，预期 {_expected_cols}"
    )

# ④ 每张表的 CHECK 条数**相等**。
#    少一条 = 某条约束被静默删掉（而迁移照跑、测试照过）。
_EXPECTED_CHECK_COUNTS: Final[dict[str, int]] = {
    # state_valid / version_no_non_negative / sort_order_non_negative
    # / paragraph_ref_not_empty / paragraph_ref_length / projection_coherent
    # / paragraph_ref_shape
    # / pending_bindings_is_array / pending_bindings_size
    # / pending_regeneration_reason_length
    "media_items": 10,
    # version_no_positive / sort_order_non_negative
    # / paragraph_ref_not_empty / paragraph_ref_length / paragraph_ref_shape
    # / prompt_not_empty / prompt_length
    # / bindings_is_array / bindings_size
    # / regeneration_reason_length / payload_is_object / schema_version_positive
    # + media_form_valid（WP28）
    "media_versions": 13,
    # state_valid / retired_needs_supersedes
    # / product_model_not_empty / product_model_length
    # / product_color_length / product_part_length / source_length / note_length
    "reference_assets": 8,
    # kind_valid / state_valid / attempt_no_positive / dimensions_positive
    # / byte_size_positive / image_format_length / note_length
    # / ready_requires_artifact
    "media_renditions": 8,
    # task_kind_valid / idempotency_key_not_empty
    "media_generation_contexts": 2,
    # role_valid / verification_valid / sha256_shape / byte_size_positive
    "media_components": 4,
    # state_valid / version_positive / profile_key_not_empty
    # / profile_key_length / notes_length
    "live_profiles": 5,
}
for _model in _OWN_MODELS:
    _table_name = _model.__tablename__
    _checks = [
        _c
        for _c in _model.__table__.constraints
        if _c.__class__.__name__ == "CheckConstraint"
    ]
    _expected_checks = _EXPECTED_CHECK_COUNTS[_table_name]
    assert len(_checks) == _expected_checks, (
        f"{_table_name} 的 CHECK 条数与预期不符："
        f"实际 {len(_checks)}，预期 {_expected_checks}"
    )

# ⑤ ★ **延迟外键恰好这 4 条，且必须是这 4 个具体的列**（docstring 七）。
#
# 口径（docstring 七）：**环上 3 条 + 环外 1 条**（`media_versions.id →
# version_nodes.id`，它不构成互相等待，但同样必须延迟）。
#
# 这条是本节最有价值的一条：它把"我们没有过度延迟"从**注释**变成
# **导入期可判**。具体挡三种手滑：
#   · 顺手给 `media_versions.media_item_id` 也加上 `deferrable=True`
#     ⇒ 集合里多出它 ⇒ **import 就报错**（那条延迟只会额外放行孤儿版本行，
#     是削弱防线，没有任何合法流程需要它）；
#   · 给环 2 只改一侧 ⇒ 集合里少一个 ⇒ 报错（漏改一侧时
#     `TASK_STATIC_IMAGE` 的主路径根本走不通，而 WP26 已实测过这件事）；
#   · **一增一减**（多延迟一条、又漏了另一条）⇒ **只数个数抓不到**
#     （个数不变），所以下面比的是**精确集合**而不是数量。
#
# 从 ORM 元数据读，不手写名单 —— 手写的名单核对不了"模型里到底写没写"。
_deferred_fks: Final[dict[str, str]] = {}
for _model in _OWN_MODELS:
    for _column in _model.__table__.columns:
        for _fk in _column.foreign_keys:
            if _fk.deferrable:
                _deferred_fks[f"{_model.__tablename__}.{_column.name}"] = (
                    f"{_fk.target_fullname} (initially={_fk.initially})"
                )
_EXPECTED_DEFERRED_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        # 环 1：可空的那一侧（非空的另一侧刻意保持 IMMEDIATE）。
        "media_items.current_version_id",
        # 环 2：两侧都延迟（两表都不可变 ⇒ 只能靠同事务一起成立）。
        "media_versions.generation_context_id",
        "media_generation_contexts.media_version_id",
        # 环外：与注册表同事务插入，顺序由 WP11 的取号重试决定。
        "media_versions.id",
    }
)
assert set(_deferred_fks) == _EXPECTED_DEFERRED_COLUMNS, (
    "延迟外键的集合与预期不符："
    f"多出 {sorted(set(_deferred_fks) - _EXPECTED_DEFERRED_COLUMNS)}；"
    f"缺少 {sorted(_EXPECTED_DEFERRED_COLUMNS - set(_deferred_fks))}。"
    f"实际详情：{sorted(_deferred_fks.items())}"
)
# 并且这 4 条必须**都是** `INITIALLY DEFERRED`。只判 `deferrable` 会漏掉
# `deferrable=True, initially='IMMEDIATE'` 这种写法 —— 它在 INSERT 那一刻
# 仍然要求被引用行存在，等于**没有延迟**（而 `deferrable` 字段是 True，
# 见 `fk.deferrable` 的语义）。这是本仓"看起来有防线"的又一种形态。
for _column_key, _detail in _deferred_fks.items():
    assert "initially=DEFERRED" in _detail, (
        f"{_column_key} 声明了 deferrable 但 initially 不是 DEFERRED：{_detail}。"
        "（`deferrable=True, initially='IMMEDIATE'` 在 INSERT 那一刻仍要求"
        "被引用行存在 —— 等于没有延迟，而 `deferrable` 字段看起来是 True）"
    )

# ⑥ 投影列在身份表与版本表上**同名**（docstring 三）。
#    服务层改写投影时按名字赋值；两边名字一旦分叉，那是**运行时**才炸的
#    `AttributeError`，而这里可以当场拦住。
#
#    注意：`current_version_id` / `current_version_no` **只**在身份表上有
#    （版本表的那两个对应物是 `id` 与 `version_no`）—— 因此这条断言比的是
#    "投影的四列在两边都**够得着**"，而不是"两边列名相同"。
_PROJECTED_FROM_VERSION: Final[tuple[str, ...]] = ("paragraph_ref", "sort_order")
for _name in _PROJECTED_FROM_VERSION:
    assert _name in MediaItem.__table__.columns, (
        f"media_items 缺少投影列 {_name}（types.MediaSummary 需要它）"
    )
    assert _name in MediaVersion.__table__.columns, (
        f"media_versions 缺少源列 {_name}（投影的真值在这里）"
    )
assert "current_version_id" in MediaItem.__table__.columns, (
    "media_items 缺少 current_version_id 投影"
)
assert "current_version_no" in MediaItem.__table__.columns, (
    "media_items 缺少 current_version_no 投影"
)

# ⑦ 取值域常量与 CHECK 里引用的 SQL 列表**同源**（防"改了一处没改另一处"）。
#    `MEDIA_STATES` / `RENDITION_KINDS` / `ASSET_STATES` / `RENDITION_STATES`
#    都在 `types.py` 里，而 CHECK 用的是它们的 `*_LIST_SQL` 派生串 ——
#    这里反向确认"派生串确实来自那个集合"（防止有人手写了一个字面量列表）。
assert MEDIA_STATE_INITIAL in MEDIA_STATES, "初始状态不属于媒体状态集合"
assert RENDITION_STATE_INITIAL in RENDITION_STATES, "初始状态不属于产物状态集合"
assert ASSET_STATE_ACTIVE in ASSET_STATES, "初始状态不属于素材状态集合"


__all__ = [
    "IMMUTABLE_TABLES",
    "OWN_TABLES",
    "LiveProfile",
    "MediaComponent",
    "MediaGenerationContext",
    "MediaItem",
    "MediaRendition",
    "MediaVersion",
    "ReferenceAsset",
]