"""WP19 的只读查询层。

**本模块只读**。它没有任何 `insert_*` / `update_*` / `delete_*` ——
本包零写端点、零表（划界见说明文档 §1.7）。"只读"这一点由
`tests/test_research_wp19.py` 的模块级断言守着（见那里对
`inspect.getmembers` 的用法），而不是靠约定。

---

## 一、为什么本模块需要自己的查询，而不是全用既有 repository

三个数据来源各有主人：

| 数据 | 主人 | 本包怎么做 |
|---|---|---|
| Brief 版本 / claims | WP18 的 `BriefRepository` | **原样调用**（`get_version` / `list_claims`） |
| 文件 / 配额 | WP12 的 `FileRepository` | **原样调用**（`compute_effective_bytes`） |
| 解析运行 / 片段数 | WP17 的 `ParseRepository` | **原样调用**（`latest_run_for_file` / `count_segments`） |

**本模块只加一样东西：把"文件 + 关联 + blob + 最新解析 + 片段数"
一次查出来**。理由：

  · 逐文件调用 `latest_run_for_file` + `count_segments` 是 **2N+1 次查询**
    （N = 文件数）。配额上限 300MB，一个项目可能上千个文件 ——
    那会让研究页变成一个 N+1 查询的教科书案例；
  · 更重要的是**一致性**：逐次查询时，两次查询之间文件可能被
    重新解析（WP17 的 `parse_runs` 是只追加的），于是列表里
    第 3 个文件的 `segmentCount` 来自 A 次运行、`parseState` 来自 B 次。
    一条 SQL 里 join 出来则一定自洽。

## 二、`RESEARCH_FILE_SCAN_LIMIT` 与分页的关系（R2 的落点）

本项目已**三次**踩到"全局扫描 + 默认小 limit → 数据量依赖的偶发红"
（ISS-49 / ISS-59 / ISS-73）。ISS-73 的判据是：

> 默认 limit 是为**生产吞吐**选的，不是为**测试可见性**选的。
> 断言"我造的对象出现在列表里"时，调用处必须显式传大 limit。

本模块的查询**按 `project_id` 过滤**（不是全局列表），但仍显式写
`RESEARCH_FILE_SCAN_LIMIT = 1000` 作为**扫描上限**，并配
**稳定排序 `(created_at DESC, id DESC)`**：

  · 稳定排序保证跨页不重不漏（游标锚点唯一）；
  · 扫描上限保证"一个项目有 5000 个文件"时不会把内存打爆。

**注意扫描上限 ≠ 返回条数**：端点用 `platform.pagination` 在内存里
对这个窗口做游标分页。这样"扫描上限"是一个**保护性的天花板**
（超了就说明这个项目的研究资料规模已经超出设计假设，需要产品决策），
而不是一个会随文件数悄悄改变语义的参数。

## 三、`where effective` 是查询条件，不是约定

说明文档 R3 / R4 的落点：`effective = false` 的关联（解绑过）
**必须不出现在列表里**，且它的字节数**不计入配额**。

这一条由 `ProjectFileLink.effective.is_(True)` 这个**查询条件**固定住，
不靠"调用方记得过滤"。同一条判据在 WP12 的
`compute_effective_bytes` 里也有（那里是配额口径）——
两处必须一致，否则"列表里的文件"与"配额里的文件"会各说各话。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import ScalarSelect

from rednote.modules.brief.models import BriefVersion
from rednote.modules.files.models import (
    PURPOSE_RESEARCH,
    Blob,
    FileObject,
    ProjectFileLink,
)
from rednote.modules.parsing.models import ParseRun, SourceSegment
from rednote.modules.research.types import (
    EVIDENCE_USABLE_STATES,
    RESEARCH_FILE_SCAN_LIMIT,
)
from rednote.platform.errors import ApiError, ErrorCode

#: 研究的用途常量。**从 files 模块 import 而不是写字符串** ——
#: `FILE_PURPOSES` 是那份取值的唯一来源，抄一份就会在新增用途时漂移。
RESEARCH_PURPOSE: Final[str] = PURPOSE_RESEARCH


class ResearchFileRow:
    """一个研究文件 + 它的解析状态（**一次 join 的结果**）。

    **不用 dataclass(frozen=True)**：本类只在 repository → service
    之间传递一次，字段全部是内部使用（对外形状由 `schema.ResearchFileView`
    决定）。用一个轻量容器而不是 ORM 实例，是为了让 service 层
    **不持有 session 绑定的对象** —— 组装响应时若触发了延迟加载，
    会报 `DetachedInstanceError`（WP18 踩过）。

    字段全部在构造时填好（`__slots__` 保证没有"悄悄多出来的属性"）。
    """

    __slots__ = (
        "file_id",
        "original_name",
        "mime_type",
        "byte_size",
        "classification",
        "scan_status",
        "uploaded_by",
        "created_at",
        "purpose",
        "effective",
        "parse_run_id",
        "parse_state",
        "parser_version",
        "total_units",
        "parsed_units",
        "parse_errors",
        "segment_count",
    )

    def __init__(
        self,
        *,
        file_id: uuid.UUID,
        original_name: str,
        mime_type: str,
        byte_size: int,
        classification: str,
        scan_status: str,
        uploaded_by: uuid.UUID,
        created_at: datetime,
        purpose: str,
        effective: bool,
        parse_run_id: uuid.UUID | None,
        parse_state: str | None,
        parser_version: str | None,
        total_units: int | None,
        parsed_units: int | None,
        parse_errors: Sequence[Any],
        segment_count: int,
    ) -> None:
        self.file_id = file_id
        self.original_name = original_name
        self.mime_type = mime_type
        self.byte_size = byte_size
        self.classification = classification
        self.scan_status = scan_status
        self.uploaded_by = uploaded_by
        self.created_at = created_at
        self.purpose = purpose
        self.effective = effective
        self.parse_run_id = parse_run_id
        self.parse_state = parse_state
        self.parser_version = parser_version
        self.total_units = total_units
        self.parsed_units = parsed_units
        self.parse_errors = tuple(parse_errors or ())
        self.segment_count = segment_count


@dataclass(frozen=True)
class QuotaTotals:
    """配额口径的三项**全量**合计（与列表筛选无关）。

    只在 `quota_totals()` 里构造。它是一个 frozen dataclass 而不是
    dict：这三个数是同一个查询的三个投影，用 dict 会让"缺一个键"
    成为可能的运行时状态，而它们在语义上是不可分的（缺任何一个
    `meta.research` 都拼不出来）。
    """

    file_count: int
    listed_bytes: int
    usable_count: int


def _latest_run_id_subquery() -> ScalarSelect[uuid.UUID]:
    """"每个文件的最新一次解析运行"的相关子查询。

    **抽出来是因为它出现在两个地方**（文件列表的 `parse_run_id` 列、
    以及 `quota_totals` 的"可用证据数"计数）。内联两份的话，
    "最新"的判据（`created_at DESC, id DESC`）就有两个副本 ——
    而追加 `id` 是必须的（`created_at` 是事务时间戳，同一批插入的
    run 值相同，WP17 的 ISS-52 实测），漏掉一处会让两个数字
    对同一个文件给出不同的 run。

    `.correlate(FileObject)` 必须在子查询内部调用：外层查询的
    `FROM` 是 `ProjectFileLink`，而 `FileObject` 是通过 join 进来的 ——
    不显式关联会让 SQLAlchemy 把 `FileObject` 也拉进子查询的 FROM，
    变成一个**笛卡尔积**（不报错，只是数错）。
    """
    return (
        select(ParseRun.id)
        .where(ParseRun.file_id == FileObject.id)
        .order_by(ParseRun.created_at.desc(), ParseRun.id.desc())
        .limit(1)
        .correlate(FileObject)
        .scalar_subquery()
    )


class ResearchRepository:
    """研究模块的只读仓库。

    **不继承任何基类、不持有缓存**：它只持有 session（与其余包的
    repository 一致）。缓存会引入"这个实例对应哪个 session"的
    生命周期问题（WP20 的 dependencies.py 同一条说明）。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # ------------------------------------------------------------------
    # Brief（读 WP18 的既有数据）
    # ------------------------------------------------------------------

    def get_brief_version(self, version_id: uuid.UUID) -> BriefVersion | None:
        """按 id 取 Brief 版本（**不做项目校验** —— 那是 service 的职责）。

        与 WP18 的 `BriefRepository.get_version` 是同一个查询。
        本模块独立保留一份，是因为 service 层要判"这版属不属于本项目"，
        而 `BriefRepository` 的该方法不返回 `brief`（只有 `brief_id`），
        service 拿不到 `project_id` 就没法判。
        比对 `brief_id` → `brief.project_id` 需要两次查询，因此这里
        直接 join 出来一次拿到 —— 见 `get_brief_version_in_project`。
        """
        return self._session.get(BriefVersion, version_id)

    def brief_version_in_project(
        self, *, version_id: uuid.UUID, project_id: uuid.UUID
    ) -> BriefVersion | None:
        """按 (版本 id, 项目 id) 取版本；**不属于本项目时返回 None**。

        **为什么在 SQL 里判项目而不是取出来再比**：
        "取出来再比"会让一个跨项目探测请求先读出别人的版本行
        （哪怕不返回它），而这行数据可能被后续某个日志/异常详情带出去。
        在 SQL 里判则**根本读不到它** —— 与协议 §1.2 的
        "对象不存在**或当前用户无权知道它存在**"同一手法（WP09）。

        调用方把 `None` 转成 404（而不是 403）—— 同上，防存在性枚举。
        """
        from rednote.modules.brief.models import Brief

        stmt = (
            select(BriefVersion)
            .join(Brief, Brief.id == BriefVersion.brief_id)
            .where(
                BriefVersion.id == version_id,
                Brief.project_id == project_id,
            )
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def active_brief_version(self, *, project_id: uuid.UUID) -> BriefVersion | None:
        """本项目当前生效的 Brief 版本（`versionId` 不传时的默认）。

        **优先按 `briefs.active_version_id` 取**（它是权威），
        缺失时才退回"最新版本" —— 与 WP18 的 `_active_version`
        同一条判据。**不 import 那个私有函数**（下划线前缀是
        "模块内部"的约定，跨模块调它等于把"它可以随意改名"的承诺作废）。
        """
        from rednote.modules.brief.models import Brief

        brief = self._session.execute(
            select(Brief).where(Brief.project_id == project_id)
        ).scalar_one_or_none()
        if brief is None:
            return None
        if brief.active_version_id is not None:
            version = self._session.get(BriefVersion, brief.active_version_id)
            if version is not None and version.brief_id == brief.id:
                return version
        return self._session.execute(
            select(BriefVersion)
            .where(BriefVersion.brief_id == brief.id)
            .order_by(BriefVersion.created_at.desc(), BriefVersion.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def list_claims_for_version(self, version_id: uuid.UUID) -> list[Any]:
        """本版的结论（**按 `(field_path, claim_hash)` 排序**）。

        排序与 WP18 的 `BriefRepository.list_claims` 一致 ——
        **顺序必须确定**（`created_at` 是事务时间戳，同一批插入的值相同，
        WP17 的 ISS-52 实测三行读回来顺序随机）。派生关键词与缺口
        都建立在这个顺序上，因此两处必须同序，否则
        "同一个 Brief 版本派生出两批顺序不同的关键词"。
        """
        from rednote.modules.brief.models import BriefClaim

        return list(
            self._session.execute(
                select(BriefClaim)
                .where(BriefClaim.brief_version_id == version_id)
                .order_by(BriefClaim.field_path.asc(), BriefClaim.claim_hash.asc())
            )
            .scalars()
            .all()
        )

    # ------------------------------------------------------------------
    # 研究文件（本模块唯一多出来的查询）
    # ------------------------------------------------------------------

    def _research_files_stmt(self, *, project_id: uuid.UUID) -> Select[Any]:
        """研究文件列表的基查询（**供列表与计数共用**）。

        三层 join 各有理由：

          · `ProjectFileLink` → 用途与有效性（本包的口径：只认
            `purpose=RESEARCH` 且 `effective`）；
          · `FileObject` → 原文件名 / 分类 / 上传人；
          · `Blob` → 字节数与 mime（**字节数只在 blob 上有**，
            `file_objects` 不存大小 —— 同一份内容被两个文件引用时
            大小是同一个事实，WP12 刻意放在 blob 上）。

        解析状态用**相关子查询**而不是 join：
        `parse_runs` 对一个文件有多行（每次都追加），join 会需要
        `DISTINCT ON` 或窗口函数才能取"最新一次"。而
        `count(source_segments)` 又要按那次 run 来数 ——
        先取最新 run 的 id，再数它的片段，两步用两个标量子查询
        最省事且**语义无歧义**（"列表里显示的就是那一次运行的结果"）。
        """
        latest_run_id = _latest_run_id_subquery()
        segment_count = (
            select(func.count())
            .select_from(SourceSegment)
            .where(SourceSegment.parse_run_id == latest_run_id)
            .correlate(FileObject)
            .scalar_subquery()
        )
        return (
            select(
                FileObject.id,
                FileObject.original_name,
                Blob.mime_type,
                Blob.byte_size,
                FileObject.classification,
                Blob.scan_status,
                FileObject.uploaded_by,
                FileObject.created_at,
                ProjectFileLink.purpose,
                ProjectFileLink.effective,
                latest_run_id.label("parse_run_id"),
                ParseRun.state,
                ParseRun.parser_version,
                ParseRun.total_units,
                ParseRun.parsed_units,
                ParseRun.errors,
                segment_count.label("segment_count"),
            )
            .select_from(ProjectFileLink)
            .join(FileObject, FileObject.id == ProjectFileLink.file_id)
            .join(Blob, Blob.id == FileObject.blob_id)
            .outerjoin(ParseRun, ParseRun.id == latest_run_id)
            .where(
                ProjectFileLink.project_id == project_id,
                ProjectFileLink.purpose == RESEARCH_PURPOSE,
                # **`effective` 是查询条件而不是约定**（模块 docstring 第三节）。
                ProjectFileLink.effective.is_(True),
            )
            # **稳定排序**：`created_at` 是事务时间戳，同一批插入的行值相同
            # （WP17 的 ISS-52）→ 必须追加 `id` 才能保证游标锚点唯一。
            .order_by(FileObject.created_at.desc(), FileObject.id.desc())
            .limit(RESEARCH_FILE_SCAN_LIMIT)
        )

    def list_research_files(
        self,
        *,
        project_id: uuid.UUID,
        parse_states: Sequence[str] | None = None,
    ) -> list[ResearchFileRow]:
        """研究文件列表（含解析状态与片段数）。

        `parse_states` 在 **Python 侧**过滤而不是下推到 SQL，理由：
        它要筛的是"最新那次运行的 state"，而那个值来自相关子查询 ——
        `WHERE` 里再引用一次子查询会让 SQL 里出现三份相同子查询
        （可读性差且容易改漏一处）。窗口上限 1000 行，
        Python 侧过滤的代价可忽略。

        `state='NONE'`（从未解析）**由本方法合成**：SQL 里它是
        `parse_run_id IS NULL`，而"没解析过"不是一个可 join 出来的值。
        """
        rows = self._session.execute(
            self._research_files_stmt(project_id=project_id)
        ).all()
        result = [
            ResearchFileRow(
                file_id=row[0],
                original_name=row[1],
                mime_type=row[2],
                byte_size=int(row[3] or 0),
                classification=row[4],
                scan_status=row[5],
                uploaded_by=row[6],
                created_at=row[7],
                purpose=row[8],
                effective=bool(row[9]),
                parse_run_id=row[10],
                parse_state=row[11] if row[11] is not None else PARSE_STATE_NONE,
                parser_version=row[12],
                total_units=row[13],
                parsed_units=row[14],
                parse_errors=row[15] or (),
                segment_count=int(row[16] or 0),
            )
            for row in rows
        ]
        if parse_states:
            wanted = {state.strip().upper() for state in parse_states if state}
            result = [row for row in result if row.parse_state in wanted]
        return result

    def count_research_files(self, *, project_id: uuid.UUID) -> int:
        """本项目有效研究文件的**总数**（不受扫描上限影响）。

        与列表分开的第二个查询。理由：列表有
        `RESEARCH_FILE_SCAN_LIMIT = 1000` 的上限，因此
        `len(list)` **不等于**总数。若用 `len(list)` 当总数，
        "文件超过 1000 个"的项目会看到"共 1000 个"这个错误数字，
        而它看起来完全正常（ISS-71 的等式基数判据同型风险：
        一个看起来对的数字比一个明显错的数字更危险）。

        **新代码请优先用 `quota_totals`**：它一次给出计数与字节和，
        避免"两个字段各查一次、口径各写一遍"。本方法保留是因为
        它语义单一（只数个数），在只关心计数的调用点更直白。
        """
        stmt = (
            select(func.count())
            .select_from(ProjectFileLink)
            .where(
                ProjectFileLink.project_id == project_id,
                ProjectFileLink.purpose == RESEARCH_PURPOSE,
                ProjectFileLink.effective.is_(True),
            )
        )
        return int(self._session.execute(stmt).scalar_one())

    def quota_totals(self, *, project_id: uuid.UUID) -> QuotaTotals:
        """配额口径的三项合计：文件数 / 字节和 / **解析成功（可用证据）数**。

        ## 为什么必须与 `list_research_files` 分开（Code Review 修正）

        `list_research_files` 的 `parse_states` 参数**只该影响列表**。
        第一版实现让 service 从（已被过滤的）`rows` 算 `listedBytes`
        与 `usableEvidenceCount`，于是带 `?status=SUCCEEDED` 请求时
        `meta.research` 内部口径不一致，且 `reconciled` 假报 `False`
        —— 前端读成"账面与明细不符"的告警。**假告警比没有告警更伤信任**。

        ## 为什么"可用证据数"要在 SQL 里数

        `evidence_usable`（只认 `SUCCEEDED` / `PARTIAL`）是**业务判据**，
        但它对应的是 `parse_runs.state` 的一个集合。放在 SQL 里用
        `in_` 表达，与放在 Python 里 filter 是同一事实的两种写法 ——
        选 SQL 是因为这里要的是**全量计数**，拉全量行回内存只为了
        数一遍（还要受扫描上限）是白费。

        判据与 `service.evidence_usable` **必须同源**：这里用
        `EVIDENCE_USABLE_STATES` 常量（定义在 `types.py`），
        而不是手写 `('SUCCEEDED', 'PARTIAL')` —— 两处各写一份，
        将来加一个"部分可用"的状态时必然只改一处。
        """
        usable_states = sorted(EVIDENCE_USABLE_STATES)
        stmt = (
            select(
                func.count(),
                func.coalesce(func.sum(Blob.byte_size), 0),
                func.count(ParseRun.id).filter(ParseRun.state.in_(usable_states)),
            )
            .select_from(ProjectFileLink)
            .join(FileObject, FileObject.id == ProjectFileLink.file_id)
            .join(Blob, Blob.id == FileObject.blob_id)
            .outerjoin(ParseRun, ParseRun.id == _latest_run_id_subquery())
            .where(
                ProjectFileLink.project_id == project_id,
                ProjectFileLink.purpose == RESEARCH_PURPOSE,
                ProjectFileLink.effective.is_(True),
            )
        )
        count, total_bytes, usable = self._session.execute(stmt).one()
        return QuotaTotals(
            file_count=int(count),
            listed_bytes=int(total_bytes or 0),
            usable_count=int(usable or 0),
        )

    def resolve_project_or_404(self, project_id: uuid.UUID) -> None:
        """项目不存在 → 404。

        **为什么不直接用 `projects.dependencies.ProjectAccessDep`**：
        那是 FastAPI 依赖（它从请求里取 `CurrentUser`），在 service 与
        证据脚本里都调不到。而本方法只是"表里有没有这一行"的检查，
        权限判定仍在依赖层（两件事分开，见 WP09 的
        `resolve_project_access` 与它的说明）。
        与 WP18 的 `BriefRepository.project_exists` 是同一个查询 ——
        这里返回 404 而不是 bool，因为本包两个端点都是读取，
        "项目不存在"必须是一个错误而不是一个空列表。
        """
        from rednote.modules.projects.models import Project

        exists = self._session.execute(
            select(Project.id).where(Project.id == project_id)
        ).scalar_one_or_none()
        if exists is None:
            raise ApiError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "项目不存在或无权访问。",
                details={"projectId": str(project_id)},
            )


#: 本层新增的解析状态取值：从未发起过解析。
#:
#: **它不属于 `parse_runs` 的 `PARSE_STATES`** —— 那里没有"从未解析"
#: 这个状态（一行都没有的状态无法存在于一张表里）。
#: 放在本模块而不是 `research/types.py`：它是**读取视角**的合成值，
#: 不是本包的领域取值（`types.py` 里放的是"本包定义的取值"）。
#: WP17 的 `latest_summary` 对同一件事返回 `None`；
#: 本层必须给它一个**可筛选的名字**（协议 §5 的 `status` 参数），
#: 因此用显式取值。两处口径的差异是刻意的，已登记为待见项。
PARSE_STATE_NONE: Final[str] = "NONE"


__all__ = [
    "PARSE_STATE_NONE",
    "RESEARCH_PURPOSE",
    "ResearchFileRow",
    "ResearchRepository",
]