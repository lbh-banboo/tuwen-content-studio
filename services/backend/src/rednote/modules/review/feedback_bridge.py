"""`review` → `feedback` 的唯一接缝（WP30）：把阻断项喂进 WP29 的既有审核闸。

## 本包**不新建**第二道"关键事实问题未解决不能通过"

那条产品规则**已经**落在 WP29 的服务层（`feedback/service.py:872-888`：
`decide_review` 对 `APPROVED` 前置查 `severity='BLOCKING' AND state NOT IN
('RESOLVED','REJECTED')`，命中抛 409 `UNRESOLVED_BLOCKING_FEEDBACK`）。
本包只负责**产出喂给它的数据**：

```
WP30 run_self_check()
  → 规则命中 severity=BLOCKING
  → 本桥接 → feedback.service.create_feedback(author_kind='AI', ...)
  → 落 content_feedback_items（state 默认 OPEN）+ 同事务首行事件
  → finding.feedback_item_id 回指
WP29（已存在，一行不改）POST /contents/{id}/reviews decision=APPROVED
  → 409 UNRESOLVED_BLOCKING_FEEDBACK
```

**为什么必须这样接**：那句话的消费点有**两个**（逐条"标记已通过" +
WP31 扩产关卡「无待处理阻断意见」）。若 WP30 自己再判一次，全系统就有
三份"什么叫未解决的阻断"，而三份之间的分歧**不会报错** —— 只表现为
"自检说能过、审核说不能过"。

`author_kind='AI'` 这条路是 WP29 **设计时就留好的**（`WP29-...说明.md:149`
原文「AI 自检（WP30，未来）｜ 服务层 `authorKind=AI`；**不经过 HTTP 端点**」）。
本桥接就是兑现那句话 —— 它调的是 service 函数，不是 HTTP 端点。

## 本包只**建**反馈，不**改**反馈

`advance_feedback` 要 `If-Match`（`feedback/api.py`），而机器持有 ETag 去抢
人工工作区的锁没有业务收益。因此：**规则下一轮不再命中时，反馈不会自动
关闭**，由人处置。这一条必须写进说明文档的已知取舍 —— 否则运营会以为
"改好了意见会自动消失"。

## 为什么去重判断不在这里

去重要读"同内容同 rule_code 同锚点、未解决的 AI 反馈是否已存在"，
而那个查询跨本包三张表 + WP29 一张表 ⇒ 读在 `repository.py`、判在
`service.py`。桥接层**只转发**，否则它就成了第二个业务判断点。

## 为什么不 catch `ApiError`

`create_feedback` 的 422 带 `details.reason` / `details.field`
（机器可判）。桥接层包一层会把这些键丢掉，端点只剩一句人话。
"""

from __future__ import annotations

import uuid
from typing import Any

from rednote.modules.feedback.service import create_feedback

#: 与 `feedback/types.ACTOR_KIND_AI` 同值（各写一份 + parity 测试）。
#: 平级模块不可互 import；一致性由 `test_review_wp30.py` 对账 ——
#: 写错的症状是 `create_feedback` 抛 422 `FEEDBACK_AUTHOR_KIND_INVALID`，
#: 即"自检跑完了但阻断项一条都没落成反馈"⇒ 审核闸放行（静默）。
AI_AUTHOR_KIND = "AI"


def create_ai_feedback(
    feedback_repo: Any,
    *,
    project_id: uuid.UUID,
    content_id: uuid.UUID,
    observed_version_id: uuid.UUID,
    anchor_kind: str,
    category: str,
    severity: str,
    scope: str,
    body: str,
    paragraph_ref: str | None = None,
    media_item_id: uuid.UUID | None = None,
    created_by: uuid.UUID | None = None,
) -> Any:
    """以 **AI 身份**新建一条反馈，**逐字转发** `feedback.service.create_feedback`。

    `author_kind` 由本函数写死成 AI（而不是做成参数）：本包的所有写入都是
    机器判定的结果，让调用方自报身份会让审计面
    （`content_feedback_events.actor_kind`）失真。

    `created_by` 仍然传入 —— 它记的是"**谁触发了这一轮自检**"，与
    `author_kind='AI'`（"这条意见是机器提的"）是两件事：前者让"谁让机器
    跑的"可追溯，后者让"这条意见不是人写的"可区分。
    `feedback.service` 把它同时用作首行事件的 `actor_id`。
    """
    return create_feedback(
        feedback_repo,
        project_id=project_id,
        content_id=content_id,
        observed_version_id=observed_version_id,
        anchor_kind=anchor_kind,
        category=category,
        severity=severity,
        scope=scope,
        # 本包唯一的写入身份。见 docstring。
        author_kind=AI_AUTHOR_KIND,
        body=body,
        paragraph_ref=paragraph_ref,
        media_item_id=media_item_id,
        created_by=created_by,
    )


__all__ = ["AI_AUTHOR_KIND", "create_ai_feedback"]
