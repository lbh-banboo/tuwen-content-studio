#!/usr/bin/env python
"""一次性、幂等的**依赖边历史回填**：给批 A/B 落地之前就存在的版本补写 `version_dependencies`。

## 它解决什么问题

接缝冻结 §5.16：E1/E2/E3/E6/E7/E8（以及 WP32 的 E10）只在**新写版本**时顺手写边，
此前已存在的版本没有边，WP34 影响图在旧数据上为空（OQ-34-01）。本工具从各表里
**已有的派生源**重算「应有的边」，经 `versions.service.link_dependency` 补写。

## 边口径（必须与在线写入逐条一致，purpose 与方向都不许变）

| 边 | parent -> dependency | 派生源 |
|---|---|---|
| E1 | 内容版本 -> PROMPT 版本 (`GENERATION_PROMPT`) | generation_contexts.prompt_version_id |
| E2 | 内容版本 -> BASELINE (`GENERATION_BASELINE`) | generation_contexts.baseline_id |
| E3 | 内容版本 -> 同内容上一版 (`CONTENT_REVISION`) | version_no > 1 的每个内容版本 |
| E6 | BASELINE -> BRIEF (`BASELINE_BRIEF`) | production_baselines.brief_version_id |
| E7 | BASELINE -> KNOWLEDGE (`BASELINE_KNOWLEDGE`) | baseline_items.knowledge_version_id（去重，含 excluded） |
| E8 | PLAN 节点 -> BASELINE (`PLAN_BASELINE`) | plan_drafts.baseline_id，**仅当指纹吻合** |
| E10 | 批次快照节点 -> PLAN / BASELINE (`BATCH_PLAN`/`BATCH_BASELINE`) | batches.plan_version_id / baseline_id |

### E1/E2 的陷阱：只写给「首个引用该 context 的内容版本」

人工编辑与采纳标题产生的内容版本**复用上一版的 generation_context_id**，在线路径对它们
只写 E3、不写 E1/E2。所以按 context 分组，只把 E1/E2 给 `version_no` 最小（并列取
`created_at`）的那一版——那才是真正由生成产生的版本；再生成会新建 context，
自然落在它自己的首个引用者上。

### E8 的跳过规则

`plan_drafts` 一项目一份且可被改，旧修订提交时的 baseline_id 可能已不可考。因此用
`planning.service.plan_payload(draft)` 重算摘要，与 PLAN 节点的 `payload_hash` 比对：
一致才补；不一致（含草稿缺失 / 重算失败）计入「无法确定」，**绝不猜**。

## 安全门（与 tools/dev/reset_shared_test_db.py 同风格）

1. 只认 `REDNOTE_DATABASE_URL`（刻意不读测试库变量，也没有任何默认 DSN）。
2. 保留库（postgres/template0/template1）拒绝；共享测试库 `rednote_test` 也拒绝，
   除非显式 `--allow-shared-test-db`。
3. 默认只读 dry-run（会话级 READ ONLY 事务）。
4. 真执行需 `--yes --confirm <库名>`，且必须原样等于解析出的库名。
5. 执行前探活跃连接（`check_shared_db_quiet.assert_quiet`）。

## 事务与失败

每 N 条（`--batch-size`，默认 200）一个事务；单条边校验失败（自引用 / 跨项目 / 成环等）
记入失败清单并继续，不中断全局，最终退出码 2。提交失败只回滚该批。

跑法：
    REDNOTE_DATABASE_URL=... python tools/ops/backfill_dependency_edges.py            # dry-run
    REDNOTE_DATABASE_URL=... python tools/ops/backfill_dependency_edges.py --yes --confirm <库名>
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 本脚本在 <repo>/tools/ops/ 下，parents[2] 是仓库根。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_SRC = _REPO_ROOT / "services" / "backend" / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))
_QUALITY_DIR = _REPO_ROOT / "tools" / "quality"
if str(_QUALITY_DIR) not in sys.path:
    sys.path.insert(0, str(_QUALITY_DIR))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from rednote.modules.planning import service as planning_service  # noqa: E402
from rednote.modules.planning.models import PlanDraft  # noqa: E402
from rednote.modules.versions import service as versions_service  # noqa: E402
from rednote.modules.versions.repository import VersionRepository  # noqa: E402

DATABASE_URL_ENV = "REDNOTE_DATABASE_URL"
SHARED_TEST_DATABASE = "rednote_test"
DEFAULT_BATCH_SIZE = 200
DEFAULT_SAMPLE = 5

EXIT_FAILED_EDGES = 2

# purpose 名与在线写入点的常量逐字一致（对账测试用在线路径造数据，漂移会被抓到）。
P_GEN_PROMPT = "GENERATION_PROMPT"
P_GEN_BASELINE = "GENERATION_BASELINE"
P_REVISION = "CONTENT_REVISION"
P_BASELINE_BRIEF = "BASELINE_BRIEF"
P_BASELINE_KNOWLEDGE = "BASELINE_KNOWLEDGE"
P_PLAN_BASELINE = "PLAN_BASELINE"
P_BATCH_PLAN = "BATCH_PLAN"
P_BATCH_BASELINE = "BATCH_BASELINE"

#: 边类别：代号 -> purpose（顺序即写入与报告顺序）。
KINDS: dict[str, str] = {
    "E1": P_GEN_PROMPT,
    "E2": P_GEN_BASELINE,
    "E3": P_REVISION,
    "E6": P_BASELINE_BRIEF,
    "E7": P_BASELINE_KNOWLEDGE,
    "E8": P_PLAN_BASELINE,
    "E10-PLAN": P_BATCH_PLAN,
    "E10-BASELINE": P_BATCH_BASELINE,
}

Edge = tuple[uuid.UUID, uuid.UUID, str]  # (parent, dependency, purpose)


@dataclass
class Expected:
    """应有的边（可确定的）与无法确定的条目。"""

    edges: dict[str, list[Edge]] = field(default_factory=lambda: {k: [] for k in KINDS})
    unresolved: dict[str, list[str]] = field(default_factory=lambda: {k: [] for k in KINDS})


@dataclass
class KindStats:
    expected: int
    existing: int
    missing: list[Edge]
    unresolved: list[str]


@dataclass
class Failure:
    kind: str
    edge: Edge
    reason: str


# ---------------------------------------------------------------------------
# 安全门
# ---------------------------------------------------------------------------


def _database_url() -> str:
    dsn = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not dsn:
        raise SystemExit(f"拒绝执行：环境变量 {DATABASE_URL_ENV} 未设置（本工具不读其它变量、无默认 DSN）。")
    return dsn


def _database_name(dsn: str) -> str | None:
    import check_test_database_target as target_tool

    return target_tool.database_name(dsn)


def _guard_database(database: str | None, *, allow_shared_test_db: bool) -> None:
    import check_test_database_target as target_tool

    if database is None:
        raise SystemExit("拒绝执行：DSN 解析不出库名。")
    if database in target_tool.STATIC_RESERVED_DATABASES:
        raise SystemExit(
            f"拒绝执行：目标库 {database!r} 是保留库"
            f"（{sorted(target_tool.STATIC_RESERVED_DATABASES)}）。"
        )
    if database == SHARED_TEST_DATABASE and not allow_shared_test_db:
        raise SystemExit(
            f"拒绝执行：目标库 {database!r} 是共享测试库；确需回填请显式加 --allow-shared-test-db。"
        )


def _assert_quiet(dsn: str) -> None:
    import check_shared_db_quiet

    check_shared_db_quiet.assert_quiet(dsn, scope="backfill_dependency_edges")


# ---------------------------------------------------------------------------
# 重算「应有的边」（纯读）
# ---------------------------------------------------------------------------


def _proj_clause(column: str, project_id: uuid.UUID | None) -> str:
    return "" if project_id is None else f" AND {column} = :pid"


def _params(project_id: uuid.UUID | None) -> dict[str, Any]:
    return {} if project_id is None else {"pid": project_id}


def collect_expected(session: Session, *, project_id: uuid.UUID | None = None) -> Expected:
    out = Expected()
    prm = _params(project_id)

    # E1/E2：按 generation_context 分组，只给首个引用者（version_no 最小，并列取 created_at）。
    rows = session.execute(
        text(
            "SELECT DISTINCT ON (cv.generation_context_id)"
            " cv.id, gc.prompt_version_id, gc.baseline_id"
            " FROM content_versions cv"
            " JOIN content_items ci ON ci.id = cv.content_item_id"
            " JOIN generation_contexts gc ON gc.id = cv.generation_context_id"
            " WHERE cv.generation_context_id IS NOT NULL"
            + _proj_clause("ci.project_id", project_id)
            + " ORDER BY cv.generation_context_id, cv.version_no, cv.created_at, cv.id"
        ),
        prm,
    ).fetchall()
    for version_id, prompt_id, baseline_id in rows:
        if prompt_id is not None:
            out.edges["E1"].append((version_id, prompt_id, P_GEN_PROMPT))
        if baseline_id is not None:
            out.edges["E2"].append((version_id, baseline_id, P_GEN_BASELINE))

    # E3：version_no > 1 的版本 -> 紧邻前一版；前一版缺失（版本号不连续）则无法确定。
    rows = session.execute(
        text(
            "SELECT id, version_no, prev_id, prev_no FROM ("
            " SELECT cv.id, cv.version_no, ci.project_id,"
            "  lag(cv.id) OVER w AS prev_id, lag(cv.version_no) OVER w AS prev_no"
            " FROM content_versions cv JOIN content_items ci ON ci.id = cv.content_item_id"
            " WINDOW w AS (PARTITION BY cv.content_item_id ORDER BY cv.version_no)"
            ") t WHERE version_no > 1"
            + _proj_clause("project_id", project_id)
            + " ORDER BY id"
        ),
        prm,
    ).fetchall()
    for version_id, version_no, prev_id, prev_no in rows:
        if prev_id is not None and prev_no == version_no - 1:
            out.edges["E3"].append((version_id, prev_id, P_REVISION))
        else:
            out.unresolved["E3"].append(f"内容版本 {version_id} v{version_no}：找不到紧邻前一版")

    # E6
    for baseline_id, brief_id in session.execute(
        text(
            "SELECT id, brief_version_id FROM production_baselines"
            " WHERE brief_version_id IS NOT NULL"
            + _proj_clause("project_id", project_id)
            + " ORDER BY id"
        ),
        prm,
    ).fetchall():
        out.edges["E6"].append((baseline_id, brief_id, P_BASELINE_BRIEF))

    # E7：去重，含 excluded 条目（在线 `_link_baseline_dependencies` 同口径）。
    for baseline_id, knowledge_id in session.execute(
        text(
            "SELECT DISTINCT bi.baseline_id, bi.knowledge_version_id"
            " FROM baseline_items bi JOIN production_baselines b ON b.id = bi.baseline_id"
            " WHERE bi.knowledge_version_id IS NOT NULL"
            + _proj_clause("b.project_id", project_id)
            + " ORDER BY bi.baseline_id, bi.knowledge_version_id"
        ),
        prm,
    ).fetchall():
        out.edges["E7"].append((baseline_id, knowledge_id, P_BASELINE_KNOWLEDGE))

    _collect_e8(session, out, project_id=project_id)
    _collect_e10(session, out, project_id=project_id)
    return out


def _collect_e8(session: Session, out: Expected, *, project_id: uuid.UUID | None) -> None:
    """E8：PLAN 链节点（entity_id = project_id）；指纹吻合才补，否则无法确定。"""
    nodes = session.execute(
        text(
            "SELECT id, entity_id, revision_no, payload_hash FROM version_nodes"
            " WHERE entity_type = 'PLAN' AND entity_id = project_id"
            + _proj_clause("project_id", project_id)
            + " ORDER BY entity_id, revision_no"
        ),
        _params(project_id),
    ).fetchall()
    drafts: dict[uuid.UUID, tuple[str | None, uuid.UUID | None]] = {}
    for node_id, entity_id, revision_no, payload_hash in nodes:
        label = f"PLAN 节点 {node_id}（项目 {entity_id} 修订 {revision_no}）"
        if entity_id not in drafts:
            drafts[entity_id] = _draft_fingerprint(session, entity_id)
        recomputed, baseline_id = drafts[entity_id]
        if recomputed is None:
            out.unresolved["E8"].append(f"{label}：草稿缺失或无法重算摘要")
        elif recomputed != payload_hash:
            out.unresolved["E8"].append(f"{label}：当前草稿摘要与节点 payload_hash 不一致（草稿已被改）")
        elif baseline_id is not None:
            out.edges["E8"].append((node_id, baseline_id, P_PLAN_BASELINE))


def _draft_fingerprint(
    session: Session, project_id: uuid.UUID
) -> tuple[str | None, uuid.UUID | None]:
    draft = session.query(PlanDraft).filter(PlanDraft.project_id == project_id).one_or_none()
    if draft is None:
        return None, None
    try:
        payload = planning_service.plan_payload(
            draft,
            scene_result=planning_service.dimension_allocation(
                draft, planning_service.DIMENSION_SCENE
            ),
            form_result=planning_service.dimension_allocation(
                draft, planning_service.DIMENSION_FORM
            ),
        )
        return versions_service.canonical_payload_hash(payload), draft.baseline_id
    except Exception:  # noqa: BLE001 - 重算失败即「无法确定」，不猜
        return None, None


def _collect_e10(session: Session, out: Expected, *, project_id: uuid.UUID | None) -> None:
    rows = session.execute(
        text(
            "SELECT b.id, b.plan_version_id, b.baseline_id, n.id"
            " FROM batches b LEFT JOIN version_nodes n"
            "  ON n.entity_type = 'PLAN' AND n.entity_id = b.id AND n.revision_no = 1"
            " WHERE true"
            + _proj_clause("b.project_id", project_id)
            + " ORDER BY b.id"
        ),
        _params(project_id),
    ).fetchall()
    for batch_id, plan_version_id, baseline_id, snapshot_id in rows:
        if snapshot_id is None:
            for kind in ("E10-PLAN", "E10-BASELINE"):
                out.unresolved[kind].append(f"批次 {batch_id}：无 PLAN 快照节点")
            continue
        out.edges["E10-PLAN"].append((snapshot_id, plan_version_id, P_BATCH_PLAN))
        out.edges["E10-BASELINE"].append((snapshot_id, baseline_id, P_BATCH_BASELINE))


def _existing_edges(session: Session, *, project_id: uuid.UUID | None) -> set[Edge]:
    join = ""
    where = ""
    if project_id is not None:
        join = " JOIN version_nodes n ON n.id = d.parent_version_id"
        where = " AND n.project_id = :pid"
    rows = session.execute(
        text(
            "SELECT d.parent_version_id, d.dependency_version_id, d.purpose"
            " FROM version_dependencies d" + join + " WHERE d.purpose = ANY(:purposes)" + where
        ),
        {"purposes": list(KINDS.values()), **_params(project_id)},
    ).fetchall()
    return {(r[0], r[1], r[2]) for r in rows}


def build_report(engine, *, project_id: uuid.UUID | None = None) -> tuple[Expected, dict[str, KindStats]]:
    """只读（会话级 READ ONLY）：算出每类边的应有 / 已有 / 将新增 / 无法确定。"""
    with Session(engine) as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        expected = collect_expected(session, project_id=project_id)
        existing = _existing_edges(session, project_id=project_id)
        session.rollback()
    stats: dict[str, KindStats] = {}
    for kind in KINDS:
        edges = list(dict.fromkeys(expected.edges[kind]))
        present = [e for e in edges if e in existing]
        stats[kind] = KindStats(
            expected=len(edges),
            existing=len(present),
            missing=[e for e in edges if e not in existing],
            unresolved=expected.unresolved[kind],
        )
    return expected, stats


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def apply_edges(
    engine, stats: dict[str, KindStats], *, batch_size: int
) -> tuple[int, int, list[Failure]]:
    """分批写入。返回 (新建数, 并发已存在数, 失败清单)。"""
    todo = [(kind, edge) for kind in KINDS for edge in stats[kind].missing]
    created = already = 0
    failures: list[Failure] = []
    for start in range(0, len(todo), batch_size):
        chunk = todo[start : start + batch_size]
        chunk_created = chunk_already = 0
        chunk_failed: list[Failure] = []
        session = Session(engine)
        try:
            repo = VersionRepository(session)
            for kind, (parent, dependency, purpose) in chunk:
                nested = session.begin_nested()
                try:
                    link = versions_service.link_dependency(
                        repo,
                        parent_version_id=parent,
                        dependency_version_id=dependency,
                        purpose=purpose,
                    )
                    nested.commit()
                except Exception as exc:  # noqa: BLE001 - 单条失败记录后继续
                    nested.rollback()
                    chunk_failed.append(Failure(kind, (parent, dependency, purpose), _reason(exc)))
                    continue
                if link.created:
                    chunk_created += 1
                else:
                    chunk_already += 1
            session.commit()
        except Exception as exc:  # noqa: BLE001 - 提交失败：只回滚该批
            session.rollback()
            failures.extend(
                Failure(kind, edge, f"该批提交失败已整批回滚：{_reason(exc)}") for kind, edge in chunk
            )
            continue
        finally:
            session.close()
        created += chunk_created
        already += chunk_already
        failures.extend(chunk_failed)
    return created, already, failures


def _reason(exc: Exception) -> str:
    details = getattr(exc, "details", None)
    reason = details.get("reason") if isinstance(details, dict) else None
    return f"{type(exc).__name__}: {reason or exc}"[:300]


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def print_report(stats: dict[str, KindStats], *, tag: str, sample: int) -> int:
    """打印每类边统计与样例，返回「将新增」总数。"""
    total_new = 0
    print(f"[{tag}] 边类别 | 应有 | 已有 | 将新增 | 无法确定")
    for kind, purpose in KINDS.items():
        s = stats[kind]
        total_new += len(s.missing)
        print(f"  {kind:<12} {purpose:<20} {s.expected:>6} {s.existing:>6} {len(s.missing):>6} {len(s.unresolved):>6}")
        for parent, dependency, _ in s.missing[:sample]:
            print(f"      新增样例：{parent} -> {dependency}")
        for line in s.unresolved[:sample]:
            print(f"      无法确定：{line}")
    return total_new


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="依赖边历史回填（一次性、幂等；默认只读 dry-run）")
    parser.add_argument("--yes", action="store_true", help="真正写库（默认 dry-run）")
    parser.add_argument("--confirm", metavar="数据库名", help="必须原样等于目标库名")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="每批事务条数")
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE, help="每类打印的样例条数")
    parser.add_argument("--project-id", type=uuid.UUID, help="只处理该项目（默认全库）")
    parser.add_argument(
        "--allow-shared-test-db", action="store_true", help="允许目标库为共享测试库 rednote_test"
    )
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        raise SystemExit("拒绝执行：--batch-size 必须 >= 1。")

    dsn = _database_url()
    database = _database_name(dsn)
    print(f"目标库（{DATABASE_URL_ENV}）：{database}")
    _guard_database(database, allow_shared_test_db=args.allow_shared_test_db)

    if args.yes and args.confirm != database:
        raise SystemExit(
            f"拒绝执行：--confirm 必须原样等于目标库名 {database!r}（收到 {args.confirm!r}）。"
        )
    if args.yes:
        _assert_quiet(dsn)

    engine = create_engine(dsn, pool_pre_ping=True)
    try:
        _, stats = build_report(engine, project_id=args.project_id)
        if not args.yes:
            print_report(stats, tag="dry-run", sample=args.sample)
            print("\n要真正执行，请加 --yes --confirm <上面打印的库名>")
            return 0

        print_report(stats, tag="执行前", sample=args.sample)
        created, already, failures = apply_edges(engine, stats, batch_size=args.batch_size)
        print(f"\n写入完成：新建 {created}，并发已存在 {already}，失败 {len(failures)}")
        for f in failures:
            print(f"  [失败] {f.kind} {f.edge[0]} -> {f.edge[1]}：{f.reason}")

        _, after = build_report(engine, project_id=args.project_id)
        remaining = print_report(after, tag="执行后", sample=args.sample)
        if remaining == len(failures):
            print("[PASS] 幂等自检：立即再 dry-run，除失败条目外新增 0。")
        else:
            print(f"[FAIL] 幂等自检：再 dry-run 仍有 {remaining} 条待新增（失败 {len(failures)} 条）。")
            return EXIT_FAILED_EDGES
        return EXIT_FAILED_EDGES if failures else 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
