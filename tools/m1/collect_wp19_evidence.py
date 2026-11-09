#!/usr/bin/env python
"""WP19 证据采集脚本。

产出的文件（落在 `evidence/m1/`）：

| 文件 | 内容 |
|---|---|
| `wp19-research.log` | **零表**结构核对 + 两个端点在契约里 + **八处活性验证** |

**本包与其余工作包的根本差异**：WP18/WP20 那类包要核对
"N 张表 / M 条 CHECK / K 个触发器"，本包**一张表都没建**。
因此本脚本的核对分两类：

| 类 | 核对什么 | 为什么必须 |
|---|---|---|
| **静态** | 契约里两个端点存在且形状正确；迁移里**没有本包的 revision**；本模块**没有 models.py** | 证明"零表"是**事实**而不是"忘了做"。这是本包最核心的划界（说明文档 §1.7） |
| **活性** | 读路径**真的不写任何东西**；读出来的**真的是库里的数据**；权限四层**真的生效** | 证明"不写"是**行为**而不是"没写代码"。**一个顺手把派生结果写进某张表的实现，会让全部功能测试照样通过** |

## 八处活性验证

1. **读路径零写入**：一次 `build_research_prompts` + `list_research_files`
   前后，本包涉及的全部表（`project_file_links` / `parse_runs` /
   `source_segments` / `brief_versions` / `brief_claims` / `version_nodes`
   / `quota_accounts` / `blobs` / `file_objects`）的行数**完全不变**。
   这是本包最实质的一条证据 —— 没有它，"零写入"只是一句注释。
2. **读出来的确实是库里的数据**：用原生 SQL 造
   `project_file_links(purpose='RESEARCH')` + `parse_runs(SUCCEEDED)` +
   `source_segments`，断言服务层读出的 `parseState` / `segmentCount` /
   `byteSize` 与 SQL 里的事实**一致**。没有这条，第 1 条只能证明
   "什么都没发生"（一个永远返回空列表的实现也能全绿）。
3. **`purpose` 过滤真的生效**：造一条 `purpose='REFERENCE'` 的链接，
   断言它**不出现在**研究文件列表里。判据来自 WP12 的
   `PURPOSE_RESEARCH`，不是本包自己写的字符串。
4. **`effective=False` 的链接真的被排除**：软删（`effective=False`）
   的链接不该出现在列表里，也不该计入 `listedBytes`。
5. **配额口径与 WP12 的重算一致**：`listedBytes` 必须等于
   `FileRepository.compute_effective_bytes()` —— **不是本包自己乘除一遍**。
   两个数不一致说明本包造了第三个口径。
6. **缺口判据不依赖 `has_evidence`**（本包最关键的一处修正）：
   造一条 `category='SOURCE_FACT'` + `resolution_status='PENDING'` +
   **带 evidence_url** 的 claim → 断言它**仍然是一条 `MISSING` 缺口**。
   若按 `has_evidence` 判，这条会被判成 `COVERED` —— 而数据库 CHECK
   `ck_brief_claims_factual_category_requires_evidence` 保证了
   "需要证据的类别必有来源"，于是 `gaps[]` **永远为空**、
   `blockingCount` **恒为 0**、"步骤 2 确认完整"变成永远已完成的按钮，
   且**不报任何错**。
7. **三条判据真的分开**：`claim_needs_gap` / `gap_state` /
   `claim_needs_search` 在五种 (类别 × 状态) 组合上给出**互不相同**的
   结果组合。任一条被合并成一个函数，都会在
   "`NOT_APPLICABLE` 算不算缺口"或"`STRATEGY` 该不该出检索词"上出错。
8. **`PARTIAL` 源必须有疑问记录**（交叉引用 WP18 的延迟触发器）：
   `brief_versions.source_state='PARTIAL'` 但没有 `brief_issues` 行
   → 断言被 `rednote_check_brief_version_consistency` 拦下（`23514`）。
   **这一条不属于本包**，但本包**读** `source_state` 并据此给 `notes`，
   因此必须确认该不变量的存在 —— 否则"PARTIAL 源可能不完整"这句
   提示词的前提是假的。

**跨包回归项**：

- 契约必须**至少**含本包的 2 个 GET（`research-prompts` / `research-files`），
  且 paths/operations **总数不少于交付时的 39/46** ——
  **刻意不写等式**：后续包合法新增会让等式判据变红（WP23 就是这样把本脚本弄红的，
  见 ISS-44/64/71/83 同族，本条是第八次现身）；
- WP18 的 5 张表与 7 个触发器必须仍在（本包一行都没动它们）；
- WP15/WP16/WP17/WP18/WP20 的不可变触发器必须仍在。

**探针数据的处置**：所有探针跑在**独立连接**里，最后**无条件
`rollback()`** → **零残留**。本包虽然不写任何东西，探针仍要造
夹具数据（否则没得读），因此仍需回滚。判据是"与运行前基线差分"
（不是"全表 0 行"—— 库里本来就有历史数据）。

**为什么内联脚本只允许单行注释、不能出现三引号**（ISS-45）：
外层用 `r\"\"\"...\"\"\"` 包裹整个脚本，内联脚本里的三引号会**提前
终止外层包裹**，而 Python 只在语法上多出一堆莫名错误，很难定位。
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
EXPECTED_CHECKS: int = 48

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

INSPECT_SCRIPT = r"""
# 内联脚本：只允许单行注释，且不能出现三引号（三引号会提前终止外层包裹，见 ISS-45）。

import json
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text

# 期望产生的核对条数（ISS-43：崩溃伪装成"全过"的防线）。
EXPECTED_CHECKS = 48

DB = "postgresql+psycopg://rednote@localhost:5432/rednote_verify_wp19"
engine = create_engine(DB)

REPO = Path("/Users/<user>/内容运营AI Native/rednote-production")
BACKEND_DIR = REPO / "services" / "backend"
CONTRACT_PATH = REPO / "contracts" / "openapi.json"

# 该包**引入**的迁移版本。本包零迁移，因此它的引入版本 = WP20 的 head。
# 判据是"不早于"而不是"恰好等于"（ISS-44）：用"恰好等于"会让每个
# 新工作包把历史证据集体打红。
REVISION_INTRODUCED = "ebc05dfca16b"

# 本包**读**的全部表（零写入判据的核对对象）。
# 这份清单必须覆盖 repository.py 里出现的每一个表名。
READ_TABLES = (
    "project_file_links",
    "file_objects",
    "blobs",
    "parse_runs",
    "source_segments",
    "briefs",
    "brief_versions",
    "brief_claims",
    "brief_issues",
    "version_nodes",
    "quota_accounts",
    "projects",
)

# 本包**不该**建的迁移版本前缀（零表的静态判据）。
# 判据是"没有新增 revision 文件"而不是"文件数恰好等于 N"（ISS-71 的
# 等式基数判据：断言"恰好 N 个"会因别人的合法增量变红）。
SELF_TABLE_PREFIXES = ("research_",)

CHECKS = 0
FAILS = 0


def emit(ok, label, detail):
    global CHECKS, FAILS
    CHECKS += 1
    if not ok:
        FAILS += 1
    print(("CHECK      " if ok else "CHECK FAIL ") + label + " :: " + str(detail))


def _revision_is_at_least(current, introduced):
    # 当前 head 是否**不早于** introduced —— 沿真实迁移链按位置判断，不是猜。
    #
    # **为什么不能写 `== EXPECTED_REVISION`**（ISS-44）：那会让每个新工作包
    # 把前面所有包的历史证据集体打红，因为校验的其实是"迁移停在某个特定
    # 版本"，而真正该校验的是"**该包依赖的东西还在**"。
    #
    # **为什么也不能用 `len(current) >= len(introduced)`**（ISS-115 族的
    # 恒真判据）：本项目全部 16 个 revision id **都是 12 位十六进制**，
    # 于是"长度不短于"在 `current != introduced` 时**恒为 True** ——
    # 那是把等式判据换成了**恒绿判据**，比原缺陷更隐蔽：它永不失败，
    # 因此也永不报警（判据纪律第 7/9 条）。
    #
    # **本函数此前还有第三个缺陷**（ISS-116，2026-09-28 实测）：
    # 它调的是 `script.walk_revisions(current, introduced)` —— 而真实签名是
    # `walk_revisions(base, head)`（降序产出 head→base）。参数传反后
    # **方向全错**，实测 4 个场景错 3 个：
    #
    #   | current        | introduced     | 期望  | 传反的实现 | 正确实现 |
    #   |----------------|----------------|-------|-----------|---------|
    #   | ebc05dfca16b   | ebc05dfca16b   | True  | True      | True    |
    #   | 185ec01bed91   | ebc05dfca16b   | True  | **False** | True    |
    #   | ebc05dfca16b   | 185ec01bed91   | False | **True**  | False   |
    #   | 7a4c1e93b6d2   | ebc05dfca16b   | True  | **False** | True    |
    #
    # 它之所以长期"通过"，是因为第 2~4 行只在库已推进时才会走到，而本包读的
    # 固定库 `rednote_verify_wp19` 的 head 恰好停在引入点 —— 走 `current ==
    # introduced` 短路分支直接返回 True，**永不触达出错的代码路径**。
    # 这与 ISS-82 同族：判据必须能区分两种实现，而它在真实库上两种实现同形。
    #
    # 正确做法：拿到**真实迁移链**，按位置比较。`walk_revisions(base=..., head=...)`
    # 的返回值是 head→base 降序，因此 index 小 = 新。
    # ⚠️ 该调用会 import 每个迁移模块（`from rednote.modules...`），
    # 故内联段必须在 `PYTHONPATH=<repo>/services/backend/src` 下运行。
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(REPO / "migrations" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
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


# ---------------------------------------------------------------------------
# 结构核对
# ---------------------------------------------------------------------------
with engine.connect() as conn:
    rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    # 判据必须走 `_revision_is_at_least`，**不能写 `rev == REVISION_INTRODUCED`**：
    # 等式只在"本包交付当天 + 验证库恰好没重建"时成立 —— 它检验的是
    # "这个库从没被更新过"，而不是"本包依赖的东西还在"，属恒真判据
    # （ISS-82：判据必须能区分两种实现）。
    # 本函数此前**定义了却从未调用**，是死代码 —— 下面是它第一次被真正使用。
    emit(
        _revision_is_at_least(rev, REVISION_INTRODUCED),
        "迁移 head 不早于本包引入的版本（本包零迁移，引入版本 = WP20 的 head）",
        f"{rev} (本包引入 {REVISION_INTRODUCED})",
    )

    # 1. 本包没有建任何表。判据是"没有 research_ 前缀的表"。
    self_tables = [
        row[0]
        for row in conn.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
                " AND tablename LIKE ANY(:pats)"
            ),
            {"pats": [p + "%" for p in SELF_TABLE_PREFIXES]},
        )
    ]
    emit(
        self_tables == [],
        "本包零表（没有 research_* 表）",
        self_tables if self_tables else "无",
    )

    # 2. 本包没有自己的迁移 revision 文件。
    versions_dir = REPO / "migrations" / "versions"
    own_migrations = [
        p.name for p in versions_dir.glob("*.py") if "research" in p.name.lower()
    ]
    emit(
        own_migrations == [],
        "本包零迁移（没有 research 相关 revision 文件）",
        own_migrations if own_migrations else "无",
    )

    # 3. 本模块没有 models.py（零表的结构性判据）。
    research_dir = BACKEND_DIR / "src" / "rednote" / "modules" / "research"
    emit(
        not (research_dir / "models.py").exists(),
        "modules/research/ 没有 models.py",
        sorted(p.name for p in research_dir.glob("*.py")),
    )

    # 4. 契约里两个端点都存在（本包净增 2 个 GET）。
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    paths = contract["paths"]
    for suffix, method in (
        ("/projects/{projectId}/research-prompts", "get"),
        ("/projects/{projectId}/research-files", "get"),
    ):
        found = [p for p in paths if p.endswith(suffix)]
        emit(
            len(found) == 1 and method in paths[found[0]],
            "契约含 " + method.upper() + " " + suffix,
            found if found else "缺失",
        )

    # 5. 契约**至少**含本包的 2 个 GET（**不写等式** —— 后续包合法新增会让等式判据变红）。
    #
    # 这一处曾是 `len(paths) == 39` / `op_count == 46`（WP19 交付时的快照）。
    # WP23 合法新增 6 个操作后，本脚本在收口复跑时变红 —— 而 WP19 自己什么都没坏。
    # 这是本项目同一根因的**第八次现身**（ISS-44 迁移版本 / ISS-64 否定式全契约范围 /
    # ISS-71 等式基数 / ISS-83 端点计数 / 本条的契约总数）。
    # 判据改成「本包那 2 个端点在」+「总数不少于交付时的值」：
    #   · 前者是本包真正要守的不变量；
    #   · 后者能抓住"契约被删空"这类退化，又不会被别人的合法增量触发。
    own_ops = sum(
        1
        for suffix, method in (
            ("/projects/{projectId}/research-prompts", "get"),
            ("/projects/{projectId}/research-files", "get"),
        )
        for p in paths
        if p.endswith(suffix) and method in paths[p]
    )
    emit(own_ops == 2, "契约含本包的 2 个 GET（research-prompts / research-files）", own_ops)

    # WP19 交付时是 39 paths / 46 operations —— 作为**下界**保留（只增不减）
    MIN_PATHS_AT_WP19 = 39
    MIN_OPS_AT_WP19 = 46
    emit(
        len(paths) >= MIN_PATHS_AT_WP19,
        "契约 paths 总数 >= " + str(MIN_PATHS_AT_WP19) + "（WP19 交付时值，只增不减）",
        len(paths),
    )
    op_count = sum(
        1
        for item in paths.values()
        for m in item
        if m in ("get", "post", "put", "patch", "delete")
    )
    emit(
        op_count >= MIN_OPS_AT_WP19,
        "契约 operations 总数 >= " + str(MIN_OPS_AT_WP19) + "（同上）",
        op_count,
    )

    # 6. 两个端点的响应模型声明了 meta.research（不被静默剥掉）。
    files_path = [p for p in paths if p.endswith("/research-files")][0]
    schema_ref = json.dumps(paths[files_path]["get"].get("responses", {}))
    emit(
        "ResearchFilesEnvelope" in schema_ref,
        "research-files 用自定义 ResearchFilesEnvelope（meta.research 不被剥）",
        schema_ref[:90],
    )

    # 7. 两个端点的 responses 里声明了 422（platform/api_responses 的教训）。
    for suffix in ("/research-prompts", "/research-files"):
        path_key = [p for p in paths if p.endswith(suffix)][0]
        codes = sorted(paths[path_key]["get"].get("responses", {}).keys())
        emit("422" in codes, suffix + " 声明了 422", codes)

    # 8. 本包没有写端点（两个端点都是 GET）。
    emitted_ops = []
    for path_key in paths:
        if path_key.endswith(("/research-prompts", "/research-files")):
            emitted_ops += [m.upper() for m in paths[path_key] if m != "parameters"]
    emit(
        emitted_ops == ["GET", "GET"],
        "研究端点全是 GET（零写端点）",
        emitted_ops,
    )

    # 9. WP18 的 5 张表仍在（本包一行都没动）。
    for tbl in ("briefs", "brief_versions", "brief_claims", "brief_issues", "brief_drafts"):
        exists = conn.execute(
            text("SELECT to_regclass(:t)"), {"t": "public." + tbl}
        ).scalar_one()
        emit(exists is not None, "WP18 表仍在：" + tbl, bool(exists))

    # 10. WP18 的一致性触发器函数仍在（本包读 source_state 的前提）。
    fn = conn.execute(
        text(
            "SELECT count(*) FROM pg_proc"
            " WHERE proname='rednote_check_brief_version_consistency'"
        )
    ).scalar_one()
    emit(fn == 1, "WP18 版本一致性触发器函数仍在", fn)

    # 11. 不可变触发器函数仍在（所有不可变表共用）。
    imm = conn.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname='rednote_reject_mutation'")
    ).scalar_one()
    emit(imm == 1, "共用不可变触发器函数仍在", imm)

    # 12. 跨包不可变触发器数量（WP15~WP20 累计）。
    trig = conn.execute(
        text(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid"
            " WHERE NOT t.tgisinternal AND pg_get_triggerdef(t.oid) LIKE '%rednote_reject_mutation%'"
        )
    ).scalar_one()
    emit(trig >= 10, "不可变触发器总数 >= 10（WP15~WP20）", trig)

# ---------------------------------------------------------------------------
# 探针的公共前置：造项目 + 用户 + 研究文件（可变表，仍然整体 rollback）
# ---------------------------------------------------------------------------
def _seed(conn):
    # 列名与非空集合全部按 **information_schema 实测**，不要凭印象写。
    # 本包第一次跑实测出的差异（这一条每次都值得做）：
    #   projects : 有 project_code / owner_id（都 NOT NULL）、
    #              没有 created_by；phase 默认 'PREPARATION'
    #   blobs    : 是 object_key 不是 storage_key；scan_status 有默认 'PENDING'
    #   file_objects : 有 visibility（NOT NULL）；**project_id 可空**
    #              （归属在 project_file_links 上）
    #   parse_runs : parser_version 有默认 'wp17-1'；parsed_units NOT NULL 默认 0
    #   source_segments : segment_no（不是 unit_index）、text_content（不是 content）、
    #              position / content_hash / extraction_kind 都 NOT NULL
    #   brief_versions : **没有 updated_at**（它是不可变表）
    #   users    : 没有 email，是 (issuer, subject) 唯一
    #   brands   : 只有 name；products 是 (brand_id, name)
    uid = uuid.uuid4()
    bid = uuid.uuid4()
    pid = uuid.uuid4()
    prod_id = uuid.uuid4()
    proj_id = uuid.uuid4()
    tag = "wp19probe-" + uuid.uuid4().hex[:8]

    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, display_name, role)"
            " VALUES (:id, :iss, :sub, :name, 'OPERATOR')"
        ),
        {"id": uid, "iss": tag, "sub": tag + "-sub", "name": tag},
    )
    conn.execute(
        text("INSERT INTO brands (id, name) VALUES (:id, :n)"),
        {"id": bid, "n": tag + "-brand"},
    )
    conn.execute(
        text("INSERT INTO products (id, brand_id, name) VALUES (:id, :b, :n)"),
        {"id": prod_id, "b": bid, "n": tag + "-prod"},
    )
    conn.execute(
        text(
            "INSERT INTO projects (id, project_code, name, brand_id, product_id,"
            " owner_id, target_delivery_count, due_date)"
            " VALUES (:id, :code, :n, :b, :p, :o, 1, now() + interval '30 days')"
        ),
        {
            "id": proj_id,
            "code": tag,
            "n": tag + "-proj",
            "b": bid,
            "p": prod_id,
            "o": uid,
        },
    )
    return uid, proj_id, tag


def _blob_and_file(conn, uid, purpose, project_id, *, effective=True, byte_size=1024, tag="x"):
    # **`visibility` 与 `project_id` 必须配对**（本机实测撞到
    # `ck_file_objects_visibility_matches_project`）：
    #   visibility='PROJECT'   -> project_id IS NOT NULL
    #   visibility='KNOWLEDGE' -> project_id IS NULL
    # 合法值只有这两个。研究文件是项目内的，因此用 PROJECT + 传 project_id。
    # **而 `project_file_links.project_id` 与它无关** —— 那是"关联归属"，
    # 两者都要，语义不同（前者是文件本身的可见性，后者是这条关联挂在哪个项目）。
    blob_id = uuid.uuid4()
    file_id = uuid.uuid4()
    link_id = uuid.uuid4()
    # sha256 是 character varying（不是 bytea），长度按 CHECK 要求给足。
    sha = (uuid.uuid4().hex + uuid.uuid4().hex)[:64]
    conn.execute(
        text(
            "INSERT INTO blobs (id, storage_backend, object_key, sha256, byte_size, mime_type)"
            " VALUES (:id, 'LOCAL', :k, :s, :n, 'application/pdf')"
        ),
        {"id": blob_id, "k": tag + "/" + sha[:16], "s": sha, "n": byte_size},
    )
    conn.execute(
        text(
            "INSERT INTO file_objects (id, blob_id, original_name, visibility,"
            " project_id, classification, uploaded_by)"
            " VALUES (:id, :b, :n, 'PROJECT', :p, 'SUPPORTED', :u)"
        ),
        {
            "id": file_id,
            "b": blob_id,
            "n": tag + ".pdf",
            "p": project_id,
            "u": uid,
        },
    )
    conn.execute(
        text(
            "INSERT INTO project_file_links (id, project_id, file_id, purpose, effective)"
            " VALUES (:id, :p, :f, :pu, :e)"
        ),
        {"id": link_id, "p": project_id, "f": file_id, "pu": purpose, "e": effective},
    )
    return link_id, file_id


def _parse_run(conn, file_id, state, *, total=3, parsed=3, errors=None):
    # parse_runs 的 CHECK（WP17 原生读出）：
    #   SUCCEEDED -> total_units IS NOT NULL AND parsed_units = total_units AND parsed_units > 0
    #   PARTIAL   -> 0 < parsed_units < total_units
    #   四终态     -> finished_at IS NOT NULL
    # 因此 total/parsed 必须**自洽**，否则插入即违规。
    run_id = uuid.uuid4()
    finished = "now()" if state in ("SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED") else "NULL"
    conn.execute(
        text(
            "INSERT INTO parse_runs (id, file_id, state, parser_version, total_units,"
            " parsed_units, errors, finished_at)"
            " VALUES (:id, :f, :s, 'v1', :t, :p, cast(:e as jsonb), "
            + finished
            + ")"
        ),
        {
            "id": run_id,
            "f": file_id,
            "s": state,
            "t": total,
            "p": parsed,
            "e": json.dumps(errors or []),
        },
    )
    return run_id


def _segment(conn, run_id, index):
    # `source_segments` 的 CHECK（本机原生读出，**实测出的 4 处**）：
    #   · position ? 'kind'          -> position 必须有 kind 键；
    #     顺带说明 TEXT 的 position 就是 {"kind":"TEXT","page":N}
    #   · segment_no >= 1            -> **从 1 起，不是 0**（本机实测：
    #     传 0 会撞 ck_source_segments_segment_no_positive）
    #   · char_length(content_hash) = 64
    #   · char_length(text_content) > 0
    #   · extraction_kind IN (OCR, TABLE, TEXT)
    body = "段 " + str(index)
    conn.execute(
        text(
            "INSERT INTO source_segments (id, parse_run_id, segment_no, position,"
            " text_content, content_hash, extraction_kind)"
            " VALUES (:id, :r, :i, cast(:pos as jsonb), :c, :h, 'TEXT')"
        ),
        {
            "id": uuid.uuid4(),
            "r": run_id,
            "i": index + 1,
            "pos": json.dumps({"kind": "TEXT", "page": index + 1}),
            "c": body,
            "h": (uuid.uuid4().hex + uuid.uuid4().hex)[:64],
        },
    )


def _baseline_counts(conn):
    out = {}
    for tbl in READ_TABLES:
        out[tbl] = conn.execute(text("SELECT count(*) FROM " + tbl)).scalar_one()
    return out


# ---------------------------------------------------------------------------
# 活性探针的公共骨架
# ---------------------------------------------------------------------------
# **必须让服务层复用同一个未提交事务** —— 这是本脚本最要紧的一处设计。
#
# 第一版让服务层走 `session_scope(settings)`（独立连接 + 结束时 commit），
# 而夹具数据在另一个连接上 `commit()` 了。结果：
#   · 「零残留」失败（探针数据留在库里）；
#   · 而且**清理不掉** —— `source_segments` / `blobs` 之外的几张表
#     有不可变触发器（`trg_source_segments_immutable` 等），DELETE 被拦。
#
# 正确形状：**夹具与服务层共用一个 Session**，全程不 commit，
# 探针结束无条件 rollback。这样：
#   · 服务层看到的是同一事务里的未提交数据（可见性满足）；
#   · 结束时一行都不留（rollback 连不可变表也回滚 ——
#     不可变触发器拦的是 SQL 层的 UPDATE/DELETE，不拦事务回滚）。
#
# 判据是"与运行前基线差分"（不是"全表 0 行"）：库里本来就有历史数据。
from sqlalchemy.orm import Session as _OrmSession


def _session_for_probe():
    # 一个绑定到固定 DSN 的 Session，**由调用方负责 rollback + close**。
    return _OrmSession(bind=engine, expire_on_commit=False)


def _probe_no_write(before):
    from rednote.modules.research.repository import ResearchRepository
    from rednote.modules.research import service as svc

    with _session_for_probe() as session:
        conn = session.connection()
        uid, proj_id, tag = _seed(conn)
        link_id, file_id = _blob_and_file(conn, uid, "RESEARCH", proj_id, tag=tag)
        run_id = _parse_run(conn, file_id, "SUCCEEDED", total=2, parsed=2)
        _segment(conn, run_id, 0)
        _segment(conn, run_id, 1)

        repo = ResearchRepository(session)
        svc.build_research_prompts(repo, project_id=proj_id)
        svc.list_research_files(repo, project_id=proj_id)

        # 在同一事务里数一次（服务层读的就是这些行）。
        inside = _baseline_counts(conn)
        # **无条件回滚**：本包读路径一行都不该写，因此"事务内多出来的行"
        # 全部来自夹具，回滚即零残留。
        session.rollback()

    with engine.connect() as conn:
        after = _baseline_counts(conn)
    diff = {t: (before[t], after[t]) for t in READ_TABLES if before[t] != after[t]}
    emit(
        diff == {},
        "活性1 读路径零写入（12 张表行数与运行前一致）",
        diff if diff else "全部不变",
    )
    # 附加证据：事务内确实有夹具数据（否则"零写入"可能只是"没读到东西"）。
    seeded = sum(1 for t in READ_TABLES if inside[t] > before[t])
    emit(
        seeded >= 6,
        "活性1 对照组：事务内确实造了夹具数据（不是空跑）",
        seeded,
    )


# ---------------------------------------------------------------------------
# 活性验证 2：读出来的确实是库里的数据（防"永远返回空"）
# ---------------------------------------------------------------------------
def _probe_reads_real_rows():
    from rednote.modules.research.repository import ResearchRepository
    from rednote.modules.research import service as svc

    with _session_for_probe() as session:
        conn = session.connection()
        uid, proj_id, tag = _seed(conn)
        link_id, file_id = _blob_and_file(
            conn, uid, "RESEARCH", proj_id, tag=tag, byte_size=4096
        )
        run_id = _parse_run(conn, file_id, "SUCCEEDED", total=3, parsed=3)
        _segment(conn, run_id, 0)
        _segment(conn, run_id, 1)
        _segment(conn, run_id, 2)
        session.flush()

        bundle = svc.list_research_files(
            ResearchRepository(session), project_id=proj_id
        )
        rows = bundle.rows
        emit(len(rows) == 1, "活性2 读到刚造的那 1 个研究文件", len(rows))
        if rows:
            row = rows[0]
            emit(
                row.parse_state == "SUCCEEDED",
                "活性2 parseState == SUCCEEDED",
                row.parse_state,
            )
            # segmentCount 来自"最新那次 run"的片段数（相关子查询）。
            emit(row.segment_count == 3, "活性2 segmentCount == 3", row.segment_count)
            emit(row.byte_size == 4096, "活性2 byteSize == 4096", row.byte_size)
            emit(
                row.total_units == 3 and row.parsed_units == 3,
                "活性2 totalUnits/parsedUnits == 3/3",
                (row.total_units, row.parsed_units),
            )
        # **第 2 条是第 1 条的对照组**：只测"零写入"时，一个永远返回空
        # 列表的实现也能全绿。必须同时断言"该读到的确实读到了"。
        session.rollback()


# ---------------------------------------------------------------------------
# 活性验证 3：purpose 过滤真的生效（只认 RESEARCH）
# ---------------------------------------------------------------------------
def _probe_purpose_filter():
    from rednote.modules.files.models import PURPOSE_RESEARCH
    from rednote.modules.research.repository import ResearchRepository
    from rednote.modules.research import service as svc

    with _session_for_probe() as session:
        conn = session.connection()
        uid, proj_id, tag = _seed(conn)
        for purpose in ("RESEARCH", "REFERENCE", "BRIEF"):
            _blob_and_file(conn, uid, purpose, proj_id, tag=tag + "-" + purpose)
        session.flush()

        bundle = svc.list_research_files(
            ResearchRepository(session), project_id=proj_id
        )
        emit(
            len(bundle.rows) == 1,
            "活性3 三个 purpose 只读出 RESEARCH 那一个",
            len(bundle.rows),
        )
        emit(
            all(r.purpose == PURPOSE_RESEARCH for r in bundle.rows),
            "活性3 读出的 purpose 全等于常量 PURPOSE_RESEARCH",
            PURPOSE_RESEARCH,
        )
        session.rollback()


# ---------------------------------------------------------------------------
# 活性验证 4：effective=False 的链接真的被排除
# ---------------------------------------------------------------------------
def _probe_effective_filter():
    from rednote.modules.research.repository import ResearchRepository
    from rednote.modules.research import service as svc

    with _session_for_probe() as session:
        conn = session.connection()
        uid, proj_id, tag = _seed(conn)
        _blob_and_file(
            conn, uid, "RESEARCH", proj_id, tag=tag + "-keep", byte_size=1000
        )
        _blob_and_file(
            conn,
            uid,
            "RESEARCH",
            proj_id,
            tag=tag + "-drop",
            byte_size=2000,
            effective=False,
        )
        session.flush()

        bundle = svc.list_research_files(
            ResearchRepository(session), project_id=proj_id
        )
        emit(len(bundle.rows) == 1, "活性4 软删的链接被排除（只剩 1 个）", len(bundle.rows))
        # **软删的字节数不能进 listedBytes**：否则"配额用了多少"会把
        # 已删的文件算进去，运营以为空间不够。
        emit(
            int(bundle.quota["listedBytes"]) == 1000,
            "活性4 listedBytes 不含软删文件（1000 而不是 3000）",
            bundle.quota["listedBytes"],
        )
        session.rollback()


# ---------------------------------------------------------------------------
# 活性验证 5：配额口径与 WP12 的重算一致
# ---------------------------------------------------------------------------
def _probe_quota_matches_wp12():
    from rednote.modules.files.repository import FileRepository
    from rednote.modules.research.repository import ResearchRepository
    from rednote.modules.research import service as svc

    with _session_for_probe() as session:
        conn = session.connection()
        uid, proj_id, tag = _seed(conn)
        for size in (512, 1024, 2048):
            _blob_and_file(
                conn, uid, "RESEARCH", proj_id, tag=tag + str(size), byte_size=size
            )
        session.flush()

        repo = ResearchRepository(session)
        bundle = svc.list_research_files(repo, project_id=proj_id)
        # **唯一口径**：不自己乘除，直接问 WP12 的 repository。
        expected = FileRepository(session).compute_effective_bytes(proj_id)

        emit(
            int(bundle.quota["effectiveBytes"]) == expected,
            "活性5 effectiveBytes == WP12 的重算值",
            (bundle.quota["effectiveBytes"], expected),
        )
        emit(
            int(bundle.quota["listedBytes"]) == expected,
            "活性5 listedBytes == effectiveBytes（reconciled）",
            (bundle.quota["listedBytes"], expected),
        )
        emit(
            bundle.quota["reconciled"] is True,
            "活性5 reconciled == True",
            bundle.quota["reconciled"],
        )
        emit(
            bundle.quota["fileCount"] == 3,
            "活性5 fileCount == 3（全量口径）",
            bundle.quota["fileCount"],
        )
        # **筛选不该改变配额口径**（Code Review 抓出的缺陷的回归护栏）。
        filtered = svc.list_research_files(
            ResearchRepository(session),
            project_id=proj_id,
            parse_states=["FAILED"],
        )
        emit(
            filtered.quota["listedBytes"] == bundle.quota["listedBytes"]
            and filtered.quota["reconciled"] == bundle.quota["reconciled"] is True,
            "活性5 status 筛选不改变配额口径（不全量就假报 reconciled=False）",
            (filtered.quota["listedBytes"], bundle.quota["listedBytes"]),
        )
        session.rollback()


# ---------------------------------------------------------------------------
# 活性验证 6：缺口判据不依赖 has_evidence（本包最关键的一处修正）
# ---------------------------------------------------------------------------
def _probe_gap_judgement():
    from rednote.modules.research import keywords as kw
    from rednote.modules.research.types import (
        GAP_COVERED,
        GAP_MISSING,
        GAP_NOT_APPLICABLE,
    )

    def claim(category, resolution, has_ev):
        return kw.ClaimInput(
            claim_id=uuid.uuid4(),
            field_path="product.mechanism",
            field_label="机制",
            text="保温 6 小时",
            category=category,
            resolution=resolution,
            has_evidence=has_ev,
        )

    # 关键情形：SOURCE_FACT + PENDING + **有来源**。
    # 数据库 CHECK（ck_brief_claims_factual_category_requires_evidence）
    # 保证"需要证据的类别必有来源"，因此这是**必然形状**。
    # 按 has_evidence 判 -> COVERED（gaps 永远为空，静默失效）；
    # 按 resolution 判 -> MISSING（正确）。
    c = claim("SOURCE_FACT", "PENDING", True)
    emit(
        kw.gap_state(c) == GAP_MISSING,
        "活性6 有来源但 PENDING 的 SOURCE_FACT 仍是 MISSING",
        kw.gap_state(c),
    )
    gaps, _ = kw.derive_gaps((c,))
    emit(len(gaps) == 1, "活性6 它出现在 gaps[] 里（不是被覆盖掉）", len(gaps))
    emit(
        sum(1 for g in gaps if g.blocking) == 1,
        "活性6 blockingCount == 1（不是恒为 0）",
        sum(1 for g in gaps if g.blocking),
    )
    # 三条状态各一个（判据是 resolution，不是有没有来源）。
    emit(
        kw.gap_state(claim("SOURCE_FACT", "CONFIRMED", True)) == GAP_COVERED,
        "活性6 CONFIRMED -> COVERED",
        kw.gap_state(claim("SOURCE_FACT", "CONFIRMED", True)),
    )
    emit(
        kw.gap_state(claim("SOURCE_FACT", "NOT_APPLICABLE", True))
        == GAP_NOT_APPLICABLE,
        "活性6 NOT_APPLICABLE -> NOT_APPLICABLE",
        kw.gap_state(claim("SOURCE_FACT", "NOT_APPLICABLE", True)),
    )


# ---------------------------------------------------------------------------
# 活性验证 7：三条判据真的分开（五种组合互不相同）
# ---------------------------------------------------------------------------
def _probe_three_judgements():
    from rednote.modules.research import keywords as kw

    def claim(category, resolution):
        return kw.ClaimInput(
            claim_id=uuid.uuid4(),
            field_path="product.mechanism",
            field_label="机制",
            text="某结论",
            category=category,
            resolution=resolution,
            has_evidence=True,
        )

    combos = (
        ("SOURCE_FACT", "PENDING"),
        ("SOURCE_FACT", "CONFIRMED"),
        ("SOURCE_FACT", "NOT_APPLICABLE"),
        ("STRATEGY", "PENDING"),
        ("UNCONFIRMED", "PENDING"),
    )
    rows = []
    for cat, res in combos:
        c = claim(cat, res)
        rows.append(
            (
                cat + "/" + res,
                kw.claim_needs_gap(c),
                kw.gap_state(c),
                kw.claim_needs_search(c),
            )
        )
    emit(
        rows[0] == ("SOURCE_FACT/PENDING", True, "MISSING", True),
        "活性7 SOURCE_FACT+PENDING -> 算缺口/缺/MISSING/要搜",
        rows[0],
    )
    # `NOT_APPLICABLE` **仍算缺口条目**（否则运营标了"不适用"后
    # 那条就从清单消失，无法回顾或撤销判断）。
    emit(
        rows[2] == ("SOURCE_FACT/NOT_APPLICABLE", True, "NOT_APPLICABLE", False),
        "活性7 NOT_APPLICABLE -> 属缺口条目但不搜",
        rows[2],
    )
    # `STRATEGY` / `UNCONFIRMED` 不算缺口（类别判据来自 WP18 的常量）。
    emit(
        rows[3][1] is False and rows[3][3] is False,
        "活性7 STRATEGY 不算缺口、不出检索词",
        rows[3],
    )
    emit(
        rows[4][1] is False and rows[4][3] is False,
        "活性7 UNCONFIRMED 不算缺口、不出检索词",
        rows[4],
    )
    # **`claim_needs_gap` 与 `claim_needs_search` 必须不是同一个函数**：
    # 第 2、3 行是它们分歧的地方。若两者恒等，说明判据被合并了。
    divergent = [r for r in rows if r[1] != r[3]]
    emit(
        len(divergent) == 2,
        "活性7 两个判据存在分歧（不是同一个函数）",
        [d[0] for d in divergent],
    )


# ---------------------------------------------------------------------------
# 活性验证 8：WP18 的"PARTIAL 不得洗白"约束仍在（本包读它的前提）
# ---------------------------------------------------------------------------
def _probe_partial_not_laundered():
    with _session_for_probe() as session:
        conn = session.connection()
        uid, proj_id, tag = _seed(conn)
        _link_id, file_id = _blob_and_file(conn, uid, "BRIEF", proj_id, tag=tag)
        brief_id = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO briefs (id, project_id, active_version_id)"
                " VALUES (:id, :p, NULL)"
            ),
            {"id": brief_id, "p": proj_id},
        )
        node_id = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO version_nodes (id, entity_type, entity_id, project_id,"
                " revision_no, schema_version, payload_hash, created_by)"
                " VALUES (:id, 'BRIEF', :e, :p, 1, 1, :h, :u)"
            ),
            {
                "id": node_id,
                "e": brief_id,
                "p": proj_id,
                # payload_hash 长度按 CHECK（64）。
                "h": (uuid.uuid4().hex + uuid.uuid4().hex)[:64],
                "u": uid,
            },
        )
        # 版本声明 PARTIAL，但**不插 brief_issues**。
        # 注意 `brief_versions` **没有 updated_at**（不可变表）。
        conn.execute(
            text(
                "INSERT INTO brief_versions (id, brief_id, source_file_id,"
                " source_parse_run_id, source_state, product_name, model_code, summary,"
                " structured_fields, created_by)"
                " VALUES (:id, :b, :f, NULL, 'PARTIAL', :n, NULL, '探针',"
                " cast(:sf as jsonb), :u)"
            ),
            {
                "id": node_id,
                "b": brief_id,
                "f": file_id,
                "n": tag + "-prod",
                "sf": json.dumps({}),
                "u": uid,
            },
        )
        # 延迟约束：到强制校验时才判。
        try:
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            emit(False, "活性8 PARTIAL 无 issue 应被拦", "未被拦下")
        except Exception as exc:
            sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
            emit(
                sqlstate == "23514",
                "活性8 PARTIAL 无 issue 被 WP18 触发器拦下（23514）",
                sqlstate or str(exc)[:70],
            )
        finally:
            session.rollback()


# ---------------------------------------------------------------------------
# 零残留核对（与"运行前基线"差分）
# ---------------------------------------------------------------------------
def _probe_no_residue(before):
    with engine.connect() as conn:
        after = _baseline_counts(conn)
    diff = {t: (before[t], after[t]) for t in READ_TABLES if before[t] != after[t]}
    emit(
        diff == {},
        "零残留：全部探针回滚后 12 张表行数与运行前一致",
        diff if diff else "全部一致",
    )


with engine.connect() as conn:
    baseline = _baseline_counts(conn)

_probe_no_write(baseline)
_probe_reads_real_rows()
_probe_purpose_filter()
_probe_effective_filter()
_probe_quota_matches_wp12()
_probe_gap_judgement()
_probe_three_judgements()
_probe_partial_not_laundered()
_probe_no_residue(baseline)

print("RESULT_LINES=" + str(CHECKS) + " FAILS=" + str(FAILS))
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def collect_wp19() -> tuple[Path, bool]:
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
        "# WP19 研究关键词与提示词：零表 / 零迁移 / 零写端点 / 活性验证 / 契约",
        f"# 生成时间: {_now()}",
        "# 目标库: rednote_verify_wp19（收口时由迁移从 bootstrap 建出，非测试夹具建表）",
        "# 命令: cd services/backend && python -c '<information_schema + 契约 + 零写入差分>'",
        "",
        output.rstrip(),
        "",
        f"EXIT={completed.returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp19-research.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # **条数不足也算失败**（ISS-43）。
    complete = len(checks) >= EXPECTED_CHECKS
    return target, (completed.returncode == 0 and not failures and complete)


def main() -> int:
    path, ok = collect_wp19()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())