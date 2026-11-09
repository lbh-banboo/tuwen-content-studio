"""任务处理器注册表与内置处理器（WP13）。

**注册表按"任务 kind"索引，投递通道按"事件类型"索引** —— 两者刻意分开：

  · **事件处理器**（`dispatch.HandlerRegistry`）回答"某件事发生了，谁关心"。
    它由**下游包**注册（WP17 关心 `file.uploaded`）；
  · **任务处理器**（本模块的 `TaskHandlerRegistry`）回答"这类活怎么干"。
    它由**本包与 WP16/WP17** 注册。

合成一个 registry 会让"注册一个事件处理器"和"注册一个任务处理器"
共用一个命名空间，而事件类型（`file.uploaded`）与任务 kind（`RESEARCH_PARSE`）
的命名习惯不同，迟早撞名。

**内置处理器只有两个，都是为了"不依赖任何外部服务也能验证本包的语义"**：

| kind | 行为 | 用途 |
|---|---|---|
| `FAKE_PROVIDER` | 直接成功，`result_ref` 回带一个假的外部 id | **G1 的"假供应商任务"**（研发计划 04 §4.2 明写"假供应商只用于此阶段测试"） |
| `ALWAYS_FAIL` | 永远失败且可重试 | 验证重试与 try=值耗尽后的落 FAILED |

真实处理器（解析文件、调模型）由 WP17/WP16 注册 ——
本包提供注册点，但不替它们决定业务语义。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from rednote.modules.jobs.repository import JobRepository
from rednote.modules.jobs.types import ClaimedJob, HandlerOutcome

TaskHandlerFn = Callable[[ClaimedJob, JobRepository], HandlerOutcome]

# 假供应商任务（G1 用）。名字里带 FAKE 是刻意的 ——
# 研发计划明写"假供应商不能作为真实生产验收"，
# 名字必须让它在任何清单里一眼可辨。
KIND_FAKE_PROVIDER: Final[str] = "FAKE_PROVIDER"
# 恒失败（验证重试耗尽）。
KIND_ALWAYS_FAIL: Final[str] = "ALWAYS_FAIL"


@dataclass(frozen=True)
class TaskHandlerRegistry:
    """任务 kind → 处理器。

    **不可变**（`register` 返回新实例）：处理器注册通常发生在应用装配期，
    让它返回新实例可以避免"测试里注册了一个处理器，影响后续测试"这种
    顺序依赖 —— 与 conftest 里清配置缓存的理由相同。
    """

    _handlers: dict[str, TaskHandlerFn]

    @classmethod
    def empty(cls) -> TaskHandlerRegistry:
        return cls(_handlers={})

    def register(self, kind: str, handler: TaskHandlerFn) -> TaskHandlerRegistry:
        """返回注册了新处理器的新 registry（原实例不变）。"""
        return TaskHandlerRegistry(
            _handlers={**self._handlers, kind: handler}
        )

    def handler_for(self, kind: str) -> TaskHandlerFn | None:
        return self._handlers.get(kind)

    def registered_kinds(self) -> frozenset[str]:
        return frozenset(self._handlers)


# ---------------------------------------------------------------------------
# 内置处理器
# ---------------------------------------------------------------------------


def fake_provider_handler(
    claimed: ClaimedJob, repo: JobRepository
) -> HandlerOutcome:
    """假供应商：不调任何外部服务，直接成功。

    **`result_ref` 用 job_id 派生**（而不是随机 UUID）：这样同一个任务
    重复执行会得到同样的 `result_ref` —— 在验证"重复投递不重复执行"时，
    断言"两次的 result_ref 相同"才有意义。随机值会让这条断言失效。
    """
    return HandlerOutcome(
        state="SUCCEEDED",
        result_ref=f"fake-provider/{claimed.job_id}",
    )


def always_fail_handler(
    claimed: ClaimedJob, repo: JobRepository
) -> HandlerOutcome:
    """恒失败且可重试 —— 用于验证"重试若干次后落 FAILED"这条路径。"""
    del claimed, repo  # 刻意不依赖任何输入：它就是要稳定地失败
    return HandlerOutcome(
        state="FAILED",
        error_code="INTENTIONAL_FAILURE",
        retryable=True,
    )


def build_default_registry() -> TaskHandlerRegistry:
    """内置处理器集合（本机与测试用）。

    真实处理器由 WP16/WP17 在各自的装配点注册 ——
    本包不替它们决定 kind 命名与业务语义。
    """
    return (
        TaskHandlerRegistry.empty()
        .register(KIND_FAKE_PROVIDER, fake_provider_handler)
        .register(KIND_ALWAYS_FAIL, always_fail_handler)
    )


__all__ = [
    "KIND_ALWAYS_FAIL",
    "KIND_FAKE_PROVIDER",
    "TaskHandlerFn",
    "TaskHandlerRegistry",
    "always_fail_handler",
    "build_default_registry",
    "fake_provider_handler",
]
