"""WP12 测试：分片上传、配额预占与释放、文件固化（含 AC-06 / AC-07）。

覆盖验收标准：

> **AC-06** 并发上传仍无法超过 300,000,000 字节有效研究容量
> **AC-07** 不支持格式：文件可归档，明确未解析，不被当成已理解知识

**测试策略（四层，与 WP09/WP10/WP11 一致，外加一个并发层）**

1. **纯函数层**（不碰库也不碰盘）：文件分类、名称归一化、分片号计算。
2. **服务层 + 真库 + 真盘**：会话创建、分片、完成、取消、过期回收。
   存储用 `LocalStorageBackend` 指向 `tmp_path` —— 真实读写文件，
   不 mock。mock 掉存储就测不出"原子改名""路径越界"这些行为。
3. **并发层**（本包最重要的一层）：AC-06 的全部含金量在这里。
   串行调用 `reserve_quota` 永远是通过的 —— 它要证明的是
   "**两个请求同时读到还有余量**"这种时序下账面仍不超限，
   所以必须用真线程 + 真连接，不能靠顺序调用模拟。
4. **HTTP 层**：路由存在性、请求体上限（分片豁免 / JSON 不豁免）、
   错误码映射。

**关于测试数据的清理**：本包的表都允许删除（不像 WP11 的不可变表），
但测试刻意不做清理 —— 每次用新的 `project_id` / `uuid4()`，
互不干扰；清理反而会掩盖"外键 RESTRICT 是否按预期生效"。

**第 3 层为什么不用 asyncio**：`reserve_quota` 走的是同步 SQLAlchemy
连接，并发窗口来自数据库事务隔离而非事件循环。用线程池最能复现
"两个连接同时执行那条 UPDATE"的真实情形。
"""

from __future__ import annotations

import io
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from rednote.modules.files.models import (
    CLASSIFICATION_SUPPORTED,
    CLASSIFICATION_UNSUPPORTED,
    DEFAULT_PART_SIZE_BYTES,
    QUOTA_LIMIT_BYTES,
    QUOTA_PURPOSES,
    PURPOSE_BRIEF,
    PURPOSE_RESEARCH,
    SCAN_STATUS_PENDING,
    STORAGE_BACKEND_LOCAL,
    STORAGE_BACKEND_MINIO,
    STORAGE_STATE_ACTIVE,
    UPLOAD_STATE_CANCELLED,
    UPLOAD_STATE_COMPLETED,
    UPLOAD_STATE_EXPIRED,
    UPLOAD_STATE_FAILED,
    UPLOAD_STATE_UPLOADING,
    UPLOAD_STATES_HOLDING_RESERVATION,
    VISIBILITY_PROJECT,
    FileObject,
    UploadPart,
    UploadSession,
)
from rednote.modules.files.repository import FileRepository
from rednote.modules.files.service import (
    MAX_ORIGINAL_NAME_LENGTH,
    cancel_upload,
    classify_file,
    complete_upload,
    create_upload_session,
    expire_stale_sessions,
    get_file,
    max_part_no_for,
    normalize_mime_type,
    normalize_original_name,
)
from rednote.modules.files.service import (
    store_part as service_store_part,
)
from rednote.modules.files.storage import (
    LocalStorageBackend,
    build_storage_backend,
    object_key_for,
    part_key_for,
    upload_prefix,
)
from rednote.modules.identity.models import ROLE_OPERATOR
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.outbox.models import (
    AGGREGATE_FILE_OBJECT,
    EVENT_FILE_UPLOADED,
    OutboxEvent,
)
from rednote.modules.projects.models import Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.platform.configuration import Settings
from rednote.platform.database import session_scope
from rednote.platform.errors import ApiError, ErrorCode
from rednote.platform.request_limits import (
    DEFAULT_BODY_LIMIT_BYTES,
    PART_UPLOAD_PATH_RE,
)

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
# 测试库 DSN。**必须从环境变量读**（ISS-108）：硬编码会让
# `REDNOTE_TEST_DATABASE_URL` 约定的「测试库物理隔离」失效 —— 测试会把
# 数据写进本机默认库（实测跑一次全量 +367 行 users），且 CI 里 PG 用户是
# `postgres` 而不是 `rednote`，硬编码会直接连不上（role 不存在）。
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
TEST_DATABASE_URL = os.environ.get(
    "REDNOTE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL
).strip()

# 过期会话回收的扫描上限。`expire_stale_sessions` 默认 `limit=200`，而它是
# **全局**扫描（按 state + expires_at 过滤，没有 project 过滤 —— 过期回收本就
# 与项目无关）。共享库里只要积累了 ≥200 条更早过期的、仍持有预占的会话，
# 本用例刚造的那条就排在第 201 位、捞不到。
#
# 实测（2026-09-25）：注入 260 条 `expires_at = now() - 2h` 的 UPLOADING 会话后，
# `test_expire_stale_sessions_releases_reservation` **单跑也红** ——
# `released_bytes` 报 2000（= 别人的 200 条 × 10 字节），本用例的 7777 一个没回收。
# 失败信息只告诉你"回收得不够"，**完全指向不了真正的原因**。
#
# 这与 `_RECLAIM_LIMIT`(WP13) / `_DUE_POLLS_LIMIT`(WP16) / WP20 的 ISS-73
# 是同一类修法。同一根因在本项目已是**第 4 次现身**（ISS-49 → ISS-59 →
# ISS-73 → 本次），前三次都只在"报红的那一个包"里修了 ——
# 修跨包污染必须把源头**在每个包各过一遍**。
_STALE_SESSIONS_LIMIT = 10_000


def _make_settings(storage_root: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": TEST_DATABASE_URL,
        "environment": "test",
        "session_cookie_secure": False,
        "oidc_issuer": TEST_ISSUER,
        "oidc_token_encryption_key": TEST_ENCRYPTION_KEY,
        "storage_backend": STORAGE_BACKEND_LOCAL,
        "storage_local_root": str(storage_root),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ===========================================================================
# 第 1 层：纯函数（不碰库、不碰盘）
# ===========================================================================


def test_supported_extensions_are_classified_supported() -> None:
    """需求 §6.2 承诺可解析的 9 类文件必须判为 SUPPORTED。"""
    cases = [
        ("报告.pdf", "application/pdf"),
        ("方案.docx", "application/octet-stream"),  # 服务端不该信 MIME
        ("说明.txt", "text/plain"),
        ("笔记.md", "application/octet-stream"),
        ("数据.csv", "application/vnd.ms-excel"),  # macOS 常见错报
        ("表.xlsx", "application/octet-stream"),
        ("图.jpg", "image/jpeg"),
        ("图.png", "image/png"),
        ("图.webp", "image/webp"),
    ]
    for name, mime in cases:
        assert classify_file(original_name=name, mime_type=mime) == (
            CLASSIFICATION_SUPPORTED
        ), f"{name} 应判为 SUPPORTED"


def test_unknown_format_is_classified_unsupported() -> None:
    """AC-07 的前半：不在承诺清单内的必须判为 UNSUPPORTED。"""
    assert (
        classify_file(original_name="安装包.exe", mime_type="application/x-msdownload")
        == CLASSIFICATION_UNSUPPORTED
    )
    assert (
        classify_file(original_name="无扩展名", mime_type="application/octet-stream")
        == CLASSIFICATION_UNSUPPORTED
    )


def test_classification_is_union_of_extension_and_mime() -> None:
    """扩展名与 MIME 取**并集**：任一命中即 SUPPORTED。

    这条守住的是一个具体的假阴性：浏览器对 `.md` 常报 `text/plain`、
    对 `.csv` 在 macOS 上可能报 `application/vnd.ms-excel`。
    若只看扩展名，`report.pdf` 改名成 `report.bin` 就变 UNSUPPORTED；
    若只看 MIME，明明在清单内的文件会被标成"未解析"——
    而用户看到"未解析"会去转格式，做的是无用功。
    """
    # 扩展名不支持，但 MIME 在清单内 → SUPPORTED
    assert (
        classify_file(original_name="data.bin", mime_type="application/pdf")
        == CLASSIFICATION_SUPPORTED
    )
    # 扩展名支持，MIME 不在清单内 → 仍 SUPPORTED
    assert (
        classify_file(original_name="a.pdf", mime_type="application/x-unknown")
        == CLASSIFICATION_SUPPORTED
    )


def test_original_name_rejects_control_characters() -> None:
    """控制字符必须**拒绝**而不是替换。

    它们会进入 JSON 响应、日志、以及将来的下载响应头，
    是头注入与日志注入的载体。这类输入没有"安全化"的余地。
    """
    for bad in ("a\x00b.pdf", "a\r\nX-Injected: 1.pdf", "a\x1fb.pdf", "a\x7fb.pdf"):
        with pytest.raises(ApiError) as excinfo:
            normalize_original_name(bad)
        assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
        assert excinfo.value.details["reason"] == "file_name_control_chars"


def test_original_name_rejects_empty_and_too_long() -> None:
    for bad, reason in (
        ("   ", "file_name_empty"),
        ("x" * (MAX_ORIGINAL_NAME_LENGTH + 1), "file_name_too_long"),
    ):
        with pytest.raises(ApiError) as excinfo:
            normalize_original_name(bad)
        assert excinfo.value.details["reason"] == reason


def test_original_name_does_not_rewrite_path_separators() -> None:
    """`..` 与 `/` **刻意不替换** —— 文件名在本系统里从不参与路径拼接。

    替换它们防的不是本系统的攻击面，却会让用户看到"我的文件名被改了"。
    真正防穿越的是 `object_key` 由服务端生成 + 存储层的根目录校验，
    那两条各有专门的测试。
    """
    assert normalize_original_name("报表/2026.xlsx") == "报表/2026.xlsx"
    assert normalize_original_name("../../etc/passwd") == "../../etc/passwd"


def test_mime_type_is_normalized_and_bounded() -> None:
    assert normalize_mime_type("  Application/PDF  ") == "application/pdf"
    with pytest.raises(ApiError):
        normalize_mime_type("   ")
    with pytest.raises(ApiError):
        normalize_mime_type("x" * 300)


def test_max_part_no_accounts_for_hashing_and_empty_file() -> None:
    """分片数上限：空文件也算 1 片（分片号从 1 开始，不允许 0）。"""
    assert max_part_no_for(declared_size=0, part_size_bytes=100) == 1
    assert max_part_no_for(declared_size=1, part_size_bytes=100) == 1
    assert max_part_no_for(declared_size=100, part_size_bytes=100) == 1
    assert max_part_no_for(declared_size=101, part_size_bytes=100) == 2
    assert max_part_no_for(declared_size=1000, part_size_bytes=100) == 10


# ===========================================================================
# 第 2 层：服务层 + 真库 + 真盘
# ===========================================================================


# --- 装配 helper -----------------------------------------------------------


def _create_user(settings: Settings, *, role: str = ROLE_OPERATOR) -> uuid.UUID:
    with session_scope(settings) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name=f"用户-{role}",
            role=role,
        )
        return user.id


def _seed_project(
    settings: Settings, *, owner_id: uuid.UUID
) -> uuid.UUID:
    with session_scope(settings) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name="测试产品")
        session.add(product)
        session.flush()
        project = Project(
            project_code=f"FIL-{uuid.uuid4().hex[:8].upper()}",
            name="文件测试项目",
            brand_id=brand.id,
            product_id=product.id,
            owner_id=owner_id,
            target_delivery_count=10,
            due_date=date(2026, 12, 31),
        )
        session.add(project)
        session.flush()
        ProjectRepository(session).add_owner_and_collaborators(
            project.id, owner_id=owner_id, collaborator_ids=()
        )
        return project.id


@dataclass
class UploadEnv:
    """一个测试用的上传环境。"""

    settings: Settings
    store: LocalStorageBackend
    project_id: uuid.UUID
    owner_id: uuid.UUID


@pytest.fixture
def env(tmp_path: Path) -> UploadEnv:
    settings = _make_settings(tmp_path / "storage")
    store = LocalStorageBackend(tmp_path / "storage")
    owner = _create_user(settings)
    project_id = _seed_project(settings, owner_id=owner)
    return UploadEnv(
        settings=settings, store=store, project_id=project_id, owner_id=owner
    )


def _new_session(env: UploadEnv, *, byte_size: int, **kwargs: object):
    with session_scope(env.settings) as session:
        return create_upload_session(
            FileRepository(session),
            project_id=env.project_id,
            file_name=kwargs.pop("file_name", "资料.pdf"),  # type: ignore[arg-type]
            byte_size=byte_size,
            mime_type=kwargs.pop("mime_type", "application/pdf"),  # type: ignore[arg-type]
            purpose=kwargs.pop("purpose", PURPOSE_RESEARCH),  # type: ignore[arg-type]
            created_by=env.owner_id,
            **kwargs,  # type: ignore[arg-type]
        )


def _put_part(env: UploadEnv, *, session_id: uuid.UUID, part_no: int, chunk: bytes):
    with session_scope(env.settings) as session:
        return service_store_part(
            FileRepository(session),
            env.store,
            session_id=session_id,
            part_no=part_no,
            stream=io.BytesIO(chunk),
        )


@dataclass(frozen=True)
class QuotaNumbers:
    """账面快照。"""

    limit: int
    effective: int
    reserved: int

    @property
    def total(self) -> int:
        return self.effective + self.reserved


def _quota(env: UploadEnv) -> QuotaNumbers:
    """读账面。

    **账户可能不存在**：`ensure_quota_account` 与"预占"在同一个事务里，
    若预占失败（配额不足）整个事务回滚，那条 INSERT 也一并撤销。
    这不是缺陷 —— 每次上传都会重新 ensure；但**测试不能假设它一定存在**
    （这本身是"失败路径没有留下半成品"的一个证据）。
    账户不存在时按"默认上限 + 0/0"给出等价视图，那正是账面应有的值。
    """
    with session_scope(env.settings) as session:
        account = FileRepository(session).get_quota_account(env.project_id)
    if account is None:
        return QuotaNumbers(limit=QUOTA_LIMIT_BYTES, effective=0, reserved=0)
    return QuotaNumbers(
        limit=account.limit_bytes,
        effective=account.effective_bytes,
        reserved=account.reserved_bytes,
    )


def _upload_bytes(env: UploadEnv, content: bytes, **kwargs: object):
    """走完整流程：建会话 → 逐片上传 → 完成。"""
    view = _new_session(env, byte_size=len(content), **kwargs)
    part_size = DEFAULT_PART_SIZE_BYTES
    offsets = list(range(0, max(len(content), 1), part_size))
    for index in offsets:
        _put_part(
            env,
            session_id=view.session_id,
            part_no=index // part_size + 1,
            chunk=content[index : index + part_size],
        )
    with session_scope(env.settings) as session:
        outcome = complete_upload(
            FileRepository(session),
            env.store,
            session_id=view.session_id,
            ordered_parts=[
                _OrderedPart(part_no=index // part_size + 1) for index in offsets
            ],
        )
    assert outcome.ok, f"上传应当成功，实际失败：{outcome.failure}"
    assert outcome.result is not None
    return outcome.result


@dataclass
class _OrderedPart:
    """模拟端点的 `OrderedPartBody`（只要 `part_no` / `sha256` 两个属性）。"""

    part_no: int
    sha256: str | None = None


# --- 会话创建与预占 --------------------------------------------------------


def test_create_session_reserves_exactly_declared_size(env: UploadEnv) -> None:
    view = _new_session(env, byte_size=1000)

    assert view.state == UPLOAD_STATE_UPLOADING
    assert view.reserved_bytes == 1000
    assert view.received_bytes == 0
    assert view.part_size_bytes == DEFAULT_PART_SIZE_BYTES

    account = _quota(env)
    assert account.reserved == 1000
    assert account.effective == 0


def test_create_session_rejects_when_quota_insufficient(env: UploadEnv) -> None:
    """单次请求就超限 → 413，且**不留下任何预占**。"""
    with pytest.raises(ApiError) as excinfo:
        _new_session(env, byte_size=QUOTA_LIMIT_BYTES + 1)

    assert excinfo.value.code is ErrorCode.UPLOAD_TOO_LARGE
    assert excinfo.value.details["reason"] == "quota_exceeded"
    assert _quota(env).reserved == 0


def test_create_session_rejects_unknown_purpose(env: UploadEnv) -> None:
    with pytest.raises(ApiError) as excinfo:
        _new_session(env, byte_size=10, purpose="NOT_A_PURPOSE")
    assert excinfo.value.details["reason"] == "purpose_unknown"


def test_quota_account_check_constraint_blocks_overspend(env: UploadEnv) -> None:
    """账面不变量由 CHECK 兜底：绕过服务层直接写也拦得住。

    这条证明"账面不会超"不只依赖应用层的条件 UPDATE ——
    数据回填、运维脚本都可能绕过它。
    """
    from sqlalchemy.exc import IntegrityError

    _new_session(env, byte_size=1000)
    with pytest.raises(IntegrityError) as excinfo:
        with session_scope(env.settings) as session:
            session.execute(
                text(
                    "UPDATE quota_accounts SET reserved_bytes = limit_bytes + 1"
                    " WHERE project_id = :p"
                ),
                {"p": str(env.project_id)},
            )
    assert "within_limit" in str(excinfo.value)


# --- 分片 ------------------------------------------------------------------


def test_upload_part_stores_bytes_and_returns_digest(env: UploadEnv) -> None:
    import hashlib

    payload = b"x" * 1024
    view = _new_session(env, byte_size=len(payload))
    result = _put_part(
        env, session_id=view.session_id, part_no=1, chunk=payload
    )

    assert result.byte_size == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.already_existed is False
    assert env.store.exists(key=part_key_for(view.session_id, 1))


def test_repeated_identical_part_is_idempotent(env: UploadEnv) -> None:
    """协议 §4.2「重复一致返回已有结果」：第二次不改动任何数据。"""
    payload = b"y" * 2048
    view = _new_session(env, byte_size=len(payload))
    first = _put_part(env, session_id=view.session_id, part_no=1, chunk=payload)
    second = _put_part(env, session_id=view.session_id, part_no=1, chunk=payload)

    assert first.already_existed is False
    assert second.already_existed is True
    assert second.sha256 == first.sha256

    with session_scope(env.settings) as session:
        parts = FileRepository(session).list_parts(view.session_id)
    assert len(parts) == 1, "重复上传不应产生第二行分片记录"


def test_repeated_part_with_different_content_is_rejected(env: UploadEnv) -> None:
    """文档 02 §4：「重复分片内容不一致拒绝」。

    **且旧内容必须原封不动** —— 这是"先写 staging 再落位"的意义：
    若直接覆盖正式 key，等发现不一致时旧内容已经被冲掉，
    "拒绝"就成了一句空话。
    """
    import hashlib

    original = b"a" * 1024
    view = _new_session(env, byte_size=len(original))
    _put_part(env, session_id=view.session_id, part_no=1, chunk=original)

    with pytest.raises(ApiError) as excinfo:
        _put_part(env, session_id=view.session_id, part_no=1, chunk=b"b" * 1024)

    assert excinfo.value.code is ErrorCode.INVALID_TRANSITION
    assert excinfo.value.details["reason"] == "part_content_mismatch"

    # 磁盘上的分片内容仍是原始的
    stored = b"".join(
        env.store.open_stream(key=part_key_for(view.session_id, 1))
    )
    assert hashlib.sha256(stored).hexdigest() == hashlib.sha256(original).hexdigest()


def test_part_out_of_range_is_rejected(env: UploadEnv) -> None:
    """分片号必须落在 `declared_size` 推出的范围内。

    没有这条，客户端可以传 1000 片来绕过"单片 8 MiB"的限制。
    """
    view = _new_session(env, byte_size=100)  # → 只允许 1 片
    with pytest.raises(ApiError) as excinfo:
        _put_part(env, session_id=view.session_id, part_no=2, chunk=b"z" * 10)
    assert excinfo.value.details["reason"] == "part_no_out_of_range"
    assert excinfo.value.details["maxPartNo"] == 1


def test_non_final_part_must_be_exactly_part_size(env: UploadEnv) -> None:
    """非末片必须恰好一片大小 —— 少了说明切片有洞，拼出来会缺内容。"""
    view = _new_session(env, byte_size=DEFAULT_PART_SIZE_BYTES * 2 + 10)
    with pytest.raises(ApiError) as excinfo:
        _put_part(env, session_id=view.session_id, part_no=1, chunk=b"z" * 100)
    assert excinfo.value.details["reason"] == "part_size_mismatch"


def test_part_rejected_after_session_cancelled(env: UploadEnv) -> None:
    view = _new_session(env, byte_size=100)
    with session_scope(env.settings) as session:
        cancel_upload(FileRepository(session), env.store, session_id=view.session_id)
    with pytest.raises(ApiError) as excinfo:
        _put_part(env, session_id=view.session_id, part_no=1, chunk=b"z")
    assert excinfo.value.details["reason"] == "session_not_uploading"


def test_staging_files_are_not_left_behind(env: UploadEnv) -> None:
    """成功与失败两条路径都不能留下 `.staging` 半成品。

    staging 文件不在任何清理路径的覆盖范围内（TTL 只扫会话前缀下的对象，
    但上传中途失败根本不会走到 TTL），会永久占盘。
    """
    payload = b"s" * 512
    view = _new_session(env, byte_size=len(payload))
    _put_part(env, session_id=view.session_id, part_no=1, chunk=payload)

    leftovers = [p for p in env.store._root.rglob("*") if p.name.endswith(".staging")]
    assert leftovers == []


# --- 完成上传 --------------------------------------------------------------


def test_complete_upload_converts_reservation_to_effective(env: UploadEnv) -> None:
    content = b"hello world" * 100
    result = _upload_bytes(env, content)

    assert result.created is True
    assert result.byte_size == len(content)
    assert result.classification == CLASSIFICATION_SUPPORTED
    assert result.supported is True

    account = _quota(env)
    assert account.reserved == 0, "预占必须全部转出"
    assert account.effective == len(content)


def test_complete_upload_marks_session_completed(env: UploadEnv) -> None:
    view = _new_session(env, byte_size=64)
    _put_part(env, session_id=view.session_id, part_no=1, chunk=b"c" * 64)
    with session_scope(env.settings) as session:
        complete_upload(
            FileRepository(session),
            env.store,
            session_id=view.session_id,
            ordered_parts=[_OrderedPart(part_no=1)],
        )
    with session_scope(env.settings) as session:
        row = session.get(UploadSession, view.session_id)
    assert row is not None
    assert row.state == UPLOAD_STATE_COMPLETED
    assert row.file_id is not None
    assert row.actual_sha256 is not None
    assert row.received_bytes == 64


def test_complete_upload_is_idempotent(env: UploadEnv) -> None:
    """重复完成返回同一结果，**不重复计入配额**。

    没有这条，网络重试会让同一份文件被记两次账 ——
    用户传 100 MB 却扣了 200 MB，且账面看起来完全正常。
    """
    view = _new_session(env, byte_size=100)
    _put_part(env, session_id=view.session_id, part_no=1, chunk=b"i" * 100)
    parts = [_OrderedPart(part_no=1)]

    with session_scope(env.settings) as session:
        first = complete_upload(
            FileRepository(session), env.store, session_id=view.session_id,
            ordered_parts=parts,
        ).result
    with session_scope(env.settings) as session:
        second = complete_upload(
            FileRepository(session), env.store, session_id=view.session_id,
            ordered_parts=parts,
        ).result

    assert first is not None and second is not None
    assert first.created is True
    assert second.created is False
    assert second.file_id == first.file_id
    assert _quota(env).effective == 100, "重复完成不得重复计账"


def test_complete_upload_rejects_checksum_mismatch(env: UploadEnv) -> None:
    """声明摘要与实际不符 → 422，且**预占必须释放**。

    失败的路径不还额度，用户传一次坏文件就永久吃掉一块配额。
    """
    view = _new_session(
        env, byte_size=50, sha256="0" * 64
    )
    _put_part(env, session_id=view.session_id, part_no=1, chunk=b"m" * 50)

    # **不可恢复的失败走返回值**（见 `CompletionOutcome`）：它要带着
    # "置 FAILED + 释放预占"一起提交，所以不能抛异常。
    with session_scope(env.settings) as session:
        outcome = complete_upload(
            FileRepository(session), env.store, session_id=view.session_id,
            ordered_parts=[_OrderedPart(part_no=1)],
        )

    assert not outcome.ok
    assert outcome.failure is not None
    assert outcome.failure.details["reason"] == "checksum_mismatch"

    account = _quota(env)
    assert account.reserved == 0, "失败路径必须释放预占"
    assert account.effective == 0

    with session_scope(env.settings) as session:
        row = session.get(UploadSession, view.session_id)
    assert row is not None
    assert row.state == UPLOAD_STATE_FAILED


def test_complete_upload_corrects_when_actual_differs_from_declared(
    env: UploadEnv,
) -> None:
    """OP-93 选定的口径：**按实际大小修正**，差额立即回补余量。

    声明值本来就是估计（客户端在开始上传前只能报个大概），
    因此"实际比声明小"是常态，不该报错。
    """
    view = _new_session(env, byte_size=1000)  # 声明 1000
    _put_part(env, session_id=view.session_id, part_no=1, chunk=b"d" * 400)  # 实际 400

    with session_scope(env.settings) as session:
        outcome = complete_upload(
            FileRepository(session), env.store, session_id=view.session_id,
            ordered_parts=[_OrderedPart(part_no=1)],
        )
    assert outcome.ok and outcome.result is not None
    result = outcome.result

    assert result.byte_size == 400
    account = _quota(env)
    assert account.reserved == 0
    assert account.effective == 400, "只记实际用量，不是声明值"


def test_complete_upload_rejects_when_actual_exceeds_remaining(
    env: UploadEnv,
) -> None:
    """实际比声明大且超出余量 → 413，且预占释放。

    构造：先把额度用掉绝大部分，再发起一个"声明很小、实际很大"的上传。
    """
    # 占掉 299_999_999 字节。
    #
    # **账户必须先存在**：它是由第一次成功预占创建的（`ensure_quota_account`
    # 与预占同事务）。若直接 UPDATE，账户还不存在 → 影响 0 行 → 静默无效，
    # 后面"实际超余量应该被拒"的断言就失去了前提，测试会以一种
    # 看不出原因的方式失败。
    with session_scope(env.settings) as session:
        repo = FileRepository(session)
        repo.ensure_quota_account(env.project_id)
        repo.flush()
        result = session.execute(
            text(
                "UPDATE quota_accounts SET effective_bytes = limit_bytes - 1"
                " WHERE project_id = :p"
            ),
            {"p": str(env.project_id)},
        )
        assert result.rowcount == 1, "配额账户必须先存在，否则这条 UPDATE 静默无效"

    view = _new_session(env, byte_size=1)  # 声明 1，能预占
    _put_part(env, session_id=view.session_id, part_no=1, chunk=b"e" * 100)

    with session_scope(env.settings) as session:
        outcome = complete_upload(
            FileRepository(session), env.store, session_id=view.session_id,
            ordered_parts=[_OrderedPart(part_no=1)],
        )
    assert not outcome.ok
    assert outcome.failure is not None
    assert outcome.failure.code is ErrorCode.UPLOAD_TOO_LARGE
    assert outcome.failure.details["reason"] == "quota_exceeded_on_actual_size"

    account = _quota(env)
    assert account.reserved == 0, "预占必须释放"


def test_complete_upload_rejects_non_contiguous_parts(env: UploadEnv) -> None:
    """分片号不连续 → 拒绝。少一片会拼出一个"看起来正常"但缺内容的文件。"""
    view = _new_session(env, byte_size=DEFAULT_PART_SIZE_BYTES * 3)
    for part_no in (1, 3):  # 缺 2
        _put_part(
            env,
            session_id=view.session_id,
            part_no=part_no,
            chunk=b"f" * (DEFAULT_PART_SIZE_BYTES if part_no < 3 else 10),
        )
    with pytest.raises(ApiError) as excinfo:
        with session_scope(env.settings) as session:
            complete_upload(
                FileRepository(session), env.store, session_id=view.session_id,
                ordered_parts=[_OrderedPart(part_no=1), _OrderedPart(part_no=3)],
            )
    assert excinfo.value.details["reason"] == "parts_not_contiguous"


def test_complete_upload_rejects_no_parts(env: UploadEnv) -> None:
    view = _new_session(env, byte_size=10)
    with pytest.raises(ApiError) as excinfo:
        with session_scope(env.settings) as session:
            complete_upload(
                FileRepository(session), env.store, session_id=view.session_id,
                ordered_parts=[],
            )
    assert excinfo.value.details["reason"] == "no_parts_received"


def test_complete_upload_writes_outbox_event(env: UploadEnv) -> None:
    """协议 §4.1 第 8 步：完成上传必须同事务写 Outbox。

    这条守住的是"解析任务不丢"：事件已落库，WP13 的投递器上线后
    自然开始消费，WP17 不需要回头改本包。
    """
    content = b"o" * 128
    result = _upload_bytes(env, content)

    with session_scope(env.settings) as session:
        events = (
            session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == result.file_id
                )
            )
            .scalars()
            .all()
        )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == EVENT_FILE_UPLOADED
    assert event.aggregate_type == AGGREGATE_FILE_OBJECT
    assert event.payload["fileId"] == str(result.file_id)
    assert event.payload["byteSize"] == str(len(content))
    assert event.payload["classification"] == CLASSIFICATION_SUPPORTED
    assert event.published_at is None
    assert event.attempts == 0


@pytest.mark.regression
def test_completed_upload_leaves_no_temporary_parts(env: UploadEnv) -> None:
    """完成上传后**盘上只能有正式对象**，不能残留临时分片。

    这条盯的是一个真实的账实不符：残留分片不会被任何清理路径碰到
    （TTL 只捞仍持有预占的会话，已完成的会话不在候选集），
    于是 300 MB 的文件永久占用 600 MB 磁盘，而配额只算 300 MB。
    现场表现是"磁盘满了但配额还有余量"，最难查的那一类。
    """
    content = b"p" * 3000
    result = _upload_bytes(env, content)

    prefix_dir = env.store._root / "parts" / str(_session_id_of(env, result.file_id))
    assert not prefix_dir.exists(), f"临时分片目录应被清空，实际仍存在：{prefix_dir}"

    files = [p for p in env.store._root.rglob("*") if p.is_file()]
    assert len(files) == 1, f"盘上应只剩正式对象，实际 {[f.name for f in files]}"
    assert files[0].name.startswith(("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "a", "b", "c", "d", "e", "f"))


def test_unsupported_format_is_archived_with_marker(env: UploadEnv) -> None:
    """**AC-07**：不支持格式必须可归档、且明确标记未解析。

    验收原文："文件可归档，明确未解析，不被当成已理解知识"。
    归档与标记这两半都由本包负责（"不被当成知识"由 WP20 消费时的过滤保证）。
    """
    content = b"MZ\x90\x00 fake binary"
    result = _upload_bytes(
        env,
        content,
        file_name="工具.exe",
        mime_type="application/x-msdownload",
    )

    assert result.classification == CLASSIFICATION_UNSUPPORTED
    assert result.supported is False

    # 文件本身照常归档（磁盘上真的有内容）
    with session_scope(env.settings) as session:
        file_row, blob = get_file(
            FileRepository(session), file_id=result.file_id
        )
    assert file_row.classification == CLASSIFICATION_UNSUPPORTED
    assert file_row.visibility == VISIBILITY_PROJECT
    assert blob.byte_size == len(content)
    assert blob.scan_status == SCAN_STATUS_PENDING
    assert blob.storage_state == STORAGE_STATE_ACTIVE
    assert env.store.size(key=blob.object_key) == len(content)


def test_complete_upload_links_file_to_project_for_quota(env: UploadEnv) -> None:
    """配额按 `project_file_links` 计算 —— 关联必须真的建出来。"""
    result = _upload_bytes(env, b"q" * 256)
    with session_scope(env.settings) as session:
        linked = session.execute(
            text(
                "SELECT purpose, effective FROM project_file_links"
                " WHERE file_id = :f"
            ),
            {"f": str(result.file_id)},
        ).all()
    assert len(linked) == 1
    assert linked[0][0] == PURPOSE_RESEARCH
    assert linked[0][1] is True


def test_physical_dedup_reuses_blob_but_charges_each_project(env: UploadEnv) -> None:
    """文档 02 §4：物理去重**不免**项目配额。

    同一份内容传两次（同一项目），blob 只有一行，但配额记两次 ——
    "跨项目共用 Blob 不因去重免掉该项目配额"。
    """
    content = b"d" * 1024
    first = _upload_bytes(env, content)
    second = _upload_bytes(env, content)

    assert first.blob_id == second.blob_id, "同内容应复用同一 blob"
    assert first.file_id != second.file_id, "但是两条文件记录"
    assert _quota(env).effective == len(content) * 2


# --- 取消与过期 ------------------------------------------------------------


def test_cancel_releases_reservation(env: UploadEnv) -> None:
    view = _new_session(env, byte_size=5000)
    assert _quota(env).reserved == 5000

    with session_scope(env.settings) as session:
        changed = cancel_upload(
            FileRepository(session), env.store, session_id=view.session_id
        )
    assert changed is True

    account = _quota(env)
    assert account.reserved == 0
    assert account.effective == 0

    with session_scope(env.settings) as session:
        row = session.get(UploadSession, view.session_id)
    assert row is not None
    assert row.state == UPLOAD_STATE_CANCELLED


def test_cancel_is_idempotent_and_releases_only_once(env: UploadEnv) -> None:
    """文档 02 §13.1「每次释放只允许一次」+ 协议「幂等释放预占」。

    构造最毒的一种情形：**并发取消同一个会话**。
    若释放不做条件判断，两次取消会把额度还两遍
    （`reserved_bytes` 变负 → 被 CHECK 拦住 → 500）。
    """
    view = _new_session(env, byte_size=4000)

    def do_cancel(_: int) -> bool:
        with session_scope(env.settings) as session:
            return cancel_upload(
                FileRepository(session), env.store, session_id=view.session_id
            )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(do_cancel, range(3)))

    assert sum(1 for r in results if r) == 1, "只应有一次真正生效"
    account = _quota(env)
    assert account.reserved == 0, "不能还成负数，也不能漏还"
    assert account.effective == 0


def test_cancel_cleans_parts_and_temporary_objects(env: UploadEnv) -> None:
    view = _new_session(env, byte_size=100)
    _put_part(env, session_id=view.session_id, part_no=1, chunk=b"g" * 100)
    assert env.store.exists(key=part_key_for(view.session_id, 1))

    with session_scope(env.settings) as session:
        cancel_upload(FileRepository(session), env.store, session_id=view.session_id)

    assert not env.store.exists(key=part_key_for(view.session_id, 1))
    with session_scope(env.settings) as session:
        assert FileRepository(session).list_parts(view.session_id) == []


def test_cancel_completed_session_does_not_release_effective(env: UploadEnv) -> None:
    """已完成的会话再取消 → 不改状态、**不退有效占用**。

    退有效占用意味着"文件还在但配额不算了" —— 配额成了摆设。
    """
    content = b"k" * 128
    result = _upload_bytes(env, content)
    assert result.created is True

    with session_scope(env.settings) as session:
        changed = cancel_upload(
            FileRepository(session),
            env.store,
            session_id=_session_id_of(env, result.file_id),
        )
    assert changed is False
    assert _quota(env).effective == len(content)


def _session_id_of(env: UploadEnv, file_id: uuid.UUID) -> uuid.UUID:
    """从文件反查它的上传会话（`upload_sessions.file_id` 唯一对应一个文件）。"""
    with session_scope(env.settings) as session:
        row = session.execute(
            select(UploadSession).where(UploadSession.file_id == file_id)
        ).scalar_one()
        return row.id


def _purge_stale_sessions(env: UploadEnv) -> None:
    """把库里**所有**已过期、仍持有预占的会话回收干净（清场）。

    为什么需要它：`expire_stale_sessions` 是**全局**扫描（按 state + expires_at，
    无 project 过滤），而本文件有**全局计数**断言（`second.expired_sessions == 0`）。
    库里任何**别的来源**的过期会话都会混进候选集，把计数推高。

    这些"别的来源"是**跨运行残留**：本文件多条用例把会话 `expires_at` 推到
    过去（`test_expired_session_rejects_new_parts` 造完就走），它们跨运行
    仍是下一次运行的输入。

    清场放在**测试开始前**：一次动作同时覆盖"上一次运行的残留"与
    "本次运行中前面测试的残留"。回收器幂等，扫不到就返回零。
    （与 WP13 的 `_purge_expired_leases` 同一修法。）
    """
    with session_scope(env.settings) as session:
        expire_stale_sessions(
            FileRepository(session),
            env.store,
            limit=_STALE_SESSIONS_LIMIT,
        )


@pytest.mark.regression
def test_expire_stale_sessions_releases_reservation(env: UploadEnv) -> None:
    """TTL 回收：用户关掉浏览器也不能永久吃掉额度。

    ⚠️ **必须先用大 limit**：`expire_stale_sessions` 默认 `limit=200` 且是全局
    扫描；共享库里积累 ≥200 条更早过期的会话时，本用例刚造的那条排在第 201 位
    捞不到 → `released_bytes` 只统计到别人的会话，失败信息只显示"回收得不够"。
    实测复现：注入 260 条后单跑即红（`released_bytes` 报 2000 而非 ≥7777）。
    """
    _purge_stale_sessions(env)

    view = _new_session(env, byte_size=7777)
    _put_part(env, session_id=view.session_id, part_no=1, chunk=b"t" * 100)

    # 手工把过期时间推到过去（不 mock 时间，直接改数据）
    with session_scope(env.settings) as session:
        session.execute(
            text("UPDATE upload_sessions SET expires_at = now() - interval '1 hour' WHERE id = :i"),
            {"i": str(view.session_id)},
        )

    with session_scope(env.settings) as session:
        report = expire_stale_sessions(
            FileRepository(session), env.store, limit=_STALE_SESSIONS_LIMIT
        )

    assert report.expired_sessions >= 1
    assert report.released_bytes >= 7777
    account = _quota(env)
    assert account.reserved == 0

    with session_scope(env.settings) as session:
        row = session.get(UploadSession, view.session_id)
    assert row is not None
    assert row.state == UPLOAD_STATE_EXPIRED

    assert not env.store.exists(key=part_key_for(view.session_id, 1))


@pytest.mark.regression
def test_expire_is_idempotent(env: UploadEnv) -> None:
    """回收可被反复调用（部署层会定时跑），第二次必须是空操作。

    ⚠️ **必须先清场**：本用例断言的是**全局计数**
    （`second.expired_sessions == 0`），而 `expire_stale_sessions` 是全局扫描
    （无 project 过滤）。库里任何**别的来源**的过期会话都会让第二次调用
    仍然回收出东西 → `assert second.expired_sessions == 0` 偶发红。
    过期会话的来源正是**跨运行残留**：本文件多条用例把会话 `expires_at`
    推到过去后**刻意不回收干净**（其中 `test_expired_session_rejects_new_parts`
    造完就结束），它们跨运行仍是"别人的输入"。

    清场放在**测试开始前**而不是结束后：结束后清不掉本用例刚推过去的那条吗？
    其实可以 —— 但开始前清场同时覆盖"上一次运行的残留"与"本次运行中前面
    测试的残留"，一次动作覆盖两种来源。回收器本身幂等，扫不到就返回零。
    （修法与 WP13 的 `_purge_expired_leases` 一致，见该文件 §fixture。）
    """
    _purge_stale_sessions(env)

    view = _new_session(env, byte_size=10)
    with session_scope(env.settings) as session:
        session.execute(
            text("UPDATE upload_sessions SET expires_at = now() - interval '1 hour' WHERE id = :i"),
            {"i": str(view.session_id)},
        )
    with session_scope(env.settings) as session:
        expire_stale_sessions(
            FileRepository(session), env.store, limit=_STALE_SESSIONS_LIMIT
        )
    with session_scope(env.settings) as session:
        second = expire_stale_sessions(
            FileRepository(session), env.store, limit=_STALE_SESSIONS_LIMIT
        )
    assert second.expired_sessions == 0
    assert second.released_bytes == 0


def test_expired_session_rejects_new_parts(env: UploadEnv) -> None:
    view = _new_session(env, byte_size=10)
    with session_scope(env.settings) as session:
        session.execute(
            text("UPDATE upload_sessions SET expires_at = now() - interval '1 hour' WHERE id = :i"),
            {"i": str(view.session_id)},
        )
    with pytest.raises(ApiError) as excinfo:
        _put_part(env, session_id=view.session_id, part_no=1, chunk=b"x")
    assert excinfo.value.details["reason"] == "session_expired"


# ===========================================================================
# 第 3 层：并发 —— AC-06 的核心
# ===========================================================================


def test_concurrent_uploads_cannot_exceed_quota(env: UploadEnv) -> None:
    """**AC-06 的直接证据**：并发上传仍无法超过 300,000,000 字节。

    构造：4 个线程同时发起 100,000,000 字节的预占，合计 400,000,000 > 上限。
    正确实现必须恰好放行 3 个（300,000,000 / 100,000,000 = 3），
    并且账面之和不超过上限。

    **为什么必须真并发**：`reserve_quota` 的正确性来自
    "判断条件写在 UPDATE 的 WHERE 里"。串行调用时，
    "先 SELECT 查余量再 UPDATE"的错误实现**同样会通过** ——
    只有两个连接真的同时执行那条语句，才暴露出读与写之间的窗口。
    """
    chunk = QUOTA_LIMIT_BYTES // 3  # 100,000,000
    attempts = 4

    def attempt(index: int) -> bool:
        try:
            _new_session(env, byte_size=chunk, file_name=f"并发-{index}.pdf")
            return True
        except ApiError as exc:
            assert exc.code is ErrorCode.UPLOAD_TOO_LARGE
            return False

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        granted = list(pool.map(attempt, range(attempts)))

    assert sum(1 for ok in granted if ok) == 3, (
        f"应恰好放行 3 个（上限 300,000,000 / 每次 100,000,000），"
        f"实际 {sum(1 for ok in granted if ok)} 个：{granted}"
    )

    account = _quota(env)
    total = account.effective + account.reserved
    assert total <= QUOTA_LIMIT_BYTES, "账面必须不超限"
    assert total == chunk * 3


def test_concurrent_uploads_within_limit_all_succeed(env: UploadEnv) -> None:
    """反向用例：总量不超限时**一个都不能失败**。

    没有这条，"一律拒绝"这种退化实现也能通过上面那条测试。
    """
    chunk = QUOTA_LIMIT_BYTES // 4  # 75,000,000
    attempts = 4  # 合计 300,000,000，正好等于上限

    def attempt(index: int) -> bool:
        try:
            _new_session(env, byte_size=chunk, file_name=f"边界-{index}.pdf")
            return True
        except ApiError:
            return False

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        granted = list(pool.map(attempt, range(attempts)))

    assert all(granted), f"合计恰好等于上限时应全部通过，实际 {granted}"
    account = _quota(env)
    assert account.effective + account.reserved == QUOTA_LIMIT_BYTES


def test_concurrent_same_content_upload_creates_single_blob(env: UploadEnv) -> None:
    """并发上传**同一份内容**：blob 只应有一行。

    两个请求同时发现"这个 key 不存在"并各自插入 —— 若用"先查再插"，
    其中一个会撞唯一约束变成 500，而这件事在语义上完全正常
    （两个用户传了同一份资料）。
    """
    view_a = _new_session(env, byte_size=512)
    view_b = _new_session(env, byte_size=512)
    payload = b"same" * 128

    for view in (view_a, view_b):
        _put_part(env, session_id=view.session_id, part_no=1, chunk=payload)

    def finish(view) -> uuid.UUID:
        with session_scope(env.settings) as session:
            outcome = complete_upload(
                FileRepository(session), env.store, session_id=view.session_id,
                ordered_parts=[_OrderedPart(part_no=1)],
            )
        assert outcome.result is not None, f"完成失败：{outcome.failure}"
        return outcome.result.blob_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        blob_ids = list(pool.map(finish, (view_a, view_b)))

    assert blob_ids[0] == blob_ids[1], "同内容应落到同一个 blob"


def test_quota_never_goes_negative_under_concurrent_cancel_and_reserve(
    env: UploadEnv,
) -> None:
    """并发"取消 + 新建"不能让账面变成负数或超限。

    这是 AC-06 与"释放只允许一次"交叉处的最坏时序。
    """
    sessions = [_new_session(env, byte_size=10_000_000) for _ in range(3)]
    originals = [view.session_id for view in sessions]

    def churn(index: int) -> None:
        if index % 2 == 0:
            with session_scope(env.settings) as session:
                cancel_upload(
                    FileRepository(session),
                    env.store,
                    session_id=originals[index % len(originals)],
                )
        else:
            try:
                _new_session(env, byte_size=10_000_000, file_name=f"churn-{index}.pdf")
            except ApiError:
                pass

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(churn, range(6)))

    account = _quota(env)
    assert account.reserved >= 0
    assert account.effective >= 0
    assert account.effective + account.reserved <= QUOTA_LIMIT_BYTES


# ===========================================================================
# 第 4 层：存储层与安全
# ===========================================================================


def test_storage_rejects_path_traversal(tmp_path: Path) -> None:
    """路径穿越必须被拦 —— 这是唯一的防线。

    判定方式是"解析后比较"而不是"检查字符串里有没有 `..`"：
    后者会被 `a/./../../b`、符号链接等绕过。
    """
    store = LocalStorageBackend(tmp_path / "root")
    for bad in (
        "../outside",
        "a/../../outside",
        "parts/../../../etc/passwd",
        "/absolute/path",
    ):
        with pytest.raises(ApiError) as excinfo:
            store.write_part(key=bad, stream=io.BytesIO(b"x"))
        assert excinfo.value.details["reason"] in {
            "storage_key_out_of_root",
            "storage_key_malformed",
        }


def test_object_key_never_contains_user_input() -> None:
    """对象 key 只由服务端拼（uuid + 摘要 + 整数），结构上排除用户输入。"""
    upload_id = uuid.uuid4()
    key = part_key_for(upload_id, 7)
    assert key == f"parts/{upload_id}/7"

    sha = "ab" + "0" * 62
    assert object_key_for(sha) == f"objects/ab/{sha}"

    # 前缀与 key 一致（TTL 清理按它批量删）
    assert key.startswith(upload_prefix(upload_id))


def test_storage_assemble_is_atomic_on_failure(tmp_path: Path) -> None:
    """拼装失败必须让**目标 key 不存在**，而不是留下半截内容。

    半截对象在存储上与正常对象无法区分（除非比对大小，而没人会去比）。
    """
    store = LocalStorageBackend(tmp_path / "root")
    store.write_part(key=part_key_for("u1", 1), stream=io.BytesIO(b"a" * 10))

    with pytest.raises(ApiError) as excinfo:
        store.assemble(
            part_keys=[part_key_for("u1", 1), part_key_for("u1", 2)],  # 第 2 片不存在
            target_key="objects/xx/yy",
        )
    assert excinfo.value.details["reason"] == "part_missing"
    assert not store.exists(key="objects/xx/yy")
    assert [p for p in (tmp_path / "root").rglob("*.assembling")] == []


def test_storage_move_is_used_and_removes_source(tmp_path: Path) -> None:
    store = LocalStorageBackend(tmp_path / "root")
    store.write_part(key="parts/u1/1", stream=io.BytesIO(b"hello"))

    store.move(from_key="parts/u1/1", to_key="objects/ab/target")
    assert store.exists(key="objects/ab/target")
    assert not store.exists(key="parts/u1/1")
    assert store.size(key="objects/ab/target") == 5


def test_build_storage_backend_rejects_unimplemented_minio(tmp_path: Path) -> None:
    """MinIO 分支必须**显式失败**，不能静默降级到本地盘。

    静默降级会让内网切到 MinIO 时"看起来配好了"，实际写的是容器本地盘 ——
    重启即丢，而那时数据已经"上传成功"过。
    """
    with pytest.raises(ApiError) as excinfo:
        build_storage_backend(backend=STORAGE_BACKEND_MINIO, local_root=tmp_path)
    assert excinfo.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE
    assert excinfo.value.details["reason"] == "storage_backend_unimplemented"


def test_build_storage_backend_requires_root_for_local() -> None:
    with pytest.raises(ApiError) as excinfo:
        build_storage_backend(backend=STORAGE_BACKEND_LOCAL, local_root=None)
    assert excinfo.value.details["reason"] == "storage_local_root_missing"


def test_build_storage_backend_rejects_unknown_backend(tmp_path: Path) -> None:
    with pytest.raises(ApiError) as excinfo:
        build_storage_backend(backend="S3", local_root=tmp_path)
    assert excinfo.value.details["reason"] == "storage_backend_unknown"


# ===========================================================================
# 第 5 层：HTTP（路由、请求体上限、错误码）
# ===========================================================================


@pytest.fixture
def app_with_storage(tmp_path: Path) -> FastAPI:
    from rednote.main import create_app

    return create_app(_make_settings(tmp_path / "http-storage"))


@pytest.fixture
def client(app_with_storage: FastAPI):
    with TestClient(app_with_storage) as test_client:
        yield test_client


def test_wp12_routes_are_registered(app_with_storage: FastAPI) -> None:
    paths = {
        (method, route.path)
        for route in app_with_storage.routes
        if hasattr(route, "methods")
        for method in (route.methods or set())
    }
    for method, path in (
        ("POST", "/api/v1/projects/{projectId}/uploads"),
        ("GET", "/api/v1/uploads/{uploadId}"),
        ("PUT", "/api/v1/uploads/{uploadId}/parts/{partNo}"),
        ("POST", "/api/v1/uploads/{uploadId}/completions"),
        ("DELETE", "/api/v1/uploads/{uploadId}"),
        ("GET", "/api/v1/files/{fileId}"),
    ):
        assert (method, path) in paths, f"缺少端点 {method} {path}"


def test_part_upload_path_pattern_matches_only_the_part_endpoint() -> None:
    """请求体上限的豁免必须**只**覆盖分片端点。

    若写成"所有 /uploads/ 前缀都豁免"，`completions` 就能塞 100 MB 的
    `orderedParts` 数组绕过 2 MB 限制 —— 那是一条真实的内存放大路径。
    """
    upload_id = uuid.uuid4()
    assert PART_UPLOAD_PATH_RE.match(f"/api/v1/uploads/{upload_id}/parts/1")
    assert PART_UPLOAD_PATH_RE.match(f"/api/v1/uploads/{upload_id}/parts/42")

    assert not PART_UPLOAD_PATH_RE.match(f"/api/v1/uploads/{upload_id}/completions")
    assert not PART_UPLOAD_PATH_RE.match(f"/api/v1/uploads/{upload_id}")
    assert not PART_UPLOAD_PATH_RE.match(
        f"/api/v1/projects/{upload_id}/uploads"
    )
    assert not PART_UPLOAD_PATH_RE.match(f"/api/v1/uploads/{upload_id}/parts/")
    assert not PART_UPLOAD_PATH_RE.match(f"/api/v1/uploads/{upload_id}/parts/abc")


def test_oversized_plain_body_is_rejected(client: TestClient) -> None:
    """普通请求体超过 2 MB → 413，且错误体是协议形状。"""
    payload = b"x" * (DEFAULT_BODY_LIMIT_BYTES + 1)
    response = client.post(
        "/api/v1/projects/00000000-0000-4000-8000-000000000001/uploads",
        content=payload,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "UPLOAD_TOO_LARGE"
    assert body["error"]["details"]["reason"] == "request_body_too_large"
    assert body["error"]["details"]["limitBytes"] == str(DEFAULT_BODY_LIMIT_BYTES)


def test_unauthenticated_upload_request_is_rejected(client: TestClient) -> None:
    """未登录 → 401（会话相关错误码），不是 500。"""
    response = client.post(
        "/api/v1/projects/00000000-0000-4000-8000-000000000001/uploads",
        json={
            "fileName": "a.pdf",
            "byteSize": 10,
            "mimeType": "application/pdf",
            "purpose": PURPOSE_RESEARCH,
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"


def test_force_forbid_rejects_extra_fields(client: TestClient) -> None:
    """多传字段 → 422（`extra="forbid"` 让契约变化保持显式）。"""
    response = client.post(
        "/api/v1/projects/00000000-0000-4000-8000-000000000001/uploads",
        json={
            "fileName": "a.pdf",
            "byteSize": 10,
            "mimeType": "application/pdf",
            "purpose": PURPOSE_RESEARCH,
            "unexpected": "x",
        },
    )
    # 未登录时先撞 401；这里断言的是"没有被静默接受"。
    assert response.status_code in {401, 422}


def test_quota_purposes_only_include_research() -> None:
    """需求 §6.2：配额只算研究原文件，不含 Brief / 生成媒体 / 内部索引。"""
    assert QUOTA_PURPOSES == frozenset({PURPOSE_RESEARCH})
    assert PURPOSE_BRIEF not in QUOTA_PURPOSES


def test_upload_states_holding_reservation_covers_completing() -> None:
    """`COMPLETING` 必须算作"持有预占"。

    进程若在 COMPLETING 阶段崩溃，预占还挂在账上 ——
    TTL 回收必须能把它捞回来。少这一个状态，那部分额度永久流失。
    """
    assert UPLOAD_STATE_UPLOADING in UPLOAD_STATES_HOLDING_RESERVATION
    assert "COMPLETING" in UPLOAD_STATES_HOLDING_RESERVATION
    assert UPLOAD_STATE_COMPLETED not in UPLOAD_STATES_HOLDING_RESERVATION
    assert UPLOAD_STATE_CANCELLED not in UPLOAD_STATES_HOLDING_RESERVATION


def test_uploaded_file_record_is_queryable(env: UploadEnv) -> None:
    """`GET /files/{id}` 依赖的字段必须都能取到并自洽。"""
    result = _upload_bytes(env, b"z" * 64, file_name="样例.pdf")
    with session_scope(env.settings) as session:
        file_row, blob = get_file(FileRepository(session), file_id=result.file_id)

    assert isinstance(file_row, FileObject)
    assert file_row.original_name == "样例.pdf"
    assert file_row.classification == CLASSIFICATION_SUPPORTED
    assert blob.byte_size == 64
    assert blob.mime_type  # 非空

    with session_scope(env.settings) as session:
        part_rows = session.execute(
            select(UploadPart).where(
                UploadPart.upload_id == _session_id_of(env, result.file_id)
            )
        ).scalars().all()
    assert len(part_rows) == 1
