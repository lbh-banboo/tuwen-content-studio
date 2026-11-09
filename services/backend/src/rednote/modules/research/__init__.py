"""研究关键词与提示词、资料列表、覆盖缺口确认（WP19）。

**对外接口**：

| 用途 | 位置 |
|---|---|
| 端点 | `api.router`（两个 GET，协议 03 §5 的第 6、7 行） |
| 派生关键词 | `keywords.derive_keywords`（**纯函数**） |
| 派生覆盖缺口 | `keywords.derive_gaps` + `keywords.build_gap_keywords`（**纯函数**） |
| 组装提示词 | `keywords.build_prompts`（**纯函数**） |
| 装配（读库 + 组装） | `service.build_research_prompts` / `service.list_research_files` |
| 只读查询 | `repository.ResearchRepository` |

**本包零表、零迁移、零写端点。** 它需要的每一张表都已存在且有主人：
Brief 在 WP18（`brief_versions` / `brief_claims`）、文件在 WP12
（`project_file_links` 的 `purpose=RESEARCH`）、解析在 WP17
（`parse_runs` / `source_segments`）、配额在 WP12（`quota_accounts`）。

**为什么零表是对的而不是"做得太少"**（划界判据见说明文档 §1.7）：

  · 关键词与缺口是**派生物**，可由 (Brief 版本, 规则版本) 重算。
    落库会让"系统推荐的是什么"与"表里现在是什么"分叉，
    且 Brief 出新版本后表里的词**没有任何机制**跟着更新；
  · "研究文件 ↔ Brief 字段"的绑定需要**人工确认**，而确认发生在
    WP22 的审核工作区 —— 本包预先固化它的形状会让 WP22 要么
    复用一张字段名对不上的表、要么再建一张（重复）；
  · 语义检索需要 embedding，那是 WP21 的整包内容。

**分层**（单向）：

    types ← keywords → brief.schema / brief.types（只读常量）
    types ← repository → models（只读）
    types ← service → repository / keywords
    api → service / dependencies

**本模块刻意不 `import` 任何 `models`**：本包没有自己的模型，
`migrations/env.py` 的 autogenerate 不需要通过本包发现表
（本包**不产生迁移**）。这与其余包的 `__init__.py` 不同 ——
那里 import models 是为了"让 autogenerate 看见这 N 张表"，
本包没有表可让它看见。
"""

from rednote.modules.research.api import router  # noqa: F401

__all__: list[str] = ["router"]