"""会话令牌与 CSRF 密钥的生成、摘要、加密（WP08）。

**三层凭据，各自的存法不同 —— 这不是可以随便统一的**：

| 凭据 | 存法 | 为什么 |
|---|---|---|
| 会话令牌 | SHA-256 摘要 | 只需"验证持有者是否匹配"，无需还原。库被只读泄露也无法接管会话 |
| CSRF 密钥 | SHA-256 摘要 | 同上 |
| OIDC 令牌 | Fernet 对称加密 | 业务要在过期前用它调 IdP（userinfo/refresh），必须能还原 |

**为什么用 `secrets` 而不是 `random`**：
`random` 是 Mersenne Twister，可被观察若干输出后预测。会话令牌必须用
密码学安全的随机源。`secrets.token_urlsafe` 底层是 `os.urandom`。

**为什么令牌要带版本前缀**：
`rn1.` 前缀让"这是本系统的令牌"一眼可辨，且为将来轮换算法留出空间 ——
格式变更时可用前缀区分新旧，而不是靠长度猜测（长度猜测会误判）。

**为什么 CSRF 用"双提交 + Origin 校验"而不是仅靠 SameSite**：
SameSite=Lax 能挡住大部分跨站 POST，但：
  1. 老浏览器或配置错误时会降级；
  2. 同站子域（如被入侵的静态资源域）之间仍算 same-site，SameSite 不生效。
因此需要 CSRF 令牌作为独立防线。两层都做，任何一层单独失效都还有另一层。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Final

from cryptography.fernet import Fernet, InvalidToken

# 令牌格式版本前缀。轮换算法时递增，旧令牌可用前缀识别而非靠长度猜测。
TOKEN_PREFIX: Final[str] = "rn1."

# 会话令牌熵：32 字节 → base64url 约 43 字符。256 bit 远超暴力破解需求，
# 同时保持 Cookie 值短小（Cookie 有 4KB 上限，但越短越省带宽）。
_SESSION_TOKEN_BYTES: Final[int] = 32

# CSRF 密钥熵：同样 32 字节。
_CSRF_SECRET_BYTES: Final[int] = 32


class TokenDecryptionError(Exception):
    """OIDC 令牌解密失败。

    单独一个异常类型，是为了让调用方能区分"密钥不对/密文损坏"
    与"没有令牌"（后者是合法的可空状态）。
    """


def generate_session_token() -> str:
    """生成会话令牌明文。**只在创建会话时出现一次，之后不再出现。**"""
    return TOKEN_PREFIX + secrets.token_urlsafe(_SESSION_TOKEN_BYTES)


def generate_csrf_secret() -> str:
    """生成 CSRF 密钥明文。响应给前端后，服务端只保留摘要。"""
    return secrets.token_urlsafe(_CSRF_SECRET_BYTES)


def hash_token(token: str) -> str:
    """令牌摘要（SHA-256 hex，64 字符）。

    **为什么不用加盐慢哈希（bcrypt/argon2）**：
    慢哈希是为了抵抗"弱口令的离线暴力破解" —— 攻击者有字典、有大量时间。
    而会话令牌是 256 bit 随机值，字典不存在，暴力枚举在数学上不可行。
    用慢哈希只会让每个请求多花几十毫秒做无意义的计算（鉴权在热路径上）。
    SHA-256 在这里是正确的选择，不是偷懒。

    用 UTF-8 编码后哈希：令牌是 ASCII，但显式编码避免依赖默认编码行为。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    """恒定时间比较，防时序侧信道。

    **为什么必须用 hmac.compare_digest 而不是 `==`**：
    Python 的字符串 `==` 在发现第一个不同字符时立即返回，
    比较耗时与"匹配前缀长度"正相关。攻击者可通过测量响应时间
    逐字节猜出正确值。compare_digest 的耗时与内容无关。
    """
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _fernet(key: bytes) -> Fernet:
    """由 32 字节密钥构造 Fernet。

    Fernet 要求密钥是 urlsafe base64 编码的 32 字节。调用方（配置层）
    负责提供符合该格式的密钥，这里只做一次转换，不吞异常 ——
    密钥格式错误应在启动时暴露，而不是等到第一次加密才失败。
    """
    import base64

    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_oidc_tokens(payload: str, key: bytes) -> str:
    """加密 OIDC 令牌包（JSON 字符串）。

    输入是一个 JSON 文本而不是多个参数：OIDC 令牌有 access/refresh/id_token
    三种，且可能带 expires_in 等元数据。打包成一个 JSON 让"新增字段"
    不需要改函数签名与数据库列。
    """
    return _fernet(key).encrypt(payload.encode("utf-8")).decode("ascii")


def decrypt_oidc_tokens(ciphertext: str, key: bytes) -> str:
    """解密 OIDC 令牌包。

    `InvalidToken` 说明密钥不对或密文被篡改 —— 两者都是"必须被看到"的异常，
    因此转成显式的 TokenDecryptionError 而不是返回 None。
    返回 None 会让调用方把"密钥配置错误"误当成"没有令牌"，进而在
    需要令牌时才发现问题，那时上下文已经丢失。
    """
    try:
        return _fernet(key).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise TokenDecryptionError(
            "OIDC 令牌解密失败：密钥不匹配或密文已损坏。"
        ) from exc


__all__ = [
    "TOKEN_PREFIX",
    "TokenDecryptionError",
    "constant_time_equals",
    "decrypt_oidc_tokens",
    "encrypt_oidc_tokens",
    "generate_csrf_secret",
    "generate_session_token",
    "hash_token",
]