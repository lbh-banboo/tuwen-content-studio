"""对象存储适配器（WP12）。

**为什么要适配器**：内网的对象存储（MinIO）在 M0 阶段尚未就位
（`docs/待定项与问题登记册.md` 的 OP-75：最小链从未在容器里跑起来）。
但上传链路是本包的全部交付，必须在本机能端到端跑通、能被测试覆盖。
因此把"存字节"这件事收敛到一个 Protocol 后面：

  · 本机 → `LocalStorageBackend`（文件系统），**完整实现、有测试**；
  · 内网 → MinIO 实现同一 Protocol，接口已经定死，测试集可直接复用。

**为什么现在不顺手写一个 MinioStorageBackend**：
本机没有 `minio` SDK，写了也无法运行一次 —— 那是"看起来完成了"的代码。
未经验证的实现比"明确未实现"更危险：它会让内网接入的人以为这条路已经趟过。
因此工厂在 `MINIO` 分支上**显式失败并说明原因**，把工作留给真正能验证它的环境。

**两大安全约定**（都对应真实攻击面，不是形式）：

1. **`key` 必须由服务端生成，绝不含用户输入**。
   模型层已经把 `original_name` 与 `object_key` 分开，这里再兜一道：
   所有 key 都要过 `_resolve_under_root()`，解析后仍在根目录内才放行。
   只靠"调用方保证"是不够的 —— 将来有人为了排障传了个带 `..` 的 key，
   没有任何地方会拦住他。

2. **流式处理，不把整个对象读进内存**。
   单个分片 8 MiB、并发 10 个就是 80 MB 常驻；300 MB 的文件若整份进内存，
   一个请求就能把服务打爆。所有读写路径都按 `CHUNK_SIZE` 分块。
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final, Protocol

from rednote.modules.files.models import (
    STORAGE_BACKEND_LOCAL,
    STORAGE_BACKEND_MINIO,
)
from rednote.platform.errors import ApiError, ErrorCode

# 分块大小。1 MiB 是"内存占用"与"系统调用次数"的折中：
# 再小则 300 MB 要走 300+ 次 syscall，再大则并发上传时内存水位抬升。
CHUNK_SIZE: Final[int] = 1024 * 1024


@dataclass(frozen=True)
class StoredObject:
    """一次写入/拼装的结果。

    `sha256` 是**本层算出来的**，不是调用方给的 —— 服务端摘要必须
    来自真实字节流（文档 02 §4："SHA 为服务端结果"）。
    """

    key: str
    byte_size: int
    sha256: str


class StorageBackend(Protocol):
    """存储后端的契约。

    **刻意只放最小集合**：分片写入、拼装、读流、删除、按前缀清理、存在与大小。
    没有"列出目录""改名"这类操作 —— 它们对 MinIO 与文件系统的语义差异很大，
    一旦进入接口，两个实现就会开始分叉。
    """

    @property
    def backend_name(self) -> str:
        """返回 `STORAGE_BACKEND_*` 之一（会写进 `blobs.storage_backend`）。"""
        ...

    def write_part(self, *, key: str, stream: BinaryIO) -> StoredObject:
        """流式写入一片，返回实测大小与摘要。已存在时覆盖。"""
        ...

    def assemble(self, *, part_keys: list[str], target_key: str) -> StoredObject:
        """按顺序流式拼装分片为最终对象，返回实测大小与摘要。"""
        ...

    def open_stream(self, *, key: str) -> Iterator[bytes]:
        """按块产出对象内容。**流式**：调用方可以不落地就转发。"""
        ...

    def delete(self, *, key: str) -> bool:
        """删除单个对象。返回是否真的删掉了（不存在返回 False，不抛）。"""
        ...

    def delete_prefix(self, *, prefix: str) -> int:
        """删除某前缀下的全部对象，返回删除个数。

        用途是上传会话过期时清理临时分片 —— 没有它，用户关掉浏览器
        就会在存储上永久留下半份文件（配额已释放，磁盘却没还）。
        """
        ...

    def move(self, *, from_key: str, to_key: str) -> None:
        """把对象移到新 key。

        **为什么需要它**：对象的最终 key 由内容摘要决定（`object_key_for`），
        而摘要是**拼装之后**才知道的 —— 这是个鸡生蛋问题。
        没有 `move` 就只能"先拼到临时位、算出摘要、再拼一次到正式位"，
        等于把 300 MB 读两遍。有了它，拼一遍、改名即可。
        MinIO 侧用 `copy_object` + `remove_object` 实现（服务端复制，不下载）。
        """
        ...

    def exists(self, *, key: str) -> bool: ...

    def size(self, *, key: str) -> int:
        """对象字节数。不存在抛 `ApiError(RESOURCE_NOT_FOUND)`。"""
        ...


def _resolve_under_root(root: Path, key: str) -> Path:
    """把相对 key 解析成根目录下的绝对路径，越界即拒绝。

    **这是路径穿越的唯一防线**，因此判定方式是"解析后比较"而不是
    "检查字符串里有没有 `..`"：后者会被
    `a/./../../b`、URL 编码、以及符号链接绕过。
    `resolve()` 会把 `.`、`..`、符号链接全部展开，再确认结果仍在
    `root` 之下 —— 这是唯一可靠的判据。
    """
    if not key or key.startswith("/"):
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "存储键非法。",
            details={"reason": "storage_key_malformed"},
        )

    candidate = (root / key).resolve()
    resolved_root = root.resolve()
    # is_relative_to 是 3.9+ 的路径语义比较，比字符串前缀匹配可靠 ——
    # 字符串匹配会把 `/data/root2` 误判成在 `/data/root` 之下。
    if not candidate.is_relative_to(resolved_root):
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "存储键越界。",
            details={"reason": "storage_key_out_of_root"},
        )
    return candidate


class LocalStorageBackend:
    """文件系统实现。用于本机开发与测试。

    **目录布局**（root 下）：

        parts/<upload_id>/<part_no>     上传中的分片
        objects/<prefix>/<sha256>       固化后的对象（按摘要前 2 位分桶）

    `objects/` 用摘要命名而不是用 upload_id：这样**同一份内容重复上传
    天然落在同一个 key 上**，物理去重不需要额外逻辑（文档 02 §4 允许
    企业内去重）。按前 2 位分桶是为了避免单目录下文件过多导致
    `readdir` 变慢（16 位前缀 = 256 个桶，足够分散）。
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        # 只在构造时建一次根目录。子目录由 `_ensure_parent` 按需建立 ——
        # 预先建满 256 个桶没有意义，且会让"根目录被误删"更难发现。
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def backend_name(self) -> str:
        return STORAGE_BACKEND_LOCAL

    def write_part(self, *, key: str, stream: BinaryIO) -> StoredObject:
        target = _resolve_under_root(self._root, key)
        target.parent.mkdir(parents=True, exist_ok=True)

        digest = hashlib.sha256()
        written = 0
        while True:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            written += len(chunk)
            _append_bytes(target, chunk, first=(written == len(chunk)))

        return StoredObject(key=key, byte_size=written, sha256=digest.hexdigest())

    def assemble(self, *, part_keys: list[str], target_key: str) -> StoredObject:
        """顺序拼装。**边拼边算摘要**，不回头读第二遍。

        做法是"写到临时文件、成功后原子改名"：直接写目标 key 的话，
        一旦中途出错会留下一个**内容不完整的对象**，而它在存储上与
        正常对象无法区分（大小对不上才能发现，而没人会去核对）。
        改名是原子的，因此目标 key 要么不存在、要么是完整内容。
        """
        target = _resolve_under_root(self._root, target_key)
        target.parent.mkdir(parents=True, exist_ok=True)

        digest = hashlib.sha256()
        written = 0
        staging = target.with_name(f".{target.name}.assembling")

        try:
            with staging.open("wb") as sink:
                for part_key in part_keys:
                    source = _resolve_under_root(self._root, part_key)
                    if not source.is_file():
                        raise ApiError(
                            ErrorCode.VALIDATION_FAILED,
                            "分片缺失，无法完成上传。",
                            details={
                                "reason": "part_missing",
                                # 只回带分片序号，不回带 key —— key 属服务端内部
                                # 结构，泄漏它等于给出存储布局。
                                "partKey": part_key.rsplit("/", 1)[-1],
                            },
                        )
                    with source.open("rb") as handle:
                        while True:
                            chunk = handle.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            digest.update(chunk)
                            written += len(chunk)
                            sink.write(chunk)
            staging.replace(target)
        except BaseException:
            # 失败时清掉半成品。不留 staging 文件 —— 它不会被任何清理
            # 路径扫到（TTL 只扫 parts/ 前缀），会永久占盘。
            staging.unlink(missing_ok=True)
            raise

        return StoredObject(key=target_key, byte_size=written, sha256=digest.hexdigest())

    def open_stream(self, *, key: str) -> Iterator[bytes]:
        source = _resolve_under_root(self._root, key)
        if not source.is_file():
            raise ApiError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "文件内容不存在。",
                details={"reason": "object_missing"},
            )
        with source.open("rb") as handle:
            while True:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    def delete(self, *, key: str) -> bool:
        target = _resolve_under_root(self._root, key)
        if not target.is_file():
            return False
        target.unlink()
        return True

    def delete_prefix(self, *, prefix: str) -> int:
        """删除前缀下的全部对象。

        **用 shutil.rmtree 而不是逐个 unlink**：一个 300 MB 的文件按 8 MiB
        分片是 38 个文件，逐个删会发 38 次系统调用；更重要的是 shutil
        对目录结构做了正确处理。

        `_resolve_under_root` 依然要过 —— 前缀同样来自服务端，
        但这道检查是"任何进入文件系统的路径都要过"的一致性要求，
        不能因为"这个调用点的参数是安全的"就跳过（下一个人可能改它）。
        """
        target = _resolve_under_root(self._root, prefix.rstrip("/"))
        if not target.exists():
            return 0
        if target.is_dir():
            count = sum(1 for item in target.rglob("*") if item.is_file())
            shutil.rmtree(target)
            return count
        target.unlink()
        return 1

    def move(self, *, from_key: str, to_key: str) -> None:
        """`pathlib.replace` 是原子的 rename（同一文件系统内）。

        原子性很重要：它保证目标 key 要么不存在、要么是完整内容。
        若用"复制 + 删除"，中途崩了会留下一个残缺对象，
        而它在存储上与正常对象无法区分。
        """
        source = _resolve_under_root(self._root, from_key)
        if not source.is_file():
            raise ApiError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "文件内容不存在。",
                details={"reason": "object_missing"},
            )
        target = _resolve_under_root(self._root, to_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)

    def exists(self, *, key: str) -> bool:
        return _resolve_under_root(self._root, key).is_file()

    def size(self, *, key: str) -> int:
        target = _resolve_under_root(self._root, key)
        if not target.is_file():
            raise ApiError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "文件内容不存在。",
                details={"reason": "object_missing"},
            )
        return target.stat().st_size


# ---------------------------------------------------------------------------
# 对象 key 的生成规则
#
# **刻意放在模块级而不是后端类上**：key 是"逻辑定位"，与"怎么存"无关 ——
# 换成 MinIO 之后，同一份内容仍必须落在同一个 key 上，否则物理去重会失效
# （本地存过、切到 MinIO 又要重存一份）。把规则挂在具体后端类上，
# 等于宣布"key 布局是实现细节"，那正是要避免的。
# ---------------------------------------------------------------------------

PARTS_PREFIX: Final[str] = "parts"
OBJECTS_PREFIX: Final[str] = "objects"
STAGING_PREFIX: Final[str] = "staging"


def object_key_for(sha256: str) -> str:
    """由内容摘要推出对象 key。**同一内容 → 同一 key**（物理去重）。

    按前 2 位分桶：单目录下文件过多会让 `readdir` 变慢，
    16 位十六进制前缀 = 256 个桶，足够分散。
    """
    return f"{OBJECTS_PREFIX}/{sha256[:2]}/{sha256}"


def part_key_for(upload_id: object, part_no: int) -> str:
    """分片的临时 key。

    只由服务端生成的 `upload_id` 与整数 `part_no` 组成 ——
    **没有任何用户可控的字符串进入路径**，这是路径穿越的结构性防护：
    不是"我过滤掉了危险字符"，而是"这条路径里根本没有用户的字"。
    """
    return f"{PARTS_PREFIX}/{upload_id}/{part_no}"


def upload_prefix(upload_id: object) -> str:
    """一次上传的全部临时分片前缀（TTL 清理按它批量删）。"""
    return f"{PARTS_PREFIX}/{upload_id}/"


def staging_key_for(*, purpose: str, token: object) -> str:
    """服务端生成资产落库前的**暂存 key**。

    **为什么需要它，而不是直接写 `object_key_for(sha256)`**：
    `LocalStorageBackend.write_part` 是**追加式**写（首块 `"wb"`、后续 `"ab"`）。
    直接对最终 key 写，一旦该 key 已存在（同内容物理去重命中），
    就会把新内容**追加**到旧内容后面 —— 产出一个大小对不上、
    但在存储上与正常对象无法区分的坏对象。
    `assemble()` 内部用 `.{name}.assembling` + `replace()` 绕开了这一点，
    而 `write_part` 没有这层保护，所以调用方必须自己"先写暂存、再 move"。

    **key 里只有服务端生成的 `token`**，`purpose` 是模块内常量、
    `token` 由调用方给 UUID —— 没有任何用户可控字符串进入路径，
    与 `part_key_for` 同一口径（路径穿越的结构性防护）。
    """
    return f"{STAGING_PREFIX}/{purpose}/{token}"


def _append_bytes(path: Path, chunk: bytes, *, first: bool) -> None:
    """追加一块。首块用 "wb" 截断，后续用 "ab" 追加。"""
    with path.open("wb" if first else "ab") as handle:
        handle.write(chunk)


def build_storage_backend(
    *, backend: str, local_root: Path | None
) -> StorageBackend:
    """按配置构造存储后端。

    **MINIO 分支显式失败**：本机没有 `minio` SDK，写了也无法运行 ——
    未经验证的实现比"明确未实现"更危险，它会让内网接入的人以为
    这条路已经趟过。这里的报错文案直接说明该做什么。
    """
    if backend == STORAGE_BACKEND_LOCAL:
        if local_root is None:
            raise ApiError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "本地存储根目录未配置。",
                details={"reason": "storage_local_root_missing"},
            )
        return LocalStorageBackend(local_root)

    if backend == STORAGE_BACKEND_MINIO:
        # 内网接入时在这里新增 MinioStorageBackend，实现上面的 Protocol。
        # 测试集（tests/test_files_wp12.py）对两种后端通用 ——
        # 它们只依赖 Protocol，不依赖具体实现。
        raise ApiError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "MinIO 存储后端尚未实现（内网接入时补）。",
            details={"reason": "storage_backend_unimplemented", "backend": backend},
        )

    raise ApiError(
        ErrorCode.VALIDATION_FAILED,
        "未知的存储后端。",
        details={"reason": "storage_backend_unknown", "backend": backend},
    )


__all__ = [
    "CHUNK_SIZE",
    "OBJECTS_PREFIX",
    "PARTS_PREFIX",
    "STAGING_PREFIX",
    "LocalStorageBackend",
    "StorageBackend",
    "StoredObject",
    "build_storage_backend",
    "object_key_for",
    "part_key_for",
    "staging_key_for",
    "upload_prefix",
]
