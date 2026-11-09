#!/usr/bin/env python3
"""OpenAPI 契约导出（WP07，协议 §13.2）。

协议 §13.2 原文要求："CI 从 Pydantic 导出并规范化 OpenAPI，与已发布契约比较；
删除字段、改必填、改枚举语义判定为潜在破坏。"

本脚本实现这条流水线的"导出 + 比较"两步：

    python tools/contracts/export_openapi.py            # 写出 contracts/openapi.json
    python tools/contracts/export_openapi.py --check     # 与已提交契约比较，有差异退出码 1

**为什么必须"规范化"（而不是直接 dump）：**

FastAPI 生成的 schema 里含有天然不稳定的成分，直接落盘会让 CI diff 全是噪声：

1. dict 键顺序依赖 Pydantic 的字段定义顺序与 Python 字典插入顺序 —— 同一份代码
   在不同 Python 小版本上可能产生不同顺序。→ 统一 `sort_keys=True`。
2. `title` 由类名派生。重命名一个 Python 类（纯重构）会让每个引用它的
   `$ref` 与 schema title 都变。→ 递归剥离 `title`（`--keep-titles` 可关闭）。
3. 描述里的空白/行尾差异。→ 统一折叠多行缩进。

规范化**只删 title**，不删任何结构性字段。判定"是不是破坏性变更"由 §13.2 的
规则在 CI 侧做，导出脚本不替它做判断 —— 脚本的职责是给出稳定的字节序列。

退出码：
    0  正常（--check 时表示无差异）
    1  有差异（仅 --check）
    2  导出过程本身失败（app 装配不了、写入失败等）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Final

# ---------------------------------------------------------------------------
# 路径解析：本文件在 tools/contracts/ 下，后端包在 services/backend/src/。
# 用相对本文件的定位而不是 cwd，这样从仓库任意位置调用都能工作
# （CI 里 cwd 不一定是仓库根）。
# ---------------------------------------------------------------------------
_THIS_FILE: Final = Path(__file__).resolve()
_REPO_ROOT: Final = _THIS_FILE.parents[2]
_BACKEND_SRC: Final = _REPO_ROOT / "services" / "backend" / "src"
_DEFAULT_OUTPUT: Final = _REPO_ROOT / "contracts" / "openapi.json"

if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

# 契约导出不应受本机 .env 影响：导出的是"接口形状"，与运行环境无关。
# 若让 .env 里的 environment=prod 生效，docs_url 会被关掉、schema 里可能少内容，
# 导致"本机导出的契约与 CI 导出的契约不一致"。因此强制用 dev 环境导出。
os.environ.setdefault("REDNOTE_ENVIRONMENT", "dev")


def _strip_titles(node: Any, *, in_info: bool = False) -> Any:
    """递归剥离 schema 中的 `title` 键，但保留 `info.title`。

    只剥离 `title`，不动 `description`。理由：description 是契约的一部分
    （前端文档与 SDK 注释都读它），而 schema 里的 title 只是 FastAPI 从类名
    派生的展示字段，没有跨语言契约价值，却是重构噪声的主要来源。

    **`info.title` 必须保留** —— OpenAPI 规范把它列为 info 对象的必需字段，
    剥掉后契约不是合法 OpenAPI 文档，下游工具（swagger-ui、openapi-generator）
    会直接报错。本机实测踩到过这个缺陷：初版无条件递归剥离所有 title，
    导致 info.title 消失。
    """
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key == "title" and not in_info:
                continue
            # info 子树内不再剥离 title。
            result[key] = _strip_titles(value, in_info=(key == "info"))
        return result
    if isinstance(node, list):
        return [_strip_titles(item, in_info=in_info) for item in node]
    return node


def _normalize_descriptions(node: Any) -> Any:
    """折叠描述文本里的行尾空白，避免"改了个空格"产生契约 diff。"""
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key in {"description", "summary"} and isinstance(value, str):
                lines = [line.rstrip() for line in value.splitlines()]
                # 去掉首尾空行，保留内部空行（内部空行是排版的一部分）。
                while lines and not lines[0]:
                    lines.pop(0)
                while lines and not lines[-1]:
                    lines.pop()
                result[key] = "\n".join(lines)
            else:
                result[key] = _normalize_descriptions(value)
        return result
    if isinstance(node, list):
        return [_normalize_descriptions(item) for item in node]
    return node


def build_schema(*, keep_titles: bool = False) -> dict[str, Any]:
    """从 FastAPI app 导出规范化后的 OpenAPI 3.1 schema。"""
    from rednote.main import create_app

    app = create_app()
    schema: dict[str, Any] = app.openapi()

    schema = _normalize_descriptions(schema)
    if not keep_titles:
        schema = _strip_titles(schema)

    _assert_valid_openapi_skeleton(schema)

    # 排序让输出字节级稳定，CI diff 只反映真实变更。
    return schema


def _assert_valid_openapi_skeleton(schema: dict[str, Any]) -> None:
    """自检：规范化不能破坏 OpenAPI 文档的必需结构。

    为什么要在导出侧做这个检查：规范化是"删字段"操作，删错一个必需字段会让
    契约不再合法，但脚本本身会静默成功、把坏契约写进版本库，直到下游工具
    （swagger-ui / openapi-generator / 前端 SDK 生成）才报错 —— 那时排查成本
    高得多。本机实测踩到过：无条件剥离 title 导致 `info.title` 消失。
    """
    problems: list[str] = []
    if not str(schema.get("openapi", "")).startswith("3.1"):
        problems.append("openapi version must be 3.1.x")
    info = schema.get("info")
    if not isinstance(info, dict):
        problems.append("info object is missing")
    else:
        if not info.get("title"):
            problems.append("info.title is missing (required by OpenAPI spec)")
        if not info.get("version"):
            problems.append("info.version is missing (required by OpenAPI spec)")
    if not isinstance(schema.get("paths"), dict):
        problems.append("paths object is missing")

    if problems:
        raise ValueError(
            "normalized schema failed OpenAPI skeleton check: " + "; ".join(problems)
        )


def dumps(schema: dict[str, Any]) -> str:
    """稳定序列化。

    - sort_keys=True       键顺序与插入顺序解耦
    - ensure_ascii=False   中文描述可读（契约文件是给人 review 的）
    - indent=2             逐行 diff 可读
    - 末尾补一个换行       POSIX 文本文件约定，避免 git 报 "no newline at end of file"
    """
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_schema(schema: dict[str, Any], output: Path) -> int:
    """写出契约文件。返回写入字节数。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    text = dumps(schema)
    output.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def compare(existing: Path, generated_text: str) -> tuple[bool, str]:
    """比较已提交契约与本次生成结果。

    返回 (是否一致, 说明)。不一致时给出行级首处差异位置，便于 CI 日志里
    直接定位，不需要人肉跑 diff。
    """
    if not existing.exists():
        return False, f"committed contract not found: {existing}"

    committed_text = existing.read_text(encoding="utf-8")
    if committed_text == generated_text:
        return True, "contract is up to date"

    committed_lines = committed_text.splitlines()
    generated_lines = generated_text.splitlines()
    first_diff = None
    for index in range(max(len(committed_lines), len(generated_lines))):
        left = committed_lines[index] if index < len(committed_lines) else "<eof>"
        right = generated_lines[index] if index < len(generated_lines) else "<eof>"
        if left != right:
            first_diff = (index + 1, left, right)
            break

    detail = "contract differs from committed file"
    if first_diff is not None:
        line_no, left, right = first_diff
        # 差异行本身可能很长（schema 里 JSON 是单行字符串），截断后打印。
        detail += (
            f"\n  first difference at line {line_no}"
            f"\n    committed: {left[:200]}"
            f"\n    generated: {right[:200]}"
        )
    return False, detail


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="export_openapi.py",
        description=(
            "从 FastAPI app 导出规范化 OpenAPI 3.1 契约到 contracts/openapi.json。"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help=f"输出路径，默认 {_DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只比较不写入；与已提交契约有差异时退出码 1。",
    )
    parser.add_argument(
        "--keep-titles",
        action="store_true",
        help="保留 schema title（默认剥离，避免类名重命名产生噪声 diff）。",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="把规范化后的契约同时打印到 stdout。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    output: Path = args.output.resolve()

    try:
        schema = build_schema(keep_titles=args.keep_titles)
        text = dumps(schema)
    except Exception as exc:  # noqa: BLE001 - CLI 边界：把异常折成退出码
        # 导出失败必须给出可诊断信息。这里是开发工具，不是对外 API，
        # 打印异常类型与消息有助于 CI 定位；但仍不回显任何配置值。
        print(f"ERROR: failed to build OpenAPI schema: {type(exc).__name__}: {exc}")
        return 2

    if args.stdout:
        sys.stdout.write(text)

    if args.check:
        consistent, detail = compare(output, text)
        if consistent:
            print(f"OK: {detail} ({output})")
            return 0
        print(f"FAIL: {detail}")
        print("  run without --check to regenerate, then review the diff.")
        return 1

    try:
        size = write_schema(schema, output)
    except OSError as exc:
        print(f"ERROR: failed to write {output}: {exc}")
        return 2

    path_count = len(schema.get("paths", {}))
    operation_count = sum(
        len([key for key in methods if key not in {"parameters", "summary", "description"}])
        for methods in schema.get("paths", {}).values()
        if isinstance(methods, dict)
    )
    print(f"OK: wrote {output}")
    print(f"  bytes: {size}")
    print(f"  paths: {path_count}")
    print(f"  operations: {operation_count}")
    print(f"  openapi: {schema.get('openapi')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())