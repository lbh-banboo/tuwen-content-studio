"""出网策略判定（WP16）—— 验收第一半的落点。

**本模块只有一个公开函数，且它是纯的**（不碰库、不碰网络、无副作用）。
这是刻意的：策略判定必须能被**毫秒级单测**穷举所有分支，
否则"LOCAL_ONLY 没有回退"就只能靠集成测试间接证明 —— 而集成测试
永远覆盖不了"某个分支忘了写"这种缺陷。

---

## 判定的七个步骤，以及**顺序为什么不能换**

| # | 判据 | 否决原因 | 为什么排在这里 |
|---|---|---|---|
| 1 | 供应商是否启用 | `provider_disabled` | 停用的供应商不该参与任何判定（先短路，避免后面拿它的能力下结论） |
| 2 | 能力是否被该供应商支持 | `capability_unsupported` | **与网络模式无关** —— 本地供应商也可能不支持某能力；放前面才能在"本地"分支前拦住 |
| 3 | 是否本地供应商 | （**允许** `local_provider_no_egress`） | 本地调用**不出网**，因此策略与它无关；必须在策略判定**之前**返回，否则"缺省拒绝"会把本地调用也拦掉 |
| 4 | 有没有策略 | `no_policy_default_deny` | 数据结构 §11 原句「项目策略优先，**缺省拒绝未授权出网**」 |
| 5 | 策略是否 `LOCAL_ONLY` | `project_local_only` | 验收第一半的核心 |
| 6 | 供应商是否在白名单 | `provider_not_allowed` | |
| 7 | 数据类别是否全在白名单内 | `data_class_not_allowed` | 最后一道：前六步全过也要逐个核对字段类别 |

**第 3 步的位置最容易写错**：若把"缺省拒绝"（第 4 步）放在"本地供应商"（第 3 步）之前，
一个没配策略的项目连本地模型都调不了 —— 而本地调用根本没有出网，
拦它没有任何合规收益，只会让系统在模式 B 下完全不可用。

**第 2 步也必须早于第 3 步**：能力不支持是"这个供应商干不了这个活"，
与"能不能出网"是两个问题。写反了会让"本地供应商不支持某能力"被报成"允许"。

## 为什么"每个否决原因都有名字"

否决原因会进三处：出网留痕、错误 details、测试断言。
用一个笼统的 `"denied"` 会让"为什么被拒"只能靠读代码猜，
而对应 ADR-10 的违规排查（"这次调用到底被哪条规则拦了"）恰恰需要它。

## 为什么没有"平台级策略回落"

`EgressPolicyView.project_id` 允许为 `None`（平台级），但**本包不做回落**：
数据结构的原句是"项目策略优先，**缺省拒绝**" —— 即"没有再拒"。
若做成"没配项目策略就按平台默认出网"，那么"忘记给项目配策略"
会静默变成"按平台默认出网"，而配置遗漏是 ADR-10 明确列为**不能免责**的情形。
登记 OP-126 / OP-127。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from rednote.modules.gateway.types import (
    EGRESS_ALLOWED,
    EXTERNAL_POLICY_LOCAL_ONLY,
    FORBIDDEN_EGRESS_DATA_CLASSES,
    NETWORK_EXTERNAL,
    PROVIDER_ACTIVE,
    REASON_CAPABILITY_UNSUPPORTED,
    REASON_DATA_CLASS_NOT_ALLOWED,
    REASON_LOCAL_PROVIDER,
    REASON_NO_POLICY,
    REASON_PROJECT_LOCAL_ONLY,
    REASON_PROVIDER_DISABLED,
    REASON_PROVIDER_NOT_ALLOWED,
    EgressDecision,
)


@dataclass(frozen=True)
class EgressPolicyView:
    """一条策略的不可变快照。`project_id=None` 表示平台级（本包不做回落，见模块 docstring）。"""

    project_id: uuid.UUID | None
    policy_version: str
    external_policy: str
    allowed_provider_ids: tuple[uuid.UUID, ...] = ()
    allowed_data_classes: tuple[str, ...] = ()


def evaluate_egress(
    *,
    policy: EgressPolicyView | None,
    provider_id: uuid.UUID,
    network_mode: str,
    provider_status: str,
    provider_capabilities: Iterable[str],
    capability: str,
    data_classes: Iterable[str],
) -> EgressDecision:
    """判定一次能力请求能否发往该供应商（以及**是否出网**）。

    纯函数：同样的入参永远得到同样的结论，可穷举单测。
    """
    supported = frozenset(provider_capabilities)
    declared = frozenset(data_classes)

    # 1) 供应商必须处于启用状态。
    if provider_status != PROVIDER_ACTIVE:
        return EgressDecision(
            allowed=False,
            reason=REASON_PROVIDER_DISABLED,
            detail={"providerStatus": provider_status},
        )

    # 2) 能力必须被该供应商支持（与网络模式无关）。
    if capability not in supported:
        return EgressDecision(
            allowed=False,
            reason=REASON_CAPABILITY_UNSUPPORTED,
            detail={"capability": capability, "supported": sorted(supported)},
        )

    # 3) 本地供应商：调用会发生，但**没有出网** → 直接允许，不进入策略判定。
    if network_mode != NETWORK_EXTERNAL:
        return EgressDecision(allowed=True, reason=REASON_LOCAL_PROVIDER)

    # —— 以下每一步都只在"确实要出网"时才有意义 ——

    # 4) 没有策略 → 拒绝（数据结构 §11「缺省拒绝未授权出网」）。
    if policy is None:
        return EgressDecision(
            allowed=False,
            reason=REASON_NO_POLICY,
            detail={"capability": capability},
        )

    # 5) 项目级 LOCAL_ONLY → 拒绝。**这条没有例外、没有回落、没有降级。**
    if policy.external_policy == EXTERNAL_POLICY_LOCAL_ONLY:
        return EgressDecision(
            allowed=False,
            reason=REASON_PROJECT_LOCAL_ONLY,
            policy_version=policy.policy_version,
            detail={"capability": capability},
        )

    # 6) 供应商必须在策略白名单内。
    #
    # **空白名单 = 一个都不允许**（不是"不限制"）：`in` 判定天然给出这个语义。
    # 与 WP15 的 `list_audit_events_for_projects` 对空集合"返回空而不是不过滤"同一判断 ——
    # 否则一个"只填了 external_policy 但忘了填白名单"的策略会变成"所有供应商都能调"，
    # 而那种失败**不报错**（策略看起来配了）。
    if provider_id not in frozenset(policy.allowed_provider_ids):
        return EgressDecision(
            allowed=False,
            reason=REASON_PROVIDER_NOT_ALLOWED,
            policy_version=policy.policy_version,
            detail={"allowedProviderCount": len(policy.allowed_provider_ids)},
        )

    # 7) 数据类别必须逐个在白名单内，且不得命中禁止出网清单（ADR-10 E-5）。
    forbidden = declared & FORBIDDEN_EGRESS_DATA_CLASSES
    if forbidden:
        return EgressDecision(
            allowed=False,
            reason=REASON_DATA_CLASS_NOT_ALLOWED,
            policy_version=policy.policy_version,
            detail={"forbiddenDataClasses": sorted(forbidden)},
        )
    allowed_classes = frozenset(policy.allowed_data_classes)
    disallowed = declared - allowed_classes
    if disallowed:
        return EgressDecision(
            allowed=False,
            reason=REASON_DATA_CLASS_NOT_ALLOWED,
            policy_version=policy.policy_version,
            detail={
                "disallowedDataClasses": sorted(disallowed),
                "allowedDataClasses": sorted(allowed_classes),
            },
        )

    return EgressDecision(
        allowed=True, reason=EGRESS_ALLOWED, policy_version=policy.policy_version
    )


__all__ = ["EgressPolicyView", "evaluate_egress"]
