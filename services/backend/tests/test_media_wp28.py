"""WP28 `media` 包 Live 形态的判据落点。

本文件按阶段 4 的步骤增量扩充：

| 步 | 层 | 是否需要数据库 |
|---|---|---|
| 4.1 | 张数口径 / 受控元组 / 常量 | 否 |

## 为什么 4.1 的判据值得单独成文件

X2a（老大 2026-10-14）让"张数"有了**两条路径**：静态经 `images_for`，
Live 经 `calls_for(..., form=LIVE)` 直乘。`R9` 的担忧是两条路径各自演化，
同一个非法输入一边放行一边拒绝。下面的用例把"两条路径行为一致"钉死。

纯函数层**不依赖数据库**，可在共享库被并行会话占用时照常运行。
"""

from __future__ import annotations

import pytest

from rednote.modules.media import types as media_types
from rednote.modules.media.types import (
    COMPONENT_ROLES,
    IMAGES_PER_CONTENT,
    LIVE_IMAGES_PER_CONTENT,
    MAX_CONTENT_COUNT,
    MEDIA_FORM_LIVE,
    MEDIA_FORM_STATIC,
    MEDIA_FORMS,
    TASK_KINDS,
    TASK_STATIC_IMAGE,
    VERIFICATION_SYNTHETIC,
    VERIFICATIONS,
    calls_for,
    images_for,
    images_per_content,
)

# ---------------------------------------------------------------------------
# 张数口径（X2a）
# ---------------------------------------------------------------------------


def test_static_default_semantics_unchanged() -> None:
    """既有 30 处无参调用的语义不变：默认仍是静态 3 张。"""
    assert images_per_content() == IMAGES_PER_CONTENT == 3
    assert images_per_content() == images_per_content(MEDIA_FORM_STATIC)
    # 值同源（原有断言的形态保留）
    assert images_per_content() is IMAGES_PER_CONTENT


def test_live_images_per_content_is_two() -> None:
    """X2a：Live = STILL + PREVIEW = 2 张；动态段不按张计（OP-206）。"""
    assert images_per_content(MEDIA_FORM_LIVE) == LIVE_IMAGES_PER_CONTENT == 2


@pytest.mark.parametrize("bad", ["live", "Live", "", " STATIC", "VIDEO", None, 1])
def test_images_per_content_unknown_form_fails_closed(bad: object) -> None:
    """未知形态不得静默回落到静态：回落的表现是 Live 按 3 张预占，账面差一笔且不报错。"""
    with pytest.raises(ValueError):
        images_per_content(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("count", [0, 1, 2, 7, MAX_CONTENT_COUNT])
def test_static_path_unchanged_by_form_argument(count: int) -> None:
    """R9（一）：给 `form=STATIC` 与不给，结果完全一致，且都等于 `images_for`。"""
    expected = count * IMAGES_PER_CONTENT
    assert images_for(count) == expected
    assert calls_for(TASK_STATIC_IMAGE, count) == expected
    assert calls_for(TASK_STATIC_IMAGE, count, form=MEDIA_FORM_STATIC) == expected


@pytest.mark.parametrize("count", [0, 1, 2, 7, MAX_CONTENT_COUNT])
def test_live_path_is_count_times_two(count: int) -> None:
    assert calls_for(TASK_STATIC_IMAGE, count, form=MEDIA_FORM_LIVE) == count * 2


@pytest.mark.parametrize(
    "bad_count",
    [-1, MAX_CONTENT_COUNT + 1, True, False, "3", 2.0, None],
)
@pytest.mark.parametrize("form", MEDIA_FORMS)
def test_both_paths_reject_the_same_invalid_counts(
    form: str, bad_count: object
) -> None:
    """R9（二）：同一个非法条数在两条路径上**同样被拒**。

    若 Live 路径把校验抄了一份而与静态各自演化，这里会出现"一边过一边拒"。
    """
    with pytest.raises(ValueError):
        calls_for(TASK_STATIC_IMAGE, bad_count, form=form)  # type: ignore[arg-type]


def test_rejection_messages_match_between_paths() -> None:
    """两条路径的拒绝理由同文：校验只有一份实现，不是抄两份。"""
    for bad in (-1, MAX_CONTENT_COUNT + 1, True):
        with pytest.raises(ValueError) as static_exc:
            calls_for(TASK_STATIC_IMAGE, bad, form=MEDIA_FORM_STATIC)  # type: ignore[arg-type]
        with pytest.raises(ValueError) as live_exc:
            calls_for(TASK_STATIC_IMAGE, bad, form=MEDIA_FORM_LIVE)  # type: ignore[arg-type]
        assert str(static_exc.value) == str(live_exc.value)


@pytest.mark.parametrize("bad", ["live", "", None, "STATIC_IMAGE"])
def test_calls_for_unknown_form_fails_closed(bad: object) -> None:
    with pytest.raises(ValueError):
        calls_for(TASK_STATIC_IMAGE, 1, form=bad)  # type: ignore[arg-type]


def test_calls_for_unknown_task_kind_still_rejected() -> None:
    """Live 不新增 task_kind（D3）：未知 kind 仍被拒，`LIVE` 不是 task_kind。"""
    assert TASK_KINDS == frozenset({TASK_STATIC_IMAGE})
    for bad in ("LIVE", "MEDIA_LIVE", "NOT_A_KIND"):
        with pytest.raises(ValueError):
            calls_for(bad, 1)
        with pytest.raises(ValueError):
            calls_for(bad, 1, form=MEDIA_FORM_LIVE)


def test_form_is_keyword_only() -> None:
    """`form` 仅关键字：位置参数顺序写反（`calls_for(kind, "LIVE", n)`）必须直接报错。"""
    with pytest.raises(TypeError):
        calls_for(TASK_STATIC_IMAGE, 1, MEDIA_FORM_LIVE)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 受控元组与红线常量
# ---------------------------------------------------------------------------


def test_controlled_tuples_exact_values() -> None:
    """受控取值钉死字面量：它们是 CHECK 约束与对外契约，改名等于契约变更。"""
    assert MEDIA_FORMS == ("STATIC", "LIVE")
    assert COMPONENT_ROLES == ("STILL", "PAIRED_MOTION", "MANIFEST", "PREVIEW")


def test_no_original_component_role() -> None:
    """Q2（老大 2026-10-14）：不设 `ORIGINAL`。原图即 `image_file_id`，再设 = 同一事实两个落点。"""
    assert "ORIGINAL" not in COMPONENT_ROLES


def test_verification_only_synthetic() -> None:
    """红线：没有真机，产出只能标 SYNTHETIC，不存在任何"已验证"取值。"""
    assert VERIFICATIONS == (VERIFICATION_SYNTHETIC,) == ("SYNTHETIC",)
    assert not any("VERIFIED" in v for v in VERIFICATIONS)


def test_live_profile_state_only_placeholder() -> None:
    """红线：占位不得当 VERIFIED。"""
    assert media_types.LIVE_PROFILE_STATES == ("PLACEHOLDER",)
    assert "VERIFIED" not in media_types.LIVE_PROFILE_STATES


@pytest.mark.parametrize(
    ("values", "sql"),
    [
        (media_types.MEDIA_FORMS, media_types.MEDIA_FORM_LIST_SQL),
        (media_types.COMPONENT_ROLES, media_types.COMPONENT_ROLE_LIST_SQL),
        (media_types.VERIFICATIONS, media_types.VERIFICATION_LIST_SQL),
        (media_types.LIVE_PROFILE_STATES, media_types.LIVE_PROFILE_STATE_LIST_SQL),
    ],
)
def test_check_sql_fragments_derive_from_tuples(
    values: tuple[str, ...], sql: str
) -> None:
    """CHECK 的 SQL 片段与元组同源：`IN (...)` 里不会比元组多或少一项。"""
    assert sql == ", ".join(f"'{v}'" for v in values)


def test_new_names_are_public() -> None:
    """新增常量都登记进 `__all__`（`check_all_exports` 的 A1 方向；漏登记的代价是下一个消费者就地手写字面量）。"""
    for name in (
        "MEDIA_FORM_STATIC",
        "MEDIA_FORM_LIVE",
        "MEDIA_FORMS",
        "MEDIA_FORM_LIST_SQL",
        "COMPONENT_ROLES",
        "COMPONENT_ROLE_LIST_SQL",
        "VERIFICATION_SYNTHETIC",
        "VERIFICATIONS",
        "VERIFICATION_LIST_SQL",
        "LIVE_PROFILE_PLACEHOLDER",
        "LIVE_PROFILE_STATES",
        "LIVE_PROFILE_STATE_LIST_SQL",
        "LIVE_IMAGES_PER_CONTENT",
    ):
        assert name in media_types.__all__, name
