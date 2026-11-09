"""固定版本 ZIP 导出的纯函数（WP35）：入参校验、成员命名、流式打包。

全部**不读库**，输入输出都是值。打包函数写入调用方给的**文件对象**
（临时文件），**绝不使用 `BytesIO`**：目标若是内存，峰值内存与总包大小同阶，
等于没有流式（R1）。
"""

from __future__ import annotations

import re
import uuid
import zipfile
from collections.abc import Callable, Iterator
from typing import BinaryIO

from rednote.modules.exports.types import (
    MAX_CONTENT_ITEMS_PER_EXPORT,
    MIN_CONTENT_ITEMS_PER_EXPORT,
    REASON_CONTENT_IDS_DUPLICATE,
    REASON_CONTENT_IDS_EMPTY,
    REASON_CONTENT_IDS_TOO_MANY,
    ZIP_FIXED_DATE_TIME,
    FrozenContent,
)

#: 复制块大小。与 `files.storage.CHUNK_SIZE` 同量级，各写一份（平级不互 import）。
COPY_CHUNK_SIZE = 1024 * 1024


class SchemaViolation(ValueError):
    """入参违规。`reason` 是 `types.REASON_*` 之一，由 API 层翻成 422。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def validate_content_item_ids(
    ids: list[uuid.UUID],
) -> tuple[uuid.UUID, ...]:
    """校验并**保序**返回内容 id。空 / 超限 / 重复 ⇒ `SchemaViolation`。

    重复必须拒而不是静默去重：静默去重会让用户以为导出了 3 篇实际只有 2 篇，
    与 `item_count` 的声明不一致（R4 同族）。
    """
    if len(ids) < MIN_CONTENT_ITEMS_PER_EXPORT:
        raise SchemaViolation(REASON_CONTENT_IDS_EMPTY, "至少选择一条内容。")
    if len(ids) > MAX_CONTENT_ITEMS_PER_EXPORT:
        raise SchemaViolation(
            REASON_CONTENT_IDS_TOO_MANY,
            f"一次最多导出 {MAX_CONTENT_ITEMS_PER_EXPORT} 条内容。",
        )
    if len(set(ids)) != len(ids):
        raise SchemaViolation(REASON_CONTENT_IDS_DUPLICATE, "内容 id 不能重复。")
    return tuple(ids)


_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_segment(text: str, *, fallback: str, max_chars: int = 40) -> str:
    """把标题变成可作 ZIP 成员路径一段的字符串。

    去掉路径分隔符与控制字符（ZIP 路径穿越防线之一）、折叠空白、截断；
    结果为空或为 `.`/`..` 时回落 `fallback`。
    """
    cleaned = _UNSAFE.sub("_", text).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)[:max_chars].strip()
    return cleaned or fallback


def member_names(frozen: FrozenContent, *, position: int) -> list[tuple[str, str]]:
    """一条内容在 ZIP 里的成员清单：`[(成员路径, 类别)]`，类别 `text` / `image:<i>`。

    目录名带 `position`（1 起、零填充）+ 内容 id 前 8 位：
    · 序号保证顺序稳定、不依赖标题；
    · id 前缀保证两条同标题内容不撞名（撞名会让 `zipfile` 写出重名成员）。
    """
    folder = (
        f"{position:03d}_"
        f"{safe_segment(frozen.title, fallback='untitled')}_"
        f"{str(frozen.content_item_id)[:8]}"
    )
    names: list[tuple[str, str]] = [(f"{folder}/content.txt", "text")]
    for index, asset in enumerate(frozen.assets, start=1):
        ext = re.sub(r"[^a-z0-9]", "", asset.image_format.lower()) or "bin"
        names.append((f"{folder}/image_{index:02d}.{ext}", f"image:{index - 1}"))
    return names


def render_text(frozen: FrozenContent) -> bytes:
    """`content.txt` 的内容：标题 + 空行 + 正文。UTF-8、LF，字节确定。"""
    return f"{frozen.title}\n\n{frozen.body}\n".encode("utf-8")


def _write_member(
    archive: zipfile.ZipFile, name: str, chunks: Iterator[bytes]
) -> None:
    """流式写一个成员，时间戳钉死。"""
    info = zipfile.ZipInfo(filename=name, date_time=ZIP_FIXED_DATE_TIME)
    info.compress_type = zipfile.ZIP_STORED  # 图片已压缩；STORED 让字节更确定
    info.external_attr = 0o644 << 16
    with archive.open(info, mode="w", force_zip64=True) as member:
        for chunk in chunks:
            member.write(chunk)


def build_zip(
    target: BinaryIO,
    contents: list[FrozenContent],
    open_asset: Callable[[uuid.UUID], Iterator[bytes]],
) -> int:
    """把冻结清单流式打进 `target`，返回成员数。

    · `open_asset(file_object_id)` 返回该图的字节块迭代器——**一次只持有一块**；
    · 成员顺序 = 入参顺序 × 目录内固定顺序 ⇒ 同一输入字节恒定；
    · `target` 必须是文件对象（临时文件），调用方负责落盘与关闭。
    """
    count = 0
    with zipfile.ZipFile(target, mode="w", allowZip64=True) as archive:
        for position, frozen in enumerate(contents, start=1):
            for name, category in member_names(frozen, position=position):
                if category == "text":
                    _write_member(archive, name, iter((render_text(frozen),)))
                else:
                    index = int(category.split(":", 1)[1])
                    _write_member(
                        archive,
                        name,
                        open_asset(frozen.assets[index].file_object_id),
                    )
                count += 1
    return count


__all__ = [
    "COPY_CHUNK_SIZE",
    "SchemaViolation",
    "build_zip",
    "member_names",
    "render_text",
    "safe_segment",
    "validate_content_item_ids",
]
