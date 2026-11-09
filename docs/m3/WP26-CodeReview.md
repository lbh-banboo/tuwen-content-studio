# WP26 Code Review（三遍法）

对象：`services/backend/src/rednote/modules/content/`（**13 个文件 / 8800 行**）
测试：`services/backend/tests/test_content_wp26.py`（**92 条用例**，四层范式）
迁移：`migrations/versions/20261006_1700_1c1c814b248e_wp26_content_generation_and_title_candidates.py`（1255 行）
基线：`92 passed`（本包）+ 全量 **`2032 passed / 0 skipped`**
方法：先读全文再下结论；凡与 docstring 声称不符者一律**以实测为准**。
机械指认：`evidence/m3/wp26-code-review.log`（**36 条判据**，可复跑）

> **本文档的读数口径**：每一条"修法"都写明它是**建议**还是**已落地**。
> 凡标注 **⚠️ 已被否决 / 部分不准确** 的，都是落地时被实测推翻的当初判断 ——
> 保留原文 + 否决理由，读者才知道"为什么不是那样改"，
> 而不是把建议悄悄改成结论（那会让本文档变成一份"事后合理化"的记录）。
>
> **本轮一条业务代码都没改**（`service.py` / `api.py` / `repository.py` 全程只读）。
> 发现的缺陷**只登记 ISS，修留作下一包** —— 依据是本轮开工前老大的两次拍板：
> 缺陷 A（CRITICAL）→「只登记 ISS，修留作下一包」；
> 缺陷 D（TESTS）→「本轮登记 ISS，随 A 一起下一包补」。
> 因此本文档里所有 `ok=True` 的发现类判据都是**钉住现状**，不是"代码没问题"。

---

## 与 WP25 的四处结构性不同（先讲清楚，后面每节都建立在这上面）

| # | 差异 | 后果 |
|---|---|---|
| 1 | **CRITICAL 不是"实测抓到的运行时缺陷"，而是"声称与实现不符"** | 判据取证必须**先剥离 docstring**（`claim_scope` 的证据逐字住在它自己的 docstring 里），否则恒绿。见 **CRITICAL-1** |
| 2 | **上一轮的 CRITICAL 定性被本轮推翻** | 上一轮判"未登记 reason ⇒ 触发 500"，本轮用**可达性闭包**证明该 reason 根本走不到 HTTP 边界。见 **CALIBER-1** |
| 3 | **本包的缺陷集中在"声明"而非"逻辑"** | 三处缺陷（CRITICAL-1 / CALIBER-1c / CALIBER-2）全是 docstring 与实现**方向相反或计数不符** —— 逻辑本身跑得通，全量 2032 全绿 |
| 4 | **本包有整整一层零覆盖，而它是所有高风险项的唯一落点** | `execute_generation` 在全 `tests/` 目录**命中 0 次**。见 **TESTS-1** |

---

## 第一遍：正确性 / 安全

### ✅ 第一遍通过项（判据在 `wp26-code-review.log` 逐条可查）

| 判据 | 检查内容 | 实测 |
|---|---|---|
| `[C1]` | 4 处宽 `except` **逐一**落在三类豁免之一 | 4 处：`service.py:756`=①补偿后重抛 / `1005`=③docstring 论证 / `780`·`768`=②pragma+只记日志 |
| `[C1b]` | 那 2 处 `pragma: no cover` 的宽 `except` 只记日志、不吞异常 | 2 处（`service.py:780`, `768`） |
| `[C2]` | 无可变默认参数（AST 判定） | **0 处** |
| `[C3]` | 13 个文件无 `commit`/`rollback`（剥离注释后） | **0 处**（事务边界归 `session_scope`） |
| `[C4]` | SQL 实参无**运行期值**插值 | 1 处 f-string（`models.py:367`），插值项**全是模块级常量** |
| `[C5]` | 函数体内无 `assert`（`-O` 下会被剥离） | **0 处** |
| `[C5b]` | 模块级 `assert` 恰为 `models.py` 的 4 处 | 4 处：`725` / `734` / `748` / `759` |
| `[C6]` | 每处 `SchemaViolation(...)` 都带 `reason=` | **0 处缺**（R1 的四类拒绝必须可区分） |
| `[C7]` | `_as_number` 显式排除 `bool` | 含 bool 判定 ✅（`True == 1` 是静默放行的入口） |

### 🔴 CRITICAL-1（本包唯一 CRITICAL，**未修，已登记 ISS，修复留作下一包**）

#### 定位

`repository.claim_scope`（`repository.py:203`）的 docstring 逐字写着：

> `## brief_version_id is None 与"基线没有 claim"`
>
> 两者都返回 `(frozenset(), 0)` —— 这是**如实报数**，不是错误。
> **服务层据此拒绝（`REASON_BASELINE_NOT_READY`）**：分母为 0 时"绑定"这件事无意义，
> 而若放行，模型会绑出 `known_claim_ids` 之外的 claim 而**必被拒**，
> 或者绑 0 条而通过（等于没有 R4）。

**而 `REASON_BASELINE_NOT_READY` 在 `service.py` 的抛点是 0 处。**

#### 取证（`[CR-1 CRITICAL-1]` / `[CR-1 CRITICAL-1b]`）

| 事实 | 实测 |
|---|---|
| `claim_scope` docstring 声称"服务层据此拒绝" | **True**（`repository.py:237`） |
| `REASON_BASELINE_NOT_READY` 在 `service.py` 的抛点 | **0 处** |
| `total_claim_count` 在 `service.py` 的出现次数 | 5 处（行号 541 / 555 / 687 / 828 / 906） |
| 其中**守卫**（`if total_claim_count == 0: raise`） | **0 处** |

第三、四行合起来是这条缺陷的要害：`total_claim_count` 只作为**入参**被传下去，
没有任何一处对它做判断。于是 `execute_generation` 的**第 5 步基线判据域**
（`service.py:538-567`）在 `baseline_id is None`、或 `brief_version_id is None`、
或基线没有 claim 这三种情况下，都会让 `known_claim_ids == frozenset()`、
`total_claim_count == 0` **原样流向第 6 步取号**，**不拒绝、直接放行**。

#### 后果链

```
total_claim_count == 0
  ⇒ schemas.validate_bindings 的"规则二"（651-666）不启用
      （判据是 `if total_claim_count >= _ALL_CLAIMS_RULE_MIN_TOTAL:`，常量 = 2）
  ⇒ "绑满全部 claim = 没绑"这条规则**静默失效**
  ⇒ 且 known_claim_ids 为空集 ⇒ "绑定的 claim 必须存在"也拦不住任何东西
  ⇒ 模型可以绑 0 条通过 ⇒ **R4 静默降级为"不判绑定"**
```

⇒ 这正是 **ISS-95 形态**：**"声称自己有防线"比"没有防线"更危险**。
防线（`REASON_BASELINE_NOT_READY` 的拒绝）在文档里存在、常量在 `types.py:337`
定义了、在 `types.py` 的 `__all__` 里也导出了 —— **唯独没有调用点**。

#### 为什么登记而不是本轮修

判据纪律第 3 条要求："文档说'有判据'必须能**当场指出脚本里哪个函数**"。
本条的修法**不是加一行 `if`** —— 加守卫会改变 `execute_generation` 的失败语义
（现在是"放行后由 `validate_bindings` 兜"，改后是"在取号之前就抛"），
而 `execute_generation` **零覆盖**（见 TESTS-1）⇒ **改了没有判据**。
两条必须同批做：**先补测试，再加守卫**。故登记为下一包的同一批欠项。

#### ⚠️ 判据本身的坑（本条踩到一次，已修）

CRITICAL-1 的**证据自己就住在 `claim_scope` 的 docstring 里**（它逐字写着那个常量名）。
第一版判据直接用原始文本做子串匹配"这个常量在 service.py 出现吗" ——
**恒绿**：因为 `repository.py` 里那行 docstring 会被算进来。
这就是"**声称必须先剥离 docstring 再看**"这条防御（见文末"判据自身的三条防御"①）。
最终取证走 AST，按 `ast.Call` 的 `reason=` 关键字实参计数抛点。

---

## 第二遍：并发 / 事务

### ✅ 第二遍通过项

| 判据 | 检查内容 | 实测 |
|---|---|---|
| `[C8]` | `get_content_item_for_update` 是执行入口**第一步** | 行锁 L502 < 取号 L570 / 幂等 L591 / 预估 L605 |
| `[C9/R2]` | 预估 < 预占 < 派发（行号严格递增：**先扣钱再出网**） | L605 < L617 < L642 |
| `[C10/R5]` | 幂等检查（`find_generation_context`）早于预占 | L591 < L617 |
| `[C11/R6]` | `resolve_active_version` 早于预占（**没模板就不花一分钱**） | L530 < L617 |
| `[C12]` | `advance_projection` 用条件 UPDATE | 含 `lock_version` 条件 ✅ |
| `[C12b]` | 投影推进在取号之后（版本号与投影同一次事务内自洽） | 取号 L570 < 投影 L692 |
| `[C13]` | 两个撞唯一约束的写入段（`_apply_copy` / `_try_settle`）都包在 savepoint 里 | 两处均 ✅ |
| `[C14]` | 循环体内无 DB 往返（无 N+1） | **0 处**（`insert_title_candidates` 循环内只有 `session.add`） |
| `[C15]` | 环 2 的解法：`context_id` 在 INSERT **之前**提前生成 | 生成 L678 < 上下文入库 L720 |

R2 / R5 / R6 三条高风险项在**顺序**这一层全部落实，且用**行号严格递增**取证
（不是"代码里看起来是这么写的"）。R2 的判据特别说明：预占（L617）严格早于派发（L642），
故"额度为 0 时 `dispatch_call` 一次都没被调用"这条 R2 判据有结构保证。

---

## 第三遍：边界 / 加固

### ✅ 第三遍通过项

| 判据 | 检查内容 | 实测 |
|---|---|---|
| `[C16]` | 不可变表清单恰为 4 张 | `('content_versions', 'narrative_blueprints', 'title_candidates', 'generation_contexts')` |
| `[C17/ISS-79]` | `consistency_items()` 恰 3 项且含 `version_nodes` | 3 项：`['content_versions', 'version_nodes', 'generation_contexts']` |
| `[C18/OP-88]` | `CONTENT` 反向触发器与配对函数都在 | `trg_version_nodes_content_paired` / `rednote_check_content_node_paired` 均命中 |
| `[C19/ISS-79]` | `consistency_function_names()`（本模块**拥有**的函数）恰 3 个且不含共用函数 | 3 个 ✅ |
| `[C19b/ISS-79]` | 本包**不拥有** `rednote_reject_mutation`（跨包共用 ⇒ 由 platform 拥有） | ✅ |
| `[C19c/ISS-79]` | `immutable_function_names()` 恰是那一个跨包共用函数（**建立 ≠ 拥有**） | `['rednote_reject_mutation']` |
| `[C20]` | `index_names()` 由 ORM 派生（不手写清单 —— 铁律 14） | 含派生逻辑 ✅ |
| `[C21]` | 契约含本包全部 6 个路径 | **6/6**（全库 71 paths） |
| `[C22]` | `api.py` 恰 6 个路由装饰器（与 6 个契约路径一一对应） | 6 个 ✅ |

#### `[C19]` / `[C19b]` / `[C19c]` 为什么是**三条**而不是一条

`ddl.py` 里有两个长得很像、语义**相反**的函数：

| 函数 | docstring 原话 | 返回值 |
|---|---|---|
| `consistency_function_names()`（561） | 本模块**自己的**函数名（`downgrade` 只删这些）。**不含** `rednote_reject_mutation` | 3 个本包函数 |
| `immutable_function_names()`（573） | 本模块**建立**的函数名。只有共用那一个 —— 且它是**跨包共用**的，`downgrade` 不许删 | `("rednote_reject_mutation",)` |

ISS-79 问的是"**拥有**"（`downgrade` 能删谁），不是"**建立**"（谁建的）。
若只判一条，两种错误会**同形**（判据纪律第 5 条："没跑"与"跑失败"同形）：
- "本包把共用函数当自己的删了" ⇒ 只要判"不拥有"就能拦；
- "本包压根没建过它" ⇒ 只判"不拥有"**永远拦不住**（没有的东西当然不拥有）。

故必须**同时**钉住"不拥有"（C19/C19b）与"确实建立且只有它一个"（C19c）。

---

## ★ CR 发现汇总（逐条钉住现状）

### 🔴 CRITICAL-1（未修，已登记）

见上。`claim_scope` 声称服务层拒绝、而服务层抛点为 0 ⇒ **R4 静默降级**。

### 🟠 CALIBER-1（口径，**上一轮判断被本轮推翻**）

**上一轮的判断**：`REASON_BASELINE_MISMATCH` 未登记进 `api.py` 的 `_HTTP_BY_REASON`
⇒ 触发 `_raise_api_error` 的 `RuntimeError` ⇒ **HTTP 500**。

**本轮的实测**：⚠️ **该结论不成立**。

`_HTTP_BY_REASON` 登记 8 条、`REASON_*` 抛点 9 条，**差集确实恰为**
`REASON_BASELINE_MISMATCH`（`[CR-1 CALIBER-1b]`，这个差集本身是对的）。
但**差集不是缺陷** —— 本脚本改用**可达性闭包**（`_reachable(entries, src)`，
从 API 入口函数沿调用图求闭包）后坐实：

| 闭包 | 含 `REASON_BASELINE_MISMATCH`？ |
|---|---|
| **API 闭包**（6 个端点入口）reason 共 **7** 个 | ❌ 不含 |
| **exec 闭包**（`execute_generation`）reason 共 5 个 | ✅ 含 |
| 只在 exec 路径、不在 API 路径 | `{REASON_BASELINE_MISMATCH, REASON_MODEL_PROFILE_NOT_FOUND}` |

而 `execute_generation` 的**唯一调用者**是 Worker 侧的 `handlers.py:169`
（`content_generate_handler` 内）—— **它不跨 HTTP 边界**。
⇒ `_raise_api_error` 根本收不到这个 reason ⇒ **不存在 500 路径**。

**真实缺陷是 CALIBER-1c**（`[CR-1 CALIBER-1c]`）：`api.py:337` 的注释写着

> `"新增 REASON_* 时必须同步登记 —— 否则它会静默变成 422。"`

而实现（`api.py:332-338`）是 `raise RuntimeError(...)`。
`RuntimeError` 会被全局异常处理器兜成 **500**，**不是 422**。
**注释与实现方向相反** —— 后来人照着这句话理解，会把"500 崩"当成"422 静默降级"，
两者的排查方向完全不同（一个去看日志找未登记 reason，一个去查参数校验）。

> **方法论沉淀**（已写入脚本文末"判据自身的三条防御"②）：
> **"集合差集"不足以下缺陷结论。** 差集是"两个集合不同"，
> 缺陷是"这条路径真的会走到"。二者之间隔着**可达性**。
> 本脚本现在把两个结论**都**落成判据（CALIBER-1 与 CALIBER-1b），
> 让"差集"与"可达"的差别可被后人复核，而不是只留一句话在报告里。

### 🟠 CALIBER-2（口径，**错引已于 2026-10-07 修；声明矛盾本体即 ISS-181，仍 OPEN**）

`service.resolve_provider_id`（`service.py:398`）的 docstring 自述：

> 本函数就是那个"一处"。它有**两个**调用方：
>
> | 调用方 | 什么时候 |
> |---|---|
> | `execute_generation` | 执行阶段第 4 步 |
> | **`api.py` 的发起端点** | 发起那次事务（展示用预估，"让用户在点确认之前看到一个金额"） |

**实测**（`[CR-1 CALIBER-2]`）：`api.py` 调用 `resolve_provider_id` **0 次**、
调用 `estimate_generation_cost` **0 次**。

而 `api.py` 自己的 docstring（`api.py:426-429`）写的是**相反**的话：

> **没有金额 / 预估字段**：预估入口在 WP25
> （`POST /projects/{projectId}/budget-estimates`），本包再算一份就是第二个入口。

#### ★ 2026-10-07 更正（ISS-190，MAJOR / OPEN）—— 上面这段引文里的端点名是**编造**的

原引文**保留在上面不动**（留作审计轨迹），但其中的事实有两根支柱、两根都是空的：

| 支柱 | 实测 | 核验命令 |
|---|---|---|
| WP25 有 `budget-estimates` 端点 | ❌ **零注册**；真名 `cost-estimates`，`modules/billing/api.py:882` | `grep -rn '"/projects/\{projectId\}/[a-z-]+"' modules/billing/api.py` |
| 那个端点接得了本需求的活 | ❌ `EstimateCostBody`（`modules/billing/schema.py:250`）只有裸 `quantity`、**无 `task_kind`**；而 `task_kind → quantity` 的换算（`calls_for`）在**本包**（`modules/content/types.py:825`） | `grep -n "class EstimateCostBody" -A 32 modules/billing/schema.py` |

⇒ 这句曾被当作"发起端点可以不展示预估"的**理由**，而它引以为据的东西根本不存在 ——
**它从未支撑住那个结论**。错引同时扩散到 `contracts/openapi.json:1394`（由该 docstring 导出）。

**本次已修的范围**：只改错引，**不含 ISS-190 的功能取舍**（三条修法待决策）。
`api.py` 的这段 docstring 改写为「金额口径只有一处 `estimate_cost`」+「⚠️ 但**不能**
据此推出别包已有展示用预估」+ 当场可复核的那条 grep + ISS-190 指向；
`contracts/openapi.json` 随重新导出回收同源错引。

⇒ **CALIBER-2 的本体未修**：实现仍站在 `api.py` 一边（发起端点零预估、零解析），
而 `resolve_provider_id` 仍自称有两个调用方 —— 那就是 ISS-181，独立 OPEN。
风险不只是"文档不准确"：`resolve_provider_id` 的整段论证
（"存 `provider_id` 就有两个来源 ⇒ 预占记在 A 供应商、实际调用走 B"）
是**为那个不存在的调用方写的**。读者会以为发起端点会做预估展示，
进而以为"发起时就锁定了供应商"，而实际发起端点只入队、不解析、不预估。

### 🟠 TESTS-1（测试缺口，**未修，已登记，与 CRITICAL-1 同批**）

`execute_generation` 在**全 `tests/` 目录**命中 **0 次**（`[CR-1 TESTS-1]`）。

它是本包**全部高风险项的唯一落点**：R1（结构化校验）、R2（先后顺序）、
R4（证据绑定）、R5（幂等）、R6（fail-closed）、R8（`UNKNOWN` 不重发）
无一例外写在它的循环体里（`service.py:580-754`）。

**成因**（`[CR-1 TESTS-1b]`）：端点用例只走 **202 入队** ——
`POST .../content-generations` 与 `POST .../regenerations` 返回 202
（`test_content_wp26.py:3032` / `3468` 断言），生成主体由 **Worker 处理器**
（`handlers.py:169`）承担，路由层**不触发**执行。
`handlers.py` 在本测试文件里也**命中 0 次**。

⇒ 目录里也没有假 generator、没有 `dispatch_call` 打桩、没有
`PromptRepository` / `BaselineRepository` / `brief_claims` 铺底 fixture。
**"92 条用例全绿"与"执行路径有没有缺陷"之间没有关系。**

> ⚠️ 与 CRITICAL-1 的强绑定：CRITICAL-1 的修法是"加一条 `total_claim_count == 0` 守卫"，
> 而**守卫的落点就在 `execute_generation` 里** ⇒ 零覆盖 ⇒ 改了没有判据。
> 两条**必须同批修**：先给 `execute_generation` 补服务层用例（脚本化生成器 + 真库），
> 再加守卫。这正是本轮登记而非本轮修的原因。

### 🟡 MINOR-1（5 条 `REASON_*` 定义后无引用）

`[CR-1 MINOR-1]` 实测 5 条：`REASON_BASELINE_NOT_READY` / `REASON_BLUEPRINT_NOT_FOUND` /
`REASON_IDEMPOTENCY_CONFLICT` / `REASON_PROJECT_NOT_FOUND` / `REASON_SCOPE_UNKNOWN`。

其中 `REASON_IDEMPOTENCY_CONFLICT` 的现场最典型：`api.py:898-903` 用的是
**字面量** `ErrorCode.IDEMPOTENCY_CONFLICT` + `"reason": "idempotency_key_reused"`，
没有引用那个常量 ⇒ 常量与字面量可以**各自漂移**而无人发现（ISS-66 形态）。

`REASON_BASELINE_NOT_READY` 属于 CRITICAL-1（是它的"防线常量"）。
另外 3 条是其他包/其他路径的常量，本包不消费 —— 但既然在本模块 `types.py` 里定义了，
"定义了却不引用"这件事本身需要一条判据守着，否则新守卫加进来时无法判断
"这个常量终于被用上了"还是"又加了一个没人用的"。

**处置（本轮）**：登记，不改。**修法（建议）**：下一包给 5 条各写一句
"它该在哪儿被引用 / 为什么现在不用"，或删除确实无用的。

### 🟡 MINOR-2（`models.py` 4 处模块级裸 `assert`）

`[C5b/MINOR-2]` 实测 4 处：`725`（字段集相等）/ `734`（两类字段不重叠）/
`748`（列数核对）/ `759`（CHECK 条数核对）。

**性质**：这 4 处是**导入期对账**（`BLUEPRINT_FIELDS` 与 ORM 列的一致性），
不是运行期防线 —— 而**这是关键区别**：`python -O` 会剥离 `assert`，
所以它们**不能**当运行期判据；但作为"导入期把配置错误当成崩溃"它们是**合理**的。

**为什么仍登记为 MINOR**：`-O` 下静默消失意味着
"配置漂移"在带 `-O` 的部署里**没有任何提示**。
建议改为显式 `if ...: raise RuntimeError(...)`（与 WP25 MINOR-2 同款处置）。

### 🟡 MINOR-3（`service.py` 4 处宽 `except`，**逐处论证后判为可接受**）

| 行 | 豁免类别 | 论证 |
|---|---|---|
| `756` | ① 补偿后原样重抛 | 先释放预占、`set_state(FAILED)`，再 `raise` 原异常（不吞） |
| `768` | ② `pragma: no cover` + 只记日志 | 释放预占失败：**原始异常优先上报**（日志写明了） |
| `780` | ② `pragma: no cover` + 只记日志 | `set_state(FAILED)` 失败：同上 |
| `1005` | ③ docstring 论证"失败不回滚" | `_try_settle` 内：内容已落库、钱可能已花 ⇒ 记为待结算，**不回滚内容** |

**`[C1]` / `[C1b]` 把这三类理由都变成判据**，而不是只写在报告里 ——
这正是判据纪律第 3 条的要求：文档说"这处有理由"必须能当场指出脚本里哪个函数。

#### ⚠️ 判据本身的坑（本条踩到两次，均已修）

| 次 | 写错的地方 | 症状 | 修法 |
|---|---|---|---|
| 1 | `[C1]` 写成"**所有**宽 except 都必须 reraise" | 把 ② ③ 两类**合法豁免**一起判成 FAIL（`未 reraise 的：['service.py:1005', '780', '768']`） | 改成**逐处给豁免类别**（①/②/③），对不上号才 FAIL |
| 2 | `[C1b]` 取块用"except 行起后 5 行"固定窗口 | `780` 的块把紧随其后的 `raise`（外层补偿路径的重抛）圈进来 ⇒ 假 FAIL | 改按 **AST 的 `handler.body` 行范围**取 |

两次都是**判据自己写错、代码没错**。教训：豁免是**分类**问题，不是单一条件问题；
窗口取值必须按 AST 节点范围，不能按固定行数。

---

## 判据自身的三条防御（本轮新增，都在脚本里）

### ① "声称"必须先剥离 docstring 再看

CRITICAL-1 的证据本身住在 `claim_scope` 的 docstring 里（它逐字写着常量名）。
用原始文本做子串判据会把这段说明当成"防线已实现" ⇒ **判据恒绿**。
本脚本对它走 AST（抛点计数按 `ast.Call` 的 `reason=` 关键字实参）。
同族：`CALIBER-2` 也用剥离 docstring 后的 `calls_in()` 计数，而不是子串匹配
（`service.py` 的 docstring 里写着 "api.py 的发起端点" 这句话本身不是调用证据）。

### ② "集合差集"不足以下缺陷结论

见 CALIBER-1。差集对、结论错。改用**可达性闭包**，并把两个结论都落成判据。

### ③ "钉住现状"型判据必须写明修复后会怎样

`CRITICAL-1` / `CRITICAL-1b` / `CALIBER-2` / `TESTS-1` / `MINOR-1` 五条
断言的都是**缺陷现状**。它们 `ok=True` 的含义是"现状与 CR 报告一致"，
**不是"代码没问题"**。修好之后这几条**会变红** —— 那是设计要求：
逼着修复者同步 CR 报告与登记册。若没有这一层，
证据会在修复后**静默地继续声称缺陷存在**（ISS-143 的变体）。

---

## 汇总

| 级别 | 数量 | 编号 | 状态 |
|---|---|---|---|
| **CRITICAL** | **1** | CRITICAL-1（`claim_scope` 声称服务层拒绝、抛点为 0 ⇒ R4 静默降级，ISS-95 形态） | 📝 **未修，已登记 ISS**（老大拍板：只登记，修留作下一包） |
| **MAJOR** | **0** | —— | —— |
| **MINOR** | **3** | MINOR-1（5 条 `REASON_*` 定义后无引用） | 📝 已登记 |
| | | MINOR-2（`models.py` 4 处模块级裸 `assert`，`-O` 下静默消失） | 📝 已登记（建议改显式 `raise`） |
| | | MINOR-3（`service.py` 4 处宽 `except`） | ✅ **逐处论证后判为可接受**，`[C1]`/`[C1b]` 把三类理由判据化 |
| **CALIBER** | **2** | CALIBER-1（**上一轮判断被推翻**：差集成立但不可达 ⇒ 无 500 路径；真实缺陷是 `api.py:337` 注释与实现方向相反） | 📝 已登记 |
| | | CALIBER-2（`resolve_provider_id` 声称两个调用方，`api.py` 实测 0 次；与 `api.py:426` 自述矛盾） | 📝 已登记 |
| **TOOLING** | **0** | —— | —— |
| **TESTS** | **1** | TESTS-1（`execute_generation` 全 `tests/` 零覆盖 —— R1/R2/R4/R5/R6/R8 的唯一落点） | 📝 **未修，已登记**（老大拍板：随 CRITICAL-1 一起下一包补） |
| **DUPL** | **0** | —— | —— |

### 三遍法各自的落点（本包与 WP25 的覆盖面差异）

| 遍 | 检查的手腕 | 本包特有 |
|---|---|---|
| 第一遍 正确性/安全 | 宽 `except` 逐处豁免 / 可变默认参 / 事务泄漏 / SQL 值插值 / 裸 `assert` 分层 / `SchemaViolation` 必带 `reason` / `bool` 排除 | `[C1]` 把宽 `except` 的豁免从"数一下有几个"升级为**逐处分类** |
| 第二遍 并发/事务 | 行锁第一步 / R2 顺序 / R5 顺序 / R6 顺序 / 条件 UPDATE / `savepoint` / 无 N+1 / 环 2 提前定 id | 三条高风险项（R2/R5/R6）全用**行号严格递增**取证 |
| 第三遍 边界/加固 | 不可变表 4 张 / `consistency_items` 长度（ISS-79）/ OP-88 双向 / 不拥有跨包函数 / 索引由 ORM 派生 / 契约 6 路径 | `[C19c]` 把"**建立 ≠ 拥有**"这个区别判据化 |
| **★ 共性** | —— | **本包 0 条 MAJOR、0 条实测并发缺陷** —— 因为执行路径**根本没有测试覆盖**（TESTS-1）。WP25 的 MAJOR-1 是被并发用例抓到的，本包**没有那条用例** |

### 测试缺口（三遍法共同暴露）

| # | 缺口 | 闭合情况 |
|---|---|---|
| 1 | `execute_generation` 全 `tests/` **0 命中** ⇒ R1/R2/R4/R5/R6/R8 零覆盖 | 📝 **未闭合，已登记**（TESTS-1，下一包与 CRITICAL-1 同批） |
| 2 | `handlers.py`（Worker 侧入口）全 `tests/` 0 命中 | 📝 同上（同一批） |
| 3 | 无 `dispatch_call` 打桩 ⇒ "额度为 0 时一次都没派发"（R2 判据）**不可测** | 📝 同上 |
| 4 | 无 `brief_claims` 铺底 fixture ⇒ R4 的"绑定数 < 基线全部 claim 数"**不可测** | 📝 同上 |

> ⚠️ **本汇总表本身就是一段自由文本** —— 它**没有**机械判据与明细对账
> （项目纪律：汇总表是最容易被"事后合理化"的地方）。
> 可核对的明细一律在 `evidence/m3/wp26-code-review.log`（**36 条判据**，可复跑）。
> 本表**刻意不写**"文档多少行"之类的数字 —— 手写的行数在写完当天就可能不符，
> 那正是铁律 14 的病长在文档上（WP24 的姊妹文档实测过一次）。

---

## 附：与证据的对应关系

| 结论 | 机械指认 |
|---|---|
| 13 文件无裸 `except` / `assert` / 可变默认参 / 事务泄漏 / SQL 插值 / `bool` 混淆 | `[C1]`~`[C7]` |
| 行锁第一步 / R2 / R5 / R6 顺序 / 条件 UPDATE / `savepoint` / 无 N+1 / 环 2 | `[C8]`~`[C15]` |
| 不可变表 4 张 / `consistency_items` 3 项 / OP-88 双向 / **建立 ≠ 拥有** / 索引由 ORM 派生 | `[C16]`~`[C20]` |
| 契约 6 路径 / 6 个路由装饰器 | `[C21]`~`[C22]` |
| **CRITICAL-1**（声称 vs 抛点为 0）+ `total_claim_count` 无守卫 | `[CR-1 CRITICAL-1]` / `[CR-1 CRITICAL-1b]` |
| **CALIBER-1**（可达性闭包）+ 差集 + 注释方向相反 | `[CR-1 CALIBER-1]` / `[CR-1 CALIBER-1b]` / `[CR-1 CALIBER-1c]` |
| **CALIBER-2**（`resolve_provider_id` 调用方计数） | `[CR-1 CALIBER-2]` |
| **TESTS-1**（`execute_generation` 零覆盖）+ 成因（202 入队） | `[CR-1 TESTS-1]` / `[CR-1 TESTS-1b]` |
| **MINOR-1**（5 条孤常量） | `[CR-1 MINOR-1]` |
| **MINOR-2**（模块级裸 `assert` 4 处） | `[C5b/MINOR-2]` |
| **MINOR-3**（宽 `except` 三类豁免） | `[C1]` / `[C1b]` |

**反向验证记录**：本轮为"钉住现状"型审查（业务代码全程只读），
不产生"退回实现 ⇒ 判据变红"类的反向验证。
判据的**区分力**由两条独立事实互证：
① `[C19]` 与 `[C19c]` 互为反命题（一个有 3 个、另一个恰是那 1 个共用函数），
若 `ddl.py` 把两个函数写反，**两条会同时红**（不是都绿）；
② `[CR-1 CALIBER-1]`（闭包）与 `[CR-1 CALIBER-1b]`（差集）对同一组数据给出
**不同**的结论，若把闭包实现退化成差集，两条会变成同一条 —— 判据本身能暴露这一点。

---

*本文档由 `tools/m3/collect_wp26_code_review.py` 的 36 条判据机械支撑。*
*复跑：`NO_PROXY='*' PYTHONPATH=services/backend/src <PY> tools/m3/collect_wp26_code_review.py`*