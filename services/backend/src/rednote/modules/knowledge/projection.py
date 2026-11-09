"""知识版本状态的**纯函数投影**（WP20）。

规格源 02 §12 原文（交接文档 §4 引）：

> `DRAFT → PENDING → ACTIVE → SUPERSEDED/ARCHIVED`；
> **恢复通过新事件，不篡改旧状态历史**

最后一句决定本模块的形态：**状态不是一列被就地改写的值，而是事件流的函数**。

---

## 为什么必须是纯函数

三条理由，缺一条都可以不要它：

1. **验收原文要求它**：「归档可追溯，**生效版本与旧引用并存**」。
   若状态存成 `asset.state` 这样一列，`RESTORE` 的那一刻就必须
   把 `ARCHIVED` 改回 `ACTIVE` —— 改完之后，"这条知识曾经归档过"这件事
   **只存在于 publication 事件表里，而当前状态已经看不到它了**。
   于是"归档可追溯"要靠人去翻事件表才能验证，而不是靠当前状态。

2. **它要能被单独测透**。投影的正确性（尤其是 ARCHIVE/RESTORE 交叉、
   多次激活、SUPERSEDE 的传递）与数据库无关。做成纯函数之后，
   这些情形可以用**构造出来的事件序列**穷举测试，不必建库、不必造数据。
   本包的纯函数测试覆盖 20+ 个事件序列组合，全部零 I/O。

3. **服务层与读路径必须用同一套判据**。`service.publish()` 需要知道
   "现在能不能 ARCHIVE"（不能重复归档），而 `GET /knowledge/assets/{id}`
   需要知道"现在这个 asset 是什么状态"。两处各写一份 if 迟早漂移 ——
   漂移的方向是"写入侧以为可以、读取侧显示成另一个状态"，
   而**不会有任何报错**。

## `sequence` 而不是 `created_at`

排序键用 events 里自带的 `sequence`（同一 asset 内从 1 开始）。
**不能用时间戳**：`now()` 在 PostgreSQL 里是**事务开始时刻**，同一事务内
插入的多条事件时间戳完全相同。用 `created_at` 排序时，
`ACTIVATE(new)` 与 `SUPERSEDE(old)` 谁先谁后是不确定的 ——
而投影对顺序敏感（见 `project_versions` 的说明）。

## ARCHIVE 语义：**整条 asset 退出可用，而不是"当前版本作废"**

这是本模块最容易做错的一处，判据来自验收原文的
「生效版本与**旧引用**并存」：

  · 「旧引用」指的是**别的版本（内容/方案）曾经引用过的 knowledge 版本**；
  · 归档一条知识之后，那些引用**仍然要能解析到内容**；
  · 若归档把 `active_version_id` 清空，`GET /knowledge/assets/{id}?versionId=X`
    仍能取到内容（版本不可变），但"这条知识当前是什么"就没有答案了。

因此本实现的 ARCHIVE **只置 `archived=True`，不动 `active_version_id`**。
`RESTORE` 只把 `archived` 置回 False。两次操作都**只增事件、不改历史**，
于是"归档过又恢复"这条轨迹完整地留在事件流里（`archived_between`）。

**取舍已登记为待定项**：另一种合理读法是"归档 = 废止当前生效版本"。
本包选前者，理由是它使"旧引用并存"与"归档可追溯"同时成立；
后一种读法会让归档变成一次不可逆的版本废止。
"""

from __future__ import annotations

from typing import Any

from rednote.modules.knowledge.types import (
    ACTION_ACTIVATE,
    ACTION_ARCHIVE,
    ACTION_RESTORE,
    ACTION_SUPERSEDE,
    ASSET_STATE_ACTIVE,
    ASSET_STATE_ARCHIVED,
    ASSET_STATE_DRAFT,
    ASSET_STATE_INDEXING,
    PUBLICATION_ACTIONS,
    VERSION_STATE_ACTIVE,
    VERSION_STATE_DRAFT,
    VERSION_STATE_SUPERSEDED,
    AssetProjection,
    PublicationEvent,
    VersionProjection,
)


def project_versions(
    events: Any,
    *,
    version_ids: Any = (),
    index_ready: bool = True,
) -> AssetProjection:
    """把 publication 事件流投影成"现在是什么状态"。

    参数：
      · `events` —— 按 `sequence` **升序**的 `PublicationEvent` 序列
        （调用方负责排序；本函数不重排，因为"顺序从哪来"是存储层的
        责任，而本函数要能被喂任意构造的序列做测试）；
      · `version_ids` —— 该 asset 的**全部**版本 id（含从未发布过的草稿）。
        必须传进来：一个从未被任何事件提到的版本，它的状态是 `DRAFT`，
        而这一点无法从事件流自身推出来（事件流里没有它）。
      · `index_ready` —— 索引是否已建好（见下方 INDEXING 的判据）。

    返回 `AssetProjection`。

    ## 算法

    单次线性扫描，维护三样东西：
      · `active_version_id` —— 当前生效版本；
      · `archived` —— 是否处于归档态；
      · `per_version` —— 每个版本的状态。

    每个事件做什么：

    | action | 对 asset | 对版本 |
    |---|---|---|
    | `ACTIVATE(v)` | `active = v`；`archived = False` | `v → ACTIVE`；**原 active 若存在且 != v → SUPERSEDED** |
    | `ARCHIVE` | `archived = True` | 不变（见模块 docstring 的 ARCHIVE 语义） |
    | `RESTORE` | `archived = False` | 不变 |
    | `SUPERSEDE(v)` | 不变 | `v → SUPERSEDED` |

    **`ACTIVATE(v)` 要顺带 SUPERSEDE 旧版本**（而不是等一条显式的
    SUPERSEDE 事件）：规格源 §277 的原文是"插入 `ACTIVATE` 事件，
    **切换** `active_version`"—— "切换"本身蕴含"旧的不再生效"。
    若要求调用方额外发一条 SUPERSEDE，就存在"忘发"的窗口：
    那一刻会有两个版本同时是 ACTIVE，而没有 `active_version_id` 能
    同时指向它们——但投影结果会自相矛盾（多个 ACTIVE），
    下游取"哪个是当前版本"就会随查询顺序变化。

    **`ACTIVATE` 也会清掉 `archived`**：这是"恢复"的第二种走法
    （直接激活一个新版本，而不是先 RESTORE）。规格源 §277 只描述了
    ACTIVATE 这一条路径，因此它必须能让知识从归档态回到可用态 ——
    否则"归档后再发新版"需要客户端记得先 RESTORE 再 ACTIVATE，
    而漏掉任一步都会得到一条**永远显示为已归档**的新版本。

    ## INDEXING（规格源 §277 的"已生效／索引处理中"）

    它是 **asset 级**状态：`active_version_id` 已确定（精确读取可用），
    但索引 Outbox 事件尚未投递完成（语义检索暂时召回不到它）。
    判据：`index_ready=False` 且当前是 `ACTIVE`。

    **为什么不让它进版本状态链**：见 `types.py` 里
    `VERSION_STATE_*` 的说明 —— 规格源把 PENDING 放在版本链上，
    但同一份规格源的 §277 又说"索引尚未完成时**知识状态**显示"
    （明写是 asset 级）。两者放一起会矛盾：一个版本若因索引未完成
    而处于 PENDING，那"谁是当前生效版本"就没有答案了。取舍已登记。
    """
    per_version: dict[Any, VersionProjection] = {
        vid: VersionProjection(version_id=vid, state=VERSION_STATE_DRAFT)
        for vid in version_ids
    }
    active_version_id: Any | None = None
    archived = False
    activation_no = 0

    for event in events:
        action = event.action
        if action not in PUBLICATION_ACTIONS:
            # 未知动作**显式报错**：静默忽略会让一条拼错的事件
            # 消失在投影里，于是"事件写了但状态没变"无法被发现。
            raise ValueError(
                f"未知的发布动作：{action!r}。"
                f"允许值：{list(PUBLICATION_ACTIONS)}（规格源 02 §8）。"
            )

        if action == ACTION_ACTIVATE:
            _activate(
                version_id=event.version_id,
                previous=active_version_id,
                per_version=per_version,
                activation_no=activation_no,
            )
            active_version_id = event.version_id
            activation_no += 1
            archived = False
        elif action == ACTION_ARCHIVE:
            archived = True
        elif action == ACTION_RESTORE:
            archived = False
        elif action == ACTION_SUPERSEDE:
            # 只影响被点名的版本。若它正是当前生效版本，
            # **不动 `active_version_id`** —— 那会让"当前有什么在生效"
            # 悬空。规格源 §277 的切换只能由 ACTIVATE 做。
            _mark(per_version, event.version_id, VERSION_STATE_SUPERSEDED)

    return AssetProjection(
        active_version_id=active_version_id,
        archived=archived,
        state=_asset_state(
            active_version_id=active_version_id,
            archived=archived,
            index_ready=index_ready,
        ),
        versions=per_version,
    )


def _activate(
    *,
    version_id: Any,
    previous: Any | None,
    per_version: dict[Any, VersionProjection],
    activation_no: int,
) -> None:
    """激活一个版本，并把原生效版本标为 SUPERSEDED。"""
    if previous is not None and previous != version_id:
        _mark(per_version, previous, VERSION_STATE_SUPERSEDED)
    current = per_version.get(version_id)
    per_version[version_id] = VersionProjection(
        version_id=version_id,
        state=VERSION_STATE_ACTIVE,
        activation_no=activation_no + 1,
    )
    del current  # 明确：重建而不是就地改（`VersionProjection` 是 frozen）


def _mark(
    per_version: dict[Any, VersionProjection], version_id: Any, state: str
) -> None:
    """把一个版本标成某状态。**已在期望状态时是空操作**（幂等）。

    幂等是必要的：`SUPERSEDE(old)` 可能在 `ACTIVATE(new)` 之后**再**来一次
    （数据回填、事件重投），此时 `old` 已经是 SUPERSEDED ——
    若这里覆盖 `activation_no`，历史"它是第几个生效的"就被抹掉了。
    """
    existing = per_version.get(version_id)
    if existing is None:
        # 事件指向一个不在 `version_ids` 里的版本：数据不一致。
        # **不静默新建**（那会掩盖"事件表引用了不存在的版本"这类问题），
        # 而是记成一个 DRAFT 之外的显式项，交给调用方按需要处理。
        per_version[version_id] = VersionProjection(
            version_id=version_id, state=state
        )
        return
    if existing.state == state:
        return
    per_version[version_id] = VersionProjection(
        version_id=version_id,
        state=state,
        activation_no=existing.activation_no,
    )


def _asset_state(
    *, active_version_id: Any | None, archived: bool, index_ready: bool
) -> str:
    """asset 的显示状态。

    **优先级：归档 > 索引中 > 生效 > 草稿。**
    归档排最前 —— 一条已归档的知识无论索引状态如何，
    对用户而言都是"不在了"；显示成"索引处理中"会让人以为它即将可用。
    """
    if archived:
        return ASSET_STATE_ARCHIVED
    if active_version_id is None:
        return ASSET_STATE_DRAFT
    if not index_ready:
        return ASSET_STATE_INDEXING
    return ASSET_STATE_ACTIVE


def allowed_actions(projection: AssetProjection) -> frozenset[str]:
    """当前投影下**还有意义的**发布动作（供端点做前置校验与提示）。

    判据是"这个动作会不会改变任何东西"：

    | 动作 | 何时无意义 |
    |---|---|
    | `ACTIVATE` | 目标版本已经是当前生效版本（重复激活） |
    | `ARCHIVE` | 已经归档 |
    | `RESTORE` | 没有归档 |
    | `SUPERSEDE` | 目标版本已经 SUPERSEDED |

    **为什么不禁掉全部无意义动作、只禁这几种**：
    `ACTIVATE` 同一个版本两次在并发重投下是正常现象（客户端超时重试），
    而"已经 ACTIVE"意味着结果与意图一致 —— 此时报错会让重试**永远失败**。
    但"重复归档"不同：它每次都会写一条新事件，于是事件表里
    出现一串同义事件，`archived_between` 的追溯会变得难读。
    这条边界取舍已登记为待定项。
    """
    allowed: set[str] = {ACTION_ACTIVATE}
    if not projection.archived:
        allowed.add(ACTION_ARCHIVE)
    else:
        allowed.add(ACTION_RESTORE)
    allowed.add(ACTION_SUPERSEDE)
    return frozenset(allowed)


def supersede_targets(projection: AssetProjection) -> tuple[Any, ...]:
    """可以被 SUPERSEDE 的版本（历史上生效过、当前不是当前生效版本）。

    规格源把 `SUPERSEDE` 列为客户端可选动作的一种，但它对
    "当前生效版本"使用是无意义的（那个版本由下一次 ACTIVATE 自动取代）。
    """
    return tuple(
        vid
        for vid, item in projection.versions.items()
        if item.state == VERSION_STATE_SUPERSEDED
    )


__all__ = [
    "allowed_actions",
    "project_versions",
    "supersede_targets",
]