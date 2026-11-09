#!/usr/bin/env python
"""WP22 证据采集脚本。

产出的文件（落在 `evidence/m1/`）：

| 文件 | 内容 |
|---|---|
| `wp22-baseline.log` | 4 张表结构 / 9 个触发器 / 2 条部分唯一索引 / 6 条延迟外键，+ **二十九处活性验证** |

## 为什么这个包要 29 处活性验证（本仓最多）

本包的实现里，**绝大多数防线都是"做不到也不报错"的形状** ——
这是本包最危险的特征，也是"结构核对"单独用不够用的原因：

| 防线 | 失效后的症状 | 会不会报错 |
|---|---|---|
| 部分唯一索引漏写 `WHERE` | 退化成"每项目只能有一条基线" → 与 AC-08（修改创建新版本）直接冲突 | **不会**（第二次创建时才炸，中间全绿） |
| `fk_*_supersedes_id` 忘了 `DEFERRABLE` | 替代链的写入顺序一变就失败 | 有时会，但多数时候是**测试与线上行为分叉** |
| `trg_*_no_blocking` 没建 | 有未处置阻断项也能建基线（AC-09 失效） | **不会** |
| `trg_*_approval_required` 没建 | 未批准的基线能落库（OP-151 失效） | **不会** |
| `ck_baseline_items_snapshot_not_empty` 没建 | 基线只存 `ref` 不存值 → 知识更新后基线被**静默污染**（R1） | **不会** |
| `trg_baseline_items_cross_brand` 的 `IS DISTINCT FROM` 写成 `<>` | NULL 比较返回 NULL → `IF` 视为 false → 跨品牌引用**静默通过** | **不会** |
| `trg_*_immutable` 没建 | 历史基线可被原地改写（AC-08 失效） | **不会** |

因此每一条防线都必须**拿本该被拦的输入去试写**，并且**配一个"本该通过"的对照组**
—— 一个"见谁都拒"的 CHECK/触发器能让所有"被拦"判据全绿，而系统已经完全不可用。

## 二十九处活性验证（分五组）

**第 1 组 · 基线的创建与批准（1~4）**
1. 合法基线（节点 + 基线行 + 同事务批准）**必须通过** —— 后面所有"被拦"的对照基准。
2. 缺批准的基线**必须被拦**（`trg_production_baselines_approval_required`，23514）。
3. **AC-08 的正面**：`supersedes_id` 指向旧基线的新基线**必须能建** —— 这一条
   与第 4 条配对，专门证明 `uq_production_baselines_project_root` 的
   `WHERE supersedes_id IS NULL` 没有退化。
4. **AC-08 的反面**：同一项目两条链根**必须被拦**（`uq_production_baselines_project_root`，23505）。
   若第 3 条漏测，一个丢掉 `WHERE` 的实现能让第 1 条全绿、第 3 条失败 ——
   但若**只有**第 3 条，一个连索引都没有的实现也能全绿。两条必须成对。

**第 2 组 · 不可变保护（5~6）**
5. `production_baselines` / `baseline_items` / `step_approvals` 三张不可变表的
   `UPDATE` **必须被拦**（`rednote_reject_mutation`，23001）。
6. **对照**：`information_drafts` 的 `UPDATE` **必须通过** —— 它是**工作区**，
   运营在里面反复改是正常用法。一个"给所有表都加锁"的实现能让第 5 条全绿，
   而它会让步骤 3 的审核工作完全做不了。

**第 3 组 · OP-88 节点一致性（7~10）**
7. **正向判据 1**：`entity_type` 不是 `BASELINE` 的节点**必须被拦**。
8. **正向判据 3**：`entity_id` ≠ `project_id` 的节点**必须被拦**
   （抓到"给 A 项目注册了一个声称属于 B 项目的基线节点"）。
9. **反向判据**：`BASELINE` 节点没有配对的 `production_baselines` 行
   **必须被拦**（`trg_version_nodes_baseline_paired`）。
10. **收窄对照**：`entity_id` 指向**不存在**的项目时**不该被拦** ——
    该触发器刻意收窄为"只对真实存在的项目强制"。漏测这一条会让
    "把收窄删掉"的实现全绿，而那会让任何先建节点后建项目的合法流程失败。

**第 4 组 · 引用范围（11~17）**
11. `scope='BRAND'` 的知识属于**别的品牌** → **必须被拦**（AC-24）。
12. **对照**：同品牌 → **必须通过**。
13. `scope='PRODUCT'` 的知识属于**别的产品** → **必须被拦**。
14. `scope='PROJECT'` 的知识属于**别的项目** → **必须被拦**。
15. **对照**：`scope='GLOBAL'` → **必须通过**（GLOBAL 放行不是放水，
    因为 `FACT` 类别被 WP20 的 CHECK 禁止为 GLOBAL）。
16. **资产范围**：`knowledge_version_id` 指向**别的资产**的版本 → **必须被拦**
    （两条 FK 各自只保证"存在"，**没有东西保证"这一版属于这个资产"**）。
17. **对照**：版本属于本资产 → **必须通过**。

**第 5 组 · 阻断项、批准链、行级 CHECK（18~29）**
18. 草稿有**未处置的阻断项** → 不得建基线（AC-09）。
19. **对照**：阻断项已 `SETTLED` → **必须通过**。
20. **对照**：`blocking=false` 的未处置项 → **必须通过**（它不阻断）。
21. 批准**已被取代** → 不构成依据，**必须被拦**（否则"先批后撤"形同没撤）。
22. 同一版两条链根批准 → **必须被拦**（`uq_step_approvals_root` 的 `WHERE` 在位）。
23. **先拒后批**（第二条带 `supersedes_id`）→ **必须通过**。
24. `inclusion='EXCLUDED'` 但没有 `exclusion_reason` → **必须被拦**。
25. **空快照**（`snapshot = '{}'`）→ **必须被拦** —— 这是 R1 的**唯一**结构防线：
    空快照 = 只有引用没有值 = 知识更新后基线被静默污染的形状。
26. `step_no=9` 越界 → **必须被拦**。
27. 一项目两份草稿 → **必须被拦**。
28. `baseline_items` 的 `UPDATE` → **必须被拦**。
29. `step_approvals` 的 `UPDATE` → **必须被拦**。

## 探针自身踩过的两处坑（写在这里，避免重复踩）

1. **`knowledge_versions.id` 也 FK `version_nodes.id`**（WP20 的设计）。
   因此造一条知识版本时**必须先注册一个 `KNOWLEDGE` 节点**，否则撞
   `fk_knowledge_versions_id_version_nodes`（23503）—— 而且这个错误
   **长得像"资产范围判据误报"**：报出来的消息是"版本不存在"，
   会让人误以为业务判据错了。本脚本第一次跑就在 16/17 两处撞上它，
   把"版本属本资产必须通过"判成了 FAIL。
2. **一条语句报错会把整个事务打成 aborted**（后续语句返回 `25P02`）。
   因此"被拦"与"对照通过"这两类探针**必须各自独立事务** ——
   与 WP23 同款（那条坑记在 WP23 脚本里，这里是同源）。

## 跨包回归项（本包最容易误伤别人的地方）

- 共用的不可变触发器函数 `rednote_reject_mutation()` 必须仍在
  （本包 `downgrade` 刻意不删它 —— 所有不可变表共用）；
- `version_nodes` 表**不得**被本包 downgrade 删除（本包只删挂在它上面的
  1 个触发器 `trg_version_nodes_baseline_paired`）；
- `version_nodes` 上**四个包**的 `*_paired` 触发器必须共存
  （`brief` / `knowledge` / `prompt` / `baseline`）—— 它们各自被 `WHEN`
  收窄到自己的 `entity_type`，删错一个会让对应包的 OP-88 反向判据失效；
- `briefs` / `brief_versions` / `knowledge_assets` / `prompt_templates`
  必须存活（本包不碰别人的表）；
- 本包 4 个端点必须都在已导出的契约里（"写了端点但没进契约"是静默的）。

## 探针数据的处置

每条验证跑在**独立连接**里，用 `SET CONSTRAINTS ALL IMMEDIATE` 强制触发
延迟判据拿到结论，最后**无条件 rollback** → **零残留**。理由：
`production_baselines` / `baseline_items` / `step_approvals` 都不可变
（触发器拦 DELETE），一旦提交就删不掉（WP18 的 ISS-63 已实测）。

**为什么用 `SET CONSTRAINTS ALL IMMEDIATE` 而不是 `trans.commit()`**：
`commit()` 之后再 `rollback()` 只能回滚"空事务"，数据已落库 → 污染证据库。
强制校验把"提交时才跑"的延迟触发器提前到当前语句执行，事务仍未提交，
因此 rollback 之后一行不留。

**本脚本用在哪个库**：默认 `rednote_verify_wp22`（收口时新建的干净库，
迁移从 bootstrap 一路跑到 head 建出全部结构）。用它的理由：
分支测试库里的表是**测试夹具 create_all 建的**，不是迁移建的 ——
在那种库上取证据，等于在验证夹具而不是验证迁移。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "services" / "backend"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m1"

# 期望条数（与内联脚本里的 EXPECTED_CHECKS 保持同值）。
# 两处都要：内联脚本用它写进 RESULT 行，外层用它判定成功与否（ISS-43）。
EXPECTED_CHECKS: int = 74

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

INSPECT_SCRIPT = r"""
# 内联脚本：只允许单行注释，且不能出现三引号（三引号会提前终止外层包裹，见 ISS-45）。

import json
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

# 期望产生的核对条数（ISS-43：崩溃伪装成"全过"的防线）。
EXPECTED_CHECKS = 74

DB = "postgresql+psycopg://rednote@localhost:5432/rednote_verify_wp22"
engine = create_engine(DB)

CONTRACT_PATH = Path(
    "/Users/<user>/内容运营AI Native/rednote-production/contracts/openapi.json"
)

# 本包引入的迁移版本。判据用"不早于"而不是"恰好等于"（ISS-44：
# 硬编码"恰好停在某个版本"会让每个新工作包把历史证据集体打红）。
REVISION_INTRODUCED = "185ec01bed91"

# 本包 4 张表的期望列数。
# ⚠️ 这几个数字按 information_schema **实测**填写，不凭印象
# （WP23 的本脚本前身在此错了 3 处；本包实测为 13/10/13/11 = 47）。
TABLE_COLUMNS = (
    ("information_drafts", 13),
    ("production_baselines", 10),
    ("baseline_items", 13),
    ("step_approvals", 11),
)

# 本包 9 个触发器（3 个不可变 + 6 个跨表一致性）。
BASELINE_TRIGGERS = (
    "trg_production_baselines_immutable",
    "trg_baseline_items_immutable",
    "trg_step_approvals_immutable",
    "trg_production_baselines_node_consistency",
    "trg_version_nodes_baseline_paired",
    "trg_baseline_items_cross_brand",
    "trg_baseline_items_asset_scope",
    "trg_production_baselines_approval_required",
    "trg_production_baselines_no_blocking",
)

# 本包 6 个一致性函数（**不含**共用的 rednote_reject_mutation）。
BASELINE_FUNCTIONS = (
    "rednote_check_baseline_version_consistency",
    "rednote_check_baseline_node_paired",
    "rednote_check_baseline_item_scope",
    "rednote_check_baseline_item_asset_scope",
    "rednote_check_baseline_approval",
    "rednote_check_baseline_no_blocking",
)

# 本包 6 条延迟外键（逐条点名核对，不是"数个数"——数个数会把
# 3 条立即检查的 FK 混进来，而它们必须**不是**延迟的）。
DEFERRED_FKS = (
    "fk_information_drafts_brief_version_id_version_nodes",
    "fk_production_baselines_id_version_nodes",
    "fk_production_baselines_brief_version_id_version_nodes",
    "fk_production_baselines_supersedes_id_production_baselines",
    "fk_step_approvals_subject_version_id_version_nodes",
    "fk_step_approvals_supersedes_id_step_approvals",
)

# baseline_items 的 3 条 FK 必须**立即检查**（与上面 6 条对照）。
# 理由是它引用的父行都由同事务的前序步骤写入，不存在需要推迟的顺序问题
# （与 WP20 的表不同：那里节点行与 asset 行交错）。
IMMEDIATE_FKS = (
    "fk_baseline_items_baseline_id_production_baselines",
    "fk_baseline_items_knowledge_asset_id_knowledge_assets",
    "fk_baseline_items_knowledge_version_id_knowledge_versions",
)

# 本包 4 张表（零残留判据用）。
OWN_TABLES = (
    "information_drafts",
    "production_baselines",
    "baseline_items",
    "step_approvals",
)

# 两条**部分唯一索引** —— 本包最容易被 autogenerate 悄悄毁掉的东西。
# ⚠️ 部分唯一索引在 `pg_constraint` 里**根本不出现**，核对只能查 `pg_indexes`。
PARTIAL_UNIQUE_INDEXES = (
    "uq_production_baselines_project_root",
    "uq_step_approvals_root",
)

# 本包自建的 6 个一致性触发器在 version_nodes 上与另三个包**同表共存**。
# 判据：那三个必须还在（本包 downgrade 不能误删别人的触发器）。
CROSS_PACK_NODE_TRIGGERS = (
    "trg_version_nodes_brief_paired",
    "trg_version_nodes_knowledge_paired",
    "trg_version_nodes_prompt_paired",
    "trg_version_nodes_immutable",
)

# 本包不碰、但必须存活的别人家的表。
CROSS_PACK_TABLES = (
    "briefs",
    "brief_versions",
    "knowledge_assets",
    "knowledge_versions",
    "prompt_templates",
    "version_nodes",
)

# 本包 4 个端点。
EXPECTED_PATHS = (
    "/api/v1/projects/{projectId}/baselines",
    "/api/v1/projects/{projectId}/baselines/{baselineId}",
    "/api/v1/projects/{projectId}/information-draft",
    "/api/v1/projects/{projectId}/step-approvals",
)


def _revision_is_at_least(current, introduced):
    # 当前 head 是否**不早于** introduced —— 沿真实迁移链判断，不是猜。
    #
    # **为什么不能写 `== EXPECTED_REVISION`**（ISS-44）：那会让每个新工作包
    # 把前面所有包的历史证据集体打红，因为校验的其实是"迁移停在某个特定
    # 版本"，而真正该校验的是"**该包引入的表/约束还在**"（由本脚本后面的
    # 核对项保证）。
    #
    # **为什么也不能用 `len(current) >= len(introduced)`**（ISS-115，2026-09-27
    # 收口轮实测）：本项目全部 16 个 revision id **都是 12 位十六进制**，
    # 于是"长度不短于"在 `current != introduced` 时**恒为 True** ——
    # 那是把等式判据换成了**恒绿判据**，比原缺陷更隐蔽：它永不失败，
    # 因此也永不报警（判据纪律第 7/9 条：判据必须能区分两种实现）。
    # 本项目**存量 12 个脚本全是这个写法**，全部靠它"通过"。
    #
    # 正确做法：拿到**真实迁移链**，按位置比较。`walk_revisions(base=..., head=...)`
    # 的返回值是 head→base 降序，因此 index 小 = 新。
    # ⚠️ 该调用会 import 每个迁移模块（`from rednote.modules...`），
    # 故内联段必须在 `PYTHONPATH=<repo>/services/backend/src` 下运行。
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo = Path.cwd().resolve()
    while repo != repo.parent and not (repo / "migrations" / "alembic.ini").exists():
        repo = repo.parent
    cfg = Config(str(repo / "migrations" / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo / "migrations"))
    chain = [
        r.revision
        for r in ScriptDirectory.from_config(cfg).walk_revisions(
            base="base", head="heads"
        )
    ]
    # 认不出来的 revision（拼错/已删除）一律判否 —— fail-closed。
    if current not in chain or introduced not in chain:
        return False
    return chain.index(current) <= chain.index(introduced)


def emit(ok, label, detail):
    print(f"CHECK {'OK  ' if ok else 'FAIL'} {label}: {detail}")


def sqlstate(exc):
    return getattr(exc.orig, "sqlstate", None)


def constraint_name(exc):
    return getattr(getattr(exc.orig, "diag", None), "constraint_name", None)


# ---------------------------------------------------------------------------
# 探针的公共前置
# ---------------------------------------------------------------------------

def _seed_tx(conn, tag="A"):
    # 造最小可用的 brand / product / project / user。
    # ⚠️ 列名与非空集合全部按 `information_schema` 实测，不要凭印象写
    # （WP18 一次踩中 5 处，WP20 先后踩了 4 处列名、5 处非空）：
    #   · users    : 没有 email，是 (issuer, subject) 唯一
    #   · projects : 没有 code/created_by；brand_id / product_id / owner_id /
    #                target_delivery_count / due_date 都是 NOT NULL
    #   · brands   : 没有 code；products 没有 code，是 model_code
    pid = uuid.uuid4()
    uid = uuid.uuid4()
    brand = uuid.uuid4()
    prod = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, display_name, role, status)"
            " VALUES (:u, :iss, :sub, :n, 'ADMIN', 'ACTIVE')"
        ),
        {"u": str(uid), "iss": "wp22-probe", "sub": f"probe-{uid}", "n": "WP22 探针"},
    )
    conn.execute(
        text("INSERT INTO brands (id, name, status) VALUES (:b, :n, 'ACTIVE')"),
        {"b": str(brand), "n": f"WP22 探针品牌{tag} {str(brand)[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO products (id, brand_id, name, model_code, status)"
            " VALUES (:p, :b, :n, :c, 'ACTIVE')"
        ),
        {
            "p": str(prod), "b": str(brand),
            "n": f"WP22 探针产品{tag} {str(prod)[:8]}", "c": f"WP22-{str(prod)[:8]}",
        },
    )
    conn.execute(
        text(
            "INSERT INTO projects"
            " (id, project_code, name, brand_id, product_id, owner_id, phase,"
            "  target_delivery_count, due_date, timezone)"
            " VALUES (:p, :c, :n, :b, :pr, :u, 'PREPARATION', 1,"
            "         (now() + interval '30 days')::date, 'Asia/Shanghai')"
        ),
        {
            "p": str(pid), "c": f"WP22P{str(pid)[:8]}", "n": f"WP22 探针项目{tag}",
            "b": str(brand), "pr": str(prod), "u": str(uid),
        },
    )
    return pid, uid, brand, prod


def _node_tx(conn, node_id, entity_type, entity_id, pid, uid, revision_no=2):
    # `revision_no` 可传：同一 (entity_type, entity_id) 下**多个探针节点必须用
    # 不同修订号**（`uq_version_nodes_entity_type_entity_id_revision_no` 会拦重复）。
    conn.execute(
        text(
            "INSERT INTO version_nodes"
            " (id, entity_type, entity_id, revision_no, project_id,"
            "  payload_hash, schema_version, created_by)"
            " VALUES (:i, :et, :eid, :rev, :p, :h, 1, :u)"
        ),
        {
            "i": str(node_id), "et": entity_type, "eid": str(entity_id),
            "rev": revision_no, "p": str(pid), "h": "a" * 64, "u": str(uid),
        },
    )


def _baseline_tx(conn, bid, pid, *, supersedes=None, reason="探针创建"):
    conn.execute(
        text(
            "INSERT INTO production_baselines"
            " (id, project_id, ruleset_version, snapshot, supersedes_id,"
            "  change_reason, schema_version)"
            " VALUES (:i, :p, '审核规则 v5', cast(:s as jsonb), :sup, :cr, 1)"
        ),
        {
            "i": str(bid), "p": str(pid), "s": json.dumps({"approver": "探针"}),
            "sup": str(supersedes) if supersedes else None, "cr": reason,
        },
    )


def _approval_tx(conn, pid, subject_version_id, uid, *, decision="APPROVED",
                 supersedes=None, step_no=3, subject_kind="BASELINE"):
    aid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO step_approvals"
            " (id, project_id, step_no, subject_kind, subject_version_id,"
            "  decision, decided_by, reason, supersedes_id)"
            " VALUES (:i, :p, :s, :k, :v, :d, :u, '探针批准', :sup)"
        ),
        {
            "i": str(aid), "p": str(pid), "s": step_no, "k": subject_kind,
            "v": str(subject_version_id), "d": decision, "u": str(uid),
            "sup": str(supersedes) if supersedes else None,
        },
    )
    return aid


def _asset_tx(conn, uid, *, category="SCENE", scope="GLOBAL", brand=None,
              product=None, project=None, title="WP22 探针知识"):
    # category 用 SCENE 而不是 FACT：WP20 的
    # `ck_knowledge_assets_fact_requires_concrete_scope` 禁止 FACT 为 GLOBAL，
    # 用 FACT 会让"GLOBAL 放行"那条对照自己撞 CHECK。
    aid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO knowledge_assets"
            " (id, category, title, scope, brand_id, product_id, project_id, created_by)"
            " VALUES (:i, :c, :t, :s, :b, :p, :j, :u)"
        ),
        {
            "i": str(aid), "c": category, "t": title, "s": scope,
            "b": str(brand) if brand else None,
            "p": str(product) if product else None,
            "j": str(project) if project else None, "u": str(uid),
        },
    )
    return aid


def _knowledge_version_tx(conn, version_id, asset_id, pid, uid, revision_no=2):
    # ⚠️ `knowledge_versions.id` **也 FK `version_nodes.id`**（WP20 的设计）。
    # 因此必须先注册一个 KNOWLEDGE 节点，否则撞
    # `fk_knowledge_versions_id_version_nodes`（23503）—— 而这个错误
    # **长得像"资产范围判据误报"**：消息说"版本不存在"，会让人以为业务判据错了。
    _node_tx(conn, version_id, "KNOWLEDGE", asset_id, pid, uid, revision_no)
    conn.execute(
        text(
            "INSERT INTO knowledge_versions"
            " (id, asset_id, body, structured_payload, change_reason, created_by)"
            " VALUES (:i, :a, :b, cast(:pl as jsonb), :cr, :u)"
        ),
        {
            "i": str(version_id), "a": str(asset_id), "b": "探针正文",
            "pl": json.dumps({"k": 1}), "cr": "探针原因", "u": str(uid),
        },
    )
    return version_id


def _item_tx(conn, bid, item_no, *, asset_id=None, version_id=None,
             inclusion="INCLUDED", reason=None, snapshot=None, item_kind="SCENE"):
    snap = json.dumps(snapshot) if snapshot is not None else json.dumps({"v": 1})
    conn.execute(
        text(
            "INSERT INTO baseline_items"
            " (id, baseline_id, item_no, item_kind, inclusion, ref_label,"
            "  knowledge_asset_id, knowledge_version_id, snapshot, snapshot_text,"
            "  exclusion_reason, context_order)"
            " VALUES (:i, :b, :n, :k, :inc, :rl, :a, :v, cast(:s as jsonb),"
            "         '探针原文', :er, :co)"
        ),
        {
            "i": str(uuid.uuid4()), "b": str(bid), "n": item_no, "k": item_kind,
            "inc": inclusion, "rl": f"REF-{item_no}",
            "a": str(asset_id) if asset_id else None,
            "v": str(version_id) if version_id else None,
            "s": snap, "er": reason, "co": item_no,
        },
    )


def _draft_tx(conn, pid, conflicts=None):
    conn.execute(
        text(
            "INSERT INTO information_drafts (id, project_id, conflicts)"
            " VALUES (:i, :p, cast(:c as jsonb))"
        ),
        {
            "i": str(uuid.uuid4()), "p": str(pid),
            "c": json.dumps(conflicts or [], ensure_ascii=False),
        },
    )


def _expect_blocked(label, fn, want_sqlstate="23514", want_name=None):
    # 期望被拦。want_sqlstate 必须命中（判据不能只看"报错了"——
    # 连不上库也会报错，那会让整组判据假通过）。
    try:
        fn()
        emit(False, label, "竟然通过了 —— 防线失效")
    except DBAPIError as exc:
        ss = sqlstate(exc)
        nm = constraint_name(exc)
        if want_name is not None:
            ok = ss == want_sqlstate and nm == want_name
            emit(ok, label, f"sqlstate={ss} constraint={nm} (期望 {want_name})")
        else:
            emit(
                ss == want_sqlstate,
                label,
                f"sqlstate={ss} constraint={nm} | {str(exc.orig)[:70]}",
            )


def _expect_ok(label, fn):
    try:
        fn()
        emit(True, label, "通过")
    except DBAPIError as exc:
        emit(False, label, f"被误拦 sqlstate={sqlstate(exc)} | {str(exc.orig)[:70]}")


# ---------------------------------------------------------------------------
# 结构核对
# ---------------------------------------------------------------------------
with engine.connect() as conn:
    for table, expected in TABLE_COLUMNS:
        actual = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_schema='public' AND table_name=:t"
            ),
            {"t": table},
        ).scalar_one()
        emit(actual == expected, f"{table} 列数", f"{actual} (期望 {expected})")

    version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    emit(
        _revision_is_at_least(version, REVISION_INTRODUCED),
        "alembic_version 不早于本包引入的版本",
        f"{version} (本包引入 {REVISION_INTRODUCED})",
    )

    for name in BASELINE_TRIGGERS:
        n = conn.execute(
            text("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname = :n"),
            {"n": name},
        ).scalar_one()
        emit(n == 1, f"触发器存在 {name}", n)

    # 一致性函数：**按名字逐个核对**，不是"数个数"—— 本包 6 个函数名
    # 里 `rednote_check_baseline_item_scope` 与
    # `rednote_check_baseline_item_asset_scope` 前缀相同，
    # 用 `LIKE 'rednote_check_baseline%'` 数只能证明"总数对"。
    missing = []
    for name in BASELINE_FUNCTIONS:
        n = conn.execute(
            text("SELECT count(*) FROM pg_proc WHERE proname = :n"), {"n": name}
        ).scalar_one()
        if n != 1:
            missing.append(name)
    emit(
        not missing,
        "6 个一致性函数按名逐个在位",
        f"缺失={missing}" if missing else "全在",
    )

    # `consistency_items()` 的长度是判据的一部分（ddl.py 自己的 docstring 说的）：
    # 数错一个的后果是 downgrade 漏删一个函数（残留对象会挡住后续迁移重建它）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_proc"
            " WHERE proname LIKE 'rednote_check_baseline%'"
        )
    ).scalar_one()
    emit(n == len(BASELINE_FUNCTIONS), "一致性函数总数", f"{n} (期望 {len(BASELINE_FUNCTIONS)})")

    # CHECK 总数（本包 4 表实测 31：drafts 10 + baselines 5 + items 11 + approvals 5）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conrelid::regclass::text = ANY(:ts) AND contype='c'"
        ),
        {"ts": list(OWN_TABLES)},
    ).scalar_one()
    emit(n == 31, "CHECK 约束总数", f"{n} (期望 31)")

    # UNIQUE 总数（3 条：drafts.project_id、items.(baseline,item_no)、
    # items.(baseline,knowledge_asset_id)）。**不含**两条部分唯一索引 ——
    # 它们在 pg_constraint 里不出现，单独查 pg_indexes。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conrelid::regclass::text = ANY(:ts) AND contype='u'"
        ),
        {"ts": list(OWN_TABLES)},
    ).scalar_one()
    emit(n == 3, "普通 UNIQUE 约束总数", f"{n} (期望 3)")

    # FK 总数（14 条）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conrelid::regclass::text = ANY(:ts) AND contype='f'"
        ),
        {"ts": list(OWN_TABLES)},
    ).scalar_one()
    emit(n == 14, "FK 约束总数", f"{n} (期望 14)")

    # 两条部分唯一索引的 `WHERE` 用 `indexdef` 逐字核对在位。
    # **不看 `pg_constraint`** —— 部分唯一索引在那里根本不出现，
    # 只查约束表的核对看不到这两条防线（本包迁移文件第二节实测确认）。
    for name in PARTIAL_UNIQUE_INDEXES:
        row = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes"
                " WHERE schemaname='public' AND indexname=:n"
            ),
            {"n": name},
        ).scalar_one_or_none()
        has_where = row is not None and "WHERE" in row and "supersedes_id IS NULL" in row
        emit(has_where, f"部分唯一索引 {name} 的 WHERE 在位", (row or "缺失")[:130])

    # 6 条延迟外键逐条点名。
    for name in DEFERRED_FKS:
        row = conn.execute(
            text(
                "SELECT condeferrable, condeferred FROM pg_constraint WHERE conname=:n"
            ),
            {"n": name},
        ).one_or_none()
        emit(
            row is not None and row.condeferrable and row.condeferred,
            f"外键 DEFERRABLE INITIALLY DEFERRED {name}",
            f"{row}" if row else "缺失",
        )

    # baseline_items 的 3 条 FK 必须**立即检查**（上一条的对照）。
    immediate_map = {}
    for name in IMMEDIATE_FKS:
        row = conn.execute(
            text(
                "SELECT condeferrable, condeferred FROM pg_constraint WHERE conname=:n"
            ),
            {"n": name},
        ).one_or_none()
        immediate_map[name] = row and (not row.condeferrable) and (not row.condeferred)
    emit(
        all(immediate_map.values()),
        "baseline_items 的 3 条 FK 是立即检查（与上 6 条对照）",
        json.dumps(immediate_map, ensure_ascii=False),
    )

    # 跨包：共用的不可变函数必须仍在（本包 downgrade 刻意不删它）。
    n = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname = 'rednote_reject_mutation'")
    ).scalar_one()
    emit(n == 1, "共用函数 rednote_reject_mutation 仍在", n)

    # 跨包：version_nodes 表必须仍在（本包只删挂在它上面的 1 个触发器）。
    n = conn.execute(
        text(
            "SELECT count(*) FROM information_schema.tables"
            " WHERE table_schema='public' AND table_name='version_nodes'"
        )
    ).scalar_one()
    emit(n == 1, "共用表 version_nodes 仍在", n)

    # 跨包：另三个包挂在同一张 version_nodes 上的触发器必须共存
    # （本包不得误删别人的）。
    alive = []
    for name in CROSS_PACK_NODE_TRIGGERS:
        n = conn.execute(
            text("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname = :n"),
            {"n": name},
        ).scalar_one()
        alive.append(f"{name}={n}")
    emit(
        all(item.endswith("=1") for item in alive),
        "version_nodes 上另三个包的配对触发器共存",
        "; ".join(alive),
    )

    # 跨包：别人家的表必须存活。
    rows = conn.execute(
        text(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema='public' AND table_name = ANY(:ts)"
        ),
        {"ts": list(CROSS_PACK_TABLES)},
    ).fetchall()
    present = sorted(r[0] for r in rows)
    emit(
        len(present) == len(CROSS_PACK_TABLES),
        "跨包表存活",
        f"{present} (期望 {len(CROSS_PACK_TABLES)})",
    )

    # 契约增量：本包端点必须在已导出的契约里（"写了端点但没进契约"是静默的）。
    spec = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    paths = spec["paths"]
    for p in EXPECTED_PATHS:
        emit(p in paths, f"契约含路径 {p}", sorted(paths.get(p, {}).keys()) or "缺失")

    # 契约里本包端点必须都声明了 422（项目规范：不依赖框架默认）。
    ok422 = True
    detail422 = []
    for p in EXPECTED_PATHS:
        for method, op in paths.get(p, {}).items():
            if method not in ("get", "post", "put"):
                continue
            codes = set(op.get("responses", {}))
            detail422.append(f"{method.upper()} {p}:{'422' in codes}")
            if "422" not in codes:
                ok422 = False
    emit(ok422, "本包端点均声明 422", "; ".join(detail422))

# ---------------------------------------------------------------------------
# 活性验证 1：合法基线必须通过（后面所有"被拦"的对照基准）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn)
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        _baseline_tx(conn, bid, pid)
        _approval_tx(conn, pid, bid, uid)
        _expect_ok("1 合法基线（节点+基线+同事务批准）通过", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "1 合法基线通过", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 2：缺批准必须被拦（OP-151）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn)
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        _baseline_tx(conn, bid, pid)
        _expect_blocked("2 OP-151：无批准基线被拦", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "2 OP-151", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 3：AC-08 正面 —— 替代链的第二条必须能建（部分唯一索引的对照）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn)
        b1 = uuid.uuid4()
        _node_tx(conn, b1, "BASELINE", pid, pid, uid, revision_no=1)
        _baseline_tx(conn, b1, pid)
        _approval_tx(conn, pid, b1, uid)
        b2 = uuid.uuid4()
        _node_tx(conn, b2, "BASELINE", pid, pid, uid, revision_no=2)
        _baseline_tx(conn, b2, pid, supersedes=b1, reason="修改后新建")
        _approval_tx(conn, pid, b2, uid)
        _expect_ok("3 AC-08 正面：修改创建新版本（带 supersedes）通过", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "3 AC-08 正面", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 4：AC-08 反面 —— 同一项目两条链根必须被拦（第 3 条的配对）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn)
        # 第 1 条链根：完整合法（节点 + 基线 + 同事务批准）。
        b1 = uuid.uuid4()
        _node_tx(conn, b1, "BASELINE", pid, pid, uid, revision_no=1)
        _baseline_tx(conn, b1, pid, reason="第1条链根")
        _approval_tx(conn, pid, b1, uid)
        # 第 2 条链根：节点先建（它不违规），基线本身才违规 ——
        # 违规语句必须落在断言 lambda 内部。
        # uq_production_baselines_project_root 是「普通部分唯一索引」，
        # 不是 DEFERRABLE 约束：它在 INSERT 那一刻就报错，
        # 不会等到 SET CONSTRAINTS ALL IMMEDIATE。把 INSERT 写在断言之外，
        # 异常就会在准备阶段抛出并被外层 except 吞掉 —— 判据变成「探针异常」，
        # 而不是「被索引拦下」。
        b2 = uuid.uuid4()
        _node_tx(conn, b2, "BASELINE", pid, pid, uid, revision_no=2)
        _expect_blocked(
            "4 AC-08 反面：同项目两条链根被拦（部分唯一索引）",
            lambda: _baseline_tx(conn, b2, pid, reason="第2条链根"),
            want_sqlstate="23505",
            want_name="uq_production_baselines_project_root",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "4 AC-08 反面", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 5：三张不可变表的 UPDATE 必须被拦（23001）
# ---------------------------------------------------------------------------
for table, column in (
    ("production_baselines", "change_reason"),
    ("baseline_items", "ref_label"),
    ("step_approvals", "reason"),
):
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            pid, uid, _, _ = _seed_tx(conn)
            bid = uuid.uuid4()
            _node_tx(conn, bid, "BASELINE", pid, pid, uid)
            _baseline_tx(conn, bid, pid)
            _approval_tx(conn, pid, bid, uid)
            _item_tx(conn, bid, 1)
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            _expect_blocked(
                f"5 不可变：{table} UPDATE 被拦",
                lambda t=table, c=column: conn.execute(
                    text(f"UPDATE {t} SET {c} = '篡改'")
                ),
                want_sqlstate="23001",
            )
            trans.rollback()
    except Exception as exc:  # noqa: BLE001
        emit(False, f"5 不可变 {table}", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 6：information_drafts 必须可改（第 5 组的对照）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn)
        _draft_tx(conn, pid)
        _expect_ok("6 对照：information_drafts UPDATE 可改（工作区）", lambda: conn.execute(
            text("UPDATE information_drafts SET operator_confirmed = true WHERE project_id = :p"),
            {"p": str(pid)},
        ))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "6 对照 drafts 可改", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 7：OP-88 正向判据 1 —— entity_type 必须是 BASELINE
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn)
        bid = uuid.uuid4()
        # 刻意造一个 entity_type='PLAN' 的节点（不是 BASELINE）。
        _node_tx(conn, bid, "PLAN", pid, pid, uid)
        _baseline_tx(conn, bid, pid)
        _approval_tx(conn, pid, bid, uid)
        _expect_blocked("7 OP-88 正向：entity_type≠BASELINE 被拦", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "7 OP-88 正向判据 1", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 8：OP-88 正向判据 3 —— entity_id 必须等于 project_id
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn)
        bid = uuid.uuid4()
        # 节点的 entity_id 是**另一个** uuid，而基线属于 pid。
        _node_tx(conn, bid, "BASELINE", uuid.uuid4(), pid, uid)
        _baseline_tx(conn, bid, pid)
        _approval_tx(conn, pid, bid, uid)
        _expect_blocked("8 OP-88 正向：entity_id≠project_id 被拦", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "8 OP-88 正向判据 3", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 9：OP-88 反向 —— BASELINE 节点必须配一条基线行
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn)
        # 项目**真实存在** + BASELINE 节点 + **不插** production_baselines 行。
        _node_tx(conn, uuid.uuid4(), "BASELINE", pid, pid, uid)
        _expect_blocked("9 OP-88 反向：孤立 BASELINE 节点被拦", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "9 OP-88 反向", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 10：OP-88 反向的收窄 —— 项目不存在时不拦（第 9 条的对照）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn)
        _node_tx(conn, uuid.uuid4(), "BASELINE", uuid.uuid4(), pid, uid)
        _expect_ok("10 OP-88 反向的收窄：项目不存在时不拦（对照）", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "10 OP-88 反向收窄", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 11~15：跨品牌 / 跨产品 / 跨项目 / GLOBAL 放行
# ---------------------------------------------------------------------------

# 11 跨品牌
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn, "A")
        _, _, brand_b, _ = _seed_tx(conn, "B")
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        _baseline_tx(conn, bid, pid)
        _approval_tx(conn, pid, bid, uid)
        aid = _asset_tx(conn, uid, category="SCENE", scope="BRAND", brand=brand_b,
                        title="WP22 跨品牌知识")
        _item_tx(conn, bid, 1, asset_id=aid)
        _expect_blocked("11 AC-24：跨品牌知识被拦", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "11 跨品牌", f"探针异常 {type(exc).__name__}: {exc}")

# 12 同品牌（对照）
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, brand_a, _ = _seed_tx(conn, "C")
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        _baseline_tx(conn, bid, pid)
        _approval_tx(conn, pid, bid, uid)
        aid = _asset_tx(conn, uid, category="SCENE", scope="BRAND", brand=brand_a,
                        title="WP22 同品牌知识")
        _item_tx(conn, bid, 1, asset_id=aid)
        _expect_ok("12 对照：同品牌 BRAND 通过", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "12 同品牌对照", f"探针异常 {type(exc).__name__}: {exc}")

# 13 跨产品
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn, "D")
        _, _, brand_b2, prod_b2 = _seed_tx(conn, "E")
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        _baseline_tx(conn, bid, pid)
        _approval_tx(conn, pid, bid, uid)
        aid = _asset_tx(conn, uid, category="SCENE", scope="PRODUCT",
                        brand=brand_b2, product=prod_b2, title="WP22 跨产品知识")
        _item_tx(conn, bid, 1, asset_id=aid)
        _expect_blocked("13 AC-24：跨产品知识被拦", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "13 跨产品", f"探针异常 {type(exc).__name__}: {exc}")

# 14 跨项目
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn, "F")
        pid2, _, _, _ = _seed_tx(conn, "G")
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        _baseline_tx(conn, bid, pid)
        _approval_tx(conn, pid, bid, uid)
        aid = _asset_tx(conn, uid, category="SCENE", scope="PROJECT", project=pid2,
                        title="WP22 跨项目知识")
        _item_tx(conn, bid, 1, asset_id=aid)
        _expect_blocked("14 AC-24：跨项目知识被拦", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "14 跨项目", f"探针异常 {type(exc).__name__}: {exc}")

# 15 GLOBAL 放行（对照）
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn, "H")
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        _baseline_tx(conn, bid, pid)
        _approval_tx(conn, pid, bid, uid)
        aid = _asset_tx(conn, uid, category="SCENE", scope="GLOBAL",
                        title="WP22 全局知识")
        _item_tx(conn, bid, 1, asset_id=aid)
        _expect_ok("15 对照：GLOBAL 知识放行", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "15 GLOBAL 放行对照", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 16：版本必须属于声明的资产（第 3 层防线的另一半）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn, "I")
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        _baseline_tx(conn, bid, pid)
        _approval_tx(conn, pid, bid, uid)
        a1 = _asset_tx(conn, uid, title="WP22 资产1")
        a2 = _asset_tx(conn, uid, title="WP22 资产2")
        v2 = _knowledge_version_tx(conn, uuid.uuid4(), a2, pid, uid, revision_no=2)
        # 声明资产是 a1，但版本属于 a2。
        _item_tx(conn, bid, 1, asset_id=a1, version_id=v2)
        _expect_blocked("16 AC-24：版本属于别的资产被拦", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "16 资产范围", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 17：正确配对必须通过（第 16 条的对照）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn, "J")
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        _baseline_tx(conn, bid, pid)
        _approval_tx(conn, pid, bid, uid)
        a = _asset_tx(conn, uid, title="WP22 资产3")
        v = _knowledge_version_tx(conn, uuid.uuid4(), a, pid, uid, revision_no=2)
        _item_tx(conn, bid, 1, asset_id=a, version_id=v)
        _expect_ok("17 对照：版本属于本资产通过", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "17 资产范围对照", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 18：存在未处置阻断项时不得建基线（AC-09）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn, "K")
        _draft_tx(conn, pid, conflicts=[
            {"conflictId": "C-1", "kind": "SOURCE", "blocking": True, "state": "OPEN"}
        ])
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        _baseline_tx(conn, bid, pid)
        _approval_tx(conn, pid, bid, uid)
        _expect_blocked("18 AC-09：未处置阻断项被拦", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "18 AC-09", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 19：阻断项已处置必须通过（第 18 条的对照）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn, "L")
        _draft_tx(conn, pid, conflicts=[
            {"conflictId": "C-2", "kind": "SOURCE", "blocking": True, "state": "SETTLED"}
        ])
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        _baseline_tx(conn, bid, pid)
        _approval_tx(conn, pid, bid, uid)
        _expect_ok("19 对照：阻断项已 SETTLED 通过", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "19 阻断项已处置对照", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 20：非阻断项未处置必须通过（blocking=false 的对照）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn, "M")
        _draft_tx(conn, pid, conflicts=[
            {"conflictId": "C-3", "kind": "SOURCE", "blocking": False, "state": "OPEN"}
        ])
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        _baseline_tx(conn, bid, pid)
        _approval_tx(conn, pid, bid, uid)
        _expect_ok("20 对照：非阻断项未处置也通过（blocking=false）", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "20 非阻断项对照", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 21：已被取代的批准不构成依据
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn, "N")
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        a1 = _approval_tx(conn, pid, bid, uid, decision="APPROVED")
        # 第二条批准取代了第一条（先批后撤）。
        _approval_tx(conn, pid, bid, uid, decision="REJECTED", supersedes=a1)
        _baseline_tx(conn, bid, pid)
        _expect_blocked("21 批准被取代后不构成依据", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "21 批准被取代", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 22：同一版两条链根批准必须被拦（uq_step_approvals_root 的 WHERE 在位）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn, "O")
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        _approval_tx(conn, pid, bid, uid, decision="REJECTED")
        # 第二条链根批准：违规语句必须在断言 lambda 内部。
        # uq_step_approvals_root 同为「普通部分唯一索引」（非 DEFERRABLE），
        # INSERT 当刻即报 23505，不会等到 SET CONSTRAINTS ALL IMMEDIATE。
        _expect_blocked(
            "22 同版两条链根批准被拦（部分唯一索引）",
            lambda: _approval_tx(conn, pid, bid, uid, decision="APPROVED"),
            want_sqlstate="23505",
            want_name="uq_step_approvals_root",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "22 批准链根唯一", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 23：先拒后批（带 supersedes）必须能通过（第 22 条的对照）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn, "P")
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        a1 = _approval_tx(conn, pid, bid, uid, decision="REJECTED")
        _approval_tx(conn, pid, bid, uid, decision="APPROVED", supersedes=a1)
        _baseline_tx(conn, bid, pid)
        _expect_ok("23 对照：先拒后批（带 supersedes）通过", lambda: conn.execute(
            text("SET CONSTRAINTS ALL IMMEDIATE")))
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "23 先拒后批对照", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 24：排除项必须有原因（AC-24 原文）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn, "Q")
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        _baseline_tx(conn, bid, pid)
        _approval_tx(conn, pid, bid, uid)
        _expect_blocked(
            "24 排除项无原因被拦",
            lambda: _item_tx(conn, bid, 1, inclusion="EXCLUDED", reason=None),
            want_name="ck_baseline_items_excluded_needs_reason",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "24 排除项须有原因", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 25：空快照被拦 —— R1 的唯一结构防线
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn, "R")
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        _baseline_tx(conn, bid, pid)
        _approval_tx(conn, pid, bid, uid)
        # snapshot = '{}' 表示"只有引用没有值" —— 正是 R1 的形状。
        _expect_blocked(
            "25 R1：空快照（只存 ref）被拦",
            lambda: _item_tx(conn, bid, 1, snapshot={}),
            want_name="ck_baseline_items_snapshot_not_empty",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "25 空快照", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 26：step_no 越界被拦
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn, "S")
        bid = uuid.uuid4()
        _node_tx(conn, bid, "BASELINE", pid, pid, uid)
        _expect_blocked(
            "26 step_no=9 越界被拦",
            lambda: _approval_tx(conn, pid, bid, uid, step_no=9),
            want_name="ck_step_approvals_step_no_in_range",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "26 step_no 越界", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 27：一项目一份草稿（R6）
# ---------------------------------------------------------------------------
try:
    with engine.connect() as conn:
        trans = conn.begin()
        pid, uid, _, _ = _seed_tx(conn, "T")
        _draft_tx(conn, pid)
        _expect_blocked(
            "27 R6：一项目两份草稿被拦",
            lambda: _draft_tx(conn, pid),
            want_sqlstate="23505",
            want_name="uq_information_drafts_project_id",
        )
        trans.rollback()
except Exception as exc:  # noqa: BLE001
    emit(False, "27 一项目一份草稿", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 活性验证 28~29：baseline_items / step_approvals 的 UPDATE（第 5 组的补全）
# ---------------------------------------------------------------------------
for table, column in (("baseline_items", "ref_label"), ("step_approvals", "reason")):
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            pid, uid, _, _ = _seed_tx(conn, "U")
            bid = uuid.uuid4()
            _node_tx(conn, bid, "BASELINE", pid, pid, uid)
            _baseline_tx(conn, bid, pid)
            _approval_tx(conn, pid, bid, uid)
            _item_tx(conn, bid, 1)
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            _expect_blocked(
                f"{28 if table == 'baseline_items' else 29} 不可变：{table} UPDATE 被拦",
                lambda t=table, c=column: conn.execute(
                    text(f"UPDATE {t} SET {c} = '篡改'")
                ),
                want_sqlstate="23001",
            )
            trans.rollback()
    except Exception as exc:  # noqa: BLE001
        emit(False, f"不可变 {table}", f"探针异常 {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# 零残留核对
# ---------------------------------------------------------------------------
with engine.connect() as conn:
    for table in OWN_TABLES:
        n = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        emit(n == 0, f"零残留 {table}", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM users WHERE issuer = 'wp22-probe'")
    ).scalar_one()
    emit(n == 0, "零残留 探针用户", f"{n} 行")
    n = conn.execute(
        text("SELECT count(*) FROM projects WHERE project_code LIKE 'WP22P%'")
    ).scalar_one()
    emit(n == 0, "零残留 探针项目", f"{n} 行")

print(f"RESULT_EMITTED (期望 {EXPECTED_CHECKS} 条)")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def collect_wp22() -> tuple[Path, bool]:
    env = os.environ.copy()
    env.update(
        {
            "NO_PROXY": "*",
            "no_proxy": "*",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "PYTHONPATH": str(BACKEND_DIR / "src"),
        }
    )
    completed = subprocess.run(
        [PYTHON, "-c", INSPECT_SCRIPT],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    checks = [line for line in output.splitlines() if line.startswith(("CHECK", "SKIP"))]
    failures = [line for line in checks if line.startswith("CHECK FAIL")]

    lines = [
        "# WP22 信息审核与基线：4 张表 / 31 CHECK / 9 触发器 / 2 部分唯一索引"
        " / 6 延迟外键 / 二十九处活性验证 / 契约",
        f"# 生成时间: {_now()}",
        "# 目标库: rednote_verify_wp22（收口时由迁移从 bootstrap 建出，非测试夹具建表）",
        "# 命令: cd services/backend && python -c '<information_schema + 延迟约束试写 + 试插>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp22-baseline.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # **条数不足也算失败**（ISS-43：崩溃伪装成"全过"）。
    complete = len(checks) >= EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete)


def main() -> int:
    path, ok = collect_wp22()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
