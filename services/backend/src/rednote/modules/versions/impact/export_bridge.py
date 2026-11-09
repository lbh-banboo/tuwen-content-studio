"""`impact` → `exports` 的接缝（WP34，**只读**）：重算资格快照。

**`impact/**` 内唯一 import `rednote.modules.exports` 的文件**。只做转发 + `ExportError → Unavailable`
的语义换位（保留 reason 与原文，不吞、不降级）。

接缝是 `exports.service.current_eligibility_snapshot`（S2）：同一次读取返回
`(version_node_id, eligibility_hash)`。本包**不**自己拼「冻结内容 + 指纹」，也不另写哈希或
「当前版本」读法（接缝冻结 §5.13）。
"""

from __future__ import annotations

import uuid
from typing import Any

from rednote.modules.exports.repository import ExportRepository
from rednote.modules.exports.service import ExportError, current_eligibility_snapshot
from rednote.modules.versions.impact.types import Observed, Unavailable


def observe(session: Any, content_item_id: uuid.UUID) -> Observed | Unavailable:
    try:
        version_id, fingerprint = current_eligibility_snapshot(
            ExportRepository(session), content_item_id=content_item_id
        )
    except ExportError as exc:
        return Unavailable(upstream_reason=exc.reason, message=str(exc))
    return Observed(version_id=version_id, eligibility_hash=fingerprint)


__all__ = ["observe"]
