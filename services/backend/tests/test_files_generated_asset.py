"""`files.store_generated_asset` 的判据（WP27 §2.4 跨包接缝 / M3）。

**这个函数为什么存在**：M3 的 `media` 需要把生成图存成正式资产，
而全仓唯一的 `FileObject` 构造点此前只被 `complete_upload`（上传会话）驱动
—— 服务端**没有**"把已在内存里的字节直接落成 file_object"的公开路径。
本文件是那条路径的判据。

**判据设计上的两条自我约束**（对应本仓已登记的判据缺陷类型）：

1. **期望值不由被测对象派生**。sha256 由 `hashlib` 独立算、
   `object_key` 的结构由字符串断言单独给出；
   不断言"函数返回的 sha256 == 函数自己算的 sha256"这种同源自证。
2. **完备性有独立对账**。"暂存目录不留残渣""Outbox 只在上传链路写"
   这类断言如果只写"某个集合为空"，变异后可能仍然通过，
   所以每个"应当为空"都配一条"确实有东西进来过"的对照。
"""

from __future__ import annotations

import hashlib
import io
import os
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from rednote.modules.files.models import (
    PURPOSE_GENERATED,
    PURPOSE_RESEARCH,
    QUOTA_LIMIT_BYTES,
    QUOTA_PURPOSES,
    STORAGE_BACKEND_LOCAL,
    VISIBILITY_PROJECT,
    Blob,
    FileObject,
    ProjectFileLink,
)
from rednote.modules.files.repository import FileRepository
from rednote.modules.files.service import (
    StoredGeneratedAsset,
    complete_upload,
    create_upload_session,
    normalize_purpose,
    quota_snapshot,
    store_generated_asset,
    store_generated_asset_from_path,
    store_part,
)
from rednote.modules.files.storage import (
    STAGING_PREFIX,
    LocalStorageBackend,
    object_key_for,
    staging_key_for,
)
from rednote.modules.identity.models import ROLE_OPERATOR
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.outbox.models import EVENT_FILE_UPLOADED, OutboxEvent
from rednote.modules.projects.models import Brand, Product, Project
from rednote.modules.projects.repository import ProjectRepository
from rednote.platform.configuration import Settings
from rednote.platform.database import session_scope
from rednote.platform.errors import ApiError

TEST_ISSUER = "https://rednote.test/issuer"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="

# DSN 只从环境变量取，**不硬编码**（`check_test_db_isolation.py` 会抓）。
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
TEST_DATABASE_URL = os.environ.get(
    "REDNOTE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL
)

# 一段最小合法 PNG 头（不是可解码图片，只用于让分类判定有真实扩展名可依）。
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"rednote-generated-asset"


def _unique_png() -> bytes:
    """每次调用都给出**库里一定没有过**的字节。

    ## 为什么需要它
    `blob_created` 表达的是"这一行 blob 是本次新建的"，
    它依赖**数据库历史**，不只依赖本次调用。同一个库被多个测试共用，
    `PNG_BYTES` 很可能已经被别的用例写进去过 ⇒ 首次调用就 `blob_created=False`
    ⇒ 断言 `first.blob_created is True` 变成**顺序依赖型假红**，
    而且红的时候错误信息会指向"我的实现没新建 blob"，与真相完全无关。
    """
    return PNG_BYTES + uuid.uuid4().bytes


def _make_settings(storage_root: Path) -> Settings:
    return Settings(
        database_url=TEST_DATABASE_URL,
        environment="test",
        session_cookie_secure=False,
        oidc_issuer=TEST_ISSUER,
        oidc_token_encryption_key=TEST_ENCRYPTION_KEY,
        storage_backend=STORAGE_BACKEND_LOCAL,
        storage_local_root=str(storage_root),
    )


def _create_user(settings: Settings, *, role: str = ROLE_OPERATOR) -> uuid.UUID:
    with session_scope(settings) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name=f"用户-{role}",
            role=role,
        )
        return user.id


def _seed_project(settings: Settings, *, owner_id: uuid.UUID) -> uuid.UUID:
    with session_scope(settings) as session:
        brand = Brand(name=f"品牌-{uuid.uuid4().hex[:8]}")
        session.add(brand)
        session.flush()
        product = Product(brand_id=brand.id, name="测试产品")
        session.add(product)
        session.flush()
        project = Project(
            project_code=f"GEN-{uuid.uuid4().hex[:8].upper()}",
            name="生成资产测试项目",
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
class GenEnv:
    settings: Settings
    store: LocalStorageBackend
    project_id: uuid.UUID
    owner_id: uuid.UUID
    storage_root: Path


@pytest.fixture
def env(tmp_path: Path) -> GenEnv:
    root = tmp_path / "storage"
    settings = _make_settings(root)
    store = LocalStorageBackend(root)
    owner = _create_user(settings)
    project_id = _seed_project(settings, owner_id=owner)
    return GenEnv(
        settings=settings,
        store=store,
        project_id=project_id,
        owner_id=owner,
        storage_root=root,
    )


class _CountingStorage:
    """包在真实后端外面的**记录型探针**。

    ⚠️ **它只记录、全部委托给内层**（真的读写盘）——
    若它自己模拟读写，断言就变成在验证探针自己。
    计数只回答"做了几次某个动作"，不回答"做对了吗"。
    """

    def __init__(self, inner: LocalStorageBackend) -> None:
        self._inner = inner
        self.moves = 0
        self.deletes = 0
        self.staging_writes = 0
        self.staging_deletes = 0

    @property
    def backend_name(self) -> str:
        return self._inner.backend_name

    def reset(self) -> None:
        self.moves = 0
        self.deletes = 0
        self.staging_writes = 0
        self.staging_deletes = 0

    def write_part(self, *, key: str, stream) -> object:
        if key.startswith(f"{STAGING_PREFIX}/"):
            self.staging_writes += 1
        return self._inner.write_part(key=key, stream=stream)

    def assemble(self, *, part_keys, target_key):  # pragma: no cover - 本路径不走
        return self._inner.assemble(part_keys=part_keys, target_key=target_key)

    def open_stream(self, *, key: str):
        return self._inner.open_stream(key=key)

    def delete(self, *, key: str) -> bool:
        if key.startswith(f"{STAGING_PREFIX}/"):
            self.staging_deletes += 1
        else:
            self.deletes += 1
        return self._inner.delete(key=key)

    def delete_prefix(self, *, prefix: str) -> int:  # pragma: no cover - 本路径不走
        return self._inner.delete_prefix(prefix=prefix)

    def move(self, *, from_key: str, to_key: str) -> None:
        self.moves += 1
        self._inner.move(from_key=from_key, to_key=to_key)

    def exists(self, *, key: str) -> bool:
        return self._inner.exists(key=key)

    def size(self, *, key: str) -> int:  # pragma: no cover - 本路径不走
        return self._inner.size(key=key)


def _params(env: GenEnv, *, content: bytes = PNG_BYTES, **kwargs: object) -> dict[str, object]:
    """`store_generated_asset` 的默认参数集（`_store` 与探针判据共用）。"""
    params: dict[str, object] = {
        "project_id": env.project_id,
        "original_name": "生成配图.png",
        "content": content,
        "mime_type": "image/png",
        "uploaded_by": env.owner_id,
    }
    params.update(kwargs)
    return params


def _store(env: GenEnv, *, content: bytes = PNG_BYTES, **kwargs: object):
    with session_scope(env.settings) as session:
        return store_generated_asset(
            FileRepository(session), env.store, **_params(env, content=content, **kwargs)
        )


def _counts(settings: Settings, *, project_id: uuid.UUID | None = None) -> dict[str, int]:
    """按项目范围数行 —— **不用全库计数**。

    ## 为什么不数全库
    全库计数依赖一个**没被建立的前提**："库里只有本用例造的数据"。
    同一个库被多个测试文件共用（`test_files_wp12.py` 也在写 `file_objects`），
    一旦字母序让对方先跑，`count(*) == 2` 就成了**顺序依赖型假红**。
    这类判据的错误信息会指向完全无关的方向（"我的实现多建了 10 行"）。
    """
    with session_scope(settings) as session:
        file_stmt = select(func.count()).select_from(FileObject)
        link_stmt = select(func.count()).select_from(ProjectFileLink)
        if project_id is not None:
            file_stmt = file_stmt.where(FileObject.project_id == project_id)
            link_stmt = link_stmt.where(ProjectFileLink.project_id == project_id)
        return {
            "files": session.scalar(file_stmt) or 0,
            "links": session.scalar(link_stmt) or 0,
        }


@dataclass(frozen=True)
class _OrderedPart:
    """`complete_upload` 读的是**属性**（`getattr(part, "part_no")`），
    传 dict 会静默取到 `None` 然后报「第 1 项的分片号应为 1」。"""

    part_no: int
    sha256: str | None = None


def _upload_research(env: GenEnv, *, content: bytes) -> int:
    """走**真实上传链路**（建会话 → 传分片 → 完成），返回实际字节数。

    这是配额判据的对照组：`store_generated_asset` **刻意不**走这条路，
    所以只有真的跑一遍上传，才能证明"配额会动"这件事本身是真的。
    """
    with session_scope(env.settings) as session:
        repo = FileRepository(session)
        created = create_upload_session(
            repo,
            project_id=env.project_id,
            file_name="研究资料.pdf",
            byte_size=len(content),
            mime_type="application/pdf",
            purpose=PURPOSE_RESEARCH,
            created_by=env.owner_id,
        )
        session_id = created.session_id

    with session_scope(env.settings) as session:
        stored = store_part(
            FileRepository(session),
            env.store,
            session_id=session_id,
            part_no=1,
            stream=io.BytesIO(content),
        )

    with session_scope(env.settings) as session:
        outcome = complete_upload(
            FileRepository(session),
            env.store,
            session_id=session_id,
            ordered_parts=[_OrderedPart(part_no=1, sha256=stored.sha256)],
        )
    assert outcome.result is not None, f"上传未成功：{outcome.failure}"
    return outcome.result.byte_size


# ===========================================================================
# 第 1 层：物理与去重
# ===========================================================================


def test_object_key_is_content_addressed_and_written_to_storage(env: GenEnv) -> None:
    """落盘 key 必须是 `objects/<sha前2位>/<sha>`，且真实存在于存储。

    sha256 由测试独立用 `hashlib` 算 —— 不复用被测对象的计算结果。
    """
    expected_sha = hashlib.sha256(PNG_BYTES).hexdigest()
    expected_key = f"objects/{expected_sha[:2]}/{expected_sha}"

    result = _store(env)

    assert result.sha256 == expected_sha
    assert env.store.exists(key=expected_key)
    assert env.store.size(key=expected_key) == len(PNG_BYTES)
    # 读回来的字节必须逐字节相同：证明没有"追加写坏内容"。
    assert b"".join(env.store.open_stream(key=expected_key)) == PNG_BYTES


def test_object_key_derivation_agrees_with_storage_helper(env: GenEnv) -> None:
    """独立实现与 `object_key_for` 必须同口径（防止两处各写一份规则）。

    期望 key 在这里**独立拼一次**（`f"objects/{sha[:2]}/{sha}"`），
    而不是把 `object_key_for` 的输出当期望值 —— 后者是同源自证。
    """
    result = _store(env)

    assert result.sha256 == hashlib.sha256(PNG_BYTES).hexdigest()

    with session_scope(env.settings) as session:
        blob = session.get(Blob, result.blob_id)
        assert blob is not None
        assert blob.object_key == object_key_for(result.sha256)
        assert blob.object_key == f"objects/{result.sha256[:2]}/{result.sha256}"


def test_staging_area_is_left_clean(env: GenEnv) -> None:
    """暂存区不得留**文件** —— 它不会被 TTL 清理扫到，残留即永久占盘。

    先做对照：确认对象区**确实**落了盘。
    否则"暂存区为空"可能仅仅因为函数压根没写任何东西。

    ⚠️ **只查文件、不查目录**：`move` 之后 `staging/<purpose>/` 会剩一个
    空目录（`_ensure_parent` 建的）。空目录零字节、不占对象存储、
    也没有清理路径会碰它 —— 断言它反而会把判据变成"实现细节的镜像"。
    真正要防的是**残留字节**。
    """
    _store(env, content=_unique_png())

    objects_root = env.storage_root / "objects"
    written = [p for p in objects_root.rglob("*") if p.is_file()]
    assert len(written) == 1, f"对象区应恰好有 1 个文件，实际 {written}"

    staging_root = env.storage_root / STAGING_PREFIX
    leftovers = (
        [p for p in staging_root.rglob("*") if p.is_file()]
        if staging_root.exists()
        else []
    )
    assert leftovers == [], f"暂存区有残留字节：{leftovers}"

    # 对照：再存一份**不同内容**，证明落盘通路仍然正常 ——
    # 否则上面的"暂存区空"可能是因为第二次调用压根什么都没做。
    assert _store(env, content=_unique_png()).blob_created is True


def test_same_content_twice_shares_blob_but_yields_distinct_file_ids(
    env: GenEnv,
) -> None:
    """**物理去重与逻辑独立必须分开**（R9 的基础性质）。

    同内容两次调用 ⇒ 同一个 `blob`、两个不同的 `file_object`。
    这两条断言必须成对出现：只断言其中一个，实现可以走另一条路而不红。

    字节用 `_unique_png()`：必须保证"第一次调用时库里确实没有这份内容"，
    否则 `blob_created` 的首条断言依赖别的测试有没有写过同一份字节。
    """
    content = _unique_png()
    first = _store(env, content=content)
    second = _store(env, content=content)

    assert first.blob_id == second.blob_id, "同内容必须复用同一个物理对象"
    assert first.file_id != second.file_id, "逻辑引用必须每次新建，否则删一个会牵连另一个"
    assert first.blob_created is True
    assert second.blob_created is False

    # 按项目数行（不用全库计数，理由见 `_counts`）。
    assert _counts(env.settings, project_id=env.project_id) == {"files": 2, "links": 2}

    # blob 侧：同一个 object_key 在库里只有一行。
    # 判据按 object_key 而非 `count(*) == 1` —— 后者会把
    # 同一库里别的测试文件写的 blob 也算进来。
    with session_scope(env.settings) as session:
        keyed = session.scalars(
            select(Blob).where(Blob.object_key == object_key_for(first.sha256))
        ).all()
        assert len(keyed) == 1
        assert keyed[0].id == first.blob_id
        assert keyed[0].byte_size == len(content)


def test_physical_dedup_skips_rewrite_when_content_already_on_disk(
    env: GenEnv,
) -> None:
    """同内容第二次落库**不得重写对象**（物理去重的实际动作）。

    ## 为什么这条判据必须用探针，而不是"跑两次看结果"
    对象 key 由 sha256 决定 ⇒ 重写与不重写**落盘结果完全相同**。
    第一次变异实测：把 `exists` 分支整段删掉、直接 `move` 覆盖，
    全部判据依然全绿 —— 因为它在**行为上等价**（等价变异）。
    也就是说"有没有真的跳过重写"在真实存储上**不可观测**。

    所以这里用一个记录调用的探针：把"写了没有"变成可数的事实。
    探针只记录、不改变行为（`move` 仍真调），因此不会把断言变成自证。

    ## 期望值不由被测对象派生
    "第二次不该有 move"来自**内容寻址的定义**（同内容 = 同 key = 无需重写），
    不是从实现里读出来的。第一条（必须有 move）是对照：
    没有它，一个"从不写盘"的退化实现也能满足"第二次没有 move"。
    """
    probe = _CountingStorage(env.store)
    content = _unique_png()

    with session_scope(env.settings) as session:
        store_generated_asset(FileRepository(session), probe, **_params(env, content=content))
    assert probe.moves, "对照失效：首次落库竟然没有 move（那它压根没写盘）"

    probe.reset()
    with session_scope(env.settings) as session:
        store_generated_asset(FileRepository(session), probe, **_params(env, content=content))
    assert probe.moves == 0, "同内容已存在时不得重写对象（物理去重被绕过）"
    assert probe.staging_writes == 1, "写入暂存区是允许的（读回内容需要），但必须被丢弃"
    assert probe.staging_deletes == 1, "去重命中时应删掉暂存件，而不是 move 覆盖"


def test_staging_key_contains_no_user_input() -> None:
    """暂存 key 里只有服务端常量与 UUID —— 路径穿越的结构性防护。"""
    key = staging_key_for(purpose="generated", token=uuid.UUID(int=1))
    assert key == f"staging/generated/{uuid.UUID(int=1)}"
    assert ".." not in key
    assert key.startswith(f"{STAGING_PREFIX}/")


def test_empty_content_is_rejected(env: GenEnv) -> None:
    """空字节必须被拒：它会落一个 size=0 的对象，且"生成资产为空"无意义。"""
    with pytest.raises(ApiError) as excinfo:
        _store(env, content=b"")
    assert excinfo.value.details["reason"] == "content_empty"

    # 对照：同一个 env 随后能正常落盘，说明上面的拒绝不是环境坏了。
    assert _store(env).byte_size == len(PNG_BYTES)


def test_invalid_classification_is_rejected(env: GenEnv) -> None:
    with pytest.raises(ApiError) as excinfo:
        _store(env, classification="MAYBE")
    assert excinfo.value.details["reason"] == "classification_invalid"


# ===========================================================================
# 第 2 层：数据库约束（不是内存状态）
# ===========================================================================


def test_rows_satisfy_visibility_and_link_constraints(env: GenEnv) -> None:
    """`file_object` 必须是 PROJECT 可见且带 project_id。

    断言的是**真实落库的行**而不是返回值：返回值可以是调用方自己编的，
    而 `visibility_matches_project` CHECK 才是不许编的那道。
    """
    result = _store(env)

    with session_scope(env.settings) as session:
        file_row = session.get(FileObject, result.file_id)
        assert file_row is not None
        assert file_row.visibility == VISIBILITY_PROJECT
        assert file_row.project_id == env.project_id
        assert file_row.uploaded_by == env.owner_id
        assert file_row.original_name == "生成配图.png"
        assert file_row.classification == "SUPPORTED"

        link = session.scalar(
            select(ProjectFileLink).where(
                ProjectFileLink.file_id == result.file_id
            )
        )
        assert link is not None
        assert link.project_id == env.project_id
        assert link.purpose == PURPOSE_GENERATED
        assert link.effective is True


def test_default_purpose_is_generated_and_not_quota_purposes() -> None:
    """默认用途必须是 `GENERATED`，且它**不在**配额用途集合里。

    这条断言直接对着 models 的常量 —— 若有人把 `GENERATED`
    加进 `QUOTA_PURPOSES`，这里立刻红。
    """
    assert normalize_purpose(PURPOSE_GENERATED) == PURPOSE_GENERATED
    assert PURPOSE_GENERATED not in QUOTA_PURPOSES
    assert PURPOSE_RESEARCH in QUOTA_PURPOSES


def test_generated_asset_does_not_consume_research_quota(env: GenEnv) -> None:
    """生成资产**不占研究配额**（需求 §6.2："不包含 Brief、生成媒体……"）。

    对照：走一遍**上传链路**存一份 `RESEARCH`，确认配额**确实会动**。
    没有这条对照，"生成后配额没变"可以由"谁都不扣配额"的退化实现满足。
    """
    with session_scope(env.settings) as session:
        before = quota_snapshot(FileRepository(session), env.project_id)
    assert before["effectiveBytes"] == 0
    assert before["reservedBytes"] == 0
    assert before["limitBytes"] == QUOTA_LIMIT_BYTES

    # 对照组：上传 RESEARCH 走 `complete_upload` ⇒ 配额按实际大小记入
    moved = _upload_research(env, content=b"research-bytes" * 8)
    assert moved > 0, "对照组失效：上传 RESEARCH 竟然没改变配额"
    with session_scope(env.settings) as session:
        after_research = quota_snapshot(FileRepository(session), env.project_id)
    assert after_research["effectiveBytes"] == moved

    # 实验组：存 GENERATED ⇒ 账面**完全不变**
    snapshot_before = after_research
    _store(env)
    with session_scope(env.settings) as session:
        after = quota_snapshot(FileRepository(session), env.project_id)
    assert after == snapshot_before, "生成资产不得改变研究配额账面"


def test_store_generated_asset_does_not_write_outbox_events(env: GenEnv) -> None:
    """本函数**刻意不写** Outbox 事件。

    `EVENT_FILE_UPLOADED` 的语义是"用户上传完成，触发扫描"；
    生成资产复用它会让下游扫描器把服务端产物当用户上传处理。
    与其发一个语义不对的事件，不如先不发，把该发什么登记为 OP。

    **对照必须真跑上传**：`count(*) == 0` 在一个"Outbox 通路整体坏了"
    的库里也成立（同样 0 事件）。所以这里先跑一遍 `complete_upload`，
    确认事件**确实被写进去了**，再断言生成资产**一条都没多**。
    """
    result = _store(env, content=_unique_png())

    with session_scope(env.settings) as session:
        mine = session.scalars(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == result.file_id)
        ).all()
        assert mine == [], f"生成资产不该发事件，实际发了 {len(mine)} 条"

    # 对照组：真实上传链路确实会为它的 file_object 发事件。
    _upload_research(env, content=b"outbox-control-" + uuid.uuid4().bytes)

    with session_scope(env.settings) as session:
        assert session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.event_type == EVENT_FILE_UPLOADED)
        ) >= 1, "对照组失效：上传链路竟然没发 file.uploaded 事件"

    # 生成资产那一条**仍然**是 0（对照之后复验，防止"先发后删"蒙混）。
    with session_scope(env.settings) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.aggregate_id == result.file_id)
            )
            == 0
        )


# ===========================================================================
# 第 3 层：R9 —— 预览与正式必须是两个独立资产
# ===========================================================================


def test_preview_and_final_are_independent_assets(env: GenEnv) -> None:
    """**R9**：预览资产与正式资产必须是两个不同的 `file_object`。

    模拟 media 的真实用法：同一份字节先存成预览，再存成正式。
    撤下预览不得影响正式。

    ⚠️ **删的顺序必须是先 link 后 file_object** ——
    `project_file_links.file_id` 上是 FK RESTRICT，反过来删会撞
    `ForeignKeyViolation`。这不是测试写法的琐事，而是**判据本身要证明的性质**：
    预览与正式各自持有独立的 link 行，所以删预览只需要动它自己那一行。
    """
    content = _unique_png()
    preview = _store(env, content=content, original_name="预览.png", purpose="REFERENCE")
    final = _store(env, content=content, original_name="正式.png", purpose=PURPOSE_GENERATED)

    assert preview.file_id != final.file_id
    assert preview.blob_id == final.blob_id

    with session_scope(env.settings) as session:
        alive = session.scalars(
            select(FileObject).where(
                FileObject.id.in_([preview.file_id, final.file_id])
            )
        ).all()
        assert len(alive) == 2

    # 撤下预览：先删它自己的 link，再删它自己的 file_object。
    with session_scope(env.settings) as session:
        link_rows = session.scalars(
            select(ProjectFileLink).where(ProjectFileLink.file_id == preview.file_id)
        ).all()
        assert len(link_rows) == 1, "预览必须只持有自己的那一条 link"
        for row in link_rows:
            session.delete(row)
        session.flush()
        victim = session.get(FileObject, preview.file_id)
        assert victim is not None
        session.delete(victim)
        session.flush()

    with session_scope(env.settings) as session:
        assert session.get(FileObject, preview.file_id) is None
        survivor = session.get(FileObject, final.file_id)
        assert survivor is not None, "删预览不得牵连正式资产"
        assert survivor.blob_id == preview.blob_id
        # 正式资产的 link 也必须还在（撤下预览不该动到它）。
        survivor_links = session.scalars(
            select(ProjectFileLink).where(ProjectFileLink.file_id == final.file_id)
        ).all()
        assert len(survivor_links) == 1


def test_cross_project_same_content_shares_blob_but_not_link(
    env: GenEnv,
) -> None:
    """跨项目同内容：blob 共用，link 各自独立。

    这是"物理去重不等于权限共用"的判据 —— 两个项目的 link 必须各自一行。
    """
    other_owner = _create_user(env.settings)
    other_project = _seed_project(env.settings, owner_id=other_owner)
    content = _unique_png()

    first = _store(env, content=content)
    with session_scope(env.settings) as session:
        second = store_generated_asset(
            FileRepository(session),
            env.store,
            project_id=other_project,
            original_name="生成配图.png",
            content=content,
            mime_type="image/png",
            uploaded_by=other_owner,
        )

    assert first.blob_id == second.blob_id
    assert first.file_id != second.file_id

    # blob 侧：同一个 object_key 只有一行（按 key 查，不用 `count(*)`）。
    with session_scope(env.settings) as session:
        keyed = session.scalars(
            select(Blob).where(Blob.object_key == object_key_for(first.sha256))
        ).all()
        assert len(keyed) == 1
    # 按项目各数各的：两个项目各自 1 个 file_object + 1 条 link。
    # ⚠️ **不用全库计数**（见 `_counts` 的 docstring）——
    # 库里还有别的测试文件写的行，数全库得到的是"字母序"，不是"我的实现"。
    assert _counts(env.settings, project_id=env.project_id) == {"files": 1, "links": 1}
    assert _counts(env.settings, project_id=other_project) == {"files": 1, "links": 1}


# ===========================================================================
# 第 4 层：返回值形状（对外契约）
# ===========================================================================


def test_result_carries_blob_and_file_separately(env: GenEnv) -> None:
    """返回值必须同时给 `file_id` 与 `blob_id`，且两者不相等。

    只给 `blob_id` 会诱使调用方拿它当业务外键 —— 那会让
    "同内容两个业务对象"在数据层就共享删除后果，R9 直接失效。
    """
    result = _store(env)
    assert isinstance(result, StoredGeneratedAsset)
    assert result.blob_id != result.file_id
    assert result.original_name == "生成配图.png"
    assert result.classification == "SUPPORTED"
    assert result.byte_size == len(PNG_BYTES)


def test_classification_defaults_to_union_rule_and_can_be_overridden(
    env: GenEnv,
) -> None:
    """`classification` 缺省走 `classify_file`（扩展名 ∪ MIME），也可显式覆盖。"""
    defaulted = _store(env)
    assert defaulted.classification == "SUPPORTED"

    forced = _store(env, classification="UNSUPPORTED")
    assert forced.classification == "UNSUPPORTED"


def test_unknown_format_is_classified_unsupported(env: GenEnv) -> None:
    """`.bin` 不在承诺清单内 ⇒ 分类为 UNSUPPORTED（但**仍然落盘**）。

    "不支持的格式"不等于"拒绝入库" —— 上传链路的口径是收下并标注。
    """
    result = _store(env, original_name="未知.bin", mime_type="application/octet-stream")
    assert result.classification == "UNSUPPORTED"
    with session_scope(env.settings) as session:
        assert session.get(FileObject, result.file_id) is not None


# ===========================================================================
# 第 5 层：流式孪生 `store_generated_asset_from_path`（WP35）
# ===========================================================================
#
# 存在理由：批量 ZIP 不能整包进内存。判据钉三件事 ——
# ① 同样字节走两个入口，得到同一个 sha256 / object_key（期望值由 hashlib 独立算）；
# ② 空文件被拒，且拒绝路径不留 file_object（对照：正常路径确实留了）；
# ③ 内存与字节数无关（tracemalloc 峰值远小于文件体积）。


def _write_tmp(tmp_path: Path, content: bytes, name: str = "payload.bin") -> Path:
    target = tmp_path / name
    target.write_bytes(content)
    return target


def _store_path(env: GenEnv, path: Path, **kwargs: object):
    params: dict[str, object] = {
        "project_id": env.project_id,
        "original_name": "导出包.zip",
        "mime_type": "application/zip",
        "uploaded_by": env.owner_id,
    }
    params.update(kwargs)
    with session_scope(env.settings) as session:
        return store_generated_asset_from_path(
            FileRepository(session), env.store, path=path, **params
        )


def test_from_path_matches_bytes_entry_on_digest_and_key(
    env: GenEnv, tmp_path: Path
) -> None:
    """同样的字节：两个入口得到同一个 sha256 与 object_key，物理 blob 共用。"""
    content = _unique_png()
    expected_sha = hashlib.sha256(content).hexdigest()  # 独立于被测对象
    via_bytes = _store(env, content=content)
    via_path = _store_path(env, _write_tmp(tmp_path, content), original_name="生成配图.png",
                           mime_type="image/png")

    assert via_bytes.sha256 == expected_sha
    assert via_path.sha256 == expected_sha
    assert via_path.byte_size == len(content)
    assert via_path.blob_id == via_bytes.blob_id          # 物理去重命中
    assert via_path.blob_created is False                 # 对照：首次入口是创建者
    assert via_bytes.blob_created is True
    assert via_path.file_id != via_bytes.file_id          # 逻辑独立（R9）
    assert env.store.exists(key=object_key_for(expected_sha))


def test_from_path_writes_file_object_link_with_export_purpose(
    env: GenEnv, tmp_path: Path
) -> None:
    """`purpose=EXPORT` 落 link；不计研究配额（EXPORT 不在 QUOTA_PURPOSES）。"""
    from rednote.modules.files.models import PURPOSE_EXPORT

    assert PURPOSE_EXPORT not in QUOTA_PURPOSES
    before = _counts(env.settings, project_id=env.project_id)
    result = _store_path(env, _write_tmp(tmp_path, _unique_png()), purpose=PURPOSE_EXPORT)
    after = _counts(env.settings, project_id=env.project_id)

    assert (after["files"] - before["files"], after["links"] - before["links"]) == (1, 1)
    with session_scope(env.settings) as session:
        link = session.scalars(
            select(ProjectFileLink).where(ProjectFileLink.file_id == result.file_id)
        ).one()
        assert link.purpose == PURPOSE_EXPORT


def test_from_path_rejects_empty_file_and_leaves_no_rows(
    env: GenEnv, tmp_path: Path
) -> None:
    """空文件 ⇒ 校验错误，且不留 file_object / link。"""
    before = _counts(env.settings, project_id=env.project_id)
    with pytest.raises(ApiError):
        _store_path(env, _write_tmp(tmp_path, b""))
    assert _counts(env.settings, project_id=env.project_id) == before

    # 对照：非空文件确实会留下行（否则上面的"没留"可能只是函数根本没工作）。
    _store_path(env, _write_tmp(tmp_path, _unique_png(), name="ok.bin"))
    assert _counts(env.settings, project_id=env.project_id)["files"] == before["files"] + 1


def test_from_path_peak_memory_is_independent_of_file_size(
    env: GenEnv, tmp_path: Path
) -> None:
    """流式：64 MiB 文件，tracemalloc 峰值必须远小于文件体积（< 16 MiB）。

    对照：同一份内容整包读成 bytes 的峰值 ≥ 文件体积 ⇒ 探针确实测得出"整包进内存"，
    否则"峰值小"可能只是探针没在测。
    """
    import tracemalloc

    size = 64 * 1024 * 1024
    chunk = os.urandom(1024 * 1024)
    big = tmp_path / "big.bin"
    with big.open("wb") as fh:
        for _ in range(size // len(chunk)):
            fh.write(chunk)

    tracemalloc.start()
    try:
        result = _store_path(env, big, original_name="big.zip")
        _, streamed_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    tracemalloc.start()
    try:
        whole = big.read_bytes()
        _, whole_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    del whole

    assert result.byte_size == size
    assert whole_peak >= size                       # 探针有效性
    assert streamed_peak < 16 * 1024 * 1024         # 流式峰值与体积脱钩
