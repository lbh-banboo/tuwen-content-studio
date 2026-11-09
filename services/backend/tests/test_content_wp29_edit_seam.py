"""WP29 的"人工编辑 = 追加一版 + 412 并发冲突"接缝（`service.append_edited_version`）。

## 本文件为什么单独成文件（而不是追加进 `test_content_wp26.py`）

`test_content_wp26.py` 已有 90+ 条用例且仍在维护 WP26 的判据；
本次新增的接缝函数是为 WP29（`modules/feedback/**`）准备的，它的
**失败语义与 WP26 不同**（412 而非 409，见 `service.append_edited_version`
的 docstring「为什么本函数抛 412 而 WP26 既有路径抛 409」）。
两类判据放在同一个文件里会让"哪个工作包守哪条语义"变得不可读；
新文件让 WP29 落地时只改它自己的测试，不翻 WP26 的牌。

## 夹具从哪里来

本文件**直接导入** `test_content_wp26` 的夹具（`_ensure_own_tables` /
`_clean_own_rows` / `env` / `_seed_valid_content` / `_raw`），
与 `test_media_wp28_service.py` 导入 `test_media_wp27` 的写法同款：
autouse 夹具导入后在本模块同样生效。重写一份建表逻辑会让两份
"怎么搭测试环境"在将来分叉（新增一张表时一边建了一边没建），
而分叉的表现是**同样的代码在一个文件里绿、另一个文件里红**。

## 三条判据对应的三条性质

| 判据 | 性质 |
|---|---|
| 成功追加一版且投影前进 | 编辑与采纳同形：`content_versions` 追加、`content_items` 的 `current_*` 指向新版、`lock_version` 前进 |
| 过期锁 ⇒ 412 + 零写入 | 并发冲突走 `platform.concurrency.version_conflict`（**不是** `ContentGenerationError`），且失败**先于**任何写入 |
| 正文段数与绑定数不一致 ⇒ `SchemaViolation` | R1/R4 的防线在编辑路径上**不旁路**（见 `append_edited_version` docstring） |

## 一个与任务书不符的实测修正

任务书写"段数与绑定不一致 ⇒ `ContentGenerationError`"。**实测** `schemas.validate_bindings`
抛的是 `SchemaViolation`（`ValueError` 族，携带 `.reason` / `.field`），
与 `ContentGenerationError` 不是同一类型。本文件按**磁盘上的实际签名**断言：
`SchemaViolation(reason=REASON_OUTPUT_FIELD_VALUE)`。HTTP 层 422 由 WP29
的端点自行映射（content 模块既有的 `api.py` 只认 `ContentGenerationError`），
本文件不起 HTTP 客户端 —— 任务书已明确那条约束。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

# 夹具与铺底助手直接导入（autouse 夹具导入后在本模块同样生效）。
from test_content_wp26 import (
    ContentEnv,
    _clean_own_rows,  # noqa: F401
    _ensure_own_tables,  # noqa: F401
    _raw,
    _seed_valid_content,
    env,  # noqa: F401
)

from rednote.modules.baseline.repository import BaselineRepository
from rednote.modules.content import service as c_service
from rednote.modules.content.repository import ContentRepository
from rednote.modules.content.schemas import SchemaViolation
from rednote.modules.content.types import (
    REASON_EDIT_LOCK_CONFLICT,
    REASON_OUTPUT_FIELD_VALUE,
)
from rednote.modules.versions.repository import VersionRepository
from rednote.platform.database import session_scope
from rednote.platform.errors import ApiError, ErrorCode


# ===========================================================================
# 铺底助手
# ===========================================================================


def _jsonb_bindings(paragraph_count: int) -> str:
    """按段数生成一份**合法**的 `bindings` JSONB 字面量（全部 claimId=null + 理由）。

    用 `json.dumps` 而不是手拼字符串：手拼 JSON 嵌在 SQL 字面量里，引号
    转义错一次的表现是**插入失败**（看起来像被测代码坏了），而不是
    "铺底写错"。这里一次生成，测试里不再出现 JSON 文本。
    """
    payload = [
        {
            "paragraphIndex": index,
            "claimId": None,
            "reason": "本段无对应事实",
            "text": f"第 {index + 1} 段",
        }
        for index in range(paragraph_count)
    ]
    return "'" + json.dumps(payload, ensure_ascii=False).replace("'", "''") + "'::jsonb"


def _seed_editable(
    env: ContentEnv, *, paragraphs: int = 2
) -> tuple[uuid.UUID, uuid.UUID]:
    """造"一条内容 + 一版**段数与绑定匹配**的版本"（编辑判据的共同底料）。

    为什么绑定必须匹配段数：本包对 bindings 的校验规则之一就是
    "段号齐全且不重"（`schemas.validate_bindings` 的规则一）。
    铺底料若不匹配，连**成功路径**都过不去 —— 而我们要断言的恰好是
    "成功路径能走通"与"段数不匹配才被拒"这两件事。
    """
    body = "\n\n".join(f"第 {index + 1} 段：内容。" for index in range(paragraphs))
    return _seed_valid_content(env, body=body, bindings=_jsonb_bindings(paragraphs))


def _append(
    env: ContentEnv,
    content_id: uuid.UUID,
    **kwargs: Any,
) -> Any:
    """一次事务里调 `append_edited_version`。

    三个仓库**共一个 session**（`dependencies.py` 已论证：节点落在 A
    session、版本落在 B session ⇒ 反向触发器报错）。`baseline_repo`
    是必填的 —— 它给"判据域"提供真值来源，而不是一个可传 None 的旁路。
    """
    with session_scope(env.settings) as session:
        return c_service.append_edited_version(
            ContentRepository(session),
            version_repo=VersionRepository(session),
            baseline_repo=BaselineRepository(session),
            content_id=content_id,
            **kwargs,
        )


def _content_row(env: ContentEnv, content_id: uuid.UUID) -> tuple[Any, ...]:
    rows = _raw(
        env,
        "SELECT current_version_no, lock_version, current_version_id, title"
        " FROM content_items WHERE id = :c",
        {"c": content_id},
    )
    assert len(rows) == 1
    return rows[0]


def _version_count(env: ContentEnv, content_id: uuid.UUID) -> int:
    rows = _raw(
        env,
        "SELECT count(*) FROM content_versions WHERE content_item_id = :c",
        {"c": content_id},
    )
    return int(rows[0][0])


# ===========================================================================
# 判据
# ===========================================================================


def test_append_edited_version_advances_projection_on_success(
    env: ContentEnv,
) -> None:
    """★ 编辑成功 = **追加一版**（不是 UPDATE），且投影随之前进。

    与 `test_adopt_title_candidate_appends_version` 同形：版本数 +1，
    `content_items.current_version_no` / `lock_version` 前进，
    `current_version_id` 指向新版。上一版原样保留（不可变）。
    """
    content_id, first_version_id = _seed_editable(env, paragraphs=2)
    before = _content_row(env, content_id)
    assert before[0] == 0 and before[1] == 0

    summary = _append(
        env,
        content_id,
        expected_lock_version=0,
        title="人工改过的标题",
    )

    assert summary.version_no == 2, "编辑应当是第 2 版（追加，不是修改第 1 版）"
    assert summary.title == "人工改过的标题"

    after = _content_row(env, content_id)
    assert after[0] == 2, "current_version_no 没有前进"
    assert after[1] == 1, "lock_version 没有前进（0 → 1）"
    assert after[2] == summary.id, "current_version_id 没有指向新版"
    assert after[3] == "人工改过的标题"

    # 上一版原样保留（不可变）；本函数不碰 feedback 表。
    old_row = _raw(
        env,
        "SELECT title FROM content_versions WHERE id = :v",
        {"v": first_version_id},
    )
    assert old_row[0][0] == "标题", "第 1 版被改了 —— 它不是不可变的吗"
    assert _version_count(env, content_id) == 2, "应当恰好两条版本行"


def test_append_edited_version_rejects_stale_lock_version_with_412_and_zero_write(
    env: ContentEnv,
) -> None:
    """★ 过期锁 ⇒ **412 `VERSION_CONFLICT`**，且**零写入**。

    两个断言缺一不可：
    - `code is ErrorCode.VERSION_CONFLICT` 证明走的是
      `platform.concurrency.version_conflict`（不是 `ContentGenerationError`，
      也不是 `REASON_PROJECTION_CONFLICT` → 409）；
    - 版本行数不变 + `lock_version` 不变证明"过期"在**任何写入之前**被拒，
      不是靠事后回滚兜住（回滚仍会留下"曾经写过"的痕迹，而且
      `content_versions` 是不可变表，写进去就删不掉）。
    """
    content_id, _ = _seed_editable(env, paragraphs=2)
    before_versions = _version_count(env, content_id)

    with pytest.raises(ApiError) as caught:
        _append(
            env,
            content_id,
            # 库里锁版本是 0，这里故意给 7（"读旧版编辑，提交时已有人改过"）。
            expected_lock_version=7,
            title="过期提交的标题",
        )

    assert caught.value.code is ErrorCode.VERSION_CONFLICT, (
        "应当走 `platform.concurrency.version_conflict`（412），"
        "不是 `ContentGenerationError`（409/422）"
    )
    assert caught.value.details["reason"] == REASON_EDIT_LOCK_CONFLICT
    assert "currentEtag" in caught.value.details, (
        "协议 §1.2：412 必须回带 currentEtag，前端才能不发第二次 GET 就重试"
    )

    # 零写入：版本行数与锁版本都**没变**。
    assert _version_count(env, content_id) == before_versions, (
        "过期锁下多了一条 content_versions 行 —— 写入先于 412 发生了"
    )
    after = _content_row(env, content_id)
    assert after[1] == 0, "lock_version 被改了（该请求应该什么都没写）"
    assert after[0] == 0, "current_version_no 被改了（该请求应该什么都没写）"


def test_append_edited_version_rejects_paragraph_count_binding_mismatch(
    env: ContentEnv,
) -> None:
    """★ 正文段数变了而绑定数没变 ⇒ `SchemaViolation`（R1/R4 在编辑路径上**不旁路**）。

    改正文从 2 段到 3 段，但沿用上一版的 2 条绑定 —— `validate_bindings`
    的规则一（"段号齐全"）必须拦下。否则编辑路径会让"绑定"退化成
    "看起来有"的状态：段数变了，绑定还指着旧坐标，AQ-01 盲审对不上段。

    **为什么断言 `SchemaViolation` 而不是 `ContentGenerationError`**：
    `validate_bindings` 抛的是前者（`ValueError` 族，携带 `.reason` / `.field`）。
    这是任务书之外的一次实测修正 —— 见本文件 docstring「一个与任务书不符的实测修正」。
    """
    content_id, _ = _seed_editable(env, paragraphs=2)

    with pytest.raises(SchemaViolation) as caught:
        _append(
            env,
            content_id,
            expected_lock_version=0,
            body="第一段：内容。\n\n第二段：内容。\n\n第三段：新增的段。",
            # `bindings` 不给 ⇒ 沿用上一版的 2 条，与 3 段不匹配。
        )

    assert caught.value.reason == REASON_OUTPUT_FIELD_VALUE
    assert caught.value.field == "paragraphIndex", (
        "拒绝应当落在'段号不齐'这条规则上，不是别的字段"
    )

    # 校验在注册节点/插版本**之前**：失败必须是零写入。
    assert _version_count(env, content_id) == 1
    after = _content_row(env, content_id)
    assert after[1] == 0 and after[0] == 0
