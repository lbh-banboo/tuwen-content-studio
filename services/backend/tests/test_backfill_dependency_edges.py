"""依赖边历史回填工具（tools/ops/backfill_dependency_edges.py）的真库测试。

造数走**在线路径**（content / baseline / planning 的真实服务函数；批次因在线创建依赖整套
计费与试产关口，改为按 `batches/service.py` 的写边步骤手工复现），记下在线路径写出的边集合；
再用 SQL 只删边、不动版本，模拟「批 A/B 落地之前的旧库」；最后跑回填，逐条对账。

隔离：工具支持 `--project-id`，所有用例只处理自己造的三个项目，不受库内其它数据影响。
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from test_baseline_wp22 import (
    BaselineEnv,
    _baseline_draft,
    _create_baseline,
    _fact_item,
    _make_brief_node,
    _make_ready,
    _own_knowledge,
)
from test_baseline_wp22 import _seed_project as _seed_baseline_project
from test_content_dependency_edges import (
    _edges_from,
    _generate,
    _generator,
    _version_ids,
)
from test_content_wp26_exec import (
    ExecEnv,
    _run,
    env,  # noqa: F401
    stub_billing,  # noqa: F401
)
from test_planning_wp24 import (
    PlanningEnv,
    _insert_baseline,
    _repo,
    _repo_with_versions,
    _save,
    _seed_submittable_draft,
)
from test_planning_wp24 import _seed_project as _seed_plan_project

from rednote.modules.baseline.baseline_types import INCLUSION_EXCLUDED
from rednote.modules.baseline.repository import BaselineRepository
from rednote.modules.content import service as c_service
from rednote.modules.content.repository import ContentRepository
from rednote.modules.planning import service as pl_service
from rednote.modules.versions import service as versions_service
from rednote.modules.versions.repository import VersionRepository
from rednote.platform.database import session_scope

# 回填工具会写库：本模块一律落在自建私有库，不碰共享库 rednote_test（守卫会拒绝）。
# 残留判据/全量复跑会把 REDNOTE_TEST_DATABASE_URL 设成共享库，所以用 autouse 夹具无条件覆盖，
# 且只作用于本文件的用例（monkeypatch 自动还原，不污染同进程里的其它测试文件）。
@pytest.fixture(autouse=True)
def _private_backfill_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """只换库名，角色/口令/host/端口一律照抄调用方给的基准 DSN。

    ⚠️ 2026-11-09：原实现把整串
    `postgresql+psycopg://<本机角色>@localhost:5432/rednote_test_backfill_v2`
    写死并**无条件覆盖**环境变量；开源脱敏把本机角色换成项目名后，本机与
    干净 CI runner 上都不存在该 role ⇒ 本文件 10 条用例全部 setup error
    `role ... does not exist`。这正是 ISS-108 的形状（本机口径被当通用口径），
    与 `tools/ops/drill_db.py::dsn()` 的处置同口径。
    """
    import os

    from sqlalchemy.engine import make_url

    base = (
        os.environ.get("REDNOTE_TEST_DATABASE_URL")
        or os.environ.get("REDNOTE_DATABASE_URL")
        or "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"
    )
    monkeypatch.setenv(
        "REDNOTE_TEST_DATABASE_URL",
        make_url(base)
        .set(database="rednote_test_backfill_v2")
        .render_as_string(hide_password=False),
    )


_TOOLS_OPS = Path(__file__).resolve().parents[3] / "tools" / "ops"
if str(_TOOLS_OPS) not in sys.path:
    sys.path.insert(0, str(_TOOLS_OPS))

import backfill_dependency_edges as tool  # noqa: E402

PURPOSES = list(tool.KINDS.values())


@dataclass
class World:
    settings: Any
    dsn: str
    database: str
    content_project: uuid.UUID
    baseline_project: uuid.UUID
    plan_project: uuid.UUID
    content_a: uuid.UUID  # v1 生成 / v2 采纳 / v3 编辑
    content_b: uuid.UUID  # v1 生成 / v2 再生成
    plan_rev1: uuid.UUID
    plan_rev2: uuid.UUID
    plan_baseline: uuid.UUID

    @property
    def projects(self) -> list[uuid.UUID]:
        return [self.content_project, self.baseline_project, self.plan_project]


def _sql(settings: Any, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    with session_scope(settings) as session:
        result = session.execute(text(sql), params or {})
        return list(result.fetchall()) if result.returns_rows else []


def edge_set(w: World, projects: list[uuid.UUID] | None = None) -> set[tool.Edge]:
    rows = _sql(
        w.settings,
        "SELECT d.parent_version_id, d.dependency_version_id, d.purpose FROM version_dependencies d"
        " JOIN version_nodes n ON n.id = d.parent_version_id"
        " WHERE n.project_id = ANY(:p) AND d.purpose = ANY(:purposes)",
        {"p": projects or w.projects, "purposes": PURPOSES},
    )
    return {(r[0], r[1], r[2]) for r in rows}


def delete_edges(w: World) -> None:
    """只删边，不动版本——模拟批 A/B 之前的旧库。表有不可变触发器，仅在本事务内临时停用。"""
    trigger = "trg_version_dependencies_immutable"
    with session_scope(w.settings) as session:
        session.execute(text(f"ALTER TABLE version_dependencies DISABLE TRIGGER {trigger}"))
        session.execute(
            text(
                "DELETE FROM version_dependencies WHERE purpose = ANY(:purposes) AND parent_version_id IN"
                " (SELECT id FROM version_nodes WHERE project_id = ANY(:p))"
            ),
            {"p": w.projects, "purposes": PURPOSES},
        )
        session.execute(text(f"ALTER TABLE version_dependencies ENABLE TRIGGER {trigger}"))


def _build_content(env: ExecEnv, monkeypatch: pytest.MonkeyPatch) -> tuple[uuid.UUID, uuid.UUID]:
    content_a, _, _ = _generate(env, monkeypatch, with_prompt=True, with_baseline=True)
    (v1,) = _version_ids(env, content_a)
    candidate_id = _sql(
        env.settings, "SELECT id FROM title_candidates WHERE content_version_id = :v", {"v": v1}
    )[0][0]
    with session_scope(env.settings) as session:
        c_service.adopt_title_candidate(
            ContentRepository(session),
            version_repo=VersionRepository(session),
            content_id=content_a,
            candidate_id=candidate_id,
            created_by=env.owner_id,
        )
    lock = _sql(env.settings, "SELECT lock_version FROM content_items WHERE id = :c", {"c": content_a})
    with session_scope(env.settings) as session:
        c_service.append_edited_version(
            ContentRepository(session),
            version_repo=VersionRepository(session),
            baseline_repo=BaselineRepository(session),
            content_id=content_a,
            expected_lock_version=int(lock[0][0]),
            title="编辑后的标题",
            created_by=env.owner_id,
        )
    content_b, _, _ = _generate(env, monkeypatch, with_prompt=True, with_baseline=False)
    _run(env, content_b, _generator())  # 再生成 v2：新 context、写 E1/E2/E3
    return content_a, content_b


def _build_baseline(env: ExecEnv) -> uuid.UUID:
    brand, product, project = _seed_baseline_project(env.settings, owner_id=env.owner_id)
    benv = BaselineEnv(
        settings=env.settings,
        project_id=project,
        brand_id=brand,
        product_id=product,
        owner_id=env.owner_id,
        admin_id=env.owner_id,
        other_brand_id=brand,
        other_product_id=product,
        other_project_id=project,
    )
    brief = _make_brief_node(benv)
    _make_ready(benv, brief_version_id=brief)
    a1, v1 = _own_knowledge(benv)
    a2, v2 = _own_knowledge(benv)
    a3, v3 = _own_knowledge(benv)
    _create_baseline(
        benv,
        _baseline_draft(
            benv,
            brief_version_id=brief,
            selected=(
                _fact_item(ref_label="事实 1", knowledge_asset_id=a1, knowledge_version_id=v1),
                _fact_item(ref_label="事实 2", knowledge_asset_id=a2, knowledge_version_id=v2),
                _fact_item(
                    ref_label="被排除",
                    knowledge_asset_id=a3,
                    knowledge_version_id=v3,
                    inclusion=INCLUSION_EXCLUDED,
                    exclusion_reason="测试排除项",
                ),
            ),
        ),
    )
    return project


def _build_plan(env: ExecEnv) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    brand, product, project = _seed_plan_project(env.settings, owner_id=env.owner_id)
    penv = PlanningEnv(
        settings=env.settings,
        project_id=project,
        brand_id=brand,
        product_id=product,
        owner_id=env.owner_id,
        admin_id=env.owner_id,
        other_brand_id=brand,
        other_product_id=product,
        other_project_id=project,
        outsider_id=env.owner_id,
    )

    def submit(lock_version: int) -> Any:
        with _repo_with_versions(penv) as (repo, version_repo):
            return pl_service.submit_draft(
                repo,
                version_repo,
                project_id=project,
                expected_lock_version=lock_version,
                actor_id=env.owner_id,
            )

    baseline_id = _insert_baseline(penv, project_id=project)
    _save(penv, baseline_id=baseline_id)
    first = submit(_seed_submittable_draft(penv, total=10))
    # 草稿一项目一份且可变：回到 EDITING 改总量，提交第二修订——第一修订的指纹从此对不上。
    _sql(env.settings, "UPDATE plan_drafts SET state = 'EDITING' WHERE project_id = :p", {"p": project})
    with _repo(penv) as repo:
        lock = pl_service.require_draft(repo, project_id=project).lock_version
    _save(penv, total_quantity=12, expected_lock_version=lock)
    with _repo(penv) as repo:
        lock = pl_service.require_draft(repo, project_id=project).lock_version
    second = submit(lock)
    assert (first.revision_no, second.revision_no) == (1, 2)
    return project, first.node_id, second.node_id, baseline_id


def _build_batch(env: ExecEnv, project: uuid.UUID, plan_node: uuid.UUID, baseline_id: uuid.UUID) -> None:
    """按 batches/service.py 步 9 手工复现：PLAN 快照节点(batch.id, rev 1) + BATCH_PLAN/BATCH_BASELINE。"""
    batch_id, content_id = uuid.uuid4(), uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO content_items (id, project_id, state, current_version_no, lock_version)"
                " VALUES (:c, :p, 'DRAFT', 0, 0)"
            ),
            {"c": content_id, "p": project},
        )
        session.execute(
            text(
                "INSERT INTO batches (id, project_id, batch_kind, seq_no, quantity, state,"
                " plan_version_id, baseline_id, allocation, limit_applied, limit_source,"
                " estimate_refs, idempotency_key, request_hash)"
                " VALUES (:id, :p, 'SCALE', 1, 1, 'SUBMITTED', :pv, :bl, '{}'::jsonb, 30, 'GLOBAL',"
                " '[]'::jsonb, :idem, 'h')"
            ),
            {"id": batch_id, "p": project, "pv": plan_node, "bl": baseline_id, "idem": uuid.uuid4().hex},
        )
        session.execute(
            text(
                "INSERT INTO batch_items (id, batch_id, ordinal, content_item_id)"
                " VALUES (gen_random_uuid(), :b, 1, :c)"
            ),
            {"b": batch_id, "c": content_id},
        )
        repo = VersionRepository(session)
        snapshot = versions_service.register_version(
            repo,
            entity_type="PLAN",
            entity_id=batch_id,
            payload={"probe": "batch-snapshot", "n": uuid.uuid4().hex},
            project_id=project,
            revision_no=1,
        )
        for dep, purpose in ((plan_node, "BATCH_PLAN"), (baseline_id, "BATCH_BASELINE")):
            versions_service.link_dependency(
                repo, parent_version_id=snapshot.node_id, dependency_version_id=dep, purpose=purpose
            )


@pytest.fixture
def world(env: ExecEnv, stub_billing: None, monkeypatch: pytest.MonkeyPatch) -> World:  # noqa: F811
    content_a, content_b = _build_content(env, monkeypatch)
    baseline_project = _build_baseline(env)
    plan_project, rev1, rev2, plan_baseline = _build_plan(env)
    _build_batch(env, plan_project, rev2, plan_baseline)
    dsn = str(env.settings.database_url)
    database = tool._database_name(dsn)
    assert database is not None
    # 工具的活跃连接探测会把本进程测试池里的连接也算成「他人」，这里桩掉；拒绝路径另有用例。
    monkeypatch.setattr(tool, "_assert_quiet", lambda _dsn: None)
    monkeypatch.setenv(tool.DATABASE_URL_ENV, dsn)
    return World(
        settings=env.settings,
        dsn=dsn,
        database=database,
        content_project=env.project_id,
        baseline_project=baseline_project,
        plan_project=plan_project,
        content_a=content_a,
        content_b=content_b,
        plan_rev1=rev1,
        plan_rev2=rev2,
        plan_baseline=plan_baseline,
    )


def run_all(w: World, *extra: str, execute: bool = True) -> list[int]:
    codes = []
    for project in w.projects:
        args = ["--project-id", str(project), *extra]
        if execute:
            args += ["--yes", "--confirm", w.database]
        codes.append(tool.main(args))
    return codes


def report(w: World, projects: list[uuid.UUID] | None = None) -> dict[str, tool.KindStats]:
    """合并多个项目的统计（只读）。"""
    merged: dict[str, tool.KindStats] = {
        k: tool.KindStats(0, 0, [], []) for k in tool.KINDS
    }
    engine = tool.create_engine(w.dsn)
    try:
        for project in projects or w.projects:
            _, stats = tool.build_report(engine, project_id=project)
            for kind, s in stats.items():
                m = merged[kind]
                m.expected += s.expected
                m.existing += s.existing
                m.missing += s.missing
                m.unresolved += s.unresolved
    finally:
        engine.dispose()
    return merged


# ---------------------------------------------------------------------------


def test_backfill_reproduces_online_edges_exactly(world: World) -> None:
    online = edge_set(world)
    assert {p for _, _, p in online} == set(PURPOSES), "造数应覆盖全部 8 类 purpose"

    delete_edges(world)
    assert edge_set(world) == set()

    assert run_all(world) == [0, 0, 0]

    # E8 的 rev1：草稿已被改，指纹对不上，工具必须跳过而不是猜。
    skipped = {(world.plan_rev1, world.plan_baseline, "PLAN_BASELINE")}
    assert skipped <= online
    assert edge_set(world) == online - skipped


def test_edited_and_adopted_versions_get_only_e3(world: World) -> None:
    exec_env = world.settings
    ids = _sql(exec_env, "SELECT id, generation_context_id FROM content_versions"
               " WHERE content_item_id = :c ORDER BY version_no", {"c": world.content_a})
    (v1, ctx1), (v2, ctx2), (v3, ctx3) = ids
    assert ctx1 == ctx2 == ctx3, "陷阱前提：采纳/编辑版复用上一版的 generation_context_id"

    delete_edges(world)
    run_all(world)

    def purposes(v: uuid.UUID) -> list[str]:
        rows = _sql(world.settings, "SELECT purpose FROM version_dependencies WHERE parent_version_id = :v"
                    " ORDER BY purpose", {"v": v})
        return [r[0] for r in rows]

    assert purposes(v1) == ["GENERATION_BASELINE", "GENERATION_PROMPT"]
    assert purposes(v2) == ["CONTENT_REVISION"]
    assert purposes(v3) == ["CONTENT_REVISION"]
    assert _sql(world.settings, "SELECT dependency_version_id FROM version_dependencies"
                " WHERE parent_version_id = :v", {"v": v3})[0][0] == v2

    # 再生成的 v2 自带新 context：E1/E2 + E3 都有。
    b1, b2 = _sql(world.settings, "SELECT id FROM content_versions WHERE content_item_id = :c"
                  " ORDER BY version_no", {"c": world.content_b})
    assert sorted(p for p in purposes(b2[0])) == [
        "CONTENT_REVISION", "GENERATION_BASELINE", "GENERATION_PROMPT"]
    assert "CONTENT_REVISION" not in purposes(b1[0])


def test_e8_fingerprint_mismatch_is_skipped_and_listed(world: World) -> None:
    delete_edges(world)
    stats = report(world)["E8"]
    assert stats.expected == 1 and len(stats.missing) == 1
    assert stats.missing[0][0] == world.plan_rev2
    assert len(stats.unresolved) == 1
    assert str(world.plan_rev1) in stats.unresolved[0]


def test_stats_columns_and_e10(world: World) -> None:
    online = edge_set(world)
    delete_edges(world)
    before = report(world)
    assert all(s.existing == 0 for s in before.values())
    assert sum(s.expected for s in before.values()) == len(online) - 1  # 不含被跳过的 rev1
    assert before["E10-PLAN"].expected == 1 and before["E10-BASELINE"].expected == 1
    assert before["E7"].expected == 3, "含 excluded 条目的知识版本"
    # 无批次的项目：E10 为 0
    assert report(world, [world.content_project])["E10-PLAN"].expected == 0

    run_all(world)
    after = report(world)
    assert all(not s.missing for s in after.values())
    assert all(s.existing == s.expected for s in after.values())


def test_backfill_is_idempotent(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    delete_edges(world)
    run_all(world)
    first = edge_set(world)
    capsys.readouterr()
    assert run_all(world) == [0, 0, 0]
    assert edge_set(world) == first
    assert "[PASS] 幂等自检" in capsys.readouterr().out
    assert sum(len(s.missing) for s in report(world).values()) == 0


def test_dry_run_writes_nothing(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    delete_edges(world)
    capsys.readouterr()
    assert run_all(world, execute=False) == [0, 0, 0]
    assert edge_set(world) == set()
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "GENERATION_PROMPT" in out and "无法确定" in out


def test_gates_refuse(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    delete_edges(world)
    pid = ["--project-id", str(world.content_project)]
    # 缺 --confirm / 库名不符
    with pytest.raises(SystemExit):
        tool.main([*pid, "--yes"])
    with pytest.raises(SystemExit):
        tool.main([*pid, "--yes", "--confirm", world.database + "_x"])
    # 保留库 / 共享测试库（默认拒绝）
    prefix = world.dsn.rsplit("/", 1)[0]
    for database in ("postgres", "template1", "rednote_test"):
        monkeypatch.setenv(tool.DATABASE_URL_ENV, f"{prefix}/{database}")
        with pytest.raises(SystemExit):
            tool.main([*pid, "--yes", "--confirm", database])
        with pytest.raises(SystemExit):
            tool.main(pid)  # dry-run 同样拒绝
    # 只认 REDNOTE_DATABASE_URL
    monkeypatch.delenv(tool.DATABASE_URL_ENV)
    with pytest.raises(SystemExit):
        tool.main(pid)
    # 显式开关才放行共享测试库（只验证闸门，不连库）
    tool._guard_database("rednote_test", allow_shared_test_db=True)
    with pytest.raises(SystemExit):
        tool._guard_database("rednote_test", allow_shared_test_db=False)
    assert edge_set(world) == set()


def test_busy_database_refuses(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    delete_edges(world)

    def busy(_dsn: str) -> None:
        raise RuntimeError("目标库上有他人连接")

    monkeypatch.setattr(tool, "_assert_quiet", busy)
    with pytest.raises(RuntimeError):
        tool.main(["--yes", "--confirm", world.database, "--project-id", str(world.content_project)])
    assert edge_set(world) == set()


def test_single_edge_failure_does_not_abort_and_exit_nonzero(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    online = edge_set(world)
    v1, v2 = [r[0] for r in _sql(world.settings, "SELECT id FROM content_versions WHERE content_item_id = :c"
                                 " ORDER BY version_no LIMIT 2", {"c": world.content_a})]
    delete_edges(world)
    # 旧库里残留一条反向边 v1 -> v2：补 E3(v2 -> v1) 会成环，link_dependency 拒绝。
    _sql(world.settings, "INSERT INTO version_dependencies (parent_version_id, dependency_version_id, purpose)"
         " VALUES (:a, :b, 'CONTENT_REVISION')", {"a": v1, "b": v2})
    capsys.readouterr()

    codes = run_all(world, "--batch-size", "1")
    assert codes[0] == tool.EXIT_FAILED_EDGES
    assert codes[1:] == [0, 0]
    assert "dependency_cycle" in capsys.readouterr().out

    skipped = {(world.plan_rev1, world.plan_baseline, "PLAN_BASELINE"), (v2, v1, "CONTENT_REVISION")}
    assert edge_set(world) == (online - skipped) | {(v1, v2, "CONTENT_REVISION")}


def test_commit_failure_rolls_back_only_that_batch(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    delete_edges(world)
    project = world.baseline_project
    expected = report(world, [project])
    total = sum(len(s.missing) for s in expected.values())
    assert total >= 3

    original_session = tool.Session
    state = {"n": 0}

    class FlakySession(tool.Session):  # type: ignore[misc]
        def commit(self) -> None:
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("commit boom")
            super().commit()

    monkeypatch.setattr(tool, "Session", FlakySession)
    capsys.readouterr()
    code = tool.main(["--project-id", str(project), "--yes", "--confirm", world.database,
                      "--batch-size", "1"])
    assert code == tool.EXIT_FAILED_EDGES
    assert "整批回滚" in capsys.readouterr().out
    assert len(edge_set(world, [project])) == total - 1

    monkeypatch.setattr(tool, "Session", original_session)
    assert tool.main(["--project-id", str(project), "--yes", "--confirm", world.database]) == 0
    assert len(edge_set(world, [project])) == total
