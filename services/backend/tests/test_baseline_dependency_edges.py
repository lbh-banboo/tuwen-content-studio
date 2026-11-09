"""基线创建时写 version_dependencies 依赖边（E6 / E7）的判据。

* E6：BASELINE → BRIEF，purpose = `BASELINE_BRIEF`
* E7：BASELINE → KNOWLEDGE，purpose = `BASELINE_KNOWLEDGE`

方向为 parent（基线）→ dependency，供 WP34 回退影响图遍历。
夹具与助手复用 `test_baseline_wp22`（同一套自包含建库 / 造数）。
"""

from __future__ import annotations

import uuid

import pytest

from rednote.modules.baseline import service as bs
from rednote.modules.baseline.repository import BaselineRepository
from rednote.modules.versions.repository import VersionRepository
from rednote.platform.database import session_scope

from test_baseline_wp22 import (  # noqa: F401  (fixture 需导入才能在本模块生效)
    BaselineEnv,
    _baseline_count,
    _baseline_draft,
    _create_baseline,
    _ensure_own_tables,
    _fact_item,
    _make_brief_node,
    _make_ready,
    _own_knowledge,
    _raw,
    _scalar,
    env,
)


def _edges(env: BaselineEnv, baseline_id: uuid.UUID) -> set[tuple[uuid.UUID, str]]:
    rows = _raw(
        env,
        "select dependency_version_id, purpose from version_dependencies"
        " where parent_version_id = :bid",
        {"bid": baseline_id},
    )
    return {(r[0], r[1]) for r in rows}


def _total_edges(env: BaselineEnv) -> int:
    return int(_scalar(env, "select count(*) from version_dependencies") or 0)


def _ready(env: BaselineEnv) -> uuid.UUID:
    brief = _make_brief_node(env)
    _make_ready(env, brief_version_id=brief)
    return brief


def test_create_baseline_writes_e6_and_e7(env: BaselineEnv) -> None:
    brief = _ready(env)
    a1, v1 = _own_knowledge(env)
    a2, v2 = _own_knowledge(env)
    created = _create_baseline(
        env,
        _baseline_draft(
            env,
            brief_version_id=brief,
            selected=(
                _fact_item(ref_label="事实 1", knowledge_asset_id=a1, knowledge_version_id=v1),
                _fact_item(ref_label="事实 2", knowledge_asset_id=a2, knowledge_version_id=v2),
                _fact_item(ref_label="无知识引用"),
            ),
        ),
    )
    assert created.created is True
    assert _edges(env, created.baseline_id) == {
        (brief, "BASELINE_BRIEF"),
        (v1, "BASELINE_KNOWLEDGE"),
        (v2, "BASELINE_KNOWLEDGE"),
    }
    # 方向：基线只能是 parent，不得反向
    assert _scalar(
        env,
        "select count(*) from version_dependencies where dependency_version_id = :bid",
        {"bid": created.baseline_id},
    ) == 0


def test_create_baseline_without_knowledge_has_only_e6(env: BaselineEnv) -> None:
    brief = _ready(env)
    created = _create_baseline(
        env, _baseline_draft(env, brief_version_id=brief, selected=(_fact_item(),))
    )
    assert _edges(env, created.baseline_id) == {(brief, "BASELINE_BRIEF")}


def test_link_helper_skips_null_brief_without_error(env: BaselineEnv) -> None:
    """brief 为空：不写 E6、不抛错。

    注意：`create_baseline` 在更早处（`_require_brief_version_node`）已对空
    brief 报 422，因此该分支经主路径不可达；这里直接测判空守卫。
    """
    brief = _ready(env)
    created = _create_baseline(
        env, _baseline_draft(env, brief_version_id=brief, selected=(_fact_item(),))
    )
    before = _total_edges(env)
    with session_scope(env.settings) as session:
        bs._link_baseline_dependencies(
            VersionRepository(session),
            baseline_id=created.baseline_id,
            brief_version_id=None,
            knowledge_version_ids=[None, None],
        )
    assert _total_edges(env) == before


def test_replay_does_not_add_edges(env: BaselineEnv) -> None:
    brief = _ready(env)
    a, v = _own_knowledge(env)
    draft = _baseline_draft(
        env,
        brief_version_id=brief,
        selected=(_fact_item(knowledge_asset_id=a, knowledge_version_id=v),),
    )
    first = _create_baseline(env, draft)
    edges = _edges(env, first.baseline_id)
    total = _total_edges(env)
    second = _create_baseline(env, draft)
    assert second.created is False
    assert second.baseline_id == first.baseline_id
    assert _edges(env, first.baseline_id) == edges
    assert _total_edges(env) == total


def test_duplicate_knowledge_version_yields_single_edge(env: BaselineEnv) -> None:
    """重复的 knowledge_version_id 只写一条边。

    经 `create_baseline` 构造不出重复（`uq_baseline_items_baseline_asset`
    先拦下同资产二次入选），因此直接对写边助手验证去重。
    """
    brief = _ready(env)
    _a, v = _own_knowledge(env)
    created = _create_baseline(
        env, _baseline_draft(env, brief_version_id=brief, selected=(_fact_item(),))
    )
    with session_scope(env.settings) as session:
        bs._link_baseline_dependencies(
            VersionRepository(session),
            baseline_id=created.baseline_id,
            brief_version_id=brief,
            knowledge_version_ids=[v, None, v, v],
        )
    assert _edges(env, created.baseline_id) == {
        (brief, "BASELINE_BRIEF"),
        (v, "BASELINE_KNOWLEDGE"),
    }


def test_link_failure_rolls_back_whole_baseline(
    env: BaselineEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief = _ready(env)

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("link failed")

    monkeypatch.setattr(bs, "link_dependency", boom)
    with pytest.raises(RuntimeError, match="link failed"):
        _create_baseline(
            env,
            _baseline_draft(env, brief_version_id=brief, selected=(_fact_item(),)),
        )
    assert _baseline_count(env) == 0
    assert _scalar(
        env,
        "select count(*) from version_nodes where entity_type = 'BASELINE'"
        " and project_id = :pid",
        {"pid": env.project_id},
    ) == 0
    assert _scalar(
        env,
        "select count(*) from baseline_items i join production_baselines b"
        " on b.id = i.baseline_id where b.project_id = :pid",
        {"pid": env.project_id},
    ) == 0
