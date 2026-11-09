"""WP27 的参考素材绑定校验与段落引用校验（纯函数，无 IO、无 session）。

**本模块是 R1 与 R4 两条高风险的唯一落点**（说明文档 §1.6），写法刻意
**不是**最自然的那种 —— 理由与 `content/schemas.py` 逐条同构，此处只写
**本包特有的**部分。

---

## 一、R1：绑定校验为什么需要**三个**上游事实（而不是一个"素材存在吗"）

最自然的写法是：

    for b in bindings:
        if b.assetId not in existing_ids:  reject()   # ← 只判存在

它**看起来**判完了"绑定有效"，但把三种**修法完全不同**的故障
压成了同一个码（"素材不存在"）：

| 故障 | 真实根因 | 该做什么 | 本模块报的码 |
|---|---|---|---|
| 素材 id 打错了 / 素材已被物理删除 | 请求内容错 | 改请求 | `REFERENCE_BINDING_MISSING` |
| 素材**属于另一个项目** | 越界（可能是攻击，也可能是前端串了 id） | 查权限/查前端 | `REFERENCE_BINDING_CROSS_PROJECT` |
| 素材存在、也同项目，但 `state=RETIRED`（已被停用） | 用了过期素材 | 换素材/恢复登记 | `REFERENCE_ASSET_NOT_ACTIVE` |

三者报同一个码时，"该做什么"不可判 —— 而这是**排障成本**上的差异，
不是风格问题。本仓在 WP26 的 `validate_bindings` 里已用过同一手法
（四个入参各自独立）。

### ★ 为什么"属于哪个项目"必须作为**独立事实**传入（而不是在 SQL 里过滤掉）

这是本包**当场改正过的一处实现**（`repository.claim_assets` 的 docstring
记了全过程）：第一版在 SQL 里就带 `project_id = :pid` 过滤，于是
"跨项目"与"压根不存在"在应用层**同形**（都不在返回集合里）⇒
`REASON_REFERENCE_BINDING_CROSS_PROJECT` **永远不可达（死码）**。

**判据形态**：三个集合的大小关系必须满足

    foreign_ids ⊆ existing_ids      # 跨项目的素材当然"存在"
    usable_ids  ⊆ existing_ids      # 可用素材当然"存在"
    foreign_ids ∩ usable_ids = ∅    # 跨项目的素材在本项目里当然不可用

这三条在 `validate_reference_bindings` 开头**显式断言**（不满足即抛
`ValueError` —— 那是 `repository.claim_assets` 的实现缺陷，不是调用方的
数据错，因此**不用 `SchemaViolation`**）。不做这个断言的话，
`claim_assets` 将来若被改成"只返回两个集合"，上面的三个分支会
**静默退化**成两个（跨项目那一路永远不进）。

---

## 二、R1 的两条**机械判据**（文档写的是自然语言，此处落成可判的形式）

文档 §1.6 R1 的判据原话：

> 构造 6 张图，断言"每张图绑定的素材 == 1 条"，且"全篇绑定的素材
> 去重后 < 素材库全部"—— 若前者 > 1 或后者相等，说明在做"整库引用"

这两句话里有**两个量各有一个歧义**，两者都会让判据**恒真地通过**：

| 歧义 | 若不管 | 本模块的处置 |
|---|---|---|
| "绑定的素材 == 1 条"的"素材数" = **条目数**还是**去重后 id 数**？ | 同一素材绑两次 ⇒ 条目数 2、素材数 1，而 `bindings_size` CHECK（1..8）**照样通过** | 显式分开算 `entry_count` 与 `distinct_count`，不等即 `REFERENCE_BINDING_DUPLICATE` |
| 绑定元素**本身可解析吗**？ | 键形状坏 ⇒ `from_payload` 抛 `ValueError` 穿到端点变 **500** | 接住并翻成 `REFERENCE_BINDING_MALFORMED` |

**为什么"去重后 id 数"这个口径要在这里显式存在**：R1 判据的后半句
"全篇绑定的素材去重后 < 素材库全部"用的正是**去重后 id 数**；
而前半句 CHECK 用的是**条目数**。同一个字段上两个量，各有一个消费点
⇒ 两处各写一遍去重逻辑必然分叉（铁律 26）。本模块把它们**都算出来
且互相对账**（`distinct_count != entry_count` 即拒），于是"去重"这件事
在本包**只有一处实现**。

---

## 三、R4：段落引用校验（`paragraph_ref` 而不是下标）

R4 的落点是 `media_versions.paragraph_ref`。校验要做的是"这个 ref
指的是**当前正文的**某一段吗"：

1. 形状合法（`p<序号>-<8位哈希>`）—— 由 `types.parse_paragraph_ref` 判；
2. **在本次生成的段落集合里**（`known_paragraph_refs`）——
   不在即 `PARAGRAPH_REF_UNKNOWN`（典型场景：正文重排后，旧图的 ref
   不再匹配任何当前段落 ⇒ **可判为"需要重做"**，而不是静默错位）。

**为什么入参是"ref 的集合"而不是"段落下标集合"**：R4 的全部意义就是
"不用下标"。若本函数收下标集合、再自己拼 ref，那么拼 ref 这件事就有
了**第二个实现**（`types.paragraph_ref_for` 是第一个）—— 两处拼法的
归一化口径一旦不同（比如一处折空白、一处不折），表现是"刚生成的图，
`paragraph_ref` 立刻被判为未知"。因此本函数**只消费 ref 本身**。

---

## 四、为什么是纯函数

无 `session`、无 `dispatch_call`、无 `datetime.now()`。理由与
`content/schemas.py` 逐字相同：这些校验是**判据的落点**，而判据必须能
在**不连数据库**的情况下跑 —— 一条需要真库才能跑的校验判据，在 CI 上
就是一条会被 skip 掉的判据。
⛔ 原文以"test_media_wp27.py 的反例用例全部是纯函数用例"举证，而该文件
不存在、本包零覆盖（2026-10-07 更正）：这些校验函数**目前没有用例** ——
"做成纯函数"是设计取向，不是已被断言的事实。

---

## 五、OP-199：「3 张图分别画哪一段」——老大 2026-10-08 拍板「顺序对位」

张数固定 3 张（OP-198，`types.IMAGES_PER_CONTENT`）之后，**张数有了、
落点没有**：`media_items.paragraph_ref` 是 NOT NULL，而"这 3 张各自画哪一段"
在原型与说明文档里**都没有来源** ⇒ `models.py` 的 docstring 把它记成
一条尚未拍板的规则（OP-199）。老大拍板：

> **顺序对位** —— 第 i 张配第 i 段（i 从 0 起）；段落数不足 3 时，
> 多余张一律回落**最后一段**。

落点是 `assign_paragraph_refs`（本模块）。三条理由：

| 为什么是它 | 不这么做的话 |
|---|---|
| 段落顺序**天然有序**，无需引入新排序键 | 均分（`i % 段数`）会让第 4 张绕回第 1 段；段落数一变，**全部映射整体漂移** |
| 不足 3 段**仍然出 3 张** | "只做 N 张"会让实际张数 < 3，而预占按**固定 3 张**算（R3）⇒ 账面永远差一笔，**且不报错** |
| 记为**纯函数**、与切段同模块 | 写进 `service.py` 则判据绑未入库产物（见 `paragraph_refs_for_body` 的搬迁论证，ISS-95 族） |

⚠️ **多张图落到同一段是合法的**，不是退化：身份键是
`(content_item_id, sort_order)`（"这是这篇内容的第 N 张"），
`paragraph_ref` 只是**当前版本的投影**，**不落唯一约束**
（`models.py` docstring 一）。因此"第 2、3 张都画最后一段"在位图上
是两行不同 `sort_order`、同 `paragraph_ref` —— 那正是"同一段的多角度图"。

⛔ **段落为空时必须 fail-closed**（报 `PARAGRAPH_REF_UNKNOWN`），
**不许**编一个 ref 或退回"不落段落坐标"：后者会让图照生成、坐标是编的，
而真正的根因（正文为空）被推迟到**执行期**才由 `validate_paragraph_ref`
每张报一次 —— 等于每张图白买一次。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from rednote.modules.media.types import (
    BINDING_KEYS,
    BINDING_KEY_ASSET_ID,
    BINDING_KEY_NOTE,
    BINDING_KEY_PART,
    MAX_ASSET_NOTE_CHARS,
    MAX_PARAGRAPH_REF_CHARS,
    MAX_PRODUCT_PART_CHARS,
    MAX_REFERENCE_ASSETS,
    MEDIA_FORM_STATIC,
    MEDIA_FORMS,
    MEDIA_REQUEST_KEYS,
    MEDIA_REQUEST_REQUIRED_KEYS,
    MIN_REFERENCE_ASSETS,
    REASON_GENERATION_REQUEST_INVALID,
    REASON_PARAGRAPH_REF_MISSING,
    REASON_PARAGRAPH_REF_UNKNOWN,
    REASON_REFERENCE_ASSET_NOT_ACTIVE,
    REASON_REFERENCE_BINDING_CROSS_PROJECT,
    REASON_REFERENCE_BINDING_DUPLICATE,
    REASON_REFERENCE_BINDING_MALFORMED,
    REASON_REFERENCE_BINDING_MISSING,
    REASON_REFERENCE_BINDING_TOO_MANY,
    REASON_TASK_KIND_UNKNOWN,
    REQUEST_KEY_MEDIA_FORM,
    TASK_KINDS,
    MediaReferenceBinding,
    images_per_content,
    paragraph_ref_for,
    parse_paragraph_ref,
)


class SchemaViolation(ValueError):
    """一次媒体结构校验失败。

    三个字段都是**机器可判**的（`field` 定位、`reason` 分类），
    与 `content/schemas.py::SchemaViolation` 逐字同形。

    ⚠️ **为什么不在本包复用 `content.schemas.SchemaViolation`**：
    两条理由，第二条是硬的 ——

    ① 它会把 `media` 模块与 `content` 模块在**导入期**绑在一起，
       而本包的分层是单向的（`types ← schemas / models / repository /
       bridges / service`），跨模块的方向由"谁消费谁"决定：本包
       **不消费** `content.schemas` 的任何校验器（段落 ref 由本包的
       `types.parse_paragraph_ref` 判），因此没有复用它的理由；
    ② **`content` 模块不是本包的依赖**：`media` 与 `content` 是**平级**
       的业务模块（两者都依赖 `types`/`platform`，不互相依赖）。
       跨平级模块 import 一个异常类型，会让"改 content 的校验器"
       可能影响 media 的端点行为 —— 而这种耦合**不会被任何判据抓到**
       （两个模块各自的测试都过）。

    `api.py` 把两种 `SchemaViolation` 分别映射到 HTTP 状态码
    （本包的那条走 `_HTTP_BY_REASON`；`content` 的那条与它无关）。
    """

    def __init__(self, *, field: str, reason: str, message: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(message)


# ---------------------------------------------------------------------------
# R1：参考素材绑定
# ---------------------------------------------------------------------------


def validate_reference_bindings(
    raw: Any,
    *,
    existing_ids: frozenset[uuid.UUID],
    foreign_ids: frozenset[uuid.UUID],
    usable_ids: frozenset[uuid.UUID],
    what: str = "参考素材绑定",
) -> tuple[MediaReferenceBinding, ...]:
    """校验一张生成图的参考素材绑定（R1 的唯一落点）。

    ## 三个上游事实的语义（**都是"集合"而不是"布尔"**）

    它们由 `repository.claim_assets()` 在**同一次读**里取出（见该方法的
    docstring：三次分别读会在一个"中途变过的素材库"上判合法性）：

    | 入参 | 含义 | 对应故障 |
    |---|---|---|
    | `existing_ids` | 这些 id 在 `reference_assets` 里**存在**（不管属于谁） | 不在 ⇒ 打错/被删 |
    | `foreign_ids` | 其中属于**别的**项目的 | 在 ⇒ 越界 |
    | `usable_ids` | 其中**属于本项目且仍可用**的（`state=ACTIVE` 且未被接替） | 不在 ⇒ 用了停用素材 |

    ## 判定的顺序是**刻意的**

    先判"不在 existing 里"（最基础），再判"在 foreign 里"，最后判
    "不在 usable 里"。顺序换了会改变**报哪个码**：

    - 一条 `foreign` 素材**必然不在** `usable_ids` 里（同项目的条件不满足）
      ⇒ 若先判 usable，跨项目会被报成 `REFERENCE_ASSET_NOT_ACTIVE`，
      而"停用"与"越界"是**相反的处置方向**（换素材 vs 查权限）;
    - 因此**必须先判 foreign**。这条在下面的代码里以
      `elif` 链的顺序体现 —— 而顺序这种事**必须配一条判据**。
      ⛔ 原文点的正是"（test_media_wp27.py：给一条 foreign 素材，断言报的是
      `CROSS_PROJECT` 而不是 `NOT_ACTIVE`）"—— 该文件不存在、本包零覆盖
      （2026-10-07 更正）⇒ 这条判据**当前缺失**，顺序只靠下面的代码排列本身。

    ## 数量：`entry_count` 与 `distinct_count` 都要判

    见模块 docstring 二。`MIN..MAX` 判的是**条目数**（与
    `models.py` 的 `bindings_size` CHECK 同口径 —— 那里也只能数条目），
    而"**去重后**至少一条"另行判 `distinct_count`。
    `distinct_count != entry_count` ⇒ `DUPLICATE`。

    ⚠️ **`MAX` 判的是条目数，而 R1 想防的"整库引用"是去重后的形态**：
    素材库有 20 条、一张图绑了其中 8 条时，条目数 8 **合法**——
    这**不是**漏网，而是 `MAX_REFERENCE_ASSETS = 8` 这个上界本身
    承载了"多部件产品的上限"语义（见 `types.py` 的定义处）。
    真正的"整库引用"是**每张图的绑定集合都相同且等于全集**，
    那是**跨图**的判据（R1 判据的后半句），落在 `service.py` 与
    **验收脚本**上，不在本函数（本函数只看一张图）。
    """

    # --- 上游三个集合的**自洽性**：不满足即 repository 的实现缺陷 -----------
    # 见模块 docstring 一末节。这里**不**抛 `SchemaViolation` ——
    # 那不是调用方的数据错，而是本包内部某个函数返回了互相矛盾的集合。
    # 静默放行的话，下面的三分支会**退化**（详见 docstring）。
    if not foreign_ids <= existing_ids:
        raise ValueError(
            "repository.claim_assets 返回的集合不自洽："
            f"foreign_ids - existing_ids = {sorted(foreign_ids - existing_ids)}。"
            "（跨项目的素材当然『存在』—— 不存在就无从谈归属。"
            "这一条是内部判据，不是调用方的数据错。）"
        )
    if not usable_ids <= existing_ids:
        raise ValueError(
            "repository.claim_assets 返回的集合不自洽："
            f"usable_ids - existing_ids = {sorted(usable_ids - existing_ids)}。"
            "（可用素材当然『存在』。）"
        )
    overlap = foreign_ids & usable_ids
    if overlap:
        raise ValueError(
            "repository.claim_assets 返回的集合不自洽："
            f"foreign_ids ∩ usable_ids = {sorted(overlap)}。"
            "（『属于别的项目』与『在本项目且可用』不可能同时成立 —— "
            "两者同时出现在返回集里，说明那一列的比较被写反了。）"
        )

    if not isinstance(raw, list):
        raise SchemaViolation(
            field="referenceBindings",
            reason=REASON_REFERENCE_BINDING_MALFORMED,
            message=f"{what}必须是数组，实际是 {type(raw).__name__}。",
        )

    parsed: list[MediaReferenceBinding] = []
    seen: list[uuid.UUID] = []
    for position, item in enumerate(raw):
        # ★ **两个名字，各司其职**（不要合并成一个）：
        #   · `field_prefix` 进 `SchemaViolation.field` —— 它必须是**线上字段名**
        #     （camelCase），因为前端拿它定位到请求体里的哪一个字段。
        #     写成中文标签的表现是"机器可判的定位"退化成"给人读的文案"
        #     （两者混用就是 ISS-137『同一事实两种写法』：同一个字段在两处
        #     报出两种 path，前端只能对其中一个写特判）。
        #   · `label` 进 `message` —— 那是给人读的。
        field_prefix = f"referenceBindings[{position}]"
        label = f"{what}第 {position} 条"
        binding = _parse_binding(item, field_prefix=field_prefix, label=label)

        # --- 判据一：存在（最基础的一层）----------------------------------
        if binding.asset_id not in existing_ids:
            raise SchemaViolation(
                field=f"{field_prefix}.{BINDING_KEY_ASSET_ID}",
                reason=REASON_REFERENCE_BINDING_MISSING,
                message=(
                    f"{label}引用的素材 {binding.asset_id} 不存在。"
                    "（要么 id 打错了，要么素材行已被删除 —— "
                    "`reference_assets` 不可变，因此后者只能是数据事故）"
                ),
            )

        # --- 判据二：**必须先判跨项目**（见 docstring 的"顺序是刻意的"）-----
        if binding.asset_id in foreign_ids:
            raise SchemaViolation(
                field=f"{field_prefix}.{BINDING_KEY_ASSET_ID}",
                reason=REASON_REFERENCE_BINDING_CROSS_PROJECT,
                message=(
                    f"{label}引用的素材 {binding.asset_id} 属于另一个项目。"
                    "（素材承载本品牌的产品事实，跨项目引用等于把别的品牌的"
                    "产品画进这组图 —— 处置方向是查权限或查前端串 id，"
                    "与『素材已停用』完全不同。）"
                ),
            )

        # --- 判据三：本项目且仍可用 ----------------------------------------
        if binding.asset_id not in usable_ids:
            raise SchemaViolation(
                field=f"{field_prefix}.{BINDING_KEY_ASSET_ID}",
                reason=REASON_REFERENCE_ASSET_NOT_ACTIVE,
                message=(
                    f"{label}引用的素材 {binding.asset_id} 已停用"
                    "（`state=RETIRED`，或被 `supersedes_asset_id` 接替）。"
                    "（停用即『不要再拿它出图』—— 换一张当前可用的素材，"
                    "或恢复登记。）"
                ),
            )

        # --- 判据四：同一条素材不许绑两次（模块 docstring 二 ①）-------------
        if binding.asset_id in seen:
            raise SchemaViolation(
                field=f"{field_prefix}.{BINDING_KEY_ASSET_ID}",
                reason=REASON_REFERENCE_BINDING_DUPLICATE,
                message=(
                    f"{label}重复引用了素材 {binding.asset_id}"
                    f"（第 {seen.index(binding.asset_id)} 条已绑过）。"
                    "（`bindings_size` CHECK 数的是**条目数**，"
                    "重复条目会让『这张图绑了几个素材』与『绑了几条』"
                    "给出两个答案 —— 而修法不同：前者是去重，"
                    "后者是数量本身越界。）"
                ),
            )
        seen.append(binding.asset_id)
        parsed.append(binding)

    # --- 数量：下界先判（0 条与 1 条是同一种"看起来有绑定"的失效形态）------
    entry_count = len(parsed)
    if entry_count < MIN_REFERENCE_ASSETS:
        raise SchemaViolation(
            field="referenceBindings",
            reason=REASON_REFERENCE_BINDING_MISSING,
            message=(
                f"{what}至少要 {MIN_REFERENCE_ASSETS} 条，实际 {entry_count} 条。"
                "（0 条绑定与『绑了一个不存在的素材』是同一种失效："
                "看起来有这个地方，实际指向空气 —— AQ-03 的对照关系不成立。）"
            ),
        )
    if entry_count > MAX_REFERENCE_ASSETS:
        raise SchemaViolation(
            field="referenceBindings",
            reason=REASON_REFERENCE_BINDING_TOO_MANY,
            message=(
                f"{what}最多 {MAX_REFERENCE_ASSETS} 条，实际 {entry_count} 条。"
                "（上界 = 多部件产品的部件数上限；超过它说明在做"
                "『整库引用』—— 每张图都绑全部素材 ⇒ 这张图画了什么"
                "仍然答不出来，绑定等于没有绑定。）"
            ),
        )

    # --- 两个量互相对账（模块 docstring 二：同一个字段上"去重"只有一处实现）--
    distinct_count = len(set(seen))
    if distinct_count != entry_count:
        # 走不到：上面已逐条拒过重复。留着是因为"上面的循环被改写"
        # 是一个真实可能的未来改动，而**改写后这里会立刻炸** ——
        # 比"重复条目静默通过、两个量从此不等"要好。
        # （本仓的取向：能变成不可能的就不要写成"不推荐"。）
        raise SchemaViolation(
            field="referenceBindings",
            reason=REASON_REFERENCE_BINDING_DUPLICATE,
            message=(
                f"{what}条目数 {entry_count} 与去重后素材数 {distinct_count} "
                "不一致 —— 重复判定被绕过了（内部判据）。"
            ),
        )

    return tuple(parsed)


def _parse_binding(
    item: Any, *, field_prefix: str, label: str
) -> MediaReferenceBinding:
    """解析一条绑定，**把 `types.py` 的 `ValueError` 翻成本模块的码**。

    见模块 docstring 二 ②：不翻的话，写入方写坏的绑定形状会一路穿到
    端点变成 **500**，而真正的根因是"这个元素的键不对"。

    `part` / `note` 的**长度上界**在这里判（`from_payload` 只判类型）：
    理由与 `content/schemas.py::_require_text` 一致 —— 这些值进不可变表，
    写进去删不掉，因此上界必须落在一处**进库之前**的地方。

    `field_prefix` 与 `label` 是两个参数（不是一个）：见调用处的注释 ——
    `field` 是机器可判的**线上字段名**，`label` 是给人读的定位文案。
    混用的后果是同一个字段在两处报出两种 path。
    """
    try:
        binding = MediaReferenceBinding.from_payload(item)
    except ValueError as exc:
        raise SchemaViolation(
            field=field_prefix,
            reason=REASON_REFERENCE_BINDING_MALFORMED,
            message=(
                f"{label}的形状不合法：{exc}"
                f"（合法键是 {sorted(BINDING_KEYS)}；"
                "`part` / `note` 可以为 null，但键必须在）"
            ),
        ) from exc

    if binding.part is not None and len(binding.part) > MAX_PRODUCT_PART_CHARS:
        raise SchemaViolation(
            field=f"{field_prefix}.{BINDING_KEY_PART}",
            reason=REASON_REFERENCE_BINDING_MALFORMED,
            message=(
                f"{label}的 {BINDING_KEY_PART} 超长："
                f"{len(binding.part)} > {MAX_PRODUCT_PART_CHARS}。"
            ),
        )
    if binding.note is not None and len(binding.note) > MAX_ASSET_NOTE_CHARS:
        raise SchemaViolation(
            field=f"{field_prefix}.{BINDING_KEY_NOTE}",
            reason=REASON_REFERENCE_BINDING_MALFORMED,
            message=(
                f"{label}的 {BINDING_KEY_NOTE} 超长："
                f"{len(binding.note)} > {MAX_ASSET_NOTE_CHARS}。"
            ),
        )
    return binding


def binding_asset_ids(
    bindings: tuple[MediaReferenceBinding, ...],
) -> tuple[uuid.UUID, ...]:
    """取出绑定里的全部 asset_id（**保序、不去重**）。

    **为什么要有这个函数**：`service.py` 拿它去 `claim_assets` 做
    "一次读"，而**去重**这件事在 `validate_reference_bindings` 里
    已经做过了（重复即拒）。此函数若自己再 `set(...)` 一次，
    "去重"就有两处实现 —— 而两处的差别在**保序**上会立刻显形
    （错误消息里的 `第 N 条` 会指错位置）。

    保序是为了让错误消息里的序号**与请求里的序号一致**：
    调用方看到"第 3 条绑定的素材不可用"时，能直接数请求体里的第 3 条。
    """
    return tuple(binding.asset_id for binding in bindings)


# ---------------------------------------------------------------------------
# R4：段落引用
# ---------------------------------------------------------------------------

# ⚠️ **本仓三处切段（`content` / `knowledge` / `media`）必须同口径**，
# 但**模块平级不可互 import** ⇒ 三处各有一份实现，无法收敛成一处
# （权威口径与委托关系登记在 **OP-197**）。改任一处必须同步另两处，
# 一致性由判据 `tools/quality/check_paragraph_split_parity.py` 钉住。
_PARAGRAPH_SPLIT: re.Pattern[str] = re.compile(r"\n\s*\n")


def paragraph_refs_for_body(body: str) -> tuple[tuple[int, str, str], ...]:
    r"""把正文切成段，返回 `(序号, 原文, ref)` 三元组序列。

    ## 切段口径（**`media` 侧唯一切段处**）

    按**空行**切（`\n\s*\n`），去掉首尾空白，丢弃纯空白的段。
    序号是**过滤后**的下标 —— 即 `paragraph_ref_for(index, text)` 里的
    `index` 从 0 开始连续编号，**跳过**空白段。

    ⚠️ **为什么"跳过空白段"而不是"给空白段也编号"**：若空白段参与编号，
    正文中删掉一个空段会让**后面所有段的序号平移** ⇒ 所有旧图的
    `paragraph_ref` 全部失效（`PARAGRAPH_REF_UNKNOWN`）⇒ 改个排版就要重做
    全部图。过滤后再编号，"删掉一个空段"不影响任何段的序号。

    ⚠️ **切段口径只有这一处**：拼 ref 只有 `types.paragraph_ref_for`
    一个实现（`validate_paragraph_ref` 的 docstring 已论证），
    而**切段**同样不能有两个实现 —— 两处切法不同会让同一段正文在
    "生成时"与"校验时"得到不同的 ref，于是刚生成的图立刻被判为未知。

    正文为空或全是空白 ⇒ 返回空元组。**调用方必须拒**（报
    `PARAGRAPH_REF_UNKNOWN`）：那是一件真的事故（正文没内容），
    而"跳过校验"会把它变成"图照生成、只是没有段落坐标" —— 静默降级。

    ## ★ 为什么落在 `schemas.py` 而不是 `service.py`（OP-197 收口期实测）

    本函数原本住在 `media/service.py`。但 `service.py` 长期是**未入库**的在途
    产物，于是以它为输入的一致性判据 `check_paragraph_split_parity.py`
    在**干净检出**上必然 FAIL（`ModuleNotFoundError: media.service`）
    —— 判据被绑在一个还没进版本库的实现上，等于"有防线但门打不开"
    （ISS-95 族）。

    本模块是**纯函数层**（无 IO、无 session，见模块 docstring），
    `types` 也在同模块内 ⇒ 搬到这里既不破坏分层，也让判据在干净检出上可跑。
    调用方（`service.py`）改为从本模块导入，**不保留第二份实现**。
    """
    out: list[tuple[int, str, str]] = []
    for chunk in _PARAGRAPH_SPLIT.split(body):
        text = chunk.strip()
        if not text:
            continue
        index = len(out)
        out.append((index, text, paragraph_ref_for(index, text)))
    return tuple(out)


def validate_paragraph_ref(
    ref: Any,
    *,
    known_paragraph_refs: frozenset[str],
    what: str = "段落引用",
) -> str:
    """校验一个 `paragraph_ref` 指向**当前正文的**某一段（R4）。

    ## 两道判据与它们各自的故障

    | 判据 | 不做的后果 | 报的码 |
    |---|---|---|
    | 形状合法（`p<序号>-<8位哈希>`） | 有人手工插了 `p3`（无哈希）⇒ 后续所有"这段是哪段"都答不出 | `PARAGRAPH_REF_UNKNOWN` |
    | 在 `known_paragraph_refs` 里 | 正文重排后旧图**静默错位**（画的还是旧内容） | `PARAGRAPH_REF_UNKNOWN` |

    两道**报同一个码**是刻意的：对调用方而言，"拆不开的 ref"与
    "拆得开但不对应当前任何段落"是同一件事（**这个 ref 现在指不到一段
    正文**），处置也相同（重做这张图）。而它们与"没给 ref"
    （`PARAGRAPH_REF_MISSING`）是**不同**的事 —— 那是请求少写了一个
    字段，处置是补上。

    ## 为什么入参是 `frozenset[str]` 而不是"段落列表"

    见模块 docstring 三末节：拼 ref 只有一个实现（`paragraph_ref_for`），
    本函数**只消费 ref 本身**。集合里放的是**已拼好的 ref**
    （由 `service.py` 从正文段落派生）。

    ## `known_paragraph_refs` 为空集合时的行为

    一律拒（任何 ref 都不在空集里）。这不设为"跳过校验"是刻意的：
    "正文切不出段落"是一件**真的事故**（正文为空 / 全部是空白），
    而跳过校验会把它变成"图照生成、只是没有段落坐标" —— 那是静默降级。
    """
    if not isinstance(ref, str) or not ref.strip():
        raise SchemaViolation(
            field="paragraphRef",
            reason=REASON_PARAGRAPH_REF_MISSING,
            message=f"{what}必须是非空字符串，实际是 {ref!r}。",
        )
    if len(ref) > MAX_PARAGRAPH_REF_CHARS:
        raise SchemaViolation(
            field="paragraphRef",
            reason=REASON_PARAGRAPH_REF_UNKNOWN,
            message=(
                f"{what}超长：{len(ref)} > {MAX_PARAGRAPH_REF_CHARS}。"
                "（它只由 `types.paragraph_ref_for` 生成，"
                "超长说明数据是从别处来的）"
            ),
        )
    # 形状由 `types.parse_paragraph_ref` 判（**唯一**的形状判定点）——
    # 本函数不自己写正则：写第二份正则就是"同一事实两种写法"
    # （ISS-137），而两份正则的分歧会表现为"这里过了那边没过"。
    try:
        index, digest = parse_paragraph_ref(ref)
    except ValueError as exc:
        raise SchemaViolation(
            field="paragraphRef",
            reason=REASON_PARAGRAPH_REF_UNKNOWN,
            message=f"{what}形状不合法：{ref!r}（{exc}）。",
        ) from exc
    # `parse_paragraph_ref` 已保证 `[0-9]+` 与 8 位，这里只做一次
    # "我拿到的东西确实被用了"的占位（下一条判据用的是 ref 本身）。
    _ = (index, digest)

    if ref not in known_paragraph_refs:
        raise SchemaViolation(
            field="paragraphRef",
            reason=REASON_PARAGRAPH_REF_UNKNOWN,
            message=(
                f"{what} {ref!r} 不对应当前正文的任何段落。"
                "（典型场景：正文改过之后旧图仍在 —— 它画的还是旧内容。"
                "这是**可判**的：ref 带文本哈希，因此重排/改写会让它"
                "不再匹配任何当前段落，而不是静默错位到另一段上。）"
            ),
        )
    return ref


# ---------------------------------------------------------------------------
# OP-199：3 张图分别画哪一段 —— 老大 2026-10-08 拍板「顺序对位」
# ---------------------------------------------------------------------------


def assign_paragraph_refs(
    paragraphs: tuple[tuple[int, str, str], ...],
    *,
    what: str = "图片段落对位",
) -> tuple[str, ...]:
    r"""按**顺序对位**给每张图派一个 `paragraph_ref`（OP-199 的唯一落点）。

    ## 规则（老大 2026-10-08 拍板，原文）

    > **顺序对位** —— 第 i 张配第 i 段（i 从 0 起）；段落数不足 3 时，
    > 多余张一律回落**最后一段**。

    因此返回值恒为**长度 3** 的元组（`images_per_content()`），
    与"实际出几张"**同源** —— 见下面 `image_count` 的论证。

    | 段落数 | 返回值（ref 的下标） |
    |---|---|
    | 1 | `[0, 0, 0]` —— 三张都画唯一那段（同一段的多角度图） |
    | 2 | `[0, 1, 1]` —— 第 3 张回落最后一段 |
    | ≥3 | `[0, 1, 2]` —— 前 3 段各一张（第 4 段起不出图） |

    ⚠️ **"多张落同一段"是合法的，不是退化**：身份键是
    `(content_item_id, sort_order)`（"这是这篇内容的第 N 张"），
    `paragraph_ref` 只是**当前版本的投影**、**不落唯一约束**
    （`models.py` docstring 一）。落到数据库上就是两行
    **不同 `sort_order`、同 `paragraph_ref`** —— 合法且可查。

    ## 为什么分段数**不足 3** 时是"回落最后一段"而不是"少出几张"

    预占按**固定 3 张**算（R3：`images_for` / `calls_for` 与
    `billing_bridge.estimate_generation_cost` 全走 `images_per_content()`）。
    若这里改成"只出 N 张"，实际张数 < 3 而账面仍按 3 张 ——
    差的那一笔**不会报错**，只表现为"账面上永远差一笔"
    （`images_for` 的 docstring 记了同一种失效形态）。
    回落保住了"张数"这一个量只有一处来源。

    ## 为什么没有 `image_count` 入参

    与 `types.images_for` 刻意去掉第二个入参**逐条同构**：
    老大拍板固定 3 张之后，那个入参**没有任何合法来源**。
    允许调用方传 ⇒ 它能绕过 `images_per_content()` 自己给一个值
    ⇒ 同一个量两个来源（铁律 26），且"传少了"不报错。
    去掉之后，"张数与预占口径不一致"在**类型层面**不可能发生。

    ## 段落为空 ⇒ fail-closed（**不编 ref、不跳过**）

    报 `PARAGRAPH_REF_UNKNOWN`。理由见模块 docstring 五末节：
    "正文切不出段落"是一件**真的事故**（正文为空 / 纯空白），
    而退化成"图照生成、只是没有段落坐标"是**静默降级** ——
    更糟的是根因会被推迟到**执行期**由 `validate_paragraph_ref`
    对每一张图各报一次 ⇒ 每张图**白买一次**才被发现。

    ## 对入参形状的**内部**自洽性检查

    `paragraphs` 的形状是 `paragraph_refs_for_body` 的产物
    `(序号, 原文, ref)`。这里断言**序号等于它的位置** ——
    三元组若不是从那个函数来的（手工拼的、或下标错位），
    派出来的 ref 会指向**别的段**且**完全静默**（ref 形状合法、
    也在 `known_paragraph_refs` 里，因此下游校验全都过）。

    不满足即抛 `ValueError` 而**不是** `SchemaViolation`：
    这是本包内部的形状缺陷（调用方给的元组不是切段函数的产物），
    不是调用方的数据错 —— 与 `validate_reference_bindings` 开头
    三条集合断言同一处置（不用 `SchemaViolation`）。

    ⚠️ 这里**不重算** `paragraph_ref_for(index, text)` 去比对 ref：
    ref 的拼法只有 `types.paragraph_ref_for` 一个实现，
    切段函数已用它拼过。重算一次不能多抓到任何东西
    （序号对了、ref 就是那个函数拼的），只多一次哈希。
    """
    for position, item in enumerate(paragraphs):
        # 形状先判：解包失败/长度不对时给出的是"入参不是切段函数的产物"。
        if not isinstance(item, tuple) or len(item) != 3:
            raise ValueError(
                f"{what}的入参形状不对：第 {position} 项是 "
                f"{type(item).__name__}、长度 "
                f"{len(item) if hasattr(item, '__len__') else '?'}，"
                "应为 `(序号, 原文, ref)` 三元组"
                "（本函数只接受 `paragraph_refs_for_body` 的返回值）。"
            )
        index, _text, _ref = item
        if index != position:
            raise ValueError(
                f"{what}的入参不自洽：第 {position} 项的序号是 {index}，"
                "两者必须相等"
                "（序号不是位置 ⇒ 派出的 ref 会指向**别的段**，"
                "且因为 ref 形状合法、也在已知集合里，下游校验**全都过** ——"
                "静默错位。这一条是内部判据，不是调用方的数据错。）"
            )

    if not paragraphs:
        raise SchemaViolation(
            field="paragraphRef",
            reason=REASON_PARAGRAPH_REF_UNKNOWN,
            message=(
                f"{what}：本次生成的正文切不出任何段落，"
                f"因此没有一段可以承载这 {images_per_content()} 张图。"
                "（正文为空或全是空白。**不退回“不落段落坐标”** —— "
                "那会让图照生成、坐标是编的，且根因要等每一张图各自"
                "失败一次才暴露，等于每张白买一次。）"
            ),
        )

    image_count = images_per_content()
    last_ref = paragraphs[-1][2]
    # 顺序对位：第 i 张 → 第 i 段；越过末段则回落末段。
    # （第 i 张的 `sort_order` 由调用方 `enumerate` 得出，本函数只派 ref ——
    #  `sort_order` 的合法域由 `types.MIN_SORT_ORDER` 与投影列定义，
    #  不在这里发明第二份口径。）
    return tuple(
        paragraphs[i][2] if i < len(paragraphs) else last_ref
        for i in range(image_count)
    )


def paragraph_ref_for_sort_order(
    paragraphs: tuple[tuple[int, str, str], ...],
    sort_order: int,
    *,
    what: str = "图片段落对位",
) -> str:
    r"""取**第 `sort_order` 张图**在**这份正文**下的 `paragraph_ref`（执行期唯一取点）。

    ## 为什么执行期要**重算**而不是读 `media_items.paragraph_ref`（老大 2026-10-08 拍板）

    `media_items.paragraph_ref` 是**入队那一刻**的投影快照，而生成是**异步**的
    —— 从入队到执行之间，正文可能已经改过（改写 / 重排 / 新增段落）。
    读快照的后果分两种，**都不报错**：

    | 正文变了 | 读快照的表现 |
    |---|---|
    | 该段文本被改写 | ref 的哈希对不上任何当前段落 ⇒ `PARAGRAPH_REF_UNKNOWN` ⇒ **整批图失败**（花了钱、没出图） |
    | 段落重排 / 增删 | ref 仍匹配某段，但那是**另一段**的内容 ⇒ 图画的是旧位置，**静默错位** |

    老大拍板「执行时按当时正文重算」：**执行那一次事务里读到的正文**才是权威
    —— 图要画的是"现在这一段"，不是"入队时那一段"。

    ## ★ 它不引入第二份对位规则

    本函数**委托** `assign_paragraph_refs`（对位规则的唯一实现），只是取出第
    `sort_order` 项。自己再写一遍 `paragraphs[i] if i < len(...) else last`
    就是第二份实现对同一规则的两个说法 —— 分叉的表现是"入队建了 3 行、
    执行时第 3 行落到别的段上"，而两边各自的测试都过。

    ## `sort_order` 越界 ⇒ fail-closed

    合法域是 `[0, images_per_content())`（`0` 是封面位，见 `types.MIN_SORT_ORDER`）。
    越界意味着这一行的 `sort_order` 不是按本包规则造的（手工插入 / 张数口径
    改过而旧行没迁）—— **不回落最后一段**：那会让"一行的身份键坏了"表现为
    "它安静地画了最后一段"，而这与合法的第 3 张**同形**。

    报 `PARAGRAPH_REF_UNKNOWN`（与"指不到任何当前段落"同码）：对调用方而言
    两者是同一件事 —— **这张图现在没有一个可以画的段**。
    """
    assigned = assign_paragraph_refs(paragraphs, what=what)
    if not isinstance(sort_order, int) or isinstance(sort_order, bool):
        raise SchemaViolation(
            field="sortOrder",
            reason=REASON_PARAGRAPH_REF_UNKNOWN,
            message=(
                f"{what}：`sort_order` 必须是整数，"
                f"实际是 {type(sort_order).__name__}。"
            ),
        )
    if not 0 <= sort_order < len(assigned):
        raise SchemaViolation(
            field="sortOrder",
            reason=REASON_PARAGRAPH_REF_UNKNOWN,
            message=(
                f"{what}：`sort_order={sort_order}` 越界"
                f"（合法域是 [0, {len(assigned)}) —— 每篇内容恒 {len(assigned)} 张，"
                "0 即封面位）。"
                "（越界说明这一行不是按本包规则造的。**不回落最后一段** ——"
                "那会让坏掉的身份键表现为『安静地画了最后一段』，"
                "与合法的最后一张图**同形**。）"
            ),
        )
    return assigned[sort_order]


# ---------------------------------------------------------------------------
# 生成请求载荷（`media_items.generation_request`）
# ---------------------------------------------------------------------------


def validate_generation_request_payload(raw: Any) -> dict[str, Any]:
    """校验 `generation_request` 的键形状（必需三键 + 可选 `mediaForm`）。

    `types.MediaGenerationRequest.from_payload` 已经做了完整的
    fail-closed 解析（键齐/不多/类型/取值域），本函数**不是**它的第二实现
    —— 它只在**端点那次事务**里被调，用来把"载荷写坏"翻成
    `SchemaViolation`（从而被 `api.py` 的 `_HTTP_BY_REASON` 映射成 422），
    而 `from_payload` 抛的是裸 `ValueError`（在处理器侧被折成任务失败）。

    ⚠️ **为什么不让端点直接调 `from_payload` 并 catch `ValueError`**：
    那样会把"载荷缺键"与"模型档案不存在"（`service.py` 也抛
    `ValueError` 族的 `MediaGenerationError`）混在同一个 `except` 里 ——
    而两者的 HTTP 码不同（422 vs 422 但 **reason 不同**，且后者
    还可能是 404）。用本模块的 `SchemaViolation` 把"形状错"单独标出来，
    端点侧的 `except` 才写得窄。

    判据与取数点都在 `types.py`（`MEDIA_REQUEST_KEYS` / `TASK_KINDS`），
    本函数**不写字面量** —— 三处（endpoint / handler / types）各写一份
    键名的表现是"端点校验过了、处理器解析炸了"，而两边各自的测试都过
    （`types.py` 的 `MEDIA_REQUEST_KEYS` 注释记了同一形态）。
    """
    if not isinstance(raw, dict):
        raise SchemaViolation(
            field="generationRequest",
            reason=REASON_GENERATION_REQUEST_INVALID,
            message=(
                f"generation_request 必须是 JSON 对象，实际是 "
                f"{type(raw).__name__}。"
            ),
        )
    missing = sorted(MEDIA_REQUEST_REQUIRED_KEYS - raw.keys())
    if missing:
        raise SchemaViolation(
            field="generationRequest",
            reason=REASON_GENERATION_REQUEST_INVALID,
            message=(
                f"generation_request 缺键：{missing}。"
                f"应有必需三键：{sorted(MEDIA_REQUEST_REQUIRED_KEYS)}"
                f"（可选 {REQUEST_KEY_MEDIA_FORM}，缺省 {MEDIA_FORM_STATIC}）。"
            ),
        )
    extra = sorted(raw.keys() - MEDIA_REQUEST_KEYS)
    if extra:
        raise SchemaViolation(
            field="generationRequest",
            reason=REASON_GENERATION_REQUEST_INVALID,
            message=(
                f"generation_request 有多余键：{extra}。"
                "（拒多余键：多一个键说明写入方与读取方对形状的理解已分叉）"
            ),
        )
    task_kind = raw.get("taskKind")
    if task_kind not in TASK_KINDS:
        raise SchemaViolation(
            field="taskKind",
            reason=REASON_TASK_KIND_UNKNOWN,
            message=(
                f"未知的生成类型：{task_kind!r}（本包只消费 {sorted(TASK_KINDS)}）。"
            ),
        )
    media_form = raw.get(REQUEST_KEY_MEDIA_FORM, MEDIA_FORM_STATIC)
    if media_form not in MEDIA_FORMS:
        raise SchemaViolation(
            field=REQUEST_KEY_MEDIA_FORM,
            reason=REASON_GENERATION_REQUEST_INVALID,
            message=(
                f"未知的媒体形态：{media_form!r}（本包只认 {list(MEDIA_FORMS)}）。"
            ),
        )
    return raw


__all__ = [
    "SchemaViolation",
    "assign_paragraph_refs",
    "binding_asset_ids",
    "paragraph_ref_for_sort_order",
    "validate_generation_request_payload",
    "validate_paragraph_ref",
    "validate_reference_bindings",
]