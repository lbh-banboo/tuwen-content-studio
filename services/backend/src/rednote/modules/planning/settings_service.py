"""配置的读取与变更（ADR-13 + 本包对 OP-60 的全部机制）。

**本模块是 `runtime_settings` 的唯一写入口，也是唯一读入口。**
`repository.py` 提供存储原语（取当前行、追加、枚举项目），
但"什么值合法"、"变更后谁要跟着变"、"什么时候惰性补副本"全部在这里。

---

## 一、对外函数（下游消费的契约）

| 函数 | 消费者 |
|---|---|
| `get_effective_limits(repo, key, project_id=None) -> EffectiveSetting` | WP25 / WP31 / WP32 |
| `change_global_setting(repo, …)` | 管理侧端点 |
| `change_project_setting(repo, …)` | 运营侧端点 |
| `list_diverged_projects(repo, key) -> list[DivergedProject]` | `GET /settings/projects/diverged` |
| `bootstrap_defaults(repo, …)` | 迁移与测试（写入初始 `GLOBAL` 行 + 回填存量项目副本） |

## 二、★ OP-60 的三条机制（老大拍板"支持且必须有项目级副本"）

| 机制 | 在哪里落地 | 判据 |
|---|---|---|
| **同步**（全局变更同事务级联刷新副本） | `change_global_setting` 的步骤 ② | J2 |
| **分叉显式化**（`source`/`global_value`/`diverged` 四条一起返回） | `get_effective_limits` + `list_diverged_projects` | J3 |
| **副本首次产生不老跨包**（惰性物化兜底） | `get_effective_limits` 的 `_materialize_inherited_copy` | J1 |

## 三、并发：按 key 的 advisory lock（见 `repository.lock_setting_key`）

三条写路径（全局变更 / 项目覆盖 / 惰性物化）在写之前都取同一把锁。
**没有它，副本刷新会静默丢写**：

    T1 全局 → V2，T2 全局 → V3 并发，项目 P 尚无副本
    · 两者都算得 P 的下一修订号 = 1
    · T2 插入被部分唯一索引拒绝 ⇒ 若用 ON CONFLICT DO NOTHING 就**静默跳过**
    结果：全局 V3、项目 P 副本 V2 —— 两值都在、都不报错，正是 J2 要拦的形态

因此本模块**不用 `ON CONFLICT DO NOTHING`**，而是"拿锁 → 普通插入"，
让真正的冲突以异常暴露（那是缺陷，不是常态）。

## 四、`revision_no` 的取号重试

即便有锁，**跨进程/跨连接**的取号仍有理论上的竞争窗口：
拿锁与取号之间不是一个原子操作里读到的（锁保证了同一时刻只有一个写入者，
但**同一把锁在不同事务里串行**，取号发生在锁内 ⇒ 实际已安全）。

保留重试循环的理由是**防御性**：若将来有人把某个写路径改成不加锁，
重试能让它退化为"慢但正确"，而不是"静默丢写"。
`MAX_SETTING_ATTEMPTS` 次都用完说明有真缺陷，抛错而不是继续。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final, Mapping

from sqlalchemy.exc import IntegrityError

from rednote.modules.planning.repository import PlanningRepository
from rednote.modules.planning.types import (
    MAX_CHANGE_REASON_CHARS,
    PLACEHOLDER_SETTING_KEYS,
    REASON_ACTOR_NOT_FOUND,
    REASON_BATCH_KIND_UNKNOWN,
    REASON_CHANGE_REASON_REQUIRED,
    REASON_PROJECT_NOT_FOUND,
    REASON_SETTING_KEY_UNKNOWN,
    REASON_SETTING_NOT_CONFIGURED,
    REASON_SETTING_VALUE_INVALID,
    SETTING_KEY_BATCH_LIMITS,
    SETTING_KEYS,
    SETTING_ORIGIN_GLOBAL,
    SETTING_ORIGIN_INHERITED,
    SETTING_ORIGIN_MANUAL,
    SETTING_SCOPE_GLOBAL,
    SETTING_SCOPE_PROJECT,
    BATCH_KINDS,
    DEFAULT_BATCH_MAX,
    MAX_QUANTITY_BOUND,
    MIN_QUANTITY,
    EffectiveSetting,
    parse_batch_limits,
)
from rednote.platform.errors import ApiError, ErrorCode

#: 取号重试次数。见模块 docstring 第四节。
MAX_SETTING_ATTEMPTS: Final[int] = 5


# ---------------------------------------------------------------------------
# 错误构造
# ---------------------------------------------------------------------------


def _validation_error(field: str, message: str, *, reason: str, **extra: Any) -> ApiError:
    """构造 422 `VALIDATION_FAILED`，带可定位的 `fieldErrors`。

    与 WP11/WP22/WP23 同一形状：前端按 `field` 高亮输入框，
    自由文本它没法用。
    """
    details: dict[str, Any] = {
        "fieldErrors": [{"field": field, "type": reason, "message": message}],
        "reason": reason,
    }
    details.update(extra)
    return ApiError(ErrorCode.VALIDATION_FAILED, message, details=details)


def _not_found(message: str, *, reason: str, **extra: Any) -> ApiError:
    details: dict[str, Any] = {"reason": reason}
    details.update(extra)
    return ApiError(ErrorCode.RESOURCE_NOT_FOUND, message, details=details)


# ---------------------------------------------------------------------------
# 值校验：按 key 分派（R8 的处置 —— 每个键有自己的校验模型）
# ---------------------------------------------------------------------------


def ensure_setting_key(key: str) -> None:
    """键名合法性（**纯函数，不查库**）。非法即抛 422。

    ## 为什么单独暴露（MINOR-7 的修复）

    `get_global_setting_history` 端点需要"未知键 ⇒ 422 而不是空列表"，
    此前它靠调 `get_effective_limits(…, materialize=False)` **当校验用** ——
    返回值被丢弃，但那次调用**真的查了库**，随后 `_history_body`
    再查一次 `list_history`。于是"不为未知键先查历史"这句注释是假的：
    它确实查了（只是顺序反了）。

    现在三处（`validate_setting_value` / `get_effective_limits` / 该端点）
    共用这一个判定，既没有多余往返，键的合法取值也仍是单点。
    """
    if key not in SETTING_KEYS:
        raise _validation_error(
            "key",
            f"未知的配置键 {key!r}（合法取值：{sorted(SETTING_KEYS)}）。",
            reason=REASON_SETTING_KEY_UNKNOWN,
        )


def validate_setting_value(key: str, value: Mapping[str, Any] | None) -> dict[str, Any]:
    """按 key 校验并**规范化**配置值，返回可直接落库的 dict。

    **为什么每个键一个校验分支，而不是一个通用的"是不是 dict"**：
    R8 已识别 `runtime_settings` 天生是"万能杂货表"。取值域（`SETTING_KEYS`）
    只拦住了键名拼错，**拦不住形状错**（`batch.limits` 写成 `{"TRIAL": "十"}`）。
    一个通用校验会让"三档缺一档"表现为"那一档用默认值"，而**不报错**。

    返回**规范化后的副本**而不是原对象：调用方传进来的 dict 可能还带着
    额外键（比如前端多传了一个 `note`），落进 JSONB 就成了永久的垃圾。
    这里只取**认得的**键，多余键**忽略**（而不是报错）——
    理由：本函数同时服务"读取已有值"（历史行里可能真有旧版本的额外键），
    报错会让读路径因为历史数据而失败。

    `None` 表示"清空"：只有占位键允许（见 `PLACEHOLDER_SETTING_KEYS`）。
    """
    ensure_setting_key(key)

    if value is None:
        if key not in PLACEHOLDER_SETTING_KEYS:
            raise _validation_error(
                "value",
                f"{key} 不允许清空（只有占位键 {sorted(PLACEHOLDER_SETTING_KEYS)} "
                "允许「清空」这一状态）。",
                reason=REASON_SETTING_VALUE_INVALID,
            )
        return {}

    if not isinstance(value, Mapping):
        raise _validation_error(
            "value",
            f"{key} 的值必须是对象，收到 {type(value).__name__}。",
            reason=REASON_SETTING_VALUE_INVALID,
        )

    if key == SETTING_KEY_BATCH_LIMITS:
        return _validate_batch_limits(value)
    # 其余键目前是**机制已建、实测值未到**（OP-64）—— 只做形状校验。
    return _validate_positive_int_map(key, value)


def _validate_batch_limits(value: Mapping[str, Any]) -> dict[str, Any]:
    """`batch.limits` 的形状：三档各一个正整数，且都必须存在。

    **缺一档必须报错而不是补默认**（`parse_batch_limits` 的立场一致）：
    静默补默认会让"少写了一档"表现为"那一档用 10 条"，而没人知道。
    """
    missing = sorted(BATCH_KINDS - set(value))
    if missing:
        raise _validation_error(
            "value",
            f"batch.limits 缺少档位：{missing}。三档必须齐全。",
            reason=REASON_SETTING_VALUE_INVALID,
            missing=missing,
        )
    unknown = sorted(set(value) - BATCH_KINDS)
    if unknown:
        raise _validation_error(
            "value",
            f"batch.limits 含未知档位：{unknown}（合法：{sorted(BATCH_KINDS)}）。",
            reason=REASON_BATCH_KIND_UNKNOWN,
            unknown=unknown,
        )

    normalized: dict[str, Any] = {}
    for kind in sorted(BATCH_KINDS):
        raw = value[kind]
        # `bool` 是 `int` 的子类 —— 不显式排除的话 `True` 会被当成 1，
        # 于是"上限 1 条"这种明显的手滑被静默接受（`permille.py` 同款处置）。
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise _validation_error(
                f"value.{kind}",
                f"{kind} 的上限必须是整数，收到 {raw!r}。",
                reason=REASON_SETTING_VALUE_INVALID,
            )
        if raw < MIN_QUANTITY:
            raise _validation_error(
                f"value.{kind}",
                f"{kind} 的上限至少 {MIN_QUANTITY}，收到 {raw}。",
                reason=REASON_SETTING_VALUE_INVALID,
            )
        if raw > MAX_QUANTITY_BOUND:
            raise _validation_error(
                f"value.{kind}",
                f"{kind} 的上限最多 {MAX_QUANTITY_BOUND}，收到 {raw}。"
                "（防的是「把 int 写成 10 的 12 次方」，那会让下游拿它乘单价）",
                reason=REASON_SETTING_VALUE_INVALID,
            )
        normalized[kind] = raw
    return normalized


def _validate_positive_int_map(key: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """通用形状：非空对象、键非空串、值为正数或 null。

    比 `batch.limits` 宽松是**有意的**：这些键的实测形状未定（OP-64），
    现在锁死形状会阻止将来落值。但"值必须是数或 null"这一层仍要守 ——
    它能拦住"把整个文件内容当值写进去"这类显然后果严重的错误。
    """
    if not value:
        raise _validation_error(
            "value",
            f"{key} 的值不能是空对象。",
            reason=REASON_SETTING_VALUE_INVALID,
        )
    normalized: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not raw_key:
            raise _validation_error(
                "value",
                f"{key} 的键必须是非空字符串，收到 {raw_key!r}。",
                reason=REASON_SETTING_VALUE_INVALID,
            )
        if raw_value is None:
            normalized[raw_key] = None
        elif isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise _validation_error(
                f"value.{raw_key}",
                f"{key}.{raw_key} 必须是数或 null，收到 {raw_value!r}。",
                reason=REASON_SETTING_VALUE_INVALID,
            )
        else:
            normalized[raw_key] = raw_value
    return normalized


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def get_effective_limits(
    repo: PlanningRepository,
    key: str,
    project_id: uuid.UUID | None = None,
    *,
    materialize: bool = True,
) -> EffectiveSetting:
    """读出生效值及其来源。**全仓唯一的配置读取路径**。

    ## 语义（表里逐格对应 4 种组合）

    | 全局行 | 项目行 | 返回 |
    |---|---|---|
    | 有 | 无 | `source='GLOBAL'`；`materialize=True` 时先补副本，再返回（`source='INHERITED'`） |
    | 有 | `INHERITED` | `source='INHERITED'`，`value` = 副本值 |
    | 有 | `MANUAL` | `source='MANUAL'`，`global_value` 仍是全局值，`diverged` 反映差异 |
    | 无 | 无 | `configured=False`（占位键的合法状态） |

    ## `project_id=None` 时的行为

    返回**全局视图**：`source='GLOBAL'`、`project_id=None`、`diverged=False`。
    管理侧端点用它显示"全局值是多少"。

    ## `materialize` 为什么存在

    惰性物化**是读路径上的写副作用**。它必须是可关闭的：
      · J1 判据要测"物化前确实没有副本行"（先断言缺失，再断言读取后存在）；
      · 迁移与证据脚本要能只看不写。
    默认 `True`（生产语义），测试按需关掉。
    """
    ensure_setting_key(key)

    global_row = repo.current_global_setting(key=key)
    global_value = None if global_row is None else global_row.value

    if project_id is None:
        return EffectiveSetting(
            key=key,
            configured=global_row is not None,
            value=global_value,
            source=SETTING_ORIGIN_GLOBAL if global_row is not None else None,
            global_value=global_value,
            diverged=False,
            revision_no=None if global_row is None else global_row.revision_no,
            project_id=None,
        )

    # ★ 项目必须存在（MAJOR-3 的修复）。
    #
    # 为什么在**读取**路径也要校验：不校验时，一个不存在的 `project_id`
    # 会走到 `_materialize_inherited_copy` 去插一行 `runtime_settings`，
    # 由外键 `ON DELETE RESTRICT` 拒绝（23503）—— 而 `_is_setting_revision_conflict`
    # 只认那两条**部分唯一索引**的名字，不认 FK ⇒ 原样抛出 ⇒ **500**。
    #
    # 姊妹函数 `change_project_setting` / `change_global_setting` 都校验项目，
    # 只有这里漏了。读取侧的"项目不存在"应该是 404（与写入侧同因同果），
    # 而不是靠 FK 兜底成 500。
    #
    # 顺序：**在取项目行之前**。不存在的项目本就没有项目行，
    # 先校验能让 404 与"未配置"这两个不同语义不互相伪装。
    if not repo.project_exists(project_id):
        raise _not_found(
            f"项目 {project_id} 不存在。",
            reason=REASON_PROJECT_NOT_FOUND,
            projectId=str(project_id),
        )

    project_row = repo.current_project_setting(key=key, project_id=project_id)

    # 惰性物化：项目级当前行不存在（或**从未有过**项目级行）时补一份。
    #
    # **条件是"没有当前项目行"，不是"origin='INHERITED'"**：
    # 若只是"最后一行是 MANUAL"，说明运营显式覆盖过，不该补（覆盖是稳定的）；
    # 完全没有行才是"从未被读过"。
    if project_row is None and global_row is not None and materialize:
        project_row = _materialize_inherited_copy(
            repo,
            key=key,
            project_id=project_id,
            global_row=global_row,
        )

    if project_row is None:
        # 项目级当前行不存在。两种成因**必须分开**，这是本分支唯一容易写错处：
        #
        # ① **全局也没有值**（占位键的合法状态）⇒ `configured=False`。
        # ② **全局有值、只是还没物化**（`materialize=False` 被显式关掉，
        #    或调用方只是想看一眼）⇒ **该项目的生效值就是全局值**，
        #    必须报 `configured=True` + `source='GLOBAL'`。
        #
        # 写成"`project_row is None` ⇒ `configured=False`"是错的（初版如此，
        # 第 2 层判据当场报红）：那会让 `materialize=False` 的读取谎报
        # "本项目尚未配置"，而它明明有一个可用的生效值。
        # 调用方（管理侧总览、证据脚本）据此渲染的会是"未配置"——
        # 与 §3.6.4 "让分叉看得见"的诉求正好相反：把一个**正常状态**
        # 报成**缺失状态**，且不报错。
        #
        # 本 docstring 上方的语义表第一行就是这一条（"全局 有 / 项目 无 ⇒
        # `source='GLOBAL'`"），实现此前与它不一致。
        return EffectiveSetting(
            key=key,
            configured=global_row is not None,
            value=global_value,
            source=SETTING_ORIGIN_GLOBAL if global_row is not None else None,
            global_value=global_value,
            diverged=False,
            revision_no=None if global_row is None else global_row.revision_no,
            project_id=project_id,
        )

    source = project_row.origin
    value = project_row.value
    return EffectiveSetting(
        key=key,
        configured=True,
        value=value,
        source=source,
        global_value=global_value,
        # `diverged` 的定义就是"生效值 ≠ 全局值"。全局行不存在时
        # 不可能是 True（没有"全局值"可比）—— 用 `global_value is not None`
        # 显式收窄，避免 `None != {...}` 意外为 True。
        diverged=global_value is not None and value != global_value,
        revision_no=project_row.revision_no,
        project_id=project_id,
    )


def _materialize_inherited_copy(
    repo: PlanningRepository,
    *,
    key: str,
    project_id: uuid.UUID,
    global_row: Any,
) -> Any:
    """为"从未有过项目级行"的项目补一份 `INHERITED` 副本。**返回补好后的当前行。**

    ## 取锁

    先取按 key 的 advisory lock（见模块 docstring 第三节）。**这把锁同时
    保护"全局变更的级联刷新"** —— 若不加锁，本函数与一次并发的全局变更
    会各自算得同一个修订号，一个插入成功、一个撞唯一约束。

    ## 为什么插入后**必须重读**

    ```python
    # ❌ 错：用"我插入了"推断"我拿到了那行"
    row = repo.insert_inherited_copy(...)
    repo.flush()
    return row          # ← 若别人抢先插过，我的插入会被拒；
                        #   即便没被拒，我拿到的也是一个**未提交**的 ORM 对象

    # ✅ 对：插入（或发现已被插）之后，重新按 revision_no 最大读一次
    repo.flush()
    return repo.current_project_setting(key=key, project_id=project_id)
    ```

    这是本项目高频坑"用 state 推断首次"的**同族**问题：状态（我这里插入了）
    不等于事实（我拿到了那一行）。在持有 advisory lock 的前提下，
    重读拿到的是本次事务内的最终状态 —— 它才是"生效值"。

    ## 为什么冲突要重试而不是静默跳过

    持锁时冲突意味着"另一条路径没持锁就写了"（缺陷）。
    重试一次让它退化为"慢但正确"，避免把一个真缺陷变成静默丢写。
    重试仍失败时让异常冒出去 —— 那是需要被人看见的信号。
    """
    last_conflict: IntegrityError | None = None
    for _attempt in range(MAX_SETTING_ATTEMPTS):
        repo.lock_setting_key(key=key)
        # 拿锁后**重读一次**：另一个事务可能刚插入了副本并提交。
        # 不重读就会拿着"锁外读到的 None"去做无谓的插入。
        existing = repo.current_project_setting(key=key, project_id=project_id)
        if existing is not None:
            return existing

        try:
            with repo.savepoint():
                revision_no = repo.max_revision_no(key=key, project_id=project_id) + 1
                repo.insert_inherited_copy(
                    key=key,
                    project_id=project_id,
                    value=dict(global_row.value),
                    revision_no=revision_no,
                    actor_id=None,
                    reason=None,
                    request_id=None,
                )
                repo.flush()
                break
        except IntegrityError as exc:
            if not _is_setting_revision_conflict(exc):
                # 其它约束（CHECK / FK）是程序缺陷或前置校验漏项，原样抛出 ——
                # 伪装成"并发冲突请重试"会让真 bug 长期潜伏（WP11 同款处置）。
                raise
            last_conflict = exc
    else:
        raise ApiError(
            ErrorCode.INVALID_TRANSITION,
            f"并发写入配置 {key} 冲突：连续 {MAX_SETTING_ATTEMPTS} 次都没能占到修订号。",
            details={"reason": "revision_allocation_exhausted", "key": key},
        ) from last_conflict

    # ← 关键：**重读**，不信任插入时构造的那个对象（见 docstring）。
    materialized = repo.current_project_setting(key=key, project_id=project_id)
    if materialized is None:  # pragma: no cover - 防御性
        raise RuntimeError(
            f"副本物化后仍读不到 {key} 的项目级当前行（project={project_id}）—— "
            "不变量被破坏，检查部分唯一索引是否在位。"
        )
    return materialized


def _is_setting_revision_conflict(exc: IntegrityError) -> bool:
    """`IntegrityError` 是否来自 `revision_no` 唯一性（而不是别的约束）。

    **必须区分**：本项目已有实证（WP11/WP22）—— 把 FK / CHECK 违反
    当成"并发冲突请重试"会让真缺陷以"重试 5 次后仍然失败"的形式表现，
    而错误文案指向并发（误导排障方向）。

    判据是**约束名**（`pg` 报在 `diag.constraint_name` 里）。
    本书的两条部分唯一索引名从 `ddl.partial_unique_index_names()` 取 ——
    与建索引的地方同源，避免这里手写一遍而漂移。
    """
    from rednote.modules.planning.ddl import partial_unique_index_names

    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    constraint = getattr(diag, "constraint_name", None)
    if constraint is None:
        return False
    return constraint in partial_unique_index_names()


@dataclass(frozen=True, slots=True)
class DivergedProject:
    """一个"与全局值不同"的项目（`GET /settings/projects/diverged` 的一行）。"""

    project_id: uuid.UUID
    value: dict[str, Any]
    source: str
    global_value: dict[str, Any] | None
    revision_no: int


def list_diverged_projects(
    repo: PlanningRepository, key: str
) -> list[DivergedProject]:
    """列出所有生效值与全局值**不同**的项目。

    **为什么要这个函数而不是让前端逐个项目调 `get_effective_limits`**：
    那要 50 次请求，而"分叉了没人知道"的杀伤路径正是"没人会去逐个点开
    50 个项目找差异"（§3.6.4 原文）。一次查询给出清单，
    管理者才知道**要去看哪几个**。

    **判定用穷举而不是抽样**：`list_project_settings` 用 `DISTINCT ON`
    取每个项目的当前行 —— 全部项目都在结果里，因此这里的比较覆盖全集。
    J2 判据复用同一条查询做"所有项目都等于全局值"的断言。
    """
    global_row = repo.current_global_setting(key=key)
    global_value = None if global_row is None else global_row.value

    diverged: list[DivergedProject] = []
    for row in repo.list_project_settings(key=key):
        if global_value is None or row.value != global_value:
            diverged.append(
                DivergedProject(
                    project_id=row.project_id,
                    value=dict(row.value),
                    source=row.origin,
                    global_value=global_value,
                    revision_no=row.revision_no,
                )
            )
    return diverged


# ---------------------------------------------------------------------------
# 变更：两条（且只有两条）公开写入口
# ---------------------------------------------------------------------------


def change_global_setting(
    repo: PlanningRepository,
    *,
    key: str,
    value: Mapping[str, Any] | None,
    reason: str,
    actor_id: uuid.UUID | None,
    request_id: str | None = None,
) -> EffectiveSetting:
    """**唯一全局写入口**。同一事务内级联刷新所有 `INHERITED` 副本。

    见 §3.6.2 的伪码。三个关键性质：

    1. **原子性**：① 写全局行、② 刷副本在同一个事务里。
       读已提交隔离级别下不存在"全局新的 + 副本旧的"混合态 ——
       这是 API 双实例下的正确性来源，**不依赖任何进程内缓存**。
    2. **覆盖稳定**：`origin='MANUAL'` 的项目**跳过**（覆盖的意义）。
    3. **代价有界**：行数增长 = 变更次数 × 项目数。

    **"尚无 PROJECT 行"为什么要刷新**（§3.6.3 路径 c）：
    项目可能在本包迁移之后创建、且从未被读取过。不刷的话它第一次
    读到的是**新全局值**（惰性物化），看起来也对 —— 但如果它是在
    "全局值还是旧值"的时候创建的、而我们从没给它副本，
    那么"创建时看到的值"就丢失了。刷新它让"每个项目都有一份
    标注了来源的副本"在任何时序下都成立（这是 J1(i) 的诉求）。
    """
    normalized = validate_setting_value(key, value)
    _require_reason(reason)
    if actor_id is not None and not repo.user_exists(actor_id):
        raise _validation_error(
            "actorId",
            f"操作人 {actor_id} 不存在。",
            reason=REASON_ACTOR_NOT_FOUND,
        )

    # 取锁：本函数是三条写路径之一，必须与另两条（项目覆盖、惰性物化）
    # 用**同一把**键锁串行（见模块 docstring 第三节）。
    repo.lock_setting_key(key=key)

    global_row = _append_setting(
        repo,
        key=key,
        scope_kind=SETTING_SCOPE_GLOBAL,
        project_id=None,
        value=normalized,
        origin=SETTING_ORIGIN_GLOBAL,
        reason=reason,
        actor_id=actor_id,
        request_id=request_id,
    )

    # ---- 步骤 ②：级联刷新所有 INHERITED 项目（含"尚无 PROJECT 行"）----
    #
    # 枚举的是**全部项目**（`list_project_ids()` 不带任何过滤）——
    # 见 `repository.list_project_ids` 的说明：漏掉任何一个都会让 J2 失败。
    #
    # ## ★ 为什么先批量查一次（MAJOR-4 的修复）
    #
    # 初版在循环里逐项目调 `current_project_setting` + `_append_setting`
    # （后者内部再 `max_revision_no`）⇒ **每项目 2 次查询的 N+1**。
    # 实测：1184 行的项目表，一次全局变更 1.09 s，且**持着全局 advisory lock**，
    # 期间同 key 的所有其它写请求（项目覆盖、惰性物化）全被阻塞。
    #
    # `list_project_settings(key)` 返回的正是"每个项目 `revision_no` 最大
    # 的那一行"（`DISTINCT ON (project_id) … ORDER BY project_id,
    # revision_no DESC`），与 `current_project_setting` **语义等价**。
    # 一次查询同时得到"当前行（判 origin）"与"当前修订号（取号）"。
    #
    # 不在本表（尚无 PROJECT 行）的项目不在结果里 ⇒ 取 0 起算。
    existing: dict[uuid.UUID, Any] = {
        row.project_id: row for row in repo.list_project_settings(key=key)
    }

    for project_id in repo.list_project_ids():
        current = existing.get(project_id)
        if current is not None and current.origin == SETTING_ORIGIN_MANUAL:
            # 运营显式覆盖过 ⇒ **跳过**。这正是 J4（覆盖稳定）的定义，
            # 也是它与 J2（同步）互为夹逼的地方：这里若漏判 origin，
            # J4 会失败；若"无差别全刷"，J4 也会失败。
            continue
        _append_setting(
            repo,
            key=key,
            scope_kind=SETTING_SCOPE_PROJECT,
            project_id=project_id,
            value=normalized,
            origin=SETTING_ORIGIN_INHERITED,
            reason=reason,
            actor_id=actor_id,
            request_id=request_id,
            # 预取号：已有行 ⇒ 在其上 +1；无行 ⇒ 从 1 起。
            # 与 `max_revision_no(...) + 1` 口径一致（无行时 max 返回 0）。
            revision_no=(0 if current is None else current.revision_no) + 1,
        )

    repo.flush()
    return EffectiveSetting(
        key=key,
        configured=True,
        value=dict(normalized),
        source=SETTING_ORIGIN_GLOBAL,
        global_value=dict(normalized),
        diverged=False,
        revision_no=global_row.revision_no,
        project_id=None,
    )


def change_project_setting(
    repo: PlanningRepository,
    *,
    key: str,
    project_id: uuid.UUID,
    value: Mapping[str, Any] | None,
    reason: str,
    actor_id: uuid.UUID | None,
    request_id: str | None = None,
) -> EffectiveSetting:
    """**唯一项目级写入口**。写 `origin='MANUAL'`（覆盖）。

    **覆盖是稳定的**：写入之后，该项目的值不再跟随全局变更（J4）。
    这正是"覆盖"与"副本"的唯一区别，也是 `origin` 列存在的全部意义。

    **不需要级联任何东西** —— 覆盖只影响这一个项目，
    这正是它与 `change_global_setting` 的不对称之处（一条要刷 N 个项目，
    一条只写一行）。
    """
    normalized = validate_setting_value(key, value)
    _require_reason(reason)
    if not repo.project_exists(project_id):
        raise _not_found(
            f"项目 {project_id} 不存在。",
            reason=REASON_PROJECT_NOT_FOUND,
            projectId=str(project_id),
        )
    if actor_id is not None and not repo.user_exists(actor_id):
        raise _validation_error(
            "actorId",
            f"操作人 {actor_id} 不存在。",
            reason=REASON_ACTOR_NOT_FOUND,
        )

    repo.lock_setting_key(key=key)

    row = _append_setting(
        repo,
        key=key,
        scope_kind=SETTING_SCOPE_PROJECT,
        project_id=project_id,
        value=normalized,
        origin=SETTING_ORIGIN_MANUAL,
        reason=reason,
        actor_id=actor_id,
        request_id=request_id,
    )
    repo.flush()

    global_row = repo.current_global_setting(key=key)
    global_value = None if global_row is None else dict(global_row.value)
    return EffectiveSetting(
        key=key,
        configured=True,
        value=dict(normalized),
        source=SETTING_ORIGIN_MANUAL,
        global_value=global_value,
        diverged=global_value is not None and normalized != global_value,
        revision_no=row.revision_no,
        project_id=project_id,
    )


def _require_reason(reason: str | None) -> None:
    """变更理由必填（ADR-13 的审计要求）。

    **在服务层校验而不是只靠 DDL 的 `reason_not_blank`**：
    DDL 那条只拦"空串"，而"没传"是 `NULL`（它放行）。
    `NULL` 的理由让审计行读起来是"有人改了但没说为什么"——
    而 ADR-13 要求审计包含"理由"这一项。
    """
    if reason is None or not reason.strip():
        raise _validation_error(
            "reason",
            "变更配置必须给出理由（ADR-13：审计需记录变更理由）。",
            reason=REASON_CHANGE_REASON_REQUIRED,
        )
    if len(reason) > MAX_CHANGE_REASON_CHARS:
        raise _validation_error(
            "reason",
            f"变更理由最多 {MAX_CHANGE_REASON_CHARS} 字，收到 {len(reason)} 字。",
            reason=REASON_CHANGE_REASON_REQUIRED,
        )


def _append_setting(
    repo: PlanningRepository,
    *,
    key: str,
    scope_kind: str,
    project_id: uuid.UUID | None,
    value: dict[str, Any],
    origin: str,
    reason: str | None,
    actor_id: uuid.UUID | None,
    request_id: str | None,
    revision_no: int | None = None,
) -> Any:
    """在一个**已持锁**的事务里追加一行。返回新行。

    **取号 + 插入 + flush 的封装**：三处（全局、级联刷副本、项目覆盖）
    都要做这三件事，且都必须"先取号再插入、在同一 SAVEPOINT 内 flush"
    （让唯一约束冲突在可捕获的位置抛出）。
    抽出来的收益是**这三处的取号口径不可能分叉**。

    **不在这里重试**：重试策略属于调用方（`get_effective_limits` 的物化
    路径有自己的重试）。本函数只做一次尝试，冲突原样冒给调用方。

    ## `revision_no` 预取（MAJOR-4 的修复）

    默认 `None` ⇒ 本函数自己 `max_revision_no` 取号（单行写入的正确做法）。
    但**级联刷新**要为每个项目各插一行，逐个取号就是 N 次查询（实测
    1.09 s / 1184 行，与项目数线性）。级联路径因此先做**一次批量查询**
    （`list_project_settings`，它返回的正是每个项目的最大修订行）
    把号算好传进来。
    """
    with repo.savepoint():
        if revision_no is None:
            revision_no = repo.max_revision_no(key=key, project_id=project_id) + 1
        row = repo.insert_setting(
            key=key,
            scope_kind=scope_kind,
            project_id=project_id,
            value=value,
            revision_no=revision_no,
            origin=origin,
            actor_id=actor_id,
            reason=reason,
            request_id=request_id,
        )
        repo.flush()
        return row


# ---------------------------------------------------------------------------
# 初始化：迁移与测试共用（保证"迁移写的"与"测试断言的"同源）
# ---------------------------------------------------------------------------


def bootstrap_defaults(
    repo: PlanningRepository,
    *,
    reason: str = "初始默认值（ADR-13）",
    actor_id: uuid.UUID | None = None,
    refresh_existing_projects: bool = True,
) -> None:
    """写入 ADR-13 的初始 `GLOBAL` 行，并为存量项目回填副本。

    **为什么它是一个函数而不是迁移里的裸 SQL**：
    迁移写一遍 `{"TRIAL":10,"SCALE":30,"BULK":50}`、测试断言一遍 ——
    两处一旦不一致，测试会**通过**（它断言的是自己写的那份）。
    这里让两处调同一个函数，值来自 `DEFAULT_BATCH_MAX`（单一来源）。

    **幂等**：已存在 `GLOBAL` 行时**不动它**（迁移是可重入的，
    而重复执行不该产生新的修订号 —— 那会让"第 2 版"毫无理由地出现）。
    """
    repo.lock_setting_key(key=SETTING_KEY_BATCH_LIMITS)

    existing = repo.current_global_setting(key=SETTING_KEY_BATCH_LIMITS)
    if existing is None:
        normalized = validate_setting_value(
            SETTING_KEY_BATCH_LIMITS, dict(DEFAULT_BATCH_MAX)
        )
        _append_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            scope_kind=SETTING_SCOPE_GLOBAL,
            project_id=None,
            value=normalized,
            origin=SETTING_ORIGIN_GLOBAL,
            reason=reason,
            actor_id=actor_id,
            request_id=None,
        )
        repo.flush()
    else:
        normalized = existing.value

    if not refresh_existing_projects:
        return

    # 为存量项目回填 INHERITED 副本（§3.6.3 路径 a）。
    #
    # **"存量项目"= 全部项目**：迁移执行时刻已存在的都算，
    # 没有"活跃/归档"的区分 —— 归档项目也可能被重新打开（WP09 的
    # `REOPEN_PROJECT`），那时它的配置不该是空的。
    for project_id in repo.list_project_ids():
        current = repo.current_project_setting(
            key=SETTING_KEY_BATCH_LIMITS, project_id=project_id
        )
        if current is not None:
            # 已有行（可能是运维手写的 MANUAL）⇒ 不动它。
            # 迁移不该覆盖任何已存在的决策。
            continue
        _append_setting(
            repo,
            key=SETTING_KEY_BATCH_LIMITS,
            scope_kind=SETTING_SCOPE_PROJECT,
            project_id=project_id,
            value=dict(normalized),
            origin=SETTING_ORIGIN_INHERITED,
            reason=reason,
            actor_id=actor_id,
            request_id=None,
        )
    repo.flush()


def require_configured(setting: EffectiveSetting) -> EffectiveSetting:
    """未配置时抛 422（供"必须已有值"的消费方使用）。

    **为什么不直接在 `get_effective_limits` 里抛**：占位键
    （`PLACEHOLDER_SETTING_KEYS`）的合法状态就是"没有值"，
    读取它的一方（前端展示"尚未配置"）需要拿到 `configured=False` 这个事实，
    而不是一个异常。只有**真正需要值**的消费方（WP25 算预算）才要求它必须存在。
    """
    if not setting.configured:
        raise _validation_error(
            "key",
            f"配置项 {setting.key} 尚未配置任何值（OP-64 等待实测）。",
            reason=REASON_SETTING_NOT_CONFIGURED,
            key=setting.key,
        )
    return setting


__all__ = [
    "MAX_SETTING_ATTEMPTS",
    "DivergedProject",
    "bootstrap_defaults",
    "change_global_setting",
    "change_project_setting",
    "get_effective_limits",
    "list_diverged_projects",
    "require_configured",
    "validate_setting_value",
]
