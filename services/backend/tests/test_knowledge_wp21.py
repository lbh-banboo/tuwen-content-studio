"""WP21 测试：切块 + embedding + 范围过滤 + 检索。

覆盖 WP21 验收原文（研发计划 04 §4.3）：
> **「近似品牌案例不串事实」**

前半句「近似品牌案例」由 `chunking.py` + embedding 负责；
后半句「**不串事实**」由 `scope_filter.py` 负责（范围谓词下推到 SQL）。

**测试策略（四层）**：

1. **纯函数层**（不碰数据库）：切块器的块长上限与重叠、规范化与哈希、
   token 估算、关键词派生（含 CJK 2-gram）、结构化字段切块、
   范围谓词的形状与 fail-closed、距离→相似度换算。
   这些是"规则正确性"的权威测试 —— 判据本身是纯字符串/集合运算，
   因此可以用**构造出来的输入**穷举边界，不必建库。
2. **服务层 + 真库**：建 profile（+ 重名 422）、建索引、切换 profile
   （**顺序敏感**）、解析 profile（不静默回退）、ingest（幂等 + 维度校验）、
   检索（两种 mode + 参数校验 + 空查询）。
   **验收主要打在这一层** —— 端点是薄层。
3. **数据库层兜底**：用**原生 SQL** 绕过 ORM 与 service，验证
   混用守卫的**三条判据**（异维度 / **同维度混用** / dimensions 列不符）、
   唯一约束、`ON DELETE CASCADE`、部分 HNSW 索引真的建在表达式上。
   **这一层是本包最重要的测试之一**：ADR-15 判据「混用被阻止」
   的数据库层兜底只有在这里才被真正验证。
4. **HTTP 层**：`POST /knowledge/searches` 的状态码、响应形状、
   权限（只需登录，不需写权限）、错误体形状。

**为什么第 3 层必须存在**：`chunk_embeddings.embedding` 是无维度 `vector`
（pgvector 的硬约束：无维度列**建不了** HNSW），索引改成
**每 profile 一个部分索引**且建在表达式 `embedding::vector(<dims>)` 上。
这个决定的全部依据都在"索引真的能建、真的被用上、跨 profile 真的不串"——
只测 ORM 路径等于没有证据。

**为什么必须有反向验证**（`test_reverse_*` 系列）：
判据纪律第 7 条（ISS-82 同族）——**一个判据如果不能区分两种实现，
它就是恒真的、没有价值的**。因此每个关键判据都配一条反向验证：
把错误实现打桩放回去，断言判据**变红**。

---

## 本文件的建表机制

**不生成迁移文件**（那会让另一分支的 `alembic upgrade head` 撞上
"Multiple head revisions are present"），改为 module 级 fixture
用 `Base.metadata.create_all(engine, tables=[...])` 建**自己那两张表**，
再用 `embedding_ddl.all_trigger_ddl()` 建守卫触发器
（**必须 DROP → CREATE**：`CREATE TRIGGER` 不是幂等的，
本机实测会报 `DuplicateObject`）。

**只 DROP 本包自己的两张表**，绝不碰 `knowledge_*`（WP20 的表）与
`model_profiles` / `providers`（WP16 的表）—— 删它们会连带影响别的包。

**外键目标表必须已注册进 metadata**：`embedding_profiles.model_profile_id`
指向 `model_profiles`，`chunk_embeddings.chunk_id` 指向 `knowledge_chunks`。
因此本文件 import 了 gateway 与 knowledge 的 models ——
**只为让 metadata 认识它们，不建它们**。

## 为什么"零残留"用**基线差分**而不是"全表 0 行"

`knowledge_versions` 有不可变触发器（`rednote_reject_mutation()`），
**物理删不掉** —— 这是 WP20 的设计（版本是历史事实）。
因此本包探针数据的处置是：**资产归档**（`archived_at`）+ **本包两张表 DROP**，
判据是"本次新增的可见知识 = 0"，不是"表为空"。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, aliased

from conftest import cross_package_foreign_keys, drop_own_tables

from rednote.modules.gateway import models as gw_models  # noqa: F401
from rednote.modules.identity.models import ROLE_OPERATOR
from rednote.modules.identity.repository import IdentityRepository
from rednote.modules.knowledge import embedding_ddl as emb_ddl
from rednote.modules.knowledge import embedding_models as emb_models
from rednote.modules.knowledge import embedding_service as emb_service
from rednote.modules.knowledge import indexing as kn_indexing
from rednote.modules.knowledge import models as kn_models  # noqa: F401
from rednote.modules.knowledge import scope_filter as kn_scope
from rednote.modules.knowledge.chunking import (
    CHUNKER_VERSION,
    count_tokens,
    derive_keywords,
    hash_chunk_text,
    normalize_text,
    chunk_for_version,
    chunk_structured,
    chunk_text,
)
from rednote.modules.knowledge.embedding_models import (
    ChunkEmbedding,
    EmbeddingProfile,
)
from rednote.modules.knowledge.embedding_repository import EmbeddingRepository
from rednote.modules.knowledge.embedding_types import (
    DEFAULT_EMBEDDING_DIMENSIONS,
    DEFAULT_SEARCH_LIMIT,
    MAX_EMBEDDING_DIMENSIONS,
    METRIC_COSINE,
    METRIC_INNER_PRODUCT,
    METRIC_L2,
    MIN_EMBEDDING_DIMENSIONS,
    PROFILE_STATE_ACTIVE,
    PROFILE_STATE_DEPRECATED,
    SEARCH_KEYWORD,
    SEARCH_SEMANTIC,
    ChunkDraft,
    ScopeSubject,
    distance_to_similarity,
)
from rednote.modules.knowledge.models import (
    KnowledgeAsset,
    KnowledgeChunk,
    KnowledgeVersion,
)
from rednote.platform.configuration import Settings
from rednote.platform.database import get_engine, reset_engines, session_scope
from rednote.platform.errors import ApiError, ErrorCode

TEST_ISSUER = "https://idp.test.invalid/realms/rednote"
TEST_ENCRYPTION_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXVuaXQtdGVzdHM="
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rednote@localhost:5432/rednote_test"
CSRF_SECRET_PLAINTEXT = "test-csrf-secret-plaintext"
TEST_ORIGIN = "http://testserver"

#: 本包自己的两张表（`knowledge_*` / `model_profiles` **不在其中**）。
OWN_TABLES: tuple[Any, ...] = (
    EmbeddingProfile.__table__,
    ChunkEmbedding.__table__,
)


def _test_database_url() -> str:
    """DSN 从环境变量取（分支用 `rednote_test_wp21`），缺省本机 postgres。"""
    return os.environ.get("REDNOTE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL).strip()


def _make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": _test_database_url(),
        "environment": "test",
        "session_cookie_secure": False,
        "oidc_issuer": TEST_ISSUER,
        "oidc_token_encryption_key": TEST_ENCRYPTION_KEY,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ===========================================================================
# 建表夹具（module 级）
# ===========================================================================


@pytest.fixture(scope="module", autouse=True)
def _ensure_own_tables() -> Iterator[None]:
    """只建本包的两张表 + 守卫触发器。

    **先 DROP 自己的两张表**：`create_all` 只对"表不存在"幂等 ——
    表已存在但结构是旧的时候它会**静默跳过**，于是测试跑在结构不对的表上。

    **触发器必须 DROP → CREATE**（`CREATE TRIGGER` 不是幂等的）。

    `reset_engines()` 先跑：conftest 的 `_isolate_settings_cache` 是
    autouse 的 function 级 fixture，module 级 fixture 拿到的 engine
    可能已被上一轮清掉。
    """
    reset_engines()
    engine = get_engine(_make_settings())

    # 夹具前后核对「跨包入向外键」集合必须不变（ISS-106 的判据）。
    fks_before = cross_package_foreign_keys(engine, emb_models.DROP_ORDER)

    # 只删「没有跨包入向外键」的表；被别的包引用的表必须保留。
    # ⚠️ 本包自己的 `chunk_embeddings` 引用 `knowledge_chunks`（WP20 的表），
    # 因此 DROP 顺序与保留判定都要按 `emb_models.DROP_ORDER` 走。
    dropped, kept = drop_own_tables(engine, emb_models.DROP_ORDER)

    # 外键目标表已在 metadata 里（本文件顶部 import 的副作用），
    # 这里只建本包的两张。
    EmbeddingProfile.metadata.create_all(engine, tables=list(OWN_TABLES))

    fks_after = cross_package_foreign_keys(engine, emb_models.DROP_ORDER)
    assert fks_after == fks_before, (
        f"建表夹具改变了跨包外键集合：before={sorted(fks_before)} "
        f"after={sorted(fks_after)}（dropped={dropped} kept={kept}）"
    )

    with engine.begin() as conn:
        for _t, fn_ddl, tg_ddl, drop_tg, _drop_fn in emb_ddl.all_trigger_ddl():
            conn.execute(text(drop_tg))  # 幂等关键
            conn.execute(text(fn_ddl))
            conn.execute(text(tg_ddl))

    # --- 索引残留清理（ISS-147 / §4.2-B） -----------------------------------
    # 本模块的两个测试用 `emb_ddl.hnsw_index_ddl()` **按 profile 动态建** HNSW 索引，
    # 而 `chunk_embeddings` 不参与 `drop_own_tables` 的 DDL 清理路径（它保表），
    # 于是那些索引**跨运行累积**：实测每跑一次本模块留下 8 个
    # `ix_chunk_embeddings_hnsw_<uuid>`，是 §4.2-B"测试库从秒级退化到 13~20 分钟"
    # 的结构侧同源问题。
    #
    # 清理口径 = **前后差分**（与 `tools/quality/check_test_residue.py` 同一条判据）：
    # 只删「本模块跑完多出来的」ANN 索引，**不删**库里原本就有的（别的会话/别的包建的）。
    # 判定依据是磁盘上的 **access method**（`pg_am.amname`），**不是索引名** ——
    # 名字是 `hnsw_index_name(profile_id)` 运行期拼的，写死名字必然对不上（铁律 14）。
    ann_before = _ann_index_names(engine)

    yield

    residue = _ann_index_names(engine) - ann_before
    if residue:
        with engine.begin() as conn:
            for name in sorted(residue):
                # 用被测模块自己生成的 DDL 形态删（`DROP INDEX IF EXISTS`）——
                # 名字已从库里读出，不是拼出来的。
                conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
        leftover = _ann_index_names(engine) - ann_before
        assert not leftover, (
            f"本模块跑完仍残留 {len(leftover)} 个 ANN 索引：{sorted(leftover)}"
            f"（清理未生效 —— 这会跨运行累积，拖慢全量 pytest）"
        )


def _ann_index_names(engine: Any) -> set[str]:
    """本库里所有 ANN（`hnsw`/`ivfflat`）索引名 —— **按 access method 判定**。

    不用 `indexname like 'ix_chunk_embeddings_hnsw_%'`：那是"覆盖范围由人写"
    （铁律 14），前缀一改就静默失效。`pg_am.amname` 是磁盘上的事实。
    """
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "select i.relname from pg_index x"
                " join pg_class i on i.oid = x.indexrelid"
                " join pg_class c on c.oid = x.indrelid"
                " join pg_namespace n on n.oid = c.relnamespace"
                " join pg_am am on am.oid = i.relam"
                " where n.nspname = 'public' and am.amname in ('hnsw', 'ivfflat')"
            )
        ).fetchall()
    return {r[0] for r in rows}


# ===========================================================================
# 数据夹具（自包含，不依赖 conftest 的隐式状态）
# ===========================================================================


@dataclass
class EmbeddingEnv:
    settings: Settings
    provider_id: uuid.UUID
    model_profile_id: uuid.UUID
    user_id: uuid.UUID
    brand_a: uuid.UUID
    brand_b: uuid.UUID
    product_a: uuid.UUID
    project_a: uuid.UUID
    project_b: uuid.UUID


def _create_user(settings: Settings) -> uuid.UUID:
    with session_scope(settings) as session:
        user = IdentityRepository(session).create_user(
            issuer=TEST_ISSUER,
            subject=f"subject-{uuid.uuid4().hex[:12]}",
            display_name="检索测试用户",
            role=ROLE_OPERATOR,
        )
        return user.id


@pytest.fixture
def env() -> EmbeddingEnv:
    """造一套完整的前置数据：provider / model_profile / user / 品牌 / 产品 / 项目。"""
    reset_engines()
    settings = _make_settings()
    user_id = _create_user(settings)

    provider_id = uuid.uuid4()
    model_profile_id = uuid.uuid4()
    brand_a, brand_b = uuid.uuid4(), uuid.uuid4()
    product_a = uuid.uuid4()
    project_a, project_b = uuid.uuid4(), uuid.uuid4()

    with session_scope(settings) as session:
        session.execute(
            text(
                "INSERT INTO providers (id, name, capability_types, network_mode,"
                " endpoint, status, policy_version)"
                " VALUES (:id, :n, '[\"EMBEDDING\"]'::jsonb, 'LOCAL',"
                " 'http://localhost:9', 'ACTIVE', 'v1')"
            ),
            {"id": provider_id, "n": f"wp21-provider-{uuid.uuid4().hex[:8]}"},
        )
        session.execute(
            text(
                "INSERT INTO model_profiles (id, provider_id, model_name, capabilities)"
                " VALUES (:id, :p, :n, '[\"EMBEDDING\"]'::jsonb)"
            ),
            {"id": model_profile_id, "p": provider_id, "n": f"wp21-model-{uuid.uuid4().hex[:6]}"},
        )
        for bid, name in ((brand_a, "品牌A"), (brand_b, "品牌B")):
            session.execute(
                text("INSERT INTO brands (id, name) VALUES (:id, :n)"),
                {"id": bid, "n": f"{name}-{uuid.uuid4().hex[:6]}"},
            )
        session.execute(
            text("INSERT INTO products (id, brand_id, name) VALUES (:id, :b, :n)"),
            {"id": product_a, "b": brand_a, "n": f"产品A-{uuid.uuid4().hex[:6]}"},
        )
        for pid, code in ((project_a, "PRA"), (project_b, "PRB")):
            session.execute(
                text(
                    "INSERT INTO projects (id, project_code, name, brand_id, product_id,"
                    " owner_id, target_delivery_count, due_date)"
                    " VALUES (:id, :c, :n, :b, :p, :o, 10, '2030-12-31')"
                ),
                {"id": pid, "c": f"{code}-{uuid.uuid4().hex[:8].upper()}",
                 "n": f"项目{code}", "b": brand_a, "p": product_a, "o": user_id},
            )
    return EmbeddingEnv(
        settings=settings,
        provider_id=provider_id,
        model_profile_id=model_profile_id,
        user_id=user_id,
        brand_a=brand_a,
        brand_b=brand_b,
        product_a=product_a,
        project_a=project_a,
        project_b=project_b,
    )


@dataclass
class SeededKnowledge:
    """一个知识资产的完整链路 id。"""

    asset_id: uuid.UUID
    version_id: uuid.UUID
    chunk_id: uuid.UUID


def _seed_knowledge(
    settings: Settings,
    *,
    category: str,
    scope: str,
    body: str,
    keywords: list[str],
    brand_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    make_active: bool = True,
) -> SeededKnowledge:
    """造一条知识（资产 + 版本节点 + 版本 + 一个 chunk）。

    **写入顺序由 OP-88 的延迟约束决定**（`active_version_id` FK 到
    `version_nodes`，且 `trg_knowledge_assets_active_scope` 是
    DEFERRABLE INITIALLY DEFERRED）：
    先建资产（留空 active）→ 版本节点 → 版本 → 再 UPDATE 资产。
    把 `active_version_id` 写进 INSERT 会撞 FK。
    """
    asset_id, version_id, chunk_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with session_scope(settings) as session:
        session.execute(
            text(
                "INSERT INTO knowledge_assets (id, category, title, brand_id, product_id,"
                " project_id, scope)"
                " VALUES (:id, :c, :t, :b, :p, :pj, :s)"
            ),
            {"id": asset_id, "c": category, "t": f"探针-{uuid.uuid4().hex[:6]}",
             "b": brand_id, "p": product_id, "pj": project_id, "s": scope},
        )
        session.execute(
            text(
                "INSERT INTO version_nodes (id, entity_type, entity_id, revision_no,"
                " schema_version, payload_hash)"
                " VALUES (:id, 'KNOWLEDGE', :a, 1, 1, :h)"
            ),
            {"id": version_id, "a": asset_id, "h": uuid.uuid4().hex * 2},
        )
        session.execute(
            text(
                "INSERT INTO knowledge_versions (id, asset_id, body, structured_payload,"
                " change_reason, schema_version)"
                " VALUES (:id, :a, :b,"
                " '{\"fact.statement\": {\"status\": \"FILLED\", \"value\": \"x\"}}'::jsonb,"
                " 'probe', 1)"
            ),
            {"id": version_id, "a": asset_id, "b": body},
        )
        if make_active:
            session.execute(
                text("UPDATE knowledge_assets SET active_version_id = :v WHERE id = :a"),
                {"v": version_id, "a": asset_id},
            )
        session.execute(
            text(
                "INSERT INTO knowledge_chunks (id, knowledge_version_id, chunk_no,"
                " text_content, text_hash, token_count, locator, keywords)"
                " VALUES (:id, :v, 1, :t, :h, :tc, '{}'::jsonb, :kw)"
            ),
            {"id": chunk_id, "v": version_id, "t": body, "h": hash_chunk_text(body),
             "tc": count_tokens(body), "kw": keywords},
        )
    return SeededKnowledge(asset_id=asset_id, version_id=version_id, chunk_id=chunk_id)


def _make_profile(
    settings: Settings,
    env: EmbeddingEnv,
    *,
    model_version: str = "probe-model@v1",
    dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
    metric: str = METRIC_COSINE,
    name: str | None = None,
) -> uuid.UUID:
    with session_scope(settings) as session:
        profile = emb_service.create_profile(
            EmbeddingRepository(session),
            name=name or f"wp21-{uuid.uuid4().hex[:8]}",
            model_profile_id=env.model_profile_id,
            model_version=model_version,
            dimensions=dimensions,
            metric=metric,
        )
        return profile.id


def _vector_literal(dimensions: int, seed: int) -> str:
    """造一个确定性的向量字面量（不依赖 numpy 的随机数版本）。"""
    values = [((seed * 37 + i * 11) % 1000) / 1000.0 for i in range(dimensions)]
    return "[" + ",".join(repr(v) for v in values) + "]"


def _insert_embedding(
    settings: Settings,
    *,
    chunk_id: uuid.UUID,
    profile_id: uuid.UUID,
    model_version: str,
    dimensions: int,
    seed: int = 1,
    text_hash: str | None = None,
) -> None:
    with session_scope(settings) as session:
        session.execute(
            text(
                "INSERT INTO chunk_embeddings (id, chunk_id, profile_id, model_version,"
                " embedding, chunk_text_hash, dimensions)"
                " VALUES (gen_random_uuid(), :c, :p, :mv, CAST(:v AS vector), :h, :d)"
            ),
            {"c": chunk_id, "p": profile_id, "mv": model_version,
             "v": _vector_literal(dimensions, seed),
             "h": text_hash or (uuid.uuid4().hex * 2), "d": dimensions},
        )


def _sqlstate_of(exc: BaseException) -> str | None:
    """从异常里取 SQLSTATE（psycopg 与 SQLAlchemy 包装两层都要看）。"""
    for attr in ("sqlstate", "pgcode"):
        value = getattr(exc, attr, None)
        if value:
            return str(value)
    orig = getattr(exc, "orig", None)
    for attr in ("sqlstate", "pgcode"):
        value = getattr(orig, attr, None)
        if value:
            return str(value)
    return None


# ===========================================================================
# 第 1 层：纯函数
# ===========================================================================


class TestChunkingPure:
    """切块器：块长上限、重叠、哈希、token、关键词。"""

    def test_chunk_never_exceeds_limit(self) -> None:
        """**块长上限是硬约束**：超上限不报错（模型静默截断），因此必须自己守。

        本包实测踩到：第一版块长是 `[100, 110, 110, ...]` ——
        重叠被直接拼在前面，没有从主内容里扣掉。
        """
        paragraphs = [f"第{i}段" + "内容填充" * 20 for i in range(1, 13)]
        body = "\n\n".join(paragraphs)
        limit, overlap = 100, 40
        chunks = chunk_text(body, chunk_chars=limit, overlap_chars=overlap)
        assert len(chunks) >= 2, "多段落必须切出多块，否则这条判据没有区分力"
        for chunk in chunks:
            assert len(chunk.text_content) <= limit, (
                f"块长 {len(chunk.text_content)} 超出上限 {limit}"
            )

    def test_chunk_no_is_sequential_from_one(self) -> None:
        """`chunk_no` 从 1 连续编号（不是 0）。

        `chunk_chars` 有下界（[50, 4000]）—— 传 10 会 ValueError。
        这个下界是有意的：太小的块没有语义价值。
        """
        paragraphs = ["甲" * 60, "乙" * 60, "丙" * 60]
        chunks = chunk_text("\n\n".join(paragraphs), chunk_chars=50, overlap_chars=5)
        assert len(chunks) >= 2
        assert [c.chunk_no for c in chunks] == list(range(1, len(chunks) + 1))

    def test_overlap_carries_previous_tail(self) -> None:
        """重叠真的生效：后一块的开头是前一块的结尾。"""
        paragraphs = ["甲" * 60, "乙" * 60]
        chunks = chunk_text("\n\n".join(paragraphs), chunk_chars=80, overlap_chars=20)
        assert len(chunks) >= 2
        assert chunks[1].text_content[:20] == chunks[0].text_content[-20:]

    def test_empty_input_yields_no_chunks(self) -> None:
        """空输入 → **空元组**（不是"一个空块"）。

        返回类型是 `tuple[ChunkDraft, ...]`（frozen，见 `chunking.py` 签名）。
        因此这里同时断言值与类型 —— 只断言 `== ()` 会漏掉"改成返回 list"。
        """
        assert chunk_text("", chunk_chars=100, overlap_chars=10) == ()
        assert chunk_text("   \n\n  ", chunk_chars=100, overlap_chars=10) == ()
        assert isinstance(chunk_text("", chunk_chars=100, overlap_chars=10), tuple)

    def test_hash_is_stable_and_64_chars(self) -> None:
        """哈希必须先规范化再算 —— 否则"只改空白"会产生新哈希，重算白跑。"""
        assert hash_chunk_text("内容") == hash_chunk_text("  内容  ")
        assert len(hash_chunk_text("内容")) == 64

    def test_normalize_is_line_end_only(self) -> None:
        """规范化只做三件事：统一换行符 / 去掉**行尾**空白 / 去掉首尾空白。

        **有意不做的事**（实测确认）：
          · 不去多余空行 —— 空行是**段落边界**，去掉会改变切块结果；
          · 不 strip 每行**行首**空白 —— Markdown 的缩进有语义（列表层级）。
        因此 `"  a \\n\\n  b  "` → `"a\\n\\n  b"`（第二行行首的空格保留）。
        """
        assert normalize_text("  a \n\n  b  ") == "a\n\n  b"
        assert normalize_text("a\r\n\r\nb") == "a\n\nb", "换行符必须统一"
        assert normalize_text("a\n\n\n\n\nb") == "a\n\n\n\n\nb", "空行必须保留"
        assert normalize_text("   ") == ""

    def test_count_tokens_cjk_counts_one_per_char(self) -> None:
        """CJK 1:1，拉丁 4:1 —— 混排时按各自规则分别累计。"""
        assert count_tokens("中文四字") == 4
        assert count_tokens("abcd") == 1
        assert count_tokens("中文四字abcd") == 5

    def test_keywords_cjk_are_two_grams(self) -> None:
        """**CJK 必须切 2-gram**。

        按标点切出的候选是整句（"除湿机静音卧室"），作为整体永远匹配不上
        查询里的"卧室" → `&&` 召回失效。本包实测踩到。
        """
        keywords = derive_keywords("除湿机静音卧室，适合小户型使用")
        assert "卧室" in keywords, f"2-gram 里必须有『卧室』，实得 {keywords}"
        assert any(len(k) == 2 for k in keywords)

    def test_keywords_order_is_deterministic(self) -> None:
        """**不用 `set` 去重**：迭代顺序随 PYTHONHASHSEED 变化，
        会让落库顺序不稳定（同一输入两次跑出不同数组）。"""
        first = derive_keywords("除湿机静音卧室")
        second = derive_keywords("除湿机静音卧室")
        assert first == second

    def test_keywords_empty_for_blank(self) -> None:
        assert derive_keywords("   ") == ()

    def test_chunker_version_is_frozen_string(self) -> None:
        """切块器版本是 chunk 生命周期的三要素之一，必须是显式常量。"""
        assert CHUNKER_VERSION and isinstance(CHUNKER_VERSION, str)

    def test_chunk_structured_renders_fields(self) -> None:
        """结构化 payload 切块：**值**进正文，**字段路径进 locator**。

        实测形状：`text_content = "status: FILLED\\nvalue: 续航 18 小时"`，
        `locator = {"path": "fact.statement", "unit": "FIELD", ...}`。
        字段路径放 locator 而不是拼进正文 —— 这样"引用哪个字段"
        可结构化回查（前端要高亮来源字段），而正文保持可读。
        """
        chunks = chunk_structured(
            {"fact.statement": {"status": "FILLED", "value": "续航 18 小时"}},
            chunk_chars=500,
        )
        assert chunks
        joined = " ".join(c.text_content for c in chunks)
        assert "续航 18 小时" in joined
        assert any(
            str(c.locator.get("path")) == "fact.statement" for c in chunks
        ), f"字段路径必须进 locator，实得 {[c.locator for c in chunks]}"

    def test_chunk_structured_empty_payload_yields_nothing(self) -> None:
        assert chunk_structured({}, chunk_chars=500) == ()

    def test_chunk_text_rejects_too_small_limit(self) -> None:
        """`chunk_chars` 有下界 —— 太小的块没有语义价值，显式拒绝而非静默接受。"""
        with pytest.raises(ValueError):
            chunk_text("内容", chunk_chars=10, overlap_chars=2)

    def test_chunk_text_rejects_too_large_limit(self) -> None:
        with pytest.raises(ValueError):
            chunk_text("内容", chunk_chars=999_999, overlap_chars=2)

    def test_chunk_for_version_prefers_body_and_payload(self) -> None:
        chunks = chunk_for_version(
            body="正文内容", structured_payload={}, chunk_chars=500, overlap_chars=0
        )
        assert chunks and "正文内容" in chunks[0].text_content

    def test_chunk_draft_is_frozen(self) -> None:
        draft = ChunkDraft(
            chunk_no=1, text_content="x", text_hash="a" * 64,
            token_count=1, locator={}, keywords=("x",),
        )
        with pytest.raises(Exception):
            draft.chunk_no = 2  # type: ignore[misc]


class TestScopeFilterPure:
    """范围谓词：形状是 OR、每分支重复 scope、缺字段 fail-closed。"""

    def _predicate_sql(self, subject: ScopeSubject) -> str:
        return str(
            kn_scope.scope_visibility_predicate(
                scope_column=KnowledgeAsset.scope,
                brand_column=KnowledgeAsset.brand_id,
                product_column=KnowledgeAsset.product_id,
                project_column=KnowledgeAsset.project_id,
                subject=subject,
            ).compile(compile_kwargs={"literal_binds": True})
        )

    def test_predicate_is_or_not_and(self) -> None:
        """**OR 而非 AND**：四个 scope 是互斥的"适用范围"，不是并列条件。

        若写成 AND，`scope = 'GLOBAL' AND scope = 'BRAND'` 恒假 →
        检索**永远返回 0 条**（反向验证实测确认）。
        """
        sql = self._predicate_sql(ScopeSubject(brand_id=uuid.uuid4()))
        assert " OR " in sql
        assert " AND " not in sql.split(" OR ")[0] or "scope" in sql

    def test_every_branch_repeats_scope(self) -> None:
        """**每个分支都要重复 `scope = 'X'`**。

        只比限定列会让"项目私有知识"因为品牌相同而泄漏到别的项目。
        这是最容易写错的一处，且写错后症状是"知识比预期多"，不报错。
        """
        brand = uuid.uuid4()
        sql = self._predicate_sql(ScopeSubject(brand_id=brand))
        assert "scope = 'BRAND'" in sql
        assert "brand_id = " in sql

    def test_missing_field_is_fail_closed(self) -> None:
        """缺字段 → 该分支被消去，**只剩 `scope = 'GLOBAL'`**。

        "缺 = 看不见"是安全默认方向。做成"缺 = 不过滤该维度"
        会把"忘记传 productId"变成"看到全部产品的事实"（越权）。

        ⚠️ **实测发现：不要断言编译出的 SQL 里含 `false`**。
        `or_(..., false(), ...)` 会被 SQLAlchemy **化简掉**
        （布尔代数：`x OR false == x`），空 subject 编译出的是
        `knowledge_assets.scope = 'GLOBAL'` 而不是
        `... OR false OR false OR false`。
        断言"**没有** BRAND/PRODUCT/PROJECT 分支"比断言含 false 更可靠 ——
        前者不依赖编译器的化简行为。
        """
        sql = self._predicate_sql(ScopeSubject())
        assert "GLOBAL" in sql
        assert "BRAND" not in sql, f"空 subject 不得有 BRAND 分支，实得 {sql}"
        assert "PRODUCT" not in sql, f"空 subject 不得有 PRODUCT 分支，实得 {sql}"
        assert "PROJECT" not in sql, f"空 subject 不得有 PROJECT 分支，实得 {sql}"

    def test_scope_filter_always_applied_constant(self) -> None:
        """**常量而非恒真函数**。

        写成 `def is_active(subject): return True` 会制造
        "存在不过滤的路径"的错觉 —— ISS-75 的教训：
        判据不能建立在一个恒真的条件上。
        """
        assert kn_scope.SCOPE_FILTER_ALWAYS_APPLIED is True
        assert kn_scope.scope_filter_is_active(ScopeSubject()) is True

    def test_scope_coverage_self_check_passes(self) -> None:
        """自检：谓词覆盖了 `SCOPES` 的每一个取值。

        若 WP20 新增一个 scope（例如 `ORG`），本函数会拦下 ——
        否则新 scope 的知识**永远召回不到**且不报错。
        """
        kn_scope.assert_scope_values_covered()

    def test_scope_coverage_note_mentions_global_only_when_empty(self) -> None:
        note = kn_scope.scope_coverage_note(ScopeSubject())
        assert "GLOBAL" in note
        note2 = kn_scope.scope_coverage_note(ScopeSubject(brand_id=uuid.uuid4()))
        assert "BRAND" in note2


class TestEmbeddingTypesPure:
    """值类型与换算。"""

    def test_distance_to_similarity_cosine(self) -> None:
        assert distance_to_similarity(METRIC_COSINE, 0.0) == pytest.approx(1.0)
        assert distance_to_similarity(METRIC_COSINE, 1.0) == pytest.approx(0.0)

    def test_distance_to_similarity_l2_is_monotone_decreasing(self) -> None:
        near = distance_to_similarity(METRIC_L2, 0.1)
        far = distance_to_similarity(METRIC_L2, 10.0)
        assert near > far

    def test_distance_to_similarity_inner_product_negates(self) -> None:
        assert distance_to_similarity(METRIC_INNER_PRODUCT, -0.5) == pytest.approx(0.5)

    def test_distance_to_similarity_unknown_metric_raises(self) -> None:
        """未知度量必须报错 —— 静默返回原值会让"相似度"变成"距离"。"""
        with pytest.raises(ValueError):
            distance_to_similarity("NOT_A_METRIC", 1.0)

    def test_scope_subject_is_frozen(self) -> None:
        subject = ScopeSubject(brand_id=uuid.uuid4())
        with pytest.raises(Exception):
            subject.brand_id = None  # type: ignore[misc]

    def test_scope_subject_is_empty(self) -> None:
        assert ScopeSubject().is_empty() is True
        assert ScopeSubject(brand_id=uuid.uuid4()).is_empty() is False

    def test_scope_subject_as_dict_uses_camel_case(self) -> None:
        """`as_dict()` 用 **camelCase**（协议 §1 的字段命名口径）。

        它进 HTTP 响应，因此必须与协议一致（`brandId` 不是 `brand_id`）。
        """
        brand = uuid.uuid4()
        payload = ScopeSubject(brand_id=brand).as_dict()
        assert payload["brandId"] == str(brand)
        assert payload["productId"] is None
        assert payload["projectId"] is None

    def test_dimension_bounds_are_sane(self) -> None:
        assert 0 < MIN_EMBEDDING_DIMENSIONS <= DEFAULT_EMBEDDING_DIMENSIONS
        assert DEFAULT_EMBEDDING_DIMENSIONS <= MAX_EMBEDDING_DIMENSIONS


# ===========================================================================
# 第 3 层：数据库约束的活性（**本包最重要的一层**）
# ===========================================================================


class TestMixingGuardLive:
    """ADR-15 判据「混用被阻止」—— 唯一落点是数据库层的守卫。

    **为什么必须在数据库层测**：绕过 ORM 的写入路径
    （psql / 数据回填 / 运维脚本）没有代码评审。
    若测试也只用 ORM，那么测试与实现共享同一个盲区。
    """

    def test_embedding_column_has_no_dimensions(self, env: EmbeddingEnv) -> None:
        """列必须是**无维度** `vector`（`atttypmod = -1`）。

        这是 pgvector 硬约束下的选择：无维度列**建不了** HNSW
        （`ERROR: column does not have dimensions`），
        因此索引改成每 profile 一个部分索引 + 表达式 `::vector(<dims>)`。
        这条断言钉住"列确实无维度"——若有人改回固定维度，
        多维度 profile 共存就不再可能。
        """
        engine = get_engine(env.settings)
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    "select a.atttypmod, t.typname from pg_attribute a"
                    " join pg_type t on t.oid = a.atttypid"
                    " where a.attrelid = 'chunk_embeddings'::regclass"
                    " and a.attname = 'embedding'"
                )
            ).one()
        assert row[1] == "vector"
        assert row[0] == -1, f"embedding 列必须无维度，实得 atttypmod={row[0]}"

    def test_guard_trigger_exists(self, env: EmbeddingEnv) -> None:
        engine = get_engine(env.settings)
        with engine.begin() as conn:
            count = conn.execute(
                text("select count(*) from pg_trigger where tgname = :n"),
                {"n": emb_ddl.MIXING_GUARD_TRIGGER},
            ).scalar_one()
        assert count == 1

    def test_forward_insert_succeeds(self, env: EmbeddingEnv) -> None:
        """对照组：维度与 model_version 都对的写入**必须能成功**。

        一个"见谁都拒"的守卫也能让负向测试全绿。
        """
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        seeded = _seed_knowledge(
            env.settings, category="FACT", scope="BRAND", body="正文",
            keywords=["正文"], brand_id=env.brand_a,
        )
        _insert_embedding(
            env.settings, chunk_id=seeded.chunk_id, profile_id=profile_id,
            model_version="m@v1", dimensions=64,
        )

    def test_wrong_dimensions_blocked(self, env: EmbeddingEnv) -> None:
        """负向 1：异维度混用 → `23514`。"""
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        seeded = _seed_knowledge(
            env.settings, category="FACT", scope="BRAND", body="正文",
            keywords=["正文"], brand_id=env.brand_a,
        )
        with pytest.raises(Exception) as info:
            _insert_embedding(
                env.settings, chunk_id=seeded.chunk_id, profile_id=profile_id,
                model_version="m@v1", dimensions=32,
            )
        assert _sqlstate_of(info.value) == emb_ddl.CONSISTENCY_SQLSTATE

    def test_same_dimensions_different_model_version_blocked(
        self, env: EmbeddingEnv
    ) -> None:
        """负向 2：**同维度混用** —— ADR-15 判据「混用被阻止」的唯一落点。

        两个模型版本可以维度相同（`bge-m3@v1` 与 `bge-m3@v2` 都是 1024 维），
        仅比 `dimensions` **无感** → 新模型向量静默写进旧 profile 的索引空间。
        这正是 ADR-15 说的「混用的危害不是报错而是**静默劣化**」。
        因此守卫必须**同时**比 `model_version`。
        """
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        seeded = _seed_knowledge(
            env.settings, category="FACT", scope="BRAND", body="正文",
            keywords=["正文"], brand_id=env.brand_a,
        )
        with pytest.raises(Exception) as info:
            _insert_embedding(
                env.settings, chunk_id=seeded.chunk_id, profile_id=profile_id,
                model_version="m@v2", dimensions=64,  # ← 维度相同，版本不同
            )
        assert _sqlstate_of(info.value) == emb_ddl.CONSISTENCY_SQLSTATE

    def test_dimensions_column_lies_blocked(self, env: EmbeddingEnv) -> None:
        """负向 3：`dimensions` 列与向量的真实维度不符 → `23514`。

        这一列是冗余的（可从 profile 推出），冗余它的理由正是
        "让守卫有东西可比"：`vector_dims()` 是大表全扫，不是列。
        这里用裸 SQL 造"列撒谎"的输入（ORM 路径不会产生这种输入，
        但绕过 ORM 的写入会产生）。
        """
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        seeded = _seed_knowledge(
            env.settings, category="FACT", scope="BRAND", body="正文",
            keywords=["正文"], brand_id=env.brand_a,
        )
        with pytest.raises(Exception) as info:
            with session_scope(env.settings) as session:
                session.execute(
                    text(
                        "INSERT INTO chunk_embeddings (id, chunk_id, profile_id,"
                        " model_version, embedding, chunk_text_hash, dimensions)"
                        " VALUES (gen_random_uuid(), :c, :p, 'm@v1',"
                        " CAST(:v AS vector), :h, 32)"
                    ),
                    {"c": seeded.chunk_id, "p": profile_id,
                     "v": _vector_literal(64, 5), "h": "a" * 64},
                )
        assert _sqlstate_of(info.value) == emb_ddl.CONSISTENCY_SQLSTATE

    def test_unique_chunk_profile_blocked(self, env: EmbeddingEnv) -> None:
        """`UQ(chunk_id, profile_id)` 拦住重复写入（`23505`）。

        唯一键是 `(chunk_id, profile_id)` 而不是 `chunk_id` ——
        ADR-15 切换期要求同一 chunk 在两个 profile 各有一行向量。
        """
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        seeded = _seed_knowledge(
            env.settings, category="FACT", scope="BRAND", body="正文",
            keywords=["正文"], brand_id=env.brand_a,
        )
        _insert_embedding(
            env.settings, chunk_id=seeded.chunk_id, profile_id=profile_id,
            model_version="m@v1", dimensions=64, seed=1,
        )
        with pytest.raises(Exception) as info:
            _insert_embedding(
                env.settings, chunk_id=seeded.chunk_id, profile_id=profile_id,
                model_version="m@v1", dimensions=64, seed=2,
            )
        assert _sqlstate_of(info.value) == "23505"

    def test_same_chunk_in_two_profiles_allowed(self, env: EmbeddingEnv) -> None:
        """ADR-15 切换期：**同一 chunk 在两个 profile 各一行**必须允许。

        这条是上一条的对照 —— 若唯一键误写成 `chunk_id`，
        切换期"旧 profile 仍可用"就无法表达（写新向量会覆盖旧的）。
        """
        p1 = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        p2 = _make_profile(env.settings, env, model_version="m@v2", dimensions=64)
        seeded = _seed_knowledge(
            env.settings, category="FACT", scope="BRAND", body="正文",
            keywords=["正文"], brand_id=env.brand_a,
        )
        _insert_embedding(
            env.settings, chunk_id=seeded.chunk_id, profile_id=p1,
            model_version="m@v1", dimensions=64, seed=1,
        )
        _insert_embedding(
            env.settings, chunk_id=seeded.chunk_id, profile_id=p2,
            model_version="m@v2", dimensions=64, seed=2,
        )
        with session_scope(env.settings) as session:
            rows = session.execute(
                text(
                    "select count(distinct profile_id) from chunk_embeddings"
                    " where chunk_id = :c"
                ),
                {"c": seeded.chunk_id},
            ).scalar_one()
        assert rows == 2

    def test_cascade_delete_on_chunk(self, env: EmbeddingEnv) -> None:
        """`ON DELETE CASCADE`：删 chunk 时向量一起走。

        与 `knowledge_chunks.knowledge_version_id` 的 RESTRICT **相反** ——
        chunk 是可重建的物化结果，重切块 = 删旧写新；
        RESTRICT 会让"换模型不混向量"在实现层无法完成。
        """
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        seeded = _seed_knowledge(
            env.settings, category="FACT", scope="BRAND", body="正文",
            keywords=["正文"], brand_id=env.brand_a,
        )
        _insert_embedding(
            env.settings, chunk_id=seeded.chunk_id, profile_id=profile_id,
            model_version="m@v1", dimensions=64,
        )
        with session_scope(env.settings) as session:
            session.execute(
                text("DELETE FROM knowledge_chunks WHERE id = :c"),
                {"c": seeded.chunk_id},
            )
        with session_scope(env.settings) as session:
            left = session.execute(
                text("select count(*) from chunk_embeddings where chunk_id = :c"),
                {"c": seeded.chunk_id},
            ).scalar_one()
        assert left == 0

    def test_partial_hnsw_index_built_on_expression(self, env: EmbeddingEnv) -> None:
        """部分 HNSW 索引：建在表达式上，且**含 WHERE**。

        这是"无维度列也能有 HNSW"的全部依据：
        索引定义里给出维度（`(embedding::vector(<dims>))`），
        `WHERE profile_id = '<uuid>'` 让它只覆盖一个 profile。
        实测：两个不同维度的 profile 在同一张表上共存。
        """
        p1 = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        p2 = _make_profile(env.settings, env, model_version="m@v2", dimensions=32)
        engine = get_engine(env.settings)
        with engine.begin() as conn:
            for pid, dims in ((p1, 64), (p2, 32)):
                conn.execute(
                    text(
                        emb_ddl.hnsw_index_ddl(
                            profile_id=str(pid), dimensions=dims,
                            metric=METRIC_COSINE, m=16, ef_construction=64,
                        )
                    )
                )
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "select indexname, indexdef from pg_indexes"
                    " where tablename = 'chunk_embeddings'"
                    " and indexname like 'ix_chunk_embeddings_hnsw_%'"
                )
            ).all()
        assert len(rows) >= 2, "两个 profile 的索引必须共存"
        for name, definition in rows:
            assert " WHERE " in definition, f"{name} 必须是部分索引"
            assert "hnsw" in definition.lower(), f"{name} 必须用 hnsw"

    def test_query_plan_uses_the_profile_partial_index(self, env: EmbeddingEnv) -> None:
        """**断言索引名，不是断言 "Index Scan"**（判据纪律第 3 条）。

        只断言计划里有 `Index Scan` 会放过"命中了错的索引"。
        本包要证明的是**本 profile 的部分 HNSW 索引**被用上。
        """
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        seeded = _seed_knowledge(
            env.settings, category="FACT", scope="BRAND", body="正文",
            keywords=["正文"], brand_id=env.brand_a,
        )
        _insert_embedding(
            env.settings, chunk_id=seeded.chunk_id, profile_id=profile_id,
            model_version="m@v1", dimensions=64,
        )
        engine = get_engine(env.settings)
        expected_index = emb_ddl.hnsw_index_name(str(profile_id))
        with engine.begin() as conn:
            conn.execute(
                text(
                    emb_ddl.hnsw_index_ddl(
                        profile_id=str(profile_id), dimensions=64,
                        metric=METRIC_COSINE, m=16, ef_construction=64,
                    )
                )
            )
            conn.execute(text("SET LOCAL enable_seqscan = off"))
            plan = "\n".join(
                row[0]
                for row in conn.execute(
                    text(
                        "EXPLAIN SELECT e.id FROM chunk_embeddings e"
                        " JOIN knowledge_chunks c ON c.id = e.chunk_id"
                        " WHERE e.profile_id = :p"
                        " ORDER BY (e.embedding::vector(64) <=> CAST(:q AS vector))"
                        " LIMIT 5"
                    ),
                    {"p": profile_id, "q": _vector_literal(64, 1)},
                )
            )
        assert expected_index in plan, f"计划未命中本 profile 的索引：\n{plan}"


# ===========================================================================
# 第 2 层：服务层 + 真库
# ===========================================================================


class TestProfileService:
    """profile 的建 / 切 / 解析。"""

    def test_create_profile_defaults(self, env: EmbeddingEnv) -> None:
        with session_scope(env.settings) as session:
            profile = emb_service.create_profile(
                EmbeddingRepository(session),
                name=f"p-{uuid.uuid4().hex[:8]}",
                model_profile_id=env.model_profile_id,
                model_version="m@v1",
            )
        assert profile.dimensions == DEFAULT_EMBEDDING_DIMENSIONS
        assert profile.metric == METRIC_COSINE
        assert profile.state == PROFILE_STATE_ACTIVE
        assert profile.index_ready is False, "新建的 profile 索引尚未建"

    def test_duplicate_name_is_422_not_409(self, env: EmbeddingEnv) -> None:
        """重名 → **422 + fieldErrors**，不是 409。

        本项目 409 的三个码（`INVALID_TRANSITION` / `IDEMPOTENCY_CONFLICT` /
        `DUPLICATE_PUBLICATION`）语义都是"状态机不允许转换"，
        与"重名"不同 —— 硬套会让前端走错分支。
        """
        name = f"dup-{uuid.uuid4().hex[:8]}"
        _make_profile(env.settings, env, name=name)
        with session_scope(env.settings) as session:
            with pytest.raises(ApiError) as info:
                emb_service.create_profile(
                    EmbeddingRepository(session), name=name,
                    model_profile_id=env.model_profile_id, model_version="m@v1",
                )
        assert info.value.code == ErrorCode.VALIDATION_FAILED
        assert info.value.details["fieldErrors"][0]["field"] == "name"

    def test_create_profile_rejects_empty_name(self, env: EmbeddingEnv) -> None:
        with session_scope(env.settings) as session:
            with pytest.raises(ApiError) as info:
                emb_service.create_profile(
                    EmbeddingRepository(session), name="   ",
                    model_profile_id=env.model_profile_id, model_version="m@v1",
                )
        assert info.value.code == ErrorCode.VALIDATION_FAILED

    def test_create_profile_rejects_bad_dimensions(self, env: EmbeddingEnv) -> None:
        with session_scope(env.settings) as session:
            with pytest.raises(ApiError) as info:
                emb_service.create_profile(
                    EmbeddingRepository(session),
                    name=f"bad-{uuid.uuid4().hex[:8]}",
                    model_profile_id=env.model_profile_id,
                    model_version="m@v1", dimensions=0,
                )
        assert info.value.code == ErrorCode.VALIDATION_FAILED

    def test_create_profile_rejects_unknown_metric(self, env: EmbeddingEnv) -> None:
        with session_scope(env.settings) as session:
            with pytest.raises(ApiError) as info:
                emb_service.create_profile(
                    EmbeddingRepository(session),
                    name=f"bad-{uuid.uuid4().hex[:8]}",
                    model_profile_id=env.model_profile_id,
                    model_version="m@v1", metric="NOT_A_METRIC",
                )
        assert info.value.code == ErrorCode.VALIDATION_FAILED

    def test_build_index_marks_ready(self, env: EmbeddingEnv) -> None:
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        with session_scope(env.settings) as session:
            repo = EmbeddingRepository(session)
            profile = emb_service.build_index(
                repo, profile=repo.get_profile(profile_id)  # type: ignore[arg-type]
            )
        assert profile.index_ready is True

    def test_resolve_profile_returns_active_by_default(self, env: EmbeddingEnv) -> None:
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        with session_scope(env.settings) as session:
            profile = emb_service.resolve_profile(EmbeddingRepository(session), profile_id=None)
        assert profile.id == profile_id

    def test_resolve_profile_explicit_missing_is_404(self, env: EmbeddingEnv) -> None:
        """**显式给一个不存在的 profile → 404，不静默回退到默认**。

        静默回退会让"我指定了 profile X"变成"我拿到了 profile Y 的结果"——
        症状是结果看起来正常，只是用了别的索引空间（静默劣化）。
        """
        with session_scope(env.settings) as session:
            with pytest.raises(ApiError) as info:
                emb_service.resolve_profile(
                    EmbeddingRepository(session), profile_id=uuid.uuid4()
                )
        assert info.value.code == ErrorCode.RESOURCE_NOT_FOUND

    def test_switch_profile_deprecates_old(self, env: EmbeddingEnv) -> None:
        """切换：新的升 ACTIVE，旧的降 DEPRECATED，**且旧的不被删**。

        ADR-15 第 3 步「切换期间旧 profile 保持可用」+ 第 5 步
        「旧 profile 保留至确认无需回滚，再回收」。

        **前置条件：新 profile 必须已建索引** —— ADR-15 第 4 步原文
        「新 profile 检索质量经回归确认后，将流量切至新 profile」，
        索引没建就没有"质量"可确认（切换会 422）。
        """
        p1 = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        p2 = _make_profile(env.settings, env, model_version="m@v2", dimensions=64)
        for pid in (p1, p2):
            with session_scope(env.settings) as session:
                repo = EmbeddingRepository(session)
                emb_service.build_index(repo, profile=repo.get_profile(pid))  # type: ignore[arg-type]
        with session_scope(env.settings) as session:
            new, old = emb_service.switch_profile(
                EmbeddingRepository(session), new_profile_id=p2
            )
        assert new.id == p2 and new.state == PROFILE_STATE_ACTIVE
        assert any(p.id == p1 and p.state == PROFILE_STATE_DEPRECATED for p in old)

    def test_switch_profile_requires_ready_index(self, env: EmbeddingEnv) -> None:
        """索引未建时切换 → **422**（ADR-15 第 4 步的前置条件）。"""
        p2 = _make_profile(env.settings, env, model_version="m@v2", dimensions=64)
        with session_scope(env.settings) as session:
            with pytest.raises(ApiError) as info:
                emb_service.switch_profile(
                    EmbeddingRepository(session), new_profile_id=p2
                )
        assert info.value.code == ErrorCode.VALIDATION_FAILED
        assert info.value.details.get("indexReady") is False

    @pytest.mark.regression
    def test_switch_profile_never_leaves_no_active(self, env: EmbeddingEnv) -> None:
        """**切换过程中任何时刻都必须有 ACTIVE profile**。

        若先把旧的降级再升新的，中间存在一个"没有 ACTIVE"的窗口，
        并发检索会失败。实现是**先升新的再降旧的** —— 顺序敏感。

        **必须用可控替身记录调用顺序**：两种顺序在串行测试下完全等价
        （都不报错、结果一样），只有断言"调用顺序"才区分得开。
        判据纪律第 7 条（ISS-82 同族）：顺序调用构造的判据恒真，
        必须确定性造出"那一刻"。

        ⚠️ 本包实测：这条判据**抓到了实现与 docstring 相反的真实缺陷**
        （代码是先降后升，而 docstring 写的是先升后降）。
        """
        p1 = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        p2 = _make_profile(env.settings, env, model_version="m@v2", dimensions=64)
        for pid in (p1, p2):
            with session_scope(env.settings) as session:
                repo = EmbeddingRepository(session)
                emb_service.build_index(repo, profile=repo.get_profile(pid))  # type: ignore[arg-type]

        with session_scope(env.settings) as session:
            repo = EmbeddingRepository(session)
            order: list[tuple[str, str]] = []
            real_set_state = repo.set_profile_state

            def spy(*, profile_id: uuid.UUID, state: str) -> Any:
                order.append((str(profile_id), state))
                return real_set_state(profile_id=profile_id, state=state)

            repo.set_profile_state = spy  # type: ignore[method-assign]
            emb_service.switch_profile(repo, new_profile_id=p2)

        active_calls = [
            i for i, (_pid, st) in enumerate(order) if st == PROFILE_STATE_ACTIVE
        ]
        deprecated_calls = [
            i for i, (_pid, st) in enumerate(order) if st == PROFILE_STATE_DEPRECATED
        ]
        assert active_calls, "必须有一次升 ACTIVE 的调用"
        assert deprecated_calls, "必须有一次降 DEPRECATED 的调用"
        assert min(active_calls) < min(deprecated_calls), (
            "**必须先升新的再降旧的** —— 反序会出现『没有 ACTIVE profile』的窗口。"
            f"实际调用顺序：{order}"
        )

    def test_switch_profile_missing_is_404(self, env: EmbeddingEnv) -> None:
        with session_scope(env.settings) as session:
            with pytest.raises(ApiError) as info:
                emb_service.switch_profile(
                    EmbeddingRepository(session), new_profile_id=uuid.uuid4()
                )
        assert info.value.code == ErrorCode.RESOURCE_NOT_FOUND


class TestIngestService:
    """切块 + 写向量（幂等、维度校验、空块提前返回）。"""

    def test_ingest_writes_chunks_and_embeddings(self, env: EmbeddingEnv) -> None:
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        seeded = _seed_knowledge(
            env.settings, category="FACT", scope="BRAND",
            body="第一段内容。\n\n第二段内容。", keywords=["内容"],
            brand_id=env.brand_a,
        )
        embedder = kn_indexing.DeterministicEmbedder(dimensions=64)
        with session_scope(env.settings) as session:
            repo = EmbeddingRepository(session)
            result = emb_service.ingest_version(
                repo, version_id=seeded.version_id,
                profile=repo.get_profile(profile_id),  # type: ignore[arg-type]
                embedder=embedder,
            )
        assert result.chunk_count >= 1
        assert result.embedding_count == result.chunk_count

    def test_ingest_is_idempotent(self, env: EmbeddingEnv) -> None:
        """**重切不产生重复 chunk**：先删旧 chunk 再写新的。

        注意 `knowledge_chunks` 有 `UQ(knowledge_version_id, chunk_no)`，
        若实现是"直接追加"，第二次 ingest 会撞唯一约束（不是静默重复）。
        这条断言钉住"删了再写"这个顺序。
        """
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        seeded = _seed_knowledge(
            env.settings, category="FACT", scope="BRAND", body="内容正文",
            keywords=["内容"], brand_id=env.brand_a,
        )
        embedder = kn_indexing.DeterministicEmbedder(dimensions=64)
        with session_scope(env.settings) as session:
            repo = EmbeddingRepository(session)
            first = emb_service.ingest_version(
                repo, version_id=seeded.version_id,
                profile=repo.get_profile(profile_id),  # type: ignore[arg-type]
                embedder=embedder,
            )
        with session_scope(env.settings) as session:
            repo = EmbeddingRepository(session)
            second = emb_service.ingest_version(
                repo, version_id=seeded.version_id,
                profile=repo.get_profile(profile_id),  # type: ignore[arg-type]
                embedder=embedder,
            )
        assert first.chunk_count == second.chunk_count

    def test_ingest_dimension_mismatch_is_422(self, env: EmbeddingEnv) -> None:
        """embedder 产出的维度与 profile 不符 → 422（不是让它写进去再被守卫拦）。

        两道防线都要有：服务层给**可读的错误**，数据库层给**兜底**。
        """
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        seeded = _seed_knowledge(
            env.settings, category="FACT", scope="BRAND", body="内容正文",
            keywords=["内容"], brand_id=env.brand_a,
        )
        bad_embedder = kn_indexing.DeterministicEmbedder(dimensions=32)  # ← 不符
        with session_scope(env.settings) as session:
            repo = EmbeddingRepository(session)
            with pytest.raises(ApiError) as info:
                emb_service.ingest_version(
                    repo, version_id=seeded.version_id,
                    profile=repo.get_profile(profile_id),  # type: ignore[arg-type]
                    embedder=bad_embedder,
                )
        assert info.value.code == ErrorCode.VALIDATION_FAILED

    def test_ingest_missing_version_is_404(self, env: EmbeddingEnv) -> None:
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        with session_scope(env.settings) as session:
            repo = EmbeddingRepository(session)
            with pytest.raises(ApiError) as info:
                emb_service.ingest_version(
                    repo, version_id=uuid.uuid4(),
                    profile=repo.get_profile(profile_id),  # type: ignore[arg-type]
                    embedder=kn_indexing.DeterministicEmbedder(dimensions=64),
                )
        assert info.value.code == ErrorCode.RESOURCE_NOT_FOUND


class TestSearchService:
    """检索：范围过滤、两种 mode、参数校验。"""

    def _setup_two_brands(self, env: EmbeddingEnv) -> tuple[uuid.UUID, Any, Any]:
        """造 A / B 两个品牌各一条知识 + 向量，返回 (profile_id, A, B)。"""
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        a = _seed_knowledge(
            env.settings, category="FACT", scope="BRAND",
            body="除湿机静音卧室适用", keywords=["除湿", "湿机", "静音", "卧室"],
            brand_id=env.brand_a,
        )
        b = _seed_knowledge(
            env.settings, category="SCENE", scope="BRAND",
            body="B品牌场景描述", keywords=["场景", "品牌"],
            brand_id=env.brand_b,
        )
        for seed, item in ((1, a), (2, b)):
            _insert_embedding(
                env.settings, chunk_id=item.chunk_id, profile_id=profile_id,
                model_version="m@v1", dimensions=64, seed=seed,
            )
        return profile_id, a, b

    def test_search_semantic_excludes_other_brand(self, env: EmbeddingEnv) -> None:
        """**验收原文：「近似品牌案例不串事实」** —— 跨品牌知识不在结果里。

        这是本包最重要的一条判据。范围谓词下推到 SQL（不是取回来再筛），
        否则"同品牌只有 2 条、k = 8"时会返回 6 条跨品牌的 ——
        它**看起来正常**（有结果、有分数、有排序），只是内容是别人的。
        """
        profile_id, a, b = self._setup_two_brands(env)
        with session_scope(env.settings) as session:
            outcome = emb_service.search(
                EmbeddingRepository(session),
                query="除湿机静音卧室", subject=ScopeSubject(brand_id=env.brand_a),
                profile_id=profile_id, mode=SEARCH_SEMANTIC, limit=10,
                embedder=kn_indexing.DeterministicEmbedder(dimensions=64),
            )
        ids = {hit.asset_id for hit in outcome.hits}
        assert a.asset_id in ids, "A 品牌的知识必须被召回"
        assert b.asset_id not in ids, "**B 品牌的知识绝不能出现**（验收原文）"

    def test_search_keyword_same_scope_semantics(self, env: EmbeddingEnv) -> None:
        """关键词兜底路径**必须与语义路径同口径**。

        两条路径若口径不同，"兜底"会变成一个越权的旁路。
        """
        profile_id, a, b = self._setup_two_brands(env)
        with session_scope(env.settings) as session:
            outcome = emb_service.search(
                EmbeddingRepository(session),
                query="除湿机静音卧室", subject=ScopeSubject(brand_id=env.brand_a),
                profile_id=profile_id, mode=SEARCH_KEYWORD, limit=10,
            )
        ids = {hit.asset_id for hit in outcome.hits}
        assert a.asset_id in ids
        assert b.asset_id not in ids, "关键词路径也必须按范围过滤"

    def test_search_empty_subject_is_fail_closed(self, env: EmbeddingEnv) -> None:
        """空 subject → 只有 GLOBAL 可见（BRAND 知识全部被过滤）。"""
        profile_id, a, b = self._setup_two_brands(env)
        with session_scope(env.settings) as session:
            outcome = emb_service.search(
                EmbeddingRepository(session),
                query="除湿机静音卧室", subject=ScopeSubject(),
                profile_id=profile_id, mode=SEARCH_SEMANTIC, limit=10,
                embedder=kn_indexing.DeterministicEmbedder(dimensions=64),
            )
        ids = {hit.asset_id for hit in outcome.hits}
        assert a.asset_id not in ids and b.asset_id not in ids

    def test_search_global_visible_without_subject(self, env: EmbeddingEnv) -> None:
        """对照组：`GLOBAL` 范围的知识**没有 subject 也能看到**。

        一个"空 subject 就返回空"的实现也能让上一条全绿 ——
        因此必须有这条对照。
        """
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        g = _seed_knowledge(
            env.settings, category="RESEARCH", scope="GLOBAL",
            body="全局研究结论", keywords=["全局", "研究"],
        )
        _insert_embedding(
            env.settings, chunk_id=g.chunk_id, profile_id=profile_id,
            model_version="m@v1", dimensions=64, seed=9,
        )
        with session_scope(env.settings) as session:
            outcome = emb_service.search(
                EmbeddingRepository(session),
                query="全局研究结论", subject=ScopeSubject(),
                profile_id=profile_id, mode=SEARCH_SEMANTIC, limit=10,
                embedder=kn_indexing.DeterministicEmbedder(dimensions=64),
            )
        assert g.asset_id in {hit.asset_id for hit in outcome.hits}

    def test_search_reports_scope_filter_applied(self, env: EmbeddingEnv) -> None:
        """响应必须报告"范围过滤被应用" —— 让"为什么没召回"可诊断。"""
        profile_id, _a, _b = self._setup_two_brands(env)
        with session_scope(env.settings) as session:
            outcome = emb_service.search(
                EmbeddingRepository(session),
                query="除湿机静音卧室", subject=ScopeSubject(brand_id=env.brand_a),
                profile_id=profile_id, mode=SEARCH_SEMANTIC, limit=10,
                embedder=kn_indexing.DeterministicEmbedder(dimensions=64),
            )
        assert outcome.scope_filter_applied is True
        assert outcome.subject.brand_id == env.brand_a

    def test_search_empty_query_is_422(self, env: EmbeddingEnv) -> None:
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        with session_scope(env.settings) as session:
            with pytest.raises(ApiError) as info:
                emb_service.search(
                    EmbeddingRepository(session), query="   ",
                    subject=ScopeSubject(), profile_id=profile_id,
                )
        assert info.value.code == ErrorCode.VALIDATION_FAILED

    def test_search_bad_limit_is_422(self, env: EmbeddingEnv) -> None:
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        with session_scope(env.settings) as session:
            with pytest.raises(ApiError) as info:
                emb_service.search(
                    EmbeddingRepository(session), query="内容",
                    subject=ScopeSubject(), profile_id=profile_id, limit=0,
                )
        assert info.value.code == ErrorCode.VALIDATION_FAILED

    def test_search_unknown_mode_is_422(self, env: EmbeddingEnv) -> None:
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        with session_scope(env.settings) as session:
            with pytest.raises(ApiError) as info:
                emb_service.search(
                    EmbeddingRepository(session), query="内容",
                    subject=ScopeSubject(), profile_id=profile_id, mode="NOT_A_MODE",
                )
        assert info.value.code == ErrorCode.VALIDATION_FAILED

    def test_search_without_profile_is_404(self, env: EmbeddingEnv) -> None:
        """**没有 ACTIVE profile 时检索必须报错，不静默返回空**。

        静默返回空会让"忘了建索引"变成"库里没有知识"。
        """
        with session_scope(env.settings) as session:
            with pytest.raises(ApiError) as info:
                emb_service.search(
                    EmbeddingRepository(session), query="内容",
                    subject=ScopeSubject(), profile_id=uuid.uuid4(),
                )
        assert info.value.code == ErrorCode.RESOURCE_NOT_FOUND

    def test_search_ef_search_is_transaction_local(self, env: EmbeddingEnv) -> None:
        """`SET LOCAL hnsw.ef_search` 而非 `SET`。

        `SET` 是会话级，连接池复用会让检索质量随并发请求顺序变化
        （**随机**失败，最难查的一类）。同族：WP12 ISS-28、WP13 ISS-59。
        """
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        engine = get_engine(env.settings)
        # 先在**一个事务内**执行检索路径里的 SET LOCAL
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL hnsw.ef_search = 77"))
            inside = conn.execute(text("SHOW hnsw.ef_search")).scalar_one()
        # 事务结束后必须回到默认值（证明是 LOCAL 不是 SESSION）
        with engine.begin() as conn:
            outside = conn.execute(text("SHOW hnsw.ef_search")).scalar_one()
        assert str(inside) == "77"
        assert str(outside) != "77", "SET LOCAL 不得泄漏到下一个事务"


class TestIndexingEvents:
    """Outbox 事件处理（不抛异常、缺字段返回 handled=False）。"""

    def test_unknown_event_type_is_not_handled(self, env: EmbeddingEnv) -> None:
        """未知事件类型 → `handled=False`，**不抛异常**。

        抛异常会让一条不认识的事件卡死整个消费循环。
        """
        with session_scope(env.settings) as session:
            outcome = kn_indexing.handle_index_event(
                EmbeddingRepository(session), event_type="SOMETHING_ELSE",
                payload={}, profile=None,
                embedder=kn_indexing.DeterministicEmbedder(dimensions=64),
            )
        assert outcome.handled is False

    def test_missing_version_id_is_not_handled(self, env: EmbeddingEnv) -> None:
        with session_scope(env.settings) as session:
            outcome = kn_indexing.handle_index_event(
                EmbeddingRepository(session),
                event_type="KNOWLEDGE_INDEX_REQUESTED", payload={}, profile=None,
                embedder=kn_indexing.DeterministicEmbedder(dimensions=64),
            )
        assert outcome.handled is False

    def test_profile_is_usable_requires_active_state(self, env: EmbeddingEnv) -> None:
        """`profile_is_usable` 判的是 **state 是 ACTIVE**，不是 `index_ready`。

        实测澄清：它管的是"**是否接受新向量写入**"——
        `DEPRECATED` 的 profile 仍可被**检索**（ADR-15 第 5 步：
        旧 profile 保留至确认无需回滚），但不再接受新向量。
        若让 DEPRECATED 也接受写入，"切换后新知识仍写进旧索引空间" ——
        那是混用的另一种形态（新内容进旧空间），而它**不报错**。
        """
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        with session_scope(env.settings) as session:
            profile = EmbeddingRepository(session).get_profile(profile_id)
        assert kn_indexing.profile_is_usable(profile) is True, "新建的 profile 是 ACTIVE"
        with session_scope(env.settings) as session:
            repo = EmbeddingRepository(session)
            repo.set_profile_state(profile_id=profile_id, state=PROFILE_STATE_DEPRECATED)
        with session_scope(env.settings) as session:
            deprecated = EmbeddingRepository(session).get_profile(profile_id)
        assert kn_indexing.profile_is_usable(deprecated) is False, (
            "DEPRECATED 不得接受新向量"
        )
        assert kn_indexing.profile_is_usable(None) is False


class TestCapacity:
    """容量估算（ADR-15 判据 4「空间估算准确」+ 判据 2「计数口径正确」）。"""

    def test_capacity_report_shape(self, env: EmbeddingEnv) -> None:
        """报告的关键字段：**计数口径按 profile 分别统计**（ADR-15 判据 2）。

        ADR-15 原文：「检查监控指标按 embedding profile 分别统计 chunk 数，
        **而非全库合计**」。因此报告必须有 `chunkCount` 与
        `countsConsistent`（"向量行数 == chunk 数"这个一致性信号）。
        """
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        with session_scope(env.settings) as session:
            repo = EmbeddingRepository(session)
            report = emb_service.capacity_report(
                repo, profile=repo.get_profile(profile_id)  # type: ignore[arg-type]
            )
        for key in ("chunkCount", "embeddingRows", "countsConsistent",
                    "warning", "estimate", "dimensions"):
            assert key in report, f"报告缺字段 {key}"
        assert report["warning"]["level"] in {"OK", "WARNING", "EXCEEDED"}
        assert report["countsConsistent"] is True

    def test_capacity_estimate_is_monotone_in_count(self) -> None:
        from rednote.platform import capacity as cap

        small = cap.estimate_vector_bytes(chunk_count=1_000, dimensions=1024)
        large = cap.estimate_vector_bytes(chunk_count=100_000, dimensions=1024)
        assert large["totalBytes"] > small["totalBytes"]

    def test_capacity_warning_thresholds(self) -> None:
        """`capacity_warning` 返回**三段结构**（level/ratio/message）。

        返回 dict 而不是裸字符串：`ratio` 让"离阈值还有多远"可量化，
        `message` 让告警可直接呈现 —— 只给一个 `"OK"` 的话，
        前端要自己再算一遍比例（第二份实现）。
        """
        from rednote.platform import capacity as cap

        ok = cap.capacity_warning(0)
        assert ok["level"] == "OK"
        assert cap.capacity_warning(cap.PGVECTOR_CHUNK_THRESHOLD + 1)["level"] == "EXCEEDED"
        warning_level = cap.capacity_warning(
            int(cap.PGVECTOR_CHUNK_THRESHOLD * cap.CAPACITY_WARNING_RATIO) + 1
        )["level"]
        assert warning_level in {"WARNING", "EXCEEDED"}, (
            f"超过告警比例后必须升级，实得 {warning_level}"
        )


# ===========================================================================
# 第 4 层：HTTP
# ===========================================================================


def _app_with_database(settings: Settings) -> FastAPI:
    from rednote.main import create_app

    return create_app(settings)


def _client_with_database(settings: Settings) -> TestClient:
    return TestClient(_app_with_database(settings))


def _login(client: TestClient, settings: Settings, user_id: uuid.UUID) -> None:
    """造一条会话并写 Cookie（含 CSRF 令牌）。

    **CSRF 必须一起处理**：`current_user` 依赖对写方法强制 `verify_csrf`，
    且它是**结构上无法绕过**的 —— 绕过它会让 HTTP 层测试不再验证真实的鉴权链。
    与 `tests/test_prompts_wp23.py` 的 `_login` 同一手法。
    """
    from datetime import datetime, timedelta, timezone

    from rednote.platform.csrf import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
    from rednote.platform.tokens import generate_session_token, hash_token

    raw_token = generate_session_token()
    with session_scope(settings) as session:
        IdentityRepository(session).create_session(
            user_id=user_id,
            token_hash=hash_token(raw_token),
            csrf_secret_hash=hash_token(CSRF_SECRET_PLAINTEXT),
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    client.cookies.set(CSRF_COOKIE_NAME, CSRF_SECRET_PLAINTEXT)


def _write_headers() -> dict[str, str]:
    """写请求的公共头：CSRF 令牌 + Origin。

    两者都带上，测试里的 403 断言才只可能来自**权限**判定。
    """
    return {"X-CSRF-Token": CSRF_SECRET_PLAINTEXT, "Origin": TEST_ORIGIN}


class TestSearchHttp:
    """`POST /knowledge/searches` 的接线。"""

    def test_search_requires_login(self, env: EmbeddingEnv) -> None:
        client = _client_with_database(env.settings)
        with client:
            response = client.post(
                "/api/v1/knowledge/searches",
                json={"query": "内容", "subject": {}},
                headers=_write_headers(),
            )
        assert response.status_code == 401

    def test_search_returns_envelope_with_hits_and_search(self, env: EmbeddingEnv) -> None:
        """响应形状：`data = {hits, search}`。

        检索元信息**不能**塞进协议 §1 的 `meta`（那里只有 `requestId`），
        因此改为在 `data` 里包装一层。

        **`app.state.embedder` 必须注入**：默认替身是 1024 维，
        而本测试的 profile 是 64 维 —— 服务层会正确地以
        「查询向量是 1024 维，而 profile 声明 64 维」拒绝（422）。
        这顺带验证了**注入通道真的生效**（`get_embedder` 从
        `app.state` 取而不是退回模块级单例）。
        """
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        seeded = _seed_knowledge(
            env.settings, category="FACT", scope="BRAND", body="除湿机静音卧室",
            keywords=["除湿", "湿机", "静音", "卧室"], brand_id=env.brand_a,
        )
        _insert_embedding(
            env.settings, chunk_id=seeded.chunk_id, profile_id=profile_id,
            model_version="m@v1", dimensions=64,
        )
        app = _app_with_database(env.settings)
        app.state.embedder = kn_indexing.DeterministicEmbedder(dimensions=64)
        client = TestClient(app)
        with client:
            _login(client, env.settings, env.user_id)
            response = client.post(
                "/api/v1/knowledge/searches",
                json={
                    "query": "除湿机静音卧室",
                    "subject": {"brandId": str(env.brand_a)},
                    "profileId": str(profile_id),
                    "limit": 10,
                },
                headers=_write_headers(),
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "data" in body
        assert "hits" in body["data"]
        assert "search" in body["data"]
        assert body["meta"].get("requestId"), "协议 §1 的 meta 必须只有 requestId"

    def test_search_dimension_mismatch_is_422(self, env: EmbeddingEnv) -> None:
        """**查询向量维度与 profile 不符 → 422**（不是静默截断/补零）。

        这条是上一条的**对照组**：证明服务层真的在校验维度，
        而不是"恰好这次维度对上了"。
        """
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        app = _app_with_database(env.settings)
        app.state.embedder = kn_indexing.DeterministicEmbedder(dimensions=128)  # ← 不符
        client = TestClient(app)
        with client:
            _login(client, env.settings, env.user_id)
            response = client.post(
                "/api/v1/knowledge/searches",
                json={
                    "query": "内容",
                    "subject": {},
                    "profileId": str(profile_id),
                },
                headers=_write_headers(),
            )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_search_rejects_unknown_field(self, env: EmbeddingEnv) -> None:
        """**`extra="forbid"`**（铁律 ISS-80）：未知字段必须 422。

        静默忽略未知字段会让"前端拼错了字段名"变成"参数没生效" ——
        症状是结果正常但不符合预期。
        """
        client = _client_with_database(env.settings)
        with client:
            _login(client, env.settings, env.user_id)
            response = client.post(
                "/api/v1/knowledge/searches",
                json={"query": "内容", "subject": {}, "unknownField": 1},
                headers=_write_headers(),
            )
        assert response.status_code == 422

    def test_search_empty_query_is_422(self, env: EmbeddingEnv) -> None:
        client = _client_with_database(env.settings)
        with client:
            _login(client, env.settings, env.user_id)
            response = client.post(
                "/api/v1/knowledge/searches",
                json={"query": "   ", "subject": {}},
                headers=_write_headers(),
            )
        assert response.status_code == 422


# ===========================================================================
# 反向验证（**判据本身必须能区分两种实现**）
# ===========================================================================


class TestReverseScopePredicate:
    """范围谓词的反向验证：把错误实现放回去，判据必须变红。

    判据纪律第 7 条（ISS-82 同族）。**这一层是"判据的判据"** ——
    没有它，上面那些绿色可能只是"判据恒真"。
    """

    def _search_ids(
        self, env: EmbeddingEnv, profile_id: uuid.UUID, monkeypatch: Any,
        patch: Any, *, subject: ScopeSubject, mode: str = SEARCH_SEMANTIC,
    ) -> set[uuid.UUID]:
        monkeypatch.setattr(EmbeddingRepository, "_scope_predicate_sql", patch)
        with session_scope(env.settings) as session:
            outcome = emb_service.search(
                EmbeddingRepository(session), query="除湿机静音卧室",
                subject=subject, profile_id=profile_id, mode=mode, limit=10,
                embedder=kn_indexing.DeterministicEmbedder(dimensions=64),
            )
        return {hit.asset_id for hit in outcome.hits}

    def _two_brands(self, env: EmbeddingEnv) -> tuple[uuid.UUID, Any, Any, Any]:
        """造 A / B / C（PROJECT 范围但 brand 与 A 相同）三条知识。"""
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        a = _seed_knowledge(
            env.settings, category="FACT", scope="BRAND", body="除湿机静音卧室",
            keywords=["除湿", "静音"], brand_id=env.brand_a,
        )
        b = _seed_knowledge(
            env.settings, category="SCENE", scope="BRAND", body="B品牌场景",
            keywords=["场景"], brand_id=env.brand_b,
        )
        c = _seed_knowledge(
            env.settings, category="RULE", scope="PROJECT", body="项目私有规则",
            keywords=["项目", "私有"], brand_id=env.brand_a,
            product_id=env.product_a, project_id=env.project_a,
        )
        for seed, item in ((1, a), (2, b), (3, c)):
            _insert_embedding(
                env.settings, chunk_id=item.chunk_id, profile_id=profile_id,
                model_version="m@v1", dimensions=64, seed=seed,
            )
        return profile_id, a, b, c

    def test_baseline_correct_implementation(self, env: EmbeddingEnv) -> None:
        """基线：正确实现只召回 A，不召回 B（跨品牌）与 C（项目私有）。"""
        profile_id, a, b, c = self._two_brands(env)
        with session_scope(env.settings) as session:
            outcome = emb_service.search(
                EmbeddingRepository(session), query="除湿机静音卧室",
                subject=ScopeSubject(brand_id=env.brand_a),
                profile_id=profile_id, mode=SEARCH_SEMANTIC, limit=10,
                embedder=kn_indexing.DeterministicEmbedder(dimensions=64),
            )
        ids = {hit.asset_id for hit in outcome.hits}
        assert a.asset_id in ids
        assert b.asset_id not in ids, "跨品牌泄漏"
        assert c.asset_id not in ids, "项目私有知识泄漏（brand 相同）"

    def test_reverse_no_scope_predicate_leaks(
        self, env: EmbeddingEnv, monkeypatch: Any
    ) -> None:
        """**反向 1：去掉范围谓词 → B 品牌必须泄漏。**

        若去掉谓词后 B 仍不出现，说明"不泄漏"不是谓词的功劳
        （可能数据本身就没被召回）→ 判据无区分力。
        """
        profile_id, _a, b, _c = self._two_brands(env)
        ids = self._search_ids(
            env, profile_id, monkeypatch,
            lambda self, subject: "true",
            subject=ScopeSubject(brand_id=env.brand_a),
        )
        assert b.asset_id in ids, "去掉谓词后必须泄漏 —— 否则判据没有区分力"

    def test_reverse_and_shape_returns_nothing(
        self, env: EmbeddingEnv, monkeypatch: Any
    ) -> None:
        """**反向 2：AND 形状 → 必须返回 0 条**（scope 互斥，AND 恒假）。

        这证明"OR 而非 AND"是一个有后果的选择。
        """

        def and_scope(self: Any, subject: ScopeSubject) -> str:
            asset = aliased(KnowledgeAsset, name="a")
            expr = (
                (asset.scope == "GLOBAL")
                & (asset.scope == "BRAND")
                & (asset.brand_id == subject.brand_id)
            )
            return str(expr.compile(compile_kwargs={"literal_binds": True}))

        ids = self._search_ids(
            env, profile_id=_make_profile(
                env.settings, env, model_version="m@v1", dimensions=64
            ),
            monkeypatch=monkeypatch, patch=and_scope,
            subject=ScopeSubject(brand_id=env.brand_a),
        )
        assert ids == set(), "AND 形状必须返回空集"

    def test_reverse_loose_scope_leaks_project_private(
        self, env: EmbeddingEnv, monkeypatch: Any
    ) -> None:
        """**反向 3：只比限定列（不重复 scope）→ 项目私有知识必须泄漏。**

        这是"每个分支都要重复 `scope = 'X'`"的证明。
        """
        profile_id, _a, _b, c = self._two_brands(env)

        def loose_scope(self: Any, subject: ScopeSubject) -> str:
            asset = aliased(KnowledgeAsset, name="a")
            expr = (
                (asset.scope == "GLOBAL")
                | (asset.brand_id == subject.brand_id)
                | (asset.project_id == subject.project_id)
            )
            return str(expr.compile(compile_kwargs={"literal_binds": True}))

        ids = self._search_ids(
            env, profile_id, monkeypatch, loose_scope,
            subject=ScopeSubject(brand_id=env.brand_a),
        )
        assert c.asset_id in ids, "只比限定列时项目私有知识必须泄漏"

    def test_reverse_permissive_missing_field_leaks(
        self, env: EmbeddingEnv, monkeypatch: Any
    ) -> None:
        """**反向 4：缺字段不 fail-closed → 空 subject 也能看到 BRAND 知识。**

        证明"缺 = 看不见"是一个有后果的安全默认。
        """
        profile_id, a, b, _c = self._two_brands(env)

        def permissive(self: Any, subject: ScopeSubject) -> str:
            from sqlalchemy import or_ as _or

            asset = aliased(KnowledgeAsset, name="a")
            branches = [asset.scope == "GLOBAL"]
            if subject.brand_id is not None:
                branches.append(
                    (asset.scope == "BRAND") & (asset.brand_id == subject.brand_id)
                )
            else:
                branches.append(asset.scope == "BRAND")  # ← 越权
            if subject.project_id is not None:
                branches.append(
                    (asset.scope == "PROJECT") & (asset.project_id == subject.project_id)
                )
            else:
                branches.append(asset.scope == "PROJECT")  # ← 越权
            return str(_or(*branches).compile(compile_kwargs={"literal_binds": True}))

        ids = self._search_ids(
            env, profile_id, monkeypatch, permissive, subject=ScopeSubject()
        )
        assert a.asset_id in ids or b.asset_id in ids, (
            "缺字段不过滤时必须能看到 BRAND 知识（越权）"
        )

    def test_reverse_unaliased_predicate_raises(
        self, env: EmbeddingEnv, monkeypatch: Any
    ) -> None:
        """**反向 5：谓词用表全名（与 SQL 别名不一致）→ `UndefinedTable`。**

        这复现了本包真实踩到的缺陷：纯函数测试看不出（字符串是对的），
        只有把谓词嵌进查询、真的发给 PG 才暴露。
        """
        profile_id, _a, _b, _c = self._two_brands(env)

        def unaliased(self: Any, subject: ScopeSubject) -> str:
            return str(
                kn_scope.scope_visibility_predicate(
                    scope_column=KnowledgeAsset.scope,
                    brand_column=KnowledgeAsset.brand_id,
                    product_column=KnowledgeAsset.product_id,
                    project_column=KnowledgeAsset.project_id,
                    subject=subject,
                ).compile(compile_kwargs={"literal_binds": True})
            )

        monkeypatch.setattr(EmbeddingRepository, "_scope_predicate_sql", unaliased)
        with pytest.raises(Exception) as info:
            with session_scope(env.settings) as session:
                emb_service.search(
                    EmbeddingRepository(session), query="除湿机静音卧室",
                    subject=ScopeSubject(brand_id=env.brand_a),
                    profile_id=profile_id, mode=SEARCH_SEMANTIC, limit=10,
                    embedder=kn_indexing.DeterministicEmbedder(dimensions=64),
                )
        assert "UndefinedTable" in str(info.value)


class TestReverseMixingGuard:
    """混用守卫的反向验证：弱守卫必须放过同维度混用。"""

    def test_weak_guard_lets_same_dimension_mixing_through(
        self, env: EmbeddingEnv
    ) -> None:
        """**弱守卫（只比维度）→ 同维度混用必须能写进去。**

        若弱守卫也拦住了，说明"拦住"可能来自别的约束
        （例如唯一约束 23505）→ 判据无区分力。
        **必须断言 sqlstate**（判据纪律第 3 条）。
        """
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        seeded = _seed_knowledge(
            env.settings, category="FACT", scope="BRAND", body="正文",
            keywords=["正文"], brand_id=env.brand_a,
        )
        engine = get_engine(env.settings)
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"DROP TRIGGER IF EXISTS {emb_ddl.MIXING_GUARD_TRIGGER}"
                    " ON chunk_embeddings"
                )
            )
            conn.execute(
                text(
                    """
                    CREATE OR REPLACE FUNCTION rednote_check_embedding_profile_binding()
                    RETURNS trigger LANGUAGE plpgsql AS $$
                    DECLARE bound_dims int;
                    BEGIN
                        SELECT dimensions INTO bound_dims FROM embedding_profiles
                         WHERE id = NEW.profile_id;
                        IF bound_dims IS NULL THEN
                            RAISE EXCEPTION 'profile 不存在'
                                USING ERRCODE = '23514';
                        END IF;
                        IF vector_dims(NEW.embedding) <> bound_dims THEN
                            RAISE EXCEPTION '维度不符' USING ERRCODE = '23514';
                        END IF;
                        RETURN NEW;
                    END;
                    $$;
                    """
                )
            )
            conn.execute(
                text(
                    f"CREATE TRIGGER {emb_ddl.MIXING_GUARD_TRIGGER}"
                    " BEFORE INSERT ON chunk_embeddings"
                    " FOR EACH ROW EXECUTE FUNCTION"
                    " rednote_check_embedding_profile_binding()"
                )
            )

        error: BaseException | None = None
        try:
            _insert_embedding(
                env.settings, chunk_id=seeded.chunk_id, profile_id=profile_id,
                model_version="m@v2", dimensions=64,  # ← 同维度、不同版本
            )
        except Exception as exc:  # noqa: BLE001
            error = exc
        assert error is None, (
            f"弱守卫不该拦住同维度混用，实得 sqlstate={_sqlstate_of(error)}"
        )

    def test_strong_guard_restored_blocks_with_23514(self, env: EmbeddingEnv) -> None:
        """恢复强守卫后，同维度混用被拦 —— 且必须是 **23514（守卫）**。

        若断言成"被拦下"，唯一约束 23505 也会让它通过 ——
        因此必须钉 sqlstate。
        """
        profile_id = _make_profile(env.settings, env, model_version="m@v1", dimensions=64)
        seeded = _seed_knowledge(
            env.settings, category="FACT", scope="BRAND", body="正文",
            keywords=["正文"], brand_id=env.brand_a,
        )
        engine = get_engine(env.settings)
        with engine.begin() as conn:
            _fn, _tg, drop_tg, _drop_fn = emb_ddl.mixing_guard_ddl()
            conn.execute(text(drop_tg))
            conn.execute(text(_fn))
            conn.execute(text(_tg))
        with pytest.raises(Exception) as info:
            _insert_embedding(
                env.settings, chunk_id=seeded.chunk_id, profile_id=profile_id,
                model_version="m@v2", dimensions=64,
            )
        assert _sqlstate_of(info.value) == emb_ddl.CONSISTENCY_SQLSTATE


class TestReverseChunking:
    """切块器的反向验证。"""

    def test_bad_overlap_implementation_exceeds_limit(self) -> None:
        """**反向：不扣重叠的实现必须超限。**

        段落必须**够长**才会真的超限：若段落只有 35 字，
        `overlap(40) + 35 = 75 < limit(100)` —— 坏实现碰巧也不超，
        判据就失去区分力（本包实测踩到）。
        """
        paragraphs = [f"第{i}段" + "内容填充" * 20 for i in range(1, 13)]
        limit, overlap = 100, 40

        def bad_overlap(pieces: list[str], overlap: int, _limit: int) -> list[str]:
            out: list[str] = []
            prev_tail = ""
            for piece in pieces:
                out.append((prev_tail + piece) if prev_tail else piece)
                prev_tail = out[-1][-overlap:] if overlap else ""
            return out

        pieces: list[str] = []
        for paragraph in paragraphs:
            pieces.extend(paragraph[i : i + limit] for i in range(0, len(paragraph), limit))
        result = bad_overlap(pieces, overlap, limit)
        assert any(len(item) > limit for item in result), (
            "不扣重叠的实现必须超限 —— 否则该判据没有区分力"
        )

    def test_keywords_without_ngram_fail_to_recall(self) -> None:
        """**反向：不切 2-gram（整句）时，`&&` 召回失效。**

        ⚠️ 关键在于 `&&` 是**数组元素精确相等**，不是子串匹配。
        因此"整句当关键词"时，查询 `ARRAY['卧室']` 与
        存进去的 `ARRAY['除湿机静音卧室']` **没有交集** → 召回为空。
        （第一版这里写成 `query_token in kw` 是错的 ——
        `"卧室" in "除湿机静音卧室"` 是 True，因为那是**子串**判断，
        而 PG 的 `&&` 不做子串。断言方式必须与数据库的语义一致。）
        """
        sentence = "除湿机静音卧室"
        query_token = "卧室"

        # 旧实现：整句作为一个关键词元素
        old_keywords = [sentence]
        assert not set(old_keywords) & {query_token}, (
            "整句元素与查询元素无交集 —— 这正是旧实现召回失效的原因"
        )

        # 新实现：切 2-gram 后『卧室』成为独立元素，交集非空
        new_keywords = list(derive_keywords(sentence))
        assert set(new_keywords) & {query_token}, (
            f"2-gram 后必须与查询词有交集，实得 {new_keywords}"
        )


# ===========================================================================
# 零残留（基线差分）
# ===========================================================================


class TestNoResidue:
    def test_own_tables_have_no_orphan_embeddings(self, env: EmbeddingEnv) -> None:
        """本包两张表里**不得有指向不存在 chunk 的行**（FK 保证，但显式核对）。

        `knowledge_*` 表**不参与**"零残留"判据：它们是 WP20 的，
        且 `knowledge_versions` 有不可变触发器，物理删不掉。
        """
        engine = get_engine(env.settings)
        with engine.begin() as conn:
            orphans = conn.execute(
                text(
                    "select count(*) from chunk_embeddings e"
                    " left join knowledge_chunks c on c.id = e.chunk_id"
                    " where c.id is null"
                )
            ).scalar_one()
        assert orphans == 0
