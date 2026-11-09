"""WP27 五张表的存储访问。

**本层职责边界**：只做存储访问，不含业务规则判定。
"这次生成要不要预占""这张图能不能用这条素材"的判定在 `service.py` /
`schemas.py`，但**判定所需的数据必须由本层在同一事务内读出** —— 与 WP11 的
`VersionRepository` / WP18 的 `BriefRepository` / WP20 的 `KnowledgeRepository`
/ WP23 的 `PromptRepository` / WP26 的 `ContentRepository` 同一条理由：
调用方先读一次、再传参进来，两次读之间就有窗口，并发时能构造出
"校验通过但写入时前提已变"。

**七条本层特有的约定**：

1. **四张不可变表（`media_versions` / `reference_assets` / `media_renditions` /
   `media_generation_contexts`）不提供任何 update / delete 方法。**
   与 WP15 审计表、WP17 片段表、WP18 `brief_versions`、WP20
   `knowledge_versions`、WP23 `prompt_versions`、WP26 `content_versions`
   同一口径：触发器 + ORM 守卫（`guard_immutable`）是第二道，
   **本层不给入口是第一道**。

2. **投影改写走条件 UPDATE**（`WHERE id = :id AND lock_version = :expected`
   + `synchronize_session=False`）。`media_items` 的五个可变列
   （`current_version_id` / `current_version_no` / `paragraph_ref` /
   `sort_order` / `state`）是**投影**（`models.py` docstring 三/四），
   只由本层在"追加版本"与"标记失败"两处改写。把"检查乐观锁版本"与"写入"
   压在同一条语句里，并发重做时后者拿到 `rowcount = 0` 而不是静默覆盖。
   改写后**必须** `reload_media_item()`（`populate_existing=True`）——
   条件 UPDATE 不经过 ORM 属性赋值，identity map 里那个对象仍是旧值，
   不强制刷新的话响应会回带**旧的** `currentVersionId`。

   ⚠️ **`generation_request` 不在投影里**（`models.py` docstring 三的
   第五列那一节）：它是"最后一次意图"的记录，追加版本**不碰**它，
   因此 `advance_projection` 的 `values(...)` 里**没有**它。
   把它并进投影会让每次重做都覆盖掉用户刚填的参数。

3. **业务版本号 `version_no` 用 `max + 1` 在同一事务内取**，
   靠 `uq_media_versions_item_version_no` 兜住并发。**不能"先查再插"** ——
   两个并发重做都会读到同一个 max，然后一个撞唯一约束，而
   一次唯一约束冲突会把**整个事务**标记为 aborted（WP10/WP11 已踩）。
   因此调用方必须用 `savepoint()` 包住"取号 + 插入"这一段，
   撞了就回滚这一段、重新取号再试（与 WP11 的 `register_version` 同一手法）。

4. **`now()` 是数据库时间、且在同一事务内是常量**。
   本层的排序**一律不用 `created_at`**：同一事务插入的多行时间戳完全相同
   （WP17 ISS-52）。版本历史按 `version_no` 排；"当前有效的预览"按
   `attempt_no` 取 `MAX`（`models.py` docstring 六 —— 那是**显式落库的事实**，
   不是时间戳巧合）。跨事务的历史（生成上下文）才按 `created_at` 排。

5. **两张表各有一处幂等域，语义不同**：
   · `media_items.last_idempotency_key` —— 一次**生成请求**的标识，
     让"同一请求重放"能找回已经物化出来的那批媒体身份行；
   · `uq_media_generation_contexts_idempotency`（`content_item_id` +
     `idempotency_key`）—— 一次**单项生成**的标识，让"同一张图的同一次生成"
     不重复落上下文（R5 的存储落点；网关侧的幂等由
     `CapabilityRequest.idempotency_key` 保证）。
   两者混同的后果见 `types.IDEMPOTENCY_SCOPE_MEDIA_GENERATE` 的注释。

6. **★ 本层承载"某素材当前可用吗"的单一取数点**
   （`models.py` docstring 五末段 / `ddl.active_asset_states()` 的 docstring
   把落点钉在这里）。**判据**：

   > 素材 `X` 当前可用 ⇔ `X` 存在，**且**不存在一行
   > `state = 'RETIRED' AND supersedes_asset_id = X.id`。

   `state` 只说"登记动作"，后继说"是否被覆盖"，**两列合起来**才是答案。
   因此本层提供 `usable_asset_ids()` / `reference_asset_usable()` /
   `list_reference_assets(only_usable=True)`，三者**共用同一个私有条件构造器**
   （`_usable_asset_condition`）—— 那是全仓唯一拼这段判断的地方。

   ⚠️ **`ddl.active_asset_states()` 不是这个判据**：它只给"登记动作"的
   状态名清单，任何调用方拿它直接过滤 `state == 'ACTIVE'` 都会
   **漏掉后继判断**，于是"已被停用的素材仍可被新图引用"（AQ-03 的对照关系
   会悄悄指向一个已作废的型号标注）。这条不是靠注释守的 ——
   `tools/quality/check_media_asset_usability_single_source.py` 用 AST
   核对"`supersedes_asset_id` 与 `RETIRED` 的组合"只出现在本文件里
   （ISS-95/ISS-177 的教训：声称有防线必须能当场指出脚本里哪个函数）。

7. **★ 本层还负责给 `schemas.validate_reference_bindings` 供"上游事实"**。
   绑定的三条判据（素材存在 / 同项目 / 当前可用）分别需要"哪些 id 存在"、
   "哪些属于**别的**项目"、"哪些在本项目且仍可用"—— 三者必须**来自同一次读**，
   否则会在一个中途变过的素材库上判合法性。详见 `claim_assets()`。

   ⚠️ **本条曾与原实现不符，2026-10-07 当场改正**：`claim_assets()` 初版
   在 SQL 里就带 `project_id = :pid` 过滤，于是"跨项目"与"压根不存在"
   在应用层**同形**（都不在返回集合里）⇒
   `REASON_REFERENCE_BINDING_CROSS_PROJECT` **永远不可达**（死码），
   而 `ddl.py` 关于"`schemas.py` 先判一道、触发器是第二道"的说法
   也就建立在一条**实际不存在**的前一道上。这正是本仓 G9 条
   （`check_media_asset_usability_single_source.py`）要抓的形态：
   **声称有防线 ≠ 有防线**。修法是**去掉 SQL 侧的项目过滤**，
   把"属于哪个项目"作为**第三个事实**一并返回，由 `schemas.py` 分流。

分层是单向的：`types ← models ← repository`（本模块 import 前两者，
**不** import `schemas` / `service`），且**不 import 任何上游模块的 ORM 类**
—— 跨模块只读一律用 `text()` 直查列名（列名是对方的契约，
写错会当场报 `UndefinedColumn`，**不会静默**）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import Select, exists, func, null, select, text, update
from sqlalchemy.orm import Session, aliased

from rednote.modules.media.models import (
    MediaComponent,
    MediaGenerationContext,
    MediaItem,
    MediaRendition,
    MediaVersion,
    ReferenceAsset,
)
from rednote.modules.media.types import (
    ASSET_STATE_RETIRED,
    MEDIA_FORM_STATIC,
    MEDIA_SCHEMA_VERSION,
)
from rednote.modules.projects.models import Project


class MediaRepository:
    """WP27 五张表的读写入口。

    事务边界由调用方（service / 端点 / 处理器）控制。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        """本仓库持有的 session（供下游在同一事务内造别的仓库）。

        与 `ContentRepository.session` 同一条理由：
        `GET /media/{id}` 的详情要一次给出媒体身份、当前版本、版本历史、
        rendition 与素材绑定 —— 它们分属本模块的五张表与 WP11 的
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
    # 跨模块只读（列名是对方的契约；写错当场报 UndefinedColumn，不会静默）
    # ------------------------------------------------------------------

    def content_item_project(self, content_item_id: uuid.UUID) -> uuid.UUID | None:
        """内容条目所属项目。条目不存在时返回 `None`。

        `media_items.content_item_id` 是 FK，但 FK **管不住**"这条内容属于
        本项目" —— 跨项目挂图是语义错误（图要对齐本品牌的产品事实），
        而它**不会有任何报错**。判定在 `service.py`，数据在这里读。

        返回 `None` = 内容不存在，与"内容属于别的项目"是两件事，
        调用方要分别给出不同的 4xx。
        """
        row = self._session.execute(
            text("SELECT project_id FROM content_items WHERE id = :cid"),
            {"cid": content_item_id},
        ).first()
        return None if row is None else row[0]

    def content_body_for_paragraphs(
        self, content_item_id: uuid.UUID
    ) -> str | None:
        """该内容**当前正文**（`content_versions.body`）。内容不存在时返回 `None`。

        ## 为什么需要它（R4 的取数点）

        `media_versions.paragraph_ref` 记的是"这张图对应正文哪一段"，
        而段的定义**来自正文本身** ⇒ 必须能取到"当前这一版的 `body`"。

        ## 取哪一版：投影优先，`version_no` 回落

        主口径是 `content_items.current_version_id` —— 那是 `content` 模块
        自己维护的**「当前版本」投影**（`content/models.py` 称其为投影，
        `content/service.py` 读详情时走的也是它，不是 `MAX(version_no)`）。
        本方法若自行按 `version_no` 推断"当前版"，就是**在对方的地盘上
        猜对方的语义**：投影是显式指针、可以指向**任意一版**（例如回滚、
        或某版被标记为当前），而 `version_no DESC` 只会给出"最后落库的那版"。
        两者在有回滚或非单调推进时**分叉**。

        投影为 `NULL` 时**回落**到 `ORDER BY version_no DESC LIMIT 1`：
        `content_items.current_version_id` 是**可空**的
        （`content/models.py` 明确它 `deferrable=True, initially="DEFERRED"`，
        为的是允许"先建身份、后建版本"）。只投影口径会在这个合法状态下
        返回 `None`，而那不是"没有正文"，是"投影还没落" ⇒ 会把一次
        **正常的内容状态**误报成缺正文。只 `version_no` 口径则相反：
        内容存在但一版都没落时它会返回 `None`（正确）。

        ⚠️ **「当前这一版」不是「生成时那一版」**：这是本方法存在的关键。
        `paragraph_ref` 带文本哈希（`types.paragraph_ref_for`），
        所以正文一改，ref 自然不在新正文的段落集合里 ⇒ `service.py` 的
        `validate_paragraph_ref` 报 `PARAGRAPH_REF_UNKNOWN` ⇒ 重做。
        若本方法改成"取生成时那版"，这个机制就失效了而**不报错**。

        ⚠️ **裸 SQL，列名是 `content_versions` / `content_items` 的契约**
        （`body` / `version_no` / `content_item_id` / `current_version_id`）。
        与 `content_item_project` 同一纪律：模块平级不可互import，
        取对方数据只能走只读 SQL；写错列名当场 `UndefinedColumn`，
        不会静默。

        ⚠️ **切段口径必须与 `content` / `knowledge` 同口径**（只按空行切）：
        三处各有一份实现（平级不可互import），一致性由
        `tools/quality/check_paragraph_split_parity.py` 钉住，
        权威定义登记在 OP-197。
        """
        row = self._session.execute(
            text(
                "SELECT cv.body FROM content_items ci "
                "JOIN content_versions cv ON cv.id = ci.current_version_id "
                "WHERE ci.id = :cid"
            ),
            {"cid": content_item_id},
        ).first()
        if row is not None:
            return row[0]
        # 回落：投影为 NULL（"先建身份、后建版本"的合法中间态）时取最后落库的一版。
        fallback = self._session.execute(
            text(
                "SELECT body FROM content_versions "
                "WHERE content_item_id = :cid ORDER BY version_no DESC LIMIT 1"
            ),
            {"cid": content_item_id},
        ).first()
        return None if fallback is None else fallback[0]

    def file_object_exists(self, file_id: uuid.UUID) -> bool:
        """文件对象是否存在（`reference_assets.file_object_id` 的前置校验）。

        素材登记前要先确认"这个 file id 真的指向一个文件"，
        否则会以 FK 违反的形式冒出来（500 而不是可读的 4xx）。
        """
        row = self._session.execute(
            text("SELECT 1 FROM file_objects WHERE id = :fid"), {"fid": file_id}
        ).first()
        return row is not None

    def file_object_project(self, file_id: uuid.UUID) -> uuid.UUID | None:
        """文件对象所属项目。文件不存在时返回 `None`。

        与 `content_item_project` 同一理由：FK 管不住"这个文件属于本项目"。
        """
        row = self._session.execute(
            text("SELECT project_id FROM file_objects WHERE id = :fid"),
            {"fid": file_id},
        ).first()
        return None if row is None else row[0]

    # ------------------------------------------------------------------
    # ★★ 素材可用性：全仓唯一的取数点（本模块 docstring 第 6 条）
    # ------------------------------------------------------------------

    @staticmethod
    def _usable_asset_condition(asset_id_column: Any) -> Any:
        """★ **唯一**拼"某素材当前可用吗"这段判断的地方（私有）。

        判据（`models.py` docstring 五）：

            可用 ⇔ 不存在 `state = 'RETIRED' AND supersedes_asset_id = <它>`

        **为什么要 NOT EXISTS 而不是 `state == 'ACTIVE'`**：
        `state` 描述的是**登记动作**（"这一行代表的那次动作是什么"），
        不是"这一行现在能不能用"。一个 `ACTIVE` 的行**可能已经被
        一行 `RETIRED` 接替掉了** —— 只判 `state` 会让它继续通过，
        于是新图可以引用一个**已被停用**的素材，
        而 AQ-03 的对照关系会悄悄指向一个已作废的型号标注。

        **为什么是私有 staticmethod 而不是把 SQL 片段交出去**：
        交出去就有两个拼点，而"两处拼同一判断"必然分叉
        （分叉的表现是上述静默放行，**不报错**）。
        公开接口只暴露"结果"（可用的 id 集合 / 布尔），不暴露"条件"。
        """
        retired = aliased(ReferenceAsset)
        return ~exists(
            select(1)
            .select_from(retired)
            .where(
                retired.state == ASSET_STATE_RETIRED,
                retired.supersedes_asset_id == asset_id_column,
            )
        )

    def usable_asset_ids(
        self, asset_ids: Sequence[uuid.UUID]
    ) -> frozenset[uuid.UUID]:
        """★ 给一批素材 id，返回其中**当前可用**的那些（唯一判据）。

        **一次查询**给出结果：调用方拿它做"绑定的素材是否都可用"的判定，
        而逐个查询会在两次查询之间留下窗口（并发停用）。

        空入参返回空集合（**不是**"全部可用"）—— 返回空是如实报数，
        调用方据此拒绝，见 `schemas.validate_reference_bindings`。
        """
        ids = list(asset_ids)
        if not ids:
            return frozenset()
        stmt = select(ReferenceAsset.id).where(
            ReferenceAsset.id.in_(ids),
            self._usable_asset_condition(ReferenceAsset.id),
        )
        return frozenset(self._session.execute(stmt).scalars().all())

    def reference_asset_usable(self, asset_id: uuid.UUID) -> bool:
        """单个素材当前可用吗（委托 `usable_asset_ids`，**不另写一遍判据**）。

        用 `usable_asset_ids` 而不是在这里再拼一次 `exists()`：
        两处判断必然分叉，而分叉的那一刻**没有任何报错**。
        """
        return asset_id in self.usable_asset_ids([asset_id])

    # ------------------------------------------------------------------
    # reference_assets（**不可变**）
    # ------------------------------------------------------------------

    def get_reference_asset(self, asset_id: uuid.UUID) -> ReferenceAsset | None:
        """取素材行。

        ⚠️ **它不回答"可用吗"** —— 那要另外调 `reference_asset_usable()`。
        两个问题分开给方法，是为了让"想用素材的人"不得不显式走一次
        可用性判定（`models.py` docstring 五的单一取数点）。
        """
        return self._session.get(ReferenceAsset, asset_id)

    def list_reference_assets(
        self,
        *,
        project_id: uuid.UUID,
        limit: int,
        offset: int,
        only_usable: bool = True,
    ) -> list[ReferenceAsset]:
        """按项目取素材列表。

        `only_usable=True`（默认）时走**唯一判据**（`_usable_asset_condition`），
        **不是** `state == 'ACTIVE'` —— 两者在"素材已被停用"时给不同答案，
        而选错的那个会静默返回一个仍可被引用的已停用素材。

        `only_usable=False` 供"素材变更史"这类界面用（要看全链条）。
        默认取 `True`：默认必须是**最小**的那一档（与 R5 的
        "默认必须是最小的那一档"同一取向）—— 拿错时宁可少列，
        不可多列一个已作废的素材。

        按 `created_at DESC, id` 排：同一事务插入的多行时间戳相同，
        没有 `id` 兜底的话翻页会重复或漏行（不稳定的排序）。
        """
        stmt: Select[tuple[ReferenceAsset]] = select(ReferenceAsset).where(
            ReferenceAsset.project_id == project_id
        )
        if only_usable:
            stmt = stmt.where(self._usable_asset_condition(ReferenceAsset.id))
        stmt = stmt.order_by(
            ReferenceAsset.created_at.desc(), ReferenceAsset.id
        ).limit(limit).offset(offset)
        return list(self._session.execute(stmt).scalars().all())

    def count_reference_assets(
        self, *, project_id: uuid.UUID, only_usable: bool = True
    ) -> int:
        """素材总数。

        `only_usable` 的默认与语义与 `list_reference_assets` **逐字一致**
        （同一个判据、同一个默认）—— 两个方法的分母必须同源，
        否则"第 2 页是空的但总数说还有"这类不一致会出现，
        而它不报错。
        """
        stmt = select(func.count()).select_from(ReferenceAsset).where(
            ReferenceAsset.project_id == project_id
        )
        if only_usable:
            stmt = stmt.where(self._usable_asset_condition(ReferenceAsset.id))
        return int(self._session.execute(stmt).scalar_one())

    def insert_reference_asset(
        self,
        *,
        project_id: uuid.UUID,
        file_object_id: uuid.UUID,
        product_model: str,
        product_color: str | None = None,
        product_part: str | None = None,
        source: str | None = None,
        note: str | None = None,
        state: str | None = None,
        supersedes_asset_id: uuid.UUID | None = None,
        created_by: uuid.UUID | None = None,
    ) -> ReferenceAsset:
        """插入一条素材**登记动作**行（不可变表，只增不减）。

        `state` 走数据库默认值（`ACTIVE`）而不是在这里写死 ——
        让"素材状态的取值只有一个来源"（`types.py`）。
        **"停用"是插入一行 `state='RETIRED'` + `supersedes_asset_id`**，
        不是改行（`models.py` docstring 五）；本表**不提供** update 方法。

        `supersedes_asset_id` 的合法性（`RETIRED` 必填、指向的行存在、
        同项目）由 CHECK 与 `service.py` 承担，本层只落值。
        """
        row = ReferenceAsset(
            project_id=project_id,
            file_object_id=file_object_id,
            product_model=product_model,
            product_color=product_color,
            product_part=product_part,
            source=source,
            note=note,
            supersedes_asset_id=supersedes_asset_id,
            created_by=created_by,
        )
        if state is not None:
            row.state = state
        self._session.add(row)
        self._session.flush()
        return row

    def claim_assets(
        self, *, project_id: uuid.UUID, asset_ids: Sequence[uuid.UUID]
    ) -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID], frozenset[uuid.UUID]]:
        """★ 一次查询同时给出 `(existing_ids, foreign_ids, usable_ids)`。

        这三个值就是 `schemas.validate_reference_bindings` 的三条上游判据域
        （另两条判据 —— "去重后 >= 1" 与 "<= 上界" —— 是纯计数，
        不需要上游事实）：

        | 返回值 | 在 `validate_reference_bindings` 里的角色 |
        |---|---|
        | `existing_ids` | "绑定的素材必须存在"的判据域 |
        | `foreign_ids` | "绑定的素材必须**同项目**"的判据域（存在、但不属于 `project_id`）|
        | `usable_ids` | "绑定的素材必须**当前可用**"的判据域（存在 + 同项目 + 未被接替）|

        ## ★★ 为什么是**三个**值（2026-10-07 改正一处"声称与实现不符"）

        初版是**两个**值，且 SQL 里直接带 `project_id = :pid` 过滤。那写法有一个
        **静默**后果：跨项目的素材与"压根不存在"的素材都**不在**返回集合里
        ⇒ 两者在应用层**同形** ⇒ `REASON_REFERENCE_BINDING_CROSS_PROJECT`
        **永远不可达**（死码），而 `ddl.py` 第 3.2 节与 `types.py` 的注释都写着
        "`schemas.py` **先判一道**、本触发器是绕不过去的**第二道**" ——
        那个"第一道"实际不存在。

        这正是本仓 `tools/quality/check_media_asset_usability_single_source.py`
        的 G9 要抓的形态（**声称有防线 ≠ 有防线**，ISS-95/ISS-177），
        差别只在这次"声称"的对象是另一条判据而不是一个脚本。

        修法不是"在 Python 侧再查一次项目"（那会引出**第二次读**，
        与本节主张的"同一次读"直接冲突），而是把项目归属**降级为第三个事实**、
        与存在性一起在同一次读里取出来，由 `schemas.py` 分流。

        ## ★ 为什么必须**一次**查询给出三个值

        `usable_ids ⊆ (existing_ids ∩ 本项目)`，且 `foreign_ids ⊆ existing_ids`。
        若分几次读，三者就来自**不同快照**，而素材是**只增不减**的表
        （"停用"是追加一行）—— 中途插入的一行 `RETIRED` 会让
        `existing_ids` 说"在"而 `usable_ids` 说"不在"，于是
        `validate_reference_bindings` 会在一个**中途变过的**素材库上
        判合法性，且三条判据会给出**互相矛盾的诊断**
        （"素材不存在" vs "素材不可用" vs "跨项目"），而响应只能报一个。

        同一次读的落地方式：**一条查询同时选 id、project_id 与可用性**，
        在 Python 侧分流。`NOT EXISTS` 由数据库算，
        不受 Python 侧列表长度影响。

        ## 与 `claim_scope`（WP26）的关系

        形状刻意同构（WP26 用它一次给出 `(known_claim_ids, total_claim_count)`）。
        差别在返回的是**三个集合**而不是"集合 + 计数" ——
        因为绑定素材的判据是"每个都可用且同项目"，不是"绑定数 < 总数"。
        """
        ids = list(asset_ids)
        if not ids:
            return frozenset(), frozenset(), frozenset()
        # ⚠️ **不带 `project_id` 过滤**：项目归属是**取出来的事实**，
        # 不是**过滤条件**（见 docstring）。带上它就会让"跨项目"与
        # "不存在"同形，从而让一条判据永远不可达。
        stmt = select(
            ReferenceAsset.id,
            ReferenceAsset.project_id,
            self._usable_asset_condition(ReferenceAsset.id).label("usable"),
        ).where(ReferenceAsset.id.in_(ids))
        rows = self._session.execute(stmt).all()
        existing = frozenset(row[0] for row in rows)
        foreign = frozenset(row[0] for row in rows if row[1] != project_id)
        usable = frozenset(
            row[0] for row in rows if row[1] == project_id and row[2]
        )
        return existing, foreign, usable

    # ------------------------------------------------------------------
    # media_items（身份 + 投影，**可变**）
    # ------------------------------------------------------------------

    def get_media_item(self, media_item_id: uuid.UUID) -> MediaItem | None:
        return self._session.get(MediaItem, media_item_id)

    def get_media_item_for_update(
        self, media_item_id: uuid.UUID
    ) -> MediaItem | None:
        """取媒体身份行并**加行锁**（`SELECT ... FOR UPDATE`）。

        重做是"读当前版本 → 校验 → 追加新版本 → 改写投影"的序列。
        加锁让同一格的并发重做在**取号之前**就串行化，
        避免无谓的唯一约束冲突与重试（与
        `ContentRepository.get_content_item_for_update` 同一理由）。
        """
        return self._session.execute(
            select(MediaItem)
            .where(MediaItem.id == media_item_id)
            .with_for_update()
        ).scalar_one_or_none()

    def list_media_items(
        self, *, content_item_id: uuid.UUID, limit: int, offset: int
    ) -> list[MediaItem]:
        """一条内容的媒体列表（`GET /contents/{contentId}/media`）。

        **按 `sort_order` 升序**（不是 `created_at`）：`sort_order` 是
        身份键的一半，也是"第几张图"这个问题的**显式答案**
        （`trial-config.html:324` 的"封面固定第 1 位"读的就是它）。
        按时间排会在同一事务插入的多行上给出不确定的顺序。
        """
        stmt = (
            select(MediaItem)
            .where(MediaItem.content_item_id == content_item_id)
            .order_by(MediaItem.sort_order, MediaItem.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.execute(stmt).scalars().all())

    def count_media_items(self, *, content_item_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(MediaItem)
            .where(MediaItem.content_item_id == content_item_id)
        )
        return int(self._session.execute(stmt).scalar_one())

    def list_media_items_by_paragraph(
        self, *, content_item_id: uuid.UUID, paragraph_ref: str
    ) -> list[MediaItem]:
        """取"这一格内容里绑到指定段落"的媒体行。

        用于段落级重做的**范围计算**（"这一段配了哪些图"）——
        支撑它的索引是 `ix_media_items_content_paragraph`
        （`models.py` docstring 三的表格）。
        按 `sort_order` 排，理由同 `list_media_items`。
        """
        stmt = (
            select(MediaItem)
            .where(
                MediaItem.content_item_id == content_item_id,
                MediaItem.paragraph_ref == paragraph_ref,
            )
            .order_by(MediaItem.sort_order, MediaItem.id)
        )
        return list(self._session.execute(stmt).scalars().all())

    def list_media_items_by_idempotency(
        self, *, content_item_id: uuid.UUID, idempotency_key: str
    ) -> list[MediaItem]:
        """按"生成请求的幂等键"找回已经物化出来的那批媒体身份行。

        用于 `POST /contents/{id}/media-generations` 的重放：同一个请求键
        第二次到来时不再新建身份行，而是复用第一批（R5 的存储侧一半）。
        按 `sort_order` 排（同一批行的顺序就是它们的身份顺序）。
        """
        stmt = (
            select(MediaItem)
            .where(
                MediaItem.content_item_id == content_item_id,
                MediaItem.last_idempotency_key == idempotency_key,
            )
            .order_by(MediaItem.sort_order, MediaItem.id)
        )
        return list(self._session.execute(stmt).scalars().all())

    def insert_media_item(
        self,
        *,
        project_id: uuid.UUID,
        content_item_id: uuid.UUID,
        paragraph_ref: str,
        sort_order: int,
        created_by: uuid.UUID | None,
        idempotency_key: str | None,
        job_id: uuid.UUID | None = None,
        generation_request: dict[str, Any] | None = None,
        pending_reference_bindings: list[Any] | None = None,
        pending_regeneration_reason: str | None = None,
    ) -> MediaItem:
        """插入一条媒体**身份**行（尚无版本，投影为空）。

        `state` 与 `current_version_no` 走数据库默认值（`PENDING` / 0）——
        不在这里写死，是为了让"状态机"的取值只有一个来源（`types.py`）。

        `generation_request` 一并写入而不是随后再 UPDATE：
        发起那次事务里"建身份"与"记下要什么"是**同一件事的两半**，
        分开写会造出"身份在、参数还没写"的中间态 —— 而那个中间态
        恰好是处理器读到的样子（读到 `None` ⇒ 判装配故障，**不可重试**）。
        同一次 INSERT 让这个中间态**不可能被观察到**
        （与 `ContentRepository.insert_content_item` 逐字同款）。

        `paragraph_ref` 由调用方从正文段落派生
        （`types.paragraph_ref_for` 是唯一派生点）：它是**当前版本的投影**，
        首次插入时给出"打算配哪一段"的初值，随后由 `advance_projection`
        跟随实际版本前进。

        ★ `pending_reference_bindings` 与 `generation_request` **同一次 INSERT**
        （理由逐字同上，且更硬）：生成是**异步**的，端点返回后处理器在
        另一次事务里读这一列 —— 若分开写，中间态"身份在、绑定还没写"
        恰好就是处理器读到的样子，而它会被判成 R1 的"没有绑定"。

        ★ `job_id` 同样可以在**首次 INSERT** 就给（默认 `None`）：
        端点有两种合法写法 —— "先建身份行 → 入队 → 回填"（`parsing/api.py`
        的顺序，此时走 `set_job_id`）与"先入队 → 带 `job_id` 建行"
        （同一事务内二者等价）。给这个形参是为了让后者（以及测试里
        直接造"任务已挂好"的行）不必绕 `set_job_id` 两步走。

        ★ `pending_regeneration_reason` 同 `pending_reference_bindings`：
        它与"这一次要生成什么"同生共死，分开写会造出"绑定写了、理由没写"
        的中间态 —— 而重做理由是要落进**不可变版本行**（`media_versions.
        regeneration_reason`）的审计事实，丢掉就再也补不回来。
        **`None` 是常态**（首次生成没有"重做理由"），不落非空 CHECK：
        见 `models.py` 该列的 docstring。
        """
        row = MediaItem(
            project_id=project_id,
            content_item_id=content_item_id,
            paragraph_ref=paragraph_ref,
            sort_order=sort_order,
            created_by=created_by,
            job_id=job_id,
            last_idempotency_key=idempotency_key,
            generation_request=generation_request,
            pending_reference_bindings=pending_reference_bindings,
            pending_regeneration_reason=pending_regeneration_reason,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def set_generation_request(
        self, *, media_item_id: uuid.UUID, request: dict[str, Any]
    ) -> int:
        """写入/覆盖"这一格这一轮请求了什么"。返回受影响行数。

        **为什么要覆盖而不是拒绝重复写**：它是**最后一次意图**的记录，
        不是历史事实 —— 历史事实在 `media_generation_contexts`
        （每次生成一行、不可变）。重做（换档位 / 换模型档案）本来就要换参数，
        拒绝覆盖会让重做走不通，而"走不通"的报错会指向一个无关的方向。
        （与 `ContentRepository.set_generation_request` 逐字同款。）

        **与 `advance_projection` 一样不进 `lock_version` 前进**：
        它不参与任何乐观锁判据（没有"基于参数版本做决策"的地方），
        推进 `lock_version` 只会让并发重做多撞一次无谓的版本冲突。
        """
        result = self._session.execute(
            update(MediaItem)
            .where(MediaItem.id == media_item_id)
            .values(generation_request=request),
            execution_options={"synchronize_session": False},
        )
        return int(result.rowcount or 0)

    def set_pending_reference_bindings(
        self, *, media_item_id: uuid.UUID, bindings: list[Any] | None
    ) -> int:
        """写入/覆盖"下一次生成要绑的素材"。返回受影响行数。

        ★ **这一列是 R1 的输入暂存位**（理由见 `types` 的
        `PENDING_BINDINGS_IS_ARRAY_SQL` 注释与迁移 `b7c1e4a9d203` §一）：
        生成是**异步**的，端点入队后立刻返回，而绑定此刻还没落
        `media_versions` ⇒ 处理器必须能从投影上读到"本次请求的绑定"。

        **`bindings=None` 是合法调用**（生成成功后清空投影），
        与 `bindings=[]`（空数组，**非法**，被 CHECK 拒）**不是一回事**：
        前者"没有待生成的绑定"，后者"明确传了空绑定" —— 后者违反 R1
        （"至少一条"），前者是正常终态。

        **同样不进 `lock_version` 前进** —— 与 `set_generation_request`
        同款理由：它不参与任何乐观锁判据。
        """
        result = self._session.execute(
            update(MediaItem)
            .where(MediaItem.id == media_item_id)
            .values(
                pending_reference_bindings=(
                    null() if bindings is None else bindings
                )
            ),
            execution_options={"synchronize_session": False},
        )
        return int(result.rowcount or 0)

    def set_pending_regeneration_reason(
        self, *, media_item_id: uuid.UUID, reason: str | None
    ) -> int:
        """写入/覆盖"这一次重做为什么重做"。返回受影响行数。

        ★ **这一列是 F7「单张重做」理由的输入暂存位**，与
        `set_pending_reference_bindings` **逐字同形、同一条理由**：
        生成是**异步**的，端点在**发起**事务里拿到理由，处理器在
        **另一次事务**里才写 `media_versions`（不可变）——
        中间这一列就是理由的唯一落点（见 `types`
        `PENDING_REGENERATION_REASON_SQL` 注释与迁移 `c4d9f2a17e35` §二）。

        **`reason=None` 是合法且最常见的调用**：
        首次生成没有理由（`media_versions.regeneration_reason` 同一口径，
        首次的行恒 NULL）。**清空**发生在落版本之后 ——
        理由进了不可变版本行，投影上的暂存就过期了。

        **不在本方法里校验长度**：`MAX_REGENERATION_REASON_CHARS` 的
        fail-closed 校验在端点层（`api.py`），那里才能把超长翻成**业务
        原因码**；本层只负责写。若越权在这里抛，端点就没机会翻码 ——
        用户会看见 `varchar(500)` 的 SQLSTATE 22001（实测：列宽先于
        CHECK 拒绝，见迁移 `c4d9f2a17e35` 的两道防线实测表）。
        SQL 侧另有 CHECK 殿后（`char_length(...) <= N`）。

        **同样不进 `lock_version` 前进** —— 与 `set_generation_request` /
        `set_pending_reference_bindings` 同款理由：它不参与任何乐观锁判据。
        """
        result = self._session.execute(
            update(MediaItem)
            .where(MediaItem.id == media_item_id)
            .values(pending_regeneration_reason=reason),
            execution_options={"synchronize_session": False},
        )
        return int(result.rowcount or 0)

    def set_job_id(self, *, media_item_id: uuid.UUID, job_id: uuid.UUID) -> int:
        """把"这一格的在途任务"钉到本行。返回受影响行数。

        ★ 这是任务寻址的**写入侧一半**（读侧是 `find_media_item_for_job`）：
        端点在同一事务里「建身份行 → `enqueue_job` → 回填 `job_id`」，
        处理器便能用 `claimed.job_id` 反查出 `media_item_id` + `content_id`
        （照 `parsing/api.py` L163~167 的顺序，逐字同构）。

        **为什么本列是"在途指针"而非历史**：唯一约束
        `uq_media_items_job_id` 不允许两个任务同时指向同一行
        （一个任务只产出一版图，`calls_for(单图) = 1`）。
        重做是**覆盖**同一行的本列（旧任务已终结），不是追加。
        ⇒ 因此**没有**"清空 `job_id`"的常规调用路径：
        任务终态由 `jobs` 表自己记，本列只回答"当前挂的是哪个任务"。

        **不可重复指向**：唯一约束在 DB 层兜住 "一个 job 指两行"
        （实测：第二行挂同一 job 被 `UniqueViolation: uq_media_items_job_id`
        点名拒绝）—— 那种行对应"一个任务产出两张图"，与
        `calls_for()` 的口径直接矛盾，必须 fail-closed 而不是静默取一行。

        **不进 `lock_version`**：它不参与乐观锁判据（没有"基于任务 id
        做决策"的地方）。
        """
        result = self._session.execute(
            update(MediaItem)
            .where(MediaItem.id == media_item_id)
            .values(job_id=job_id),
            execution_options={"synchronize_session": False},
        )
        return int(result.rowcount or 0)

    def find_media_item_for_job(self, job_id: uuid.UUID) -> MediaItem | None:
        """按任务反查它要产出的媒体行（**任务处理器用的入口**）。

        这是"任务 → 媒体"关联的**唯一真相**（`media_items.job_id`），
        与 `ParseRepository.find_run_for_job` 逐字同形（WP17 先例）。

        **为什么不用 `jobs.content_id`**：那个列只能装一个 UUID，
        而本任务要 **两个** id（`media_item_id` 定位到"哪一张图"、
        `content_id` 定位到"哪条内容的正文"）。装得下 `content_id`
        就装不下 `media_item_id`，反过来亦然 ⇒ 借它必然丢一半信息。
        （三条先例的形态差异见 `types.py` 的"任务寻址"注释块。）

        **`ORDER BY` 是防御而非语义**：`uq_media_items_job_id` 保证
        至多一行命中，排序在正常情况下无用武之地。保留它是为了
        "万一有人绕过 service 手工造了两行"时给出**确定**的答案 ——
        与 `find_run_for_job` 同一取舍（那里同样不假设唯一）。
        取最早的一行（`created_at ASC`）：**不用 `sort_order`** ——
        跨内容比较 `sort_order` 没有意义，而 `created_at` 在同一事务内
        是常量（本模块 docstring 第 4 条）⇒ 真要打破平局得靠 `id`，
        但那属于"已经不合法"的场景，不必为它设计确定性。
        """
        stmt = (
            select(MediaItem)
            .where(MediaItem.job_id == job_id)
            .order_by(MediaItem.created_at.asc())
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def advance_projection(
        self,
        *,
        media_item_id: uuid.UUID,
        expected_lock_version: int,
        version_id: uuid.UUID,
        version_no: int,
        paragraph_ref: str,
        sort_order: int,
        state: str,
    ) -> int:
        """条件 UPDATE 把媒体身份的投影推进到新版本，返回受影响行数。

        **为什么用条件 UPDATE 而不是读-改-写**：`WHERE id = :id AND
        lock_version = :expected` 把"检查乐观锁"与"写入"压在同一条语句里。
        两个并发重做只有一个能改成功（另一个 `rowcount = 0`），
        而失败的那个**不会静默覆盖**已经前进的投影。

        **必须**随后 `reload_media_item()` —— 见本模块 docstring 第 2 条。

        ⚠️ `values(...)` 里**没有** `generation_request`：
        它不在投影里（见同一处的说明），投影推进不该动"最后一次意图"。
        """
        result = self._session.execute(
            update(MediaItem)
            .where(
                MediaItem.id == media_item_id,
                MediaItem.lock_version == expected_lock_version,
            )
            .values(
                current_version_id=version_id,
                current_version_no=version_no,
                paragraph_ref=paragraph_ref,
                sort_order=sort_order,
                state=state,
                lock_version=MediaItem.lock_version + 1,
            ),
            execution_options={"synchronize_session": False},
        )
        return int(result.rowcount or 0)

    def set_state(self, *, media_item_id: uuid.UUID, state: str) -> int:
        """把媒体状态改掉（用于生成失败落 `FAILED`），返回受影响行数。

        与 `advance_projection` 分开而不合并：失败路径**不动投影**
        （没有新版本），把两件事压进一个方法会让"失败时不小心把
        `current_version_id` 清空"变成一次无声的事故
        （与 `ContentRepository.set_state` 逐字同款）。
        """
        result = self._session.execute(
            update(MediaItem)
            .where(MediaItem.id == media_item_id)
            .values(state=state, lock_version=MediaItem.lock_version + 1),
            execution_options={"synchronize_session": False},
        )
        return int(result.rowcount or 0)

    def reload_media_item(self, media_item_id: uuid.UUID) -> MediaItem | None:
        """强制从库重新读（`populate_existing=True`）。

        **为什么不用 `session.expire_all()`**：它会把调用方正在编辑的
        其它对象一起失效（WP17 ISS-53 的教训）。只刷这一个对象。
        """
        return self._session.get(
            MediaItem, media_item_id, populate_existing=True
        )

    # ------------------------------------------------------------------
    # media_versions（**不可变**）
    # ------------------------------------------------------------------

    def get_version(self, version_id: uuid.UUID) -> MediaVersion | None:
        return self._session.get(MediaVersion, version_id)

    def latest_version(self, *, media_item_id: uuid.UUID) -> MediaVersion | None:
        """本格业务版本号最大的那一版。

        **按 `version_no` 排而不是 `created_at`**：见本模块 docstring 第 4 条 ——
        同一事务里插入的多行 `created_at` 完全相同，按它排得不到唯一答案。
        """
        stmt = (
            select(MediaVersion)
            .where(MediaVersion.media_item_id == media_item_id)
            .order_by(MediaVersion.version_no.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def list_versions(
        self, *, media_item_id: uuid.UUID, limit: int, offset: int
    ) -> list[MediaVersion]:
        """版本历史（`GET /media/{mediaId}` 的版本段），新版本在前。"""
        stmt = (
            select(MediaVersion)
            .where(MediaVersion.media_item_id == media_item_id)
            .order_by(MediaVersion.version_no.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.execute(stmt).scalars().all())

    def count_versions(self, *, media_item_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(MediaVersion)
            .where(MediaVersion.media_item_id == media_item_id)
        )
        return int(self._session.execute(stmt).scalar_one())

    def next_version_no(self, *, media_item_id: uuid.UUID) -> int:
        """下一个业务版本号（`max + 1`）。

        **同一事务内取**，靠 `uq_media_versions_item_version_no` 兜住并发
        （本模块 docstring 第 3 条）。调用方必须用 `savepoint()` 包住
        "取号 + 插入"这一段。
        """
        stmt = select(
            func.coalesce(func.max(MediaVersion.version_no), 0)
        ).where(MediaVersion.media_item_id == media_item_id)
        return int(self._session.execute(stmt).scalar_one()) + 1

    def insert_version(
        self,
        *,
        version_id: uuid.UUID,
        media_item_id: uuid.UUID,
        version_no: int,
        paragraph_ref: str,
        sort_order: int,
        reference_bindings: Sequence[dict[str, Any]],
        image_file_id: uuid.UUID,
        prompt: str,
        payload: dict[str, Any],
        payload_hash: str,
        regeneration_reason: str | None = None,
        generation_context_id: uuid.UUID | None = None,
        schema_version: int = MEDIA_SCHEMA_VERSION,
        created_by: uuid.UUID | None = None,
        media_form: str = MEDIA_FORM_STATIC,
    ) -> MediaVersion:
        """插入一个媒体版本行。

        `media_form`（WP28）：默认 `STATIC`，既有调用零改动；`LIVE` 由 4.5 的 Live 分支传入。
        取值域由列上的 CHECK（`media_form_valid`）兜，本层不重复判。

        **`version_id` 由调用方给**（不是数据库生成）：它必须等于 WP11
        `register_version` 返回的 `node_id` —— 两行同事务插入且 `id` 相同，
        这是"注册表与具体表"的配对方式（与
        `ContentRepository.insert_version` 逐字同款，OP-88 正向一半）。

        `regeneration_reason` 的写入规则见 `models.py` docstring 十：
        **只有重做端点产生的版本行**会写它，首次生成恒为 `None`。
        """
        row = MediaVersion(
            id=version_id,
            media_item_id=media_item_id,
            version_no=version_no,
            paragraph_ref=paragraph_ref,
            sort_order=sort_order,
            reference_bindings=[dict(item) for item in reference_bindings],
            image_file_id=image_file_id,
            prompt=prompt,
            regeneration_reason=regeneration_reason,
            generation_context_id=generation_context_id,
            payload=payload,
            payload_hash=payload_hash,
            schema_version=schema_version,
            created_by=created_by,
            media_form=media_form,
        )
        self._session.add(row)
        self._session.flush()
        return row

    # ------------------------------------------------------------------
    # media_components（**不可变**，WP28）
    # ------------------------------------------------------------------

    def insert_component(
        self,
        *,
        media_version_id: uuid.UUID,
        role: str,
        file_id: uuid.UUID,
        verification: str,
        sha256: str,
        byte_size: int,
    ) -> MediaComponent:
        """插入一个 Live 组件行。

        调用方必须已经插入了 `media_version_id` 对应的版本行（FK 不延迟）。
        角色 / 验证取值域 / 摘要形状 / 体积由列上的 CHECK 兜，本层不重复判；
        STILL 组件 `file_id == 版本 image_file_id` 与"仅 LIVE 版本可挂组件"
        由 `ddl.py` 的延迟触发器兜（提交时才判）。
        """
        row = MediaComponent(
            media_version_id=media_version_id,
            role=role,
            file_id=file_id,
            verification=verification,
            sha256=sha256,
            byte_size=byte_size,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def list_components(self, *, media_version_id: uuid.UUID) -> list[MediaComponent]:
        """某一版的全部组件。按 `role` 排（每版每角色至多一个 ⇒ 顺序确定）。"""
        stmt = (
            select(MediaComponent)
            .where(MediaComponent.media_version_id == media_version_id)
            .order_by(MediaComponent.role, MediaComponent.id)
        )
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # media_renditions（**不可变**）
    # ------------------------------------------------------------------

    def get_rendition(self, rendition_id: uuid.UUID) -> MediaRendition | None:
        return self._session.get(MediaRendition, rendition_id)

    def list_renditions(
        self, *, media_version_id: uuid.UUID
    ) -> list[MediaRendition]:
        """某一版的全部加工产物。

        按 `(kind, attempt_no)` 排 —— **不是 `created_at`**：
        同一事务插入的多行时间戳相同（docstring 第 4 条），
        而 `attempt_no` 是显式落库的事实，能给出确定顺序。
        """
        stmt = (
            select(MediaRendition)
            .where(MediaRendition.media_version_id == media_version_id)
            .order_by(
                MediaRendition.kind,
                MediaRendition.attempt_no,
                MediaRendition.id,
            )
        )
        return list(self._session.execute(stmt).scalars().all())

    def latest_rendition(
        self, *, media_version_id: uuid.UUID, kind: str
    ) -> MediaRendition | None:
        """"当前有效的这一档产物是哪一行" = `MAX(attempt_no)` 那一行。

        **不用 `created_at DESC LIMIT 1`** —— `models.py` docstring 六 已论证：
        那正是 ISS-52（"按某个恰好存在的列排序不是排序"）的同款，
        同一毫秒内两行就会让"当前"有两个答案。
        `attempt_no` 是**为这个问题专门落的列**，支撑索引是
        `ix_media_renditions_version_kind`。
        """
        stmt = (
            select(MediaRendition)
            .where(
                MediaRendition.media_version_id == media_version_id,
                MediaRendition.kind == kind,
            )
            .order_by(MediaRendition.attempt_no.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def next_attempt_no(
        self, *, media_version_id: uuid.UUID, kind: str
    ) -> int:
        """同一 `(版本, 档位)` 的下一个加工序号（`max + 1`，从 1 起）。

        与 `next_version_no` 同一手法：同一事务内取，
        靠 `uq_media_renditions_version_kind_attempt` 兜住并发；
        调用方用 `savepoint()` 包住"取号 + 插入"。
        """
        stmt = select(
            func.coalesce(func.max(MediaRendition.attempt_no), 0)
        ).where(
            MediaRendition.media_version_id == media_version_id,
            MediaRendition.kind == kind,
        )
        return int(self._session.execute(stmt).scalar_one()) + 1

    def insert_rendition(
        self,
        *,
        media_version_id: uuid.UUID,
        kind: str,
        attempt_no: int,
        state: str | None = None,
        file_object_id: uuid.UUID | None = None,
        width: int | None = None,
        height: int | None = None,
        byte_size: int | None = None,
        image_format: str | None = None,
        note: str | None = None,
    ) -> MediaRendition:
        """插入一条加工产物行。

        `state` 走数据库默认值（`PENDING`）。**`state='READY'` 时
        产物五列必须齐全**（`models.py` docstring 六的蕴含式 CHECK）——
        这条由 DDL 拦，本层如实落值：
        非 `READY` 的行**允许**带着已算出的部分信息
        （"尺寸知道了但转格式失败"），那是合法状态不是漏值。
        """
        row = MediaRendition(
            media_version_id=media_version_id,
            kind=kind,
            attempt_no=attempt_no,
            file_object_id=file_object_id,
            width=width,
            height=height,
            byte_size=byte_size,
            image_format=image_format,
            note=note,
        )
        if state is not None:
            row.state = state
        self._session.add(row)
        self._session.flush()
        return row

    # ------------------------------------------------------------------
    # media_generation_contexts（**不可变**；OP-160 落点）
    # ------------------------------------------------------------------

    def get_generation_context(
        self, context_id: uuid.UUID
    ) -> MediaGenerationContext | None:
        return self._session.get(MediaGenerationContext, context_id)

    def list_generation_contexts(
        self, *, content_item_id: uuid.UUID
    ) -> list[MediaGenerationContext]:
        """一条内容的全部生成上下文（详情端点用）。

        按 `created_at` 升序：这里读的是**跨事务**的历史（每次生成一个事务），
        因此时间戳各不相同 —— 与"同一事务内插入多行"是两种情形
        （docstring 第 4 条的例外，与 `ContentRepository` 同款）。
        """
        stmt = (
            select(MediaGenerationContext)
            .where(MediaGenerationContext.content_item_id == content_item_id)
            .order_by(
                MediaGenerationContext.created_at, MediaGenerationContext.id
            )
        )
        return list(self._session.execute(stmt).scalars().all())

    def find_generation_context(
        self, *, content_item_id: uuid.UUID, idempotency_key: str
    ) -> MediaGenerationContext | None:
        """按幂等键找一次生成（R5 的存储侧一半）。

        `uq_media_generation_contexts_idempotency` 保证最多一条。
        ⚠️ 键里的"内容"是 `content_item_id`（身份表）而**不是**
        `media_item_id` —— 见该唯一约束的注释（键里含媒体版本号）。
        """
        stmt = (
            select(MediaGenerationContext)
            .where(
                MediaGenerationContext.content_item_id == content_item_id,
                MediaGenerationContext.idempotency_key == idempotency_key,
            )
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def list_contexts_by_prompt_version(
        self, *, prompt_version_id: uuid.UUID, limit: int, offset: int
    ) -> list[MediaGenerationContext]:
        """**OP-160 的核心查询**：改了这个提示词版本，哪些历史生成受影响。

        这条查询能成立的前提是 `ix_media_generation_contexts_prompt_version`
        （`models.py` docstring 的"这张表就是 OP-160 在媒体侧的答案"一节）——
        没有它就退化成全表扫，而本表是"每次生成一行、只增不减"的。
        """
        stmt = (
            select(MediaGenerationContext)
            .where(MediaGenerationContext.prompt_version_id == prompt_version_id)
            .order_by(
                MediaGenerationContext.created_at.desc(),
                MediaGenerationContext.id,
            )
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.execute(stmt).scalars().all())

    def insert_generation_context(
        self,
        *,
        project_id: uuid.UUID,
        content_item_id: uuid.UUID,
        media_version_id: uuid.UUID | None,
        task_kind: str,
        idempotency_key: str,
        context_id: uuid.UUID | None = None,
        prompt_template_id: uuid.UUID | None = None,
        prompt_version_id: uuid.UUID | None = None,
        model_profile_id: uuid.UUID | None = None,
        provider_call_id: uuid.UUID | None = None,
        created_by: uuid.UUID | None = None,
    ) -> MediaGenerationContext:
        """插入一条生成上下文。

        ## `context_id` 为什么可显式给（不是让数据库生成就完了）

        `media_versions.generation_context_id` 与
        `media_generation_contexts.media_version_id` 构成**环 2**，两侧 FK 都是
        `DEFERRABLE INITIALLY DEFERRED`（`models.py` docstring 七）。
        这个环的**唯一**可行写入顺序是"先插一侧、后插另一侧"，而
        `media_versions` 是**不可变表**（`ddl.py` 挂
        `rednote_reject_mutation()`）⇒ 版本行那一侧的
        `generation_context_id` **只能在 INSERT 时给出**，事后 UPDATE 必撞
        `23001`。因此调用方必须**先知道**上下文行将用的 id，再插版本行。

        不给（`None`）时仍走列的 `server_default=gen_random_uuid()`。
        显式给时**只在赋值处覆盖主键**，不改列的生成方式：本仓的
        主键一律服务端生成（`platform/base.py:uuid_pk`），
        在 Python 侧再生成一次就是"两个 id 来源"。
        （与 `ContentRepository.insert_generation_context` 逐字同款。）
        """
        row = MediaGenerationContext(
            project_id=project_id,
            content_item_id=content_item_id,
            media_version_id=media_version_id,
            task_kind=task_kind,
            idempotency_key=idempotency_key,
            prompt_template_id=prompt_template_id,
            prompt_version_id=prompt_version_id,
            model_profile_id=model_profile_id,
            provider_call_id=provider_call_id,
            created_by=created_by,
        )
        if context_id is not None:
            row.id = context_id
        self._session.add(row)
        self._session.flush()
        return row


__all__ = ["MediaRepository"]