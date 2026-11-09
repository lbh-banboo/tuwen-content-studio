"""WP22 四张表的存储访问。

**本层职责边界**：只做存储访问，**不含业务判定**。
"能不能创建基线"（三条完成条件、跨品牌、同事务批准）的判定在 `service.py`，
但**判定所需的数据必须由本层在同一事务内读出** ——
与 WP11 的 `VersionRepository` / WP18 的 `BriefRepository` /
WP20 的 `KnowledgeRepository` / WP23 的 `PromptRepository` 同一条理由：
若调用方先读一次、再传参进来，两次读之间就有窗口，
并发时能构造出"校验通过但写入时前提已变"。

---

## 本层特有的四条约定

### 1. `information_drafts` 是**唯一可变**的表，它的写必须带 `lock_version`

`brief_drafts` / `information_drafts` 这类"工作区"是运营反复改的地方，
它们的并发保护靠 `WHERE lock_version = :expected` 条件 UPDATE（前置校验
`require_if_match` 只挡"客户端拿着旧值"，挡不住"两个请求同时通过前置校验"）。
`update_draft` 返回受影响行数，0 表示"读到之后有人改过" → 服务层报 412。

### 2. 三张不可变表**不提供任何 update / delete 方法**

`production_baselines` / `baseline_items` / `step_approvals`
—— 与 WP15 审计表、WP17 片段表、WP18 `brief_versions`、
WP20 `knowledge_versions`、WP23 `prompt_versions` 同一口径：
"想改也改不了"（触发器 + ORM 守卫）是**第二道**，本层不给入口是**第一道**。
**不可变表的正确用法是"不产生 UPDATE"，而不是"产生 UPDATE 然后被拦"。**

### 3. `jsonb` 的写入一律走 SQLAlchemy 构造，**不用 `text()` 拼字面量**

本项目高频坑第 2 条：`text()` 会把 `{"page":1}` 里的 `:1` 当绑定参数，
报出莫名其妙的错误。凡必须用 `text()` 的地方，jsonb 参数一律
`CAST(:x AS jsonb)`。

### 4. **`baseline_items` 与 `step_approvals` 的插入顺序不重要**

三条不变量都是 `DEFERRABLE INITIALLY DEFERRED` 的约束触发器，
在 **COMMIT 期**校验。因此本层不必猜"谁先落库"——
`production_baselines` → `step_approvals` 与反过来都合法。
这是设计文档 §0.3 实测 2 的附带好处之一，也是本层**刻意不做顺序编排**的理由。

---

## 为什么 `projects.active_baseline_id` 的写入在本层

它是**投影列**（决策 5），与 `knowledge_assets.active_version_id` /
`prompt_templates.active_version_id` 同构 ——
"当前生效的是哪一条"由一个**单值列**回答（理由见 `models.py` 第二、三节）。
它的维护是**存储层的职责**（服务层只决定"要不要改"），
而它的**可重建性**由本层的 `newest_baseline_of()` 保证：
增量维护（`set_active_baseline`）与全量重算（`rebuild_active_baseline`）
必须一致，测试会断言这一点（与 WP20 `rebuild_active_version` 同款）。

⚠️ **`active_baseline_id` 存的是 `production_baselines.id`，而后者同时
就是 `version_nodes.id`** —— 两个名字指同一个 uuid，因为
`production_baselines.id` 是 `PK` 且同时 `FK version_nodes.id`
（数据结构 02 §6）。这是本包最容易被写错的一处（说明文档 §1.4 疏漏 #10
转述的 WP19 W4 教训）：**不要在服务层再生成一个 uuid 传给这里**。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select, text, update
from sqlalchemy.orm import Session, aliased

from rednote.modules.baseline.models import (
    BaselineItem,
    InformationDraft,
    ProductionBaseline,
    StepApproval,
)
from rednote.modules.versions.models import VersionNode



class BaselineRepository:
    """WP22 四张表（+ `projects.active_baseline_id`）的读写入口。

    事务边界由调用方（service / 端点）控制。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # 通用
    # ------------------------------------------------------------------

    @property
    def session(self) -> Session:
        """本仓库持有的 session（供下游在同一事务内造别的仓库）。

        与 `BriefRepository.session` / `KnowledgeRepository.session` /
        `PromptRepository.session` 同一条理由：创建基线的事务要同时
        做四件事（注册版本节点、写基线、写批准、更新 `active_baseline_id`），
        它们必须来自同一个 session —— 否则"原子"不成立。
        """
        return self._session

    def flush(self) -> None:
        """把待写变更立刻发给数据库。

        让约束错误在**预期位置**抛出（那里能给出可读的 4xx），
        而不是等到 `session_scope` 退出时提交才炸 —— 那时已经离开
        能给出可读错误的代码位置（WP12/WP13/WP16/WP18/WP20/WP23 的教训）。

        ⚠️ **它不能替代 COMMIT 期的延迟约束**：三条不变量的触发器
        在 COMMIT 时才跑，`flush()` 不会触发它们。这正是设计意图
        （实测 2）——"同事务"的定义就是"提交时一起成立"。
        """
        self._session.flush()

    def now(self) -> datetime:
        return self._session.execute(select(func.now())).scalar_one()

    # ------------------------------------------------------------------
    # projects / brands / products（判定所需的外部数据）
    # ------------------------------------------------------------------

    def project_scope(
        self, project_id: uuid.UUID
    ) -> tuple[uuid.UUID, uuid.UUID] | None:
        """取项目的 `(brand_id, product_id)`。项目不存在时返回 `None`。

        **它是跨品牌判据的项目侧数据源**（AC-24）。用一条只取两列的
        查询而不是 `session.get(Project, ...)`：本层不需要 Project 对象，
        拉一个完整 ORM 实例只为了让 `ddl.py` 的触发器有一条
        **等价但不同实现**的路径会更糟（两处判据漂移）。
        此处只提供数据，**判据在 `baseline_types.KNOWLEDGE_SCOPE_MATCH_RULE`**
        （唯一权威声明，由 `ddl.py` 与 `service.py` 共用）。
        """
        row = self._session.execute(
            text("SELECT brand_id, product_id FROM projects WHERE id = :pid"),
            {"pid": project_id},
        ).first()
        if row is None:
            return None
        return (row[0], row[1])

    def project_exists(self, project_id: uuid.UUID) -> bool:
        return bool(
            self._session.execute(
                text("SELECT 1 FROM projects WHERE id = :pid"),
                {"pid": project_id},
            ).first()
        )

    def knowledge_scope(
        self, asset_id: uuid.UUID
    ) -> tuple[str, uuid.UUID | None, uuid.UUID | None, uuid.UUID | None] | None:
        """取知识资产的 `(scope, brand_id, product_id, project_id)`。

        资产不存在时返回 `None`。返回四元组而不是 ORM 对象：
        `KnowledgeAsset` 绑在 session 上，跨层持有会 `DetachedInstanceError`；
        而且这四列就是跨品牌判据的**全部输入**（多取一列都是浪费）。
        """
        row = self._session.execute(
            text(
                "SELECT scope, brand_id, product_id, project_id "
                "FROM knowledge_assets WHERE id = :aid"
            ),
            {"aid": asset_id},
        ).first()
        if row is None:
            return None
        return (row[0], row[1], row[2], row[3])

    def knowledge_version_asset(self, version_id: uuid.UUID) -> uuid.UUID | None:
        """取知识版本的 `asset_id`（第 3 层"版本必须属于资产"的服务层预检数据源）。

        `ddl.py` 的 `trg_baseline_items_asset_scope` 已经在 COMMIT 期兜住这件事，
        但那时错误来自一个延迟触发器：消息里没有"哪个字段错了"，
        客户端无法据此修正输入（与 WP20 `_require_asset_version` 同一条理由）。
        服务层这一道给出可读的 422，数据库那一道管住绕过本层的所有写入。

        版本不存在时返回 `None`：调用方要能区分"版本不存在"与
        "版本属于别的资产" —— 两者都是 422，但 `reason` 不同。
        """
        row = self._session.execute(
            text("SELECT asset_id FROM knowledge_versions WHERE id = :vid"),
            {"vid": version_id},
        ).first()
        return None if row is None else row[0]

    def revision_no(self, node_id: uuid.UUID) -> int | None:
        """取版本节点的修订号（列表/详情要展示"BL-v3"里的那个 3）。

        `production_baselines` 自己不存 `revision_no`（决策 5：不造第二个真值来源），
        它只在 `version_nodes` 里 —— 因此这个数字必须从注册表取，
        与 `GET /versions/{id}` 返回的是**同一个**来源。
        """
        row = self._session.execute(
            text("SELECT revision_no FROM version_nodes WHERE id = :nid"),
            {"nid": node_id},
        ).first()
        return None if row is None else int(row[0])

    def revision_numbers(
        self, node_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """批量取修订号（列表端点用，避免逐行 N+1）。

        分页窗口最多 100 条，逐条查是可接受的；但既然列表本来就要拼
        `BaselineSummary`，一次取回更简单，也让"列表里的 BL-v2 与详情里的
        BL-v2 是同一个数字"这条不变量只有一个数据来源。

        **走 ORM 的 `in_()` 而不是 `text("id = ANY(:ids)")`**：后者要把
        Python 的 `list[UUID]` 作为数组参数送进去，psycopg 侧的元素类型
        推断依赖上下文，写错会报 `operator does not exist: uuid = text`
        （本项目高频坑第 2 条：`text()` 只适合标量绑定）。`in_()` 由
        SQLAlchemy 逐元素绑定，类型明确。
        """
        if not node_ids:
            return {}
        stmt = select(VersionNode.id, VersionNode.revision_no).where(
            VersionNode.id.in_(list(node_ids))
        )
        return {
            row[0]: int(row[1]) for row in self._session.execute(stmt).all()
        }

    def node_exists(self, node_id: uuid.UUID) -> bool:
        """版本节点是否存在（批准对象的校验用）。

        `step_approvals.subject_version_id` 指向 `version_nodes.id`。
        本层提供存在性查询；"这个节点的 `entity_type` 对不对"
        由服务层判（它需要给出可读的错误消息）。
        """
        return bool(
            self._session.execute(
                text("SELECT 1 FROM version_nodes WHERE id = :nid"),
                {"nid": node_id},
            ).first()
        )

    def node_entity_type(self, node_id: uuid.UUID) -> str | None:
        """取版本节点的 `entity_type`（服务层据此判"批的对象类型对不对"）。

        **为什么要查它**：`step_approvals.subject_kind` 是"这一步的产物是什么"
        （受控取值，见 `baseline_types.SUBJECT_KINDS`），而
        `version_nodes.entity_type` 是"注册表里的类型标签"。
        两者语义不同（说明文档 §2.3 决策 4 的说明），
        但**一个声称 `BASELINE` 的批准指向一个 `BRIEF` 节点**显然是矛盾 ——
        这类矛盾能在写入前抓到，就不必等下游读到时才发现。
        """
        row = self._session.execute(
            text("SELECT entity_type FROM version_nodes WHERE id = :nid"),
            {"nid": node_id},
        ).first()
        return None if row is None else str(row[0])

    # ------------------------------------------------------------------
    # information_drafts（**唯一可变表**）
    # ------------------------------------------------------------------

    def get_draft(self, project_id: uuid.UUID) -> InformationDraft | None:
        """取本项目的审核工作区（`UQ(project_id)` 保证至多一条）。"""
        return self._session.execute(
            select(InformationDraft).where(InformationDraft.project_id == project_id)
        ).scalar_one_or_none()

    def insert_draft(
        self, *, project_id: uuid.UUID, brief_version_id: uuid.UUID | None
    ) -> InformationDraft:
        """创建空工作区。

        **五块内容全部留空**（`[]` / `{}`）而不是给一份预填的样例：
        一份"看起来已经被审核过"的初始草稿是**假数据** ——
        它会让 `readiness` 的三条条件在没有人工介入的情况下变绿
        （若样例里恰好有一个 PRODUCIBLE 场景引用了一条 CONFIRMED 事实）。
        空态的语义是明确的："什么都还没审核"。
        """
        row = InformationDraft(
            project_id=project_id,
            brief_version_id=brief_version_id,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def update_draft(
        self,
        *,
        draft_id: uuid.UUID,
        expected_lock_version: int,
        brief_version_id: uuid.UUID | None,
        claims: list[Any] | None,
        conflicts: list[Any] | None,
        scenes: list[Any] | None,
        boundaries: dict[str, Any] | None,
        research_selection: list[Any] | None,
        operator_confirmed: bool | None,
    ) -> int:
        """条件 UPDATE 更新工作区，返回受影响行数。

        **为什么用条件 UPDATE 而不是读-改-写**：
        `WHERE id = :id AND lock_version = :expected` 把"检查版本"
        与"写入"压在同一条语句里。前置的 `require_if_match` 只保证
        "客户端拿的不是过期值"，它挡不住两个**同时通过**前置校验的请求
        —— 那正是"乐观锁"这个词的全部含义。

        返回 0 表示"读到之后有人改过"，服务层据此报 412
        并回带当前 ETag（协议 §1.1）。

        **`None` 表示"本次不改这一块"**，与"改成空"不同 ——
        用 `None` 而不用哨兵对象是因为请求体本身就是
        "只有出现的字段才改"（与 WP18 `PUT /brief-draft` 同款语义）。

        ⚠️ **`operator_confirmed` 的特殊性**：它是 `bool | None`，
        而 `False` 是**合法的显式值**（运营可以取消确认）。
        因此这里用 `is not None` 判断而不是真值判断 ——
        写 `if operator_confirmed:` 会让"取消确认"这个动作**静默无效**，
        而那意味着条件 ③ 永远为真（能创建基线的判据被绕过）。

        ⚠️ **`brief_version_id` 必须和 `operator_confirmed` 一样用 `is not None`
        判断**（MAJOR-3 实测：这里曾是**无条件写**，而本函数的 docstring
        明说「`None` 表示本次不改这一块」—— **代码违反了自己的契约**）。
        症状是「只改 `scenes`」的保存会把运营设好的 `briefVersionId`
        静默清成 NULL，且返回 HTTP 200。它与下面六块是**同类字段**，
        写法必须一致；把 `None` 当成「改成空」在这里是不可恢复的数据丢失。
        """
        values: dict[str, Any] = {
            "updated_at": func.now(),
            "lock_version": InformationDraft.lock_version + 1,
        }
        if brief_version_id is not None:
            values["brief_version_id"] = brief_version_id
        if claims is not None:
            values["claims"] = claims
        if conflicts is not None:
            values["conflicts"] = conflicts
        if scenes is not None:
            values["scenes"] = scenes
        if boundaries is not None:
            values["boundaries"] = boundaries
        if research_selection is not None:
            values["research_selection"] = research_selection
        if operator_confirmed is not None:
            values["operator_confirmed"] = operator_confirmed

        result = self._session.execute(
            update(InformationDraft)
            .where(
                InformationDraft.id == draft_id,
                InformationDraft.lock_version == expected_lock_version,
            )
            .values(**values),
            execution_options={"synchronize_session": False},
        )
        return int(result.rowcount or 0)

    def reload_draft(self, draft_id: uuid.UUID) -> InformationDraft | None:
        """强制从库重新读（`populate_existing=True`）。

        **为什么不用 `session.expire_all()`**：它会把调用方正在编辑的
        其它对象一起失效（WP17 ISS-53 的教训）。只刷这一个对象。
        """
        return self._session.get(
            InformationDraft, draft_id, populate_existing=True
        )

    # ------------------------------------------------------------------
    # production_baselines（**不可变**，无 update / delete）
    # ------------------------------------------------------------------

    def get_baseline(self, baseline_id: uuid.UUID) -> ProductionBaseline | None:
        return self._session.get(ProductionBaseline, baseline_id)

    def insert_baseline(
        self,
        *,
        baseline_id: uuid.UUID,
        project_id: uuid.UUID,
        brief_version_id: uuid.UUID | None,
        ruleset_version: str,
        snapshot: dict[str, Any],
        supersedes_id: uuid.UUID | None,
        change_reason: str,
        created_by: uuid.UUID | None,
    ) -> ProductionBaseline:
        """插入一条基线。

        **`baseline_id` 由调用方给**（不是数据库生成）：它必须等于
        WP11 `register_version` 返回的 `node_id` —— 两行同事务插入且
        `id` 相同，这是"注册表与具体表"的配对方式（数据结构 02 §6）。

        **`supersedes_id` 是单向的**（决策 5）：本行记"我替代了谁"，
        旧行**不写**"我被谁替代"（它不可变）。
        "谁替代了 BL-v2" 由 `list_superseding()` 反向查询得到。
        """
        row = ProductionBaseline(
            id=baseline_id,
            project_id=project_id,
            brief_version_id=brief_version_id,
            ruleset_version=ruleset_version,
            snapshot=snapshot,
            supersedes_id=supersedes_id,
            change_reason=change_reason,
            created_by=created_by,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def list_baselines(
        self, *, project_id: uuid.UUID, limit: int, offset: int
    ) -> list[ProductionBaseline]:
        stmt: Select[tuple[ProductionBaseline]] = (
            select(ProductionBaseline)
            .where(ProductionBaseline.project_id == project_id)
            .order_by(
                ProductionBaseline.created_at.desc(), ProductionBaseline.id
            )
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.execute(stmt).scalars().all())

    def count_baselines(self, *, project_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(ProductionBaseline)
            .where(ProductionBaseline.project_id == project_id)
        )
        return int(self._session.execute(stmt).scalar_one())

    def list_superseding(self, baseline_id: uuid.UUID) -> list[ProductionBaseline]:
        """**反向查询**："谁替代了这条基线"（决策 5 的落点）。

        没有 `superseded_by_id` 列，因此这个问题只能这样回答 ——
        而这正是"不可变"的代价与证据：若某天有人加回了那一列，
        这个方法就会变成死代码，测试会通过（因为它查得到空集），
        而"替代关系只有单向"这条不变量已经被破坏了。
        """
        stmt = (
            select(ProductionBaseline)
            .where(ProductionBaseline.supersedes_id == baseline_id)
            .order_by(ProductionBaseline.created_at, ProductionBaseline.id)
        )
        return list(self._session.execute(stmt).scalars().all())

    def newest_baseline_of(self, project_id: uuid.UUID) -> ProductionBaseline | None:
        """本项目**链尾**（没有任何其它行 `supersedes` 它的那条）。

        它是 `active_baseline_id` 的**全量重算**依据
        （`rebuild_active_baseline` 用它）。

        **为什么按 `NOT EXISTS` 而不是 `ORDER BY created_at DESC LIMIT 1`**：
        后者回答的是"最新创建的那条"，而链尾回答的是"没有被任何行替代的
        那条"——当数据被回填（补录一条历史基线）时，两者**不同**，
        而"当前生效"的语义是后者。

        用 `supersedes_id IS NULL` 也能表达同一个意思（链根只有一条，
        见部分唯一索引 `uq_production_baselines_project_root`），
        但那依赖"链是唯一的"这个前提；`NOT EXISTS` 在链被破坏时
        会返回**多条或零条**（暴露问题），而 `IS NULL` 会静默返回那条链根
        （即使它已被替代）。**判据要能区分两种实现**，这里选暴露的那个。

        ⚠️ **子查询里的比较对象必须是「另一行的 `supersedes_id`」**（MAJOR-4）：
        初版写成 `ProductionBaseline.supersedes_id == ProductionBaseline.id`
        —— **同一行自比，恒假**（一条记录的 `supersedes_id` 永远不等于它
        自己的 `id`）→ `EXISTS` 恒 False → `~EXISTS` 恒 True → 过滤条件
        **恒真** → 返回的其实是任意一行，且**不报任何错**。
        修法：用一个**别名** `_child` 代表"可能取代我的那一行"，
        比较 `_child.supersedes_id == ProductionBaseline.id`（相关子查询）。

        代价说明：这个错误让 `_require_supersedes_target` 的判据 2
        把"链根"误认成"链尾"，于是「替代一条已被替代的基线」能通过校验
        → 基线链分叉（两条同时声称取代同一条），而
        `projects.active_baseline_id` 只能指向其中一条。
        """
        _child = aliased(ProductionBaseline)
        stmt = (
            select(ProductionBaseline)
            .where(
                ProductionBaseline.project_id == project_id,
                ~select(_child.id)
                .where(_child.supersedes_id == ProductionBaseline.id)
                .exists(),
            )
        )
        return self._session.execute(stmt).scalar_one_or_none()

    # ------------------------------------------------------------------
    # baseline_items（**不可变**，无 update / delete）
    # ------------------------------------------------------------------

    def insert_items(self, rows: list[dict[str, Any]]) -> None:
        """批量插入基线项（**不可变表**，只插不改）。

        调用方传 dict 而不是 ORM 对象：服务层已经从 `ItemDraft`
        （frozen dataclass）算好了每一列的最终值（含 `context_order`
        与 `item_no`），本层只负责落库 —— 让"哪一列填什么"这件事
        只有一处实现（服务层），本层不参与派生。

        用 `add_all` 而不是逐条 `add`：一条基线的条目数可达数百，
        逐条 flush 会把往返次数放大到不可接受。
        """
        if not rows:
            return
        self._session.add_all([BaselineItem(**row) for row in rows])
        self._session.flush()

    def list_items(self, baseline_id: uuid.UUID) -> list[BaselineItem]:
        """按 `item_no` 升序取全部条目（含未纳入项）。

        **含 `EXCLUDED`**：AC-24 要求「实际选用条目、版本、片段与
        **未纳入原因**」。过滤掉排除项会让"为什么没纳入"在详情接口里
        无处可查 —— 而它正是半年后复盘时唯一想知道的事。

        按 `item_no` 而不是 `context_order`：前者是**入库时的编号**
        （稳定、唯一），后者是**上下文顺序**（可跨档混排）。
        详情接口要展示的是"这份基线里有哪几项"，用编号。
        """
        stmt = (
            select(BaselineItem)
            .where(BaselineItem.baseline_id == baseline_id)
            .order_by(BaselineItem.item_no)
        )
        return list(self._session.execute(stmt).scalars().all())

    def count_items(self, baseline_id: uuid.UUID) -> tuple[int, int]:
        """返回 `(纳入数, 排除数)`。

        **为什么返回元组而不是两个方法**：两个数字来自同一次聚合，
        分成两次调用会让它们来自两个不同的时间点（并发下可能出现
        "纳入 5 + 排除 2 = 7"而实际只有 6 行）。这是 ISS-76 的形态
        （整体与分项不一致 → 前端显示矛盾）。

        用 `FILTER (WHERE ...)` 而不是 `CASE WHEN`：前者是 SQL 标准
        且语义更直白（"数满足条件的行"而不是"把不满足的算 0 再求和"）。
        """
        stmt = select(
            func.count().filter(BaselineItem.inclusion == "INCLUDED"),
            func.count().filter(BaselineItem.inclusion == "EXCLUDED"),
        ).where(BaselineItem.baseline_id == baseline_id)
        row = self._session.execute(stmt).one()
        return (int(row[0]), int(row[1]))

    # ------------------------------------------------------------------
    # step_approvals（**不可变**，无 update / delete）
    # ------------------------------------------------------------------

    def insert_approval(
        self,
        *,
        project_id: uuid.UUID,
        step_no: int,
        subject_kind: str,
        subject_version_id: uuid.UUID,
        decision: str,
        reason: str,
        decided_by: uuid.UUID | None,
        supersedes_id: uuid.UUID | None,
    ) -> StepApproval:
        """插入一条批准（**不可变**，闭合 OP-151 的写入路径）。

        `supersedes_id` 而非 `superseded_by_id`：见
        `baseline_types.StepApprovalDraft` 的 docstring（与设计文档
        §2.3 决策 4 表格的一处措辞分歧，已记录）。
        """
        row = StepApproval(
            project_id=project_id,
            step_no=step_no,
            subject_kind=subject_kind,
            subject_version_id=subject_version_id,
            decision=decision,
            reason=reason,
            decided_by=decided_by,
            supersedes_id=supersedes_id,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get_approval(self, approval_id: uuid.UUID) -> StepApproval | None:
        return self._session.get(StepApproval, approval_id)

    def find_effective_approval(
        self,
        *,
        project_id: uuid.UUID,
        step_no: int,
        subject_kind: str,
        subject_version_id: uuid.UUID,
    ) -> StepApproval | None:
        """找「这一版」**当前生效**的那条批准（没有任何其它行取代它）。

        ⚠️ **不是链根**（MAJOR-6）：本方法原名 `find_root_approval`，按
        `supersedes_id IS NULL` 取，而那个口径**与 DDL 触发器不一致** ——
        `trg_production_baselines_approval_required` 判的是
        「`decision = 'APPROVED'` **且未被任何行取代**」。
        链根会**永久停留在第一次写入的那条**上，于是：

          · 先 `REJECTED`、再 `APPROVED`（取代①）之后，链根仍是①的
            `REJECTED` → 想重批时被误判成「已经是 APPROVED，无需重复」
            → **409 把重批永久堵死**；
          · 反过来，链根是 `APPROVED` 而链尾是 `REJECTED` 时
            `require_baseline_approval` 会**放行**，而触发器会拒绝
            → 「能读到的结论」与「真实约束」相反。

        同一口径的两个读法必须给出同一个答案，否则服务层就是在
        骗调用方。这里与触发器对齐：**未被取代的那条**。

        `uq_step_approvals_root` 只保证"链根最多一条"，与本方法无关 ——
        它管的是插入侧的约束（新批准必须取代旧的，不能再插一条链根）。
        """
        _child = aliased(StepApproval)
        stmt = (
            select(StepApproval)
            .where(
                StepApproval.project_id == project_id,
                StepApproval.step_no == step_no,
                StepApproval.subject_kind == subject_kind,
                StepApproval.subject_version_id == subject_version_id,
                ~select(_child.id)
                .where(_child.supersedes_id == StepApproval.id)
                .exists(),
            )
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def list_approvals(
        self, *, project_id: uuid.UUID, step_no: int | None = None
    ) -> list[StepApproval]:
        stmt: Select[tuple[StepApproval]] = select(StepApproval).where(
            StepApproval.project_id == project_id
        )
        if step_no is not None:
            stmt = stmt.where(StepApproval.step_no == step_no)
        stmt = stmt.order_by(
            StepApproval.step_no, StepApproval.decided_at, StepApproval.id
        )
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # projects.active_baseline_id（投影列，见模块 docstring）
    # ------------------------------------------------------------------

    def active_baseline_id(self, project_id: uuid.UUID) -> uuid.UUID | None:
        row = self._session.execute(
            text("SELECT active_baseline_id FROM projects WHERE id = :pid"),
            {"pid": project_id},
        ).first()
        return None if row is None else row[0]

    def set_active_baseline(
        self, *, project_id: uuid.UUID, baseline_id: uuid.UUID | None
    ) -> int:
        """条件 UPDATE 设置 `projects.active_baseline_id`，返回受影响行数。

        与 `PromptRepository.set_active_version` 同款：
        `WHERE ... AND active_baseline_id IS DISTINCT FROM :new` 把
        "检查是否已经是目标值"与"写入"压在同一条语句里 ——
        重复提交会让 `rowcount = 0`，调用方据此知道"这次没有真正改变"。

        **为什么用 `IS DISTINCT FROM` 而不是 `<>`**：
        `NULL <> NULL` 返回 NULL（不是 true），因此"从 NULL 改成 NULL"
        在 `<>` 下会被判为不匹配而 `rowcount = 0` ——
        巧合地正确，但"从某值改成 NULL"也会因为同一条语句的
        NULL 语义在不同位置表现不同而难以推理。`IS DISTINCT FROM`
        是 NULL 安全的，语义唯一。
        """
        result = self._session.execute(
            text(
                "UPDATE projects SET active_baseline_id = :bid, updated_at = now() "
                "WHERE id = :pid "
                "AND active_baseline_id IS DISTINCT FROM :bid"
            ),
            {"pid": project_id, "bid": baseline_id},
        )
        return int(result.rowcount or 0)

    def rebuild_active_baseline(self, project_id: uuid.UUID) -> uuid.UUID | None:
        """**全量重算** `active_baseline_id` 并写入，返回新值。

        它是"增量维护（`set_active_baseline`）与投影定义一致"的判据 ——
        测试断言"用增量路径走 N 步"与"直接重算"得到同一个值
        （与 WP20 `rebuild_active_version` 同款）。

        **它与 `set_active_baseline` 必须调用同一个 `newest_baseline_of()`**：
        若重算逻辑自己写一份"取最新"，两处一旦漂移，
        问题会在恢复数据时才暴露（那时既没有增量历史也没有对照）。
        """
        newest = self.newest_baseline_of(project_id)
        target = None if newest is None else newest.id
        self.set_active_baseline(project_id=project_id, baseline_id=target)
        return target


__all__ = ["BaselineRepository"]
