"""乐观并发控制：ETag 与 If-Match（协议 §1.1 / §1.2）。

**为什么需要这一层**：
协议 §1.1 要求"对可变资源修改／审核要求其当前 ETag"，§1.2 规定
412 `VERSION_CONFLICT`（重新读取并展示差异）与 428 `PRECONDITION_REQUIRED`
（缺少 If-Match 等前置条件）。WP10 是第一个真正有"可变资源"的工作包
（项目可 PATCH、可交接负责人），因此在这里把机制一次做对。

**ETag 取值 = `"{resource_id}:{version}"`**，与协议 §1.2 示例
`"content-01:18"` 同形。`version` 取各表的 `lock_version` 列 ——
它本来就是这个语义（每次修改 +1），不必新增字段。

**为什么用单调整数而不是行更新时间**：
时间戳在同一秒内两次修改、或时钟回拨时无法区分版本；`lock_version`
是单调整数，没有歧义。协议示例里的 `18` 也是整数，说明这是原设计意图。

**为什么不允许 `If-Match: *`**：
RFC 9110 允许 `*` 表示"资源存在即可"，但本系统的 `If-Match` 专用于
**版本防护**。若接受 `*`，客户端只要写上 `*` 就绕过了全部并发检查 ——
这会把这个机制降级成"必须多写一个头部"的形式主义。
因此明确拒绝（428），让"忘传 ETag"的写法无法通过。

**为什么按逗号拆分逐个比对**：
RFC 允许 `If-Match: "a", "b"` 的列表形式。拆分后任一匹配即通过，
这样与标准客户端的行为一致，也避免把合法请求误判为 412。
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import Request

from rednote.platform.errors import ApiError, ErrorCode

IF_MATCH_HEADER: Final[str] = "If-Match"

# ETag 由 `resource_id:version` 构成。resource_id 只允许不含 `:` 的稳定标识
# （本项目用 UUID 字符串），因此用 rpartition 从右侧分割最稳。
_ETAG_SEPARATOR: Final[str] = ":"

# 不可变资源 ETag 的版本位前缀。作用见 `build_immutable_etag`：
# 保证它永远不会被 `parse_etag` 当成十进制版本号。
_IMMUTABLE_MARKER: Final[str] = "h"


def build_etag(resource_id: Any, version: int) -> str:
    """构造强 ETag（带双引号，符合 RFC 9110 的 entity-tag 形式）。

    带引号不是装饰：不带引号的 ETag 不是合法 entity-tag，
    客户端与代理可能忽略它，导致并发保护静默失效。
    """
    return f'"{resource_id}{_ETAG_SEPARATOR}{version}"'


def build_immutable_etag(resource_id: Any, payload_hash: str) -> str:
    """构造**不可变资源**的强 ETag：由内容摘要决定，不随时间变化。

    **为什么不能复用 `build_etag(id, lock_version)`**：
    可变资源（草稿、项目、字典）的 ETag 承载"当前是第几版"，
    它必须随每次修改而变 —— 这正是乐观并发的载体。
    不可变资源（版本节点）没有"第几版"这回事：它**永远只有一个版本**。
    若给它也编一个递增号，就等于承诺"它还会变"，与不可变性自相矛盾。

    因此不可变资源的 ETag 取内容摘要的前 16 位十六进制：
      · 同一份内容任何时刻算出同一个 ETag（可缓存、可长期引用）；
      · 内容若被非法改写，ETag 立刻不同 —— 它是**篡改检测器**，
        而不是并发控制手段（不可变资源不需要 If-Match）。
    16 位（64 bit）足够避免偶然碰撞；这里防的是"数据被改过没被发现"，
    不是恶意构造碰撞（真要有冲突，两端必然同时持有原内容，可逐字节比）。

    **与 `parse_etag` 不兼容是有意的，而且做到了"必然不兼容"**：
    `parse_etag` 只接受十进制版本号（`str.isdigit()`）。
    若不可变 ETag 直接写 16 位十六进制，那么**当它恰好全是数字时**
    （概率约 0.27%），`parse_etag` 会误判成一个合法的版本号，
    把一个"格式错误"变成"版本不匹配"（400 → 412），前端行为随之不同。
    加一个 `h` 前缀让十六进制摘要在任何取值下都不满足 `isdigit()`，
    这个边界才不会随运气漂移。
    """
    return f'"{resource_id}{_ETAG_SEPARATOR}{_IMMUTABLE_MARKER}{payload_hash[:16]}"'


def parse_etag(value: str) -> tuple[str, int] | None:
    """解析 ETag 为 `(resource_id, version)`。非法返回 None。

    只接受强 ETag（必须带引号）。弱 ETag（`W/"..."`）返回 None ——
    本系统从不签发弱 ETag，收到弱形式说明客户端改写过它，
    此时应报格式错而不是"版本不匹配"，因为两者对客户端的含义不同：
    前者是"你别改我的 ETag"，后者是"数据真的变了，去重新读"。
    """
    text = value.strip()
    if len(text) < 3 or not (text.startswith('"') and text.endswith('"')):
        return None

    inner = text[1:-1]
    resource_id, separator, version_text = inner.rpartition(_ETAG_SEPARATOR)
    if not separator or not resource_id or not version_text:
        return None

    if not version_text.isdigit():
        return None

    return resource_id, int(version_text)


def require_if_match(
    request: Request,
    *,
    resource_id: Any,
    current_version: int,
    resource_label: str,
) -> None:
    """校验 `If-Match` 前置条件。不满足即抛 `ApiError`。

    判定顺序（不可调换）：
      1. 缺失 → **428**（不是 412 —— "没提供前置条件"与"版本冲突"要分开，
         前者客户端该去读一次，后者客户端该把用户编辑的内容与新版本比较）
      2. 通配符 `*` → **428**（明确不支持，见模块 docstring）
      3. 格式非法 → **400**（格式问题，与版本无关）
      4. 均不匹配当前版本 → **412**，`details.currentEtag` 回带当前值，
         让前端能直接拿到"该改成什么"而不必多发一次 GET
    """
    raw = request.headers.get(IF_MATCH_HEADER)

    if raw is None or not raw.strip():
        raise ApiError(
            ErrorCode.PRECONDITION_REQUIRED,
            f"修改{resource_label}必须携带 {IF_MATCH_HEADER} 头，"
            "值为最近一次读取时返回的 ETag。",
            details={
                "header": IF_MATCH_HEADER,
                "resourceId": str(resource_id),
                "reason": "if_match_missing",
            },
        )

    candidates = [item.strip() for item in raw.split(",") if item.strip()]
    if not candidates:
        raise ApiError(
            ErrorCode.PRECONDITION_REQUIRED,
            f"{IF_MATCH_HEADER} 为空，无法校验版本。",
            details={
                "header": IF_MATCH_HEADER,
                "resourceId": str(resource_id),
                "reason": "if_match_empty",
            },
        )

    if any(item == "*" for item in candidates):
        raise ApiError(
            ErrorCode.PRECONDITION_REQUIRED,
            f"{IF_MATCH_HEADER} 不支持通配符 *，请提供具体的 ETag。",
            details={
                "header": IF_MATCH_HEADER,
                "resourceId": str(resource_id),
                "reason": "if_match_wildcard_unsupported",
            },
        )

    target_id = str(resource_id)
    for item in candidates:
        parsed = parse_etag(item)
        if parsed is None:
            raise ApiError(
                ErrorCode.MALFORMED_REQUEST,
                f"{IF_MATCH_HEADER} 格式非法，应为 \"<资源ID>:<版本号>\" 的带引号形式。",
                details={
                    "field": IF_MATCH_HEADER,
                    "resourceId": target_id,
                    "reason": "if_match_malformed",
                },
            )
        item_id, item_version = parsed
        if item_id == target_id and item_version == current_version:
            return

    raise ApiError(
        ErrorCode.VERSION_CONFLICT,
        f"{resource_label}已被更新，请重新读取最新版本后再提交。",
        details={
            "resourceId": target_id,
            # 回带当前 ETag：前端可直接用它重试，或展示差异。
            "currentEtag": build_etag(resource_id, current_version),
            "providedEtag": raw,
            "reason": "version_mismatch",
        },
    )


def version_conflict(
    *,
    resource_id: Any,
    current_version: int,
    reason: str,
    message: str,
) -> ApiError:
    """构造 412 `VERSION_CONFLICT`（协议 §1.2）。

    **为什么抽成工厂而不是各端点自己拼**：
    `details` 的键名（`resourceId` / `currentEtag` / `reason`）是前端契约的一部分。
    让 5 个端点各自手写这段字典，迟早会有一处漏掉 `currentEtag` ——
    而那正是前端"不发第二次 GET 就能重试"所依赖的字段。
    失败模式是静默的（HTTP 412 照常返回），只有前端行为不对才暴露。

    典型用法是在"前置校验已过、但 SQL 层 `WHERE lock_version = ?` 未命中"时抛出，
    即读与写之间被别的请求抢先提交。
    """
    return ApiError(
        ErrorCode.VERSION_CONFLICT,
        message,
        details={
            "resourceId": str(resource_id),
            # 回带当前 ETag：前端可直接用它重试，或展示差异。
            "currentEtag": build_etag(resource_id, current_version),
            "reason": reason,
        },
    )


__all__ = [
    "IF_MATCH_HEADER",
    "build_etag",
    "build_immutable_etag",
    "parse_etag",
    "require_if_match",
    "version_conflict",
]
