"""`review` → `media` 的唯一接缝（WP30）。**本包零切段实现。**

## 为什么这件事对本包比对 WP29 更要紧

`MEDIA_PARAGRAPH_REF_STALE` 这条规则比较的是两个集合：

```
{media_items.paragraph_ref}   ⊆?   {当前正文切出来的段 ref}
```

左边由 media 在生成图片时用 `media/schemas.paragraph_refs_for_body` 写入。
若本包自己按空行切一次正文（`body.split("\\n\\n")` 或 `.splitlines()`），
右边就来自**另一套**切段实现 —— 于是这条规则不是偶尔误报，而是
**系统性恒红**：每张图都会被判成"坐标失配"，运营看到的是"媒体一致性
全是红的"，而真正的失配被噪声埋掉。

反方向同样坏：若两套实现恰好在常见输入上一致，分叉只在 emoji + 段内换行
这类小红书常态输入上出现（契约 §1.4 实测 11 场景里 4 个分叉），
那就是**静默**误报 —— 两套实现各自的测试都会过。

接缝冻结 §5.1 点名切段只许三处实现（`content` / `knowledge` / `media`），
这是 OP-197 的第二次现身。判据载体：
`tools/quality/check_paragraph_split_parity.py` 的 `_IMPL_FILES` 必须
**仍是 3 个** —— 本包落地后它不该变成 4 个。

两个转发目标都是**纯函数**（无 IO、无 session），所以桥接层不需要任何
上下文，也没有可以"顺手加一点逻辑"的空间。
"""

from __future__ import annotations

from rednote.modules.media.schemas import (
    paragraph_refs_for_body,
    validate_paragraph_ref,
)


def split_body_into_paragraphs(body: str) -> tuple[tuple[int, str, str], ...]:
    """把正文切成 `(序号, 原文, ref)` 三元组，**逐字转发**给 media。

    本包用它做三件事：① 装配 `CheckSubject.paragraphs`（禁用词逐段扫的
    输入）；② 给 `MEDIA_PARAGRAPH_REF_STALE` 提供判据域；③ 给事实类规则
    把 `bindings[].paragraphIndex` 翻成可点击的段落锚点。
    三处用的是**同一个** ref 生成器。
    """
    return paragraph_refs_for_body(body)


def resolve_paragraph_ref(
    ref: str, *, known_paragraph_refs: frozenset[str]
) -> str:
    """校验 ref 指向当前正文的某一段，**逐字转发**给 media。

    本包的 finding 的 `paragraph_ref` 都是**自己从当前正文派生**的，理论上
    不需要再校验一次。本函数存在的理由是写入前的最后一道自检：若某条
    evaluator 将来开始接受外部传入的 ref（例如规则参数化），落库前必须过
    media 的形状与归属判定，而不是在本包长出第二份形状正则。

    校验不过抛 `media.schemas.SchemaViolation`（`reason` 为
    `PARAGRAPH_REF_UNKNOWN` / `PARAGRAPH_REF_MISSING`）—— 刻意不转换类型：
    端点层按 `reason` 映射 4xx 的分支只有一处。
    """
    return validate_paragraph_ref(ref, known_paragraph_refs=known_paragraph_refs)


__all__ = ["resolve_paragraph_ref", "split_body_into_paragraphs"]
