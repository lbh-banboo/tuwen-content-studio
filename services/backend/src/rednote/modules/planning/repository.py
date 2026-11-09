"""WP24 两张表的存储访问。

**本层职责边界**：只做存储访问，不含业务规则判定。
"这个值合法吗"、"该不该级联刷新"的判定在 `settings_service.py`，
但**判定所需的数据必须由本层在同一事务内读出** ——
与 WP11 的 `VersionRepository` / WP20 的 `KnowledgeRepository` /
WP23 的 `PromptRepository` 同一条理由：若调用方先读一次、再传参进来，
两次读之间就有窗口，并发时能构造出"校验通过但写入时前提已变"。

---

## 本层特有的四条约定

### 1. `revision_no` 在同一事务内 `max + 1` 取，靠**部分唯一索引**兜并发

**不能"先查再插"当作正确性依据** —— 两个并发请求都会读到同一个 max，
然后一个撞唯一约束。本层负责让冲突以 `IntegrityError` 冒出来，
由 `settings_service` 捕获后**重新取号再试**（与 WP11 的 `register_version`
同一手法：SAVEPOINT 包住、失败后把 pending 置空重取）。

**为什么本包不能像 WP11 那样只用 SAVEPOINT**：`runtime_settings` 的
`revision_no` 唯一性是**部分索引**（`WHERE scope_kind = …`），
所以在 `repo.savepoint()` 里 flush 才能让冲突在可捕获的位置抛出。
本层照搬 WP11 的 `savepoint()` 上下文管理器形态。

### 2. 全局变更的级联刷新要**枚举全部项目** —— 用 `projects` 表而不是 `project_members`

"哪些项目需要跟随"的答案是**所有项目**，不是"有成员的项目"或
"活跃项目"。用 `project_members` 会漏掉刚创建还没加成员的项目，
而那个项目第一次读配置时会惰性物化 —— 值恰好是对的，
于是这个漏项**在任何测试里都不会暴露**，直到"创建后立刻改全局值"这种顺序出现。

**它同时必须包含"尚无 PROJECT 行"的项目** —— 那正是幂等插入要覆盖的集合。

### 3. 惰性物化：**不靠"我插入了"推断"我拿到了那行"，必须插入后重读**

`materialize_inherited_copy()` 返回的行**永远是重读出来的**，不是本次 INSERT
构造的那个对象。理由有两个，都是本项目已踩过的同族坑：

· **并发**：两个实例同时首次读取同一 `(key, project)` 时，只有一个插得进去；
  另一个即便"也执行了插入"，拿到的也不是生效行。
· **生效值属于哪一行**：即便插入成功，`revision_no` 也是**读**出来的最大者，
  而不是"我算的那个"。用 state 推断首次 = WP17/WP23 反复出现的形态。

### 4. 并发正确性靠**事务级 advisory lock 按 key 串行化**，不靠 `ON CONFLICT DO NOTHING`

这是本层最要紧的一处设计，先说清被否决的方案：

**曾考虑的写法**：副本插入用 `INSERT … ON CONFLICT DO NOTHING`，
"两个并发请求都发现没有副本 → 一个成功一个静默跳过"。

**为什么否决**：静默跳过在**同一个值**的竞争下是对的（幂等），
但在**不同值**的竞争下会**丢写且不报错**。具体序列：

    T1（全局改成 V2）与 T2（全局改成 V3）并发，项目 P 尚无副本行
    · T1 算得 P 的下一修订号 = 1，插入 (rev 1, V2)
    · T2 也算得 1，插入 (rev 1, V3)  ← 被部分唯一索引拒绝
    · T2 的 ON CONFLICT DO NOTHING **静默跳过**
    结果：全局 = V3，项目 P 的生效副本 = **V2**

这正是判据 J2 要拦的形态（"全局变更后所有 `INHERITED` 项目生效值 == 新全局值"），
而它是**静默**的 —— 两个数字都在、都不报错、合计也对得上。

**采用的做法**：`change_global_setting` / `change_project_setting` /
惰性物化 三条路径在开始写之前，都先取**同一把按 key 的事务级排他锁**：

    SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)

于是"同一个 key 的配置写入"在数据库层**串行**。锁在事务提交/回滚时自动释放
（`xact` 后缀的语义），不会泄漏。哈希碰撞只会让两个无关的 key 偶尔互相等待
（无害），不会让本该串行的两个写入并行。

**因此副本插入是一条普通 INSERT**，不需要 `ON CONFLICT` ——
在锁的保护下，冲突不再是"正常的并发"而是"不变量被破坏"，
应当**报错而不是静默跳过**（本项目一贯立场：宁可炸，不要静默）。

**锁的粒度为什么是 key 而不是 key+project**：全局变更要刷新**所有**项目的副本，
逐项目加锁会引入"先锁 A 后锁 B、另一事务先锁 B 后锁 A"的死锁。
按 key 一把锁最简单且无死锁。

### 5. **不可变表不提供任何 update / delete 方法**

`runtime_settings` 不给入口（第一道），触发器 + ORM 守卫是第二道。
本层只有 `insert_setting`，没有 `update_setting` —— 需要"改值"时
插入一个 `revision_no + 1` 的新行。
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from rednote.modules.planning.models import PlanDraft, RuntimeSetting
from rednote.modules.planning.types import (
    SETTING_ORIGIN_INHERITED,
    SETTING_SCOPE_GLOBAL,
    SETTING_SCOPE_PROJECT,
)
from rednote.modules.projects.models import Project


class PlanningRepository:
    """配置与草稿的读写入口。事务边界由调用方控制。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        """本仓库持有的 session（供下游在同一事务内造别的仓库）。

        与 `PromptRepository.session` 同一条理由：`get_effective_limits`
        要同时读 `runtime_settings` 与判断项目是否存在，
        两处若用不同 session，"刚校验存在的项目"与"查配置的项目"会来自
        不同事务（WP17 的 ISS-54 是本仓已有实证）。
        """
        return self._session

    def flush(self) -> None:
        """把待写变更立刻发给数据库。

        让约束错误在**预期位置**抛出（那里能给出可读的 4xx），
        而不是等到 `session_scope` 退出时提交才炸。
        """
        self._session.flush()

    def now(self) -> datetime:
        """数据库时间（不用 Python 的 `datetime.now()`）。

        **为什么**：`now()` 在 PostgreSQL 里是**事务开始时刻**，同一事务内
        多次调用返回同一个值。用它做"这行是什么时候写的"的判据时，
        Python 侧时间与数据库时间可能因机器时钟漂移而不一致，
        而"哪一行更新"的答案全在数据库侧。
        """
        return self._session.execute(select(func.now())).scalar_one()

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        """SAVEPOINT 包住一段可能撞唯一约束的写入。

        **为什么必须有**（WP10/WP11 已踩）：一次唯一约束冲突会把
        **整个事务**标记为 aborted，之后任何语句都报
        `current transaction is aborted`。包在 SAVEPOINT 里，
        冲突只回滚这一段，调用方可以重新取号再试。
        """
        with self._session.begin_nested():
            yield

    # ------------------------------------------------------------------
    # projects（级联刷新要枚举全部项目）
    # ------------------------------------------------------------------

    def list_project_ids(self) -> list[uuid.UUID]:
        """**全部**项目 id（不分是否有成员、不分阶段）。

        见模块 docstring 第 2 条：级联刷新要守住的是"所有项目的生效值
        都等于新全局值"，漏掉任何一个项目都会让 J2 判据失败 ——
        因此查询条件里**刻意不加任何过滤**。
        """
        return list(
            self._session.execute(select(Project.id).order_by(Project.id)).scalars()
        )

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
    # runtime_settings：读取
    # ------------------------------------------------------------------

    def current_global_setting(
        self, *, key: str
    ) -> RuntimeSetting | None:
        """取某个 key 的**当前全局行**（`revision_no` 最大那行）。

        **排序键必须是 `revision_no` 而不是 `created_at`**（R9）：
        `now()` 是事务开始时刻，同一事务内插入的多行时间戳完全相同，
        按它排序得到的是不确定的顺序（WP17 的 ISS-52 实证）。
        """
        return self._session.execute(
            select(RuntimeSetting)
            .where(
                RuntimeSetting.key == key,
                RuntimeSetting.scope_kind == SETTING_SCOPE_GLOBAL,
            )
            .order_by(RuntimeSetting.revision_no.desc())
            .limit(1)
        ).scalar_one_or_none()

    def current_project_setting(
        self, *, key: str, project_id: uuid.UUID
    ) -> RuntimeSetting | None:
        """取某个 key 的**当前项目级行**（副本或覆盖，`revision_no` 最大那行）。"""
        return self._session.execute(
            select(RuntimeSetting)
            .where(
                RuntimeSetting.key == key,
                RuntimeSetting.scope_kind == SETTING_SCOPE_PROJECT,
                RuntimeSetting.project_id == project_id,
            )
            .order_by(RuntimeSetting.revision_no.desc())
            .limit(1)
        ).scalar_one_or_none()

    def max_revision_no(self, *, key: str, project_id: uuid.UUID | None) -> int:
        """某个 `(key, scope, project)` 组合下的最大修订号（无行时 0）。

        `project_id=None` 表示全局作用域 —— 与 `current_global_setting`
        的 where 条件同源，避免两处口径分叉（一处用 `IS NULL`、
        另一处忘了加，会读出跨作用域的最大值）。
        """
        conditions = [RuntimeSetting.key == key]
        if project_id is None:
            conditions.append(RuntimeSetting.scope_kind == SETTING_SCOPE_GLOBAL)
        else:
            conditions.append(RuntimeSetting.scope_kind == SETTING_SCOPE_PROJECT)
            conditions.append(RuntimeSetting.project_id == project_id)
        return int(
            self._session.execute(
                select(func.coalesce(func.max(RuntimeSetting.revision_no), 0)).where(
                    *conditions
                )
            ).scalar_one()
        )

    def list_project_settings(self, *, key: str) -> list[RuntimeSetting]:
        """取某个 key 的**全部**项目级当前行（一项目一行）。

        用于两个地方：
          · `GET /settings/projects/diverged` —— 找出与全局不同的项目；
          · J2 判据 —— 断言**所有**项目的生效值都等于新全局值。

        实现用 `DISTINCT ON` 取每个 `project_id` 的最大 `revision_no`：
        在 SQL 里一次完成，避免"先查全部再在 Python 里去重"时读到
        中间状态的窗口。`DISTINCT ON` 的语义与 `ORDER BY` 强绑定 ——
        先按 `project_id` 分组，再按 `revision_no DESC` 取每组第一行。
        """
        return list(
            self._session.execute(
                select(RuntimeSetting)
                .where(
                    RuntimeSetting.key == key,
                    RuntimeSetting.scope_kind == SETTING_SCOPE_PROJECT,
                )
                .order_by(
                    RuntimeSetting.project_id,
                    RuntimeSetting.revision_no.desc(),
                )
                .distinct(RuntimeSetting.project_id)
            ).scalars()
        )

    def list_history(self, *, key: str) -> list[RuntimeSetting]:
        """按 `revision_no` 升序列出某个 key 的全部行（管理侧"谁改过"）。

        含全局行与项目行 —— 由调用方按需要过滤。
        """
        return list(
            self._session.execute(
                select(RuntimeSetting)
                .where(RuntimeSetting.key == key)
                .order_by(RuntimeSetting.revision_no, RuntimeSetting.created_at)
            ).scalars()
        )

    # ------------------------------------------------------------------
    # runtime_settings：写入（只有 insert，没有 update/delete）
    # ------------------------------------------------------------------

    def lock_setting_key(self, *, key: str) -> None:
        """取按 key 的**事务级排他锁**（见模块 docstring 第 3 条）。

        `pg_advisory_xact_lock` 在事务提交/回滚时**自动释放** ——
        不需要（也不能）手工 unlock，因此不会因异常路径泄漏锁。

        `hashtext(:key)` 把字符串映射成 bigint：advisory lock 的键空间是
        `bigint`，而配置键是文本。哈希碰撞只会让两个无关的 key 偶尔
        互相等待（**无害**，只是慢一点），不会破坏正确性 ——
        因为锁的语义是"同一个键的写入串行"，碰撞只是多串行了一对无关键。

        **必须用 `xact` 版本**：`pg_advisory_lock`（非 xact）会一直持有到
        显式 unlock，而异常路径下会泄漏，把后续所有同 key 请求全堵死。
        """
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)"),
            {"key": key},
        )

    def insert_setting(
        self,
        *,
        key: str,
        scope_kind: str,
        project_id: uuid.UUID | None,
        value: dict[str, Any],
        revision_no: int,
        origin: str,
        actor_id: uuid.UUID | None,
        reason: str | None,
        request_id: str | None,
    ) -> RuntimeSetting:
        """插入一行配置历史。**本层唯一的写入方法**。

        `revision_no` 由调用方（`settings_service`）在重试循环里取号，
        本层不自己取 —— 若本层取号，重试就必须在本层内部循环，
        而"重试几次"是业务策略（`MAX_SETTING_ATTEMPTS`），不属于存储层。
        """
        row = RuntimeSetting(
            key=key,
            scope_kind=scope_kind,
            project_id=project_id,
            value=value,
            revision_no=revision_no,
            origin=origin,
            actor_id=actor_id,
            reason=reason,
            request_id=request_id,
        )
        self._session.add(row)
        return row

    def insert_inherited_copy(
        self,
        *,
        key: str,
        project_id: uuid.UUID,
        value: dict[str, Any],
        revision_no: int,
        actor_id: uuid.UUID | None,
        reason: str | None,
        request_id: str | None,
    ) -> RuntimeSetting:
        """插入一行项目级**副本**（`origin='INHERITED'`）。

        **是一条普通 INSERT，不带 `ON CONFLICT`** —— 见模块 docstring 第 3 条：
        调用方必须先持有 `lock_setting_key()` 的锁，因此"同一项目的同一
        `revision_no` 已存在"不再是并发常态，而是**不变量被破坏**，
        应当让 `IntegrityError` 冒出来（由 `settings_service` 的重试循环
        重新取号，或作为真缺陷暴露）。

        **走 ORM 而不是原生 SQL**（与初稿相反）：既然不需要 `ON CONFLICT`，
        原生 SQL 的唯一收益就消失了，而它的代价仍在（JSON 参数要手工
        序列化、`text()` 会把 JSON 里的 `:1` 当绑定参数 —— 本项目高频坑第 2 条）。
        用 ORM 让 `value` 直接以 Python 对象传入，由方言适配器处理。
        """
        row = RuntimeSetting(
            key=key,
            scope_kind=SETTING_SCOPE_PROJECT,
            project_id=project_id,
            value=value,
            revision_no=revision_no,
            origin=SETTING_ORIGIN_INHERITED,
            actor_id=actor_id,
            reason=reason,
            request_id=request_id,
        )
        self._session.add(row)
        return row

    # ------------------------------------------------------------------
    # plan_drafts
    # ------------------------------------------------------------------

    def get_draft(self, project_id: uuid.UUID) -> PlanDraft | None:
        """取某个项目的草稿（一项目一份）。"""
        return self._session.execute(
            select(PlanDraft).where(PlanDraft.project_id == project_id)
        ).scalar_one_or_none()

    def get_draft_for_update(self, project_id: uuid.UUID) -> PlanDraft | None:
        """取草稿并**加行锁**（`SELECT … FOR UPDATE`）。

        `lock_version` 是乐观锁（客户端提交时带 `If-Match`），
        但**乐观锁不能替代行锁**：两个请求同时读到 `lock_version=3`、
        各自校验通过后写入，第二个会因 `lock_version` 不匹配而失败 ——
        这是**正确**的（它会重试）。行锁在这里的作用是让
        "固化出版本"（读草稿 → 写 `version_nodes` → 改草稿状态）
        这一串操作不交错，否则两次固化会产出两个 PLAN 版本而只有一个草稿状态。
        """
        return self._session.execute(
            select(PlanDraft)
            .where(PlanDraft.project_id == project_id)
            .with_for_update()
        ).scalar_one_or_none()

    def insert_draft(
        self,
        *,
        project_id: uuid.UUID,
        baseline_id: uuid.UUID | None,
        created_by: uuid.UUID | None,
        total_quantity: int | None = None,
        scene_items: list[Any] | None = None,
        form_items: list[Any] | None = None,
        pairings: list[Any] | None = None,
    ) -> PlanDraft:
        """新建草稿（一项目一份，`unique` 保证）。"""
        row = PlanDraft(
            project_id=project_id,
            baseline_id=baseline_id,
            created_by=created_by,
            total_quantity=total_quantity,
            scene_items=scene_items if scene_items is not None else [],
            form_items=form_items if form_items is not None else [],
            pairings=pairings if pairings is not None else [],
        )
        self._session.add(row)
        return row

    def update_draft(
        self,
        *,
        draft_id: uuid.UUID,
        expected_lock_version: int,
        baseline_id: uuid.UUID | None,
        total_quantity: int | None,
        state: str | None,
        scene_items: list[Any] | None,
        form_items: list[Any] | None,
        pairings: list[Any] | None,
    ) -> int:
        """条件 UPDATE 更新草稿，返回受影响行数。

        **为什么用条件 UPDATE 而不是读-改-写**（与 WP18/WP22 同款）：
        `WHERE id = :id AND lock_version = :expected` 把"检查版本"与"写入"
        压在同一条语句里。前置的 `require_if_match` 只保证"客户端拿的不是
        过期值"，它挡不住两个**同时通过**前置校验的请求 —— 那正是"乐观锁"
        这个词的全部含义。返回 0 表示"读到之后有人改过"。

        ⚠️ **`state` / `total_quantity` 一律用 `is not None` 判，不用真值判**：
        `total_quantity=0` 不是合法值（DDL 有 `>= 1`），但更重要的是**写法
        一致性** —— WP22 实测过一处 MAJOR：`brief_version_id` 被无条件写，
        于是"只改 scenes"的保存把运营设好的 `briefVersionId` 静默清成 NULL，
        且返回 HTTP 200。本函数与它同类，七个字段写法必须一致。
        """
        values: dict[str, Any] = {
            "updated_at": func.now(),
            "lock_version": PlanDraft.lock_version + 1,
        }
        if baseline_id is not None:
            values["baseline_id"] = baseline_id
        if total_quantity is not None:
            values["total_quantity"] = total_quantity
        if state is not None:
            values["state"] = state
        if scene_items is not None:
            values["scene_items"] = scene_items
        if form_items is not None:
            values["form_items"] = form_items
        if pairings is not None:
            values["pairings"] = pairings

        result = self._session.execute(
            update(PlanDraft)
            .where(
                PlanDraft.id == draft_id,
                PlanDraft.lock_version == expected_lock_version,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    def reload_draft(self, draft_id: uuid.UUID) -> PlanDraft | None:
        """绕开身份映射重新读一行草稿。

        **为什么必须有**：上面的条件 UPDATE 用了
        `synchronize_session=False` —— 它**不刷新** session 里的 ORM 实例。
        不重读的话，服务层随后算出的任何派生值（比例校验、分配结果）
        用的都是**保存之前**的内容，而响应里回带的正是它们。
        症状是"保存成功但返回的数字是旧的"，且下一次请求就正常 ——
        排障时会以为是前端缓存问题。
        """
        return self._session.execute(
            select(PlanDraft)
            .where(PlanDraft.id == draft_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def baseline_belongs_to_project(
        self, *, baseline_id: uuid.UUID, project_id: uuid.UUID
    ) -> bool:
        """基线是否属于该项目。

        **这条校验存在的理由**：`plan_drafts.baseline_id` 只是一个 FK
        （指向 `production_baselines.id`），FK **不保证**基线属于本项目。
        不校验的话，项目 A 的草稿可以钉住项目 B 的基线 ——
        那是一处**静默的越权读取**：草稿固化出的 PLAN 版本会带着
        别的项目的基线内容，而权限判定只看草稿所属的项目
        （与 WP23 的 `prompt_publications` 范围校验同一形态的缺陷）。
        """
        from rednote.modules.baseline.models import ProductionBaseline

        row = self._session.execute(
            select(text("1"))
            .select_from(ProductionBaseline)
            .where(
                ProductionBaseline.id == baseline_id,
                ProductionBaseline.project_id == project_id,
            )
        ).first()
        return row is not None


__all__ = ["PlanningRepository"]
