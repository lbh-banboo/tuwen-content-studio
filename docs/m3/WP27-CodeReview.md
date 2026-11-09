# WP27 Code Review（三遍法）

对象：`services/backend/src/rednote/modules/media/`（**13 个文件 / 11031 行**）
测试：`services/backend/tests/test_media_wp27.py`（**26 条用例**）
迁移：`migrations/versions/20261008_0610_b7c1e4a9d203_wp27_pending_reference_bindings.py`、`migrations/versions/20261008_0930_c4d9f2a17e35_wp27_job_addressing_and_regen_reason.py`
方法：先读全文再下结论；凡与 docstring 声称不符者一律**以实测为准**。
机械指认：`evidence/m3/wp27-code-review.log`（**34 条判据**，可复跑）

> **本文档的读数口径**：每一条"修法"都写明它是**建议**还是**已落地**。
> 凡标注 **⚠️ 已被否决 / 部分不准确** 的，都是落地时被实测推翻的当初判断 ——
> 保留原文 + 否决理由，读者才知道"为什么不是那样改"，
> 而不是把建议悄悄改成结论（那会让本文档变成一份"事后合理化"的记录）。
>
> **本轮一条业务代码都没改**（`service.py` / `api.py` / `repository.py` 全程只读）。
> 发现的缺陷**只登记 ISS，修留作下一包** —— 与 WP26 同款处置。

---

## 与 WP26 的三处结构性不同（先讲清楚，后面每节都建立在这上面）

| # | 差异 | 后果 |
|---|---|---|
| 1 | **本包 CRITICAL 与 WP26 位置对称**：WP26 是 `claim_scope` 声称服务层拒绝而抛点为 0；WP27 是**第 6 步声称"幂等查重"而调用点为 0** | 见 **CRITICAL-1**。判据取证必须**先剥离注释**（"幂等查重"四个字就住在第 6 步那行的注释里），否则恒绿 |
| 2 | **本包多出一类缺陷面：docstring 的"零覆盖"声称已过时** | 勘察期（2026-10-07）批量写下"该文件全仓不存在、本包零覆盖"，而 `test_media_wp27.py` 此后已落地（**1068 行 / 26 用例**）⇒ 那批声称现为**方向相反**的过时陈述。见 **MAJOR-1** |
| 3 | **本包的迁移是"化石字面量"而非活常量委托** | 两条 WP27 迁移的 CHECK 文本一度引用模块常量，被 `check_migration_value_domain_no_live_import.py` 判红后**已改回字面量**（2026-10-10）。见 **[C22]~[C24]** |

---

## 第一遍：正确性 / 安全

### ✅ 第一遍通过项（判据在 `wp27-code-review.log` 逐条可查）

| 判据 | 检查内容 | 实测 |
|---|---|---|
| `[C1]` | 宽 `except` **逐一**落在三类豁免之一 | 6 处：`service.py:515`=③注释论证（Pillow 异常类型随版本变）/ `1589`·`1657`=①补偿后重抛 / `1678`=③注释论证（"结算失败不掩盖已成功的结果"）/ `1992`=③（`_release_quietly`，释放失败不掩盖原异常） |
| `[C2]` | 无可变默认参数（AST 判定） | **0 处** |
| `[C3]` | 13 个文件无 `commit`/`rollback`（剥离注释后） | **0 处**（事务边界归 `session_scope`） |
| `[C4]` | SQL 实参无**运行期值**插值 | 3 处 f-string（`models.py:463/763/1103`），插值项全是**模块级常量**（`MEDIA_STATE_INITIAL` / `ASSET_STATE_ACTIVE` / `RENDITION_STATE_INITIAL`） |
| `[C5]` | 函数体内 `assert` | **1 处**：`service.py:1775`（`-O` 下失效；当前由调用方 `L1606` 拦截） |
| `[C5b]` | 模块级 `assert` | **13 处**，全在 `models.py`（8 处顶格 + 5 处在模块级 `for` 循环体内） |
| `[C6]` | 每处 `SchemaViolation(...)` 都带 `reason=` | **0 处缺**（45 处命中） |
| `[C6b]` | `SchemaViolation.__init__` 的 `reason` 是 keyword-only 且**无默认值** | 结构强制 ✅（`schemas.py:192`） |
| `[C8]` | 幂等键派生点唯一 | `gateway_bridge.py:436 media_generation_idempotency_key` —— **本包唯一派生点** |

### 🔴 CRITICAL-1（本包唯一 CRITICAL，**未修，已登记 ISS-218**）

#### 定位

`service.py` 的模块 docstring（`service.py:19`）逐字写着七步编排的第 6 步：

> `| 6 | 幂等查重→ 预估 → 预占 → 渲染 → 派发 | R2：先占后发 |`

`service.py:1530` 的段注释同样写着：

> `# --- 第 6 步：幂等 → 预估 → 预占→ 渲染 → 派发 -----------------------`

`gateway_bridge.py:257` 的幂等键层次对照表也声称：

> `| 单项上下文层（service） | content_item_id + idempotency_key | ✅ | find_generation_context 查重 |`

**而 `find_generation_context` 在 `media/service.py` 的调用点是 0 处。**

#### 取证（`[CR-1 CRITICAL-1①/②]`，走 AST 调用计数）

| 事实 | 实测 |
|---|---|
| `find_generation_context` 定义位置 | `repository.py:1145`（**有**） |
| `media/service.py` 内调用点 | **0 处** |
| `_execute_locked` 内调用点 | **0 处** |
| 第 6 步（`service.py:1531-1575`）实际动作序列 | `billing_repo` 判空 → `next_version_no`(L1539) → `render_system_prompt`(L1542) → `estimate_generation_cost`(L1557) → `reserve_generation_budget`(L1568) → **`generator.dispatch`(L1579)** —— **无任何查重调用** |
| 对照：`content/service.py`（WP26） | **确有** `find_generation_context` 调用（`content/service.py:611-621`）—— **幂等查重有先例** |

第三行是要害：编排表第 6 步第 1 项就是"幂等查重"，而实现里**这一步不存在**，
`_execute_locked` 直接进了"预估 → 预占 → 派发"。

#### 后果链

```
第 6 步无查重
  ⇒ media_generation_contexts 的幂等键 = "media-generate:{media_item_id}:v{target_version_no}:{task_kind}"（含版本号）
  ⇒ 任务重放时 target_version_no 会前进（上一版已落）
  ⇒ 键不同 ⇒ uq_media_generation_contexts_idempotency 不会拦
  ⇒ "同一次生成被买两次"只能靠任务层幂等 IDEMPOTENCY_SCOPE_MEDIA_GENERATE 兜
  ⇒ 而模型调用层（本应查重的那层）无实现 ⇒ R5 静默降级
```

⇒ 这正是 **ISS-95 形态**：**"声称自己有防线"比"没有防线"更危险**。
防线（`find_generation_context`）在 `repository` 里存在、在 docstring 里声称、
在编排表里列为第 6 步第 1 项 —— **唯独没有调用点**。

#### 为什么登记而不是本轮修

修法（**建议**）不是加一行 —— 它要决定"查重命中后如何返回既有 outcome"，
而 `_execute_locked` 的失败语义与返回值形状会变，且 `_execute_locked`
在全 `tests/` 目录**命中 0 次**（端点用例只走 202 入队，见 **TESTS-1**）
⇒ **改了没有判据**。故登记为下一包的同一批欠项（与 WP26 的 CRITICAL-1 同款处置）。

#### ⚠️ 判据本身的坑（本条踩到一次，已修）

CRITICAL-1 的**证据自己就住在注释里**（第 6 步那行逐字写着"幂等查重"）。
第一版判据若用原始文本做子串匹配 —— **恒绿**（注释会被算进来）。
最终取证走 **AST 的 `ast.Call` 计数**（`calls_in()` 扫 `Name`/`Attribute` 两种形态），
`service.py` 的数字本体（去掉注释与 docstring 后）才是计数的面。

---

## 第二遍：并发 / 事务

### ✅ 第二遍通过项

| 判据 | 检查内容 | 实测 |
|---|---|---|
| `[C9]` | `get_media_item_for_update` 是执行入口**第一步** | 行锁 L1344 < 取号 L1539 / 预估 L1557 |
| `[C10/R2]` | 预估 < 预占 < 派发（行号严格递增：**先扣钱再出网**） | L1557 < L1568 < L1579 |
| `[C11]` | `advance_projection` 用**条件 UPDATE** | 含 `MediaItem.lock_version == expected_lock_version` + `lock_version=MediaItem.lock_version + 1` ✅ |
| `[C11b]` | 投影推进在取号**之后**（版本号与投影同一次事务内自洽） | 取号 L1539 < 投影 L1850 |
| `[C12]` | 撞唯一约束的写入段包在 `savepoint` 里 | 3 处：`service.py:1280` / `1633` / `1670` |
| `[C13]` | `_execute_locked` 函数体内无 `commit`/`rollback` | **0 处** |
| `[C14]` | `set_state` 与 `advance_projection` 分离（失败路径不动投影） | ✅（`set_state` 按自身语义处理 `lock_version`） |

R2 这条高风险项在**顺序**这一层落实，且用**行号严格递增**取证
（不是"代码里看起来是这么写的"）：预占（L1568）严格早于派发（L1579），
故"额度为 0 时 `dispatch` 一次都没被调用"这条 R2 判据有结构保证。

---

## 第三遍：边界 / 加固

### ✅ 第三遍通过项

| 判据 | 检查内容 | 实测 |
|---|---|---|
| `[C15/ISS-79]` | `consistency_items` / `consistency_trigger_names` / `consistency_function_names` **三者等长** | **4 / 4 / 4** |
| `[C15b/ISS-79]` | `immutable_function_names()` 恰是跨包共用那一个（**建立 ≠ 拥有**） | `('rednote_reject_mutation',)` |
| `[C16]` | `index_names()` 由 ORM 派生（不手写清单 —— 铁律 14） | 含派生逻辑 ✅ |
| `[C17]` | 契约含本包全部 6 个路径 | **6/6** |
| `[C17b]` | `api.py` 路由装饰器个数与契约自洽 | **7 个**（6 路径 + `reference-assets` 的 GET/POST） |
| `[C18]` | `_HTTP_BY_REASON` 键数 >= 20（**活性下界**） | **20 个键** |
| `[C19]` | 段落切分三处**同口径**（只按空行切） | `media.schemas:474` 与 `content.schemas:318` 逐字同值 `r"\n\s*\n"` |
| `[C19b]` | 切段同口径有独立判据守住 | `tools/quality/check_paragraph_split_parity.py` 存在 |
| `[C19c/OP-197]` | 权威口径**第二处文档副本**存在且被判据 G8 对账 | `docs/数据结构与接口契约.md` 存在 + 判据源码含 `G8` |
| `[C20/OP-195]` | `ImageByteSource` 无默认实现 ⇒ fail-closed | ✅（检测到 `byte_source is None` 分支） |
| `[C22]` | WP27 两条迁移**不 import** 本仓模块 | **0 处** rednote import |
| `[C23]` | WP27 两条迁移**不活引用** `media.types` 值域常量 | **0 处**活引用 |
| `[C24]` | 迁移活常量委托有独立判据 | `check_migration_value_domain_no_live_import.py` 存在 |

#### `[C19]` 为什么必须**跨文件**对账，而不是各判各的

段号坐标系必须**跨模块唯一** —— `paragraph_ref` 与段落绑定若由不同切段实现生成，
图刚生成就报 `PARAGRAPH_REF_UNKNOWN`。故判据不是"本包有没有按空行切"，
而是"**三处**（`content/schemas.py` / `knowledge/chunking.py` / `media/schemas.py`）
是不是**同一个正则**"。本判据比对 `media` 与 `content` 的字面值，逐字相同才算过。

#### `[C19c]` 顺带闭合 OP-197：把"幽灵落点"补成真落点（2026-10-11）

`[C19b]` 只查"判据脚本存在"，而 **OP-197 明确要求的是"权威定义必须有第二处文档副本"**。
CR 期间实测发现：该副本点名的落点 `docs/数据结构与接口契约.md`
**全仓、全机都不存在**（`find` 零结果、`git log --all` 零历史、研发计划 V2.0/V2.1 均无定义），
而登记册 **OP-197 与 OP-199 两条都点名它** ⇒ **幽灵落点**（两条 OP 共同指向一个不存在的落点，
ISS-95 族在"文档落点"上的形态）。

处置两步（缺一不可，**只做第一步就与 `[C19b]` 同病**）：

1. **补写** `docs/数据结构与接口契约.md` —— 承载四条跨模块契约的权威口径
   （切段 / 取数投影 / `paragraph_ref` / 顺序对位派图），每条给出"实现落点 + **有无独立判据**"，
   **无判据的显式写"无"**（写"有测试覆盖"冒充"有判据"才是 ISS-95 族）。
2. **给判据加 G8** —— 核对该文档**存在且已入库**，并让它的 §1.4 场景期望表与
   `check_paragraph_split_parity.py::_EXPECTED` **双向对账**。
   > 只查存在性 = `[C19b]`/G6 的老毛病（"点名一个路径就算有防线"）。
   > 双向对账才让**任一侧单独改动都报红**：文档改值 → 报红；文档缺行 → 报红；判据多行 → 报红。

**变异实测 3/3 全部有效报红**（M1 改文档期望值 / M2 删文档一行 / M3 判据多一个场景），
还原后全绿。这一条把 OP-197 的"剩下真问题 (a)"（**判据只防三处不一致、防不住三处一起改错**）
从"只有一份手写表"推进到"**手写表有两份且双向对账**" —— 仍防不住"四处一起改错"，
但把单点改动的成本从 1 处提到 4 处，并如实登记该边界（见契约文档 §1.5 末尾）。

#### `[C22]` / `[C23]` 为什么是**两条**而不是一条

**迁移文本是"建库那一刻的化石"**：`ALTER TABLE ... ADD CONSTRAINT` 只在迁移执行
那一刻生效，之后活常量怎么改老库都不动。因此：

- `[C22]`（不 import 本仓模块）拦的是"迁移**顶层** import `rednote.*`"；
- `[C23]`（不活引用值域常量）拦的是"**间接**：import 了某个模块，再用它的常量名"。

两条**不同的形态**，只判一条会漏。本包这两条在 2026-10-10 都**真跑验证过**：
改回字面量后 `alembic upgrade head` 成功（`rednote_wp27mig`：
`df6bb2f41f84 → b7c1e4a9d203 → c4d9f2a17e35`）、库里 `pg_get_constraintdef`
三条 CHECK 文本符合预期、九取值行为验证通过、`downgrade → upgrade` 复原零残留。

---

## ★ CR 发现汇总（逐条钉住现状）

### 🔴 CRITICAL-1（未修，已登记 ISS-218）

见上。`service.py:19` 编排表第 6 步声称"幂等查重"、`gateway_bridge.py:257`
声称 `find_generation_context` 查重，而 `media/service.py` 调用点 **0 处**
⇒ **R5 静默降级**（参照实现 `content/service.py:611-621` 确有该调用）。

### 🟠 MAJOR-1（docstring 过时"零覆盖"声称，**未修，已登记 ISS-219**）

勘察期（2026-10-07）在 5 处写下"该文件全仓不存在、本包零覆盖"，
而 `test_media_wp27.py` 此后已落地（**1068 行 / 26 用例**）：

| 位置 | 声称原文（摘） | 实测 |
|---|---|---|
| `types.py:86` | `而那个文件**全仓不存在**（本包在 services/backend/tests/ 下**零覆盖**）` | 文件**存在** |
| `models.py:131` | `"+ test_media_wp27.py 的用例"，而**该文件全仓不存在、本包零覆盖**` | 文件**存在** |
| `models.py:644` | `该文件不存在、本包零覆盖（2026-10-07 更正）` | 文件**存在** |
| `models.py:888` | `该文件不存在、本包零覆盖（2026-10-07）` | 文件**存在** |
| `schemas.py:98` | `而该文件不存在、本包零覆盖（2026-10-07 更正）：这些校验函数**目前没有用例**` | 文件**存在**，且对 `schemas` 纯函数层有 8 条用例 |
| `schemas.py:235` | `—— 该文件不存在、本包零覆盖` | 文件**存在** |

**性质**：这是"**反向过时**"（声称零覆盖、实则有覆盖），方向上比"声称有覆盖"
更保守，但**仍会让下一轮 CR 误判本包测试空白**、把一个已被覆盖的层当缺口重复补测。
⇒ 建议同步更正这 6 处，或改为"该文件已落地、但下列断言仍缺"。

⚠️ **注意**：这些 docstring 里**另一部分**（"`images_per_content() is
IMAGES_PER_CONTENT` 断言不存在"、"`test_task_kind_constants_match_prompts` 从未落地"）
**仍然成立**（实测 test 文件内 **0 命中**）—— 见 **TESTS-1**。改时须**只**改"文件不存在"
那半句，别把仍成立的缺口一起删掉。

### 🟡 TESTS-1（被点名声称的断言确实缺失，**未修，已登记 ISS-220**）

`types.py:156` 声称"测试里有一条断言 `images_per_content() is IMAGES_PER_CONTENT`"，
实测 `test_media_wp27.py` 内 `images_per_content` **0 命中**。
同族：`types.py:84-93` 声称的 `test_task_kind_constants_match_prompts`
在本包测试文件内也 **0 命中**（该名在 `test_content_wp26.py` 里守的是
`content ↔ prompts`，与本包无关）。

⇒ 这是"**声称有判据、判据不存在**"（ISS-95 在测试侧）的形态。
建议：给这两条补真断言，或把声称改成"当前无判据"。

### 🟡 MINOR-1（`service.py:1775` 函数体内 `assert`）

`assert dispatch.image is not None  # noqa: S101 - 由调用方（第 6 步）判过`。
真正判定在 `service.py:1606`（`if dispatch.image is None: ... raise`）。
**风险**：`python -O` 会剥离本行 ⇒ 若调用方漏判，`dispatch.image.content`（`:1776`）
抛 `AttributeError`。当前不可达。建议改为显式 `if ...: raise`。

### 🟡 MINOR-2（`models.py` 13 处模块级裸 `assert`）

13 处：8 处顶格（`1378/1396/1509/1541/1544/1552/1553/1554`）+
5 处在模块级 `for` 循环体内（`1431/1469/1520/1535/1538`，仍是模块作用域）。
**性质**：这些是**导入期对账**（`OWN_TABLES` 与实际表集合、CHECK 条数、延迟 FK 集合），
不是运行期防线 —— 作为"导入期把配置错误当崩溃"是合理的；
但 `-O` 下静默消失意味着"配置漂移"在带 `-O` 的部署里没有任何提示。
建议改显式 `raise RuntimeError(...)`（与 WP25/WP26 MINOR-2 同款处置）。

---

## 判据自身的三条防御（本轮新增，都在脚本里）

### ① "声称"必须先剥离注释再计数

CRITICAL-1 的证据本身住在注释里（第 6 步那行写着"幂等查重"）。
用原始文本做子串判据会把这段说明当成"查重已实现" ⇒ **判据恒绿**。
本脚本对它走 AST（抛点/调用点计数按 `ast.Call`）。

### ② 判据的"取法"错误会伪装成"代码缺陷"（本轮踩到 3 次，均已修）

| 次 | 写错的地方 | 症状 | 修法 |
|---|---|---|---|
| 1 | `[C1]` 只扫 docstring 找豁免论证 | `service.py:1678` 的论证住在**handler 体内的注释**里（`unparse` 剥掉注释）⇒ 假 FAIL | 改扫**原始行**（`_lines[lineno-1:end]`）+ docstring |
| 2 | `[C15]` 数 `consistency_items` 的 `append` 调用点 | 它 `for ...: items.append(...)` ⇒ 数到 **1**（实际 4 项）⇒ 假 FAIL | 改数**首个 `for` 的字面量元组项数** |
| 3 | `[C18]` `_dict_keys` 只认字符串字面量键 | `_HTTP_BY_REASON` 的键是**变量名**（`ast.Name`）⇒ 只抓到 3 个（实际 20）| 键**两种形态都认** |

三次都是**判据自己写错、代码没错**。教训：判据的**取法**（数什么、取哪个节点）
与判据的**语义**一样重要，而前者只能靠一次真跑暴露。

### ③ "钉住现状"型判据必须写明修复后会怎样

`[CR-1 CRITICAL-1①/②]` / `[MAJOR-1]` / `[TESTS-1]` 三条断言的都是**缺陷现状**。
它们 `ok=True` 的含义是"现状与 CR 报告一致"，**不是"代码没问题"**。
修好之后这几条**会变红** —— 那是设计要求：逼着修复者同步 CR 报告与登记册。
若没有这一层，证据会在修复后**静默地继续声称缺陷存在**（ISS-143 的变体）。

---

## 汇总

| 级别 | 数量 | 编号 | 状态 |
|---|---|---|---|
| **CRITICAL** | **1** | CRITICAL-1（第 6 步声称"幂等查重"、`media/service.py` 调用点为 0 ⇒ R5 静默降级，ISS-95 形态） | 📝 **未修，已登记 ISS-218** |
| **MAJOR** | **1** | MAJOR-1（6 处 docstring "零覆盖 / 该文件不存在"已过时，与 `test_media_wp27.py` 已落地相反） | 📝 **已登记 ISS-219** |
| **MINOR** | **2** | MINOR-1（`service.py:1775` 函数体内 `assert`，`-O` 下失效） | 📝 已登记（建议改显式 `raise`） |
| | | MINOR-2（`models.py` 13 处模块级裸 `assert`，`-O` 下静默消失） | 📝 已登记（建议改显式 `raise`） |
| **CALIBER** | **0** | —— | —— |
| **TESTS** | **1** | TESTS-1（`images_per_content() is IMAGES_PER_CONTENT` 被点名声称而无断言；`task_kind` 同值断言同样缺失） | 📝 **已登记 ISS-220** |
| **DUPL** | **0** | —— | —— |
| **TOOLING** | **0** | —— | —— |

### 三遍法各自的落点（本包与 WP26 的覆盖面差异）

| 遍 | 检查的手腕 | 本包特有 |
|---|---|---|
| 第一遍 正确性/安全 | 宽 `except` 逐处豁免 / 可变默认参 / 事务泄漏 / SQL 值插值 / 裸 `assert` 分层 / `SchemaViolation` 必带 `reason` / 幂等键唯一 | `[CR-1]` 用 **AST 调用计数**把"声称查重 vs 零调用"钉住（注释里的声称不算） |
| 第二遍 并发/事务 | 行锁第一步 / R2 顺序 / 条件 UPDATE / `savepoint` / 无 commit | R2 用**行号严格递增**取证；`[C14]` 把"`set_state` 与 `advance_projection` 分离"判据化 |
| 第三遍 边界/加固 | 三项等长（ISS-79）/ 建立 ≠ 拥有 / 索引由 ORM 派生 / 契约 7 路由 / 活性下界 20 / 切段同口径 / 迁移化石字面量 | `[C22]`+`[C23]` 把"迁移活常量委托"拆成**两条不同形态**；`[C19]` **跨文件**比对切段正则 |
| **★★ 本包新增一遍** | **docstring 声称 vs 实测** | `[MAJOR-1]` / `[TESTS-1]`：本包的"零覆盖"声称是**批量过时**的，而它们**没有任何判据对账**（正是一条"覆盖范围由人写"的复发，铁律 14） |

> ⚠️ **本汇总表本身就是一段自由文本** —— 它**没有**机械判据与明细对账
> （项目纪律：汇总表是最容易被"事后合理化"的地方）。
> 可核对的明细一律在 `evidence/m3/wp27-code-review.log`（**34 条判据**，可复跑）。
> 本表**刻意不写**"文档多少行"之类的数字 —— 手写的行数在写完当天就可能不符。

---

## 附：与证据的对应关系

| 结论 | 机械指认 |
|---|---|
| 宽 `except` 三类豁免 / 无提交泄漏 / `reason` 必带 / 幂等键唯一 | `[C1]`~`[C8]` |
| 行锁第一步 / R2 顺序 / 条件 UPDATE / `savepoint` / 投影在取号后 | `[C9]`~`[C14]` |
| 三项等长 / 建立 ≠ 拥有 / 索引派生 / 契约 6 路径 7 路由 / 活性下界 20 / 切段同口径 / fail-closed | `[C15]`~`[C20]` |
| **CRITICAL-1**（声称查重 vs 调用点 0）+ 对照实现 | `[CR-1 CRITICAL-1①/②]` / `[C8]` |
| **MAJOR-1**（过时"零覆盖"声称 6 处）+ **TESTS-1**（被点名断言缺失） | `[MAJOR-1]` / `[TESTS-1]` |
| 迁移化石字面量（两条形态）+ 独立判据 | `[C22]` / `[C23]` / `[C24]` |
| MINOR-1（函数体内 `assert`）/ MINOR-2（模块级 `assert` 13 处） | `[C5]` / `[C5b]` |

**反向验证记录**：本轮为"钉住现状"型审查（业务代码全程只读）。
判据的**区分力**由一次**变异实测**互证：
把 `media/service.py::_execute_locked` 里补一行 `find_generation_context(...)`
调用后，`[CR-1 CRITICAL-1①]` 与 `[CR-1 CRITICAL-1②]` **2/2 同时报红**
（`service 调用点=1 处`、`_execute_locked 内调用点=1 处`），
还原后 `git diff` 为空、随即回绿 —— 证明这两条判据**真能分辨"修复"与"未修复"**，
而不是形状级恒绿。

---

*本文档由 `tools/m3/collect_wp27_code_review.py` 的 34 条判据机械支撑。*
*复跑：`NO_PROXY='*' PYTHONPATH=services/backend/src <PY> tools/m3/collect_wp27_code_review.py`*
