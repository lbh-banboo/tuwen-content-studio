"""Brief 五张表的存储访问（WP18）。

**本层职责边界**：只做存储访问，不含业务规则判定。
"能不能固化"（必填字段、阻断疑问、输入完整度）的判定在 `service.py`，
但**判定所需的数据必须由本层在同一事务内读出** —— 这一点与 WP11 的
`VersionRepository` 同一条理由：若调用方先读一次、再传参进来，
两次读之间就有窗口，并发时能构造出"校验通过但写入时前提已变"。

**两条本层特有的约定**：

1. **裸 SQL 改状态后读回必须 `populate_existing=True`**（本项目高频坑第 3 条）。
   草稿的更新走 `UPDATE ... WHERE lock_version = ?`（条件 UPDATE，
   为的是把"检查"与"写入"压在同一条语句里），它**不经过 ORM 属性赋值**，
   于是 identity map 里那个对象仍是旧值。不强制刷新的话，
   `PUT /brief-draft` 的响应会回带**旧的** `lockVersion`，
   前端下一次提交必然 412 —— 而它明明刚拿到这个 ETag。

2. **jsonb 的写入一律走 SQLAlchemy 构造**（`update().values(...)` 传 Python 对象），
   **不用 `text()` 拼字面量**。本项目高频坑第 2 条：`text()` 会把
   `{"page":1}` 里的 `:1` 当绑定参数，报出莫名其妙的错误。
   凡必须用 `text()` 的地方，jsonb 参数一律 `CAST(:x AS jsonb)`。

**不可变表（`brief_versions` / `brief_claims` / `brief_issues`）不提供任何
update / delete 方法** —— 与 WP15 审计表、WP17 片段表同一口径：
"想改也改不了"（触发器 + ORM 守卫）是第二道，本层不给入口是第一道。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from rednote.modules.brief.models import (
    Brief,
    BriefClaim,
    BriefDraft,
    BriefIssue,
    BriefVersion,
)
from rednote.modules.brief.types import ISSUE_OPEN, SEVERITY_BLOCK


class BriefRepository:
    """Brief 五张表的读写入口。事务边界由调用方（service / 处理器）控制。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        """本仓库持有的 session（供下游在同一事务内造别的仓库）。

        与 `JobRepository.session` / `FileRepository.session` 同一条理由：
        `GET /brief` 要一次给出草稿、版本、结论、疑问、以及原文片段 ——
        它们分属四张表与两个模块（本模块 + WP17）。
        不复用同一个 session 的话，同一个响应里的字段会来自不同事务。
        """
        return self._session

    def flush(self) -> None:
        """把待写变更立刻发给数据库。

        让约束错误在**预期位置**抛出（那里能给出可读的 4xx），
        而不是等到 `session_scope` 退出时提交才炸 —— 那时已经离开
        能给出可读错误的代码位置（WP12/WP13/WP16 的教训）。
        """
        self._session.flush()

    def now(self) -> datetime:
        return self._session.execute(select(func.now())).scalar_one()

    # ------------------------------------------------------------------
    # briefs
    # ------------------------------------------------------------------

    def get_brief(self, brief_id: uuid.UUID) -> Brief | None:
        return self._session.get(Brief, brief_id)

    def get_brief_by_project(self, project_id: uuid.UUID) -> Brief | None:
        """按项目取逻辑 Brief（`UQ(briefs.project_id)` 支撑，一次索引命中）。"""
        return self._session.execute(
            select(Brief).where(Brief.project_id == project_id)
        ).scalar_one_or_none()

    def insert_brief(self, *, project_id: uuid.UUID) -> Brief:
        row = Brief(project_id=project_id)
        self._session.add(row)
        return row

    def set_active_version(
        self, *, brief_id: uuid.UUID, version_id: uuid.UUID
    ) -> None:
        """移动"当前生效版本"。

        用 ORM 属性赋值而不是条件 UPDATE：本表**没有** `lock_version`，
        不存在"读-改-写"的并发窗口需要压掉（两个并发的固化请求各自
        注册了自己的版本号，`active_version_id` 落到哪个都是合法结果 ——
        它表达的是"最近一次固化"，而"最近"由提交顺序决定）。
        """
        row = self._session.get(Brief, brief_id)
        if row is not None:
            row.active_version_id = version_id

    def set_draft(self, *, brief_id: uuid.UUID, draft_id: uuid.UUID) -> None:
        row = self._session.get(Brief, brief_id)
        if row is not None:
            row.draft_id = draft_id

    # ------------------------------------------------------------------
    # brief_drafts（可变，lock_version 是 ETag 载体）
    # ------------------------------------------------------------------

    def get_draft(self, draft_id: uuid.UUID) -> BriefDraft | None:
        """读草稿，**强制从数据库取最新值**。

        理由见模块 docstring 第 1 条：本表的状态变更走条件 UPDATE，
        identity map 里可能留着陈旧副本。
        """
        return self._session.get(BriefDraft, draft_id, populate_existing=True)

    def get_draft_for_brief(self, brief_id: uuid.UUID) -> BriefDraft | None:
        return self._session.execute(
            select(BriefDraft).where(BriefDraft.brief_id == brief_id)
        ).scalar_one_or_none()

    def insert_draft(
        self, *, brief_id: uuid.UUID, structured_fields: dict[str, Any]
    ) -> BriefDraft:
        row = BriefDraft(brief_id=brief_id, structured_fields=structured_fields)
        self._session.add(row)
        return row

    def update_draft(
        self,
        *,
        draft_id: uuid.UUID,
        expected_lock_version: int,
        values: dict[str, Any],
    ) -> int | None:
        """条件 UPDATE 草稿，返回**新的** `lock_version`；未命中返回 `None`。

        `WHERE lock_version = :expected` 是并发保证的落点（与 WP10 的
        `update_project_versioned` 同一手法）：前置的 `If-Match` 校验与
        这次写入之间有窗口，只有把期望值压进 SQL 才能闭合它。

        `lock_version = lock_version + 1` 在**数据库侧**自增而不是由调用方算 ——
        调用方算的话，两个并发请求会都写入 `expected + 1`，
        于是"版本号变了但没检测到冲突"。
        """
        payload = dict(values)
        payload["lock_version"] = BriefDraft.lock_version + 1
        payload["updated_at"] = func.now()
        stmt = (
            update(BriefDraft)
            .where(
                BriefDraft.id == draft_id,
                BriefDraft.lock_version == expected_lock_version,
            )
            .values(**payload)
            .returning(BriefDraft.lock_version)
        )
        row = self._session.execute(stmt).first()
        return None if row is None else int(row[0])

    def list_drafts_with_pending_enrichment(
        self, *, project_id: uuid.UUID, limit: int
    ) -> list[BriefDraft]:
        """**本项目**还有待处理加厚请求的草稿（处理器入口用）。

        **为什么是"扫描"而不是"按 id 查"**：`BRIEF_ENRICH` 任务的
        `ClaimedJob` 不带草稿 id（`jobs.result_ref` 是结果引用，
        不能用来装输入 —— 见 `handlers.py` 的模块 docstring）。
        因此处理器只能问"现在有没有待处理的请求"。

        **`project_id` 过滤不是可选的**：没有它，两个项目同时有待处理
        请求时，A 项目的 Worker 会消费掉 B 项目的请求 ——
        表现为"B 的加厚任务报请求缺失、A 的任务拿着 B 的来源跑"。
        而 `enrichment_request` 在入队事务里写入、被消费时立刻清空，
        所以"本项目存在待处理请求"这个状态本身
        就等价于"本项目有一个刚入队、还没被消费的加厚请求"。
        重复投递时两个 attempt 会抢同一个请求，而
        `read_enrichment_request` 的条件 UPDATE 只让一个成功。

        `populate_existing=True` 的理由与 `get_draft` 相同：本表的
        变更走裸 UPDATE，identity map 里可能留着"请求已被清空"之前的副本。
        """
        stmt = (
            select(BriefDraft)
            .join(Brief, Brief.id == BriefDraft.brief_id)
            .where(
                Brief.project_id == project_id,
                BriefDraft.enrichment_request.is_not(None),
            )
            .order_by(BriefDraft.updated_at.asc(), BriefDraft.id.asc())
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # brief_versions（不可变）
    # ------------------------------------------------------------------

    def get_version(self, version_id: uuid.UUID) -> BriefVersion | None:
        return self._session.get(BriefVersion, version_id)

    def latest_version(self, brief_id: uuid.UUID) -> BriefVersion | None:
        """该逻辑 Brief 的最新版本。

        按 `created_at DESC, id DESC` 排序而不是按 `revision_no`：
        `revision_no` 在 `version_nodes` 上，本表没有这一列
        （数据结构 02 §6 的 `brief_versions` 列清单里没有它，
        它属于注册表的职责）。`id` 作为平局打破者保证顺序确定 ——
        `created_at` 是**事务**时间戳，同一事务里插入的行值相同。
        """
        return self._session.execute(
            select(BriefVersion)
            .where(BriefVersion.brief_id == brief_id)
            .order_by(BriefVersion.created_at.desc(), BriefVersion.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def list_versions(
        self, brief_id: uuid.UUID, *, limit: int
    ) -> list[BriefVersion]:
        """版本历史，最新在前。返回**最多 limit+1 条**（调用方判 `hasMore`）。"""
        return list(
            self._session.execute(
                select(BriefVersion)
                .where(BriefVersion.brief_id == brief_id)
                .order_by(BriefVersion.created_at.desc(), BriefVersion.id.desc())
                .limit(limit + 1)
            )
            .scalars()
            .all()
        )

    def insert_version(
        self,
        *,
        version_id: uuid.UUID,
        brief_id: uuid.UUID,
        source_file_id: uuid.UUID,
        source_parse_run_id: uuid.UUID | None,
        source_state: str,
        product_name: str,
        model_code: str | None,
        summary: str,
        structured_fields: dict[str, Any],
        created_by: uuid.UUID | None,
    ) -> BriefVersion:
        """插入具体版本行。

        `version_id` **由调用方给**（= `register_version` 返回的 `node_id`），
        不是本层生成：`brief_versions.id` 必须与 `version_nodes.id` 相同
        （数据结构 02 §5），而那两行在同一事务里插入。
        让本层自己生成 id 就得先把节点插进去、再读回它的 id ——
        多一次往返，且把"两行必须同 id"这条不变量变成了一个需要读回来的约定。

        **不在这里 flush**：调用方需要把"插入注册表 + 插入具体表 +
        插入 claims/issues"放在同一个 SAVEPOINT / 事务里（OP-88 的延迟
        触发器在提交时校验，顺序不重要，但原子性重要）。
        """
        row = BriefVersion(
            id=version_id,
            brief_id=brief_id,
            source_file_id=source_file_id,
            source_parse_run_id=source_parse_run_id,
            source_state=source_state,
            product_name=product_name,
            model_code=model_code,
            summary=summary,
            structured_fields=structured_fields,
            created_by=created_by,
        )
        self._session.add(row)
        return row

    # ------------------------------------------------------------------
    # brief_claims / brief_issues（不可变，随版本冻结）
    # ------------------------------------------------------------------

    def insert_claims(
        self, *, version_id: uuid.UUID, claims: list[dict[str, Any]]
    ) -> int:
        """批量插入结论。`claims` 的元素形状见 `repository_claims` 的产出。"""
        if not claims:
            return 0
        rows = [
            BriefClaim(
                brief_version_id=version_id,
                field_path=item["field_path"],
                claim_text=item["claim_text"],
                category=item["category"],
                evidence_segment_id=item["evidence_segment_id"],
                evidence_url=item["evidence_url"],
                applicability=item["applicability"],
                resolution_status=item["resolution_status"],
                resolution_reason=item["resolution_reason"],
                confirmed_by=item["confirmed_by"],
                claim_hash=item["claim_hash"],
            )
            for item in claims
        ]
        self._session.add_all(rows)
        return len(rows)

    def insert_issues(
        self, *, version_id: uuid.UUID, project_id: uuid.UUID, issues: list[dict[str, Any]]
    ) -> int:
        if not issues:
            return 0
        rows = [
            BriefIssue(
                project_id=project_id,
                brief_version_id=version_id,
                field_path=item["field_path"],
                severity=item["severity"],
                description=item["description"],
                state=item["state"],
                resolution=item["resolution"],
                resolved_by=item["resolved_by"],
            )
            for item in issues
        ]
        self._session.add_all(rows)
        return len(rows)

    def list_claims(self, version_id: uuid.UUID) -> list[BriefClaim]:
        """按 `(field_path, claim_hash)` 排序 —— **顺序必须确定**。

        `created_at` 是事务时间戳，同一批插入的行值相同（WP17 的 ISS-52
        实测：三行读回来顺序随机）。不显式排序的话，前端每次刷新
        看到的结论顺序都可能不同，而"我上次看到的那条不见了"是最难查的
        一类用户反馈。
        """
        return list(
            self._session.execute(
                select(BriefClaim)
                .where(BriefClaim.brief_version_id == version_id)
                .order_by(BriefClaim.field_path.asc(), BriefClaim.claim_hash.asc())
            )
            .scalars()
            .all()
        )

    def list_issues(self, version_id: uuid.UUID) -> list[BriefIssue]:
        """按 `(severity, field_path, id)` 排序 —— 同上，顺序必须确定。

        `severity` 升序让 `BLOCK` 排在 `WARN` 前面（字符串序：B < W），
        这正是审核人想先看到的。
        """
        return list(
            self._session.execute(
                select(BriefIssue)
                .where(BriefIssue.brief_version_id == version_id)
                .order_by(
                    BriefIssue.severity.asc(),
                    BriefIssue.field_path.asc(),
                    BriefIssue.id.asc(),
                )
            )
            .scalars()
            .all()
        )

    def count_blocking_issues(self, version_id: uuid.UUID) -> int:
        """本版未处置的 `BLOCK` 疑问数。

        `brief_issues` 上的 CHECK 已经禁止"BLOCK 且 OPEN"的行存在 ——
        所以这个计数在**正常数据**下恒为 0。它的用途有两个：
          1. 固化前的自查（若不为 0，说明有人绕过 CHECK 写了数据，
             或 CHECK 被改坏了 —— 那时宁可拒绝固化）；
          2. 测试里断言"确实没有 BLOCK 且 OPEN 的行"。
        """
        return int(
            self._session.execute(
                select(func.count())
                .select_from(BriefIssue)
                .where(
                    BriefIssue.brief_version_id == version_id,
                    BriefIssue.severity == SEVERITY_BLOCK,
                    BriefIssue.state == ISSUE_OPEN,
                )
            ).scalar_one()
        )

    def list_blocking_issues(
        self, *, project_id: uuid.UUID
    ) -> list[BriefIssue]:
        """本项目**全部**未处置的 `BLOCK` 疑问（跨版本）。

        用途是"步骤 1 能不能推进"的判断入口（数据结构 02 §6：
        "核心疑问未解决阻断步骤 1"）—— 它问的是项目级的问题，
        不是版本级的。命中 `ix_brief_issues_open` 这个部分索引。
        """
        return list(
            self._session.execute(
                select(BriefIssue)
                .where(
                    BriefIssue.project_id == project_id,
                    BriefIssue.severity == SEVERITY_BLOCK,
                    BriefIssue.state == ISSUE_OPEN,
                )
                .order_by(BriefIssue.field_path.asc(), BriefIssue.id.asc())
            )
            .scalars()
            .all()
        )

    # ------------------------------------------------------------------
    # 跨模块只读：项目与用户是否存在
    # ------------------------------------------------------------------

    def project_exists(self, project_id: uuid.UUID) -> bool:
        """项目是否存在（把外键错误提前成可读的 422）。

        与 WP11 的 `VersionRepository.project_exists` 同一条理由 ——
        但**不 import 它的实现**：那会把"两个模块的仓库互相依赖"
        变成一个必须维护的关系，而这是一行查询。
        """
        from rednote.modules.projects.models import Project

        return (
            self._session.execute(
                select(Project.id).where(Project.id == project_id).limit(1)
            ).first()
            is not None
        )


__all__ = ["BriefRepository"]
