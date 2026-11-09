"""静态图适配与产品参考素材（WP27）—— M3「生产」的第四个包。

## 本模块解决什么

把"给一篇已经写好的文案配图"从**一次不透明的出图**，
拆成一组**可校验、可绑定产品素材、可单张重做、可加工、可对账**的结构化产物。

| 拼图 | 落点 | 表 |
|---|---|---|
| 图片入库契约（**R2 的地基**） | `service.measure_image`（纯函数，四道判据） | — |
| 图片**身份**（第 N 张，跨版本不变） | `service.start_media_generation` | `media_items`（**可变**） |
| 图片**版本**（字节 + 段落绑定 + 素材绑定） | `service._persist_generation` | `media_versions`（**不可变**） |
| **产品参考素材**（型号 / 颜色 / 部件） | `service.register_reference_asset` | `reference_assets`（**不可变**） |
| **加工产物**（预览 / 正式） | `service.start_rendition` | `media_renditions`（**不可变**） |
| **生成上下文**（**OP-160 的同构落点**） | `service._persist_generation` 每次生成落一行 | `media_generation_contexts`（**不可变**） |
| 段落对位规则（**R4 落点**） | `schemas.assign_paragraph_refs`（纯函数，**顺序对位**） | — |
| 计费接线（估 → 占 → 派发 → 结算） | `billing_bridge.py`（WP25 接缝） | — |
| 网关接线（本地导入，R7） | `gateway_bridge.py`（WP16 接缝） | — |
| 提示词接线（R6：先解析后预占） | `prompts_bridge.py`（WP23 接缝） | — |
| 任务框架适配 | `handlers.py`（WP13 的 `TaskHandlerRegistry` 注册点） | — |

## 表清单（5 张）

| 表 | 可变性 | 说明 |
|---|---|---|
| `media_items` | **可变**（`lock_version` + 当前有效版本投影） | 图片**身份**（一条内容里的第 N 张图，跨版本不变）；另存 `generation_request` / `pending_reference_bindings` / `pending_regeneration_reason` |
| `media_versions` | **不可变** | 图片**版本**（文件对象 + 段落绑定 + 参考素材绑定）。**`id` 同时 FK `version_nodes.id`** |
| `reference_assets` | **不可变** | **产品参考素材**（`PURPOSE_REFERENCE` 的落点）：型号 / 颜色 / 部件标注 |
| `media_renditions` | **不可变** | **加工产物**（预览 / 正式；尺寸 / 格式 / 体积） |
| `media_generation_contexts` | **不可变** | 生成上下文（**OP-160 同构落点**） |

**为什么 `media_versions.id` 同时是 `version_nodes.id`**：与 WP26 的
`content_versions` 同款 —— 版本节点表是**跨模块的统一版本索引**，
图片版本不挂上去，`versions` 侧的"这条内容一共有几版"就只数得到文案版。
代价是两张表**共享主键**（不是 FK 到另一个序列）：插版本行前必须
先 `register_version`。

## 分层（单向，不反向依赖）

```
api.py             →  端点（7 个；协议原件不在本机 ⇒ 全部由本包定义，§2.4）
  ↓
handlers.py        →  任务框架适配（只调 service；失败折算的**唯一**落点）
  ↓
schemas.py         →  ★ 纯函数校验（零 I/O；R1/R4 落点，反例驱动）
  ↓
service.py         →  唯一业务入口（编排：解析 → 校验 → 预估 → 预占 → 派发 → 入库 → 结算）
  ↓
repository.py      →  存储原语（条件 UPDATE、投影推进、幂等域）
  ↓
models.py / ddl.py / types.py
       ↑            ↑            ↑
billing_bridge.py  gateway_bridge.py  prompts_bridge.py   （三条上游接缝）
```

`api.py` 只做协议（HTTP 形状 / 权限 / 分页），`handlers.py` 只做任务框架适配 ——
**两者都不含业务判断**。把判断放在那两处会让"同一规则两条路径"，
而分叉的表现通常是**静默**的。

## ★ 本包的四条"定死口径"

1. **固定 3 张**（`types.IMAGES_PER_CONTENT = 3`，老大 2026-10-07 拍板）：
   `images_per_content()` 是**全包唯一读点**，`images_for()` 是**全包唯一算法**。
   这是一处**与原型显式不一致**的偏差（原型下拉是 6/4/9 可配置），
   已登记为 OP-198。保留四条路径的切换点 —— 将来要改成可配置，改**这一个函数**即可。
2. **身份是"第 N 张图"而不是"第 N 段"**：稳定身份键是
   `(content_item_id, sort_order)`（唯一键 `uq_media_items_content_sort`），
   而"这一张配的是哪一段"是**版本级**事实（`media_versions.paragraph_ref`）。
   以段落为键时"正文只有 2 段"就出不了 3 张图 —— 与"固定 3 张"直接矛盾。
3. **参考素材绑定是**一列 JSONB（`reference_bindings`），**不是** FK 列、
   **不是**第 6 张表。`types.MIN/MAX_REFERENCE_ASSETS = 1/8` 定死它是
   **1~8 条的列表**；外加"主素材 FK"会违反铁律 26（同一个量两个来源）。
   放弃 FK 的代价补在两处：CHECK（形状 + 长度）+ `ddl.py` 一致性触发器（逐元素校验存在性与归属）。
4. **预占在执行阶段**（不是发起阶段）：发起与执行是两次事务，
   `POST .../media-generations` 只建身份行 + 入队；钱在 Worker 里
   `execute_media_generation` 那次事务预占，且**严格早于**派发（R2）。
   图片按**张**计费 ⇒ "提示词没配"这类部署问题若在预占之后才暴露，
   每次都要**占住 3 张的额度**再释放。

## 装配

- `migrations/env.py` 已有 `from rednote.modules.media import models`（断点前已就位）
- `main.py` 注册本模块 `router`（7 条路径）
- ⚠️ **任务处理器（`build_task_registry()`）刻意不在 `main.py` 合并**：
  全仓实测，`run_job` / `claim_jobs` / `build_default_registry` **只在测试里
  被消费**，生产侧**没有 worker 入口**（WP13 的既有状态）。
  因此本包只交付**可被装配的入口**，不在 `app.state` 上挂一个零消费者的
  注册表（那是 ISS-95 形态）。合并口径写在 `handlers.py` 的 docstring 里。

## 已知取舍（均登记收口事项）

| 取舍 | 理由 |
|---|---|
| 端点是**本项目补充**（协议原件不在本机） | 与 WP24 / WP25 / WP26 同一先例；7 条路径逐条登记在说明文档 §2.4 |
| **`media_items` 有 `paragraph_ref` 但不落唯一约束** | 它是**当前版本的投影**，不是身份的一部分（两条身份可以指向同一段） |
| **"3 张如何分配到 N 段"曾未拍板**，本包曾拒绝发明 | 老大 2026-10-08 拍板**顺序对位**后，规则落在 `schemas.assign_paragraph_refs`（第 i 张配第 i 段；段落数不足 3 时多余张回落最后一段） |
| **`start_rendition` 只建 PENDING 行，不做真加工** | 全仓没有任何加工 worker（FFmpeg / Pillow 转码调用点一处都没有）⇒ 本包交付**登记能力**，加工接线登记待定项 |
| **`media_items.job_id` 反查**（不往 `jobs` 加列） | `ClaimedJob` **无自由参数位**，`jobs.content_id` 只装一个 UUID，而生成任务必须同时知道 `media_item_id` 与 `content_id` ⇒ 落 `media_items.job_id`（唯一约束 `uq_media_items_job_id`）+ 处理器 `find_media_item_for_job` 反查。**接缝冻结**：不改 `jobs` 表。与 WP17 `parsing` 的 `find_run_for_job` 逐字同构 |
| 真实模型端到端**不在本包** | 本机无已审批供应商 / 无 `model_profiles` 行（OP-130 未关闭）；交付**接线**，真实验证登记内网补做 |
"""

from __future__ import annotations

from rednote.modules.media import models  # noqa: F401  （建表注册，必须 import）
from rednote.modules.media.api import router  # noqa: F401  （端点注册）

__all__: list[str] = ["router"]
