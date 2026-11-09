"""场景形式草稿与数量配置（WP24）—— M3「生产」的第一个包。

## 本模块解决什么

把"这一批生成多少条、每一类各多少条"从**运营脑子里的约定**变成
**可复现、可审计、可配置的整数**。三块拼图：

| 拼图 | 落点 | 表 |
|---|---|---|
| 数量上下限从 DDL 移出 | `runtime_settings` + ADR-13 审计 | `runtime_settings`（追加式不可变） |
| 比例用千分位整数 | `permille.py`（纯函数） | 草稿列 `scene_items` / `form_items`（JSONB） |
| 整数分配和不漂移 | `allocation.py`（纯函数，最大余数法） | — |

## 表清单（2 张）

| 表 | 可变性 | 说明 |
|---|---|---|
| `runtime_settings` | **不可变**（触发器拦 UPDATE/DELETE） | 配置的历史；每次变更插一行；当前生效值 = 同 `(key, scope, project)` 下 `revision_no` 最大那行 |
| `plan_drafts` | 可变（`lock_version`） | 步骤 4 的工作区，一项目一份 |

**`batches` 表刻意不建**（§1.5）：真正的消费者是 WP31/WP32，
本包的产出是**方案（PLAN）版本**，不是批次。建了不用就是死表。

## 分层（单向，不反向依赖）

```
api.py             →  端点（8 个；含 GET /settings/projects/diverged）
  ↓
schema.py          →  请求/响应 DTO（16 个；EffectiveSetting 含 source/global_value/diverged）
settings_service.py→  配置读写 + 审计 + ★ 全局变更时同事务级联刷新副本（唯一写入口）
service.py         →  草稿读写 + 固化出版本
  ↓
allocation.py / permille.py / limits.py   →  纯函数（不读库、不写库，可穷举验证）
  ↓
repository.py      →  存储原语（无 runtime_settings 的 update/delete）
  ↓
models.py / ddl.py / types.py
```

## ★ OP-60：项目级副本机制（老大拍板"支持且必须有项目级副本"）

这是本包**唯一**一处"用户选择了一条助手不推荐的路"，
因此它必须由机制兜住，而不能靠人记得：

| 机制 | 落点 | 判据 |
|---|---|---|
| 同步（全局变更同事务级联刷新所有未覆盖副本） | `settings_service.change_global_setting` | J2 |
| 分叉显式化（四条一起返回） | `settings_service.get_effective_limits` + API | J3 |
| 副本首次产生不跨包（惰性物化） | `settings_service._materialize_inherited_copy` | J1 |
| 覆盖稳定（`MANUAL` 不跟随） | `settings_service.change_project_setting` | J4 |

**写 `runtime_settings` 的路径只有两条**（§2.6 约束第 4 条）：
`change_global_setting` 与 `change_project_setting`。
**副本的插入是这两条路径与惰性物化的内部行为**，不是第三个公开入口 ——
否则"副本跟随全局"就有了绕过口，而 J2 只能测到"走服务层时成立"。

## 并发：按 key 的 advisory lock，**不用 `ON CONFLICT DO NOTHING`**

`repository.lock_setting_key` 取 `pg_advisory_xact_lock`（事务级、自动释放）。
没有它，两个并发全局变更会算得同一个下一修订号，
后者被部分唯一索引拒绝 —— 若用 `ON CONFLICT DO NOTHING` 就会**静默丢写**，
结果"全局 V3、某项目副本 V2"，两值都在、都不报错。**正是 J2 要拦的形态。**

⚠️ 文档 §3.6.3 的惰性物化伪码仍写着 `ON CONFLICT DO NOTHING`，
与实际实现（advisory lock + 普通 INSERT + 重读）分歧 —— 收口必须回填文档。

## 装配

- `migrations/env.py` 补一行 `from rednote.modules.planning import models`
- `main.py` 末尾区注册本模块 `router`
- 迁移 `down_revision = "185ec01bed91"`（WP22 是当前文件链头）

## 已知取舍（均登记收口事项）

| 取舍 | 理由 |
|---|---|
| `GET .../plan-draft` 会写库（懒创建草稿） | 与 WP18 `GET /brief` / WP22 `GET .../information-draft` 同款；修法是主 agent 在项目创建事务里补调用 |
| `submit_draft` 的 `reason` 无下游接收点 | `register_version` 签名（WP11 接缝）没有 `reason` 参数，单方面改它是跨包改动 |
| `GET .../diverged` 不分页 | 项目数上限几十；破百再改 |
| 配置写端点**不要求** `If-Match` | 追加式表没有 `lock_version`；并发由 advisory lock 保证（比 ETag 更强） |
| `GET .../diverged` **不校验未知键**（返回空清单），而姊妹端点 `.../history` **返回 422** | 两处都是"读一个未知键"，却给出不同的可观察行为。修法有两种（前者改 422 或后者改空列表），都是**契约层变更**（改变前端可观察行为），由协议侧裁定。已由第 4 层用例 `test_diverged_projects_does_not_reject_unknown_key` 钉住现状 |
| `PUT /settings/global/{key}` 的权限**无法测 403** | `_ORG_MATRIX` 里 `KNOWLEDGE_MANAGE` 对 `ROLE_ADMIN` 与 `ROLE_OPERATOR` 都开放（运营唯一开放的组织级操作 —— 本包正是因此选它而非 `MANAGE_DICTIONARY`）。本 environment 造不出"已登录但无权限"的用户 ⇒ 该端点只能测 401。若某天矩阵收回运营的该权限，需**新增** 403 用例 |

## ★ 阶段 6 实测发现的一处**端到端可用性缺陷**（已修，需登记 ISS）

`api.py::save_plan_draft` 第一版把 pydantic 模型转 dict 时写的是
`item.model_dump(exclude_none=True)` / `item.model_dump()` ——
**漏了 `by_alias=True`**。

`DimensionItemBody` / `PairingBody` 用 alias 声明字段名
（`weightPermille` / `sortOrder` / `sceneKey` / `formKey`），
而 pydantic 的 `model_dump()` **默认 `by_alias=False`**，
于是它吐的是 **snake_case**。服务层白名单恰恰是 camelCase
（`service._ALLOWED_ITEM_KEYS` / `_validate_pairings` 的 `sceneKey`），
于是**整条保存/提交路径 422**：

    sceneItems[0] 含未知字段：['sort_order', 'weight_permille']
    （合法：['capacity', 'key', 'name', 'sortOrder', 'state', 'weightPermille']）

**为什么前三层全绿也拦不住它**：第 1/2 层直接调服务层函数（模型对象
根本没机会经过 `model_dump`），第 3 层测数据库约束 —— 三者都不经过
HTTP 边界。触发条件是"模型对象 → dict"的那一步，**只在第 4 层显形**。

**修法**：三处 `model_dump` 全部显式 `by_alias=True`（已改，并加注释说明
这条约束）。**教训**：凡 DTO 字段名与响应键不同（alias），
`model_dump` 必须显式给 `by_alias`，不要依赖默认值。

## 其他地方没有的东西（避免"重复实现"）

- **本包不使用 `runtime_settings` 做缓存**：每次读都查库（R4 的处置 ——
  API 双实例下进程内缓存必然跨实例失效）。
- **本包不建 `batches` 表**：`TRIAL/SCALE/BULK` 的上限是**配置**（ADR-13），
  数量本身由 M3 后续包的产生方记录，本包只提供关口函数
  （`limits.validate_quantity`）与配置读写。
"""

from __future__ import annotations

from rednote.modules.planning import models  # noqa: F401  （建表注册，必须 import）
from rednote.modules.planning.api import router  # noqa: F401  （端点注册）

__all__: list[str] = ["router"]
