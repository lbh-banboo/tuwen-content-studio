"""`delivery` → `exports` 的唯一接缝（WP36）。

## 为什么必须有一个桥接文件

`exports` 与 `delivery` 是**平级**业务模块，本仓纪律是「平级模块不可互 import
对方的 ORM 模型 / 写入原语」（`exports/types.py:3-4`、`exports/repository.py:1-4`）。
而 `modules/exports/**` 还被 `docs/m4/M4并行开发接缝冻结.md` §7.1 第 24 行列为
**禁改**（WP35 续作包独占）。本包需要从那边拿两样东西，走两条不同的路：

| 要什么 | 怎么拿 | 为什么 |
|---|---|---|
| `export_items` 的六列（冻结字段 + 归属） | **裸 SQL**（本文件内） | 列名就是对方的契约：写错当场 `UndefinedColumn`，不静默。比 import 一个会被改签名的函数更稳 |
| 「**当前**的资格指纹」 | **转发** `exports.service.current_eligibility_hash` | 重算需要"冻结取数口径"（投影优先 + `DELIVERY`/`READY`/`MAX(attempt_no)`）与"哈希口径"两样，两样都是 `exports` 的私有实现。本包再写一份 ⇒ 两份口径任一侧改动即**静默分叉**，而分叉的表现是**资格复验恒通过或恒失败，没有报警**（OP-197「正文切段三处实现」同族） |

该只读函数由主 agent 在 WP36 开工**之前**独立提交（仲裁项① 处置 A），签名已在
`docs/m3/M3并行开发接缝冻结.md` §5.13 登记。**本文件只转发，不重新实现。**

## 为什么把 `ExportError` 换成本包的异常

`current_eligibility_hash` 继承 `freeze_content` 的拒绝路径：内容一版正文都没有、
或有配图位却没有可用正式图时抛 `ExportError`。那在**导出**语境里是"不能导"，
在**交付**语境里是"算不出当前指纹，因此无法判断内容是否已改稿"——**不是**
"资格已失效"。两者的处置不同（前者该让用户去出图，后者该让用户重新导出），
所以这里换成 `EligibilityUnavailable` 并**保留原因与原文**，由 `service` 决定怎么处置。

**不是吞异常**：既不返回 `None`，也不降级成"复验通过"。
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from rednote.modules.delivery.types import FrozenExportItem

#: ⚠️ 只 import `exports` 的 **service 层只读函数与异常**，不 import 它的
#: ORM 模型（`exports.models.ExportItem`）—— 取数一律裸 SQL。
from rednote.modules.exports.repository import ExportRepository
from rednote.modules.exports.service import (
    ExportError,
    current_eligibility_hash as _exports_current_eligibility_hash,
)


class EligibilityUnavailable(Exception):
    """当前资格指纹**算不出来**（内容没有正文版本 / 配图位缺正式图）。

    与"指纹算出来了但与交付时不一致"是两件事：后者是 `ELIGIBILITY_STALE`。
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        #: 上游 `ExportError` 的原因码，原样带出（排障时要知道是缺版本还是缺图）。
        self.upstream_reason = reason


_EXPORT_ITEM_SQL = text(
    "SELECT ei.id, ei.project_id, ei.content_item_id, ei.version_node_id,"
    " ei.eligibility_hash, ei.export_group_id"
    " FROM export_items ei WHERE ei.id = :export_item_id"
)


def read_export_item(
    session: Session, *, export_item_id: uuid.UUID
) -> FrozenExportItem | None:
    """读一个导出行的冻结字段。不存在 ⇒ `None`（归属判定由调用方做）。

    **只读六列**：三个核心冻结字段 + `project_id`（归属）+ `export_group_id`
    （排障时能回到"哪一次导出"）。刻意不读 `zip_sha256` / `artifact_file_id`
    —— 本包零打包、零下载代码，读了也没有消费者。
    """
    row = session.execute(_EXPORT_ITEM_SQL, {"export_item_id": export_item_id}).first()
    if row is None:
        return None
    return FrozenExportItem(
        export_item_id=row[0],
        project_id=row[1],
        content_item_id=row[2],
        version_node_id=row[3],
        eligibility_hash=row[4],
        export_group_id=row[5],
    )


def current_eligibility_hash(session: Session, *, content_item_id: uuid.UUID) -> str:
    """按**当前**正文版本与正式图重算资格指纹，**逐字转发**只读接缝。

    算不出来 ⇒ `EligibilityUnavailable`（见模块 docstring）。
    """
    try:
        return _exports_current_eligibility_hash(
            ExportRepository(session), content_item_id=content_item_id
        )
    except ExportError as exc:
        raise EligibilityUnavailable(str(exc), reason=exc.reason) from exc


__all__ = [
    "EligibilityUnavailable",
    "current_eligibility_hash",
    "read_export_item",
]
