"""WP26 **执行层**（`execute_generation`）的补测 —— ISS-177 / ISS-178 同批。

## 本文件为什么单独成文件（而不是追加进 `test_content_wp26.py`）

`test_content_wp26.py` 已有 92 条用例，覆盖第 1/2/3/4 层，但执行层
（`execute_generation`）**零覆盖** —— ISS-178 实测：`execute_generation`
在 `tests/` 全目录命中 **0 次**。而本仓唯一一条 CRITICAL（ISS-177）
恰好长在**那条零覆盖的路径上**：第 5 步「基线判据域」之后没有任何
`total_claim_count == 0` 的守卫。

因此本文件与那条守卫**同批落地，顺序不可颠倒**：先有用例，守卫才有判据。
ISS-177 `resolution` 的原话是「守卫的落点就在零覆盖的那条路径上，
先加守卫等于改了没有判据」。

## ★★ 本文件最重要的一条判据：`test_guard_reason_is_reachable_from_service`

ISS-177 的形态是**仓库层 docstring 声称服务层会拒绝，而服务层从未拒绝**。
这类缺陷的取证陷阱是：**用文本子串查 `REASON_BASELINE_NOT_READY` 会恒绿**
—— `repository.claim_scope` 那句声称专用的 docstring 自己就会被算进来，
于是判据在自己校对的东西上永远通过（ISS-95 第 3 次复发的同款形态）。

因此那条判据走 **AST**：解析 `service.py`，先剔除所有 **docstring**
（`ast.Module` / `ClassDef` / `FunctionDef` / `AsyncFunctionDef` 的第一个
表达式语句），再按 `ast.Call` 的**关键字实参** `reason=` 计数值匹配。
文档怎么写都不会喂绿它 —— 只有真正的 `raise ... reason=...` 才算数。

## ★★ 第二重要的事：判据的探针必须落在**判定点之后**

`execute_generation` 的步骤顺序是**写死的**：

```
第 1 步 取内容行(锁) → 第 2 步 解析请求 → 第 3 步 解析激活提示词(R6)
→ 第 4 步 解析 provider → 第 5 步 基线判据域(R4，守卫在这里)
→ 第 6 步 目标版本号 → 第 7 步 逐 kind〔预估 → 预占 → 渲染 → 派发 …〕
```

本文件初版曾用「断言抛出 `PromptUnavailable`」当作"守卫没误伤"的探针 ——
**那是错的，而且错得很隐蔽**：`PromptUnavailable` 抛在**第 3 步**，
在守卫（第 5 步）**之前**。于是"守卫误判了合法基线"与"压根没走到守卫"
在断言层面**完全同形** —— 一条**恒绿判据**。

> **实测反证（本轮记录）**：把守卫改写成无条件形态
> （`if total_claim_count == 0:`，去掉 `brief_version_id is not None`），
> 旧探针仍然"绿"。即：**这条用例抓不到它本该抓的那个 bug。**

对症药是**哨兵探针**：把第 7 步的第一个动作（`estimate_generation_cost`）
换成一个只可能在第 7 步发生的哨兵异常。这样：

| 实际发生 | 观测到的异常 | 含义 |
|---|---|---|
| 守卫**误伤**了合法基线 | `ContentGenerationError(reason=BASELINE_NOT_READY)` | 判据**变红** |
| 守卫**正确放行** | `Step7Reached`（哨兵） | 判据**变绿** |

两者可区分 —— 这才是有判别力的判据。**"能区分两种世界"是判据的最低门槛。**

## 为什么这些用例不依赖库里的提示词模板

`prompts_bridge.resolve_active_version` 走的是
`PromptRepository.list_templates(task_kind=…, limit=1)`，而它**全局可见、
不按项目隔离**（按 `created_at DESC` 取最新）。这意味着第 3 步能否通过，
取决于**库里此刻有什么** —— 包括**并行会话留下的行**。

本文件因此把第 3 步也**显式桩掉**（`_stub_prompt_resolution`）：本文件要判的是
**第 5 步的守卫**，第 3 步只是路上的一道门，让它"恒通行"才能让判据只对
守卫敏感。这样做的代价是**失去了对真实 `resolve_active_version` 的覆盖** ——
那份覆盖属于 `prompts_bridge` 自己的测试，不属于这里；而收益是本文件的
结论**不再取决于别的会话在 `prompt_templates` 里留了什么**（铁律 17）。

> **为什么不用 `TRUNCATE prompt_templates` 来"清干净"**：那会连带清掉
> **别人的** `prompt_versions` / `prompt_publications`（并行会话正在用），
> 且**解决不了上面的探针位置问题** —— 清干净之后第 3 步会抛
> `PromptUnavailable`，旧探针照样"绿"。**清数据是治标，移探针是治本。**

## 本文件不做的（留给 ISS-178 的剩余部分）

完整跑通一次成功生成（`dispatch → validate → 落库 → 结算`）需要跨 4 个包铺底
（prompts 激活版 / baseline / billing 的 `batch.limits` 与计价版本 / content）。
本文件只把 `execute_generation` 送到**第 7 步的门口**，用"抛出的东西"当
"走到了哪一步"的探针。完整一次成功生成另行补齐。

## 本文件覆盖的四层

| 层 | 判的是什么 | 需要数据库 |
|---|---|---|
| 第 0 层 AST | 守卫**连到了**服务层（`reason=` 实参） | 否 |
| 第 1 层仓储 | `claim_scope` 对两个 0 **如实报数** | 是 |
| 第 2 层服务 | 两个 0 被**区别对待**（哨兵探针） | 是 |
| 第 3 层接线 | 处理器与注册表**在位**（kind 不漏、默认 fail-closed） | 部分是 |

前两层是"ISS-177 的守卫有没有落点"，后两层是"ISS-178 说的零覆盖补到哪一步"。

> **第 3 层为什么要单独提**：ISS-178 的名字是"**执行路径**零覆盖"，
> 而生产里执行**不从 `execute_generation` 进入** —— 它从
> `content_generate_handler` 进来。只补服务层的话，"注册表漏 kind"
> 这类缺陷仍然无判据（症状是静默的 `NO_HANDLER` 失败）。

## ★ 为什么还需要 `stub_billing`：一次实测反证

写"未配 adapter"那条用例时，我假设它会在第 7 步的**派发点**抛
`GENERATOR_NOT_CONFIGURED`。**实测抛的是 `VALIDATION_FAILED`** ——
因为第 7 步的顺序是 **预估 → 预占 → 渲染 → 派发**，而
`estimate_generation_cost` 在**预估**这一步就抛了 `ApiError`
（项目没配 `batch.limits`，`billing/types.py` 的 `LIMITS_NOT_CONFIGURED`）。
执行**根本走不到派发点**。

这不是环境问题，是**用例的前提错了**。修法与 `stub_prompt` 同源：把计费
那一段也桩掉，让执行能走到派发点 —— 那才是本用例要判的地方。
（若不打桩，用例会一直"绿在一条**与断言无关**的失败上"，
这种"绿在错的地方"比红更难发现。）
"""

import ast
import os
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from rednote.modules.billing.types import CostEstimateView, ReservationView
from rednote.modules.content import handlers as c_handlers
from rednote.modules.content import service as c_service
from rednote.modules.content.gateway_bridge import GatewayUnavailable
from rednote.modules.content.prompts_bridge import ActivePromptVersion
from rednote.modules.content.repository import ContentRepository
from rednote.modules.content.service import (
    ContentGenerationError,
    GatewayGenerator,
    GenerationDispatch,
    UnavailableGenerator,
    execute_generation,
)
from rednote.modules.content.types import (
    IDEMPOTENCY_SCOPE_CONTENT_GENERATE,
    KIND_CONTENT_GENERATE,
    QUEUE_CONTENT,
    REASON_BASELINE_NOT_READY,
    REASON_CONTENT_NOT_FOUND,
    SCOPE_COPY,
    TASK_CONTENT_COPY,
    GenerationRequest,
)
from rednote.modules.gateway.repository import GatewayRepository
from rednote.modules.gateway.types import (
    CAPABILITY_TEXT_STRUCTURED,
    NETWORK_EXTERNAL,
)
from rednote.modules.identity.models import ROLE_OPERATOR
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.jobs import service as jobs_service
from rednote.modules.jobs.handlers import build_default_registry
from rednote.modules.jobs.models import LEASE_DURATION_SECONDS, STATE_FAILED
from rednote.modules.jobs.repository import JobRepository
from rednote.modules.jobs.types import ClaimedJob
from rednote.modules.projects.models import Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.modules.versions.repository import VersionRepository
from rednote.modules.versions.service import register_version
from rednote.platform.configuration import Settings
from rednote.platform.database import reset_engines, session_scope

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
TEST_DATABASE_URL_ENV = "REDNOTE_TEST_DATABASE_URL"

#: `services/backend/` 的绝对路径 —— 给 AST 判据定位 `service.py` 用。
#: 从本文件（`services/backend/tests/`）上溯两级，**不写死绝对路径**
#: （写死会让整仓挪位置时判据静默指向不存在的目录）。
SERVICES_BACKEND = Path(__file__).resolve().parents[1]
SERVICE_PY = SERVICES_BACKEND / "src/rednote/modules/content/service.py"


def _test_database_url() -> str:
    return os.environ.get(TEST_DATABASE_URL_ENV, DEFAULT_TEST_DATABASE_URL).strip()


def _make_settings(**overrides: object) -> Settings:
    payload: dict[str, Any] = {
        "database_url": _test_database_url(),
        "environment": "test",
    }
    payload.update(overrides)
    return Settings(**payload)


# ===========================================================================
# 探针：脚本化生成器 + 第 7 步哨兵
# ===========================================================================


class Step7Reached(Exception):
    """哨兵：**只在**第 7 步的预估点被抛出。

    ## 它为什么必须是一个独立类型，而不是复用某个既有异常

    "守卫放行了"与"守卫误伤了"必须能被**一条 `pytest.raises` 分开**。
    复用既有异常（例如让桩抛 `ApiError`）会让两者都落进同一个
    `except`，于是判据又回到"两种世界同形"的老问题。

    本类**不继承** `ValueError` / `ApiError`：它不是业务语义的一部分，
    它是一次**测试探针**，任何产品代码都不该捕获它。
    """


class PromptStepProbe(Exception):
    """哨兵：**只在第 3 步**（解析激活提示词）被替换时抛出。

    用途见 `test_prompt_resolution_runs_before_baseline_guard` ——
    把"第 3 步先于第 5 步"这条顺序声称变成可执行断言。
    """


@dataclass(frozen=True, slots=True)
class ScriptedGenerator:
    """脚本化 `ContentGenerator`：按 `task_kind` 返回预设输出，并记录调用。

    **为什么必须记录调用**：ISS-178 的补测清单第 3 项要求"预算为 0 的项目上
    `dispatch_call` 一次都没被调用"。在本文件的形态下，`ContentGenerator`
    就是那个出网点 —— 断言"拒绝发生在第 5 步、任何人没花钱"的唯一办法是
    看它的调用记录，而不是看返回值。

    本文件的用例都止步于第 7 步的**门口**（估算就抛哨兵），因此
    `generate()` 实际上永远不该被调用 —— `calls == []` 正是这个断言。
    """

    calls: list[str] = field(default_factory=list)
    outputs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: 每次派发的入参快照（`task_kind` → 本次的 `content_version_no` / `prompt`）。
    #: ISS-178 的第 ④ 项要求"同一幂等键连调两次只产生一次真实派发"，
    #: 而"一次"这件事必须能连**版本号**一起断（幂等键的成分之一就是它）。
    dispatched: list[dict[str, Any]] = field(default_factory=list)

    def generate(
        self,
        *,
        task_kind: str,
        prompt: str,
        prompt_version_id: uuid.UUID,
        content_id: uuid.UUID,
        content_version_no: int,
        model_profile_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> GenerationDispatch:
        """记录本次调用；**给了预设输出就返回它**，否则抛（见 `outputs` 的 docstring）。"""
        self.calls.append(task_kind)
        self.dispatched.append(
            {
                "task_kind": task_kind,
                "content_version_no": content_version_no,
                "prompt_version_id": prompt_version_id,
                "prompt": prompt,
            }
        )
        if task_kind in self.outputs:
            # ISS-178 第 ① 项：「脚本化生成器（替换 gateway 派发，可断言调用次数）」。
            # 只"可断言调用次数"还不够 —— 那些用例要走到**派发之后**（校验/落库），
            # 而恒抛的桩会让执行停在派发点。给出预设输出这一分支，
            # 才能让"派发发生了几次"与"派发之后的哪一步失败"两件事分开断言。
            return GenerationDispatch(
                structured_output=self.outputs[task_kind],
                # 桩给 `None`：那一列可空（`GenerationDispatch` 的 docstring 明写）。
                provider_call_id=None,
                state="SUCCEEDED",
            )
        # 没给预设输出 ⇒ 保持原来的语义：本文件的用例预期只到"第 7 步门口"。
        # 抛而不是静默返回：静默返回会让"钱花了"这件事从断言里消失。
        raise AssertionError(
            f"生成器被调用了 kind={task_kind!r}，但本文件的用例预期"
            "只把执行推到第 7 步的预估点（哨兵处），且没有为它预设输出。"
            "这说明执行越过了哨兵，或哨兵没装上 —— 用例的前提已经不成立，"
            "不能继续。（若这个用例**本就要**走到派发，请给它 `outputs`。）"
        )


# ===========================================================================
# 夹具
# ===========================================================================


@dataclass(frozen=True, slots=True)
class ExecEnv:
    settings: Settings
    project_id: uuid.UUID
    owner_id: uuid.UUID
    provider_id: uuid.UUID
    model_profile_id: uuid.UUID


def _create_user(settings: Settings, *, name: str = "Exec 测试用户") -> uuid.UUID:
    with session_scope(settings) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name=name,
            role=ROLE_OPERATOR,
        )
        return user.id


def _seed_project(settings: Settings, *, owner_id: uuid.UUID) -> uuid.UUID:
    with session_scope(settings) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name=f"产品-{uuid.uuid4().hex[:6]}")
        session.add(product)
        session.flush()
        project = Project(
            project_code=f"EXEC-{uuid.uuid4().hex[:8].upper()}",
            name="执行层测试项目",
            brand_id=brand.id,
            product_id=product.id,
            owner_id=owner_id,
            target_delivery_count=10,
            # `projects.due_date` 是 NOT NULL（psql 实测）—— 漏它会得到
            # 一条 `IntegrityError: null value in column "due_date"`，
            # 而那个报错指向的是夹具而不是被测代码。
            due_date=date(2030, 12, 31),
        )
        session.add(project)
        session.flush()
        ProjectRepository(session).add_owner_and_collaborators(
            project.id, owner_id=owner_id, collaborator_ids=()
        )
        return project.id


def _seed_provider(settings: Settings) -> tuple[uuid.UUID, uuid.UUID]:
    with session_scope(settings) as session:
        repo = GatewayRepository(session)
        provider = repo.insert_provider(
            name=f"供应商-{uuid.uuid4().hex[:8]}",
            capability_types=[CAPABILITY_TEXT_STRUCTURED],
            network_mode=NETWORK_EXTERNAL,
            endpoint="https://api.test.invalid/v1",
        )
        session.flush()
        profile = repo.insert_model_profile(
            provider_id=provider.id,
            model_name=f"model-{uuid.uuid4().hex[:6]}",
            capabilities=[CAPABILITY_TEXT_STRUCTURED],
        )
        session.flush()
        return provider.id, profile.id


@pytest.fixture
def env() -> ExecEnv:
    reset_engines()
    settings = _make_settings()
    owner = _create_user(settings)
    project_id = _seed_project(settings, owner_id=owner)
    provider_id, profile_id = _seed_provider(settings)
    return ExecEnv(
        settings=settings,
        project_id=project_id,
        owner_id=owner,
        provider_id=provider_id,
        model_profile_id=profile_id,
    )


@pytest.fixture
def stub_prompt(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把**第 3 步**（解析激活提示词）替换成返回一个固定的合成版本。

    ## 为什么必须桩掉它（而不是"在库里建一个真模板"）

    见模块 docstring：`list_templates` **全局可见、不按项目隔离**，于是
    "第 3 步能不能过"取决于库里此刻有什么（含并行会话留下的行）。
    桩掉之后，本文件的结论只对**被测的那一步**（第 5 步守卫）敏感 ——
    这正是判据该有的样子：**一次只让一个变量动**。

    这张桩**不改任何一行产品代码**：它只是把 `service` 模块命名空间里的
    `resolve_active_version` 换掉（`monkeypatch` 在用例结束后自动还原）。
    """
    synthetic_template_id = uuid.uuid4()
    synthetic_version_id = uuid.uuid4()

    def _fake_resolve(prompt_repo: Any, *, task_kind: str) -> ActivePromptVersion:
        return ActivePromptVersion(
            template_id=synthetic_template_id,
            version_id=synthetic_version_id,
            task_kind=task_kind,
            # 不含占位符 ⇒ 将来若真有用例越过第 5 步走到渲染，
            # `check_renderable` / `render_system_prompt` 也不会替它提前失败。
            system_template="你是文案助手。",
            input_schema={},
            output_schema={},
        )

    monkeypatch.setattr(c_service, "resolve_active_version", _fake_resolve)
    yield


@pytest.fixture
def step7_sentinel(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把第 7 步的预估点换成抛 `Step7Reached` —— **判定点在守卫之后**。

    这是本文件判别力的来源，见模块 docstring 的表格。
    """

    def _fake_estimate(*_args: Any, **_kwargs: Any) -> Any:
        raise Step7Reached(
            "执行已越过第 5 步（基线守卫），到达第 7 步的预估点。"
        )

    monkeypatch.setattr(c_service, "estimate_generation_cost", _fake_estimate)
    yield


@pytest.fixture
def stub_billing(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把**计费三件套**换成纯内存替身 —— 让执行能越过第 7 步的预估/预占/结算。

    ## 为什么必须桩掉（实测反证，见模块 docstring）

    第 7 步的顺序是**预估 → 预占 → 渲染 → 派发**。而
    `estimate_generation_cost` 会读 `get_effective_limits(…)`，项目没配
    `batch.limits` 时抛 `ApiError(details.reason="LIMITS_NOT_CONFIGURED")`。
    于是**任何**想走到派发点的用例，都先要在库里铺一整套计费配置
    （限额 + 计价版本 + 币种）—— 那是 WP24/WP25 的铺底，不是本文件要判的东西。

    本夹具把三个桥接函数换成"恒成功且金额固定"的替身，让被测的那一段
    （派发 → 折叠）成为唯一变量。`amount` 固定为 `Decimal("1")`：
    本文件不判金额口径（那是 `test_content_wp26.py` 的活），
    只要它是个合法 `Decimal` 就能让 `reserve` 与 `settle` 走通。

    ## 为什么替身返回的对象**不必落库**

    `estimate` / `reservation` 的返回值只有 `.total_amount` 与 `.id`
    被 `execute_generation` 用到（结算入参、`pending_reservations` 记账）。
    没有一行代码会拿它们去查库 —— 因为真正的预占/结算已经在替身内部
    "假装做完"了。这也是本夹具能用纯内存对象的原因。
    """

    def _fake_estimate(
        _repo: Any, *, project_id: uuid.UUID, batch_kind: str, **_kwargs: Any
    ) -> CostEstimateView:
        return CostEstimateView(
            id=uuid.uuid4(),
            project_id=project_id,
            batch_kind=batch_kind,
            quantity=1,
            currency="CNY",
            state="ESTIMATED",
            total_amount=Decimal("1"),
            actual_amount=None,
            variance_amount=None,
            pricing_version_id=None,
            lines=(),
        )

    def _fake_reserve(
        _repo: Any, *, project_id: uuid.UUID, content_id: uuid.UUID | None = None,
        **_kwargs: Any,
    ) -> ReservationView:
        return ReservationView(
            id=uuid.uuid4(),
            project_id=project_id,
            state="RESERVED",
            currency="CNY",
            amount=Decimal("1"),
            settled_amount=None,
            ref=None if content_id is None else f"content:{content_id}",
            note=None,
        )

    def _fake_settle(*_args: Any, **_kwargs: Any) -> Any:
        # 结算成功即可 —— 本文件不判结算口径（见夹具 docstring）。
        return type("Settled", (), {"state": "SETTLED"})()

    def _fake_release(*_args: Any, **_kwargs: Any) -> Any:
        return None

    monkeypatch.setattr(c_service, "estimate_generation_cost", _fake_estimate)
    monkeypatch.setattr(c_service, "reserve_generation_budget", _fake_reserve)
    monkeypatch.setattr(c_service, "settle_generation_budget", _fake_settle)
    monkeypatch.setattr(c_service, "release_generation_budget", _fake_release)
    yield


def _register_brief_version(env: ExecEnv) -> uuid.UUID:
    """注册一个 `BRIEF` 节点并返回其 node id —— **不**给它任何 `brief_claims`。

    这正是 ISS-177 的场景：Brief **存在**（基线也认它），但它是空的，
    于是 `claim_scope` 如实返回 `(frozenset(), 0)`。
    """
    with session_scope(env.settings) as session:
        return register_version(
            VersionRepository(session),
            entity_type="BRIEF",
            entity_id=env.project_id,
            payload={"probe": "brief"},
            project_id=env.project_id,
            created_by=env.owner_id,
        ).node_id


def _seed_baseline(
    env: ExecEnv, *, brief_version_id: uuid.UUID | None
) -> uuid.UUID:
    """生效基线 + 激活它，返回 `baseline_id`。

    ## `brief_version_id` 的两个 node 都必须**先注册**

    `production_baselines.id` 与 `brief_version_id` 都是 → `version_nodes.id`
    的**延迟**外键（psql `\\d production_baselines` 实测）。所以它们不能是随手
    `uuid4()` —— 必须先 `register_version` 拿到真实 node id。

    `brief_version_id=None` 建模的是 repository docstring 里说的
    "合法的没有 Brief 的基线" —— 那**不该**被 ISS-177 的守卫判为拒绝
    （详见 `test_no_brief_baseline_is_not_treated_as_unready`）。
    """
    from rednote.modules.baseline.repository import BaselineRepository

    with session_scope(env.settings) as session:
        baseline_id = register_version(
            VersionRepository(session),
            entity_type="BASELINE",
            entity_id=env.project_id,
            payload={"probe": "baseline"},
            project_id=env.project_id,
            created_by=env.owner_id,
        ).node_id
        repo = BaselineRepository(session)
        row = repo.insert_baseline(
            baseline_id=baseline_id,
            project_id=env.project_id,
            brief_version_id=brief_version_id,
            ruleset_version="v1",
            snapshot={"probe": True},
            supersedes_id=None,
            change_reason="执行层补测：造一条生效基线",
            created_by=env.owner_id,
        )
        assert row.id == baseline_id
        # OP-151：基线在 COMMIT 时必须在**同一个事务**里有一条
        # 「步骤 3 / BASELINE / APPROVED」的批准，否则 `step_approvals`
        # 的约束触发器会拒绝整个事务。这条不是可选装饰 —— 少它时报错是
        # 一条 `CheckViolation` 而不是"没有基线"。
        repo.insert_approval(
            project_id=env.project_id,
            step_no=3,
            subject_kind="BASELINE",
            subject_version_id=baseline_id,
            decision="APPROVED",
            reason="执行层补测：给生效基线配一条步骤 3 批准",
            decided_by=env.owner_id,
            supersedes_id=None,
        )
        repo.set_active_baseline(project_id=env.project_id, baseline_id=baseline_id)
        return baseline_id


def _start_content(env: ExecEnv) -> uuid.UUID:
    """发一批内容（1 条）并返回 `content_id`。

    用 `start_generation` 而不是裸 INSERT：后者要自己拼 `generation_request`
    的 JSON，而拼错的表现是第 2 步抛 `GENERATION_REQUEST_INVALID` ——
    **那个报错是真的，但它指向的不是要测的东西**（路径 TypeError 的老套路）。
    """
    with session_scope(env.settings) as session:
        repo = ContentRepository(session)
        request = GenerationRequest(
            scope=SCOPE_COPY,
            batch_kind="TRIAL",
            model_profile_id=env.model_profile_id,
        )
        rows, created = c_service.start_generation(
            repo,
            project_id=env.project_id,
            request=request,
            content_count=1,
            idempotency_key=f"exec-{uuid.uuid4().hex[:10]}",
            created_by=env.owner_id,
        )
        assert created is True
        assert len(rows) == 1
        return rows[0].id


def _run(env: ExecEnv, content_id: uuid.UUID, generator: ScriptedGenerator) -> Any:
    """在一个事务里跑 `execute_generation`。"""
    with session_scope(env.settings) as session:
        return execute_generation(
            ContentRepository(session),
            content_id=content_id,
            generator=generator,
            actor_id=env.owner_id,
        )


def _enqueue_generate_job(
    env: ExecEnv, *, content_id: uuid.UUID | None
) -> uuid.UUID:
    """入队一个 `CONTENT_GENERATE` 任务，返回 `job_id`。

    `content_id=None` 建模的是**装配故障**（端点忘了传、或有人手工造任务）——
    它在这个 schema 下完全合法（`jobs.content_id` 刻意没有外键、可空），
    因此只有处理器能发现它。这正是 `test_handler_with_missing_target_fails_closed`
    要复现的形状。

    走 `jobs_service.enqueue_job` 而不是裸 INSERT：与端点
    （`content/api.py`）走的是同一条路（含 `job.enqueued` 事件与幂等语义），
    否则用例可能在一条**生产不会走**的路径上通过。
    """
    with session_scope(env.settings) as session:
        view = jobs_service.enqueue_job(
            JobRepository(session),
            kind=KIND_CONTENT_GENERATE,
            queue=QUEUE_CONTENT,
            idempotency_scope=IDEMPOTENCY_SCOPE_CONTENT_GENERATE,
            # 键里带随机后缀：同一用例内多次入队（或与并行会话的残留）
            # 不会撞唯一约束（`UQ(idempotency_scope, idempotency_key)`）。
            idempotency_key=f"exec-{uuid.uuid4().hex[:12]}",
            input_hash=uuid.uuid4().hex,
            project_id=env.project_id,
            content_id=content_id,
        )
        return view.job_id


def _claim_job(env: ExecEnv, job_id: uuid.UUID) -> ClaimedJob:
    """**只领指定的那一个任务**，拿到带 `fence_token` 的凭据。

    ## 为什么不用 `jobs_service.claim_jobs`

    `claim_jobs` 的签名是"按队列领一批"（`limit=MAX_CLAIM_BATCH`=20，
    排序 `priority DESC, created_at ASC`），**没有按 id 过滤的入参**。
    而检索这个私有库实测有 **20 条以上**历史遗留的 `QUEUED` 任务
    （历次跑 `test_content_wp26.py` 攒下的、`created_at` 更早）——
    于是"入队 → `claim_jobs` → 挑出我的那条"这条路会因为我那一行
    排在 limit 之外而**永远挑不到**。

    那是一个**用例依赖"队列表恰好干净"**的设计缺陷：判据的成立与否
    取决于库里有多少别人的行，而那不是判据该管的事。改成按 id 单条领取后，
    队列里堆多少条都与本用例无关。

    ## 为什么用仓储层的三个原语而不是手工造 `ClaimedJob`

    这里复刻的正是 `claim_jobs` 对一个任务做的事（`claim_job` →
    `next_attempt_no` → `insert_attempt`），因此 `fence_token` 与
    `attempt_id` 都和库里那行**真的一致** —— 将来处理器若加了按 fence 的
    写回，用例仍然成立。手工造则会在那一刻静默失效。
    """
    worker_id = f"worker-{uuid.uuid4().hex[:8]}"
    with session_scope(env.settings) as session:
        repo = JobRepository(session)
        fence = repo.claim_job(
            job_id=job_id,
            worker_id=worker_id,
            lease_duration=timedelta(seconds=LEASE_DURATION_SECONDS),
            now=repo.now(),
        )
        assert fence is not None, (
            f"任务 {job_id} 不可领取（`claim_job` 返回 None）。"
            "它必须在 `QUEUED` 态且未被别人持有 —— 若刚入队就领不到，"
            "说明有别的会话在并发消费这个库。"
        )
        attempt_no = repo.next_attempt_no(job_id)
        attempt = repo.insert_attempt(
            job_id=job_id,
            attempt_no=attempt_no,
            worker_id=worker_id,
            fence_token=fence,
        )
        repo.flush()
        return ClaimedJob(
            job_id=job_id,
            kind=KIND_CONTENT_GENERATE,
            attempt_id=attempt.id,
            fence_token=fence,
            attempt_no=attempt_no,
            worker_id=worker_id,
        )


def _synthetic_claimed(env: ExecEnv, *, job_id: uuid.UUID) -> ClaimedJob:
    """造一个**库里不存在**的 `ClaimedJob` —— 只给"行不存在"那条用例用。

    为什么不能复用 `_claim_job`：那条路径必然产生一个**真实存在**的 job 行，
    而本用例要的恰好是相反的世界（`repo.get_job` 返回 `None`）。
    手工造的凭据不需要 `fence_token` 与库一致 —— 处理器在读到 `job is None`
    时就返回了，根本不会用它写回。
    """
    return ClaimedJob(
        job_id=job_id,
        kind=KIND_CONTENT_GENERATE,
        attempt_id=uuid.uuid4(),
        fence_token=1,
        attempt_no=1,
        worker_id="worker-synthetic",
    )


def _build_gateway_generator(env: ExecEnv, claimed: ClaimedJob) -> GatewayGenerator:
    """在**一个事务**里造 `GatewayGenerator`，返回后立刻退出该事务。

    仅用于断言"`adapter` 非 `None` 时造的是 `GatewayGenerator`"这种
    **形状**判据 —— 不需要真的调它（真调要 adapter 与出网策略）。
    `GatewayGenerator` 是 `frozen` dataclass，构造完就不依赖 session 了。
    """
    with session_scope(env.settings) as session:
        return c_handlers._build_generator(
            claimed,
            JobRepository(session),
            adapter=object(),
            output_schema_version=1,
            policy_version="v1",
        )


# ===========================================================================
# 第 0 层： AST 判据 —— 守卫**真的连到了**服务层（ISS-177 的根子）
# ===========================================================================


def _keyword_reason_values(tree: ast.Module) -> list[str]:
    """收集**剥掉 docstring 后**所有 `f(..., reason=<字面量或 Name>)` 的取值。

    返回 AST 节点的源码表达式字符串（`unparse`），让断言能同时接受
    字面量与常量名两种写法。
    """
    detached = _strip_docstrings(tree)
    found: list[str] = []
    for node in ast.walk(detached):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "reason":
                found.append(ast.unparse(keyword.value))
    return found


def _strip_docstrings(tree: ast.Module) -> ast.Module:
    """剔除所有 docstring。

    **这一步就是 ISS-177 取证陷阱的对症药**：`repository.claim_scope` 的
    docstring 里写着「服务层据此拒绝（`REASON_BASELINE_NOT_READY`）」，
    而它永远不会成为一次函数调用 —— 只做文本搜索会让那句话把判据喂成恒绿。
    """
    for node in ast.walk(tree):
        docstring_holder = isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        )
        if not docstring_holder:
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body.pop(0)
    return tree


def test_guard_reason_is_reachable_from_service() -> None:
    """★ `REASON_BASELINE_NOT_READY` 必须在 `service.py` 里**被用作 `reason=` 实参**。

    ## 这条判据为什么不用文本搜索

    本缺陷（ISS-177）被发现，就是因为此前用
    「`REASON_BASELINE_NOT_READY` 在 `service.py` 里出现吗」当判据，
    而答案是**出现** —— 出现在 `claim_scope` 的一句声称里。那是 ISS-95 的
    第三次现身：**文档自称有防线，比没有防线更危险**，因为它会让人跳过检查。

    ## 为什么它不需要数据库就能跑

    它检查的是**源码形状**而不是运行时行为 —— 这也让它成为最可靠的
    一条回归护栏：任何一次重构只要把守卫删了，它立刻变红，不受数据影响。
    """
    source = SERVICE_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SERVICE_PY))

    bare_occurrences = source.count("REASON_BASELINE_NOT_READY")
    # 文本内容**至少**要出现一次：常量得先在源码里写得出来（作为 sanity check）。
    assert bare_occurrences >= 1, (
        f"{SERVICE_PY} 里连 'REASON_BASELINE_NOT_READY' 这个字符串都没有。"
        "这条断言本身不是证据（见 docstring），它只是防止判据指向错文件。"
    )

    reasons = _keyword_reason_values(tree)
    matched = [expr for expr in reasons if expr == "REASON_BASELINE_NOT_READY"]
    assert matched, (
        "`service.py` 剥掉 docstring 后，没有任何一处调用以 "
        "`reason=REASON_BASELINE_NOT_READY` 传参 —— 即 ISS-177 未修："
        "`claim_scope` 的 docstring 声称服务层会拒绝空判据域，"
        "而服务层从未抛过这个 reason。\n"
        f"  源码里该字符串出现 {bare_occurrences} 次（可能全在注释/docstring 里）\n"
        f"  实际 `reason=` 实参共 {len(reasons)} 个：{sorted(set(reasons))}"
    )


def test_guard_reason_constant_exists() -> None:
    """`REASON_BASELINE_NOT_READY` 常量确实存在（防止上一条指向拼错的常量名）。"""
    from rednote.modules.content import types as c_types

    constant = getattr(c_types, "REASON_BASELINE_NOT_READY", None)
    assert isinstance(constant, str) and constant, (
        "`types.REASON_BASELINE_NOT_READY` 不存在或不是非空字符串。"
        "上一条 AST 判据只认这个名字 —— 若常量改名而这里没跟上，"
        "会静默变成 '永远搜不到'。"
    )


# ===========================================================================
# 第 1 层：仓储层——空判据域是**如实报 0**，不是错误
# ===========================================================================


def test_claim_scope_reports_zero_for_version_without_claims(env: ExecEnv) -> None:
    """没有任何 `brief_claims` 的版本 ⇒ `(frozenset(), 0)`，且不报错。

    这是 ISS-177 的前半段：**仓储层的行为是对的**（如实报数）。
    缺陷在后半段 —— 没有任何消费方把这个 0 变成一次拒绝。
    """
    brief_version_id = _register_brief_version(env)
    with session_scope(env.settings) as session:
        repo = ContentRepository(session)
        known, total = repo.claim_scope(brief_version_id)

    assert known == frozenset()
    assert total == 0


def test_claim_scope_reports_zero_for_none_version(env: ExecEnv) -> None:
    """`brief_version_id is None` ⇒ 同样 `(frozenset(), 0)`。

    与上一条的差别在**服务层**如何处置这两个 0 —— 见下面两条用例：
    一个是"基线还没走 Brief 流程"（合法），一个是"Brief 存在却是空的"（不完整）。
    仓储层把它们报成同一个值是对的（都是"没有 claim"），
    区分的职责在服务层，因为只有它知道基线这两个 0 各自的语义。
    """
    with session_scope(env.settings) as session:
        known, total = ContentRepository(session).claim_scope(None)

    assert known == frozenset()
    assert total == 0


# ===========================================================================
# 第 2 层：服务层——两个 0 必须被区别对待
# ===========================================================================


def test_execute_generation_rejects_baseline_with_empty_claim_scope(
    env: ExecEnv, stub_prompt: None
) -> None:
    """★ ISS-177 正面：有生效基线、其 Brief **存在但没有 claim** ⇒ 拒绝。

    ## 这条守卫为什么必须有

    放行时 `validate_bindings` 的两个入参是 `(frozenset(), 0)`：
    "绑定的 claim 必须存在"这条规则的判据域是**空集**，而"绑满即视为没绑"
    那条规则的**分母是 0** ⇒ `_ALL_CLAIMS_RULE_MIN_TOTAL` 那条规则不启用
    （分母 0 时"全绑了"的比例无从计算）⇒ 模型绑 0 条就能通过 ⇒
    **R4 整条降级成"不判绑定"，且不报错**。

    ## 为什么用 `pytest.raises` 断言 `.reason` 而不是消息文本

    `reason` 是本包**跨模块**的错误契约（`types.py` 的一组常量，
    `api.py` 的 `_HTTP_BY_REASON` 按它翻 HTTP 码）；消息是给人读的，
    文本一变就红。断言 `reason` 才是断言契约，断言消息是断言措辞。

    ## 这条用例的探针位置

    守卫在第 5 步，**早于**第 7 步的任何出网动作。因此这条用例只需要
    "§到第 5 步"就够了 —— `generator.calls == []` 是"没花钱"的断言。
    下面两条**边界**用例则必须越过第 5 步，故用哨兵（见其 docstring）。
    """
    brief_version_id = _register_brief_version(env)
    _seed_baseline(env, brief_version_id=brief_version_id)
    content_id = _start_content(env)
    generator = ScriptedGenerator(calls=[], outputs={})

    with pytest.raises(ContentGenerationError) as excinfo:
        _run(env, content_id, generator)

    assert excinfo.value.reason == REASON_BASELINE_NOT_READY, (
        f"期望 reason={REASON_BASELINE_NOT_READY!r}，实际 "
        f"{excinfo.value.reason!r}（message={excinfo.value.message!r}）。"
    )
    # 拒绝必须发生在**任何人花钱之前**：出网点一次都没被调用。
    assert generator.calls == [], (
        f"守卫应当在第 5 步就返回，但生成器已被调用 {generator.calls} 次。"
        "这意味着 `total_claim_count == 0` 的基线上仍产生了出网调用。"
    )


def test_no_brief_baseline_is_not_treated_as_unready(
    env: ExecEnv, stub_prompt: None, step7_sentinel: None
) -> None:
    """★ ISS-177 边界：`brief_version_id IS NULL` 的基线**不是**"基线未就绪"。

    ## 为什么要区分（这条比正面用例更容易写错）

    `repository.claim_scope` 的 docstring 把两种情况并列：

    > `brief_version_id is None` 与"基线没有 claim"，两者都返回
    > `(frozenset(), 0)` —— 服务层据此拒绝

    但两者语义不同：`brief_version_id IS NULL` 建模的是
    **"这个项目本就没走 Brief 流程"** —— `baseline_brief_version` 的
    docstring 自己就写着那是"合法的'没有 Brief 的基线'"。把它判成
    `BASELINE_NOT_READY` 会让整个不走 Brief 的项目**永远无法生成**，
    而这属于"加了一条守卫，把合法路径也堵了"——比原来的洞更糟
    （原洞是"R4 静默降级"，新洞是"功能不可用"）。

    ## ★ 怎么证明"没有被守卫拦住"（本文件的判别力所在）

    **不能用"断言抛 `PromptUnavailable`"** —— 那条路抛在**第 3 步**，
    在守卫（第 5 步）**之前**：守卫误伤与没走到守卫，两种世界同形
    （本文件初版就是这么写的，已被变异测试证伪；见模块 docstring）。

    正确做法是**哨兵越过守卫**：把第 7 步的预估点换成抛 `Step7Reached`。

    - 守卫**没**误伤 ⇒ 执行走到第 7 步 ⇒ 抛 `Step7Reached` ⇒ 本条绿；
    - 守卫**误伤**了 ⇒ 第 5 步就抛 `BASELINE_NOT_READY` ⇒ 本条红。

    这条用例**只**在"守卫正确区分两个 0"时通过 —— 这才有判别力。
    """
    _seed_baseline(env, brief_version_id=None)
    content_id = _start_content(env)
    generator = ScriptedGenerator(calls=[], outputs={})

    with pytest.raises(Step7Reached):
        _run(env, content_id, generator)

    assert generator.calls == [], "守卫放行不该产生任何派发（钱是第 7 步之后才花的）。"


def test_execute_generation_without_baseline_skips_scope_guard(
    env: ExecEnv, stub_prompt: None, step7_sentinel: None
) -> None:
    """没有生效基线 ⇒ 守卫不介入（同样用哨兵越过第 5 步）。

    与上一条的区别：那边是"基线存在但没 Brief"，这边是"压根没基线"。
    两者映射到同一句维护者容易写错的守卫：

    ```python
    if total_claim_count == 0:                              # ❌ 无条件：把"无基线"也拒了
    if baseline_id is not None and total_claim_count == 0:  # ✅ 只在有基线时判
    ```

    本用例就是防止有人把守卫写成前者 —— 而**只有哨兵探针能抓到它**：
    无条件守卫下第 5 步会抛 `BASELINE_NOT_READY`，哨兵不会触发。
    """
    content_id = _start_content(env)
    generator = ScriptedGenerator(calls=[], outputs={})

    with pytest.raises(Step7Reached):
        _run(env, content_id, generator)

    assert generator.calls == []


def test_prompt_resolution_runs_before_baseline_guard(
    env: ExecEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 顺序判据：**第 3 步（提示词）排在 第 5 步（守卫）之前**。

    ## 这条用例存在的唯一理由

    它是上面两条边界用例修复的**元证据** —— 说明"为什么不能用
    `PromptUnavailable` 当边界探针"。

    做法：把第 3 步换成抛一个**独有的**哨兵 `PromptStepProbe`，
    而**不桩**第 5 步，且把基线造成"空判据域"（第 5 步若有机会执行，
    必然抛 `BASELINE_NOT_READY`）：

    - 实测抛 `PromptStepProbe` ⇒ 第 3 步确实**先于**第 5 步 ⇒ 绿；
    - 若有人把守卫提到第 3 步之前 ⇒ 抛 `BASELINE_NOT_READY` ⇒ 红。

    它把"步骤顺序"从 docstring 里的一句声称，变成一条**可执行的断言**
    （纪律：凡声称，必有判据）。
    """

    def _probe_resolve(prompt_repo: Any, *, task_kind: str) -> ActivePromptVersion:
        raise PromptStepProbe(f"第 3 步先于第 5 步被执行（kind={task_kind}）。")

    # 基线是**空判据域**的 ⇒ 第 5 步若有机会执行，必然抛 BASELINE_NOT_READY。
    _seed_baseline(env, brief_version_id=_register_brief_version(env))
    content_id = _start_content(env)
    generator = ScriptedGenerator(calls=[], outputs={})

    monkeypatch.setattr(c_service, "resolve_active_version", _probe_resolve)

    with pytest.raises(PromptStepProbe):
        _run(env, content_id, generator)

    assert generator.calls == []


def test_content_missing_is_rejected_before_baseline_check(
    env: ExecEnv, stub_prompt: None
) -> None:
    """内容不存在 ⇒ `CONTENT_NOT_FOUND`，且**不查**任何基线相关表。

    这是顺序判据：第 1 步必须在第 5 步之前。它的价值不在"内容不存在会报错"
    （那是显然的），而在**固定了"第 5 步不是第一步"** —— 若有人把
    基线守卫提到最前面，"内容不存在"就会以 `BASELINE_NOT_READY` 的面目出现，
    排障方向完全错。
    """
    _seed_baseline(env, brief_version_id=_register_brief_version(env))
    generator = ScriptedGenerator(calls=[], outputs={})

    with pytest.raises(ContentGenerationError) as excinfo:
        _run(env, uuid.uuid4(), generator)

    assert excinfo.value.reason == REASON_CONTENT_NOT_FOUND
    assert generator.calls == []


# ===========================================================================
# 第 3 层：接线层 —— 处理器与注册表（ISS-178 的另一半）
# ===========================================================================
#
# **为什么这一层也归 ISS-178**：那条缺陷的名字是「**执行路径零覆盖**」。上面
# 三层判的是 `execute_generation` 本体，而"执行"在生产里**不从这里进入** ——
# 它从 `content_generate_handler` 进来（WP13 的 Worker 调它）。此前实测：
# `content_generate_handler` 在 `tests/` 全目录命中 **0 次**，
# `content.build_task_registry` 也**无任何用例**（WP17 / WP18 都有）。
#
# 于是"零覆盖"其实是两段：`execute_generation` 本体一段（上一组用例补），
# 处理器与注册表一段（本组补）。只补前一段的话，下面这些**真实的**缺陷
# 都不够红：
#
#   · 有人把 `build_task_registry` 里的 `KIND_CONTENT_GENERATE` 改成别的串
#     ⇒ 生产 Worker 领到 `CONTENT_GENERATE` 任务后走 `_invoke_handler` 的
#     `NO_HANDLER` 分支（`jobs/service.py:471`）—— **不报错、不重试、
#     静默地把任务判成失败**（`retryable=False`），排障时看到的是
#     "CONTENT_GENERATE 没有处理器"，而代码里明明注册了（名字对不上）；
#   · 有人把 `adapter` 的默认值从 `None` 改成一个"看起来能调"的东西
#     ⇒ `UnavailableGenerator` 的 fail-closed 保护被绕过，
#     "链路未验证"变成一次真的出网调用。
#
# 这两条正是 WP17（`test_parsing_wp17.py:1114`）与 WP18（`test_brief_wp18.py:762`）
# 各自已经守住的同一个形状 —— 本组把它们对齐到本包。


def test_task_registry_registers_content_generate() -> None:
    """★ `kind=CONTENT_GENERATE` 必须在注册表里 —— 否则任务静默判失败。

    ## 这条为什么不是"显然的"

    注册表与入队点**不在同一个文件**：入队发生在 `content/api.py`（写
    `KIND_CONTENT_GENERATE`），注册发生在 `content/handlers.py`。两者靠
    同一个常量对齐，而**没有任何东西强制注册表必须包含它** ——
    `build_task_registry()` 返回一个空注册表在语法上完全合法。

    漏注册的后果不是报错而是**静默的 `NO_HANDLER` 失败**（见上），
    所以必须有一条用例把"入队的 kind"与"已注册的 kind"焊在一起。

    ## 为什么用 `in` 而不是 `==`

    WP18 用了 `==`（它注册且仅注册两个 kind，是个闭合集合）。本包将来可能
    在同一个注册表里加 kind（例如批量生成的处理器），用 `==` 会让**每次新增
    kind 都要来改这条断言** —— 而改断言比加 kind 更容易出错（有人会顺手改成
    `>=` 或者删掉这条）。用 `in` 断言"这一个必须在"，语义更窄也更稳。
    """
    kinds = c_handlers.build_task_registry().registered_kinds()
    assert KIND_CONTENT_GENERATE in kinds, (
        f"`content.build_task_registry()` 没有注册 {KIND_CONTENT_GENERATE!r}，"
        f"实际注册的是 {sorted(kinds)}。入队点（`content/api.py`）会建出这个 "
        "kind 的任务，而 Worker 找不到处理器 ⇒ `NO_HANDLER` ⇒ 任务被判失败"
        "且**不可重试**，排障时看不到任何异常堆栈。"
    )


def test_task_registry_does_not_mutate_default() -> None:
    """本包的注册表是**新实例**，不污染 WP13 的默认集合。

    照 WP17 `test_task_registry_does_not_mutate_default` 的形状。
    `TaskHandlerRegistry.register` 返回新实例，但"调用 `build_task_registry`
    会不会改到默认集合"取决于**本包怎么用它** —— 若有人改成
    `build_default_registry()` 再原地 `register`，默认集合就被污染了，
    而症状是**测试之间产生顺序依赖**（后跑的用例多了一个处理器），
    极难定位。这条用例把它钉住。
    """
    default_before = build_default_registry().registered_kinds()
    c_handlers.build_task_registry()
    assert build_default_registry().registered_kinds() == default_before, (
        "`build_task_registry()` 改动了 `build_default_registry()` 的集合 —— "
        "它必须返回新实例（`TaskHandlerRegistry.register` 的不可变语义）。"
    )


def test_default_adapter_is_fail_closed() -> None:
    """★ `adapter=None` 时造出的是 `UnavailableGenerator`（**调用即抛**）。

    ## 这条守的是什么

    `content_generate_handler` 的 `adapter` 默认 `None`，落到
    `_build_generator` 的 `if adapter is None: return UnavailableGenerator()`。
    这条分支是**生产未接线时的唯一防线**：若有人把默认值改成一个能调的东西
    （或删掉这个分支、直接造 `GatewayGenerator`），"本地跑得通"就会变成
    **一次真的出网调用** —— 而那正是 ADR-10 要避免的盲区。

    ## 为什么直接测 `UnavailableGenerator` 而不是绕处理器

    绕处理器需要铺 billing / pricing / prompts 一整条链，而铺完之后
    断言的是"抛了 `GatewayUnavailable`" —— 与直接调用它的**结论完全相同**，
    代价却高得多。这里测的是**这个类本身的契约**：任何 `generate` 调用
    必抛，且 `reason` 是那个常量。处理器与它的接线由
    `test_handler_with_missing_target_fails_closed` 与下面两条用例覆盖。
    """
    generator = UnavailableGenerator()
    with pytest.raises(GatewayUnavailable) as excinfo:
        generator.generate(
            task_kind=TASK_CONTENT_COPY,
            prompt="你是文案助手。",
            prompt_version_id=uuid.uuid4(),
            content_id=uuid.uuid4(),
            content_version_no=1,
            model_profile_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
        )
    # ★ 用**字面量**断言 reason，而不是 `UnavailableGenerator.reason`。
    #
    # 两个理由：
    #   1) `UnavailableGenerator` 是 `@dataclass(frozen=True, slots=True)`，
    #      而 `slots=True` 会把这个字段换成 slot descriptor ——
    #      **类属性访问拿到的是 descriptor 对象，不是默认值**（实测：
    #      `UnavailableGenerator.reason` 是 `<member 'reason' of ...>`）。
    #      写 `== UnavailableGenerator.reason` 会拿一个 descriptor 去比字符串，
    #      必然是 False —— 而且报错信息里"期望值"看起来像个类名，
    #      极难看出是用例自己写错了。
    #   2) 即使改用实例访问（`UnavailableGenerator().reason`），
    #      两边仍然**同源**：有人把默认值改成 ""（或改名）时两边一起变，
    #      判据恒绿。而 reason 是**跨模块的排障口径**（按它过滤日志），
    #      把它的取值钉死才是这条判据该干的事。
    #
    # 用字面量钉常量的取值在本仓有先例：
    # `test_content_wp26.py:1115` 的 `assert KIND_CONTENT_GENERATE == "CONTENT_GENERATE"`。
    assert excinfo.value.reason == "GENERATOR_NOT_CONFIGURED", (
        f"未接线的生成器抛出的 reason 是 {excinfo.value.reason!r}，"
        "而排障口径（文档与日志过滤）约定的值是 'GENERATOR_NOT_CONFIGURED'。"
    )
    assert isinstance(excinfo.value, GatewayUnavailable), (
        "未接线必须抛 `GatewayUnavailable`（通道没通），而不是 `ValueError` 族"
        "（输入不对）—— 两者在 `handlers` 里的映射不同：前者可能是"
        "`WAITING_RECONCILIATION`，后者恒是 `FAILED`。"
    )
    # 实例字段本身也非空 —— 防止上面那条被"改成一个同样拼错的常量"绕过。
    assert UnavailableGenerator().reason, (
        "`UnavailableGenerator().reason` 是空值 —— 排障时按 reason 过滤会漏掉这一类。"
    )


def test_handler_with_missing_target_fails_closed(env: ExecEnv) -> None:
    """★ 任务没带 `content_id` ⇒ `FAILED` + `ERROR_TARGET_MISSING`，**不重试**。

    ## 这条为什么必须存在（它是"装配故障"的唯一可见痕迹）

    `jobs.content_id` 是 WP13 留下的**可空**关联位（`jobs/models.py:201`，
    刻意没有外键）。因此"有人手工造了一个 `CONTENT_GENERATE` 任务"
    或"端点忘了 `enqueue_job(content_id=…)`"在**数据库层完全合法** ——
    没有一个约束会拦住它。

    处理器是唯一能发现它的地方。而它的处置必须是：

    - `state=FAILED`（不是 `QUEUED`，也不是抛出去让框架折成可重试）；
    - `retryable=False`（重试一万次也不会长出一个 content_id）；
    - `error_code=ERROR_TARGET_MISSING`（**具名常量**，排障时按它过滤）。

    ## 为什么不断言"没查过基线表"

    处理器在 `job.content_id is None` 时就返回了，根本没构造
    `ContentRepository`。但"证明没做过某事"需要插桩计数，而本用例的
    判别力已经足够：`error_code` 是那个常量就说明走的是早退分支 ——
    若它往下走了，`execute_generation(content_id=None, …)` 会抛
    `CONTENT_NOT_FOUND`（`reason` 不同）。
    """
    job_id = _enqueue_generate_job(env, content_id=None)
    claimed = _claim_job(env, job_id)

    with session_scope(env.settings) as session:
        outcome = c_handlers.content_generate_handler(
            claimed, JobRepository(session)
        )

    assert outcome.state == STATE_FAILED
    # ★ 用**字面量**，不用 `c_handlers.ERROR_TARGET_MISSING`。
    #
    # 这条踩过坑：写成 `== c_handlers.ERROR_TARGET_MISSING` 时两边**同源** ——
    # 有人把这个常量的值改掉（例如改成 "TARGET_MISSING"），用例两边一起变，
    # **恒绿**。而它的值正是排障口径（按 error_code 过滤日志/告警），
    # 改了值就等于改了对外契约，必须有一条判据红。
    # 先例：`test_content_wp26.py:1115` 的
    # `assert KIND_CONTENT_GENERATE == "CONTENT_GENERATE"`。
    assert outcome.error_code == "CONTENT_GENERATE_TARGET_MISSING", (
        f"期望 'CONTENT_GENERATE_TARGET_MISSING'，实际 {outcome.error_code!r}。"
        "若这里是 CONTENT_NOT_FOUND，说明处理器在 content_id 为空时**往下走了**"
        "—— 那会把装配故障伪装成业务失败。"
    )
    assert c_handlers.ERROR_TARGET_MISSING == "CONTENT_GENERATE_TARGET_MISSING", (
        "`handlers.ERROR_TARGET_MISSING` 的取值变了 —— 它是排障与告警的过滤口径，"
        "改名等于对外契约变更（且会让既有的告警规则静默失配）。"
    )
    assert outcome.retryable is False, "装配故障不可重试（重试不会长出 content_id）。"


def test_handler_with_missing_job_row_fails_closed(env: ExecEnv) -> None:
    """任务行不存在（job_id 是悬空的）⇒ 同样走早退分支，不抛异常。

    `get_job` 返回 `None` 与 `content_id is None` 在处理器里走**同一个分支**
    （`if job is None or job.content_id is None:`）。这里单独固定"行不存在"
    这一侧：它建模的是"任务被删了但 Worker 手里还有凭据"（或在别的库上跑）。
    处理器**不能抛** —— Worker 的主循环会因此中断，而正确行为是
    "这一个失败、继续下一个"（`_invoke_handler` 的 docstring）。
    """
    claimed = _synthetic_claimed(env, job_id=uuid.uuid4())

    with session_scope(env.settings) as session:
        outcome = c_handlers.content_generate_handler(
            claimed, JobRepository(session)
        )

    assert outcome.state == STATE_FAILED
    assert outcome.error_code == "CONTENT_GENERATE_TARGET_MISSING"
    assert outcome.retryable is False


def test_handler_with_unconfigured_adapter_fails_without_spending(
    env: ExecEnv, stub_prompt: None, stub_billing: None
) -> None:
    """★ 处理器端到端：目标存在、但 `adapter=None` ⇒ 不可重试的 `FAILED`。

    ## 这是 ISS-178 真正要的那条路径

    上一条测的是**装配故障**（`content_id` 为空）。本用例测的是**接线完成、
    但网关未注入**的正常形态 —— 也就是"有人在本地起了 Worker，
    但没配 `adapter`"。这条路上：

    - 执行会一直走到**第 7 步**（提示词用 `stub_prompt` 桩掉、基线无关）；
    - 第 7 步第一个动作是预估，然后**渲染**，然后 `generator.generate(...)`
      ⇒ `UnavailableGenerator` 抛 `GatewayUnavailable`；
    - 处理器把它折成 `FAILED` + `error_code=GENERATOR_NOT_CONFIGURED`，
      **`retryable=False`**。

    ## 为什么 `retryable=False` 在这里是硬要求（而不是"顺手")

    `GatewayUnavailable` 的 `state` 为 `None`（`UnavailableGenerator` 构造时
    没传），因此 `_outcome_for_gateway_unavailable` 走的是**非在途态**分支。
    若有人把这个分支也改成"等核对"（`WAITING_RECONCILIATION`），
    本用例会红 —— 而那个改动的后果是：**每一次"忘了配 adapter"** 都会
    变出一个需要人工核对的挂起任务，把部署配置问题伪装成"外部调用状态未知"。

    ## 为什么它会走到第 7 步（而不是在第 5 步被守卫拦下）

    本用例**不**铺基线（`_start_content` 之后直接跑），所以
    `active_baseline_id` 为 `None` ⇒ 第 5 步的守卫不介入、`total_claim_count`
    保持 0 ⇒ `validate_bindings` 那条"绑满"规则不启用。但**走不到校验**：
    生成器在第 7 步的派发点就抛了。这也是"未接线时 fail-closed"的直接证据 ——
    **一次出网都没有**。
    """
    content_id = _start_content(env)
    job_id = _enqueue_generate_job(env, content_id=content_id)
    claimed = _claim_job(env, job_id)

    with session_scope(env.settings) as session:
        outcome = c_handlers.content_generate_handler(
            claimed, JobRepository(session)
        )

    assert outcome.state == STATE_FAILED, (
        f"未接线时状态是 {outcome.state!r} —— 必须折成 FAILED（不是 WAITING_*）。"
    )
    assert outcome.error_code == "GENERATOR_NOT_CONFIGURED", (
        f"期望 'GENERATOR_NOT_CONFIGURED'，实际 {outcome.error_code!r}。"
    )
    assert outcome.retryable is False, (
        "未接线是**部署配置**问题：重试一万次也不会有 adapter —— "
        "标成可重试会让任务被反复领取、每次都在同一个地方失败。"
    )
    # 内容落 FAILED 是"这一条生成没成功"的可见痕迹（service 失败路径的职责）。
    with session_scope(env.settings) as session:
        item = ContentRepository(session).get_content_item_for_update(content_id)
        assert item is not None
        assert item.state == "FAILED", (
            f"内容状态是 {item.state!r} —— 生成失败必须落 FAILED，"
            "否则列表页会把它显示成'正在生成'，永远不会收敛。"
        )


def test_configured_adapter_builds_gateway_generator(env: ExecEnv) -> None:
    """`adapter` 非 `None` ⇒ 造的是 `GatewayGenerator`，且 job/attempt 透传。

    ## 这条与上一条是一对（两种世界必须可区分）

    `_build_generator` 是一个二分支：`None` → `UnavailableGenerator`（拒绝），
    否则 → `GatewayGenerator`（真接线）。只测 `None` 那一侧的话，这个函数
    被写成 **`return UnavailableGenerator()` 恒返回**也能过 ——
    而那个 bug 的症状是"生产接好线了却永远不出网"，且**不报错**。

    这里断言接线侧的两件事实：

    - 类型是 `GatewayGenerator`（不是恒返回未接线那个）；
    - `job_id` / `attempt_id` 来自本次凭据 ——
      `gateway_bridge` 靠它们写 `provider_calls` 留痕，
      传错（例如恒传 `None`）会让出网调用**没有留痕**，
      而那正是 ADR-10 明令禁止的盲区。

    不真的调 `generate()`：那需要 adapter 与出网策略（OP-130 未关闭），
    且本用例要判的是**装配形状**而不是通道行为。
    """
    claimed = _synthetic_claimed(env, job_id=uuid.uuid4())
    generator = _build_gateway_generator(env, claimed)

    assert isinstance(generator, GatewayGenerator), (
        "给定 adapter 却造出了未接线的生成器 —— "
        "生产接好线之后仍不会出网，且不报错。"
    )
    assert generator.job_id == claimed.job_id
    assert generator.attempt_id == claimed.attempt_id


__all__ = [
    "ExecEnv",
    "PromptStepProbe",
    "ScriptedGenerator",
    "Step7Reached",
    "env",
    "step7_sentinel",
    "stub_billing",
    "stub_prompt",
]