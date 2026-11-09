"""`self_check_runs` / `self_check_rule_results` / `self_check_findings` 的仓储。

跨模块取数走**裸 SQL**（平级模块不可互 import），列名即对方契约：
写错列名当场 `UndefinedColumn`，不会静默。

## 当前版本的口径（与 `media` / `feedback` 同一条）

**当前版本** = `content_items.current_version_id` 投影；投影为 NULL 时回落
`ORDER BY version_no DESC LIMIT 1`。**禁止**直接 `ORDER BY version_no DESC`
取末版：回滚后投影指向旧版，而末版是回滚前的那一版 ⇒ "这一轮判的是当前版"
会在回滚过的内容上给出错误答案。

本包只在**读端点**用它（算 `subjectState: CURRENT | STALE`）；跑一轮自检时
"当前版本"来自 `content_bridge.read_content_edit_target` 的同一次快照
（不另查一次，否则段落坐标与 `subject_version_id` 可能来自两个时刻）。

## 跨模块只读方法逐个给理由

| 方法 | 读谁的表 | 为什么仓储层要提供 |
|---|---|---|
| `current_content_version_id` | `content_items` / `content_versions` | 读端点判 `subjectState`；口径必须与 content 的编辑目标同源 |
| `list_media_snapshots` | `media_items` / `media_versions` / `media_renditions` | 媒体一致性与技术格式两类规则的全部输入；**一次查询**拿齐"位 + 当前版本的产物规格"，避免 N+1 与跨快照 |
| `effective_baseline_claim_scope` | `projects` / `production_baselines` / `brief_claims` | 事实类规则的判据域。口径与 `content/repository.claim_scope` 同源（`brief_claims WHERE brief_version_id = ...`），但起点是 `projects.active_baseline_id`（"**当前生效**基线"而不是"生成时用的基线"） |
| `generation_baseline_id` | `generation_contexts` | 这一版**生成时**用的基线 —— 与上一条不同即"基线前进过"，是事实类规则不恒绿的来源 |
| `unresolved_ai_feedback_by_anchor` | 本包三张表 + `content_feedback_items` | 阻断项落反馈前的去重键查询（R4）。`content_feedback_items` 没有 `rule_code` 列（也不该加 —— 别人的表），所以去重键只能落在**本包侧** |

## 去重键为什么是四元组而不是"同内容同 rule_code"

键 = `(rule_code, anchor_kind, paragraph_ref, media_item_id)`。
只用 `rule_code` 会把"第 1 段的禁用词"与"第 3 段的禁用词"当成同一件事
⇒ 第二处永远不会落成反馈（**漏建 ⇒ 审核闸放行，静默**）。
反过来，键里加上 `message` 会让"同一处问题但措辞变了"重复堆积
（message 含实测值，正文一改就变）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session

from rednote.modules.review.models import (
    SelfCheckFinding,
    SelfCheckRuleResult,
    SelfCheckRun,
)
from rednote.modules.review.types import (
    MediaSnapshot,
    RenditionSnapshot,
)


class ReviewRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def flush(self) -> None:
        self._session.flush()

    # ------------------------------------------------------------------
    # 跨模块只读（裸 SQL）
    # ------------------------------------------------------------------

    def current_content_version_id(
        self, content_item_id: uuid.UUID
    ) -> uuid.UUID | None:
        """当前版本 id（投影优先，投影为 NULL 时回落末版）。见模块 docstring。"""
        row = self._session.execute(
            text("SELECT current_version_id FROM content_items WHERE id = :cid"),
            {"cid": content_item_id},
        ).first()
        if row is not None and row[0] is not None:
            return row[0]
        fallback = self._session.execute(
            text(
                "SELECT id FROM content_versions"
                " WHERE content_item_id = :cid ORDER BY version_no DESC LIMIT 1"
            ),
            {"cid": content_item_id},
        ).first()
        return None if fallback is None else fallback[0]

    def list_media_snapshots(
        self, content_item_id: uuid.UUID
    ) -> tuple[MediaSnapshot, ...]:
        """一条内容的全部图位 + 其**当前版本**的产物档位规格。

        **一次查询**（LEFT JOIN）而不是"先查位、再逐位查档位"：后者是 N+1，
        且 N 次查询之间可以插进一次重做 ⇒ 同一轮自检里不同图位的规格来自
        不同时刻。

        `LEFT JOIN` 而不是 `JOIN`：**没有版本的图位必须出现在结果里** ——
        那正是 `MEDIA_ITEM_NOT_READY` 要报的那一类（"位在、图没出"）。
        内连接会让它们静默消失，于是那条规则在最该命中的场景下恒绿。

        规格取自 `media_items.current_version_id` 指向的那一版（投影），
        **不是**"最后落库的一版"：重做失败后投影仍指向上一个可交付版本。

        ⚠️ 裸 SQL，列名是 `media_items` / `media_renditions` 的契约。
        """
        rows = self._session.execute(
            text(
                "SELECT mi.id, mi.paragraph_ref, mi.sort_order, mi.state,"
                "       mr.id, mr.kind, mr.state, mr.width, mr.height,"
                "       mr.byte_size, mr.image_format"
                "  FROM media_items mi"
                "  LEFT JOIN media_renditions mr"
                "         ON mr.media_version_id = mi.current_version_id"
                " WHERE mi.content_item_id = :cid"
                " ORDER BY mi.sort_order, mr.kind, mr.attempt_no"
            ),
            {"cid": content_item_id},
        ).all()

        ordered: list[uuid.UUID] = []
        base: dict[uuid.UUID, tuple[str | None, int, str]] = {}
        renditions: dict[uuid.UUID, list[RenditionSnapshot]] = {}
        for row in rows:
            media_item_id = row[0]
            if media_item_id not in base:
                ordered.append(media_item_id)
                base[media_item_id] = (row[1], int(row[2]), row[3])
                renditions[media_item_id] = []
            if row[4] is None:
                continue
            renditions[media_item_id].append(
                RenditionSnapshot(
                    rendition_id=row[4],
                    kind=row[5],
                    state=row[6],
                    width=None if row[7] is None else int(row[7]),
                    height=None if row[8] is None else int(row[8]),
                    byte_size=None if row[9] is None else int(row[9]),
                    image_format=row[10],
                )
            )
        return tuple(
            MediaSnapshot(
                media_item_id=media_item_id,
                paragraph_ref=base[media_item_id][0],
                sort_order=base[media_item_id][1],
                state=base[media_item_id][2],
                renditions=tuple(renditions[media_item_id]),
            )
            for media_item_id in ordered
        )

    def effective_baseline_claim_scope(
        self, project_id: uuid.UUID
    ) -> tuple[uuid.UUID | None, frozenset[uuid.UUID]]:
        """`(当前生效基线 id, 它的 claim 集合)`。

        起点是 `projects.active_baseline_id`（与
        `baseline/repository.active_baseline_id` 同一列、同一语义），
        而不是"最新那条基线" —— 投影才是"当前生效"的权威。

        基线为 NULL ⇒ `(None, frozenset())`，调用方必须据此走
        `SKIPPED(NO_EFFECTIVE_BASELINE)`：判据域为空时既不能判通过
        （没判据）也不能判失败（会把每条 binding 都误报成超范围）。

        基线存在但 `brief_version_id` 为 NULL（项目没走 Brief 流程）
        ⇒ `(baseline_id, frozenset())`：**如实报空**，不假装没有基线。
        """
        row = self._session.execute(
            text(
                "SELECT pb.id, pb.brief_version_id"
                "  FROM projects p"
                "  JOIN production_baselines pb ON pb.id = p.active_baseline_id"
                " WHERE p.id = :pid"
            ),
            {"pid": project_id},
        ).first()
        if row is None:
            return None, frozenset()
        baseline_id, brief_version_id = row[0], row[1]
        if brief_version_id is None:
            return baseline_id, frozenset()
        claims = self._session.execute(
            text("SELECT id FROM brief_claims WHERE brief_version_id = :vid"),
            {"vid": brief_version_id},
        ).all()
        return baseline_id, frozenset(claim[0] for claim in claims)

    def generation_baseline_id(
        self, content_version_id: uuid.UUID
    ) -> uuid.UUID | None:
        """这一版**生成时**用的基线（`generation_contexts.baseline_id`）。

        为空有两种合法来源：这一版是人工编辑追加的（没有生成上下文），
        或生成时项目还没有基线。两者在 evidence 里都显示为 `null` ——
        本方法不区分，因为事实类规则的判据是"**现在**是否在生效基线的范围内"，
        生成时的基线只是解释"为什么会不一致"的旁证。
        """
        row = self._session.execute(
            text(
                "SELECT gc.baseline_id"
                "  FROM content_versions cv"
                "  JOIN generation_contexts gc ON gc.id = cv.generation_context_id"
                " WHERE cv.id = :vid"
            ),
            {"vid": content_version_id},
        ).first()
        return None if row is None else row[0]

    def unresolved_ai_feedback_by_anchor(
        self, content_item_id: uuid.UUID
    ) -> dict[tuple[str, str, str | None, uuid.UUID | None], uuid.UUID]:
        """去重键 → 既有 AI 反馈 id（仅**未解决**的）。

        键 = `(rule_code, anchor_kind, paragraph_ref, media_item_id)`，
        见模块 docstring"去重键为什么是四元组"。

        筛选条件三条，缺一条就错：
          · `author_kind = 'AI'` —— 人提的同主题意见不该被机器复用；
          · `state NOT IN ('RESOLVED','REJECTED')` —— 已处置的阻断项**应当**
            在下一轮重新建一条（阻断是"当前事实"，不是永久禁令）；
          · 本包 findings 的 `feedback_item_id IS NOT NULL` —— 只认本包建过的。

        ⚠️ `content_feedback_items` 的 `author_kind` / `state` 列名是 WP29 的
        契约（本包不 import 它的 ORM 模型）。
        """
        rows = self._session.execute(
            text(
                "SELECT rr.rule_code, f.anchor_kind, f.paragraph_ref,"
                "       f.media_item_id, f.feedback_item_id"
                "  FROM self_check_findings f"
                "  JOIN self_check_rule_results rr ON rr.id = f.rule_result_id"
                "  JOIN self_check_runs r ON r.id = rr.run_id"
                "  JOIN content_feedback_items fi ON fi.id = f.feedback_item_id"
                " WHERE r.content_item_id = :cid"
                "   AND f.feedback_item_id IS NOT NULL"
                "   AND fi.author_kind = 'AI'"
                "   AND fi.state NOT IN ('RESOLVED', 'REJECTED')"
                " ORDER BY f.created_at"
            ),
            {"cid": content_item_id},
        ).all()
        return {
            (row[0], row[1], row[2], row[3]): row[4] for row in rows
        }

    def count_runtime_settings(self) -> int:
        """`runtime_settings` 的行数 —— **只给判据用**（R9 的断言对象）。

        为什么放在仓储层而不是测试里直接连库：R9 的判据是"跑一轮自检后
        这张表的行数**不变**"，而它必须在**同一个 session** 里数 ——
        另起一个连接数到的是未提交之前的旧值，断言会恒绿。
        """
        return int(
            self._session.execute(
                text("SELECT count(*) FROM runtime_settings")
            ).scalar_one()
        )

    # ------------------------------------------------------------------
    # self_check_runs（不可变，只追加）
    # ------------------------------------------------------------------

    def insert_run(self, **columns: Any) -> SelfCheckRun:
        row = SelfCheckRun(**columns)
        self._session.add(row)
        return row

    def get_run(self, run_id: uuid.UUID) -> SelfCheckRun | None:
        return self._session.get(SelfCheckRun, run_id)

    def run_project_id(self, run_id: uuid.UUID) -> uuid.UUID | None:
        """轮次 → 所属项目 id；不存在时 `None`。

        **为什么单独一个方法而不是让端点先 `get_run`**：
        `GET /self-checks/{runId}` 的路径里没有 `projectId`，而权限判定需要
        它（两步骤鉴权的第一步）。这一步只问"它属于哪个项目"，不需要把整行
        载入 session（随后的详情读取会连带 rule_results 与 findings 一起取）。
        """
        row = self._session.execute(
            text("SELECT project_id FROM self_check_runs WHERE id = :rid"),
            {"rid": run_id},
        ).first()
        return None if row is None else row[0]

    def list_runs_for_content(
        self,
        *,
        content_item_id: uuid.UUID,
        before: tuple[datetime, uuid.UUID] | None = None,
        limit: int = 20,
    ) -> list[SelfCheckRun]:
        """一条内容的自检轮次，**新的在前**（第一条即最新）。

        用 keyset（`before = (上一页末条的 created_at, id)`）而不是 OFFSET：
        `created_at` 是持续写入的排序键，OFFSET 在并发新增时会漏项或重复。
        排序 `created_at DESC, id DESC` 与 `before` 的比较严格对应。
        """
        stmt = select(SelfCheckRun).where(
            SelfCheckRun.content_item_id == content_item_id
        )
        if before is not None:
            before_created_at, before_id = before
            stmt = stmt.where(
                or_(
                    SelfCheckRun.created_at < before_created_at,
                    and_(
                        SelfCheckRun.created_at == before_created_at,
                        SelfCheckRun.id < before_id,
                    ),
                )
            )
        stmt = stmt.order_by(
            SelfCheckRun.created_at.desc(), SelfCheckRun.id.desc()
        ).limit(limit)
        return list(self._session.execute(stmt).scalars().all())

    def latest_run_ids_for_project(
        self,
        *,
        project_id: uuid.UUID,
        content_item_ids: Sequence[uuid.UUID] | None = None,
    ) -> list[uuid.UUID]:
        """项目下每条内容**最新一轮**的 id（看板列的数据源）。

        用 `DISTINCT ON (content_item_id)` 而不是"全取回来再在 Python 里按
        内容分组取第一条"：后者的窗口一旦被某条内容的历史轮次占满，
        别的内容就**一轮都看不到**（看板会显示"未自检"）。

        排序键里 `id DESC` 是必须的第二键：同一事务里插入的多行
        `created_at` 相同，只按时间排不出唯一答案（ISS-52 同款）。
        """
        sql = (
            "SELECT DISTINCT ON (content_item_id) id"
            "  FROM self_check_runs"
            " WHERE project_id = :pid"
        )
        params: dict[str, Any] = {"pid": project_id}
        if content_item_ids:
            sql += " AND content_item_id = ANY(CAST(:cids AS uuid[]))"
            params["cids"] = [str(cid) for cid in content_item_ids]
        sql += " ORDER BY content_item_id, created_at DESC, id DESC"
        return [row[0] for row in self._session.execute(text(sql), params).all()]

    def get_runs(self, run_ids: Sequence[uuid.UUID]) -> list[SelfCheckRun]:
        """按 id 批量取轮次，**保持入参顺序**（看板列的顺序由调用方决定）。"""
        if not run_ids:
            return []
        rows = self._session.execute(
            select(SelfCheckRun).where(SelfCheckRun.id.in_(list(run_ids)))
        ).scalars().all()
        by_id = {row.id: row for row in rows}
        return [by_id[run_id] for run_id in run_ids if run_id in by_id]

    # ------------------------------------------------------------------
    # self_check_rule_results（不可变，只追加）
    # ------------------------------------------------------------------

    def insert_rule_result(self, **columns: Any) -> SelfCheckRuleResult:
        row = SelfCheckRuleResult(**columns)
        self._session.add(row)
        return row

    def list_rule_results(self, run_id: uuid.UUID) -> list[SelfCheckRuleResult]:
        """一轮的逐规则结论，按 `(category, rule_code)` 排序。

        **不按 `created_at`**：同一事务里插入的多行时间戳相同，排不出唯一
        答案；而响应要按分类分组（原型的 8 张卡片），按分类排序让调用方
        不必自己再排一次。
        """
        stmt = (
            select(SelfCheckRuleResult)
            .where(SelfCheckRuleResult.run_id == run_id)
            .order_by(SelfCheckRuleResult.category, SelfCheckRuleResult.rule_code)
        )
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # self_check_findings（不可变，只追加）
    # ------------------------------------------------------------------

    def insert_finding(self, **columns: Any) -> SelfCheckFinding:
        row = SelfCheckFinding(**columns)
        self._session.add(row)
        return row

    def list_findings_for_run(
        self, run_id: uuid.UUID
    ) -> list[tuple[SelfCheckFinding, str]]:
        """一轮的全部 finding，附其所属的 `rule_code`。

        返回 `(finding, rule_code)` 而不是只返回 finding：findings 表**刻意
        不带** `rule_code` 投影列（见 `models.py` 的说明），而响应需要
        "这条命中是哪条规则报的" ⇒ join 在这里做一次，不在读端点里逐条反查。
        """
        stmt = (
            select(SelfCheckFinding, SelfCheckRuleResult.rule_code)
            .join(
                SelfCheckRuleResult,
                SelfCheckRuleResult.id == SelfCheckFinding.rule_result_id,
            )
            .where(SelfCheckRuleResult.run_id == run_id)
            .order_by(
                SelfCheckRuleResult.rule_code,
                SelfCheckFinding.created_at,
                SelfCheckFinding.id,
            )
        )
        return [(row[0], row[1]) for row in self._session.execute(stmt).all()]


__all__ = ["ReviewRepository"]
