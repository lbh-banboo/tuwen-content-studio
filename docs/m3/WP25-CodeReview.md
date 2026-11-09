# WP25 Code Review（三遍法）

对象：`services/backend/src/rednote/modules/billing/`（**12 个文件 / 7751 行**）
测试：`services/backend/tests/test_billing_wp25.py`（**107 条用例**，四层范式）
基线：`107 passed`（本包）+ 全量 **`1924 passed / 0 skipped`**
方法：先读全文再下结论；凡与 docstring 声称不符者一律**以实测为准**。
机械指认：`evidence/m3/wp25-code-review.log`（**40 条判据**，可复跑）

> **本文档的读数口径**：每一条"修法"都写明它是**建议**还是**已落地**。
> 凡标注 **⚠️ 已被否决 / 部分不准确** 的，都是落地时被实测推翻的当初判断 ——
> 保留原文 + 否决理由，读者才知道"为什么不是那样改"，
> 而不是把建议悄悄改成结论（那会让本文档变成一份"事后合理化"的记录）。

---

## 与 WP24 的四处结构性不同（先讲清楚，后面每节都建立在这上面）

| # | 差异 | 后果 |
|---|---|---|
| 1 | **本包的 MAJOR 不是"文档声称"，而是实测抓到** | 做了完整的 probe → apply → red → revert → green 五步验证，见 MAJOR-1 |
| 2 | **本包有一条"用例缺陷"，前两遍都看不见** | 暴露路径是"换一个由迁移建出的库"，见 **CR-3** |
| 3 | **本包的 CR 收集器自己恒返回退出码 0** | 判据报红对下游完全不可见，见 **CR-2 TOOLING-1** |
| 4 | **`within_limit` 是教科书级恒真式** | 对它的判据必须**反向**：证明它拦不住并发 |

---

## 第一遍：正确性 / 安全

### ✅ 第一遍通过项（判据在 `wp25-code-review.log` 逐条可查）

| 判据 | 检查内容 | 实测 |
|---|---|---|
| `[C1]` | 12 个文件无裸 `except` / 宽 `except`（AST 判定） | **0 处** |
| `[C2]` | 生产代码无 `assert`（`python -O` 会剥离防线） | **0 处**（本包已清零） |
| `[C2b]` | 键缺失路径改用显式 `if ... is None: raise RuntimeError` | 守卫 ✅ RuntimeError ✅ |
| `[C3]` | 无可变默认参数（AST 判定） | **0 处** |
| `[C4]` | `service/api/reconciliation/repository` 无 `commit`/`rollback`（剥离注释后） | **0 处** |
| `[C5]` | SQL 实参无 f-string / `%` / `.format` 插值（AST 判定） | **0 处**（全部绑定参数） |
| `[C6]` | `service.py` 写库前经 `money.quantize` | **15 处** |

### 🟡 MINOR-1（新）指纹函数的 docstring **声称的第二道判据实际未接线**

**位置**：`pricing.py::pricing_version_fingerprint`

**这是 ISS-95 的教科书形态**：函数存在、有确定性实现、docstring 声称
"对账能立刻发现单价被绕过修改" —— 但：

- `reconciliation.py` **不 import** 它（`[C28 反向]` 实测确证）；
- `cost_estimates` 表**没有**存指纹的列（`[C28 反向]` 实测确证）。

⇒ 读者会以为"对账在用它"。**处置：不删函数，如实标注状态。**

**修法（已落地）**：docstring 增加
`## ⚠️ 当前状态：**原语已备，接线未做**`，并逐条列出"谁没接":
不 import 的模块、缺的列，以及"接线是哪个包的活"。
由 `[C28]` 守卫：docstring 里**必须**出现"未接线"，否则报红 ——
也就是说这条标注**不能**悄悄消失（否则又变回"声称有防线"）。

---

## 第二遍：并发 / 事务

### 🔴 MAJOR-1（本包唯一 MAJOR，**实测抓到**）`append_ledger_entry` 并发同键 ⇒ HTTP 500

#### 发现路径（静态判据**没有**报红）

是**读实现时发现 docstring 与代码不一致**：

```python
# repository.find_ledger_entry_by_idempotency 的 docstring（原文）
『直接 INSERT → 撞 23505 → 回查。**而不是**先查再插』
```

而 `service.append_ledger_entry` 的实现正是"**先查再插**"，
**且不捕获 `IntegrityError`**：

```python
existing = repo.find_ledger_entry_by_idempotency(key)   # ← 先查
if existing is not None: return existing
...INSERT...                                            # ← 再插
```

⇒ 这是 ISS-95 形态：**声称自己有防线**（`repository` 层写对了流程，
`service` 层没照它做，而看代码的人读到那段 docstring 会以为已处理）。

#### 实证 ① 探针（8 线程同键并发追加）

| 状态 | 结果 |
|---|---|
| 修复前 | 返回成功 **3**（`ids=['d04daf03-...']`，只有同一个 id）/ 抛异常 **5**（`IntegrityError/23505`） |
| 修复后 | **8/8** 收敛到同一条，**0** 抛异常，库里**恰好 1 行** |

而"同一笔账单重复导入"在业务上**完全正常**，报 500 是错的。

#### 根因：为什么"先查再插"在并发下必错

两个请求同时查到"不存在"（窗口就在这两行之间），于是都去 INSERT，
其中一边撞 `uq_usage_ledger_idempotency`，`IntegrityError` 被 `session_scope`
原样抛出 ⇒ 调用方看到 **HTTP 500**。

#### 修法（已落地）：SAVEPOINT + 捕获 `23505` + 回查

新增 `_is_unique_violation(exc)` 与 `_insert_ledger_entry_idempotent(...)`：

```python
try:
    with repo.session.begin_nested():          # ← SAVEPOINT
        written = repo.insert_ledger_entry(...)
        repo.flush()
except IntegrityError as exc:
    if not _is_unique_violation(exc):
        raise                                  # FK / CHECK 是真问题，原样冒
    if entry.idempotency_key is None:
        raise RuntimeError(...)                # 显式，不用 assert
    existing = repo.find_ledger_entry_by_idempotency(entry.idempotency_key)
    if existing is None:
        raise RuntimeError(...)
    return existing
return written
```

**三个设计点，每个都有理由**（写进了函数的 docstring）：

| 点 | 为什么 |
|---|---|
| 用 **SAVEPOINT** | `23505` 会让**整个事务**进入 aborted 状态（PG：任一语句失败后后续报 `25P02`）。不划子边界，回查那条 SELECT 会立刻失败 |
| 只对 **`23505`** 回查 | 其它 `IntegrityError`（FK / CHECK）是**真问题**，必须原样冒出去 —— 否则会把"数据不一致"伪装成"重复导入" |
| 判定走 `ddl.UNIQUE_VIOLATION_SQLSTATE` 常量 | **不看 message 文本**：文本随 PG 版本/语言变，`23505` 是 SQL 标准里的稳定标识 |

**保留"先查一次"是有意的**（`[C8]` 守卫）：它是**快路径**
（重复导入的常见形态是"早就记过了"，省一次必然失败的 INSERT + SAVEPOINT 回滚）。
但它**不能替代** `except` —— 并发正确性**只**来自 `23505` 捕获。
docstring 里显式写了这一点，因为这是最容易误读成"冗余"的地方。

#### 实证 ② 反向验证（apply → red → revert → green）

把 `append_ledger_entry` 的调用点换回裸 `_insert_ledger_entries`（= 修复前形态）：

```
AssertionError: 并发同键追加有 3/8 抛异常（['23505']）
                23505 必须被当成正常并发结果回查，不能冒成 500。
恢复修复后 ⇒ 105 passed / 0 failed（含该判据）
```

> ⚠️ **这一步不可省**（判据纪律第 5 条：『没跑』与『跑失败』同形）：
> 若不退回缺陷实现，就无法证明这条判据**真的打在缺陷路径上** ——
> 而原来的**串行**用例 `test_append_ledger_entry_is_idempotent_on_key`
> 在缺陷实现下**照样绿**（它只走快路径），正是"判据没打在缺陷所在路径"的形态。

### 🟡 MINOR-2（已修）幂等原语里的键缺失守卫原本写成 `assert`

`assert` 在 `python -O` 下会被剥离，防线**凭空消失**；
此后 `find_ledger_entry_by_idempotency(None)` 会渲染成
`WHERE idempotency_key IS NULL` ⇒ 返回**任意一条无键分录** ——
调用方拿到"成功但对象是别人的"视图（最难的静默错值）。

**已改为显式 `if ... is None: raise RuntimeError(...)`**，并由 `[C2b]` 守卫。

> **【跨包登记】** 全仓另有 **24 处**同型 `assert`（`audit`/`baseline`/`brief`/
> `files`/`gateway`/`jobs`/`knowledge`/`parsing`/`planning`），其中
> `planning/allocation.py:364` 被 WP24 当作正当防线。
> **当前部署未用 `-O` / `PYTHONOPTIMIZE`（全仓实测）**，故当前不失效。
> 本包先做到零，并把『全仓 assert 防线在 `-O` 下失效』登记为跨包议题 ——
> **不在本包单独发明新写法后沉默**（否则全仓会同时存在两种真相）。

### ✅ 第二遍通过项

| 判据 | 检查内容 | 实测 |
|---|---|---|
| `[C7]` | `_insert_ledger_entry_idempotent` 存在（幂等载体） | `L1033` |
| `[C11]` | 三条账面 UPDATE 的条件**写在 WHERE 里**（reserve/settle/release） | 三处齐备 |
| `[C12]` | `release_budget` 非正 delta **早返回**（重复释放幂等） | 命中 |
| `[C13]` | `set_budget_limit` **单语句双条件**（额度可容纳 + 币种一致） | 命中 |
| `[C14]` | 循环体内**无逐行查询**（无 N+1） | **0 处** |
| `[C15]` | `ensure_budget` 走 `ON CONFLICT DO NOTHING`（并发建行不撞 23505） | 命中 |
| `[C16]` | `_require_positive_amount` 存在（0 元预占会通过所有并发判据） | `L188` |

> **`[C11]` 是"预算并发预占 = W10 验收线"的实现点**：
> 条件写在 `WHERE` 里 ⇒ 靠数据库的行锁原子性判；
> 若写成"先 SELECT 判断、再 UPDATE"，两个并发请求会**同时通过判断**，
> 超发就发生了 —— 而这不会让任何断言报红（账面事后看是"合法"的）。

---

## 第三遍：边界 / 加固

| 判据 | 检查内容 | 实测 |
|---|---|---|
| `[C17]` | `IMMUTABLE_TABLES`（`models.py` 单一来源）== `(pricing_versions, usage_ledger)`，且 `ddl.py` **import 它并真的驱动触发器** | declared ✅ imported ✅ drives ✅ |
| `[C18]` | 部分唯一索引 spec **逐字段**：名 + 键列（含顺序）+ WHERE `idempotency_key IS NOT NULL`，且 WHERE **真的进 SQL** | 四项齐备 |
| `[C19]` | `within_limit` 存在，且文档/注释**显式说明它是恒真式**（不是并发防线） | ✅ |
| `[C20]` | `recompute_committed` **不过滤符号**（冲正分录必须计入） | 无反滤 |
| `[C21]` | `recompute_reserved` 走 `HOLDING_RESERVATION_STATES`（含 `EXPIRED`） | 命中 |
| `[C22]` | `find_usable_pricing_version`：仅可用状态 + 生效下界 + **专属优先** | 三要素齐备 |
| `[C23]` | `estimate_cost`：数量校验**在取价之前** | 位置顺序正确 |
| `[C24]` | `settle_reservation`：`Σ entries != actual` ⇒ **拒绝**（不替调用方选一个当真） | 命中 |
| `[C25]` | `settle_reservation`：`entries` 为空**自动补一条 BILL** | 命中 |
| `[C26]` | `set_budget` 拒绝负额度 / 已有金额时改币种 | 命中 |
| `[C27]` | `api.py` **恰好 14 条路由**（与第 4 层用例互证） | 14 条 |
| `[C35]` | 账本分录**两处 kwargs 构造保持同步**（复制是已知形态，把"同步"变成判据） | 11 键 / 11 键 / 一致 ✅ |

### ★ `[C19]` 单列一节：`within_limit` 是恒真式，判据必须**反向**

约束本体：`committed_amount + reserved_amount <= limit_amount`。

**它只在"绕过条件 UPDATE 直接写"时才响** —— 因为所有正常写路径都先在
`WHERE` 里判过（`[C11]`），能走到 UPDATE 的行必然已满足该式。
⇒ 它**不是并发防线**（并发防线是 `WHERE` 里的条件 + 行锁）。

这正是项目纪律第 2 条（"不能把数据库已保证的事当判据"）的教科书案例，
因此 `[C19]` 的判据**不能**是"约束存在" —— 那是个恒真式。
必须是：**约束存在 且 文档显式说明了它为什么是恒真式**。
否则读者会以为"有 CHECK 兜着"，从而敢于去掉 `WHERE` 里的条件。

### 🟡 DUPL-1（阶段 7 产出）两处 21 行逐字相同的 kwargs 构造

`_insert_ledger_entry_idempotent`（`service.py:1092`）与
`_insert_ledger_entries`（`service.py:1163`）在**同一位置**有 21 行
**逐字相同**的 `insert_ledger_entry(...)` 构造（AST 归一化后仍完全相同）。

**判定：保留不合并**，理由（按重要性）：

1. **合并会改变两个字段校验的求值顺序** —— 现在 `source_ref` 先于 `description`
   求值，而测试 `test_append_ledger_entry_source_ref_too_long_is_422` 断言的正是
   `fieldErrors[0]["field"] == "sourceRef"`。抽 helper 时若有人顺手换序，
   **两个字段单独超长的用例全都照样绿**，只在"同一请求两个字段同时非法"时现形
   （前端把错误提示标到另一个输入框）—— 典型静默错值。
2. 收益仅 21 行，代价是一个新的"分录行结构"（`append_ledger_entry` 手里本就是单条）。
3. 项目内既有判例：WP24 §四 对 14 个"参数 > 4"的函数给出的理由同款。

**但"判定不合并"不等于"不需要检查"** ⇒ 升级为判据 **`[C35]`**：
断言两处**关键字序列完全一致**，配反向验证（只把 batch 版两个 kwarg 换序 ⇒ 报红）。
详见 `evidence/m3/wp25-simplify.log` 第三节与第五节。

---

## ★★ CR-3：一条"环境相关"的**用例缺陷**（前两遍都看不见，`ISS-170`）

**这是本包最值得记录的一节** —— 它不是实现缺陷，而是**用例缺陷**，
且它的暴露路径**不是读代码，而是换一个库**。

### 现象

`test_estimate_cost_without_limits_configured_is_422`：

| 库 | 结果 |
|---|---|
| `rednote_test`（**夹具建表**，无全局行） | ✅ 绿 |
| 迁移建出的库（`rednote_test_wp25` / `rednote_fresh_probe`） | 🔴 `Failed: DID NOT RAISE <ApiError>`（`test_billing_wp25.py:1776`） |

### 根因

该用例用"**另一个项目不写项目级配置**"冒充"未配置上限"。
但 `GLOBAL batch.limits` 是 **WP24 迁移 `022ba9804d66` 主动种下的**
（其 docstring §五：调 `bootstrap_defaults` 写 ADR-13 初始行，为存量项目回填 `INHERITED` 副本）。

`get_effective_limits` 对"全局有 + 项目无"返回 `source='GLOBAL'`
⇒ 项目**继承**默认值 ⇒ `as_limits()` 成功 ⇒ **不抛 422**。

⇒ **这条用例的通过条件是"这个库没跑过 WP24+ 的迁移"** ——
而"测试库应由迁移建出"恰恰是本项目的标准做法（第 17 条纪律）。

> **判决：用例把库的**历史**当成了前提，而实现（继承全局默认值）是 ADR-13 要的。**
> **错的是用例，不是实现。**

### 为什么它在两遍法里都不会暴露

| 审查手段 | 为什么看不见 |
|---|---|
| 第一遍（读代码） | 用例本身的逻辑**自洽** —— 它确实构造了一个"没有项目级配置"的项目 |
| 第二遍（并发/事务） | 与并发无关 |
| 在单库上跑用例 | 在夹具建表的库上**一直绿**（夹具不跑迁移 ⇒ 没有全局行） |

⇒ 只有**换一个更接近生产的库**（迁移建出）跑同一份测试，差异才现形。
`[C34b]` 把这个根因**坐实到那个迁移的源码里**（断言 `022ba9804d66`
确实含 `bootstrap_defaults`）—— 不靠文档声称。

### 修法（已落地）：把前提**显式化**，而不是删用例或换库

```python
unconfigured = EffectiveSetting(
    key=SETTING_KEY_BATCH_LIMITS, configured=False, value=None, source=None,
    global_value=None, diverged=False, revision_no=None,
    project_id=env.other_project_id,
)
monkeypatch.setattr(
    planning_settings_service, "get_effective_limits", lambda *a, **k: unconfigured
)
```

显式构造"全局、项目都没有"的语义 ⇒ 与**库怎么建出的无关**。

### ⚠️ 打桩有一个比原缺陷更坏的风险：**变成恒绿**

打桩若把用例改成"无论实现怎样都过"，那比环境依赖更坏（ISS-95）。
因此做了**区分力反向验证**（`/tmp/wp25/reverse_limits.py`，5/5 PASS）：

| 步骤 | 断言 | 实测 |
|---|---|---|
| 变异锚点唯一 | 只出现 1 次（否则会注错位置） | ✅ |
| 基线（修后） | 绿 | ✅ exit=0 |
| **退化实现**：`except ValueError:` 分支**兜底默认值** `{TRIAL:10, SCALE:30, BULK:50}` | **报红**（`DID NOT RAISE`） | ✅ exit=1 |
| 恢复 | `service.py` 字节与原文一致 | ✅ `667893da30ca20d5` |
| 恢复后 | 回到绿（证明报红确由变异引起） | ✅ exit=0 |

⇒ 用例**仍有区分力**（能识破"用默认值兜底"这种退化实现），不是被改成了恒绿。

> **一处必须实测而不能推断的细节**：`service.estimate_cost` 对
> `get_effective_limits` 用的是**函数内局部 import**（避开 `billing ← planning`
> 的模块级依赖）。`monkeypatch.setattr` 模块属性**照样生效**
> （局部 `from ... import name` 每次执行都去模块取属性）——
> 但这必须**实测**，故上面那 5 步就是它的实测。
> 由 `[C34]` 守卫"确实打了桩"、`[C34c]` 守卫"仍断言具体原因码
> `LIMITS_NOT_CONFIGURED`"（不是改成"抛任何错就算过"）。

### ★ 由此提炼的一条**新判据型反模式**（已登记为 `ISS-170`）

> **用例的通过条件若依赖"库处于某个历史状态"（没跑过某迁移 / 没有某行），
> 它就是一个时间炸弹：迁移一落地，它从绿变红，而没人改过那行代码。**

这一形态与既有纪律的关系：

| 已有纪律 | 本条的区别 |
|---|---|
| ISS-44（判据里"恰好停在某版本"） | 那说的是**断言**；本条说的是**用例的前提**（不写出来但等价于断言） |
| 第 2 条（判据不能建立在 DB 已保证的事上） | 方向相反：这里是**建立在 DB 恰好没做的事上** |
| 第 17 条（测试库隔离） | 本条是它的**推论**：既然库由迁移建出，用例就不能假设"迁移没跑过" |

**可机械化的部分（本包已做）**：`[C34]` 断言那条用例**必须**用打桩
（而不只是"没有 configured=False 就报红"—— 那样会被"改用别的写法"绕过）。
**不可机械化的部分（如实说明）**：扫描"哪些用例依赖库的历史状态"
**做不到**一般化 —— 本包实测扫过 29 个测试文件，
"`other_project`"形态在**全线**出现（`baseline` / `brief` / `research` /
`planning` / `billing` 共 60+ 处），而它们**绝大多数是正当的跨项目隔离测试**。
⇒ 形态扫描无法区分，**不建立**该判据（否则会产生大量假阳性，
最终被人加白名单关掉 —— 恒 FAIL 的判据与恒 PASS 的一样无用）。

---

## ★ CR-2 TOOLING-1：CR 收集器**恒返回退出码 0** ⇒ 判据报红没有机械后果（`ISS-168`）

**这是本包在"判据自身的缺陷"上发现的一条新形态。** 已登记为 **`ISS-168`**。

### 现场

首轮运行时本脚本报 `31 PASS / 4 FAIL`，而 `echo $?` 是 **0**。

这不是笔误，是本脚本**当时**的真实形态：

- 结尾只有 `write_text(...)` + `print(...)` —— **没有任何退出码语句**；
- 也没有 `EXPECTED_CHECKS`，也没有 `RESULT=` 行。

### 后果链（逐环可核对）

1. `tools/m1/rerun_all_evidence.py`（收口第 11 步的载体）对每个脚本只断言两件事：
   `rc == 0` 与"证据文件 mtime 前进"—— 一个**恒返回 0** 的脚本，**两条都会 PASS**；
2. 它虽然会把 `RESULT` 列进表格，但那列**只打印、不参与任何断言**；
3. ⇒ **判据红了，没有任何机械后果。**

这是 **ISS-143 的变体**（"没跑"与"跑失败"同形）：
**"判据报红"与"判据全绿"在机器可读层面同形。**
它比"4 条判据口径错"更危险 —— 口径错只是局部失真，
而这里整份证据的"红/绿"对下游**完全不可见**（ISS-95：声称有防线）。

### 修正与防复发（已落地）

| 动作 | 内容 |
|---|---|
| 修正本脚本 | 补 `EXPECTED_CHECKS` + `RESULT=` 行（写出期望条数与条数核对）+ `raise SystemExit(0 if _verdict else 1)` |
| 防复发（全仓） | `tools/m1/check_evidence_scripts.py` 新增第五条检查 `check_returns_exit_code` |
| 判据口径 | 凡累积 `checks` 的脚本必须有**非裸 0** 的显式退出码；`SystemExit(0)` 视为"没有退出码"（行为等价，只是看起来更努力） |
| 同款一并修 | `tools/m3/collect_wp24_code_review.py` 有**完全相同的缺陷**（已随 `afdd91f` 提交）—— 它**只因为"恰好 0 FAIL"而从未暴露**，那是运气不是防线 |

### ⚠️ 这条检查的第一版自己踩了同型的坑（必须记一笔）

第一版用**文本正则**找退出码。而 `_strip_comments()` **只删整行 `#` 注释，
不删字符串字面量** —— 本仓脚本**故意保留**旧写法的说明文字，
其中就有用引号引起来的 `raise SystemExit(0 if ... else 1)`。

实测：该正则在同一份文件里命中 **3 处，其中 2 处是引文**。
把**真**退出码删掉后，正则**照样命中引文** ⇒ 检查**不报红** ⇒
反向验证用例永远拿不到 `rc=1`。

> **这与 `[C31]`（docstring 里的 `inbound == 0` 反例）是同一个坑**，
> 只是这次坑在"判据自己的文档"里 ——
> **写判据的人也会犯他要防的那个错。**
>
> 故改为 **AST**：只认 `ast.Raise` / `ast.Call` 节点，
> 字符串字面量里的文字**永远不是节点**。实测（全仓 26 个 CHECK 模式脚本）：
> AST 口径与正则口径**逐脚本零差异**，改口径不会把好脚本判红。

---

## ★ 判据口径修正记录（4 条判据**恒 FAIL** 的根因）

首轮运行时本脚本报 `31 PASS / 4 FAIL`。逐条定位后：
**4 条全部是判据口径错，不是实现错。**
这与 WP24 的"**错误 ⑫**"同族 —— 判据本身在两种实现下报同一个结果。

> **【为什么必须记下来】** 判据纪律：**恒 FAIL 的判据与恒 PASS 的判据一样无用。**
> 恒 FAIL ⇒ 人会去改脚本或干脆关掉它 ⇒ 真正的回归从此无人看着（ISS-95 的近亲）。
> 故每一条都附"根因 + 修正 + 反向验证"，而不是默默把正则改一行了事。

| # | 判据 | 恒 FAIL 根因 | 修正 | 反向验证 |
|---|---|---|---|---|
| ① | `[C17]` | 在 `ddl.py` 里正则找 `IMMUTABLE_TABLES = (...)`，但该常量**定义在 `models.py`**（`ddl.py` 只 import —— 反向 import 会成环） | AST 取 `models.py` 的**字面量真身**，另加"`ddl.py` 必须 import 它"+"必须真的用它驱动触发器" | 把 `models.py` 改成只留 `("pricing_versions",)` ⇒ **变红** ✓ |
| ② | `[C18]` | `PARTIAL_UNIQUE_INDEXES` 真身是 **tuple-of-tuple**，旧正则写的是 dict 形态 `= \{` | AST 取真身 + 逐字段比对 + "WHERE 真的拼进 SQL" | 把 WHERE 写成 `None` ⇒ **变红** ✓ |
| ③ | `[C31]` | 在**原始文本**里找 `inbound == 0`，命中的是 docstring 里**逐字写的反例说明** ⇒ 把"讲解为什么不能这么做"当成了"真的这么做了" | 正向断言用 `cross_package_foreign_keys`，反向断言在**剥离 docstring 后**无 `inbound == 0`（**双向**：只做反向会被"整段删掉"骗过） | 注入一段真实的 `assert inbound == 0`（非 docstring）⇒ **变红** ✓（剥离后命中 0→1） |
| ④ | `[C32]` | **(a)** 逐行正则抓 `down_revision`，但迁移 docstring 里存在**以 `down_revision` 起头的说明行**（`down_revision = 同 (key,...) 最大那行 \|`），被当成赋值 ⇒ 解析出 9 个假 head；且 `revision: str = "..."` 是 **AnnAssign**，旧正则也匹配不到 | AST 取 `revision`/`down_revision` + **拓扑**断言（见下） | 把 wp25 迁移的 `down_revision` 改成 `None` ⇒ **变红** ✓（heads 1→2） |

### ④ 的两层修正（第二层是"避免时间炸弹"，尤其值得记）

若修 (a) 时改用**等式** `heads == ["daea2b27212c"]`，就会埋下时间炸弹 ——
下一个包（WP26）**合法地**追加一个迁移后，head 必然推进，
判据在**没人做错任何事**的情况下变红
（ISS-44/64/71/83/87 已现身 **5 次**的形态，且本文件会被收口第 11 步复跑）。

⇒ 改用两条**拓扑**断言（对任何后续包恒成立）：

1. `len(heads) == 1` —— alembic 的硬约束（多 head 会 `Multiple head` 直接崩）；
2. `daea2b27212c` 是 head 的**祖先**（沿 `down_revision` 回溯可达）—— "我建的还在链上"。

**为什么拓扑比"链长/字典序"好**：后者是"**长度/字典序近似**"
（ISS-115 的恒真式：本项目全部 revision id 都是 12 位十六进制，
`len(a) >= len(b)` 在不相等时**恒为 True**）；拓扑可达性是**事实**。

> **【四条口径修正的反向验证证据】** `/tmp/wp25_cr_caliber_reverse.log`（本机留存）：
> 阶段 1 正常态 4/4 绿 · 阶段 2 四条退化**各自**变红且**无连带误报** ·
> 阶段 3 恢复后回到 4/4 绿 · 阶段 4 git 零残留。
>
> **"无连带误报"这一项必须核**：如果改坏 A 导致 B 也红，
> 那说明两条判据**不独立**（判据纪律第 4 条）——
> 它们可能只是"同一事实的两种写法"，加起来仍然只覆盖一件事。
>
> ⚠️ 这一步不可省（纪律第 7 条）：改判据若不做反向验证，
> 只是把"假红"换成"**假绿**" —— 那是更坏的形态，因为它看起来在保护你。

---

## ★ 收口期追加 `ISS-171`：**反向验证脚本自己的基线写死了**（尺子没跟着涨）

这一条不在阶段 5 的三遍法里，是**收口期重跑**时才暴露的（与 `ISS-142` / `ISS-143` 同一发现窗口）。
它值得单独成节，因为它把 `ISS-167` 的教训**推进了一层**。

### 现象

重跑 CR 的四条口径反向验证脚本（`tools/m3/collect_billing_wp25_cr_reverse_evidence.py`）：

```
RESULT=FAIL checks=14 PASS / 2 FAIL（共 14 条，期望 14 条，条数核对 一致）
  FAIL baseline-36-pass-0-fail
  FAIL restored-green
```

而**同一时刻被它验证的收集器是健康的**：`40 PASS / 0 FAIL / EXIT=0`。

红的形状是「条数行未命中」—— 读起来像「收集器坏了」，
**实际是尺子没跟着涨**。

### 根因

| 事实 | 数值 |
|---|---|
| 脚本生成时收集器的条数 | 36 |
| 写死在两处断言里的字面量 | `"36 PASS / 0 FAIL / 36 checks"` |
| 收口时收集器的实际条数 | **40** |
| 两次扩容 | 36 → 39（`CR-3` 用例缺陷判据化 `C34/C34b/C34c`）；39 → 40（`C35` 重复度不变量） |
| 期间同步过吗 | **没有** |
| 后果 | 该脚本自 36 时代生成过一次证据后**再没重跑** ⇒ `evidence/m3/wp25-code-review-reverse.log` **自此陈旧** |

### 它为什么比 `ISS-167` 更值得记

`ISS-167` 判给**本脚本**的修法原话是「**绝对断言 → 相对基线**」。
它当时把「副本没有基线之外的新 FAIL」改对了 —— 但**基线本身**的数值仍然提前写死。
⇒ **同一个修法没有贯彻到底**：改了一半，另一半成了新的时间炸弹。

而且这一条的**红的形状具有误导性**：前两条反向验证失败时，
输出会指名「扫描器没抓到注入的符号」（一眼看出是扫描器的问题）；
这一条失败时输出是「条数行未命中」，指向被验对象。

### 修法（已落地）

| 动作 | 内容 |
|---|---|
| 派生参照物 | 新增 `_expected_checks_from_tool()`：从**被测脚本自己的源码**读顶层 `EXPECTED_CHECKS = <int>` |
| 参照物必须唯一 | 该函数断言 `EXPECTED_CHECKS` 在文件里**只出现一次** —— 出现两处说明有人留了第二份真相，**那正是本函数要防的形态** |
| 去掉腐烂的名字 | `CHECK` 名 `baseline-36-pass-0-fail` → `baseline-all-pass-0-fail`（**名字里不再带数字**，避免名字本身腐烂） |
| 结果 | 修前 `2 FAIL / EXIT=1` → 修后 **14 PASS / 0 FAIL / EXIT=0** |

### 推进后的纪律表述

> **判据的参照物必须从被验对象派生。**
> 「参照物」和「被测对象」是两个会**独立演化**的东西；参照物写死就一定过期。
> 自查动作：凡断言里出现一个**由被验代码决定**的数字，问一句
> **「这个数字是从哪儿来的」** —— 答案若是「我写的时候它正好是 X」，
> 就必须改为从被验对象派生。

与既有纪律的关系：

| 已有纪律 | 本条的区别 |
|---|---|
| `ISS-72`（阈值长期小于实测值） | 那是**下界**失守；本条是**等式**失守，红得更早但被误读成"被验对象坏了" |
| `ISS-78`（证据元信息写死） | 那是**文案**腐烂；本条是**判据**腐烂，影响面更大 |
| `ISS-167`（绝对断言 → 相对基线） | **同一句话**，但它只改了"有没有新 FAIL"，没改"基线是多少" |
| 纪律第 7 条（改判据必须反向验证） | 本条升级为：**反向验证脚本自己也是判据，也需要被验参照物** |

---

## 汇总

| 级别 | 数量 | 编号 | 状态 |
|---|---|---|---|
| **CRITICAL** | **0** | —— | —— |
| **MAJOR** | **1** | MAJOR-1（`append_ledger_entry` 并发同键 ⇒ 500，**实测可达**） | ✅ 已修（SAVEPOINT + `23505` 回查）+ 已加判据 `[C7~C10b]` + 已反向验证 |
| **MINOR** | **2** | MINOR-1（指纹函数"声称的第二道判据"未接线） | ✅ 已修（docstring 如实标注，`[C28]` 守卫标注不消失） |
| | | MINOR-2（幂等守卫用 `assert`） | ✅ 已修（显式 `RuntimeError`，`[C2b]` 守卫） |
| **CALIBER** | **4** | 判据口径修正 ①~④ | ✅ 已修正 + 四条各自反向验证（**无连带误报**） |
| **TOOLING** | **2** | TOOLING-1（CR 收集器恒返回 0，`ISS-168`） | ✅ 已修 + 全仓门禁 `check_returns_exit_code` 防复发 + `collect_wp24_code_review.py` 同款一并修 |
| | | TOOLING-2（反向验证脚本自己的基线写死，`ISS-171`） | ✅ 已修（参照物从被验脚本源码派生）+ 14/14 反向验证绿 |
| **TESTS** | **1** | TESTS-1 / CR-3（用例把"库没跑过 WP24+ 迁移"当通过前提） | ✅ 已修（打桩制造未配置）+ 区分力反向验证 5/5 |
| **DUPL** | **1** | DUPL-1（两处 21 行逐字相同的 kwargs 构造） | 📝 **判定保留**（合并会改校验顺序），升级为同步判据 `[C35]` + 反向验证 5/5 |

### 三遍法各自的落点（本包与 WP24 的覆盖面差异）

| 遍 | 检查的手腕 | 本包特有 |
|---|---|---|
| 第一遍 正确性/安全 | 裸 `except` / `assert` / 可变默认参 / 事务泄漏 / SQL 插值 / 量化 | `[C2]` 本包 `assert` **已清零**（WP24 有 1 处正当使用） |
| 第二遍 并发/事务 | 幂等原语三要素 / 唯一冲突判定 / 三条条件 UPDATE / 无 N+1 / `ON CONFLICT` | **实测抓到一个真并发缺陷**（WP24 只有静态推断） |
| 第三遍 边界/加固 | 不可变表清单 / 部分索引 WHERE / 恒真式的反向说明 / 冲正不过滤 / 分层 | `[C35]` 把"复制保持一致"变成判据 |
| **第三遍补充（CR-3）** | —— | **换一个更接近生产的库跑同一份测试** —— 环境差异暴露出的用例缺陷，静态审查与单库运行**都看不见** |

### 测试缺口（三遍法共同暴露）

| # | 缺口 | 闭合情况 |
|---|---|---|
| 1 | 并发路径**只有串行用例**（`test_append_ledger_entry_is_idempotent_on_key` 只走快路径）⇒ MAJOR-1 藏身于此 | ✅ 已闭合（新增 8 线程并发用例 + `[C9]`/`[C10]` 结构判据） |
| 2 | 一条用例的**通过前提是"库的历史"**（没跑过 WP24+ 迁移） | ✅ 已闭合（打桩 + 区分力反向验证） |
| 3 | 没有任何用例断言"两处 kwargs 构造同步" | ✅ 已闭合（`[C35]` + 反向验证） |

> ⚠️ **本汇总表本身就是一段自由文本** —— 它**没有**机械判据与明细对账
> （项目纪律：汇总表是最容易被"事后合理化"的地方）。
> 可核对的明细一律在 `evidence/m3/wp25-code-review.log`（40 条判据，可复跑）。
> 本表**刻意不写**"文档多少行"之类的数字 —— WP24 的姊妹文档里手写的
> "593 行"在写完的**当天**就与实际（592 行）不符，那正是铁律 14 的病长在文档上。

---

## 附：与证据的对应关系

| 结论 | 机械指认 |
|---|---|
| 12 文件无裸 except / assert / 可变默认参 / 事务泄漏 / SQL 插值 | `[C1]`~`[C6]` |
| MAJOR-1 的**实现形态**（SAVEPOINT + `23505` + 回查） | `[C7]`~`[C10b]` |
| 三条条件 UPDATE / 无 N+1 / `ON CONFLICT` | `[C11]`~`[C16]` |
| 不可变表 / 部分索引 / 恒真式说明 / 冲正 / 取价优先级 | `[C17]`~`[C22]` |
| 校验顺序 / 结算守恒 / 自动补 BILL / 14 条路由 | `[C23]`~`[C27]` |
| MINOR-1（未接线标注）+ **反向确证** | `[C28]` / `[C28 反向]` |
| 测试规模 / 四层结构 / 夹具口径 / 迁移拓扑 | `[C29]`~`[C32]` |
| TOOLING-1（本脚本自查退出码） | `[C33]` |
| CR-3（打桩 + 根因坐实 + 区分力） | `[C34]` / `[C34b]` / `[C34c]` |
| DUPL-1（两处 kwargs 同步） | `[C35]` |

**反向验证记录**（本文档所有"✅ 已反向验证"的出处）：

| 验证 | 脚本 | 结果 |
|---|---|---|
| MAJOR-1（退回裸插入 ⇒ 并发用例红） | 内联于 `wp25-code-review.log` 的"反向验证记录"节 | 3/8 抛 `23505` ⇒ 红 |
| CALIBER ①~④（四条退化各自红、无连带） | `/tmp/wp25_cr_caliber_reverse.log` | 4/4 阶段通过 |
| TESTS-1（退化实现兜底默认值 ⇒ 用例红） | `/tmp/wp25/reverse_limits.py` | 5/5 PASS |
| DUPL-1（只改一处 kwarg 顺序 ⇒ `[C35]` 红） | `/tmp/wp25/reverse_c35.py` + `wp25-simplify.log` 第五节 | 5/5 PASS |
| 阶段 7 扫描器本身（注入零引用符号 ⇒ 报红） | `/tmp/wp25/reverse_scan.py` + `wp25-simplify.log` 第五节 | 3/3 PASS |
| TOOLING-1（门禁能抓到缺退出码） | `check_evidence_scripts.py::check_returns_exit_code` | 已由门禁的 `gate-detects-missing-exit-code` 用例覆盖 |