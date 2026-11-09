"""BriefSchema —— Brief 结构化字段的**唯一权威规格**（WP18）。

需求说明 §5.1 原文（「必须覆盖」共 10 组）：

> 1. 产品图片及使用权限说明。
> 2. 产品名称、品牌、型号。
> 3. 选购话术及适用购买场景。
> 4. 产品解决的痛点。
> 5. 核心卖点及优先级。
> 6. 原理与功效及支持材料。
> 7. 话题标签示例。
> 8. 产品参数、单位、适用型号。
> 9. 目标人群、使用场景和限制条件。
> 10. 必须提及与禁止提及内容。

数据结构 02 §6 只说 `brief_versions.structured_fields jsonb`「JSON 使用
BriefSchema」，**没有给出 BriefSchema 本身**。协议 03 §5 也只给了
`BriefClaim` 的形状。因此这一份规格是本包**主动补全**的（见说明文档 §1.4）。

---

## 为什么字段路径是"点分字符串"而不是嵌套 JSON 的键

`brief_claims.field_path` 与 `brief_issues.field_path` 都要指向"这条结论/疑问
是关于哪个字段的"，而它们的类型是 `text`（数据结构 02 §6）。
因此字段的身份必须是一个**扁平的、稳定的字符串**，不能是 JSON 路径表达式
（那会让 `WHERE field_path = ?` 退化成模糊匹配）。

同时它必须**稳定**：一旦写入 `brief_claims`，它就随版本固化不可改。
若将来把 `product.name` 改名成 `product.title`，历史 claim 的
`field_path` 就全部指向一个不存在的字段 —— 而**没有任何报错**，
前端只是点不中、跳不过去。

因此：
  · 路径一经发布**只增不改**（改名 = 新增路径 + 保留旧路径的别名映射）；
  · 本模块提供 `field_spec(path)`，未知路径**显式报错**而不是静默接受
    （静默接受会让 `field_path` 变成自由文本，前端无法据此定位）。

## 值与其状态必须在一起

`structured_fields` 的每个值是 `{"status": "...", "value": ...}`，
**不是**"值在 A 键、状态在 B 键"：

    # 对
    {"product.name": {"status": "FILLED", "value": "某产品"}}
    # 错
    {"values": {"product.name": "某产品"}, "statuses": {"product.name": "FILLED"}}

第二种形状会出现"改了值忘了改状态"的对不齐，而这类错误不会报错，
只会让固化校验基于错误的完整度判断放行 —— 即"必填字段已填写"这句话
变成一句无法验证的声明。

## `required` 的判据（**规格缺口，本包自行定义并登记**）

需求 §5.3 的完成条件第一句是"必填字段已填写或明确不适用"，
但**需求与数据结构都没有说哪些字段是必填**。这是一个真实的规格缺口。

本包的判据是：**"缺了它，下一步产出必然是编造或违规"**。
按这条判据只有 5 个字段必填（见 `REQUIRED_FIELD_PATHS`），
其余 13 个缺失时是"待补充"（需求 §5.1 原文：缺失字段必须显示"待补充"，
不得由 AI 填成确定事实）—— **不阻断**，只是如实展示。

把 10 组全设为必填是最容易写的做法，但那会让绝大多数真实 Brief
卡在步骤 1 无法推进，而需求明确允许"原材料不足时如实记录"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from rednote.modules.brief.types import (
    FIELD_FILLED,
    FIELD_MISSING,
    FIELD_NOT_APPLICABLE,
    FIELD_STATUSES,
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
    #: 需求 §5.1 的哪一组（1~10）。用于前端分组渲染。
    group: int
    #: 是否必填（见模块 docstring 的判据）。
    required: bool = False
    #: 是否多值（列表）。
    multi: bool = False


#: **BriefSchema 的唯一权威列表**（需求 §5.1 的 10 组，共 18 个字段）。
BRIEF_FIELDS: Final[tuple[FieldSpec, ...]] = (
    # 1. 产品图片及使用权限说明
    FieldSpec("product.images", "产品图片", group=1, multi=True),
    FieldSpec("product.image_usage_rights", "图片使用权限说明", group=1),
    # 2. 产品名称、品牌、型号
    FieldSpec("product.name", "产品名称", group=2, required=True),
    FieldSpec("product.brand", "品牌", group=2, required=True),
    FieldSpec("product.model_code", "型号", group=2),
    # 3. 选购话术及适用购买场景
    FieldSpec("purchase.script", "选购话术", group=3),
    FieldSpec("purchase.scenarios", "适用购买场景", group=3, multi=True),
    # 4. 产品解决的痛点
    FieldSpec("product.pain_points", "产品解决的痛点", group=4, multi=True),
    # 5. 核心卖点及优先级
    FieldSpec(
        "product.selling_points",
        "核心卖点及优先级",
        group=5,
        required=True,
        multi=True,
    ),
    # 6. 原理与功效及支持材料
    FieldSpec("product.mechanism", "原理", group=6),
    FieldSpec("product.efficacy", "功效", group=6),
    FieldSpec("product.supporting_materials", "支持材料", group=6, multi=True),
    # 7. 话题标签示例
    FieldSpec("content.hashtag_examples", "话题标签示例", group=7, multi=True),
    # 8. 产品参数、单位、适用型号
    FieldSpec("product.parameters", "产品参数", group=8, multi=True),
    FieldSpec("product.applicable_models", "适用型号", group=8, multi=True),
    # 9. 目标人群、使用场景和限制条件
    FieldSpec("audience.target", "目标人群", group=9, required=True),
    FieldSpec("audience.usage_scenarios", "使用场景", group=9, multi=True),
    FieldSpec("audience.constraints", "限制条件", group=9, multi=True),
    # 10. 必须提及与禁止提及内容
    FieldSpec("content.must_mention", "必须提及内容", group=10, multi=True),
    FieldSpec(
        "content.must_not_mention",
        "禁止提及内容",
        group=10,
        required=True,
        multi=True,
    ),
)

FIELD_BY_PATH: Final[dict[str, FieldSpec]] = {spec.path: spec for spec in BRIEF_FIELDS}

#: 必填字段路径（`BRIEF_FIELDS` 里 `required=True` 的那些）。
#: **由列表推导而不是手抄一份** —— 手抄一份就会与 `required` 标记漂移，
#: 而漂移的方向恰好是"漏掉一个必填字段"（固化校验会放行）。
REQUIRED_FIELD_PATHS: Final[tuple[str, ...]] = tuple(
    spec.path for spec in BRIEF_FIELDS if spec.required
)

#: 需要覆盖的字段组（需求 §5.1 的 1~10）。测试据此断言"一组都没漏"。
REQUIRED_GROUPS: Final[frozenset[int]] = frozenset(spec.group for spec in BRIEF_FIELDS)

# 产品名与型号在 `brief_versions` 上有**投影列**（数据结构 02 §6 要求
# `product_name` / `model_code` 两列）。路径常量放在这里，
# 让"投影列取自哪个字段"只有一处定义。
FIELD_PATH_PRODUCT_NAME: Final[str] = "product.name"
FIELD_PATH_MODEL_CODE: Final[str] = "product.model_code"

#: 卖点字段路径（固化时据此生成三项摘要）。
FIELD_PATH_SELLING_POINTS: Final[str] = "product.selling_points"

#: 禁提项路径（固化时据此生成"必须提及与禁止提及"摘要）。
FIELD_PATH_MUST_MENTION: Final[str] = "content.must_mention"
FIELD_PATH_MUST_NOT_MENTION: Final[str] = "content.must_not_mention"

# 结构化字段值的 schema 版本。写入方带上它，消费方据此选择解析方式
# （与 WP11 的 `payload_schema_version`、WP17 的 `position_schema_version` 同一手法）。
BRIEF_SCHEMA_VERSION: Final[int] = 1


def field_spec(path: str) -> FieldSpec:
    """取字段规格；**未知路径显式报错**。

    **为什么不静默接受未知路径**：`field_path` 是前端"点字段跳原文"的锚点。
    允许自由文本之后，一个拼错的路径会让那条 claim 永远定位不到字段，
    而系统不会报任何错 —— 前端只是"点它没反应"。
    在写入点报错，把这类问题挡在数据产生之前。
    """
    spec = FIELD_BY_PATH.get(path)
    if spec is None:
        raise ValueError(
            f"未知的 Brief 字段路径：{path!r}。"
            "BriefSchema 见 modules/brief/schema.py（需求 §5.1 的 10 组）。"
        )
    return spec


def empty_field_values() -> dict[str, Any]:
    """全新草稿的 `structured_fields`：**每个字段都在，状态是 MISSING**。

    **为什么预置全部字段而不是空对象**：
      · 前端拿到就能渲染完整表单（不必自己抄一份字段清单）；
      · `MISSING` 是一个**显式事实**（"这个字段我们知道要问，但原文里没有"），
        与"这个字段不在 Schema 里"是两件事 —— 空对象会让两者混为一谈。
    """
    return {
        spec.path: {"status": FIELD_MISSING, "value": None} for spec in BRIEF_FIELDS
    }


def normalize_field_values(raw: Any) -> dict[str, Any]:
    """把外部传入的 `structured_fields` 归一化成受控形状。

    **为什么要归一化而不是原样存**：`structured_fields` 是 jsonb，
    存什么就长什么样。若不归一化，就会出现三种形状并存
    （`{"status": "FILLED", "value": x}` / 裸值 / 缺 status），
    而固化校验与前端渲染都得为每种形状写一个分支。

    三条规则：
      1. 未知字段路径 → 报错（见 `field_spec`）；
      2. 缺 `status` → 按 `value` 推断（有非空值 = FILLED，否则 MISSING）；
      3. `status` 与 `value` 不一致 → 报错（**不"以某一方为准"**：
         那是替调用方猜意图，而猜测的方向决定了固化校验会不会放行）。
    """
    if raw is None:
        return empty_field_values()
    if not isinstance(raw, dict):
        raise ValueError("structuredFields 必须是对象")

    normalized: dict[str, Any] = {}
    for path, entry in raw.items():
        spec = field_spec(path)
        status, value = _read_entry(path=path, entry=entry, spec=spec)
        normalized[path] = {"status": status, "value": value}

    # 未出现的字段补成 MISSING（而不是让它缺席）—— 见 `empty_field_values`。
    for spec in BRIEF_FIELDS:
        normalized.setdefault(spec.path, {"status": FIELD_MISSING, "value": None})
    return normalized


def _read_entry(*, path: str, entry: Any, spec: FieldSpec) -> tuple[str, Any]:
    """读一个字段条目，返回 `(status, value)`。"""
    if isinstance(entry, dict) and "status" in entry:
        status = entry.get("status")
        value = entry.get("value")
    else:
        # 裸值（前端可能直接传 `{"product.name": "某产品"}`）。
        value = entry
        status = FIELD_FILLED if _has_value(value) else FIELD_MISSING

    if status not in FIELD_STATUSES:
        raise ValueError(
            f"字段 {path} 的状态 {status!r} 非法；允许值：{list(FIELD_STATUSES)}"
        )

    if status == FIELD_FILLED and not _has_value(value):
        raise ValueError(
            f"字段 {path} 标为 FILLED 但值为空 —— "
            "状态与值必须一致（否则固化校验会基于错误的完整度判断放行）。"
        )
    if status == FIELD_MISSING and _has_value(value):
        raise ValueError(
            f"字段 {path} 标为 MISSING 但值非空 —— "
            "状态与值必须一致（只清状态不清值会让下游以为这个字段可以用了）。"
        )
    if status == FIELD_NOT_APPLICABLE:
        # 需求 §5.1："确实不适用的字段可由运营明确标记'不适用'并填写原因"。
        # 因此 value 位置承载**原因**，且必须非空 ——
        # 一个没有理由的"不适用"无法复核。
        if not _has_value(value):
            raise ValueError(
                f"字段 {path} 标为 NOT_APPLICABLE 必须填写原因"
                "（需求 §5.1：明确标记'不适用'**并填写原因**）。"
            )
    if spec.multi and status == FIELD_FILLED and not isinstance(value, list):
        # 多值字段必须是列表。允许标量会让下游（WP26 文案）为两种形状写分支。
        raise ValueError(f"字段 {path} 是多值字段，值必须是数组")
    return status, value


def _has_value(value: Any) -> bool:
    """值是否"有内容"。

    `False` / `0` 算有内容（它们是有意义的取值，不是"没填"）；
    `""` / `[]` / `{}` / `None` 算没填 —— 空字符串与空数组在 jsonb 里
    与 `null` 表达的是同一件事（"运营没填"），分成两种状态没有信息量。
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def missing_required_fields(fields: dict[str, Any]) -> tuple[str, ...]:
    """未处置的必填字段（既没填、也没标"不适用"）。

    需求 §5.3 完成条件第 1 条：**必填字段已填写或明确不适用** ——
    所以 `NOT_APPLICABLE` 也算已处置。
    """
    missing: list[str] = []
    for path in REQUIRED_FIELD_PATHS:
        entry = fields.get(path) or {}
        status = entry.get("status")
        if status not in (FIELD_FILLED, FIELD_NOT_APPLICABLE):
            missing.append(path)
    return tuple(missing)


def selling_point_summary(fields: dict[str, Any]) -> tuple[str, ...]:
    """核心卖点的三项摘要（需求 §5.3 完成条件第 2 条）。

    **不足三项时如实返回不足的条数，不补空串、不凑数** ——
    需求原文："原材料不足三项时如实记录，不虚构卖点凑数"。
    返回元组的长度本身就是那个"如实记录"。
    """
    from rednote.modules.brief.types import SELLING_POINT_SUMMARY_COUNT

    entry = fields.get(FIELD_PATH_SELLING_POINTS) or {}
    value = entry.get("value")
    if not isinstance(value, list):
        return ()

    summary: list[str] = []
    for item in value:
        text = _selling_point_text(item)
        if text:
            summary.append(text)
        if len(summary) >= SELLING_POINT_SUMMARY_COUNT:
            break
    return tuple(summary)


def _selling_point_text(item: Any) -> str:
    """从一条卖点里取文本。

    卖点可以是 `"续航长"`（裸字符串）或
    `{"text": "续航长", "priority": 1}`（需求 §5.1 第 5 组要求"及优先级"）。
    两种都接受，但**只取文本**：优先级不参与摘要（它是排序依据，
    不是摘要内容）。
    """
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("text") or "").strip()
    return ""


__all__ = [
    "BRIEF_FIELDS",
    "BRIEF_SCHEMA_VERSION",
    "FIELD_BY_PATH",
    "FIELD_PATH_MODEL_CODE",
    "FIELD_PATH_MUST_MENTION",
    "FIELD_PATH_MUST_NOT_MENTION",
    "FIELD_PATH_PRODUCT_NAME",
    "FIELD_PATH_SELLING_POINTS",
    "FieldSpec",
    "REQUIRED_FIELD_PATHS",
    "REQUIRED_GROUPS",
    "empty_field_values",
    "field_spec",
    "missing_required_fields",
    "normalize_field_values",
    "selling_point_summary",
]
