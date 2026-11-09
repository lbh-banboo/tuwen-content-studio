"""集中日志配置与 requestId 注入（WP47 ①，ADR-11 C6）。

## 这个模块解决的真实问题

本仓有 27 处 `logger.*` 调用（`info` 10 / `warning` 10 / `exception` 5 / `debug` 2），
但**全仓没有任何日志配置**：`logging.basicConfig` / `dictConfig` 零命中，
`Settings.log_level` 也没有任何消费者（本机实测 `root.handlers == []` 且
`root.level == WARNING`）。

后果是**反直觉的**：`logger.info(...)` 默认走 root、root 无 handler 且级别高于
INFO ⇒ **这些日志一条都没有落过盘**。排障时"日志里怎么没有这一条"不是代码没写，
而是配置没接。

同时协议 §1 要求每条日志能串到具体请求，而 `requestId` 已经有单源
（`platform/errors.py` 的 `_request_id_var`），只是没有人把它送进日志。

## 为什么用 Record Factory，不用 Filter（本机实测的决定性依据）

两条路都能给日志加字段，但**取值时刻不同**：工厂在**构造记录时**取，
Filter 挂在 handler 上时在 **`handle()` 时**取。两者仅在"构造与输出不在同一
执行上下文"时分化 —— 而这个前提**必须实测界定**（本机走了三次弯路）：

| 输出形态 | 工厂 | Filter | 说明 |
|---|---|---|---|
| 同步 `StreamHandler`（构造与输出同栈） | 正确 | **也正确** | 此时 `handle()` 就在调用点，ContextVar 仍有效 ⇒ 两方案等价 |
| `QueueHandler` + `QueueListener`（`handle()` 在**监听线程**） | **正确** | **记成 `unknown`** | 监听线程里没有请求上下文，`ContextVar` 读的是默认值 |

因此选工厂的**准确理由**是「**取值与输出路径解耦**」，不是"Filter 一定错"。
第 18 条纪律的实例：第一次写这条论据时我把它写成"Filter 必然记错"，实测
发现同步路径下它是对的 —— **论据比结论更容易写错**，因为结论能测、论据常被
当成"显然"。

为什么这个区分对本仓是**当下就重要**的：`QueueHandler` 是生产环境异步/批量
落盘与多进程日志的标准做法（内网日志采集器一旦接入，就是这条路径）。而这类
错值的形态是**静默的** —— 字段在、格式对、日志也输出，只有值没用。比缺字段
难查一个量级。

## 工厂是进程级全局状态，必须幂等

`setLogRecordFactory` 是**全局**的。本仓的测试夹具会反复调用 `create_app()`
（`conftest.py` 有 4 个 app fixture），若每次都包一层，经过 N 个测试后工厂链会
有 N 层深 —— 不报错，但每建一条日志记录都要穿过 N 个包装函数。
`install_request_id_factory()` 因此带**幂等标记**：重复调用只更新取值器，
不重复包装（本机实测：连续调用 3 次，只装 1 层，值仍然正确）。

## `requestId` 是保留字段

工厂在**构造时**就往 record 上写 `requestId`。而 `logging` 的 `extra=` 是在
工厂之后执行的 `setattr`，且它会拒绝"覆盖已存在的属性"：

```
KeyError: "Attempt to overwrite 'requestId' in LogRecord"
```

⇒ **调用方不得用 `extra={"requestId": ...}`**。要改这个值请改 ContextVar
（`set_request_id`），因为那是唯一真相。本模块有对应测试把这个边界钉住，
避免它将来变成"莫名其妙崩在日志行上"的谜团。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Final

from rednote.platform.configuration import Settings
from rednote.platform.errors import get_request_id

#: 日志里的 requestId 字段名。与协议 §1 的 `meta.requestId` 保持**同一口径**
#: （camelCase）—— 日志与响应体用同一个名字，排障时不需要做心智转换。
#:
#: ⚠️ 这是**保留字段**：`extra={"requestId": ...}` 会抛 `KeyError`（见模块 docstring）。
REQUEST_ID_LOG_FIELD: Final[str] = "requestId"

#: 日志行格式。
#:
#: 决策 D4（`docs/m5/WP47-探针与指标说明.md` §2.2）：**在末尾追加 requestId 占位，
#: 不改既有格式结构**。本仓此前无任何 format（root 无 handler），因此这里定义的
#: 是一个新格式，但其形态刻意贴近 `logging` 的惯例（时间/级别/来源/消息），
#: 并**只做加法** —— 从零建立时不会打断任何既有解析器。
LOG_FORMAT: Final[str] = (
    "%(asctime)s %(levelname)s %(name)s %(message)s requestId=%(requestId)s"
)

#: 时间用 ISO8601（带时区偏移）。日志要跨机器比对，"本地时间无时区"会在
#: 双实例（ADR-11 C8）场景下产生难以察觉的排序错误。
LOG_DATEFMT: Final[str] = "%Y-%m-%dT%H:%M:%S%z"

#: 不接管 root logger 的环境。
#:
#: **为什么需要这个豁免**：`configure_logging` 会往 root 挂 handler，而 pytest
#: 与 root 日志共享同一个进程 —— 在测试环境接管 root，会改变**同进程里其他包**
#: 的日志行为（风险 R8：表现为"别的包莫名其妙红"）。测试环境已经由 pytest
#: 自己管理捕获，本模块不该插手。
#:
#: ⚠️ 代价必须说清楚：**测试环境不会真正安装 handler**，因此"handler 装没装对"
#: 这件事在默认测试路径上**测不到**（纪律："没跑"与"跑失败"同形）。
#: 补偿方式：测试里用非 test 环境显式调一次，真的抓一遍输出（见
#: `tests/test_observability.py::test_configure_logging_installs_handler_in_non_test_env`）。
LOG_SETUP_EXEMPT_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"test"})

#: 幂等标记：标记在**工厂对象**上，用于识别"这已经是我们装的工厂"。
_FACTORY_MARKER: Final[str] = "_is_rednote_request_id_factory"

#: 幂等标记：标记在**handler 对象**上，用于识别"这个 handler 是本模块装的"。
_HANDLER_MARKER: Final[str] = "_is_rednote_observability_handler"

#: 本应用 logger 的根名字。**级别设在这里，不设在 root**（CR-3）。
APP_LOGGER_NAME: Final[str] = "rednote"


def get_request_id_for_logging() -> str:
    """供日志配置使用的 requestId 读取器。**这是 `install_request_id_factory` 的默认取值器。**

    ## 为什么它不是摆设（CR-1）

    本模块第一版写成 `getter = request_id_getter or get_request_id` —— 用的是从
    `errors` **直接导入**的那个函数，于是这个转发层**零调用**，
    而它的 docstring 却写着"这个真的被调用"。

    这与本包立项要修的那条缺陷（`main.py` 的 `get_request_id_for_logging` 悬空、
    却声称服务一份不存在的日志配置）**是同一个形态**：不是"声称有防线"，
    是"声称自己不是摆设"。**删掉一个悬空符号的同时新造了一个** ⇒ 缺陷的形态
    没有被消灭，只换了藏身处（铁律 14 的"修完一个实例 ≠ 修完这族病"）。

    因此默认取值器**直接指向本函数**，让"真的被调用"成为可阅读的事实，
    并由测试断言默认链的终点（见 `tests/test_observability.py`）。

    ## 与 `rednote.main.get_request_id_for_logging` 的分工

    `main.py` 的同名函数是本包之前的遗留，**零调用**，现保留为薄转发
    （理由见 `platform/observability/__init__.py` 的模块 docstring）：
    删掉它会让那个历史缺陷的证据一起消失。
    """
    return get_request_id()


class RequestIdLogRecordFactory:
    """在 `LogRecord` **构造时**把当前 requestId 写进 record。

    见模块 docstring 的实测对照表 —— 选工厂而非 Filter 的唯一理由是**取值时刻**：
    工厂在请求处理中执行（ContextVar 有效），Filter 在输出时执行（可能已复位）。

    用组合（持有 `previous`）而不是继承 `logging.LogRecord`：`setLogRecordFactory`
    的既有值可能是另一个第三方包装器，不是类。链式调用保持它们全部生效。
    """

    def __init__(
        self,
        previous: Callable[..., logging.LogRecord],
        request_id_getter: Callable[[], str],
    ) -> None:
        self._previous = previous
        self._request_id_getter = request_id_getter

    @property
    def previous(self) -> Callable[..., logging.LogRecord]:
        """被包装的上一层工厂（供测试断言链深）。"""
        return self._previous

    def __call__(self, *args: Any, **kwargs: Any) -> logging.LogRecord:
        record = self._previous(*args, **kwargs)
        # 只在缺失时写：给"将来某个 handler 或中间件想显式指定"留出扩展位。
        # 注意 `extra=` 不走这里 —— 它在工厂**之后**执行且拒绝覆盖（见模块 docstring）。
        if REQUEST_ID_LOG_FIELD not in record.__dict__:
            record.__dict__[REQUEST_ID_LOG_FIELD] = self._request_id_getter()
        return record


def install_request_id_factory(
    request_id_getter: Callable[[], str] | None = None,
) -> RequestIdLogRecordFactory:
    """幂等安装 requestId 工厂；重复调用只更新取值器，不重复包装。

    返回**当前生效的**工厂对象（无论本次是否新建），让调用方/测试能断言链深。

    ⚠️ 默认取值器是**本模块的** `get_request_id_for_logging`，**不是**直接从
    `errors` 导入的 `get_request_id`（CR-1）。两者行为相同，但用前者让"这个
    转发层真的被调用"成为事实而不是声称 —— 首版写成后者时，转发层零调用，
    而它的 docstring 写着"这个真的被调用"。
    """
    getter = request_id_getter or get_request_id_for_logging
    current = logging.getLogRecordFactory()

    if getattr(current, _FACTORY_MARKER, False):
        # 已装过 ⇒ 只换取值器。重复包装会让每条日志穿越 N 层包装函数，
        # 且层数随 create_app() 调用次数增长（测试夹具会反复调用）。
        current._request_id_getter = getter  # type: ignore[attr-defined]  # noqa: SLF001
        return current  # type: ignore[return-value]

    factory = RequestIdLogRecordFactory(current, getter)
    setattr(factory, _FACTORY_MARKER, True)
    logging.setLogRecordFactory(factory)
    return factory


def _resolve_level(raw: str) -> int:
    """把配置里的级别名解析成 `logging` 的整数级别。

    非法值**回落 INFO 并告警**，而不是抛异常：日志级别写错的正确后果是
    "记多了/记少了"，不该升级成"服务起不来"。但必须让这件事**可见** ——
    静默回落会让"设了 DEBUG 却没生效"变成需要读源码才能明白的现象。
    """
    candidate = raw.strip().upper()
    level = logging.getLevelNamesMapping().get(candidate)
    if isinstance(level, int):
        return level
    logging.getLogger(__name__).warning(
        "无法识别的日志级别，回落 INFO",
        extra={"configured_log_level": raw, "fallback_log_level": "INFO"},
    )
    return logging.INFO


def _build_formatter() -> logging.Formatter:
    """构造日志 formatter。

    ⚠️ **必须给 `defaults`**（本机实测）：formatter 引用了 `%(requestId)s`，
    而该字段由工厂注入。一旦工厂因任何原因未生效（例如第三方代码在
    `import` 期就调了 `setLogRecordFactory` 把我们挤掉），缺字段的后果是
    **`ValueError: Formatting field not found in record`，整条日志被丢弃**
    —— 不是"少个字段"，是"这条日志没了"。

    `defaults` 让最坏情况降级为"字段值是 unknown"，日志仍在。这是**保险**，
    不是主路径（主路径是工厂注入真实值）。
    """
    return logging.Formatter(
        LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        defaults={REQUEST_ID_LOG_FIELD: "unknown"},
    )


def _ensure_root_handler() -> logging.StreamHandler:
    """确保 root 上**有一个**本模块的 handler；**不动**既有 handler。

    **为什么不用 `dictConfig`**：`dictConfig` 的 `handlers` 是声明式的 ——
    它会把 root 的 handler 列表**替换**成配置里的那一组。本仓的现状是
    root 无 handler（替换无损失），但"当前恰好无损失"不是设计依据：
    任何将来加进来的 handler（或测试框架临时挂的捕获 handler）都会静默消失。
    风险 R8 要求配置函数"可重入且不删除既有 handler"，命令式实现能在代码里
    **显式地**保证这一点，声明式配置做不到。

    `propagate` / `disable_existing_loggers` 同理：一律不碰。
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, _HANDLER_MARKER, False):
            return handler

    handler = logging.StreamHandler()
    handler.setFormatter(_build_formatter())
    setattr(handler, _HANDLER_MARKER, True)
    root.addHandler(handler)
    return handler


def configure_logging(
    settings: Settings,
    request_id_getter: Callable[[], str] | None = None,
) -> bool:
    """装配集中日志配置。**幂等**，可被 `create_app()` 在每次装配时调用。

    返回**本次是否安装了 root handler**（`True` = 装了/已存在，`False` = 环境豁免）。
    返回值存在的理由是纪律第 5 条："没跑"与"跑失败"同形 —— 让"这次到底配没配"
    变成一个**可断言的事实**，而不是靠读日志文件发现。

    只做三件事：装工厂、装 handler（除豁免环境）、设级别。**不改**既有 handler、
    不改 `propagate`、不改 `disable_existing_loggers`。

    ## 级别设在 `rednote` logger 上，**不设在 root**（CR-3）

    第一版写的是 `logging.getLogger().setLevel(level)` —— 那会把 **root** 降到 INFO，
    于是**所有未被单独设级的第三方 logger 一起继承 INFO**。本机实测三个后果：

    ```
    INFO sqlalchemy.engine.Engine BEGIN (implicit) SELECT users.email, users.password_hash FROM users WHERE id = (...)
    INFO uvicorn.access GET /x 200
    INFO httpx HTTP Request: POST https://internal.svc/token
    ```

    即 **SQL 语句与绑定参数（含个人数据）、内网 URL、全部访问日志** 一起打开 ——
    而这是**默认行为**（`Settings.log_level` 默认 `INFO`），不需要任何人做错事。
    它与本包的设计目标直接冲突：ADR-11 C6 是"仅运维面"，`metrics.py` 的 build info
    标签刻意不含主机名/DSN/凭据，而**日志面反而是敞开的**。

    修法实测有效（只把 `rednote` 设成配置级别、root 保持 WARNING）：
    本 app 的 INFO 照常经 root handler 输出，第三方 INFO **不**输出。
    点分名天然分层 ⇒ 本 app 全部子 logger（`rednote.modules.*`）一并生效。
    """
    install_request_id_factory(request_id_getter)

    if settings.environment in LOG_SETUP_EXEMPT_ENVIRONMENTS:
        return False

    level = _resolve_level(settings.log_level)
    handler = _ensure_root_handler()
    handler.setLevel(level)
    # ⚠️ 只降**本应用**的 logger，root 不动（CR-3）。见 docstring 的实测对照。
    logging.getLogger(APP_LOGGER_NAME).setLevel(level)
    return True


__all__ = [
    "APP_LOGGER_NAME",
    "LOG_DATEFMT",
    "LOG_FORMAT",
    "LOG_SETUP_EXEMPT_ENVIRONMENTS",
    "REQUEST_ID_LOG_FIELD",
    "RequestIdLogRecordFactory",
    "configure_logging",
    "get_request_id_for_logging",
    "install_request_id_factory",
]