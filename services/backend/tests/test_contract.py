"""契约导出脚本测试（WP07，协议 §13.2）。

这些测试守的是"CI 契约 diff 可用"这件事本身，而不是接口行为：

- 导出必须**确定性**（同代码两次导出字节一致），否则 CI diff 全是噪声；
- 规范化必须**不破坏 OpenAPI 必需结构**，否则坏契约会静默进版本库；
- `--check` 必须能**真的发现差异**并给出退出码 1，否则 CI 门禁形同虚设。

本机实测踩到过一个真实缺陷并被这里钉住：无条件递归剥离 `title` 会把
`info.title` 一起删掉（OpenAPI 规范要求它是必需字段）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

# 冒烟集：契约与代码同步是前后端协作的前提，秒级可跑完。
pytestmark = pytest.mark.smoke

# 脚本不在包内，用文件路径加载。这样测试不依赖仓库布局是否被 pip 安装过。
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "tools" / "contracts" / "export_openapi.py"
)
_COMMITTED_CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "openapi.json"

# 测试用的临时目录放在项目 evidence/m1/tmp 下，而不是用 pytest 的 tmp_path。
# 原因：受限执行环境里 pytest 的 tmp_path 基目录（/private/var/folders/...）
# 可能因沙箱策略无法创建，报 PermissionError: EEXIST mkdir。放在项目内可写
# 目录里，测试在任何环境都能跑。目录用完即删。
_TMP_ROOT = Path(__file__).resolve().parents[3] / "evidence" / "m1" / "tmp"

# 协议 §1.2 错误码表的独立副本（用于验证契约导出的 enum 与协议一致）。
# 这里刻意不从 rednote 包 import —— 从被测代码 import 会让"代码改错、测试跟着
# 一起错"，测试就失去独立核对的意义。
PROTOCOL_ERROR_CODES: frozenset[str] = frozenset(
    {
        "MALFORMED_REQUEST",
        "SESSION_EXPIRED",
        "PERMISSION_DENIED",
        "EXTERNAL_POLICY_DENIED",
        "RESOURCE_NOT_FOUND",
        "INVALID_TRANSITION",
        "IDEMPOTENCY_CONFLICT",
        "DUPLICATE_PUBLICATION",
        "EVENT_HISTORY_EXPIRED",
        "VERSION_CONFLICT",
        "UPLOAD_TOO_LARGE",
        "VALIDATION_FAILED",
        "BUDGET_EXCEEDED",
        "DIRECTION_UNVERIFIED",
        "PRECONDITION_REQUIRED",
        "RATE_LIMITED",
        "DEPENDENCY_UNAVAILABLE",
    }
)


@pytest.fixture
def tmp_contract() -> Iterator[Path]:
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    target = _TMP_ROOT / f"openapi-{uuid.uuid4().hex}.json"
    try:
        yield target
    finally:
        target.unlink(missing_ok=True)
        # 目录里还有其他并发测试的文件时 rmdir 会失败，忽略即可。
        try:
            _TMP_ROOT.rmdir()
        except OSError:
            pass


def _load_export_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_openapi", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_openapi"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def export_module() -> ModuleType:
    return _load_export_module()


@pytest.fixture(scope="module")
def schema(export_module: ModuleType) -> dict:
    return export_module.build_schema()


def test_export_script_exists() -> None:
    assert _SCRIPT_PATH.is_file(), f"export script missing: {_SCRIPT_PATH}"


def test_schema_is_openapi_31(schema: dict) -> None:
    assert schema["openapi"] == "3.1.0"


@pytest.mark.regression
def test_schema_keeps_required_openapi_fields(schema: dict) -> None:
    """规范化不能删掉 OpenAPI 规范要求的必需字段。

    这是对已修复缺陷的回归保护：初版会剥离 `info.title`。
    """
    assert schema["info"]["title"]
    assert schema["info"]["version"]
    assert isinstance(schema["paths"], dict)


def test_schema_contains_health_paths(schema: dict) -> None:
    """健康检查路径必须在契约里。

    WP08 起增加了 identity 路径，因此这里断言"包含"而不是"等于"。
    用子集断言而非全等，是为了让新增端点不需要改这条测试 ——
    它守的是"健康检查没被漏掉"，不是"契约里只有这两个路径"。
    """
    assert {"/api/v1/health", "/api/v1/health/db"}.issubset(set(schema["paths"]))


def test_schema_contains_identity_paths(schema: dict) -> None:
    """协议 §2 的认证端点必须在契约里（WP08）。"""
    assert {
        "/api/v1/auth/login",
        "/api/v1/auth/callback",
        "/api/v1/auth/logout",
        "/api/v1/me",
    }.issubset(set(schema["paths"]))


@pytest.mark.regression
def test_no_unused_framework_error_schemas(schema: dict) -> None:
    """契约里不得出现 FastAPI 自动生成的 HTTPValidationError / ValidationError。

    这两者是 FastAPI 为"未显式声明错误响应"的端点自动加的默认模型。
    它们与本项目的协议错误体（ErrorEnvelopeForOpenApi）形状不同，
    若混进契约，前端会生成出两套错误的错误类型。

    这条测试守的是"所有端点都必须显式声明 responses"这条约定 ——
    漏声明时不会报错，只会让契约悄悄多出这两个 schema。
    """
    schemas = schema.get("components", {}).get("schemas", {})
    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas


def test_operation_ids_are_stable_and_path_derived(schema: dict) -> None:
    """operationId 必须由 HTTP 契约派生，不含 Python 函数名。

    否则重命名一个函数（纯重构）会在 CI 契约 diff 里被误判成破坏性变更。
    """
    health = schema["paths"]["/api/v1/health"]["get"]
    health_db = schema["paths"]["/api/v1/health/db"]["get"]

    assert health["operationId"] == "get_api_v1_health"
    assert health_db["operationId"] == "get_api_v1_health_db"
    # 不能出现 Python 函数名。
    assert "get_health" not in json.dumps(schema, ensure_ascii=False)


def test_health_success_response_has_concrete_data_schema(schema: dict) -> None:
    """成功响应必须带出 data 的具体结构，而不是裸 object。

    这是"前端能生成 TS 类型"（协议 §13.3）的前提：如果 data 是 Any，
    SDK 会退化成 any，契约就失去约束力。
    """
    response_schema = schema["paths"]["/api/v1/health"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    ref = response_schema["$ref"].rsplit("/", 1)[-1]
    envelope = schema["components"]["schemas"][ref]

    assert set(envelope["properties"]) == {"data", "meta"}
    assert envelope["required"] == ["data", "meta"]
    data_ref = envelope["properties"]["data"]["$ref"].rsplit("/", 1)[-1]
    assert "status" in schema["components"]["schemas"][data_ref]["properties"]


def test_error_response_is_declared_in_contract(schema: dict) -> None:
    """503 错误响应必须在契约里有 body 结构（协议 §1.2 + §13.3）。

    错误响应由 exception_handler 直接构造 JSONResponse 返回，不经过
    response_model。如果端点不显式声明 responses，契约里 503 就是空的，
    前端无法生成错误 DTO。
    """
    responses = schema["paths"]["/api/v1/health/db"]["get"]["responses"]
    assert "503" in responses

    def resolve(node: dict) -> dict:
        return schema["components"]["schemas"][node["$ref"].rsplit("/", 1)[-1]]

    envelope = resolve(responses["503"]["content"]["application/json"]["schema"])
    assert set(envelope["properties"]) == {"error", "meta"}
    assert set(envelope["required"]) == {"error", "meta"}

    error_body = resolve(envelope["properties"]["error"])
    assert set(error_body["properties"]) == {"code", "message", "details", "retryable"}
    assert set(error_body["required"]) == {"code", "message", "details", "retryable"}


def test_error_code_enum_is_exported_for_typescript_generation(schema: dict) -> None:
    """错误码必须作为 enum 进入契约，前端才能生成 TS 联合类型（协议 §13.3）。

    若 `ErrorBody.code` 是裸 string，前端就得手抄一份错误码清单 ——
    正是 §13.2 要禁止的"手工维护两套字段"。
    """
    error_body = schema["components"]["schemas"]["ErrorBody"]
    code_schema = error_body["properties"]["code"]
    assert "$ref" in code_schema
    enum_name = code_schema["$ref"].rsplit("/", 1)[-1]

    enum_schema = schema["components"]["schemas"][enum_name]
    assert set(enum_schema["enum"]) == PROTOCOL_ERROR_CODES


def test_internal_error_code_is_not_a_business_error_code(schema: dict) -> None:
    """INTERNAL_ERROR 不得混入业务错误码 enum。

    它代表代码缺陷，不是业务状态。若进了 `ErrorCode`，前端会以为可以按它写
    业务分支，且它会自动获得一个 HTTP 状态映射。
    """
    error_code_enum = schema["components"]["schemas"]["ErrorCode"]["enum"]
    assert "INTERNAL_ERROR" not in error_code_enum

    # 但它必须以单值 enum 出现在 500 响应里，前端才能生成类型。
    internal_body = schema["components"]["schemas"]["InternalErrorBody"]
    assert internal_body["properties"]["code"]["enum"] == ["INTERNAL_ERROR"]


def test_universal_error_responses_are_declared(schema: dict) -> None:
    """400/422/500 是所有端点都可能返回的框架级错误，必须在契约里声明。"""
    for path in ("/api/v1/health", "/api/v1/health/db"):
        responses = schema["paths"][path]["get"]["responses"]
        assert {"400", "422", "500"}.issubset(set(responses)), path


def test_identity_endpoints_declare_auth_errors(schema: dict) -> None:
    """认证端点必须声明 401/403。

    漏声明会让前端无法生成"会话过期"的处理分支 ——
    而那正是认证端点最需要客户端处理的错误。
    """
    auth_paths = {
        "/api/v1/auth/login": "get",
        "/api/v1/auth/callback": "get",
        "/api/v1/auth/logout": "post",
        "/api/v1/me": "get",
    }
    for path, method in auth_paths.items():
        responses = schema["paths"][path][method]["responses"]
        assert {"401", "403"}.issubset(set(responses)), path


def test_health_db_declares_503(schema: dict) -> None:
    responses = schema["paths"]["/api/v1/health/db"]["get"]["responses"]
    assert "503" in responses


def test_schema_has_no_schema_titles_by_default(schema: dict) -> None:
    """schema 内部的 title 被剥离（避免类名重命名产生噪声 diff）。"""

    def count_titles(node: object) -> int:
        if isinstance(node, dict):
            return sum(1 for key in node if key == "title") + sum(
                count_titles(value) for value in node.values()
            )
        if isinstance(node, list):
            return sum(count_titles(item) for item in node)
        return 0

    assert count_titles(schema.get("components", {})) == 0


def test_export_is_deterministic(export_module: ModuleType) -> None:
    """同代码连续两次导出必须字节一致 —— CI diff 可用的前提。"""
    first = export_module.dumps(export_module.build_schema())
    second = export_module.dumps(export_module.build_schema())

    assert first == second


def test_dumps_ends_with_newline(export_module: ModuleType, schema: dict) -> None:
    """POSIX 文本约定：避免 git 报 "no newline at end of file"。"""
    assert export_module.dumps(schema).endswith("\n")


def test_skeleton_check_rejects_missing_info_title(export_module: ModuleType) -> None:
    """自检必须能拦住"规范化删掉必需字段"这类事故。"""
    with pytest.raises(ValueError, match="info.title"):
        export_module._assert_valid_openapi_skeleton(
            {"openapi": "3.1.0", "info": {"version": "1.0"}, "paths": {}}
        )


def test_compare_detects_difference_and_reports_line(
    export_module: ModuleType, tmp_contract: Path, schema: dict
) -> None:
    """--check 的比较逻辑必须能发现差异并给出行号。"""
    target = tmp_contract
    target.write_text(export_module.dumps(schema), encoding="utf-8")

    consistent, _detail = export_module.compare(target, export_module.dumps(schema))
    assert consistent is True

    broken = export_module.dumps(schema).replace('"operationId"', '"operationIdx"', 1)
    consistent, detail = export_module.compare(target, broken)
    assert consistent is False
    assert "first difference at line" in detail


def test_compare_reports_missing_committed_file(
    export_module: ModuleType, tmp_contract: Path, schema: dict
) -> None:
    """已提交契约不存在时明确报错，而不是当成"一致"。"""
    missing = tmp_contract.parent / "definitely-not-here.json"

    consistent, detail = export_module.compare(missing, export_module.dumps(schema))

    assert consistent is False
    assert "not found" in detail


def test_main_check_mode_exit_codes(export_module: ModuleType, tmp_contract: Path) -> None:
    """端到端验证退出码：一致 -> 0，有差异 -> 1。"""
    target = tmp_contract

    # 首次写盘。
    assert export_module.main(["--output", str(target)]) == 0
    # 未改动 -> 一致。
    assert export_module.main(["--check", "--output", str(target)]) == 0
    # 改坏 -> 差异，退出码 1。
    text = target.read_text(encoding="utf-8").replace(
        '"openapi": "3.1.0"', '"openapi": "3.0.0"'
    )
    target.write_text(text, encoding="utf-8")
    assert export_module.main(["--check", "--output", str(target)]) == 1


def test_committed_contract_is_in_sync_with_code(export_module: ModuleType) -> None:
    """已提交的 contracts/openapi.json 必须与当前代码导出的结果一致。

    这条测试把"契约漂移"从 CI 阶段提前到 pytest 阶段：改了一个端点的响应模型
    却忘了重跑导出脚本，这里就会红。
    """
    if not _COMMITTED_CONTRACT.exists():
        pytest.fail(
            f"committed contract missing: {_COMMITTED_CONTRACT}. "
            "Run: python tools/contracts/export_openapi.py"
        )

    consistent, detail = export_module.compare(
        _COMMITTED_CONTRACT, export_module.dumps(export_module.build_schema())
    )

    assert consistent, (
        f"{detail}\nRun: python tools/contracts/export_openapi.py"
    )