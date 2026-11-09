"""WP26 五张表的存储访问。

**本层职责边界**：只做存储访问，不含业务规则判定。
"这次生成要不要预占""采纳的候选属不属于本内容"的判定在 `service.py`，
但**判定所需的数据必须由本层在同一事务内读出** —— 与 WP11 的
`VersionRepository` / WP18 的 `BriefRepository` / WP20 的 `KnowledgeRepository`
/ WP23 的 `PromptRepository` 同一条理由：调用方先读一次、再传参进来，
两次读之间就有窗口，并发时能构造出"校验通过但写入时前提已变"。

**五条本层特有的约定**：

1. **四张不可变表（`content_versions` / `narrative_blueprints` /
   `title_candidates` / `generation_contexts`）不提供任何 update / delete 方法**。
   与 WP15 审计表、WP17 片段表、WP18 `brief_versions`、WP20 `knowledge_versions`
   / WP23 `prompt_versions` 同一口径：触发器 + ORM 守卫（`guard_immutable`）
   是第二道，**本层不给入口是第一道**。

2. **投影改写走条件 UPDATE**（`WHERE id = :id AND lock_version = :expected`
   + `synchronize_session=False`）。`content_items.current_version_id` /
   `current_version_no` / `title` / `state` 是**投影**（models.py docstring 五），
   只由本层在"追加版本"与"标记失败"两处改写。把"检查乐观锁版本"与"写入"
   压在同一条语句里，并发重做时后者拿到 `rowcount = 0` 而不是静默覆盖。
   改写后**必须** `reload_content_item()`（`populate_existing=True`）——
   条件 UPDATE 不经过 ORM 属性赋值，identity map 里那个对象仍是旧值，
   不强制刷新的话响应会回带**旧的** `currentVersionId`。

3. **业务版本号 `version_no` 用 `max + 1` 在同一事务内取**，
   靠 `uq_content_versions_item_version_no` 兜住并发。**不能"先查再插"** ——
   两个并发重做都会读到同一个 max，然后一个撞唯一约束，而
   一次唯一约束冲突会把**整个事务**标记为 aborted（WP10/WP11 已踩）。
   因此调用方必须用 `savepoint()` 包住"取号 + 插入"这一段，
   撞了就回滚这一段、重新取号再试（与 WP11 的 `register_version` 同一手法）。

4. **`now()` 是数据库时间、且在同一事务内是常量**。
   本层的排序**一律不用 `created_at`**：同一事务插入的多行时间戳完全相同
   （WP17 ISS-52）。版本历史按 `version_no` 排，生成上下文按 `created_at` 排
   只在"跨事务的历史"这一用途上成立（那时每行来自不同事务）。

5. **幂等域有两处，语义不同**：
   · `content_items.last_idempotency_key` —— 一次**批量生成请求**的标识，
     让"同一请求重放"能找回已经物化出来的那批内容条目；
   · `uq_generation_contexts_idempotency`（`content_id` + `idempotency_key`）
     —— 一次**单项生成**的标识，让"同一篇内容的同一次生成"不重复落上下文
     （R5 的存储落点；网关侧的幂等由 `CapabilityRequest.idempotency_key` 保证）。

6. **★ 本层还负责给 `schemas.validate_bindings` 供"上游事实"**。
   它的四个入参里有**两个**不在模型产出里，本层用 `claim_scope()` 一次给出
   （`paragraph_count` 由 `validate_copy` 自己从正文算，不用本层供）。
   详见 `claim_scope` 的 docstring —— 一句话：
   **分母与判据域必须来自同一次读**，否则会在一个中途变过的基线上判合法性。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select, text, update
from sqlalchemy.orm import Session

from rednote.modules.content.models import (
    ContentItem,
    ContentVersion,
    GenerationContext,
    NarrativeBlueprint,
    TitleCandidate,
)
from rednote.modules.content.types import CONTENT_SCHEMA_VERSION
from rednote.modules.projects.models import Project


class ContentRepository:
    """WP26 五张表的读写入口。

    事务边界由调用方（service / 端点）控制。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        """本仓库持有的 session（供下游在同一事务内造别的仓库）。

        与 `BriefRepository.session` / `PromptRepository.session` 同一条理由：
        `GET /contents/{id}` 的详情要一次给出内容、版本、蓝图、候选、
        证据绑定与生成上下文 —— 它们分属本模块的五张表与 WP11 的
        `version_nodes`。不复用同一个 session 的话，同一个响应里的字段
        会来自不同事务。
        """
        return self._session

    def flush(self) -> None:
        """把待写变更立刻发给数据库。

        让约束错误在**预期位置**抛出（那里能给出可读的 4xx），
        而不是等到 `session_scope` 退出时提交才炸 —— 那时已经离开
        能给出可读错误的代码位置。
        """
        self._session.flush()

    def now(self) -> datetime:
        """数据库时间（**不是** Python 的 `datetime.now()`）。

        `now()` 在 PostgreSQL 里是**事务开始时刻**，同一事务内多次调用
        返回同一个值。用它做"这行是什么时候写的"的判据时，
        Python 侧时间与数据库时间可能因机器时钟漂移而不一致，
        而"哪一行更新"的答案全在数据库侧。
        """
        return self._session.execute(select(func.now())).scalar_one()

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        """SAVEPOINT 包住一段可能撞唯一约束的写入（本模块 docstring 第 3 条）。

        一次唯一约束冲突会把整个事务标记为 aborted，之后任何语句都报
        `current transaction is aborted`。包在 SAVEPOINT 里，
        冲突只回滚这一段，调用方可以重新取号再试。
        """
        with self._session.begin_nested():
            yield

    # ------------------------------------------------------------------
    # projects / users（级联与审计字段的前置校验）
    # ------------------------------------------------------------------

    def project_exists(self, project_id: uuid.UUID) -> bool:
        """项目是否存在。

        **用 `select(1)` 而不是 `session.get(Project)`**：后者会把整个
        Project 行加载进 identity map 并可能触发关系加载；
        这里只判断存在性，不产生 ORM 副作用。
        """
        row = self._session.execute(
            select(text("1")).select_from(Project).where(Project.id == project_id)
        ).first()
        return row is not None

    def user_exists(self, user_id: uuid.UUID) -> bool:
        """操作人是否存在（审计字段的 FK 前置校验）。

        不做的话，一个错误的 `actor_id` 会以 FK 违反的形式冒出来 ——
        那是 500 而不是可读的 4xx。
        """
        from rednote.modules.identity.models import User

        row = self._session.execute(
            select(text("1")).select_from(User).where(User.id == user_id)
        ).first()
        return row is not None

    # ------------------------------------------------------------------
    # ★ 上游事实供给：为 `schemas.validate_bindings` 供前两个入参
    # ------------------------------------------------------------------

    def baseline_project(self, baseline_id: uuid.UUID) -> uuid.UUID | None:
        """基线所属项目。基线不存在时返回 `None`。

        `generation_contexts.baseline_id` 是溯源六分之一（OP-160）：
        "这批内容的生成为什么变差了"要按基线归组。而**跨项目引用基线**
        是语义错误（基线承载本品牌的口径，用别的项目的口径生成
        是本包最严重的一类错）——判定在 `service.py`，
        但数据必须在这里、同事务内读出。
        """
        row = self._session.execute(
            text("SELECT project_id FROM production_baselines WHERE id = :bid"),
            {"bid": baseline_id},
        ).first()
        return None if row is None else row[0]

    def baseline_brief_version(self, baseline_id: uuid.UUID) -> uuid.UUID | None:
        """基线快照所依据的 Brief 版本 id。

        ⚠️ **两个 `None` 的语义不同**（本函数无法区分，因为 SQL 上都是"没有行"）：
        ① 基线**不存在**；② 基线存在但 `brief_version_id IS NULL`
        （合法的"没有 Brief 的基线"）。

        因此**必须**配合 `baseline_exists()` 一起用，让调用方把两件事分开判。
        合成一个"返回 None 就是不能用"会把②误判成①，
        而两者的用户可见后果不同（一个是"基线还没批准"，
        一个是"这个项目本就没走 Brief 流程"）。
        """
        row = self._session.execute(
            text(
                "SELECT brief_version_id FROM production_baselines"
                " WHERE id = :bid"
            ),
            {"bid": baseline_id},
        ).first()
        return None if row is None else row[0]

    def baseline_exists(self, baseline_id: uuid.UUID) -> bool:
        """基线的"存在性"与"有无 Brief"分开判（见 `baseline_brief_version`）。"""
        row = self._session.execute(
            text("SELECT 1 FROM production_baselines WHERE id = :bid"),
            {"bid": baseline_id},
        ).first()
        return row is not None

    def claim_scope(
        self, brief_version_id: uuid.UUID | None
    ) -> tuple[frozenset[uuid.UUID], int]:
        """★ 一次查询同时给出 `(known_claim_ids, total_claim_count)`。

        这两个值就是 `schemas.validate_bindings` 的前两个入参
        （见它的 docstring "四个入参为什么都是必需的"）：

        | 返回值 | 在 `validate_bindings` 里的角色 |
        |---|---|
        | `known_claim_ids` | "绑定的 claim 必须存在"的**判据域**；同时是跨项目越界的拦截点 |
        | `total_claim_count` | "绑满全部 claim = 没绑"规则的**分母** |

        ## ★ 为什么必须**一次**查询给出两个值

        `total_claim_count` 是那条"绑满即视为没绑"规则的**分母**，
        而 `known_claim_ids` 是**分子**的取值域。若分两次读，
        两者就来自**两次快照** —— `validate_bindings` 会在一个
        **中途变过的**基线上判合法性。

        `brief_claims` 不可变，所以实践中不会变；但"实践中不会"
        不是判据，**同一次读**才是。用 `count(*) OVER ()` 窗口函数：
        每一行都带同一个全表计数，两者必然同源。

        ## 为什么分母不用 `len(rows)`

        `len(rows)` 在有人误加了 `LIMIT` 之后会**静默变小**，
        而分母变小意味着"绑满"这条规则更容易被触发成**放行**
        （判据被削弱，且没有任何报错）。`count(*) OVER ()` 由数据库算，
        不受 Python 侧列表长度影响。

        ## `brief_version_id is None` 与"基线有 Brief 但一条 claim 都没有"

        两者都返回 `(frozenset(), 0)` —— 这是**如实报数**，不是错误。

        ⚠️ **仓储层不区分这两个 0**（SQL 上都是"没有行"），区分的职责在
        **服务层**：只有那里知道这条基线到底有没有关联 Brief。`service.py`
        第 5 步据此做两件事 —— 前者是"这个项目本就没走 Brief 流程"
        （合法，放行）；后者才拒（`REASON_BASELINE_NOT_READY`）。

        ## ★ 本段曾经是 ISS-177 的本体（**如实留作审计轨迹**）

        它原先写"服务层据此拒绝（`REASON_BASELINE_NOT_READY`）"，
        而服务层当时**一次都没抛过**这个 reason。一句写在文档里的声称
        让"该处有防线"看起来成立，从而没人去真的核对 —— 这是 ISS-95 的
        第三次现身。

        对这类声称的取证必须走 **AST** 而不是文本搜索：这句 docstring
        自己会让"`REASON_BASELINE_NOT_READY` 是否出现在 `service.py`"的
        文本判据**恒绿**。见 `tests/test_content_wp26_exec.py` 的
        `test_guard_reason_is_reachable_from_service` —— 它剥离所有
        docstring 后按 `ast.Call` 的 `reason=` 关键字实参计数。

        ## 后者为什么必须拒

        分母为 0 时"绑定"这件事无意义：放行会让 `validate_bindings`
        拿到**空判据域 + 0 分母**，"绑满即视为没绑"那条规则因此
        **不启用**（`_ALL_CLAIMS_RULE_MIN_TOTAL` 无从比较），
        模型绑 0 条即可通过 —— 等于没有 R4，且全程不报错。

        ## 跨模块只读查询的说明

        `brief_claims` 属于 WP18，本模块**不 import 它的 ORM 类**
        （分层单向：`content` 不依赖 `brief`）。这里用列名字符串直查 ——
        列名是对方模块的**契约**，写错会当场报 `UndefinedColumn`
        （**不会静默**），因此可接受。这与范本
        `baseline/repository.py` 用 `text()` 读 `projects` 同一取向。
        """
        if brief_version_id is None:
            return frozenset(), 0
        rows = self._session.execute(
            text(
                "SELECT id, count(*) OVER () AS total"
                " FROM brief_claims WHERE brief_version_id = :vid"
            ),
            {"vid": brief_version_id},
        ).all()
        if not rows:
            return frozenset(), 0
        total = int(rows[0][1])
        return frozenset(row[0] for row in rows), total

    def candidate_owner(self, candidate_id: uuid.UUID) -> uuid.UUID | None:
        """取标题候选**所属的内容 id**（跨两张表）。

        用于采纳端点最要紧的一条跨表不变量（`models.py` docstring 六末段）：

        > 被采纳的候选，其所属版本必须与**本行**属于**同一条内容**。

        `content_versions.adopted_title_candidate_id` 那条 FK **管不住**它 ——
        FK 只保证"这个候选存在"，不保证"它属于我这条内容"。
        跨内容互相采纳是**语义错误**（采纳了别人家的标题，
        而 AQ-01 要按内容算事实正确率），且它**不会有任何报错**。

        返回 `None` = 候选不存在（与"候选属于别的内容"是两件事，
        调用方要分别给出不同的 4xx：前者 `TITLE_CANDIDATE_NOT_FOUND`，
        后者 `TITLE_CANDIDATE_MISMATCH`）。
        """
        row = self._session.execute(
            text(
                "SELECT cv.content_item_id"
                " FROM title_candidates tc"
                " JOIN content_versions cv ON cv.id = tc.content_version_id"
                " WHERE tc.id = :cid"
            ),
            {"cid": candidate_id},
        ).first()
        return None if row is None else row[0]

    def candidate_title(self, candidate_id: uuid.UUID) -> str | None:
        """取候选的标题文本。

        **采纳要复用它的标题**（`models.py` docstring 六：采纳产生的版本行
        `title` 换成被采纳候选的标题，其余字段原样复用上一版）。
        与 `candidate_owner` 分开两个方法而不是一次返回
        `(owner, title)`：两者的**失败语义**不同（owner 为 None 是"候选不存在"，
        title 为 None 是"候选的标题是 NULL" —— 而后者被 CHECK
        `title_not_empty` 挡住，不可能发生）。合成一个元组会让人
        对 `(None, None)` 做统一处置，那会把一个不可能的状态和一个
        真实的错误混在一起。
        """
        row = self._session.execute(
            text("SELECT title FROM title_candidates WHERE id = :cid"),
            {"cid": candidate_id},
        ).first()
        return None if row is None else row[0]

    # ------------------------------------------------------------------
    # content_items（身份 + 投影，**可变**）
    # ------------------------------------------------------------------

    def get_content_item(self, content_id: uuid.UUID) -> ContentItem | None:
        return self._session.get(ContentItem, content_id)

    def get_content_item_for_update(self, content_id: uuid.UUID) -> ContentItem | None:
        """取内容条目并**加行锁**（`SELECT ... FOR UPDATE`）。

        采纳与重做是"读当前版本 → 校验 → 追加新版本 → 改写投影"的序列。
        加锁让同一内容的并发采纳/重做在**取号之前**就串行化，
        避免无谓的唯一约束冲突与重试。
        """
        return self._session.execute(
            select(ContentItem)
            .where(ContentItem.id == content_id)
            .with_for_update()
        ).scalar_one_or_none()

    def list_content_items(
        self, *, project_id: uuid.UUID, limit: int, offset: int
    ) -> list[ContentItem]:
        """按项目取内容列表。

        **按 `updated_at` 倒序、`id` 兜底**：`updated_at` 是一个可比较的
        投影（最近动过的排前面），而 `id` 兜底让同一时间戳下的顺序稳定 ——
        没有它，翻页时同一行可能出现两次或漏掉（不稳定的排序）。
        """
        stmt: Select[tuple[ContentItem]] = (
            select(ContentItem)
            .where(ContentItem.project_id == project_id)
            .order_by(ContentItem.updated_at.desc(), ContentItem.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.execute(stmt).scalars().all())

    def count_content_items(self, *, project_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(ContentItem)
            .where(ContentItem.project_id == project_id)
        )
        return int(self._session.execute(stmt).scalar_one())

    def list_content_items_by_idempotency(
        self, *, project_id: uuid.UUID, idempotency_key: str
    ) -> list[ContentItem]:
        """按"批量生成请求的幂等键"找回已经物化出来的那批内容条目。

        用于 `POST .../content-generations` 的重放：同一个请求键第二次到来时
        不再新建条目，而是复用第一批（R5 的存储侧一半）。
        """
        stmt = (
            select(ContentItem)
            .where(
                ContentItem.project_id == project_id,
                ContentItem.last_idempotency_key == idempotency_key,
            )
            .order_by(ContentItem.created_at, ContentItem.id)
        )
        return list(self._session.execute(stmt).scalars().all())

    def insert_content_item(
        self,
        *,
        project_id: uuid.UUID,
        created_by: uuid.UUID | None,
        idempotency_key: str | None,
        generation_request: dict[str, Any] | None = None,
        scene_key: str | None = None,
        form_key: str | None = None,
    ) -> ContentItem:
        """插入一条内容**身份**行（尚无版本，投影为空）。

        `state` 与 `current_version_no` 走数据库默认值（`DRAFT` / 0）——
        不在这里写死，是为了让"状态机"的取值只有一个来源（`types.py`）。

        `generation_request` 一并写入而不是随后再 UPDATE：
        发起那次事务里"建身份"与"记下要什么"是**同一件事的两半**，
        分开写会造出"身份在、参数还没写"的中间态 —— 而那个中间态
        恰好是处理器读到的样子（读到 `None` ⇒ 判装配故障）。
        同一次 INSERT 让这个中间态**不可能被观察到**。
        """
        row = ContentItem(
            project_id=project_id,
            created_by=created_by,
            last_idempotency_key=idempotency_key,
            generation_request=generation_request,
            scene_key=scene_key,
            form_key=form_key,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def set_generation_request(
        self, *, content_id: uuid.UUID, request: dict[str, Any]
    ) -> int:
        """写入/覆盖"这一轮请求了什么"。返回受影响行数。

        **为什么要覆盖而不是拒绝重复写**：它是**最后一次意图**的记录，
        不是历史事实 —— 历史事实在 `generation_contexts`（每次生成一行、
        不可变）。重做（"仅标题"→"整篇"）本来就要换参数，
        拒绝覆盖会让重做走不通，而"走不通"的报错会指向一个无关的方向。

        **与 `advance_projection` 一样不进 `lock_version` 前进**：
        它不参与任何乐观锁判据（没有"基于参数版本做决策"的地方），
        推进 `lock_version` 只会让并发重做多撞一次无谓的版本冲突。
        """
        result = self._session.execute(
            update(ContentItem)
            .where(ContentItem.id == content_id)
            .values(generation_request=request),
            execution_options={"synchronize_session": False},
        )
        return int(result.rowcount or 0)

    def advance_projection(
        self,
        *,
        content_id: uuid.UUID,
        expected_lock_version: int,
        version_id: uuid.UUID,
        version_no: int,
        title: str,
        state: str,
    ) -> int:
        """条件 UPDATE 把内容条目的投影推进到新版本，返回受影响行数。

        **为什么用条件 UPDATE 而不是读-改-写**：`WHERE id = :id AND
        lock_version = :expected` 把"检查乐观锁"与"写入"压在同一条语句里。
        两个并发重做请求只有一个能改成功（另一个 `rowcount = 0`），
        而失败的那个**不会静默覆盖**已经前进的投影。

        **必须**随后 `reload_content_item()` —— 见本模块 docstring 第 2 条。
        """
        result = self._session.execute(
            update(ContentItem)
            .where(
                ContentItem.id == content_id,
                ContentItem.lock_version == expected_lock_version,
            )
            .values(
                current_version_id=version_id,
                current_version_no=version_no,
                title=title,
                state=state,
                lock_version=ContentItem.lock_version + 1,
            ),
            execution_options={"synchronize_session": False},
        )
        return int(result.rowcount or 0)

    def set_state(self, *, content_id: uuid.UUID, state: str) -> int:
        """把内容状态改掉（用于生成失败落 `FAILED`），返回受影响行数。

        与 `advance_projection` 分开而不合并：失败路径**不动投影**
        （没有新版本），把两件事压进一个方法会让"失败时不小心把
        `current_version_id` 清空"变成一次无声的事故。
        """
        result = self._session.execute(
            update(ContentItem)
            .where(ContentItem.id == content_id)
            .values(state=state, lock_version=ContentItem.lock_version + 1),
            execution_options={"synchronize_session": False},
        )
        return int(result.rowcount or 0)

    def reload_content_item(self, content_id: uuid.UUID) -> ContentItem | None:
        """强制从库重新读（`populate_existing=True`）。

        **为什么不用 `session.expire_all()`**：它会把调用方正在编辑的
        其它对象一起失效（WP17 ISS-53 的教训）。只刷这一个对象。
        """
        return self._session.get(
            ContentItem, content_id, populate_existing=True
        )

    # ------------------------------------------------------------------
    # content_versions（**不可变**）
    # ------------------------------------------------------------------

    def get_version(self, version_id: uuid.UUID) -> ContentVersion | None:
        return self._session.get(ContentVersion, version_id)

    def latest_version(self, *, content_item_id: uuid.UUID) -> ContentVersion | None:
        """本内容业务版本号最大的那一版。

        **按 `version_no` 排而不是 `created_at`**：见本模块 docstring 第 4 条 ——
        同一事务里插入的多行 `created_at` 完全相同，按它排得不到唯一答案。
        """
        stmt = (
            select(ContentVersion)
            .where(ContentVersion.content_item_id == content_item_id)
            .order_by(ContentVersion.version_no.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def list_versions(
        self, *, content_item_id: uuid.UUID, limit: int, offset: int
    ) -> list[ContentVersion]:
        """版本历史（`GET /contents/{id}/versions`），新版本在前。"""
        stmt = (
            select(ContentVersion)
            .where(ContentVersion.content_item_id == content_item_id)
            .order_by(ContentVersion.version_no.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.execute(stmt).scalars().all())

    def count_versions(self, *, content_item_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(ContentVersion)
            .where(ContentVersion.content_item_id == content_item_id)
        )
        return int(self._session.execute(stmt).scalar_one())

    def next_version_no(self, *, content_item_id: uuid.UUID) -> int:
        """下一个业务版本号（`max + 1`）。

        **同一事务内取**，靠 `uq_content_versions_item_version_no` 兜住并发
        （本模块 docstring 第 3 条）。调用方必须用 `savepoint()` 包住
        "取号 + 插入"这一段。
        """
        stmt = select(
            func.coalesce(func.max(ContentVersion.version_no), 0)
        ).where(ContentVersion.content_item_id == content_item_id)
        return int(self._session.execute(stmt).scalar_one()) + 1

    def insert_version(
        self,
        *,
        version_id: uuid.UUID,
        content_item_id: uuid.UUID,
        version_no: int,
        title: str,
        body: str,
        tags: Sequence[str],
        bindings: Sequence[dict[str, Any]],
        payload: dict[str, Any],
        payload_hash: str,
        blueprint_id: uuid.UUID | None = None,
        adopted_title_candidate_id: uuid.UUID | None = None,
        generation_context_id: uuid.UUID | None = None,
        schema_version: int = CONTENT_SCHEMA_VERSION,
        created_by: uuid.UUID | None = None,
    ) -> ContentVersion:
        """插入一个内容版本行。

        **`version_id` 由调用方给**（不是数据库生成）：它必须等于 WP11
        `register_version` 返回的 `node_id` —— 两行同事务插入且 `id` 相同，
        这是"注册表与具体表"的配对方式（数据结构 02 §5，OP-88 正向一半）。

        `adopted_title_candidate_id` 的写入规则见 `models.py` docstring 六：
        **只有采纳端点产生的版本行**会写它，生成产生的版本行恒为 `None`。
        """
        row = ContentVersion(
            id=version_id,
            content_item_id=content_item_id,
            version_no=version_no,
            blueprint_id=blueprint_id,
            adopted_title_candidate_id=adopted_title_candidate_id,
            title=title,
            body=body,
            tags=list(tags),
            bindings=list(bindings),
            generation_context_id=generation_context_id,
            payload=payload,
            payload_hash=payload_hash,
            schema_version=schema_version,
            created_by=created_by,
        )
        self._session.add(row)
        self._session.flush()
        return row

    # ------------------------------------------------------------------
    # narrative_blueprints（**不可变**）
    # ------------------------------------------------------------------

    def get_blueprint(self, blueprint_id: uuid.UUID) -> NarrativeBlueprint | None:
        return self._session.get(NarrativeBlueprint, blueprint_id)

    def latest_blueprint(
        self, *, content_item_id: uuid.UUID
    ) -> NarrativeBlueprint | None:
        """本内容最近的一条蓝图（最近生成的那一版所依据的蓝图）。"""
        stmt = (
            select(NarrativeBlueprint)
            .where(NarrativeBlueprint.content_item_id == content_item_id)
            .order_by(NarrativeBlueprint.created_at.desc(), NarrativeBlueprint.id.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def insert_blueprint(
        self,
        *,
        content_item_id: uuid.UUID,
        fields: dict[str, str],
        created_by: uuid.UUID | None = None,
        schema_version: int = CONTENT_SCHEMA_VERSION,
    ) -> NarrativeBlueprint:
        """插入一条叙事蓝图。

        `fields` 是**已校验**的 9 字段字典（`schemas.validate_blueprint` 的产物），
        由本方法逐字段展开成列 —— 不做 `**fields` 展开：手抄列名会让
        "蓝图新增一个字段但忘了在这里展开"变成一次**静默丢字段**
        （models.py 的模块级断言会在导入期拦下类型侧的漂移，
        但拦不下"本方法漏了某个键"）。
        """
        row = NarrativeBlueprint(
            content_item_id=content_item_id,
            opening_function=fields["opening_function"],
            context=fields["context"],
            decision_process=fields["decision_process"],
            usage_scene=fields["usage_scene"],
            evidence_beats=fields["evidence_beats"],
            limitation=fields["limitation"],
            conclusion_strength=fields["conclusion_strength"],
            interaction_question=fields["interaction_question"],
            paragraph_plan=fields["paragraph_plan"],
            schema_version=schema_version,
            created_by=created_by,
        )
        self._session.add(row)
        self._session.flush()
        return row

    # ------------------------------------------------------------------
    # title_candidates（**不可变**）
    # ------------------------------------------------------------------

    def get_title_candidate(self, candidate_id: uuid.UUID) -> TitleCandidate | None:
        return self._session.get(TitleCandidate, candidate_id)

    def list_title_candidates(
        self, *, content_version_id: uuid.UUID
    ) -> list[TitleCandidate]:
        """某一版产出的全部候选，**按输入顺序**（`candidate_index` 升序）。

        模型给的顺序是优先级信号（第一个标题权重最高），因此排序**不能**
        用 `created_at`（同一事务插入的多行时间戳相同，见 docstring 第 4 条）。
        """
        stmt = (
            select(TitleCandidate)
            .where(TitleCandidate.content_version_id == content_version_id)
            .order_by(TitleCandidate.candidate_index)
        )
        return list(self._session.execute(stmt).scalars().all())

    def insert_title_candidates(
        self,
        *,
        content_version_id: uuid.UUID,
        candidates: Sequence[dict[str, Any]],
    ) -> list[TitleCandidate]:
        """批量插入标题候选，`candidate_index` 按传入顺序 0 基编号。

        `candidates` 的每一项是**已校验**的字典
        （`schemas.validate_title_candidates` 的产物），键为
        `title` / `scores` / `keywords` / `highlight` / `risk_note`。
        """
        rows: list[TitleCandidate] = []
        for index, item in enumerate(candidates):
            row = TitleCandidate(
                content_version_id=content_version_id,
                candidate_index=index,
                title=item["title"],
                scores=dict(item["scores"]),
                keywords=list(item.get("keywords") or ()),
                highlight=item.get("highlight") or None,
                risk_note=item.get("risk_note") or None,
            )
            self._session.add(row)
            rows.append(row)
        self._session.flush()
        return rows

    # ------------------------------------------------------------------
    # generation_contexts（**不可变**；OP-160 落点）
    # ------------------------------------------------------------------

    def get_generation_context(
        self, context_id: uuid.UUID
    ) -> GenerationContext | None:
        return self._session.get(GenerationContext, context_id)

    def list_generation_contexts(
        self, *, content_id: uuid.UUID
    ) -> list[GenerationContext]:
        """一条内容的全部生成上下文（详情端点用）。

        按 `created_at` 升序：这里读的是**跨事务**的历史（每次生成一个事务），
        因此时间戳各不相同 —— 与"同一事务内插入多行"是两种情形。
        """
        stmt = (
            select(GenerationContext)
            .where(GenerationContext.content_id == content_id)
            .order_by(GenerationContext.created_at, GenerationContext.id)
        )
        return list(self._session.execute(stmt).scalars().all())

    def find_generation_context(
        self, *, content_id: uuid.UUID, idempotency_key: str
    ) -> GenerationContext | None:
        """按幂等键找一次生成（R5 的存储侧一半）。

        `uq_generation_contexts_idempotency` 保证最多一条。
        """
        stmt = (
            select(GenerationContext)
            .where(
                GenerationContext.content_id == content_id,
                GenerationContext.idempotency_key == idempotency_key,
            )
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def list_contexts_by_prompt_version(
        self, *, prompt_version_id: uuid.UUID, limit: int, offset: int
    ) -> list[GenerationContext]:
        """**OP-160 的核心查询**：改了这个提示词版本，哪些历史生成受影响。

        这条查询能成立的前提是 `ix_generation_contexts_prompt_version`
        （models.py docstring 四）—— 没有它就退化成全表扫，
        而本表是"每次生成一行、只增不减"的。
        """
        stmt = (
            select(GenerationContext)
            .where(GenerationContext.prompt_version_id == prompt_version_id)
            .order_by(GenerationContext.created_at.desc(), GenerationContext.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.execute(stmt).scalars().all())

    def insert_generation_context(
        self,
        *,
        project_id: uuid.UUID,
        content_id: uuid.UUID,
        content_version_id: uuid.UUID | None,
        task_kind: str,
        idempotency_key: str,
        context_id: uuid.UUID | None = None,
        prompt_template_id: uuid.UUID | None = None,
        prompt_version_id: uuid.UUID | None = None,
        baseline_id: uuid.UUID | None = None,
        model_profile_id: uuid.UUID | None = None,
        provider_call_id: uuid.UUID | None = None,
        created_by: uuid.UUID | None = None,
    ) -> GenerationContext:
        """插入一条生成上下文。

        `content_version_id` 可空：`CONTENT_STRUCTURE` 那次调用发生时
        内容版本还没有（它产出的是蓝图），处置见 `models.py` 的
        `GenerationContext` docstring。

        ## `context_id` 为什么可显式给（不是让数据库生成就完了）

        `content_versions.generation_context_id` 与
        `generation_contexts.content_version_id` 构成**环 2**，两侧 FK 都是
        `DEFERRABLE INITIALLY DEFERRED`（`models.py` docstring 三）。
        这个环的**唯一**可行写入顺序是"先插一侧、后插另一侧"，而
        `content_versions` 是**不可变表**（`ddl.py` 挂
        `rednote_reject_mutation()`）⇒ 版本行那一侧的
        `generation_context_id` **只能在 INSERT 时给出**，事后 UPDATE 必撞
        `23001`。因此调用方必须**先知道**上下文行将用的 id，再插版本行。

        不给（`None`）时仍走列的 `server_default=gen_random_uuid()` ——
        结构那一路（`CONTENT_STRUCTURE`）没有版本行，不需要提前知道 id。
        显式给时**只在赋值处覆盖主键**，不改列的生成方式：本仓的
        主键一律服务端生成（`platform/base.py:uuid_pk`），
        在 Python 侧再生成一次就是"两个 id 来源"。
        """
        row = GenerationContext(
            project_id=project_id,
            content_id=content_id,
            content_version_id=content_version_id,
            task_kind=task_kind,
            idempotency_key=idempotency_key,
            prompt_template_id=prompt_template_id,
            prompt_version_id=prompt_version_id,
            baseline_id=baseline_id,
            model_profile_id=model_profile_id,
            provider_call_id=provider_call_id,
            created_by=created_by,
        )
        if context_id is not None:
            row.id = context_id
        self._session.add(row)
        self._session.flush()
        return row


__all__ = ["ContentRepository"]
