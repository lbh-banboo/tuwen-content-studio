"""文案结构与文案生成、标题候选与证据绑定（WP26）—— M3「生产」的第三个包。

## 本模块解决什么

把"让模型写一篇小红书文案"从**一次不透明的文本生成**，
拆成一组**可校验、可绑定证据、可局部重做、可对账**的结构化产物。

| 拼图 | 落点 | 表 |
|---|---|---|
| 结构化输出契约（**R1 的地基**） | `schemas.py`（纯函数，逐字段 + 反例驱动） | — |
| 叙事蓝图（9 字段结构） | `service.execute_generation`（`TASK_CONTENT_STRUCTURE`） | `narrative_blueprints`（**不可变**） |
| 正文 / 标题 / 标签（3 件） | `service.execute_generation`（`TASK_CONTENT_COPY`） | `content_versions`（**不可变**） |
| 标题候选 + 6 维评分 + 采纳 | `service.adopt_title_candidate` | `title_candidates`（**不可变**） |
| **证据绑定**（**AQ-01 的地基**） | `types.ParagraphBinding` + `schemas.validate_bindings` | `content_versions.bindings`（JSONB） |
| **生成上下文**（**OP-160 落点**） | `service.execute_generation` 每次生成落一行 | `generation_contexts`（**不可变**） |
| 计费接线（估 → 占 → 派发 → 结算） | `billing_bridge.py`（WP25 接缝） | — |
| 网关接线（本地导入，R7） | `gateway_bridge.py`（WP16 接缝） | — |
| 任务框架适配 | `handlers.py`（WP13 的 `TaskHandlerRegistry` 注册点） | — |

## 表清单（5 张）

| 表 | 可变性 | 说明 |
|---|---|---|
| `content_items` | **可变**（`lock_version` + 当前版本投影） | 内容**身份**（一项目一条内容，跨版本不变）；另存 `generation_request`（见下） |
| `content_versions` | **不可变** | 内容**版本**（标题 / 正文 / 标签 / 采纳的标题候选 / 段级证据绑定） |
| `narrative_blueprints` | **不可变** | **叙事蓝图**（9 字段结构），生成正文的输入 |
| `title_candidates` | **不可变** | 标题候选（每批 N 个 + 6 维评分 + 关键词/吸引点/风险说明） |
| `generation_contexts` | **不可变** | 生成上下文（**OP-160 落点**） |

**为什么蓝图与内容版本分两张表**：合成一张（蓝图塞 JSONB）会让"仅标题重做"
必须整车重写一版（蓝图是不可变的历史），且蓝图无法独立审阅、字段漂移无人发现。
与 `brief_claims`（WP18，事实 claim 独立成表）**同构**。

**为什么"采纳标题"记在 `content_versions` 上而不是给候选加 `adopted` 列**：
给候选加列会让它变成**可变表**，不可变触发器就得开一个"只许改某一列"的例外 ——
而写错的后果是候选历史可以被整行改写（一张"看起来不可变、实际有后门"的表）。
采纳落成"追加一版"（`models.py` docstring 六），于是候选表纯不可变。

## 分层（单向，不反向依赖）

```
api.py             →  端点（6 个；协议原件不在本机 ⇒ 全部由本包定义，§2.4-5）
  ↓
schemas.py         →  ★ 纯函数校验（零 I/O；R1 落点，反例驱动）
  ↓
service.py         →  唯一业务入口（编排：解析 → 预估 → 预占 → 派发 → 校验 → 落库 → 结算）
  ↓
repository.py      →  存储原语（条件 UPDATE、投影推进、幂等域）
  ↓
models.py / ddl.py / types.py
       ↑                    ↑
billing_bridge.py    gateway_bridge.py    prompts_bridge.py   （三条上游接缝）
handlers.py          （任务框架适配，调 service）
```

`api.py` 只做协议（HTTP 形状 / 权限 / 分页），`handlers.py` 只做任务框架适配 ——
**两者都不含业务判断**。把判断放在那两处会让"同一规则两条路径"，
而分叉的表现通常是**静默**的。

## ★ 本包的三条"定死口径"

1. **预占在执行阶段**（不是发起阶段）：发起与执行是两次事务，
   `POST .../content-generations` 只建身份行 + 入队；钱在 Worker 里的
   `execute_generation` 那次事务预占，且**严格早于**派发（R2）。
   理由逐条见 `service.py` 模块 docstring。
2. **`estimate_cost` 的 `quantity` 是"调用次数"**（经 `types.calls_for`），
   不是"内容条数" —— 一次"文案生成"其实是 2 次模型调用（结构 + 文案，R3）。
3. **"生成参数"落在 `content_items.generation_request`**（JSONB，可空）：
   从发起到执行之间没有任何既有传输通道（逐条实测见 `types.py` §「★ 一次生成请求的载荷」），
   本包借用 WP18 `brief_drafts.enrichment_request` 的同款处置 ——
   **不修改 `jobs`、不新建"只为传一次参数"的表**。

## 装配

- `migrations/env.py` 已有 `from rednote.modules.content import models`（断点前已就位）
- `main.py` 末尾区注册本模块 `router`
- 迁移 `down_revision = "daea2b27212c"`（WP25 是当前文件链头）

## 已知取舍（均登记收口事项）

| 取舍 | 理由 |
|---|---|
| 端点是**本项目补充**（协议 03 原件不在本机） | §1.4 第 1 条 + §2.4-5；已登记为 OP-162 同族回填项 |
| 蓝图**不挂** `version_nodes` | 挂上去会让同一内容身份下出现两条互相干扰的 `revision_no` 序列（`models.py` docstring 三，已登记偏离） |
| `content_versions.content_item_id` **刻意留 IMMEDIATE** | 延迟可空侧是为了允许"先建身份后建版本"；延迟非空侧只会额外放行孤儿版本行 |
| `TAGS` 范围**归一为** `COPY` | WP23 只定义了两个 `task_kind`，标签随正文一起重做（`types.normalize_scope`） |
| 真实模型端到端**不在本包** | 本机无已审批供应商 / 无 `model_profiles` 行（OP-130 未关闭）；交付**接线**，真实验证登记内网补做 |
"""

from __future__ import annotations

from rednote.modules.content import models  # noqa: F401  （建表注册，必须 import）
from rednote.modules.content.api import router  # noqa: F401  （端点注册）

__all__: list[str] = ["router", "read_edit_target", "content_edit_etag", "append_edited_version"]