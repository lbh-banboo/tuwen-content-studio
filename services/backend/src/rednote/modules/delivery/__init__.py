"""KOC 记录 / 交付与替换 / 发布草稿与版本映射（WP36）—— M4「数据链」的起点包。

把 WP35 冻结出来的「某一组内容的某一版 ZIP」变成**可追溯的商业事实**：
谁（KOC）在什么时候拿到了哪一篇的哪一版（`deliveries`），后来发没发出来、
发成了哪条笔记（`publications`）；并且让这两件事在**改稿、重交付、重复提交**
面前都不会被改写。

## 表（4 张）

| 表 | 可变性 | 说明 |
|---|---|---|
| `koc_records` | 可变 | KOC 的轻量记录。**KOC 不是系统用户**；`contact_ref` 是引用，不存任何凭据 |
| `deliveries` | **列级**可变 | 状态机（`PREPARED → DELIVERED / CANCELLED`）之外的列由 `trg_deliveries_frozen_columns` 冻结；冻结列名单**由磁盘派生**，不由人手写 |
| `publications` | 可变 | 「我方交付的那一版 ↔ KOC 实际发出的那条笔记」的登记。本包只**登记** |
| `publication_links` | 可变（只追加） | **本包只建表**；规范化与去重判定归 WP37 |

## ⚠️ 本仓有三个「publication」，语义互不相干

| 表 | 属于 | 「发布」指什么 |
|---|---|---|
| `knowledge_publications` | WP20 | 把某个**知识资产版本**发布上线（内部资产生效） |
| `prompt_publications` | WP23 | 把某个**提示词版本**发布上线（内部资产生效） |
| **`publications`（本包）** | WP36 | **KOC 在小红书上实际发出的那条笔记**的登记（外部事实） |

裸名 `publications` 是契约草案与 DDL 草案已冻结的名字，不改。ORM 类名
`Publication` 只在本模块出现；平级模块本来就不可互 import。

## 两条产品规则怎么落地

| 规则 | 落在哪 |
|---|---|
| **下载不计交付** | ① 结构：`delivered_at` 只在本包表上，下载端点属 `exports` 且全程只读；② DB：`ck_deliveries_state_timestamps` 双向等式；③ 服务层：`delivered_at` 的赋值**全包仅 1 处**（`repository.mark_delivered`），没有通用 UPDATE 入口；④ 机械判据：`tools/quality/check_download_not_delivery.py`（AST，不是文本 grep） |
| **原交付版本不随改稿变化** | ① **复制**而非引用（三个冻结字段从 `export_items` 逐列复制）；② 列级冻结触发器（`IS DISTINCT FROM`，NULL 安全）；③ 冻结列名单由 `models.frozen_delivery_columns()` 派生 + 逐列变异断言；④ 替换只 INSERT，对旧行零 UPDATE |

## 分层（单向）

```
api.py          →  11 个端点（项目范围集合 + 名词化子资源，零动词）
  ↓
service.py      →  七步编排（校验 → 幂等 → 项目 → 导出行 → KOC → 复制落行 → flush）
  ↓
schemas.py      →  纯函数：入参校验 / 状态机合法迁移表
  ↓
repository.py   →  本包 4 表 + 跨模块只读裸 SQL（projects / content_items / export_items 投影）
export_bridge.py→  `exports` 的唯一出口：裸 SQL 读导出行 + 转发资格复验只读接缝
  ↓
models.py / ddl.py / types.py
```

## 装配（主 agent 串行动作，本包不碰）

- `migrations/env.py` 须 import 本包 `models`（建表注册）。
- `main.py` 注册本包 `router`。
- `migrations/versions/` 的迁移由主 agent 建（取值域写**字面量**，ISS-191）。
- `tests/test_content_wp26.py` 的 `_KNOWN_CROSS_PACKAGE_FKS` 须 +1
  （`fk_deliveries_content_item_id_content_items`，RG-130 第 4 次事件）。

## 本包刻意不做

URL 规范化 / 笔记去重 / `DUPLICATE_PUBLICATION` 的去重判定 / 发布可达性抓取
与验证（**WP37**）；任何指标、快照、阈值标签（**WP38**）；导出资格失效的影响图
遍历与传播（**WP34**）；任何打包代码（WP35 已做完）；批量交付端点；
「下载即交付」的自动推断与任何定时任务（那是产品规则的反面）。

**本包零切段实现、零"当前版本"读法实现、零资格哈希实现** ——
三者分别在 `content` / `exports` 侧，各只有一份。
"""

from __future__ import annotations

from rednote.modules.delivery import models  # noqa: F401  （建表注册，必须 import）
from rednote.modules.delivery.api import router  # noqa: F401  （端点注册）

__all__: list[str] = ["router"]
