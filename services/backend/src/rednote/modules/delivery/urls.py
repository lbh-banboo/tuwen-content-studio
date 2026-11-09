"""链接规范化纯函数（WP37）。标准库、无 IO、幂等：`normalize(normalize(x)) == normalize(x)`。

## 范围（拍板 Q1/Q2，2026-10-25）

**只做平台无关的保守处理**，不凭记忆写任何平台（含小红书）专属规则：

| 规则 | 说明 |
|---|---|
| scheme / host 小写 | 仅 `http` / `https`，其余 scheme 拒绝 |
| 去默认端口 | `http:80`、`https:443` |
| 去 fragment | `#...` 不参与同一性 |
| 去尾部多余 `/` | `https://a.com/x//` → `https://a.com/x`；仅有根路径时也去掉 |
| 查询参数按 key（再按 value）排序 | 同参数不同顺序视为同一链接 |
| 去通用追踪参数 | 见 `TRACKING_PARAM_EXACT` / `TRACKING_PARAM_PREFIXES` |

**刻意不做**：联网展开短链（不发任何出站请求）；识别短链（`link_kind` 只产出
`LONG` / `OTHER`，**永远不产出 `SHORT`**）；短链 ⇄ 长链等价（同一条笔记的短链与
长链会被当成两条不同链接，这是已知缺口）；从 URL 抽 `note_id`；大小写以外的路径
改写；percent-encoding 大小写归一。

## `link_kind` 的判法（保守、与平台无关）

- `LONG`：带有非根路径或查询串的 http(s) 链接（"看起来指向具体资源"）；
- `OTHER`：只有主机名、没有路径也没有（去追踪参数后的）查询串。

这**不是**"长链 / 短链"的语义判定，只是取值域里两个可产出值的区分；真正的短链
识别要等有真实样例后另开包。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, quote, urlsplit

from rednote.modules.delivery.types import (
    LINK_KIND_LONG,
    LINK_KIND_OTHER,
    MAX_LINK_URL_CHARS,
)

#: 通用追踪参数（精确名）。★ 来源是**跨平台通用约定**（广告 / 邮件营销点击标识），
#: **不是小红书调研**；小红书专属参数名没有真实样例，故不收录。
TRACKING_PARAM_EXACT: Final[frozenset[str]] = frozenset(
    {"fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "mc_cid", "mc_eid"}
)
#: 通用追踪参数（前缀，大小写不敏感）。同上：通用约定，非小红书调研。
TRACKING_PARAM_PREFIXES: Final[tuple[str, ...]] = ("utm_",)

_DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}


class UrlInvalid(ValueError):
    """链接无法规范化（空、过长、非 http(s)、无主机、含凭据、端口非法等）。"""


@dataclass(frozen=True)
class NormalizedUrl:
    canonical: str
    link_kind: str


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAM_EXACT or lowered.startswith(TRACKING_PARAM_PREFIXES)


def normalize_url(raw: str) -> NormalizedUrl:
    """规范化一条链接。非法输入抛 `UrlInvalid`。"""
    text = raw.strip()
    if not text:
        raise UrlInvalid("链接不能为空。")
    if len(text) > MAX_LINK_URL_CHARS:
        raise UrlInvalid(f"链接不能超过 {MAX_LINK_URL_CHARS} 个字符。")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise UrlInvalid("链接不能含空白或控制字符。")

    try:
        parts = urlsplit(text)
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise UrlInvalid(f"链接格式不合法：{exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise UrlInvalid("只支持 http / https 链接。")
    if not host:
        raise UrlInvalid("链接缺少主机名。")
    if "@" in parts.netloc:
        raise UrlInvalid("链接不能含用户名或密码。")

    netloc = f"[{host}]" if ":" in host else host  # IPv6 字面量需要方括号
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"

    path = parts.path.rstrip("/")

    pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(k)
    ]
    pairs.sort()
    query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs)

    canonical = f"{scheme}://{netloc}{path}"
    if query:
        canonical = f"{canonical}?{query}"
    kind = LINK_KIND_LONG if (path or query) else LINK_KIND_OTHER
    return NormalizedUrl(canonical=canonical, link_kind=kind)


__all__ = [
    "TRACKING_PARAM_EXACT",
    "TRACKING_PARAM_PREFIXES",
    "NormalizedUrl",
    "UrlInvalid",
    "normalize_url",
]
