#!/usr/bin/env python
"""G1 演示种子数据（**只允许写 rednote_dev**）。

## 它解决什么问题

G1 的出口判据是"**用 Demo 页面展示后端真实数据**"。协议已冻结 60 个操作，
但没有任何消费者 —— 页面全是硬编码假数据。本脚本往库里放一套
**自洽的**演示数据，让页面接上后端后能显示真东西。

## 三条设计约束（每条都有理由，不是风格偏好）

| 约束 | 为什么 |
|---|---|
| **库名白名单**（只允许 `rednote_dev`） | 本仓的 `postgres` 库被测试反复写入，projects 已在 **4 万量级**（全叫"测试项目"）、brands 在 **5 万量级**，且每次跑测试还在涨。种子脚本一旦跑错库，就是往脏库里再灌一层脏数据 —— 而脏数据**不会报错**，只会在几天后让某人的查询结果莫名其妙 |
| **幂等**（固定 UUID + `ON CONFLICT DO NOTHING`） | 演示前重跑是常态。非幂等的种子脚本第二次就把库灌成两套数据，页面出现"重复项目"，排查方向会歪到代码上 |
| **取值来自文档而非发明** | 见下方"取值来源" |

> 那两处脏库计数**刻意只写量级**，不写精确值：它们随每次跑测试变化，
> 写死了必然过期，而文档过期是**不报错的**（本仓已因这类"写死计数器"
> 踩过多次）。量级足以支撑"别往这里灌数据"这个判断，精确值无额外价值。

## 取值来源（**没有一个值是凭空造的**）

| 数据 | 来源 |
|---|---|
| 用户名/品牌/产品/项目名/文件名 | `services/backend/static/*.html` 里已有的硬编码演示数据（`projects.html` / `research-files.html` / `login.html`） |
| 项目编号格式 | `platform/codes.py::generate_display_code` → `PRJ-{YYYYMM}-{6 位 Crockford Base32}` |
| 阶段 / 成员角色 / 出网策略 / 配额用途 / 扫描状态 … | 各 `models.py` 的 `Final[str]` 常量与 CHECK 约束 |
| `structured_fields`（20 字段 / 10 组） | `modules/brief/schema.py::BRIEF_FIELDS`；每个值是 `{"status", "value"}`（同文件 41~51 行） |
| Brief 草稿的 claims / issues / suggestions | `modules/brief/types.py` 的 `ClaimDraft.to_json` / `IssueDraft.to_json` / `EnrichmentSuggestion.to_json` |
| `information_drafts` 五块内容 | `modules/baseline/models.py` 的列注释与 `modules/baseline/readiness.py` 的三条完成条件 |
| 基线 snapshot / baseline_items / step_approvals | `modules/baseline/service.py::_baseline_snapshot`、`models.py`、`baseline_types.py` 的受控取值域 |
| plan-draft 的场景 / 形式 / 配对 | `modules/planning/models.py` 的 `PlanDraft` 列注释；`service.py` 的 `_validate_dimension` / `_validate_pairings`；提交四项见 `service.py::submit_draft` |
| pricing_versions / cost_estimates / project_budgets | `modules/billing/models.py` 的列与 CHECK；响应形状见 `billing/schema.py` |
| reference_assets | `modules/media/models.py::ReferenceAsset`（AQ-03 的型号/颜色/部件标注） |
| `parse_runs.state` 与计数的一致性 | `parsing/types.py::derive_state`（**直接调用它做自检**，见 `_verify`） |
| `work_items.kind` | PRD §21.1 的"7 类通知事件"（`tasks.html` 副标题逐条列出）。**本仓 `work_items.kind` 没有 CHECK**，取值域属后续模块 —— 这些名字在 M3 落地前**不构成契约** |

**注意展示编号与原型的差异**：原型里写的是 `PRJ-2026-0037` 这种
"年份 + 流水号"，而真实格式是 `PRJ-202609-XXXXXX`。种子按**真实格式**生成，
因此接入后页面显示的编号会与原型截图不同 —— 这是预期结果，不是 bug。

## 自检（`_verify`，退出码非零即失败）

1. **状态与计数一致**：`parse_runs` 的状态必须与计数自洽。
   **判据分两类**，因为生产的 `derive_state` 只推导**终态**：
   - 终态 → 必须等于 `derive_state(...)` 的返回值（**调用生产函数而非自己判**：
     自己再写一遍判定就等于造了第二个口径）；
   - 非终态（QUEUED/RUNNING）→ 计数只是进行中的快照，改判
     `finished_at IS NULL`。
2. **配额账实相符**：每个项目的 `quota_accounts.effective_bytes` 必须等于
   `FileRepository.compute_effective_bytes()` 的重算值。
   这条不变量对应 `research-files` 页面的 `reconciled` 字段 ——
   账面与明细不符时页面会显示告警。
3. **每项目一名活跃负责人**：`projects.owner_id` 必须与该项目唯一的
   `member_role='OWNER'` 活跃成员一致。
4. **P1 链路完整**（仅 `_P1_PROJECT`）：Brief 有生效版本且
   `structured_fields` 与 `BRIEF_FIELDS` 完全一致；草稿有加厚建议；
   信息审核五块非空且 `readiness.ready=true`（**调用生产函数**
   `step3_readiness` 复核）；基线有条目；八步都有 decision 且前 4 个
   `APPROVED`。
5. **P2 试产配置可提交**（仅 `_P1_PROJECT`）：plan-draft 绑定生效基线、
   `state=EDITING`、`totalQuantity` 已设、场景 / 形式启用项合计均为
   1000‰（**调用生产函数** `permille.validate_total` 复核）；
   `pricing_versions` / `cost_estimates` / `project_budgets` /
   `reference_assets` 均非空。

**P1/P2 数据只补「屿见 Yujian 身体乳」**：它是 `GET /projects` 的第一条，
其余项目保持"待办"状态 —— 列表里因此同时有可演示的完整链路与
还没开始的项目。

## 用法

```bash
PY=/Users/<user>/.workbuddy/binaries/python/envs/default/bin/python
NO_PROXY='*' $PY tools/dev/seed_demo_data.py            # 幂等灌入
NO_PROXY='*' $PY tools/dev/seed_demo_data.py --reset    # 先清空再灌（见下方警告）
```

## 三道守卫（`_assert_seedable`，任一不过即退出码 1）

| 门 | 判据 | 失败时的表现 |
|---|---|---|
| 环境 | `REDNOTE_ENVIRONMENT` ∈ `{dev, test}`（**正列举白名单**） | 拒绝执行，不解析 DSN |
| DSN | `REDNOTE_DATABASE_URL` 非空 | 拒绝执行 |
| 库名 | DSN 里的库名 == `rednote_dev`（**正列举白名单**） | 拒绝执行 |

两道门都用白名单而非黑名单，理由见 `SEED_ALLOWED_ENVIRONMENTS` 的注释：
黑名单漏列举会**静默**放行，白名单漏列举会**当场报错**。

`--reset` 用 `TRUNCATE ... CASCADE`。**绕过行级触发器是刻意的**：
`source_segments` / `audit_events` 有不可变触发器（`rednote_reject_mutation`，
BEFORE UPDATE OR DELETE），逐行 DELETE 会被拒 —— 而这两个表恰恰是需要清理的。
TRUNCATE 不触发行级 BEFORE DELETE，因此能清掉。

**`--reset` 的爆炸半径**：CASCADE 会连带清空引用这些表的**其它**表
（`user_sessions` / `upload_sessions` 等）。这在专用演示库上是可接受的，
但脚本会先把要清的表名打印出来。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

# ---------------------------------------------------------------------------
# 路径与导入
# ---------------------------------------------------------------------------
# 本脚本在 <repo>/tools/dev/ 下，因此 parents[2] 是仓库根。
# 用相对本文件定位而非 cwd：从任何目录调用都能跑。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_SRC = _REPO_ROOT / "services" / "backend" / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from sqlalchemy import func, select, text, update  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from rednote.modules.audit.models import (  # noqa: E402
    ACTOR_KIND_SYSTEM,
    ACTOR_KIND_USER,
    AuditEvent,
)
from rednote.modules.baseline.baseline_types import (  # noqa: E402
    APPROVAL_DECISION_APPROVED,
    APPROVAL_DECISION_REJECTED,
    CLAIM_STATE_CONFIRMED,
    CONFLICT_ACTION_ADOPT_ONE,
    CONFLICT_ACTION_RANGE_STATEMENT,
    CONFLICT_KIND_SOURCE,
    CONFLICT_KIND_TIMELINESS,
    CONFLICT_STATE_SETTLED,
    INCLUSION_EXCLUDED,
    INCLUSION_INCLUDED,
    ITEM_KIND_FACT,
    ITEM_KIND_RESEARCH,
    ITEM_KIND_RULE,
    ITEM_KIND_SCENE,
    ITEM_KIND_STYLE,
    SCENE_STATE_NEEDS_LIMIT,
    SCENE_STATE_PRODUCIBLE,
    SUBJECT_KIND_BASELINE,
    SUBJECT_KIND_BRIEF,
    SUBJECT_KIND_RESEARCH,
)
from rednote.modules.baseline.models import (  # noqa: E402
    BaselineItem,
    InformationDraft,
    ProductionBaseline,
    StepApproval,
)
from rednote.modules.baseline.readiness import step3_readiness  # noqa: E402
from rednote.modules.billing.models import (  # noqa: E402
    CostEstimate,
    PricingVersion,
    ProjectBudget,
)
from rednote.modules.billing.types import (  # noqa: E402
    CURRENCY_CNY,
    ESTIMATE_STATE_DRAFT,
    PRICING_STATE_PUBLISHED,
)
from rednote.modules.brief.models import (  # noqa: E402
    BRIEF_SCHEMA_VERSION,
    Brief,
    BriefClaim,
    BriefDraft,
    BriefIssue,
    BriefVersion,
)
from rednote.modules.brief.schema import BRIEF_FIELDS  # noqa: E402
from rednote.modules.brief.service import claim_hash  # noqa: E402
from rednote.modules.brief.types import (  # noqa: E402
    CATEGORY_EXTERNAL_EVIDENCE,
    CATEGORY_SOURCE_FACT,
    CATEGORY_STRATEGY,
    CATEGORY_UNCONFIRMED,
    FIELD_FILLED,
    FIELD_NOT_APPLICABLE,
    ISSUE_OPEN,
    ISSUE_RESOLVED,
    RESOLUTION_PENDING,
    SEVERITY_WARN,
    SOURCE_STATE_SUCCEEDED,
)
from rednote.modules.files.models import (  # noqa: E402
    CLASSIFICATION_SUPPORTED,
    PURPOSE_BRIEF,
    PURPOSE_REFERENCE,
    PURPOSE_RESEARCH,
    QUOTA_LIMIT_BYTES,
    SCAN_STATUS_CLEAN,
    STORAGE_BACKEND_LOCAL,
    STORAGE_STATE_ACTIVE,
    VISIBILITY_PROJECT,
    Blob,
    FileObject,
    ProjectFileLink,
    QuotaAccount,
)
from rednote.modules.files.repository import FileRepository  # noqa: E402
from rednote.modules.gateway.models import Provider  # noqa: E402
from rednote.modules.gateway.types import (  # noqa: E402
    CAPABILITY_STATIC_IMAGE,
    CAPABILITY_TEXT_STRUCTURED,
    NETWORK_LOCAL,
    PROVIDER_ACTIVE,
)
from rednote.modules.identity.models import (  # noqa: E402
    ROLE_ADMIN,
    ROLE_OPERATOR,
    User,
)
from rednote.modules.media.models import ReferenceAsset  # noqa: E402
from rednote.modules.media.types import ASSET_STATE_ACTIVE  # noqa: E402
from rednote.modules.parsing.models import ParseRun, SourceSegment  # noqa: E402
from rednote.modules.parsing.types import (  # noqa: E402
    EXTRACTION_TEXT,
    PARSE_STATE_FAILED,
    PARSE_STATE_PARTIAL,
    PARSE_STATE_RUNNING,
    PARSE_STATE_SUCCEEDED,
    PARSE_TERMINAL_STATES,
    PARSER_VERSION,
    POSITION_PAGE,
    derive_state,
)
from rednote.modules.projects.models import (  # noqa: E402
    EXTERNAL_POLICY_LOCAL_ONLY,
    MEMBER_COLLABORATOR,
    MEMBER_OWNER,
    PHASE_DATA_COLLECTION,
    PHASE_PREPARATION,
    PHASE_PRODUCTION,
    Brand,
    Product,
    Project,
    ProjectMember,
)
from rednote.modules.planning import permille as permille_rules  # noqa: E402
from rednote.modules.planning.models import PlanDraft  # noqa: E402
from rednote.modules.planning.types import (  # noqa: E402
    DRAFT_STATE_EDITING,
    ITEM_STATE_ENABLED,
    PERMILLE_TOTAL,
    AllocationItem,
)
from rednote.modules.versions.models import (  # noqa: E402
    ENTITY_TYPE_BASELINE,
    ENTITY_TYPE_BRIEF,
    VersionNode,
)
from rednote.modules.versions.service import canonical_payload_hash  # noqa: E402
from rednote.modules.work_items.models import (  # noqa: E402
    WORK_ITEM_CANCELLED,
    WORK_ITEM_DONE,
    WORK_ITEM_OPEN,
    WorkItem,
)
from rednote.platform.configuration import Settings  # noqa: E402
from rednote.platform.database import session_scope  # noqa: E402

# ---------------------------------------------------------------------------
# 守卫常量
# ---------------------------------------------------------------------------

# dev 账号的 issuer。与 `modules/devtools/api.py::DEV_ISSUER` **取同一个值**：
# 这样 dev 登录找到的就是种子建的用户，而不是另建一个。
DEV_ISSUER: Final[str] = "urn:rednote:dev"

# **只允许写这一个库**。不是"排除生产库"那种黑名单 ——
# 黑名单挡不住"某天有人建了个 rednote_staging 并拿它当准生产用"。
ALLOWED_DATABASES: Final[frozenset[str]] = frozenset({"rednote_dev"})

# **放行环境白名单**。与 `modules/devtools/api.py::DEV_LOGIN_ENVIRONMENTS`
# 同一个集合，两个门用同一套口径（"哪些环境是开发环境"只应有一处定义）。
#
# **为什么是白名单而不是原来的禁用黑名单**：黑名单
# （`frozenset({"prod", "production", "staging"})`）只能挡住**列举过**的名字。
# `prd` / `live` / `PRODUCTION ` 大小写与空格 / 拼错成 `porudction` ——
# 全部会通过，然后种子数据就写进了那个库。而这两种失败模式的代价**不对等**：
#   · 白名单漏列举 → 种子脚本在某个新环境里**拒绝执行**（退出码 1），
#     人当场看到，改一行就好；
#   · 黑名单漏列举 → 种子数据**静默写进**某个环境，没人知道。
# 前者吵，后者静。对"往库里灌假数据"这种动作，必须选会吵的那个。
#
# 这与 `platform/configuration.py` 里 `session_cookie_secure` 的处理是同一种
# 取向：不是"降低严格度"，而是让**默认失败模式是安全侧**。
SEED_ALLOWED_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"dev", "test"})

# UUID 命名空间。固定值 → `uuid5` 每次算出同一个 id → 幂等。
_SEED_NAMESPACE: Final[uuid.UUID] = uuid.UUID("8f2c1e40-7b3a-4d51-9c6e-2a5f8d0b4713")

# 演示基准日。所有相对时间由它派生，保证重跑结果逐字节一致。
_SEED_DAY: Final[date] = date(2026, 9, 28)


def _uid(kind: str, name: str) -> uuid.UUID:
    """确定性 UUID。同一个 (kind, name) 永远得到同一个 UUID。"""
    return uuid.uuid5(_SEED_NAMESPACE, f"{kind}:{name}")


def _utc(*, days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
    """相对 `_SEED_DAY` 的 UTC 时刻（带 tz）。"""
    base = datetime(
        _SEED_DAY.year, _SEED_DAY.month, _SEED_DAY.day, 9, 0, 0, tzinfo=timezone.utc
    )
    return base + timedelta(days=days, hours=hours, minutes=minutes)


# ---------------------------------------------------------------------------
# 演示数据定义
# ---------------------------------------------------------------------------

# 用户。第一行是 `login.html` 里硬编码的演示账号，因此**必须是管理员** ——
# `POST /projects` 要求 `OrgAction.CREATE_PROJECT`，只有 ADMIN 能建项目。
USERS: Final[tuple[tuple[str, str, str], ...]] = (
    ("member06@example.com", "林知远", ROLE_ADMIN),
    ("member11@example.com", "周予桐", ROLE_OPERATOR),
    ("member01@example.com", "陈默", ROLE_OPERATOR),
    ("member10@example.com", "许南嘉", ROLE_OPERATOR),
)

ADMIN_EMAIL: Final[str] = USERS[0][0]

# 项目。字段来源见模块 docstring 的"取值来源"表。
# `code_tail` 是展示编号后缀，必须只含 Crockford Base32 字母表字符（无 I/L/O/U）。
PROJECTS: Final[tuple[dict[str, Any], ...]] = (
    {
        "code_tail": "7K3M2Q",
        "name": "素与 Su& 氨基酸洁面",
        "brand": "素与 Su&",
        "product": "氨基酸氨基洁面乳 120g",
        "model_code": "SY-AM120",
        "phase": PHASE_PREPARATION,
        "due_date": date(2026, 10, 20),
        "target_delivery_count": 30,
        "target_publication_count": 32,
        "owner": ADMIN_EMAIL,
        "collaborators": ("member11@example.com", "member01@example.com"),
        "description": "氨基酸洁面品类，主攻温和清洁与敏感肌场景。研究资料已齐备。",
    },
    {
        "code_tail": "4H8N1T",
        "name": "初见 Chuu 精华水",
        "brand": "初见 Chuu",
        "product": "Chuu 水感平衡精华水 150ml",
        "model_code": "CH-EW150",
        "phase": PHASE_PRODUCTION,
        "due_date": date(2026, 10, 5),
        "target_delivery_count": 50,
        "target_publication_count": 52,
        "owner": ADMIN_EMAIL,
        "collaborators": (
            "member11@example.com",
            "member01@example.com",
            "member10@example.com",
        ),
        "description": "投产期。10 条试产已通过，扩产批次进行中。",
    },
    {
        "code_tail": "9C5RW2",
        "name": "木野 MUYE 蒸汽眼罩",
        "brand": "木野 MUYE",
        "product": "MUYE 恒温蒸汽眼罩 10 片装",
        "model_code": "MY-ST10",
        "phase": PHASE_PRODUCTION,
        "due_date": date(2026, 9, 30),
        "target_delivery_count": 20,
        "target_publication_count": 20,
        "owner": "member11@example.com",
        "collaborators": ("member01@example.com",),
        "description": "助眠场景，交付倒计时 2 天。",
    },
    {
        "code_tail": "2F6YV8",
        "name": "光屿 Glowis 防晒霜",
        "brand": "光屿 Glowis",
        "product": "Glowis 轻透防晒乳 SPF50+ 50ml",
        "model_code": "GL-SP50",
        "phase": PHASE_DATA_COLLECTION,
        "due_date": date(2026, 9, 25),
        "target_delivery_count": 40,
        "target_publication_count": 42,
        "owner": ADMIN_EMAIL,
        "collaborators": ("member10@example.com",),
        "description": "数据回收期，部分笔记发布数据待回填。",
    },
    {
        "code_tail": "5J9PX4",
        "name": "茶栖 Chaqi 冷萃茶",
        "brand": "茶栖 Chaqi",
        "product": "Chaqi 冷萃乌龙茶 500ml×6",
        "model_code": "CQ-CU06",
        "phase": PHASE_PREPARATION,
        "due_date": date(2026, 11, 8),
        "target_delivery_count": 25,
        "target_publication_count": 26,
        "owner": "member01@example.com",
        "collaborators": ("member11@example.com",),
        "description": "准备期，Brief 已解析，待信息审核。",
    },
    {
        "code_tail": "6Q2ZK7",
        "name": "屿见 Yujian 身体乳",
        "brand": "屿见 Yujian",
        "product": "Yujian 修护身体乳 250ml",
        "model_code": "YJ-BD250",
        "phase": PHASE_PRODUCTION,
        "due_date": date(2026, 9, 15),
        "target_delivery_count": 30,
        "target_publication_count": 30,
        "owner": ADMIN_EMAIL,
        "collaborators": ("member10@example.com",),
        "description": "**已逾期**（截止 09-15），仍有 3 条待交付。",
    },
    {
        "code_tail": "8M4DT3",
        "name": "叁物 SANWU 香薰",
        "brand": "叁物 SANWU",
        "product": "SANWU 雪松香薰蜡烛 200g",
        "model_code": "SW-CD200",
        "phase": PHASE_PREPARATION,
        "due_date": date(2026, 11, 22),
        "target_delivery_count": 18,
        "target_publication_count": 18,
        "owner": "member10@example.com",
        "collaborators": ("member01@example.com",),
        "description": "准备期，研究资料收集中。",
    },
    {
        "code_tail": "3B7GS5",
        "name": "砚白 Yanbai 手霜",
        "brand": "砚白 Yanbai",
        "product": "Yanbai 乳木果护手霜 75ml",
        "model_code": "YB-HC75",
        "phase": PHASE_DATA_COLLECTION,
        "due_date": date(2026, 10, 12),
        "target_delivery_count": 22,
        "target_publication_count": 22,
        "owner": ADMIN_EMAIL,
        "collaborators": ("member11@example.com",),
        "description": "数据回收期，回收率 86%。",
    },
)

# P1 链路演示项目与它的文件。**只补「屿见 Yujian 身体乳」** ——
# 该项目是 `GET /projects`（按 `due_date, id` 排序）返回的第一条，
# 也是唯一被补全 brief → 研究 → 信息审核 → 基线 → 八步批准的项目；
# 其余项目保持原样，列表里因此同时存在"可演示"与"待办"两种状态。
_P1_PROJECT: Final[str] = "屿见 Yujian 身体乳"

# Brief 原文件。与 `_RESEARCH_FILES` **分开**：用途是 BRIEF（不是 RESEARCH），
# 因此既不计入研究配额，也不会出现在 research-files 列表里。
_BRIEF_FILE: Final[str] = "屿见身体乳_Brief_终版.pdf"
_BRIEF_FILE_SPEC: Final[tuple[Any, ...]] = (
    _BRIEF_FILE,
    4_915_712,
    "application/pdf",
    (PARSE_STATE_SUCCEEDED, 8, 8),
    ADMIN_EMAIL,
    -120,
)

# 产品参考素材（`PURPOSE_REFERENCE`）。**不参与解析**，只作为
# `reference_assets.file_object_id` 的落点；形状见
# `modules/media/models.py::ReferenceAsset`。
_REFERENCE_FILES: Final[tuple[tuple[str, int, str, str, int], ...]] = (
    ("产品参考图_正面.png", 1_258_291, "image/png", ADMIN_EMAIL, -168),
    ("产品参考图_泵头细节.png", 942_080, "image/png", ADMIN_EMAIL, -166),
)

# 两项研究资料。`research_selection` 与基线条目会引用它们的真实 file id。
_YUJIAN_RESEARCH_FILES: Final[tuple[str, ...]] = (
    "身体乳_用户痛点访谈.pdf",
    "身体乳_成分与功效资料.pdf",
)

# 研究资料。名称与体积取自 `research-files.html` 的硬编码表格。
# `parse` 是解析规格：(state, total_units, parsed_units)；
# `None` 表示**从未解析**（`latestParse` 返回 null，页面应显示"未解析"）。
_RESEARCH_FILES: Final[dict[str, tuple[tuple[Any, ...], ...]]] = {
    "素与 Su& 氨基酸洁面": (
        # (文件名, 字节, MIME, 解析规格, 上传者, 上传时间偏移小时)
        (
            "氨基酸洁面_品类认知合集.pdf",
            44_669_337,
            "application/pdf",
            (PARSE_STATE_SUCCEEDED, 128, 128),
            ADMIN_EMAIL,
            -384,
        ),
        (
            "小红书_评论抓取_218条.xlsx",
            3_355_443,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            (PARSE_STATE_SUCCEEDED, 218, 218),
            ADMIN_EMAIL,
            -384 + 1,
        ),
        (
            "成分表_备案信息.pdf",
            1_887_437,
            "application/pdf",
            (PARSE_STATE_SUCCEEDED, 12, 12),
            "member11@example.com",
            -336,
        ),
        (
            "竞品话术拆解_12条.pdf",
            29_780_787,
            "application/pdf",
            (PARSE_STATE_PARTIAL, 96, 78),
            ADMIN_EMAIL,
            -288,
        ),
        (
            "场景研究_长文档合集.pdf",
            67_318_579,
            "application/pdf",
            (PARSE_STATE_RUNNING, 240, 61),
            ADMIN_EMAIL,
            -240,
        ),
        (
            "品牌配方备案_加密版.pdf",
            943_718,
            "application/pdf",
            (PARSE_STATE_SUCCEEDED, 8, 8),
            "member01@example.com",
            -360,
        ),
        (
            "功效宣称合规清单.docx",
            2_202_010,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            (PARSE_STATE_SUCCEEDED, 24, 24),
            "member11@example.com",
            -312,
        ),
        (
            "产品问答_客服纪要.txt",
            104_858,
            "text/plain",
            None,  # 从未解析
            "member10@example.com",
            -168,
        ),
    ),
    "初见 Chuu 精华水": (
        (
            "精华水_用户痛点访谈.pdf",
            18_874_368,
            "application/pdf",
            (PARSE_STATE_SUCCEEDED, 64, 64),
            ADMIN_EMAIL,
            -216,
        ),
        (
            "成分机理_文献摘要.pdf",
            8_388_608,
            "application/pdf",
            (PARSE_STATE_FAILED, None, 0),
            "member01@example.com",
            -192,
        ),
    ),
    "茶栖 Chaqi 冷萃茶": (
        (
            "冷萃茶_口味测试纪要.xlsx",
            1_572_864,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            (PARSE_STATE_SUCCEEDED, 36, 36),
            "member01@example.com",
            -120,
        ),
    ),
    # P1 演示项目：两份研究资料都是完整解析，并在信息审核里被选用。
    _P1_PROJECT: (
        (
            _YUJIAN_RESEARCH_FILES[0],
            12_582_912,
            "application/pdf",
            (PARSE_STATE_SUCCEEDED, 48, 48),
            "member10@example.com",
            -144,
        ),
        (
            _YUJIAN_RESEARCH_FILES[1],
            9_437_184,
            "application/pdf",
            (PARSE_STATE_SUCCEEDED, 32, 32),
            "member10@example.com",
            -132,
        ),
    ),
}

# ---------------------------------------------------------------------------
# P1 链路演示数据（仅「屿见 Yujian 身体乳」）
# ---------------------------------------------------------------------------
#
# 形状来源（都在 `services/backend/src/rednote/modules/` 下，只读参考）：
#   · `brief/schema.py::BRIEF_FIELDS` —— structured_fields 的 20 个字段，
#     每个值是 `{"status": FILLED|MISSING|NOT_APPLICABLE, "value": ...}`；
#   · `brief/types.py::ClaimDraft.to_json` —— 草稿 claims 的形状
#     （fieldPath / text / category / applicability / evidence / resolution）；
#   · `brief/types.py::IssueDraft.to_json` —— issue_resolutions 的形状；
#   · `brief/types.py::EnrichmentSuggestion.to_json` —— suggestions 的形状；
#   · `baseline/models.py::InformationDraft` 的列注释 —— claims / conflicts /
#     scenes / boundaries / research_selection 各块的字段；
#   · `baseline/readiness.py` —— 信息审核 `ready=true` 的三条判据：
#     无未处置阻断冲突、至少一个 PRODUCIBLE 场景且引用已确认事实、运营已确认；
#   · `baseline/service.py::_baseline_snapshot` —— 基线级 snapshot 的形状；
#   · `baseline/types.py` —— item_kind / inclusion / decision 的受控取值域。
#
# **这些值不是凭空造的**：产品资料来自 `PROJECTS` 里屿见项目的产品与描述，
# 研究资料名来自本脚本新增的 `_YUJIAN_RESEARCH_FILES`，时间由 `_SEED_DAY` 派生。


def _filled(value: Any) -> dict[str, Any]:
    return {"status": FIELD_FILLED, "value": value}


def _not_applicable(reason: str) -> dict[str, Any]:
    return {"status": FIELD_NOT_APPLICABLE, "value": reason}


def _yujian_structured_fields() -> dict[str, Any]:
    """屿见 Brief 的 20 个结构化字段（需求 §5.1 的 10 组全覆盖）。

    只把 `product.applicable_models` 标为 `NOT_APPLICABLE`（该产品只有
    250ml 单一规格），其余为 `FILLED` —— 这样页面既能展示真实取值，
    也能展示状态域里的第三种状态。
    """
    return {
        # 1. 产品图片及使用权限说明
        "product.images": _filled(
            [
                "官方主图：白色泵瓶正面（250ml）",
                "官方细节图：泵头与质地特写",
                "官方场景图：沐浴后涂抹小腿",
            ]
        ),
        "product.image_usage_rights": _filled(
            "品牌方已授权在小红书图文与视频中使用官方产品图；"
            "禁止二次修改包装文字或遮挡品牌标识。"
        ),
        # 2. 产品名称、品牌、型号
        "product.name": _filled("屿见 Yujian 修护身体乳 250ml"),
        "product.brand": _filled("屿见 Yujian"),
        "product.model_code": _filled("YJ-BD250"),
        # 3. 选购话术及适用购买场景
        "purchase.script": _filled(
            "换季干痒、洗完澡三分钟内的锁水窗口期，按压两泵涂满全身，"
            "重点照顾小腿、手肘和膝盖。"
        ),
        "purchase.scenarios": _filled(
            ["秋冬换季身体干痒", "沐浴后即时保湿", "空调房久待补水"]
        ),
        # 4. 产品解决的痛点
        "product.pain_points": _filled(
            [
                "换季小腿起皮、脱屑",
                "沐浴后皮肤紧绷发痒",
                "空调房长时间待着皮肤干燥",
                "身体乳黏腻、沾睡衣",
            ]
        ),
        # 5. 核心卖点及优先级
        "product.selling_points": _filled(
            [
                {"text": "神经酰胺复配，修护皮肤屏障", "priority": 1},
                {"text": "10 秒吸收，不黏腻", "priority": 2},
                {"text": "无香精配方，敏感肌可用", "priority": 3},
                {"text": "250ml 大容量，全身可用", "priority": 4},
            ]
        ),
        # 6. 原理与功效及支持材料
        "product.mechanism": _filled(
            "神经酰胺 NP、胆固醇与脂肪酸按 3:1:1 比例复配，"
            "补充角质层脂质，帮助修护皮肤屏障。"
        ),
        "product.efficacy": _filled(
            "第三方检测机构 14 天人体试验：连续使用后皮肤水分含量提升 32%。"
        ),
        "product.supporting_materials": _filled(
            ["第三方检测报告：14 天保湿功效人体试验", "品牌方成分备案表"]
        ),
        # 7. 话题标签示例
        "content.hashtag_examples": _filled(
            ["#身体乳", "#换季护肤", "#干皮救星", "#身体护理"]
        ),
        # 8. 产品参数、单位、适用型号
        "product.parameters": _filled(
            [
                "净含量：250ml",
                "质地：乳液",
                "适用肤质：干性 / 中性 / 敏感肌",
                "保质期：3 年",
            ]
        ),
        "product.applicable_models": _not_applicable(
            "目前仅有 250ml 单一规格，无其他适用型号。"
        ),
        # 9. 目标人群、使用场景和限制条件
        "audience.target": _filled(
            "20-35 岁干性 / 敏感肌女性，秋冬换季易起皮发痒。"
        ),
        "audience.usage_scenarios": _filled(
            ["沐浴后 3 分钟内全身涂抹", "睡前重点护理手肘与小腿", "空调房白天补涂"]
        ),
        "audience.constraints": _filled(
            ["避免用于破损皮肤", "首次使用建议先做局部试用", "孕妇使用前请咨询医生"]
        ),
        # 10. 必须提及与禁止提及内容
        "content.must_mention": _filled(
            ["修护皮肤屏障", "神经酰胺复配", "10 秒吸收不黏腻", "250ml 大容量"]
        ),
        "content.must_not_mention": _filled(
            ["治疗湿疹", "替代药物", "7 天彻底根治", "绝对不过敏"]
        ),
    }


# 屿见 Brief 版本的 claims。`segment_no` 指向 Brief 原文解析出的片段
# （`_seed_brief_source` 会按同一命名规则造出这些片段）。
# `SOURCE_FACT` / `EXTERNAL_EVIDENCE` 必须带证据（`brief/types.py` 的
# `ClaimDraft.__post_init__` 与 `brief_claims` 的 CHECK 同一判据）。
_YUJIAN_CLAIMS: Final[tuple[dict[str, Any], ...]] = (
    {
        "key": "name",
        "field_path": "product.name",
        "text": "产品全称为屿见 Yujian 修护身体乳 250ml，型号 YJ-BD250。",
        "category": CATEGORY_SOURCE_FACT,
        "segment_no": 1,
    },
    {
        "key": "selling-points",
        "field_path": "product.selling_points",
        "text": "神经酰胺 NP、胆固醇与脂肪酸按 3:1:1 复配，主打屏障修护。",
        "category": CATEGORY_EXTERNAL_EVIDENCE,
        "segment_no": 2,
    },
    {
        "key": "efficacy",
        "field_path": "product.efficacy",
        "text": "第三方检测报告显示，连续使用 14 天皮肤水分含量提升 32%。",
        "category": CATEGORY_SOURCE_FACT,
        "segment_no": 3,
    },
    {
        "key": "audience",
        "field_path": "audience.target",
        "text": "目标人群为 20-35 岁干性及敏感肌女性，秋冬换季易起皮发痒。",
        "category": CATEGORY_SOURCE_FACT,
        "segment_no": 4,
    },
    {
        "key": "must-not",
        "field_path": "content.must_not_mention",
        "text": "品牌明确禁止宣称治疗湿疹、替代药物或「绝对不过敏」。",
        "category": CATEGORY_SOURCE_FACT,
        "segment_no": 5,
    },
    {
        "key": "mechanism",
        "field_path": "product.mechanism",
        "text": "以角质层脂质补充解释修护路径，适合做科普型内容。",
        "category": CATEGORY_STRATEGY,
        "segment_no": None,
    },
    {
        "key": "pain-points",
        "field_path": "product.pain_points",
        "text": "换季起皮与沐浴后紧绷是主要痛点，仍需评论抓取进一步验证。",
        "category": CATEGORY_UNCONFIRMED,
        "segment_no": None,
    },
)

# 草稿上的两条疑问：一条已处置、一条待处理（WARN 不阻断固化）。
_YUJIAN_DRAFT_ISSUES: Final[tuple[dict[str, Any], ...]] = (
    {
        "key": "models",
        "field_path": "product.applicable_models",
        "description": "Brief 未提供其他适用型号，需确认是否只有 250ml 规格。",
        "state": ISSUE_RESOLVED,
        "resolution": "已与品牌方确认：目前仅有 250ml 单一规格。",
    },
    {
        "key": "hashtags",
        "field_path": "content.hashtag_examples",
        "description": "话题标签示例偏泛，建议补充品牌自有话题。",
        "state": ISSUE_OPEN,
        "resolution": None,
    },
)

# 加厚建议（`brief_drafts.suggestions`）。**不自动生效**，只展示给运营。
_YUJIAN_SUGGESTIONS: Final[tuple[dict[str, Any], ...]] = (
    {
        "fieldPath": "audience.target",
        "text": "补充「产后妈妈」与「健身人群」两个细分人群，并说明各自的干燥场景。",
        "category": CATEGORY_STRATEGY,
        "rationale": "两类人群在评论抓取中反复出现，但 Brief 原文未单列。",
    },
    {
        "fieldPath": "content.hashtag_examples",
        "text": "增加品牌自有话题 #屿见修护计划#，便于后续内容聚合。",
        "category": CATEGORY_STRATEGY,
        "rationale": "现有标签均为品类词，缺品牌可沉淀的话题。",
    },
    {
        "fieldPath": "product.supporting_materials",
        "text": "补充检测报告的出具机构与报告编号，提升外部引用可信度。",
        "category": CATEGORY_STRATEGY,
        "rationale": "当前只写了「第三方检测机构」，无法核验具体来源。",
    },
)

# 信息审核工作区的事实清单（`information_drafts.claims`）。
# `claimId` 是场景 `factRefs` 的引用目标；状态用 `CONFIRMED`，
# 这样 `readiness` 才能派生出「可生产场景」。
_YUJIAN_INFO_CLAIMS: Final[tuple[dict[str, Any], ...]] = (
    {
        "claimId": "clm-yj-01",
        "statement": "产品名称与型号：屿见 Yujian 修护身体乳 250ml / YJ-BD250。",
        "source": "Brief 原文 P2",
        "state": CLAIM_STATE_CONFIRMED,
        "kind": "PRODUCT",
    },
    {
        "claimId": "clm-yj-02",
        "statement": "核心配方为神经酰胺 NP、胆固醇与脂肪酸 3:1:1 复配。",
        "source": "Brief 原文 P4",
        "state": CLAIM_STATE_CONFIRMED,
        "kind": "FORMULA",
    },
    {
        "claimId": "clm-yj-03",
        "statement": "14 天人体试验显示皮肤水分含量提升 32%。",
        "source": "第三方检测报告（研究资料 R-02）",
        "state": CLAIM_STATE_CONFIRMED,
        "kind": "EFFICACY",
    },
    {
        "claimId": "clm-yj-04",
        "statement": "目标人群为 20-35 岁干性 / 敏感肌女性。",
        "source": "Brief 原文 P7",
        "state": CLAIM_STATE_CONFIRMED,
        "kind": "AUDIENCE",
    },
    {
        "claimId": "clm-yj-05",
        "statement": "产品为无香精配方，250ml 单一规格。",
        "source": "Brief 原文 P3",
        "state": CLAIM_STATE_CONFIRMED,
        "kind": "PRODUCT",
    },
    {
        "claimId": "clm-yj-06",
        "statement": "禁止宣称治疗湿疹、替代药物或绝对不过敏。",
        "source": "Brief 原文 P9",
        "state": CLAIM_STATE_CONFIRMED,
        "kind": "COMPLIANCE",
    },
)

# 冲突与处置。阻断冲突必须 `SETTLED`（`readiness` 条件 ①），
# 且已处置必须给 `action`（`_require_conflicts` 的判据）。
_YUJIAN_INFO_CONFLICTS: Final[tuple[dict[str, Any], ...]] = (
    {
        "conflictId": "cft-yj-01",
        "kind": CONFLICT_KIND_TIMELINESS,
        "blocking": True,
        "state": CONFLICT_STATE_SETTLED,
        "action": CONFLICT_ACTION_RANGE_STATEMENT,
        "reason": "「提升 32%」只是 14 天试验结果，不能外推为长期功效。",
        "resolution": "采纳区间表述「连续使用 14 天，水分含量提升 32%」。",
    },
    {
        "conflictId": "cft-yj-02",
        "kind": CONFLICT_KIND_SOURCE,
        "blocking": False,
        "state": CONFLICT_STATE_SETTLED,
        "action": CONFLICT_ACTION_ADOPT_ONE,
        "reason": "Brief 与检测报告对试验周期的写法不一致（14 天 / 两周）。",
        "resolution": "以检测报告的「14 天」为准，Brief 说法视为同义表述。",
    },
)

# 人群场景。PRODUCIBLE 场景必须引用至少一条已确认事实
# （`readiness.producible_scene_ids` 的判据）。
_YUJIAN_INFO_SCENES: Final[tuple[dict[str, Any], ...]] = (
    {
        "sceneId": "scn-yj-01",
        "name": "秋冬换季干痒急救",
        "audience": "20-30 岁干性肌女性",
        "factRefs": ["clm-yj-01", "clm-yj-02"],
        "state": SCENE_STATE_PRODUCIBLE,
    },
    {
        "sceneId": "scn-yj-02",
        "name": "沐浴后 3 分钟锁水",
        "audience": "25-35 岁敏感肌女性",
        "factRefs": ["clm-yj-03", "clm-yj-05"],
        "state": SCENE_STATE_PRODUCIBLE,
    },
    {
        "sceneId": "scn-yj-03",
        "name": "空调房白天补涂",
        "audience": "办公室久坐人群",
        "factRefs": ["clm-yj-03"],
        "state": SCENE_STATE_NEEDS_LIMIT,
    },
)

# 表达边界（`information_drafts.boundaries` 是对象，不是数组）。
_YUJIAN_INFO_BOUNDARIES: Final[dict[str, Any]] = {
    "mustMention": ["修护皮肤屏障", "神经酰胺复配", "10 秒吸收不黏腻"],
    "mustNotMention": ["治疗湿疹", "替代药物", "7 天彻底根治", "绝对不过敏"],
    "bannedPhrases": ["药到病除", "医院同款", "零刺激"],
    "risks": [
        "功效表述必须带「14 天人体试验」限定，不得外推为长期功效。",
        "敏感肌表述需保留「首次使用先做局部试用」的限制条件。",
    ],
}

# 研究资料选用。`included=false` 的行也要留痕并写 `exclusionReason`
# （AC-24：未纳入原因要可查）。
_YUJIAN_RESEARCH_SELECTION: Final[tuple[dict[str, Any], ...]] = (
    {
        "key": "pain-interview",
        "title": _YUJIAN_RESEARCH_FILES[0],
        "included": True,
    },
    {
        "key": "ingredient-doc",
        "title": _YUJIAN_RESEARCH_FILES[1],
        "included": True,
    },
    {
        "key": "brief-source",
        "title": _BRIEF_FILE,
        "included": False,
        "exclusionReason": "Brief 是任务输入，不作为外部研究证据。",
    },
)


def _yujian_claims(project_name: str) -> list[dict[str, Any]]:
    """把 `_YUJIAN_CLAIMS` 展开成草稿 claims（`ClaimDraft.to_json` 的形状）。"""
    claims: list[dict[str, Any]] = []
    for spec in _YUJIAN_CLAIMS:
        item: dict[str, Any] = {
            "fieldPath": spec["field_path"],
            "text": spec["text"],
            "category": spec["category"],
            "applicability": {},
            "resolution": RESOLUTION_PENDING,
        }
        if spec["segment_no"] is not None:
            item["evidence"] = {
                "segmentId": str(
                    _uid(
                        "segment",
                        f"{project_name}/{_BRIEF_FILE}/{spec['segment_no']}",
                    )
                )
            }
        claims.append(item)
    return claims


def _yujian_draft_issues(project_name: str, admin_id: uuid.UUID) -> list[dict[str, Any]]:
    """草稿疑问（`IssueDraft.to_json` 的形状）。"""
    issues: list[dict[str, Any]] = []
    for spec in _YUJIAN_DRAFT_ISSUES:
        item: dict[str, Any] = {
            "id": str(_uid("brief_issue", f"{project_name}/{spec['key']}")),
            "fieldPath": spec["field_path"],
            "severity": SEVERITY_WARN,
            "description": spec["description"],
            "state": spec["state"],
        }
        if spec["state"] != ISSUE_OPEN:
            item["resolution"] = spec["resolution"]
            item["resolvedBy"] = str(admin_id)
        issues.append(item)
    return issues


def _yujian_research_selection(project_name: str) -> list[dict[str, Any]]:
    """研究选用清单；`fileId` 用与 `_seed_research_files` 相同的 `_uid` 规则。"""
    rows: list[dict[str, Any]] = []
    for spec in _YUJIAN_RESEARCH_SELECTION:
        row = {
            "fileId": str(_uid("file", f"{project_name}/{spec['title']}")),
            "title": spec["title"],
            "included": spec["included"],
        }
        if not spec["included"]:
            row["exclusionReason"] = spec["exclusionReason"]
        rows.append(row)
    return rows


def _yujian_baseline_items(project_name: str) -> list[dict[str, Any]]:
    """基线条目（含一条 EXCLUDED，演示「未纳入原因」留痕）。

    `knowledge_asset_id` / `knowledge_version_id` 刻意留空：本次演示
    没有灌知识资产，而这两列非空会触发跨品牌与资产归属校验。
    `context_order` 全部显式给出 —— RESEARCH 类没有默认顺序
    （`baseline/context_order.py` 的 `UNMAPPED_ITEM_KINDS`）。
    """
    segment = str(_uid("segment", f"{project_name}/{_BRIEF_FILE}/3"))
    research_file = str(_uid("file", f"{project_name}/{_YUJIAN_RESEARCH_FILES[0]}"))
    return [
        {
            "no": 1,
            "kind": ITEM_KIND_FACT,
            "inclusion": INCLUSION_INCLUDED,
            "ref_label": "事实 v1",
            "snapshot": {
                "claimId": "clm-yj-01",
                "value": "屿见 Yujian 修护身体乳 250ml / YJ-BD250",
                "sourceSegmentId": segment,
            },
            "snapshot_text": "产品全称为屿见 Yujian 修护身体乳 250ml，型号 YJ-BD250。",
            "context_order": 1,
        },
        {
            "no": 2,
            "kind": ITEM_KIND_FACT,
            "inclusion": INCLUSION_INCLUDED,
            "ref_label": "事实 v2",
            "snapshot": {
                "claimId": "clm-yj-03",
                "value": "14 天人体试验水分含量提升 32%",
                "unit": "percent",
                "qualifier": "14 天试验",
                "sourceSegmentId": segment,
            },
            "snapshot_text": "第三方检测报告显示，连续使用 14 天皮肤水分含量提升 32%。",
            "context_order": 2,
        },
        {
            "no": 3,
            "kind": ITEM_KIND_SCENE,
            "inclusion": INCLUSION_INCLUDED,
            "ref_label": "场景 S1",
            "snapshot": {
                "sceneId": "scn-yj-01",
                "name": "秋冬换季干痒急救",
                "state": SCENE_STATE_PRODUCIBLE,
            },
            "snapshot_text": "秋冬换季干痒急救：20-30 岁干性肌女性，引用事实 clm-yj-01 / clm-yj-02。",
            "context_order": 3,
        },
        {
            "no": 4,
            "kind": ITEM_KIND_RULE,
            "inclusion": INCLUSION_INCLUDED,
            "ref_label": "规则 R-01",
            "snapshot": {
                "rule": "禁止宣称治疗湿疹、替代药物或绝对不过敏",
                "rulesetVersion": "审核规则 v5（9 阻断 · 14 提醒）",
            },
            "snapshot_text": "禁提项：治疗湿疹、替代药物、7 天彻底根治、绝对不过敏。",
            "context_order": 4,
        },
        {
            "no": 5,
            "kind": ITEM_KIND_STYLE,
            "inclusion": INCLUSION_INCLUDED,
            "ref_label": "风格 ST-01",
            "snapshot": {
                "tone": "温和科普",
                "example": "把「屏障修护」讲成人话，不堆砌成分表。",
            },
            "snapshot_text": "项目风格：温和科普，先讲场景再讲成分。",
            "context_order": 5,
        },
        {
            "no": 6,
            "kind": ITEM_KIND_RESEARCH,
            "inclusion": INCLUSION_INCLUDED,
            "ref_label": "R-015",
            "snapshot": {
                "fileId": research_file,
                "title": _YUJIAN_RESEARCH_FILES[0],
            },
            "snapshot_text": "身体乳用户痛点访谈：换季起皮、沐浴后紧绷为主要场景。",
            "context_order": 6,
        },
        {
            "no": 7,
            "kind": ITEM_KIND_RESEARCH,
            "inclusion": INCLUSION_EXCLUDED,
            "ref_label": "R-016",
            "snapshot": {
                "title": _YUJIAN_RESEARCH_FILES[1],
                "reason": "仅有摘要，无公开引用的检测原文",
            },
            "snapshot_text": "成分与功效资料：仅内部参考，未纳入公开证据。",
            "exclusion_reason": "仅有摘要，未取得可公开引用的检测原文。",
            "context_order": 7,
        },
    ]


# --- P2：素材试产配置（plan_drafts / pricing / cost / budget / reference） ---
#
# 形状来源：
#   · `planning/models.py:283-406` —— scene_items / form_items 的行形状
#     与 CHECK 约束；`planning/service.py:453-645` —— 维度与配对的校验；
#   · `planning/service.py:802-953` —— 提交校验的四项
#     （EDITING / totalQuantity / 两维启用项合计 1000‰）；
#   · `billing/models.py:161-449` —— pricing_versions / cost_estimates /
#     project_budgets 的列与 CHECK；
#   · `media/models.py:736-864` —— reference_assets 的列与状态域；
#   · `files/models.py:76-94` —— 用途取值域（REFERENCE 不计研究配额）。

#: 试产总量。与 `DEFAULT_BATCH_MAX['TRIAL'] = 10` 一致
#: （`planning/types.py:126-130`），因此试产计划不会撞批次上限。
_P1_PLAN_TOTAL_QUANTITY: Final[int] = 10

#: 场景维度：启用项合计必须严格等于 1000‰（`PERMILLE_TOTAL`）。
_P1_PLAN_SCENES: Final[tuple[dict[str, Any], ...]] = (
    {
        "key": "scene-yj-01",
        "name": "秋冬换季干痒急救",
        "weightPermille": 400,
        "sortOrder": 0,
        "capacity": None,
        "state": ITEM_STATE_ENABLED,
    },
    {
        "key": "scene-yj-02",
        "name": "沐浴后 3 分钟锁水",
        "weightPermille": 350,
        "sortOrder": 1,
        "capacity": None,
        "state": ITEM_STATE_ENABLED,
    },
    {
        "key": "scene-yj-03",
        "name": "空调房白天补涂",
        "weightPermille": 250,
        "sortOrder": 2,
        "capacity": None,
        "state": ITEM_STATE_ENABLED,
    },
)

#: 形式维度：四项，合计 1000‰。
_P1_PLAN_FORMS: Final[tuple[dict[str, Any], ...]] = (
    {
        "key": "form-yj-01",
        "name": "图文笔记",
        "weightPermille": 500,
        "sortOrder": 0,
        "capacity": None,
        "state": ITEM_STATE_ENABLED,
    },
    {
        "key": "form-yj-02",
        "name": "短视频口播",
        "weightPermille": 300,
        "sortOrder": 1,
        "capacity": None,
        "state": ITEM_STATE_ENABLED,
    },
    {
        "key": "form-yj-03",
        "name": "合集清单",
        "weightPermille": 150,
        "sortOrder": 2,
        "capacity": None,
        "state": ITEM_STATE_ENABLED,
    },
    {
        "key": "form-yj-04",
        "name": "长图文测评",
        "weightPermille": 50,
        "sortOrder": 3,
        "capacity": None,
        "state": ITEM_STATE_ENABLED,
    },
)

#: 场景 × 形式配对。每行只要求两个 key 非空
#: （`planning/service.py:612-645`）；这里按"换季急救优先图文/视频"配。
_P1_PAIRINGS: Final[tuple[dict[str, str], ...]] = (
    {"sceneKey": "scene-yj-01", "formKey": "form-yj-01"},
    {"sceneKey": "scene-yj-01", "formKey": "form-yj-02"},
    {"sceneKey": "scene-yj-02", "formKey": "form-yj-01"},
    {"sceneKey": "scene-yj-02", "formKey": "form-yj-02"},
    {"sceneKey": "scene-yj-03", "formKey": "form-yj-03"},
    {"sceneKey": "scene-yj-03", "formKey": "form-yj-04"},
)

#: 计价供应商。价目是全局资源，不属于任何项目；用 LOCAL 模式避免
#: 出网策略相关字段成为演示前置条件。
_P1_PROVIDER_NAME: Final[str] = "屿见演示 · 内部推理网关"

#: 供应商级价目（`model_profile_id` 为空）。值**必须是字符串**
#: （`billing/models.py:168-179` 的说明：jsonb number 会丢位数）。
_P1_PRICING_LABEL: Final[str] = "2026 年 9 月内部推理价目（演示）"
_P1_PRICING_UNIT_PRICES: Final[dict[str, str]] = {
    "TOKEN": "0.00012000",
    "IMAGE": "0.05000000",
    "SECOND": "0.01000000",
    "CALL": "0.02000000",
}

#: 试产费用预估的明细。
#: 10 次调用 / 30 张图 / 120k token 的演示口径：
#: 0.2 + 1.5 + 14.4 = 16.1 元。
_P1_ESTIMATE_LINES: Final[tuple[dict[str, str], ...]] = (
    {"unit": "CALL", "quantity": "10.00000000", "unitPrice": "0.02000000", "amount": "0.20000000"},
    {"unit": "IMAGE", "quantity": "30.00000000", "unitPrice": "0.05000000", "amount": "1.50000000"},
    {"unit": "TOKEN", "quantity": "120000.00000000", "unitPrice": "0.00012000", "amount": "14.40000000"},
)
_P1_ESTIMATE_TOTAL: Final[Decimal] = Decimal("16.10000000")

#: 项目预算上限。高于预估总额，因此预占路径不会因预算不足被拒。
_P1_BUDGET_LIMIT: Final[Decimal] = Decimal("50000.00000000")

#: 参考素材的标注（`reference_assets` 的 AQ-03 三列）。
_P1_REFERENCE_ASSETS: Final[tuple[dict[str, Any], ...]] = (
    {
        "file_name": _REFERENCE_FILES[0][0],
        "product_model": "YJ-BD250",
        "product_color": "白色",
        "product_part": "瓶身",
        "source": "品牌方样张",
        "note": "正面主图，用于核对瓶型与标签位置。",
    },
    {
        "file_name": _REFERENCE_FILES[1][0],
        "product_model": "YJ-BD250",
        "product_color": "白色",
        "product_part": "泵头",
        "source": "品牌方样张",
        "note": "泵头细节，用于核对出液口与压泵结构。",
    },
)


# 待办。`kind` 取值见模块 docstring —— 来自 PRD §21.1 的 7 类通知事件，
# 在 M3 落地前**不构成契约**。
# subject 指向真实存在的对象（project / file / parse_run），
# 因为 `subject_id` 无外键，写错不会有任何报错 —— 所以这里刻意只用
# 能由本脚本确定的对象。
WORK_ITEMS: Final[tuple[dict[str, Any], ...]] = (
    {
        "key": "wu-parse-failed-chuu",
        "project": "初见 Chuu 精华水",
        "kind": "TASK_FAILED",
        "subject_type": "FILE",
        "subject_file": "成分机理_文献摘要.pdf",
        "assignee": ADMIN_EMAIL,
        "due_offset_days": 1,
        "dedupe_key": "TASK_FAILED:chuu:成分机理_文献摘要.pdf",
    },
    {
        "key": "wu-partial-yujian",
        "project": "屿见 Yujian 身体乳",
        "kind": "MANUAL_CONFIRM",
        "subject_type": "PROJECT",
        "assignee": ADMIN_EMAIL,
        "due_offset_days": 0,
        "dedupe_key": "MANUAL_CONFIRM:yujian:delivery-count",
    },
    {
        "key": "wu-overdue-yujian",
        "project": "屿见 Yujian 身体乳",
        "kind": "DUE_SOON",
        "subject_type": "PROJECT",
        "assignee": ADMIN_EMAIL,
        "due_offset_days": -13,
        "dedupe_key": "DUE_SOON:yujian:2026-09-15",
    },
    {
        "key": "wu-datacollection-glowis",
        "project": "光屿 Glowis 防晒霜",
        "kind": "DATA_COLLECTION_BLOCKED",
        "subject_type": "PROJECT",
        "assignee": ADMIN_EMAIL,
        "due_offset_days": 2,
        "dedupe_key": "DATA_COLLECTION_BLOCKED:glowis:note-118",
    },
    {
        "key": "wu-budget-muye",
        "project": "木野 MUYE 蒸汽眼罩",
        "kind": "BUDGET_ALERT",
        "subject_type": "PROJECT",
        "assignee": "member11@example.com",
        "due_offset_days": 3,
        "dedupe_key": "BUDGET_ALERT:muye:0.85",
    },
    {
        "key": "wu-owner-changed-chaqi",
        "project": "茶栖 Chaqi 冷萃茶",
        "kind": "OWNER_CHANGED",
        "subject_type": "PROJECT",
        "assignee": "member01@example.com",
        "due_offset_days": 5,
        "dedupe_key": "OWNER_CHANGED:chaqi:2026-09-20",
    },
    {
        "key": "wu-fact-correction-sanwu",
        "project": "叁物 SANWU 香薰",
        "kind": "FACT_CORRECTION",
        "subject_type": "PROJECT",
        "assignee": "member10@example.com",
        "due_offset_days": 7,
        "dedupe_key": "FACT_CORRECTION:sanwu:01",
    },
    {
        # 未认领待办（`assignee_id IS NULL`）。需求 §2.3 第 2 条明确存在这种状态。
        "key": "wu-unclaimed-yanbai",
        "project": "砚白 Yanbai 手霜",
        "kind": "TASK_COMPLETED",
        "subject_type": "PROJECT",
        "assignee": None,
        "due_offset_days": 10,
        "dedupe_key": "TASK_COMPLETED:yanbai:batch-02",
    },
)

# 审计事件。`before_hash` / `after_hash` 必须**同时有或同时无**（CHECK 约束）。
AUDIT_EVENTS: Final[tuple[dict[str, Any], ...]] = (
    {
        "key": "audit-project-created",
        "project": "素与 Su& 氨基酸洁面",
        "actor_email": ADMIN_EMAIL,
        "actor_kind": ACTOR_KIND_USER,
        "action": "project.created",
        "object_type": "PROJECT",
        "reason": "按品牌方 2026 秋季新品计划立项。",
        "hours_offset": -480,
    },
    {
        "key": "audit-member-added",
        "project": "初见 Chuu 精华水",
        "actor_email": ADMIN_EMAIL,
        "actor_kind": ACTOR_KIND_USER,
        "action": "project.member.added",
        "object_type": "PROJECT",
        "reason": "新增协作者以承接扩产批次。",
        "hours_offset": -200,
    },
    {
        "key": "audit-research-linked",
        "project": "素与 Su& 氨基酸洁面",
        "actor_email": "member11@example.com",
        "actor_kind": ACTOR_KIND_USER,
        "action": "file.linked",
        "object_type": "FILE",
        "reason": None,
        "hours_offset": -336,
    },
    {
        "key": "audit-parse-scheduled",
        "project": "素与 Su& 氨基酸洁面",
        "actor_email": None,
        "actor_kind": ACTOR_KIND_SYSTEM,
        "action": "parse.scheduled",
        "object_type": "PARSE_RUN",
        "reason": None,
        "hours_offset": -240,
    },
)


# ---------------------------------------------------------------------------
# 守卫
# ---------------------------------------------------------------------------


def _database_name(dsn: str) -> str:
    """从 DSN 取库名。取不到就返回空串（后续白名单会拒绝）。"""
    try:
        return str(make_url(dsn).database or "")
    except Exception:  # noqa: BLE001 - DSN 形态错误时返回空串，交给白名单拒绝
        return ""


def _assert_seedable(settings: Settings) -> str:
    """三道守卫：环境白名单 + DSN 存在 + 库名白名单。**任一不满足即退出**。

    为什么要有环境这道门（库名已经够严了）：库名是**连接串里写的**，
    而一个"连对了库名、但进程跑在生产配置下"的场景是可能的 ——
    例如有人在生产机器上把 `REDNOTE_DATABASE_URL` 指到了那个库。
    环境变量是另一个独立信号，两个都过才算安全。

    门与门的顺序**刻意如此**：环境门放最前，因为它不依赖 DSN 能否解析 ——
    `REDNOTE_ENVIRONMENT=prod` 时应当在读连接串之前就被挡住。
    """
    environment = settings.environment.strip().lower()
    if environment not in SEED_ALLOWED_ENVIRONMENTS:
        raise SystemExit(
            f"拒绝执行：REDNOTE_ENVIRONMENT={settings.environment!r} 不在放行白名单"
            f"（{sorted(SEED_ALLOWED_ENVIRONMENTS)}）内。\n"
            "种子数据是演示用的假数据，只允许进 dev / test。\n"
            "（这是**白名单**：未列举的环境名一律拒绝，不做例外判断。）"
        )

    if not settings.database_url:
        raise SystemExit("拒绝执行：REDNOTE_DATABASE_URL 未配置。")

    db_name = _database_name(settings.database_url)
    if db_name not in ALLOWED_DATABASES:
        raise SystemExit(
            f"拒绝执行：目标库为 {db_name!r}，不在白名单 {sorted(ALLOWED_DATABASES)} 内。"
            "\n本脚本只允许写 rednote_dev（理由见模块 docstring）。"
        )
    return db_name


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------

# 清理顺序即依赖顺序（先子后父）。TRUNCATE 一次性列出所有表，
# PG 会自动处理表间引用，无需严格排序 —— 但保持有序便于阅读与核对。
_RESET_TABLES: Final[tuple[str, ...]] = (
    "source_segments",
    "parse_runs",
    "project_file_links",
    "file_objects",
    "blobs",
    "quota_accounts",
    "work_items",
    "audit_events",
    "project_members",
    "projects",
    "products",
    "brands",
    "users",
)


def _reset(session: Any) -> None:
    """清空种子涉及的表。

    用 `TRUNCATE ... CASCADE` 而非逐行 DELETE：`source_segments` 与
    `audit_events` 挂了不可变触发器（BEFORE UPDATE OR DELETE），
    逐行删会被 `rednote_reject_mutation` 拒绝（SQLSTATE 23001）——
    而这两个表正是需要清理的。TRUNCATE 不触发行级 BEFORE DELETE。
    """
    print("[reset] 即将 TRUNCATE（CASCADE 会连带清空引用这些表的其它表）：")
    for name in _RESET_TABLES:
        print(f"         - {name}")
    session.execute(
        text(f"TRUNCATE {', '.join(_RESET_TABLES)} RESTART IDENTITY CASCADE")
    )


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def _insert(session: Any, model: Any, rows: list[dict[str, Any]]) -> int:
    """幂等批量插入。返回实际插入行数。

    用 `on_conflict_do_nothing()`（**不带 target**）：目标表上任何唯一约束
    冲突都跳过。带上 `index_elements` 也能work，但本脚本对 13 张表写入，
    每张表的唯一键不同（PK / `uq_users_issuer_subject` /
    `uq_projects_project_code` / 部分唯一索引 …），逐一列举只会增加
    写错的机会 —— 而写错的后果是"冲突没被跳过，第二次运行直接报错"，
    或更糟：跳过条件不对，重复插入了数据且不报错。
    """
    if not rows:
        return 0
    stmt = pg_insert(model.__table__).values(rows).on_conflict_do_nothing()
    result = session.execute(stmt)
    return int(result.rowcount or 0)


def _seed_users(session: Any) -> dict[str, uuid.UUID]:
    rows = [
        {
            "id": _uid("user", email),
            "issuer": DEV_ISSUER,
            "subject": email,
            "display_name": display_name,
            "role": role,
            # status 用服务端默认 ACTIVE。CHECK 要求
            # (status='DISABLED') = (disabled_at IS NOT NULL)，不传 disabled_at 即一致。
        }
        for email, display_name, role in USERS
    ]
    _insert(session, User, rows)
    return {email: _uid("user", email) for email, _, _ in USERS}


def _seed_dictionary(session: Any, projects: tuple[dict[str, Any], ...]) -> None:
    """灌品牌与产品字典。

    **去重按名字**：多个项目可能共享同一品牌（本数据集没有，但结构上允许），
    因此这里按出现过的品牌名去重，而不是逐项目建一个品牌。
    """
    brands: dict[str, uuid.UUID] = {}
    products: dict[tuple[str, str], uuid.UUID] = {}

    for spec in projects:
        brands.setdefault(spec["brand"], _uid("brand", spec["brand"]))
        products.setdefault(
            (spec["brand"], spec["product"]),
            _uid("product", f"{spec['brand']}/{spec['product']}"),
        )

    _insert(
        session,
        Brand,
        [{"id": bid, "name": name} for name, bid in brands.items()],
    )
    _insert(
        session,
        Product,
        [
            {
                "id": pid,
                "brand_id": brands[brand_name],
                "name": product_name,
                "model_code": _model_code_for(projects, brand_name, product_name),
            }
            for (brand_name, product_name), pid in products.items()
        ],
    )


def _model_code_for(
    projects: tuple[dict[str, Any], ...], brand: str, product: str
) -> str | None:
    for spec in projects:
        if spec["brand"] == brand and spec["product"] == product:
            return spec["model_code"]
    return None


def _seed_projects(
    session: Any,
    projects: tuple[dict[str, Any], ...],
    users: dict[str, uuid.UUID],
) -> dict[str, uuid.UUID]:
    """灌项目 + 成员 + 配额账户。返回 {项目名: uuid}。"""
    project_ids = {spec["name"]: _uid("project", spec["name"]) for spec in projects}

    # 展示编号：`PRJ-{YYYYMM}-{tail}`。月份取 `_SEED_DAY`。
    month = f"{_SEED_DAY.year}{_SEED_DAY.month:02d}"
    project_rows = [
        {
            "id": project_ids[spec["name"]],
            "project_code": f"PRJ-{month}-{spec['code_tail']}",
            "name": spec["name"],
            "brand_id": _uid("brand", spec["brand"]),
            "product_id": _uid("product", f"{spec['brand']}/{spec['product']}"),
            "owner_id": users[spec["owner"]],
            "phase": spec["phase"],
            "target_delivery_count": spec["target_delivery_count"],
            "target_publication_count": spec["target_publication_count"],
            "due_date": spec["due_date"],
            "external_policy": EXTERNAL_POLICY_LOCAL_ONLY,
            "description": spec["description"],
            # phase != CLOSED ⇒ closed_at 必须为 NULL（CHECK 约束）。
            "closed_at": None,
        }
        for spec in projects
    ]
    _insert(session, Project, project_rows)

    # 成员：每项目恰好一名活跃 OWNER。`uq_project_members_active_owner`
    # 是"每项目活跃负责人唯一"的数据库级保证 —— 多插一条 OWNER 会直接报错。
    member_rows: list[dict[str, Any]] = []
    for spec in projects:
        pid = project_ids[spec["name"]]
        member_rows.append(
            {
                "id": _uid("member", f"{spec['name']}/{spec['owner']}"),
                "project_id": pid,
                "user_id": users[spec["owner"]],
                "member_role": MEMBER_OWNER,
            }
        )
        for email in spec.get("collaborators", ()):
            member_rows.append(
                {
                    "id": _uid("member", f"{spec['name']}/{email}"),
                    "project_id": pid,
                    "user_id": users[email],
                    "member_role": MEMBER_COLLABORATOR,
                }
            )
    _insert(session, ProjectMember, member_rows)

    return project_ids


def _seed_research_files(
    session: Any,
    project_ids: dict[str, uuid.UUID],
    users: dict[str, uuid.UUID],
) -> None:
    """灌研究资料：blobs → file_objects → project_file_links。

    **配额账面（`quota_accounts.effective_bytes`）不在这里写** ——
    它必须等于 `FileRepository.compute_effective_bytes()` 的重算值，
    因此由 `_seed_quota_accounts` 在明细写完后统一算。
    分两处写会让两个数有机会不一致，而不一致**不会报错**，
    只会在页面上显示一个配额告警（`reconciled=false`）。
    """
    blob_rows: list[dict[str, Any]] = []
    file_rows: list[dict[str, Any]] = []
    link_rows: list[dict[str, Any]] = []

    for project_name, files in _RESEARCH_FILES.items():
        pid = project_ids[project_name]
        for file_name, byte_size, mime, _parse, uploader_email, hour_offset in files:
            digest = hashlib.sha256(f"{project_name}/{file_name}".encode()).hexdigest()
            bid = _uid("blob", f"{project_name}/{file_name}")
            fid = _uid("file", f"{project_name}/{file_name}")

            blob_rows.append(
                {
                    "id": bid,
                    "storage_backend": STORAGE_BACKEND_LOCAL,
                    "object_key": f"demo/{pid}/{digest[:12]}/{file_name}",
                    "sha256": digest,
                    "byte_size": byte_size,
                    "mime_type": mime,
                    # 资料可用 ⇒ 必须是被扫过的（未扫的 blob 在上传完成态才允许存在）。
                    "scan_status": SCAN_STATUS_CLEAN,
                    "storage_state": STORAGE_STATE_ACTIVE,
                }
            )
            file_rows.append(
                {
                    "id": fid,
                    "blob_id": bid,
                    "original_name": file_name,
                    # PROJECT ⇒ project_id NOT NULL（CHECK 约束）。
                    "visibility": VISIBILITY_PROJECT,
                    "project_id": pid,
                    "classification": CLASSIFICATION_SUPPORTED,
                    "uploaded_by": users[uploader_email],
                    "created_at": _utc(hours=hour_offset),
                }
            )
            link_rows.append(
                {
                    "id": _uid("link", f"{project_name}/{file_name}"),
                    "project_id": pid,
                    "file_id": fid,
                    # RESEARCH 是唯一计入配额的用途（`QUOTA_PURPOSES`）。
                    "purpose": PURPOSE_RESEARCH,
                    "effective": True,
                }
            )

    _insert(session, Blob, blob_rows)
    _insert(session, FileObject, file_rows)
    _insert(session, ProjectFileLink, link_rows)


def _seed_quota_accounts(session: Any, project_ids: dict[str, uuid.UUID]) -> None:
    """按**重算值**写配额账户。

    `compute_effective_bytes` 是生产代码里的重算函数（文档 02 §13.1 的
    对账口径）。种子脚本调用它而不是自己求和 —— 自己求和就等于造了
    第二个口径，两个口径一旦漂移，页面的 `reconciled` 会长期告警，
    而没人能说清是数据错还是代码错。
    """
    repo = FileRepository(session)
    rows = [
        {
            "project_id": pid,
            "limit_bytes": QUOTA_LIMIT_BYTES,
            "effective_bytes": repo.compute_effective_bytes(pid),
            "reserved_bytes": 0,
        }
        for pid in project_ids.values()
    ]
    # **这里必须 upsert 而不是 `DO NOTHING`**：配额账面是重算值的投影。
    # 本脚本新增了屿见项目的研究资料，而该项目的配额行在之前的运行里
    # 已存在（值为旧的 0）—— `DO NOTHING` 会保留旧账面，于是 `_verify`
    # 的"账实相符"必然失败。upsert 让"账面 = 重算值"在每次运行后都成立，
    # 这也正是 `_verify` 第 2 条不变量要求的。
    insert_stmt = pg_insert(QuotaAccount.__table__).values(rows)
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=[QuotaAccount.project_id],
        set_={
            "limit_bytes": insert_stmt.excluded.limit_bytes,
            "effective_bytes": insert_stmt.excluded.effective_bytes,
            "reserved_bytes": insert_stmt.excluded.reserved_bytes,
        },
    )
    session.execute(stmt)


def _seed_parsing(
    session: Any, project_ids: dict[str, uuid.UUID]
) -> dict[str, uuid.UUID]:
    """灌 parse_runs + source_segments。返回 {文件名: parse_run_id}。

    **状态不是随便写的**：`parse_runs` 有 4 条 CHECK 把状态与计数绑死
    （SUCCEEDED ⇒ total 非空且 parsed==total>0；PARTIAL ⇒ 0<parsed<total；
    终态 ⇒ finished_at 非空）。这里按分支逐条构造，最后再由 `_verify`
    用生产的 `derive_state` 复核一遍。
    """
    run_rows: list[dict[str, Any]] = []
    segment_rows: list[dict[str, Any]] = []
    run_ids: dict[str, uuid.UUID] = {}
    order = 0

    for project_name, files in _RESEARCH_FILES.items():
        pid = project_ids[project_name]
        for file_name, _size, _mime, spec, _up, hour_offset in files:
            if spec is None:
                continue  # 从未解析 → 没有 parse_run 行
            state, total, parsed = spec
            order += 1
            fid = _uid("file", f"{project_name}/{file_name}")
            rid = _uid("parse_run", f"{project_name}/{file_name}")
            run_ids[file_name] = rid

            terminal = state in {
                PARSE_STATE_SUCCEEDED,
                PARSE_STATE_PARTIAL,
                PARSE_STATE_FAILED,
            }
            started = _utc(hours=hour_offset)
            run_rows.append(
                {
                    "id": rid,
                    "file_id": fid,
                    "parser_version": PARSER_VERSION,
                    "state": state,
                    "total_units": total,
                    "parsed_units": parsed,
                    "errors": (
                        [{"unit": 5, "message": "解析器在复杂版式上提前中止。"}]
                        if state in {PARSE_STATE_PARTIAL, PARSE_STATE_FAILED}
                        else []
                    ),
                    "ocr_engine": "none",
                    "started_at": started,
                    # 终态必须有 finished_at（CHECK）。非终态必须没有 ——
                    # 否则"卡住的解析"与"跑完的解析"在库里长得一样。
                    "finished_at": started + timedelta(minutes=6) if terminal else None,
                    "created_at": started,
                }
            )

            # 片段只给"解出了内容"的 run 造。`parsed > 0` 才有东西可解出来。
            if parsed <= 0:
                continue
            for idx in range(1, min(parsed, 4) + 1):
                segment_text = (
                    f"{file_name} · 第 {idx} 段："
                    f"{project_name} 的{('成分机理' if idx % 2 else '用户场景')}摘录。"
                )
                segment_rows.append(
                    {
                        "id": _uid("segment", f"{project_name}/{file_name}/{idx}"),
                        "parse_run_id": rid,
                        "segment_no": idx,
                        # position 必须含 kind（CHECK `position_has_kind`）。
                        "position": {"kind": POSITION_PAGE, "page": idx},
                        "text_content": segment_text,
                        "content_hash": hashlib.sha256(
                            segment_text.encode()
                        ).hexdigest(),
                        "extraction_kind": EXTRACTION_TEXT,
                    }
                )

    _insert(session, ParseRun, run_rows)
    _insert(session, SourceSegment, segment_rows)
    return run_ids


def _seed_brief_source(
    session: Any,
    project_ids: dict[str, uuid.UUID],
    users: dict[str, uuid.UUID],
) -> dict[str, Any]:
    """为 P1 演示项目灌 Brief 原文件 + 解析运行 + 片段。

    **为什么单独一份文件**：`brief_versions.source_file_id` 是 NOT NULL，
    且 `GET /brief` 的 `source` 块与 claims 的原文对照都以它为锚点。
    该文件的用途是 `BRIEF`（不是 `RESEARCH`），因此不计入研究配额
    （`files/models.py` 的 `QUOTA_PURPOSES` 只含 `RESEARCH`）。
    """
    project_name = _P1_PROJECT
    pid = project_ids[project_name]
    file_name, byte_size, mime, spec, uploader_email, hour_offset = _BRIEF_FILE_SPEC
    state, total, parsed = spec
    assert state == PARSE_STATE_SUCCEEDED  # 本演示只造"完整解析"的 Brief

    digest = hashlib.sha256(f"{project_name}/{file_name}".encode()).hexdigest()
    blob_id = _uid("blob", f"{project_name}/{file_name}")
    file_id = _uid("file", f"{project_name}/{file_name}")
    run_id = _uid("parse_run", f"{project_name}/{file_name}")
    started = _utc(hours=hour_offset)

    _insert(
        session,
        Blob,
        [
            {
                "id": blob_id,
                "storage_backend": STORAGE_BACKEND_LOCAL,
                "object_key": f"demo/{pid}/{digest[:12]}/{file_name}",
                "sha256": digest,
                "byte_size": byte_size,
                "mime_type": mime,
                "scan_status": SCAN_STATUS_CLEAN,
                "storage_state": STORAGE_STATE_ACTIVE,
            }
        ],
    )
    _insert(
        session,
        FileObject,
        [
            {
                "id": file_id,
                "blob_id": blob_id,
                "original_name": file_name,
                "visibility": VISIBILITY_PROJECT,
                "project_id": pid,
                "classification": CLASSIFICATION_SUPPORTED,
                "uploaded_by": users[uploader_email],
                "created_at": started,
            }
        ],
    )
    _insert(
        session,
        ProjectFileLink,
        [
            {
                "id": _uid("link", f"{project_name}/{file_name}"),
                "project_id": pid,
                "file_id": file_id,
                # BRIEF 用途是"能对该文件发起 Brief 解构"的前提
                # （`brief/service.py::require_brief_file` 判据 3）。
                "purpose": PURPOSE_BRIEF,
                "effective": True,
            }
        ],
    )
    _insert(
        session,
        ParseRun,
        [
            {
                "id": run_id,
                "file_id": file_id,
                "parser_version": PARSER_VERSION,
                "state": state,
                "total_units": total,
                "parsed_units": parsed,
                "errors": [],
                "ocr_engine": "none",
                "started_at": started,
                "finished_at": started + timedelta(minutes=4),
                "created_at": started,
            }
        ],
    )

    segments: dict[int, uuid.UUID] = {}
    segment_rows: list[dict[str, Any]] = []
    for idx in range(1, parsed + 1):
        segment_id = _uid("segment", f"{project_name}/{file_name}/{idx}")
        segment_text = (
            f"{file_name} · 第 {idx} 段：屿见 Yujian 身体乳 Brief 原文摘录"
            f"（产品资料 / 成分 / 人群 / 禁提项之一）。"
        )
        segments[idx] = segment_id
        segment_rows.append(
            {
                "id": segment_id,
                "parse_run_id": run_id,
                "segment_no": idx,
                "position": {"kind": POSITION_PAGE, "page": idx},
                "text_content": segment_text,
                "content_hash": hashlib.sha256(segment_text.encode()).hexdigest(),
                "extraction_kind": EXTRACTION_TEXT,
            }
        )
    _insert(session, SourceSegment, segment_rows)
    return {
        "file_id": file_id,
        "parse_run_id": run_id,
        "segments": segments,
    }


def _seed_p1_chain(
    session: Any,
    project_ids: dict[str, uuid.UUID],
    users: dict[str, uuid.UUID],
    source: dict[str, Any],
) -> dict[str, Any]:
    """给屿见项目补全 P1 链路：brief → 信息审核 → 基线 → 八步批准。

    **只处理一个项目**：其余项目保持"待办"状态，列表里既有可演示的
    完整链路，也有还没开始的数据。

    **为什么这里用 `SELECT → UPDATE/INSERT` 而不是纯 `ON CONFLICT DO NOTHING`**：
    `briefs` / `brief_drafts` / `information_drafts` 可能已经由
    `GET /brief`、`GET /information-draft` 的懒创建写入（本机现状就是如此），
    它们的 id 是随机的。纯 DO NOTHING 会保留那些空行，页面仍然空白。
    因此这三张**可变表**按唯一键找到既有行后就地更新；其余**不可变表**
    （版本 / claims / issues / 基线 / 条目 / 批准）仍用固定 UUID +
    `ON CONFLICT DO NOTHING`，第二次运行不会新增任何行。
    """
    project_name = _P1_PROJECT
    pid = project_ids[project_name]
    admin_id = users[ADMIN_EMAIL]
    now = _utc(hours=-60)

    fields = _yujian_structured_fields()
    draft_claims = _yujian_claims(project_name)
    draft_issues = _yujian_draft_issues(project_name, admin_id)

    # --- 1. 逻辑 Brief（一项目一行） ------------------------------------
    brief_id = session.execute(
        select(Brief.id).where(Brief.project_id == pid)
    ).scalar_one_or_none()
    if brief_id is None:
        brief_id = _uid("brief", project_name)
        _insert(
            session,
            Brief,
            [{"id": brief_id, "project_id": pid, "created_at": now, "updated_at": now}],
        )

    # --- 2. 草稿（一个 Brief 一行） --------------------------------------
    draft_id = session.execute(
        select(BriefDraft.id).where(BriefDraft.brief_id == brief_id)
    ).scalar_one_or_none()
    if draft_id is None:
        draft_id = _uid("brief_draft", project_name)
        _insert(
            session,
            BriefDraft,
            [
                {
                    "id": draft_id,
                    "brief_id": brief_id,
                    "structured_fields": fields,
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )

    # --- 3. Brief 版本节点 + 具体版本行 + claims / issues -----------------
    version_id = _uid("brief_version", project_name)
    source_file_id = source["file_id"]
    source_run_id = source["parse_run_id"]

    claim_rows: list[dict[str, Any]] = []
    for index, spec in enumerate(_YUJIAN_CLAIMS, start=1):
        segment_id = (
            None
            if spec["segment_no"] is None
            else source["segments"][spec["segment_no"]]
        )
        claim_rows.append(
            {
                "id": _uid("brief_claim", f"{project_name}/{spec['key']}"),
                "brief_version_id": version_id,
                "field_path": spec["field_path"],
                "claim_text": spec["text"],
                "category": spec["category"],
                "evidence_segment_id": segment_id,
                "evidence_url": None,
                "applicability": {},
                "resolution_status": RESOLUTION_PENDING,
                "resolution_reason": None,
                "confirmed_by": None,
                "claim_hash": claim_hash(
                    field_path=spec["field_path"], text=spec["text"]
                ),
                "created_at": now,
            }
        )

    issue_rows: list[dict[str, Any]] = []
    for spec in _YUJIAN_DRAFT_ISSUES:
        settled = spec["state"] != ISSUE_OPEN
        issue_rows.append(
            {
                "id": _uid("brief_issue", f"{project_name}/{spec['key']}"),
                "project_id": pid,
                "brief_version_id": version_id,
                "field_path": spec["field_path"],
                "severity": SEVERITY_WARN,
                "description": spec["description"],
                "state": spec["state"],
                "resolution": spec["resolution"] if settled else None,
                "resolved_by": admin_id if settled else None,
                "created_at": now,
            }
        )

    version_payload = {
        "schemaVersion": BRIEF_SCHEMA_VERSION,
        "briefId": str(brief_id),
        "sourceFileId": str(source_file_id),
        "sourceParseRunId": str(source_run_id),
        "sourceState": SOURCE_STATE_SUCCEEDED,
        "structuredFields": fields,
        "claims": draft_claims,
        "issues": draft_issues,
    }
    _insert(
        session,
        VersionNode,
        [
            {
                "id": version_id,
                "entity_type": ENTITY_TYPE_BRIEF,
                "entity_id": brief_id,
                "project_id": pid,
                "revision_no": 1,
                "schema_version": BRIEF_SCHEMA_VERSION,
                "payload_hash": canonical_payload_hash(version_payload),
                "created_at": now,
                "created_by": admin_id,
            }
        ],
    )
    summary = (
        "屿见 Yujian 修护身体乳 250ml｜卖点 3/3 项："
        "神经酰胺复配，修护皮肤屏障；10 秒吸收，不黏腻；无香精配方，敏感肌可用"
        "｜必须提及 4 项｜禁止提及 4 项"
    )
    _insert(
        session,
        BriefVersion,
        [
            {
                "id": version_id,
                "brief_id": brief_id,
                "source_file_id": source_file_id,
                "source_parse_run_id": source_run_id,
                "source_state": SOURCE_STATE_SUCCEEDED,
                "product_name": "屿见 Yujian 修护身体乳 250ml",
                "model_code": "YJ-BD250",
                "summary": summary,
                "structured_fields": fields,
                "schema_version": BRIEF_SCHEMA_VERSION,
                "created_at": now,
                "created_by": admin_id,
            }
        ],
    )
    _insert(session, BriefClaim, claim_rows)
    _insert(session, BriefIssue, issue_rows)

    # --- 4. 回填草稿与 Brief 投影列（可变行，固定值 → 幂等） --------------
    session.execute(
        update(BriefDraft)
        .where(BriefDraft.id == draft_id)
        .values(
            base_version_id=version_id,
            structured_fields=fields,
            claims=draft_claims,
            issue_resolutions=draft_issues,
            suggestions=[dict(item) for item in _YUJIAN_SUGGESTIONS],
            suggestions_version_id=version_id,
            enrichment_request=None,
            source_file_id=source_file_id,
            source_parse_run_id=source_run_id,
            source_state=SOURCE_STATE_SUCCEEDED,
            source_errors=[],
            schema_version=BRIEF_SCHEMA_VERSION,
            updated_at=now,
        )
    )
    session.execute(
        update(Brief)
        .where(Brief.id == brief_id)
        .values(active_version_id=version_id, draft_id=draft_id, updated_at=now)
    )

    # --- 5. 信息审核工作区（一项目一行） ---------------------------------
    info_values: dict[str, Any] = {
        "brief_version_id": version_id,
        "claims": [dict(item) for item in _YUJIAN_INFO_CLAIMS],
        "conflicts": [dict(item) for item in _YUJIAN_INFO_CONFLICTS],
        "scenes": [dict(item) for item in _YUJIAN_INFO_SCENES],
        "boundaries": dict(_YUJIAN_INFO_BOUNDARIES),
        "research_selection": _yujian_research_selection(project_name),
        "operator_confirmed": True,
        "schema_version": 1,
        "updated_at": now,
    }
    information_draft_id = session.execute(
        select(InformationDraft.id).where(InformationDraft.project_id == pid)
    ).scalar_one_or_none()
    if information_draft_id is None:
        information_draft_id = _uid("information_draft", project_name)
        _insert(
            session,
            InformationDraft,
            [
                {
                    "id": information_draft_id,
                    "project_id": pid,
                    "created_at": now,
                    **info_values,
                }
            ],
        )
    else:
        session.execute(
            update(InformationDraft)
            .where(InformationDraft.id == information_draft_id)
            .values(**info_values)
        )

    # --- 6. 基线节点 + 基线行 + 条目 -------------------------------------
    baseline_id = _uid("baseline_version", project_name)
    ruleset_version = "审核规则 v5（9 阻断 · 14 提醒）"
    change_reason = "屿见身体乳进入批产前，固化本版事实、场景与表达边界。"
    baseline_payload = {
        "projectId": str(pid),
        "briefVersionId": str(version_id),
        "rulesetVersion": ruleset_version,
        "changeReason": change_reason,
        "supersedesId": None,
        "items": [
            {
                "itemKind": item["kind"],
                "refLabel": item["ref_label"],
                "snapshot": item["snapshot"],
                "inclusion": item["inclusion"],
            }
            for item in _yujian_baseline_items(project_name)
        ],
    }
    _insert(
        session,
        VersionNode,
        [
            {
                "id": baseline_id,
                "entity_type": ENTITY_TYPE_BASELINE,
                "entity_id": pid,
                "project_id": pid,
                "revision_no": 1,
                "schema_version": 1,
                "payload_hash": canonical_payload_hash(baseline_payload),
                "created_at": now + timedelta(hours=6),
                "created_by": admin_id,
            }
        ],
    )
    _insert(
        session,
        ProductionBaseline,
        [
            {
                "id": baseline_id,
                "project_id": pid,
                "brief_version_id": version_id,
                "ruleset_version": ruleset_version,
                "snapshot": {
                    "schemaVersion": 1,
                    "rulesetVersion": ruleset_version,
                    "changeReason": change_reason,
                    "briefVersionId": str(version_id),
                    "readiness": {
                        "blockingResolved": True,
                        "hasProducibleScene": True,
                        "operatorConfirmed": True,
                    },
                    "counts": {
                        "claims": len(_YUJIAN_INFO_CLAIMS),
                        "conflicts": len(_YUJIAN_INFO_CONFLICTS),
                        "scenes": len(_YUJIAN_INFO_SCENES),
                    },
                    "boundaries": dict(_YUJIAN_INFO_BOUNDARIES),
                },
                "supersedes_id": None,
                "change_reason": change_reason,
                "schema_version": 1,
                "created_at": now + timedelta(hours=6),
                "created_by": admin_id,
            }
        ],
    )
    baseline_item_rows = []
    for item in _yujian_baseline_items(project_name):
        baseline_item_rows.append(
            {
                "id": _uid("baseline_item", f"{project_name}/{item['no']}"),
                "baseline_id": baseline_id,
                "item_no": item["no"],
                "item_kind": item["kind"],
                "inclusion": item["inclusion"],
                "ref_label": item["ref_label"],
                "knowledge_asset_id": None,
                "knowledge_version_id": None,
                "snapshot": item["snapshot"],
                "snapshot_text": item["snapshot_text"],
                "exclusion_reason": item.get("exclusion_reason"),
                "context_order": item["context_order"],
                "created_at": now + timedelta(hours=6),
            }
        )
    _insert(session, BaselineItem, baseline_item_rows)

    # --- 7. 八步批准（步骤 3 必须与基线同一事务且 APPROVED） --------------
    #
    # `step_approvals` 的 `subject_version_id` 只能指向 `version_nodes`。
    # 步骤 1 指向 Brief 版本；步骤 3~8 指向基线版本（本演示没有
    # plan/content 版本节点，基线的"固定版本"是后续步骤的依据）。
    # 步骤 2 的 `subject_kind=RESEARCH` 没有对应的 entity_type
    # （`baseline/service.py::_SUBJECT_KIND_ENTITY_TYPE` 显式映射为 None），
    # 因此指向 Brief 版本节点即可通过交叉核对。
    approval_specs = (
        (1, SUBJECT_KIND_BRIEF, version_id, APPROVAL_DECISION_APPROVED,
         "Brief 字段完整、必填项齐全，批准进入研究补充。", -96),
        (2, SUBJECT_KIND_RESEARCH, version_id, APPROVAL_DECISION_APPROVED,
         "研究与缺口清单已确认，批准进入信息审核。", -72),
        (3, SUBJECT_KIND_BASELINE, baseline_id, APPROVAL_DECISION_APPROVED,
         "三条完成条件全部满足，批准创建基线。", -48),
        (4, SUBJECT_KIND_BASELINE, baseline_id, APPROVAL_DECISION_APPROVED,
         "内容规划基于基线 v1 完成，批准进入文案生成。", -36),
        (5, SUBJECT_KIND_BASELINE, baseline_id, APPROVAL_DECISION_APPROVED,
         "首批文案自检通过，批准进入自检与审核。", -24),
        (6, SUBJECT_KIND_BASELINE, baseline_id, APPROVAL_DECISION_APPROVED,
         "人工审核通过，批准进入试产。", -12),
        (7, SUBJECT_KIND_BASELINE, baseline_id, APPROVAL_DECISION_APPROVED,
         "10 条试产内容数据达标，批准进入批产。", -6),
        (8, SUBJECT_KIND_BASELINE, baseline_id, APPROVAL_DECISION_REJECTED,
         "批产终检仍缺 3 条交付数据，暂不批准。", -2),
    )
    approval_rows = []
    for step_no, subject_kind, subject_version_id, decision, reason, hours in approval_specs:
        decided_at = now + timedelta(hours=hours)
        approval_rows.append(
            {
                "id": _uid("step_approval", f"{project_name}/{step_no}"),
                "project_id": pid,
                "step_no": step_no,
                "subject_kind": subject_kind,
                "subject_version_id": subject_version_id,
                "decision": decision,
                "decided_by": admin_id,
                "decided_at": decided_at,
                "reason": reason,
                "supersedes_id": None,
                "created_at": decided_at,
            }
        )
    _insert(session, StepApproval, approval_rows)

    # --- 8. 当前生效基线（`projects.active_baseline_id` 是唯一答案） -------
    session.execute(
        update(Project)
        .where(Project.id == pid)
        .values(active_baseline_id=baseline_id, updated_at=now)
    )

    return {
        "brief_id": brief_id,
        "draft_id": draft_id,
        "brief_version_id": version_id,
        "information_draft_id": information_draft_id,
        "baseline_id": baseline_id,
    }


def _seed_reference_files(
    session: Any,
    project_ids: dict[str, uuid.UUID],
    users: dict[str, uuid.UUID],
) -> dict[str, uuid.UUID]:
    """为屿见项目灌产品参考素材文件（`purpose=REFERENCE`）。

    参考素材走的是 `files` 的上传产物三件套（blob / file / link），
    但**不计入研究配额**（`QUOTA_PURPOSES` 只含 `RESEARCH`），
    也不参与解析 —— 它只是 `reference_assets.file_object_id` 的落点。
    """
    project_name = _P1_PROJECT
    pid = project_ids[project_name]
    file_ids: dict[str, uuid.UUID] = {}

    for file_name, byte_size, mime, uploader_email, hour_offset in _REFERENCE_FILES:
        digest = hashlib.sha256(f"{project_name}/{file_name}".encode()).hexdigest()
        blob_id = _uid("blob", f"{project_name}/{file_name}")
        file_id = _uid("file", f"{project_name}/{file_name}")
        created_at = _utc(hours=hour_offset)
        file_ids[file_name] = file_id
        _insert(
            session,
            Blob,
            [
                {
                    "id": blob_id,
                    "storage_backend": STORAGE_BACKEND_LOCAL,
                    "object_key": f"demo/{pid}/reference/{digest[:12]}/{file_name}",
                    "sha256": digest,
                    "byte_size": byte_size,
                    "mime_type": mime,
                    "scan_status": SCAN_STATUS_CLEAN,
                    "storage_state": STORAGE_STATE_ACTIVE,
                }
            ],
        )
        _insert(
            session,
            FileObject,
            [
                {
                    "id": file_id,
                    "blob_id": blob_id,
                    "original_name": file_name,
                    "visibility": VISIBILITY_PROJECT,
                    "project_id": pid,
                    "classification": CLASSIFICATION_SUPPORTED,
                    "uploaded_by": users[uploader_email],
                    "created_at": created_at,
                }
            ],
        )
        _insert(
            session,
            ProjectFileLink,
            [
                {
                    "id": _uid("link", f"{project_name}/{file_name}"),
                    "project_id": pid,
                    "file_id": file_id,
                    "purpose": PURPOSE_REFERENCE,
                    "effective": True,
                }
            ],
        )
    return file_ids


def _seed_plan_draft(
    session: Any,
    project_ids: dict[str, uuid.UUID],
    users: dict[str, uuid.UUID],
    baseline_id: uuid.UUID,
) -> dict[str, Any]:
    """给屿见补「可提交」的 trial-plan 草稿。

    提交校验的四项（`planning/service.py:813-907`）：
      1. 草稿存在；
      2. `state == 'EDITING'`；
      3. `total_quantity` 已设；
      4. 场景 / 形式两个维度的**启用项**比例合计都严格等于 1000‰。
    """
    project_name = _P1_PROJECT
    pid = project_ids[project_name]
    admin_id = users[ADMIN_EMAIL]
    now = _utc(hours=-30)

    values: dict[str, Any] = {
        "baseline_id": baseline_id,
        "state": DRAFT_STATE_EDITING,
        "total_quantity": _P1_PLAN_TOTAL_QUANTITY,
        "scene_items": [dict(item) for item in _P1_PLAN_SCENES],
        "form_items": [dict(item) for item in _P1_PLAN_FORMS],
        "pairings": [dict(item) for item in _P1_PAIRINGS],
        "schema_version": 1,
        "updated_at": now,
    }
    draft_id = session.execute(
        select(PlanDraft.id).where(PlanDraft.project_id == pid)
    ).scalar_one_or_none()
    if draft_id is None:
        draft_id = _uid("plan_draft", project_name)
        _insert(
            session,
            PlanDraft,
            [
                {
                    "id": draft_id,
                    "project_id": pid,
                    "created_by": admin_id,
                    "created_at": now,
                    "lock_version": 1,
                    **values,
                }
            ],
        )
    else:
        # **不写 `lock_version`**：保存路径每写一次都会 +1，而种子重复运行
        # 若也 +1，会把 demo 的 ETag 变成每次运行都不同的值；保持不动即可幂等。
        session.execute(
            update(PlanDraft).where(PlanDraft.id == draft_id).values(**values)
        )
    return {"plan_draft_id": draft_id, "total_quantity": _P1_PLAN_TOTAL_QUANTITY}


def _seed_billing(
    session: Any,
    project_ids: dict[str, uuid.UUID],
    users: dict[str, uuid.UUID],
) -> dict[str, uuid.UUID]:
    """补供应商价目、试产费用预估与项目预算账面。

    `pricing_versions` 是**全局**资源（不属于项目），但 `cost_estimates`
    与 `project_budgets` 都是项目级的 —— 因此价目只造一份，预估与预算
    只给屿见项目造。
    """
    project_name = _P1_PROJECT
    pid = project_ids[project_name]
    admin_id = users[ADMIN_EMAIL]
    now = _utc(hours=-24)

    provider_id = _uid("provider", _P1_PROVIDER_NAME)
    _insert(
        session,
        Provider,
        [
            {
                "id": provider_id,
                "name": _P1_PROVIDER_NAME,
                "capability_types": [
                    CAPABILITY_TEXT_STRUCTURED,
                    CAPABILITY_STATIC_IMAGE,
                ],
                "network_mode": NETWORK_LOCAL,
                "endpoint": "http://gateway.internal.invalid/v1",
                "secret_ref": None,
                "status": PROVIDER_ACTIVE,
                "policy_version": "v1",
                "created_at": now,
                "updated_at": now,
            }
        ],
    )

    pricing_id = _uid("pricing_version", _P1_PRICING_LABEL)
    _insert(
        session,
        PricingVersion,
        [
            {
                "id": pricing_id,
                "provider_id": provider_id,
                "model_profile_id": None,
                "state": PRICING_STATE_PUBLISHED,
                "currency": CURRENCY_CNY,
                "label": _P1_PRICING_LABEL,
                "unit_prices": dict(_P1_PRICING_UNIT_PRICES),
                "effective_from": _utc(days=-30),
                "note": "演示种子：仅供 trial-config 价目卡与费用预估使用。",
                "created_at": now,
            }
        ],
    )

    estimate_id = _uid("cost_estimate", f"{project_name}/TRIAL")
    _insert(
        session,
        CostEstimate,
        [
            {
                "id": estimate_id,
                "project_id": pid,
                "batch_kind": "TRIAL",
                "quantity": _P1_PLAN_TOTAL_QUANTITY,
                "pricing_version_id": pricing_id,
                "currency": CURRENCY_CNY,
                "state": ESTIMATE_STATE_DRAFT,
                "estimated_amount": _P1_ESTIMATE_TOTAL,
                "actual_amount": None,
                "variance_amount": None,
                "lines": [dict(line) for line in _P1_ESTIMATE_LINES],
                "created_by": admin_id,
                "created_at": now,
                "updated_at": now,
                "lock_version": 1,
            }
        ],
    )

    # 预算账面：upsert 只改限额 / 币种 / 设置人，**不碰 committed / reserved**
    # （那两列是预占与结算路径维护的，种子不应把它们清零）。
    budget_stmt = pg_insert(ProjectBudget.__table__).values(
        [
            {
                "project_id": pid,
                "currency": CURRENCY_CNY,
                "limit_amount": _P1_BUDGET_LIMIT,
                "committed_amount": Decimal("0"),
                "reserved_amount": Decimal("0"),
                "set_by": admin_id,
                "created_at": now,
                "updated_at": now,
            }
        ]
    )
    session.execute(
        budget_stmt.on_conflict_do_update(
            index_elements=[ProjectBudget.project_id],
            set_={
                "currency": budget_stmt.excluded.currency,
                "limit_amount": budget_stmt.excluded.limit_amount,
                "set_by": budget_stmt.excluded.set_by,
                "updated_at": budget_stmt.excluded.updated_at,
            },
        )
    )

    return {
        "provider_id": provider_id,
        "pricing_version_id": pricing_id,
        "cost_estimate_id": estimate_id,
    }


def _seed_reference_assets(
    session: Any,
    project_ids: dict[str, uuid.UUID],
    users: dict[str, uuid.UUID],
    reference_file_ids: dict[str, uuid.UUID],
) -> None:
    """给屿见登记两条 `reference_assets`（`state=ACTIVE`）。

    素材表**不可变**；这里只追加固定 id 的行。`only_usable` 的判据是
    "没有一条 RETIRED 行以它为 supersedes"（`media/repository.py:317-344`），
    两条 ACTIVE 且无后继 ⇒ 都会出现在列表里。
    """
    project_name = _P1_PROJECT
    pid = project_ids[project_name]
    now = _utc(hours=-20)
    rows = []
    for spec in _P1_REFERENCE_ASSETS:
        rows.append(
            {
                "id": _uid(
                    "reference_asset", f"{project_name}/{spec['file_name']}"
                ),
                "project_id": pid,
                "file_object_id": reference_file_ids[spec["file_name"]],
                "state": ASSET_STATE_ACTIVE,
                "product_model": spec["product_model"],
                "product_color": spec["product_color"],
                "product_part": spec["product_part"],
                "source": spec["source"],
                "note": spec["note"],
                "supersedes_asset_id": None,
                "created_at": now,
                "created_by": users[ADMIN_EMAIL],
            }
        )
    _insert(session, ReferenceAsset, rows)


def _seed_work_items(
    session: Any,
    project_ids: dict[str, uuid.UUID],
    users: dict[str, uuid.UUID],
) -> None:
    """灌待办。

    `dedupe_key` 在**未结**状态下唯一（部分唯一索引
    `uq_work_items_open_dedupe`）。本数据集每条 dedupe_key 都不同，
    因此不受影响；写成常量列表是为了将来加数据时不去踩这个约束。
    """
    rows: list[dict[str, Any]] = []
    for index, spec in enumerate(WORK_ITEMS):
        pid = project_ids[spec["project"]]
        # subject_id 无外键，但**必须指向真实存在的对象** ——
        # 悬空 subject 不会报错，只会在用户点开待办时 404。
        if spec["subject_type"] == "FILE":
            subject_id = _uid("file", f"{spec['project']}/{spec['subject_file']}")
        else:
            subject_id = pid
        assignee = spec["assignee"]
        rows.append(
            {
                "id": _uid("work_item", spec["key"]),
                "project_id": pid,
                "assignee_id": users[assignee] if assignee else None,
                "kind": spec["kind"],
                "subject_type": spec["subject_type"],
                "subject_id": subject_id,
                # 第 8 条演示"已结"与"取消"两种非 OPEN 状态。
                "state": (
                    WORK_ITEM_DONE
                    if index == 4
                    else WORK_ITEM_CANCELLED
                    if index == 5
                    else WORK_ITEM_OPEN
                ),
                "due_at": _utc(days=spec["due_offset_days"], hours=2),
                "dedupe_key": spec["dedupe_key"],
            }
        )
    _insert(session, WorkItem, rows)


def _seed_audit_events(
    session: Any,
    project_ids: dict[str, uuid.UUID],
    users: dict[str, uuid.UUID],
) -> None:
    """灌审计事件。

    `before_hash` / `after_hash` 两条 CHECK：长度必须为 64（或 NULL），
    且**必须同时有或同时无**。这里只造"状态变更"型事件（有对哈希），
    以及 NULL/NULL 的纯记录型事件 —— 两种都合法，且都能被页面渲染。
    """
    rows: list[dict[str, Any]] = []
    for spec in AUDIT_EVENTS:
        pid = project_ids[spec["project"]]
        actor_email = spec["actor_email"]
        # object_id 无外键，但必须指向真实存在的对象 —— 悬空引用不会报错，
        # 只会在页面上渲染出一个点不开的对象。
        if spec["object_type"] == "FILE":
            object_id = _uid("file", f"{spec['project']}/小红书_评论抓取_218条.xlsx")
        else:
            object_id = pid

        before = hashlib.sha256(f"{spec['key']}:before".encode()).hexdigest()
        after = hashlib.sha256(f"{spec['key']}:after".encode()).hexdigest()
        rows.append(
            {
                "id": _uid("audit", spec["key"]),
                "actor_kind": spec["actor_kind"],
                "actor_id": users[actor_email] if actor_email else None,
                "action": spec["action"],
                "object_type": spec["object_type"],
                "object_id": object_id,
                "project_id": pid,
                "before_hash": before if actor_email else None,
                "after_hash": after if actor_email else None,
                "reason": spec["reason"],
                # NOT NULL。用确定性值，便于按 request_id 反查。
                "request_id": f"seed-{spec['key']}",
                "created_at": _utc(hours=spec["hours_offset"]),
            }
        )
    _insert(session, AuditEvent, rows)


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------


def _verify(session: Any, project_ids: dict[str, uuid.UUID]) -> None:
    """三条不变量自检。失败即抛 SystemExit（退出码非零）。"""
    failures: list[str] = []

    # 1. parse_runs 的状态与计数一致。
    #
    # **判据分两类，因为 `derive_state` 只对终态成立**（见其 docstring：
    # 它回答的是"全解出来了 / 部分失败 / 失败"这种**结论**）：
    #
    #   · 终态（SUCCEEDED/PARTIAL/FAILED/CANCELLED）→ 必须等于 `derive_state`
    #   · 非终态（QUEUED/RUNNING）→ 计数是**进行中的快照**，没有结论可推导，
    #     改判 `finished_at IS NULL`（"还没跑完就不该有结束时间"，
    #     与 CHECK `terminal_has_finished_at` 互补的那一半）
    #
    # 第一版这里对全部行都断言 `derive_state` 相等，于是 `RUNNING`
    # 那一条报错 —— **是判据写宽了，不是数据错**。放宽成"跳过非终态"
    # 会留下一个无人看的空洞，因此这里换成对非终态真的成立的那个判据。
    for run_id, state, total, parsed, finished_at in session.execute(
        select(
            ParseRun.id,
            ParseRun.state,
            ParseRun.total_units,
            ParseRun.parsed_units,
            ParseRun.finished_at,
        )
    ):
        if state in PARSE_TERMINAL_STATES:
            expected = derive_state(total_units=total, parsed_units=parsed)
            if expected != state:
                failures.append(
                    f"parse_runs {run_id}: state={state} 但 derive_state 给出 "
                    f"{expected}（total={total}, parsed={parsed}）"
                )
        else:
            if finished_at is not None:
                failures.append(
                    f"parse_runs {run_id}: 非终态 {state} 却有 finished_at"
                    "（进行中的解析不应有结束时间）"
                )
            if total is not None and not (0 <= parsed <= total):
                failures.append(
                    f"parse_runs {run_id}: 非终态计数越界（total={total}, parsed={parsed}）"
                )

    # 2. 配额账实相符。
    repo = FileRepository(session)
    for pid in project_ids.values():
        booked = session.execute(
            select(QuotaAccount.effective_bytes).where(QuotaAccount.project_id == pid)
        ).scalar_one_or_none()
        recomputed = repo.compute_effective_bytes(pid)
        if booked != recomputed:
            failures.append(
                f"quota_accounts {pid}: 账面={booked} 重算={recomputed}（应为相等）"
            )

    # 3. 每项目恰好一名活跃 OWNER，且与 projects.owner_id 一致。
    for spec_name, pid in project_ids.items():
        owner_rows = session.execute(
            select(ProjectMember.user_id).where(
                ProjectMember.project_id == pid,
                ProjectMember.member_role == MEMBER_OWNER,
                ProjectMember.removed_at.is_(None),
            )
        ).scalars().all()
        if len(owner_rows) != 1:
            failures.append(
                f"project {spec_name!r}: 活跃 OWNER 有 {len(owner_rows)} 条（应为 1）"
            )
            continue
        project_owner = session.execute(
            select(Project.owner_id).where(Project.id == pid)
        ).scalar_one()
        if project_owner != owner_rows[0]:
            failures.append(
                f"project {spec_name!r}: projects.owner_id 与成员表 OWNER 不一致"
            )

    # 4. P1 演示链路完整（brief → 信息审核 → 基线 → 八步批准）。
    #    这条是**追加**的：前面三条原有断言未改动。
    _verify_p1_chain(session, project_ids, failures)

    # 5. P2 试产配置可提交（plan-draft / 价目 / 预估 / 预算 / 参考素材）。
    _verify_trial_config(session, project_ids, failures)

    if failures:
        raise SystemExit("自检失败：\n  - " + "\n  - ".join(failures))


def _verify_p1_chain(
    session: Any,
    project_ids: dict[str, uuid.UUID],
    failures: list[str],
) -> None:
    """P1 链路自检（只针对屿见项目）。

    判据尽量调用生产函数（`BRIEF_FIELDS` / `step3_readiness`），
    避免种子脚本自己再造一套"页面需要什么"的口径。
    """
    project_name = _P1_PROJECT
    pid = project_ids[project_name]

    brief = session.execute(
        select(Brief).where(Brief.project_id == pid)
    ).scalar_one_or_none()
    if brief is None:
        failures.append(f"p1 {project_name!r}: 缺少 briefs 行")
        return
    if brief.active_version_id is None:
        failures.append(f"p1 {project_name!r}: briefs.active_version_id 为空")
        return

    version = session.get(BriefVersion, brief.active_version_id)
    if version is None:
        failures.append(f"p1 {project_name!r}: 当前生效 Brief 版本行不存在")
        return

    expected_paths = {spec.path for spec in BRIEF_FIELDS}
    actual_paths = set(version.structured_fields or {})
    if actual_paths != expected_paths:
        failures.append(
            f"p1 {project_name!r}: structured_fields 字段集合与 BRIEF_FIELDS "
            f"不一致（缺 {sorted(expected_paths - actual_paths)}，"
            f"多 {sorted(actual_paths - expected_paths)}）"
        )

    claim_count = session.execute(
        select(func.count())
        .select_from(BriefClaim)
        .where(BriefClaim.brief_version_id == version.id)
    ).scalar_one()
    if claim_count == 0:
        failures.append(f"p1 {project_name!r}: Brief 版本没有 claims")

    draft = session.execute(
        select(BriefDraft).where(BriefDraft.brief_id == brief.id)
    ).scalar_one_or_none()
    if draft is None:
        failures.append(f"p1 {project_name!r}: 缺少 brief_drafts 行")
    else:
        if not draft.suggestions:
            failures.append(f"p1 {project_name!r}: draft.suggestions 为空")

    info = session.execute(
        select(InformationDraft).where(InformationDraft.project_id == pid)
    ).scalar_one_or_none()
    if info is None:
        failures.append(f"p1 {project_name!r}: 缺少 information_drafts 行")
    else:
        if not (info.claims and info.conflicts and info.scenes and info.research_selection):
            failures.append(f"p1 {project_name!r}: 信息审核有一块为空")
        if not info.boundaries:
            failures.append(f"p1 {project_name!r}: boundaries 为空")
        readiness = step3_readiness(
            claims=tuple(info.claims or ()),
            conflicts=tuple(info.conflicts or ()),
            scenes=tuple(info.scenes or ()),
            operator_confirmed=bool(info.operator_confirmed),
        )
        if not readiness.ready:
            failures.append(
                f"p1 {project_name!r}: readiness.ready=false（"
                + "；".join(
                    condition.reason for condition in readiness.unsatisfied()
                )
                + "）"
            )

    baseline_ids = session.execute(
        select(ProductionBaseline.id).where(ProductionBaseline.project_id == pid)
    ).scalars().all()
    if not baseline_ids:
        failures.append(f"p1 {project_name!r}: 没有 production_baselines 行")
    else:
        item_count = session.execute(
            select(func.count())
            .select_from(BaselineItem)
            .where(BaselineItem.baseline_id.in_(baseline_ids))
        ).scalar_one()
        if item_count == 0:
            failures.append(f"p1 {project_name!r}: 基线没有 baseline_items")

    approvals = session.execute(
        select(StepApproval).where(StepApproval.project_id == pid)
    ).scalars().all()
    by_step = {row.step_no: row for row in approvals}
    if set(by_step) != set(range(1, 9)):
        failures.append(
            f"p1 {project_name!r}: 八步批准缺失（有步骤 {sorted(by_step)}）"
        )
    first_four = [by_step.get(step) for step in range(1, 5)]
    if any(row is None or row.decision != APPROVAL_DECISION_APPROVED for row in first_four):
        failures.append(f"p1 {project_name!r}: 前 4 个关口不是全部 APPROVED")


def _verify_trial_config(
    session: Any,
    project_ids: dict[str, uuid.UUID],
    failures: list[str],
) -> None:
    """P2 试产配置自检（plan-draft 可提交 + 价目/预估/预算/参考素材）。

    两维比例用生产的 `permille_rules.validate_total` 复核 —— 不在种子里
    再写一遍"启用项求和"的判据（`planning/service.py:956-978` 用的是同一个函数）。
    """
    project_name = _P1_PROJECT
    pid = project_ids[project_name]

    draft = session.execute(
        select(PlanDraft).where(PlanDraft.project_id == pid)
    ).scalar_one_or_none()
    if draft is None:
        failures.append(f"p2 {project_name!r}: 缺少 plan_drafts 行")
    else:
        expected_baseline = _uid("baseline_version", project_name)
        if draft.baseline_id != expected_baseline:
            failures.append(
                f"p2 {project_name!r}: plan_drafts.baseline_id 未绑定生效基线"
            )
        if draft.state != DRAFT_STATE_EDITING:
            failures.append(
                f"p2 {project_name!r}: plan_drafts.state={draft.state!r}，"
                "提交要求 EDITING"
            )
        if draft.total_quantity is None:
            failures.append(f"p2 {project_name!r}: total_quantity 为空")
        if not draft.scene_items:
            failures.append(f"p2 {project_name!r}: scene_items 为空")
        if not draft.form_items:
            failures.append(f"p2 {project_name!r}: form_items 为空")
        if not draft.pairings:
            failures.append(f"p2 {project_name!r}: pairings 为空")

        for field, rows in (
            ("sceneItems", draft.scene_items or ()),
            ("formItems", draft.form_items or ()),
        ):
            active = [
                AllocationItem(
                    key=str(row.get("key") or ""),
                    weight_permille=int(row.get("weightPermille") or 0),
                    sort_order=int(row.get("sortOrder") or 0),
                )
                for row in rows
                if isinstance(row, dict)
                and row.get("state") == ITEM_STATE_ENABLED
            ]
            total, _deviation = permille_rules.validate_total(active)
            if total != PERMILLE_TOTAL:
                failures.append(
                    f"p2 {project_name!r}: {field} 启用项比例合计 {total}‰"
                    f"（提交要求 {PERMILLE_TOTAL}‰）"
                )

    pricing_count = session.execute(
        select(func.count()).select_from(PricingVersion)
    ).scalar_one()
    if pricing_count == 0:
        failures.append("p2: pricing_versions 为空")

    estimate_count = session.execute(
        select(func.count())
        .select_from(CostEstimate)
        .where(CostEstimate.project_id == pid)
    ).scalar_one()
    if estimate_count == 0:
        failures.append(f"p2 {project_name!r}: cost_estimates 为空")

    budget_exists = session.execute(
        select(ProjectBudget.project_id).where(ProjectBudget.project_id == pid)
    ).scalar_one_or_none()
    if budget_exists is None:
        failures.append(f"p2 {project_name!r}: 缺少 project_budgets 行")

    reference_count = session.execute(
        select(func.count())
        .select_from(ReferenceAsset)
        .where(ReferenceAsset.project_id == pid)
    ).scalar_one()
    if reference_count == 0:
        failures.append(f"p2 {project_name!r}: reference_assets 为空")


# ---------------------------------------------------------------------------
# 汇总输出
# ---------------------------------------------------------------------------

_SUMMARY_TABLES: Final[tuple[tuple[str, Any], ...]] = (
    ("users", User),
    ("brands", Brand),
    ("products", Product),
    ("projects", Project),
    ("project_members", ProjectMember),
    ("quota_accounts", QuotaAccount),
    ("blobs", Blob),
    ("file_objects", FileObject),
    ("project_file_links", ProjectFileLink),
    ("parse_runs", ParseRun),
    ("source_segments", SourceSegment),
    ("version_nodes", VersionNode),
    ("briefs", Brief),
    ("brief_drafts", BriefDraft),
    ("brief_versions", BriefVersion),
    ("brief_claims", BriefClaim),
    ("brief_issues", BriefIssue),
    ("information_drafts", InformationDraft),
    ("production_baselines", ProductionBaseline),
    ("baseline_items", BaselineItem),
    ("step_approvals", StepApproval),
    ("plan_drafts", PlanDraft),
    ("providers", Provider),
    ("pricing_versions", PricingVersion),
    ("cost_estimates", CostEstimate),
    ("project_budgets", ProjectBudget),
    ("reference_assets", ReferenceAsset),
    ("work_items", WorkItem),
    ("audit_events", AuditEvent),
)


def _print_summary(session: Any) -> None:
    print("\n=== rednote_dev 种子数据汇总 ===")
    for table_name, model in _SUMMARY_TABLES:
        count = session.execute(
            select(func.count()).select_from(model.__table__)
        ).scalar_one()
        print(f"  {table_name:22s} {count:>5d}")
    quota = session.execute(
        select(
            func.count(),
            func.coalesce(func.sum(QuotaAccount.effective_bytes), 0),
            func.coalesce(func.sum(QuotaAccount.limit_bytes), 0),
        ).select_from(QuotaAccount)
    ).one()
    print(
        f"\n  配额：{quota[0]} 个项目，已用 {int(quota[1]) / 1_048_576:.1f} MB"
        f" / 上限 {int(quota[2]) / 1_048_576:.1f} MB"
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="G1 演示种子数据（仅 rednote_dev）")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="先 TRUNCATE 种子表再灌（CASCADE 会连带清空引用表，仅演示库使用）",
    )
    args = parser.parse_args()

    settings = Settings()
    db_name = _assert_seedable(settings)
    print(f"目标库：{db_name}（environment={settings.environment}）  守卫通过")

    with session_scope(settings) as session:
        if args.reset:
            _reset(session)

        users = _seed_users(session)
        _seed_dictionary(session, PROJECTS)
        project_ids = _seed_projects(session, PROJECTS, users)
        _seed_research_files(session, project_ids, users)
        _seed_quota_accounts(session, project_ids)
        _seed_parsing(session, project_ids)
        brief_source = _seed_brief_source(session, project_ids, users)
        chain = _seed_p1_chain(session, project_ids, users, brief_source)
        _seed_plan_draft(session, project_ids, users, chain["baseline_id"])
        _seed_billing(session, project_ids, users)
        reference_file_ids = _seed_reference_files(session, project_ids, users)
        _seed_reference_assets(session, project_ids, users, reference_file_ids)
        _seed_work_items(session, project_ids, users)
        _seed_audit_events(session, project_ids, users)

        session.flush()
        _verify(session, project_ids)
        _print_summary(session)

    print("\n自检全部通过：解析状态 / 配额账实 / 负责人一致性 / P1 链路 / P2 试产配置")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
