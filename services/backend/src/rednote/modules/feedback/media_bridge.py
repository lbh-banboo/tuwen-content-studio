"""`feedback` → `media` 的唯一接缝（WP29）。

## 为什么切段只能调用 media

段落 ref（`p<序号>-<8位哈希>`）的**形状与切段口径**由 media 拥有。反馈模块
若自己按空行切一次正文，就会与 media 生成图片时的切段口径分叉：
同一段正文在"生成时"和"提意见时"得到不同的 ref，于是刚生成的图立刻被
判成"不对应当前正文的任何段落"（`PARAGRAPH_REF_UNKNOWN`）。这种分叉是
**静默的** —— 两套实现各自的测试都会过，只有跨模块的真数据才会暴露。

因此本模块是 WP29 的 R2 风险缓解点：**本包零切段实现**，只做两件事 ——
把正文交给 `media.schemas.paragraph_refs_for_body`，把 ref 交给
`media.schemas.validate_paragraph_ref`。二者都是纯函数（无 IO、无 session），
所以桥接层不需要任何上下文，也没有可以"顺手加一点逻辑"的空间。
"""

from __future__ import annotations

from rednote.modules.media.schemas import (
    paragraph_refs_for_body,
    validate_paragraph_ref,
)


def split_body_into_paragraphs(body: str) -> tuple[tuple[int, str, str], ...]:
    """把正文切成 `(序号, 原文, ref)` 三元组，**逐字转发**给 media。

    为什么反馈模块需要切段：① 预览页要把图片与段落按 `paragraph_ref`
    对位展示；② 列表页要把"锚点已失效"的反馈标成 `STALE`；③ 新建段落反馈
    时要判 ref 是否对应当前正文。三处用的是**同一个** ref 生成器，
    否则"预览里能点的段落"与"提交时能通过的段落"会不一致。
    """
    return paragraph_refs_for_body(body)


def resolve_paragraph_ref(
    ref: str, *, known_paragraph_refs: frozenset[str]
) -> str:
    """校验 ref 指向当前正文的某一段，**逐字转发**给 media。

    校验不过抛 `media.schemas.SchemaViolation`（`reason` 为
    `PARAGRAPH_REF_UNKNOWN` 或 `PARAGRAPH_REF_MISSING`）—— 这里刻意
    不转换成别的异常：端点层按 `reason` 映射 4xx 的分支只有一处，
    桥接层再包一层会让"缺字段"与"锚点失效"混成同一个错误。

    `known_paragraph_refs` 为空集合时一律拒（media 的既有语义）：
    "正文切不出段落"是真事故，跳过校验等于让锚点静默失效。
    """
    return validate_paragraph_ref(ref, known_paragraph_refs=known_paragraph_refs)


__all__ = ["resolve_paragraph_ref", "split_body_into_paragraphs"]
