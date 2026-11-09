"""KnowledgeSchema —— 六类知识的结构化字段规格（WP20）。

规格源 02 §8 的 `knowledge_versions` 备注原文：

> **类别对应 Schema，禁止无结构散文替代事实字段**

规格源**只给了这一句判据，没有给出六类各自的 Schema**。因此这一份规格是
本包**主动补全**的（同 WP18 的 `BriefSchema`，见说明文档 §1.4）。

---

## 为什么字段路径是"点分字符串"而不是嵌套 JSON 的键

与 WP18 完全同构的理由：`structured_payload` 是 jsonb，但下游要按字段定位 ——
WP21 的检索要能说"命中的是**适用范围**那一栏"，WP23 的 Prompt 组装要能
按角色取用（`context_items.role FACT/RULE/STYLE/REFERENCE` 的粒度是**字段**
而不是整段 payload）。

若字段身份不固定，检索只能整段返回，Prompt 只能整段塞 —— 那就退回到
"无结构散文"，正是这条备注要禁止的。

因此：
  · 路径一经发布**只增不改**（改名 = 新增路径 + 保留旧路径别名）；
  · `field_spec()` 对未知路径**显式报错**，不静默接受。

## 值与其状态必须在一起

`structured_payload` 的形状是：

    {"<path>": {"status": "FILLED", "value": ...}}

**不是**"值在一个键、状态在另一个键"（WP18 §39~51 的完整论证）。
本包沿用同一形状，理由是**下游要能区分"填了空"与"没这一项"**：
`FILLED` + `value=[]` 是"这个问题我们查过，答案是空"；
路径整条缺席是"我们没查过"。两者对 WP21 的召回质量判断完全不同。

## 六类为什么各要有"至少一个必填字段"

**判据是"缺了它，这条知识就不能被下游正确使用"**（与 WP18 的 required
判据同一条）。

| 类别 | 必填 | 缺了会怎样 |
|---|---|---|
| `FACT` | `fact.statement` | 一条没有陈述的事实 = 空记录 |
| `RESEARCH` | `research.conclusion` | 没有结论的研究无法支撑任何决策 |
| `SCENE` | `scene.description` | 没有描述的场景无法被匹配（WP21 的召回全靠它） |
| `STYLE` | `style.guideline` | 没有规范条文的风格无法被遵循 |
| `RULE` | `rule.directive` | 没有指令的规则无法被校验 |
| `FEEDBACK_EXPERIMENT` | `experiment.observation` | 只说"做了个实验"而不说看到什么，等于没做 |

**其余字段一律非必填**。把全部字段设为必填是最容易写的做法，
但它会逼运营为了存一条"产品续航 18 小时（常温、50% 亮度下）"而去
填满 `fact.measured_by` / `fact.effective_conditions` —— 而真实的
知识录入里绝大多数条目本来就只有一个陈述句。**必填项多一条，
真实数据里的假值就多一批**（同 WP18 §53~64 的判断）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from rednote.modules.knowledge.types import (
    CATEGORY_FACT,
    CATEGORY_FEEDBACK_EXPERIMENT,
    CATEGORY_RESEARCH,
    CATEGORY_RULE,
    CATEGORY_SCENE,
    CATEGORY_STYLE,
    CATEGORIES,
    MAX_FIELD_TEXT_CHARS,
    SCOPE_REQUIRED_COLUMN,
)

# ---------------------------------------------------------------------------
# 字段状态（与 WP18 的 FIELD_* 同形）
# ---------------------------------------------------------------------------
# **刻意复用 WP18 的三个取值字面量而不是新造一套**：两个模块的
# `structured_*` 字段在协议里是同一类东西（结构化字段容器），
# 前端渲染同一套表单组件。取不同字面量只会让前端写两个分支。

FIELD_FILLED: Final[str] = "FILLED"
FIELD_MISSING: Final[str] = "MISSING"
FIELD_NOT_APPLICABLE: Final[str] = "NOT_APPLICABLE"

FIELD_STATUSES: Final[tuple[str, ...]] = (
    FIELD_FILLED,
    FIELD_MISSING,
    FIELD_NOT_APPLICABLE,
)


# ---------------------------------------------------------------------------
# 字段规格
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """一个结构化字段的规格。"""

    path: str
    #: 中文标签（前端展示与错误提示用）。**只有一处** —— 各端点各写一份
    #: 会让同一个字段在不同页面叫不同名字。
    label: str
    #: 是否必填（见模块 docstring 的判据）。
    required: bool = False
    #: 是否多值（列表）。
    multi: bool = False


#: 六类各自的字段规格。**键是 category，值是字段元组。**
#:
#: 每类的字段分两组：
#:   · 该类特有的实质字段（必填的那个一定在这里）；
#:   · 通用溯源字段（`source.*`）—— 六类共用，在下方 `COMMON_FIELDS`。
#:
#: **为什么通用字段不在每类里各写一遍**：写六遍就会有六处漂移，
#: 而漂移的方向恰好是"某一类漏了来源字段"，于是那一类的知识
#: 无法被追溯，且没有任何报错。
CATEGORY_FIELDS: Final[dict[str, tuple[FieldSpec, ...]]] = {
    CATEGORY_FACT: (
        FieldSpec("fact.statement", "事实陈述", required=True),
        FieldSpec("fact.measured_by", "测量 / 认定方式"),
        FieldSpec("fact.effective_conditions", "生效条件", multi=True),
        FieldSpec("fact.applicable_models", "适用型号", multi=True),
        FieldSpec("fact.exceptions", "例外情况", multi=True),
    ),
    CATEGORY_RESEARCH: (
        FieldSpec("research.conclusion", "研究结论", required=True),
        FieldSpec("research.method", "研究方法"),
        FieldSpec("research.sample", "样本 / 数据范围"),
        FieldSpec("research.caveats", "局限与注意事项", multi=True),
    ),
    CATEGORY_SCENE: (
        FieldSpec("scene.description", "场景描述", required=True),
        FieldSpec("scene.audience", "适用人群", multi=True),
        FieldSpec("scene.trigger", "触发时机"),
        FieldSpec("scene.pain_points", "该场景下的痛点", multi=True),
    ),
    CATEGORY_STYLE: (
        FieldSpec("style.guideline", "风格规范", required=True),
        FieldSpec("style.dos", "应当", multi=True),
        FieldSpec("style.donts", "避免", multi=True),
        FieldSpec("style.examples", "正例", multi=True),
        FieldSpec("style.counter_examples", "反例", multi=True),
    ),
    CATEGORY_RULE: (
        FieldSpec("rule.directive", "规则条文", required=True),
        FieldSpec("rule.rationale", "规则依据"),
        FieldSpec("rule.enforcement", "检查方式"),
        FieldSpec("rule.exceptions", "例外情况", multi=True),
    ),
    CATEGORY_FEEDBACK_EXPERIMENT: (
        FieldSpec("experiment.observation", "实验观察", required=True),
        FieldSpec("experiment.hypothesis", "假设"),
        FieldSpec("experiment.variant", "变量 / 分组"),
        FieldSpec("experiment.metric", "度量口径"),
        FieldSpec("experiment.outcome", "结果判定"),
    ),
}

#: 六类共用的溯源字段。**加在这一个元组里即对六类生效** ——
#: 这是"六类都必须能追溯"这条要求的结构性落点。
COMMON_FIELDS: Final[tuple[FieldSpec, ...]] = (
    FieldSpec("source.note", "来源说明"),
)


def fields_for(category: str) -> tuple[FieldSpec, ...]:
    """某一类的全部字段（特有字段 + 通用字段）。

    未知类别**显式报错**：静默返回空元组会让"这类没有必填字段"
    看起来成立，于是任何 payload 都能通过校验。
    """
    specific = CATEGORY_FIELDS.get(category)
    if specific is None:
        raise ValueError(
            f"未知的知识类别：{category!r}。"
            f"允许值：{list(CATEGORIES)}（规格源 02 §8）。"
        )
    return specific + COMMON_FIELDS


#: 全部字段（跨六类）。路径**必须唯一** —— 否则 `field_spec(path)` 无法
#: 确定"这个路径属于哪一类"，而校验时按路径查到的规格可能来自另一类。
ALL_FIELDS: Final[tuple[FieldSpec, ...]] = tuple(
    spec for category in CATEGORIES for spec in fields_for(category)
)

FIELD_BY_PATH: Final[dict[str, FieldSpec]] = {spec.path: spec for spec in ALL_FIELDS}

#: 每一类的必填字段路径。**由列表推导而不是手抄** —— 手抄一份就会与
#: `required` 标记漂移，而漂移的方向恰好是"漏掉一个必填字段"（校验会放行）。
REQUIRED_FIELDS_BY_CATEGORY: Final[dict[str, tuple[str, ...]]] = {
    category: tuple(spec.path for spec in fields_for(category) if spec.required)
    for category in CATEGORIES
}

#: 路径 → 它所属的类别集合。一个路径**可以**被多类共用（`source.note`
#: 就是六类共用的），因此值是集合而不是单值。
#:
#: **为什么需要它**：校验时必须判"这个 payload 里出现的路径放错了类别没有"。
#: 例如把 `fact.statement` 写进一条 `STYLE` 知识 —— 若只按
#: `FIELD_BY_PATH` 查存在性，它是存在的，于是静默通过；
#: 而下游按 STYLE 取字段时永远取不到 `style.guideline`。
CATEGORIES_BY_PATH: Final[dict[str, frozenset[str]]] = {
    path: frozenset(
        category
        for category in CATEGORIES
        if path in {spec.path for spec in fields_for(category)}
    )
    for path in FIELD_BY_PATH
}


def field_spec(path: str) -> FieldSpec:
    """取字段规格；**未知路径显式报错**（同 WP18 的 `field_spec`）。"""
    spec = FIELD_BY_PATH.get(path)
    if spec is None:
        raise ValueError(
            f"未知的知识字段路径：{path!r}。"
            "KnowledgeSchema 见 modules/knowledge/schema.py（规格源 02 §8）。"
        )
    return spec


def empty_field_values(category: str) -> dict[str, Any]:
    """某类新版本的初始 `structured_payload`：**每个字段都在，状态是 MISSING**。

    与 WP18 的 `empty_field_values()` 同一条理由：预置全部字段让前端
    拿到就能渲染完整表单；`MISSING` 是一个**显式事实**（"这个字段我们知道
    该有，但还没填"），与"这个字段不在 Schema 里"是两件事。
    """
    return {
        spec.path: {"status": FIELD_MISSING, "value": None}
        for spec in fields_for(category)
    }


# ---------------------------------------------------------------------------
# 归一化与校验
# ---------------------------------------------------------------------------


def normalize_payload(category: str, raw: Any) -> dict[str, Any]:
    """把外部传入的 `structured_payload` 归一化成受控形状。

    三条规则（与 WP18 的 `normalize_field_values` 同构）：

      1. **未知路径 → 报错**（不静默丢弃）；
      2. 路径**放错类别 → 报错**（见 `CATEGORIES_BY_PATH` 的说明）；
      3. 缺 `status` → 按 `value` 推断；`status` 与 `value` 不一致 → 报错
         （**不"以某一方为准"**：那是替调用方猜意图，而猜测的方向
         决定了校验会不会放行）。

    `raw is None` 返回全 MISSING 的完整形状（而不是空对象）——
    空对象会被 `models.py` 的非空 CHECK 拦下，那是"没给 payload"
    这个**程序错误**的合理归宿；但走服务层的调用方不该撞到它，
    因此这里给一个合法的起点。
    """
    if raw is None:
        return empty_field_values(category)
    if not isinstance(raw, dict):
        raise ValueError("structuredPayload 必须是对象")

    allowed = {spec.path for spec in fields_for(category)}
    normalized: dict[str, Any] = {}
    for path, entry in raw.items():
        spec = _require_path_for_category(path=path, category=category, allowed=allowed)
        status, value = _read_entry(path=path, entry=entry, spec=spec)
        normalized[path] = {"status": status, "value": value}

    # 未出现的字段补成 MISSING（而不是让它缺席）。
    for spec in fields_for(category):
        normalized.setdefault(spec.path, {"status": FIELD_MISSING, "value": None})
    return normalized


def _require_path_for_category(
    *, path: str, category: str, allowed: set[str]
) -> FieldSpec:
    """校验路径存在**且属于本类别**，返回它的规格。"""
    spec = FIELD_BY_PATH.get(path)
    if spec is None:
        raise ValueError(
            f"未知的知识字段路径：{path!r}。"
            f"类别 {category} 允许的路径见 modules/knowledge/schema.py。"
        )
    if path not in allowed:
        owners = sorted(CATEGORIES_BY_PATH.get(path, frozenset()))
        raise ValueError(
            f"字段 {path!r} 不属于类别 {category}（它属于 {owners}）。"
            "把别的类别的字段写进来会让下游按本类别取字段时永远取不到 —— "
            "而校验若只查存在性，这个是静默通过的。"
        )
    return spec


def _read_entry(*, path: str, entry: Any, spec: FieldSpec) -> tuple[str, Any]:
    """读一个字段条目，返回 `(status, value)`。"""
    if isinstance(entry, dict) and "status" in entry:
        status = entry.get("status")
        value = entry.get("value")
    else:
        # 裸值（前端可能直接传 `{"fact.statement": "续航 18 小时"}`）。
        value = entry
        status = FIELD_FILLED if has_value(value) else FIELD_MISSING

    if status not in FIELD_STATUSES:
        raise ValueError(
            f"字段 {path} 的状态 {status!r} 非法；允许值：{list(FIELD_STATUSES)}"
        )

    if status == FIELD_FILLED and not has_value(value):
        raise ValueError(
            f"字段 {path} 标为 FILLED 但值为空 —— "
            "状态与值必须一致（否则校验会基于错误的完整度判断放行）。"
        )
    if status == FIELD_MISSING and has_value(value):
        raise ValueError(
            f"字段 {path} 标为 MISSING 但值非空 —— "
            "状态与值必须一致（只清状态不清值会让下游以为这个字段可以用了）。"
        )
    if status == FIELD_NOT_APPLICABLE and not has_value(value):
        # `value` 位置承载**原因**，且必须非空 ——
        # 一个没有理由的"不适用"无法复核（同 WP18）。
        raise ValueError(f"字段 {path} 标为 NOT_APPLICABLE 必须填写原因")
    if spec.multi and status == FIELD_FILLED and not isinstance(value, list):
        # 多值字段必须是列表。允许标量会让下游为两种形状写分支。
        raise ValueError(f"字段 {path} 是多值字段，值必须是数组")
    if status == FIELD_FILLED:
        _require_not_overlong(path=path, value=value)
    return status, value


def _require_not_overlong(*, path: str, value: Any) -> None:
    """拒绝超长的单字段值。

    上限的作用不是"够不够用"的判断，而是**防止某次导入把整篇正文
    塞进一个字段** —— 那会让 jsonb 行膨胀、让列表接口的响应体失控。
    """
    texts: list[str] = []
    if isinstance(value, str):
        texts.append(value)
    elif isinstance(value, list):
        texts.extend(item for item in value if isinstance(item, str))
    for text in texts:
        if len(text) > MAX_FIELD_TEXT_CHARS:
            raise ValueError(
                f"字段 {path} 的值超过 {MAX_FIELD_TEXT_CHARS} 字符。"
            )


def has_value(value: Any) -> bool:
    """值是否"有内容"。

    `False` / `0` 算有内容（它们是有意义的取值，不是"没填"）；
    `""` / `[]` / `{}` / `None` 算没填 —— 空字符串与空数组在 jsonb 里
    与 `null` 表达的是同一件事（"没填"），分成两种状态没有信息量。
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def missing_required_fields(category: str, payload: dict[str, Any]) -> tuple[str, ...]:
    """未处置的必填字段（既没填、也没标"不适用"）。

    `NOT_APPLICABLE` 也算已处置 —— 与 WP18 的
    `missing_required_fields` 同一口径：一条事实的"生效条件"确实可能
    "无（无条件适用）"，运营应能显式地说这句话，而不是被迫留空。
    """
    missing: list[str] = []
    for path in REQUIRED_FIELDS_BY_CATEGORY.get(category, ()):
        entry = payload.get(path) or {}
        status = entry.get("status")
        if status not in (FIELD_FILLED, FIELD_NOT_APPLICABLE):
            missing.append(path)
    return tuple(missing)


# ---------------------------------------------------------------------------
# 范围校验（"事实范围必须明确"）
# ---------------------------------------------------------------------------


def scope_violation(
    *,
    category: str,
    scope: str,
    brand_id: Any,
    product_id: Any,
    project_id: Any,
) -> str | None:
    """校验"范围声明"与"范围限定列"是否自洽。返回违规原因，`None` = 通过。

    **两条规则**（`models.py` 的 CHECK 把它们也写了一份，两层各管一件事）：

      1. **范围必须有对应的限定列**：`scope='BRAND'` 却没有 `brand_id`
         是一句无法执行的声明 —— 它会在检索时被当成"适用于所有品牌"
         （因为过滤条件 `brand_id = ?` 匹配不上任何值，而不过滤又不报错）。
      2. **反向也必须成立**：给了 `brand_id` 却声明 `scope='GLOBAL'`，
         意思是"这是全组织知识（但只属于这个品牌）"—— 自相矛盾，
         且两种解读会导致不同的检索结果。

    第 1 条里的**"事实范围必须明确"**是本包的核心验收落点：
    `CATEGORY_FACT` 不允许 `scope='GLOBAL'`（见
    `CATEGORIES_REQUIRING_CONCRETE_SCOPE` 的说明）。
    """
    from rednote.modules.knowledge.types import (
        CATEGORIES_REQUIRING_CONCRETE_SCOPE,
        SCOPE_GLOBAL,
        SCOPES,
    )

    if scope not in SCOPES:
        return f"scope {scope!r} 非法；允许值：{list(SCOPES)}"

    if (
        category in CATEGORIES_REQUIRING_CONCRETE_SCOPE
        and scope == SCOPE_GLOBAL
    ):
        return (
            f"类别 {category} 的事实范围必须明确：不允许 scope='{SCOPE_GLOBAL}'。"
            "一条适用于所有品牌的事实断言会横穿全部项目"
            "（规格源 02 §8：「事实范围必须明确」）。"
        )

    columns = {
        "brand_id": brand_id,
        "product_id": product_id,
        "project_id": project_id,
    }
    required = SCOPE_REQUIRED_COLUMN.get(scope, "")
    if required and not columns[required]:
        return f"scope='{scope}' 必须提供 {required}"
    if not required:
        # GLOBAL：任何限定列都不该出现（见规则 2）。
        for name, value in columns.items():
            if value:
                return (
                    f"scope='{SCOPE_GLOBAL}' 与 {name} 同时出现 —— "
                    "声明的范围与限定的范围矛盾，两种解读会给出不同的检索结果。"
                )
    return None


__all__ = [
    "ALL_FIELDS",
    "CATEGORIES_BY_PATH",
    "CATEGORY_FIELDS",
    "COMMON_FIELDS",
    "FIELD_BY_PATH",
    "FIELD_FILLED",
    "FIELD_MISSING",
    "FIELD_NOT_APPLICABLE",
    "FIELD_STATUSES",
    "FieldSpec",
    "REQUIRED_FIELDS_BY_CATEGORY",
    "empty_field_values",
    "field_spec",
    "fields_for",
    "has_value",
    "missing_required_fields",
    "normalize_payload",
    "scope_violation",
]