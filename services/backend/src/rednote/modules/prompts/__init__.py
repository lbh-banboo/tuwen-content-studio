"""Prompt 版本与工具权限（WP23）。

**对外接口**：

| 用途 | 位置 |
|---|---|
| 端点 | `api.router`（3 个协议端点 + 3 个本包补充端点） |
| 业务规则 | `service`（**唯一对外入口**） |
| 可渲染校验（纯函数） | `render.check_renderable` |
| 工具权限上界（纯函数） | `tools_policy.validate_allowed_tools` |
| 数据库级保证 | `ddl.all_trigger_ddl` / `ddl.trigger_names` |

**三张表**（数据结构 02 §8）：

| 表 | 性质 |
|---|---|
| `prompt_templates` | 模板**身份**，可变（`active_version_id` 是**投影列**） |
| `prompt_versions` | 模板版本，**不可变**（`id` 同时 FK `version_nodes.id`） |
| `prompt_publications` | 激活事件，**不可变**（协议"激活，记录影响范围"的落点） |

**分层**（单向）：

    types ← render
    types ← tools_policy
    types ← models → platform.immutability
    types ← repository → models
    types ← service → repository / render / tools_policy / versions（WP11 接缝）
    api → service / dependencies

显式 import 模型与 router，使 `migrations/env.py` 的 autogenerate
与 `main.py` 的装配都能看见它们。

---

## 本包的四条硬约束（违反即返工）

1. **工具集不在编辑路径上** —— 协议 §9 第 12 行的入参没有 `allowedTools`
   （同一张表连 `cursor,limit` 都逐个列了），与 §424「允许工具列表
   **由平台配置管理**」严丝合缝。编辑路径（`create_template` /
   `add_version`）**不接收**工具集，新版本**继承**上一版；
   改工具集只能走 `service.grant_tools`（平台侧）。
   **"校验通过"不等于"权限没被扩大"**：真正的越权是编辑者拿到了
   本不该由他决定的那个决定权。

2. **`allowed_tools` 的上界是闭集，且落成数据库 CHECK** ——
   `allowed_tools <@ ARRAY[...]::text[]`（`<@` = "被包含于"）。
   **判非空是假防线**（恒真地放行任何输入），子集关系**有反例**
   （`ROOT_SHELL` 不在上界内 → 必须被拒），因此不可能恒真。
   与 WP19 的 ISS-75（判据被 CHECK 保证恒真 → 防线静默失效）同源。

3. **`task_kind` 的取值必须与 WP19 对齐** —— WP19 的
   `FACT_VERIFICATION` / `EVIDENCE_COLLECTION` 已经出现在已交付的
   API 响应里。本包初稿曾另起一套拼写（`RESEARCH_EVIDENCE`），
   后果是同一任务类型在库里出现两种写法且**不报错**。
   现在有一条跨包断言（`research.TASK_KINDS ⊆ prompts.TASK_KINDS`）
   把"对齐"从约定变成机械判据。

4. **OP-88 的 `PROMPT` 那一半在本包闭合** ——
   `ddl.py` 的正向（子表 → 节点）与反向（节点 → 子表）两个延迟触发器，
   反向那个挂在**共用表 `version_nodes`** 上并用
   `WHEN (entity_type = 'PROMPT')` 收窄（与 WP18 的 `BRIEF`、
   WP20 的 `KNOWLEDGE` 两个并存，互不干扰）。

## 接缝调用不得改签名

`versions.service.register_version(entity_type="PROMPT", …)`、
`versions.repository.VersionRepository`、
`projects.dependencies.require_org_action` —— 全部**原样调用**。
"""

from rednote.modules.prompts import models  # noqa: F401
from rednote.modules.prompts.api import router  # noqa: F401

__all__: list[str] = ["router"]
