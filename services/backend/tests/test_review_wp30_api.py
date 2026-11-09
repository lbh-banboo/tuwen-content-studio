"""WP30 API 层端到端测试：真实 HTTP 请求走一遍六个端点。

与 `test_review_wp30.py` 的区别：那份文件直接调 `service.py` 的函数，
验证的是编排逻辑本身；本文件经 `TestClient` 发真实请求，验证
`api.py` / `dependencies.py` / `schemas.py` 这一层把请求解析、两步骤鉴权、
响应过滤正确接到了服务层。

## 本文件守的六条（**断精确状态码 + `details.reason` 常量**）

| # | 判据 | 守的是什么 |
|---|---|---|
| 1 | ① 跑自检 **201**，响应里五个计数齐全 | `response_model` 是输出过滤器：声明里没有的键会被**静默剥掉**。少了 `skippedCount` 就会让「5 类判了 + 3 类没判」被渲染成「全通过」 |
| 2 | ① 的请求体 `extra="forbid"` ⇒ 传未知字段 **422** | OP-156。必须证明"被拒"，而不是证明"被忽略" |
| 3 | ② 轮次历史 **200** + 分页包络有 `page` 块 | `PaginatedEnvelope` 用错会把 `page` 剥掉（WP10 踩过） |
| 4 | ③ 单轮详情 **200**，8 类卡片一张不少；不存在的 runId **404** + `SELF_CHECK_RUN_NOT_FOUND` | 两步骤鉴权第一步；缺卡片会被前端渲染成"通过" |
| 5 | ④ 批量 **200**（不是 201）+ 逐条 `ok`；⑤ 看板 **200** | 部分成功时 201 会说谎 |
| 6 | ⑥ 规则目录 **200**，三条恒 SKIPPED 的 `implemented=false` 照常出现 | 缺席会被渲染成"通过" |

另有两条跨包联动：自检出阻断 ⇒ `POST /contents/{id}/reviews` 必 **409**
`UNRESOLVED_BLOCKING_FEEDBACK`；处置掉后必 **201**。

## 为什么要手工 `include_router`

`main.py` 是**跨会话共享文件**，分支禁改（接缝冻结 §1.1 第 2 处）——
review router 的注册属主 agent 的收口动作。因此本文件按
`test_content_wp26.py` 的既有先例补挂一次，并用**路径前缀**探测幂等
（`main.py` 接线之后本文件不会重复注册）。

鉴权 / CSRF 设施照抄 `test_content_wp26.py` 的 `_login` / `_write_headers`，
只是把 `who` 固定成 `owner`（本文件不测权限矩阵，那是 projects 包自己的
覆盖范围）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from rednote.modules.review.types import (
    REVIEW_CATEGORIES,
    RULE_SET_VERSION,
    SKIP_REASON_MODEL_RULE_NOT_ENABLED,
    TRIGGER_KIND_BATCH,
    TRIGGER_KIND_MANUAL,
)
from rednote.platform.csrf import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from rednote.platform.database import session_scope
from rednote.platform.tokens import generate_session_token, hash_token

from test_content_wp26 import API_PREFIX
from test_review_wp30 import (  # noqa: F401  （env 是本文件要用的夹具）
    ReviewEnv,
    _seed_blocking_content,
    _seed_content,
    env,
)

CSRF_SECRET_PLAINTEXT = "wp30-api-csrf-secret-plaintext"
TEST_ORIGIN = "http://testserver"

#: ① 的路径（用它探测 router 是否已被 `main.py` 挂上）。
_PROBE_PATH = f"{API_PREFIX}/contents/{{contentId}}/self-checks"


@pytest.fixture
def app(env: ReviewEnv) -> FastAPI:
    """按注入配置装配 app，并**在 `main.py` 尚未接线时**补挂 review router。

    **挂载必须幂等**：主 agent 收口后 `main.py` 会 `include_router`，
    此时再挂一次会让每条路由**重复注册**（WP20 收口时实测：路由数翻倍）。

    探测依据用**路径前缀**而不是 router 对象身份：`include_router` 会把
    router 复制成新对象，身份比较不可靠。
    """
    from rednote.main import create_app

    assert env.settings.api_prefix == API_PREFIX, (
        f"本文件写死了 {API_PREFIX!r}，注入配置给的是 "
        f"{env.settings.api_prefix!r} —— 两者必须一致"
    )
    application = create_app(env.settings)
    already_mounted = any(
        getattr(route, "path", "") == _PROBE_PATH for route in application.routes
    )
    if not already_mounted:
        from rednote.modules.review.api import router as review_router

        application.include_router(review_router, prefix=env.settings.api_prefix)
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, env: ReviewEnv) -> None:
    """造一条会话并写 Cookie（含 CSRF 令牌）。

    CSRF 不能绕过：`current_user` 依赖对**写方法**强制 `verify_csrf`，
    绕过它会让本层不再验证真实的鉴权链 —— 那时状态码断言就成了对一段
    假鉴权的断言。
    """
    from rednote.modules.identity.repository import IdentityRepository

    raw_token = generate_session_token()
    with session_scope(env.settings) as session:
        IdentityRepository(session).create_session(
            user_id=env.owner_id,
            token_hash=hash_token(raw_token),
            csrf_secret_hash=hash_token(CSRF_SECRET_PLAINTEXT),
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    client.cookies.set(CSRF_COOKIE_NAME, CSRF_SECRET_PLAINTEXT)


def _write_headers(**extra: str) -> dict[str, str]:
    headers = {"X-CSRF-Token": CSRF_SECRET_PLAINTEXT, "Origin": TEST_ORIGIN}
    headers.update(extra)
    return headers


# ===========================================================================
# ① POST /contents/{contentId}/self-checks
# ===========================================================================


def test_create_self_check_returns_201_with_all_five_counts(
    client: TestClient, env: ReviewEnv
) -> None:
    """① **201**，且响应里**五个计数齐全**（含 `skippedCount`）。

    `response_model` 是 FastAPI 的输出过滤器：声明里没有的键会被静默剥掉。
    少了 `skippedCount` / `erroredCount`，前端就只能从"命中数为 0"推断
    "全通过"——而本包有三类恒 SKIPPED 的规则，那个推断是**假绿**。
    """
    _login(client, env)
    seeded = _seed_content(env, paragraphs=3)

    resp = client.post(
        f"{API_PREFIX}/contents/{seeded.content_id}/self-checks",
        json={},
        headers=_write_headers(),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]

    for key in (
        "blockingCount",
        "warningCount",
        "infoCount",
        "evaluatedCount",
        "skippedCount",
        "erroredCount",
    ):
        assert key in data, f"响应里缺 {key} —— 它被 response_model 静默剥掉了"
    assert data["skippedCount"] >= 3, (
        "三类恒 SKIPPED 的规则必须出现在未判数里（否则它们被折算成了通过）"
    )
    assert data["triggerKind"] == TRIGGER_KIND_MANUAL
    assert data["ruleSetVersion"] == RULE_SET_VERSION
    assert data["subjectState"] == "CURRENT"
    assert data["subjectVersionId"] == str(seeded.version_id)
    # 8 类卡片一张不少 + 逐规则结论 + findings 三块都在。
    assert {item["category"] for item in data["categories"]} == REVIEW_CATEGORIES
    assert len(data["ruleResults"]) == 11
    assert isinstance(data["findings"], list)


def test_create_self_check_rejects_unknown_body_field_with_422(
    client: TestClient, env: ReviewEnv
) -> None:
    """① 的请求体 `extra="forbid"` ⇒ 传未知字段必须 **422**（OP-156）。

    必须证明"**被拒**"而不是"被忽略"：客户端想"只跑某几条规则"时传了
    `ruleCodes`，静默忽略会让它以为生效了 —— 而那正是"8 类全绿"与
    "只跑了 2 条且都绿"长得一样的那个口子。
    """
    _login(client, env)
    seeded = _seed_content(env, paragraphs=3)

    resp = client.post(
        f"{API_PREFIX}/contents/{seeded.content_id}/self-checks",
        json={"ruleCodes": ["FACT_BINDING_CLAIM_OUT_OF_SCOPE"]},
        headers=_write_headers(),
    )
    assert resp.status_code == 422, resp.text


def test_create_self_check_on_unknown_content_returns_404(
    client: TestClient, env: ReviewEnv
) -> None:
    """两步骤鉴权第一步：内容不存在 ⇒ **404**（不区分"不存在"与"不属于你"）。"""
    _login(client, env)
    resp = client.post(
        f"{API_PREFIX}/contents/{uuid.uuid4()}/self-checks",
        json={},
        headers=_write_headers(),
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["details"]["reason"] == "CONTENT_NOT_FOUND"


def test_create_self_check_on_versionless_content_returns_409(
    client: TestClient, env: ReviewEnv
) -> None:
    """内容存在但**一版都没有** ⇒ **409**（换任何参数都不行，要先跑一次生成）。

    不是 422：422 的定义是"改请求参数能成功"，而这里刷新重试无用。
    """
    _login(client, env)
    content_id = uuid.uuid4()
    with session_scope(env.settings) as session:
        session.execute(
            text(
                "INSERT INTO content_items (id, project_id, state,"
                " current_version_no, lock_version)"
                " VALUES (:c, :p, 'DRAFT', 0, 0)"
            ),
            {"c": content_id, "p": env.project_id},
        )

    resp = client.post(
        f"{API_PREFIX}/contents/{content_id}/self-checks",
        json={},
        headers=_write_headers(),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["reason"] == "CONTENT_VERSION_NOT_FOUND"


# ===========================================================================
# ② GET /contents/{contentId}/self-checks
# ===========================================================================


def test_list_self_checks_returns_200_with_page_block(
    client: TestClient, env: ReviewEnv
) -> None:
    """② **200**，分页包络必须带 `page` 块（新的在前）。

    `PaginatedEnvelope` 用错（例如写成 `DataEnvelope[list[...]]`）会把
    `page` **静默剥掉** ⇒ 前端永远翻不到第二页。
    """
    _login(client, env)
    seeded = _seed_content(env, paragraphs=3)
    for _ in range(2):
        created = client.post(
            f"{API_PREFIX}/contents/{seeded.content_id}/self-checks",
            json={},
            headers=_write_headers(),
        )
        assert created.status_code == 201, created.text
        newest = created.json()["data"]["runId"]

    resp = client.get(f"{API_PREFIX}/contents/{seeded.content_id}/self-checks")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "page" in body and body["page"]["hasMore"] is False
    assert len(body["data"]) == 2
    assert body["data"][0]["runId"] == newest, "轮次历史必须新的在前"
    # 列表项是摘要，不该把 findings 带出来（响应体是两个不同的模型）。
    assert "findings" not in body["data"][0]


# ===========================================================================
# ③ GET /self-checks/{runId}
# ===========================================================================


def test_get_self_check_detail_returns_200_with_eight_cards(
    client: TestClient, env: ReviewEnv
) -> None:
    """③ **200**，8 类卡片一张不少，三条恒 SKIPPED 的原因码照常透出。"""
    _login(client, env)
    seeded = _seed_content(env, paragraphs=3)
    created = client.post(
        f"{API_PREFIX}/contents/{seeded.content_id}/self-checks",
        json={},
        headers=_write_headers(),
    )
    run_id = created.json()["data"]["runId"]

    resp = client.get(f"{API_PREFIX}/self-checks/{run_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert {item["category"] for item in data["categories"]} == REVIEW_CATEGORIES
    by_code = {item["ruleCode"]: item for item in data["ruleResults"]}
    assert by_code["TONE_MODEL_JUDGEMENT"]["status"] == "SKIPPED"
    assert by_code["TONE_MODEL_JUDGEMENT"]["skipReason"] == (
        SKIP_REASON_MODEL_RULE_NOT_ENABLED
    )


def test_get_self_check_detail_unknown_run_returns_404(
    client: TestClient, env: ReviewEnv
) -> None:
    """③ 的两步骤鉴权第一步：轮次不存在 ⇒ **404** + 受控 `reason`。"""
    _login(client, env)
    resp = client.get(f"{API_PREFIX}/self-checks/{uuid.uuid4()}")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["details"]["reason"] == "SELF_CHECK_RUN_NOT_FOUND"


# ===========================================================================
# ④⑤ /projects/{projectId}/self-checks
# ===========================================================================


def test_batch_self_check_returns_200_with_per_item_results(
    client: TestClient, env: ReviewEnv
) -> None:
    """④ **200**（不是 201）+ 逐条 `ok`：部分成功时统一 201 会说谎。"""
    _login(client, env)
    good = _seed_content(env, paragraphs=3)
    missing = uuid.uuid4()

    resp = client.post(
        f"{API_PREFIX}/projects/{env.project_id}/self-checks",
        json={"contentIds": [str(good.content_id), str(missing)]},
        headers=_write_headers(),
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["data"]["results"]
    assert [item["ok"] for item in results] == [True, False]
    assert results[0]["run"]["triggerKind"] == TRIGGER_KIND_BATCH
    assert results[1]["run"] is None
    assert results[1]["error"]


def test_batch_self_check_empty_list_returns_422(
    client: TestClient, env: ReviewEnv
) -> None:
    """④ 空批量 ⇒ **422** + 受控 `reason`（不允许"不传就是全项目"）。"""
    _login(client, env)
    resp = client.post(
        f"{API_PREFIX}/projects/{env.project_id}/self-checks",
        json={"contentIds": []},
        headers=_write_headers(),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["reason"] == "CONTENT_IDS_EMPTY"


def test_project_board_returns_latest_run_per_content(
    client: TestClient, env: ReviewEnv
) -> None:
    """⑤ **200**：每条内容最新一轮；未自检的内容**不出现**。

    不为未自检的内容编造一行是刻意的 —— 那会让"未自检"与"自检过"在数据
    结构上同形，而原型列表页本来就有「未自检」这一态。
    """
    _login(client, env)
    checked = _seed_content(env, paragraphs=3)
    never = _seed_content(env, paragraphs=3)
    client.post(
        f"{API_PREFIX}/contents/{checked.content_id}/self-checks",
        json={},
        headers=_write_headers(),
    )

    resp = client.get(f"{API_PREFIX}/projects/{env.project_id}/self-checks")
    assert resp.status_code == 200, resp.text
    runs = resp.json()["data"]["runs"]
    assert [item["contentItemId"] for item in runs] == [str(checked.content_id)]
    assert str(never.content_id) not in {item["contentItemId"] for item in runs}


# ===========================================================================
# ⑥ GET /self-check-rules
# ===========================================================================


def test_rule_catalog_returns_200_with_unimplemented_rules_listed(
    client: TestClient, env: ReviewEnv
) -> None:
    """⑥ **200**：三条恒 SKIPPED 的规则 `implemented=false` **照常列出**。

    缺席会被前端渲染成"通过"。一条明确声明"我没实现"的规则是防线；
    一条缺席而被读成通过的规则是假绿。
    """
    _login(client, env)
    resp = client.get(f"{API_PREFIX}/self-check-rules")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["ruleSetVersion"] == RULE_SET_VERSION
    assert len(data["rules"]) == 11
    unimplemented = {
        item["code"] for item in data["rules"] if item["implemented"] is False
    }
    assert unimplemented == {
        "TONE_MODEL_JUDGEMENT",
        "DUPLICATE_CROSS_ITEM_SIMILARITY",
        "SCENE_LINKAGE_UNAVAILABLE",
    }
    assert all(
        item["defaultSeverity"] is None
        for item in data["rules"]
        if item["implemented"] is False
    )
    # 8 类都有规则（没有哪一类在目录里缺席）。
    assert {item["category"] for item in data["rules"]} == REVIEW_CATEGORIES


def test_rule_catalog_requires_login(client: TestClient, env: ReviewEnv) -> None:
    """⑥ 虽无项目维度，但仍要登录 ⇒ 未登录 **401**。"""
    resp = client.get(f"{API_PREFIX}/self-check-rules")
    assert resp.status_code == 401, resp.text


# ===========================================================================
# 跨包联动：自检出阻断 ⇒ WP29 的审核端点 409；处置后 201
# ===========================================================================


def test_blocking_self_check_makes_wp29_review_endpoint_return_409_then_201(
    client: TestClient, env: ReviewEnv
) -> None:
    """**本包最重要的一条端到端判据**：

    自检出阻断 ⇒ `POST /contents/{id}/reviews` 必 **409**
    `UNRESOLVED_BLOCKING_FEEDBACK`；把那条 AI 反馈处置掉后必 **201**。

    它证明本包**不需要**第二道审核闸：阻断项经服务层落成
    `authorKind=AI` 的 BLOCKING 反馈，WP29 既有的闸当场拒绝"通过"。
    两个端点分属两个模块、走的是真实 HTTP 调用链。
    """
    _login(client, env)
    seeded = _seed_blocking_content(env)

    created = client.post(
        f"{API_PREFIX}/contents/{seeded.content_id}/self-checks",
        json={},
        headers=_write_headers(),
    )
    assert created.status_code == 201, created.text
    detail = created.json()["data"]
    assert detail["outcome"] == "BLOCKED"
    assert detail["blockingCount"] == 1
    blocking = [
        item for item in detail["findings"] if item["feedbackItemId"] is not None
    ]
    assert len(blocking) == 1
    feedback_id = blocking[0]["feedbackItemId"]

    # WP29 的审核端点：必须 409。
    blocked = client.post(
        f"{API_PREFIX}/contents/{seeded.content_id}/reviews",
        json={
            "subjectVersionId": str(seeded.version_id),
            "decision": "APPROVED",
            "reason": "看起来没问题",
        },
        headers=_write_headers(),
    )
    assert blocked.status_code == 409, blocked.text
    error = blocked.json()["error"]["details"]
    assert error["reason"] == "UNRESOLVED_BLOCKING_FEEDBACK"
    assert error["blockingFeedbackIds"] == [feedback_id]

    # 处置那条 AI 反馈（WP29 的 PATCH 需要 If-Match）。
    feedback_list = client.get(
        f"{API_PREFIX}/contents/{seeded.content_id}/feedback"
    )
    assert feedback_list.status_code == 200, feedback_list.text
    row = next(
        item for item in feedback_list.json()["data"] if item["id"] == feedback_id
    )
    assert row["authorKind"] == "AI"
    assert row["severity"] == "BLOCKING"
    patched = client.patch(
        f"{API_PREFIX}/feedback/{feedback_id}",
        json={"toState": "REJECTED", "rejectReason": "复核后确认事实无误"},
        headers=_write_headers(**{"If-Match": f'"{feedback_id}:{row["lockVersion"]}"'}),
    )
    assert patched.status_code == 200, patched.text

    # 再审一次：必须 201。
    approved = client.post(
        f"{API_PREFIX}/contents/{seeded.content_id}/reviews",
        json={
            "subjectVersionId": str(seeded.version_id),
            "decision": "APPROVED",
            "reason": "阻断项已处置",
        },
        headers=_write_headers(),
    )
    assert approved.status_code == 201, approved.text
    assert approved.json()["data"]["decision"] == "APPROVED"


def test_six_endpoints_are_mounted_under_four_paths(
    app: FastAPI, env: ReviewEnv
) -> None:
    """契约面：本包恰好 **4 条新 path / 6 个新 operation**。

    收口时主 agent 要串行重导出 `contracts/openapi.json`（86 → 90 paths、
    106 → 112 ops）。这条断言让"少挂了一个端点"在分支里就暴露，
    而不是等到契约导出的数对不上。
    """
    paths: dict[str, set[str]] = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        if "self-check" not in path:
            continue
        paths.setdefault(path, set()).update(
            method for method in getattr(route, "methods", set()) or set()
            if method in {"GET", "POST", "PATCH", "DELETE", "PUT"}
        )
    assert set(paths) == {
        f"{API_PREFIX}/contents/{{contentId}}/self-checks",
        f"{API_PREFIX}/self-checks/{{runId}}",
        f"{API_PREFIX}/projects/{{projectId}}/self-checks",
        f"{API_PREFIX}/self-check-rules",
    }, sorted(paths)
    assert sum(len(methods) for methods in paths.values()) == 6, paths
