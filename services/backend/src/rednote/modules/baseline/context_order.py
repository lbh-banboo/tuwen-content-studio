"""动态上下文组装顺序（PRD §15.4 的五项优先级）—— **纯函数，不碰数据库**。

原型 `baseline.html` L71~96 逐字列出 PRD §15.4 的 5 项顺序（附「最高优先级，
裁剪时优先保留」的标注）：

    ① 当前项目批准事实      ← 最高优先级，裁剪时优先保留
    ② 当前产品有效资产
    ③ 当前场景与规则
    ④ 适用风格
    ⑤ 可迁移的跨项目方法

---

## 为什么这是一个单独的纯函数模块（而不是写在 service 里）

三个理由，每一个都单独成立：

1. **它是本包唯一交付的"规则"**。说明文档 §1.3/§1.7 明确：本包**不做**
   动态上下文的实际组装与裁剪（那是 WP26），只交付**顺序规则**。
   规则的落点必须是一个能被独立喂输入、断言输出的东西 ——
   写进 service 就意味着"验证它需要先建好四张表"。
2. **"最高优先级，裁剪时优先保留"是一句必须在代码里有对应物的话**。
   如果顺序只存在于文档里，WP26 的裁剪逻辑就得自己再实现一遍排序 ——
   而两处排序漂移时**不报错**：症状是"裁剪后重要的东西被丢了"，
   而没人能说清哪一处错了。这与本包 D6（`readiness.py` 单一来源）同一条判据。
3. **同源排序是幂等与稳定的前提**。`order_context_items()` 的输出顺序
   由 `(priority, item_id)` 两个键**完全确定**：同一批输入任何次数调用
   得到同一顺序。这一点必须可断言，否则"重建顺序"会与"增量维护"不一致。

## 为什么排序键里必须有 `item_id`（而不是只按 priority）

只按 `priority` 排序时，同一来源内的多条（例如 12 项事实）的相对顺序
是**输入顺序**——而输入顺序取决于调用方怎么拼这个 tuple，
于是同一个基线在两个不同的读取路径下会得到两种上下文顺序。
把 `item_id` 作为**次级键**，排序结果就与输入顺序无关（稳定且可复现）。

## 次序的"未知来源"处理（白名单而非兜底桶）

`priority_of()` 对未知 `source` **抛错**，不做"其余全部排最后"的兜底。
理由是本项目已现身多次的形态（ISS-78/84/85 同族）：
一个写成"其余全部"的兜底桶**永远不会告警** —— 一个新的来源被加进来时，
它会静默地排到末尾，而没有任何人知道 PRD §15.4 的意思是"它该排第几"。
**新增来源必须显式回答"它排第几"这个问题。**

---

## `item_kind` → `source` 的映射，以及**它刻意的两处缺口**

`baseline_items.context_order` 是 NOT NULL 列，而 `ItemDraft.context_order`
可空 —— 因此"空的时候填什么"必须有一个确定的答案，这个答案就是
`ITEM_KIND_CONTEXT_SOURCE`。映射**不是我推演的**，是把原型两处清单逐条对上：

| `item_kind` | `source` | 原型的对应（逐字复核） |
|---|---|---|
| `FACT` | `PROJECT_FACTS` | L77「1. 当前项目批准事实」/ L78「12 项」 |
| `SCENE` | `SCENES_AND_RULES` | L85「3. 当前场景与规则」/ L86「5 场景」 |
| `RULE` | `SCENES_AND_RULES` | L85 同上 / L86「+ 规则集 v5」 |
| `STYLE` | `STYLES` | L89「4. 适用风格」/ L90「风格指纹 v4」 |

**两处缺口是事实，不是遗漏**（各自都由一个显式集合钉住，见下）：

1. **`RESEARCH` 不对应任何一档**。原型的 5 档时间线（L75~96）里
   没有"研究资料"，它在**另一张表**里（L57 快照清单的「研究资料 21 份选用」）。
   这正是本模块开头那句"强行合并会让'研究资料排第几'没有答案"的**确切含义**：
   研究资料确实不在 PRD §15.4 的五项里 ——
   若把 `item_kind` 当 `source` 直接使，`RESEARCH` 要么静默排到末尾、
   要么让整批排序抛错。两者都错，所以**必须缺席**。
2. **`PRODUCT_ASSETS` 与 `CROSS_PROJECT_METHODS` 没有 `item_kind` 来源**。
   原型 L82 的「关联 8 条生效资产」与 L94 的「3 条表达方法」讲的是
   **别的东西**（知识资产本身 / 其他项目的方法），不是本条基线项的种类。

两个缺口各自有集合常量（`UNMAPPED_ITEM_KINDS` / `UNSOURCED_CONTEXT_SOURCES`），
测试对它们做**等式**断言 —— 新增一个 `ITEM_KIND` 或一档 `source` 时，
不回答"它排第几 / 它从哪来"就会红。
这条与上文"白名单而非兜底桶"是同一纪律的两个方向。
"""

from __future__ import annotations

from typing import Final

from rednote.modules.baseline.baseline_types import (
    ITEM_KIND_FACT,
    ITEM_KIND_RULE,
    ITEM_KIND_SCENE,
    ITEM_KIND_STYLE,
    ITEM_KINDS,
    ContextItem,
)

# ---------------------------------------------------------------------------
# 五项优先级（PRD §15.4 原文顺序，数字越小越优先）
# ---------------------------------------------------------------------------
# 键名是**受控取值**，与 `ItemDraft.item_kind` 刻意不同名：
# `item_kind` 是"这条基线项是什么"（持久化的），
# `source` 是"它进动态上下文时属于哪一档"（组装期的）。
# 两者现在接近，但 `RESEARCH` 与 `RULE` 在两项里落在不同档 ——
# 强行合并会让"研究资料排第几"没有答案。

CONTEXT_SOURCE_PROJECT_FACTS: Final[str] = "PROJECT_FACTS"
CONTEXT_SOURCE_PRODUCT_ASSETS: Final[str] = "PRODUCT_ASSETS"
CONTEXT_SOURCE_SCENES_AND_RULES: Final[str] = "SCENES_AND_RULES"
CONTEXT_SOURCE_STYLES: Final[str] = "STYLES"
CONTEXT_SOURCE_CROSS_PROJECT_METHODS: Final[str] = "CROSS_PROJECT_METHODS"

#: **PRD §15.4 的五项顺序，唯一权威定义。**
#: 语法上是 dict，但语义是"有序清单"——`CONTEXT_PRIORITY_ORDER` 的迭代顺序
#: 就是优先级顺序，测试会逐项断言它与 PRD 原文一致。
CONTEXT_PRIORITY_ORDER: Final[dict[str, int]] = {
    CONTEXT_SOURCE_PROJECT_FACTS: 1,
    CONTEXT_SOURCE_PRODUCT_ASSETS: 2,
    CONTEXT_SOURCE_SCENES_AND_RULES: 3,
    CONTEXT_SOURCE_STYLES: 4,
    CONTEXT_SOURCE_CROSS_PROJECT_METHODS: 5,
}

#: 优先级最高的那一档。「裁剪时优先保留」的对象 —— WP26 会读它。
#: **导出一个名字而不是让 WP26 写 `== 1`**：那个 `1` 是一个魔法数，
#: 而"最高优先级是哪一档"是本包的定义，不是 WP26 的。
TOP_PRIORITY: Final[int] = min(CONTEXT_PRIORITY_ORDER.values())

#: 同一档内部的稳定次序（同 priority 的条目按它排）。
#: 用 `item_id` 的字符串序 —— 它唯一且稳定（见模块 docstring 第二点）。
#: 若有两条同 `(priority, item_id)`，`item_id` 相同意味着它们本就是同一条。
_SORT_SECONDARY: Final[str] = "item_id"

# ---------------------------------------------------------------------------
# `item_kind` → `source`（见模块 docstring「映射，以及它刻意的两处缺口」）
# ---------------------------------------------------------------------------
# 键是 `ItemDraft.item_kind`（持久化的"这条基线项是什么"），
# 值是 `CONTEXT_SOURCE_*`（组装期的"它进动态上下文时属于哪一档"）。
ITEM_KIND_CONTEXT_SOURCE: Final[dict[str, str]] = {
    ITEM_KIND_FACT: CONTEXT_SOURCE_PROJECT_FACTS,
    ITEM_KIND_SCENE: CONTEXT_SOURCE_SCENES_AND_RULES,
    ITEM_KIND_RULE: CONTEXT_SOURCE_SCENES_AND_RULES,
    ITEM_KIND_STYLE: CONTEXT_SOURCE_STYLES,
}

#: **刻意没有映射的 `item_kind`**（`RESEARCH`：原型里它在另一张表，不在 §15.4 五项内）。
#: 写成显式集合而不是"其余全部"，理由同下：兜底桶永不告警。
#: 测试对它做等式断言 —— 新增 `ITEM_KIND` 时必须在"排第几"这个问题上表态。
UNMAPPED_ITEM_KINDS: Final[frozenset[str]] = frozenset(ITEM_KINDS) - frozenset(
    ITEM_KIND_CONTEXT_SOURCE
)

#: **刻意没有任何 `item_kind` 来源的档**（原型 L82 的「生效资产」与
#: L94 的「跨项目表达方法」讲的是别的东西，不是本条基线项的种类）。
UNSOURCED_CONTEXT_SOURCES: Final[frozenset[str]] = frozenset(
    CONTEXT_PRIORITY_ORDER
) - frozenset(ITEM_KIND_CONTEXT_SOURCE.values())


class UnknownItemKindError(ValueError):
    """`item_kind` 不在 `ITEM_KIND_CONTEXT_SOURCE` 里。

    **与 `UnknownContextSourceError` 一样刻意不用 `ApiError`**：
    `item_kind` 的取值由数据库 CHECK 与本包受控取值域双重限定，
    走到这里说明代码里出现了一个本包没定义过的种类 —— 那是编程错误。
    若映射成 422，一个拼错的种类会变成"用户提交了非法数据"，
    而用户提交的是 `ItemDraft`，`item_kind` 是**我们**在别处赋的。
    """


def context_source_of(item_kind: str) -> str:
    """`item_kind` → 动态上下文档位。未映射的种类**抛错**（见模块 docstring）。

    服务层在写入 `baseline_items.context_order` 时调用它，
    因此"研究资料排第几"这个问题会在**创建基线那一刻**暴露，
    而不是在 WP26 组装上下文时变成一个静默的末尾项。
    """
    try:
        return ITEM_KIND_CONTEXT_SOURCE[item_kind]
    except KeyError:
        raise UnknownItemKindError(
            f"基线项种类 {item_kind!r} 没有对应的动态上下文档位。"
            f"已映射：{', '.join(sorted(ITEM_KIND_CONTEXT_SOURCE))}；"
            f"刻意未映射（不在 PRD §15.4 五项内）：{', '.join(sorted(UNMAPPED_ITEM_KINDS))}"
        ) from None


def order_for_item_kind(item_kind: str) -> int:
    """`item_kind` → 它在动态上下文里的优先级数字（写入 `context_order` 用）。

    组合 `context_source_of()` 与 `priority_of()` —— 服务层只需调这一个。
    """
    return priority_of(context_source_of(item_kind))


class UnknownContextSourceError(ValueError):
    """`source` 不在 `CONTEXT_PRIORITY_ORDER` 里。

    **刻意不用 `ApiError`**：这个错误是**编程错误**（调用方传了一个
    本包没定义过的档），不是用户输入错误。若把它映射成 422，
    一个拼错的档名会变成"用户提交了非法数据"——而用户根本没提交过它。
    """


def unknown_sources(sources: object) -> tuple[str, ...]:
    """返回不在受控取值域里的来源（供 API 预检与测试共用）。

    抽成函数而不是在 `priority_of` 里内联，因为**两处需要它**：
      · `priority_of` 抛错时用它拼消息；
      · `order_context_items` 在排序前做一次**整体**检查 ——
        若只靠排序过程中的抛错，一个空列表（或被 slice 掉的来源）
        会让非法档名**静默通过**（ISS-78 同族：判据被数据形状绕过）。
    """
    return tuple(
        sorted(
            {
                str(source)
                for source in sources  # type: ignore[union-attr]
                if str(source) not in CONTEXT_PRIORITY_ORDER
            }
        )
    )


def priority_of(source: str) -> int:
    """取某一档的优先级。未知档**抛错**（见模块 docstring 的"未知来源"一节）。"""
    try:
        return CONTEXT_PRIORITY_ORDER[source]
    except KeyError:
        raise UnknownContextSourceError(
            f"未知的动态上下文来源 {source!r}。"
            f"合法取值：{', '.join(sorted(CONTEXT_PRIORITY_ORDER))}"
        ) from None


def order_context_items(items: tuple[ContextItem, ...]) -> tuple[ContextItem, ...]:
    """按 PRD §15.4 的五项优先级重排上下文条目。

    纯函数：不读库、不改输入、多次调用结果一致。

    排序键是 `(priority, item_id)` 两元组 —— 单靠 priority 会让同一档内的
    顺序取决于输入顺序（见模块 docstring 第二点）。

    **先做整体合法性检查再排序**：`sorted()` 遇到未知来源时是在比较过程中
    才抛错，若输入为空或该来源排在最后被短路，非法值就漏过去了。
    """
    illegal = unknown_sources(item.source for item in items)
    if illegal:
        raise UnknownContextSourceError(
            f"未知的动态上下文来源：{', '.join(illegal)}。"
            f"合法取值：{', '.join(sorted(CONTEXT_PRIORITY_ORDER))}"
        )
    return tuple(
        sorted(
            items,
            key=lambda item: (priority_of(item.source), getattr(item, _SORT_SECONDARY)),
        )
    )


def is_top_priority(source: str) -> bool:
    """该档是否是"裁剪时优先保留"的那一档（PRD §15.4 的 ① ）。"""
    return priority_of(source) == TOP_PRIORITY


__all__ = [
    "CONTEXT_PRIORITY_ORDER",
    "CONTEXT_SOURCE_CROSS_PROJECT_METHODS",
    "CONTEXT_SOURCE_PRODUCT_ASSETS",
    "CONTEXT_SOURCE_PROJECT_FACTS",
    "CONTEXT_SOURCE_SCENES_AND_RULES",
    "CONTEXT_SOURCE_STYLES",
    "ITEM_KIND_CONTEXT_SOURCE",
    "TOP_PRIORITY",
    "UNMAPPED_ITEM_KINDS",
    "UNSOURCED_CONTEXT_SOURCES",
    "UnknownContextSourceError",
    "UnknownItemKindError",
    "context_source_of",
    "is_top_priority",
    "order_context_items",
    "order_for_item_kind",
    "priority_of",
    "unknown_sources",
]
