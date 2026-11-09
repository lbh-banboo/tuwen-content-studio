"""planning 依赖边 E8（PLAN -> BASELINE，purpose=PLAN_BASELINE）的真库测试。

边由 `submit_draft` 在 PLAN 版本注册的同一事务里经 `link_dependency` 写入，
供 WP34 回退影响图遍历。parent 是 PLAN **节点 id**（不是 entity_id=project_id）。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

# 复用 WP24 测试的夹具与造数 helper（同一套 env / 项目 / 基线造法）。
from test_planning_wp24 import (  # noqa: F401
    PlanningEnv,
    _insert_baseline,
    _raw,
    _repo,
    _repo_with_versions,
    _save,
    _scalar,
    _seed_submittable_draft,
    env,
)

from rednote.modules.planning import service as pl_service
from rednote.modules.planning.types import DRAFT_STATE_EDITING
from rednote.modules.versions import service as versions_service
from rednote.modules.versions.models import ENTITY_TYPE_PLAN
from rednote.platform.errors import ApiError

PURPOSE = "PLAN_BASELINE"


def _submit(env: PlanningEnv, lock_version: int) -> Any:
    with _repo_with_versions(env) as (repo, version_repo):
        return pl_service.submit_draft(
            repo,
            version_repo,
            project_id=env.project_id,
            expected_lock_version=lock_version,
            actor_id=env.owner_id,
        )


def _bind_baseline(env: PlanningEnv, baseline_id: uuid.UUID) -> None:
    _save(env, baseline_id=baseline_id)


def _edges_from(env: PlanningEnv, parent_id: uuid.UUID) -> list[Any]:
    return _raw(
        env,
        "SELECT dependency_version_id, purpose FROM version_dependencies"
        " WHERE parent_version_id = :p",
        {"p": str(parent_id)},
    )


def _plan_node_count(env: PlanningEnv) -> int:
    return int(
        _scalar(
            env,
            "SELECT count(*) FROM version_nodes"
            " WHERE entity_type = :t AND entity_id = :e",
            {"t": ENTITY_TYPE_PLAN, "e": str(env.project_id)},
        )
        or 0
    )


def _edge_count_for_project(env: PlanningEnv) -> int:
    return int(
        _scalar(
            env,
            "SELECT count(*) FROM version_dependencies d"
            " JOIN version_nodes n ON n.id = d.parent_version_id"
            " WHERE n.project_id = :p",
            {"p": str(env.project_id)},
        )
        or 0
    )


def test_submit_writes_plan_baseline_edge(env: PlanningEnv) -> None:
    baseline_id = _insert_baseline(env, project_id=env.project_id)
    _bind_baseline(env, baseline_id)
    version = _seed_submittable_draft(env)  # 再 save 一次：baseline_id 为 None 时不改
    registered = _submit(env, version)

    edges = _edges_from(env, registered.node_id)
    assert [(row[0], row[1]) for row in edges] == [(baseline_id, PURPOSE)]
    # 方向：BASELINE 节点本身不依赖 PLAN。
    assert _edges_from(env, baseline_id) == []


def test_submit_without_baseline_writes_no_edge(env: PlanningEnv) -> None:
    version = _seed_submittable_draft(env)
    registered = _submit(env, version)
    assert registered.created is True
    assert _edges_from(env, registered.node_id) == []
    assert _edge_count_for_project(env) == 0


def test_resubmit_does_not_add_edges(env: PlanningEnv) -> None:
    baseline_id = _insert_baseline(env, project_id=env.project_id)
    _bind_baseline(env, baseline_id)
    version = _seed_submittable_draft(env)
    registered = _submit(env, version)
    assert _edge_count_for_project(env) == 1

    # 同一提交重放：被 409 挡下，边数不变。
    with pytest.raises(ApiError):
        _submit(env, _scalar(env, "SELECT lock_version FROM plan_drafts"
                             " WHERE project_id = :p", {"p": str(env.project_id)}))
    assert _edge_count_for_project(env) == 1

    # 直接重放写边（link_dependency 幂等）：不新增、不报错。
    with _repo_with_versions(env) as (_repo_, version_repo):
        link = versions_service.link_dependency(
            version_repo,
            parent_version_id=registered.node_id,
            dependency_version_id=baseline_id,
            purpose=PURPOSE,
        )
    assert link.created is False
    assert _edge_count_for_project(env) == 1


def test_each_revision_gets_its_own_node_and_edge(env: PlanningEnv) -> None:
    baseline_id = _insert_baseline(env, project_id=env.project_id)
    _bind_baseline(env, baseline_id)
    first = _submit(env, _seed_submittable_draft(env, total=10))

    # 草稿表可变：回到 EDITING 后改总量，提交第二修订。
    _raw(
        env,
        "UPDATE plan_drafts SET state = :s WHERE project_id = :p",
        {"s": DRAFT_STATE_EDITING, "p": str(env.project_id)},
    )
    with _repo(env) as repo:
        current = pl_service.require_draft(repo, project_id=env.project_id).lock_version
    _save(env, total_quantity=12, expected_lock_version=current)
    with _repo(env) as repo:
        current = pl_service.require_draft(repo, project_id=env.project_id).lock_version
    second = _submit(env, current)

    assert first.node_id != second.node_id
    assert (first.revision_no, second.revision_no) == (1, 2)
    assert _plan_node_count(env) == 2
    for node in (first, second):
        edges = _edges_from(env, node.node_id)
        assert [(row[0], row[1]) for row in edges] == [(baseline_id, PURPOSE)]
    assert _edge_count_for_project(env) == 2


def test_link_failure_rolls_back_plan_version(
    env: PlanningEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline_id = _insert_baseline(env, project_id=env.project_id)
    _bind_baseline(env, baseline_id)
    version = _seed_submittable_draft(env)

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("link failed")

    monkeypatch.setattr(versions_service, "link_dependency", _boom)
    with pytest.raises(RuntimeError, match="link failed"):
        _submit(env, version)

    assert _plan_node_count(env) == 0
    assert _edge_count_for_project(env) == 0
    state = _scalar(
        env, "SELECT state FROM plan_drafts WHERE project_id = :p",
        {"p": str(env.project_id)},
    )
    assert state == DRAFT_STATE_EDITING
