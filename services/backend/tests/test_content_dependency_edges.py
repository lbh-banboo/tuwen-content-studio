"""内容版本注册时写 version_dependencies 依赖边（E1 / E2 / E3）的判据。

* E1：CONTENT → PROMPT，purpose = `GENERATION_PROMPT`
* E2：CONTENT → BASELINE，purpose = `GENERATION_BASELINE`
* E3：新内容版本 → 上一版内容版本，purpose = `CONTENT_REVISION`（采纳标题 / 人工编辑）

方向为 parent（下游内容版本）→ dependency（上游），供 WP34 回退影响图遍历。
夹具与助手复用 `test_content_wp26_exec`（同一套自包含造数 + 脚本化生成器）。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

# 夹具与助手直接导入（与 test_content_wp29_edit_seam 同款）。
from test_content_wp26_exec import (
    ExecEnv,
    ScriptedGenerator,
    _run,
    _seed_baseline,
    _start_content,
    env,  # noqa: F401
    stub_billing,  # noqa: F401
)

from rednote.modules.baseline.repository import BaselineRepository
from rednote.modules.content import service as c_service
from rednote.modules.content.prompts_bridge import ActivePromptVersion
from rednote.modules.content.repository import ContentRepository
from rednote.modules.content.types import TASK_CONTENT_COPY
from rednote.modules.prompts.repository import PromptRepository
from rednote.modules.versions import service as versions_service
from rednote.modules.versions.repository import VersionRepository
from rednote.modules.versions.service import register_version
from rednote.platform.database import session_scope

COPY_OUTPUT: dict[str, Any] = {
    "title": "生成标题",
    "body": "第一段。\n\n第二段。",
    "tags": ["标签"],
    "bindings": [
        {"paragraphIndex": i, "claimId": None, "reason": "本段无对应事实", "text": f"第 {i + 1} 段"}
        for i in range(2)
    ],
    "titleCandidates": [
        {
            "title": "候选标题",
            "scores": {
                "search_match": 1,
                "clarity": 1,
                "audience_relevance": 1,
                "title_body_consistency": 1,
                "freshness": 1,
                "risk": 1,
            },
        }
    ],
}


def _raw(env: ExecEnv, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    with session_scope(env.settings) as session:
        return list(session.execute(text(sql), params or {}).fetchall())


def _register_prompt(env: ExecEnv) -> tuple[uuid.UUID, uuid.UUID]:
    """落一个真实的提示词模板 + 平台级 PROMPT 版本节点，返回 (template_id, version_id)。"""
    with session_scope(env.settings) as session:
        prepo = PromptRepository(session)
        template = prepo.insert_template(
            task_kind=TASK_CONTENT_COPY, name=f"edges-{uuid.uuid4().hex[:8]}"
        )
        node_id = register_version(
            VersionRepository(session),
            entity_type="PROMPT",
            entity_id=template.id,
            payload={"probe": "prompt", "n": uuid.uuid4().hex},
            project_id=None,
            created_by=env.owner_id,
        ).node_id
        prepo.insert_version(
            version_id=node_id,
            template_id=template.id,
            system_template="你是文案助手。",
            input_schema={},
            output_schema={},
            allowed_tools=(),
            change_reason="依赖边测试",
            created_by=env.owner_id,
        )
        return template.id, node_id


def _stub_prompt(
    monkeypatch: pytest.MonkeyPatch, template_id: uuid.UUID, version_id: uuid.UUID | None
) -> None:
    def _fake(_repo: Any, *, task_kind: str) -> ActivePromptVersion:
        return ActivePromptVersion(
            template_id=template_id,
            version_id=version_id,  # type: ignore[arg-type]
            task_kind=task_kind,
            system_template="你是文案助手。",
            input_schema={},
            output_schema={},
        )

    monkeypatch.setattr(c_service, "resolve_active_version", _fake)


def _generator() -> ScriptedGenerator:
    return ScriptedGenerator(calls=[], outputs={TASK_CONTENT_COPY: COPY_OUTPUT})


def _edges_from(env: ExecEnv, parent: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    rows = _raw(
        env,
        "SELECT dependency_version_id, purpose FROM version_dependencies"
        " WHERE parent_version_id = :p ORDER BY purpose",
        {"p": parent},
    )
    return [(r[0], r[1]) for r in rows]


def _version_ids(env: ExecEnv, content_id: uuid.UUID) -> list[uuid.UUID]:
    rows = _raw(
        env,
        "SELECT id FROM content_versions WHERE content_item_id = :c ORDER BY version_no",
        {"c": content_id},
    )
    return [r[0] for r in rows]


def _generate(
    env: ExecEnv, monkeypatch: pytest.MonkeyPatch, *, with_prompt: bool, with_baseline: bool
) -> tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]:
    template_id, node_id = _register_prompt(env)
    prompt_id = node_id if with_prompt else None
    baseline_id = _seed_baseline(env, brief_version_id=None) if with_baseline else None
    _stub_prompt(monkeypatch, template_id, prompt_id)
    content_id = _start_content(env)
    _run(env, content_id, _generator())
    return content_id, prompt_id, baseline_id


def test_generation_writes_e1_e2(
    env: ExecEnv, stub_billing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_id, prompt_id, baseline_id = _generate(
        env, monkeypatch, with_prompt=True, with_baseline=True
    )
    (v1,) = _version_ids(env, content_id)
    assert _edges_from(env, v1) == [
        (baseline_id, "GENERATION_BASELINE"),
        (prompt_id, "GENERATION_PROMPT"),
    ]
    # 方向：没有反向边
    assert _raw(
        env,
        "SELECT count(*) FROM version_dependencies WHERE dependency_version_id = :v",
        {"v": v1},
    )[0][0] == 0


def test_missing_prompt_and_baseline_skip_edges(
    env: ExecEnv, stub_billing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 无基线：不写 E2；prompt 版本 id 为空：不写 E1；均不抛错。
    content_id, _, _ = _generate(env, monkeypatch, with_prompt=False, with_baseline=False)
    (v1,) = _version_ids(env, content_id)
    assert _edges_from(env, v1) == []


def test_missing_only_baseline_keeps_e1(
    env: ExecEnv, stub_billing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_id, prompt_id, _ = _generate(env, monkeypatch, with_prompt=True, with_baseline=False)
    (v1,) = _version_ids(env, content_id)
    assert _edges_from(env, v1) == [(prompt_id, "GENERATION_PROMPT")]


def test_adopt_and_edit_write_only_e3(
    env: ExecEnv, stub_billing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_id, _, _ = _generate(env, monkeypatch, with_prompt=True, with_baseline=True)
    (v1,) = _version_ids(env, content_id)
    candidate_id = _raw(
        env, "SELECT id FROM title_candidates WHERE content_version_id = :v", {"v": v1}
    )[0][0]

    with session_scope(env.settings) as session:
        c_service.adopt_title_candidate(
            ContentRepository(session),
            version_repo=VersionRepository(session),
            content_id=content_id,
            candidate_id=candidate_id,
            created_by=env.owner_id,
        )
    v1_, v2 = _version_ids(env, content_id)
    assert _edges_from(env, v2) == [(v1, "CONTENT_REVISION")]

    lock = _raw(env, "SELECT lock_version FROM content_items WHERE id = :c", {"c": content_id})[0][0]
    with session_scope(env.settings) as session:
        c_service.append_edited_version(
            ContentRepository(session),
            version_repo=VersionRepository(session),
            baseline_repo=BaselineRepository(session),
            content_id=content_id,
            expected_lock_version=int(lock),
            title="编辑后的标题",
            created_by=env.owner_id,
        )
    _, _, v3 = _version_ids(env, content_id)
    assert _edges_from(env, v3) == [(v2, "CONTENT_REVISION")]
    # 不复制上一版 E1/E2：v1 仍各一条，v2/v3 无 GENERATION_*。
    assert len(_edges_from(env, v1)) == 2


def test_regeneration_writes_e1_e2_and_e3_to_previous(
    env: ExecEnv, stub_billing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """再生成（v2）也是修订：写 E1/E2，并只用 E3 连回上一版；首版没有 E3。"""
    content_id, prompt_id, baseline_id = _generate(
        env, monkeypatch, with_prompt=True, with_baseline=True
    )
    (v1,) = _version_ids(env, content_id)
    assert not any(p == "CONTENT_REVISION" for _, p in _edges_from(env, v1))

    # 真实世界里 v1 提交后 next_version_no 已是 2，幂等键随之变化 ⇒ 这是新一版而非重放。
    _run(env, content_id, _generator())
    v1_again, v2 = _version_ids(env, content_id)
    assert v1_again == v1
    assert sorted(_edges_from(env, v2), key=lambda e: e[1]) == sorted(
        [
            (baseline_id, "GENERATION_BASELINE"),
            (prompt_id, "GENERATION_PROMPT"),
            (v1, "CONTENT_REVISION"),
        ],
        key=lambda e: e[1],
    )
    # v1 自己的边没有被动过
    assert not any(p == "CONTENT_REVISION" for _, p in _edges_from(env, v1))


def test_idempotent_replay_adds_no_edges(
    env: ExecEnv, stub_billing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_id, _, _ = _generate(env, monkeypatch, with_prompt=True, with_baseline=True)
    before = _raw(env, "SELECT count(*) FROM version_dependencies")[0][0]
    # 同一幂等键命中既有上下文 ⇒ 跳过：不派发、不注册版本、不写边。
    # （真实世界里 v1 提交后 next_version_no 已是 2，幂等键变了——那是再生成，不是重放；
    # 这里强制让查找命中，直接验证 `continue` 分支不写边。）
    monkeypatch.setattr(
        ContentRepository, "find_generation_context", lambda *_a, **_k: object()
    )
    replay = _generator()
    _run(env, content_id, replay)
    assert replay.calls == []
    assert len(_version_ids(env, content_id)) == 1
    assert _raw(env, "SELECT count(*) FROM version_dependencies")[0][0] == before


def test_link_failure_rolls_back_generation(
    env: ExecEnv, stub_billing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    template_id, prompt_id = _register_prompt(env)
    _stub_prompt(monkeypatch, template_id, prompt_id)
    content_id = _start_content(env)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("link boom")

    monkeypatch.setattr(versions_service, "link_dependency", _boom)
    with pytest.raises(RuntimeError, match="link boom"):
        _run(env, content_id, _generator())

    assert _version_ids(env, content_id) == []
    assert _raw(
        env, "SELECT count(*) FROM version_nodes WHERE entity_id = :c", {"c": content_id}
    )[0][0] == 0


def test_link_failure_rolls_back_adopt_and_edit(
    env: ExecEnv, stub_billing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_id, _, _ = _generate(env, monkeypatch, with_prompt=True, with_baseline=False)
    (v1,) = _version_ids(env, content_id)
    candidate_id = _raw(
        env, "SELECT id FROM title_candidates WHERE content_version_id = :v", {"v": v1}
    )[0][0]
    lock = _raw(env, "SELECT lock_version FROM content_items WHERE id = :c", {"c": content_id})[0][0]

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("link boom")

    monkeypatch.setattr(versions_service, "link_dependency", _boom)
    with pytest.raises(RuntimeError, match="link boom"):
        with session_scope(env.settings) as session:
            c_service.adopt_title_candidate(
                ContentRepository(session),
                version_repo=VersionRepository(session),
                content_id=content_id,
                candidate_id=candidate_id,
                created_by=env.owner_id,
            )
    with pytest.raises(RuntimeError, match="link boom"):
        with session_scope(env.settings) as session:
            c_service.append_edited_version(
                ContentRepository(session),
                version_repo=VersionRepository(session),
                baseline_repo=BaselineRepository(session),
                content_id=content_id,
                expected_lock_version=int(lock),
                title="x",
                created_by=env.owner_id,
            )
    assert _version_ids(env, content_id) == [v1]
    assert _raw(
        env, "SELECT count(*) FROM version_nodes WHERE entity_id = :c", {"c": content_id}
    )[0][0] == 1
