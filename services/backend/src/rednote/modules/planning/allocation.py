"""整数分配（**纯函数**，不读库、不依赖 ORM）。

本模块承载验收 AC-10~12 的配置侧，以及历史收敛决议 C14 / C19 的全部算法规则。

---

## 一、为什么必须是纯函数（本包最要紧的一句话）

"10 篇不能变成 11 篇"只有在**能枚举所有输入**时才是**被证明的**。
一旦函数能读库，输入空间就不再是可枚举的，只剩几条样例 ——
而"和漂移一篇"是**静默**缺陷：合计照样对得上时段，只有某一类多/少一篇，
成本错了但账面看不出。

历史原型里实测过一次：`[3, 3, 94]` 有 4 个分类对不上。

因此本模块**只接受纯数据**（`AllocationItem`），不接受 session / repository /
模型的任何引用。

## 一之二、★ 什么**不能**当判据：`sum(result) + unallocated == total`（ISS 级记录）

这条等式看着像不变量，实际是**代数恒等式**，与输入无关：

    result.allocated_total  ≡  sum(line.allocated for line in lines)   # 是个 @property
    unallocated             ≡  total - sum(allocation 里的同一个 dict)

两者相加展开成 `sum(x) + (total - sum(x)) == total`，**永远成立**。
本包实测过：400 组离谱输入（权重合计最高 8000‰、容量乱填、total 0~5000）
下该断言**一次都没破** —— 而同期它也没能发现"负 `unallocated`"。
（记为错误 ⑪：*看起来在守、其实永远不报红*，比没有判据更危险。）

**因此真正的判据必须是"两条独立数据源互证"**：

1. 测试侧按 C19/C14 规则**独立实现**一遍期望分配额，与返回值**逐条**比对；
2. `unallocated` 的边界（权重合计 ≤ 1000 时必须 `>= 0`）写成**断言**而非注释；
3. 每条断言都要做过**反向验证**：把实现改坏一次，断言必须报红。

穷举（total 1..120 × 项数 2..8 × 是否传容量）依然是必要的形式 ——
但穷举的**价值在覆盖输入空间**，不在于那个恒真等式。

## 二、两条算法路径（判据是"条目有没有容量上限"）

| 路径 | 何时用 | 算法 |
|---|---|---|
| **最大余数法** | 条目**都没有**容量上限（`capacity is None`） | C19：`floor(total × w / 1000)` + 按余数降序补 1 |
| **容量削峰** | 至少一个条目有容量上限 | C14：每轮按权重比例取整 → 超容量的削到容量 → 削下来的进下一轮 |

**为什么必须分两条路径**，而不是"一条路走到底"（本包实测）：
两条规则**在容量受限时互相打架**。考虑 `[{w:300,cap:None},{w:600,cap:1},{w:100,cap:5}]`、
`total=10`：最大余数法给出 `[3, 6→1, 1]`，合计 5，剩 5 条；
而"削下来的再按权重分给还有余量的场景"要求把这 5 条分给 a（它还有容量）⇒ 结果 `[9, 1, 0]`。
两个答案分别属于两条规则，**必须显式选一条**。
本包的选择：**有容量约束时以容量规则为准**（容量是业务硬约束，
比例只是期望值）；没有容量约束时以最大余数法为准（它是 AC-10~12 的判据形态）。

## 三、C19 的三级平局次序（可复现性的全部来源）

**余数大的优先 → `sort_order` 小的优先 → `key` 字典序**。

第三级为什么留着：人工新增的分类可能忘了给次序，那时仍要可复现。

**为什么第三级不是"配置顺序"**：原型写的是"按配置顺序依次 +1"。
"配置顺序"是一个**隐式**的量（取决于数组下标），而运营调整顺序
（拖动排序）会**静默改变分配结果** —— 同一份比例、同一份总数，
只因为拖动了一下就多一篇少一篇。`sort_order` 是**显式**的列，
改动它是一次有意图的操作，且进得了审计。

**为什么第三级不能是 `name`**：名称是可改的展示字段。

## 四、全部满容时剩余计入 `unallocated`，**不硬塞**（C14 原文）

塞进去就是产出重复内容。宁可报「分不完」让人决定：补场景，还是降低总篇数。

因此 `unallocated > 0` 是**合法结果**，调用方（草稿服务 / 前端）必须
显式处理它，而不是当成错误吞掉。

## 五、子配额（`allocate_from_remaining`）与"列和恒等于总计划"

C19：「首批 10 篇和扩产 20 篇分别建立子配额，不要求每个小批次都完全符合
总批次比例，但**累计配额必须收敛到总计划**」。

关键设计：**子配额从"剩余额度"里分**,而不是从总配额里重算。
若每批都按原始权重重算，前几批会把大分类顶到上限，
第 9、10 批一篇都拿不到（而累计仍等于总数 —— 又是账面看不出）。

实现上，"从剩余额度里分"就是 `allocate_by_weight`（权重保持不变，
但每条目的**可用容量**由调用方扣减后传入）。本模块为此提供一个
便利包装：`allocate_from_remaining(items, remaining_by_key, remaining_total)`。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Final

from rednote.modules.planning.types import (
    PERMILLE_TOTAL,
    AllocationItem,
    AllocationLine,
    AllocationResult,
)

#: 削峰轮数的硬上界。**存在的理由是防御不收敛**，不是算法需要这么多轮：
#: 每一轮至少削掉一个条目的容量（削到 0 之后它不再参与），
#: 因此真实轮数 ≤ 条目数。给一个 4 倍的余量是为了让
#: "轮数异常"以**显式异常**的形式暴露，而不是静默跑满循环。
_MAX_SHAVE_ROUNDS: Final[int] = 256


def _check_items(items: Sequence[AllocationItem]) -> None:
    """输入合法性。**在这里拒绝而不是静默处理**。

    重复 `key` 会让 `by_key` 覆盖前一个 ⇒ 有一行的结果被丢弃，
    而**合计照样等于 total**（被丢弃那条的份额会被分配给别的行）。
    这正是本包要防的"账面看不出"形态，因此必须在入口拒绝。

    ## ★ 权重合计必须 ≤ 1000‰（错误 2 的机械防线）

    千分位权重的语义是"占全部的多少"。合计 > 1000‰ 意味着
    **要分的比总量还多** —— 而 `floor(total × w / 1000)` 会和 `w` 一起放大：

        weights = [1000, 1000]（合计 2000‰）, total = 10
        floor  = [10, 10]，合计 20          ← 超发一倍
        unallocated = 10 - 20 = **-10**      ← 负值被原样送前端

    实测确认过：`next_batch_allocation` 路径（**不做归一化**）上，
    `batch_total=10` 会分出 20 条。危险的是它**不报错** ——
    账面合计"看起来"自洽，只有 `unallocated` 是负的，而前端不拦负数。

    这里拒绝而不是"自动归一"：归一化是**调用方的语义决策**
    （`dimension_allocation` 会做并写明 note），纯函数不该替它改输入。
    """
    seen: set[str] = set()
    for item in items:
        if item.key in seen:
            raise ValueError(f"分配条目的 key 重复：{item.key!r}")
        seen.add(item.key)
        if item.weight_permille < 0:
            raise ValueError(
                f"分配条目的权重不能为负：{item.key!r} = {item.weight_permille}"
            )
        if item.capacity is not None and item.capacity < 0:
            raise ValueError(
                f"分配条目的容量不能为负：{item.key!r} = {item.capacity}"
            )

    weight_total = sum(item.weight_permille for item in items)
    if weight_total > PERMILLE_TOTAL:
        raise ValueError(
            f"权重合计 {weight_total}‰ 超过 {PERMILLE_TOTAL}‰ —— "
            "占比总和不可能超过全部；继续分配会让分配额超过总数"
            f"（本批共 {weight_total - PERMILLE_TOTAL}‰ 的超发空间）。"
            "调用方应先归一化（见 permille.normalize_permille）。"
        )


def _remainder_of(item: AllocationItem, total: int) -> int:
    """`total × w mod 1000` —— 余数的**唯一**计算口径。

    ⚠️ **本包实际踩过一次**：平局排序曾写成 `item.weight_permille % PERMILLE_TOTAL`。
    因为权重都 < 1000，那个式子**恒等于权重本身** —— 于是"按余数降序"
    静默退化成了"按权重大小降序"，两者在多数输入下**给出相同结果**，
    只在"大权重条目余数为 0、小权重条目余数很大"时才分叉。
    实测样本：`[300,250,200,150,60]` × total=10 ⇒ 正确解 `[3,3,2,1,1]`，
    错误解 `[4,3,2,1,0]` —— **两者合计都是 10**。

    因此余数只允许由这一个函数计算，`_tie_key` 与两个算法路径都调它。
    """
    return total * item.weight_permille % PERMILLE_TOTAL


def _tie_key(item: AllocationItem, total: int) -> tuple[int, int, str]:
    """三级平局次序的排序键（**升序排**，因此余数取负）。

    与模块 docstring 第三节一一对应：
    余数大的优先（`-remainder`）→ `sort_order` 小的优先 → `key` 字典序。

    **`total` 是必需参数**（见 `_remainder_of` 的踩坑记录）：
    余数是 `total` 的函数，不是权重的函数。
    """
    return (-_remainder_of(item, total), item.sort_order, item.key)


def _largest_remainder(
    items: Sequence[AllocationItem], total: int
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """最大余数法（**无容量约束**路径）。

    返回 `(分配额, 整数部分, 余数)`。

    算法（C19 §3.7 第 4 条）：

      1. `floor_part = total × w // 1000`；
      2. `remainder = total × w % 1000`（**整数余数**，不是小数）；
      3. 未分完的份额按"余数降序 → sortOrder 升序 → key 字典序"逐条 +1；
      4. 权重为 0 的条目**不参与补 1**（否则"停用"的场景会白拿一条）。

    第 4 条是本包的一处显式取舍：C19 只说"按余数补齐"，没说 zero-weight 怎么办。
    权重 0 的余数也是 0，因此它排在最后；只有当"所有正权重条目都已 +1
    且仍有剩余"时才会轮到它 —— 而那意味着权重合计 < 1000（数据不一致）。
    显式排除它，让"停用 = 不参与"成为一条可断言的性质。
    """
    allocated: dict[str, int] = {}
    floors: dict[str, int] = {}
    remainders: dict[str, int] = {}

    for item in items:
        floor_part = total * item.weight_permille // PERMILLE_TOTAL
        allocated[item.key] = floor_part
        floors[item.key] = floor_part
        remainders[item.key] = _remainder_of(item, total)

    leftover = total - sum(allocated.values())
    if leftover > 0:
        eligible = [item for item in items if item.weight_permille > 0]
        eligible.sort(key=lambda item: _tie_key(item, total))
        for item in eligible:
            if leftover == 0:
                break
            allocated[item.key] += 1
            leftover -= 1

    return allocated, floors, remainders


def _distribute_bonus(
    grant: dict[str, int],
    room: dict[str, int],
    active: Sequence[AllocationItem],
    total: int,
    bonus: int,
) -> None:
    """把一轮里"没分完的额度"按三级次序逐条补 1（**原地修改 `grant`**）。

    三级次序由 `_tie_key` 给出（余数大的优先 → `sort_order` 小的优先 →
    `id` 字典序）—— 与无容量路径的补位次序同源，这样"两个维度的平局
    处理"不会各自演化。

    ## 为什么单独一个函数

    这段有**独立的语义**："余数怎么用"这件事在两条路径（无容量 / 有容量）
    里是同一个规则，而它嵌在削峰循环里时读起来像"循环的一部分"。
    拆出来的另一个好处是嵌套深度：留在原处是 4 层（while → if → for → if），
    拆出后主循环是 2 层。

    **必须传 `room`**：补 1 不能把条目顶过容量 —— 那是削峰的逆操作，
    不判 `room` 会让"容量 5"的条目在补位阶段拿到第 6 条。
    """
    for item in sorted(active, key=lambda it: _tie_key(it, total)):
        if bonus <= 0:
            return
        if grant[item.key] < room[item.key]:
            grant[item.key] += 1
            bonus -= 1


def _capacity_shave(
    items: Sequence[AllocationItem], total: int
) -> tuple[dict[str, int], dict[str, int], dict[str, int], int, int]:
    """容量削峰（**有容量约束**路径）。

    返回 `(分配额, 整数部分, 余数, 被削掉的累计量, 轮数)`。

    算法（C14 原文）：

    > 先按权重分，超容量的削到容量，削下来的再按权重分给还有余量的场景，
    > 反复直到分完或全部满容。**全部满容时剩余篇数计入 `unallocated`，不硬塞。**

    ## 实现要点：`pending` 一个变量就够了

    不需要单独追踪"被削下来的量"。每一轮的待分配额 `pending`
    （初始 = `total`）里，凡是"没分出去的"都自然留到下一轮，
    其中就包含"因为容量被削减掉的那部分"与"整数取整的余数"。
    两者进入下一轮的方式完全相同（都是"还有额度没花掉"），
    **分开追踪只会多一处可能算错的地方**。

    因此循环体是纯函数式的：

        room   = 每条目还能放多少（容量 - 已分配）
        fair   = pending 按权重分给每条目应得的量
        grant  = min(fair, room)          ← 削峰就发生在这一步
        grant += 补 1（三级次序，且不超 room）
        pending = pending - sum(grant)

    ## 收敛性（为什么不会死循环）

    `active` **只收正权重条目**（见下节"停用项不参与"），因此：

    - `active` 非空 ⇒ `weight_sum > 0` ⇒ 按权重算出的 `fair` 有依据；
    - 补 1 那一步**至少补出 1 条**：`bonus > 0` 时 `active` 里第一条
      `room > 0` 就能 `+1`（`active` 里每条 `room > 0`）；
    - 若某轮 `sum(grant)` 没涨满 `pending`，`pending` 仍会因补 1 而**严格递减**。

    即便出现"补 1 补不进去"（`active` 里所有条目都已满容），那也是因为
    这些条目的 `room` 在本轮被填满，下一轮它们退出 `active` ⇒
    **`active` 只减不增** ⇒ 轮数 ≤ 条目数。

    `_MAX_SHAVE_ROUNDS` 只是"实现写错时让异常显式出现"的护栏。

    ## 停用项不参与（**两条路径必须一致**）

    权重为 0 的条目**不作为分配目标**，理由与 `_largest_remainder` 第 4 条同源：
    "停用 = 不参与"必须是一条可断言的性质。

    ⚠️ **本包实测踩过一次**（错误 5）：曾经的 `active` 只判 `room > 0`，
    于是"正权重条目被容量顶满 + 另有停用项"时，`active` 里只剩停用项，
    走了 `fair = pending // len(active)` 的兜底分支 ⇒ **停用场景照样分到内容**：

        场景A 启用(1000‰, 容量 5) + 场景B 停用(0‰)
        total=10  ⇒  A=5, B=5      ← B 已被点「停用」却拿到 5 条

    这个缺陷的危害高于它引出的死循环：错的是**产出**（给停用场景排了内容），
    且账面合计照样等于 `total` —— 正是本模块开头要防的"账面看不出"。

    因此 `active` 的判据是 **`room > 0 且 weight_permille > 0`**，
    并且**全零权重（全部停用）时 `active` 为空 ⇒ 直接 `break`**，
    `pending` 全部进 `unallocated`。这与无容量路径的读数一致。


    `rounds` 与 `shaved` 都返回，原因是**它们是可解释性数据**：
    "为什么这一批分不完"要求能说出"削了几轮、削掉多少"。
    """
    allocated = {item.key: 0 for item in items}
    floors = {item.key: 0 for item in items}
    remainders = {item.key: 0 for item in items}

    # `capacity is None` ⇒ 无上限。用 `total` 作为等价上界：
    # "本批不可能给某一类分超过 total 条"，语义完全等价，
    # 而这样后面就不必到处判 None。
    limits = {
        item.key: (item.capacity if item.capacity is not None else total)
        for item in items
    }

    pending = total
    shaved = 0
    rounds = 0

    while pending > 0:
        rounds += 1
        if rounds > _MAX_SHAVE_ROUNDS:
            raise RuntimeError(
                "容量削峰未能收敛（轮数超过 "
                f"{_MAX_SHAVE_ROUNDS}）—— 这是实现缺陷，不是输入问题"
            )

        room = {
            item.key: max(limits[item.key] - allocated[item.key], 0)
            for item in items
        }
        # `active` 必须同时满足两个条件（理由见 docstring"停用项不参与"）：
        #   room > 0            —— 还有容量可放；
        #   weight_permille > 0 —— 没被停用。
        # 只判 room 会让"正权重条目满容 + 另有停用项"时把额度分给停用项。
        active = [
            item for item in items if room[item.key] > 0 and item.weight_permille > 0
        ]
        if not active:
            # 全部满容，**或**剩下的都是停用项（权重 0）。
            # 两种情形都按 C14 原文处理：剩余计入 unallocated，不硬塞。
            break

        weight_sum = sum(item.weight_permille for item in active)
        assert weight_sum > 0, "active 已排除零权重条目，此处不该出现零合计"
        grant: dict[str, int] = {item.key: 0 for item in active}

        for item in active:
            fair = pending * item.weight_permille // weight_sum
            granted = min(fair, room[item.key])
            grant[item.key] = granted
            # 削峰量 = "按权重本应得"超出"实际能放"的部分。
            if fair > granted:
                shaved += fair - granted

        # 同一轮里"没分完的额度"用三级次序逐条补 1（不超各条目的 room）。
        # 这里不再需要 `weight_sum > 0` 的门：active 已保证合计为正。
        bonus = pending - sum(grant.values())
        if bonus > 0:
            _distribute_bonus(grant, room, active, total, bonus)

        for item in active:
            allocated[item.key] += grant[item.key]
            floors[item.key] += grant[item.key]

        pending -= sum(grant.values())

    # 余数只用于**可解释性**（原型「余数排序」列）。容量路径下的分配不是
    # 由余数决定的，因此这里按"整批一次的余数"填 —— 它是运营能对得上的
    # 那个数字（`total × w mod 1000`），而不是某一轮的中间量。
    for item in items:
        remainders[item.key] = _remainder_of(item, total)

    return allocated, floors, remainders, shaved, rounds


def allocate_by_weight(
    items: Sequence[AllocationItem], total: int
) -> AllocationResult:
    """把 `total` 条按权重分配给 `items`。**纯函数**。

    **不变量（证据脚本穷举断言的就是它）**：

        result.allocated_total + result.unallocated == total

    `total` 为 0 时返回全 0 的结果（不报错）—— 运营可以把某轮总量设为 0
    来做"只配置不生产"的预演。

    路径选择见模块 docstring 第二节。
    """
    if total < 0:
        raise ValueError(f"总数不能为负：{total}")
    _check_items(items)

    if not items:
        return AllocationResult(total=total, lines=(), unallocated=total)

    has_capacity = any(item.capacity is not None for item in items)

    if not has_capacity:
        allocated, floors, remainders = _largest_remainder(items, total)
        unallocated = total - sum(allocated.values())
        rounds = 1
        notes: tuple[str, ...] = ()
    else:
        allocated, floors, remainders, shaved, rounds = _capacity_shave(items, total)
        unallocated = total - sum(allocated.values())
        notes = (f"容量削峰 {rounds} 轮", f"累计削峰 {shaved} 条")

    # 补 1 的次序（`tie_rank`）：按三级次序排序后，**实际拿到补 1 的那些**
    # 依次编号。没拿到的是 0。
    #
    # 为什么 rank 只给"拿到的人"：原型「余数排序」列写的是"第 1 位 +1"、
    # "第 2 位 +1"、"额度已用尽" —— 也就是说 rank 是**分配动作的顺序**，
    # 不是"所有人的余数排名"。给所有人排名会让"排第 3 但没拿到"看起来
    # 像是异常，而它其实是容量/额度用尽的正常结果。
    #
    # ⚠️ **容量路径下 `tie_rank` 恒为 0、`floor_part` 恒等于 `allocated`**
    # （MINOR-3 的处置说明，不是缺陷但必须写清）：
    # `_capacity_shave` 每轮把 `grant` 同时累加进 `allocated` 与 `floors`，
    # 因此循环结束时 `floors == allocated` ⇒ 下面 `floors < allocated` 永不成立。
    # 也就是说原型那张"最大余数法换算过程"表**只在无容量路径上有意义**；
    # 容量路径的可解释性由 `notes`（削峰轮数 / 累计削峰）与
    # `capacity_shaved`（该条是否被容量顶满）承担，两者都是实测可核的。
    # 不把 `tie_rank` 硬凑成非 0：那需要一个"容量路径下什么算次序"的定义，
    # 而 C14 原文没给 —— 编一个比留 0 更糟（前端会照它渲染"第 N 位 +1"）。
    rank_order = sorted(items, key=lambda item: _tie_key(item, total))
    tie_rank: dict[str, int] = {item.key: 0 for item in items}
    cursor = 0
    for item in rank_order:
        if floors[item.key] < allocated[item.key]:
            cursor += 1
            tie_rank[item.key] = cursor

    lines = tuple(
        AllocationLine(
            key=item.key,
            allocated=allocated[item.key],
            weight_permille=item.weight_permille,
            capacity=item.capacity,
            sort_order=item.sort_order,
            floor_part=floors[item.key],
            remainder=remainders[item.key],
            tie_rank=tie_rank[item.key],
            capacity_shaved=(
                item.capacity is not None and allocated[item.key] >= item.capacity
            ),
        )
        for item in items
    )

    return AllocationResult(
        total=total,
        lines=lines,
        unallocated=unallocated,
        rounds=rounds,
        notes=notes,
    )


def allocate_from_remaining(
    items: Sequence[AllocationItem],
    allocated_so_far: dict[str, int],
    *,
    batch_total: int,
) -> AllocationResult:
    """子配额：把 `batch_total` 条按权重分配，但**从剩余额度里分**（C19）。

    `allocated_so_far` 是"该项目在此维度上已经分配掉的累计量"。
    本函数把它转成"每条目剩余可用容量"，再委托 `allocate_by_weight`。

    ## 为什么是"扣减容量"而不是"扣减权重"

    两条路都能让本批不超总计划，但只有扣减容量能满足 C19 的
    「不要求每个小批次都完全符合总批次比例，但**累计配额必须收敛到总计划**」：

    - **扣减权重**（`w' = max(w - 已分, 0)`）：第 2 批的权重变成
      "总权重减去第 1 批"，于是第 2 批内部的比例被前一批扭曲 ——
      前一批分得多的类，后一批权重被削得更多 ⇒ **累计会偏移**。
    - **扣减容量**：权重不变（比例意图不变），只是"这个类最多还能放多少条"
      变小了。这正是 C19 那句"列和恒等于总计划"的机械落点。

    传 `capacity=None` 的条目视为**无上限**；本函数给它的"剩余容量"是
    `batch_total`（即本批不可能超过它）—— 保持"无上限"语义。

    **`batch_total` 必须 ≤ 全部条目的剩余容量之和 ＋ 无上限条目数 × batch_total**，
    否则结果里会有 `unallocated > 0` —— 那是正确的结果，不是错误（C14）。
    """
    adjusted: list[AllocationItem] = []
    for item in items:
        used = allocated_so_far.get(item.key, 0)
        if item.capacity is None:
            adjusted.append(item)
            continue
        remaining_capacity = item.capacity - used
        if remaining_capacity < 0:
            raise ValueError(
                f"条目 {item.key!r} 的已分配量（{used}）已超过其容量"
                f"（{item.capacity}）—— 累计配额已经越界"
            )
        adjusted.append(replace(item, capacity=remaining_capacity))

    return allocate_by_weight(adjusted, batch_total)


def capacity_remaining(
    items: Sequence[AllocationItem], allocated_so_far: dict[str, int]
) -> dict[str, int | None]:
    """每条目还剩多少可用容量（`None` = 无上限）。

    单独暴露这个函数，是因为"剩余额度"是**前端与后端都要用到的同一个量**
    （前端要显示"这一类还能加多少条"）。若两处各算一遍，
    算法改一次就会有一处漏改，而**症状是前端显示的数字与后端校验的数字不同**
    —— 与本项目已踩过的"两套真相"同型。
    """
    result: dict[str, int | None] = {}
    for item in items:
        if item.capacity is None:
            result[item.key] = None
        else:
            result[item.key] = max(item.capacity - allocated_so_far.get(item.key, 0), 0)
    return result


__all__ = [
    "allocate_by_weight",
    "allocate_from_remaining",
    "capacity_remaining",
]
