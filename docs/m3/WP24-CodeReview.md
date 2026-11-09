# WP24 Code Review（三遍法）

对象：`services/backend/src/rednote/modules/planning/`（14 个文件，约 250 KB）
基线：`190 passed`（`tests/test_planning_wp24.py`）+ 全量 `1717 passed`
方法：先读全文再下结论；凡与 docstring 声称不符者一律**以探针实测为准**
（探针脚本：`/tmp/probe_wp24_shave.py`、`probe_wp24_neg.py`、`probe_wp24_reach.py`、
`probe_wp24_hash.py`、`probe_wp24_tauto.py`、`probe_wp24_nplus1.py`、
`probe_wp24_nplus1b.py`）

> **本文档的读数口径**：每一条"修法（建议）"都写明了它是**建议**还是**已落地**。
> 凡标注 **⚠️ 已被否决** 的，都是阶段 5 落地时**实测推翻**了当初的建议 ——
> 保留原文 + 否决理由，是为了让读者知道"为什么不是那样改"，
> 而不是把建议悄悄改成结论（那会让本文档变成一份"事后合理化"的记录）。

---

## 第一遍：正确性 / 安全

### 🔴 MAJOR-1（新，可达 500）`_capacity_shave` 在「全零权重 + 容量上限」下不收敛

**位置**：`allocation.py::_capacity_shave`（第 238~285 行）

**触发条件**：至少一个条目有 `capacity`（走削峰路径）**且**本轮参与分配的条目权重全为 0**且** `pending < len(active)`。

**探针实测**（`/tmp/probe_wp24_shave.py`）：

```
[RAISE] 全零权重+容量, total=1, len=2: RuntimeError: 容量削峰未能收敛（轮数超过 256）
[RAISE] 全零权重+容量, total=2, len=3: RuntimeError: 容量削峰未能收敛（轮数超过 256）
[OK]    全零权重+容量, total=2, len=2: allocated={'a': 1, 'b': 1}
[OK]    全零权重+无容量, total=1, len=2: allocated={'a': 0, 'b': 0} unallocated=1
```

**服务层可达链已实测**（`/tmp/probe_wp24_reach.py`）——不需要手工造数据，运营在页面上两步就能到：

```
草稿：两个场景都点「停用」⇒ state=DISABLED ⇒ _to_allocation_items 把权重归 0
      其中一个场景填了容量 5（页面有这一列）
      本轮总量填 1
⇒ dimension_allocation(draft, "scene")
⇒ RuntimeError: 容量削峰未能收敛（轮数超过 256）
```

**根因**（两条设计论证在这里都不成立）：

1. `docstring` 声称"若 `active` 非空则补 1 那一步**至少补出 1 条**"—— 但补 1 被 `if weight_sum > 0:` 门住（第 271 行），权重全 0 时**整段跳过**。
2. `fair = pending // len(active)`（第 262 行）在 `pending < len(active)` 时恒为 0 ⇒ `grant` 全 0 ⇒ `pending` 不减 ⇒ 循环空转，只能撞 `_MAX_SHAVE_ROUNDS` 护栏抛异常。
   护栏本身是**正确**的（它把死循环变成显式错误），但它抛的是 `RuntimeError` ⇒ HTTP 500。

**注意**：无容量路径（`_largest_remainder`）不受影响 —— 它 `eligible` 为空就自然剩进 `unallocated`，实测 `unallocated=1`。**只有"停用项带了容量"这一种组合会炸**，而 `models.PlanDraft` 的 JSONB 行示例里 `capacity` 与 `state` 是并列字段，两者同时有值是正常数据。

**修法（建议）**：`weight_sum == 0` 分支改为**按 `room` 均分**（每个 active 条目尽量 `+1`），即把第 262 行改成
`fair = min(pending, len(active)) 时的逐条 1`，或更简单：`weight_sum == 0` 时直接 `grant = {key: min(1, room)}` 逐条补到 `pending` 用尽。**无论怎么改，都必须补一条反向验证**：把 `if weight_sum > 0:` 的门去掉再跑，必须报红。

> ### ⚠️ 上条建议**已被否决**（阶段 5 落地时的实测结论）
>
> **建议的方向是"在零权重情况下继续分配"，而正确方向是"根本不参与分配"。**
>
> 落地的修法是**把 `active` 的判据从 `room > 0` 收窄为 `room > 0 and weight_permille > 0`**，
> 并让 `active` 为空时**直接 `break`**（`allocation.py` 第 325~336 行）。
>
> 为什么建议错了：
>
> 1. **按 `room` 均分会把额度分给停用项** —— 那正是同一处根因的另一半（MAJOR-5）。
>    建议里的"每个 active 条目尽量 +1"隐含"active 里都是正常条目"，
>    而当时 `active` 的定义是 `room > 0`，**包含停用项**。
>    照建议改，MAJOR-1 的死循环消失了，但 MAJOR-5 会**被固化进实现**。
> 2. **C14 原文的口径是"剩余计入 `unallocated`，不硬塞"**（§3.6.1）。
>    零权重场景不该"想办法分完"，而该让 `pending` 落进 `unallocated` ——
>    这与无容量路径（`_largest_remainder`）的读数**自然地一致**
>    （实测无容量路径 `unallocated=1`，不需要任何额外分支）。
> 3. 因此这是一次"**根治 vs 缓解**"的选择：建议是缓解（让循环能退出），
>    落地是根治（让不该参与的东西一开始就不在 `active` 里）。

**测试缺口**：`test_allocation_invariant_holds_exhaustively` 的穷举**没有构造容量**（`AllocationItem(... sort_order=index)` 不传 `capacity`），因此这条 3000+ 组的不变量证明**完全覆盖不到削峰路径**。`test_capacity_shave_moves_overflow_to_item_with_room` 等三个容量用例的权重都非 0。这是"穷举证明"与"容量路径"之间的一个**结构性缺口**。

> ### ✅ 该缺口已闭合（阶段 6）
>
> 穷举现在跑**两条路径**，各自 ≥1000 组（`checked_ok >= 1000` /
> `checked_capacity >= 1000`，都是能 FAIL 的下界断言）：
>
> | 路径 | 构造 | 覆盖 MAJOR-1 | 覆盖 MAJOR-5 |
> |---|---|---|---|
> | A 无容量 | `AllocationItem(...)` 不传 `capacity` | ✅ | ✅ |
> | B 有容量 | `capacity=max(1, total // 4)` | ✅ | ✅ |
>
> **`capacity` 与权重解耦**这件事是刻意踩坑后定的：初版写 `(total*w//1000)//2`，
> 于是 `w=0` 条目的 `capacity` 也是 0 ⇒ **没有 room** ⇒ MAJOR-5 反而不在覆盖面里，
> 反向验证**不报红**。改成 `max(1, total // 4)` 后，零权重条目始终有 room
> —— 那正是缺陷的触发条件（详见 `test_planning_wp24.py` 第 933 行的注释）。
>
> 缺口闭合的证据不只是"加了循环"：`result.unallocated >= 0` 与
> `sum(got.values()) + result.unallocated == total` 现在是**真断言**
> （前一条是独立边界，后一条配 `_expected_largest_remainder` 独立实现互证）——
> 关于"原来那条恒真式"见文末**错误 ⑪ / 错误 ⑫**。

---

### 🟠 MAJOR-2（新）`unallocated` 可为负数并被原样送到前端

**位置**：`allocation.py::allocate_by_weight`（第 321、326 行）+ `schema.py::AllocationBody.unallocated`

**探针实测**（`/tmp/probe_wp24_neg.py`）：权重合计 2000‰、`total=10` ⇒ `unallocated = -10`，且 DTO 照收（`int` 不拦负数）。

**为什么可达**：`save_draft` 明确允许保存合计 ≠ 1000 的中间态（这是**有意的**，见 `service.py` docstring"比例合计不在这里强制等于 1000"），而 `GET/PUT plan-draft` 的响应**无条件**带两维分配读数（`_draft_body` 的 `with_allocation` 默认 True）。于是"运营把比例从 300/300 拖成 300/300 再加一个 400"这一次保存的响应里，`unallocated` 就是负数。

**杀伤**：不变量 `allocated_total + unallocated == total` 仍然成立（我的探针验证过），所以**没有任何断言会因此报红**；前端按 §3.6.4 渲染的是"还剩 −10 条没分完"，读起来像系统算错。与项目铁律 #2 同型：**建立在"合计恒等于 1000"这个前提上的字段，在没有该前提的路径上给出了无意义的值。**

**修法（建议，二选一）**：
- 在 `allocate_by_weight` 里 `unallocated = max(total - sum(allocated.values()), 0)` 并加 `notes` 说明"权重合计 > 1000，已按比例缩放"；或
- 明确**不变量是 `sum(allocated) + unallocated == total` 且 `unallocated` 可为负**（写进 docstring + 前端文案处理）。

倾向前者：负的"未分配"没有业务含义。

---

### 🟠 MAJOR-3（新）`get_effective_limits` 不校验项目存在 ⇒ 读取侧可以凭任意 UUID 触发写入

**位置**：`settings_service.py::get_effective_limits`（第 256 行）

写了 `_append_setting` 的三条路径里，`change_global_setting` 与 `change_project_setting` 都显式校验了目标（前者枚举真实项目、后者 `repo.project_exists`），**只有惰性物化这条不校验**：`project_row is None and global_row is not None and materialize` ⇒ 直接 `insert_inherited_copy`。

**后果**：`runtime_settings.project_id` 上有 `ON DELETE RESTRICT` 的 FK，因此插入一个不存在的 `project_id` 会被 FK 拒绝（`IntegrityError`）。而 `_materialize_inherited_copy` 的重试循环只把**部分唯一索引冲突**当成可重试（`_is_setting_revision_conflict`），FK 违反会**原样抛出** ⇒ 端点 500，且错误文案指向数据库约束名。

**判定**：目前**不可达**——`get_effective_limits` 的唯一带 `project_id` 的调用者是 `change_project_setting`（它先校验过），以及测试/证据脚本。因此**不是现行缺陷，而是一处缺口的防线**（第一遍的定位：等 WP25/WP31/WP32 按 docstring 第 13 行的契约接上去，"传一个项目 UUID"就是常规写法，届时它会变成 500 而不是可读的 404）。

> ### ⚠️ 上条判定**部分不准确**（阶段 5 复核）
>
> **"唯一带 `project_id` 的调用者"这个前提当时就不完全成立**：
> `api.py::_overview_body` 用 `project_id=None`（对），但**本包之外**
> 没有任何调用者这件事只对当时的分支成立 —— 而 docstring 第 13 行写的是
> `WP25 / WP31 / WP32` 会接。真正的问题是：**判定用"今天的调用者"论证了
> 一个"明天的契约"**，两者不是同一件事。
>
> 更重要的是，"不可达"这个结论会**随一次无关的接线而失效**，
> 而失效的表现是 500（不是可读的 404）—— 属于"判据随别人开发而反向变化"。
> 因此阶段的处置与判定无关：**直接修**（多校验一次的代价是一次
> `project_exists` 查询，只在 `project_id is not None` 时发生，
> 与"全局总览"这条热路径无关）。
>
> 落地位置：`settings_service.py` 第 333~338 行。顺序刻意放在
> **取项目行之前** —— 不存在的项目本就没有项目行，先校验能让
> "404（项目不存在）"与"configured=False（未配置）"两个不同语义
> 不互相伪装。判据见 `test_get_effective_limits_rejects_missing_project`
> （服务层，两路 `materialize` 都测）。

**修法（建议）**：在 `get_effective_limits` 的 `project_id is not None` 分支最前面加一次 `repo.project_exists()` ⇒ 404 `REASON_PROJECT_NOT_FOUND`（与姊妹函数同口径）。同时把"未校验"这一条从"依赖调用方"改成"函数自己守"。

> ### ✅ 已按建议落地，并补了 HTTP 层的**可达性边界**判据
>
> `test_project_level_setting_read_has_no_endpoint`（第 4 层）钉住了一件
> 容易被忽略的事：**这个修复只能从服务层验证，因为项目级配置没有 HTTP 读出口**。
> 本包 router 的 8 个端点里带 `{projectId}` 的只有草稿三兄弟；
> `/settings/projects/...` 下只有 `diverged`（跨项目清单）与 `{projectId}/{key}` 的 PUT。
>
> 顺带实测到一个反直觉结果并当场记档：`GET /settings/projects/{ghost}/{key}`
> 返回的是 **400 而不是 405** —— 成因在 devtools 的 `Mount("/")`
> （`main.py` 第 398 行，`StaticFiles` 对非 GET 抛 405，而
> `_CODE_BY_STATUS` **没有 405 条目** ⇒ 折成 `MALFORMED_REQUEST`）。
> 该行为在 `test_devtools.py` 第 443 行有同款登记且**刻意不修**。
> 不记下来的话，排障时会把 400 误读成"路由挂错了"。
>
> 另外实测到：写侧幽灵项目的 404 是**依赖层**（`resolve_project_access`
> 查 `projects` 表取不到）拦下的，**不是**服务层的 `project_exists`
> —— 语句构成里没有任何 `runtime_settings` 查询，只有
> `user_sessions` / `users` / `projects`。即 HTTP 路径上服务层那道校验
> 被依赖**遮蔽**了。这不是缺陷（双层防线），但解释了为什么
> MAJOR-3 的判据**必须直接调服务层**才够得到。

---

### 🟡 MINOR-1（新）`disabledReason` 只存在于 docstring，服务层白名单不认它

`models.PlanDraft` 的 JSONB 行示例（第 288~296 行）明写有 `"disabledReason": null # 停用原因（原型有这一列）`，而 `service._ALLOWED_ITEM_KEYS` 只有 `{key,name,weightPermille,sortOrder,capacity,state}` ⇒ 请求体里带 `disabledReason` 会 422"含未知字段"。

**这是"文档声称"与"实现"的分歧**，且方向危险：前端照 docstring 的字段表发请求会稳定失败。二选一（都要改一处）：
- 把 `disabledReason` 加进 `_ALLOWED_ITEM_KEYS` + `DimensionItemBody`；或
- 从 `models.py` 的行示例里删掉它，并把"原型那一列本包不做"写清楚。

**倾向前者**：原型确实有这一列，且"为什么停用"是可解释性数据（与 `capacityShaved` 同一取向）。

**测试缺口**：`disabledReason` 在测试文件里出现 **0** 次，两边都不会拦住这个分歧。

---

### 🟡 MINOR-2（新）`ENABLED`/`DISABLED` 是字面量，且 `state` 未进 `_ALLOWED_ITEM_KEYS` 的常量源

`state` 的取值域在 `models.py`（JSONB，无 CHECK）与 `service._validate_dimension`（`("ENABLED", "DISABLED")` 字面量）两处各写一遍，而 `types.py` 里**没有** `ITEM_STATE_ENABLED` / `ITEM_STATE_LIST_SQL`。对比 `DRAFT_STATE_LIST_SQL` / `SETTING_ORIGIN_LIST_SQL` 都有常量。
`_to_allocation_items` 也硬编码 `row.get("state") == "DISABLED"`。

**风险**：与项目既有的"取值域单点定义"纪律不一致；`state` 将来加第三个取值（比如 `ARCHIVED`）时会漏改一处而**不报错**（`ARCHIVED` 会被 `_to_allocation_items` 当作启用，权重照算）。

**修法**：在 `types.py` 加 `ITEM_STATES` / `ITEM_STATE_LIST_SQL`，三处引用它。

---

### 🟡 MINOR-3（新）`tie_rank` 在容量削峰路径下**恒为 0**

**探针实测**（`/tmp/probe_wp24_hash.py`）：容量路径下 `floors[key] == allocated[key]`（第 281~283 行把每一轮的 `grant` 同时加进两者）⇒ 第 340 行的 `floors[key] < allocated[key]` 永不成立 ⇒ `tie_rank` 全 0。

而 `AllocationLineBody.tie_rank` 的 docstring 说它是"原型「最大余数法换算过程」表的直接数据源，也是'为什么这一类多一篇'的唯一解释"。**在有容量的维度上，这个解释字段是死的**（前端会显示一列全 0）。

**判定**：不报错、不出错值，但**字段的可解释性承诺在该路径上不成立**。修法二选一：容量路径给出"这一轮第几个拿到补 1"的真实次序，或在 docstring 里写明"容量路径下 `tieRank` 恒为 0（该路径的分配不由余数决定）"。
倾向后者（改一行注释），因为容量路径的分配本来就不按余数补。

---

### ✅ 第一遍已核对通过（不是问题，记录以免重复审查）

| 项 | 结论 |
|---|---|
| `payload_hash` 确定性 | `canonical_payload_hash` 用 `sort_keys=True` + `separators=(",",":")` + `default=str`，`plan_payload` 只放输入与结果、**不放**时间戳/actor/revision ⇒ 幂等命中成立 |
| SQL 注入 | 全模块无字符串拼接 SQL；`SETTING_KEY_LIST_SQL` 等来自 `types.py` 的常量拼装，取值域是闭集，无外部输入进入 SQL 文本 |
| 越权（草稿） | `baseline_belongs_to_project` 校验了 FK 不保证的"基线属于本项目"，闭掉了一处静默越权读取 |
| 越权（配置） | `PUT /settings/projects/{P}/{key}` 走 `ProjectAccessDep` ⇒ 非成员 404（防枚举），实测断言过 |
| 提交幂等 | 行锁（`FOR UPDATE`）+ 条件 UPDATE 双层；`updated == 0` 报 `draft_update_race` 而**不是**静默继续（静默继续会产出第二个 PLAN 版本） |
| 审计不可篡改 | `runtime_settings` 的不可变触发器 + 审计四要素（值/人/时间/理由/requestId）齐全；`reason` 服务层必填（DDL 只拦空串、放行 NULL，故必须有服务层这一道） |
| `lambda` 晚绑定 | `sorted(active, key=lambda it: _tie_key(it, total))` 用默认参数以外的方式闭包，`total` 在循环内不变，无晚绑定问题 |
| 除零 | `_capacity_shave` 与 `normalize_permille` 的除法都有 `> 0` 守卫 |

---

## 第二遍：并发 / 事务

### ⚠️ 并发正确性总体成立（advisory lock 的论证无误）

三条写路径（全局变更 / 项目覆盖 / 惰性物化）都在写前取**同一把** `pg_advisory_xact_lock(hashtext(key))`，且用 `xact` 版本（异常路径自动释放，不泄漏锁）。文档 §3.6.3 初稿的 `ON CONFLICT DO NOTHING` 已被实测否决并回填 —— 这一步是对的：`ON CONFLICT DO NOTHING` 在**不同值**竞争下会静默丢写，正是 J2 要拦的形态。

### 🟠 MAJOR-4（新）`change_global_setting` 的级联刷新逐项目取当前行 ⇒ N+1 查询

**位置**：`settings_service.py::change_global_setting`（第 577~594 行）

每个项目一次 `current_project_setting`（`SELECT ... ORDER BY revision_no DESC LIMIT 1`）+ 一次 `_append_setting`（内含 `max_revision_no` 的 `SELECT MAX()` + `INSERT`）。实测成本：一次 `change_global_setting` **1.09s / 写 1184 行**（测试库项目数 ~1184），**与项目数线性**。

**判定**：功能正确，但 `PUT /settings/global/{key}` 的延迟直接等于项目数 × 常数。M4/M5 若项目数到几百，这个端点的 p99 会变得很难看，而**它同时持有全局 advisory lock**（按 key 串行）⇒ 所有同 key 的写请求排队等待。

**修法（建议，不阻塞本包）**：`list_project_settings(key)`（已有 `DISTINCT ON` 的单次查询）替换掉逐项目 `current_project_setting`；`revision_no` 用一条 `INSERT ... SELECT` 批量取号（或一次 `SELECT key, project_id, MAX(revision_no) ... GROUP BY`）。已登记为**性能收口项**（不是正确性缺陷）。

> ### ✅ 已按建议落地（阶段 5），并附**语句计数判据**与反向验证
>
> 落地形态与建议略有不同，两处差异都有理由：
>
> | 事项 | 建议 | 落地 | 理由 |
> |---|---|---|---|
> | 项目当前行 | 一次 `DISTINCT ON` | 同 | —— |
> | 修订号取号 | `INSERT ... SELECT` 批量 / `GROUP BY` | **全局一次 `coalesce(max(...))` + 内存 `+1`** | 级联刷新写的是**同一把 advisory lock 下**、且每个项目只追加一行 ⇒ 序号在内存里递推与服务端 `MAX()` 等价，少一次往返 |
>
> **实测（本项目测试库，3835 个项目）**：
>
> | 指标 | 修复前 | 修复后 |
> |---|---|---|
> | `runtime_settings` 的 `SELECT` | **7671**（2 × 3835 + 1） | **2** |
> | 其中逐项目 `ORDER BY revision_no DESC LIMIT 1` | 3835 | **0** |
> | 总语句 | 11509 | 11509 |
>
> 总语句**没变**（3835 INSERT + 3835 SAVEPOINT + 3835 RELEASE + 4 条控制语句）
> —— 这是对的：级联刷新**必须**为每个项目插一行，`INSERT` 数是"没漏刷"的
> **正面证据**，不是性能问题。变的是 7671 → 2 条 `SELECT`。
>
> ✅ **反向验证已做**：把批量查询改回循环内 `repo.current_project_setting(...)`，
> `test_j2_cascade_uses_constant_number_of_selects` 报红：
>
> ```
> AssertionError: 级联刷新用了 3862 条 runtime_settings SELECT（项目数 3861）
>                —— 逐项目查询回来了，这是 N+1
> assert 3862 <= 3
> ```
>
> （`2 × 3861 + 1`；项目数比 3835 多是因为该用例自己又造了项目。）
>
> **判据本身踩过的坑**：断言 `insert_runtime == project_count` 时实测
> `3848 != 3847` —— 差的 1 行是**全局行自己**。已改成 `project_count + 1`
> 并把这条事实写进用例注释。

### 🟡 MINOR-4（新）只对 `key` 取锁，不锁 `project_id`

`lock_setting_key(key)` 的粒度是 key。两个不同项目的并发 `PUT /settings/projects/{P}/{key}` 会**互相串行**（无害，只是慢）。而已登记文档说明"锁粒度选 key 而非 key+project"的理由是保证级联刷新与惰性物化互斥 —— 这个理由成立，**记下代价即可**。

### ✅ 第二遍已核对通过

| 项 | 结论 |
|---|---|
| 提交的行锁顺序 | `get_draft_for_update` 在**读内容之前**取得（模块 docstring 第三节），因此"两个事务都读到 EDITING、都写 version_nodes"的杀链被堵死 |
| 条件 UPDATE 的 `synchronize_session=False` | 已用 `reload_draft`（`populate_existing=True`）显式重读，不会把过期实例回带进响应 |
| 取号重试的 SAVEPOINT | `_append_setting` / `_materialize_inherited_copy` 的 `flush` 都在 `repo.savepoint()` 内，一次约束冲突不会把外层事务标记为 aborted |
| 事务边界 | 副本刷新与全局变更在同一事务（§3.6.2 的核心承诺），实测无中间窗口 |
| 死锁风险 | 锁只有按 key 的 advisory lock 一把，且无"持 A 等 B"的嵌套；`FOR UPDATE` 只锁单行草稿 |

---

### 🔴 MAJOR-5（新增，第一遍漏掉）停用项在容量路径上**白拿分配额**

**位置**：`allocation.py::_capacity_shave` 的 `active` 定义（修复前为 `room > 0`）

**这一条与 MAJOR-1 是同一处根因的**两半**，但危害更大，且第一遍**完全没看到**。

**触发条件**：至少一个条目有 `capacity`，**且**有正权重条目被容量顶满，**且**此时 `active` 里只剩停用项（权重 0）。

**探针实测的形态**：

```
场景A 启用(1000‰, 容量 5) + 场景B 停用(0‰)
total=10  ⇒  A=5, B=5      ← B 已被运营点「停用」，却拿到 5 条
```

**为什么它比 MAJOR-1 更严重**：

| | MAJOR-1（死循环） | MAJOR-5（停用项白拿） |
|---|---|---|
| 表现 | `RuntimeError` ⇒ 500，**看得见** | 200，账面合计**恰好等于 `total`** |
| 后果 | 请求失败，运营重试或报障 | **给已停用的场景排了内容**，且账面无从发现 |
| 与模块开篇承诺 | 一致（护栏把死循环变成显式错误） | **违背**（模块开篇正是要防"账面看不出"） |

**为什么第一遍漏了**：当时的审查读的是"循环会不会退出"（MAJOR-1 的角度），
而 MAJOR-5 要问的是"`active` 里的东西**该不该**在里面" —— 这是两个不同的问题，
却共用同一个变量。**同一处代码上问错问题，就会得出"这里只有一个缺陷"的结论。**

**根因**：`active = [item for item in items if room[item.key] > 0]`
—— 只判容量，不判权重。于是"正权重条目满容"那一刻，停用项就成了 `active` 的全部内容，
走 `fair = pending // len(active)` 的兜底分支，**照分**。

**修法（已落地）**：

```python
active = [
    item for item in items if room[item.key] > 0 and item.weight_permille > 0
]
if not active:
    break          # 全部满容，或剩下的都是停用项 ⇒ 剩余进 unallocated
```

`active` 的判据收窄为**两个条件同时成立**，且 `active` 为空时直接 `break`
（而不是"想办法分完"）。判据三条：

1. `test_allocation_invariant_holds_exhaustively` 路径 B —— 零权重条目
   `allocated == 0`（穷举 ≥1000 组，`capacity=max(1, total // 4)` 与权重解耦）；
2. 同一穷举里的 `unallocated >= 0`（容量路径）；
3. ✅ **反向验证已做**：把 `active` 改回只判 `room > 0`，
   路径 B 的"停用项拿到了分配额"必须报红。

> ⚠️ **反向验证第一次做的时候不报红** —— 因为容量构造与权重耦合
> （`capacity=(total*w//1000)//2`），`w=0` 时 `capacity=0` ⇒ 停用项**没有 room**，
> 缺陷根本触发不到。这条经验已写进 `test_planning_wp24.py` 第 933 行的注释：
> **反向验证不报红时，先怀疑"缺陷有没有被真的构造出来"，再怀疑判据。**

---

## 第三遍：可维护性 / 一致性

### 🟡 MINOR-5（新）`save_draft` 的 `actor_id` / `request_id` 是死参数

`save_draft(repo, *, ..., actor_id, request_id)` 声明了两个参数，**函数体一次都没用**（grep 确认：`actor_id` 在 `service.py` 只出现在 `submit_draft` 的 `created_by=actor_id`）。端点照传（第 432~433 行）。`plan_drafts` 表也确实没有审计列。

**判定**：不是缺陷，但它是"**留下了将来会骗人的形状**"—— 读者会以为保存草稿有审计。二选一：删掉两个参数（改签名，本包内），或在 docstring 写明"当前无审计列，参数为 WP25+ 预留（届时会真的用）"。倾向后者 + 加 `# noqa: ARG001` 或用 `del`，与 `api.py` 里 `del user` / `del body` 的处理保持一致。

### 🟡 MINOR-6（新）`api.py::_overview_body` 对 `batch.limits` 查了两次库

第 254~270 行：先 `for key in sorted(SETTING_KEYS)` 里面含 `batch.limits`，循环结束后**又**单独查一次同一个键喂给 `BatchLimitsBody.of`。两次查询结果相同（同事务），但多一次往返，且**读的两次值若将来有人加缓存就会出现分歧**。
修法：循环里把 `setting` 存进 dict，循环后复用。

### 🟡 MINOR-7（新）`get_global_setting_history` 为了 422 而做一次"白查"

第 579 行 `settings_service.get_effective_limits(repo, key, None, materialize=False)` 只为触发未知键的 422，**返回值被丢弃**，随后 `_history_body` 再查一次 `list_history`。两次查询、两次不同语义。功能对，但注释里说的"不为未知键先查历史"其实**还是查了历史**（只是顺序反了）。
修法：抽一个 `ensure_setting_key(key)` 纯校验函数（不查库），两处共用。

### ✅ 第三遍已核对通过

| 项 | 结论 |
|---|---|
| `_DTO_CONFIG` 的 `populate_by_name=True` + `extra="forbid"` | 契约严格；`sceneitems` 拼错会 422 而不是静默忽略 |
| 三个 `of()` 显式赋值 | 在导入时就暴露字段名漂移（不用 `from_attributes`），`AllocationBody.of` 甚至能抓到 `weight_permille` 与属性名的一致性问题 |
| 已登记的口径不一致 | `diverged` 不校验未知键而 `history` 返回 422 —— **已被第 4 层用例钉住现状**，属契约层待裁定，不是缺陷 |
| 已登记的权限不可测 | `PUT /settings/global/{key}` 造不出 403（运营的 `KNOWLEDGE_MANAGE=True`）—— 已登记，且注释解释了为何不改动作 |
| 分层单向依赖 | `api → schema → settings_service/service → 纯函数 → repository → models/types`，无反向 import；`schema` 只依赖 `service.DraftView`（值对象）不依赖 repository |
| 常量单点 | `DEFAULT_BATCH_MAX` / `MIN_QUANTITY` / `MAX_QUANTITY_BOUND` / `SETTING_KEYS` / `REASON_*` 均在 `types.py` 单点定义，无第二处硬编码 |

---

## 汇总

| 级别 | 数量 | 编号 | 状态 |
|---|---|---|---|
| MAJOR | **5** | 1（`_capacity_shave` 不收敛 ⇒ 500，**可达**） | ✅ 已修（`active` 收窄 + 空即 `break`） |
| | | 2（负 `unallocated`） | ✅ 已修（入口拒绝合计 >1000 + 补归一） |
| | | 3（读取侧不校验项目存在 ⇒ 500 缺口） | ✅ 已修（`get_effective_limits` 加 `project_exists`） |
| | | 4（级联刷新 N+1，性能） | ✅ 已修（7671 → 2 条 SELECT） |
| | | **5（停用项白拿分配额）** | ✅ 已修（同 MAJOR-1 的根因另一半） |
| MINOR | 7 | 1（`disabledReason` 文档/实现分歧） | ✅ 已修 |
| | | 2（`state` 无常量源） | ✅ 已修 |
| | | 3（`tie_rank` 恒 0） | ✅ 已修（补注释，不改行为） |
| | | 4（锁粒度按 key） | 📝 已登记（记下代价，不改） |
| | | 5（`save_draft` 死参数） | ✅ 已修（`del actor_id, request_id`） |
| | | 6（总览重复查库） | ✅ 已修（`values_by_key` 复用） |
| | | 7（为 422 白查一次） | ✅ 已修（抽 `ensure_setting_key` 纯函数） |

**MAJOR-1 与 MAJOR-5 必须一起修**（同一处根因的两半）：
只修 MAJOR-1（让循环能退出）会把 MAJOR-5 固化进实现；
只修 MAJOR-5（排除停用项）会让死循环在"全零权重"下依然存在。

> ⚠️ **本表在上一次汇总时只有 4 个 MAJOR，漏了 MAJOR-5**。
> 漏的原因不是"序号跳号"，而是**同一处代码上只问了"循环会不会退出"、
> 没问"`active` 里的东西该不该在里面"**（详见 MAJOR-5 一节）。
> 保留这段说明，是因为"汇总表本身就是个自由文本"——
> 它没有任何机械判据与明细对账（项目铁律 #8）。

**测试缺口（三遍法共同暴露）**：
1. 穷举判据完全不覆盖容量削峰路径（`capacity` 参数没传）⇒ MAJOR-1 藏身于此；
   **✅ 已闭合**（路径 B，`capacity=max(1, total // 4)`，与权重解耦）；
2. `disabledReason` 在文档与实现之间分歧，而测试对它 0 次引用；
   **✅ 已闭合**（roundtrip + 三条不一致 + 边界含端）；
3. 无一条用例断言"合计 ≠ 1000 时的分配读数"，故 MAJOR-2 无告警；
   **✅ 已闭合**（`unallocated >= 0` 进穷举，两条路径都断言）；
4. **★ 已存在的穷举不变量断言本身是恒真式**（见下节，错误 ⑪）—— 它不是"缺口"，
   而是"**看起来在守、其实永远不报红**"，比缺口更危险。
   **✅ 已闭合**（改为与 `_expected_largest_remainder` 独立实现互证）；
5. **★★ 新：判据能"报红"，但报红的原因是错的**（见错误 ⑫）。
   **✅ 已闭合**（MINOR-7 判据从"未知键 0 条"改为"已知键恰好 1 条"）。

---

## ★ 判据自身的缺陷 —— 错误 ⑪：穷举不变量断言在结构上恒真

**发现路径**：写 MAJOR-1/2 的反向验证时，先想"那条 3000+ 组的企业级穷举为什么没抓到？
MAJOR-1 会抛 `RuntimeError`（会报红），但 MAJOR-2 的 `unallocated = -10` **居然也一次没报红**"——
于是回头读断言的两端。

**证据链**：

1. `types.py::AllocationResult.allocated_total` 是 **`@property`**：

   ```python
   @property
   def allocated_total(self) -> int:
       return sum(line.allocated for line in self.lines)   # types.py 第 431~433 行
   ```

2. `allocation.py` 里 `unallocated` 的定义就是同一个式子取反：

   ```python
   unallocated = total - sum(allocated.values())           # 第 321 / 326 行
   ```

3. 而 `lines` 是**由 `allocated` 这个 dict 逐项构造出来的**（同一个 dict，无第二条数据源）。

**⇒ 断言 `allocated_total + unallocated == total` 展开后是 `sum(x) + (total - sum(x)) == total`，
在代数上恒等，与输入完全无关。**

**探针实测**（`/tmp/probe_wp24_tauto.py`，400 组离谱输入：权重合计最高 8000‰、
`capacity` 乱填、`total` 从 0 到 5000）：

```
[断言失败次数] 0        ← 恒真式确认：一次都没破
[RuntimeError 次数] 2   ← MAJOR-1 的死循环倒是被抓到了（因为它是抛异常，不是断言）
```

**危害定级**：这是 ISS-95 那一类"**声称自己有防线比没有防线更危险**"的变体 ——
上一个会话在 `types.py::AllocationResult` 的 docstring 里明写
"不变量（`allocation.py` 的判据断言的就是它）：`sum(...) + unallocated == total`"，
并配了 3000+ 组穷举。**读者会以为分配读数已被守住，实际两个端点（`unallocated` 负值、
`allocated` 与 `lines` 不一致）都无告警。**

**修复方向**（阶段 6 必须落地，且必须能 FAIL）：

- 不变量改写成"**两条独立数据源互证**"，而不是"同一式子自证"：
  从 `allocate_by_weight` 的**入参 `items` 与 `total`** 出发重算一遍期望值
  （期望各条 `allocated` 由测试侧按最大余数法独立实现算），与返回值逐条比对；
- 同时钉死**边界**：`unallocated >= 0`（权重合计 ≤ 1000 时）必须是**断言**而非注释；
- 反向验证：把 `unallocated = total - sum(allocated.values())` 改成
  `unallocated = max(0, ...)`（掩盖负值）→ 新断言**必须报红**。

> ### ✅ 已全部落地（阶段 6）
>
> | 要求 | 落地 |
> |---|---|
> | 独立数据源互证 | `_expected_largest_remainder(weights, total)` —— 测试侧**独立实现**一遍最大余数法（含"权重 0 不参与补 1"），与 `result.allocated_by_key()` 逐条比对 |
> | `unallocated >= 0` 成为断言 | 两条路径各一条，都是真断言（不是注释、不是 `assert True`） |
> | 覆盖容量路径 | 路径 B（见 MAJOR-1 的缺口闭合表） |
>
> **为什么"独立实现"是这条的**唯一**解**：`AllocationResult` 的字段里
> `allocated_total` 与 `unallocated` 是**同一个 `allocated` dict 的两种投影**
> （前者 `@property` 求和、后者 `total - 求和`）⇒ 任何在这两者之间的等式
> 都是代数恒等。**只有从"入参"重算一遍才可能与"返回值"不一致。**
>
> 这与项目铁律 #2（"判据不能建立在数据库已保证的事上"）同源：
> **不能用被验证对象自己的两个视图互证。**

## 反向验证要求（修完必须做）

按项目铁律 #7（"判据必须能区分两种实现"），MAJOR-1 的修复必须配一条**能 FAIL 的反向验证**：
把 `if weight_sum > 0:` 的门恢复成"无条件补 1"，再跑新用例 —— **必须报红**。只加正例不算完成。

错误 ⑪ 的重写同样必须配反向验证：前述"把负值抹平"的改动必须让新断言报红；
否则说明新断言还是一条恒真式。

> **✅ 均已执行**，且执行过程中又暴露了一个**新的判据缺陷类型**（错误 ⑫，见下）。

---

## ★★ 错误 ⑫：判据**能报红**，但报红的**原因**是错的 —— "抛出点巧合"

**这是项目铁律 #1~#8 之外的一个新形态。** 错误 ⑪ 是"判据恒真、永不报红"；
错误 ⑫ 是"判据**看起来有区分力、也确实会报红**，但它报红的理由与它声称守卫的
缺陷**无关**" —— 因此它同样是**没有防线**，而且更难发现：
**反向验证时它表现正常**（改坏实现确实报红了），但报红来自另一条路径。

### 现场：MINOR-7 的判据（我第一版写的就是这个）

**缺陷本体**：`get_global_setting_history` 初版用
`get_effective_limits(repo, key, None, materialize=False)` **当键名校验用**，
返回值直接丢弃 ⇒ 每次读历史白跑一条 `current_global_setting`，
随后 `_history_body` 再查一条 `list_history` ⇒ **2 条**。

**我第一版判据**：发一个**未知键**请求，断言 `runtime_settings` 相关查询 **0 条**，
并在 docstring 里声称"把 `ensure_setting_key` 换回 `get_effective_limits`，本判据必须报红"。

**实测：改坏实现后判据依然通过（不报红）。**

### 根因：抛出点在查库之前

```python
def get_effective_limits(repo, key, project_id=None, *, materialize=True):
    ensure_setting_key(key)                                   # ← 未知键在这里就抛了
    global_row = repo.current_global_setting(key=key)         # 走不到
```

`get_effective_limits` 的**第一行就是** `ensure_setting_key`。
因此对**未知键**而言：

| 实现 | 未知键时的 `runtime_settings` 查询数 |
|---|---|
| 缺陷版（`get_effective_limits(...)` 当校验用） | **0** |
| 修复版（`ensure_setting_key(key)`） | **0** |

**两行数字相同 ⇒ 判据对两种实现给出同一结论 ⇒ 无区分力。**
我测到的"0 条"是真的，但它证明的是
"**异常的抛出点恰好在查库之前**"这个与缺陷无关的巧合，
而不是"没有白查"。

### 为什么这一类特别危险

| | 错误 ⑪（恒真） | 错误 ⑫（抛出点巧合） |
|---|---|---|
| 判据会报红吗 | 永不 | **会**（在真实缺陷上） |
| 反向验证能发现吗 | 能（改坏不报红） | **不能**（改坏确实报红） |
| 谁发现它 | 写反向验证时 | **只有把判据换到另一条路径上重测** |
| 伪装成什么 | "这条防线很强（3000+ 组穷举）" | "这条判据有区分力（反向验证通过了）" |

**关键区别**：错误 ⑫ 的判据在**它挑的那条输入路径**上确实区分了两种实现
—— 但它挑的路径（未知键）**恰好绕开了缺陷发生的位置**（已知键的正常读取）。
缺陷在"正常路径"上，判据在"异常路径"上，两者不相交。

### 处置：把判据挪到缺陷真正发生的路径上

判据改为**已知键**（正常读取路径）：

```python
_measure_statements(env, _read_history)   # 已知键 + 已有全局行
assert counted["select_runtime"] == 1     # 恰好 = list_history
```

实测：

| 实现 | 已知键时的 `runtime_settings` 查询数 | 判据 |
|---|---|---|
| 缺陷版（`get_effective_limits(...)` 当校验） | **2** | ❌ 报红 |
| 缺陷变体 2（`ensure_setting_key` + 显式多读一次全局行） | **2** | ❌ 报红 |
| 修复版（只 `ensure_setting_key`） | **1** | ✅ 通过 |

**两个不同的缺陷变体都报红** —— 这才是"判据有区分力"的证据。
（只测一个变体不够：它可能只识别"这一种写法"而不是"这一类缺陷"。）

### 提炼成可复用纪律

> **判据必须打在"缺陷会发生的那条路径"上。**
> 对一个关于**正常路径代价**的缺陷（多查一次库、多一次往返），
> 用**异常路径**（错误输入、边界值）去检验是无效的 ——
> 异常路径往往在到达缺陷点之前就已经分支出去了。
>
> **自检方法**：写下判据后问一句
> "**缺陷是在这条输入下发生的吗？**"
> 若答案不确定，就把它当成错误 ⑫ 的候选，换路径重测。

### 本包已有的同类判据复核（执行结果）

按上面这条纪律回扫本包全部"计数型 / 差分型"判据，确认它们打在正确的路径上：

| 判据 | 缺陷发生的路径 | 判据用的输入 | 结论 |
|---|---|---|---|
| `test_j2_cascade_uses_constant_number_of_selects` | 全局变更的**级联循环**（正常路径） | 一组已物化的项目 + 一次正常全局变更 | ✅ 正确路径 |
| `test_overview_reads_each_setting_key_exactly_once` | 总览的**循环之后**（正常路径） | 已配置 + 未配置并存的正常总览 | ✅ 正确路径（实测反向验证 4 → 报红） |
| `test_history_reads_rows_exactly_once` | 历史的**正常读取** | 已知键 + 已有全局行 | ✅ 修正后正确路径（原来在错误路径上） |
| `test_j2_all_inherited_projects_follow_global_change` | 级联刷新的**全项目覆盖** | 穷举全库项目 | ✅ 正确路径 |
